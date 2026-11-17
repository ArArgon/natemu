//! AFP accurate plan, chunk 3 (section 3): trapped FP exceptions handled in the resident.
//!
//! Generated code runs at EL0 with the FPCR trap enables the mode wants ([`natemu_abi::execution::fpcr_run`]; UFE in accurate mode
//! without AFP). A trapping Arm word is a synchronous exception, EC 0x2C, taken at the lower-EL vector with the destination and FPSR
//! unchanged and ELR at the word. [`trap`] works on that one Arm word, never on the guest instruction (nothing is banked, no step is
//! compiled):
//! 1. the word is read from the code area's writable alias and classified ([`fptrap::classify`]); an EC 0x2C at EL1 (`resident_fault`)
//!    or from EL0 at a PC inside no admitted block or the step block, or an Arm word the table does not know, halts with a named stop
//!    ([`stop::FP_TRAP`], [`stop::FP_WORD`]) and never reaches the decoder;
//! 2. its source registers, FPCR and FPSR go into `Shared::fp_rerun`, whose `active` is the per-vCPU "FP re-run in flight" marker;
//! 3. the word runs once in place: back to ELR with the trap enables cleared in the frame's FPCR (the value captured at the trap, so
//!    ROUND's own rounding mode is kept), FPSR cleared, and the hardware software step armed (MDSCR_EL1.SS, SPSR.SS, IRQs masked);
//! 4. the step exception ([`stepped`]) brings it back: FPSR then holds this word's flags alone; [`fptrap::patch`] decides them by mode
//!    (what is patched depends on the mode's enabled traps, never on the cause in ESR), they are ORed into the FPSR from before the
//!    trap, and the trap enables are put back in the frame's FPCR.
//!
//! AFP accurate plan chunk 4 (IOE, NaN lanes): in accurate mode the trap enables also hold IOE. The re-run's patch then also decides the NaN
//! lanes and an FMA's IOC by FPCR.AH = 1's rules ([`fptrap::patch`]'s `nan`; the mode, never the cause in ESR: `FPCR_IOE` in the trapped
//! FPCR). An FMA's result is also tested by a check word, `fcmge vD, vR, #0` (a site of kind FMA): it traps on any NaN lane, and there no
//! re-run is needed: the word is skipped after the NaN lanes of R are recomputed from the three operand registers the site names
//! ([`fptrap::fma_check`]).
//!
//! The resident already single-steps for watchpoints (`watch`), which assumes every step is one: it re-arms MDE and restores nothing
//! in FPCR. The marker tells the two apart ([`returned`] dispatches on it); MDE is left as it was here, so a watchpoint armed while an
//! FP word re-runs stays armed; [`cancel`] (any other entry while the marker is set) restores the trap enables and the step state, so
//! a later entry never resumes with the trap-free FPCR of the re-run. The counts (`Control::fp_traps`, `fp_steps`) are apart from
//! `debug_steps` and `watch_hits`.
use super::*;
use natemu_abi::{FPTRAPS_MASK, fptrap};

const MDSCR_SS: u64 = 1;
const SPSR_SS: u64 = 1 << 21;
const SPSR_I: u64 = 1 << 7;
/// ESR_EL1.EC of a trapped floating-point exception.
pub(super) const EC_FP_TRAP: u64 = 0x2c;
/// ESR_EL1.EC of a software step from a lower EL.
pub(super) const EC_STEP: u64 = 0x32;

fn rerun() -> *mut FpRerun {
    // SAFETY: the shared page is this vCPU's; only the resident touches the marker.
    unsafe { &raw mut (*shared()).fp_rerun }
}

/// MDSCR_EL1.SS on or off; MDE (watchpoints) is never touched.
fn step(on: bool) {
    // SAFETY: debug control of this PE.
    unsafe {
        asm!("mrs {t}, mdscr_el1", "bic {t}, {t}, #{ss}", "orr {t}, {t}, {on}", "msr mdscr_el1, {t}", "isb",
             t = out(reg) _, ss = const MDSCR_SS, on = in(reg) u64::from(on));
    }
}

/// Whether an FP word's re-run step is in flight on this vCPU.
#[inline(always)]
pub(super) fn active() -> bool {
    // SAFETY: as `rerun`.
    unsafe { core::ptr::read_volatile(&raw const (*rerun()).active) != 0 }
}

/// The resident starts: no re-run is in flight.
pub(super) fn reset() {
    // SAFETY: as `rerun`.
    unsafe { core::ptr::write_volatile(&raw mut (*rerun()).active, 0) };
}

/// A trapped FP exception from EL0 (EC 0x2C): `f` is the gate's frame (the state at the trap), `esr` and `elr` the exception's. Never
/// returns: the word is re-run by a software step, or the vCPU halts.
#[inline(never)]
pub(super) unsafe fn trap(f: &NativeFrame, esr: u64, elr: u64, spsr: u64) -> ! {
    unsafe {
        let stop_at = |word: u64, at: u64, reason: u64| -> ! { halt_native([esr, word, at], reason) };
        let Some(b) = cache::block_at(elr) else {
            stop_at(0, elr, stop::FP_TRAP)
        };
        let at = elr - b.code_va();
        // The FMA NaN check: a site of kind FMA at this very word (the compare itself is skipped, no flag is added).
        let sites = cache::sites(b);
        if let Ok(i) = sites.binary_search_by_key(&at, |site| u64::from(site.native_offset)) {
            let site = sites[i];
            let word = ((natemu_abi::execution::cache::CODE_WRITE_VA + (elr - cache::CODE_VA)) as *const u32).read_volatile();
            let Some(d) = natemu_abi::tiny::Desc::decode(site.width, site.imm).filter(|d| site.kind == natemu_abi::code_object::kind::FMA && d.meta & 7 == natemu_abi::tiny::FMA) else {
                stop_at(u64::from(word), at, stop::FP_WORD)
            };
            // The word must be `fcmge v1, vR, #0` of the descriptor's shape (vector .4s/.2d or scalar s/d): a misplaced site would skip an FMA.
            let sz = u32::from(d.meta & natemu_abi::tiny::ELEM8 != 0) << 22;
            let shape = if d.meta & natemu_abi::tiny::SCALAR != 0 { 0x7ea0_c800 } else { 0x6ea0_c800 } | sz | 1;
            if word & 0xffff_fc1f != shape || word >> 5 & 31 < 2 {
                stop_at(u64::from(word), at, stop::FP_WORD)
            }
            let frame = &raw mut (*shared()).captured;
            let q = [(*frame).q[usize::from(d.regs[0])], (*frame).q[usize::from(d.regs[1])], (*frame).q[usize::from(d.regs[2])]];
            let wide = d.meta & natemu_abi::tiny::ELEM8 != 0;
            let lanes = if d.meta & natemu_abi::tiny::SCALAR != 0 { 1 } else if wide { 2 } else { 4 };
            // the result register: the compare's source
            fptrap::fma_check(wide, lanes, &q, &mut (*frame).q[(word >> 5 & 31) as usize]);
            cache::control().fp_traps += 1;
            let s = load();
            resume(&s, frame, elr + 4, spsr)
        }
        // The word, from the code area's writable alias (the executable one is not readable at EL1's choice of attributes).
        let word = ((natemu_abi::execution::cache::CODE_WRITE_VA + (elr - cache::CODE_VA)) as *const u32).read_volatile();
        let Some(class) = fptrap::classify(word) else {
            stop_at(u64::from(word), at, stop::FP_WORD)
        };
        let r = rerun();
        (*r).word = u64::from(word);
        (*r).fpcr = f.fpcr;
        (*r).fpsr = f.fpsr;
        if let fptrap::Class::Patch(op) = class {
            for (k, reg) in op.sources().into_iter().enumerate() {
                (*r).q[k] = f.q[usize::from(reg)];
            }
        }
        core::ptr::write_volatile(&raw mut (*r).active, 1);
        let c = cache::control();
        c.fp_traps += 1;
        // The re-run: the same registers with the enables off and FPSR clear.
        let resume_frame = &raw mut (*shared()).captured;
        (*resume_frame).fpcr = f.fpcr & !FPTRAPS_MASK;
        (*resume_frame).fpsr = 0;
        step(true);
        let s = load();
        resume(&s, resume_frame, elr, spsr | SPSR_SS | SPSR_I)
    }
}

/// The software step after the re-run (EC 0x32 from EL0, marker set): FPSR is this word's flags, patched, and ORed into the guest's.
#[inline(never)]
pub(super) unsafe fn stepped(elr: u64, spsr: u64) -> ! {
    unsafe {
        step(false);
        let r = rerun();
        let frame = &raw mut (*shared()).captured;
        let word = (*r).word as u32;
        let Some(class) = fptrap::classify(word) else {
            halt_native([0, u64::from(word), elr], stop::FP_WORD)
        };
        let s = load();
        // FTZ alone (a trap with DAZ and FTZ both would be none: FZ = 1 never traps): the block was compiled under the key of the MXCSR
        // the bank holds (LDMXCSR ends a block), so its effective FTZ is that value's; the patch then also flushes a band lane's value.
        let ftz = ftz_key(s.cpu.mxcsr, true) != 0;
        let rd = match class {
            fptrap::Class::Patch(op) => usize::from(op.rd),
            fptrap::Class::Keep => 0,
        };
        let nan = (*r).fpcr & natemu_abi::execution::FPCR_IOE != 0;
        // FZ = 1 (DAZ and FTZ both: the hardware flush; UFE never traps then, only IOE does): the re-run's UFC is the flush's, which stays as
        // the hardware gives it (chunk 5 decides the rest), so the UFC model is not run.
        let class = match class {
            fptrap::Class::Patch(o) if (*r).fpcr >> 24 & 1 != 0 => fptrap::Class::Patch(fptrap::Op { kind: 0, ..o }),
            c => c,
        };
        let flags = fptrap::patch(class, fptrap::x86_rc((*r).fpcr), &(*r).q, (*frame).fpsr, ftz, &mut (*frame).q[rd], nan);
        (*frame).fpsr = (*r).fpsr | flags;
        (*frame).fpcr = (*r).fpcr;
        core::ptr::write_volatile(&raw mut (*r).active, 0);
        cache::control().fp_steps += 1;
        resume(&s, frame, elr, spsr & !(SPSR_SS | SPSR_I))
    }
}

/// At every exception entry, before the gate's frame is copied (`finish`): an FP re-run still in flight that this entry is not the step
/// of (the word did not complete: an abort or an interrupt came first, which the masked step makes unreachable in practice) is abandoned
/// the way a watchpoint step is. The word will trap again when it runs again; meanwhile the frame holds the guest's own FPCR and FPSR,
/// not the trap-free re-run's, and the step state and the IRQ mask it added to the saved SPSR are gone.
#[inline(always)]
pub(super) unsafe fn cancel(frame: *mut NativeFrame, kind: u64) {
    if !active() {
        return;
    }
    unsafe { abandon(frame, kind) }
}
#[inline(never)]
unsafe fn abandon(frame: *mut NativeFrame, kind: u64) {
    unsafe {
        let esr: u64;
        asm!("mrs {0}, esr_el1", out(reg) esr);
        if kind != 3 && esr >> 26 == EC_STEP {
            return; // the step itself
        }
        let r = rerun();
        step(false);
        (*frame).fpcr = (*r).fpcr;
        (*frame).fpsr = (*r).fpsr;
        core::ptr::write_volatile(&raw mut (*r).active, 0);
        let spsr: u64;
        asm!("mrs {0}, spsr_el1", out(reg) spsr);
        asm!("msr spsr_el1, {0}", in(reg) spsr & !(SPSR_SS | SPSR_I));
    }
}
