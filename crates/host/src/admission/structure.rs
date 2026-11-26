//! Structural verification of a slice-1 code region, in two parts. Generated code runs at EL0, where the hardware
//! protects EL1 resident pages (system instructions trap, private pages are EL1-only or execute-only). The selected
//! EL0 FEX-frame alias remains guest-accessible, a documented deviation. Entry into the resident is through `svc`,
//! the check stubs, aborts and interrupts, and the resident
//! classifies each by the block's records. One linear pass decodes every word with a small table of the A64 classes
//! fexc emits.
//!
//! **Object validation (always):** what the resident relies on to place, link, stop and recover the block.
//! - Words: a word the decoder does not know is admitted only if it is no branch or exception-generating word and
//!   names neither x25 nor x28 in any register field ([`screened`]); x28 (FEX's guest-state base, the guest-state page)
//!   is never written and addresses only FEX's guest-state fields: loads of `frame::GUEST_STATE`, stores only to
//!   `frame::GUEST_STATE_WRITABLE` (DF), and in a CPL-0 block to IF's byte (perf chunk 15: its CLI and STI, which the code
//!   shape checks) and to the GS base and IA32_KERNEL_GS_BASE's field, which it may also load (perf chunk 17: its SWAPGS,
//!   likewise), and in a CPL-0 block it may load CR2's field (perf chunk 26: its MOV r64, CR2) and the GDT base and limit
//!   fields (perf P2: its VERW); x25 is written only by `mrs x25,
//!   nzcv` (the pre-NZCV save the resident reads at `nzcv=saved` sites); the only `svc`s are `#LINK`, `#DIV_U`,
//!   `#DIV_S`, `#X87` and `#STRING`; direct branches go forward to a word of the code region, but the one back edge of a
//!   bulk REP STOS loop (perf chunk 26, [`Verifier::bulk_loop`]: its exact words, whose count is at most 256) and a probing
//!   split stub's return to its store (M2 task 4, [`Verifier::probe_return`]: to an instruction at or before the stub's
//!   access, from a stub that nothing after that instruction reaches, so it closes no loop); no word falls
//!   through into a literal slot, a check stub or past the region. So every path ends at an exit the resident knows (a link
//!   point or the dispatch branch, which a precise stop patches) or at a check stub.
//! - Sites: each access site lies inside its guest instruction and its word is a load, store, swap or LOCK atomic
//!   (`ld{add,clr,eor,set}al`, `casal`, `caspal`) exactly as recorded
//!   (the resident computes the faulting address and completes split stores from the record).
//! - Exits: a literal exit is a link point, exactly a guest-RIP move into x0 then `svc #LINK`, its value the block end
//!   or the decoded target of its branch, and no branch enters it after its first word (the linker rewrites that word
//!   to `b` the target's entry; a precise stop patches it to the resident's stop `brk`); `svc #LINK` occurs nowhere
//!   else. A computed exit (at most one) is exactly `mov x0, xR; b .` at a dispatch relocation (the resident relocates
//!   the `b` to its dispatcher, and a stop patches it). Admission returns the link points and the dispatch branch.
//! - Guest-RIP moves: each decodes to its relocation's value, the instruction end plus a displacement in its bytes.
//! - Division: each `svc #DIV_U` / `#DIV_S` lies in a 64-bit div / idiv.
//! - Helpers (I6b): each `svc #X87` lies in an x87 transcendental, remainder, scale or extract instruction and each
//!   `svc #STRING` in a PCMPxSTRx, right after a `movz w0` of that instruction's function code or control word (in the same
//!   instruction, no branch to the `svc`).
//!
//! **Code shape (debug, `Code::shape`; off by default, on in the test suites):** that fexc emitted the x86 checks
//! exactly and nothing else, a producer-correctness check whose violation stays confined to the vCPU's guest.
//! - Words: only the decoded classes (no system, SIMD, `ret`, `bl`, `br` or `blr` word; `brk` only at stubs); sp is
//!   neither written nor accessed; no literal loads; every load and store is an access site; a swap or fetch-and-op
//!   atomic keeps its result (with XZR there the architecture drops the acquire).
//! - Split: every store wider than one byte but a bulk REP STOS loop's (which stays inside its page by its words) has `add x0, xA, #w-1; eor x0, x0, xA; tbnz w0, #12, S` on exactly its
//!   address (xA its base, or x1 computed as exactly its address: `add|sub x1, base, #imm`, `add x1, base, index{, ext
//!   #s}`, or a mov/movk constant into x1 and `add x1, base, x1`), only x0/x1 written between it and the store
//!   (guest-RIP-addressed: `b S` iff it crosses a page, nothing entering between the move and the access).
//! - Branch target: the computed exit's RIP is checked by `eor x0, xG, xG, lsl #16; tbnz x0, #63, G` in its
//!   instruction (G a branch-target stub, xG's value reaching xR through moves; x86 raises #GP at the branch).
//! - Division: each div/idiv (from its x86 opcode: width, signedness) has exactly its per-width #DE checks, in their
//!   exact form, before FEX's divide, reading that divide's own divisor (and, at 8 bits, dividend) registers; the
//!   128/64 division is followed by `cbnz x2, D`.
//! - Floating-point exceptions (I5c): a SIMD stub is reached by exactly `mrs x0, fpsr; movz w1, #M; and w1, w0, w1; cbnz w1`
//!   followed by the flags' move into the SIMD flag byte and `msr fpsr, xzr`; an x87 stub by the pending byte with the
//!   operands' bits (`ldrb w0; sub w1, wF, #1; and w0, w0, w1; orr w0, w0, wR, lsl #8..11; movz w1, #M; and w1, w0, w1;
//!   cbnz w1`); a pending stub by the FSW flags' test at its instruction's first word. (Which instructions need one is the
//!   producer's table, as for alignment.)
//! - Every stub is reached by exactly one branch, which is one of the checks above, and no branch enters a check's
//!   window (from the check's first word to the access, exit or divide) from before it.
//!
//! There is no address guard (E3): non-canonical accesses outside the selected frame alias fault at their site,
//! where the resident raises #GP/#SS from the record. Randomization does not isolate the selected alias.
//! x30 is an ordinary allocator temporary: no `bl` is admitted.
use std::ops::Range;

use natemu_abi::code_object::{self as object, PcEntry, Reloc, Site};
use natemu_abi::execution::{
    DISPATCH_PLACEHOLDER, ENTRY_OFFSET, LinkPoint, PRE_NZCV_REG, bitmask, frame, svc,
};

/// The 64-bit index forms (LSL, UXTX, SXTX) compute the same address: one extend value.
const EXT_X: u8 = object::EXT_SXTX;
const SP: u8 = 31;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct Addr {
    base: u8,
    /// (register, extend, shift)
    index: Option<(u8, u8, u8)>,
    imm: i64,
}
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Form {
    Unsigned,
    Unscaled,
    Pre,
    Post,
    Register,
    /// `ldp`/`stp` of two X registers, signed offset (I5a: MOVAPS/MOVAPD/MOVDQA m128, one 16-byte atomic access).
    Pair,
}
#[derive(Clone, Copy, Debug)]
struct Mem {
    load: bool,
    size: u8,
    rt: u8,
    form: Form,
    /// The address as it is before the access (post-index: the base).
    addr: Addr,
    /// Writeback amount (pre/post-index).
    delta: i64,
    /// A SIMD&FP register transfer (I5a): `rt` is a vector register, no general register is written.
    vector: bool,
    /// [`Form::Pair`]: the second transfer register.
    rt2: Option<u8>,
}
#[derive(Clone, Copy, Debug)]
enum Insn {
    /// Register-only instruction writing at most this register (31: sp; a zero-register destination writes nothing).
    Data(Option<u8>),
    Mem(Mem),
    /// `swpal{b,h} Rs, Rt, [Rn]` (a memory XCHG): `size` bytes at Rn (no offset), Rt receives the old value.
    Swap {
        size: u8,
        rs: u8,
        rt: u8,
        rn: u8,
    },
    /// A LOCK read-modify-write (I3), acquire and release: `ld{add,clr,eor,set}al{b,h} Rs, Rt, [Rn]` (Rt receives the old
    /// value), `casal{b,h} Rs, Rt, [Rn]` (Rs compared and receives the old value) or `caspal Rs, Rs+1, Rt, Rt+1, [Rn]`
    /// (even pairs; `size` is both elements). No offset.
    Atomic {
        op: AtomicOp,
        size: u8,
        rs: u8,
        rt: u8,
        rn: u8,
    },
    /// `staddb wzr, [Rn]`: a split stub's probe of one byte of its store (an atomic add of zero), only there
    /// ([`Verifier::probing`]).
    Probe(u8),
    /// `dmb ish`: only right before the casal/caspal of a LOCK CMPXCHG / CMPXCHG8B / CMPXCHG16B (I3: x86 orders a locked
    /// compare that fails, while a failing Arm CAS writes nothing and has only acquire semantics).
    DmbIsh,
    /// `ldr xT, label` (refused).
    Literal {
        rt: u8,
    },
    B(i64),
    /// b.cond, cbz/cbnz, tbz/tbnz.
    Cond(i64),
    /// `svc #imm`: a literal exit's (no return) or a division's (x0-x2 change).
    Svc(u16),
    /// A computed exit's placeholder `b .` at a dispatch relocation (never returns).
    Dispatch,
    /// A probing split stub's placeholder `b .` at a probe relocation: to the store's shared probe, which returns to the
    /// stub's `adr` target ([`Verifier::probe_return`]).
    ProbeBranch,
    Brk,
    Udf,
    /// Perf chunk 15: a read of the virtual count (`mrs xT, cntvct_el0 | cntvctss_el0`: fexc emits `isb; mrs cntvct_el0`, or the
    /// self-synchronising CNTVCTSS where the vCPU presents FEAT_ECV): EL0 may read the count (CNTKCTL_EL1.EL0VCTEN), admitted
    /// only in a CPL-0 RDTSC.
    Counter(Option<u8>),
    /// `isb`: in a CPL-0 RDTSC (before its count read); elsewhere, as before, an unknown word (the code shape refuses it).
    Isb,
    /// A word the decoder does not know, admitted by [`screened`] with the code-shape check off: no branch, no x25/x28.
    Other,
    /// `nop`: only as the third word of a guest-RIP move of 32-bit code (fexc's one form there, [`Verifier::rip_move`]).
    MovePad,
}
impl Insn {
    /// Registers written (bit 31: sp).
    fn writes(&self) -> u32 {
        let bit = |r: u8| if r == 31 { 0 } else { 1 << r };
        match *self {
            Insn::Data(Some(r)) | Insn::Counter(Some(r)) => 1 << r,
            Insn::Mem(m) => {
                let wb = if matches!(m.form, Form::Pre | Form::Post) {
                    1 << m.addr.base
                } else {
                    0
                };
                wb | if m.load && !m.vector {
                    bit(m.rt) | m.rt2.map_or(0, bit)
                } else {
                    0
                }
            }
            Insn::Literal { rt, .. }
            | Insn::Swap { rt, .. }
            | Insn::Atomic {
                op: AtomicOp::Fetch,
                rt,
                ..
            } => bit(rt),
            Insn::Atomic {
                op: AtomicOp::Cas,
                rs,
                ..
            } => bit(rs),
            Insn::Atomic {
                op: AtomicOp::Casp,
                rs,
                ..
            } => bit(rs) | bit(rs + 1),
            Insn::Svc(svc::DIV_U | svc::DIV_S) => 0b111, // the division's contract: x0-x2 change
            Insn::Svc(svc::STRING) => 0b1, // the string compare's: w0 (`#X87` changes no register)
            _ => 0,
        }
    }
    fn falls_through(&self) -> bool {
        !matches!(
            self,
            Insn::B(_) | Insn::Dispatch | Insn::ProbeBranch | Insn::Svc(svc::LINK) | Insn::Brk | Insn::Udf
        )
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum AtomicOp {
    Fetch,
    Cas,
    Casp,
}

fn sext(v: u32, bits: u32) -> i64 {
    ((v << (32 - bits)) as i32 >> (32 - bits)) as i64
}

/// The A64 classes fexc emits under profile slice1, or `None`.
fn decode(w: u32, at: u32) -> Option<Insn> {
    use Insn::*;
    let rd = (w & 31) as u8;
    let zr = |r: u8| (r != 31).then_some(r);
    let rel = |imm: i64| at as i64 + imm * 4;
    let sets_flags = w >> 29 & 1 == 1;
    let (sf, n, imm6) = (w >> 31 == 1, w >> 22 & 1, w >> 10 & 63);
    Some(match w {
        _ if w >> 16 == 0 => Udf,
        _ if w & 0xffe0_001f == 0xd420_0000 => Brk,
        _ if w & 0xffe0_001f == 0xd400_0001 => Svc((w >> 5) as u16),
        _ if w & 0xfc00_0000 == 0x1400_0000 => B(rel(sext(w & 0x3ff_ffff, 26))),
        _ if w & 0xff00_0010 == 0x5400_0000 || w & 0x7e00_0000 == 0x3400_0000 => {
            Cond(rel(sext(w >> 5 & 0x7_ffff, 19)))
        }
        _ if w & 0x7e00_0000 == 0x3600_0000 => Cond(rel(sext(w >> 5 & 0x3fff, 14))),
        // The ordering words and hints FEX emits for x86 instructions (S3e): exactly these.
        0xd503_3dbf | 0xd503_3fbf | 0xd503_3ebf => Data(None), // dmb ld (LFENCE), dmb sy (MFENCE), dmb st (SFENCE)
        0xd503_203f => Data(None),                             // yield (PAUSE)
        0xd503_3bbf => DmbIsh, // dmb ish (LOCK CMPXCHG*, checked by `words`)
        _ if w & 0x3fe0_fc00 == 0x38e0_8000 => {
            let reg = |shift: u32| (w >> shift & 31) as u8;
            Swap {
                size: 1 << (w >> 30),
                rs: reg(16),
                rt: reg(0),
                rn: reg(5),
            } // swpal{b,h,,x}
        }
        _ if w & 0xffff_fc1f == 0x383f_001f => Probe((w >> 5 & 31) as u8), // staddb wzr, [xN]
        // LOCK atomics (I3), exactly the acquire-release forms FEX emits with LSE: ldaddal, ldclral, ldeoral, ldsetal (opc
        // 0xx, o3 = 0), casal, caspal (even pairs: odd ones are UNDEFINED).
        _ if w & 0x3fe0_cc00 == 0x38e0_0000 => {
            let reg = |shift: u32| (w >> shift & 31) as u8;
            Atomic {
                op: AtomicOp::Fetch,
                size: 1 << (w >> 30),
                rs: reg(16),
                rt: reg(0),
                rn: reg(5),
            }
        }
        _ if w & 0x3fe0_fc00 == 0x08e0_fc00 => {
            let reg = |shift: u32| (w >> shift & 31) as u8;
            Atomic {
                op: AtomicOp::Cas,
                size: 1 << (w >> 30),
                rs: reg(16),
                rt: reg(0),
                rn: reg(5),
            }
        }
        _ if w & 0xbfe0_fc00 == 0x0860_fc00 && w >> 16 & 1 == 0 && w & 1 == 0 => {
            let reg = |shift: u32| (w >> shift & 31) as u8;
            Atomic {
                op: AtomicOp::Casp,
                size: 8 << (w >> 30 & 1),
                rs: reg(16),
                rt: reg(0),
                rn: reg(5),
            }
        }
        _ if w & 0xffff_ffe0 == 0xd53b_4200 => Data(zr(rd)), // mrs xT, nzcv
        _ if w & 0xffff_ffe0 == 0xd53b_e040 || w & 0xffff_ffe0 == 0xd53b_e0c0 => Counter(zr(rd)), // mrs xT, cntvct_el0 | cntvctss_el0
        0xd503_3fdf => Isb,
        _ if w & 0xffff_ffe0 == 0xd51b_4200 => Data(None), // msr nzcv, xT
        0xd500_401f => Data(None),                         // cfinv
        _ if w & 0xffe0_7c10 == 0xba00_0400 => Data(None), // rmif
        _ if w & 0xffff_bc1f == 0x3a00_080d => Data(None), // setf8, setf16
        _ if w & 0x9f00_0000 == 0x1000_0000 => Data(zr(rd)), // adr
        // Unallocated encodings (sf = 0 with N = 1 or a shift of 32 or more, reserved shift or option fields) are
        // refused, not left to the CPU.
        // add/sub (immediate, extended register): 31 is sp unless flags are set
        _ if w & 0x1f80_0000 == 0x1100_0000 => Data(if sets_flags { zr(rd) } else { Some(rd) }),
        _ if w & 0x1f20_0000 == 0x0b20_0000 && w >> 22 & 3 == 0 && w >> 10 & 7 <= 4 => {
            Data(if sets_flags { zr(rd) } else { Some(rd) })
        }
        // logical (immediate): 31 is sp unless ands
        _ if w & 0x1f80_0000 == 0x1200_0000 && (sf || n == 0) => {
            Data(if w >> 29 & 3 == 3 { zr(rd) } else { Some(rd) })
        }
        _ if w & 0x1f80_0000 == 0x1280_0000 && w >> 29 & 3 != 1 && (sf || w >> 22 & 1 == 0) => {
            Data(zr(rd))
        } // movn, movz, movk
        _ if w & 0x1f80_0000 == 0x1300_0000
            && w >> 29 & 3 != 3
            && n == sf as u32
            && (sf || w & 0x0020_8000 == 0) =>
        {
            Data(zr(rd))
        } // sbfm, bfm, ubfm
        _ if w & 0x7fa0_0000 == 0x1380_0000 && n == sf as u32 && (sf || imm6 < 32) => Data(zr(rd)), // extr
        _ if w & 0x1f00_0000 == 0x0a00_0000 && (sf || imm6 < 32) => Data(zr(rd)), // logical (shifted register)
        _ if w & 0x1f20_0000 == 0x0b00_0000 && w >> 22 & 3 != 3 && (sf || imm6 < 32) => {
            Data(zr(rd))
        } // add/sub (shifted register)
        _ if w & 0x1fe0_fc00 == 0x1a00_0000 => Data(zr(rd)),                      // adc, sbc(s)
        _ if w & 0x3fe0_0410 == 0x3a40_0000 => Data(None),                        // ccmp, ccmn
        _ if w & 0x3fe0_0800 == 0x1a80_0000 => Data(zr(rd)), // csel, csinc, csinv, csneg
        _ if w & 0x5fe0_0000 == 0x1ac0_0000 && matches!(w >> 10 & 63, 2 | 3 | 8..=11) => {
            Data(zr(rd))
        } // div, shifts
        // crc32{,c}{b,h,w,x} (I5a, x86 CRC32): X only with sf = 1, the others with sf = 0.
        _ if w & 0x5fe0_0000 == 0x1ac0_0000
            && matches!(w >> 10 & 63, 16..=23)
            && (w >> 10 & 3 == 3) == sf =>
        {
            Data(zr(rd))
        }
        0xd500_405f | 0xd500_403f => Data(None), // axflag, xaflag (FlagM2: SSE compares' flags)
        // I5a: FPSR around RCPPS/RSQRTPS (x86 raises no SIMD exception there), FPCR.RMode around ROUND*'s `frintx`.
        _ if w & 0xffff_ffc0 == 0xd53b_4400 => Data(zr(rd)), // mrs xT, fpcr | fpsr (op2 0 | 1)
        _ if w & 0xffff_ffc0 == 0xd51b_4400 => Data(None),   // msr fpcr | fpsr, xT
        _ if w & 0x5fff_0000 == 0x5ac0_0000 && w >> 10 & 63 <= 5 => Data(zr(rd)), // rbit, rev*, clz, cls
        _ if w & 0x7f00_0000 == 0x1b00_0000 && matches!(w >> 21 & 7, 0 | 1 | 2 | 5 | 6) => {
            Data(zr(rd))
        } // madd..umulh
        // POPCNT without FEAT_CSSC (I1, FEX JIT/ALUOps.cpp Popcount): `fmov d0, xN | fmov s0, wN; cnt v0.8b, v0.8b;
        // addv b0, v0.8b | addp v0.8b, v0.8b, v0.8b; umov wD, v0.b[0]`, exactly these on v0 (FEX's VTMP1).
        _ if w & 0xffff_fc1f == 0x9e67_0000 => Data(None), // fmov d0, xN
        _ if w & 0xffff_fc1f == 0x1e27_0000 => Data(None), // fmov s0, wN (32- and 16-bit sources)
        0x0e20_5800 | 0x0e31_b800 | 0x0e20_bc00 => Data(None), // cnt v0.8b, v0.8b; addv b0, v0.8b; addp v0.8b (16-bit)
        _ if w & 0xffff_ffe0 == 0x0e01_3c00 => Data(zr(rd)),   // umov wD, v0.b[0]
        // Prefetch hints (I1, x86 prefetchh: never faults): `prfm <op>, [xN{, #imm}]`, no data transfer.
        _ if w & 0xffc0_0000 == 0xf980_0000 => Data(None),
        // Scalar floating-point and Advanced SIMD data processing (op0 = x111; SSE, I5a): no branch, no memory access, writes
        // vector registers and NZCV only, but for the forms with a general destination: FP to integer conversions and the
        // moves to a general register (opcode 000, 001, 100, 101, 110 of the FP<->integer class) and UMOV/SMOV. An
        // unallocated encoding of the class is UNDEFINED and traps at EL0 (`trapped`).
        _ if w & 0x0e00_0000 == 0x0e00_0000 => {
            // ... and the fixed-point FCVTZS/FCVTZU (bit 21 clear, rmode 11, opcode 00x), also to a general register.
            let to_gpr = (w & 0x7f20_fc00 == 0x1e20_0000
                && matches!(w >> 16 & 7, 0 | 1 | 4 | 5 | 6))
                || w & 0x7f3e_0000 == 0x1e18_0000
                || (w & 0xbfe0_8400 == 0x0e00_0400
                    && w >> 29 & 1 == 0
                    && matches!(w >> 11 & 15, 5 | 7));
            Data(if to_gpr { zr(rd) } else { None })
        }
        _ => return memory(w),
    })
}

/// General-register and SIMD&FP (I5a: b, h, s, d, q) loads and stores: literal, unsigned offset, unscaled, pre/post-index,
/// register offset. No sign-extending SIMD&FP forms exist; a 128-bit one is size 0 with opc 1x.
fn memory(w: u32) -> Option<Insn> {
    let (rt, rn) = ((w & 31) as u8, (w >> 5 & 31) as u8);
    if w & 0x3b00_0000 == 0x1800_0000 {
        return (w >> 26 & 1 == 0 && w >> 30 == 1).then_some(Insn::Literal { rt });
    }
    // I5a: `ldp`/`stp xT, xT2, [xN, #imm7*8]` (signed offset, no writeback); a load naming one register twice is UNPREDICTABLE.
    if matches!(w & 0xffc0_0000, 0xa900_0000 | 0xa940_0000) {
        let (load, rt2) = (w >> 22 & 1 == 1, (w >> 10 & 31) as u8);
        if load && rt == rt2 {
            return None;
        }
        let addr = Addr {
            base: rn,
            index: None,
            imm: sext(w >> 15 & 0x7f, 7) * 8,
        };
        return Some(Insn::Mem(Mem {
            load,
            size: 16,
            rt,
            form: Form::Pair,
            addr,
            delta: 0,
            vector: false,
            rt2: Some(rt2),
        }));
    }
    // I8: `ldp`/`stp qT, qT2, [xN, #imm7*16]` (signed offset, no writeback): a VEX.256 operand's 32 bytes, and FEX's paired
    // accesses to the YMM upper halves in the guest-state page. A load naming one register twice is UNPREDICTABLE.
    if matches!(w & 0xffc0_0000, 0xad00_0000 | 0xad40_0000) {
        let (load, rt2) = (w >> 22 & 1 == 1, (w >> 10 & 31) as u8);
        if load && rt == rt2 {
            return None;
        }
        let addr = Addr {
            base: rn,
            index: None,
            imm: sext(w >> 15 & 0x7f, 7) * 16,
        };
        return Some(Insn::Mem(Mem {
            load,
            size: 32,
            rt,
            form: Form::Pair,
            addr,
            delta: 0,
            vector: true,
            rt2: Some(rt2),
        }));
    }
    let vector = w >> 26 & 1 != 0;
    let opc = w >> 22 & 3;
    let (size, load) = if vector {
        match (w >> 30, opc) {
            (s, 0 | 1) => (s, opc == 1),
            (0, 2 | 3) => (4, opc == 3),
            _ => return None,
        }
    } else {
        let size = w >> 30;
        let load = match (size, opc) {
            (_, 0) => false,
            (_, 1) | (0 | 1, 2 | 3) | (2, 2) => true,
            _ => return None,
        };
        (size, load)
    };
    let base = |imm: i64| Addr {
        base: rn,
        index: None,
        imm,
    };
    let simm9 = sext(w >> 12 & 0x1ff, 9);
    let (form, addr, delta) = if w & 0x3b00_0000 == 0x3900_0000 {
        (Form::Unsigned, base(((w >> 10 & 0xfff) << size) as i64), 0)
    } else if w & 0x3b20_0000 == 0x3800_0000 {
        match w >> 10 & 3 {
            0 => (Form::Unscaled, base(simm9), 0),
            1 => (Form::Post, base(0), simm9),
            3 => (Form::Pre, base(simm9), simm9),
            _ => return None,
        }
    } else if w & 0x3b20_0c00 == 0x3820_0800 {
        let ext = match w >> 13 & 7 {
            2 => object::EXT_UXTW,
            6 => object::EXT_SXTW,
            3 | 7 => EXT_X,
            _ => return None,
        };
        let shift = if w >> 12 & 1 == 1 { size as u8 } else { 0 };
        (
            Form::Register,
            Addr {
                base: rn,
                index: Some(((w >> 16 & 31) as u8, ext, shift)),
                imm: 0,
            },
            0,
        )
    } else {
        return None;
    };
    // Writeback with the transfer register as base is CONSTRAINED UNPREDICTABLE (general registers only).
    if matches!(form, Form::Pre | Form::Post) && rn == rt && rn != SP && !vector {
        return None;
    }
    Some(Insn::Mem(Mem {
        load,
        size: 1 << size,
        rt,
        form,
        addr,
        delta,
        vector,
        rt2: None,
    }))
}

/// Object validation of a word the decoder does not know (admitted only with the code-shape check off; the hardware
/// contains whatever it does at EL0): nothing from the branch, exception-generating and system group (op0 = x101) but
/// its system class (0xd5: MSR/MRS/SYS/hints/barriers, which trap or are harmless at EL0), so no branch of any present
/// or future encoding (e.g. Armv9.6 CMPBR) can loop or skip an exit: every exit, stop and resident entry is a form
/// verified here. And no register field names x25 or x28 (CASP's pairs: nor x24 or x27), so it neither writes them
/// nor addresses the frame through x28. ponytail: conservative, refuses any such word even when the field is an
/// immediate; LD64B (8 registers) is UNDEFINED on the M4 and traps at EL0.
fn screened(w: u32) -> bool {
    let branch = w & 0x1c00_0000 == 0x1400_0000 && w >> 24 != 0xd5;
    let pair = w & 0xbfa0_7c00 == 0x0820_7c00; // casp: Rs, Rs+1 and Rt, Rt+1
    let named = |r: u32| r == 25 || r == 28 || (pair && (r == 24 || r == 27));
    !branch
        && ![w & 31, w >> 5 & 31, w >> 10 & 31, w >> 16 & 31]
            .into_iter()
            .any(named)
}

// Exact words of the fixed sequences.
const SVC_LINK: u32 = 0xd400_0001 | (svc::LINK as u32) << 5;
const SVC_DIV_U: u32 = 0xd400_0001 | (svc::DIV_U as u32) << 5;
const SVC_DIV_S: u32 = 0xd400_0001 | (svc::DIV_S as u32) << 5;
#[cfg(test)]
const SVC_X87: u32 = 0xd400_0001 | (svc::X87 as u32) << 5;
#[cfg(test)]
const SVC_STRING: u32 = 0xd400_0001 | (svc::STRING as u32) << 5;
const MOVZ_W0: u32 = 0x5280_0000; // movz w0, #imm16 (imm16 << 5)
const MRS_PRE_NZCV: u32 = 0xd53b_4200 | PRE_NZCV_REG as u32; // mrs x25, nzcv
/// `eor x0, xA, xA, lsl #16` (the branch-target check)
fn eor_guard(a: u32) -> u32 {
    0xca00_4000 | a << 16 | a << 5
}
/// Normalized check branches (offset field zero): taken when the check fails.
const TBNZ_X0_63: u32 = 0xb7f8_0000;
const TBNZ_W0_12: u32 = 0x3760_0000;
const TBZ_X0_63: u32 = 0xb6f8_0000;
const CBZ_X: u32 = 0xb400_0000;
const CBZ_W: u32 = 0x3400_0000;
const CBNZ_X: u32 = 0xb500_0000;
const CBNZ_W: u32 = 0x3500_0000;

/// Perf chunk 26: MOV r64, CR2 as fexc translates it (`--cpl0`): an optional REX without R (CR10 is #UD), 0f 20, ModRM
/// with mod 11 and reg 2.
fn x86_mov_from_cr2(insn: &[u8]) -> bool {
    match insn {
        [0x0f, 0x20, m] => m >> 3 == 0b11_010,
        [rex, 0x0f, 0x20, m] => rex & 0xf4 == 0x40 && m >> 3 == 0b11_010,
        _ => false,
    }
}
/// Perf P2: VERW m16 as fexc translates it (`--cpl0`): an optional REX, 0f 00, ModRM with reg 5 and a memory operand (its
/// bytes after it).
fn x86_verw(insn: &[u8]) -> bool {
    let o = usize::from(insn.first().is_some_and(|b| b & 0xf0 == 0x40));
    insn.get(o..o + 2) == Some(&[0x0f, 0x00])
        && insn
            .get(o + 2)
            .is_some_and(|m| m >> 3 & 7 == 5 && m >> 6 != 3)
}
/// Perf chunk 26: the element width of a REP STOS in 64-bit addressing (legacy prefixes with F3, no 67; 66 for words; REX.W
/// for quadwords), or `None`.
fn x86_rep_stos(insn: &[u8]) -> Option<u32> {
    let (o, rex) = crate::system::opcode_at(insn);
    let prefixes = &insn[..o];
    if !prefixes.contains(&0xf3)
        || prefixes.contains(&0xf2)
        || prefixes.contains(&0x67)
        || o + 1 != insn.len()
    {
        return None;
    }
    match insn[o] {
        0xaa => Some(1),
        0xab if rex & 8 != 0 => Some(8),
        0xab if prefixes.contains(&0x66) => Some(2),
        0xab => Some(4),
        _ => None,
    }
}
/// How a word of [`bulk_words`] is compared: exactly, or as a cbz/cbnz/tbnz whose offset is to the sequence's end (a skip)
/// or back to its `stp` (the loop's back edge).
#[derive(Clone, Copy)]
enum Bulk {
    Word,
    Skip,
    Back,
}
/// The cbz/cbnz/tbz/tbnz `w` (offset field zero) branching `delta` bytes.
fn with_offset(w: u32, delta: i64) -> u32 {
    let imm = (delta / 4) as u32;
    if w & 0x7e00_0000 == 0x3600_0000 {
        w | (imm & 0x3fff) << 5
    } else {
        w | (imm & 0x7_ffff) << 5
    }
}
/// FexcRepStos's words for `w`-byte elements (RCX x7, RDI x11, RAX x4: the static register map admission requires; see
/// `fexc_sites.inc`).
fn bulk_words(w: u32) -> Vec<(u32, Bulk)> {
    use Bulk::*;
    let mut v = vec![
        (0x3940_0000 | frame::FAST_STRINGS << 10 | 28 << 5, Word), // ldrb w0, [x28, #fast_strings]
        (0x3400_0000, Skip),                                       // cbz w0, S
        (0x3940_0000 | frame::DF << 10 | 28 << 5, Word),           // ldrb w0, [x28, #df]
        (0x3638_0000 | 1 << 24, Skip),                             // tbnz w0, #7, S
    ];
    if w > 1 {
        v.extend([(0x9240_0960, Word), (0xb500_0000, Skip)]); // and x0, x11, #7; cbnz x0, S
    }
    let s = 4 - w.trailing_zeros();
    v.extend([
        (0xd340_fce1 | s << 16, Word), // lsr x1, x7, #s
        (0x9240_2d60, Word),           // and x0, x11, #0xfff
        (0x5282_0002, Word),           // movz w2, #0x1000
        (0xcb00_0040, Word),           // sub x0, x2, x0
        (0xd344_fc00, Word),           // lsr x0, x0, #4
        (0xcb00_0022, Word),           // sub x2, x1, x0
        (0x8a82_fc42, Word),           // and x2, x2, x2, asr #63
        (0x8b02_0001, Word),           // add x1, x0, x2
        (0xb400_0001, Skip),           // cbz x1, S
    ]);
    let d = match w {
        8 => 4,
        4 => {
            v.extend([(0x2a04_03e2, Word), (0xaa02_8042, Word)]); // mov w2, w4; orr x2, x2, x2, lsl #32
            2
        }
        _ => {
            v.push((if w == 2 { 0x9240_3c82 } else { 0x9240_1c82 }, Word)); // and x2, x4, #0xffff | #0xff
            v.extend(
                (0..)
                    .map(|k| 8 * w << k)
                    .take_while(|&b| b < 64)
                    .map(|b| (0xaa02_0042 | b << 10, Word)),
            ); // orr x2, x2, x2, lsl #b
            2
        }
    };
    v.extend([
        (0xa900_0160 | d << 10 | d, Word),    // stp xD, xD, [x11]
        (0x9100_416b, Word),                  // add x11, x11, #16
        (0xd100_00e7 | (16 / w) << 10, Word), // sub x7, x7, #16/w
        (0xd100_0421, Word),                  // sub x1, x1, #1
        (0xb500_0001, Back),                  // cbnz x1, L
    ]);
    v
}
/// Bulk REP plan (first cut): the element width of a REP MOVSB or MOVSQ that fexc may give a bulk loop: F3 among the legacy
/// prefixes, no F2, 67, FS or GS (64-bit mode ignores the other segment prefixes, and FEX's decoder records none of them);
/// A4, or REX.W A5. `None` for anything else.
fn x86_rep_movs(insn: &[u8]) -> Option<u32> {
    let (o, rex) = crate::system::opcode_at(insn);
    let prefixes = &insn[..o];
    if !prefixes.contains(&0xf3)
        || prefixes.iter().any(|p| matches!(p, 0xf2 | 0x67 | 0x64 | 0x65))
        || o + 1 != insn.len()
    {
        return None;
    }
    match insn[o] {
        0xa4 => Some(1),
        0xa5 if rex & 8 != 0 => Some(8),
        _ => None,
    }
}
/// Gather plan (docs/restructure/gather-fast-path-plan.md 2.5): an AVX2 gather that fexc may compile inline in 64-bit code
/// (C4 VEX, map 0F38, pp 66, opcode 90..93; no 0x67, FS or GS prefix: those stay the resident's): its element width and number
/// of elements (the form's 16 or 32 bytes over the wider of element and index). `None` for anything else (a 66, F2, F3 or REX
/// before VEX is #UD, a system cut).
fn x86_gather(insn: &[u8]) -> Option<(u8, u32)> {
    let o = insn.iter().position(|b| !matches!(b, 0x26 | 0x2e | 0x36 | 0x3e))?;
    let [0xc4, p1, p2, op, ..] = insn[o..] else {
        return None;
    };
    if p1 & 0x1f != 2 || p2 & 3 != 1 || !(0x90..=0x93).contains(&op) {
        return None;
    }
    let (ew, iw) = (if p2 & 0x80 != 0 { 8 } else { 4 }, if op & 1 != 0 { 8 } else { 4 });
    Some((ew as u8, (16u32 << (p2 >> 2 & 1)) / ew.max(iw)))
}
/// Bulk REP plan (section 14): how a word of [`bulk_movs_words`] is compared: exactly, or as a cbz/cbnz/tbnz to the sequence's
/// end (S), to its element loop's entry (E), or back to the rounds' `ldp` (B) or the element `ldr` (L).
#[derive(Clone, Copy, PartialEq)]
enum Movs {
    Word,
    ToEnd,
    ToElements,
    BackRounds,
    BackElement,
}
/// Bulk REP plan: FexcRepMovs's words for `w`-byte elements (RCX x7, RSI x10, RDI x11, x28 the state frame; see
/// `fexc_sites.inc`), and the indices of the rounds' `ldp` (B), the element loop's entry `cbz x1` (E) and its `ldr` (L).
fn bulk_movs_words(w: u32) -> (Vec<(u32, Movs)>, usize, usize, usize) {
    use Movs::*;
    let (lw, sr) = (w.trailing_zeros(), 4 - w.trailing_zeros());
    let mut v = vec![
        (0xd341_fce0, Word),                                       // lsr x0, x7, #1
        (0xb400_0000, ToEnd),                                      // cbz x0, S
        (0xb7f8_0007, ToEnd),                                      // tbnz x7, #63, S (the mins below are signed)
        (0x3940_0000 | frame::FAST_STRINGS << 10 | 28 << 5, Word), // ldrb w0, [x28, #fast_strings]
        (0x3400_0000, ToEnd),                                      // cbz w0, S
        (0x3940_0000 | frame::DF << 10 | 28 << 5, Word),           // ldrb w0, [x28, #df]
        (0x3638_0000 | 1 << 24, ToEnd),                            // tbnz w0, #7, S
    ];
    // x1 = min(x7 | x1, the whole elements before x11's | x10's page end)
    let lsr = 0xd340_fc00 | lw << 16; // lsr x0, x0, #log2 w
    for (and, first) in [(0x9240_2d60, true), (0x9240_2d40, false)] {
        v.extend([
            (and, Word),                                              // and x0, x11 | x10, #0xfff
            (0x5282_0002, Word),                                      // movz w2, #0x1000
            (0xcb00_0040, Word),                                      // sub x0, x2, x0
            (lsr, Word),                                              // lsr x0, x0, #log2 w
            (if first { 0xcb00_00e2 } else { 0xcb00_0022 }, Word),   // sub x2, x7 | x1, x0
            (0x8a82_fc42, Word),                                      // and x2, x2, x2, asr #63
            (0x8b02_0001, Word),                                      // add x1, x0, x2
        ]);
    }
    v.extend([
        (0xb400_0001, ToEnd),                     // cbz x1, S
        (0xd340_fc20 | sr << 16, Word),           // lsr x0, x1, #(4 - log2 w)
        (0xb400_0000, ToElements),                // cbz x0, E
    ]);
    if w > 1 {
        // orr x2, x10, x11; and x2, x2, #7; cbnz x2, E
        v.extend([(0xaa0b_0142, Word), (0x9240_0842, Word), (0xb500_0002, ToElements)]);
    }
    v.extend([
        (0xcb0a_0162, Word),                      // sub x2, x11, x10
        (0xd100_0442, Word),                      // sub x2, x2, #1
        (0x9240_2c42, Word),                      // and x2, x2, #0xfff
        (0xd100_3c42, Word),                      // sub x2, x2, #15
        (0xb7f8_0002, ToElements),                // tbnz x2, #63, E
        (0xcb00_0021 | sr << 10, Word),           // sub x1, x1, x0, lsl #(4 - log2 w)
    ]);
    let b = v.len();
    v.extend([
        (0xa940_0d42, Word),                      // ldp x2, x3, [x10]
        (0xa900_0d62, Word),                      // stp x2, x3, [x11]
        (0x9100_414a, Word),                      // add x10, x10, #16
        (0x9100_416b, Word),                      // add x11, x11, #16
        (0xd100_00e7 | (16 / w) << 10, Word),     // sub x7, x7, #16/w
        (0xd100_0400, Word),                      // sub x0, x0, #1
        (0xb500_0000, BackRounds),                // cbnz x0, B
    ]);
    let e = v.len();
    let (ld, st) = if w == 1 { (0x3940_0142, 0x3900_0162) } else { (0xf940_0142, 0xf900_0162) };
    v.extend([
        (0xb400_0001, ToEnd),                     // E: cbz x1, S
        (ld, Word),                               // L: ldr(b) x2, [x10]
        (st, Word),                               // str(b) x2, [x11]
        (0x9100_014a | w << 10, Word),            // add x10, x10, #w
        (0x9100_016b | w << 10, Word),            // add x11, x11, #w
        (0xd100_04e7, Word),                      // sub x7, x7, #1
        (0xd100_0421, Word),                      // sub x1, x1, #1
        (0xb500_0001, BackElement),               // cbnz x1, L
    ]);
    (v, b, e, e + 1)
}
/// How a stub is reached: the words `start..=end` end in its branch; `cond` is the normalized conditional branch
/// (the far form `!cond +8; b stub` normalized to `cond`), `None` for a plain `b`.
use natemu_abi::execution::RUN_END;
#[derive(Clone, Copy)]
struct Check {
    start: u32,
    end: u32,
    cond: Option<u32>,
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
enum DivCheck {
    /// Unsigned 8/16/32: high part >= divisor.
    High,
    Zero,
    Overflow,
    /// After the 128/64 division (`svc`): `cbnz x2`.
    Verdict,
}

pub(super) struct Code<'a> {
    /// The code region, from the block start (literal slots resolved).
    pub code: &'a [u8],
    pub literals: &'a [Range<usize>],
    pub accesses: &'a [Site],
    pub stubs: &'a [Site],
    /// The FMA NaN checks (AFP accurate plan chunk 4), in the body.
    pub fma: &'a [Site],
    pub pcs: &'a [PcEntry],
    pub relocs: &'a [Reloc],
    /// Guest bytes given to fexc, at `rip`; the block translated `guest_bytes` of them.
    pub source: &'a [u8],
    pub rip: u64,
    pub guest_bytes: u32,
    /// The guest RIPs a literal exit may go to: the block end, and the direct target of the branch ending it.
    pub targets: &'a [u64],
    /// Also run the code-shape check (debug).
    pub shape: bool,
    /// The request was at CPL 0 (perf chunk 15): IF's flag byte is writable, by CLI and STI only ([`Verifier::frame_access`]).
    pub cpl0: bool,
    /// M2: 32-bit code: guest addresses wrap at 4 GiB and the ES/CS/SS/DS bases may be loaded ([`Verifier::frame_access`]).
    pub bits32: bool,
    /// M2 checkpoint C, 32-bit code: the request was at CPL 3 (only there may an instruction have an FS or GS operand: the
    /// operand check's record is the user dispatcher table's), and its FS, GS was a limited segment (the code identity's
    /// bits: every instruction's operand through it is checked, [`Verifier::segment_checks`]).
    pub cpl3: bool,
    pub limited: [bool; 2],
}

impl Code<'_> {
    /// The guest address `offset` bytes into the block.
    fn address(&self, offset: u64) -> u64 {
        super::wrap(self.rip.wrapping_add(offset), self.bits32)
    }
}

pub(super) fn canonical(a: u64) -> bool {
    a >> 47 == 0 || a >> 47 == 0x1ffff
}

struct Verifier<'a> {
    c: &'a Code<'a>,
    insns: Vec<Option<Insn>>,
    /// Direct branches (source, target), sorted by target.
    branches: Vec<(u32, u32)>,
    /// Guest-RIP move sequences: words, register, value.
    moves: Vec<(Range<u32>, u8, u64)>,
    /// Per stub (same order as `c.stubs`): how it is reached, and whether a rule accepted that check.
    checks: Vec<Check>,
    claimed: Vec<bool>,
    /// The literal exits found by `exits`.
    links: Vec<LinkPoint>,
    /// The computed exit's dispatch branch (a dispatch relocation).
    dispatch: Option<u32>,
    /// Perf chunk 26: the bulk REP STOS loops, as the native range of each one's words ([`Verifier::bulk_loop`]).
    bulk: Vec<Range<u32>>,
    /// Stack check runs (fexc --push-run): [`Verifier::aligned`], computed with the code shape (after `stub_checks`).
    aligned_before: Vec<u32>,
    /// M2 checkpoint C: the loads of the read-only FS/GS records, each proved part of an operand check by `segment_checks`.
    segment_view_reads: Vec<u32>,
}

/// Verifies the code (object validation, and the code-shape check when `c.shape`); returns its link points, its computed
/// exit's dispatch branch and whether it holds a bulk REP STOS loop (perf chunk 26), a bulk REP MOVS sequence or an inline
/// gather's element load (gather plan): whether only a `--bulk` request may have it.
pub(super) fn check(c: &Code) -> Result<(Vec<LinkPoint>, Option<u32>, bool), String> {
    let operands = segment_operands(c)?;
    // A block with an operand check relies on the exact memory-word, stub and control-flow rules of the code shape, also in
    // production: no native access of it is unrecorded.
    let checked = Code { shape: c.shape || operands.iter().any(|o| o.limited), ..*c };
    let c = &checked;
    let mut v = Verifier::new(c)?;
    v.segment_checks(&operands)?;
    v.segment_bases(&operands)?;
    v.words()?;
    v.exits()?;
    // Gather plan (2.5): an inline gather's element loads exist only in 64-bit `--bulk` code; such an object is a bulk one,
    // which a request without `--bulk` (a step) refuses, so a step never holds one (the resident's STEP guard is the other half).
    let gather = c.accesses.iter().any(object::gather_element);
    if gather && c.bits32 {
        return Err("a gather element site in a 32-bit object".into());
    }
    if c.shape {
        v.gathers()?;
        v.stub_checks()?;
        v.aligned_before = v.aligned();
        v.sites()?;
        v.branch_target()?;
        v.divisions()?;
        v.fp_checks()?;
        v.fma_checks()?;
        if let Some(i) = v.claimed.iter().position(|&ok| !ok) {
            return Err(format!(
                "stub at {:#x} is reached by no check of its kind",
                c.stubs[i].native_offset
            ));
        }
    }
    Ok((v.links, v.dispatch, !v.bulk.is_empty() || gather))
}

/// M2 checkpoint C: an instruction's memory operand through a limited FS or GS, from an independent decode of the source.
struct SegmentOperand {
    offset: u32,
    segment: u8,
    width: u8,
    write: bool,
    /// The segment is limited (`c.limited`): the operand needs its check.
    limited: bool,
}

/// M2 checkpoint C. Decodes the whole source of a 32-bit block, independently of the producer's PC table and of any list of
/// instruction forms (a prefix byte inside a displacement or an immediate is ordinary data), and returns each instruction's
/// memory operand through FS or GS, with whether that segment is limited (`c.limited`: the operand needs a check), the
/// operand's width and whether the instruction can write it (a conditional write is a write, a conditional read a read;
/// LEA and the hint NOPs access nothing). Refused here: an FS or GS operand in a block that is not CPL 3; an access site
/// whose segment tag is not its instruction's FS or GS (the resident judges an access that runs across 4 GiB by that tag);
/// a based string, which is the resident's.
fn segment_operands(c: &Code) -> Result<Vec<SegmentOperand>, String> {
    use iced_x86::{Decoder, DecoderOptions, InstructionInfoFactory, OpAccess, Register};
    use object::kind::*;
    let src = &c.source[..c.guest_bytes as usize];
    let tagged = |s: &Site| matches!(s.segment, object::SEG_FS | object::SEG_GS);
    let mut found = Vec::new();
    if !c.bits32 || !src.iter().any(|b| matches!(b, 0x64 | 0x65)) {
        if c.accesses.iter().chain(c.stubs).any(|s| tagged(s) || s.kind == SEGMENT) {
            return Err("an FS or GS site in a block without an FS or GS prefix".into());
        }
        return Ok(found);
    }
    let mut factory = InstructionInfoFactory::new();
    let mut decoder = Decoder::new(32, src, DecoderOptions::NONE);
    let mut starts = Vec::new();
    while decoder.can_decode() {
        let g = decoder.position() as u32;
        let i = decoder.decode();
        if i.is_invalid() {
            return Err("invalid compatibility source instruction".into());
        }
        if crate::system::based_string(&i).is_some() {
            return Err("based compatibility string must be a system transaction".into());
        }
        starts.push(g);
        let mut through = None;
        for m in factory.info(&i).used_memory() {
            let segment = match m.segment() {
                Register::FS => object::SEG_FS,
                Register::GS => object::SEG_GS,
                _ => continue,
            };
            let write = match m.access() {
                OpAccess::Read | OpAccess::CondRead => false,
                OpAccess::Write | OpAccess::CondWrite | OpAccess::ReadWrite | OpAccess::ReadCondWrite => true,
                _ => continue,
            };
            let Ok(width) = u8::try_from(m.memory_size().size()) else {
                return Err(format!("FS or GS operand at guest offset {g:#x}: its width"));
            };
            if through.replace((segment, width, write)).is_some() {
                return Err(format!("two FS or GS operands at guest offset {g:#x}"));
            }
        }
        // The instruction's own accesses of its explicit operand carry the tag; its stack accesses (push and pop kinds) and
        // every site of an instruction without such an operand do not.
        for a in c.accesses.iter().chain(c.stubs).filter(|a| a.guest_offset == g && a.kind != SEGMENT) {
            let explicit = !matches!(a.kind, PUSH | POP | PUSH_PAIR | POP_PAIR);
            let expected = through.filter(|_| explicit).map(|t| t.0);
            let stub = a.native_offset >= c.stubs.first().map_or(u32::MAX, |s| s.native_offset);
            // (A stub's tag is its access's; the stubs that cover no access have none.)
            if if stub { tagged(a) && expected != Some(a.segment) } else { expected.unwrap_or(a.segment) != a.segment || (tagged(a) && expected.is_none()) } {
                return Err(format!("site at {:#x}: its segment is not its instruction's operand's", a.native_offset));
            }
        }
        let Some((segment, width, write)) = through else { continue };
        if !c.cpl3 {
            return Err(format!("FS or GS operand at guest offset {g:#x} in a block that is not CPL 3"));
        }
        found.push(SegmentOperand { offset: g, segment, width, write, limited: c.limited[usize::from(segment - object::SEG_FS)] });
    }
    let mut reported: Vec<_> = c.pcs.iter().map(|p| p.guest_offset).filter(|&g| g < c.guest_bytes).collect();
    reported.dedup();
    if starts != reported {
        return Err("the instruction boundaries of a block with an FS or GS prefix".into());
    }
    Ok(found)
}

/// A retained 32-bit block can rewrite a guest-RIP literal slot or a complete MOVZ/MOVK/NOP sequence at another RIP, and a
/// placed one's dispatch and probe branches are rewritten: none of them may lie in `words`.
fn overlaps_relocation32(words: Range<u32>, relocs: &[Reloc]) -> bool {
    relocs.iter().any(|r| {
        let bytes = match r.kind {
            object::RELOC_GUEST_RIP_LITERAL => 8,
            object::RELOC_GUEST_RIP_MOVE => 12,
            _ => 4,
        };
        r.offset < words.end && r.offset.saturating_add(bytes) > words.start
    })
}

impl<'a> Verifier<'a> {
    /// M2 checkpoint C: each operand through a limited FS or GS has exactly one check, fexc's `FexcSegmentBase`, before every
    /// access of its instruction through that segment:
    /// `and x1, x28, #~0x1fffff; ubfx x2, x28, #21, #9; add x2, x2, #1; lsr x2, x2, #9; add x1, x1, #512, lsl #12;
    /// sub x1, x1, x2, lsl #22; ldr x1, [x1, #slot]` (the vCPU's read-only record: `(limit + 1) << 32 | base`, or 0),
    /// `lsr x2, x1, #32; sub x2, x2, #W; sub x2, x2, wOffset, uxtw; tbnz x2, #63, stub`, then `mov wBase, w1`, where the resident resumes with the base in x1. W is the operand's width and the
    /// stub's intent the operand's, both as this module decoded them. No branch enters the sequence and no relocation lies in
    /// it. With [`Verifier::frame_access`] refusing every load of a limited segment's base field, the base an access adds can
    /// only be this check's.
    fn segment_checks(&mut self, operands: &[SegmentOperand]) -> Result<(), String> {
        use object::kind::SEGMENT;
        for op in operands.iter().filter(|o| o.limited) {
            let g = op.offset;
            let fail = |what: &str| Err(format!("operand check of the instruction at guest offset {g:#x}: {what}"));
            let mut stubs = self.c.stubs.iter().filter(|s| s.kind == SEGMENT && s.guest_offset == g);
            let (Some(s), None) = (stubs.next(), stubs.next()) else {
                return fail("not exactly one");
            };
            if s.segment != op.segment || s.width != op.width || s.intent != u8::from(op.write) {
                return fail("its segment, width or intent is not the operand's");
            }
            let (sub, back, offset) = (s.guest_offset2, s.imm as u32, u32::from(s.base_reg));
            let Some(start) = sub.checked_sub(32).filter(|&o| o >= ENTRY_OFFSET && sub % 4 == 0) else {
                return fail("its place");
            };
            if back % 4 != 0 || back as usize + 4 > self.c.code.len() || back <= sub || back - sub > 16 {
                return fail("its continuation");
            }
            let slot = natemu_abi::execution::cache::SEGMENT_SLOTS[usize::from(op.segment - object::SEG_FS)] as u32 * 16;
            let head = [
                0x926b_ab81,
                0xd355_7782,
                0x9100_0442,
                0xd349_fc42,
                0x9148_0021,
                0xcb02_5821,
                0xf940_0021 | (slot / 8) << 10,
                0xd360_fc22,
                0xd100_0042 | u32::from(op.width) << 10,
                0xcb20_4042 | offset << 16,
            ];
            if !(3..31).contains(&offset) || offset == 28 || head.iter().enumerate().any(|(k, &w)| self.word(start + 4 * k as u32) != w) {
                return fail("its words");
            }
            let test = sub + 8;
            let to = |at: u32, imm: u32, bits: u32| at.wrapping_add(((imm << (32 - bits)) as i32 >> (30 - bits)) as u32);
            // (fexc's far form of the test, for a stub beyond `tbnz`'s reach, cannot occur: no admitted block is that long,
            // [`natemu_abi::execution::cache::MAX_BLOCK_BYTES`].)
            let w0 = self.word(test);
            let near = w0 & 0xfff8_001f == 0xb7f8_0002 && to(test, w0 >> 5 & 0x3fff, 14) == s.native_offset && back == test + 4;
            let base = self.word(back) & 31;
            if !near || self.word(back) & !31 != 0x2a01_03e0 || base == 28 || base == 31 {
                return fail("its test or its base move");
            }
            if self.branches_into(start, back).next().is_some() {
                return fail("a branch enters it");
            }
            if overlaps_relocation32(start..back + 4, self.c.relocs) {
                return fail("a relocation lies in it");
            }
            if self.instruction(start).map(|i| i.0) != Some(g) || self.instruction(back).map(|i| i.0) != Some(g) {
                return fail("it is not in its instruction");
            }
            let mut through = self.c.accesses.iter().filter(|a| a.guest_offset == g && a.segment == op.segment).peekable();
            if through.peek().is_none() || through.any(|a| a.native_offset <= back) {
                return fail("an access through the segment is not after it");
            }
            // The FILD m64, FISTP m64 pair: the resident keeps the integer for a rerun at this check from the marked store's
            // register (`bank_at`), which must therefore hold at the check what the store writes.
            if let Some(store) = self.c.accesses.iter().find(|a| a.guest_offset == g && a.kind == object::kind::STORE && a.guest_offset2 == object::X87_INT64)
                && (store.native_offset <= back || self.written(1 << store.data_reg[0], back, store.native_offset))
            {
                return fail("the kept integer's register is written between it and the store");
            }
            self.segment_view_reads.push(start + 24);
            self.claim(s.native_offset);
        }
        if self.c.stubs.iter().filter(|s| s.kind == SEGMENT).count() != operands.iter().filter(|o| o.limited).count() {
            return Err("an operand check without a limited FS or GS operand".into());
        }
        self.segment_view_reads.sort_unstable();
        Ok(())
    }

    /// M2 checkpoint C: every access of an instruction through FS or GS adds that segment's base. The base is the operand
    /// check's (`mov wB, w1`, a limited segment) or the load of the segment's field in the guest-state page (`ldr wB,
    /// [x28, #field]`, otherwise), inside the instruction; the access's address register must be computed from it: the
    /// result of a chain of `add`/`sub` (register, extended register or immediate) and register moves that starts at wB,
    /// with no other write of a register of the chain on the way. A translation that forgot the base (FEX's MOVD and
    /// MOVNT stores did: the access then went to the flat address) has no such chain, whatever its instruction.
    fn segment_bases(&self, operands: &[SegmentOperand]) -> Result<(), String> {
        use object::kind::*;
        for op in operands {
            let g = op.offset;
            let fail = |what: &str| Err(format!("access through {} of the instruction at guest offset {g:#x}: {what}", if op.segment == object::SEG_FS { "FS" } else { "GS" }));
            let Some(first) = self.c.pcs.iter().position(|p| p.guest_offset == g) else {
                return fail("the instruction has no code");
            };
            let start = self.c.pcs[first].native_offset;
            let end = self.c.pcs[first..].iter().find(|p| p.guest_offset != g).map(|p| p.native_offset)
                .unwrap_or_else(|| self.c.stubs.first().map_or(self.c.code.len() as u32, |s| s.native_offset));
            let field = if op.segment == object::SEG_FS { frame::FS_BASE } else { frame::GS_BASE };
            let check = self.c.stubs.iter().find(|s| s.kind == SEGMENT && s.guest_offset == g).map(|s| s.imm as u32);
            let mut chain = 0u32;
            let mut accesses = self.c.accesses.iter().filter(|a| a.guest_offset == g && a.segment == op.segment).peekable();
            if accesses.peek().is_none() {
                return fail("none is recorded");
            }
            for at in (start..end).step_by(4) {
                let w = self.word(at);
                if let Some(a) = accesses.peek().filter(|a| a.native_offset == at) {
                    let index = if a.index_reg == object::NO_REG { 0 } else { 1 << a.index_reg };
                    if a.base_reg >= 31 || chain & (1 << a.base_reg | index) == 0 {
                        return fail("its address does not come from the segment's base");
                    }
                    accesses.next();
                }
                let (rd, rn, rm) = (w & 31, w >> 5 & 31, w >> 16 & 31);
                let from = |r: u32| r < 31 && chain & 1 << r != 0;
                let source = if op.limited { check == Some(at) } else { w & !31 == 0xb940_0380 | field / 4 << 10 };
                let derived = source
                    || (w & 0x1f20_0000 == 0x0b00_0000 && (from(rn) || from(rm))) // add/sub, shifted register
                    || (w & 0x1fe0_0000 == 0x0b20_0000 && (from(rn) || from(rm))) // add/sub, extended register
                    || (w & 0x1f00_0000 == 0x1100_0000 && from(rn)) // add/sub, immediate
                    || (w & 0x1f20_0000 == 0x0a00_0000 && rn == 31 && from(rm)); // mov (orr with the zero register)
                let writes = self.insn(at).map_or(u32::MAX, |i| i.writes());
                chain = if derived && rd < 31 { chain & !writes | 1 << rd } else { chain & !writes };
            }
            if accesses.next().is_some() {
                return fail("one is outside the instruction's code");
            }
        }
        Ok(())
    }

    fn new(c: &'a Code<'a>) -> Result<Self, String> {
        let words = c.code.len() / 4;
        let mut insns = vec![None; words];
        for (i, slot) in insns.iter_mut().enumerate().skip(ENTRY_OFFSET as usize / 4) {
            if c.literals.iter().any(|l| l.contains(&(i * 4))) {
                continue;
            }
            let w = u32::from_le_bytes(c.code[i * 4..i * 4 + 4].try_into().unwrap());
            let pad = c.bits32
                && w == natemu_abi::execution::NOP
                && c.relocs.iter().any(|r| r.kind == object::RELOC_GUEST_RIP_MOVE && r.offset as usize + 8 == i * 4);
            *slot = Some(match decode(w, i as u32 * 4) {
                _ if pad => Insn::MovePad,
                Some(insn) => insn,
                None if !c.shape && screened(w) => Insn::Other,
                None => {
                    return Err(format!(
                        "unrecognised instruction {w:#010x} at {:#x}",
                        i * 4
                    ));
                }
            });
        }
        // The computed exit's branch placeholder, relocated by the resident (at most one: a block ends at one branch).
        let mut dispatch = None;
        for r in c
            .relocs
            .iter()
            .filter(|r| r.kind == object::RELOC_DISPATCH_BRANCH)
        {
            let at = r.offset as usize;
            let placeholder = at % 4 == 0
                && c.code.get(at..at + 4) == Some(&DISPATCH_PLACEHOLDER.to_le_bytes()[..]);
            if dispatch.is_some() || !placeholder || insns.get(at / 4).is_none_or(|i| i.is_none()) {
                return Err(format!(
                    "dispatch branch at {at:#x}: not a single placeholder `b .` in the code"
                ));
            }
            insns[at / 4] = Some(Insn::Dispatch);
            dispatch = Some(r.offset);
        }
        // Probing split stubs' branches to their shared probes (object validation checked each stub's value).
        for r in c.relocs.iter().filter(|r| r.kind == object::RELOC_PROBE_BRANCH) {
            let at = r.offset as usize;
            if at % 4 != 0
                || c.code.get(at..at + 4) != Some(&DISPATCH_PLACEHOLDER.to_le_bytes()[..])
                || !matches!(insns.get(at / 4), Some(Some(Insn::B(_))))
            {
                return Err(format!("probe branch at {at:#x}: not a placeholder `b .` in the code"));
            }
            insns[at / 4] = Some(Insn::ProbeBranch);
        }
        let all: Vec<(u32, i64)> = (insns.iter().enumerate())
            .filter_map(|(i, x)| match x {
                Some(Insn::B(t) | Insn::Cond(t)) => Some((i as u32 * 4, *t)),
                _ => None,
            })
            .collect();
        let mut branches = Vec::new();
        let mut back = Vec::new();
        for (at, t) in all {
            match u32::try_from(t) {
                Ok(t) if t > at && insns.get(t as usize / 4).is_some_and(|x| x.is_some()) => {
                    branches.push((at, t))
                }
                // Perf chunk 26: a backward branch only as a bulk REP STOS loop's back edge (checked below).
                Ok(t) if t < at && t >= ENTRY_OFFSET => back.push((at, t)),
                _ => {
                    return Err(format!(
                        "branch at {at:#x} to {t:#x}: not forward to an instruction of the code region"
                    ));
                }
            }
        }
        branches.sort_by_key(|&(_, t)| t);
        let mut v = Verifier {
            c,
            insns,
            branches,
            moves: Vec::new(),
            checks: Vec::new(),
            claimed: vec![false; c.stubs.len()],
            links: Vec::new(),
            dispatch,
            bulk: Vec::new(),
            aligned_before: Vec::new(),
            segment_view_reads: Vec::new(),
        };
        // A probing split stub's branch back to its store, and the branch back from the run's end of a top x87 run test:
        // branches like the others for every rule on targets.
        let (mut returns, back): (Vec<_>, Vec<_>) = back.into_iter().partition(|&(at, t)| v.run_end_return(at, t));
        // A probing split stub's way back from its shared probe: from the stub's `b` to its `adr`'s target.
        for (i, st) in c.stubs.iter().enumerate() {
            let at = st.native_offset + 4;
            if matches!(v.insn(at), Some(Insn::ProbeBranch)) {
                let t = natemu_abi::execution::probe::adr_x0_target(v.word(st.native_offset), u64::from(st.native_offset))
                    .and_then(|t| u32::try_from(t).ok())
                    .filter(|&t| v.probe_return(at, t))
                    .ok_or_else(|| format!("probing split stub {i} at {:#x}: its way back", st.native_offset))?;
                returns.push((at, t));
            }
        }
        // (No other backward branch into the run's end: its return is reached from the test alone.)
        if let Some(e) = v.run_test()
            && returns.iter().chain(&back).any(|&(at, t)| at != e + 36 && t >= e && t <= e + 36)
        {
            return Err(format!("backward branch into the x87 run's end at {e:#x}"));
        }
        v.branches.extend(returns);
        for (at, t) in back {
            let words = v.bulk_loop(at, t).or_else(|| v.bulk_movs_loop(at, t)).ok_or_else(|| format!(
                "branch at {at:#x} to {t:#x}: not forward to an instruction of the code region"
            ))?;
            v.bulk.push(words);
        }
        v.branches.sort_by_key(|&(_, t)| t);
        for r in c
            .relocs
            .iter()
            .filter(|r| r.kind == object::RELOC_GUEST_RIP_MOVE)
        {
            let (words, value) = v
                .rip_move(r.offset, r.register)
                .ok_or_else(|| format!("guest-RIP move at {:#x}", r.offset))?;
            // A literal exit's move (into x0, right before `svc #LINK`) holds its target, which `exits` checks.
            let exit = r.register == 0
                && v.c.code.get(words.end as usize..words.end as usize + 4)
                    == Some(&SVC_LINK.to_le_bytes()[..]);
            // x0 is FEX's scratch: fexc moves a guest RIP into it only for a literal exit.
            if r.register == 0 && !exit {
                return Err(format!(
                    "guest-RIP move at {:#x} into x0 is not a literal exit",
                    r.offset
                ));
            }
            if value != r.value || !(exit || v.displacement_ok(r.offset, value)) {
                return Err(format!(
                    "guest-RIP move at {:#x}: value {value:#x} is not the instruction's RIP-relative address",
                    r.offset
                ));
            }
            if !exit {
                v.moves.push((words, r.register, value));
            }
        }
        Ok(v)
    }

    /// Gather plan (2.5), code shape: fexc's inline gather exactly. Every translated instruction that is a gather fexc may
    /// compile inline ([`x86_gather`]) has as many access sites as its form has elements, all of them element loads
    /// ([`object::GATHER_ELEMENT`]) of the element's width into a vector register, and no element load is anything else's.
    /// Each is FexcGatherLoad's three words `umov w0|x0, vM.s|d[i]; tbz w0|x0, #31|#63, +8; ldr s|d<t>, [xA]` (the `tbz` bit
    /// the element's sign bit, skipping exactly the `ldr`, which is the site as recorded), and no branch enters the `tbz` or
    /// the `ldr`. (Which lane and mask register the `umov` reads are temporaries' and cannot be named here: fexc's tests
    /// and the system family's gathers cover them.)
    fn gathers(&self) -> Result<(), String> {
        let mut starts: Vec<u32> = self.c.pcs.iter().map(|p| p.guest_offset).filter(|&g| g < self.c.guest_bytes).collect();
        starts.dedup();
        for g in starts {
            let fail = |what: &str| Err(format!("gather at guest offset {g:#x}: {what}"));
            let sites: Vec<&Site> = self.c.accesses.iter().filter(|s| s.guest_offset == g).collect();
            let Some((ew, n)) = x86_gather(self.guest_insn(g)) else {
                if sites.iter().any(|s| object::gather_element(s)) {
                    return fail("an element load in an instruction that is no inline gather");
                }
                continue;
            };
            if sites.len() != n as usize || !sites.iter().all(|s| object::gather_element(s)) {
                return fail(&format!("{} access sites, not its {n} element loads", sites.len()));
            }
            let (umov, tbz, ldr) = if ew == 8 { ((0xffef_fc1f, 0x4e08_3c00), 0xb6f8_0040, 0xfd40_0000) } else { ((0xffe7_fc1f, 0x0e04_3c00), 0x36f8_0040, 0xbd40_0000) };
            for s in sites {
                let at = s.native_offset;
                let w = self.word(at);
                let ok = at >= ENTRY_OFFSET + 8
                    && (s.width, s.data_class, s.imm, s.index_reg) == (ew, object::CLASS_FPR, 0, object::NO_REG)
                    && self.word(at - 8) & umov.0 == umov.1
                    && self.word(at - 4) == tbz
                    && w & 0xffff_fc00 == ldr
                    && (w >> 5 & 31, w & 31) == (u32::from(s.base_reg), u32::from(s.data_reg[0]))
                    && self.instruction(at - 8).is_some_and(|(h, _)| h == g)
                    && self.branches_into(at - 8, at).next().is_none();
                if !ok {
                    return fail(&format!("its element load at {at:#x} is not `umov w0|x0; tbz w0|x0, #31|#63, +8; ldr s|d` as recorded"));
                }
            }
        }
        Ok(())
    }

    /// Perf chunk 26: the backward branch at `at` to `to` is the back edge of a bulk REP STOS loop (fexc_sites.inc
    /// `FexcRepStos`): the words before and after it are exactly that sequence for the element width of the REP STOS (F3,
    /// 64-bit addressing) holding them, its `stp` is that instruction's recorded site, and no branch from outside enters it
    /// (its own skips go to its end). Returns the sequence's native range. So its count x1 is min(RCX's whole 16-byte chunks,
    /// the chunks before RDI's page end) <= 256, decremented once a round and written by nothing else: the loop ends, and each
    /// store stays inside RDI's page (no split check is needed: at a fault RCX/RDI are an iteration boundary's).
    fn bulk_loop(&self, at: u32, to: u32) -> Option<Range<u32>> {
        let (g, _) = self.instruction(at)?;
        let words = bulk_words(x86_rep_stos(self.guest_insn(g))?);
        let n = words.len() as u32 * 4;
        let start = (at + 4).checked_sub(n).filter(|&s| s >= ENTRY_OFFSET)?;
        let (end, stp) = (start + n, start + n - 20);
        let exact = words.iter().enumerate().all(|(i, &(w, to))| {
            let o = start + 4 * i as u32;
            self.word(o)
                == match to {
                    Bulk::Word => w,
                    Bulk::Skip => with_offset(w, end as i64 - o as i64),
                    Bulk::Back => with_offset(w, stp as i64 - o as i64),
                }
        });
        let site = self
            .c
            .accesses
            .binary_search_by_key(&stp, |s| s.native_offset)
            .ok()
            .map(|i| &self.c.accesses[i]);
        // One instruction holds every word; a branch into the sequence comes from inside it and goes to its end.
        let inside = (start..end)
            .step_by(4)
            .all(|o| self.instruction(o).is_some_and(|(h, _)| h == g));
        let sealed = self
            .branches
            .iter()
            .all(|&(src, t)| !(start < t && t < end) && !(start <= src && src < end && t != end));
        (exact
            && to == stp
            && inside
            && sealed
            && site.is_some_and(|s| s.kind == object::kind::STORE_PAIR && s.guest_offset == g))
        .then_some(start..end)
    }

    /// Bulk REP plan (section 14): the backward branch at `at` to `to` is one of the two back edges of a bulk REP MOVS
    /// sequence (fexc_sites.inc `FexcRepMovs`) in a 64-bit object: the words around it are exactly that sequence for the width
    /// of the REP MOVSB/MOVSQ holding them ([`x86_rep_movs`]), its four accesses that instruction's recorded sites (the `ldp` a
    /// 16-byte load pair and the element `ldr` a load, both read; the `stp` and the element `str` stores), the back edges to
    /// the `ldp` and to the element `ldr`, and no branch from outside enters it (its forward branches go to its end or to its
    /// element loop's entry). Returns the sequence's native range. So, RCX being below 2^63 there (its `tbnz x7, #63`; the
    /// branch-free mins are signed), x1 is min(RCX, the whole elements before RDI's page end, those before RSI's) <= 4096 / w, and the rounds' count x0 = x1's rounds, each decremented once a pass and written by
    /// nothing else: the loops end, every access stays inside its pointer's page (no split check), and the rounds run only
    /// past the alignment and overlap skips, word for word.
    fn bulk_movs_loop(&self, at: u32, to: u32) -> Option<Range<u32>> {
        if self.c.bits32 {
            return None;
        }
        let (g, _) = self.instruction(at)?;
        let (words, b, e, l) = bulk_movs_words(x86_rep_movs(self.guest_insn(g))?);
        let n = words.len() as u32 * 4;
        // `at` is the rounds' back edge (index e - 1) or the element loop's (the last word).
        [(e - 1) as u32, words.len() as u32 - 1].into_iter().find_map(|k| {
            let start = at.checked_sub(4 * k).filter(|&s| s >= ENTRY_OFFSET)?;
            let (end, rounds, entry, element) = (start + n, start + 4 * b as u32, start + 4 * e as u32, start + 4 * l as u32);
            let exact = words.iter().enumerate().all(|(i, &(w, kind))| {
                let o = start + 4 * i as u32;
                self.word(o)
                    == match kind {
                        Movs::Word => w,
                        Movs::ToEnd => with_offset(w, end as i64 - o as i64),
                        Movs::ToElements => with_offset(w, entry as i64 - o as i64),
                        Movs::BackRounds => with_offset(w, rounds as i64 - o as i64),
                        Movs::BackElement => with_offset(w, element as i64 - o as i64),
                    }
            });
            let site = |o: u32, kind: u8, width: u8, intent: u8| {
                self.c
                    .accesses
                    .binary_search_by_key(&o, |s| s.native_offset)
                    .ok()
                    .map(|i| &self.c.accesses[i])
                    .is_some_and(|s| {
                        (s.guest_offset, s.kind, s.width, s.intent, s.data_class) == (g, kind, width, intent, object::CLASS_GPR)
                    })
            };
            let width = if words[l].0 & 0xffc0_0000 == 0x3940_0000 { 1 } else { 8 };
            let inside = (start..end)
                .step_by(4)
                .all(|o| self.instruction(o).is_some_and(|(h, _)| h == g));
            let sealed = self.branches.iter().all(|&(src, t)| {
                let from_inside = start <= src && src < end;
                if from_inside { t == end || t == entry } else { !(start < t && t < end) }
            });
            (exact
                && to == if at == start + 4 * (e as u32 - 1) { rounds } else { element }
                && inside
                && sealed
                && site(rounds, object::kind::LOAD_PAIR, 16, object::INTENT_READ)
                && site(rounds + 4, object::kind::STORE_PAIR, 16, object::INTENT_WRITE)
                && site(element, object::kind::LOAD, width, object::INTENT_READ)
                && site(element + 4, object::kind::STORE, width, object::INTENT_WRITE))
            .then_some(start..end)
        })
    }

    /// Code shape: how each stub is reached (exactly one branch).
    fn stub_checks(&mut self) -> Result<(), String> {
        for s in self.c.stubs {
            let reached: Vec<u32> = self
                .branches_into(s.native_offset - 1, s.native_offset)
                .map(|(src, _)| src)
                .collect();
            let [src] = reached[..] else {
                return Err(format!(
                    "stub at {:#x} is reached by {} branches",
                    s.native_offset,
                    reached.len()
                ));
            };
            let check = match self.insn(src) {
                Some(Insn::Cond(_)) => Check {
                    start: src,
                    end: src,
                    cond: Some(normalize(self.word(src))),
                },
                _ if src >= 4
                    && matches!(self.insn(src - 4), Some(Insn::Cond(t)) if t == src as i64 + 4)
                    && is_cbtb(self.word(src - 4)) =>
                {
                    Check {
                        start: src - 4,
                        end: src,
                        cond: Some(normalize(self.word(src - 4)) ^ 1 << 24),
                    }
                }
                _ => Check {
                    start: src,
                    end: src,
                    cond: None,
                },
            };
            self.checks.push(check);
        }
        Ok(())
    }

    fn word(&self, at: u32) -> u32 {
        u32::from_le_bytes(
            self.c.code[at as usize..at as usize + 4]
                .try_into()
                .unwrap(),
        )
    }
    fn insn(&self, at: u32) -> Option<Insn> {
        self.insns.get(at as usize / 4).copied().flatten()
    }
    fn branches_into(&self, after: u32, to: u32) -> impl Iterator<Item = (u32, u32)> + '_ {
        let from = self.branches.partition_point(|&(_, t)| t <= after);
        self.branches[from..]
            .iter()
            .copied()
            .take_while(move |&(_, t)| t <= to)
    }
    /// No branch enters `(start, to]` from before the check `start..=end` (the far form's own skip excepted).
    fn sealed(&self, start: u32, end: u32, to: u32) -> bool {
        self.branches_into(start, to)
            .all(|(src, t)| src > end || (src == start && start + 4 == end && t == end + 4))
    }
    fn written(&self, regs: u32, from: u32, to: u32) -> bool {
        (from..to)
            .step_by(4)
            .any(|at| self.insn(at).is_some_and(|i| i.writes() & regs != 0))
    }
    /// The last word in `[from, before)` that writes `reg`.
    fn last_write(&self, reg: u8, from: u32, before: u32) -> Option<u32> {
        (from..before)
            .step_by(4)
            .rev()
            .find(|&at| self.insn(at).is_some_and(|i| i.writes() & 1 << reg != 0))
    }
    /// Guest offset of the instruction holding native offset `at`, and that instruction's native start.
    fn instruction(&self, at: u32) -> Option<(u32, u32)> {
        let i = self.c.pcs.partition_point(|p| p.native_offset <= at);
        let p = self.c.pcs.get(i.checked_sub(1)?)?;
        Some((p.guest_offset, p.native_offset))
    }
    /// The guest bytes of the instruction at guest offset `g`.
    /// The word at `at` is the casal/caspal site of a LOCK CMPXCHG, CMPXCHG8B or CMPXCHG16B (the guest bytes say so).
    fn lock_cas(&self, at: u32) -> bool {
        let site = self
            .c
            .accesses
            .binary_search_by_key(&at, |s| s.native_offset)
            .ok()
            .map(|i| &self.c.accesses[i]);
        matches!(
            self.insn(at),
            Some(Insn::Atomic {
                op: AtomicOp::Cas | AtomicOp::Casp,
                ..
            })
        ) && site.is_some_and(|s| {
            s.kind == object::kind::ATOMIC && lock_cmpxchg(self.guest_insn(s.guest_offset))
        })
    }
    fn guest_insn(&self, g: u32) -> &[u8] {
        let end = super::next_boundary(self.c.pcs, g, self.c.guest_bytes);
        &self.c.source[g as usize..end as usize]
    }
    /// A cbz/cbnz/tbz/tbnz at `at` (or the far form `inverse +8; b target`), normalized, and its target.
    fn cond_branch(&self, at: u32) -> Option<(u32, i64)> {
        let Some(Insn::Cond(t)) = self.insn(at) else {
            return None;
        };
        let w = self.word(at);
        if !is_cbtb(w) {
            return None;
        }
        match self.insn(at + 4) {
            Some(Insn::B(far)) if t == at as i64 + 8 => Some((normalize(w) ^ 1 << 24, far)),
            _ => Some((normalize(w), t)),
        }
    }
    /// The words at `at` are a link point (a guest-RIP move into x0, then `svc #LINK`) that no branch enters after its
    /// first word: its target and the `svc`'s offset.
    fn link_point(&self, at: u32) -> Option<(u64, u32)> {
        let r = self.c.relocs.iter().find(|r| {
            r.kind == object::RELOC_GUEST_RIP_MOVE && r.offset == at && r.register == 0
        })?;
        let (words, value) = self.rip_move(at, 0)?;
        (value == r.value && self.word(words.end) == SVC_LINK && self.sealed(at, at, words.end))
            .then_some((value, words.end))
    }
    /// The word at `at` is a 128/64 division's `svc`.
    fn division_svc(&self, at: u32) -> bool {
        matches!(self.insn(at), Some(Insn::Svc(svc::DIV_U | svc::DIV_S)))
    }

    /// The guest-RIP move at `at` building `reg`: its words and value. 64-bit code: a [`Self::move_sequence`]; 32-bit code:
    /// fexc's one form alone, its `nop` included (`movz w; movk w, lsl #16; nop`, [`natemu_abi::execution::move32_value`]:
    /// the three words the resident's position-independent rewrite needs).
    fn rip_move(&self, at: u32, reg: u8) -> Option<(Range<u32>, u64)> {
        if !self.c.bits32 {
            return self.move_sequence(at, reg);
        }
        let mut words = [0; 3];
        for (k, w) in words.iter_mut().enumerate() {
            let o = at + 4 * k as u32;
            *w = self.insn(o).and(Some(self.word(o)))?;
        }
        natemu_abi::execution::move32_value(&words, reg.into()).map(|v| (at..at + 12, v))
    }
    /// The movz/movn/`orr #bitmask, wzr` (+ movk) sequence at `at` building `reg`: its words and value.
    fn move_sequence(&self, at: u32, reg: u8) -> Option<(Range<u32>, u64)> {
        let w = self.insn(at).and(Some(self.word(at)))?;
        let sf = w >> 31;
        let mask = if sf == 1 { u64::MAX } else { 0xffff_ffff };
        let part = |w: u32| ((w >> 5 & 0xffff) as u64, 16 * (w >> 21 & 3));
        let (imm, sh) = part(w);
        let mut value = match w & 0x7f80_0000 {
            _ if w & 31 != reg as u32 => return None,
            0x1280_0000 => !(imm << sh) & mask, // movn
            0x5280_0000 => imm << sh,           // movz
            0x3200_0000 if w >> 5 & 31 == 31 => bitmask(w)? & mask, // orr xD, xzr, #bitmask
            _ => return None,
        };
        let mut end = at + 4;
        while self.insn(end).is_some()
            && self.word(end) & 0xff80_001f == (0x7280_0000 | sf << 31 | reg as u32)
        {
            let (imm, sh) = part(self.word(end));
            value = (value & !(0xffff << sh) | imm << sh) & mask;
            end += 4;
        }
        Some((at..end, value))
    }
    /// A guest-RIP move's value is its instruction's end (a pushed return address) or the end plus a 32-bit
    /// displacement whose bytes are in the instruction; or, in the first instruction of 32-bit code, the block's own RIP
    /// (M2 reuse: a FISTP m64 that starts its unit compares its RIP with the kept integer's, `X87_INT_RIP`).
    fn displacement_ok(&self, at: u32, value: u64) -> bool {
        let Some((g, _)) = self.instruction(at) else {
            return false;
        };
        let insn = self.guest_insn(g);
        let disp = value.wrapping_sub(self.c.address(g as u64 + insn.len() as u64)) as i64;
        disp == 0
            || i32::try_from(disp).is_ok_and(|d| insn.windows(4).any(|b| b == d.to_le_bytes()))
            || (self.c.bits32 && g == 0 && value == self.c.address(0))
    }

    /// The per-word rules of the linear pass, then the site words.
    fn words(&self) -> Result<(), String> {
        let fail = |at: u32, what: &str| Err(format!("{what} at {at:#x}"));
        let shape = self.c.shape;
        let site = |at: u32| {
            self.c
                .accesses
                .binary_search_by_key(&at, |s| s.native_offset)
                .is_ok()
        };
        // M2 task 4, the second entry: a block that begins with the test for an open x87 run (`ldrb w0, [x28, #X87_RUN];
        // cbnz w0, E`; the resident's linker enters it behind those two words from another such block,
        // `runtime::cache::link_entry`) is 32-bit code, has at E exactly the run's end and the branch back to its third
        // word ([`Self::run_end_return`]), and never opens a run: no store to the run byte but of zero. So behind the test
        // no run is open, to every exit, which is what a link that skips the next block's test relies on.
        let run_tested = self.run_test().is_some();
        if shape && run_tested && !self.c.bits32 {
            return fail(ENTRY_OFFSET, "x87 run test at the top of 64-bit code");
        }
        // The run's end's return as admitted is to the block's third word (the resident points it at the hot copy's when
        // it lays the block out): exactly that word, also with the code shape off.
        if let Some(e) = self.run_test()
            && self.word(e + 36) != 0x1400_0000 | (ENTRY_OFFSET + 8).wrapping_sub(e + 36) / 4 & 0x3ff_ffff
        {
            return fail(e + 36, "x87 run test's end does not return to the block's third word");
        }
        // (Its return is among the branches only if `run_end_return` held.)
        if shape && run_tested && !self.branches.iter().any(|&(_, t)| t == ENTRY_OFFSET + 8) {
            return fail(ENTRY_OFFSET, "x87 run test at the block's top without the run's end and its return");
        }
        for at in (ENTRY_OFFSET..self.c.code.len() as u32).step_by(4) {
            let Some(i) = self.insn(at) else { continue };
            let (w, writes) = (self.word(at), i.writes());
            // (strb wN, [x28, #X87_RUN], N not the zero register: a run opened.)
            if shape && run_tested && w & !31 == RUN_END[8] & !31 && w & 31 != 31 {
                return fail(at, "x87 run opened in a block that begins with the test for one");
            }
            if writes & 1 << 28 != 0 {
                return fail(at, "write to x28");
            }
            if shape && writes & 1 << 31 != 0 {
                return fail(at, "write to sp");
            }
            if writes & 1 << PRE_NZCV_REG != 0 && w != MRS_PRE_NZCV {
                return fail(at, "write to x25 other than the pre-NZCV save");
            }
            match i {
                Insn::Mem(m) => match m.addr.base {
                    28 => self.frame_access(m, at)?,
                    0 if shape && self.x87_slot(m, at) => {}
                    1 if self.segment_view_reads.binary_search(&at).is_ok() => {}
                    SP if shape => return fail(at, "stack access"),
                    _ if shape && !site(at) => return fail(at, "unrecorded memory access"),
                    _ => {}
                },
                Insn::Swap { rn: 28, .. } => return fail(at, "frame access by a swap"),
                // With Rt = XZR, SWPAL is not an acquire (the architecture drops the load-acquire when the result is discarded).
                Insn::Swap { rt: 31, .. } if shape => {
                    return fail(
                        at,
                        "swap discards its result (Rt is XZR): no acquire semantics",
                    );
                }
                Insn::Swap { .. } if shape && !site(at) => {
                    return fail(at, "swap that is not a swap site");
                }
                Insn::Atomic { rn: 28, .. } => return fail(at, "frame access by an atomic"),
                Insn::Probe(28) => return fail(at, "frame access by a probe"),
                // (The probes are the resident's: generated code has none.)
                Insn::Probe(_) => return fail(at, "page probe outside a split stub"),
                Insn::ProbeBranch if self.probing(at).is_none_or(|(_, k)| k != 4) => {
                    return fail(at, "probe branch outside a split stub");
                }
                // A fetch-and-op or CAS whose result is XZR is not an acquire (as for SWPAL); FEX never emits one.
                Insn::Atomic {
                    op: AtomicOp::Fetch,
                    rt: 31,
                    ..
                }
                | Insn::Atomic {
                    op: AtomicOp::Cas,
                    rs: 31,
                    ..
                } if shape => {
                    return fail(at, "atomic discards its result (XZR): no acquire semantics");
                }
                Insn::Atomic { .. } if shape && !site(at) => {
                    return fail(at, "atomic that is not an atomic site");
                }
                // A barrier only right before the CAS of a LOCK CMPXCHG*; and (code shape) every such CAS has it.
                Insn::DmbIsh if !self.lock_cas(at + 4) => {
                    return fail(at, "dmb ish that is not right before a LOCK CMPXCHG's CAS");
                }
                Insn::Atomic {
                    op: AtomicOp::Cas | AtomicOp::Casp,
                    ..
                } if shape
                    && self.lock_cas(at)
                    && !matches!(self.insn(at - 4), Some(Insn::DmbIsh)) =>
                {
                    return fail(at, "LOCK CMPXCHG's CAS without the dmb ish before it");
                }
                Insn::Literal { .. } if shape => return fail(at, "literal load"),
                // Division `svc`s are checked below, link points' by `exits`.
                Insn::Svc(svc::LINK | svc::DIV_U | svc::DIV_S | svc::X87 | svc::STRING) => {}
                Insn::Svc(_) => return fail(at, "svc"),
                Insn::Brk
                    if shape
                        && self
                            .c
                            .stubs
                            .binary_search_by_key(&at, |s| s.native_offset)
                            .is_err() =>
                {
                    return fail(at, "brk");
                }
                // Perf chunk 15: the virtual count, read at EL0 since CNTKCTL_EL1.EL0VCTEN (the value RDTSC gives the guest at
                // any CPL without CR4.TSD, scaled), only inside a CPL-0 block's RDTSC.
                Insn::Counter(_) | Insn::Isb
                    if !(self.c.cpl0
                        && self
                            .instruction(at)
                            .is_some_and(|(g, _)| self.guest_insn(g) == [0x0f, 0x31])) =>
                {
                    if matches!(i, Insn::Counter(_)) {
                        return fail(at, "counter read outside a CPL-0 RDTSC");
                    }
                    if shape {
                        return Err(format!("unrecognised instruction {w:#010x} at {at:#x}"));
                    }
                }
                _ => {}
            }
            if i.falls_through() && self.insn(at + 4).is_none() {
                return fail(at, "fall-through into data");
            }
            // The 128/64 division (`svc #DIV_U` for div, `#DIV_S` for idiv) only inside a 64-bit div/idiv.
            if let Insn::Svc(imm @ (svc::DIV_U | svc::DIV_S)) = i {
                let divide = self
                    .instruction(at)
                    .and_then(|(g, _)| x86_divide(self.guest_insn(g)));
                if divide != Some((imm == svc::DIV_S, 64)) {
                    return fail(
                        at,
                        "division svc outside a 64-bit div/idiv of its signedness",
                    );
                }
            }
            // I6b: a helper `svc` only inside its x86 instruction, right after the `movz w0` of the instruction's function code
            // (x87) or control word (PCMPxSTRx), in the same instruction, with no branch to the `svc`.
            if let Insn::Svc(imm @ (svc::X87 | svc::STRING)) = i {
                let code = self.instruction(at).and_then(|(g, _)| {
                    let insn = self.guest_insn(g);
                    if imm == svc::X87 {
                        x86_x87_helper(insn)
                    } else {
                        x86_string_compare(insn)
                    }
                });
                let movz = at >= ENTRY_OFFSET + 4
                    && self.instruction(at - 4) == self.instruction(at)
                    && self.sealed(at - 4, at - 4, at);
                match code {
                    Some(c) if movz && self.word(at - 4) == MOVZ_W0 | c << 5 => {}
                    Some(_) => {
                        return fail(
                            at,
                            "helper svc without its instruction's movz w0 right before it",
                        );
                    }
                    None => return fail(at, "helper svc outside its x86 instruction"),
                }
            }
        }
        for s in self.c.stubs {
            if self
                .insn(s.native_offset - 4)
                .is_some_and(|i| i.falls_through())
            {
                return fail(s.native_offset, "fall-through into a stub");
            }
        }
        // Each access site lies inside its guest instruction, and its word is the access it records.
        for s in self.c.accesses {
            let at = s.native_offset;
            let lo = s.insn_native_offset;
            // (The PC table's native offsets ascend: the entries in (lo, at] are one slice of it.)
            let pcs = self.c.pcs;
            let (from, to) = (pcs.partition_point(|p| p.native_offset <= lo), pcs.partition_point(|p| p.native_offset <= at));
            let inside = &pcs[from.min(to)..to];
            if !super::pc_entry(pcs, lo, s.guest_offset) || inside.iter().any(|p| p.guest_offset != s.guest_offset) {
                return Err(format!(
                    "access at {at:#x}: not inside its guest instruction"
                ));
            }
            match self.insn(at) {
                Some(Insn::Mem(m))
                    if !matches!(s.kind, object::kind::SWAP | object::kind::ATOMIC) =>
                {
                    self.site_word(m, at)?
                }
                Some(Insn::Swap { size, rs, rt, rn }) if s.kind == object::kind::SWAP => {
                    self.swap_word(size, rs, rt, rn, at)?
                }
                Some(Insn::Atomic {
                    size, rs, rt, rn, ..
                }) if s.kind == object::kind::ATOMIC => self.atomic_word(size, rs, rt, rn, at)?,
                _ => {
                    return Err(format!(
                        "access at {at:#x}: no memory access of its kind there"
                    ));
                }
            }
        }
        Ok(())
    }

    /// x28 accesses: unsigned offset (or a signed-offset pair, I8: FEX's paired YMM upper-half accesses) inside one of FEX's
    /// guest-state fields; a store only inside a writable one.
    fn frame_access(&self, m: Mem, at: u32) -> Result<(), String> {
        // i64: an unscaled access can be at a negative offset (`stur x0, [x28, #-4]`), outside every field.
        let (offset, end) = (m.addr.imm, m.addr.imm + m.size as i64);
        let inside =
            |&(field, len): &(u32, u32)| offset >= field as i64 && end <= (field + len) as i64;
        let fields: &[(u32, u32)] = if m.load {
            &frame::GUEST_STATE
        } else {
            &frame::GUEST_STATE_WRITABLE
        };
        // Perf chunk 15: a CPL-0 block's CLI and STI store IF's byte (with the code shape: only inside those instructions).
        let flag = || {
            let insn = self.instruction(at).map(|(g, _)| self.guest_insn(g));
            !m.load
                && self.c.cpl0
                && (offset, m.size) == (i64::from(frame::IF), 1)
                && (!self.c.shape || matches!(insn, Some([0xfa] | [0xfb])))
        };
        // Perf chunk 17: a CPL-0 block's SWAPGS loads and stores IA32_KERNEL_GS_BASE's field and stores GS.base (with the code
        // shape: only inside a SWAPGS), each as one 8-byte access.
        let swapgs = || {
            let insn = self.instruction(at).map(|(g, _)| self.guest_insn(g));
            let field = (offset, m.size) == (i64::from(frame::KERNEL_GS_BASE), 8)
                || (!m.load && (offset, m.size) == (i64::from(frame::GS_BASE), 8));
            self.c.cpl0 && field && (!self.c.shape || insn == Some(&[0x0f, 0x01, 0xf8][..]))
        };
        // Perf chunk 26: a CPL-0 block's MOV r64, CR2 loads CR2's field (with the code shape: only inside that instruction).
        let cr2 = || {
            let insn = self.instruction(at).map(|(g, _)| self.guest_insn(g));
            m.load
                && self.c.cpl0
                && (offset, m.size) == (i64::from(frame::CR2), 8)
                && (!self.c.shape || insn.is_some_and(x86_mov_from_cr2))
        };
        // Perf P2: a CPL-0 block's VERW loads the GDT base and limit fields (with the code shape: only inside a VERW).
        let verw = || {
            let insn = self.instruction(at).map(|(g, _)| self.guest_insn(g));
            let field = [frame::VERW_GDT_BASE, frame::VERW_GDT_LIMIT]
                .iter()
                .any(|&f| (offset, m.size) == (i64::from(f), 8));
            m.load && self.c.cpl0 && field && (!self.c.shape || insn.is_some_and(x86_verw))
        };
        // M2: 32-bit code loads the ES, CS, SS and DS bases (FEX adds a named segment's base, and ES's for every string
        // destination): `frame::LEGACY_BASES`, which the resident holds at 0 while only flat segments run.
        let legacy = self.c.bits32 && m.load && inside(&frame::LEGACY_BASES);
        // M2 checkpoint C: the base of a limited FS or GS comes from the operand check alone ([`Verifier::segment_checks`]):
        // code that loads its field would add it unchecked.
        if [frame::FS_BASE, frame::GS_BASE].iter().zip(self.c.limited).any(|(&f, limited)| limited && offset < i64::from(f) + 8 && end > i64::from(f)) {
            return Err(format!("frame access {:#010x} at {at:#x}: the base of a limited FS or GS outside its operand check", self.word(at)));
        }
        if matches!(m.form, Form::Unsigned | Form::Pair)
            && (fields.iter().any(inside)
                || legacy
                || flag()
                || (m.form == Form::Unsigned && (swapgs() || cr2() || verw())))
        {
            Ok(())
        } else {
            Err(format!(
                "frame access {:#010x} at {at:#x}: not a guest-state field",
                self.word(at)
            ))
        }
    }

    /// I6 (code shape): an x87 stack slot, FEX's `mm[I]` in the guest-state page: `add x0, x28, xI, lsl #4` right before an
    /// 8-byte access `[x0, #mm]` (I = (TOP + i) & 7, which fexc masks; the hardware keeps any address inside EL0's mappings).
    /// The index register's last write before the `add` must be `and wI, wN, #7` (fexc masks every index: TOP is guest data),
    /// and no branch from outside that span may enter after that `and` up to the access, nor any branch reach the access itself,
    /// so every path to the access masks the index and adds it
    /// (the slot stays inside the guest-state page: x28 + 0x420 + 7 * 16 + 8 <= x28 + 0x498).
    fn x87_slot(&self, m: Mem, at: u32) -> bool {
        if at < ENTRY_OFFSET + 4
            || self.word(at - 4) & 0xffe0_ffff != 0x8b00_1380
            || (m.form, m.addr.index, m.size, m.addr.imm)
                != (Form::Unsigned, None, 8, i64::from(frame::X87_MM))
        {
            return false;
        }
        let index = self.word(at - 4) >> 16 & 31;
        let mut k = at - 4;
        while k > ENTRY_OFFSET {
            k -= 4;
            if self.insn(k).is_some_and(|i| i.writes() & 1 << index != 0) {
                return self.word(k) & 0xffff_fc1f == 0x1200_0800 | index   // and wI, wN, #0x7
                    // (a branch from between the mask and the `add`, as AFP fallback's x87 NaN fix-up has, passed the mask itself;
                    // its target must not be the access, which would skip the `add`)
                    // (or a probing split stub's return, when the stub was entered from after the mask: [`Self::probe_entry`])
                    && self.branches_into(k, at).all(|(src, t)| {
                        t < at && (src > k && src < at || self.probe_entry(src).is_some_and(|e| e > k))
                    });
            }
        }
        false
    }

    /// A guest access word must be an access site recording exactly this access.
    fn site_word(&self, m: Mem, at: u32) -> Result<(), String> {
        let Ok(i) = self
            .c
            .accesses
            .binary_search_by_key(&at, |s| s.native_offset)
        else {
            return Err(format!("unrecorded memory access at {at:#x}"));
        };
        let s = &self.c.accesses[i];
        let (wb, delta) = match m.form {
            Form::Pre => (object::WB_PRE, m.delta),
            Form::Post => (object::WB_POST, m.delta),
            _ => (object::WB_NONE, 0),
        };
        let index = match m.addr.index {
            None => s.index_reg == object::NO_REG,
            Some((r, ext, shift)) => {
                (s.index_reg, s.shift) == (r, shift)
                    && (s.extend == ext || (ext == EXT_X && s.extend == object::EXT_NONE))
            }
        };
        let load = matches!(
            s.kind,
            object::kind::LOAD | object::kind::POP | object::kind::LOAD_PAIR
        );
        let class = if m.vector {
            object::CLASS_FPR
        } else {
            object::CLASS_GPR
        };
        let pair = matches!(s.kind, object::kind::LOAD_PAIR | object::kind::STORE_PAIR);
        if !(!matches!(s.kind, object::kind::SWAP | object::kind::ATOMIC)
            && index
            && load == m.load
            && s.data_class == class
            && pair == (m.form == Form::Pair)
            && (!pair || Some(s.data_reg[1]) == m.rt2)
            && (s.base_reg, s.width, s.data_reg[0]) == (m.addr.base, m.size, m.rt)
            && s.imm as i64 == m.addr.imm
            && (s.writeback, s.writeback_delta as i64) == (wb, delta))
        {
            return Err(format!(
                "access site at {at:#x} does not describe its instruction"
            ));
        }
        Ok(())
    }

    /// A swap word must be a swap site recording exactly it: base (a guest register or a computed address, never x28
    /// or sp), width, Rt and Rs; no offset, index or writeback.
    fn swap_word(&self, size: u8, rs: u8, rt: u8, rn: u8, at: u32) -> Result<(), String> {
        let s = self
            .c
            .accesses
            .binary_search_by_key(&at, |s| s.native_offset)
            .ok()
            .map(|i| &self.c.accesses[i]);
        match s {
            Some(s)
                if s.kind == object::kind::SWAP
                    && !matches!(rn, 28 | SP)
                    && (s.base_reg, s.width, s.data_reg) == (rn, size, [rt, rs])
                    && (s.imm, s.index_reg, s.writeback, s.rmw)
                        == (0, object::NO_REG, object::WB_NONE, object::RMW_NONE) =>
            {
                Ok(())
            }
            _ => Err(format!("swap at {at:#x} is not a swap site describing it")),
        }
    }

    /// An atomic word must be an atomic site recording exactly it: base (never x28 or sp), width, Rt and Rs; no offset,
    /// index or writeback; alone, or the store half of a read-modify-write (LOCK NEG's `casal` after its load).
    fn atomic_word(&self, size: u8, rs: u8, rt: u8, rn: u8, at: u32) -> Result<(), String> {
        let s = self
            .c
            .accesses
            .binary_search_by_key(&at, |s| s.native_offset)
            .ok()
            .map(|i| &self.c.accesses[i]);
        match s {
            Some(s)
                if s.kind == object::kind::ATOMIC
                    && !matches!(rn, 28 | SP)
                    && (s.base_reg, s.width, s.data_reg) == (rn, size, [rt, rs])
                    && (s.imm, s.index_reg, s.writeback)
                        == (0, object::NO_REG, object::WB_NONE)
                    && matches!(s.rmw, object::RMW_NONE | object::RMW_STORE) =>
            {
                Ok(())
            }
            _ => Err(format!(
                "atomic at {at:#x} is not an atomic site describing it"
            )),
        }
    }

    /// The probing split stub the word at `at` belongs to (its index), and the word's offset in it: 0 its `adr`, 4 its
    /// branch to its shared probe. Object validation checked the stub's words.
    fn probing(&self, at: u32) -> Option<(usize, i64)> {
        self.c.stubs.iter().enumerate().find_map(|(i, s)| {
            let k = i64::from(at) - i64::from(s.native_offset);
            (s.kind == object::kind::SPLIT && (0..=4).contains(&k) && matches!(self.insn(s.native_offset + 4), Some(Insn::ProbeBranch)))
                .then_some((i, k))
        })
    }
    /// The block's top test for an open x87 run (M2 task 4, [`natemu_abi::execution::run_test_end`]: its two words, the
    /// run's end where they branch and a `b` after it): where the run's end is. (An x87 block may begin with the same two
    /// words, the start of a run: no run's end behind them, no top test.)
    fn run_test(&self) -> Option<u32> {
        natemu_abi::execution::run_test_end(|o| self.word(o as u32), self.c.code.len() as u64).map(|e| e as u32)
    }
    /// The backward branch at `at` to `t` is the return from the run's end of the block's top test: the run's end's nine
    /// words stand where the test branches, this branch after them, back to the block's third word (the test's fall-through),
    /// and nothing else enters them. It closes no loop: only the test, before `t`, leads here.
    fn run_end_return(&self, at: u32, t: u32) -> bool {
        self.run_test().is_some_and(|e| {
            at == e + 36
                && t == ENTRY_OFFSET + 8
                && self.insn(e - 4).is_none_or(|x| !x.falls_through())
                && self.branches_into(e - 4, at).all(|(src, to)| src == ENTRY_OFFSET + 4 && to == e)
        })
    }
    /// The way back at `at` (a probing split stub's branch to its shared probe) to `t` (where the probe returns) makes no
    /// loop: `t` is a word at or before the stub's access, the stub is not fallen into, and every branch into the stub comes
    /// from before `t`, so nothing after `t` reaches the stub again.
    fn probe_return(&self, at: u32, t: u32) -> bool {
        let Some((i, 4)) = self.probing(at) else {
            return false;
        };
        let s = &self.c.stubs[i];
        // (Every branch into the stub goes to its `adr`: one to its `b` would send the probe's `br x0` wherever x0 points.)
        t % 4 == 0
            && t <= s.guest_offset2
            && self.insn(t).is_some()
            && self.insn(s.native_offset - 4).is_none_or(|x| !x.falls_through())
            && self
                .branches_into(s.native_offset - 4, at)
                .all(|(src, to)| src < t && to == s.native_offset)
    }
    /// For a probing split stub's return branch at `src`: the first word that branches into its stub (every one of them
    /// lies before the return's target, [`Self::probe_return`]).
    fn probe_entry(&self, src: u32) -> Option<u32> {
        let (i, 4) = self.probing(src)? else {
            return None;
        };
        let at = self.c.stubs[i].native_offset;
        self.branches_into(at - 4, src).map(|(from, _)| from).filter(|&from| from < at).min()
    }
    fn claim(&mut self, stub: u32) {
        if let Ok(i) = self
            .c
            .stubs
            .binary_search_by_key(&stub, |s| s.native_offset)
        {
            self.claimed[i] = true;
        }
    }
    /// The stubs linked to the access at `at`, of `kind`.
    // ponytail: linear scan per access (also `exits`' stub scan and `guest_insn`'s PC-table scan): quadratic in the
    // block, ~2 us per block on the corpora; index stubs by guest_offset2 if blocks grow to thousands of sites.
    fn linked(&self, at: u32, kind: u8) -> Vec<usize> {
        (0..self.c.stubs.len())
            .filter(|&i| self.c.stubs[i].guest_offset2 == at && self.c.stubs[i].kind == kind)
            .collect()
    }

    /// The address x1 holds after the word at `def` (one of the split check's address forms) and that form's first word.
    fn x1_address(&self, def: u32, from: u32) -> Option<(Addr, u32)> {
        let w = self.word(def);
        let (rn, rm) = ((w >> 5 & 31) as u8, (w >> 16 & 31) as u8);
        if w & 31 != 1 || w >> 31 == 0 {
            return None;
        }
        let shift = (w >> 10 & 7) as u8;
        let (addr, start) = if w & 0xff80_0000 == 0x9100_0000 || w & 0xff80_0000 == 0xd100_0000 {
            let imm = ((w >> 10 & 0xfff) << (12 * (w >> 22 & 1))) as i64;
            (
                Addr {
                    base: rn,
                    index: None,
                    imm: if w >> 30 & 1 == 1 { -imm } else { imm },
                },
                def,
            )
        } else if w & 0xffe0_0000 == 0x8b20_0000 && w >> 10 & 7 <= 4 {
            let ext = match w >> 13 & 7 {
                2 => object::EXT_UXTW,
                6 => object::EXT_SXTW,
                3 | 7 => EXT_X,
                _ => return None,
            };
            (
                Addr {
                    base: rn,
                    index: Some((rm, ext, shift)),
                    imm: 0,
                },
                def,
            )
        } else if w & 0xffe0_0000 == 0x8b00_0000 && w >> 10 & 63 <= 4 {
            (
                Addr {
                    base: rn,
                    index: Some((rm, EXT_X, (w >> 10 & 63) as u8)),
                    imm: 0,
                },
                def,
            )
        } else {
            return None;
        };
        if addr
            .index
            .is_some_and(|(r, ext, s)| r == 1 && ext == EXT_X && s == 0)
        {
            // `add x1, base, x1`: x1 is a constant built right before.
            let first = (from..def)
                .step_by(4)
                .rev()
                .take_while(|&a| self.word(a) & 0x1f80_001f == 0x1280_0001)
                .last()?;
            let (words, value) = self.move_sequence(first, 1)?;
            return (words.end == def).then_some((
                Addr {
                    base: rn,
                    index: None,
                    imm: value as i64,
                },
                first,
            ));
        }
        Some((addr, start))
    }
    /// The address register `a` holds at `at` (x1: by its last write in `[from, at)`), and where that holds from.
    fn address_in(&self, a: u8, from: u32, at: u32) -> Option<(Addr, u32)> {
        if a != 1 {
            return Some((
                Addr {
                    base: a,
                    index: None,
                    imm: 0,
                },
                at,
            ));
        }
        self.x1_address(self.last_write(1, from, at)?, from)
    }
    /// The address `addr` is unchanged from `from` to the access at `at`.
    fn kept(&self, addr: Addr, from: u32, at: u32) -> bool {
        let regs = 1 << addr.base | addr.index.map_or(0, |(r, _, _)| 1 << r);
        !self.written(regs, from, at)
    }

    /// Code shape: split and guest-RIP rules of every access site.
    fn sites(&mut self) -> Result<(), String> {
        for s in self.c.accesses {
            let at = s.native_offset;
            let fail = |what: &str| Err(format!("access at {at:#x}: {what}"));
            let lo = s.insn_native_offset;
            // A swap or LOCK atomic has no split check (an operand crossing 16 bytes faults at the atomic). A 16-byte caspal
            // (CMPXCHG16B) faults unless 16-aligned: #GP(0) at run time, never refused here.
            let (addr, size, store) = match self.insn(at) {
                Some(Insn::Mem(m)) => (m.addr, m.size, !m.load),
                Some(Insn::Swap { size, rn, .. }) | Some(Insn::Atomic { size, rn, .. }) => (
                    Addr {
                        base: rn,
                        index: None,
                        imm: 0,
                    },
                    size,
                    false,
                ),
                _ => return fail("no memory access of its kind there"),
            };
            let splits = self.linked(at, object::kind::SPLIT);
            let aligns = self.linked(at, object::kind::ALIGN);
            // Guest-RIP-addressed: the base was last written by a guest-RIP move.
            let mv = (addr.index.is_none())
                .then(|| self.last_write(addr.base, lo, at))
                .flatten()
                .and_then(|w| {
                    self.moves
                        .iter()
                        .find(|(words, r, _)| *r == addr.base && words.contains(&w))
                        .cloned()
                });
            if let Some((words, _, value)) = mv {
                let a = value.wrapping_add(addr.imm as u64);
                // (A split lock is not refused here: its atomic faults at run time and the resident stops `unsupported_atomic`,
                // the typed stop of the declared refusal, as for a computed address; X1c.)
                let crosses =
                    store && size > 1 && (a ^ a.wrapping_add(size as u64 - 1)) & 0x1000 != 0;
                // With a `b stub`, nothing may enter between the move and the access, not even from before the `b`.
                let mut sealed_to = words.start;
                // A misaligned SSE operand (I5a): `b` to its alignment stub instead of any split check.
                if let &[i] = &aligns[..] {
                    let (st, c) = (&self.c.stubs[i], self.checks[i]);
                    if !splits.is_empty()
                        || a & if size == 32 { 31 } else { 15 } == 0
                        || c.cond.is_some()
                        || c.start + 4 != at
                        || (st.base_reg, st.imm as i64) != (addr.base, addr.imm)
                    {
                        return fail(
                            "guest-RIP address: an alignment stub needs a misaligned address and `b stub` right before the access",
                        );
                    }
                    self.claim(st.native_offset);
                    if !self.sealed(words.start, c.start, at) {
                        return fail("a branch enters between its guest-RIP move and the access");
                    }
                    continue;
                } else if !aligns.is_empty() {
                    return fail("several alignment stubs");
                }
                match (crosses, &splits[..]) {
                    (false, []) => {}
                    (true, &[i]) => {
                        let (st, c) = (&self.c.stubs[i], self.checks[i]);
                        if c.cond.is_some()
                            || c.start + 4 != at
                            || (st.base_reg, st.imm as i64) != (addr.base, addr.imm)
                        {
                            return fail(
                                "guest-RIP address needs `b stub` right before the access",
                            );
                        }
                        sealed_to = c.start;
                        self.claim(st.native_offset);
                    }
                    _ => return fail(&format!("guest-RIP address {a:#x} has the wrong stubs")),
                }
                if !self.sealed(words.start, sealed_to, at) {
                    return fail("a branch enters between its guest-RIP move and the access");
                }
                continue;
            }
            // The alignment check (I5a), before the split check: `and x0, xA, #15; cbnz x0, A` on exactly the access's address,
            // then only x0/x1 written and at most the access's split check; `#31` for a 32-byte access (I8: VMOVAPS ymm and
            // the like). (Which SSE/AVX instructions need one is the producer's table, FexcSlice1::Sse/Avx: not re-derived here.)
            match &aligns[..] {
                [] => {}
                &[x] => {
                    let (st, c) = (self.c.stubs[x], self.checks[x]);
                    let a = st.base_reg as u32;
                    let and = c.start.wrapping_sub(4);
                    let split_branch = |o: u32| {
                        splits
                            .iter()
                            .any(|&i| self.checks[i].end == o && self.checks[i].start == o)
                    };
                    let ok = c.cond == Some(CBNZ_X)
                        && c.start == c.end
                        && st.imm == 0
                        && and >= lo
                        && c.end < at
                        && self.word(and)
                            == if size == 32 { 0x9240_1000 } else { 0x9240_0c00 } | a << 5
                        && self
                            .address_in(st.base_reg, lo, and)
                            .is_some_and(|(x, from)| {
                                x == addr
                                    && self.kept(addr, from, at)
                                    && self.sealed(from.min(and), c.end, at)
                            })
                        && (c.end + 4..at).step_by(4).all(|o| {
                            matches!(self.insn(o), Some(Insn::Data(d)) if d.is_none_or(|r| r < 2))
                                || split_branch(o)
                        });
                    if !ok {
                        return fail(
                            "no alignment check `and x0, xA, #15|#31; cbnz x0` on exactly its address, then only x0/x1 written",
                        );
                    }
                    self.claim(st.native_offset);
                }
                _ => return fail("several alignment stubs"),
            }
            // Split check on every store wider than a byte (perf chunk 26: but a bulk REP STOS loop's, inside its page).
            let bulk = self.bulk.iter().any(|r| r.contains(&at));
            // Stack check runs (fexc --push-run): a store of 2, 4 or 8 bytes at base + k*width, its base known 8-byte aligned
            // ([`Verifier::aligned`]), is naturally aligned and stays inside its page: it may have instead of the split check
            // its own stack alignment check, or none.
            // M2 task 4: in 32-bit code the stack slot is 4 bytes: "aligned" means 4-byte aligned ([`Self::slot`]), the check is
            // `and x0, xB, #3`, and an 8-byte store keeps its split check.
            let natural = store
                && (2..=self.slot()).contains(&u64::from(size))
                && !bulk
                && splits.is_empty()
                && addr.index.is_none()
                && addr.imm % i64::from(size) == 0
                && matches!(self.insn(at), Some(Insn::Mem(m)) if m.form != Form::Pair);
            if natural {
                if let Err(e) = self.stack_aligned(at, lo, addr.base) {
                    return fail(&e);
                }
                continue;
            }
            match (store && size > 1 && !bulk, &splits[..]) {
                (false, []) => {}
                (true, &[x]) => {
                    let (st, c) = (self.c.stubs[x], self.checks[x]);
                    let a = st.base_reg as u32;
                    let add = c.start.wrapping_sub(8);
                    let ok = c.cond == Some(TBNZ_W0_12)
                        && st.imm == 0
                        && add >= lo
                        && c.end < at
                        && self.word(add) == 0x9100_0000 | (size as u32 - 1) << 10 | a << 5
                        && self.word(add + 4) == 0xca00_0000 | a << 16
                        && self.address_in(st.base_reg, lo, add).is_some_and(|(x, from)| {
                            x == addr && self.kept(addr, from, at) && self.sealed(from.min(add), c.end, at)
                        })
                        && (c.end + 4..at).step_by(4).all(|o| matches!(self.insn(o), Some(Insn::Data(d)) if d.is_none_or(|r| r < 2)));
                    if !ok {
                        return fail(
                            "no split check `add x0, xA, #w-1; eor x0, x0, xA; tbnz w0, #12` on exactly its address, then only x0/x1 written",
                        );
                    }
                    // A stub that probes comes back to the word after its check.
                    if matches!(self.insn(st.native_offset + 4), Some(Insn::ProbeBranch))
                        && natemu_abi::execution::probe::adr_x0_target(self.word(st.native_offset), u64::from(st.native_offset))
                            != Some(u64::from(c.end) + 4)
                    {
                        return fail("its split stub's probes do not come back to the word after the check");
                    }
                    self.claim(st.native_offset);
                }
                _ => {
                    return fail(&format!(
                        "{} split stubs for a {size}-byte {}",
                        splits.len(),
                        if store { "store" } else { "load" }
                    ));
                }
            }
        }
        Ok(())
    }

    /// The `and x0, xB, #7` word of stub `i`'s stack alignment check `and x0, xB, #7; cbnz x0, K` (K that `STACK` stub, B its
    /// base register), if it has exactly that form.
    fn stack_check(&self, i: usize) -> Option<u32> {
        let (st, c) = (self.c.stubs[i], self.checks.get(i)?);
        let and = c.start.checked_sub(4)?;
        // (B is x2 or above: x0 is the check's own result and x1 may be written before the store.)
        (st.kind == object::kind::STACK
            && st.base_reg >= 2
            && st.imm == 0
            && c.cond == Some(CBNZ_X)
            && c.start == c.end
            && self.word(and) == (if self.c.bits32 { 0x9240_0400 } else { 0x9240_0800 }) | u32::from(st.base_reg) << 5)
            .then_some(and)
    }
    /// The stack slot's bytes, which the stack check's "aligned" means: 8, or 4 in 32-bit code (M2 task 4).
    fn slot(&self) -> u64 {
        if self.c.bits32 { 4 } else { 8 }
    }
    /// Stack check runs (fexc --push-run): per word, the registers known 8-byte aligned before it (bit 31 and x0/x1 never). A forward
    /// dataflow from the entry (nothing aligned): a stack alignment check's fall-through adds its register; a load/store
    /// writeback of a multiple of 8 keeps its base; `add|sub{s} xD, xN, #imm` (64-bit) of a multiple of 8 and `mov xD, xN` (orr
    /// with xzr) set D from an aligned N; every other write clears its registers ([`Insn::writes`]), an `svc` all. A branch
    /// target meets the intersection of its sources; a bulk REP STOS loop's back edge clears what the loop writes.
    fn aligned(&self) -> Vec<u32> {
        let n = self.c.code.len() / 4;
        let mut before = vec![0u32; n];
        let mut pending: Vec<Option<u32>> = vec![None; n + 1];
        let checks: Vec<(u32, u8)> = (0..self.c.stubs.len())
            .filter(|&i| self.stack_check(i).is_some())
            .map(|i| (self.checks[i].end, self.c.stubs[i].base_reg))
            .collect();
        // (32-bit code: multiples of 4, and the 32-bit forms `add|sub{s} wD, wN, #imm` and `mov wD, wN` too: the low bits are the same.)
        let (slot, sf) = (self.slot() as i64, if self.c.bits32 { 0x7fff_ffffu32 } else { !0 });
        let (mut m, mut dead) = (0u32, false);
        for i in ENTRY_OFFSET as usize / 4..n {
            let at = i as u32 * 4;
            m = match (pending[i], dead) {
                (Some(p), true) => p,
                (Some(p), false) => m & p,
                (None, true) => 0,
                (None, false) => m,
            };
            dead = false;
            before[i] = m;
            let w = self.word(at);
            let Some(insn) = self.insn(at) else {
                m = 0;
                continue;
            };
            let bit = |r: u32| if r < 31 { 1u32 << r } else { 0 };
            let mut next = m & !insn.writes();
            match insn {
                Insn::Mem(x) if matches!(x.form, Form::Pre | Form::Post) => {
                    if m & bit(u32::from(x.addr.base)) != 0
                        && i64::from(x.delta) % slot == 0
                        && !(x.load && !x.vector && (x.rt == x.addr.base || x.rt2 == Some(x.addr.base)))
                    {
                        next |= bit(u32::from(x.addr.base));
                    }
                }
                Insn::Data(Some(d)) => {
                    let (rn, rm) = (w >> 5 & 31, w >> 16 & 31);
                    let imm = (w >> 10 & 0xfff) << (12 * (w >> 22 & 1));
                    if (w & 0x9f80_0000 & sf == 0x9100_0000 & sf && m & bit(rn) != 0 && i64::from(imm) % slot == 0)
                        || (w & 0xffe0_ffe0 & sf == 0xaa00_03e0 & sf && m & bit(rm) != 0)
                    {
                        next |= bit(u32::from(d));
                    }
                }
                Insn::Svc(_) => next = 0,
                _ => {}
            }
            for &(end, r) in &checks {
                if end == at {
                    next |= bit(u32::from(r));
                }
            }
            match insn {
                Insn::B(t) | Insn::Cond(t) => {
                    if let Ok(t) = u32::try_from(t) {
                        if t > at && ((t / 4) as usize) < n {
                            let p = &mut pending[(t / 4) as usize];
                            *p = Some(p.map_or(m, |p| p & m));
                        } else if t <= at {
                            let loop_writes = (t..at).step_by(4).fold(0, |a, o| a | self.insn(o).map_or(!0, |x| x.writes()));
                            next &= !loop_writes;
                        }
                    }
                }
                _ => {}
            }
            dead = !insn.falls_through() || matches!(insn, Insn::B(_));
            m = next & !3 & !(1 << 31);
        }
        before
    }
    /// Stack check runs (fexc --push-run): the naturally aligned store at `at` on base B has its stack alignment check
    /// `and x0, xB, #7; cbnz x0, K` (K its `STACK` stub) with only x0/x1 written between it and the store and no branch entering;
    /// or none, and B is known aligned there ([`Verifier::aligned`]). So the store stays inside one page.
    fn stack_aligned(&mut self, at: u32, lo: u32, base: u8) -> Result<(), String> {
        match self.linked(at, object::kind::STACK)[..] {
            [i] => {
                let end = self.checks[i].end;
                let ok = self.stack_check(i).filter(|&and| {
                    and >= lo
                        && end < at
                        && self.c.stubs[i].base_reg == base
                        && self.sealed(and, end, at)
                        && (end + 4..at)
                            .step_by(4)
                            .all(|o| matches!(self.insn(o), Some(Insn::Data(d)) if d.is_none_or(|r| r < 2)))
                });
                if ok.is_none() {
                    return Err("no stack alignment check `and x0, xB, #7; cbnz x0` on its base, then only x0/x1 written".into());
                }
                self.claim(self.c.stubs[i].native_offset);
                Ok(())
            }
            [] => {
                if self.aligned_before[at as usize / 4] & 1 << base != 0 {
                    Ok(())
                } else {
                    Err("store with neither a split check nor a stack alignment check covering its base".into())
                }
            }
            _ => Err("several stack alignment stubs".into()),
        }
    }

    /// Every exit: each `svc #LINK` ends a link point to a proven RIP; the dispatch branch follows `mov x0, xR`.
    fn exits(&mut self) -> Result<(), String> {
        // The link point at each relocation's offset ([`Self::link_point`]: the same for every exit), in the relocations'
        // order, found once for the block at its first exit; and the unit's last instruction.
        let mut points: Option<Vec<(u32, Option<(u64, u32)>)>> = None;
        let last = (self.c.pcs.iter().map(|p| p.guest_offset))
            .filter(|&g| g < self.c.guest_bytes)
            .max();
        for at in (ENTRY_OFFSET..self.c.code.len() as u32).step_by(4) {
            if !matches!(self.insn(at), Some(Insn::Svc(svc::LINK))) {
                continue;
            }
            let points = points.get_or_insert_with(|| self.c.relocs.iter().map(|r| (r.offset, self.link_point(r.offset))).collect());
            let point = points
                .iter()
                .filter(|&&(o, _)| o < at)
                .find_map(|&(o, p)| p.filter(|&(_, end)| end == at).map(|p| (o, p)));
            let Some((offset, (target, _))) = point
            else {
                return Err(format!(
                    "exit at {at:#x}: a literal exit is not exactly a link point"
                ));
            };
            if !self.c.targets.contains(&target) {
                return Err(format!(
                    "exit at {at:#x}: literal {target:#x} is neither the block end nor its branch target"
                ));
            }
            // An exit a branch inside the unit reaches is the exit of one guest instruction: the one whose code starts at the
            // exit if the branch comes from the instruction right before it (its skip branch), else the branch's own (a conditional's taken
            // exit, placed after the last instruction). Only the unit's last instruction may exit to the whole set (end,
            // ...); another goes to its own direct target (or retries at its own RIP: only an STI and a VERW, [`super::retries_own_rip`]; CLI and
            // SWAPGS never exit).
            let at_exit = self.instruction(offset);
            // (The branches are sorted by target: those to the exit are one slice of them, in their order. The PC table's
            // guest offsets ascend: the instruction before the exit's is the last entry below its guest offset.)
            let srcs = &self.branches
                [self.branches.partition_point(|&(_, t)| t < offset)..self.branches.partition_point(|&(_, t)| t <= offset)];
            let before = at_exit.and_then(|(g, _)| {
                let pcs = self.c.pcs;
                pcs[..pcs.partition_point(|p| p.guest_offset < g)].last().map(|p| p.guest_offset)
            });
            // An exit no branch reaches is entered by falling through: it belongs to the instruction holding it.
            for src in srcs.iter().map(|&(s, _)| Some(s)).chain(srcs.is_empty().then_some(None)) {
                let owner = match (at_exit, src.map(|s| self.instruction(s))) {
                    (Some((g, start)), Some(Some((s, _))))
                        if start == offset
                            && Some(s) == before
                            && super::branch_target(self.guest_insn(s), 0).is_none() =>
                    {
                        Some(g)
                    }
                    (_, Some(s)) => s.map(|(g, _)| g),
                    (at_exit, None) => at_exit.map(|(g, _)| g),
                };
                let Some(g) = owner else {
                    return Err(format!("exit at {at:#x}: its branch is outside the PC table"));
                };
                let insn = self.guest_insn(g);
                let end = self.c.address(g as u64 + insn.len() as u64);
                let own = self.c.address(g as u64);
                // (An exit owned by the unit's end, past the last instruction, goes to the end RIP: `own` there.)
                let retry = target == own && (g >= self.c.guest_bytes || super::retries_own_rip(insn));
                let direct = super::branch_target(insn, end).map(|t| super::wrap(t, self.c.bits32));
                if Some(g) != last && !retry && direct != Some(target) {
                    return Err(format!(
                        "exit at {at:#x}: literal {target:#x} is not the direct target of the branch instruction reaching it (guest {insn:02x?} at {own:#x}, branch at {})",
                        src.map_or("none: reached by falling through".into(), |s| format!("{s:#x}"))
                    ));
                }
            }
            self.links.push(LinkPoint {
                offset,
                word: self.word(offset),
                target,
            });
        }
        if self.dispatch.is_some() && self.computed_exit().is_none() {
            return Err(format!(
                "exit at {:#x}: the dispatch branch does not follow `mov x0, xR`",
                self.dispatch.unwrap()
            ));
        }
        Ok(())
    }
    /// The dispatch branch and the register R of the `mov x0, xR` before it.
    fn computed_exit(&self) -> Option<(u32, u8)> {
        let at = self.dispatch?;
        let mov = at
            .checked_sub(4)
            .map(|m| self.word(m))
            .filter(|w| w & 0xffe0_ffff == 0xaa00_03e0); // mov x0, xR
        mov.map(|w| (at, (w >> 16 & 31) as u8))
            .filter(|&(_, r)| r != 0 && r != 31)
    }
    /// Code shape: the computed exit's xR is checked canonical by a branch-target stub in its instruction (x86 raises
    /// #GP at the branch, before any push, pop or RIP change).
    fn branch_target(&mut self) -> Result<(), String> {
        let Some((at, r)) = self.computed_exit() else {
            return Ok(());
        };
        let Some((g, lo)) = self.instruction(at) else {
            return Err(format!("exit at {at:#x} outside the PC table"));
        };
        let found = (0..self.c.stubs.len()).find(|&i| {
            let (st, c) = (&self.c.stubs[i], self.checks[i]);
            let eor = c.start.wrapping_sub(4);
            st.kind == object::kind::GUARD
                && st.guest_offset2 == object::NONE32
                && st.guest_offset == g
                && c.cond == Some(TBNZ_X0_63)
                && eor >= lo
                && c.end < at - 4
                && self.word(eor) == eor_guard(st.base_reg as u32)
                && self.holds(st.base_reg, c.end + 4, at - 4, r)
                && self.sealed(eor, c.end, at)
        });
        match found {
            Some(i) => self.claimed[i] = true,
            None => {
                return Err(format!(
                    "exit at {at:#x}: x{r} is not checked by a branch-target guard in its instruction"
                ));
            }
        }
        Ok(())
    }

    /// `reg`'s value reaches register `r` at `at` from `from`, through `mov` copies only.
    fn holds(&self, reg: u8, from: u32, at: u32, r: u8) -> bool {
        let mut alias = 1u32 << reg;
        for o in (from..at).step_by(4) {
            let w = self.word(o);
            let copy = w & 0xffe0_ffe0 == 0xaa00_03e0 && alias & 1 << (w >> 16 & 31) != 0; // mov xD, xS
            alias &= !self.insn(o).map_or(0, |i| i.writes());
            if copy {
                alias |= 1 << (w & 31);
            }
        }
        alias & 1 << r != 0
    }

    /// The #DE check ending in the check `c` of a div/idiv of `width` bits: its kind, first word, divisor register
    /// (high and zero checks) and the dividend register it reads (8-bit forms).
    fn div_check(&self, c: Check, signed: bool, width: u32) -> Option<(DivCheck, u32, u32, u32)> {
        let cond = c.cond?;
        let before = |k: u32| (c.start >= 4 * k).then(|| self.word(c.start - 4 * k));
        let regs = |r: u32| r >= 4 && r != 28 && r < 31; // not FEX's scratch x0-x3
        let matches = |words: &[u32]| {
            (1..=words.len() as u32).all(|k| before(k) == Some(words[words.len() - k as usize]))
        };
        let start = |k: u32| c.start - 4 * k;
        use DivCheck::*;
        if width == 64 && cond & !31 == CBZ_X && regs(cond & 31) && cond != CBZ_X | 1 {
            return Some((Zero, c.start, cond & 31, 0));
        }
        if !signed {
            let (h, d) = (
                if width == 8 { before(2)? >> 5 & 31 } else { 0 },
                before(1)? >> 16 & 31,
            );
            let form: [u32; 2] = match width {
                8 => [0xd348_3c00 | h << 5, 0xcb20_0000 | d << 16], // ubfx x0, xH, #8, #8; sub x0, x0, wD, uxtb
                16 => [0x5300_3ca0, 0xcb20_2000 | d << 16], // uxth w0, w5; sub x0, x0, wD, uxth
                32 => [0x2a05_03e0, 0xcb20_4000 | d << 16], // mov w0, w5; sub x0, x0, wD, uxtw
                _ => return None,
            };
            return (cond == TBZ_X0_63 && (width != 8 || regs(h)) && regs(d) && matches(&form))
                .then_some((High, start(2), d, h));
        }
        if cond == CBNZ_X | 2 {
            let ok = width == 64 && before(1) == Some(SVC_DIV_S);
            return ok.then_some((Verdict, start(1), 2, 0));
        }
        let d = before(1)? >> 5 & 31;
        let zero = match width {
            8 => Some([0x1300_1c01 | d << 5, CBZ_W | 1]), // sxtb w1, wD; cbz w1
            16 => Some([0x1300_3c01 | d << 5, CBZ_W | 1]), // sxth w1, wD; cbz w1
            32 => Some([0x9340_7c01 | d << 5, CBZ_X | 1]), // sxtw x1, wD; cbz x1
            _ => None,
        };
        if zero.is_some_and(|[sxt, cbz]| regs(d) && cond == cbz && before(1) == Some(sxt)) {
            return Some((Zero, start(1), d, 0));
        }
        let n = before(3)? >> 5 & 31;
        let overflow: &[u32] = match width {
            8 => &[0x1300_3c00 | n << 5, 0x1ac1_0c00, 0x4b20_8000, CBNZ_W], // sxth w0, wN; sdiv w0, w0, w1; sub w0, w0, w0, sxtb
            16 => &[0x5300_3c80, 0x3310_3ca0, 0x1ac1_0c00, 0x4b20_a000, CBNZ_W], // uxth w0, w4; bfi w0, w5, #16, #16; sdiv; sxth
            32 => &[0x2a04_03e0, 0xb360_7ca0, 0x9ac1_0c00, 0xcb20_c000, CBNZ_X], // mov w0, w4; bfi x0, x5, #32, #32; sdiv; sxtw
            // eor x0, x4, #1 << 63; add x1, x5, #1; orr x0, x0, x1; add x1, xD, #1; orr x0, x0, x1; cbz x0
            _ => {
                let d = before(2)? >> 5 & 31;
                return (regs(d)
                    && cond == CBZ_X
                    && matches(&[
                        0xd241_0080,
                        0x9100_04a1,
                        0xaa01_0000,
                        0x9100_0401 | d << 5,
                        0xaa01_0000,
                    ]))
                .then_some((Overflow, start(5), d, 0));
            }
        };
        let (words, last) = overflow.split_at(overflow.len() - 1);
        (cond == last[0] && (width != 8 || regs(n)) && matches(words)).then_some((
            Overflow,
            start(words.len() as u32),
            0,
            n,
        ))
    }

    /// Every div/idiv has exactly its #DE checks before FEX's divide; every divide stub is one of them.
    fn divisions(&mut self) -> Result<(), String> {
        let mut found: Vec<(u32, DivCheck, u32, u32, u32, u32)> = Vec::new(); // guest offset, kind, start, end, divisor, dividend
        for i in 0..self.c.stubs.len() {
            let (st, c) = (self.c.stubs[i], self.checks[i]);
            if st.kind != object::kind::DIVIDE {
                continue;
            }
            let at = st.native_offset;
            let insn = self
                .instruction(c.start)
                .filter(|&(g, lo)| g == st.guest_offset && c.start >= lo);
            let form = insn
                .and_then(|(g, _)| x86_divide(self.guest_insn(g)))
                .and_then(|(signed, width)| {
                    // The verdict's division is unsigned for div: the unsigned form shares the zero check and verdict.
                    let unsigned_verdict = !signed && width == 64 && c.cond == Some(CBNZ_X | 2);
                    if unsigned_verdict {
                        let ok = c.start >= 4 && self.word(c.start - 4) == SVC_DIV_U;
                        return ok.then_some((DivCheck::Verdict, c.start - 4, 2, 0));
                    }
                    self.div_check(c, signed, width)
                });
            let Some((kind, start, d, h)) = form else {
                return Err(format!(
                    "divide stub at {at:#x}: its branch is not an exact #DE check of its div/idiv"
                ));
            };
            found.push((st.guest_offset, kind, start, c.end, d, h));
            self.claimed[i] = true;
        }
        let mut guest: Vec<u32> = self
            .c
            .pcs
            .iter()
            .map(|p| p.guest_offset)
            .filter(|&g| g < self.c.guest_bytes)
            .collect();
        guest.dedup();
        for g in guest {
            let Some((signed, width)) = x86_divide(self.guest_insn(g)) else {
                continue;
            };
            let fail = |what: &str| Err(format!("div/idiv at guest offset {g:#x}: {what}"));
            let mut mine: Vec<_> = found
                .iter()
                .filter(|f| f.0 == g)
                .map(|&(_, k, s, e, d, h)| (k, s, e, d, h))
                .collect();
            mine.sort();
            use DivCheck::*;
            let want: &[DivCheck] = match (signed, width) {
                (false, 64) => &[Zero, Verdict],
                (false, _) => &[High],
                (true, 64) => &[Zero, Overflow, Verdict],
                (true, _) => &[Zero, Overflow],
            };
            if mine.iter().map(|m| m.0).collect::<Vec<_>>() != want {
                return fail("not exactly its #DE checks");
            }
            // The overflow check follows the zero check (it divides by the zero check's x1, or tests its divisor).
            if signed && (mine[1].1 != mine[0].2 + 4 || (width == 64 && mine[1].3 != mine[0].3)) {
                return fail("overflow check does not follow its zero check");
            }
            // FEX's divide: a udiv/sdiv into another register than the check's scratch x0, or the division `svc`.
            let lo = self
                .c
                .pcs
                .iter()
                .find(|p| p.guest_offset == g)
                .map_or(0, |p| p.native_offset);
            let hi = self
                .c
                .pcs
                .iter()
                .find(|p| p.guest_offset > g)
                .map_or(self.c.code.len() as u32, |p| p.native_offset);
            let divide = (lo..hi).step_by(4).find(|&o| {
                let w = self.word(o);
                self.insn(o).is_some()
                    && ((w & 0x7fe0_f800 == 0x1ac0_0800 && w & 31 != 0) || self.division_svc(o))
            });
            let Some(divide) = divide else {
                return fail("no divide");
            };
            let fex = (lo..hi).step_by(4).find(|&o| {
                self.insn(o).is_some()
                    && self.word(o) & 0x7fe0_f800 == 0x1ac0_0800
                    && self.word(o) & 31 != 0
            });
            let Some(fex) = fex else {
                return fail("no inline divide");
            };
            for &(kind, start, end, _, _) in &mine {
                let to = if kind == Verdict {
                    end
                } else {
                    divide.max(fex)
                };
                if (kind != Verdict && end >= divide) || !self.sealed(start, end, to) || start < lo
                {
                    return fail("a #DE check is not before its divide, or a branch enters it");
                }
            }
            // The checks read FEX's own operands: its udiv/sdiv's divisor (directly or through FEX's x1 copy), at 8
            // bits its dividend, and the division `svc`'s divisor in x2; the divisor is unchanged from the checks to the divide.
            let (d, h) = (mine[0].3, mine.iter().map(|m| m.4).max().unwrap_or(0));
            let w = self.word(fex);
            let (rn, rm, sdiv) = (w >> 5 & 31, w >> 16 & 31, w >> 10 & 1 == 1);
            let copy = |o: u32| {
                [
                    0x2a00_03e1 | d << 16,
                    0x1300_3c01 | d << 5,
                    0x9340_7c01 | d << 5,
                ]
                .contains(&self.word(o))
            }; // mov w1, sxth w1, sxtw x1
            let divisor = rm == d || (rm == 1 && self.last_write(1, lo, fex).is_some_and(copy));
            let helper = width != 64
                || self
                    .last_write(2, lo, mine[mine.len() - 1].1)
                    .is_some_and(|o| self.word(o) == 0xaa00_03e2 | d << 16);
            if !divisor
                || sdiv != signed
                || (width == 8 && rn != h)
                || !helper
                || self.written(1 << d, mine[0].1, divide)
            {
                return fail("its #DE checks do not read its divide's operands");
            }
            // The dividend is RDX:RAX (x5:x4), unchanged from the instruction start to FEX's divide; the 8-bit forms
            // read FEX's copy of AX (`uxth wH, w4` / `sxth xH, w4`), unchanged up to the divide.
            let first = mine[0].1;
            let ax = if signed { 0x9340_3c80 } else { 0x5300_3c80 } | h;
            let ax_copy = width != 8
                || (self
                    .last_write(h as u8, lo, first)
                    .is_some_and(|o| self.word(o) == ax)
                    && !self.written(1 << h, first, fex));
            if !ax_copy || self.written(1 << 4 | 1 << 5, lo, divide.max(fex)) {
                return fail("its #DE checks do not read its dividend RDX:RAX");
            }
            // 64 bits: FEX's selector of its inline divide is exactly `cbz x5` (div) or `asr x0, x4, #63; eor x0, x0, x5;
            // cbz x0` (idiv), right after the checks, to that divide.
            if width == 64 {
                let after = mine
                    .iter()
                    .find(|m| m.0 == if signed { Overflow } else { Zero })
                    .map_or(0, |m| m.2)
                    + 4;
                let (words, test): (&[u32], u32) = if signed {
                    (&[0x937f_fc80, 0xca05_0000], CBZ_X)
                } else {
                    (&[], CBZ_X | 5)
                };
                let at = after + 4 * words.len() as u32;
                let prefix = words
                    .iter()
                    .enumerate()
                    .all(|(k, &w)| self.word(after + 4 * k as u32) == w);
                if !prefix || self.cond_branch(at) != Some((test, fex as i64)) {
                    return fail("FEX's 64-bit fast-path selector is not exact");
                }
            }
        }
        // Every division `svc` is a div/idiv's (its verdict was matched above).
        for at in (ENTRY_OFFSET..self.c.code.len() as u32).step_by(4) {
            if self.division_svc(at) && !found.iter().any(|f| f.1 == DivCheck::Verdict && f.2 == at)
            {
                return Err(format!(
                    "division svc at {at:#x} without its division's #DE verdict"
                ));
            }
        }
        Ok(())
    }
}

/// Whether the floating-point or SIMD word `x` reads v1 (AFP accurate plan chunk 4: the FMA check's stale destination): as Rn, as Rm (the
/// three-register classes and the by-element, scalar and permute forms), as a scalar FMA's Rm or addend, as the destination of an accumulating
/// form (fmla, fmls, mla, mls, bsl, bit, bif, ins), or as the data of a SIMD store.
fn reads_v1(x: u32) -> bool {
    let (rd, rn, rm, ra) = (x & 31, x >> 5 & 31, x >> 16 & 31, x >> 10 & 31);
    if x >> 26 & 1 == 1 && x >> 25 & 5 == 4 {
        return x >> 22 & 1 == 0 && rd == 1; // a store of q1 (a load writes it)
    }
    let three_same = x & 0x9f20_0400 == 0x0e20_0400;
    let opcode = x >> 11 & 31;
    let scalar_fma = x & 0xff00_0000 == 0x1f00_0000;
    let accumulates = three_same && (x >> 29 & 1 == 0 && matches!(opcode, 0x19 | 0x12) || x >> 29 & 1 == 1 && opcode == 0x03 && x >> 22 & 3 != 0)
        || x & 0x9f00_0400 == 0x0f00_0000 && matches!(x >> 12 & 15, 1 | 5) // fmla, fmls by element
        || x & 0xffe0_8400 == 0x6e00_0400 || x & 0xffe0_fc00 == 0x4e00_1c00; // ins (element, general)
    let rm_read = three_same
        || x & 0xff20_0c00 == 0x1e20_0800 // scalar two sources
        || scalar_fma
        || x & 0x9f00_0400 == 0x0f00_0000 // by element
        || x & 0xbf20_0c00 == 0x0e00_0800 // zip, uzp, trn
        || x & 0xbf20_8400 == 0x2e00_0000 // ext
        || x & 0xdf20_0400 == 0x5e20_0400; // scalar three-same
    rn == 1 || rm_read && rm == 1 || accumulates && rd == 1 || scalar_fma && ra == 1
}

/// `ldrb|ldr wT, [x28, #at]` (unsigned offset).
const fn ldr_state(size: u32, t: u32, at: u32) -> u32 {
    (if size == 1 { 0x3940_0000 } else { 0xb940_0000 }) | (at / size) << 10 | 28 << 5 | t
}
const MOVZ_W1: u32 = 0x5280_0001; // movz w1, #imm16 (imm16 << 5)
const AND_W1_W0_W1: u32 = 0x0a01_0001;
/// `and w1, wD, #0x80` with D cleared (AFP fallback 3b: the software denormal flag's word of a check) and `orr w0, w0, w1`
const AND_W1_WD_80: u32 = 0x1219_0001;
const ORR_W0_W0_W1: u32 = 0x2a01_0000;
const AND_W0_W0_W1: u32 = 0x0a01_0000;
impl Verifier<'_> {
    /// AFP accurate plan chunk 4 (R4-2): each FMA NaN check site is the word `fcmge vT, vR, #0` (T = 1, JIT's VTMP2; vector .4s/.2d or
    /// scalar s/d, as the descriptor says; R no temporary), in the instruction the site names, and its destination is never read: the handler
    /// skips the word, so v1 is stale after it. The words after it (up to the next branch) are scanned: a word that reads v1 before one writes it
    /// is refused ([`reads_v1`]: Rn, Rm and a scalar FMA's addend of the floating-point and SIMD classes, the destination of the
    /// accumulating forms (fmla, fmls, mla, mls, bsl, bit, bif, ins), and a store of q1).
    fn fma_checks(&mut self) -> Result<(), String> {
        for s in self.c.fma {
            let at = s.native_offset;
            let fail = |why: &str| Err(format!("FMA check at {at:#x}: {why}"));
            let w = self.word(at);
            let d = natemu_abi::tiny::Desc::decode(s.width, s.imm).ok_or("FMA check: descriptor")?;
            let (dbl, scalar) = (d.meta & natemu_abi::tiny::ELEM8 != 0, d.meta & natemu_abi::tiny::SCALAR != 0);
            let sz = u32::from(dbl) << 22;
            let shape = if scalar { 0x7ea0_c800 | sz } else { 0x6ea0_c800 | sz };
            if w & 0xffff_fc1f != shape | 1 || w >> 5 & 31 < 2 {
                return fail("not `fcmge v1, vR, #0`");
            }
            if self.instruction(at).is_none_or(|(g, lo)| g != s.guest_offset || lo != s.insn_native_offset) {
                return fail("not in its instruction");
            }
            let mut k = at + 4;
            while k < self.c.code.len() as u32 {
                let Some(i) = self.insn(k) else { break };
                // (A 32-bit guest-RIP move's `nop` writes nothing: the scan goes on past it as past the move's words.)
                if !matches!(i, Insn::Data(_) | Insn::Mem(_) | Insn::Other | Insn::MovePad) {
                    break;
                }
                let x = self.word(k);
                let fp = x >> 25 & 7 == 7 || x >> 26 & 1 == 1 && x >> 25 & 5 == 4;
                if fp && reads_v1(x) {
                    return fail("its destination v1 is read");
                }
                if fp && x & 31 == 1 && !(x >> 26 & 1 == 1 && x >> 25 & 5 == 4 && x >> 22 & 1 == 0) {
                    break; // written again: the stale value is gone
                }
                k += 4;
            }
        }
        Ok(())
    }
    /// Code shape (I5c): each floating-point exception stub is reached by its exact check.
    fn fp_checks(&mut self) -> Result<(), String> {
        use object::kind::*;
        for i in 0..self.c.stubs.len() {
            let (st, c) = (self.c.stubs[i], self.checks[i]);
            if !matches!(st.kind, SIMD | X87 | PENDING | DENORM | FLDCW | X87TAG | X87GUESS) {
                continue;
            }
            let at = st.native_offset;
            let fail = || {
                Err(format!(
                    "floating-point stub at {at:#x}: its branch is not its exact check"
                ))
            };
            // The compared bits: FPSR's flags (IOC DZC OFC UFC IXC IDC), for x87 also its operand bits 11:8.
            let mask = |w: u32, bits: u32| {
                w & 0xffe0_001f == MOVZ_W1 && w >> 5 & 0xffff != 0 && w >> 5 & !bits & 0xffff == 0
            };
            let words = |n: u32| {
                (c.start >= 4 * n).then(|| {
                    (0..n)
                        .map(|k| self.word(c.start - 4 * (n - k)))
                        .collect::<Vec<_>>()
                })
            };
            let insn = self
                .instruction(c.start)
                .filter(|&(g, _)| g == st.guest_offset);
            let start = match st.kind {
                SIMD => {
                    let after = [
                        ldr_state(1, 1, frame::SIMD_FLAGS),
                        0x2a00_0021,
                        ldr_state(1, 1, frame::SIMD_FLAGS) & !(1 << 22),
                        0xd51b_443f,
                    ];
                    // Without AFP and with DE unmasked (AFP fallback 3b, row 7) the software denormal flag, 0 or nonzero in wD, is
                    // FPSR bit 7 of the check word: `and w1, wD, #0x80; orr w0, w0, w1` right after the `mrs`.
                    let plain = words(3).is_some_and(|w| {
                        w[0] == 0xd53b_4420 && mask(w[1], 0x9f) && w[2] == AND_W1_W0_W1
                    });
                    let soft = words(5).is_some_and(|w| {
                        w[0] == 0xd53b_4420
                            && w[1] & 0xffff_fc1f == AND_W1_WD_80
                            && w[2] == ORR_W0_W0_W1
                            && mask(w[3], 0x9f)
                            && w[4] == AND_W1_W0_W1
                    });
                    let ok = c.cond == Some(CBNZ_W | 1)
                        && (plain || soft)
                        && after
                            .iter()
                            .enumerate()
                            .all(|(k, &w)| self.word(c.end + 4 + 4 * k as u32) == w);
                    // A described stub (the operands of the instruction's multiplies, divides, FMAs and narrowing converts, read by the resident when
                    // UFC is the only unmasked flag) resumes at the word after the `cbnz`.
                    if !ok || st.guest_offset2 != object::NONE32 && st.guest_offset2 != c.end + 4 {
                        return fail();
                    }
                    c.start - if plain { 12 } else { 20 }
                }
                FLDCW => {
                    // M2 task 4, a translated FLDCW: `ldrh w0, [x28, #FCW]; eor w0, w0, wN; and w0, w0, #0x3f; cbnz w0, stub`.
                    let ok = c.cond == Some(CBNZ_W) && c.start == c.end && words(3).is_some_and(|w| {
                        w[0] == 0x7940_0000 | (frame::X87_FCW / 2) << 10 | 28 << 5
                            && w[1] & 0xffe0_ffff == 0x4a00_0000
                            && w[2] == 0x1200_1400
                    });
                    if !ok {
                        return fail();
                    }
                    c.start - 12
                }
                X87GUESS => {
                    // M2 task 4, a unit compiled for an x87 stack top and tag byte: `ldrb w0, [x28, #TOP]; ldrb w1, [x28, #FTW];
                    // orr w0, w1, w0, lsl #8; sub w0, w0, #V; cbnz w0, stub`.
                    let ok = c.cond == Some(CBNZ_W) && c.start == c.end && words(4).is_some_and(|w| {
                        w[0] == ldr_state(1, 0, frame::X87_TOP)
                            && w[1] == ldr_state(1, 1, frame::X87_FTW)
                            && w[2] == 0x2a00_2020
                            && w[3] & 0xffc0_03ff == 0x5100_0000
                    });
                    if !ok {
                        return fail();
                    }
                    c.start - 16
                }
                X87TAG => {
                    // M2 task 4, an x87 instruction's stack check: `lsr w0, wF, wI; tbz|tbnz w0, #0, stub`.
                    let ok = matches!(c.cond, Some(0x3600_0000 | 0x3700_0000))
                        && c.start == c.end
                        && words(1).is_some_and(|w| w[0] & 0xffe0_fc1f == 0x1ac0_2400);
                    if !ok {
                        return fail();
                    }
                    c.start - 4
                }
                DENORM => {
                    // The in-block DE screen skip's check: `ldrb w0, [x28, #DE_PENDING]; cbnz w0, stub`, before its instruction's first floating-point
                    // op (the state at the stub is the state before the instruction; the resident exits to the dispatcher there).
                    let ok = c.cond == Some(CBNZ_W) && c.start == c.end && words(1).is_some_and(|w| w[0] == ldr_state(1, 0, frame::DE_PENDING));
                    if !ok {
                        return fail();
                    }
                    c.start - 4
                }
                X87 => {
                    // Row 8: with DE unmasked and no AFP, `and w1, wD, #0x80; orr w0, w0, w1` follow the pending byte's load (the stack
                    // fault then drops the software flag as it drops every flag).
                    let soft = words(11).is_some_and(|w| {
                        w[1] & 0xffff_fc1f == AND_W1_WD_80 && w[2] == ORR_W0_W0_W1
                    });
                    let words = |n: u32| {
                        if n == 9 && soft {
                            words(11).map(|w| [&w[..1], &w[3..]].concat())
                        } else {
                            words(n)
                        }
                    };
                    let ok = c.cond == Some(CBNZ_W | 1)
                        && words(9).is_some_and(|w| {
                            let f = w[1] >> 5 & 31;
                            w[0] == ldr_state(1, 0, frame::X87_PENDING)
                                && w[1] & 0xffff_fc1f == 0x5100_0401
                                && f < 31
                                && w[2] == AND_W0_W0_W1
                                && w[3] == 0x2a00_2000 | f << 16
                                && (4..7).all(|k| {
                                    w[k] & 0xffe0_ffff == 0x2a00_0000 | (k as u32 + 5) << 10
                                })
                                && mask(w[7], 0xf9f)
                                && w[8] == AND_W1_W0_W1
                        });
                    if !ok {
                        return fail();
                    }
                    c.start - if soft { 44 } else { 36 }
                }
                _ => {
                    let want = [
                        ldr_state(1, 0, frame::X87_IE),
                        ldr_state(1, 1, frame::X87_PE),
                        0x2a01_1000,
                        ldr_state(4, 1, frame::X87_EXCEPTIONS),
                        0x2a01_0000,
                    ];
                    let ok = c.cond == Some(CBNZ_W)
                        && words(7).is_some_and(|w| {
                            w[..5] == want && mask(w[5], 0x9f) && w[6] == AND_W0_W0_W1
                        })
                        && insn.is_some_and(|(_, lo)| lo == c.start - 28);
                    if !ok {
                        return fail();
                    }
                    c.start - 28
                }
            };
            if insn.is_none_or(|(_, lo)| start < lo) || !self.sealed(start, c.end, c.end + 4) {
                return fail();
            }
            self.claimed[i] = true;
        }
        Ok(())
    }
}

/// Legacy prefixes including LOCK (and REX bytes, [`crate::system::opcode_at`]), then cmpxchg (0f b0/b1) or cmpxchg8b/16b (0f c7 /1).
fn lock_cmpxchg(insn: &[u8]) -> bool {
    let (o, _) = crate::system::opcode_at(insn);
    insn[..o].contains(&0xf0)
        && insn.get(o) == Some(&0x0f)
        && match insn.get(o + 1) {
            Some(0xb0 | 0xb1) => true,
            Some(0xc7) => insn.get(o + 2).is_some_and(|m| m >> 3 & 7 == 1),
            _ => false,
        }
}

fn is_cbtb(w: u32) -> bool {
    w & 0x7c00_0000 == 0x3400_0000
}
/// A cbz/cbnz/tbz/tbnz word with its offset field cleared (other branches unchanged).
fn normalize(w: u32) -> u32 {
    match w & 0x7e00_0000 {
        0x3400_0000 => w & !(0x7_ffff << 5),
        0x3600_0000 => w & !(0x3fff << 5),
        _ => w,
    }
}

/// (signed, operand width) of an x86 `div`/`idiv` (F6/F7 /6, /7), else `None`.
fn x86_divide(insn: &[u8]) -> Option<(bool, u32)> {
    let (i, rex) = crate::system::opcode_at(insn);
    let size16 = insn[..i].contains(&0x66);
    let (op, reg) = (*insn.get(i)?, insn.get(i + 1)? >> 3 & 7);
    if !matches!(op, 0xf6 | 0xf7) || reg < 6 {
        return None;
    }
    Some((
        reg == 7,
        if op == 0xf6 {
            8
        } else if rex & 8 != 0 {
            64
        } else if size16 {
            16
        } else {
            32
        },
    ))
}

/// I6b: the helper function of an x87 transcendental, remainder, scale or extract instruction, the D9 opcode's second byte (F0..F5,
/// F8, F9, FB, FD..FF), after segment, address-size and REX prefixes (fexc refuses 66, F2 and F3 on x87 forms).
fn x86_x87_helper(insn: &[u8]) -> Option<u32> {
    let (i, _) = crate::system::opcode_at(insn);
    if !insn[..i].iter().all(|b| {
        matches!(
            b,
            0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 | 0x67 | 0x40..=0x4f
        )
    }) {
        return None;
    }
    match insn.get(i..)? {
        [0xd9, m @ (0xf0..=0xf5 | 0xf8 | 0xf9 | 0xfb | 0xfd..=0xff)] => Some(u32::from(*m)),
        _ => None,
    }
}
/// I6b: the control word of PCMPESTRM/PCMPESTRI/PCMPISTRM/PCMPISTRI (66 [REX] 0F 3A 60..63 or VEX.128.66.0F3A 60..63): imm8 (the
/// instruction's last byte), and for the explicit-length forms (60, 61) REX.W/VEX.W << 8 and 1 << 9.
fn x86_string_compare(insn: &[u8]) -> Option<u32> {
    let (i, rex) = crate::system::opcode_at(insn);
    if !insn[..i].iter().all(|b| {
        matches!(
            b,
            0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 | 0x67 | 0x66 | 0x40..=0x4f
        )
    }) {
        return None;
    }
    let (opsize, any_rex) = (
        insn[..i].contains(&0x66),
        insn[..i].iter().any(|b| b & 0xf0 == 0x40),
    );
    let (w, op) = match insn.get(i..)? {
        [0xc4, p1, p2, op, ..] if !opsize && !any_rex && p1 & 0x1f == 3 && p2 & 7 == 1 => {
            (p2 >> 7, *op)
        } // map 0F3A, L = 0, pp = 66
        [0x0f, 0x3a, op, ..] if opsize => (rex >> 3 & 1, *op),
        _ => return None,
    };
    let imm = u32::from(*insn.last()?);
    match op {
        0x60 | 0x61 => Some(imm | u32::from(w) << 8 | natemu_abi::strings::EXPLICIT),
        0x62 | 0x63 => Some(imm),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::code_object::Header;

    /// Real fexc (series13, profile slice1; unchanged by series14) objects and the guest bytes they were compiled from.
    macro_rules! fixture {
        ($name:literal) => {
            (
                include_bytes!(concat!("../../tests/admission/", $name, ".obj")) as &[u8],
                include_bytes!(concat!("../../tests/admission/", $name, ".src")) as &[u8],
            )
        };
    }
    const FIXTURES: [(&[u8], &[u8]); 12] = [
        fixture!("address-forms"),
        fixture!("division-forms"),
        fixture!("call-rsp"),
        fixture!("rip-relative"),
        fixture!("rip-relative-noncanonical"),
        fixture!("large-displacements"),
        fixture!("jcc"),
        fixture!("fix-edge-rip-relative"),
        fixture!("xchg-fences"),
        // I3 (series18): lock add, lock cmpxchg (`dmb ish` at 0x18, casal at 0x1c), cmpxchg8b (caspal at 0x30, no barrier),
        // lock cmpxchg16b (`dmb ish` at 0x54, caspal at 0x58), lock xadd, lock neg (load, casal, its own-RIP exit).
        fixture!("lock-forms"),
        // I6b (series31): fld qword [rax]; fsin; fprem; pcmpistri xmm1, [rbx + 3], 0x12; REX.W pcmpestri xmm1, xmm2, 0x4c;
        // vpcmpistrm xmm2, xmm3, 0x40; fstp qword [rax]. `movz w0, #fn; svc #X87` at 0x98 (FSIN) and 0xa0 (FPREM); `movz w0,
        // #control; svc #STRING` at 0xb4 (0x12), 0xf0 (0x34c: explicit, REX.W) and 0x11c (0x40).
        fixture!("helper-forms"),
        // Extended block (--ext-conds 4): `cmp rax, rcx; jnz +0x10; inc rax; jz +0x20; dec rcx; jnz +0x1e` at RIP 0x401000;
        // exits: the fall-through's 0x40100e at 0x40, the taken 0x401015, 0x40102a, 0x40102c at 0x4c, 0x58, 0x64.
        fixture!("ext-jcc"),
    ];
    /// Admission with the code-shape check (as in the test suites).
    fn admit(fixture: (&[u8], &[u8])) -> Result<(), String> {
        admit_with(fixture, true)
    }
    fn admit_with((object, source): (&[u8], &[u8]), shape: bool) -> Result<(), String> {
        admit_at((object, source), shape, false)
    }
    /// `cpl0`: as a CPL-0 request's object (perf chunks 15, 17).
    fn admit_at((object, source): (&[u8], &[u8]), shape: bool, cpl0: bool) -> Result<(), String> {
        let h: Header = super::super::record(object, 0);
        super::super::admit(object, h.guest_rip, source, u64::MAX, shape, cpl0).map(|_| ())
    }
    /// M2, with `FEXC` set: 32-bit code (`--bitness 32`) is admitted under its mode only, with FEX's compact register map,
    /// branch targets and return addresses that wrap at 4 GiB, and the ES/CS/SS/DS base loads; a 64-bit object is refused
    /// under the 32-bit mode, and a base load patched into 64-bit code stays refused (`frame_fields`).
    /// M2 task 4, with `FEXC` set: a split stub that probes through its shared probe. `mov [rbx+8], rax`: the check at
    /// 0x4..0x10 (on x1 = rbx + 8), the store at 0x14, the stub at 0x24: `adr x0, 0x14; b .`, the `b` at a probe_branch
    /// relocation naming the probe of a 64-bit, x1, 8-byte store (`natemu_abi::execution::probe::index`).
    #[test]
    fn probing_split_stubs_are_verified() {
        use natemu_abi::execution::probe;
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let object = std::env::temp_dir().join(format!("natemu-admit-probe-{}", std::process::id()));
        let out = std::process::Command::new(&fexc)
            .args(["--hex", "48894308", "--rip", "0x8048000", "--profile", "slice1", "--narrow-cmp", "--ext-conds", "4", "--object"])
            .arg(&object)
            .output()
            .unwrap();
        assert!(out.status.success());
        let block = (std::fs::read(&object).unwrap(), vec![0x48, 0x89, 0x43, 0x08]);
        let _ = std::fs::remove_file(&object);
        let block = (&block.0[..], &block.1[..]);
        admit(block).unwrap();
        admit_with(block, false).unwrap();
        let adr = |to: u64| probe::adr_x0(0x24, to).unwrap();
        // Object validation (so also with the code shape off): the stub's words exactly.
        for (at, old, new) in [
            (0x24, adr(0x14), adr(0x18)),     // back to a word after the access
            (0x24, adr(0x14), adr(0x0)),      // back to before the code
            (0x24, adr(0x14), adr(0x14) | 1), // adr x1: the probe returns through x0
            (0x28, DISPATCH_PLACEHOLDER, 0x1400_0001), // not the placeholder
        ] {
            validated(block, at, old, new, Some("check stub at native offset 0x24"));
        }
        // The relocation names another store's probe (x2), or another width's, or none.
        let h: Header = super::super::record(block.0, 0);
        let reloc = (0..h.relocs_count as usize)
            .map(|i| h.relocs_offset as usize + 16 * i)
            .find(|&at| block.0[at + 4] == object::RELOC_PROBE_BRANCH)
            .unwrap();
        assert_eq!(block.0[reloc + 8..reloc + 16], u64::from(probe::index(false, 1, 8).unwrap()).to_le_bytes());
        for value in [probe::index(false, 2, 8), probe::index(false, 1, 4), probe::index(true, 1, 8), Some(probe::THUNKS as u32)] {
            let mut o = block.0.to_vec();
            o[reloc + 8..reloc + 16].copy_from_slice(&u64::from(value.unwrap()).to_le_bytes());
            let e = admit_with((&o, block.1), false).unwrap_err();
            assert!(e.contains("check stub at native offset 0x24") || e.contains("relocation at 0x28"), "{value:?}: {e}");
        }
        // Back to the check itself (its `tbnz` at 0x10, or before it): a loop through the stub, refused with the code shape off.
        validated(block, 0x24, adr(0x14), adr(0x10), Some("probing split stub 0 at 0x24: its way back"));
        validated(block, 0x24, adr(0x14), adr(0x8), Some("probing split stub 0 at 0x24: its way back"));
        // The check's branch to the stub's `b` instead of its `adr` (the probe would return wherever x0 points).
        validated(block, 0x10, 0x3760_00a0, 0x3760_00c0, Some("probing split stub 0 at 0x24: its way back"));
        // A probe in generated code; a second way into the stub, from after the word it returns to.
        validated(block, 0x4, 0x9100_20c1, 0x383f_003f, Some("page probe outside a split stub"));
        assert!(admit_with((&patched(block, 0x1c, 0x72a1_0080, 0x1400_0002), block.1), false).is_err()); // b 0x24
    }

    /// Gather plan (2.5), with `FEXC` set: fexc compiles no inline gather in 32-bit code, and admission refuses the mark of a
    /// gather's element load in a 32-bit object (with and without the code shape): `movss xmm0, [eax]`, whose 4-byte load
    /// into a vector register has the marked form, is admitted, and refused once its site carries the mark.
    #[test]
    fn gather_element_loads_are_refused_in_32_bit_objects() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let object = std::env::temp_dir().join(format!("natemu-admit-gather32-{}", std::process::id()));
        let out = std::process::Command::new(&fexc)
            .args(["--hex", "f30f1000", "--rip", "0x8048000", "--profile", "slice1", "--bitness", "32", "--push-run", "--narrow-cmp", "--ext-conds", "4", "--object"])
            .arg(&object)
            .output()
            .unwrap();
        assert!(out.status.success());
        let o = std::fs::read(&object).unwrap();
        let _ = std::fs::remove_file(&object);
        let source = [0xf3, 0x0f, 0x10, 0x00];
        let h: Header = super::super::record(&o, 0);
        let admit = |o: &[u8], shape: bool| super::super::admit_mode(o, h.guest_rip, &source, u64::MAX, shape, false, true).map(|_| ());
        admit(&o, true).unwrap();
        let size = std::mem::size_of::<object::Site>();
        let i = (0..h.sites_count as usize)
            .find(|&i| {
                let s = super::super::record::<object::Site>(&o, h.sites_offset as usize + i * size);
                (s.kind, s.width, s.data_class) == (object::kind::LOAD, 4, object::CLASS_FPR)
            })
            .expect("the load site");
        let mut marked = o.clone();
        let at = h.sites_offset as usize + i * size + std::mem::offset_of!(object::Site, guest_offset2);
        marked[at..at + 4].copy_from_slice(&object::GATHER_ELEMENT.to_le_bytes());
        for shape in [false, true] {
            let e = admit(&marked, shape).unwrap_err();
            assert!(e.contains("a gather element site in a 32-bit object"), "{e}");
        }
    }

    /// M2 task 4, with `FEXC` set: the test for an open x87 run at the top of a 32-bit block of SSE code (`mulsd xmm0, xmm1`
    /// twice, so that a zero word pads the code after the run's end: a branch there is forward):
    /// `ldrb w0, [x28, #X87_RUN]` at 0x4, `cbnz w0, E` at 0x8, the run's end's nine words at E and the branch back to 0xc.
    #[test]
    fn top_run_tests_are_verified() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let object = std::env::temp_dir().join(format!("natemu-admit-run-{}", std::process::id()));
        let out = std::process::Command::new(&fexc)
            .args(["--hex", "f20f59c1f20f59c1", "--rip", "0x8048000", "--profile", "slice1", "--bitness", "32", "--narrow-cmp", "--ext-conds", "4", "--object"])
            .arg(&object)
            .output()
            .unwrap();
        assert!(out.status.success());
        let block = (std::fs::read(&object).unwrap(), vec![0xf2, 0x0f, 0x59, 0xc1, 0xf2, 0x0f, 0x59, 0xc1]);
        let _ = std::fs::remove_file(&object);
        let block = (&block.0[..], &block.1[..]);
        let admit = |o: &[u8], shape: bool| {
            let h: Header = super::super::record(o, 0);
            super::super::admit_mode(o, h.guest_rip, block.1, u64::MAX, shape, false, true).map(|_| ())
        };
        admit(block.0, true).unwrap();
        admit(block.0, false).unwrap();
        let h: Header = super::super::record(block.0, 0);
        let word = |at: u32| u32::from_le_bytes(block.0[(h.code_offset + at) as usize..][..4].try_into().unwrap());
        assert_eq!(word(4), 0x3940_0380 | frame::X87_RUN << 10);
        let end = 8 + (word(8) >> 5 & 0x7_ffff) * 4;
        assert_eq!((word(8) & 0xff00_001f, word(end), word(end + 36)), (0x3500_0000, RUN_END[0], 0x1400_0000 | (0xc_u32.wrapping_sub(end + 36) / 4) & 0x3ff_ffff));
        // The return is the one backward branch: not to another word, and only behind the run's end's exact words (so
        // also with the code shape off).
        for (at, new) in [(end + 36, word(end + 36) + 1), (end, MOV_X0), (end + 32, MOV_X0)] {
            let e = admit(&patched(block, at, word(at), new), false).expect_err("admitted");
            assert!(e.contains("not forward"), "{at:#x}: {e}");
        }
        // A branch forward from the run's end is no return (it would be taken for one by the resident's layout).
        let e = admit(&patched(block, end + 36, word(end + 36), 0x1400_0001), false).expect_err("admitted");
        assert!(e.contains("does not return to the block's third word"), "{e}");
        // No other backward branch into the run's end: the last word of the code region, made `b` to it.
        let last = h.code_region_end - 4;
        if last > end + 36 {
            let back = 0x1400_0000 | (end + 4).wrapping_sub(last) / 4 & 0x3ff_ffff;
            let e = admit(&patched(block, last, word(last), back), false).expect_err("admitted");
            assert!(e.contains("backward branch into the x87 run's end") || e.contains("not forward"), "{e}");
        }
        // A block that begins with the test opens no run (the code shape).
        let opened = patched(block, end + 32, word(end + 32), word(end + 32) & !31 | 1); // strb w1, [x28, #X87_RUN]
        assert!(admit(&opened, true).is_err());
    }

    #[test]
    fn code_of_32_bit_mode_is_admitted_under_its_mode_only() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let dir = std::env::temp_dir().join(format!("natemu-admit32-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let compile = |hex: &str, rip: &str, bits: &str| -> (Vec<u8>, Vec<u8>) {
            let object = dir.join("o");
            let out = std::process::Command::new(&fexc)
                .args(["--hex", hex, "--rip", rip, "--profile", "slice1", "--bitness", bits, "--narrow-cmp", "--ext-conds", "4"])
                .arg("--object")
                .arg(&object)
                .output()
                .unwrap();
            assert!(out.status.success(), "{hex} at {rip}");
            let source = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect();
            (std::fs::read(&object).unwrap(), source)
        };
        let admit = |(object, source): &(Vec<u8>, Vec<u8>), bits32: bool| {
            let h: Header = super::super::record(object, 0);
            super::super::admit_mode(object, h.guest_rip, source, u64::MAX, true, false, bits32).map(|a| a.links.len())
        };
        // (hex, RIP): a frame set-up with inc/dec r32; string instructions (DS and ES bases); every segment prefix; a pushed
        // return address and a call target that wrap (call +0 ending at 4 GiB); a jcc whose fall-through is 4 GiB; a jmp to
        // 4 GiB; a wrapping backward jmp at the bottom; an indirect call and a ret; x87 and SSE forms; DAA.
        for (hex, rip) in [
            ("5589e58b450840894424044f5dc3", "0x8048000"),
            ("a5aa66adaf", "0x8048000"),
            ("f3a5", "0x8048000"),
            ("268b032e8b0b368b133e8b4500648b03658b0d10000000", "0x8048000"),
            ("e800000000", "0xfffffffb"),
            ("7402ebfe", "0xfffffffc"),
            ("eb7e", "0xffffff80"),
            ("e9fbfffffe", "0x10"),
            ("ff10", "0x8048000"),
            ("c20800", "0x8048000"),
            ("d900dc03db1fdf2cdf7c2438", "0x8048000"),
            ("0f28000f580b660fefc0f20f2a17", "0x8048000"),
            ("27d50a", "0x8048000"),
        ] {
            let block = compile(hex, rip, "32");
            admit(&block, true).unwrap_or_else(|e| panic!("{hex} at {rip}: {e}"));
            let e = admit(&block, false).expect_err("32-bit code admitted as 64-bit code");
            assert!(e.contains("profile or settings"), "{hex}: {e}");
        }
        let e = admit(&compile("4889d8c3", "0x8048000", "64"), true).expect_err("64-bit code admitted as 32-bit code");
        assert!(e.contains("profile or settings"), "{e}");
        // The link points of the wrapping exits carry the wrapped RIPs (0, not 4 GiB).
        assert_eq!(admit(&compile("7402ebfe", "0xfffffffc", "32"), true), Ok(2));
        // fexc's one form of a 32-bit guest-RIP move, `movz w; movk w, lsl #16; nop` (zero halves too: a call at 0 pushes 5,
        // a jmp to 0x10000 has a zero low half): the `nop` belongs to it, so a move without it is refused, with the code
        // shape check and without.
        for (hex, rip) in [("e800000000", "0x8048000"), ("e800000000", "0x0"), ("e9fbffffff", "0x10000")] {
            let (o, source) = compile(hex, rip, "32");
            let h: Header = super::super::record(&o, 0);
            let word = |o: &[u8], at: u32| u32::from_le_bytes(o[(h.code_offset + at) as usize..][..4].try_into().unwrap());
            let moves: Vec<Reloc> = (0..h.relocs_count as usize)
                .map(|i| super::super::record::<Reloc>(&o, h.relocs_offset as usize + 16 * i))
                .filter(|r| r.kind == object::RELOC_GUEST_RIP_MOVE)
                .collect();
            assert!(!moves.is_empty(), "{hex} at {rip}");
            for r in moves {
                let [z, k] = natemu_abi::execution::guest_rip_move32(r.value, r.register.into());
                assert_eq!([word(&o, r.offset), word(&o, r.offset + 4), word(&o, r.offset + 8)], [z, k, 0xd503_201f], "{hex} at {rip}");
                for shape in [true, false] {
                    let p = patched((&o, &source), r.offset + 8, 0xd503_201f, MOV_X0);
                    let e = super::super::admit_mode(&p, h.guest_rip, &source, u64::MAX, shape, false, true).expect_err("an unpadded move");
                    assert!(e.contains("guest-RIP move at"), "{hex} at {rip}: {e}");
                }
            }
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// `fixture` with the code word at `at` (which must be `old`) replaced by `new`.
    fn patched(fixture: (&[u8], &[u8]), at: u32, old: u32, new: u32) -> Vec<u8> {
        let h: Header = super::super::record(fixture.0, 0);
        let mut object = fixture.0.to_vec();
        let word = &mut object[(h.code_offset + at) as usize..][..4];
        assert_eq!(
            u32::from_le_bytes(word.try_into().unwrap()),
            old,
            "word at {at:#x}"
        );
        word.copy_from_slice(&new.to_le_bytes());
        object
    }
    /// `fixture` patched: refused with `why` in the error (with the code-shape check).
    fn refused(fixture: (&[u8], &[u8]), at: u32, old: u32, new: u32, why: &str) {
        let e = admit((&patched(fixture, at, old, new), fixture.1))
            .expect_err(&format!("{new:#010x} at {at:#x} was admitted"));
        assert!(e.contains(why), "{new:#010x} at {at:#x}: {e}");
    }
    /// `fixture` patched: refused with `why` by object validation alone (`why` = None: admitted without the code-shape check).
    fn validated(fixture: (&[u8], &[u8]), at: u32, old: u32, new: u32, why: Option<&str>) {
        match (
            admit_with((&patched(fixture, at, old, new), fixture.1), false),
            why,
        ) {
            (Ok(()), None) => {}
            (Err(e), Some(why)) => assert!(e.contains(why), "{new:#010x} at {at:#x}: {e}"),
            (r, _) => panic!("{new:#010x} at {at:#x}: {r:?}, want {why:?}"),
        }
    }
    /// `mov x0, x0`: a harmless word to put in place of another.
    const MOV_X0: u32 = 0xaa00_03e0;
    const ADDRESS: (&[u8], &[u8]) = FIXTURES[0];
    const DIVISION: (&[u8], &[u8]) = FIXTURES[1];
    /// `call rsp`: branch-target check at 0x4, `mov x0, x22` at 0x34, the dispatch branch at 0x38.
    const CALL: (&[u8], &[u8]) = FIXTURES[2];
    /// A word in the middle of a straight-line block (`mov x4, x20` after the first load).
    const MIDDLE: (u32, u32) = (0xc, 0xaa14_03e4);

    fn key_modes<'a>(modes: [bool; 2], keys: [&'a [&'a str]; 4]) -> Vec<(bool, &'a [&'a str])> {
        modes.iter().flat_map(|&m| keys.iter().map(move |&k| (m, k))).collect()
    }
    /// AFP fallback chunk 3a: with `FEXC` set, the code of the software DAZ and FTZ flushes (and of the unmasked-PE check under
    /// FTZ) passes admission with the code-shape check, as every key and unmasked-exception combination of these forms compiles.
    #[test]
    fn the_flush_code_is_admitted() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let forms = [
            "0f58ca", "660f5cca", "f30f59ca", "f20f5eca", "0f51ca", "0f5dca", "f20f5fca", "f20f7cca", "660fd0ca", "660f3a40caff",
            "0fc2ca01", "0f2fca", "660f2eca", "0f5aca", "660f5aca", "f30f5aca", "f20f5aca", "660f5bca", "f2480f2cc2", "660f3a08ca04",
            "0f53ca", "f30f52ca", "c4e269b8cb", "c4e2e9afcb", "c5ec59cb", "c5ed5ecb", "c4e3791dd100", "c5fd5aca", "c5fc5aca", "c5ed7ccb",
            // chunk 5: the negating and sign-mask multiplies (ADDSUB, FMSUB, FNMADD, FMADDSUB, FMSUBADD; xmm and ymm) in accurate DAZ + FTZ
            "c5e9d0cb", "c5edd0cb", "c4e269bacb", "c4e26dbacb", "c4e269bccb", "c4e269b6cb", "c4e2e9b7cb", "f30f58ca", "f20f58ca", "660f58ca",
        ];
        let dir = std::env::temp_dir().join(format!("natemu-flush-admit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        for hex in forms {
            let source: Vec<u8> = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect();
            // (chunk 5: accurate mode too: DAZ + FTZ there is FZ = 0 with the software input flush, and must not spill)
            for (accurate, keys) in key_modes([false, true], [&[][..], &["--daz"], &["--ftz"], &["--daz", "--ftz"]]) {
                for unmasked in ["0", "0x20", "0x3f"] {
                    let object = dir.join("o");
                    let json = dir.join("j");
                    let out = std::process::Command::new(&fexc)
                        .args(["--hex", hex, "--rip", "0x1000", "--profile", "slice1", "--feature", "afp=0", "--simd-unmasked", unmasked])
                        .args(keys)
                        .args(accurate.then_some("--accurate"))
                        .arg("--object")
                        .arg(&object)
                        .arg("--out")
                        .arg(&json)
                        .output()
                        .unwrap();
                    assert!(out.status.success(), "{hex} {keys:?}");
                    let report: serde_json::Value = serde_json::from_slice(&std::fs::read(&json).unwrap()).unwrap();
                    assert!(!accurate || report["code"]["layout"]["spill_bytes"] == 0, "{hex} {keys:?} {unmasked}: a spill");
                    let object = std::fs::read(&object).unwrap();
                    admit((&object, &source)).unwrap_or_else(|e| panic!("{hex} {keys:?} {accurate} {unmasked}: {e}"));
                }
            }
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }
    /// AFP accurate plan chunk 5 (R3-3): in accurate DAZ + FTZ the sign-mask and negating multiplies (ADDSUB, FMSUB, FNMADD, FMADDSUB) are never
    /// flushed by the resident, so row 4's input flush (`bic`, the mask's last word) must come before the first multiply: a denormal into one would
    /// trap with a tiny result. Fast mode (FZ = 1, no flush at all) and the AFP-less build with DAZ + FTZ outside accurate mode are not changed.
    #[test]
    fn accurate_daz_and_ftz_flush_the_sources_before_the_negating_multiplies() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let dir = std::env::temp_dir().join(format!("natemu-daz-ftz-order-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let object = dir.join("o");
        let words = |hex: &str, args: &[&str]| -> Vec<u32> {
            let out = std::process::Command::new(&fexc)
                .args(["--hex", hex, "--rip", "0x1000", "--profile", "slice1", "--feature", "afp=0"])
                .args(args)
                .arg("--object")
                .arg(&object)
                .arg("--out")
                .arg("/dev/null")
                .output()
                .unwrap();
            assert!(out.status.success(), "{hex} {args:?}");
            let o = std::fs::read(&object).unwrap();
            let h: natemu_abi::code_object::Header = super::super::record(&o, 0);
            o[h.code_offset as usize..][..h.code_region_end as usize].chunks_exact(4).map(|w| u32::from_le_bytes(w.try_into().unwrap())).collect()
        };
        let is_fmul = |w: u32| w & 0xbfa0_fc00 == 0x2e20_dc00;
        let is_bic = |w: u32| w & 0xffe0_fc00 == 0x4e60_1c00;
        for hex in ["660fd0ca", "c5e9d0cb", "c4e269bacb", "c4e269b6cb", "c4e26dbacb"] {
            let w = words(hex, &["--accurate", "--daz", "--ftz"]);
            let (m, b) = (w.iter().position(|&x| is_fmul(x)).expect("a multiply"), w.iter().position(|&x| is_bic(x)).expect("a flush"));
            assert!(b < m, "{hex}: the first flush at {b} is after the first multiply at {m}");
            // every sign-mask multiply reads a register written by an earlier `bic` (the flush); the other operand is the constant (`fmov`)
            for (i, &x) in w.iter().enumerate().filter(|&(_, &x)| is_fmul(x)) {
                let (rn, rm) = (x >> 5 & 31, x >> 16 & 31);
                let flushed = |r: u32| w[..i].iter().any(|&y| is_bic(y) && y & 31 == r);
                assert!(flushed(rn) || flushed(rm), "{hex}: the multiply at {i} has no flushed source");
            }
            // FCADD is a Keep word in the handler's table: accurate mode must never emit it (its exact tiny lanes would trap unflushed)
            assert!(!w.iter().any(|&x| x & 0xbf20_ec00 == 0x2e00_e400), "{hex}: fcadd");
            // the other keys and modes: no flush at all with DAZ + FTZ in fast mode (FZ = 1), and accurate FTZ alone flushes outputs only
            assert!(!words(hex, &["--daz", "--ftz"]).iter().any(|&x| is_bic(x)), "{hex}: fast mode has no flush");
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }
    /// AFP accurate plan chunk 4 (R4-2), the exhaustive admission and no-spill test of the FMA NaN check: with `FEXC` set, every FMA form
    /// (vfmadd, vfmsub, vfnmadd, vfnmsub, vfmaddsub, vfmsubadd; 132, 213, 231; ps, pd, ss, sd where they exist; xmm and ymm; register and memory
    /// operand) under every key (none, DAZ, FTZ, both) and unmasked set, compiled alone in accurate mode without AFP, has no spill area, one
    /// check site per 128-bit FMA, and passes admission with the code-shape check; mutated checks are refused (the destination register, a
    /// compared register that is a temporary, a branch target). Fast mode has no check.
    #[test]
    fn the_fma_check_code_is_admitted_for_every_fma_form_and_key() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        // (opcode of the packed form, has a scalar form, is an add-sub form): the scalar opcode is the packed one + 1
        let families: [(u8, bool); 6] = [(0x98, true), (0x9a, true), (0x9c, true), (0x9e, true), (0x96, false), (0x97, false)];
        // (hex, two 128-bit halves): register forms also with the destination aliasing each source (vvvv and rm = xmm1), scalar forms also VEX.L1
        // (ignored) and W1
        let mut forms: Vec<(String, bool, bool)> = Vec::new();
        for (base, scalar) in families {
            for order in [0x00u8, 0x10, 0x20] {
                for w in [false, true] {
                    for (ymm, mem) in [(false, false), (false, true), (true, false), (true, true)] {
                        for sc in [false, true] {
                            if sc && !scalar {
                                continue;
                            }
                            let operands: &[(u8, u8)] = if mem { &[(2, 0x0b)] } else { &[(2, 3), (1, 3), (2, 1), (1, 1), (2, 2)] };
                            for &(vvvv, rm) in operands {
                                // VEX.66.0F38.W: c4 e2 [W vvvv L 01] opcode modrm (xmm1 = dest, xmm<vvvv>, xmm<rm> or [rbx])
                                let vex = (w as u8) << 7 | ((!vvvv & 15) << 3) | (ymm as u8) << 2 | 1;
                                let (opcode, modrm) = (base + order + sc as u8, if mem { rm } else { 0xc8 | rm });
                                forms.push((format!("c4e2{vex:02x}{opcode:02x}{modrm:02x}"), ymm && !sc, mem || (vvvv, rm) == (2, 3)));
                            }
                        }
                    }
                }
            }
        }
        assert!(forms.len() >= 400, "{}", forms.len());
        let dir = std::env::temp_dir().join(format!("natemu-fma-admit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (object, json) = (dir.join("o"), dir.join("j"));
        let compile = |hex: &str, args: &[&str]| -> (Vec<u8>, Vec<u64>) {
            let out = std::process::Command::new(&fexc)
                .args(["--hex", hex, "--rip", "0x1000", "--profile", "slice1", "--feature", "afp=0"])
                .args(args)
                .arg("--object")
                .arg(&object)
                .arg("--out")
                .arg(&json)
                .output()
                .unwrap();
            assert!(out.status.success(), "{hex} {args:?}");
            let report: serde_json::Value = serde_json::from_slice(&std::fs::read(&json).unwrap()).unwrap();
            // (fast mode is only compared for its sites: its blocks with IE and DE both unmasked spill today, a case the host retries)
            assert!(report["code"]["layout"]["spill_bytes"] == 0 || !args.contains(&"--accurate"), "{hex} {args:?}: a spill");
            let at = report["sites"].as_array().unwrap().iter().filter(|s| s["kind"] == "fma").map(|s| s["native_offset"].as_u64().unwrap()).collect();
            (std::fs::read(&object).unwrap(), at)
        };
        let mut checked = 0;
        for (hex, ymm, full) in &forms {
            let (hex, ymm) = (hex.as_str(), *ymm);
            let source: Vec<u8> = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect();
            // (the aliasing register forms: the plain key and the heaviest one)
            for keys in [&[][..], &["--daz"], &["--ftz"], &["--daz", "--ftz"]] {
                for unmasked in ["0", "0x1", "0x10", "0x3f"] {
                    if !full && !(keys.is_empty() && unmasked == "0" || keys.len() == 2 && unmasked == "0x3f") {
                        continue;
                    }
                    let mut args = vec!["--accurate", "--simd-unmasked", unmasked];
                    args.extend_from_slice(keys);
                    let (obj, sites) = compile(hex, &args);
                    assert_eq!(sites.len(), if ymm { 2 } else { 1 }, "{hex} {args:?}");
                    admit((&obj, &source)).unwrap_or_else(|e| panic!("{hex} {args:?}: {e}"));
                    checked += 1;
                    // fast mode: no check
                    let fast: Vec<&str> = args.iter().copied().filter(|a| *a != "--accurate").collect();
                    assert!(compile(hex, &fast).1.is_empty(), "{hex}");
                }
            }
        }
        assert!(checked >= 3000, "{checked}");
        // mutations of one check
        let hex = "c4e269b8cb";
        let source = vec![0xc4, 0xe2, 0x69, 0xb8, 0xcb];
        let (obj, sites) = compile(hex, &["--accurate"]);
        let h: natemu_abi::code_object::Header = super::super::record(&obj, 0);
        let at = sites[0] as usize;
        let word = u32::from_le_bytes(obj[h.code_offset as usize + at..][..4].try_into().unwrap());
        assert_eq!(word & 0xffff_fc1f, 0x6ea0_c800 | 1, "fcmge.4s v1, vR, #0");
        let with = |new: u32| {
            let mut o = obj.clone();
            o[h.code_offset as usize + at..][..4].copy_from_slice(&new.to_le_bytes());
            admit((&o, &source))
        };
        admit((&obj, &source)).unwrap();
        for (new, why) in [
            (word & !31 | 5, "not `fcmge v1"),           // the destination is another register
            (word & !(31 << 5) | 1 << 5, "not `fcmge v1"), // the compared register is a temporary
            (word | 1 << 22, "not `fcmge v1"),           // .2d for a single-precision FMA
            (word & !(1 << 30), "not `fcmge v1"),        // .2s
            (0xd503_201f, "unrecognised instruction"),   // a nop (no decoded class)
        ] {
            let e = with(new).expect_err(&format!("{new:#x}"));
            assert!(e.contains(why), "{new:#x}: {e}");
        }
        // v1 read after the check (the word at +8 is `orr v17.16b, v2.16b, v2.16b`, the FMA result's move to the guest register)
        let moved = u32::from_le_bytes(obj[h.code_offset as usize + at + 8..][..4].try_into().unwrap());
        assert_eq!(moved, 0x4ea2_1c51, "orr v17.16b, v2.16b, v2.16b");
        let with_next = |new: u32| {
            let mut o = obj.clone();
            o[h.code_offset as usize + at + 8..][..4].copy_from_slice(&new.to_le_bytes());
            admit((&o, &source))
        };
        for (new, what) in [
            (0x4ea1_1c31, "Rn"),                                  // orr v17.16b, v1.16b, v1.16b
            (0x4e21_d451, "Rm"),                                  // fadd v17.4s, v2.4s, v1.4s
            (0x6e63_1c41, "an accumulating destination"),         // bsl v1.16b, v2.16b, v3.16b
            (0x4e0c_1c01, "ins"),                                // mov v1.s[1], w0 (keeps the other lanes)
            (0x3d80_0000 | 13 << 10 | 28 << 5 | 1, "a store of q1"), // str q1, [x28, #0xd0]
        ] {
            let e = with_next(new).expect_err(what);
            assert!(e.contains("v1 is read"), "{what} {new:#x}: {e}");
        }
        // a write of v1 first ends the scan: `orr v1.16b, v2.16b, v2.16b` then a read is fine
        assert!(with_next(0x4ea2_1c41).is_ok());
        // the record: a site naming another instruction, a temporary as an operand, an extra descriptor slot, an address register
        let site_at = h.sites_offset as usize + sites_index(&obj, &h, at as u32) * std::mem::size_of::<Site>();
        let with_site = |f: &dyn Fn(&mut Site)| {
            let mut o = obj.clone();
            let mut st: Site = super::super::record(&o, site_at);
            f(&mut st);
            // SAFETY: `Site` is plain data (repr(C)), written back over its own bytes.
            let bytes = unsafe { std::slice::from_raw_parts((&raw const st).cast::<u8>(), std::mem::size_of::<Site>()) };
            o[site_at..site_at + bytes.len()].copy_from_slice(bytes);
            admit((&o, &source))
        };
        let (want, ok) = ("FMA check", with_site(&|_| {}));
        ok.unwrap();
        for (f, what) in [
            (&(|s: &mut Site| s.guest_offset = 1) as &dyn Fn(&mut Site), "not in its instruction"),
            (&|s: &mut Site| s.imm = s.imm & !31 | 1, "a temporary as Rn"),
            (&|s: &mut Site| s.imm |= 1 << 15, "an extra descriptor slot"),
            (&|s: &mut Site| s.base_reg = 3, "a base register"),
            (&|s: &mut Site| s.index_reg = 3, "an index register"),
            (&|s: &mut Site| s.width |= tiny_half2(), "a second-half flag"),
        ] {
            let e = with_site(f).expect_err(what);
            assert!(e.contains(want) || e.contains("not in its instruction"), "{what}: {e}");
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }
    /// The index of the site at native offset `at` in the object's site table.
    fn sites_index(obj: &[u8], h: &natemu_abi::code_object::Header, at: u32) -> usize {
        (0..h.sites_count as usize)
            .find(|&i| super::super::record::<Site>(obj, h.sites_offset as usize + i * std::mem::size_of::<Site>()).native_offset == at)
            .unwrap()
    }
    fn tiny_half2() -> u8 {
        natemu_abi::tiny::HALF2
    }
    /// AFP fallback chunk 3b: with `FEXC` set, the code of the software denormal flag (rows 7 and 8: `and w1, wD, #0x80; orr w0, w0, w1`
    /// in the SIMD and x87 checks) and of the FMA sanitizing (row 10) passes admission with the code-shape check, with no spill, under
    /// every unmasked set that keys it (and the others), alone and with the flush keys; the same forms with AFP have the plain checks.
    #[test]
    fn the_software_flag_code_is_admitted() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let simd = [
            "f30f58ca", "0f58ca", "660f5eca", "f20f5eca", "0f51ca", "f20f5fca", "f20f7cca", "660f7dca", "f20fd0ca", "660fd0ca", "660f3a40caff",
            "0fc2ca01", "f30fc2ca00", "0f2fca", "660f2eca", "0f5aca", "660f5aca", "f30f5aca", "f20f5aca", "c4e3791dd100", "c5fc5aca", "c5fd5aca",
            "c5e858cb", "c5ed5ecb", "c4e269b8cb", "c4e269b9cb", "c4e2e9b8cb", "c4e2e9b9cb", "c4e269bacb", "c4e2e9bdcb", "c4e2e9becb", "c4e2e9bbcb",
            "c5ed7ccb", "c5ed7dcb", // memory operands, 256-bit forms (two halves, one check) and DPPS's steps follow
            "f30f580b", "0f580b", "0f5a0b", "f20f5e0b", "f30f510b", "c4e269b90b", "c5fc5a0b", "c5ec58cb", "c4e26db8cb", "c5ef7ccb", "c4e36d40cbff",
            "c5ecc2cb01", "c4e27913ca", "c4e27d13ca", "c4e2791300", // VCVTPH2PS: half-precision sources, no DE
            "660f3a40ca71", "660f3a41caff", "660f3a41ca31", "c4e36d40cbff", "c4e36941cbff", // DPPS, DPPD, VDPPS ymm: the add levels' DE inside VFAddV
            "c4e26998cb", "c4e269a8cb", "c4e269a9cb", "c4e2e9a8cb", "c4e2e999cb", "c4e26d98cb", // VFMADD132/213 (ps, ss, pd, sd, ymm)
            "c4e269a6cb", "c4e269a7cb", "c4e26d96cb", "c4e269b6cb", // VFMADDSUB/VFMSUBADD
            "c5efd0cb", "c5edd0cb",   // VADDSUBPS/PD ymm
            "f30f59ca", "f20f59ca", "660f59ca", "c5ec59cb", "c5e85ecb", "c4e2e9b9cb", "c5fd5aca", "f20f590b", // AFP fallback decision C: described stubs
        ];
        let x87 = [
            "d903", "dd03", "d803", "dc03", "d80b", "dc0b", "d813", "dc13", "d81b", "dc1b", "d823", "dc23", "d82b", "dc2b", "d833", "dc33", "d83b",
            "dc3b", "da03", "d9c0", "d8c1",
            // the NaN fix-up of AFP fallback (afp=0): registers, P forms, the destination aliasing a source, FSQRT, FLD
            "d8c9", "d8f1", "dcc1", "dce9", "dec1", "dec9", "def1", "d9fa", "d9e0",
        ];
        let dir = std::env::temp_dir().join(format!("natemu-soft-flag-admit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (object, json) = (dir.join("o"), dir.join("j"));
        let compile = |hex: &str, args: &[&str]| -> Vec<u8> {
            let out = std::process::Command::new(&fexc)
                .args(["--hex", hex, "--rip", "0x1000", "--profile", "slice1"])
                .args(args)
                .arg("--object")
                .arg(&object)
                .arg("--out")
                .arg(&json)
                .output()
                .unwrap();
            assert!(out.status.success(), "{hex} {args:?}");
            let report: serde_json::Value = serde_json::from_slice(&std::fs::read(&json).unwrap()).unwrap();
            assert_eq!(report["code"]["layout"]["spill_bytes"], 0, "{hex} {args:?}: a spill");
            std::fs::read(&object).unwrap()
        };
        for (forms, flag) in [(&simd[..], "--simd-unmasked"), (&x87[..], "--x87-unmasked")] {
            for hex in forms {
                let source: Vec<u8> = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect();
                // (accurate mode, AFP accurate plan chunk 2: the masked DE flag's `cbz; mrs; orr; msr` forward-skipped span; `--daz` drops it)
                for afp in [&["--feature", "afp=0"][..], &["--feature", "afp=0", "--accurate"], &[]] {
                    for unmasked in ["0x0", "0x1", "0x2", "0x3", "0x10", "0x3f"] {
                        for keys in [&[][..], &["--ftz"], &["--daz"], &["--daz", "--ftz"]] {
                            if afp.is_empty() && keys.contains(&"--daz") {
                                continue;
                            }
                            let args = [afp, &[flag, unmasked][..], keys].concat();
                            let object = compile(hex, &args);
                            admit((&object, &source)).unwrap_or_else(|e| panic!("{hex} {args:?}: {e}"));
                        }
                    }
                }
            }
        }
        // The NaN fix-up's `b` (between the mask and the `add`) retargeted to the slot store skips the `add`: refused.
        // (Accurate mode's code: fast mode without AFP runs x87 in runs and has no fix-up.)
        let object = compile("d8c9", &["--feature", "afp=0", "--accurate"]);
        let h: Header = super::super::record(&object, 0);
        let word = |i: usize| u32::from_le_bytes(object[h.code_offset as usize + 4 * i..][..4].try_into().unwrap());
        let words = (object.len() - h.code_offset as usize) / 4;
        let store = (0..words).rev().find(|&i| word(i) & 0xffff_ffe0 == 0xfd02_1000).expect("str dN, [x0, #0x420]");
        let b = (0..store).rev().find(|&i| word(i) & 0xfc00_0000 == 0x1400_0000).expect("the fix-up's b");
        refused(
            (&object, &[0xd8, 0xc9]),
            4 * b as u32,
            word(b),
            0x1400_0000 | (store - b) as u32,
            "unrecorded memory access",
        );
        // AFP accurate plan chunk 2b: the masked-DE screen's `cbz` skips the exact test forward; retargeted one word into the unmasked-IE check's
        // window (past its `mrs x0, fpsr`) it enters the check from before it: refused. (Left as built it is admitted: the loop above.)
        let hex = [0x0f, 0x58, 0xca];
        let object = compile("0f58ca", &["--feature", "afp=0", "--accurate", "--simd-unmasked", "0x1"]);
        let h: Header = super::super::record(&object, 0);
        let word = |i: usize| u32::from_le_bytes(object[h.code_offset as usize + 4 * i..][..4].try_into().unwrap());
        let words = (object.len() - h.code_offset as usize) / 4;
        let target = |i: usize| i as i64 + sext(word(i) >> 5 & 0x7_ffff, 19);
        let mrs = (0..words).rev().find(|&i| word(i) == 0xd53b_4420).expect("mrs x0, fpsr");
        // the screen's own `cbz w0` (chunk 2's flag branch, `cbz w20`, also targets the check: not that one)
        let skip = (0..mrs).find(|&i| word(i) & 0xff00_001f == 0x3400_0000 && (i as i64) < target(i) && target(i) <= mrs as i64)
            .expect("the screen's cbz w0");
        let retargeted = word(skip) & !(0x7_ffff << 5) | ((mrs as i64 + 1 - skip as i64) as u32 & 0x7_ffff) << 5;
        refused((&object, &hex), 4 * skip as u32, word(skip), retargeted, "floating-point stub");
        std::fs::remove_dir_all(&dir).unwrap();
    }
    /// The in-block skip of the masked DE screen (accurate mode without AFP): with `FEXC` set, a block whose later instruction computes on an
    /// earlier instruction's result has the `DENORM` check (`ldrb w0, [x28, #DE_PENDING]; cbnz w0, stub; brk #9`) and is admitted under every
    /// key; fast mode, DAZ, FTZ and DE unmasked have none; a check that reads another byte, or whose stub is reached by another branch, is refused.
    #[test]
    fn the_denormal_check_is_admitted() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        // subsd, addsd, mulsd (scalar chain); addps, mulps, subps (packed); addsd, movaps, mulsd (a copy); addss, addpd (sizes differ: no skip)
        let chains = ["f20f5ccaf20f58c9f20f59c9", "0f58ca0f59c90f5cc9", "f20f58caf20f28d9f20f59d9", "f30f58ca660f58c9"];
        let dir = std::env::temp_dir().join(format!("natemu-denorm-admit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (object, json) = (dir.join("o"), dir.join("j"));
        let compile = |hex: &str, args: &[&str]| -> Vec<u8> {
            let out = std::process::Command::new(&fexc)
                .args(["--hex", hex, "--rip", "0x1000", "--profile", "slice1"])
                .args(args)
                .arg("--object")
                .arg(&object)
                .arg("--out")
                .arg(&json)
                .output()
                .unwrap();
            assert!(out.status.success(), "{hex} {args:?}");
            let report: serde_json::Value = serde_json::from_slice(&std::fs::read(&json).unwrap()).unwrap();
            assert_eq!(report["code"]["layout"]["spill_bytes"], 0, "{hex} {args:?}: a spill");
            std::fs::read(&object).unwrap()
        };
        let denorm_sites = |object: &[u8]| -> usize {
            let h: Header = super::super::record(object, 0);
            (0..h.sites_count as usize)
                .filter(|&i| super::super::record::<Site>(object, h.sites_offset as usize + i * std::mem::size_of::<Site>()).kind == natemu_abi::code_object::kind::DENORM)
                .count()
        };
        let mut seen = 0;
        for hex in chains {
            let source: Vec<u8> = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect();
            for afp in [&["--feature", "afp=0"][..], &["--feature", "afp=0", "--accurate"], &[]] {
                for unmasked in ["0x0", "0x1", "0x2", "0x10", "0x3f"] {
                    for keys in [&[][..], &["--ftz"], &["--daz"], &["--daz", "--ftz"]] {
                        if afp.is_empty() && keys.contains(&"--daz") {
                            continue;
                        }
                        let args = [afp, &["--simd-unmasked", unmasked][..], keys].concat();
                        let object = compile(hex, &args);
                        admit((&object, &source)).unwrap_or_else(|e| panic!("{hex} {args:?}: {e}"));
                        let accurate_skip = afp.contains(&"--accurate") && keys.is_empty() && matches!(unmasked, "0x0" | "0x1" | "0x10");
                        if !accurate_skip {
                            assert_eq!(denorm_sites(&object), 0, "{hex} {args:?}: a denormal check outside accurate mode with DE masked and no DAZ or FTZ");
                        }
                        seen += denorm_sites(&object);
                    }
                }
            }
        }
        assert!(seen >= 6, "{seen} denormal checks");
        // The check's byte: another x28 field (the x87 PE byte) is no denormal check: refused.
        let hex = [0xf2, 0x0f, 0x5c, 0xca, 0xf2, 0x0f, 0x58, 0xc9];
        let object = compile("f20f5ccaf20f58c9", &["--feature", "afp=0", "--accurate"]);
        let h: Header = super::super::record(&object, 0);
        let word = |i: usize| u32::from_le_bytes(object[h.code_offset as usize + 4 * i..][..4].try_into().unwrap());
        let words = (object.len() - h.code_offset as usize) / 4;
        let check = (0..words).find(|&i| word(i) == ldr_state(1, 0, frame::DE_PENDING)).expect("the check's ldrb");
        refused((&object, &hex), 4 * check as u32, word(check), ldr_state(1, 0, frame::X87_PE), "floating-point stub");
        std::fs::remove_dir_all(&dir).unwrap();
    }
    #[test]
    fn real_blocks_are_admitted() {
        for (i, &f) in FIXTURES.iter().enumerate() {
            admit(f).unwrap_or_else(|e| panic!("fixture {i}: {e}"));
        }
    }

    #[test]
    fn decoder_and_bitmask() {
        assert!(
            matches!(decode(0xf940_0a94, 0x1c), Some(Insn::Mem(m)) if m.load && m.size == 8 && m.addr == Addr { base: 20, index: None, imm: 16 })
        );
        assert!(
            matches!(decode(0xf81f_0d14, 0), Some(Insn::Mem(m)) if m.form == Form::Pre && m.delta == -16)
        ); // str x20, [x8, #-16]!
        assert!(matches!(decode(0xd100_03ff, 0), Some(Insn::Data(Some(31))))); // sub sp, sp, #0
        assert!(matches!(decode(0xeb00_001f, 0), Some(Insn::Data(None)))); // cmp x0, x0
        assert!(matches!(decode(SVC_LINK, 0), Some(Insn::Svc(svc::LINK))));
        // ret eret hvc nop tlbi ic ldp (pre-index; I5a decodes the offset form) ldar bl (E1's stop placeholder `bl .` too) br blr
        for refused in [
            0xd65f_03c0,
            0xd69f_03e0,
            0xd400_0002,
            0xd503_201f,
            0xd508_871f,
            0xd50b_7520,
            0xa9c0_0780,
            0xc8df_fc00,
            0x9400_0001,
            0x9400_0000,
            0xd61f_0020,
            0xd63f_0060,
        ] {
            assert!(decode(refused, 0).is_none(), "{refused:#x}");
        }
        // unallocated: add-ext option 01 / imm3 5, add-shifted ROR, sf=0 with N=1 or imm6=32, bitfield N != sf
        for unallocated in [
            0x8b60_0000,
            0x8b20_1400,
            0x8bc0_0000,
            0x1240_0000,
            0x0a00_8000,
            0x9300_0000,
            0x1340_0000,
        ] {
            assert!(decode(unallocated, 0).is_none(), "{unallocated:#x}");
        }
        assert_eq!(bitmask(0x320b_83f4), Some(0x0020_0020_0020_0020)); // orr w20, wzr, #0x200020 (replicated to 64 bits)
        assert_eq!(bitmask(0xd241_0080), Some(1 << 63)); // eor x0, x4, #0x8000000000000000
        assert_eq!(x86_divide(&[0x48, 0xf7, 0xf1]), Some((false, 64)));
        assert_eq!(x86_divide(&[0x66, 0x41, 0xf7, 0xf9]), Some((true, 16)));
        assert_eq!(x86_divide(&[0xf6, 0x33]), Some((false, 8)));
        assert_eq!(x86_divide(&[0xf7, 0xe1]), None); // mul ecx
        // R2 review: a REX chain (the last REX counts) and a REX before a legacy prefix (ignored), SDM Vol.2 2.2.1.
        assert_eq!(x86_divide(&[0x48, 0x41, 0xf7, 0xf1]), Some((false, 32))); // div r9d
        assert_eq!(x86_divide(&[0x41, 0x4c, 0xf7, 0xf9]), Some((true, 64))); // idiv rcx
        assert_eq!(x86_divide(&[0x48, 0x66, 0xf7, 0xf1]), Some((false, 16))); // div cx
        assert!(lock_cmpxchg(&[0xf0, 0x41, 0x48, 0x0f, 0xb1, 0x0a]));
        assert_eq!(x86_x87_helper(&[0x41, 0x40, 0xd9, 0xfe]), Some(0xfe));
        assert_eq!(
            x86_string_compare(&[0x66, 0x41, 0x4c, 0x0f, 0x3a, 0x60, 0xca, 0x7f]),
            Some(0x7f | 1 << 8 | natemu_abi::strings::EXPLICIT)
        );
        assert_eq!(
            x86_string_compare(&[0x66, 0x41, 0xc4, 0xe3, 0x79, 0x63, 0xca, 0x7f]),
            None
        ); // REX before VEX: #UD, not this
    }

    #[test]
    fn lock_cmpxchg_barrier_pairs() {
        const LOCK: (&[u8], &[u8]) = FIXTURES[9];
        const DMB_ISH: u32 = 0xd503_3bbf;
        // Each LOCK CMPXCHG* CAS needs its `dmb ish` right before it (code shape) ...
        refused(LOCK, 0x18, DMB_ISH, MOV_X0, "without the dmb ish");
        refused(LOCK, 0x54, DMB_ISH, MOV_X0, "without the dmb ish");
        validated(LOCK, 0x18, DMB_ISH, MOV_X0, None);
        // ... and a `dmb ish` anywhere else is refused, also by object validation alone: before the unlocked cmpxchg8b's
        // caspal, before LOCK XADD's ldaddal, in a straight-line block.
        for (f, at, old) in [
            (LOCK, 0x2c, 0xaa05_03f5),
            (LOCK, 0x74, 0x2a04_03f4),
            (ADDRESS, MIDDLE.0, MIDDLE.1),
        ] {
            refused(f, at, old, DMB_ISH, "dmb ish that is not right before");
            validated(
                f,
                at,
                old,
                DMB_ISH,
                Some("dmb ish that is not right before"),
            );
        }
    }

    #[test]
    fn no_stop_check_and_no_bl() {
        // No block carries E1's stop placeholder `bl .`; it, the old stop store and the stop `brk` are refused anywhere.
        for (i, &f) in FIXTURES.iter().enumerate() {
            let h: Header = super::super::record(f.0, 0);
            let code = &f.0[h.code_offset as usize..][..h.code_region_end as usize];
            assert!(
                code.chunks(4).all(|w| w != 0x9400_0000u32.to_le_bytes()),
                "fixture {i}"
            );
        }
        let (at, old) = MIDDLE;
        refused(
            ADDRESS,
            ENTRY_OFFSET,
            0x8b07_0cd4,
            0x9400_0000,
            "unrecognised",
        ); // at the entry
        refused(ADDRESS, at, old, 0x9400_0000, "unrecognised");
        refused(ADDRESS, at, old, 0xf907_639f, "frame access"); // str xzr, [x28, #3776]
        refused(ADDRESS, at, old, natemu_abi::execution::STOP_WORD, "brk");
    }

    #[test]
    fn no_address_guards() {
        // The only `eor x0, xA, xA, lsl #16` is a computed exit's branch-target check, and no guard stub covers an access.
        for (i, &f) in FIXTURES.iter().enumerate() {
            let h: Header = super::super::record(f.0, 0);
            let code = &f.0[h.code_offset as usize..][..h.code_region_end as usize];
            let guards = code
                .chunks(4)
                .filter(|w| (0..31).any(|a| *w == eor_guard(a).to_le_bytes()))
                .count();
            assert_eq!(guards, usize::from(i == 2), "fixture {i}");
            let sites: Vec<Site> = (0..h.sites_count as usize)
                .map(|k| super::super::record(f.0, h.sites_offset as usize + k * size_of::<Site>()))
                .collect();
            assert!(
                sites
                    .iter()
                    .all(|s| s.kind != object::kind::GUARD || s.guest_offset2 == object::NONE32),
                "fixture {i}"
            );
        }
    }

    #[test]
    fn split_checks_are_verified() {
        refused(ADDRESS, 0x1c, 0x9100_1c20, 0x9100_0c20, "split check"); // add x0, x1, #3 for an 8-byte store
        refused(ADDRESS, 0x1c, 0x9100_1c20, MOV_X0, "split check");
        refused(ADDRESS, 0x18, 0x8b34_e0c1, 0x8b34_e4c1, "split check"); // add x1, x6, x20, sxtx #1: not its address
        refused(ADDRESS, 0x4, 0x8b07_0cd4, 0x1400_0006, "split check"); // b 0x1c: enters the checked window
        refused(ADDRESS, 0x24, 0x3760_0580, MOV_X0, "reached by 0 branches"); // tbnz removed
        let large = FIXTURES[5];
        refused(large, 0x20, 0x5284_0001, 0x5282_0001, "split check"); // mov w1, #0x1000 for a store at +0x2000
        // Without the code-shape check none of this is refused: a missing or wrong split check is a producer bug whose
        // effect (a page-crossing store committed in part) stays in the guest.
        validated(ADDRESS, 0x24, 0x3760_0580, MOV_X0, None);
        validated(ADDRESS, 0x1c, 0x9100_1c20, 0x9100_0c20, None);
    }

    /// Stack check runs (fexc --push-run): push r15; push r14; mov eax, esi; push r13; ret. One stack alignment check
    /// `and x0, x8, #7` (0x4) `cbnz x0` (0x8) before the first push (0xc), no check on the pushes at 0x10 and 0x18; `mov w4, w10` at 0x14.
    #[test]
    fn push_run_alignment_checks_are_verified() {
        const RUN: (&[u8], &[u8]) = fixture!("push-run");
        admit(RUN).unwrap();
        refused(RUN, 0x4, 0x9240_0900, MOV_X0, "stack alignment check"); // the check's `and` removed
        refused(RUN, 0x4, 0x9240_0900, 0x9240_0d00, "stack alignment check"); // and x0, x8, #15: not the alignment of an 8-byte push
        refused(RUN, 0x14, 0x2a0a_03e4, 0xaa0a_03e8, "covering its base"); // mov x8, x10 between the second and third push
        refused(RUN, 0x14, 0x2a0a_03e4, 0xd100_1108, "covering its base"); // sub x8, x8, #4: misaligns it
        validated(RUN, 0x4, 0x9240_0900, MOV_X0, None); // a producer bug, confined to the guest: object validation admits it
        // The check's base is x2 or above (x0 is the check's result; x1 may be written before the store): the same run with
        // the first push on base x0 (the `and` destroys it; object validation refuses) or x1 is refused at that push (0xc).
        for base in [0u32, 1] {
            let mut o = patched(RUN, 0x4, 0x9240_0900, 0x9240_0800 | base << 5);
            let h: Header = super::super::record(&o, 0);
            let code = h.code_offset as usize;
            o[code + 0xc..][..4].copy_from_slice(&(0xf81f_8d1d_u32 & !(31 << 5) | base << 5).to_le_bytes());
            for i in [0, 4] {
                o[h.sites_offset as usize + 40 * i + 25] = base as u8; // the access's and the stub's base_reg
            }
            let e = admit((&o, RUN.1)).unwrap_err();
            // (Object validation already refuses a stub on x0.)
            let want = if base == 0 { "check stub at native offset 0x34" } else { "stack alignment check" };
            assert!(e.contains(want) && (base == 0 || e.contains("access at 0xc")), "base x{base}: {e}");
        }
        // push rbp; mov rbp, rsp; push rbx; sub rsp, 0x18; mov [rsp+8], rax; mov [rbp-16], rcx; mov dword [rsp+4], 7;
        // mov [rsp+3], rax (split-checked); add rsp, 0x18; pop rbx; pop rbp; cmp al, 13; ret. `mov x9, x8` at 0x10 (RBP = RSP),
        // `subs x26, x8, #0x18; mov x8, x26` at 0x1c (RSP - 24), unchecked stores at 0x24 [x8, #8], 0x28 [x9, #-16], 0x30 [x8, #4].
        const FRAME: (&[u8], &[u8]) = fixture!("stack-frame");
        admit(FRAME).unwrap();
        refused(FRAME, 0x10, 0xaa08_03e9, 0xaa0a_03e9, "covering its base"); // mov x9, x10: RBP not known aligned
        refused(FRAME, 0x1c, 0xf100_611a, 0xf100_511a, "covering its base"); // subs x26, x8, #0x14: RSP - 20
        refused(FRAME, 0x8, 0xb500_0440, 0xb500_0480, "reached by"); // the cbnz no longer reaches the stub
        // fixtures random-07 with --push-run --narrow-cmp --ext-conds 4: a 64-bit div's helper path (`svc #DIV_U`, which clears
        // every register's alignment) and its inline path meet after the op. A fexc that dropped the helper path's set kept RSP
        // aligned and left the push at 0x184 unchecked (refused); the fixed one checks it again.
        let e = admit(fixture!("push-run-div-merge")).unwrap_err();
        assert!(e.contains("access at 0x184") && e.contains("covering its base"), "{e}");
        admit(fixture!("push-run-div-fixed")).unwrap();
    }

    #[test]
    fn guest_rip_addresses_are_verified() {
        let (rip, noncanonical) = (FIXTURES[3], FIXTURES[4]);
        refused(rip, 0x8, 0x72a0_0214, 0x72a0_0234, "guest-RIP move"); // movk w20, #0x11, lsl #16
        validated(rip, 0x8, 0x72a0_0214, 0x72a0_0234, Some("guest-RIP move")); // a relocation: object validation
        refused(rip, 0x1c, 0x1400_001d, MOV_X0, "reached by 0 branches"); // the crossing store's `b split`
        // A non-canonical guest-RIP load has no stub (it faults at its site); the crossing non-canonical store keeps
        // its `b split` (the resident's split raises #GP before writing).
        refused(
            noncanonical,
            0x24,
            0x1400_0006,
            MOV_X0,
            "reached by 0 branches",
        );
        refused(
            noncanonical,
            0xc,
            0xf2d0_0014,
            0xf2c0_0014,
            "guest-RIP move",
        ); // canonical, but not the relocation
        // `mov x4, x20` replaced by a branch over the store's move and `b split` to the store itself: the window is
        // sealed from the move, the `b` included.
        refused(
            noncanonical,
            0x14,
            0xaa14_03e4,
            0x1400_0005,
            "a branch enters between its guest-RIP move and the access",
        );
    }

    #[test]
    fn computed_exits_are_verified() {
        refused(CALL, 0x4, 0xca08_4100, 0xca09_4120, "branch-target guard"); // checks x9, exits through rsp's copy
        refused(CALL, 0x2c, 0xaa08_03f6, 0xaa09_03f6, "branch-target guard"); // mov x22, x9: an unchecked RIP
        validated(CALL, 0x2c, 0xaa08_03f6, 0xaa09_03f6, None); // x86 semantics: the code-shape check
        refused(
            CALL,
            0x34,
            0xaa16_03e0,
            0xaa16_03e1,
            "does not follow `mov x0, xR`",
        ); // mov x1, x22
        validated(
            CALL,
            0x34,
            0xaa16_03e0,
            0xaa16_03e1,
            Some("does not follow `mov x0, xR`"),
        ); // the exit's form
        refused(CALL, 0x34, 0xaa16_03e0, MOV_X0, "does not follow `mov x0, xR`");
        refused(
            CALL,
            0x38,
            DISPATCH_PLACEHOLDER,
            DISPATCH_PLACEHOLDER + 1,
            "placeholder",
        ); // a real branch
        validated(
            CALL,
            0x38,
            DISPATCH_PLACEHOLDER,
            DISPATCH_PLACEHOLDER + 1,
            Some("placeholder"),
        );
        refused(CALL, 0x38, DISPATCH_PLACEHOLDER, SVC_LINK, "placeholder"); // a literal exit's gate instead
        // The placeholder where no dispatch relocation names it.
        let (at, old) = MIDDLE;
        refused(ADDRESS, at, old, DISPATCH_PLACEHOLDER, "not forward");
        // Admission returns the dispatch branch.
        let h: Header = super::super::record(CALL.0, 0);
        let a = super::super::admit(CALL.0, h.guest_rip, CALL.1, u64::MAX, false, false).unwrap();
        assert_eq!((a.dispatch_exit, a.links.len()), (Some(0x38), 0));
    }

    /// `jcc` (`cmp rax, rcx; jne +0x40`): the taken exit's link point at 0x1c, the fall-through's at 0x10.
    const JCC: (&[u8], &[u8]) = FIXTURES[6];
    const LINK_FT: u32 = 0x10;
    const MOV_W0_5: u32 = 0x5280_00a0; // mov w0, #5 (then movk w0, #0x10, lsl #16: 0x100005)

    #[test]
    fn link_points_are_returned() {
        let h: Header = super::super::record(JCC.0, 0);
        let a = super::super::admit(JCC.0, h.guest_rip, JCC.1, u64::MAX, false, false).unwrap();
        let point = |offset, word, target| LinkPoint {
            offset,
            word,
            target,
        };
        assert_eq!(
            a.links,
            [
                point(LINK_FT, MOV_W0_5, h.guest_rip + 5),
                point(0x1c, 0x5280_08a0, h.guest_rip + 0x45)
            ]
        );
        assert_eq!(a.dispatch_exit, None);
    }

    /// Perf chunk 26: a bulk REP STOS loop (fexc `--bulk`: `xor rax, rax; rep stosq`, `mov al, 0xa5; rep stosb`) is admitted
    /// with and without the code shape; its back edge only with every word of FexcRepStos exact for the instruction's width.
    #[test]
    fn bulk_rep_stos_loops_are_pinned() {
        const STOSQ: (&[u8], &[u8]) = fixture!("rep-stosq-bulk");
        const STOSB: (&[u8], &[u8]) = fixture!("rep-stosb-bulk");
        let find = |f: (&[u8], &[u8]), word: u32| -> u32 {
            let h: Header = super::super::record(f.0, 0);
            let code = &f.0[h.code_offset as usize..][..h.code_region_end as usize];
            (0..code.len() as u32)
                .step_by(4)
                .find(|&o| code[o as usize..o as usize + 4] == word.to_le_bytes())
                .expect("word")
        };
        for f in [STOSQ, STOSB] {
            admit(f).unwrap();
            admit_with(f, false).unwrap();
            // The count decremented by 2 (it could step over zero), the page room in 8-byte units (a store past the page),
            // RDI stepped by 32, the back edge to the `add`, a skip into the loop: refused by object validation.
            let back = find(f, 0xd100_0421) + 4;
            for (at, old, new) in [
                (find(f, 0xd100_0421), 0xd100_0421, 0xd100_0821),
                (find(f, 0xd344_fc00), 0xd344_fc00, 0xd343_fc00),
                (find(f, 0x9100_416b), 0x9100_416b, 0x9100_816b),
                (back, 0xb5ff_ff81, 0xb5ff_ffa1),
            ] {
                validated(f, at, old, new, Some("not forward"));
            }
        }
        // The tbnz on DF retargeted to the `stp` (past the count): refused.
        let tbnz = find(STOSQ, 0x3940_0000 | frame::DF << 10 | 28 << 5) + 4;
        let stp = find(STOSQ, 0xa900_1164);
        validated(
            STOSQ,
            tbnz,
            0x3738_0220,
            0x3738_0000 | ((stp - tbnz) / 4) << 5,
            Some("not forward"),
        );
        // The loop under another width's bytes (REX.W made REX: `rep stosd`, four elements a chunk, not two): refused.
        let stosd = [0x48, 0x31, 0xc0, 0xf3, 0x40, 0xab];
        let e = admit_with((STOSQ.0, &stosd), false).unwrap_err();
        assert!(e.contains("not forward"), "{e}");
        // The stp moved off its site (`stp x4, x4, [x11, #16]`): its record no longer describes it.
        validated(STOSQ, stp, 0xa900_1164, 0xa901_1164, Some("not forward"));
        // Review: a forward branch from before the sequence (the XOR's word) into the loop body; the `cbz x1` (no chunk)
        // removed; min made max (`bic` for `and`, so the room no longer bounds the count); the fast-string test removed.
        let before = find(STOSQ, 0x3940_0000 | frame::FAST_STRINGS << 10 | 28 << 5) - 4;
        validated(
            STOSQ,
            before,
            0x5280_0004,
            0x1400_0000 | (stp - before) / 4,
            Some("not forward"),
        );
        validated(
            STOSQ,
            find(STOSQ, 0xb400_00c1),
            0xb400_00c1,
            MOV_X0,
            Some("not forward"),
        );
        validated(
            STOSQ,
            find(STOSQ, 0x8a82_fc42),
            0x8a82_fc42,
            0x8aa2_fc42,
            Some("not forward"),
        );
        let fast = find(STOSQ, 0x3940_0000 | frame::FAST_STRINGS << 10 | 28 << 5) + 4;
        validated(STOSQ, fast, 0x3400_0260, MOV_X0, Some("not forward"));
    }
    /// Bulk REP plan (9.1, section 14): a bulk REP MOVS sequence (fexc `--bulk`: `mov ecx, 64; rep movsq`, `mov ecx, 256;
    /// rep movsb`) is admitted with and without the code shape; its back edges only with every word of FexcRepMovs exact for
    /// the instruction's width and its four sites recorded as they are.
    #[test]
    fn bulk_rep_movs_loops_are_pinned() {
        const MOVSQ: (&[u8], &[u8]) = fixture!("rep-movsq-bulk");
        const MOVSB: (&[u8], &[u8]) = fixture!("rep-movsb-bulk");
        let word = |f: (&[u8], &[u8]), at: u32| u32::from_le_bytes(f.0[128 + at as usize..][..4].try_into().unwrap());
        for (f, w) in [(MOVSQ, 8u32), (MOVSB, 1)] {
            admit(f).unwrap();
            admit_with(f, false).unwrap();
            let h: Header = super::super::record(f.0, 0);
            assert_eq!(h.code_offset, 128);
            let (words, b, e, l) = super::bulk_movs_words(w);
            let start = (0..h.code_region_end - 8)
                .step_by(4)
                .find(|&o| word(f, o) == words[0].0 && word(f, o + 12) == words[3].0)
                .expect("the sequence");
            let at = |i: usize| start + 4 * i as u32;
            let idx = |x: u32| words.iter().position(|&(v, _)| v == x).unwrap();
            let refuse = |i: usize, new: u32| validated(f, at(i), word(f, at(i)), new, Some("not forward"));
            // Words changed: each room from the other pointer (one page's room twice), the min made max (`bic`), the overlap
            // mask 0xff and its bound 16, an access off its site, a pointer or RCX stepped by another amount, each back edge
            // one word on.
            refuse(idx(0x9240_2d60), 0x9240_2d40);
            refuse(idx(0x9240_2d40), 0x9240_2d60);
            refuse(idx(0x8a82_fc42), 0x8aa2_fc42);
            refuse(idx(0x9240_2c42), 0x9240_1c42);
            refuse(idx(0xd100_3c42), 0xd100_4042);
            refuse(b, 0xa941_0d42);
            refuse(l, words[l].0 | 1 << 10);
            refuse(l + 1, words[l + 1].0 | 1 << 10);
            refuse(b + 2, 0x9100_814a);
            refuse(l + 2, 0x9100_014a | (2 * w) << 10);
            refuse(b + 4, 0xd100_00e7 | (32 / w) << 10);
            refuse(l + 4, 0xd100_08e7);
            refuse(e - 1, word(f, at(e - 1)) + 0x20);
            refuse(words.len() - 1, word(f, at(words.len() - 1)) + 0x20);
            // Each skip removed: RCX < 2's, the fast-string bit's, DF's, the crossing element's, the rounds' count,
            // (MOVSQ) the alignment's, the overlap's, the element loop's entry; the overlap skip retargeted into the rounds.
            for (i, &(_, kind)) in words.iter().enumerate() {
                if matches!(kind, super::Movs::ToEnd | super::Movs::ToElements) {
                    refuse(i, MOV_X0);
                }
            }
            let tb = idx(0xd100_3c42) + 1;
            refuse(tb, 0xb7f8_0002 | ((at(b) - at(tb)) / 4) << 5);
            // A branch from outside (the word before the sequence) into the element loop.
            validated(f, start - 4, word(f, start - 4), 0x1400_0000 | (at(l) - (start - 4)) / 4, Some("not forward"));
            // The sites: the ldp with write intent or as a plain load, the element load as a store, the element store read.
            let sites = h.sites_offset as usize;
            let size = std::mem::size_of::<object::Site>();
            let site_at = |o: u32| {
                (0..h.sites_count as usize)
                    .find(|&i| super::super::record::<object::Site>(f.0, sites + i * size).native_offset == o)
                    .unwrap()
            };
            for (o, field, value) in [
                (at(b), std::mem::offset_of!(object::Site, intent), object::INTENT_WRITE),
                (at(b), std::mem::offset_of!(object::Site, kind), object::kind::LOAD),
                (at(l), std::mem::offset_of!(object::Site, kind), object::kind::STORE),
                (at(l + 1), std::mem::offset_of!(object::Site, intent), object::INTENT_READ),
            ] {
                let mut bad = f.0.to_vec();
                bad[sites + site_at(o) * size + field] = value;
                assert!(admit_with((&bad, f.1), false).is_err(), "site at {o:#x}, field {field}");
            }
        }
        // The code under other bytes: `rep movsd` (REX.W dropped: a 40 REX), or F2 for F3.
        for src in [&[0xb9, 0x40, 0, 0, 0, 0xf3, 0x40, 0xa5][..], &[0xb9, 0x40, 0, 0, 0, 0xf2, 0x48, 0xa5]] {
            let e = admit_with((MOVSQ.0, src), false).unwrap_err();
            assert!(e.contains("not forward"), "{e}");
        }
    }
    /// Gather plan (2.5): fexc's inline gathers (`--bulk`: `vpgatherdd ymm0, [rdi + ymm1*4], ymm2`, eight 4-byte elements;
    /// `vpgatherqq ymm0, [ymm1*8 + 0x40], ymm2`, four 8-byte ones with no base) are admitted with and without the code shape,
    /// as bulk objects (so a step's request refuses them, and no step holds one). With the code shape, each element load is
    /// pinned: a `tbz` moved past the `ldr` or testing another bit, a `umov` from w1, an `ldr` with an offset, a branch into
    /// the `ldr`, an unmarked element load, one site fewer, and the marks on an instruction that is no inline gather (other
    /// guest bytes, or a 0x67 or FS prefix) are refused; object validation admits a marked element load only of a 4- or 8-byte
    /// load into a vector register.
    #[test]
    fn gather_element_loads_are_pinned() {
        const DD: (&[u8], &[u8]) = fixture!("gather-dd-bulk");
        const QQ: (&[u8], &[u8]) = fixture!("gather-qq-bulk");
        let word = |f: (&[u8], &[u8]), at: u32| u32::from_le_bytes(f.0[128 + at as usize..][..4].try_into().unwrap());
        for (f, ew, n) in [(DD, 4u8, 8usize), (QQ, 8, 4)] {
            let h: Header = super::super::record(f.0, 0);
            assert_eq!(h.code_offset, 128);
            for shape in [true, false] {
                let a = super::super::admit(f.0, h.guest_rip, f.1, u64::MAX, shape, false).unwrap();
                assert!(a.bulk, "an inline gather makes a bulk object");
            }
            let size = std::mem::size_of::<object::Site>();
            let site = |i: usize| super::super::record::<object::Site>(f.0, h.sites_offset as usize + i * size);
            let elements: Vec<usize> = (0..h.sites_count as usize).filter(|&i| object::gather_element(&site(i))).collect();
            assert_eq!(elements.len(), n);
            assert!(elements.iter().all(|&i| (site(i).width, site(i).data_class) == (ew, object::CLASS_FPR)));
            let refuse = |at: u32, new: u32| refused(f, at, word(f, at), new, "gather at guest offset 0x0");
            for &i in &elements {
                let at = site(i).native_offset;
                let tbz = word(f, at - 4);
                refuse(at - 4, tbz + (1 << 5)); // over the ldr and the word after it
                refuse(at - 4, tbz - (1 << 19)); // bit 30|62
                refuse(at - 8, word(f, at - 8) | 1); // umov into w1|x1
                // ldr s|d, [xA, #4|#8]: no longer the recorded site (object validation)
                refused(f, at, word(f, at), word(f, at) | 1 << 10, "does not describe its instruction");
            }
            // A branch into the first element's `ldr` from the word before its `umov`.
            let at = site(elements[0]).native_offset;
            refuse(at - 12, 0x1400_0003);
            // The site table: an element unmarked, an element dropped (its row made a stub-less OTHER... an unmarked plain
            // load is all that is left of it), the mark on a 16-byte or a general-register load (object validation).
            let field = |i: usize, off: usize, bytes: &[u8]| {
                let mut o = f.0.to_vec();
                let at = h.sites_offset as usize + i * size + off;
                o[at..at + bytes.len()].copy_from_slice(bytes);
                o
            };
            let g2 = std::mem::offset_of!(object::Site, guest_offset2);
            let unmarked = field(elements[n / 2], g2, &object::NONE32.to_le_bytes());
            let e = admit((&unmarked, f.1)).unwrap_err();
            assert!(e.contains("gather at guest offset 0x0"), "{e}");
            // (Without the code shape the object stays a bulk one while any of its element loads is marked; with none marked
            // it is an ordinary one, whose loads a step may hold.)
            assert!(super::super::admit(&unmarked, h.guest_rip, f.1, u64::MAX, false, false).unwrap().bulk);
            let mut none = f.0.to_vec();
            for &i in &elements {
                let at = h.sites_offset as usize + i * size + g2;
                none[at..at + 4].copy_from_slice(&object::NONE32.to_le_bytes());
            }
            assert!(!super::super::admit(&none, h.guest_rip, f.1, u64::MAX, false, false).unwrap().bulk);
            for (off, value) in [(std::mem::offset_of!(object::Site, width), 16), (std::mem::offset_of!(object::Site, data_class), object::CLASS_GPR)] {
                let e = admit_with((&field(elements[0], off, &[value]), f.1), false).unwrap_err();
                assert!(e.contains("access site"), "{e}");
            }
            // The guest bytes: no gather (VEX.66.0F38 98, an FMA: same length), the VEX.L bit flipped (another element
            // count), a 0x67 or FS prefix (the cut's): refused with the code shape.
            let mut fma = f.1.to_vec();
            fma[3] = 0x98;
            let mut other_l = f.1.to_vec();
            other_l[2] ^= 4;
            for src in [fma, other_l] {
                let e = admit((f.0, &src)).unwrap_err();
                assert!(e.contains("gather at guest offset 0x0"), "{src:02x?}: {e}");
            }
            assert_eq!(super::x86_gather(&[&[0x67][..], f.1].concat()), None);
            assert_eq!(super::x86_gather(&[&[0x64][..], f.1].concat()), None);
            assert_eq!(super::x86_gather(&[&[0x3e][..], f.1].concat()), Some((ew, n as u32)));
        }
    }
    #[test]
    fn link_point_form_is_pinned() {
        let at = LINK_FT;
        refused(JCC, at, MOV_W0_5, MOV_W0_5 | 2, "guest-RIP move"); // mov w2: not the relocation's register
        refused(JCC, at + 8, SVC_LINK, SVC_DIV_U, "not a literal exit"); // the move then a division
        refused(JCC, at + 8, SVC_LINK, MOV_X0, "not a literal exit"); // no gate: falls through into the next exit
        refused(JCC, at + 4, 0x72a0_0200, 0x9100_0400, "guest-RIP move"); // add x0, x0, #1 instead of the movk
        // A branch into its later words (the b.ne at 0xc targets the taken exit's first word, 0x1c).
        refused(
            JCC,
            0xc,
            0x5400_0081,
            0x5400_0001 | (at + 4 - 0xc) / 4 << 5,
            "link point",
        );
        refused(
            JCC,
            0xc,
            0x5400_0081,
            0x5400_0001 | (at + 8 - 0xc) / 4 << 5,
            "link point",
        );
        // `svc #LINK` anywhere else: in a straight-line block, or as a computed exit's gate.
        let (mid, old) = MIDDLE;
        refused(ADDRESS, mid, old, SVC_LINK, "not exactly a link point");
        refused(
            CALL,
            0x38,
            DISPATCH_PLACEHOLDER,
            SVC_LINK,
            "dispatch branch",
        );
        validated(JCC, at + 8, SVC_LINK, MOV_X0, Some("not a literal exit")); // object validation
        validated(
            ADDRESS,
            mid,
            old,
            SVC_LINK,
            Some("not exactly a link point"),
        );
    }

    #[test]
    fn link_targets_must_be_proven() {
        // The fall-through exit moved to the block's own RIP (a plausible value, not this exit's target): code and
        // relocation agree.
        let h: Header = super::super::record(JCC.0, 0);
        let mut object = JCC.0.to_vec();
        let reloc = (0..h.relocs_count as usize)
            .map(|i| h.relocs_offset as usize + i * size_of::<Reloc>())
            .find(|&r| u32::from_le_bytes(object[r..r + 4].try_into().unwrap()) == LINK_FT)
            .unwrap();
        let set = |object: &mut Vec<u8>, value: u64, word: u32| {
            object[reloc + 8..reloc + 16].copy_from_slice(&value.to_le_bytes());
            object[(h.code_offset + LINK_FT) as usize..][..4].copy_from_slice(&word.to_le_bytes());
        };
        set(&mut object, h.guest_rip, 0x5280_0000); // mov w0, #0; movk w0, #0x10, lsl #16
        let e = admit((&object, JCC.1)).expect_err("unproven link target admitted");
        assert!(
            e.contains("neither the block end nor its branch target"),
            "{e}"
        );
        set(&mut object, h.guest_rip + 0x45, 0x5280_08a0);
        admit((&object, JCC.1)).unwrap(); // both exits to the taken target
        let mut source = JCC.1.to_vec();
        source[3] = 0x90; // nop instead of jne: the block's branch has no decoded target
        let e = admit((&object, &source)).expect_err("link target without a branch admitted");
        assert!(
            e.contains("neither the block end nor its branch target"),
            "{e}"
        );
        // I2: the last instruction's own RIP is a target only when that instruction is a REP string (its self-exit).
        set(&mut object, h.guest_rip + 3, 0x5280_0060); // mov w0, #3: the jne's own RIP
        let e =
            admit((&object, JCC.1)).expect_err("exit to a non-REP last instruction's RIP admitted");
        assert!(
            e.contains(&format!("literal {:#x} is neither", h.guest_rip + 3)),
            "{e}"
        );
        let mut source = JCC.1.to_vec();
        source[3..5].copy_from_slice(&[0xf3, 0xaa]); // rep stosb in the jne's place: its RIP becomes proven
        let e = admit((&object, &source)).expect_err("the taken exit has no branch to prove it");
        assert!(
            !e.contains(&format!("literal {:#x}", h.guest_rip + 3)),
            "{e}"
        );
    }

    /// Extended blocks: a conditional branch inside the unit exits only to its own taken target, not to another branch's
    /// target or the unit's end; the last instruction keeps the whole set.
    #[test]
    fn inner_exits_are_their_own_branch_targets() {
        const EXT: (&[u8], &[u8]) = FIXTURES[11];
        admit(EXT).unwrap();
        let h: Header = super::super::record(EXT.0, 0);
        let with = |exits: [(u32, u64); 2]| {
            let mut object = EXT.0.to_vec();
            for (at, value) in exits {
                let reloc = (0..h.relocs_count as usize)
                    .map(|i| h.relocs_offset as usize + i * size_of::<Reloc>())
                    .find(|&r| u32::from_le_bytes(object[r..r + 4].try_into().unwrap()) == at)
                    .unwrap();
                object[reloc + 8..reloc + 16].copy_from_slice(&value.to_le_bytes());
                let code = (h.code_offset + at) as usize;
                let (lo, hi) = (value as u32 & 0xffff, (value >> 16) as u32);
                object[code..code + 4].copy_from_slice(&(0x5280_0000 | lo << 5).to_le_bytes());
                object[code + 4..code + 8].copy_from_slice(&(0x72a0_0000 | hi << 5).to_le_bytes());
            }
            admit((&object, EXT.1))
        };
        // The first two taken exits swapped: both targets are in the unit's set, neither is its own branch's.
        let e = with([(0x4c, 0x40_102a), (0x58, 0x40_1015)]).expect_err("swapped targets admitted");
        assert!(e.contains("direct target of the branch"), "{e}");
        // A taken exit naming the unit's end.
        let e = with([(0x4c, 0x40_100e), (0x58, 0x40_102a)]).expect_err("exit to the end admitted");
        assert!(e.contains("direct target of the branch"), "{e}");
        // The last instruction's taken exit may name the unit's end (the whole set).
        with([(0x64, 0x40_100e), (0x4c, 0x40_1015)]).unwrap();
    }

    /// With `FEXC` set: a conditional's taken branch aimed at the start of the last instruction's code (the `jmp`'s exit, not its
    /// own) is refused: only the instruction right before may skip to the start of an instruction's code (the `ror rax, cl` test
    /// below). A branch instruction before the last one does not count as such a skip. And an exit no branch reaches (entered by
    /// falling through) belongs to the instruction holding it: the jmp's exit inside the `inc`'s code is refused.
    #[test]
    fn forged_exits_of_middle_instructions_are_refused() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let dir = std::env::temp_dir().join(format!("natemu-skip-admit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        // jz +0x20; inc rax; jmp +0x30: the jz's `b.eq` at 4 goes to its taken exit (40); forged, to the jmp's code (28).
        let source = [0x74, 0x20, 0x48, 0xff, 0xc0, 0xeb, 0x30];
        let hex: String = source.iter().map(|b| format!("{b:02x}")).collect();
        let path = dir.join("o");
        let out = std::process::Command::new(&fexc)
            .args(["--hex", &hex, "--rip", "0x401000", "--profile", "slice1", "--ext-conds", "4", "--push-run", "--narrow-cmp"])
            .arg("--object")
            .arg(&path)
            .arg("--out")
            .arg("/dev/null")
            .output()
            .unwrap();
        assert!(out.status.success(), "{hex}");
        let mut object = std::fs::read(&path).unwrap();
        std::fs::remove_dir_all(&dir).unwrap();
        let h: Header = super::super::record(&object, 0);
        let at = (h.code_offset + 4) as usize;
        let eq_taken = u32::from_le_bytes(object[at..at + 4].try_into().unwrap());
        assert_eq!(eq_taken, 0x5400_0120, "b.eq to the taken exit at 40");
        admit((&object, &source)).unwrap();
        object[at..at + 4].copy_from_slice(&0x5400_00c0u32.to_le_bytes()); // b.eq 28
        let e = admit((&object, &source)).expect_err("a jz skipping to the jmp's code admitted");
        assert!(e.contains("direct target of the branch"), "{e}");
        // The jmp's PC entry moved to its taken exit's offset (40): its own exit (28) lies in the `inc`'s code, entered by falling through.
        object[at..at + 4].copy_from_slice(&eq_taken.to_le_bytes());
        let pcs = h.pcs_offset as usize;
        let last = (0..h.pcs_count as usize).map(|i| pcs + i * size_of::<PcEntry>()).find(|&p| {
            u32::from_le_bytes(object[p + 4..p + 8].try_into().unwrap()) == 5
        });
        let last = last.expect("the jmp's PC entry");
        assert_eq!(u32::from_le_bytes(object[last..last + 4].try_into().unwrap()), 28);
        object[last..last + 4].copy_from_slice(&40u32.to_le_bytes());
        let e = admit((&object, &source)).expect_err("a fall-through exit of a middle instruction admitted");
        assert!(e.contains("reached by falling through"), "{e}");
    }

    /// With `FEXC` set, CPL-0 requests: the STI and VERW of a unit, mid-unit, exit to their own RIPs and are admitted (they and the
    /// unit's own end are the only instructions that retry at their own RIP: CLI and SWAPGS never exit), but a jz that exits to
    /// its own RIP is refused although another instruction branching there (the last `jmp`) puts that RIP in the unit's set.
    #[test]
    fn only_sti_and_verw_retry_at_their_own_rip() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let dir = std::env::temp_dir().join(format!("natemu-retry-admit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let compile = |source: &[u8]| {
            let hex: String = source.iter().map(|b| format!("{b:02x}")).collect();
            let path = dir.join("o");
            let out = std::process::Command::new(&fexc)
                .args(["--hex", &hex, "--rip", "0x401000", "--profile", "slice1", "--ext-conds", "4", "--push-run", "--narrow-cmp", "--cpl0"])
                .arg("--object")
                .arg(&path)
                .arg("--out")
                .arg("/dev/null")
                .output()
                .unwrap();
            assert!(out.status.success(), "{hex}");
            std::fs::read(&path).unwrap()
        };
        let inc_jmp = [0x48, 0xff, 0xc0, 0xeb, 0x10];
        for source in [
            [&[0xfb][..], &inc_jmp].concat(),                                      // sti; inc rax; jmp
            [&[0x48, 0xff, 0xc0, 0x0f, 0x00, 0x2f][..], &inc_jmp].concat(),        // inc rax; verw [rdi]; inc rax; jmp
            [&[0xfb, 0x48, 0xff, 0xc0, 0x0f, 0x00, 0x2f][..], &inc_jmp].concat(), // sti; inc rax; verw [rdi]; inc rax; jmp
            [&[0xfa, 0x0f, 0x01, 0xf8][..], &inc_jmp].concat(),                    // cli; swapgs; inc rax; jmp
        ] {
            let object = compile(&source);
            admit_at((&object, &source), true, true).unwrap_or_else(|e| panic!("{source:02x?}: {e}"));
        }
        // jz +5; inc rax; jmp -7 (to the unit's start): the jz's taken exit (at 40) forged to the jz's own RIP.
        let source = [0x74, 0x05, 0x48, 0xff, 0xc0, 0xeb, 0xf9];
        let mut object = compile(&source);
        admit_at((&object, &source), true, true).unwrap();
        let h: Header = super::super::record(&object, 0);
        let reloc = (0..h.relocs_count as usize)
            .map(|i| h.relocs_offset as usize + i * size_of::<Reloc>())
            .find(|&r| u32::from_le_bytes(object[r..r + 4].try_into().unwrap()) == 40)
            .unwrap();
        object[reloc + 8..reloc + 16].copy_from_slice(&0x40_1000u64.to_le_bytes());
        let code = (h.code_offset + 40) as usize;
        object[code..code + 4].copy_from_slice(&(0x5280_0000u32 | 0x1000 << 5).to_le_bytes()); // mov w0, #0x1000
        object[code + 4..code + 8].copy_from_slice(&(0x72a0_0000u32 | 0x40 << 5).to_le_bytes()); // movk w0, #0x40, lsl #16
        let e = admit_at((&object, &source), true, true).expect_err("a jz exiting to its own RIP admitted");
        assert!(e.contains("direct target of the branch"), "{e}");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// With `FEXC` set: extended blocks of every conditional form (jcc rel8 and rel32, loop*, jrcxz, with prefixes) as the host
    /// asks for them are admitted, in the middle of a unit and ending it.
    #[test]
    fn real_extended_blocks_are_admitted() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        let dir = std::env::temp_dir().join(format!("natemu-ext-admit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut forms: Vec<Vec<u8>> = (0x70..=0x7f).chain(0xe0..=0xe3).map(|op| vec![op, 5]).collect();
        forms.extend((0x80..=0x8f).map(|op| vec![0x0f, op, 5, 0, 0, 0]));
        forms.extend([vec![0x67, 0xe2, 5], vec![0x66, 0x75, 5], vec![0x3e, 0x74, 5], vec![0x64, 0x66, 0x0f, 0x82, 5, 0, 0, 0]]);
        for f in forms {
            for n in 1..=4 {
                let source: Vec<u8> = (0..n).flat_map(|_| [&[0x48, 0x39, 0xc8][..], &f, &[0x48, 0xff, 0xc0]].concat()).collect();
                let hex: String = source.iter().map(|b| format!("{b:02x}")).collect();
                let object = dir.join("o");
                let out = std::process::Command::new(&fexc)
                    .args(["--hex", &hex, "--rip", "0x401000", "--profile", "slice1", "--ext-conds", "4", "--push-run", "--narrow-cmp"])
                    .arg("--object")
                    .arg(&object)
                    .arg("--out")
                    .arg("/dev/null")
                    .output()
                    .unwrap();
                assert!(out.status.success(), "{hex}");
                let object = std::fs::read(&object).unwrap();
                admit((&object, &source)).unwrap_or_else(|e| panic!("{hex}: {e}"));
            }
        }
        // A skip branch of the instruction before the last (`ror rax, cl` with a zero count jumps to the `jmp`'s exit): the corpora's flow-33.
        for source in [&[0xb1, 0x40, 0x48, 0xd3, 0xc8, 0xeb, 0x61][..], &[0x48, 0x39, 0xc8, 0x75, 0x10, 0xb1, 0x40, 0x48, 0xd3, 0xc8, 0xeb, 0x61]] {
            let hex: String = source.iter().map(|b| format!("{b:02x}")).collect();
            let object = dir.join("o");
            let out = std::process::Command::new(&fexc)
                .args(["--hex", &hex, "--rip", "0x401000", "--profile", "slice1", "--ext-conds", "4", "--push-run", "--narrow-cmp"])
                .arg("--object")
                .arg(&object)
                .arg("--out")
                .arg("/dev/null")
                .output()
                .unwrap();
            assert!(out.status.success(), "{hex}");
            let object = std::fs::read(&object).unwrap();
            admit((&object, source)).unwrap_or_else(|e| panic!("{hex}: {e}"));
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn no_frame_stack_or_literal_access() {
        let (at, old) = MIDDLE;
        refused(ADDRESS, at, old, 0xf900_0380, "frame access"); // str x0, [x28]: the old block header store
        refused(ADDRESS, at, old, 0xf900_0f80, "frame access"); // str x0, [x28, #24]: State.rip
        refused(ADDRESS, at, old, 0xb904_0b80, "frame access"); // str w0, [x28, #1032]: the old pre-NZCV slot
        refused(ADDRESS, at, old, 0xf945_b781, "frame access"); // ldr x1, [x28, #DispatcherLoopTop]
        refused(ADDRESS, at, old, 0xf945_eb83, "frame access"); // ldr x3, [x28, #LUDIVHandler]
        refused(ADDRESS, at, old, 0xf81f_c380, "frame access"); // stur x0, [x28, #-4]: before the frame
        refused(ADDRESS, at, old, 0xf81f_0ffe, "write to sp"); // str x30, [sp, #-16]!
        refused(ADDRESS, at, old, 0xf940_03e0, "stack access"); // ldr x0, [sp]
        refused(ADDRESS, at, old, 0x5800_0040, "literal load"); // ldr x0, .+8
        refused(ADDRESS, at, old, 0xaa14_03f9, "write to x25"); // mov x25, x20: only the pre-NZCV save writes it
    }

    #[test]
    fn division_checks_are_pinned() {
        refused(DIVISION, 0xe8, 0xcb27_4000, 0xcb27_2000, "divide stub"); // div ecx: uxtw -> uxth
        refused(DIVISION, 0xc, 0xd348_3ee0, 0xd349_42e0, "divide stub"); // div cl: ubfx x0, x23, #9, #8
        refused(
            DIVISION,
            0x15c,
            SVC_DIV_U,
            SVC_DIV_S,
            "division svc outside",
        ); // div rcx with the signed division
        validated(
            DIVISION,
            0x15c,
            SVC_DIV_U,
            SVC_DIV_S,
            Some("division svc outside"),
        ); // object validation
        refused(DIVISION, 0x184, 0xb400_0c74, 0xb400_0c75, "zero check"); // idiv: cbz x21, overflow tests x20
        refused(
            DIVISION,
            0x14,
            0xb6f8_0000 | DIVISION_STUB_IMM,
            MOV_X0,
            "reached by 0 branches",
        ); // check removed
        validated(DIVISION, 0x14, 0xb6f8_0000 | DIVISION_STUB_IMM, MOV_X0, None); // x86 semantics: the code-shape check
        // The checks read FEX's own operands.
        refused(DIVISION, 0x10, 0xcb34_0000, 0xcb35_0000, "operands"); // div cl: sub x0, x0, w21, uxtb (divisor is w20)
        refused(DIVISION, 0xc, 0xd348_3ee0, 0xd348_3ec0, "operands"); // div cl: ubfx x0, x22 (dividend is x23)
        refused(DIVISION, 0x58, 0x1300_1e81, 0x1300_1ea1, "operands"); // idiv bh: sxtb w1, w21
        refused(DIVISION, 0xac, 0x1300_3e81, 0x1300_3ea1, "operands"); // idiv r9w: sxth w1, w21 (FEX divides by sxth w1, w20)
        refused(DIVISION, 0x148, 0xb400_0e07, 0xb400_0e06, "operands"); // div rcx: cbz x6
        refused(DIVISION, 0x158, 0xaa07_03e2, 0xaa06_03e2, "operands"); // div rcx: the division gets x6 as divisor
        // Review H1: FEX's 64-bit fast-path selector and the 8-bit dividend copy are pinned.
        refused(DIVISION, 0x14c, 0xb400_0125, 0xb400_0126, "selector"); // div rcx: cbz x6, inline
        refused(DIVISION, 0x14c, 0xb400_0125, 0xb400_013f, "selector"); // cbz xzr: always the inline divide
        refused(DIVISION, 0x14c, 0xb400_0125, 0x1400_0009, "selector"); // b inline
        refused(DIVISION, 0x1a4, 0xca05_0000, 0xca00_0000, "selector"); // idiv: eor x0, x0, x0
        refused(DIVISION, 0x8, 0x5300_3c97, 0x5300_3cd7, "dividend"); // div cl: uxth w23, w6 (AX is w4)
        refused(DIVISION, 0x4, 0x5300_1cf4, 0x5300_1ce4, "dividend"); // div cl: uxtb w4, w7 writes RAX first
        // A division `svc` without its verdict, or anywhere else.
        refused(DIVISION, 0x160, 0xb500_0d62, MOV_X0, "reached by 0 branches");
        let (at, old) = MIDDLE;
        refused(ADDRESS, at, old, SVC_DIV_U, "division svc outside");
        validated(ADDRESS, at, old, SVC_DIV_U, Some("division svc outside"));
    }
    /// I6b: a helper `svc` only in its instruction, right after the `movz w0` of exactly its function code or control word.
    /// G1: an x87 stack slot (`ldrb w20, [x28, #0x41b]` (TOP) at 0x1c0, `and w20, w20, #7` at 0x1c4, `add x0, x28, x20, lsl #4` at 0x1c8, `ldr d2, [x0, #0x420]` at
    /// 0x1cc: fstp's ST(0)) is admitted only when no branch enters after the mask: a path skipping it could index outside
    /// the guest-state page.
    #[test]
    fn x87_slot_index_is_masked_on_every_path() {
        const HELPERS: (&[u8], &[u8]) = FIXTURES[10];
        let ldrb_top = 0x1c0;
        assert_eq!(
            super::super::record::<Header>(HELPERS.0, 0).code_offset,
            0x80
        );
        // `b` to the mask itself: every path still masks.
        admit((
            &patched(HELPERS, ldrb_top, 0x3950_6f94, 0x1400_0001),
            HELPERS.1,
        ))
        .unwrap();
        // `b` past the mask to the `add`, or to the access.
        refused(
            HELPERS,
            ldrb_top,
            0x3950_6f94,
            0x1400_0002,
            "unrecorded memory access at 0x1cc",
        );
        refused(
            HELPERS,
            ldrb_top,
            0x3950_6f94,
            0x1400_0003,
            "unrecorded memory access at 0x1cc",
        );
    }

    #[test]
    fn helper_svcs_only_in_their_instruction() {
        const HELPERS: (&[u8], &[u8]) = FIXTURES[10];
        let movz = |c: u32| MOVZ_W0 | c << 5;
        admit(HELPERS).unwrap();
        for (at, old, new) in [
            (0x98, movz(0xfe), movz(0xff)), // FSIN's svc with FCOS's function code
            (0xa0, movz(0xf8), movz(0xf5)), // FPREM's with FPREM1's
            (0xa0, movz(0xf8), MOV_X0),        // no movz
            (0xb4, movz(0x12), movz(0x13)), // another imm8
            (0xf0, movz(0x34c), movz(0x24c)), // REX.W dropped
            (0xf0, movz(0x34c), movz(0x14c)), // the explicit bit dropped
            (0x11c, movz(0x40), movz(0x240)), // an implicit form with the explicit bit
        ] {
            refused(HELPERS, at, old, new, "movz w0");
            validated(HELPERS, at, old, new, Some("movz w0"));
        }
        // A branch to the svc, over its movz.
        refused(
            HELPERS,
            0x94,
            u32::from_le_bytes(
                HELPERS.0
                    [(super::super::record::<Header>(HELPERS.0, 0).code_offset + 0x94) as usize..]
                    [..4]
                    .try_into()
                    .unwrap(),
            ),
            0x1400_0002,
            "movz w0",
        );
        // The other helper's svc, or a helper svc in any other instruction.
        for (fixture, at, old, new) in [
            (HELPERS, 0x9c, SVC_X87, SVC_STRING),
            (HELPERS, 0xb8, SVC_STRING, SVC_X87),
            (ADDRESS, MIDDLE.0, MIDDLE.1, SVC_X87),
            (ADDRESS, MIDDLE.0, MIDDLE.1, SVC_STRING),
        ] {
            refused(fixture, at, old, new, "helper svc outside");
            validated(fixture, at, old, new, Some("helper svc outside"));
        }
        // The decoders: prefixes, REX, the helper set, the control word.
        assert_eq!(x86_x87_helper(&[0x64, 0x41, 0xd9, 0xf4]), Some(0xf4));
        assert_eq!(
            [0xf6, 0xf7, 0xfa, 0xfc, 0xe0].map(|m| x86_x87_helper(&[0xd9, m])),
            [None; 5]
        );
        assert_eq!(x86_x87_helper(&[0x66, 0xd9, 0xfe]), None);
        assert_eq!(
            x86_string_compare(&[0x66, 0x0f, 0x3a, 0x60, 0xca, 0x7f]),
            Some(0x27f)
        );
        assert_eq!(
            x86_string_compare(&[0x66, 0x4c, 0x0f, 0x3a, 0x63, 0xca, 0x7f]),
            Some(0x7f)
        ); // REX.W: no length registers
        assert_eq!(
            x86_string_compare(&[0xc4, 0xe3, 0xf9, 0x61, 0xca, 0x01]),
            Some(0x301)
        ); // VEX.W1
        assert_eq!(
            x86_string_compare(&[0xc4, 0xe3, 0x7d, 0x61, 0xca, 0x01]),
            None
        ); // VEX.L1
        assert_eq!(x86_string_compare(&[0x0f, 0x3a, 0x61, 0xca, 0x01]), None); // no 66
        assert_eq!(
            x86_string_compare(&[0x66, 0x0f, 0x3a, 0x64, 0xca, 0x01]),
            None
        );
    }

    /// The div-cl check's tbz offset field (stub at 0x2e0 from 0x14).
    const DIVISION_STUB_IMM: u32 = ((0x2e0 - 0x14) / 4) << 5;

    /// `mov eax, 5; xchg [rbx], eax; lfence; mfence; sfence; pause; lock xchg [rbx+8], rcx; xchg [rip+0x10], al;
    /// xchg dx, [rsp]; mov [rbx], eax`: swaps at 0x8, 0x20, 0x2c, 0x34; fences and pause at 0xc-0x18.
    const XCHG: (&[u8], &[u8]) = FIXTURES[8];
    const SWPAL_W4_X6: u32 = 0xb8e4_80c4; // swpal w4, w4, [x6]

    #[test]
    fn swaps_fences_and_pause_are_verified() {
        assert!(matches!(
            decode(SWPAL_W4_X6, 0),
            Some(Insn::Swap {
                size: 4,
                rs: 4,
                rt: 4,
                rn: 6
            })
        ));
        assert!(matches!(
            decode(0x38e4_8294, 0),
            Some(Insn::Swap {
                size: 1,
                rs: 4,
                rt: 20,
                rn: 20
            })
        )); // swpalb
        // Only the acquire-release swap: swp, swpa, swpl are refused (not FEX's xchg), as are other barriers and hints.
        for other in [
            0xb824_80c4,
            0xb8a4_80c4,
            0xb864_80c4,
            0xd503_39bf,
            0xd503_3fdf,
            0xd503_205f,
            0xd503_3f9f,
        ] {
            refused(XCHG, 0xc, 0xd503_3dbf, other, "unrecognised"); // swp swpa swpl; dmb ishld; isb; wfe; dsb sy
        }
        {
            refused(
                XCHG,
                0xc,
                0xd503_3dbf,
                0xd503_3bbf,
                "dmb ish that is not right before",
            ); // dmb ish (I3: only before a LOCK CMPXCHG CAS)
        }
        refused(XCHG, 0x8, SWPAL_W4_X6, SWPAL_W4_X6 | 31, "XZR"); // swpal w4, wzr, [x6]: Rt = XZR loses the acquire
        refused(XCHG, 0x8, SWPAL_W4_X6, SWPAL_W4_X6 + 1, "swap site"); // Rt w5: the record says w4
        refused(XCHG, 0x8, SWPAL_W4_X6, SWPAL_W4_X6 + (1 << 16), "swap site"); // Rs w5
        refused(
            XCHG,
            0x8,
            SWPAL_W4_X6,
            SWPAL_W4_X6 & !(31 << 5) | 28 << 5,
            "frame access by a swap",
        ); // [x28]: the frame
        refused(XCHG, 0x8, SWPAL_W4_X6, SWPAL_W4_X6 | 1 << 30, "swap site"); // 8 bytes, recorded 4
        refused(
            XCHG,
            0x4,
            0x5280_00a4,
            SWPAL_W4_X6,
            "swap that is not a swap site at 0x4",
        ); // a swap where no site is
        refused(
            XCHG,
            0x8,
            SWPAL_W4_X6,
            0xb900_00c4,
            "no memory access of its kind",
        ); // str w4, [x6] at the swap site
        // Object validation: a site's word is its access, x28 is never a swap's base; the rest is code shape.
        validated(XCHG, 0x8, SWPAL_W4_X6, SWPAL_W4_X6 + 1, Some("swap site"));
        validated(
            XCHG,
            0x4,
            0x5280_00a4,
            SWPAL_W4_X6 & !(31 << 5) | 28 << 5,
            Some("frame access by a swap"),
        );
        validated(XCHG, 0x4, 0x5280_00a4, SWPAL_W4_X6, None);
        validated(XCHG, 0xc, 0xd503_3dbf, 0xd503_3fdf, None); // isb
        // A guest-RIP swap whose operand crosses 16 bytes (`xchg [rip+0x18], eax` at 0x100000: 0x10001e) is admitted: the
        // resident's typed stop `unsupported_atomic` refuses it at run time (X1c).
        admit(fixture!("xchg-rip-crossing")).expect("split lock refused at admission");
    }

    #[test]
    fn forbidden_instructions_are_refused() {
        let (at, old) = MIDDLE;
        refused(ADDRESS, at, old, 0xd63f_00a0, "unrecognised"); // blr x5
        refused(ADDRESS, at, old, 0xd61f_0020, "unrecognised"); // br x1
        refused(ADDRESS, at, old, 0xd51b_4220, "unrecognised"); // msr daif, x0
        refused(ADDRESS, at, old, 0xd65f_03c0, "unrecognised"); // ret
        refused(ADDRESS, at, old, 0xd400_0002, "unrecognised"); // hvc #0
        refused(ADDRESS, at, old, 0xd400_0001, "svc at"); // svc #0
        refused(ADDRESS, at, old, 0xd400_0001 | 2 << 5, "svc at"); // svc #MISS: the dispatcher's own
        refused(ADDRESS, at, old, 0xa940_0780, "frame access"); // ldp x0, x1, [x28] (I5a: a decoded pair, never at x28)
        refused(ADDRESS, at, old, 0xa940_0a80, "unrecorded memory access"); // ldp x0, x2, [x20]: no site
        assert!(decode(0xa940_0280, 0).is_none()); // ldp x0, x0, [x20]: UNPREDICTABLE
        refused(ADDRESS, at, old, 0xaa14_03fc, "write to x28"); // mov x28, x20
        refused(ADDRESS, at, old, 0x9100_029f, "write to sp"); // mov sp, x20
        refused(ADDRESS, at, old, 0xf940_00c5, "unrecorded memory access"); // ldr x5, [x6]
        refused(ADDRESS, at, old, 0x17ff_ffff, "not forward"); // b .-4
        refused(ADDRESS, at, old, 0xd420_0020, "brk"); // brk #1 that is no stub
    }

    #[test]
    fn guest_state_fields_and_i1_words() {
        let (at, old) = MIDDLE;
        let admitted = |new: u32| {
            admit((&patched(ADDRESS, at, old, new), ADDRESS.1))
                .unwrap_or_else(|e| panic!("{new:#010x}: {e}"))
        };
        admitted(0x390f_eb94); // strb w20, [x28, #0x3fa]: cld/std write DF
        admitted(0x394f_e795); // ldrb w21, [x28, #0x3f9]: pushf reads IF
        admitted(0x398f_eb95); // ldrsb x21, [x28, #0x3fa]: pushf reads DF
        admitted(0x3950_1795); // ldrb w21, [x28, #0x405]: pushf reads ID
        refused(ADDRESS, at, old, 0x390f_e794, "frame access"); // strb w20, [x28, #0x3f9]: IF is the resident's (but in a CPL-0 CLI/STI)
        refused(ADDRESS, at, old, 0x390f_f394, "frame access"); // strb w20, [x28, #0x3fc]: IOPL
        refused(ADDRESS, at, old, 0x394f_ef95, "frame access"); // ldrb w21, [x28, #0x3fb]: no field
        admitted(0x3950_1b95); // ldrb w21, [x28, #0x406]: an inline STI reads the event-pending byte (perf chunk 15)
        admitted(0xf940_5f95); // ldr x21, [x28, #0xb8]: an inline RDTSC reads the TSC offset (perf chunk 15)
        refused(ADDRESS, at, old, 0xf900_5f95, "frame access"); // str x21, [x28, #0xb8]: the resident's
        // Perf chunk 15: the virtual count only in a CPL-0 RDTSC (EL0 may read it), with or without the code shape.
        for word in [0xd53b_e0d4, 0xd53b_e054] {
            refused(ADDRESS, at, old, word, "counter read"); // mrs x20, cntvctss_el0 | cntvct_el0
            validated(ADDRESS, at, old, word, Some("counter read"));
        }
        refused(ADDRESS, at, old, 0x3950_2395, "frame access"); // ldrb w21, [x28, #0x408]: past the flags
        // Perf chunk 26: the fast-string byte (flags[23]) is readable, never writable.
        validated(ADDRESS, at, old, 0x3950_1f95, None); // ldrb w21, [x28, #0x407]
        refused(ADDRESS, at, old, 0x3910_1f94, "frame access"); // strb w20, [x28, #0x407]
        refused(ADDRESS, at, old, 0x3910_1b94, "frame access"); // strb w20, [x28, #0x406]: the event-pending byte is the resident's
        // Perf chunk 17: IA32_KERNEL_GS_BASE's field and a store of GS.base only in a CPL-0 block's SWAPGS (these fixtures are not
        // CPL 0: the cpl0_translates_swapgs_inline test admits fexc's).
        refused(ADDRESS, at, old, 0xf940_5395, "frame access"); // ldr x21, [x28, #0xa0]
        refused(ADDRESS, at, old, 0xf900_5394, "frame access"); // str x20, [x28, #0xa0]
        refused(ADDRESS, at, old, 0xf901_f395, "frame access"); // str x21, [x28, #0x3e0]
        for word in [0xf940_5395, 0xf900_5394, 0xf901_f395] {
            validated(ADDRESS, at, old, word, Some("frame access")); // without the code shape too
        }
        // ... and in a CPL-0 block: admitted without the code shape, refused with it outside a SWAPGS (this fixture holds none);
        // a 4-byte, pair or unscaled access to the field is refused either way.
        let cpl0 = |word: u32, shape: bool| {
            admit_at((&patched(ADDRESS, at, old, word), ADDRESS.1), shape, true)
        };
        for word in [0xf940_5395, 0xf900_5394, 0xf901_f395] {
            assert_eq!(cpl0(word, false), Ok(()), "{word:#010x}");
            assert!(
                cpl0(word, true).is_err_and(|e| e.contains("frame access")),
                "{word:#010x}"
            );
        }
        // ldr w21, [x28, #0xa0]; ldp x20, x21, [x28, #0xa0]; ldur x21, [x28, #0xa0]; str x21, [x28, #0x3e8] (the FS base: never written)
        for word in [0xb940_a395, 0xa94a_5794, 0xf84a_0395, 0xf901_f795] {
            assert!(
                cpl0(word, false).is_err_and(|e| e.contains("frame access")),
                "{word:#010x}"
            );
        }
        // Perf P2: the VERW fields (GDT base 0x4a0, limit 0x4a8): loads only in a CPL-0 block (with the code shape only inside a
        // VERW, which this fixture lacks); never stored; 8-byte loads only.
        for word in [0xf942_5395, 0xf942_5795] {
            refused(ADDRESS, at, old, word, "frame access"); // ldr x21, [x28, #0x4a0] | #0x4a8
            validated(ADDRESS, at, old, word, Some("frame access"));
            assert_eq!(cpl0(word, false), Ok(()), "{word:#010x}");
            assert!(
                cpl0(word, true).is_err_and(|e| e.contains("frame access")),
                "{word:#010x}"
            );
        }
        for word in [0xf902_5394, 0xb944_a395] {
            assert!(
                cpl0(word, false).is_err_and(|e| e.contains("frame access")),
                "{word:#010x}"
            ); // str x20, [x28, #0x4a0]; ldr w21, [x28, #0x4a0]
        }
        // I7: the segment bases (gs_cached 992, fs_cached 1000) are loads only.
        admitted(0xf941_f794); // ldr x20, [x28, #1000]: an FS-prefixed access reads FS.base
        admitted(0xf941_f394); // ldr x20, [x28, #992]: GS.base
        refused(ADDRESS, at, old, 0xf901_f794, "frame access"); // str x20, [x28, #1000]: the resident's
        refused(ADDRESS, at, old, 0xf901_f394, "frame access"); // str x20, [x28, #992]
        refused(ADDRESS, at, old, 0xf941_ef94, "frame access"); // ldr x20, [x28, #984]: below gs_cached (ds_cached)
        // Loads that straddle or cover both bases. `ldur x20, [x28, #1004]` has no encoding (imm9 is -256..255) and
        // neither has `ldp x20, x21, [x28, #992]` (imm7 * 8 <= 504): the unscaled form is refused at any offset, and the
        // pair at 992 exists only for q registers.
        refused(ADDRESS, at, old, 0xf84f_c394, "frame access"); // ldur x20, [x28, #252]: unscaled
        refused(ADDRESS, at, old, 0x3dc0_fb80, "frame access"); // ldr q0, [x28, #992]: both bases in one load (I5a: decoded)
        refused(ADDRESS, at, old, 0xad5f_0780, "frame access"); // ldp q0, q1, [x28, #992] (I8: a decoded pair)
        refused(ADDRESS, at, old, 0xf941_fb94, "frame access"); // ldr x20, [x28, #1008]: past fs_cached
        for word in [0xf84f_c394, 0x3dc0_fb80, 0xad5f_0780, 0xf941_fb94] {
            validated(ADDRESS, at, old, word, Some("")); // the same without the code-shape check
        }
        // I8: the YMM upper halves (avx_high, 0xc0..0x1c0) by Q registers, single or paired (FEX's register cache, VZEROUPPER's
        // ContextClear), and FEX's zero store of a VEX.128 destination's upper half (`stp xzr, xzr`); nothing past them.
        for word in [
            0x3dc0_3380,
            0x3d80_3380,
            0xad00_0380 | 12 << 15,
            0xad40_0780 | 12 << 15,
            0xa900_7f9f | 0x1e << 15,
            0xad00_0380 | 0x1a << 15,
        ] {
            admitted(word); // ldr q0, [x28, #0xc0]; str q0, [x28, #0xc0]; stp q0, q0 / ldp q0, q1, [x28, #0xc0]; stp xzr, xzr, [x28, #0xf0]; stp q0, q0, [x28, #0x1a0]
        }
        refused(ADDRESS, at, old, 0xad00_0380 | 0x1c << 15, "frame access"); // stp q0, q0, [x28, #0x1c0]: past avx_high
        refused(ADDRESS, at, old, 0x3d80_2f80, "frame access"); // str q0, [x28, #0xb0]: below it
        // POPCNT without FEAT_CSSC, and prefetch hints.
        for word in [
            0x9e67_00c0,
            0x1e27_00e0,
            0x0e20_5800,
            0x0e31_b800,
            0x0e20_bc00,
            0x0e01_3c04,
            0xf980_00c0,
        ] {
            admitted(word); // fmov d0, x6; fmov s0, w7; cnt v0.8b; addv b0, v0.8b; umov w4, v0.b[0]; prfm pldl1keep, [x6]
        }
        refused(ADDRESS, at, old, 0x0e01_3c19, "write to x25"); // umov w25, v0.b[0]
        // I5a: a fixed-point FCVTZS/FCVTZU writes a general register too (fcvtzs x28/x25/x3, s0, #1).
        refused(ADDRESS, at, old, 0x9e18_fc1c, "write to x28");
        refused(ADDRESS, at, old, 0x9e18_fc19, "write to x25");
        validated(ADDRESS, at, old, 0x9e18_fc1c, Some("write to x28"));
        validated(ADDRESS, at, old, 0x9e19_fc19, Some("write to x25")); // fcvtzu x25, s0, #1
        admitted(0x9e18_fc03);
    }

    #[test]
    fn object_validation_without_the_code_shape_check() {
        let (at, old) = MIDDLE;
        // Words the decoder does not know are left to the hardware at EL0 (it traps or contains them) unless they branch
        // or name x25/x28; so are sp, literal and unrecorded accesses, and brk words at no stub.
        for word in [
            0x1e62_2820,
            0xd503_3fdf,
            0xd51b_4220,
            0xf940_03e0,
            0xf81f_0ffe,
            0x5800_0040,
            0xf940_00c5,
            0xd420_0020,
            0x9100_029f,
        ] {
            validated(ADDRESS, at, old, word, None); // fadd d0; isb; msr daif; ldr [sp]; str x30 [sp, #-16]!; ldr literal; ldr [x6]; brk #1; mov sp
        }
        for (word, why) in [
            (0xd63f_00a0, "unrecognised"), // blr x5
            (0xd61f_0020, "unrecognised"), // br x1
            (0xd65f_03c0, "unrecognised"), // ret
            (0x9400_0000, "unrecognised"), // bl .
            (0xd400_0002, "unrecognised"), // hvc #0
            (0xd51b_d059, "unrecognised"), // msr tpidr_el0, x25
            (0x3dc0_0380, "frame access"), // ldr q0, [x28] (I5a: a decoded vector load)
            (0xa940_0780, "frame access"), // ldp x0, x1, [x28] (I5a: decoded)
            (0x0820_7f18, "unrecognised"), // casp w0, w1, w24, w25, [x24]: writes x25
            // The whole branch, exception and system group but its system class (0xd5), whatever the encoding:
            (0x7400_0020, "unrecognised"), // Armv9.6 CMPBR (compare and branch, may go backward)
            (0x7500_0041, "unrecognised"), // CMPBR
            (0xf400_0020, "unrecognised"), // CMPBR, 64-bit
            (0xf500_0041, "unrecognised"), // CMPBR, 64-bit
            (0xd71f_0820, "unrecognised"), // braa x1, x0
            (0xd65f_0bff, "unrecognised"), // retaa
            (0xd400_0003, "unrecognised"), // smc #0
            (0x1600_0000, "not forward"),  // b .-0x8000000: decoded, a backward branch
            (0x5400_0010, "unrecognised"), // bc.eq .
            (0xd400_0001, "svc at"),       // svc #0
            (0xd400_0001 | 2 << 5, "svc at"), // svc #MISS: the dispatcher's own
            (0xaa14_03fc, "write to x28"), // mov x28, x20
            (0xaa14_03f9, "write to x25"), // mov x25, x20
            (0xf940_0380, "frame access"), // ldr x0, [x28]
            (0x17ff_ffff, "not forward"),  // b .-4
            (0xd4000021, "not exactly a link point"),
        ] {
            validated(ADDRESS, at, old, word, Some(why));
        }
        // Real blocks: the same results either way.
        for (i, &f) in FIXTURES.iter().enumerate() {
            admit_with(f, false).unwrap_or_else(|e| panic!("fixture {i}: {e}"));
        }
        assert!(admit_with(fixture!("xchg-rip-crossing"), false).is_ok()); // a split lock: the resident's halt at run time
    }
    /// M2 checkpoint C, with `FEXC` set: the sweep of 32-bit memory forms through a limited FS or GS. Every opcode of the
    /// legacy maps (one byte, 0f, 0f 38, 0f 3a, each plain and with 66, f3, f2 and LOCK), of x87 and of the VEX maps, with
    /// every ModRM.reg, as `fs: op [ebx]`: alone in a unit (`--max-insts 1`, the resident's step), followed by a `ret`, and
    /// that with every x87 and SIMD exception unmasked; and, each for half of the forms, as `fs: op [disp32]`,
    /// `fs: op [ebx+ecx*4+disp32]`, `gs: op [ebx]` and second in its unit (after an `inc eax`). For each: fexc answers; what
    /// it translates without the check it translates with it; the checked unit is admitted, with its one operand check
    /// (width and intent as admission decodes them) and every access adding the check's base, and is refused under any other
    /// identity; and the unchecked unit of `fs: op [ebx]` is admitted too (every access adds the base it loads). The forms
    /// admission refuses with and without the check are listed (VPCMPESTRI/VPCMPESTRM with VEX.W = 1).
    #[test]
    fn every_limited_fs_form_is_checked_or_refused() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        use iced_x86::{Decoder, DecoderOptions, OpKind, Register};
        // (segment prefix, ModRM mod and r/m, the bytes after ModRM, a leading instruction, step, unmasked, which half)
        struct Arm { prefix: u8, modrm: u8, tail: &'static [u8], lead: &'static [u8], step: bool, unmasked: bool, half: Option<usize>, flat: bool }
        let arm = Arm { prefix: 0x64, modrm: 3, tail: &[], lead: &[], step: false, unmasked: false, half: None, flat: false };
        let arms = [
            Arm { step: true, ..arm },
            Arm { flat: true, ..arm },
            Arm { unmasked: true, ..arm },
            Arm { modrm: 5, tail: &[0x10, 0x20, 0x30, 0x00], half: Some(0), ..arm },
            Arm { modrm: 0x84, tail: &[0x8b, 0x10, 0x20, 0x30, 0x00], half: Some(1), ..arm },
            Arm { prefix: 0x65, half: Some(0), ..arm },
            Arm { lead: &[0x40], half: Some(1), ..arm },
        ];
        let mut heads: Vec<Vec<u8>> = Vec::new();
        for prefix in [&[][..], &[0x66], &[0xf3], &[0xf2], &[0xf0]] {
            for op in 0..=255u8 {
                for map in [&[][..], &[0x0f], &[0x0f, 0x38], &[0x0f, 0x3a]] {
                    heads.push([prefix, map, &[op]].concat());
                }
            }
        }
        for op in 0..=255u8 {
            for pp in 0..4u8 {
                for l in 0..2u8 {
                    heads.push(vec![0xc5, 0xf8 | l << 2 | pp, op]); // VEX map 1, vvvv = 1111b
                    heads.push(vec![0xc5, 0xb8 | l << 2 | pp, op]); // vvvv = 0111b (the forms that take a second source)
                    for map in [2u8, 3] {
                        for w in 0..2u8 {
                            heads.push(vec![0xc4, 0xe0 | map, w << 7 | 0x78 | l << 2 | pp, op]);
                            heads.push(vec![0xc4, 0xe0 | map, w << 7 | 0x38 | l << 2 | pp, op]);
                        }
                    }
                }
            }
        }
        // (head index, reg): the instruction of an arm, when iced decodes a memory operand through the arm's segment.
        let form = |arm: &Arm, head: &[u8], reg: u8| -> Option<Vec<u8>> {
            let mut bytes = vec![arm.prefix];
            bytes.extend_from_slice(head);
            bytes.push(reg << 3 | arm.modrm);
            bytes.extend_from_slice(arm.tail);
            bytes.extend_from_slice(&[0x10, 0x20, 0x30, 0x40, 0, 0, 0, 0]);
            let i = Decoder::new(32, &bytes, DecoderOptions::NONE).decode();
            let segment = if arm.prefix == 0x64 { Register::FS } else { Register::GS };
            (!i.is_invalid() && i.memory_segment() == segment && i.op_kinds().any(|k| k == OpKind::Memory)).then(|| bytes[..i.len()].to_vec())
        };
        let dir = std::env::temp_dir().join(format!("natemu-fs-sweep-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let chunks: Vec<&[Vec<u8>]> = heads.chunks(heads.len().div_ceil(8)).collect();
        let (mut translated, mut refused) = (0, Vec::new());
        std::thread::scope(|scope| {
            let workers: Vec<_> = chunks
                .iter()
                .enumerate()
                .map(|(n, chunk)| {
                    let (fexc, dir, arms, form) = (&fexc, &dir, &arms, &form);
                    scope.spawn(move || {
                        let (mut translated, mut refused) = (0, Vec::new());
                        for (k, head) in chunk.iter().enumerate() {
                            for (arm, reg) in arms.iter().flat_map(|a| (0..8u8).map(move |r| (a, r))) {
                                if arm.half.is_some_and(|h| (k + usize::from(reg)) % 2 != h) {
                                    continue;
                                }
                                let Some(insn) = form(arm, head, reg) else { continue };
                                let at = arm.lead.len();
                                let source = [arm.lead, &insn[..], if arm.step { &[][..] } else { &[0xc3] }].concat();
                                let hex: String = source.iter().map(|b| format!("{b:02x}")).collect();
                                let label = if arm.step { " (step)" } else if arm.unmasked { " (unmasked)" } else { "" };
                                let limited = [arm.prefix == 0x64, arm.prefix == 0x65];
                                // The unit's object when fexc translated the instruction (not cut before it or after it);
                                // fexc must answer either way.
                                let compile = |checked: bool| -> Option<Vec<u8>> {
                                    let object = dir.join(format!("o{n}"));
                                    let _ = std::fs::remove_file(&object);
                                    let mut c = std::process::Command::new(fexc);
                                    c.args(["--hex", &hex, "--rip", "0x8048000", "--profile", "slice1", "--bitness", "32", "--narrow-cmp", "--push-run"]);
                                    if checked {
                                        c.arg(if limited[0] { "--fs-limited" } else { "--gs-limited" });
                                    }
                                    if arm.step {
                                        c.args(["--max-insts", "1"]);
                                    }
                                    if arm.unmasked {
                                        c.args(["--x87-unmasked", "0x3f", "--simd-unmasked", "0x3f"]);
                                    }
                                    let out = c.arg("--object").arg(&object).arg("--out").arg("/dev/null").output().unwrap();
                                    assert!(out.status.code().is_some(), "{hex}{label}: fexc died");
                                    let o = std::fs::read(&object).ok().filter(|_| out.status.success())?;
                                    (super::super::record::<Header>(&o, 0).guest_bytes as usize >= at + insn.len()).then_some(o)
                                };
                                let admit = |o: &[u8], cpl3: bool, limited: [bool; 2]| {
                                    let h: Header = super::super::record(o, 0);
                                    super::super::admit_user(o, h.guest_rip, &source, u64::MAX, true, false, true, super::super::User32 { cpl3, limited })
                                };
                                let flat = if arm.flat { compile(false) } else { None };
                                let Some(o) = compile(true) else {
                                    // The check must not cost a form: what fexc translates without it, it translates with it.
                                    if flat.is_some() || (!arm.flat && compile(false).is_some()) {
                                        refused.push(format!("{hex}{label}: translated only without the check"));
                                    }
                                    continue;
                                };
                                translated += 1;
                                let unchecked_refused = |f: Option<Vec<u8>>| f.is_none_or(|f| admit(&f, true, [false; 2]).is_err());
                                match admit(&o, true, limited) {
                                    Ok(_) => {
                                        if arm.flat && unchecked_refused(flat) {
                                            refused.push(format!("{hex}: refused without the check only"));
                                        }
                                    }
                                    // Not the check's doing when the same unit compiled without it is refused too.
                                    Err(e) => {
                                        let also = unchecked_refused(if arm.flat { flat } else { compile(false) });
                                        refused.push(format!("{hex}{label}{}: {e}", if also { " (also without the check)" } else { "" }));
                                    }
                                }
                                // The same object is never admitted as a block whose segment is not limited, nor outside CPL 3
                                // (a form that accesses nothing, a prefetch or a hint NOP, has no check and is). It has exactly
                                // one check, its instruction's.
                                let h: Header = super::super::record(&o, 0);
                                let checks: Vec<Site> = (0..h.sites_count as usize)
                                    .map(|k| super::super::record::<Site>(&o, h.sites_offset as usize + k * size_of::<Site>()))
                                    .filter(|s| s.kind == object::kind::SEGMENT)
                                    .collect();
                                assert!(checks.len() <= 1 && checks.iter().all(|s| s.guest_offset as usize == at), "{hex}{label}");
                                if !checks.is_empty() {
                                    assert!(admit(&o, true, [limited[1], limited[0]]).is_err() && admit(&o, true, [false; 2]).is_err() && admit(&o, false, limited).is_err(), "{hex}{label}");
                                }
                            }
                        }
                        (translated, refused)
                    })
                })
                .collect();
            for w in workers {
                let (t, r) = w.join().unwrap();
                translated += t;
                refused.extend(r);
            }
        });
        std::fs::remove_dir_all(&dir).unwrap();
        eprintln!("{} opcode heads, {translated} units translated, {} refused", heads.len(), refused.len());
        let new: Vec<_> = refused.iter().filter(|r| !(r.contains("(also without the check)") && r.contains("c4e3f96"))).collect();
        assert!(new.is_empty(), "{} unexpected, the first: {:#?}", new.len(), &new[..new.len().min(40)]);
        assert!(translated > 50_000, "{translated}");
    }

    /// M2 checkpoint C, with `FEXC` set: an operand check that is not exactly the instruction's is refused, and so is every
    /// way around it: a check of another width or intent, a changed word, a record of the other segment, the unchecked code
    /// under a limited identity, a load of the limited segment's base field, a site tag that is not the operand's, and an FS
    /// operand outside CPL 3.
    #[test]
    fn operand_check_mutations_are_refused() {
        let Some(fexc) = std::env::var_os("FEXC") else {
            return;
        };
        use super::super::User32;
        use core::mem::offset_of;
        let dir = std::env::temp_dir().join(format!("natemu-fs-check-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let compile = |hex: &str, extra: &[&str]| -> (Vec<u8>, Vec<u8>) {
            let object = dir.join("o");
            let out = std::process::Command::new(&fexc)
                .args(["--hex", hex, "--rip", "0x8048000", "--profile", "slice1", "--bitness", "32", "--narrow-cmp", "--push-run"])
                .args(extra)
                .arg("--object")
                .arg(&object)
                .arg("--out")
                .arg("/dev/null")
                .output()
                .unwrap();
            assert!(out.status.success(), "{hex}");
            ((0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect(), std::fs::read(&object).unwrap())
        };
        let admit = |(source, object): &(Vec<u8>, Vec<u8>), cpl3: bool, limited: [bool; 2]| {
            let h: Header = super::super::record(object, 0);
            super::super::admit_user(object, h.guest_rip, source, u64::MAX, true, false, true, User32 { cpl3, limited }).map(|_| ())
        };
        let sites = |object: &[u8]| -> Vec<(usize, Site)> {
            let h: Header = super::super::record(object, 0);
            (0..h.sites_count as usize)
                .map(|k| h.sites_offset as usize + k * size_of::<Site>())
                .map(|at| (at, super::super::record::<Site>(object, at)))
                .collect()
        };
        let refused = |r: Result<(), String>, why: &str| {
            let e = r.expect_err(why);
            assert!(e.contains(why), "{e}: expected {why}");
        };
        // inc dword fs:[ebx]; ret: a write of 4 bytes.
        let checked = compile("64ff03c3", &["--fs-limited"]);
        admit(&checked, true, [true, false]).unwrap();
        refused(admit(&checked, true, [false, false]), "operand check without a limited");
        refused(admit(&checked, true, [false, true]), "operand check without a limited");
        refused(admit(&checked, false, [true, false]), "not CPL 3");
        let h: Header = super::super::record(&checked.1, 0);
        let (at, check) = sites(&checked.1).into_iter().find(|(_, s)| s.kind == object::kind::SEGMENT).unwrap();
        assert_eq!((check.segment, check.width, check.intent), (object::SEG_FS, 4, 1));
        let patched = |f: &dyn Fn(&mut Vec<u8>)| {
            let mut o = checked.1.clone();
            f(&mut o);
            (checked.0.clone(), o)
        };
        for (field, value) in [(offset_of!(Site, width), 2u8), (offset_of!(Site, intent), 0), (offset_of!(Site, segment), object::SEG_GS)] {
            refused(admit(&patched(&|o| o[at + field] = value), true, [true, true]), "operand check");
        }
        let code = h.code_offset as usize;
        let word = |o: &[u8], n: u32| u32::from_le_bytes(o[code + n as usize..][..4].try_into().unwrap());
        let sub = check.guest_offset2;
        // The width in the code (4 -> 2), the record's slot (FS's -> GS's), the offset's register, the base move's source.
        for (n, flip) in [(sub, 6 << 10), (sub - 8, 4 << 10), (sub + 4, 1 << 16), (check.imm as u32, 1 << 16)] {
            let o = patched(&|o| o[code + n as usize..][..4].copy_from_slice(&(word(&checked.1, n) ^ flip).to_le_bytes()));
            refused(admit(&o, true, [true, false]), "operand check");
        }
        // An access site of the operand that does not say FS; a site of the `ret` that does.
        let tagged: Vec<_> = sites(&checked.1).into_iter().filter(|(_, s)| s.segment == object::SEG_FS && s.kind != object::kind::SEGMENT).collect();
        assert!(tagged.iter().any(|(_, s)| s.kind == object::kind::LOAD) && tagged.iter().any(|(_, s)| s.kind == object::kind::STORE));
        refused(admit(&patched(&|o| o[tagged[0].0 + offset_of!(Site, segment)] = object::SEG_DS), true, [true, false]), "segment is not its instruction's");
        let (ret, _) = sites(&checked.1).into_iter().find(|(_, s)| s.guest_offset == 3 && s.kind == object::kind::LOAD).unwrap();
        refused(admit(&patched(&|o| o[ret + offset_of!(Site, segment)] = object::SEG_FS), true, [true, false]), "segment is not its instruction's");
        // The unchecked code of the same instruction under the limited identity: no check, and its base load.
        let flat = compile("64ff03c3", &[]);
        admit(&flat, true, [false, false]).unwrap();
        admit(&flat, true, [false, true]).unwrap();
        refused(admit(&flat, true, [true, false]), "not exactly one");
        refused(admit(&flat, false, [false, false]), "not CPL 3");
        // A GS operand's unchecked code is admitted while only FS is limited; with its base load turned into FS's
        // (ldr wN, [x28, #gs_cached] -> #fs_cached) its access no longer adds GS's base.
        let gs = compile("65ff03c3", &[]);
        admit(&gs, true, [true, false]).unwrap();
        let g: Header = super::super::record(&gs.1, 0);
        let gword = |n: u32| u32::from_le_bytes(gs.1[g.code_offset as usize + n as usize..][..4].try_into().unwrap());
        let load = |field: u32| 0xb940_0380 | field / 4 << 10;
        let n = (0..g.code_region_end).step_by(4).find(|&n| gword(n) & !31 == load(frame::GS_BASE)).expect("the GS base load");
        let with = |n: u32, w: u32| {
            let mut o = gs.1.clone();
            o[g.code_offset as usize + n as usize..][..4].copy_from_slice(&w.to_le_bytes());
            (gs.0.clone(), o)
        };
        refused(admit(&with(n, gword(n) & 31 | load(frame::FS_BASE)), true, [true, false]), "does not come from the segment's base");
        // The address formed without the base (the add after the load, both its sources the offset): refused under either
        // identity, which is what FEX's MOVD and MOVNT stores once did.
        let without = |w: u32, base: u32| {
            let offset = if w >> 5 & 31 == base { w >> 16 & 31 } else { w >> 5 & 31 };
            w & !(31 << 16 | 31 << 5) | offset << 16 | offset << 5
        };
        let add = (n + 4..g.code_region_end).step_by(4).find(|&a| gword(a) & 0x7f20_0000 == 0x0b00_0000).expect("the add of the base");
        refused(admit(&with(add, without(gword(add), gword(n) & 31)), true, [false, false]), "does not come from the segment's base");
        let sub_of = |o: &[u8]| sites(o).into_iter().find(|(_, s)| s.kind == object::kind::SEGMENT).unwrap().1;
        let c = sub_of(&checked.1);
        let cadd = (c.imm as u32 + 4..h.code_region_end).step_by(4).find(|&a| word(&checked.1, a) & 0x7f20_0000 == 0x0b00_0000).expect("the add of the base");
        let o = patched(&|o| o[code + cadd as usize..][..4].copy_from_slice(&without(word(&checked.1, cadd), word(&checked.1, c.imm as u32) & 31).to_le_bytes()));
        refused(admit(&o, true, [true, false]), "does not come from the segment's base");
        // A load of the limited FS's base field anywhere else (here in the place of the first word of `inc eax` before
        // the GS instruction): the frame rule.
        let two = compile("4065ff03c3", &[]);
        admit(&two, true, [true, false]).unwrap();
        let t: Header = super::super::record(&two.1, 0);
        let first = super::super::record::<PcEntry>(&two.1, t.pcs_offset as usize).native_offset;
        let mut o = two.1.clone();
        o[t.code_offset as usize + first as usize..][..4].copy_from_slice(&(load(frame::FS_BASE)).to_le_bytes());
        refused(admit(&(two.0.clone(), o), true, [true, false]), "base of a limited FS or GS");
        // A unit too long for the test's `tbnz` to reach its stub has fexc's far form (`tbz x2, #63, +8; b stub`); no block
        // that long is admitted, so admission knows only the near form.
        let long = compile(&format!("64ff03{}c3", "0fa5c8".repeat(700)), &["--fs-limited"]); // (700 shld eax, ecx, cl: code, no sites)
        let l: Header = super::super::record(&long.1, 0);
        let far = sub_of(&long.1);
        let lword = |n: u32| u32::from_le_bytes(long.1[l.code_offset as usize + n as usize..][..4].try_into().unwrap());
        assert_eq!((lword(far.guest_offset2 + 8), lword(far.guest_offset2 + 12) >> 26), (0xb6f8_0042, 5));
        refused(admit(&long, true, [true, false]), "code layout");
        // The check's place and continuation are the record's: a `sub` word elsewhere, a continuation far from it.
        refused(admit(&patched(&|o| o[at + offset_of!(Site, guest_offset2)..][..4].copy_from_slice(&(sub - 4).to_le_bytes())), true, [true, false]), "operand check");
        refused(admit(&patched(&|o| o[at + offset_of!(Site, guest_offset2)..][..4].copy_from_slice(&8u32.to_le_bytes())), true, [true, false]), "its place");
        refused(admit(&patched(&|o| o[at + offset_of!(Site, imm)..][..4].copy_from_slice(&(check.imm as u32 + 64).to_le_bytes())), true, [true, false]), "its continuation");
        std::fs::remove_dir_all(&dir).unwrap();
    }

}
