//! Finite per-instruction protected32 context, address formation and FEX packets.
use crate::{NativeFrame, paging::Access, recovery::GuestState, families::validation::Packet};
pub const CASE: u64 = 3;
pub const STATE_OFFSET: usize = 0x23000;
pub const SLOT_BYTES: usize = 32;
pub const CODE_BYTES: usize = 9 * SLOT_BYTES;
pub const MISS: u64 = 0x49345631;
pub const DELIVER: u64 = 0x49345632;
pub const EIP: u64 = 0x2000;
pub const OFFSETS: [usize; 10] = [0, 5, 10, 12, 17, 22, 24, 29, 35, 37];
// This profile assigns segment slots0/1 to CS/DS; the other slots retain their seed.
pub const CS: usize = 0;
pub const DS: usize = 1;
pub const LENGTHS: [usize; 9] = [4, 4, 8, 4, 12, 4, 4, 16, 4];
pub const BODIES: [[u8; 16]; 9] = [
    [
        0x5, 0x2, 0xa0, 0x52, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    ],
    [
        0x44, 0x8, 0x80, 0x52, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    ],
    [
        0x1f, 0x20, 0x3, 0xd5, 0xa4, 0xfc, 0x9f, 0x88, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    ],
    [
        0x4, 0x0, 0x80, 0x12, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    ],
    [
        0x14, 0x2, 0xa0, 0x52, 0x84, 0xfe, 0xdf, 0x88, 0x1f, 0x20, 0x3, 0xd5, 0x0, 0x0, 0x0, 0x0,
    ],
    [
        0xe5, 0x3, 0x4, 0xaa, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    ],
    [
        0x4, 0x0, 0x80, 0x12, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    ],
    [
        0x14, 0x2, 0xa0, 0x52, 0x94, 0xfe, 0xdf, 0x48, 0x1f, 0x20, 0x3, 0xd5, 0x84, 0x3e, 0x40,
        0xb3,
    ],
    [
        0xe7, 0x3, 0x4, 0xaa, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0, 0x0,
    ],
];
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Profile {
    pub cs: [u64; 4],
    pub ds: [u64; 4],
    pub a20: u64,
}
pub fn profile(id: u64) -> Option<Profile> {
    let mut p = Profile {
        cs: [8, 0x300000, 0xffff_ffff, 0xc09b],
        ds: [0x10, 0x200000, 0xffff_ffff, 0xc093],
        a20: 1,
    };
    match id {
        0 => {}
        1 => p.a20 = 0,
        2 => {
            p.ds[2] = 0xfffff;
            p.ds[3] = 0x4093;
        }
        3 | 4 | 5 => {
            p.cs[2] = match id {
                3 => 0x2010,
                4 => 0x2013,
                _ => 0x1fff,
            };
            p.cs[3] = 0x409b;
        }
        6 => p.ds[3] = 0xc091,
        7 => p.ds[1] = 0x400000,
        8 => p.ds[1] = 0x500000,
        9 => p.ds[1] = 0xfff00000,
        _ => return None,
    }
    Some(p)
}
pub fn admitted(g: &GuestState, id: u64, a20: u64) -> bool {
    profile(id).is_some_and(|p| {
        p.a20 == a20
            && g.control[0] == 0x10001
            && g.control[3] == 0x600
            && g.control[4] == 0
            && g.segments[CS] == p.cs
            && g.segments[DS] == p.ds
            && g.segments[2..]
                .iter()
                .all(|s| *s == [0x10, 0, 0xffff_ffff, 0xc093])
    })
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AddressError {
    GeneralProtection,
    Unsupported,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Address {
    pub offset: u64,
    pub linear: u64,
    pub gpa: u64,
}
pub fn address(
    segment: [u64; 4],
    a20: u64,
    offset: u64,
    bytes: u64,
    access: Access,
) -> Result<Address, AddressError> {
    if a20 > 1
        || bytes == 0
        || segment[1] > u32::MAX as u64
        || segment[1] & 4095 != 0
        || segment[2] > u32::MAX as u64
        || offset > u32::MAX as u64
    {
        return Err(AddressError::Unsupported);
    }
    let end = offset
        .checked_add(bytes - 1)
        .ok_or(AddressError::Unsupported)?;
    // The admitted descriptors are present, expand-up and privilege-checked.
    if end > segment[2] || (access == Access::Write && segment[3] & 2 == 0) {
        return Err(AddressError::GeneralProtection);
    }
    let linear = (segment[1] as u32).wrapping_add(offset as u32) as u64;
    let gpa = if a20 == 0 {
        linear & !(1 << 20)
    } else {
        linear
    };
    // These fixtures do not split a single access across physical/native pages.
    if gpa & 4095 != offset & 4095 || (gpa & 4095) + bytes > 4096 {
        return Err(AddressError::Unsupported);
    }
    Ok(Address {
        offset,
        linear,
        gpa,
    })
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route {
    Ram(usize),
    Device,
    Hole,
}
pub fn route(gpa: u64, bytes: u64) -> Option<Route> {
    let end = gpa.checked_add(bytes.checked_sub(1)?)?;
    for (base, offset) in [
        (0x300000, 0x70000),
        (0x200000, 0x71000),
        (0x302000, 0x72000),
        (0x202000, 0x73000),
        (0, 0x74000),
    ] {
        if gpa >= base && end < base + 4096 {
            return Some(Route::Ram(offset + (gpa - base) as usize));
        }
    }
    if gpa == 0x500000 && bytes <= 4 {
        Some(Route::Device)
    } else if gpa == 0x600000 && bytes <= 4 {
        Some(Route::Hole)
    } else {
        None
    }
}
pub fn source(step: usize) -> Option<&'static [u8]> {
    (step < 9).then(|| &crate::families::paging_off32::SOURCE[OFFSETS[step]..OFFSETS[step + 1]])
}
pub fn request(vm: u64, id: u64, step: usize) -> Option<Packet> {
    let p = profile(id)?;
    let source = source(step)?;
    let eip = EIP + OFFSETS[step] as u64;
    let addr = address(p.cs, p.a20, eip, source.len() as u64, Access::Fetch).ok()?;
    let mut bytes = [0; 8];
    bytes[..source.len()].copy_from_slice(source);
    Some([
        MISS,
        1,
        vm,
        0,
        1,
        1,
        id + 1,
        eip,
        32,
        u64::from_le_bytes(bytes),
        source.len() as u64,
        addr.linear,
        addr.gpa,
        step as u64 + 1,
        LENGTHS[step] as u64,
        step as u64,
    ])
}
pub fn reply(p: &Packet) -> Option<Packet> {
    let id = p[6].checked_sub(1)?;
    let step = usize::try_from(p[15]).ok()?;
    if p[2] == 0 || Some(*p) != request(p[2], id, step) {
        return None;
    }
    let mut out = *p;
    out[0] = DELIVER;
    out[9] = u64::from_le_bytes(BODIES[step][..8].try_into().unwrap());
    out[10] = u64::from_le_bytes(BODIES[step][8..].try_into().unwrap());
    Some(out)
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct State {
    pub profile: u64,
    pub a20: u64,
    pub transfers: u64,
    pub step: u64,
    pub entries: [u64; 9],
    pub requests: [Packet; 9],
    pub replies: [Packet; 9],
    pub fetch_linear: [u64; 9],
    pub fetch_gpa: [u64; 9],
    pub data_linear: [u64; 9],
    pub data_gpa: [u64; 9],
    pub prefixes: [NativeFrame; 9],
    pub guest_prefixes: [[u64; 4]; 9],
    pub fault_steps: [u64; 3],
    pub data_before_retry: [[u8; 16]; 3],
    pub service_count: u64,
    pub services: [[u64; 6]; 3],
    pub device_value: u64,
}
const _: () =
    assert!(STATE_OFFSET + core::mem::size_of::<State>() <= crate::concurrency::ROUND_OFFSET);
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn segment_limits_precede_base_and_a20_routing() {
        let p = profile(0).unwrap();
        assert_eq!(
            address(p.ds, 1, 0x100000, 4, Access::Write).unwrap(),
            Address {
                offset: 0x100000,
                linear: 0x300000,
                gpa: 0x300000
            }
        );
        assert_eq!(
            address(p.ds, 0, 0x100000, 4, Access::Write).unwrap().gpa,
            0x200000
        );
        assert_eq!(
            address(profile(9).unwrap().ds, 1, 0x100000, 4, Access::Write)
                .unwrap()
                .linear,
            0
        );
        for id in [2, 6] {
            assert_eq!(
                address(profile(id).unwrap().ds, 1, 0x100000, 4, Access::Write),
                Err(AddressError::GeneralProtection)
            );
        }
        for id in [3, 4] {
            assert_eq!(
                address(profile(id).unwrap().cs, 1, 0x2011, 5, Access::Fetch),
                Err(AddressError::GeneralProtection)
            );
        }
        let mut subpage = p.ds;
        subpage[1] += 1;
        assert_eq!(
            address(subpage, 1, 0x100000, 4, Access::Read),
            Err(AddressError::Unsupported)
        );
        assert_eq!(route(0x300fff, 2), None);
        assert_eq!(route(crate::PRIVATE, 4), None);
        assert_ne!(route(0x300000, 4), route(0x200000, 4));
        assert_ne!(route(0x302000, 4), route(0x202000, 4));
    }
    #[test]
    fn each_instruction_binds_source_context_and_both_addresses() {
        for id in 0..10 {
            for step in 0..9 {
                if let Some(p) = request(37, id, step) {
                    let good = reply(&p).unwrap();
                    for word in 0..16 {
                        let mut bad = p;
                        bad[word] ^= 1;
                        assert!(reply(&bad).is_none_or(|r| r != good));
                    }
                }
            }
        }
        assert!(request(37, 5, 0).is_none());
        assert!(reply(&request(0, 0, 0).unwrap()).is_none());
    }
}
