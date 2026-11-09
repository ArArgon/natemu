//! x87 values (I6): FEX's reduced-precision mode keeps ST(i) as doubles; the 80-bit memory images (FLD/FSTP m80, FNSAVE/
//! FRSTOR, FXSAVE/FXRSTOR and XSAVE's x87 area) are converted here. Widening a double is exact; narrowing an 80-bit value
//! rounds it to a double by FCW.RC (the reduced-precision deviation, plan I6).

/// The x87 QNaN floating-point indefinite as a double (the 80-bit one: sign, exponent 7FFFH, significand C000..0H).
pub const INDEFINITE: u64 = 0xfff8_0000_0000_0000;

/// A double widened to the 80-bit format (exact): (significand with its integer bit, sign and exponent).
pub fn to_f80(d: u64) -> (u64, u16) {
    let (sign, exp, frac) = (
        (d >> 63) as u16,
        (d >> 52 & 0x7ff) as u16,
        d & ((1 << 52) - 1),
    );
    let (mant, e) = match exp {
        0 if frac == 0 => (0, 0),
        0 => {
            let lz = frac.leading_zeros();
            (frac << lz, 15372 - lz as u16)
        }
        0x7ff => (1 << 63 | frac << 11, 0x7fff),
        _ => (1 << 63 | frac << 11, exp + 15360),
    };
    (mant, sign << 15 | e)
}
/// An 80-bit value rounded to a double under rounding control `rc` (FCW.RC). Unsupported encodings (pseudo-NaN,
/// pseudo-infinity, unnormal) become the indefinite; a NaN keeps its sign, its quiet bit and as much payload as fits (an
/// SNaN whose payload all lies in the dropped bits keeps bit 0, so it stays a NaN).
pub fn from_f80(mant: u64, se: u16, rc: u64) -> u64 {
    let sign = u64::from(se >> 15) << 63;
    let e = i64::from(se & 0x7fff);
    let integer = mant >> 63 != 0;
    if e == 0x7fff {
        if !integer {
            return INDEFINITE;
        }
        if mant << 1 == 0 {
            return sign | 0x7ff << 52;
        }
        let payload = (mant >> 11) & ((1 << 52) - 1);
        return sign | 0x7ff << 52 | if payload == 0 { 1 } else { payload };
    }
    if e != 0 && !integer {
        return INDEFINITE;
    }
    if mant == 0 {
        return sign;
    }
    // value = mant * 2^(E - 63), E the unbiased exponent of bit 63 (a denormal or pseudo-denormal has E = -16382).
    let unbiased = if e == 0 { -16382 } else { e - 16383 };
    let lz = i64::from(mant.leading_zeros());
    let (m, exp) = (mant << lz, unbiased - lz); // bit 63 set; value = m * 2^(exp - 63)
    // Bits kept: 53 for a normal result; fewer below 2^-1022 (denormal); none at all below 2^-1075.
    let keep = if exp >= -1022 { 53 } else { 53 - (-1022 - exp) };
    let (kept, round, sticky) = if keep <= 0 {
        (
            0,
            keep == 0 && m >> 63 != 0,
            if keep == 0 { m << 1 != 0 } else { true },
        )
    } else {
        let drop = 64 - keep;
        (m >> drop, m >> (drop - 1) & 1 != 0, m << (65 - drop) != 0)
    };
    let up = match rc {
        0 => round && (sticky || kept & 1 != 0),
        1 => sign != 0 && (round || sticky),
        2 => sign == 0 && (round || sticky),
        _ => false,
    };
    let v = kept + u64::from(up);
    let bits = if exp >= -1022 {
        // kept has bit 52 set (or 53 after a carry): exponent field exp + 1023, + 1 on a carry.
        let (frac, carry) = if v >> 53 != 0 { (v >> 1, 1) } else { (v, 0) };
        let biased = exp + 1023 + carry;
        if biased >= 0x7ff {
            let to_inf = match rc {
                0 => true,
                1 => sign != 0,
                2 => sign == 0,
                _ => false,
            };
            return sign
                | if to_inf {
                    0x7ff << 52
                } else {
                    0x7fe << 52 | ((1 << 52) - 1)
                };
        }
        (biased as u64) << 52 | frac & ((1 << 52) - 1)
    } else {
        v // a denormal (exponent field 0), or the smallest normal after a carry into bit 52
    };
    sign | bits
}

// I6b: the transcendental, remainder, scale and extract instructions (D9 F0..FF), run whole by the resident (`svc #X87`,
// [`crate::execution::svc::X87`]) on doubles: FEX's reduced precision. Values come from the toolchain's libm (compiler_builtins:
// sin cos tan atan2 expm1 log2 log1p fmod ldexp fma), correctly rounded or within an ulp, to nearest whatever FCW.RC and FCW.PC;
// FSCALE, FPREM, FPREM1 and FXTRACT are exact (FSCALE rounds by FCW.RC). The x87 rules come from the SDM (Vol.2 per
// instruction, "FPU Flags Affected" and the result tables; Vol.1 8.5 for the masked responses) where it defines them, else from
// Bochs (both registers of FPTAN/FSINCOS/FXTRACT get the indefinite, or the NaN, on a stack fault or NaN operand) and QEMU
// (FPREM's partial remainder: N = 32 + D mod 32, C0, C1, C3 cleared; C0..C3 cleared on a special operand).

unsafe extern "C" {
    fn sin(x: f64) -> f64;
    fn cos(x: f64) -> f64;
    fn tan(x: f64) -> f64;
    fn atan2(y: f64, x: f64) -> f64;
    fn expm1(x: f64) -> f64;
    fn log2(x: f64) -> f64;
    fn log1p(x: f64) -> f64;
    fn fmod(x: f64, y: f64) -> f64;
    fn ldexp(x: f64, n: i32) -> f64;
    fn fma(x: f64, y: f64, z: f64) -> f64;
}

/// x87 exception-word bits (the guest-state page's `X87_EXCEPTIONS`, FPSR order; SF in bit 6). DE is never raised here: a
/// double operand is never an 80-bit denormal.
pub mod flag {
    pub const IE: u32 = 1;
    pub const ZE: u32 = 2;
    pub const OE: u32 = 4;
    pub const UE: u32 = 8;
    pub const PE: u32 = 0x10;
    pub const SF: u32 = 0x40;
}
use flag::*;

/// What a helper reads: ST(0) and ST(1) (`None`: empty), whether the register a push lands in (TOP - 1) is empty, FCW.RC.
#[derive(Clone, Copy, Debug)]
pub struct Operands {
    pub st0: Option<u64>,
    pub st1: Option<u64>,
    pub free: bool,
    pub rc: u64,
}
/// What it does, in this order: write ST(0) and ST(1) (each then valid), pop (ST(0) emptied, TOP + 1) or push (TOP - 1, the
/// value valid), set the condition codes C0..C3 given (`None`: unchanged, undefined by the SDM) and OR the flags in.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Effect {
    pub st0: Option<u64>,
    pub st1: Option<u64>,
    pub pop: bool,
    pub push: Option<u64>,
    pub c: [Option<bool>; 4],
    pub flags: u32,
}

const SIGN: u64 = 1 << 63;
const FRAC: u64 = (1 << 52) - 1;
const ONE: u64 = 0x3ff0_0000_0000_0000;
const NEG_INF: u64 = 0xfff0_0000_0000_0000;
const POS_INF: u64 = 0x7ff0_0000_0000_0000;
const TWO63: f64 = 9_223_372_036_854_775_808.0;

fn is_nan(d: u64) -> bool {
    d & !SIGN > POS_INF
}
fn is_snan(d: u64) -> bool {
    is_nan(d) && d >> 51 & 1 == 0
}
/// One NaN operand: quieted, IE for an SNaN.
fn nan1(d: u64, fl: &mut u32) -> u64 {
    if is_snan(d) {
        *fl |= IE;
    }
    d | 1 << 51
}
/// Two operands, one at least a NaN, by the x87 rule (SDM Vol.1 Table 4-7): the QNaN over an SNaN, else the larger
/// significand (the positive one on a tie), quieted; IE for any SNaN.
fn nan2(a: u64, b: u64, fl: &mut u32) -> u64 {
    if is_snan(a) || is_snan(b) {
        *fl |= IE;
    }
    let pick = match (is_nan(a), is_nan(b)) {
        (true, false) => a,
        (false, true) => b,
        _ if is_snan(a) != is_snan(b) => {
            if is_snan(a) {
                b
            } else {
                a
            }
        }
        _ if a & FRAC != b & FRAC => {
            if a & FRAC > b & FRAC {
                a
            } else {
                b
            }
        }
        _ => {
            if a & SIGN == 0 {
                a
            } else {
                b
            }
        }
    };
    pick | 1 << 51
}
/// The flags of a result computed from finite operands, inexact unless `exact`: OE for an overflow, UE for a tiny one (the
/// double's thresholds, deviation 1), PE.
fn rounded(r: f64, exact: bool) -> u32 {
    let a = r.abs();
    match () {
        _ if exact => 0,
        _ if a.is_infinite() => OE | PE,
        _ if a < f64::MIN_POSITIVE => UE | PE,
        _ => PE,
    }
}
/// The unbiased exponent of a finite nonzero double (a denormal's as normalized: the 80-bit value's exponent).
fn expo(d: u64) -> i64 {
    let e = (d >> 52 & 0x7ff) as i64;
    if e != 0 {
        e - 1023
    } else {
        (63 - (d & FRAC).leading_zeros()) as i64 - 1074
    }
}
fn pow2(d: u64) -> bool {
    let e = d >> 52 & 0x7ff;
    if e == 0 {
        (d & FRAC).count_ones() == 1
    } else {
        d & FRAC == 0
    }
}

/// The helper instruction D9 `function` (F0..FF; FXAM's and the others' bytes are not helpers: `None`).
pub fn helper(function: u8, o: Operands) -> Option<Effect> {
    let mut e = Effect::default();
    let f = f64::from_bits;
    let b = f64::to_bits;
    // Stack faults (SDM Vol.1 8.5.1.1, masked): IE and SF, C1 = 1 for an overflow, else 0; the destinations get the indefinite.
    let fault = |mut e: Effect, overflow: bool| {
        e.flags |= IE | SF;
        e.c[1] = Some(overflow);
        e
    };
    match function {
        // FSIN, FCOS, FPTAN (pushes 1.0), FSINCOS (sine in ST(0), then pushes the cosine).
        0xfe | 0xff | 0xf2 | 0xfb => {
            let push = matches!(function, 0xf2 | 0xfb);
            e.c[2] = Some(false);
            e.c[1] = Some(false);
            let x = match o.st0 {
                Some(x) if o.free || !push => x,
                st0 => {
                    e.st0 = Some(INDEFINITE);
                    e.push = push.then_some(INDEFINITE);
                    return Some(fault(e, st0.is_some()));
                }
            };
            if is_nan(x) || f(x).is_infinite() {
                let n = if is_nan(x) {
                    nan1(x, &mut e.flags)
                } else {
                    e.flags |= IE;
                    INDEFINITE
                };
                e.st0 = Some(n);
                e.push = push.then_some(n);
            } else if f(x).abs() >= TWO63 {
                e.c[2] = Some(true); // out of range: ST(0) unchanged, nothing pushed, no exception
            } else {
                let exact = f(x) == 0.0;
                let (r0, r1) = unsafe {
                    match function {
                        0xfe => (sin(f(x)), None),
                        0xff => (cos(f(x)), None),
                        0xf2 => (tan(f(x)), Some(1.0)),
                        _ => (sin(f(x)), Some(cos(f(x)))),
                    }
                };
                e.st0 = Some(b(r0));
                e.flags |= rounded(r0, exact);
                if let Some(r1) = r1 {
                    e.push = Some(b(r1));
                    e.flags |= rounded(r1, exact || function == 0xf2);
                }
            }
        }
        // F2XM1: 2^x - 1 (the SDM defines -1 <= x <= +1; computed beyond as well).
        0xf0 => {
            e.c[1] = Some(false);
            let Some(x) = o.st0 else {
                e.st0 = Some(INDEFINITE);
                return Some(fault(e, false));
            };
            e.st0 = Some(match f(x) {
                _ if is_nan(x) => nan1(x, &mut e.flags),
                v if v == 0.0 || v == f64::INFINITY => x,
                f64::NEG_INFINITY => b(-1.0),
                1.0 => ONE,
                -1.0 => b(-0.5),
                v => {
                    let r = unsafe { expm1(v * core::f64::consts::LN_2) };
                    e.flags |= rounded(r, false);
                    b(r)
                }
            });
        }
        // FYL2X: ST(1) * log2(ST(0)); FYL2XP1: ST(1) * log2(ST(0) + 1); FPATAN: arctan(ST(1) / ST(0)). Each pops.
        0xf1 | 0xf9 | 0xf3 => {
            e.c[1] = Some(false);
            e.pop = true;
            let (Some(x), Some(y)) = (o.st0, o.st1) else {
                e.st1 = Some(INDEFINITE);
                return Some(fault(e, false));
            };
            if is_nan(x) || is_nan(y) {
                e.st1 = Some(nan2(y, x, &mut e.flags));
                return Some(e);
            }
            let (xf, yf) = (f(x), f(y));
            let r = if function == 0xf3 {
                let r = unsafe { atan2(yf, xf) };
                let exact = (yf == 0.0 && x & SIGN == 0) || (yf.is_finite() && xf == f64::INFINITY);
                e.flags |= if xf.is_finite() && yf.is_finite() {
                    rounded(r, exact)
                } else if exact {
                    0
                } else {
                    PE
                };
                r
            } else {
                // The logarithm: invalid for a negative argument; -infinity for a zero one (log2(+-0), log2(1 + -1)).
                let (l, exact) = if function == 0xf1 {
                    if xf == 0.0 {
                        (f64::NEG_INFINITY, true)
                    } else {
                        (unsafe { log2(xf) }, pow2(x) || xf.is_infinite())
                    }
                } else if xf == -1.0 {
                    (f64::NEG_INFINITY, true)
                } else if xf == 0.0 {
                    (xf, true)
                } else {
                    (
                        unsafe { log1p(xf) } * core::f64::consts::LOG2_E,
                        xf.is_infinite(),
                    )
                };
                if l.is_nan() || (l == f64::NEG_INFINITY && yf == 0.0) {
                    e.flags |= IE;
                    f64::NAN // the indefinite, below
                } else if l == f64::NEG_INFINITY && yf.is_finite() {
                    e.flags |= ZE;
                    yf * l
                } else {
                    let r = yf * l;
                    if r.is_nan() {
                        e.flags |= IE; // 0 * infinity
                    } else if yf.is_finite() && l.is_finite() {
                        let exact = yf == 0.0
                            || (exact && r.is_finite() && unsafe { fma(yf, l, -r) } == 0.0);
                        e.flags |= rounded(r, exact);
                    }
                    r
                }
            };
            e.st1 = Some(if e.flags & IE != 0 { INDEFINITE } else { b(r) });
        }
        // FSCALE: ST(0) * 2^trunc(ST(1)), rounded by FCW.RC.
        0xfd => {
            e.c[1] = Some(false);
            let (Some(x), Some(s)) = (o.st0, o.st1) else {
                e.st0 = Some(INDEFINITE);
                return Some(fault(e, false));
            };
            if is_nan(x) || is_nan(s) {
                e.st0 = Some(nan2(x, s, &mut e.flags));
                return Some(e);
            }
            let (xf, sf) = (f(x), f(s));
            e.st0 = Some(if sf.is_infinite() {
                // SDM FSCALE table: -inf scales a finite value to a zero, +inf a nonzero one to an infinity; inf * 2^-inf and
                // 0 * 2^inf are invalid.
                match (sf > 0.0, xf == 0.0, xf.is_infinite()) {
                    (false, _, true) | (true, true, _) => {
                        e.flags |= IE;
                        INDEFINITE
                    }
                    (false, _, false) => x & SIGN,
                    (true, false, _) => x & SIGN | POS_INF,
                }
            } else if xf == 0.0 || xf.is_infinite() {
                x
            } else {
                let n = sf.clamp(-65536.0, 65536.0) as i64; // beyond any double's range either way
                let (mant, se) = to_f80(x);
                let biased = i64::from(se & 0x7fff) + n;
                let r = from_f80(mant, (se & 0x8000) | biased.clamp(1, 0x7ffe) as u16, o.rc);
                let unbiased = biased - 16383;
                let exact = (1..0x7fff).contains(&biased)
                    && to_f80(r) == (mant, (se & 0x8000) | biased as u16);
                e.flags |= match () {
                    _ if exact => 0,
                    _ if unbiased >= 1024 => OE | PE,
                    _ => UE | PE,
                };
                r
            });
        }
        // FPREM (truncating) and FPREM1 (to nearest): the remainder of ST(0) by ST(1); C0, C3, C1 = the quotient's bits 2, 1, 0;
        // C2 = 1 for a partial remainder (exponent difference >= 64).
        0xf8 | 0xf5 => {
            e.c = [Some(false); 4];
            let (Some(x), Some(y)) = (o.st0, o.st1) else {
                e.st0 = Some(INDEFINITE);
                return Some(fault(e, false));
            };
            if is_nan(x) || is_nan(y) {
                e.st0 = Some(nan2(x, y, &mut e.flags));
                return Some(e);
            }
            let (xf, yf) = (f(x), f(y));
            if xf.is_infinite() || yf == 0.0 {
                e.flags |= IE;
                e.st0 = Some(INDEFINITE);
                return Some(e);
            }
            if yf.is_infinite() || xf == 0.0 {
                return Some(e); // ST(0) unchanged, quotient 0
            }
            let (ax, ay) = (xf.abs(), yf.abs());
            let d = expo(x) - expo(y);
            let r = if d < 64 {
                let r = unsafe { fmod(ax, ay) };
                // The quotient's low three bits: fmod(|x|, 8|y|) - fmod(|x|, |y|) = (q mod 8) |y|.
                let r8 = if (8.0 * ay).is_finite() {
                    unsafe { fmod(ax, 8.0 * ay) }
                } else {
                    ax
                };
                let mut q = ((r8 - r) / ay + 0.5) as u64 & 7;
                let mut r = r;
                if function == 0xf5 && (r > ay - r || (r == ay - r && q & 1 != 0)) {
                    r -= ay; // exact (Sterbenz)
                    q = (q + 1) & 7;
                }
                e.c = [
                    Some(q & 4 != 0),
                    Some(q & 1 != 0),
                    Some(false),
                    Some(q & 2 != 0),
                ];
                r
            } else {
                // QEMU's (and AMD's) partial step: reduce by |y| * 2^(D - N), N = 32 + D mod 32, truncating, C0 C1 C3 cleared.
                e.c[2] = Some(true);
                unsafe { fmod(ax, ldexp(ay, (d - (32 + d % 32)) as i32)) }
            };
            e.st0 = Some(b(r) ^ (x & SIGN));
        }
        // FXTRACT: ST(0) = the exponent, then pushes the significand (sign kept, exponent 0).
        0xf4 => {
            e.c[1] = Some(false);
            let x = match o.st0 {
                Some(x) if o.free => x,
                st0 => {
                    e.st0 = Some(INDEFINITE);
                    e.push = Some(INDEFINITE);
                    return Some(fault(e, st0.is_some()));
                }
            };
            let (exp, sig) = match f(x) {
                _ if is_nan(x) => {
                    let n = nan1(x, &mut e.flags);
                    (n, n)
                }
                v if v == 0.0 => {
                    e.flags |= ZE;
                    (NEG_INF, x)
                }
                v if v.is_infinite() => (POS_INF, x),
                _ => {
                    let m = if x >> 52 & 0x7ff != 0 {
                        x & FRAC
                    } else {
                        (x & FRAC) << ((x & FRAC).leading_zeros() - 11) & FRAC
                    };
                    (b(expo(x) as f64), x & SIGN | ONE | m)
                }
            };
            e.st0 = Some(exp);
            e.push = Some(sig);
        }
        _ => return None,
    }
    Some(e)
}

#[cfg(test)]
mod tests {
    use super::*;
    /// Widening is exact and rounding back is the identity on doubles; 80-bit values round by RC.
    /// I9: an MMX value is the raw 64 bits of its slot, which the 80-bit images (FXSAVE/XSAVE/FNSAVE then FXRSTOR/XRSTOR/FRSTOR)
    /// carry as that double widened: every bit pattern must come back unchanged under any RC (NaN payloads, SNaNs, denormals).
    #[test]
    fn mmx_values_round_trip() {
        let mut v = 0x9e37_79b9_7f4a_7c15u64;
        let mut check = |d: u64| {
            let (m, se) = to_f80(d);
            for rc in 0..4 {
                assert_eq!(from_f80(m, se, rc), d, "{d:#x} rc {rc}");
            }
        };
        for top in 0..1u64 << 13 {
            for low in [0, 1, 0x7ff, 1 << 51, (1 << 52) - 1] {
                check(top << 51 | low);
            }
        }
        for _ in 0..1_000_000 {
            v ^= v << 13;
            v ^= v >> 7;
            v ^= v << 17;
            check(v);
        }
    }
    #[test]
    fn f80_conversions() {
        for d in [
            0u64,
            1 << 63,
            1,
            0x000f_ffff_ffff_ffff,
            0x0010_0000_0000_0000,
            0x3ff0_0000_0000_0000,
            0xc009_21fb_5444_2d18,
            0x7fef_ffff_ffff_ffff,
            0x7ff0_0000_0000_0000,
            0xfff8_0000_0000_0000,
            0x7ff4_0000_0000_0001,
            0x8000_0000_0000_0003,
        ] {
            let (m, se) = to_f80(d);
            for rc in 0..4 {
                assert_eq!(from_f80(m, se, rc), d, "{d:#x} rc {rc}");
            }
        }
        assert_eq!(to_f80(0x3ff0_0000_0000_0000), (1 << 63, 0x3fff));
        assert_eq!(to_f80(1), (1 << 63, 15372 - 63)); // 2^-1074
        // 1 + 2^-63: nearest and down give 1.0, up gives 1 + 2^-52.
        let (m, se) = (1 << 63 | 1, 0x3fff);
        assert_eq!(
            [0, 1, 2, 3].map(|rc| from_f80(m, se, rc)),
            [
                0x3ff0_0000_0000_0000,
                0x3ff0_0000_0000_0000,
                0x3ff0_0000_0000_0001,
                0x3ff0_0000_0000_0000
            ]
        );
        // Ties to even: 1 + 2^-53 (halfway) rounds to 1.0; 1 + 3 * 2^-53 to 1 + 2^-51.
        assert_eq!(
            from_f80(1 << 63 | 1 << 10, 0x3fff, 0),
            0x3ff0_0000_0000_0000
        );
        assert_eq!(
            from_f80(1 << 63 | 3 << 10, 0x3fff, 0),
            0x3ff0_0000_0000_0002
        );
        // Overflow: 2^1024 is infinity to nearest, the largest double toward zero.
        assert_eq!(from_f80(1 << 63, 0x3fff + 1024, 0), 0x7ff0_0000_0000_0000);
        assert_eq!(from_f80(1 << 63, 0x3fff + 1024, 3), 0x7fef_ffff_ffff_ffff);
        assert_eq!(from_f80(1 << 63, 0xbfff + 1024, 2), 0xffef_ffff_ffff_ffff);
        // Underflow: 2^-1075 is a tie to 0 (even), 2^-1075 + tiny rounds to 2^-1074; 2^-2000 is 0, or 2^-1074 rounding up.
        assert_eq!(from_f80(1 << 63, 0x3fff - 1075, 0), 0);
        assert_eq!(from_f80(1 << 63 | 1, 0x3fff - 1075, 0), 1);
        assert_eq!(
            [0, 2].map(|rc| from_f80(1 << 63, 0x3fff - 2000, rc)),
            [0, 1]
        );
        // The largest denormal rounding up carries into the smallest normal.
        assert_eq!(from_f80(u64::MAX, 0x3fff - 1023, 0), 0x0010_0000_0000_0000);
        // Unsupported encodings and NaN payloads.
        assert_eq!(from_f80(0x4000_0000_0000_0000, 0x3fff, 0), INDEFINITE); // unnormal
        assert_eq!(from_f80(0x4000_0000_0000_0000, 0x7fff, 0), INDEFINITE); // pseudo-NaN
        assert_eq!(from_f80(0xc000_0000_0000_0000, 0xffff, 0), INDEFINITE); // the 80-bit indefinite
        assert_eq!(
            from_f80(0x8000_0000_0000_0001, 0x7fff, 0),
            0x7ff0_0000_0000_0001
        ); // an SNaN stays one
        assert_eq!(from_f80(0x0000_0000_0000_0001, 0, 2), 1); // the smallest 80-bit denormal, rounded up
    }

    fn ops(st0: f64, st1: f64) -> Operands {
        Operands {
            st0: Some(st0.to_bits()),
            st1: Some(st1.to_bits()),
            free: true,
            rc: 0,
        }
    }
    fn c(c0: bool, c1: bool, c2: bool, c3: bool) -> [Option<bool>; 4] {
        [Some(c0), Some(c1), Some(c2), Some(c3)]
    }
    fn ulps(a: f64, b: f64) -> u64 {
        (a.to_bits() as i64 - b.to_bits() as i64).unsigned_abs()
    }

    /// I6b helpers: the SDM's special cases, the C2 out-of-range rule, FPREM's quotient bits and partial remainders, stack faults.
    #[test]
    fn helpers() {
        let h = |f: u8, o: Operands| helper(f, o).unwrap();
        // FSIN/FCOS: zeros exact, |x| >= 2^63 out of range (C2, nothing written), infinity invalid, an empty ST(0) underflows.
        assert_eq!(
            h(0xfe, ops(-0.0, 0.0)),
            Effect {
                st0: Some((-0.0f64).to_bits()),
                c: [None, Some(false), Some(false), None],
                ..Default::default()
            }
        );
        assert_eq!(h(0xff, ops(0.0, 0.0)).st0, Some(ONE));
        for x in [TWO63, -TWO63, 1e300] {
            assert_eq!(
                h(0xfe, ops(x, 0.0)),
                Effect {
                    c: [None, Some(false), Some(true), None],
                    ..Default::default()
                }
            );
            assert_eq!(
                h(0xf2, ops(x, 0.0)),
                Effect {
                    c: [None, Some(false), Some(true), None],
                    ..Default::default()
                },
                "fptan: no push"
            );
        }
        assert!(h(0xfe, ops(TWO63 - 1024.0, 0.0)).c[2] == Some(false));
        let inf = h(0xff, ops(f64::INFINITY, 0.0));
        assert_eq!((inf.st0, inf.flags), (Some(INDEFINITE), IE));
        let under = h(
            0xfe,
            Operands {
                st0: None,
                st1: None,
                free: true,
                rc: 0,
            },
        );
        assert_eq!(
            (under.st0, under.flags, under.c[1]),
            (Some(INDEFINITE), IE | SF, Some(false))
        );
        let s = h(0xfe, ops(0.5, 0.0));
        assert!(ulps(f64::from_bits(s.st0.unwrap()), 0.479_425_538_604_203) <= 1 && s.flags == PE);
        // FPTAN pushes 1.0; FSINCOS pushes the cosine; a full stack (TOP - 1 not empty) overflows: both indefinite, C1 = 1.
        let t = h(0xf2, ops(0.25, 0.0));
        assert_eq!((t.push, t.flags), (Some(ONE), PE));
        let sc = h(0xfb, ops(1.0, 0.0));
        assert!(ulps(f64::from_bits(sc.push.unwrap()), 0.540_302_305_868_139_8) <= 1);
        let full = h(
            0xfb,
            Operands {
                free: false,
                ..ops(1.0, 0.0)
            },
        );
        assert_eq!(
            (full.st0, full.push, full.flags, full.c[1]),
            (Some(INDEFINITE), Some(INDEFINITE), IE | SF, Some(true))
        );
        // FPREM: 7 / 2 -> 1, quotient 3 (C0 = 0, C3 = 1, C1 = 1); -7 -> -1; FPREM1 rounds to nearest even: 7 / 2 = 3.5 -> 4, -1.
        assert_eq!(
            h(0xf8, ops(7.0, 2.0)),
            Effect {
                st0: Some(1f64.to_bits()),
                c: c(false, true, false, true),
                ..Default::default()
            }
        );
        assert_eq!(h(0xf8, ops(-7.0, 2.0)).st0, Some((-1f64).to_bits()));
        assert_eq!(
            h(0xf5, ops(7.0, 2.0)),
            Effect {
                st0: Some((-1f64).to_bits()),
                c: c(true, false, false, false),
                ..Default::default()
            }
        );
        assert_eq!(
            h(0xf5, ops(5.0, 2.0)),
            Effect {
                st0: Some(1f64.to_bits()),
                c: c(false, false, false, true),
                ..Default::default()
            }
        );
        assert_eq!(h(0xf8, ops(6.0, -2.0)).st0, Some(0)); // +0, the dividend's sign
        assert_eq!(h(0xf8, ops(1.0, 0.0)).flags, IE);
        assert_eq!(
            h(0xf8, ops(3.0, f64::INFINITY)),
            Effect {
                c: [Some(false); 4],
                ..Default::default()
            }
        );
        // The partial remainder: 2^100 by 3 takes several steps (C2 = 1) and ends with 1 and the quotient's bits (q = 5 mod 8).
        let mut x = 2f64.powi(100);
        let mut steps = 0;
        let last = loop {
            let r = h(0xf8, ops(x, 3.0));
            x = f64::from_bits(r.st0.unwrap());
            steps += 1;
            if r.c[2] == Some(false) {
                break r;
            }
            assert_eq!(r.c, c(false, false, true, false));
        };
        assert!(steps > 1);
        assert_eq!((x, last.c), (1.0, c(true, true, false, false)));
        // FSCALE: truncates ST(1); rounds by RC; overflow and underflow at the double's thresholds.
        assert_eq!(h(0xfd, ops(1.5, 3.7)).st0, Some(12f64.to_bits()));
        assert_eq!(
            h(0xfd, ops(1.0, 2000.0)),
            Effect {
                st0: Some(POS_INF),
                c: [None, Some(false), None, None],
                flags: OE | PE,
                ..Default::default()
            }
        );
        assert_eq!(
            h(
                0xfd,
                Operands {
                    rc: 3,
                    ..ops(1.0, 2000.0)
                }
            )
            .st0,
            Some(f64::MAX.to_bits())
        );
        assert_eq!(
            h(0xfd, ops(3.0, -1074.0)),
            Effect {
                st0: Some(3),
                c: [None, Some(false), None, None],
                ..Default::default()
            }
        );
        let tie = h(0xfd, ops(3.0, -1075.0));
        assert_eq!((tie.st0, tie.flags), (Some(2), UE | PE));
        assert_eq!(
            h(
                0xfd,
                Operands {
                    rc: 2,
                    ..ops(1.0, -1100.0)
                }
            )
            .st0,
            Some(1)
        );
        assert_eq!(h(0xfd, ops(0.0, f64::INFINITY)).flags, IE);
        assert_eq!(h(0xfd, ops(-5.0, f64::NEG_INFINITY)).st0, Some(SIGN));
        // FXTRACT.
        assert_eq!(
            h(0xf4, ops(12.0, 0.0)),
            Effect {
                st0: Some(3f64.to_bits()),
                push: Some(1.5f64.to_bits()),
                c: [None, Some(false), None, None],
                ..Default::default()
            }
        );
        assert_eq!(
            h(0xf4, ops(-0.0, 0.0)),
            Effect {
                st0: Some(NEG_INF),
                push: Some(SIGN),
                c: [None, Some(false), None, None],
                flags: ZE,
                ..Default::default()
            }
        );
        assert_eq!(
            h(0xf4, ops(-f64::from_bits(1), 0.0)).st0,
            Some((-1074f64).to_bits())
        );
        assert_eq!(
            h(0xf4, ops(-f64::from_bits(3), 0.0)).push,
            Some((-1.5f64).to_bits())
        );
        // FYL2X (ST(0) = x, ST(1) = y): exact for a power of two; zero is ZE; negative, 0 * log2(0) and inf * log2(1) invalid.
        let pop = |st1: u64, flags: u32| Effect {
            st1: Some(st1),
            pop: true,
            c: [None, Some(false), None, None],
            flags,
            ..Default::default()
        };
        assert_eq!(h(0xf1, ops(8.0, 3.0)), pop(9f64.to_bits(), 0));
        assert_eq!(h(0xf1, ops(0.0, 2.0)), pop(NEG_INF, ZE));
        assert_eq!(h(0xf1, ops(0.0, -2.0)), pop(POS_INF, ZE));
        assert_eq!(h(0xf1, ops(0.0, f64::NEG_INFINITY)), pop(POS_INF, 0));
        for (x, y) in [
            (-1.0, 1.0),
            (1.0, f64::INFINITY),
            (0.0, 0.0),
            (f64::INFINITY, 0.0),
            (f64::NEG_INFINITY, 2.0),
        ] {
            assert_eq!(h(0xf1, ops(x, y)), pop(INDEFINITE, IE), "fyl2x {x} {y}");
        }
        assert_eq!(h(0xf1, ops(3.0, 0.0)), pop(0, 0));
        assert_eq!(h(0xf9, ops(-0.0, -2.0)), pop(0, 0)); // -2 * log2(1 - 0) = +0
        assert_eq!(h(0xf9, ops(0.0, f64::INFINITY)), pop(INDEFINITE, IE));
        // FPATAN: atan2(ST(1), ST(0)).
        assert_eq!(h(0xf3, ops(1.0, 1.0)).flags, PE);
        assert_eq!(h(0xf3, ops(2.0, -0.0)), pop(SIGN, 0));
        assert_eq!(h(0xf3, ops(-2.0, 0.0)).flags, PE); // +pi
        assert_eq!(h(0xf3, ops(f64::INFINITY, 5.0)), pop(0, 0));
        let empty = h(
            0xf3,
            Operands {
                st1: None,
                ..ops(1.0, 1.0)
            },
        );
        assert_eq!(empty, pop(INDEFINITE, IE | SF));
        // F2XM1.
        assert_eq!(h(0xf0, ops(1.0, 0.0)).st0, Some(ONE));
        assert_eq!(h(0xf0, ops(-1.0, 0.0)).st0, Some((-0.5f64).to_bits()));
        assert_eq!(
            h(0xf0, ops(f64::NEG_INFINITY, 0.0)).st0,
            Some((-1f64).to_bits())
        );
        assert!(
            ulps(
                f64::from_bits(h(0xf0, ops(0.5, 0.0)).st0.unwrap()),
                0.414_213_562_373_095_03
            ) <= 1
        );
        // NaNs by the x87 rule: the larger significand, the QNaN over an SNaN, IE for an SNaN.
        let (q1, q2, s1) = (
            0x7ff8_0000_0000_0001u64,
            0xfff8_0000_0000_0002u64,
            0x7ff0_0000_0000_0009u64,
        );
        assert_eq!(
            h(0xf1, ops(f64::from_bits(q1), f64::from_bits(q2))),
            pop(q2, 0)
        );
        assert_eq!(
            h(0xf1, ops(f64::from_bits(s1), f64::from_bits(q1))),
            pop(q1, IE)
        );
        assert_eq!(
            h(0xfe, ops(f64::from_bits(s1), 0.0)).st0,
            Some(s1 | 1 << 51)
        );
        assert_eq!(helper(0xe5, ops(0.0, 0.0)), None);
    }
}
