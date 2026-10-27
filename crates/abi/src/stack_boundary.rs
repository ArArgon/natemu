//! Terminal and split-stack wave after the unchanged concurrent stack pressure run.
use crate::{NativeFrame, concurrency::Coordinator, paging::Update, recovery::GuestState};
pub const OWNER_OFFSET: usize = 0x84000;
pub const SHARED_OFFSET: usize = 0x93000;
pub const STAGE: u64 = 5;
pub const ENABLE_OFFSET: usize = 0x2ff00;
pub const SS: u64 = 1;
pub const GP: u64 = 2;
pub const SPLIT: u64 = 3;
pub const SPLIT_NP: u64 = 4;
pub const SPLIT_RO: u64 = 5;
pub const BOUNDARY: u64 = crate::shadow::HIGH_BASE + crate::DATA_VA + 0x5000;
/// Only a direct store translation abort has the valid operand address required
/// by the saved-route and split-store continuations.
pub fn direct_store_translation_abort(esr: u64) -> bool {
    esr >> 26 == 0x25
        && esr & (1 << 6) != 0
        && esr & ((1 << 7) | (1 << 8) | (1 << 10)) == 0
        && matches!(esr & 63, 4..=7)
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Fragment {
    pub linear: u64,
    pub bytes: u64,
    pub gpa: u64,
    pub leaf: u64,
    pub before: u64,
    pub after: u64,
    pub ready: u64,
}
impl Fragment {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Owner {
    pub before_frame: NativeFrame,
    pub before_guest: GuestState,
    pub lower_before: [u64; 512],
    pub upper_before: [u64; 512],
    pub initial: NativeFrame,
    pub initial_guest: GuestState,
    pub current: NativeFrame,
    pub prefixes: [NativeFrame; 5],
    pub entries: u64,
    pub completed: u64,
    pub step: u64,
    pub fault_frame: NativeFrame,
    pub esr: u64,
    pub far: u64,
    pub pc: u64,
    pub spsr: u64,
    pub faults: u64,
    pub guest_after: GuestState,
    pub vector: u64,
    pub error: u64,
    pub address: u64,
    pub rip: u64,
    pub saved_page: u64,
    pub saved_writable: u64,
    pub native_pair_before: [u64; 2],
    pub native_pair_after: [u64; 2],
    pub service_attempts: u64,
    pub service_completed: u64,
    pub service_pc: u64,
    pub service_resume_pc: u64,
    pub fragments: [Fragment; 2],
    pub data_before_service: [u8; 8],
    pub data_after_service: [u8; 8],
    pub dirty_before: [u64; 2],
    pub dirty_after: [u64; 2],
    pub ready_mask: u64,
    pub retained_at_outcome: u64,
    pub ack_pair: [u64; 2],
    pub released: u64,
}
impl Owner {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
pub struct Shared {
    pub n: u64,
    pub profile: u64,
    pub actor: u64,
    pub ready: u64,
    pub setup: u64,
    pub actor_done: u64,
    pub done: u64,
    pub remapped: u64,
    pub acknowledgements: u64,
    pub released: u64,
    pub reclaimed: u64,
    pub reuse_blocked: u64,
    pub entry_rejected: u64,
    pub retained_before_release: u64,
    pub trace_start: u64,
    pub finished_before: u64,
    pub old_page: u64,
    pub other_page: u64,
    pub configured_leaves: [u64; 3],
    pub code_before: [u8; crate::stack::CODE_BYTES],
    pub code_at_outcome: [u8; crate::stack::CODE_BYTES],
    pub data_before: [u8; 8192],
    pub tables_before: [u8; 16384],
    pub wave: Coordinator,
}
impl Shared {
    pub fn new(n: usize, profile: u64, actor: usize) -> Option<Self> {
        if !(1..=4).contains(&n) || !(SS..=SPLIT_RO).contains(&profile) || actor >= n {
            return None;
        }
        Some(Self {
            n: n as u64,
            profile,
            actor: actor as u64,
            ready: 0,
            setup: 0,
            actor_done: 0,
            done: 0,
            remapped: 0,
            acknowledgements: 0,
            released: 0,
            reclaimed: 0,
            reuse_blocked: 0,
            entry_rejected: 0,
            retained_before_release: 0,
            trace_start: 0,
            finished_before: 0,
            old_page: 0,
            other_page: 0,
            configured_leaves: [0; 3],
            code_before: [0; crate::stack::CODE_BYTES],
            code_at_outcome: [0; crate::stack::CODE_BYTES],
            data_before: [0; 8192],
            tables_before: [0; 16384],
            wave: Coordinator::new(n)?,
        })
    }
}
pub fn actor_roots(profile: u64, rsp: u64, _rdx: u64) -> Option<(u64, u64)> {
    Some(match profile {
        SS => (crate::shadow::HIGH_BASE + 8, crate::shadow::HIGH_BASE + 8),
        GP => (rsp, crate::PRIVATE + 8),
        SPLIT | SPLIT_NP | SPLIT_RO => (BOUNDARY + 4, BOUNDARY + 8),
        _ => return None,
    })
}
const _: () = assert!(OWNER_OFFSET + core::mem::size_of::<Owner>() <= 0x8a000);
const _: () = assert!(SHARED_OFFSET + core::mem::size_of::<Shared>() <= 0xa0000);

/// Write permission checks retain A and existing D, while new leaf D waits for
/// whole-access readiness. The caller holds both routes until commit completes.
pub struct DeferredDirty<T> {
    pub tables: T,
    pending: [[u64; 3]; 2],
    count: usize,
}
impl<T: crate::paging::Tables> DeferredDirty<T> {
    pub fn new(tables: T) -> Self {
        Self {
            tables,
            pending: [[0; 3]; 2],
            count: 0,
        }
    }
    /// Set each deferred D by compare-exchange from the A-only value this walk
    /// wrote; an entry that already holds the final value counts. All entries
    /// are checked first, so a route changed before commit sets no D at all.
    pub fn commit(&mut self) -> bool {
        let pending = &self.pending[..self.count];
        pending
            .iter()
            .all(|p| matches!(self.tables.read(p[0]), Some(v) if v == p[1] || v == p[2]))
            && pending
                .iter()
                .all(|p| self.tables.write(p[0], p[1], p[2]) == Update::Written)
    }
}
impl<T: crate::paging::Tables> crate::paging::Tables for DeferredDirty<T> {
    fn read(&mut self, gpa: u64) -> Option<u64> {
        self.tables.read(gpa)
    }
    fn write(&mut self, gpa: u64, old: u64, new: u64) -> Update {
        let dirty = crate::paging::DIRTY;
        if new & dirty == 0 || old & dirty != 0 {
            return self.tables.write(gpa, old, new);
        }
        if self.count == 2 {
            return Update::Refused;
        }
        let masked = new & !dirty;
        let update = self.tables.write(gpa, old, masked);
        if update == Update::Written {
            self.pending[self.count] = [gpa, masked, new];
            self.count += 1;
        }
        update
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::paging::{self, Access, Control, Fault, Tables};
    #[test]
    fn indirect_cache_and_unknown_address_aborts_cannot_resume_a_store() {
        for level in 4..=7 {
            let direct = (0x25 << 26) | (1 << 6) | level;
            assert!(direct_store_translation_abort(direct));
            for forbidden in [7, 8, 10] {
                assert!(!direct_store_translation_abort(direct | (1 << forbidden)));
            }
            assert!(!direct_store_translation_abort(direct & !(1 << 6)));
        }
        for other in [(0x24 << 26) | 0x47, (0x25 << 26) | 0x4f] {
            assert!(!direct_store_translation_abort(other));
        }
    }
    struct Ram([u64; 2048]);
    impl Tables for Ram {
        fn read(&mut self, p: u64) -> Option<u64> {
            self.0.get((p / 8) as usize).copied()
        }
        fn write(&mut self, p: u64, old: u64, v: u64) -> Update {
            match self.0.get_mut((p / 8) as usize) {
                Some(a) if *a == old || *a == v => {
                    *a = v;
                    Update::Written
                }
                Some(_) => Update::Changed,
                None => Update::Refused,
            }
        }
    }
    fn fixture(second: u64) -> DeferredDirty<Ram> {
        let mut r = Ram([0; 2048]);
        r.0[0] = 0x1007;
        r.0[512] = 0x2007;
        r.0[1024] = 0x3007;
        r.0[1536 + 256] = 0x100007;
        r.0[1536 + 257] = second;
        DeferredDirty::new(r)
    }
    const C: Control = Control {
        cr3: 0,
        cpl: 0,
        wp: 1,
        nxe: 1,
        smep: 0,
    };
    #[test]
    fn second_fragment_failure_keeps_all_new_dirty_clear_and_checks_write_permissions() {
        for (pte, error) in [(0, 2), (0x101005, 3)] {
            let mut d = fixture(pte);
            assert!(paging::walk(&mut d, C, 0x100ffc, Access::Write).is_ok());
            assert_eq!(d.tables.0[1792] & paging::DIRTY, 0);
            assert_eq!(
                paging::walk(&mut d, C, 0x101000, Access::Write),
                Err(Fault::Page {
                    address: 0x101000,
                    error
                })
            );
            assert_eq!(d.tables.0[1792] & paging::DIRTY, 0);
            assert_eq!(d.tables.0[1793], pte);
        }
    }
    #[test]
    fn both_clean_routes_commit_together_and_stale_second_route_commits_neither() {
        for stale in [false, true] {
            let mut d = fixture(0x101007);
            for at in [0x100ffc, 0x101000] {
                assert!(paging::walk(&mut d, C, at, Access::Write).is_ok());
            }
            for index in [1792, 1793] {
                assert_eq!(d.tables.0[index] & paging::DIRTY, 0);
            }
            if stale {
                d.tables.0[1793] ^= 2;
            }
            assert_eq!(d.commit(), !stale);
            for index in [1792, 1793] {
                assert_eq!(d.tables.0[index] & paging::DIRTY != 0, !stale);
            }
        }
    }
    #[test]
    fn actor_fault_operands_are_noncanonical_before_native_access() {
        let (rsp, _) = actor_roots(SS, 0x100080, 0x100080).unwrap();
        assert!(paging::canonical_span(rsp - 8, 8));
        assert!(!paging::canonical_span(rsp - 10, 2));
        let (_, rdx) = actor_roots(GP, 0x100080, 0x100080).unwrap();
        assert!(!paging::canonical_span(rdx - 8, 8));
        let (rsp, rdx) = actor_roots(SPLIT, 0, 0).unwrap();
        assert_eq!((rsp - 8) & 4095, 4092);
        assert_eq!((rdx - 8) & 7, 0);
        assert_eq!((rdx - 12) & 3, 0);
    }
    #[test]
    fn deferred_write_passes_the_walked_value_and_commit_accepts_identical_d() {
        let mut d = fixture(0x101007);
        // A stale `old` is not replaced by a value built from it.
        assert_eq!(d.write(1793 * 8, 0x101005, 0x101067), Update::Changed);
        assert_eq!(d.tables.0[1793], 0x101007);
        for at in [0x100ffc, 0x101000] {
            assert!(paging::walk(&mut d, C, at, Access::Write).is_ok());
        }
        // Another agent already set the same D on one leaf.
        d.tables.0[1792] |= paging::DIRTY;
        assert!(d.commit());
        assert_ne!(d.tables.0[1793] & paging::DIRTY, 0);
    }
    #[test]
    fn successful_readiness_defers_only_new_dirty_until_both_routes_valid() {
        let mut d = fixture(0x101007);
        d.tables.0[1792] |= paging::DIRTY;
        for at in [0x100ffc, 0x101000] {
            assert!(paging::walk(&mut d, C, at, Access::Write).is_ok());
        }
        assert_ne!(d.tables.0[1792] & paging::DIRTY, 0);
        assert_eq!(d.tables.0[1793] & paging::DIRTY, 0);
        assert!(d.commit());
        assert_ne!(d.tables.0[1793] & paging::DIRTY, 0);
    }
}

const _: () =
    assert!(crate::alias::OFFSET + core::mem::size_of::<crate::alias::Record>() <= ENABLE_OFFSET);
