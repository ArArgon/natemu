//! Sustained protected writes and real ordinary intervals under one coordinator.
use super::*;
use natemu_abi::maintenance as m;

fn state() -> *mut m::State {
    m::VA as *mut m::State
}
fn interval(index: u64) -> *mut m::Interval {
    (m::RECORD_VA + index * 4096) as *mut m::Interval
}
fn leaf() -> *mut u64 {
    (PRIVATE + natemu_abi::recovery::TABLE_OFFSET as u64 + 0x3000 + 256 * 8) as *mut u64
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
                            if checked(s.enqueue(id as usize)).is_some() {
                                checked(g.trace(23, id));
                                let saved = s;
                                if s.enqueue(id as usize) != Err(m::DUPLICATE) || s != saved {
                                    stop(PANIC, 133);
                                }
                                s.duplicate_rejections += 1;
                                checked(g.trace(24, id));
                            }
                        }
                        if s.maximum_depth == s.n && s.current.serial == 0 && s.ordinary == NO_CPU {
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
                                ptr::write_volatile(leaf(), new | 7);
                                asm!("dsb ish");
                                checked(g.trace(25, r.cpu));
                            }
                        }
                        if s.current.serial != 0
                            && s.acknowledgements & (1 << id) == 0
                            && (id != s.epoch % s.n || s.acknowledgements == all ^ (1 << id))
                        {
                            if c.active.load(Ordering::Relaxed) != 0 {
                                stop(PANIC, 136);
                            }
                            let address = DATA_VA + id * 256;
                            let route = recovery::walk(address, Access::Read)
                                .unwrap_or_else(|_| stop(PANIC, 137));
                            if route.gpa
                                != (ptr::read_volatile(leaf()) & natemu_abi::paging::PA_MASK)
                                    + id * 256
                            {
                                stop(PANIC, 138);
                            }
                            aliases::install_pair(address, route);
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
                            r.gpa = (ptr::read_volatile(leaf()) & natemu_abi::paging::PA_MASK)
                                + id * 256;
                            let address = PRIVATE + DATA_OFFSET as u64 + r.gpa - DATA_VA;
                            // Declared fixture input makes a skipped/idempotent
                            // OR body observable without inventing register state.
                            ptr::write_volatile(
                                address as *mut u64,
                                0x4a00_0000_0000_0000 | s.total << 16 | id,
                            );
                            for b in 128..144 {
                                ptr::write_volatile((address + b) as *mut u8, 0xa5);
                            }
                            r.before = read_record(address as *const [u8; 256]);
                            r.initial = read_record(&raw const (*shared()).captured);
                            r.initial.x[16] = CODE_NEXT;
                            r.initial.sp = STACK_TOP;
                            if c.alias_profile != 0 {
                                r.initial.x[6] = natemu_abi::alias::data_address(
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
                            run = Some(r.initial);
                        }
                    }
                    write_record(state(), s);
                }
            }
            if done {
                c.finished.fetch_or(1 << id, Ordering::Release);
                stop(SUCCESS, 0);
            }
            if let Some(frame) = run {
                write_record(&raw mut (*shared()).initial, frame);
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
            if esr >> 26 != 0x25
                || esr & (1 << 6) == 0
                || !matches!(esr & 63, 12..=15)
                || pc != CODE_NEXT + 36
                || far != (*at).initial.x[11]
                || spsr & 15 != 4
                || ptr::read_volatile(&raw const (*at).faults) != 0
            {
                stop(NATIVE_FAULT, 141);
            }
            write_record(
                &raw mut (*at).dirty_frame,
                read_record(&raw const (*owner).captured),
            );
            ptr::write_volatile(&raw mut (*at).esr, esr);
            ptr::write_volatile(&raw mut (*at).far, far);
            ptr::write_volatile(&raw mut (*at).pc, pc);
            ptr::write_volatile(&raw mut (*at).spsr, spsr);
            let route = {
                let g = coordinator::lock(c);
                let s = read_record(state());
                if s.epoch != snapshot.epoch
                    || s.current.serial != 0
                    || c.phase.load(Ordering::Relaxed) != CLOSING
                {
                    stop(PANIC, 142);
                }
                let route = recovery::walk(far, Access::Write).unwrap_or_else(|_| stop(PANIC, 143));
                if route.gpa != ptr::read_volatile(&raw const (*at).gpa) + 128 {
                    stop(PANIC, 144);
                }
                checked(g.trace(31, id));
                route
            };
            recovery::install(far, route);
            ptr::write_volatile(&raw mut (*at).faults, 1);
            resume_exception(&raw const (*owner).captured, pc, spsr);
        }
        write_record(
            &raw mut (*at).captured,
            read_record(&raw const (*owner).captured),
        );
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
