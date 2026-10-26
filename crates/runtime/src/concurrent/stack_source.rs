//! Three-chunk compilation uses the same source/retirement coordinator as I4.
use super::*;
use natemu_abi::{stack as p, stack_concurrent as sc, validation::Packet};
unsafe extern "C" {
    fn service(packet: *mut Packet);
    fn service_resume();
}
fn state() -> *mut sc::State {
    (PRIVATE + sc::OFFSET as u64) as *mut sc::State
}
unsafe fn request(serial: u64, variant: u64, chunk: u64) -> Packet {
    unsafe {
        p::request_context(
            (*shared()).lifecycle.vm,
            0,
            1,
            variant + 1,
            1,
            serial,
            natemu_abi::alias::source_rip(coordinator().alias_profile),
            variant,
            chunk,
        )
    }
}
unsafe fn exchange(mut packet: Packet) -> Packet {
    unsafe {
        write_record(&raw mut (*state()).expected, packet);
        service(&mut packet);
        while packet[0] == natemu_abi::translation::BUSY {
            events::wait_check();
            for _ in 0..16 {
                asm!("yield", options(nomem, nostack));
            }
            packet = read_record(&raw const (*state()).expected);
            packet[0] = POLL_COMPILE;
            write_record(&raw mut (*state()).expected, packet);
            service(&mut packet);
        }
        packet
    }
}
unsafe fn collect(serial: u64, variant: u64, take: bool) -> [u8; 52] {
    unsafe {
        let mut body = [0; 52];
        for chunk in 0..3 {
            let expected = request(serial, variant, chunk);
            let mut sent = expected;
            if take && chunk == 0 {
                sent[0] = TAKE_COMPILE;
            }
            let reply = exchange(sent);
            write_record(
                &raw mut (*state()).replies[serial as usize - 1][chunk as usize],
                reply,
            );
            let bytes = sc::validate_reply(&expected, &reply).unwrap_or_else(|| stop(PANIC, 160));
            if take {
                let current = request(3, 1, chunk);
                if sc::validate_reply(&current, &reply).is_some()
                    || (*state()).compile_requests[2] != request(3, 1, 0)
                {
                    stop(PANIC, 161);
                }
                (*state()).stale_chunks += 1;
            } else if serial == 3 && chunk == 0 {
                let mut wrong = reply;
                wrong[3] ^= 1;
                if sc::validate_reply(&expected, &wrong).is_some() {
                    stop(PANIC, 162);
                }
                (*state()).wrong_owner_rejections = 1;
            }
            let start = chunk as usize * 24;
            let count = (52 - start).min(24);
            body[start..start + count].copy_from_slice(&bytes[..count]);
        }
        body
    }
}
unsafe fn source_bytes() -> [u8; 20] {
    unsafe {
        read_record((PRIVATE + natemu_abi::recovery::SOURCE_OFFSET as u64) as *const [u8; 20])
    }
}
pub unsafe fn initialize() {
    unsafe {
        (*shared()).service_pc = service_resume as *const () as u64;
        if (*shared()).lifecycle.vm == 0 || source_bytes() != p::source(0) {
            stop(PANIC, 163);
        }
        {
            let _g = coordinator::lock(coordinator());
            aliases::fetch();
        }
        write_record(&raw mut (*state()).compile_requests[0], request(1, 0, 0));
        coordinator().translation_wait.store(1, Ordering::Release);
        if coordinator().participants == 1 {
            events::post_all(18);
        }
        let body = collect(1, 0, false);
        source::publish_slot(CODE_VA, &p::image(&body));
        let mut deferred = request(2, 0, 0);
        write_record(&raw mut (*state()).compile_requests[1], deferred);
        deferred[0] = DEFER_COMPILE;
        write_record(&raw mut (*state()).expected, deferred);
        service(&mut deferred);
        if deferred[0] != natemu_abi::translation::BUSY {
            stop(PANIC, 164);
        }
        while coordinator().event_pending[0].load(Ordering::Acquire) == 0 {
            events::wait_check();
            asm!("yield", options(nomem, nostack));
        }
        coordinator().published.store(1, Ordering::Release);
    }
}
// Caller holds the exclusive coordinator grant; all old programs have finished.
pub unsafe fn invalidate() {
    unsafe {
        coordinator().old_eligible.store(0, Ordering::Relaxed);
        write_record(&raw mut (*state()).source_before, source_bytes());
        aliases::rewrite_source(&p::source(1));
        write_record(&raw mut (*state()).source_after, source_bytes());
        write_record(&raw mut (*state()).compile_requests[2], request(3, 1, 0));
    }
}
pub unsafe fn replacement() {
    unsafe {
        events::post_all(19);
        let _old = collect(2, 0, true);
        let body = collect(3, 1, false);
        let c = coordinator();
        let g = coordinator::lock(c);
        if c.phase.load(Ordering::Relaxed) != COMPILING
            || c.generation.load(Ordering::Relaxed) != 2
            || c.old_eligible.load(Ordering::Relaxed) != 0
            || source_bytes() != p::source(1)
            || (*state()).stale_chunks != 3
            || core::slice::from_raw_parts(CODE_NEXT as *const u8, p::CODE_BYTES)
                != [0; p::CODE_BYTES]
        {
            stop(PANIC, 165);
        }
        aliases::fetch();
        (*state()).blank_before_publish = 1;
        source::publish_slot(CODE_NEXT, &p::image(&body));
        c.stale_replies.store(1, Ordering::Relaxed);
        c.published.store(2, Ordering::Relaxed);
        checked(g.trace(16, 0));
        c.phase.store(RELEASING, Ordering::Relaxed);
    }
}
