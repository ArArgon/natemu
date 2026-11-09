//! I6b: the helper-based instructions FEX calls host helpers for, run by the resident for an `svc` from generated code (the
//! division precedent, [`super::division`]): x87 transcendental, remainder, scale and extract instructions (`svc #X87`) and the
//! PCMPxSTRx intermediate result (`svc #STRING`). Admission accepts each `svc` only inside its x86 instruction, right after
//! the `movz w0` of that instruction's function code or control. The gates saved every register (Q registers, FPCR and FPSR
//! included) and `resume` restores them, so the resident's own floating-point and vector use touches no guest state; the
//! only results are w0 (`STRING`) and the guest-state page's x87 fields (`X87`). NZCV is the captured one.
use super::*;

/// `svc #X87`: w0 = the D9 opcode's second byte.
#[inline(never)] // its frame (and libm's) stays out of `returned` (the nested-entry stack bound)
pub(super) unsafe fn x87(f: &NativeFrame, elr: u64, spsr: u64) -> ! {
    unsafe {
        let mut s = load();
        if cache::block_at(elr.wrapping_sub(4)).is_none() || !x87::helper(f.x[0], s.cpu.fcw) {
            s.native = [0, f.x[0], elr];
            halt(&s, stop::UNKNOWN_ABORT)
        }
        resume(&s, &raw const (*shared()).captured, elr, spsr)
    }
}

/// `svc #STRING`: v0 = xmm1, v1 = xmm2/m128, w0 = the control word, x1/x2 = RAX/RDX; w0 out.
#[inline(never)]
pub(super) unsafe fn string(f: &NativeFrame, elr: u64, spsr: u64) -> ! {
    unsafe {
        let mut s = load();
        if cache::block_at(elr.wrapping_sub(4)).is_none() || f.x[0] >> 10 != 0 {
            s.native = [0, f.x[0], elr];
            halt(&s, stop::UNKNOWN_ABORT)
        }
        let r = natemu_abi::strings::pcmpstr(
            u128::from_le_bytes(f.q[0]),
            u128::from_le_bytes(f.q[1]),
            f.x[0] as u32,
            f.x[1],
            f.x[2],
        );
        let resume = &raw mut (*shared()).captured;
        (*resume).x[0] = u64::from(r);
        self::resume(&s, resume, elr, spsr)
    }
}
