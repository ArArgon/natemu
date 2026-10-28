//! Structural verification of a slice-1 code region: the isolation and precision checks fexc emits are verified on
//! the native words, every time, instead of being trusted to the producer and its offline verifier
//! (`tools/fexc/verify_precision.py` P8-P11). One linear pass decodes every executable word with a small table of the
//! A64 classes fexc emits (anything else is refused, never guessed) and applies the per-word rules; each access site,
//! check stub and exit is then verified in a short backward window. Rules:
//!
//! - Words: only the decoded classes; `mrs`/`msr` of NZCV only; no `ret`, `bl`, system, exception-generating or SIMD
//!   instruction; x28 is never written; sp is written only by the helper call frame; direct branches go forward to a
//!   decoded word of the code region; no word falls through into a literal slot or past the region; a literal load
//!   names a guest-RIP literal slot.
//! - Memory: base x28 is an unsigned-offset access inside an admitted frame field; the native pointers are loaded
//!   only by the exits (a computed one `str xR, [x28, #rip]; ldr x1, [x28, #DispatcherLoopTop]; br x1`, a literal one
//!   below) and the division helper call (`ldr x3, [x28, #helper]; str x30, [sp, #-16]!; blr x3; ldr x30, [sp], #16`),
//!   the only indirect branches and sp accesses; State.rip is written only whole; `InlineJITBlockHeader` only by the
//!   entry prologue. Every other access is an access site whose record matches the word.
//! - Address guard: every access not addressed by a guest-RIP move is preceded in its instruction by `eor x0, xA, xA,
//!   lsl #16; tbnz x0, #63, G` (or `tbz x0, #63, +8; b G`), G a guard stub linked to it, xA its base or x1 computed as
//!   exactly its address (`add|sub x1, base, #imm`, `add x1, base, index{, ext #s}`, or a mov/movk constant into x1 and
//!   `add x1, base, x1`); base and index unchanged up to the access. A read-modify-write store may share its load's
//!   guard. A guest-RIP-addressed access has its move's value checked (decoded constant = relocation value, and the
//!   value is the instruction end plus a displacement found in its bytes); a non-canonical address needs `b G` right
//!   before the access.
//! - Split: every store wider than one byte has `add x0, xA, #w-1; eor x0, x0, xA; tbnz w0, #12, S` on exactly its
//!   address, only x0/x1 written between it and the store (guest-RIP-addressed: `b S` iff it crosses a page).
//! - Entry: the prologue is exactly `adr x0, header; str x0, [x28]; str xzr, [x28, #INTERRUPT_FAULT_PAGE]`: the only
//!   store to `InlineJITBlockHeader` (the linker trusts it to name the running block) and FEX's suspend/interrupt
//!   fault check (the runtime stops a vCPU by write-protecting the page that store targets, so the stop is precise:
//!   the next block entry faults with the entry state intact). That word is the only access to those 8 bytes.
//! - Exits: every computed State.rip store has `eor x0, xR, xR, lsl #16; tbnz x0, #63, G` on its register in its
//!   instruction (G a branch-target guard) and loads DispatcherLoopTop. A literal one is a link point, exactly `ldr x0,
//!   L; str x0, [x28, #rip]; ldr x1, [x28, #ExitFunctionLinker]; br x1`, L holding the block end or the decoded target
//!   of its branch, and no branch enters its words 2-4 (the linker rewrites word 1 to `b` the target's entry);
//!   ExitFunctionLinker is loaded nowhere else. Admission returns the link points.
//! - Division: each div/idiv (from its x86 opcode: width, signedness) has exactly its per-width #DE checks, in their
//!   exact form, before FEX's divide, reading that divide's own divisor (and, at 8 bits, dividend) registers; the
//!   128/64 helper gets the same divisor and its call is followed by `cbnz x2, D`.
//! - Every stub is reached by exactly one branch, which is one of the checks above, and no branch enters a check's
//!   window (from the check's first word to the access, exit store or divide) from before it.
//!
//! Premise: register values derived from sp, x28 or `adr` (private addresses) may flow anywhere, because every guest
//! access is guarded on its exact address and every private address has bit 47 != bit 63 (non-canonical).
use std::ops::Range;

use natemu_abi::code_object::{self as object, PcEntry, Reloc, Site};
use natemu_abi::execution::{ENTRY_OFFSET, LinkPoint, frame, frame::INTERRUPT_FAULT_PAGE};

/// The 64-bit index forms (LSL, UXTX, SXTX) compute the same address: one extend value.
const EXT_X: u8 = object::EXT_SXTX;
const SP: u8 = 31;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct Addr {
    base: u8,
    /// (register, extend, shift)
    index: Option<(u8, u8, u8)>,
    imm: i64,
}
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Form {
    Unsigned,
    Unscaled,
    Pre,
    Post,
    Register,
}
#[derive(Clone, Copy, Debug)]
struct Mem {
    load: bool,
    size: u8,
    rt: u8,
    form: Form,
    /// The address as it is before the access (post-index: the base).
    addr: Addr,
    /// Writeback amount (pre/post-index).
    delta: i64,
}
#[derive(Clone, Copy, Debug)]
enum Insn {
    /// Register-only instruction writing at most this register (31: sp; a zero-register destination writes nothing).
    Data(Option<u8>),
    Mem(Mem),
    /// `ldr xT, label`, the label's code offset.
    Literal { rt: u8, target: i64 },
    B(i64),
    /// b.cond, cbz/cbnz, tbz/tbnz.
    Cond(i64),
    Br,
    Blr,
    Brk,
    Udf,
}
impl Insn {
    /// Registers written (bit 31: sp).
    fn writes(&self) -> u32 {
        let bit = |r: u8| if r == 31 { 0 } else { 1 << r };
        match *self {
            Insn::Data(Some(r)) => 1 << r,
            Insn::Mem(m) => {
                let wb = if matches!(m.form, Form::Pre | Form::Post) { 1 << m.addr.base } else { 0 };
                wb | if m.load { bit(m.rt) } else { 0 }
            }
            Insn::Literal { rt, .. } => bit(rt),
            Insn::Blr => 1 << 30 | 0b1111, // the helper's contract: x0-x3 change
            _ => 0,
        }
    }
    fn falls_through(&self) -> bool {
        !matches!(self, Insn::B(_) | Insn::Br | Insn::Brk | Insn::Udf)
    }
}

/// The value of a logical-immediate word's bitmask (DecodeBitMasks), `None` if reserved.
fn bitmask(w: u32) -> Option<u64> {
    let (n, immr, imms) = (w >> 22 & 1, w >> 16 & 63, w >> 10 & 63);
    let len = 31 - (n << 6 | (!imms & 63)).leading_zeros();
    let esize = 1u32.checked_shl(len).filter(|&e| e >= 2 && (w >> 31 == 1 || e <= 32))?;
    let (s, r) = (imms & (esize - 1), immr & (esize - 1));
    if s == esize - 1 {
        return None;
    }
    let ones = (1u64 << (s + 1)) - 1;
    let emask = if esize == 64 { u64::MAX } else { (1 << esize) - 1 };
    let elem = (ones >> r | ones << (esize - r) % esize) & emask;
    Some((0..64 / esize).fold(0, |v, i| v | elem << (i * esize)))
}

fn sext(v: u32, bits: u32) -> i64 {
    ((v << (32 - bits)) as i32 >> (32 - bits)) as i64
}

/// The A64 classes fexc emits under profile slice1, or `None`.
fn decode(w: u32, at: u32) -> Option<Insn> {
    use Insn::*;
    let rd = (w & 31) as u8;
    let zr = |r: u8| (r != 31).then_some(r);
    let rel = |imm: i64| at as i64 + imm * 4;
    let sets_flags = w >> 29 & 1 == 1;
    let (sf, n, imm6) = (w >> 31 == 1, w >> 22 & 1, w >> 10 & 63);
    Some(match w {
        _ if w >> 16 == 0 => Udf,
        _ if w & 0xffe0_001f == 0xd420_0000 => Brk,
        _ if w & 0xffff_fc1f == 0xd61f_0000 => Br,
        _ if w & 0xffff_fc1f == 0xd63f_0000 => Blr,
        _ if w & 0xfc00_0000 == 0x1400_0000 => B(rel(sext(w & 0x3ff_ffff, 26))),
        _ if w & 0xff00_0010 == 0x5400_0000 || w & 0x7e00_0000 == 0x3400_0000 => Cond(rel(sext(w >> 5 & 0x7_ffff, 19))),
        _ if w & 0x7e00_0000 == 0x3600_0000 => Cond(rel(sext(w >> 5 & 0x3fff, 14))),
        _ if w & 0xffff_ffe0 == 0xd53b_4200 => Data(zr(rd)), // mrs xT, nzcv
        _ if w & 0xffff_ffe0 == 0xd51b_4200 => Data(None),   // msr nzcv, xT
        0xd500_401f => Data(None),                           // cfinv
        _ if w & 0xffe0_7c10 == 0xba00_0400 => Data(None),   // rmif
        _ if w & 0xffff_bc1f == 0x3a00_080d => Data(None),   // setf8, setf16
        _ if w & 0x9f00_0000 == 0x1000_0000 => Data(zr(rd)), // adr
        // Unallocated encodings (sf = 0 with N = 1 or a shift of 32 or more, reserved shift or option fields) are
        // refused, not left to the CPU.
        // add/sub (immediate, extended register): 31 is sp unless flags are set
        _ if w & 0x1f80_0000 == 0x1100_0000 => Data(if sets_flags { zr(rd) } else { Some(rd) }),
        _ if w & 0x1f20_0000 == 0x0b20_0000 && w >> 22 & 3 == 0 && w >> 10 & 7 <= 4 => Data(if sets_flags { zr(rd) } else { Some(rd) }),
        // logical (immediate): 31 is sp unless ands
        _ if w & 0x1f80_0000 == 0x1200_0000 && (sf || n == 0) => Data(if w >> 29 & 3 == 3 { zr(rd) } else { Some(rd) }),
        _ if w & 0x1f80_0000 == 0x1280_0000 && w >> 29 & 3 != 1 && (sf || w >> 22 & 1 == 0) => Data(zr(rd)), // movn, movz, movk
        _ if w & 0x1f80_0000 == 0x1300_0000 && w >> 29 & 3 != 3 && n == sf as u32 && (sf || w & 0x0020_8000 == 0) => Data(zr(rd)), // sbfm, bfm, ubfm
        _ if w & 0x7fa0_0000 == 0x1380_0000 && n == sf as u32 && (sf || imm6 < 32) => Data(zr(rd)), // extr
        _ if w & 0x1f00_0000 == 0x0a00_0000 && (sf || imm6 < 32) => Data(zr(rd)),                      // logical (shifted register)
        _ if w & 0x1f20_0000 == 0x0b00_0000 && w >> 22 & 3 != 3 && (sf || imm6 < 32) => Data(zr(rd)), // add/sub (shifted register)
        _ if w & 0x1fe0_fc00 == 0x1a00_0000 => Data(zr(rd)),                      // adc, sbc(s)
        _ if w & 0x3fe0_0410 == 0x3a40_0000 => Data(None),                        // ccmp, ccmn
        _ if w & 0x3fe0_0800 == 0x1a80_0000 => Data(zr(rd)),                      // csel, csinc, csinv, csneg
        _ if w & 0x5fe0_0000 == 0x1ac0_0000 && matches!(w >> 10 & 63, 2 | 3 | 8..=11) => Data(zr(rd)), // div, shifts
        _ if w & 0x5fff_0000 == 0x5ac0_0000 && w >> 10 & 63 <= 5 => Data(zr(rd)), // rbit, rev*, clz, cls
        _ if w & 0x7f00_0000 == 0x1b00_0000 && matches!(w >> 21 & 7, 0 | 1 | 2 | 5 | 6) => Data(zr(rd)), // madd..umulh
        _ => return memory(w, at),
    })
}

/// General-register loads and stores: literal, unsigned offset, unscaled, pre/post-index, register offset.
fn memory(w: u32, at: u32) -> Option<Insn> {
    let (rt, rn) = ((w & 31) as u8, (w >> 5 & 31) as u8);
    if w & 0x3b00_0000 == 0x1800_0000 {
        let target = at as i64 + sext(w >> 5 & 0x7_ffff, 19) * 4;
        return (w >> 26 & 1 == 0 && w >> 30 == 1).then_some(Insn::Literal { rt, target });
    }
    if w >> 26 & 1 != 0 {
        return None;
    }
    let size = w >> 30;
    let load = match (size, w >> 22 & 3) {
        (_, 0) => false,
        (_, 1) | (0 | 1, 2 | 3) | (2, 2) => true,
        _ => return None,
    };
    let base = |imm: i64| Addr { base: rn, index: None, imm };
    let simm9 = sext(w >> 12 & 0x1ff, 9);
    let (form, addr, delta) = if w & 0x3b00_0000 == 0x3900_0000 {
        (Form::Unsigned, base(((w >> 10 & 0xfff) << size) as i64), 0)
    } else if w & 0x3b20_0000 == 0x3800_0000 {
        match w >> 10 & 3 {
            0 => (Form::Unscaled, base(simm9), 0),
            1 => (Form::Post, base(0), simm9),
            3 => (Form::Pre, base(simm9), simm9),
            _ => return None,
        }
    } else if w & 0x3b20_0c00 == 0x3820_0800 {
        let ext = match w >> 13 & 7 {
            2 => object::EXT_UXTW,
            6 => object::EXT_SXTW,
            3 | 7 => EXT_X,
            _ => return None,
        };
        let shift = if w >> 12 & 1 == 1 { size as u8 } else { 0 };
        (Form::Register, Addr { base: rn, index: Some(((w >> 16 & 31) as u8, ext, shift)), imm: 0 }, 0)
    } else {
        return None;
    };
    // Writeback with the transfer register as base is CONSTRAINED UNPREDICTABLE.
    if matches!(form, Form::Pre | Form::Post) && rn == rt && rn != SP {
        return None;
    }
    Some(Insn::Mem(Mem { load, size: 1 << size, rt, form, addr, delta }))
}

// Exact words of the fixed sequences.
const LDR_GATE: u32 = 0xf940_0381 | (frame::DISPATCHER_LOOP_TOP / 8) << 10; // ldr x1, [x28, #DispatcherLoopTop]
const LDR_LINKER: u32 = 0xf940_0381 | (frame::EXIT_FUNCTION_LINKER / 8) << 10; // ldr x1, [x28, #ExitFunctionLinker]
const STR_X0_RIP: u32 = 0xf900_0380 | (frame::RIP / 8) << 10; // str x0, [x28, #State.rip]
const BR_X1: u32 = 0xd61f_0020;
const ADR_X0_HEADER: u32 = 0x10ff_ffe0; // adr x0, #-4 (the block's JITCodeHeader, from the entry)
const STR_X0_HEADER: u32 = 0xf900_0380; // str x0, [x28, #BLOCK_HEADER]
const STR_XZR_STOP: u32 = 0xf900_039f | (INTERRUPT_FAULT_PAGE / 8) << 10; // str xzr, [x28, #INTERRUPT_FAULT_PAGE]
/// Native offset of the stop store: `adr x0, header; str x0, [x28]; str xzr, [x28, #stop]` after the 4-byte header.
const STOP_AT: u32 = natemu_abi::execution::STOP_STORE;
const LDR_LUDIV: u32 = 0xf940_0383 | (frame::LUDIV_HANDLER / 8) << 10; // ldr x3, [x28, #LUDIVHandler]
const LDR_LDIV: u32 = 0xf940_0383 | (frame::LDIV_HANDLER / 8) << 10;
const STR_X30_PRE: u32 = 0xf81f_0ffe; // str x30, [sp, #-16]!
const BLR_X3: u32 = 0xd63f_0060;
const LDR_X30_POST: u32 = 0xf841_07fe; // ldr x30, [sp], #16
/// `eor x0, xA, xA, lsl #16`
fn eor_guard(a: u32) -> u32 {
    0xca00_4000 | a << 16 | a << 5
}
/// Normalized check branches (offset field zero): taken when the check fails.
const TBNZ_X0_63: u32 = 0xb7f8_0000;
const TBNZ_W0_12: u32 = 0x3760_0000;
const TBZ_X0_63: u32 = 0xb6f8_0000;
const CBZ_X: u32 = 0xb400_0000;
const CBZ_W: u32 = 0x3400_0000;
const CBNZ_X: u32 = 0xb500_0000;
const CBNZ_W: u32 = 0x3500_0000;

/// How a stub is reached: the words `start..=end` end in its branch; `cond` is the normalized conditional branch
/// (the far form `!cond +8; b stub` normalized to `cond`), `None` for a plain `b`.
#[derive(Clone, Copy)]
struct Check {
    start: u32,
    end: u32,
    cond: Option<u32>,
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
enum DivCheck {
    /// Unsigned 8/16/32: high part >= divisor.
    High,
    Zero,
    Overflow,
    /// After the 128/64 helper: `cbnz x2`.
    Verdict,
}

pub(super) struct Code<'a> {
    /// The code region, from the block start (literal slots resolved).
    pub code: &'a [u8],
    pub literals: &'a [Range<usize>],
    pub accesses: &'a [Site],
    pub stubs: &'a [Site],
    pub pcs: &'a [PcEntry],
    pub relocs: &'a [Reloc],
    /// Guest bytes given to fexc, at `rip`; the block translated `guest_bytes` of them.
    pub source: &'a [u8],
    pub rip: u64,
    pub guest_bytes: u32,
    /// The guest RIPs a literal exit may go to: the block end, and the direct target of the branch ending it.
    pub targets: &'a [u64],
}

pub(super) fn canonical(a: u64) -> bool {
    a >> 47 == 0 || a >> 47 == 0x1ffff
}

struct Verifier<'a> {
    c: &'a Code<'a>,
    insns: Vec<Option<Insn>>,
    /// Direct branches (source, target), sorted by target.
    branches: Vec<(u32, u32)>,
    /// Guest-RIP move sequences: words, register, value.
    moves: Vec<(Range<u32>, u8, u64)>,
    /// Per stub (same order as `c.stubs`): how it is reached, and whether a rule accepted that check.
    checks: Vec<Check>,
    claimed: Vec<bool>,
    /// The literal exits found by `exits`.
    links: Vec<LinkPoint>,
}

/// Verifies the code; returns its link points.
pub(super) fn check(c: &Code) -> Result<Vec<LinkPoint>, String> {
    let mut v = Verifier::new(c)?;
    v.entry()?;
    v.words()?;
    v.sites()?;
    v.exits()?;
    v.divisions()?;
    match v.claimed.iter().position(|&ok| !ok) {
        Some(i) => Err(format!("stub at {:#x} is reached by no check of its kind", c.stubs[i].native_offset)),
        None => Ok(v.links),
    }
}

impl<'a> Verifier<'a> {
    fn new(c: &'a Code<'a>) -> Result<Self, String> {
        let words = c.code.len() / 4;
        let mut insns = vec![None; words];
        for (i, slot) in insns.iter_mut().enumerate().skip(ENTRY_OFFSET as usize / 4) {
            if c.literals.iter().any(|l| l.contains(&(i * 4))) {
                continue;
            }
            let w = u32::from_le_bytes(c.code[i * 4..i * 4 + 4].try_into().unwrap());
            *slot = Some(decode(w, i as u32 * 4).ok_or(format!("unrecognised instruction {w:#010x} at {:#x}", i * 4))?);
        }
        let mut branches: Vec<(u32, u32)> = (insns.iter().enumerate())
            .filter_map(|(i, x)| match x {
                Some(Insn::B(t) | Insn::Cond(t)) => Some((i as u32 * 4, *t)),
                _ => None,
            })
            .map(|(at, t)| match u32::try_from(t) {
                Ok(t) if t > at && insns.get(t as usize / 4).is_some_and(|x| x.is_some()) => Ok((at, t)),
                _ => Err(format!("branch at {at:#x} to {t:#x}: not forward to an instruction of the code region")),
            })
            .collect::<Result<_, _>>()?;
        branches.sort_by_key(|&(_, t)| t);
        let mut v = Verifier { c, insns, branches, moves: Vec::new(), checks: Vec::new(), claimed: vec![false; c.stubs.len()], links: Vec::new() };
        for r in c.relocs.iter().filter(|r| r.kind == object::RELOC_GUEST_RIP_MOVE) {
            let (words, value) = v.move_sequence(r.offset, r.register).ok_or(format!("guest-RIP move at {:#x}", r.offset))?;
            if value != r.value || !v.displacement_ok(r.offset, value) {
                return Err(format!("guest-RIP move at {:#x}: value {value:#x} is not the instruction's RIP-relative address", r.offset));
            }
            v.moves.push((words, r.register, value));
        }
        for s in c.stubs {
            let reached: Vec<u32> = v.branches_into(s.native_offset - 1, s.native_offset).map(|(src, _)| src).collect();
            let [src] = reached[..] else {
                return Err(format!("stub at {:#x} is reached by {} branches", s.native_offset, reached.len()));
            };
            let check = match v.insn(src) {
                Some(Insn::Cond(_)) => Check { start: src, end: src, cond: Some(normalize(v.word(src))) },
                _ if src >= 4 && matches!(v.insn(src - 4), Some(Insn::Cond(t)) if t == src as i64 + 4) && is_cbtb(v.word(src - 4)) => {
                    Check { start: src - 4, end: src, cond: Some(normalize(v.word(src - 4)) ^ 1 << 24) }
                }
                _ => Check { start: src, end: src, cond: None },
            };
            v.checks.push(check);
        }
        Ok(v)
    }

    fn word(&self, at: u32) -> u32 {
        u32::from_le_bytes(self.c.code[at as usize..at as usize + 4].try_into().unwrap())
    }
    fn insn(&self, at: u32) -> Option<Insn> {
        self.insns.get(at as usize / 4).copied().flatten()
    }
    fn branches_into(&self, after: u32, to: u32) -> impl Iterator<Item = (u32, u32)> + '_ {
        let from = self.branches.partition_point(|&(_, t)| t <= after);
        self.branches[from..].iter().copied().take_while(move |&(_, t)| t <= to)
    }
    /// No branch enters `(start, to]` from before the check `start..=end` (the far form's own skip excepted).
    fn sealed(&self, start: u32, end: u32, to: u32) -> bool {
        self.branches_into(start, to).all(|(src, t)| src > end || (src == start && start + 4 == end && t == end + 4))
    }
    fn written(&self, regs: u32, from: u32, to: u32) -> bool {
        (from..to).step_by(4).any(|at| self.insn(at).is_some_and(|i| i.writes() & regs != 0))
    }
    /// The last word in `[from, before)` that writes `reg`.
    fn last_write(&self, reg: u8, from: u32, before: u32) -> Option<u32> {
        (from..before).step_by(4).rev().find(|&at| self.insn(at).is_some_and(|i| i.writes() & 1 << reg != 0))
    }
    /// Guest offset of the instruction holding native offset `at`, and that instruction's native start.
    fn instruction(&self, at: u32) -> Option<(u32, u32)> {
        let i = self.c.pcs.partition_point(|p| p.native_offset <= at);
        let p = self.c.pcs.get(i.checked_sub(1)?)?;
        Some((p.guest_offset, p.native_offset))
    }
    /// The guest bytes of the instruction at guest offset `g`.
    fn guest_insn(&self, g: u32) -> &[u8] {
        let end = (self.c.pcs.iter().map(|p| p.guest_offset).filter(|&o| o > g).min()).unwrap_or(self.c.guest_bytes);
        &self.c.source[g as usize..end as usize]
    }
    /// A cbz/cbnz/tbz/tbnz at `at` (or the far form `inverse +8; b target`), normalized, and its target.
    fn cond_branch(&self, at: u32) -> Option<(u32, i64)> {
        let Some(Insn::Cond(t)) = self.insn(at) else { return None };
        let w = self.word(at);
        if !is_cbtb(w) {
            return None;
        }
        match self.insn(at + 4) {
            Some(Insn::B(far)) if t == at as i64 + 8 => Some((normalize(w) ^ 1 << 24, far)),
            _ => Some((normalize(w), t)),
        }
    }
    fn rip_store(&self, at: u32) -> bool {
        matches!(self.insn(at), Some(Insn::Mem(m)) if !m.load && m.addr.base == 28 && m.addr.imm == frame::RIP as i64)
    }
    /// The four words at `at` are a link point (`ldr x0, L; str x0, [x28, #rip]; ldr x1, [x28, #linker]; br x1`) that
    /// no branch enters after its first word; `L`'s offset.
    fn link_point(&self, at: u32) -> Option<i64> {
        let Some(Insn::Literal { rt: 0, target }) = self.insn(at) else { return None };
        let tail = at as usize + 16 <= self.c.code.len() && [self.word(at + 4), self.word(at + 8), self.word(at + 12)] == [STR_X0_RIP, LDR_LINKER, BR_X1];
        (tail && self.sealed(at, at, at + 12)).then_some(target)
    }
    fn helper_at(&self, at: u32) -> bool {
        at as usize + 16 <= self.c.code.len()
            && [LDR_LUDIV, LDR_LDIV].contains(&self.word(at))
            && [self.word(at + 4), self.word(at + 8), self.word(at + 12)] == [STR_X30_PRE, BLR_X3, LDR_X30_POST]
    }

    /// The movz/movn/`orr #bitmask, wzr` (+ movk) sequence at `at` building `reg`: its words and value.
    fn move_sequence(&self, at: u32, reg: u8) -> Option<(Range<u32>, u64)> {
        let w = self.insn(at).and(Some(self.word(at)))?;
        let sf = w >> 31;
        let mask = if sf == 1 { u64::MAX } else { 0xffff_ffff };
        let part = |w: u32| ((w >> 5 & 0xffff) as u64, 16 * (w >> 21 & 3));
        let (imm, sh) = part(w);
        let mut value = match w & 0x7f80_0000 {
            _ if w & 31 != reg as u32 => return None,
            0x1280_0000 => !(imm << sh) & mask, // movn
            0x5280_0000 => imm << sh,           // movz
            0x3200_0000 if w >> 5 & 31 == 31 => bitmask(w)? & mask, // orr xD, xzr, #bitmask
            _ => return None,
        };
        let mut end = at + 4;
        while self.insn(end).is_some() && self.word(end) & 0xff80_001f == (0x7280_0000 | sf << 31 | reg as u32) {
            let (imm, sh) = part(self.word(end));
            value = (value & !(0xffff << sh) | imm << sh) & mask;
            end += 4;
        }
        Some((at..end, value))
    }
    /// A guest-RIP move's value is its instruction's end (a pushed return address) or the end plus a 32-bit
    /// displacement whose bytes are in the instruction.
    fn displacement_ok(&self, at: u32, value: u64) -> bool {
        let Some((g, _)) = self.instruction(at) else { return false };
        let insn = self.guest_insn(g);
        let disp = value.wrapping_sub(self.c.rip.wrapping_add(g as u64 + insn.len() as u64)) as i64;
        disp == 0 || i32::try_from(disp).is_ok_and(|d| insn.windows(4).any(|b| b == d.to_le_bytes()))
    }

    /// Every block begins with the header store and the stop store (no branch can enter the prologue: branches go
    /// forward, and `new` requires each target to be a decoded word after its source).
    fn entry(&self) -> Result<(), String> {
        let word = |at: u32| (at as usize + 4 <= self.c.code.len()).then(|| self.word(at));
        if [word(ENTRY_OFFSET), word(STOP_AT - 4), word(STOP_AT)] == [Some(ADR_X0_HEADER), Some(STR_X0_HEADER), Some(STR_XZR_STOP)] {
            Ok(())
        } else {
            Err(format!("entry prologue at {ENTRY_OFFSET:#x}: not `adr x0, header; str x0, [x28]; str xzr, [x28, #{INTERRUPT_FAULT_PAGE}]`"))
        }
    }

    /// The per-word rules of the linear pass.
    fn words(&self) -> Result<(), String> {
        let fail = |at: u32, what: &str| Err(format!("{what} at {at:#x}"));
        for at in (ENTRY_OFFSET..self.c.code.len() as u32).step_by(4) {
            let Some(i) = self.insn(at) else { continue };
            let w = self.word(at);
            let frame_word = matches!(w, STR_X30_PRE if at >= 4 && self.helper_at(at - 4))
                || matches!(w, LDR_X30_POST if at >= 12 && self.helper_at(at - 12));
            if i.writes() & 1 << 28 != 0 {
                return fail(at, "write to x28");
            }
            if i.writes() & 1 << 31 != 0 && !frame_word {
                return fail(at, "write to sp");
            }
            match i {
                Insn::Mem(m) => match m.addr.base {
                    28 => self.frame_access(m, at)?,
                    SP if !frame_word => return fail(at, "stack access"),
                    SP => {}
                    _ => self.site_word(m, at)?,
                },
                Insn::Literal { target, .. } if !self.c.literals.iter().any(|l| l.start as i64 == target && l.len() == 8) => {
                    return fail(at, "literal load of a non-literal");
                }
                // The exits' gates: a computed exit's, right after its State.rip store, or a link point's last word.
                Insn::Br
                    if !(w == BR_X1
                        && ((self.word(at - 4) == LDR_GATE && self.rip_store(at.wrapping_sub(8)) && self.sealed(at - 8, at - 8, at))
                            || (at >= 12 && self.link_point(at - 12).is_some()))) =>
                {
                    return fail(at, "indirect branch");
                }
                Insn::Blr if !(at >= 8 && self.helper_at(at - 8)) => return fail(at, "indirect branch"),
                Insn::Brk if self.c.stubs.binary_search_by_key(&at, |s| s.native_offset).is_err() => return fail(at, "brk"),
                _ => {}
            }
            if i.falls_through() && self.insn(at + 4).is_none() {
                return fail(at, "fall-through into data");
            }
        }
        for s in self.c.stubs {
            if self.insn(s.native_offset - 4).is_some_and(|i| i.falls_through()) {
                return fail(s.native_offset, "fall-through into a stub");
            }
        }
        Ok(())
    }

    /// x28 accesses: unsigned offset inside one admitted field; native pointers only loaded by the exit gate and the
    /// helper call; State.rip only written whole (checked by `exits`).
    fn frame_access(&self, m: Mem, at: u32) -> Result<(), String> {
        // i64: an unscaled access can be at a negative offset (`stur x0, [x28, #-4]`), outside every field.
        let (offset, end) = (m.addr.imm, m.addr.imm + m.size as i64);
        let overlaps = |field: u32, len: u32| offset < (field + len) as i64 && (field as i64) < end;
        let w = self.word(at);
        if overlaps(INTERRUPT_FAULT_PAGE, 8) {
            // The stop store, only as the block's third prologue word (`entry` checks it is there).
            return if at == STOP_AT && w == STR_XZR_STOP {
                Ok(())
            } else {
                Err(format!("frame access {w:#010x} at {at:#x}: the interrupt-fault word is accessed only by the block entry's stop store"))
            };
        }
        if overlaps(frame::BLOCK_HEADER, 8) {
            // The header store, only as the block's second prologue word (`entry` checks the prologue).
            return if at == STOP_AT - 4 && w == STR_X0_HEADER {
                Ok(())
            } else {
                Err(format!("frame access {w:#010x} at {at:#x}: the block header is accessed only by the entry prologue's store"))
            };
        }
        if overlaps(frame::EXIT_FUNCTION_LINKER, 8) {
            return if w == LDR_LINKER && at >= 8 && self.link_point(at - 8).is_some() {
                Ok(())
            } else {
                Err(format!("frame access {w:#010x} at {at:#x}: ExitFunctionLinker is loaded only as the third word of a link point"))
            };
        }
        let ok = m.form == Form::Unsigned
            && frame::FIELDS.iter().any(|&(field, len)| offset >= field as i64 && end <= (field + len) as i64)
            && (!frame::POINTERS.iter().any(|&p| overlaps(p, 8))
                || (w == LDR_GATE && self.word(at + 4) == BR_X1)
                || ((w == LDR_LUDIV || w == LDR_LDIV) && self.helper_at(at)))
            && (m.load || !overlaps(frame::RIP, 8) || (offset == frame::RIP as i64 && m.size == 8));
        if ok { Ok(()) } else { Err(format!("frame access {w:#010x} at {at:#x}")) }
    }

    /// A guest access word must be an access site recording exactly this access.
    fn site_word(&self, m: Mem, at: u32) -> Result<(), String> {
        let Ok(i) = self.c.accesses.binary_search_by_key(&at, |s| s.native_offset) else {
            return Err(format!("unrecorded memory access at {at:#x}"));
        };
        let s = &self.c.accesses[i];
        let (wb, delta) = match m.form {
            Form::Pre => (object::WB_PRE, m.delta),
            Form::Post => (object::WB_POST, m.delta),
            _ => (object::WB_NONE, 0),
        };
        let index = match m.addr.index {
            None => s.index_reg == object::NO_REG,
            Some((r, ext, shift)) => (s.index_reg, s.shift) == (r, shift) && (s.extend == ext || (ext == EXT_X && s.extend == object::EXT_NONE)),
        };
        let load = matches!(s.kind, object::kind::LOAD | object::kind::POP);
        if !(index
            && load == m.load
            && (s.base_reg, s.width, s.data_reg[0]) == (m.addr.base, m.size, m.rt)
            && s.imm as i64 == m.addr.imm
            && (s.writeback, s.writeback_delta as i64) == (wb, delta))
        {
            return Err(format!("access site at {at:#x} does not describe its instruction"));
        }
        Ok(())
    }

    fn claim(&mut self, stub: u32) {
        if let Ok(i) = self.c.stubs.binary_search_by_key(&stub, |s| s.native_offset) {
            self.claimed[i] = true;
        }
    }
    /// The stubs linked to the access at `at`, of `kind`.
    // ponytail: linear scan per access (also `exits`' stub scan and `guest_insn`'s PC-table scan): quadratic in the
    // block, ~2 us per block on the corpora; index stubs by guest_offset2 if blocks grow to thousands of sites.
    fn linked(&self, at: u32, kind: u8) -> Vec<usize> {
        (0..self.c.stubs.len()).filter(|&i| self.c.stubs[i].guest_offset2 == at && self.c.stubs[i].kind == kind).collect()
    }

    /// The address x1 holds after the word at `def` (one of the guard's address forms) and that form's first word.
    fn x1_address(&self, def: u32, from: u32) -> Option<(Addr, u32)> {
        let w = self.word(def);
        let (rn, rm) = ((w >> 5 & 31) as u8, (w >> 16 & 31) as u8);
        if w & 31 != 1 || w >> 31 == 0 {
            return None;
        }
        let shift = (w >> 10 & 7) as u8;
        let (addr, start) = if w & 0xff80_0000 == 0x9100_0000 || w & 0xff80_0000 == 0xd100_0000 {
            let imm = ((w >> 10 & 0xfff) << (12 * (w >> 22 & 1))) as i64;
            (Addr { base: rn, index: None, imm: if w >> 30 & 1 == 1 { -imm } else { imm } }, def)
        } else if w & 0xffe0_0000 == 0x8b20_0000 && w >> 10 & 7 <= 4 {
            let ext = match w >> 13 & 7 {
                2 => object::EXT_UXTW,
                6 => object::EXT_SXTW,
                3 | 7 => EXT_X,
                _ => return None,
            };
            (Addr { base: rn, index: Some((rm, ext, shift)), imm: 0 }, def)
        } else if w & 0xffe0_0000 == 0x8b00_0000 && w >> 10 & 63 <= 4 {
            (Addr { base: rn, index: Some((rm, EXT_X, (w >> 10 & 63) as u8)), imm: 0 }, def)
        } else {
            return None;
        };
        if addr.index.is_some_and(|(r, ext, s)| r == 1 && ext == EXT_X && s == 0) {
            // `add x1, base, x1`: x1 is a constant built right before.
            let first = (from..def).step_by(4).rev().take_while(|&a| self.word(a) & 0x1f80_001f == 0x1280_0001).last()?;
            let (words, value) = self.move_sequence(first, 1)?;
            return (words.end == def).then_some((Addr { base: rn, index: None, imm: value as i64 }, first));
        }
        Some((addr, start))
    }
    /// The address register `a` holds at `at` (x1: by its last write in `[from, at)`), and where that holds from.
    fn address_in(&self, a: u8, from: u32, at: u32) -> Option<(Addr, u32)> {
        if a != 1 {
            return Some((Addr { base: a, index: None, imm: 0 }, at));
        }
        self.x1_address(self.last_write(1, from, at)?, from)
    }
    /// The address `addr` is unchanged from `from` to the access at `at`.
    fn kept(&self, addr: Addr, from: u32, at: u32) -> bool {
        let regs = 1 << addr.base | addr.index.map_or(0, |(r, _, _)| 1 << r);
        !self.written(regs, from, at)
    }

    /// Guard, split and guest-RIP rules of every access site.
    fn sites(&mut self) -> Result<(), String> {
        let mut guarded: Vec<(u32, u32, u32)> = Vec::new(); // access, window start, check end
        for s in self.c.accesses {
            let at = s.native_offset;
            let fail = |what: &str| Err(format!("access at {at:#x}: {what}"));
            let lo = s.insn_native_offset;
            let entry = self.c.pcs.iter().any(|p| (p.native_offset, p.guest_offset) == (lo, s.guest_offset));
            if !entry || self.c.pcs.iter().any(|p| lo < p.native_offset && p.native_offset <= at && p.guest_offset != s.guest_offset) {
                return fail("not inside its guest instruction");
            }
            let Some(Insn::Mem(m)) = self.insn(at) else { return fail("no memory access there") };
            let addr = m.addr;
            let store = !m.load;
            let (guards, splits) = (self.linked(at, object::kind::GUARD), self.linked(at, object::kind::SPLIT));
            // Guest-RIP-addressed: the base was last written by a guest-RIP move.
            let mv = (addr.index.is_none())
                .then(|| self.last_write(addr.base, lo, at))
                .flatten()
                .and_then(|w| self.moves.iter().find(|(words, r, _)| *r == addr.base && words.contains(&w)).cloned());
            if let Some((words, _, value)) = mv {
                let a = value.wrapping_add(addr.imm as u64);
                let crosses = store && m.size > 1 && (a ^ a.wrapping_add(m.size as u64 - 1)) & 0x1000 != 0;
                let need = if !canonical(a) { Some((object::kind::GUARD, &guards)) } else if crosses { Some((object::kind::SPLIT, &splits)) } else { None };
                let stubs = guards.len() + splits.len();
                // With a `b stub`, nothing may enter between the move and the access, not even from before the `b`.
                let mut sealed_to = words.start;
                match need {
                    None if stubs == 0 => {}
                    Some((_, list)) if stubs == 1 && list.len() == 1 => {
                        let (i, st) = (list[0], &self.c.stubs[list[0]]);
                        let c = self.checks[i];
                        if c.cond.is_some() || c.start + 4 != at || (st.base_reg, st.imm as i64) != (addr.base, addr.imm) {
                            return fail("guest-RIP address needs `b stub` right before the access");
                        }
                        let stub = st.native_offset;
                        sealed_to = c.start;
                        self.claim(stub);
                    }
                    _ => return fail(&format!("guest-RIP address {a:#x} has the wrong stubs")),
                }
                if !self.sealed(words.start, sealed_to, at) {
                    return fail("a branch enters between its guest-RIP move and the access");
                }
                continue;
            }
            // Address guard.
            match guards[..] {
                [g] => {
                    let (st, c) = (self.c.stubs[g], self.checks[g]);
                    let a = st.base_reg;
                    let eor = c.start.wrapping_sub(4);
                    let ok = c.cond == Some(TBNZ_X0_63)
                        && st.imm == 0
                        && eor >= lo
                        && c.end < at
                        && self.word(eor) == eor_guard(a as u32)
                        && self.address_in(a, lo, eor).is_some_and(|(x, from)| {
                            x == addr && self.kept(addr, from, at) && self.sealed(from, c.end, at) && {
                                guarded.push((at, from, c.end));
                                true
                            }
                        });
                    if !ok {
                        return fail("no address guard `eor x0, xA, xA, lsl #16; tbnz x0, #63` on exactly its address");
                    }
                    self.claim(st.native_offset);
                }
                // A read-modify-write store shares its load's guard.
                [] if s.rmw == object::RMW_STORE => {
                    let load = self.c.accesses.iter().find(|l| l.guest_offset == s.guest_offset && l.rmw == object::RMW_LOAD);
                    let shared = load.and_then(|l| guarded.iter().find(|g| g.0 == l.native_offset)).filter(|&&(l, from, end)| {
                        matches!(self.insn(l), Some(Insn::Mem(lm)) if lm.addr == addr) && self.kept(addr, from, at) && self.sealed(from, end, at)
                    });
                    if shared.is_none() {
                        return fail("read-modify-write store without a guard, and its load's guard does not cover it");
                    }
                }
                _ => return fail(&format!("{} guard stubs", guards.len())),
            }
            // Split check on every store wider than a byte.
            match (store && m.size > 1, &splits[..]) {
                (false, []) => {}
                (true, &[x]) => {
                    let (st, c) = (self.c.stubs[x], self.checks[x]);
                    let a = st.base_reg as u32;
                    let add = c.start.wrapping_sub(8);
                    let ok = c.cond == Some(TBNZ_W0_12)
                        && st.imm == 0
                        && add >= lo
                        && c.end < at
                        && self.word(add) == 0x9100_0000 | (m.size as u32 - 1) << 10 | a << 5
                        && self.word(add + 4) == 0xca00_0000 | a << 16
                        && self.address_in(st.base_reg, lo, add).is_some_and(|(x, from)| {
                            x == addr && self.kept(addr, from, at) && self.sealed(from.min(add), c.end, at)
                        })
                        && (c.end + 4..at).step_by(4).all(|o| matches!(self.insn(o), Some(Insn::Data(d)) if d.is_none_or(|r| r < 2)));
                    if !ok {
                        return fail("no split check `add x0, xA, #w-1; eor x0, x0, xA; tbnz w0, #12` on exactly its address, then only x0/x1 written");
                    }
                    self.claim(st.native_offset);
                }
                _ => return fail(&format!("{} split stubs for a {}-byte {}", splits.len(), m.size, if store { "store" } else { "load" })),
            }
        }
        Ok(())
    }

    /// Every State.rip store: a link point to a proven RIP, or a register checked canonical by a branch-target guard.
    fn exits(&mut self) -> Result<(), String> {
        for at in (ENTRY_OFFSET..self.c.code.len() as u32).step_by(4) {
            let Some(Insn::Mem(m)) = self.insn(at) else { continue };
            if m.load || m.addr.base != 28 || m.addr.imm != frame::RIP as i64 {
                continue;
            }
            let r = m.rt;
            if matches!(self.insn(at - 4), Some(Insn::Literal { rt, .. }) if rt == r) {
                let Some(literal) = self.link_point(at - 4) else {
                    return Err(format!("exit at {at:#x}: a literal exit is not exactly a link point"));
                };
                let target = u64::from_le_bytes(self.c.code[literal as usize..][..8].try_into().unwrap());
                if !self.c.targets.contains(&target) {
                    return Err(format!("exit at {at:#x}: literal {target:#x} is neither the block end nor its branch target"));
                }
                self.links.push(LinkPoint { offset: at - 4, reserved: 0, target });
                continue;
            }
            let Some((g, lo)) = self.instruction(at) else { return Err(format!("exit at {at:#x} outside the PC table")) };
            let found = (0..self.c.stubs.len()).find(|&i| {
                let (st, c) = (&self.c.stubs[i], self.checks[i]);
                let eor = c.start.wrapping_sub(4);
                st.kind == object::kind::GUARD
                    && st.guest_offset2 == object::NONE32
                    && st.guest_offset == g
                    && c.cond == Some(TBNZ_X0_63)
                    && eor >= lo
                    && c.end < at
                    && self.word(eor) == eor_guard(st.base_reg as u32)
                    && self.holds(st.base_reg, c.end + 4, at, r)
                    && self.sealed(eor, c.end, at)
            });
            match found {
                Some(i) => self.claimed[i] = true,
                None => return Err(format!("exit at {at:#x}: x{r} is not checked by a branch-target guard in its instruction")),
            }
        }
        Ok(())
    }

    /// `reg`'s value reaches register `r` at `at` from `from`, through `mov` copies only.
    fn holds(&self, reg: u8, from: u32, at: u32, r: u8) -> bool {
        let mut alias = 1u32 << reg;
        for o in (from..at).step_by(4) {
            let w = self.word(o);
            let copy = w & 0xffe0_ffe0 == 0xaa00_03e0 && alias & 1 << (w >> 16 & 31) != 0; // mov xD, xS
            alias &= !self.insn(o).map_or(0, |i| i.writes());
            if copy {
                alias |= 1 << (w & 31);
            }
        }
        alias & 1 << r != 0
    }

    /// The #DE check ending in the check `c` of a div/idiv of `width` bits: its kind, first word, divisor register
    /// (high and zero checks) and the dividend register it reads (8-bit forms).
    fn div_check(&self, c: Check, signed: bool, width: u32) -> Option<(DivCheck, u32, u32, u32)> {
        let cond = c.cond?;
        let before = |k: u32| (c.start >= 4 * k).then(|| self.word(c.start - 4 * k));
        let regs = |r: u32| r >= 4 && r != 28 && r < 31; // not FEX's scratch x0-x3
        let matches = |words: &[u32]| (1..=words.len() as u32).all(|k| before(k) == Some(words[words.len() - k as usize]));
        let start = |k: u32| c.start - 4 * k;
        use DivCheck::*;
        if width == 64 && cond & !31 == CBZ_X && regs(cond & 31) && cond != CBZ_X | 1 {
            return Some((Zero, c.start, cond & 31, 0));
        }
        if !signed {
            let (h, d) = (if width == 8 { before(2)? >> 5 & 31 } else { 0 }, before(1)? >> 16 & 31);
            let form: [u32; 2] = match width {
                8 => [0xd348_3c00 | h << 5, 0xcb20_0000 | d << 16], // ubfx x0, xH, #8, #8; sub x0, x0, wD, uxtb
                16 => [0x5300_3ca0, 0xcb20_2000 | d << 16],         // uxth w0, w5; sub x0, x0, wD, uxth
                32 => [0x2a05_03e0, 0xcb20_4000 | d << 16],         // mov w0, w5; sub x0, x0, wD, uxtw
                _ => return None,
            };
            return (cond == TBZ_X0_63 && (width != 8 || regs(h)) && regs(d) && matches(&form)).then_some((High, start(2), d, h));
        }
        if cond == CBNZ_X | 2 {
            let ok = width == 64 && c.start >= 16 && self.helper_at(c.start - 16) && self.word(c.start - 16) == LDR_LDIV;
            return ok.then_some((Verdict, start(4), 2, 0));
        }
        let d = before(1)? >> 5 & 31;
        let zero = match width {
            8 => Some([0x1300_1c01 | d << 5, CBZ_W | 1]),  // sxtb w1, wD; cbz w1
            16 => Some([0x1300_3c01 | d << 5, CBZ_W | 1]), // sxth w1, wD; cbz w1
            32 => Some([0x9340_7c01 | d << 5, CBZ_X | 1]), // sxtw x1, wD; cbz x1
            _ => None,
        };
        if zero.is_some_and(|[sxt, cbz]| regs(d) && cond == cbz && before(1) == Some(sxt)) {
            return Some((Zero, start(1), d, 0));
        }
        let n = before(3)? >> 5 & 31;
        let overflow: &[u32] = match width {
            8 => &[0x1300_3c00 | n << 5, 0x1ac1_0c00, 0x4b20_8000, CBNZ_W], // sxth w0, wN; sdiv w0, w0, w1; sub w0, w0, w0, sxtb
            16 => &[0x5300_3c80, 0x3310_3ca0, 0x1ac1_0c00, 0x4b20_a000, CBNZ_W], // uxth w0, w4; bfi w0, w5, #16, #16; sdiv; sxth
            32 => &[0x2a04_03e0, 0xb360_7ca0, 0x9ac1_0c00, 0xcb20_c000, CBNZ_X], // mov w0, w4; bfi x0, x5, #32, #32; sdiv; sxtw
            // eor x0, x4, #1 << 63; add x1, x5, #1; orr x0, x0, x1; add x1, xD, #1; orr x0, x0, x1; cbz x0
            _ => {
                let d = before(2)? >> 5 & 31;
                return (regs(d) && cond == CBZ_X && matches(&[0xd241_0080, 0x9100_04a1, 0xaa01_0000, 0x9100_0401 | d << 5, 0xaa01_0000]))
                    .then_some((Overflow, start(5), d, 0));
            }
        };
        let (words, last) = overflow.split_at(overflow.len() - 1);
        (cond == last[0] && (width != 8 || regs(n)) && matches(words)).then_some((Overflow, start(words.len() as u32), 0, n))
    }

    /// Every div/idiv has exactly its #DE checks before FEX's divide; every divide stub is one of them.
    fn divisions(&mut self) -> Result<(), String> {
        let mut found: Vec<(u32, DivCheck, u32, u32, u32, u32)> = Vec::new(); // guest offset, kind, start, end, divisor, dividend
        for i in 0..self.c.stubs.len() {
            let (st, c) = (self.c.stubs[i], self.checks[i]);
            if st.kind != object::kind::DIVIDE {
                continue;
            }
            let at = st.native_offset;
            let insn = self.instruction(c.start).filter(|&(g, lo)| g == st.guest_offset && c.start >= lo);
            let form = insn.and_then(|(g, _)| x86_divide(self.guest_insn(g))).and_then(|(signed, width)| {
                // The verdict's helper is LUDIV for div: the unsigned form shares the zero check and verdict.
                let unsigned_verdict = !signed && width == 64 && c.cond == Some(CBNZ_X | 2);
                if unsigned_verdict {
                    let ok = c.start >= 16 && self.helper_at(c.start - 16) && self.word(c.start - 16) == LDR_LUDIV;
                    return ok.then_some((DivCheck::Verdict, c.start - 16, 2, 0));
                }
                self.div_check(c, signed, width)
            });
            let Some((kind, start, d, h)) = form else {
                return Err(format!("divide stub at {at:#x}: its branch is not an exact #DE check of its div/idiv"));
            };
            found.push((st.guest_offset, kind, start, c.end, d, h));
            self.claimed[i] = true;
        }
        let mut guest: Vec<u32> = self.c.pcs.iter().map(|p| p.guest_offset).filter(|&g| g < self.c.guest_bytes).collect();
        guest.dedup();
        for g in guest {
            let Some((signed, width)) = x86_divide(self.guest_insn(g)) else { continue };
            let fail = |what: &str| Err(format!("div/idiv at guest offset {g:#x}: {what}"));
            let mut mine: Vec<_> = found.iter().filter(|f| f.0 == g).map(|&(_, k, s, e, d, h)| (k, s, e, d, h)).collect();
            mine.sort();
            use DivCheck::*;
            let want: &[DivCheck] = match (signed, width) {
                (false, 64) => &[Zero, Verdict],
                (false, _) => &[High],
                (true, 64) => &[Zero, Overflow, Verdict],
                (true, _) => &[Zero, Overflow],
            };
            if mine.iter().map(|m| m.0).collect::<Vec<_>>() != want {
                return fail("not exactly its #DE checks");
            }
            // The overflow check follows the zero check (it divides by the zero check's x1, or tests its divisor).
            if signed && (mine[1].1 != mine[0].2 + 4 || (width == 64 && mine[1].3 != mine[0].3)) {
                return fail("overflow check does not follow its zero check");
            }
            // FEX's divide: a udiv/sdiv into another register than the check's scratch x0, or the helper call.
            let lo = self.c.pcs.iter().find(|p| p.guest_offset == g).map_or(0, |p| p.native_offset);
            let hi = self.c.pcs.iter().find(|p| p.guest_offset > g).map_or(self.c.code.len() as u32, |p| p.native_offset);
            let divide = (lo..hi).step_by(4).find(|&o| {
                let w = self.word(o);
                self.insn(o).is_some() && ((w & 0x7fe0_f800 == 0x1ac0_0800 && w & 31 != 0) || w == BLR_X3)
            });
            let Some(divide) = divide else { return fail("no divide") };
            let fex = (lo..hi).step_by(4).find(|&o| self.insn(o).is_some() && self.word(o) & 0x7fe0_f800 == 0x1ac0_0800 && self.word(o) & 31 != 0);
            let Some(fex) = fex else { return fail("no inline divide") };
            for &(kind, start, end, _, _) in &mine {
                let to = if kind == Verdict { end } else { divide.max(fex) };
                if (kind != Verdict && end >= divide) || !self.sealed(start, end, to) || start < lo {
                    return fail("a #DE check is not before its divide, or a branch enters it");
                }
            }
            // The checks read FEX's own operands: its udiv/sdiv's divisor (directly or through FEX's x1 copy), at 8
            // bits its dividend, and the helper's divisor in x2; the divisor is unchanged from the checks to the divide.
            let (d, h) = (mine[0].3, mine.iter().map(|m| m.4).max().unwrap_or(0));
            let w = self.word(fex);
            let (rn, rm, sdiv) = (w >> 5 & 31, w >> 16 & 31, w >> 10 & 1 == 1);
            let copy = |o: u32| [0x2a00_03e1 | d << 16, 0x1300_3c01 | d << 5, 0x9340_7c01 | d << 5].contains(&self.word(o)); // mov w1, sxth w1, sxtw x1
            let divisor = rm == d || (rm == 1 && self.last_write(1, lo, fex).is_some_and(copy));
            let helper = width != 64 || self.last_write(2, lo, mine[mine.len() - 1].1).is_some_and(|o| self.word(o) == 0xaa00_03e2 | d << 16);
            if !divisor || sdiv != signed || (width == 8 && rn != h) || !helper || self.written(1 << d, mine[0].1, divide) {
                return fail("its #DE checks do not read its divide's operands");
            }
            // The dividend is RDX:RAX (x5:x4), unchanged from the instruction start to FEX's divide; the 8-bit forms
            // read FEX's copy of AX (`uxth wH, w4` / `sxth xH, w4`), unchanged up to the divide.
            let first = mine[0].1;
            let ax = if signed { 0x9340_3c80 } else { 0x5300_3c80 } | h;
            let ax_copy = width != 8
                || (self.last_write(h as u8, lo, first).is_some_and(|o| self.word(o) == ax) && !self.written(1 << h, first, fex));
            if !ax_copy || self.written(1 << 4 | 1 << 5, lo, divide.max(fex)) {
                return fail("its #DE checks do not read its dividend RDX:RAX");
            }
            // 64 bits: FEX's selector of its inline divide is exactly `cbz x5` (div) or `asr x0, x4, #63; eor x0, x0, x5;
            // cbz x0` (idiv), right after the checks, to that divide.
            if width == 64 {
                let after = mine.iter().find(|m| m.0 == if signed { Overflow } else { Zero }).map_or(0, |m| m.2) + 4;
                let (words, test): (&[u32], u32) = if signed { (&[0x937f_fc80, 0xca05_0000], CBZ_X) } else { (&[], CBZ_X | 5) };
                let at = after + 4 * words.len() as u32;
                let prefix = words.iter().enumerate().all(|(k, &w)| self.word(after + 4 * k as u32) == w);
                if !prefix || self.cond_branch(at) != Some((test, fex as i64)) {
                    return fail("FEX's 64-bit fast-path selector is not exact");
                }
            }
        }
        // Every helper call is a division's (its verdict was matched above).
        for at in (ENTRY_OFFSET..self.c.code.len() as u32).step_by(4) {
            if matches!(self.insn(at), Some(Insn::Blr))
                && !found.iter().any(|f| f.1 == DivCheck::Verdict && f.2 == at - 8)
            {
                return Err(format!("helper call at {at:#x} without its division's #DE verdict"));
            }
        }
        Ok(())
    }
}

fn is_cbtb(w: u32) -> bool {
    w & 0x7c00_0000 == 0x3400_0000
}
/// A cbz/cbnz/tbz/tbnz word with its offset field cleared (other branches unchanged).
fn normalize(w: u32) -> u32 {
    match w & 0x7e00_0000 {
        0x3400_0000 => w & !(0x7_ffff << 5),
        0x3600_0000 => w & !(0x3fff << 5),
        _ => w,
    }
}

/// (signed, operand width) of an x86 `div`/`idiv` (F6/F7 /6, /7), else `None`.
fn x86_divide(insn: &[u8]) -> Option<(bool, u32)> {
    let mut i = 0;
    let mut size16 = false;
    while let Some(&b @ (0x66 | 0x67 | 0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 | 0xf0 | 0xf2 | 0xf3)) = insn.get(i) {
        size16 |= b == 0x66;
        i += 1;
    }
    let mut rex_w = false;
    if let Some(&b @ 0x40..=0x4f) = insn.get(i) {
        rex_w = b & 8 != 0;
        i += 1;
    }
    let (op, reg) = (*insn.get(i)?, insn.get(i + 1)? >> 3 & 7);
    if !matches!(op, 0xf6 | 0xf7) || reg < 6 {
        return None;
    }
    Some((reg == 7, if op == 0xf6 { 8 } else if rex_w { 64 } else if size16 { 16 } else { 32 }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::code_object::Header;

    /// Real fexc (series8, profile slice1) objects and the guest bytes they were compiled from.
    macro_rules! fixture {
        ($name:literal) => {
            (
                include_bytes!(concat!("../../tests/admission/", $name, ".obj")) as &[u8],
                include_bytes!(concat!("../../tests/admission/", $name, ".src")) as &[u8],
            )
        };
    }
    const FIXTURES: [(&[u8], &[u8]); 8] = [
        fixture!("address-forms"),
        fixture!("division-forms"),
        fixture!("call-rsp"),
        fixture!("rip-relative"),
        fixture!("rip-relative-noncanonical"),
        fixture!("large-displacements"),
        fixture!("jcc"),
        fixture!("fix-edge-rip-relative"),
    ];
    fn admit((object, source): (&[u8], &[u8])) -> Result<(), String> {
        let h: Header = super::super::record(object, 0);
        super::super::admit(object, h.guest_rip, source, u64::MAX).map(|_| ())
    }
    /// `fixture` with the code word at `at` (which must be `old`) replaced by `new`: refused with `why` in the error.
    fn refused(fixture: (&[u8], &[u8]), at: u32, old: u32, new: u32, why: &str) {
        let h: Header = super::super::record(fixture.0, 0);
        let mut object = fixture.0.to_vec();
        let word = &mut object[(h.code_offset + at) as usize..][..4];
        assert_eq!(u32::from_le_bytes(word.try_into().unwrap()), old, "word at {at:#x}");
        word.copy_from_slice(&new.to_le_bytes());
        let e = admit((&object, fixture.1)).expect_err(&format!("{new:#010x} at {at:#x} was admitted"));
        assert!(e.contains(why), "{new:#010x} at {at:#x}: {e}");
    }
    const NOP: u32 = 0xaa00_03e0; // mov x0, x0
    const ADDRESS: (&[u8], &[u8]) = FIXTURES[0];
    const DIVISION: (&[u8], &[u8]) = FIXTURES[1];

    #[test]
    fn real_blocks_are_admitted() {
        for (i, &f) in FIXTURES.iter().enumerate() {
            admit(f).unwrap_or_else(|e| panic!("fixture {i}: {e}"));
        }
    }

    #[test]
    fn decoder_and_bitmask() {
        assert!(matches!(decode(0xf940_0a94, 0x1c), Some(Insn::Mem(m)) if m.load && m.size == 8 && m.addr == Addr { base: 20, index: None, imm: 16 }));
        assert!(matches!(decode(0xf81f_0d14, 0), Some(Insn::Mem(m)) if m.form == Form::Pre && m.delta == -16)); // str x20, [x8, #-16]!
        assert!(matches!(decode(0xd100_03ff, 0), Some(Insn::Data(Some(31))))); // sub sp, sp, #0
        assert!(matches!(decode(0xeb00_001f, 0), Some(Insn::Data(None)))); // cmp x0, x0
        for refused in [0xd65f_03c0, 0xd69f_03e0, 0xd400_0001, 0xd400_0002, 0xd503_201f, 0xd508_871f, 0xd50b_7520, 0xa940_0780, 0xc8df_fc00, 0x9400_0000] {
            assert!(decode(refused, 0).is_none(), "{refused:#x}"); // ret eret svc hvc nop tlbi ic ldp ldar bl
        }
        // unallocated: add-ext option 01 / imm3 5, add-shifted ROR, sf=0 with N=1 or imm6=32, bitfield N != sf
        for unallocated in [0x8b60_0000, 0x8b20_1400, 0x8bc0_0000, 0x1240_0000, 0x0a00_8000, 0x9300_0000, 0x1340_0000] {
            assert!(decode(unallocated, 0).is_none(), "{unallocated:#x}");
        }
        assert_eq!(bitmask(0x320b_83f4), Some(0x0020_0020_0020_0020)); // orr w20, wzr, #0x200020 (replicated to 64 bits)
        assert_eq!(bitmask(0xd241_0080), Some(1 << 63)); // eor x0, x4, #0x8000000000000000
        assert_eq!(x86_divide(&[0x48, 0xf7, 0xf1]), Some((false, 64)));
        assert_eq!(x86_divide(&[0x66, 0x41, 0xf7, 0xf9]), Some((true, 16)));
        assert_eq!(x86_divide(&[0xf6, 0x33]), Some((false, 8)));
        assert_eq!(x86_divide(&[0xf7, 0xe1]), None); // mul ecx
    }

    #[test]
    fn the_stop_store_is_pinned_at_the_block_entry() {
        // Every block has `str x0, [x28]; str xzr, [x28, #stop]` as its prologue words after `adr`.
        assert_eq!((STOP_AT, STR_XZR_STOP), (12, 0xf907_639f));
        for (i, &f) in FIXTURES.iter().enumerate() {
            let h: Header = super::super::record(f.0, 0);
            let w = |at: u32| u32::from_le_bytes(f.0[(h.code_offset + at) as usize..][..4].try_into().unwrap());
            assert_eq!((w(8), w(12)), (STR_X0_HEADER, STR_XZR_STOP), "fixture {i}");
        }
        let at = STOP_AT;
        refused(ADDRESS, at, STR_XZR_STOP, NOP, "entry prologue"); // stop store removed
        refused(ADDRESS, at, STR_XZR_STOP, STR_XZR_STOP + (1 << 10), "entry prologue"); // the next 8 bytes
        refused(ADDRESS, at, STR_XZR_STOP, STR_XZR_STOP - (1 << 10), "entry prologue"); // the previous 8 bytes
        refused(ADDRESS, at, STR_XZR_STOP, STR_XZR_STOP & !31, "entry prologue"); // str x0, not xzr
        refused(ADDRESS, at, STR_XZR_STOP, 0xb900_039f | INTERRUPT_FAULT_PAGE / 4 << 10, "entry prologue"); // str wzr
        refused(ADDRESS, at, STR_XZR_STOP, 0xf900_039f | (INTERRUPT_FAULT_PAGE + 8) / 8 << 10, "entry prologue");
        refused(ADDRESS, at - 4, STR_X0_HEADER, NOP, "entry prologue"); // header store removed
        // Moved: the stop store as the second word, the header store third.
        let h: Header = super::super::record(ADDRESS.0, 0);
        let mut object = ADDRESS.0.to_vec();
        for (o, w) in [(8, STR_XZR_STOP), (12, STR_X0_HEADER)] {
            object[(h.code_offset + o) as usize..][..4].copy_from_slice(&w.to_le_bytes());
        }
        let e = admit((&object, ADDRESS.1)).expect_err("moved stop store admitted");
        assert!(e.contains("entry prologue"), "{e}");
        // Stored again, or loaded, anywhere else in the block (the word after the first load: mov x4, x20).
        let ldr_stop = STR_XZR_STOP & !31 | 1 | 1 << 22; // ldr x1, [x28, #stop]
        refused(ADDRESS, 0x24, 0xaa14_03e4, STR_XZR_STOP, "interrupt-fault word");
        refused(ADDRESS, 0x24, 0xaa14_03e4, STR_XZR_STOP & !31, "interrupt-fault word"); // str x0, [x28, #stop]
        refused(ADDRESS, 0x24, 0xaa14_03e4, ldr_stop, "interrupt-fault word");
        refused(ADDRESS, 0x24, 0xaa14_03e4, 0xb900_039f | (INTERRUPT_FAULT_PAGE + 4) / 4 << 10, "interrupt-fault word"); // str wzr, the field's upper half
    }

    #[test]
    fn address_guards_are_verified() {
        refused(ADDRESS, 0x18, 0xca01_4020, NOP, "address guard"); // eor x0, x1, x1, lsl #16 removed
        refused(ADDRESS, 0x18, 0xca01_4020, 0xca02_4040, "address guard"); // guard on x2
        refused(ADDRESS, 0x14, 0x9100_4281, 0x9100_6281, "address guard"); // add x1, x20, #0x18: not the access's address
        refused(ADDRESS, 0x10, 0x8b07_0cd4, 0x1400_0004, "address guard"); // b 0x1c: enters the guarded window
        refused(ADDRESS, 0x1c, 0xb7f8_09a0, NOP, "reached by 0 branches"); // tbnz removed
        // add x6, x6, #1 between a read-modify-write's (shared) guard and its store
        refused(ADDRESS, 0xa0, 0xca04_0295, 0x9100_04c6, "read-modify-write store");
        let large = FIXTURES[5];
        refused(large, 0x10, 0x5282_0001, 0x5284_0001, "address guard"); // mov w1, #0x2000 for an access at +0x1000
    }

    #[test]
    fn split_checks_are_verified() {
        refused(ADDRESS, 0x3c, 0x9100_1c20, 0x9100_0c20, "split check"); // add x0, x1, #3 for an 8-byte store
        refused(ADDRESS, 0x3c, 0x9100_1c20, NOP, "split check");
    }

    #[test]
    fn guest_rip_addresses_are_verified() {
        let (rip, noncanonical) = (FIXTURES[3], FIXTURES[4]);
        refused(rip, 0x14, 0x72a0_0214, 0x72a0_0234, "guest-RIP move"); // movk w20, #0x11, lsl #16
        refused(rip, 0x28, 0x1400_0024, NOP, "reached by 0 branches"); // the crossing store's `b split`
        refused(noncanonical, 0x1c, 0x1400_000e, NOP, "reached by 0 branches"); // the non-canonical load's `b guard`
        refused(noncanonical, 0x18, 0xf2d0_0014, 0xf2c0_0014, "guest-RIP move"); // canonical, but not the relocation
        // Review H1: `mov rax, [rip + 0x40000ff9]` = PRIVATE (0x8000_4000_0000), `b G` jumped over by a branch between
        // the move and the `b`: the window is sealed from the move, the `b` included.
        let h: Header = super::super::record(noncanonical.0, 0);
        let (mut object, mut source) = (noncanonical.0.to_vec(), noncanonical.1.to_vec());
        source[3..7].copy_from_slice(&[0xf9, 0x0f, 0x00, 0x40]);
        let reloc = h.relocs_offset as usize;
        assert_eq!(object[reloc..reloc + 6], [0x10, 0, 0, 0, object::RELOC_GUEST_RIP_MOVE, 20]);
        object[reloc + 8..reloc + 16].copy_from_slice(&0x8000_4000_0000u64.to_le_bytes());
        for (at, w) in [(0x10, 0xd2a8_0014u32), (0x14, 0xf2d0_0014), (0x18, 0x1400_0002)] {
            object[(h.code_offset + at) as usize..][..4].copy_from_slice(&w.to_le_bytes());
        }
        let e = admit((&object, &source)).expect_err("branch over `b guard` admitted");
        assert!(e.contains("a branch enters between its guest-RIP move and the access"), "{e}");
        object[(h.code_offset + 0x18) as usize..][..4].copy_from_slice(&NOP.to_le_bytes());
        admit((&object, &source)).unwrap(); // the same block without the branch is admitted
    }

    #[test]
    fn exits_and_branch_targets_are_verified() {
        let (call, jcc) = (FIXTURES[2], FIXTURES[6]);
        refused(call, 0x10, 0xca08_4100, 0xca09_4120, "branch-target guard"); // checks x9, exits through rsp's copy
        refused(call, 0x40, 0xaa08_03f6, 0xaa09_03f6, "branch-target guard"); // mov x22, x9: an unchecked RIP
        refused(jcc, 0x18, 0x5400_00e1, 0x5400_0041, "link point"); // b.ne to the str, skipping its literal
        refused(jcc, 0x20, 0xf900_0f80, 0xf940_0f80, "link point"); // exit without storing State.rip
        refused(jcc, 0x18, 0x5400_00e1, 0x5400_0061, "link point"); // b.ne to the gate load, skipping the exit's store
    }

    /// `jcc` (`cmp rax, rcx; jne +0x40`): fall-through exit at 0x1c (literal 0x2c), taken exit at 0x34 (literal 0x44).
    const JCC: (&[u8], &[u8]) = FIXTURES[6];
    const LINK_FT: u32 = 0x1c;
    const LDR_X0_FT: u32 = 0x5800_0080; // ldr x0, 0x2c

    #[test]
    fn link_points_are_returned() {
        let h: Header = super::super::record(JCC.0, 0);
        let a = super::super::admit(JCC.0, h.guest_rip, JCC.1, u64::MAX).unwrap();
        let point = |offset, target| LinkPoint { offset, reserved: 0, target };
        assert_eq!(a.links, [point(LINK_FT, h.guest_rip + 5), point(0x34, h.guest_rip + 0x45)]);
        // A computed exit (`call rsp`) is no link point.
        let (call, _) = FIXTURES[2];
        let h: Header = super::super::record(call, 0);
        assert!(super::super::admit(call, h.guest_rip, FIXTURES[2].1, u64::MAX).unwrap().links.is_empty());
    }

    #[test]
    fn link_point_form_is_pinned() {
        let at = LINK_FT;
        // Form altered: the dispatcher instead of the linker; another register; the tail's words rewritten.
        refused(JCC, at + 8, LDR_LINKER, LDR_GATE, "not exactly a link point");
        refused(JCC, at, LDR_X0_FT, LDR_X0_FT | 2, "link point"); // ldr x2, L; str x0
        refused(JCC, at + 4, STR_X0_RIP, STR_X0_RIP | 2, "link point"); // ldr x0, L; str x2
        refused(JCC, at + 12, BR_X1, 0xd61f_0040, "link point"); // br x2
        // An extra write in the tail: x1 (the branch register) from a guest register, or x0 changed before its store.
        refused(JCC, at + 8, LDR_LINKER, 0xaa05_03e1, "indirect branch"); // mov x1, x5
        refused(JCC, at + 4, STR_X0_RIP, 0x9100_0400, "link point"); // add x0, x0, #1
        // A branch into words 2-4 (the b.ne at 0x18 targets the taken exit's first word, 0x34).
        refused(JCC, 0x18, 0x5400_00e1, 0x5400_0001 | (at + 4 - 0x18) / 4 << 5, "link point");
        refused(JCC, 0x18, 0x5400_00e1, 0x5400_0001 | (at + 8 - 0x18) / 4 << 5, "link point");
        refused(JCC, 0x18, 0x5400_00e1, 0x5400_0001 | (at + 12 - 0x18) / 4 << 5, "link point");
        // The linker pointer loaded anywhere else: in a straight-line block, or as a computed exit's gate.
        refused(ADDRESS, 0x24, 0xaa14_03e4, LDR_LINKER, "only as the third word of a link point");
        let (call, src) = FIXTURES[2];
        let h: Header = super::super::record(call, 0);
        let words: Vec<u32> = call[h.code_offset as usize..][..h.code_region_end as usize].chunks(4).map(|w| u32::from_le_bytes(w.try_into().unwrap())).collect();
        let gate = words.iter().position(|&w| w == LDR_GATE).unwrap() as u32 * 4;
        refused((call, src), gate, LDR_GATE, LDR_LINKER, "only as the third word of a link point");
    }

    #[test]
    fn link_targets_must_be_proven() {
        // The fall-through literal set to the block's own RIP (a plausible literal value, not this exit's target).
        let h: Header = super::super::record(JCC.0, 0);
        let mut object = JCC.0.to_vec();
        let reloc = (0..h.relocs_count as usize)
            .map(|i| h.relocs_offset as usize + i * size_of::<Reloc>())
            .find(|&r| u32::from_le_bytes(object[r..r + 4].try_into().unwrap()) == 0x2c)
            .unwrap();
        object[reloc + 8..reloc + 16].copy_from_slice(&h.guest_rip.to_le_bytes());
        let e = admit((&object, JCC.1)).expect_err("unproven link target admitted");
        assert!(e.contains("neither the block end nor its branch target"), "{e}");
        // Without a branch ending the block (fall-through only), its target is not proven either (refused already as
        // a relocation value).
        object[reloc + 8..reloc + 16].copy_from_slice(&(h.guest_rip + 0x45).to_le_bytes());
        admit((&object, JCC.1)).unwrap(); // both exits to the taken target
        let mut source = JCC.1.to_vec();
        source[3] = 0x90; // nop instead of jne: the block's branch has no decoded target
        let e = admit((&object, &source)).expect_err("link target without a branch admitted");
        assert!(e.contains("relocation at 0x2c"), "{e}");
    }

    #[test]
    fn only_the_entry_prologue_writes_the_block_header() {
        refused(ADDRESS, 0x24, 0xaa14_03e4, STR_X0_HEADER, "block header"); // a second header store
        refused(ADDRESS, 0x24, 0xaa14_03e4, 0xf900_0384, "block header"); // str x4, [x28]: a guest value as header
        refused(ADDRESS, 0x24, 0xaa14_03e4, 0xb900_0784, "block header"); // str w4, [x28, #4]: its upper half
        refused(ADDRESS, 0x24, 0xaa14_03e4, 0xf940_0381, "block header"); // ldr x1, [x28]
        refused(ADDRESS, ENTRY_OFFSET, ADR_X0_HEADER, 0x1000_0000, "entry prologue"); // adr x0, #0: the entry, not the header
        refused(ADDRESS, ENTRY_OFFSET, ADR_X0_HEADER, 0x10ff_ffc0, "entry prologue"); // adr x0, #-8
    }

    #[test]
    fn division_checks_are_pinned() {
        refused(DIVISION, 0x108, 0xcb27_4000, 0xcb27_2000, "divide stub"); // div ecx: uxtw -> uxth
        refused(DIVISION, 0x18, 0xd348_3ee0, 0xd349_42e0, "divide stub"); // div cl: ubfx x0, x23, #9, #8
        refused(DIVISION, 0x188, LDR_LUDIV, LDR_LDIV, "divide stub"); // div rcx calling the signed helper
        refused(DIVISION, 0x1c8, 0xb400_0f34, 0xb400_0f35, "zero check"); // idiv: cbz x21, overflow tests x20
        refused(DIVISION, 0x20, 0xb6f8_0000 | DIVISION_STUB_IMM, NOP, "reached by 0 branches"); // check removed
        // The checks read FEX's own operands.
        refused(DIVISION, 0x1c, 0xcb34_0000, 0xcb35_0000, "operands"); // div cl: sub x0, x0, w21, uxtb (divisor is w20)
        refused(DIVISION, 0x18, 0xd348_3ee0, 0xd348_3ec0, "operands"); // div cl: ubfx x0, x22 (dividend is x23)
        refused(DIVISION, 0x6c, 0x1300_1e81, 0x1300_1ea1, "operands"); // idiv bh: sxtb w1, w21
        refused(DIVISION, 0xcc, 0x1300_3e81, 0x1300_3ea1, "operands"); // idiv r9w: sxth w1, w21 (FEX divides by sxth w1, w20)
        refused(DIVISION, 0x174, 0xb400_1167, 0xb400_1166, "operands"); // div rcx: cbz x6
        refused(DIVISION, 0x184, 0xaa07_03e2, 0xaa06_03e2, "operands"); // div rcx: the helper gets x6 as divisor
        // Review H1: FEX's 64-bit fast-path selector and the 8-bit dividend copy are pinned.
        refused(DIVISION, 0x178, 0xb400_0185, 0xb400_0186, "selector"); // div rcx: cbz x6, inline
        refused(DIVISION, 0x178, 0xb400_0185, 0xb400_019f, "selector"); // cbz xzr: always the inline divide
        refused(DIVISION, 0x178, 0xb400_0185, 0x1400_000c, "selector"); // b inline
        refused(DIVISION, 0x1e8, 0xca05_0000, 0xca00_0000, "selector"); // idiv: eor x0, x0, x0
        refused(DIVISION, 0x14, 0x5300_3c97, 0x5300_3cd7, "dividend"); // div cl: uxth w23, w6 (AX is w4)
        refused(DIVISION, 0x10, 0x5300_1cf4, 0x5300_1ce4, "dividend"); // div cl: uxtb w4, w7 writes RAX first
    }
    /// The div-cl check's tbz offset field (stub at 0x368 from 0x1c).
    const DIVISION_STUB_IMM: u32 = ((0x368 - 0x1c) / 4) << 5;

    #[test]
    fn forbidden_instructions_are_refused() {
        let at = 0x24; // mov x4, x20 after the first load
        let old = 0xaa14_03e4;
        refused(ADDRESS, at, old, 0xd63f_00a0, "indirect branch"); // blr x5
        refused(ADDRESS, at, old, 0xd61f_0020, "indirect branch"); // br x1 without the gate load
        refused(ADDRESS, at, old, 0xd51b_4220, "unrecognised"); // msr daif, x0
        refused(ADDRESS, at, old, 0xd65f_03c0, "unrecognised"); // ret
        refused(ADDRESS, at, old, 0xd400_0002, "unrecognised"); // hvc #0
        refused(ADDRESS, at, old, 0xf900_0794, "frame access"); // str x20, [x28, #8]: no admitted field
        refused(ADDRESS, at, old, 0xb900_1b94, "frame access"); // str w20, [x28, #24]: half of State.rip
        refused(ADDRESS, at, old, 0xf905_b781, "frame access"); // str x1, [x28, #DispatcherLoopTop]
        refused(ADDRESS, at, old, 0xf945_b781, "frame access"); // ldr x1, [x28, #DispatcherLoopTop] without its br
        refused(ADDRESS, at, old, 0xf81f_c380, "frame access"); // stur x0, [x28, #-4]: before the frame (once overflowed u32)
        refused(ADDRESS, at, old, 0xf85f_c380, "frame access"); // ldur x0, [x28, #-4]
        refused(ADDRESS, at, old, 0xa940_0780, "unrecognised"); // ldp x0, x1, [x28]
        refused(ADDRESS, at, old, 0xaa14_03fc, "write to x28"); // mov x28, x20
        refused(ADDRESS, at, old, 0x9100_029f, "write to sp"); // mov sp, x20
        refused(ADDRESS, at, old, 0xf940_00c5, "unrecorded memory access"); // ldr x5, [x6]
        refused(ADDRESS, at, old, 0x17ff_ffff, "not forward"); // b .-4
        refused(ADDRESS, at, old, 0xd420_0020, "brk"); // brk #1 that is no stub
    }
}
