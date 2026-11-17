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
//! halts with a named stop rather than patch it wrongly. UFE traps every result tiny before rounding, exact ones too, so the Keep class
//! holds the adds and subtracts, which never raise UFC (a tiny sum is exact).
use crate::tiny;

/// FPSR.UFC.
pub const UFC: u64 = 8;

/// A word whose UFC the model decides: the operation's kind and element size, the lanes it covers, and its source registers.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Op {
    /// [`tiny::MUL`], [`tiny::DIV`], [`tiny::FMA`], [`tiny::CVT`] (binary64 to binary32) or [`tiny::CVT16`] (binary32 to binary16).
    pub kind: u8,
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
        wide,
        neg: (false, false),
        lanes,
        rn: (w >> 5 & 31) as u8,
        rm: (w >> 16 & 31) as u8,
        ra: (w & 31) as u8,
        index: None,
    }
}

/// The class of the Arm word `w`, `None` when the table does not know it.
pub fn classify(w: u32) -> Option<Class> {
    use Class::{Keep, Patch};
    let ty = w >> 22 & 3;
    // Scalar floating point, two sources: FMUL 0, FDIV 1, FADD 2, FSUB 3, FMAX/FMIN/FMAXNM/FMINNM 4-7, FNMUL 8 (binary32, binary64).
    if w & 0xff20_0c00 == 0x1e20_0800 && ty < 2 {
        return match w >> 12 & 15 {
            0 | 8 => Some(Patch(op(tiny::MUL, ty == 1, 1, w))),
            1 => Some(Patch(op(tiny::DIV, ty == 1, 1, w))),
            2..=7 => Some(Keep),
            _ => None,
        };
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
    let (u, a, sz, q) = (w >> 29 & 1, w >> 23 & 1, w >> 22 & 1, w >> 30 & 1);
    let wide = sz == 1;
    // Vector three-register-same, floating point: FMUL, FDIV, FMLA, FMLS (patched), FADD, FSUB, FADDP, FABD (kept).
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
            (0, _, 0x1a) | (1, 0 | 1, 0x1a) => Some(Keep),
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
    // Vector FCVTN/FCVTN2 (S to H, D to S; Q picks the destination half), FCVTL/FCVTL2 (kept); the estimates kept.
    if w & 0x9f3e_0c00 == 0x0e20_0800 {
        return match (u, a, w >> 12 & 31) {
            (0, 0, 0x16) if wide => Some(Patch(op(tiny::CVT, true, 2, w))),
            (0, 0, 0x16) => Some(Patch(op(tiny::CVT16, false, 4, w))),
            (0, 0, 0x17) | (0, 1, 0x1d) | (1, 1, 0x1d) => Some(Keep),
            _ => None,
        };
    }
    // Scalar FRECPE and FRSQRTE, scalar FADDP (pairwise add), vector FCADD: kept.
    if w & 0xdf3e_0c00 == 0x5e20_0800 && matches!((u, a, w >> 12 & 31), (0, 1, 0x1d) | (1, 1, 0x1d)) {
        return Some(Keep);
    }
    if w & 0xffbf_fc00 == 0x7e30_d800 || w & 0xbf20_ec00 == 0x2e00_e400 {
        return Some(Keep);
    }
    None
}

/// The x86 rounding mode (MXCSR.RC encoding) of an FPCR: RMode (bits 23:22) is the same permutation back.
pub const fn x86_rc(fpcr: u64) -> u32 {
    [0, 2, 1, 3][(fpcr >> 22 & 3) as usize]
}

/// FPSR after the word, from `rerun` (the flags of the re-run alone) and the word's `class`: [`UFC`] is kept exactly when some lane is
/// tiny after rounding and inexact ([`tiny::lane_underflow`]); the rest is the hardware's. `q` holds the registers [`Op::sources`] names, in
/// that order, at the trap; `rc` is the x86 rounding mode the word ran under. A re-run without UFC needs no model: Arm's UFC (tiny before
/// rounding and inexact) holds whenever x86's masked UE does.
pub fn patch(class: Class, rc: u32, q: &[[u8; 16]; 3], rerun: u64) -> u64 {
    let Class::Patch(o) = class else {
        return rerun;
    };
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
    let under = (0..usize::from(o.lanes)).any(|i| {
        let a = lane(0, i, o.wide);
        let b = match (o.kind, o.index) {
            (tiny::CVT | tiny::CVT16, _) => 0,
            (_, Some(k)) => lane(1, usize::from(k), o.wide),
            _ => lane(1, i, o.wide),
        };
        let c = if o.kind == tiny::FMA { lane(2, i, o.wide) } else { 0 };
        tiny::lane_underflow(o.kind, o.wide, o.neg, rc, a, b, c)
    });
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
            0x1e63_2841, 0x1e23_3841, 0x1e23_4841, // fadd, fsub, fmax
            0x1e22_c041, 0x1ee2_4041, 0x1ee2_c041, // fcvt widening
            0x4e23_d441, 0x4ee3_d441, 0x6e23_d441, 0x6ea3_d441, // fadd, fsub, faddp, fabd (vector)
            0x0e21_7841, 0x4e61_7841, // fcvtl, fcvtl2
            0x4ea1_d841, 0x6ee1_d841, 0x5ea1_d841, 0x7ee1_d841, // frecpe, frsqrte (vector, scalar)
            0x7e30_d841, 0x7e70_d841, // faddp (scalar)
            0x6e83_e441, 0x6ec3_f441, // fcadd #90, #270
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
        // Unknown: fsqrt, fmulx, frecps, fneg (none can trap UFE), an integer add, a load.
        for unknown in [0x6ea1_f841, 0x1e21_c041, 0x4e23_dc41, 0x4e23_fc41, 0x6ea0_f841, 0x4ea3_8441, 0xf940_0000] {
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
            wide: false,
            neg: (false, false),
            lanes: 4,
            rn: 0,
            rm: 1,
            ra: 2,
            index: None,
        });
        let regs = |lanes: [(u32, u32); 4]| {
            [q(lanes.map(|l| l.0)), q(lanes.map(|l| l.1)), [0; 16]]
        };
        let ioc = 1;
        let none = (one, one);
        // band + exact tiny: x86 raises nothing; the re-run's UFC goes
        let r = regs([band, exact_tiny, none, none]);
        assert_eq!(patch(mul, 0, &r, UFC | 0x10), 0x10);
        // band + real underflow: UFC stays
        let r = regs([band, real, none, none]);
        assert_eq!(patch(mul, 0, &r, UFC | 0x10), UFC | 0x10);
        // a NaN lane's IOC next to a band lane: IOC kept, UFC removed
        let r = regs([(0x7fc0_0000, one), band, none, none]);
        assert_eq!(patch(mul, 0, &r, UFC | 0x10 | ioc), 0x10 | ioc);
        // the band depends on the rounding mode: up (x86 rc 2) rounds a positive band value to MIN_NORMAL too, down (1) and zero (3) do not
        let r = regs([band, none, none, none]);
        assert_eq!([0, 1, 2, 3].map(|rc| patch(mul, rc, &r, UFC | 0x10) & UFC), [0, UFC, 0, UFC]);
        // no UFC in the re-run: nothing to do (and no model run)
        assert_eq!(patch(mul, 0, &r, 0x10), 0x10);
        assert_eq!(patch(Class::Keep, 0, &r, UFC), UFC);
        // a scalar word looks at lane 0 only
        let scalar = Class::Patch(Op { lanes: 1, ..match mul { Class::Patch(o) => o, _ => unreachable!() } });
        let r = regs([none, band, none, none]);
        assert_eq!(patch(scalar, 0, &r, UFC | 0x10), 0x10);
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
                assert_eq!(matches!(class, Class::Patch(_)), expect_patch, "{name} {word:#x}");
                // the registers the table names, as the trap would have saved them (v1..v4 are `q[0..4]`)
                let mut saved = [[0u8; 16]; 3];
                if let Class::Patch(o) = class {
                    for (s, reg) in saved.iter_mut().zip(o.sources()) {
                        *s = q[usize::from(reg) - 1];
                    }
                }
                let got = patch(class, x86_rc(fpcr), &saved, rerun);
                assert_eq!(got & 0x1f, afp & 0x1f, "{name} rmode {rmode} word {word:#x} rerun {rerun:#x} afp {afp:#x}\nq {q:x?}");
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
            ("fabd .4s", form!("fabd v1.4s, v2.4s, v3.4s"), 4),
        ];
        for &(name, f, e) in kept {
            check(name, f, e, false, n / 4, false);
        }
    }
}

