use super::*;
use core::sync::atomic::Ordering;
use natemu_abi::concurrency::*;
use natemu_abi::paging::Access;
mod source;

unsafe extern "C" {
    fn resume_exception(frame: *const NativeFrame, pc: u64, spsr: u64) -> !;
}
fn round() -> *mut Round {
    ROUND_VA as *mut Round
}
fn checked<T>(result: Result<T, u64>) -> T {
    result.unwrap_or_else(|reason| stop(PANIC, reason))
}

fn coordinator() -> &'static Coordinator {
    // Host initializes before any owner enters; only atomic fields are mutable.
    unsafe { &*(COORD_VA as *const Coordinator) }
}

pub unsafe fn initialize() -> ! {
    unsafe {
        let s = shared();
        let o = ptr::read_volatile(&raw const (*s).object);
        let c = coordinator();
        if c.participants == 0
            || c.participants > MAX_CPUS as u64
            || o.configured_vcpus as u64 != c.participants
            || o.vcpu_id >= o.configured_vcpus
        {
            stop(REJECTED, 80);
        }
        let mut frame = read_record(&raw const (*s).initial);
        if c.scenario == 3 {
            if o.vcpu_id == 0 {
                source::initialize();
            }
            while c.published.load(Ordering::Acquire) == 0 {
                asm!("yield", options(nomem, nostack));
            }
        }
        if let Err(reason) = validate_object(
            &o,
            &frame,
            core::slice::from_raw_parts(CODE_VA as *const u8, 48),
        ) {
            stop(REJECTED, reason);
        }
        frame.x[16] = CODE_VA;
        frame.x[30] = returned as *const () as u64;
        write_record(&raw mut (*s).initial, frame);
        ptr::write_volatile(&raw mut (*s).capture_pc, frame.x[30]);
        if c.scenario >= 1 {
            recovery::initialize();
            let guard = coordinator::lock(c);
            let route =
                recovery::walk(frame.x[6], Access::Read).unwrap_or_else(|_| stop(PANIC, 94));
            ptr::write_volatile(&raw mut (*round()).old_gpa, route.gpa);
            checked(guard.admit(o.vcpu_id as u64));
        }
        c.ready.fetch_or(1 << o.vcpu_id, Ordering::AcqRel);
        while c.ready.load(Ordering::Acquire) != (1 << c.participants) - 1 {
            asm!("yield", options(nomem, nostack));
        }
        if c.scenario >= 1 && o.vcpu_id as u64 == c.participants - 1 {
            // Force a drain to wait for a still-admitted peer, rather than
            // relying on host timing to happen to expose the interleaving.
            while c.queued.load(Ordering::Acquire).count_ones() as u64 != c.participants - 1 {
                asm!("yield", options(nomem, nostack));
            }
            let g = coordinator::lock(c);
            checked(g.trace(7, o.vcpu_id as u64));
        }
        asm!("dsb ish", "ic iallu", "dsb ish", "isb");
        enter(&raw const (*s).initial)
    }
}

pub unsafe fn completed(fault: u64) -> ! {
    unsafe {
        if coordinator().scenario >= 1 {
            queued(fault);
        }
        if fault != 0 {
            stop(NATIVE_FAULT, 81);
        }
        let id = ptr::read_volatile(&raw const (*shared()).object.vcpu_id);
        asm!("dsb ish");
        coordinator().finished.fetch_or(1 << id, Ordering::Release);
        stop(SUCCESS, 0)
    }
}

unsafe fn queued(fault: u64) -> ! {
    unsafe {
        let c = coordinator();
        let s = shared();
        let id = ptr::read_volatile(&raw const (*s).object.vcpu_id) as u64;
        if ptr::read_volatile(&raw const (*round()).stage) == 2 {
            new_completed(fault);
        }
        if fault != 0 {
            let (esr, far, pc, spsr): (u64, u64, u64, u64);
            asm!("mrs {0}, ESR_EL1", "mrs {1}, FAR_EL1", "mrs {2}, ELR_EL1", "mrs {3}, SPSR_EL1",
                out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
            let frame = read_record(&raw const (*s).captured);
            if esr >> 26 != 0x25
                || esr & (1 << 6) != 0
                || !matches!(esr & 63, 4..=7)
                || pc != CODE_VA + 16
                || far != DATA_VA + id * 256
                || frame.sp != STACK_TOP
                || spsr & 15 != 4
                || ptr::read_volatile(&raw const (*round()).faults) != 0
            {
                stop(NATIVE_FAULT, 95);
            }
            ptr::write_volatile(&raw mut (*round()).faults, 1);
            write_record(&raw mut (*round()).fault_frame, frame);
            ptr::write_volatile(&raw mut (*round()).fault_esr, esr);
            ptr::write_volatile(&raw mut (*round()).fault_far, far);
            ptr::write_volatile(&raw mut (*round()).fault_pc, pc);
            ptr::write_volatile(&raw mut (*round()).fault_spsr, spsr);
            asm!("dsb ish");
            {
                let g = coordinator::lock(c);
                checked(g.detach_fault(id));
            }
            loop {
                let selected = {
                    let g = coordinator::lock(c);
                    checked(g.select()) == id
                };
                if selected {
                    let route = {
                        let _g = coordinator::lock(c);
                        recovery::walk(frame.x[11], Access::Write)
                            .unwrap_or_else(|_| stop(PANIC, 96))
                    };
                    if route.gpa != ptr::read_volatile(&raw const (*round()).old_gpa) + 128 {
                        stop(PANIC, 97);
                    }
                    // Exclusive service finishes this admitted old route. No
                    // peer may mutate it until the returned body acknowledges.
                    recovery::install(far, route);
                    resume_exception(&raw const (*s).captured, pc, spsr);
                }
                asm!("yield", options(nomem, nostack));
            }
        }
        write_record(
            &raw mut (*round()).first_frame,
            read_record(&raw const (*s).captured),
        );
        ptr::write_volatile(&raw mut (*round()).stage, 1);
        asm!("dsb ish");
        {
            let g = coordinator::lock(c);
            checked(g.complete(id));
        }
        if c.scenario >= 2 {
            remap_wait(id);
        }
        loop {
            let complete = {
                let g = coordinator::lock(c);
                let all = c.completed.load(Ordering::Relaxed) == (1 << c.participants) - 1;
                if all {
                    c.retained.fetch_and(!(1 << id), Ordering::Relaxed);
                    checked(g.trace(6, id));
                }
                all
            };
            if complete {
                break;
            }
            asm!("yield", options(nomem, nostack));
        }
        c.finished.fetch_or(1 << id, Ordering::Release);
        stop(SUCCESS, 0)
    }
}

// Every participant is detached here. Only one lock holder commits mutation;
// each executor updates its *own* root and acknowledges after local TLBI/DSB.
unsafe fn remap_wait(id: u64) -> ! {
    unsafe {
        let c = coordinator();
        let all = (1 << c.participants) - 1;
        let bit = 1 << id;
        loop {
            let open = {
                let g = coordinator::lock(c);
                if c.phase.load(Ordering::Relaxed) == EXCLUSIVE
                    && c.completed.load(Ordering::Relaxed) == all
                    && (c.scenario != 3 || id == 0)
                {
                    ptr::write_volatile(
                        (PRIVATE + natemu_abi::recovery::TABLE_OFFSET as u64 + 0x3000 + 256 * 8)
                            as *mut u64,
                        (DATA_VA + 4096) | 7,
                    );
                    asm!("dsb ish");
                    c.generation.store(2, Ordering::Relaxed);
                    if c.scenario == 3 {
                        source::invalidate();
                        c.phase.store(COMPILING, Ordering::Relaxed);
                    } else {
                        c.phase.store(RELEASING, Ordering::Relaxed);
                    }
                    checked(g.trace(8, id));
                }
                if c.phase.load(Ordering::Relaxed) == RELEASING
                    && c.acknowledgements.load(Ordering::Relaxed) & bit == 0
                    && (id != c.participants - 1
                        || c.acknowledgements.load(Ordering::Relaxed) == all ^ bit)
                {
                    let address = DATA_VA + id * 256;
                    let route =
                        recovery::walk(address, Access::Read).unwrap_or_else(|_| stop(PANIC, 98));
                    if route.gpa != address + 4096 {
                        stop(PANIC, 99);
                    }
                    recovery::install(address, route);
                    asm!("dsb ish", "ic iallu", "dsb ish", "isb");
                    c.acknowledgements.fetch_or(bit, Ordering::Relaxed);
                    checked(g.trace(9, id));
                    if c.acknowledgements.load(Ordering::Relaxed) == all {
                        c.phase.store(OPEN, Ordering::Relaxed);
                        checked(g.trace(10, id));
                    }
                }
                c.phase.load(Ordering::Relaxed) == OPEN
            };
            if c.scenario == 3 && id == 0 && c.phase.load(Ordering::Acquire) == COMPILING {
                source::replacement();
            }
            if open {
                break;
            }
            asm!("yield", options(nomem, nostack));
        }
        // Force old retained references to overlap a new ordinary entry.
        if id == c.participants - 1 && c.participants > 1 {
            while c.new_entries.load(Ordering::Acquire) == 0 {
                asm!("yield", options(nomem, nostack));
            }
        }
        {
            let g = coordinator::lock(c);
            if c.phase.load(Ordering::Relaxed) != OPEN
                || c.generation.load(Ordering::Relaxed) != 2
                || c.acknowledgements.load(Ordering::Relaxed) != all
            {
                stop(PANIC, 100);
            }
            if c.scenario == 3 {
                if source::eligible(1, CODE_VA) || !source::eligible(2, CODE_NEXT) {
                    stop(PANIC, 116);
                }
                c.stale_entries.fetch_or(bit, Ordering::Relaxed);
                // Attempting to reuse the retired slot while our own frame
                // still retains it must fail before any native byte is changed.
                if source::try_reuse_old(&g) {
                    stop(PANIC, 117);
                }
                c.reuse_blocked.fetch_or(bit, Ordering::Relaxed);
                checked(g.trace(17, id));
            }
            c.retained.fetch_and(!bit, Ordering::Relaxed);
            checked(g.trace(6, id));
            if c.retained.load(Ordering::Relaxed) == 0 {
                if c.scenario == 3 {
                    if !source::try_reuse_old(&g) {
                        stop(PANIC, 119);
                    }
                }
                c.reclaimed.store(1, Ordering::Relaxed);
                checked(g.trace(14, id));
            }
            c.active.fetch_or(bit, Ordering::Relaxed);
            c.new_references.fetch_or(bit, Ordering::Relaxed);
            c.new_entries.fetch_or(bit, Ordering::Release);
            checked(g.trace(11, id));
        }
        let s = shared();
        let mut frame = read_record(&raw const (*s).captured);
        frame.x[16] = if c.scenario == 3 { CODE_NEXT } else { CODE_VA };
        frame.x[30] = returned as *const () as u64;
        frame.sp = STACK_TOP;
        let mut object = ptr::read_volatile(&raw const (*s).object);
        object.kind = if c.scenario == 3 { LIVE_OR } else { LIVE_LOAD };
        object.code_address = frame.x[16];
        checked(validate_object(
            &object,
            &frame,
            core::slice::from_raw_parts(frame.x[16] as *const u8, 48),
        ));
        ptr::write_volatile(&raw mut (*s).object, object);
        write_record(&raw mut (*round()).new_initial, frame);
        write_record(&raw mut (*s).initial, frame);
        ptr::write_volatile(&raw mut (*round()).stage, 2);
        enter(&raw const (*s).initial)
    }
}

unsafe fn new_completed(fault: u64) -> ! {
    unsafe {
        let c = coordinator();
        let s = shared();
        let id = ptr::read_volatile(&raw const (*s).object.vcpu_id) as u64;
        if fault != 0 {
            let (esr, far, pc, spsr): (u64, u64, u64, u64);
            asm!("mrs {0}, ESR_EL1", "mrs {1}, FAR_EL1", "mrs {2}, ELR_EL1", "mrs {3}, SPSR_EL1",
                out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
            if esr >> 26 != 0x25
                || esr & (1 << 6) == 0
                || !matches!(esr & 63, 12..=15)
                || pc != (if c.scenario == 3 { CODE_NEXT } else { CODE_VA }) + 36
                || far != DATA_VA + id * 256 + 128
                || spsr & 15 != 4
                || ptr::read_volatile(&raw const (*round()).dirty_faults) != 0
            {
                stop(NATIVE_FAULT, 101);
            }
            write_record(
                &raw mut (*round()).dirty_frame,
                read_record(&raw const (*s).captured),
            );
            ptr::write_volatile(&raw mut (*round()).dirty_esr, esr);
            ptr::write_volatile(&raw mut (*round()).dirty_far, far);
            ptr::write_volatile(&raw mut (*round()).dirty_pc, pc);
            ptr::write_volatile(&raw mut (*round()).dirty_spsr, spsr);
            let route = {
                let g = coordinator::lock(c);
                if c.phase.load(Ordering::Relaxed) != OPEN
                    || c.active.load(Ordering::Relaxed) & (1 << id) == 0
                {
                    stop(PANIC, 102);
                }
                let t = recovery::walk(far, Access::Write).unwrap_or_else(|_| stop(PANIC, 103));
                checked(g.trace(15, id));
                t
            };
            if route.gpa != far + 4096 {
                stop(PANIC, 104);
            }
            recovery::install(far, route);
            ptr::write_volatile(&raw mut (*round()).dirty_faults, 1);
            resume_exception(&raw const (*s).captured, pc, spsr);
        }
        asm!("dsb ish");
        {
            let g = coordinator::lock(c);
            if c.active.load(Ordering::Relaxed) & (1 << id) == 0 {
                stop(PANIC, 105);
            }
            c.active.fetch_and(!(1 << id), Ordering::Relaxed);
            c.new_references.fetch_and(!(1 << id), Ordering::Relaxed);
            c.new_completed.fetch_or(1 << id, Ordering::Relaxed);
            checked(g.trace(12, id));
        }
        c.finished.fetch_or(1 << id, Ordering::Release);
        stop(SUCCESS, 0)
    }
}
