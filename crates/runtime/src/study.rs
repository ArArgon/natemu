//! PCID gain study (`pcid-study` builds only; measurement, never a default build): what a (CR3, PCID) design could keep.
//! It models the TLB a PCID Linux guest would have: [`SPACES`] address spaces by CR3 value (found, else taken round-robin,
//! as Linux takes its 6 dynamic ASIDs), each one set, or with `NATEMU_PCID_STUDY_PTI` set at build time a kernel and a
//! user set (page-table isolation: the two roots differ in bit 12), plus one set for the kept half's global shadows.
//! A set holds, by 2 MiB region, which 4 KiB pages had a shadow, by space and read/write. A load of another CR3 keeps
//! the old sets. What a PCID guest's TLB loses too clears them: a load of the current CR3 (both sets of the space), a
//! load of a kernel-only root (the space left: Linux flushes a lazily held space that way, at exit too, so a reused root
//! is not taken for the old space on this vCPU), a global flush, INVLPG, a #PF, and every shadow reset a CR3 load did
//! not cause. A shadow miss whose page is still in the set is a refill. A block revalidation is a refill when the block
//! was last current in an epoch of the current set, after that set's last clear ([`Head::born`], the ring). Refills are
//! work a CR3 load caused and PCID would spare; the resident entry holding one is charged (`Control::pcid_study`,
//! [`natemu_abi::execution::cache::pcid_study`]). An upper bound: no table pool limit, and a flush of a space another
//! vCPU holds unloaded is not seen (Linux catches up by generation when it loads it again).
#[cfg(feature = "pcid-study")]
mod on {
    use crate::cache;
    use natemu_abi::execution::cache::{PCID_STUDY, pcid_study::*};
    use natemu_abi::execution::perf::*;

    const PTI: bool = option_env!("NATEMU_PCID_STUDY_PTI").is_some();
    const SPACES: usize = 6;
    const SLOTS: usize = 2 * SPACES;
    const GLOBAL_SET: usize = SLOTS;
    const ENTRIES: usize = 4096;
    const LIMIT: u64 = 3072;
    const RING: usize = 1 << 16;
    const TABLES: usize = 4096 + RING;
    /// Buckets a refill's entry would keep (or that are another matter), and the ones a refill itself is.
    const KEPT: u64 = 1 << HOST | 1 << HALTED | 1 << MAILBOX | 1 << SHOOTDOWN | 1 << TLB | 1 << SYSTEM | 1 << DELIVERY | 1 << PLACE;
    const OWN: u64 = 1 << REVALIDATE | 1 << RELINK | 1 << SHADOW | 1 << LOOKUP;
    #[repr(C)]
    struct Region {
        /// `generation << 32 | 1 << 31 | region`; another generation's is free.
        tag: u64,
        bits: [[u64; 8]; 4],
    }
    #[repr(C)]
    struct Head {
        current: u64,
        next: u64,
        /// A CR3 load's own full reset is under way: no set is cleared.
        hold: u64,
        entry: u64,
        flags: u64,
        kind: u64,
        kept: u64,
        own: u64,
        last: u64,
        cr3: [u64; SPACES],
        taken: [u64; SPACES],
        generation: [u64; SLOTS + 1],
        used: [u64; SLOTS + 1],
        /// The epoch each set was last cleared for code at.
        born: [u64; SLOTS],
    }
    const _: () = assert!(size_of::<Head>() <= 4096);
    const _: () = assert!(TABLES + (SLOTS + 1) * ENTRIES * size_of::<Region>() <= natemu_abi::execution::cache::PCID_STUDY_BYTES);
    fn head() -> &'static mut Head {
        // SAFETY: this vCPU's lookup area; only its resident touches the study's part.
        unsafe { &mut *cache::lookup_area::<Head>(PCID_STUDY) }
    }
    /// The set current in each of the last [`RING`] epochs.
    fn ring(epoch: u64) -> *mut u8 {
        unsafe { cache::lookup_area::<u8>(PCID_STUDY + 4096).add(epoch as usize % RING) }
    }
    fn table(slot: usize) -> *mut Region {
        unsafe { cache::lookup_area::<Region>(PCID_STUDY + TABLES).add(slot * ENTRIES) }
    }
    fn count() -> &'static mut [u64; COUNT] {
        &mut cache::control().pcid_study
    }
    fn clear(slot: usize) {
        let h = head();
        (h.generation[slot], h.used[slot]) = (h.generation[slot] + 1, 0);
    }
    /// The space's sets go, for shadows and (from `epoch` on) for code.
    fn clear_space(space: usize, epoch: u64) {
        for slot in [2 * space, 2 * space + 1] {
            clear(slot);
            head().born[slot] = epoch;
        }
    }
    fn key(cr3: u64) -> u64 {
        if PTI { cr3 & !0x1000 } else { cr3 }
    }
    /// The region record of `va` in `slot`, made when `make` (none when the set is full).
    fn region(slot: usize, va: u64, make: bool) -> Option<&'static mut Region> {
        let h = head();
        let r = va >> 21 & 0x7ff_ffff;
        let tag = (h.generation[slot] + 1) << 32 | 1 << 31 | r;
        let mut i = (r.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> 52) as usize;
        loop {
            // SAFETY: inside the slot's table.
            let e = unsafe { &mut *table(slot).add(i) };
            if e.tag == tag {
                return Some(e);
            }
            if e.tag >> 32 != tag >> 32 {
                if !make {
                    return None;
                }
                if h.used[slot] >= LIMIT {
                    count()[FULL] += 1;
                    return None;
                }
                h.used[slot] += 1;
                count()[PEAK] = count()[PEAK].max(h.used[slot]);
                (e.tag, e.bits) = (tag, [[0; 8]; 4]);
                return Some(e);
            }
            i = (i + 1) & (ENTRIES - 1);
        }
    }
    fn held(slot: usize, va: u64, kind: usize) -> bool {
        let p = (va >> 12 & 511) as usize;
        region(slot, va, false).is_some_and(|e| e.bits[kind][p / 64] >> (p % 64) & 1 != 0)
    }
    fn hold(slot: usize, va: u64, kind: usize) {
        let p = (va >> 12 & 511) as usize;
        if let Some(e) = region(slot, va, true) {
            e.bits[kind][p / 64] |= 1 << (p % 64);
        }
    }
    /// The set a shadow of `va` belongs to: the kept half's global ones outlive CR3 loads today.
    fn data_slot(user: bool, va: u64, global: bool) -> usize {
        if global && !user && va >= natemu_abi::shadow::HIGH_BASE { GLOBAL_SET } else { head().current as usize }
    }
    /// A CR3 load, before its flush (which moves the epoch). `kernel_only`: the new root maps no user half.
    pub fn cr3(old: u64, new: u64, kernel_only: impl FnOnce() -> bool) {
        let h = head();
        let epoch = cache::control().epoch + 1;
        let space = h.current as usize / 2;
        h.last = if old == new {
            clear_space(space, epoch);
            SAME
        } else if key(old) == key(new) {
            h.current = (2 * space + (new >> 12 & 1) as usize) as u64;
            KNOWN
        } else {
            if kernel_only() {
                clear_space(space, epoch);
                count()[LAZY] += 1;
            }
            let half = if PTI { (new >> 12 & 1) as usize } else { 0 };
            if let Some(k) = (0..SPACES).find(|&k| h.taken[k] != 0 && h.cr3[k] == key(new)) {
                h.current = (2 * k + half) as u64;
                KNOWN
            } else {
                h.next = (h.next + 1) % SPACES as u64;
                let k = h.next as usize;
                (h.cr3[k], h.taken[k], h.current) = (key(new), 1, (2 * k + half) as u64);
                clear_space(k, epoch);
                NEW
            }
        } as u64;
        unsafe { ring(epoch).write(h.current as u8) };
        count()[h.last as usize] += 1;
    }
    /// The flush took `ticks` (`global`: no CR3 load, every set goes; the epoch has moved).
    pub fn flushed(global: bool, ticks: u64) {
        if global {
            let epoch = cache::control().epoch;
            (0..SPACES).for_each(|k| clear_space(k, epoch));
            clear(GLOBAL_SET);
            unsafe { ring(epoch).write(head().current as u8) };
        } else {
            count()[TLB_TICKS + head().last as usize] += ticks;
        }
    }
    pub fn holding(on: bool) {
        head().hold = on as u64;
    }
    /// Every shadow went (capacity, a peer's request, a device change): a PCID design loses them too. Blocks stay current.
    pub fn reset() {
        if head().hold == 0 {
            (0..=SLOTS).for_each(clear);
        }
    }
    /// INVLPG drops the shadows of the address's 2 MiB range (`large`), a #PF those of its page.
    pub fn invalidated(va: u64, large: bool) {
        let space = head().current as usize / 2;
        for slot in [2 * space, 2 * space + 1, GLOBAL_SET] {
            if let Some(e) = region(slot, va, false) {
                let p = (va >> 12 & 511) as usize;
                e.bits.iter_mut().for_each(|b| if large { *b = [0; 8] } else { b[p / 64] &= !(1 << (p % 64)) });
            }
        }
    }
    /// A data access found no shadow it could use (`old`: the descriptor there) and its walk succeeded.
    pub fn missed(user: bool, va: u64, write: bool, global: bool, old: u64) {
        if old & 1 != 0 && !(write && old & natemu_abi::shadow::READ_ONLY != 0) {
            return;
        }
        let slot = data_slot(user, va, global);
        let refill = held(slot, va, 2 * !user as usize + write as usize);
        count()[DATA + if !refill { FIRST } else if slot == GLOBAL_SET { REFILL_GLOBAL } else { REFILL }] += 1;
        head().flags |= if refill { 1 } else { 4 };
    }
    pub fn installed(user: bool, va: u64, writable: bool, global: bool) {
        let slot = data_slot(user, va, global);
        hold(slot, va, 2 * !user as usize);
        if writable {
            hold(slot, va, 2 * !user as usize + 1);
        }
    }
    /// A block not current, last current in epoch `checked` (0: cut since), was found mapped as before.
    pub fn revalidated(checked: u64, global: bool) {
        let h = head();
        let refill = !global
            && checked != 0
            && cache::control().epoch - checked < RING as u64
            && unsafe { ring(checked).read() } as u64 == h.current
            && checked >= h.born[h.current as usize];
        count()[CODE + if refill { REFILL } else { FIRST }] += 1;
        h.flags |= if refill { 2 } else { 8 };
    }
    /// Every bucket switch (`from` ran for `ticks`): a resident entry runs from leaving `GUEST` to entering it.
    #[inline(always)]
    pub fn switched(from: u64, to: u64, t: u64, ticks: u64) {
        let h = head();
        if from == GUEST {
            (h.entry, h.flags, h.kind, h.kept, h.own) = (t, 0, to, 0, 0);
            return;
        }
        if 1 << from.min(63) & KEPT != 0 {
            h.kept += ticks;
        }
        if 1 << from.min(63) & OWN != 0 {
            h.own += ticks;
        }
        if to == GUEST && h.flags != 0 {
            let k = if h.flags & 2 != 0 {
                ENTRY_CODE
            } else if h.flags & 1 != 0 {
                ENTRY_DATA
            } else if h.flags & 8 != 0 {
                ENTRY_CODE_FIRST
            } else {
                ENTRY_DATA_FIRST
            };
            let gone = h.kind == MISS || h.kind == LINK || h.kind == ABORT;
            count()[k] += 1;
            count()[k + 1] += if gone { t.wrapping_sub(h.entry).saturating_sub(h.kept) } else { h.own };
            count()[k + 2] += h.own;
            h.flags = 0;
        }
    }
}
#[cfg(feature = "pcid-study")]
pub use on::*;

#[cfg(not(feature = "pcid-study"))]
mod off {
    #[inline(always)]
    pub fn cr3(_: u64, _: u64, _: impl FnOnce() -> bool) {}
    #[inline(always)]
    pub fn flushed(_: bool, _: u64) {}
    #[inline(always)]
    pub fn holding(_: bool) {}
    #[inline(always)]
    pub fn reset() {}
    #[inline(always)]
    pub fn invalidated(_: u64, _: bool) {}
    #[inline(always)]
    pub fn missed(_: bool, _: u64, _: bool, _: bool, _: u64) {}
    #[inline(always)]
    pub fn installed(_: bool, _: u64, _: bool, _: bool) {}
    #[inline(always)]
    pub fn revalidated(_: u64, _: bool) {}
}
#[cfg(not(feature = "pcid-study"))]
pub use off::*;

/// Translated-code profile (`profile` builds): [`sample`] at each interrupt taken in generated code. Counters (64 words at
/// the study part's start) and a table of the blocks hit most (keyed by frame, page offset and privilege, so one shared
/// translation is one entry whatever addresses it runs at), read by the host at the vCPU's end
/// (`natemu_abi::execution::cache::profile`).
#[cfg(feature = "profile")]
pub mod prof {
    use crate::cache;
    use natemu_abi::execution::cache::{profile::*, CODE_VA, CODE_WRITE_VA, DEAD, DISPATCH_VA, HEAD_BYTES, PCID_STUDY, PIC, SLOT_BYTES, SLOT_VA, STEP, SYSTEM};
    fn counters() -> &'static mut [u64; COUNTERS] {
        // SAFETY: this vCPU's lookup area; only its resident writes the study part.
        unsafe { &mut *cache::lookup_area::<[u64; COUNTERS]>(PCID_STUDY) }
    }
    fn entry(i: usize) -> &'static mut Hot {
        unsafe { &mut *cache::lookup_area::<Hot>(PCID_STUDY + 4096).add(i) }
    }
    /// The class of A64 instruction word `w` ([`CLASS_NAMES`]).
    fn class(w: u32) -> usize {
        match w >> 25 & 0xf {
            8 | 9 => 5,
            10 | 11 => {
                if w & 0x7c00_0000 == 0x1400_0000 {
                    8
                } else if w & 0xff00_0010 == 0x5400_0000 || w & 0x7e00_0000 == 0x3400_0000 || w & 0x7e00_0000 == 0x3600_0000 {
                    7
                } else if w & 0xfe00_0000 == 0xd600_0000 {
                    9
                } else if w & 0xff00_0000 == 0xd400_0000 {
                    10
                } else if w & 0xffc0_0000 == 0xd500_0000 {
                    11
                } else {
                    14
                }
            }
            4 | 6 | 12 | 14 => {
                if w >> 26 & 1 != 0 {
                    2
                } else if w & 0x3b00_0000 == 0x0800_0000 || w & 0x3b20_0c00 == 0x3820_0000 {
                    3
                } else if w >> 22 & 1 != 0 {
                    0
                } else {
                    1
                }
            }
            5 | 13 => {
                if w & 0x1fe0_0000 == 0x1a80_0000 || w & 0x1fe0_0000 == 0x1a40_0000 {
                    6
                } else if w & 0x1f00_0000 == 0x1b00_0000 || w & 0x5fe0_f000 == 0x1ac0_0000 {
                    13
                } else {
                    4
                }
            }
            7 | 15 => 12,
            _ => 14,
        }
    }
    /// One sample: generated code was interrupted before the instruction at `elr`.
    pub fn sample(elr: u64, x1: u64) {
        let c = counters();
        c[TOTAL] += 1;
        if elr == c[RESUME_PC] {
            c[RESUMED] += 1;
            return;
        }
        if (DISPATCH_VA..CODE_VA + HEAD_BYTES as u64).contains(&elr) {
            c[HEAD] += 1;
            return;
        }
        if (SLOT_VA..SLOT_VA + SLOT_BYTES as u64).contains(&elr) {
            c[STEP_SLOT] += 1;
            return;
        }
        let Some(b) = cache::block_at(elr).filter(|b| b.flags & DEAD == 0) else {
            c[NO_BLOCK] += 1;
            return;
        };
        c[BLOCK] += 1;
        let user = cache::user(b.context);
        c[if user { USER } else { KERNEL }] += 1;
        c[if b.flags & (SYSTEM | STEP) != 0 { OTHER_KIND } else if b.flags & PIC != 0 { SHARED } else { ABSOLUTE }] += 1;
        let offset = crate::cache::native_offset(b, elr);
        if b.hot_size != 0 {
            c[if crate::cache::native_offset(b, elr) == elr.wrapping_sub(b.code_va()) { SPLIT_COLD } else { SPLIT_HOT }] += 1;
        }
        let exits = b.links.iter().filter(|p| p.offset != 0).map(|p| u64::from(p.offset)).min().unwrap_or(u64::MAX);
        c[if offset < 12 { PROLOGUE } else if offset >= exits { EXIT } else { BODY }] += 1;
        let words = offset.wrapping_sub(u64::from(natemu_abi::execution::ENTRY_OFFSET)) / 4;
        c[AT_ENTRY + match words { 0 => 0, 1 => 1, 2 | 3 => 2, _ => 3 }] += 1;
        if words == 0 || offset < u64::from(natemu_abi::execution::ENTRY_OFFSET) {
            c[if elr.wrapping_sub(x1) <= 8 { ENTRY_DISPATCHED } else { ENTRY_LINKED }] += 1;
        }
        // SAFETY: a word of the block's code, through the resident's alias of the code area.
        let w = unsafe { ((CODE_WRITE_VA + (elr - CODE_VA)) as *const u32).read_volatile() };
        c[if user { CLASS_USER } else { CLASS_KERNEL } + class(w)] += 1;
        let key = b.source_gpa[0] & !0xfff | b.rip & 0xfff | 1 << 62 | (user as u64) << 63;
        let mut i = (key.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> 46) as usize;
        for _ in 0..32 {
            let e = entry(i);
            if e.key == 0 {
                e.key = key;
                e.guest_bytes = b.guest_bytes as u16;
                let n = (b.guest_bytes as usize).min(24).min(4096 - (b.rip & 0xfff) as usize);
                if let Some(p) = crate::execution::guest_ram().range(b.source_gpa[0] & !0xfff | b.rip & 0xfff, n as u64) {
                    // SAFETY: `n` bytes of guest RAM inside one page.
                    unsafe { core::ptr::copy_nonoverlapping(p, e.bytes.as_mut_ptr(), n) };
                }
            }
            if e.key == key {
                e.count += 1;
                (e.code, e.code_size, e.flushes, e.rip) = (b.start(), b.code_size + (b.code - b.start()), cache::control().flushes as u32, b.rip);
                return;
            }
            i = (i + 1) & (HOT_ENTRIES - 1);
        }
        c[HOT_FULL] += 1;
    }
}
