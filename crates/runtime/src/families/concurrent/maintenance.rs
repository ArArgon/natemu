//! Sustained protected writes and real ordinary intervals under one coordinator.
use super::*;
use natemu_abi::families::maintenance as m;

fn state() -> *mut m::State {
    m::VA as *mut m::State
}
fn interval(index: u64) -> *mut m::Interval {
    (m::RECORD_VA + index * 4096) as *mut m::Interval
}
fn leaf() -> *mut u64 {
    (PRIVATE + natemu_abi::recovery::TABLE_OFFSET as u64 + 0x3000 + 256 * 8) as *mut u64
}
unsafe fn route_gpa(address: u64, access: Access) -> u64 {
    unsafe {
        if mixed::enabled() {
            let route = mixed::resolve(address, 4, access);
            // Read admission deliberately leaves a native RO mapping in both modes.
            recovery::install_ram(
                address,
                route.gpa(),
                access == Access::Write && route.writable(),
            );
            route.gpa()
        } else {
            let route = recovery::walk(address, access).unwrap_or_else(|_| stop(PANIC, 137));
            if access == Access::Read {
                aliases::install_pair(address, route);
            } else {
                recovery::install(address, route);
            }
            route.gpa
        }
    }
}
unsafe fn expected_gpa(address: u64) -> u64 {
    unsafe {
        if mixed::enabled() {
            mixed::expected_gpa(address)
        } else {
            (ptr::read_volatile(leaf()) & natemu_abi::paging::PA_MASK) + (address & 4095)
        }
    }
}

pub unsafe fn start(id: u64) -> ! {
    unsafe {
        prepare(id);
        drive(id)
    }
}

// Drop baseline/state temporaries before the long-lived dispatcher frame.
#[inline(never)]
unsafe fn prepare(id: u64) {
    unsafe {
        write_record(
            m::BASELINE_VA as *mut m::Baseline,
            m::Baseline {
                frame: read_record(&raw const (*shared()).captured),
                old_memory: read_record(
                    (PRIVATE + DATA_OFFSET as u64 + id * 256) as *const [u8; 256],
                ),
                new_memory: read_record(
                    (PRIVATE + DATA_OFFSET as u64 + 4096 + id * 256) as *const [u8; 256],
                ),
            },
        );
        ptr::write_volatile(&raw mut (*round()).stage, 3);
        {
            let c = coordinator();
            let _g = coordinator::lock(c);
            let mut s = read_record(state());
            s.ready |= 1 << id;
            c.new_references.fetch_or(1 << id, Ordering::Relaxed);
            write_record(state(), s);
        }
    }
}

unsafe fn drive(id: u64) -> ! {
    unsafe {
        let c = coordinator();
        let all = (1 << c.participants) - 1;
        loop {
            let mut run = None;
            let mut done = false;
            {
                let g = coordinator::lock(c);
                let mut s = read_record(state());
                if s.ready == all {
                    if s.compose_leaf == 0 {
                        s.compose_leaf = ptr::read_volatile(leaf());
                    }
                    if s.finished != 0 {
                        c.new_references.fetch_and(!(1 << id), Ordering::Relaxed);
                        done = true;
                    } else {
                        if s.pending[id as usize] == 0 && s.ordinary != id {
                            let admitted = crate::families::schedule::enqueue(&g, &mut s, id as usize);
                            let admitted = if crate::families::schedule::enabled() && admitted == Err(m::FULL)
                            {
                                None
                            } else {
                                checked(admitted)
                            };
                            if admitted.is_some() {
                                checked(g.trace(23, id));
                                let saved = s;
                                if s.enqueue(id as usize) != Err(m::DUPLICATE) || s != saved {
                                    stop(PANIC, 133);
                                }
                                s.duplicate_rejections += 1;
                                checked(g.trace(24, id));
                            }
                        }
                        if crate::families::schedule::pool_ready(&g, &s)
                            && s.current.serial == 0
                            && s.ordinary == NO_CPU
                        {
                            if s.ordinary_due() {
                                let owner = checked(s.begin_ordinary());
                                if c.active.load(Ordering::Relaxed) != 0 {
                                    stop(PANIC, 134);
                                }
                                c.phase.store(OPEN, Ordering::Relaxed);
                                if !source::eligible(2, CODE_NEXT)
                                    || c.new_references.load(Ordering::Relaxed) & (1 << owner) == 0
                                {
                                    stop(PANIC, 146);
                                }
                                c.active.store(1 << owner, Ordering::Relaxed);
                                checked(g.trace(29, owner));
                            } else if let Some(r) = checked(s.start_head()) {
                                if c.active.load(Ordering::Relaxed) != 0 {
                                    stop(PANIC, 135);
                                }
                                c.phase.store(EXCLUSIVE, Ordering::Relaxed);
                                let old = ptr::read_volatile(leaf()) & natemu_abi::paging::PA_MASK;
                                let new = if old == DATA_VA {
                                    DATA_VA + 4096
                                } else {
                                    DATA_VA
                                };
                                aliases::set_leaf(new | 7);
                                asm!("dsb ish");
                                checked(g.trace(25, r.cpu));
                            }
                        }
                        if s.current.serial != 0
                            && s.acknowledgements & (1 << id) == 0
                            && (id != s.epoch % s.n || s.acknowledgements == all ^ (1 << id))
                            && crate::families::schedule::permit(
                                &g,
                                natemu_abi::families::schedule::MAINTENANCE_ACK | s.epoch,
                                all,
                                crate::families::schedule::except_last(
                                    all,
                                    s.epoch % s.n,
                                    s.acknowledgements,
                                ),
                                c.phase.load(Ordering::Relaxed),
                                s.epoch,
                            )
                        {
                            if c.active.load(Ordering::Relaxed) != 0 {
                                stop(PANIC, 136);
                            }
                            let address = DATA_VA + id * 256;
                            let gpa = route_gpa(address, Access::Read);
                            if gpa != expected_gpa(address) {
                                stop(PANIC, 138);
                            }
                            if c.alias_profile != 0 {
                                aliases::acknowledge(s.epoch);
                            }
                            asm!("dsb ish", "isb");
                            checked(s.acknowledge(id as usize));
                            checked(g.trace(26, id));
                            if s.acknowledgements == all {
                                s.leaf_history[s.total as usize] = ptr::read_volatile(leaf());
                                let r = checked(s.complete_head());
                                checked(g.trace(27, r.cpu));
                                c.phase.store(CLOSING, Ordering::Relaxed);
                            }
                        }
                        if s.ordinary != NO_CPU && s.closing == 0 && (id != s.ordinary || s.n == 1)
                        {
                            c.phase.store(CLOSING, Ordering::Relaxed);
                            s.closing = 1;
                            checked(g.trace(28, id));
                        }
                        if s.ordinary == id && s.closing != 0 {
                            if s.pending[id as usize] != 0
                                || c.active.load(Ordering::Relaxed) != 1 << id
                            {
                                stop(PANIC, 139);
                            }
                            let at = interval(s.intervals[id as usize]);
                            let mut r = m::Interval::ZERO;
                            r.ordinal = s.ordinary_total + 1;
                            r.heads = s.total;
                            r.epoch = s.epoch;
                            r.gpa = expected_gpa(DATA_VA + id * 256);
                            let address = PRIVATE + DATA_OFFSET as u64 + r.gpa - DATA_VA;
                            // Declared fixture input makes a skipped/idempotent
                            // OR body observable without inventing register state.
                            if !stack::enabled() {
                                ptr::write_volatile(
                                    address as *mut u64,
                                    0x4a00_0000_0000_0000 | s.total << 16 | id,
                                );
                                for b in 128..144 {
                                    ptr::write_volatile((address + b) as *mut u8, 0xa5);
                                }
                            }
                            r.before = read_record(address as *const [u8; 256]);
                            r.initial = read_record(&raw const (*shared()).captured);
                            r.initial.x[16] = CODE_NEXT;
                            r.initial.sp = STACK_TOP;
                            if c.alias_profile != 0 && !stack::enabled() {
                                r.initial.x[6] = natemu_abi::families::alias::data_address(
                                    c.alias_profile,
                                    id,
                                    s.intervals[id as usize] + 2,
                                );
                                r.initial.x[11] = r.initial.x[6] + 128;
                            }
                            checked(aliases::validate(
                                &ptr::read_volatile(&raw const (*shared()).object),
                                &r.initial,
                                core::slice::from_raw_parts(CODE_NEXT as *const u8, 48),
                                s.intervals[id as usize] + 2,
                            ));
                            write_record(at, r);
                            run = Some((r.initial, s.intervals[id as usize] + 2));
                        }
                    }
                    write_record(state(), s);
                }
            }
            if done {
                if stack_boundary::enabled() {
                    stack_boundary::start();
                }
                if c.stack_profile >= 2 {
                    stack::probe_start();
                }
                c.finished.fetch_or(1 << id, Ordering::Release);
                stop(SUCCESS, 0);
            }
            if let Some((frame, pass)) = run {
                write_record(&raw mut (*shared()).initial, frame);
                if stack::enabled() {
                    stack::start(pass, CODE_NEXT);
                }
                enter(&raw const (*shared()).initial)
            }
            asm!("yield", options(nomem, nostack));
        }
    }
}

pub unsafe fn finish(fault: u64) -> ! {
    unsafe {
        let c = coordinator();
        let owner = shared();
        let id = ptr::read_volatile(&raw const (*owner).object.vcpu_id) as u64;
        let (at, snapshot) = {
            let _g = coordinator::lock(c);
            let s = read_record(state());
            if s.ordinary != id || c.active.load(Ordering::Relaxed) != 1 << id {
                stop(PANIC, 140);
            }
            (interval(s.intervals[id as usize]), s)
        };
        if fault != 0 {
            let (esr, far, pc, spsr): (u64, u64, u64, u64);
            asm!("mrs {0}, ESR_EL1","mrs {1}, FAR_EL1","mrs {2}, ELR_EL1","mrs {3}, SPSR_EL1",
                out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
            let valid = if mixed::enabled() {
                mixed::fault_valid(esr, far, pc, spsr, false)
            } else if stack::enabled() {
                stack::fault_valid(esr, far, pc, spsr, false)
            } else {
                !(esr >> 26 != 0x25
                    || esr & (1 << 6) == 0
                    || !matches!(esr & 63, 12..=15)
                    || pc != CODE_NEXT + 36
                    || far != (*at).initial.x[11]
                    || spsr & 15 != 4)
            };
            if !valid || ptr::read_volatile(&raw const (*at).faults) != 0 {
                stop(NATIVE_FAULT, 141);
            }
            if mixed::enabled() {
                mixed::materialize(&(*owner).captured, 2);
            }
            write_record(
                &raw mut (*at).dirty_frame,
                read_record(&raw const (*owner).captured),
            );
            ptr::write_volatile(&raw mut (*at).esr, esr);
            ptr::write_volatile(&raw mut (*at).far, far);
            ptr::write_volatile(&raw mut (*at).pc, pc);
            ptr::write_volatile(&raw mut (*at).spsr, spsr);
            {
                let g = coordinator::lock(c);
                let s = read_record(state());
                if s.epoch != snapshot.epoch
                    || s.current.serial != 0
                    || c.phase.load(Ordering::Relaxed) != CLOSING
                {
                    stop(PANIC, 142);
                }
                let gpa = route_gpa(far, Access::Write);
                if gpa
                    != ptr::read_volatile(&raw const (*at).gpa)
                        + if mixed::enabled() {
                            0
                        } else if stack::enabled() {
                            far & 255
                        } else {
                            128
                        }
                {
                    stop(PANIC, 144);
                }
                checked(g.trace(31, id));
            }
            ptr::write_volatile(&raw mut (*at).faults, 1);
            resume_exception(&raw const (*owner).captured, pc, spsr);
        }
        write_record(
            &raw mut (*at).captured,
            read_record(&raw const (*owner).captured),
        );
        if mixed::enabled() {
            mixed::materialize(&(*owner).captured, 6);
        }
        let address =
            PRIVATE + DATA_OFFSET as u64 + ptr::read_volatile(&raw const (*at).gpa) - DATA_VA;
        write_record(
            &raw mut (*at).after,
            read_record(address as *const [u8; 256]),
        );
        asm!("dsb ish");
        {
            let g = coordinator::lock(c);
            let mut s = read_record(state());
            if s.epoch != snapshot.epoch {
                stop(PANIC, 145);
            }
            checked(s.finish_ordinary(id));
            c.active.store(0, Ordering::Relaxed);
            c.phase.store(
                if s.finished != 0 { OPEN } else { CLOSING },
                Ordering::Relaxed,
            );
            checked(g.trace(30, id));
            write_record(state(), s);
        }
        drive(id)
    }
}
