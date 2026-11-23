//! The interpreter's executor core (plan sections 3 and 4): one record ([`super::Insn`]) executed on the vCPU's
//! registers over a [`Memory`], check then commit. Every address the instruction touches (its memory operand, its stack
//! slots) and every branch target is checked first, segment (layer 1) before memory ([`Memory::check`], layers 2-3);
//! only then do registers, flags, memory and EIP change, so a fault leaves the state as it was and comes back as an
//! [`Exit`]. Forms that need the resident's transactions or descriptor helpers come back as a [`Resident`] operation with
//! their operands read and checked and nothing committed.
//!
//! Segmentation, as the hidden descriptors hold it: the offset wraps at the address size; protected mode checks P
//! (unusable: a null load), the rights (no write to a code or read-only data segment, no read of an execute-only code
//! segment) and the limit (expand-down data segments: above the limit, up to FFFFh or FFFFFFFFh by B); real mode checks
//! the hidden limit alone, as expand-up. A fault is #SS(0) through SS, else #GP(0). Linear = base + offset, wrapped at 32
//! bits (no A20 masking).
//!
//! Undefined flags, natemu's fixed choice (masked when compared with another processor): AF is 0 after AND, OR, XOR,
//! TEST and every shift by a non-zero count; a shift's OF follows the one-bit rule at every count (SHL: the result's
//! sign XOR CF; SHR: the destination's sign; SAR: 0) and its CF is the last bit shifted out, zeros past the width (SHL
//! and SHR by the width or more); BT, BTS, BTR and BTC leave OF, SF, AF and PF unchanged.
use super::{Insn, NO_REG, Operand, kind, op, register, set_register};
use crate::execution::{ATTR_D, Cpu, SREG_CS, SREG_GS, SREG_SS};

pub const CF: u32 = 1;
pub const PF: u32 = 1 << 2;
pub const AF: u32 = 1 << 4;
pub const ZF: u32 = 1 << 6;
pub const SF: u32 = 1 << 7;
pub const IF: u32 = 1 << 9;
pub const DF: u32 = 1 << 10;
pub const OF: u32 = 1 << 11;
/// CF PF AF ZF SF OF.
pub const ARITHMETIC: u32 = 0x8d5;

/// The facts of the current mode the core needs; the caller classifies.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Context {
    /// CR0.PE: descriptor rights and expand-down segments are checked, segment loads and far transfers are the
    /// resident's.
    pub protected: bool,
    /// CS.D: the default operand and address size (the decode's `bits`; the record carries the sizes it implies).
    pub code32: bool,
    /// SS.B: ESP (else SP, wrapping at 64 KiB) addresses the stack.
    pub stack32: bool,
    pub cpl: u8,
}
impl Context {
    /// The facts as the bank holds them (CR0.PE, the hidden CS.D and SS.B, the CPL).
    pub fn of(cpu: &Cpu) -> Self {
        Self {
            protected: cpu.cr0 & crate::execution::CR0_PE != 0,
            code32: cpu.seg[SREG_CS].attributes & ATTR_D != 0,
            stack32: cpu.seg[SREG_SS].attributes & ATTR_D != 0,
            cpl: cpu.cpl as u8,
        }
    }
}
/// Why an instruction did not complete; nothing is committed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Exit {
    /// An exception: its vector, error code (0 for #GP(0), #SS(0), #UD) and, for #PF, the linear address (CR2).
    Fault {
        vector: u8,
        error: u32,
        address: u32,
    },
    /// The record is [`op::OTHER`]: decoded but not in the first version (a typed stop naming its bytes).
    Unsupported,
    /// The memory layer's typed stop (a device page ...), with its own code.
    Stop(u32),
}
impl Exit {
    pub const fn fault(vector: u8) -> Self {
        Exit::Fault {
            vector,
            error: 0,
            address: 0,
        }
    }
}
const UD: Exit = Exit::fault(6);
const SS: Exit = Exit::fault(12);
const GP: Exit = Exit::fault(13);
/// An operation the resident performs (its transaction and privilege checks; then EIP: [`complete`], or the far
/// transfer's target). The core has read and checked its operands and committed nothing.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Resident {
    /// A protected-mode far JMP (immediate or memory pointer).
    FarJump {
        selector: u16,
        offset: u32,
    },
    /// A protected-mode far RET (same privilege): the popped offset and selector, and the stack bytes it releases
    /// ([`release`]) when it completes.
    FarReturn {
        selector: u16,
        offset: u32,
        release: u32,
    },
    /// A protected-mode MOV Sreg (never CS).
    LoadSegment {
        sreg: u8,
        selector: u16,
    },
    /// LGDT (`idt` false) or LIDT: the image's base (24 bits with a 16-bit operand size) and limit.
    LoadTable {
        idt: bool,
        base: u32,
        limit: u16,
    },
    /// MOV CRn := the 32-bit `value`; MOV register `reg` := CRn (zero-extended).
    MovToCr {
        cr: u8,
        value: u32,
    },
    MovFromCr {
        cr: u8,
        reg: u8,
    },
    Rdmsr,
    Wrmsr,
    Cpuid,
    Hlt,
    Wbinvd,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// Committed: registers, flags, memory and EIP.
    Done,
    Resident(Resident),
}
/// Guest memory by linear address (the plan's layers 2 and 3: the translation, if any, and the backing).
pub trait Memory {
    /// Whether `size` bytes (1..=6) at `linear` can be read, or written too (`write`: a store or a read-modify-write):
    /// `Ok`, or the exception (#PF) or typed stop. They may cross a page, or 4 GiB (wrapping to 0). No architectural
    /// effect but what a page walk leaves (A and D bits).
    fn check(&mut self, linear: u32, size: u32, write: bool) -> Result<(), Exit>;
    /// `size` (1, 2, 4) bytes at `linear`, after a successful check that covered them.
    fn read(&mut self, linear: u32, size: u8) -> u32;
    fn write(&mut self, linear: u32, size: u8, value: u32);
    /// LOCK: atomically, if the bytes hold `old`, replaces them with `new`; returns what they held.
    fn compare_exchange(&mut self, linear: u32, size: u8, old: u32, new: u32) -> u32;
}

pub const fn mask(size: u8) -> u32 {
    if size >= 4 {
        u32::MAX
    } else {
        (1 << (size as u32 * 8)) - 1
    }
}
const fn sext(v: u32, size: u8) -> i32 {
    match size {
        1 => v as i8 as i32,
        2 => v as i16 as i32,
        _ => v as i32,
    }
}
/// ZF, SF and PF of a `size`-byte result.
fn szp(r: u32, size: u8) -> u32 {
    let r = r & mask(size);
    u32::from(r == 0) * ZF
        | (r >> (size as u32 * 8 - 1) & 1) * SF
        | u32::from((r as u8).count_ones() & 1 == 0) * PF
}
/// The result and arithmetic flags of an ALU operation ([`op::ADD`] ..= [`op::CMP`]) on `size`-byte operands; `carry` is
/// the CF ADC and SBB add in.
pub fn alu(o: u8, a: u32, b: u32, carry: bool, size: u8) -> (u32, u32) {
    let (a, b, bits) = (a & mask(size), b & mask(size), size as u32 * 8);
    let c = u64::from(carry && matches!(o, op::ADC | op::SBB));
    let (r, sub) = match o {
        op::ADD | op::ADC => (u64::from(a) + u64::from(b) + c, false),
        op::SUB | op::SBB | op::CMP => (u64::from(a).wrapping_sub(u64::from(b) + c), true),
        op::OR => return logic(a | b, size),
        op::AND => return logic(a & b, size),
        _ => return logic(a ^ b, size),
    };
    let r32 = r as u32;
    let over = if sub {
        (a ^ b) & (a ^ r32)
    } else {
        (a ^ r32) & (b ^ r32)
    };
    let flags = szp(r32, size)
        | (r >> bits) as u32 & CF
        | (a ^ b ^ r32) & AF
        | (over >> (bits - 1) & 1) * OF;
    (r32 & mask(size), flags)
}
fn logic(r: u32, size: u8) -> (u32, u32) {
    (r, szp(r, size))
}
/// The result and arithmetic flags of SHL/SHR/SAR of a `size`-byte `d` by `count` (masked to 5 bits); `None` for a count
/// of 0 (nothing changes).
pub fn shift(o: u8, d: u32, count: u32, size: u8) -> Option<(u32, u32)> {
    let (n, bits, d) = (count & 31, size as u32 * 8, d & mask(size));
    if n == 0 {
        return None;
    }
    let sign = |v: u32| v >> (bits - 1) & 1;
    let (r, cf, of) = match o {
        op::SHL => {
            let w = u64::from(d) << n;
            let (r, cf) = (w as u32 & mask(size), (w >> bits) as u32 & 1);
            (r, cf, sign(r) ^ cf)
        }
        op::SHR => (d >> n, d >> (n - 1) & 1, sign(d)),
        _ => {
            let s = i64::from(sext(d, size));
            ((s >> n) as u32 & mask(size), (s >> (n - 1)) as u32 & 1, 0)
        }
    };
    Some((r, szp(r, size) | cf | of * OF))
}
/// Jcc's condition `cc` (tttn) on `flags`.
pub fn condition(cc: u8, flags: u64) -> bool {
    let f = |bit: u32| flags as u32 & bit != 0;
    let lt = f(SF) != f(OF);
    let base = match cc >> 1 {
        0 => f(OF),
        1 => f(CF),
        2 => f(ZF),
        3 => f(CF) || f(ZF),
        4 => f(SF),
        5 => f(PF),
        6 => lt,
        _ => f(ZF) || lt,
    };
    base != (cc & 1 != 0)
}
/// The memory operand's offset in its segment, wrapped at the address size.
pub fn offset(cpu: &Cpu, i: &Insn) -> u32 {
    let reg = |r: u8| {
        if r == NO_REG {
            0
        } else {
            cpu.gpr[r as usize & 7] as u32
        }
    };
    reg(i.base)
        .wrapping_add(reg(i.index) << (i.scale & 3))
        .wrapping_add(i.disp)
        & mask(i.address)
}
/// The linear address of `size` bytes at `offset` in segment register `sreg`, after the segment's checks (layer 1).
pub fn linear(
    cpu: &Cpu,
    ctx: Context,
    sreg: usize,
    offset: u32,
    size: u32,
    write: bool,
) -> Result<u32, Exit> {
    let s = &cpu.seg[sreg.min(SREG_GS)];
    if !s.allows(u64::from(offset), u64::from(size), write, ctx.protected) {
        return Err(if sreg == SREG_SS { SS } else { GP });
    }
    Ok((s.base as u32).wrapping_add(offset))
}
/// [`linear`], then the memory's own check.
fn access<M: Memory>(
    m: &mut M,
    cpu: &Cpu,
    ctx: Context,
    sreg: usize,
    offset: u32,
    size: u32,
    write: bool,
) -> Result<u32, Exit> {
    let l = linear(cpu, ctx, sreg, offset, size, write)?;
    m.check(l, size, write)?;
    Ok(l)
}
fn set_sp(cpu: &mut Cpu, ctx: Context, v: u32) {
    set_register(cpu, 4, if ctx.stack32 { 4 } else { 2 }, v)
}
/// A resident operation's completion: EIP past the instruction.
pub fn complete(cpu: &mut Cpu, i: &Insn) {
    cpu.rip = u64::from((cpu.rip as u32).wrapping_add(u32::from(i.length)));
}
/// Releases `bytes` of stack (a far RET's slots and immediate), wrapping at the stack's size.
pub fn release(cpu: &mut Cpu, ctx: Context, bytes: u32) {
    set_sp(cpu, ctx, (cpu.gpr[4] as u32).wrapping_add(bytes))
}
/// An operand's value (a memory one from `linear`, already checked).
fn get<M: Memory>(cpu: &Cpu, m: &mut M, o: &Operand, linear: u32) -> u32 {
    match o.kind {
        kind::REG => register(cpu, o.reg, o.size),
        kind::MEM => m.read(linear, o.size),
        kind::SREG => u32::from(cpu.seg[usize::from(o.reg).min(SREG_GS)].selector),
        _ => o.value,
    }
}

/// Executes `i` (well formed, [`Insn::well_formed`]) at `cpu.rip` in `ctx`.
pub fn step<M: Memory>(cpu: &mut Cpu, ctx: Context, i: &Insn, m: &mut M) -> Result<Outcome, Exit> {
    let [a, b, _] = i.operands;
    let (o, size, seg) = (i.op, i.size, i.segment as usize);
    let n = u32::from(size);
    let next = (cpu.rip as u32).wrapping_add(u32::from(i.length));
    let smask = if ctx.stack32 { u32::MAX } else { 0xffff };
    let sp = cpu.gpr[4] as u32 & smask;
    let mut off = offset(cpu, i);
    let cs_limit = cpu.seg[SREG_CS].limit;
    // A near target: the operand size wraps it; past the CS limit it is #GP(0).
    let target = |disp: u32| {
        let t = next.wrapping_add(disp) & mask(size);
        if t > cs_limit { Err(GP) } else { Ok(t) }
    };
    let writes =
        a.kind == kind::MEM && !matches!(o, op::CMP | op::TEST | op::BT | op::MOVZX | op::LEA);
    // LOCK only on a lockable read-modify-write of memory (SDM Vol.2 LOCK: not CMP, TEST, BT, the shifts or MOV; the host
    // decodes the rest as #UD records).
    let lockable = matches!(o, op::ADD..=op::XOR | op::INC | op::DEC | op::BTS..=op::XADD);
    if i.lock != 0 && o < op::UD && !(writes && lockable) {
        return Err(UD);
    }
    let rip = match o {
        op::ADD..=op::LEA => {
            // The bit-string form: the signed bit offset selects the word that holds the bit.
            if matches!(o, op::BT..=op::BTC) && a.kind == kind::MEM && b.kind == kind::REG {
                let sh = if size == 2 { 4 } else { 5 };
                let words = sext(register(cpu, b.reg, size), size) >> sh;
                off = off.wrapping_add((words << (sh - 3)) as u32) & mask(i.address);
            }
            let memory = if a.kind == kind::MEM {
                a.size
            } else if b.kind == kind::MEM {
                b.size
            } else {
                0
            };
            let l = if memory != 0 && o != op::LEA {
                access(m, cpu, ctx, seg, off, u32::from(memory), writes)?
            } else {
                0
            };
            let s = if o == op::LEA {
                off
            } else {
                get(cpu, m, &b, l)
            };
            let carry = cpu.rflags & 1 != 0;
            let compute = |d: u32| -> (u32, u32, u32) {
                match o {
                    op::ADD..=op::TEST => {
                        let (r, f) =
                            alu(if o == op::TEST { op::AND } else { o }, d, s, carry, size);
                        (r, f, ARITHMETIC)
                    }
                    op::INC | op::DEC => {
                        let (r, f) = alu(
                            if o == op::INC { op::ADD } else { op::SUB },
                            d,
                            1,
                            false,
                            size,
                        );
                        (r, f, ARITHMETIC & !CF)
                    }
                    op::SHL..=op::SAR => match shift(o, d, s, size) {
                        Some((r, f)) => (r, f, ARITHMETIC),
                        None => (d, 0, 0),
                    },
                    op::BT..=op::BTC => {
                        let bit = 1 << (s & (n * 8 - 1));
                        let r = match o {
                            op::BTS => d | bit,
                            op::BTR => d & !bit,
                            op::BTC => d ^ bit,
                            _ => d,
                        };
                        (r, u32::from(d & bit != 0), CF)
                    }
                    op::XADD => {
                        let (r, f) = alu(op::ADD, d, s, false, size);
                        (r, f, ARITHMETIC)
                    }
                    // MOV, MOVZX (its source read zero-extended), LEA.
                    _ => (s, 0, 0),
                }
            };
            let (d, (r, f, fm)) = if i.lock != 0 {
                loop {
                    let d = m.read(l, size);
                    let x = compute(d);
                    if m.compare_exchange(l, size, d, x.0) == d {
                        break (d, x);
                    }
                }
            } else {
                let d = if matches!(o, op::MOV..=op::LEA) {
                    0
                } else {
                    get(cpu, m, &a, l)
                };
                (d, compute(d))
            };
            // XADD: the source register takes the old destination before the destination is written (one register: the sum).
            if o == op::XADD {
                set_register(cpu, b.reg, size, d);
            }
            if i.lock == 0 && !matches!(o, op::CMP | op::TEST | op::BT) {
                match a.kind {
                    kind::MEM => m.write(l, a.size, r),
                    _ => set_register(cpu, a.reg, a.size, r),
                }
            }
            cpu.rflags = cpu.rflags & !u64::from(fm) | u64::from(f & fm);
            next
        }
        op::PUSH | op::PUSHF | op::CALL => {
            let rip = if o == op::CALL {
                target(a.value)?
            } else {
                next
            };
            let slot = sp.wrapping_sub(n) & smask;
            let l = access(m, cpu, ctx, SREG_SS, slot, n, true)?;
            let v = match o {
                op::CALL => next,
                // VM and RF are not pushed.
                op::PUSHF => cpu.rflags as u32 & if size == 4 { 0x00fc_ffff } else { 0xffff },
                _ => get(cpu, m, &a, 0),
            };
            set_sp(cpu, ctx, slot);
            m.write(l, size, v);
            rip
        }
        op::POP | op::POPF | op::RET => {
            let l = access(m, cpu, ctx, SREG_SS, sp, n, false)?;
            let v = m.read(l, size);
            let rip = if o == op::RET {
                if v > cs_limit {
                    return Err(GP);
                }
                v
            } else {
                next
            };
            let imm = if a.kind == kind::IMM {
                a.value & 0xffff
            } else {
                0
            };
            set_sp(cpu, ctx, sp.wrapping_add(n + imm) & smask);
            match o {
                // (POP ESP/SP: the popped value, written after the increment.)
                op::POP => set_register(cpu, a.reg, size, v),
                op::POPF => {
                    // At CPL 0 every flag but VM, RF, VIF and VIP (and the reserved bits); IOPL only at CPL 0, IF only at a
                    // CPL not above IOPL. A 32-bit POPF clears RF.
                    let iopl = (cpu.rflags >> 12 & 3) as u8;
                    let mut keep = 0x7fd5 | if size == 4 { 0x24_0000 } else { 0 };
                    if ctx.protected && ctx.cpl > 0 {
                        keep &= !0x3000 & if ctx.cpl > iopl { !IF } else { !0 };
                    }
                    let f = cpu.rflags & !u64::from(keep) | u64::from(v & keep);
                    cpu.rflags = if size == 4 { f & !0x1_0000 } else { f } | 2;
                }
                _ => {}
            }
            rip
        }
        op::CLI => {
            if ctx.protected && u64::from(ctx.cpl) > cpu.rflags >> 12 & 3 {
                return Err(GP);
            }
            cpu.rflags &= !u64::from(IF);
            next
        }
        op::CLD => {
            cpu.rflags &= !u64::from(DF);
            next
        }
        op::NOP | op::PAUSE => next,
        op::JCC if !condition(i.cc, cpu.rflags) => next,
        op::JCC | op::JMP => target(a.value)?,
        op::LOOP => {
            let c = cpu.gpr[1] as u32 & mask(i.address);
            let (count, taken) = match i.cc {
                3 => (c, c == 0),
                cc => {
                    let c = c.wrapping_sub(1) & mask(i.address);
                    let zf = cpu.rflags as u32 & ZF != 0;
                    (c, c != 0 && (cc == 2 || zf == (cc == 1)))
                }
            };
            let rip = if taken { target(a.value)? } else { next };
            if i.cc != 3 {
                set_register(cpu, 1, i.address, count);
            }
            rip
        }
        op::JMP_FAR | op::RET_FAR => {
            let (selector, offset, release) = if o == op::RET_FAR {
                // Two pops: the offset at SP, the selector (the slot's low 16 bits) after it.
                // (Both slots' stack limit before either's memory: SDM Vol.2 RET, #SS before #PF.)
                let l0 = linear(cpu, ctx, SREG_SS, sp, n, false)?;
                let l1 = linear(cpu, ctx, SREG_SS, sp.wrapping_add(n) & smask, n, false)?;
                m.check(l0, n, false)?;
                m.check(l1, n, false)?;
                let imm = if a.kind == kind::IMM {
                    a.value & 0xffff
                } else {
                    0
                };
                (m.read(l1, 2), m.read(l0, size), 2 * n + imm)
            } else if a.kind == kind::MEM {
                let l = access(m, cpu, ctx, seg, off, n + 2, false)?;
                (m.read(l.wrapping_add(n), 2), m.read(l, size), 0)
            } else {
                (b.value, a.value, 0)
            };
            let (selector, offset) = (selector as u16, offset & mask(size));
            if ctx.protected {
                return Ok(Outcome::Resident(if o == op::JMP_FAR {
                    Resident::FarJump { selector, offset }
                } else {
                    Resident::FarReturn {
                        selector,
                        offset,
                        release,
                    }
                }));
            }
            // Real mode: CS := selector, base selector × 16, its limit and attributes as they are.
            if offset > cs_limit {
                return Err(GP);
            }
            release_and_load(cpu, ctx, SREG_CS, selector, release);
            offset
        }
        op::MOV_SREG => {
            if a.reg as usize == SREG_CS {
                return Err(UD);
            }
            let selector = if b.kind == kind::MEM {
                let l = access(m, cpu, ctx, seg, off, 2, false)?;
                m.read(l, 2)
            } else {
                register(cpu, b.reg, 2)
            } as u16;
            if ctx.protected {
                return Ok(Outcome::Resident(Resident::LoadSegment {
                    sreg: a.reg,
                    selector,
                }));
            }
            release_and_load(cpu, ctx, a.reg as usize, selector, 0);
            next
        }
        op::LGDT | op::LIDT => {
            let l = access(m, cpu, ctx, seg, off, 6, false)?;
            let (limit, base) = (m.read(l, 2) as u16, m.read(l.wrapping_add(2), 4));
            let base = if size == 2 { base & 0xff_ffff } else { base };
            return Ok(Outcome::Resident(Resident::LoadTable {
                idt: o == op::LIDT,
                base,
                limit,
            }));
        }
        op::MOV_TO_CR => {
            return Ok(Outcome::Resident(Resident::MovToCr {
                cr: a.reg,
                value: register(cpu, b.reg, 4),
            }));
        }
        op::MOV_FROM_CR => {
            return Ok(Outcome::Resident(Resident::MovFromCr {
                cr: b.reg,
                reg: a.reg,
            }));
        }
        op::RDMSR => return Ok(Outcome::Resident(Resident::Rdmsr)),
        op::WRMSR => return Ok(Outcome::Resident(Resident::Wrmsr)),
        op::CPUID => return Ok(Outcome::Resident(Resident::Cpuid)),
        op::HLT => return Ok(Outcome::Resident(Resident::Hlt)),
        op::WBINVD => return Ok(Outcome::Resident(Resident::Wbinvd)),
        op::UD => return Err(UD),
        op::TOO_LONG => return Err(GP),
        _ => return Err(Exit::Unsupported),
    };
    cpu.rip = u64::from(rip);
    Ok(Outcome::Done)
}
/// A real-mode segment load (base = selector × 16; limit and attributes unchanged), after releasing `bytes` of stack
/// (none: ESP is not written).
fn release_and_load(cpu: &mut Cpu, ctx: Context, sreg: usize, selector: u16, bytes: u32) {
    if bytes != 0 {
        release(cpu, ctx, bytes);
    }
    let s = &mut cpu.seg[sreg.min(SREG_GS)];
    (s.selector, s.base) = (selector, u64::from(selector) << 4);
}
