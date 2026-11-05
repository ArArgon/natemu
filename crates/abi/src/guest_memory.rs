//! Guest-physical memory as shared external RAM. Other vCPUs and the host may
//! change it at any time, so access is volatile or atomic through raw pointers;
//! no Rust reference ever covers it.
use core::{
    arch::asm,
    mem::MaybeUninit,
    ptr,
    sync::atomic::{
        AtomicU64,
        Ordering::{Relaxed, SeqCst},
    },
};

use crate::paging::{self, Update};

/// Copy bytes without LLVM expanding aggregate volatile loads into thousands of
/// byte instructions. MaybeUninit preserves structure padding.
///
/// # Safety
/// Both ranges must be valid for `count` bytes.
#[inline(never)]
pub unsafe fn copy_volatile(dst: *mut u8, src: *const u8, count: usize) {
    for i in 0..count {
        unsafe {
            let b = ptr::read_volatile(src.add(i).cast::<MaybeUninit<u8>>());
            ptr::write_volatile(dst.add(i).cast::<MaybeUninit<u8>>(), b);
        }
    }
}

/// An 8/16/32/64-bit guest memory value.
pub trait Word: Copy {}
impl Word for u8 {}
impl Word for u16 {}
impl Word for u32 {}
impl Word for u64 {}

/// Guest-physical RAM as up to four disjoint regions, each `size` bytes from
/// `base` aliased at resident address `va` (RAM may have holes). Accesses
/// outside one region or misaligned for their width are refused.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct GuestRam {
    regions: [[u64; 3]; 4],
    count: usize,
}

impl GuestRam {
    /// # Safety
    /// `[va, va + size)` must be resident memory backing `[base, base + size)`
    /// for as long as this value is used. `va` and `base` must agree modulo 8,
    /// so guest alignment is pointer alignment for the atomic accesses.
    pub const unsafe fn new(base: u64, size: u64, va: u64) -> Self {
        unsafe {
            Self {
                regions: [[0; 3]; 4],
                count: 0,
            }
            .and(base, size, va)
        }
    }
    /// Adds a region disjoint from the others.
    ///
    /// # Safety
    /// As for `new`.
    pub const unsafe fn and(mut self, base: u64, size: u64, va: u64) -> Self {
        assert!(va % 8 == base % 8, "guest RAM alias misaligned");
        assert!(
            self.count < self.regions.len(),
            "too many guest RAM regions"
        );
        self.regions[self.count] = [base, size, va];
        self.count += 1;
        self
    }
    /// Resident pointer to `bytes` in-bounds bytes at `gpa`.
    pub fn range(&self, gpa: u64, bytes: u64) -> Option<*mut u8> {
        self.regions[..self.count]
            .iter()
            .find_map(|&[base, size, va]| {
                let offset = gpa.checked_sub(base)?;
                (offset.checked_add(bytes)? <= size).then_some((va + offset) as *mut u8)
            })
    }
    fn word<T>(&self, gpa: u64) -> Option<*mut T> {
        let bytes = size_of::<T>() as u64;
        (gpa & (bytes - 1) == 0)
            .then(|| self.range(gpa, bytes))?
            .map(|p| p.cast())
    }
    pub fn read<T: Word>(&self, gpa: u64) -> Option<T> {
        // SAFETY: the owner maps [base, base + size) at va for this RAM's lifetime.
        self.word(gpa).map(|p| unsafe { ptr::read_volatile(p) })
    }
    /// Store and complete the write before later accesses or maintenance.
    #[must_use]
    pub fn write<T: Word>(&self, gpa: u64, value: T) -> bool {
        let Some(p) = self.word(gpa) else {
            return false;
        };
        unsafe {
            ptr::write_volatile(p, value);
            asm!("dsb ish");
        }
        true
    }
    /// Single-copy-atomic load for entries other agents may update concurrently.
    pub fn load64(&self, gpa: u64) -> Option<u64> {
        let p = self.word::<u64>(gpa)?;
        Some(unsafe { AtomicU64::from_ptr(p) }.load(Relaxed))
    }
    /// `Some(Ok(current))` when swapped, `Some(Err(actual))` when RAM held
    /// another value, `None` when out of bounds or misaligned.
    pub fn compare_exchange64(&self, gpa: u64, current: u64, new: u64) -> Option<Result<u64, u64>> {
        let p = self.word::<u64>(gpa)?;
        let result =
            unsafe { AtomicU64::from_ptr(p) }.compare_exchange(current, new, SeqCst, SeqCst);
        unsafe { asm!("dsb ish") };
        Some(result)
    }
    /// Store `bytes` at `gpa` byte by byte (no single-copy atomicity), then complete.
    #[must_use]
    pub fn copy_in(&self, gpa: u64, bytes: &[u8]) -> bool {
        let Some(p) = self.range(gpa, bytes.len() as u64) else {
            return false;
        };
        unsafe {
            copy_volatile(p, bytes.as_ptr(), bytes.len());
            asm!("dsb ish");
        }
        true
    }
    /// Whether the guest bytes at `gpa` equal `bytes` (false when out of bounds).
    pub fn matches(&self, gpa: u64, bytes: &[u8]) -> bool {
        self.range(gpa, bytes.len() as u64).is_some_and(|p| {
            (bytes.iter().enumerate()).all(|(i, &b)| unsafe { ptr::read_volatile(p.add(i)) } == b)
        })
    }
    /// Copy `out.len()` bytes starting at `gpa`; false leaves `out` unchanged.
    pub fn copy_out(&self, gpa: u64, out: &mut [u8]) -> bool {
        let Some(p) = self.range(gpa, out.len() as u64) else {
            return false;
        };
        unsafe { copy_volatile(out.as_mut_ptr(), p, out.len()) };
        true
    }
}

/// Optional instrumentation of page-table accesses a test driver may enable.
/// Returning false refuses the access.
pub trait Observer {
    fn read(&mut self, _gpa: u64, _value: u64) -> bool {
        true
    }
    fn write(&mut self, _gpa: u64, _old: u64, _new: u64) -> bool {
        true
    }
    /// The entry at `gpa` was written (or already held the value the walker wanted).
    fn wrote(&mut self, _gpa: u64) {}
}
impl Observer for () {}

/// The global code-page mask as the walker's A/D writes must respect it (natemu's cross-vCPU code coherence).
pub trait CodePages {
    /// Some vCPU holds translations from `gpa`'s page.
    fn translated(&mut self, gpa: u64) -> bool;
    /// `gpa`'s page, which holds translations, is about to be written: clears its code bits for every vCPU and posts
    /// the page to the others, or does not return (the resident single-steps the instruction instead).
    fn code_write(&mut self, gpa: u64);
    /// `gpa`'s page was written: posts it to any vCPU that marked it since the code bits were last cleared.
    fn rechecked(&mut self, gpa: u64);
}
/// A walk observer applying [`CodePages`]: an A/D write into a page holding translations takes `code_write` first and
/// is then made, never refused, so no walk repeats because of a code page, whoever marks the page again meanwhile;
/// every write is re-checked after it is made.
pub struct CodeWrites<C>(pub C);
impl<C: CodePages> Observer for CodeWrites<C> {
    fn write(&mut self, gpa: u64, _: u64, _: u64) -> bool {
        if self.0.translated(gpa) {
            self.0.code_write(gpa);
        }
        true
    }
    fn wrote(&mut self, gpa: u64) {
        self.0.rechecked(gpa);
    }
}

/// Guest page tables in `ram` for the paging, PAE and legacy 32-bit walkers.
/// A walker writes an entry only from the value it read (to set A/D), by
/// compare-exchange. If another agent stored the same value meanwhile the
/// update stands; any other change makes the walker re-walk.
pub struct GuestTables<O = ()> {
    pub ram: GuestRam,
    pub observer: O,
}

impl GuestTables {
    pub const fn new(ram: GuestRam) -> Self {
        Self::observed(ram, ())
    }
}
impl<O: Observer> GuestTables<O> {
    pub const fn observed(ram: GuestRam, observer: O) -> Self {
        Self { ram, observer }
    }
    fn observe(&mut self, gpa: u64, value: Option<u64>) -> Option<u64> {
        value.filter(|&v| self.observer.read(gpa, v))
    }
    fn update(&mut self, gpa: u64, old: u64, new: u64) -> Update {
        if !self.observer.write(gpa, old, new) {
            return Update::Refused;
        }
        match self.ram.compare_exchange64(gpa, old, new) {
            Some(Ok(_)) => {}
            Some(Err(v)) if v == new => {}
            Some(Err(_)) => return Update::Changed,
            None => return Update::Refused,
        }
        self.observer.wrote(gpa);
        Update::Written
    }
}
impl<O: Observer> paging::Tables for GuestTables<O> {
    fn read(&mut self, gpa: u64) -> Option<u64> {
        let value = self.ram.load64(gpa);
        self.observe(gpa, value)
    }
    fn write(&mut self, gpa: u64, old: u64, new: u64) -> Update {
        self.update(gpa, old, new)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::paging::{Access, Control, Fault, Tables};

    struct Backing([u64; 2048]);
    impl Backing {
        // Guest-physical 0x1000.. maps here; the walk below uses four tables.
        fn ram(&mut self) -> GuestRam {
            unsafe { GuestRam::new(0x1000, 0x4000, self.0.as_mut_ptr() as u64) }
        }
    }
    fn tables() -> Backing {
        let mut b = Backing([0; 2048]);
        b.0[0] = 0x2007;
        b.0[512] = 0x3007;
        b.0[1024] = 0x4007;
        b.0[1536 + 256] = 0x10_0007;
        b
    }
    const CONTROL: Control = Control {
        cr3: 0x1000,
        cpl: 0,
        wp: 1,
        nxe: 1,
        smep: 0,
    };

    #[test]
    fn bounds_alignment_and_widths() {
        let mut b = tables();
        let ram = b.ram();
        assert_eq!(ram.read::<u64>(0x1000), Some(0x2007));
        assert_eq!(ram.read::<u8>(0x1001), Some(0x20));
        assert_eq!(ram.read::<u16>(0x1001), None);
        assert_eq!(ram.read::<u32>(0x4ffc), Some(0));
        assert_eq!(ram.read::<u64>(0x5000), None);
        assert_eq!(ram.read::<u64>(0xff8), None);
        assert!(ram.write::<u16>(0x1002, 0xbeef));
        assert_eq!(ram.read::<u64>(0x1000), Some(0xbeef_2007));
        assert_eq!(ram.compare_exchange64(0x1000, 1, 2), Some(Err(0xbeef_2007)));
        assert_eq!(
            ram.compare_exchange64(0x1000, 0xbeef_2007, 3),
            Some(Ok(0xbeef_2007))
        );
        assert_eq!(ram.compare_exchange64(0x1004, 0, 1), None);
        let mut out = [0; 4];
        assert!(ram.copy_out(0x1000, &mut out));
        assert_eq!(out, [3, 0, 0, 0]);
        assert!(!ram.copy_out(0x4ffe, &mut out));
        assert!(ram.copy_in(0x1001, &[7, 8]) && !ram.copy_in(0x4fff, &[0, 0]));
        assert!(ram.matches(0x1000, &[3, 7, 8, 0]));
        assert!(ram.copy_in(0x1001, &[0, 0]));
        assert!(!ram.copy_out(0x4fff, &mut [0; 2]));
        assert!(ram.matches(0x1000, &[3, 0]) && !ram.matches(0x1000, &[3, 1]));
        assert!(!ram.matches(0x4fff, &[0, 0]));
    }

    #[test]
    fn regions_are_separate_and_accesses_do_not_span_them() {
        let mut b = tables();
        let p = b.0.as_mut_ptr() as u64;
        let ram = unsafe { GuestRam::new(0x1000, 0x1000, p).and(0x10_0000, 0x1000, p + 0x2000) };
        assert!(ram.write::<u64>(0x10_0000, 9));
        assert_eq!(b.0[1024], 9);
        assert_eq!(ram.read::<u64>(0x1000), Some(0x2007));
        assert_eq!(ram.read::<u64>(0x2000), None);
        assert!(!ram.copy_out(0x1fff, &mut [0; 2]));
        assert_eq!(ram.read::<u64>(0x10_1000), None);
    }

    #[test]
    fn walk_sets_accessed_dirty_through_ram() {
        let mut b = tables();
        let mut t = GuestTables::new(b.ram());
        let r = paging::walk(&mut t, CONTROL, 0x10_0000, Access::Write, true).unwrap();
        assert_eq!(r.gpa, 0x10_0000);
        assert_eq!(b.0[0], 0x2027);
        assert_eq!(b.0[1536 + 256], 0x10_0067);
    }


    #[test]
    fn concurrent_entry_change_restarts_the_walk() {
        let mut b = tables();
        let ram = b.ram();
        let mut t = GuestTables::new(ram);
        // A stale `old` fails without overwriting the newer entry.
        assert!(ram.write::<u64>(0x1000, 0x2005));
        assert_eq!(t.write(0x1000, 0x2007, 0x2027), Update::Changed);
        assert_eq!(b.0[0], 0x2005);
        // Another agent already stored the intended value: success.
        assert_eq!(t.write(0x1000, 0x2005, 0x2005 | 0x20), Update::Written);
        assert!(ram.write::<u64>(0x1000, 0x2025));
        assert_eq!(t.write(0x1000, 0x2005, 0x2025), Update::Written);
        assert_eq!(b.0[0], 0x2025);
    }

    #[test]
    fn racing_write_rewalks_and_identical_ad_is_success() {
        // A racing writer that changes a non-A/D bit forces a second walk,
        // which sees and keeps the new value.
        struct Race {
            ram: GuestRam,
            writes: usize,
            racer: u64,
        }
        impl Observer for Race {
            fn write(&mut self, gpa: u64, old: u64, _: u64) -> bool {
                self.writes += 1;
                if self.writes == 1 {
                    assert!(self.ram.write::<u64>(gpa, old | self.racer));
                }
                true
            }
        }
        for (racer, writes, root) in [
            // Non-A/D change (PWT): re-walk; the root keeps it.
            (0x8, 2, 0x202f),
            // Identical A/D (another vCPU set A first): success, no re-walk.
            (0x20, 1, 0x2027),
        ] {
            let mut b = tables();
            let ram = b.ram();
            let observer = Race {
                ram,
                writes: 0,
                racer,
            };
            let mut t = GuestTables::observed(ram, observer);
            let r = paging::walk(&mut t, CONTROL, 0x10_0000, Access::Read, true);
            assert_eq!(r.map(|r| r.gpa), Ok(0x10_0000));
            assert_eq!(t.observer.writes, writes + 3);
            assert_eq!(b.0[0], root);
        }
    }

    #[test]
    fn endless_race_is_contained() {
        struct Always(GuestRam);
        impl Observer for Always {
            fn write(&mut self, gpa: u64, old: u64, _: u64) -> bool {
                assert!(self.0.write::<u64>(gpa, old ^ 0x8));
                true
            }
        }
        let mut b = tables();
        let ram = b.ram();
        let mut t = GuestTables::observed(ram, Always(ram));
        assert_eq!(
            paging::walk(&mut t, CONTROL, 0x10_0000, Access::Read, true),
            Err(Fault::Contended)
        );
    }

    #[test]
    fn observer_sees_and_may_refuse_accesses() {
        #[derive(Default)]
        struct Log(usize, usize);
        impl Observer for Log {
            fn read(&mut self, _: u64, _: u64) -> bool {
                self.0 += 1;
                self.0 <= 4
            }
            fn write(&mut self, _: u64, _: u64, _: u64) -> bool {
                self.1 += 1;
                false
            }
        }
        let mut b = tables();
        let mut t = GuestTables::observed(b.ram(), Log::default());
        assert_eq!(
            paging::walk(&mut t, CONTROL, 0x10_0000, Access::Read, true),
            Err(Fault::Unsupported)
        );
        assert_eq!((t.observer.0, t.observer.1), (1, 1));
        assert_eq!(b.0[0], 0x2007);
        // Pre-set A on every level: the walk reads four entries and writes only the leaf.
        for i in [0, 512, 1024, 1536 + 256] {
            b.0[i] |= paging::ACCESSED;
        }
        let mut t = GuestTables::observed(b.ram(), Log::default());
        assert!(paging::walk(&mut t, CONTROL, 0x10_0000, Access::Write, true).is_err());
        assert_eq!((t.observer.0, t.observer.1), (4, 1));
    }

    /// A walk whose A writes land in pages holding a peer's translations, the peer marking each page again right after
    /// every clear (`translated` stays true): each write into such a page takes `code_write` once and is then made, the
    /// walk is never repeated (no livelock, and no refusal: S2e's refuse-and-rewalk could repeat for as long as a peer
    /// re-marked), and every write is re-checked after it is made.
    #[test]
    fn writes_into_code_pages_never_repeat_the_walk() {
        struct Peer {
            code: [u64; 2],
            events: [(u8, u64); 8],
            n: usize,
        }
        impl CodePages for Peer {
            fn translated(&mut self, gpa: u64) -> bool {
                self.code.contains(&(gpa & !4095))
            }
            fn code_write(&mut self, gpa: u64) {
                self.events[self.n] = (b'c', gpa);
                self.n += 1;
            }
            fn rechecked(&mut self, gpa: u64) {
                self.events[self.n] = (b'r', gpa);
                self.n += 1;
            }
        }
        let mut b = tables();
        let peer = Peer { code: [0x1000, 0x4000], events: [(0, 0); 8], n: 0 };
        let mut t = GuestTables::observed(b.ram(), CodeWrites(peer));
        assert_eq!(paging::walk(&mut t, CONTROL, 0x10_0000, Access::Read, true).map(|r| r.gpa), Ok(0x10_0000));
        let p = &t.observer.0;
        let want = [(b'c', 0x1000), (b'r', 0x1000), (b'r', 0x2000), (b'r', 0x3000), (b'c', 0x4800), (b'r', 0x4800)];
        assert_eq!(&p.events[..p.n], &want);
        assert_eq!((b.0[0], b.0[512], b.0[1024], b.0[1536 + 256]), (0x2027, 0x3027, 0x4027, 0x10_0027));
    }
}
