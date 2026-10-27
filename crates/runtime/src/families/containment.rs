//! Failure injection is entered only at captured native authority boundaries.
//! Witness publication never changes canonical state or drains retained work.
use super::*;
use core::sync::atomic::Ordering::{Acquire, Relaxed, Release};
use natemu_abi::{
    concurrency as n, families::containment as c, families::control_concurrent as cc, families::controls as ctl,
    families::partial_progress as p, recovery, families::schedule as sc, translation as wire,
};
unsafe extern "C" {
    fn service(packet: *mut wire::Packet);
    fn service_resume();
}
fn state() -> *mut c::State {
    c::VA as *mut c::State
}
fn coord() -> &'static n::Coordinator {
    unsafe { &*(n::COORD_VA as *const n::Coordinator) }
}
fn id() -> u64 {
    unsafe { (*shared()).object.vcpu_id as u64 }
}
pub fn enabled() -> bool {
    unsafe {
        if !schedule::enabled() {
            return false;
        }
        match (*(sc::VA as *const sc::State)).containment {
            0 => false,
            c::MAGIC => {
                let s = state();
                if (*s).magic != c::MAGIC
                    || (*s).version != c::VERSION
                    || !(*s).config.valid()
                    || (*s).config.schedule() != Some((*(sc::VA as *const sc::State)).config)
                {
                    stop(PANIC, c::BAD);
                }
                true
            }
            _ => stop(PANIC, c::BAD),
        }
    }
}
pub fn config() -> Option<c::Config> {
    enabled().then(|| unsafe { (*state()).config })
}
pub fn source_cut() -> bool {
    config().is_some_and(|c| c.family == sc::CONTROLS && c.failure != c::WITHHELD_ACK)
}
pub fn partial_cut() -> bool {
    config().is_some_and(|c| c.family == sc::PARTIAL)
}
pub fn delayed(default: u64) -> u64 {
    config()
        .filter(|c| c.failure == c::WITHHELD_ACK)
        .map_or(default, |c| c.actor)
}
pub fn park() -> ! {
    loop {
        core::hint::spin_loop();
    }
}
#[inline(never)]
unsafe fn witness(g: &coordinator::Guard<'_>, site: u64, packet: wire::Packet) {
    unsafe {
        let s = state();
        let cfg = (*s).config;
        if id() != cfg.actor || (*s).ready.load(Acquire) != 0 {
            stop(PANIC, c::BAD);
        }
        let w = &raw mut (*s).witness;
        let co = coord();
        (*w).site = site;
        (*w).owner = id();
        (*w).phase = co.phase.load(Relaxed);
        (*w).active = co.active.load(Relaxed);
        (*w).queued = co.queued.load(Relaxed);
        (*w).retained = co.retained.load(Relaxed);
        (*w).acknowledgements = co.acknowledgements.load(Relaxed);
        (*w).servicing = co.servicing.load(Relaxed);
        (*w).lock = co.lock.load(Relaxed);
        (*w).next_ticket = co.next_ticket.load(Relaxed);
        (*w).pending_events = 0;
        if cfg.family == sc::CONTROLS {
            for cpu in 0..cfg.n as usize {
                let previous = co.event_pending[cpu].load(Relaxed);
                if previous != 1 {
                    if !matches!(previous, 0 | 2)
                        || co.event_pending[cpu]
                            .compare_exchange(previous, 1, Release, Relaxed)
                            .is_err()
                    {
                        stop(PANIC, c::BAD);
                    }
                }
                co.event_notifications[cpu].fetch_add(1, Relaxed);
                g.trace(c::NOTIFY_EVENT, cpu as u64)
                    .unwrap_or_else(|e| stop(PANIC, e));
            }
            let r = (PRIVATE + ctl::OFFSET as u64) as *const ctl::State;
            let common = (PRIVATE + cc::SHARED_OFFSET as u64) as *const cc::Shared;
            (*w).ready_owners = (*common).final_ready;
            (*w).profile = (*r).profile;
            (*w).visit = (*r).visits;
            (*w).completed = (*r).completed;
            (*w).context = (*r).generation;
            (*w).source_generation = (*common).source_generation;
            for cpu in 0..cfg.n as usize {
                if co.event_pending[cpu].load(Relaxed) == 1 {
                    (*w).pending_events |= 1 << cpu;
                }
            }
            copy_record(
                (&raw mut (*w).guest).cast(),
                (recovery::GUEST_VA as *const recovery::GuestState).cast(),
                size_of::<recovery::GuestState>(),
            );
            copy_frame(&raw mut (*w).frame, &raw const (*r).current);
            copy_record((*w).code.as_mut_ptr(), CODE_VA as *const u8, 640);
        } else {
            let o = (PRIVATE + p::OWNER_OFFSET as u64) as *const p::Owner;
            let common = (PRIVATE + p::COMMON_OFFSET as u64) as *const p::Common;
            (*w).ready_owners = co.queued.load(Relaxed);
            (*w).profile = p::MODE;
            (*w).visit = (*o).subaccess;
            (*w).completed = (*o).writes;
            (*w).context = 1;
            (*w).source_generation = (*common).source_generation;
            (*w).pending_events = (*common).pending_events;
            (*w).helper = [
                (*o).reads,
                (*o).writes,
                (*o).phase,
                (*o).subaccess,
                (*o).sampled,
                (*o).source_route,
                (*o).destination_route,
                (*o).destination_writable,
            ];
            copy_record(
                (&raw mut (*w).guest).cast(),
                (&raw const (*o).after).cast(),
                size_of::<recovery::GuestState>(),
            );
            copy_frame(&raw mut (*w).frame, &raw const (*o).fault[0].frame);
        }
        (*w).deferred_started = (*s).deferred_started;
        (*w).producer_ready = (*s).producer_ready;
        (*w).container_ready = (*s).container_ready;
        (*w).packet = packet;
        g.trace(c::GATE_EVENT, id())
            .unwrap_or_else(|e| stop(PANIC, e));
        (*w).trace_length = g.trace_length();
        (*s).ready.store(1, Release);
    }
}
/// Called under the real source authority. A PANIC intentionally never drops
/// this guard; the host must destroy the VM rather than fabricate an unlock.
pub unsafe fn panic_at_source(g: &coordinator::Guard<'_>, packet: wire::Packet) {
    unsafe {
        if config().is_some_and(|c| c.failure == c::LOCKED_PANIC && c.actor == id()) {
            witness(g, c::SOURCE_GATE, packet);
            stop(PANIC, c::INJECTED_PANIC);
        }
    }
}
/// Caller drops the guard and parks if true. Other owners have acknowledged;
/// the actor's real acknowledgement bit remains clear.
pub unsafe fn withhold_ack(g: &coordinator::Guard<'_>, packet: wire::Packet) -> bool {
    unsafe {
        let Some(cfg) = config() else { return false };
        if cfg.failure != c::WITHHELD_ACK || cfg.actor != id() {
            return false;
        }
        if coord().acknowledgements.load(Relaxed) != cfg.all() ^ (1 << id()) {
            return false;
        }
        witness(g, c::ACK_GATE, packet);
        true
    }
}
unsafe fn send(packet: wire::Packet, action: u64) -> wire::Packet {
    unsafe {
        let s = state();
        (*s).expected = packet;
        (*s).expected[0] = action;
        (*shared()).service_pc = service_resume as *const () as u64;
        if (*s).config.family == sc::CONTROLS {
            (*((PRIVATE + ctl::OFFSET as u64) as *mut ctl::State)).expected = (*s).expected;
        }
        service(&raw mut (*s).expected);
        (*s).expected
    }
}
/// An extra compile-only job belongs to this fixture cut. It is never published
/// or executed; the existing current block and saved routes remain retained.
#[inline(never)]
pub unsafe fn deferred_gate(packet: wire::Packet, site: u64) -> ! {
    unsafe {
        let s = state();
        let cfg = (*s).config;
        if id() != cfg.actor || (*s).deferred_started != 0 || ctl::reply(&packet).is_none() {
            stop(PANIC, c::BAD);
        }
        if send(packet, n::DEFER_COMPILE) != wire::command(wire::BUSY) {
            stop(PANIC, c::BAD);
        }
        (*s).deferred_started = 1;
        if cfg.failure == c::MALFORMED || cfg.failure == c::CANCEL {
            loop {
                let opcode = if cfg.failure == c::CANCEL {
                    c::PEEK_CONTAINER
                } else {
                    c::PEEK_DEFERRED
                };
                let reply = send(packet, opcode);
                if reply == wire::command(c::PRODUCER_READY) {
                    if cfg.failure == c::CANCEL {
                        (*s).container_ready = 1;
                    } else {
                        (*s).producer_ready = 1;
                    }
                    break;
                }
                if reply != wire::command(wire::BUSY) {
                    stop(PANIC, c::BAD);
                }
                core::hint::spin_loop();
            }
        }
        {
            let g = coordinator::lock(coord());
            witness(&g, site, packet);
        }
        if cfg.failure == c::CANCEL {
            park();
        }
        while (*s).command.load(Acquire) != c::PROCEED {
            core::hint::spin_loop();
        }
        if send(packet, n::TAKE_COMPILE) != wire::command(wire::BUSY) {
            stop(PANIC, c::BAD);
        }
        loop {
            let reply = send(packet, n::POLL_COMPILE);
            if reply == wire::command(wire::BUSY) {
                core::hint::spin_loop();
                continue;
            }
            if !ctl::valid(&packet, &reply) {
                stop(REJECTED, 276);
            }
            // A healthy reply means the requested failure did not occur. Do not
            // publish it or report successful guest execution.
            stop(PANIC, c::UNEXPECTED_COMPLETION);
        }
    }
}
pub unsafe fn controls_request(mut packet: wire::Packet) -> wire::Packet {
    unsafe {
        let r = (PRIVATE + ctl::OFFSET as u64) as *mut ctl::State;
        (*r).serial = (*r)
            .serial
            .checked_add(1)
            .unwrap_or_else(|| stop(PANIC, c::BAD));
        packet[12] = (*r).serial;
        (*((PRIVATE + natemu_abi::families::validation::STATE_OFFSET as u64)
            as *mut natemu_abi::families::validation::State))
            .requests += 1;
        if ctl::reply(&packet).is_none() {
            stop(PANIC, c::BAD);
        }
        packet
    }
}
