//! One level-triggered fixture request per CPU, with repeated notifications.
//! Canonical handoff is exercised here; IDT delivery and APIC emulation are not.
use super::*;
use natemu_abi::recovery::{GUEST_VA, GuestState};

fn record() -> *mut EventRecord {
    EVENT_VA as *mut EventRecord
}
fn guest() -> *mut GuestState {
    GUEST_VA as *mut GuestState
}

pub fn post_all(trace: u64) {
    let c = coordinator();
    let g = coordinator::lock(c);
    for id in 0..c.participants as usize {
        // A notification for the same identified level request cannot rearm
        // it after consumption (state2). Distinct edge requests need a queue.
        let _ = c.event_pending[id].compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire);
        c.event_notifications[id].fetch_add(1, Ordering::Release);
        checked(g.trace(trace, id as u64));
    }
}

pub unsafe fn wait_check() {
    unsafe {
        let r = record();
        ptr::write_volatile(
            &raw mut (*r).wait_checks,
            ptr::read_volatile(&raw const (*r).wait_checks) + 1,
        );
        ptr::write_volatile(
            &raw mut (*r).blocked,
            ptr::read_volatile(&raw const (*r).blocked) | 1,
        );
        // Publish/recheck a durable request rather than consuming a wake bit.
        let id = ptr::read_volatile(&raw const (*shared()).object.vcpu_id) as usize;
        let _pending = coordinator().event_pending[id].load(Ordering::Acquire);
    }
}

pub unsafe fn materialize(frame: &NativeFrame) {
    unsafe {
        let mut state = read_record(guest());
        state.materialize(frame, 17, true);
        if coordinator().alias_profile != 0 {
            state.rip = natemu_abi::alias::source_rip(coordinator().alias_profile) + 17;
        }
        write_record(guest(), state);
    }
}

pub unsafe fn clear_fixture_controls() {
    unsafe {
        let mut state = read_record(guest());
        state.rflags |= 0x200;
        write_record(guest(), state);
        ptr::write_volatile(&raw mut (*record()).shadow, 0);
        ptr::write_volatile(&raw mut (*record()).priority, 0);
    }
}

pub unsafe fn initial_boundary(g: &coordinator::Guard<'_>, id: u64) {
    unsafe {
        attempt(g, id, 3);
        if id == 1 {
            // Eligibility-predicate fixture, not an implementation of x86
            // shadow expiry: toggle it before any guest instruction executes.
            ptr::write_volatile(&raw mut (*record()).shadow, 0);
            checked(g.trace(22, id));
            attempt(g, id, 3);
        }
    }
}

// Caller owns coordinator authority. Deliver before granting a new native entry
// or after clearing this owner's active grant at a completed guest boundary.
pub unsafe fn attempt(g: &coordinator::Guard<'_>, id: u64, boundary: u64) {
    unsafe {
        let c = coordinator();
        if c.event_pending[id as usize].load(Ordering::Acquire) != 1 {
            return;
        }
        let r = record();
        let state = read_record(guest());
        let blockers = event_blockers(
            c.phase.load(Ordering::Relaxed),
            c.active.load(Ordering::Relaxed) & (1 << id) != 0,
            boundary != 0,
            state.rflags,
            ptr::read_volatile(&raw const (*r).shadow),
            ptr::read_volatile(&raw const (*r).priority),
        );
        if blockers != 0 {
            ptr::write_volatile(
                &raw mut (*r).blocked,
                ptr::read_volatile(&raw const (*r).blocked) | blockers,
            );
            return;
        }
        if ptr::read_volatile(&raw const (*r).deliveries) != 0 {
            stop(PANIC, 120);
        }
        write_record(&raw mut (*r).packet, state);
        ptr::write_volatile(&raw mut (*r).boundary, boundary);
        ptr::write_volatile(&raw mut (*r).deliveries, 1);
        ptr::write_volatile(&raw mut (*r).consumed, 1);
        c.event_pending[id as usize].store(2, Ordering::Release);
        checked(g.trace(20, id));
    }
}
