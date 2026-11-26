//! Reuse across load addresses (perf plan §3, chunks 3 and 4): fexc's raw object for guest bytes compiled at RIP `from`
//! turned into the object fexc gives for the same bytes at `to` (same half and page offset), by its relocation records
//! alone. The relocation proof (plan, "Measured (chunk 3)") found every accepted relocation byte-identical to a fresh
//! compile; whatever this module cannot make identical it refuses, and the caller compiles at `to`. Admission validates
//! the result at `to` as it does a fresh object.
use natemu_abi::code_object::{self as object, Header, Reloc};
use crate::admission::Admitted;
use natemu_abi::execution::{
    LinkPoint, MAX_LINK_POINTS, cache::MAX_RELOCS, canonical, in_32_bits, pack_reloc, relocate_code_mode,
    relocate_move_mode,
};

/// The object for `to`, or `None` when relocation alone would not give fexc's own answer there:
/// - the block was cut before a direct branch to a non-canonical target (`unsupported_form`): at `to` the target may be
///   canonical and fexc compile further (the reverse canonical case);
/// - a relocated value, `to` or the block's end is not canonical (fexc cuts such a branch at `to`);
/// - in 64-bit code, a guest-RIP move's words are a 32-bit form, before or after (conservative: nothing proves fexc's
///   choice there; such code runs at one RIP);
/// - in 32-bit code (the object's own `guest_bitness`, which admission holds to the request's mode; M2 reuse), where fexc
///   masks every guest-EIP constant to 32 bits and loads it in the 32-bit form: a move in another form, or a RIP, block
///   end or relocated value, old or new, not below 4 GiB (a wrap at 4 GiB is never relocated);
/// - a move needs another number of words for its new value (FEX's `LoadConstant` picks the form by the value);
/// - a relocation of any other kind, or a malformed object (admission's to refuse; not relocated).
pub(crate) fn object(o: &[u8], from: u64, to: u64) -> Option<Vec<u8>> {
    // Only between RIPs of the same half and page offset (the cache keys promise it; a forged disk entry's RIP is only
    // checksum-protected).
    if !same_place(from, to) {
        return None;
    }
    moved(o, from, to)
}

/// [`object`] between RIPs of the same half at any page offsets, for 64-bit code whose guest bytes lie in one page at both
/// RIPs (so its page crossing, and the bytes fexc was given before the page's end, are alike): the same relocation, by the
/// same records. The reuse check (`Compiler::check_reuse`) compares its result with fexc's own answer at the new RIP.
///
/// A guest-RIP-addressed access's checks are fexc's decision at compile time, from its address (fexc_sites.inc, the
/// RIP-relative path of the split and alignment checks): an SSE/AVX operand gets `b` to its alignment stub when misaligned
/// and no check when aligned, a store `b` to its split stub when it crosses a page and no check when not. Moving the code
/// moves those addresses by `to - from`, so every access whose base register a guest-RIP move sets (each such move's value
/// with the access's offset, [`rip_addressed_alike`]) must be aligned alike, for every alignment a check may test, and
/// cross a page alike at both RIPs; else `None`.
pub(crate) fn object_moved(o: &[u8], from: u64, to: u64) -> Option<Vec<u8>> {
    let h: Header = read(o, 0)?;
    let in_page = |r: u64| (r & 0xfff) + u64::from(h.guest_bytes) <= 4096;
    if (from ^ to) >> 63 != 0 || h.guest_bitness != 64 || !in_page(from) || !in_page(to) {
        return None;
    }
    if !rip_addressed_alike(o, &h, to.wrapping_sub(from))? {
        return None;
    }
    moved(o, from, to)
}

/// [`object_moved`]'s rule for the accesses whose base register a guest-RIP move sets, the code moved by `d`: `Some(true)`
/// when every such access is aligned alike at both addresses to each alignment a check may test (SSE/AVX's 16 and 32 bytes,
/// and with alignment checking the operand's width: the access's, or half of a pair's) and crosses a page alike.
/// An access is paired with the last guest-RIP move into its base register before it (fexc materialises a RIP-relative
/// operand's address right before its access, and reuses the register for the next; a move into x0 is a literal exit's,
/// never a base); an access with an index register has its checks at run time (fexc decides at compile time only without
/// one).
fn rip_addressed_alike(o: &[u8], h: &Header, d: u64) -> Option<bool> {
    let mut moves = vec![];
    for i in 0..h.relocs_count as usize {
        let r: Reloc = read(o, (h.relocs_offset as usize).checked_add(i * size_of::<Reloc>())?)?;
        if r.kind == object::RELOC_GUEST_RIP_MOVE && r.register != 0 {
            moves.push((r.offset, r.register, r.value));
        }
    }
    if moves.is_empty() {
        return Some(true);
    }
    let crosses = |a: u64, w: u64| w > 1 && (a ^ a.wrapping_add(w - 1)) & 0x1000 != 0;
    for i in 0..h.sites_count as usize {
        let s: object::Site = read(o, (h.sites_offset as usize).checked_add(i * size_of::<object::Site>())?)?;
        if object::kind::is_stub_kind(s.kind) || s.kind == object::kind::X87MAP || s.index_reg != object::NO_REG {
            continue;
        }
        let w = u64::from(s.width);
        let pair = matches!(s.kind, object::kind::LOAD_PAIR | object::kind::STORE_PAIR | object::kind::PUSH_PAIR | object::kind::POP_PAIR);
        let masks = [16, 32, w, if pair { w / 2 } else { w }].map(|n| n.max(1) - 1);
        let last = moves
            .iter()
            .filter(|&&(at, reg, _)| reg == s.base_reg && at < s.native_offset)
            .max_by_key(|&&(at, ..)| at);
        if let Some(&(_, _, v)) = last {
            let (a, b) = (v.wrapping_add(s.imm as i64 as u64), v.wrapping_add(s.imm as i64 as u64).wrapping_add(d));
            if masks.iter().any(|&m| (a & m == 0) != (b & m == 0)) || crosses(a, w) != crosses(b, w) {
                return Some(false);
            }
        }
    }
    Some(true)
}

fn moved(o: &[u8], from: u64, to: u64) -> Option<Vec<u8>> {
    let h: Header = read(o, 0)?;
    let d = to.wrapping_sub(from);
    if h.guest_rip != from
        || h.end_reason == object::end_reason::UNSUPPORTED_FORM
    {
        return None;
    }
    if !canonical(to) || !canonical(to.wrapping_add(h.guest_bytes.into())) {
        return None;
    }
    let bits32 = match h.guest_bitness {
        64 => false,
        32 if in_32_bits(from, h.guest_bytes) && in_32_bits(to, h.guest_bytes) => true,
        _ => return None,
    };
    let (code, size) = (h.code_offset as usize, h.code_size as usize);
    let mut out = o.to_vec();
    out[24..32].copy_from_slice(&to.to_le_bytes());
    for i in 0..h.relocs_count as usize {
        let at = (h.relocs_offset as usize).checked_add(i * size_of::<Reloc>())?;
        let r: Reloc = read(o, at)?;
        match r.kind {
            object::RELOC_GUEST_RIP_LITERAL | object::RELOC_GUEST_RIP_MOVE => {}
            object::RELOC_NAMED_SYMBOL_LITERAL | object::RELOC_DISPATCH_BRANCH | object::RELOC_PROBE_BRANCH => continue,
            _ => return None,
        }
        let value = r.value.wrapping_add(d);
        if !canonical(value) || (bits32 && (r.value | value) >> 32 != 0) {
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
            let (new, n, old) = relocate_move_mode(&words, u32::from(r.register), d, bits32)?;
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

/// Option (d): `o`, an answer's object, as admission gives it back: its guest-RIP literal slots filled with their values
/// (the only bytes admission changes). `None` when a slot lies outside the code (admission refused such an object).
pub(crate) fn resolved(o: &[u8]) -> Option<Vec<u8>> {
    let h: Header = read(o, 0)?;
    let mut out = o.to_vec();
    for i in 0..h.relocs_count as usize {
        let r: Reloc = read(o, (h.relocs_offset as usize).checked_add(i * size_of::<Reloc>())?)?;
        if r.kind == object::RELOC_GUEST_RIP_LITERAL {
            let at = (h.code_offset as usize).checked_add(r.offset as usize)?;
            out.get_mut(at..at.checked_add(8)?)?.copy_from_slice(&r.value.to_le_bytes());
        }
    }
    Some(out)
}

/// Option (d): the block admission gave the answer `from_object` at `from` (`form`: its record without the object), as it
/// gives `to_object` (that answer moved to `to` by [`object`]; itself when `to == from`) without admitting it. At another
/// RIP only where every relocation packs (at most `MAX_RELOCS`), the resident's re-check, [`relocate_code`], accepts, and
/// its code is the moved answer's. `None`: admit it. The caller pairs the objects with the entry `form` was kept for: the
/// answer at `from` and the same answer at `to`.
pub(crate) fn kept(
    form: &Admitted,
    from_object: &[u8],
    from: u64,
    to_object: &[u8],
    to: u64,
) -> Option<Admitted> {
    let rest = Admitted { object: resolved(to_object)?, ..form.clone() };
    if from == to {
        return Some(rest);
    }
    let h: Header = read(from_object, 0)?;
    let mut relocs = vec![];
    for i in 0..h.relocs_count as usize {
        let r: Reloc = read(from_object, (h.relocs_offset as usize).checked_add(i * size_of::<Reloc>())?)?;
        relocs.extend(pack_reloc(&r, h.code_size)?);
    }
    relocs.sort();
    if relocs.len() > MAX_RELOCS as usize || form.links.len() > MAX_LINK_POINTS {
        return None;
    }
    let mut code = resolved(from_object)?
        .get(h.code_offset as usize..)?
        .get(..h.code_size as usize)?
        .to_vec();
    let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
    links[..form.links.len()].copy_from_slice(&form.links);
    relocate_code_mode(&mut code, &relocs, &mut links, from, to, h.guest_bytes, h.guest_bitness == 32).ok()?;
    // Safety does not rest on two relocators agreeing: the code is the moved object's.
    if rest.object.get(h.code_offset as usize..)?.get(..h.code_size as usize)? != code {
        return None;
    }
    Some(Admitted {
        links: links[..form.links.len()].to_vec(),
        rep_self: form.rep_self.map(|r| r.wrapping_add(to.wrapping_sub(from))),
        ..rest
    })
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

pub(crate) fn read<T: Copy>(b: &[u8], at: usize) -> Option<T> {
    let bytes = b.get(at..at.checked_add(size_of::<T>())?)?;
    // SAFETY: in bounds; the records are plain little-endian integers (`read_unaligned`).
    Some(unsafe { std::ptr::read_unaligned(bytes.as_ptr().cast()) })
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::execution::{
        LinkPoint, MAX_LINK_POINTS, RELOC_MOVE, bitmask, load_constant, logical_immediate,
        move_value, pack_reloc, refusal, relocate_code, relocate_move,
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

    /// Moved to another page offset only within the half and with the bytes in one page at both RIPs; at the same
    /// page offset it is [`object`]'s relocation.
    #[test]
    fn moved_relocation_refusals() {
        let h: Header = read(USER_A.0, 0).unwrap();
        assert_eq!(h.guest_bytes, 26);
        assert_eq!(object_moved(USER_A.0, USER_A.1, 0xffff_ffff_8123_4340), None);
        // 26 bytes from page offset 0xff0 cross the page; from 0xfe6 they end at its end.
        assert_eq!(object_moved(USER_A.0, USER_A.1, 0x7f12_3456_8ff0), None);
        assert!(object_moved(USER_A.0, USER_A.1, 0x7f12_3456_8fe6).is_some());
        assert_eq!(object_moved(USER_A.0, USER_A.1, USER_B.1), object(USER_A.0, USER_A.1, USER_B.1));
        assert_eq!(object_moved(USER_A.0, USER_B.1, 0x7f12_3456_8340), None);
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

    /// With `FEXC` set: a block below 4 GiB, where fexc's guest-RIP moves (the link point's target) are the 32-bit form, is
    /// accepted by the re-check at the SAME RIP (nothing moves: the template pool serves such entries as they were admitted) and
    /// refused at another (relocation of that form is refused, `refusal::MOVE`); a block above 4 GiB is accepted at both, and
    /// the same-RIP result is the admitted code and link points exactly.
    #[test]
    fn the_same_rip_needs_no_move_re_encoding() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        // inc rax; jmp $+0x10 (one literal exit: a link point whose target is a RIP move)
        let source = [0x48, 0xff, 0xc0, 0xeb, 0x10];
        let compile = |rip: u64| {
            let dir = std::env::temp_dir().join(format!("natemu-same-{}-{rip:x}", std::process::id()));
            let hex: String = source.iter().map(|b| format!("{b:02x}")).collect();
            let st = std::process::Command::new(&fexc)
                .args(["--hex", &hex, "--rip", &format!("{rip:#x}"), "--profile", "slice1", "--object"])
                .arg(&dir)
                .stdout(std::process::Stdio::null())
                .status()
                .unwrap();
            assert!(st.success());
            let o = std::fs::read(&dir).unwrap();
            std::fs::remove_file(&dir).unwrap();
            o
        };
        for (low, a, b) in [(true, 0x40_1000u64, 0x50_2000u64), (false, 0x7f12_3440_1000, 0x7f12_3450_2000)] {
            let o = compile(a);
            let (code0, relocs, links0, bytes) = placed_at(&o, a, &source, false);
            assert!(relocs.iter().any(|r| r & RELOC_MOVE != 0) && links0[0].offset != 0);
            // The same RIP: accepted, code and link points as admitted (the placeholder `placed_at` put in the link point
            // is restored).
            let (mut code, mut links) = (code0.clone(), links0);
            assert_eq!(relocate_code(&mut code, &relocs, &mut links, a, a, bytes), Ok(()), "{a:#x} to itself");
            let admitted = crate::admission::admit(&o, a, &source, u64::MAX, true, false).unwrap();
            let h: Header = read(&admitted.object, 0).unwrap();
            assert_eq!(code, admitted.object[h.code_offset as usize..][..h.code_size as usize]);
            assert_eq!(links[0], admitted.links[0]);
            // A link point that is no exit of the code is still refused at distance 0.
            let mut bad = links0;
            bad[0].target ^= 1 << 20;
            let mut code = code0.clone();
            assert_eq!(relocate_code(&mut code, &relocs, &mut bad, a, a, bytes), Err(refusal::LINK));
            // Another RIP: the 32-bit form below 4 GiB is refused (a word count or form fexc may not choose there), the
            // 64-bit form relocated.
            let mut code = code0.clone();
            let mut links = links0;
            let moved = relocate_code(&mut code, &relocs, &mut links, a, b, bytes);
            if low {
                assert_eq!(moved, Err(refusal::MOVE));
            } else {
                assert_eq!(moved, Ok(()));
            }
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
            let (linked, relocs, mut links, _) = placed(from.0, from.1, USER);
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
            assert_eq!(pic::eligible(&code, &relocs, &links, from.1, false), Ok(()));
            assert_eq!(pic::rewrite(&mut code, &relocs, &mut links, from.1, false), Ok(()));
            assert!(pic::proved(
                w(&original),
                w(&code),
                code.len(),
                &relocs,
                &links,
                from.1,
                0,
                false
            ));
            assert!(
                links
                    .iter()
                    .any(|p| p.offset != 0 && p.word == pic::load_base(0))
            );
            // Every move rewritten: no word of an absolute guest-RIP move of x0 or another register is left.
            assert_ne!(code, original);
            // The same code at the other RIP (an instance's: link targets moved there) is the object admitted there
            // rewritten; not with the template's targets, and not against the template's object.
            let there = admitted(to.0, to.1);
            fn w(o: &[u8]) -> impl Fn(usize) -> u32 + '_ {
                move |at| u32::from_le_bytes(o[at..at + 4].try_into().unwrap())
            }
            let mut moved = links;
            for p in moved.iter_mut().filter(|p| p.offset != 0) {
                p.target = p.target.wrapping_add(to.1.wrapping_sub(from.1));
            }
            assert!(pic::proved(
                w(&there),
                w(&code),
                code.len(),
                &relocs,
                &moved,
                to.1,
                0,
                false
            ));
            assert!(
                !pic::proved(w(&there), w(&code), code.len(), &relocs, &links, to.1, 0, false),
                "the template's link targets"
            );
            assert!(!pic::proved(
                w(&original),
                w(&code),
                code.len(),
                &relocs,
                &moved,
                to.1,
                0,
                false
            ));
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

    /// M2 reuse, the move rule for 32-bit code: fexc's one form alone (`movz w; movk w, lsl #16; nop`), re-encoded in its
    /// three words for every value below 4 GiB (zero halves too, which other forms once took); refused when the value wraps
    /// at 4 GiB either way, without the `nop`, in another form (a one-word or 64-bit move), or for another register.
    #[test]
    fn the_move_rule_of_32_bit_code() {
        use natemu_abi::execution::{NOP, guest_rip_move32, in_32_bits, relocate_code_mode, relocate_move_mode as mv};
        let form = |c: u64, reg: u32| {
            let [z, k] = guest_rip_move32(c, reg);
            [z, k, NOP, 0]
        };
        for (old, new) in [
            (0x5655_5346u64, 0xf7c5_6346u64),
            (0xf7c5_6346, 0x5655_5346),
            (0xffff_1346, 0xffff_8346), // once a one-word movn
            (0x00ff_f000, 0x0ff0_0000), // once bitmasks
            (0x5655_5346, 0x5655_0000), // a zero low half
            (0x5655_5346, 0x0000_2346), // a zero high half
            (0x5655_5346, 0),
        ] {
            let w = form(old, 0);
            assert_eq!(mv(&w[..3], 0, new.wrapping_sub(old), true), Some((form(new, 0), 3, old)), "{old:#x} to {new:#x}");
            // 64-bit code refuses the form, as before.
            assert_eq!(mv(&w[..3], 0, new.wrapping_sub(old), false), None);
            assert_eq!(relocate_move(&w[..3], 0, new.wrapping_sub(old)), None);
        }
        let w = form(0x5655_5346, 7);
        // Past 4 GiB and below 0 (FEX wraps; not relocated, though the rule would be exact: rare, fexc's to compile).
        for to in [0x1_0000_0346u64, 0xffff_ffff_ffff_f346] {
            assert_eq!(mv(&w[..3], 7, to.wrapping_sub(0x5655_5346), true), None, "to {to:#x}");
        }
        // Not the form: no `nop` (an unpadded move, or the end of the code), FEX's other forms, another register.
        let (low, n) = load_constant(0x5655_5346, 7);
        let (one, _) = load_constant(0xffff_1346, 7);
        let (wide, m) = load_constant(0x1_5655_5346, 7);
        for bad in [&w[..2], &[w[0], w[1], 0xd503_201e][..], &low[..n], &[low[0], low[1], 0], &[one[0], NOP, 0], &wide[..m]] {
            assert_eq!(mv(bad, 7, 0x1000, true), None, "{bad:x?}");
        }
        assert_eq!(mv(&w[..3], 6, 0x1000, true), None);
        assert_eq!(mv(&form(0x5655_5346, 31)[..3], 31, 0x1000, true), None);
        // A link point of the form (`svc #LINK` after its `nop`) moved by the copy rule; the old two-word form refused.
        let svc = natemu_abi::pic::SVC_LINK;
        let block = |w: &[u32]| -> Vec<u8> { w.iter().flat_map(|w| w.to_le_bytes()).collect() };
        let at = |rip: u64| {
            let f = form(rip + 0x1f00, 0);
            block(&[0, f[0], f[1], NOP, svc])
        };
        let point = |rip: u64, w: &[u8]| {
            let mut l = [LinkPoint::NONE; MAX_LINK_POINTS];
            l[0] = LinkPoint { offset: 4, word: u32::from_le_bytes(w[4..8].try_into().unwrap()), target: rip + 0x1f00 };
            l
        };
        let (from, to) = (0x5655_5100u64, 0xf7c5_6100u64);
        let mut code = at(from);
        let mut l = point(from, &code);
        assert_eq!(relocate_code_mode(&mut code, &[1 << 6 | RELOC_MOVE], &mut l, from, to, 8, true), Ok(()));
        assert_eq!((code, l), (at(to), point(to, &at(to))));
        let (old, n) = load_constant(from + 0x1f00, 0);
        let mut code = block(&[0, old[0], old[1], svc]);
        assert_eq!(n, 2);
        let mut l = point(from, &code);
        assert_eq!(relocate_code_mode(&mut code, &[1 << 6 | RELOC_MOVE], &mut l, from, to, 8, true), Err(refusal::MOVE));
        // The reply check's rule (abi `link_point`) by mode: 32-bit code's link point is the one form, `svc` after its `nop`;
        // 64-bit code's is the move then `svc` (a w-form move too, for a value below 4 GiB), never with a `nop` between.
        let lp = |w: &[u8], bits32: bool| {
            let p = point(from, w)[0];
            natemu_abi::execution::link_point(|o| u32::from_le_bytes(w[o as usize..o as usize + 4].try_into().unwrap()), w.len() as u32, &p, bits32)
        };
        let (padded, unpadded) = (at(from), block(&[0, old[0], old[1], svc]));
        assert_eq!([lp(&padded, true), lp(&unpadded, true)], [true, false]);
        assert_eq!([lp(&padded, false), lp(&unpadded, false)], [false, true]);
        // The block's place: both ends below 4 GiB at both RIPs.
        assert!(in_32_bits(0xffff_f000, 0xfff) && !in_32_bits(0xffff_f000, 0x1000) && !in_32_bits(1 << 32, 0));
        let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
        for (from, to, bytes) in [(0xffff_f000u64, 0x5655_5000u64, 0x1000), (0x5655_5000, 0xffff_f000, 0x1000), (0x5655_5000, 0x1_5655_5000, 4)] {
            assert_eq!(relocate_code_mode(&mut [], &[], &mut links, from, to, bytes, true), Err(refusal::PLACE));
        }
        assert_eq!(relocate_code_mode(&mut [], &[], &mut links, 0x5655_5000, 0xffff_f000, 0xfff, true), Ok(()));
        // A literal slot's value, old and new.
        for (value, to, ok) in [(0x5655_5346u64, 0xf7c5_6000u64, true), (0xffff_f346, 0x5655_6000, false), (0x1_0000_0346, 0x5655_4000, false)] {
            let mut code = value.to_le_bytes();
            let moved = relocate_code_mode(&mut code, &[0], &mut links, 0x5655_5000, to, 4, true);
            assert_eq!(moved.is_ok(), ok, "{value:#x}: {moved:?}");
        }
    }

    /// M2 reuse inside the resident: 32-bit code's user templates are shared as position-independent translations (but
    /// under `PIC_USER64`, the switch that copies them as before), as 64-bit code's are; a block on the last page below 4
    /// GiB that runs to 4 GiB is refused by the rule both copy routines call; and a copy of a block with the x87 run test
    /// restores its run end's return.
    #[test]
    fn templates_of_32_bit_code_are_shared_or_copied() {
        use natemu_abi::execution::cache::{PIC_ALL, PIC_OFF, PIC_USER, PIC_USER64, shares};
        use natemu_abi::execution::{CodeMode, ENTRY_OFFSET, RUN_END, context_bits32, frame, relocate_code_mode, run_return_word, run_test_end};
        // The code context's mode field (bits 4:2) and CPL (bits 1:0).
        let context = |mode: CodeMode, cpl: u64| (mode as u64) << 2 | cpl | 1 << 11;
        let (user64, kernel64, user32) = (context(CodeMode::Bits64, 3), context(CodeMode::Bits64, 0), context(CodeMode::Compat32, 3));
        assert!(!context_bits32(user64) && !context_bits32(kernel64) && context_bits32(user32));
        assert!(!context_bits32(context(CodeMode::Compat16, 3)));
        let modes = [PIC_OFF, PIC_USER, PIC_ALL, PIC_USER64];
        assert_eq!(modes.map(|p| shares(p, user64)), [false, true, true, true]);
        assert_eq!(modes.map(|p| shares(p, kernel64)), [false, false, true, false]);
        assert_eq!(modes.map(|p| shares(p, user32)), [false, true, true, false]);
        assert_eq!(modes.map(|p| shares(p, context(CodeMode::Compat32, 0))), [false, false, true, false]);
        // A block with no relocation (inc eax; ret) on the last page: moved below it, refused where it would end at 4 GiB
        // or come from there, in 32-bit code only.
        let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
        let mut code = [0u8; 16];
        assert_eq!(relocate_code_mode(&mut code, &[], &mut links, 0x5655_5ffe, 0xffff_fffe, 2, true), Err(refusal::PLACE));
        assert_eq!(relocate_code_mode(&mut code, &[], &mut links, 0xffff_fffe, 0x5655_5ffe, 2, true), Err(refusal::PLACE));
        assert_eq!(relocate_code_mode(&mut code, &[], &mut links, 0x5655_5ffd, 0xffff_fffd, 2, true), Ok(()));
        assert_eq!(relocate_code_mode(&mut code, &[], &mut links, 0x5655_5ffe, 0xffff_fffe, 2, false), Ok(()));
        // The run test: `ldrb w0, [x28, #X87_RUN]; cbnz w0, E`, a body, then at E the run's end and its return to the third
        // word, which is the word a copy restores.
        let entry = ENTRY_OFFSET as usize / 4;
        let e = entry + 6;
        let mut words = vec![0xd503_201fu32; e + 10];
        words[entry] = 0x3940_0380 | frame::X87_RUN << 10;
        words[entry + 1] = 0x3500_0000 | ((e - entry - 1) as u32) << 5;
        words[e..e + 9].copy_from_slice(&RUN_END);
        words[e + 9] = run_return_word(4 * e as u64);
        let word = |o: u64| words[o as usize / 4];
        assert_eq!(run_test_end(word, 4 * words.len() as u64), Some(4 * e as u64));
        // (`b` back to the third word: 9 + 6 - 2 words before it.)
        assert_eq!(words[e + 9], 0x1400_0000 | (0u32.wrapping_sub(13) & 0x03ff_ffff));
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
    const REPSRC: &[u8] = fixture!("rep.src");
    /// `xor eax, eax; rep movsb`: the block ends at a REP string, so its `rep_self` (the RIP of the instruction) moves.
    const REP_A: (&[u8], u64) = (fixture!("rep-7f1234567100.obj"), 0x7f12_3456_7100);
    const REP_B: (&[u8], u64) = (fixture!("rep-7f1234568100.obj"), 0x7f12_3456_8100);

    /// Option (d): `rep_self` moves with the RIP, as admission gives it there.
    #[test]
    fn kept_form_moves_rep_self() {
        let admit = |o: &[u8], rip| crate::admission::admit(o, rip, REPSRC, u64::MAX, true, false).unwrap();
        for (from, to) in [(REP_A, REP_B), (REP_B, REP_A)] {
            let form = Admitted { object: vec![], ..admit(from.0, from.1) };
            assert_eq!(form.rep_self, Some(from.1 + 2));
            let moved = object(from.0, from.1, to.1).unwrap();
            assert_eq!(kept(&form, from.0, from.1, &moved, to.1), Some(admit(to.0, to.1)));
        }
    }

    /// Option (d): the kept form moved to the other RIP is admission's block there (both ways), and every refusal of the
    /// move leaves `None` (admit it).
    #[test]
    fn kept_form_moves_as_admission_does() {
        let admit = |o: &[u8], rip| crate::admission::admit(o, rip, USER, u64::MAX, true, false).unwrap();
        for (from, to) in [(USER_A, USER_B), (USER_B, USER_A)] {
            let form = Admitted { object: vec![], ..admit(from.0, from.1) };
            let moved = object(from.0, from.1, to.1).unwrap();
            assert_eq!(kept(&form, from.0, from.1, &moved, to.1), Some(admit(to.0, to.1)));
            assert_eq!(kept(&form, from.0, from.1, from.0, from.1), Some(admit(from.0, from.1)));
        }
        let form = Admitted { object: vec![], ..admit(USER_A.0, USER_A.1) };
        // Another page offset: relocate_code's place check.
        assert_eq!(kept(&form, USER_A.0, USER_A.1, USER_B.0, USER_B.1 + 8), None);
        // The moved answer's code is not what the re-check gives: admitted (two relocators must agree).
        let mut tampered = USER_B.0.to_vec();
        let h: Header = read(&tampered, 0).unwrap();
        tampered[h.code_offset as usize + 8] ^= 1;
        assert_eq!(kept(&form, USER_A.0, USER_A.1, &tampered, USER_B.1), None);
        // A relocation that does not pack (a kind no RIP is in, or a literal outside the code).
        let mut odd = USER_A.0.to_vec();
        let h: Header = read(&odd, 0).unwrap();
        odd[h.relocs_offset as usize + 4] = object::RELOC_OTHER;
        assert_eq!(kept(&form, &odd, USER_A.1, USER_B.0, USER_B.1), None);
    }
}
