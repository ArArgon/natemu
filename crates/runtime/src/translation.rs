use super::*;
use natemu_abi::translation as wire;

unsafe extern "C" {
    fn service(packet: *mut wire::Packet);
    fn service_resume();
    fn restart_dispatch() -> !;
}

pub unsafe fn initialize() -> ! {
    unsafe {
        let s = shared();
        let vm = ptr::read_volatile(&raw const (*s).lifecycle.vm);
        if vm == 0 {
            stop(PANIC, 20);
        }
        ptr::write_volatile(&raw mut (*s).lifecycle, wire::Lifecycle::new(vm));
        ptr::write_volatile(&raw mut (*s).service_pc, service_resume as *const () as u64);
        ptr::copy_nonoverlapping(wire::XOR.as_ptr(), wire::SOURCE_VA as *mut u8, 17);
        restart_dispatch();
    }
}

pub unsafe fn completed() -> ! {
    unsafe {
        let s = shared();
        let mut state = ptr::read_volatile(&raw const (*s).lifecycle);
        state.executions = state
            .executions
            .checked_add(1)
            .unwrap_or_else(|| stop(PANIC, 21));
        state.event = wire::EXECUTED;
        // Return has captured the current roots. Never restore the original seed.
        ptr::write_volatile(
            &raw mut (*s).initial,
            ptr::read_volatile(&raw const (*s).captured),
        );
        ptr::write_volatile(&raw mut (*s).lifecycle, state);
        restart_dispatch();
    }
}

unsafe fn replace_code(body: &[u8; 44]) {
    unsafe {
        let pte = (PRIVATE + 0x14000 + (CODE_OFFSET / 4096 * 8) as u64) as *mut u64;
        let attrs = 3 | 1 << 10 | 3 << 8;
        let update = |entry| {
            ptr::write_volatile(pte, 0);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
            ptr::write_volatile(pte, entry);
            asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb");
        };
        // No execution or saved native continuation into the slot is live here.
        update((IPA + CODE_OFFSET as u64) | attrs | (3 << 53));
        ptr::copy_nonoverlapping(body.as_ptr(), CODE_VA as *mut u8, 44);
        ptr::copy_nonoverlapping(RET.as_ptr(), (CODE_VA + 44) as *mut u8, 4);
        // Four-byte stepping covers every cache line without a host-line-size premise.
        for offset in (0..48).step_by(4) {
            asm!("dc cvau, {0}",in(reg)CODE_VA+offset);
        }
        asm!("dsb ish");
        for offset in (0..48).step_by(4) {
            asm!("ic ivau, {0}",in(reg)CODE_VA+offset);
        }
        asm!("dsb ish", "isb");
        update((IPA + CODE_OFFSET as u64) | attrs | (1 << 7));
    }
}

unsafe fn execute(state: wire::Lifecycle) -> ! {
    unsafe {
        let s = shared();
        let kind = if state.variant == 0 {
            LIVE_LOAD
        } else {
            LIVE_OR
        };
        let object = Object::new(kind);
        let mut frame = ptr::read_volatile(&raw const (*s).initial);
        let code = core::slice::from_raw_parts(CODE_VA as *const u8, 48);
        if state.pending != 0 || state.cached != 1 {
            stop(PANIC, 22);
        }
        if let Err(reason) = object.validate(&frame, code) {
            stop(REJECTED, reason);
        }
        frame.sp = STACK_TOP;
        frame.x[16] = CODE_VA;
        frame.x[30] = returned as *const () as u64;
        ptr::write_volatile(&raw mut (*s).object, object);
        ptr::write_volatile(&raw mut (*s).initial, frame);
        ptr::write_volatile(&raw mut (*s).capture_pc, frame.x[30]);
        ptr::write_volatile(&raw mut (*s).lifecycle, state);
        enter(&raw const (*s).initial);
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn runtime_dispatch() -> ! {
    unsafe {
        let s = shared();
        loop {
            let mut state = ptr::read_volatile(&raw const (*s).lifecycle);
            let mut packet = state.packet();
            let source = ptr::read_volatile(wire::SOURCE_VA as *const [u8; 17]);
            if source != state.source() {
                stop(PANIC, 29);
            }
            wire::put_bytes(&mut packet[7..10], &source);
            service(&mut packet);
            if packet[1] != VERSION as u64 {
                state.event = wire::BAD_REPLY;
            } else {
                state.event = match packet[0] {
                    wire::DISPATCH => match state.dispatch() {
                        Ok(wire::EXECUTED) => execute(state),
                        Ok(event) => event,
                        Err(_) => stop(PANIC, 23),
                    },
                    wire::SOURCE => match wire::source_words(&packet[7..10]) {
                        Ok(source) if packet[6] == 17 => {
                            if state.change_source(source).is_err() {
                                stop(PANIC, 24);
                            }
                            ptr::copy_nonoverlapping(
                                source.as_ptr(),
                                wire::SOURCE_VA as *mut u8,
                                17,
                            );
                            asm!("dsb ish");
                            wire::CHANGED
                        }
                        _ => wire::BAD_REPLY,
                    },
                    wire::CONTEXT => {
                        if state.change_context().is_err() {
                            stop(PANIC, 25);
                        }
                        wire::CHANGED
                    }
                    wire::CANCEL => match state.cancel(&packet) {
                        Ok(()) => wire::CANCELLED,
                        Err(e) => e,
                    },
                    wire::DELIVER => match state.validate_reply(&packet) {
                        Ok(body) => {
                            let actual = ptr::read_volatile(wire::SOURCE_VA as *const [u8; 17]);
                            if actual != state.source() {
                                stop(PANIC, 26);
                            }
                            replace_code(&body);
                            state.cached = 1;
                            state.pending = 0;
                            execute(state);
                        }
                        Err(e) => e,
                    },
                    wire::SHUTDOWN => {
                        if state.pending != 0 {
                            stop(REJECTED, 27);
                        }
                        stop(SUCCESS, 0)
                    }
                    _ => wire::BAD_REPLY,
                };
            }
            if state.event == wire::STALE || state.event == wire::BAD_REPLY {
                state.rejections = state
                    .rejections
                    .checked_add(1)
                    .unwrap_or_else(|| stop(PANIC, 28));
            }
            ptr::write_volatile(&raw mut (*s).lifecycle, state);
        }
    }
}
