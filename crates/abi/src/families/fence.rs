//! Finite live-result LFENCE adapter with generated flag/vector consumers.
pub use crate::families::ordering::{
    DATA, Gate, IMAGE_BYTES, IMAGE_VA_OFFSET, RAM_OFFSET, RESULTS, RIP, TABLE_OFFSET,
};
use crate::{NativeFrame, families::validation::Packet};
pub const MODE: u64 = 11;
pub const OWNER_OFFSET: usize = 0x26000;
pub const RECORD_OFFSET: usize = 0x70000;
pub const COMMON_OFFSET: usize = 0x90000;
pub const TRIALS: usize = 16;
pub const CODE_BYTES: usize = 128;
pub const BODY_BYTES: usize = 120;
pub const CHUNKS: usize = 4;
pub const MISS: u64 = 0x49345691;
pub const DELIVER: u64 = 0x49345692;
pub const FENCE_OFFSET: usize = 16;
pub const SINK_BRANCH: usize = 20;
pub const CONTINUATION: usize = 24;
pub const RETURN_BRANCH: usize = 124;
pub const MXCSR: u32 = 0x1f80;
pub const SOURCE: [u8; 25] = [
    15, 88, 193, 72, 1, 209, 15, 174, 232, 159, 15, 144, 192, 72, 137, 15, 102, 137, 71, 8, 243,
    15, 127, 71, 16,
];
pub const BODY: [u8; 120] = [
    16, 214, 49, 78, 251, 0, 5, 202, 250, 0, 5, 171, 231, 3, 26, 170, 191, 61, 3, 213, 244, 55,
    159, 154, 117, 3, 26, 202, 181, 18, 4, 83, 148, 18, 21, 170, 64, 19, 90, 74, 0, 8, 64, 74, 21,
    4, 64, 74, 181, 250, 127, 178, 148, 250, 245, 170, 21, 66, 59, 213, 181, 6, 98, 146, 148, 98,
    85, 170, 148, 2, 127, 178, 132, 30, 120, 179, 244, 119, 159, 154, 132, 30, 64, 179, 31, 32, 3,
    213, 103, 253, 159, 200, 116, 33, 0, 145, 31, 32, 3, 213, 132, 254, 159, 72, 20, 2, 128, 82,
    191, 59, 3, 213, 112, 233, 180, 60, 31, 64, 0, 213,
];

pub fn source() -> [u8; 29] {
    let mut s = [0; 29];
    s[..25].copy_from_slice(&SOURCE);
    s[25..].copy_from_slice(&[0x0f, 0xae, 0x5f, 0x20]);
    s
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    pub n: u64,
    pub case: u64,
}
impl Config {
    pub fn new(n: u64, case: u64) -> Option<Self> {
        (n > 0 && n <= 4 && case < 5).then_some(Self { n, case })
    }
    pub fn context(self) -> u64 {
        (self.n << 8) | (self.case + 1)
    }
    pub fn selected(self, id: u64, trial: u64) -> u64 {
        (self.case + id + trial) % 5
    }
}
pub fn operands(case: u64) -> Option<[u64; 2]> {
    Some(match case {
        0 => [u64::MAX, 1],
        1 => [0x7fffffffffffffff, 1],
        2 => [0xf, 1],
        3 => [1, 2],
        4 => [1, 2],
        _ => return None,
    })
}
pub fn mxcsr_before(case: u64) -> Option<u32> {
    (case < 5).then_some(
        MXCSR
            | match case {
                2 => 1,
                3 => 0x10,
                _ => 0,
            },
    )
}
pub fn vectors(case: u64) -> Option<[[u32; 4]; 2]> {
    if case > 4 {
        return None;
    }
    Some([
        [0x3f800000, 0x40000000, 0x40400000, 0x40800000],
        [
            if case == 4 { 0x34000000 } else { 0x33800000 },
            0x40000000,
            0x3f800000,
            0x3f800000,
        ],
    ])
}
pub fn request(vm: u64, c: Config, id: u64, chunk: u64) -> Option<Packet> {
    if vm == 0 || Config::new(c.n, c.case) != Some(c) || id >= c.n || chunk >= 4 {
        return None;
    }
    let mut bytes = [0; 32];
    bytes[..25].copy_from_slice(&SOURCE);
    let mut p = [
        MISS,
        1,
        vm,
        id,
        1,
        1,
        c.context(),
        RIP + id * 256,
        64,
        0,
        0,
        0,
        0,
        1,
        BODY_BYTES as u64,
        chunk,
    ];
    for i in 0..4 {
        p[9 + i] = u64::from_le_bytes(bytes[i * 8..i * 8 + 8].try_into().unwrap());
    }
    Some(p)
}
pub fn decode(p: &Packet) -> Option<Config> {
    let c = Config::new(p[6] >> 8, (p[6] & 255).checked_sub(1)?)?;
    (request(p[2], c, p[3], p[15]) == Some(*p)).then_some(c)
}
pub fn reply(p: &Packet) -> Option<Packet> {
    decode(p)?;
    let mut r = *p;
    r[0] = DELIVER;
    let at = p[15] as usize * 32;
    let mut bytes = [0; 32];
    let len = 32.min(BODY_BYTES - at);
    bytes[..len].copy_from_slice(&BODY[at..at + len]);
    for i in 0..4 {
        r[9 + i] = u64::from_le_bytes(bytes[i * 8..i * 8 + 8].try_into().unwrap());
    }
    Some(r)
}
fn branch(from: u64, to: u64) -> Option<[u8; 4]> {
    let delta = to as i128 - from as i128;
    if delta % 4 != 0 || !(-(1i128 << 27)..(1i128 << 27)).contains(&delta) {
        return None;
    }
    Some((0x14000000u32 | ((delta / 4) as u32 & 0x03ffffff)).to_le_bytes())
}
pub fn image(base: u64, sink: u64, returned: u64) -> Option<[u8; CODE_BYTES]> {
    let mut out = [0; CODE_BYTES];
    out[..20].copy_from_slice(&BODY[..20]);
    out[20..24].copy_from_slice(&branch(base.checked_add(20)?, sink)?);
    out[24..124].copy_from_slice(&BODY[20..]);
    out[124..].copy_from_slice(&branch(base.checked_add(124)?, returned)?);
    Some(out)
}
#[repr(C)]
pub struct Common {
    pub n: u64,
    pub case: u64,
    pub compiled: Gate,
    pub epoch: Gate,
    pub ready: [Gate; 4],
    pub done: [Gate; 4],
    pub stopped: Gate,
}
impl Common {
    pub fn new(c: Config) -> Self {
        Self {
            n: c.n,
            case: c.case,
            compiled: Gate::new(),
            epoch: Gate::new(),
            ready: [const { Gate::new() }; 4],
            done: [const { Gate::new() }; 4],
            stopped: Gate::new(),
        }
    }
}
#[repr(C)]
pub struct Trial {
    pub epoch: u64,
    pub case: u64,
    pub completed: u64,
    pub fence_completed: u64,
    pub sink_count: u64,
    pub initial: NativeFrame,
    pub sink: NativeFrame,
    pub captured: NativeFrame,
    pub sink_output: [u8; 40],
    pub mxcsr_before: u64,
    pub mxcsr: u64,
    pub actlr: u64,
    pub continuation: u64,
}
impl Trial {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
pub struct Owner {
    pub id: u64,
    pub trial: u64,
    pub completed: u64,
    pub expected: Packet,
    pub requests: [Packet; 4],
    pub replies: [Packet; 4],
    pub transfers: u64,
    pub initial: NativeFrame,
    pub current: NativeFrame,
    pub walks: u64,
    pub table_reads: u64,
    pub table_writes: u64,
    pub routes: [u64; 3],
    pub code: [u8; CODE_BYTES],
    pub code_length: u64,
    pub sink_pc: u64,
    pub return_pc: u64,
}
impl Owner {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
const _: () = assert!(OWNER_OFFSET + core::mem::size_of::<Owner>() <= 0x28000);
const _: () = assert!(RECORD_OFFSET + TRIALS * core::mem::size_of::<Trial>() <= COMMON_OFFSET);
const _: () = assert!(COMMON_OFFSET + core::mem::size_of::<Common>() <= RAM_OFFSET);
const _: () = assert!(IMAGE_VA_OFFSET + TRIALS * IMAGE_BYTES <= 0x200000);
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn replies_bind_every_stored_identity_word() {
        for id in 0..4 {
            for chunk in 0..4 {
                let p = request(37, Config::new(4, 2).unwrap(), id, chunk).unwrap();
                let expected = reply(&p).unwrap();
                for i in 0..16 {
                    let mut bad = p;
                    bad[i] ^= 1;
                    assert_ne!(reply(&bad), Some(expected));
                }
                for i in 0..16 {
                    let mut bad = expected;
                    bad[i] ^= 1;
                    assert_ne!(Some(bad), reply(&p));
                }
            }
        }
    }
    #[test]
    fn image_preserves_emission_and_direct_boundaries() {
        let base = 0x100000;
        let out = image(base, base + 0x1000, base + 0x2000).unwrap();
        assert_eq!(&out[..20], &BODY[..20]);
        assert_eq!(&out[24..124], &BODY[20..]);
        assert_eq!(
            u32::from_le_bytes(out[20..24].try_into().unwrap()),
            0x140003fb
        );
        assert_eq!(
            u32::from_le_bytes(out[124..].try_into().unwrap()),
            0x140007e1
        );
        assert!(image(base, base + 1, base + 0x2000).is_none());
        assert!(image(base, base + (1 << 27) + 20, base + 0x2000).is_none());
    }
    #[test]
    fn cases_cover_distinct_af_pf_and_fp_results() {
        assert_eq!(operands(2), Some([15, 1]));
        assert_eq!(operands(3), Some([1, 2]));
        assert_ne!(vectors(3), vectors(4));
        for case in 0..5 {
            let c = Config::new(4, case).unwrap();
            for id in 0..4 {
                let mut seen = 0;
                for trial in 0..16 {
                    seen |= 1 << c.selected(id, trial);
                }
                assert_eq!(seen, 31);
            }
        }
    }
}
