//! The masked moves (no-stop change, November 6, 2026), as the host decoded them ([`system::MASKMOV`]): VMASKMOVPS/PD and
//! VPMASKMOVD/Q loads and stores (VEX.66.0F38 2C..2F, 8C, 8E; SDM Vol.2 each, exceptions Type 6 "No AC# reported for any mask
//! bit combinations"), MASKMOVDQU (66 0F F7, Type 4) and VMASKMOVDQU (VEX.128.66.0F F7). fexc cuts every block before one
//! and raises the encoding #UDs itself (a register operand of the VEX.0F38 forms, a memory operand of MASKMOVDQU, VEX.W or
//! VEX.L outside the form, VEX.vvvv of VMASKMOVDQU).
//!
//! An element whose mask MSB is set (each byte for MASKMOVDQU) is accessed; one whose MSB is clear is not: a load gives it
//! 0, a store leaves its memory alone, and no fault comes from it (SDM VMASKMOV: "Faults will not occur due to referencing
//! any memory location if the corresponding mask bit for that memory location is 0"). Every access is checked before any
//! byte moves (all or nothing, as one instruction: the reference AMD machine leaves no element of a faulting store written,
//! `natemu-raw/nostop-20261106-01/probe`): the elements in order, each as one explicit access of its width at the CPL (page
//! walks with their A and, for a store, D bits; #PF with CR2 its first faulting byte, which is the machine's si_addr for
//! the first faulting masked-in element, also with masked-in elements below it on a good page; #GP(0) or #SS(0) when not
//! canonical; never #AC); then the data moves, element by element, meeting data breakpoints. A masked-off element sets no
//! A or D bit (the SDM leaves those implementation dependent). MASKMOVDQU and VMASKMOVDQU are one 16-byte write access
//! whatever the mask: the AMD machine faults on the whole region's pages with any mask, an all-zero one included (the SDM
//! leaves the all-zero case implementation dependent), and writes only the masked-in bytes; its non-temporal hint is
//! dropped (a plain store orders more strongly). A load's destination is written whole: its elements, 0 beyond them (to
//! bit 255, VEX). A device page stops `unsupported_mmio` as any vector form's (M3).
//! MASKMOVQ (NP 0F F7) uses physical MM0..7, preflights eight bytes even for a zero mask (an implementation choice
//! explicitly permitted by its SDM page), and writes selected bytes. Unlike the SIMD forms it checks pending #MF and
//! natural alignment under #AC (Vol.3B Table 24-8); canonicality precedes #AC, which precedes page faults as for `linear`.
//! Only successful completion sets TOP=0 and all tags valid, including a zero mask. CR4.OSFXSR does not gate it.
use super::segments::{Exception, address, copy, noncanonical, pages};
use super::xsave::{set_ymm_hi, ymm_hi};
use super::*;

fn ymm(c: &Cpu, r: usize) -> [u8; 32] {
    let mut v = [0; 32];
    for (k, q) in [c.xmm[r][0], c.xmm[r][1], ymm_hi(2 * r), ymm_hi(2 * r + 1)].into_iter().enumerate() {
        v[8 * k..8 * k + 8].copy_from_slice(&q.to_le_bytes());
    }
    v
}

/// Executes the masked move `i` at RIP (any CPL; LOCK was #UD before this) and commits it, or returns its exception with
/// nothing changed (guest memory and registers alike).
pub(super) fn transaction(s: &mut State, i: System) -> Result<(), Exception> {
    let (mmx, vex, store) = (i.cr & 128 != 0, i.cr & 64 != 0, i.cr & 32 != 0);
    // The gating, as the host's gated cuts give it (checked again so the transaction stands alone): VEX (Table 2-21/2-24):
    // #UD without CR4.OSXSAVE or with XCR0[2:1] != 11b, then #NM with CR0.TS; SSE (MASKMOVDQU): #UD with CR0.EM or without
    // CR4.OSFXSR, then #NM with CR0.TS.
    if if vex { !s.cpu.avx_enabled() } else { s.cpu.cr0 & CR0_EM != 0 || (!mmx && s.cpu.cr4 & CR4_OSFXSR == 0) } {
        return Err(UD);
    }
    if s.cpu.cr0 & CR0_TS != 0 {
        return Err([7, 0, 0]);
    }
    if mmx { x87::pending(s)?; }
    let ew = i.size as usize;
    let n = if mmx { 8 } else { (16 << (i.cr >> 4 & 1)) / ew };
    let mm = |r| {
        let mut v = [0u8; 32];
        v[..8].copy_from_slice(&x87::mm(r).to_le_bytes());
        v
    };
    let (data, mask) = if mmx { (mm(i.reg as u64), mm((i.cr & 7) as u64)) }
        else { (ymm(&s.cpu, i.reg as usize), ymm(&s.cpu, (i.cr & 15) as usize)) };
    let at = address(&s.cpu, &i);
    let nc = noncanonical(&i);
    let on = |j: usize| mask[ew * j + ew - 1] & 0x80 != 0;
    let element = |j: usize| at.wrapping_add((ew * j) as u64);
    // The checks: every masked-in element's walk (one 16-byte write for MASKMOVDQU), before any byte moves.
    if mmx && alignment_check(&s.cpu) != 0 && at & 7 != 0
        && paging::canonical(at) && paging::canonical(at.wrapping_add(7)) && at <= at.wrapping_add(7) {
        return Err([17, 0, 0]);
    }
    if ew == 1 {
        pages(s, at, n as u64, true, false, nc)?;
    } else {
        for j in (0..n).filter(|&j| on(j)) {
            pages(s, element(j), ew as u64, store, false, nc)?;
        }
    }
    let mut d = [0u8; 32];
    for j in (0..n).filter(|&j| on(j)) {
        let bytes = &mut d[ew * j..ew * j + ew];
        if store {
            bytes.copy_from_slice(&data[ew * j..ew * j + ew]);
        }
        copy(s, element(j), bytes, store, false, nc)?; // walked above: no fault left
    }
    if !store {
        let r = i.reg as usize;
        let q = |k: usize| u64::from_le_bytes(d[8 * k..8 * k + 8].try_into().unwrap());
        s.cpu.xmm[r] = [q(0), q(1)];
        set_ymm_hi(2 * r, q(2));
        set_ymm_hi(2 * r + 1, q(3));
    }
    if mmx { x87::enter_mmx(); }
    commit(s, i, false);
    Ok(())
}
