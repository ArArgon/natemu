//! Finite native litmus trials; synchronization occurs outside participant blocks.
use crate::{NativeFrame, families::validation::Packet};
use core::sync::atomic::AtomicU64;

pub const MODE: u64 = 10;
pub const OWNER_OFFSET: usize = 0x26000;
pub const RECORD_OFFSET: usize = 0x70000;
pub const COMMON_OFFSET: usize = 0x90000;
pub const RAM_OFFSET: usize = 0x94000;
pub const TABLE_OFFSET: usize = 0x98000;
pub const IMAGE_VA_OFFSET: usize = 0x100000;
pub const TRIALS: usize = 32;
pub const IMAGE_BYTES: usize = 8192;
pub const CODE_BYTES: usize = 64;
pub const MISS: u64 = 0x49345681;
pub const DELIVER: u64 = 0x49345682;
pub const RIP: u64 = 0x102000;
pub const DATA: u64 = 0x100000;
pub const RESULTS: u64 = 0x101000;

/// This finite profile admits only the reviewed LSE feature encoding.
pub fn admitted_lse(isar0: u64) -> bool {
    (isar0 >> 20) & 15 == 2
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    pub n: u64,
    pub profile: u64,
    pub rotation: u64,
    pub seed: u64,
}
impl Config {
    pub fn new(n: u64, profile: u64, rotation: u64, seed: u64) -> Option<Self> {
        if n == 0 || n > 4 || profile > 12 || rotation >= n || seed == 0 || seed > 0xffff {
            return None;
        }
        if profile < 7 && n != [2, 2, 2, 3, 4, 2, 2][profile as usize] {
            return None;
        }
        Some(Self {
            n,
            profile,
            rotation,
            seed,
        })
    }
    pub fn role(self, id: u64) -> u64 {
        (id + self.n - self.rotation) % self.n
    }
    pub fn variant(self, id: u64) -> usize {
        let role = self.role(id);
        match self.profile {
            0 => 0,
            1 => {
                if role == 0 {
                    1
                } else {
                    2
                }
            }
            2 => 3,
            3 => [4, 3, 2][role as usize],
            4 => {
                if role < 2 {
                    4
                } else {
                    2
                }
            }
            5 => 5,
            6 => 6,
            7..=9 => 7,
            10 => 8,
            11 => 9,
            12 => 10,
            _ => unreachable!(),
        }
    }
    pub fn pointers(self, id: u64) -> [u64; 3] {
        let role = self.role(id);
        let reverse = match self.profile {
            0 | 1 | 2 | 5 | 6 => role == 1,
            3 => role == 2,
            4 => role == 1 || role == 3,
            _ => false,
        };
        let (x, y) = if reverse {
            (DATA + 128, DATA)
        } else {
            (DATA, DATA + 128)
        };
        [
            x,
            if self.profile >= 7 {
                RESULTS + id * 128
            } else {
                y
            },
            DATA + 256 + role * 128,
        ]
    }
    pub fn token(self, id: u64, trial: u64) -> u64 {
        (self.seed << 16) | ((trial + 1) << 4) | (self.role(id) + 1)
    }
    pub fn context(self) -> u64 {
        (self.profile + 1) | (self.n << 8) | (self.rotation << 16) | (self.seed << 32)
    }
    pub fn fault(self, id: u64) -> u64 {
        if id == self.rotation && matches!(self.profile, 8 | 9) {
            self.profile - 7
        } else {
            0
        }
    }
}

pub fn request(vm: u64, c: Config, id: u64) -> Option<Packet> {
    if vm == 0 || Config::new(c.n, c.profile, c.rotation, c.seed) != Some(c) || id >= c.n {
        return None;
    }
    let variant = c.variant(id);
    let source = source(variant)?;
    let body = body(variant)?;
    let mut bytes = [0; 16];
    bytes[..source.len()].copy_from_slice(source);
    Some([
        MISS,
        1,
        vm,
        id,
        1,
        1,
        c.context(),
        RIP + id * 256,
        64,
        u64::from_le_bytes(bytes[..8].try_into().unwrap()),
        u64::from_le_bytes(bytes[8..].try_into().unwrap()),
        source.len() as u64,
        body.len() as u64,
        1,
        0,
        variant as u64,
    ])
}
pub fn decode(p: &Packet) -> Option<Config> {
    let c = Config::new(
        (p[6] >> 8) & 255,
        (p[6] & 255).checked_sub(1)?,
        (p[6] >> 16) & 255,
        p[6] >> 32,
    )?;
    (request(p[2], c, p[3]) == Some(*p)).then_some(c)
}
pub fn reply(p: &Packet) -> Option<Packet> {
    decode(p)?;
    let body = body(p[15] as usize)?;
    let mut bytes = [0; 32];
    bytes[..body.len()].copy_from_slice(body);
    let mut out = *p;
    out[0] = DELIVER;
    for i in 0..4 {
        out[9 + i] = u64::from_le_bytes(bytes[i * 8..i * 8 + 8].try_into().unwrap());
    }
    Some(out)
}

/// Preserve every emitted instruction; qualify local fence/atomic ordering separately.
pub fn image(variant: usize) -> Option<([u8; CODE_BYTES], usize)> {
    let original = body(variant)?;
    let mut image = [0; CODE_BYTES];
    let mut length = 0;
    let mut emit = |bytes: &[u8]| {
        image[length..length + bytes.len()].copy_from_slice(bytes);
        length += bytes.len();
    };
    for (index, word) in original.chunks_exact(4).enumerate() {
        let atomic = (variant == 6 && index == 3) || (matches!(variant, 7 | 11) && index == 0);
        if atomic {
            emit(&0xd5033fbfu32.to_le_bytes());
        } // DMB SY before RMW.
        emit(word);
        if atomic {
            emit(&0xd5033fbfu32.to_le_bytes());
        }
        if variant == 5 && index == 3 {
            emit(&0xd5033f9fu32.to_le_bytes());
            emit(&0xd5033fdfu32.to_le_bytes());
        }
    }
    emit(&crate::RET);
    Some((image, length))
}

#[repr(C, align(128))]
pub struct Gate {
    pub value: AtomicU64,
}
impl Gate {
    pub const fn new() -> Self {
        Self {
            value: AtomicU64::new(0),
        }
    }
}
#[repr(C)]
pub struct Common {
    pub n: u64,
    pub profile: u64,
    pub rotation: u64,
    pub seed: u64,
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
            profile: c.profile,
            rotation: c.rotation,
            seed: c.seed,
            compiled: Gate::new(),
            epoch: Gate::new(),
            ready: [const { Gate::new() }; 4],
            done: [const { Gate::new() }; 4],
            stopped: Gate::new(),
        }
    }
}
#[derive(Clone, Copy)]
#[repr(C)]
pub struct Fault {
    pub frame: NativeFrame,
    pub esr: u64,
    pub far: u64,
    pub pc: u64,
    pub spsr: u64,
    pub resume: u64,
}
impl Fault {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[derive(Clone, Copy)]
#[repr(C)]
pub struct Trial {
    pub epoch: u64,
    pub token: u64,
    pub completed: u64,
    pub faults: u64,
    pub captured: NativeFrame,
    pub fault: [Fault; 2],
    pub actlr: u64,
    pub fence_completed: u64,
}
impl Trial {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
#[repr(C)]
pub struct Owner {
    pub id: u64,
    pub variant: u64,
    pub trial: u64,
    pub completed: u64,
    pub expected: Packet,
    pub request: Packet,
    pub reply: Packet,
    pub initial: NativeFrame,
    pub current: NativeFrame,
    pub walks: u64,
    pub table_reads: u64,
    pub table_writes: u64,
    pub routes: [u64; 4],
    pub code: [u8; CODE_BYTES],
    pub code_length: u64,
    pub isar0: u64,
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
    fn lse_admission_rejects_unqualified_feature_encodings() {
        for field in 0..16 {
            assert_eq!(admitted_lse(field << 20), field == 2);
        }
    }
    #[test]
    fn roles_and_tokens_cannot_alias_another_trial() {
        for n in 1..=4 {
            for rotation in 0..n {
                let c = Config::new(n, 7, rotation, 37).unwrap();
                for trial in 0..TRIALS as u64 {
                    for id in 0..n {
                        assert!(c.token(id, trial) <= u32::MAX as u64);
                        for peer in 0..n {
                            assert_ne!(c.token(id, trial), c.token(peer, trial + 1));
                            if id != peer {
                                assert_ne!(c.token(id, trial), c.token(peer, trial));
                            }
                        }
                    }
                }
            }
        }
    }
    #[test]
    fn packets_bind_full_source_owner_and_trial_profile() {
        for profile in 0..=12 {
            for n in 1..=4 {
                for rotation in 0..n {
                    if let Some(c) = Config::new(n, profile, rotation, 1) {
                        for id in 0..n {
                            let p = request(37, c, id).unwrap();
                            let expected = reply(&p).unwrap();
                            for i in 0..16 {
                                let mut bad = p;
                                bad[i] ^= 1;
                                assert_ne!(reply(&bad), Some(expected));
                            }
                        }
                    }
                }
            }
        }
    }
    #[test]
    fn atomic_adaptation_preserves_original_body_and_exact_later_store() {
        let (code, len) = image(7).unwrap();
        assert_eq!(len, 24);
        assert_eq!(&code[4..8], &body(7).unwrap()[..4]);
        assert_eq!(&code[12..20], &body(7).unwrap()[4..12]);
        assert_eq!(&code[16..20], &0x889ffca4u32.to_le_bytes());
    }
}

pub fn source(variant: usize) -> Option<&'static [u8]> {
    match variant {
        0 => Some(&[0xc7, 0x03, 0x01, 0x00, 0x00, 0x00, 0x8b, 0x02]),
        1 => Some(&[
            0xc7, 0x03, 0x01, 0x00, 0x00, 0x00, 0xc7, 0x02, 0x01, 0x00, 0x00, 0x00,
        ]),
        2 => Some(&[0x8b, 0x03, 0x8b, 0x0a]),
        3 => Some(&[0x8b, 0x03, 0xc7, 0x02, 0x01, 0x00, 0x00, 0x00]),
        4 => Some(&[0xc7, 0x03, 0x01, 0x00, 0x00, 0x00]),
        5 => Some(&[
            0xc7, 0x03, 0x01, 0x00, 0x00, 0x00, 0x0f, 0xae, 0xf0, 0x8b, 0x02,
        ]),
        6 => Some(&[0xc7, 0x03, 0x01, 0x00, 0x00, 0x00, 0x87, 0x06, 0x8b, 0x02]),
        7 => Some(&[0x87, 0x03, 0x89, 0x02]),
        8 => Some(&[0x0f, 0xae, 0xe8]),
        9 => Some(&[0x0f, 0xae, 0xf8]),
        10 => Some(&[0x0f, 0xae, 0xf0]),
        11 => Some(&[0x87, 0x03]),
        _ => None,
    }
}

pub fn body(variant: usize) -> Option<&'static [u8]> {
    match variant {
        0 => Some(&[
            0x34, 0x00, 0x80, 0x52, 0x1f, 0x20, 0x03, 0xd5, 0xd4, 0xfc, 0x9f, 0x88, 0xa4, 0xfc,
            0xdf, 0x88, 0x1f, 0x20, 0x03, 0xd5,
        ]),
        1 => Some(&[
            0x34, 0x00, 0x80, 0x52, 0x1f, 0x20, 0x03, 0xd5, 0xd4, 0xfc, 0x9f, 0x88, 0x1f, 0x20,
            0x03, 0xd5, 0xb4, 0xfc, 0x9f, 0x88,
        ]),
        2 => Some(&[
            0xc4, 0xfc, 0xdf, 0x88, 0x1f, 0x20, 0x03, 0xd5, 0xa7, 0xfc, 0xdf, 0x88, 0x1f, 0x20,
            0x03, 0xd5,
        ]),
        3 => Some(&[
            0xc4, 0xfc, 0xdf, 0x88, 0x1f, 0x20, 0x03, 0xd5, 0x34, 0x00, 0x80, 0x52, 0x1f, 0x20,
            0x03, 0xd5, 0xb4, 0xfc, 0x9f, 0x88,
        ]),
        4 => Some(&[
            0x34, 0x00, 0x80, 0x52, 0x1f, 0x20, 0x03, 0xd5, 0xd4, 0xfc, 0x9f, 0x88,
        ]),
        5 => Some(&[
            0x34, 0x00, 0x80, 0x52, 0x1f, 0x20, 0x03, 0xd5, 0xd4, 0xfc, 0x9f, 0x88, 0xbf, 0x3f,
            0x03, 0xd5, 0xa4, 0xfc, 0xdf, 0x88, 0x1f, 0x20, 0x03, 0xd5,
        ]),
        6 => Some(&[
            0x34, 0x00, 0x80, 0x52, 0x1f, 0x20, 0x03, 0xd5, 0xd4, 0xfc, 0x9f, 0x88, 0x44, 0x81,
            0xe4, 0xb8, 0xa4, 0xfc, 0xdf, 0x88, 0x1f, 0x20, 0x03, 0xd5,
        ]),
        7 => Some(&[
            0xc4, 0x80, 0xe4, 0xb8, 0x1f, 0x20, 0x03, 0xd5, 0xa4, 0xfc, 0x9f, 0x88,
        ]),
        8 => Some(&[0xbf, 0x3d, 0x03, 0xd5]),
        9 => Some(&[0xbf, 0x3e, 0x03, 0xd5]),
        10 => Some(&[0xbf, 0x3f, 0x03, 0xd5]),
        11 => Some(&[0xc4, 0x80, 0xe4, 0xb8]),
        _ => None,
    }
}
