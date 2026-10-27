//! Finite changed-on-read helper composition, not translated REP coverage.
use crate::{NativeFrame, PRIVATE, families::coherence::Walk, recovery::GuestState, families::schedule};
pub const MODE: u64 = 14;
pub const OWNER_OFFSET: usize = 0x26000;
pub const COMMON_OFFSET: usize = 0x93000;
pub const RAM_OFFSET: usize = 0x94000;
pub const SOURCE_OFFSET: usize = 0x96000;
pub const TABLE_OFFSET: usize = 0x98000;
pub const DATA: u64 = 0x100000;
pub const RIP: u64 = 0x102000;
pub const DEVICE: u64 = 0x500000;
pub const IMAGE_OFFSET: usize = 0x100000;
pub const IMAGES: usize = 3;
pub const WALKS: usize = 5;
pub const SOURCE: [u8; 2] = [0xf3, 0xa4];
pub const READY: u64 = 140;
pub const SAMPLED: u64 = 141;
pub const HELD: u64 = 142;
pub const MUTATION_BLOCKED: u64 = 143;
pub const RETRY: u64 = 144;
pub const HELPER_DONE: u64 = 145;
pub const RELEASED: u64 = 146;
pub const MUTATED: u64 = 147;
pub const ADMISSION_BLOCKED: u64 = 148;
pub const ACK: u64 = 149;
pub const OPENED: u64 = 150;
pub const PROBE_DONE: u64 = 151;
pub const EVENT_BLOCKED: u64 = 152;
pub const EVENT_DELIVERED: u64 = 153;
pub const STOPPED: u64 = 154;
pub const NOTIFIED: u64 = 155;

pub fn value(id: u64, element: u64) -> u64 {
    0x40 + id * 0x20 + (element + 1) * 0x17
}
pub fn destination(id: u64) -> u64 {
    DATA + id * 256
}
pub fn device(id: u64) -> u64 {
    DEVICE + id * 256
}
pub fn probe_value(id: u64) -> u64 {
    0xd0 + id
}
pub fn guest(frame: &NativeFrame, id: u64) -> GuestState {
    let mut g = GuestState::seed(frame);
    g.gpr[1] = 2;
    g.gpr[6] = device(id);
    g.gpr[7] = destination(id);
    g.rip = RIP + id * 256;
    g
}
#[derive(Clone, Copy)]
#[repr(C)]
pub struct Fault {
    /// Native Rust-helper continuation, not a generated guest-root frame.
    pub frame: NativeFrame,
    pub esr: u64,
    pub far: u64,
    pub pc: u64,
    pub spsr: u64,
    pub resume_pc: u64,
    pub value: u64,
    pub gpa: u64,
}
impl Fault {
    pub const ZERO: Self = Self {
        frame: NativeFrame::ZERO,
        esr: 0,
        far: 0,
        pc: 0,
        spsr: 0,
        resume_pc: 0,
        value: 0,
        gpa: 0,
    };
}
#[repr(C)]
pub struct Owner {
    pub id: u64,
    pub phase: u64,
    pub reads: u64,
    pub writes: u64,
    pub read_values: [u64; 2],
    pub sampled: u64,
    pub subaccess: u64,
    pub source_route: u64,
    pub destination_route: u64,
    pub destination_writable: u64,
    pub retained: u64,
    pub released: u64,
    pub faults: u64,
    pub retries: u64,
    pub probe_writes: u64,
    pub probe_route: u64,
    pub notifications: u64,
    pub pending: u64,
    pub event_attempts: u64,
    pub event_blocked: u64,
    pub delivered: u64,
    pub delivery_state: [u64; 5],
    pub walk_count: u64,
    pub walks: [Walk; WALKS],
    pub fault: [Fault; 2],
    pub initial: NativeFrame,
    pub before: GuestState,
    pub held: GuestState,
    pub resumed: GuestState,
    pub after: GuestState,
    pub store_pc: u64,
    pub store_bytes: [u8; 8],
}
impl Owner {
    pub fn new(id: u64, frame: NativeFrame) -> Self {
        let g = guest(&frame, id);
        Self {
            id,
            phase: 0,
            reads: 0,
            writes: 0,
            read_values: [0; 2],
            sampled: 0,
            subaccess: 0,
            source_route: 0,
            destination_route: 0,
            destination_writable: 0,
            retained: 0,
            released: 0,
            faults: 0,
            retries: 0,
            probe_writes: 0,
            probe_route: 0,
            notifications: 0,
            pending: 0,
            event_attempts: 0,
            event_blocked: 0,
            delivered: 0,
            delivery_state: [0; 5],
            walk_count: 0,
            walks: [Walk::ZERO; WALKS],
            fault: [Fault::ZERO; 2],
            initial: frame,
            before: g,
            held: g,
            resumed: g,
            after: g,
            store_pc: 0,
            store_bytes: [0; 8],
        }
    }
}
#[repr(C)]
pub struct Common {
    pub n: u64,
    pub actor: u64,
    pub ordinal: u64,
    pub ready: u64,
    pub mutation_blocked: u64,
    pub helper_done: u64,
    pub released: u64,
    pub mutated: u64,
    pub acknowledgements: u64,
    pub admission_blocked: u64,
    pub opened: u64,
    pub probes_done: u64,
    pub delivered: u64,
    pub finished: u64,
    pub images: u64,
    pub source_generation: u64,
    pub destination_generation: u64,
    pub pending_events: u64,
}
impl Common {
    pub fn new(config: schedule::Config) -> Option<Self> {
        if !config.valid() || config.family != schedule::PARTIAL {
            return None;
        }
        Some(Self {
            n: config.n,
            actor: config.actor,
            ordinal: config.seed_ordinal,
            ready: 0,
            mutation_blocked: 0,
            helper_done: 0,
            released: 0,
            mutated: 0,
            acknowledgements: 0,
            admission_blocked: 0,
            opened: 0,
            probes_done: 0,
            delivered: 0,
            finished: 0,
            images: 0,
            source_generation: 1,
            destination_generation: 1,
            pending_events: 0,
        })
    }
}
pub fn tables() -> [u8; 16384] {
    let mut b = [0; 16384];
    for (at, value) in [
        (0, 0x2027u64),
        (4096, 0x3027),
        (8192, 0x4027),
        (12288 + 256 * 8, DATA | 0x67),
        (12288 + 258 * 8, RIP | 0x27),
    ] {
        b[at..at + 8].copy_from_slice(&value.to_le_bytes());
    }
    b
}
pub fn direct_store(
    esr: u64,
    far: u64,
    pc: u64,
    spsr: u64,
    store_pc: u64,
    id: u64,
    probe: bool,
) -> bool {
    esr >> 26 == 0x25
        && esr & (1 << 6) != 0
        && esr & ((1 << 7) | (1 << 8) | (1 << 10)) == 0
        && if probe {
            matches!(esr & 63, 12..=15)
        } else {
            matches!(esr & 63, 4..=7)
        }
        && far == destination(id) + if probe { 2 } else { 1 }
        && pc == store_pc
        && spsr & 15 == 4
}
const _: () = assert!(OWNER_OFFSET + core::mem::size_of::<Owner>() < 0x30000);
const _: () = assert!(COMMON_OFFSET + core::mem::size_of::<Common>() <= RAM_OFFSET);
const _: () = assert!(PRIVATE + IMAGE_OFFSET as u64 + IMAGES as u64 * 8192 < schedule::VA);
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn device_samples_and_probe_are_distinct_and_spans_do_not_overlap() {
        for id in 0..4 {
            assert_ne!(value(id, 0), value(id, 1));
            assert_ne!(value(id, 1), value(id, 2));
            assert!(value(id, 1) < 256);
            assert_ne!(value(id, 1), probe_value(id));
            assert!(destination(id) + 2 < destination(id + 1));
        }
    }
    #[test]
    fn only_precise_direct_store_faults_are_admitted() {
        for id in 0..4 {
            for probe in [false, true] {
                let esr = (0x25 << 26) | (1 << 6) | if probe { 15 } else { 7 };
                let far = destination(id) + if probe { 2 } else { 1 };
                assert!(direct_store(esr, far, 0x1234, 4, 0x1234, id, probe));
                for bit in [6, 7, 8, 10] {
                    assert!(!direct_store(
                        esr ^ (1 << bit),
                        far,
                        0x1234,
                        4,
                        0x1234,
                        id,
                        probe
                    ));
                }
                assert!(!direct_store(esr, far + 1, 0x1234, 4, 0x1234, id, probe));
                assert!(!direct_store(esr, far, 0x1238, 4, 0x1234, id, probe));
            }
        }
    }
}
