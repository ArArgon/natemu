//! Segment returns preserve the whole-program grant and retained route.
use super::*;
use natemu_abi::{recovery::GuestState, families::stack as p, families::stack_concurrent as sc};
fn state() -> *mut sc::State {
    (PRIVATE + sc::OFFSET as u64) as *mut sc::State
}
pub fn enabled() -> bool {
    coordinator().stack_profile != 0
}
pub unsafe fn validate(o: &Object, frame: &NativeFrame, pass: u64) -> Result<(), u64> {
    unsafe {
        let base = if pass == 0 { CODE_VA } else { CODE_NEXT };
        if o.version != natemu_abi::VERSION
            || o.profile != natemu_abi::PROFILE
            || o.size as usize != core::mem::size_of::<Object>()
            || o.reserved != 0
            || o.configured_vcpus as u64 != coordinator().participants
            || o.vcpu_id >= o.configured_vcpus
            || o.kind != if pass == 0 { LIVE_LOAD } else { LIVE_OR }
            || o.code_address != base
            || o.code_length != p::CODE_BYTES as u64
            || o.entry_offset != 0
            || pass >= sc::MAX_PROGRAMS as u64
            || frame.sp != STACK_TOP
            || frame.x[28] != BANK_VA
            || frame.reserved != 0
            || frame.x[8]
                != sc::initial_rsp_for(coordinator().stack_profile, o.vcpu_id as u64) - pass * 12
            || frame.x[5] != sc::initial_rsp_for(coordinator().stack_profile, o.vcpu_id as u64)
        {
            return Err(170);
        }
        if core::slice::from_raw_parts(base as *const u8, p::CODE_BYTES)
            != p::image(&p::body(u64::from(pass != 0)))
        {
            return Err(171);
        }
        Ok(())
    }
}
#[inline(never)]
pub unsafe fn materialize(frame: &NativeFrame, offset: u64) {
    unsafe {
        let g = natemu_abi::recovery::GUEST_VA as *mut GuestState;
        let mut guest = read_record(g);
        guest.gpr[4] = frame.x[8];
        guest.gpr[2] = frame.x[5];
        guest.gpr[0] = frame.x[4];
        guest.gpr[3] = frame.x[6];
        guest.rip = natemu_abi::families::alias::source_rip(coordinator().alias_profile) + offset;
        write_record(g, guest);
    }
}
#[inline(never)]
unsafe fn enter_step() -> ! {
    unsafe {
        let at = state();
        let step = (*at).step as usize;
        let pass = (*at).pass as usize;
        if step >= 5 || pass >= sc::MAX_PROGRAMS || (*at).programs[pass].entries & (1 << step) != 0
        {
            stop(PANIC, 172);
        }
        let s = shared();
        let mut frame = read_record(&raw const (*s).initial);
        let operand = sc::due(&frame, step);
        let width = p::WIDTHS[step];
        let id = (*s).object.vcpu_id as u64;
        let start = sc::initial_rsp_for(coordinator().stack_profile, id) - 0x80;
        if !natemu_abi::paging::canonical_span(operand, width)
            || operand < start
            || operand + width > start + 256
            || (step >= 3 && operand & (width - 1) != 0)
        {
            stop(REJECTED, 173);
        }
        frame.x[16] = (*at).code + step as u64 * p::SLOT_BYTES as u64;
        frame.x[30] = returned as *const () as u64;
        write_record(&raw mut (*s).initial, frame);
        (*s).capture_pc = frame.x[30];
        (*at).programs[pass].entries |= 1 << step;
        enter(&raw const (*s).initial)
    }
}
#[inline(never)]
pub unsafe fn start(pass: u64, code: u64) -> ! {
    unsafe {
        let at = state();
        if pass >= sc::MAX_PROGRAMS as u64 || (*at).programs[pass as usize].entries != 0 {
            stop(PANIC, 174);
        }
        (*at).step = 0;
        (*at).pass = pass;
        (*at).code = code;
        materialize(&*(&raw const (*shared()).initial), 0);
        enter_step()
    }
}
// Returns only at a whole-program boundary or a native memory fault. No grant
// changes occur at the inserted RET gates between guest instructions.
// Keep prefix-frame temporaries off the long-lived maintenance dispatcher chain.
#[inline(never)]
pub unsafe fn advance(fault: u64) {
    unsafe {
        let at = state();
        let step = (*at).step as usize;
        let pass = (*at).pass as usize;
        if step >= 5
            || pass >= sc::MAX_PROGRAMS
            || (*at).programs[pass].entries & (1 << step) == 0
            || (*at).programs[pass].completed & (1 << step) != 0
        {
            stop(PANIC, 175);
        }
        if fault != 0 {
            return;
        }
        let s = shared();
        let frame = read_record(&raw const (*s).captured);
        if frame.x[8]
            != if step < 3 {
                sc::due(&(*s).initial, step)
            } else {
                (*s).initial.x[8]
            }
            || frame.x[5] != (*s).initial.x[5]
        {
            stop(NATIVE_FAULT, 176);
        }
        write_record(
            &raw mut (*at).programs[pass].prefixes[step],
            [frame.x[8], frame.x[4], frame.x[6], frame.x[20]],
        );
        (*at).programs[pass].completed |= 1 << step;
        materialize(&frame, p::GUEST_OFFSETS[step + 1]);
        if step == 4 {
            return;
        }
        write_record(&raw mut (*s).initial, frame);
        (*at).step = step as u64 + 1;
        if pass == 0 && step == 0 {
            recovery::clear_mapping(frame.x[8]);
            let c = coordinator();
            let id = (*s).object.vcpu_id as u64;
            if id == roles().delayed {
                while c.queued.load(Ordering::Acquire).count_ones() as u64 != c.participants - 1 {
                    asm!("yield", options(nomem, nostack));
                }
                let g = coordinator::lock(c);
                checked(g.trace(7, id));
            }
        }
        enter_step()
    }
}
pub unsafe fn fault_valid(esr: u64, far: u64, pc: u64, spsr: u64, old: bool) -> bool {
    unsafe {
        let at = state();
        let s = shared();
        let step = (*at).step as usize;
        let frame = &(*s).captured;
        let initial = &(*s).initial;
        let guest = &*(natemu_abi::recovery::GUEST_VA as *const GuestState);
        step == if old { 1 } else { 0 }
            && esr >> 26 == 0x25
            && esr & (1 << 6) != 0
            && (if old {
                matches!(esr & 63, 4..=7)
            } else {
                matches!(esr & 63, 12..=15)
            })
            && far == sc::due(initial, step)
            && pc == (*at).code + step as u64 * p::SLOT_BYTES as u64 + p::SITES[step]
            && spsr & 15 == 4
            && frame.sp == STACK_TOP
            && frame.x[8] == initial.x[8]
            && frame.x[4] == initial.x[4]
            && frame.x[6] == initial.x[6]
            && guest.rip
                == natemu_abi::families::alias::source_rip(coordinator().alias_profile)
                    + p::GUEST_OFFSETS[step]
            && guest.gpr[4] == initial.x[8]
    }
}

unsafe fn tables() -> [u64; 4] {
    unsafe {
        let base = PRIVATE + natemu_abi::recovery::TABLE_OFFSET as u64;
        [0, 256 * 8, 0x3000 + 256 * 8, 0x3000 + 257 * 8]
            .map(|offset| ptr::read_volatile((base + offset) as *const u64))
    }
}

// Borrow/copy private records in place: the maintenance caller has a bounded,
// large frame that must not also retain temporary guest/native snapshots.
#[inline(never)]
pub unsafe fn probe_start() -> ! {
    unsafe {
        let c = coordinator();
        let s = shared();
        let id = (*s).object.vcpu_id as u64;
        let bit = 1 << id;
        let p = &raw mut (*state()).probe;
        let guest = natemu_abi::recovery::GUEST_VA as *mut GuestState;
        {
            let g = coordinator::lock(c);
            if c.stack_profile < 2
                || (*p).done != 0
                || !source::eligible(2, CODE_NEXT)
                || c.active.load(Ordering::Relaxed) & bit != 0
                || c.new_references.load(Ordering::Relaxed) & bit != 0
            {
                stop(PANIC, 180);
            }
            let address = (*s).captured.x[8] - 8;
            let route = recovery::walk(address, Access::Read).unwrap_or_else(|_| stop(PANIC, 181));
            if !route.dirty || !route.writable {
                stop(PANIC, 181);
            }
            aliases::install_pair(address, route);
            write_record(&raw mut (*p).pair, aliases::leaves());
            write_record(&raw mut (*p).tables_before, tables());
            c.active.fetch_or(bit, Ordering::Relaxed);
            c.new_references.fetch_or(bit, Ordering::Relaxed);
            checked(g.trace(32, id));
        }
        copy_record(
            (&raw mut (*p).guest_before).cast(),
            guest.cast(),
            size_of::<GuestState>(),
        );
        copy_frame(&raw mut (*s).initial, &raw const (*s).captured);
        (*s).initial.x[8] = aliases::other((*s).initial.x[8]);
        (*s).initial.x[16] = CODE_NEXT;
        (*s).initial.x[30] = returned as *const () as u64;
        copy_frame(&raw mut (*p).initial, &raw const (*s).initial);
        materialize(&*(&raw const (*s).initial), 0);
        (*round()).stage = 4;
        enter(&raw const (*s).initial)
    }
}

#[inline(never)]
pub unsafe fn probe_finish(fault: u64) -> ! {
    unsafe {
        let c = coordinator();
        let s = shared();
        let id = (*s).object.vcpu_id as u64;
        let bit = 1 << id;
        let p = &raw mut (*state()).probe;
        let (esr, far, pc, spsr): (u64, u64, u64, u64);
        asm!("mrs {0}, ESR_EL1","mrs {1}, FAR_EL1","mrs {2}, ELR_EL1","mrs {3}, SPSR_EL1",
            out(reg)esr,out(reg)far,out(reg)pc,out(reg)spsr);
        if fault == 0
            || c.stack_profile < 2
            || (*p).done != 0
            || esr >> 26 != 0x25
            || esr & (1 << 6) == 0
            || !matches!(esr & 63, 12..=15)
            || far != (*p).initial.x[8] - 8
            || pc != CODE_NEXT + 8
            || spsr & 15 != 4
            || (*s).captured.x[8] != (*p).initial.x[8]
        {
            stop(NATIVE_FAULT, 182);
        }
        let g = coordinator::lock(c);
        if c.active.load(Ordering::Relaxed) & bit == 0
            || c.new_references.load(Ordering::Relaxed) & bit == 0
        {
            stop(PANIC, 183);
        }
        if recovery::walk(far, Access::Write)
            != Err(natemu_abi::paging::Fault::Page {
                address: far,
                error: 3,
            })
        {
            stop(PANIC, 184);
        }
        let rip = natemu_abi::families::alias::source_rip(c.alias_profile);
        recovery::guest_fault(far, 3, rip);
        copy_frame(&raw mut (*p).captured, &raw const (*s).captured);
        copy_record(
            (&raw mut (*p).guest_after).cast(),
            natemu_abi::recovery::GUEST_VA as *const u8,
            size_of::<GuestState>(),
        );
        write_record(&raw mut (*p).tables_after, tables());
        (*p).vector = 14;
        (*p).error = 3;
        (*p).address = far;
        (*p).rip = rip;
        (*p).esr = esr;
        (*p).far = far;
        (*p).pc = pc;
        (*p).spsr = spsr;
        (*p).done = 1;
        c.active.fetch_and(!bit, Ordering::Relaxed);
        c.new_references.fetch_and(!bit, Ordering::Relaxed);
        checked(g.trace(33, id));
        c.finished.fetch_or(bit, Ordering::Release);
        drop(g);
        stop(SUCCESS, 0)
    }
}
