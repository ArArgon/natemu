//! Legacy two-level, 4-KiB translation for the PSE-off profile.
use crate::paging::{Access, Control, Fault, Translation};
pub trait Tables {
    fn read32(&mut self, gpa: u64) -> Option<u32>;
    fn write32(&mut self, gpa: u64, value: u32) -> bool;
}
pub fn walk<T: Tables>(
    tables: &mut T,
    c: Control,
    address: u64,
    access: Access,
) -> Result<Translation, Fault> {
    if !matches!(c.cpl, 0 | 3)
        || c.wp > 1
        || c.nxe != 0
        || c.smep != 0
        || address > u32::MAX as u64
        || c.cr3 > u32::MAX as u64
        || c.cr3 & 0x18 != 0
    {
        return Err(Fault::Unsupported);
    }
    let error = u64::from(access == Access::Write) * 2 | u64::from(c.cpl == 3) * 4;
    let pf = |present| Fault::Page {
        address,
        error: error | present,
    };
    let pd = (c.cr3 & 0xffff_f000) + ((address >> 22) & 1023) * 4;
    let pde = tables.read32(pd).ok_or(Fault::Unsupported)?;
    if pde & 1 == 0 {
        return Err(pf(0));
    }
    // PDE.PS is ignored with CR4.PSE=0; it is not a large-page selector here.
    if pde & 32 == 0 && !tables.write32(pd, pde | 32) {
        return Err(Fault::Unsupported);
    }
    let pt = (pde as u64 & 0xffff_f000) + ((address >> 12) & 1023) * 4;
    let pte = tables.read32(pt).ok_or(Fault::Unsupported)?;
    if pte & 1 == 0 {
        return Err(pf(0));
    }
    let writable = pde & pte & 2 != 0;
    let user = pde & pte & 4 != 0;
    if (c.cpl == 3 && !user) || (access == Access::Write && !writable && (c.cpl == 3 || c.wp != 0))
    {
        return Err(pf(1));
    }
    let next = pte | 32 | if access == Access::Write { 64 } else { 0 };
    if next != pte && !tables.write32(pt, next) {
        return Err(Fault::Unsupported);
    }
    Ok(Translation {
        gpa: (pte as u64 & 0xffff_f000) | (address & 4095),
        leaf_bytes: 4096,
        writable: writable || (c.cpl == 0 && c.wp == 0),
        user,
        executable: true,
        dirty: pte & 64 != 0 || access == Access::Write,
        wb: (pde | pte) & 0x18 == 0 && pte & 128 == 0,
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    struct Ram([u32; 2048]);
    impl Tables for Ram {
        fn read32(&mut self, p: u64) -> Option<u32> {
            self.0.get((p / 4) as usize).copied()
        }
        fn write32(&mut self, p: u64, v: u32) -> bool {
            if let Some(x) = self.0.get_mut((p / 4) as usize) {
                *x = v;
                true
            } else {
                false
            }
        }
    }
    #[test]
    fn widths_permissions_and_pse_off_ps_are_distinct_from_four_level_paging() {
        let mut m = Ram([0; 2048]);
        m.0[0] = 0x1087;
        m.0[1024 + 256] = 0x5005;
        let mut c = Control {
            cr3: 3,
            cpl: 0,
            wp: 0,
            nxe: 0,
            smep: 0,
        };
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Write).unwrap().gpa,
            0x5000
        );
        assert_eq!(m.0[0], 0x10a7);
        assert_eq!(m.0[1280], 0x5065);
        c.wp = 1;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Write),
            Err(Fault::Page {
                address: 0x100000,
                error: 3
            })
        );
        c.cpl = 3;
        c.wp = 0;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Write),
            Err(Fault::Page {
                address: 0x100000,
                error: 7
            })
        );
        m.0[1280] = 0;
        c.cpl = 0;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Fetch),
            Err(Fault::Page {
                address: 0x100000,
                error: 0
            })
        );
        c.cr3 = 0x10;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Read),
            Err(Fault::Unsupported)
        );
    }
}
