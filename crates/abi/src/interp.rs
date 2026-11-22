//! M2 Part 2: the boot-time interpreter's record and its decode request (the plan's sections 1-4). The host decodes
//! (iced-x86, `natemu::interp`); the resident executes through the core in [`exec`] and owns everything else: fetching,
//! caching records keyed by linear address and validating them by their bytes, the interpreter loop, events and the
//! resident operations ([`exec::Resident`]). Nothing here knows the interpreted modes: the core takes the few facts it
//! needs as an [`exec::Context`].
//!
//! # The decode request ([`REQUEST_DECODE`])
//!
//! A resident request like the others (`State::request`, `hvc #TRANSLATE_HVC`, `State::reply`), but its operands and
//! reply live in the vCPU's staging area ([`crate::execution::STAGING_VA`]), never in `State`. The host serves it with
//! iced alone: no fexc, no answer cache, no admission, and it reads no guest memory.
//!
//! 1. **The resident supplies the bytes.** It fetches up to [`MAX_FETCH`] bytes at CS:EIP itself, as an instruction
//!    fetch: each byte inside the CS limit (#GP(0) past it), CS executable, through the current translation (the
//!    identity with paging off, the 4-level walk in compatibility mode) and from fetchable memory only (guest RAM, the
//!    firmware region). It stops at the first byte it cannot fetch and remembers why (the #GP(0), the next page's #PF,
//!    or a typed stop). It writes a [`Decode`] at `STAGING_VA + DECODE_OFFSET`: `bits` (16 or 32: CS.D, the only mode
//!    fact decoding needs), `max` (1 ..= [`MAX_RUN`] records wanted) and `available` bytes. Fifteen bytes are always
//!    enough for one record.
//! 2. **The host replies** [`REPLY_DECODE`] with a [`Decoded`] at `STAGING_VA + DECODED_OFFSET`: `count` records for
//!    consecutive instructions from `bytes[0]`, each carrying its own bytes as the host decoded them (copied from the
//!    request, so the snapshot and the record agree; the resident never re-reads memory to fill a record). A straight
//!    run ends after a control transfer, a resident operation or a stop record ([`ends_run`]), at `max` records, or before
//!    an instruction not all of whose bytes lie within `available` (that one is not reported).
//! 3. **Truncated decode:** `count` 0 means the first instruction needs bytes past `available` (never with 15 or more).
//!    The resident then raises what stopped its fetch; nothing is cached. A fetch fault is never cached: a record is
//!    complete in its bytes, and a fault past them belongs to the instruction after it.
//! 4. **The resident checks every record** before it uses or caches one: [`Insn::well_formed`], its bytes equal to what
//!    it sent at that position and the lengths adding up within `available`; anything else is a bad reply (a halt).
//!    A cached record is keyed by the linear address of its first byte and the context it was decoded in; before each
//!    execution its `bytes[..length]` are compared with memory fetched as in step 1 (byte validation: the CS limit and
//!    rights are checked again), and a mismatch decodes again.
//!
//! A malformed request (`bits` not 16 or 32, `max` or `available` out of range) ends the run with the host's error.
use crate::execution::Cpu;

pub mod exec;

/// Operation numbers of [`Insn::op`]: our own, not iced's `Code`, so the resident needs no decoder tables. Grouped so
/// that a range test classifies: data operations below [`op::JCC`], control transfers from it, resident operations from
/// [`op::MOV_SREG`], stops from [`op::UD`].
pub mod op {
    /// The eight ALU operations in x86's order (ModRM.reg of 80-83, opcode bits 5:3 of 00-3D): destination `operands[0]`,
    /// source `operands[1]`. CMP writes no destination.
    pub const ADD: u8 = 0;
    pub const OR: u8 = 1;
    pub const ADC: u8 = 2;
    pub const SBB: u8 = 3;
    pub const AND: u8 = 4;
    pub const SUB: u8 = 5;
    pub const XOR: u8 = 6;
    pub const CMP: u8 = 7;
    /// TEST `operands[0]`, `operands[1]` (no write). INC/DEC `operands[0]`. SHL (SAL)/SHR/SAR `operands[0]` by
    /// `operands[1]` (CL or an immediate; 1 for D0/D1).
    pub const TEST: u8 = 8;
    pub const INC: u8 = 9;
    pub const DEC: u8 = 10;
    pub const SHL: u8 = 11;
    pub const SHR: u8 = 12;
    pub const SAR: u8 = 13;
    /// BT/BTS/BTR/BTC `operands[0]` (register or memory), bit `operands[1]` (register or imm8). With a memory operand and
    /// a register offset the offset is signed and selects the word (bit-string form).
    pub const BT: u8 = 14;
    pub const BTS: u8 = 15;
    pub const BTR: u8 = 16;
    pub const BTC: u8 = 17;
    /// XADD `operands[0]`, register `operands[1]`.
    pub const XADD: u8 = 18;
    /// MOV `operands[0]` := `operands[1]` (register, memory, immediate, or a segment register's selector); MOVZX the same,
    /// zero-extended from `operands[1].size`; LEA register `operands[0]` := the memory operand's offset.
    pub const MOV: u8 = 19;
    pub const MOVZX: u8 = 20;
    pub const LEA: u8 = 21;
    /// PUSH `operands[0]` (register or immediate), POP register `operands[0]`, PUSHF, POPF: `size`-byte slots on the stack.
    pub const PUSH: u8 = 22;
    pub const POP: u8 = 23;
    pub const PUSHF: u8 = 24;
    pub const POPF: u8 = 25;
    /// CLI, CLD, NOP (90, 66 90, 0F 1F /0) and PAUSE (F3 90): no operands.
    pub const CLI: u8 = 26;
    pub const CLD: u8 = 27;
    pub const NOP: u8 = 28;
    pub const PAUSE: u8 = 29;
    /// Control transfers, relative to the next instruction: Jcc (`cc` the condition, tttn), JMP and CALL near with the
    /// displacement in `operands[0]` (an immediate, sign-extended to 32 bits); LOOP (`cc` 0 LOOPNE, 1 LOOPE, 2 LOOP,
    /// 3 JCXZ/JECXZ, as E0-E3; the count register's width is `address`); RET near (`operands[0]` the imm16 released, or
    /// none); far JMP (ptr16:16/32: `operands[0]` the offset, `operands[1]` the selector; m16:16/32: `operands[0]` the
    /// memory operand) and far RET (`operands[0]` the imm16 released, or none). `size` is the operand size (2 or 4):
    /// it wraps the target and sizes the stack slots and the far pointer's offset.
    pub const JCC: u8 = 32;
    pub const JMP: u8 = 33;
    pub const LOOP: u8 = 34;
    pub const CALL: u8 = 35;
    pub const RET: u8 = 36;
    pub const JMP_FAR: u8 = 37;
    pub const RET_FAR: u8 = 38;
    /// MOV Sreg `operands[0]` := r/m16 `operands[1]` (the core executes it in real mode), LGDT/LIDT m (`size` 2: 24 bits of
    /// the base), MOV CR `operands[0]` := register `operands[1]` and the reverse, RDMSR, WRMSR, CPUID, HLT, WBINVD: the
    /// core returns them to the resident ([`super::exec::Resident`]) with their operands read and checked.
    pub const MOV_SREG: u8 = 40;
    pub const LGDT: u8 = 41;
    pub const LIDT: u8 = 42;
    pub const MOV_TO_CR: u8 = 43;
    pub const MOV_FROM_CR: u8 = 44;
    pub const RDMSR: u8 = 45;
    pub const WRMSR: u8 = 46;
    pub const CPUID: u8 = 47;
    pub const HLT: u8 = 48;
    pub const WBINVD: u8 = 49;
    /// Stops: an encoding x86 defines no instruction for (#UD; `bytes` holds every byte that decided it), one that needs a
    /// 16th byte (#GP(0)), and an instruction decoded but not in the first version (a typed stop naming its bytes).
    pub const UD: u8 = 60;
    pub const TOO_LONG: u8 = 61;
    pub const OTHER: u8 = 62;
}
/// [`Operand::kind`].
pub mod kind {
    pub const NONE: u8 = 0;
    /// A general register: `reg` 0..7 (EAX ECX EDX EBX ESP EBP ESI EDI; with `size` 1: AL CL DL BL AH CH DH BH).
    pub const REG: u8 = 1;
    /// The record's memory operand ([`super::Insn::base`] ..), `size` bytes (1, 2, 4; 6 for LGDT/LIDT and m16:32).
    pub const MEM: u8 = 2;
    /// An immediate (`value`, sign-extended where the encoding says so) or a branch displacement.
    pub const IMM: u8 = 3;
    /// A segment register (`reg`, [`crate::execution::SREG_ES`] ..), 2 bytes.
    pub const SREG: u8 = 4;
    /// A control register (`reg` 0..15), 4 bytes.
    pub const CR: u8 = 5;
}
/// [`Insn::base`] / [`Insn::index`] when absent.
pub const NO_REG: u8 = 0xff;

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Operand {
    /// [`kind::REG`] ..
    pub kind: u8,
    pub reg: u8,
    /// Bytes.
    pub size: u8,
    pub pad: u8,
    pub value: u32,
}
impl Operand {
    pub const NONE: Self = Self {
        kind: kind::NONE,
        reg: 0,
        size: 0,
        pad: 0,
        value: 0,
    };
}
/// One decoded instruction for the interpreter (plan section 3). Nothing in it depends on where it lies: branch targets
/// are displacements, and the CS limit and IP wrap are the executor's.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Insn {
    /// [`op::ADD`] ..
    pub op: u8,
    /// Bytes, 1..=15.
    pub length: u8,
    /// The operation's size in bytes: the destination's (1, 2, 4) for data operations, the operand size (2, 4) for
    /// stack, branch and table forms.
    pub size: u8,
    /// The address size (2, 4): the memory operand's offset wraps there, and LOOP counts CX or ECX.
    pub address: u8,
    /// The memory operand's segment register ([`crate::execution::SREG_ES`] ..): its override, else its default (SS for
    /// a BP or SP base, else DS).
    pub segment: u8,
    /// 1: a LOCK prefix (on a lockable form with a memory destination; elsewhere the record is [`op::UD`]).
    pub lock: u8,
    /// [`op::JCC`]'s condition (tttn, 0..15), [`op::LOOP`]'s form (0..3).
    pub cc: u8,
    pub pad: u8,
    pub operands: [Operand; 3],
    /// The memory operand: base and index ([`NO_REG`]: none; the 16-bit forms name BX, BP, SI and DI), the index's scale
    /// (a shift, 0..3) and the displacement (the offset is `base + (index << scale) + disp` wrapped at `address`).
    pub base: u8,
    pub index: u8,
    pub scale: u8,
    pub pad2: u8,
    pub disp: u32,
    /// The instruction's bytes as decoded (`bytes[..length]`; zero after them).
    pub bytes: [u8; 16],
}
impl Insn {
    pub const NONE: Self = Self {
        op: op::OTHER,
        length: 0,
        size: 0,
        address: 0,
        segment: 0,
        lock: 0,
        cc: 0,
        pad: 0,
        operands: [Operand::NONE; 3],
        base: NO_REG,
        index: NO_REG,
        scale: 0,
        pad2: 0,
        disp: 0,
        bytes: [0; 16],
    };
    /// The field ranges the executor relies on (the resident checks every reply): it indexes registers and segments only
    /// with values this admits.
    pub fn well_formed(&self) -> bool {
        let gpr = |r: u8| r < 8;
        let operand = |o: &Operand| match o.kind {
            kind::NONE => true,
            kind::REG => gpr(o.reg) && matches!(o.size, 1 | 2 | 4),
            kind::MEM => matches!(o.size, 1 | 2 | 4 | 6),
            kind::IMM => true,
            kind::SREG => o.reg < 6,
            kind::CR => o.reg < 16,
            _ => false,
        };
        let known = self.op <= op::PAUSE
            || (op::JCC..=op::RET_FAR).contains(&self.op)
            || (op::MOV_SREG..=op::WBINVD).contains(&self.op)
            || (op::UD..=op::OTHER).contains(&self.op);
        known
            && (1..=15).contains(&self.length)
            && matches!(self.size, 1 | 2 | 4)
            && matches!(self.address, 2 | 4)
            && self.segment < 6
            && self.lock <= 1
            && self.cc < 16
            && self.operands.iter().all(operand)
            && (gpr(self.base) || self.base == NO_REG)
            && (gpr(self.index) || self.index == NO_REG)
            && self.scale <= 3
    }
}
/// Whether a straight run of records ([`Decoded`]) ends after one of `op`: a control transfer, a resident operation or a
/// stop.
pub const fn ends_run(op: u8) -> bool {
    op >= op::JCC
}

/// The decode request (resident → host) and its reply, numbered apart from the other requests
/// (`crate::execution::REQUEST_*`).
pub const REQUEST_DECODE: u64 = 0x37;
pub const REPLY_DECODE: u64 = 0x37;
/// The most bytes one request carries, and the most records one reply holds.
pub const MAX_FETCH: usize = 240;
pub const MAX_RUN: usize = 16;
/// Where the request and the reply lie in the staging area.
pub const DECODE_OFFSET: usize = 0;
pub const DECODED_OFFSET: usize = 256;
/// The request, written by the resident ([`REQUEST_DECODE`]).
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Decode {
    /// The code segment's default size in bits: 16 or 32 (CS.D).
    pub bits: u32,
    /// Records wanted, 1..=[`MAX_RUN`].
    pub max: u32,
    /// Bytes fetched at CS:EIP, 0..=[`MAX_FETCH`].
    pub available: u32,
    pub pad: u32,
    pub bytes: [u8; MAX_FETCH],
}
/// The reply, written by the host ([`REPLY_DECODE`]).
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Decoded {
    /// Records filled; 0: the first instruction needs bytes past `available` (truncated).
    pub count: u32,
    pub pad: [u32; 3],
    pub records: [Insn; MAX_RUN],
}
const _: () = {
    use crate::execution as e;
    assert!(DECODE_OFFSET + core::mem::size_of::<Decode>() <= DECODED_OFFSET);
    assert!(DECODED_OFFSET + core::mem::size_of::<Decoded>() <= e::STAGING_BYTES);
    assert!(core::mem::size_of::<Insn>() == 56);
    let taken = [
        e::REQUEST_TRANSLATE,
        e::REQUEST_STEP,
        e::REQUEST_DEVICE,
        e::REQUEST_ENTROPY,
        e::REQUEST_PORT,
        e::REQUEST_PAGE,
        e::REQUEST_IRQ,
        e::REQUEST_DEVICE_POLL,
        e::REQUEST_POWER_POLL,
    ];
    let mut k = 0;
    while k < taken.len() {
        assert!(taken[k] != REQUEST_DECODE);
        k += 1;
    }
};

/// The 32-bit value of general register `r` (0..7) as an operand of `size` bytes: 1 names AL CL DL BL AH CH DH BH.
pub fn register(cpu: &Cpu, r: u8, size: u8) -> u32 {
    let r = r as usize & 7;
    match size {
        1 => (cpu.gpr[r & 3] >> (r & 4) * 2) as u8 as u32,
        2 => cpu.gpr[r] as u16 as u32,
        _ => cpu.gpr[r] as u32,
    }
}
/// Writes general register `r` as an operand of `size` bytes: 1 and 2 merge into the register, 4 zero-extends (as a
/// 32-bit write in 64-bit mode does; the upper halves are undefined after leaving 64-bit mode, natemu's fixed choice).
pub fn set_register(cpu: &mut Cpu, r: u8, size: u8, value: u32) {
    let r = r as usize & 7;
    let (i, shift, mask) = match size {
        1 => (r & 3, (r & 4) * 2, 0xff),
        2 => (r, 0, 0xffff),
        _ => (r, 0, u64::MAX),
    };
    cpu.gpr[i] = cpu.gpr[i] & !(mask << shift) | (u64::from(value) & mask) << shift;
}

#[cfg(test)]
mod tests;
