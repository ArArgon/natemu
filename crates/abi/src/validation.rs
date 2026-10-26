//! First executable I4-V profile. Guest addresses never name native backing.
pub const MODE: u64 = 9;
pub const STATE_OFFSET: usize = 0x22000;
pub const SOURCE: [u8; 3] = [0x48, 0x8b, 0x03];
pub const BODY: [u8; 8] = [0xc4, 0xfc, 0xdf, 0xc8, 0x1f, 0x20, 0x03, 0xd5];
pub const RETURN: [u8; 4] = [0xc0, 0x03, 0x5f, 0xd6];
pub type Packet = [u64; 16];
pub const MISS: u64 = 0x49345601;
pub const DELIVER: u64 = 0x49345602;

/// One immutable compile request in the first N=1 profile. The identity fields
/// are explicit even though this slice cannot yet mutate their generations.
pub fn request(vm: u64, rip: u64) -> Packet {
    [MISS, 1, vm, 0, 1, 1, 1, rip, 64, 3, 0x038b48, 0, 0, 1, 0, 0]
}

pub fn reply(p: &Packet) -> Option<Packet> {
    if p[2] == 0 || !crate::paging::canonical_span(p[7], 3) || *p != request(p[2], p[7]) {
        return None;
    }
    let mut result = *p;
    result[0] = DELIVER;
    result[11] = BODY.len() as u64;
    result[12] = u64::from_le_bytes(BODY);
    Some(result)
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct State {
    pub guest_rip: u64,
    pub case: u64,
    pub completed: u64,
    pub operand: u64,
    pub width: u64,
    // 0: admitted, 1: native execution, 2: completed, 3: guest exception.
    pub stage: u64,
    pub live: u64,
    pub requests: u64,
    pub request: Packet,
    pub reply: Packet,
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn compile_identity_includes_full_rip_source_and_each_context_field() {
        let p = request(37, crate::shadow::HIGH_BASE + crate::translation::SOURCE_VA);
        let expected = reply(&p).unwrap();
        assert_eq!(expected[12].to_le_bytes(), BODY);
        for i in 0..16 {
            let mut bad = p;
            bad[i] ^= 1;
            // Another valid VM/RIP is a distinct request, never an equal reply.
            assert!(reply(&bad).is_none_or(|q| q != expected));
        }
        assert!(reply(&request(0, p[7])).is_none());
        assert!(reply(&request(37, crate::PRIVATE)).is_none());
    }
}
