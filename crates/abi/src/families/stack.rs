//! Finite I4-V stack program, with a guard at each guest-instruction boundary.
use crate::{
    NativeFrame,
    families::validation::{Packet, RETURN},
};

pub const CASE: u64 = 1;
pub const STATE_OFFSET: usize = 0x23000;
pub const SLOT_BYTES: usize = 64;
pub const CODE_BYTES: usize = 5 * SLOT_BYTES;
pub const SOURCE: [u8; 20] = [
    0x68, 0xbb, 0xbc, 0xbd, 0xbe, 0x66, 0x68, 0x52, 0x51, 0x66, 0x68, 0x62, 0x61, 0x48, 0x8b, 0x42,
    0xf8, 0x8b, 0x5a, 0xf4,
];
pub const BODY: [u8; 52] = [
    0x94, 0x68, 0x88, 0x92, 0xb4, 0xd7, 0xb7, 0xf2, 0x14, 0x8d, 0x1f, 0xf8, 0x54, 0x2a, 0x8a, 0x52,
    0x14, 0xed, 0x1f, 0x78, 0x54, 0x2c, 0x8c, 0x52, 0x14, 0xed, 0x1f, 0x78, 0xb4, 0x20, 0x00, 0xd1,
    0x84, 0xfe, 0xdf, 0xc8, 0x1f, 0x20, 0x03, 0xd5, 0xb4, 0x30, 0x00, 0xd1, 0x86, 0xfe, 0xdf, 0x88,
    0x1f, 0x20, 0x03, 0xd5,
];
pub const GUEST_OFFSETS: [u64; 6] = [0, 5, 9, 13, 17, 20];
pub const NATIVE_OFFSETS: [usize; 6] = [0, 12, 20, 28, 40, 52];
pub const SITES: [u64; 5] = [8, 4, 4, 4, 4];
pub const WIDTHS: [u64; 5] = [8, 2, 2, 8, 4];
pub const MISS: u64 = 0x49345611;
pub const DELIVER: u64 = 0x49345612;

pub fn source(variant: u64) -> [u8; 20] {
    assert!(variant <= 1);
    let mut bytes = SOURCE;
    bytes[7] += variant as u8;
    bytes
}
pub fn body(variant: u64) -> [u8; 52] {
    assert!(variant <= 1);
    let mut bytes = BODY;
    bytes[12] += (variant * 32) as u8;
    bytes
}

pub fn request(vm: u64, rip: u64, chunk: u64) -> Packet {
    request_context(vm, 0, 1, 1, 1, 1, rip, 0, chunk)
}

pub fn request_context(
    vm: u64,
    id: u64,
    incarnation: u64,
    source_generation: u64,
    context_generation: u64,
    serial: u64,
    rip: u64,
    variant: u64,
    chunk: u64,
) -> Packet {
    let mut source = [0; 24];
    source[..SOURCE.len()].copy_from_slice(&self::source(variant));
    [
        MISS,
        1,
        vm,
        id,
        incarnation,
        source_generation,
        context_generation,
        rip,
        64,
        SOURCE.len() as u64,
        u64::from_le_bytes(source[..8].try_into().unwrap()),
        u64::from_le_bytes(source[8..16].try_into().unwrap()),
        u64::from_le_bytes(source[16..24].try_into().unwrap()),
        serial,
        BODY.len() as u64,
        chunk,
    ]
}

pub fn reply(p: &Packet) -> Option<Packet> {
    if *p != request(p[2], p[7], p[15]) {
        return None;
    }
    reply_context(p)
}

pub fn reply_context(p: &Packet) -> Option<Packet> {
    let chunk = p[15];
    let variant = match p[10] {
        x if x == request(p[2], p[7], chunk)[10] => 0,
        x if x == request_context(p[2], p[3], p[4], p[5], p[6], p[13], p[7], 1, chunk)[10] => 1,
        _ => return None,
    };
    if p[2] == 0
        || p[3] >= 4
        || p[4] == 0
        || p[5] == 0
        || p[6] == 0
        || p[13] == 0
        || chunk >= 3
        || !crate::paging::canonical_span(p[7], SOURCE.len() as u64)
        || *p != request_context(p[2], p[3], p[4], p[5], p[6], p[13], p[7], variant, chunk)
    {
        return None;
    }
    let offset = chunk as usize * 24;
    let count = (BODY.len() - offset).min(24);
    let mut data = [0; 24];
    data[..count].copy_from_slice(&body(variant)[offset..offset + count]);
    let mut result = *p;
    result[0] = DELIVER;
    result[9] = BODY.len() as u64;
    for i in 0..3 {
        result[10 + i] = u64::from_le_bytes(data[i * 8..i * 8 + 8].try_into().unwrap());
    }
    result[14] = chunk;
    result[15] = count as u64;
    Some(result)
}

/// Preserve every generated instruction. RET gates only occur between guest
/// instructions, so due-address guards cannot discard an earlier valid effect.
pub fn image(body: &[u8; 52]) -> [u8; CODE_BYTES] {
    let mut code = [0; CODE_BYTES];
    for i in 0..5 {
        let start = i * SLOT_BYTES;
        let count = NATIVE_OFFSETS[i + 1] - NATIVE_OFFSETS[i];
        code[start..start + count].copy_from_slice(&body[NATIVE_OFFSETS[i]..NATIVE_OFFSETS[i + 1]]);
        code[start + count..start + count + 4].copy_from_slice(&RETURN);
    }
    code
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct State {
    pub transfers: u64,
    pub entries: [u64; 5],
    pub completions: [u64; 5],
    pub requests: [Packet; 3],
    pub replies: [Packet; 3],
    pub prefixes: [NativeFrame; 5],
}
impl State {
    pub const ZERO: Self = Self {
        transfers: 0,
        entries: [0; 5],
        completions: [0; 5],
        requests: [[0; 16]; 3],
        replies: [[0; 16]; 3],
        prefixes: [NativeFrame::ZERO; 5],
    };
}
const _: () = assert!(core::mem::size_of::<State>() <= 0x2000);

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn chunk_identity_and_padding_prevent_mixed_or_reordered_publication() {
        let mut body = [0; 52];
        for chunk in 0..3 {
            let p = request(
                39,
                crate::shadow::HIGH_BASE + crate::translation::SOURCE_VA,
                chunk,
            );
            let q = reply(&p).unwrap();
            let mut data = [0; 24];
            for i in 0..3 {
                data[i * 8..i * 8 + 8].copy_from_slice(&q[10 + i].to_le_bytes());
            }
            let start = chunk as usize * 24;
            let count = q[15] as usize;
            body[start..start + count].copy_from_slice(&data[..count]);
            assert!(data[count..].iter().all(|&b| b == 0));
            for word in 0..16 {
                let mut bad = p;
                bad[word] ^= 1;
                assert!(reply(&bad).is_none_or(|r| r != q));
            }
        }
        assert_eq!(body, BODY);
        assert!(reply(&request(39, crate::translation::SOURCE_VA, 3)).is_none());
    }
    #[test]
    fn every_original_instruction_is_preserved_between_guards() {
        let code = image(&BODY);
        let mut recovered = [0; 52];
        for i in 0..5 {
            let count = NATIVE_OFFSETS[i + 1] - NATIVE_OFFSETS[i];
            recovered[NATIVE_OFFSETS[i]..NATIVE_OFFSETS[i + 1]]
                .copy_from_slice(&code[i * SLOT_BYTES..i * SLOT_BYTES + count]);
            assert_eq!(
                code[i * SLOT_BYTES + count..i * SLOT_BYTES + count + 4],
                RETURN
            );
            assert!(
                code[i * SLOT_BYTES + count + 4..(i + 1) * SLOT_BYTES]
                    .iter()
                    .all(|&b| b == 0)
            );
        }
        assert_eq!(recovered, BODY);
    }
}
