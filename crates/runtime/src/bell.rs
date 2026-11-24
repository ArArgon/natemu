//! EL0 sampler (`profile` builds; docs/restructure/el0-sampler-plan.md section 3, [`natemu_abi::sample`]): the host rings
//! each vCPU's sample bell (an SPI of its own, its INTID in `Head::bell`); a bell taken from generated code writes one record
//! to the vCPU's ring and resumes it ([`sample`], `execution::interrupted`): no bank, no stop, no arm. A bell taken in a wait
//! is only counted ([`wfi`], [`bells_only`], `events::acknowledge`), so it neither wakes the vCPU nor shifts halt polling.
//! Without the feature every function here is empty or constant and adds no code.
#[cfg(all(feature = "profile", feature = "pcid-study"))]
compile_error!("`profile` and `pcid-study` both use the study part of the lookup area (`cache::PCID_STUDY`)");
#[cfg(feature = "profile")]
mod on {
    use crate::cache;
    use core::sync::atomic::{AtomicU64, Ordering::Relaxed};
    use natemu_abi::NativeFrame;
    use natemu_abi::execution::cache::{
        CODE_BYTES_MAX, CODE_VA, DEAD, DISPATCH_BYTES, DISPATCH_SLOT_BYTES, DISPATCH_VA, HEAD_BYTES, NO_CONTEXT, PCID_STUDY,
        STEP,
    };
    use natemu_abi::sample::{self as sm, Head, Ring, Sample};
    fn word(offset: usize) -> &'static AtomicU64 {
        // SAFETY: this vCPU's study part (its head), shared with the host word by word.
        unsafe { AtomicU64::from_ptr(cache::lookup_area::<u64>(PCID_STUDY + offset)) }
    }
    fn add(offset: usize) {
        word(offset).store(word(offset).load(Relaxed) + 1, Relaxed);
    }
    fn ring() -> Ring {
        // SAFETY: the study part holds the head and the ring (`natemu_abi::sample::BYTES`); this resident writes it alone.
        unsafe { Ring::at(cache::lookup_area::<u8>(PCID_STUDY)) }
    }
    const BELLS: usize = core::mem::offset_of!(Head, bells);
    const IN_WAIT: usize = core::mem::offset_of!(Head, bells_in_wait);
    const UNKNOWN: usize = core::mem::offset_of!(Head, unknown_calls);
    const HELD: usize = core::mem::offset_of!(Head, held);
    /// At the resident's start: from now on the host rings this vCPU.
    pub fn start() {
        word(sm::HEAD_MAGIC).store(sm::MAGIC, Relaxed);
    }
    /// `intid` is this vCPU's sample bell.
    #[inline(always)]
    pub fn is(intid: u64) -> bool {
        let bell = word(sm::HEAD_BELL).load(Relaxed);
        bell != 0 && intid == bell
    }
    /// A bell taken from generated code, interrupted before `elr` at CNTVCT `t0`: one record. Rung before the last resume
    /// (the host's ring time against `resume_exception`'s), it was pending while the vCPU was outside generated code.
    pub fn sample(frame: &NativeFrame, elr: u64, t0: u64) {
        add(BELLS);
        let (rung, resumed) = (word(sm::HEAD_RING_TIME).load(Relaxed), word(sm::HEAD_RESUME_TIME).load(Relaxed));
        let since = |from: u64| t0.saturating_sub(from).min(u64::from(u32::MAX)) as u32;
        let mut s = Sample { time: rung, ..Sample::default() };
        if rung < resumed {
            (s.kind, s.since) = (sm::RESIDENT, since(rung));
        } else {
            (s.kind, s.since) = (sm::EL0, since(resumed));
            s.flushes = (cache::control().flushes & 0xfff) as u16;
            place(&mut s, frame, elr);
        }
        ring().push(s.encode());
    }
    /// Where generated code was: a block (a probe charged to its store's), the dispatcher (its target and the call test),
    /// or elsewhere in the code area.
    fn place(s: &mut Sample, frame: &NativeFrame, elr: u64) {
        let x = elr.wrapping_sub(CODE_VA);
        if x >= CODE_BYTES_MAX as u64 {
            (s.place, s.frame) = (sm::OUTSIDE, x as u32);
            return;
        }
        if (DISPATCH_VA..DISPATCH_VA + DISPATCH_BYTES as u64).contains(&elr) {
            (s.place, s.at, s.rip) = (sm::DISPATCH, (elr - DISPATCH_VA) as u16, frame.x[0]);
            // Slot 0 (the dispatcher itself) serves every context without a slot: the target's context is not known there.
            let k = (elr - DISPATCH_VA) as usize / DISPATCH_SLOT_BYTES;
            let context = cache::control().slot_contexts[k];
            let target = (k != 0 && context != NO_CONTEXT).then(|| cache::current_at(s.rip, context)).flatten();
            let call = match target {
                Some(b) => {
                    (s.context, s.frame) = (context, (b.source_gpa[0] >> 12) as u32);
                    call_test(b.source_gpa[0] & !0xfff, s.rip)
                }
                None => sm::CALL_UNKNOWN,
            };
            if call == sm::CALL_UNKNOWN {
                add(UNKNOWN);
            }
            s.flags |= call << sm::CALL_SHIFT;
            return;
        }
        // In a shared probe: charged to its store's block, at the word the probe returns to (x0, then d0).
        let probe = natemu_abi::execution::probe::at(elr).is_some();
        let d0 = u64::from_le_bytes(frame.q[0][..8].try_into().unwrap_or([0; 8]));
        let pc = natemu_abi::execution::probe::returns_to(elr, frame.x[0], d0);
        // (`block_at` finds the step slot's block too; nothing else of the head is a block.)
        let Some(b) = cache::block_at(pc) else {
            (s.place, s.frame) = (if x < HEAD_BYTES as u64 { sm::HEAD } else { sm::NO_BLOCK }, x as u32);
            return;
        };
        s.place = if probe {
            sm::PROBE
        } else if b.flags & STEP != 0 {
            sm::STEP
        } else {
            sm::BLOCK
        };
        (s.rip, s.context, s.frame) = (b.rip, b.context, (b.source_gpa[0] >> 12) as u32);
        let offset = cache::native_offset(b, pc);
        s.at = offset as u16;
        if offset != pc.wrapping_sub(b.code_va()) {
            s.flags |= sm::HOT;
        }
        if b.flags & DEAD != 0 {
            s.flags |= sm::DEAD;
        }
    }
    /// The call test of a dispatcher target at `rip` in the guest-physical page `page`: the bytes before it, read through
    /// the resident's RAM alias (never a guest walk: it sets A bits and can fault).
    fn call_test(page: u64, rip: u64) -> u8 {
        let o = rip & 0xfff;
        if o < sm::BEFORE as u64 {
            return sm::CALL_UNKNOWN;
        }
        let mut b = [0; sm::BEFORE];
        if !crate::execution::guest_ram().copy_out(page | (o - sm::BEFORE as u64), &mut b) {
            return sm::CALL_UNKNOWN;
        }
        if sm::call_before(&b) { sm::CALL_YES } else { sm::CALL_NO }
    }
    /// An interrupt is signalled to this PE (ISR_EL1.I).
    fn signalled() -> bool {
        let isr: u64;
        // SAFETY: reads the pending-interrupt status.
        unsafe { core::arch::asm!("isb", "mrs {}, isr_el1", out(reg) isr) };
        isr & 1 << 7 != 0
    }
    /// In a wait, with an interrupt signalled: takes every pending sample bell (counted, no wake). True when nothing else
    /// was pending; false when another interrupt was, acknowledged here and held for `acknowledge` ([`held`]).
    pub fn bells_only() -> bool {
        loop {
            if !signalled() {
                return true;
            }
            let intid: u64;
            // SAFETY: GIC CPU interface system registers (enabled at start): ICC_IAR1_EL1, then ICC_EOIR1_EL1 for a bell.
            unsafe { core::arch::asm!("mrs {i}, S3_0_C12_C12_0", i = out(reg) intid) };
            if intid >= 1020 {
                return true;
            }
            if !is(intid) {
                word(HELD).store(intid + 1, Relaxed);
                return false;
            }
            unsafe { core::arch::asm!("msr S3_0_C12_C12_1, {i}", "isb", i = in(reg) intid) };
            add(IN_WAIT);
        }
    }
    /// The INTID [`bells_only`] acknowledged and held, taken: `acknowledge` serves it before reading another (while it is
    /// active no interrupt of its priority is signalled, so ISR_EL1 would not show the others).
    #[inline(always)]
    pub fn held() -> Option<u64> {
        let h = word(HELD).load(Relaxed);
        if h == 0 {
            return None;
        }
        word(HELD).store(0, Relaxed);
        Some(h - 1)
    }
    /// A bell `acknowledge` took (in a wait: the resident runs with interrupts masked, so only a wait acknowledges).
    #[inline(always)]
    pub fn in_wait() {
        add(IN_WAIT);
    }
    /// `wfi` until something other than a sample bell ended it (each bell taken and counted).
    pub fn wfi() {
        loop {
            // SAFETY: waits for an interrupt (masked at EL1: it only wakes the PE); no memory effect.
            unsafe { core::arch::asm!("dsb sy", "wfi") };
            if !(signalled() && bells_only()) {
                return;
            }
        }
    }
}
#[cfg(feature = "profile")]
pub use on::*;

#[cfg(not(feature = "profile"))]
mod off {
    #[inline(always)]
    pub fn start() {}
    #[inline(always)]
    pub fn is(_: u64) -> bool {
        false
    }
    #[inline(always)]
    pub fn sample(_: &natemu_abi::NativeFrame, _: u64, _: u64) {}
    #[inline(always)]
    pub fn bells_only() -> bool {
        false
    }
    #[inline(always)]
    pub fn held() -> Option<u64> {
        None
    }
    #[inline(always)]
    pub fn in_wait() {}
    #[inline(always)]
    pub fn wfi() {
        // SAFETY: waits for an interrupt (masked at EL1: it only wakes the PE); no memory effect.
        unsafe { core::arch::asm!("dsb sy", "wfi") };
    }
}
#[cfg(not(feature = "profile"))]
pub use off::*;
