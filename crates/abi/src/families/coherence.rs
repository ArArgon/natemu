//! Finite shared-table coherence campaign; observations are resident records.
use crate::{
    CODE_VA, NativeFrame, PRIVATE, families::pae::Hidden, paging::Translation, recovery::GuestState,
};
pub const MODE: u64 = 12;
pub const OWNER_OFFSET: usize = 0x26000;
pub const RECORD_OFFSET: usize = 0x70000;
pub const COMMON_OFFSET: usize = 0x93000;
pub const RAM_OFFSET: usize = 0x94000;
pub const TABLE_OFFSET: usize = 0x98000;
pub const TABLE_BYTES: usize = 0x6000;
pub const DATA_BYTES: usize = 8192;
pub const IMAGE_VA_OFFSET: usize = 0x100000;
pub const SNAPSHOT_BYTES: usize = TABLE_BYTES + DATA_BYTES;
pub const MAX_SNAPSHOTS: usize = 24;
pub const DATA: u64 = 0x100000;
pub const RIP: u64 = 0x102000;
pub const STAGES: usize = 8;
pub const VISITS: usize = STAGES * 2;
pub const CODE_BYTES: usize = 32;
pub const MAX_WALKS: usize = 6;
pub const MISS: u64 = 0x493456a1;
pub const DELIVER: u64 = 0x493456a2;
pub const STORE: usize = 0;
pub const LOAD: usize = 1;

// Trace: 0x1000+visit*8+walk = complete ordered table walk; 0x2000+visit
// admitted/prepared; 0x3000+visit native completion; 0x4000+visit guest PF.
// Coordinator events1..5 retain their existing meanings. Other fixed events:
pub const COMPILED: u64 = 60;
pub const START: u64 = 61;
pub const READY: u64 = 62;
pub const RETAINED: u64 = 63;
pub const MUTATION: u64 = 64;
pub const ACK: u64 = 65;
pub const ENTRY_BLOCKED: u64 = 66;
pub const REUSE_BLOCKED: u64 = 67;
pub const OPENED: u64 = 68;
pub const RELEASE: u64 = 69;
pub const CODE_CLEARED: u64 = 70;
pub const TERMINAL: u64 = 71;
pub const PDPT_CHANGED: u64 = 72;
pub const REPAIR: u64 = 73;
pub const RETRY: u64 = 74;
pub const TERMINAL_REUSE_BLOCKED: u64 = 75;

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
    pub fn context(self) -> u64 {
        (self.profile + 1) | (self.n << 8) | (self.actor << 16)
    }
    pub fn four(self) -> bool {
        matches!(self.profile, 0 | 3 | 5)
    }
    pub fn pae(self) -> bool {
        matches!(self.profile, 2 | 4)
    }
    pub fn ad(self) -> bool {
        matches!(self.profile, 1..=3)
    }
    pub fn bitness(self) -> u64 {
        if self.four() { 64 } else { 32 }
    }
    pub fn mode(self) -> u64 {
        if self.four() {
            4
        } else if self.pae() {
            2
        } else {
            1
        }
    }
    pub fn cr3(self) -> u64 {
        if self.pae() { 0x1020 } else { 0x1000 }
    }
    pub fn parent(self) -> u64 {
        if self.four() {
            0x3000
        } else if self.pae() {
            0x2000
        } else {
            0x1000
        }
    }
    pub fn first(self) -> u64 {
        if self.four() {
            0x4000
        } else if self.pae() {
            0x3000
        } else {
            0x2000
        }
    }
    pub fn second(self) -> u64 {
        self.first() + 4096
    }
    pub fn entry_bytes(self) -> usize {
        if self.profile == 1 { 4 } else { 8 }
    }
    pub fn guest(self, frame: &NativeFrame) -> GuestState {
        let mut g = GuestState::seed(frame);
        for (i, r) in [
            (0, 4),
            (3, 6),
            (1, 7),
            (2, 5),
            (6, 10),
            (7, 11),
            (5, 9),
            (4, 8),
        ] {
            g.gpr[i] = if self.four() {
                frame.x[r]
            } else {
                frame.x[r] & 0xffffffff
            };
        }
        g.control[0] = 0x80010011;
        g.control[2] = self.cr3();
        g.control[3] = if self.profile == 1 { 0x600 } else { 0x620 };
        g.control[4] = if self.four() { 0xd00 } else { 0 };
        g.segments[0] = [8, 0, 0xffffffff, if self.four() { 0xa09b } else { 0xc09b }];
        g
    }
    pub fn address(self, id: u64, visit: usize) -> u64 {
        let stage = visit / 2;
        let page = visit % 2;
        DATA + page as u64 * 4096
            + id * 256
            + if self.four() && (id + stage as u64 + page as u64) % 2 != 0 {
                crate::shadow::HIGH_BASE
            } else {
                0
            }
    }
}
pub fn tables(c: Config) -> [u8; TABLE_BYTES] {
    let mut out = [0; TABLE_BYTES];
    let bytes = c.entry_bytes();
    let mut set = |address: u64, value: u64| {
        let at = (address - 0x1000) as usize;
        out[at..at + bytes].copy_from_slice(&value.to_le_bytes()[..bytes]);
    };
    if c.four() {
        set(0x1000, 0x2007);
        set(0x1800, 0x2007);
        set(0x2000, 0x3007);
    }
    if c.pae() {
        set(0x1020, 0x2001);
        set(
            0x6000,
            if c.profile == 4 {
                c.first() | 7
            } else {
                c.second() | 7
            },
        );
    }
    set(
        c.parent(),
        if c.profile >= 4 {
            c.second() | 7
        } else {
            c.first() | 7
        },
    );
    for (pt, frames) in [
        (c.first(), [DATA, DATA + 4096]),
        (c.second(), [DATA + 4096, DATA]),
    ] {
        for (index, frame) in [(256, frames[0]), (257, frames[1]), (258, RIP)] {
            set(pt + index * bytes as u64, frame | 7);
        }
    }
    out
}
pub fn kind(visit: usize) -> usize {
    if matches!(visit / 2, 4 | 6) {
        LOAD
    } else {
        STORE
    }
}
pub fn retained(visit: usize) -> bool {
    matches!(visit / 2, 1 | 3)
}
pub fn fault_class(c: Config, visit: usize) -> u64 {
    match visit / 2 {
        1 | 3 | 4 | 6 => 1,
        2 if !c.ad() => 1,
        5 if c.ad() => 2,
        7 => 2,
        _ => 0,
    }
}
pub fn direct_abort(
    c: Config,
    visit: usize,
    esr: u64,
    far: u64,
    pc: u64,
    spsr: u64,
    id: u64,
) -> bool {
    let write = kind(visit) == STORE;
    let class = fault_class(c, visit);
    visit < VISITS
        && id < c.n
        && class != 0
        && esr >> 26 == 0x25
        && ((esr >> 6) & 1) == write as u64
        && esr & ((1 << 7) | (1 << 8) | (1 << 10)) == 0
        && spsr & 0x3df == 0x3c4
        && far == c.address(id, visit)
        && pc == CODE_VA + kind(visit) as u64 * 16 + if write { 4 } else { 0 }
        && if class == 1 {
            matches!(esr & 63, 4..=7)
        } else {
            matches!(esr & 63, 12..=15)
        }
}
pub fn token(id: u64, visit: usize) -> u64 {
    0x5c000000 | ((visit as u64 + 1) << 12) | (id << 4) | (visit as u64 & 1)
}
pub fn source(kind: usize) -> Option<[u8; 2]> {
    match kind {
        STORE => Some([0x89, 3]),
        LOAD => Some([0x8b, 3]),
        _ => None,
    }
}
pub fn body(kind: usize) -> Option<[u8; 8]> {
    match kind {
        STORE => Some([0x1f, 0x20, 0x03, 0xd5, 0xc4, 0xfc, 0x9f, 0x88]),
        LOAD => Some([0xc4, 0xfc, 0xdf, 0x88, 0x1f, 0x20, 0x03, 0xd5]),
        _ => None,
    }
}
pub fn request(vm: u64, c: Config, id: u64, kind: usize) -> Option<[u64; 16]> {
    if vm == 0 || Config::new(c.n, c.profile, c.actor) != Some(c) || id >= c.n {
        return None;
    }
    let src = source(kind)?;
    Some([
        MISS,
        1,
        vm,
        id,
        1,
        kind as u64 + 1,
        c.context(),
        RIP + id * 256 + kind as u64 * 16,
        c.bitness(),
        u16::from_le_bytes(src) as u64,
        2,
        8,
        kind as u64,
        1,
        0,
        0,
    ])
}
pub fn decode(p: &[u64; 16]) -> Option<Config> {
    let c = Config::new((p[6] >> 8) & 255, (p[6] & 255).checked_sub(1)?, p[6] >> 16)?;
    (request(p[2], c, p[3], p[12] as usize) == Some(*p)).then_some(c)
}
pub fn reply(p: &[u64; 16]) -> Option<[u64; 16]> {
    decode(p)?;
    let mut r = *p;
    r[0] = DELIVER;
    r[14] = u64::from_le_bytes(body(p[12] as usize)?);
    Some(r)
}
pub fn image(returned: u64) -> Option<[u8; CODE_BYTES]> {
    let mut out = [0; CODE_BYTES];
    for kind in 0..2 {
        let at = kind * 16;
        out[at..at + 8].copy_from_slice(&body(kind)?);
        let delta = returned as i128 - (CODE_VA + at as u64 + 8) as i128;
        if delta % 4 != 0 || !(-(1i128 << 27)..(1i128 << 27)).contains(&delta) {
            return None;
        }
        out[at + 8..at + 12]
            .copy_from_slice(&(0x14000000 | ((delta / 4) as u32 & 0x03ffffff)).to_le_bytes());
    }
    Some(out)
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Walk {
    pub address: u64,
    pub intent: u64,
    pub gpa: u64,
    pub leaf_bytes: u64,
    pub writable: u64,
    pub dirty: u64,
    pub user: u64,
    pub executable: u64,
    pub wb: u64,
    pub vector: u64,
    pub error: u64,
    pub read_count: u64,
    pub write_count: u64,
    pub reads: [[u64; 2]; 4],
    pub writes: [[u64; 3]; 4],
}
impl Walk {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
    pub fn translation(&self) -> Translation {
        Translation {
            gpa: self.gpa,
            leaf_bytes: self.leaf_bytes,
            writable: self.writable != 0,
            dirty: self.dirty != 0,
            user: self.user != 0,
            executable: self.executable != 0,
            wb: self.wb != 0,
        }
    }
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Visit {
    pub index: u64,
    pub stage: u64,
    pub page: u64,
    pub kind: u64,
    pub address: u64,
    pub rip: u64,
    pub initial: NativeFrame,
    pub fault: NativeFrame,
    pub captured: NativeFrame,
    pub admitted: u64,
    pub completed: u64,
    pub faults: u64,
    pub retries: u64,
    pub esr: u64,
    pub far: u64,
    pub pc: u64,
    pub spsr: u64,
    pub resume_pc: u64,
    pub saved_gpa: u64,
    pub saved_writable: u64,
    pub saved_leaf_bytes: u64,
    pub vector: u64,
    pub error: u64,
    pub cr2_before: u64,
    pub cr2_after: u64,
    pub native_before: [u64; 4],
    pub native_after: [u64; 4],
    pub memory_before: [u8; 4],
    pub memory_after: [u8; 4],
    pub walk_count: u64,
    pub walks: [Walk; MAX_WALKS],
}
impl Visit {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Owner {
    pub id: u64,
    pub visit: u64,
    pub completed: u64,
    pub faults: u64,
    pub retries: u64,
    pub initial: NativeFrame,
    pub current: NativeFrame,
    pub guest_before: GuestState,
    pub guest_after: GuestState,
    pub expected: [u64; 16],
    pub requests: [[u64; 16]; 2],
    pub replies: [[u64; 16]; 2],
    pub compile_walks: [Walk; 2],
    pub transfers: u64,
    pub hidden_before: Hidden,
    pub hidden_after: Hidden,
    pub hidden_reads: [u64; 4],
    pub hidden_loads: u64,
    pub code: [u8; CODE_BYTES],
    pub code_before_clear: [u8; CODE_BYTES],
    pub code_after_clear: [u8; CODE_BYTES],
    pub return_pc: u64,
    pub acknowledgements: u64,
    pub ack_leaves: [[u64; 4]; 3],
    pub mapping_gpa: [u64; 2],
    pub mapping_writable: [u64; 2],
    pub mapping_leaf_bytes: [u64; 2],
    pub entry_blocked: u64,
    pub reuse_blocked: u64,
    pub released: u64,
    pub entry_rejected: u64,
    pub terminal_reuse_blocked: u64,
    pub code_while_held: [u8; CODE_BYTES],
}
impl Owner {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Snapshot {
    pub kind: u64,
    pub visit: u64,
    pub mutation: u64,
    pub generation: u64,
    pub active: u64,
    pub queued: u64,
    pub servicing: u64,
    pub retained: u64,
    pub acknowledgements: u64,
}
impl Snapshot {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Common {
    pub n: u64,
    pub profile: u64,
    pub actor: u64,
    pub initialized: u64,
    pub compiled: u64,
    pub index: u64,
    pub prepared: u64,
    pub done: u64,
    pub mutations: u64,
    pub acknowledgements: u64,
    pub blocked: u64,
    pub released: u64,
    pub reclaimed: u64,
    pub pdpt_changed: u64,
    pub reuse_blocked: u64,
    pub snapshot_count: u64,
    pub snapshots: [Snapshot; MAX_SNAPSHOTS],
}
impl Common {
    pub fn new(c: Config) -> Self {
        Self {
            n: c.n,
            profile: c.profile,
            actor: c.actor,
            ..unsafe { core::mem::zeroed() }
        }
    }
}
const _: () = assert!(core::mem::size_of::<Owner>() <= 8192);
const _: () = assert!(core::mem::size_of::<Visit>() * VISITS <= 0x20000);
const _: () = assert!(core::mem::size_of::<Common>() <= 4096);
const _: () = assert!(MAX_SNAPSHOTS * SNAPSHOT_BYTES <= crate::RAM_SIZE);
const _: () = assert!(PRIVATE + IMAGE_VA_OFFSET as u64 > crate::CODE_VA);

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn packet_mode_owner_kind_and_every_reply_word_are_bound() {
        for profile in 0..6 {
            for n in 1..=4 {
                for actor in 0..n {
                    let c = Config::new(n, profile, actor).unwrap();
                    for id in 0..n {
                        for kind in 0..2 {
                            let p = request(37, c, id, kind).unwrap();
                            assert_eq!(decode(&p), Some(c));
                            assert_eq!(p[8], if matches!(profile, 0 | 3 | 5) { 64 } else { 32 });
                            let r = reply(&p).unwrap();
                            assert_eq!(r[14], u64::from_le_bytes(body(kind).unwrap()));
                            for word in 0..16 {
                                let mut corrupt = r;
                                corrupt[word] ^= 1;
                                assert_ne!(Some(corrupt), reply(&p));
                            }
                        }
                    }
                }
            }
        }
    }
    #[test]
    fn four_level_addresses_really_use_both_aliases_and_two_subpages() {
        let c = Config::new(4, 3, 2).unwrap();
        assert_eq!(c.address(0, 0), DATA);
        assert_eq!(c.address(0, 1), crate::shadow::HIGH_BASE + DATA + 4096);
        assert_eq!(c.address(0, 2), crate::shadow::HIGH_BASE + DATA);
        assert!(Config::new(4, 2, 2).unwrap().address(3, 15) < 1 << 32);
    }
    #[test]
    fn table_profiles_keep_source_identity_and_pae_alternate_root_distinct() {
        for profile in 0..6 {
            let c = Config::new(1, profile, 0).unwrap();
            let t = tables(c);
            let read = |at: u64| {
                let mut b = [0; 8];
                let i = (at - 0x1000) as usize;
                b[..c.entry_bytes()].copy_from_slice(&t[i..i + c.entry_bytes()]);
                u64::from_le_bytes(b)
            };
            assert_eq!(read(c.first() + 258 * c.entry_bytes() as u64), RIP | 7);
            assert_eq!(read(c.second() + 258 * c.entry_bytes() as u64), RIP | 7);
            assert_eq!(
                read(c.parent()),
                if profile >= 4 {
                    c.second() | 7
                } else {
                    c.first() | 7
                }
            );
            if c.pae() {
                assert_eq!(
                    read(0x6000),
                    if profile == 4 {
                        c.first() | 7
                    } else {
                        c.second() | 7
                    }
                );
            }
        }
    }
    #[test]
    fn precise_abort_classes_and_syndrome_flags_are_required() {
        for profile in 0..6 {
            let c = Config::new(4, profile, 0).unwrap();
            for visit in 0..VISITS {
                let class = fault_class(c, visit);
                let write = kind(visit) == STORE;
                let esr =
                    (0x25 << 26) | if write { 64 } else { 0 } | if class == 2 { 15 } else { 7 };
                let pc = CODE_VA + kind(visit) as u64 * 16 + if write { 4 } else { 0 };
                assert_eq!(
                    direct_abort(c, visit, esr, c.address(2, visit), pc, 0xa00003c4, 2),
                    class != 0
                );
                if class != 0 {
                    for bit in [6, 7, 8, 10] {
                        assert!(!direct_abort(
                            c,
                            visit,
                            esr ^ (1 << bit),
                            c.address(2, visit),
                            pc,
                            0x3c4,
                            2
                        ));
                    }
                    assert!(!direct_abort(
                        c,
                        visit,
                        (esr & !63) | if class == 1 { 15 } else { 7 },
                        c.address(2, visit),
                        pc,
                        0x3c4,
                        2
                    ));
                }
            }
        }
    }
}
