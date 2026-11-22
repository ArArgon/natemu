//! The boot-time interpreter's decode (M2 Part 2, plan section 3): iced-x86 in a 16- or 32-bit code segment, with Intel's
//! decoder behaviour, mapped to the record the resident executes ([`natemu_abi::interp::Insn`]), and the decode request
//! the resident makes for it ([`natemu_abi::interp::REQUEST_DECODE`]; the contract is that module's documentation). The
//! request bypasses fexc, the answer cache and admission, and reads no guest memory: the resident sends the bytes.
//!
//! The first version's forms (the plan's section 1 table) map to their operations; every other instruction iced decodes
//! is [`op::OTHER`] (a typed stop carrying its bytes), an encoding iced finds no instruction in (LOCK on a form that is
//! not lockable among them) is [`op::UD`], and one that needs a 16th byte is [`op::TOO_LONG`]. F2/F3 prefixes on forms
//! that do not take them are ignored, as the processor does.
use crate::{Result, execution::Area};
use iced_x86::{
    Code, Decoder, DecoderError, DecoderOptions, Instruction, Mnemonic, OpKind, Register,
};
use natemu_abi::execution::{SREG_CS, SREG_DS, STAGING_OFFSET, State};
use natemu_abi::interp::{
    DECODE_OFFSET, DECODED_OFFSET, Decode, Decoded, Insn, MAX_FETCH, MAX_RUN, NO_REG, Operand,
    REPLY_DECODE, ends_run, kind, op,
};

/// The operation of a decoded instruction (the first version's forms), else [`op::OTHER`].
fn class(i: &Instruction) -> u8 {
    use Code as C;
    use Mnemonic as M;
    let register = |k: u32| (i.op_kind(k) == OpKind::Register).then(|| i.op_register(k));
    let any = |f: fn(Register) -> bool| (0..i.op_count()).any(|k| register(k).is_some_and(f));
    match i.mnemonic() {
        M::Add => op::ADD,
        M::Or => op::OR,
        M::Adc => op::ADC,
        M::Sbb => op::SBB,
        M::And => op::AND,
        M::Sub => op::SUB,
        M::Xor => op::XOR,
        M::Cmp => op::CMP,
        M::Test => op::TEST,
        M::Inc => op::INC,
        M::Dec => op::DEC,
        M::Shl | M::Sal => op::SHL,
        M::Shr => op::SHR,
        M::Sar => op::SAR,
        M::Bt => op::BT,
        M::Bts => op::BTS,
        M::Btr => op::BTR,
        M::Btc => op::BTC,
        M::Xadd => op::XADD,
        // Debug- and test-register moves are not in the first version.
        M::Mov if any(Register::is_dr) || any(Register::is_tr) => op::OTHER,
        M::Mov if register(0).is_some_and(Register::is_cr) => op::MOV_TO_CR,
        M::Mov if register(1).is_some_and(Register::is_cr) => op::MOV_FROM_CR,
        M::Mov if register(0).is_some_and(Register::is_segment_register) => op::MOV_SREG,
        M::Mov => op::MOV,
        M::Movzx => op::MOVZX,
        M::Lea => op::LEA,
        // PUSH/POP register and PUSH immediate; not PUSH/POP r/m or the segment-register forms.
        M::Push => match i.code() {
            C::Push_r16
            | C::Push_r32
            | C::Pushw_imm8
            | C::Pushd_imm8
            | C::Push_imm16
            | C::Pushd_imm32 => op::PUSH,
            _ => op::OTHER,
        },
        M::Pop => match i.code() {
            C::Pop_r16 | C::Pop_r32 => op::POP,
            _ => op::OTHER,
        },
        M::Pushf | M::Pushfd => op::PUSHF,
        M::Popf | M::Popfd => op::POPF,
        M::Cli => op::CLI,
        M::Cld => op::CLD,
        M::Nop => op::NOP,
        M::Pause => op::PAUSE,
        M::Jo
        | M::Jno
        | M::Jb
        | M::Jae
        | M::Je
        | M::Jne
        | M::Jbe
        | M::Ja
        | M::Js
        | M::Jns
        | M::Jp
        | M::Jnp
        | M::Jl
        | M::Jge
        | M::Jle
        | M::Jg => op::JCC,
        // JMP short and near relative, far JMP ptr16:16/32 and m16:16/32; not JMP near indirect.
        M::Jmp => match i.code() {
            C::Jmp_rel8_16 | C::Jmp_rel8_32 | C::Jmp_rel16 | C::Jmp_rel32_32 => op::JMP,
            C::Jmp_ptr1616 | C::Jmp_ptr1632 | C::Jmp_m1616 | C::Jmp_m1632 => op::JMP_FAR,
            _ => op::OTHER,
        },
        M::Loopne | M::Loope | M::Loop | M::Jcxz | M::Jecxz => op::LOOP,
        // CALL near relative; not CALL near indirect or far CALL.
        M::Call => match i.code() {
            C::Call_rel16 | C::Call_rel32_32 => op::CALL,
            _ => op::OTHER,
        },
        M::Ret => op::RET,
        M::Retf => op::RET_FAR,
        M::Lgdt => op::LGDT,
        M::Lidt => op::LIDT,
        M::Rdmsr => op::RDMSR,
        M::Wrmsr => op::WRMSR,
        M::Cpuid => op::CPUID,
        M::Hlt => op::HLT,
        M::Wbinvd => op::WBINVD,
        _ => op::OTHER,
    }
}

/// The record of the instruction at the start of `bytes` in a `bits`-bit (16 or 32) code segment, or `None` when it, or
/// the verdict on it, needs bytes past them (a truncated decode).
pub(crate) fn record(bytes: &[u8], bits: u32) -> Option<Insn> {
    // iced calls an encoding with no instruction "no more bytes" unless 15 are there, so it decodes them padded: whatever
    // it read of the padding (an instruction or none) is truncated.
    let n = bytes.len().min(15);
    let mut padded = [0; 15];
    padded[..n].copy_from_slice(&bytes[..n]);
    let mut decoder = Decoder::new(bits, &padded, DecoderOptions::NONE);
    let i = decoder.decode();
    (i.len() <= n).then(|| insn(&i, decoder.last_error(), &padded, bits))
}
/// The record of `i`, decoded (at any IP) from `bytes` in a `bits`-bit code segment with `error` its decoder's verdict.
pub(crate) fn insn(i: &Instruction, error: DecoderError, bytes: &[u8], bits: u32) -> Insn {
    // Every record, a stop's too, has the sizes its prefixes give (the resident checks them all).
    let (size, address) = sizes(&bytes[..i.len()], bits);
    let mut r = Insn {
        length: i.len() as u8,
        size,
        address,
        ..Insn::NONE
    };
    r.bytes[..i.len()].copy_from_slice(&bytes[..i.len()]);
    // UD0, UD1 and UD2 are #UD by definition.
    if i.is_invalid() || matches!(i.mnemonic(), Mnemonic::Ud0 | Mnemonic::Ud1 | Mnemonic::Ud2) {
        let long =
            i.len() == 15 && error == DecoderError::InvalidInstruction && too_long(bytes, bits);
        r.op = if long { op::TOO_LONG } else { op::UD };
        return r;
    }
    r.op = class(i);
    if r.op != op::OTHER {
        match fields(i, bits, r) {
            Some(f) if f.well_formed() => r = f,
            _ => {
                debug_assert!(
                    false,
                    "a first-version form without its record: {:?}",
                    i.code()
                );
                r.op = op::OTHER;
            }
        }
    }
    r
}
/// Whether the 15 `bytes` iced found no instruction in need a 16th (#GP(0)) rather than end there with an invalid opcode
/// (#UD): iced stops at 15 bytes either way. A legacy prefix repeated later in the run changes nothing when dropped, so
/// the rest is decoded without the first byte: past 14 bytes it needs a 16th. Without a repeated prefix iced cannot tell
/// the two apart, and the instruction is taken as too long (natemu's choice). The length is iced's (for an undefined
/// two-byte opcode such as 0F 04 iced reads a ModRM byte too).
fn too_long(bytes: &[u8], bits: u32) -> bool {
    let run = bytes.iter().take_while(|b| legacy(b)).count();
    if bytes.get(1..run).is_none_or(|p| !p.contains(&bytes[0])) {
        return true;
    }
    let mut rest = [0; 15];
    rest[..14].copy_from_slice(&bytes[1..15]);
    Decoder::new(bits, &rest, DecoderOptions::NONE)
        .decode()
        .len()
        > 14
}
fn legacy(b: &u8) -> bool {
    matches!(
        b,
        0xf0 | 0xf2 | 0xf3 | 0x2e | 0x36 | 0x3e | 0x26 | 0x64 | 0x65 | 0x66 | 0x67
    )
}
/// The legacy prefixes' sizes: the operand and address size (bytes) in a `bits`-bit code segment, each toggled by its
/// prefix (66, 67).
fn sizes(bytes: &[u8], bits: u32) -> (u8, u8) {
    let prefixes = &bytes[..bytes.iter().take_while(|b| legacy(b)).count()];
    let size = |p: u8| {
        if (bits == 32) != prefixes.contains(&p) {
            4
        } else {
            2
        }
    };
    (size(0x66), size(0x67))
}
/// The record's fields from iced's operands, or `None` for an operand kind the first version has no use for.
fn fields(i: &Instruction, bits: u32, mut r: Insn) -> Option<Insn> {
    let (osize, asize) = sizes(&r.bytes[..r.length as usize], bits);
    (r.address, r.lock) = (asize, u8::from(i.has_lock_prefix()));
    let mut operands = Vec::with_capacity(3);
    for k in 0..i.op_count() {
        let operand = |kind, reg: u32, size: usize, value| Operand {
            kind,
            reg: reg as u8,
            size: size as u8,
            pad: 0,
            value,
        };
        match i.op_kind(k) {
            OpKind::Register => {
                let g = i.op_register(k);
                operands.push(if g.is_gpr8() || g.is_gpr16() || g.is_gpr32() {
                    operand(kind::REG, g.number() as u32, g.size(), 0)
                } else if g.is_segment_register() {
                    operand(kind::SREG, g.number() as u32, 2, 0)
                } else if g.is_cr() {
                    operand(kind::CR, g.number() as u32, 4, 0)
                } else {
                    return None;
                });
            }
            OpKind::Memory => {
                let number = |g: Register| {
                    if g == Register::None {
                        NO_REG
                    } else {
                        g.number() as u8
                    }
                };
                (r.base, r.index) = (number(i.memory_base()), number(i.memory_index()));
                r.scale = i.memory_index_scale().trailing_zeros() as u8;
                // (iced holds a 16-bit displacement zero-extended: the offset wraps at the address size anyway.)
                r.disp = i.memory_displacement64() as u32;
                r.segment = match i.memory_segment() {
                    Register::None => SREG_DS as u8,
                    s => s.number() as u8,
                };
                // (LEA's has no size: it accesses nothing; the operand size stands in.)
                let size = match i.memory_size().size() {
                    0 => usize::from(osize),
                    s => s,
                };
                operands.push(operand(kind::MEM, 0, size, 0));
            }
            OpKind::Immediate8 => operands.push(operand(kind::IMM, 0, 1, i.immediate(k) as u32)),
            OpKind::Immediate16 | OpKind::Immediate8to16 => {
                operands.push(operand(kind::IMM, 0, 2, i.immediate(k) as u32))
            }
            OpKind::Immediate32 | OpKind::Immediate8to32 => {
                operands.push(operand(kind::IMM, 0, 4, i.immediate(k) as u32))
            }
            // Relative to the next instruction (decoded at IP 0, so the target is the displacement past the instruction).
            OpKind::NearBranch16 | OpKind::NearBranch32 => {
                let d = i.near_branch_target().wrapping_sub(i.next_ip());
                let d = if i.op_kind(k) == OpKind::NearBranch16 {
                    d as u16 as i16 as u32
                } else {
                    d as u32
                };
                operands.push(operand(kind::IMM, 0, osize as usize, d));
            }
            // The offset, then the selector.
            OpKind::FarBranch16 | OpKind::FarBranch32 => {
                let offset = if i.op_kind(k) == OpKind::FarBranch16 {
                    i.far_branch16().into()
                } else {
                    i.far_branch32()
                };
                operands.push(operand(kind::IMM, 0, osize as usize, offset));
                operands.push(operand(kind::IMM, 0, 2, i.far_branch_selector().into()));
            }
            _ => return None,
        }
    }
    if operands.len() > 3 {
        return None;
    }
    r.operands[..operands.len()].copy_from_slice(&operands);
    r.size = match r.op {
        op::MOV_TO_CR | op::MOV_FROM_CR => 4,
        o if o <= op::LEA || o == op::MOV_SREG => r.operands[0].size,
        _ => osize,
    };
    r.cc = match i.mnemonic() {
        _ if r.op == op::JCC => i.condition_code() as u8 - 1,
        Mnemonic::Loopne => 0,
        Mnemonic::Loope => 1,
        Mnemonic::Loop => 2,
        Mnemonic::Jcxz | Mnemonic::Jecxz => 3,
        _ => 0,
    };
    Some(r)
}

/// The reply to a decode request: records for the straight run at the start of `bytes`, at most `max`.
#[cfg(test)]
pub(crate) fn run(bytes: &[u8], bits: u32, max: usize) -> Decoded {
    run_to(bytes, bits, max, usize::MAX)
}
/// [`run`], ending before an instruction that starts `limit` or more bytes in (the next stop the run's hook answers: no
/// record is ever made for a stop's address, so the resident never caches one).
pub(crate) fn run_to(bytes: &[u8], bits: u32, max: usize, limit: usize) -> Decoded {
    let mut out = Decoded {
        count: 0,
        pad: [0; 3],
        records: [Insn::NONE; MAX_RUN],
    };
    let mut at = 0;
    while (out.count as usize) < max.min(MAX_RUN) && (at == 0 || at < limit) {
        let Some(r) = record(&bytes[at..], bits) else {
            break;
        };
        out.records[out.count as usize] = r;
        out.count += 1;
        at += usize::from(r.length);
        if ends_run(r.op) {
            break;
        }
    }
    out
}
/// The linear address of the vCPU's next instruction in an interpreted mode: CS.base + EIP, wrapping at 32 bits (where
/// the run's hook is asked, `execution::Hook::at`).
pub(crate) fn linear_ip(state: &State) -> u64 {
    state.cpu.seg[SREG_CS].base.wrapping_add(state.cpu.rip) & 0xffff_ffff
}
/// Serves [`natemu_abi::interp::REQUEST_DECODE`]: the request from the vCPU's staging area, the reply into it; the run
/// ends before the instruction at `stop` (the next address the run's hook answers, above [`linear_ip`]), if any, and
/// before 4 GiB.
pub(crate) fn serve(area: Area, state: &mut State, stop: Option<u64>) -> Result<()> {
    // (And before 4 GiB: an address the run would reach past the wrap is one the hook was not asked about.)
    let to_wrap = (1 << 32) - linear_ip(state);
    let limit = usize::try_from(stop.map_or(to_wrap, |s| (s - linear_ip(state)).min(to_wrap))).unwrap_or(usize::MAX);
    let reply = answer(&area.read_record(STAGING_OFFSET + DECODE_OFFSET), limit)?;
    area.write_record(STAGING_OFFSET + DECODED_OFFSET, reply);
    state.reply = REPLY_DECODE;
    Ok(())
}
/// The reply to request `q` (ending before `limit` bytes in), or the error that ends the run for a malformed one.
fn answer(q: &Decode, limit: usize) -> Result<Decoded> {
    if !matches!(q.bits, 16 | 32)
        || !(1..=MAX_RUN as u32).contains(&q.max)
        || q.available as usize > MAX_FETCH
    {
        return Err(format!(
            "decode request with bits {}, max {}, available {}",
            q.bits, q.max, q.available
        ));
    }
    Ok(run_to(
        &q.bytes[..q.available as usize],
        q.bits,
        q.max as usize,
        limit,
    ))
}

#[cfg(test)]
mod tests;
