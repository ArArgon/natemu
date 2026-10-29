//! x87 values (I6): FEX's reduced-precision mode keeps ST(i) as doubles; the 80-bit memory images (FLD/FSTP m80, FNSAVE/
//! FRSTOR, FXSAVE/FXRSTOR and XSAVE's x87 area) are converted here. Widening a double is exact; narrowing an 80-bit value
//! rounds it to a double by FCW.RC (the reduced-precision deviation, plan I6).

/// The x87 QNaN floating-point indefinite as a double (the 80-bit one: sign, exponent 7FFFH, significand C000..0H).
pub const INDEFINITE: u64 = 0xfff8_0000_0000_0000;

/// A double widened to the 80-bit format (exact): (significand with its integer bit, sign and exponent).
pub fn to_f80(d: u64) -> (u64, u16) {
    let (sign, exp, frac) = ((d >> 63) as u16, (d >> 52 & 0x7ff) as u16, d & ((1 << 52) - 1));
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
        (0, keep == 0 && m >> 63 != 0, if keep == 0 { m << 1 != 0 } else { true })
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
            return sign | if to_inf { 0x7ff << 52 } else { 0x7fe << 52 | ((1 << 52) - 1) };
        }
        (biased as u64) << 52 | frac & ((1 << 52) - 1)
    } else {
        v // a denormal (exponent field 0), or the smallest normal after a carry into bit 52
    };
    sign | bits
}


#[cfg(test)]
mod tests {
    use super::*;
    /// Widening is exact and rounding back is the identity on doubles; 80-bit values round by RC.
    #[test]
    fn f80_conversions() {
        for d in [0u64, 1 << 63, 1, 0x000f_ffff_ffff_ffff, 0x0010_0000_0000_0000, 0x3ff0_0000_0000_0000, 0xc009_21fb_5444_2d18,
                  0x7fef_ffff_ffff_ffff, 0x7ff0_0000_0000_0000, 0xfff8_0000_0000_0000, 0x7ff4_0000_0000_0001, 0x8000_0000_0000_0003] {
            let (m, se) = to_f80(d);
            for rc in 0..4 {
                assert_eq!(from_f80(m, se, rc), d, "{d:#x} rc {rc}");
            }
        }
        assert_eq!(to_f80(0x3ff0_0000_0000_0000), (1 << 63, 0x3fff));
        assert_eq!(to_f80(1), (1 << 63, 15372 - 63)); // 2^-1074
        // 1 + 2^-63: nearest and down give 1.0, up gives 1 + 2^-52.
        let (m, se) = (1 << 63 | 1, 0x3fff);
        assert_eq!([0, 1, 2, 3].map(|rc| from_f80(m, se, rc)), [0x3ff0_0000_0000_0000, 0x3ff0_0000_0000_0000, 0x3ff0_0000_0000_0001, 0x3ff0_0000_0000_0000]);
        // Ties to even: 1 + 2^-53 (halfway) rounds to 1.0; 1 + 3 * 2^-53 to 1 + 2^-51.
        assert_eq!(from_f80(1 << 63 | 1 << 10, 0x3fff, 0), 0x3ff0_0000_0000_0000);
        assert_eq!(from_f80(1 << 63 | 3 << 10, 0x3fff, 0), 0x3ff0_0000_0000_0002);
        // Overflow: 2^1024 is infinity to nearest, the largest double toward zero.
        assert_eq!(from_f80(1 << 63, 0x3fff + 1024, 0), 0x7ff0_0000_0000_0000);
        assert_eq!(from_f80(1 << 63, 0x3fff + 1024, 3), 0x7fef_ffff_ffff_ffff);
        assert_eq!(from_f80(1 << 63, 0xbfff + 1024, 2), 0xffef_ffff_ffff_ffff);
        // Underflow: 2^-1075 is a tie to 0 (even), 2^-1075 + tiny rounds to 2^-1074; 2^-2000 is 0, or 2^-1074 rounding up.
        assert_eq!(from_f80(1 << 63, 0x3fff - 1075, 0), 0);
        assert_eq!(from_f80(1 << 63 | 1, 0x3fff - 1075, 0), 1);
        assert_eq!([0, 2].map(|rc| from_f80(1 << 63, 0x3fff - 2000, rc)), [0, 1]);
        // The largest denormal rounding up carries into the smallest normal.
        assert_eq!(from_f80(u64::MAX, 0x3fff - 1023, 0), 0x0010_0000_0000_0000);
        // Unsupported encodings and NaN payloads.
        assert_eq!(from_f80(0x4000_0000_0000_0000, 0x3fff, 0), INDEFINITE); // unnormal
        assert_eq!(from_f80(0x4000_0000_0000_0000, 0x7fff, 0), INDEFINITE); // pseudo-NaN
        assert_eq!(from_f80(0xc000_0000_0000_0000, 0xffff, 0), INDEFINITE); // the 80-bit indefinite
        assert_eq!(from_f80(0x8000_0000_0000_0001, 0x7fff, 0), 0x7ff0_0000_0000_0001); // an SNaN stays one
        assert_eq!(from_f80(0x0000_0000_0000_0001, 0, 2), 1); // the smallest 80-bit denormal, rounded up
    }
}
