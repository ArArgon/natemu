use crate::{NativeFrame, PRIVATE, paging::Control};
pub const MODE: u64 = 3;
pub const STATE_OFFSET: usize = 0x21000;
pub const STATE_VA: u64 = PRIVATE + STATE_OFFSET as u64;
pub const GUEST_OFFSET: usize = 0x41000;
pub const GUEST_VA: u64 = PRIVATE + GUEST_OFFSET as u64;
pub const TABLE_OFFSET: usize = 0x80000;
pub const TABLE_GPA: u64 = 0x1000;
pub const SOURCE_OFFSET: usize = 0x72000;
pub const MAX_FAULTS: usize = 4;
pub const PAGE_FAULT: u64 = 18;

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct GuestState {
    pub gpr: [u64; 16],
    pub rip: u64,
    pub rflags: u64,
    pub ymm: [[u8; 32]; 16],
    pub mxcsr: u32,
    pub x87_control: u16,
    pub x87_status: u16,
    pub x87_tag: u16,
    pub x87_opcode: u16,
    pub reserved: u32,
    pub x87_ip: u64,
    pub x87_dp: u64,
    pub x87_regs: [[u8; 16]; 8],
    // CR0,CR2,CR3,CR4,EFER,XCR0,DR6,DR7; segment selector/base/limit/attributes.
    pub control: [u64; 8],
    pub segments: [[u64; 4]; 8],
}
impl GuestState {
    pub fn seed(frame: &NativeFrame) -> Self {
        let mut gpr = [0; 16];
        for (i, x) in gpr.iter_mut().enumerate() {
            *x = 0xbadc_0000_0000_0000 + i as u64;
        }
        let mut ymm = [[0; 32]; 16];
        for (i, q) in ymm.iter_mut().enumerate() {
            for (j, x) in q.iter_mut().enumerate() {
                *x = (i * 19 + j * 7 + 3) as u8;
            }
        }
        let mut x87_regs = [[0; 16]; 8];
        for (i, r) in x87_regs.iter_mut().enumerate() {
            for (j, b) in r[..10].iter_mut().enumerate() {
                *b = (i * 11 + j + 1) as u8;
            }
        }
        let mut s = Self {
            gpr,
            rip: crate::translation::SOURCE_VA,
            rflags: 0x202,
            ymm,
            mxcsr: 0x1f80,
            x87_control: 0x37f,
            x87_status: 0,
            x87_tag: 0xffff,
            x87_opcode: 0,
            reserved: 0,
            x87_ip: 0,
            x87_dp: 0,
            x87_regs,
            control: [
                0x80010001, 0xdead1000, 0x1000, 0x620, 0xd00, 3, 0xffff0ff0, 0x400,
            ],
            segments: [[0x10, 0, 0xffff_ffff, 0xc093]; 8],
        };
        s.segments[0] = [8, 0, 0xffff_ffff, 0xa09b];
        s.copy_live(frame);
        s
    }
    pub fn copy_live(&mut self, frame: &NativeFrame) {
        for (g, n) in [(0, 4), (1, 7), (2, 5), (3, 6), (7, 11)] {
            self.gpr[g] = frame.x[n];
        }
        self.ymm[0][..16].copy_from_slice(&frame.q[16]);
        self.ymm[1][..16].copy_from_slice(&frame.q[17]);
    }
    pub fn materialize(&mut self, frame: &NativeFrame, guest_offset: u64, carry_inverted: bool) {
        self.copy_live(frame);
        let n = frame.nzcv;
        let carry = (n >> 29) & 1;
        let sum = frame.x[26];
        let flags = (carry ^ u64::from(carry_inverted))
            | u64::from((sum as u8).count_ones() % 2 == 0) << 2
            | ((frame.x[27] ^ sum) & 16)
            | ((n >> 30) & 1) << 6
            | ((n >> 31) & 1) << 7
            | ((n >> 28) & 1) << 11;
        self.rflags = (self.rflags & !0x8d5) | flags;
        self.rip = crate::translation::SOURCE_VA + guest_offset;
    }
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct FaultTrace {
    pub frame: NativeFrame,
    pub esr: u64,
    pub far: u64,
    pub elr: u64,
    pub spsr: u64,
    pub outcome: u64,
    pub error: u64,
}
impl FaultTrace {
    pub const ZERO: Self = Self {
        frame: NativeFrame::ZERO,
        esr: 0,
        far: 0,
        elr: 0,
        spsr: 0,
        outcome: 0,
        error: 0,
    };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Recovery {
    pub control: Control,
    pub scenario: u64,
    pub native_fpcr: u64,
    pub native_fpsr: u64,
    pub faults: u64,
    pub walks: u64,
    pub retries: u64,
    pub vector: u64,
    pub error: u64,
    pub linear: u64,
    pub rip: u64,
    pub trace: [FaultTrace; MAX_FAULTS],
}
impl Recovery {
    pub fn new(scenario: u64) -> Self {
        Self {
            control: Control {
                cr3: TABLE_GPA,
                cpl: 0,
                wp: 1,
                nxe: 1,
                smep: 0,
            },
            scenario,
            native_fpcr: 0,
            native_fpsr: 0,
            faults: 0,
            walks: 0,
            retries: 0,
            vector: 0,
            error: 0,
            linear: 0,
            rip: 0,
            trace: [FaultTrace::ZERO; MAX_FAULTS],
        }
    }
}
const _: () = assert!(core::mem::size_of::<Recovery>() <= 4096);
const _: () = assert!(core::mem::size_of::<GuestState>() <= 4096);

pub const HELPER_MODE: u64 = 6;
pub const HELPER_VA: u64 = PRIVATE + 0x25000;
#[repr(C)]
#[derive(Clone, Copy)]
pub struct HelperState {
    pub calls: u64,
    pub outputs: u64,
    pub before: NativeFrame,
    pub staged: NativeFrame,
    pub poisoned: NativeFrame,
}
impl HelperState {
    pub const ZERO: Self = Self {
        calls: 0,
        outputs: 0,
        before: NativeFrame::ZERO,
        staged: NativeFrame::ZERO,
        poisoned: NativeFrame::ZERO,
    };
}
const _: () = assert!(core::mem::size_of::<HelperState>() <= 4096);

pub const PARTIAL_MODE: u64 = 7;
pub const PARTIAL_COMMAND: u64 = 20;
pub const PARTIAL_DONE: u64 = 21;
pub const PARTIAL_VA: u64 = PRIVATE + 0x26000;
pub const DEVICE_VA: u64 = 0x200000;
#[repr(C)]
#[derive(Clone, Copy)]
pub struct PartialState {
    pub reads: u64,
    pub writes: u64,
    pub faults: u64,
    pub retries: u64,
    pub phase: u64,
    pub value: u64,
    pub read_values: [u64; 2],
    pub prefix: GuestState,
    pub store_pc: u64,
    pub at_fault_counts: [u64; 4],
    pub before: GuestState,
    pub trace: FaultTrace,
}
const _: () = assert!(core::mem::size_of::<PartialState>() <= 4096);
