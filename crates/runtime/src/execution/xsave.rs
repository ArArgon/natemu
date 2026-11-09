//! FXSAVE/FXRSTOR, XSAVE/XRSTOR, XSAVEOPT, XSAVEC and XGETBV/XSETBV (I5b; SDM Vol.1 10.5 and 13, Vol.2 each instruction), as
//! the profile advertises them (CPU 16): XSAVE with XCR0 ⊆ x87 | SSE | AVX (CPUID.0DH.0:EAX = 7); since November 3 XSAVEOPT and
//! the compaction extensions (CPUID.0DH.1:EAX = 3: XSAVEC, the compacted form of XRSTOR); no XGETBV with ECX = 1, XSAVES or
//! XRSTORS (#UD by the host's decode). XSAVEOPT is XSAVE's transaction (the host decodes it so): XINUSE and XMODIFIED are all
//! ones (a processor that does not implement the init and modified optimizations "implicitly maintains" them so, Vol.1 13.6,
//! 13.8.3), so it saves every component of RFBM and sets those XSTATE_BV bits, exactly XSAVE (MXCSR with RFBM[1] or RFBM[2]
//! for both, 13.9). XSAVEC (13.10) stores the same component bytes, with MXCSR/MXCSR_MASK only for RFBM[1], AVX at 576 (the
//! first and only extended component; CPUID.0DH.2:ECX[1] = 0), XSTATE_BV := RFBM and XCOMP_BV := 2^63 | RFBM, and no other
//! header byte; it does not read XSTATE_BV. The images:
//! - legacy region (FXSAVE's 512 bytes, XSAVE's first 512): FCW, FSW, the abridged tag byte (byte 5 0), FOP/FIP/FCS/FDP/FDS 0
//!   (not modelled, plan I6 deviation 8: so FXSAVE and REX.W's FXSAVE64 store the same bytes), MXCSR, MXCSR_MASK FFFFH,
//!   ST0..ST7 as 80-bit values widened exactly from the doubles at 32 + 16i (bytes 10..15 of each slot 0), XMM0..15 at 160;
//!   bytes 416..511 are never written (464..511 are software's, SDM; 416..463 reserved, as QEMU);
//! - XSAVE header at 512 (XSTATE_BV, XCOMP_BV) and YMM_Hi128 at 576 (CPUID.0DH.2: 256 bytes), kept in the guest-state page
//!   where FEX keeps it ([`frame::AVX_HIGH`]): zero until an XRSTOR loads it, since no AVX form runs yet (I8).
//!
//! Choices the SDM leaves open: misalignment (16 bytes FX*, 64 bytes X*) is #GP(0), never #AC ("in all implementations
//! where #AC is not signaled, a #GP is signaled in its place"), checked before the operand's canonical form and its walk;
//! FXSAVE/FXRSTOR save and restore XMM and MXCSR whatever CR4.OSFXSR ("may not ... implementation dependent"; QEMU skips
//! them); XINUSE is all ones (a processor may track any component as in use), so XSAVE sets XSTATE_BV[i] for every i in
//! RFBM. Gating: FX* #NM with CR0.TS or CR0.EM; X* and XGETBV/XSETBV #UD without CR4.OSXSAVE, then X* #NM with CR0.TS
//! (QEMU 11.1.2 raises no #NM for them). Every transaction is precise: all bytes it reads are read and every page it writes is walked
//! for write before any state or memory changes (a #PF or #GP leaves both as they were). I5c: an FCW or MXCSR with masks
//! clear re-keys the code (`Cpu::code_context`); FXRSTOR/XRSTOR, like FXSAVE/XSAVE, ignore a pending x87 exception (SDM
//! Vol.3 event 16), and an unmasked flag they load is pending for the next waiting x87 instruction.
use super::segments::{Exception, address, linear, noncanonical, pages};
use super::*;

use natemu_abi::x87::{from_f80, to_f80};

const NM: Exception = [7, 0, 0];
/// The legacy region bytes the processor writes, the XSAVE header and the AVX component (offset and size, CPUID.0DH.2).
const LEGACY: usize = 416;
const HEADER: usize = 512;
const AVX: usize = 576;
const AREA: usize = AVX + 256;
const COMPACTED: u64 = 1 << 63;
/// CPUID.(EAX=0DH,ECX=1):EAX.XSAVEC[1] of the generated profile: the compacted form of XRSTOR is #GP(0) without it.
const XSAVEC: bool = {
    let (mut k, mut on) = (0, false);
    while k < cpu_profile::CPUID.len() {
        let (leaf, sub, regs) = cpu_profile::CPUID[k];
        on |= leaf == 0xd && sub == 1 && regs[0] & 2 != 0;
        k += 1;
    }
    on
};

/// Executes FXSAVE ..= XSETBV (any CPL but XSETBV's; LOCK is #UD before this). `Ok`: committed, RIP moves past it.
#[inline(never)]
pub(super) fn transaction(s: &mut State, i: System) -> Result<(), Exception> {
    let (cr0, cr4, xcr0, cpl) = (s.cpu.cr0, s.cpu.cr4, s.cpu.xcr0, s.cpu.cpl);
    let [rax, rcx, rdx] = [s.cpu.gpr[0], s.cpu.gpr[1], s.cpu.gpr[2]];
    let fx = matches!(i.kind, system::FXSAVE | system::FXRSTOR);
    if !fx && cr4 & CR4_OSXSAVE == 0 {
        return Err(UD);
    }
    if matches!(i.kind, system::FXSAVE..=system::XRSTOR | system::XSAVEC)
        && cr0 & if fx { CR0_TS | CR0_EM } else { CR0_TS } != 0
    {
        return Err(NM);
    }
    let rfbm = xcr0 & (rdx << 32 | rax & 0xffff_ffff);
    let va = address(&s.cpu, &i);
    let nc = noncanonical(&i);
    if i.memory == 1 && va & if fx { 15 } else { 63 } != 0 {
        return Err(GP);
    }
    // The canonical check (#GP(0), or #SS(0) for an SS-class reference; a range wrapping past 2^64 is non-canonical) is
    // made on each range below before its walk, by `linear` and `pages`: FX* bytes 0..415, XSAVE the span from its first
    // to its last stored byte, XRSTOR first the base through the header's 64 bytes (the base checked though only the header
    // is read then), then each part it restores.
    let mut img = [0u8; AREA];
    let word = |img: &[u8], at: usize| u64::from_le_bytes(img[at..at + 8].try_into().unwrap());
    match i.kind {
        system::FXSAVE => {
            legacy(&s.cpu, &mut img);
            linear(s, va, &mut img[..LEGACY], true, false, nc)?;
        }
        system::FXRSTOR => {
            linear(s, va, &mut img[..LEGACY], false, false, nc)?;
            let mxcsr = mxcsr(&img)?;
            load_x87(s, &img);
            s.cpu.mxcsr = mxcsr;
            load_xmm(&mut s.cpu, Some(&img));
        }
        system::XSAVE | system::XSAVEC => {
            let compact = i.kind == system::XSAVEC;
            legacy(&s.cpu, &mut img);
            for q in 0..32 {
                img[AVX + 8 * q..AVX + 8 * q + 8].copy_from_slice(&ymm_hi(q).to_le_bytes());
            }
            // The parts RFBM selects (MXCSR and MXCSR_MASK with SSE or, for XSAVE, AVX), then XSTATE_BV (and XSAVEC's
            // XCOMP_BV).
            let parts: [(bool, usize, usize); 6] = [
                (rfbm & 1 != 0, 0, 24),
                (rfbm & 1 != 0, 32, 160),
                (rfbm & if compact { 2 } else { 6 } != 0, 24, 32),
                (rfbm & 2 != 0, 160, LEGACY),
                (true, HEADER, HEADER + if compact { 16 } else { 8 }),
                (rfbm & 4 != 0, AVX, AREA),
            ];
            let (lo, hi) = parts
                .iter()
                .filter(|p| p.0)
                .fold((AREA, 0), |(lo, hi), p| (lo.min(p.1), hi.max(p.2)));
            // Every page the stores reach (both ends of [lo, hi) are stored, so each page in it is) walked for write first;
            // as `linear`, nothing is written unless all of it is guest RAM (M3: a device page stops first).
            let (start, n) = (va.wrapping_add(lo as u64), (hi - lo) as u64);
            let gpa = pages(s, start, n, true, false, nc)?;
            let crosses = (start ^ start.wrapping_add(n - 1)) >> 12 != 0;
            if !mmio::ram(gpa[0]) || (crosses && !mmio::ram(gpa[1])) {
                mmio::system_access(s)
            }
            let bv = if compact {
                img[HEADER + 8..HEADER + 16].copy_from_slice(&(COMPACTED | rfbm).to_le_bytes());
                rfbm
            } else {
                linear(
                    s,
                    va.wrapping_add(HEADER as u64),
                    &mut img[HEADER..HEADER + 8],
                    false,
                    false,
                    nc,
                )?;
                word(&img, HEADER) | rfbm
            };
            img[HEADER..HEADER + 8].copy_from_slice(&bv.to_le_bytes());
            for &(on, from, to) in &parts {
                if on {
                    linear(
                        s,
                        va.wrapping_add(from as u64),
                        &mut img[from..to],
                        true,
                        false,
                        nc,
                    )?;
                }
            }
        }
        system::XRSTOR => {
            // The header (all 64 bytes, in the area's first page with the 64-byte alignment) is read first, but its canonical
            // check starts at the area's base: a non-canonical base is #GP/#SS before a #PF on the header's page.
            let last = va.wrapping_add(HEADER as u64 + 63);
            if !paging::canonical(va) || !paging::canonical(last) || last < va {
                return Err(nc);
            }
            linear(
                s,
                va.wrapping_add(HEADER as u64),
                &mut img[HEADER..AVX],
                false,
                false,
                nc,
            )?;
            let (bv, comp, reserved) = (
                word(&img, HEADER),
                word(&img, HEADER + 8),
                word(&img, HEADER + 16),
            );
            let compact = comp & COMPACTED != 0;
            // Standard form (13.8.1): XSTATE_BV within XCR0, header bytes 23:8 zero (24..63 not checked). Compacted form
            // (13.8.2; #GP without XSAVEC): XCOMP_BV[62:0] within XCR0, XSTATE_BV within XCOMP_BV[62:0], bytes 63:16 zero.
            // XSTATE_BV[63] (a reserved bit; the SDM's "including bit 63" read literally lets it pass, since XCOMP_BV[63] is
            // set) is #GP(0), as on the reference AMD machine (gen_xsave.py's probe).
            if if compact {
                !XSAVEC
                    || comp & !COMPACTED & !xcr0 != 0
                    || bv & !(comp & !COMPACTED) != 0
                    || img[HEADER + 16..AVX].iter().any(|&b| b != 0)
            } else {
                bv & !xcr0 != 0 || comp != 0 || reserved != 0
            } {
                return Err(GP);
            }
            // Compacted: a component of RFBM outside XCOMP_BV is initialized (FORCE_INIT); AVX, the only extended one, is
            // at 576 when XCOMP_BV has it. MXCSR is SSE state there: loaded with it, else 1F80H when SSE is initialized
            // (RFBM[2] plays no part); the standard form loads it for RFBM[1] or RFBM[2] whatever XSTATE_BV.
            let restore = rfbm & bv & if compact { comp } else { !0 };
            let mx = if compact {
                restore & 2 != 0
            } else {
                rfbm & 6 != 0
            };
            let lo = if restore & 1 != 0 {
                0
            } else if mx {
                24
            } else {
                LEGACY
            };
            let hi = if restore & 2 != 0 {
                LEGACY
            } else if restore & 1 != 0 {
                160
            } else if mx {
                32
            } else {
                LEGACY
            };
            if lo < hi {
                linear(
                    s,
                    va.wrapping_add(lo as u64),
                    &mut img[lo..hi],
                    false,
                    false,
                    nc,
                )?;
            }
            if restore & 4 != 0 {
                linear(
                    s,
                    va.wrapping_add(AVX as u64),
                    &mut img[AVX..AREA],
                    false,
                    false,
                    nc,
                )?;
            }
            let mxcsr = if mx {
                Some(mxcsr(&img)?)
            } else if compact && rfbm & 2 != 0 {
                Some(MXCSR_RESET)
            } else {
                None
            };
            if restore & 1 != 0 {
                load_x87(s, &img);
            } else if rfbm & 1 != 0 {
                // The x87 initial configuration (SDM Vol.1 13.6): FCW 037FH, FSW 0, every tag empty, ST0..7 zero.
                s.cpu.fcw = FCW_INIT;
                x87::set_fsw(0);
                x87::set_abridged(0);
                (0..8).for_each(|r| x87::set_mm(r, 0));
            }
            if let Some(m) = mxcsr {
                s.cpu.mxcsr = m;
            }
            if rfbm & 2 != 0 {
                load_xmm(&mut s.cpu, (restore & 2 != 0).then_some(&img[..]));
            }
            if rfbm & 4 != 0 {
                (0..32).for_each(|q| {
                    set_ymm_hi(
                        q,
                        if restore & 4 != 0 {
                            word(&img, AVX + 8 * q)
                        } else {
                            0
                        },
                    )
                });
            }
        }
        system::XGETBV => {
            // XCR0 only: ECX = 1 needs CPUID.(EAX=0DH,ECX=1):EAX.XG1, which the profile does not advertise.
            if rcx as u32 != 0 {
                return Err(GP);
            }
            (s.cpu.gpr[0], s.cpu.gpr[2]) = (xcr0 & 0xffff_ffff, xcr0 >> 32);
        }
        system::XSETBV => {
            // CPL 0; ECX = 0 (XCR0); EDX:EAX one of the profile's values (bit 0 set, bit 2 only with bit 1, no bit outside
            // CPUID.0DH.0:EDX:EAX). XCR0 gates nothing at translation time yet: AVX forms are refused (I8 keys code by it).
            let value = (rdx & 0xffff_ffff) << 32 | rax & 0xffff_ffff;
            if cpl != 0 || rcx as u32 != 0 || !cpu_profile::XCR0_VALID.contains(&value) {
                return Err(GP);
            }
            s.cpu.xcr0 = value;
        }
        _ => halt(s, stop::BAD_REPLY),
    }
    Ok(())
}

/// Quadword `q` (0..32) of YMM_Hi128 in the guest-state page ([`frame::AVX_HIGH`]), and its write.
pub(super) fn ymm_hi(q: usize) -> u64 {
    // SAFETY: FRAME_VA maps the vCPU's frame page; AVX_HIGH + 256 lies inside it.
    unsafe {
        (FRAME_VA as *const u64)
            .byte_add(frame::AVX_HIGH as usize + 8 * (q & 31))
            .read_volatile()
    }
}
pub(super) fn set_ymm_hi(q: usize, v: u64) {
    // SAFETY: as in ymm_hi.
    unsafe {
        (FRAME_VA as *mut u64)
            .byte_add(frame::AVX_HIGH as usize + 8 * (q & 31))
            .write_volatile(v)
    }
}

/// The legacy region's first 416 bytes for `c` and the x87 state in the page.
fn legacy(c: &Cpu, img: &mut [u8]) {
    img[0..2].copy_from_slice(&(c.fcw as u16).to_le_bytes());
    img[2..4].copy_from_slice(&(x87::fsw(c.fcw) as u16).to_le_bytes());
    img[4] = x87::abridged() as u8;
    img[24..28].copy_from_slice(&(c.mxcsr as u32).to_le_bytes());
    img[28..32].copy_from_slice(&(MXCSR_MASK as u32).to_le_bytes());
    let top = x87::top();
    for k in 0..8 {
        let (mant, se) = to_f80(x87::mm(top + k));
        let at = 32 + 16 * k as usize;
        img[at..at + 8].copy_from_slice(&mant.to_le_bytes());
        img[at + 8..at + 10].copy_from_slice(&se.to_le_bytes());
    }
    for (r, x) in c.xmm.iter().enumerate() {
        img[160 + 16 * r..168 + 16 * r].copy_from_slice(&x[0].to_le_bytes());
        img[168 + 16 * r..176 + 16 * r].copy_from_slice(&x[1].to_le_bytes());
    }
}
/// The image's MXCSR: #GP(0) for a 1 in a bit MXCSR_MASK clears.
fn mxcsr(img: &[u8]) -> Result<u64, Exception> {
    let m = u64::from(u32::from_le_bytes(img[24..28].try_into().unwrap()));
    if m & !MXCSR_MASK != 0 { Err(GP) } else { Ok(m) }
}
/// FCW, FSW, the abridged tags and ST0..7 (rounded to doubles by the new FCW.RC: the reduced-precision deviation).
fn load_x87(s: &mut State, img: &[u8]) {
    let fcw = u64::from(u16::from_le_bytes([img[0], img[1]]));
    let fsw = u64::from(u16::from_le_bytes([img[2], img[3]]));
    s.cpu.fcw = fcw;
    x87::set_fsw(fsw);
    x87::set_abridged(u64::from(img[4]));
    for k in 0..8 {
        let at = 32 + 16 * k as usize;
        let mant = u64::from_le_bytes(img[at..at + 8].try_into().unwrap());
        x87::set_mm(
            (fsw >> 11) + k,
            from_f80(
                mant,
                u16::from_le_bytes([img[at + 8], img[at + 9]]),
                fcw >> 10 & 3,
            ),
        );
    }
}
/// XMM0..15 from the image, or zero (their initial configuration).
fn load_xmm(c: &mut Cpu, img: Option<&[u8]>) {
    for (r, x) in c.xmm.iter_mut().enumerate() {
        let q =
            |at: usize| img.map_or(0, |b| u64::from_le_bytes(b[at..at + 8].try_into().unwrap()));
        *x = [q(160 + 16 * r), q(168 + 16 * r)];
    }
}
