//! Native stage-1 shadows of guest linear pages in an owner's private
//! translation tables (4 KiB granule, 48-bit VA, separate from guest
//! translation). TTBR0 maps the guest lower canonical half (below 2^47) beside
//! the resident's private region at L0 index 256; TTBR1 maps the upper half.
//! The x86 non-canonical gap is never mapped. Tables live in the owner's 1 MiB
//! window: the host pre-links the lower and upper 2 MiB windows at fixed pages,
//! and further intermediate tables come from a bounded pool.
pub const HIGH_BASE: u64 = 0xffff_8000_0000_0000;
pub const WINDOW_BYTES: u64 = 0x20_0000;
pub const LOWER_ROOT: usize = 0x10000;
pub const LOWER_LEAF: usize = 0x16000;
pub const UPPER_ROOT: usize = 0x17000;
pub const UPPER_L1: usize = 0x18000;
pub const UPPER_L2: usize = 0x19000;
pub const UPPER_LEAF: usize = 0x1a000;
/// Pool allocation count (one u64) in its own private page.
pub const STATE_OFFSET: usize = 0x1b000;
/// Intermediate/leaf table pages allocated on demand; never freed except by `reset`.
pub const POOL_OFFSET: usize = 0xe0000;
pub const POOL_PAGES: usize = 32;
pub const TCR_BOTH: u64 =
    16 | 1 << 8 | 1 << 10 | 3 << 12 | 16 << 16 | 1 << 24 | 1 << 26 | 3 << 28 | 2 << 30 | 2 << 32;
/// Valid page, AF, inner shareable, normal memory (MAIR index 0), UXN and PXN:
/// generated code never executes from guest pages.
pub const PAGE: u64 = 3 | 1 << 10 | 3 << 8 | 3 << 53;
pub const READ_ONLY: u64 = 1 << 7;
const TABLE: u64 = 3;
const OUTPUT: u64 = 0x0000_ffff_ffff_f000;
const _: () = assert!(POOL_OFFSET + POOL_PAGES * 4096 <= crate::RAM_SIZE);
const _: () = assert!(crate::resident::TEXT_END <= POOL_OFFSET);

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
/// (VAE1/VMALLE1) suffices only while the table pages are walked by this CPU
/// alone (owner-private tables); tables shared between CPUs need the
/// inner-shareable variants (VAE1IS/VMALLE1IS).
pub trait Maintenance {
    /// Complete prior table stores for the walker (DSB), then ISB.
    fn publish(&mut self);
    /// Complete prior table stores, invalidate `va`'s cached translations,
    /// complete the invalidation, then ISB.
    fn invalidate(&mut self, va: u64);
    fn invalidate_all(&mut self);
}

/// The owner's private tables: window offset 0 is at resident address
/// `window` and at IPA `ipa`. `upper` says whether TTBR1 walks are enabled
/// (TCR_EL1.EPD1 clear); without it the upper half and its tables are unused
/// and their pages need not be mapped.
pub struct Space<M> {
    pub window: *mut u8,
    pub ipa: u64,
    pub pool_pages: usize,
    pub upper: bool,
    pub tlb: M,
}
/// The pre-linked lower and upper shadow tables below the roots.
const LOWER_TABLES: [usize; 3] = [0x11000, 0x15000, LOWER_LEAF];
const UPPER_TABLES: [usize; 3] = [UPPER_L1, UPPER_L2, UPPER_LEAF];
/// Window pages holding the owner's tables and shadow state: never a target.
const PRIVATE_TABLES: core::ops::Range<usize> = LOWER_ROOT..STATE_OFFSET + 4096;

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
    fn allocated(&self) -> usize {
        self.get(STATE_OFFSET, 0) as usize
    }
    /// Table pages taken from the pool.
    pub fn used(&self) -> usize {
        self.allocated()
    }
    fn allocate(&mut self) -> Result<usize, Error> {
        let used = self.allocated();
        if used >= self.pool_pages {
            return Err(Error::Capacity);
        }
        let table = POOL_OFFSET + used * 4096;
        for index in 0..512 {
            self.set(table, index, 0);
        }
        self.set(STATE_OFFSET, 0, used as u64 + 1);
        Ok(table)
    }
    fn root(&self, va: u64) -> Result<usize, Error> {
        if va >> 47 == 0 {
            Ok(LOWER_ROOT)
        } else if va >= HIGH_BASE && self.upper {
            Ok(UPPER_ROOT)
        } else {
            Err(Error::Noncanonical)
        }
    }
    /// Guest halves as (root, L0 indices); the private region (lower index
    /// 256) and the non-canonical gap are never included.
    fn halves(&self) -> impl Iterator<Item = (usize, core::ops::Range<usize>)> + use<M> {
        [(LOWER_ROOT, 0..256), (UPPER_ROOT, 256..512)]
            .into_iter()
            .take(if self.upper { 2 } else { 1 })
    }
    fn pool(&self, offset: usize, pages: usize) -> bool {
        offset
            .checked_sub(POOL_OFFSET)
            .is_some_and(|o| o / 4096 < pages)
    }
    /// The table a descriptor links to, `None` when invalid. Only shadow
    /// table pages qualify, so a corrupt link never reaches private pages.
    fn next(&self, descriptor: u64) -> Result<Option<usize>, Error> {
        if descriptor & 1 == 0 {
            return Ok(None);
        }
        let offset = (descriptor & OUTPUT).wrapping_sub(self.ipa) as usize;
        let table = LOWER_TABLES.contains(&offset)
            || (self.upper && UPPER_TABLES.contains(&offset))
            || self.pool(offset, self.allocated());
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
                    let next = self.allocate()?;
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
        if va & 4095 != 0 || target & !OUTPUT != 0 {
            return Err(Error::Misaligned);
        }
        let own = target.wrapping_sub(self.ipa) as usize;
        if PRIVATE_TABLES.contains(&own) || self.pool(own, POOL_PAGES) {
            return Err(Error::Private);
        }
        let (table, index) = self.leaf(va, true)?.ok_or(Error::Corrupt)?;
        let new = target | PAGE | if writable { 0 } else { READ_ONLY };
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
    /// The leaf descriptor shadowing `va` (0 when none).
    pub fn descriptor(&mut self, va: u64) -> u64 {
        match self.leaf(va & !4095, false) {
            Ok(Some((table, index))) => self.get(table, index),
            _ => 0,
        }
    }
    /// Remove every shadow under the enabled roots; tables stay linked for
    /// reuse. Leaves cleared before a corrupt link are still invalidated.
    pub fn clear(&mut self) -> Result<(), Error> {
        let result = self.halves().try_for_each(|(root, indices)| {
            indices
                .into_iter()
                .try_for_each(|index| self.clear_table(self.get(root, index), 1))
        });
        self.tlb.invalidate_all();
        result
    }
    fn clear_table(&mut self, descriptor: u64, level: usize) -> Result<(), Error> {
        let Some(table) = self.next(descriptor)? else {
            return Ok(());
        };
        for index in 0..512 {
            if level == 3 {
                self.set(table, index, 0);
            } else {
                self.clear_table(self.get(table, index), level + 1)?;
            }
        }
        Ok(())
    }
    /// Evict everything: unlink both guest halves and return the pool. Tables
    /// the host pre-linked are dropped too; later maps allocate from the pool.
    pub fn reset(&mut self) {
        for (root, indices) in self.halves() {
            for index in indices {
                self.set(root, index, 0);
            }
        }
        self.set(STATE_OFFSET, 0, 0);
        self.tlb.invalidate_all();
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
    fn space(memory: &mut Vec<u64>, pool_pages: usize) -> Space<Log> {
        Space {
            window: memory.as_mut_ptr().cast(),
            ipa: IPA,
            pool_pages,
            upper: true,
            tlb: Log::default(),
        }
    }
    fn window() -> Vec<u64> {
        vec![0; crate::RAM_SIZE / 8]
    }
    fn word(memory: &[u64], offset: usize) -> u64 {
        memory[offset / 8]
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
        assert_eq!(s.descriptor(0x10_0000), 0x4007_0000 | PAGE | READ_ONLY);
        assert_eq!(s.descriptor(HIGH_BASE + 0x10_0abc), 0x4007_0000 | PAGE);
        assert_eq!(s.descriptor(0x10_2000), 0);
        drop(s);
        // Lower: root[0] -> pool 0 -> pool 1 -> pool 2; upper: root[256] -> pool 3..5.
        let table = |page: usize| (IPA + (POOL_OFFSET + page * 4096) as u64) | 3;
        assert_eq!(word(&m, LOWER_ROOT), table(0));
        assert_eq!(word(&m, POOL_OFFSET), table(1));
        assert_eq!(word(&m, POOL_OFFSET + 4096), table(2));
        assert_eq!(
            word(&m, POOL_OFFSET + 2 * 4096 + 256 * 8),
            0x4007_0000 | PAGE | READ_ONLY
        );
        assert_eq!(word(&m, UPPER_ROOT + 256 * 8), table(3));
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
        s.map(crate::DATA_VA, 0x4007_0000, true).unwrap();
        s.map(HIGH_BASE + crate::DATA_VA, 0x4007_0000, false)
            .unwrap();
        assert_eq!(s.map(0x20_0000, 0x4007_0000, false), Err(Error::Capacity));
        drop(s);
        assert_eq!(word(&m, LOWER_LEAF + 256 * 8), 0x4007_0000 | PAGE);
        assert_eq!(
            word(&m, UPPER_LEAF + 256 * 8),
            0x4007_0000 | PAGE | READ_ONLY
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
        assert_eq!(s.descriptor(0), 0x4000_0000 | PAGE);
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
        assert_eq!(s.descriptor(va), 0x4007_0000 | PAGE);
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
    }

    #[test]
    fn clear_drops_every_leaf_but_keeps_tables() {
        let mut m = window();
        let mut s = space(&mut m, POOL_PAGES);
        for va in [0, 0x10_0000, 0x40_0000_0000, HIGH_BASE, u64::MAX & !4095] {
            s.map(va, 0x4000_0000, true).unwrap();
        }
        let used = s.used();
        s.clear().unwrap();
        assert_eq!(s.tlb.0.last(), Some(&ALL));
        for va in [0, 0x10_0000, 0x40_0000_0000, HIGH_BASE, u64::MAX & !4095] {
            assert_eq!(s.descriptor(va), 0);
            s.map(va, 0x4000_0000, true).unwrap();
        }
        assert_eq!(s.used(), used);
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
        s.clear().unwrap();
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
            // clear() stops at the corrupt link but still invalidates.
            assert_eq!(s.clear(), Err(Error::Corrupt));
            assert_eq!(s.tlb.0.last(), Some(&ALL));
        }
        let mut s = space(&mut m, POOL_PAGES);
        for own in [LOWER_ROOT, 0x14000, STATE_OFFSET, POOL_OFFSET + 31 * 4096] {
            let target = IPA + own as u64;
            assert_eq!(s.map(0x20_0000, target, false), Err(Error::Private));
        }
        s.map(0x20_0000, IPA + crate::DATA_OFFSET as u64, true)
            .unwrap_or_else(|e| panic!("{e:?}"));
    }
}
