//! L3, port I/O (SDM Vol.2 IN, OUT, INS, OUTS, REP; Vol.1 19 "Input/Output"), executed as resident system transactions
//! (K2: decoded once by the host, kept, then run with no decode exit). **Protection** (Vol.1 19.5): at CPL > IOPL every
//! port byte the access touches must have a 0 bit in the TSS's I/O permission bit map, else #GP(0): TR must hold a present
//! 64-bit TSS (type 9 or busy 11) with a limit of at least 67H; the map's base is the word at TSS + 66H; the processor
//! reads the two bytes holding the bits at base + port / 8, both within the TSS limit (QEMU's and Bochs's `check_io`
//! read the same two bytes; a map covering every port must therefore be followed by a byte of ones, Vol.1 19.5.2). The
//! TSS reads are implicit supervisor accesses through the walker (a #PF there is delivered). **Ports** are 16 bits: DX's
//! low word or the imm8; a wide access touches port .. port + size - 1, each byte modulo 10000H on the host's bus
//! (`devices::Devices::port`). **Accesses** go to the host ([`REQUEST_PORT`], the [`Mmio`] record with the port in
//! `gpa`) unless [`local`] answers: one host exit per access, the device performing it exactly once under its lock; every
//! earlier memory access of this vCPU is complete before it (`dsb sy`) and every later one starts after the reply, so an
//! I/O instruction is ordered with memory both ways (Vol.3 10.2.2: reads and writes are not reordered with I/O
//! instructions). **INS/OUTS** move one element per transaction (I2's rule: between elements RCX/RSI/RDI are exact and RIP
//! stays at the instruction, `State::mid_string` set, so events and stops are taken between elements with RF = 1, and
//! the next element runs from the kept decode). INS writes ES:RDI, OUTS reads DS:RSI (an FS/GS override adds its base);
//! 0x67 uses ESI/EDI/ECX (a write zero-extends); DF steps down. INS walks its destination (canonical #GP, #AC, #PF for a
//! write, a device page refused) before the port is read, so a fault on the memory side reads nothing (the SDM lets INS
//! read the port and fault on the write; this order never does). A REP with a zero count makes the I/O permission check
//! (#GP(0) when denied: QEMU's and Bochs's order, the permission check belonging to the instruction, not to an iteration),
//! then does nothing.
//! **Refused** (typed): an INS/OUTS element on a device page (`unsupported_mmio`, status `system`, before the port access);
//! a port access the host refuses (`unsupported_port`: an unassigned port under the strict switch, or a device's refusal),
//! with the state at the instruction after the elements done.
use super::segments::{self, Exception};
use super::*;
use natemu_abi::execution::{
    MMIO_VA, Mmio, REPLY_PORT, REPLY_PORT_POWER, REPLY_POWER_POLL, REQUEST_PORT,
    REQUEST_POWER_POLL, mmio as status,
};

const GP0: Exception = [13, 0, 0];
const AC0: Exception = [17, 0, 0];

fn mask(size: u64) -> u64 {
    (1 << (8 * size)) - 1
}

/// The I/O permission check of an access to `size` bytes from `port` (Vol.1 19.5): `Err(#GP(0))` when it is denied.
fn permitted(s: &mut State, port: u64, size: u64) -> Result<(), Exception> {
    let c = s.cpu;
    if c.cpl <= (c.rflags >> 12) & 3 {
        return Ok(());
    }
    let tr = c.tr;
    // Present, a system segment, type 9 or 11 (bit 1 is busy).
    if tr.attributes & 0x9d != 0x89 || tr.limit < 0x67 {
        return Err(GP0);
    }
    let mut b = [0u8; 2];
    segments::linear(s, tr.base.wrapping_add(0x66), &mut b, false, true, GP0)?;
    let offset = u64::from(u16::from_le_bytes(b)) + port / 8;
    if offset + 1 > u64::from(tr.limit) {
        return Err(GP0);
    }
    segments::linear(s, tr.base.wrapping_add(offset), &mut b, false, true, GP0)?;
    if (u64::from(u16::from_le_bytes(b)) >> (port & 7)) & ((1 << size) - 1) != 0 {
        return Err(GP0);
    }
    Ok(())
}

/// Resident port devices (the hook for hot ports, as `mmio::local` is for device pages): `None` sends the access to the
/// host. None today.
fn local(_s: &mut State, _port: u64, _size: u64, _data: Option<u64>) -> Option<u64> {
    None
}

/// One access to `size` bytes from `port` (a write of `data`, or a read, whose value is returned).
fn access(s: &mut State, port: u64, size: u64, data: Option<u64>) -> (u64, bool) {
    if let Some(v) = local(s, port, size, data) {
        return (v, false);
    }
    let exchange = MMIO_VA as *mut Mmio;
    unsafe {
        let completed = read_record(exchange).completed;
        let request = Mmio {
            gpa: port,
            width: size,
            write: u64::from(data.is_some()),
            value: data.unwrap_or(0),
            completed,
            ..Mmio::default()
        };
        write_record(exchange, &request);
        (s.request, s.reply) = (REQUEST_PORT, NONE);
        write_record(record(), s);
        // Every earlier access of this vCPU is complete before the device sees this one.
        let p = crate::perf::enter(crate::perf::HOST);
        asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
        crate::perf::leave(p);
        *s = load();
        s.request = NONE;
        let m = read_record(exchange);
        if !matches!(s.reply, REPLY_PORT | REPLY_PORT_POWER) {
            halt(s, stop::BAD_REPLY)
        }
        match m.status {
            status::DONE => (m.value & mask(size), s.reply == REPLY_PORT_POWER),
            status::UNASSIGNED | status::DEVICE => halt(s, stop::UNSUPPORTED_PORT),
            _ => halt(s, stop::BAD_REPLY),
        }
    }
}

/// Executes IN, OUT, INS or OUTS (one element), committing it: `Err` with the exception due at the instruction.
pub(super) fn transaction(s: &mut State, i: System) -> Result<(), Exception> {
    let c = s.cpu;
    let size = u64::from(i.size);
    if matches!(i.kind, system::IN | system::OUT) {
        let port = if i.reg == system::NO_REG {
            u64::from(i.cr)
        } else {
            c.gpr[2] & 0xffff
        };
        permitted(s, port, size)?;
        super::watch::port(s, port, size);
        let power;
        if i.kind == system::IN {
            let (v, requested) = access(s, port, size, None);
            power = requested;
            let rax = &mut s.cpu.gpr[0];
            *rax = if size == 4 { v } else { *rax & !mask(size) | v };
        } else {
            power = access(s, port, size, Some(c.gpr[0] & mask(size))).1;
        }
        commit(s, i, false);
        if power {
            power_wait(s);
        }
        return Ok(());
    }
    let rep = i.prefixes & system::REP != 0;
    let wide = if i.prefixes & system::ADDRESS32 != 0 {
        0xffff_ffff
    } else {
        u64::MAX
    };
    let port = c.gpr[2] & 0xffff;
    permitted(s, port, size)?;
    if rep && c.gpr[1] & wide == 0 {
        commit(s, i, false);
        return Ok(());
    }
    let reg = if i.kind == system::INS { 7 } else { 6 };
    let offset = c.gpr[reg] & wide;
    let base = match i.segment as usize {
        SREG_FS => c.fs_base,
        SREG_GS => c.gs_base,
        _ => 0,
    };
    let va = offset.wrapping_add(base);
    let mut bytes = [0u8; 4];
    let element = &mut bytes[..size as usize];
    let power;
    if i.kind == system::INS {
        // The destination's faults first, as an explicit write would raise them (linear's order: canonical, #AC, walk),
        // and a device page refused, all before the port is read.
        let last = va.wrapping_add(size - 1);
        if alignment_check(&s.cpu) != 0
            && va & (size - 1) != 0
            && paging::canonical(va)
            && paging::canonical(last)
            && last >= va
        {
            return Err(AC0);
        }
        let gpa = segments::pages(s, va, size, true, false, GP0)?;
        if !super::mmio::ram(gpa[0]) || ((last ^ va) >> 12 != 0 && !super::mmio::ram(gpa[1])) {
            super::mmio::system_access(s)
        }
        let (v, requested) = access(s, port, size, None);
        power = requested;
        element.copy_from_slice(&v.to_le_bytes()[..size as usize]);
        // Written through the translation just walked (as a TLB entry would serve it), with linear's code-write protocol.
        let (low, high) = element.split_at((4096 - (va & 4095)).min(size) as usize);
        for (gpa, chunk) in [(gpa[0], low), (gpa[1], high)] {
            if chunk.is_empty() {
                continue;
            }
            if cache::translated(gpa) {
                FetchWrites(s).code_write(gpa);
            }
            if !guest_ram().copy_in(gpa, chunk) {
                halt(s, stop::UNSUPPORTED_SYSTEM)
            }
            cache::rechecked(gpa);
        }
        super::watch::access(s, va, size, true);
    } else {
        segments::linear(s, va, element, false, false, GP0)?;
        power = access(s, port, size, Some(u64::from(u32::from_le_bytes(bytes)))).1;
    }
    super::watch::port(s, port, size);
    let step = if c.rflags & RFLAGS_DF != 0 {
        size.wrapping_neg()
    } else {
        size
    };
    s.cpu.gpr[reg] = offset.wrapping_add(step) & wide;
    if rep {
        let count = (c.gpr[1] & wide) - 1;
        s.cpu.gpr[1] = count;
        if count != 0 {
            // Between two elements: RIP stays at the instruction (its kept decode runs the next element); RF stays (S4).
            s.mid_string = 1;
            commit(s, System { length: 0, ..i }, false);
            if power {
                power_wait(s);
            }
            return Ok(());
        }
    }
    commit(s, i, false);
    if power {
        power_wait(s);
    }
    Ok(())
}

/// The port instruction (or one REP element) has committed; no guest continuation survives.
fn power_wait(s: &mut State) -> ! {
    reset_shadows(s);
    loop {
        io_checkpoint(s);
        unsafe {
            (s.request, s.reply) = (REQUEST_POWER_POLL, NONE);
            write_record(record(), s);
            asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
            *s = load();
        }
        s.request = NONE;
        if s.reply != REPLY_POWER_POLL {
            halt(s, stop::BAD_REPLY);
        }
    }
}
