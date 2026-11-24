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
