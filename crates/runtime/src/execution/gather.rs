//! The AVX2 gathers (VPGATHERDD/DQ/QD/QQ, VGATHERDPS/DPD/QPS/QPD: VEX.66.0F38 90..93 with a VSIB operand; SDM Vol.2 each,
//! exceptions Type 12, Table 2-27), as the host decoded them ([`system::GATHER`]): fexc cuts every block before one and
//! raises the encoding #UDs itself (no SIB byte, a register operand, two of destination, mask and index the same register).
//!
//! Element by element, lowest first, as the SDM's operation: the element's address is the base, plus its index
//! (sign-extended from 32 or 64 bits) shifted by the scale, plus the displacement, modulo 2^64 (32 bits with 0x67), plus
//! the FS/GS base; an element whose mask MSB is set is read from memory as one explicit access of its width at the CPL (a
//! separate load, in order: TSO; page walks with their A bits, #PF with CR2 its first faulting byte, #GP(0) or #SS(0) when
//! not canonical, data breakpoints; never #AC), and every element processed, loaded or not, has its mask element cleared.
//! Completed, the whole mask register is 0 and the destination's bytes past the gathered elements are 0 (to bit 255).
//!
//! At a fault on element k (the first faulting element: faults are delivered right to left), elements 0..k-1 are complete:
//! their destination elements loaded where their mask was set, their mask elements 0; nothing else of either register
//! changes, neither the mask elements from k on (not normalised) nor the parts no element covers. This is the reference AMD
//! machine's state (`natemu-raw/gather-20261105-01/probe`, user mode: every form, k = 0..n-1), which the SDM allows (it
//! lets elements left of the fault complete or not, and the unused parts be zeroed or not, at an exception); the SDM's
//! pseudocode extends the mask MSBs before its loop, which no restart can tell from this (only the MSB is ever read).
//! Returning to the instruction restarts it from that state. If an element completed before the fault met a data
//! breakpoint, that trap is delivered in lieu of the fault, the instruction partly done, RIP at it and RF = 1 in the frame
//! (SDM: "If any traps or interrupts are pending from already gathered elements, they will be delivered in lieu of the
//! exception; in this case, EFLAG.RF is set to one"); the restart then meets the fault. A device page stops
//! `unsupported_mmio` as any vector form's (M3); UC RAM is read in order like any RAM.
use super::segments::{Exception, copy, noncanonical};
use super::xsave::{set_ymm_hi, ymm_hi};
use super::*;

/// YMM `r` as 32 little-endian bytes: XMM from the bank, the upper half from the guest-state page.
fn ymm(c: &Cpu, r: usize) -> [u8; 32] {
    let mut v = [0; 32];
    for (k, q) in [c.xmm[r][0], c.xmm[r][1], ymm_hi(2 * r), ymm_hi(2 * r + 1)]
        .into_iter()
        .enumerate()
    {
        v[8 * k..8 * k + 8].copy_from_slice(&q.to_le_bytes());
    }
    v
}
fn set_ymm(c: &mut Cpu, r: usize, v: &[u8; 32]) {
    let q = |k: usize| u64::from_le_bytes(v[8 * k..8 * k + 8].try_into().unwrap());
    c.xmm[r] = [q(0), q(1)];
    set_ymm_hi(2 * r, q(2));
    set_ymm_hi(2 * r + 1, q(3));
}

/// Executes the gather `i` at RIP (any CPL; LOCK was #UD before this) and commits it, or returns the fault of its first
/// faulting element with the completed elements' state in the registers.
pub(super) fn transaction(s: &mut State, i: System) -> Result<(), Exception> {
    // The VEX gating (Table 2-27: #UD without CR4.OSXSAVE or with XCR0[2:1] != 11b, then #NM with CR0.TS). The host cuts
    // a gated form before this (`--avx-gated`, part of the code context); checked again so the transaction stands alone.
    if !s.cpu.avx_enabled() {
        return Err(UD);
    }
    if s.cpu.cr0 & CR0_TS != 0 {
        return Err([7, 0, 0]);
    }
    let (dst, mask, index) = (i.reg as usize, (i.cr & 15) as usize, i.index as usize);
    let (ew, iw) = (i.size as usize, if i.cr & 32 != 0 { 8 } else { 4 });
    let n = (16 << (i.cr >> 4 & 1)) / ew.max(iw);
    let (mut d, mut m, x) = (ymm(&s.cpu, dst), ymm(&s.cpu, mask), ymm(&s.cpu, index));
    let base = if i.base == system::NO_REG {
        0
    } else {
        s.cpu.gpr[i.base as usize]
    };
    let segment = match i.segment as usize {
        SREG_FS => s.cpu.fs_base,
        SREG_GS => s.cpu.gs_base,
        _ => 0,
    };
    let nc = noncanonical(&i);
    for j in 0..n {
        if m[ew * j + ew - 1] & 0x80 != 0 {
            let at = iw * j;
            let ix = if iw == 4 {
                i64::from(i32::from_le_bytes(x[at..at + 4].try_into().unwrap())) as u64
            } else {
                u64::from_le_bytes(x[at..at + 8].try_into().unwrap())
            };
            let ea = base.wrapping_add(ix << i.scale).wrapping_add(i.disp as u64);
            let ea = if i.prefixes & system::ADDRESS32 != 0 {
                ea as u32 as u64
            } else {
                ea
            };
            let mut buf = [0; 8];
            if let Err(e) = copy(
                s,
                ea.wrapping_add(segment),
                &mut buf[..ew],
                false,
                false,
                nc,
            ) {
                (set_ymm(&mut s.cpu, dst, &d), set_ymm(&mut s.cpu, mask, &m));
                let hits = s.debug >> debug::HITS & 0xf;
                if hits == 0 {
                    return Err(e);
                }
                // In lieu of the fault: the completed elements' data breakpoints, RF = 1 (a boundary inside the
                // instruction, as between two REP iterations).
                (s.debug, s.mid_string) = (s.debug & debug::TRAP | hits, 1);
                commit(s, System { length: 0, ..i }, false);
                return Ok(());
            }
            d[ew * j..ew * j + ew].copy_from_slice(&buf[..ew]);
        }
        m[ew * j..ew * j + ew].fill(0);
    }
    d[ew * n..].fill(0);
    (
        set_ymm(&mut s.cpu, dst, &d),
        set_ymm(&mut s.cpu, mask, &[0; 32]),
    );
    commit(s, i, false);
    Ok(())
}
