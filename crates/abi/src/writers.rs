//! Per-vCPU reverse map of the writable shadows a vCPU installed (docs/writable-drop.md), so that `mark` can remove its
//! own writable shadows of one guest-physical page instead of every shadow it holds.
//!
//! An entry is two words: `[va | KEPT | VALID, page | full tag << 32 | cr3 tag << 48]` (`va` page-aligned, `page` the
//! guest-physical page number). It is *live* while its tags equal the vCPU's epochs ([`Epochs`]): the full epoch moves
//! at every full shadow reset, the cr3 epoch at every CR3 load that kept the kept half (the supervisor space's upper
//! half), which outlives the latter. A live entry is never evicted; an insert that finds no slot in its probe window
//! makes the map lossy for the current full epoch, and a lossy map answers no drop (the caller resets everything).
//!
//! Over-approximation is always allowed (a dead entry looking live, an entry whose shadow was removed by an INVLPG): it
//! only costs an extra unmap. Under-approximation never happens while the map is not lossy: every writable shadow
//! installed since the last full reset has a live entry until its shadow is gone.

/// Entries (16 bytes each).
pub const ENTRIES: usize = 1 << 17;
pub const BYTES: usize = ENTRIES * 16;
/// Slots probed from an entry's home slot (inserts and lookups alike).
pub const WINDOW: usize = 16;
pub const VALID: u64 = 1;
pub const KEPT: u64 = 2;
const TAG_MASK: u64 = 0xffff;

/// The vCPU's epochs and the lossy mark (`lossy == full + 1` while the current full epoch is lossy).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Epochs {
    pub full: u64,
    pub cr3: u64,
    pub lossy: u64,
}
impl Epochs {
    pub fn lossy(&self) -> bool {
        self.lossy == self.full.wrapping_add(1)
    }
}

/// Whether a shadow of `va` in the space of `user` survives a CR3 load that keeps the kept half
/// (`natemu_abi::shadow::Space::reset_cr3`): the supervisor space's upper half. Its non-global shadows there do not, but
/// counting them as kept only over-approximates.
pub const fn kept(user: bool, va: u64) -> bool {
    !user && va >> 47 != 0
}

pub fn home(page: u64) -> usize {
    (page.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> (64 - ENTRIES.trailing_zeros())) as usize
}
fn slot(home: usize, i: usize) -> usize {
    (home + i) & (ENTRIES - 1)
}
fn second(page: u64, e: &Epochs) -> u64 {
    (page & 0xffff_ffff) | (e.full & TAG_MASK) << 32 | (e.cr3 & TAG_MASK) << 48
}
/// Whether entry `w` is live under epochs `e`.
pub fn live(w: [u64; 2], e: &Epochs) -> bool {
    w[0] & VALID != 0
        && (w[1] >> 32) & TAG_MASK == e.full & TAG_MASK
        && (w[0] & KEPT != 0 || (w[1] >> 48) & TAG_MASK == e.cr3 & TAG_MASK)
}
fn page_of(w: [u64; 2]) -> u64 {
    w[1] & 0xffff_ffff
}

/// Records a writable shadow of `va` (page-aligned) onto guest page `page` (in the space of `user`) in the first slot of
/// its probe window that holds no live entry. Live entries before that slot are checked for the same shadow; one after it
/// (a slot before it died since) is not, so a shadow may be recorded twice: an over-approximation that costs only a
/// second unmap at its drop. Returns false when the window was full of live entries: the caller marks the map lossy.
pub fn insert(table: &mut [[u64; 2]], e: &Epochs, user: bool, va: u64, page: u64) -> bool {
    let first = [va & !4095 | if kept(user, va) { KEPT } else { 0 } | VALID, second(page, e)];
    let h = home(page);
    for i in 0..WINDOW {
        let s = slot(h, i);
        let w = table[s];
        if !live(w, e) {
            table[s] = first;
            return true;
        }
        // Same shadow already recorded; a kept entry also covers a non-kept one of the same va.
        if w[0] & !KEPT == first[0] & !KEPT
            && page_of(w) == page & 0xffff_ffff
            && (w[0] & KEPT != 0 || first[0] & KEPT == 0)
        {
            return true;
        }
    }
    false
}

/// Calls `unmap(va)` for every live entry of guest page `page` and invalidates it. Returns how many.
pub fn drop_page(table: &mut [[u64; 2]], e: &Epochs, page: u64, mut unmap: impl FnMut(u64)) -> usize {
    let h = home(page);
    let mut n = 0;
    for i in 0..WINDOW {
        let s = slot(h, i);
        let w = table[s];
        if live(w, e) && page_of(w) == page & 0xffff_ffff {
            unmap(w[0] & !4095);
            table[s] = [0, 0];
            n += 1;
        }
    }
    n
}

/// Counter indices of `cache::Control::writers`.
pub mod count {
    /// Drops answered from the map (no full reset, own marks and peers' requests), shadows they unmapped, drops answered by
    /// a full reset because the map was lossy, inserts that made it lossy, inserts.
    pub const DROPS: usize = 0;
    pub const UNMAPPED: usize = 1;
    pub const LOSSY_RESETS: usize = 2;
    pub const OVERFLOWS: usize = 3;
    pub const INSERTS: usize = 4;
    /// Peers' page requests this vCPU served (by a drop or a reset), and asks of this vCPU to a peer that found its slots
    /// full (a full reset asked instead).
    pub const SERVED: usize = 5;
    pub const SLOTS_FULL: usize = 6;
    pub const COUNT: usize = 7;
    pub const NAMES: [&str; COUNT] = ["drops", "unmapped", "lossy_resets", "overflows", "inserts", "served", "slots_full"];
}

#[cfg(test)]
mod tests {
    extern crate std;
    use super::*;
    use std::{vec, vec::Vec};

    fn table() -> Vec<[u64; 2]> {
        vec![[0, 0]; ENTRIES]
    }
    fn dropped(t: &mut [[u64; 2]], e: &Epochs, page: u64) -> Vec<u64> {
        let mut v = Vec::new();
        drop_page(t, e, page, |va| v.push(va));
        v.sort();
        v
    }

    #[test]
    fn a_zeroed_table_holds_nothing() {
        let mut t = table();
        assert!(dropped(&mut t, &Epochs::default(), 0).is_empty());
    }

    #[test]
    fn every_alias_of_a_page_is_dropped_once_and_only_its_own() {
        let mut t = table();
        let e = Epochs { full: 3, cr3: 7, lossy: 0 };
        let physmap = 0xffff_8880_0123_4000;
        assert!(insert(&mut t, &e, true, 0x7f00_0000_1000, 42));
        assert!(insert(&mut t, &e, false, physmap, 42));
        assert!(insert(&mut t, &e, true, 0x7f00_0000_1000, 42)); // a re-install: recorded once
        assert!(insert(&mut t, &e, true, 0x7f00_0000_2000, 43));
        assert_eq!(dropped(&mut t, &e, 42), vec![0x7f00_0000_1000, physmap]);
        assert!(dropped(&mut t, &e, 42).is_empty());
        assert_eq!(dropped(&mut t, &e, 43), vec![0x7f00_0000_2000]);
    }

    #[test]
    fn a_full_reset_kills_every_entry_and_a_cr3_load_only_the_unkept() {
        let mut t = table();
        let mut e = Epochs { full: 1, cr3: 1, lossy: 0 };
        let kernel = 0xffff_8880_0000_5000;
        assert!(insert(&mut t, &e, true, 0x1000, 9)); // user space: not kept
        assert!(insert(&mut t, &e, false, 0x2000, 9)); // supervisor lower half: not kept
        assert!(insert(&mut t, &e, false, kernel, 9)); // supervisor upper half: kept
        e.cr3 += 1;
        assert_eq!(dropped(&mut t.clone(), &e, 9), vec![kernel]);
        e.full += 1;
        assert!(dropped(&mut t, &e, 9).is_empty());
    }

    #[test]
    fn a_dead_slot_is_reused_and_a_live_one_never() {
        let mut t = table();
        let mut e = Epochs { full: 1, cr3: 1, lossy: 0 };
        // Fill page 5's window with live entries of pages sharing its home.
        let h = home(5);
        let mut same: Vec<u64> = (0..1u64 << 24).filter(|&p| home(p) == h).take(WINDOW).collect();
        assert_eq!(same.len(), WINDOW);
        for (i, &p) in same.iter().enumerate() {
            assert!(insert(&mut t, &e, true, 0x10_0000 + 4096 * i as u64, p));
        }
        let extra = (1u64 << 24..).find(|&p| home(p) == h).unwrap();
        assert!(!insert(&mut t, &e, true, 0x90_0000, extra)); // full of live entries: lossy
        e.cr3 += 1; // they die
        assert!(insert(&mut t, &e, true, 0x90_0000, extra));
        same.push(extra);
        assert_eq!(dropped(&mut t, &e, extra), vec![0x90_0000]);
    }

    #[test]
    fn an_insert_takes_the_first_free_slot_so_a_shadow_after_a_hole_is_recorded_twice() {
        let mut t = table();
        let e = Epochs { full: 1, cr3: 1, lossy: 0 };
        let h = home(5);
        let q = (6..).find(|&p| home(p) == h).unwrap();
        assert!(insert(&mut t, &e, true, 0x1000, 5)); // slot 0
        assert!(insert(&mut t, &e, true, 0x2000, q)); // slot 1
        assert!(insert(&mut t, &e, true, 0x2000, q)); // already recorded before any free slot: not again
        assert_eq!(dropped(&mut t, &e, 5), vec![0x1000]); // slot 0 is free now
        assert!(insert(&mut t, &e, true, 0x2000, q)); // into slot 0: a second entry, unmapped twice at the drop
        assert_eq!(dropped(&mut t, &e, q), vec![0x2000, 0x2000]);
    }

    #[test]
    fn entries_outside_the_window_never_hide_a_live_one() {
        // A live entry is always inside its own page's window, so a drop that probes the window sees it.
        let mut t = table();
        let e = Epochs { full: 2, cr3: 2, lossy: 0 };
        for p in 0..20_000u64 {
            if !insert(&mut t, &e, p % 2 == 0, 0x4000_0000 + 4096 * p, p) {
                continue;
            }
        }
        let mut n = 0;
        for p in 0..20_000u64 {
            let v = dropped(&mut t, &e, p);
            assert!(v.len() <= 1);
            if v.len() == 1 {
                assert_eq!(v[0], 0x4000_0000 + 4096 * p);
                n += 1;
            }
        }
        assert!(n > 19_000);
    }

    #[test]
    fn lossy_is_per_full_epoch() {
        let mut e = Epochs { full: 4, cr3: 0, lossy: 0 };
        assert!(!e.lossy());
        e.lossy = e.full + 1;
        assert!(e.lossy());
        e.full += 1;
        assert!(!e.lossy());
    }
}
