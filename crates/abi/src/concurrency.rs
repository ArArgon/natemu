//! Finite concurrent execution profile. Private virtual addresses are identical
//! on every CPU, but only this coordinator and guest/code pages share backing.
use crate::{LIVE_LOAD, LIVE_OR, NativeFrame, Object, PRIVATE};
use core::sync::atomic::AtomicU64;

pub const MODE: u64 = 8;
pub const MAX_CPUS: usize = 4;
pub const COORD_OFFSET: usize = 0x90000;
pub const COORD_VA: u64 = PRIVATE + COORD_OFFSET as u64;
pub const ROUND_OFFSET: usize = 0x28000;
pub const ROUND_VA: u64 = PRIVATE + ROUND_OFFSET as u64;
pub const TRACE_CAPACITY: usize = 768;
pub const OPEN: u64 = 0;
pub const CLOSING: u64 = 1;
pub const EXCLUSIVE: u64 = 2;
pub const RELEASING: u64 = 3;
pub const NO_CPU: u64 = u64::MAX;
pub const COMPILING: u64 = 4;
pub const DEFER_COMPILE: u64 = 80;
pub const TAKE_COMPILE: u64 = 81;
pub const POLL_COMPILE: u64 = 82;
pub const EVENT_OFFSET: usize = 0x29000;
pub const EVENT_VA: u64 = PRIVATE + EVENT_OFFSET as u64;
pub const CODE_NEXT: u64 = crate::CODE_VA + 4096;

pub fn validate_object(object: &Object, frame: &NativeFrame, code: &[u8]) -> Result<(), u64> {
    if object.configured_vcpus == 0
        || object.configured_vcpus as usize > MAX_CPUS
        || object.vcpu_id >= object.configured_vcpus
        || !matches!(object.kind, LIVE_LOAD | LIVE_OR)
        || object.code_address
            != if object.kind == LIVE_LOAD {
                crate::CODE_VA
            } else {
                CODE_NEXT
            }
    {
        return Err(80);
    }
    // The same finite FEX binding, with a disjoint 256-byte guest span per CPU.
    let mut local = *object;
    local.configured_vcpus = 1;
    local.vcpu_id = 0;
    local.code_address = crate::CODE_VA;
    let mut normalized = *frame;
    let offset = object.vcpu_id as u64 * 256;
    normalized.x[6] = normalized.x[6].checked_sub(offset).ok_or(81u64)?;
    normalized.x[11] = normalized.x[11].checked_sub(offset).ok_or(81u64)?;
    local.validate(&normalized, code)
}

#[repr(C, align(64))]
pub struct Coordinator {
    pub participants: u64,
    pub scenario: u64,
    pub alias_profile: u64,
    pub stack_profile: u64,
    pub mixed_profile: u64,
    pub mixed_compiled: AtomicU64,
    pub ready: AtomicU64,
    pub finished: AtomicU64,
    pub lock: AtomicU64,
    pub next_ticket: AtomicU64,
    pub phase: AtomicU64,
    pub active: AtomicU64,
    pub queued: AtomicU64,
    pub queue: [AtomicU64; MAX_CPUS],
    pub head: AtomicU64,
    pub length: AtomicU64,
    pub servicing: AtomicU64,
    pub completed: AtomicU64,
    pub retained: AtomicU64,
    pub generation: AtomicU64,
    pub acknowledgements: AtomicU64,
    pub new_entries: AtomicU64,
    pub new_completed: AtomicU64,
    pub new_references: AtomicU64,
    pub reclaimed: AtomicU64,
    pub old_eligible: AtomicU64,
    pub published: AtomicU64,
    pub stale_replies: AtomicU64,
    pub stale_entries: AtomicU64,
    pub reuse_blocked: AtomicU64,
    pub translation_wait: AtomicU64,
    pub event_pending: [AtomicU64; MAX_CPUS],
    pub event_notifications: [AtomicU64; MAX_CPUS],
    pub trace_length: AtomicU64,
    pub trace: [AtomicU64; TRACE_CAPACITY],
}
impl Coordinator {
    pub fn new(n: usize) -> Option<Self> {
        if n == 0 || n > MAX_CPUS {
            return None;
        }
        Some(Self {
            participants: n as u64,
            scenario: 0,
            alias_profile: 0,
            stack_profile: 0,
            mixed_profile: 0,
            mixed_compiled: AtomicU64::new(0),
            ready: AtomicU64::new(0),
            finished: AtomicU64::new(0),
            lock: AtomicU64::new(0),
            next_ticket: AtomicU64::new(0),
            phase: AtomicU64::new(OPEN),
            active: AtomicU64::new(0),
            queued: AtomicU64::new(0),
            queue: [const { AtomicU64::new(NO_CPU) }; MAX_CPUS],
            head: AtomicU64::new(0),
            length: AtomicU64::new(0),
            servicing: AtomicU64::new(NO_CPU),
            completed: AtomicU64::new(0),
            retained: AtomicU64::new(0),
            generation: AtomicU64::new(1),
            acknowledgements: AtomicU64::new(0),
            new_entries: AtomicU64::new(0),
            new_completed: AtomicU64::new(0),
            new_references: AtomicU64::new(0),
            reclaimed: AtomicU64::new(0),
            old_eligible: AtomicU64::new(1),
            published: AtomicU64::new(1),
            stale_replies: AtomicU64::new(0),
            stale_entries: AtomicU64::new(0),
            reuse_blocked: AtomicU64::new(0),
            translation_wait: AtomicU64::new(0),
            event_pending: [const { AtomicU64::new(0) }; MAX_CPUS],
            event_notifications: [const { AtomicU64::new(0) }; MAX_CPUS],
            trace_length: AtomicU64::new(0),
            trace: [const { AtomicU64::new(0) }; TRACE_CAPACITY],
        })
    }
}
const _: () = assert!(core::mem::size_of::<Coordinator>() <= 8192);

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Round {
    pub stage: u64,
    pub faults: u64,
    pub old_gpa: u64,
    pub fault_frame: NativeFrame,
    pub fault_esr: u64,
    pub fault_far: u64,
    pub fault_pc: u64,
    pub fault_spsr: u64,
    pub first_frame: NativeFrame,
    pub new_initial: NativeFrame,
    pub dirty_faults: u64,
    pub dirty_frame: NativeFrame,
    pub dirty_esr: u64,
    pub dirty_far: u64,
    pub dirty_pc: u64,
    pub dirty_spsr: u64,
}
impl Round {
    pub const ZERO: Self = Self {
        stage: 0,
        faults: 0,
        old_gpa: 0,
        fault_frame: NativeFrame::ZERO,
        fault_esr: 0,
        fault_far: 0,
        fault_pc: 0,
        fault_spsr: 0,
        first_frame: NativeFrame::ZERO,
        new_initial: NativeFrame::ZERO,
        dirty_faults: 0,
        dirty_frame: NativeFrame::ZERO,
        dirty_esr: 0,
        dirty_far: 0,
        dirty_pc: 0,
        dirty_spsr: 0,
    };
}
const _: () = assert!(core::mem::size_of::<Round>() <= 4096);

#[repr(C)]
#[derive(Clone, Copy)]
pub struct EventRecord {
    pub consumed: u64,
    pub deliveries: u64,
    pub boundary: u64,
    pub blocked: u64,
    pub wait_checks: u64,
    pub shadow: u64,
    pub priority: u64,
    pub packet: crate::recovery::GuestState,
}
impl EventRecord {
    pub fn new(frame: &NativeFrame, id: usize) -> Self {
        Self {
            consumed: 0,
            deliveries: 0,
            boundary: 0,
            blocked: 0,
            wait_checks: 0,
            shadow: u64::from(id == 1),
            priority: if id == 2 { 4 } else { 0 },
            packet: crate::recovery::GuestState::seed(frame),
        }
    }
}
const _: () = assert!(core::mem::size_of::<EventRecord>() <= 4096);

pub fn event_blockers(
    phase: u64,
    active: bool,
    complete: bool,
    rflags: u64,
    shadow: u64,
    priority: u64,
) -> u64 {
    u64::from(phase != OPEN) * 2
        | u64::from(rflags & 0x200 == 0) * 4
        | u64::from(shadow != 0) * 8
        | u64::from(priority >= 4) * 16
        | u64::from(!complete) * 32
        | u64::from(active) * 64
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn capacity_is_admitted_before_entry() {
        for n in [0, MAX_CPUS + 1, usize::MAX] {
            assert!(Coordinator::new(n).is_none());
        }
        for n in 1..=MAX_CPUS {
            assert_eq!(Coordinator::new(n).unwrap().participants, n as u64);
        }
    }
    #[test]
    fn pending_delivery_requires_completed_unmasked_owner() {
        assert_eq!(event_blockers(OPEN, false, true, 0x202, 0, 0), 0);
        assert_eq!(
            event_blockers(CLOSING, true, false, 2, 1, 4),
            2 | 4 | 8 | 16 | 32 | 64
        );
        // Clearing IF or priority alone cannot bypass another delivery gate.
        assert_eq!(event_blockers(OPEN, false, true, 0x202, 1, 4), 8 | 16);
        assert_eq!(event_blockers(OPEN, false, true, 0x202, 0, 4), 16);
        assert_eq!(event_blockers(OPEN, true, true, 0x202, 0, 0), 64);
        assert_eq!(event_blockers(OPEN, false, false, 0x202, 0, 0), 32);
    }
    #[test]
    fn concurrent_admission_keeps_object_and_operand_guards() {
        use crate::*;
        let mut code = [0; 48];
        code[..44].copy_from_slice(&FEX_BODY);
        code[44..].copy_from_slice(&RET);
        let mut o = Object::new(LIVE_LOAD);
        o.configured_vcpus = 4;
        o.vcpu_id = 3;
        let mut f = NativeFrame::ZERO;
        f.sp = STACK_TOP;
        f.x[28] = BANK_VA;
        f.x[6] = DATA_VA + 3 * 256;
        f.x[11] = DATA_VA + 3 * 256 + 128;
        assert_eq!(validate_object(&o, &f, &code), Ok(()));
        let mut bad = o;
        bad.version += 1;
        assert!(validate_object(&bad, &f, &code).is_err());
        bad = o;
        bad.entry_offset = 4;
        assert!(validate_object(&bad, &f, &code).is_err());
        bad = o;
        bad.vcpu_id = 4;
        assert!(validate_object(&bad, &f, &code).is_err());
        let mut other = f;
        other.x[6] -= 256;
        assert!(validate_object(&o, &other, &code).is_err());
        other = f;
        other.sp -= 16;
        assert!(validate_object(&o, &other, &code).is_err());
        code[44] ^= 1;
        assert!(validate_object(&o, &f, &code).is_err());
    }
}
