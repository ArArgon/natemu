//! Shared 32-bit code with per-owner paging and compiler identity.
use crate::{
    NativeFrame,
    paging::{self, Access, Fault, Tables, Translation},
    recovery::GuestState,
    families::validation::Packet,
};
pub const OFFSET: usize = 0x23000;
pub const MISS: u64 = 0x49345641;
pub const DELIVER: u64 = 0x49345642;
pub const RIP: u64 = crate::translation::SOURCE_VA;
pub const BODY: [u8; 20] = [
    0xe7, 3, 4, 0xaa, 0x1f, 0x20, 3, 0xd5, 0xc7, 0xfc, 0x9f, 0x88, 0xa4, 0xfc, 0xdf, 0x88, 0x1f,
    0x20, 3, 0xd5,
];
pub const SOURCE: [u8; 6] = [0x89, 0xc1, 0x89, 0x0b, 0x8b, 0x02];
pub const REPLACEMENT: [u8; 6] = [0x89, 0xc1, 0x89, 0x13, 0x8b, 0x02];
pub fn source(generation: u64) -> Option<[u8; 6]> {
    match generation {
        1 => Some(SOURCE),
        2 => Some(REPLACEMENT),
        _ => None,
    }
}
pub fn body(generation: u64) -> Option<[u8; 20]> {
    source(generation)?;
    let mut out = BODY;
    if generation == 2 {
        out[8] = 0xc5;
    }
    Some(out)
}
pub fn owner_mode(profile: u64, owner: u64) -> Option<u64> {
    if !(1..=2).contains(&profile) || owner >= crate::concurrency::MAX_CPUS as u64 {
        return None;
    }
    Some((owner + profile - 1) & 1)
}
pub fn mode(g: &GuestState, a20: u64) -> Option<u64> {
    if a20 != 1
        || g.segments[0] != [8, 0, 0xffff_ffff, 0xc09b]
        || !g.segments[1..]
            .iter()
            .all(|s| *s == [0x10, 0, 0xffff_ffff, 0xc093])
    {
        return None;
    }
    match (g.control[0], g.control[3], g.control[4]) {
        (0x10001, 0x600, 0) => Some(0),
        (0x80010001, 0x620, 0xd00) if g.control[2] == 0x1000 => Some(1),
        _ => None,
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route {
    Identity(u64),
    Paged(Translation),
}
impl Route {
    pub fn gpa(self) -> u64 {
        match self {
            Self::Identity(gpa) => gpa,
            Self::Paged(t) => t.gpa,
        }
    }
    pub fn writable(self) -> bool {
        match self {
            Self::Identity(_) => true,
            Self::Paged(t) => t.writable && t.dirty,
        }
    }
}
/// Flat present CPL0 segments and 32-bit offsets are admission requirements.
/// Ownership is checked after translation; a missing physical route is not #PF.
pub fn resolve<T: Tables>(
    tables: &mut T,
    g: &GuestState,
    a20: u64,
    address: u64,
    bytes: u64,
    access: Access,
) -> Result<Route, Fault> {
    let m = mode(g, a20).ok_or(Fault::Unsupported)?;
    let end = address
        .checked_add(bytes.checked_sub(1).ok_or(Fault::Unsupported)?)
        .ok_or(Fault::Unsupported)?;
    let source = access == Access::Fetch
        || (access == Access::Write && (RIP..RIP + 4096).contains(&address));
    let base = if source { RIP } else { crate::DATA_VA };
    if address < base || end >= base + 4096 {
        return Err(Fault::Unsupported);
    }
    let route = if m == 0 {
        Route::Identity(address)
    } else {
        Route::Paged(paging::walk(
            tables,
            paging::Control {
                cr3: g.control[2],
                cpl: 0,
                wp: 1,
                nxe: 1,
                smep: 0,
            },
            address,
            access,
        )?)
    };
    let gpa = route.gpa();
    let owned = if source {
        (RIP..RIP + 4096).contains(&gpa)
    } else {
        (crate::DATA_VA..crate::DATA_VA + 8192).contains(&gpa)
    };
    if !owned || gpa & 4095 != address & 4095 || matches!(route, Route::Paged(t) if !t.wb) {
        return Err(Fault::Unsupported);
    }
    Ok(route)
}
pub fn request(
    vm: u64,
    owner: u64,
    incarnation: u64,
    generation: u64,
    context: u64,
    mode: u64,
    serial: u64,
) -> Option<Packet> {
    if vm == 0
        || owner >= crate::concurrency::MAX_CPUS as u64
        || incarnation == 0
        || context == 0
        || mode > 1
        || serial == 0
    {
        return None;
    }
    let mut bytes = [0; 8];
    bytes[..6].copy_from_slice(&source(generation)?);
    Some([
        MISS,
        1,
        vm,
        owner,
        incarnation,
        generation,
        context,
        RIP,
        mode,
        u64::from_le_bytes(bytes),
        RIP,
        serial,
        0,
        0,
        0,
        0,
    ])
}
pub fn reply(p: &Packet) -> Option<Packet> {
    if Some(*p) != request(p[2], p[3], p[4], p[5], p[6], p[8], p[11]) {
        return None;
    }
    let mut bytes = [0; 24];
    bytes[..20].copy_from_slice(&body(p[5])?);
    let mut out = *p;
    out[0] = DELIVER;
    for i in 0..3 {
        out[13 + i] = u64::from_le_bytes(bytes[i * 8..i * 8 + 8].try_into().unwrap());
    }
    Some(out)
}
pub fn validate_reply(expected: &Packet, received: &Packet) -> Option<[u8; 20]> {
    if reply(expected)? != *received {
        return None;
    }
    body(expected[5])
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct State {
    pub mode: u64,
    pub incarnation: u64,
    pub context: u64,
    pub a20: u64,
    pub expected: Packet,
    pub requests: [Packet; 3],
    pub replies: [Packet; 3],
    pub stale_replies: u64,
    pub wrong_owner: u64,
    pub wrong_mode: u64,
    pub blank_before_publish: u64,
    pub fetch_gpa: [u64; 2],
    pub routes: [u64; 2],
    pub source_before: [u8; 6],
    pub source_after: [u8; 6],
    pub initial: NativeFrame,
    pub old_memory_after: [u8; 256],
    pub new_memory_before: [u8; 256],
    pub new_memory_after: [u8; 256],
}
impl State {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
const _: () = assert!(OFFSET + core::mem::size_of::<State>() <= crate::concurrency::ROUND_OFFSET);
#[cfg(test)]
mod tests {
    use super::*;
    fn guest(paged: bool) -> GuestState {
        let mut g = GuestState::seed(&NativeFrame::ZERO);
        g.segments[0][3] = 0xc09b;
        if !paged {
            g.control[0] = 0x10001;
            g.control[3] = 0x600;
            g.control[4] = 0;
        }
        g
    }
    struct Memory {
        words: [u64; 2048],
        reads: usize,
        writes: usize,
    }
    impl Tables for Memory {
        fn read(&mut self, gpa: u64) -> Option<u64> {
            self.reads += 1;
            self.words
                .get((gpa.checked_sub(0x1000)? / 8) as usize)
                .copied()
        }
        fn write(&mut self, gpa: u64, _: u64, value: u64) -> crate::paging::Update {
            self.writes += 1;
            if let Some(at) = gpa
                .checked_sub(0x1000)
                .and_then(|p| self.words.get_mut((p / 8) as usize))
            {
                *at = value;
                crate::paging::Update::Written
            } else {
                crate::paging::Update::Refused
            }
        }
    }
    #[test]
    fn shared_table_remap_does_not_move_identity_routes() {
        let mut m = Memory {
            words: [0; 2048],
            reads: 0,
            writes: 0,
        };
        m.words[0] = 0x2007;
        m.words[512] = 0x3007;
        m.words[1024] = 0x4007;
        m.words[1536 + 256] = crate::DATA_VA | 7;
        m.words[1536 + 258] = RIP | 7;
        let mut off = guest(false);
        off.control[2] = u64::MAX;
        for access in [Access::Read, Access::Write, Access::Fetch] {
            let address = if access == Access::Fetch {
                RIP
            } else {
                crate::DATA_VA + 0x80
            };
            assert_eq!(
                resolve(&mut m, &off, 1, address, 4, access),
                Ok(Route::Identity(address))
            );
        }
        assert_eq!((m.reads, m.writes), (0, 0));
        let paged = guest(true);
        let before = resolve(&mut m, &paged, 1, crate::DATA_VA + 128, 4, Access::Write).unwrap();
        assert_eq!(before.gpa(), crate::DATA_VA + 128);
        assert!(before.writable());
        assert_eq!(m.words[1536 + 256], crate::DATA_VA | 0x67);
        m.words[1536 + 256] = crate::DATA_VA + 4096 | 7;
        let accesses = (m.reads, m.writes);
        assert_eq!(
            resolve(&mut m, &off, 1, crate::DATA_VA + 128, 4, Access::Write)
                .unwrap()
                .gpa(),
            crate::DATA_VA + 128
        );
        assert_eq!((m.reads, m.writes), accesses);
        let after = resolve(&mut m, &paged, 1, crate::DATA_VA + 128, 4, Access::Read).unwrap();
        assert_eq!(after.gpa(), crate::DATA_VA + 4096 + 128);
        assert!(!after.writable());
        m.words[1536 + 256] = 0;
        assert!(matches!(
            resolve(&mut m, &paged, 1, crate::DATA_VA, 4, Access::Read),
            Err(Fault::Page { error: 0, .. })
        ));
        assert!(resolve(&mut m, &off, 1, crate::DATA_VA, 4, Access::Read).is_ok());
    }
    #[test]
    fn mode_and_identity_cannot_be_inferred_from_decode_width() {
        for paged in [false, true] {
            let g = guest(paged);
            assert_eq!(mode(&g, 1), Some(u64::from(paged)));
            let mut bad = g;
            bad.segments[0][3] = 0xa09b;
            assert_eq!(mode(&bad, 1), None);
            assert_eq!(mode(&g, 0), None);
        }
        for owner in 0..4 {
            for mode in 0..2 {
                for generation in 1..=2 {
                    let p =
                        request(37, owner, 1, generation, mode + 1, mode, generation + 1).unwrap();
                    let good = reply(&p).unwrap();
                    assert_eq!(validate_reply(&p, &good), body(generation));
                    for word in 0..16 {
                        let mut bad = good;
                        bad[word] ^= 1;
                        assert!(validate_reply(&p, &bad).is_none());
                    }
                }
            }
        }
        assert!(request(0, 0, 1, 1, 1, 0, 1).is_none());
    }
}
