//! Exception and software-interrupt delivery through the 64-bit IDT (plan X1a; SDM Vol.3 7.12-7.15, Vol.2 INT n/INTO/
//! INT3), executed as a resident transaction: every IDT, GDT/LDT and TSS read is an implicit supervisor access through
//! the walker ([`segments::linear`], never a shadow), the frame is written at the new CPL, and an attempt that faults
//! changes no vCPU state (guest memory may hold an accessed bit or frame bytes on a writable page, as the SDM allows).
//! A fault detected while delivering is chained per Table 7-5: a second contributory or page fault becomes #DF, a fault
//! while delivering #DF is a shutdown (`stop::SHUTDOWN`), anything else is delivered serially. `tools/models/
//! delivery_model.py` is the oracle; its check order (A5) and its choices (A1 CR2, A11 error codes) are followed.
//! Gates may name a TSS stack (IST n, or RSPn for a gate to an inner level: CPL 3 -> 0); CPL 1 and 2 stop
//! `unsupported_mode` before any effect.
use super::segments::{self, Exception};
use super::*;

/// What is delivered: an exception raised at the banked state (vector, error code, CR2; the bank is the state at the
/// faulting instruction for faults), a software interrupt (INT n, INT3) of `length` bytes at the banked RIP, or an
/// external event (X2: a maskable interrupt's vector, or 2 for an NMI) taken at the banked instruction boundary;
/// `mid_string` when that boundary is between two iterations of a REP string instruction (its frame holds RF = 1).
#[derive(Clone, Copy)]
pub(super) enum Event {
    Exception(Exception),
    Software { vector: u64, length: u64 },
    External { vector: u64, mid_string: bool },
}

const TF: u64 = 1 << 8;
const IF: u64 = 1 << 9;
const NT: u64 = 1 << 14;
const RF: u64 = 1 << 16;
const VM: u64 = 1 << 17;
/// Vectors with an error code (§7.15): #DF #TS #NP #SS #GP #PF #AC #CP.
const ERROR_CODE: u64 = 1 << 8 | 1 << 10 | 1 << 11 | 1 << 12 | 1 << 13 | 1 << 14 | 1 << 17 | 1 << 21;
/// Fault-class vectors (Table 7-1), whose frame holds RF = 1 (Vol.3 "RF"). #DB counts as a fault: its only source
/// before S4 is general detect (DR7.GD), a fault; S4 adds the trap-class and instruction-breakpoint sources.
const FAULTS: u64 = 1 | 1 << 1 | 1 << 5 | 1 << 6 | 1 << 7 | 1 << 10 | 1 << 11 | 1 << 12 | 1 << 13 | 1 << 14 | 1 << 16 | 1 << 17 | 1 << 19 | 1 << 20 | 1 << 21;
/// Table 7-4 classes.
const CONTRIBUTORY: u64 = 1 | 1 << 10 | 1 << 11 | 1 << 12 | 1 << 13 | 1 << 21;
const PAGE: u64 = 1 << 14 | 1 << 20;

#[derive(Clone, Copy, PartialEq)]
enum Class {
    Benign,
    Contributory,
    Page,
    Double,
}
fn class(e: Event) -> Class {
    match e {
        Event::Software { .. } | Event::External { .. } => Class::Benign,
        Event::Exception([8, ..]) => Class::Double,
        Event::Exception([v, ..]) if CONTRIBUTORY >> v & 1 != 0 => Class::Contributory,
        Event::Exception([v, ..]) if PAGE >> v & 1 != 0 => Class::Page,
        Event::Exception(_) => Class::Benign,
    }
}

/// Delivers `event` from the banked state; on return the bank is at the handler. A shutdown halts. Not inlined: one
/// frame for every caller (the resident's stack bound, tools/check_resident_stack.py).
#[inline(never)]
pub(super) fn deliver(s: &mut State, mut event: Event) {
    if let Event::Exception([14, _, cr2]) = event {
        s.cpu.cr2 = cr2; // §7.15 interrupt 14: CR2 before delivery
        super::page_faulted(s, cr2);
    }
    loop {
        s.deliveries += 1;
        let Err(f) = attempt(s, event) else { return };
        if f[0] == 14 {
            s.cpu.cr2 = f[2]; // a detected #PF writes CR2 whatever follows
            super::page_faulted(s, f[2]);
        }
        let nested = Event::Exception(f);
        event = match (class(event), class(nested)) {
            (Class::Double, Class::Contributory | Class::Page) => {
                s.fault = [f[0], if f[0] == 14 { f[2] } else { 0 }, f[1], s.cpu.rip];
                halt(s, stop::SHUTDOWN)
            }
            (Class::Contributory, Class::Contributory) | (Class::Page, Class::Contributory | Class::Page) => Event::Exception([8, 0, 0]),
            _ => nested,
        };
    }
}

/// One delivery attempt (the delivery model's `_attempt`): the state changes only on success.
#[inline(never)]
fn attempt(s: &mut State, event: Event) -> Result<(), Exception> {
    let c = s.cpu;
    let (vector, software, length) = match event {
        Event::Exception([v, ..]) => (v, false, 0),
        Event::Software { vector, length } => (vector, true, length),
        Event::External { vector, .. } => (vector, false, 0),
    };
    let ext = u64::from(!software); // §7.13 EXT
    let gate_error = vector << 3 | 2 | ext;
    let gp = |e: u64| -> Exception { [13, e, 0] };
    if vector * 16 + 15 > c.idtr.limit {
        return Err(gp(gate_error));
    }
    let mut raw = [0u8; 16];
    segments::linear(s, c.idtr.base.wrapping_add(vector * 16), &mut raw, false, true, gp(gate_error))?;
    let (lo, hi) = (u64::from_le_bytes(raw[..8].try_into().unwrap()), u64::from_le_bytes(raw[8..].try_into().unwrap()));
    let (gtype, dpl, present) = (lo >> 40 & 0x1f, lo >> 45 & 3, lo >> 47 & 1 != 0);
    let (selector, ist) = ((lo >> 16) as u16, lo >> 32 & 7);
    let offset = lo & 0xffff | (lo >> 48 & 0xffff) << 16 | (hi & 0xffff_ffff) << 32;
    if matches!(gtype, 6 | 7) {
        return Err(gp(0)); // 16-bit gates: §7.14.1 (A11; vector * 8 + 2 + EXT also permitted)
    }
    if !matches!(gtype, 0xe | 0xf) {
        return Err(gp(gate_error)); // task gates, other system types, S = 1
    }
    if software && dpl < c.cpl {
        return Err(gp(vector << 3 | 2)); // §7.12.1.2
    }
    if !present {
        return Err([11, gate_error, 0]);
    }
    if selector & 0xfffc == 0 {
        return Err(gp(ext));
    }
    // The CS descriptor (GDT, or the LDT for TI = 1): [`segments::descriptor`]'s #GP(selector) gains EXT.
    let cs_error = u64::from(selector & 0xfffc) | ext;
    let (cs_va, cs_raw) = match segments::descriptor(s, selector, false) {
        Ok((va, raw)) => (va, raw as u64),
        Err([13, e, _]) if e == u64::from(selector & 0xfffc) => return Err(gp(cs_error)),
        Err(e) => return Err(e),
    };
    let (code_or_data, kind, cs_dpl, cs_present) = segments::fields(cs_raw);
    if !code_or_data || kind & 8 == 0 || cs_dpl > c.cpl {
        return Err(gp(cs_error));
    }
    if !cs_present {
        return Err([11, cs_error, 0]);
    }
    if cs_raw >> 53 & 3 != 1 {
        return Err(gp(gate_error)); // not a 64-bit CS (L = 1, D = 0): §7.14.1 (A11; selector + EXT also permitted)
    }
    let new_cpl = if kind & 4 != 0 { c.cpl } else { cs_dpl };
    if matches!(new_cpl, 1 | 2) {
        halt(s, stop::UNSUPPORTED_MODE) // CPL 1 and 2 are not built ([`Cpu::control`])
    }
    // The stack: IST n, or RSPn of the new level when it changes (§7.14.4, §7.14.5), else the current one.
    let rsp = if ist != 0 || new_cpl != c.cpl {
        let slot = if ist != 0 { 0x24 + 8 * (ist - 1) } else { 4 + 8 * new_cpl };
        if slot + 7 > u64::from(c.tr.limit) {
            return Err([10, u64::from(c.tr.selector & 0xfffc) | ext, 0]);
        }
        let mut b = [0u8; 8];
        segments::linear(s, c.tr.base.wrapping_add(slot), &mut b, false, true, gp(0))?;
        let rsp = u64::from_le_bytes(b);
        if !paging::canonical(rsp) {
            return Err([12, ext, 0]);
        }
        rsp
    } else {
        c.gpr[4]
    };
    if !paging::canonical(offset) {
        return Err(gp(ext)); // after the stack checks (A5)
    }
    // The frame (§7.14.2): 16-byte aligned; SS, RSP, RFLAGS, CS, RIP and the error code at decreasing addresses. RF image:
    // 0 for INT n/INT3 (cleared at their start), 1 for faults and for an event between REP iterations (mid-string),
    // else the current RF (Vol.3 "RF", 20.3.1.1).
    let top = rsp & !15;
    let rf = match event {
        Event::Software { .. } => 0,
        Event::External { mid_string, .. } => if mid_string { RF } else { c.rflags & RF },
        Event::Exception(_) if FAULTS >> vector & 1 != 0 => RF,
        Event::Exception(_) => c.rflags & RF,
    };
    let error = match event {
        Event::Exception([v, e, _]) if ERROR_CODE >> v & 1 != 0 => Some(e),
        _ => None,
    };
    let slots = [error.unwrap_or(0), c.rip.wrapping_add(length), u64::from(c.seg[SREG_CS].selector), c.rflags & !RF | rf, c.gpr[4], u64::from(c.seg[SREG_SS].selector)];
    let slots = &slots[usize::from(error.is_none())..];
    let low = top.wrapping_sub(8 * slots.len() as u64);
    if !paging::canonical(top.wrapping_sub(8)) || !paging::canonical(low) || low > top {
        return Err([12, ext, 0]);
    }
    let mut frame = [0u8; 48];
    for (k, v) in slots.iter().enumerate() {
        frame[8 * k..8 * k + 8].copy_from_slice(&v.to_le_bytes());
    }
    // At the new CPL (A3): an implicit-supervisor-like walk at CPL 0, else explicit at CPL 3 (the CPL stays 3).
    segments::linear(s, low, &mut frame[..8 * slots.len()], true, new_cpl == 0, [12, ext, 0])?;
    segments::accessed(s, cs_va, cs_raw)?;
    s.cpu.gpr[4] = low;
    s.cpu.rip = offset;
    s.cpu.seg[SREG_CS] = Segment { base: 0, ..Segment::loaded(selector & 0xfffc | new_cpl as u16, cs_raw | 1 << 40) };
    if new_cpl != c.cpl {
        s.cpu.seg[SREG_SS] = Segment { selector: new_cpl as u16, ..Segment::NULL }; // a null SS with RPL = new CPL
    }
    s.cpu.rflags &= !(TF | NT | RF | VM | if gtype == 0xe { IF } else { 0 }); // §7.12.1.3
    // Both interrupt shadows end, and the boundary is no longer between REP iterations (X2).
    (s.cpu.mov_ss, s.sti_shadow, s.mid_string) = (0, 0, 0);
    s.delivered = [vector, error.unwrap_or(u64::MAX)];
    if vector == 1 && !software {
        s.cpu.dr[5] &= !DR7_GD; // SDM Vol.3 17.2.4: the processor clears GD on entering the #DB handler
    }
    if new_cpl != c.cpl {
        set_cpl(s, new_cpl);
    }
    Ok(())
}
