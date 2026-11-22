//! The executor core against an x86 processor (the ALU, INC/DEC, shift, bit-test and XADD forms: vectors the reference
//! machine produced, `tools/models/interp_vectors_hw.c`) and against the SDM's rules, written here independently of the core
//! (segmentation, the stack, branches, the bit string, LOCK, check then commit).
extern crate std;
use super::exec::*;
use super::{Insn, NO_REG, Operand, kind, op, register};
use crate::execution::*;
use std::{collections::HashMap, vec::Vec};

/// A real-mode bank: every segment's selector × 16 its base, 64 KiB limits, CS 0x200, IP 0x100.
fn real() -> Cpu {
    let seg = |selector: u16, attributes| Segment {
        selector,
        attributes,
        limit: 0xffff,
        base: u64::from(selector) << 4,
    };
    Cpu {
        gpr: [0; 16],
        rip: 0x100,
        rflags: 2,
        cr0: 0x10,
        cr3: 0,
        cr4: 0,
        efer: 0,
        cpl: 0,
        cr2: 0,
        cr8: 0,
        dr: DR_RESET,
        seg: [
            seg(0x100, 0x93),
            seg(0x200, 0x9b),
            seg(0x300, 0x93),
            seg(0x400, 0x93),
            seg(0x500, 0x93),
            seg(0x600, 0x93),
        ],
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
/// A flat 32-bit protected-mode bank (CPL 0, 4 GiB segments).
fn flat() -> Cpu {
    let mut c = real();
    c.cr0 |= CR0_PE;
    for (k, s) in c.seg.iter_mut().enumerate() {
        let code = k == SREG_CS;
        *s = Segment {
            selector: if code { 8 } else { 0x10 },
            attributes: if code { 0x9b } else { 0x93 } | ATTR_D | 1 << 15,
            limit: u32::MAX,
            base: 0,
        };
    }
    c
}
const REAL: Context = Context {
    protected: false,
    code32: false,
    stack32: false,
    cpl: 0,
};
const P32: Context = Context {
    protected: true,
    code32: true,
    stack32: true,
    cpl: 0,
};

/// Sparse memory by linear address: the pages in `absent` give #PF; the next `contended` compare-exchanges find that another
/// CPU stored first (it added 1).
#[derive(Default, Clone, PartialEq, Debug)]
struct Mem {
    bytes: HashMap<u32, u8>,
    absent: Vec<u32>,
    contended: u32,
}
impl Mem {
    fn byte(&self, a: u32) -> u8 {
        self.bytes.get(&a).copied().unwrap_or(0)
    }
}
impl Memory for Mem {
    fn check(&mut self, linear: u32, size: u32, write: bool) -> Result<(), Exit> {
        assert!((1..=6).contains(&size));
        match (0..size)
            .map(|k| linear.wrapping_add(k))
            .find(|a| self.absent.contains(&(a >> 12)))
        {
            Some(address) => Err(Exit::Fault {
                vector: 14,
                error: u32::from(write) << 1,
                address,
            }),
            None => Ok(()),
        }
    }
    fn read(&mut self, linear: u32, size: u8) -> u32 {
        (0..u32::from(size)).fold(0, |v, k| {
            v | u32::from(self.byte(linear.wrapping_add(k))) << (8 * k)
        })
    }
    fn write(&mut self, linear: u32, size: u8, value: u32) {
        for k in 0..u32::from(size) {
            self.bytes
                .insert(linear.wrapping_add(k), (value >> (8 * k)) as u8);
        }
    }
    fn compare_exchange(&mut self, linear: u32, size: u8, old: u32, new: u32) -> u32 {
        let now = self.read(linear, size);
        if self.contended > 0 {
            self.contended -= 1;
            self.write(linear, size, now.wrapping_add(1));
            return now.wrapping_add(1);
        }
        if now == old {
            self.write(linear, size, new);
        }
        now
    }
}

fn reg(r: u8, size: u8) -> Operand {
    Operand {
        kind: kind::REG,
        reg: r,
        size,
        pad: 0,
        value: 0,
    }
}
fn imm(value: u32) -> Operand {
    Operand {
        kind: kind::IMM,
        reg: 0,
        size: 4,
        pad: 0,
        value,
    }
}
fn mem(size: u8) -> Operand {
    Operand {
        kind: kind::MEM,
        size,
        ..Operand::NONE
    }
}
fn sreg(r: usize) -> Operand {
    Operand {
        kind: kind::SREG,
        reg: r as u8,
        size: 2,
        ..Operand::NONE
    }
}
/// A two-byte record of `o` at operation size `size`, 16-bit addressing through DS with no base.
fn insn(o: u8, size: u8, operands: &[Operand]) -> Insn {
    let mut i = Insn {
        op: o,
        length: 2,
        size,
        address: 2,
        segment: SREG_DS as u8,
        ..Insn::NONE
    };
    i.operands[..operands.len()].copy_from_slice(operands);
    assert!(i.well_formed(), "{i:?}");
    i
}
fn at(mut i: Insn, base: u8, index: u8, scale: u8, disp: u32) -> Insn {
    (i.base, i.index, i.scale, i.disp) = (base, index, scale, disp);
    i
}
fn run(c: &mut Cpu, ctx: Context, i: &Insn, m: &mut Mem) -> Result<Outcome, Exit> {
    step(c, ctx, i, m)
}
/// A fault that leaves the bank and memory exactly as they were.
fn faults(c: &Cpu, ctx: Context, i: &Insn, m: &Mem) -> Exit {
    let (mut c2, mut m2) = (*c, m.clone());
    let e = run(&mut c2, ctx, i, &mut m2).expect_err("a fault");
    assert_eq!((&c2, &m2), (c, m), "a fault committed something: {e:?}");
    e
}
const GP: Exit = Exit::fault(13);
const SS: Exit = Exit::fault(12);
const UD: Exit = Exit::fault(6);

/// The vectors the reference machine's processor produced (`tools/models/interp_vectors_hw.c`): `op bits a b flags result
/// b_out flags_out`. Each runs as a register form (destination EAX, source ECX, a shift's count CL); results, XADD's source and
/// every flag the SDM defines must match, and nothing outside the arithmetic flags may change.
#[test]
fn matches_the_hardware() {
    const VECTORS: &str = include_str!("../../tests/interp_vectors.txt");
    // In operation-number order: ADD (0) .. XADD (18).
    let names = [
        "add", "or", "adc", "sbb", "and", "sub", "xor", "cmp", "test", "inc", "dec", "shl", "shr",
        "sar", "bt", "bts", "btr", "btc", "xadd",
    ];
    let mut seen = [0; 19];
    for line in VECTORS.lines().filter(|l| !l.starts_with('#')) {
        let f: Vec<&str> = line.split(' ').collect();
        let hex = |s: &str| u32::from_str_radix(s, 16).unwrap();
        let o = names.iter().position(|&n| n == f[0]).unwrap() as u8;
        let size = (f[1].parse::<u32>().unwrap() / 8) as u8;
        let (a, b, flags, result, b_out, flags_out) = (
            hex(f[2]),
            hex(f[3]),
            hex(f[4]),
            hex(f[5]),
            hex(f[6]),
            hex(f[7]),
        );
        let source = match o {
            op::SHL..=op::SAR => reg(1, 1),
            op::INC | op::DEC => Operand::NONE,
            _ => reg(1, size),
        };
        let mut c = real();
        (c.gpr[0], c.gpr[1], c.rflags) = (a.into(), b.into(), flags.into());
        let i = insn(o, size, &[reg(0, size), source]);
        assert_eq!(
            run(&mut c, REAL, &i, &mut Mem::default()),
            Ok(Outcome::Done),
            "{line}"
        );
        let (count, bits) = (b & 31, u32::from(size) * 8);
        let undefined = match o {
            op::OR | op::AND | op::XOR | op::TEST => AF,
            op::SHL..=op::SAR if count == 0 => 0,
            op::SHL..=op::SAR => {
                AF | if count != 1 { OF } else { 0 }
                    | if o != op::SAR && count >= bits { CF } else { 0 }
            }
            op::BT..=op::BTC => OF | SF | AF | PF,
            _ => 0,
        };
        let defined = ARITHMETIC & !undefined;
        assert_eq!(
            (register(&c, 0, size), c.rflags as u32 & defined),
            (result, flags_out & defined),
            "{line}"
        );
        assert_eq!(c.rflags as u32 & !ARITHMETIC, flags & !ARITHMETIC, "{line}");
        assert_eq!(
            register(&c, 1, 4),
            if o == op::XADD { b_out } else { b },
            "{line}"
        );
        assert_eq!(c.rip, 0x102);
        seen[o as usize] += 1;
    }
    assert!(seen.iter().all(|&n| n >= 128), "{seen:?}");
}

/// natemu's fixed choice for the flags the SDM leaves undefined (the module documentation).
#[test]
fn undefined_flags_are_the_documented_choice() {
    let flags = |o: u8, a: u32, b: u32, size: u8, f_in: u64| {
        let mut c = real();
        (c.gpr[0], c.gpr[1], c.rflags) = (a.into(), b.into(), f_in | 2);
        let source = if matches!(o, op::SHL..=op::SAR) {
            reg(1, 1)
        } else {
            reg(1, size)
        };
        run(
            &mut c,
            REAL,
            &insn(o, size, &[reg(0, size), source]),
            &mut Mem::default(),
        )
        .unwrap();
        c.rflags as u32
    };
    for o in [op::AND, op::OR, op::XOR, op::TEST] {
        assert_eq!(flags(o, 0x1f, 0x11, 1, AF.into()) & AF, 0);
    }
    // SHL 0x60 by 2 (8 bits): result 0x80, CF 1 (bit 6 out): OF = SF ^ CF = 0. SHR 0x80 by 3: OF = the sign, 1. SAR: OF 0.
    assert_eq!(
        flags(op::SHL, 0x60, 2, 1, (AF | OF).into()) & (AF | OF | CF),
        CF
    );
    assert_eq!(flags(op::SHR, 0x80, 3, 1, AF.into()) & (AF | OF), OF);
    assert_eq!(flags(op::SAR, 0x80, 3, 1, OF.into()) & OF, 0);
    // SHL/SHR by the width or more: zeros shifted in (CF 0 past it).
    assert_eq!(flags(op::SHL, 0xff, 9, 1, CF.into()) & CF, 0);
    assert_eq!(flags(op::SHR, 0xff, 8, 1, 0) & CF, CF);
    // BT*: only CF.
    let others = OF | SF | AF | PF | ZF;
    for o in [op::BT, op::BTS, op::BTR, op::BTC] {
        assert_eq!(
            flags(o, 1, 0, 4, others.into()) & (others | CF),
            others | CF
        );
        assert_eq!(flags(o, 0, 0, 4, 0) & (others | CF), 0);
    }
}

/// Jcc's sixteen conditions over every combination of CF, PF, ZF, SF and OF, against the SDM's table (Vol.2 Jcc).
#[test]
fn conditions_follow_the_sdm_table() {
    for bits in 0..32u32 {
        let [cf, pf, zf, sf, of] = [0, 1, 2, 3, 4].map(|k| bits >> k & 1 != 0);
        let flags = u64::from(cf)
            | u64::from(pf) << 2
            | u64::from(zf) << 6
            | u64::from(sf) << 7
            | u64::from(of) << 11;
        let table = [
            of,              // O
            !of,             // NO
            cf,              // B
            !cf,             // AE
            zf,              // E
            !zf,             // NE
            cf || zf,        // BE
            !cf && !zf,      // A
            sf,              // S
            !sf,             // NS
            pf,              // P
            !pf,             // NP
            sf != of,        // L
            sf == of,        // GE
            zf || sf != of,  // LE
            !zf && sf == of, // G
        ];
        for (cc, &taken) in table.iter().enumerate() {
            let mut c = real();
            c.rflags = flags | 2;
            let mut i = insn(op::JCC, 2, &[imm(0x10)]);
            i.cc = cc as u8;
            run(&mut c, REAL, &i, &mut Mem::default()).unwrap();
            assert_eq!(
                c.rip,
                if taken { 0x112 } else { 0x102 },
                "cc {cc} flags {flags:#x}"
            );
        }
    }
}

/// Register operands: AH..BH are bits 15:8, 8- and 16-bit writes merge, 32-bit writes zero-extend.
#[test]
fn register_operands() {
    let mut c = real();
    c.gpr[0] = 0xdead_beef_1234_5678;
    c.gpr[1] = 0xffff_ffff_0000_9a00;
    // ADD AH, CH: 0x56 + 0x9a = 0xf0.
    run(
        &mut c,
        REAL,
        &insn(op::ADD, 1, &[reg(4, 1), reg(5, 1)]),
        &mut Mem::default(),
    )
    .unwrap();
    assert_eq!(c.gpr[0], 0xdead_beef_1234_f078);
    run(
        &mut c,
        REAL,
        &insn(op::MOV, 2, &[reg(0, 2), imm(0xffff_1111)]),
        &mut Mem::default(),
    )
    .unwrap();
    assert_eq!(c.gpr[0], 0xdead_beef_1234_1111);
    run(
        &mut c,
        REAL,
        &insn(op::MOV, 4, &[reg(0, 4), imm(7)]),
        &mut Mem::default(),
    )
    .unwrap();
    assert_eq!(c.gpr[0], 7);
    // MOVZX EAX, CH; MOVZX AX, byte [0] keeps EAX's upper half.
    run(
        &mut c,
        REAL,
        &insn(op::MOVZX, 4, &[reg(0, 4), reg(5, 1)]),
        &mut Mem::default(),
    )
    .unwrap();
    assert_eq!(c.gpr[0], 0x9a);
    c.gpr[0] = 0x5555_0000;
    let mut m = Mem::default();
    m.write(0x4000, 1, 0xfe);
    run(
        &mut c,
        REAL,
        &insn(op::MOVZX, 2, &[reg(0, 2), mem(1)]),
        &mut m,
    )
    .unwrap();
    assert_eq!(c.gpr[0], 0x5555_00fe);
}

/// The memory operand's offset wraps at the address size; linear = base + offset wraps at 32 bits.
#[test]
fn effective_addresses() {
    let mut c = real();
    let i16 = |base, index, disp| at(insn(op::MOV, 2, &[reg(0, 2), mem(2)]), base, index, 0, disp);
    // [BX+SI]: 0xffff + 2 wraps to 1; [BP+DI-2] with BP 1: 0xffff (SS by default, but the offset is the record's).
    (c.gpr[3], c.gpr[6]) = (0x1_ffff, 2);
    assert_eq!(offset(&c, &i16(3, 6, 0)), 1);
    (c.gpr[5], c.gpr[7]) = (1, 0);
    assert_eq!(offset(&c, &i16(5, 7, 0xfffe)), 0xffff);
    // [EAX + ECX*4 + disp] at 32 bits.
    let mut i = at(insn(op::MOV, 4, &[reg(0, 4), mem(4)]), 0, 1, 2, 0x10);
    i.address = 4;
    (c.gpr[0], c.gpr[1]) = (0xffff_fff0, 0x4000_0001);
    assert_eq!(offset(&c, &i), 0x0000_0004);
    // A base near 4 GiB: the linear address wraps.
    c.seg[SREG_DS].base = 0xffff_f000;
    c.seg[SREG_DS].limit = 0xffff;
    assert_eq!(linear(&c, REAL, SREG_DS, 0x2000, 4, false), Ok(0x1000));
    // Real mode reaches past 1 MiB (no A20 masking): FFFF:FFFF is 10FFEFh.
    c.seg[SREG_DS].base = 0xffff0;
    assert_eq!(linear(&c, REAL, SREG_DS, 0xffff, 1, true), Ok(0x10_ffef));
}

/// The segment checks against the hidden descriptor (SDM Vol.3 5.3, 5.4, 5.5), with #SS(0) for SS.
#[test]
fn segment_checks() {
    let mut c = flat();
    let check = |c: &Cpu, ctx, s: usize, offset: u32, size: u32, write| {
        linear(c, ctx, s, offset, size, write).map(|_| ())
    };
    let set = |c: &mut Cpu, s: usize, attributes: u16, limit: u32| {
        (c.seg[s].attributes, c.seg[s].limit) = (attributes, limit);
    };
    // Expand-up, byte limit 0xfff: the last byte at 0xfff.
    set(&mut c, SREG_DS, 0x93, 0xfff);
    assert_eq!(check(&c, P32, SREG_DS, 0xffe, 2, true), Ok(()));
    assert_eq!(check(&c, P32, SREG_DS, 0xfff, 2, false), Err(GP));
    assert_eq!(check(&c, P32, SREG_DS, 0xffff_ffff, 2, false), Err(GP));
    // Read-only data: no write; execute-only code: no read; readable code: read, no write.
    set(&mut c, SREG_DS, 0x91, 0xffff);
    assert_eq!(
        (
            check(&c, P32, SREG_DS, 0, 4, false),
            check(&c, P32, SREG_DS, 0, 4, true)
        ),
        (Ok(()), Err(GP))
    );
    set(&mut c, SREG_DS, 0x99, 0xffff);
    assert_eq!(check(&c, P32, SREG_DS, 0, 1, false), Err(GP));
    set(&mut c, SREG_DS, 0x9b, 0xffff);
    assert_eq!(
        (
            check(&c, P32, SREG_DS, 0, 4, false),
            check(&c, P32, SREG_DS, 0, 1, true)
        ),
        (Ok(()), Err(GP))
    );
    // Unusable (a null load): #GP(0), or #SS(0) through SS.
    set(&mut c, SREG_DS, 0, 0xffff);
    assert_eq!(check(&c, P32, SREG_DS, 0, 1, false), Err(GP));
    set(&mut c, SREG_SS, 0, 0xffff);
    assert_eq!(check(&c, P32, SREG_SS, 0, 1, false), Err(SS));
    // Expand-down, B = 0: offsets limit+1 ..= 0xffff; B = 1: up to 0xffffffff.
    set(&mut c, SREG_SS, 0x97, 0xfff);
    assert_eq!(check(&c, P32, SREG_SS, 0xfff, 1, true), Err(SS));
    assert_eq!(check(&c, P32, SREG_SS, 0x1000, 2, true), Ok(()));
    assert_eq!(check(&c, P32, SREG_SS, 0xfffe, 2, true), Ok(()));
    assert_eq!(check(&c, P32, SREG_SS, 0xffff, 2, true), Err(SS));
    set(&mut c, SREG_SS, 0x97 | ATTR_D, 0xfff);
    assert_eq!(check(&c, P32, SREG_SS, 0xffff, 2, true), Ok(()));
    assert_eq!(check(&c, P32, SREG_SS, 0xffff_fffe, 2, true), Ok(()));
    assert_eq!(check(&c, P32, SREG_SS, 0xffff_ffff, 2, true), Err(SS));
    // Real mode: the hidden limit alone (rights and expand-down are not looked at).
    let mut r = real();
    set(&mut r, SREG_DS, 0x91, 0xffff);
    assert_eq!(check(&r, REAL, SREG_DS, 0xfffe, 2, true), Ok(()));
    assert_eq!(check(&r, REAL, SREG_DS, 0xffff, 2, false), Err(GP));
    assert_eq!(check(&r, REAL, SREG_SS, 0xffff, 2, false), Err(SS));
    set(&mut r, SREG_DS, 0x93, 0xffff_ffff);
    assert_eq!(check(&r, REAL, SREG_DS, 0x10_0000, 4, false), Ok(()));
}

/// Memory operands go through the segment, then the memory's check; a fault commits nothing (registers, flags, memory).
#[test]
fn check_then_commit() {
    let mut c = real();
    c.seg[SREG_DS].base = 0x1_0000;
    c.gpr[3] = 0x0ffe;
    let mut m = Mem::default();
    m.absent.push(0x11);
    // ADD [BX], EAX: four bytes at 0x10ffe cross into the absent page 0x11000: #PF with the address of the first absent byte.
    let add = at(insn(op::ADD, 4, &[mem(4), reg(0, 4)]), 3, NO_REG, 0, 0);
    assert_eq!(
        faults(&c, REAL, &add, &m),
        Exit::Fault {
            vector: 14,
            error: 2,
            address: 0x1_1000
        }
    );
    // Past the limit: #GP(0) before the memory is asked.
    c.gpr[3] = 0xfffe;
    assert_eq!(faults(&c, REAL, &add, &m), GP);
    // Through SS (an override or a BP base): #SS(0).
    let mut i = add;
    i.segment = SREG_SS as u8;
    assert_eq!(faults(&c, REAL, &i, &m), SS);
    // A MOV store into a page that is present: committed.
    c.gpr[3] = 0x10;
    c.gpr[0] = 0x1122_3344;
    let mov = at(insn(op::MOV, 4, &[mem(4), reg(0, 4)]), 3, NO_REG, 0, 0);
    run(&mut c, REAL, &mov, &mut m).unwrap();
    assert_eq!(m.read(0x1_0010, 4), 0x1122_3344);
}

/// PUSH, POP, PUSHF and POPF on 16- and 32-bit stacks (SDM Vol.2 PUSH, POP, PUSHF, POPF).
#[test]
fn stack() {
    let mut c = real();
    let mut m = Mem::default();
    let ss = c.seg[SREG_SS].base as u32;
    // A 16-bit stack wraps at 64 KiB and keeps ESP's upper half; PUSH SP pushes the value before the push.
    c.gpr[4] = 0x1234_0000;
    run(&mut c, REAL, &insn(op::PUSH, 2, &[reg(4, 2)]), &mut m).unwrap();
    assert_eq!((c.gpr[4], m.read(ss + 0xfffe, 2)), (0x1234_fffe, 0));
    // POP SP: the popped value.
    m.write(ss + 0xfffe, 2, 0x4321);
    run(&mut c, REAL, &insn(op::POP, 2, &[reg(4, 2)]), &mut m).unwrap();
    assert_eq!(c.gpr[4], 0x1234_4321);
    // A word slot at 0xffff passes the 64 KiB limit: #SS(0), nothing changed.
    c.gpr[4] = 1;
    assert_eq!(faults(&c, REAL, &insn(op::PUSH, 2, &[imm(5)]), &m), SS);
    // A 32-bit push in 16-bit code (66): four bytes, SP - 4.
    c.gpr[4] = 0x100;
    run(
        &mut c,
        REAL,
        &insn(op::PUSH, 4, &[imm(0xffff_fffe)]),
        &mut m,
    )
    .unwrap();
    assert_eq!((c.gpr[4], m.read(ss + 0xfc, 4)), (0xfc, 0xffff_fffe));
    // A 32-bit stack: ESP whole.
    let mut f = flat();
    f.gpr[4] = 0x1_0000;
    run(&mut f, P32, &insn(op::PUSH, 4, &[imm(9)]), &mut m).unwrap();
    assert_eq!((f.gpr[4], m.read(0xfffc, 4)), (0xfffc, 9));
    // PUSHF leaves VM (17) and RF (16) out; POPF at CPL 0 sets every flag but VM, VIF, VIP and RF, and clears RF.
    f.rflags = 0x3_0ed7;
    run(&mut f, P32, &insn(op::PUSHF, 4, &[]), &mut m).unwrap();
    assert_eq!(m.read(0xfff8, 4), 0x0ed7);
    m.write(0xfff8, 4, 0xffff_ffff);
    f.rflags = 0x1a_0002; // VM, VIF, VIP
    run(&mut f, P32, &insn(op::POPF, 4, &[]), &mut m).unwrap();
    assert_eq!((f.rflags, f.gpr[4]), (0x3e_7fd7, 0xfffc));
    // At CPL 3 (protected) IOPL stays, and IF too while CPL > IOPL; a 16-bit POPF changes bits 15:0 only.
    f.gpr[4] = 0xfff8;
    f.rflags = 0x2;
    run(
        &mut f,
        Context { cpl: 3, ..P32 },
        &insn(op::POPF, 2, &[]),
        &mut m,
    )
    .unwrap();
    assert_eq!(f.rflags, 0x4dd7);
}

/// Near branches: the operand size wraps the target, the CS limit bounds it (#GP(0), nothing changed); CALL checks the
/// target before its push, RET its slot before the target.
#[test]
fn near_branches() {
    let mut c = real();
    let mut m = Mem::default();
    let ss = c.seg[SREG_SS].base as u32;
    c.rip = 0xfff0;
    run(&mut c, REAL, &insn(op::JMP, 2, &[imm(0x20)]), &mut m).unwrap();
    assert_eq!(c.rip, 0x12);
    // A 32-bit operand size is not wrapped at 64 KiB: past the limit.
    c.rip = 0xfff0;
    assert_eq!(faults(&c, REAL, &insn(op::JMP, 4, &[imm(0x20)]), &m), GP);
    c.seg[SREG_CS].limit = 0x1000;
    assert_eq!(faults(&c, REAL, &insn(op::JMP, 2, &[imm(0xfffe)]), &m), GP);
    // CALL past the limit: #GP(0) before any stack check (SP 1 would be #SS).
    c.gpr[4] = 1;
    c.rip = 0x100;
    assert_eq!(faults(&c, REAL, &insn(op::CALL, 2, &[imm(0x2000)]), &m), GP);
    assert_eq!(faults(&c, REAL, &insn(op::CALL, 2, &[imm(0x20)]), &m), SS);
    c.gpr[4] = 0x200;
    run(&mut c, REAL, &insn(op::CALL, 2, &[imm(0x20)]), &mut m).unwrap();
    assert_eq!(
        (c.rip, c.gpr[4], m.read(ss + 0x1fe, 2)),
        (0x122, 0x1fe, 0x102)
    );
    // RET imm16: pops, then releases the immediate.
    run(&mut c, REAL, &insn(op::RET, 2, &[imm(6)]), &mut m).unwrap();
    assert_eq!((c.rip, c.gpr[4]), (0x102, 0x206));
    // RET to a target past the limit: #GP(0), SP unchanged.
    m.write(ss + 0x206, 2, 0x2000);
    assert_eq!(faults(&c, REAL, &insn(op::RET, 2, &[]), &m), GP);
}

/// LOOP, LOOPE, LOOPNE and JCXZ: CX or ECX by the address size; a taken branch past the limit changes nothing.
#[test]
fn loops() {
    let mut c = real();
    let m = &mut Mem::default();
    let lp = |cc: u8, address: u8| {
        let mut i = insn(op::LOOP, 2, &[imm(0x10)]);
        (i.cc, i.address) = (cc, address);
        i
    };
    // CX 1: decremented to 0, not taken; ECX's upper half stays.
    c.gpr[1] = 0x7_0001;
    run(&mut c, REAL, &lp(2, 2), m).unwrap();
    assert_eq!((c.gpr[1], c.rip), (0x7_0000, 0x102));
    // ECX (address size 4): 0x7_0000 - 1, taken.
    run(&mut c, REAL, &lp(2, 4), m).unwrap();
    assert_eq!((c.gpr[1], c.rip), (0x6_ffff, 0x114));
    // LOOPE needs ZF, LOOPNE its absence.
    c.rflags = 2;
    run(&mut c, REAL, &lp(1, 4), m).unwrap();
    assert_eq!((c.gpr[1], c.rip), (0x6_fffe, 0x116));
    run(&mut c, REAL, &lp(0, 4), m).unwrap();
    assert_eq!((c.gpr[1], c.rip), (0x6_fffd, 0x128));
    // JCXZ: taken at CX 0, no decrement.
    c.gpr[1] = 0x1_0000;
    run(&mut c, REAL, &lp(3, 2), m).unwrap();
    assert_eq!((c.gpr[1], c.rip), (0x1_0000, 0x13a));
    c.seg[SREG_CS].limit = 0x140;
    c.gpr[1] = 5;
    assert_eq!(faults(&c, REAL, &lp(2, 2), m), GP);
}

/// The memory bit string (BT/BTS/BTR/BTC m, r): against the SDM's definition, bit `offset` (signed) counted from bit 0 of the
/// byte at the operand's address, so byte `floor(offset / 8)` bit `offset mod 8`.
#[test]
fn bit_string() {
    let base = 0x8000u32;
    for size in [2u8, 4] {
        for o in [op::BT, op::BTS, op::BTR, op::BTC] {
            for k in 0..120i32 {
                let offset = k * 37 % 701 - 350 - i32::from(size == 4) * 70_000 * (k % 3 - 1);
                let mut c = real();
                c.seg[SREG_DS].base = 0x10_0000;
                let mut m = Mem::default();
                let ds = c.seg[SREG_DS].base as u32;
                (c.gpr[3], c.gpr[0]) = (u64::from(base), offset as u32 as u64);
                if size == 2 {
                    c.gpr[0] = u64::from(offset as i16 as u16) | 0xabcd_0000;
                }
                let effective = if size == 2 {
                    i32::from(offset as i16)
                } else {
                    offset
                };
                let byte = ds
                    .wrapping_add(base)
                    .wrapping_add(effective.div_euclid(8) as u32);
                let bit = effective.rem_euclid(8);
                // The bytes around it hold a pattern; a store anywhere else adds a byte the comparison sees.
                for b in -8i32..8 {
                    m.bytes
                        .insert(byte.wrapping_add(b as u32), (b * 7 + k) as u8);
                }
                let mut i = at(insn(o, size, &[mem(size), reg(0, size)]), 3, NO_REG, 0, 0);
                i.address = if size == 4 { 4 } else { 2 };
                let before = m.clone();
                let old = m.byte(byte) >> bit & 1;
                run(&mut c, REAL, &i, &mut m).unwrap();
                assert_eq!(
                    c.rflags as u32 & CF,
                    u32::from(old),
                    "size {size} op {o} offset {offset}"
                );
                let new = match o {
                    op::BTS => 1,
                    op::BTR => 0,
                    op::BTC => old ^ 1,
                    _ => old,
                };
                let mut expected = before.clone();
                expected
                    .bytes
                    .insert(byte, before.byte(byte) & !(1 << bit) | new << bit);
                assert_eq!(m, expected, "size {size} op {o} offset {offset}");
            }
        }
    }
}

/// LOCK: a compare-and-swap loop over the memory, which retries when another CPU stored first; LOCK on any other form is
/// #UD.
#[test]
fn locked_read_modify_writes() {
    let mut c = flat();
    let mut m = Mem::default();
    m.write(0x1000, 4, 10);
    c.gpr[1] = 5;
    let mut xadd = at(
        insn(op::XADD, 4, &[mem(4), reg(1, 4)]),
        NO_REG,
        NO_REG,
        0,
        0x1000,
    );
    (xadd.lock, xadd.address) = (1, 4);
    m.contended = 2;
    run(&mut c, P32, &xadd, &mut m).unwrap();
    // Two stores came first (10 -> 11 -> 12): the sum is 12 + 5, and ECX the value it replaced.
    assert_eq!((m.read(0x1000, 4), c.gpr[1]), (17, 12));
    let mut bts = at(
        insn(op::BTS, 4, &[mem(4), imm(3)]),
        NO_REG,
        NO_REG,
        0,
        0x1000,
    );
    (bts.lock, bts.address) = (1, 4);
    m.write(0x1000, 4, 0);
    run(&mut c, P32, &bts, &mut m).unwrap();
    assert_eq!((m.read(0x1000, 4), c.rflags as u32 & CF), (8, 0));
    run(&mut c, P32, &bts, &mut m).unwrap();
    assert_eq!((m.read(0x1000, 4), c.rflags as u32 & CF), (8, CF));
    // A register destination, CMP, TEST, BT, a shift, MOV, PUSH: #UD. Every other lockable form commits.
    let lock = |mut i: Insn| {
        i.lock = 1;
        i
    };
    for i in [
        insn(op::ADD, 4, &[reg(0, 4), reg(1, 4)]),
        insn(op::CMP, 4, &[mem(4), reg(1, 4)]),
        insn(op::TEST, 4, &[mem(4), reg(1, 4)]),
        insn(op::BT, 4, &[mem(4), imm(1)]),
        insn(op::SHL, 4, &[mem(4), imm(1)]),
        insn(op::SAR, 4, &[mem(4), reg(1, 1)]),
        insn(op::MOV, 4, &[mem(4), reg(1, 4)]),
        insn(op::PUSH, 4, &[reg(1, 4)]),
    ] {
        assert_eq!(faults(&c, P32, &lock(i), &m), UD);
    }
    for o in [
        op::ADD,
        op::OR,
        op::ADC,
        op::SBB,
        op::AND,
        op::SUB,
        op::XOR,
        op::INC,
        op::DEC,
        op::BTR,
        op::BTC,
    ] {
        let source = if matches!(o, op::INC | op::DEC) {
            Operand::NONE
        } else {
            imm(1)
        };
        run(&mut c, P32, &lock(insn(o, 4, &[mem(4), source])), &mut m).unwrap();
    }
}

/// Real-mode far JMP and far RET (CS := selector, base selector × 16, the limit and attributes kept), MOV Sreg in real mode,
/// and the protected-mode forms handed to the resident with their operands read.
#[test]
fn far_transfers_and_segment_loads() {
    let mut c = real();
    let mut m = Mem::default();
    let cs = c.seg[SREG_CS];
    run(
        &mut c,
        REAL,
        &insn(op::JMP_FAR, 2, &[imm(0x1234), imm(0xf000)]),
        &mut m,
    )
    .unwrap();
    assert_eq!(
        (c.seg[SREG_CS].selector, c.seg[SREG_CS].base, c.rip),
        (0xf000, 0xf_0000, 0x1234)
    );
    assert_eq!(
        (c.seg[SREG_CS].limit, c.seg[SREG_CS].attributes),
        (cs.limit, cs.attributes)
    );
    // Through memory (m16:16 at DS:0x10); a 32-bit offset past the limit is #GP(0).
    let ds = c.seg[SREG_DS].base as u32;
    m.write(ds + 0x10, 2, 0x5678);
    m.write(ds + 0x12, 2, 0x0800);
    let jm = at(insn(op::JMP_FAR, 2, &[mem(4)]), NO_REG, NO_REG, 0, 0x10);
    run(&mut c, REAL, &jm, &mut m).unwrap();
    assert_eq!((c.seg[SREG_CS].selector, c.rip), (0x800, 0x5678));
    assert_eq!(
        faults(
            &c,
            REAL,
            &insn(op::JMP_FAR, 4, &[imm(0x1_0000), imm(0)]),
            &m
        ),
        GP
    );
    // Far RET imm16: offset, then selector, then the immediate.
    let ss = c.seg[SREG_SS].base as u32;
    c.gpr[4] = 0x100;
    m.write(ss + 0x100, 2, 0x42);
    m.write(ss + 0x102, 2, 0x900);
    run(&mut c, REAL, &insn(op::RET_FAR, 2, &[imm(4)]), &mut m).unwrap();
    assert_eq!(
        (c.seg[SREG_CS].base, c.rip, c.gpr[4]),
        (0x9000, 0x42, 0x108)
    );
    // The selector's slot in an absent page (the offset's present): nothing changes.
    m.absent.push((ss + 0x1000) >> 12);
    c.gpr[4] = 0xffe;
    assert_eq!(
        faults(&c, REAL, &insn(op::RET_FAR, 2, &[]), &m),
        Exit::Fault {
            vector: 14,
            error: 0,
            address: ss + 0x1000
        }
    );
    // MOV DS, AX; MOV ESI, CS (zero-extended); MOV [0], SS; MOV CS, AX is #UD.
    c.gpr[0] = 0x1_2345;
    run(
        &mut c,
        REAL,
        &insn(op::MOV_SREG, 2, &[sreg(SREG_DS), reg(0, 2)]),
        &mut m,
    )
    .unwrap();
    assert_eq!(
        (
            c.seg[SREG_DS].selector,
            c.seg[SREG_DS].base,
            c.seg[SREG_DS].limit
        ),
        (0x2345, 0x2_3450, 0xffff)
    );
    c.gpr[6] = u64::MAX;
    run(
        &mut c,
        REAL,
        &insn(op::MOV, 4, &[reg(6, 4), sreg(SREG_CS)]),
        &mut m,
    )
    .unwrap();
    assert_eq!(c.gpr[6], 0x900);
    run(
        &mut c,
        REAL,
        &insn(op::MOV, 2, &[mem(2), sreg(SREG_SS)]),
        &mut m,
    )
    .unwrap();
    assert_eq!(m.read(0x2_3450, 2), 0x300);
    assert_eq!(
        faults(
            &c,
            REAL,
            &insn(op::MOV_SREG, 2, &[sreg(SREG_CS), reg(0, 2)]),
            &m
        ),
        UD
    );
    // Protected mode: the resident's.
    let mut f = flat();
    let r = |f: &mut Cpu, i: &Insn, m: &mut Mem| run(f, P32, i, m).unwrap();
    assert_eq!(
        r(
            &mut f,
            &insn(op::JMP_FAR, 4, &[imm(0x10_0000), imm(0x18)]),
            &mut m
        ),
        Outcome::Resident(Resident::FarJump {
            selector: 0x18,
            offset: 0x10_0000
        })
    );
    f.gpr[4] = 0x2000;
    m.write(0x2000, 4, 0x4000);
    m.write(0x2004, 4, 0xffff_0028);
    assert_eq!(
        r(&mut f, &insn(op::RET_FAR, 4, &[imm(8)]), &mut m),
        Outcome::Resident(Resident::FarReturn {
            selector: 0x28,
            offset: 0x4000,
            release: 16
        })
    );
    assert_eq!(f.gpr[4], 0x2000, "nothing committed");
    release(&mut f, P32, 16);
    assert_eq!(f.gpr[4], 0x2010);
    f.gpr[0] = 0x10;
    assert_eq!(
        r(
            &mut f,
            &insn(op::MOV_SREG, 2, &[sreg(SREG_SS), reg(0, 2)]),
            &mut m
        ),
        Outcome::Resident(Resident::LoadSegment {
            sreg: SREG_SS as u8,
            selector: 0x10
        })
    );
    // LGDT: a 16-bit operand size keeps 24 bits of the base.
    m.write(0x3000, 2, 0x27);
    m.write(0x3002, 4, 0x1234_5678);
    let mut lgdt = at(insn(op::LGDT, 2, &[mem(6)]), NO_REG, NO_REG, 0, 0x3000);
    lgdt.address = 4;
    assert_eq!(
        r(&mut f, &lgdt, &mut m),
        Outcome::Resident(Resident::LoadTable {
            idt: false,
            base: 0x34_5678,
            limit: 0x27
        })
    );
    lgdt.size = 4;
    lgdt.op = op::LIDT;
    assert_eq!(
        r(&mut f, &lgdt, &mut m),
        Outcome::Resident(Resident::LoadTable {
            idt: true,
            base: 0x1234_5678,
            limit: 0x27
        })
    );
    f.gpr[3] = 0xffff_ffff_8000_0011;
    let cr = |kind, reg| Operand {
        kind,
        reg,
        size: 4,
        ..Operand::NONE
    };
    assert_eq!(
        r(
            &mut f,
            &insn(op::MOV_TO_CR, 4, &[cr(kind::CR, 0), cr(kind::REG, 3)]),
            &mut m
        ),
        Outcome::Resident(Resident::MovToCr {
            cr: 0,
            value: 0x8000_0011
        })
    );
    assert_eq!(f.rip, 0x100, "a resident operation leaves EIP");
    complete(&mut f, &lgdt);
    assert_eq!(f.rip, 0x102);
}

/// The other forms: CLI and CLD, the stops, and the records the resident checks.
#[test]
fn flags_stops_and_records() {
    let mut c = real();
    let mut m = Mem::default();
    c.rflags = 0x602;
    run(&mut c, REAL, &insn(op::CLI, 2, &[]), &mut m).unwrap();
    run(&mut c, REAL, &insn(op::CLD, 2, &[]), &mut m).unwrap();
    assert_eq!((c.rflags, c.rip), (2, 0x104));
    // CLI above IOPL in protected mode: #GP(0).
    let mut f = flat();
    f.rflags = 0x202;
    assert_eq!(
        faults(&f, Context { cpl: 3, ..P32 }, &insn(op::CLI, 4, &[]), &m),
        GP
    );
    assert_eq!(faults(&c, REAL, &insn(op::UD, 2, &[]), &m), UD);
    assert_eq!(faults(&c, REAL, &insn(op::TOO_LONG, 2, &[]), &m), GP);
    assert_eq!(
        faults(&c, REAL, &insn(op::OTHER, 2, &[]), &m),
        Exit::Unsupported
    );
    // LEA: the offset at the address size, truncated to the destination.
    c.gpr[3] = 0xfff0;
    let lea = at(insn(op::LEA, 2, &[reg(0, 2), mem(2)]), 3, NO_REG, 0, 0x20);
    run(&mut c, REAL, &lea, &mut m).unwrap();
    assert_eq!(c.gpr[0] & 0xffff, 0x10);
    // Records out of range.
    let good = insn(op::ADD, 4, &[reg(0, 4), reg(1, 4)]);
    let bad = [
        Insn { length: 0, ..good },
        Insn { length: 16, ..good },
        Insn { size: 3, ..good },
        Insn { address: 8, ..good },
        Insn { segment: 6, ..good },
        Insn { op: 30, ..good },
        Insn { base: 8, ..good },
        Insn { scale: 4, ..good },
        Insn {
            operands: [reg(8, 4), reg(1, 4), Operand::NONE],
            ..good
        },
        Insn {
            operands: [mem(3), reg(1, 4), Operand::NONE],
            ..good
        },
        Insn {
            operands: [sreg(6), reg(1, 4), Operand::NONE],
            ..good
        },
        // Shapes the operation does not have: the executor would read an operand it never checked.
        Insn {
            operands: [reg(0, 2), reg(1, 4), Operand::NONE],
            ..good
        },
        Insn {
            operands: [mem(4), mem(4), Operand::NONE],
            ..good
        },
        Insn {
            operands: [reg(0, 4), reg(1, 4), imm(1)],
            ..good
        },
        Insn {
            op: op::PUSH,
            operands: [mem(4), Operand::NONE, Operand::NONE],
            ..good
        },
        Insn {
            op: op::POP,
            operands: [imm(1), Operand::NONE, Operand::NONE],
            ..good
        },
        Insn {
            op: op::SHL,
            operands: [reg(0, 4), reg(2, 1), Operand::NONE],
            ..good
        },
        Insn {
            op: op::JMP_FAR,
            operands: [mem(4), Operand::NONE, Operand::NONE],
            ..good
        },
        Insn {
            op: op::JMP_FAR,
            operands: [imm(1), Operand::NONE, Operand::NONE],
            ..good
        },
        Insn {
            op: op::LGDT,
            operands: [mem(4), Operand::NONE, Operand::NONE],
            ..good
        },
        Insn {
            op: op::JCC,
            operands: [reg(0, 4), Operand::NONE, Operand::NONE],
            ..good
        },
        Insn {
            op: op::CPUID,
            operands: [reg(0, 4), Operand::NONE, Operand::NONE],
            ..good
        },
    ];
    for (k, i) in bad.iter().enumerate() {
        assert!(!i.well_formed(), "{k}: {i:?}");
    }
    assert!(good.well_formed());
}

/// Sequential EIP wraps at 32 bits (a branch's target at the operand size, `near_branches`).
#[test]
fn eip_wraps_at_32_bits() {
    let mut c = flat();
    c.rip = 0xffff_ffff;
    run(&mut c, P32, &insn(op::NOP, 4, &[]), &mut Mem::default()).unwrap();
    assert_eq!(c.rip, 1);
    c.rip = 0xffff_fffe;
    run(
        &mut c,
        P32,
        &insn(op::INC, 4, &[reg(0, 4)]),
        &mut Mem::default(),
    )
    .unwrap();
    assert_eq!(c.rip, 0);
}

/// A real-mode segment load or far JMP releases no stack and leaves ESP alone, whatever SS.B (it would otherwise rewrite
/// ESP, clearing RSP's upper half on a 32-bit stack).
#[test]
fn real_mode_loads_leave_the_stack() {
    let ctx = Context {
        stack32: true,
        ..REAL
    };
    let mut c = real();
    c.gpr[4] = 0xdead_beef_0000_1234;
    c.gpr[0] = 0x2000;
    let m = &mut Mem::default();
    run(
        &mut c,
        ctx,
        &insn(op::MOV_SREG, 2, &[sreg(SREG_ES), reg(0, 2)]),
        m,
    )
    .unwrap();
    run(
        &mut c,
        ctx,
        &insn(op::JMP_FAR, 2, &[imm(0x10), imm(0x3000)]),
        m,
    )
    .unwrap();
    assert_eq!(
        (c.seg[SREG_ES].base, c.seg[SREG_CS].base, c.gpr[4]),
        (0x2_0000, 0x3_0000, 0xdead_beef_0000_1234)
    );
}

/// Far RET checks both slots against the stack limit before the memory of either (SDM Vol.2 RET: #SS(0) before #PF).
#[test]
fn far_return_checks_both_limits_first() {
    let mut f = flat();
    f.seg[SREG_SS].limit = 0x1003;
    f.gpr[4] = 0x1000;
    let mut m = Mem::default();
    m.absent.push(1);
    // Slot 0 (0x1000..0x1003) is within the limit but its page is absent; slot 1 (0x1004..) is past the limit.
    assert_eq!(faults(&f, P32, &insn(op::RET_FAR, 4, &[]), &m), SS);
    f.seg[SREG_SS].limit = 0x1007;
    assert_eq!(
        faults(&f, P32, &insn(op::RET_FAR, 4, &[]), &m),
        Exit::Fault {
            vector: 14,
            error: 0,
            address: 0x1000
        }
    );
}
