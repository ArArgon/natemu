//! This owner's native translation tables: guest shadows through
//! `natemu_abi::shadow::Space`, with maintenance on this CPU. Two shadow spaces, one per x86 privilege class
//! (supervisor: CPL 0-2; user: CPL 3), each with its own roots and ASID; generated code always runs with the space of
//! the vCPU's CPL loaded ([`select`]), and every removal hits both ([`unmap_large`], [`reset`]).
//! M2 checkpoint C: user code that is not 64-bit code runs under a third pair of roots with a third ASID, which reach the
//! user space's shadows below 4 GiB and no other guest address (`shadow::COMPAT_LOWER_ROOT`).
use super::*;
use natemu_abi::shadow::{CLASS_ROOTS, Error, Maintenance, POOL_PAGES, Space, asids};

const OUTPUT: u64 = 0x0000_ffff_ffff_f000;

/// Local (non-broadcast) maintenance of this owner's ASIDs: each owner's tables are
/// walked only by its own vCPU, and its entries are not global (they carry the ASID of
/// their space, TTBR0_EL1[63:48]), so no other vCPU's entries are hit or invalidated.
/// Shared tables would need the inner-shareable TLBI forms.
pub struct Tlb {
    /// The space's ASID and the owner's three (supervisor, user, compat), in TLBI operand position (bits 63:48).
    asid: u64,
    all: [u64; 3],
}
impl Maintenance for Tlb {
    fn publish(&mut self) {
        unsafe { asm!("dsb ishst", "isb") }
    }
    fn invalidate(&mut self, va: u64) {
        // TLBI VAE1 takes VA[55:12] and the ASID. M2 checkpoint C: a user shadow below 4 GiB is also reached through the
        // compat root (the same leaf entry, `shadow::COMPAT_L1`), under its ASID.
        let page = (va >> 12) & ((1 << 44) - 1);
        unsafe { asm!("dsb ishst", "tlbi vae1, {0}", in(reg) page | self.asid) };
        if self.asid == self.all[1] && va >> 32 == 0 {
            unsafe { asm!("tlbi vae1, {0}", in(reg) page | self.all[2]) };
        }
        unsafe { asm!("dsb ish", "isb") }
    }
    fn invalidate_all(&mut self) {
        let [s, u, c] = self.all;
        unsafe {
            asm!("dsb ishst", "tlbi aside1, {0}", "tlbi aside1, {1}", "tlbi aside1, {2}", "dsb ish", "isb",
                 in(reg) s, in(reg) u, in(reg) c)
        }
    }
}

/// The owner's `user` (CPL 3) or supervisor space; the window's IPA and the ASIDs come from its TTBR0 (whichever
/// root it holds: `shadow::asids`), and the upper half is in use unless TCR_EL1.EPD1 (bit 23) disables TTBR1 walks. Every
/// owner has ASIDs of its own (never 0) and tags each space's shadows with that space's. The compat root has no space
/// of its own: its shadows are the user space's below 4 GiB.
pub fn space(user: bool) -> Space<Tlb> {
    let (root, tcr): (u64, u64);
    unsafe { asm!("mrs {0}, ttbr0_el1", "mrs {1}, tcr_el1", out(reg) root, out(reg) tcr) };
    let (class, all) = asids(root >> 48);
    let all = all.map(|a| a << 48);
    Space {
        window: PRIVATE as *mut u8,
        ipa: (root & OUTPUT) - CLASS_ROOTS[class][0] as u64,
        pool_pages: POOL_PAGES,
        upper: tcr & 1 << 23 == 0,
        user,
        tlb: Tlb { asid: all[user as usize], all },
    }
}
/// The space of an access at `cpl`: x86 grants supervisor rights at CPL 0-2, user rights at 3.
fn user(cpl: u64) -> bool {
    cpl == 3
}

/// Loads the roots of `cpl` (TTBR0 with its ASID, and TTBR1), unless they are loaded: called before every entry
/// into generated code, so a CPL change needs no flush. M2 checkpoint C: with `compat` (code that is not 64-bit) CPL 3
/// runs under the compat root, which maps the user space's shadows below 4 GiB and no other guest address, so a native
/// address above 4 GiB always aborts and the resident makes the access at the wrapped address (`access_aborted`). All
/// lower roots map the resident's private region alike,
/// and all upper roots the guest-state page's EL0 alias (G1, `cache::Control::guest_state_va`, an upper-root entry below 256
/// that no shadow uses). TTBR1 carries no ASID (TCR_EL1.A1 = 0: TTBR0's tags both halves), so TTBR1 walks are disabled
/// (EPD1) while the roots change: no walk ever pairs one space's upper root with the other's ASID. EPD1 is set only
/// inside this function, at EL1 with generated code not running, and cleared before it returns, so the alias is
/// walkable whenever generated code runs.
pub fn select(cpl: u64, compat: bool) {
    let root: u64;
    unsafe { asm!("mrs {0}, ttbr0_el1", out(reg) root) };
    let (loaded, all) = asids(root >> 48);
    let class = if !user(cpl) { 0 } else if compat { 2 } else { 1 };
    if loaded == class {
        return;
    }
    let ipa = (root & OUTPUT) - CLASS_ROOTS[loaded][0] as u64;
    let [lower, upper] = CLASS_ROOTS[class];
    let (ttbr0, ttbr1) = ((ipa + lower as u64) | all[class] << 48, ipa + upper as u64);
    unsafe {
        asm!("mrs {t}, tcr_el1", "orr {d}, {t}, #0x800000", "msr tcr_el1, {d}", "isb",
             "msr ttbr1_el1, {t1}", "msr ttbr0_el1, {t0}", "isb", "msr tcr_el1, {t}", "isb",
             t = out(reg) _, d = out(reg) _, t0 = in(reg) ttbr0, t1 = in(reg) ttbr1);
    }
}

/// IPA behind resident address `va`, from the resident's own private mapping.
fn native(va: u64) -> Option<u64> {
    let par: u64;
    unsafe { asm!("at s1e1r, {0}", "isb", "mrs {1}, par_el1", in(reg) va, out(reg) par) };
    (par & 1 == 0).then_some((par & OUTPUT) | (va & 4095))
}

/// Shadow guest linear page `va` with guest-physical page `gpa` of `ram` in the space of `cpl` (whose rights the walk
/// granted), read-only unless `writable` (the walk granted W and D is set); `global`: the walk's translation is global
/// (its leaf's G with CR4.PGE), so the shadow may outlive a CR3 load ([`reset_cr3`]).
pub fn install(
    cpl: u64,
    va: u64,
    ram: &GuestRam,
    gpa: u64,
    writable: bool,
    global: bool,
) -> Result<(), Error> {
    let page = ram.range(gpa & !4095, 4096).ok_or(Error::OutsideRam)?;
    let ipa = native(page as u64).ok_or(Error::OutsideRam)?;
    let mut space = space(user(cpl));
    match global {
        true => space.map_global(va & !4095, ipa, writable),
        false => space.map(va & !4095, ipa, writable),
    }
}
/// L3 (amendment): shadow guest linear page `va`, which maps an unassigned guest-physical page, read-only onto the shared
/// all-ones page (`natemu_abi::ALL_ONES_IPA`, read-only at stage 2 too; never executable: `PAGE` has PXN/UXN).
pub fn install_ones(cpl: u64, va: u64) -> Result<(), Error> {
    space(user(cpl)).map(va & !4095, natemu_abi::ALL_ONES_IPA, false)
}
/// Removes the shadows of the 2 MiB-aligned range holding `va` from both spaces (an INVLPG). An address no space can map
/// (the gap, or the upper half with TTBR1 walks off) has no shadow: nothing to remove.
pub fn unmap_large(va: u64) -> Result<(), Error> {
    for user in [false, true] {
        match space(user).unmap_large(va) {
            Ok(()) | Err(Error::Noncanonical) => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}
/// Removes the shadow of linear page `va` from both spaces (M2: a UC page shadowed for one instruction).
pub fn unmap(va: u64) -> Result<(), Error> {
    for user in [false, true] {
        match space(user).unmap(va) {
            Ok(()) | Err(Error::Noncanonical) => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}
/// Removes every shadow of both spaces.
pub fn reset() {
    space(false).reset()
}
/// Perf chunk 14: a CR3 load's reset (`Space::reset_cr3`): the supervisor space's global upper-half shadows stay, its
/// non-global ones go (listed; a full reset past `LIST_MAX`). Whether every shadow went.
pub fn reset_cr3() -> bool {
    space(false).reset_cr3()
}
/// The native leaf descriptor shadowing `va` in the space of `cpl` (0 when none).
pub fn descriptor(cpl: u64, va: u64) -> u64 {
    space(user(cpl)).descriptor(va)
}
