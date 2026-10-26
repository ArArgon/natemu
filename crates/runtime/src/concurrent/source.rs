//! Compile/publication service for the finite concurrent source-mutation case.
use super::*;
use natemu_abi::translation as wire;

unsafe extern "C" {
    fn service(packet: *mut wire::Packet);
    fn service_resume();
}

unsafe fn publish_slot(address: u64, body: &[u8]) {
    unsafe {
        if !matches!(address, CODE_VA | CODE_NEXT) || body.len() != 44 {
            stop(PANIC, 110);
        }
        let offset = address - PRIVATE;
        let pte = (PRIVATE + 0x14000 + (offset / 4096) * 8) as *mut u64;
        let attrs = 3 | 1 << 10 | 3 << 8;
        let replace = |entry| {
            ptr::write_volatile(pte, 0);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
            ptr::write_volatile(pte, entry);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        };
        replace((IPA + offset) | attrs | (3 << 53));
        ptr::copy_nonoverlapping(body.as_ptr(), address as *mut u8, 44);
        ptr::copy_nonoverlapping(RET.as_ptr(), (address + 44) as *mut u8, 4);
        for off in (0..48).step_by(4) {
            asm!("dc cvau, {0}",in(reg)address+off);
        }
        asm!("dsb ish");
        for off in (0..48).step_by(4) {
            asm!("ic ivau, {0}",in(reg)address+off);
        }
        asm!("dsb ish", "isb");
        replace((IPA + offset) | attrs | (1 << 7));
    }
}

pub unsafe fn initialize() {
    unsafe {
        let s = shared();
        ptr::write_volatile(&raw mut (*s).service_pc, service_resume as *const () as u64);
        let mut state = ptr::read_volatile(&raw const (*s).lifecycle);
        if state.vm == 0 {
            stop(PANIC, 111);
        }
        if ptr::read_volatile(
            (PRIVATE + natemu_abi::recovery::SOURCE_OFFSET as u64) as *const [u8; 17],
        ) != state.source()
        {
            stop(PANIC, 118);
        }
        checked(state.dispatch());
        ptr::write_volatile(&raw mut (*s).lifecycle, state);
        let mut p = state.packet();
        service(&mut p);
        let bytes = checked(state.validate_reply(&p));
        publish_slot(CODE_VA, &bytes);
        state.pending = 0;
        state.cached = 0;
        // Start a real second compile now; its reply is delivered only after
        // the source changes and a replacement request owns the pending slot.
        checked(state.dispatch());
        ptr::write_volatile(&raw mut (*s).lifecycle, state);
        let mut deferred = state.packet();
        deferred[0] = DEFER_COMPILE;
        service(&mut deferred);
        if deferred[0] != wire::BUSY {
            stop(PANIC, 112);
        }
        ptr::write_volatile(&raw mut (*s).lifecycle, state);
        coordinator().published.store(1, Ordering::Release);
    }
}

// Called with coordinator authority, before changing source bytes.
pub unsafe fn invalidate() {
    unsafe {
        let c = coordinator();
        c.old_eligible.store(0, Ordering::Relaxed);
        let mut state = ptr::read_volatile(&raw const (*shared()).lifecycle);
        checked(state.change_source(wire::OR));
        ptr::copy_nonoverlapping(
            wire::OR.as_ptr(),
            (PRIVATE + natemu_abi::recovery::SOURCE_OFFSET as u64) as *mut u8,
            17,
        );
        checked(state.dispatch());
        ptr::write_volatile(&raw mut (*shared()).lifecycle, state);
        asm!("dsb ish");
    }
}

pub unsafe fn replacement() {
    unsafe {
        let c = coordinator();
        let s = shared();
        let mut state = ptr::read_volatile(&raw const (*s).lifecycle);
        let mut late = state.packet();
        late[0] = TAKE_COMPILE;
        service(&mut late);
        if state.validate_reply(&late) != Err(wire::STALE) {
            stop(PANIC, 113);
        }
        state.rejections += 1;
        let mut current = state.packet();
        service(&mut current);
        let body = checked(state.validate_reply(&current));
        let g = coordinator::lock(c);
        if c.phase.load(Ordering::Relaxed) != COMPILING
            || c.generation.load(Ordering::Relaxed) != 2
            || c.old_eligible.load(Ordering::Relaxed) != 0
        {
            stop(PANIC, 114);
        }
        // Source/context are stable under the same authority that publishes.
        if ptr::read_volatile(
            (PRIVATE + natemu_abi::recovery::SOURCE_OFFSET as u64) as *const [u8; 17],
        ) != state.source()
        {
            stop(PANIC, 115);
        }
        publish_slot(CODE_NEXT, &body);
        state.pending = 0;
        state.cached = 1;
        ptr::write_volatile(&raw mut (*s).lifecycle, state);
        c.stale_replies.store(1, Ordering::Relaxed);
        c.published.store(2, Ordering::Relaxed);
        checked(g.trace(16, 0));
        c.phase.store(RELEASING, Ordering::Relaxed);
    }
}

pub fn eligible(generation: u64, target: u64) -> bool {
    let c = coordinator();
    c.phase.load(Ordering::Relaxed) == OPEN
        && c.generation.load(Ordering::Relaxed) == generation
        && c.published.load(Ordering::Relaxed) == generation
        && match generation {
            1 => target == CODE_VA && c.old_eligible.load(Ordering::Relaxed) != 0,
            2 => target == CODE_NEXT,
            _ => false,
        }
}

pub unsafe fn try_reuse_old(_guard: &coordinator::Guard<'_>) -> bool {
    let c = coordinator();
    if c.phase.load(Ordering::Relaxed) != OPEN
        || c.old_eligible.load(Ordering::Relaxed) != 0
        || c.retained.load(Ordering::Relaxed) != 0
        || c.published.load(Ordering::Relaxed) != 2
        || c.reclaimed.load(Ordering::Relaxed) != 0
        || c.active.load(Ordering::Relaxed) != c.new_references.load(Ordering::Relaxed)
    {
        return false;
    }
    unsafe {
        publish_slot(CODE_VA, &[0; 44]);
    }
    true
}
