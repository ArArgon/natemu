//! Per-owner control execution composed with the shared I4 coordinator.
use crate::{DATA_VA, concurrency::MAX_CPUS, controls};
pub const PROGRAM_REVISION: u64 = 1;
pub const OWNER_OFFSET: usize = 0x26000;
pub const SHARED_OFFSET: usize = 0x93000;
pub const SHARED_END: usize = 0xa0000;
pub const fn source(id: u64) -> u64 {
    controls::RIP + id * 256
}
pub const fn data(id: u64) -> u64 {
    DATA_VA + id * 256
}
pub const fn gdt(id: u64) -> u64 {
    DATA_VA + 0x800 + id * 256
}
pub const fn pointer(id: u64) -> u64 {
    gdt(id) + 0x40
}
pub fn supported(profile: u64) -> bool {
    matches!(profile, 0 | 3 | 21 | 22 | 24)
}
pub fn cut_completed(profile: u64) -> Option<u64> {
    Some(match profile {
        0 | 3 | 21 => 3,
        22 => 7,
        24 => 2,
        _ => return None,
    })
}
pub fn first_memory_visit(profile: u64) -> Option<u64> {
    Some(match profile {
        0 | 3 => 2,
        21 => 5,
        22 => 10,
        24 => 1,
        _ => return None,
    })
}
pub fn second_cut_completed(profile: u64) -> Option<u64> {
    Some(match profile {
        0 | 21 => 6,
        22 => 8,
        24 => 3,
        _ => return None,
    })
}
pub fn final_visit(profile: u64) -> Option<u64> {
    Some(match profile {
        0 => 16,
        21 => 15,
        22 => 18,
        24 => 12,
        _ => return None,
    })
}
pub fn replacement(profile: u64) -> Option<controls::Instruction> {
    Some(match profile {
        0 => controls::mov(0x11),
        21 | 22 | 24 => controls::Instruction {
            bytes: [0x89, 3, 0, 0, 0, 0, 0, 0],
            len: 2,
        },
        _ => return None,
    })
}
pub fn replacement_offset(profile: u64) -> Option<usize> {
    Some(match profile {
        0 | 24 => 0,
        21 => 16,
        22 => source_bytes(profile, 0, 1)?.1 - 2,
        _ => return None,
    })
}
// Extra generated progress belongs only to this concurrent fixture. The
// semantic transitions and original single-owner programs remain unchanged.
pub fn program(profile: u64) -> Option<([controls::Instruction; controls::MAX_VISITS], usize)> {
    let original = controls::instructions(profile)?;
    let zero = controls::Instruction {
        bytes: [0; 8],
        len: 0,
    };
    let mut out = [zero; controls::MAX_VISITS];
    let mut len = 0;
    for (index, instruction) in original.iter().enumerate() {
        let additions: &[controls::Instruction] = match (profile, index) {
            (21, 8) => &[
                controls::mov(0),
                controls::mov(0x100),
                controls::mov(0x1020),
            ],
            (22, 12) => &[
                controls::mov(0),
                controls::mov(0x100),
                controls::mov(0x10011),
            ],
            (24, 7) => &[controls::mov(0)],
            _ => &[],
        };
        for added in additions {
            out[len] = *added;
            len += 1;
        }
        out[len] = *instruction;
        len += 1;
    }
    Some((out, len))
}
pub fn source_bytes(profile: u64, id: u64, generation: u64) -> Option<([u8; 128], usize)> {
    if id >= MAX_CPUS as u64 || !matches!(generation, 1 | 2) {
        return None;
    }
    let (instructions, count) = program(profile)?;
    let mut bytes = [0; 128];
    let mut len = 0;
    for instruction in &instructions[..count] {
        bytes[len..len + instruction.len].copy_from_slice(&instruction.bytes[..instruction.len]);
        if instruction.bytes[0] == 0xea {
            bytes[len + 1..len + 5].copy_from_slice(
                &((source(id) + len as u64 + instruction.len as u64) as u32).to_le_bytes(),
            );
        }
        if instruction.bytes[0] == 0xe9 {
            let target = if profile == 21 { 16 } else { 0 };
            bytes[len + 1..len + 5].copy_from_slice(&(target - (len as i32 + 5)).to_le_bytes());
        }
        len += instruction.len;
    }
    if generation == 2 {
        let instruction = replacement(profile)?;
        let at = replacement_offset(profile)?;
        bytes[at..at + instruction.len].copy_from_slice(&instruction.bytes[..instruction.len]);
    }
    Some((bytes, len))
}
pub fn instruction(
    profile: u64,
    id: u64,
    generation: u64,
    rip: u64,
) -> Option<controls::Instruction> {
    let (instructions, count) = program(profile)?;
    let (bytes, _) = source_bytes(profile, id, generation)?;
    let mut at = 0;
    for mut instruction in instructions[..count].iter().copied() {
        if source(id) + at as u64 == rip {
            instruction.bytes[..instruction.len].copy_from_slice(&bytes[at..at + instruction.len]);
            return Some(instruction);
        }
        at += instruction.len;
    }
    None
}
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub control: [u64; 8],
    pub cs: [u64; 4],
    pub tr: [u64; 4],
    pub hidden: [u64; 4],
    pub hidden_generation: u64,
    pub context: u64,
    pub source_generation: u64,
    pub rip: u64,
    pub completed: u64,
    pub gpr: [u64; 4],
    pub route: u64,
}
impl Snapshot {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Owner {
    pub enabled: u64,
    pub ttbr1: u64,
    pub incarnation: u64,
    pub source_generation: u64,
    pub admitted: u64,
    pub route: u64,
    pub writable: u64,
    pub detached: u64,
    pub local_cut: u64,
    pub peer_reply_cut: u64,
    pub peer_reply_executed: u64,
    pub local_controls: u64,
    pub source_rejections: u64,
    pub cached_before_source: u64,
    pub source_entry_rejected: u64,
    pub binding_rejections: u64,
    pub retained_code: u64,
    pub reuse_blocked: u64,
    pub released_code: u64,
    pub reclaimed: u64,
    pub maintenance_intervals: u64,
    pub maintenance_acks: [[u64; 6]; 36],
    pub interval_visits: [u64; 4],
    pub semantic_admissions: u64,
    pub source_expected: [u64; 16],
    pub source_reply: [u64; 16],
    pub peer_expected: [u64; 16],
    pub peer_reply: [u64; 16],
    pub initial: Snapshot,
    pub before_local: Snapshot,
    pub after_local: Snapshot,
    pub before_peer_reply: Snapshot,
    pub after_peer_reply: Snapshot,
    pub metadata_gpas: [[u64; 2]; controls::MAX_VISITS],
    pub code_before_retire: [u8; controls::MAX_VISITS * controls::SLOT_BYTES],
    pub code_after_reclaim: [u8; controls::MAX_VISITS * controls::SLOT_BYTES],
}
impl Owner {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
    pub fn new() -> Self {
        Self {
            enabled: 1,
            incarnation: 1,
            source_generation: 1,
            ..Self::ZERO
        }
    }
}
// Mutable fields are protected by the existing coordinator lock; never copy
// this entire record onto the bounded native exception stack.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Shared {
    pub n: u64,
    pub profile: u64,
    pub actor: u64,
    pub phase: u64,
    pub ready: u64,
    pub held: u64,
    pub local_done: u64,
    pub drained: u64,
    pub peer_compiling: u64,
    pub second_commit: u64,
    pub peer_executed: u64,
    pub final_ready: u64,
    pub source_generation: u64,
    pub source_acknowledgements: u64,
    pub finished: u64,
    pub pae_ready: [u64; 2],
    pub pae_acknowledgements: [u64; 2],
    pub pae_published: u64,
    pub semantic_active: u64,
    pub maintenance_started: u64,
    pub maintenance_leaves: [[u64; 5]; 36],
    pub before: [Snapshot; MAX_CPUS],
    pub after: [Snapshot; MAX_CPUS],
}
impl Shared {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
    pub fn new(n: usize, profile: u64, actor: usize) -> Option<Self> {
        if n == 0 || n > MAX_CPUS || actor >= n || !supported(profile) {
            return None;
        }
        Some(Self {
            n: n as u64,
            profile,
            actor: actor as u64,
            source_generation: 1,
            ..Self::ZERO
        })
    }
}
const _: () = assert!(OWNER_OFFSET + core::mem::size_of::<Owner>() <= 0x28000);
const _: () = assert!(SHARED_OFFSET + core::mem::size_of::<Shared>() <= SHARED_END);

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn concurrent_extensions_preserve_owner_branches_and_replacement_site() {
        for (profile, count, final_count) in [(0, 15, 16), (21, 14, 15), (22, 18, 18), (24, 11, 12)]
        {
            let (instructions, len) = program(profile).unwrap();
            assert_eq!(len, count);
            assert_eq!(final_visit(profile), Some(final_count));
            for id in 0..4 {
                let (bytes, source_len) = source_bytes(profile, id, 1).unwrap();
                assert!(source_len < 128 && len <= controls::MAX_VISITS);
                let mut offset = 0;
                for original in &instructions[..len] {
                    let decoded = instruction(profile, id, 1, source(id) + offset as u64).unwrap();
                    assert_eq!(
                        &decoded.bytes[..decoded.len],
                        &bytes[offset..offset + decoded.len]
                    );
                    if original.bytes[0] == 0xea {
                        assert_eq!(
                            u32::from_le_bytes(decoded.bytes[1..5].try_into().unwrap()) as u64,
                            source(id) + offset as u64 + 7
                        );
                    }
                    if original.bytes[0] == 0xe9 {
                        let delta =
                            i32::from_le_bytes(decoded.bytes[1..5].try_into().unwrap()) as i64;
                        assert_eq!(
                            (offset as i64 + 5 + delta) as u64,
                            if profile == 21 { 16 } else { 0 }
                        );
                    }
                    offset += original.len;
                }
                assert_eq!(offset, source_len);
                assert!(instruction(profile, id, 1, source(id) + source_len as u64).is_none());
                let (changed, changed_len) = source_bytes(profile, id, 2).unwrap();
                let start = replacement_offset(profile).unwrap();
                let replacement = replacement(profile).unwrap();
                assert_eq!(changed_len, source_len);
                assert_eq!(&changed[..start], &bytes[..start]);
                assert_eq!(
                    &changed[start..start + replacement.len],
                    &replacement.bytes[..replacement.len]
                );
                assert_eq!(
                    &changed[start + replacement.len..],
                    &bytes[start + replacement.len..]
                );
            }
        }
        // Original single-owner fixtures retain their exact instruction counts.
        assert_eq!(controls::instructions(21).unwrap().len(), 11);
        assert_eq!(controls::instructions(22).unwrap().len(), 15);
        assert_eq!(controls::instructions(24).unwrap().len(), 10);
    }
    #[test]
    fn all_roles_have_four_generated_boundaries_before_final_source_cut() {
        // The slowest peer has completed these visits at the maintenance gate.
        for (profile, completed) in [(0, 7usize), (21, 8), (22, 12), (24, 4)] {
            let (instructions, count) = program(profile).unwrap();
            let ordinary = instructions[completed..count]
                .iter()
                .filter(|i| controls::body(&i.bytes[..i.len]).is_some())
                .count();
            // Direct22's last source instruction is the held replacement job.
            let before_final = ordinary - usize::from(profile == 22);
            assert!(
                before_final >= 4,
                "profile{profile} only{before_final} boundaries"
            );
        }
    }
}
