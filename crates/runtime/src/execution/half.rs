//! VCVTPS2PH memory destination. Memory faults precede conversion publication; Type 11 does not raise #AC.
use super::*;
use segments::{Exception, address, noncanonical, pages};

pub(super) fn store(s: &mut State, i: System) -> Result<(), Exception> {
    if !s.cpu.avx_enabled() {
        return Err(UD);
    }
    if s.cpu.cr0 & CR0_TS != 0 {
        return Err([7, 0, 0]);
    }
    let va = address(&s.cpu, &i);
    let n = i.size as usize;
    let gpa = pages(s, va, n as u64, true, false, noncanonical(&i))?;
    let head = n.min((4096 - (va & 4095)) as usize);
    if !mmio::ram(gpa[0]) || (head < n && !mmio::ram(gpa[1])) {
        mmio::system_access(s);
    }
    let before = s.cpu.mxcsr;
    let rc = if i.cr & 4 != 0 {
        (before >> 13 & 3) as u32
    } else {
        i.cr & 3
    };
    let r = i.reg as usize;
    let source = [
        s.cpu.xmm[r][0],
        s.cpu.xmm[r][1],
        xsave::ymm_hi(2 * r),
        xsave::ymm_hi(2 * r + 1),
    ];
    let mut bytes = [0u8; 16];
    let mut flags = 0;
    for lane in 0..n / 2 {
        let input = (source[lane / 2] >> (32 * (lane & 1))) as u32;
        let (result, raised) = natemu_abi::half::from_f32(input, rc, before);
        bytes[2 * lane..2 * lane + 2].copy_from_slice(&result.to_le_bytes());
        flags |= raised;
    }
    if flags & simd_unmasked(before) != 0 {
        let raised = simd_fault_flags(fpsr_bits(flags), before);
        s.cpu.mxcsr = before | mxcsr_flags(raised, before);
        return Err(if s.cpu.cr4 & CR4_OSXMMEXCPT != 0 {
            [19, 0, 0]
        } else {
            UD
        });
    }
    // Keep aligned m64 stores indivisible and use the same native pair width as translated m128 stores.
    // Cross-page stores may tear, but neither page is written before the complete access has passed preflight.
    for (pa, count) in [(gpa[0], head), (gpa[1], n - head)] {
        if count != 0 && cache::translated(pa) {
            FetchWrites(s).code_write(pa);
        }
    }
    unsafe { asm!("dmb ish") };
    if head == n {
        let Some(p) = guest_ram().range(gpa[0], n as u64) else {
            halt(s, stop::SHADOW)
        };
        let low = u64::from_le_bytes(bytes[..8].try_into().unwrap());
        let high = u64::from_le_bytes(bytes[8..].try_into().unwrap());
        // SAFETY: the complete operand is writable RAM, held against split-lock exclusion; Normal memory supports
        // unaligned scalar/pair accesses. No host floating-point state or guest source register is modified.
        unsafe {
            if n == 8 {
                asm!("str {v}, [{p}]", v = in(reg) low, p = in(reg) p);
            } else {
                asm!("stp {lo}, {hi}, [{p}]", lo = in(reg) low, hi = in(reg) high, p = in(reg) p);
            }
        }
    } else if !guest_ram().copy_in(gpa[0], &bytes[..head])
        || !guest_ram().copy_in(gpa[1], &bytes[head..n])
    {
        halt(s, stop::SHADOW);
    }
    unsafe { asm!("dsb ish") };
    cache::rechecked(gpa[0]);
    if head < n {
        cache::rechecked(gpa[1]);
    }
    watch::access(s, va, n as u64, true);
    s.cpu.mxcsr = before | flags;
    Ok(())
}
