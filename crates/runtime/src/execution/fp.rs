//! AFP accurate plan, chunks 3-6b (section 3): trapped FP exceptions handled in the resident.
//!
//! Generated code runs at EL0 with the FPCR trap enables the mode wants ([`natemu_abi::execution::fpcr_run`]; UFE in accurate mode
//! without AFP). A trapping Arm word is a synchronous exception, EC 0x2C, taken at the lower-EL vector with the destination and FPSR
//! unchanged and ELR at the word. [`trap`] works on that one Arm word, never on the guest instruction (nothing is banked, no step is
//! compiled), on the gate's frame in place (`frame_only`: no copy of it):
//! 1. the word is read from the code area's writable alias and classified ([`fptrap::classify`]); an EC 0x2C at EL1 (`resident_fault`)
//!    or from EL0 at a PC inside no admitted block or the step block, or an Arm word the table does not know, halts with a named stop
//!    ([`stop::FP_TRAP`], [`stop::FP_WORD`]) and never reaches the decoder;
//! 2. **the table path** (chunk 6b, every [`fptrap::Class::Patch`] word): the operation is performed here, in the resident, by the
//!    word's entry in a fixed table of pre-assembled instructions ([`fptrap::TABLE`], in this image's text: read-only and executable, never written;
//!    the entry is the word with its registers renamed v0..v3, so arrangement, element size, by-element index and form are the guest's)
//!    on registers loaded from the frame, under the trapped FPCR with the trap enables cleared and FPSR cleared ([`fptrap::exec`]), the
//!    result stored to the frame's Rd: what the hardware's re-run gave, with no second exception;
//! 3. [`fptrap::patch_flush`] then decides the flags and lanes by mode (what is patched depends on the mode's enabled traps, never on
//!    the cause in ESR), the flags are ORed into the FPSR from before the trap, and execution resumes after the word. The frame's FPCR
//!    was never changed: the trap enables are in force on every path;
//! 4. **the step fallback** ([`fptrap::Class::Keep`] words: compares, conversions to integer, min/max, roundings, widening converts and
//!    the like, which trap only for invalid operands and whose result is the hardware's anyway): the word's source registers, FPCR and
//!    FPSR go into `Shared::fp_rerun` (its `active` is the per-vCPU "FP re-run in flight" marker), and the word runs once in place: back
//!    to ELR with the trap enables cleared in the frame's FPCR (the value captured at the trap, so ROUND's own rounding mode is kept),
//!    FPSR cleared, and the hardware software step armed (MDSCR_EL1.SS, SPSR.SS, IRQs masked). The step exception ([`stepped`]) brings it
//!    back: FPSR then holds this word's flags alone, they are patched, ORed into the FPSR from before the trap, and the trap enables are
//!    put back in the frame's FPCR.
//!
//! AFP accurate plan chunk 4 (IOE, NaN lanes): in accurate mode the trap enables also hold IOE. The patch then also decides the NaN
//! lanes and an FMA's IOC by FPCR.AH = 1's rules ([`fptrap::patch`]'s `nan`; the mode, never the cause in ESR: `FPCR_IOE` in the trapped
//! FPCR). An FMA's result is also tested by a check word, `fcmge vD, vR, #0` (a site of kind FMA): it traps on any NaN lane, and there no
//! re-run is needed: the word is skipped after the NaN lanes of R are recomputed from the three operand registers the site names
//! ([`fptrap::fma_check`]).
//!
//! AFP accurate plan chunk 5 (DAZ + FTZ): accurate mode never runs FZ = 1; with DAZ and FTZ both on, UFE traps every tiny result (exact ones
//! too) and the patch flushes it to x86's FTZ result ([`fptrap::FLUSH_ALL`], chosen from the MXCSR the block was compiled under); fexc's
//! row 4 has flushed the inputs.
//!
//! The resident already single-steps for watchpoints (`watch`), which assumes every step is one: it re-arms MDE and restores nothing
//! in FPCR. The marker tells the two apart ([`returned`] dispatches on it); MDE is left as it was here, so a watchpoint armed while an
//! FP word re-runs stays armed; [`cancel`] (any other entry while the marker is set) restores the trap enables and the step state, so
//! a later entry never resumes with the trap-free FPCR of the re-run. The counts (`Control::fp_traps`, `fp_steps`) are apart from
//! `debug_steps` and `watch_hits`; `fp_steps` counts the fallback's steps only, so `fp_traps - fp_steps` words went through the table.
use super::*;
use natemu_abi::{FPTRAPS_MASK, fptrap};

const MDSCR_SS: u64 = 1;
const SPSR_SS: u64 = 1 << 21;
const SPSR_I: u64 = 1 << 7;
/// ESR_EL1.EC of a trapped floating-point exception.
pub(super) const EC_FP_TRAP: u64 = 0x2c;
/// ESR_EL1.EC of a software step from a lower EL.
pub(super) const EC_STEP: u64 = 0x32;
/// The instruction table of [`fptrap::exec`], in the image's text (R+X, never written).
#[unsafe(link_section = ".text.fptab")]
#[used]
static TABLE: [u32; 2 * fptrap::SLOTS] = fptrap::TABLE;

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

/// A trapped FP exception from EL0 (EC 0x2C): `f` is the gate's frame (the state at the trap; `frame_only`: not copied, and resumed
/// from in place), `esr` and `elr` the exception's. Never returns: the word is performed from the table, or re-run by a software step, or
/// the vCPU halts.
#[inline(never)]
pub(super) unsafe fn trap(f: *mut NativeFrame, esr: u64, elr: u64, spsr: u64) -> ! {
    unsafe {
        let stop_at = |word: u64, at: u64, reason: u64| -> ! { halt_native([esr, word, at], reason) };
        let Some(b) = cache::block_at(elr) else {
            stop_at(0, elr, stop::FP_TRAP)
        };
        let at = elr - b.code_va();
        let s = &*record();
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
            let q = [(*f).q[usize::from(d.regs[0])], (*f).q[usize::from(d.regs[1])], (*f).q[usize::from(d.regs[2])]];
            let wide = d.meta & natemu_abi::tiny::ELEM8 != 0;
            let lanes = if d.meta & natemu_abi::tiny::SCALAR != 0 { 1 } else if wide { 2 } else { 4 };
            // the result register: the compare's source
            fptrap::fma_check(wide, lanes, &q, &mut (*f).q[(word >> 5 & 31) as usize]);
            cache::control().fp_traps += 1;
            cache::control().fp_hist[24 + usize::from(d.meta & 7).min(4)] += 1;
            resume(s, f, elr + 4, spsr)
        }
        // The word, from the code area's writable alias (the executable one is not readable at EL1's choice of attributes).
        let word = ((natemu_abi::execution::cache::CODE_WRITE_VA + (elr - cache::CODE_VA)) as *const u32).read_volatile();
        let Some(class) = fptrap::classify(word) else {
            stop_at(u64::from(word), at, stop::FP_WORD)
        };
        let c = cache::control();
        c.fp_traps += 1;
        // chunk 6: by cause (ISS: UFF bit 3, IOF bit 0) and word class
        let cause = if esr >> 3 & 1 != 0 { 0 } else if esr & 1 != 0 { 1 } else { 2 };
        let class_no = match class {
            fptrap::Class::Patch(o) => usize::from(o.kind).min(4),
            fptrap::Class::Keep if word & 0xff20_fc07 == 0x1e20_2000 => 5,
            fptrap::Class::Keep if word & 0x7f3e_fc00 == 0x1e38_0000 => 6,
            fptrap::Class::Keep => 7,
        };
        c.fp_hist[8 * cause + class_no] += 1;
        let (fpcr, fpsr) = ((*f).fpcr, (*f).fpsr);
        // The in-block DE screen skip (accurate mode, DAZ and FTZ clear): the patch may write a denormal result, which code compiled after
        // an earlier op of its block no longer screens: leave the flag its next check tests (it exits there: `denormal_exit`). Under FTZ
        // the result is flushed, under DAZ nothing is skipped; fast mode (FZ = 1) never traps for UFE.
        if fpcr >> 24 & 1 == 0 && ftz_key(s.cpu.mxcsr, true) == 0 && daz_key(s.cpu.mxcsr, true) == 0 {
            (FRAME_VA as *mut u8).add(frame::DE_PENDING as usize).write_volatile(1);
        }
        if let fptrap::Class::Patch(op) = class {
            if let Some(entry) = fptrap::slot(TABLE.as_ptr(), word, &op) {
                // The table path: the operation performed here under the trapped FPCR without its enables (so ROUND's rounding mode is
                // kept), FPSR isolated. The sources are saved first: the result may overwrite one.
                let q = &raw mut (*f).q;
                let src = [(*q)[usize::from(op.rn)], (*q)[usize::from(op.rm)], (*q)[usize::from(op.ra)]];
                let rerun = fptrap::exec(
                    entry,
                    fpcr & !FPTRAPS_MASK,
                    (&raw mut (*q)[usize::from(op.rd)]).cast(),
                    (&raw const (*q)[usize::from(op.rn)]).cast(),
                    (&raw const (*q)[usize::from(op.rm)]).cast(),
                    (&raw const (*q)[usize::from(op.ra)]).cast(),
                );
                let (flush, nan, fz) = patch_mode(s, fpcr);
                let flags = fptrap::patch_flush(class, fptrap::x86_rc(fpcr), &src, rerun, flush, &mut (*q)[usize::from(op.rd)], nan);
                (*f).fpsr = fpsr | fz_flags(fz, flags, fpsr);
                resume(s, f, elr + 4, spsr)
            }
        }
        // The step fallback: the frame is copied (the step's own entry copies again, and the re-run needs the trap-free FPCR in it).
        let r = rerun();
        (*r).word = u64::from(word);
        (*r).fpcr = fpcr;
        (*r).fpsr = fpsr;
        if let fptrap::Class::Patch(op) = class {
            for (k, reg) in op.sources().into_iter().enumerate() {
                (*r).q[k] = (*f).q[usize::from(reg)];
            }
        }
        core::ptr::write_volatile(&raw mut (*r).active, 1);
        // The re-run: the same registers with the enables off and FPSR clear.
        let resume_frame = &raw mut (*shared()).captured;
        crate::record::copy_frame(resume_frame, f);
        (*resume_frame).fpcr = fpcr & !FPTRAPS_MASK;
        (*resume_frame).fpsr = 0;
        step(true);
        resume(s, resume_frame, elr, spsr | SPSR_SS | SPSR_I)
    }
}

/// How the patch is decided for a word run under the guest's FPCR `fpcr` (the trap's): the flush mode, whether NaN lanes are patched (the
/// mode's IOE enable), and whether FZ = 1 ran (fast mode's hardware flush, which never traps for UFE).
///
/// The block was compiled under the key of the MXCSR the bank holds (LDMXCSR ends a block). FTZ alone: software flushes the denormals and the
/// patch fixes the band's MIN_NORMAL. DAZ + FTZ (accurate mode runs FZ = 0 then, row 4 flushed the inputs): every tiny result traps and the
/// patch flushes it (x86's FTZ result), chunk 5.
#[inline(always)]
fn patch_mode(s: &State, fpcr: u64) -> (u8, bool, bool) {
    let ftz = ftz_key(s.cpu.mxcsr, true) != 0;
    // Fast mode's hardware flush (FZ = 1, the re-run's FPCR) never traps for UFE; an IOE trap there patches NaNs only (UFC kept as run).
    let fz = fpcr >> 24 & 1 != 0;
    let flush = if fz || !ftz { 0 } else if daz_key(s.cpu.mxcsr, true) != 0 { fptrap::FLUSH_ALL } else { 1 };
    (flush, fpcr & natemu_abi::execution::FPCR_IOE != 0, fz)
}
/// Under FZ = 1 the patch keeps the UFC the re-run raised (the guest's own from before the trap, ORed by the caller).
#[inline(always)]
fn fz_flags(fz: bool, flags: u64, before: u64) -> u64 {
    if fz { flags & !natemu_abi::execution::FPSR_UFC | before & natemu_abi::execution::FPSR_UFC } else { flags }
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
        let s = &*record();
        let (flush, nan, fz) = patch_mode(s, (*r).fpcr);
        let rd = match class {
            fptrap::Class::Patch(op) => usize::from(op.rd),
            fptrap::Class::Keep => 0,
        };
        let flags = fptrap::patch_flush(class, fptrap::x86_rc((*r).fpcr), &(*r).q, (*frame).fpsr, flush, &mut (*frame).q[rd], nan);
        let flags = fz_flags(fz, flags, (*r).fpsr);
        (*frame).fpsr = (*r).fpsr | flags;
        (*frame).fpcr = (*r).fpcr;
        core::ptr::write_volatile(&raw mut (*r).active, 0);
        cache::control().fp_steps += 1;
        resume(s, frame, elr, spsr & !(SPSR_SS | SPSR_I))
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
