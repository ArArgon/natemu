//! Tininess after rounding for hosts without FEAT_AFP (AFP fallback plan, decision C, unmasked case).
//!
//! x86 judges a result tiny after rounding it to the destination precision as if the exponent range were unbounded (SDM Vol.1
//! 4.9.1.5), Arm (FPCR.AH = 0, every host without FEAT_AFP) before rounding. The two differ exactly for results whose exact value
//! lies below +-MIN_NORMAL and rounds, by Arm's bounded rounding, to +-MIN_NORMAL: Arm raises UFC, x86 may not. The Arm result
//! (+-MIN_NORMAL with UFC) does not tell which: x86 rounds to p bits, Arm to the coarser denormal grid, so the exact values just
//! below MIN_NORMAL that Arm rounds up form a band twice as wide (round to nearest) or as wide as x86's. Hence the exact value
//! has to be recomputed from the operands: [`any_tiny`] does it, lane by lane, for a multiply, divide, FMA or narrowing convert,
//! with integer arithmetic only (the resident's stub reads the operands from the registers the check's site names).
//!
//! A value `v` (exact, nonzero, |v| < MIN_NORMAL = 2^emin) is NOT tiny after rounding exactly when rounding it to p bits
//! reaches 2^emin. With ulp = 2^(emin - p) the spacing below 2^emin and 2^emin's significand even:
//! - round to nearest (ties to even): |v| >= 2^emin - ulp/2;
//! - rounding that increases the magnitude (up for a positive v, down for a negative one): |v| > 2^emin - ulp;
//! - toward zero, and the other directed mode: never.
//!
//! An exact tiny result (a denormal, or zero from a nonzero v) is tiny whether or not it is inexact: x86 raises an unmasked UE
//! for it (the shim's exact-tiny test sets UFC for denormal results).
use core::cmp::Ordering;

/// Descriptor kinds (bits 2:0 of [`Desc`]'s meta byte; 0 = none).
pub const MUL: u8 = 1;
pub const DIV: u8 = 2;
pub const FMA: u8 = 3;
/// binary64 to binary32 (CVTSD2SS, CVTPD2PS).
pub const CVT: u8 = 4;
/// Meta byte bits: 8-byte source elements, scalar (lane 0 only), a second half (ymm: two 128-bit ops), FMA's negated product (FNMADD,
/// FNMSUB) and negated addend (FMSUB, FNMSUB).
pub const ELEM8: u8 = 1 << 3;
pub const SCALAR: u8 = 1 << 4;
pub const HALF2: u8 = 1 << 5;
pub const NEG_PRODUCT: u8 = 1 << 6;
pub const NEG_ADDEND: u8 = 1 << 7;

/// What a SIMD check site says about the instruction's operations: `meta` (the site's `width`) and the vector registers of the
/// operands (the site's `imm`, five bits each: slot 3 * half + position; positions: multiply and divide 0, 1; FMA 0, 1 (multiplicands),
/// 2 (addend); convert 0).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Desc {
    pub meta: u8,
    pub regs: [u8; 6],
}

impl Desc {
    /// `None` for a descriptor no shim would write (admission refuses such a site).
    pub fn decode(meta: u8, imm: i32) -> Option<Desc> {
        let kind = meta & 7;
        if !(MUL..=CVT).contains(&kind) || imm as u32 >> 30 != 0 {
            return None;
        }
        // CVT is binary64 to binary32 only; a scalar and a vector form never have a second half (the second half is the upper 128
        // bits of a 256-bit operand).
        if kind == CVT && meta & ELEM8 == 0 || meta & SCALAR != 0 && meta & HALF2 != 0 {
            return None;
        }
        // The negations are FMA's.
        if kind != FMA && meta & (NEG_PRODUCT | NEG_ADDEND) != 0 {
            return None;
        }
        let mut regs = [0u8; 6];
        for (k, r) in regs.iter_mut().enumerate() {
            *r = (imm as u32 >> (5 * k) & 31) as u8;
        }
        Some(Desc { meta, regs })
    }
    /// The inverse of [`Desc::decode`]: (meta, imm).
    pub fn encode(&self) -> (u8, i32) {
        let imm = self
            .regs
            .iter()
            .enumerate()
            .fold(0u32, |a, (k, &r)| a | u32::from(r & 31) << (5 * k));
        (self.meta, imm as i32)
    }
}

/// A fixed-width unsigned integer, 384 bits, little-endian limbs (the widest exact FMA sum is under 320 bits).
#[derive(Clone, Copy, PartialEq, Eq)]
struct Wide([u64; 6]);

impl Wide {
    fn from_u128(v: u128) -> Wide {
        let mut w = [0; 6];
        w[0] = v as u64;
        w[1] = (v >> 64) as u64;
        Wide(w)
    }
    fn is_zero(&self) -> bool {
        self.0 == [0; 6]
    }
    fn bits(&self) -> i64 {
        for k in (0..6).rev() {
            if self.0[k] != 0 {
                return 64 * k as i64 + 64 - i64::from(self.0[k].leading_zeros());
            }
        }
        0
    }
    fn shl(&self, n: u32) -> Wide {
        let (limbs, bits) = ((n / 64) as usize, n % 64);
        let mut out = [0u64; 6];
        for k in (limbs..6).rev() {
            let lo = self.0[k - limbs];
            out[k] |= lo << bits;
            if bits != 0 && k + 1 < 6 {
                out[k + 1] |= lo >> (64 - bits);
            }
        }
        Wide(out)
    }
    fn add(&self, o: &Wide) -> Wide {
        let (mut out, mut carry) = ([0u64; 6], false);
        for k in 0..6 {
            let (a, c1) = self.0[k].overflowing_add(o.0[k]);
            let (b, c2) = a.overflowing_add(u64::from(carry));
            out[k] = b;
            carry = c1 || c2;
        }
        Wide(out)
    }
    /// `self - o`, `self >= o`.
    fn sub(&self, o: &Wide) -> Wide {
        let (mut out, mut borrow) = ([0u64; 6], false);
        for k in 0..6 {
            let (a, b1) = self.0[k].overflowing_sub(o.0[k]);
            let (b, b2) = a.overflowing_sub(u64::from(borrow));
            out[k] = b;
            borrow = b1 || b2;
        }
        Wide(out)
    }
    fn cmp(&self, o: &Wide) -> Ordering {
        for k in (0..6).rev() {
            if self.0[k] != o.0[k] {
                return self.0[k].cmp(&o.0[k]);
            }
        }
        Ordering::Equal
    }
}

/// A nonnegative dyadic number `mant * 2^exp`.
#[derive(Clone, Copy)]
struct Dyadic {
    mant: Wide,
    exp: i64,
}

impl Dyadic {
    /// The position of the leading bit; only for a nonzero value.
    fn top(&self) -> i64 {
        self.mant.bits() - 1 + self.exp
    }
    fn cmp(&self, o: &Dyadic) -> Ordering {
        match self.top().cmp(&o.top()) {
            Ordering::Equal => {}
            other => return other,
        }
        // Equal leading bits: the one with the larger exponent has fewer significant bits; shifting it left by the exponents'
        // difference gives it as many, so the shift is at most the widest mantissa.
        let e = self.exp.min(o.exp);
        self.mant
            .shl((self.exp - e) as u32)
            .cmp(&o.mant.shl((o.exp - e) as u32))
    }
}

/// A finite binary32/binary64 operand as sign, significand and exponent (value = significand * 2^exp, zero has significand 0),
/// or `None` for an infinity or NaN.
fn parse(bits: u64, wide: bool) -> Option<(bool, u64, i64)> {
    let (frac_bits, exp_bits, bias) = if wide { (52, 11, 1023) } else { (23, 8, 127) };
    let sign = bits >> (frac_bits + exp_bits) & 1 != 0;
    let e = (bits >> frac_bits & ((1 << exp_bits) - 1)) as i64;
    let frac = bits & ((1 << frac_bits) - 1);
    if e == (1 << exp_bits) - 1 {
        return None;
    }
    Some(if e == 0 {
        (sign, frac, 1 - bias - frac_bits)
    } else {
        (sign, frac | 1 << frac_bits, e - bias - frac_bits)
    })
}

/// Is the exact nonzero value `sign * mag` tiny after rounding to `p` significant bits with minimum normal exponent `emin`
/// under the x86 rounding mode `rc` (MXCSR.RC encoding: 0 nearest, 1 down, 2 up, 3 zero)?
fn tiny_value(negative: bool, mag: Dyadic, rc: u32, p: u32, emin: i64) -> bool {
    let constant = |mant: u128, exp: i64| Dyadic {
        mant: Wide::from_u128(mant),
        exp,
    };
    // |v| >= 2^emin: normal.
    if mag.cmp(&constant(1, emin)) != Ordering::Less {
        return false;
    }
    let p = p as i64;
    match rc & 3 {
        // not tiny iff |v| >= 2^emin - 2^(emin - p - 1) = (2^(p+1) - 1) * 2^(emin - p - 1)
        0 => mag.cmp(&constant((1u128 << (p + 1)) - 1, emin - p - 1)) == Ordering::Less,
        // the magnitude-increasing directed mode: not tiny iff |v| > (2^p - 1) * 2^(emin - p)
        1 | 2 => {
            let away = (rc & 3 == 1) == negative;
            !away || mag.cmp(&constant((1u128 << p) - 1, emin - p)) != Ordering::Greater
        }
        _ => true,
    }
}

fn dyadic(m: u128, exp: i64) -> Dyadic {
    Dyadic {
        mant: Wide::from_u128(m),
        exp,
    }
}

/// One lane's exact tininess after rounding, for operands `a`, `b`, `c` (bit patterns; `wide`: binary64, else binary32; for
/// [`CVT`] `a` is a binary64 and the result binary32). Zero, infinity, NaN and an exact zero result are not tiny.
pub fn lane_tiny(kind: u8, wide: bool, neg: (bool, bool), rc: u32, a: u64, b: u64, c: u64) -> bool {
    let (p, emin) = match (kind, wide) {
        (CVT, _) | (_, false) => (24, -126),
        _ => (53, -1022),
    };
    let Some((sa, ma, ea)) = parse(a, wide) else {
        return false;
    };
    let (negative, mag) = match kind {
        CVT => (sa, dyadic(ma.into(), ea)),
        MUL | FMA => {
            let Some((sb, mb, eb)) = parse(b, wide) else {
                return false;
            };
            let product = (sa ^ sb ^ neg.0, u128::from(ma) * u128::from(mb), ea + eb);
            if kind == MUL {
                (product.0, dyadic(product.1, product.2))
            } else {
                let Some((sc, mc, ec)) = parse(c, wide) else {
                    return false;
                };
                match sum(product, (sc ^ neg.1, u128::from(mc), ec)) {
                    Some(v) => v,
                    None => return false,
                }
            }
        }
        _ => {
            // a / b: compare ma * 2^ea / (mb * 2^eb) with the constants K * 2^k through ma * 2^ea against K * mb * 2^(k + eb).
            let Some((sb, mb, eb)) = parse(b, wide) else {
                return false;
            };
            if ma == 0 || mb == 0 {
                return false;
            }
            return div_tiny(sa ^ sb, ma, ea, mb, eb, rc, p, emin);
        }
    };
    !mag.mant.is_zero() && tiny_value(negative, mag, rc, p, emin)
}

/// The exact sum of two signed dyadic numbers (sign, significand, exponent); `None` for zero.
fn sum(x: (bool, u128, i64), y: (bool, u128, i64)) -> Option<(bool, Dyadic)> {
    let (mut x, mut y) = (x, y);
    if x.1 == 0 {
        return (y.1 != 0).then(|| (y.0, dyadic(y.1, y.2)));
    }
    if y.1 == 0 {
        return Some((x.0, dyadic(x.1, x.2)));
    }
    let top = |v: (bool, u128, i64)| 128 - i64::from(v.1.leading_zeros()) - 1 + v.2;
    if top(x) < top(y) {
        core::mem::swap(&mut x, &mut y);
    }
    // A much smaller addend decides nothing but a tie (the compared constants have at most 54 significant bits, the larger
    // addend at most 106): keep its sign and shrink it so the sum fits the window.
    if top(x) - top(y) > 200 {
        y = (y.0, 1, top(x) - 200);
    }
    let e = x.2.min(y.2);
    let (wx, wy) = (
        Wide::from_u128(x.1).shl((x.2 - e) as u32),
        Wide::from_u128(y.1).shl((y.2 - e) as u32),
    );
    if x.0 == y.0 {
        return Some((x.0, Dyadic { mant: wx.add(&wy), exp: e }));
    }
    match wx.cmp(&wy) {
        Ordering::Equal => None,
        Ordering::Greater => Some((x.0, Dyadic { mant: wx.sub(&wy), exp: e })),
        Ordering::Less => Some((y.0, Dyadic { mant: wy.sub(&wx), exp: e })),
    }
}

/// The quotient's tininess without dividing: the thresholds of [`tiny_value`] are `K * 2^k`, and `ma * 2^ea / (mb * 2^eb) < K * 2^k`
/// iff `ma * 2^ea < K * mb * 2^(k + eb)` (the products fit 128 bits).
#[allow(clippy::too_many_arguments)]
fn div_tiny(negative: bool, ma: u64, ea: i64, mb: u64, eb: i64, rc: u32, p: u32, emin: i64) -> bool {
    let num = dyadic(ma.into(), ea);
    let less = |k_mant: u128, k_exp: i64| {
        num.cmp(&dyadic(k_mant * u128::from(mb), k_exp + eb)) == Ordering::Less
    };
    // (the same decision as `tiny_value` against 2^emin, then the band's threshold)
    if !less(1, emin) {
        return false;
    }
    let p = i64::from(p);
    match rc & 3 {
        0 => less((1u128 << (p + 1)) - 1, emin - p - 1),
        1 | 2 => {
            let away = (rc & 3 == 1) == negative;
            // |v| > T  is  !(|v| <= T): the quotient equals T exactly when ma * 2^ea == T * mb * 2^eb
            !away || {
                let t = dyadic(((1u128 << p) - 1) * u128::from(mb), emin - p + eb);
                num.cmp(&t) != Ordering::Greater
            }
        }
        _ => true,
    }
}

/// Is any lane of the operation `d` describes tiny after rounding? `reg(r)` is the 16 bytes of vector register `r` at the check.
/// `rc` is MXCSR.RC (bits 14:13 of MXCSR as an integer 0..=3). A lane whose operands are not finite numbers is not tiny.
pub fn any_tiny(d: &Desc, rc: u32, reg: impl Fn(u8) -> [u8; 16]) -> bool {
    let kind = d.meta & 7;
    let wide = d.meta & ELEM8 != 0;
    let lanes = if d.meta & SCALAR != 0 {
        1
    } else if wide {
        2
    } else {
        4
    };
    let halves = 1 + usize::from(d.meta & HALF2 != 0);
    let neg = (d.meta & NEG_PRODUCT != 0, d.meta & NEG_ADDEND != 0);
    // lane `i` of a register of 4- or 8-byte elements
    let lane = |r: u8, i: usize| -> u64 {
        let bytes = reg(r);
        if wide {
            u64::from_le_bytes(bytes[8 * i..8 * i + 8].try_into().unwrap())
        } else {
            u64::from(u32::from_le_bytes(bytes[4 * i..4 * i + 4].try_into().unwrap()))
        }
    };
    for h in 0..halves {
        let r = &d.regs[3 * h..3 * h + 3];
        for i in 0..lanes {
            let a = lane(r[0], i);
            let (b, c) = match kind {
                CVT => (0, 0),
                MUL | DIV => (lane(r[1], i), 0),
                _ => (lane(r[1], i), lane(r[2], i)),
            };
            if lane_tiny(kind, wide, neg, rc, a, b, c) {
                return true;
            }
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Cases of the exact reference (Python `fractions`, tools/models/gen_tiny_vectors.py): one per line,
    /// `kind wide rc negp negc a b c tiny` (hex bit patterns).
    const VECTORS: &str = include_str!("../tests/tiny_vectors.txt");

    #[test]
    fn matches_the_exact_reference() {
        let mut n = 0;
        let mut seen = [[0u32; 2]; 5];
        for line in VECTORS.lines().filter(|l| !l.starts_with('#')) {
            let mut it = line.split(' ');
            let mut f = [""; 9];
            f.iter_mut().for_each(|x| *x = it.next().unwrap());
            let hex = |s: &str| u64::from_str_radix(s, 16).unwrap();
            let kind = f[0].parse::<u8>().unwrap();
            let (wide, rc) = (f[1] == "1", f[2].parse::<u32>().unwrap());
            let neg = (f[3] == "1", f[4] == "1");
            let (a, b, c) = (hex(f[5]), hex(f[6]), hex(f[7]));
            let want = f[8] == "1";
            let got = lane_tiny(kind, wide, neg, rc, a, b, c);
            assert_eq!(got, want, "{line}");
            seen[kind as usize][want as usize] += 1;
            n += 1;
        }
        // every kind has both outcomes
        for kind in MUL..=CVT {
            assert!(seen[kind as usize][0] > 100 && seen[kind as usize][1] > 100, "{seen:?}");
        }
        assert!(n > 4000, "{n}");
    }

    #[test]
    fn the_descriptor_round_trips_and_selects_lanes() {
        let d = Desc {
            meta: MUL | HALF2,
            regs: [1, 2, 0, 31, 30, 0],
        };
        let (m, imm) = d.encode();
        assert_eq!(Desc::decode(m, imm), Some(d));
        assert_eq!(Desc::decode(0, 0), None);
        assert_eq!(Desc::decode(CVT, 0), None);
        assert_eq!(Desc::decode(MUL | NEG_ADDEND, 0), None);
        assert_eq!(Desc::decode(MUL, 1 << 31), None);
        // CVTSD2SS of 2^-126 - 2^-151 (exact in binary64; half an ulp of binary32's 2^-150 spacing below MIN_NORMAL) in lane 1 of
        // register 0 (lane 0 is 1.0): x86 rounds it to MIN_NORMAL in nearest-even (not tiny) and up for a positive value, but not
        // down or toward zero.
        let x = (896u64 << 52) | (((1u64 << 24) - 1) << 28);
        let cvt = Desc {
            meta: CVT | ELEM8,
            regs: [0; 6],
        };
        let reg = |_r: u8| {
            let mut q = [0u8; 16];
            q[..8].copy_from_slice(&1f64.to_bits().to_le_bytes());
            q[8..].copy_from_slice(&x.to_le_bytes());
            q
        };
        assert_eq!([0, 1, 2, 3].map(|rc| any_tiny(&cvt, rc, reg)), [false, true, false, true]);
        // the scalar form looks at lane 0 only
        let scalar = Desc {
            meta: CVT | ELEM8 | SCALAR,
            regs: [0; 6],
        };
        assert_eq!([0, 1, 2, 3].map(|rc| any_tiny(&scalar, rc, reg)), [false; 4]);
    }
}
