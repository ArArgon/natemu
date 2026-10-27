//! Fetched control instructions and a bounded legacy32 transition campaign.
use crate::{NativeFrame, recovery::GuestState, families::validation::Packet};
pub const CASE: u64 = 4;
pub const OFFSET: usize = 0x23000;
pub const RECORD_OFFSET: usize = 0x84000;
pub const MAX_VISITS: usize = 20;
pub const SLOT_BYTES: usize = 32;
pub const RIP: u64 = 0x102000;
pub const MISS: u64 = 0x49345651;
pub const DELIVER: u64 = 0x49345652;
pub const CR0_DEFINED: u64 = 0xe005003f;
// Finite CPU profile: baseline CR4 fields and SMEP; optional later features absent.
pub const CR4_EXPOSED: u64 = 0x1507ff;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    GeneralProtection,
    Undefined,
    Unsupported,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ControlOp {
    pub cr: usize,
    pub gpr: usize,
    pub bytes: usize,
}
pub fn decode(source: &[u8]) -> Result<ControlOp, Error> {
    let mut i = 0;
    let mut lock = false;
    while i < source.len()
        && matches!(
            source[i],
            0x66 | 0x67 | 0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 | 0xf0
        )
    {
        lock |= source[i] == 0xf0;
        i += 1;
        if i >= 12 {
            return Err(Error::Unsupported);
        }
    }
    if source.get(i..i + 2) != Some(&[0x0f, 0x22]) {
        return Err(Error::Unsupported);
    }
    let modrm = *source.get(i + 2).ok_or(Error::Unsupported)?;
    let cr = ((modrm >> 3) & 7) as usize;
    if lock || matches!(cr, 1 | 5 | 6 | 7) {
        return Err(Error::Undefined);
    }
    if cr == 2 {
        return Err(Error::Unsupported);
    }
    Ok(ControlOp {
        cr,
        gpr: (modrm & 7) as usize,
        bytes: i + 3,
    })
}
pub fn mode(g: &GuestState) -> Option<u64> {
    crate::families::modes::mode(g)
}
pub fn prepare(
    g: &GuestState,
    cpl: u64,
    op: ControlOp,
    allow_pae: bool,
) -> Result<[u64; 8], Error> {
    if cpl != 0 {
        return Err(Error::GeneralProtection);
    }
    let value = g.gpr[op.gpr] & 0xffff_ffff;
    let next = match op.cr {
        0 => {
            let value = (value & CR0_DEFINED) | 16;
            if (value & (1 << 31) != 0 && value & 1 == 0)
                || (value & (1 << 29) != 0 && value & (1 << 30) == 0)
            {
                return Err(Error::GeneralProtection);
            }
            if value & !0x80010011 != 0 || value & 1 == 0 {
                return Err(Error::Unsupported);
            }
            value
        }
        3 => {
            if g.control[3] & 32 == 0 && value & 0x18 != 0 {
                return Err(Error::Unsupported);
            }
            value
        }
        4 => {
            if value & !CR4_EXPOSED != 0 {
                return Err(Error::GeneralProtection);
            }
            if value & !(if allow_pae { 0x6a0 } else { 0x680 }) != 0 {
                return Err(Error::Unsupported);
            }
            value
        }
        _ => return Err(Error::Unsupported),
    };
    let mut controls = g.control;
    // Architectural register indices differ from the transport CR array indices.
    controls[match op.cr {
        0 => 0,
        3 => 2,
        4 => 3,
        _ => unreachable!(),
    }] = next;
    Ok(controls)
}
pub fn apply(
    g: &mut GuestState,
    generation: &mut u64,
    cpl: u64,
    op: ControlOp,
) -> Result<(), Error> {
    let controls = prepare(g, cpl, op, false)?;
    let next = generation.checked_add(1).ok_or(Error::Unsupported)?;
    g.control = controls;
    *generation = next;
    // Deterministic policy: preserve MOV-CR's undefined arithmetic flags.
    Ok(())
}
pub fn immediate(value: u32) -> Option<([u8; 8], usize)> {
    let words: &[u32] = match value {
        0x10011 => &[0x52800224, 0x72a00024],
        0x80010011 => &[0x52800224, 0x72b00024],
        0x1000 => &[0x52820004],
        0x3000 => &[0x52860004],
        0 => &[0x52800004],
        0x80010010 => &[0x52800204, 0x72b00024],
        0x20010011 => &[0x52800224, 0x72a40024],
        0xffffffff => &[0x12800004],
        0x600 => &[0x5280c004],
        0x1003 => &[0x52820064],
        0x11 => &[0x52800224],
        0x80000011 => &[0x52800224, 0x72b00004],
        0x680 => &[0x5280d004],
        0x620 => &[0x5280c404],
        0x6a0 => &[0x5280d404],
        0x1020 => &[0x52820404],
        0x100 => &[0x52802004],
        0x6000 => &[0x528c0004],
        _ => return None,
    };
    let mut bytes = [0; 8];
    for (i, w) in words.iter().enumerate() {
        bytes[i * 4..i * 4 + 4].copy_from_slice(&w.to_le_bytes());
    }
    Some((bytes, words.len() * 4))
}
pub fn body(source: &[u8]) -> Option<([u8; 8], usize)> {
    match source {
        [0xb8, a, b, c, d] => immediate(u32::from_le_bytes([*a, *b, *c, *d])),
        [0x89, 0x03] => Some(([0x1f, 0x20, 3, 0xd5, 0xc4, 0xfc, 0x9f, 0x88], 8)),
        [0x8b, 0x03] => Some(([0xc4, 0xfc, 0xdf, 0x88, 0x1f, 0x20, 3, 0xd5], 8)),
        [0x48, 0x8b, 0x03] => Some(([0xc4, 0xfc, 0xdf, 0xc8, 0x1f, 0x20, 3, 0xd5], 8)),
        _ => None,
    }
}
pub fn body_for(source: &[u8], mode: u64) -> Option<([u8; 8], usize)> {
    if mode > 4
        || (mode == 4 && !matches!(source, [0x48, 0x8b, 3] | [0xb8, 0x11, 0, 1, 0]))
        || (mode != 4 && source.first() == Some(&0x48))
    {
        return None;
    }
    body(source)
}
#[derive(Clone, Copy)]
pub struct Instruction {
    pub bytes: [u8; 8],
    pub len: usize,
}
pub(crate) const fn mov(value: u32) -> Instruction {
    let v = value.to_le_bytes();
    Instruction {
        bytes: [0xb8, v[0], v[1], v[2], v[3], 0, 0, 0],
        len: 5,
    }
}
pub(crate) const fn cr(reg: u8) -> Instruction {
    Instruction {
        bytes: [0x0f, 0x22, 0xc0 | (reg << 3), 0, 0, 0, 0, 0],
        len: 3,
    }
}
const STORE: Instruction = Instruction {
    bytes: [0x89, 3, 0, 0, 0, 0, 0, 0],
    len: 2,
};
pub(crate) const LOAD: Instruction = Instruction {
    bytes: [0x8b, 3, 0, 0, 0, 0, 0, 0],
    len: 2,
};
pub(crate) const JUMP: Instruction = Instruction {
    bytes: [0xe9, 0, 0, 0, 0, 0, 0, 0],
    len: 5,
};
const INVLPG: Instruction = Instruction {
    bytes: [0x0f, 1, 0x3b, 0, 0, 0, 0, 0],
    len: 3,
};
pub fn instructions(profile: u64) -> Option<&'static [Instruction]> {
    if profile >= 22 {
        return crate::families::modes::instructions(profile);
    }
    const PROGRAMS: [&[Instruction]; 22] = [
        &[
            mov(0x10011),
            STORE,
            mov(0x80010011),
            cr(0),
            LOAD,
            mov(0x3000),
            cr(3),
            LOAD,
            mov(0x1000),
            cr(3),
            LOAD,
            mov(0x10011),
            cr(0),
            LOAD,
            JUMP,
        ],
        &[mov(0x80010011), cr(0), LOAD, mov(0x1000), cr(3), LOAD],
        &[
            mov(0x80000011),
            cr(0),
            mov(0x10011),
            STORE,
            mov(0x80010011),
            cr(0),
            mov(0xffffffff),
            STORE,
        ],
        &[mov(0x10011), STORE, mov(0x80010010), cr(0)],
        &[mov(0x10011), STORE, mov(0x20010011), cr(0)],
        &[mov(0x10011), STORE, mov(0xffffffff), cr(4)],
        &[mov(0x10011), STORE, mov(0x80010011), cr(0)],
        &[
            mov(0x10011),
            STORE,
            mov(0x80010011),
            cr(0),
            mov(0x3000),
            cr(3),
            LOAD,
        ],
        &[mov(0x1003), cr(3), mov(0x80010011), cr(0), LOAD],
        &[
            mov(0x80010011),
            Instruction {
                bytes: [0x66, 0x0f, 0x22, 0, 0, 0, 0, 0],
                len: 4,
            },
            LOAD,
        ],
        &[
            mov(0x10011),
            STORE,
            mov(0x80010011),
            Instruction {
                bytes: [0xf0, 0x0f, 0x22, 0xc0, 0, 0, 0, 0],
                len: 4,
            },
        ],
        &[mov(0x10011), STORE, mov(0x80010011), cr(1)],
        &[
            mov(0x80010011),
            cr(0),
            LOAD,
            mov(0x680),
            cr(4),
            LOAD,
            mov(0x600),
            cr(4),
            LOAD,
        ],
        &[mov(0x620), cr(4), mov(0x80010011), cr(0), LOAD],
        &[
            mov(0x620),
            cr(4),
            mov(0x80010011),
            cr(0),
            LOAD,
            INVLPG,
            LOAD,
            mov(0x1020),
            cr(3),
            LOAD,
        ],
        &[
            mov(0x620),
            cr(4),
            mov(0x80010011),
            cr(0),
            LOAD,
            LOAD,
            mov(0x1020),
            cr(3),
            LOAD,
        ],
        &[
            mov(0x620),
            cr(4),
            mov(0x80010011),
            cr(0),
            LOAD,
            mov(0x80000011),
            cr(0),
            LOAD,
            mov(0x6a0),
            cr(4),
            LOAD,
        ],
        &[
            mov(0x10011),
            STORE,
            mov(0x620),
            cr(4),
            mov(0x80010011),
            cr(0),
        ],
        &[
            mov(0x620),
            cr(4),
            mov(0x80010011),
            cr(0),
            LOAD,
            mov(0x10011),
            STORE,
            mov(0x1020),
            cr(3),
            LOAD,
        ],
        &[
            mov(0x620),
            cr(4),
            mov(0x80010011),
            cr(0),
            LOAD,
            mov(0x10011),
            STORE,
            mov(0x6a0),
            cr(4),
            LOAD,
        ],
        &[mov(0x620), cr(4), mov(0x80010011), cr(0), LOAD],
        &[
            mov(0x620),
            cr(4),
            mov(0x80010011),
            cr(0),
            LOAD,
            mov(0x1020),
            cr(3),
            LOAD,
            mov(0x1020),
            cr(3),
            JUMP,
        ],
    ];
    PROGRAMS.get(usize::try_from(profile).ok()?).copied()
}
pub fn source(profile: u64) -> Option<([u8; 128], usize)> {
    source_at(profile, RIP)
}
pub fn source_at(profile: u64, base: u64) -> Option<([u8; 128], usize)> {
    let mut source = [0; 128];
    let mut size = 0;
    for inst in instructions(profile)? {
        source[size..size + inst.len].copy_from_slice(&inst.bytes[..inst.len]);
        if inst.bytes[0] == 0xe9 {
            let destination = if profile == 21 { 16 } else { 0 };
            source[size + 1..size + 5]
                .copy_from_slice(&(destination - (size as i32 + 5)).to_le_bytes());
        }
        if inst.bytes[0] == 0xea {
            source[size + 1..size + 5]
                .copy_from_slice(&((base + size as u64 + inst.len as u64) as u32).to_le_bytes());
        }
        size += inst.len;
    }
    Some((source, size))
}
pub fn instruction(profile: u64, rip: u64) -> Option<Instruction> {
    instruction_at(profile, RIP, rip)
}
pub fn instruction_at(profile: u64, base: u64, rip: u64) -> Option<Instruction> {
    let (source, size) = source_at(profile, base)?;
    let mut at = 0;
    for inst in instructions(profile)? {
        if base + at as u64 == rip {
            let mut out = *inst;
            out.bytes[..out.len].copy_from_slice(&source[at..at + out.len]);
            return Some(out);
        }
        at += inst.len;
    }
    let _ = size;
    None
}
pub fn request(
    vm: u64,
    generation: u64,
    mode: u64,
    rip: u64,
    source: &[u8],
    serial: u64,
) -> Option<Packet> {
    request_owner(vm, 0, 1, 1, generation, mode, rip, source, serial)
}
pub fn request_owner(
    vm: u64,
    owner: u64,
    incarnation: u64,
    source_generation: u64,
    generation: u64,
    mode: u64,
    rip: u64,
    source: &[u8],
    serial: u64,
) -> Option<Packet> {
    let (_, len) = body_for(source, mode)?;
    if vm == 0 || owner >= crate::concurrency::MAX_CPUS as u64
        || incarnation == 0 || source_generation == 0 || generation == 0
        || serial == 0 || source.len() > 8
        || rip < RIP + owner * 256 || rip.checked_add(source.len() as u64)? > RIP + (owner + 1) * 256
    {
        return None;
    }
    let mut bytes = [0; 8];
    bytes[..source.len()].copy_from_slice(source);
    Some([
        MISS,
        1,
        vm,
        owner,
        incarnation,
        source_generation,
        generation,
        rip,
        mode,
        u64::from_le_bytes(bytes),
        source.len() as u64,
        rip,
        serial,
        len as u64,
        0,
        0,
    ])
}
pub fn reply(p: &Packet) -> Option<Packet> {
    let len = usize::try_from(p[10]).ok()?;
    if len > 8 {
        return None;
    }
    let bytes = p[9].to_le_bytes();
    let src = &bytes[..len];
    if Some(*p) != request_owner(p[2], p[3], p[4], p[5], p[6], p[8], p[7], src, p[12]) {
        return None;
    }
    let (body, _) = body(src)?;
    let mut out = *p;
    out[0] = DELIVER;
    out[14] = u64::from_le_bytes(body);
    Some(out)
}
pub fn valid(expected: &Packet, reply: &Packet) -> bool {
    self::reply(expected) == Some(*reply)
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Visit {
    pub rip: u64,
    pub bytes: u64,
    pub kind: u64,
    pub source: u64,
    pub fetch_gpa: u64,
    pub before: [u64; 8],
    pub after: [u64; 8],
    pub generation_before: u64,
    pub generation_after: u64,
    pub gpr_before: [u64; 4],
    pub gpr_after: [u64; 4],
    pub flags_before: u64,
    pub flags_after: u64,
    pub frame: NativeFrame,
    pub fault_frame: NativeFrame,
    pub exception: [u64; 4],
    pub request: Packet,
    pub reply: Packet,
    pub entries: u64,
    pub faults: u64,
    pub gpa: u64,
    pub memory_before: [u8; 16],
    pub memory_after: [u8; 16],
    pub vector: u64,
    pub error: u64,
    pub linear: u64,
}
impl Visit {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct State {
    pub profile: u64,
    pub cpl: u64,
    pub generation: u64,
    pub visits: u64,
    pub completed: u64,
    pub transfers: u64,
    pub serial: u64,
    pub walks: u64,
    pub invalidations: u64,
    pub mapping: u64,
    pub fixture_mutated: u64,
    pub warm_before_reload: u64,
    pub stale_reply: u64,
    pub stale_entry: u64,
    pub expected: Packet,
    pub deferred: Packet,
    pub late_reply: Packet,
    pub rebound_reply: Packet,
    pub old_slot_context: u64,
    pub initial: NativeFrame,
    pub current: NativeFrame,
    pub old_controls: [u64; 8],
}
impl State {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
const _: () = assert!(OFFSET + core::mem::size_of::<State>() <= crate::families::pae::OFFSET);
const _: () = assert!(RECORD_OFFSET + MAX_VISITS * core::mem::size_of::<Visit>() <= 0x90000);
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn owner_lifetime_and_source_are_independent_reply_bindings() {
        let source = [0x8b, 3];
        let p = request_owner(7, 3, 4, 5, 6, 3, RIP + 3 * 256, &source, 9).unwrap();
        assert!(valid(&p, &reply(&p).unwrap()));
        for word in [3, 4, 5, 6, 8, 12] {
            let mut wrong = reply(&p).unwrap();
            wrong[word] ^= 1;
            assert!(!valid(&p, &wrong));
        }
        assert!(request_owner(7, 2, 4, 5, 6, 3, RIP + 3 * 256, &source, 9).is_none());
        assert!(request_owner(7, 4, 4, 5, 6, 3, RIP + 4 * 256, &source, 9).is_none());
        for (incarnation, source_generation) in [(0, 1), (1, 0)] {
            assert!(request_owner(7, 0, incarnation, source_generation, 6, 3, RIP, &source, 9).is_none());
        }
    }
    #[test]
    fn owner_program_relocates_far_targets_but_preserves_relative_backedges() {
        let base = RIP + 3 * 256;
        let mut at = base;
        for inst in instructions(22).unwrap() {
            let actual = instruction_at(22, base, at).unwrap();
            if actual.bytes[0] == 0xea {
                assert_eq!(u32::from_le_bytes(actual.bytes[1..5].try_into().unwrap()) as u64, at + actual.len as u64);
            }
            at += inst.len as u64;
        }
        let (a, n) = source(24).unwrap();
        let (b, m) = source_at(24, base).unwrap();
        assert_eq!(n, m);
        assert_eq!(&a[n - 5..n], &b[m - 5..m]);
    }
    #[test]
    fn decoder_ignores_mod_and_operand_size_but_keeps_fault_classes() {
        for modbits in [0, 0x40, 0x80, 0xc0] {
            assert_eq!(
                decode(&[0x66, 0x0f, 0x22, modbits]),
                Ok(ControlOp {
                    cr: 0,
                    gpr: 0,
                    bytes: 4
                })
            );
        }
        assert_eq!(decode(&[0xf0, 0x0f, 0x22, 0xc0]), Err(Error::Undefined));
        assert_eq!(decode(&[0x0f, 0x22, 0xc8]), Err(Error::Undefined));
        assert_eq!(decode(&[0x0f, 0x22, 0xd0]), Err(Error::Unsupported));
    }
    #[test]
    fn validation_precedes_commit_and_numeric_equality_is_not_identity() {
        let mut g = GuestState::seed(&NativeFrame::ZERO);
        g.control[0] = 0x10011;
        g.control[3] = 0x600;
        g.control[4] = 0;
        let mut generation = 1;
        let op = decode(&[0x0f, 0x22, 0xc0]).unwrap();
        for value in [0x80010010, 0x20010011] {
            g.gpr[0] = value;
            let before = g;
            assert_eq!(
                apply(&mut g, &mut generation, 0, op),
                Err(Error::GeneralProtection)
            );
            assert_eq!(g, before);
            assert_eq!(generation, 1);
        }
        g.gpr[0] = 0x10081;
        apply(&mut g, &mut generation, 0, op).unwrap();
        assert_eq!(g.control[0], 0x10011);
        let cr3 = decode(&[0x0f, 0x22, 0xd8]).unwrap();
        for value in [0x1000, 0x3000, 0x1000, 0x1000, 0x1003] {
            g.gpr[0] = value;
            apply(&mut g, &mut generation, 0, cr3).unwrap();
            assert_eq!(g.control[2], value);
        }
        assert_eq!(generation, 7);
        let before = g;
        assert_eq!(
            apply(&mut g, &mut generation, 3, op),
            Err(Error::GeneralProtection)
        );
        assert_eq!(g, before);
        g.gpr[0] = 0x620;
        assert_eq!(
            apply(
                &mut g,
                &mut generation,
                0,
                decode(&[0x0f, 0x22, 0xe0]).unwrap()
            ),
            Err(Error::Unsupported)
        );
        for bit in [16, 18] {
            g.gpr[0] = 0x600 | (1 << bit);
            let before = g;
            let old_generation = generation;
            assert_eq!(
                apply(
                    &mut g,
                    &mut generation,
                    0,
                    decode(&[0x0f, 0x22, 0xe0]).unwrap()
                ),
                Err(Error::Unsupported)
            );
            assert_eq!(g, before);
            assert_eq!(generation, old_generation);
        }
        g.gpr[0] = 1 << 31;
        assert_eq!(
            apply(
                &mut g,
                &mut generation,
                0,
                decode(&[0x0f, 0x22, 0xe0]).unwrap()
            ),
            Err(Error::GeneralProtection)
        );
    }
}
