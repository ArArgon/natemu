//! First protected32 paging-off profile; native mappings still enforce ownership.
use crate::{paging::Access, recovery::GuestState, families::validation::Packet};
pub const CASE: u64 = 2;
pub const STATE_OFFSET: usize = 0x23000;
pub const MISS: u64 = 0x49345621;
pub const DELIVER: u64 = 0x49345622;
pub const CODE_BYTES: usize = 68;
pub const SOURCE: [u8; 37] = [
    0xba, 0x0, 0x0, 0x10, 0x0, 0xb8, 0x42, 0x0, 0x0, 0x0, 0x89, 0x2, 0xb8, 0xff, 0xff, 0xff, 0xff,
    0xa1, 0x0, 0x0, 0x10, 0x0, 0x89, 0xc2, 0xb8, 0xff, 0xff, 0xff, 0xff, 0x66, 0xa1, 0x0, 0x0,
    0x10, 0x0, 0x89, 0xc1,
];
pub const BODY: [u8; 64] = [
    0x5, 0x2, 0xa0, 0x52, 0x44, 0x8, 0x80, 0x52, 0xf4, 0x3, 0x5, 0xaa, 0x1f, 0x20, 0x3, 0xd5, 0xa4,
    0xfc, 0x9f, 0x88, 0x4, 0x0, 0x80, 0x12, 0x95, 0xfe, 0xdf, 0x88, 0x1f, 0x20, 0x3, 0xd5, 0xf6,
    0x3, 0x4, 0xaa, 0xe4, 0x3, 0x15, 0xaa, 0xe5, 0x3, 0x4, 0xaa, 0xe4, 0x3, 0x16, 0xaa, 0x94, 0xfe,
    0xdf, 0x48, 0x1f, 0x20, 0x3, 0xd5, 0x84, 0x3e, 0x40, 0xb3, 0xe7, 0x3, 0x4, 0xaa,
];

pub fn request(vm: u64, rip: u64, chunk: u64) -> Packet {
    let mut padded = [0u8; 40];
    padded[..SOURCE.len()].copy_from_slice(&SOURCE);
    let mut p = [MISS, 1, vm, 0, 1, 1, 1, rip, 32, 0, 0, 0, 0, 0, 1, chunk];
    for i in 0..5 {
        p[9 + i] = u64::from_le_bytes(padded[i * 8..i * 8 + 8].try_into().unwrap());
    }
    p
}
pub fn reply(p: &Packet) -> Option<Packet> {
    if p[2] == 0
        || p[15] >= 3
        || !matches!(p[7], crate::translation::SOURCE_VA | 0x102080)
        || *p != request(p[2], p[7], p[15])
    {
        return None;
    }
    let chunk = p[15] as usize;
    let count = (BODY.len() - chunk * 24).min(24);
    let mut bytes = [0; 24];
    bytes[..count].copy_from_slice(&BODY[chunk * 24..chunk * 24 + count]);
    let mut out = *p;
    out[0] = DELIVER;
    for i in 0..3 {
        out[9 + i] = u64::from_le_bytes(bytes[i * 8..i * 8 + 8].try_into().unwrap());
    }
    out[12] = chunk as u64;
    out[13] = count as u64;
    out[15] = BODY.len() as u64;
    Some(out)
}

pub fn admitted(guest: &GuestState, a20: u64) -> bool {
    a20 == 1
        && guest.control[0] == 0x10001
        && guest.control[3] == 0x600
        && guest.control[4] == 0
        && guest.segments[0] == [8, 0, 0xffff_ffff, 0xc09b]
        && guest.segments[1..]
            .iter()
            .all(|s| *s == [0x10, 0, 0xffff_ffff, 0xc093])
}

/// Flat segmentation and A20-on are admission requirements, never inferred from
/// absent page tables. Valid addresses outside this finite RAM profile are unsupported.
pub fn physical(
    guest: &GuestState,
    a20: u64,
    address: u64,
    bytes: u64,
    access: Access,
) -> Option<u64> {
    if !admitted(guest, a20) || bytes == 0 {
        return None;
    }
    let end = address.checked_add(bytes - 1)?;
    if end > u32::MAX as u64 {
        return None;
    }
    let base = if access == Access::Fetch {
        crate::translation::SOURCE_VA
    } else {
        crate::DATA_VA
    };
    (address >= base && end < base + 4096).then_some(address)
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct State {
    pub a20: u64,
    // 0 cold, 1 native read-only, 2 native writable.
    pub scenario: u64,
    pub transfers: u64,
    pub fetch_gpa: u64,
    pub repair_gpa: u64,
    pub data_before_retry: [u8; 16],
    pub requests: [Packet; 3],
    pub replies: [Packet; 3],
    pub prefix: GuestState,
}
const _: () = assert!(
    STATE_OFFSET
        >= crate::families::validation::STATE_OFFSET + core::mem::size_of::<crate::families::validation::State>()
);
const _: () =
    assert!(STATE_OFFSET + core::mem::size_of::<State>() <= crate::concurrency::ROUND_OFFSET);

#[cfg(test)]
mod tests {
    use super::*;
    fn guest() -> GuestState {
        let mut g = GuestState::seed(&crate::NativeFrame::ZERO);
        g.control[0] = 0x10001;
        g.control[3] = 0x600;
        g.control[4] = 0;
        g.segments[0][3] = 0xc09b;
        g
    }
    #[test]
    fn physical_routing_has_no_page_table_input() {
        let mut g = guest();
        g.control[2] = u64::MAX;
        for access in [Access::Read, Access::Write] {
            assert_eq!(
                physical(&g, 1, crate::DATA_VA, 4, access),
                Some(crate::DATA_VA)
            );
            for (address, bytes) in [
                (crate::PRIVATE, 4),
                (crate::DATA_VA + 4095, 2),
                (u64::MAX, 8),
                (crate::DATA_VA, 0),
            ] {
                assert_eq!(physical(&g, 1, address, bytes, access), None);
            }
        }
        assert_eq!(physical(&g, 1, 0x102080, 37, Access::Fetch), Some(0x102080));
        assert_eq!(physical(&g, 1, crate::DATA_VA, 4, Access::Fetch), None);
        assert_eq!(physical(&g, 0, crate::DATA_VA, 4, Access::Read), None);
        for field in [0, 3, 4] {
            let mut bad = g;
            bad.control[field] ^= 1 << 31;
            assert!(!admitted(&bad, 1));
        }
        let mut bad = g;
        bad.segments[1][1] = 4096;
        assert!(!admitted(&bad, 1));
        let mut bad = g;
        bad.segments[0][3] = 0xa09b;
        assert!(!admitted(&bad, 1));
    }
    #[test]
    fn all_source_bytes_and_chunks_are_bound() {
        for chunk in 0..3 {
            let request = request(37, 0x102000, chunk);
            let good = reply(&request).unwrap();
            for word in 0..16 {
                let mut bad = request;
                bad[word] ^= 1;
                assert!(reply(&bad).is_none_or(|r| r != good));
            }
            assert_eq!(good[12], chunk);
            assert_eq!(good[13], if chunk == 2 { 16 } else { 24 });
        }
        assert!(reply(&request(0, 0x102000, 0)).is_none());
        assert!(reply(&request(37, 0x102000, 3)).is_none());
    }
}
