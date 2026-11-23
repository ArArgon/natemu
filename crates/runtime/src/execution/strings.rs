//! Compatibility MOVS/CMPS/LODS with a loaded FS or GS source. Each transaction completes at most one
//! element; the next iteration is a normal interruptible boundary, as for REP port strings.
//!
//! The FEX dispatcher’s `FexcStringOp` uses DS:ESI (overridable) for A4/A5 MOVS, A6/A7 CMPS and
//! AC/AD LODS. MOVS and CMPS also use ES:EDI. AA/AB STOS and AE/AF SCAS use only ES:EDI and
//! remain translated. INS and OUTS use the separate port transaction. The compiler handles 67h
//! at its existing compatibility address-size boundary and leaves 64-bit strings translated.
use super::*;
use natemu_abi::interp::{
    exec::{self, ARITHMETIC, ZF},
    op,
};
use segments::Exception;

fn element(
    s: &mut State,
    segment: usize,
    index: usize,
    bytes: &mut [u8],
    write: bool,
) -> Result<(), Exception> {
    let offset = u64::from(s.cpu.gpr[index] as u32);
    let width = bytes.len();
    let va = segments::operand(&s.cpu, segment as u32, offset, width as u64, write)?;
    let range = if segment == SREG_ES {
        segments::OperandRange::Flat(GP)
    } else {
        segments::OperandRange::Based
    };
    segments::string_element(s, va, bytes, write, range)
}

pub(super) fn transaction(s: &mut State, i: System) -> Result<(), Exception> {
    if s.cpu.code_mode() != Some(CodeMode::Compat32) {
        halt(s, stop::BAD_REPLY)
    }
    let previous = core::mem::take(&mut s.mid_string);
    let repeat = i.prefixes & system::REP != 0;
    let count = s.cpu.gpr[1] as u32;
    if repeat && count == 0 {
        return Ok(commit(s, i, false));
    }
    let width = i.size as usize;
    let mut source = [0; 4];
    let mut dest = [0; 4];
    let load = |s: &mut State, bytes: &mut [u8]| element(s, i.segment as usize, 6, bytes, false);
    // FexcStringOp and the reference read ES first for CMPS. MOVS reads its source before writing ES.
    // An iteration that fails changes neither registers nor flags. REP CMPS additionally restores its
    // entry arithmetic flags on a fault after completed comparisons (Intel SDM Vol. 2, REP/REPE/REPNE).
    let accesses = match i.kind {
        system::CMPS => element(s, SREG_ES, 7, &mut dest[..width], false)
            .and_then(|()| load(s, &mut source[..width])),
        system::MOVS => load(s, &mut source[..width])
            .and_then(|()| element(s, SREG_ES, 7, &mut source[..width], true)),
        _ => load(s, &mut source[..width]),
    };
    let arithmetic = u64::from(ARITHMETIC);
    let saved = if previous & 2 != 0 {
        previous & arithmetic
    } else {
        s.cpu.rflags & arithmetic
    };
    if let Err(e) = accesses {
        if repeat && i.kind == system::CMPS {
            s.cpu.rflags = (s.cpu.rflags & !arithmetic) | saved;
        }
        return Err(e);
    }
    let src = u32::from_le_bytes(source);
    let mut again = repeat && count > 1;
    if i.kind == system::CMPS {
        let (_, flags) = exec::alu(op::CMP, src, u32::from_le_bytes(dest), false, i.size as u8);
        s.cpu.rflags = (s.cpu.rflags & !arithmetic) | u64::from(flags);
        again &= (flags & ZF != 0) == (i.cr == 0);
    } else if i.kind == system::LODS {
        let keep = match width {
            1 => !0xff,
            2 => !0xffff,
            _ => 0,
        };
        s.cpu.gpr[0] = (s.cpu.gpr[0] & keep) | u64::from(src);
    }
    let step = if s.cpu.rflags & RFLAGS_DF == 0 {
        i.size
    } else {
        i.size.wrapping_neg()
    };
    s.cpu.gpr[6] = u64::from((s.cpu.gpr[6] as u32).wrapping_add(step));
    if i.kind != system::LODS {
        s.cpu.gpr[7] = u64::from((s.cpu.gpr[7] as u32).wrapping_add(step));
    }
    if repeat {
        s.cpu.gpr[1] = u64::from(count - 1);
    }
    if again {
        s.mid_string = if i.kind == system::CMPS { 2 | saved } else { 1 };
        commit(s, System { length: 0, ..i }, false);
    } else {
        commit(s, i, false);
    }
    Ok(())
}
