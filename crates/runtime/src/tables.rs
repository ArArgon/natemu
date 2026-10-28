//! This owner's native translation tables: guest shadows through
//! `natemu_abi::shadow::Space`, with maintenance on this CPU.
use super::*;
use natemu_abi::shadow::{Error, LOWER_ROOT, Maintenance, POOL_PAGES, Space};

const OUTPUT: u64 = 0x0000_ffff_ffff_f000;

/// Local (non-broadcast) maintenance of this owner's ASID: each owner's tables are
/// walked only by its own vCPU, and its entries are not global (they carry its ASID,
/// TTBR0_EL1[63:48]), so no other vCPU's entries are hit or invalidated. Shared tables
/// would need the inner-shareable TLBI forms.
pub struct Tlb {
    /// The ASID in TLBI operand position (bits 63:48).
    asid: u64,
}
impl Maintenance for Tlb {
    fn publish(&mut self) {
        unsafe { asm!("dsb ishst", "isb") }
    }
    fn invalidate(&mut self, va: u64) {
        // TLBI VAE1 takes VA[55:12] and the ASID.
        let operand = (va >> 12) & ((1 << 44) - 1) | self.asid;
        unsafe { asm!("dsb ishst", "tlbi vae1, {0}", "dsb ish", "isb", in(reg) operand) }
    }
    fn invalidate_all(&mut self) {
        // ASIDE1 leaves global entries: ASID 0 (untagged, global shadows) drops everything.
        if self.asid == 0 {
            unsafe { asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb") }
        } else {
            unsafe { asm!("dsb ishst", "tlbi aside1, {0}", "dsb ish", "isb", in(reg) self.asid) }
        }
    }
}

/// The owner's tables; the window's IPA and the ASID come from its TTBR0, and the
/// upper half is in use unless TCR_EL1.EPD1 (bit 23) disables TTBR1 walks. ASID 0 is
/// untagged (global shadows, one tag for all owners: the S3a control); an owner with
/// an ASID of its own tags its shadows with it.
pub fn space() -> Space<Tlb> {
    let (root, tcr): (u64, u64);
    unsafe { asm!("mrs {0}, ttbr0_el1", "mrs {1}, tcr_el1", out(reg) root, out(reg) tcr) };
    Space {
        window: PRIVATE as *mut u8,
        ipa: (root & OUTPUT) - LOWER_ROOT as u64,
        pool_pages: POOL_PAGES,
        upper: tcr & 1 << 23 == 0,
        tagged: root >> 48 != 0,
        tlb: Tlb { asid: root & 0xffff << 48 },
    }
}

/// IPA behind resident address `va`, from the resident's own private mapping.
fn native(va: u64) -> Option<u64> {
    let par: u64;
    unsafe { asm!("at s1e1r, {0}", "isb", "mrs {1}, par_el1", in(reg) va, out(reg) par) };
    (par & 1 == 0).then_some((par & OUTPUT) | (va & 4095))
}

/// Shadow guest linear page `va` with guest-physical page `gpa` of `ram`,
/// read-only unless `writable` (the walk granted W and D is set).
pub fn install(va: u64, ram: &GuestRam, gpa: u64, writable: bool) -> Result<(), Error> {
    let page = ram.range(gpa & !4095, 4096).ok_or(Error::OutsideRam)?;
    let ipa = native(page as u64).ok_or(Error::OutsideRam)?;
    space().map(va & !4095, ipa, writable)
}
pub fn unmap(va: u64) -> Result<(), Error> {
    space().unmap(va)
}
/// The native leaf descriptor shadowing `va` (0 when none).
pub fn descriptor(va: u64) -> u64 {
    space().descriptor(va)
}
