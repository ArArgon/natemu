//! System instructions the resident executes as transactions (`natemu_abi::execution::System`): the decode of the
//! instruction fexc cut a block before (fexc reports only its opcode map and opcode). iced-x86 decodes it, with Intel's
//! decoder behaviour, in the mode of the code segment (64, 32 or 16 bits); [`decode`] maps the form it finds to the record
//! and keeps natemu's recorded behaviour on top: CS/DS/ES/SS prefixes ignored in 64-bit mode (the operand's default
//! segment instead), F2/F3 refused on a form that does not take them, #UD for an encoding Intel defines no instruction for
//! (LOCK on one that is not lockable, a blank cell, an invalid operand type) and for the forms of a CPUID feature the
//! presented CPU does not advertise ([`available`]). A form with no record (another system instruction, an advertised one
//! nothing builds, a 32-bit-only form not built yet) is `None`: the vCPU gets fexc's reason (`Code::refused`, #UD).
use iced_x86::{
    Code, CpuidFeature, Decoder, DecoderOptions, Instruction, Mnemonic, OpKind, Register,
};
use natemu_abi::cpu_gates as gf;
use natemu_abi::execution::{SREG_DS, SREG_ES, SREG_FS, SREG_GS, SREG_SS, System, system};

/// The legacy prefixes as x86 reads them, with REX bytes among them: the opcode's index in `insn` and the REX in force
/// (0: none). Only a REX immediately before the opcode counts, the last of several; one followed by a legacy prefix is
/// ignored (SDM Vol.2 2.2.1, "other placements are ignored"; FEX's `DecodeREXIfValid` does the same). OpenSSL's
/// CRYPTOGAMS strings in `.text` decode as such chains (census, R2 review).
pub(crate) fn opcode_at(insn: &[u8]) -> (usize, u8) {
    let mut rex = 0;
    for (i, &b) in insn.iter().enumerate() {
        match b {
            0x40..=0x4f => rex = b,
            0xf0 | 0xf2 | 0xf3 | 0x66 | 0x67 | 0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 => rex = 0,
            _ => return (i, rex),
        }
    }
    (insn.len(), rex)
}

/// Whether fexc's opcode map and opcode (`FEXC_MAP_*`: 0 primary, 1..4 two-byte with no or an F3, F2, 66 overlay, 5 0f
/// 38, 6 0f 3a, 8 the opcode after a VEX prefix) name the opcode at the start of `bytes` in a `bitness`-bit code segment,
/// past its prefixes (REX bytes among them in 64-bit mode only). Outside 64-bit mode C4/C5 is VEX only with ModRM.mod
/// 11b, else LES/LDS. A disagreement is FEX and iced reading different instructions there.
pub(crate) fn cut_matches(bytes: &[u8], bitness: u32, (map, opcode): (u8, u8)) -> bool {
    let at = if bitness == 64 {
        opcode_at(bytes).0
    } else {
        let legacy = |b: &&u8| {
            matches!(
                b,
                0xf0 | 0xf2 | 0xf3 | 0x66 | 0x67 | 0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65
            )
        };
        bytes.iter().take_while(legacy).count()
    };
    let b = &bytes[at..];
    let vex = matches!(b, [0xc4 | 0xc5, m, ..] if bitness == 64 || m >> 6 == 3);
    match (map, b) {
        (8, [0xc5, _, op, ..] | [0xc4, _, _, op, ..]) => vex && *op == opcode,
        (5, [0x0f, 0x38, op, ..]) | (6, [0x0f, 0x3a, op, ..]) => *op == opcode,
        // An escape byte is no opcode of these maps.
        (1..=4, [0x0f, op, ..]) => !matches!(op, 0x38 | 0x3a) && *op == opcode,
        (0, [op, ..]) => !vex && *op != 0x0f && *op == opcode,
        _ => false,
    }
}

/// String instructions with an implicit based source; ES is fixed for their destination.
pub(crate) fn based_string(i: &Instruction) -> Option<u32> {
    use Code::*;
    if !matches!(i.segment_prefix(), Register::FS | Register::GS) {
        return None;
    }
    match i.code() {
        Movsb_m8_m8 | Movsw_m16_m16 | Movsd_m32_m32 => Some(system::MOVS),
        Cmpsb_m8_m8 | Cmpsw_m16_m16 | Cmpsd_m32_m32 => Some(system::CMPS),
        Lodsb_AL_m8 | Lodsw_AX_m16 | Lodsd_EAX_m32 => Some(system::LODS),
        _ => None,
    }
}

/// An instruction that reads past the bytes supplied, or whose verdict depends on bytes past them: the caller fetches the
/// next page first (its fetch fault comes before any #UD).
#[derive(Debug, PartialEq)]
pub(crate) struct Truncated;

/// Decodes the instruction at the start of `bytes`, which fexc cut as a system instruction, in a `bitness`-bit code
/// segment (64, 32 or 16). `None` for a form with no record.
pub(crate) fn decode(bytes: &[u8], bitness: u32) -> Result<Option<System>, Truncated> {
    use Code::*;
    use system::*;
    // `bytes` may end with the instruction: iced calls an encoding it finds no instruction in "no more bytes" unless 15 are
    // there, so it decodes them padded; whatever iced read of the padding (an instruction or no instruction) is truncated.
    let mut padded = [0; 15];
    padded[..bytes.len().min(15)].copy_from_slice(&bytes[..bytes.len().min(15)]);
    // The Reserved-NOP cells (0f 0d, 0f 18..1f) decode as such whatever prefetch, CET or MPX form they also are.
    let i = Decoder::new(bitness, &padded, DecoderOptions::FORCE_RESERVED_NOP).decode();
    // A one-byte opcode with no 64-bit form (SDM Vol.2 Table A-2 "i64", and D6): #UD by the opcode alone, though iced reads
    // a ModRM byte after it (one at the end of the bytes is no wait for the next page).
    let at = opcode_at(bytes).0;
    let i64_opcode = bitness == 64
        && i.is_invalid()
        && bytes.get(at).is_some_and(|op| {
            matches!(
                op,
                0x06 | 0x07 | 0x0e | 0x16 | 0x17 | 0x1e | 0x1f | 0x27 | 0x2f | 0x37 | 0x3f | 0x60 | 0x61 | 0x82 | 0x9a
                    | 0xce | 0xd4 | 0xd5 | 0xd6 | 0xea
            )
        });
    if i.len() > bytes.len() && !i64_opcode {
        return Err(Truncated);
    }
    // #UD leaves RIP at the instruction: no operand is used.
    let length = if i64_opcode { at as u32 + 1 } else { i.len() as u32 };
    let ud = Some(System {
        kind: UD,
        length,
        ..System::NONE
    });
    if i.is_invalid() || !available(&i) {
        return Ok(ud);
    }
    let mut s = System {
        length,
        lock: u32::from(i.has_lock_prefix()),
        ..System::NONE
    };
    let gpr = |k: u32| i.op_register(k).number() as u32;
    let width = |k: u32| i.op_register(k).size() as u32;
    // An r/m operand that is a register (a memory one is decoded below).
    let rm_reg = |k: u32| {
        if i.op_kind(k) == OpKind::Register {
            gpr(k)
        } else {
            0
        }
    };
    // F2/F3 prefixes the form does not take (SDM Vol.2 2.1.1: reserved): only those that name them below decode.
    let mut rep = false;
    let (kind, size) = match i.code() {
        Mov_cr_r32 | Mov_cr_r64 => {
            (s.cr, s.reg) = (gpr(0), gpr(1));
            (MOV_TO_CR, 0)
        }
        Mov_r32_cr | Mov_r64_cr => {
            (s.cr, s.reg) = (gpr(1), gpr(0));
            (MOV_FROM_CR, 0)
        }
        Mov_dr_r32 | Mov_dr_r64 => {
            (s.cr, s.reg) = (gpr(0), gpr(1));
            (MOV_TO_DR, 0)
        }
        Mov_r32_dr | Mov_r64_dr => {
            (s.cr, s.reg) = (gpr(1), gpr(0));
            (MOV_FROM_DR, 0)
        }
        // An FS/GS override on INVLPG: not decoded.
        Invlpg_m if matches!(i.segment_prefix(), Register::FS | Register::GS) => return Ok(None),
        Invlpg_m => (INVLPG, 0),
        Rdmsr => (RDMSR, 0),
        Wrmsr => (WRMSR, 0),
        Rdpmc => (RDPMC, 0),
        Cpuid => (CPUID, 0),
        Rdtsc => (RDTSC, 0),
        Rdtscp => (RDTSCP, 0),
        Swapgs => (SWAPGS, 0),
        Xgetbv => (XGETBV, 0),
        Xsetbv => (XSETBV, 0),
        Syscall => (SYSCALL, 0),
        // 66 on SYSRET (without REX.W) and RDFSBASE..WRGSBASE: iced ignores it, the SDM defines no 16-bit form (not decoded).
        Sysretd | Rdfsbase_r32 | Rdfsbase_r64 | Rdgsbase_r32 | Rdgsbase_r64 | Wrfsbase_r32
        | Wrfsbase_r64 | Wrgsbase_r32 | Wrgsbase_r64
            if bytes[..opcode_at(bytes).0].contains(&0x66) =>
        {
            return Ok(None);
        }
        Clts => (CLTS, 0),
        Sysenter => (SYSENTER, 0),
        // INVD, WBINVD and (F3) WBNOINVD.
        Invd | Wbinvd | Wbnoinvd => (WBINVD, 0),
        Int3 => (INT3, 0),
        Into => (INTO, 0),
        Int1 => (INT1, 0),
        Int_imm8 => {
            s.cr = u32::from(i.immediate8());
            (INT, 0)
        }
        Cli => (CLI, 0),
        Sti => (STI, 0),
        Hlt => (HLT, 0),
        Fninit => (FNINIT, 0),
        Fnclex => (FNCLEX, 0),
        Ud0 | Ud0_r16_rm16 | Ud0_r32_rm32 | Ud0_r64_rm64 | Ud1_r16_rm16 | Ud1_r32_rm32
        | Ud1_r64_rm64 | Ud2 => return Ok(ud),
        // The descriptor-table forms: `size` the operand size, 8 (64-bit mode: the 10-byte image), else 4 or 2 (the 6-byte
        // image; LGDT/LIDT keep 24 bits of the base at 2).
        Sgdt_m1632_16 | Sgdt_m1632 | Sgdt_m1664 => (SGDT, operand_size(i.code())),
        Sidt_m1632_16 | Sidt_m1632 | Sidt_m1664 => (SIDT, operand_size(i.code())),
        Lgdt_m1632_16 | Lgdt_m1632 | Lgdt_m1664 => (LGDT, operand_size(i.code())),
        Lidt_m1632_16 | Lidt_m1632 | Lidt_m1664 => (LIDT, operand_size(i.code())),
        // SMSW r/m16, r32, r64 (a memory destination is always 16 bits) and LMSW r/m16. F2/F3 on SMSW (reserved) are ignored,
        // as on the reference AMD machine (owner rule; user-mode probe, November 3: every F2/F3 form took the UMIP #GP of
        // plain SMSW, none #UD; plan §5).
        Smsw_rm16 | Smsw_r32m16 | Smsw_r64m16 => {
            (s.reg, rep) = (rm_reg(0), true);
            (SMSW, operand_size(i.code()))
        }
        Lmsw_rm16 | Lmsw_r32m16 | Lmsw_r64m16 => {
            s.reg = rm_reg(0);
            (LMSW, 0)
        }
        // The 0f 00 group and MOV Sreg (the segment register in `cr`): the operand size, a register destination's (2: its
        // low 16 bits; 4, 8: the selector zero-extended); a memory operand is always 16 bits.
        Sldt_rm16 | Sldt_r32m16 | Sldt_r64m16 | Str_rm16 | Str_r32m16 | Str_r64m16 | Lldt_rm16
        | Lldt_r32m16 | Lldt_r64m16 | Ltr_rm16 | Ltr_r32m16 | Ltr_r64m16 | Verr_rm16
        | Verr_r32m16 | Verr_r64m16 | Verw_rm16 | Verw_r32m16 | Verw_r64m16 => {
            s.reg = rm_reg(0);
            let kind = match i.mnemonic() {
                Mnemonic::Sldt => SLDT,
                Mnemonic::Str => STR,
                Mnemonic::Lldt => LLDT,
                Mnemonic::Ltr => LTR,
                Mnemonic::Verr => VERR,
                _ => VERW,
            };
            (kind, operand_size(i.code()))
        }
        Mov_rm16_Sreg | Mov_r32m16_Sreg | Mov_r64m16_Sreg => {
            (s.cr, s.reg) = (gpr(1), rm_reg(0));
            (MOV_FROM_SREG, operand_size(i.code()))
        }
        Mov_Sreg_rm16 | Mov_Sreg_r32m16 | Mov_Sreg_r64m16 => {
            (s.cr, s.reg) = (gpr(0), rm_reg(1));
            (MOV_TO_SREG, operand_size(i.code()))
        }
        // LAR/LSL r, r/m16: the destination in `cr`.
        Lar_r16_rm16 | Lar_r32_r32m16 | Lar_r64_r64m16 => {
            (s.cr, s.reg) = (gpr(0), rm_reg(1));
            (LAR, width(0))
        }
        Lsl_r16_rm16 | Lsl_r32_r32m16 | Lsl_r64_r64m16 => {
            (s.cr, s.reg) = (gpr(0), rm_reg(1));
            (LSL, width(0))
        }
        // PUSH/POP Sreg (FS and GS only in 64-bit mode, 64-bit operand size by default there, 16 with 66, REX.W wins): the
        // segment register in `cr`.
        Pushw_ES | Pushw_CS | Pushw_SS | Pushw_DS | Pushw_FS | Pushw_GS | Pushd_ES | Pushd_CS
        | Pushd_SS | Pushd_DS | Pushd_FS | Pushd_GS | Pushq_FS | Pushq_GS => {
            s.cr = gpr(0);
            (PUSH_SREG, operand_size(i.code()))
        }
        Popw_ES | Popw_SS | Popw_DS | Popw_FS | Popw_GS | Popd_ES | Popd_SS | Popd_DS | Popd_FS
        | Popd_GS | Popq_FS | Popq_GS => {
            s.cr = gpr(0);
            (POP_SREG, operand_size(i.code()))
        }
        // M2 task 3b: forms only 32- and 16-bit code has (their opcodes are #UD or other instructions in 64-bit mode, and the
        // 64-bit LSS/LFS/LGS stay refused). LDS/LES/LSS/LFS/LGS: the segment register in `cr`, the destination in `reg`.
        Les_r16_m1616 | Les_r32_m1632 | Lds_r16_m1616 | Lds_r32_m1632 | Lss_r16_m1616
        | Lss_r32_m1632 | Lfs_r16_m1616 | Lfs_r32_m1632 | Lgs_r16_m1616 | Lgs_r32_m1632
            if bitness != 64 =>
        {
            s.reg = gpr(0);
            s.cr = match i.mnemonic() {
                Mnemonic::Les => SREG_ES,
                Mnemonic::Lds => SREG_DS,
                Mnemonic::Lss => SREG_SS,
                Mnemonic::Lfs => SREG_FS,
                _ => SREG_GS,
            } as u32;
            (LOAD_FAR, width(0))
        }
        // ARPL r/m16, r16 (a 32-bit operand size changes nothing): the source in `cr`.
        Arpl_rm16_r16 | Arpl_r32m16_r32 => {
            (s.reg, s.cr) = (rm_reg(0), gpr(1));
            (ARPL, 2)
        }
        Bound_r16_m1616 | Bound_r32_m3232 => {
            s.cr = gpr(0);
            (BOUND, width(0))
        }
        Pushaw => (PUSHA, 2),
        Pushad => (PUSHA, 4),
        Popaw => (POPA, 2),
        Popad => (POPA, 4),
        Aam_imm8 => {
            s.cr = u32::from(i.immediate8());
            (AAM, 0)
        }
        // JMP/CALL far ptr16:16/32: the offset in `disp`, the selector in `cr` (`memory` 0).
        Jmp_ptr1616 | Jmp_ptr1632 | Call_ptr1616 | Call_ptr1632 => {
            let short = matches!(i.code(), Jmp_ptr1616 | Call_ptr1616);
            s.cr = u32::from(i.far_branch_selector());
            s.disp = if short {
                i64::from(i.far_branch16())
            } else {
                i64::from(i.far_branch32())
            };
            let jmp = matches!(i.code(), Jmp_ptr1616 | Jmp_ptr1632);
            (if jmp { JMP_FAR } else { CALL_FAR }, if short { 2 } else { 4 })
        }
        // SYSEXIT and SYSRET: REX.W to 64-bit mode, else compatibility mode.
        Sysexitd => (SYSEXIT, 4),
        Sysexitq => (SYSEXIT, 8),
        Sysretd => (SYSRET, 4),
        Sysretq => (SYSRET, 8),
        // JMP/CALL far m16:16/32/64 (a register operand is no instruction: #UD above), RET far and IRET: the offset width.
        Jmp_m1616 => (JMP_FAR, 2),
        Jmp_m1632 => (JMP_FAR, 4),
        Jmp_m1664 => (JMP_FAR, 8),
        Call_m1616 => (CALL_FAR, 2),
        Call_m1632 => (CALL_FAR, 4),
        Call_m1664 => (CALL_FAR, 8),
        // RET far imm16: `disp` the bytes released.
        Retfw_imm16 | Retfd_imm16 | Retfq_imm16 => {
            s.disp = i64::from(i.immediate16());
            (RET_FAR, operand_size(i.code()))
        }
        Retfw | Retfd | Retfq => (RET_FAR, operand_size(i.code())),
        Iretw => (IRET, 2),
        Iretd => (IRET, 4),
        Iretq => (IRET, 8),
        // POPF pops 8 bytes in 64-bit mode (REX.W changes nothing), 2 with a 66 prefix.
        Popfw => (POPF, 2),
        Popfd => (POPF, 4),
        Popfq => (POPF, 8),
        // ENTER: `disp` the unsigned allocation, `cr` the nesting level modulo 32.
        Enterw_imm16_imm8 | Enterd_imm16_imm8 | Enterq_imm16_imm8 => {
            (s.disp, s.cr) = (
                i64::from(i.immediate16()),
                u32::from(i.immediate8_2nd() & 31),
            );
            (ENTER, operand_size(i.code()))
        }
        // L3: IN/OUT name their port by an imm8 (`reg` NO_REG, the port in `cr`) or DX (`reg` 2); 8, 16 or 32 bits (REX.W
        // does not promote port I/O, SDM Vol.2 IN/OUT/INS/OUTS). INS/OUTS: F3 is REP (F2 there is reserved, not decoded);
        // INS writes ES:rDI (no override), OUTS reads rSI in its segment.
        In_AL_imm8 | In_AX_imm8 | In_EAX_imm8 | Out_imm8_AL | Out_imm8_AX | Out_imm8_EAX => {
            (s.reg, s.cr) = (NO_REG, u32::from(i.immediate8()));
            let out = matches!(i.code(), Out_imm8_AL | Out_imm8_AX | Out_imm8_EAX);
            (if out { OUT } else { IN }, width(u32::from(out)))
        }
        In_AL_DX | In_AX_DX | In_EAX_DX => {
            s.reg = 2;
            (IN, width(0))
        }
        Out_DX_AL | Out_DX_AX | Out_DX_EAX => {
            s.reg = 2;
            (OUT, width(1))
        }
        _ if bitness == 32 && based_string(&i).is_some() => {
            let kind = based_string(&i).unwrap();
            if address_size(&i) != ADDRESS32 || (i.has_repne_prefix() && kind != CMPS) {
                return Ok(None);
            }
            rep = true;
            s.cr = u32::from(i.has_repne_prefix());
            s.prefixes = ADDRESS32 | u32::from(i.has_rep_prefix() || i.has_repne_prefix()) * REP;
            s.segment = i.segment_prefix().number() as u32;
            (kind, i.memory_size().size() as u32)
        }
        Insb_m8_DX | Insw_m16_DX | Insd_m32_DX | Outsb_DX_m8 | Outsw_DX_m16 | Outsd_DX_m32 => {
            let ins = matches!(i.code(), Insb_m8_DX | Insw_m16_DX | Insd_m32_DX);
            if i.has_repne_prefix() {
                return Ok(None);
            }
            rep = true;
            s.prefixes = u32::from(i.has_rep_prefix()) * REP | address_size(&i);
            s.segment = if ins {
                SREG_ES as u32
            } else {
                segment(&i, bitness)
            };
            (if ins { INS } else { OUTS }, i.memory_size().size() as u32)
        }
        // Memory RCL/RCR by CL (`reg` 1) or an imm8 (`reg` NO_REG, the count in `cr` bits 8:1); `cr` bit 0 RCR.
        Rcl_rm8_CL | Rcl_rm16_CL | Rcl_rm32_CL | Rcl_rm64_CL | Rcr_rm8_CL | Rcr_rm16_CL
        | Rcr_rm32_CL | Rcr_rm64_CL | Rcl_rm8_imm8 | Rcl_rm16_imm8 | Rcl_rm32_imm8
        | Rcl_rm64_imm8 | Rcr_rm8_imm8 | Rcr_rm16_imm8 | Rcr_rm32_imm8 | Rcr_rm64_imm8
            if i.op0_kind() == OpKind::Memory =>
        {
            let by_cl = i.op1_kind() == OpKind::Register;
            s.reg = if by_cl { 1 } else { NO_REG };
            s.cr = u32::from(i.mnemonic() == Mnemonic::Rcr)
                | if by_cl {
                    0
                } else {
                    u32::from(i.immediate8()) << 1
                };
            (ROTATE, i.memory_size().size() as u32)
        }
        // SHLD/SHRD r/m16, r16, CL: `cr` bits 3:0 the source, bit 4 SHRD.
        Shld_rm16_r16_CL | Shrd_rm16_r16_CL => {
            s.reg = rm_reg(0);
            s.cr = gpr(1) | if i.code() == Shrd_rm16_r16_CL { 16 } else { 0 };
            (DOUBLE_SHIFT16, 2)
        }
        // X2: BT/BTS/BTR/BTC with a memory operand, cut only under alignment checking (fexc --alignment-gated): `cr` the
        // operation, `reg` the bit-offset register (NO_REG for the imm8 form), `size` the operand.
        Bt_rm16_r16 | Bt_rm32_r32 | Bt_rm64_r64 | Bts_rm16_r16 | Bts_rm32_r32 | Bts_rm64_r64
        | Btr_rm16_r16 | Btr_rm32_r32 | Btr_rm64_r64 | Btc_rm16_r16 | Btc_rm32_r32
        | Btc_rm64_r64 | Bt_rm16_imm8 | Bt_rm32_imm8 | Bt_rm64_imm8 | Bts_rm16_imm8
        | Bts_rm32_imm8 | Bts_rm64_imm8 | Btr_rm16_imm8 | Btr_rm32_imm8 | Btr_rm64_imm8
        | Btc_rm16_imm8 | Btc_rm32_imm8 | Btc_rm64_imm8
            if i.op0_kind() == OpKind::Memory =>
        {
            s.reg = if i.op1_kind() == OpKind::Register {
                gpr(1)
            } else {
                NO_REG
            };
            s.cr = match i.mnemonic() {
                Mnemonic::Bt => 0,
                Mnemonic::Bts => 1,
                Mnemonic::Btr => 2,
                _ => 3,
            };
            (BT_ALIGNED, i.memory_size().size() as u32)
        }
        // I6: the x87 forms the resident executes; the environment and save images are the 16-bit ones with a 66 prefix
        // (`size` 2, else 4; REX.W wins).
        Fldcw_m2byte => (FLDCW, 0),
        Fld_m80fp => (FLD80, 0),
        Fstp_m80fp => (FSTP80, 0),
        Fldenv_m14byte => (FLDENV, 2),
        Fldenv_m28byte => (FLDENV, 4),
        Fnstenv_m14byte => (FNSTENV, 2),
        Fnstenv_m28byte => (FNSTENV, 4),
        Frstor_m94byte => (FRSTOR, 2),
        Frstor_m108byte => (FRSTOR, 4),
        Fnsave_m94byte => (FNSAVE, 2),
        Fnsave_m108byte => (FNSAVE, 4),
        // I5b: FXSAVE, FXRSTOR, XSAVE, XRSTOR, XSAVEC; XSAVEOPT is XSAVE's transaction (what it stores with XINUSE and
        // XMODIFIED all ones, SDM Vol.1 13.9).
        Fxsave_m512byte | Fxsave64_m512byte => (FXSAVE, 0),
        Fxrstor_m512byte | Fxrstor64_m512byte => (FXRSTOR, 0),
        Xsave_mem | Xsave64_mem | Xsaveopt_mem | Xsaveopt64_mem => (XSAVE, 0),
        Xrstor_mem | Xrstor64_mem => (XRSTOR, 0),
        Xsavec_mem | Xsavec64_mem => (XSAVEC, 0),
        Ldmxcsr_m32 => (LDMXCSR, 0),
        Stmxcsr_m32 => (STMXCSR, 0),
        VEX_Vldmxcsr_m32 => (VLDMXCSR, 0),
        VEX_Vstmxcsr_m32 => (VSTMXCSR, 0),
        // CLFLUSH, CLFLUSHOPT and CLWB: the same transaction, the faults of a byte load (SDM Vol.2 each).
        Clflush_m8 | Clflushopt_m8 | Clwb_m8 => (CLFLUSH, 0),
        Rdfsbase_r32 | Rdfsbase_r64 | Rdgsbase_r32 | Rdgsbase_r64 | Wrfsbase_r32 | Wrfsbase_r64
        | Wrgsbase_r32 | Wrgsbase_r64 | Rdrand_r16 | Rdrand_r32 | Rdrand_r64 => {
            s.reg = gpr(0);
            let kind = match i.mnemonic() {
                Mnemonic::Rdfsbase => RDFSBASE,
                Mnemonic::Rdgsbase => RDGSBASE,
                Mnemonic::Wrfsbase => WRFSBASE,
                Mnemonic::Wrgsbase => WRGSBASE,
                _ => RDRAND,
            };
            (kind, width(0))
        }
        // X1c: a Reserved-NOP cell fexc cut (its rule decides which): the operand decoded for the length, nothing accessed;
        // any 66/F2/F3 prefix. A register-operand 0f 0d is fexc's #UD (`cut_ud`), never decoded here.
        Reservednop_rm16_r16_0F0D | Reservednop_rm32_r32_0F0D | Reservednop_rm64_r64_0F0D
            if i.op0_kind() != OpKind::Register =>
        {
            rep = true;
            (RESERVED_NOP, 0)
        }
        Reservednop_rm16_r16_0F18
        | Reservednop_rm32_r32_0F18
        | Reservednop_rm64_r64_0F18
        | Reservednop_rm16_r16_0F19
        | Reservednop_rm32_r32_0F19
        | Reservednop_rm64_r64_0F19
        | Reservednop_rm16_r16_0F1A
        | Reservednop_rm32_r32_0F1A
        | Reservednop_rm64_r64_0F1A
        | Reservednop_rm16_r16_0F1B
        | Reservednop_rm32_r32_0F1B
        | Reservednop_rm64_r64_0F1B
        | Reservednop_rm16_r16_0F1C
        | Reservednop_rm32_r32_0F1C
        | Reservednop_rm64_r64_0F1C
        | Reservednop_rm16_r16_0F1D
        | Reservednop_rm32_r32_0F1D
        | Reservednop_rm64_r64_0F1D => {
            (s.reg, rep) = (rm_reg(0), true);
            (RESERVED_NOP, 0)
        }
        // The AVX2 gathers (VSIB, SDM Vol.2 2.3.12): destination `reg`, vector index `index`; `cr` the mask (bits 3:0),
        // VEX.L (bit 4) and a quadword index (bit 5); `size` the element (VEX.W).
        VEX_Vpgatherdd_xmm_vm32x_xmm
        | VEX_Vpgatherdd_ymm_vm32y_ymm
        | VEX_Vpgatherdq_xmm_vm32x_xmm
        | VEX_Vpgatherdq_ymm_vm32x_ymm
        | VEX_Vpgatherqd_xmm_vm64x_xmm
        | VEX_Vpgatherqd_xmm_vm64y_xmm
        | VEX_Vpgatherqq_xmm_vm64x_xmm
        | VEX_Vpgatherqq_ymm_vm64y_ymm
        | VEX_Vgatherdps_xmm_vm32x_xmm
        | VEX_Vgatherdps_ymm_vm32y_ymm
        | VEX_Vgatherdpd_xmm_vm32x_xmm
        | VEX_Vgatherdpd_ymm_vm32x_ymm
        | VEX_Vgatherqps_xmm_vm64x_xmm
        | VEX_Vgatherqps_xmm_vm64y_xmm
        | VEX_Vgatherqpd_xmm_vm64x_xmm
        | VEX_Vgatherqpd_ymm_vm64y_ymm => {
            let wide = i.op0_register().is_ymm() || i.memory_index().is_ymm();
            s.reg = gpr(0);
            s.cr = gpr(2) | u32::from(wide) << 4 | u32::from(i.is_vsib64()) << 5;
            (GATHER, i.memory_size().size() as u32)
        }
        // The masked moves ([`system::MASKMOV`]): VMASKMOVPS/PD and VPMASKMOVD/Q (data `reg`, mask VEX.vvvv: `cr` bits 3:0,
        // VEX.L bit 4, a store bit 5, VEX bit 6; elements of 4 or 8 bytes); VMASKMOVDQU, MASKMOVDQU and MASKMOVQ (data
        // ModRM.reg, mask ModRM.rm, memory rDI: bit 5, and bit 7 for MMX's eight registers; bytes).
        VEX_Vmaskmovps_xmm_xmm_m128
        | VEX_Vmaskmovps_ymm_ymm_m256
        | VEX_Vmaskmovpd_xmm_xmm_m128
        | VEX_Vmaskmovpd_ymm_ymm_m256
        | VEX_Vpmaskmovd_xmm_xmm_m128
        | VEX_Vpmaskmovd_ymm_ymm_m256
        | VEX_Vpmaskmovq_xmm_xmm_m128
        | VEX_Vpmaskmovq_ymm_ymm_m256 => {
            s.reg = gpr(0);
            s.cr = gpr(1) | u32::from(i.op0_register().is_ymm()) << 4 | 64;
            (MASKMOV, i.memory_size().element_size() as u32)
        }
        VEX_Vmaskmovps_m128_xmm_xmm
        | VEX_Vmaskmovps_m256_ymm_ymm
        | VEX_Vmaskmovpd_m128_xmm_xmm
        | VEX_Vmaskmovpd_m256_ymm_ymm
        | VEX_Vpmaskmovd_m128_xmm_xmm
        | VEX_Vpmaskmovd_m256_ymm_ymm
        | VEX_Vpmaskmovq_m128_xmm_xmm
        | VEX_Vpmaskmovq_m256_ymm_ymm => {
            s.reg = gpr(2);
            s.cr = gpr(1) | u32::from(i.op2_register().is_ymm()) << 4 | 32 | 64;
            (MASKMOV, i.memory_size().element_size() as u32)
        }
        VEX_Vmaskmovdqu_rDI_xmm_xmm | Maskmovdqu_rDI_xmm_xmm | Maskmovq_rDI_mm_mm => {
            s.reg = gpr(1);
            s.cr = gpr(2)
                | 32
                | match i.code() {
                    VEX_Vmaskmovdqu_rDI_xmm_xmm => 64,
                    Maskmovq_rDI_mm_mm => 128,
                    _ => 0,
                };
            s.base = 7;
            (MASKMOV, 1)
        }
        // VCVTPS2PH m64/m128, xmm/ymm, imm8: the source in `reg`, the immediate in `cr`, the stored bytes in `size`.
        VEX_Vcvtps2ph_xmmm64_xmm_imm8 | VEX_Vcvtps2ph_xmmm128_ymm_imm8
            if i.op0_kind() == OpKind::Memory =>
        {
            (s.reg, s.cr) = (gpr(1), u32::from(i.immediate8()));
            (CVTPS2PH, i.memory_size().size() as u32)
        }
        _ => return Ok(None),
    };
    if !rep && (i.has_rep_prefix() || i.has_repne_prefix()) {
        return Ok(None);
    }
    (s.kind, s.size) = (kind, size);
    let memory = (0..i.op_count()).map(|k| i.op_kind(k)).find(|&k| {
        matches!(
            k,
            OpKind::Memory | OpKind::MemorySegDI | OpKind::MemorySegEDI | OpKind::MemorySegRDI
        )
    });
    match memory {
        Some(OpKind::Memory) => {
            let (base, index) = (i.memory_base(), i.memory_index());
            s.base = match base {
                Register::RIP | Register::EIP => RIP,
                Register::None => NO_REG,
                r => r.number() as u32,
            };
            if index != Register::None {
                (s.index, s.scale) = (
                    index.number() as u32,
                    i.memory_index_scale().trailing_zeros(),
                );
            }
            // iced holds a RIP-relative operand's target (from IP 0), other displacements at the address size.
            let disp = i.memory_displacement64();
            s.prefixes = address_size(&i);
            s.disp = match (s.base, s.prefixes) {
                (RIP, _) => disp.wrapping_sub(i.next_ip()) as i32 as i64,
                (_, ADDRESS32) => disp as i32 as i64,
                (_, ADDRESS16) => disp as i16 as i64,
                _ => disp as i64,
            };
            (s.memory, s.segment) = (1, segment(&i, bitness));
        }
        // The rDI operand of the byte-masked moves (DS:rDI, overridable).
        Some(OpKind::MemorySegDI | OpKind::MemorySegEDI | OpKind::MemorySegRDI) => {
            (s.memory, s.prefixes, s.segment) = (1, address_size(&i), segment(&i, bitness));
        }
        _ => {}
    }
    Ok(s.well_formed().then_some(s))
}

/// The operand size (bytes) of the forms [`decode`] takes it from iced's name of: `_rm16`/`w` 2, `_r64m16`/`q` 8, else 4.
fn operand_size(c: Code) -> u32 {
    use Code::*;
    match c {
        Smsw_rm16 | Sldt_rm16 | Str_rm16 | Lldt_rm16 | Ltr_rm16 | Verr_rm16 | Verw_rm16
        | Mov_rm16_Sreg | Mov_Sreg_rm16 | Pushw_ES | Pushw_CS | Pushw_SS | Pushw_DS | Pushw_FS
        | Pushw_GS | Popw_ES | Popw_SS | Popw_DS | Popw_FS | Popw_GS | Enterw_imm16_imm8
        | Retfw | Retfw_imm16 | Sgdt_m1632_16 | Sidt_m1632_16 | Lgdt_m1632_16 | Lidt_m1632_16 => 2,
        Smsw_r64m16 | Sldt_r64m16 | Str_r64m16 | Lldt_r64m16 | Ltr_r64m16 | Verr_r64m16
        | Verw_r64m16 | Mov_r64m16_Sreg | Mov_Sreg_r64m16 | Pushq_FS | Pushq_GS | Popq_FS
        | Popq_GS | Enterq_imm16_imm8 | Retfq | Retfq_imm16 | Sgdt_m1664 | Sidt_m1664
        | Lgdt_m1664 | Lidt_m1664 => 8,
        _ => 4,
    }
}

/// [`system::ADDRESS32`] or [`system::ADDRESS16`] for a memory operand of that address size (0x67 in 64-bit mode; the
/// default or 0x67 in 32- and 16-bit code), else 0.
fn address_size(i: &Instruction) -> u32 {
    let bytes = (0..i.op_count()).find_map(|k| match i.op_kind(k) {
        OpKind::MemorySegSI | OpKind::MemoryESDI | OpKind::MemorySegDI => Some(2),
        OpKind::MemorySegESI | OpKind::MemoryESEDI | OpKind::MemorySegEDI => Some(4),
        OpKind::Memory => Some(match (i.memory_base(), i.memory_index()) {
            (Register::None, x) if x == Register::None || i.is_vsib() => {
                i.memory_displ_size() as usize
            }
            (Register::None, x) => x.size(),
            (b, _) => b.size(),
        }),
        _ => None,
    });
    match bytes {
        Some(4) => system::ADDRESS32,
        Some(2) => system::ADDRESS16,
        _ => 0,
    }
}

/// The segment register ([`natemu_abi::execution::SREG_ES`] ..) of the memory operand: its override, else its default
/// (SS for an rSP or rBP base, else DS). CS/DS/ES/SS overrides are ignored in 64-bit mode (owner decision, November 3:
/// as on the reference AMD machine), so the default decides a non-canonical address's exception there; of several FS/GS
/// overrides the last applies (the same machine; iced's priority of FS/GS).
fn segment(i: &Instruction, bitness: u32) -> u32 {
    let segment = i.memory_segment();
    if bitness == 64 && !matches!(segment, Register::FS | Register::GS) {
        let stack = matches!(
            i.memory_base(),
            Register::RSP | Register::RBP | Register::ESP | Register::EBP
        );
        return if stack { SREG_SS } else { SREG_DS } as u32;
    }
    segment.number() as u32
}

/// Whether the presented CPU has the instruction: every one of iced's CPUID features for it (XTEST's either of HLE and
/// RTM) that the catalogue (generated `natemu_abi::cpu_gates`) lists is advertised, and none is one no profile has
/// ([`NEVER`]); else the instruction is #UD (SDM Vol.2 each: "#UD If CPUID ... = 0", or "not in VMX operation"). Features
/// outside both (X64, INTEL386, MSR ..) are every 64-bit processor's.
fn available(i: &Instruction) -> bool {
    let has = |f| catalogue(f).map_or(!NEVER.contains(&f), advertised);
    i.cpuid_features().iter().all(|&f| match f {
        CpuidFeature::HLE_or_RTM => has(CpuidFeature::HLE) || has(CpuidFeature::RTM),
        f => has(f),
    })
}
/// The features of the system instructions the catalogue does not list, which the presented CPU has none of: ENCLV
/// (OSS), the SVM group, PCONFIG, WRMSRNS, MONITORX/MWAITX, CLZERO, RDPRU, the SEV-SNP and INVLPGB groups, TDX's SEAM
/// forms, FRED's ERETS/ERETU, LKGS, PBNDKB (TSE), GETSEC (SMX).
const NEVER: &[CpuidFeature] = {
    use CpuidFeature::*;
    &[
        OSS,
        SVM,
        SKINIT,
        SKINIT_or_SVM,
        PCONFIG,
        WRMSRNS,
        MSRLIST,
        MONITORX,
        CLZERO,
        RDPRU,
        SEV_SNP,
        SEV_ES,
        RMPQUERY,
        INVLPGB,
        TDX,
        FRED,
        LKGS,
        TSE,
        SMX,
    ]
};
/// The catalogue's CPUID bit of an iced feature (iced's names are the catalogue's, but SGX1, PTWRITE (leaf 14H's, which is
/// Intel PT's) and the VMX instructions iced names apart).
fn catalogue(f: CpuidFeature) -> Option<Feature> {
    use CpuidFeature as F;
    Some(match f {
        F::SSE3 => gf::GF_SSE3,
        F::PCLMULQDQ => gf::GF_PCLMULQDQ,
        F::MONITOR => gf::GF_MONITOR,
        F::VMX | F::INVEPT | F::INVVPID => gf::GF_VMX,
        F::SSSE3 => gf::GF_SSSE3,
        F::SSE4_1 => gf::GF_SSE4_1,
        F::SSE4_2 => gf::GF_SSE4_2,
        F::MOVBE => gf::GF_MOVBE,
        F::AES => gf::GF_AES,
        F::RDRAND => gf::GF_RDRAND,
        F::FSGSBASE => gf::GF_FSGSBASE,
        F::SGX1 => gf::GF_SGX,
        F::HLE => gf::GF_HLE,
        F::INVPCID => gf::GF_INVPCID,
        F::RTM => gf::GF_RTM,
        F::RDSEED => gf::GF_RDSEED,
        F::ADX => gf::GF_ADX,
        F::SMAP => gf::GF_SMAP,
        F::CLFLUSHOPT => gf::GF_CLFLUSHOPT,
        F::CLWB => gf::GF_CLWB,
        F::PTWRITE => gf::GF_PT,
        F::SHA => gf::GF_SHA,
        F::PKU => gf::GF_PKU,
        F::CET_SS => gf::GF_CET_SS,
        F::GFNI => gf::GF_GFNI,
        F::RDPID => gf::GF_RDPID,
        F::KL => gf::GF_KL,
        F::MOVDIRI => gf::GF_MOVDIRI,
        F::MOVDIR64B => gf::GF_MOVDIR64B,
        F::ENQCMD => gf::GF_ENQCMD,
        F::UINTR => gf::GF_UINTR,
        F::SERIALIZE => gf::GF_SERIALIZE,
        F::TSXLDTRK => gf::GF_TSXLDTRK,
        F::XSAVEOPT => gf::GF_XSAVEOPT,
        F::XSAVEC => gf::GF_XSAVEC,
        F::XSAVES => gf::GF_XSAVES,
        F::SSE4A => gf::GF_SSE4A,
        F::RDTSCP => gf::GF_RDTSCP,
        _ => return None,
    })
}

/// A CPUID feature bit of the presented CPU: (leaf, subleaf, register EAX..EDX = 0..3, bit): the catalogue's, generated
/// (`natemu_abi::cpu_gates`, from tools/models/cpu_profile.json), read from the generated profile (`cpu_profile::CPUID`).
type Feature = gf::Feature;
fn advertised((leaf, sub, reg, bit): Feature) -> bool {
    use natemu_abi::cpu_profile::{ANY, CPUID};
    CPUID
        .iter()
        .find(|&&(l, s, _)| l == leaf && (s == ANY || s == sub))
        .is_some_and(|(_, _, r)| r[reg] >> bit & 1 != 0)
}
/// SSE and SSE2 (with MMX) are part of every 64-bit processor: their cells are defined whatever the profile.
const BASE: Option<Feature> = None;
/// The legacy (non-VEX) cells of the SIMD rows of SDM Vol.2 Tables A-3 (two-byte), A-4 (0f 38) and A-5 (0f 3a): (map: 1,
/// 2 or 3; first and last opcode; the mandatory prefixes defined there: N none, 6 66, 3 F3, 2 F2; the feature defining
/// them). A cell of these rows not listed is blank.
const SIMD: &[(u8, u8, u8, &[u8], Option<Feature>)] = &[
    (1, 0x10, 0x11, b"N632", BASE),
    (1, 0x12, 0x12, b"N6", BASE),
    (1, 0x12, 0x12, b"32", Some(gf::GF_SSE3)),
    (1, 0x13, 0x15, b"N6", BASE),
    (1, 0x16, 0x16, b"N6", BASE),
    (1, 0x16, 0x16, b"3", Some(gf::GF_SSE3)),
    (1, 0x17, 0x17, b"N6", BASE),
    (1, 0x28, 0x29, b"N6", BASE),
    (1, 0x2a, 0x2a, b"N632", BASE),
    (1, 0x2b, 0x2b, b"N6", BASE),
    (1, 0x2c, 0x2d, b"N632", BASE),
    (1, 0x2e, 0x2f, b"N6", BASE),
    (1, 0x50, 0x50, b"N6", BASE),
    (1, 0x51, 0x51, b"N632", BASE),
    (1, 0x52, 0x53, b"N3", BASE),
    (1, 0x54, 0x57, b"N6", BASE),
    (1, 0x58, 0x5a, b"N632", BASE),
    (1, 0x5b, 0x5b, b"N63", BASE),
    (1, 0x5c, 0x5f, b"N632", BASE),
    (1, 0x60, 0x6b, b"N6", BASE),
    (1, 0x6c, 0x6d, b"6", BASE),
    (1, 0x6e, 0x6e, b"N6", BASE),
    (1, 0x6f, 0x6f, b"N63", BASE),
    (1, 0x70, 0x70, b"N632", BASE),
    (1, 0x71, 0x76, b"N6", BASE),
    (1, 0x77, 0x77, b"N", BASE),
    (1, 0x7c, 0x7d, b"62", Some(gf::GF_SSE3)),
    (1, 0x7e, 0x7f, b"N63", BASE),
    (1, 0xc2, 0xc2, b"N632", BASE),
    (1, 0xc4, 0xc6, b"N6", BASE),
    (1, 0xd0, 0xd0, b"62", Some(gf::GF_SSE3)),
    (1, 0xd1, 0xd5, b"N6", BASE),
    (1, 0xd6, 0xd6, b"632", BASE),
    (1, 0xd7, 0xe5, b"N6", BASE),
    (1, 0xe6, 0xe6, b"632", BASE),
    (1, 0xe7, 0xef, b"N6", BASE),
    (1, 0xf0, 0xf0, b"2", Some(gf::GF_SSE3)),
    (1, 0xf1, 0xfe, b"N6", BASE),
    (2, 0x00, 0x0b, b"N6", Some(gf::GF_SSSE3)),
    (2, 0x10, 0x10, b"6", Some(gf::GF_SSE4_1)),
    (2, 0x14, 0x15, b"6", Some(gf::GF_SSE4_1)),
    (2, 0x17, 0x17, b"6", Some(gf::GF_SSE4_1)),
    (2, 0x1c, 0x1e, b"N6", Some(gf::GF_SSSE3)),
    (2, 0x20, 0x25, b"6", Some(gf::GF_SSE4_1)),
    (2, 0x28, 0x2b, b"6", Some(gf::GF_SSE4_1)),
    (2, 0x30, 0x35, b"6", Some(gf::GF_SSE4_1)),
    (2, 0x37, 0x37, b"6", Some(gf::GF_SSE4_2)),
    (2, 0x38, 0x41, b"6", Some(gf::GF_SSE4_1)),
    (2, 0x80, 0x82, b"6", BASE),
    (2, 0xc8, 0xcd, b"N", Some(gf::GF_SHA)),
    (2, 0xcf, 0xcf, b"6", Some(gf::GF_GFNI)),
    (2, 0xd8, 0xd8, b"3", Some(gf::GF_KL)),
    (2, 0xdb, 0xdf, b"6", Some(gf::GF_AES)),
    (2, 0xdc, 0xdf, b"3", Some(gf::GF_KL)),
    (2, 0xf0, 0xf1, b"N6", Some(gf::GF_MOVBE)),
    (2, 0xf0, 0xf1, b"2", Some(gf::GF_SSE4_2)),
    (2, 0xf5, 0xf5, b"6", Some(gf::GF_CET_SS)),
    (2, 0xf6, 0xf6, b"63", Some(gf::GF_ADX)),
    (2, 0xf8, 0xf8, b"6", Some(gf::GF_MOVDIR64B)),
    (2, 0xf8, 0xf8, b"32", Some(gf::GF_ENQCMD)),
    (2, 0xf9, 0xf9, b"N", Some(gf::GF_MOVDIRI)),
    (2, 0xfa, 0xfb, b"3", Some(gf::GF_KL)),
    (3, 0x08, 0x0e, b"6", Some(gf::GF_SSE4_1)),
    (3, 0x0f, 0x0f, b"N6", Some(gf::GF_SSSE3)),
    (3, 0x14, 0x17, b"6", Some(gf::GF_SSE4_1)),
    (3, 0x20, 0x22, b"6", Some(gf::GF_SSE4_1)),
    (3, 0x40, 0x42, b"6", Some(gf::GF_SSE4_1)),
    (3, 0x44, 0x44, b"6", Some(gf::GF_PCLMULQDQ)),
    (3, 0x60, 0x63, b"6", Some(gf::GF_SSE4_2)),
    (3, 0xcc, 0xcc, b"N", Some(gf::GF_SHA)),
    (3, 0xce, 0xcf, b"6", Some(gf::GF_GFNI)),
    (3, 0xdf, 0xdf, b"6", Some(gf::GF_AES)),
];
/// Two-byte opcodes whose rows are SIMD rows (the mandatory prefix selects the instruction); elsewhere F2/F3 are REP
/// prefixes and 66 the operand size (SDM Vol.2 2.1.1: reserved, not #UD, on other instructions).
fn simd_row(op: u8) -> bool {
    matches!(op, 0x10..=0x17 | 0x28..=0x2f | 0x50..=0x77 | 0x7c..=0x7f | 0xc2 | 0xc4..=0xc6 | 0xd0..=0xfe)
}

/// X1c: an encoding fexc refuses (`unsupported_form`) that the SDM makes #UD whatever its operands:
/// - blank cells: Table A-2 `d6`; Table A-3 `0f 04 0a 0c 0e 24..27 36 39 3b 3c..3f 7a 7b`, JMPE `0f b8` without F3
///   ("reserved for emulator on IPF") and 66/F2 `0f b8`; group 9's `0f c7` register forms /0../5 and memory forms /0 /2;
///   and every mandatory-prefix cell of the SIMD rows of Tables A-3/A-4/A-5 that [`SIMD`] does not define, or defines by a
///   feature the profile does not advertise (CPUID's "#UD if CPUID.(...) = 0" of each page);
/// - "an operand type that is invalid for its accompanying opcode" (Vol.3 7.15 interrupt 6): a register operand of LEA
///   (`8d`), MOVNTI (`0f c3`), the memory-only SIMD forms (MOVLPS/MOVHPS/MOVLPD/MOVHPD stores `0f 13`/`0f 17` and 66 loads
///   `66 0f 12`/`66 0f 16`, MOVNTPS/PD `0f 2b`, MOVNTQ/MOVNTDQ `0f e7`, LDDQU `f2 0f f0`, MOVNTDQA `66 0f 38 2a`), CMPXCHG8B/
///   16B; a memory operand of the register-only ones (MOVMSKPS/PD `0f 50`, PMOVMSKB `0f d7`, MASKMOVQ/DQU `0f f7`, the
///   shift groups `0f 71..73`, PEXTRW `0f c5`, MOVQ2DQ/MOVDQ2Q `f3/f2 0f d6`);
/// - unadvertised instructions: GETSEC (`0f 37`, CR4.SMXE = 0), RSM outside SMM (`0f aa`), the VMX group-9 forms (`0f c7
///   /6`, `/7` m, any prefix: "#UD If not in VMX operation"); PTWRITE (`f3 0f ae /4`, Intel PT not advertised) is the
///   system decode's.
/// A cell with both 66 and F2/F3 is left alone (no claim on which prefix decides). fexc's own `cut_ud` covers what it
/// classifies; this covers what it refuses instead.
pub(crate) fn undefined(bytes: &[u8]) -> Option<System> {
    let mut at = 0;
    let (mut p66, mut rep) = (false, 0);
    loop {
        match *bytes.get(at)? {
            0x66 => p66 = true,
            p @ (0xf2 | 0xf3) => rep = p,
            0x67 | 0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 | 0x40..=0x4f => {}
            _ => break,
        }
        at += 1;
    }
    let modrm = |k: usize| bytes.get(at + k).map(|m| (m >> 6 == 3, m >> 3 & 7));
    let prefix = match (p66, rep) {
        (true, 0xf2 | 0xf3) => None,
        (_, 0xf3) => Some(b'3'),
        (_, 0xf2) => Some(b'2'),
        (true, _) => Some(b'6'),
        _ => Some(b'N'),
    };
    // A SIMD-row cell: blank or unadvertised (Some(true)), defined (Some(false)), not a SIMD row or both prefixes (None).
    let cell = |map: u8, op: u8| -> Option<bool> {
        let p = prefix?;
        let defined = SIMD.iter().any(|&(m, lo, hi, ps, f)| {
            m == map && (lo..=hi).contains(&op) && ps.contains(&p) && f.is_none_or(advertised)
        });
        Some(!defined)
    };
    let ud = match *bytes.get(at)? {
        0x8d => modrm(1)?.0,
        0xd6 => true,
        0x0f => match *bytes.get(at + 1)? {
            0x04
            | 0x0a
            | 0x0c
            | 0x0e
            | 0x24..=0x27
            | 0x36
            | 0x37
            | 0x39
            | 0x3b
            | 0x3c..=0x3f
            | 0x7a
            | 0x7b
            | 0xaa => true,
            0xb8 => rep != 0xf3,
            0xc3 => modrm(2)?.0,
            // PTWRITE r (F3 /4 register; the memory form is a system cut, [`decode`]): Intel PT not advertised.
            0xae => rep == 0xf3 && !p66 && modrm(2)? == (true, 4) && !advertised(gf::GF_PT),
            0xc7 => match modrm(2)? {
                (true, reg) => reg <= 5,
                (false, reg) => matches!(reg, 0 | 2 | 6 | 7),
            },
            op if simd_row(op) => {
                let (register, reg) = modrm(2)?;
                let p = prefix;
                cell(1, op)?
                    || match op {
                        0x13 | 0x17 | 0x2b | 0xe7 => register,
                        0x12 | 0x16 => register && p == Some(b'6'),
                        0xf0 => register,
                        0x50 | 0xd7 | 0xf7 | 0xc5 => !register,
                        0x71 | 0x72 => !register || !matches!(reg, 2 | 4 | 6),
                        0x73 => {
                            !register
                                || !(matches!(reg, 2 | 6)
                                    || (matches!(reg, 3 | 7) && p == Some(b'6')))
                        }
                        0xd6 => !register && p != Some(b'6'),
                        _ => false,
                    }
            }
            0x38 => {
                let op = *bytes.get(at + 2)?;
                cell(2, op)? || (op == 0x2a && modrm(3)?.0)
            }
            0x3a => cell(3, *bytes.get(at + 2)?)?,
            _ => false,
        },
        _ => false,
    };
    ud.then_some(System {
        kind: system::UD,
        length: 1,
        ..System::NONE
    })
}

/// X1c: a direct near branch whose target is not canonical, which fexc refuses (`unsupported_form`, its "direct branch
/// to a non-canonical target", or a prefix it does not compile): JMP rel8/rel32 `eb`/`e9`, CALL rel32 `e8`, Jcc rel8/rel32
/// `70..7f`/`0f 80..8f`, LOOPNE/LOOPE/LOOP/JrCXZ `e0..e3`. SDM Vol.2 JMP, CALL, Jcc, LOOP/LOOPcc, JrCXZ (64-bit mode):
/// #GP(0) if the target is non-canonical, raised only when the branch is taken; the resident evaluates the condition and
/// the count ([`system::BRANCH`]: `cr` the condition, `size` the count's width, `disp` the displacement). Prefixes a near
/// branch ignores in 64-bit mode are skipped, so the outcome is the unprefixed form's: segment overrides (2e/3e are Jcc
/// hints), 66 ("operand size prefixes are silently ignored ... the displacement field for relative branches is still
/// limited to 32 bits", Vol.1 3.3.7 / 64-bit near branches), REX (W included: the operand size is forced to 64 bits), F2
/// (BND, ignored without MPX) and F3; 0x67 sets the count's width of `e0..e3` and is ignored by the others.
pub(crate) fn branch(bytes: &[u8], rip: u64) -> Option<System> {
    let mut at = 0;
    let mut a32 = false;
    while let Some(
        &p @ (0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 | 0x66 | 0x67 | 0xf2 | 0xf3 | 0x40..=0x4f),
    ) = bytes.get(at)
    {
        a32 |= p == 0x67;
        at += 1;
    }
    let op = *bytes.get(at)?;
    let rel8 = || bytes.get(at + 1).map(|&b| i64::from(b as i8));
    let rel32 = |k: usize| {
        bytes
            .get(at + k..at + k + 4)
            .map(|b| i64::from(i32::from_le_bytes(b.try_into().unwrap())))
    };
    let (cr, disp, length) = match op {
        0x70..=0x7f => (u32::from(op & 15), rel8()?, 2),
        0xeb => (16, rel8()?, 2),
        0xe9 => (16, rel32(1)?, 5),
        0xe8 => (17, rel32(1)?, 5),
        0xe0..=0xe3 => (18 + u32::from(op - 0xe0), rel8()?, 2),
        0x0f if matches!(*bytes.get(at + 1)?, 0x80..=0x8f) => {
            (u32::from(bytes[at + 1] & 15), rel32(2)?, 6)
        }
        _ => return None,
    };
    let length = length + at as u64;
    if length > 15 {
        return None;
    }
    let target = rip.wrapping_add(length).wrapping_add(disp as u64);
    let canonical = (target as i64) << 16 >> 16 == target as i64;
    let size = if a32 && cr >= 18 { 4 } else { 8 };
    (!canonical).then_some(System {
        kind: system::BRANCH,
        cr,
        size,
        disp,
        length: length as u32,
        ..System::NONE
    })
}

/// [`decode`]'s record, `None` for a truncated instruction too.
#[cfg(test)]
fn dec(bytes: &[u8], bitness: u32) -> Option<System> {
    decode(bytes, bitness).ok().flatten()
}

#[cfg(test)]
mod x1c_tests {
    fn b(hex: &str) -> Vec<u8> {
        (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
            .collect()
    }
    #[test]
    fn undefined_forms() {
        for hex in [
            "8dc0",
            "d6",
            "0f04",
            "0f0e",
            "0f25",
            "0f37",
            "0f3f",
            "0f7b",
            "0faa",
            "0f13c0",
            "660f17c0",
            "0f2bc0",
            "660fe7c1",
            "f20ff0c0",
            "0fc3c0",
            "480fc7c8",
            "0fc7e8",
            "0fc700",
            "0fc710",
            "f30fc730",
            "0fc738",
            "660f382ac0",
            "0f3850",
            "0f3a00c000",
            // blank mandatory-prefix cells, JMPE, operand types (review)
            "0fd0c0",
            "0fd6c0",
            "0fe6c0",
            "0ff000",
            "0f7cc0",
            "f30f54c0",
            "f20f54c0",
            "f30f2b00",
            "f20f2b00",
            "f30f2bc0",
            "f20f5bc0",
            "0fb8c0",
            "660fb8c0",
            "f20fb8c0",
            "660f3850",
            "660f12c0",
            "0f5000",
            "0fd700",
            "f30fd600",
            "0f711000",
            "0f73d8",
            "f30f38f0c0",
            "660f3a0000",
        ] {
            assert!(
                super::undefined(&b(hex))
                    .is_some_and(|s| s.well_formed() && s.kind == super::system::UD),
                "{hex}"
            );
        }
        for hex in [
            "8d00",
            "0f1300",
            "0fc300",
            "f30fb8c0",
            "660f73d8",
            "f30fd6c0",
            "660f1200",
            "66f30f10c0",
            "0fc708",
            "0fc7f0",
            "0fc7f8",
            "0f38f0c0",
            "0f3800c0",
            "0f3a0fc000",
            "0f05",
            "0f0d08",
        ] {
            assert!(super::undefined(&b(hex)).is_none(), "{hex}");
        }
    }
    /// Every feature-gated cell of `SIMD` (and PTWRITE) is #UD exactly when tools/models/cpu_profile.json, the profile's
    /// source, says its CPUID bit is clear: a profile change cannot leave a false #UD behind (or a missed one).
    #[test]
    fn undefined_follows_the_profile() {
        use serde_json::Value;
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tools/models/cpu_profile.json"
        );
        let profile: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let Some(Value::Array(leaves)) = profile.get("cpuid") else {
            panic!("no cpuid table")
        };
        let state = |(leaf, sub, reg, bit): super::Feature| -> String {
            let entry = leaves.iter().find(|e| {
                let n = |k: &str| {
                    e.get(k)
                        .and_then(Value::as_str)
                        .map(|v| u32::from_str_radix(v.trim_start_matches("0x"), 16).unwrap())
                };
                let sub_of = e.get("subleaf").and_then(Value::as_u64);
                n("leaf") == Some(leaf) && sub_of.is_none_or(|s| s == u64::from(sub))
            });
            let field = entry.and_then(|e| {
                e.get("fields")?
                    .get(["eax", "ebx", "ecx", "edx"][reg])?
                    .get(&bit.to_string())
            });
            field
                .and_then(|f| f.get("state")?.as_str())
                .unwrap_or_else(|| {
                    panic!("CPUID {leaf:#x}.{sub} reg {reg} bit {bit} not in the profile")
                })
                .to_string()
        };
        let mut checked = 0;
        for &(map, lo, hi, prefixes, feature) in super::SIMD {
            let Some(feature) = feature else { continue };
            let on = match state(feature).as_str() {
                "advertised" => true,
                "clear" => false,
                s => panic!("feature {feature:?}: state {s}"),
            };
            assert_eq!(
                super::advertised(feature),
                on,
                "{feature:?}: generated profile and cpu_profile.json disagree"
            );
            for op in lo..=hi {
                for &p in prefixes {
                    let pre: &[u8] = match p {
                        b'6' => &[0x66],
                        b'3' => &[0xf3],
                        b'2' => &[0xf2],
                        _ => &[],
                    };
                    let esc: &[u8] = match map {
                        1 => &[0x0f],
                        2 => &[0x0f, 0x38],
                        _ => &[0x0f, 0x3a],
                    };
                    let bytes = [pre, esc, &[op, 0x00, 0x00]].concat();
                    // Another feature may define the same cell (e.g. 66 0f 38 dc: AES and KL with F3): judge by all of them.
                    let any = super::SIMD.iter().any(|&(m, l, h, ps, f)| {
                        m == map
                            && (l..=h).contains(&op)
                            && ps.contains(&p)
                            && f.is_none_or(|f| state(f) == "advertised")
                    });
                    assert_eq!(
                        super::undefined(&bytes).is_some(),
                        !any,
                        "{bytes:02x?} ({feature:?} {})",
                        if on { "advertised" } else { "clear" }
                    );
                    checked += 1;
                }
            }
        }
        assert!(checked > 100, "{checked}");
        // Every generated gate constant (`natemu_abi::cpu_gates`) is the catalogue's state, in the JSON and in the generated
        // profile: a gate cannot disagree with what the resident's CPUID answers.
        for (name, feature, on) in super::gf::GATES {
            assert_eq!(state(feature) == "advertised", on, "{name}");
            assert_eq!(super::advertised(feature), on, "{name}");
        }
    }
    /// The forms of the features the profile does not advertise are #UD exactly while the catalogue's bit is clear: the
    /// decode asks the generated gate, never a hand-written verdict. A form whose feature is advertised is not decoded here (a
    /// stop: nothing builds it), and RDTSCP, FSGSBASE and RDRAND decode only while advertised.
    #[test]
    fn unadvertised_forms_follow_the_catalogue() {
        use super::gf::*;
        use super::system;
        let kind = |hex: &str| super::dec(&b(hex), 64).map(|s| s.kind);
        let mut checked = 0;
        for (hex, features) in [
            ("0fc7f8", &[GF_RDSEED][..]),
            ("f30fc7f8", &[GF_RDPID]),
            ("0fc72b", &[GF_XSAVES]),
            ("0fc71b", &[GF_XSAVES]),
            ("660f388200", &[GF_INVPCID]),
            ("660f388000", &[GF_VMX]),
            ("660f388100", &[GF_VMX]),
            ("0f38f608", &[GF_CET_SS]),
            ("660f38f508", &[GF_CET_SS]),
            ("0f01e8", &[GF_SERIALIZE, GF_TSXLDTRK]),
            ("f20f01e8", &[GF_SERIALIZE, GF_TSXLDTRK]),
            ("f20f01e9", &[GF_TSXLDTRK]),
            ("0f01ee", &[GF_PKU]),
            ("0f01ef", &[GF_PKU]),
            ("0f01ca", &[GF_SMAP]),
            ("0f01cb", &[GF_SMAP]),
            ("0f01c8", &[GF_MONITOR]),
            ("0f01c9", &[GF_MONITOR]),
            ("0f01d5", &[GF_RTM]),
            ("0f01d6", &[GF_RTM, GF_HLE]),
            ("0f01c1", &[GF_VMX]),
            ("0f01cf", &[GF_SGX]),
            ("f30f01ea", &[GF_CET_SS]),
            ("f30f01ec", &[GF_UINTR]),
            ("f30f01ed", &[GF_UINTR]),
            ("f30f01ee", &[GF_PKU, GF_UINTR]),
            ("f30f01ef", &[GF_PKU, GF_UINTR]),
            ("f30f01e8", &[GF_SERIALIZE, GF_TSXLDTRK, GF_CET_SS]),
            ("f30f012b", &[GF_CET_SS]),
            ("f30fae30", &[GF_CET_SS]),
        ] {
            let on = features.iter().any(|&f| super::advertised(f));
            assert_eq!(kind(hex), if on { None } else { Some(system::UD) }, "{hex}");
            checked += 1;
        }
        // Decoded only while advertised, #UD otherwise.
        for (hex, on, want) in [
            ("0f01f9", GF_RDTSCP, system::RDTSCP),
            ("f3480faec0", GF_FSGSBASE, system::RDFSBASE),
            ("0fc7f0", GF_RDRAND, system::RDRAND),
        ] {
            let got = kind(hex);
            assert_eq!(
                got,
                Some(if super::advertised(on) {
                    want
                } else {
                    system::UD
                }),
                "{hex}"
            );
            checked += 1;
        }
        assert!(checked > 25);
    }
    #[test]
    fn reserved_nops() {
        use super::system::RESERVED_NOP;
        let d = |hex: &str| super::dec(&b(hex), 64).map(|s| (s.kind, s.length));
        for (hex, length) in [
            ("0f1820", 3),
            ("0f18c0", 3),
            ("f30f180400", 5),
            ("0f1900", 3),
            ("660f1a00", 4),
            ("f20f1b8000010000", 8),
            ("0f1cc8", 3),
            ("0f1d4408f0", 5),
            ("0f0d00", 3),
            ("640f0d3d00000000", 8),
            ("480f1d05ffffffff", 8),
        ] {
            assert_eq!(d(hex), Some((RESERVED_NOP, length)), "{hex}");
        }
        assert_eq!(d("0f0dc0"), None); // fexc's #UD
    }
    #[test]
    fn push_pop_fs_gs() {
        use super::system::{POP_SREG, PUSH_SREG};
        let d = |hex: &str| super::dec(&b(hex), 64).map(|s| (s.kind, s.cr, s.size, s.length));
        let (fs, gs) = (
            natemu_abi::execution::SREG_FS as u32,
            natemu_abi::execution::SREG_GS as u32,
        );
        assert_eq!(d("0fa0"), Some((PUSH_SREG, fs, 8, 2)));
        assert_eq!(d("660fa9"), Some((POP_SREG, gs, 2, 3)));
        assert_eq!(d("66480fa8"), Some((PUSH_SREG, gs, 8, 4)));
        assert_eq!(d("f30fa1"), None); // F3: not a form of it
    }
    #[test]
    fn noncanonical_branches() {
        use super::system::BRANCH;
        let s = |hex: &str, rip| {
            super::branch(&b(hex), rip).map(|s| (s.kind, s.cr, s.size, s.disp, s.length))
        };
        let top = 0x7fff_ffff_fff0;
        assert_eq!(s("eb20", top), Some((BRANCH, 16, 8, 0x20, 2)));
        assert_eq!(s("eb0c", top), None); // 0x7fff_ffff_fffe: canonical
        assert_eq!(s("e810000000", top), Some((BRANCH, 17, 8, 0x10, 5)));
        assert_eq!(s("0f8410000000", top), Some((BRANCH, 4, 8, 0x10, 6)));
        assert_eq!(
            s("75e0", 0xffff_8000_0000_0010),
            Some((BRANCH, 5, 8, -0x20, 2))
        );
        assert_eq!(s("e220", top), Some((BRANCH, 20, 8, 0x20, 2)));
        assert_eq!(s("67e320", top), Some((BRANCH, 21, 4, 0x20, 3)));
        assert_eq!(s("67eb20", top), Some((BRANCH, 16, 8, 0x20, 3))); // 0x67 ignored by JMP
        assert_eq!(s("3e7420", top), Some((BRANCH, 4, 8, 0x20, 3))); // ds (a hint) je
        assert_eq!(s("48e900100000", top), Some((BRANCH, 16, 8, 0x1000, 6))); // REX.W jmp
        assert_eq!(s("66e900100000", top), Some((BRANCH, 16, 8, 0x1000, 6))); // 66 ignored: rel32 still
        assert_eq!(s("f2e800100000", top), Some((BRANCH, 17, 8, 0x1000, 6))); // bnd call
        assert_eq!(s("2e0f8500100000", top), Some((BRANCH, 5, 8, 0x1000, 7)));
        assert_eq!(s("f0eb20", top), None); // LOCK: not a branch prefix (#UD elsewhere)
        assert_eq!(s("90", top), None);
        assert!(super::branch(&b("e810000000"), top).unwrap().well_formed());
    }
}

#[cfg(test)]
mod lock_tests {
    /// I8: a VEX prefix after a 66, F2 or F3 prefix, or right after a REX: #UD (SDM Vol.2 2.3.2), whatever the instruction.
    #[test]
    fn legacy_prefix_before_vex() {
        for bytes in [
            &[0x66, 0xc5, 0xf8, 0x77][..],
            &[0xf3, 0xc5, 0xfe, 0x6f, 0x07],
            &[0xf2, 0x64, 0xc4, 0xe2, 0x7d, 0x78, 0xff],
            &[0x48, 0xc5, 0xf8, 0x77],
        ] {
            assert!(
                super::dec(&[bytes, &[0x90; 4]].concat(), 64)
                    .is_some_and(|s| s.well_formed() && s.kind == super::system::UD),
                "{bytes:x?}"
            );
        }
    }
    /// I3: LOCK on an instruction that is not lockable is #UD (SDM Vol.2 "LOCK"), wherever it is among the prefixes.
    #[test]
    fn lock_prefix_anywhere_in_the_legacy_prefixes() {
        // R2 review: a REX before F0 is ignored, the F0 is still a prefix (SDM Vol.2 2.2.1).
        for bytes in [
            &[0xf0, 0x89, 0x03][..],
            &[0x66, 0xf0, 0x01, 0xd8],
            &[0xf3, 0xf0, 0x0f, 0x1e, 0xfa],
            &[0x64, 0xf0, 0xf4],
            &[0x48, 0xf0, 0x89, 0x03],
            &[0x48, 0xf0, 0x0f, 0x22, 0xd8],
        ] {
            let s = super::dec(&[bytes, &[0x90; 4]].concat(), 64).unwrap();
            assert!(s.well_formed() && s.kind == super::system::UD, "{bytes:x?}");
        }
        // No LOCK, or an f0 byte after the opcode (a ModRM or immediate) is no prefix: no system instruction.
        for bytes in [&[0x89, 0x03][..], &[0x48, 0x89, 0xf0], &[0xb0, 0xf0], &[]] {
            assert!(
                super::dec(&[bytes, &[0x90; 4]].concat(), 64).is_none(),
                "{bytes:x?}"
            );
        }
    }
}

/// Perf chunk 12: the number of instructions in `bytes` when it is wholly a run of no-operation instructions
/// ([`system::NOP_RUN`]): NOP (`90`, with 66 prefixes), NOP r/m (`0f 1f /0`, register or memory: never accessed, SDM Vol.2
/// NOP) after 66, CS/DS/ES/SS (ignored in 64-bit mode) and REX prefixes, and ENDBR64/ENDBR32 (`f3 0f 1e fa`/`fb`; the
/// profile has no CET, so they execute as NOPs, SDM Vol.1 18.3.3), each at most 15 bytes. Anything else (LOCK, F2/F3,
/// 0x67, FS/GS, PAUSE, `41 90` = XCHG R8, EAX, a ModRM.reg other than 0) is `None`. 64-bit mode only (REX bytes are REX:
/// the resident runs no other mode, `Cpu::code_mode`).
pub(crate) fn nops(bytes: &[u8]) -> Option<u32> {
    let (mut at, mut n) = (0, 0);
    while at < bytes.len() {
        let b = &bytes[at..];
        let length = if b.starts_with(&[0xf3, 0x0f, 0x1e, 0xfa])
            || b.starts_with(&[0xf3, 0x0f, 0x1e, 0xfb])
        {
            4
        } else {
            let mut k = b.iter().take_while(|&&p| p == 0x66).count();
            if b.get(k) == Some(&0x90) {
                k + 1
            } else {
                k += b[k..]
                    .iter()
                    .take_while(|&&p| matches!(p, 0x66 | 0x2e | 0x3e | 0x26 | 0x36))
                    .count();
                let rex = match b.get(k) {
                    Some(&r @ 0x40..=0x4f) => {
                        k += 1;
                        r
                    }
                    _ => 0,
                };
                if b.get(k..k + 2)? != [0x0f, 0x1f] {
                    return None;
                }
                k += 2;
                if rm(b, &mut k, rex, &mut System { ..System::NONE })? != 0 {
                    return None;
                }
                k
            }
        };
        if length > 15 {
            return None;
        }
        (at, n) = (at + length, n + 1);
    }
    Some(n)
}

/// Decodes the ModRM (with SIB and displacement) at `at` into `s`: a register r/m (`s.reg`, `s.memory` = 0) or a memory
/// operand (`s.base`, `s.index`, `s.scale`, `s.disp`, `s.memory` = 1). Returns ModRM.reg (without REX.R).
fn rm(bytes: &[u8], at: &mut usize, rex: u8, s: &mut System) -> Option<u32> {
    let (x, b) = (u32::from(rex >> 1 & 1) << 3, u32::from(rex & 1) << 3);
    let modrm = *bytes.get(*at)?;
    *at += 1;
    let (mode, rm) = (modrm >> 6, u32::from(modrm & 7));
    if mode == 3 {
        s.reg = rm | b;
        return Some(u32::from(modrm >> 3 & 7));
    }
    s.memory = 1;
    let disp_bytes = match (mode, rm) {
        (0, 5) => {
            s.base = system::RIP;
            4
        }
        (_, 4) => {
            let sib = *bytes.get(*at)?;
            *at += 1;
            let index = u32::from(sib >> 3 & 7) | x;
            s.index = if index == 4 { system::NO_REG } else { index };
            s.scale = u32::from(sib >> 6);
            let base = u32::from(sib & 7);
            s.base = if base == 5 && mode == 0 {
                system::NO_REG
            } else {
                base | b
            };
            if base == 5 && mode == 0 {
                4
            } else {
                [0, 1, 4][mode as usize]
            }
        }
        _ => {
            s.base = rm | b;
            [0, 1, 4][mode as usize]
        }
    };
    let disp = bytes.get(*at..*at + disp_bytes)?;
    s.disp = match disp_bytes {
        1 => i64::from(disp[0] as i8),
        4 => i64::from(i32::from_le_bytes(disp.try_into().unwrap())),
        _ => 0,
    };
    *at += disp_bytes;
    Some(u32::from(modrm >> 3 & 7))
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::execution::{SREG_FS, SREG_GS};
    use system::*;

    /// No record the resident executes: #UD at the instruction (no instruction there, or `None`: fexc's reason stands,
    /// delivered as #UD, `Code::refused`).
    fn refused(s: Option<System>) -> bool {
        s.is_none_or(|s| s.kind == UD)
    }

    /// LGDT/LIDT/SGDT/SIDT: the operand size per mode (8 in 64-bit mode whatever 66 says; 4, or 2 with 66, in 32-bit
    /// code; 2, or 4 with 66, in 16-bit code).
    #[test]
    fn descriptor_table_sizes() {
        let size = |bits, b: &[u8]| dec(&[b, &[0x90; 4]].concat(), bits).map(|s| (s.kind, s.size));
        for (bits, plain, o16) in [(64, 8, 8), (32, 4, 2), (16, 2, 4)] {
            for (modrm, kind) in [(0x10, LGDT), (0x18, LIDT), (0x00, SGDT), (0x08, SIDT)] {
                assert_eq!(
                    size(bits, &[0x0f, 0x01, modrm]),
                    Some((kind, plain)),
                    "{bits} {modrm:x}"
                );
                assert_eq!(
                    size(bits, &[0x66, 0x0f, 0x01, modrm]),
                    Some((kind, o16)),
                    "{bits} 66 {modrm:x}"
                );
            }
        }
    }

    /// fexc's opcode map and opcode must be the bytes' (the prefixes, REX in 64-bit mode only, skipped).
    #[test]
    fn cut_matches_the_bytes() {
        let h = |hex: &str| -> Vec<u8> {
            (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect()
        };
        for (hex, bits, cut, ok) in [
            ("0f22d8", 64, (1, 0x22), true),
            ("0f22d8", 64, (1, 0x20), false),
            ("0f22d8", 64, (6, 0x22), false),
            ("0f22d8", 64, (0, 0x0f), false),
            ("f3480faec0", 64, (2, 0xae), true),
            ("660f388200", 64, (5, 0x82), true),
            ("660f388200", 64, (1, 0x38), false),
            ("48cf", 64, (0, 0xcf), true),
            ("48cf", 32, (0, 0xcf), false), // 48 is DEC EAX outside 64-bit mode
            ("48c5f877", 64, (8, 0x77), true),
            ("c4e26d90048f", 64, (8, 0x90), true),
            ("c51e", 32, (0, 0xc5), true), // LDS ebx, [esi]: mod 00
            ("c51e", 32, (8, 0x1e), false),
            ("c5f877", 32, (8, 0x77), true), // mod 11: VEX
            ("c5f877", 32, (0, 0xc5), false),
        ] {
            assert_eq!(cut_matches(&h(hex), bits, cut), ok, "{hex} {bits} {cut:x?}");
        }
    }

    /// An instruction iced decodes from the padding, or no instruction it found only by reading the padding, waits for the
    /// next page: neither a record nor #UD.
    #[test]
    fn truncated_instructions_wait_for_the_next_page() {
        for b in [
            &[0x0f, 0x01][..],
            &[0xcd],
            &[0xf0, 0x0f],
            &[0x0f, 0x01, 0x3c],
            &[0xc4, 0xe2],
        ] {
            assert_eq!(decode(b, 64), Err(Truncated), "{b:x?}");
        }
        assert_eq!(
            decode(&[0xcd, 0x80], 64).map(|s| s.map(|s| s.kind)),
            Ok(Some(INT))
        );
    }

    /// The 32- and 16-bit decode (nothing runs it yet): operand and address sizes, the segment an override names (not
    /// ignored outside 64-bit mode) and 16-bit address forms, the segment loads only those modes have.
    #[test]
    fn other_modes() {
        let at = |bits, hex: &str| {
            let bytes: Vec<u8> = (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect();
            dec(&[bytes, vec![0x90; 4]].concat(), bits)
        };
        let ds = SREG_DS as u32;
        let m32 = |kind, base, disp, length| System {
            prefixes: ADDRESS32,
            segment: ds,
            ..mem(kind, base, disp, length)
        };
        let k = |kind, size, cr, length| {
            Some(System {
                kind,
                size,
                cr,
                length,
                ..System::NONE
            })
        };
        // mov ds, [ebx+4]; mov ds, ss:[eax]; call far [0x1000]; lgdt [eax]
        assert_eq!(
            at(32, "8e5b04"),
            Some(System {
                cr: 3,
                size: 4,
                ..m32(MOV_TO_SREG, 3, 4, 3)
            })
        );
        assert_eq!(at(32, "368e18").map(|s| s.segment), Some(SREG_SS as u32));
        assert_eq!(
            at(32, "ff1d00100000"),
            Some(System {
                size: 4,
                ..m32(CALL_FAR, NO_REG, 0x1000, 6)
            })
        );
        assert_eq!(
            at(32, "0f0110"),
            Some(System {
                size: 4,
                ..m32(LGDT, 0, 0, 3)
            })
        );
        // 16-bit address forms: mov ds, [bx+si]; mov ds, [bp-8] (SS by default)
        assert_eq!(
            at(32, "678e18"),
            Some(System {
                cr: 3,
                size: 4,
                index: 6,
                prefixes: ADDRESS16,
                ..m32(MOV_TO_SREG, 3, 0, 3)
            })
        );
        assert_eq!(
            at(32, "678e5ef8"),
            Some(System {
                cr: 3,
                size: 4,
                prefixes: ADDRESS16,
                segment: SREG_SS as u32,
                ..m32(MOV_TO_SREG, 5, -8, 4)
            })
        );
        // POP DS, POP ES (66), PUSH CS, PUSH SS; IRET, RET far, POPF, ENTER at 32 bits; INTO; MOV CR3, EAX
        assert_eq!(at(32, "1f"), k(POP_SREG, 4, SREG_DS as u32, 1));
        assert_eq!(at(32, "6607"), k(POP_SREG, 2, SREG_ES as u32, 2));
        assert_eq!(at(32, "0e"), k(PUSH_SREG, 4, 1, 1));
        assert_eq!(at(32, "16"), k(PUSH_SREG, 4, SREG_SS as u32, 1));
        assert_eq!(at(32, "cf"), k(IRET, 4, 0, 1));
        assert_eq!(at(32, "cb"), k(RET_FAR, 4, 0, 1));
        assert_eq!(at(32, "9d"), k(POPF, 4, 0, 1));
        assert_eq!(
            at(32, "c8100002").map(|s| (s.kind, s.size, s.cr, s.disp)),
            Some((ENTER, 4, 2, 16))
        );
        assert_eq!(at(32, "ce"), k(INTO, 0, 0, 1));
        assert_eq!(at(32, "0f22d8"), Some(mov(MOV_TO_CR, 3, 0, 3, 0)));
        // M2 task 3b: lds esi, [ebx+4]; les ax, [eax] (66); lgs edi, [ebx] (64-bit mode: refused); arpl [eax], dx;
        // arpl cx, bx; bound eax, [ebx]; pusha (66), popad; aam 10; jmp 0x23:0x1234, call 0x1b:0x5678 (66)
        assert_eq!(
            at(32, "c57304"),
            Some(System {
                cr: SREG_DS as u32,
                reg: 6,
                size: 4,
                ..m32(LOAD_FAR, 3, 4, 3)
            })
        );
        assert_eq!(
            at(32, "66c400").map(|s| (s.kind, s.cr, s.reg, s.size)),
            Some((LOAD_FAR, SREG_ES as u32, 0, 2))
        );
        assert_eq!(
            at(32, "0fb53b").map(|s| (s.kind, s.cr, s.reg)),
            Some((LOAD_FAR, SREG_GS as u32, 7))
        );
        assert_eq!(at(64, "0fb53b"), None);
        assert_eq!(
            at(32, "6310"),
            Some(System {
                cr: 2,
                size: 2,
                ..m32(ARPL, 0, 0, 2)
            })
        );
        assert_eq!(
            at(32, "63d9").map(|s| (s.kind, s.memory, s.reg, s.cr)),
            Some((ARPL, 0, 1, 3))
        );
        assert_eq!(
            at(32, "6203"),
            Some(System {
                size: 4,
                ..m32(BOUND, 3, 0, 2)
            })
        );
        assert_eq!(at(32, "6660"), k(PUSHA, 2, 0, 2));
        assert_eq!(at(32, "61"), k(POPA, 4, 0, 1));
        assert_eq!(at(32, "d40a"), k(AAM, 0, 10, 2));
        assert_eq!(
            at(32, "ea341200002300").map(|s| (s.kind, s.memory, s.size, s.cr, s.disp, s.length)),
            Some((JMP_FAR, 0, 4, 0x23, 0x1234, 7))
        );
        assert_eq!(
            at(32, "669a78561b00").map(|s| (s.kind, s.memory, s.size, s.cr, s.disp, s.length)),
            Some((CALL_FAR, 0, 2, 0x1b, 0x5678, 6))
        );
        // INC EAX is no prefix and no system instruction; SWAPGS is 64-bit only.
        assert_eq!(at(32, "40"), None);
        assert_eq!(at(32, "0f01f8").map(|s| s.kind), Some(UD));
        // 16-bit code: IRET (66: 32 bits), mov ds, [0x1234]
        assert_eq!(at(16, "cf"), k(IRET, 2, 0, 1));
        assert_eq!(at(16, "66cf"), k(IRET, 4, 0, 2));
        assert_eq!(
            at(16, "8e1e3412"),
            Some(System {
                cr: 3,
                size: 2,
                prefixes: ADDRESS16,
                segment: ds,
                ..mem(MOV_TO_SREG, NO_REG, 0x1234, 4)
            })
        );
    }

    /// Perf chunk 12: runs of no-operation instructions, counted; anything else refused.
    #[test]
    fn nop_runs() {
        let h = |hex: &str| -> Vec<u8> {
            (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect()
        };
        for (hex, n) in [
            ("f30f1efa", 1),
            ("90909090909090", 7),
            ("6690", 1),
            ("0f1f00", 1),
            ("0f1f4000", 1),
            ("0f1f440000", 1),
            ("660f1f440000", 1),
            ("0f1f8000000000", 1),
            ("0f1f840000000000", 1),
            ("662e0f1f840000000000", 1),
            ("66662e0f1f840000000000", 1),
            ("480f1fc0", 1),
            ("f30f1efb900f1f00", 3),
            ("0f1f0525000000", 1),
        ] {
            assert_eq!(nops(&h(hex)), Some(n), "{hex}");
        }
        for hex in [
            "4190",
            "f390",
            "f00f1f00",
            "0f1f08",
            "670f1f00",
            "640f1f00",
            "f20f1f00",
            "0f1f",
            "0f1f44",
            "90cc",
            "f30f1efc",
            "6666666666666666666666662e0f1f840000000000",
            "0f18c0",
        ] {
            assert_eq!(nops(&h(hex)), None, "{hex}");
        }
    }

    /// R2 review: REX bytes among the prefixes, the last before the opcode in force (SDM Vol.2 2.2.1).
    #[test]
    fn rex_chains() {
        assert_eq!(opcode_at(&[0x4f, 0x47, 0x41, 0x4d, 0x53]), (4, 0x4d)); // CRYPTOGAMS: push r11
        assert_eq!(opcode_at(&[0x48, 0x66, 0x89, 0xc0]), (2, 0)); // the REX before 66 is ignored
        assert_eq!(opcode_at(&[0x66, 0x48]), (2, 0x48));
        // mov cr3, r8 with a W/X chain before its REX.B; mov rax, cr0 with a REX.B before a 66 (ignored: rax, not r8)
        let crs = |s: Option<System>| s.map(|s| (s.kind, s.cr, s.reg, s.length, s.lock));
        assert_eq!(
            crs(d("49410f22d8")),
            crs(d("410f22d8")).map(|(k, c, r, l, x)| (k, c, r, l + 1, x))
        );
        assert_eq!(
            crs(d("41660f20c0")),
            crs(d("660f20c0")).map(|(k, c, r, l, x)| (k, c, r, l + 1, x))
        );
    }

    fn d(hex: &str) -> Option<System> {
        let bytes: Vec<u8> = (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
            .collect();
        dec(&[bytes, vec![0x90; 4]].concat(), 64)
    }
    fn mov(kind: u32, cr: u32, reg: u32, length: u32, lock: u32) -> System {
        System {
            kind,
            cr,
            reg,
            length,
            lock,
            ..System::NONE
        }
    }
    fn invlpg(base: u32, index: u32, scale: u32, disp: i64, length: u32) -> System {
        System {
            kind: INVLPG,
            base,
            index,
            scale,
            disp,
            length,
            memory: 1,
            segment: if matches!(base, 4 | 5) {
                SREG_SS
            } else {
                SREG_DS
            } as u32,
            ..System::NONE
        }
    }
    fn p(hex: &str) -> Option<System> {
        let bytes: Vec<u8> = (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
            .collect();
        dec(&[bytes, vec![0x90; 4]].concat(), 64)
    }
    /// A memory operand in its default segment (SS for an RSP or RBP base, else DS); the descriptor-table forms at their
    /// 64-bit operand size.
    fn mem(kind: u32, base: u32, disp: i64, length: u32) -> System {
        System {
            kind,
            base,
            disp,
            length,
            memory: 1,
            size: if (LGDT..=SIDT).contains(&kind) { 8 } else { 0 },
            segment: if matches!(base, 4 | 5) {
                SREG_SS
            } else {
                SREG_DS
            } as u32,
            ..System::NONE
        }
    }

    #[test]
    fn descriptor_and_segment_forms() {
        assert_eq!(d("0f0110"), Some(mem(LGDT, 0, 0, 3))); // lgdt [rax]
        assert_eq!(d("0f011c24"), Some(mem(LIDT, 4, 0, 4))); // lidt [rsp]
        assert_eq!(d("0f010580000000"), Some(mem(SGDT, RIP, 0x80, 7))); // sgdt [rip+0x80]
        assert_eq!(
            d("650f0108"),
            Some(System {
                segment: 5,
                ..mem(SIDT, 0, 0, 4)
            })
        ); // sidt gs:[rax]
        assert_eq!(
            d("0f00d8"),
            Some(System {
                kind: LTR,
                reg: 0,
                length: 3,
                size: 4,
                ..System::NONE
            })
        ); // ltr ax
        assert_eq!(
            d("410f00d1"),
            Some(System {
                kind: LLDT,
                reg: 9,
                length: 4,
                size: 4,
                ..System::NONE
            })
        ); // lldt r9w
        assert_eq!(
            d("480f00c8"),
            Some(System {
                kind: STR,
                reg: 0,
                length: 4,
                size: 8,
                ..System::NONE
            })
        ); // str rax
        assert_eq!(
            d("660f00c3"),
            Some(System {
                kind: SLDT,
                reg: 3,
                length: 4,
                size: 2,
                ..System::NONE
            })
        ); // sldt bx
        assert_eq!(
            d("0f0003"),
            Some(System {
                size: 4,
                ..mem(SLDT, 3, 0, 3)
            })
        ); // sldt [rbx]
        assert_eq!(
            p("8ed8"),
            Some(System {
                kind: MOV_TO_SREG,
                cr: 3,
                reg: 0,
                length: 2,
                size: 4,
                ..System::NONE
            })
        ); // mov ds, eax
        assert_eq!(
            p("8e5608"),
            Some(System {
                cr: 2,
                size: 4,
                ..mem(MOV_TO_SREG, 6, 8, 3)
            })
        ); // mov ss, [rsi+8]
        assert_eq!(
            p("668cc8"),
            Some(System {
                kind: MOV_FROM_SREG,
                cr: 1,
                reg: 0,
                length: 3,
                size: 2,
                ..System::NONE
            })
        ); // mov ax, cs
        assert_eq!(
            p("498ce1"),
            Some(System {
                kind: MOV_FROM_SREG,
                cr: 4,
                reg: 9,
                length: 3,
                size: 8,
                ..System::NONE
            })
        ); // mov r9, fs
        assert_eq!(
            p("48ff28"),
            Some(System {
                size: 8,
                ..mem(JMP_FAR, 0, 0, 3)
            })
        ); // jmp far [rax] (m16:64)
        assert_eq!(
            p("ff5b10"),
            Some(System {
                size: 4,
                ..mem(CALL_FAR, 3, 0x10, 3)
            })
        ); // call far [rbx+0x10] (m16:32)
        assert_eq!(
            p("48cb"),
            Some(System {
                kind: RET_FAR,
                size: 8,
                length: 2,
                ..System::NONE
            })
        ); // lretq
        assert_eq!(
            p("ca1000"),
            Some(System {
                kind: RET_FAR,
                size: 4,
                disp: 0x10,
                length: 3,
                ..System::NONE
            })
        ); // lret 0x10
        assert_eq!(
            p("48cf"),
            Some(System {
                kind: IRET,
                size: 8,
                length: 2,
                ..System::NONE
            })
        ); // iretq
        assert_eq!(
            p("f048cf"),
            Some(System {
                kind: UD,
                length: 3,
                ..System::NONE
            })
        ); // LOCK: #UD
        assert_eq!(p("2e8e18").map(|s| s.segment), Some(SREG_DS as u32)); // mov ds, cs:[rax]: CS ignored
        assert_eq!(p("368e5d08").map(|s| s.segment), Some(SREG_SS as u32)); // mov ds, ss:[rbp+8]: SS ignored (RBP: SS by default)
        // I10: 0x67 on a memory operand (mov ds, [eax]; lgdt [eip+0x80]).
        assert_eq!(
            p("678e18"),
            Some(System {
                cr: 3,
                size: 4,
                prefixes: ADDRESS32,
                ..mem(MOV_TO_SREG, 0, 0, 3)
            })
        );
        assert_eq!(
            d("670f011580000000"),
            Some(System {
                prefixes: ADDRESS32,
                ..mem(LGDT, RIP, 0x80, 8)
            })
        );
        assert_eq!(p("362e8e18").map(|s| s.segment), Some(SREG_DS as u32)); // two null prefixes: ignored
        assert_eq!(
            p("66cb"),
            Some(System {
                kind: RET_FAR,
                size: 2,
                length: 2,
                ..System::NONE
            })
        );
        assert_eq!(
            p("66cf"),
            Some(System {
                kind: IRET,
                size: 2,
                length: 2,
                ..System::NONE
            })
        );
        assert_eq!(
            p("66ff28"),
            Some(System {
                size: 2,
                ..mem(JMP_FAR, 0, 0, 3)
            })
        );
        for hex in ["ff20"] {
            assert!(refused(p(hex)), "{hex}");
        }
        for hex in ["ffe8", "ffd8", "66ffe8"] {
            assert_eq!(p(hex).map(|s| s.kind), Some(UD), "{hex}"); // X1c: far JMP/CALL with a register operand
        }
        assert_eq!(d("f30f00e0"), None); // f3 verr (X1c: 0f 00 /7 decodes, `x1c_forms`)
    }

    #[test]
    fn enter_width_and_nesting() {
        for (hex, size, length) in [
            ("c8ffff21", 8, 4),
            ("66c8ffff21", 2, 5),
            ("6648c8ffff21", 8, 6),
            ("4866c8ffff21", 2, 6),
        ] {
            let i = p(hex).unwrap();
            assert_eq!(
                (i.kind, i.size, i.cr, i.disp, i.length),
                (ENTER, size, 1, 65535, length)
            );
            assert!(i.well_formed());
        }
    }

    #[test]
    fn half_store_decode() {
        for (hex, reg, size, length) in [
            ("c4e3791d0703", 0, 8, 6),
            ("c4637d1d0f04", 9, 16, 6),
            ("c4e3791d0501000000ff", 0, 8, 10),
        ] {
            let bytes: Vec<u8> = (0..hex.len())
                .step_by(2)
                .map(|k| u8::from_str_radix(&hex[k..k + 2], 16).unwrap())
                .collect();
            let i = dec(&bytes, 64).unwrap();
            assert_eq!(
                (i.kind, i.reg, i.size, i.length),
                (CVTPS2PH, reg, size, length)
            );
            assert!(i.well_formed());
        }
        for bytes in [
            [0xc4, 0xe3, 0xf9, 0x1d, 7, 0],
            [0xc4, 0xe3, 0x71, 0x1d, 7, 0],
        ] {
            assert!(refused(dec(&bytes, 64)));
        }
    }

    /// The AVX2 gathers (VEX.66.0F38 90..93, VSIB): destination ModRM.reg with VEX.R, mask VEX.vvvv, vector index SIB.index
    /// with VEX.X (100b included), VEX.L and the index width in `cr`, the element width from VEX.W.
    #[test]
    fn gathers() {
        let v = |hex: &str| {
            let bytes: Vec<u8> = (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect();
            dec(&[bytes, vec![0x90; 4]].concat(), 64)
        };
        let g = |reg, cr, index, base, scale, disp, size, length| {
            Some(System {
                kind: GATHER,
                reg,
                cr,
                index,
                base,
                scale,
                disp,
                size,
                length,
                memory: 1,
                segment: if matches!(base, 4 | 5) {
                    SREG_SS
                } else {
                    SREG_DS
                } as u32,
                ..System::NONE
            })
        };
        assert_eq!(v("c4e26d90048f"), g(0, 2 | 16, 1, 7, 2, 0, 4, 6)); // vpgatherdd ymm0, [rdi+ymm1*4], ymm2
        assert_eq!(v("c4e2e99104c8"), g(0, 2 | 32, 1, 0, 3, 0, 8, 6)); // vpgatherqq xmm0, [rax+xmm1*8], xmm2
        assert_eq!(v("c4e2e9934c2010"), g(1, 2 | 32, 4, 0, 0, 0x10, 8, 7)); // vgatherqpd xmm1, [rax+xmm4+0x10], xmm2
        assert_eq!(
            v("c442e592040d78563412"),
            g(8, 3 | 16, 1, NO_REG, 0, 0x12345678, 8, 10)
        ); // vgatherdpd ymm8, [ymm1+disp32], ymm3
        assert_eq!(v("c4a26593443d00"), g(0, 3 | 16 | 32, 15, 5, 0, 0, 4, 7)); // vgatherqps xmm0, [rbp+ymm15], xmm3
        assert_eq!(v("67c4e26d90048f").map(|s| s.prefixes), Some(ADDRESS32));
        assert_eq!(v("64c4e26d90048f").map(|s| s.segment), Some(SREG_FS as u32));
        // A register operand or no SIB byte: fexc's #UD, never decoded; the 0F map's 90 is no gather; F2/66 before VEX: `vex_ud`.
        for hex in [
            "c4e26d90c1",
            "c4e26d9000",
            "c4e16d90048f",
            "66c4e26d90048f",
            "c4e26c90048f",
            "c5f8900c90",
            "c4e178900c90",
        ] {
            assert!(refused(v(hex)), "{hex}");
        }
        // Overlapping registers are refused by the resident's check (fexc cut them as #UD first).
        assert!(refused(v("c4e27590048f"))); // mask = index = ymm1
    }

    #[test]
    fn control_register_moves() {
        assert_eq!(d("0f22d8"), Some(mov(MOV_TO_CR, 3, 0, 3, 0))); // mov cr3, rax
        assert_eq!(d("0f20e1"), Some(mov(MOV_FROM_CR, 4, 1, 3, 0))); // mov rcx, cr4
        assert_eq!(d("440f22c7"), Some(mov(MOV_TO_CR, 8, 7, 4, 0))); // mov cr8, rdi (REX.R)
        assert_eq!(d("410f2003"), Some(mov(MOV_FROM_CR, 0, 11, 4, 0))); // mov r11, cr0 (mod 0 ignored, REX.B)
        assert_eq!(d("f00f22e0"), Some(mov(UD, 0, 0, 4, 0))); // lock mov cr4, rax: #UD (Intel: no LOCK MOV CR8 alias)
        assert_eq!(d("660f22c0"), Some(mov(MOV_TO_CR, 0, 0, 4, 0))); // 66 ignored
        assert_eq!(
            d("0f30"),
            Some(System {
                kind: WRMSR,
                length: 2,
                ..System::NONE
            })
        );
        assert_eq!(
            d("480f32"),
            Some(System {
                kind: RDMSR,
                length: 3,
                ..System::NONE
            })
        );
        assert_eq!(
            d("0fa2"),
            Some(System {
                kind: CPUID,
                length: 2,
                ..System::NONE
            })
        );
        assert_eq!(
            d("f00fa2"),
            Some(System {
                kind: UD,
                length: 3,
                ..System::NONE
            })
        ); // LOCK: #UD
        assert_eq!(
            d("480f31"),
            Some(System {
                kind: RDTSC,
                length: 3,
                ..System::NONE
            })
        );
        assert_eq!(
            d("670f01f9"),
            Some(System {
                kind: RDTSCP,
                length: 4,
                ..System::NONE
            })
        ); // 67 ignored
        assert_eq!(d("0f21c0"), Some(mov(MOV_FROM_DR, 0, 0, 3, 0))); // mov rax, dr0
        assert_eq!(d("0f23f9"), Some(mov(MOV_TO_DR, 7, 1, 3, 0))); // mov dr7, rcx
        assert_eq!(d("450f21e3"), Some(mov(UD, 0, 0, 4, 0))); // mov r11, dr12: #UD (REX.R)
        assert_eq!(d("f00f2300"), Some(mov(UD, 0, 0, 4, 0))); // lock mov dr0, rax: #UD
        assert_eq!(d("0f2300"), Some(mov(MOV_TO_DR, 0, 0, 3, 0))); // mov dr0, rax (mod 0 ignored)
        assert_eq!(
            d("0f33"),
            Some(System {
                kind: RDPMC,
                length: 2,
                ..System::NONE
            })
        );
    }

    #[test]
    fn software_interrupts_and_ud() {
        let s = |kind, cr, length| {
            Some(System {
                kind,
                cr,
                length,
                ..System::NONE
            })
        };
        assert_eq!(super::dec(&[0xcc], 64), s(INT3, 0, 1));
        assert_eq!(super::dec(&[0xf1], 64), s(INT1, 0, 1));
        assert_eq!(super::dec(&[0xcd, 0x80], 64), s(INT, 0x80, 2));
        assert_eq!(super::dec(&[0xcd], 64), None); // truncated immediate
        assert_eq!(super::dec(&[0xce, 0x90], 64).map(|s| s.kind), Some(UD)); // INTO: no 64-bit form
        assert_eq!(super::dec(&[0xce], 64), s(UD, 0, 1)); // the last byte supplied: no wait for the next one
        assert_eq!(super::dec(&[0x66, 0x41, 0x06], 64), s(UD, 0, 3)); // PUSH ES, prefixed
        assert_eq!(super::dec(&[0x8d], 64), None); // LEA: its ModRM decides (truncated)
        assert_eq!(super::dec(&[0x0f, 0x0b], 64), s(UD, 0, 2));
        assert_eq!(super::dec(&[0x0f, 0xb9, 0xc0], 64), s(UD, 0, 3));
        assert_eq!(
            super::dec(&[0xf0, 0xcc, 0x90, 0x90, 0x90, 0x90], 64).map(|s| (s.kind, s.lock)),
            Some((UD, 0))
        ); // #UD by LOCK
    }

    #[test]
    fn interrupt_flag_forms() {
        let s = |kind, size, length| {
            Some(System {
                kind,
                size,
                length,
                ..System::NONE
            })
        };
        assert_eq!(super::dec(&[0xfa], 64), s(CLI, 0, 1));
        assert_eq!(super::dec(&[0xfb], 64), s(STI, 0, 1));
        assert_eq!(super::dec(&[0xf4], 64), s(HLT, 0, 1));
        assert_eq!(super::dec(&[0x9d], 64), s(POPF, 8, 1));
        assert_eq!(super::dec(&[0x48, 0x9d], 64), s(POPF, 8, 2));
        assert_eq!(super::dec(&[0x66, 0x9d], 64), s(POPF, 2, 2));
        assert_eq!(super::dec(&[0x66, 0x48, 0x9d], 64), s(POPF, 8, 3)); // REX.W wins
        assert_eq!(
            super::dec(&[0xf0, 0xfa, 0x90, 0x90, 0x90, 0x90], 64).map(|s| (s.kind, s.lock)),
            Some((UD, 0))
        ); // #UD by LOCK
        assert!(
            s(POPF, 2, 2).unwrap().well_formed()
                && !System {
                    size: 1,
                    ..s(POPF, 4, 1).unwrap()
                }
                .well_formed()
        );
    }

    #[test]
    fn memory_carry_rotates() {
        let p = |hex: &str| {
            let bytes: Vec<u8> = (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect();
            dec(&bytes, 64)
        };
        for (hex, size, cr, reg) in [
            ("d210", 1, 0, 1),
            ("66d318", 2, 1, 1),
            ("d310", 4, 0, 1),
            ("48d318", 8, 1, 1),
            ("c01800", 1, 1, NO_REG),
            ("48c11040", 8, 128, NO_REG),
        ] {
            let s = p(hex).unwrap();
            assert_eq!(
                (s.kind, s.size, s.cr, s.reg, s.memory, s.base),
                (ROTATE, size, cr, reg, 1, 0)
            );
            assert!(s.well_formed());
        }
        assert_eq!(
            p("6764d318").map(|s| (s.prefixes, s.segment)),
            Some((ADDRESS32, SREG_FS as u32))
        );
        for hex in ["d3d0", "d300", "c010"] {
            assert!(refused(p(hex)));
        }
        let left = d("660fa5d3").unwrap();
        assert_eq!(
            (left.kind, left.cr, left.reg, left.memory, left.size),
            (DOUBLE_SHIFT16, 2, 3, 0, 2)
        );
        let right = d("66440fad10").unwrap();
        assert_eq!(
            (right.kind, right.cr, right.base, right.memory, right.size),
            (DOUBLE_SHIFT16, 26, 0, 1, 2)
        );
        assert!(left.well_formed() && right.well_formed());
        assert!(refused(d("0fa5d3")));
    }

    #[test]
    fn bit_tests_under_alignment_checking() {
        let bt =
            |hex: &str| d(hex).map(|s| (s.kind, s.cr, s.reg, s.size, s.memory, s.base, s.length));
        assert_eq!(bt("0fa30b"), Some((BT_ALIGNED, 0, 1, 4, 1, 3, 3))); // bt [rbx], ecx
        assert_eq!(bt("0fab0b"), Some((BT_ALIGNED, 1, 1, 4, 1, 3, 3))); // bts
        assert_eq!(bt("0fb30b"), Some((BT_ALIGNED, 2, 1, 4, 1, 3, 3))); // btr
        assert_eq!(bt("0fbb0b"), Some((BT_ALIGNED, 3, 1, 4, 1, 3, 3))); // btc
        assert_eq!(bt("0fba2b05"), Some((BT_ALIGNED, 1, NO_REG, 4, 1, 3, 4))); // bts dword [rbx], 5
        assert_eq!(bt("0fa3cb"), None); // register operand: never cut
        let w = super::dec(&[0x48, 0x0f, 0xab, 0x0b], 64).unwrap();
        let h = super::dec(&[0x66, 0x0f, 0xab, 0x0b], 64).unwrap();
        assert_eq!(
            (w.size, h.size, w.well_formed(), h.well_formed()),
            (8, 2, true, true)
        );
        // CS/DS/ES/SS overrides are ignored in 64-bit mode (the default segment decides the class); FS/GS add their base,
        // the last of several applies, a null prefix before or after it changes nothing (owner decisions, November 3).
        for prefix in [0x26, 0x2e, 0x36, 0x3e] {
            let p = super::dec(&[prefix, 0x0f, 0xa3, 0x4b, 0x01], 64).unwrap();
            assert_eq!(
                (p.kind, p.segment, p.disp, p.length),
                (BT_ALIGNED, SREG_DS as u32, 1, 5)
            );
        }
        assert_eq!(
            super::dec(&[0x64, 0x0f, 0xa3, 0x4b, 0x01], 64)
                .unwrap()
                .segment,
            SREG_FS as u32
        );
        assert_eq!(
            d("3e480fb39540050000").map(|s| (s.kind, s.base, s.segment)),
            Some((BT_ALIGNED, 5, SREG_SS as u32))
        ); // ds btr [rbp+0x540], rdx
        for (hex, segment) in [
            ("2e3e0fa34b01", SREG_DS),
            ("363e0fa34b01", SREG_DS),
            ("3e640fa34b01", SREG_FS),
            ("643e0fa34b01", SREG_FS),
            ("64650fa34b01", SREG_GS),
            ("65640fa34b01", SREG_FS),
            ("6564360fa34b01", SREG_FS),
        ] {
            assert_eq!(d(hex).map(|s| s.segment), Some(segment as u32), "{hex}");
        }
        assert_eq!(d("360f0138").map(|s| s.segment), Some(SREG_DS as u32)); // invlpg ss:[rax]: faults on nothing
    }

    #[test]
    fn mxcsr_forms() {
        assert_eq!(d("0fae10"), Some(mem(LDMXCSR, 0, 0, 3))); // ldmxcsr [rax]
        // I6: the resident's x87 forms.
        let x87 = p;
        assert_eq!(
            x87("dbe3"),
            Some(System {
                kind: FNINIT,
                length: 2,
                ..System::NONE
            })
        );
        assert_eq!(
            x87("dbe2"),
            Some(System {
                kind: FNCLEX,
                length: 2,
                ..System::NONE
            })
        );
        assert_eq!(x87("d92b"), Some(mem(FLDCW, 3, 0, 2))); // fldcw [rbx]
        assert_eq!(
            x87("66d92648").map(|s| (s.kind, s.size, s.base, s.length)),
            Some((FLDENV, 2, 6, 3))
        ); // fldenv [rsi] (16-bit image)
        assert_eq!(
            x87("d93424").map(|s| (s.kind, s.size, s.base, s.length)),
            Some((FNSTENV, 4, 4, 3))
        ); // fnstenv [rsp]
        assert_eq!(
            x87("6648d97740").map(|s| (s.kind, s.size, s.disp, s.length)),
            Some((FNSTENV, 4, 0x40, 5))
        ); // 66 REX.W: 28 bytes
        assert_eq!(x87("66dd6008").map(|s| (s.kind, s.size)), Some((FRSTOR, 2))); // 66 alone: the 94-byte image
        assert_eq!(
            x87("dd6008").map(|s| (s.kind, s.disp, s.length)),
            Some((FRSTOR, 8, 3))
        ); // frstor [rax+8]
        assert_eq!(
            x87("64dd30").map(|s| (s.kind, s.segment)),
            Some((FNSAVE, SREG_FS as u32))
        ); // fnsave fs:[rax]
        assert_eq!(x87("db2b").map(|s| s.kind), Some(FLD80));
        assert_eq!(x87("db3b").map(|s| s.kind), Some(FSTP80));
        for hex in ["d9e8", "dbc0", "dd00", "d900", "f3d92b"] {
            assert!(refused(x87(hex)), "{hex}"); // admitted forms, register operands, F3
        }
        assert_eq!(
            x87("67d92b").map(|s| (s.kind, s.prefixes)),
            Some((FLDCW, ADDRESS32))
        ); // fldcw [ebx] (I10)
        assert_eq!(d("410fae5c2408"), Some(mem(STMXCSR, 12, 8, 6))); // stmxcsr [r12+8]
        assert_eq!(
            d("640fae18"),
            Some(System {
                segment: 4,
                ..mem(STMXCSR, 0, 0, 4)
            })
        ); // stmxcsr fs:[rax]
        // I8: VLDMXCSR/VSTMXCSR (map 8: fexc's VEX map); VEX.B/X extend base and index; VEX.L 1 or vvvv != 1111b is no form here.
        let v = |h: &str| {
            let bytes: Vec<u8> = (0..h.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&h[i..i + 2], 16).unwrap())
                .collect();
            dec(&[bytes, vec![0x90; 4]].concat(), 64)
        };
        assert_eq!(
            v("c5f8ae10").map(|s| (s.kind, s.base, s.memory, s.length)),
            Some((VLDMXCSR, 0, 1, 4))
        ); // vldmxcsr [rax]
        assert_eq!(
            v("c4c178ae5c2408").map(|s| (s.kind, s.base, s.disp, s.length)),
            Some((VSTMXCSR, 12, 8, 7))
        ); // vstmxcsr [r12+8]
        assert_eq!(
            v("64c5f8ae18").map(|s| (s.kind, s.segment)),
            Some((VSTMXCSR, SREG_FS as u32))
        ); // vstmxcsr fs:[rax]
        assert!(
            refused(v("c5fcae10"))
                && refused(v("c5f0ae10"))
                && refused(v("c5f8aed0"))
                && refused(v("66c5f8ae10"))
        );
        for hex in ["660fae10", "f30fae10"] {
            assert!(refused(d(hex)), "{hex}"); // 66, f3
        }
        assert_eq!(
            d("670fae10"),
            Some(System {
                prefixes: ADDRESS32,
                ..mem(LDMXCSR, 0, 0, 4)
            })
        ); // ldmxcsr [eax] (I10)
    }

    #[test]
    fn xsave_family() {
        assert_eq!(d("0fae00"), Some(mem(FXSAVE, 0, 0, 3))); // fxsave [rax]
        assert_eq!(d("480fae03"), Some(mem(FXSAVE, 3, 0, 4))); // fxsave64 [rbx]
        assert_eq!(d("0fae4e10"), Some(mem(FXRSTOR, 6, 0x10, 4))); // fxrstor [rsi+0x10]
        assert_eq!(d("0fae2424"), Some(mem(XSAVE, 4, 0, 4))); // xsave [rsp]
        assert_eq!(
            d("650fae2f"),
            Some(System {
                segment: 5,
                ..mem(XRSTOR, 7, 0, 4)
            })
        ); // xrstor gs:[rdi]
        let xsaveopt = if super::advertised(super::gf::GF_XSAVEOPT) {
            Some(mem(XSAVE, 0, 0, 3))
        } else {
            Some(System {
                kind: UD,
                length: 3,
                ..System::NONE
            })
        };
        assert_eq!(d("0fae30"), xsaveopt); // xsaveopt [rax]: XSAVE's transaction
        assert_eq!(d("480fae7708").map(|s| s.kind), xsaveopt.map(|s| s.kind)); // xsaveopt64 [rdi+8]
        assert_eq!(d("f20fae30").map(|s| s.kind), Some(UD));
        let xsavec = super::advertised(super::gf::GF_XSAVEC);
        assert_eq!(
            d("0fc727"),
            if xsavec {
                Some(mem(XSAVEC, 7, 0, 3))
            } else {
                Some(System {
                    kind: UD,
                    length: 3,
                    ..System::NONE
                })
            }
        ); // xsavec [rdi]
        assert_eq!(
            d("480fc7642408").map(|s| s.kind),
            Some(if xsavec { XSAVEC } else { UD })
        ); // xsavec64 [rsp+8]
        assert!(refused(d("660fc727"))); // 66: no XSAVEC form
        let ud4 = Some(System {
            kind: UD,
            length: 4,
            ..System::NONE
        });
        let clwb = if super::advertised(super::gf::GF_CLWB) {
            Some(System {
                prefixes: 0,
                ..mem(CLFLUSH, 0, 0, 4)
            })
        } else {
            ud4
        };
        assert_eq!(d("660fae30"), clwb); // clwb [rax]
        assert_eq!(d("f3660fae30").map(|s| s.kind), Some(UD)); // F3: CLRSSBSY (CET not advertised)
        for hex in ["0fc718", "480fc728"] {
            assert_eq!(d(hex).map(|s| s.kind), Some(UD), "{hex}"); // xrstors, xsaves64
        }
        assert_eq!(d("0fc708"), None); // cmpxchg8b stays admitted
        for hex in ["660fae00", "f20fae28"] {
            assert!(refused(d(hex)), "{hex}"); // NP forms with 66/F2
        }
        for hex in ["f30fae20", "f30faee0"] {
            assert_eq!(d(hex).map(|s| s.kind), Some(UD), "{hex}"); // X1c: PTWRITE m, r (Intel PT not advertised)
        }
        assert_eq!(
            d("670fae00"),
            Some(System {
                prefixes: ADDRESS32,
                ..mem(FXSAVE, 0, 0, 4)
            })
        ); // fxsave [eax] (I10)
        assert_eq!(
            d("0f01d0"),
            Some(System {
                kind: XGETBV,
                length: 3,
                ..System::NONE
            })
        );
        assert_eq!(
            d("0f01d1"),
            Some(System {
                kind: XSETBV,
                length: 3,
                ..System::NONE
            })
        );
        assert_eq!(d("f00f01d1").map(|s| (s.kind, s.lock)), Some((UD, 0))); // LOCK: #UD in the resident
        assert!(refused(d("660f01d0")));
        assert!(refused(d("f30f01d0")));
    }

    #[test]
    fn invlpg_operands() {
        assert_eq!(d("0f0138"), Some(invlpg(0, NO_REG, 0, 0, 3))); // [rax]
        assert_eq!(d("410f017810"), Some(invlpg(8, NO_REG, 0, 0x10, 5))); // [r8+0x10]
        assert_eq!(d("0f013d00100000"), Some(invlpg(RIP, NO_REG, 0, 0x1000, 7))); // [rip+0x1000]
        assert_eq!(d("420f013c8b"), Some(invlpg(3, 9, 2, 0, 5))); // [rbx+r9*4]
        assert_eq!(
            d("0f013c25f0ffffff"),
            Some(invlpg(NO_REG, NO_REG, 0, -16, 8))
        ); // [disp32]
        assert_eq!(
            d("0f01bc2400000080"),
            Some(invlpg(4, NO_REG, 0, -0x8000_0000, 8))
        ); // [rsp+disp32]
        assert_eq!(d("0f017d00"), Some(invlpg(5, NO_REG, 0, 0, 4))); // [rbp+0]
        assert_eq!(
            d("670f0138"),
            Some(System {
                prefixes: ADDRESS32,
                ..invlpg(0, NO_REG, 0, 0, 4)
            })
        ); // [eax] (I10)
    }

    #[test]
    fn unsupported_forms_are_refused() {
        let bytes = |hex: &str| -> Vec<u8> {
            (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect()
        };
        for hex in [
            "0fb218",   // lss m16:32: not built
            "650f0138", // GS override
            "f30f22d8", // F3
            "f30f33",   // F3 rdpmc
            "0f22",     // truncated ModRM
            "0f013c",   // truncated SIB
            "0f017810", // fine, but see below: complete
            "f30fa2",   // F3 cpuid
        ] {
            let got = dec(&bytes(hex), 64);
            assert_eq!(got.is_some(), hex == "0f017810", "{hex}");
        }
    }

    /// No-stop change: the masked moves' decode (`system::MASKMOV`): data, mask, L, store, VEX, width and the operand.
    #[test]
    fn masked_moves() {
        let bytes = |hex: &str| -> Vec<u8> {
            (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect()
        };
        for (hex, reg, cr, size, base) in [
            ("c4e2752c00", 0, 1 | 16 | 64, 4, 0), // vmaskmovps ymm0, ymm1, [rax]
            ("c4e2752e08", 1, 1 | 16 | 32 | 64, 4, 0), // vmaskmovps [rax], ymm1, ymm1
            ("c4e2e98c17", 2, 2 | 64, 8, 7),      // vpmaskmovq xmm2, xmm2, [rdi]
            ("c462698e0f", 9, 2 | 32 | 64, 4, 7), // vpmaskmovd [rdi], xmm2, xmm9
            ("c4c2712d03", 0, 1 | 64, 8, 11),     // vmaskmovpd xmm0, xmm1, [r11]
            ("c5f9f7ca", 1, 2 | 32 | 64, 1, 7),   // vmaskmovdqu xmm1, xmm2
            ("660ff7ca", 1, 2 | 32, 1, 7),        // maskmovdqu xmm1, xmm2
            ("66440ff7c1", 8, 1 | 32, 1, 7),
            ("0ff7ca", 1, 2 | 32 | 128, 1, 7),
            ("4f0ff7ca", 1, 2 | 32 | 128, 1, 7), // maskmovq mm1, mm2; REX ignored
        ] {
            let s = dec(&bytes(hex), 64).unwrap_or_else(|| panic!("{hex}"));
            assert_eq!(
                (
                    s.kind,
                    s.reg,
                    s.cr,
                    s.size,
                    s.memory,
                    s.base,
                    s.length as usize
                ),
                (MASKMOV, reg, cr, size, 1, base, hex.len() / 2),
                "{hex}"
            );
        }
        let mm = dec(&bytes("0ff7ca"), 64).unwrap();
        assert!(mm.well_formed());
        for cr in [128, 160 | 8, 160 | 16, 160 | 64, 160 | 256] {
            assert!(!System { cr, ..mm }.well_formed());
        }
        assert!(!System { reg: 8, ..mm }.well_formed());
        assert!(!System { size: 8, ..mm }.well_formed());
        // A register operand of the VEX.0F38 forms, a memory one of MASKMOVDQU, VMASKMOVDQU with VEX.L = 1: not decoded.
        for hex in ["0ff700", "c4e2752cc0", "660ff700", "c5fdf7ca"] {
            assert!(refused(dec(&bytes(hex), 64)), "{hex}");
        }
    }

    #[test]
    fn x1c_forms() {
        let bytes = |hex: &str| -> Vec<u8> {
            (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect()
        };
        let kind = |hex: &str| dec(&[bytes(hex), vec![0x90; 4]].concat(), 64).map(|s| s.kind);
        for hex in [
            "06",
            "1f",
            "9a00000000080000",
            "ea000000000800",
            "0fb4c0",
            "0f01c7",
            "660f01cd",
            "f20f01d2",
            "0f01d3",
            "f30f01ec",
            "0f012d00000000",
            "0f00f8",
            "0f003800",
        ] {
            assert_eq!(kind(hex), Some(UD), "{hex}");
        }
        assert_eq!(kind("0f06"), Some(CLTS));
        assert_eq!(kind("0f34"), Some(SYSENTER));
        let sys =
            |hex: &str| dec(&bytes(hex), 64).map(|s| (s.kind, s.size, s.memory, s.reg, s.length));
        assert_eq!(sys("0f35"), Some((SYSEXIT, 4, 0, 0, 2)));
        assert_eq!(sys("480f35"), Some((SYSEXIT, 8, 0, 0, 3)));
        assert_eq!(sys("0f01e0"), Some((SMSW, 4, 0, 0, 3))); // smsw eax
        assert_eq!(sys("660f01e3"), Some((SMSW, 2, 0, 3, 4))); // smsw bx
        assert_eq!(sys("490f01e1"), Some((SMSW, 8, 0, 9, 4))); // smsw r9
        assert_eq!(sys("0f0120").map(|s| (s.0, s.2)), Some((SMSW, 1))); // smsw [rax]
        // F2/F3 ignored (the reference AMD machine): rep smsw rsp, repne smsw rbp, rep smsw r9w, rep smsw [rax].
        assert_eq!(sys("f3480f01e4"), Some((SMSW, 8, 0, 4, 5)));
        assert_eq!(sys("f2480f01e5"), Some((SMSW, 8, 0, 5, 5)));
        assert_eq!(sys("f366410f01e1"), Some((SMSW, 2, 0, 9, 6)));
        assert_eq!(sys("f30f0120").map(|s| (s.0, s.2)), Some((SMSW, 1)));
        assert_eq!(sys("0f01f0").map(|s| (s.0, s.3)), Some((LMSW, 0))); // lmsw ax
        assert_eq!(sys("0f0130").map(|s| (s.0, s.2)), Some((LMSW, 1))); // lmsw [rax]
        assert_eq!(kind("0f01c0"), Some(UD)); // ENCLV/VMX: unchanged
        assert_eq!(kind("0f01d0"), Some(XGETBV)); // unchanged
        assert_eq!(kind("0fb218"), None); // lss m: not built
    }

    #[test]
    fn port_io_forms() {
        let io = |kind, size, reg, cr, length| {
            Some(System {
                kind,
                size,
                reg,
                cr,
                length,
                ..System::NONE
            })
        };
        assert_eq!(p("e480"), io(IN, 1, NO_REG, 0x80, 2)); // in al, 0x80
        assert_eq!(p("66e5f8"), io(IN, 2, NO_REG, 0xf8, 3)); // in ax, 0xf8
        assert_eq!(p("48e5f8"), io(IN, 4, NO_REG, 0xf8, 3)); // REX.W: still eax
        assert_eq!(p("6648e5f8"), io(IN, 4, NO_REG, 0xf8, 4)); // REX.W wins over 66: 32 bits
        assert_eq!(p("e6e9"), io(OUT, 1, NO_REG, 0xe9, 2)); // out 0xe9, al
        assert_eq!(p("e7ff"), io(OUT, 4, NO_REG, 0xff, 2));
        assert_eq!(p("ec"), io(IN, 1, 2, 0, 1)); // in al, dx
        assert_eq!(p("66ed"), io(IN, 2, 2, 0, 2));
        assert_eq!(p("ef"), io(OUT, 4, 2, 0, 1)); // out dx, eax
        assert_eq!(p("2e67ee"), io(OUT, 1, 2, 0, 3)); // overrides and 67 do not apply
        assert_eq!(p("f0ec").map(|s| (s.kind, s.lock)), Some((UD, 0))); // #UD by LOCK
        let string = |kind, size, prefixes, segment, length| {
            Some(System {
                kind,
                size,
                prefixes,
                segment,
                length,
                ..System::NONE
            })
        };
        assert_eq!(p("6c"), string(INS, 1, 0, 0, 1));
        assert_eq!(p("f3666d"), string(INS, 2, REP, 0, 3)); // rep insw
        assert_eq!(p("f3486f"), string(OUTS, 4, REP, SREG_DS as u32, 3)); // rep outsd (REX.W ignored)
        assert_eq!(
            p("67f36e"),
            string(OUTS, 1, REP | ADDRESS32, SREG_DS as u32, 3)
        ); // rep outsb with esi/ecx
        assert_eq!(p("646e"), string(OUTS, 1, 0, SREG_FS as u32, 2)); // outsb fs:[rsi]
        assert_eq!(p("656c"), string(INS, 1, 0, 0, 2)); // INS: ES only, the override is ignored
        assert_eq!(p("3e6f"), string(OUTS, 4, 0, SREG_DS as u32, 2)); // a null DS override
        assert_eq!(p("366e"), string(OUTS, 1, 0, SREG_DS as u32, 2)); // SS ignored: DS:RSI
        assert_eq!(p("64656e"), string(OUTS, 1, 0, SREG_GS as u32, 3)); // the last FS/GS applies
        for hex in ["f26c", "f3ec", "f2e480"] {
            assert!(refused(p(hex)), "{hex}"); // REPNE, F3 on IN
        }
        assert_eq!(super::dec(&[0xe4], 64), None); // truncated imm8
    }

    #[test]
    fn s3_forms() {
        let k = |kind, length| {
            Some(System {
                kind,
                length,
                ..System::NONE
            })
        };
        assert_eq!(d("0f01f8"), k(SWAPGS, 3));
        assert_eq!(d("0f05"), k(SYSCALL, 2));
        assert_eq!(
            d("480f07"),
            Some(System {
                kind: SYSRET,
                size: 8,
                length: 3,
                ..System::NONE
            })
        );
        assert_eq!(
            d("0f07"),
            Some(System {
                kind: SYSRET,
                size: 4,
                length: 2,
                ..System::NONE
            })
        ); // sysretl
        assert_eq!(d("660f07"), None);
        let base = |kind, reg, size, length| {
            Some(System {
                kind,
                reg,
                size,
                length,
                ..System::NONE
            })
        };
        assert_eq!(d("f3480faec0"), base(RDFSBASE, 0, 8, 5));
        assert_eq!(d("f30faecb"), base(RDGSBASE, 3, 4, 4));
        assert_eq!(d("f3490faed1"), base(WRFSBASE, 9, 8, 5));
        assert_eq!(d("f3480faedf"), base(WRGSBASE, 7, 8, 5));
        assert_eq!(d("0faec0"), k(UD, 3)); // fxsave with a register operand
        assert_eq!(d("66f30faec0"), None); // 66 on rdfsbase: no SDM encoding
        assert_eq!(
            d("660f02c1"),
            Some(System {
                kind: LAR,
                cr: 0,
                reg: 1,
                size: 2,
                length: 4,
                ..System::NONE
            })
        );
        assert_eq!(
            d("4c0f03c9"),
            Some(System {
                kind: LSL,
                cr: 9,
                reg: 1,
                size: 8,
                length: 4,
                ..System::NONE
            })
        ); // lsl r9, rcx
        assert_eq!(
            d("0f030d10000000"),
            Some(System {
                cr: 1,
                size: 4,
                ..mem(LSL, RIP, 0x10, 7)
            })
        ); // lsl ecx, [rip+0x10]
        assert_eq!(
            d("0f00e1"),
            Some(System {
                kind: VERR,
                reg: 1,
                size: 4,
                length: 3,
                ..System::NONE
            })
        );
        assert_eq!(
            d("0f002d10000000"),
            Some(System {
                size: 4,
                ..mem(VERW, RIP, 0x10, 7)
            })
        ); // verw [rip+0x10]
        assert_eq!(d("f20f00f0"), k(UD, 4)); // lkgs
        assert_eq!(d("0fc7f0"), base(RDRAND, 0, 4, 3));
        assert_eq!(d("660fc7f1"), base(RDRAND, 1, 2, 4));
        assert_eq!(d("490fc7f7"), base(RDRAND, 15, 8, 4));
        for hex in ["0fc7f8", "f30fc7f8", "f30fc7f0"] {
            assert_eq!(d(hex), k(UD, hex.len() as u32 / 2), "{hex}"); // rdseed, rdpid, f3 rdrand
        }
        assert_eq!(d("0fc708"), None); // cmpxchg8b: admitted, never cut
        assert_eq!(d("0f09"), k(WBINVD, 2));
        assert_eq!(d("f30f09"), k(WBINVD, 3)); // wbnoinvd
        assert_eq!(d("0f08"), k(WBINVD, 2)); // invd
        assert_eq!(d("f20f09"), None);
        assert_eq!(d("0fae3b"), Some(mem(CLFLUSH, 3, 0, 3))); // clflush [rbx]
        assert_eq!(
            d("650fae7808"),
            Some(System {
                segment: 5,
                ..mem(CLFLUSH, 0, 8, 5)
            })
        ); // clflush gs:[rax+8]
        let clflushopt = if super::advertised(super::gf::GF_CLFLUSHOPT) {
            Some(mem(CLFLUSH, 0, 0, 4))
        } else {
            k(UD, 4)
        };
        assert_eq!(d("660fae38"), clflushopt); // clflushopt [rax]
        assert_eq!(d("f2660fae38").map(|s| s.kind), Some(UD)); // SDM CLFLUSHOPT: #UD with F2/F3
        assert_eq!(d("66f30fae38").map(|s| s.kind), Some(UD));
        for hex in [
            "0f01c0", "0f01c5", "0f01c6", "f20f01e9", "0f01ca", "0f01cb", "0f01c8", "0f01c9",
            "0f01e8", "0f01ee", "0f01ef", "0f01c1", "0f01d9", "660f01cc", "f30f01fe", "f20f01ff",
            "f30f01ea", "f30f01e8", "f30f012b",
        ] {
            assert_eq!(d(hex), k(UD, hex.len() as u32 / 2), "{hex}");
        }
        assert_eq!(d("0f78c0"), k(UD, 3));
        // SSE4a EXTRQ (66, FEX's 66 overlay) and INSERTQ (F2, its F2 overlay, map 3): not advertised, #UD.
        assert_eq!(
            super::dec(
                &[0x66, 0x0f, 0x78, 0xc0, 0x00, 0x00, 0x90, 0x90, 0x90, 0x90],
                64
            )
            .map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::dec(&[0x66, 0x41, 0x0f, 0x79, 0xc0, 0x90, 0x90, 0x90, 0x90], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::dec(
                &[0xf2, 0x0f, 0x78, 0xc1, 0x00, 0x3f, 0x90, 0x90, 0x90, 0x90],
                64
            )
            .map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::dec(&[0xf2, 0x41, 0x0f, 0x79, 0xc0, 0x90, 0x90, 0x90, 0x90], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::dec(
                &[0x66, 0x0f, 0x38, 0x82, 0x00, 0x90, 0x90, 0x90, 0x90, 0x90],
                64
            )
            .map(|s| s.kind),
            Some(UD)
        ); // invpcid
        // I9: MOVBE register forms, WRUSS: #UD; a memory MOVBE and CRC32 are never decoded here. ADCX/ADOX and SHA: #UD
        // exactly while the profile does not advertise them.
        assert_eq!(
            super::dec(&[0x48, 0x0f, 0x38, 0xf0, 0xc1, 0x90, 0x90, 0x90, 0x90], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::dec(&[0x66, 0x0f, 0x38, 0xf1, 0xc8, 0x90, 0x90, 0x90, 0x90], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(super::dec(&[0x0f, 0x38, 0xf0, 0x07], 64), None);
        assert_eq!(super::dec(&[0xf2, 0x0f, 0x38, 0xf0, 0xc1], 64), None);
        let adx = (!super::advertised(super::gf::GF_ADX)).then_some(UD);
        assert_eq!(
            super::dec(&[0x66, 0x48, 0x0f, 0x38, 0xf6, 0xc1], 64).map(|s| s.kind),
            adx
        );
        assert_eq!(
            super::dec(&[0xf3, 0x48, 0x0f, 0x38, 0xf6, 0x07], 64).map(|s| s.kind),
            adx
        );
        assert_eq!(
            super::dec(&[0x66, 0xf3, 0x0f, 0x38, 0xf6, 0xc1], 64).map(|s| s.kind),
            adx
        );
        assert_eq!(
            super::dec(&[0x0f, 0x38, 0xf6, 0xc1, 0x90, 0x90, 0x90, 0x90], 64).map(|s| s.kind),
            Some(UD)
        ); // wrss (NP; CET_SS not advertised)
        assert_eq!(
            super::dec(&[0xf2, 0x0f, 0x38, 0xf6, 0xc1, 0x90, 0x90, 0x90, 0x90], 64).map(|s| s.kind),
            Some(UD)
        ); // a blank cell
        assert_eq!(
            super::dec(
                &[0x66, 0xf2, 0x0f, 0x38, 0xf6, 0xc1, 0x90, 0x90, 0x90, 0x90],
                64
            )
            .map(|s| s.kind),
            Some(UD)
        ); // #UD on the AMD machine too
        let sha = (!super::advertised(super::gf::GF_SHA)).then_some(UD);
        assert_eq!(
            super::dec(&[0x0f, 0x38, 0xcb, 0xc1], 64).map(|s| s.kind),
            sha
        );
        assert_eq!(
            super::dec(&[0x0f, 0x3a, 0xcc, 0xc1, 0x01], 64).map(|s| s.kind),
            sha
        );
        assert!(refused(super::dec(
            &[0x66, 0x0f, 0x3a, 0xcc, 0xc1, 0x01],
            64
        )));
        assert_eq!(d("f00f01f8").map(|s| (s.kind, s.lock)), Some((UD, 0))); // #UD by LOCK
    }
}
