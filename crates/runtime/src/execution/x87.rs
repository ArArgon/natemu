//! x87 transactions (I6), on FEX's reduced-precision state in the guest-state page (`frame::X87_*`): ST(i) as doubles,
//! TOP, C0..C3, the abridged tag word, FEX's IE byte and the x87 exception word (FPSR order, SF in bit 6); FCW is the
//! bank's ([`Cpu::fcw`], written into the page at every block entry). FNINIT, FNCLEX, FLDCW, FLDENV/FNSTENV, FRSTOR/FNSAVE
//! and FLD/FSTP m80 run here; every other admitted x87 form is generated code. Gating (SDM Vol.3 Table 2-2): #NM under
//! CR0.EM or CR0.TS before anything else (translation cuts gated forms as [`system::X87_GATED`]). Unmasked exceptions (I5c):
//! FCW's masks key the code (`Cpu::code_context`); an instruction's unmasked exception sets its flags (ES and B follow,
//! [`fsw_summary`]) and #MF is due at the next waiting instruction (SDM Vol.1 8.6, 8.7.1): generated code checks at each
//! one, the waiting transactions here ([`pending`]). Not modelled: the last instruction and data pointers and opcode
//! (stored as 0). 80-bit values are rounded to doubles on the way in (by FCW.RC) and widened exactly on the way out: the
//! reduced-precision deviation (plan I6).
use super::segments::{Exception, address, linear, noncanonical};
use super::*;

use natemu_abi::x87::{INDEFINITE, from_f80, to_f80};

const NM: Exception = [7, 0, 0];
const FSW_SF: u64 = 1 << 6;
const FSW_IE: u64 = 1;

/// The guest-state page through the resident's EL1 alias.
fn page(at: u32) -> *mut u8 {
    // SAFETY (callers): FRAME_VA maps the vCPU's frame page; every x87 field lies inside it (abi layout test).
    (FRAME_VA as *mut u8).wrapping_add(at as usize)
}
fn get8(at: u32) -> u64 {
    unsafe { u64::from(page(at).read_volatile()) }
}
fn set8(at: u32, v: u64) {
    unsafe { page(at).write_volatile(v as u8) }
}
fn get32(at: u32) -> u64 {
    unsafe { u64::from((page(at) as *mut u32).read_volatile()) }
}
fn set32(at: u32, v: u64) {
    unsafe { (page(at) as *mut u32).write_volatile(v as u32) }
}
pub(super) fn top() -> u64 {
    get8(frame::X87_TOP) & 7
}
/// Physical register `r`'s double.
pub(super) fn mm(r: u64) -> u64 {
    unsafe { (page(frame::X87_MM + 16 * (r as u32 & 7)) as *mut u64).read_volatile() }
}
pub(super) fn set_mm(r: u64, v: u64) {
    unsafe { (page(frame::X87_MM + 16 * (r as u32 & 7)) as *mut u64).write_volatile(v) }
}
/// The abridged tag byte (bit i: physical register i is not empty; FXSAVE's FTW field) and its write (I5b).
pub(super) fn abridged() -> u64 {
    get8(frame::X87_FTW)
}
pub(super) fn set_abridged(v: u64) {
    set8(frame::X87_FTW, v & 0xff)
}
/// Complete a successful MMX instruction without changing its physical registers or other x87 status.
pub(super) fn enter_mmx() {
    set8(frame::X87_TOP, 0);
    set_abridged(0xff);
}
fn valid(r: u64) -> bool {
    get8(frame::X87_FTW) >> (r & 7) & 1 != 0
}
fn set_valid(r: u64, on: bool) {
    let ftw = get8(frame::X87_FTW) & !(1 << (r & 7));
    set8(frame::X87_FTW, ftw | u64::from(on) << (r & 7));
}

/// FSW from the page under `fcw`: TOP, C0..C3, the exception flags (FEX's IE byte, the exception word in FPSR order and SF)
/// and ES and B from them and FCW's masks (I5c).
pub(super) fn fsw(fcw: u64) -> u64 {
    let x = get32(frame::X87_EXCEPTIONS);
    let flags = get8(frame::X87_IE) & 1 | (get8(frame::X87_PE) & 1) << 5 | (get8(frame::X87_SF) & 1) << 6 | x & 1 | (x >> 7 & 1) << 1 | (x >> 1 & 0xf) << 2 | x & FSW_SF;
    flags | fsw_summary(flags, fcw) | top() << 11 | (get8(frame::X87_C0) & 1) << 8 | (get8(frame::X87_C1) & 1) << 9 | (get8(frame::X87_C2) & 1) << 10
        | (get8(frame::X87_C3) & 1) << 14
}
/// Writes FSW's TOP, condition codes and exception flags (bits 6:0; ES and B follow from the masks) into the page.
pub(super) fn set_fsw(v: u64) {
    set8(frame::X87_TOP, v >> 11 & 7);
    for (at, bit) in [(frame::X87_C0, 8), (frame::X87_C1, 9), (frame::X87_C2, 10), (frame::X87_C3, 14)] {
        set8(at, v >> bit & 1);
    }
    set8(frame::X87_IE, 0);
    set8(frame::X87_PE, 0);
    set8(frame::X87_SF, 0);
    set32(frame::X87_EXCEPTIONS, v & 1 | (v >> 1 & 1) << 7 | (v >> 2 & 0xf) << 1 | v & FSW_SF);
}
/// The full tag word (SDM Vol.1 8.1.7): 11 empty, else from the double: 01 zero, 10 NaN or infinity, 00 valid (a denormal
/// double is a normal 80-bit value).
pub(super) fn ftw() -> u64 {
    (0..8).fold(0, |t, r| {
        let d = mm(r);
        let tag = if !valid(r) {
            3
        } else if d << 1 == 0 {
            1
        } else if d >> 52 & 0x7ff == 0x7ff {
            2
        } else {
            0
        };
        t | tag << (2 * r)
    })
}

/// I6b: the helper instruction D9 `function` (`svc #X87`, [`natemu_abi::x87::helper`]) on the page's stack under FCW `fcw`:
/// reads ST(0), ST(1) and whether TOP - 1 is empty, writes the values, tags, TOP, the condition codes the instruction defines
/// and ORs its flags into the exception word. False (nothing written) for a function that is no helper.
pub(super) fn helper(function: u64, fcw: u64) -> bool {
    let t = top();
    let reg = |i: u64| valid(t + i).then(|| mm(t + i));
    let o = natemu_abi::x87::Operands { st0: reg(0), st1: reg(1), free: !valid(t + 7), rc: fcw >> 10 & 3 };
    let Some(mut e) = u8::try_from(function).ok().and_then(|f| natemu_abi::x87::helper(f, o)) else { return false };
    // I5c: an unmasked stack fault, IE or ZE leaves TOP and the operands unchanged, setting only its flags (and C1 for the
    // stack fault: SDM Vol.1 8.5.1-8.5.3); OE, UE and PE store the result (a register destination; its exponent is the
    // double's, not biased: the reduced-precision deviation) and #MF follows.
    use natemu_abi::x87::flag::{IE, SF, ZE};
    let unmasked = fpsr_bits(x87_unmasked(fcw)) as u32;
    let pre = if e.flags & SF != 0 { e.flags & (IE | SF) } else { e.flags & (IE | ZE) };
    if pre & unmasked & (IE | ZE) != 0 {
        e = natemu_abi::x87::Effect { c: [None, if e.flags & SF != 0 { e.c[1] } else { None }, None, None], flags: pre, ..Default::default() };
    }
    for (i, v) in [(0, e.st0), (1, e.st1)] {
        if let Some(v) = v {
            set_mm(t + i, v);
            set_valid(t + i, true);
        }
    }
    let mut t = t;
    if e.pop {
        set_valid(t, false);
        t = (t + 1) & 7;
    }
    if let Some(v) = e.push {
        t = (t + 7) & 7;
        set_mm(t, v);
        set_valid(t, true);
    }
    set8(frame::X87_TOP, t);
    for (at, c) in [(frame::X87_C0, e.c[0]), (frame::X87_C1, e.c[1]), (frame::X87_C2, e.c[2]), (frame::X87_C3, e.c[3])] {
        if let Some(c) = c {
            set8(at, u64::from(c));
        }
    }
    set32(frame::X87_EXCEPTIONS, get32(frame::X87_EXCEPTIONS) | u64::from(e.flags));
    true
}

/// #NM under CR0.EM or CR0.TS (any x87 form; WAIT, which the host gates only under TS with MP, as well).
fn gate(c: &Cpu) -> Result<(), Exception> {
    if c.cr0 & (CR0_EM | CR0_TS) != 0 { Err(NM) } else { Ok(()) }
}
/// #AC(0) under alignment checking for an x87 memory operand that is not `align`-aligned (SDM Vol.3 Table 7-7), after the
/// canonical check (which [`linear`] makes first otherwise).
fn aligned(s: &State, va: u64, n: u64, align: u64, i: &System) -> Result<(), Exception> {
    let last = va.wrapping_add(n - 1);
    if !paging::canonical(va) || !paging::canonical(last) || last < va {
        return Err(noncanonical(i));
    }
    if alignment_check(&s.cpu) != 0 && va & (align - 1) != 0 { Err([17, 0, 0]) } else { Ok(()) }
}
/// I5c: a waiting x87 instruction with an exception pending (a flag whose FCW mask is clear: FSW.ES; SDM Vol.1 8.7.1): #MF
/// before it runs with CR0.NE, else the stop `unsupported_ferr` (MS-DOS compatibility mode reports through the FERR# pin,
/// IRQ13 and IGNNE#, which this board does not wire; plan I5c).
pub(super) fn pending(s: &State) -> Result<(), Exception> {
    if fsw_summary(fsw(s.cpu.fcw), s.cpu.fcw) == 0 {
        return Ok(());
    }
    if s.cpu.cr0 & CR0_NE == 0 {
        halt(s, stop::UNSUPPORTED_FERR)
    }
    Err([16, 0, 0])
}
/// I5c: an unmasked stack fault of a resident form: IE, SF and C1 (1 for an overflow), nothing else.
fn stack_fault(overflow: bool) {
    set32(frame::X87_EXCEPTIONS, get32(frame::X87_EXCEPTIONS) | FSW_IE | FSW_SF);
    set8(frame::X87_C1, u64::from(overflow));
}
/// I5c: an x87 instruction's check found an unmasked exception that leaves its operands unaltered (its stub; check word `w`,
/// [`x87_fault_flags`]): only its flags (and C1) are set; #MF follows at the next waiting instruction.
pub(super) fn exception(fcw: u64, w: u64) {
    let (flags, c1) = x87_fault_flags(w, fcw);
    set32(frame::X87_EXCEPTIONS, get32(frame::X87_EXCEPTIONS) | flags);
    if let Some(c1) = c1 {
        set8(frame::X87_C1, u64::from(c1));
    }
}

/// Executes the x87 transaction `i` (any CPL; LOCK is #UD before this). `Ok`: committed, RIP moves past it.
pub(super) fn transaction(s: &mut State, i: System) -> Result<(), Exception> {
    gate(&s.cpu)?;
    // I5c: the waiting forms take a pending exception first (the non-waiting FNINIT, FNCLEX, FNSTENV, FNSAVE do not).
    if matches!(i.kind, system::FLDCW | system::FLDENV | system::FRSTOR | system::FLD80 | system::FSTP80) {
        pending(s)?;
    }
    let va = address(&s.cpu, &i);
    let fault = noncanonical(&i);
    // The environment image: FCW, FSW, FTW at 2- or 4-byte strides (the rest: instruction and data pointers, 0 here).
    let (env, stride) = if i.size == 2 { (14, 2) } else { (28, 4) };
    match i.kind {
        system::X87_GATED => halt(s, stop::BAD_REPLY), // cut as gated under these very controls
        system::FNINIT => {
            s.cpu.fcw = FCW_INIT;
            set_fsw(0);
            set8(frame::X87_FTW, 0);
        }
        system::FNCLEX => {
            set8(frame::X87_IE, 0);
            set8(frame::X87_PE, 0);
            set8(frame::X87_SF, 0);
            set32(frame::X87_EXCEPTIONS, 0);
        }
        system::FLDCW => {
            aligned(s, va, 2, 2, &i)?;
            let mut buf = [0; 2];
            linear(s, va, &mut buf, false, false, fault)?;
            // I5c: new masks re-key the code; a flag already set that the new FCW unmasks is pending (SDM FLDCW).
            s.cpu.fcw = u64::from(u16::from_le_bytes(buf));
        }
        system::FNSTENV | system::FNSAVE => {
            let n = if i.kind == system::FNSAVE { env + 80 } else { env };
            aligned(s, va, n, stride as u64, &i)?;
            let mut buf = [0u8; 108];
            for (k, v) in [s.cpu.fcw, fsw(s.cpu.fcw), ftw()].into_iter().enumerate() {
                buf[k * stride..k * stride + 2].copy_from_slice(&(v as u16).to_le_bytes());
            }
            for k in 0..8 * u64::from(i.kind == system::FNSAVE) {
                let (mant, se) = to_f80(mm(top() + k));
                let at = env as usize + 10 * k as usize;
                buf[at..at + 8].copy_from_slice(&mant.to_le_bytes());
                buf[at + 8..at + 10].copy_from_slice(&se.to_le_bytes());
            }
            linear(s, va, &mut buf[..n as usize], true, false, fault)?;
            if i.kind == system::FNSAVE {
                s.cpu.fcw = FCW_INIT;
                set_fsw(0);
                set8(frame::X87_FTW, 0);
            } else {
                s.cpu.fcw |= FCW_MASKS; // FNSTENV then masks every exception (SDM FSTENV/FNSTENV)
            }
        }
        system::FLDENV | system::FRSTOR => {
            let n = if i.kind == system::FRSTOR { env + 80 } else { env };
            aligned(s, va, n, stride as u64, &i)?;
            let mut buf = [0u8; 108];
            linear(s, va, &mut buf[..n as usize], false, false, fault)?;
            let word = |k: usize| u64::from(u16::from_le_bytes([buf[k * stride], buf[k * stride + 1]]));
            let (fcw, sw, tw) = (word(0), word(1), word(2));
            s.cpu.fcw = fcw; // (an unmasked flag in the new FSW is pending: ES follows from it, SDM FLDENV)
            set_fsw(sw);
            // The tag word's empty/non-empty per physical register (the class is the value's).
            set8(frame::X87_FTW, (0..8).fold(0, |a, r| a | u64::from(tw >> (2 * r) & 3 != 3) << r));
            if i.kind == system::FRSTOR {
                let top = sw >> 11 & 7;
                for k in 0..8 {
                    let at = env as usize + 10 * k as usize;
                    let mant = u64::from_le_bytes(buf[at..at + 8].try_into().unwrap());
                    let se = u16::from_le_bytes([buf[at + 8], buf[at + 9]]);
                    set_mm(top + k, from_f80(mant, se, fcw >> 10 & 3));
                }
            }
        }
        system::FLD80 => {
            aligned(s, va, 10, 8, &i)?;
            let mut buf = [0u8; 10];
            linear(s, va, &mut buf, false, false, fault)?;
            let new = top().wrapping_sub(1) & 7;
            // FLD m80 raises nothing but a stack overflow (SDM Vol.2 FLD: no #IA, #D for an 80-bit source): IE and SF with
            // C1 = 1, the indefinite loaded (masked response).
            let overflow = valid(new);
            if overflow && x87_unmasked(s.cpu.fcw) & 1 != 0 {
                // I5c: IE unmasked: TOP and the registers unchanged, only IE, SF and C1 = 1 (SDM Vol.1 8.5.1.1).
                return Ok(stack_fault(true));
            }
            let value = if overflow {
                INDEFINITE
            } else {
                from_f80(u64::from_le_bytes(buf[..8].try_into().unwrap()), u16::from_le_bytes([buf[8], buf[9]]), s.cpu.fcw >> 10 & 3)
            };
            if overflow {
                set_fsw(fsw(s.cpu.fcw) | FSW_IE | FSW_SF);
            }
            set8(frame::X87_C1, u64::from(overflow));
            set_mm(new, value);
            set_valid(new, true);
            set8(frame::X87_TOP, new);
        }
        system::FSTP80 => {
            aligned(s, va, 10, 8, &i)?;
            // An empty ST(0) is a stack underflow: IE and SF with C1 = 0, the indefinite stored (masked response).
            let underflow = !valid(top());
            if underflow && x87_unmasked(s.cpu.fcw) & 1 != 0 {
                // I5c: IE unmasked: nothing stored, no pop, only IE, SF and C1 = 0 (SDM Vol.1 8.5.1.1; the address checks
                // above came first, the store's #PF cannot: no store is made).
                return Ok(stack_fault(false));
            }
            let (mant, se) = if underflow { (0xc000_0000_0000_0000, 0xffff) } else { to_f80(mm(top())) };
            let mut buf = [0u8; 10];
            buf[..8].copy_from_slice(&mant.to_le_bytes());
            buf[8..].copy_from_slice(&se.to_le_bytes());
            linear(s, va, &mut buf, true, false, fault)?;
            if underflow {
                set_fsw(fsw(s.cpu.fcw) | FSW_IE | FSW_SF);
            }
            set8(frame::X87_C1, 0);
            set_valid(top(), false);
            set8(frame::X87_TOP, top() + 1 & 7);
        }
        _ => halt(s, stop::BAD_REPLY),
    }
    Ok(())
}
