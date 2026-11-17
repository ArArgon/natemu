//! Trapped FP exceptions, the part that needs no hardware (AFP accurate plan, section 3): which Arm word trapped, and what the
//! re-run's FPSR becomes.
//!
//! The resident's EL0 vector takes EC 0x2C with the destination and FPSR unchanged. It re-runs the one Arm word in place with the trap
//! enables cleared, so the word's own flags are in FPSR afterwards, and then calls [`patch`] with the word, the x86 rounding mode and
//! the word's source registers as they were at the trap. What is patched depends on the mode's enabled traps alone, never on the cause
//! in ESR (one word can raise IOC in one lane and UFC in another, and the ISS names one exception): chunk 3 enables UFE only, so the
//! only flag decided here is UFC, per lane, by [`crate::tiny`] (x86's masked underflow: tiny after rounding and inexact).
//!
//! A word is either [`Class::Keep`] (the re-run's flags are what the hardware gives without traps, which is right) or [`Class::Patch`]
//! (a multiply, divide, fused multiply-add or narrowing convert: UFC is recomputed). A word in neither class is unknown: the resident
//! halts with a named stop rather than patch it wrongly. (`fcvt h, d` is unclassified on purpose: no x86 instruction produces it.) UFE traps every result tiny before rounding, exact ones too, so the Keep class
//! holds the adds and subtracts, which never raise UFC (a tiny sum is exact).
use crate::tiny;

/// FPSR.UFC.
pub const UFC: u64 = 8;

/// FPSR.IOC.
pub const IOC: u64 = 1;

/// How a word's NaN lanes are decided in accurate mode (AFP accurate plan chunk 4): by FPCR.AH = 1's rules, which are x86's.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Nan {
    /// No NaN rule differs from the hardware's (conversions: a NaN operand is quieted, the same under both).
    None,
    /// Two sources `rn`, `rm` (or its element `index`): add, subtract, multiply, divide.
    Bin,
    /// One source `rn` (square root).
    Un,
    /// Three sources `rn`, `rm`, `ra`: fused multiply-add.
    Fma,
    /// Pairwise add: lane i of the first half is the pair (2i, 2i + 1) of `rn`, of the second half of `rm`; a scalar word adds the two
    /// elements of `rn`.
    Pair,
}

/// A word the handler patches: the operation's kind and element size, the lanes it covers, and its source registers. `kind` is for the
/// UFC model ([`tiny`]) and `nan` for accurate mode's NaN lanes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Op {
    /// [`tiny::MUL`], [`tiny::DIV`], [`tiny::FMA`], [`tiny::CVT`] (binary64 to binary32) or [`tiny::CVT16`] (binary32 to binary16); 0: the
    /// word never raises UFC (an add, subtract, square root, pairwise add), only its NaN lanes are patched.
    pub kind: u8,
    pub nan: Nan,
    /// Source elements are binary64 (not for [`tiny::CVT16`], whose sources are binary32).
    pub wide: bool,
    /// FMA's negated product (FMSUB, FNMADD, FMLS) and negated addend (FNMADD, FNMSUB).
    pub neg: (bool, bool),
    /// Source lanes: 1 for a scalar word, else 2 or 4.
    pub lanes: u8,
    /// Registers: `rn` (first multiplicand, dividend, converted value), `rm` (second multiplicand, divisor), `ra` (the addend: Ra, or the
    /// destination of FMLA and FMLS).
    pub rn: u8,
    pub rm: u8,
    pub ra: u8,
    /// A by-element word: `rm`'s element `index` is every lane's second operand.
    pub index: Option<u8>,
    /// The destination register, and for FCVTN2 whether the narrowed lanes go to its upper half.
    pub rd: u8,
    pub upper: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Class {
    Keep,
    Patch(Op),
}

impl Op {
    /// The registers whose trap-time values [`patch`] reads (the trap saves them: the re-run may write one with its result).
    pub fn sources(&self) -> [u8; 3] {
        [self.rn, self.rm, self.ra]
    }
}

fn op(kind: u8, wide: bool, lanes: u8, w: u32) -> Op {
    Op {
        kind,
        nan: match kind {
            tiny::MUL | tiny::DIV => Nan::Bin,
            tiny::FMA => Nan::Fma,
            _ => Nan::None,
        },
        wide,
        neg: (false, false),
        lanes,
        rn: (w >> 5 & 31) as u8,
        rm: (w >> 16 & 31) as u8,
        ra: (w & 31) as u8,
        index: None,
        rd: (w & 31) as u8,
        upper: false,
    }
}

/// The class of the Arm word `w`, `None` when the table does not know it. Accurate mode's IOE makes every word that can raise IOC trap, so
/// the table holds all the floating-point words FEX emits for SSE/AVX: the arithmetic (patched), and the compares, conversions and
/// roundings (kept: the re-run's result and flags are the hardware's, the same under AH = 1).
pub fn classify(w: u32) -> Option<Class> {
    use Class::{Keep, Patch};
    let nan = |kind: u8, nan: Nan, wide: bool, lanes: u8| Some(Patch(Op { nan, ..op(kind, wide, lanes, w) }));
    let ty = w >> 22 & 3;
    // Scalar floating point, two sources: FMUL 0, FDIV 1, FADD 2, FSUB 3, FMAX/FMIN/FMAXNM/FMINNM 4-7, FNMUL 8 (binary32, binary64).
    if w & 0xff20_0c00 == 0x1e20_0800 && ty < 2 {
        return match w >> 12 & 15 {
            0 | 8 => Some(Patch(op(tiny::MUL, ty == 1, 1, w))),
            1 => Some(Patch(op(tiny::DIV, ty == 1, 1, w))),
            2 | 3 => nan(0, Nan::Bin, ty == 1, 1),
            4..=7 => Some(Keep),
            _ => None,
        };
    }
    // Scalar compares: FCMP, FCMPE (with a register or zero).
    if w & 0xff20_fc07 == 0x1e20_2000 && ty < 2 {
        return Some(Keep);
    }
    // Scalar fused multiply-add: FMADD, FMSUB, FNMADD, FNMSUB (o1 bit 21, o0 bit 15).
    if w & 0xff00_0000 == 0x1f00_0000 && ty < 2 {
        let mut o = op(tiny::FMA, ty == 1, 1, w);
        o.ra = (w >> 10 & 31) as u8;
        o.neg = match (w >> 21 & 1, w >> 15 & 1) {
            (0, 0) => (false, false),
            (0, _) => (true, false),
            (_, 0) => (true, true),
            _ => (false, true),
        };
        return Some(Patch(o));
    }
    // Scalar FCVT: type is the source, opc (bits 16:15) the destination; 0 binary32, 1 binary64, 3 binary16.
    if w & 0xff3e_7c00 == 0x1e22_4000 {
        return match (ty, w >> 15 & 3) {
            (1, 0) => Some(Patch(op(tiny::CVT, true, 1, w))),
            (0, 3) => Some(Patch(op(tiny::CVT16, false, 1, w))),
            (0, 1) | (3, 0 | 1) => Some(Keep), // widening: exact
            _ => None,
        };
    }
    // Scalar one source: FSQRT 3 (patched), FRINT* 8-0xf and FRINT32/64 0x10-0x13 (kept; FMOV, FABS and FNEG raise nothing).
    if w & 0xff20_7c00 == 0x1e20_4000 && ty < 2 {
        return match w >> 15 & 0x3f {
            3 => nan(0, Nan::Un, ty == 1, 1),
            8..=0xf | 0x10..=0x13 => Some(Keep),
            _ => None,
        };
    }
    // Floating point to integer: FCVT{N,P,M,Z,A}{S,U} (rmode bits 20:19, opcode bits 18:16 0 or 1), and the fixed-point FCVTZS/FCVTZU.
    if w & 0x7f20_fc00 == 0x1e20_0000 && matches!(w >> 16 & 7, 0 | 1) || w & 0x7f3e_0000 == 0x1e18_0000 {
        return Some(Keep);
    }
    let (u, a, sz, q) = (w >> 29 & 1, w >> 23 & 1, w >> 22 & 1, w >> 30 & 1);
    let wide = sz == 1;
    // Vector three-register-same, floating point: FMUL, FDIV, FMLA, FMLS, FADD, FSUB, FADDP (patched), FABD, the compares (kept).
    if w & 0x9f20_0400 == 0x0e20_0400 && !(wide && q == 0) {
        let lanes = if wide || q == 0 { 2 } else { 4 };
        return match (u, a, w >> 11 & 31) {
            (1, 0, 0x1b) => Some(Patch(op(tiny::MUL, wide, lanes, w))),
            (1, 0, 0x1f) => Some(Patch(op(tiny::DIV, wide, lanes, w))),
            (0, _, 0x19) => {
                let mut o = op(tiny::FMA, wide, lanes, w);
                o.neg.0 = a == 1;
                Some(Patch(o))
            }
            (0, _, 0x1a) => nan(0, Nan::Bin, wide, lanes),
            (1, 0, 0x1a) => nan(0, Nan::Pair, wide, lanes),
            (1, 1, 0x1a) => Some(Keep),
            (_, _, 0x1c | 0x1d) => Some(Keep), // FCMEQ, FCMGE, FCMGT, FACGE, FACGT
            _ => None,
        };
    }
    // By element, vector or scalar: FMLA 1, FMLS 5, FMUL 9 (U = 0), binary32 (size 2: index H:L) or binary64 (size 3: index H).
    if (w & 0x9f00_0400 == 0x0f00_0000 || w & 0xdf00_0400 == 0x5f00_0000) && u == 0 && ty >= 2 {
        let scalar = w >> 28 & 1 == 1;
        let wide = ty == 3;
        if wide && w >> 21 & 1 != 0 || wide && q == 0 && !scalar {
            return None;
        }
        let lanes = if scalar { 1 } else if wide || q == 0 { 2 } else { 4 };
        let mut o = match w >> 12 & 15 {
            1 | 5 => op(tiny::FMA, wide, lanes, w),
            9 => op(tiny::MUL, wide, lanes, w),
            _ => return None,
        };
        o.rm = ((w >> 20 & 1) << 4 | w >> 16 & 15) as u8;
        o.index = Some(if wide { (w >> 11 & 1) as u8 } else { (w >> 10 & 2 | w >> 21 & 1) as u8 });
        o.neg.0 = w >> 12 & 15 == 5;
        return Some(Patch(o));
    }
    // Vector two-register misc: FCVTN/FCVTN2 (S to H, D to S; Q picks the destination half), FCVTL/FCVTL2, the estimates, the compares with
    // zero, the roundings and the conversions to integer (kept), FSQRT (patched).
    if w & 0x9f3e_0c00 == 0x0e20_0800 {
        return match (u, a, w >> 12 & 31) {
            (0, 0, 0x16) if wide => Some(Patch(Op { upper: q == 1, ..op(tiny::CVT, true, 2, w) })),
            (0, 0, 0x16) => Some(Patch(op(tiny::CVT16, false, 4, w))),
            (0, 0, 0x17) | (0, 1, 0x1d) | (1, 1, 0x1d) => Some(Keep),
            (_, 1, 0xc | 0xd) | (0, 1, 0xe) => Some(Keep), // FCMGT, FCMEQ, FCMGE, FCMLE, FCMLT with zero
            (_, 0, 0x18 | 0x19) | (0, 1, 0x18 | 0x19) | (1, 1, 0x19) => Some(Keep), // FRINTN M A X P Z I
            (_, 0, 0x1e | 0x1f) => Some(Keep), // FRINT32, FRINT64
            (_, _, 0x1a..=0x1c) => Some(Keep), // FCVTxS, FCVTxU
            (1, 1, 0x1f) if !(wide && q == 0) => nan(0, Nan::Un, wide, if wide || q == 0 { 2 } else { 4 }),
            _ => None,
        };
    }
    // Scalar two-register misc: FRECPE, FRSQRTE, the compares with zero, the conversions to integer: kept.
    if w & 0xdf3e_0c00 == 0x5e20_0800
        && matches!((u, a, w >> 12 & 31), (0, 1, 0x1d) | (1, 1, 0x1d) | (_, 1, 0xc | 0xd) | (0, 1, 0xe) | (_, _, 0x1a..=0x1c))
    {
        return Some(Keep);
    }
    // Scalar compares, two registers.
    if w & 0xdf20_0400 == 0x5e20_0400 && matches!(w >> 11 & 31, 0x1c | 0x1d) {
        return Some(Keep);
    }
    // Scalar FADDP (pairwise add of the two elements of a vector), vector FCADD.
    if w & 0xffbf_fc00 == 0x7e30_d800 {
        return nan(0, Nan::Pair, w >> 22 & 1 == 1, 1);
    }
    if w & 0xbf20_ec00 == 0x2e00_e400 {
        return Some(Keep);
    }
    None
}

/// The x86 rounding mode (MXCSR.RC encoding) of an FPCR: RMode (bits 23:22) is the same permutation back.
pub const fn x86_rc(fpcr: u64) -> u32 {
    [0, 2, 1, 3][(fpcr >> 22 & 3) as usize]
}

/// The NaN class of an `e`-byte element: 0 none, 1 quiet, 2 signalling.
fn nan_class(v: u64, wide: bool) -> u8 {
    let (exp, frac, quiet) = if wide { (v >> 52 & 0x7ff == 0x7ff, v & (1 << 52) - 1, 1u64 << 51) } else { (v >> 23 & 0xff == 0xff, v & (1 << 23) - 1, 1u64 << 22) };
    if !exp || frac == 0 {
        0
    } else if frac & quiet != 0 {
        1
    } else {
        2
    }
}
fn elem(q: &[u8; 16], k: usize, wide: bool) -> u64 {
    if wide {
        u64::from_le_bytes(q[8 * k..8 * k + 8].try_into().unwrap())
    } else {
        u64::from(u32::from_le_bytes(q[4 * k..4 * k + 4].try_into().unwrap()))
    }
}
fn put_elem(q: &mut [u8; 16], k: usize, wide: bool, v: u64) {
    if wide {
        q[8 * k..8 * k + 8].copy_from_slice(&v.to_le_bytes());
    } else {
        q[4 * k..4 * k + 4].copy_from_slice(&(v as u32).to_le_bytes());
    }
}
/// FPCR.AH = 1's result for a lane whose operands `ops` (in the operation's order: first source first, the addend of an FMA last) include a
/// NaN: the first NaN, quieted (x86 returns the first source's NaN, converted to quiet if signalling), and whether an operand was an SNaN
/// (IOC). `None` when no operand is a NaN.
fn first_nan(ops: &[u64], wide: bool) -> Option<(u64, bool)> {
    let first = ops.iter().copied().find(|&v| nan_class(v, wide) != 0)?;
    let quiet = if wide { 1u64 << 51 } else { 1 << 22 };
    Some((first | quiet, ops.iter().any(|&v| nan_class(v, wide) == 2)))
}
/// The default NaN of AH = 1: negative (x86's real indefinite).
fn default_nan(wide: bool) -> u64 {
    if wide { 0xfff8_0000_0000_0000 } else { 0xffc0_0000 }
}

/// Accurate mode's NaN lanes of a re-run word (AFP accurate plan chunk 4, §4 A): every lane of `dest` whose operands hold a NaN is the first
/// NaN among them quieted, and every lane that is a NaN with none among its operands (an invalid operation) is the negative default NaN, as
/// FPCR.AH = 1 (x86) does; for an FMA the IOC is also recomputed (AH = 1 raises none for infinity times zero beside a quiet NaN addend: D).
/// Returns the FPSR flags of `rerun` with that IOC.
fn patch_nan(o: &Op, q: &[[u8; 16]; 3], rerun: u64, dest: &mut [u8; 16]) -> u64 {
    let w = o.wide;
    let mut ioc = false;
    for i in 0..usize::from(o.lanes) {
        let b_at = |k: Option<u8>| usize::from(k.unwrap_or(i as u8));
        let ops: [u64; 3] = match o.nan {
            Nan::None => return rerun,
            Nan::Un => [elem(&q[0], i, w), 0, 0],
            Nan::Bin => [elem(&q[0], i, w), elem(&q[1], b_at(o.index), w), 0],
            Nan::Fma => [elem(&q[0], i, w), elem(&q[1], b_at(o.index), w), elem(&q[2], i, w)],
            Nan::Pair => {
                let n = if o.lanes == 1 { 1 } else { usize::from(o.lanes) / 2 };
                let (r, k) = if i < n { (0, i) } else { (1, i - n) };
                [elem(&q[r], 2 * k, w), elem(&q[r], 2 * k + 1, w), 0]
            }
        };
        let count = match o.nan { Nan::Un => 1, Nan::Fma => 3, _ => 2 };
        let got = elem(dest, i, w);
        match first_nan(&ops[..count], w) {
            Some((v, snan)) => {
                put_elem(dest, i, w, v);
                ioc |= snan;
            }
            None if nan_class(got, w) != 0 => {
                put_elem(dest, i, w, default_nan(w));
                ioc = true;
            }
            None => {}
        }
    }
    // Only an FMA's IOC is recomputed: for the others the hardware's is the same under both AH values.
    if o.nan == Nan::Fma { rerun & !IOC | if ioc { IOC } else { 0 } } else { rerun }
}

/// The FMA NaN check (AFP accurate plan chunk 4): the lanes of `result` (the FMA's result register, `lanes` of `wide` elements) whose
/// operands `q` (Rn, Rm, the addend, as they were at the check: the site names the registers) hold a NaN become the first of them,
/// quieted, as AH = 1 (x86) chooses; the other lanes are left (an invalid lane was patched at the FMA's own IOE trap). No flag changes:
/// the compare that trapped is skipped.
pub fn fma_check(wide: bool, lanes: usize, q: &[[u8; 16]; 3], result: &mut [u8; 16]) {
    for i in 0..lanes {
        let ops = [elem(&q[0], i, wide), elem(&q[1], i, wide), elem(&q[2], i, wide)];
        if let Some((v, _)) = first_nan(&ops, wide) {
            put_elem(result, i, wide, v);
        }
    }
}

/// FPSR after the word, from `rerun` (the flags of the re-run alone) and the word's `class`: [`UFC`] is kept exactly when some lane is
/// tiny after rounding and inexact ([`tiny::lane_underflow`]); the rest is the hardware's, but with `nan` (accurate mode, FPCR.IOE on) the
/// NaN lanes of `dest` and an FMA's IOC are AH = 1's too ([`patch_nan`]). `q` holds the registers [`Op::sources`] names, in
/// that order, at the trap; `rc` is the x86 rounding mode the word ran under. A re-run without UFC needs no model: Arm's UFC (tiny before
/// rounding and inexact) holds whenever x86's masked UE does.
///
/// `ftz` (x86's FTZ in effect with UM masked, and DAZ clear: with both the hardware flushes and nothing traps) also fixes the value, in
/// `dest` (the destination register after the re-run): software flushes only results with a zero exponent, but a result that x86 rounds to
/// something still below MIN_NORMAL (tiny after rounding) and that Arm's denormal grid rounds up to +-MIN_NORMAL must be +-0 too (with
/// UE and PE: the UFC kept here becomes both). Single and double lanes of multiplies, divides, FMAs and the double-to-single narrowing
/// (VCVTPS2PH ignores FTZ).
pub fn patch(class: Class, rc: u32, q: &[[u8; 16]; 3], rerun: u64, ftz: bool, dest: &mut [u8; 16], nan: bool) -> u64 {
    let Class::Patch(o) = class else {
        return rerun;
    };
    let flags = if o.kind != 0 { patch_ufc(&o, rc, q, rerun, ftz, dest) } else { rerun };
    if nan { patch_nan(&o, q, flags, dest) } else { flags }
}

fn patch_ufc(o: &Op, rc: u32, q: &[[u8; 16]; 3], rerun: u64, ftz: bool, dest: &mut [u8; 16]) -> u64 {
    if rerun & UFC == 0 {
        return rerun;
    }
    let lane = |r: usize, i: usize, wide: bool| -> u64 {
        if wide {
            u64::from_le_bytes(q[r][8 * i..8 * i + 8].try_into().unwrap())
        } else {
            u64::from(u32::from_le_bytes(q[r][4 * i..4 * i + 4].try_into().unwrap()))
        }
    };
    let mut under = false;
    for i in 0..usize::from(o.lanes) {
        let a = lane(0, i, o.wide);
        let b = match (o.kind, o.index) {
            (tiny::CVT | tiny::CVT16, _) => 0,
            (_, Some(k)) => lane(1, usize::from(k), o.wide),
            _ => lane(1, i, o.wide),
        };
        let c = if o.kind == tiny::FMA { lane(2, i, o.wide) } else { 0 };
        if ftz && o.kind != tiny::CVT16 && tiny::lane_tiny(o.kind, o.wide, o.neg, rc, a, b, c) {
            // result lane: same index and size, or for the narrowing 4 bytes at 4 * i (+ 8 for FCVTN2)
            let (size, at) = if o.kind == tiny::CVT { (4, 4 * i + 8 * usize::from(o.upper)) } else if o.wide { (8, 8 * i) } else { (4, 4 * i) };
            let mut raw = [0u8; 8];
            raw[..size].copy_from_slice(&dest[at..at + size]);
            let (v, sign) = (u64::from_le_bytes(raw), 1u64 << (8 * size - 1));
            if v & !sign == if size == 4 { 0x0080_0000 } else { 0x0010_0000_0000_0000 } {
                dest[at..at + size].copy_from_slice(&(v & sign).to_le_bytes()[..size]);
                under = true;
                continue;
            }
        }
        under |= tiny::lane_underflow(o.kind, o.wide, o.neg, rc, a, b, c);
    }
    rerun & !UFC | if under { UFC } else { 0 }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn patched(w: u32) -> Op {
        match classify(w) {
            Some(Class::Patch(o)) => o,
            c => panic!("{w:#x}: {c:?}"),
        }
    }

    /// Words assembled by clang (`fmul s1, s2, s3`, ...): the table's decode of each.
    #[test]
    fn the_table_decodes_what_the_assembler_emits() {
        let o = patched(0x1e23_0841); // fmul s1, s2, s3
        assert_eq!((o.kind, o.wide, o.lanes, o.rn, o.rm), (tiny::MUL, false, 1, 2, 3));
        assert_eq!(patched(0x1e63_8841).kind, tiny::MUL); // fnmul d1, d2, d3
        let o = patched(0x1e26_18a4); // fdiv s4, s5, s6
        assert_eq!((o.kind, o.rn, o.rm), (tiny::DIV, 5, 6));
        let o = patched(0x1f43_9041); // fmsub d1, d2, d3, d4
        assert_eq!((o.kind, o.wide, o.neg, o.rn, o.rm, o.ra), (tiny::FMA, true, (true, false), 2, 3, 4));
        assert_eq!(patched(0x1f63_1041).neg, (true, true)); // fnmadd d1, d2, d3, d4
        assert_eq!(patched(0x1f23_9041).neg, (false, true)); // fnmsub s1, s2, s3, s4
        assert_eq!(patched(0x1f03_1041).neg, (false, false)); // fmadd s1, s2, s3, s4
        assert_eq!((patched(0x1e62_4041).kind, patched(0x1e62_4041).wide), (tiny::CVT, true)); // fcvt s1, d2
        assert_eq!(patched(0x1e23_c041).kind, tiny::CVT16); // fcvt h1, s2
        assert_eq!(classify(0x1e63_c041), None); // fcvt h1, d2: x86 has no such conversion
        for keep in [
            0x1e23_4841, // fmax
            0x1e22_c041, 0x1ee2_4041, 0x1ee2_c041, // fcvt widening
            0x6ea3_d441, // fabd (vector)
            0x1e62_2020, 0x1e22_2030, 0x1e20_2038, // fcmp, fcmpe (register, zero)
            0x4e23_e441, 0x6ee3_e441, 0x6e23_e441, 0x6ea3_ec41, 0x5e23_e441, 0x7ea3_e441, // fcmeq, fcmgt, fcmge, facgt (vector, scalar)
            0x4ea0_d841, 0x4ea0_e841, 0x6ee0_d841, 0x5ee0_d841, // fcmeq, fcmlt, fcmle with zero (vector, scalar)
            0x1e64_c041, 0x1e27_4041, 0x6e21_9841, 0x6ee1_9841, 0x6e21_e841, 0x1e68_4041, // frintp, frintx, frintx, frinti, frint32x, frint32z
            0x1e78_0041, 0x4ea1_b841, 0x9e28_0041, 0x5ea1_b841, // fcvtzs, fcvtzs, fcvtps, fcvtzs (scalar vector form)
        ] {
            assert_eq!(classify(keep), Some(Class::Keep), "{keep:#x}");
        }
        let o = patched(0x6e23_dc41); // fmul v1.4s, v2.4s, v3.4s
        assert_eq!((o.kind, o.wide, o.lanes, o.rn, o.rm), (tiny::MUL, false, 4, 2, 3));
        assert_eq!(patched(0x2e23_dc41).lanes, 2); // .2s
        assert_eq!((patched(0x6e63_dc41).wide, patched(0x6e63_dc41).lanes), (true, 2)); // .2d
        assert_eq!(patched(0x6e63_fc41).kind, tiny::DIV);
        let o = patched(0x4e23_cc41); // fmla v1.4s, v2.4s, v3.4s: the addend is the destination
        assert_eq!((o.kind, o.neg, o.ra), (tiny::FMA, (false, false), 1));
        assert_eq!(patched(0x4ee3_cc41).neg, (true, false)); // fmls v1.2d, v2.2d, v3.2d
        let o = patched(0x4fa3_1841); // fmla v1.4s, v2.4s, v3.s[3]
        assert_eq!((o.kind, o.rm, o.index, o.lanes), (tiny::FMA, 3, Some(3), 4));
        let o = patched(0x4fd3_5841); // fmls v1.2d, v2.2d, v19.d[1]
        assert_eq!((o.kind, o.neg, o.rm, o.index, o.wide), (tiny::FMA, (true, false), 19, Some(1), true));
        let o = patched(0x4f93_9841); // fmul v1.4s, v2.4s, v19.s[2]
        assert_eq!((o.kind, o.rm, o.index), (tiny::MUL, 19, Some(2)));
        assert_eq!(patched(0x4fc3_9841).index, Some(1)); // fmul v1.2d, v2.2d, v3.d[1]
        let o = patched(0x5fa3_1041); // fmla s1, s2, v3.s[1]
        assert_eq!((o.kind, o.lanes, o.index), (tiny::FMA, 1, Some(1)));
        assert_eq!((patched(0x5fd3_9841).lanes, patched(0x5fd3_9841).kind), (1, tiny::MUL)); // fmul d1, d2, v19.d[1]
        assert_eq!((patched(0x0e21_6841).kind, patched(0x0e21_6841).lanes), (tiny::CVT16, 4)); // fcvtn v1.4h, v2.4s
        assert_eq!(patched(0x4e21_6841).kind, tiny::CVT16); // fcvtn2
        assert_eq!((patched(0x0e61_6841).kind, patched(0x0e61_6841).lanes), (tiny::CVT, 2)); // fcvtn v1.2s, v2.2d
        for (w, kind, nan) in [(0x1e63_2841, 0, Nan::Bin), (0x1e23_3841, 0, Nan::Bin), (0x4e23_d441, 0, Nan::Bin), (0x4ee3_d441, 0, Nan::Bin), (0x6e23_d441, 0, Nan::Pair), (0x7e30_d841, 0, Nan::Pair), (0x1e21_c041, 0, Nan::Un), (0x6ea1_f841, 0, Nan::Un)] {
            let o = patched(w);
            assert_eq!((o.kind, o.nan), (kind, nan), "{w:#x}");
        }
        // Unknown: fmulx, frecps, fneg, an integer add, a load.
        for unknown in [0x4e23_dc41, 0x4e23_fc41, 0x6ea0_f841, 0x4ea3_8441, 0xf940_0000] {
            assert_eq!(classify(unknown), None, "{unknown:#x}");
        }
    }

    fn q(lanes: [u32; 4]) -> [u8; 16] {
        let mut b = [0u8; 16];
        for (k, l) in lanes.iter().enumerate() {
            b[4 * k..4 * k + 4].copy_from_slice(&l.to_le_bytes());
        }
        b
    }

    /// UFC is decided per lane: a band lane (Arm raises UFC, x86 does not) beside an exact tiny lane stays clear; beside a real underflow
    /// it stays set; and flags other than UFC (a NaN lane's IOC) are the hardware's.
    #[test]
    fn ufc_is_decided_per_lane() {
        let one = 0x3f80_0000u32;
        let half = 0x3f00_0000u32;
        // The band: 18631 * 2^-75 times 1801 * 2^-76 is (2^25 - 1) * 2^-151 = MIN_NORMAL - 2^-151, exactly the midpoint x86 rounds up to
        // MIN_NORMAL (not tiny) in nearest-even, and up for a positive value; Arm judges before rounding and raises UFC.
        let band = ((18631.0f32 * 2f32.powi(-75)).to_bits(), (1801.0f32 * 2f32.powi(-76)).to_bits());
        let exact_tiny = (0x0000_0400u32, one); // denormal * 1.0: exact
        let real = (0x0000_0003u32, half); // 3 * 2^-149 * 0.5 = 1.5 * 2^-149: inexact and tiny
        let mul = Class::Patch(Op {
            kind: tiny::MUL,
            nan: Nan::Bin,
            wide: false,
            neg: (false, false),
            lanes: 4,
            rn: 0,
            rm: 1,
            ra: 2,
            index: None,
            rd: 0,
            upper: false,
        });
        let regs = |lanes: [(u32, u32); 4]| {
            [q(lanes.map(|l| l.0)), q(lanes.map(|l| l.1)), [0; 16]]
        };
        let ioc = 1;
        let none = (one, one);
        // band + exact tiny: x86 raises nothing; the re-run's UFC goes
        let r = regs([band, exact_tiny, none, none]);
        assert_eq!(patch(mul, 0, &r, UFC | 0x10, false, &mut [0; 16], false), 0x10);
        // band + real underflow: UFC stays
        let r = regs([band, real, none, none]);
        assert_eq!(patch(mul, 0, &r, UFC | 0x10, false, &mut [0; 16], false), UFC | 0x10);
        // a NaN lane's IOC next to a band lane: IOC kept, UFC removed
        let r = regs([(0x7fc0_0000, one), band, none, none]);
        assert_eq!(patch(mul, 0, &r, UFC | 0x10 | ioc, false, &mut [0; 16], false), 0x10 | ioc);
        // the band depends on the rounding mode: up (x86 rc 2) rounds a positive band value to MIN_NORMAL too, down (1) and zero (3) do not
        let r = regs([band, none, none, none]);
        assert_eq!([0, 1, 2, 3].map(|rc| patch(mul, rc, &r, UFC | 0x10, false, &mut [0; 16], false) & UFC), [0, UFC, 0, UFC]);
        // no UFC in the re-run: nothing to do (and no model run)
        assert_eq!(patch(mul, 0, &r, 0x10, false, &mut [0; 16], false), 0x10);
        assert_eq!(patch(Class::Keep, 0, &r, UFC, false, &mut [0; 16], false), UFC);
        // a scalar word looks at lane 0 only
        let scalar = Class::Patch(Op { lanes: 1, ..match mul { Class::Patch(o) => o, _ => unreachable!() } });
        let r = regs([none, band, none, none]);
        assert_eq!(patch(scalar, 0, &r, UFC | 0x10, false, &mut [0; 16], false), 0x10);
    }
}

/// The native oracle (AFP accurate plan, chunk 3 gate): every patched form is run on this machine twice, the way the trap runs it
/// (FPCR with AH, NEP and FIZ clear and FZ = 0, the M2's floating point: the re-run) and under the M4's own AFP (AH = 1: tininess after
/// rounding, which is x86's), on random operands in the classes that matter (the band below MIN_NORMAL, real and exact tiny results,
/// NaNs, infinities, zeros, denormals and anything), in every rounding mode. [`patch`] of the re-run's flags, decoded from the
/// instruction's own word, must equal AFP's flags (IOC, DZC, OFC, UFC, IXC) bit for bit.
#[cfg(all(test, target_arch = "aarch64"))]
mod native {
    extern crate std;
    use super::*;
    use core::arch::asm;
    use crate::execution::mxcsr_flags;
    use std::eprintln;

    type Form = fn(u64, &[[u8; 16]; 4]) -> ([u8; 16], u64, u32);
    /// A form: runs `insn` on v1..v4 (loaded from `q`) under `fpcr`, FPSR cleared; its result v1, FPSR, and its own word.
    macro_rules! form {
        ($insn:literal) => {{
            fn run(fpcr: u64, q: &[[u8; 16]; 4]) -> ([u8; 16], u64, u32) {
                let (mut out, fpsr, at) = ([0u8; 16], 0u64, 0u64);
                let (mut fpsr, mut at) = (fpsr, at);
                // SAFETY: loads and stores through `q` and `out` (16-byte values); FPCR is zero again at the end; the label is the word read below.
                unsafe {
                    asm!(
                        "ldp q1, q2, [{q}]", "ldp q3, q4, [{q}, #32]",
                        "msr fpcr, {c}", "msr fpsr, xzr",
                        "adr {a}, 2f",
                        concat!("2: ", $insn),
                        "mrs {s}, fpsr", "msr fpcr, xzr",
                        "str q1, [{o}]",
                        q = in(reg) q.as_ptr(), c = in(reg) fpcr, o = in(reg) out.as_mut_ptr(),
                        a = out(reg) at, s = out(reg) fpsr,
                        out("v1") _, out("v2") _, out("v3") _, out("v4") _,
                    );
                    (out, fpsr, (at as *const u32).read())
                }
            }
            run as Form
        }};
    }

    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            self.0
        }
        fn below(&mut self, n: u64) -> u64 {
            self.next() % n
        }
    }

    /// Source element parameters: (bytes, p, emin, bias).
    fn params(e: usize) -> (u32, i64, i64) {
        if e == 4 { (24, -126, 127) } else { (53, -1022, 1023) }
    }
    fn pack(e: usize, sign: u64, field: i64, frac: u64) -> u64 {
        let (p, ..) = params(e);
        sign << (e * 8 - 1) | (field.clamp(0, if e == 4 { 255 } else { 2047 }) as u64) << (p - 1) | frac & ((1 << (p - 1)) - 1)
    }
    /// A value of `e`-byte elements from one of the classes the oracle wants.
    fn value(r: &mut Rng, e: usize) -> u64 {
        let (p, emin, bias) = params(e);
        let sign = r.below(2);
        let frac = r.next();
        let top = if e == 4 { 255 } else { 2047 };
        match r.below(12) {
            0 => r.next() & (u64::MAX >> (64 - 8 * e)),
            1 => pack(e, sign, [0, top, top, 1][r.below(4) as usize], [0, 0, 1 << (p - 2), 0][r.below(4) as usize] | r.below(2)),
            2 | 3 => pack(e, sign, 0, frac >> r.below(20)),                       // zero or denormal
            4 | 5 => pack(e, sign, 1 + r.below(6) as i64, frac),                  // just above MIN_NORMAL
            6 => pack(e, sign, 1, 0),                                             // MIN_NORMAL itself
            7 => pack(e, sign, bias + emin + (e as i64 * 4) - r.below(8) as i64 - 3, frac), // around a square root of the band
            8 => pack(e, sign, 0, 1 << r.below(u64::from(p) - 1)),                // a power of two denormal
            9 => pack(e, sign, bias - r.below(40) as i64 + 10, frac),             // moderate
            10 => pack(e, sign, bias, 0),                                         // 1.0 (exact results)
            _ => pack(e, sign, bias - 1 - r.below(3) as i64, frac & 0),           // 0.5, 0.25: exact scalings
        }
    }
    /// `a` such that a * b is within a few units of the last place of 2^emin (the band below MIN_NORMAL), for a normal `b`, elements of
    /// `e` bytes; `None` if `b` is not a normal number or `a` would not be one.
    fn band_partner(r: &mut Rng, e: usize, b: u64) -> Option<u64> {
        let (p, emin, bias) = params(e);
        let field = (b >> (p - 1) & ((1 << (e * 8 - p as usize)) - 1)) as i64;
        if field == 0 || field == (if e == 4 { 255 } else { 2047 }) {
            return None;
        }
        let mb = u128::from(b & ((1 << (p - 1)) - 1) | 1 << (p - 1));
        let eb = field - bias - i64::from(p - 1);
        let ma = ((1u128 << (2 * p - 1)) / mb) as i128 + r.below(7) as i128 - 3;
        let ea = emin - i64::from(2 * p - 1) - eb;
        let af = ea + i64::from(p - 1) + bias;
        if ma < 1 << (p - 1) || ma >= 1 << p || af < 1 || af > (if e == 4 { 254 } else { 2046 }) {
            return None;
        }
        Some(pack(e, r.below(2), af, ma as u64))
    }
    fn put(q: &mut [u8; 16], e: usize, lane: usize, v: u64) {
        q[e * lane..e * lane + e].copy_from_slice(&v.to_le_bytes()[..e]);
    }
    fn get(q: &[u8; 16], e: usize, lane: usize) -> u64 {
        let mut b = [0u8; 8];
        b[..e].copy_from_slice(&q[e * lane..e * lane + e]);
        u64::from_le_bytes(b)
    }

    /// Operands for one trial: v1 (the vector FMLA's addend), v2, v3 (v4: a scalar FMA's addend). Lanes pair the registers in the classes;
    /// `e` is the source element size and `band` makes lane `l`'s second register the band partner of the first.
    fn operands(r: &mut Rng, e: usize, narrowing: bool) -> [[u8; 16]; 4] {
        let mut q = [[0u8; 16]; 4];
        for lane in 0..16 / e {
            for reg in 0..4 {
                put(&mut q[reg], e, lane, value(r, e));
            }
            // an addend near zero, or opposite to the product, half the time
            if r.below(2) == 0 {
                put(&mut q[0], e, lane, [0, 1, pack(e, 1, 0, 1)][r.below(3) as usize]);
                put(&mut q[3], e, lane, [0, 1, pack(e, 1, 0, 1)][r.below(3) as usize]);
            }
            // FMA cancellation: an addend that cancels the (rounded) product up to a few units of the last place, so the sum is tiny and
            // exact or inexact by the product's low bits and the sign of the leftover
            if r.below(4) == 0 && !narrowing {
                let (a, b) = (get(&q[1], e, lane), get(&q[2], e, lane));
                let prod = if e == 4 { u64::from((f32::from_bits(a as u32) * f32::from_bits(b as u32)).to_bits()) } else { (f64::from_bits(a) * f64::from_bits(b)).to_bits() };
                let c = (prod ^ 1 << (8 * e - 1)).wrapping_add(r.below(5)).wrapping_sub(2) & (u64::MAX >> (64 - 8 * e));
                put(&mut q[0], e, lane, c);
                put(&mut q[3], e, lane, c);
            }
            if r.below(3) != 0 && !narrowing {
                if let Some(a) = band_partner(r, e, get(&q[2], e, lane)) {
                    put(&mut q[1], e, lane, a);
                }
            }
            if narrowing && r.below(2) == 0 {
                // a source just under the destination's MIN_NORMAL: binary64 -> binary32 (2^-126) or binary32 -> binary16 (2^-14)
                let (field, frac) = if e == 8 { (1023 - 127, (1u64 << 52) - 1 - r.below(1 << 29)) } else { (127 - 15, (1u64 << 23) - 1 - r.below(1 << 13)) };
                put(&mut q[1], e, lane, pack(e, r.below(2), field, frac));
            }
        }
        q
    }

    fn check(name: &str, f: Form, e: usize, narrowing: bool, trials: u32, expect_patch: bool) {
        let mut r = Rng(0x9e37_79b9_7f4a_7c15 ^ name.len() as u64 * 7919);
        let (mut removed, mut kept) = (0u32, 0u32);
        for _ in 0..trials {
            let q = operands(&mut r, e, narrowing);
            for rmode in 0..4u64 {
                let fpcr = rmode << 22;
                let (_, rerun, word) = f(fpcr, &q);
                let (_, afp, _) = f(fpcr | 1 << 1 | 1 << 2, &q);
                let class = classify(word).unwrap_or_else(|| panic!("{name}: {word:#x} unknown"));
                // the registers the table names, as the trap would have saved them (v1..v4 are `q[0..4]`)
                let mut saved = [[0u8; 16]; 3];
                if let Class::Patch(o) = class {
                    for (s, reg) in saved.iter_mut().zip(o.sources()) {
                        *s = q[(usize::from(reg).wrapping_sub(1)).min(3)];
                    }
                }
                let got = patch(class, x86_rc(fpcr), &saved, rerun, false, &mut [0; 16], false);
                // (FMA's IOC for infinity times zero plus a QNaN is deviation D, chunk 4's: AH = 1 raises none)
                // (the estimates differ under AH = 1 in OFC/IXC for a denormal input; FEX's RCP/RSQRT here use fdiv/fsqrt, only UFC matters)
                let mask = if name.starts_with("fr") { UFC } else if matches!(class, Class::Patch(Op { kind: tiny::FMA, .. })) { 0x1e } else { 0x1f };
                assert_eq!(got & mask, afp & mask, "{name} rmode {rmode} word {word:#x} rerun {rerun:#x} afp {afp:#x}\nq {q:x?}");
                // (and the patch only ever touches UFC)
                assert_eq!(got & !UFC, rerun & !UFC, "{name}");
                if rerun & UFC != 0 {
                    if got & UFC == 0 { removed += 1 } else { kept += 1 }
                }
            }
        }
        eprintln!("{name}: {trials} trials x 4 modes, UFC kept {kept}, removed {removed}");
        if expect_patch && !narrowing {
            assert!(kept > 0, "{name}: no real underflow met");
        }
    }

    /// Under FTZ alone (DAZ clear, effective FTZ: FZ stays 0 and software flushes zero-exponent results, row 5) the trap's patch also flushes
    /// a band lane to +-0: the whole destination and the x86 flags (UE, PE) must equal the M4's AH = 1 run with FZ = 1 (the hardware's x86-like
    /// flush). Row 5 is emulated here on every written lane: a result with a zero exponent and a nonzero mantissa becomes +-0 and raises UFC.
    fn check_ftz(name: &str, f: Form, e: usize, re: usize, lanes: usize, narrowing: bool, trials: u32) {
        let mut r = Rng(0xfeed_f00d_1234_5678 ^ name.len() as u64 * 104_729);
        let (mut flushed, mut band) = (0u32, 0u32);
        let mxcsr = 0x1f80 | 1 << 15;
        let nan = |v: u64| (v >> (8 * re - 1 - if re == 4 { 8 } else { 11 })) & ((1 << if re == 4 { 8 } else { 11 }) - 1) == (1 << if re == 4 { 8 } else { 11 }) - 1 && v & ((1 << if re == 4 { 23 } else { 52 }) - 1) != 0;
        for _ in 0..trials {
            let q = operands(&mut r, e, narrowing);
            for rmode in 0..4u64 {
                let fpcr = rmode << 22;
                let (mut out, rerun, word) = f(fpcr, &q);
                let (afp_out, afp, _) = f(fpcr | 1 << 1 | 1 << 2 | 1 << 24, &q);
                let class = classify(word).unwrap();
                let mut saved = [[0u8; 16]; 3];
                if let Class::Patch(o) = class {
                    for (s, reg) in saved.iter_mut().zip(o.sources()) {
                        *s = q[usize::from(reg) - 1];
                    }
                }
                let before = out;
                let mut got = patch(class, x86_rc(fpcr), &saved, rerun, true, &mut out, false);
                band += u32::from(before != out);
                for k in 0..16 / re {
                    let mut raw = [0u8; 8];
                    raw[..re].copy_from_slice(&out[re * k..re * k + re]);
                    let v = u64::from_le_bytes(raw);
                    let (exp_bits, man_bits) = if re == 4 { (8, 23) } else { (11, 52) };
                    if v >> man_bits & ((1 << exp_bits) - 1) == 0 && v & ((1 << man_bits) - 1) != 0 {
                        out[re * k..re * k + re].copy_from_slice(&(v & 1 << (8 * re - 1)).to_le_bytes()[..re]);
                        got |= UFC;
                        flushed += 1;
                    }
                }
                let m = if matches!(class, Class::Patch(Op { kind: tiny::FMA, .. })) { !3 } else { !2 };
                let ours = mxcsr_flags(got, mxcsr, true) & m;
                let theirs = mxcsr_flags(afp, mxcsr, false) & m;
                assert_eq!(ours, theirs, "{name} rmode {rmode} word {word:#x} flags\nq {q:x?}");
                for k in 0..lanes {
                    let at = |o: &[u8; 16]| {
                        let mut raw = [0u8; 8];
                        raw[..re].copy_from_slice(&o[re * k..re * k + re]);
                        u64::from_le_bytes(raw)
                    };
                    if nan(at(&out)) || nan(at(&afp_out)) {
                        continue;
                    }
                    assert_eq!(at(&out), at(&afp_out), "{name} rmode {rmode} word {word:#x} lane {k}\nq {q:x?}");
                }
            }
        }
        eprintln!("{name}: FTZ, {trials} trials x 4 modes, {band} band lanes flushed by the patch, {flushed} by row 5");
    }

    #[test]
    fn under_ftz_alone_the_patch_flushes_the_band_like_the_m4s_fz() {
        let n: u32 = std::env::var("ORACLE_TRIALS").ok().and_then(|v| v.parse().ok()).unwrap_or(4000);
        let forms: &[(&str, Form, usize, usize, usize, bool)] = &[
            ("fmul s", form!("fmul s1, s2, s3"), 4, 4, 1, false),
            ("fmul d", form!("fmul d1, d2, d3"), 8, 8, 1, false),
            ("fmul .4s", form!("fmul v1.4s, v2.4s, v3.4s"), 4, 4, 4, false),
            ("fmul .2d", form!("fmul v1.2d, v2.2d, v3.2d"), 8, 8, 2, false),
            ("fmul .4s[2]", form!("fmul v1.4s, v2.4s, v3.s[2]"), 4, 4, 4, false),
            ("fdiv .4s", form!("fdiv v1.4s, v2.4s, v3.4s"), 4, 4, 4, false),
            ("fdiv d", form!("fdiv d1, d2, d3"), 8, 8, 1, false),
            ("fmadd s", form!("fmadd s1, s2, s3, s4"), 4, 4, 1, false),
            ("fnmsub d", form!("fnmsub d1, d2, d3, d4"), 8, 8, 1, false),
            ("fmla .4s", form!("fmla v1.4s, v2.4s, v3.4s"), 4, 4, 4, false),
            ("fmls .2d", form!("fmls v1.2d, v2.2d, v3.2d"), 8, 8, 2, false),
            ("fmla .4s[3]", form!("fmla v1.4s, v2.4s, v3.s[3]"), 4, 4, 4, false),
            ("fcvt s,d", form!("fcvt s1, d2"), 8, 4, 1, true),
            ("fcvtn .2s,.2d", form!("fcvtn v1.2s, v2.2d"), 8, 4, 2, true),
            ("fadd .4s", form!("fadd v1.4s, v2.4s, v3.4s"), 4, 4, 4, false),
            ("fsub d", form!("fsub d1, d2, d3"), 8, 8, 1, false),
        ];
        for &(name, f, e, re, lanes, narrowing) in forms {
            check_ftz(name, f, e, re, lanes, narrowing, n);
        }
        // the reviewer's case: 4095 * 2^-75 times 4097 * 2^-75 is 2^-126 - 2^-150, which x86 rounds below MIN_NORMAL (flush to 0) and Arm's
        // denormal grid rounds up to it
        let (a, b) = ((4095.0f32 * 2f32.powi(-75)).to_bits(), (4097.0f32 * 2f32.powi(-75)).to_bits());
        let mut q = [[0u8; 16]; 4];
        put(&mut q[1], 4, 0, u64::from(a));
        put(&mut q[2], 4, 0, u64::from(b));
        let f = form!("fmul s1, s2, s3");
        let (mut out, rerun, word) = f(0, &q);
        assert_eq!(get(&out, 4, 0), 0x0080_0000);
        let mut saved = [[0u8; 16]; 3];
        let Some(Class::Patch(o)) = classify(word) else { panic!() };
        for (s, reg) in saved.iter_mut().zip(o.sources()) {
            *s = q[usize::from(reg) - 1];
        }
        let got = patch(Class::Patch(o), 0, &saved, rerun, true, &mut out, false);
        assert_eq!((get(&out, 4, 0), got & UFC), (0, UFC));
    }

    #[test]
    fn patched_words_match_the_m4s_afp_flags() {
        let n: u32 = std::env::var("ORACLE_TRIALS").ok().and_then(|v| v.parse().ok()).unwrap_or(4000);
        let patched: &[(&str, Form, usize, bool)] = &[
            ("fmul s", form!("fmul s1, s2, s3"), 4, false),
            ("fmul d", form!("fmul d1, d2, d3"), 8, false),
            ("fnmul d", form!("fnmul d1, d2, d3"), 8, false),
            ("fmul .4s", form!("fmul v1.4s, v2.4s, v3.4s"), 4, false),
            ("fmul .2s", form!("fmul v1.2s, v2.2s, v3.2s"), 4, false),
            ("fmul .2d", form!("fmul v1.2d, v2.2d, v3.2d"), 8, false),
            ("fmul .4s[2]", form!("fmul v1.4s, v2.4s, v3.s[2]"), 4, false),
            ("fmul .2d[1]", form!("fmul v1.2d, v2.2d, v3.d[1]"), 8, false),
            ("fmul d[1]", form!("fmul d1, d2, v3.d[1]"), 8, false),
            ("fmul s[1]", form!("fmul s1, s2, v3.s[1]"), 4, false),
            ("fdiv .2s", form!("fdiv v1.2s, v2.2s, v3.2s"), 4, false),
            ("fmadd d", form!("fmadd d1, d2, d3, d4"), 8, false),
            ("fdiv s", form!("fdiv s1, s2, s3"), 4, false),
            ("fdiv d", form!("fdiv d1, d2, d3"), 8, false),
            ("fdiv .4s", form!("fdiv v1.4s, v2.4s, v3.4s"), 4, false),
            ("fdiv .2d", form!("fdiv v1.2d, v2.2d, v3.2d"), 8, false),
            ("fmadd s", form!("fmadd s1, s2, s3, s4"), 4, false),
            ("fmsub d", form!("fmsub d1, d2, d3, d4"), 8, false),
            ("fnmadd s", form!("fnmadd s1, s2, s3, s4"), 4, false),
            ("fnmsub d", form!("fnmsub d1, d2, d3, d4"), 8, false),
            ("fmla .4s", form!("fmla v1.4s, v2.4s, v3.4s"), 4, false),
            ("fmls .4s", form!("fmls v1.4s, v2.4s, v3.4s"), 4, false),
            ("fmls .2d", form!("fmls v1.2d, v2.2d, v3.2d"), 8, false),
            ("fmla .2s", form!("fmla v1.2s, v2.2s, v3.2s"), 4, false),
            ("fmla .4s[3]", form!("fmla v1.4s, v2.4s, v3.s[3]"), 4, false),
            ("fmls .2d[1]", form!("fmls v1.2d, v2.2d, v3.d[1]"), 8, false),
            ("fmla s[1]", form!("fmla s1, s2, v3.s[1]"), 4, false),
            ("fmla d[0]", form!("fmla d1, d2, v3.d[0]"), 8, false),
            ("fcvt s,d", form!("fcvt s1, d2"), 8, true),
            ("fcvtn .2s,.2d", form!("fcvtn v1.2s, v2.2d"), 8, true),
            ("fcvtn2 .4s,.2d", form!("fcvtn2 v1.4s, v2.2d"), 8, true),
            ("fcvt h,s", form!("fcvt h1, s2"), 4, true),
            ("fcvtn .4h,.4s", form!("fcvtn v1.4h, v2.4s"), 4, true),
            ("fcvtn2 .8h,.4s", form!("fcvtn2 v1.8h, v2.4s"), 4, true),
        ];
        for &(name, f, e, narrowing) in patched {
            check(name, f, e, narrowing, n, true);
        }
        let kept: &[(&str, Form, usize)] = &[
            ("fadd d", form!("fadd d1, d2, d3"), 8),
            ("fsub s", form!("fsub s1, s2, s3"), 4),
            ("fadd .4s", form!("fadd v1.4s, v2.4s, v3.4s"), 4),
            ("fsub .2d", form!("fsub v1.2d, v2.2d, v3.2d"), 8),
            ("faddp .4s", form!("faddp v1.4s, v2.4s, v3.4s"), 4),
            ("fcadd .2d", form!("fcadd v1.2d, v2.2d, v3.2d, #90"), 8),
            ("fcadd .4s #270", form!("fcadd v1.4s, v2.4s, v3.4s, #270"), 4),
            ("faddp s", form!("faddp s1, v2.2s"), 4),
            ("fcvtl .4s", form!("fcvtl v1.4s, v2.4h"), 4),
            ("frecpe .4s", form!("frecpe v1.4s, v2.4s"), 4),
            ("frsqrte .2d", form!("frsqrte v1.2d, v2.2d"), 8),
            ("fabd .4s", form!("fabd v1.4s, v2.4s, v3.4s"), 4),
        ];
        for &(name, f, e) in kept {
            check(name, f, e, false, n / 4, false);
        }
    }

    /// NaN-heavy operands: every lane of v1..v4 is one of the special classes (quiet and signalling NaNs of both signs with distinct payloads,
    /// infinities, zeros, ones, a denormal) or random, so that every operand position holds NaNs, several at once, and infinity times zero.
    fn nan_operands(r: &mut Rng, e: usize) -> [[u8; 16]; 4] {
        let (p, ..) = params(e);
        let top = if e == 4 { 255 } else { 2047 };
        let mut q = [[0u8; 16]; 4];
        for lane in 0..16 / e {
            for reg in 0..4 {
                let (sign, payload) = (r.below(2), 1 + r.below(0xffff));
                let v = match r.below(11) {
                    0 | 1 => pack(e, sign, top, 1 << (p - 2) | payload), // quiet NaN
                    2 => pack(e, sign, top, payload),                    // signalling NaN
                    3 => pack(e, sign, top, 0),                          // infinity
                    4 => pack(e, sign, 0, 0),                            // zero
                    5 | 6 => pack(e, sign, if e == 4 { 127 } else { 1023 }, 0), // one
                    7 => pack(e, sign, 0, payload),                      // denormal
                    8 => pack(e, sign, if e == 4 { 130 } else { 1030 }, r.next()), // a few
                    _ => r.next() & (u64::MAX >> (64 - 8 * e)),
                };
                put(&mut q[reg], e, lane, v);
            }
        }
        q
    }

    /// AFP accurate plan chunk 4: the word re-run as the trap runs it (AH = 0, no traps) then patched with accurate mode's NaN rules equals
    /// the M4's AH = 1 run: every result lane bit for bit and the flags IOC, DZC, OFC, UFC, IXC; for an FMA also after the check
    /// ([`fma_check`]) that follows it in generated code. Operands as the table saves them.
    fn check_nan(name: &str, f: Form, e: usize, trials: u32) {
        let mut r = Rng(0x2545_f491_4f6c_dd1d ^ name.len() as u64 * 6151);
        let (mut nan_lanes, mut ioc_removed, mut checked, mut quiet_checks) = (0u32, 0u32, 0u32, 0u32);
        for _ in 0..trials {
            let q = nan_operands(&mut r, e);
            for rmode in 0..4u64 {
                let fpcr = rmode << 22;
                let (mut out, rerun, word) = f(fpcr, &q);
                let (afp_out, afp, _) = f(fpcr | 1 << 1 | 1 << 2, &q);
                let class = classify(word).unwrap_or_else(|| panic!("{name}: {word:#x} unknown"));
                let Class::Patch(o) = class else { panic!("{name}: {word:#x} is not patched") };
                let mut saved = [[0u8; 16]; 3];
                for (s, reg) in saved.iter_mut().zip(o.sources()) {
                    *s = q[usize::from(reg).wrapping_sub(1).min(3)];
                }
                // An FMA that raised no IOC does not trap at itself (two quiet NaNs, a negated NaN): only the check that follows it is taken
                if o.kind == tiny::FMA && o.index.is_none() && rerun & IOC == 0 {
                    let (lanes, wide) = (usize::from(o.lanes), o.wide);
                    let mut raw = out;
                    fma_check(wide, lanes, &saved, &mut raw);
                    let bytes = lanes * if wide { 8 } else { 4 };
                    assert_eq!(raw[..bytes], afp_out[..bytes], "{name} untrapped rmode {rmode} word {word:#x}\nq {q:x?}");
                    quiet_checks += 1;
                }
                let got = patch(class, x86_rc(fpcr), &saved, rerun, false, &mut out, true);
                if o.kind == tiny::FMA {
                    let (lanes, wide) = (usize::from(o.lanes), o.wide);
                    let mut by_check = out;
                    fma_check(wide, lanes, &saved, &mut by_check);
                    // (the check only touches lanes with a NaN operand: the by-element forms' index is not part of it)
                    if o.index.is_none() {
                        let bytes = lanes * if wide { 8 } else { 4 };
                        assert_eq!(by_check[..bytes], afp_out[..bytes], "{name} check rmode {rmode} word {word:#x}\nq {q:x?}");
                        checked += 1;
                    }
                }
                assert_eq!(got & 0x1f, afp & 0x1f, "{name} rmode {rmode} word {word:#x} rerun {rerun:#x} afp {afp:#x}\nq {q:x?}");
                // every lane (the NaN rules differ only in NaN lanes, but the whole register must match)
                // (the lanes the word writes: a scalar word under NEP keeps the destination's upper lanes, which a word without it zeroes)
                let bytes = usize::from(o.lanes) * if o.wide { 8 } else { 4 };
                assert_eq!(out[..bytes], afp_out[..bytes], "{name} rmode {rmode} word {word:#x}\nq {q:x?}");
                nan_lanes += 1;
                ioc_removed += u32::from(rerun & IOC != 0 && got & IOC == 0);
            }
        }
        eprintln!("{name}: {trials} trials x 4 modes ({nan_lanes} runs), IOC removed {ioc_removed}, check ran {checked}, of which without a trap at the FMA {quiet_checks}");
    }

    #[test]
    fn patched_nan_lanes_match_the_m4s_ah1() {
        let n: u32 = std::env::var("ORACLE_TRIALS").ok().and_then(|v| v.parse().ok()).unwrap_or(3000);
        let forms: &[(&str, Form, usize)] = &[
            ("fmul s", form!("fmul s1, s2, s3"), 4),
            ("fmul d", form!("fmul d1, d2, d3"), 8),
            ("fnmul d", form!("fnmul d1, d2, d3"), 8),
            ("fmul .4s", form!("fmul v1.4s, v2.4s, v3.4s"), 4),
            ("fmul .2d", form!("fmul v1.2d, v2.2d, v3.2d"), 8),
            ("fmul .4s[2]", form!("fmul v1.4s, v2.4s, v3.s[2]"), 4),
            ("fdiv s", form!("fdiv s1, s2, s3"), 4),
            ("fdiv .2d", form!("fdiv v1.2d, v2.2d, v3.2d"), 8),
            ("fdiv .4s", form!("fdiv v1.4s, v2.4s, v3.4s"), 4),
            ("fadd d", form!("fadd d1, d2, d3"), 8),
            ("fsub s", form!("fsub s1, s2, s3"), 4),
            ("fadd .4s", form!("fadd v1.4s, v2.4s, v3.4s"), 4),
            ("fsub .2d", form!("fsub v1.2d, v2.2d, v3.2d"), 8),
            ("faddp .4s", form!("faddp v1.4s, v2.4s, v3.4s"), 4),
            ("faddp .2d", form!("faddp v1.2d, v2.2d, v3.2d"), 8),
            ("faddp s", form!("faddp s1, v2.2s"), 4),
            ("faddp d", form!("faddp d1, v2.2d"), 8),
            ("fsqrt s", form!("fsqrt s1, s2"), 4),
            ("fsqrt d", form!("fsqrt d1, d2"), 8),
            ("fsqrt .4s", form!("fsqrt v1.4s, v2.4s"), 4),
            ("fsqrt .2d", form!("fsqrt v1.2d, v2.2d"), 8),
            ("fmadd s", form!("fmadd s1, s2, s3, s4"), 4),
            ("fmadd d", form!("fmadd d1, d2, d3, d4"), 8),
            ("fmsub d", form!("fmsub d1, d2, d3, d4"), 8),
            ("fnmadd s", form!("fnmadd s1, s2, s3, s4"), 4),
            ("fnmsub d", form!("fnmsub d1, d2, d3, d4"), 8),
            ("fmla .4s", form!("fmla v1.4s, v2.4s, v3.4s"), 4),
            ("fmls .4s", form!("fmls v1.4s, v2.4s, v3.4s"), 4),
            ("fmla .2d", form!("fmla v1.2d, v2.2d, v3.2d"), 8),
            ("fmls .2d", form!("fmls v1.2d, v2.2d, v3.2d"), 8),
            ("fmla .4s[3]", form!("fmla v1.4s, v2.4s, v3.s[3]"), 4),
            ("fmls .2d[1]", form!("fmls v1.2d, v2.2d, v3.d[1]"), 8),
            ("fmla s[1]", form!("fmla s1, s2, v3.s[1]"), 4),
        ];
        for &(name, f, e) in forms {
            check_nan(name, f, e, n);
        }
    }
}

