//! Per-owner evidence and staged compilation for the concurrent stack profile.
use crate::{NativeFrame, stack, validation::Packet};
pub const OFFSET: usize = 0x23000;
pub const MAX_PROGRAMS: usize = 6;
/// Logical fixture roles; shared physical backing is independent of these IDs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Roles {
    pub n: u64,
    pub actor: u64,
    pub delayed: u64,
}
impl Roles {
    pub fn for_fixture(n: u64, schedule: Option<crate::schedule::Config>) -> Option<Self> {
        if n == 0 || n > crate::concurrency::MAX_CPUS as u64 {
            return None;
        }
        let actor = match schedule {
            Some(config)
                if config.valid() && config.family == crate::schedule::STACK && config.n == n =>
            {
                config.actor
            }
            Some(_) => return None,
            None => 0,
        };
        Some(Self {
            n,
            actor,
            delayed: (actor + n - 1) % n,
        })
    }
    pub fn event_role(self, id: u64) -> u64 {
        (id + self.n - self.actor) % self.n
    }
}

pub fn initial_rsp(id: u64) -> u64 {
    initial_rsp_for(1, id)
}
pub fn initial_rsp_for(profile: u64, id: u64) -> u64 {
    (if profile >= 2 {
        crate::DATA_VA + id * 256
    } else {
        crate::alias::data_address(1, id, 0)
    }) + 0x80
}
pub fn other_alias(profile: u64, address: u64) -> Option<u64> {
    if profile < 2 {
        return crate::alias::other_address(address);
    }
    if !(crate::DATA_VA..crate::DATA_VA + 4096).contains(&address) {
        return None;
    }
    Some(address + crate::shadow::HIGH_BASE + if profile == 3 { 4096 } else { 0 })
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Probe {
    pub initial: NativeFrame,
    pub captured: NativeFrame,
    pub guest_before: crate::recovery::GuestState,
    pub guest_after: crate::recovery::GuestState,
    pub pair: [u64; 2],
    // Low/upper PML4, low leaf256, companion leaf257.
    pub tables_before: [u64; 4],
    pub tables_after: [u64; 4],
    pub vector: u64,
    pub error: u64,
    pub address: u64,
    pub rip: u64,
    pub esr: u64,
    pub far: u64,
    pub pc: u64,
    pub spsr: u64,
    pub done: u64,
}
impl Probe {
    // Every field is an integer transport value; zero denotes an unused probe.
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Program {
    pub entries: u64,
    pub completed: u64,
    // RSP, RAX, RBX, generated scratch x20 after each guest instruction.
    pub prefixes: [[u64; 4]; 5],
}
impl Program {
    pub const ZERO: Self = Self {
        entries: 0,
        completed: 0,
        prefixes: [[0; 4]; 5],
    };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct State {
    pub step: u64,
    pub pass: u64,
    pub code: u64,
    pub expected: Packet,
    pub compile_requests: [Packet; 3],
    pub replies: [[Packet; 3]; 3],
    pub programs: [Program; MAX_PROGRAMS],
    pub stale_chunks: u64,
    pub wrong_owner_rejections: u64,
    pub blank_before_publish: u64,
    pub source_before: [u8; 20],
    pub source_after: [u8; 20],
    pub probe: Probe,
}
impl State {
    pub const ZERO: Self = Self {
        step: 0,
        pass: 0,
        code: 0,
        expected: [0; 16],
        compile_requests: [[0; 16]; 3],
        replies: [[[0; 16]; 3]; 3],
        programs: [Program::ZERO; MAX_PROGRAMS],
        stale_chunks: 0,
        wrong_owner_rejections: 0,
        blank_before_publish: 0,
        source_before: [0; 20],
        source_after: [0; 20],
        probe: Probe::ZERO,
    };
}
pub fn due(frame: &NativeFrame, step: usize) -> u64 {
    if step < 3 {
        frame.x[8].wrapping_sub(stack::WIDTHS[step])
    } else {
        frame.x[5].wrapping_sub(if step == 3 { 8 } else { 12 })
    }
}
pub fn validate_reply(expected: &Packet, actual: &Packet) -> Option<[u8; 24]> {
    if stack::reply_context(expected)? != *actual {
        return None;
    }
    let mut bytes = [0; 24];
    for i in 0..3 {
        bytes[i * 8..i * 8 + 8].copy_from_slice(&actual[10 + i].to_le_bytes());
    }
    Some(bytes)
}
const _: () = assert!(
    OFFSET >= crate::validation::STATE_OFFSET + core::mem::size_of::<crate::validation::State>()
);
const _: () = assert!(OFFSET + core::mem::size_of::<State>() <= crate::concurrency::ROUND_OFFSET);
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn seeded_roles_rotate_actor_and_delayed_peer_without_changing_defaults() {
        for n in 1..=4 {
            let default = Roles::for_fixture(n, None).unwrap();
            assert_eq!((default.actor, default.delayed), (0, n - 1));
            for ordinal in 0..4 {
                let config =
                    crate::schedule::Config::new(crate::schedule::STACK, n, ordinal).unwrap();
                let roles = Roles::for_fixture(n, Some(config)).unwrap();
                assert_eq!(
                    (roles.actor, roles.delayed),
                    (ordinal % n, (ordinal + n - 1) % n)
                );
                assert_eq!(roles.event_role(roles.actor), 0);
                for role in 0..n {
                    assert_eq!(roles.event_role((roles.actor + role) % n), role);
                }
                let request = stack::request_context(83, roles.actor, 1, 2, 1, 3, 0x102000, 1, 0);
                let reply = stack::reply_context(&request).unwrap();
                assert!(validate_reply(&request, &reply).is_some());
                let mut other = reply;
                other[3] = (roles.actor + 1) % 4;
                assert!(validate_reply(&request, &other).is_none());
            }
        }
        assert!(Roles::for_fixture(0, None).is_none());
        assert!(Roles::for_fixture(5, None).is_none());
        assert!(
            Roles::for_fixture(
                2,
                crate::schedule::Config::new(crate::schedule::CONTROLS, 2, 0)
            )
            .is_none()
        );
        assert!(
            Roles::for_fixture(
                3,
                crate::schedule::Config::new(crate::schedule::STACK, 2, 0)
            )
            .is_none()
        );
    }
    #[test]
    fn chunks_are_bound_to_stored_owner_and_generation() {
        let p =
            stack::request_context(83, 2, 7, 2, 3, 9, crate::shadow::HIGH_BASE + 0x102000, 1, 2);
        let good = stack::reply_context(&p).unwrap();
        assert!(validate_reply(&p, &good).is_some());
        for word in [2, 3, 4, 5, 6, 7, 8, 13, 14, 15] {
            let mut bad = good;
            bad[word] ^= 1;
            assert!(validate_reply(&p, &bad).is_none());
        }
        assert!(stack::reply(&p).is_none());
    }
    #[test]
    fn all_program_stacks_stay_in_the_owner_span() {
        for id in 0..4 {
            let s = initial_rsp(id);
            let base = crate::alias::data_address(1, id, 0);
            assert!(s - 12 * MAX_PROGRAMS as u64 >= base);
            assert!(s <= base + 256);
        }
    }
}
