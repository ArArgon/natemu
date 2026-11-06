//! Native stage-1 shadows of guest linear pages in an owner's private
//! translation tables (4 KiB granule, 48-bit VA, separate from guest
//! translation). TTBR0 maps the guest lower canonical half (below 2^47) beside
//! the resident's private region at L0 index 256; TTBR1 maps the upper half.
//! The x86 non-canonical gap is never mapped. Tables live in the owner's 2 MiB
//! window: the host pre-links the lower and upper 2 MiB windows at fixed pages,
//! and further intermediate tables come from a bounded pool.
//!
//! Each owner has two shadow spaces, one per x86 privilege class: supervisor
//! (CPL 0-2; roots [`LOWER_ROOT`]/[`UPPER_ROOT`], the host's pre-linked windows)
//! and user (CPL 3; [`USER_LOWER_ROOT`]/[`USER_UPPER_ROOT`]), each with its own
//! ASID. A space shadows only accesses the software walker granted at its
//! privilege, so a supervisor shadow never serves a CPL3 access and a CPL change
//! switches roots instead of dropping shadows. Both spaces share the pool;
//! [`Space::reset`] empties both, and unmapping is per space (callers unmap a
//! page in both, as for INVLPG).
//!
//! Perf chunk 14: a CR3 load without PCID keeps global translations (SDM Vol.3
//! 4.10.4.1), and so do the shadows ([`Space::reset_cr3`]): the supervisor
//! space's upper half (the "kept half", where a 64-bit kernel's global pages
//! live) survives a CR3 load while every shadow in it came from a global
//! translation ([`Space::map_global`]); any other shadow there makes the next
//! CR3 load a full reset. Its tables come from the top of the pool, every other
//! table from the bottom, so a CR3 load returns the bottom part alone.
pub const HIGH_BASE: u64 = 0xffff_8000_0000_0000;
pub const WINDOW_BYTES: u64 = 0x20_0000;
pub const LOWER_ROOT: usize = 0x10000;
pub const LOWER_LEAF: usize = 0x16000;
pub const UPPER_ROOT: usize = 0x17000;
pub const UPPER_L1: usize = 0x18000;
pub const UPPER_L2: usize = 0x19000;
pub const UPPER_LEAF: usize = 0x1a000;
/// Roots of the user (CPL 3) space; its TTBR0 root links the resident's private region like [`LOWER_ROOT`].
pub const USER_LOWER_ROOT: usize = 0x1c000;
pub const USER_UPPER_ROOT: usize = 0x1d000;
/// `[lower, upper]` roots of the supervisor (index 0) and user (index 1) spaces.
pub const ROOTS: [[usize; 2]; 2] = [[LOWER_ROOT, UPPER_ROOT], [USER_LOWER_ROOT, USER_UPPER_ROOT]];
/// Pool allocation counts in their own private page: word 0 the bottom of the pool, word 1 the top (the kept half's
/// tables); word 2 nonzero once the kept half holds a shadow of a non-global translation.
pub const STATE_OFFSET: usize = 0x1b000;
const BOTTOM: usize = 0;
const TOP: usize = 1;
const NOT_ALL_GLOBAL: usize = 2;
/// Intermediate/leaf table pages allocated on demand; never freed except by `reset` (a full pool resets: the vCPU's
/// `evictions`). M1: 288 pages, to the end of the window's 2 MiB (the execution mode's block owns it; the leaf maps it),
/// so the shadows of a kernel's working set (one leaf table per 2 MiB of linear addresses) span up to ~560 MiB.
pub const POOL_OFFSET: usize = 0xe0000;
pub const POOL_PAGES: usize = 288;
pub const TCR_BOTH: u64 =
    16 | 1 << 8 | 1 << 10 | 3 << 12 | 16 << 16 | 1 << 24 | 1 << 26 | 3 << 28 | 2 << 30 | 2 << 32;
/// Valid page, AF, inner shareable, normal memory (MAIR index 0), EL0 read/write (AP 01: generated code runs at EL0),
/// UXN and PXN: generated code never executes from guest pages. x86 privilege is not in the descriptor but in the space:
/// a shadow exists only for a data access the software walker granted at the space's CPL class (U/S, WP; SMEP concerns
/// fetches, which never use shadows), and generated code runs with the space of its CPL selected.
pub const PAGE: u64 = 3 | 1 << 10 | 3 << 8 | 1 << 6 | 3 << 53;
/// nG: the entry is tagged with the owner's ASID (every owner maps the same
/// addresses differently, so its entries must not match another owner's).
pub const NOT_GLOBAL: u64 = 1 << 11;
/// AP[2]: read-only at both levels (AP 11).
pub const READ_ONLY: u64 = 1 << 7;
const TABLE: u64 = 3;
const OUTPUT: u64 = 0x0000_ffff_ffff_f000;
const _: () = assert!(POOL_OFFSET + POOL_PAGES * 4096 <= 2 * crate::RAM_SIZE);
const _: () = assert!(crate::resident::TEXT_END <= POOL_OFFSET);
const _: () = assert!(STATE_OFFSET < USER_LOWER_ROOT && USER_UPPER_ROOT + 4096 <= crate::SHARED_OFFSET);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// In the x86 non-canonical gap (or the resident's private half).
    Noncanonical,
    Misaligned,
    /// The guest-physical page is not in the owner's mappable RAM.
    OutsideRam,
    /// No free table page: the caller evicts (`reset`) or contains.
    Capacity,
    /// A table descriptor does not link to one of the shadow table pages.
    Corrupt,
    /// The target is one of the owner's own table, state or pool pages.
    Private,
}

/// Translation-table maintenance on the owning CPU. Non-broadcast TLBI
/// (VAE1/ASIDE1) suffices only while the table pages are walked by this CPU
/// alone (owner-private tables); tables shared between CPUs need the
/// inner-shareable variants (VAE1IS/VMALLE1IS).
pub trait Maintenance {
    /// Complete prior table stores for the walker (DSB), then ISB.
    fn publish(&mut self);
    /// Complete prior table stores, invalidate `va`'s cached translations
    /// in this space (its ASID), complete the invalidation, then ISB.
    fn invalidate(&mut self, va: u64);
    /// The same for every translation of both of the owner's spaces.
    fn invalidate_all(&mut self);
}

/// The owner's private tables: window offset 0 is at resident address
/// `window` and at IPA `ipa`. `upper` says whether TTBR1 walks are enabled
/// (TCR_EL1.EPD1 clear); without it the upper half and its tables are unused
/// and their pages need not be mapped. `user` selects the space (CPL 3, else
/// supervisor) that `map`, `unmap` and `descriptor` work on; `tlb` names its ASID.
pub struct Space<M> {
    pub window: *mut u8,
    pub ipa: u64,
    pub pool_pages: usize,
    pub upper: bool,
    pub user: bool,
    pub tlb: M,
}
/// The pre-linked lower and upper shadow tables below the roots.
const LOWER_TABLES: [usize; 3] = [0x11000, 0x15000, LOWER_LEAF];
const UPPER_TABLES: [usize; 3] = [UPPER_L1, UPPER_L2, UPPER_LEAF];
/// Window pages holding the owner's tables and shadow state: never a target.
const PRIVATE_TABLES: core::ops::Range<usize> = LOWER_ROOT..USER_UPPER_ROOT + 4096;
/// G1: the frame page (the guest-state page, also reached by its EL0 alias) and the alias's tables: never a target.
const GUEST_STATE: [core::ops::Range<usize>; 2] = [
    crate::execution::FRAME_OFFSET..crate::execution::FRAME_OFFSET + 4096,
    crate::execution::GUEST_STATE_TABLES[0]..crate::execution::GUEST_STATE_TABLES[2] + 4096,
];

impl<M: Maintenance> Space<M> {
    fn slot(&self, table: usize, index: usize) -> *mut u64 {
        self.window.wrapping_add(table + index * 8).cast()
    }
    fn get(&self, table: usize, index: usize) -> u64 {
        unsafe { self.slot(table, index).read_volatile() }
    }
    fn set(&self, table: usize, index: usize, value: u64) {
        unsafe { self.slot(table, index).write_volatile(value) }
    }
    fn count(&self, end: usize) -> usize {
        self.get(STATE_OFFSET, end) as usize
    }
    /// Table pages taken from the pool.
    pub fn used(&self) -> usize {
        self.count(BOTTOM) + self.count(TOP)
    }
    /// Whether `va`'s tables are the kept half's: the supervisor space's upper half.
    fn kept(&self, va: u64) -> bool {
        !self.user && va >> 47 != 0
    }
    /// A zeroed table page from the pool: from its top for the kept half, else from its bottom.
    fn allocate(&mut self, kept: bool) -> Result<usize, Error> {
        let (bottom, top) = (self.count(BOTTOM), self.count(TOP));
        if bottom.saturating_add(top) >= self.pool_pages {
            return Err(Error::Capacity);
        }
        let (table, end, used) = match kept {
            true => (POOL_OFFSET + (self.pool_pages - 1 - top) * 4096, TOP, top),
            false => (POOL_OFFSET + bottom * 4096, BOTTOM, bottom),
        };
        for index in 0..512 {
            self.set(table, index, 0);
        }
        self.set(STATE_OFFSET, end, used as u64 + 1);
        Ok(table)
    }
    fn root(&self, va: u64) -> Result<usize, Error> {
        let [lower, upper] = ROOTS[self.user as usize];
        if va >> 47 == 0 {
            Ok(lower)
        } else if va >= HIGH_BASE && self.upper {
            Ok(upper)
        } else {
            Err(Error::Noncanonical)
        }
    }
    /// Guest halves of both spaces as (root, L0 indices); the private region
    /// (lower index 256) and the non-canonical gap are never included.
    fn halves(&self) -> impl Iterator<Item = (usize, core::ops::Range<usize>)> + use<M> {
        let upper = self.upper;
        ROOTS.into_iter().flat_map(move |[lower, high]| {
            [(lower, 0..256), (high, 256..512)].into_iter().take(if upper { 2 } else { 1 })
        })
    }
    fn pool(&self, offset: usize, pages: usize) -> bool {
        offset
            .checked_sub(POOL_OFFSET)
            .is_some_and(|o| o / 4096 < pages)
    }
    /// Whether `offset` is an allocated pool table page (either end).
    fn allocated(&self, offset: usize) -> bool {
        offset.checked_sub(POOL_OFFSET).map(|o| o / 4096).is_some_and(|page| {
            page < self.count(BOTTOM) || (page < self.pool_pages && self.pool_pages.checked_sub(self.count(TOP)).is_some_and(|first| page >= first))
        })
    }
    /// The table a descriptor links to, `None` when invalid. Only shadow
    /// table pages qualify, so a corrupt link never reaches private pages.
    fn next(&self, descriptor: u64) -> Result<Option<usize>, Error> {
        if descriptor & 1 == 0 {
            return Ok(None);
        }
        let offset = (descriptor & OUTPUT).wrapping_sub(self.ipa) as usize;
        // The host pre-links its windows in the supervisor space only.
        let table = (!self.user && LOWER_TABLES.contains(&offset))
            || (!self.user && self.upper && UPPER_TABLES.contains(&offset))
            || self.allocated(offset);
        if descriptor & 3 != TABLE || !table {
            return Err(Error::Corrupt);
        }
        Ok(Some(offset))
    }
    /// `(table, index)` of `va`'s leaf entry; allocates missing tables when asked.
    fn leaf(&mut self, va: u64, allocate: bool) -> Result<Option<(usize, usize)>, Error> {
        let mut table = self.root(va)?;
        for level in 0..3 {
            let index = ((va >> (39 - 9 * level)) & 511) as usize;
            table = match self.next(self.get(table, index))? {
                Some(next) => next,
                None if allocate => {
                    let next = self.allocate(self.kept(va))?;
                    // The zeroed table is complete before the walker can reach it.
                    self.tlb.publish();
                    self.set(table, index, (self.ipa + next as u64) | TABLE);
                    next
                }
                None => return Ok(None),
            };
        }
        Ok(Some((table, ((va >> 12) & 511) as usize)))
    }

    /// Map guest linear page `va` to native page `target` (an IPA). A changed
    /// valid entry is broken first: store 0, invalidate `va`, then store the
    /// new entry (break-before-make); this covers read-only → writable.
    pub fn map(&mut self, va: u64, target: u64, writable: bool) -> Result<(), Error> {
        self.install(va, target, writable, false)
    }
    /// [`Space::map`] of a global translation (its leaf's G with CR4.PGE): in the kept half it survives a CR3 load.
    pub fn map_global(&mut self, va: u64, target: u64, writable: bool) -> Result<(), Error> {
        self.install(va, target, writable, true)
    }
    fn install(&mut self, va: u64, target: u64, writable: bool, global: bool) -> Result<(), Error> {
        if va & 4095 != 0 || target & !OUTPUT != 0 {
            return Err(Error::Misaligned);
        }
        let own = target.wrapping_sub(self.ipa) as usize;
        if PRIVATE_TABLES.contains(&own) || GUEST_STATE.iter().any(|r| r.contains(&own)) || self.pool(own, POOL_PAGES) {
            return Err(Error::Private);
        }
        let (table, index) = self.leaf(va, true)?.ok_or(Error::Corrupt)?;
        // Before the entry: a CR3 load that sees the entry sees the word too (one CPU, program order).
        if !global && self.kept(va) {
            self.set(STATE_OFFSET, NOT_ALL_GLOBAL, 1);
        }
        let new = target | PAGE | NOT_GLOBAL | if writable { 0 } else { READ_ONLY };
        let old = self.get(table, index);
        if old == new {
            return Ok(());
        }
        if old & 1 != 0 {
            self.set(table, index, 0);
            self.tlb.invalidate(va);
        }
        self.set(table, index, new);
        self.tlb.publish();
        Ok(())
    }
    /// Remove `va`'s shadow, if any, and invalidate it.
    pub fn unmap(&mut self, va: u64) -> Result<(), Error> {
        if let Some((table, index)) = self.leaf(va & !4095, false)? {
            if self.get(table, index) & 1 != 0 {
                self.set(table, index, 0);
                self.tlb.invalidate(va & !4095);
            }
        }
        Ok(())
    }
    /// Removes every shadow of the 2 MiB-aligned range holding `va` (its leaf table's entries: one walk) and invalidates
    /// each.
    pub fn unmap_large(&mut self, va: u64) -> Result<(), Error> {
        let start = va & !0x1f_ffff;
        if let Some((table, _)) = self.leaf(start, false)? {
            for index in 0..512 {
                if self.get(table, index) & 1 != 0 {
                    self.set(table, index, 0);
                    self.tlb.invalidate(start + 4096 * index as u64);
                }
            }
        }
        Ok(())
    }
    /// The leaf descriptor shadowing `va` (0 when none).
    pub fn descriptor(&mut self, va: u64) -> u64 {
        match self.leaf(va & !4095, false) {
            Ok(Some((table, index))) => self.get(table, index),
            _ => 0,
        }
    }
    /// Evict everything in both spaces: unlink every guest half and return the
    /// pool. Tables the host pre-linked are dropped too; later maps allocate
    /// from the pool.
    pub fn reset(&mut self) {
        for (root, indices) in self.halves() {
            for index in indices {
                self.set(root, index, 0);
            }
        }
        for word in [BOTTOM, TOP, NOT_ALL_GLOBAL] {
            self.set(STATE_OFFSET, word, 0);
        }
        self.tlb.invalidate_all();
    }
    /// A CR3 load (no PCID): every shadow goes but the kept half's while it holds global translations only (else
    /// everything goes, [`Space::reset`]); the bottom of the pool returns. Whether everything went.
    pub fn reset_cr3(&mut self) -> bool {
        if self.get(STATE_OFFSET, NOT_ALL_GLOBAL) != 0 || !self.upper {
            self.reset();
            return true;
        }
        let [lower, _] = ROOTS[0];
        let [user_lower, user_upper] = ROOTS[1];
        for (root, indices) in [(lower, 0..256), (user_lower, 0..256), (user_upper, 256..512)] {
            for index in indices {
                self.set(root, index, 0);
            }
        }
        self.set(STATE_OFFSET, BOTTOM, 0);
        self.tlb.invalidate_all();
        false
    }
}

#[cfg(test)]
mod tests {
    extern crate std;
    use super::*;
    use std::{vec, vec::Vec};

    /// Maintenance calls in order; `invalidate` also records the watched
    /// entry's value at that moment.
    #[derive(Default, Debug, PartialEq)]
    struct Log(Vec<u64>, Option<*const u64>);
    const ALL: u64 = u64::MAX;
    const PUBLISH: u64 = 1;
    impl Maintenance for Log {
        fn publish(&mut self) {
            self.0.push(PUBLISH);
        }
        fn invalidate(&mut self, va: u64) {
            self.0.push(va);
            if let Some(entry) = self.1 {
                self.0.push(unsafe { entry.read() });
            }
        }
        fn invalidate_all(&mut self) {
            self.0.push(ALL);
        }
    }
    const IPA: u64 = 0x4010_0000;
    /// Every shadow leaf: valid page, tagged with the owner's ASID.
    const LEAF: u64 = PAGE | NOT_GLOBAL;
    fn space(memory: &mut Vec<u64>, pool_pages: usize) -> Space<Log> {
        Space {
            window: memory.as_mut_ptr().cast(),
            ipa: IPA,
            pool_pages,
            upper: true,
            user: false,
            tlb: Log::default(),
        }
    }
    fn window() -> Vec<u64> {
        vec![0; 2 * crate::RAM_SIZE / 8]
    }
    fn word(memory: &[u64], offset: usize) -> u64 {
        memory[offset / 8]
    }

    /// K1: `unmap_large` removes and invalidates exactly the shadows of the 2 MiB region holding its address.
    #[test]
    fn unmap_large_clears_one_region() {
        let mut m = window();
        let mut s = space(&mut m, POOL_PAGES);
        for va in [0x20_0000, 0x20_3000, 0x3f_f000, 0x40_0000] {
            s.map(va, 0x4007_0000, false).unwrap();
        }
        s.tlb.0.clear();
        s.unmap_large(0x21_2345).unwrap();
        assert_eq!(s.tlb.0, vec![0x20_0000, 0x20_3000, 0x3f_f000]);
        assert_eq!([0x20_0000, 0x20_3000, 0x3f_f000].map(|va| s.descriptor(va)), [0; 3]);
        assert_ne!(s.descriptor(0x40_0000), 0);
        // A region with no leaf table: nothing to do.
        s.unmap_large(0x80_0000).unwrap();
        assert_eq!(s.tlb.0.len(), 3);
    }
    #[test]
    fn halves_allocate_tables_from_the_pool_and_gap_is_refused() {
        let mut m = window();
        let mut s = space(&mut m, POOL_PAGES);
        s.map(0x10_0000, 0x4007_0000, false).unwrap();
        assert_eq!(s.used(), 3);
        s.map(HIGH_BASE + 0x10_0000, 0x4007_0000, true).unwrap();
        assert_eq!(s.used(), 6);
        // Same 2 MiB region: no new table.
        s.map(0x10_1000, 0x4007_1000, true).unwrap();
        s.map(0x7fff_ffff_f000, 0x4007_2000, true).unwrap();
        assert_eq!(s.used(), 9);
        for va in [
            0x8000_0000_0000,
            crate::PRIVATE,
            HIGH_BASE - 4096,
            0xffff_0000_0000_0000,
        ] {
            assert_eq!(s.map(va, 0x4007_0000, true), Err(Error::Noncanonical));
            assert_eq!(s.descriptor(va), 0);
        }
        assert_eq!(s.map(0x10_0800, 0x4007_0000, true), Err(Error::Misaligned));
        assert_eq!(s.map(0x10_0000, 0x4007_0800, true), Err(Error::Misaligned));
        assert_eq!(s.descriptor(0x10_0000), 0x4007_0000 | LEAF | READ_ONLY);
        assert_eq!(s.descriptor(HIGH_BASE + 0x10_0abc), 0x4007_0000 | LEAF);
        assert_eq!(s.descriptor(0x10_2000), 0);
        drop(s);
        // Lower: root[0] -> pool 0 -> pool 1 -> pool 2; upper (the kept half): root[256] -> the pool's last three pages.
        let table = |page: usize| (IPA + (POOL_OFFSET + page * 4096) as u64) | 3;
        assert_eq!(word(&m, UPPER_ROOT + 256 * 8), table(POOL_PAGES - 1));
        assert_eq!(word(&m, POOL_OFFSET + (POOL_PAGES - 1) * 4096), table(POOL_PAGES - 2));
        assert_eq!(word(&m, LOWER_ROOT), table(0));
        assert_eq!(word(&m, POOL_OFFSET), table(1));
        assert_eq!(word(&m, POOL_OFFSET + 4096), table(2));
        assert_eq!(
            word(&m, POOL_OFFSET + 2 * 4096 + 256 * 8),
            0x4007_0000 | LEAF | READ_ONLY
        );
        // Nothing reaches the private half or the gap.
        assert_eq!(word(&m, LOWER_ROOT + 256 * 8), 0);
        assert!((0..256).all(|i| word(&m, UPPER_ROOT + i * 8) == 0));
    }

    #[test]
    fn host_prelinked_windows_are_reused() {
        let mut m = window();
        let link = |m: &mut Vec<u64>, at: usize, to: usize| m[at / 8] = (IPA + to as u64) | 3;
        link(&mut m, LOWER_ROOT, 0x11000);
        link(&mut m, 0x11000, 0x15000);
        link(&mut m, 0x15000, LOWER_LEAF);
        link(&mut m, UPPER_ROOT + 256 * 8, UPPER_L1);
        link(&mut m, UPPER_L1, UPPER_L2);
        link(&mut m, UPPER_L2, UPPER_LEAF);
        let mut s = space(&mut m, 0);
        s.map(0x10_0000, 0x4007_0000, true).unwrap();
        s.map(HIGH_BASE + 0x10_0000, 0x4007_0000, false)
            .unwrap();
        assert_eq!(s.map(0x20_0000, 0x4007_0000, false), Err(Error::Capacity));
        drop(s);
        assert_eq!(word(&m, LOWER_LEAF + 256 * 8), 0x4007_0000 | LEAF);
        assert_eq!(
            word(&m, UPPER_LEAF + 256 * 8),
            0x4007_0000 | LEAF | READ_ONLY
        );
    }

    #[test]
    fn capacity_exhaustion_is_reported_and_reset_returns_the_pool() {
        let mut m = window();
        let mut s = space(&mut m, 4);
        s.map(0, 0x4000_0000, true).unwrap();
        // Same L1 table, new L2/leaf: needs two pages but one remains.
        assert_eq!(s.map(0x4000_0000, 0x4000_0000, true), Err(Error::Capacity));
        assert_eq!(s.used(), 4);
        // The partially linked path stays valid and needs no new page later.
        assert_eq!(s.map(0x4000_0000, 0x4000_0000, true), Err(Error::Capacity));
        assert_eq!(s.descriptor(0), 0x4000_0000 | LEAF);
        s.reset();
        assert_eq!((s.used(), s.descriptor(0)), (0, 0));
        assert_eq!(s.tlb.0.last(), Some(&ALL));
        s.map(0x4000_0000, 0x4000_0000, true).unwrap();
        assert_eq!(s.used(), 3);
    }

    #[test]
    fn upgrade_breaks_before_make_and_unmap_invalidates() {
        let mut m = window();
        let mut s = space(&mut m, POOL_PAGES);
        let va = HIGH_BASE + 0x10_0000;
        s.map(va, 0x4007_0000, false).unwrap();
        // First map of an invalid entry needs no invalidation.
        assert!(!s.tlb.0.contains(&va));
        s.tlb.0.clear();
        // Unchanged entry: no maintenance at all.
        s.map(va, 0x4007_0000, false).unwrap();
        assert!(s.tlb.0.is_empty());
        // Read-only -> writable: invalidate while the entry is 0, then publish.
        let (table, index) = s.leaf(va, false).unwrap().unwrap();
        s.tlb.1 = Some(s.slot(table, index));
        s.map(va, 0x4007_0000, true).unwrap();
        assert_eq!(s.tlb.0, [va, 0, PUBLISH]);
        assert_eq!(s.descriptor(va), 0x4007_0000 | LEAF);
        s.tlb.0.clear();
        s.unmap(va + 0x123).unwrap();
        assert_eq!(s.descriptor(va), 0);
        assert_eq!(s.tlb.0, [va, 0]);
        // Absent mappings and missing tables need nothing.
        s.tlb.0.clear();
        s.unmap(va).unwrap();
        s.unmap(0x7000_0000_0000).unwrap();
        assert!(s.tlb.0.is_empty());
        assert_eq!(s.unmap(0x8000_0000_0000), Err(Error::Noncanonical));
        // Every shadow is tagged with the owner's ASID.
        s.map(va, 0x4007_0000, false).unwrap();
        assert_eq!(s.descriptor(va), 0x4007_0000 | LEAF | READ_ONLY);
    }

    #[test]
    fn user_and_supervisor_spaces_are_separate_and_reset_together() {
        let mut m = window();
        let mut s = space(&mut m, POOL_PAGES);
        s.map(0x10_0000, 0x4007_0000, true).unwrap();
        s.user = true;
        // Nothing of the supervisor space shows in the user space, and the reverse.
        assert_eq!(s.descriptor(0x10_0000), 0);
        s.map(0x10_0000, 0x4008_0000, false).unwrap();
        s.map(HIGH_BASE + 0x20_0000, 0x4009_0000, true).unwrap();
        assert_eq!(s.descriptor(0x10_0000), 0x4008_0000 | LEAF | READ_ONLY);
        s.user = false;
        assert_eq!(s.descriptor(0x10_0000), 0x4007_0000 | LEAF);
        assert_eq!(s.descriptor(HIGH_BASE + 0x20_0000), 0);
        // One pool: 3 + 3 + 3 tables.
        assert_eq!(s.used(), 9);
        // A space's own table pages, roots included, are never targets.
        for own in [USER_LOWER_ROOT, USER_UPPER_ROOT] {
            assert_eq!(s.map(0x30_0000, IPA + own as u64, false), Err(Error::Private));
        }
        s.unmap(0x10_0000).unwrap();
        assert_eq!(s.descriptor(0x10_0000), 0);
        s.user = true;
        assert_eq!(s.descriptor(0x10_0000), 0x4008_0000 | LEAF | READ_ONLY);
        s.reset();
        assert_eq!((s.used(), s.descriptor(0x10_0000), s.descriptor(HIGH_BASE + 0x20_0000)), (0, 0, 0));
        drop(s);
        // Both spaces' guest halves are unlinked; the private region index stays.
        m[USER_LOWER_ROOT / 8 + 256] = 7;
        // G1: the guest-state alias's upper-root entry (below 256) stays in both spaces.
        let alias = (crate::execution::GUEST_STATE_VA >> 39 & 511) as usize;
        for [_, upper] in ROOTS {
            m[upper / 8 + alias] = 9;
        }
        let mut s = space(&mut m, POOL_PAGES);
        s.reset();
        drop(s);
        for [lower, upper] in ROOTS {
            assert!((0..256).all(|i| word(&m, lower + i * 8) == 0));
            assert!((256..512).all(|i| word(&m, upper + i * 8) == 0));
        }
        assert_eq!(word(&m, USER_LOWER_ROOT + 256 * 8), 7);
        assert!(ROOTS.iter().all(|&[_, upper]| word(&m, upper + alias * 8) == 9));
        // The host's pre-linked tables belong to the supervisor space: a user root linking one is corrupt.
        m[USER_LOWER_ROOT / 8] = (IPA + 0x11000) | 3;
        let mut s = space(&mut m, POOL_PAGES);
        s.user = true;
        assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
    }

    #[test]
    fn foreign_descriptor_is_corrupt_not_followed() {
        let mut m = window();
        m[LOWER_ROOT / 8] = 0x4000_0000 | 1; // block, as the boot identity alias
        let mut s = space(&mut m, POOL_PAGES);
        assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
        drop(s);
        m[LOWER_ROOT / 8] = (IPA + crate::RAM_SIZE as u64) | 3; // outside the window
        let mut s = space(&mut m, POOL_PAGES);
        assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
        assert_eq!(s.descriptor(0x1000), 0);
    }

    #[test]
    fn single_root_space_never_touches_the_upper_root() {
        let mut m = window();
        // An unmapped upper root would fault natively; poison it here instead.
        m[UPPER_ROOT / 8..(UPPER_LEAF + 4096) / 8].fill(u64::MAX);
        let mut s = space(&mut m, POOL_PAGES);
        s.upper = false;
        for va in [HIGH_BASE, u64::MAX & !4095] {
            assert_eq!(s.map(va, 0x4000_0000, true), Err(Error::Noncanonical));
            assert_eq!(s.unmap(va), Err(Error::Noncanonical));
            assert_eq!(s.descriptor(va), 0);
        }
        s.map(0x10_0000, 0x4000_0000, true).unwrap();
        s.reset();
        drop(s);
        assert!(
            m[UPPER_ROOT / 8..(UPPER_LEAF + 4096) / 8]
                .iter()
                .all(|&w| w == u64::MAX)
        );
        // Without the upper half, its pre-linked pages are not tables either.
        m[LOWER_ROOT / 8] = (IPA + UPPER_L1 as u64) | 3;
        let mut s = space(&mut m, POOL_PAGES);
        s.upper = false;
        assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
    }

    #[test]
    fn links_reach_only_shadow_tables_and_targets_exclude_own_tables() {
        let mut m = window();
        let mut s = space(&mut m, POOL_PAGES);
        s.map(0, 0x4000_0000, true).unwrap();
        drop(s);
        // A link into the private leaf, an unallocated pool page or a root.
        for to in [0x14000, POOL_OFFSET + 5 * 4096, UPPER_ROOT] {
            m[(POOL_OFFSET + 4096) / 8] = (IPA + to as u64) | 3;
            let mut s = space(&mut m, POOL_PAGES);
            assert_eq!(s.map(0, 0x4000_0000, true), Err(Error::Corrupt));
        }
        let mut s = space(&mut m, POOL_PAGES);
        // G1: also the frame page and the guest-state alias's tables.
        let [l1, _, leaf] = crate::execution::GUEST_STATE_TABLES;
        for own in [LOWER_ROOT, 0x14000, STATE_OFFSET, POOL_OFFSET + 31 * 4096, crate::execution::FRAME_OFFSET, l1, leaf] {
            let target = IPA + own as u64;
            assert_eq!(s.map(0x20_0000, target, false), Err(Error::Private));
        }
        s.map(0x20_0000, IPA + 0x7_0000, true)
            .unwrap_or_else(|e| panic!("{e:?}"));
    }

    /// Perf chunk 14: a CR3 load keeps the supervisor space's upper half while every shadow there is global, with its
    /// tables at the top of the pool; everything else goes and the bottom of the pool returns.
    #[test]
    fn cr3_reset_keeps_global_upper_shadows_only() {
        let mut m = window();
        let mut s = space(&mut m, POOL_PAGES);
        let kernel = HIGH_BASE + 0x10_0000;
        s.map_global(kernel, 0x4007_0000, true).unwrap();
        s.map_global(0x10_0000, 0x4008_0000, true).unwrap(); // global, but the lower half: not kept
        s.user = true;
        s.map(0x20_0000, 0x4009_0000, false).unwrap();
        s.map_global(HIGH_BASE + 0x40_0000, 0x400a_0000, false).unwrap(); // the user space's upper half: not kept
        s.user = false;
        assert_eq!(s.used(), 12);
        s.tlb.0.clear();
        assert!(!s.reset_cr3());
        assert_eq!(s.tlb.0, [ALL]);
        assert_eq!(s.used(), 3);
        assert_eq!(s.descriptor(kernel), 0x4007_0000 | LEAF);
        assert_eq!(s.descriptor(0x10_0000), 0);
        s.user = true;
        assert_eq!((s.descriptor(0x20_0000), s.descriptor(HIGH_BASE + 0x40_0000)), (0, 0));
        // The bottom is reused; it never meets the kept tables.
        s.map(0x20_0000, 0x4009_0000, false).unwrap();
        s.user = false;
        assert_eq!(s.used(), 6);
        assert_eq!(s.descriptor(kernel), 0x4007_0000 | LEAF);
        // An INVLPG reaches a kept shadow.
        s.map_global(kernel + 0x1000, 0x4007_1000, false).unwrap();
        s.unmap_large(kernel).unwrap();
        assert_eq!((s.descriptor(kernel), s.descriptor(kernel + 0x1000)), (0, 0));
        s.map_global(kernel, 0x4007_0000, true).unwrap();
        // A non-global shadow in the kept half: the next CR3 load resets everything, and the one after keeps again.
        s.map(HIGH_BASE + 0x80_0000, 0x400b_0000, false).unwrap();
        assert!(s.reset_cr3());
        assert_eq!((s.used(), s.descriptor(kernel)), (0, 0));
        s.map_global(kernel, 0x4007_0000, false).unwrap();
        assert!(!s.reset_cr3());
        assert_eq!(s.descriptor(kernel), 0x4007_0000 | LEAF | READ_ONLY);
        // A full reset returns the top of the pool too.
        assert_eq!(s.used(), 3);
        s.reset();
        assert_eq!((s.used(), s.descriptor(kernel)), (0, 0));
        // A full reset clears the mark too.
        s.map(HIGH_BASE + 0x80_0000, 0x400b_0000, false).unwrap();
        s.reset();
        s.map_global(kernel, 0x4007_0000, false).unwrap();
        assert!(!s.reset_cr3());
        // Without the upper half there is nothing to keep: a full reset.
        s.upper = false;
        assert!(s.reset_cr3());
    }
    /// Perf chunk 14: the two ends of the pool share its capacity, and a link into the gap between them is corrupt.
    #[test]
    fn pool_ends_share_capacity() {
        let mut m = window();
        let mut s = space(&mut m, 5);
        s.map_global(HIGH_BASE, 0x4000_0000, true).unwrap(); // 3 from the top
        assert_eq!(s.map(0, 0x4000_0000, true), Err(Error::Capacity)); // 2 of 3 from the bottom
        assert_eq!(s.used(), 5);
        assert!(!s.reset_cr3());
        assert_eq!(s.used(), 3);
        drop(s);
        // The bottom's pages went back at the reset: a link to one is corrupt.
        m[LOWER_ROOT / 8] = (IPA + POOL_OFFSET as u64) | 3;
        let mut s = space(&mut m, 5);
        assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
    }
}
