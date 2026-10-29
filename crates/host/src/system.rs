//! System instructions the resident executes as transactions (`natemu_abi::execution::System`): operand decoding of
//! the instruction fexc cut a block before. FEX's decoder classifies the instruction (fexc's `system_instruction` cut
//! with its opcode map and opcode); it does not decode the ModRM of `0f 20..23` (two bytes there), so the operands of
//! the supported forms are decoded here and must agree with FEX's opcode: MOV to/from CR0..CR15 (`0f 22`/`0f 20`) and
//! DR0..DR15 (`0f 23`/`0f 21`; ModRM always a register pair), INVLPG m (`0f 01 /7`, mod != 3), RDMSR (`0f 32`), WRMSR
//! (`0f 30`), RDPMC (`0f 33`), CPUID (`0f a2`), RDTSC (`0f 31`) and RDTSCP (`0f 01 f9`); the descriptor-table forms
//! SGDT/SIDT/LGDT/LIDT m (`0f 01 /0../3`, mod != 3), SLDT/STR/LLDT/LTR r/m (`0f 00 /0../3`), MOV r/m, Sreg (`8c`) and
//! MOV Sreg, r/m (`8e`), JMP/CALL far m16:32 or m16:64 (`ff /5`, `ff /3`, memory), RET far (`cb`, `ca iw`) and IRET (`cf`),
//! with 32- or 64-bit operand size (REX.W) where it matters, in 64-bit mode. Anything else (other system instructions,
//! F2/F3 prefixes, an address-size prefix on a memory operand, a 66 prefix on a far transfer, a CS/DS/ES/SS override on a
//! new memory form or an FS/GS override on INVLPG) is `None`: the vCPU stops with fexc's reason, as for code it cannot
//! compile.
use natemu_abi::execution::{SREG_FS, SREG_GS, System, system};

/// fexc's opcode maps (`FEXC_MAP_PRIMARY`, `FEXC_MAP_TWO`). The two-byte map is reported as 1 even with a 66 prefix (the
/// 66 overlay, map 4, is not reported for these opcodes).
const MAP_PRIMARY: u8 = 0;
const MAP_TWO: u8 = 1;

/// Decodes the instruction at the start of `bytes`, which fexc cut as a system instruction with opcode map `map` and
/// opcode `opcode`.
pub(crate) fn decode(bytes: &[u8], map: u8, opcode: u8) -> Option<System> {
    let (mut at, mut lock, mut segment, mut address_size, mut operand16) = (0, false, 0, false, false);
    loop {
        match *bytes.get(at)? {
            0xf0 => lock = true,
            0x66 => operand16 = true,
            0x67 => address_size = true,
            s @ (0x26 | 0x2e | 0x36 | 0x3e | 0x64 | 0x65) => segment = s,
            _ => break, // F2/F3 (mandatory-prefix overlays) are not supported forms
        }
        at += 1;
    }
    let rex = match bytes[at] {
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
            s.kind = *[system::SLDT, system::STR, system::LLDT, system::LTR].get(reg as usize)?;
            s.size = size;
        }
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
        // UD1/UD0's ModRM is not decoded: #UD leaves RIP at the instruction, its length is never used.
        (MAP_TWO, 0x0b | 0xb9 | 0xff) => s.kind = system::UD,
        // I5a: LDMXCSR/STMXCSR m32 (`0f ae /2`, `/3`); with a 66 prefix or a register operand another instruction.
        (MAP_TWO, 0xae) if !operand16 => {
            s.kind = match rm(bytes, &mut at, rex, &mut s)? {
                2 => system::LDMXCSR,
                3 => system::STMXCSR,
                _ => return None,
            };
            if s.memory == 0 {
                return None;
            }
        }
        _ => return None,
    }
    if s.memory == 1 {
        if address_size {
            return None;
        }
        // INVLPG keeps its null CS/DS/ES/SS overrides; the newer memory forms take only FS/GS (I7's rule for the others).
        s.fs_gs = match segment {
            0x64 => SREG_FS as u32,
            0x65 => SREG_GS as u32,
            _ if s.kind == system::INVLPG || segment == 0 => 0,
            _ => return None,
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

#[cfg(test)]
mod lock_tests {
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
        assert_eq!(d("650f0108", 0x01), Some(System { fs_gs: 5, ..mem(SIDT, 0, 0, 4) })); // sidt gs:[rax]
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
        for (hex, op) in [("66cb", 0xcb), ("66ff28", 0xff), ("ffe8", 0xff), ("ff20", 0xff), ("2e8e18", 0x8e), ("678e18", 0x8e), ("8ed8", 0x8c)] {
            assert_eq!(p(hex, op), None, "{hex}");
        }
        for (hex, op) in [("0f01d0", 0x01), ("0f0120", 0x01), ("0f00e0", 0x00), ("0f00e8", 0x00)] {
            assert_eq!(d(hex, op), None, "{hex}"); // xgetbv, smsw, verr, verw
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
        assert_eq!(super::decode(&[0xcd, 0x80], MAP_PRIMARY, 0xcd), s(INT, 0x80, 2));
        assert_eq!(super::decode(&[0xcd], MAP_PRIMARY, 0xcd), None); // truncated immediate
        assert_eq!(super::decode(&[0xce], MAP_PRIMARY, 0xce), s(INTO, 0, 1));
        assert_eq!(super::decode(&[0x0f, 0x0b], MAP_TWO, 0x0b), s(UD, 0, 2));
        assert_eq!(super::decode(&[0x0f, 0xb9, 0xc0], MAP_TWO, 0xb9), s(UD, 0, 2));
        assert_eq!(super::decode(&[0xf0, 0xcc], MAP_PRIMARY, 0xcc).map(|s| (s.kind, s.lock)), Some((INT3, 1))); // #UD by LOCK
    }

    #[test]
    fn mxcsr_forms() {
        assert_eq!(d("0fae10", 0xae), Some(mem(LDMXCSR, 0, 0, 3))); // ldmxcsr [rax]
        assert_eq!(d("410fae5c2408", 0xae), Some(mem(STMXCSR, 12, 8, 6))); // stmxcsr [r12+8]
        assert_eq!(d("640fae18", 0xae), Some(System { fs_gs: 4, ..mem(STMXCSR, 0, 0, 4) })); // stmxcsr fs:[rax]
        for hex in ["660fae10", "0faed0", "0fae38", "670fae10"] {
            assert_eq!(d(hex, 0xae), None, "{hex}"); // 66, register form, clflush, 32-bit address
        }
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
            ("0f01f8", MAP_TWO, 0x01),   // swapgs (mod 3)
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
}
