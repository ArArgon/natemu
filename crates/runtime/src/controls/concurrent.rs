//! Control transactions retain private CPU state while sharing coordinator authority.
use super::*;
use core::sync::atomic::Ordering::{AcqRel, Acquire, Relaxed, Release};
use natemu_abi::{concurrency as n, control_concurrent as cc};
pub(super) fn owner() -> *mut cc::Owner {
    (PRIVATE + cc::OWNER_OFFSET as u64) as *mut cc::Owner
}
pub(super) fn common() -> *mut cc::Shared {
    (PRIVATE + cc::SHARED_OFFSET as u64) as *mut cc::Shared
}
pub(super) fn coordinator() -> &'static n::Coordinator {
    unsafe { &*(n::COORD_VA as *const n::Coordinator) }
}
pub(super) fn enabled() -> bool {
    unsafe { (*owner()).enabled == 1 }
}
pub(super) fn id() -> u64 {
    unsafe { (*shared()).object.vcpu_id as u64 }
}
pub(super) fn incarnation() -> u64 {
    if enabled() {
        unsafe { (*owner()).incarnation }
    } else {
        1
    }
}
pub(super) fn source_generation() -> u64 {
    if enabled() {
        unsafe { (*owner()).source_generation }
    } else {
        1
    }
}
pub(super) fn lock() -> Option<crate::coordinator::Guard<'static>> {
    enabled().then(|| crate::coordinator::lock(coordinator()))
}
fn checked<T>(r: Result<T, u64>) -> T {
    r.unwrap_or_else(|e| stop(PANIC, e))
}
pub(super) fn all() -> u64 {
    (1 << coordinator().participants) - 1
}
fn actor() -> u64 {
    unsafe { (*common()).actor }
}
fn peers() -> u64 {
    all() ^ (1 << actor())
}
unsafe fn snapshot() -> cc::Snapshot {
    unsafe {
        let g = &*guest();
        cc::Snapshot {
            control: g.control,
            cs: g.segments[0],
            tr: (*long_mode::state()).tr,
            hidden: (*pae_state()).hidden.entries,
            hidden_generation: (*pae_state()).hidden.generation,
            context: (*state()).generation,
            source_generation: source_generation(),
            rip: g.rip,
            completed: (*state()).completed,
            gpr: g.gpr[..4].try_into().unwrap(),
            route: (*owner()).route,
        }
    }
}
pub(super) unsafe fn trace_visit(kind: u64) {
    if let Some(g) = lock() {
        unsafe {
            checked(g.trace(kind + (*state()).visits, id()));
        }
    }
}
pub(super) unsafe fn initialize() {
    if !enabled() {
        return;
    }
    unsafe {
        let c = coordinator();
        let p = common();
        let o = owner();
        if c.participants == 0
            || c.participants > n::MAX_CPUS as u64
            || (*shared()).object.configured_vcpus as u64 != c.participants
            || id() >= c.participants
            || (*p).n != c.participants
            || (*p).actor >= c.participants
            || (*p).profile != (*state()).profile
            || !cc::supported((*p).profile)
            || (*o).incarnation != 1
            || (*o).source_generation != 1
        {
            stop(REJECTED, 311);
        }
        asm!("mrs {0}, ttbr1_el1", out(reg)(*o).ttbr1);
        (*o).initial = snapshot();
        c.event_pending[id() as usize].store(1, Release);
        c.event_notifications[id() as usize].store(1, Release);
        c.ready.fetch_or(1 << id(), AcqRel);
        while c.ready.load(Acquire) != all() {
            core::hint::spin_loop();
        }
        if id() != actor() {
            loop {
                let ready = {
                    let _g = lock();
                    (*p).phase >= 1
                };
                if ready {
                    break;
                }
                core::hint::spin_loop();
            }
        }
    }
}
pub(super) unsafe fn before_control() {
    if !enabled() || id() != actor() {
        return;
    }
    unsafe {
        if (*owner()).local_cut == 2
            && cc::second_cut_completed((*state()).profile) == Some((*state()).completed)
        {
            loop {
                let ready = {
                    let _g = lock();
                    (*common()).peer_compiling == peers()
                };
                if ready {
                    break;
                }
                core::hint::spin_loop();
            }
            let g = lock().unwrap();
            (*owner()).local_cut = 3;
            checked(g.trace(100, id()));
            return;
        }
        if (*owner()).local_cut != 0
            || (*state()).completed != cc::cut_completed((*state()).profile).unwrap()
        {
            return;
        }
        {
            let g = lock().unwrap();
            (*owner()).local_cut = 1;
            (*owner()).before_local = snapshot();
            (*common()).before[id() as usize] = snapshot();
            (*common()).phase = 1;
            checked(g.trace(90, id()));
        }
        while coordinator().queued.load(Acquire) != peers() {
            core::hint::spin_loop();
        }
    }
}
pub(super) unsafe fn semantic_done(success: bool) {
    if !enabled() {
        return;
    }
    unsafe {
        {
            let g = lock().unwrap();
            (*owner()).local_controls += u64::from(success);
            checked(g.trace(0x3000 + (*state()).visits, id()));
            if id() == actor() && (*owner()).local_cut == 3 {
                if !success {
                    stop(PANIC, 315);
                }
                (*owner()).local_cut = 4;
                (*common()).second_commit = 1;
                checked(g.trace(101, id()));
                drop(g);
                loop {
                    let done = {
                        let _g = lock();
                        (*common()).peer_executed == peers()
                    };
                    if done {
                        break;
                    }
                    core::hint::spin_loop();
                }
                return;
            }
            if id() != actor() || (*owner()).local_cut != 1 {
                return;
            }
            (*owner()).local_cut = 2;
            (*owner()).after_local = snapshot();
            (*common()).after[id() as usize] = snapshot();
            (*common()).local_done = 1;
            (*common()).phase = 2;
            checked(g.trace(if success { 91 } else { 92 }, id()));
        }
        loop {
            let drained = {
                let _g = lock();
                (*common()).drained == peers()
            };
            if drained {
                break;
            }
            core::hint::spin_loop();
        }
        let g = lock().unwrap();
        (*common()).phase = 3;
        checked(g.trace(93, id()));
    }
}
pub(super) unsafe fn admit(source: &[u8]) {
    if !enabled() {
        return;
    }
    unsafe {
        if source_generation() == 2 {
            let _g = lock();
            if coordinator().reclaimed.load(Relaxed) != 1 {
                stop(PANIC, 324);
            }
            coordinator().new_entries.fetch_or(1 << id(), Relaxed);
            coordinator().new_references.fetch_or(1 << id(), Relaxed);
        }
        if !matches!(source.first(), Some(0x89 | 0x8b | 0x48)) {
            return;
        }
        let first = id() != actor()
            && (*owner()).local_cut == 0
            && (*state()).visits == cc::first_memory_visit((*state()).profile).unwrap();
        loop {
            let g = lock().unwrap();
            if coordinator().phase.load(Relaxed) == n::OPEN {
                let (key, cohort) = if first {
                    (
                        natemu_abi::schedule::ADMIT | 0x100 | (*state()).visits,
                        peers(),
                    )
                } else {
                    (
                        natemu_abi::schedule::ADMIT | 0x10000 | ((*state()).visits << 8) | id(),
                        1 << id(),
                    )
                };
                if !crate::schedule::permit(
                    &g,
                    key,
                    cohort,
                    cohort,
                    coordinator().phase.load(Relaxed),
                    (*state()).generation,
                ) {
                    drop(g);
                    core::hint::spin_loop();
                    continue;
                }
                let access = if source[0] == 0x89 {
                    Access::Write
                } else {
                    Access::Read
                };
                let result =
                    route_under_authority(operand(), if source[0] == 0x48 { 8 } else { 4 }, access);
                let (gpa, writable) = match result {
                    Ok(v) => v,
                    Err(e) => {
                        drop(g);
                        match e {
                            Fault::Page { address, error } => exception(14, address, error),
                            _ => stop(REJECTED, 312),
                        }
                    }
                };
                (*owner()).route = gpa;
                (*owner()).writable = u64::from(writable);
                (*owner()).admitted = 1;
                checked(g.admit(id()));
                if source_generation() == 2 {
                    coordinator().retained.fetch_and(!(1 << id()), Relaxed);
                }
                checked(g.trace(0x2000 + (*state()).visits, id()));
                if first {
                    (*common()).held |= 1 << id();
                    (*owner()).retained_code = 1;
                    checked(g.trace(94, id()));
                }
                break;
            }
            drop(g);
            core::hint::spin_loop();
        }
        if first {
            loop {
                let ready = {
                    let _g = lock();
                    (*common()).held == peers()
                };
                if ready {
                    break;
                }
                core::hint::spin_loop();
            }
            let delayed = (actor() + coordinator().participants - 1) % coordinator().participants;
            if coordinator().participants >= 3 && id() == delayed {
                while coordinator().queued.load(Acquire).count_ones() as u64
                    != coordinator().participants - 2
                {
                    core::hint::spin_loop();
                }
                let g = lock().unwrap();
                checked(g.trace(95, id()));
            }
        }
    }
}
pub(super) unsafe fn fault() -> (u64, bool) {
    unsafe {
        let first = id() != actor()
            && (*owner()).local_cut == 0
            && (*state()).visits == cc::first_memory_visit((*state()).profile).unwrap();
        loop {
            let g = lock().unwrap();
            if (*owner()).admitted != 1 || (*owner()).detached != 0 {
                stop(PANIC, 313);
            }
            let delayed = (actor() + coordinator().participants - 1) % coordinator().participants;
            let early = peers() & !(1 << delayed);
            let (key, cohort) = if first && coordinator().participants >= 3 && id() != delayed {
                (
                    natemu_abi::schedule::DETACH | 0x100 | (*state()).visits,
                    early,
                )
            } else {
                (
                    natemu_abi::schedule::DETACH | 0x10000 | ((*state()).visits << 8) | id(),
                    1 << id(),
                )
            };
            if !crate::schedule::permit(
                &g,
                key,
                cohort,
                cohort,
                coordinator().phase.load(Relaxed),
                (*state()).generation,
            ) {
                drop(g);
                core::hint::spin_loop();
                continue;
            }
            (*owner()).detached = 1;
            checked(g.detach_fault(id()));
            checked(g.trace(0x5000 + (*state()).visits, id()));
            if first {
                (*owner()).before_local = snapshot();
                (*common()).before[id() as usize] = snapshot();
            }
            break;
        }
        if first {
            loop {
                let done = {
                    let _g = lock();
                    (*common()).local_done != 0
                };
                if done {
                    break;
                }
                core::hint::spin_loop();
            }
            let g = lock().unwrap();
            (*owner()).after_local = snapshot();
            (*common()).after[id() as usize] = snapshot();
            if (*owner()).before_local != (*owner()).after_local {
                stop(PANIC, 314);
            }
            (*owner()).local_cut = 2;
            checked(g.trace(96, id()));
        }
        loop {
            let g = lock().unwrap();
            if checked(g.select()) == id() {
                return ((*owner()).route, (*owner()).writable != 0);
            }
            drop(g);
            core::hint::spin_loop();
        }
    }
}
pub(super) unsafe fn completed() {
    if !enabled() {
        return;
    }
    unsafe {
        let g = lock().unwrap();
        checked(g.trace(0x4000 + (*state()).visits, id()));
        if source_generation() == 2 {
            coordinator().new_completed.fetch_or(1 << id(), Relaxed);
        }
        if (*owner()).peer_reply_cut == 2
            && (*owner()).peer_reply_executed == 0
            && (*visit()).request == (*owner()).peer_expected
        {
            (*owner()).peer_reply_executed = 1;
            (*common()).peer_executed |= 1 << id();
            checked(g.trace(103, id()));
        }
        if (*owner()).admitted == 0 {
            return;
        }
        let c = coordinator();
        if (*owner()).detached != 0 {
            checked(g.complete(id()));
            (*owner()).detached = 0;
        } else {
            c.active.fetch_and(!(1 << id()), Relaxed);
        }
        (*owner()).admitted = 0;
        if id() != actor()
            && (*state()).visits == cc::first_memory_visit((*state()).profile).unwrap()
        {
            (*common()).drained |= 1 << id();
            checked(g.trace(97, id()));
        }
        if c.head.load(Relaxed) == c.length.load(Relaxed) && c.active.load(Relaxed) == 0 {
            c.phase.store(n::OPEN, Release);
            c.queued.store(0, Relaxed);
            c.head.store(0, Relaxed);
            c.length.store(0, Relaxed);
            c.completed.store(0, Relaxed);
        }
    }
}
pub(super) unsafe fn hold_reply(expected: &wire::Packet, reply: &wire::Packet) {
    if !enabled() || id() == actor() {
        return;
    }
    unsafe {
        if (*state()).profile == 3
            || (*owner()).peer_reply_cut != 0
            || (*state()).visits <= cc::first_memory_visit((*state()).profile).unwrap()
        {
            return;
        }
        {
            let g = lock().unwrap();
            (*owner()).peer_expected = *expected;
            (*owner()).peer_reply = *reply;
            (*owner()).before_peer_reply = snapshot();
            (*owner()).peer_reply_cut = 1;
            (*common()).peer_compiling |= 1 << id();
            checked(g.trace(99, id()));
        }
        loop {
            let ready = {
                let _g = lock();
                (*common()).second_commit != 0
            };
            if ready {
                break;
            }
            core::hint::spin_loop();
        }
        let g = lock().unwrap();
        (*owner()).after_peer_reply = snapshot();
        if (*owner()).before_peer_reply != (*owner()).after_peer_reply || !c::valid(expected, reply)
        {
            stop(PANIC, 316);
        }
        (*owner()).peer_reply_cut = 2;
        checked(g.trace(102, id()));
    }
}
pub(super) unsafe fn finished() {
    if !enabled() {
        return;
    }
    unsafe {
        let g = lock().unwrap();
        coordinator()
            .new_references
            .fetch_and(!(1 << id()), Relaxed);
        (*common()).finished |= 1 << id();
        checked(g.trace(98, id()));
        drop(g);
        loop {
            let done = {
                let _g = lock();
                (*common()).finished == all()
            };
            if done {
                break;
            }
            core::hint::spin_loop();
        }
        maintenance::event_boundary();
    }
}
pub(super) unsafe fn source_cut(expected: &wire::Packet, reply: &wire::Packet) -> bool {
    if !enabled() {
        return false;
    }
    unsafe {
        if cc::final_visit((*state()).profile) != Some((*state()).visits)
            || source_generation() != 1
        {
            return false;
        }
        let o = owner();
        let c = coordinator();
        {
            let g = lock().unwrap();
            if (*(natemu_abi::maintenance::VA as *const natemu_abi::maintenance::State)).finished
                != 1
            {
                stop(PANIC, 340);
            }
            (*o).source_expected = *expected;
            (*o).source_reply = *reply;
            (*o).retained_code = 1;
            copy_record(
                (&raw mut (*o).code_before_retire).cast(),
                CODE_VA as *const u8,
                c::MAX_VISITS * c::SLOT_BYTES,
            );
            (*common()).final_ready |= 1 << id();
            checked(g.trace(114, id()));
        }
        loop {
            let g = lock().unwrap();
            if (*common()).final_ready == all() && crate::containment::source_cut() {
                if id() == actor() {
                    if c.active.load(Relaxed) != 0
                        || (*common()).semantic_active != 0
                        || c.queued.load(Relaxed) != 0
                        || c.servicing.load(Relaxed) != n::NO_CPU
                        || c.retained.load(Relaxed) != all()
                    {
                        stop(PANIC, 319);
                    }
                    crate::containment::panic_at_source(&g, *expected);
                    let packet = crate::containment::controls_request(*expected);
                    drop(g);
                    crate::containment::deferred_gate(packet, natemu_abi::containment::SOURCE_GATE);
                }
                drop(g);
                core::hint::spin_loop();
                continue;
            }
            if (*common()).final_ready == all()
                && id() == actor()
                && (*common()).source_generation == 1
            {
                if c.active.load(Relaxed) != 0
                    || (*common()).semantic_active != 0
                    || c.queued.load(Relaxed) != 0
                    || c.servicing.load(Relaxed) != n::NO_CPU
                    || c.retained.load(Relaxed) != all()
                {
                    stop(PANIC, 319);
                }
                c.phase.store(n::EXCLUSIVE, Relaxed);
                c.old_eligible.store(0, Relaxed);
                c.generation.store(2, Relaxed);
                let inst = cc::replacement((*state()).profile).unwrap();
                let offset = cc::replacement_offset((*state()).profile).unwrap();
                for cpu in 0..c.participants {
                    copy_record(
                        (PRIVATE + r::SOURCE_OFFSET as u64 + cpu * 256 + offset as u64) as *mut u8,
                        inst.bytes.as_ptr(),
                        inst.len,
                    );
                }
                asm!("dsb ish");
                (*common()).source_generation = 2;
                checked(g.trace(115, id()));
            }
            if (*common()).source_generation == 2 {
                break;
            }
            drop(g);
            core::hint::spin_loop();
        }
        let delayed = crate::containment::delayed((actor() + c.participants - 1) % c.participants);
        loop {
            let g = lock().unwrap();
            if crate::containment::withhold_ack(&g, *expected) {
                drop(g);
                crate::containment::park();
            }
            let others = all() ^ (1 << id());
            if (id() != delayed || (*common()).source_acknowledgements == others)
                && crate::schedule::permit(
                    &g,
                    natemu_abi::schedule::SOURCE_ACK,
                    all(),
                    crate::schedule::except_last(
                        all(),
                        delayed,
                        (*common()).source_acknowledgements,
                    ),
                    c.phase.load(Relaxed),
                    2,
                )
            {
                (*o).source_generation = 2;
                invalidate();
                (*common()).source_acknowledgements |= 1 << id();
                c.acknowledgements.fetch_or(1 << id(), Relaxed);
                let mut current = *expected;
                current[5] = 2;
                if c::valid(&current, reply)
                    || (*o).cached_before_source != 1
                    || eligible((*state()).visits as usize - 1)
                {
                    stop(PANIC, 320);
                }
                (*o).source_rejections = 1;
                (*o).source_entry_rejected = 1;
                checked(g.trace(116, id()));
                break;
            }
            drop(g);
            core::hint::spin_loop();
        }
        loop {
            let g = lock().unwrap();
            if (*common()).source_acknowledgements == all() {
                if core::slice::from_raw_parts(CODE_VA as *const u8, c::MAX_VISITS * c::SLOT_BYTES)
                    != (*o).code_before_retire
                {
                    stop(PANIC, 321);
                }
                if c.retained.load(Relaxed) != 0 && (*o).reuse_blocked == 0 {
                    (*o).reuse_blocked = 1;
                    c.reuse_blocked.fetch_add(1, Relaxed);
                    checked(g.trace(117, id()));
                }
                let others = all() ^ (1 << id());
                if (id() != delayed || c.retained.load(Relaxed) & others == 0)
                    && crate::schedule::permit(
                        &g,
                        natemu_abi::schedule::SOURCE_RELEASE,
                        all(),
                        crate::schedule::except_last(
                            all(),
                            delayed,
                            all() ^ c.retained.load(Relaxed),
                        ),
                        c.phase.load(Relaxed),
                        2,
                    )
                {
                    c.retained.fetch_and(!(1 << id()), Relaxed);
                    (*o).released_code = 1;
                    checked(g.trace(118, id()));
                    if c.retained.load(Relaxed) == 0 {
                        c.reclaimed.store(1, Relaxed);
                        c.phase.store(n::OPEN, Release);
                    }
                    break;
                }
            }
            drop(g);
            core::hint::spin_loop();
        }
        while c.reclaimed.load(Acquire) == 0 {
            core::hint::spin_loop();
        }
        translation::replace_bytes_for_owner(&[0; c::MAX_VISITS * c::SLOT_BYTES], id());
        copy_record(
            (&raw mut (*o).code_after_reclaim).cast(),
            CODE_VA as *const u8,
            c::MAX_VISITS * c::SLOT_BYTES,
        );
        (*o).reclaimed = 1;
        let g = lock().unwrap();
        checked(g.trace(119, id()));
        true
    }
}
pub(super) unsafe fn after_native() {
    if !enabled() {
        return;
    }
    unsafe {
        if (*owner()).peer_reply_executed != 0 && (*visit()).request == (*owner()).peer_expected {
            loop {
                let done = {
                    let _g = lock();
                    (*common()).peer_executed == peers()
                };
                if done {
                    break;
                }
                core::hint::spin_loop();
            }
        }
        if (*state()).profile != 21 || !matches!((*state()).completed, 5 | 8) {
            return;
        }
        let index = usize::from((*state()).completed == 8);
        let hidden = (*pae_state()).hidden;
        {
            let g = lock().unwrap();
            (*common()).pae_ready[index] |= 1 << id();
            checked(g.trace(108 + index as u64, id()));
        }
        loop {
            let g = lock().unwrap();
            if (*common()).pae_ready[index] == all()
                && id() == actor()
                && (*common()).pae_published & (1 << index) == 0
            {
                if coordinator().active.load(Relaxed) != 0
                    || (*common()).semantic_active != 0
                    || coordinator().queued.load(Relaxed) != 0
                {
                    stop(PANIC, 317);
                }
                coordinator().phase.store(n::EXCLUSIVE, Relaxed);
                let root: u64 = if index == 0 { 0x4001 } else { 0x2001 };
                if !PAE_TABLES.write(pae::ROOT, root) {
                    stop(PANIC, 317);
                }
                (*pae_state()).mutations += 1;
                (*common()).pae_published |= 1 << index;
                checked(g.trace(110 + index as u64, id()));
            }
            if (*common()).pae_published & (1 << index) != 0 {
                if (*pae_state()).hidden != hidden {
                    stop(PANIC, 318);
                }
                if (*common()).pae_acknowledgements[index] & (1 << id()) == 0
                    && crate::schedule::permit(
                        &g,
                        natemu_abi::schedule::ACK | index as u64,
                        all(),
                        all(),
                        coordinator().phase.load(Relaxed),
                        index as u64 + 1,
                    )
                {
                    (*common()).pae_acknowledgements[index] |= 1 << id();
                    checked(g.trace(112 + index as u64, id()));
                }
                if (*common()).pae_acknowledgements[index] == all() {
                    coordinator().phase.store(n::OPEN, Release);
                    break;
                }
            }
            drop(g);
            core::hint::spin_loop();
        }
    }
}
