//! Injection configuration and an immutable, Release-published native witness.
//! The reservation is shared with atomic-progress only across disjoint VMs.
use crate::{NativeFrame, PRIVATE, recovery::GuestState, families::schedule, translation::Packet};
use core::sync::atomic::AtomicU64;
pub const MAGIC: u64 = 0x434f_4e54_4149_4e01;
pub const VERSION: u64 = 1;
pub const OFFSET: usize = 0x1c0000;
pub const VA: u64 = PRIVATE + OFFSET as u64;
pub const BACKING_OFFSET: usize = 0xc0000;
pub const BYTES: usize = 0x20000;
pub const WORKER_EXIT: u64 = 1;
pub const MALFORMED: u64 = 2;
pub const LOCKED_PANIC: u64 = 3;
pub const WITHHELD_ACK: u64 = 4;
pub const CANCEL: u64 = 5;
pub const PEEK_DEFERRED: u64 = 83;
pub const PEEK_CONTAINER: u64 = 85;
pub const PRODUCER_READY: u64 = 84;
pub const PROCEED: u64 = 1;
pub const INJECTED_PANIC: u64 = 880;
pub const BAD: u64 = 881;
pub const UNEXPECTED_COMPLETION: u64 = 882;
pub const SOURCE_GATE: u64 = 1;
pub const ACK_GATE: u64 = 2;
pub const PARTIAL_GATE: u64 = 3;
pub const GATE_EVENT: u64 = 160;
pub const NOTIFY_EVENT: u64 = 161;
pub const PARTIAL_SOURCE: [u8; 3] = [0x48, 0x8b, 0x03];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct Config {
    pub failure: u64,
    pub family: u64,
    pub n: u64,
    pub actor: u64,
}
impl Config {
    pub fn new(failure: u64, n: u64, actor: u64) -> Option<Self> {
        if !(WORKER_EXIT..=CANCEL).contains(&failure)
            || !matches!((n, actor), (1, 0) | (2, 0) | (2, 1) | (4, 0) | (4, 3))
        {
            return None;
        }
        Some(Self {
            failure,
            family: if failure == CANCEL && n == 2 && actor == 0 {
                schedule::PARTIAL
            } else {
                schedule::CONTROLS
            },
            n,
            actor,
        })
    }
    pub fn valid(self) -> bool {
        Self::new(self.failure, self.n, self.actor) == Some(self)
    }
    pub fn schedule(self) -> Option<schedule::Config> {
        if !self.valid() {
            return None;
        }
        (0..4)
            .filter_map(|ordinal| schedule::Config::new(self.family, self.n, ordinal))
            .find(|c| c.actor == self.actor)
    }
    pub fn all(self) -> u64 {
        (1 << self.n) - 1
    }
}

#[derive(Clone, Copy)]
#[repr(C)]
pub struct Witness {
    pub site: u64,
    pub owner: u64,
    pub phase: u64,
    pub active: u64,
    pub queued: u64,
    pub retained: u64,
    pub acknowledgements: u64,
    pub servicing: u64,
    pub lock: u64,
    pub next_ticket: u64,
    pub trace_length: u64,
    pub ready_owners: u64,
    pub pending_events: u64,
    pub profile: u64,
    pub visit: u64,
    pub completed: u64,
    pub context: u64,
    pub source_generation: u64,
    pub deferred_started: u64,
    pub producer_ready: u64,
    pub container_ready: u64,
    /// reads,writes,phase,subaccess,sample,source-route,destination-route,writable.
    pub helper: [u64; 8],
    pub packet: Packet,
    pub guest: GuestState,
    pub frame: NativeFrame,
    pub code: [u8; 640],
}
impl Witness {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
pub struct State {
    pub magic: u64,
    pub version: u64,
    pub config: Config,
    /// Written once after witness completion. Host observes with Acquire.
    pub ready: AtomicU64,
    /// Host sets PROCEED after applying worker/reply injection.
    pub command: AtomicU64,
    pub witness: Witness,
    /// Mutable service transport, read by the host only at the stopped gateway.
    pub expected: Packet,
    pub deferred_started: u64,
    pub producer_ready: u64,
    pub container_ready: u64,
}
impl State {
    pub fn new(config: Config) -> Option<Self> {
        if !config.valid() {
            return None;
        }
        Some(Self {
            magic: MAGIC,
            version: VERSION,
            config,
            ready: AtomicU64::new(0),
            command: AtomicU64::new(0),
            witness: Witness::ZERO,
            expected: [0; 16],
            deferred_started: 0,
            producer_ready: 0,
            container_ready: 0,
        })
    }
}
const _: () = assert!(core::mem::size_of::<State>() <= BYTES);
const _: () = assert!(OFFSET + BYTES == schedule::OFFSET);
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_twenty_five_cases_and_one_partial_cancellation() {
        let mut count = 0;
        let mut partial = 0;
        for failure in 0..7 {
            for n in 0..=5 {
                for actor in 0..=5 {
                    if let Some(c) = Config::new(failure, n, actor) {
                        count += 1;
                        partial += usize::from(c.family == schedule::PARTIAL);
                        let s = c.schedule().unwrap();
                        assert_eq!((s.n, s.actor, s.family), (n, actor, c.family));
                        if s.family == schedule::PARTIAL {
                            assert_eq!((failure, n, actor, s.seed_ordinal), (CANCEL, 2, 0, 0));
                        }
                    }
                }
            }
        }
        assert_eq!(count, 25);
        assert_eq!(partial, 1);
    }
    #[test]
    fn configuration_cannot_change_family_or_actor_after_validation() {
        let mut c = Config::new(CANCEL, 2, 0).unwrap();
        c.family = schedule::CONTROLS;
        assert!(!c.valid());
        assert!(Config::new(WITHHELD_ACK, 3, 0).is_none());
        assert!(Config::new(LOCKED_PANIC, 4, 2).is_none());
    }
}
