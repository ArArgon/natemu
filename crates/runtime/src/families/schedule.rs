//! Nonblocking seeded grants. Every caller already holds its coordinator lock;
//! false means drop that lock before polling again. Ordinary fixtures do not
//! touch the additional shared mapping.
use crate::{coordinator::Guard, shared, stop};
use natemu_abi::{PANIC, families::maintenance, families::schedule as s};

pub fn enabled() -> bool {
    unsafe {
        match (*shared()).schedule {
            0 => false,
            s::MAGIC => true,
            _ => stop(PANIC, s::BAD),
        }
    }
}
/// Read only immutable configuration; decisions remain under coordinator authority.
pub fn stack_roles(n: u64) -> natemu_abi::families::stack_concurrent::Roles {
    let config = if enabled() {
        let config =
            unsafe { core::ptr::read_volatile(&raw const (*(s::VA as *const s::State)).config) };
        Some(config)
    } else {
        None
    };
    natemu_abi::families::stack_concurrent::Roles::for_fixture(n, config)
        .unwrap_or_else(|| stop(PANIC, s::BAD))
}
unsafe fn state() -> &'static mut s::State {
    unsafe {
        let at = &mut *(s::VA as *mut s::State);
        if at.magic != s::MAGIC
            || at.version != s::VERSION
            || !at.config.valid()
            || at.config.n != (*shared()).object.configured_vcpus as u64
        {
            stop(PANIC, s::BAD);
        }
        at
    }
}
pub fn permit(
    guard: &Guard<'_>,
    key: u64,
    cohort: u64,
    eligible: u64,
    phase: u64,
    epoch: u64,
) -> bool {
    if !enabled() {
        return true;
    }
    unsafe {
        let s = state();
        let before = s.decision_count;
        let accepted = s
            .permit(
                (*shared()).object.vcpu_id as u64,
                key,
                cohort,
                eligible,
                phase,
                epoch,
            )
            .unwrap_or_else(|e| stop(PANIC, e));
        if s.decision_count != before {
            s.decisions[before as usize].trace_before = guard.trace_length();
        }
        if accepted {
            let last = s.decisions[..s.decision_count as usize]
                .iter_mut()
                .rev()
                .find(|d| d.cut == key && d.chosen == (*shared()).object.vcpu_id as u64)
                .unwrap_or_else(|| stop(PANIC, s::BAD));
            last.claim_trace_before = guard.trace_length();
        }
        accepted
    }
}
pub fn enqueue(
    guard: &Guard<'_>,
    queue: &mut maintenance::State,
    id: usize,
) -> Result<Option<maintenance::Request>, u64> {
    if !enabled() {
        return queue.enqueue(id);
    }
    unsafe {
        let s = state();
        let before = s.pool_event_count;
        let result = s.enqueue(queue, id);
        if s.pool_event_count != before {
            s.pool_events[before as usize].trace_before = guard.trace_length();
        }
        result
    }
}
/// The first full pool must cause a genuine distinct-request rejection before
/// its head is serviced. Subsequent epochs retain the existing fairness rules.
pub fn pool_ready(_guard: &Guard<'_>, queue: &maintenance::State) -> bool {
    if !enabled() {
        return queue.maximum_depth == queue.n;
    }
    unsafe {
        let s = state();
        queue.maximum_depth >= s.config.pool_limit
            && (!s.config.pool_applicable() || s.full_count != 0)
    }
}
/// Last-owner delay is part of the underlying protocol, not a random choice.
pub fn except_last(all: u64, delayed: u64, done: u64) -> u64 {
    let early = all & !(1 << delayed);
    if done & early == early { all } else { early }
}
