//! System instructions the resident executes as transactions (`natemu_abi::execution::System`): operand decoding of
//! the instruction fexc cut a block before. FEX's decoder classifies the instruction (fexc's `system_instruction` cut
//! with its opcode map and opcode); it does not decode the ModRM of `0f 20..23` (two bytes there), so the operands of
//! the supported forms are decoded here and must agree with FEX's opcode: MOV to/from CR0..CR15 (`0f 22`/`0f 20`,
//! ModRM always a register pair), INVLPG m (`0f 01 /7`, mod != 3), RDMSR (`0f 32`), WRMSR (`0f 30`), CPUID (`0f a2`),
//! RDTSC (`0f 31`) and RDTSCP (`0f 01 f9`) in 64-bit mode. Anything else (other system instructions, F2/F3 prefixes, an address-size prefix or an FS/GS override on
//! INVLPG) is `None`: the vCPU stops with fexc's reason, as for code it cannot compile.
use natemu_abi::execution::{System, system};

/// fexc's opcode map of the two-byte table (`FEXC_MAP_TWO`). It reports map 1 even with a 66 prefix (the 66 overlay,
/// map 4, is not reported for these opcodes).
const MAP_TWO: u8 = 1;

/// Decodes the instruction at the start of `bytes`, which fexc cut as a system instruction with opcode map `map` and
/// opcode `opcode`.
pub(crate) fn decode(bytes: &[u8], map: u8, opcode: u8) -> Option<System> {
    let (mut at, mut lock, mut segment, mut address_size) = (0, false, 0, false);
    loop {
        match *bytes.get(at)? {
            0xf0 => lock = true,
            0x66 => {}
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
    let (r, x, b) = (u32::from(rex >> 2 & 1) << 3, u32::from(rex >> 1 & 1) << 3, u32::from(rex & 1) << 3);
    if *bytes.get(at)? != 0x0f || *bytes.get(at + 1)? != opcode || map != MAP_TWO {
        return None;
    }
    at += 2;
    let mut s = System { lock: u32::from(lock), ..System::NONE };
    match opcode {
        0x20 | 0x22 => {
            // The mod field is ignored: the operand is always a register.
            let modrm = *bytes.get(at)?;
            at += 1;
            s.kind = if opcode == 0x22 { system::MOV_TO_CR } else { system::MOV_FROM_CR };
            (s.cr, s.reg) = (u32::from(modrm >> 3 & 7) | r, u32::from(modrm & 7) | b);
        }
        0x30 | 0x32 => s.kind = if opcode == 0x30 { system::WRMSR } else { system::RDMSR },
        0xa2 => s.kind = system::CPUID,
        0x31 => s.kind = system::RDTSC,
        0x01 if *bytes.get(at)? == 0xf9 => {
            at += 1;
            s.kind = system::RDTSCP;
        }
        0x01 => {
            let modrm = *bytes.get(at)?;
            at += 1;
            let (mode, rm) = (modrm >> 6, u32::from(modrm & 7));
            if modrm >> 3 & 7 != 7 || mode == 3 || address_size || matches!(segment, 0x64 | 0x65) {
                return None;
            }
            s.kind = system::INVLPG;
            let disp_bytes = match (mode, rm) {
                (0, 5) => {
                    s.base = system::RIP;
                    4
                }
                (_, 4) => {
                    let sib = *bytes.get(at)?;
                    at += 1;
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
            let disp = bytes.get(at..at + disp_bytes)?;
            s.disp = match disp_bytes {
                1 => i64::from(disp[0] as i8),
                4 => i64::from(i32::from_le_bytes(disp.try_into().unwrap())),
                _ => 0,
            };
            at += disp_bytes;
        }
        _ => return None,
    }
    s.length = at as u32;
    s.well_formed().then_some(s)
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
        System { kind: INVLPG, base, index, scale, disp, length, ..System::NONE }
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
            ("0f0110", MAP_TWO, 0x01),   // lgdt is /2
            ("670f0138", MAP_TWO, 0x01), // 32-bit address
            ("650f0138", MAP_TWO, 0x01), // GS override
            ("f30f22d8", MAP_TWO, 0x22), // F3
            ("0f21c0", MAP_TWO, 0x21),   // mov from a debug register
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
