//! Asynchronous x86 events (plan X2): external interrupts and NMIs, their eligibility, CLI/STI/POPF/HLT.
//!
//! Sources post into the vCPU's shared event record ([`Events`], in the global area: an atomic OR of a vector bit or the
//! NMI bit) and then ring its doorbell; the doorbell only stops generated code precisely (`interrupted`), and the
//! x86 semantics are applied here, at every boundary the resident sees: the top of `find`'s loop (every precise stop,
//! every resident entry that dispatches, and after every system instruction). An event is taken when it is eligible
//! (SDM Vol.3 7.9 Table 7-2: NMI before maskable interrupts; `tools/models/delivery_model.py` `recognise`):
//! - a maskable interrupt (the one the local APIC delivers by its priority rules, [`lapic::deliverable`], X3) needs
//!   RFLAGS.IF and no shadow;
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
use super::segments::{Exception, linear};
use super::*;
use core::sync::atomic::{AtomicU64, Ordering::SeqCst};
use super::lapic;
use natemu_abi::execution::cache::Events;

const IF: u64 = 1 << 9;
const TF: u64 = 1 << 8;
const IOPL: u64 = 3 << 12;
/// The flags POPF may write (CF PF AF ZF SF TF IF DF OF IOPL NT AC ID: not RF, VM, VIF, VIP; SDM Vol.2 POPF).
const POPF_FLAGS: u64 = 0x1 | 0x4 | 0x10 | 0x40 | 0x80 | TF | IF | 1 << 10 | 1 << 11 | IOPL | 1 << 14 | 1 << 18 | 1 << 21;

/// This vCPU's event record word at `field` (an offset in [`Events`]).
fn word(s: &State, field: usize) -> &'static AtomicU64 {
    lapic::event_word(s.vcpu, field)
}

#[derive(Clone, Copy, PartialEq)]
enum Next {
    Nmi,
    Interrupt(u64),
    /// An event a shadow alone blocks.
    Step,
}
/// The event this boundary may take (Table 7-2), if any.
fn next(s: &State) -> Option<Next> {
    let nmi = s.nmi_blocked == 0 && word(s, core::mem::offset_of!(Events, nmi)).load(SeqCst) != 0;
    let vector = if s.cpu.rflags & IF == 0 { None } else { lapic::deliverable(s) };
    match (nmi, vector) {
        (false, None) => None,
        _ if s.cpu.mov_ss != 0 || s.sti_shadow != 0 => Some(Next::Step),
        (true, _) => Some(Next::Nmi),
        (false, Some(v)) => Some(Next::Interrupt(v)),
    }
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
        };
        let mid_string = s.mid_string != 0;
        deliver(s, Event::External { vector: event, mid_string });
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
/// the instruction, `commit`). Setting TF stops `unsupported_system` before any effect: single-step is S4's.
fn popf(s: &mut State, i: System) -> Result<(), Exception> {
    let n = i.size as usize;
    let rsp = s.cpu.gpr[4];
    let mut b = [0u8; 8];
    linear(s, rsp, &mut b[..n], false, false, [12, 0, 0])?;
    let mut mask = if n == 2 { POPF_FLAGS & 0xffff } else { POPF_FLAGS };
    if s.cpu.cpl > 0 {
        mask &= !IOPL;
        if s.cpu.cpl > s.cpu.rflags >> 12 & 3 {
            mask &= !IF;
        }
    }
    let rflags = s.cpu.rflags & !mask | u64::from_le_bytes(b) & mask;
    if rflags & TF != 0 {
        halt(s, stop::UNSUPPORTED_SYSTEM)
    }
    s.cpu.rflags = rflags | 2;
    s.cpu.gpr[4] = rsp.wrapping_add(n as u64);
    Ok(())
}

/// BT/BTS/BTR/BTC with a memory operand under alignment checking (fexc cut it: FEX accesses only the byte holding the bit,
/// so the operand-size access x86 checks never happens natively). LOCK BT is #UD. The operand at EA (and, for a register
/// bit offset, the operand-sized unit EA + size * (offset >> log2(bits)) it selects, which has EA's alignment) must be
/// aligned to its size: #AC(0) when it is not and that unit is canonical (a non-canonical one is the step's #GP/#SS, which
/// comes first). Otherwise `Ok`: the caller steps the instruction compiled without the cut.
pub(super) fn bit_test(s: &mut State, i: System) -> Result<(), Exception> {
    if i.lock != 0 && i.cr == 0 {
        return Err(UD);
    }
    let size = u64::from(i.size);
    let ea = super::segments::address(&s.cpu, &i);
    let unit = if i.reg == system::NO_REG {
        ea
    } else {
        let r = s.cpu.gpr[i.reg as usize];
        let offset = match size {
            2 => r as i16 as i64,
            4 => r as i32 as i64,
            _ => r as i64,
        };
        ea.wrapping_add((offset >> (size * 8).trailing_zeros()) as u64 * size)
    };
    let last = unit.wrapping_add(size - 1);
    if s.cpu.alignment_check() && ea & (size - 1) != 0 && paging::canonical(unit) && paging::canonical(last) && last > unit {
        return Err([17, 0, 0]);
    }
    Ok(())
}

/// HLT (committed: RIP is past it): parks in `wfi` until an event can be taken, which the caller then delivers (its
/// frame holds the RIP after the HLT). Each wake acknowledges every pending doorbell (IRQs stay masked at EL1; a pending
/// one still ends `wfi`) and takes this vCPU's invalidations. The host's end request stops the vCPU `halted` here.
pub(super) fn park(s: &mut State) {
    // A stop a doorbell asked for while this vCPU ran to its HLT is served here, at the boundary after the HLT.
    served(s);
    unsafe { write_record(record(), *s) };
    let halted = word(s, core::mem::offset_of!(Events, halted));
    let posts = word(s, core::mem::offset_of!(Events, posts));
    let mut parked = false;
    loop {
        // Doorbells that woke it count as one stop taken at a resident entry (the others merged into it): the host's
        // kickers wait for one, so it is made visible at once.
        let n = acknowledge();
        if n > 0 {
            taken(s);
            (s.stops_resident, s.merged) = (s.stops_resident + 1, s.merged + n - 1);
            unsafe { write_record(record(), *s) };
        }
        take_invalidations(s);
        // X3: the posts seen are counted before the look, so a post after it shows as unseen (senders count after the bit).
        let seen = posts.load(SeqCst);
        lapic::poll(s);
        if next(s).is_some() {
            break;
        }
        // X3: armed, its own LAPIC timer wakes it (the host does not end it then).
        let wakes = lapic::can_wake(s);
        halted.store(if wakes { 2 } else { 1 } | seen << natemu_abi::execution::cache::HALTED_SEEN_SHIFT, SeqCst);
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
        // SAFETY: waits for an interrupt (masked at EL1: it only wakes the PE); no memory effect.
        unsafe { asm!("dsb sy", "wfi") };
    }
    halted.store(0, SeqCst);
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
        any += 1;
        if intid == lapic::TIMER_PPI {
            lapic::timer_irq();
        }
        unsafe { asm!("msr S3_0_C12_C12_1, {i}", "isb", i = in(reg) intid) };
        let c = cache::control();
        c.irqs += 1;
        c.irq_sgis += u64::from(intid < 16);
    }
}
