//! Memory carry rotates with a dynamic or zero count. The memory operand remains a read-modify-write at count zero.
use super::*;

// One scalar access, including unaligned RAM within a page, as in translated code. Byte copying would tear even an
// aligned operand. Separate load/store is intentional: RCL/RCR cannot be LOCKed. Barriers preserve x86 load/store order.
pub(super) unsafe fn load(p: *mut u8, n: u64) -> u64 {
    let value: u64;
    unsafe {
        match n {
            1 => asm!("ldrb {v:w}, [{p}]", v = out(reg) value, p = in(reg) p),
            2 => asm!("ldrh {v:w}, [{p}]", v = out(reg) value, p = in(reg) p),
            4 => asm!("ldr {v:w}, [{p}]", v = out(reg) value, p = in(reg) p),
            _ => asm!("ldr {v}, [{p}]", v = out(reg) value, p = in(reg) p),
        }
        asm!("dmb ish");
    }
    value
}

pub(super) unsafe fn store(p: *mut u8, n: u64, value: u64) {
    unsafe {
        asm!("dmb ish");
        match n {
            1 => asm!("strb {v:w}, [{p}]", v = in(reg) value, p = in(reg) p),
            2 => asm!("strh {v:w}, [{p}]", v = in(reg) value, p = in(reg) p),
            4 => asm!("str {v:w}, [{p}]", v = in(reg) value, p = in(reg) p),
            _ => asm!("str {v}, [{p}]", v = in(reg) value, p = in(reg) p),
        }
        asm!("dsb ish");
    }
}

fn memory(
    s: &mut State,
    i: System,
    update: impl FnOnce(u64) -> (u64, u64),
) -> Result<(), segments::Exception> {
    let va = segments::address(&s.cpu, &i);
    let n = u64::from(i.size);
    let nc = segments::noncanonical(&i);
    let last = va.wrapping_add(n - 1);
    if !paging::canonical(va) || !paging::canonical(last) || last < va {
        return Err(nc);
    }
    if s.cpu.alignment_check() && va & (n - 1) != 0 {
        return Err([17, 0, 0]);
    }
    // Walk for the complete write before reading: the first page's write protection wins over a later page's fault.
    let gpa = segments::pages(s, va, n, true, false, nc)?;
    let first = (4096 - (va & 4095)).min(n) as usize;
    if !mmio::ram(gpa[0]) || (first < n as usize && !mmio::ram(gpa[1])) {
        mmio::system_access(s);
    }
    let mut bytes = [0u8; 8];
    let scalar = (first == n as usize)
        .then(|| guest_ram().range(gpa[0], n))
        .flatten();
    if let Some(p) = scalar {
        // SAFETY: the complete RAM operand was walked and range-checked; the resident's alias is Normal memory.
        bytes = unsafe { load(p, n) }.to_le_bytes();
    } else {
        let (lo, hi) = bytes[..n as usize].split_at_mut(first);
        for (at, chunk) in [(gpa[0], lo), (gpa[1], hi)] {
            if !chunk.is_empty() && !guest_ram().copy_out(at, chunk) {
                halt(s, stop::UNSUPPORTED_SYSTEM);
            }
        }
        unsafe { asm!("dmb ish") };
    }
    let (value, flags) = update(u64::from_le_bytes(bytes));
    bytes = (value).to_le_bytes();
    if let Some(p) = scalar {
        if cache::translated(gpa[0]) {
            FetchWrites(s).code_write(gpa[0]);
        }
        // SAFETY: same checked RAM range as the load, still held against peer split-lock exclusion.
        unsafe { store(p, n, value) };
        cache::rechecked(gpa[0]);
    } else {
        unsafe { asm!("dmb ish") };
        let (lo, hi) = bytes[..n as usize].split_at(first);
        for (at, chunk) in [(gpa[0], lo), (gpa[1], hi)] {
            if chunk.is_empty() {
                continue;
            }
            if cache::translated(at) {
                FetchWrites(s).code_write(at);
            }
            if !guest_ram().copy_in(at, chunk) {
                halt(s, stop::UNSUPPORTED_SYSTEM);
            }
            cache::rechecked(at);
        }
    }
    watch::access(s, va, n, true);
    s.cpu.rflags = flags;
    Ok(())
}

/// All flags and destination writes follow the complete operand preflight.
pub(super) fn execute(s: &mut State, i: System) -> Result<(), segments::Exception> {
    let initial_flags = s.cpu.rflags;
    let cl = s.cpu.gpr[1];
    if i.kind == system::DOUBLE_SHIFT16 {
        let count = (cl & 31) as u32;
        // Counts beyond the operand width are architecturally undefined, and retain this profile's refusal.
        if count > 16 {
            return Err([6, 0, 0]);
        }
        let source = s.cpu.gpr[(i.cr & 15) as usize] as u16;
        let update = |value| double_shift(value, source, count, i.cr & 16 != 0, initial_flags);
        if i.memory != 0 {
            return memory(s, i, update);
        }
        let r = i.reg as usize;
        let (value, flags) = update(s.cpu.gpr[r]);
        s.cpu.gpr[r] = s.cpu.gpr[r] & !0xffff | value & 0xffff;
        s.cpu.rflags = flags;
        return Ok(());
    }
    memory(s, i, |value| {
        let bits = i.size * 8;
        let masked = (if i.reg == system::NO_REG {
            i.cr >> 1
        } else {
            cl as u32
        }) & if bits == 64 { 63 } else { 31 };
        let count = masked % (bits + 1);
        let mut flags = initial_flags;
        let mut wide = u128::from(value) | (u128::from(flags & 1) << bits);
        if count != 0 {
            let span = bits + 1;
            wide = if i.cr & 1 != 0 {
                (wide >> count) | (wide << (span - count))
            } else {
                (wide << count) | (wide >> (span - count))
            };
            wide &= (1u128 << span) - 1;
            flags = flags & !1 | (wide >> bits) as u64;
            if masked == 1 {
                let of = if i.cr & 1 != 0 {
                    (wide >> (bits - 1)) ^ (wide >> (bits - 2))
                } else {
                    (wide >> (bits - 1)) ^ (wide >> bits)
                };
                flags = flags & !(1 << 11) | ((of as u64 & 1) << 11);
            }
        }
        (wide as u64, flags)
    })
}

fn double_shift(value: u64, source: u16, count: u32, right: bool, mut flags: u64) -> (u64, u64) {
    if count == 0 {
        return (value, flags);
    }
    let dest = value as u16 as u32;
    let result = if right {
        ((u32::from(source) << 16) | dest) >> count
    } else {
        ((dest << 16) | u32::from(source)) << count >> 16
    } as u16;
    let cf = if right {
        dest >> (count - 1)
    } else {
        dest >> (16 - count)
    } & 1;
    flags = flags & !0xc5
        | u64::from(cf)
        | (u64::from(result == 0) << 6)
        | (u64::from(result >> 15) << 7)
        | (u64::from((result as u8).count_ones() & 1 == 0) << 2);
    if count == 1 {
        flags = flags & !(1 << 11) | (u64::from(((dest as u16 ^ result) >> 15) & 1) << 11);
    }
    (u64::from(result), flags)
}
