//! The architectural rules of the interpreter's resident operations (boot-interpreter plan sections 5 and 6;
//! docs/restructure/interp-transitions.md): the descriptor checks of a far JMP, a far RET and a segment load in the
//! interpreted modes, and MOV to CR0 and CR4 with their mode transitions. Pure functions of the bank and the operands
//! (the raw descriptor once the resident has read it): the resident reads and writes memory (the descriptor, its accessed
//! bit) and commits. The order of checks and their results are `tools/models/segment_model.py`'s (`jmp_far_interp`,
//! `ret_far_interp`, `load_interp`) and `tools/models/modes_model.py`'s, with the rules outside 64-bit mode: L is
//! ignored while EFER.LMA = 0 (a code descriptor with L = D = 1 is a 32-bit segment there), and a null SS is #GP(0) at
//! every CPL. The resident's 64-bit functions (`segments::far_target`, `load_data`) keep the 64-bit rules for their own
//! callers.
use crate::cpu_profile::{CR0_VALID, CR4_VALID};
use crate::execution::{
    ATTR_L, ATTR_P, CR0_ET, CR0_PE, CR0_PG, CR4_PAE, Cpu, EFER_LMA, EFER_LME, SREG_CS, SREG_SS,
    Segment,
};

const CR0_NW: u64 = 1 << 29;
const CR0_CD: u64 = 1 << 30;

/// Why an operation does not complete; nothing is committed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Check {
    /// An exception: its vector and error code (#GP, #NP, #SS with a selector's index, TI and EXT bits clear; or 0).
    Fault { vector: u8, error: u16 },
    /// A far JMP through a call gate, or (EFER.LMA = 0) a task gate or a TSS: a task switch. The first version runs
    /// neither (a typed stop).
    Gate,
    /// A far RET to an outer privilege level (the first version has no privilege changes: a typed stop).
    Outer,
    /// A MOV to CR0 that enables paging without EFER.LME: legacy two-level or PAE paging, a typed stop (plan section 11).
    Paging,
}
pub const GP0: Check = Check::Fault {
    vector: 13,
    error: 0,
};
const fn fault(vector: u8, selector: u16) -> Check {
    Check::Fault {
        vector,
        error: selector & 0xfffc,
    }
}
const fn gp(selector: u16) -> Check {
    fault(13, selector)
}

/// A null selector (index 0 in the GDT, any RPL).
pub const fn null(selector: u16) -> bool {
    selector & 0xfffc == 0
}

/// The linear address of the 8-byte descriptor `selector` names: in the LDT for TI = 1 (#GP(selector) with a null
/// LDTR), else the GDT; #GP(selector) when it does not lie within the table's limit. (Not wrapped: the caller applies the
/// mode's linear-address width.)
pub fn entry(cpu: &Cpu, selector: u16) -> Result<u64, Check> {
    let (base, limit) = if selector & 4 != 0 {
        let l = cpu.ldtr;
        if l.attributes & ATTR_P == 0 {
            return Err(gp(selector));
        }
        (l.base, u64::from(l.limit))
    } else {
        (cpu.gdtr.base, cpu.gdtr.limit)
    };
    let index = u64::from(selector & !7);
    if index + 7 > limit {
        return Err(gp(selector));
    }
    Ok(base.wrapping_add(index))
}

/// A descriptor's fields: (S, type, DPL, P, L, D).
pub const fn fields(raw: u64) -> (bool, u64, u64, bool, bool, bool) {
    (
        raw >> 44 & 1 != 0,
        raw >> 40 & 15,
        raw >> 45 & 3,
        raw >> 47 & 1 != 0,
        raw >> 53 & 1 != 0,
        raw >> 54 & 1 != 0,
    )
}

/// `selector` loaded from `raw` with its accessed bit set (the resident sets it in memory when `raw` has it clear).
const fn loaded(selector: u16, raw: u64) -> Segment {
    Segment::loaded(selector, raw | 1 << 40)
}

/// The new CS of a far JMP to non-null `selector`, descriptor `raw`, at `offset` (already cut to the operand size), at the
/// bank's CPL (SDM Vol.2 JMP, protected mode; `segment_model.jmp_far_interp`): a system descriptor is a call gate or
/// (LMA = 0) a task gate or TSS ([`Check::Gate`]), else #GP(selector); a data segment, or with LMA = 1 L = D = 1,
/// #GP(selector); conforming code with DPL > CPL, nonconforming with RPL > CPL or DPL != CPL, #GP(selector); not
/// present #NP(selector); an offset past the new limit #GP(0), except into 64-bit code (LMA and L: no limit). CS gets
/// RPL = CPL.
pub fn jump(cpu: &Cpu, selector: u16, raw: u64, offset: u32) -> Result<Segment, Check> {
    let (code_or_data, kind, dpl, present, l, d) = fields(raw);
    let (lma, cpl) = (cpu.efer & EFER_LMA != 0, cpu.cpl);
    if !code_or_data {
        let gate = kind == 0xc || (!lma && matches!(kind, 1 | 3 | 4 | 5 | 9 | 0xb));
        return Err(if gate { Check::Gate } else { gp(selector) });
    }
    let rpl = u64::from(selector & 3);
    if kind & 8 == 0
        || (lma && l && d)
        || if kind & 4 != 0 {
            dpl > cpl
        } else {
            rpl > cpl || dpl != cpl
        }
    {
        return Err(gp(selector));
    }
    if !present {
        return Err(fault(11, selector));
    }
    let cs = loaded(selector & !3 | cpl as u16, raw);
    if !(lma && l) && offset > cs.limit {
        return Err(GP0);
    }
    Ok(cs)
}

/// The new CS of a far RET to non-null `selector`, descriptor `raw`, at `offset` (SDM Vol.2 RET, protected mode and
/// IA-32e mode, return to the same privilege level; `segment_model.ret_far_interp`): not code, or with LMA = 1 L = D = 1,
/// #GP(selector) (IA-32e mode's order: before the RPL checks); RPL < CPL, conforming code with DPL > RPL, nonconforming
/// with DPL != RPL, #GP(selector); not present #NP(selector); RPL > CPL [`Check::Outer`]; an offset past the new limit
/// #GP(0), except into 64-bit code.
pub fn ret(cpu: &Cpu, selector: u16, raw: u64, offset: u32) -> Result<Segment, Check> {
    let (code_or_data, kind, dpl, present, l, d) = fields(raw);
    let (lma, cpl, rpl) = (cpu.efer & EFER_LMA != 0, cpu.cpl, u64::from(selector & 3));
    if !code_or_data
        || kind & 8 == 0
        || (lma && l && d)
        || rpl < cpl
        || if kind & 4 != 0 { dpl > rpl } else { dpl != rpl }
    {
        return Err(gp(selector));
    }
    if !present {
        return Err(fault(11, selector));
    }
    if rpl > cpl {
        return Err(Check::Outer);
    }
    let cs = loaded(selector, raw);
    if !(lma && l) && offset > cs.limit {
        return Err(GP0);
    }
    Ok(cs)
}

/// MOV `sreg` (not CS) := a null selector (SDM Vol.2 MOV, protected mode): #GP(0) for SS (at every CPL: the 64-bit
/// allowance below CPL 3 is not this mode's), else the unusable segment (selector kept; base, limit and attributes 0).
pub fn null_load(sreg: usize, selector: u16) -> Result<Segment, Check> {
    if sreg == SREG_SS {
        return Err(GP0);
    }
    Ok(Segment {
        selector,
        ..Segment::NULL
    })
}

/// MOV `sreg` (not CS) := non-null `selector`, descriptor `raw` (SDM Vol.2 MOV, protected-mode exceptions; the rules of
/// the resident's `segments::load_data` for a non-null selector): SS needs RPL = CPL, a writable data segment and DPL =
/// CPL (#GP(selector)), present (#SS(selector)); the others data or readable code, with max(CPL, RPL) <= DPL unless
/// conforming code (#GP(selector)), present (#NP(selector)).
pub fn load(cpu: &Cpu, sreg: usize, selector: u16, raw: u64) -> Result<Segment, Check> {
    let (code_or_data, kind, dpl, present, _, _) = fields(raw);
    let (cpl, rpl, code) = (cpu.cpl, u64::from(selector & 3), kind & 8 != 0);
    if sreg == SREG_SS {
        if rpl != cpl || !code_or_data || code || kind & 2 == 0 || dpl != cpl {
            return Err(gp(selector));
        }
        if !present {
            return Err(fault(12, selector));
        }
    } else {
        let conforming = code && kind & 4 != 0;
        if !code_or_data || (code && kind & 2 == 0) || (!conforming && (rpl > dpl || cpl > dpl)) {
            return Err(gp(selector));
        }
        if !present {
            return Err(fault(11, selector));
        }
    }
    Ok(loaded(selector, raw))
}

/// MOV CR0 := `value` outside 64-bit mode (SDM Vol.2 MOV CR; Vol.3 2.5, 12.8.5 "Initializing IA-32e Mode", 12.8.5.4,
/// 12.9; `modes_model`): the new CR0 and EFER. Reserved bits are not written and ET reads 1 (modes_model M3); PG without
/// PE and NW without CD are #GP(0). Setting PG: without EFER.LME [`Check::Paging`]; with it, IA-32e mode is activated
/// (LMA := 1), #GP(0) without CR4.PAE, with CS.L = 1 or with a 16-bit TSS in TR (the Intel profile, modes_model M5).
/// Clearing PG deactivates it (LMA := 0; from compatibility mode: 64-bit mode never interprets, so its #GP is not here).
pub fn cr0(cpu: &Cpu, value: u32) -> Result<(u64, u64), Check> {
    let (old, new) = (cpu.cr0, u64::from(value) & CR0_VALID | CR0_ET);
    if new & CR0_PG != 0 && new & CR0_PE == 0 || new & (CR0_NW | CR0_CD) == CR0_NW {
        return Err(GP0);
    }
    let mut efer = cpu.efer;
    if old & CR0_PG == 0 && new & CR0_PG != 0 {
        if efer & EFER_LME == 0 {
            return Err(Check::Paging);
        }
        if cpu.cr4 & CR4_PAE == 0
            || cpu.seg[SREG_CS].attributes & ATTR_L != 0
            || matches!(cpu.tr.attributes & 15, 1 | 3)
        {
            return Err(GP0);
        }
        efer |= EFER_LMA;
    } else if old & CR0_PG != 0 && new & CR0_PG == 0 {
        efer &= !EFER_LMA;
    }
    Ok((new, efer))
}

/// MOV CR4 := `value` outside 64-bit mode: #GP(0) for a bit the presented CPU does not support (`CR4_VALID`: MCE, LA57,
/// PCIDE and SMAP among them) and for a change of PAE while IA-32e mode is active.
pub fn cr4(cpu: &Cpu, value: u32) -> Result<u64, Check> {
    let v = u64::from(value);
    if v & !CR4_VALID != 0 || (cpu.efer & EFER_LMA != 0 && (v ^ cpu.cr4) & CR4_PAE != 0) {
        return Err(GP0);
    }
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution::{ATTR_D, CR4_LA57, Table};

    const CODE32: u64 = 0x00CF_9B00_0000_FFFF; // flat 32-bit code, accessed
    const CODE64: u64 = 0x00AF_9B00_0000_FFFF;
    const CODE16: u64 = 0x0000_9B00_0000_FFFF;
    const LONG_D: u64 = 0x00EF_9B00_0000_FFFF; // L = D = 1
    const DATA: u64 = 0x00CF_9300_0000_FFFF;

    fn protected(lma: bool) -> Cpu {
        let mut c: Cpu = unsafe { core::mem::zeroed() };
        c.cr0 = CR0_PE | CR0_ET | if lma { CR0_PG } else { 0 };
        c.cr4 = if lma { CR4_PAE } else { 0 };
        c.efer = if lma { EFER_LME | EFER_LMA } else { 0 };
        c.gdtr = Table {
            base: 0x1000,
            limit: 0x37,
        };
        c.seg[SREG_CS] = Segment::loaded(8, CODE32);
        c.tr.attributes = 0x8b;
        c
    }
    /// `raw` with access byte `access` (P DPL S type).
    fn with(raw: u64, access: u64) -> u64 {
        raw & !(0xff << 40) | access << 40
    }

    #[test]
    fn entries_lie_within_their_table() {
        let mut c = protected(false);
        assert_eq!(entry(&c, 0x30), Ok(0x1030));
        assert_eq!(entry(&c, 0x33), Ok(0x1030));
        assert_eq!(entry(&c, 0x38), Err(gp(0x38)));
        assert_eq!(entry(&c, 0x0c), Err(gp(0x0c))); // TI = 1, null LDTR
        c.ldtr = Segment {
            selector: 0,
            attributes: 0x82,
            limit: 0xffff,
            base: 0,
        };
        assert_eq!(entry(&c, 0x0c), Ok(0x08)); // the INIT state's LDTR: base 0
        assert_eq!(entry(&c, 0xfffc), Ok(0xfff8)); // its limit FFFFH
        assert_eq!(entry(&c, 0xfff8), Err(gp(0xfff8)));
    }

    /// Far JMP: segment_model.jmp_far_interp's outcomes, at LMA = 0 and 1.
    #[test]
    fn far_jump_targets() {
        let (p, c) = (protected(false), protected(true));
        let cs = jump(&p, 0x08, with(CODE32, 0x9a), 0x11000).unwrap();
        assert_eq!((cs.selector, cs.attributes & 0xff, cs.limit), (0x08, 0x9b, u32::MAX));
        // L is ignored with LMA = 0: L = D = 1 is 32-bit code, L = 1 D = 0 16-bit code with its limit.
        assert_eq!(jump(&p, 0x08, LONG_D, 0x11000).map(|s| s.attributes & ATTR_D != 0), Ok(true));
        assert_eq!(jump(&c, 0x08, LONG_D, 0x11000), Err(gp(0x08)));
        assert_eq!(jump(&p, 0x28, CODE64, 0x11000).map(|s| s.attributes & ATTR_D), Ok(0)); // 16-bit code
        assert!(jump(&c, 0x28, CODE64, 0xffff_0000).is_ok()); // into 64-bit code: no limit
        let small = CODE64 & !(0xf << 48 | 1 << 55); // L = 1, limit FFFFH, byte granular
        assert_eq!(jump(&p, 0x28, small, 0x1_0000), Err(GP0)); // the limit applies with LMA = 0
        assert!(jump(&c, 0x28, small, 0x1_0000).is_ok());
        assert_eq!(jump(&p, 0x20, CODE16, 0x10000), Err(GP0));
        assert!(jump(&p, 0x20, CODE16, 0xffff).is_ok());
        // System descriptors: gates and TSSs are a task switch or a gate (refused); an LDT is #GP.
        for (kind, legacy, long) in [(0xc, Check::Gate, Check::Gate), (4, Check::Gate, gp(0x30)), (5, Check::Gate, gp(0x30)),
                                     (9, Check::Gate, gp(0x30)), (0xb, Check::Gate, gp(0x30)), (2, gp(0x30), gp(0x30))] {
            let raw = with(CODE32, 0x80 | kind);
            assert_eq!((jump(&p, 0x30, raw, 0), jump(&c, 0x30, raw, 0)), (Err(legacy), Err(long)), "type {kind:#x}");
        }
        assert_eq!(jump(&p, 0x10, DATA, 0), Err(gp(0x10)));
        assert_eq!(jump(&p, 0x0b, CODE32, 0), Err(gp(0x08))); // RPL 3 > CPL
        assert_eq!(jump(&p, 0x08, with(CODE32, 0xfb), 0), Err(gp(0x08))); // DPL 3
        let conforming = jump(&p, 0x0b, with(CODE32, 0x9f), 0).unwrap(); // conforming DPL 0, RPL 3: CS gets RPL 0
        assert_eq!(conforming.selector, 0x08);
        assert_eq!(jump(&p, 0x08, with(CODE32, 0xff), 0), Err(gp(0x08))); // conforming DPL 3 > CPL
        assert_eq!(jump(&p, 0x08, with(CODE32, 0x1b), 0), Err(fault(11, 0x08)));
    }

    /// Far RET: segment_model.ret_far_interp's outcomes.
    #[test]
    fn far_return_targets() {
        let (p, c) = (protected(false), protected(true));
        assert!(ret(&p, 0x08, CODE32, 0x11000).is_ok());
        assert!(ret(&c, 0x28, CODE64, 0x1_0000).is_ok());
        assert_eq!(ret(&c, 0x08, LONG_D, 0), Err(gp(0x08)));
        assert!(ret(&p, 0x08, LONG_D, 0).is_ok());
        assert_eq!(ret(&p, 0x10, DATA, 0), Err(gp(0x10)));
        assert_eq!(ret(&p, 0x0b, with(CODE32, 0xfb), 0), Err(Check::Outer));
        assert_eq!(ret(&p, 0x0b, with(CODE32, 0x9f), 0), Err(Check::Outer)); // conforming, RPL 3
        assert_eq!(ret(&p, 0x08, with(CODE32, 0xfb), 0), Err(gp(0x08))); // DPL 3 != RPL 0
        assert_eq!(ret(&p, 0x08, with(CODE32, 0x1b), 0), Err(fault(11, 0x08)));
        assert_eq!(ret(&p, 0x20, CODE16, 0x1_0000), Err(GP0));
        // A not-present outer return is #NP before the refusal.
        assert_eq!(ret(&p, 0x0b, with(CODE32, 0x7b), 0), Err(fault(11, 0x08)));
    }

    /// MOV Sreg: segment_model.load_interp's outcomes at CPL 0.
    #[test]
    fn segment_loads() {
        let p = protected(false);
        assert_eq!(null_load(SREG_SS, 0), Err(GP0));
        assert_eq!(null_load(3, 2), Ok(Segment { selector: 2, ..Segment::NULL }));
        assert_eq!(load(&p, SREG_SS, 0x10, with(DATA, 0x92)).map(|s| s.attributes & 0xff), Ok(0x93));
        assert_eq!(load(&p, SREG_SS, 0x13, DATA), Err(gp(0x10))); // RPL != CPL
        assert_eq!(load(&p, SREG_SS, 0x10, with(DATA, 0x91)), Err(gp(0x10))); // read-only
        assert_eq!(load(&p, SREG_SS, 0x08, CODE32), Err(gp(0x08)));
        assert_eq!(load(&p, SREG_SS, 0x10, with(DATA, 0xf3)), Err(gp(0x10))); // DPL 3
        assert_eq!(load(&p, SREG_SS, 0x10, with(DATA, 0x13)), Err(fault(12, 0x10)));
        assert!(load(&p, 3, 0x08, CODE32).is_ok()); // readable code
        assert_eq!(load(&p, 3, 0x08, with(CODE32, 0x99)), Err(gp(0x08))); // execute-only
        assert_eq!(load(&p, 3, 0x13, DATA), Err(gp(0x10))); // RPL 3 > DPL 0
        assert!(load(&p, 3, 0x0b, with(CODE32, 0x9f)).is_ok()); // conforming readable code, RPL 3
        assert_eq!(load(&p, 3, 0x10, with(DATA, 0x13)), Err(fault(11, 0x10)));
        assert_eq!(load(&p, 3, 0x10, with(DATA, 0x83)), Err(gp(0x10))); // S = 0
    }

    /// MOV CR0 and CR4: modes_model's #GP conditions and transitions.
    #[test]
    fn control_register_transitions() {
        let mut p = protected(false);
        assert_eq!(cr0(&p, 0x8000_0010), Err(GP0)); // PG without PE
        assert_eq!(cr0(&p, 0x2000_0011), Err(GP0)); // NW without CD
        assert_eq!(cr0(&p, 0x6000_0011), Ok((0x6000_0011, 0)));
        assert_eq!(cr0(&p, 0x0000_0041), Ok((0x11, 0))); // a reserved bit is not written; ET reads 1
        assert_eq!(cr0(&p, 0x10), Ok((0x10, 0))); // PE cleared: real mode
        assert_eq!(cr0(&p, 0x8000_0011), Err(Check::Paging)); // legacy paging
        p.cr4 = CR4_PAE;
        assert_eq!(cr0(&p, 0x8000_0011), Err(Check::Paging)); // PAE paging without LME
        p.efer = EFER_LME;
        assert_eq!(cr0(&p, 0x8000_0011), Ok((0x8000_0011, EFER_LME | EFER_LMA)));
        p.cr4 = 0;
        assert_eq!(cr0(&p, 0x8000_0011), Err(GP0)); // no PAE
        p.cr4 = CR4_PAE;
        p.seg[SREG_CS].attributes |= ATTR_L;
        assert_eq!(cr0(&p, 0x8000_0011), Err(GP0)); // CS.L = 1
        p.seg[SREG_CS].attributes &= !ATTR_L;
        p.tr.attributes = 0x83;
        assert_eq!(cr0(&p, 0x8000_0011), Err(GP0)); // a 16-bit TSS
        let c = protected(true);
        assert_eq!(cr0(&c, 0x11), Ok((0x11, EFER_LME))); // deactivation
        assert_eq!(cr0(&c, 0x8001_0011), Ok((0x8001_0011, EFER_LME | EFER_LMA))); // WP
        assert_eq!(cr0(&c, 0x8000_0010), Err(GP0)); // PE cleared under PG
        // CR4: the profile's features under PG = 0; MCE, LA57 reserved; PAE fixed while IA-32e mode is active.
        let p = protected(false);
        assert_eq!(cr4(&p, 0x15_0eb0), Ok(0x15_0eb0)); // a Linux trampoline's
        assert_eq!(cr4(&p, 0x710), Ok(0x710));
        assert_eq!(cr4(&p, 0x750), Err(GP0)); // MCE
        assert_eq!(cr4(&p, CR4_LA57 as u32 | 0x20), Err(GP0));
        assert_eq!(cr4(&p, 0), Ok(0));
        assert_eq!(cr4(&c, 0), Err(GP0));
        assert_eq!(cr4(&c, 0xa0), Ok(0xa0));
    }
}
