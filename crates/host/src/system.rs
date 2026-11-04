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
//! else (other system instructions, F2/F3 prefixes on other forms, a 66 prefix on a far transfer, an FS/GS override on
//! INVLPG) is `None`: the vCPU stops with fexc's reason, as for code it cannot compile.
use natemu_abi::execution::{SREG_FS, SREG_GS, System, system};

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

/// Decodes the instruction at the start of `bytes`, which fexc cut as a system instruction with opcode map `map` and
/// opcode `opcode`.
pub(crate) fn decode(bytes: &[u8], map: u8, opcode: u8) -> Option<System> {
    // `segment`: the last FS/GS prefix (0x64, 0x65), else 0. CS/DS/ES/SS prefixes are ignored in 64-bit mode (owner decision,
    // November 3: as on the reference AMD machine), so the operand's default segment decides a non-canonical address's
    // exception (the resident's `segments::noncanonical`); of several FS/GS prefixes the last applies (the same machine).
    let (mut at, mut lock, mut segment, mut address_size, mut operand16, mut rep) = (0, false, 0, false, false, 0);
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
    let (w, r) = (rex & 8 != 0, u32::from(rex >> 2 & 1) << 3);
    match map {
        MAP_TWO if *bytes.get(at)? == 0x0f && *bytes.get(at + 1)? == opcode => at += 2,
        // X1c: a Reserved-NOP cell under a mandatory-prefix overlay map (F3, F2, 66): the same two-byte opcode; also FEX's
        // SSE4a overlays of 0f 78/79 (66 EXTRQ, F2 INSERTQ).
        MAP_TWO_F3..=MAP_TWO_66 if matches!(opcode, 0x0d | 0x18..=0x1d | 0x78 | 0x79) && *bytes.get(at)? == 0x0f && *bytes.get(at + 1)? == opcode => at += 2,
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
        // I10: SAVEPREVSSP (f3 0f 01 ea; SETSSBSY is f3 e8): "#UD If CR4.CET = 0" (CET not advertised); reserved otherwise.
        // X1c: the rest of mod = 11 outside SMSW (/4) and LMSW (/6): PBNDKB (c7), TDX SEAMRET/SEAMOPS (66 cd/ce; reserved
        // without 66), FRED ERETS/ERETU (f2/f3 ca, in c8..cc), UIRET (f3 ec): not advertised; d2, d3, eb, ed and the unprefixed forms are blank
        // in Table A-6 (reserved: #UD, SDM Vol.3 7.15 interrupt 6).
        (MAP_TWO, 0x01) if matches!(*bytes.get(at)?, 0xc0..=0xcf | 0xd2..=0xdf | 0xe8..=0xef | 0xfa..=0xff) => {
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
                // I10: RSTORSSP m64 (f3 0f 01 /5): "#UD If CR4.CET = 0" (CET not advertised); X1c: /5 m without F3 is blank in
                // Table A-6 (reserved).
                5 if s.memory == 1 => {
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
            if (s.memory == 0 && matches!(s.kind, system::LGDT..=system::SIDT | system::INVLPG)) || (s.kind == system::INVLPG && matches!(segment, 0x64 | 0x65)) {
                return None;
            }
        }
        (MAP_TWO, 0x00) => {
            let reg = rm(bytes, &mut at, rex, &mut s)?;
            s.kind = [system::SLDT, system::STR, system::LLDT, system::LTR, system::VERR, system::VERW, system::UD, system::UD][reg as usize];
            // /6: LKGS (f2) is not advertised, and reserved without it; X1c: /7 is blank in Table A-6 (reserved).
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
            s.kind = if opcode & 1 == 0 { system::PUSH_SREG } else { system::POP_SREG };
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
        (MAP_TWO, 0xb2 | 0xb4 | 0xb5) if *bytes.get(at)? >= 0xc0 => (s.kind, rep_used) = (system::UD, true),
        // X1c: PUSH ES/CS/SS/DS, POP ES/SS/DS and CALL/JMP ptr16:16/32 are "Invalid" in 64-bit mode (SDM Vol.2 PUSH, POP,
        // CALL, JMP opcode tables; PUSH: "#UD If the PUSH is of CS, SS, DS, or ES"; Table A-2 "i64").
        (MAP_PRIMARY, 0x06 | 0x07 | 0x0e | 0x16 | 0x17 | 0x1e | 0x1f | 0x9a | 0xea) => (s.kind, rep_used) = (system::UD, true),
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
        // VMREAD/VMWRITE outside VMX operation; INVEPT/INVVPID (likewise) and INVPCID (not advertised): #UD. 66/F2 0f 78/79
        // are AMD's SSE4a EXTRQ/INSERTQ (CPUID.80000001H:ECX.SSE4A[6] = 0 on the profile; blank in Intel's Table A-3): #UD.
        (MAP_TWO, 0x78 | 0x79) | (MAP_0F38, 0x80..=0x82) => (s.kind, rep_used) = (system::UD, true),
        (MAP_TWO_F3..=MAP_TWO_66, 0x78 | 0x79) if !advertised(SSE4A) => (s.kind, rep_used) = (system::UD, true),
        // I9: MOVBE with a register operand (SDM MOVBE: #UD); F2 is CRC32, F3 is refused by fexc and a memory MOVBE is
        // admitted, none ever cut. ADCX (66 0f 38 f6) and ADOX (F3; ADX not advertised), WRSS (NP 0f 38 f6) and WRUSS (66 0f 38
        // f5; CET_SS not advertised) and the rest of 0f 38 f5/f6: #UD whatever their operands; SHA (NP 0f 38 c8..cd, NP 0f 3a
        // cc) likewise only while the profile does not advertise it (advertised since November 3: fexc compiles it, no cut).
        (MAP_0F38, 0xf0 | 0xf1) => {
            rm(bytes, &mut at, rex, &mut s)?;
            if rep != 0 || s.memory != 0 {
                return None;
            }
            (s.kind, rep_used) = (system::UD, true);
        }
        (MAP_0F38, 0xf5 | 0xf6) => (s.kind, rep_used) = (system::UD, true),
        (MAP_0F38, 0xc8..=0xcd) | (MAP_0F3A, 0xcc) if !operand16 && rep == 0 && !advertised(SHA.unwrap()) => s.kind = system::UD,
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
            // X1c: a register operand is "an operand type that is invalid for its accompanying opcode" (SDM Vol.3 7.15
            // interrupt 6; JMP/CALL far take m16:16/32/64 only): #UD whatever the operand size.
            if s.memory == 0 {
                s.kind = system::UD;
            } else {
                s.size = far_size?;
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
            s.size = if opcode & 1 == 0 { 1 } else if operand16 && !w { 2 } else { 4 };
            if string {
                rep_used = rep == 0xf3;
                s.prefixes = if rep_used { system::REP } else { 0 } | if address_size { system::ADDRESS32 } else { 0 };
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
                // X1c: PTWRITE r/m (F3 /4): "#UD If CPUID.(EAX=14H, ECX=0):EBX.PTWRITE[bit 4] = 0"; leaf 14H is Intel PT's
                // (CPUID.07H:EBX.PT, not advertised).
                (_, 4) if rep == 0xf3 && !operand16 && !advertised(PT.unwrap()) => system::UD,
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

/// A CPUID feature bit of the presented CPU: (leaf, subleaf, register EAX..EDX = 0..3, bit), read from the generated
/// profile (`cpu_profile::CPUID`, from tools/models/cpu_profile.json).
type Feature = (u32, u32, usize, u32);
fn advertised((leaf, sub, reg, bit): Feature) -> bool {
    use natemu_abi::cpu_profile::{ANY, CPUID};
    CPUID.iter().find(|&&(l, s, _)| l == leaf && (s == ANY || s == sub)).is_some_and(|(_, _, r)| r[reg] >> bit & 1 != 0)
}
/// SSE and SSE2 (with MMX) are part of every 64-bit processor: their cells are defined whatever the profile.
const BASE: Option<Feature> = None;
const SSE3: Option<Feature> = Some((1, 0, 2, 0));
const SSSE3: Option<Feature> = Some((1, 0, 2, 9));
const SSE4_1: Option<Feature> = Some((1, 0, 2, 19));
const SSE4_2: Option<Feature> = Some((1, 0, 2, 20));
const PCLMULQDQ: Option<Feature> = Some((1, 0, 2, 1));
const SSE4A: Feature = (0x8000_0001, 0, 2, 6);
const AES: Option<Feature> = Some((1, 0, 2, 25));
const MOVBE: Option<Feature> = Some((1, 0, 2, 22));
const ADX: Option<Feature> = Some((7, 0, 1, 19));
const SHA: Option<Feature> = Some((7, 0, 1, 29));
const CET_SS: Option<Feature> = Some((7, 0, 2, 7));
const GFNI: Option<Feature> = Some((7, 0, 2, 8));
const KL: Option<Feature> = Some((7, 0, 2, 23));
const MOVDIRI: Option<Feature> = Some((7, 0, 2, 27));
const MOVDIR64B: Option<Feature> = Some((7, 0, 2, 28));
const ENQCMD: Option<Feature> = Some((7, 0, 2, 29));
/// Intel PT (PTWRITE is enumerated in its leaf 14H, which exists only with PT).
const PT: Option<Feature> = Some((7, 0, 1, 25));
/// The legacy (non-VEX) cells of the SIMD rows of SDM Vol.2 Tables A-3 (two-byte), A-4 (0f 38) and A-5 (0f 3a): (map: 1,
/// 2 or 3; first and last opcode; the mandatory prefixes defined there: N none, 6 66, 3 F3, 2 F2; the feature defining
/// them). A cell of these rows not listed is blank.
const SIMD: &[(u8, u8, u8, &[u8], Option<Feature>)] = &[
    (1, 0x10, 0x11, b"N632", BASE), (1, 0x12, 0x12, b"N6", BASE), (1, 0x12, 0x12, b"32", SSE3), (1, 0x13, 0x15, b"N6", BASE),
    (1, 0x16, 0x16, b"N6", BASE), (1, 0x16, 0x16, b"3", SSE3), (1, 0x17, 0x17, b"N6", BASE), (1, 0x28, 0x29, b"N6", BASE),
    (1, 0x2a, 0x2a, b"N632", BASE), (1, 0x2b, 0x2b, b"N6", BASE), (1, 0x2c, 0x2d, b"N632", BASE), (1, 0x2e, 0x2f, b"N6", BASE),
    (1, 0x50, 0x50, b"N6", BASE), (1, 0x51, 0x51, b"N632", BASE), (1, 0x52, 0x53, b"N3", BASE), (1, 0x54, 0x57, b"N6", BASE),
    (1, 0x58, 0x5a, b"N632", BASE), (1, 0x5b, 0x5b, b"N63", BASE), (1, 0x5c, 0x5f, b"N632", BASE), (1, 0x60, 0x6b, b"N6", BASE),
    (1, 0x6c, 0x6d, b"6", BASE), (1, 0x6e, 0x6e, b"N6", BASE), (1, 0x6f, 0x6f, b"N63", BASE), (1, 0x70, 0x70, b"N632", BASE),
    (1, 0x71, 0x76, b"N6", BASE), (1, 0x77, 0x77, b"N", BASE), (1, 0x7c, 0x7d, b"62", SSE3), (1, 0x7e, 0x7f, b"N63", BASE),
    (1, 0xc2, 0xc2, b"N632", BASE), (1, 0xc4, 0xc6, b"N6", BASE), (1, 0xd0, 0xd0, b"62", SSE3), (1, 0xd1, 0xd5, b"N6", BASE),
    (1, 0xd6, 0xd6, b"632", BASE), (1, 0xd7, 0xe5, b"N6", BASE), (1, 0xe6, 0xe6, b"632", BASE), (1, 0xe7, 0xef, b"N6", BASE),
    (1, 0xf0, 0xf0, b"2", SSE3), (1, 0xf1, 0xfe, b"N6", BASE),
    (2, 0x00, 0x0b, b"N6", SSSE3), (2, 0x10, 0x10, b"6", SSE4_1), (2, 0x14, 0x15, b"6", SSE4_1), (2, 0x17, 0x17, b"6", SSE4_1),
    (2, 0x1c, 0x1e, b"N6", SSSE3), (2, 0x20, 0x25, b"6", SSE4_1), (2, 0x28, 0x2b, b"6", SSE4_1), (2, 0x30, 0x35, b"6", SSE4_1),
    (2, 0x37, 0x37, b"6", SSE4_2), (2, 0x38, 0x41, b"6", SSE4_1), (2, 0x80, 0x82, b"6", BASE), (2, 0xc8, 0xcd, b"N", SHA),
    (2, 0xcf, 0xcf, b"6", GFNI), (2, 0xd8, 0xd8, b"3", KL), (2, 0xdb, 0xdf, b"6", AES), (2, 0xdc, 0xdf, b"3", KL),
    (2, 0xf0, 0xf1, b"N6", MOVBE), (2, 0xf0, 0xf1, b"2", SSE4_2), (2, 0xf5, 0xf5, b"6", CET_SS), (2, 0xf6, 0xf6, b"63", ADX),
    (2, 0xf8, 0xf8, b"6", MOVDIR64B), (2, 0xf8, 0xf8, b"32", ENQCMD), (2, 0xf9, 0xf9, b"N", MOVDIRI), (2, 0xfa, 0xfb, b"3", KL),
    (3, 0x08, 0x0e, b"6", SSE4_1), (3, 0x0f, 0x0f, b"N6", SSSE3), (3, 0x14, 0x17, b"6", SSE4_1), (3, 0x20, 0x22, b"6", SSE4_1),
    (3, 0x40, 0x42, b"6", SSE4_1), (3, 0x44, 0x44, b"6", PCLMULQDQ), (3, 0x60, 0x63, b"6", SSE4_2), (3, 0xcc, 0xcc, b"N", SHA),
    (3, 0xce, 0xcf, b"6", GFNI), (3, 0xdf, 0xdf, b"6", AES),
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
        let defined = SIMD.iter().any(|&(m, lo, hi, ps, f)| m == map && (lo..=hi).contains(&op) && ps.contains(&p) && f.is_none_or(advertised));
        Some(!defined)
    };
    let ud = match *bytes.get(at)? {
        0x8d => modrm(1)?.0,
        0xd6 => true,
        0x0f => match *bytes.get(at + 1)? {
            0x04 | 0x0a | 0x0c | 0x0e | 0x24..=0x27 | 0x36 | 0x37 | 0x39 | 0x3b | 0x3c..=0x3f | 0x7a | 0x7b | 0xaa => true,
            0xb8 => rep != 0xf3,
            0xc3 => modrm(2)?.0,
            // PTWRITE r (F3 /4 register; the memory form is a system cut, [`decode`]): Intel PT not advertised.
            0xae => rep == 0xf3 && !p66 && modrm(2)? == (true, 4) && !advertised(PT.unwrap()),
            0xc7 => match modrm(2)? {
                (true, reg) => reg <= 5,
                (false, reg) => matches!(reg, 0 | 2 | 6 | 7),
            },
            op if simd_row(op) => {
                let (register, reg) = modrm(2)?;
                let p = prefix;
                cell(1, op)? || match op {
                    0x13 | 0x17 | 0x2b | 0xe7 => register,
                    0x12 | 0x16 => register && p == Some(b'6'),
                    0xf0 => register,
                    0x50 | 0xd7 | 0xf7 | 0xc5 => !register,
                    0x71 | 0x72 => !register || !matches!(reg, 2 | 4 | 6),
                    0x73 => !register || !(matches!(reg, 2 | 6) || (matches!(reg, 3 | 7) && p == Some(b'6'))),
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
    ud.then_some(System { kind: system::UD, length: 1, ..System::NONE })
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
    while let Some(&p @ (0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65 | 0x66 | 0x67 | 0xf2 | 0xf3 | 0x40..=0x4f)) = bytes.get(at) {
        a32 |= p == 0x67;
        at += 1;
    }
    let op = *bytes.get(at)?;
    let rel8 = || bytes.get(at + 1).map(|&b| i64::from(b as i8));
    let rel32 = |k: usize| bytes.get(at + k..at + k + 4).map(|b| i64::from(i32::from_le_bytes(b.try_into().unwrap())));
    let (cr, disp, length) = match op {
        0x70..=0x7f => (u32::from(op & 15), rel8()?, 2),
        0xeb => (16, rel8()?, 2),
        0xe9 => (16, rel32(1)?, 5),
        0xe8 => (17, rel32(1)?, 5),
        0xe0..=0xe3 => (18 + u32::from(op - 0xe0), rel8()?, 2),
        0x0f if matches!(*bytes.get(at + 1)?, 0x80..=0x8f) => (u32::from(bytes[at + 1] & 15), rel32(2)?, 6),
        _ => return None,
    };
    let length = length + at as u64;
    if length > 15 {
        return None;
    }
    let target = rip.wrapping_add(length).wrapping_add(disp as u64);
    let canonical = (target as i64) << 16 >> 16 == target as i64;
    let size = if a32 && cr >= 18 { 4 } else { 8 };
    (!canonical).then_some(System { kind: system::BRANCH, cr, size, disp, length: length as u32, ..System::NONE })
}

#[cfg(test)]
mod x1c_tests {
    fn b(hex: &str) -> Vec<u8> {
        (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect()
    }
    #[test]
    fn undefined_forms() {
        for hex in ["8dc0", "d6", "0f04", "0f0e", "0f25", "0f37", "0f3f", "0f7b", "0faa", "0f13c0", "660f17c0", "0f2bc0", "660fe7c1",
                    "f20ff0c0", "0fc3c0", "480fc7c8", "0fc7e8", "0fc700", "0fc710", "f30fc730", "0fc738", "660f382ac0", "0f3850", "0f3a00c000",
                    // blank mandatory-prefix cells, JMPE, operand types (review)
                    "0fd0c0", "0fd6c0", "0fe6c0", "0ff000", "0f7cc0", "f30f54c0", "f20f54c0", "f30f2b00", "f20f2b00", "f30f2bc0", "f20f5bc0",
                    "0fb8c0", "660fb8c0", "f20fb8c0", "660f3850", "660f12c0", "0f5000", "0fd700", "f30fd600", "0f711000", "0f73d8", "f30f38f0c0", "660f3a0000"] {
            assert!(super::undefined(&b(hex)).is_some_and(|s| s.well_formed() && s.kind == super::system::UD), "{hex}");
        }
        for hex in ["8d00", "0f1300", "0fc300", "f30fb8c0", "660f73d8", "f30fd6c0", "660f1200", "66f30f10c0", "0fc708", "0fc7f0", "0fc7f8", "0f38f0c0", "0f3800c0", "0f3a0fc000", "0f05", "0f0d08"] {
            assert!(super::undefined(&b(hex)).is_none(), "{hex}");
        }
    }
    /// Every feature-gated cell of `SIMD` (and PTWRITE) is #UD exactly when tools/models/cpu_profile.json, the profile's
    /// source, says its CPUID bit is clear: a profile change cannot leave a false #UD behind (or a missed one).
    #[test]
    fn undefined_follows_the_profile() {
        use crate::json::{Value, parse};
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../tools/models/cpu_profile.json");
        let profile = parse(&std::fs::read_to_string(path).unwrap()).unwrap();
        let Some(Value::Array(leaves)) = profile.get("cpuid") else { panic!("no cpuid table") };
        let state = |(leaf, sub, reg, bit): super::Feature| -> String {
            let entry = leaves.iter().find(|e| {
                let n = |k: &str| e.get(k).and_then(Value::as_str).map(|v| u32::from_str_radix(v.trim_start_matches("0x"), 16).unwrap());
                n("leaf") == Some(leaf) && n("subleaf").is_none_or(|s| s == sub)
            });
            let field = entry.and_then(|e| e.get("fields")?.get(["eax", "ebx", "ecx", "edx"][reg])?.get(&bit.to_string()));
            field.and_then(|f| f.get("state")?.as_str()).unwrap_or_else(|| panic!("CPUID {leaf:#x}.{sub} reg {reg} bit {bit} not in the profile")).to_string()
        };
        let mut checked = 0;
        for &(map, lo, hi, prefixes, feature) in super::SIMD {
            let Some(feature) = feature else { continue };
            let on = match state(feature).as_str() {
                "advertised" => true,
                "clear" => false,
                s => panic!("feature {feature:?}: state {s}"),
            };
            assert_eq!(super::advertised(feature), on, "{feature:?}: generated profile and cpu_profile.json disagree");
            for op in lo..=hi {
                for &p in prefixes {
                    let pre: &[u8] = match p { b'6' => &[0x66], b'3' => &[0xf3], b'2' => &[0xf2], _ => &[] };
                    let esc: &[u8] = match map { 1 => &[0x0f], 2 => &[0x0f, 0x38], _ => &[0x0f, 0x3a] };
                    let bytes = [pre, esc, &[op, 0x00, 0x00]].concat();
                    // Another feature may define the same cell (e.g. 66 0f 38 dc: AES and KL with F3): judge by all of them.
                    let any = super::SIMD.iter().any(|&(m, l, h, ps, f)| m == map && (l..=h).contains(&op) && ps.contains(&p) && f.is_none_or(|f| state(f) == "advertised"));
                    assert_eq!(super::undefined(&bytes).is_some(), !any, "{bytes:02x?} ({feature:?} {})", if on { "advertised" } else { "clear" });
                    checked += 1;
                }
            }
        }
        assert!(checked > 100, "{checked}");
        assert_eq!(super::advertised(super::PT.unwrap()), state(super::PT.unwrap()) == "advertised");
    }
    #[test]
    fn reserved_nops() {
        use super::system::RESERVED_NOP;
        let d = |hex: &str, map| super::decode(&b(hex), map, b(hex)[b(hex).iter().position(|&x| x == 0x0f).unwrap() + 1]).map(|s| (s.kind, s.length));
        for (hex, map, length) in [("0f1820", 1, 3), ("0f18c0", 1, 3), ("f30f180400", 2, 5), ("0f1900", 1, 3), ("660f1a00", 4, 4), ("f20f1b8000010000", 3, 8),
                                   ("0f1cc8", 1, 3), ("0f1d4408f0", 1, 5), ("0f0d00", 1, 3), ("640f0d3d00000000", 1, 8), ("480f1d05ffffffff", 1, 8)] {
            assert_eq!(d(hex, map), Some((RESERVED_NOP, length)), "{hex}");
        }
        assert_eq!(d("0f0dc0", 1), None); // fexc's #UD
    }
    #[test]
    fn push_pop_fs_gs() {
        use super::system::{POP_SREG, PUSH_SREG};
        let d = |hex: &str| super::decode(&b(hex), super::MAP_TWO, b(hex).iter().rev().next().copied().unwrap()).map(|s| (s.kind, s.cr, s.size, s.length));
        let (fs, gs) = (natemu_abi::execution::SREG_FS as u32, natemu_abi::execution::SREG_GS as u32);
        assert_eq!(d("0fa0"), Some((PUSH_SREG, fs, 8, 2)));
        assert_eq!(d("660fa9"), Some((POP_SREG, gs, 2, 3)));
        assert_eq!(d("66480fa8"), Some((PUSH_SREG, gs, 8, 4)));
        assert_eq!(d("f30fa1"), None); // F3: not a form of it
    }
    #[test]
    fn noncanonical_branches() {
        use super::system::BRANCH;
        let s = |hex: &str, rip| super::branch(&b(hex), rip).map(|s| (s.kind, s.cr, s.size, s.disp, s.length));
        let top = 0x7fff_ffff_fff0;
        assert_eq!(s("eb20", top), Some((BRANCH, 16, 8, 0x20, 2)));
        assert_eq!(s("eb0c", top), None); // 0x7fff_ffff_fffe: canonical
        assert_eq!(s("e810000000", top), Some((BRANCH, 17, 8, 0x10, 5)));
        assert_eq!(s("0f8410000000", top), Some((BRANCH, 4, 8, 0x10, 6)));
        assert_eq!(s("75e0", 0xffff_8000_0000_0010), Some((BRANCH, 5, 8, -0x20, 2)));
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
        // R2 review: a REX before F0 is ignored, the F0 is still a prefix (SDM Vol.2 2.2.1).
        for bytes in [&[0xf0, 0x89, 0x03][..], &[0x66, 0xf0, 0x01, 0xd8], &[0xf3, 0xf0, 0x0f, 0x1e, 0xfa], &[0x64, 0xf0, 0xf4], &[0x48, 0xf0, 0x89, 0x03]] {
            let s = super::lock_ud(bytes).unwrap();
            assert!(s.well_formed() && s.lock == 1 && s.kind == super::system::LOCK_UD, "{bytes:x?}");
        }
        // No LOCK, or an f0 byte after the opcode (a ModRM or immediate) is no prefix.
        for bytes in [&[0x89, 0x03][..], &[0x48, 0x89, 0xf0], &[0xb0, 0xf0], &[]] {
            assert!(super::lock_ud(bytes).is_none(), "{bytes:x?}");
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
        let length = if b.starts_with(&[0xf3, 0x0f, 0x1e, 0xfa]) || b.starts_with(&[0xf3, 0x0f, 0x1e, 0xfb]) {
            4
        } else {
            let mut k = b.iter().take_while(|&&p| p == 0x66).count();
            if b.get(k) == Some(&0x90) {
                k + 1
            } else {
                k += b[k..].iter().take_while(|&&p| matches!(p, 0x66 | 0x2e | 0x3e | 0x26 | 0x36)).count();
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

    /// Perf chunk 12: runs of no-operation instructions, counted; anything else refused.
    #[test]
    fn nop_runs() {
        let h = |hex: &str| -> Vec<u8> { (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect() };
        for (hex, n) in [("f30f1efa", 1), ("90909090909090", 7), ("6690", 1), ("0f1f00", 1), ("0f1f4000", 1), ("0f1f440000", 1),
                         ("660f1f440000", 1), ("0f1f8000000000", 1), ("0f1f840000000000", 1), ("662e0f1f840000000000", 1),
                         ("66662e0f1f840000000000", 1), ("480f1fc0", 1), ("f30f1efb900f1f00", 3), ("0f1f0525000000", 1)] {
            assert_eq!(nops(&h(hex)), Some(n), "{hex}");
        }
        for hex in ["4190", "f390", "f00f1f00", "0f1f08", "670f1f00", "640f1f00", "f20f1f00", "0f1f", "0f1f44", "90cc", "f30f1efc",
                    "6666666666666666666666662e0f1f840000000000", "0f18c0"] {
            assert_eq!(nops(&h(hex)), None, "{hex}");
        }
    }

    /// R2 review: REX bytes among the prefixes, the last before the opcode in force (SDM Vol.2 2.2.1).
    #[test]
    fn rex_chains() {
        assert_eq!(opcode_at(&[0x4f, 0x47, 0x41, 0x4d, 0x53]), (4, 0x4d)); // CRYPTOGAMS: push r11
        assert_eq!(opcode_at(&[0x48, 0x66, 0x89, 0xc0]), (2, 0)); // the REX before 66 is ignored
        assert_eq!(opcode_at(&[0x66, 0x48]), (2, 0x48));
        // mov cr3, r8 with a W/X chain before its REX.B; mov rax, cr0 with a REX.B before an F0 (ignored: rax, not r8)
        let crs = |s: Option<System>| s.map(|s| (s.kind, s.cr, s.reg, s.length, s.lock));
        assert_eq!(crs(d("49410f22d8", 0x22)), crs(d("410f22d8", 0x22)).map(|(k, c, r, l, x)| (k, c, r, l + 1, x)));
        assert_eq!(crs(d("41f00f20c0", 0x20)), crs(d("f00f20c0", 0x20)).map(|(k, c, r, l, x)| (k, c, r, l + 1, x)));
        assert!(lock_ud(&[0x48, 0xf0, 0x0f, 0x22, 0xd8]).is_some());
    }

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
        assert_eq!(p("2e8e18", 0x8e).map(|s| s.segment), Some(0)); // mov ds, cs:[rax]: CS ignored
        assert_eq!(p("368e5d08", 0x8e).map(|s| s.segment), Some(0)); // mov ds, ss:[rbp+8]: SS ignored (RBP: SS by default)
        // I10: 0x67 on a memory operand (mov ds, [eax]; lgdt [eip+0x80]).
        assert_eq!(p("678e18", 0x8e), Some(System { cr: 3, size: 4, prefixes: ADDRESS32, ..mem(MOV_TO_SREG, 0, 0, 3) }));
        assert_eq!(d("670f011580000000", 0x01), Some(System { prefixes: ADDRESS32, ..mem(LGDT, RIP, 0x80, 8) }));
        assert_eq!(p("362e8e18", 0x8e).map(|s| s.segment), Some(0)); // two null prefixes: ignored
        for (hex, op) in [("66cb", 0xcb), ("66ff28", 0xff), ("ff20", 0xff), ("8ed8", 0x8c)] {
            assert_eq!(p(hex, op), None, "{hex}");
        }
        for hex in ["ffe8", "ffd8", "66ffe8"] {
            assert_eq!(p(hex, 0xff).map(|s| s.kind), Some(UD), "{hex}"); // X1c: far JMP/CALL with a register operand
        }
        assert_eq!(d("f30f00e0", 0x00), None); // f3 verr (X1c: 0f 00 /7 decodes, `x1c_forms`)
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
        // CS/DS/ES/SS overrides are ignored in 64-bit mode (the default segment decides the class); FS/GS add their base,
        // the last of several applies, a null prefix before or after it changes nothing (owner decisions, November 3).
        for prefix in [0x26, 0x2e, 0x36, 0x3e] {
            let p = super::decode(&[prefix, 0x0f, 0xa3, 0x4b, 0x01], MAP_TWO, 0xa3).unwrap();
            assert_eq!((p.kind, p.segment, p.disp, p.length), (BT_ALIGNED, 0, 1, 5));
        }
        assert_eq!(super::decode(&[0x64, 0x0f, 0xa3, 0x4b, 0x01], MAP_TWO, 0xa3).unwrap().segment, SREG_FS as u32);
        assert_eq!(d("3e480fb39540050000", 0xb3).map(|s| (s.kind, s.base, s.segment)), Some((BT_ALIGNED, 5, 0))); // ds btr [rbp+0x540], rdx
        for (hex, segment) in [("2e3e0fa34b01", 0), ("363e0fa34b01", 0), ("3e640fa34b01", SREG_FS), ("643e0fa34b01", SREG_FS), ("64650fa34b01", SREG_GS),
                               ("65640fa34b01", SREG_FS), ("6564360fa34b01", SREG_FS)] {
            assert_eq!(d(hex, 0xa3).map(|s| s.segment), Some(segment as u32), "{hex}");
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
        for (hex, op) in [("d9e8", 0xd9), ("dbc0", 0xdb), ("dd00", 0xdd), ("d900", 0xd9), ("f3d92b", 0xd9)] {
            assert_eq!(x87(hex, op), None, "{hex}"); // admitted forms, register operands, F3
        }
        assert_eq!(x87("67d92b", 0xd9).map(|s| (s.kind, s.prefixes)), Some((FLDCW, ADDRESS32))); // fldcw [ebx] (I10)
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
        for hex in ["660fae10", "f30fae10"] {
            assert_eq!(d(hex, 0xae), None, "{hex}"); // 66, f3
        }
        assert_eq!(d("670fae10", 0xae), Some(System { prefixes: ADDRESS32, ..mem(LDMXCSR, 0, 0, 4) })); // ldmxcsr [eax] (I10)
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
        for hex in ["660fae00", "f20fae28"] {
            assert_eq!(d(hex, 0xae), None, "{hex}"); // NP forms with 66/F2
        }
        for hex in ["f30fae20", "f30faee0"] {
            assert_eq!(d(hex, 0xae).map(|s| s.kind), Some(UD), "{hex}"); // X1c: PTWRITE m, r (Intel PT not advertised)
        }
        assert_eq!(d("670fae00", 0xae), Some(System { prefixes: ADDRESS32, ..mem(FXSAVE, 0, 0, 4) })); // fxsave [eax] (I10)
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
        assert_eq!(d("670f0138", 0x01), Some(System { prefixes: ADDRESS32, ..invlpg(0, NO_REG, 0, 0, 4) })); // [eax] (I10)
    }

    #[test]
    fn unsupported_forms_are_refused() {
        let bytes = |hex: &str| -> Vec<u8> { (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect() };
        for (hex, map, op) in [
            ("0fb218", MAP_TWO, 0xb2),   // lss m16:32: not built
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
    fn x1c_forms() {
        let bytes = |hex: &str| -> Vec<u8> { (0..hex.len()).step_by(2).map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap()).collect() };
        let kind = |hex: &str, map, op| decode(&bytes(hex), map, op).map(|s| s.kind);
        for (hex, map, op) in [("06", MAP_PRIMARY, 0x06), ("1f", MAP_PRIMARY, 0x1f), ("9a00000000080000", MAP_PRIMARY, 0x9a), ("ea000000000800", MAP_PRIMARY, 0xea),
                               ("0fb4c0", MAP_TWO, 0xb4), ("0f01c7", MAP_TWO, 0x01), ("660f01cd", MAP_TWO, 0x01), ("f20f01d2", MAP_TWO, 0x01), ("0f01d3", MAP_TWO, 0x01),
                               ("f30f01ec", MAP_TWO, 0x01), ("0f012d00000000", MAP_TWO, 0x01), ("0f00f8", MAP_TWO, 0x00), ("0f003800", MAP_TWO, 0x00)] {
            assert_eq!(kind(hex, map, op), Some(UD), "{hex}");
        }
        assert_eq!(kind("0f06", MAP_TWO, 0x06), Some(CLTS));
        assert_eq!(kind("0f34", MAP_TWO, 0x34), Some(SYSENTER));
        let sys = |hex: &str, map, op| decode(&bytes(hex), map, op).map(|s| (s.kind, s.size, s.memory, s.reg, s.length));
        assert_eq!(sys("0f35", MAP_TWO, 0x35), Some((SYSEXIT, 4, 0, 0, 2)));
        assert_eq!(sys("480f35", MAP_TWO, 0x35), Some((SYSEXIT, 8, 0, 0, 3)));
        assert_eq!(sys("0f01e0", MAP_TWO, 0x01), Some((SMSW, 4, 0, 0, 3))); // smsw eax
        assert_eq!(sys("660f01e3", MAP_TWO, 0x01), Some((SMSW, 2, 0, 3, 4))); // smsw bx
        assert_eq!(sys("490f01e1", MAP_TWO, 0x01), Some((SMSW, 8, 0, 9, 4))); // smsw r9
        assert_eq!(sys("0f0120", MAP_TWO, 0x01).map(|s| (s.0, s.2)), Some((SMSW, 1))); // smsw [rax]
        // F2/F3 ignored (the reference AMD machine): rep smsw rsp, repne smsw rbp, rep smsw r9w, rep smsw [rax].
        assert_eq!(sys("f3480f01e4", MAP_TWO, 0x01), Some((SMSW, 8, 0, 4, 5)));
        assert_eq!(sys("f2480f01e5", MAP_TWO, 0x01), Some((SMSW, 8, 0, 5, 5)));
        assert_eq!(sys("f366410f01e1", MAP_TWO, 0x01), Some((SMSW, 2, 0, 9, 6)));
        assert_eq!(sys("f30f0120", MAP_TWO, 0x01).map(|s| (s.0, s.2)), Some((SMSW, 1)));
        assert_eq!(sys("0f01f0", MAP_TWO, 0x01).map(|s| (s.0, s.3)), Some((LMSW, 0))); // lmsw ax
        assert_eq!(sys("0f0130", MAP_TWO, 0x01).map(|s| (s.0, s.2)), Some((LMSW, 1))); // lmsw [rax]
        assert_eq!(kind("0f01c0", MAP_TWO, 0x01), Some(UD)); // ENCLV/VMX: unchanged
        assert_eq!(kind("0f01d0", MAP_TWO, 0x01), Some(XGETBV)); // unchanged
        assert_eq!(kind("0fb218", MAP_TWO, 0xb2), None); // lss m: not built
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
        assert_eq!(p("366e", 0x6e), string(OUTS, 1, 0, 0, 2)); // SS ignored: DS:RSI
        assert_eq!(p("64656e", 0x6e), string(OUTS, 1, 0, SREG_GS as u32, 3)); // the last FS/GS applies
        for (hex, op) in [("f26c", 0x6c), ("f3ec", 0xec), ("f2e480", 0xe4), ("ec", 0xed)] {
            assert_eq!(p(hex, op), None, "{hex}"); // REPNE, F3 on IN, FEX's opcode differs
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
        for hex in ["0f01c0", "0f01c5", "0f01c6", "f20f01e9", "0f01ca", "0f01cb", "0f01c8", "0f01c9", "0f01e8", "0f01ee", "0f01ef", "0f01c1", "0f01d9", "660f01cc", "f30f01fe", "f20f01ff", "f30f01ea", "f30f01e8", "f30f012b"] {
            assert_eq!(d(hex, 0x01), k(UD, hex.len() as u32 / 2), "{hex}");
        }
        assert_eq!(d("0f78c0", 0x78), k(UD, 2));
        // SSE4a EXTRQ (66, FEX's 66 overlay) and INSERTQ (F2, its F2 overlay, map 3): not advertised, #UD.
        assert_eq!(super::decode(&[0x66, 0x0f, 0x78, 0xc0, 0x00, 0x00], MAP_TWO_66, 0x78), k(UD, 3));
        assert_eq!(super::decode(&[0x66, 0x41, 0x0f, 0x79, 0xc0], MAP_TWO_66, 0x79), k(UD, 4));
        assert_eq!(super::decode(&[0xf2, 0x0f, 0x78, 0xc1, 0x00, 0x3f], 3, 0x78), k(UD, 3));
        assert_eq!(super::decode(&[0xf2, 0x41, 0x0f, 0x79, 0xc0], 3, 0x79), k(UD, 4));
        assert_eq!(super::decode(&[0x66, 0x0f, 0x38, 0x82, 0x00, 0x90], MAP_0F38, 0x82), k(UD, 4)); // invpcid
        // I9: MOVBE register forms, ADCX/ADOX, WRUSS: #UD; a memory MOVBE and CRC32 are never decoded here. SHA: #UD exactly
        // while the profile does not advertise it.
        assert_eq!(super::decode(&[0x48, 0x0f, 0x38, 0xf0, 0xc1], MAP_0F38, 0xf0), k(UD, 5));
        assert_eq!(super::decode(&[0x66, 0x0f, 0x38, 0xf1, 0xc8], MAP_0F38, 0xf1), k(UD, 5));
        assert_eq!(super::decode(&[0x0f, 0x38, 0xf0, 0x07], MAP_0F38, 0xf0), None);
        assert_eq!(super::decode(&[0xf2, 0x0f, 0x38, 0xf0, 0xc1], MAP_0F38, 0xf0), None);
        assert_eq!(super::decode(&[0x66, 0x48, 0x0f, 0x38, 0xf6, 0xc1], MAP_0F38, 0xf6), k(UD, 5));
        assert_eq!(super::decode(&[0xf3, 0x48, 0x0f, 0x38, 0xf6, 0x07], MAP_0F38, 0xf6), k(UD, 5));
        let sha = if super::advertised(super::SHA.unwrap()) { None } else { k(UD, 3) };
        assert_eq!(super::decode(&[0x0f, 0x38, 0xcb, 0xc1], MAP_0F38, 0xcb), sha);
        assert_eq!(super::decode(&[0x0f, 0x3a, 0xcc, 0xc1, 0x01], MAP_0F3A, 0xcc), sha);
        assert_eq!(super::decode(&[0x66, 0x0f, 0x3a, 0xcc, 0xc1, 0x01], MAP_0F3A, 0xcc), None);
        assert_eq!(d("f00f01f8", 0x01).map(|s| (s.kind, s.lock)), Some((SWAPGS, 1))); // #UD by LOCK
    }
}
