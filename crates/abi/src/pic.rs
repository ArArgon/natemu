//! Position-independent translations (docs/restructure/pic-plan.md), the rewrite the resident makes of a reuse template
//! (perf chunk 10's relocations) and the rules it is checked by. A guest-RIP move of n words building v becomes, in exactly
//! n words, "load the page base of the block from the guest-state page ([`crate::execution::frame::PAGE_BASE`]), add
//! k = v - base": `ldr xR, [x28, #PAGE_BASE]` and 1-3 `add`/`sub xR, xR, #imm` (`nop` filling a fourth word). Two words reach
//! base ± 4 KiB or a page multiple below 16 MiB, three or four words ± 16 MiB, one word nothing. The block's entry records
//! the page base ([`AND_PAGE`], [`STORE_BASE`]); every other word stays FEX's (its literal slots too: the tail's RIP, which
//! no generated code reads). [`proved`] is the static check of one rewritten block against the object it came from.
use crate::execution::{LinkPoint, MAX_LINK_POINTS, RELOC_MOVE, canonical, frame, move_value, svc};

/// `ldr xR, [x28, #PAGE_BASE]`.
pub const fn load_base(reg: u32) -> u32 {
    0xf940_0000 | (frame::PAGE_BASE / 8) << 10 | 28 << 5 | reg
}
/// The full entry's two words (an L1 entry and an `svc`-word link arrive with x0 = the block's RIP): `and x0, x0,
/// #!0xfff` (N = 1, immr = 52, imms = 51: 52 ones rotated to bits 63:12) and `str x0, [x28, #PAGE_BASE]`.
pub const AND_PAGE: u32 = 0x9274_cc00;
pub const STORE_BASE: u32 = 0xf900_0000 | (frame::PAGE_BASE / 8) << 10 | 28 << 5;
const NOP: u32 = 0xd503_201f;
/// `svc #LINK`, the second word of a link point's exit after its move.
pub const SVC_LINK: u32 = 0xd400_0001 | (svc::LINK as u32) << 5;
const ADD: u32 = 0x9100_0000;
const SUB: u32 = 0xd100_0000;
/// The reach of three words (`add #hi, lsl #12; add #lo`): |k| below 16 MiB.
pub const REACH: u64 = 1 << 24;

/// Why a template is not rewritten (the index of `Control::pic_counts`, [`count`]).
pub mod refusal {
    /// A one-word move (`movn`, `orr` or `movz` alone).
    pub const ONE_WORD: usize = super::count::ONE_WORD;
    /// A move whose value is out of its word count's reach.
    pub const REACH: usize = super::count::REACH;
    /// A move that is not one (not a move, outside the code, out of order or overlapping, a reserved register) or a link
    /// point no rewritten move of x0 starts.
    pub const MOVE: usize = super::count::MOVE;
    /// The block's page is within [`super::REACH`] of a canonical edge (a copy only: its values might not be canonical).
    pub const PLACE: usize = super::count::PLACE;
}

/// Counters (`Control::pic_counts`; PIC study stage 1, kept by the stages after it). User (CPL 3) blocks unless named.
pub mod count {
    /// User blocks placed from the host's answers; of them, not templates (the host's relocation rule refused them).
    pub const HOST: usize = 0;
    pub const NOT_TEMPLATE: usize = 1;
    /// User templates rewritten (stage 1: that would be); refused for a global mapping, a one-word move, a move out of
    /// reach, a malformed move, a page near a canonical edge.
    pub const ELIGIBLE: usize = 2;
    pub const GLOBAL: usize = 3;
    pub const ONE_WORD: usize = 4;
    pub const REACH: usize = 5;
    pub const MOVE: usize = 6;
    pub const PLACE: usize = 7;
    /// User copies made in the resident; of them, of a rewritten template (stage 1: one that would be).
    pub const COPIES: usize = 8;
    pub const COPIES_COVERED: usize = 9;
    /// Links made, by class ([`super::Class`]): five counters from `LINKS`.
    pub const LINKS: usize = 10;
    /// A frame met at two linear pages in one TLB epoch (one address space) as a user block's first source page.
    pub const ALIASES: usize = 15;
    /// (Requests a translation seen earlier would have served are the host's: its hits of this vCPU's own answers and its
    /// compiles of an answer it had dropped, `recompiled`.)
    /// Kernel (CPL 0) templates and copies, for scale.
    pub const KERNEL_TEMPLATES: usize = 16;
    pub const KERNEL_COPIES: usize = 17;
    /// Stage 2: the slot disagreed with the block's own page base where the resident could see both (a stop at an entry
    /// word, a linked exit's x0); never acted on.
    pub const SLOT_MISMATCH: usize = 18;
    /// Stage 2: templates whose rewrite failed its static proof ([`super::proved`]; a resident fault: left absolute, and
    /// the check halts); copies the check could not compare with the host's answer at their RIP (another code size or
    /// relocation list there: fexc chose other move lengths, which position-independent code does not depend on).
    pub const UNPROVED: usize = 19;
    pub const CHECK_SHAPE: usize = 20;
    pub const COUNTS: usize = 32;
}

/// The class of a link (source, target): both position-independent and the target starting on the source's start
/// page, both and another page, a position-independent source into an absolute target (global ones included), an
/// absolute source into a position-independent target, both absolute.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Class {
    SamePage = 0,
    CrossPage = 1,
    SharedToAbsolute = 2,
    AbsoluteToShared = 3,
    Absolute = 4,
}
pub fn class(source_pic: bool, source_rip: u64, target_pic: bool, target_rip: u64) -> Class {
    match (source_pic, target_pic) {
        (true, true) if source_rip >> 12 == target_rip >> 12 => Class::SamePage,
        (true, true) => Class::CrossPage,
        (true, false) => Class::SharedToAbsolute,
        (false, true) => Class::AbsoluteToShared,
        (false, false) => Class::Absolute,
    }
}

/// The `n`-word form of a move building `base + k` in `reg` (module comment); `None` when `n` words cannot reach `k`.
pub fn form(n: usize, reg: u32, k: i64) -> Option<[u32; 4]> {
    let (op, m) = (if k < 0 { SUB } else { ADD }, k.unsigned_abs());
    let add =
        |shift: u64, imm: u64| op | (shift as u32) << 22 | (imm as u32) << 10 | reg << 5 | reg;
    let ldr = load_base(reg);
    match n {
        2 if m < 4096 => Some([ldr, add(0, m), 0, 0]),
        2 if m % 4096 == 0 && m < REACH => Some([ldr, add(1, m >> 12), 0, 0]),
        3 | 4 if m < REACH => Some([
            ldr,
            add(1, m >> 12),
            add(0, m & 0xfff),
            if n == 4 { NOP } else { 0 },
        ]),
        _ => None,
    }
}
/// The k a form of exactly `words.len()` words builds in `reg` (the decode [`proved`] checks with, written apart from
/// [`form`]): `ldr reg, [x28, #PAGE_BASE]`, then 64-bit `add`/`sub reg, reg, #imm{, lsl #12}` or `nop`.
pub fn value(words: &[u32], reg: u32) -> Option<i64> {
    let (&first, rest) = words.split_first()?;
    if first != load_base(reg) || rest.len() > 3 {
        return None;
    }
    rest.iter().try_fold(0i64, |k, &w| {
        if w == NOP {
            return Some(k);
        }
        let imm = i64::from(w >> 10 & 0xfff) << (12 * (w >> 22 & 1));
        match w & 0xff80_0000 {
            _ if w & 31 != reg || w >> 5 & 31 != reg => None,
            0x9100_0000 => Some(k + imm),
            0xd100_0000 => Some(k - imm),
            _ => None,
        }
    })
}
/// Whether [`form`]s of every reach stay canonical at page `base`: base ± [`REACH`] (and its next page, for a block's end)
/// lie in one canonical half.
pub fn reaches(base: u64) -> bool {
    let (lo, hi) = (base.wrapping_sub(REACH), base.wrapping_add(REACH + 8192));
    canonical(lo) && canonical(hi) && lo < base && base < hi && lo >> 63 == hi >> 63
}

fn word(code: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(code[at..at + 4].try_into().unwrap())
}
/// The move of `reg` at `at` in `code` (as admitted, at page `base`): its word count and its [`form`]. `Err`: the
/// [`refusal`].
fn one(code: &[u8], at: usize, reg: u32, base: u64) -> Result<(usize, [u32; 4]), usize> {
    if at + 4 > code.len() || reg == 28 {
        return Err(refusal::MOVE);
    }
    let mut words = [0; 5];
    let n = ((code.len() - at) / 4).min(5);
    for (k, w) in words[..n].iter_mut().enumerate() {
        *w = word(code, at + 4 * k);
    }
    let Some((n, v)) = move_value(&words[..n], reg) else {
        return Err(refusal::MOVE);
    };
    if n == 1 {
        return Err(refusal::ONE_WORD);
    }
    form(n, reg, v.wrapping_sub(base) as i64)
        .map(|f| (n, f))
        .ok_or(refusal::REACH)
}
/// Whether the template `code` (admitted for `rip`; `relocs` packed, [`RELOC_MOVE`]) has a rewrite: each relocation in
/// order, none starting inside the one before (as perf chunk 10's re-check), each move with a [`form`]. `Err`: the
/// [`refusal`]; nothing is written.
pub fn eligible(code: &[u8], relocs: &[u32], rip: u64) -> Result<(), usize> {
    let mut free = 0;
    for &r in relocs {
        let at = (r >> 6) as usize * 4;
        if at < free {
            return Err(refusal::MOVE);
        }
        let n = if r & RELOC_MOVE == 0 {
            if at + 8 > code.len() {
                return Err(refusal::MOVE);
            }
            2
        } else {
            one(code, at, r & 31, rip & !0xfff)?.0
        };
        free = at + 4 * n;
    }
    Ok(())
}
/// Rewrites the template `code` (as admitted for `rip`, copied where it is placed; `relocs` packed and in order) into its
/// position-independent form: each move into its [`form`], the code header word into [`STORE_BASE`] (the caller puts
/// [`AND_PAGE`] in the word before), each link point's word into its move's new first word. `Err`: the [`refusal`]
/// (nothing written).
pub fn rewrite(
    code: &mut [u8],
    relocs: &[u32],
    links: &mut [LinkPoint; MAX_LINK_POINTS],
    rip: u64,
) -> Result<(), usize> {
    eligible(code, relocs, rip)?;
    let at_move = |o: u32| {
        relocs
            .iter()
            .any(|&r| r & RELOC_MOVE != 0 && r & 31 == 0 && (r >> 6) * 4 == o)
    };
    if links.iter().any(|p| p.offset != 0 && !at_move(p.offset)) {
        return Err(refusal::MOVE);
    }
    for &r in relocs.iter().filter(|&&r| r & RELOC_MOVE != 0) {
        let at = (r >> 6) as usize * 4;
        let (n, f) = one(code, at, r & 31, rip & !0xfff)?;
        for (j, w) in f[..n].iter().enumerate() {
            code[at + 4 * j..at + 4 * j + 4].copy_from_slice(&w.to_le_bytes());
        }
    }
    code[..4].copy_from_slice(&STORE_BASE.to_le_bytes());
    for p in links.iter_mut().filter(|p| p.offset != 0) {
        p.word = load_base(0);
    }
    Ok(())
}
/// The offset of the word after link point `p`'s move (its `svc #LINK`, or the `b` an `svc`-word link put there): past the
/// point's first word (whatever it holds now: linked, armed), the rest of the move, the `add`/`sub x0`/`nop` words of a
/// position-independent form or the `movk x0` words of an absolute one.
pub fn svc_slot(word: impl Fn(u64) -> u32, p: &LinkPoint) -> u64 {
    let at = u64::from(p.offset);
    let plain = |w: u32| {
        w == NOP
            || (matches!(w & 0xff80_0000, ADD | SUB) && w & 0x3ff == 0)
            || w & 0x7f80_001f == 0x7280_0000
    };
    (1..4)
        .find(|&k| !plain(word(at + 4 * k)))
        .map_or(at + 16, |k| at + 4 * k)
}
/// `p` names an exit of position-independent `code` (`code_size` bytes, at page `base`) as [`rewrite`] leaves it: its word
/// is in the code and is `p.word` = `ldr x0, [x28, #PAGE_BASE]`, the form after it builds `p.target` from `base`, and
/// `svc #LINK` follows.
pub fn link_point(word: impl Fn(u64) -> u32, code_size: u32, p: &LinkPoint, base: u64) -> bool {
    let at = u64::from(p.offset);
    if at % 4 != 0 || at < 4 || at + 8 > u64::from(code_size) || p.word != load_base(0) {
        return false;
    }
    let svc = svc_slot(&word, p);
    if svc + 4 > u64::from(code_size) || word(svc) != SVC_LINK {
        return false;
    }
    let mut words = [0; 4];
    let n = ((svc - at) / 4) as usize;
    for (k, w) in words[..n].iter_mut().enumerate() {
        *w = word(at + 4 * k as u64);
    }
    value(&words[..n], 0).is_some_and(|k| base.wrapping_add(k as u64) == p.target)
}
/// The static proof of one rewritten block: `code` (from its header word) is `original` (the admitted object's code for
/// `rip`) but for the header word, now [`STORE_BASE`] (the only store [`rewrite`] adds); each move of `relocs` (in order,
/// none inside the one before; its words decoded from `original`), now `value` k in as many words with base + k its old
/// value; the word at `skip` (a computed exit's `b`, 0 for none) and literal slots (the tail's RIP, which no generated code
/// reads), not compared; and every link point a [`link_point`] of `code`. Every other word is the same.
pub fn proved(
    original: &[u8],
    code: &[u8],
    relocs: &[u32],
    links: &[LinkPoint; MAX_LINK_POINTS],
    rip: u64,
    skip: u32,
) -> bool {
    let base = rip & !0xfff;
    let len = code.len();
    if original.len() != len || len % 4 != 0 || len < 4 || word(code, 0) != STORE_BASE {
        return false;
    }
    // Words [from, to) are the same, but `skip`'s.
    let same = |from: usize, to: usize| {
        (from..to)
            .step_by(4)
            .all(|at| (skip != 0 && at == skip as usize) || word(code, at) == word(original, at))
    };
    let mut free = 4;
    for &r in relocs {
        let at = (r >> 6) as usize * 4;
        if at < free || at + 4 > len || !same(free, at) {
            return false;
        }
        let n = if r & RELOC_MOVE == 0 {
            2
        } else {
            let reg = r & 31;
            let mut words = [0; 5];
            let avail = ((len - at) / 4).min(5);
            for (k, w) in words[..avail].iter_mut().enumerate() {
                *w = word(original, at + 4 * k);
            }
            let Some((n, v)) = move_value(&words[..avail], reg) else {
                return false;
            };
            let mut new = [0; 4];
            for (k, w) in new[..n].iter_mut().enumerate() {
                *w = word(code, at + 4 * k);
            }
            if reg == 28 || value(&new[..n], reg).map(|k| base.wrapping_add(k as u64)) != Some(v) {
                return false;
            }
            n
        };
        free = at + 4 * n;
        if free > len {
            return false;
        }
    }
    same(free, len)
        && links
            .iter()
            .filter(|p| p.offset != 0)
            .all(|p| link_point(|at| word(code, at as usize), len as u32, p, base))
}
/// Perf chunk 10's copy for position-independent code: `code`, a rewritten block placed for `from` (link points `links`,
/// maybe linked), copied for `to` (same half and page offset): its link points restored (first word, `svc` word) and
/// their targets moved by `to - from`. Nothing else changes, so it is refused only where a [`form`]'s value might not be
/// canonical ([`reaches`]) or the copy's place differs.
pub fn relocate(
    code: &mut [u8],
    links: &mut [LinkPoint; MAX_LINK_POINTS],
    from: u64,
    to: u64,
    guest_bytes: u32,
) -> Result<(), usize> {
    if (from ^ to) & (1 << 63 | 0xfff) != 0
        || !reaches(to & !0xfff)
        || !to.checked_add(guest_bytes.into()).is_some_and(canonical)
    {
        return Err(refusal::PLACE);
    }
    let d = to.wrapping_sub(from);
    let size = code.len();
    for p in links.iter_mut().filter(|p| p.offset != 0) {
        let at = p.offset as usize;
        if at % 4 != 0 || at + 4 > size || p.word != load_base(0) {
            return Err(refusal::MOVE);
        }
        code[at..at + 4].copy_from_slice(&p.word.to_le_bytes());
        let svc = svc_slot(|o| word(code, o as usize), p) as usize;
        if svc + 4 > size {
            return Err(refusal::MOVE);
        }
        code[svc..svc + 4].copy_from_slice(&SVC_LINK.to_le_bytes());
        p.target = p.target.wrapping_add(d);
        if !canonical(p.target)
            || !link_point(|o| word(code, o as usize), size as u32, p, to & !0xfff)
        {
            return Err(refusal::MOVE);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    extern crate std;
    use super::*;
    use crate::execution::{bitmask, load_constant};
    use std::vec::Vec;

    #[test]
    fn entry_words_decode() {
        assert_eq!(bitmask(AND_PAGE), Some(!0xfff));
        assert_eq!(AND_PAGE & 0xff80_03ff, 0x9200_0000);
        assert_eq!(frame::PAGE_BASE % 8, 0);
        // str/ldr x0, [x28, #PAGE_BASE]: unsigned-offset forms, the offset scaled by 8.
        assert_eq!(STORE_BASE, 0xf900_0f80);
        assert_eq!(load_base(5), 0xf940_0f85);
    }

    /// Every form decodes to its k in its word count, at each reach's edges; out of reach is none.
    #[test]
    fn forms_reach_and_decode() {
        let edge = REACH as i64;
        for (n, k, some) in [
            (2, 0, true),
            (2, 4095, true),
            (2, -4095, true),
            (2, 4096, true),
            (2, -(edge - 4096), true),
            (2, 4097, false),
            (2, edge, false),
            (3, edge - 1, true),
            (3, -(edge - 1), true),
            (3, 0x12_3456, true),
            (3, edge, false),
            (4, -0x12_3456, true),
            (1, 0, false),
            (5, 0, false),
        ] {
            let f = form(n, 7, k);
            assert_eq!(f.is_some(), some, "{n} {k:#x}");
            if let Some(f) = f {
                assert_eq!(value(&f[..n], 7), Some(k), "{n} {k:#x}");
                assert_eq!(value(&f[..n], 8), None);
            }
        }
        // Another base register, a 32-bit add, an add of another register: not a form.
        let f = form(3, 7, 0x1234).unwrap();
        assert_eq!(value(&[f[0] ^ 1 << 5, f[1], f[2]], 7), None);
        assert_eq!(value(&[f[0], f[1] & !(1 << 31), f[2]], 7), None);
        assert_eq!(value(&[f[0], f[1], f[2] ^ 1 << 5], 7), None);
        assert_eq!(value(&[f[0], f[1], f[2], NOP, NOP], 7), None);
    }

    /// A template at a user address: a three-word move rewritten, a link point's exit too; the proof holds, and each
    /// mutation of the rewritten code is refused by it.
    #[test]
    fn rewrite_is_proved_and_mutations_are_not() {
        let rip = 0x7f12_3456_7100u64;
        let base = rip & !0xfff;
        // header; mov x5, #rip + 0x40 (3 words); nop; mov x0, #rip + 0x2000 (3 words); svc #LINK; literal slot (8 bytes).
        let mut words = std::vec![0x0000_0040u32];
        let (m5, n5) = load_constant(rip + 0x40, 5);
        let (m0, n0) = load_constant(rip + 0x2000, 0);
        assert_eq!((n5, n0), (3, 3));
        words.extend_from_slice(&m5[..3]);
        words.push(NOP);
        let link_at = 4 * words.len() as u32;
        words.extend_from_slice(&m0[..3]);
        words.push(SVC_LINK);
        let literal_at = 4 * words.len() as u32;
        words.extend_from_slice(&[0, 0]);
        let original: Vec<u8> = words.iter().flat_map(|w| w.to_le_bytes()).collect();
        let relocs = [
            1 << 6 | RELOC_MOVE | 5,
            link_at / 4 << 6 | RELOC_MOVE,
            literal_at / 4 << 6,
        ];
        let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
        links[0] = LinkPoint {
            offset: link_at,
            word: m0[0],
            target: rip + 0x2000,
        };
        assert_eq!(eligible(&original, &relocs, rip), Ok(()));
        let mut code = original.clone();
        assert_eq!(rewrite(&mut code, &relocs, &mut links, rip), Ok(()));
        assert_eq!(links[0].word, load_base(0));
        assert!(proved(&original, &code, &relocs, &links, rip, 0));
        assert_eq!(
            value(&[word(&code, 4), word(&code, 8), word(&code, 12)], 5),
            Some(rip as i64 + 0x40 - base as i64)
        );
        assert_eq!(
            svc_slot(|o| word(&code, o as usize), &links[0]),
            u64::from(link_at) + 12
        );
        // Each mutation is red: a word outside the rewrite, the header, a form's value, its register, the link point's
        // target, a literal slot listed as a move, a move dropped from the list.
        let red = |at: usize, w: u32| {
            let mut c = code.clone();
            c[at..at + 4].copy_from_slice(&w.to_le_bytes());
            !proved(&original, &c, &relocs, &links, rip, 0)
        };
        assert!(red(16, 0xd503_201e));
        assert!(red(0, 0));
        assert!(red(12, word(&code, 12) ^ 1 << 10));
        assert!(red(4, load_base(6)));
        assert!(red(link_at as usize + 12, NOP));
        let mut l = links;
        l[0].target += 4;
        assert!(!proved(&original, &code, &relocs, &l, rip, 0));
        assert!(!proved(
            &original,
            &code,
            &[relocs[0], relocs[1], literal_at / 4 << 6 | RELOC_MOVE],
            &links,
            rip,
            0
        ));
        assert!(!proved(&original, &code, &relocs[1..], &links, rip, 0));
        // The literal slot is not compared (the tail's RIP is left as the template's, or poisoned in the check mode).
        let mut c = code.clone();
        c[literal_at as usize..][..8].copy_from_slice(&u64::MAX.to_le_bytes());
        assert!(proved(&original, &c, &relocs, &links, rip, 0));
        // A copy: link points restored and moved; refused near a canonical edge or at another offset.
        let to = 0x7f00_0000_0100;
        let mut c = code.clone();
        c[link_at as usize + 12..][..4].copy_from_slice(&0x1400_0010u32.to_le_bytes());
        let mut l = links;
        assert_eq!(relocate(&mut c, &mut l, rip, to, 0x40), Ok(()));
        assert_eq!((c.clone(), l[0].target), (code.clone(), to + 0x2000));
        assert!(
            !proved(&original, &c, &relocs, &l, to, 0),
            "the original is the template's"
        );
        let mut l = links;
        assert_eq!(
            relocate(&mut code.clone(), &mut l, rip, 0x7fff_ffff_0100, 0x40),
            Err(refusal::PLACE)
        );
        assert_eq!(
            relocate(&mut code.clone(), &mut l, rip, to + 8, 0x40),
            Err(refusal::PLACE)
        );
        // Refusals of the rewrite: a one-word move, a move out of reach, a link point at no move.
        let one: Vec<u8> = [0u32, 0x9280_0005, NOP]
            .iter()
            .flat_map(|w| w.to_le_bytes())
            .collect();
        assert_eq!(
            eligible(&one, &[1 << 6 | RELOC_MOVE | 5], rip),
            Err(refusal::ONE_WORD)
        );
        let (far, nf) = load_constant(rip + REACH, 5);
        let far: Vec<u8> = [0]
            .iter()
            .chain(&far[..nf])
            .flat_map(|w: &u32| w.to_le_bytes())
            .collect();
        assert_eq!(
            eligible(&far, &[1 << 6 | RELOC_MOVE | 5], rip),
            Err(refusal::REACH)
        );
        let mut l = links;
        l[0].offset = 16;
        assert_eq!(
            rewrite(&mut original.clone(), &relocs, &mut l, rip),
            Err(refusal::MOVE)
        );
    }

    #[test]
    fn edges_and_classes() {
        assert!(reaches(0x7f12_3456_7000) && reaches(REACH) && !reaches(REACH - 4096));
        assert!(!reaches(0x7fff_ff00_0000) && reaches(0x7fff_feff_e000 - 8192));
        assert!(reaches(0xffff_ffff_8000_0000) && !reaches(0xffff_8000_0000_0000));
        assert_eq!(class(true, 0x1000, true, 0x1ff0), Class::SamePage);
        assert_eq!(class(true, 0x1000, true, 0x2000), Class::CrossPage);
        assert_eq!(class(true, 0x1000, false, 0x1000), Class::SharedToAbsolute);
        assert_eq!(class(false, 0x1000, true, 0x1000), Class::AbsoluteToShared);
        assert_eq!(class(false, 0, false, 0), Class::Absolute);
    }
}
