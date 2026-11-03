//! Reuse across load addresses (perf plan §3, chunks 3 and 4): fexc's raw object for guest bytes compiled at RIP `from`
//! turned into the object fexc gives for the same bytes at `to` (same half and page offset), by its relocation records
//! alone. The relocation proof (plan, "Measured (chunk 3)") found every accepted relocation byte-identical to a fresh
//! compile; whatever this module cannot make identical it refuses, and the caller compiles at `to`. Admission validates
//! the result at `to` as it does a fresh object.
use natemu_abi::code_object::{self as object, Header, Reloc};
use natemu_abi::execution::move_value;

/// The object for `to`, or `None` when relocation alone would not give fexc's own answer there:
/// - the block was cut before a direct branch to a non-canonical target (`unsupported_form`): at `to` the target may be
///   canonical and fexc compile further (the reverse canonical case);
/// - a relocated value, `to` or the block's end is not canonical (fexc cuts such a branch at `to`);
/// - a guest-RIP move's words are a 32-bit form, before or after (conservative: a 32-bit `EntrypointOffset` would be
///   masked by FEX, which an added shift is not; slice1 is 64-bit only, so this never occurs, but nothing here proves it);
/// - a move needs another number of words for its new value (FEX's `LoadConstant` picks the form by the value);
/// - a relocation of any other kind, or a malformed object (admission's to refuse; not relocated).
pub(crate) fn object(o: &[u8], from: u64, to: u64) -> Option<Vec<u8>> {
    let h: Header = read(o, 0)?;
    let d = to.wrapping_sub(from);
    // Only between RIPs of the same half and page offset (the cache keys promise it; a forged disk entry's RIP is only
    // checksum-protected).
    if !same_place(from, to) || h.guest_rip != from || h.end_reason == object::end_reason::UNSUPPORTED_FORM {
        return None;
    }
    if !canonical(to) || !canonical(to.wrapping_add(h.guest_bytes.into())) {
        return None;
    }
    let (code, size) = (h.code_offset as usize, h.code_size as usize);
    let mut out = o.to_vec();
    out[24..32].copy_from_slice(&to.to_le_bytes());
    for i in 0..h.relocs_count as usize {
        let at = (h.relocs_offset as usize).checked_add(i * size_of::<Reloc>())?;
        let r: Reloc = read(o, at)?;
        match r.kind {
            object::RELOC_GUEST_RIP_LITERAL | object::RELOC_GUEST_RIP_MOVE => {}
            object::RELOC_NAMED_SYMBOL_LITERAL | object::RELOC_DISPATCH_BRANCH => continue,
            _ => return None,
        }
        let value = r.value.wrapping_add(d);
        if !canonical(value) {
            return None;
        }
        out[at + 8..at + 16].copy_from_slice(&value.to_le_bytes());
        if r.kind == object::RELOC_GUEST_RIP_MOVE {
            let start = code.checked_add(r.offset as usize).filter(|&s| s < code + size && s % 4 == 0)?;
            let words: Vec<u32> = o.get(start..code + size)?.chunks_exact(4).map(|w| u32::from_le_bytes(w.try_into().unwrap())).collect();
            let (n, old) = move_value(&words, u32::from(r.register))?;
            let new = load_constant(value, r.register);
            if old != r.value || words[0] >> 31 == 0 || new[0] >> 31 == 0 || new.len() != n {
                return None;
            }
            for (k, w) in new.iter().enumerate() {
                out[start + 4 * k..start + 4 * k + 4].copy_from_slice(&w.to_le_bytes());
            }
        }
    }
    Some(out)
}

/// Whether a no-code answer of fexc's `reason` at one RIP is its answer at any other of the same half and page offset:
/// only the reasons fexc decides from the bytes (the proof's classes; `unsupported_form` is a non-canonical target).
pub(crate) fn no_code_reusable(reason: &str) -> bool {
    matches!(reason, "system_instruction" | "partial_decode" | "instruction_too_long" | "end_of_input")
}

/// `from` and `to` have the same half (bit 63) and page offset: the only RIPs an answer is moved between.
pub(crate) fn same_place(from: u64, to: u64) -> bool {
    (from ^ to) & (1 << 63 | 0xfff) == 0
}

fn canonical(a: u64) -> bool {
    a >> 47 == 0 || a >> 47 == 0x1ffff
}

fn read<T: Copy>(b: &[u8], at: usize) -> Option<T> {
    let bytes = b.get(at..at.checked_add(size_of::<T>())?)?;
    // SAFETY: in bounds; the records are plain little-endian integers (`read_unaligned`).
    Some(unsafe { std::ptr::read_unaligned(bytes.as_ptr().cast()) })
}

/// The logical-immediate fields (N, immr, imms) of `v` as a `width`-bit bitmask, if it is one.
fn logical_immediate(v: u64, width: u32) -> Option<(u32, u32, u32)> {
    let mask = if width == 64 { u64::MAX } else { (1 << width) - 1 };
    if v & !mask != 0 || v == 0 || v == mask {
        return None;
    }
    let mut e = width;
    while e > 2 && (v ^ v >> (e / 2)) & ((1 << (e / 2)) - 1) == 0 {
        e /= 2;
    }
    let emask = if e == 64 { u64::MAX } else { (1 << e) - 1 };
    let elem = v & emask;
    let ones = elem.count_ones();
    let run = if ones == 64 { u64::MAX } else { (1 << ones) - 1 };
    // elem = ROR(run, r): find r by rotating elem left until it is the run at the bottom.
    let r = (0..e).find(|&r| (elem << r | elem >> ((e - r) % e)) & emask == run)?;
    let n = u32::from(e == 64);
    Some((n, r, (!(2 * e - 1) & 0x3f) | (ones - 1)))
}

/// FEX's `Arm64Emitter::LoadConstant(i64, reg, c, NOPAD)` (series48-lean): the words that build `c` in `reg`.
fn load_constant(c: u64, reg: u8) -> Vec<u32> {
    let rd = u32::from(reg);
    let mov = |op: u32, sf: u32, imm: u64, hw: u32| op | sf << 31 | hw << 21 | ((imm & 0xffff) as u32) << 5 | rd;
    const MOVN: u32 = 0x1280_0000;
    const MOVZ: u32 = 0x5280_0000;
    const MOVK: u32 = 0x7280_0000;
    if !c >> 16 == 0 {
        return vec![mov(MOVN, 1, !c, 0)];
    }
    let (sf, segments) = if c >> 32 == 0 { (0, 2) } else { (1, 4) };
    if sf == 0 && !c & 0xffff_0000 == 0 {
        return vec![mov(MOVN, 0, !c, 0)];
    }
    let parts: Vec<u64> = (0..segments).map(|i| c >> (16 * i) & 0xffff).collect();
    if parts.iter().filter(|&&p| p != 0).count() > 1 {
        if let Some((n, immr, imms)) = logical_immediate(c, if sf == 1 { 64 } else { 32 }) {
            return vec![0x3200_0000 | sf << 31 | n << 22 | immr << 16 | imms << 10 | 31 << 5 | rd];
        }
    }
    if sf == 1 && !c >> 32 == 0 {
        return vec![mov(MOVN, 1, !c, 0), mov(MOVK, 1, c >> 16, 1)];
    }
    let mut words = vec![];
    for (i, &p) in parts.iter().enumerate() {
        if p != 0 {
            words.push(mov(if words.is_empty() { MOVZ } else { MOVK }, sf, p, i as u32));
        }
    }
    if words.is_empty() {
        words.push(mov(MOVZ, sf, 0, 0));
    }
    words
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::execution::bitmask;

    macro_rules! fixture {
        ($name:literal) => {
            include_bytes!(concat!("../tests/relocation/", $name)) as &[u8]
        };
    }
    const USER: &[u8] = fixture!("user.src");
    const USER_A: (&[u8], u64) = (fixture!("user-7f1234567100.obj"), 0x7f12_3456_7100);
    const USER_B: (&[u8], u64) = (fixture!("user-7f1234568100.obj"), 0x7f12_3456_8100);
    /// `add rax, rbx; jmp +0x7fffffff` (review): at 0x7fffff000100 fexc cuts before the jmp (a non-canonical target);
    /// at 0x7ff0ff000100 it compiles both.
    const CUT_A: (&[u8], u64) = (fixture!("cut-7fffff000100.obj"), 0x7fff_ff00_0100);
    const CUT_B: (&[u8], u64) = (fixture!("cut-7ff0ff000100.obj"), 0x7ff0_ff00_0100);

    #[test]
    fn relocation_gives_fexcs_object_at_the_new_rip() {
        // Both ways, and admitted at the new RIP as the fresh object is.
        assert_eq!(object(USER_A.0, USER_A.1, USER_B.1).as_deref(), Some(USER_B.0));
        assert_eq!(object(USER_B.0, USER_B.1, USER_A.1).as_deref(), Some(USER_A.0));
        let moved = object(USER_A.0, USER_A.1, USER_B.1).unwrap();
        assert!(crate::admission::admit(&moved, USER_B.1, USER, u64::MAX, true).is_ok());
        // Not at the RIP it was not made for (admission's own check).
        assert!(crate::admission::admit(&moved, USER_A.1, USER, u64::MAX, true).is_err());
    }

    #[test]
    fn each_refusal() {
        // Kept for another RIP than said; to another page offset or half.
        assert_eq!(object(USER_A.0, USER_B.1, USER_A.1), None);
        assert_eq!(object(USER_A.0, USER_A.1, USER_B.1 + 8), None);
        assert_eq!(object(USER_A.0, USER_A.1, 0xffff_ffff_8123_4100), None);
        assert!(same_place(USER_A.1, USER_B.1) && !same_place(USER_A.1, USER_B.1 + 8));
        // Move length: at 0x7f1200000100 bits 31:16 of every RIP-relative value are 0 (movz+movk, not movz+movk+movk).
        assert_eq!(object(USER_A.0, USER_A.1, 0x7f12_0000_0100), None);
        // A 32-bit form: below 4 GiB (here into it).
        assert_eq!(object(USER_A.0, USER_A.1, 0x1234_5100), None);
        // The reverse canonical case: the cut object is never relocated, though every value would be canonical.
        assert_eq!(object(CUT_A.0, CUT_A.1, CUT_B.1), None);
        // Non-canonical: the other way, the jmp's target passes the half's top.
        assert_eq!(object(CUT_B.0, CUT_B.1, CUT_A.1), None);
        // A relocation kind it does not know.
        let mut odd = USER_A.0.to_vec();
        let h: Header = read(&odd, 0).unwrap();
        odd[h.relocs_offset as usize + 4] = object::RELOC_OTHER;
        assert_eq!(object(&odd, USER_A.1, USER_B.1), None);
        // Truncated.
        assert_eq!(object(&USER_A.0[..200], USER_A.1, USER_B.1), None);
        assert!(no_code_reusable("system_instruction") && !no_code_reusable("unsupported_form"));
    }

    /// Every logical immediate the decoder knows encodes back to one that decodes to it, and nothing else encodes.
    #[test]
    fn logical_immediates_round_trip() {
        for sf in [0u32, 1] {
            let width = 32 << sf;
            for n in 0..=sf {
                for immr in 0..64 {
                    for imms in 0..64 {
                        if n << 6 | (!imms & 63) == 0 {
                            continue; // reserved (and `bitmask` would overflow in a debug build)
                        }
                        let w = 0x3200_0000 | sf << 31 | n << 22 | immr << 16 | imms << 10 | 31 << 5;
                        let Some(v) = bitmask(w) else { continue };
                        let v = if sf == 1 { v } else { v & 0xffff_ffff };
                        let (n2, r2, s2) = logical_immediate(v, width).unwrap_or_else(|| panic!("{v:#x}"));
                        let w2 = 0x3200_0000 | sf << 31 | n2 << 22 | r2 << 16 | s2 << 10 | 31 << 5;
                        assert_eq!(bitmask(w2).map(|b| if sf == 1 { b } else { b & 0xffff_ffff }), Some(v), "{w:#x}");
                    }
                }
            }
        }
        assert_eq!(logical_immediate(0x5, 64), None);
        assert_eq!(logical_immediate(0x7f12_3456_7207, 64), None);
    }

    /// The words FEX emitted (in the fixtures and the proof's classes) and their values.
    #[test]
    fn load_constant_is_fexs() {
        for (c, n) in [(0x7f12_3456_7207u64, 3), (0xffff_ffff_8181_1fff, 2), (0x100005, 2), (0x7f8f_affa_0000, 2), (5, 1),
                       (0xffff_ffff_ffff_fff0, 1), (0x7fff_ffff_f000, 1), (0x1000_0000_1000, 1), (0, 1)] {
            let w = load_constant(c, 20);
            assert_eq!(w.len(), n, "{c:#x}");
            let mask = if w[0] >> 31 == 1 { u64::MAX } else { 0xffff_ffff };
            assert_eq!(move_value(&w, 20), Some((n, c & mask)), "{c:#x}");
        }
        // The fixture's moves are exactly these words.
        let h: Header = read(USER_A.0, 0).unwrap();
        let code = &USER_A.0[h.code_offset as usize..][..h.code_size as usize];
        for i in 0..h.relocs_count as usize {
            let r: Reloc = read(USER_A.0, h.relocs_offset as usize + 16 * i).unwrap();
            if r.kind == object::RELOC_GUEST_RIP_MOVE {
                let w = load_constant(r.value, r.register);
                let have: Vec<u32> = code[r.offset as usize..][..4 * w.len()].chunks(4).map(|b| u32::from_le_bytes(b.try_into().unwrap())).collect();
                assert_eq!(have, w);
            }
        }
    }
}
