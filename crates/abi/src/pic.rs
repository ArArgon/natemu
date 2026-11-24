//! Position-independent translations (docs/restructure/pic-plan.md), the rewrite the resident makes of a reuse template
//! (perf chunk 10's relocations) and the rules it is checked by. A guest-RIP move of n words building v becomes, in exactly
//! n words, "load the page base of the block from the guest-state page ([`crate::execution::frame::PAGE_BASE`]), add
//! k = v - base": `ldr xR, [x28, #PAGE_BASE]` and 1-3 `add`/`sub xR, xR, #imm` (`nop` filling a fourth word). Two words reach
//! base ± 4 KiB or a page multiple below 16 MiB, three or four words ± 16 MiB, one word nothing. The block's entry records
//! the page base ([`AND_PAGE`], [`STORE_BASE`]); every other word stays FEX's (its literal slots too: the tail's RIP, which
//! no generated code reads). [`proved`] is the static check of one rewritten block against the object it came from.
//!
//! 32-bit code (`bits32`): fexc's one move form, `movz w; movk w, lsl #16; nop` ([`crate::execution::guest_rip_move32`]),
//! becomes `ldr xR, [x28, #PAGE_BASE]` and two 32-bit `add`/`sub wR, wR, #imm`, always three words, for k = the move's value
//! less the page base modulo 2^32, as a signed offset: the adds wrap at 4 GiB as fexc's masked value does, so the form is
//! exact at every page base of the instance's 32-bit range ([`moved`] moves its link targets the same way).
use crate::execution::{LinkPoint, MAX_LINK_POINTS, RELOC_MOVE, canonical, frame, move_value, move32_value, svc};

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
const ADD32: u32 = 0x1100_0000;
const SUB32: u32 = 0x5100_0000;
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
}

/// Counters (`Control::pic_counts`; PIC study stage 1, kept by the stages after it). User (CPL 3) blocks unless named.
pub mod count {
    /// User blocks placed from the host's answers; of them, not templates (the host's relocation rule refused them).
    pub const HOST: usize = 0;
    pub const NOT_TEMPLATE: usize = 1;
    /// User templates rewritten (stage 1: that would be); refused for a global mapping, a one-word move, a move out of
    /// reach, a malformed move (`PLACE`: unused; copies refused near a canonical edge are perf chunk 10's `PLACE` kind).
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
    /// Stage 3 study: links (by source) from a shared translation into an absolute block on the source's own page and
    /// frames (dispatcher-slot links that sharing the target at link time would make local).
    pub const SHAREABLE_TARGETS: usize = 18;
    /// Stage 2: templates whose rewrite failed its static proof ([`super::proved`]; a resident fault: left absolute, and
    /// the check halts); copies the check could not compare with the host's answer at their RIP (another code size or
    /// relocation list there: fexc chose other move lengths, which position-independent code does not depend on).
    pub const UNPROVED: usize = 19;
    pub const CHECK_SHAPE: usize = 20;
    /// Stage 2: stops at a position-independent block's `and`, `str` and body entry (three counters from
    /// `ENTRY_STOPS`), and at an armed `svc` word of a link point linked there.
    pub const ENTRY_STOPS: usize = 21;
    pub const SVC_STOPS: usize = 24;
    /// Stage 3: templates shared (rewritten into a [`crate::execution::cache::TRANSLATION`] at their first use at a second
    /// address); instances made; instances refused (a page near a canonical edge, a global mapping); checked links made.
    pub const SHARES: usize = 25;
    pub const INSTANCES: usize = 26;
    pub const INSTANCE_REFUSED: usize = 27;
    pub const CHECKED_LINKS: usize = 28;
    /// `pic-count` resident builds only: dispatcher misses (L1 had no entry for x0) by what the resident then needed: none
    /// (the block was current in L2: an L1 conflict or a table given away), a revalidation (cached, not current), a new
    /// block (nothing cached: a host request or an in-resident copy or instance).
    pub const MISS_CURRENT: usize = 29;
    pub const MISS_REVALIDATE: usize = 30;
    pub const MISS_NEW: usize = 31;
    /// Indirect plan step 1 (every build): computed exits given their own appended words, as a jump (`br x1`) and as a
    /// call (`blr x1`); link points of shared code given theirs; checked links made through a link point's words (the
    /// others go to the slot); stops taken at once at an appended word, and of them in a dead block.
    pub const TAILS: usize = 32;
    pub const TAIL_CALLS: usize = 33;
    pub const TAIL_POINTS: usize = 34;
    pub const TAIL_LINKS: usize = 35;
    pub const TAIL_STOPS: usize = 36;
    pub const TAIL_DEAD_STOPS: usize = 37;
    pub const COUNTS: usize = 38;
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

/// The address a form of k builds at page `base`: base + k, in 32-bit code modulo 2^32 ([`form`]'s 32-bit adds).
pub fn built(base: u64, k: i64, bits32: bool) -> u64 {
    let v = base.wrapping_add(k as u64);
    if bits32 { v & 0xffff_ffff } else { v }
}
/// Link target `target` of code at `from` as code at `to` exits to it ([`built`]: in 32-bit code modulo 2^32). The one
/// rule for a shared translation's targets at the address it runs for: an instance's, a link's, the linker's and a stop's.
pub fn moved(target: u64, from: u64, to: u64, bits32: bool) -> u64 {
    built(to, target.wrapping_sub(from) as i64, bits32)
}
/// The `n`-word form of a move building `base + k` in `reg` (module comment); `None` when `n` words cannot reach `k`. In
/// 32-bit code (`bits32`) only three words, the adds 32-bit.
pub fn form(n: usize, reg: u32, k: i64, bits32: bool) -> Option<[u32; 4]> {
    let (add, sub) = if bits32 { (ADD32, SUB32) } else { (ADD, SUB) };
    let (op, m) = (if k < 0 { sub } else { add }, k.unsigned_abs());
    let add =
        |shift: u64, imm: u64| op | (shift as u32) << 22 | (imm as u32) << 10 | reg << 5 | reg;
    let ldr = load_base(reg);
    match n {
        _ if bits32 => (n == 3 && m < REACH).then(|| [ldr, add(1, m >> 12), add(0, m & 0xfff), 0]),
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
/// [`form`]): `ldr reg, [x28, #PAGE_BASE]`, then 64-bit `add`/`sub reg, reg, #imm{, lsl #12}` or `nop`; in 32-bit code
/// exactly two 32-bit `add`/`sub` (what they build: [`built`]).
pub fn value(words: &[u32], reg: u32, bits32: bool) -> Option<i64> {
    let (&first, rest) = words.split_first()?;
    if first != load_base(reg) || rest.len() > 3 || (bits32 && rest.len() != 2) {
        return None;
    }
    let (add, sub) = if bits32 { (ADD32, SUB32) } else { (ADD, SUB) };
    rest.iter().try_fold(0i64, |k, &w| {
        if w == NOP && !bits32 {
            return Some(k);
        }
        let imm = i64::from(w >> 10 & 0xfff) << (12 * (w >> 22 & 1));
        match w & 0xff80_0000 {
            _ if w & 31 != reg || w >> 5 & 31 != reg => None,
            op if op == add => Some(k + imm),
            op if op == sub => Some(k - imm),
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
/// [`refusal`]. 32-bit code: fexc's one form alone, k its value less `base` modulo 2^32 (signed).
fn one(code: &[u8], at: usize, reg: u32, base: u64, bits32: bool) -> Result<(usize, [u32; 4]), usize> {
    if at + 4 > code.len() || reg == 28 {
        return Err(refusal::MOVE);
    }
    let mut words = [0; 5];
    let n = ((code.len() - at) / 4).min(5);
    for (k, w) in words[..n].iter_mut().enumerate() {
        *w = word(code, at + 4 * k);
    }
    if bits32 {
        let v = move32_value(&words[..n], reg).ok_or(refusal::MOVE)?;
        let k = i64::from(v.wrapping_sub(base) as u32 as i32);
        return form(3, reg, k, true).map(|f| (3, f)).ok_or(refusal::REACH);
    }
    // In 64-bit code a 32-bit form (a value below 4 GiB) is refused, as perf chunk 10's rule refuses it: FEX would wrap such
    // a value at 4 GiB, the 64-bit form at another base would not.
    let Some((n, v)) = move_value(&words[..n], reg).filter(|_| words[0] >> 31 == 1) else {
        return Err(refusal::MOVE);
    };
    if n == 1 {
        return Err(refusal::ONE_WORD);
    }
    form(n, reg, v.wrapping_sub(base) as i64, false)
        .map(|f| (n, f))
        .ok_or(refusal::REACH)
}
/// Whether the template `code` (admitted for `rip`; `relocs` packed, [`RELOC_MOVE`]; its link points `links`) has a
/// rewrite: each relocation in order, none starting inside the one before (as perf chunk 10's re-check), each move with a
/// [`form`], every link point at a move of x0. `Err`: the [`refusal`]; nothing is written. `bits32`: 32-bit code's.
pub fn eligible(code: &[u8], relocs: &[u32], links: &[LinkPoint], rip: u64, bits32: bool) -> Result<(), usize> {
    let mut free = 0;
    for &r in relocs {
        let at = (r >> 6) as usize * 4;
        if at < free {
            return Err(refusal::MOVE);
        }
        let n = if r & RELOC_MOVE == 0 {
            // The one guest-RIP literal is the block tail's `RIP` (FEX's `JITCodeTail`, 8 bytes in, at the offset the
            // code header word holds), which no generated code reads (it stays the template's): one anywhere else would
            // be read with the template's RIP.
            if code.len() < 4 || at != word(code, 0) as usize + 8 || at + 8 > code.len() {
                return Err(refusal::MOVE);
            }
            2
        } else {
            one(code, at, r & 31, rip & !0xfff, bits32)?.0
        };
        free = at + 4 * n;
    }
    let at_move = |o: u32| {
        relocs
            .iter()
            .any(|&r| r & RELOC_MOVE != 0 && r & 31 == 0 && (r >> 6) * 4 == o)
    };
    if links.iter().any(|p| p.offset != 0 && !at_move(p.offset)) {
        return Err(refusal::MOVE);
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
    bits32: bool,
) -> Result<(), usize> {
    eligible(code, relocs, links, rip, bits32)?;
    for &r in relocs.iter().filter(|&&r| r & RELOC_MOVE != 0) {
        let at = (r >> 6) as usize * 4;
        let (n, f) = one(code, at, r & 31, rip & !0xfff, bits32)?;
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
/// position-independent form (32-bit code: `add`/`sub w0`) or the `movk x0` words of an absolute one (32-bit code: `movk
/// w0` and its `nop`).
pub fn svc_slot(word: impl Fn(u64) -> u32, p: &LinkPoint) -> u64 {
    let at = u64::from(p.offset);
    let plain = |w: u32| {
        w == NOP
            || (matches!(w & 0xff80_0000, ADD | SUB | ADD32 | SUB32) && w & 0x3ff == 0)
            || w & 0x7f80_001f == 0x7280_0000
    };
    (1..4)
        .find(|&k| !plain(word(at + 4 * k)))
        .map_or(at + 16, |k| at + 4 * k)
}
/// `p` names an exit of position-independent `code` (`code_size` bytes, at page `base`) as [`rewrite`] leaves it: its word
/// is in the code and is `p.word` = `ldr x0, [x28, #PAGE_BASE]`, the form after it builds `p.target` from `base` ([`built`]),
/// and `svc #LINK` follows.
pub fn link_point(word: impl Fn(u64) -> u32, code_size: u32, p: &LinkPoint, base: u64, bits32: bool) -> bool {
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
    value(&words[..n], 0, bits32).is_some_and(|k| built(base, k, bits32) == p.target)
}
/// The static proof of one rewritten block: `code` (its word at each offset from its header word, `len` bytes, its link
/// points as admitted) is `original` (the word at each offset of the admitted object's code for `rip`) but for the header word, now [`STORE_BASE`] (the only store [`rewrite`] adds); each move of `relocs` (in order,
/// none inside the one before; its words decoded from `original`), now `value` k in as many words with base + k its old
/// value; the word at `skip` (a computed exit's `b`, 0 for none) and literal slots (the tail's RIP, which no generated code
/// reads), not compared; and every link point a [`link_point`] of `code`. Every other word is the same. 32-bit code
/// (`bits32`): each move fexc's one form, its rewrite three words building its value modulo 2^32.
#[allow(clippy::too_many_arguments)]
pub fn proved(
    original: impl Fn(usize) -> u32,
    code: impl Fn(usize) -> u32,
    len: usize,
    relocs: &[u32],
    links: &[LinkPoint; MAX_LINK_POINTS],
    rip: u64,
    skip: u32,
    bits32: bool,
) -> bool {
    let base = rip & !0xfff;
    if len % 4 != 0 || len < 4 || code(0) != STORE_BASE {
        return false;
    }
    // Words [from, to) are the same, but `skip`'s.
    let same = |from: usize, to: usize| {
        (from..to)
            .step_by(4)
            .all(|at| (skip != 0 && at == skip as usize) || code(at) == original(at))
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
                *w = original(at + 4 * k);
            }
            let decoded = if bits32 {
                move32_value(&words[..avail], reg).map(|v| (3, v))
            } else {
                move_value(&words[..avail], reg).filter(|&(n, _)| n <= 4)
            };
            let Some((n, v)) = decoded else {
                return false;
            };
            let mut new = [0; 4];
            for (k, w) in new[..n].iter_mut().enumerate() {
                *w = code(at + 4 * k);
            }
            if reg == 28 || value(&new[..n], reg, bits32).map(|k| built(base, k, bits32)) != Some(v) {
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
        && links.iter().filter(|p| p.offset != 0).all(|p| {
            link_point(
                |at| {
                    if at as usize + 4 <= len {
                        code(at as usize)
                    } else {
                        0
                    }
                },
                len as u32,
                p,
                base,
                bits32,
            )
        })
}
/// Under the check, where fexc chose other move lengths at an instance's RIP than at its template's (the two cannot be
/// compared word for word, [`proved`]): the guest-RIP relocations agree in order, kind and register, and each of the
/// host's absolute moves (`host`, `host_len` bytes, packed `host_relocs`, admitted for `rip`) builds the value the
/// translation's rewritten move (`code`, `len`, `relocs`) builds at `rip`'s page (its [`form`] re-encoded exactly).
#[allow(clippy::too_many_arguments)]
pub fn moves_agree(
    host: impl Fn(usize) -> u32,
    host_len: usize,
    host_relocs: impl Iterator<Item = u32>,
    code: impl Fn(usize) -> u32,
    len: usize,
    relocs: &[u32],
    rip: u64,
    bits32: bool,
) -> bool {
    let base = rip & !0xfff;
    let mut mine = relocs.iter();
    for h in host_relocs {
        let Some(&r) = mine.next() else {
            return false;
        };
        if h & (RELOC_MOVE | 31) != r & (RELOC_MOVE | 31) {
            return false;
        }
        if h & RELOC_MOVE == 0 {
            continue;
        }
        let (reg, ha, at) = (h & 31, (h >> 6) as usize * 4, (r >> 6) as usize * 4);
        let mut words = [0; 5];
        let avail = (host_len.saturating_sub(ha) / 4).min(5);
        for (k, w) in words[..avail].iter_mut().enumerate() {
            *w = host(ha + 4 * k);
        }
        let decoded = if bits32 {
            move32_value(&words[..avail], reg)
        } else {
            move_value(&words[..avail], reg).map(|(_, v)| v)
        };
        let Some(v) = decoded else {
            return false;
        };
        let mut new = [0; 4];
        let avail = (len.saturating_sub(at) / 4).min(4);
        for (k, w) in new[..avail].iter_mut().enumerate() {
            *w = code(at + 4 * k);
        }
        let k = (2..=avail).rev().find_map(|n| {
            value(&new[..n], reg, bits32).filter(|&k| form(n, reg, k, bits32).is_some_and(|f| f[..n] == new[..n]))
        });
        if k.map(|k| built(base, k, bits32)) != Some(v) {
            return false;
        }
    }
    mine.next().is_none()
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
            let f = form(n, 7, k, false);
            assert_eq!(f.is_some(), some, "{n} {k:#x}");
            if let Some(f) = f {
                assert_eq!(value(&f[..n], 7, false), Some(k), "{n} {k:#x}");
                assert_eq!(value(&f[..n], 8, false), None);
            }
        }
        // Another base register, a 32-bit add, an add of another register: not a form.
        let f = form(3, 7, 0x1234, false).unwrap();
        assert_eq!(value(&[f[0] ^ 1 << 5, f[1], f[2]], 7, false), None);
        assert_eq!(value(&[f[0], f[1] & !(1 << 31), f[2]], 7, false), None);
        assert_eq!(value(&[f[0], f[1], f[2] ^ 1 << 5], 7, false), None);
        assert_eq!(value(&[f[0], f[1], f[2], NOP, NOP], 7, false), None);
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
        words[0] = literal_at - 8; // the header: the tail's offset (its RIP 8 bytes in)
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
        assert_eq!(eligible(&original, &relocs, &links, rip, false), Ok(()));
        // A guest-RIP literal anywhere but the tail (here: the `svc` word and the slot's first half) is refused.
        let mid = [relocs[0], relocs[1], (literal_at / 4 - 1) << 6];
        assert_eq!(eligible(&original, &mid, &links, rip, false), Err(refusal::MOVE));
        let mut code = original.clone();
        assert_eq!(rewrite(&mut code, &relocs, &mut links, rip, false), Ok(()));
        assert_eq!(links[0].word, load_base(0));
        assert!(proved(
            |at| word(&original, at),
            |at| word(&code, at),
            code.len(),
            &relocs,
            &links,
            rip,
            0,
            false
        ));
        assert_eq!(
            value(&[word(&code, 4), word(&code, 8), word(&code, 12)], 5, false),
            Some(rip as i64 + 0x40 - base as i64)
        );
        // The check's comparison by move values: the host's absolute code at this RIP agrees; at another page (the
        // values differ), with another register, or with a move missing, it does not.
        let agree = |host: &[u8], hr: &[u32], at: u64| {
            moves_agree(
                |o| word(host, o),
                host.len(),
                hr.iter().copied(),
                |o| word(&code, o),
                code.len(),
                &relocs,
                at,
                false,
            )
        };
        assert!(agree(&original, &relocs, rip));
        assert!(!agree(&original, &relocs, rip + 0x1000));
        assert!(!agree(
            &original,
            &[relocs[0] ^ 1, relocs[1], relocs[2]],
            rip
        ));
        assert!(!agree(&original, &relocs[1..], rip));
        assert_eq!(
            svc_slot(|o| word(&code, o as usize), &links[0]),
            u64::from(link_at) + 12
        );
        // Each mutation is red: a word outside the rewrite, the header, a form's value, its register, the link point's
        // target, a literal slot listed as a move, a move dropped from the list.
        let red = |at: usize, w: u32| {
            let mut c = code.clone();
            c[at..at + 4].copy_from_slice(&w.to_le_bytes());
            !proved(
                |at| word(&original, at),
                |at| word(&c, at),
                c.len(),
                &relocs,
                &links,
                rip,
                0,
                false,
            )
        };
        assert!(red(16, 0xd503_201e));
        assert!(red(0, 0));
        assert!(red(12, word(&code, 12) ^ 1 << 10));
        assert!(red(4, load_base(6)));
        assert!(red(link_at as usize + 12, NOP));
        let mut l = links;
        l[0].target += 4;
        assert!(!proved(
            |at| word(&original, at),
            |at| word(&code, at),
            code.len(),
            &relocs,
            &l,
            rip,
            0,
            false
        ));
        assert!(!proved(
            |at| word(&original, at),
            |at| word(&code, at),
            code.len(),
            &[relocs[0], relocs[1], literal_at / 4 << 6 | RELOC_MOVE],
            &links,
            rip,
            0,
            false
        ));
        assert!(!proved(
            |at| word(&original, at),
            |at| word(&code, at),
            code.len(),
            &relocs[1..],
            &links,
            rip,
            0,
            false
        ));
        // The literal slot is not compared (the tail's RIP is left as the template's, or poisoned in the check mode).
        let mut c = code.clone();
        c[literal_at as usize..][..8].copy_from_slice(&u64::MAX.to_le_bytes());
        assert!(proved(
            |at| word(&original, at),
            |at| word(&c, at),
            c.len(),
            &relocs,
            &links,
            rip,
            0,
            false
        ));
        // At another address (an instance's) the same code proves only with its link targets moved there and against the
        // code admitted there: here the template's original is not it.
        let to = 0x7f00_0000_0100u64;
        let mut l = links;
        l[0].target = to + 0x2000;
        assert!(!proved(
            |at| word(&original, at),
            |at| word(&code, at),
            code.len(),
            &relocs,
            &l,
            to,
            0,
            false
        ));
        // Refusals of the rewrite: a one-word move, a move out of reach, a link point at no move.
        let one: Vec<u8> = [0u32, 0x9280_0005, NOP]
            .iter()
            .flat_map(|w| w.to_le_bytes())
            .collect();
        assert_eq!(
            eligible(&one, &[1 << 6 | RELOC_MOVE | 5], &[], rip, false),
            Err(refusal::ONE_WORD)
        );
        let (far, nf) = load_constant(rip + REACH, 5);
        let far: Vec<u8> = [0]
            .iter()
            .chain(&far[..nf])
            .flat_map(|w: &u32| w.to_le_bytes())
            .collect();
        assert_eq!(
            eligible(&far, &[1 << 6 | RELOC_MOVE | 5], &[], rip, false),
            Err(refusal::REACH)
        );
        // A 32-bit form (a value below 4 GiB, near a 4 GiB base), as perf chunk 10 refuses it.
        let (low, nl) = load_constant(0x1234_5678, 5);
        assert_eq!((nl, low[0] >> 31), (2, 0));
        let low: Vec<u8> = [0]
            .iter()
            .chain(&low[..nl])
            .flat_map(|w: &u32| w.to_le_bytes())
            .collect();
        assert_eq!(
            eligible(&low, &[1 << 6 | RELOC_MOVE | 5], &[], 0x1234_0100, false),
            Err(refusal::MOVE)
        );
        let mut l = links;
        l[0].offset = 16;
        assert_eq!(
            rewrite(&mut original.clone(), &relocs, &mut l, rip, false),
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

    /// 32-bit code's form: three words at both reach edges, decoded only as itself (not with a `nop`, a 64-bit add, one
    /// add short or another register), and exact across the wrap at 4 GiB both ways, where the 64-bit sum is 2^32 off.
    #[test]
    fn the_32_bit_form_reaches_and_wraps() {
        use crate::execution::{NOP as N, guest_rip_move32};
        let edge = REACH as i64;
        for k in [0, 1, -1, 0xfff, -0x1000, 0x12_3456, edge - 1, -(edge - 1)] {
            let f = form(3, 7, k, true).unwrap();
            assert_eq!((value(&f[..3], 7, true), value(&f[..3], 7, false)), (Some(k), None), "{k:#x}");
            assert_eq!(f[1] >> 31 | f[2] >> 31, 0);
        }
        assert_eq!([form(3, 7, edge, true), form(3, 7, -edge, true), form(2, 7, 0, true), form(4, 7, 0, true)], [None; 4]);
        let f = form(3, 7, 0x1234, true).unwrap();
        for bad in [&[f[0], f[1]][..], &[f[0], f[1], N], &[f[0], f[1] | 1 << 31, f[2]], &[f[0], f[1], f[2] ^ 1 << 5], &[f[0], f[1], f[2], N]] {
            assert_eq!(value(bad, 7, true), None, "{bad:x?}");
        }
        // A move at offset 4 of a block whose page base is `base`, building `v` across the wrap.
        for (base, v, k) in [(0xffff_f000u64, 0x0000_0234u64, 0x1234i64), (0x1000, 0xffff_ff00, -0x1100)] {
            let [z, m] = guest_rip_move32(v, 5);
            let code: Vec<u8> = [0, z, m, N].iter().flat_map(|w| w.to_le_bytes()).collect();
            let (n, f) = one(&code, 4, 5, base, true).unwrap();
            assert_eq!((n, value(&f[..3], 5, true)), (3, Some(k)));
            assert_eq!((built(base, k, true), built(base, k, false) & 0xffff_ffff), (v, v));
            assert_ne!(built(base, k, false), v, "a 64-bit sum is 2^32 off");
            // Not fexc's form (no `nop`, a 64-bit move, another register): refused.
            for w in [[0, z, m, 0], [0, z | 1 << 31, m | 1 << 31, N], [0, z ^ 1, m ^ 1, N]] {
                let code: Vec<u8> = w.iter().flat_map(|w| w.to_le_bytes()).collect();
                assert_eq!(one(&code, 4, 5, base, true), Err(refusal::MOVE));
            }
        }
        assert_eq!(moved(0x1100, 0xffff_f100, 0x5655_5100, true), 0x5655_7100);
        assert_eq!(moved(0x5655_7100, 0x5655_5100, 0xffff_f100, true), 0x1100);
        assert_eq!(moved(0x1100, 0xffff_f100, 0x5655_5100, false), 0xffff_ffff_5655_7100);
        // Out of reach (17 MiB away): refused as such.
        let [z, m] = guest_rip_move32(0x5655_5000 + (17 << 20), 5);
        let code: Vec<u8> = [0, z, m, N].iter().flat_map(|w| w.to_le_bytes()).collect();
        assert_eq!(one(&code, 4, 5, 0x5655_5000, true), Err(refusal::REACH));
    }

    /// A 32-bit template on the last page below 4 GiB whose link target wraps to page 1: rewritten and proved; its
    /// instance's link point (target moved modulo 2^32) is a link point of the same code at another base; the host's absolute
    /// code there agrees by values; each mutation is red (a 64-bit add, the original without its `nop`, one add short, the
    /// wrong register, an unmasked target).
    #[test]
    fn a_32_bit_rewrite_is_proved_across_the_wrap() {
        use crate::execution::{NOP as N, guest_rip_move32};
        let words_at = |rip: u64| {
            let [z5, m5] = guest_rip_move32(rip + 0x40, 5);
            let [z0, m0] = guest_rip_move32(rip + 0x2000, 0);
            // header; mov w5, #rip + 0x40; nop; nop; mov w0, #rip + 0x2000 (wrapped); nop; svc #LINK; literal slot.
            let mut w = std::vec![0, z5, m5, N, N, z0, m0, N, SVC_LINK, 0, 0];
            w[0] = 4 * 9 - 8;
            w
        };
        let bytes = |w: &[u32]| -> Vec<u8> { w.iter().flat_map(|w| w.to_le_bytes()).collect() };
        let rip = 0xffff_f100u64;
        let original = bytes(&words_at(rip));
        let relocs = [1 << 6 | RELOC_MOVE | 5, 5 << 6 | RELOC_MOVE, 9 << 6];
        let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
        links[0] = LinkPoint { offset: 20, word: words_at(rip)[5], target: (rip + 0x2000) & 0xffff_ffff };
        assert_eq!(links[0].target, 0x1100);
        assert_eq!(eligible(&original, &relocs, &links, rip, false), Err(refusal::MOVE));
        assert_eq!(eligible(&original, &relocs, &links, rip, true), Ok(()));
        let mut code = original.clone();
        assert_eq!(rewrite(&mut code, &relocs, &mut links, rip, true), Ok(()));
        let w = |c: &[u8], at: usize| word(c, at);
        let prove = |orig: &[u8], c: &[u8], l: &[LinkPoint; MAX_LINK_POINTS], at: u64| {
            proved(|o| w(orig, o), |o| w(c, o), c.len(), &relocs, l, at, 0, true)
        };
        assert!(prove(&original, &code, &links, rip));
        assert!(!proved(|o| w(&original, o), |o| w(&code, o), code.len(), &relocs, &links, rip, 0, false));
        assert_eq!(svc_slot(|o| w(&code, o as usize), &links[0]), 32);
        // At another base: the code is the same, its link point's target moved there modulo 2^32; the host's code admitted
        // there is fexc's at that RIP, which the rewritten code proves against and agrees with by values.
        let to = 0x5655_5100u64;
        let there = bytes(&words_at(to));
        let mut l = links;
        l[0].target = moved(links[0].target, rip, to, true);
        assert_eq!(l[0].target, 0x5655_7100);
        assert!(prove(&there, &code, &l, to));
        assert!(link_point(|o| w(&code, o as usize), code.len() as u32, &l[0], to & !0xfff, true));
        assert!(!prove(&there, &code, &links, to), "the template's target");
        let mut unmasked = l;
        unmasked[0].target = moved(links[0].target, rip, to, false);
        assert!(!prove(&there, &code, &unmasked, to), "a target 2^32 off");
        let agree = |c: &[u8], at: u64| {
            moves_agree(|o| w(&there, o), there.len(), relocs.iter().copied(), |o| w(c, o), c.len(), &relocs, at, true)
        };
        assert!(agree(&code, to) && !agree(&code, to + 0x1000));
        // Mutations of the rewritten code: a 64-bit add, the lo add dropped (a `nop`), the wrong register.
        let red = |at: usize, word: u32| {
            let mut c = code.clone();
            c[at..at + 4].copy_from_slice(&word.to_le_bytes());
            !prove(&original, &c, &links, rip) && !agree(&c, to)
        };
        assert!(red(8, w(&code, 8) | 1 << 31));
        assert!(red(12, N));
        assert!(red(12, w(&code, 12) ^ (1 | 1 << 5)));
        assert!(red(4, load_base(6)));
        // The original without its `nop` (an unpadded move): not a template, and not what the proof accepts.
        let mut unpadded = words_at(rip);
        unpadded[3] = 0xd503_201e;
        let unpadded = bytes(&unpadded);
        assert_eq!(eligible(&unpadded, &relocs, &links, rip, true), Err(refusal::MOVE));
        assert!(!prove(&unpadded, &code, &links, rip));
    }
}
