//! Performance phase, first chunk (`natemu_abi::execution::perf`): with the `perf` feature, CNTVCT ticks and entries by
//! where this vCPU is. Without it every call is empty and inlined away: the default resident is unchanged.
pub use natemu_abi::execution::perf::*;

#[cfg(feature = "perf")]
fn record() -> &'static mut Perf {
    // SAFETY: the record is this vCPU's, in its EL1-only state page; only the resident writes it while the vCPU runs.
    unsafe { &mut *(VA as *mut Perf) }
}
/// Charges the current bucket up to now and makes `to` current; the bucket it was.
#[cfg(feature = "perf")]
#[inline(always)]
fn charge(to: u64) -> u64 {
    let t: u64;
    // SAFETY: reads the virtual counter (no barrier: accounting, not ordering).
    unsafe { core::arch::asm!("mrs {}, cntvct_el0", out(reg) t, options(nomem, nostack)) };
    let p = record();
    let from = p.current;
    p.ticks[(from as usize).min(BUCKETS - 1)] += t.wrapping_sub(p.since);
    (p.since, p.current) = (t, to);
    from
}
/// The resident starts: everything from now on is charged.
#[cfg(feature = "perf")]
pub fn start() {
    let p = record();
    p.magic = MAGIC;
    // SAFETY: as in `charge`.
    unsafe { core::arch::asm!("mrs {}, cntvct_el0", out(reg) p.since, options(nomem, nostack)) };
    p.current = START;
}
/// Switches to bucket `to`, counting it; the bucket it was (for [`leave`]).
#[cfg(feature = "perf")]
#[inline(always)]
pub fn enter(to: u64) -> u64 {
    record().count[(to as usize).min(BUCKETS - 1)] += 1;
    charge(to)
}
/// Back to bucket `from` (not counted again).
#[cfg(feature = "perf")]
#[inline(always)]
pub fn leave(from: u64) {
    charge(from);
}

#[cfg(not(feature = "perf"))]
#[inline(always)]
pub fn start() {}
#[cfg(not(feature = "perf"))]
#[inline(always)]
pub fn enter(_: u64) -> u64 {
    0
}
#[cfg(not(feature = "perf"))]
#[inline(always)]
pub fn leave(_: u64) {}
