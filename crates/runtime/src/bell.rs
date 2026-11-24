//! EL0 sampler (`profile` builds; docs/restructure/el0-sampler-plan.md section 3, [`natemu_abi::sample`]): the host rings
//! each vCPU's sample bell (an SPI of its own, its INTID in `Head::bell`); a bell taken from generated code writes one record
//! to the vCPU's ring and resumes it ([`sample`], `execution::interrupted`): no bank, no stop, no arm. A bell taken in a wait
//! is only counted ([`wfi`], [`bells_only`], `events::acknowledge`), so it neither wakes the vCPU nor shifts halt polling.
//! The decisions (where a sample is, what a wait does with a bell) are `natemu_abi::sample`'s, unit-tested there; this
//! module gives them the cache, guest RAM and the GIC's registers. Without the feature [`Bells`] has no bell and every
//! function here is empty or constant, adding no code.
#[cfg(all(feature = "profile", feature = "pcid-study"))]
compile_error!("`profile` and `pcid-study` both use the study part of the lookup area (`cache::PCID_STUDY`)");

/// The GIC CPU interface's registers (enabled at start).
pub struct Gic;
impl natemu_abi::sample::Gic for Gic {
    #[inline(always)]
    fn signalled(&mut self) -> bool {
        let isr: u64;
        // SAFETY: reads the pending-interrupt status.
        unsafe { core::arch::asm!("isb", "mrs {}, isr_el1", out(reg) isr) };
        isr & 1 << 7 != 0
    }
    #[inline(always)]
    fn acknowledge(&mut self) -> u64 {
        let intid: u64;
        // SAFETY: ICC_IAR1_EL1.
        unsafe { core::arch::asm!("mrs {i}, S3_0_C12_C12_0", i = out(reg) intid) };
        intid
    }
    #[inline(always)]
    fn end(&mut self, intid: u64) {
        // SAFETY: ICC_EOIR1_EL1.
        unsafe { core::arch::asm!("msr S3_0_C12_C12_1, {i}", "isb", i = in(reg) intid) };
    }
}

#[cfg(feature = "profile")]
mod on {
    use crate::cache;
    use core::sync::atomic::{AtomicU64, Ordering::Relaxed};
    use natemu_abi::NativeFrame;
    use natemu_abi::execution::cache::{DEAD, PCID_STUDY, STEP};
    use natemu_abi::sample::{self as sm, BlockView, Head, Places, Ring};
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
    /// This vCPU's bell state in its head.
    pub struct Bells;
    impl sm::Bells for Bells {
        #[inline(always)]
        fn bell(&self) -> u64 {
            word(sm::HEAD_BELL).load(Relaxed)
        }
        #[inline(always)]
        fn held(&mut self) -> Option<u64> {
            let h = word(HELD).load(Relaxed);
            if h == 0 {
                return None;
            }
            word(HELD).store(0, Relaxed);
            Some(h - 1)
        }
        fn hold(&mut self, intid: u64) {
            word(HELD).store(intid + 1, Relaxed);
        }
        fn in_wait(&mut self) {
            add(IN_WAIT);
        }
    }
    /// The code cache and guest RAM, as `sample::record` asks them.
    struct Here;
    impl Places for Here {
        fn block(&self, pc: u64) -> Option<BlockView> {
            cache::block_at(pc).map(|b| {
                let offset = cache::native_offset(b, pc);
                BlockView {
                    rip: b.rip,
                    context: b.context,
                    frame: b.source_gpa[0],
                    offset: offset as i64,
                    hot: offset != pc.wrapping_sub(b.code_va()),
                    step: b.flags & STEP != 0,
                    dead: b.flags & DEAD != 0,
                }
            })
        }
        fn tail(&self, pc: u64) -> Option<(BlockView, u16)> {
            if let Some((b, w)) = cache::pair_at(pc) {
                let view = BlockView {
                    rip: b.rip,
                    context: b.context,
                    frame: b.source_gpa[0],
                    dead: b.flags & DEAD != 0,
                    ..BlockView::default()
                };
                return Some((view, w as u16 | sm::TAIL_CALL | sm::TAIL_PAIR));
            }
            cache::tail_at(pc).map(|(b, point, w)| {
                let view = BlockView {
                    rip: b.rip,
                    context: b.context,
                    frame: b.source_gpa[0],
                    dead: b.flags & DEAD != 0,
                    ..BlockView::default()
                };
                let role = w as u16
                    | if b.tail_calls(point) { sm::TAIL_CALL } else { 0 }
                    | if point.is_some() { sm::TAIL_POINT } else { 0 };
                (view, role)
            })
        }
        fn slot_context(&self, k: usize) -> u64 {
            cache::control().slot_contexts[k]
        }
        fn target(&self, rip: u64, context: u64) -> Option<u64> {
            cache::current_at(rip, context).map(|b| b.source_gpa[0])
        }
        fn read(&self, gpa: u64, out: &mut [u8; sm::BEFORE]) -> bool {
            crate::execution::guest_ram().copy_out(gpa, out)
        }
    }
    /// A trip out of generated code begins (`finish`, the first Rust every gate calls): its CNTVCT becomes the last exit
    /// (`Head::exit_time`), the previous one `Head::exit_before`, which a bell taken from generated code classes its ring
    /// time by (`sample::record`). Taken after the gate's register save, so a few tens of nanoseconds late: a ring in that
    /// window is counted EL0 with no place rather than resident.
    #[inline(always)]
    pub fn left() {
        let now: u64;
        // SAFETY: reads the virtual counter.
        unsafe { core::arch::asm!("mrs {}, cntvct_el0", out(reg) now, options(nomem, nostack, preserves_flags)) };
        ring().left(now);
    }
    /// At the resident's start: from now on the host rings this vCPU.
    pub fn start() {
        word(sm::HEAD_MAGIC).store(sm::MAGIC, Relaxed);
    }
    /// `intid` is this vCPU's sample bell.
    #[inline(always)]
    pub fn is(intid: u64) -> bool {
        sm::is_bell(word(sm::HEAD_BELL).load(Relaxed), intid)
    }
    /// A bell taken from generated code, interrupted before `elr` at CNTVCT `t0`: one record (`sample::record`).
    pub fn sample(frame: &NativeFrame, elr: u64, t0: u64) {
        add(BELLS);
        let d0 = u64::from_le_bytes(frame.q[0][..8].try_into().unwrap_or([0; 8]));
        let s = sm::record(
            word(sm::HEAD_RING_TIME).load(Relaxed),
            word(sm::HEAD_RESUME_TIME).load(Relaxed),
            word(sm::HEAD_EXIT_BEFORE).load(Relaxed),
            t0,
            cache::control().flushes,
            elr,
            frame.x[0],
            d0,
            &Here,
        );
        if s.place == sm::DISPATCH && s.flags >> sm::CALL_SHIFT & 3 == sm::CALL_UNKNOWN {
            add(UNKNOWN);
        }
        ring().push(s.encode());
    }
}
#[cfg(feature = "profile")]
pub use on::*;

#[cfg(not(feature = "profile"))]
mod off {
    /// No bell: `natemu_abi::sample::next` is then `acknowledge`'s plain loop.
    pub struct Bells;
    impl natemu_abi::sample::Bells for Bells {
        #[inline(always)]
        fn bell(&self) -> u64 {
            0
        }
        #[inline(always)]
        fn held(&mut self) -> Option<u64> {
            None
        }
        #[inline(always)]
        fn hold(&mut self, _: u64) {}
        #[inline(always)]
        fn in_wait(&mut self) {}
    }
    #[inline(always)]
    pub fn left() {}
    #[inline(always)]
    pub fn start() {}
    #[inline(always)]
    pub fn is(_: u64) -> bool {
        false
    }
    #[inline(always)]
    pub fn sample(_: &natemu_abi::NativeFrame, _: u64, _: u64) {}
}
#[cfg(not(feature = "profile"))]
pub use off::*;

/// In a wait, with an interrupt signalled: every pending sample bell taken and counted; false when another interrupt is
/// pending (held for `acknowledge`). Always false without the feature (an interrupt signalled is a wake).
#[inline(always)]
pub fn bells_only() -> bool {
    cfg!(feature = "profile") && natemu_abi::sample::bells_only(&mut Gic, &mut Bells)
}
/// `wfi` until something other than a sample bell ended it (each bell taken and counted).
#[inline(always)]
pub fn wfi() {
    loop {
        // SAFETY: waits for an interrupt (masked at EL1: it only wakes the PE); no memory effect.
        unsafe { core::arch::asm!("dsb sy", "wfi") };
        if !cfg!(feature = "profile") || !(natemu_abi::sample::Gic::signalled(&mut Gic) && bells_only()) {
            return;
        }
    }
}
