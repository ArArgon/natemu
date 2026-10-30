//! SSE state the resident owns (I5a): XMM0..XMM15 and MXCSR in the bank ([`Cpu::xmm`], [`Cpu::mxcsr`]), moved into and out of
//! FEX's static vector registers v16..v31 and FPCR/FPSR at every block entry and resident entry; LDMXCSR/STMXCSR as
//! transactions; the gating of every SSE form (SDM Vol.2 2.5, exception classes 1-5 and 7: #UD with CR0.EM or without
//! CR4.OSFXSR, then #NM with CR0.TS). Translated SSE forms are gated at translation time (the host's `--sse-gated` cut, which
//! arrives here as [`system::SSE_GATED`]): every CR0/CR4 change drops the vCPU's code, so a block never outlives the
//! controls it was made under; the gating bits are also part of `Cpu::code_context`. Obligation on every path that changes
//! CR0.EM, CR0.TS or CR4.OSFXSR (MOV CR0/CR4 today; INIT/SIPI, CLTS, LMSW and task switches if ever added, host edits of
//! the bank; XSETBV for XCR0 once AVX is gated): flush the vCPU's code or key it, as MOV CR0/CR4 do. Every MXCSR exception
//! flag, DE included, comes from FPSR (`mxcsr_flags`: IDC is DE unless DAZ). Unmasked SIMD exceptions (#XM, or #UD without
//! OSXMMEXCPT) are not built: an LDMXCSR that would clear any exception mask stops `unsupported_system` before any effect
//! (the plan's I5c holds the design; FP trap enables stay clear: FPCR is built from MXCSR without them).
use super::segments::{Exception, address, linear};
use super::*;

const NM: Exception = [7, 0, 0];

/// XMM and MXCSR into the frame generated code is entered with: v16..v31, FPCR from MXCSR, FPSR clear.
pub(super) fn fill(f: &mut NativeFrame, c: &Cpu) {
    for (q, x) in f.q[XMM_VREG..].iter_mut().zip(&c.xmm) {
        *q = (u128::from(x[0]) | u128::from(x[1]) << 64).to_le_bytes();
    }
    f.fpcr = fpcr(c.mxcsr);
    f.fpsr = 0;
}
/// XMM from v16..v31 and the exception flags FPSR accumulated since the entry into the bank.
pub(super) fn bank(c: &mut Cpu, f: &NativeFrame) {
    for (x, q) in c.xmm.iter_mut().zip(&f.q[XMM_VREG..]) {
        let v = u128::from_le_bytes(*q);
        *x = [v as u64, (v >> 64) as u64];
    }
    c.mxcsr |= mxcsr_flags(f.fpsr, c.mxcsr);
}

/// #UD or #NM for an SSE instruction under `c`'s controls (#UD first: SDM Vol.2 2.5, the SIMD exception classes: CR0.EM,
/// CR4.OSFXSR, then CR0.TS).
fn gate(c: &Cpu) -> Result<(), Exception> {
    if c.cr0 & CR0_EM != 0 || c.cr4 & CR4_OSFXSR == 0 {
        return Err(UD);
    }
    if c.cr0 & CR0_TS != 0 {
        return Err(NM);
    }
    Ok(())
}

/// LDMXCSR, STMXCSR and a gated SSE instruction (any CPL; LOCK is #UD before this): the gating exception, then the m32
/// operand at the CPL (#SS(0) or #GP(0) for a non-canonical one by its class, `segments::noncanonical`; #PF), then for LDMXCSR #GP(0)
/// for a 1 in a reserved bit (31:16, MXCSR_MASK = FFFFH; SDM Vol.2 LDMXCSR). `Ok`: committed.
pub(super) fn transaction(s: &mut State, i: System) -> Result<(), Exception> {
    gate(&s.cpu)?;
    if i.kind == system::SSE_GATED {
        // The host cut it as gated under these very controls.
        halt(s, stop::BAD_REPLY)
    }
    let va = address(&s.cpu, &i);
    let noncanonical = super::segments::noncanonical(&i);
    if i.kind == system::LDMXCSR {
        let mut buf = [0; 4];
        linear(s, va, &mut buf, false, false, noncanonical)?;
        let value = u64::from(u32::from_le_bytes(buf));
        if value & !MXCSR_MASK != 0 {
            return Err(GP);
        }
        // ponytail: unmasked SIMD exceptions are refused, not raised; #XM needs per-instruction checks (plan I5a).
        if value & MXCSR_MASKS != MXCSR_MASKS {
            halt(s, stop::UNSUPPORTED_SYSTEM)
        }
        s.cpu.mxcsr = value;
    } else {
        let mut buf = (s.cpu.mxcsr as u32).to_le_bytes();
        linear(s, va, &mut buf, true, false, noncanonical)?;
    }
    Ok(())
}

