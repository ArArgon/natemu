//! S4: x86 data and I/O breakpoints (SDM Vol.3 18.2.4, 18.2.5, 18.3.1.2): trap-class #DB after the instruction whose
//! access met an armed range (`State::data_hits`, added to `State::debug_trap` when the instruction completes).
//!
//! **Generated code** is watched natively: each armed data breakpoint is one Arm watchpoint (DBGWVRn/DBGWCRn, EL0 only),
//! programmed only while DR7 arms one ([`program`]). Shadows map x86 linear addresses at the same EL0 virtual addresses,
//! and an aligned x86 range of 1, 2, 4 or 8 bytes is a byte mask (BAS) of one doubleword, so any access touching a watched
//! byte stops before it is made. [`hit`]: in an ordinary block, the instruction is run again alone from its start (the
//! site's exact state, as for a fault); in that step block every hit adds the bits the site's access meets by the x86
//! rule, then the one Arm access runs with watchpoints off under a hardware software step (IRQs masked), and the step
//! exception ([`stepped`]) turns them back on. Any other exception taken meanwhile (a shadow miss, a split store) cancels
//! the step ([`cancel`]); the access is made again later, watched.
//!
//! **The resident's own accesses** are checked in software where they are made: `segments::linear` for every explicit
//! access (POPF, IRET and far-return pops, LGDT/LIDT operands, SGDT/SIDT/STR/SLDT stores, LDMXCSR/STMXCSR, FXSAVE/XSAVE
//! images, x87 environment, state and m80 images, OUTS sources), INS destinations, a split store, a device completion,
//! BT* units (cut while data breakpoints are armed: FEX touches only the byte holding the bit); implicit supervisor
//! accesses (descriptor tables, the TSS, the IDT, page-table walks and their A/D writes, descriptor accessed and busy
//! bits) never count. I/O breakpoints (R/W 10 with CR4.DE) are checked on the port of IN, OUT, INS and OUTS.
use super::*;

const MDSCR_SS: u64 = 1;
const MDSCR_MDE: u64 = 1 << 15;
const SPSR_SS: u64 = 1 << 21;
const SPSR_I: u64 = 1 << 7;

/// Programs the Arm watchpoints from DR0-DR3/DR7 (after every MOV to a debug register): one per armed data breakpoint,
/// EL0 accesses only (PAC 10), stores for R/W 01 and loads or stores for 11 (LSC), its range's bytes in its doubleword
/// (BAS). MDSCR_EL1.MDE is set only while one is armed.
pub(super) fn program(s: &mut State) {
    let c = s.cpu;
    // ID_AA64DFR0_EL1.WRPs: the watchpoints this PE implements, less one (the M4 has 4; a data breakpoint beyond them
    // would be refused).
    let dfr0: u64;
    // SAFETY: an ID register read; no effect.
    unsafe { asm!("mrs {0}, id_aa64dfr0_el1", out(reg) dfr0) };
    let watchpoints = (dfr0 >> 20 & 0xf) + 1;
    cache::control().watchpoints = watchpoints;
    let mut armed = false;
    for n in 0..4 {
        let rw = dr7_rw(c.dr[5], n);
        // A non-canonical range meets no x86 access (and a watchpoint there could only match the resident's private
        // window or the guest-state page): not programmed.
        let (wvr, wcr) = if dr7_enabled(c.dr[5], n) && rw & 1 != 0 && paging::canonical(dr_range(&c.dr, n).0) {
            if n as u64 >= watchpoints {
                halt(s, stop::UNSUPPORTED_SYSTEM)
            }
            armed = true;
            let (base, len) = dr_range(&c.dr, n);
            let bas = ((1u64 << len) - 1) << (base & 7);
            (base & !7, 1 | 2 << 1 | (if rw == 1 { 2 } else { 3 }) << 3 | bas << 5)
        } else {
            (0, 0)
        };
        // SAFETY: debug registers the VM owns (the host turned their traps off); no memory effect.
        unsafe {
            match n {
                0 => asm!("msr S2_0_C0_C0_7, xzr", "msr S2_0_C0_C0_6, {v}", "msr S2_0_C0_C0_7, {c}", v = in(reg) wvr, c = in(reg) wcr),
                1 => asm!("msr S2_0_C0_C1_7, xzr", "msr S2_0_C0_C1_6, {v}", "msr S2_0_C0_C1_7, {c}", v = in(reg) wvr, c = in(reg) wcr),
                2 => asm!("msr S2_0_C0_C2_7, xzr", "msr S2_0_C0_C2_6, {v}", "msr S2_0_C0_C2_7, {c}", v = in(reg) wvr, c = in(reg) wcr),
                _ => asm!("msr S2_0_C0_C3_7, xzr", "msr S2_0_C0_C3_6, {v}", "msr S2_0_C0_C3_7, {c}", v = in(reg) wvr, c = in(reg) wcr),
            }
        }
    }
    // SAFETY: as above. (OSLAR_EL1 traps to the host under HVF; the OS lock is found clear: the watchpoints fire.)
    unsafe {
        asm!("mrs {t}, mdscr_el1", "bic {t}, {t}, #{mde}", "orr {t}, {t}, {on}", "msr mdscr_el1, {t}", "isb",
             t = out(reg) _, mde = const MDSCR_MDE, on = in(reg) if armed { MDSCR_MDE } else { 0 });
    }
}

/// Whether the access of `site` is a write for DR purposes (a store, push, exchange or atomic read-modify-write).
fn writes(site: &Site) -> bool {
    use object::kind::*;
    matches!(site.kind, STORE | STORE_PAIR | PUSH | PUSH_PAIR | SWAP | ATOMIC)
}

/// A watchpoint exception at `site` of `b` (EC 0x34, from EL0): the access has not been made.
pub(super) fn hit(mut s: State, f: &NativeFrame, b: &Block, site: &Site, elr: u64, spsr: u64) -> ! {
    cache::control().watch_hits += 1;
    // In an ordinary block the instruction runs again alone, so the trap follows it exactly.
    if b.flags & cache::STEP == 0 {
        step_from(s, f, b, site)
    }
    s.debug |= data_hits(&s.cpu.dr, site_address(site, f), u64::from(site.width), writes(site)) << debug::HITS;
    unsafe { write_record(record(), &s) };
    // The one Arm access with watchpoints off: MDE clear, a software step of the next instruction from EL0, IRQs masked.
    // SAFETY: debug control of this PE; `resume` erets with the given SPSR.
    unsafe { asm!("mrs {t}, mdscr_el1", "bic {t}, {t}, #{mde}", "orr {t}, {t}, #{ss}", "msr mdscr_el1, {t}", "isb", t = out(reg) _, mde = const MDSCR_MDE, ss = const MDSCR_SS) };
    unsafe { resume(&s, &raw const (*shared()).captured, elr, spsr | SPSR_SS | SPSR_I) }
}

/// The software step after a watched access completed (EC 0x32 from EL0): watchpoints on again, the step block goes on.
pub(super) fn stepped(elr: u64, spsr: u64) -> ! {
    rearm();
    let s = unsafe { load() };
    unsafe { resume(&s, &raw const (*shared()).captured, elr, spsr & !(SPSR_SS | SPSR_I)) }
}

/// At every other entry from generated code: a step still in flight (an abort or a split store came first: it was taken
/// before the stepped access completed, so its SPSR.SS is 1, never otherwise) is abandoned, its IRQ mask dropped from the
/// saved SPSR; the access is made again, watched.
#[inline(always)]
pub(super) fn cancel(spsr: u64) -> u64 {
    if spsr & SPSR_SS == 0 {
        return spsr;
    }
    rearm();
    let spsr = spsr & !(SPSR_SS | SPSR_I);
    // SAFETY: the exception being served returns through SPSR_EL1, which its handler may read again.
    unsafe { asm!("msr spsr_el1, {0}", in(reg) spsr) };
    spsr
}
fn rearm() {
    // SAFETY: debug control of this PE (a step only runs while watchpoints are armed).
    unsafe { asm!("mrs {t}, mdscr_el1", "bic {t}, {t}, #{ss}", "orr {t}, {t}, #{mde}", "msr mdscr_el1, {t}", "isb", t = out(reg) _, ss = const MDSCR_SS, mde = const MDSCR_MDE) };
}

/// The resident's own explicit access of `width` bytes at linear `va` (made, or about to be made by a committing
/// instruction): its data breakpoint bits.
#[inline(always)]
pub(super) fn access(s: &mut State, va: u64, width: u64, write: bool) {
    if s.cpu.dr[5] & DR7_ENABLES != 0 {
        s.debug |= data_hits(&s.cpu.dr, va, width, write) << debug::HITS;
    }
}
/// The bits of the armed I/O breakpoints (R/W 10, CR4.DE) a port access of `width` bytes meets (SDM Vol.3 18.2.4: R/W 10
/// is undefined without DE, and refused at the DR7 write).
pub(super) fn port(s: &mut State, port: u64, width: u64) {
    if s.cpu.dr[5] & DR7_ENABLES == 0 || s.cpu.cr4 & CR4_DE == 0 {
        return;
    }
    s.debug |= (0..4).filter(|&n| dr7_enabled(s.cpu.dr[5], n) && dr7_rw(s.cpu.dr[5], n) == 2).fold(0, |m, n| {
        let (base, len) = dr_range(&s.cpu.dr, n);
        m | u64::from(base.wrapping_sub(port) < width || port.wrapping_sub(base) < len) << n
    }) << debug::HITS;
}
