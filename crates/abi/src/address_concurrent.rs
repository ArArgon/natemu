//! Finite protected32 offsets, segment formation, physical routes and live replies.
use crate::{NativeFrame, paging::Access, recovery::GuestState, validation::Packet};
pub const OWNER_OFFSET: usize = 0x26000;
pub const RECORD_OFFSET: usize = 0x84000;
pub const SHARED_OFFSET: usize = 0x93000;
pub const MAX_VISITS: usize = 21;
pub const SLOT_BYTES: usize = 32;
pub const CODE_BYTES: usize = 96;
pub const EIP: u64 = 0x2000;
pub const MISS: u64 = 0x49345671;
pub const DELIVER: u64 = 0x49345672;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    pub n: u64,
    pub profile: u64,
    pub actor: u64,
}
impl Config {
    pub fn new(n: u64, profile: u64, actor: u64) -> Option<Self> {
        (n > 0 && n <= 4 && profile < 6 && actor < n).then_some(Self { n, profile, actor })
    }
    pub fn mode(self, id: u64) -> u64 {
        u64::from(id != self.actor)
    }
    pub fn a20(self) -> u64 {
        u64::from(self.profile != 1)
    }
    pub fn mask(self, gpa: u64) -> u64 {
        if self.a20() == 0 {
            gpa & !(1 << 20)
        } else {
            gpa
        }
    }
    pub fn cs(self, id: u64, terminal: bool) -> [u64; 4] {
        [
            8,
            if id == self.actor { 0x300123 } else { 0x400123 },
            if terminal && self.profile == 5 {
                EIP + 2
            } else {
                0xffff_ffff
            },
            if terminal && self.profile == 5 {
                0x409b
            } else {
                0xc09b
            },
        ]
    }
    pub fn ds(self, id: u64) -> [u64; 4] {
        let base = if id == self.actor {
            if self.profile == 2 {
                0xfff00080
            } else {
                0x200123
            }
        } else if self.profile == 2 {
            0x400080
        } else {
            0x400123
        };
        [0x10, base, 0xffff_ffff, 0xc093]
    }
    pub fn ebx(self, id: u64, terminal: bool) -> u64 {
        if terminal && self.profile == 4 {
            0xffff_fffe
        } else if self.profile == 3 {
            0x1234fff0
        } else {
            0x100040 + id * 256
        }
    }
    pub fn edx(self, id: u64) -> u64 {
        if self.profile == 3 {
            0x33440000 + id
        } else {
            0x1000c0 + id * 256
        }
    }
    pub fn esi(self, id: u64) -> u64 {
        0x56780030 + id * 256
    }
    pub fn data_raw(self, alternate: bool) -> u64 {
        let old = match self.profile {
            2 => 0,
            3 => 0x200000,
            _ => 0x300000,
        };
        if alternate {
            if self.profile == 2 {
                0x300000
            } else {
                old + 4096
            }
        } else {
            old
        }
    }
    pub fn data_leaf(self) -> u64 {
        if self.profile == 3 { 0 } else { 256 }
    }
    pub fn fetch(self, id: u64, terminal: bool, step: usize) -> Option<Address> {
        let source = source(self.profile, 1, step)?;
        let mut a = form(
            self.cs(id, terminal),
            EIP + offset(self.profile, step)?,
            source.len() as u64,
            Access::Fetch,
        )
        .ok()?;
        a.raw = 0x302123 + offset(self.profile, step)?;
        a.gpa = self.mask(a.raw);
        Some(a)
    }
    pub fn context(self, id: u64, terminal: bool) -> u64 {
        (self.profile + 1)
            | (self.n << 8)
            | (self.actor << 16)
            | (u64::from(terminal) << 24)
            | (self.mode(id) << 25)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct Address {
    pub offset: u64,
    pub linear: u64,
    pub raw: u64,
    pub gpa: u64,
}
impl Address {
    pub const ZERO: Self = Self {
        offset: 0,
        linear: 0,
        raw: 0,
        gpa: 0,
    };
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AddressError {
    GeneralProtection,
    Unsupported,
}
pub fn form(
    segment: [u64; 4],
    offset: u64,
    bytes: u64,
    access: Access,
) -> Result<Address, AddressError> {
    if bytes == 0
        || offset > u32::MAX as u64
        || segment[1] > u32::MAX as u64
        || segment[2] > u32::MAX as u64
    {
        return Err(AddressError::Unsupported);
    }
    let end = offset
        .checked_add(bytes - 1)
        .ok_or(AddressError::Unsupported)?;
    if end > segment[2] || (access == Access::Write && segment[3] & 2 == 0) {
        return Err(AddressError::GeneralProtection);
    }
    let linear = (segment[1] as u32).wrapping_add(offset as u32) as u64;
    if (linear & 4095) + bytes > 4096 {
        return Err(AddressError::Unsupported);
    }
    Ok(Address {
        offset,
        linear,
        raw: 0,
        gpa: 0,
    })
}
pub fn operand(profile: u64, frame: &NativeFrame, step: usize) -> Option<u64> {
    if !matches!(step, 1 | 2) {
        return None;
    }
    Some(if profile == 3 {
        frame.x[6].wrapping_add(frame.x[10]) & 0xffff
    } else {
        frame.x[if step == 1 { 6 } else { 5 }] & 0xffff_ffff
    })
}
pub fn offset(profile: u64, step: usize) -> Option<u64> {
    [
        0,
        2,
        if profile == 3 { 5 } else { 4 },
        if profile == 3 { 8 } else { 6 },
    ]
    .get(step)
    .copied()
}
pub fn source(profile: u64, generation: u64, step: usize) -> Option<&'static [u8]> {
    if profile >= 6 || !matches!(generation, 1 | 2) {
        return None;
    }
    match step {
        0 => Some(&[0x89, 0xc1]),
        1 => Some(match (profile == 3, generation) {
            (false, 1) => &[0x89, 0x0b],
            (false, 2) => &[0x89, 0x13],
            (true, 1) => &[0x67, 0x89, 0x08],
            _ => &[0x67, 0x89, 0x10],
        }),
        2 => Some(if profile == 3 {
            &[0x67, 0x8b, 0x00]
        } else {
            &[0x8b, 0x02]
        }),
        _ => None,
    }
}
pub fn body(profile: u64, generation: u64, step: usize) -> Option<&'static [u8]> {
    source(profile, generation, step)?;
    Some(match step {
        0 => &[0xe7, 3, 4, 0xaa],
        1 => match (profile == 3, generation) {
            (false, 1) => &[0x1f, 0x20, 3, 0xd5, 0xc7, 0xfc, 0x9f, 0x88],
            (false, 2) => &[0x1f, 0x20, 3, 0xd5, 0xc5, 0xfc, 0x9f, 0x88],
            (true, 1) => &[
                0xd4, 0, 0x0a, 0x0b, 0x94, 0x3e, 0, 0x53, 0x1f, 0x20, 3, 0xd5, 0x87, 0xfe, 0x9f,
                0x88,
            ],
            _ => &[
                0xd4, 0, 0x0a, 0x0b, 0x94, 0x3e, 0, 0x53, 0x1f, 0x20, 3, 0xd5, 0x85, 0xfe, 0x9f,
                0x88,
            ],
        },
        _ => {
            if profile == 3 {
                &[
                    0xd4, 0, 0x0a, 0x0b, 0x94, 0x3e, 0, 0x53, 0x84, 0xfe, 0xdf, 0x88, 0x1f, 0x20,
                    3, 0xd5,
                ]
            } else {
                &[0xa4, 0xfc, 0xdf, 0x88, 0x1f, 0x20, 3, 0xd5]
            }
        }
    })
}
pub fn site(profile: u64, step: usize) -> Option<(u64, usize)> {
    match step {
        1 => Some(if profile == 3 { (12, 20) } else { (4, 6) }),
        2 => Some(if profile == 3 { (8, 20) } else { (0, 5) }),
        _ => None,
    }
}
/// Guest RAM uses shared backing; source poison and inactive A20 routes are distinct.
pub fn ram(c: Config, gpa: u64, bytes: u64) -> Option<usize> {
    let end = gpa.checked_add(bytes.checked_sub(1)?)?;
    let mappings = [
        (c.mask(c.data_raw(false)), 0x70000),
        (c.mask(c.data_raw(true)), 0x71000),
        (0x302000, if c.a20() == 0 { 0x73000 } else { 0x72000 }),
        (0x202000, if c.a20() == 0 { 0x72000 } else { 0x73000 }),
    ];
    mappings.into_iter().find_map(|(base, back)| {
        (gpa >= base && end < base + 4096).then(|| back + (gpa - base) as usize)
    })
}
pub fn request(
    vm: u64,
    c: Config,
    id: u64,
    generation: u64,
    serial: u64,
    terminal: bool,
    step: usize,
    fetch_gpa: u64,
) -> Option<Packet> {
    if vm == 0
        || serial == 0
        || id >= c.n
        || Config::new(c.n, c.profile, c.actor) != Some(c)
        || terminal && (c.profile < 4 || id != c.actor)
    {
        return None;
    }
    let a = c.fetch(id, terminal, step)?;
    if a.gpa != fetch_gpa {
        return None;
    }
    let source = source(c.profile, generation, step)?;
    let body = body(c.profile, generation, step)?;
    let mut bytes = [0; 8];
    bytes[..source.len()].copy_from_slice(source);
    Some([
        MISS,
        1,
        vm,
        id,
        1,
        generation,
        c.context(id, terminal),
        EIP + offset(c.profile, step)?,
        32,
        u64::from_le_bytes(bytes),
        source.len() as u64,
        a.linear,
        a.gpa,
        serial,
        body.len() as u64,
        step as u64,
    ])
}
pub fn decode(p: &Packet) -> Option<(Config, bool)> {
    let c = Config::new(
        (p[6] >> 8) & 255,
        (p[6] & 255).checked_sub(1)?,
        (p[6] >> 16) & 255,
    )?;
    let terminal = p[6] & (1 << 24) != 0;
    (request(p[2], c, p[3], p[5], p[13], terminal, p[15] as usize, p[12]) == Some(*p))
        .then_some((c, terminal))
}
pub fn reply(p: &Packet) -> Option<Packet> {
    let (c, _) = decode(p)?;
    let mut bytes = [0; 16];
    let body = body(c.profile, p[5], p[15] as usize)?;
    bytes[..body.len()].copy_from_slice(body);
    let mut out = *p;
    out[0] = DELIVER;
    out[9] = u64::from_le_bytes(bytes[..8].try_into().unwrap());
    out[10] = u64::from_le_bytes(bytes[8..].try_into().unwrap());
    Some(out)
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Visit {
    pub request: Packet,
    pub reply: Packet,
    pub prefix: NativeFrame,
    pub fault: NativeFrame,
    pub program: u64,
    pub step: u64,
    pub generation: u64,
    pub terminal: u64,
    pub fetch: Address,
    pub access: Address,
    pub entered: u64,
    pub completed: u64,
    pub esr: u64,
    pub far: u64,
    pub pc: u64,
    pub spsr: u64,
    pub service: u64,
    pub continuation: u64,
    pub value: u64,
    pub memory_before: [u8; 4],
    pub memory_after: [u8; 4],
}
impl Visit {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
pub struct Owner {
    pub enabled: u64,
    pub id: u64,
    pub mode: u64,
    pub context: u64,
    pub generation: u64,
    pub serial: u64,
    pub program: u64,
    pub step: u64,
    pub visits: u64,
    pub current_visit: u64,
    pub terminal: u64,
    pub code_context: u64,
    pub expected: Packet,
    pub deferred: Packet,
    pub stale_reply: Packet,
    pub initial: NativeFrame,
    pub current: NativeFrame,
    pub guest_before: GuestState,
    pub guest_after: GuestState,
    pub saved_store: Address,
    pub saved_load: Address,
    pub walks: u64,
    pub reads: u64,
    pub writes: u64,
    pub stale_rejected: u64,
    pub wrong_owner_rejected: u64,
    pub entry_rejected: u64,
    pub retained_before_release: u64,
    pub reclaimed: u64,
    pub vector: u64,
    pub error: u64,
    pub fault_address: u64,
    pub fault_rip: u64,
    pub maintenance_acks: [[u64; 6]; 36],
    pub source_ack: [u64; 6],
    pub terminal_initial: NativeFrame,
}
impl Owner {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
pub struct Shared {
    pub n: u64,
    pub profile: u64,
    pub actor: u64,
    pub compiled: u64,
    pub old_done: u64,
    pub source_changed: u64,
    pub acknowledgements: u64,
    pub new_done: u64,
    pub maintenance_started: u64,
    pub terminal_ready: u64,
    pub terminal_done: u64,
    pub terminal_queued: u64,
    pub terminal_queue: [u64; 4],
    pub terminal_length: u64,
    pub terminal_head: u64,
    pub terminal_servicing: u64,
    pub released: u64,
    pub reclaimed: u64,
    pub source_write: Address,
    pub source_before: [u8; 8],
    pub source_after: [u8; 8],
    pub source_leaf_before: u64,
    pub source_leaf_after: u64,
    pub data_leaf_before: u64,
    pub data_leaf_after: u64,
    pub code_before: [[u8; CODE_BYTES]; 4],
    pub code_after: [[u8; CODE_BYTES]; 4],
    pub terminal_tables_before: [u8; 16384],
    pub terminal_data_before: [u8; 8192],
}
impl Shared {
    pub fn new(n: u64, profile: u64, actor: u64) -> Option<Self> {
        Config::new(n, profile, actor)?;
        let mut out: Self = unsafe { core::mem::zeroed() };
        out.n = n;
        out.profile = profile;
        out.actor = actor;
        Some(out)
    }
}
const _: () = assert!(OWNER_OFFSET + core::mem::size_of::<Owner>() <= 0x28000);
const _: () = assert!(RECORD_OFFSET + MAX_VISITS * core::mem::size_of::<Visit>() <= 0x90000);
const _: () = assert!(SHARED_OFFSET + core::mem::size_of::<Shared>() <= 0xa0000);
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn truncate_segment_wrap_and_a20_are_separate() {
        let c = Config::new(4, 3, 2).unwrap();
        let mut f = NativeFrame::ZERO;
        f.x[6] = c.ebx(2, false);
        f.x[10] = c.esi(2);
        assert_eq!(operand(3, &f, 1), Some(0x220));
        assert_eq!(
            form(c.ds(2), 0x220, 4, Access::Write).unwrap().linear,
            0x200343
        );
        let c = Config::new(4, 2, 2).unwrap();
        assert_eq!(
            form(c.ds(2), c.ebx(2, false), 4, Access::Write)
                .unwrap()
                .linear,
            0x2c0
        );
        let c = Config::new(4, 1, 2).unwrap();
        assert_eq!(c.fetch(2, false, 0).unwrap().gpa, 0x202123);
        assert_eq!(c.fetch(0, false, 0).unwrap().linear, 0x402123);
        assert_eq!(
            form(c.ds(2), 0xfffffffe, 4, Access::Write),
            Err(AddressError::GeneralProtection)
        );
    }
    #[test]
    fn packet_binds_owner_mode_address_source_and_terminal() {
        for profile in 0..6 {
            let c = Config::new(4, profile, 2).unwrap();
            for id in 0..4 {
                for step in 0..3 {
                    let a = c.fetch(id, false, step).unwrap();
                    let p = request(13, c, id, 1, 1, false, step, a.gpa).unwrap();
                    assert!(reply(&p).is_some());
                    for i in 0..16 {
                        let mut bad = p;
                        bad[i] ^= 1;
                        assert_ne!(reply(&bad), reply(&p), "{profile} {id} {step} {i}");
                    }
                }
            }
        }
        let c = Config::new(2, 5, 1).unwrap();
        assert!(c.fetch(1, true, 1).is_none());
    }
}
