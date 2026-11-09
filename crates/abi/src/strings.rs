//! PCMPESTRI/PCMPESTRM/PCMPISTRI/PCMPISTRM and their VEX.128 forms (I6b, I5c's string compares): the intermediate result
//! FEX's VPCMPESTRX/VPCMPISTRX helpers return (`Interpreter/Fallbacks/VectorFallbacks.h` and `StringCompareFallbacks.cpp`,
//! ported), computed by the resident for `svc #STRING` ([`crate::execution::svc::STRING`]). FEX's IR derives ECX or XMM0
//! and RFLAGS from it (CF, ZF, SF, OF from bits 29, 30, 31, 28; AF = PF = 0).

/// The control word in w0: the instruction's imm8, then REX.W/VEX.W (64-bit RAX/RDX lengths), then the explicit-length bit.
pub const WIDE: u32 = 1 << 8;
pub const EXPLICIT: u32 = 1 << 9;

/// IntRes2 (bits 15:0) with SF ZF CF OF in bits 31:28. `lhs` = xmm1, `rhs` = xmm2/m128; `rax`/`rdx` are read only for the
/// explicit forms (`control & EXPLICIT`).
pub fn pcmpstr(lhs: u128, rhs: u128, control: u32, rax: u64, rdx: u64) -> u32 {
    let words = control & 1 != 0;
    let n = if words { 8 } else { 16 };
    let (len1, len2) = if control & EXPLICIT != 0 {
        (explicit(rax, control), explicit(rdx, control))
    } else {
        (implicit(lhs, words), implicit(rhs, words))
    };
    let int1 = aggregate(lhs, len1, rhs, len2, control, n);
    let all = (1u32 << n) - 1;
    let int2 = match control >> 4 & 3 {
        1 => int1 ^ all,
        3 => int1 ^ ((1u32 << len2) - 1),
        _ => int1,
    };
    let flags =
        u32::from(len1 < n) << 3 | u32::from(len2 < n) << 2 | u32::from(int2 != 0) << 1 | int2 & 1;
    int2 | flags << 28
}

/// The element count up to the first zero element (n if none).
fn implicit(v: u128, words: bool) -> u32 {
    let n = if words { 8 } else { 16 };
    (0..n)
        .find(|&i| element(v, i, words as u32) == 0)
        .unwrap_or(n)
}
/// |EAX| or |RAX| (REX.W), saturated to the element count.
fn explicit(reg: u64, control: u32) -> u32 {
    let n: i64 = if control & 1 != 0 { 8 } else { 16 };
    let v = if control & WIDE != 0 {
        reg as i64
    } else {
        reg as i32 as i64
    };
    if v < -n || v > n {
        n as u32
    } else {
        v.unsigned_abs() as u32
    }
}
/// Element `i` by the data format (control 1:0: unsigned bytes, unsigned words, signed bytes, signed words).
fn element(v: u128, i: u32, control: u32) -> i32 {
    match control & 3 {
        0 => (v >> (8 * i)) as u8 as i32,
        1 => (v >> (16 * i)) as u16 as i32,
        2 => (v >> (8 * i)) as i8 as i32,
        _ => (v >> (16 * i)) as i16 as i32,
    }
}

/// IntRes1 by the aggregation (control 3:2), FEX's loops with the SDM's validity overrides folded in.
fn aggregate(lhs: u128, len1: u32, rhs: u128, len2: u32, control: u32, n: u32) -> u32 {
    let (l, r) = (|i| element(lhs, i, control), |j| element(rhs, j, control));
    let mut res = 0;
    match control >> 2 & 3 {
        // Equal any: rhs[j] equals some valid lhs element.
        0 => (0..len2).for_each(|j| res |= u32::from((0..len1).any(|i| l(i) == r(j))) << j),
        // Ranges: rhs[j] inside some complete valid pair [lhs[2k], lhs[2k + 1]].
        1 => (0..len2).for_each(|j| {
            res |= u32::from((0..len1 / 2).any(|k| l(2 * k) <= r(j) && r(j) <= l(2 * k + 1))) << j
        }),
        // Equal each: both valid and equal, or both invalid.
        2 => (0..n).for_each(|i| {
            let (a, b) = (i < len1, i < len2);
            res |= u32::from(if a && b { l(i) == r(i) } else { !a && !b }) << i
        }),
        // Equal ordered: the valid lhs prefix matches rhs from j on (an lhs element past rhs's end fails, one past the
        // vector's end is not compared).
        _ => (0..n).for_each(|j| {
            res |= u32::from((0..len1.min(n - j)).all(|i| i + j < len2 && l(i) == r(i + j))) << j
        }),
    }
    res
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The SDM's definition (Vol.2 4.1, "Imm8 Control Byte Operation for PCMPESTRI / PCMPESTRM / PCMPISTRI / PCMPISTRM"),
    /// written from the tables independently of the port: BoolRes[j][i] (j over xmm2/m128, i over xmm1), the validity
    /// overrides of Table 4-7, the aggregations of 4.1.4, the polarities of 4.1.5 and the flags of 4.1.7.
    fn sdm(lhs: u128, rhs: u128, imm: u32, explicit: Option<(u64, u64, bool)>) -> u32 {
        let n = if imm & 1 != 0 { 8u32 } else { 16 };
        let get = |v: u128, i: u32| -> i64 {
            match imm & 3 {
                0 => (v >> (8 * i) & 0xff) as i64,
                1 => (v >> (16 * i) & 0xffff) as i64,
                2 => (v >> (8 * i) & 0xff) as u8 as i8 as i64,
                _ => (v >> (16 * i) & 0xffff) as u16 as i16 as i64,
            }
        };
        let len = |v: u128, reg: Option<(u64, bool)>| -> u32 {
            match reg {
                Some((r, w)) => {
                    let x: i128 = if w {
                        r as i64 as i128
                    } else {
                        r as i32 as i128
                    };
                    x.unsigned_abs().min(n as u128) as u32
                }
                None => (0..n).position(|i| get(v, i) == 0).map_or(n, |p| p as u32),
            }
        };
        let (len1, len2) = match explicit {
            Some((rax, rdx, w)) => (len(lhs, Some((rax, w))), len(rhs, Some((rdx, w)))),
            None => (len(lhs, None), len(rhs, None)),
        };
        let agg = imm >> 2 & 3;
        let boolres = |j: u32, i: u32| -> bool {
            let (vi, vj) = (i < len1, j < len2);
            match agg {
                0 | 1 if !vi || !vj => false,
                2 if !vi || !vj => !vi && !vj,
                3 if !vi => true,
                3 if !vj => false,
                1 => {
                    if i % 2 == 0 {
                        get(lhs, i) <= get(rhs, j)
                    } else {
                        get(lhs, i) >= get(rhs, j)
                    }
                }
                _ => get(lhs, i) == get(rhs, j),
            }
        };
        let mut int1 = 0u32;
        for j in 0..n {
            let bit = match agg {
                0 => (0..n).any(|i| boolres(j, i)),
                1 => (0..n)
                    .step_by(2)
                    .any(|i| boolres(j, i) && boolres(j, i + 1)),
                2 => boolres(j, j),
                _ => (0..n - j).all(|i| boolres(i + j, i)),
            };
            int1 |= u32::from(bit) << j;
        }
        let mut int2 = 0u32;
        for i in 0..n {
            let b = int1 >> i & 1 != 0;
            let v = match imm >> 4 & 3 {
                1 => !b,
                3 => {
                    if i < len2 {
                        !b
                    } else {
                        b
                    }
                }
                _ => b,
            };
            int2 |= u32::from(v) << i;
        }
        let (cf, zf, sf, of) = (int2 != 0, len2 < n, len1 < n, int2 & 1 != 0);
        int2 | u32::from(sf) << 31 | u32::from(zf) << 30 | u32::from(cf) << 29 | u32::from(of) << 28
    }

    struct Rng(u64);
    impl Rng {
        fn next(&mut self) -> u64 {
            self.0 ^= self.0 << 13;
            self.0 ^= self.0 >> 7;
            self.0 ^= self.0 << 17;
            self.0
        }
        /// A string vector: elements from a small alphabet (so matches happen), zeros at random places, sometimes none.
        fn vector(&mut self, words: bool) -> u128 {
            let (n, w) = if words { (8, 16) } else { (16, 8) };
            let mut v = 0u128;
            let zero_at = if self.next() % 3 == 0 {
                n
            } else {
                (self.next() % (n as u64 + 1)) as u32
            };
            for i in 0..n {
                let e: u128 = match self.next() % 8 {
                    0 if i != zero_at => self.next() as u128 & ((1 << w) - 1),
                    1 => {
                        if words {
                            0x8000 | (self.next() & 0xff) as u128
                        } else {
                            0x80 | (self.next() & 7) as u128
                        }
                    }
                    _ => 0x41 + (self.next() % 6) as u128,
                };
                v |= if i == zero_at { 0 } else { e.max(1) } << (w * i);
            }
            v
        }
    }

    #[test]
    fn port_matches_sdm_every_imm8() {
        let mut r = Rng(0x9e37_79b9_7f4a_7c15);
        let lens = [
            0u64,
            1,
            2,
            3,
            5,
            7,
            8,
            9,
            15,
            16,
            17,
            100,
            -1i64 as u64,
            -3i64 as u64,
            -8i64 as u64,
            -16i64 as u64,
            -17i64 as u64,
            0x8000_0000,
            0xffff_ffff_0000_0003,
            0x1_0000_0002,
            i64::MIN as u64,
        ];
        for imm in 0..256u32 {
            for _ in 0..200 {
                let words = imm & 1 != 0;
                let (a, b) = (r.vector(words), r.vector(words));
                let ctl = imm & 0x7f; // bit 6 selects only the output form, FEX's IR's
                assert_eq!(
                    pcmpstr(a, b, ctl, 0, 0),
                    sdm(a, b, ctl, None),
                    "implicit imm {imm:#x} {a:#x} {b:#x}"
                );
                for wide in [false, true] {
                    let (rax, rdx) = (
                        lens[(r.next() % lens.len() as u64) as usize],
                        lens[(r.next() % lens.len() as u64) as usize],
                    );
                    let c = ctl | EXPLICIT | if wide { WIDE } else { 0 };
                    assert_eq!(
                        pcmpstr(a, b, c, rax, rdx),
                        sdm(a, b, ctl, Some((rax, rdx, wide))),
                        "explicit imm {imm:#x} wide {wide} {a:#x} {b:#x} {rax:#x} {rdx:#x}"
                    );
                }
            }
        }
    }

    #[test]
    fn glibc_strcspn_case() {
        // pcmpistri $0x12 (unsigned bytes, equal any, negative polarity): the kernel's stop in glibc's strcspn.
        let set = u128::from_le_bytes(*b"abc\0\0\0\0\0\0\0\0\0\0\0\0\0");
        let s = u128::from_le_bytes(*b"xyzb\0\0\0\0\0\0\0\0\0\0\0\0");
        let r = pcmpstr(set, s, 0x12, 0, 0);
        // Negative polarity over all 16 elements: x y z (no match) -> 1, b -> 0, the invalid tail -> 1.
        assert_eq!(r & 0xffff, 0xfff7);
        assert_eq!(r >> 28, 0b1111); // SF (the set has a NUL), ZF (the string has one), CF (IntRes2 != 0), OF (IntRes2[0])
    }
}
