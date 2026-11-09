//! F16C's binary32-to-binary16 conversion, with x86 MXCSR exception bits. No host floating-point state is used.

/// `rc` is the x86 rounding mode. FTZ is ignored; DAZ applies to the binary32 input.
/// Tininess is measured after rounding to 11 significant bits with an unbounded exponent (SDM Vol.1 4.9.1.5).
pub fn from_f32(bits: u32, rc: u32, mxcsr: u64) -> (u16, u64) {
    let sign = (bits >> 16 & 0x8000) as u16;
    let negative = sign != 0;
    let exponent = (bits >> 23 & 255) as i32;
    let fraction = bits & 0x7fffff;
    if exponent == 255 {
        return if fraction == 0 {
            (sign | 0x7c00, 0)
        } else {
            (
                sign | 0x7e00 | (fraction >> 13) as u16,
                u64::from(fraction & 0x400000 == 0),
            )
        };
    }
    if exponent == 0 && (fraction == 0 || mxcsr & (1 << 6) != 0) {
        return (sign, 0);
    }
    let mut flags = u64::from(exponent == 0) << 1;
    let mut mantissa = fraction | if exponent != 0 { 1 << 23 } else { 0 };
    let mut e = exponent.max(1) - 127;
    if exponent == 0 {
        let shift = mantissa.leading_zeros() - 8;
        mantissa <<= shift;
        e -= shift as i32;
    }
    let round = |shift: u32| {
        let kept = if shift < 32 { mantissa >> shift } else { 0 };
        let remainder = if shift < 32 {
            mantissa & ((1 << shift) - 1)
        } else {
            mantissa
        };
        let increment = remainder != 0
            && match rc & 3 {
                0 => {
                    shift < 32
                        && (remainder > (1 << (shift - 1))
                            || (remainder == (1 << (shift - 1)) && kept & 1 != 0))
                }
                1 => negative,
                2 => !negative,
                _ => false,
            };
        (kept + u32::from(increment), remainder != 0)
    };
    let (unbounded, _) = round(13);
    let tiny = e + i32::from(unbounded == 2048) < -14;
    let (rounded, inexact) = round((13 + (-14 - e).max(0)) as u32);
    let result = if e >= -14 {
        (((e + 14).max(0) as u32) << 10) + rounded
    } else {
        rounded
    };
    if e > 15 || result >= 0x7c00 {
        let infinity = match rc & 3 {
            0 => true,
            1 => negative,
            2 => !negative,
            _ => false,
        };
        return (
            sign | if infinity { 0x7c00 } else { 0x7bff },
            flags | 8 | 32,
        );
    }
    if inexact {
        flags |= 32;
    }
    if tiny && (inexact || mxcsr & (1 << 11) == 0) {
        flags |= 16;
    }
    (sign | result as u16, flags)
}

#[cfg(test)]
mod tests {
    use super::from_f32;
    #[test]
    fn every_half_encoding_round_trips() {
        for h in 0u32..=0xffff {
            let sign = (h & 0x8000) << 16;
            let exp = h >> 10 & 31;
            let fraction = h & 1023;
            let bits = if exp == 31 {
                sign | 0x7f800000 | fraction << 13
            } else if exp != 0 {
                sign | (exp + 112) << 23 | fraction << 13
            } else if fraction == 0 {
                sign
            } else {
                let top = 31 - fraction.leading_zeros();
                sign | (top + 103) << 23 | (fraction - (1 << top)) << (23 - top)
            };
            let want = if exp == 31 && fraction != 0 {
                h | 0x200
            } else {
                h
            };
            let flags = u64::from(exp == 31 && fraction != 0 && fraction & 0x200 == 0);
            for rc in 0..4 {
                assert_eq!(
                    from_f32(bits, rc, 0x1f80),
                    (want as u16, flags),
                    "half={h:04x} rc={rc}"
                );
            }
        }
    }
    #[test]
    fn rounding_and_exception_boundaries() {
        for (input, rc, mxcsr, result, flags) in [
            (0x3f800000, 0, 0x1f80, 0x3c00, 0),
            (0x80000000, 0, 0x1f80, 0x8000, 0),
            (0x7f800000, 0, 0x1f80, 0x7c00, 0),
            (0xff800001, 0, 0x1f80, 0xfe00, 1),
            (0x33800000, 0, 0x1f80, 1, 0),
            (0x33800000, 0, 0x1780, 1, 16),
            (0x33000000, 0, 0x1f80, 0, 48),
            (0x33000000, 2, 0x1f80, 1, 48),
            (0x387fffff, 0, 0x1f80, 0x400, 32),
            (0x387fe000, 0, 0x1f80, 0x400, 48),
            (1, 0, 0x1f80, 0, 50),
            (1, 0, 0x1fc0, 0, 0),
            (0x47800000, 0, 0x1f80, 0x7c00, 40),
            (0x47800000, 3, 0x1f80, 0x7bff, 40),
        ] {
            assert_eq!(
                from_f32(input, rc, mxcsr),
                (result, flags),
                "{input:08x} rc={rc} mxcsr={mxcsr:x}"
            );
        }
    }
}
