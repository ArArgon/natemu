//! Opt-in postcommit cohort: retain STLR only, drain, remap, then reopen.
use super::*;
use core::sync::atomic::Ordering::Relaxed;
use natemu_abi::{
    atomic_progress as a, concurrency as c, ordering as o,
    paging::{self, Access, Control, Tables},
    schedule as sched, shadow,
};
unsafe extern "C" {
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
}
fn state() -> *mut a::State {
    (PRIVATE + a::OFFSET as u64) as *mut a::State
}
fn coord() -> &'static c::Coordinator {
    unsafe { &(*state()).coordinator }
}
fn id() -> u64 {
    unsafe { (*shared()).object.vcpu_id as u64 }
}
fn all() -> u64 {
    unsafe { (1 << (*state()).n) - 1 }
}
fn record() -> *mut a::Owner {
    unsafe { &raw mut (*state()).owners[id() as usize] }
}
fn ordered() -> *mut o::Owner {
    (PRIVATE + o::OWNER_OFFSET as u64) as *mut o::Owner
}
fn leaf() -> *mut u64 {
    (PRIVATE + shadow::leaf_offset(o::RESULTS).unwrap() as u64) as *mut u64
}
fn checked(result: Result<(), u64>) {
    if let Err(e) = result {
        stop(PANIC, e);
    }
}
pub fn enabled() -> bool {
    if !schedule::enabled() {
        return false;
    }
    unsafe { (*(sched::VA as *const sched::State)).config.family == sched::ATOMIC }
}
pub unsafe fn initialize() {
    unsafe {
        let s = state();
        let c = &(*(sched::VA as *const sched::State)).config;
        let common = &*((PRIVATE + o::COMMON_OFFSET as u64) as *const o::Common);
        if common.profile != 9 || common.rotation != c.actor || common.n != c.n {
            stop(REJECTED, 830);
        }
        if !c.valid()
            || (*s).magic != a::MAGIC
            || (*s).n != c.n
            || (*s).actor != c.actor
            || (*s).writer != (c.actor + 1) % c.n
            || (*ordered()).variant != 7
        {
            stop(REJECTED, 830);
        }
    }
}
pub fn first() -> bool {
    enabled() && unsafe { (*ordered()).trial == 0 }
}
pub unsafe fn result_gpa() -> u64 {
    unsafe {
        if enabled() {
            (*ordered()).routes[1]
        } else {
            o::RESULTS
        }
    }
}
#[inline(never)]
pub unsafe fn copy_image(to: *mut u8) {
    unsafe {
        copy_record(
            to,
            (PRIVATE + o::RAM_OFFSET as u64) as *const u8,
            o::IMAGE_BYTES,
        );
        copy_record(
            to.add(o::IMAGE_BYTES),
            (PRIVATE + a::SPARE_OFFSET as u64) as *const u8,
            4096,
        );
    }
}
unsafe fn cut(index: usize) {
    unsafe {
        if (*state()).snapshot_count != index as u64 {
            stop(PANIC, 831);
        }
        copy_image((*state()).cuts[index].as_mut_ptr());
        (*state()).snapshot_count += 1;
    }
}
pub unsafe fn reset_spare() {
    unsafe {
        let page = (PRIVATE + a::SPARE_OFFSET as u64) as *mut u8;
        ptr::write_bytes(page, 0x5a, 4096);
        for cpu in 0..(*state()).n {
            ptr::write_volatile(page.add(cpu as usize * 128).cast::<u32>(), 0);
        }
    }
}
#[inline(never)]
pub unsafe fn admit(index: u64) {
    unsafe {
        loop {
            let g = coordinator::lock(coord());
            if schedule::permit(&g, sched::ADMIT | index, all(), all(), c::OPEN, index + 1) {
                checked(g.admit(id()));
                checked(g.trace(a::ADMIT_BASE + index, id()));
                return;
            }
            drop(g);
            core::hint::spin_loop();
        }
    }
}
unsafe fn post(g: &coordinator::Guard<'_>) {
    unsafe {
        let cpu = id() as usize;
        for _ in 0..2 {
            let _ = coord().event_pending[cpu].compare_exchange(0, 1, Relaxed, Relaxed);
            coord().event_notifications[cpu].fetch_add(1, Relaxed);
            checked(g.trace(a::POST, id()));
        }
        let r = record();
        (*r).event.wait_checks += 1;
        let blockers = c::event_blockers(
            coord().phase.load(Relaxed),
            false,
            false,
            (*r).before_guest.rflags,
            0,
            0,
        );
        if blockers == 0 || (*r).event.deliveries != 0 {
            stop(PANIC, 832);
        }
        (*r).event.blocked |= blockers;
        checked(g.trace(a::BLOCKED_EVENT, id()));
    }
}
#[inline(never)]
pub unsafe fn park(pc: u64, spsr: u64) -> ! {
    unsafe {
        let r = record();
        let o = ordered();
        let bit = 1 << id();
        if pc != CODE_VA + 16
            || (*r).queued != 0
            || ptr::read_volatile(leaf()) != 0
            || (*o).routes[1] != o::RESULTS
        {
            stop(PANIC, 833);
        }
        (*r).saved_gpa = (*o).routes[1] + id() * 128;
        (*r).fault_pc = pc;
        copy_record(
            (&raw mut (*r).fault_frame).cast(),
            (&raw const (*o).current).cast(),
            size_of::<NativeFrame>(),
        );
        loop {
            let g = coordinator::lock(coord());
            if (*r).queued == 0 {
                if !schedule::permit(&g, sched::DETACH, all(), all(), coord().phase.load(Relaxed), 1) {
                    drop(g);
                    core::hint::spin_loop();
                    continue;
                }
                checked(g.detach_fault(id()));
                (*r).queued = 1;
                (*state()).parked |= bit;
                post(&g);
            }
            if (*state()).parked == all() && (*state()).requested == 0 && id() == (*state()).writer
            {
                // Every SWPAL has committed, while every result store is still
                // faulted. This is a coherent snapshot with no native writer.
                cut(0);
                copy_record(
                    (*state()).tables_before.as_mut_ptr(),
                    (PRIVATE + o::TABLE_OFFSET as u64) as *const u8,
                    16384,
                );
                (*state()).requested = 1;
                checked(g.trace(a::REMAP_REQUEST, id()));
            }
            if (*state()).requested != 0 {
                let selected = g.select().unwrap_or_else(|e| stop(PANIC, e));
                if selected == id() {
                    if (*r).resumed != 0 || (*r).saved_gpa != o::RESULTS + id() * 128 {
                        stop(PANIC, 834);
                    }
                    ordering::mapping_gpa(o::RESULTS, (*r).saved_gpa & !4095, true, true);
                    (*r).saved_leaf = ptr::read_volatile(leaf());
                    (*r).resumed = 1;
                    (*r).resume_pc = pc;
                    checked(g.trace(a::RETRY_STORE, id()));
                    drop(g);
                    resume_exception(&raw const (*o).current, pc, spsr)
                }
            }
            drop(g);
            core::hint::spin_loop();
        }
    }
}
struct WalkTables;
impl Tables for WalkTables {
    fn read(&mut self, gpa: u64) -> Option<u64> {
        if !(0x1000..0x5000).contains(&gpa) || gpa & 7 != 0 {
            return None;
        }
        unsafe {
            let w = &raw mut (*record()).walk;
            let index = (*w).read_count as usize;
            if index >= 4 {
                return None;
            }
            let value =
                ptr::read_volatile((PRIVATE + o::TABLE_OFFSET as u64 + gpa - 0x1000) as *const u64);
            (*w).reads[index] = [gpa, value];
            (*w).read_count += 1;
            (*ordered()).table_reads += 1;
            Some(value)
        }
    }
    fn write(&mut self, _: u64, _: u64) -> bool {
        unsafe {
            (*record()).walk.writes += 1;
            (*ordered()).table_writes += 1;
        }
        false
    }
}
unsafe fn new_route() {
    unsafe {
        let address = o::RESULTS + id() * 128;
        (*record()).walk.address = address;
        (*ordered()).walks += 1;
        let t = paging::walk(
            &mut WalkTables,
            Control {
                cr3: 0x1000,
                cpl: 0,
                wp: 1,
                nxe: 1,
                smep: 0,
            },
            address,
            Access::Write,
        )
        .unwrap_or_else(|_| stop(PANIC, 835));
        if t.gpa != a::NEW_RESULTS + id() * 128
            || t.leaf_bytes != 4096
            || !t.wb
            || !t.writable
            || !t.dirty
        {
            stop(PANIC, 836);
        }
        let w = &raw mut (*record()).walk;
        (*w).gpa = t.gpa;
        (*w).leaf_bytes = t.leaf_bytes;
        (*w).writable = u64::from(t.writable);
        (*w).dirty = u64::from(t.dirty);
        (*ordered()).routes[1] = t.gpa & !4095;
    }
}
unsafe fn deliver(g: &coordinator::Guard<'_>) {
    unsafe {
        let r = record();
        let bit = 1 << id();
        if (*state()).delivered & bit != 0 {
            return;
        }
        let blockers = c::event_blockers(
            coord().phase.load(Relaxed),
            coord().active.load(Relaxed) & bit != 0,
            (*r).completed != 0,
            (*r).completed_guest.rflags,
            (*r).event.shadow,
            (*r).event.priority,
        );
        if blockers != 0 || coord().event_pending[id() as usize].load(Relaxed) != 1 {
            stop(PANIC, 837);
        }
        copy_record(
            (&raw mut (*r).event.packet).cast(),
            (&raw const (*r).completed_guest).cast(),
            size_of::<natemu_abi::recovery::GuestState>(),
        );
        (*r).event.boundary = 4;
        (*r).event.deliveries = 1;
        (*r).event.consumed = 1;
        coord().event_pending[id() as usize].store(2, Relaxed);
        (*state()).delivered |= bit;
        checked(g.trace(a::DELIVER, id()));
    }
}
#[inline(never)]
pub unsafe fn returned(index: u64) {
    unsafe {
        let bit = 1 << id();
        if index != 0 {
            let g = coordinator::lock(coord());
            if coord().active.load(Relaxed) & bit == 0 || (*state()).opened != 1 {
                stop(PANIC, 838);
            }
            coord().active.fetch_and(!bit, Relaxed);
            coord().retained.fetch_and(!bit, Relaxed);
            checked(g.trace(a::RETURN_BASE + index, id()));
            return;
        }
        {
            let g = coordinator::lock(coord());
            let r = record();
            if (*r).resumed != 1 || (*r).completed != 0 {
                stop(PANIC, 839);
            }
            checked(g.complete(id()));
            (*r).completed = 1;
            (*state()).drained |= bit;
            (*r).completed_guest.copy_live(&(*ordered()).current);
            (*r).completed_guest.rip = o::RIP + id() * 256 + 4;
            coord().retained.fetch_and(!bit, Relaxed);
            checked(g.trace(a::RETIRED_STORE, id()));
            // Even a completed owner cannot hand off while maintenance owns
            // the context. The existing event predicate is evaluated again.
            (*r).event.blocked |= c::event_blockers(
                coord().phase.load(Relaxed),
                false,
                true,
                (*r).completed_guest.rflags,
                0,
                0,
            );
        }
        loop {
            let g = coordinator::lock(coord());
            let s = state();
            if (*s).drained == all() && (*s).mutated == 0 && id() == (*s).writer {
                if coord().active.load(Relaxed) != 0
                    || coord().retained.load(Relaxed) != 0
                    || coord().head.load(Relaxed) != (*s).n
                    || coord().servicing.load(Relaxed) != c::NO_CPU
                {
                    stop(PANIC, 840);
                }
                cut(1);
                let pte = (PRIVATE + o::TABLE_OFFSET as u64 + 12288 + 257 * 8) as *mut u64;
                if ptr::read_volatile(pte) != 0x101067 {
                    stop(PANIC, 841);
                }
                ptr::write_volatile(pte, a::NEW_RESULTS | 0x67);
                asm!("dsb sy");
                (*s).mutated = 1;
                coord().generation.store(2, Relaxed);
                checked(g.trace(a::MUTATE, id()));
                cut(2);
                copy_record(
                    (*s).tables_after.as_mut_ptr(),
                    (PRIVATE + o::TABLE_OFFSET as u64) as *const u8,
                    16384,
                );
            }
            if (*s).mutated != 0 && (*s).acknowledged & bit == 0 {
                let eligible = schedule::except_last(all(), (*s).actor, (*s).acknowledged);
                if eligible & bit != 0
                    && schedule::permit(&g, sched::ACK, all(), eligible, c::EXCLUSIVE, 2)
                {
                    if id() == (*s).actor {
                        if (*s).acknowledged != all() ^ bit || g.admit(id()).is_ok() {
                            stop(PANIC, 842);
                        }
                        (*s).blocked = 1;
                        checked(g.trace(a::ENTRY_BLOCKED, id()));
                    }
                    ordering::mapping_gpa(o::RESULTS, o::RESULTS, false, true);
                    (*record()).ack_leaf = ptr::read_volatile(leaf());
                    new_route();
                    ordering::mapping_gpa(o::RESULTS, (*ordered()).routes[1], true, true);
                    (*record()).new_leaf = ptr::read_volatile(leaf());
                    (*s).acknowledged |= bit;
                    coord().acknowledgements.fetch_or(bit, Relaxed);
                    checked(g.trace(a::ACK, id()));
                }
            }
            if (*s).acknowledged == all() && (*s).opened == 0 && id() == (*s).writer {
                if (*s).blocked != 1 {
                    stop(PANIC, 843);
                }
                coord().queued.store(0, Relaxed);
                coord().head.store(0, Relaxed);
                coord().length.store(0, Relaxed);
                coord().completed.store(0, Relaxed);
                coord().servicing.store(c::NO_CPU, Relaxed);
                for q in &coord().queue {
                    q.store(c::NO_CPU, Relaxed);
                }
                coord().phase.store(c::OPEN, Relaxed);
                (*s).opened = 1;
                checked(g.trace(a::OPENED, id()));
            }
            if (*s).opened != 0 {
                deliver(&g);
                return;
            }
            drop(g);
            core::hint::spin_loop();
        }
    }
}

pub unsafe fn stopped() {
    let g = coordinator::lock(coord());
    coord().finished.fetch_or(1 << id(), Relaxed);
    checked(g.trace(90, id()));
}
