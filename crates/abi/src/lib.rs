#![no_std]

// Same-build I1/I2 records. Pointers are owned native addresses, never guest GPAs.
pub const VERSION: u32 = 3;
pub const PROFILE: u32 = 1;
pub const IPA: u64 = 0x4000_0000;
pub const PRIVATE: u64 = 0x0000_8000_4000_0000;
pub const RAM_SIZE: usize = 0x10_0000;
pub const SHARED_OFFSET: usize = 0x2_0000;
pub const CODE_OFFSET: usize = 0x3_0000;
pub const BANK_OFFSET: usize = 0x4_0000;
pub const STACK_TOP: u64 = PRIVATE + 0x5_4000;
pub const EXCEPTION_STACK_TOP: u64 = PRIVATE + 0x6_4000;
pub const DATA_OFFSET: usize = 0x7_0000;
pub const DATA_VA: u64 = 0x10_0000;
pub const CODE_VA: u64 = PRIVATE + CODE_OFFSET as u64;
pub const BANK_VA: u64 = PRIVATE + BANK_OFFSET as u64;
pub const FEX_BODY: [u8; 44] = [
    0x10, 0x1e, 0x31, 0x6e, 0xfb, 0x00, 0x05, 0xca, 0xfa, 0x00, 0x05, 0xab, 0xe7, 0x03, 0x1a, 0xaa,
    0xc4, 0xfc, 0xdf, 0xc8, 0x1f, 0x20, 0x03, 0xd5, 0xf4, 0x17, 0x9f, 0x9a, 0x85, 0x1e, 0x40, 0xb3,
    0xbf, 0x3b, 0x03, 0xd5, 0x70, 0x01, 0x80, 0x3d, 0x1f, 0x40, 0x00, 0xd5,
];
pub const RET: [u8; 4] = 0xd65f03c0u32.to_le_bytes();
pub const NOP: [u8; 4] = 0xd503201fu32.to_le_bytes();
pub const UDF: [u8; 4] = 0u32.to_le_bytes();
pub const CONTROL: u32 = 1;
pub const LIVE_LOAD: u32 = 2;
pub const FAULT: u32 = 3;
pub const LIVE_OR: u32 = 4;
pub const FEX_OR_BODY: [u8; 44] = {
    let mut body = FEX_BODY;
    body[2] = 0xb1;
    body[3] = 0x4e;
    body
};
pub mod concurrency;
pub mod maintenance;
pub mod paging;
pub mod pressure;
pub mod recovery;
pub mod shadow;
pub mod translation;
pub mod validation;
pub const SUCCESS: u64 = 1;
pub const REJECTED: u64 = 2;
pub const NATIVE_FAULT: u64 = 3;
pub const PANIC: u64 = 4;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C, align(16))]
pub struct NativeFrame {
    pub x: [u64; 31],
    pub sp: u64,
    pub q: [[u8; 16]; 32],
    pub nzcv: u64,
    pub fpcr: u64,
    pub fpsr: u64,
    pub reserved: u64,
}

impl NativeFrame {
    pub const ZERO: Self = Self {
        x: [0; 31],
        sp: 0,
        q: [[0; 16]; 32],
        nzcv: 0,
        fpcr: 0,
        fpsr: 0,
        reserved: 0,
    };
}

#[derive(Clone, Copy, Debug)]
#[repr(C)]
pub struct Object {
    pub version: u32,
    pub profile: u32,
    pub size: u32,
    pub kind: u32,
    pub code_address: u64,
    pub code_length: u64,
    pub entry_offset: u64,
    pub vcpu_id: u32,
    pub configured_vcpus: u32,
    pub reserved: u64,
}

impl Object {
    pub fn new(kind: u32) -> Self {
        Self {
            version: VERSION,
            profile: PROFILE,
            size: core::mem::size_of::<Self>() as u32,
            kind,
            code_address: CODE_VA,
            code_length: if kind == LIVE_LOAD || kind == LIVE_OR {
                48
            } else {
                8
            },
            entry_offset: 0,
            vcpu_id: 0,
            configured_vcpus: 1,
            reserved: 0,
        }
    }

    // The only admitted I1 profile has one participant and no arbitrary entrypoints.
    pub fn validate(&self, frame: &NativeFrame, code: &[u8]) -> Result<(), u64> {
        if self.version != VERSION
            || self.profile != PROFILE
            || self.size != core::mem::size_of::<Self>() as u32
            || self.reserved != 0
        {
            return Err(1);
        }
        if self.configured_vcpus != 1 || self.vcpu_id >= self.configured_vcpus {
            return Err(2);
        }
        if self.code_address != CODE_VA
            || self.entry_offset != 0
            || self.code_length != code.len() as u64
        {
            return Err(3);
        }
        let body: &[u8] = match self.kind {
            CONTROL => &NOP,
            LIVE_LOAD => &FEX_BODY,
            LIVE_OR => &FEX_OR_BODY,
            FAULT => &UDF,
            _ => return Err(4),
        };
        if code.len() != body.len() + 4 || code[..body.len()] != *body || code[body.len()..] != RET
        {
            return Err(5);
        }
        if frame.sp != STACK_TOP || frame.x[28] != BANK_VA || frame.reserved != 0 {
            return Err(6);
        }
        if (self.kind == LIVE_LOAD || self.kind == LIVE_OR)
            && (frame.x[6] != DATA_VA || frame.x[11] != DATA_VA + 128)
        {
            return Err(7);
        }
        Ok(())
    }
}

#[derive(Clone, Copy)]
#[repr(C, align(16))]
pub struct Shared {
    pub object: Object,
    pub initial: NativeFrame,
    pub captured: NativeFrame,
    pub status: u64,
    pub detail: u64,
    pub capture_pc: u64,
    pub esr: u64,
    pub far: u64,
    pub elr: u64,
    pub spsr: u64,
    pub current_el: u64,
    pub sctlr: u64,
    pub actlr: u64,
    pub tcr: u64,
    pub service_pc: u64,
    pub mode: u64,
    pub lifecycle: translation::Lifecycle,
}

const _: () = assert!(core::mem::size_of::<Shared>() <= 4096);
const _: () = assert!(core::mem::size_of::<NativeFrame>() == 800);

#[cfg(test)]
mod tests {
    use super::*;
    fn admitted() -> (Object, NativeFrame, [u8; 48]) {
        let mut f = NativeFrame::ZERO;
        f.sp = STACK_TOP;
        f.x[28] = BANK_VA;
        f.x[6] = DATA_VA;
        f.x[11] = DATA_VA + 128;
        let mut code = [0; 48];
        code[..44].copy_from_slice(&FEX_BODY);
        code[44..].copy_from_slice(&RET);
        (Object::new(LIVE_LOAD), f, code)
    }
    #[test]
    fn valid_and_truncated_code() {
        let (o, f, c) = admitted();
        assert_eq!(o.validate(&f, &c), Ok(()));
        for len in 0..c.len() {
            assert!(o.validate(&f, &c[..len]).is_err());
        }
    }
    #[test]
    fn reject_before_execution() {
        let (o, f, c) = admitted();
        let mut bad = o;
        bad.version += 1;
        assert!(bad.validate(&f, &c).is_err());
        bad = o;
        bad.profile += 1;
        assert!(bad.validate(&f, &c).is_err());
        bad = o;
        bad.size -= 1;
        assert!(bad.validate(&f, &c).is_err());
        bad = o;
        bad.code_address = DATA_VA;
        assert!(bad.validate(&f, &c).is_err());
        bad = o;
        bad.entry_offset = u64::MAX;
        assert!(bad.validate(&f, &c).is_err());
        bad = o;
        bad.configured_vcpus = 4;
        assert!(bad.validate(&f, &c).is_err());
        bad = o;
        bad.vcpu_id = 1;
        assert!(bad.validate(&f, &c).is_err());
        let mut wrong = f;
        wrong.x[6] = PRIVATE;
        assert!(o.validate(&wrong, &c).is_err());
        let mut changed = c;
        changed[0] ^= 1;
        assert!(o.validate(&f, &changed).is_err());
    }
}
