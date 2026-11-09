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

/// Perf chunk 14: one halt wake-up of `kind` ([`Wakes`]) that took `ticks`.
#[cfg(feature = "perf")]
pub fn wake(kind: usize, ticks: u64) {
    // SAFETY: the record is this vCPU's, in its EL1-only state page (as `record`).
    let w = unsafe { &mut *(WAKES_VA as *mut Wakes) };
    let f: u64;
    // SAFETY: reads the counter's frequency.
    unsafe { core::arch::asm!("mrs {}, cntfrq_el0", out(reg) f, options(nomem, nostack)) };
    let us = ticks * 1_000_000 / f.max(1);
    let bin = (0..WAKE_BINS - 1)
        .find(|&b| us < 1 << (2 * b))
        .unwrap_or(WAKE_BINS - 1);
    (w.count[kind], w.ticks[kind], w.max[kind]) = (
        w.count[kind] + 1,
        w.ticks[kind] + ticks,
        w.max[kind].max(ticks),
    );
    w.bins[kind][bin] += 1;
}
/// Whether the `perf` accounting is built in.
pub const ON: bool = cfg!(feature = "perf");

#[cfg(not(feature = "perf"))]
#[inline(always)]
pub fn wake(_: usize, _: u64) {}
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
