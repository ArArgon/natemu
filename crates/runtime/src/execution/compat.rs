//! M2 task 3b: the instructions only 32- and 16-bit code has that the compiler leaves to the resident (fexc's 32-bit rows,
//! m2-task2-findings): ARPL, BOUND, PUSHA/POPA and AAM (SDM Vol.2 each). The host decodes them only outside 64-bit mode,
//! where their opcodes are other instructions or #UD. Each checks everything before its first change.
use super::segments::{Exception, address, aligned_operand, noncanonical, wrap};
use super::*;

const SS0: Exception = [12, 0, 0];
const CF: u64 = 1;
const PF: u64 = 1 << 2;
const AF: u64 = 1 << 4;
const ZF: u64 = 1 << 6;
const SF: u64 = 1 << 7;
const OF: u64 = 1 << 11;

/// Executes `i` (ARPL ..= AAM). `Ok`: committed by the caller, RIP moves past it.
pub(super) fn execute(s: &mut State, i: System) -> Result<(), Exception> {
    let c = s.cpu;
    let w = i.size as usize;
    match i.kind {
        // ARPL: the destination selector's RPL raised to the source's, ZF = 1; else ZF = 0 and the selector unchanged. A
        // memory destination is a read-modify-write (written whatever the outcome, as QEMU does), 16 bits.
        system::ARPL => {
            let source = c.gpr[i.cr as usize] & 3;
            let update = |dest: u64| {
                let raise = dest & 3 < source;
                let flags = c.rflags & !ZF | if raise { ZF } else { 0 };
                (if raise { dest & !3 | source } else { dest }, flags)
            };
            if i.memory == 1 {
                return super::rotate::memory(s, System { size: 2, ..i }, update);
            }
            let r = i.reg as usize;
            let (value, flags) = update(c.gpr[r] & 0xffff);
            (s.cpu.gpr[r], s.cpu.rflags) = (c.gpr[r] & !0xffff | value, flags);
        }
        // BOUND: the signed index against the two signed bounds at the operand (lower, then upper): #BR (a fault) when it
        // lies outside them.
        system::BOUND => {
            let mut b = [0u8; 8];
            aligned_operand(s, address(&c, &i, (2 * w) as u64, false)?, &mut b[..2 * w], w, w as u64, false, noncanonical(&i))?;
            let signed = |v: u64| if w == 2 { v as i16 as i64 } else { v as i32 as i64 };
            let word = |k: usize| {
                let mut v = [0u8; 8];
                v[..w].copy_from_slice(&b[k * w..(k + 1) * w]);
                signed(u64::from_le_bytes(v))
            };
            let (lower, upper) = (word(0), word(1));
            let index = signed(c.gpr[i.cr as usize]);
            if index < lower || index > upper {
                return Err([5, 0, 0]);
            }
        }
        // PUSHA: EAX, ECX, EDX, EBX, the ESP before it, EBP, ESI, EDI (EDI lowest) at ESP - 8 * size; POPA the reverse,
        // ESP's slot skipped. One access for the eight slots, so a fault leaves registers and memory as they were. The
        // stack is 32-bit (the code runs only on one): ESP wraps, whatever the operand size.
        system::PUSHA => {
            let sp = wrap(&c, c.gpr[4].wrapping_sub(8 * w as u64));
            let mut b = [0u8; 32];
            for k in 0..8 {
                b[k * w..(k + 1) * w].copy_from_slice(&c.gpr[7 - k].to_le_bytes()[..w]);
            }
            aligned_operand(s, sp, &mut b[..8 * w], w, w as u64, true, SS0)?;
            s.cpu.gpr[4] = sp;
        }
        system::POPA => {
            let mut b = [0u8; 32];
            aligned_operand(s, c.gpr[4], &mut b[..8 * w], w, w as u64, false, SS0)?;
            for k in (0..8).filter(|&k| k != 3) {
                let mut v = [0u8; 8];
                v[..w].copy_from_slice(&b[k * w..(k + 1) * w]);
                let (v, r) = (u64::from_le_bytes(v), &mut s.cpu.gpr[7 - k]);
                *r = if w == 2 { *r & !0xffff | v } else { v };
            }
            s.cpu.gpr[4] = wrap(&c, c.gpr[4].wrapping_add(8 * w as u64));
        }
        // AAM: AH = AL / imm8, AL = AL mod imm8; SF, ZF, PF from AL; CF, OF, AF (undefined) cleared, as QEMU and FEX's
        // logical flags do. #DE for a zero immediate.
        system::AAM => {
            let (al, base) = (c.gpr[0] as u8, i.cr as u8);
            if base == 0 {
                return Err([0, 0, 0]);
            }
            let (ah, al) = (al / base, al % base);
            s.cpu.gpr[0] = c.gpr[0] & !0xffff | u64::from(ah) << 8 | u64::from(al);
            s.cpu.rflags = c.rflags & !(CF | PF | AF | ZF | SF | OF)
                | if al == 0 { ZF } else { 0 }
                | if al & 0x80 != 0 { SF } else { 0 }
                | if al.count_ones() % 2 == 0 { PF } else { 0 };
        }
        _ => halt(s, stop::BAD_REPLY),
    }
    Ok(())
}
