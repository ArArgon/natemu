//! Asynchronous x86 events (plan X2): external interrupts and NMIs, their eligibility, CLI/STI/POPF/HLT.
//!
//! Sources post into the vCPU's shared event record ([`Events`], in the global area: an atomic OR of a vector bit or the
//! NMI bit) and then ring its doorbell; the doorbell only stops generated code precisely (`interrupted`), and the
//! x86 semantics are applied here, at every boundary the resident sees: the top of `find`'s loop (every precise stop,
//! every resident entry that dispatches, and after every system instruction). An event is taken when it is eligible
//! (SDM Vol.3 7.9 Table 7-2: NMI before maskable interrupts; `tools/models/delivery_model.py` `recognise`):
//! - a maskable interrupt (the one the local APIC delivers by its priority rules, [`lapic::deliverable`], X3, or before
//!   it an ExtINT from the 8259 at LINT0, [`lapic::extint`], L4a) needs RFLAGS.IF and no shadow;
//! - an NMI needs NMI blocking clear (set when one is delivered, released by IRET) and no shadow. The model permits an
//!   STI shadow to block NMIs or not (A9); this one blocks them (one rule for both shadows).
//! An event blocked only by a shadow (the STI shadow when STI set IF, or the MOV SS shadow) is taken at the boundary
//! after exactly one more instruction: that instruction runs alone (a single-instruction block in the step slot, or a
//! system transaction; the step block's every exit enters the resident), then the boundary after it is evaluated again
//! ([`take`] returns true). A shadow is honoured exactly at the boundary after its instruction: a system instruction or a
//! delivery ends it, and so does entering generated code (`enter`; a doorbell at that block's first word arms a stop
//! instead of stopping there, `State::shadow_entry`), since every later resident entry follows a completed instruction.
//! A shadow-setting instruction inside a shadow replaces it with its own (STI only when IF was 0): within
//! the model's set-valued `sti?`/`mov_ss?`. Delivery is `delivery::deliver` with EXT = 1, no error code and RF = 1 when
//! the boundary is between REP iterations (`State::mid_string`), else the current RF.
//!
//! HLT parks the vCPU in `wfi` in the VM ([`park`]): with the in-kernel GIC, HVF holds it without a host exit; a
//! doorbell wakes it, the resident acknowledges every pending IRQ, takes its invalidations (a peer may be waiting for its
//! shadow reset) and wakes only for an event it can take (an enabled interrupt or an unblocked NMI, model A10). While
//! parked its record says so (`Events::halted`): the host's watchdog treats it as idle, and the host may end its run
//! ([`Events::end`], [`stop::HALTED`]) when no source could wake it.
use super::lapic;
use super::segments::{Exception, linear};
use super::*;
use core::sync::atomic::{AtomicU64, Ordering::SeqCst};
use natemu_abi::execution::cache::Events;

const IF: u64 = 1 << 9;
const TF: u64 = 1 << 8;
const IOPL: u64 = 3 << 12;
/// The flags POPF may write (CF PF AF ZF SF TF IF DF OF IOPL NT AC ID: not RF, VM, VIF, VIP; SDM Vol.2 POPF).
const POPF_FLAGS: u64 = 0x1
    | 0x4
    | 0x10
    | 0x40
    | 0x80
    | TF
    | IF
    | 1 << 10
    | 1 << 11
    | IOPL
    | 1 << 14
    | 1 << 18
    | 1 << 21;

/// This vCPU's event record word at `field` (an offset in [`Events`]).
fn word(s: &State, field: usize) -> &'static AtomicU64 {
    lapic::event_word(s.vcpu, field)
}

#[derive(Clone, Copy, PartialEq)]
enum Next {
    Nmi,
    Interrupt(u64),
    /// L4a: an ExtINT at LINT0 (its vector comes from the PIC when taken).
    ExtInt,
    /// An event a shadow alone blocks.
    Step,
}
/// The event this boundary may take (Table 7-2), if any.
fn next(s: &State) -> Option<Next> {
    let nmi = s.nmi_blocked == 0 && word(s, core::mem::offset_of!(Events, nmi)).load(SeqCst) != 0;
    // L4a: an ExtINT ignores the APIC's priority and comes before the IRR (QEMU's `apic_get_interrupt`).
    let interrupt = match s.cpu.rflags & IF {
        0 => None,
        _ if lapic::extint(s) => Some(Next::ExtInt),
        _ => lapic::deliverable(s).map(Next::Interrupt),
    };
    match (nmi, interrupt) {
        (false, None) => None,
        _ if s.cpu.mov_ss != 0 || s.sti_shadow != 0 => Some(Next::Step),
        (true, _) => Some(Next::Nmi),
        (false, i) => i,
    }
}

/// Perf chunk 22: [`take`] would take nothing at this boundary and no shadow step is owed (no event it may take, none a
/// shadow holds).
pub(super) fn none(s: &State) -> bool {
    next(s).is_none()
}

/// Perf chunk 15: an event is pending that would be taken at a boundary with RFLAGS.IF set and no shadow (an NMI not blocked,
/// the ExtINT or an APIC interrupt [`next`] takes): the resident's event-pending byte, which an inline STI reads. The common
/// case (no vector, ExtINT or unblocked NMI posted) is one relaxed look at the record's words: a post this misses rings the
/// doorbell after its store (the sender's order), whose IRQ is taken once generated code runs and writes the byte again.
/// Not seen here: a LAPIC timer expiry that is due but not yet polled, and reinject's backlog (both enter the IRR at the
/// next boundary's `lapic::poll`). Such an interrupt, with IF = 0 when it fell due, is taken at the first block exit after
/// the inline STI's next instruction rather than right after it: later than an interrupt already pending would be, which
/// the SDM permits for one recognised after the STI (the timer's expiry is asynchronous to the instruction stream).
pub(super) fn held(s: &State) -> bool {
    use core::mem::offset_of;
    let w = |field: usize| word(s, field).load(core::sync::atomic::Ordering::Relaxed);
    let v = offset_of!(Events, vectors);
    let nmi = s.nmi_blocked == 0 && w(offset_of!(Events, nmi)) != 0;
    nmi || (w(v) | w(v + 8) | w(v + 16) | w(v + 24) | w(offset_of!(Events, lint0)) != 0
        && (lapic::extint(s) || lapic::deliverable(s).is_some()))
}

/// Takes every event this boundary may take, delivering each (the handler's first instruction is a boundary too).
/// True when an event is pending that a shadow alone blocks: the caller runs exactly one instruction next.
pub(super) fn take(s: &mut State) -> bool {
    loop {
        let event = match next(s) {
            None => return false,
            Some(Next::Step) => return true,
            Some(Next::Nmi) => {
                word(s, core::mem::offset_of!(Events, nmi)).store(0, SeqCst);
                s.nmi_blocked = 1; // before delivery, and kept if it faults (SDM Vol.3 7.7.2; model A8)
                s.nmis += 1;
                2
            }
            Some(Next::Interrupt(v)) => {
                lapic::accept(s, v);
                s.interrupts += 1;
                v
            }
            Some(Next::ExtInt) => {
                s.interrupts += 1;
                lapic::take_extint(s)
            }
        };
        let mid_string = s.mid_string != 0;
        deliver(
            s,
            Event::External {
                vector: event,
                mid_string,
            },
        );
        // S4: a frame store that met a data breakpoint traps before anything else at the handler's first boundary
        // (the caller takes it first; a delivery ended any shadow, so nothing holds it).
        if s.debug & debug::TRAP != 0 {
            return false;
        }
    }
}

/// CLI, STI, POPF, HLT (its check: the park follows the commit, [`park`]) and the test drivers' event hook, after the
/// common #UD (LOCK) check. 64-bit mode, no VME/PVI in the profile (CR4.VME, PVI stay 0): CLI and STI at CPL > IOPL are
/// #GP(0); HLT at CPL > 0 is #GP(0).
pub(super) fn transaction(s: &mut State, i: System) -> Result<(), Exception> {
    let (cpl, iopl) = (s.cpu.cpl, s.cpu.rflags >> 12 & 3);
    match i.kind {
        system::CLI | system::STI if cpl > iopl => Err(GP),
        system::CLI => {
            s.cpu.rflags &= !IF;
            Ok(())
        }
        system::STI => {
            // The shadow only when STI sets IF (SDM Vol.3 7.8.1); it replaces a shadow this STI runs in.
            if s.cpu.rflags & IF == 0 {
                s.sti_shadow = 1;
            }
            s.cpu.rflags |= IF;
            Ok(())
        }
        system::HLT if cpl != 0 => Err(GP),
        system::HLT | system::EVENT_HOOK => Ok(()),
        system::POPF => popf(s, i),
        _ => halt(s, stop::BAD_REPLY),
    }
}

/// POPF/POPFQ (SDM Vol.2 POPF, 64-bit mode, VM = 0): pops 8 bytes (2 with a 66 prefix) at RSP (#SS(0) non-canonical,
/// #PF, #AC at CPL 3) and writes the flags the CPL allows: at CPL 0 every flag of [`POPF_FLAGS`]; above CPL 0 not IOPL,
/// and not IF when CPL > IOPL (both stay as they were, no fault). RF, VM, VIF and VIP are unaffected (RF then ends with
/// the instruction, `commit`). A TF it sets takes effect after the next instruction (S4: `find` reads TF at each start).
fn popf(s: &mut State, i: System) -> Result<(), Exception> {
    let n = i.size as usize;
    let rsp = s.cpu.gpr[4];
    let mut b = [0u8; 8];
    linear(s, rsp, &mut b[..n], false, false, [12, 0, 0])?;
    let mut mask = if n == 2 {
        POPF_FLAGS & 0xffff
    } else {
        POPF_FLAGS
    };
    if s.cpu.cpl > 0 {
        mask &= !IOPL;
        if s.cpu.cpl > s.cpu.rflags >> 12 & 3 {
            mask &= !IF;
        }
    }
    s.cpu.rflags = s.cpu.rflags & !mask | u64::from_le_bytes(b) & mask | 2;
    s.cpu.gpr[4] = rsp.wrapping_add(n as u64);
    Ok(())
}

/// BT/BTS/BTR/BTC with a memory operand under alignment checking or data breakpoints (fexc cut it for the
/// operand-unit precheck). LOCK BT is #UD. The operand at EA (and, for a register
/// bit offset, the operand-sized unit EA + size * (offset >> log2(bits)) it selects, which has EA's alignment) must be
/// aligned to its size: #AC(0) when it is not and that unit is canonical (a non-canonical one is the step's #GP/#SS, which
/// comes first). Otherwise `Ok`: the caller steps the instruction compiled without the cut.
pub(super) fn bit_test(s: &mut State, i: System) -> Result<(), Exception> {
    if i.lock != 0 && i.cr == 0 {
        return Err(UD);
    }
    let size = u64::from(i.size);
    let mut operand = i;
    if i.reg != system::NO_REG {
        let r = s.cpu.gpr[i.reg as usize];
        let offset = match size {
            2 => r as i16 as i64,
            4 => r as i32 as i64,
            _ => r as i64,
        };
        operand.disp = operand
            .disp
            .wrapping_add((offset >> (size * 8).trailing_zeros()).wrapping_mul(size as i64));
    }
    let unit = super::segments::address(&s.cpu, &operand);
    let last = unit.wrapping_add(size - 1);
    // S4: the complete operand-sized unit meets data breakpoints. The following native step now accesses that same
    // unit; duplicate hit bits are harmless and a fault suppresses the pending data-breakpoint trap.
    super::watch::access(s, unit, size, i.cr != 0);
    if s.cpu.alignment_check()
        && unit & (size - 1) != 0
        && paging::canonical(unit)
        && paging::canonical(last)
        && last > unit
    {
        return Err([17, 0, 0]);
    }
    Ok(())
}

/// HLT (committed: RIP is past it): parks in `wfi` until an event can be taken, which the caller then delivers (its
/// frame holds the RIP after the HLT). Each wake acknowledges every pending doorbell (IRQs stay masked at EL1; a pending
/// one still ends `wfi`) and takes this vCPU's invalidations. The host's end request stops the vCPU `halted` here.
pub(super) fn park(s: &mut State) {
    let p = crate::perf::enter(crate::perf::HALTED);
    // A stop a doorbell asked for while this vCPU ran to its HLT is served here, at the boundary after the HLT.
    served(s);
    unsafe { write_record(record(), s) };
    let halted = word(s, core::mem::offset_of!(Events, halted));
    let posts = word(s, core::mem::offset_of!(Events, posts));
    // Perf chunk 14 (`perf` resident only): the wake latency, from the first post after this point (`Events::rung`).
    let rung = word(s, core::mem::offset_of!(Events, rung));
    if crate::perf::ON {
        rung.store(0, SeqCst);
    }
    let (mut parked, mut woke, mut deadline) = (false, false, u64::MAX);
    loop {
        doorbells(s);
        // Drain the wake before checking its work: the opposite order can consume an I/O
        // kick after the checkpoint and then sleep with an unacknowledged window pending.
        super::io_checkpoint(s);
        take_invalidations(s);
        // X3: the posts seen are counted before the look, so a post after it shows as unseen (senders count after the bit).
        let seen = posts.load(SeqCst);
        lapic::poll(s);
        if next(s).is_some() {
            break;
        }
        // X3: armed, its own LAPIC timer wakes it (the host does not end it then).
        let wakes = lapic::can_wake(s);
        halted.store(
            if wakes { 2 } else { 1 } | seen << natemu_abi::execution::cache::HALTED_SEEN_SHIFT,
            SeqCst,
        );
        if !wakes && word(s, core::mem::offset_of!(Events, end)).load(SeqCst) != 0 {
            // A peer's post may land between `next` and the load of `end`: look once more before ending.
            if next(s).is_some() {
                break;
            }
            halt(s, stop::HALTED)
        }
        if !parked {
            parked = true;
            s.halts += 1; // it waits: an event it could take at once never parks it
        }
        // L5: `wfi`, woken on time for the timer's next expiry.
        deadline = lapic::deadline();
        super::io_halt_wait(s);
        if crate::perf::ON && !woke {
            let r = rung.load(SeqCst);
            if r != 0 {
                woke = true;
                crate::perf::wake(crate::perf::POST_WAKE, lapic::now().saturating_sub(r));
            }
        }
    }
    if crate::perf::ON && parked {
        let (r, t) = (rung.load(SeqCst), lapic::now());
        if r != 0 {
            crate::perf::wake(crate::perf::POST_RUN, t.saturating_sub(r));
        } else if deadline <= t {
            crate::perf::wake(crate::perf::TIMER_RUN, t - deadline);
        }
    }
    halted.store(0, SeqCst);
    crate::perf::leave(p);
}
/// Doorbells that woke a waiting vCPU count as one stop taken at a resident entry (the others merged into it): the host's
/// kickers wait for one, so it is made visible at once.
fn doorbells(s: &mut State) {
    let n = acknowledge();
    if n > 0 {
        taken(s);
        (s.stops_resident, s.merged) = (s.stops_resident + 1, s.merged + n - 1);
        unsafe { write_record(record(), s) };
    }
}

/// X4: an AP in the firmware's waiting state (`State::mailbox`), before it ever runs: the responder of the ACPI
/// multiprocessor wakeup mailbox, version 0 (ACPI 6.5 5.2.12.19). The OS writes the APIC ID and the wakeup vector, then
/// the command (Linux: a release store, `acpi_wakeup_cpu`). The AP whose APIC ID (= its vCPU number, L2) matches a
/// Wakeup command caches the vector, then clears the command to Noop as its acknowledgement and starts at the vector in
/// its bank's state, the firmware's (long mode, paging with the vector's page identity mapped, flat selectors, IF = 0:
/// the host's); any other ID or command is ignored, and the mailbox is never looked at again (one wakeup per AP).
///
/// **The wait** is the code cache's own write watch: the AP marks the mailbox page as holding its translations
/// ([`super::mark`]: no vCPU keeps a writable shadow of it once that returns), so any later guest store to the page
/// faults in its writer's resident, which posts the page to the AP and rings its doorbell (`cache::written`) before the
/// store completes; the AP sleeps in `wfi` (no host exit with the in-kernel GIC) and, on each doorbell, drains, marks
/// the page again and reads the mailbox. A store made before a mark is seen by the read after it, one made after rings
/// the AP: no wake-up is lost and nothing polls. (Measured alternative: `ldaxr` + `wfe`, the exclusive monitor's wake,
/// does not sleep under HVF: `wfe` returned about every 0.8 us.) While waiting the vCPU is idle for the watchdog and the
/// host's end rule (`Events::halted` state 1, every post seen: no post wakes it, the APIC's events stay pending for
/// after the wakeup); the host's end request is read before the mailbox, so a command written before the decision to
/// end (by a vCPU that then halted) is still served.
pub(super) fn wait_mailbox(s: &mut State) {
    let from = crate::perf::enter(crate::perf::MAILBOX);
    let gpa = s.mailbox;
    let Some(p) = guest_ram().range(gpa, 16).filter(|_| gpa % 4096 == 0) else {
        halt(s, stop::UNSUPPORTED_MODE)
    };
    let (halted, posts, end) = (
        word(s, core::mem::offset_of!(Events, halted)),
        word(s, core::mem::offset_of!(Events, posts)),
        word(s, core::mem::offset_of!(Events, end)),
    );
    loop {
        doorbells(s);
        // As in HLT, never consume the only I/O wake after checking the window request.
        super::io_checkpoint(s);
        take_invalidations(s);
        halted.store(
            1 | posts.load(SeqCst) << natemu_abi::execution::cache::HALTED_SEEN_SHIFT,
            SeqCst,
        );
        let ending = end.load(SeqCst) != 0;
        // The re-mark's cost (the review's item): it may spin until a writer that holds a writable shadow of the page
        // resets its shadows (a writer in the host answers only when it next enters the VM), and each such writer
        // drops all its shadows (`State::shootdowns` counts the writers asked).
        let t0 = ticks();
        super::mark(s, gpa);
        s.mailbox_mark_ticks += ticks() - t0;
        // SAFETY: aligned loads of guest RAM through the resident's alias; the vector after the command (acquire).
        let head = unsafe { AtomicU64::from_ptr(p.cast()) }.load(SeqCst);
        if head & 0xffff == mailbox::WAKEUP && head >> 32 == s.vcpu {
            // ORDER (an invariant held by this code, checked by inspection; no test can see it, since the OS may write
            // the vector again only after the acknowledgement, and `run_x4.py`'s probe of that window rarely lands in
            // it): the vector is read here, before the acknowledgement's store below (ACPI 6.5 5.2.12.19: the firmware
            // "must cache the content in the mailbox which might be used later before clear the command").
            s.cpu.rip =
                unsafe { core::ptr::read_volatile(p.add(mailbox::VECTOR as usize).cast::<u64>()) };
            (s.mailbox, s.mailbox_acked) = (0, ticks());
            halted.store(0, SeqCst); // running before the OS can see the acknowledgement (the host's end rule)
            // The acknowledgement, a store through the alias with the code-write protocol (peers' marks: other waiting APs).
            if cache::translated(gpa) {
                match cache::written(gpa) {
                    Some(n) => (s.invalidations, s.unlinks) = (s.invalidations + 1, s.unlinks + n),
                    None => halt(s, stop::BAD_LINK),
                }
            }
            unsafe { core::sync::atomic::AtomicU16::from_ptr(p.cast()) }.store(0, SeqCst);
            cache::rechecked(gpa);
            crate::perf::leave(from);
            return;
        }
        if ending {
            halt(s, stop::HALTED)
        }
        s.mailbox_wakes += 1;
        // SAFETY: waits for an interrupt (masked at EL1: it only wakes the PE); no memory effect.
        unsafe { asm!("dsb sy", "wfi") };
    }
}

/// Acknowledges and ends every pending doorbell (ICC_IAR1_EL1 until it reads a spurious INTID, ICC_EOIR1_EL1 each);
/// how many there were.
fn acknowledge() -> u64 {
    let mut any = 0;
    loop {
        let intid: u64;
        // SAFETY: GIC CPU interface system registers (enabled at start).
        unsafe { asm!("isb", "mrs {i}, S3_0_C12_C12_0", i = out(reg) intid) };
        if intid >= 1020 {
            return any;
        }
        // L5: a timer IRQ before its expiry (a halted vCPU's early wake, `lapic::wait`) is no stop and no counted IRQ.
        let counted = intid != lapic::TIMER_PPI || lapic::timer_irq();
        unsafe { asm!("msr S3_0_C12_C12_1, {i}", "isb", i = in(reg) intid) };
        if !counted {
            continue;
        }
        any += 1;
        let c = cache::control();
        c.irqs += 1;
        c.irq_sgis += u64::from(intid < 16);
    }
}
