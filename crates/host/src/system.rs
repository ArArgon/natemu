//! System instructions the resident executes as transactions (`natemu_abi::execution::System`): operand decoding of
//! the instruction fexc cut a block before. FEX's decoder classifies the instruction (fexc's `system_instruction` cut
//! with its opcode map and opcode); it does not decode the ModRM of `0f 20..23` (two bytes there), so the operands of
//! the supported forms are decoded here and must agree with FEX's opcode: MOV to/from CR0..CR15 (`0f 22`/`0f 20`) and
//! DR0..DR15 (`0f 23`/`0f 21`; ModRM always a register pair), INVLPG m (`0f 01 /7`, mod != 3), RDMSR (`0f 32`), WRMSR
//! (`0f 30`), RDPMC (`0f 33`), CPUID (`0f a2`), RDTSC (`0f 31`) and RDTSCP (`0f 01 f9`); the descriptor-table forms
//! SGDT/SIDT/LGDT/LIDT m (`0f 01 /0../3`, mod != 3), SLDT/STR/LLDT/LTR r/m (`0f 00 /0../3`), MOV r/m, Sreg (`8c`) and
//! MOV Sreg, r/m (`8e`), JMP/CALL far m16:32 or m16:64 (`ff /5`, `ff /3`, memory), RET far (`cb`, `ca iw`) and IRET (`cf`),
//! with 32- or 64-bit operand size (REX.W) where it matters, in 64-bit mode; S3: SWAPGS (`0f 01 f8`), SYSCALL (`0f 05`),
//! SYSRET (`0f 07`), RDFSBASE..WRGSBASE (`f3 0f ae /0../3`, register), LAR/LSL (`0f 02`/`0f 03`), VERR/VERW (`0f 00 /4`,
//! `/5`), RDRAND (`0f c7 /6`, register), INVD/WBINVD/WBNOINVD (`0f 08`, `0f 09`, `f3 0f 09`), CLFLUSH m (`0f ae /7`); L3:
//! IN/OUT (`e4..e7`, `ec..ef`) and INS/OUTS (`6c..6f`; F3 = REP, 0x67); I5b: FXSAVE/FXRSTOR/XSAVE/XRSTOR m (`0f ae /0 /1 /4
//! /5`), XGETBV/XSETBV (`0f 01 d0`, `d1`); and forms the presented CPU raises #UD for whatever their operands (unadvertised or VMX/SVM-only instructions). Anything
//! else (other system instructions, F2/F3 prefixes on other forms, an address-size prefix on a memory operand, a 66 prefix on a far transfer, segment
//! prefixes of different classes or an FS/GS override on INVLPG) is `None`: the vCPU stops with fexc's reason, as for code it cannot
//! compile.
use natemu_abi::execution::{SREG_DS, SREG_FS, SREG_GS, SREG_SS, System, system};

/// fexc's opcode maps (`FEXC_MAP_PRIMARY`, `FEXC_MAP_TWO`). The two-byte map is reported as 1 even with a 66 prefix (the
/// 66 overlay, map 4, is not reported for these opcodes).
const MAP_PRIMARY: u8 = 0;
const MAP_TWO: u8 = 1;
const MAP_0F38: u8 = 5;
const MAP_0F3A: u8 = 6;
/// fexc's VEX map (`FEXC_MAP_VEX`, I8): the opcode byte after the VEX prefix.
const MAP_VEX: u8 = 8;

/// Decodes the instruction at the start of `bytes`, which fexc cut as a system instruction with opcode map `map` and
/// opcode `opcode`.
pub(crate) fn decode(bytes: &[u8], map: u8, opcode: u8) -> Option<System> {
    let (mut at, mut lock, mut segment, mut address_size, mut operand16, mut rep) = (0, false, 0, false, false, 0);
    // Segment prefixes of different classes (SS; CS/DS/ES; FS; GS): no SDM rule for several of one group, refused below.
    let class = |p: u8| match p {
        0x26 | 0x2e | 0x3e => 0x3e,
        p => p,
    };
    let mut mixed = false;
    loop {
        match *bytes.get(at)? {
            0xf0 => lock = true,
            0x66 => operand16 = true,
            0x67 => address_size = true,
            s @ (0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65) => {
                mixed |= segment != 0 && class(segment) != class(s);
                segment = s
            }
            // F2/F3 (mandatory-prefix overlays): only the forms that name them below (`rep_used`) take them.
            p @ (0xf2 | 0xf3) => rep = p,
            _ => break,
        }
        at += 1;
    }
    let mut rep_used = false;
    let mut rex = match bytes[at] {
        r @ 0x40..=0x4f => {
            at += 1;
            r
        }
        _ => 0,
    };
    let (w, r) = (rex & 8 != 0, u32::from(rex >> 2 & 1) << 3);
    match map {
        MAP_TWO if *bytes.get(at)? == 0x0f && *bytes.get(at + 1)? == opcode => at += 2,
        MAP_PRIMARY if *bytes.get(at)? == opcode => at += 1,
        MAP_0F38 if bytes.get(at..at + 3)? == [0x0f, 0x38, opcode] => at += 3,
        MAP_0F3A if bytes.get(at..at + 3)? == [0x0f, 0x3a, opcode] => at += 3,
        // I8: only VLDMXCSR/VSTMXCSR: map 0F, pp 0, VEX.L 0, VEX.vvvv 1111b (fexc admitted nothing else here); VEX.B and VEX.X
        // extend the ModRM operand's base and index as REX would. No legacy SSE prefix or REX before VEX (`vex_ud`).
        MAP_VEX if rex == 0 && !operand16 && rep == 0 => {
            let (b1, b2) = (*bytes.get(at + 1)?, *bytes.get(at + 2)?);
            let (map_select, p, op_at, xb) = match bytes[at] {
                0xc5 => (1, b1, at + 2, 0),
                0xc4 => (b1 & 31, b2, at + 3, (!b1 >> 5 & 1) | (!b1 >> 6 & 1) << 1),
                _ => return None,
            };
            if map_select != 1 || p & 0x7f != 0x78 || *bytes.get(op_at)? != opcode {
                return None;
            }
            rex = xb;
            at = op_at + 1;
        }
        _ => return None,
    }
    let mut s = System { lock: u32::from(lock), ..System::NONE };
    // Operand size of a register destination (66: 16 bits, REX.W wins) and of far pointers and pops (66 refused).
    let size = if w { 8 } else if operand16 { 2 } else { 4 };
    let far_size = if w { Some(8) } else if operand16 { None } else { Some(4) };
    match (map, opcode) {
        (MAP_TWO, 0x20..=0x23) => {
            // The mod field is ignored: the operand is always a register.
            let modrm = *bytes.get(at)?;
            at += 1;
            s.kind = [system::MOV_FROM_CR, system::MOV_FROM_DR, system::MOV_TO_CR, system::MOV_TO_DR][usize::from(opcode - 0x20)];
            (s.cr, s.reg) = (u32::from(modrm >> 3 & 7) | r, u32::from(modrm & 7) | u32::from(rex & 1) << 3);
        }
        (MAP_TWO, 0x30 | 0x32) => s.kind = if opcode == 0x30 { system::WRMSR } else { system::RDMSR },
        (MAP_TWO, 0x33) => s.kind = system::RDPMC,
        (MAP_TWO, 0xa2) => s.kind = system::CPUID,
        (MAP_TWO, 0x31) => s.kind = system::RDTSC,
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
        (MAP_TWO, 0x01) if matches!(*bytes.get(at)?, 0xc0..=0xc6 | 0xc8..=0xcc | 0xcf | 0xd4..=0xdf | 0xe8 | 0xe9 | 0xee | 0xef | 0xfa..=0xff) => {
            (at, rep_used, s.kind) = (at + 1, true, system::UD);
        }
        // I5b: XGETBV and XSETBV (`0f 01 d0`, `d1`; NP: with 66 not decoded).
        (MAP_TWO, 0x01) if matches!(*bytes.get(at)?, 0xd0 | 0xd1) && !operand16 => {
            s.kind = if bytes[at] == 0xd0 { system::XGETBV } else { system::XSETBV };
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
                _ => return None,
            };
            if s.memory == 0 || (s.kind == system::INVLPG && matches!(segment, 0x64 | 0x65)) {
                return None;
            }
        }
        (MAP_TWO, 0x00) => {
            let reg = rm(bytes, &mut at, rex, &mut s)?;
            s.kind = *[system::SLDT, system::STR, system::LLDT, system::LTR, system::VERR, system::VERW, system::UD].get(reg as usize)?;
            // /6: LKGS (f2) is not advertised, and reserved without it.
            rep_used = s.kind == system::UD;
            s.size = size;
        }
        // S3: LAR/LSL r, r/m16 (destination ModRM.reg with REX.R).
        (MAP_TWO, 0x02 | 0x03) => {
            s.cr = rm(bytes, &mut at, rex, &mut s)? | r;
            s.kind = if opcode == 0x02 { system::LAR } else { system::LSL };
            s.size = size;
        }
        (MAP_TWO, 0x05) => s.kind = system::SYSCALL,
        (MAP_TWO, 0x07) => {
            s.kind = system::SYSRET;
            s.size = far_size?;
        }
        // INVD, WBINVD and (f3) WBNOINVD.
        (MAP_TWO, 0x08 | 0x09) => {
            s.kind = system::WBINVD;
            rep_used = opcode == 0x09 && rep == 0xf3;
        }
        // RDRAND (/6; F2/F3 #UD), RDSEED and RDPID (/7, f3), neither advertised: #UD.
        (MAP_TWO, 0xc7) => {
            let reg = rm(bytes, &mut at, rex, &mut s)?;
            // I5b: XRSTORS, XSAVEC, XSAVES m (/3, /4, /5): not advertised (CPUID.(EAX=0DH,ECX=1):EAX = 0), #UD.
            (s.kind, rep_used) = if s.memory != 0 && (3..=5).contains(&reg) {
                (system::UD, true)
            } else if s.memory != 0 || reg < 6 {
                return None; // cmpxchg8b/16b (admitted) and other memory forms are never cut
            } else if reg == 6 && rep == 0 {
                (system::RDRAND, false)
            } else {
                (system::UD, true)
            };
            s.size = size;
        }
        // VMREAD/VMWRITE outside VMX operation; INVEPT/INVVPID (likewise) and INVPCID (not advertised): #UD.
        (MAP_TWO, 0x78 | 0x79) | (MAP_0F38, 0x80..=0x82) => (s.kind, rep_used) = (system::UD, true),
        // I9: MOVBE with a register operand (SDM MOVBE: #UD); F2 is CRC32, F3 is refused by fexc and a memory MOVBE is
        // admitted, none ever cut. ADCX (66 0f 38 f6) and ADOX (F3; ADX not advertised), WRSS (NP 0f 38 f6) and WRUSS (66 0f 38
        // f5; CET_SS not advertised) and the rest of 0f 38 f5/f6, SHA (NP 0f 38 c8..cd, NP 0f 3a cc; not advertised): #UD
        // whatever their operands.
        (MAP_0F38, 0xf0 | 0xf1) => {
            rm(bytes, &mut at, rex, &mut s)?;
            if rep != 0 || s.memory != 0 {
                return None;
            }
            (s.kind, rep_used) = (system::UD, true);
        }
        (MAP_0F38, 0xf5 | 0xf6) => (s.kind, rep_used) = (system::UD, true),
        (MAP_0F38, 0xc8..=0xcd) | (MAP_0F3A, 0xcc) if !operand16 && rep == 0 => s.kind = system::UD,
        (MAP_PRIMARY, 0x8c | 0x8e) => {
            s.cr = rm(bytes, &mut at, rex, &mut s)?;
            s.kind = if opcode == 0x8c { system::MOV_FROM_SREG } else { system::MOV_TO_SREG };
            s.size = size;
        }
        (MAP_PRIMARY, 0xff) => {
            s.kind = match rm(bytes, &mut at, rex, &mut s)? {
                3 => system::CALL_FAR,
                5 => system::JMP_FAR,
                _ => return None,
            };
            s.size = far_size?;
            if s.memory == 0 {
                return None; // a register operand is #UD; not decoded
            }
        }
        (MAP_PRIMARY, 0xca | 0xcb | 0xcf) => {
            s.kind = if opcode == 0xcf { system::IRET } else { system::RET_FAR };
            s.size = far_size?;
            if opcode == 0xca {
                s.disp = i64::from(u16::from_le_bytes(bytes.get(at..at + 2)?.try_into().unwrap()));
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
        // an FS/GS override (CS/DS/ES are null; SS, whose non-canonical fault would be #SS, is not decoded, as I2's MOVS).
        // F3 is REP on INS/OUTS; F2 there (reserved, SDM Vol.2 REP) and F2/F3 on IN/OUT are not decoded.
        (MAP_PRIMARY, 0xe4..=0xe7 | 0xec..=0xef | 0x6c..=0x6f) => {
            let string = opcode < 0x70;
            s.kind = match (string, opcode & 2 != 0) {
                (false, false) => system::IN,
                (false, true) => system::OUT,
                (true, false) => system::INS,
                (true, true) => system::OUTS,
            };
            s.size = if opcode & 1 == 0 { 1 } else if operand16 && !w { 2 } else { 4 };
            if string {
                rep_used = rep == 0xf3;
                s.prefixes = if rep_used { system::REP } else { 0 } | if address_size { system::ADDRESS32 } else { 0 };
                s.segment = match segment {
                    _ if mixed => return None,
                    0x64 if s.kind == system::OUTS => SREG_FS as u32,
                    0x65 if s.kind == system::OUTS => SREG_GS as u32,
                    0x36 if s.kind == system::OUTS => return None,
                    _ => 0,
                };
            } else if opcode & 8 == 0 {
                (s.reg, s.cr) = (system::NO_REG, u32::from(*bytes.get(at)?));
                at += 1;
            } else {
                s.reg = 2;
            }
        }
        // X2: BT/BTS/BTR/BTC with a memory operand, cut only under alignment checking (fexc --alignment-gated): `cr` names
        // the operation, `reg` the bit-offset register (NO_REG for the imm8 form, its immediate skipped), `size` the operand.
        (MAP_TWO, 0xa3 | 0xab | 0xb3 | 0xbb) => {
            let bit = rm(bytes, &mut at, rex, &mut s)? | r;
            (s.kind, s.cr, s.reg, s.size) = (system::BT_ALIGNED, u32::from((opcode >> 3) & 3), bit, size);
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
        // CLFLUSH m8 (`/7`; with 66 CLFLUSHOPT, not advertised: #UD); f3 with a register operand RDFSBASE, RDGSBASE,
        // WRFSBASE, WRGSBASE (`/0../3`), whose register forms without f3 are #UD.
        // I6: the x87 forms the resident executes: FNINIT, FNCLEX (`db e3`, `db e2`), FLDCW m16 (`d9 /5`), FLDENV/FNSTENV m
        // (`d9 /4`, `/6`) and FRSTOR/FNSAVE m (`dd /4`, `/6`), whose image is the 16-bit one with a 66 prefix (`size` 2, else 4),
        // FLD/FSTP m80 (`db /5`, `/7`).
        (MAP_PRIMARY, 0xdb) if matches!(*bytes.get(at)?, 0xe2 | 0xe3) => {
            s.kind = if bytes[at] == 0xe3 { system::FNINIT } else { system::FNCLEX };
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
                (1, 2) if !operand16 => system::LDMXCSR,
                (1, 3) if !operand16 => system::STMXCSR,
                // I5b: FXSAVE, FXRSTOR, XSAVE, XRSTOR (NP); /6 is XSAVEOPT (CPUID.(EAX=0DH,ECX=1):EAX.XSAVEOPT = 0) or with
                // 66 CLWB (CPUID.7.0:EBX.CLWB = 0): #UD.
                (1, 0) if !operand16 => system::FXSAVE,
                (1, 1) if !operand16 => system::FXRSTOR,
                (1, 4) if !operand16 => system::XSAVE,
                (1, 5) if !operand16 => system::XRSTOR,
                (1, 6) => system::UD,
                (1, 7) if operand16 => system::UD,
                (1, 7) => system::CLFLUSH,
                (0, 0..=3) if operand16 => return None, // 66 there: no SDM encoding (a stop, not a guess)
                (0, 0..=3) if rep == 0xf3 => {
                    rep_used = true;
                    s.size = if w { 8 } else { 4 };
                    [system::RDFSBASE, system::RDGSBASE, system::WRFSBASE, system::WRGSBASE][reg as usize]
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
        s = System { kind: system::UD, lock: s.lock, ..System::NONE };
    }
    if s.memory == 1 {
        if address_size {
            return None;
        }
        // CS/DS/ES/SS (base 0 in 64-bit mode) only choose the exception for a non-canonical address. The presented CPU is
        // GenuineIntel, so Intel's rule (SDM Vol. 3A 4.5.1): an SS prefix makes the reference SS-class (#SS(0)) whatever the
        // base, CS/DS/ES DS-class (#GP(0)) even on an RSP/RBP base; fexc's sites follow the same rule (a BT* form found
        // aligned under alignment checking is stepped compiled by it). INVLPG faults on nothing: they are dropped there.
        s.segment = match segment {
            _ if mixed => return None,
            0 => 0,
            0x64 => SREG_FS as u32,
            0x65 => SREG_GS as u32,
            _ if s.kind == system::INVLPG => 0,
            0x36 => SREG_SS as u32,
            _ => SREG_DS as u32,
        };
    }
    s.length = at as u32;
    s.well_formed().then_some(s)
}

/// A system cut [`decode`] does not decode, whose legacy prefixes include LOCK: #UD (I3). fexc cuts every LOCK on an
/// instruction that is not lockable as a system instruction, and LOCK on a system instruction is #UD too (SDM Vol.2
/// "LOCK"), so no operand needs decoding.
pub(crate) fn lock_ud(bytes: &[u8]) -> Option<System> {
    let prefixes = bytes.iter().take_while(|b| matches!(b, 0xf0 | 0xf2 | 0xf3 | 0x66 | 0x67 | 0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65));
    prefixes.take(14).any(|&b| b == 0xf0).then_some(System { kind: system::LOCK_UD, lock: 1, length: 1, ..System::NONE })
}

/// I8: a VEX prefix (C4/C5, 64-bit mode) after a 66, F2 or F3 prefix, or right after a REX: #UD (SDM Vol.2 2.3.2), whatever
/// the instruction and its gating (fexc cuts it as a system instruction).
pub(crate) fn vex_ud(bytes: &[u8]) -> Option<System> {
    let (mut legacy, mut rex) = (false, false);
    for &b in bytes.iter().take(15) {
        match b {
            0xc4 | 0xc5 => return (legacy || rex).then_some(System { kind: system::UD, length: 1, ..System::NONE }),
            0x40..=0x4f => rex = true,
            0x66 | 0xf2 | 0xf3 => (legacy, rex) = (true, false),
            0xf0 | 0x67 | 0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 => rex = false,
            _ => return None,
        }
    }
    None
}

#[cfg(test)]
mod lock_tests {
    #[test]
    fn legacy_prefix_before_vex() {
        for bytes in [&[0x66, 0xc5, 0xf8, 0x77][..], &[0xf3, 0xc5, 0xfe, 0x6f, 0x07], &[0xf2, 0x64, 0xc4, 0xe2, 0x7d, 0x78, 0xff], &[0x48, 0xc5, 0xf8, 0x77]] {
            assert!(super::vex_ud(bytes).is_some_and(|s| s.well_formed() && s.kind == super::system::UD), "{bytes:x?}");
        }
        for bytes in [&[0xc5, 0xf8, 0x77][..], &[0x64, 0xc5, 0xfe, 0x6f, 0x07], &[0x66, 0x0f, 0x6f, 0xc5], &[0x48, 0x89, 0xc4]] {
            assert!(super::vex_ud(bytes).is_none(), "{bytes:x?}");
        }
    }
    #[test]
    fn lock_prefix_anywhere_in_the_legacy_prefixes() {
        for bytes in [&[0xf0, 0x89, 0x03][..], &[0x66, 0xf0, 0x01, 0xd8], &[0xf3, 0xf0, 0x0f, 0x1e, 0xfa], &[0x64, 0xf0, 0xf4]] {
            let s = super::lock_ud(bytes).unwrap();
            assert!(s.well_formed() && s.lock == 1 && s.kind == super::system::LOCK_UD, "{bytes:x?}");
        }
        // No LOCK, or an f0 byte after the REX/opcode (a ModRM or immediate) is no prefix.
        for bytes in [&[0x89, 0x03][..], &[0x48, 0xf0], &[0xb0, 0xf0], &[]] {
            assert!(super::lock_ud(bytes).is_none(), "{bytes:x?}");
        }
    }
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
            s.base = if base == 5 && mode == 0 { system::NO_REG } else { base | b };
            if base == 5 && mode == 0 { 4 } else { [0, 1, 4][mode as usize] }
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
    use system::*;

    fn d(hex: &str, opcode: u8) -> Option<System> {
        let bytes: Vec<u8> = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect();
        decode(&[bytes, vec![0x90; 4]].concat(), MAP_TWO, opcode)
    }
    fn mov(kind: u32, cr: u32, reg: u32, length: u32, lock: u32) -> System {
        System { kind, cr, reg, length, lock, ..System::NONE }
    }
    fn invlpg(base: u32, index: u32, scale: u32, disp: i64, length: u32) -> System {
        System { kind: INVLPG, base, index, scale, disp, length, memory: 1, ..System::NONE }
    }
    fn p(hex: &str, opcode: u8) -> Option<System> {
        let bytes: Vec<u8> = (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect();
        decode(&[bytes, vec![0x90; 4]].concat(), MAP_PRIMARY, opcode)
    }
    fn mem(kind: u32, base: u32, disp: i64, length: u32) -> System {
        System { kind, base, disp, length, memory: 1, ..System::NONE }
    }

    #[test]
    fn descriptor_and_segment_forms() {
        assert_eq!(d("0f0110", 0x01), Some(mem(LGDT, 0, 0, 3))); // lgdt [rax]
        assert_eq!(d("0f011c24", 0x01), Some(mem(LIDT, 4, 0, 4))); // lidt [rsp]
        assert_eq!(d("0f010580000000", 0x01), Some(mem(SGDT, RIP, 0x80, 7))); // sgdt [rip+0x80]
        assert_eq!(d("650f0108", 0x01), Some(System { segment: 5, ..mem(SIDT, 0, 0, 4) })); // sidt gs:[rax]
        assert_eq!(d("0f00d8", 0x00), Some(System { kind: LTR, reg: 0, length: 3, size: 4, ..System::NONE })); // ltr ax
        assert_eq!(d("410f00d1", 0x00), Some(System { kind: LLDT, reg: 9, length: 4, size: 4, ..System::NONE })); // lldt r9w
        assert_eq!(d("480f00c8", 0x00), Some(System { kind: STR, reg: 0, length: 4, size: 8, ..System::NONE })); // str rax
        assert_eq!(d("660f00c3", 0x00), Some(System { kind: SLDT, reg: 3, length: 4, size: 2, ..System::NONE })); // sldt bx
        assert_eq!(d("0f0003", 0x00), Some(System { size: 4, ..mem(SLDT, 3, 0, 3) })); // sldt [rbx]
        assert_eq!(p("8ed8", 0x8e), Some(System { kind: MOV_TO_SREG, cr: 3, reg: 0, length: 2, size: 4, ..System::NONE })); // mov ds, eax
        assert_eq!(p("8e5608", 0x8e), Some(System { cr: 2, size: 4, ..mem(MOV_TO_SREG, 6, 8, 3) })); // mov ss, [rsi+8]
        assert_eq!(p("668cc8", 0x8c), Some(System { kind: MOV_FROM_SREG, cr: 1, reg: 0, length: 3, size: 2, ..System::NONE })); // mov ax, cs
        assert_eq!(p("498ce1", 0x8c), Some(System { kind: MOV_FROM_SREG, cr: 4, reg: 9, length: 3, size: 8, ..System::NONE })); // mov r9, fs
        assert_eq!(p("48ff28", 0xff), Some(System { size: 8, ..mem(JMP_FAR, 0, 0, 3) })); // jmp far [rax] (m16:64)
        assert_eq!(p("ff5b10", 0xff), Some(System { size: 4, ..mem(CALL_FAR, 3, 0x10, 3) })); // call far [rbx+0x10] (m16:32)
        assert_eq!(p("48cb", 0xcb), Some(System { kind: RET_FAR, size: 8, length: 2, ..System::NONE })); // lretq
        assert_eq!(p("ca1000", 0xca), Some(System { kind: RET_FAR, size: 4, disp: 0x10, length: 3, ..System::NONE })); // lret 0x10
        assert_eq!(p("48cf", 0xcf), Some(System { kind: IRET, size: 8, length: 2, ..System::NONE })); // iretq
        assert_eq!(p("f048cf", 0xcf), Some(System { kind: IRET, size: 8, length: 3, lock: 1, ..System::NONE })); // #UD
        assert_eq!(p("2e8e18", 0x8e).map(|s| s.segment), Some(SREG_DS as u32)); // mov ds, cs:[rax]
        assert_eq!(p("368e5d08", 0x8e).map(|s| s.segment), Some(SREG_SS as u32)); // mov ds, ss:[rbp+8]
        for (hex, op) in [("66cb", 0xcb), ("66ff28", 0xff), ("ffe8", 0xff), ("ff20", 0xff), ("362e8e18", 0x8e), ("678e18", 0x8e), ("8ed8", 0x8c)] {
            assert_eq!(p(hex, op), None, "{hex}");
        }
        for (hex, op) in [("0f0120", 0x01), ("0f00f8", 0x00), ("f30f00e0", 0x00)] {
            assert_eq!(d(hex, op), None, "{hex}"); // smsw, 0f 00 /7, f3 verr
        }
    }

    #[test]
    fn control_register_moves() {
        assert_eq!(d("0f22d8", 0x22), Some(mov(MOV_TO_CR, 3, 0, 3, 0))); // mov cr3, rax
        assert_eq!(d("0f20e1", 0x20), Some(mov(MOV_FROM_CR, 4, 1, 3, 0))); // mov rcx, cr4
        assert_eq!(d("440f22c7", 0x22), Some(mov(MOV_TO_CR, 8, 7, 4, 0))); // mov cr8, rdi (REX.R)
        assert_eq!(d("410f2003", 0x20), Some(mov(MOV_FROM_CR, 0, 11, 4, 0))); // mov r11, cr0 (mod 0 ignored, REX.B)
        assert_eq!(d("f00f22e0", 0x22), Some(mov(MOV_TO_CR, 4, 0, 4, 1))); // lock mov cr4, rax
        assert_eq!(d("660f22c0", 0x22), Some(mov(MOV_TO_CR, 0, 0, 4, 0))); // 66 ignored
        assert_eq!(d("0f30", 0x30), Some(System { kind: WRMSR, length: 2, ..System::NONE }));
        assert_eq!(d("480f32", 0x32), Some(System { kind: RDMSR, length: 3, ..System::NONE }));
        assert_eq!(d("0fa2", 0xa2), Some(System { kind: CPUID, length: 2, ..System::NONE }));
        assert_eq!(d("f00fa2", 0xa2), Some(System { kind: CPUID, length: 3, lock: 1, ..System::NONE })); // #UD
        assert_eq!(d("480f31", 0x31), Some(System { kind: RDTSC, length: 3, ..System::NONE }));
        assert_eq!(d("670f01f9", 0x01), Some(System { kind: RDTSCP, length: 4, ..System::NONE })); // 67 ignored
        assert_eq!(d("0f21c0", 0x21), Some(mov(MOV_FROM_DR, 0, 0, 3, 0))); // mov rax, dr0
        assert_eq!(d("0f23f9", 0x23), Some(mov(MOV_TO_DR, 7, 1, 3, 0))); // mov dr7, rcx
        assert_eq!(d("450f21e3", 0x21), Some(mov(MOV_FROM_DR, 12, 11, 4, 0))); // mov r11, dr12 (REX.R: #UD in the resident)
        assert_eq!(d("f00f2300", 0x23), Some(mov(MOV_TO_DR, 0, 0, 4, 1))); // lock mov dr0, rax (mod 0 ignored)
        assert_eq!(d("0f33", 0x33), Some(System { kind: RDPMC, length: 2, ..System::NONE }));
    }

    #[test]
    fn software_interrupts_and_ud() {
        let s = |kind, cr, length| Some(System { kind, cr, length, ..System::NONE });
        assert_eq!(super::decode(&[0xcc], MAP_PRIMARY, 0xcc), s(INT3, 0, 1));
        assert_eq!(super::decode(&[0xf1], MAP_PRIMARY, 0xf1), s(INT1, 0, 1));
        assert_eq!(super::decode(&[0xcd, 0x80], MAP_PRIMARY, 0xcd), s(INT, 0x80, 2));
        assert_eq!(super::decode(&[0xcd], MAP_PRIMARY, 0xcd), None); // truncated immediate
        assert_eq!(super::decode(&[0xce], MAP_PRIMARY, 0xce), s(INTO, 0, 1));
        assert_eq!(super::decode(&[0x0f, 0x0b], MAP_TWO, 0x0b), s(UD, 0, 2));
        assert_eq!(super::decode(&[0x0f, 0xb9, 0xc0], MAP_TWO, 0xb9), s(UD, 0, 2));
        assert_eq!(super::decode(&[0xf0, 0xcc], MAP_PRIMARY, 0xcc).map(|s| (s.kind, s.lock)), Some((INT3, 1))); // #UD by LOCK
    }

    #[test]
    fn interrupt_flag_forms() {
        let s = |kind, size, length| Some(System { kind, size, length, ..System::NONE });
        assert_eq!(super::decode(&[0xfa], MAP_PRIMARY, 0xfa), s(CLI, 0, 1));
        assert_eq!(super::decode(&[0xfb], MAP_PRIMARY, 0xfb), s(STI, 0, 1));
        assert_eq!(super::decode(&[0xf4], MAP_PRIMARY, 0xf4), s(HLT, 0, 1));
        assert_eq!(super::decode(&[0x9d], MAP_PRIMARY, 0x9d), s(POPF, 8, 1));
        assert_eq!(super::decode(&[0x48, 0x9d], MAP_PRIMARY, 0x9d), s(POPF, 8, 2));
        assert_eq!(super::decode(&[0x66, 0x9d], MAP_PRIMARY, 0x9d), s(POPF, 2, 2));
        assert_eq!(super::decode(&[0x66, 0x48, 0x9d], MAP_PRIMARY, 0x9d), s(POPF, 8, 3)); // REX.W wins
        assert_eq!(super::decode(&[0xf0, 0xfa], MAP_PRIMARY, 0xfa).map(|s| (s.kind, s.lock)), Some((CLI, 1))); // #UD by LOCK
        assert!(s(POPF, 2, 2).unwrap().well_formed() && !System { size: 4, ..s(POPF, 4, 1).unwrap() }.well_formed());
    }

    #[test]
    fn bit_tests_under_alignment_checking() {
        let bt = |hex: &str, opcode| d(hex, opcode).map(|s| (s.kind, s.cr, s.reg, s.size, s.memory, s.base, s.length));
        assert_eq!(bt("0fa30b", 0xa3), Some((BT_ALIGNED, 0, 1, 4, 1, 3, 3))); // bt [rbx], ecx
        assert_eq!(bt("0fab0b", 0xab), Some((BT_ALIGNED, 1, 1, 4, 1, 3, 3))); // bts
        assert_eq!(bt("0fb30b", 0xb3), Some((BT_ALIGNED, 2, 1, 4, 1, 3, 3))); // btr
        assert_eq!(bt("0fbb0b", 0xbb), Some((BT_ALIGNED, 3, 1, 4, 1, 3, 3))); // btc
        assert_eq!(bt("0fba2b05", 0xba), Some((BT_ALIGNED, 1, NO_REG, 4, 1, 3, 4))); // bts dword [rbx], 5
        assert_eq!(bt("0fa3cb", 0xa3), None); // register operand: never cut
        let w = super::decode(&[0x48, 0x0f, 0xab, 0x0b], MAP_TWO, 0xab).unwrap();
        let h = super::decode(&[0x66, 0x0f, 0xab, 0x0b], MAP_TWO, 0xab).unwrap();
        assert_eq!((w.size, h.size, w.well_formed(), h.well_formed()), (8, 2, true, true));
        // CS/DS/ES/SS overrides add nothing in 64-bit mode but name the reference's class (Intel: SS or DS); FS/GS add their
        // base. Prefixes of two classes are not decoded.
        for (prefix, class) in [(0x26, SREG_DS), (0x2e, SREG_DS), (0x36, SREG_SS), (0x3e, SREG_DS)] {
            let p = super::decode(&[prefix, 0x0f, 0xa3, 0x4b, 0x01], MAP_TWO, 0xa3).unwrap();
            assert_eq!((p.kind, p.segment, p.disp, p.length), (BT_ALIGNED, class as u32, 1, 5));
        }
        assert_eq!(super::decode(&[0x64, 0x0f, 0xa3, 0x4b, 0x01], MAP_TWO, 0xa3).unwrap().segment, SREG_FS as u32);
        assert_eq!(d("3e480fb39540050000", 0xb3).map(|s| (s.kind, s.base, s.segment)), Some((BT_ALIGNED, 5, SREG_DS as u32))); // ds btr [rbp+0x540], rdx
        assert_eq!(d("2e3e0fa34b01", 0xa3).map(|s| s.segment), Some(SREG_DS as u32)); // two of one class
        for hex in ["363e0fa34b01", "3e640fa34b01", "64650fa34b01"] {
            assert_eq!(d(hex, 0xa3), None, "{hex}");
        }
        assert_eq!(d("360f0138", 0x01).map(|s| s.segment), Some(0)); // invlpg ss:[rax]: faults on nothing
    }

    #[test]
    fn mxcsr_forms() {
        assert_eq!(d("0fae10", 0xae), Some(mem(LDMXCSR, 0, 0, 3))); // ldmxcsr [rax]
        // I6: the resident's x87 forms.
        let x87 = p;
        assert_eq!(x87("dbe3", 0xdb), Some(System { kind: FNINIT, length: 2, ..System::NONE }));
        assert_eq!(x87("dbe2", 0xdb), Some(System { kind: FNCLEX, length: 2, ..System::NONE }));
        assert_eq!(x87("d92b", 0xd9), Some(System { size: 4, ..mem(FLDCW, 3, 0, 2) })); // fldcw [rbx]
        assert_eq!(x87("66d92648", 0xd9).map(|s| (s.kind, s.size, s.base, s.length)), Some((FLDENV, 2, 6, 3))); // fldenv [rsi] (16-bit image)
        assert_eq!(x87("d93424", 0xd9).map(|s| (s.kind, s.size, s.base, s.length)), Some((FNSTENV, 4, 4, 3))); // fnstenv [rsp]
        assert_eq!(x87("6648d97740", 0xd9).map(|s| (s.kind, s.size, s.disp, s.length)), Some((FNSTENV, 4, 0x40, 5))); // 66 REX.W: 28 bytes
        assert_eq!(x87("66dd6008", 0xdd).map(|s| (s.kind, s.size)), Some((FRSTOR, 2))); // 66 alone: the 94-byte image
        assert_eq!(x87("dd6008", 0xdd).map(|s| (s.kind, s.disp, s.length)), Some((FRSTOR, 8, 3))); // frstor [rax+8]
        assert_eq!(x87("64dd30", 0xdd).map(|s| (s.kind, s.segment)), Some((FNSAVE, SREG_FS as u32))); // fnsave fs:[rax]
        assert_eq!(x87("db2b", 0xdb).map(|s| s.kind), Some(FLD80));
        assert_eq!(x87("db3b", 0xdb).map(|s| s.kind), Some(FSTP80));
        for (hex, op) in [("d9e8", 0xd9), ("dbc0", 0xdb), ("dd00", 0xdd), ("d900", 0xd9), ("f3d92b", 0xd9), ("67d92b", 0xd9)] {
            assert_eq!(x87(hex, op), None, "{hex}"); // admitted forms, register operands, F3, a 32-bit address
        }
        assert_eq!(d("410fae5c2408", 0xae), Some(mem(STMXCSR, 12, 8, 6))); // stmxcsr [r12+8]
        assert_eq!(d("640fae18", 0xae), Some(System { segment: 4, ..mem(STMXCSR, 0, 0, 4) })); // stmxcsr fs:[rax]
        // I8: VLDMXCSR/VSTMXCSR (map 8: fexc's VEX map); VEX.B/X extend base and index; VEX.L 1 or vvvv != 1111b is no form here.
        let v = |h: &str| {
            let bytes: Vec<u8> = (0..h.len()).step_by(2).map(|i| u8::from_str_radix(&h[i..i + 2], 16).unwrap()).collect();
            decode(&[bytes, vec![0x90; 4]].concat(), MAP_VEX, 0xae)
        };
        assert_eq!(v("c5f8ae10").map(|s| (s.kind, s.base, s.memory, s.length)), Some((VLDMXCSR, 0, 1, 4))); // vldmxcsr [rax]
        assert_eq!(v("c4c178ae5c2408").map(|s| (s.kind, s.base, s.disp, s.length)), Some((VSTMXCSR, 12, 8, 7))); // vstmxcsr [r12+8]
        assert_eq!(v("64c5f8ae18").map(|s| (s.kind, s.segment)), Some((VSTMXCSR, SREG_FS as u32))); // vstmxcsr fs:[rax]
        assert!(v("c5fcae10").is_none() && v("c5f0ae10").is_none() && v("c5f8aed0").is_none() && v("66c5f8ae10").is_none());
        for hex in ["660fae10", "f30fae10", "670fae10"] {
            assert_eq!(d(hex, 0xae), None, "{hex}"); // 66, f3, 32-bit address
        }
    }

    #[test]
    fn xsave_family() {
        assert_eq!(d("0f0107", 0xae), None); // the opcode must agree
        assert_eq!(d("0f0100", 0xae), None);
        assert_eq!(d("0fae00", 0xae), Some(mem(FXSAVE, 0, 0, 3))); // fxsave [rax]
        assert_eq!(d("480fae03", 0xae), Some(mem(FXSAVE, 3, 0, 4))); // fxsave64 [rbx]
        assert_eq!(d("0fae4e10", 0xae), Some(mem(FXRSTOR, 6, 0x10, 4))); // fxrstor [rsi+0x10]
        assert_eq!(d("0fae2424", 0xae), Some(mem(XSAVE, 4, 0, 4))); // xsave [rsp]
        assert_eq!(d("650fae2f", 0xae), Some(System { segment: 5, ..mem(XRSTOR, 7, 0, 4) })); // xrstor gs:[rdi]
        assert_eq!(d("0fae30", 0xae), Some(System { kind: UD, length: 3, ..System::NONE })); // xsaveopt: not advertised
        assert_eq!(d("660fae30", 0xae), Some(System { kind: UD, length: 4, ..System::NONE })); // clwb: not advertised
        for hex in ["0fc718", "0fc720", "480fc728"] {
            assert_eq!(d(hex, 0xc7).map(|s| s.kind), Some(UD), "{hex}"); // xrstors, xsavec, xsaves64
        }
        assert_eq!(d("0fc708", 0xc7), None); // cmpxchg8b stays admitted
        for hex in ["660fae00", "f30fae20", "f20fae28", "670fae00"] {
            assert_eq!(d(hex, 0xae), None, "{hex}"); // NP forms with 66/F2/F3, a 32-bit address
        }
        assert_eq!(d("0f01d0", 0x01), Some(System { kind: XGETBV, length: 3, ..System::NONE }));
        assert_eq!(d("0f01d1", 0x01), Some(System { kind: XSETBV, length: 3, ..System::NONE }));
        assert_eq!(d("f00f01d1", 0x01).map(|s| (s.kind, s.lock)), Some((XSETBV, 1))); // LOCK: #UD in the resident
        assert_eq!(d("660f01d0", 0x01), None);
        assert_eq!(d("f30f01d0", 0x01), None);
    }

    #[test]
    fn invlpg_operands() {
        assert_eq!(d("0f0138", 0x01), Some(invlpg(0, NO_REG, 0, 0, 3))); // [rax]
        assert_eq!(d("410f017810", 0x01), Some(invlpg(8, NO_REG, 0, 0x10, 5))); // [r8+0x10]
        assert_eq!(d("0f013d00100000", 0x01), Some(invlpg(RIP, NO_REG, 0, 0x1000, 7))); // [rip+0x1000]
        assert_eq!(d("420f013c8b", 0x01), Some(invlpg(3, 9, 2, 0, 5))); // [rbx+r9*4]
        assert_eq!(d("0f013c25f0ffffff", 0x01), Some(invlpg(NO_REG, NO_REG, 0, -16, 8))); // [disp32]
        assert_eq!(d("0f01bc2400000080", 0x01), Some(invlpg(4, NO_REG, 0, -0x8000_0000, 8))); // [rsp+disp32]
        assert_eq!(d("0f017d00", 0x01), Some(invlpg(5, NO_REG, 0, 0, 4))); // [rbp+0]
    }

    #[test]
    fn unsupported_forms_are_refused() {
        let bytes = |hex: &str| -> Vec<u8> { (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect() };
        for (hex, map, op) in [
            ("0f01e0", MAP_TWO, 0x01),   // smsw eax (mod 3)
            ("0f0120", MAP_TWO, 0x01),   // smsw is /4
            ("670f0138", MAP_TWO, 0x01), // 32-bit address
            ("650f0138", MAP_TWO, 0x01), // GS override
            ("f30f22d8", MAP_TWO, 0x22), // F3
            ("0f21c0", MAP_TWO, 0x23),   // FEX's opcode disagrees
            ("f30f33", MAP_TWO, 0x33),   // F3 rdpmc
            ("0f22", MAP_TWO, 0x22),     // truncated ModRM
            ("0f013c", MAP_TWO, 0x01),   // truncated SIB
            ("0f017810", MAP_TWO, 0x01), // fine, but see below: complete
            ("f30fa2", MAP_TWO, 0xa2),   // F3 cpuid
            ("0f01f8", MAP_TWO, 0x31),   // FEX's opcode disagrees
            ("0f22d8", MAP_TWO, 0x20),   // FEX's opcode disagrees
            ("0f22d8", 6, 0x22),         // not the two-byte map
        ] {
            let got = decode(&bytes(hex), map, op);
            assert_eq!(got.is_some(), hex == "0f017810", "{hex}");
        }
    }

    #[test]
    fn port_io_forms() {
        let io = |kind, size, reg, cr, length| Some(System { kind, size, reg, cr, length, ..System::NONE });
        assert_eq!(p("e480", 0xe4), io(IN, 1, NO_REG, 0x80, 2)); // in al, 0x80
        assert_eq!(p("66e5f8", 0xe5), io(IN, 2, NO_REG, 0xf8, 3)); // in ax, 0xf8
        assert_eq!(p("48e5f8", 0xe5), io(IN, 4, NO_REG, 0xf8, 3)); // REX.W: still eax
        assert_eq!(p("6648e5f8", 0xe5), io(IN, 4, NO_REG, 0xf8, 4)); // REX.W wins over 66: 32 bits
        assert_eq!(p("e6e9", 0xe6), io(OUT, 1, NO_REG, 0xe9, 2)); // out 0xe9, al
        assert_eq!(p("e7ff", 0xe7), io(OUT, 4, NO_REG, 0xff, 2));
        assert_eq!(p("ec", 0xec), io(IN, 1, 2, 0, 1)); // in al, dx
        assert_eq!(p("66ed", 0xed), io(IN, 2, 2, 0, 2));
        assert_eq!(p("ef", 0xef), io(OUT, 4, 2, 0, 1)); // out dx, eax
        assert_eq!(p("2e67ee", 0xee), io(OUT, 1, 2, 0, 3)); // overrides and 67 do not apply
        assert_eq!(p("f0ec", 0xec).map(|s| (s.kind, s.lock)), Some((IN, 1))); // #UD by LOCK
        let string = |kind, size, prefixes, segment, length| Some(System { kind, size, prefixes, segment, length, ..System::NONE });
        assert_eq!(p("6c", 0x6c), string(INS, 1, 0, 0, 1));
        assert_eq!(p("f3666d", 0x6d), string(INS, 2, REP, 0, 3)); // rep insw
        assert_eq!(p("f3486f", 0x6f), string(OUTS, 4, REP, 0, 3)); // rep outsd (REX.W ignored)
        assert_eq!(p("67f36e", 0x6e), string(OUTS, 1, REP | ADDRESS32, 0, 3)); // rep outsb with esi/ecx
        assert_eq!(p("646e", 0x6e), string(OUTS, 1, 0, SREG_FS as u32, 2)); // outsb fs:[rsi]
        assert_eq!(p("656c", 0x6c), string(INS, 1, 0, 0, 2)); // INS: ES only, the override is ignored
        assert_eq!(p("3e6f", 0x6f), string(OUTS, 4, 0, 0, 2)); // a null DS override
        for (hex, op) in [("f26c", 0x6c), ("f3ec", 0xec), ("f2e480", 0xe4), ("366e", 0x6e), ("ec", 0xed)] {
            assert_eq!(p(hex, op), None, "{hex}"); // REPNE, F3 on IN, SS on OUTS, FEX's opcode differs
        }
        assert_eq!(super::decode(&[0xe4], MAP_PRIMARY, 0xe4), None); // truncated imm8
    }

    #[test]
    fn s3_forms() {
        let k = |kind, length| Some(System { kind, length, ..System::NONE });
        assert_eq!(d("0f01f8", 0x01), k(SWAPGS, 3));
        assert_eq!(d("0f05", 0x05), k(SYSCALL, 2));
        assert_eq!(d("480f07", 0x07), Some(System { kind: SYSRET, size: 8, length: 3, ..System::NONE }));
        assert_eq!(d("0f07", 0x07), Some(System { kind: SYSRET, size: 4, length: 2, ..System::NONE })); // sysretl
        assert_eq!(d("660f07", 0x07), None);
        let base = |kind, reg, size, length| Some(System { kind, reg, size, length, ..System::NONE });
        assert_eq!(d("f3480faec0", 0xae), base(RDFSBASE, 0, 8, 5));
        assert_eq!(d("f30faecb", 0xae), base(RDGSBASE, 3, 4, 4));
        assert_eq!(d("f3490faed1", 0xae), base(WRFSBASE, 9, 8, 5));
        assert_eq!(d("f3480faedf", 0xae), base(WRGSBASE, 7, 8, 5));
        assert_eq!(d("0faec0", 0xae), k(UD, 3)); // fxsave with a register operand
        assert_eq!(d("66f30faec0", 0xae), None); // 66 on rdfsbase: no SDM encoding
        assert_eq!(d("660f02c1", 0x02), Some(System { kind: LAR, cr: 0, reg: 1, size: 2, length: 4, ..System::NONE }));
        assert_eq!(d("4c0f03c9", 0x03), Some(System { kind: LSL, cr: 9, reg: 1, size: 8, length: 4, ..System::NONE })); // lsl r9, rcx
        assert_eq!(d("0f030d10000000", 0x03), Some(System { cr: 1, size: 4, ..mem(LSL, RIP, 0x10, 7) })); // lsl ecx, [rip+0x10]
        assert_eq!(d("0f00e1", 0x00), Some(System { kind: VERR, reg: 1, size: 4, length: 3, ..System::NONE }));
        assert_eq!(d("0f002d10000000", 0x00), Some(System { size: 4, ..mem(VERW, RIP, 0x10, 7) })); // verw [rip+0x10]
        assert_eq!(d("f20f00f0", 0x00), k(UD, 4)); // lkgs
        assert_eq!(d("0fc7f0", 0xc7), base(RDRAND, 0, 4, 3));
        assert_eq!(d("660fc7f1", 0xc7), base(RDRAND, 1, 2, 4));
        assert_eq!(d("490fc7f7", 0xc7), base(RDRAND, 15, 8, 4));
        for hex in ["0fc7f8", "f30fc7f8", "f30fc7f0"] {
            assert_eq!(d(hex, 0xc7), k(UD, hex.len() as u32 / 2), "{hex}"); // rdseed, rdpid, f3 rdrand
        }
        assert_eq!(d("0fc708", 0xc7), None); // cmpxchg8b: admitted, never cut
        assert_eq!(d("0f09", 0x09), k(WBINVD, 2));
        assert_eq!(d("f30f09", 0x09), k(WBINVD, 3)); // wbnoinvd
        assert_eq!(d("0f08", 0x08), k(WBINVD, 2)); // invd
        assert_eq!(d("f20f09", 0x09), None);
        assert_eq!(d("0fae3b", 0xae), Some(mem(CLFLUSH, 3, 0, 3))); // clflush [rbx]
        assert_eq!(d("650fae7808", 0xae), Some(System { segment: 5, ..mem(CLFLUSH, 0, 8, 5) })); // clflush gs:[rax+8]
        assert_eq!(d("660fae38", 0xae), k(UD, 4)); // clflushopt
        for hex in ["0f01c0", "0f01c5", "0f01c6", "f20f01e9", "0f01ca", "0f01cb", "0f01c8", "0f01c9", "0f01e8", "0f01ee", "0f01ef", "0f01c1", "0f01d9", "660f01cc", "f30f01fe", "f20f01ff"] {
            assert_eq!(d(hex, 0x01), k(UD, hex.len() as u32 / 2), "{hex}");
        }
        assert_eq!(d("0f78c0", 0x78), k(UD, 2));
        assert_eq!(super::decode(&[0x66, 0x0f, 0x38, 0x82, 0x00, 0x90], MAP_0F38, 0x82), k(UD, 4)); // invpcid
        // I9: MOVBE register forms, ADCX/ADOX, WRUSS, SHA: #UD; a memory MOVBE and CRC32 are never decoded here.
        assert_eq!(super::decode(&[0x48, 0x0f, 0x38, 0xf0, 0xc1], MAP_0F38, 0xf0), k(UD, 5));
        assert_eq!(super::decode(&[0x66, 0x0f, 0x38, 0xf1, 0xc8], MAP_0F38, 0xf1), k(UD, 5));
        assert_eq!(super::decode(&[0x0f, 0x38, 0xf0, 0x07], MAP_0F38, 0xf0), None);
        assert_eq!(super::decode(&[0xf2, 0x0f, 0x38, 0xf0, 0xc1], MAP_0F38, 0xf0), None);
        assert_eq!(super::decode(&[0x66, 0x48, 0x0f, 0x38, 0xf6, 0xc1], MAP_0F38, 0xf6), k(UD, 5));
        assert_eq!(super::decode(&[0xf3, 0x48, 0x0f, 0x38, 0xf6, 0x07], MAP_0F38, 0xf6), k(UD, 5));
        assert_eq!(super::decode(&[0x0f, 0x38, 0xcb, 0xc1], MAP_0F38, 0xcb), k(UD, 3));
        assert_eq!(super::decode(&[0x0f, 0x3a, 0xcc, 0xc1, 0x01], MAP_0F3A, 0xcc), k(UD, 3));
        assert_eq!(super::decode(&[0x66, 0x0f, 0x3a, 0xcc, 0xc1, 0x01], MAP_0F3A, 0xcc), None);
        assert_eq!(d("f00f01f8", 0x01).map(|s| (s.kind, s.lock)), Some((SWAPGS, 1))); // #UD by LOCK
    }
}
