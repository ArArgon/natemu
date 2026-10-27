//! A semantic two-element helper retains its sampled device value across a
//! native store fault, writer closing, FIFO repair, and a later remap.
use super::*;
use core::sync::atomic::Ordering::Relaxed;
use natemu_abi::{
    coherence::Walk,
    concurrency as n,
    paging::{self, Access, Control},
    partial_progress as p, schedule as sc,
};
unsafe extern "C" {
    fn partial_store(address: u64, value: u64);
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
}
fn owner() -> *mut p::Owner {
    (PRIVATE + p::OWNER_OFFSET as u64) as *mut p::Owner
}
fn common() -> *mut p::Common {
    (PRIVATE + p::COMMON_OFFSET as u64) as *mut p::Common
}
fn coord() -> &'static n::Coordinator {
    unsafe { &*(n::COORD_VA as *const n::Coordinator) }
}
fn id() -> u64 {
    unsafe { (*owner()).id }
}
fn all() -> u64 {
    unsafe { (1 << (*common()).n) - 1 }
}
fn checked<T>(r: Result<T, u64>) -> T {
    r.unwrap_or_else(|e| stop(PANIC, e))
}
fn pause() {
    core::hint::spin_loop();
}
const RAM: GuestRam = unsafe { GuestRam::new(p::DATA, 0x4000, PRIVATE + p::RAM_OFFSET as u64) };
const TABLES: GuestRam = unsafe { GuestRam::new(0x1000, 0x4000, PRIVATE + p::TABLE_OFFSET as u64) };
unsafe fn walk(g: &coordinator::Guard<'_>, address: u64, access: Access) -> paging::Translation {
    unsafe {
        let o = owner();
        let slot = (*o).walk_count as usize;
        if slot >= p::WALKS {
            stop(PANIC, 840);
        }
        let w = &raw mut (*o).walks[slot];
        ptr::write(w, Walk::ZERO);
        (*w).address = address;
        (*w).intent = match access {
            Access::Read => 0,
            Access::Write => 1,
            Access::Fetch => 2,
        };
        let t = paging::walk(
            &mut GuestTables::observed(TABLES, table_log::WalkLog(w)),
            Control {
                cr3: 0x1000,
                cpl: 0,
                wp: 1,
                nxe: 1,
                smep: 0,
            },
            address,
            access,
        )
        .unwrap_or_else(|_| stop(REJECTED, 841));
        (*w).gpa = t.gpa;
        (*w).leaf_bytes = t.leaf_bytes;
        (*w).writable = t.writable as u64;
        (*w).dirty = t.dirty as u64;
        (*w).user = t.user as u64;
        (*w).executable = t.executable as u64;
        (*w).wb = t.wb as u64;
        (*o).walk_count += 1;
        checked(g.trace(0x1000 + slot as u64, id()));
        t
    }
}
/// The one shadowed guest page of this profile.
fn clear() {
    tables::unmap(p::DATA).unwrap_or_else(|_| stop(REJECTED, 842));
}
unsafe fn install(t: paging::Translation) {
    if !t.wb || !(p::DATA..p::DATA + 8192).contains(&t.gpa) {
        stop(REJECTED, 842);
    }
    tables::install(p::DATA, &RAM, t.gpa, t.writable && t.dirty)
        .unwrap_or_else(|_| stop(REJECTED, 842));
}
unsafe fn snapshot(index: usize) {
    unsafe {
        if index >= p::IMAGES || (*common()).images & (1 << index) != 0 {
            stop(PANIC, 843);
        }
        copy_record(
            (PRIVATE + p::IMAGE_OFFSET as u64 + index as u64 * 8192) as *mut u8,
            (PRIVATE + p::RAM_OFFSET as u64) as *const u8,
            8192,
        );
        (*common()).images |= 1 << index;
    }
}
unsafe fn event(g: &coordinator::Guard<'_>) {
    unsafe {
        let o = owner();
        if (*o).pending == 0 {
            return;
        }
        (*o).event_attempts += 1;
        let guest = &(*o).after;
        let eligible = coord().phase.load(Relaxed) == n::OPEN
            && coord().active.load(Relaxed) == 0
            && coord().retained.load(Relaxed) == 0
            && (*o).phase == 3
            && (*o).reads == 2
            && (*o).writes == 2
            && guest.gpr[1] == 0
            && guest.rip == (*o).before.rip + 2
            && guest.rflags & (1 << 9) != 0;
        if eligible {
            (*o).pending = 0;
            (*o).delivered += 1;
            (*o).delivery_state = [
                guest.rip,
                guest.gpr[1],
                guest.gpr[6],
                guest.gpr[7],
                guest.rflags,
            ];
            (*common()).delivered |= 1 << id();
            (*common()).pending_events &= !(1 << id());
            checked(g.trace(p::EVENT_DELIVERED, id()));
        } else {
            (*o).event_blocked += 1;
            checked(g.trace(p::EVENT_BLOCKED, id()));
        }
    }
}
/// Actual guarded mutation eligibility, used for both the denied and committed
/// attempts. No raw page-table write occurs outside this operation.
unsafe fn try_mutate(g: &coordinator::Guard<'_>) -> bool {
    unsafe {
        if id() != (*common()).actor
            || (*common()).mutated != 0
            || (*common()).helper_done != all()
            || (*common()).released != all()
            || coord().retained.load(Relaxed) != 0
            || coord().active.load(Relaxed) != 0
            || coord().completed.load(Relaxed) != all()
            || coord().servicing.load(Relaxed) != n::NO_CPU
        {
            return false;
        }
        snapshot(1);
        if !TABLES.write(0x4000 + 256 * 8, (p::DATA + 4096) | 7) {
            stop(PANIC, 843);
        }
        (*common()).mutated = 1;
        (*common()).destination_generation = 2;
        checked(g.trace(p::MUTATED, id()));
        true
    }
}
pub unsafe fn initialize() -> ! {
    unsafe {
        let o = owner();
        let c = common();
        (*o).store_pc = partial_store as *const () as u64;
        copy_record((*o).store_bytes.as_mut_ptr(), (*o).store_pc as *const u8, 8);
        let config = sc::Config::new(sc::PARTIAL, (*c).n, (*c).ordinal)
            .unwrap_or_else(|| stop(REJECTED, 844));
        if !schedule::enabled()
            || config.actor != (*c).actor
            || id() >= config.n
            || coord().participants != config.n
            || (*shared()).object.configured_vcpus as u64 != config.n
            || (*shared()).object.vcpu_id as u64 != id()
            || (*o).before != p::guest(&(*o).initial, id())
        {
            stop(REJECTED, 845);
        }
        loop {
            let g = coordinator::lock(coord());
            if !schedule::permit(&g, sc::ADMIT, all(), all(), coord().phase.load(Relaxed), 1) {
                drop(g);
                pause();
                continue;
            }
            let t = walk(&g, (*o).before.rip, Access::Fetch);
            if t.gpa != (*o).before.rip || !t.wb {
                stop(REJECTED, 846);
            }
            if !RAM.matches(p::RIP + id() * 256, &p::SOURCE) {
                stop(REJECTED, 847);
            }
            let t = walk(&g, p::destination(id()), Access::Write);
            install(t);
            checked(g.admit(id()));
            // Two real notifications coalesce into one pending handoff while
            // this helper is admitted; neither creates an architectural event.
            for _ in 0..2 {
                (*o).notifications += 1;
                (*o).pending = 1;
                (*c).pending_events |= 1 << id();
                checked(g.trace(p::NOTIFIED, id()));
            }
            (*o).retained = 1;
            (*c).ready |= 1 << id();
            checked(g.trace(p::READY, id()));
            break;
        }
        loop {
            let g = coordinator::lock(coord());
            let ready = (*c).ready == all();
            drop(g);
            if ready {
                break;
            }
            pause();
        }
        for element in 0..2 {
            {
                let g = coordinator::lock(coord());
                if (*o).phase != 0 || (*o).reads != element || (*o).writes != element {
                    stop(PANIC, 848);
                }
                (*o).reads += 1;
                // Device side effect is driven by actual reads, not requested
                // element number: repeating a read changes the returned byte.
                (*o).sampled = p::value(id(), (*o).reads - 1);
                (*o).read_values[element as usize] = (*o).sampled;
                (*o).subaccess = element;
                (*o).phase = 1;
                (*o).source_route = p::device(id()) + element;
                if element == 1 {
                    let t = walk(&g, p::destination(id()) + 1, Access::Write);
                    (*o).destination_route = t.gpa;
                    (*o).destination_writable = (t.writable && t.dirty) as u64;
                    clear();
                }
                checked(g.trace(p::SAMPLED, id()));
            }
            partial_store(p::destination(id()) + element, (*o).sampled);
            let g = coordinator::lock(coord());
            (*o).writes += 1;
            (*o).phase = 0;
            (*o).after.gpr[1] -= 1;
            (*o).after.gpr[6] += 1;
            (*o).after.gpr[7] += 1;
            if element == 1 {
                (*o).after.rip += 2;
                checked(g.complete(id()));
                (*c).helper_done |= 1 << id();
                checked(g.trace(p::HELPER_DONE, id()));
            }
        }
        loop {
            let g = coordinator::lock(coord());
            if schedule::permit(
                &g,
                sc::RELEASE,
                all(),
                all(),
                coord().phase.load(Relaxed),
                1,
            ) {
                (*o).retained = 0;
                (*o).released = 1;
                (*c).released |= 1 << id();
                coord().retained.fetch_and(!(1 << id()), Relaxed);
                checked(g.trace(p::RELEASED, id()));
                break;
            }
            drop(g);
            pause();
        }
        loop {
            let g = coordinator::lock(coord());
            if (*c).mutated == 0 {
                try_mutate(&g);
            }
            if (*c).mutated != 0 {
                let delayed = ((*c).actor + (*c).n - 1) % (*c).n;
                if id() == delayed
                    && (*c).acknowledgements == all() ^ (1 << delayed)
                    && (*c).admission_blocked == 0
                {
                    if g.admit(id()).is_ok() {
                        stop(PANIC, 849);
                    }
                    (*c).admission_blocked = 1;
                    checked(g.trace(p::ADMISSION_BLOCKED, id()));
                }
                if (*c).acknowledgements & (1 << id()) == 0
                    && (id() != delayed || (*c).admission_blocked == 1)
                    && schedule::permit(
                        &g,
                        sc::ACK,
                        all(),
                        schedule::except_last(all(), delayed, (*c).acknowledgements),
                        coord().phase.load(Relaxed),
                        2,
                    )
                {
                    clear();
                    let t = walk(&g, p::destination(id()), Access::Read);
                    install(t);
                    (*c).acknowledgements |= 1 << id();
                    coord()
                        .acknowledgements
                        .store((*c).acknowledgements, Relaxed);
                    checked(g.trace(p::ACK, id()));
                }
                if id() == (*c).actor && (*c).acknowledgements == all() && (*c).opened == 0 {
                    coord().queued.store(0, Relaxed);
                    coord().length.store(0, Relaxed);
                    coord().head.store(0, Relaxed);
                    coord().completed.store(0, Relaxed);
                    coord().phase.store(n::OPEN, Relaxed);
                    (*c).opened = 1;
                    checked(g.trace(p::OPENED, id()));
                }
            }
            if (*c).opened != 0 {
                break;
            }
            drop(g);
            pause();
        }
        loop {
            let g = coordinator::lock(coord());
            if schedule::permit(
                &g,
                sc::ADMIT | 1,
                all(),
                all(),
                coord().phase.load(Relaxed),
                2,
            ) {
                checked(g.admit(id()));
                (*o).phase = 2;
                break;
            }
            drop(g);
            pause();
        }
        partial_store(p::destination(id()) + 2, p::probe_value(id()));
        {
            let g = coordinator::lock(coord());
            (*o).probe_writes = 1;
            (*o).phase = 3;
            coord().active.fetch_and(!(1 << id()), Relaxed);
            coord().retained.fetch_and(!(1 << id()), Relaxed);
            (*c).probes_done |= 1 << id();
            checked(g.trace(p::PROBE_DONE, id()));
        }
        loop {
            let g = coordinator::lock(coord());
            if (*c).probes_done == all() {
                if id() == (*c).actor && (*c).images & 4 == 0 {
                    snapshot(2);
                }
                event(&g);
                if (*o).delivered == 1 && (*c).images == 7 {
                    (*c).finished |= 1 << id();
                    coord().finished.fetch_or(1 << id(), Relaxed);
                    checked(g.trace(p::STOPPED, id()));
                    drop(g);
                    stop(SUCCESS, 0);
                }
            }
            drop(g);
            pause();
        }
    }
}
pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        if fault == 0 {
            stop(NATIVE_FAULT, 850);
        }
        let o = owner();
        let s = shared();
        let probe = (*o).phase == 2;
        let (esr, far, pc, spsr): (u64, u64, u64, u64);
        asm!("mrs {0},ESR_EL1","mrs {1},FAR_EL1","mrs {2},ELR_EL1","mrs {3},SPSR_EL1",out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
        if !p::direct_store(
            esr,
            far,
            pc,
            spsr,
            partial_store as *const () as u64,
            id(),
            probe,
        ) || (*o).faults != u64::from(probe)
            || (*o).reads != 2
            || (*o).writes != if probe { 2 } else { 1 }
            || (*s).captured.x[0] != far
            || (*s).captured.x[1]
                != if probe {
                    p::probe_value(id())
                } else {
                    (*o).sampled
                }
            || !matches!((*o).phase, 1 | 2)
        {
            stop(NATIVE_FAULT, 851);
        }
        let f = &raw mut (*o).fault[usize::from(probe)];
        ptr::write(
            f,
            p::Fault {
                frame: (*s).captured,
                esr,
                far,
                pc,
                spsr,
                resume_pc: 0,
                value: (*s).captured.x[1],
                gpa: if probe { 0 } else { (*o).destination_route },
            },
        );
        (*o).faults += 1;
        if probe {
            let g = coordinator::lock(coord());
            let t = walk(&g, far, Access::Write);
            install(t);
            (*o).probe_route = t.gpa;
            (*f).gpa = t.gpa;
            (*f).resume_pc = pc;
            (*o).retries += 1;
            checked(g.trace(p::RETRY, id()));
            drop(g);
            resume_exception(&raw const (*f).frame, pc, spsr);
        }
        (*o).held = (*o).after;
        loop {
            let g = coordinator::lock(coord());
            if schedule::permit(&g, sc::DETACH, all(), all(), coord().phase.load(Relaxed), 1) {
                checked(g.detach_fault(id()));
                checked(g.trace(p::HELD, id()));
                event(&g);
                break;
            }
            drop(g);
            pause();
        }
        loop {
            let g = coordinator::lock(coord());
            if coord().queued.load(Relaxed) == all() {
                if id() == (*common()).actor && (*common()).mutation_blocked == 0 {
                    if try_mutate(&g) || coord().retained.load(Relaxed) != all() {
                        stop(PANIC, 852);
                    }
                    (*common()).mutation_blocked = 1;
                    snapshot(0);
                    checked(g.trace(p::MUTATION_BLOCKED, id()));
                }
                if crate::containment::partial_cut() {
                    if id() == (*common()).actor {
                        let rip = (*o).before.rip + 16;
                        let route = walk(&g, rip, Access::Fetch);
                        if route.gpa != rip
                            || !route.wb
                            || !RAM.matches(
                                p::RIP + id() * 256 + 16,
                                &natemu_abi::containment::PARTIAL_SOURCE,
                            )
                        {
                            stop(REJECTED, 854);
                        }
                        let packet = natemu_abi::controls::request_owner(
                            (*shared()).lifecycle.vm,
                            id(),
                            1,
                            1,
                            1,
                            4,
                            rip,
                            &natemu_abi::containment::PARTIAL_SOURCE,
                            1,
                        )
                        .unwrap_or_else(|| stop(REJECTED, 855));
                        drop(g);
                        crate::containment::deferred_gate(
                            packet,
                            natemu_abi::containment::PARTIAL_GATE,
                        );
                    }
                    drop(g);
                    pause();
                    continue;
                }
                if (*common()).mutation_blocked == 1 && checked(g.select()) == id() {
                    if (*o).source_route != p::device(id()) + 1
                        || (*o).sampled != p::value(id(), 1)
                        || (*o).destination_writable != 1
                        || (*o).after != (*o).held
                    {
                        stop(PANIC, 853);
                    }
                    let t = paging::Translation {
                        gpa: (*o).destination_route,
                        leaf_bytes: 4096,
                        writable: true,
                        dirty: true,
                        user: true,
                        executable: true,
                        wb: true,
                    };
                    install(t);
                    (*o).resumed = (*o).after;
                    (*f).resume_pc = pc;
                    (*o).retries += 1;
                    checked(g.trace(p::RETRY, id()));
                    drop(g);
                    resume_exception(&raw const (*f).frame, pc, spsr);
                }
            }
            drop(g);
            pause();
        }
    }
}
