//! Repeated table writers yield to the next real generated instruction.
//! The control program is never reset or replayed to manufacture progress.
use super::concurrent::{all, common, coordinator, enabled, id, lock, owner};
use super::*;
use core::sync::atomic::Ordering::{Acquire, Relaxed, Release};
use natemu_abi::{concurrency as n, families::maintenance as m};
fn pressure() -> *mut m::State {
    m::VA as *mut m::State
}
fn interval(index: usize) -> *mut m::Interval {
    (m::RECORD_VA + index as u64 * 4096) as *mut m::Interval
}
fn event() -> *mut n::EventRecord {
    n::EVENT_VA as *mut n::EventRecord
}
fn checked<T>(r: Result<T, u64>) -> T {
    r.unwrap_or_else(|e| stop(PANIC, e))
}

pub(super) unsafe fn compiler_wait() {
    if !enabled() {
        return;
    }
    unsafe {
        let c = coordinator();
        let e = event();
        if (*e).wait_checks == 0 {
            c.event_notifications[id() as usize].fetch_add(1, Release);
            let _ = c.event_pending[id() as usize].compare_exchange(0, 1, Release, Relaxed);
        }
        (*e).wait_checks += 1;
        (*e).blocked |= 1;
        let _ = c.event_pending[id() as usize].load(Acquire);
    }
}
// All callers hold coordinator authority. Canonical IF/control/CS are untouched.
unsafe fn attempt(g: &crate::coordinator::Guard<'_>) {
    unsafe {
        let c = coordinator();
        let e = event();
        if c.event_pending[id() as usize].load(Relaxed) != 1 {
            return;
        }
        let blockers = n::event_blockers(
            c.phase.load(Relaxed),
            c.active.load(Relaxed) & (1 << id()) != 0
                || (*common()).semantic_active & (1 << id()) != 0,
            (*state()).completed != 0,
            (*guest()).rflags,
            (*e).shadow,
            (*e).priority,
        );
        (*e).blocked |= blockers;
        if blockers != 0 {
            return;
        }
        if (*e).deliveries != 0 {
            stop(PANIC, 330);
        }
        copy_record(
            (&raw mut (*e).packet).cast(),
            guest().cast(),
            size_of::<r::GuestState>(),
        );
        (*e).boundary = (*state()).completed;
        (*e).consumed = 1;
        (*e).deliveries = 1;
        c.event_pending[id() as usize].store(2, Release);
        checked(g.trace(20, id()));
    }
}
pub(super) unsafe fn event_boundary() {
    if !enabled() {
        return;
    }
    unsafe {
        let g = lock().unwrap();
        attempt(&g);
        // Expire only the declared event-predicate fixture; never modify IF.
        if (*event()).shadow != 0 || (*event()).priority != 0 {
            (*event()).shadow = 0;
            (*event()).priority = 0;
            checked(g.trace(22, id()));
            attempt(&g);
        }
    }
}
pub(super) unsafe fn semantic_begin(source: &[u8]) {
    if !enabled() || c::body(source).is_some() {
        return;
    }
    unsafe {
        let _g = lock();
        if (*common()).semantic_active & (1 << id()) != 0 {
            stop(PANIC, 331);
        }
        (*common()).semantic_active |= 1 << id();
        (*owner()).semantic_admissions += 1;
    }
}
pub(super) unsafe fn semantic_end() {
    if !enabled() {
        return;
    }
    unsafe {
        let _g = lock();
        (*common()).semantic_active &= !(1 << id());
    }
}
unsafe fn ready() -> bool {
    unsafe {
        if (*state()).profile == 3 {
            return false;
        }
        if (*state()).profile == 21 {
            (*common()).pae_acknowledgements[1] == all()
        } else {
            (*common()).second_commit != 0
                && (*common()).peer_executed == (all() ^ (1 << (*common()).actor))
        }
    }
}
// Fixture leaves: the legacy pair, the PAE pair and the long-mode data leaf.
fn leaf_gpa(i: usize) -> u64 {
    [0x2000 + 256 * 4, 0x4000 + 256 * 4, 0x3000 + 256 * 8, 0x5000 + 256 * 8, 0x9000 + 256 * 8][i]
}
unsafe fn leaves() -> [u64; 5] {
    unsafe {
        let profile = (*state()).profile;
        let read = |i: usize| {
            if i < 2 {
                recovery::RAM.read::<u32>(leaf_gpa(i)).map(u64::from)
            } else if i < 4 {
                PAE_TABLES.read::<u64>(leaf_gpa(i))
            } else {
                long_mode::TABLES.read::<u64>(leaf_gpa(i))
            }
            .unwrap_or_else(|| stop(PANIC, 339))
        };
        core::array::from_fn(|i| match i {
            0 | 1 if profile < 13 => read(i),
            2 | 3 if profile >= 13 => read(i),
            4 if profile >= 22 => read(i),
            _ => 0,
        })
    }
}
unsafe fn mutate() {
    unsafe {
        let values = leaves();
        let profile = (*state()).profile;
        for (i, value) in values.into_iter().enumerate() {
            let next = if value & natemu_abi::paging::PA_MASK == DATA_VA {
                DATA_VA + 4096 | 7
            } else {
                DATA_VA | 7
            };
            let written = match i {
                0 | 1 if profile < 13 => recovery::RAM.write(leaf_gpa(i), next as u32),
                2 | 3 if profile >= 13 => PAE_TABLES.write(leaf_gpa(i), next),
                4 if profile >= 22 => long_mode::TABLES.write(leaf_gpa(i), next),
                _ => true,
            };
            if !written {
                stop(PANIC, 339);
            }
        }
    }
}
// Called between instructions. An ordinary grant may include the immediately
// preceding semantic instructions, but completes only on actual generated work.
#[inline(never)]
pub(super) unsafe fn boundary() {
    if !enabled() {
        return;
    }
    unsafe {
        let c = coordinator();
        let p = pressure();
        {
            let _g = lock();
            if !ready() {
                return;
            }
            (*p).ready |= 1 << id();
        }
        loop {
            let g = lock().unwrap();
            if (*p).ready != all() {
                drop(g);
                core::hint::spin_loop();
                continue;
            }
            if (*p).finished != 0 {
                return;
            }
            if (*common()).maintenance_started == 0 {
                if c.active.load(Relaxed) != 0 || (*common()).semantic_active != 0 {
                    stop(PANIC, 332);
                }
                (*common()).maintenance_started = 1;
                (*p).compose_leaf = leaves()[if (*state()).profile < 13 { 0 } else { 2 }];
            }
            if (*p).pending[id() as usize] == 0 && (*p).ordinary != id() {
                let admitted = crate::families::schedule::enqueue(&g, &mut *p, id() as usize);
                let admitted = if crate::families::schedule::enabled() && admitted == Err(m::FULL) {
                    None
                } else {
                    checked(admitted)
                };
                if admitted.is_some() {
                    // Duplicate admission is checked against the live queue,
                    // and cannot consume a second slot or serial.
                    let serial = (*p).serial;
                    let length = (*p).length;
                    if (*p).enqueue(id() as usize) != Err(m::DUPLICATE)
                        || (*p).serial != serial
                        || (*p).length != length
                    {
                        stop(PANIC, 333);
                    }
                    (*p).duplicate_rejections += 1;
                    checked(g.trace(23, id()));
                }
            }
            if crate::families::schedule::pool_ready(&g, &*p)
                && (*p).current.serial == 0
                && (*p).ordinary == n::NO_CPU
            {
                if c.active.load(Relaxed) != 0
                    || c.queued.load(Relaxed) != 0
                    || (*common()).semantic_active != 0
                {
                    stop(PANIC, 334);
                }
                if (*p).ordinary_due() {
                    let cpu = checked((*p).begin_ordinary());
                    (*p).closing = 1;
                    c.phase.store(n::OPEN, Release);
                    checked(g.trace(29, cpu));
                } else if let Some(request) = checked((*p).start_head()) {
                    c.phase.store(n::EXCLUSIVE, Relaxed);
                    mutate();
                    checked(g.trace(25, request.cpu));
                }
            }
            if (*p).current.serial != 0
                && (*p).acknowledgements & (1 << id()) == 0
                && (id() != (*p).epoch % (*p).n || (*p).acknowledgements == all() ^ (1 << id()))
                && crate::families::schedule::permit(
                    &g,
                    natemu_abi::families::schedule::MAINTENANCE_ACK | (*p).epoch,
                    all(),
                    crate::families::schedule::except_last(all(), (*p).epoch % (*p).n, (*p).acknowledgements),
                    c.phase.load(Relaxed),
                    (*p).epoch,
                )
            {
                if c.active.load(Relaxed) != 0 || (*common()).semantic_active != 0 {
                    stop(PANIC, 335);
                }
                let hidden = (*pae_state()).hidden;
                invalidate();
                let (gpa, _) = route_under_authority(operand(), 4, Access::Read)
                    .unwrap_or_else(|_| stop(PANIC, 336));
                recovery::install_ram(operand(), gpa, false);
                (*state()).mapping = gpa;
                (*owner()).route = gpa;
                (*owner()).writable = 0;
                let pte = tables::descriptor(operand());
                (*owner()).maintenance_acks[(*p).total as usize] = [
                    (*p).epoch,
                    gpa,
                    c::mode(&*guest()).unwrap(),
                    (*state()).generation,
                    hidden.generation,
                    pte,
                ];
                if hidden != (*pae_state()).hidden {
                    stop(PANIC, 337);
                }
                checked((*p).acknowledge(id() as usize));
                checked(g.trace(26, id()));
                if (*p).acknowledgements == all() {
                    let value = leaves();
                    (*common()).maintenance_leaves[(*p).total as usize] = value;
                    (*p).leaf_history[(*p).total as usize] =
                        value[if (*state()).profile < 13 { 0 } else { 2 }];
                    checked((*p).complete_head());
                    c.phase.store(n::CLOSING, Relaxed);
                }
            }
            if (*p).ordinary == id() {
                return;
            }
            drop(g);
            core::hint::spin_loop();
        }
    }
}
pub(super) unsafe fn before_native(source: &[u8]) {
    if !enabled() {
        return;
    }
    unsafe {
        let g = lock().unwrap();
        let p = pressure();
        if (*p).ordinary != id() {
            return;
        }
        let index = (*p).intervals[id() as usize] as usize;
        let out = interval(index);
        if (*out).ordinal != 0 {
            stop(PANIC, 338);
        }
        let gpa = if matches!(source.first(), Some(0x89 | 0x8b | 0x48)) {
            (*owner()).route
        } else {
            route_under_authority(operand(), 4, Access::Read)
                .unwrap_or_else(|_| stop(PANIC, 336))
                .0
        };
        (*out).ordinal = (*p).ordinary_total + 1;
        (*out).heads = (*p).total;
        (*out).epoch = (*p).epoch;
        (*out).gpa = gpa;
        copy_frame(&raw mut (*out).initial, &raw const (*shared()).initial);
        (*out).before = guest_bytes(gpa);
        (*owner()).interval_visits[index] = (*state()).visits;
        attempt(&g);
    }
}
pub(super) unsafe fn native_done() {
    if !enabled() {
        return;
    }
    unsafe {
        let g = lock().unwrap();
        let p = pressure();
        if (*p).ordinary != id() {
            return;
        }
        let out = interval((*p).intervals[id() as usize] as usize);
        let v = visit();
        if (*out).ordinal == 0 || (*out).epoch != (*p).epoch {
            stop(PANIC, 339);
        }
        copy_frame(&raw mut (*out).captured, &raw const (*shared()).captured);
        (*out).after = guest_bytes((*out).gpa);
        if (*v).faults != 0 {
            copy_frame(&raw mut (*out).dirty_frame, &raw const (*v).fault_frame);
            (*out).faults = (*v).faults;
            [(*out).esr, (*out).far, (*out).pc, (*out).spsr] = (*v).exception;
        }
        checked((*p).finish_ordinary(id()));
        (*owner()).maintenance_intervals += 1;
        coordinator().phase.store(n::OPEN, Release);
        checked(g.trace(30, id()));
        attempt(&g);
        drop(g);
        event_boundary();
    }
}
