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
use natemu_abi::execution::{SREG_DS, SREG_ES, SREG_SS, System, system};

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

/// Decodes the instruction at the start of `bytes`, which fexc cut as a system instruction, in a `bitness`-bit code
/// segment (64, 32 or 16). `None` for a truncated instruction or a form with no record.
pub(crate) fn decode(bytes: &[u8], bitness: u32) -> Option<System> {
    use Code::*;
    use system::*;
    // fexc cut a whole instruction, but `bytes` may end with it: iced calls an encoding it finds no instruction in "no
    // more bytes" unless 15 are there, so it decodes them padded; a longer instruction than `bytes` is truncated.
    let mut padded = [0; 15];
    padded[..bytes.len().min(15)].copy_from_slice(&bytes[..bytes.len().min(15)]);
    // The Reserved-NOP cells (0f 0d, 0f 18..1f) decode as such whatever prefetch, CET or MPX form they also are.
    let i = Decoder::new(bitness, &padded, DecoderOptions::FORCE_RESERVED_NOP).decode();
    if !i.is_invalid() && i.len() > bytes.len() {
        return None;
    }
    // #UD leaves RIP at the instruction: no operand is used, nor the length (iced's, at most `bytes`, for no instruction).
    let length = i.len().min(bytes.len()) as u32;
    let ud = Some(System {
        kind: UD,
        length,
        ..System::NONE
    });
    if i.is_invalid() || !available(&i) {
        return ud;
    }
    let mut s = System {
        length,
        lock: u32::from(i.has_lock_prefix()),
        ..System::NONE
    };
    let gpr = |k: u32| i.op_register(k).number() as u32;
    let width = |k: u32| i.op_register(k).size() as u32;
    // An r/m operand that is a register (a memory one is decoded below).
    let rm = |k: u32| {
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
        Invlpg_m if matches!(i.segment_prefix(), Register::FS | Register::GS) => return None,
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
            return None;
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
        | Ud1_r64_rm64 | Ud2 => return ud,
        Sgdt_m1632_16 | Sgdt_m1632 | Sgdt_m1664 => (SGDT, 0),
        Sidt_m1632_16 | Sidt_m1632 | Sidt_m1664 => (SIDT, 0),
        Lgdt_m1632_16 | Lgdt_m1632 | Lgdt_m1664 => (LGDT, 0),
        Lidt_m1632_16 | Lidt_m1632 | Lidt_m1664 => (LIDT, 0),
        // SMSW r/m16, r32, r64 (a memory destination is always 16 bits) and LMSW r/m16. F2/F3 on SMSW (reserved) are ignored,
        // as on the reference AMD machine (owner rule; user-mode probe, November 3: every F2/F3 form took the UMIP #GP of
        // plain SMSW, none #UD; plan §5).
        Smsw_rm16 | Smsw_r32m16 | Smsw_r64m16 => {
            (s.reg, rep) = (rm(0), true);
            (SMSW, operand_size(i.code()))
        }
        Lmsw_rm16 | Lmsw_r32m16 | Lmsw_r64m16 => {
            s.reg = rm(0);
            (LMSW, 0)
        }
        // The 0f 00 group and MOV Sreg (the segment register in `cr`): the operand size, a register destination's (2: its
        // low 16 bits; 4, 8: the selector zero-extended); a memory operand is always 16 bits.
        Sldt_rm16 | Sldt_r32m16 | Sldt_r64m16 | Str_rm16 | Str_r32m16 | Str_r64m16 | Lldt_rm16
        | Lldt_r32m16 | Lldt_r64m16 | Ltr_rm16 | Ltr_r32m16 | Ltr_r64m16 | Verr_rm16
        | Verr_r32m16 | Verr_r64m16 | Verw_rm16 | Verw_r32m16 | Verw_r64m16 => {
            s.reg = rm(0);
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
            (s.cr, s.reg) = (gpr(1), rm(0));
            (MOV_FROM_SREG, operand_size(i.code()))
        }
        Mov_Sreg_rm16 | Mov_Sreg_r32m16 | Mov_Sreg_r64m16 => {
            (s.cr, s.reg) = (gpr(0), rm(1));
            (MOV_TO_SREG, operand_size(i.code()))
        }
        // LAR/LSL r, r/m16: the destination in `cr`.
        Lar_r16_rm16 | Lar_r32_r32m16 | Lar_r64_r64m16 => {
            (s.cr, s.reg) = (gpr(0), rm(1));
            (LAR, width(0))
        }
        Lsl_r16_rm16 | Lsl_r32_r32m16 | Lsl_r64_r64m16 => {
            (s.cr, s.reg) = (gpr(0), rm(1));
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
        Insb_m8_DX | Insw_m16_DX | Insd_m32_DX | Outsb_DX_m8 | Outsw_DX_m16 | Outsd_DX_m32 => {
            let ins = matches!(i.code(), Insb_m8_DX | Insw_m16_DX | Insd_m32_DX);
            if i.has_repne_prefix() {
                return None;
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
            s.reg = rm(0);
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
            (s.reg, rep) = (rm(0), true);
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
        _ => return None,
    };
    if !rep && (i.has_rep_prefix() || i.has_repne_prefix()) {
        return None;
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
    s.well_formed().then_some(s)
}

/// The operand size (bytes) of the forms [`decode`] takes it from iced's name of: `_rm16`/`w` 2, `_r64m16`/`q` 8, else 4.
fn operand_size(c: Code) -> u32 {
    use Code::*;
    match c {
        Smsw_rm16 | Sldt_rm16 | Str_rm16 | Lldt_rm16 | Ltr_rm16 | Verr_rm16 | Verw_rm16
        | Mov_rm16_Sreg | Mov_Sreg_rm16 | Pushw_ES | Pushw_CS | Pushw_SS | Pushw_DS | Pushw_FS
        | Pushw_GS | Popw_ES | Popw_SS | Popw_DS | Popw_FS | Popw_GS | Enterw_imm16_imm8
        | Retfw | Retfw_imm16 => 2,
        Smsw_r64m16 | Sldt_r64m16 | Str_r64m16 | Lldt_r64m16 | Ltr_r64m16 | Verr_r64m16
        | Verw_r64m16 | Mov_r64m16_Sreg | Mov_Sreg_r64m16 | Pushq_FS | Pushq_GS | Popq_FS
        | Popq_GS | Enterq_imm16_imm8 | Retfq | Retfq_imm16 => 8,
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

/// Whether the presented CPU has the instruction: any of iced's CPUID features for it that the catalogue (generated
/// `natemu_abi::cpu_gates`) advertises; while all of the catalogue's are clear, or it is a feature no profile has
/// ([`NEVER`]), the instruction is #UD (SDM Vol.2 each: "#UD If CPUID ... = 0", or "not in VMX operation"). Features
/// outside both (X64, INTEL386, MSR ..) are every 64-bit processor's.
fn available(i: &Instruction) -> bool {
    let mut base = true;
    let features = i.cpuid_features().iter().flat_map(|&f| match f {
        CpuidFeature::HLE_or_RTM => vec![CpuidFeature::HLE, CpuidFeature::RTM],
        f => vec![f],
    });
    for f in features {
        match catalogue(f) {
            Some(g) if advertised(g) => return true,
            Some(_) => base = false,
            None => base &= !NEVER.contains(&f),
        }
    }
    base
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

/// The hand-written 64-bit decoder iced-x86 replaces, kept for the differential test below until the comparison is done.
#[cfg(test)]
pub(crate) mod old {
    #![allow(dead_code)]
    use super::*;
    use natemu_abi::execution::{SREG_FS, SREG_GS};
    /// fexc's opcode maps (`FEXC_MAP_PRIMARY`, `FEXC_MAP_TWO`). The two-byte map is reported as 1 even with a 66 prefix (the
    /// 66 overlay, map 4, is not reported for these opcodes).
    const MAP_PRIMARY: u8 = 0;
    const MAP_TWO: u8 = 1;
    const MAP_TWO_F3: u8 = 2;
    const MAP_TWO_66: u8 = 4;
    const MAP_0F38: u8 = 5;
    const MAP_0F3A: u8 = 6;
    /// fexc's VEX map (`FEXC_MAP_VEX`, I8): the opcode byte after the VEX prefix.
    const MAP_VEX: u8 = 8;

    /// Decodes the instruction at the start of `bytes`, which fexc cut as a system instruction with opcode map `map` and
    /// opcode `opcode`.
    pub(crate) fn decode(bytes: &[u8], map: u8, opcode: u8) -> Option<System> {
        // `segment`: the last FS/GS prefix (0x64, 0x65), else 0. CS/DS/ES/SS prefixes are ignored in 64-bit mode (owner decision,
        // November 3: as on the reference AMD machine), so the operand's default segment decides a non-canonical address's
        // exception (the resident's `segments::noncanonical`); of several FS/GS prefixes the last applies (the same machine).
        let (mut at, mut lock, mut segment, mut address_size, mut operand16, mut rep) =
            (0, false, 0, false, false, 0);
        let mut rex = 0;
        loop {
            match *bytes.get(at)? {
                // REX bytes among the prefixes: the last one before the opcode counts ([`opcode_at`]).
                r @ 0x40..=0x4f => {
                    rex = r;
                    at += 1;
                    continue;
                }
                0xf0 => lock = true,
                0x66 => operand16 = true,
                0x67 => address_size = true,
                s @ (0x64 | 0x65) => segment = s,
                0x26 | 0x2e | 0x36 | 0x3e => {}
                // F2/F3 (mandatory-prefix overlays): only the forms that name them below (`rep_used`) take them.
                p @ (0xf2 | 0xf3) => rep = p,
                _ => break,
            }
            rex = 0;
            at += 1;
        }
        let mut rep_used = false;
        // The VEX prefix's R byte and its W vvvv L pp byte (I8; the R bit alone for a C5 form).
        let mut vex = (0u8, 0u8);
        let (w, r) = (rex & 8 != 0, u32::from(rex >> 2 & 1) << 3);
        match map {
            MAP_TWO if *bytes.get(at)? == 0x0f && *bytes.get(at + 1)? == opcode => at += 2,
            // X1c: a Reserved-NOP cell under a mandatory-prefix overlay map (F3, F2, 66): the same two-byte opcode; also FEX's
            // SSE4a overlays of 0f 78/79 (66 EXTRQ, F2 INSERTQ).
            MAP_TWO_F3..=MAP_TWO_66
                if matches!(opcode, 0x0d | 0x18..=0x1d | 0x78 | 0x79 | 0xf7)
                    && *bytes.get(at)? == 0x0f
                    && *bytes.get(at + 1)? == opcode =>
            {
                at += 2
            }
            MAP_PRIMARY if *bytes.get(at)? == opcode => at += 1,
            MAP_0F38 if bytes.get(at..at + 3)? == [0x0f, 0x38, opcode] => at += 3,
            MAP_0F3A if bytes.get(at..at + 3)? == [0x0f, 0x3a, opcode] => at += 3,
            // I8: VLDMXCSR/VSTMXCSR: map 0F, pp 0, VEX.L 0, VEX.vvvv 1111b (fexc admitted nothing else here); and the AVX2 gathers
            // (map 0F38, pp 66, opcodes 90..93, either VEX.L and VEX.W). VEX.B and VEX.X extend the ModRM operand's base and index
            // as REX would. No legacy SSE prefix or REX before VEX (`vex_ud`).
            MAP_VEX if rex == 0 && !operand16 && rep == 0 => {
                let (b1, b2) = (*bytes.get(at + 1)?, *bytes.get(at + 2)?);
                let (map_select, p, op_at, xb) = match bytes[at] {
                    0xc5 => (1, b1, at + 2, 0),
                    0xc4 => (b1 & 31, b2, at + 3, (!b1 >> 5 & 1) | (!b1 >> 6 & 1) << 1),
                    _ => return None,
                };
                let gather = map_select == 2 && p & 3 == 1 && matches!(opcode, 0x90..=0x93);
                let half = map_select == 3 && p & 3 == 1 && opcode == 0x1d;
                // No-stop change: the masked moves (VMASKMOVPS/PD, VPMASKMOVD/Q: map 0F38, pp 66; VMASKMOVDQU: map 0F, pp 66).
                let masked = p & 3 == 1
                    && ((map_select == 2 && matches!(opcode, 0x2c..=0x2f | 0x8c | 0x8e))
                        || (map_select == 1 && opcode == 0xf7));
                if !(gather
                    || masked
                    || half
                    || (map_select == 1 && p & 0x7f == 0x78 && opcode == 0xae))
                    || *bytes.get(op_at)? != opcode
                {
                    return None;
                }
                (rex, vex) = (xb, (b1, p));
                at = op_at + 1;
            }
            _ => return None,
        }
        let mut s = System {
            lock: u32::from(lock),
            ..System::NONE
        };
        // Operand size of a register destination and far pointers/pops (66: 16 bits, REX.W wins).
        let size = if w {
            8
        } else if operand16 {
            2
        } else {
            4
        };
        let far_size = if w {
            8
        } else if operand16 {
            2
        } else {
            4
        };
        match (map, opcode) {
            (MAP_TWO, 0x20..=0x23) => {
                // The mod field is ignored: the operand is always a register.
                let modrm = *bytes.get(at)?;
                at += 1;
                s.kind = [
                    system::MOV_FROM_CR,
                    system::MOV_FROM_DR,
                    system::MOV_TO_CR,
                    system::MOV_TO_DR,
                ][usize::from(opcode - 0x20)];
                (s.cr, s.reg) = (
                    u32::from(modrm >> 3 & 7) | r,
                    u32::from(modrm & 7) | u32::from(rex & 1) << 3,
                );
            }
            (MAP_TWO, 0x30 | 0x32) => {
                s.kind = if opcode == 0x30 {
                    system::WRMSR
                } else {
                    system::RDMSR
                }
            }
            (MAP_TWO, 0x33) => s.kind = system::RDPMC,
            (MAP_TWO, 0xa2) => s.kind = system::CPUID,
            (MAP_TWO, 0x31) => s.kind = system::RDTSC,
            (MAP_TWO, 0x01) if *bytes.get(at)? == 0xf9 && !advertised(gf::GF_RDTSCP) => {
                (at, rep_used, s.kind) = (at + 1, true, system::UD);
            }
            (MAP_TWO, 0x01) if *bytes.get(at)? == 0xf9 => {
                at += 1;
                s.kind = system::RDTSCP;
            }
            (MAP_TWO, 0x01) if *bytes.get(at)? == 0xf8 => {
                at += 1;
                s.kind = system::SWAPGS;
            }
            // S3: register forms of group 7 whose CPUID feature the profile does not advertise, or that need VMX/SVM/SGX/TDX
            // operation the presented CPU has none of: ENCLV, VMCALL/VMLAUNCH/VMRESUME/VMXOFF, PCONFIG, WRMSRNS, MONITOR/MWAIT, CLAC/STAC, TDCALL,
            // ENCLS, VMFUNC/XEND/XTEST/ENCLU, the SVM group, SERIALIZE and XSUSLDTRK/XRESLDTRK (f2 e8/e9), RDPKRU/WRPKRU, MONITORX/MWAITX, CLZERO, RDPRU and the
            // RMP/INVLPGB group: #UD whatever the prefixes (SDM Vol.2 each: "#UD If CPUID ... = 0" / "not in VMX operation").
            // I10: SAVEPREVSSP (f3 0f 01 ea; SETSSBSY is f3 e8): "#UD If CR4.CET = 0" (CET not advertised); reserved otherwise.
            // X1c: the rest of mod = 11 outside SMSW (/4) and LMSW (/6): PBNDKB (c7), TDX SEAMRET/SEAMOPS (66 cd/ce; reserved
            // without 66), FRED ERETS/ERETU (f2/f3 ca, in c8..cc), UIRET (f3 ec): not advertised; d2, d3, eb, ed and the unprefixed forms are blank
            // in Table A-6 (reserved: #UD, SDM Vol.3 7.15 interrupt 6).
            // The catalogue's feature of each form ([`group7`]): #UD while it is clear; advertised, the feature's own implementation
            // decides, and none is built (a stop). The forms of no catalogue feature are #UD whatever the profile.
            (MAP_TWO, 0x01) if matches!(*bytes.get(at)?, 0xc0..=0xcf | 0xd2..=0xdf | 0xe8..=0xef | 0xfa..=0xff) =>
            {
                if group7(bytes[at]).iter().any(|&f| advertised(f)) {
                    return None;
                }
                (at, rep_used, s.kind) = (at + 1, true, system::UD);
            }
            // I5b: XGETBV and XSETBV (`0f 01 d0`, `d1`; NP: with 66 not decoded).
            (MAP_TWO, 0x01) if matches!(*bytes.get(at)?, 0xd0 | 0xd1) && !operand16 => {
                s.kind = if bytes[at] == 0xd0 {
                    system::XGETBV
                } else {
                    system::XSETBV
                };
                at += 1;
            }
            (MAP_TWO, 0x01) => {
                let reg = rm(bytes, &mut at, rex, &mut s)?;
                s.kind = match reg {
                    0 => system::SGDT,
                    1 => system::SIDT,
                    2 => system::LGDT,
                    3 => system::LIDT,
                    7 => system::INVLPG,
                    // I10: RSTORSSP m64 (f3 0f 01 /5): "#UD If CR4.CET = 0" (CET not advertised); X1c: /5 m without F3 is blank in
                    // Table A-6 (reserved).
                    5 if s.memory == 1 => {
                        if rep == 0xf3 && advertised(gf::GF_CET_SS) {
                            return None;
                        }
                        rep_used = true;
                        system::UD
                    }
                    // X1c: SMSW r/m16, r32, r64 (`size`; a memory destination is always 16 bits) and LMSW r/m16.
                    // F2/F3 on SMSW (reserved, SDM Vol.2 2.1.1) are ignored, as on the reference AMD machine (owner rule; user-mode
                    // probe, November 3: every F2/F3 form took the UMIP #GP of plain SMSW, none #UD; plan §5).
                    4 => {
                        (s.size, rep_used) = (size, true);
                        system::SMSW
                    }
                    6 => system::LMSW,
                    _ => return None,
                };
                if (s.memory == 0 && matches!(s.kind, system::LGDT..=system::SIDT | system::INVLPG))
                    || (s.kind == system::INVLPG && matches!(segment, 0x64 | 0x65))
                {
                    return None;
                }
            }
            (MAP_TWO, 0x00) => {
                let reg = rm(bytes, &mut at, rex, &mut s)?;
                s.kind = [
                    system::SLDT,
                    system::STR,
                    system::LLDT,
                    system::LTR,
                    system::VERR,
                    system::VERW,
                    system::UD,
                    system::UD,
                ][reg as usize];
                // /6: LKGS (f2) is not advertised, and reserved without it; X1c: /7 is blank in Table A-6 (reserved).
                rep_used = s.kind == system::UD;
                s.size = size;
            }
            // S3: LAR/LSL r, r/m16 (destination ModRM.reg with REX.R).
            (MAP_TWO, 0x02 | 0x03) => {
                s.cr = rm(bytes, &mut at, rex, &mut s)? | r;
                s.kind = if opcode == 0x02 {
                    system::LAR
                } else {
                    system::LSL
                };
                s.size = size;
            }
            (MAP_TWO, 0x05) => s.kind = system::SYSCALL,
            // X1c: a Reserved-NOP cell fexc cut (its rule decides which): the ModRM decoded for the length, nothing accessed; any
            // 66/F2/F3 prefix. A register-operand 0f 0d is fexc's #UD (`cut_ud`), never decoded here.
            (MAP_TWO..=MAP_TWO_66, 0x0d | 0x18..=0x1d) => {
                rm(bytes, &mut at, rex, &mut s)?;
                (s.kind, rep_used) = (system::RESERVED_NOP, true);
                if opcode == 0x0d && s.memory == 0 {
                    return None;
                }
            }
            // X1c: PUSH FS/GS, POP FS/GS: 64-bit operand size by default, 16 with 66 (REX.W wins; no 32-bit form in 64-bit mode).
            (MAP_TWO, 0xa0 | 0xa1 | 0xa8 | 0xa9) => {
                s.kind = if opcode & 1 == 0 {
                    system::PUSH_SREG
                } else {
                    system::POP_SREG
                };
                s.cr = if opcode < 0xa8 { SREG_FS } else { SREG_GS } as u32;
                s.size = if operand16 && !w { 2 } else { 8 };
            }
            // X1c: CLTS; SYSENTER; SYSEXIT (REX.W: to 64-bit mode, else compatibility mode).
            (MAP_TWO, 0x06) => s.kind = system::CLTS,
            (MAP_TWO, 0x34) => s.kind = system::SYSENTER,
            (MAP_TWO, 0x35) => {
                s.kind = system::SYSEXIT;
                s.size = if w { 8 } else { 4 };
            }
            // X1c: LSS/LFS/LGS with a register operand: "an operand type that is invalid for its accompanying opcode" (SDM Vol.3
            // 7.15 interrupt 6, its example is LES); the memory forms are not built.
            (MAP_TWO, 0xb2 | 0xb4 | 0xb5) if *bytes.get(at)? >= 0xc0 => {
                (s.kind, rep_used) = (system::UD, true)
            }
            // X1c: PUSH ES/CS/SS/DS, POP ES/SS/DS and CALL/JMP ptr16:16/32 are "Invalid" in 64-bit mode (SDM Vol.2 PUSH, POP,
            // CALL, JMP opcode tables; PUSH: "#UD If the PUSH is of CS, SS, DS, or ES"; Table A-2 "i64").
            (MAP_PRIMARY, 0x06 | 0x07 | 0x0e | 0x16 | 0x17 | 0x1e | 0x1f | 0x9a | 0xea) => {
                (s.kind, rep_used) = (system::UD, true)
            }
            (MAP_TWO, 0x07) => {
                s.kind = system::SYSRET;
                s.size = far_size;
            }
            // INVD, WBINVD and (f3) WBNOINVD.
            (MAP_TWO, 0x08 | 0x09) => {
                s.kind = system::WBINVD;
                rep_used = opcode == 0x09 && rep == 0xf3;
            }
            // RDRAND (/6; F2/F3 #UD), RDSEED and RDPID (/7, f3): #UD while their feature is clear.
            (MAP_TWO, 0xc7) => {
                let reg = rm(bytes, &mut at, rex, &mut s)?;
                // I5b: XRSTORS, XSAVES m (/3, /5): #UD while XSAVES (CPUID.(EAX=0DH,ECX=1):EAX[3]) is clear. XSAVEC m (/4, NP; since
                // November 3, EAX[1]): the resident's; with 66/F2/F3 not decoded (a stop, as XSAVE's), #UD while not advertised.
                (s.kind, rep_used) = if s.memory != 0 && reg == 4 && advertised(gf::GF_XSAVEC) {
                    if operand16 || rep != 0 {
                        return None;
                    }
                    (system::XSAVEC, false)
                } else if s.memory != 0 && (3..=5).contains(&reg) {
                    if reg != 4 && advertised(gf::GF_XSAVES) {
                        return None;
                    }
                    (system::UD, true)
                } else if s.memory != 0 || reg < 6 {
                    return None; // cmpxchg8b/16b (admitted) and other memory forms are never cut
                } else if reg == 6 && rep == 0 && advertised(gf::GF_RDRAND) {
                    (system::RDRAND, false)
                } else if reg == 7
                    && ((rep == 0 && advertised(gf::GF_RDSEED))
                        || (rep == 0xf3 && advertised(gf::GF_RDPID)))
                {
                    return None; // advertised: built with the feature (none is)
                } else {
                    (system::UD, true)
                };
                s.size = size;
            }
            // VMREAD/VMWRITE outside VMX operation; INVEPT/INVVPID (likewise) and INVPCID (not advertised): #UD. 66/F2 0f 78/79
            // are AMD's SSE4a EXTRQ/INSERTQ (CPUID.80000001H:ECX.SSE4A[6] = 0 on the profile; blank in Intel's Table A-3): #UD.
            (MAP_TWO, 0x78 | 0x79) | (MAP_0F38, 0x80 | 0x81) if !advertised(gf::GF_VMX) => {
                (s.kind, rep_used) = (system::UD, true)
            }
            (MAP_0F38, 0x82) if !advertised(gf::GF_INVPCID) => {
                (s.kind, rep_used) = (system::UD, true)
            }
            (MAP_TWO_F3..=MAP_TWO_66, 0x78 | 0x79) if !advertised(gf::GF_SSE4A) => {
                (s.kind, rep_used) = (system::UD, true)
            }
            // I9: MOVBE with a register operand (SDM MOVBE: #UD); F2 is CRC32, F3 is refused by fexc and a memory MOVBE is
            // admitted, none ever cut. WRSS (NP 0f 38 f6) and WRUSS (66 0f 38 f5; CET_SS not advertised) and the rest of 0f 38
            // f5/f6: #UD whatever their operands; ADCX (66 0f 38 f6) and ADOX (F3) and SHA (NP 0f 38 c8..cd, NP 0f 3a cc) likewise
            // only while the profile does not advertise them (both advertised since November 3: fexc compiles them, no cut). With
            // ADX, 0f 38 f6 with F2 before F3 is cut but not decoded (a stop: the AMD machine runs it as ADOX; FEX does not decode
            // it); with F2 last (alone or with 66) it is a blank cell, #UD.
            (MAP_0F38, 0xf0 | 0xf1) => {
                rm(bytes, &mut at, rex, &mut s)?;
                if rep != 0 || s.memory != 0 {
                    return None;
                }
                (s.kind, rep_used) = (system::UD, true);
            }
            // (ADCX/ADOX, ADOX with 66 included, are never cut; cut with F3 last: F2 before F3, which the AMD machine runs as ADOX;
            // F2 last, alone or with 66, is #UD there.)
            (MAP_0F38, 0xf6)
                if advertised(gf::GF_ADX) && (rep == 0xf3 || (operand16 && rep == 0)) =>
            {
                return None;
            }
            // WRSS (NP f6) and WRUSS (66 f5): #UD while CET_SS is clear; the rest of f5/f6 (and ADCX/ADOX while ADX is clear) always.
            (MAP_0F38, 0xf5 | 0xf6) => {
                let wrss = rep == 0
                    && if opcode == 0xf6 {
                        !operand16
                    } else {
                        operand16
                    };
                if wrss && advertised(gf::GF_CET_SS) {
                    return None;
                }
                (s.kind, rep_used) = (system::UD, true)
            }
            (MAP_0F38, 0xc8..=0xcd) | (MAP_0F3A, 0xcc)
                if !operand16 && rep == 0 && !advertised(gf::GF_SHA) =>
            {
                s.kind = system::UD
            }
            (MAP_PRIMARY, 0x8c | 0x8e) => {
                s.cr = rm(bytes, &mut at, rex, &mut s)?;
                s.kind = if opcode == 0x8c {
                    system::MOV_FROM_SREG
                } else {
                    system::MOV_TO_SREG
                };
                s.size = size;
            }
            (MAP_PRIMARY, 0xff) => {
                s.kind = match rm(bytes, &mut at, rex, &mut s)? {
                    3 => system::CALL_FAR,
                    5 => system::JMP_FAR,
                    _ => return None,
                };
                // X1c: a register operand is "an operand type that is invalid for its accompanying opcode" (SDM Vol.3 7.15
                // interrupt 6; JMP/CALL far take m16:16/32/64 only): #UD whatever the operand size.
                if s.memory == 0 {
                    s.kind = system::UD;
                } else {
                    s.size = far_size;
                }
            }
            (MAP_PRIMARY, 0xca | 0xcb | 0xcf) => {
                s.kind = if opcode == 0xcf {
                    system::IRET
                } else {
                    system::RET_FAR
                };
                s.size = far_size;
                if opcode == 0xca {
                    s.disp = i64::from(u16::from_le_bytes(
                        bytes.get(at..at + 2)?.try_into().unwrap(),
                    ));
                    at += 2;
                }
            }
            // X1a: software interrupts and #UD instructions, delivered by the resident.
            (MAP_PRIMARY, 0xcc) => s.kind = system::INT3,
            (MAP_PRIMARY, 0xcd) => {
                s.kind = system::INT;
                s.cr = u32::from(*bytes.get(at)?);
                at += 1;
            }
            (MAP_PRIMARY, 0xce) => s.kind = system::INTO,
            // S4: INT1 (ICEBP), a #DB trap the resident delivers.
            (MAP_PRIMARY, 0xf1) => s.kind = system::INT1,
            // X2: CLI, STI, HLT; POPF pops 8 bytes in 64-bit mode (REX.W changes nothing), 2 with a 66 prefix.
            (MAP_PRIMARY, 0xfa) => s.kind = system::CLI,
            (MAP_PRIMARY, 0xfb) => s.kind = system::STI,
            (MAP_PRIMARY, 0xf4) => s.kind = system::HLT,
            (MAP_PRIMARY, 0x9d) => {
                s.kind = system::POPF;
                s.size = if operand16 && !w { 2 } else { 8 };
            }
            // L3: IN/OUT (`e4..e7` imm8, `ec..ef` DX; bit 1 OUT, bit 0 the wide form) and INS/OUTS (`6c..6f`). The wide forms
            // are 16-bit with 66 and 32-bit otherwise: REX.W does not promote port I/O (SDM Vol.2 IN/OUT/INS/OUTS: 8, 16 or 32
            // bits). Segment overrides and 0x67 do not apply to IN/OUT; INS writes ES:RDI (no override); OUTS reads DS:RSI with
            // an FS/GS override (CS/DS/ES/SS are ignored: DS-class whatever they are, as MOVS's source).
            // F3 is REP on INS/OUTS; F2 there (reserved, SDM Vol.2 REP) and F2/F3 on IN/OUT are not decoded.
            (MAP_PRIMARY, 0xe4..=0xe7 | 0xec..=0xef | 0x6c..=0x6f) => {
                let string = opcode < 0x70;
                s.kind = match (string, opcode & 2 != 0) {
                    (false, false) => system::IN,
                    (false, true) => system::OUT,
                    (true, false) => system::INS,
                    (true, true) => system::OUTS,
                };
                s.size = if opcode & 1 == 0 {
                    1
                } else if operand16 && !w {
                    2
                } else {
                    4
                };
                if string {
                    rep_used = rep == 0xf3;
                    s.prefixes = if rep_used { system::REP } else { 0 }
                        | if address_size { system::ADDRESS32 } else { 0 };
                    s.segment = match segment {
                        0x64 if s.kind == system::OUTS => SREG_FS as u32,
                        0x65 if s.kind == system::OUTS => SREG_GS as u32,
                        _ => 0,
                    };
                } else if opcode & 8 == 0 {
                    (s.reg, s.cr) = (system::NO_REG, u32::from(*bytes.get(at)?));
                    at += 1;
                } else {
                    s.reg = 2;
                }
            }
            (MAP_PRIMARY, 0xc8) => {
                s.kind = system::ENTER;
                s.size = if operand16 && !w { 2 } else { 8 };
                s.disp = i64::from(u16::from_le_bytes(bytes.get(at..at + 2)?.try_into().ok()?));
                s.cr = u32::from(*bytes.get(at + 2)? & 31);
                at += 3;
            }
            (MAP_PRIMARY, 0xc0 | 0xc1 | 0xd2 | 0xd3) => {
                let op = rm(bytes, &mut at, rex, &mut s)?;
                if s.memory == 0 || !matches!(op, 2 | 3) {
                    return None;
                }
                s.kind = system::ROTATE;
                s.size = if opcode & 1 == 0 { 1 } else { size };
                s.cr = op - 2;
                if opcode < 0xd0 {
                    s.reg = system::NO_REG;
                    s.cr |= u32::from(*bytes.get(at)?) << 1;
                    at += 1;
                } else {
                    s.reg = 1;
                }
            }
            (MAP_TWO, 0xa5 | 0xad) if size == 2 => {
                let source = rm(bytes, &mut at, rex, &mut s)? | r;
                (s.kind, s.cr, s.size) = (
                    system::DOUBLE_SHIFT16,
                    source | if opcode == 0xad { 16 } else { 0 },
                    2,
                );
            }
            // X2: BT/BTS/BTR/BTC with a memory operand, cut only under alignment checking (fexc --alignment-gated): `cr` names
            // the operation, `reg` the bit-offset register (NO_REG for the imm8 form, its immediate skipped), `size` the operand.
            (MAP_TWO, 0xa3 | 0xab | 0xb3 | 0xbb) => {
                let bit = rm(bytes, &mut at, rex, &mut s)? | r;
                (s.kind, s.cr, s.reg, s.size) =
                    (system::BT_ALIGNED, u32::from((opcode >> 3) & 3), bit, size);
                if s.memory == 0 {
                    return None;
                }
            }
            (MAP_TWO, 0xba) => {
                let op = rm(bytes, &mut at, rex, &mut s)?;
                bytes.get(at)?;
                at += 1;
                if s.memory == 0 || op < 4 {
                    return None;
                }
                (s.kind, s.cr, s.reg, s.size) = (system::BT_ALIGNED, op - 4, system::NO_REG, size);
            }
            // UD1/UD0's ModRM is not decoded: #UD leaves RIP at the instruction, its length is never used.
            (MAP_TWO, 0x0b | 0xb9 | 0xff) => s.kind = system::UD,
            // I5a: LDMXCSR/STMXCSR m32 (`0f ae /2`, `/3`); with a 66 prefix or a register operand another instruction. S3:
            // CLFLUSH m8 (`/7`; since November 3 with 66 CLFLUSHOPT and `/6` with 66 CLWB, both advertised: the same transaction,
            // the faults of a byte load, SDM Vol.2 each; CLFLUSHOPT with F2/F3 #UD); f3 with a register operand RDFSBASE, RDGSBASE,
            // WRFSBASE, WRGSBASE (`/0../3`), whose register forms without f3 are #UD.
            // I6: the x87 forms the resident executes: FNINIT, FNCLEX (`db e3`, `db e2`), FLDCW m16 (`d9 /5`), FLDENV/FNSTENV m
            // (`d9 /4`, `/6`) and FRSTOR/FNSAVE m (`dd /4`, `/6`), whose image is the 16-bit one with a 66 prefix (`size` 2, else 4),
            // FLD/FSTP m80 (`db /5`, `/7`).
            (MAP_PRIMARY, 0xdb) if matches!(*bytes.get(at)?, 0xe2 | 0xe3) => {
                s.kind = if bytes[at] == 0xe3 {
                    system::FNINIT
                } else {
                    system::FNCLEX
                };
                at += 1;
            }
            (MAP_PRIMARY, 0xd9 | 0xdb | 0xdd) => {
                let reg = rm(bytes, &mut at, rex, &mut s)?;
                s.kind = match (opcode, reg) {
                    (0xd9, 4) => system::FLDENV,
                    (0xd9, 5) => system::FLDCW,
                    (0xd9, 6) => system::FNSTENV,
                    (0xdd, 4) => system::FRSTOR,
                    (0xdd, 6) => system::FNSAVE,
                    (0xdb, 5) => system::FLD80,
                    (0xdb, 7) => system::FSTP80,
                    _ => return None,
                };
                // REX.W wins over 66 (the 64-bit operand size): the 28/108-byte image.
                s.size = if operand16 && !w { 2 } else { 4 };
                if s.memory == 0 {
                    return None;
                }
            }
            // The gathers' VSIB operand (SDM Vol.2 2.3.12): a SIB byte whose index (with VEX.X) names a vector register, 100b
            // included; base, scale and displacement as any SIB form. Without a SIB byte or with a register operand fexc cuts it
            // as #UD (`cut_ud`), never decoded here. Map 0F38 only (the VEX arm above admits these opcodes nowhere else).
            (MAP_VEX, 0x90..=0x93) => {
                let (modrm, sib) = (*bytes.get(at)?, *bytes.get(at + 1)?);
                if modrm >> 6 == 3 || modrm & 7 != 4 {
                    return None;
                }
                rm(bytes, &mut at, rex, &mut s)?;
                let (b1, p) = vex;
                s.kind = system::GATHER;
                s.reg = u32::from(modrm >> 3 & 7) | u32::from(!b1 >> 7 & 1) << 3;
                s.index = u32::from(sib >> 3 & 7) | u32::from(rex >> 1 & 1) << 3;
                s.cr = u32::from(!p >> 3 & 15)
                    | u32::from(p >> 2 & 1) << 4
                    | u32::from(opcode & 1) << 5;
                s.size = if p & 0x80 != 0 { 8 } else { 4 };
            }
            // The masked moves ([`system::MASKMOV`]). VMASKMOVPS/PD and VPMASKMOVD/Q: data ModRM.reg (VEX.R), mask VEX.vvvv, memory
            // ModRM.rm (fexc's #UD for a register operand and for VEX.W outside the PS/PD forms' W0); loads 2c/2d/8c, stores
            // 2e/2f/8e; elements of 4 bytes (PS, D: VEX.W 0) or 8 (PD, Q: VEX.W 1). VMASKMOVDQU (VEX.128, vvvv 1111b: fexc's #UD
            // otherwise): data ModRM.reg, mask ModRM.rm (a register), memory DS:RDI.
            (MAP_VEX, 0x1d) => {
                let (b1, p) = vex;
                if p & 0xf8 != 0x78 {
                    return None;
                }
                let source = rm(bytes, &mut at, rex, &mut s)? | u32::from(!b1 >> 7 & 1) << 3;
                if s.memory != 1 {
                    return None;
                }
                (s.kind, s.reg, s.cr, s.size) = (
                    system::CVTPS2PH,
                    source,
                    u32::from(*bytes.get(at)?),
                    8 << (p >> 2 & 1),
                );
                at += 1;
            }
            (MAP_VEX, 0x2c..=0x2f | 0x8c | 0x8e | 0xf7) => {
                let (b1, p) = vex;
                let modrm = *bytes.get(at)?;
                rm(bytes, &mut at, rex, &mut s)?;
                let data = u32::from(modrm >> 3 & 7) | u32::from(!b1 >> 7 & 1) << 3;
                if opcode == 0xf7 {
                    if s.memory != 0 || p & 0x7c != 0x78 {
                        return None;
                    }
                    let mask = s.reg;
                    (s.memory, s.base, s.reg, s.cr, s.size) = (1, 7, data, mask | 32 | 64, 1);
                } else {
                    if s.memory == 0 {
                        return None;
                    }
                    let store = u32::from(opcode & 2 != 0);
                    let wide = if opcode < 0x80 {
                        opcode & 1 != 0
                    } else {
                        p & 0x80 != 0
                    };
                    s.cr = u32::from(!p >> 3 & 15) | u32::from(p >> 2 & 1) << 4 | store << 5 | 64;
                    (s.reg, s.size) = (data, if wide { 8 } else { 4 });
                }
                s.kind = system::MASKMOV;
            }
            // MASKMOVDQU (66 0f f7, register operands only: a memory operand is the host's #UD, [`undefined`]): data ModRM.reg
            // (REX.R), mask ModRM.rm (REX.B), memory DS:RDI; F2/F3 with it is not decoded.
            (MAP_TWO, 0xf7) if !operand16 => {
                // MMX has only eight registers; REX.R/B do not extend their ModRM fields (SDM Vol.2A 2.2.1.2).
                let data = rm(bytes, &mut at, 0, &mut s)?;
                if s.memory != 0 {
                    return None;
                }
                let mask = s.reg;
                (s.memory, s.base, s.reg, s.cr, s.size, s.kind) =
                    (1, 7, data, mask | 32 | 128, 1, system::MASKMOV);
            }
            (MAP_TWO | MAP_TWO_66, 0xf7) if operand16 => {
                let data = rm(bytes, &mut at, rex, &mut s)? | r;
                if s.memory != 0 {
                    return None;
                }
                let mask = s.reg;
                (s.memory, s.base, s.reg, s.cr, s.size, s.kind) =
                    (1, 7, data, mask | 32, 1, system::MASKMOV);
            }
            (MAP_VEX, 0xae) => {
                let reg = rm(bytes, &mut at, rex, &mut s)?;
                s.kind = match (s.memory, reg) {
                    (1, 2) => system::VLDMXCSR,
                    (1, 3) => system::VSTMXCSR,
                    _ => return None,
                };
            }
            (MAP_TWO, 0xae) => {
                let reg = rm(bytes, &mut at, rex, &mut s)?;
                s.kind = match (s.memory, reg) {
                    // X1c: PTWRITE r/m (F3 /4): "#UD If CPUID.(EAX=14H, ECX=0):EBX.PTWRITE[bit 4] = 0"; leaf 14H is Intel PT's
                    // (CPUID.07H:EBX.PT, not advertised).
                    (_, 4) if rep == 0xf3 && !operand16 && !advertised(gf::GF_PT) => system::UD,
                    (1, 2) if !operand16 => system::LDMXCSR,
                    (1, 3) if !operand16 => system::STMXCSR,
                    // I5b: FXSAVE, FXRSTOR, XSAVE, XRSTOR (NP); /6 is XSAVEOPT (CPUID.(EAX=0DH,ECX=1):EAX.XSAVEOPT, since November 3:
                    // XSAVE's transaction, which is what it stores with XINUSE and XMODIFIED all ones, SDM Vol.1 13.9) or with 66
                    // CLWB (CPUID.7.0:EBX.CLWB; with F2/F3 #UD here: F3 is CLRSSBSY, CET not advertised).
                    (1, 0) if !operand16 => system::FXSAVE,
                    (1, 1) if !operand16 => system::FXRSTOR,
                    (1, 4) if !operand16 => system::XSAVE,
                    (1, 5) if !operand16 => system::XRSTOR,
                    (1, 6) if operand16 && rep == 0 && advertised(gf::GF_CLWB) => system::CLFLUSH,
                    (1, 6) if !operand16 && rep == 0 && advertised(gf::GF_XSAVEOPT) => {
                        system::XSAVE
                    }
                    (1, 6) if !operand16 && rep == 0xf3 && advertised(gf::GF_CET_SS) => {
                        return None;
                    } // CLRSSBSY
                    (1, 6) => system::UD,
                    (1, 7) if operand16 && (rep != 0 || !advertised(gf::GF_CLFLUSHOPT)) => {
                        system::UD
                    }
                    (1, 7) if operand16 => system::CLFLUSH,
                    (1, 7) => system::CLFLUSH,
                    (0, 0..=3) if operand16 => return None, // 66 there: no SDM encoding (a stop, not a guess)
                    (0, 0..=3) if rep == 0xf3 && advertised(gf::GF_FSGSBASE) => {
                        rep_used = true;
                        s.size = if w { 8 } else { 4 };
                        [
                            system::RDFSBASE,
                            system::RDGSBASE,
                            system::WRFSBASE,
                            system::WRGSBASE,
                        ][reg as usize]
                    }
                    (0, 0..=3) => system::UD,
                    _ => return None,
                };
                rep_used |= s.kind == system::UD;
            }
            _ => return None,
        }
        // A UD form's operands are never used (the fault leaves RIP at the instruction).
        if rep != 0 && !rep_used {
            return None;
        }
        if s.kind == system::UD {
            s = System {
                kind: system::UD,
                lock: s.lock,
                ..System::NONE
            };
        }
        if s.memory == 1 {
            // I10: 0x67 makes the effective address 32 bits (truncated, zero-extended, then the FS/GS base: `segments::address`).
            if address_size {
                s.prefixes |= system::ADDRESS32;
            }
            // The FS/GS override adds its base; with none the default segment decides the class (SS for an RSP/RBP base, as
            // fexc's sites: a BT* form found aligned under alignment checking is stepped compiled by it).
            s.segment = match segment {
                0x64 => SREG_FS as u32,
                0x65 => SREG_GS as u32,
                _ => 0,
            };
        }
        s.length = at as u32;
        s.well_formed().then_some(s)
    }

    /// A system cut [`decode`] does not decode, whose legacy prefixes include LOCK: #UD (I3). fexc cuts every LOCK on an
    /// instruction that is not lockable as a system instruction, and LOCK on a system instruction is #UD too (SDM Vol.2
    /// "LOCK"), so no operand needs decoding.
    pub(crate) fn lock_ud(bytes: &[u8]) -> Option<System> {
        let prefixes = bytes[..opcode_at(bytes).0].iter();
        prefixes.take(14).any(|&b| b == 0xf0).then_some(System {
            kind: system::LOCK_UD,
            lock: 1,
            length: 1,
            ..System::NONE
        })
    }

    /// I8: a VEX prefix (C4/C5, 64-bit mode) after a 66, F2 or F3 prefix, or right after a REX: #UD (SDM Vol.2 2.3.2), whatever
    /// the instruction and its gating (fexc cuts it as a system instruction).
    pub(crate) fn vex_ud(bytes: &[u8]) -> Option<System> {
        let (mut legacy, mut rex) = (false, false);
        for &b in bytes.iter().take(15) {
            match b {
                0xc4 | 0xc5 => {
                    return (legacy || rex).then_some(System {
                        kind: system::UD,
                        length: 1,
                        ..System::NONE
                    });
                }
                0x40..=0x4f => rex = true,
                0x66 | 0xf2 | 0xf3 => (legacy, rex) = (true, false),
                0xf0 | 0x67 | 0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 => rex = false,
                _ => return None,
            }
        }
        None
    }

    /// The catalogue features that define the group-7 register forms (`0f 01 /r`, mod = 3) it knows, by ModRM byte and
    /// whatever the mandatory prefix (the union over the prefixes): the form is #UD while all of them are clear. The forms of
    /// no catalogue feature (PCONFIG, WRMSRNS, PBNDKB, the SVM group, MONITORX..INVLPGB, TDX, FRED, blank cells) are #UD
    /// whatever the profile.
    fn group7(modrm: u8) -> &'static [Feature] {
        match modrm {
            0xc0 | 0xcf | 0xd7 => &[gf::GF_SGX], // ENCLV, ENCLS, ENCLU
            0xc1..=0xc4 | 0xd4 => &[gf::GF_VMX], // VMCALL, VMLAUNCH, VMRESUME, VMXOFF; VMFUNC
            0xc8 | 0xc9 => &[gf::GF_MONITOR],    // MONITOR, MWAIT
            0xca | 0xcb => &[gf::GF_SMAP],       // CLAC, STAC
            0xd5 => &[gf::GF_RTM],               // XEND
            0xd6 => &[gf::GF_RTM, gf::GF_HLE],   // XTEST
            0xe8 => &[gf::GF_SERIALIZE, gf::GF_TSXLDTRK, gf::GF_CET_SS], // SERIALIZE (NP), XSUSLDTRK (F2), SETSSBSY (F3)
            0xe9 => &[gf::GF_TSXLDTRK],                                  // XRESLDTRK
            0xea => &[gf::GF_CET_SS],                                    // SAVEPREVSSP
            0xec | 0xed => &[gf::GF_UINTR],                              // UIRET, TESTUI (F3)
            0xee | 0xef => &[gf::GF_PKU, gf::GF_UINTR], // RDPKRU, WRPKRU; CLUI, STUI (F3)
            _ => &[],
        }
    }
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
        let kind = |hex: &str| super::decode(&b(hex), 64).map(|s| s.kind);
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
        let d = |hex: &str| super::decode(&b(hex), 64).map(|s| (s.kind, s.length));
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
        let d = |hex: &str| super::decode(&b(hex), 64).map(|s| (s.kind, s.cr, s.size, s.length));
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
                super::decode(bytes, 64)
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
            let s = super::decode(bytes, 64).unwrap();
            assert!(s.well_formed() && s.kind == super::system::UD, "{bytes:x?}");
        }
        // No LOCK, or an f0 byte after the opcode (a ModRM or immediate) is no prefix: no system instruction.
        for bytes in [&[0x89, 0x03][..], &[0x48, 0x89, 0xf0], &[0xb0, 0xf0], &[]] {
            assert!(super::decode(bytes, 64).is_none(), "{bytes:x?}");
        }
    }
}

/// Perf chunk 12: the number of instructions in `bytes` when it is wholly a run of no-operation instructions
/// ([`system::NOP_RUN`]): NOP (`90`, with 66 prefixes), NOP r/m (`0f 1f /0`, register or memory: never accessed, SDM Vol.2
/// NOP) after 66, CS/DS/ES/SS (ignored in 64-bit mode) and REX prefixes, and ENDBR64/ENDBR32 (`f3 0f 1e fa`/`fb`; the
/// profile has no CET, so they execute as NOPs, SDM Vol.1 18.3.3), each at most 15 bytes. Anything else (LOCK, F2/F3,
/// 0x67, FS/GS, PAUSE, `41 90` = XCHG R8, EAX, a ModRM.reg other than 0) is `None`. 64-bit mode only (REX bytes are REX:
/// the resident runs no other mode, `Cpu::control`).
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

    /// The 32- and 16-bit decode (nothing runs it yet): operand and address sizes, the segment an override names (not
    /// ignored outside 64-bit mode) and 16-bit address forms, the segment loads only those modes have.
    #[test]
    fn other_modes() {
        let at = |bits, hex: &str| {
            let bytes: Vec<u8> = (0..hex.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
                .collect();
            decode(&[bytes, vec![0x90; 4]].concat(), bits)
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
        assert_eq!(at(32, "0f0110"), Some(m32(LGDT, 0, 0, 3)));
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
        decode(&[bytes, vec![0x90; 4]].concat(), 64)
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
        decode(&[bytes, vec![0x90; 4]].concat(), 64)
    }
    /// A memory operand in its default segment (SS for an RSP or RBP base, else DS).
    fn mem(kind: u32, base: u32, disp: i64, length: u32) -> System {
        System {
            kind,
            base,
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
            let i = decode(&bytes, 64).unwrap();
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
            assert!(refused(decode(&bytes, 64)));
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
            decode(&[bytes, vec![0x90; 4]].concat(), 64)
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
        assert_eq!(super::decode(&[0xcc], 64), s(INT3, 0, 1));
        assert_eq!(super::decode(&[0xf1], 64), s(INT1, 0, 1));
        assert_eq!(super::decode(&[0xcd, 0x80], 64), s(INT, 0x80, 2));
        assert_eq!(super::decode(&[0xcd], 64), None); // truncated immediate
        assert_eq!(super::decode(&[0xce], 64), s(UD, 0, 1)); // INTO: no 64-bit form
        assert_eq!(super::decode(&[0x0f, 0x0b], 64), s(UD, 0, 2));
        assert_eq!(super::decode(&[0x0f, 0xb9, 0xc0], 64), s(UD, 0, 3));
        assert_eq!(
            super::decode(&[0xf0, 0xcc], 64).map(|s| (s.kind, s.lock)),
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
        assert_eq!(super::decode(&[0xfa], 64), s(CLI, 0, 1));
        assert_eq!(super::decode(&[0xfb], 64), s(STI, 0, 1));
        assert_eq!(super::decode(&[0xf4], 64), s(HLT, 0, 1));
        assert_eq!(super::decode(&[0x9d], 64), s(POPF, 8, 1));
        assert_eq!(super::decode(&[0x48, 0x9d], 64), s(POPF, 8, 2));
        assert_eq!(super::decode(&[0x66, 0x9d], 64), s(POPF, 2, 2));
        assert_eq!(super::decode(&[0x66, 0x48, 0x9d], 64), s(POPF, 8, 3)); // REX.W wins
        assert_eq!(
            super::decode(&[0xf0, 0xfa], 64).map(|s| (s.kind, s.lock)),
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
            decode(&bytes, 64)
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
        let w = super::decode(&[0x48, 0x0f, 0xab, 0x0b], 64).unwrap();
        let h = super::decode(&[0x66, 0x0f, 0xab, 0x0b], 64).unwrap();
        assert_eq!(
            (w.size, h.size, w.well_formed(), h.well_formed()),
            (8, 2, true, true)
        );
        // CS/DS/ES/SS overrides are ignored in 64-bit mode (the default segment decides the class); FS/GS add their base,
        // the last of several applies, a null prefix before or after it changes nothing (owner decisions, November 3).
        for prefix in [0x26, 0x2e, 0x36, 0x3e] {
            let p = super::decode(&[prefix, 0x0f, 0xa3, 0x4b, 0x01], 64).unwrap();
            assert_eq!(
                (p.kind, p.segment, p.disp, p.length),
                (BT_ALIGNED, SREG_DS as u32, 1, 5)
            );
        }
        assert_eq!(
            super::decode(&[0x64, 0x0f, 0xa3, 0x4b, 0x01], 64)
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
            decode(&[bytes, vec![0x90; 4]].concat(), 64)
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
            let got = decode(&bytes(hex), 64);
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
            let s = decode(&bytes(hex), 64).unwrap_or_else(|| panic!("{hex}"));
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
        let mm = decode(&bytes("0ff7ca"), 64).unwrap();
        assert!(mm.well_formed());
        for cr in [128, 160 | 8, 160 | 16, 160 | 64, 160 | 256] {
            assert!(!System { cr, ..mm }.well_formed());
        }
        assert!(!System { reg: 8, ..mm }.well_formed());
        assert!(!System { size: 8, ..mm }.well_formed());
        // A register operand of the VEX.0F38 forms, a memory one of MASKMOVDQU, VMASKMOVDQU with VEX.L = 1: not decoded.
        for hex in ["0ff700", "c4e2752cc0", "660ff700", "c5fdf7ca"] {
            assert!(refused(decode(&bytes(hex), 64)), "{hex}");
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
        let kind = |hex: &str| decode(&bytes(hex), 64).map(|s| s.kind);
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
        let sys = |hex: &str| {
            decode(&bytes(hex), 64).map(|s| (s.kind, s.size, s.memory, s.reg, s.length))
        };
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
        assert_eq!(super::decode(&[0xe4], 64), None); // truncated imm8
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
            super::decode(&[0x66, 0x0f, 0x78, 0xc0, 0x00, 0x00], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::decode(&[0x66, 0x41, 0x0f, 0x79, 0xc0], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::decode(&[0xf2, 0x0f, 0x78, 0xc1, 0x00, 0x3f], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::decode(&[0xf2, 0x41, 0x0f, 0x79, 0xc0], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::decode(&[0x66, 0x0f, 0x38, 0x82, 0x00, 0x90], 64).map(|s| s.kind),
            Some(UD)
        ); // invpcid
        // I9: MOVBE register forms, WRUSS: #UD; a memory MOVBE and CRC32 are never decoded here. ADCX/ADOX and SHA: #UD
        // exactly while the profile does not advertise them.
        assert_eq!(
            super::decode(&[0x48, 0x0f, 0x38, 0xf0, 0xc1], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(
            super::decode(&[0x66, 0x0f, 0x38, 0xf1, 0xc8], 64).map(|s| s.kind),
            Some(UD)
        );
        assert_eq!(super::decode(&[0x0f, 0x38, 0xf0, 0x07], 64), None);
        assert_eq!(super::decode(&[0xf2, 0x0f, 0x38, 0xf0, 0xc1], 64), None);
        let adx = (!super::advertised(super::gf::GF_ADX)).then_some(UD);
        assert_eq!(
            super::decode(&[0x66, 0x48, 0x0f, 0x38, 0xf6, 0xc1], 64).map(|s| s.kind),
            adx
        );
        assert_eq!(
            super::decode(&[0xf3, 0x48, 0x0f, 0x38, 0xf6, 0x07], 64).map(|s| s.kind),
            adx
        );
        assert_eq!(
            super::decode(&[0x66, 0xf3, 0x0f, 0x38, 0xf6, 0xc1], 64).map(|s| s.kind),
            adx
        );
        assert_eq!(
            super::decode(&[0x0f, 0x38, 0xf6, 0xc1], 64).map(|s| s.kind),
            Some(UD)
        ); // wrss (NP; CET_SS not advertised)
        assert_eq!(
            super::decode(&[0xf2, 0x0f, 0x38, 0xf6, 0xc1], 64).map(|s| s.kind),
            Some(UD)
        ); // a blank cell
        assert_eq!(
            super::decode(&[0x66, 0xf2, 0x0f, 0x38, 0xf6, 0xc1], 64).map(|s| s.kind),
            Some(UD)
        ); // #UD on the AMD machine too
        let sha = (!super::advertised(super::gf::GF_SHA)).then_some(UD);
        assert_eq!(
            super::decode(&[0x0f, 0x38, 0xcb, 0xc1], 64).map(|s| s.kind),
            sha
        );
        assert_eq!(
            super::decode(&[0x0f, 0x3a, 0xcc, 0xc1, 0x01], 64).map(|s| s.kind),
            sha
        );
        assert!(refused(super::decode(
            &[0x66, 0x0f, 0x3a, 0xcc, 0xc1, 0x01],
            64
        )));
        assert_eq!(d("f00f01f8").map(|s| (s.kind, s.lock)), Some((UD, 0))); // #UD by LOCK
    }
}

/// Temporary: the iced-x86 decode against the hand-written one on every 64-bit encoding of a prefix set, an opcode and
/// a ModRM; the outcomes compared as the resident sees them.
#[cfg(test)]
mod differential {
    use super::*;
    use natemu_abi::execution::system::*;
    use natemu_abi::execution::{SREG_DS, SREG_SS};

    fn old(b: &[u8]) -> Option<System> {
        let (at, _) = opcode_at(b);
        let (maps, op): (&[u8], u8) = match b.get(at..)? {
            [0xc4, _, _, op, ..] => (&[8], *op),
            [0xc5, _, op, ..] => (&[8], *op),
            [0x0f, 0x38, op, ..] => (&[5], *op),
            [0x0f, 0x3a, op, ..] => (&[6], *op),
            [0x0f, op, ..] => (&[1, 2, 3, 4], *op),
            [op, ..] => (&[0], *op),
            [] => return None,
        };
        maps.iter()
            .find_map(|&m| super::old::decode(b, m, op))
            .or_else(|| super::old::lock_ud(b))
            .or_else(|| super::old::vex_ud(b))
    }
    /// #UD at the instruction whatever the state (`execution::transaction`'s first checks; `None` is `Code::refused`).
    fn ud_like(s: &Option<System>) -> bool {
        let Some(s) = s else { return true };
        matches!(s.kind, UD | LOCK_UD | INTO)
            || (s.lock == 1 && !(s.kind == BT_ALIGNED && s.cr != 0))
            || (matches!(s.kind, MOV_TO_CR | MOV_FROM_CR) && !matches!(s.cr, 0 | 2 | 3 | 4 | 8))
            || (matches!(s.kind, MOV_TO_DR | MOV_FROM_DR) && s.cr > 7)
            || (matches!(s.kind, MOV_TO_SREG | MOV_FROM_SREG)
                && (s.cr > 5 || (s.kind == MOV_TO_SREG && s.cr == 1)))
    }
    /// The old record in the widened encoding: the default segment named.
    fn widen(mut s: System) -> System {
        if (s.memory == 1 || s.kind == OUTS) && s.segment == 0 {
            s.segment = if s.memory == 1 && matches!(s.base, 4 | 5) {
                SREG_SS
            } else {
                SREG_DS
            } as u32;
        }
        if s.index == NO_REG {
            s.scale = 0;
        }
        // An operand size the resident does not read (`x87`, `xsave`): the hand-written decode filled it in.
        if matches!(s.kind, FLDCW | FLD80 | FSTP80 | XSAVEC) {
            s.size = 0;
        }
        s
    }

    #[test]
    fn iced_against_hand_written() {
        let hex = |h: &str| -> Vec<u8> {
            h.split_whitespace()
                .map(|x| u8::from_str_radix(x, 16).unwrap())
                .collect()
        };
        let prefixes: Vec<Vec<u8>> = [
            "", "66", "f2", "f3", "f0", "67", "2e", "36", "26", "3e", "64", "65", "48", "41", "44",
            "42", "4c", "4f", "40", "66 48", "48 66", "f3 48", "f2 48", "48 f3", "66 f3", "f3 66",
            "66 f2", "f2 66", "f2 f3", "f3 f2", "64 65", "65 64", "65 2e", "2e 65", "f0 48",
            "67 64", "f3 67", "66 67", "f3 66 48", "36 2e",
        ]
        .iter()
        .map(|p| hex(p))
        .collect();
        let tails = [
            hex("24 11 22 33 44 55 66 77 88 99 aa bb"),
            hex("8d f0 ff ff ff 80 01 02 03 04 05 06"),
            hex("65 08 00 00 80 ff 01 02 03 04 05 06"),
        ];
        let mut bodies: Vec<(Vec<u8>, Vec<u8>)> = Vec::new(); // (prefix set, opcode bytes); ModRM and tail appended
        for p in &prefixes {
            for op in 0..=255u8 {
                for esc in [&[][..], &[0x0f], &[0x0f, 0x38], &[0x0f, 0x3a]] {
                    bodies.push((p.clone(), [esc, &[op]].concat()));
                }
            }
        }
        let vex_ops = [
            0xae, 0xf7, 0x90, 0x91, 0x92, 0x93, 0x2c, 0x2d, 0x2e, 0x2f, 0x8c, 0x8e, 0x1d, 0x77,
            0x6f,
        ];
        for p in ["", "66", "f3", "48", "64", "67", "f0"] {
            for b1 in 0..=255u8 {
                for &op in &vex_ops {
                    bodies.push((hex(p), vec![0xc5, b1, op]));
                    for b0 in [
                        0xe1u8, 0xe2, 0xe3, 0xc1, 0xc2, 0xc3, 0xa2, 0x62, 0x42, 0x22, 0x02, 0xe0,
                        0xe4,
                    ] {
                        bodies.push((hex(p), vec![0xc4, b0, b1, op]));
                    }
                }
            }
        }
        let modrms: Vec<u8> = (0..=255).collect();
        let vex_modrms = [
            0x00u8, 0x04, 0x05, 0x0c, 0x44, 0x84, 0xc1, 0xca, 0xd0, 0x10, 0x18, 0x3c, 0x8f, 0x2c,
            0x1c, 0x5c,
        ];
        let (mut n, mut agree, mut ud, mut diag) = (0u64, 0u64, 0u64, 0u64);
        let mut differ: std::collections::BTreeMap<String, (u64, Vec<String>)> = Default::default();
        for (p, body) in &bodies {
            let vex = matches!(body[0], 0xc4 | 0xc5);
            for &m in if vex { &vex_modrms[..] } else { &modrms[..] } {
                for t in &tails {
                    let b = [&p[..], body, &[m], t].concat();
                    n += 1;
                    let (o, i) = (old(&b), decode(&b, 64));
                    if ud_like(&o) && ud_like(&i) {
                        ud += 1;
                        diag += u64::from(o.is_none() != i.is_none());
                        continue;
                    }
                    if o.map(widen) == i {
                        agree += 1;
                        continue;
                    }
                    let h: String = b.iter().take(16).map(|x| format!("{x:02x}")).collect();
                    let (key, example) = match (o.map(widen), i) {
                        (Some(o), Some(i)) => {
                            let (a, b) = (format!("{o:?}"), format!("{i:?}"));
                            let fields: Vec<String> = a
                                .split(", ")
                                .zip(b.split(", "))
                                .filter(|(x, y)| x != y)
                                .map(|(x, y)| format!("{x}->{}", y.rsplit(": ").next().unwrap()))
                                .collect();
                            (format!("{}->{}", o.kind, i.kind), fields.join(" "))
                        }
                        (o, i) => (
                            format!("{:?}->{:?}", o.map(|s| s.kind), i.map(|s| s.kind)),
                            format!("{o:?} {i:?}"),
                        ),
                    };
                    let e = differ.entry(key).or_default();
                    e.0 += 1;
                    if e.1.len() < 1 {
                        e.1.push(format!("{h} {example}"));
                    }
                }
            }
        }
        let mut rows: Vec<_> = differ.iter().collect();
        rows.sort_by_key(|r| std::cmp::Reverse(r.1.0));
        let mut report = String::new();
        for (k, (c, ex)) in rows.iter().take(34) {
            report += &format!(
                "{k}: {c} e.g. {}\n",
                ex[0].chars().take(300).collect::<String>()
            );
        }
        report += &format!(
            "{} categories; {n} encodings: {agree} same record, {ud} #UD both ({diag} of them None on one side)\n",
            differ.len()
        );
        // VMASKMOVPS/PD with VEX.W 1: the hand-written decode took it, Intel defines only W0 (SDM Vol.2 VMASKMOV: VEX.W0),
        // iced finds no instruction (#UD); fexc cuts it as #UD before any decode (`cut_ud`).
        let accepted = ["124->30"];
        assert!(
            differ.keys().all(|k| accepted.contains(&k.as_str())),
            "{report}"
        );
    }
}
