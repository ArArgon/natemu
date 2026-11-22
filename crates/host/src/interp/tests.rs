//! The interpreter's decode against iced over every instruction the 16- and 32-bit decoders produce in the primary and 0F
//! maps (every opcode × every ModRM byte, under several prefix sets), the forms the surveyed stubs use, and the decode
//! request's runs.
use super::*;
use iced_x86::Code as C;
use iced_x86::Mnemonic as M;
use natemu_abi::execution::{
    Cpu, DR_RESET, FCW_INIT, LDTR_RESET, MXCSR_RESET, Segment, TABLE_RESET, TR_RESET,
};
use natemu_abi::interp::{Decode, MAX_FETCH, exec};
use std::collections::BTreeSet;

/// The first version's mnemonics (the plan's section 1 table) ...
const MNEMONICS: &[Mnemonic] = &[
    M::Add,
    M::Or,
    M::Adc,
    M::Sbb,
    M::And,
    M::Sub,
    M::Xor,
    M::Cmp,
    M::Test,
    M::Inc,
    M::Dec,
    M::Shl,
    M::Sal,
    M::Shr,
    M::Sar,
    M::Bt,
    M::Bts,
    M::Btr,
    M::Btc,
    M::Xadd,
    M::Mov,
    M::Movzx,
    M::Lea,
    M::Push,
    M::Pop,
    M::Pushf,
    M::Pushfd,
    M::Popf,
    M::Popfd,
    M::Cli,
    M::Cld,
    M::Nop,
    M::Pause,
    M::Jo,
    M::Jno,
    M::Jb,
    M::Jae,
    M::Je,
    M::Jne,
    M::Jbe,
    M::Ja,
    M::Js,
    M::Jns,
    M::Jp,
    M::Jnp,
    M::Jl,
    M::Jge,
    M::Jle,
    M::Jg,
    M::Jmp,
    M::Loopne,
    M::Loope,
    M::Loop,
    M::Jcxz,
    M::Jecxz,
    M::Call,
    M::Ret,
    M::Retf,
    M::Lgdt,
    M::Lidt,
    M::Rdmsr,
    M::Wrmsr,
    M::Cpuid,
    M::Hlt,
    M::Wbinvd,
];
/// ... and their forms that are not in it: PUSH/POP r/m and segment registers, JMP and CALL near indirect, far CALL, MOV DR.
const EXCLUDED: &[Code] = &[
    C::Push_rm16,
    C::Push_rm32,
    C::Pushw_ES,
    C::Pushw_CS,
    C::Pushw_SS,
    C::Pushw_DS,
    C::Pushw_FS,
    C::Pushw_GS,
    C::Pushd_ES,
    C::Pushd_CS,
    C::Pushd_SS,
    C::Pushd_DS,
    C::Pushd_FS,
    C::Pushd_GS,
    C::Pop_rm16,
    C::Pop_rm32,
    C::Popw_ES,
    C::Popw_SS,
    C::Popw_DS,
    C::Popw_FS,
    C::Popw_GS,
    C::Popd_ES,
    C::Popd_SS,
    C::Popd_DS,
    C::Popd_FS,
    C::Popd_GS,
    C::Jmp_rm16,
    C::Jmp_rm32,
    C::Call_rm16,
    C::Call_rm32,
    C::Call_m1616,
    C::Call_m1632,
    C::Call_ptr1616,
    C::Call_ptr1632,
    C::Mov_r32_dr,
    C::Mov_dr_r32,
];

/// Every instruction of the sweep: each prefix set, then optionally 0F, then every opcode and ModRM byte, then fixed bytes
/// for a SIB, displacement and immediate, in 16- and 32-bit code.
fn sweep(mut each: impl FnMut(u32, &[u8], &Instruction, DecoderError)) {
    const PREFIXES: &[&[u8]] = &[
        &[],
        &[0x66],
        &[0x67],
        &[0xf3],
        &[0xf2],
        &[0xf0],
        &[0xf0, 0x66],
        &[0x2e, 0x66, 0x67],
        &[0x36],
    ];
    const TAIL: [u8; 12] = [
        0x24, 0x9c, 0x10, 0x32, 0x54, 0x76, 0x98, 0xba, 0xdc, 0xfe, 0x11, 0x22,
    ];
    let mut b = Vec::with_capacity(32);
    for bits in [16, 32] {
        for p in PREFIXES {
            for escape in [false, true] {
                for opcode in 0..=255u8 {
                    for modrm in 0..=255u8 {
                        b.clear();
                        b.extend_from_slice(p);
                        if escape {
                            b.push(0x0f);
                        }
                        b.extend_from_slice(&[opcode, modrm]);
                        b.extend_from_slice(&TAIL);
                        let mut d = Decoder::new(bits, &b, DecoderOptions::NONE);
                        let i = d.decode();
                        each(bits, &b, &i, d.last_error());
                    }
                }
            }
        }
    }
}
/// A bank with values in every register (garbage in the upper halves) and distinct segment bases.
fn bank() -> Cpu {
    let mut x = 0x9e37_79b9_7f4a_7c15u64;
    let mut next = || {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        x
    };
    let mut seg = [Segment::NULL; 6];
    for s in &mut seg {
        (s.base, s.limit, s.attributes) = (next() & 0xffff_ffff, u32::MAX, 0x93);
    }
    Cpu {
        gpr: std::array::from_fn(|_| next()),
        rip: 0,
        rflags: 2,
        cr0: 0,
        cr3: 0,
        cr4: 0,
        efer: 0,
        cpl: 0,
        cr2: 0,
        cr8: 0,
        dr: DR_RESET,
        seg,
        ldtr: LDTR_RESET,
        tr: TR_RESET,
        gdtr: TABLE_RESET,
        idtr: TABLE_RESET,
        mov_ss: 0,
        xmm: [[0; 2]; 16],
        mxcsr: MXCSR_RESET,
        fcw: FCW_INIT,
        xcr0: 1,
        no_afp: 0,
        accurate_fp: 0,
        stack_traps: 0,
    }
}
fn prefixes(bytes: &[u8]) -> &[u8] {
    let n = bytes
        .iter()
        .take_while(|b| {
            matches!(
                b,
                0xf0 | 0xf2 | 0xf3 | 0x2e | 0x36 | 0x3e | 0x26 | 0x64 | 0x65 | 0x66 | 0x67
            )
        })
        .count();
    &bytes[..n]
}
/// A record's fields against what iced computes from the same instruction, each derived another way: nothing depends on
/// the IP, branch targets and the memory operand's linear address are iced's, the segment follows the default rule, the
/// stack slots are iced's stack pointer increment, Jcc's condition is the opcode's low nibble, LOCK is the prefix.
fn check(bits: u32, bytes: &[u8], i: &Instruction, r: &Insn, cpu: &Cpu) {
    let what = format!("{bits}-bit {:02x?} {:?}", &bytes[..i.len()], i.code());
    assert!(r.well_formed(), "{what}: {r:?}");
    let eip = if bits == 16 { 0xfff0 } else { 0x8765_4321 };
    let mut d = Decoder::with_ip(bits, bytes, eip, DecoderOptions::NONE);
    let j = d.decode();
    assert_eq!(
        insn(&j, d.last_error(), bytes, bits),
        *r,
        "{what}: decoded at another IP"
    );
    if matches!(r.op, op::JCC | op::JMP | op::CALL | op::LOOP) {
        let t = (eip as u32)
            .wrapping_add(u32::from(r.length))
            .wrapping_add(r.operands[0].value)
            & exec::mask(r.size);
        assert_eq!(u64::from(t), j.near_branch_target(), "{what}");
    }
    let p = prefixes(bytes);
    if let Some(k) = (0..i.op_count()).find(|&k| i.op_kind(k) == OpKind::Memory) {
        let value = |g: Register| {
            Some(if g.is_segment_register() {
                cpu.seg[g.number()].base
            } else if g.is_gpr16() {
                cpu.gpr[g.number()] & 0xffff
            } else {
                assert!(g.is_gpr32(), "{what}: {g:?}");
                cpu.gpr[g.number()] & 0xffff_ffff
            })
        };
        let va = i.virtual_address(k, 0, |g, _, _| value(g)).unwrap() as u32;
        assert_eq!(
            r.operands.iter().filter(|o| o.kind == kind::MEM).count(),
            1,
            "{what}"
        );
        // (iced's address of LEA's operand is its offset: LEA uses no segment.)
        if r.op == op::LEA {
            assert_eq!(exec::offset(cpu, r), va, "{what}");
        } else {
            let linear =
                (cpu.seg[r.segment as usize].base as u32).wrapping_add(exec::offset(cpu, r));
            assert_eq!(linear, va, "{what}");
            let sreg = |b: &u8| {
                [0x26, 0x2e, 0x36, 0x3e, 0x64, 0x65]
                    .iter()
                    .position(|s| s == b)
            };
            let stack = matches!(
                i.memory_base(),
                Register::BP | Register::SP | Register::EBP | Register::ESP
            );
            let default = if stack { 2 } else { 3 };
            assert_eq!(
                usize::from(r.segment),
                p.iter().rev().find_map(sreg).unwrap_or(default),
                "{what}"
            );
        }
    }
    let imm = |r: &Insn| {
        r.operands
            .iter()
            .find(|o| o.kind == kind::IMM)
            .map_or(0, |o| o.value as i32)
    };
    let spi = match r.op {
        op::PUSH | op::PUSHF | op::CALL => Some(-i32::from(r.size)),
        op::POP | op::POPF => Some(i32::from(r.size)),
        op::RET => Some(i32::from(r.size) + imm(r)),
        op::RET_FAR => Some(2 * i32::from(r.size) + imm(r)),
        _ => None,
    };
    if let Some(spi) = spi {
        assert_eq!(j.stack_pointer_increment(), spi, "{what}");
    }
    if r.op == op::JCC {
        let o = &bytes[p.len()..];
        assert_eq!(r.cc, if o[0] == 0x0f { o[1] } else { o[0] } & 15, "{what}");
    }
    assert_eq!(r.lock == 1, p.contains(&0xf0), "{what}");
    let osize = if (bits == 32) != p.contains(&0x66) {
        4
    } else {
        2
    };
    assert_eq!(
        r.address,
        if (bits == 32) != p.contains(&0x67) {
            4
        } else {
            2
        },
        "{what}"
    );
    if r.op >= op::PUSH && !matches!(r.op, op::MOV_SREG | op::MOV_TO_CR | op::MOV_FROM_CR) {
        assert_eq!(r.size, osize, "{what}");
    }
}

/// The mapping run over every iced `Code` the sweep decodes: those it accepts are exactly the first version's mnemonics
/// less [`EXCLUDED`] (every mnemonic with at least one form), each checked against iced ([`check`]); everything else is a
/// stop record carrying its bytes.
#[test]
fn maps_every_first_version_code() {
    let cpu = bank();
    let (mut decoded, mut accepted) = (BTreeSet::new(), BTreeSet::new());
    let mut checked = std::collections::HashMap::<(u32, Code), u32>::new();
    let mut instructions = 0;
    sweep(|bits, bytes, i, error| {
        instructions += 1;
        decoded.insert(i.code());
        let r = insn(i, error, bytes, bits);
        let n = usize::from(r.length);
        assert_eq!((n, &r.bytes[..n]), (i.len(), &bytes[..i.len()]));
        assert!(r.bytes[n..].iter().all(|&b| b == 0));
        // Every record the host can send is one the resident accepts, a stop's too.
        assert!(
            r.well_formed(),
            "{bits}-bit {:02x?} {:?}: {r:?}",
            &bytes[..n],
            i.code()
        );
        // F2/F3 on a form that does not take them are ignored: the same instruction (iced's code) without the prefix
        // has the same record but for its bytes.
        if matches!(bytes[0], 0xf2 | 0xf3) {
            let mut d = Decoder::new(bits, &bytes[1..], DecoderOptions::NONE);
            let j = d.decode();
            if j.code() == i.code() {
                let plain = insn(&j, d.last_error(), &bytes[1..], bits);
                let what = format!("{bits}-bit {:02x?} {:?}", &bytes[..n], i.code());
                assert_eq!(
                    Insn {
                        length: plain.length,
                        bytes: plain.bytes,
                        ..r
                    },
                    plain,
                    "{what}"
                );
            }
        }
        if matches!(r.op, op::OTHER | op::UD | op::TOO_LONG) {
            return;
        }
        accepted.insert(i.code());
        let k = checked.entry((bits, i.code())).or_default();
        *k += 1;
        if *k <= 24 {
            check(bits, bytes, i, &r, &cpu);
        }
    });
    let expected: BTreeSet<Code> = decoded
        .iter()
        .copied()
        .filter(|c| MNEMONICS.contains(&c.mnemonic()) && !EXCLUDED.contains(c))
        .collect();
    assert_eq!(
        accepted.difference(&expected).collect::<Vec<_>>(),
        Vec::<&Code>::new(),
        "accepted, not expected"
    );
    assert_eq!(
        expected.difference(&accepted).collect::<Vec<_>>(),
        Vec::<&Code>::new(),
        "expected, not accepted"
    );
    assert!(EXCLUDED.iter().all(|c| decoded.contains(c)));
    for m in MNEMONICS {
        assert!(accepted.iter().any(|c| c.mnemonic() == *m), "{m:?}");
    }
    assert!(
        accepted.len() > 300 && instructions > 2_300_000,
        "{} codes, {instructions}",
        accepted.len()
    );
}

fn one(bits: u32, bytes: &[u8]) -> Insn {
    let r = record(bytes, bits).expect("complete");
    assert_eq!(usize::from(r.length), bytes.len(), "{bytes:02x?}");
    r
}
fn operand(kind: u8, reg: u8, size: u8, value: u32) -> Operand {
    Operand {
        kind,
        reg,
        size,
        pad: 0,
        value,
    }
}
/// Forms the surveyed stubs use (plan section 9), field by field.
#[test]
fn stub_forms() {
    let mem = |size| operand(kind::MEM, 0, size, 0);
    let imm = |size, value| operand(kind::IMM, 0, size, value);
    let reg = |r, size| operand(kind::REG, r, size, 0);
    // OVMF: o32 lgdt [cs:bx] in 16-bit code.
    let r = one(16, &[0x66, 0x2e, 0x0f, 0x01, 0x17]);
    assert_eq!(
        (r.op, r.size, r.address, r.segment, r.base, r.index),
        (op::LGDT, 4, 2, 1, 3, NO_REG)
    );
    assert_eq!(r.operands[0], mem(6));
    // Linux's trampoline: lock btsl $0, tr_lock (16-bit code, a disp16 address).
    let r = one(16, &[0x66, 0xf0, 0x0f, 0xba, 0x2e, 0x34, 0x12, 0x00]);
    assert_eq!(
        (r.op, r.lock, r.size, r.address, r.base, r.disp, r.segment),
        (op::BTS, 1, 4, 2, NO_REG, 0x1234, 3)
    );
    assert_eq!(r.operands[..2], [mem(4), imm(1, 0)]);
    // ljmpl $0x10, $0x1000 from 16-bit code; jmp far [m16:32] in 32-bit code.
    let r = one(16, &[0x66, 0xea, 0x00, 0x10, 0x00, 0x00, 0x10, 0x00]);
    assert_eq!(
        (r.op, r.size, r.operands[..2].to_vec()),
        (op::JMP_FAR, 4, vec![imm(4, 0x1000), imm(2, 0x10)])
    );
    let r = one(32, &[0xff, 0x2d, 0x78, 0x56, 0x34, 0x12]);
    assert_eq!(
        (r.op, r.size, r.disp, r.operands[0]),
        (op::JMP_FAR, 4, 0x1234_5678, mem(6))
    );
    // OVMF's page-table build: movl $imm32, (%edi); bt [mem], ecx.
    let r = one(32, &[0xc7, 0x07, 0x78, 0x56, 0x34, 0x12]);
    assert_eq!(
        (r.op, r.size, r.base, r.operands[..2].to_vec()),
        (op::MOV, 4, 7, vec![mem(4), imm(4, 0x1234_5678)])
    );
    let r = one(32, &[0x0f, 0xa3, 0x08]);
    assert_eq!(
        (r.op, r.base, r.operands[..2].to_vec()),
        (op::BT, 0, vec![mem(4), reg(1, 4)])
    );
    // kvm-unit-tests: lock xaddl %eax, (%ebx).
    let r = one(32, &[0xf0, 0x0f, 0xc1, 0x03]);
    assert_eq!(
        (r.op, r.lock, r.base, r.operands[..2].to_vec()),
        (op::XADD, 1, 3, vec![mem(4), reg(0, 4)])
    );
    // jne $-2 (16-bit), loop with 67 (ECX), mov %eax, %cr0, mov %ax, %ss, mov %cs, %eax and %ax.
    let r = one(16, &[0x75, 0xfe]);
    assert_eq!(
        (r.op, r.cc, r.size, r.operands[0].value),
        (op::JCC, 5, 2, (-2i32) as u32)
    );
    let r = one(16, &[0x67, 0xe2, 0xfe]);
    assert_eq!(
        (r.op, r.cc, r.size, r.address, r.operands[0].value),
        (op::LOOP, 2, 2, 4, (-2i32) as u32)
    );
    let r = one(16, &[0x0f, 0x22, 0xc0]);
    assert_eq!(
        (r.op, r.size, r.operands[..2].to_vec()),
        (
            op::MOV_TO_CR,
            4,
            vec![operand(kind::CR, 0, 4, 0), reg(0, 4)]
        )
    );
    let r = one(16, &[0x8e, 0xd0]);
    assert_eq!(
        (r.op, r.size, r.operands[..2].to_vec()),
        (
            op::MOV_SREG,
            2,
            vec![operand(kind::SREG, 2, 2, 0), reg(0, 2)]
        )
    );
    let r = one(32, &[0x8c, 0xc8]);
    assert_eq!(
        (r.op, r.size, r.operands[..2].to_vec()),
        (op::MOV, 4, vec![reg(0, 4), operand(kind::SREG, 1, 2, 0)])
    );
    let r = one(32, &[0x66, 0x8c, 0xc8]);
    assert_eq!((r.op, r.size, r.operands[0]), (op::MOV, 2, reg(0, 2)));
    // pushw $-1 (sign-extended), push $0x10, retf, lretl $8, shl $1, pause, wrmsr.
    let r = one(16, &[0x6a, 0xff]);
    assert_eq!(
        (r.op, r.size, r.operands[0]),
        (op::PUSH, 2, imm(2, u32::MAX))
    );
    let r = one(32, &[0x6a, 0x10]);
    assert_eq!((r.op, r.size, r.operands[0]), (op::PUSH, 4, imm(4, 0x10)));
    assert_eq!(
        (one(16, &[0xcb]).op, one(16, &[0xcb]).size),
        (op::RET_FAR, 2)
    );
    let r = one(16, &[0x66, 0xca, 0x08, 0x00]);
    assert_eq!((r.op, r.size, r.operands[0]), (op::RET_FAR, 4, imm(2, 8)));
    let r = one(32, &[0xd1, 0xe0]);
    assert_eq!(
        (r.op, r.size, r.operands[..2].to_vec()),
        (op::SHL, 4, vec![reg(0, 4), imm(1, 1)])
    );
    assert_eq!(one(16, &[0xf3, 0x90]).op, op::PAUSE);
    assert_eq!(one(16, &[0x0f, 0x30]).op, op::WRMSR);
    // Stops: UD2, MOV CS, LOCK on a register destination (#UD); MUL (not in the first version); fifteen prefixes. Each is
    // well formed, with the sizes its prefixes give, so the resident gets the typed stop, not a bad reply.
    for b in [&[0x0f, 0x0b][..], &[0x8e, 0xc8], &[0xf0, 0x01, 0xc0]] {
        let r = one(32, b);
        assert_eq!(
            (r.op, &r.bytes[..b.len()], r.well_formed()),
            (op::UD, b, true)
        );
    }
    let r = one(16, &[0x66, 0x67, 0xf7, 0xe0]);
    assert_eq!(
        (r.op, r.size, r.address, r.well_formed()),
        (op::OTHER, 4, 4, true)
    );
    let mut long = [0x66; 16];
    long[15] = 0x90;
    let r = record(&long, 32).unwrap();
    assert_eq!(
        (r.op, r.length, r.bytes[..15].to_vec(), r.well_formed()),
        (op::TOO_LONG, 15, vec![0x66; 15], true)
    );
    // Fifteen bytes and no instruction: 13 prefixes then MOV CS ends at the 15th byte (#UD); 13 then 0F 04 needs a 16th by
    // iced's reading (it takes a ModRM byte for 0F 04), so too long.
    let mut b = [0x66; 15];
    b[13..].copy_from_slice(&[0x8e, 0xc8]);
    assert_eq!((one(32, &b).op, one(32, &b).length), (op::UD, 15));
    b[13..].copy_from_slice(&[0x0f, 0x04]);
    assert_eq!(record(&b, 32).unwrap().op, op::TOO_LONG);
}

/// A request's straight run: it ends after a control transfer, a resident operation or a stop, at `max`, or before an
/// instruction not all of whose bytes were supplied; none at all when the first is cut (truncated).
#[test]
fn runs() {
    let ops = |d: &Decoded| {
        d.records[..d.count as usize]
            .iter()
            .map(|r| r.op)
            .collect::<Vec<_>>()
    };
    // nop; inc ax; jmp $; nop
    let code = [0x90, 0x40, 0xeb, 0xfe, 0x90];
    assert_eq!(ops(&run(&code, 16, MAX_RUN)), [op::NOP, op::INC, op::JMP]);
    assert_eq!(ops(&run(&code, 16, 2)), [op::NOP, op::INC]);
    // A run's records hold their own bytes.
    let d = run(&code, 16, MAX_RUN);
    assert_eq!(d.records[2].bytes[..2], [0xeb, 0xfe]);
    // An instruction cut at the end of the bytes ends the run before it; the first one cut: nothing.
    assert_eq!(ops(&run(&[0x90, 0x66], 16, MAX_RUN)), [op::NOP]);
    assert_eq!(ops(&run(&[0x90, 0xb8, 0x01], 16, MAX_RUN)), [op::NOP]);
    assert_eq!(run(&[0x0f], 32, MAX_RUN).count, 0);
    assert_eq!(run(&[], 32, MAX_RUN).count, 0);
    // A stop ends the run (MUL), a resident operation too (CPUID), as does an invalid encoding.
    assert_eq!(
        ops(&run(&[0x40, 0xf7, 0xe0, 0x90], 32, MAX_RUN)),
        [op::INC, op::OTHER]
    );
    assert_eq!(ops(&run(&[0x0f, 0xa2, 0x90], 32, MAX_RUN)), [op::CPUID]);
    assert_eq!(ops(&run(&[0x0f, 0xff, 0x00, 0x90], 32, MAX_RUN)), [op::UD]);
    // At most MAX_RUN records, from as many bytes as a request carries.
    assert_eq!(run(&[0x90; MAX_FETCH], 32, MAX_RUN).count as usize, MAX_RUN);
    // A stop ends the run before the instruction at it (none is made for its address), not before one it falls inside;
    // the first is always made (the hook answered a stop there before the decode).
    let code = [0x90, 0xb8, 1, 0, 0, 0, 0x90, 0x90]; // nop; mov eax, 1; nop; nop
    assert_eq!(ops(&run_to(&code, 32, MAX_RUN, 6)), [op::NOP, op::MOV]);
    assert_eq!(ops(&run_to(&code, 32, MAX_RUN, 3)), [op::NOP, op::MOV]);
    assert_eq!(ops(&run_to(&code, 32, MAX_RUN, 1)), [op::NOP]);
    assert_eq!(ops(&run_to(&code, 32, MAX_RUN, 0)), [op::NOP]);
    assert_eq!(run_to(&code, 32, MAX_RUN, usize::MAX).count, 4);
    // The request's checks.
    let q = |bits, max, available| {
        let mut d = Decode {
            bits,
            max,
            available,
            pad: 0,
            bytes: [0x90; MAX_FETCH],
        };
        d.bytes[1] = 0xc3;
        answer(&d, usize::MAX).map(|d| d.count)
    };
    assert_eq!(q(16, 4, 15), Ok(2));
    assert_eq!(q(32, 1, 0), Ok(0));
    for bad in [
        q(64, 4, 15),
        q(8, 4, 15),
        q(32, 0, 15),
        q(32, MAX_RUN as u32 + 1, 15),
        q(32, 4, MAX_FETCH as u32 + 1),
    ] {
        assert!(bad.is_err());
    }
}
