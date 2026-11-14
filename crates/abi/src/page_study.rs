//! Page-currency plan, chunk 0 (docs/restructure/page-currency-plan.md sections 3.1-3.3, 8): the per-vCPU record of fetch-walk
//! results and its counters. Chunk 0 only observes (the `page-study` resident feature): every fetch walk is still made, the
//! record's answer is compared with it, and a miss records the walk's. The record and its rules are pure functions here
//! (unit-tested on the host); the resident supplies the epochs, the controls and the guest RAM.
//!
//! A [`Record`] maps a linear page (or 2 MiB region) found by a fetch walk, under a privilege class, to the leaf's
//! guest-physical base, for the TLB epoch of the walk. It is a TLB entry: it answers until the epoch moves (a CR3 load, a
//! global flush; a global leaf survives CR3 loads, as a global block does) or until an INVLPG or a #PF names its page.
use crate::guest_memory::GuestRam;

/// Records per vCPU (direct-mapped by a hash of the key), 32 bytes each: two per 64-byte line, none straddling one.
pub const ENTRY_BITS: u32 = 13;
pub const ENTRIES: usize = 1 << ENTRY_BITS;
pub const ENTRY_BYTES: usize = 32;
pub const BYTES: usize = ENTRIES * ENTRY_BYTES;
pub const PAGE: u64 = 4096;
pub const LARGE_BYTES: u64 = 2 << 20;
/// The leaf's G bit, in [`Record::frame`] (the value of `cache::GLOBAL`, so the flags a hit gives are the walk's).
pub const GLOBAL: u64 = 8;
/// The flag a 2 MiB leaf's result carries (`cache::LARGE`).
pub const LARGE: u64 = 32;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Record {
    /// [`key`]; meaningful only while `epoch` is not 0.
    pub key: u64,
    /// The `Control::epoch` of the walk; 0: empty (an epoch is at least 1, `cache::reset`), which never hits.
    pub epoch: u64,
    /// The leaf's guest-physical base (4 KiB or 2 MiB aligned) with the leaf's G bit in [`GLOBAL`].
    pub frame: u64,
    pub _pad: u64,
}
const _: () = assert!(core::mem::size_of::<Record>() == ENTRY_BYTES);

/// The key of a record: the linear leaf base's page number << 2, | `large` << 1, | `user` (the privilege class of the walk:
/// CPL 3). The base is the page for a 4 KiB leaf, the first page of its 2 MiB region for a 2 MiB one.
pub const fn key(va: u64, large: bool, user: bool) -> u64 {
    (base(va, large) >> 12) << 2 | (large as u64) << 1 | user as u64
}
/// `va`'s leaf base for a leaf of the size.
pub const fn base(va: u64, large: bool) -> u64 {
    va & !(if large { LARGE_BYTES } else { PAGE } - 1)
}
pub const fn key_large(key: u64) -> bool {
    key & 2 != 0
}
pub const fn key_user(key: u64) -> bool {
    key & 1 != 0
}
/// The linear base a key names.
pub const fn key_base(key: u64) -> u64 {
    (key >> 2) << 12
}
/// The slot of a key: a multiplicative hash, the top [`ENTRY_BITS`] bits.
pub const fn index(key: u64) -> usize {
    (key.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> (64 - ENTRY_BITS)) as usize
}

/// A fetch walk's result as the record holds it: the 4 KiB page `gpa` is in (the walk's answer, as `fetch_walk` returns it)
/// and the leaf's flags (`GLOBAL`, `LARGE`; the `cache` values), for a leaf of 4 KiB or 2 MiB.
pub const fn leaf_frame(gpa_page: u64, flags: u64) -> u64 {
    let large = flags & LARGE != 0;
    base(gpa_page, large) | flags & GLOBAL
}
/// A new record for the walk of `va` under the class at epoch `epoch`, found with `flags`, whose page is in `gpa_page`.
pub const fn record(va: u64, gpa_page: u64, flags: u64, user: bool, epoch: u64) -> Record {
    Record {
        key: key(va, flags & LARGE != 0, user),
        epoch,
        frame: leaf_frame(gpa_page, flags),
        _pad: 0,
    }
}

/// The RAM rule (plan 3.1): a 4 KiB leaf is recorded only if its page is RAM (the walk already requires it); a 2 MiB leaf
/// only if its whole 2 MiB lies in one RAM region, else a record made from the RAM part would later answer a fetch in the
/// non-RAM part with a frame (the typed stop `no_code fetch_not_ram` would become a host error on the copy).
pub fn recordable(ram: &GuestRam, leaf_base: u64, large: bool) -> bool {
    ram.range(leaf_base, if large { LARGE_BYTES } else { PAGE })
        .is_some()
}

/// Whether `r` answers for `key` at the vCPU's `epoch`/`global_epoch` with CR4.PGE `pge`: its epoch is the present one, or
/// the leaf is global, PGE is set and no global flush has come since (the rule of `cache::current` for a global block).
pub const fn hits(r: &Record, key: u64, epoch: u64, global_epoch: u64, pge: bool) -> bool {
    r.epoch != 0
        && r.key == key
        && (r.epoch == epoch || (r.frame & GLOBAL != 0 && pge && r.epoch >= global_epoch))
}

/// What a hit says: the 4 KiB page `va` is fetched from and the leaf's flags.
pub const fn answer(r: &Record, va: u64) -> (u64, u64) {
    let large = key_large(r.key);
    let page = (r.frame & !(PAGE - 1)) | if large { va & (LARGE_BYTES - 1) & !(PAGE - 1) } else { 0 };
    (page, (r.frame & GLOBAL) | if large { LARGE } else { 0 })
}

/// The table's answer for `va` under `user`: the 4 KiB key's record, else the 2 MiB key's (plan 3.2): `(page, flags,
/// large)`.
pub fn probe(
    table: &[Record],
    va: u64,
    user: bool,
    epoch: u64,
    global_epoch: u64,
    pge: bool,
) -> Option<(u64, u64, bool)> {
    for large in [false, true] {
        let k = key(va, large, user);
        let r = &table[index(k)];
        if hits(r, k, epoch, global_epoch, pge) {
            let (page, flags) = answer(r, va);
            return Some((page, flags, large));
        }
    }
    None
}

/// Stores `r` in its slot; the record it replaced if that was a different key still live at `epoch` (an eviction).
pub fn insert(table: &mut [Record], r: Record, epoch: u64, global_epoch: u64, pge: bool) -> Option<Record> {
    let slot = &mut table[index(r.key)];
    let old = *slot;
    *slot = r;
    (old.key != r.key && hits(&old, old.key, epoch, global_epoch, pge)).then_some(old)
}

/// INVLPG or a #PF at `va`, at any CPL: the 4 KiB record of its page and the 2 MiB record of its region, both privilege
/// classes (plan 3.3). Returns how many were cleared.
pub fn invalidate(table: &mut [Record], va: u64) -> u64 {
    let mut cleared = 0;
    for large in [false, true] {
        for user in [false, true] {
            let k = key(va, large, user);
            let r = &mut table[index(k)];
            if r.epoch != 0 && r.key == k {
                r.epoch = 0;
                cleared += 1;
            }
        }
    }
    cleared
}

/// The callers of a fetch walk, by index into the counters: a block's revalidation (`lookup`) and a placement (a translation
/// request's walk of its pages).
pub const REVAL: usize = 0;
pub const PLACE: usize = 1;
/// [`Counts::class_calls`]'s classes: the walk found a global leaf, a 2 MiB leaf; the walk's privilege class is user (CPL 3).
pub const C_GLOBAL: usize = 0;
pub const C_LARGE: usize = 1;
pub const C_USER: usize = 2;
/// [`Counts::blocks`]'s classes: every revalidated block; those whose flags came out global / large; whose context is
/// user (CPL 3); and those over two pages.
pub const B_ALL: usize = 0;
pub const B_GLOBAL: usize = 1;
pub const B_LARGE: usize = 2;
pub const B_USER: usize = 3;
pub const B_CROSSING: usize = 4;
pub const B_CLASSES: usize = 5;

/// The study's counters (`Control::page_study`; written by a `page-study` resident only, all zero otherwise). Indexed by
/// caller ([`REVAL`], [`PLACE`]) where an array has two entries of the first rank.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Counts {
    /// Fetch walks made, by caller (each probed the record first).
    pub calls: [u64; 2],
    /// Calls the record would have answered, by the key's granule (4 KiB, 2 MiB), and the same by class: the walk's
    /// leaf was global / large, the walk's class was user ([`C_GLOBAL`]..), by caller.
    pub hit_4k: [u64; 2],
    pub hit_2m: [u64; 2],
    pub class_calls: [[u64; 3]; 2],
    pub class_hits: [[u64; 3]; 2],
    /// Walks that faulted, and of those the ones ending in `fetch_not_ram`, by caller.
    pub faulted: [u64; 2],
    pub not_ram: [u64; 2],
    /// Answers that differed from the walk, by caller; by kind: another frame, the same frame and other flags (size or G),
    /// the walk faulted where the record answered.
    pub disagree: [u64; 2],
    pub disagree_frame: u64,
    pub disagree_flags: u64,
    pub disagree_fault: u64,
    /// Records made; live records another key's record replaced; 2 MiB leaves not recorded (not inside one RAM region);
    /// INVLPGs and #PFs that cleared at least one record, and the records cleared.
    pub records: u64,
    pub evictions: u64,
    pub split_2m: u64,
    pub clear_events: u64,
    pub cleared: u64,
    /// The most records made in one epoch that were not replaced by another key's before (a lower bound of the live
    /// records), the present epoch's count and the epoch it counts (working fields).
    pub live_max: u64,
    pub epoch_records: u64,
    pub epoch_of_count: u64,
    /// CNTVCT ticks and counts of the probes (all calls), and of the walks by caller and by whether the probe would have
    /// hit (index 1) or not (0).
    pub probe_ticks: u64,
    pub walk_ticks: [[u64; 2]; 2],
    pub walk_count: [[u64; 2]; 2],
    /// Revalidated blocks by class ([`B_ALL`]..), and of them the ones whose every page the record would have answered (no
    /// walk needed). `block_all_hit` is the working flag of the block being revalidated.
    pub blocks: [u64; B_CLASSES],
    pub blocks_hit: [u64; B_CLASSES],
    pub block_all_hit: u64,
    /// The cost of reading the counter twice in a row (CNTVCT ticks over `tick_pairs` pairs), measured at start-up.
    pub tick_pair_ticks: u64,
    pub tick_pairs: u64,
}

#[cfg(test)]
mod tests {
    extern crate std;
    use super::*;
    use crate::execution::guest_ram;
    use std::vec::Vec;

    const V: u64 = 0x7f12_3456_7000;
    fn table() -> Vec<Record> {
        std::vec![Record::default(); ENTRIES]
    }
    fn put(t: &mut [Record], va: u64, gpa: u64, flags: u64, user: bool, epoch: u64) {
        let r = record(va, gpa, flags, user, epoch);
        insert(t, r, epoch, 1, false);
    }

    #[test]
    fn key_packs_page_granule_and_class() {
        let k = key(V + 0x123, false, true);
        assert_eq!(k, (V >> 12) << 2 | 1);
        assert_eq!(key_base(k), V);
        assert!(!key_large(k) && key_user(k));
        // A 2 MiB key names the region's first page, whatever the address inside it.
        let region = V & !(LARGE_BYTES - 1);
        let kl = key(V, true, false);
        assert_eq!(kl, (region >> 12) << 2 | 2);
        assert_eq!(key(region + LARGE_BYTES - 1, true, false), kl);
        assert_eq!(key_base(kl), region);
        assert!(key_large(kl) && !key_user(kl));
        // The four keys of one address differ.
        let keys = [(false, false), (false, true), (true, false), (true, true)].map(|(l, u)| key(V, l, u));
        for i in 0..4 {
            for j in 0..i {
                assert_ne!(keys[i], keys[j]);
            }
        }
        // Both halves of the canonical space stay distinct.
        assert_ne!(key(0xffff_8000_0000_1000, false, false), key(0x0000_8000_0000_1000, false, false));
    }

    #[test]
    fn index_stays_in_the_table() {
        for n in 0..100_000u64 {
            assert!(index(key(n << 12, n % 3 == 0, n % 2 == 0)) < ENTRIES);
        }
        assert_eq!(index(0), 0);
        assert_eq!(BYTES, 256 << 10);
    }

    #[test]
    fn granule_and_offset_arithmetic() {
        let mut t = table();
        // A 2 MiB leaf at guest-physical 6 MiB found for the fetch of region + 0x5123: the walk's page is 6 MiB + 0x5000.
        let region = 0x7f00_0040_0000u64;
        let gpa = 6 << 20;
        put(&mut t, region + 0x5123, gpa + 0x5000, LARGE | GLOBAL, false, 4);
        // Any page of the region is answered from the one record, with that page's offset.
        let (page, flags, large) = probe(&t, region + 0x1f_f000, false, 4, 1, false).unwrap();
        assert_eq!((page, flags, large), (gpa + 0x1f_f000, LARGE | GLOBAL, true));
        let (page, _, _) = probe(&t, region, false, 4, 1, false).unwrap();
        assert_eq!(page, gpa);
        // The next region is not covered; the other class does not hit.
        assert_eq!(probe(&t, region + LARGE_BYTES, false, 4, 1, false), None);
        assert_eq!(probe(&t, region, true, 4, 1, false), None);
        // A 4 KiB leaf answers its page alone, with its frame.
        put(&mut t, V + 0x77, 0x9000 + 0x5000, 0, false, 4);
        assert_eq!(probe(&t, V, false, 4, 1, false), Some((0xe000, 0, false)));
        assert_eq!(probe(&t, V + 0x1000, false, 4, 1, false), None);
    }

    #[test]
    fn a_large_record_never_hits_a_small_key() {
        let mut t = table();
        let region = 0x7f00_0040_0000u64;
        put(&mut t, region, 6 << 20, LARGE, false, 3);
        // The record's key is the 2 MiB key; the 4 KiB key of the region's first page is another key (same page number
        // bits, different granule bit), so it never matches that record even if the hash put them in one slot.
        let small = key(region, false, false);
        let r = &t[index(key(region, true, false))];
        assert!(!hits(r, small, 3, 1, false));
        let mut forced = table();
        forced[index(small)] = *r;
        assert_eq!(probe(&forced, region, false, 3, 1, false), None);
        // And a 4 KiB record never answers the 2 MiB key.
        let mut t = table();
        put(&mut t, region, 0x5000, 0, false, 3);
        let large_key = key(region, true, false);
        assert!(!hits(&t[index(small)], large_key, 3, 1, false));
        assert_eq!(probe(&t, region + 0x1000, false, 3, 1, false), None);
    }

    #[test]
    fn epoch_rule_empty_records_never_hit() {
        let k = key(V, false, false);
        let empty = Record::default();
        for epoch in [0, 1, 2, u64::MAX] {
            assert!(!hits(&empty, k, epoch, 1, true), "an empty record never hits (epoch {epoch})");
        }
        // Even one whose key matches and epoch is 0 (a cleared record): still empty.
        let cleared = Record { key: k, epoch: 0, frame: GLOBAL | 0x5000, _pad: 0 };
        assert!(!hits(&cleared, k, 0, 0, true));
        let r = record(V, 0x5000, 0, false, 7);
        assert!(hits(&r, k, 7, 1, false));
        assert!(!hits(&r, k, 8, 1, false), "a CR3 load moved the epoch: not global, no hit");
        assert!(!hits(&r, k, 6, 1, false), "records are never from the future either");
        assert!(!hits(&r, key(V + 0x1000, false, false), 7, 1, false));
        assert!(!hits(&r, key(V, false, true), 7, 1, false), "the other privilege class");
    }

    #[test]
    fn global_record_survives_cr3_loads_not_global_flushes() {
        let k = key(V, false, false);
        let g = record(V, 0x5000, GLOBAL, false, 7);
        // A CR3 load moves `epoch` only.
        assert!(hits(&g, k, 8, 1, true));
        assert!(hits(&g, k, 100, 7, true), "made at the last global flush's epoch: kept");
        // A global flush: `global_epoch` = the new epoch, above the record's.
        assert!(!hits(&g, k, 9, 9, true));
        // CR4.PGE off: a global leaf is no longer global, so only its own epoch answers.
        assert!(!hits(&g, k, 8, 1, false));
        assert!(hits(&g, k, 7, 1, false));
        // A non-global record is never kept past its epoch, whatever PGE says.
        let n = record(V, 0x5000, 0, false, 7);
        assert!(!hits(&n, k, 8, 1, true));
    }

    #[test]
    fn invalidation_clears_both_classes_and_both_sizes() {
        let region = 0x7f00_0040_0000u64;
        let mut t = table();
        put(&mut t, region + 0x3000, 0x2000, 0, false, 5); // 4 KiB, supervisor
        put(&mut t, region + 0x3000, 0x2000, 0, true, 5); // 4 KiB, user
        put(&mut t, region + 0x7000, 0x4000, 0, true, 5); // another page: stays
        put(&mut t, region, 6 << 20, LARGE, false, 5); // the region's 2 MiB leaf, supervisor
        put(&mut t, region, 6 << 20, LARGE, true, 5); // user
        let (e, g, p) = (5, 1, false);
        assert!(probe(&t, region + 0x3000, false, e, g, p).is_some());
        // An INVLPG of one page of the large leaf clears the leaf's records of both classes, the page's own two, not others.
        assert_eq!(invalidate(&mut t, region + 0x3123), 4);
        for user in [false, true] {
            assert_eq!(probe(&t, region + 0x3000, user, e, g, p), None);
            assert_eq!(probe(&t, region + 0x9000, user, e, g, p), None, "the 2 MiB record is gone for every page");
        }
        assert_eq!(probe(&t, region + 0x7000, true, e, g, p), Some((0x4000, 0, false)));
        assert_eq!(invalidate(&mut t, region + 0x3123), 0);
        // Only the INVLPG's class is not enough: one class alone.
        put(&mut t, V, 0x5000, 0, true, 5);
        assert_eq!(invalidate(&mut t, V), 1);
        assert_eq!(probe(&t, V, true, e, g, p), None);
    }

    #[test]
    fn eviction_counts_live_records_of_other_keys_only() {
        let mut t = table();
        let a = record(V, 0x5000, 0, false, 5);
        assert_eq!(insert(&mut t, a, 5, 1, false), None);
        // The same key again: a refresh, no eviction.
        assert_eq!(insert(&mut t, a, 5, 1, false), None);
        // Another key in the same slot: find one.
        let b = (1..)
            .map(|n| record(V + n * 0x1000, 0x6000, 0, false, 5))
            .find(|r| index(r.key) == index(a.key))
            .unwrap();
        assert_eq!(insert(&mut t, b, 5, 1, false), Some(a));
        // Over a record of an older, non-global epoch: not live, no eviction.
        let c = (1..)
            .map(|n| record(V + n * 0x1000, 0x7000, 0, false, 6))
            .find(|r| index(r.key) == index(b.key) && r.key != b.key)
            .unwrap();
        assert_eq!(insert(&mut t, c, 6, 1, false), None);
    }

    #[test]
    fn ram_rule_for_two_mib_leaves() {
        // 1 GiB of RAM: regions [0, 0xa0000), [0x100000, 1 GiB) (the legacy hole between).
        let ram = unsafe { guest_ram(1 << 30, 0x1000_0000) };
        // A 4 KiB leaf: its page in RAM.
        assert!(recordable(&ram, 0x9f000, false));
        assert!(!recordable(&ram, 0xa0000, false), "the hole");
        assert!(recordable(&ram, 0x10_0000, false));
        // A 2 MiB leaf at guest-physical 0 straddles the legacy hole: not recorded; one inside the second region is.
        assert!(!recordable(&ram, 0, true));
        assert!(recordable(&ram, 2 << 20, true));
        assert!(recordable(&ram, (1 << 30) - LARGE_BYTES, true));
        // Across the end of RAM, or wholly past it.
        assert!(!recordable(&ram, 1 << 30, true));
        // RAM above 3 GiB resumes at 4 GiB: a 2 MiB leaf across the PC hole's edge is not inside one region.
        let big = unsafe { guest_ram(4 << 30, 0x1000_0000) };
        assert!(recordable(&big, (3 << 30) - LARGE_BYTES, true));
        assert!(!recordable(&big, 3 << 30, true), "inside the 3-4 GiB hole");
        assert!(recordable(&big, 4 << 30, true));
    }
}
