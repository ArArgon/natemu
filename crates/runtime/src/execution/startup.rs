//! M2 Part 2: INIT and start-up IPIs (SIPI) in the resident (boot-interpreter plan section 6; the protocol is natemu's in
//! `tools/models/initsipi_model.py`, checked there against the SDM's and AMD's rules, KVM and QEMU over every interleaving;
//! the design is docs/restructure/initsipi-resident.md).
//!
//! **The state** is the vCPU's pending word ([`Events::pending`], bits [`pending`]): INIT pending (I), waiting for a SIPI
//! (W), a SIPI latched with its vector (S), beside the NMI bit (N). Senders ([`send`], from `lapic::send`) change it in one
//! atomic update each and post only when it changed; the vCPU takes I ([`init`]) and S ([`wait`]) in one update each
//! (`natemu_abi::lapic::startup`'s rules). So a sender latches or drops a SIPI on the word the vCPU publishes, with no
//! window between the vCPU entering the wait and senders seeing it.
//!
//! **Taking INIT** ([`init`]): at a boundary (`events::take`, which then enters the interpreter through
//! `debug::INTERPRET`), after HLT wakes for it (`events::park` looks with `events::next`, which reports I first), in the
//! ACPI mailbox wait (`events::wait_mailbox`), and in the interpreter loop (`events::interpreted`). Translated code reaches a
//! boundary at the sender's doorbell; a vCPU inside a host request at the boundary after it. INIT is not masked by
//! RFLAGS.IF, NMI blocking or a shadow (model A6).
//!
//! **The wait for a SIPI** ([`wait`]) is a `wfi` loop like the mailbox wait: idle for the host's end rule and watchdog
//! (`Events::halted` state 1 with the posts it has seen), serving peers' shadow requests, ended by the host
//! ([`stop::WAIT_FOR_SIPI`]: reported as waiting for a SIPI, never as halted). An NMI sent there stays pending (held: the
//! first event after the SIPI, model A1); a fixed interrupt is not accepted (INIT left the APIC software disabled).
use super::*;
use core::sync::atomic::{AtomicU64, Ordering::SeqCst};
use natemu_abi::execution::cache::{Events, HALTED_SEEN_SHIFT, pending};
use natemu_abi::lapic::startup as rule;

/// vCPU `v`'s pending word.
fn word(v: u64) -> &'static AtomicU64 {
    lapic::event_word(v, core::mem::offset_of!(Events, pending))
}
/// This vCPU's event record word at `field` (an offset in [`Events`]).
fn event(s: &State, field: usize) -> &'static AtomicU64 {
    lapic::event_word(s.vcpu, field)
}

/// Sends an INIT (`sipi` `None`) or a SIPI with that vector to vCPU `t`: its pending word's update, in one atomic step.
/// Whether the word changed: only then does the sender post and ring (an INIT to a vCPU waiting with nothing latched, or a
/// SIPI it discards, wakes nothing).
pub(super) fn send(t: u64, sipi: Option<u8>) -> bool {
    let new = |w: u64| match sipi {
        Some(vector) => rule::sipi_sent(w, vector),
        None => rule::init_sent(w),
    };
    // (The closure always gives a word: the update retries until its compare-and-swap succeeds.)
    let old = word(t)
        .fetch_update(SeqCst, SeqCst, |w| Some(new(w)))
        .unwrap_or_else(|w| w);
    new(old) != old
}

/// An INIT is pending for this vCPU.
pub(super) fn init_pending(s: &State) -> bool {
    word(s.vcpu).load(SeqCst) & pending::INIT != 0
}

/// The host left this vCPU waiting for a SIPI before it first ran (`execution::wait_for_sipi`: an AP of a boot without
/// the ACPI mailbox, or a program driver's `wait-for-sipi` section).
pub(super) fn waiting(s: &State) -> bool {
    word(s.vcpu).load(SeqCst) & pending::WAIT != 0
}

/// Takes this vCPU's pending INIT (an INIT is pending: the caller looked): the take in one atomic update (an AP clears I
/// and sets W, keeping a SIPI latched while it was pending; the BSP, by IA32_APIC_BASE's BSP flag, clears I and S), then
/// the INIT state ([`reset`]); an AP then waits for its SIPI ([`wait`]) and leaves with the SIPI's state, the BSP at the
/// reset vector. Again while another INIT is pending (the boundary after the start, with no instruction between: an INIT
/// that came after the SIPI that started it). Its caller enters the interpreter (every state it leaves is one).
#[cold]
#[inline(never)]
pub(super) fn init(s: &mut State) {
    let bsp = msr_get(cpu::msr(cpu::MSR_APIC_BASE).unwrap()) & 1 << 8 != 0;
    loop {
        let _ = word(s.vcpu).fetch_update(SeqCst, SeqCst, |w| Some(rule::init_taken(w, bsp)));
        reset(s);
        if !bsp {
            wait(s);
        }
        if !init_pending(s) {
            return;
        }
    }
}

/// What INIT resets (SDM Vol.3 12.1.1, Table 12-1's INIT column; initsipi_model A4): the architectural state
/// (`power_on::init`: CR0.CD/NW, the x87, SSE and AVX state, XCR0 and every MSR but EFER and the FS/GS bases kept), the
/// resident's own event and step state (NMI blocking, the STI and MOV SS shadows, the REP boundary, pending traps: INIT
/// clears pending events, A4), the ACPI mailbox wait (the AP left it), the local APIC ([`lapic::init_reset`]), and the
/// TLBs: every shadow, every L1 entry and every link into a block no longer current (a global flush, as a paging-control
/// change makes; [`tlb`]). `Events::halted` is 0 while it runs.
fn reset(s: &mut State) {
    cache::publish_segment_view([0; 2]); // INIT can park here before any subsequent native entry
    natemu_abi::power_on::init(&mut s.cpu);
    (s.nmi_blocked, s.sti_shadow, s.mid_string, s.shadow_entry, s.plain_step) = (0, 0, 0, 0, 0);
    (s.debug, s.mailbox) = (0, 0);
    event(s, core::mem::offset_of!(Events, halted)).store(0, SeqCst);
    lapic::init_reset(s);
    tlb(s, true);
}

/// The wait-for-SIPI state, until a SIPI starts this vCPU (an AP after INIT, or one the host left there): then the SIPI's
/// state (`power_on::sipi`: CS VV00H, base VV000H, IP 0) and the latch cleared, W with it, in one update. Each pass, as
/// the mailbox wait: drain the doorbells, the I/O checkpoint, this vCPU's invalidations (a peer may wait for its shadow
/// reset), then read `posts` (the posts seen) before the look, so a SIPI latched after it shows as unseen; publish
/// `halted` state 1 (idle for the host's end rule) only when nothing is latched; on the host's end request look once more
/// (a SIPI may land between the look and the load of `end`, as in `park`), else end [`stop::WAIT_FOR_SIPI`]. No INIT is
/// looked at: one sent here changes no I (model W1: it drops a latched SIPI and a held NMI). The SIPI's start clears
/// `halted` before its update, so the host never takes a vCPU that is starting for an idle one.
pub(super) fn wait(s: &mut State) {
    // The INIT state, for the host while this vCPU waits (as `park` writes the record).
    unsafe { write_record(record(), s) };
    let (halted, posts, end) = (
        event(s, core::mem::offset_of!(Events, halted)),
        event(s, core::mem::offset_of!(Events, posts)),
        event(s, core::mem::offset_of!(Events, end)),
    );
    loop {
        events::doorbells(s);
        io_checkpoint(s);
        take_invalidations(s);
        let seen = posts.load(SeqCst);
        if start(s, halted) {
            return;
        }
        halted.store(1 | seen << HALTED_SEEN_SHIFT, SeqCst);
        if end.load(SeqCst) != 0 {
            if start(s, halted) {
                return;
            }
            halt(s, stop::WAIT_FOR_SIPI)
        }
        // SAFETY: waits for an interrupt (masked at EL1: it only wakes the PE); no memory effect.
        unsafe { asm!("dsb sy", "wfi") };
    }
}

/// The start, when a SIPI is latched: true with the SIPI's state in the bank. False when none is, or an INIT took it away
/// between the look and the update (the caller's next pass publishes `halted` again).
fn start(s: &mut State, halted: &AtomicU64) -> bool {
    let w = word(s.vcpu);
    if w.load(SeqCst) & pending::SIPI == 0 {
        return false;
    }
    halted.store(0, SeqCst);
    match w.fetch_update(SeqCst, SeqCst, |w| rule::started(w).map(|(new, _)| new)) {
        Ok(old) => {
            natemu_abi::power_on::sipi(&mut s.cpu, (old >> pending::VECTOR_SHIFT) as u8);
            unsafe { write_record(record(), s) };
            true
        }
        Err(_) => false,
    }
}
