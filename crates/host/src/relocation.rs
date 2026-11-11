//! Reuse across load addresses (perf plan §3, chunks 3 and 4): fexc's raw object for guest bytes compiled at RIP `from`
//! turned into the object fexc gives for the same bytes at `to` (same half and page offset), by its relocation records
//! alone. The relocation proof (plan, "Measured (chunk 3)") found every accepted relocation byte-identical to a fresh
//! compile; whatever this module cannot make identical it refuses, and the caller compiles at `to`. Admission validates
//! the result at `to` as it does a fresh object.
use natemu_abi::code_object::{self as object, Header, Reloc};
use natemu_abi::execution::{canonical, relocate_move};

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
    if !same_place(from, to)
        || h.guest_rip != from
        || h.end_reason == object::end_reason::UNSUPPORTED_FORM
    {
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
            let start = code
                .checked_add(r.offset as usize)
                .filter(|&s| s < code + size && s % 4 == 0)?;
            let words: Vec<u32> = o
                .get(start..code + size)?
                .chunks_exact(4)
                .map(|w| u32::from_le_bytes(w.try_into().unwrap()))
                .collect();
            // The rule the resident's copies share (perf chunk 10).
            let (new, n, old) = relocate_move(&words, u32::from(r.register), d)?;
            if old != r.value {
                return None;
            }
            for (k, w) in new[..n].iter().enumerate() {
                out[start + 4 * k..start + 4 * k + 4].copy_from_slice(&w.to_le_bytes());
            }
        }
    }
    Some(out)
}

/// Whether a no-code answer of fexc's `reason` at one RIP is its answer at any other of the same half and page offset:
/// only the reasons fexc decides from the bytes (the proof's classes; `unsupported_form` is a non-canonical target).
pub(crate) fn no_code_reusable(reason: &str) -> bool {
    matches!(
        reason,
        "system_instruction" | "partial_decode" | "instruction_too_long" | "end_of_input"
    )
}

/// `from` and `to` have the same half (bit 63) and page offset: the only RIPs an answer is moved between.
pub(crate) fn same_place(from: u64, to: u64) -> bool {
    (from ^ to) & (1 << 63 | 0xfff) == 0
}

fn read<T: Copy>(b: &[u8], at: usize) -> Option<T> {
    let bytes = b.get(at..at.checked_add(size_of::<T>())?)?;
    // SAFETY: in bounds; the records are plain little-endian integers (`read_unaligned`).
    Some(unsafe { std::ptr::read_unaligned(bytes.as_ptr().cast()) })
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::execution::{
        LinkPoint, MAX_LINK_POINTS, RELOC_MOVE, bitmask, load_constant, logical_immediate,
        move_value, pack_reloc, refusal, relocate_code,
    };

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
        assert_eq!(
            object(USER_A.0, USER_A.1, USER_B.1).as_deref(),
            Some(USER_B.0)
        );
        assert_eq!(
            object(USER_B.0, USER_B.1, USER_A.1).as_deref(),
            Some(USER_A.0)
        );
        let moved = object(USER_A.0, USER_A.1, USER_B.1).unwrap();
        assert!(crate::admission::admit(&moved, USER_B.1, USER, u64::MAX, true, false).is_ok());
        // Not at the RIP it was not made for (admission's own check).
        assert!(crate::admission::admit(&moved, USER_A.1, USER, u64::MAX, true, false).is_err());
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

    /// Perf chunk 10: a placed block's code as the resident holds it (the admitted object's code, its link points linked:
    /// a `b` in each first word), its packed relocations and link points.
    fn placed(
        o: &[u8],
        rip: u64,
        source: &[u8],
    ) -> (Vec<u8>, Vec<u32>, [LinkPoint; MAX_LINK_POINTS], u32) {
        placed_at(o, rip, source, false)
    }
    fn placed_at(
        o: &[u8],
        rip: u64,
        source: &[u8],
        cpl0: bool,
    ) -> (Vec<u8>, Vec<u32>, [LinkPoint; MAX_LINK_POINTS], u32) {
        let a = crate::admission::admit(o, rip, source, u64::MAX, true, cpl0).unwrap();
        let h: Header = read(&a.object, 0).unwrap();
        let mut code = a.object[h.code_offset as usize..][..h.code_size as usize].to_vec();
        let mut relocs: Vec<u32> = (0..h.relocs_count as usize)
            .filter_map(|i| {
                pack_reloc(
                    &read::<Reloc>(&a.object, h.relocs_offset as usize + 16 * i).unwrap(),
                    h.code_size,
                )
                .unwrap()
            })
            .collect();
        relocs.sort(); // as `cache::place` keeps them
        let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
        links[..a.links.len()].copy_from_slice(&a.links);
        for p in &a.links {
            code[p.offset as usize..][..4].copy_from_slice(&0x1400_0040u32.to_le_bytes());
        }
        (code, relocs, links, h.guest_bytes)
    }

    /// The resident's re-check gives, for the block placed at one RIP, the code and link points admission gives fexc's
    /// object at the other (both ways): the same rewrite the host's relocator and admission make.
    #[test]
    fn resident_copy_is_the_admitted_block_at_the_new_rip() {
        for (from, to) in [(USER_A, USER_B), (USER_B, USER_A)] {
            let (mut code, relocs, mut links, bytes) = placed(from.0, from.1, USER);
            assert!(
                relocs.iter().any(|r| r & RELOC_MOVE != 0)
                    && relocs.iter().any(|r| r & RELOC_MOVE == 0)
                    && links[0].offset != 0
            );
            assert_eq!(
                relocate_code(&mut code, &relocs, &mut links, from.1, to.1, bytes),
                Ok(())
            );
            // The copy's link points are unlinked, as admitted.
            let a = crate::admission::admit(to.0, to.1, USER, u64::MAX, true, false).unwrap();
            let mut want_links = [LinkPoint::NONE; MAX_LINK_POINTS];
            let h: Header = read(&a.object, 0).unwrap();
            assert_eq!(
                code,
                a.object[h.code_offset as usize..][..h.code_size as usize]
            );
            want_links[..a.links.len()].copy_from_slice(&a.links);
            assert_eq!(links, want_links);
        }
    }

    /// With `FEXC` set (perf chunk 15 review): a CPL-0 block whose STI exits to its own RIP (and a RIP-relative LEA and, perf
    /// chunk 17, an inline SWAPGS) is fexc's object at another RIP once relocated, by the host's relocator and by the resident's
    /// re-check (link points included: the STI's exit goes to the STI at the new RIP), and is admitted there as a CPL-0 block.
    #[test]
    fn cpl0_sti_exit_is_relocated() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        // lea rax, [rip + 0x10]; swapgs; cli; sti; inc rax; ret
        let source = [
            0x48, 0x8d, 0x05, 0x10, 0, 0, 0, 0x0f, 0x01, 0xf8, 0xfa, 0xfb, 0x48, 0xff, 0xc0, 0xc3,
        ];
        let compile = |rip: u64| {
            let dir =
                std::env::temp_dir().join(format!("natemu-reloc-{}-{rip:x}", std::process::id()));
            let hex: String = source.iter().map(|b| format!("{b:02x}")).collect();
            let st = std::process::Command::new(&fexc)
                .args([
                    "--hex",
                    &hex,
                    "--rip",
                    &format!("{rip:#x}"),
                    "--profile",
                    "slice1",
                    "--cpl0",
                    "--object",
                ])
                .arg(&dir)
                .stdout(std::process::Stdio::null())
                .status()
                .unwrap();
            assert!(st.success());
            let o = std::fs::read(&dir).unwrap();
            std::fs::remove_file(&dir).unwrap();
            o
        };
        let (a, b) = (0xffff_ffff_8100_0100u64, 0xffff_ffff_8123_4100u64);
        let (oa, ob) = (compile(a), compile(b));
        assert_eq!(object(&oa, a, b).as_deref(), Some(&ob[..]));
        let admitted = crate::admission::admit(&ob, b, &source, u64::MAX, true, true).unwrap();
        assert!(
            admitted.links.iter().any(|p| p.target == b + 11),
            "the STI's exit at the new RIP (after the inline SWAPGS)"
        );
        let (mut code, relocs, mut links, bytes) = placed_at(&oa, a, &source, true);
        assert_eq!(
            relocate_code(&mut code, &relocs, &mut links, a, b, bytes),
            Ok(())
        );
        let h: Header = read(&admitted.object, 0).unwrap();
        assert_eq!(
            code,
            admitted.object[h.code_offset as usize..][..h.code_size as usize]
        );
        let mut want = [LinkPoint::NONE; MAX_LINK_POINTS];
        want[..admitted.links.len()].copy_from_slice(&admitted.links);
        assert_eq!(links, want);
    }

    /// PIC study stage 2 on fexc's objects: the block admitted at one fixture RIP, rewritten position-independent, is
    /// proved against its admitted code; copied to the other RIP (its link points linked first, as the resident's may be)
    /// it is proved against the object admission gives there; the proof fails against the wrong RIP's object.
    #[test]
    fn position_independent_rewrite_of_fexcs_objects() {
        use natemu_abi::pic;
        let admitted = |o: &[u8], rip: u64| {
            let a = crate::admission::admit(o, rip, USER, u64::MAX, true, false).unwrap();
            let h: Header = read(&a.object, 0).unwrap();
            a.object[h.code_offset as usize..][..h.code_size as usize].to_vec()
        };
        for (from, to) in [(USER_A, USER_B), (USER_B, USER_A)] {
            let (linked, relocs, mut links, bytes) = placed(from.0, from.1, USER);
            let original = admitted(from.0, from.1);
            let mut code = original.clone();
            for p in links.iter().filter(|p| p.offset != 0) {
                assert_eq!(
                    &linked[p.offset as usize..][..4],
                    0x1400_0040u32.to_le_bytes()
                );
                let at = p.offset as usize;
                code[at..at + 4].copy_from_slice(&p.word.to_le_bytes());
            }
            assert_eq!(pic::eligible(&code, &relocs, &links, from.1), Ok(()));
            assert_eq!(pic::rewrite(&mut code, &relocs, &mut links, from.1), Ok(()));
            assert!(pic::proved(&original, &code, &relocs, &links, from.1, 0));
            assert!(
                links
                    .iter()
                    .any(|p| p.offset != 0 && p.word == pic::load_base(0))
            );
            // Every move rewritten: no word of an absolute guest-RIP move of x0 or another register is left.
            assert_ne!(code, original);
            // Copied: a link point linked in its first word and another in its `svc` word, both restored.
            let mut copy = code.clone();
            for (i, p) in links.iter().filter(|p| p.offset != 0).enumerate() {
                let at = if i % 2 == 0 {
                    p.offset as u64
                } else {
                    pic::svc_slot(
                        |o| u32::from_le_bytes(copy[o as usize..][..4].try_into().unwrap()),
                        p,
                    )
                };
                copy[at as usize..][..4].copy_from_slice(&0x1400_0040u32.to_le_bytes());
            }
            let mut moved = links;
            assert_eq!(
                pic::relocate(&mut copy, &mut moved, from.1, to.1, bytes),
                Ok(())
            );
            assert_eq!(copy, code, "the copy's code is the template's");
            let there = admitted(to.0, to.1);
            assert!(pic::proved(&there, &copy, &relocs, &moved, to.1, 0));
            assert!(
                !pic::proved(&there, &copy, &relocs, &links, to.1, 0),
                "the template's link targets"
            );
            assert!(!pic::proved(&original, &copy, &relocs, &moved, to.1, 0));
        }
    }

    /// Each refusal of the re-check, and the shared move rule's.
    #[test]
    fn resident_copy_refusals() {
        let (code, relocs, links, bytes) = placed(USER_A.0, USER_A.1, USER);
        let try_to = |to: u64, relocs: &[u32], links: [LinkPoint; MAX_LINK_POINTS]| {
            let (mut code, mut links) = (code.clone(), links);
            relocate_code(&mut code, relocs, &mut links, USER_A.1, to, bytes)
        };
        // Place: another page offset or half, a non-canonical RIP or block end.
        assert_eq!(try_to(USER_B.1 + 8, &relocs, links), Err(refusal::PLACE));
        assert_eq!(
            try_to(0xffff_ffff_8123_4100, &relocs, links),
            Err(refusal::PLACE)
        );
        assert_eq!(
            try_to(0x0000_8000_0000_0100, &relocs, links),
            Err(refusal::PLACE)
        );
        let (mut c, mut l) = (code.clone(), links);
        assert_eq!(
            relocate_code(&mut c, &relocs, &mut l, USER_A.1, 0x7fff_ffff_f100, 0x1000),
            Err(refusal::PLACE)
        );
        // Move: another word count (bits 31:16 of every value become 0), a 32-bit form (below 4 GiB).
        assert_eq!(try_to(0x7f12_0000_0100, &relocs, links), Err(refusal::MOVE));
        assert_eq!(try_to(0x1234_5100, &relocs, links), Err(refusal::MOVE));
        // A move outside the code, or at a word that is no move.
        assert_eq!(
            try_to(
                USER_B.1,
                &[(code.len() as u32 / 4) << 6 | RELOC_MOVE],
                links
            ),
            Err(refusal::MOVE)
        );
        assert_eq!(
            try_to(USER_B.1, &[1 << 6 | RELOC_MOVE | 3], links),
            Err(refusal::MOVE)
        );
        // Literal: outside the code; a value made non-canonical (a forged slot: the half's top).
        assert_eq!(
            try_to(USER_B.1, &[((code.len() as u32 - 4) / 4) << 6], links),
            Err(refusal::LITERAL)
        );
        let literal = *relocs.iter().find(|r| *r & RELOC_MOVE == 0).unwrap();
        let mut forged = code.clone();
        let at = (literal >> 6) as usize * 4;
        forged[at..at + 8].copy_from_slice(&0x7fff_ffff_ff00u64.to_le_bytes());
        let mut l = links;
        assert_eq!(
            relocate_code(&mut forged, &[literal], &mut l, USER_A.1, USER_B.1, bytes),
            Err(refusal::LITERAL)
        );
        // A relocation listed twice (admission does not refuse it; a move rewritten twice would hold + 2d), or one starting
        // inside the one before, or out of order.
        let first_move = *relocs.iter().find(|r| *r & RELOC_MOVE != 0).unwrap();
        let mut twice = relocs.clone();
        twice.insert(
            twice.iter().position(|&r| r == first_move).unwrap(),
            first_move,
        );
        assert_eq!(try_to(USER_B.1, &twice, links), Err(refusal::MOVE));
        let mut twice = relocs.clone();
        twice.insert(twice.iter().position(|&r| r == literal).unwrap(), literal);
        assert_eq!(try_to(USER_B.1, &twice, links), Err(refusal::LITERAL));
        assert_eq!(
            try_to(USER_B.1, &[literal, literal + (1 << 6)], links),
            Err(refusal::LITERAL)
        );
        let mut reversed = relocs.clone();
        reversed.reverse();
        assert!(try_to(USER_B.1, &reversed, links).is_err());
        // Link points: one no move rewrote (its relocation dropped), and one whose target is not its move's value.
        let point = links[0].offset;
        let without: Vec<u32> = relocs
            .iter()
            .copied()
            .filter(|r| (r >> 6) * 4 != point)
            .collect();
        assert_eq!(try_to(USER_B.1, &without, links), Err(refusal::LINK));
        let mut off = links;
        off[0].target ^= 0x10;
        assert_eq!(try_to(USER_B.1, &relocs, off), Err(refusal::LINK));
        assert_eq!(try_to(USER_B.1, &relocs, links), Ok(()));
        // The move rule alone: a 32-bit form, a non-canonical new value, another word count.
        let (w, n) = load_constant(0x7f12_3456_7207, 5);
        assert_eq!(
            relocate_move(&w[..n], 5, 0x1000),
            Some((load_constant(0x7f12_3456_8207, 5).0, 3, 0x7f12_3456_7207))
        );
        assert_eq!(relocate_move(&w[..n], 5, 0x0000_0d00_0000_0000), None);
        assert_eq!(relocate_move(&w[..n], 5, 0xffff_ffff_cbaa_0000), None);
        assert_eq!(
            relocate_move(&load_constant(0x1234_5207, 5).0[..2], 5, 0x1000),
            None
        );
        assert_eq!(relocate_move(&w[..n], 6, 0x1000), None);
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
                        let w =
                            0x3200_0000 | sf << 31 | n << 22 | immr << 16 | imms << 10 | 31 << 5;
                        let Some(v) = bitmask(w) else { continue };
                        let v = if sf == 1 { v } else { v & 0xffff_ffff };
                        let (n2, r2, s2) =
                            logical_immediate(v, width).unwrap_or_else(|| panic!("{v:#x}"));
                        let w2 = 0x3200_0000 | sf << 31 | n2 << 22 | r2 << 16 | s2 << 10 | 31 << 5;
                        assert_eq!(
                            bitmask(w2).map(|b| if sf == 1 { b } else { b & 0xffff_ffff }),
                            Some(v),
                            "{w:#x}"
                        );
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
        for (c, n) in [
            (0x7f12_3456_7207u64, 3),
            (0xffff_ffff_8181_1fff, 2),
            (0x100005, 2),
            (0x7f8f_affa_0000, 2),
            (5, 1),
            (0xffff_ffff_ffff_fff0, 1),
            (0x7fff_ffff_f000, 1),
            (0x1000_0000_1000, 1),
            (0, 1),
        ] {
            let (w, k) = load_constant(c, 20);
            assert_eq!(k, n, "{c:#x}");
            let mask = if w[0] >> 31 == 1 {
                u64::MAX
            } else {
                0xffff_ffff
            };
            assert_eq!(move_value(&w[..k], 20), Some((n, c & mask)), "{c:#x}");
        }
        // The fixture's moves are exactly these words.
        let h: Header = read(USER_A.0, 0).unwrap();
        let code = &USER_A.0[h.code_offset as usize..][..h.code_size as usize];
        for i in 0..h.relocs_count as usize {
            let r: Reloc = read(USER_A.0, h.relocs_offset as usize + 16 * i).unwrap();
            if r.kind == object::RELOC_GUEST_RIP_MOVE {
                let (w, n) = load_constant(r.value, r.register);
                let have: Vec<u32> = code[r.offset as usize..][..4 * n]
                    .chunks(4)
                    .map(|b| u32::from_le_bytes(b.try_into().unwrap()))
                    .collect();
                assert_eq!(have, w[..n]);
            }
        }
    }
}
