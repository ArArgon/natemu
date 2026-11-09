//! Compile-time-only finite scheduling holds. These never write production protocol words.
use core::sync::atomic::{AtomicU64, Ordering::SeqCst};
use natemu_abi::execution::cache::{GLOBAL_VA, io_test::*};
fn word(v: usize, at: usize) -> &'static AtomicU64 {
    // SAFETY: aligned, fixed private metadata; layout statically checked against CODE_PAGES.
    unsafe { AtomicU64::from_ptr((GLOBAL_VA + (BASE + v * STRIDE + at) as u64) as *mut u64) }
}
pub fn ready(v: usize) {
    word(v, READY).store(MAGIC, SeqCst);
}
fn ticks() -> u64 {
    let t;
    unsafe { core::arch::asm!("mrs {}, cntvct_el0", out(reg) t, options(nomem, nostack)) };
    t
}
pub fn point(v: usize, point: u64) {
    let tag = word(v, ARM).load(SeqCst);
    if tag & 255 != point
        || word(v, ARM)
            .compare_exchange(tag, 0, SeqCst, SeqCst)
            .is_err()
    {
        return;
    }
    word(v, HITS).fetch_add(1, SeqCst);
    word(v, REACHED).store(tag, SeqCst);
    let hz: u64;
    unsafe { core::arch::asm!("mrs {}, cntfrq_el0", out(reg) hz, options(nomem, nostack)) };
    let began = ticks();
    while word(v, RELEASE).load(SeqCst) < tag {
        if ticks().wrapping_sub(began) > hz * 5 {
            word(v, TIMEOUT).store(tag, SeqCst);
            break;
        }
        core::hint::spin_loop();
    }
}
pub fn late_shadows(v: usize) {
    word(v, LATE_SHADOWS).fetch_add(1, SeqCst);
}
