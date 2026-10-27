//! This owner's native translation tables: guest shadows through
//! `natemu_abi::shadow::Space`, with maintenance on this CPU.
use super::*;
use natemu_abi::shadow::{Error, LOWER_ROOT, Maintenance, POOL_PAGES, Space};

const OUTPUT: u64 = 0x0000_ffff_ffff_f000;

/// Local (non-broadcast) maintenance: each owner's tables are walked only by
/// its own vCPU. Shared tables would need the inner-shareable TLBI forms.
pub struct Tlb;
impl Maintenance for Tlb {
    fn publish(&mut self) {
        unsafe { asm!("dsb ishst", "isb") }
    }
    fn invalidate(&mut self, va: u64) {
        // TLBI VAE1 takes VA[55:12]; ASID 0, and shadow entries are global.
        let operand = (va >> 12) & ((1 << 44) - 1);
        unsafe { asm!("dsb ishst", "tlbi vae1, {0}", "dsb ish", "isb", in(reg) operand) }
    }
    fn invalidate_all(&mut self) {
        unsafe { asm!("dsb ishst", "tlbi vmalle1", "dsb ish", "isb") }
    }
}

/// The owner's tables; the window's IPA comes from its TTBR0 root, and the
/// upper half is in use unless TCR_EL1.EPD1 (bit 23) disables TTBR1 walks.
pub fn space() -> Space<Tlb> {
    let (root, tcr): (u64, u64);
    unsafe { asm!("mrs {0}, ttbr0_el1", "mrs {1}, tcr_el1", out(reg) root, out(reg) tcr) };
    Space {
        window: PRIVATE as *mut u8,
        ipa: (root & OUTPUT) - LOWER_ROOT as u64,
        pool_pages: POOL_PAGES,
        upper: tcr & 1 << 23 == 0,
        tlb: Tlb,
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
#[cfg_attr(not(feature = "families"), allow(dead_code))]
pub fn unmap(va: u64) -> Result<(), Error> {
    space().unmap(va)
}
/// The native leaf descriptor shadowing `va` (0 when none).
pub fn descriptor(va: u64) -> u64 {
    space().descriptor(va)
}
#[cfg_attr(not(feature = "families"), allow(dead_code))]
pub fn clear() -> Result<(), Error> {
    space().clear()
}
