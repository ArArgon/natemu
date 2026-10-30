//! System calls, segment bases, entropy and cache maintenance in 64-bit mode (plan S3; SDM Vol.2 SWAPGS, SYSCALL,
//! SYSRET, RDFSBASE/RDGSBASE, WRFSBASE/WRGSBASE, RDRAND, WBINVD, INVD, WBNOINVD, CLFLUSH; LAR, LSL, VERR, VERW are
//! [`segments::inspect`]'s), executed as system transactions: every check raises its exception before any change.
//!
//! - SWAPGS (CPL 0, else #GP(0)) exchanges the bank's GS.base with IA32_KERNEL_GS_BASE; the guest-state page takes the new
//!   base at the next block entry (I7).
//! - SYSCALL (#UD without EFER.SCE): RCX := the next RIP, R11 := RFLAGS, RFLAGS &= !IA32_FMASK, CS := STAR[47:32] & !3 and
//!   SS := STAR[47:32] + 8 (unmasked, as the SDM writes it) with the SDM's fixed flat attributes (no descriptor is read), CPL 0, RIP := IA32_LSTAR. No stack switch.
//! - SYSRET (#UD without EFER.SCE, then #GP(0) at CPL > 0, then #GP(0) for a non-canonical RCX, Intel's check at CPL 0
//!   before any change): RIP := RCX, RFLAGS := R11 & 3C7FD7H | 2, CS := (STAR[63:48] + 16) | 3 (64-bit) and SS :=
//!   (STAR[63:48] + 8) | 3 with the fixed attributes, CPL 3. SYSRETL returns to compatibility mode, which is not built:
//!   [`set_cpl`] stops `unsupported_mode` with the new RFLAGS, CS and SS and RIP at the instruction. CR0.AM with an AC from R11
//!   turns alignment checking on (X2): the SYSRET completes and the next misaligned access at CPL 3 is #AC(0).
//! - S4, single-step: SYSCALL and SYSRET trap (#DB, DR6.BS) right after themselves when the TF they leave is 1, not when
//!   TF was 1 at their start: a SYSCALL whose FMASK clears TF enters CPL 0 without a trap, and a SYSRET loading TF = 1
//!   traps before the first CPL 3 instruction (unlike IRET and POPF, whose TF acts after the next instruction). The SDM's
//!   SYSCALL/SYSRET pages say nothing either way; this is pinned QEMU's rule (`gen_eob_syscall`: "TF is checked after the
//!   syscall insn completes") and the behaviour Linux relies on (`entry_64.S`: SYSRET "can restore TF, but unlike IRET,
//!   restoring TF results in a trap from userspace immediately after SYSRET").
//! - RDFSBASE..WRGSBASE (#UD without CR4.FSGSBASE, any CPL): 32-bit forms read the low half zero-extended and write the
//!   zero-extended low half; a 64-bit write of a non-canonical value is #GP(0).
//! - RDRAND: the next word of the vCPU's entropy pool ([`Entropy`], filled by the host): CF = 1, OF SF ZF AF PF = 0, the
//!   destination written at its size (16 bits merge, 32 zero-extend); an empty pool is refilled by one host exit
//!   ([`REQUEST_ENTROPY`], K2), and a pool still empty after it (the host's source failed) is RDRAND's architectural
//!   failure, CF = 0 and a zero destination.
//! - WBINVD, INVD, WBNOINVD (CPL 0, else #GP(0)) have no architectural effect on memory here: guest RAM is coherent
//!   write-back with no cache model, so there is nothing to write back or to discard. INVD's discard without write-back
//!   is therefore WBINVD (the SDM lets INVD lose data, never invent it; no data is lost here). Like every system
//!   instruction each is a draining entry ([`find`]).
//! - CLFLUSH m8 has the faults of a byte load at the CPL (SDM Vol.2 CLFLUSH: "subject to all permission checking and
//!   faults associated with a byte load"): #GP(0)/#SS(0) when not canonical, #PF from the walk (which sets A bits as a
//!   load does); the flush itself has nothing to do.
use super::*;
use segments::Exception;

const GP0: Exception = GP;
const EFER_SCE: u64 = 1;
const CR4_FSGSBASE: u64 = 1 << 16;
const RFLAGS_TF: u64 = 1 << 8;
/// RFLAGS bits SYSRET loads from R11 (SDM Vol.2 SYSRET: RF, VM and the reserved bits 3, 5, 15, 22..63 cleared).
const SYSRET_FLAGS: u64 = 0x3c_7fd7;
/// The fixed hidden attributes SYSCALL and SYSRET load (SDM Vol.2 SYSCALL, SYSRET; `Segment::attributes` encoding):
/// execute/read accessed code, S, P, G and L (64-bit) or D (compatibility); read/write accessed data, S, P, B, G; DPL 0 or 3.
const CS64: u16 = 0xa09b;
const CS32: u16 = 0xc09b;
const SS_FLAT: u16 = 0xc093;
const DPL3: u16 = 3 << 5;

fn slot(index: u32) -> usize {
    cpu::msr(index).unwrap()
}
fn flat(selector: u16, attributes: u16) -> Segment {
    Segment { selector, attributes, limit: u32::MAX, base: 0 }
}

/// Executes `i` (SWAPGS ..= CLFLUSH). `Ok(None)`: committed, RIP moves past it; `Ok(Some(rip))`: SYSCALL or SYSRET
/// committed to `rip`. On `Err` nothing changed.
pub(super) fn execute(s: &mut State, i: System) -> Result<Option<u64>, Exception> {
    let c = s.cpu;
    match i.kind {
        system::SWAPGS => {
            if c.cpl != 0 {
                return Err(GP0);
            }
            let at = slot(cpu::MSR_KERNEL_GS_BASE);
            s.cpu.gs_base = msr_get(at);
            msr_set(at, c.gs_base);
        }
        system::SYSCALL => {
            if c.efer & EFER_SCE == 0 {
                return Err(UD);
            }
            let selector = (msr_get(slot(cpu::MSR_STAR)) >> 32) as u16;
            s.cpu.gpr[1] = c.rip.wrapping_add(u64::from(i.length));
            s.cpu.gpr[11] = c.rflags;
            s.cpu.rflags = c.rflags & !msr_get(slot(cpu::MSR_FMASK)) | 2;
            s.cpu.seg[SREG_CS] = flat(selector & 0xfffc, CS64);
            s.cpu.seg[SREG_SS] = flat(selector.wrapping_add(8), SS_FLAT);
            set_cpl(s, 0);
            s.debug = s.debug & !debug::STEP | if s.cpu.rflags & RFLAGS_TF != 0 { debug::STEP } else { 0 };
            return Ok(Some(msr_get(slot(cpu::MSR_LSTAR))));
        }
        system::SYSRET => {
            if c.efer & EFER_SCE == 0 {
                return Err(UD);
            }
            if c.cpl != 0 {
                return Err(GP0);
            }
            let wide = i.size == 8;
            let rip = if wide { c.gpr[1] } else { c.gpr[1] & 0xffff_ffff };
            if !paging::canonical(rip) {
                return Err(GP0);
            }
            let rflags = c.gpr[11] & SYSRET_FLAGS | 2;
            let base = (msr_get(slot(cpu::MSR_STAR)) >> 48) as u16;
            s.cpu.rflags = rflags;
            s.cpu.seg[SREG_CS] = flat(base.wrapping_add(if wide { 16 } else { 0 }) | 3, if wide { CS64 } else { CS32 } | DPL3);
            s.cpu.seg[SREG_SS] = flat(base.wrapping_add(8) | 3, SS_FLAT | DPL3);
            set_cpl(s, 3);
            s.debug = s.debug & !debug::STEP | if rflags & RFLAGS_TF != 0 { debug::STEP } else { 0 };
            return Ok(Some(rip));
        }
        system::RDFSBASE..=system::WRGSBASE => {
            if c.cr4 & CR4_FSGSBASE == 0 {
                return Err(UD);
            }
            let mask = if i.size == 8 { u64::MAX } else { 0xffff_ffff };
            let fs = matches!(i.kind, system::RDFSBASE | system::WRFSBASE);
            if i.kind <= system::RDGSBASE {
                s.cpu.gpr[i.reg as usize] = if fs { c.fs_base } else { c.gs_base } & mask;
            } else {
                let v = c.gpr[i.reg as usize] & mask;
                if !paging::canonical(v) {
                    return Err(GP0);
                }
                *if fs { &mut s.cpu.fs_base } else { &mut s.cpu.gs_base } = v;
            }
        }
        system::LAR..=system::VERW => segments::inspect(s, i)?,
        system::RDRAND => {
            let pool = ENTROPY_VA as *mut Entropy;
            // SAFETY: the pool is this vCPU's own record in its state page (EL1 read/write); the host writes it only while
            // the vCPU is in the host.
            let value = unsafe {
                if (&raw const (*pool).taken).read_volatile() >= ENTROPY_WORDS as u64 {
                    refill(s);
                }
                let taken = (&raw const (*pool).taken).read_volatile();
                (taken < ENTROPY_WORDS as u64).then(|| {
                    (&raw mut (*pool).taken).write_volatile(taken + 1);
                    (&raw const (*pool).words[taken as usize]).read_volatile()
                })
            };
            let v = value.unwrap_or(0);
            let r = &mut s.cpu.gpr[i.reg as usize];
            *r = match i.size {
                2 => *r & !0xffff | v & 0xffff,
                4 => v & 0xffff_ffff,
                _ => v,
            };
            s.cpu.rflags = c.rflags & !ARITHMETIC_FLAGS | u64::from(value.is_some());
        }
        system::WBINVD => {
            if c.cpl != 0 {
                return Err(GP0);
            }
        }
        system::CLFLUSH => {
            // The walk of a byte load (its faults; M3: a device page passes, uncached, nothing to flush).
            segments::pages(s, segments::address(&c, &i), 1, false, false, segments::noncanonical(&i))?;
        }
        _ => halt(s, stop::BAD_REPLY),
    }
    Ok(None)
}
/// K2: an empty entropy pool is refilled by one host exit (the host tops it up at every exit). The state goes to the
/// record and back in place (no copy on this frame: it is on the gate's deepest path).
#[inline(never)]
fn refill(s: &mut State) {
    (s.request, s.reply) = (REQUEST_ENTROPY, NONE);
    let bytes = size_of::<State>();
    unsafe {
        copy_record(record().cast(), (&raw const *s).cast(), bytes);
        asm!("dsb sy", "hvc #{imm}", imm = const TRANSLATE_HVC);
        copy_record((&raw mut *s).cast(), record().cast(), bytes);
    }
    s.request = NONE;
    if s.reply != REPLY_ENTROPY {
        halt(s, stop::BAD_REPLY)
    }
}
