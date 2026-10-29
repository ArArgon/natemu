#![no_std]

// The resident's window layout and its record shared with the host. Pointers are owned native addresses, never guest
// GPAs.
pub const IPA: u64 = 0x4000_0000;
pub const PRIVATE: u64 = 0x0000_8000_4000_0000;
pub const RAM_SIZE: usize = 0x10_0000;
pub const SHARED_OFFSET: usize = 0x2_0000;
pub const STACK_TOP: u64 = PRIVATE + 0x5_4000;
pub const EXCEPTION_STACK_TOP: u64 = PRIVATE + 0x6_4000;
pub mod guest_memory;
pub mod paging;
pub mod resident;
pub mod shadow;
pub mod code_object;
pub mod execution;
pub mod cpu;
pub mod cpu_profile;
pub mod x87;
pub const SUCCESS: u64 = 1;
pub const REJECTED: u64 = 2;
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

#[derive(Clone, Copy)]
#[repr(C, align(16))]
pub struct Shared {
    pub initial: NativeFrame,
    pub captured: NativeFrame,
    pub status: u64,
    pub detail: u64,
    pub current_el: u64,
    pub sctlr: u64,
    pub actlr: u64,
    pub tcr: u64,
    pub mode: u64,
}

const _: () = assert!(core::mem::size_of::<Shared>() <= 4096);
const _: () = assert!(core::mem::size_of::<NativeFrame>() == 800);

