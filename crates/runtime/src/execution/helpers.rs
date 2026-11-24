//! I6b: the helper-based instructions FEX calls host helpers for, run by the resident for an `svc` from generated code (the
//! division precedent, [`super::division`]): x87 transcendental, remainder, scale and extract instructions (`svc #X87`) and the
//! PCMPxSTRx intermediate result (`svc #STRING`). Admission accepts each `svc` only inside its x86 instruction, right after
//! the `movz w0` of that instruction's function code or control. The gates saved every register (Q registers, FPCR and FPSR
//! included) and `resume` restores them, so the resident's own floating-point and vector use touches no guest state; the
//! only results are w0 (`STRING`) and the guest-state page's x87 fields (`X87`). NZCV is the captured one.
use super::*;

/// `svc #X87`: w0 = the D9 opcode's second byte. Trip-cost plan step 2 (both helpers): `f` is the gate's frame (no copy into
/// `Shared::captured`), read before anything is written into it, and the block resumes from it; the record is read in place
/// (nothing here copies it out and back; a halt copies).
#[inline(never)] // its frame (and libm's) stays out of `returned` (the nested-entry stack bound)
pub(super) unsafe fn x87(f: *mut NativeFrame, elr: u64, spsr: u64) -> ! {
    unsafe {
        let s = &*record();
        let function = (*f).x[0];
        // (FCW from the page: a translated FLDCW may have changed the rounding control since the last bank.)
        let fcw = u64::from((FRAME_VA as *const u16).byte_add(frame::X87_FCW as usize).read_volatile());
        if cache::block_at(elr.wrapping_sub(4)).is_none() || !x87::helper(function, s.cpu.fcw & FCW_MASKS | fcw & !FCW_MASKS) {
            halt_native([0, function, elr], stop::UNKNOWN_ABORT)
        }
        resume(s, f, elr, spsr)
    }
}

/// `svc #STRING`: v0 = xmm1, v1 = xmm2/m128, w0 = the control word, x1/x2 = RAX/RDX; w0 out.
#[inline(never)]
pub(super) unsafe fn string(f: *mut NativeFrame, elr: u64, spsr: u64) -> ! {
    unsafe {
        let control = (*f).x[0];
        if cache::block_at(elr.wrapping_sub(4)).is_none() || control >> 10 != 0 {
            halt_native([0, control, elr], stop::UNKNOWN_ABORT)
        }
        let r = natemu_abi::strings::pcmpstr(
            u128::from_le_bytes((*f).q[0]),
            u128::from_le_bytes((*f).q[1]),
            control as u32,
            (*f).x[1],
            (*f).x[2],
        );
        (*f).x[0] = u64::from(r);
        resume(&*record(), f, elr, spsr)
    }
}
