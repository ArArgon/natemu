//! First-trial postcommit cohort, then real stores through the remapped page.
use crate::{
    NativeFrame,
    concurrency::{Coordinator, EventRecord},
    recovery::GuestState,
    families::schedule,
};
pub const MAGIC: u64 = 0x4154_4f4d_5052_0001;
pub const OFFSET: usize = 0x1c0000;
pub const BACKING_OFFSET: usize = 0xc0000;
pub const BYTES: usize = 0x20000;
pub const IMAGE_BYTES: usize = 12288;
pub const NEW_RESULTS: u64 = 0x103000;
pub const SPARE_OFFSET: usize = crate::families::ordering::RAM_OFFSET + 12288;
pub const POST: u64 = 80;
pub const BLOCKED_EVENT: u64 = 81;
pub const REMAP_REQUEST: u64 = 82;
pub const RETRY_STORE: u64 = 83;
pub const RETIRED_STORE: u64 = 84;
pub const MUTATE: u64 = 85;
pub const ACK: u64 = 86;
pub const ENTRY_BLOCKED: u64 = 87;
pub const OPENED: u64 = 88;
pub const DELIVER: u64 = 89;
pub const ADMIT_BASE: u64 = 0x1000;
pub const RETURN_BASE: u64 = 0x2000;
#[derive(Clone, Copy)]
#[repr(C)]
pub struct Walk {
    pub address: u64,
    pub gpa: u64,
    pub leaf_bytes: u64,
    pub writable: u64,
    pub dirty: u64,
    pub read_count: u64,
    pub writes: u64,
    pub reads: [[u64; 2]; 4],
}
impl Walk {
    pub const ZERO: Self = Self {
        address: 0,
        gpa: 0,
        leaf_bytes: 0,
        writable: 0,
        dirty: 0,
        read_count: 0,
        writes: 0,
        reads: [[0; 2]; 4],
    };
}
#[derive(Clone, Copy)]
#[repr(C)]
pub struct Owner {
    pub saved_gpa: u64,
    pub saved_leaf: u64,
    pub fault_pc: u64,
    pub resume_pc: u64,
    pub queued: u64,
    pub resumed: u64,
    pub completed: u64,
    pub ack_leaf: u64,
    pub new_leaf: u64,
    pub fault_frame: NativeFrame,
    pub before_guest: GuestState,
    pub completed_guest: GuestState,
    pub event: EventRecord,
    pub walk: Walk,
}
impl Owner {
    pub fn new(frame: &NativeFrame, id: usize) -> Self {
        let mut guest = GuestState::seed(frame);
        guest.rip = crate::families::ordering::RIP + id as u64 * 256;
        let mut event = EventRecord::new(frame, id);
        event.shadow = 0;
        event.priority = 0;
        Self {
            saved_gpa: 0,
            saved_leaf: 0,
            fault_pc: 0,
            resume_pc: 0,
            queued: 0,
            resumed: 0,
            completed: 0,
            ack_leaf: 0,
            new_leaf: 0,
            fault_frame: NativeFrame::ZERO,
            before_guest: guest,
            completed_guest: guest,
            event,
            walk: Walk::ZERO,
        }
    }
}
#[repr(C)]
pub struct State {
    pub magic: u64,
    pub n: u64,
    pub actor: u64,
    pub writer: u64,
    pub parked: u64,
    pub requested: u64,
    pub drained: u64,
    pub mutated: u64,
    pub acknowledged: u64,
    pub blocked: u64,
    pub opened: u64,
    pub delivered: u64,
    pub snapshot_count: u64,
    pub coordinator: Coordinator,
    pub owners: [Owner; 4],
    /// Stable cuts: all RMWs committed/stores pending; all old stores drained;
    /// remap published. Images concatenate DATA, old RESULTS and spare page.
    pub cuts: [[u8; IMAGE_BYTES]; 3],
    pub tables_before: [u8; 16384],
    pub tables_after: [u8; 16384],
}
impl State {
    pub fn new(config: schedule::Config, frames: &[NativeFrame; 4]) -> Option<Self> {
        if !config.valid() || config.family != schedule::ATOMIC {
            return None;
        }
        Some(Self {
            magic: MAGIC,
            n: config.n,
            actor: config.actor,
            writer: (config.actor + 1) % config.n,
            parked: 0,
            requested: 0,
            drained: 0,
            mutated: 0,
            acknowledged: 0,
            blocked: 0,
            opened: 0,
            delivered: 0,
            snapshot_count: 0,
            coordinator: Coordinator::new(config.n as usize)?,
            owners: core::array::from_fn(|i| Owner::new(&frames[i], i)),
            cuts: [[0; IMAGE_BYTES]; 3],
            tables_before: [0; 16384],
            tables_after: [0; 16384],
        })
    }
}
const _: () = assert!(core::mem::size_of::<State>() <= BYTES);
const _: () = assert!(OFFSET + BYTES == schedule::OFFSET);
const _: () =
    assert!(crate::families::ordering::IMAGE_VA_OFFSET + crate::families::ordering::TRIALS * IMAGE_BYTES <= OFFSET);
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn supplementary_state_and_images_do_not_overlap_scheduler_or_source() {
        assert!(IMAGE_BYTES * crate::families::ordering::TRIALS <= BACKING_OFFSET);
        assert_eq!(SPARE_OFFSET + 4096, crate::families::ordering::TABLE_OFFSET);
        for n in 1..=4 {
            for ordinal in 0..4 {
                let c = schedule::Config::new(schedule::ATOMIC, n, ordinal).unwrap();
                let state = State::new(c, &[NativeFrame::ZERO; 4]).unwrap();
                assert_eq!(state.writer, (c.actor + 1) % n);
                assert_eq!(state.coordinator.participants, n);
                for id in 0..n as usize {
                    assert_eq!(
                        state.owners[id].before_guest.rip,
                        crate::families::ordering::RIP + id as u64 * 256
                    );
                }
            }
        }
        assert!(
            State::new(
                schedule::Config::new(schedule::COHERENCE, 1, 0).unwrap(),
                &[NativeFrame::ZERO; 4]
            )
            .is_none()
        );
    }
}
