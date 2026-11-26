//! Native stage-1 shadows of guest linear pages in an owner's private
//! translation tables (4 KiB granule, 48-bit VA, separate from guest
//! translation). TTBR0 maps the guest lower canonical half (below 2^47) beside
//! the resident's private region at L0 index 256; TTBR1 maps the upper half.
//! The x86 non-canonical gap is never mapped. Roots, the host's pre-linked
//! tables and the shadow state live in the owner's 2 MiB window at fixed pages;
//! further intermediate and leaf tables come from a bounded pool of the owner's,
//! outside the window ([`POOL_OFFSET`]).
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
//! live) survives a CR3 load but for its shadows of non-global translations,
//! which the load removes alone (listed since the last reset, up to
//! `LIST_MAX`; more make the load a full reset, IO5). Its tables come from the
//! top of the pool, every other table from the bottom, so a CR3 load returns
//! the bottom part alone.
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
/// tables); word 2 nonzero once the kept half held a shadow of a non-global translation the list could not take;
/// word 3 the count and words 4.. the linear pages of the kept half's non-global shadows since the last reset.
pub const STATE_OFFSET: usize = 0x1b000;
const BOTTOM: usize = 0;
const TOP: usize = 1;
const NOT_ALL_GLOBAL: usize = 2;
const LISTED: usize = 3;
const LIST: usize = 4;
/// IO5 (November 13): Linux maps some of its direct map non-global (4 KiB, G clear, with CR4.PGE): under configure 80-97 k
/// such shadows a vCPU, a few per CR3 load, made 58-68% of the loads reset the kept half whole. Listed, a load removes
/// only them (what the load must drop); past this many it resets everything, as before.
const LIST_MAX: usize = 64;
/// Intermediate/leaf table pages allocated on demand; never freed except by `reset` (a full pool resets: the vCPU's
/// `evictions`). M1 put 288 pages (285 after checkpoint C's compat tables) at the end of the window's 2 MiB: a leaf table
/// per 2 MiB of linear addresses spans ~560 MiB when the working set is dense, but node's V8 (and SpiderMonkey) place
/// 256 KiB heap pages at random addresses, so its layout needs about 2,140 table pages (one sample: 1,283 distinct 2 MiB,
/// 728 distinct 1 GiB, 131 distinct 512 GiB regions), and a Web Tooling Benchmark run reset every shadow about 946 k
/// times. Since November 25 the pool has a reservation of its own: [`POOL_MAX_PAGES`] pages (64 MiB) in the vCPU's own
/// block of the backing right after the resident's text block, at the same offset of the private region as of the block
/// ([`POOL_OFFSET`] is both), mapped whole by the host as 2 MiB blocks, EL1 read/write, no EL0 access, never executable
/// (host `memory::private_space`). A vCPU allocates from its first `execution::cache::Control::shadow_pool_pages` pages
/// (the host's launch setting: the whole reservation unless `machine.shadow_pool_pages` caps it, [`pool_pages`]). Pages
/// are backed as allocated (each is zeroed when taken), so the reservation costs address space; the worst case, a guest
/// filling every vCPU's pool, is 64 MiB of host memory per vCPU.
pub const POOL_OFFSET: usize = crate::resident::TEXT_END;
/// The reservation (64 MiB). The private region has room up to the code area (`execution::cache::CODE_VA`, 256 MiB), the
/// backing at any size (it is address space until touched).
pub const POOL_MAX_PAGES: usize = 16384;
/// The least pool a launch may set (`machine.shadow_pool_pages`): the window's pool before November 25, so tests can still
/// fill a pool of that size.
pub const POOL_MIN_PAGES: usize = 285;
/// The end of the reservation: the window, the text block and the pool precede the cache's areas in a vCPU's block
/// (`execution::cache::LOOKUP_OFFSET`).
pub const POOL_END: usize = POOL_OFFSET + POOL_MAX_PAGES * 4096;
/// Whether a launch's pool size (`Control::shadow_pool_pages`) is one the host maps and the resident accepts.
pub const fn pool_pages_valid(pages: u64) -> bool {
    pages >= POOL_MIN_PAGES as u64 && pages <= POOL_MAX_PAGES as u64
}
/// The pages a space allocates from, from the launch's `Control::shadow_pool_pages`: never past the reservation (the
/// resident refuses to start with an invalid value; this bound holds whatever the field says).
pub const fn pool_pages(field: u64) -> usize {
    if field > POOL_MAX_PAGES as u64 { POOL_MAX_PAGES } else { field as usize }
}
/// M2 checkpoint C: the roots 32-bit user code runs under (three window pages, never pool pages), with an ASID
/// of their own. They map the first 4 GiB and nothing else of the guest: [`COMPAT_LOWER_ROOT`]'s entry 0 links
/// [`COMPAT_L1`] and its entry 256 the resident's private region (the host writes both); [`COMPAT_UPPER_ROOT`] holds
/// only the guest-state alias's entry. `COMPAT_L1`'s entries 0 to 3 are copies of the user space's level-1 entries of
/// the same range ([`Space::leaf`] writes a copy with each link, the resets clear them), so both roots share every
/// table below and a shadow of a page under 4 GiB is one leaf entry. Any native address with a bit above 31 set then
/// aborts under these roots, whatever a generated 64-bit sum or a register's upper half made it.
pub const COMPAT_LOWER_ROOT: usize = 0xe0000;
pub const COMPAT_UPPER_ROOT: usize = 0xe1000;
pub const COMPAT_L1: usize = 0xe2000;
/// Level-1 entries (GiB) the compat root shares with the user space.
pub const COMPAT_GIBS: usize = 4;
/// `[lower, upper]` roots by class: supervisor, user, compat.
pub const CLASS_ROOTS: [[usize; 2]; 3] = [ROOTS[0], ROOTS[1], [COMPAT_LOWER_ROOT, COMPAT_UPPER_ROOT]];
/// ASIDs are 8 bits: vCPU `v` has 2v + 2 (supervisor, the host's, loaded at boot), 2v + 3 (user) and, clear of both for
/// 64 vCPUs, `COMPAT_ASID + v` for the compat root.
pub const COMPAT_ASID: u64 = 130;
/// From the loaded ASID (TTBR0_EL1[55:48]): the loaded class ([`CLASS_ROOTS`]' index) and the owner's three ASIDs.
pub const fn asids(loaded: u64) -> (usize, [u64; 3]) {
    let (class, supervisor) = if loaded >= COMPAT_ASID {
        (2, 2 * (loaded - COMPAT_ASID) + 2)
    } else {
        ((loaded & 1) as usize, loaded & !1)
    };
    (class, [supervisor, supervisor + 1, COMPAT_ASID - 1 + supervisor / 2])
}
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
// The pool follows the resident's text block and precedes the cache's areas in the vCPU's block; in the private region it
// lies below the code area, the lowest of the areas above it (nothing else is mapped between). The host maps it in 2 MiB
// blocks: its start and size are 2 MiB multiples.
const _: () = assert!(POOL_OFFSET >= crate::resident::TEXT_END && POOL_END <= crate::execution::cache::LOOKUP_OFFSET);
const _: () = assert!(POOL_END as u64 <= crate::execution::cache::CODE_VA - crate::PRIVATE);
const _: () = assert!(POOL_OFFSET % crate::execution::cache::BLOCK == 0 && POOL_END % crate::execution::cache::BLOCK == 0);
// The compat tables are window pages.
const _: () = assert!(COMPAT_TABLES.end <= crate::resident::TEXT_OFFSET);
const _: () =
    assert!(STATE_OFFSET < USER_LOWER_ROOT && USER_UPPER_ROOT + 4096 <= crate::SHARED_OFFSET);

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
    /// The target is a page of the owner's window, text block or pool reservation (below [`OWN_END`]).
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
    /// [`Maintenance::invalidate`] of the pages of the 2 MiB region at `base` whose bits `pages` holds (bit `i` of word
    /// `i / 64`: page `base + 4096 i`), as one: complete prior table stores once, invalidate each page, complete the
    /// invalidations once, then ISB (fault-around plan chunk 2: an INVLPG's region drop no longer pays a full barrier
    /// sequence per valid entry). By default one [`Maintenance::invalidate`] per page.
    fn invalidate_many(&mut self, base: u64, pages: &[u64; 8]) {
        for (w, &bits) in pages.iter().enumerate() {
            let mut bits = bits;
            while bits != 0 {
                let i = w * 64 + bits.trailing_zeros() as usize;
                bits &= bits - 1;
                self.invalidate(base + 4096 * i as u64);
            }
        }
    }
}

/// One neighbour shadow for [`Space::fill`] (fault-around plan 3.4): its leaf index in the 2 MiB region, its target (an IPA,
/// page-aligned), whether writable and whether its translation is global; `written` is the fill's answer.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Fill {
    pub index: u16,
    pub target: u64,
    pub writable: bool,
    pub global: bool,
    pub written: bool,
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
/// Window pages holding the owner's tables and shadow state, the compat tables, and (G1) the frame page (the guest-state
/// page, also reached by its EL0 alias) and the alias's tables: all below [`OWN_END`], so never a target.
const PRIVATE_TABLES: core::ops::Range<usize> = LOWER_ROOT..USER_UPPER_ROOT + 4096;
const COMPAT_TABLES: core::ops::Range<usize> = COMPAT_LOWER_ROOT..COMPAT_L1 + 4096;
const GUEST_STATE: [core::ops::Range<usize>; 3] = [
    crate::execution::FRAME_OFFSET..crate::execution::FRAME_OFFSET + 4096,
    crate::execution::GUEST_STATE_TABLES[0]..crate::execution::GUEST_STATE_TABLES[2] + 4096,
    crate::execution::GUEST_STATE_USER_TABLES[0]..crate::execution::GUEST_STATE_USER_TABLES[1] + 4096,
];
/// No shadow targets a page of the owner's block below this offset (relative to its window's IPA): the window (tables,
/// state, frame, stacks, staging, the pages the pool held before November 25), the resident's text block and the pool's
/// whole reservation. Targets are guest RAM and device pages, which lie past every vCPU's block, or the all-ones page,
/// below the backing (so far above any offset here, wrapped).
pub const OWN_END: usize = POOL_END;
const _: () = assert!(
    PRIVATE_TABLES.end <= OWN_END
        && COMPAT_TABLES.end <= OWN_END
        && GUEST_STATE[0].end <= OWN_END
        && GUEST_STATE[1].end <= OWN_END
        && GUEST_STATE[2].end <= OWN_END
        && crate::resident::TEXT_END <= OWN_END
        && crate::ALL_ONES_IPA + crate::ALL_ONES_BYTES <= crate::IPA
);

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
            [(lower, 0..256), (high, 256..512)]
                .into_iter()
                .take(if upper { 2 } else { 1 })
        })
    }
    /// Whether `offset` is an allocated pool table page (either end).
    fn allocated(&self, offset: usize) -> bool {
        offset
            .checked_sub(POOL_OFFSET)
            .map(|o| o / 4096)
            .is_some_and(|page| {
                page < self.count(BOTTOM)
                    || (page < self.pool_pages
                        && self
                            .pool_pages
                            .checked_sub(self.count(TOP))
                            .is_some_and(|first| page >= first))
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
                    // The compat root's copy of a user level-1 entry below 4 GiB ([`COMPAT_L1`]).
                    if self.user && level == 1 && va >> 32 == 0 {
                        self.set(COMPAT_L1, index, (self.ipa + next as u64) | TABLE);
                    }
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
        // Any page of the owner's window, text block or pool reservation (its tables, state and pool among them).
        if (target.wrapping_sub(self.ipa) as usize) < OWN_END {
            return Err(Error::Private);
        }
        let (table, index) = self.leaf(va, true)?.ok_or(Error::Corrupt)?;
        // Before the entry: a CR3 load that sees the entry sees the list too (one CPU, program order).
        if !global && self.kept(va) && self.get(STATE_OFFSET, NOT_ALL_GLOBAL) == 0 {
            let n = self.get(STATE_OFFSET, LISTED) as usize;
            if !(0..n).any(|i| self.get(STATE_OFFSET, LIST + i) == va) {
                if n < LIST_MAX {
                    self.set(STATE_OFFSET, LIST + n, va);
                    self.set(STATE_OFFSET, LISTED, n as u64 + 1);
                } else {
                    self.set(STATE_OFFSET, NOT_ALL_GLOBAL, 1);
                }
            }
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
    /// them, every entry cleared before one batch of invalidations ([`Maintenance::invalidate_many`]).
    pub fn unmap_large(&mut self, va: u64) -> Result<(), Error> {
        let start = va & !0x1f_ffff;
        if let Some((table, _)) = self.leaf(start, false)? {
            let mut cleared = [0u64; 8];
            for index in 0..512 {
                if self.get(table, index) & 1 != 0 {
                    self.set(table, index, 0);
                    cleared[index / 64] |= 1 << (index % 64);
                }
            }
            if cleared != [0; 8] {
                self.tlb.invalidate_many(start, &cleared);
            }
        }
        Ok(())
    }
    /// Fault-around plan 3.2: the slots of `range` (leaf indices) in the leaf table of the 2 MiB region holding `region`
    /// that hold a valid descriptor (bit `i % 64` of word `i / 64` for index `i`), `None` when the region has no leaf table.
    pub fn valid_slots(&mut self, region: u64, range: core::ops::Range<usize>) -> Result<Option<[u64; 8]>, Error> {
        let Some((table, _)) = self.leaf(region & !0x1f_ffff, false)? else {
            return Ok(None);
        };
        let mut valid = [0u64; 8];
        for index in range.start.min(512)..range.end.min(512) {
            if self.get(table, index) & 1 != 0 {
                valid[index / 64] |= 1 << (index % 64);
            }
        }
        Ok(Some(valid))
    }
    /// Fault-around plan 3.4: installs the neighbour shadows `entries` in the leaf table of the 2 MiB region holding
    /// `region`, which a demand install has just made (the fill never allocates: no table, nothing written). Only empty
    /// slots are written (no entry is replaced, so no break-before-make and no TLB invalidation: an invalid descriptor is
    /// never cached), then one publish for the batch. Refused (left unwritten): a non-global entry in the kept half (a CR3
    /// load would have to remove it, and the fill lists nothing, so the kept-half invariant is the install's alone), a
    /// private target (any page below [`OWN_END`] of the owner's block), a misaligned target, an index past the table.
    /// Each entry's `written` says whether it was installed; returns how many were.
    pub fn fill(&mut self, region: u64, entries: &mut [Fill]) -> Result<usize, Error> {
        let start = region & !0x1f_ffff;
        let Some((table, _)) = self.leaf(start, false)? else {
            entries.iter_mut().for_each(|e| e.written = false);
            return Ok(0);
        };
        let kept = self.kept(start);
        let mut written = 0;
        for e in entries.iter_mut() {
            let index = e.index as usize;
            e.written = index < 512
                && e.target & !OUTPUT == 0
                && !(kept && !e.global)
                && (e.target.wrapping_sub(self.ipa) as usize) >= OWN_END
                && self.get(table, index) & 1 == 0;
            if e.written {
                self.set(table, index, e.target | PAGE | NOT_GLOBAL | if e.writable { 0 } else { READ_ONLY });
                written += 1;
            }
        }
        if written != 0 {
            self.tlb.publish();
        }
        Ok(written)
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
        self.unlink_compat();
        for word in [BOTTOM, TOP, NOT_ALL_GLOBAL, LISTED] {
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
        // The kept half's non-global shadows go (the invalidation below covers them); its global ones stay.
        for i in 0..self.get(STATE_OFFSET, LISTED) as usize {
            match self.leaf(self.get(STATE_OFFSET, LIST + i), false) {
                Ok(Some((table, index))) => self.set(table, index, 0),
                Ok(None) => {}
                Err(_) => {
                    self.reset();
                    return true;
                }
            }
        }
        self.set(STATE_OFFSET, LISTED, 0);
        let [lower, _] = ROOTS[0];
        let [user_lower, user_upper] = ROOTS[1];
        for (root, indices) in [
            (lower, 0..256),
            (user_lower, 0..256),
            (user_upper, 256..512),
        ] {
            for index in indices {
                self.set(root, index, 0);
            }
        }
        self.unlink_compat();
        self.set(STATE_OFFSET, BOTTOM, 0);
        self.tlb.invalidate_all();
        false
    }
    /// The compat root's copies go with the user root's entries: the tables they link return to the pool.
    fn unlink_compat(&mut self) {
        for index in 0..COMPAT_GIBS {
            self.set(COMPAT_L1, index, 0);
        }
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
        fn invalidate_many(&mut self, base: u64, pages: &[u64; 8]) {
            for i in 0..512 {
                if pages[i / 64] >> (i % 64) & 1 != 0 {
                    self.0.push(base + 4096 * i as u64);
                }
            }
            self.0.push(MANY);
        }
    }
    /// The end of one [`Maintenance::invalidate_many`] batch.
    const MANY: u64 = 2;
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
    /// The owner's window, text block and pool (the pool's pages are backed only as touched).
    fn window() -> Vec<u64> {
        vec![0; POOL_END / 8]
    }
    fn word(memory: &[u64], offset: usize) -> u64 {
        memory[offset / 8]
    }

    /// M2 checkpoint C: the compat root's level-1 table holds exactly the user space's level-1 entries below 4 GiB, through
    /// links and both resets, and its three pages are never a shadow's target.
    #[test]
    fn every_asid_names_its_class_and_its_owners_three() {
        let mut seen = std::collections::BTreeSet::new();
        for v in 0..64 {
            let own = [2 * v + 2, 2 * v + 3, COMPAT_ASID + v];
            for (class, asid) in own.into_iter().enumerate() {
                assert_eq!(asids(asid), (class, own));
                assert!(asid != 0 && asid < 256 && seen.insert(asid));
            }
        }
    }
    #[test]
    fn compat_root_copies_the_user_links_below_4_gib() {
        let mut m = window();
        let target = 0x4007_0000;
        let copies = |m: &[u64]| [0, 1, 2, 3, 4].map(|i| word(m, COMPAT_L1 + i * 8));
        let user_l1 = |m: &[u64], i: usize| word(m, (word(m, USER_LOWER_ROOT) & OUTPUT) as usize - IPA as usize + i * 8);
        // A supervisor shadow below 4 GiB links nothing under the compat root.
        space(&mut m, POOL_MAX_PAGES).map(0x1000, target, true).unwrap();
        assert_eq!(copies(&m), [0; 5]);
        let mut s = space(&mut m, POOL_MAX_PAGES);
        s.user = true;
        for va in [0x1000, 0xffff_f000, 1 << 32, 0x7fff_ffff_f000, u64::MAX & !4095] {
            s.map(va, target, true).unwrap();
        }
        let got = copies(&m);
        assert_eq!(got, [user_l1(&m, 0), 0, 0, user_l1(&m, 3), 0]);
        assert!(got[0] & 3 == TABLE && got[3] & 3 == TABLE && got[0] != got[3]);
        for full in [true, false] {
            let mut s = space(&mut m, POOL_MAX_PAGES);
            s.user = true;
            s.map(0x4000_0000, target, false).unwrap();
            assert_ne!(copies(&m)[1], 0);
            assert!(if full { s.reset(); true } else { !s.reset_cr3() });
            assert_eq!(copies(&m), [0; 5]);
        }
        let mut s = space(&mut m, POOL_MAX_PAGES);
        s.user = true;
        for table in [COMPAT_LOWER_ROOT, COMPAT_UPPER_ROOT, COMPAT_L1] {
            assert_eq!(s.map(0x1000, IPA + table as u64, true), Err(Error::Private));
        }
        assert!(COMPAT_TABLES.end <= crate::resident::TEXT_OFFSET && COMPAT_TABLES.end <= POOL_OFFSET);
    }

    #[test]
    fn unmap_large_clears_one_region() {
        let mut m = window();
        let mut s = space(&mut m, POOL_MAX_PAGES);
        for va in [0x20_0000, 0x20_3000, 0x3f_f000, 0x40_0000] {
            s.map(va, 0x4007_0000, false).unwrap();
        }
        s.tlb.0.clear();
        s.unmap_large(0x21_2345).unwrap();
        // Every entry cleared, then one batch of invalidations (fault-around plan chunk 2).
        assert_eq!(s.tlb.0, vec![0x20_0000, 0x20_3000, 0x3f_f000, MANY]);
        assert_eq!(
            [0x20_0000, 0x20_3000, 0x3f_f000].map(|va| s.descriptor(va)),
            [0; 3]
        );
        assert_ne!(s.descriptor(0x40_0000), 0);
        // A region with no leaf table, or none valid: nothing to do.
        s.unmap_large(0x80_0000).unwrap();
        s.unmap_large(0x20_0000).unwrap();
        assert_eq!(s.tlb.0.len(), 4);
    }
    /// The default `invalidate_many` (host-side maintenance) invalidates each page of the batch, in order.
    #[test]
    fn default_invalidate_many_invalidates_each_page() {
        struct Each(Vec<u64>);
        impl Maintenance for Each {
            fn publish(&mut self) {}
            fn invalidate(&mut self, va: u64) {
                self.0.push(va)
            }
            fn invalidate_all(&mut self) {}
        }
        let mut e = Each(Vec::new());
        let mut pages = [0u64; 8];
        pages[0] = 1 | 1 << 63;
        pages[7] = 1 << 63;
        e.invalidate_many(HIGH_BASE, &pages);
        assert_eq!(e.0, [HIGH_BASE, HIGH_BASE + 63 * 4096, HIGH_BASE + 511 * 4096]);
    }

    /// Fault-around plan 3.4 (Tier 0; M10): `fill` writes only empty slots of an existing leaf table, never allocates,
    /// refuses kept-half non-global entries and private targets, publishes once for the batch and returns the count.
    #[test]
    fn fill_writes_only_empty_slots_and_publishes_once() {
        let mut m = window();
        let mut s = space(&mut m, POOL_MAX_PAGES);
        let fill = |index, target, writable, global| Fill { index, target, writable, global, written: false };
        // No leaf table: nothing written, nothing allocated, no maintenance.
        let mut e = [fill(1, 0x4007_1000, false, false)];
        assert_eq!(s.fill(0x20_0000, &mut e), Ok(0));
        assert_eq!((s.used(), e[0].written, s.tlb.0.len()), (0, false, 0));
        // The demand install made the table; its own slot (3) is valid already and is never replaced.
        s.map(0x20_3000, 0x4007_3000, false).unwrap();
        let used = s.used();
        s.tlb.0.clear();
        let mut e = [
            fill(0, 0x4007_0000, true, false),
            fill(3, 0x4009_0000, true, false),
            fill(4, 0x4007_4000, false, true),
            fill(5, IPA + STATE_OFFSET as u64, false, false),
            fill(6, 0x4007_6800, false, false),
            fill(512, 0x4007_0000, false, false),
        ];
        assert_eq!(s.fill(0x20_0000 + 0x1234, &mut e), Ok(2));
        assert_eq!(e.map(|e| e.written), [true, false, true, false, false, false]);
        assert_eq!(s.tlb.0, [PUBLISH]);
        assert_eq!(s.used(), used);
        assert_eq!(s.descriptor(0x20_0000), 0x4007_0000 | LEAF);
        assert_eq!(s.descriptor(0x20_3000), 0x4007_3000 | LEAF | READ_ONLY);
        assert_eq!(s.descriptor(0x20_4000), 0x4007_4000 | LEAF | READ_ONLY);
        assert_eq!(s.valid_slots(0x20_0000, 0..16), Ok(Some([0b11001, 0, 0, 0, 0, 0, 0, 0])));
        assert_eq!(s.valid_slots(0x20_0000, 4..512), Ok(Some([0b10000, 0, 0, 0, 0, 0, 0, 0])));
        assert_eq!(s.valid_slots(0x80_0000, 0..512), Ok(None));
        assert_eq!([0x20_5000, 0x20_6000].map(|va| s.descriptor(va)), [0, 0]);
        // Everything refused or present already: no publish.
        s.tlb.0.clear();
        let mut e = [fill(0, 0x4008_0000, false, false), fill(3, 0x4008_3000, false, false)];
        assert_eq!(s.fill(0x20_0000, &mut e), Ok(0));
        assert!(s.tlb.0.is_empty());
        assert_eq!(s.descriptor(0x20_0000), 0x4007_0000 | LEAF);
        // The kept half (supervisor, upper): global entries only; the list is untouched, so a CR3 load keeps the half.
        let kernel = HIGH_BASE + 0x40_0000;
        s.map_global(kernel, 0x400a_0000, false).unwrap();
        let mut e = [fill(1, 0x400a_1000, false, true), fill(2, 0x400a_2000, true, false)];
        assert_eq!(s.fill(kernel, &mut e), Ok(1));
        assert_eq!(e.map(|e| e.written), [true, false]);
        assert!(!s.reset_cr3());
        assert_eq!(s.descriptor(kernel + 0x1000), 0x400a_1000 | LEAF | READ_ONLY);
        assert_eq!(s.descriptor(kernel + 0x2000), 0);
        // The user space's upper half is not kept: non-global entries are written there.
        s.user = true;
        s.map(kernel, 0x400a_0000, false).unwrap();
        let mut e = [fill(2, 0x400a_2000, true, false)];
        assert_eq!(s.fill(kernel, &mut e), Ok(1));
        assert_eq!(s.descriptor(kernel + 0x2000), 0x400a_2000 | LEAF);
        // Every private page of the owner's block is refused, in both spaces.
        for user in [false, true] {
            s.user = user;
            s.map(0x60_0000, 0x4007_0000, false).unwrap();
            for page in (0..OWN_END / 4096).step_by(97) {
                let mut e = [fill(1, IPA + (page * 4096) as u64, false, false)];
                assert_eq!(s.fill(0x60_0000, &mut e), Ok(0), "page {page:#x}");
            }
        }
    }
    #[test]
    fn halves_allocate_tables_from_the_pool_and_gap_is_refused() {
        let mut m = window();
        let mut s = space(&mut m, POOL_MAX_PAGES);
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
        assert_eq!(word(&m, UPPER_ROOT + 256 * 8), table(POOL_MAX_PAGES - 1));
        assert_eq!(
            word(&m, POOL_OFFSET + (POOL_MAX_PAGES - 1) * 4096),
            table(POOL_MAX_PAGES - 2)
        );
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
        s.map(HIGH_BASE + 0x10_0000, 0x4007_0000, false).unwrap();
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
        let mut s = space(&mut m, POOL_MAX_PAGES);
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
        let mut s = space(&mut m, POOL_MAX_PAGES);
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
            assert_eq!(
                s.map(0x30_0000, IPA + own as u64, false),
                Err(Error::Private)
            );
        }
        s.unmap(0x10_0000).unwrap();
        assert_eq!(s.descriptor(0x10_0000), 0);
        s.user = true;
        assert_eq!(s.descriptor(0x10_0000), 0x4008_0000 | LEAF | READ_ONLY);
        s.reset();
        assert_eq!(
            (
                s.used(),
                s.descriptor(0x10_0000),
                s.descriptor(HIGH_BASE + 0x20_0000)
            ),
            (0, 0, 0)
        );
        drop(s);
        // Both spaces' guest halves are unlinked; the private region index stays.
        m[USER_LOWER_ROOT / 8 + 256] = 7;
        // G1: the guest-state alias's upper-root entry (below 256) stays in both spaces.
        let alias = (crate::execution::GUEST_STATE_SLOT.start >> 39 & 511) as usize;
        for [_, upper] in ROOTS {
            m[upper / 8 + alias] = 9;
        }
        let mut s = space(&mut m, POOL_MAX_PAGES);
        s.reset();
        drop(s);
        for [lower, upper] in ROOTS {
            assert!((0..256).all(|i| word(&m, lower + i * 8) == 0));
            assert!((256..512).all(|i| word(&m, upper + i * 8) == 0));
        }
        assert_eq!(word(&m, USER_LOWER_ROOT + 256 * 8), 7);
        assert!(
            ROOTS
                .iter()
                .all(|&[_, upper]| word(&m, upper + alias * 8) == 9)
        );
        // The host's pre-linked tables belong to the supervisor space: a user root linking one is corrupt.
        m[USER_LOWER_ROOT / 8] = (IPA + 0x11000) | 3;
        let mut s = space(&mut m, POOL_MAX_PAGES);
        s.user = true;
        assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
    }

    #[test]
    fn foreign_descriptor_is_corrupt_not_followed() {
        let mut m = window();
        m[LOWER_ROOT / 8] = 0x4000_0000 | 1; // block, as the boot identity alias
        let mut s = space(&mut m, POOL_MAX_PAGES);
        assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
        drop(s);
        m[LOWER_ROOT / 8] = (IPA + crate::RAM_SIZE as u64) | 3; // outside the window
        let mut s = space(&mut m, POOL_MAX_PAGES);
        assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
        assert_eq!(s.descriptor(0x1000), 0);
    }

    #[test]
    fn single_root_space_never_touches_the_upper_root() {
        let mut m = window();
        // An unmapped upper root would fault natively; poison it here instead.
        m[UPPER_ROOT / 8..(UPPER_LEAF + 4096) / 8].fill(u64::MAX);
        let mut s = space(&mut m, POOL_MAX_PAGES);
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
        let mut s = space(&mut m, POOL_MAX_PAGES);
        s.upper = false;
        assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
    }

    #[test]
    fn links_reach_only_shadow_tables_and_targets_exclude_own_tables() {
        let mut m = window();
        let mut s = space(&mut m, POOL_MAX_PAGES);
        s.map(0, 0x4000_0000, true).unwrap();
        drop(s);
        // A link into the private leaf, an unallocated pool page or a root.
        for to in [0x14000, POOL_OFFSET + 5 * 4096, UPPER_ROOT] {
            m[(POOL_OFFSET + 4096) / 8] = (IPA + to as u64) | 3;
            let mut s = space(&mut m, POOL_MAX_PAGES);
            assert_eq!(s.map(0, 0x4000_0000, true), Err(Error::Corrupt));
        }
        let mut s = space(&mut m, POOL_MAX_PAGES);
        // G1: also the frame page and the guest-state alias's tables.
        let [l1, _, leaf] = crate::execution::GUEST_STATE_TABLES;
        for own in [
            LOWER_ROOT,
            0x14000,
            STATE_OFFSET,
            POOL_OFFSET + 31 * 4096,
            crate::execution::FRAME_OFFSET,
            l1,
            leaf,
            crate::execution::GUEST_STATE_USER_TABLES[1],
        ] {
            let target = IPA + own as u64;
            assert_eq!(s.map(0x20_0000, target, false), Err(Error::Private));
        }
        s.map(0x20_0000, IPA + OWN_END as u64, true)
            .unwrap_or_else(|e| panic!("{e:?}"));
    }

    /// Perf chunk 14: a CR3 load keeps the supervisor space's upper half while every shadow there is global, with its
    /// tables at the top of the pool; everything else goes and the bottom of the pool returns.
    #[test]
    fn cr3_reset_keeps_global_upper_shadows_only() {
        let mut m = window();
        let mut s = space(&mut m, POOL_MAX_PAGES);
        let kernel = HIGH_BASE + 0x10_0000;
        s.map_global(kernel, 0x4007_0000, true).unwrap();
        s.map_global(0x10_0000, 0x4008_0000, true).unwrap(); // global, but the lower half: not kept
        s.user = true;
        s.map(0x20_0000, 0x4009_0000, false).unwrap();
        s.map_global(HIGH_BASE + 0x40_0000, 0x400a_0000, false)
            .unwrap(); // the user space's upper half: not kept
        s.user = false;
        assert_eq!(s.used(), 12);
        s.tlb.0.clear();
        assert!(!s.reset_cr3());
        assert_eq!(s.tlb.0, [ALL]);
        assert_eq!(s.used(), 3);
        assert_eq!(s.descriptor(kernel), 0x4007_0000 | LEAF);
        assert_eq!(s.descriptor(0x10_0000), 0);
        s.user = true;
        assert_eq!(
            (s.descriptor(0x20_0000), s.descriptor(HIGH_BASE + 0x40_0000)),
            (0, 0)
        );
        // The bottom is reused; it never meets the kept tables.
        s.map(0x20_0000, 0x4009_0000, false).unwrap();
        s.user = false;
        assert_eq!(s.used(), 6);
        assert_eq!(s.descriptor(kernel), 0x4007_0000 | LEAF);
        // An INVLPG reaches a kept shadow.
        s.map_global(kernel + 0x1000, 0x4007_1000, false).unwrap();
        s.unmap_large(kernel).unwrap();
        assert_eq!(
            (s.descriptor(kernel), s.descriptor(kernel + 0x1000)),
            (0, 0)
        );
        s.map_global(kernel, 0x4007_0000, true).unwrap();
        // Non-global shadows in the kept half: the next CR3 load removes them alone (listed; one listed twice).
        s.map(HIGH_BASE + 0x80_0000, 0x400b_0000, false).unwrap();
        s.map(HIGH_BASE + 0x80_1000, 0x400b_1000, false).unwrap();
        s.map(HIGH_BASE + 0x80_0000, 0x400b_0000, true).unwrap();
        assert!(!s.reset_cr3());
        assert_eq!(
            (s.descriptor(HIGH_BASE + 0x80_0000), s.descriptor(HIGH_BASE + 0x80_1000)),
            (0, 0)
        );
        assert_eq!(s.descriptor(kernel), 0x4007_0000 | LEAF);
        // More than the list holds: the next CR3 load resets everything, and the one after keeps again.
        for i in 0..=LIST_MAX as u64 {
            s.map(HIGH_BASE + 0x100_0000 + 4096 * i, 0x400c_0000, false).unwrap();
        }
        assert!(s.reset_cr3());
        assert_eq!((s.used(), s.descriptor(kernel)), (0, 0));
        s.map_global(kernel, 0x4007_0000, false).unwrap();
        assert!(!s.reset_cr3());
        assert_eq!(s.descriptor(kernel), 0x4007_0000 | LEAF | READ_ONLY);
        // A full reset returns the top of the pool too.
        assert_eq!(s.used(), 3);
        s.reset();
        assert_eq!((s.used(), s.descriptor(kernel)), (0, 0));
        // A full reset clears the overflow mark too.
        for i in 0..=LIST_MAX as u64 {
            s.map(HIGH_BASE + 0x100_0000 + 4096 * i, 0x400c_0000, false).unwrap();
        }
        s.reset();
        s.map_global(kernel, 0x4007_0000, false).unwrap();
        assert!(!s.reset_cr3());
        // Without the upper half there is nothing to keep: a full reset.
        s.upper = false;
        assert!(s.reset_cr3());
    }
    /// IO5: the kept half's non-global shadows are listed whatever path made them (a global one made non-global in place,
    /// with the same frame and rights), exactly `LIST_MAX` fit, and both kinds of reset empty the list.
    #[test]
    fn cr3_reset_removes_listed_non_global_shadows() {
        let mut m = window();
        let mut s = space(&mut m, POOL_MAX_PAGES);
        let (kernel, va) = (HIGH_BASE + 0x10_0000, HIGH_BASE + 0x20_0000);
        s.map_global(kernel, 0x4007_0000, true).unwrap();
        s.map_global(va, 0x4008_0000, true).unwrap();
        s.map(va, 0x4008_0000, true).unwrap(); // the same descriptor: listed all the same
        assert!(!s.reset_cr3());
        assert_eq!((s.descriptor(va), s.descriptor(kernel)), (0, 0x4007_0000 | LEAF));
        let fill = |s: &mut Space<Log>, base: u64, n: u64| {
            for i in 0..n {
                s.map(base + 4096 * i, 0x400c_0000, false).unwrap();
            }
        };
        fill(&mut s, HIGH_BASE + 0x100_0000, LIST_MAX as u64);
        assert!(!s.reset_cr3()); // exactly full: still removed one by one
        fill(&mut s, HIGH_BASE + 0x200_0000, 1);
        assert!(!s.reset_cr3()); // the count went back to zero
        fill(&mut s, HIGH_BASE + 0x100_0000, LIST_MAX as u64);
        s.reset();
        fill(&mut s, HIGH_BASE + 0x200_0000, 1);
        assert!(!s.reset_cr3()); // a full reset empties the list too
        assert_eq!(s.descriptor(kernel), 0);
    }
    /// Maps one 2 MiB region after another from `base` in the space `s` selects (globally, so the supervisor upper half's
    /// tables come from the top of the pool) until the pool is full: the regions mapped, the error that ended them.
    fn fill(s: &mut Space<Log>, base: u64, global: bool) -> (u64, Error) {
        let mut regions = 0;
        loop {
            let va = base + regions * 0x20_0000;
            match if global { s.map_global(va, 0x4000_0000, true) } else { s.map(va, 0x4000_0000, true) } {
                Ok(()) => regions += 1,
                Err(e) => return (regions, e),
            }
        }
    }
    /// The pool page `link` links to (a table descriptor's output, as a pool page number).
    fn pool_page(link: u64) -> usize {
        assert_eq!(link & 3, TABLE);
        ((link & OUTPUT) - IPA) as usize / 4096 - POOL_OFFSET / 4096
    }

    /// No shadow of either space ever targets a page of the owner's block below [`OWN_END`]: every page of the window (the
    /// tables, state, frame, stacks, staging and the pages the pool held before November 25, which nothing refused by name
    /// any more), the text block and the pool's whole reservation, whatever the `pool_pages` a space allocates from; the
    /// first page past it, guest RAM's pages and the all-ones page below the backing are targets.
    #[test]
    fn no_shadow_targets_the_owners_window_text_or_pool() {
        let mut m = window();
        for pool_pages in [POOL_MAX_PAGES, POOL_MIN_PAGES] {
            for user in [false, true] {
                let mut s = space(&mut m, pool_pages);
                s.user = user;
                for page in 0..OWN_END / 4096 {
                    let target = IPA + (page * 4096) as u64;
                    assert_eq!(s.map(0x20_0000, target, true), Err(Error::Private), "page {:#x}", page * 4096);
                    assert_eq!(s.map_global(HIGH_BASE + 0x20_0000, target, false), Err(Error::Private));
                }
                assert_eq!(s.descriptor(0x20_0000), 0);
                for target in [IPA + OWN_END as u64, IPA + (1 << 30), crate::ALL_ONES_IPA] {
                    s.map(0x20_0000, target, false).unwrap_or_else(|e| panic!("{target:#x}: {e:?}"));
                    assert_eq!(s.descriptor(0x20_0000), target | LEAF | READ_ONLY);
                }
                s.reset();
            }
        }
        // The old window pool, named: refused although no table lives there now.
        let mut s = space(&mut m, POOL_MAX_PAGES);
        for own in [0xe3000, 0x100000, 0x1ff000] {
            assert_eq!(s.map(0x20_0000, IPA + own, true), Err(Error::Private));
        }
        // The reservation is its own: below it the text block, above it the lookup area (no window pages, no tables).
        assert!(POOL_OFFSET >= crate::resident::TEXT_END && POOL_END <= crate::execution::cache::LOOKUP_OFFSET);
    }

    /// The pool sizes a launch can set: the whole reservation (the default), the least, and one between.
    const SIZES: [usize; 3] = [POOL_MAX_PAGES, POOL_MIN_PAGES, 1000];

    /// A reset returns every page of the pool, both ends, at each size: filled to its last page (the kept half's tables
    /// from the top, the rest from the bottom), emptied, it fills again to its last page from the bottom alone, over the
    /// pages the top held; a link to a page the reset returned is corrupt until the page is taken again.
    #[test]
    fn reset_returns_the_whole_pool() {
        for pages in SIZES {
            let end = POOL_OFFSET + pages * 4096;
            let mut m = window();
            let mut s = space(&mut m, pages);
            let (kept, e) = fill(&mut s, HIGH_BASE + (1 << 39), true);
            assert_eq!((e, s.used()), (Error::Capacity, pages));
            assert!(kept as usize > pages - pages / 256 - 3, "{kept} regions: the kept half alone fills the pool");
            s.reset();
            assert_eq!((s.used(), s.descriptor(HIGH_BASE + (1 << 39))), (0, 0));
            drop(s);
            // The kept half's first level-1 table was the pool's last page: returned, so a link to it is corrupt.
            m[LOWER_ROOT / 8] = (IPA + (end - 4096) as u64) | TABLE;
            let mut s = space(&mut m, pages);
            assert_eq!(s.map(0x1000, 0x4000_0000, true), Err(Error::Corrupt));
            s.reset();
            // About half from each end, then everything from the bottom.
            for r in 0..pages as u64 / 2 {
                s.map_global(HIGH_BASE + (1 << 39) + r * 0x20_0000, 0x4000_0000, true).unwrap();
            }
            assert_eq!((fill(&mut s, 1 << 39, false).1, s.used()), (Error::Capacity, pages));
            s.reset();
            assert_eq!(s.used(), 0);
            let (regions, e) = fill(&mut s, 1 << 39, false);
            assert_eq!((e, s.used()), (Error::Capacity, pages));
            drop(s);
            // The bottom's first table is the pool's first page, its last tables its last pages (the top's before).
            assert_eq!(pool_page(word(&m, LOWER_ROOT + 8)), 0);
            let mut s = space(&mut m, pages);
            assert!((0..regions).all(|r| s.descriptor((1 << 39) + r * 0x20_0000) == 0x4000_0000 | LEAF));
            let last = (1u64 << 39) + (regions - 1) * 0x20_0000;
            let (table, _) = s.leaf(last, false).unwrap().unwrap();
            assert!(table >= end - 2 * 4096 && table < end, "{table:#x}");
        }
    }

    /// Perf chunk 14 at each pool size: the kept half's tables are the top pages of the pool the launch set (not of the
    /// reservation), every other table a bottom page; the two meet exactly at the pool's size; a CR3 load returns the bottom
    /// alone, keeps the top's tables and shadows, and the bottom refills to the same page count, never reaching a kept table.
    #[test]
    fn kept_half_split_at_each_pool_size() {
        for pages in SIZES {
            let end = POOL_OFFSET + pages * 4096;
            let mut m = window();
            let mut s = space(&mut m, pages);
            let kernel = HIGH_BASE + (1 << 39);
            // A quarter of the pool's kept regions: a level-1 table, a level-2 table per GiB, a leaf table each.
            let kept = pages as u64 / 4;
            for r in 0..kept {
                s.map_global(kernel + r * 0x20_0000, 0x4007_0000, true).unwrap();
            }
            let top = s.used();
            assert_eq!(top as u64, kept + 1 + kept.div_ceil(512));
            let (bottom, e) = fill(&mut s, 1 << 39, false);
            assert_eq!((e, s.used()), (Error::Capacity, pages));
            assert!(!s.reset_cr3());
            assert_eq!(s.used(), top);
            assert!((0..kept).all(|r| s.descriptor(kernel + r * 0x20_0000) == 0x4007_0000 | LEAF));
            let (again, e) = fill(&mut s, 1 << 39, false);
            assert_eq!((e, s.used(), again), (Error::Capacity, pages, bottom));
            assert!((0..kept).all(|r| s.descriptor(kernel + r * 0x20_0000) == 0x4007_0000 | LEAF));
            drop(s);
            // The kept half's level-1 table is the pool's last page, the lower half's its first.
            assert_eq!(pool_page(word(&m, UPPER_ROOT + 257 * 8)), pages - 1);
            assert_eq!(pool_page(word(&m, LOWER_ROOT + 8)), 0);
            // Every kept table is a top page, every lower one a bottom page; none lies past the pool.
            let mut s = space(&mut m, pages);
            for r in 0..kept {
                let (table, _) = s.leaf(kernel + r * 0x20_0000, false).unwrap().unwrap();
                assert!(table >= end - top * 4096 && table < end, "kept leaf {r} at {table:#x}");
            }
            for r in 0..bottom {
                let (table, _) = s.leaf((1 << 39) + r * 0x20_0000, false).unwrap().unwrap();
                assert!(table >= POOL_OFFSET && table < end - top * 4096, "lower leaf {r} at {table:#x}");
            }
        }
    }

    /// A launch's pool size: valid from [`POOL_MIN_PAGES`] to the reservation; a space never allocates past the
    /// reservation, whatever the field says.
    #[test]
    fn launch_pool_sizes_are_bounded_by_the_reservation() {
        let valid = [POOL_MIN_PAGES as u64, 1000, POOL_MAX_PAGES as u64];
        assert!(valid.iter().all(|&n| pool_pages_valid(n) && pool_pages(n) == n as usize));
        for n in [0, POOL_MIN_PAGES as u64 - 1, POOL_MAX_PAGES as u64 + 1, u64::MAX] {
            assert!(!pool_pages_valid(n), "{n}");
            assert!(pool_pages(n) <= POOL_MAX_PAGES, "{n}");
        }
        assert_eq!((POOL_MAX_PAGES * 4096, POOL_END), (64 << 20, crate::resident::TEXT_END + (64 << 20)));
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
