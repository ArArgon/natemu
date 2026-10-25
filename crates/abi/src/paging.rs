//! Bounded four-level x86 translation for the admitted 48/40-bit profile.
//! The caller owns page-table mutation and physical-route resolution.
pub const PA_MASK: u64 = 0x0000_00ff_ffff_f000;
pub const ACCESSED: u64 = 1 << 5;
pub const DIRTY: u64 = 1 << 6;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub struct Control {
    pub cr3: u64,
    pub cpl: u64,
    pub wp: u64,
    pub nxe: u64,
    pub smep: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    Read,
    Write,
    Fetch,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault {
    Page { address: u64, error: u64 },
    Noncanonical,
    Unsupported,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Translation {
    pub gpa: u64,
    pub leaf_bytes: u64,
    pub writable: bool,
    pub user: bool,
    pub executable: bool,
    pub dirty: bool,
    pub wb: bool,
}
pub trait Tables {
    fn read(&mut self, gpa: u64) -> Option<u64>;
    fn write(&mut self, gpa: u64, value: u64) -> bool;
}

pub fn walk<T: Tables>(
    memory: &mut T,
    c: Control,
    va: u64,
    access: Access,
) -> Result<Translation, Fault> {
    if !matches!(c.cpl, 0 | 3) || c.wp > 1 || c.nxe > 1 || c.smep > 1 || c.cr3 & !PA_MASK != 0 {
        return Err(Fault::Unsupported);
    }
    if va >> 47 != 0 && va >> 47 != 0x1ffff {
        return Err(Fault::Noncanonical);
    }
    let base_error: u64 = if access == Access::Write { 2 } else { 0 }
        | if c.cpl == 3 { 4 } else { 0 }
        | if access == Access::Fetch && (c.nxe != 0 || c.smep != 0) {
            16
        } else {
            0
        };
    let pf = |extra: u64| Fault::Page {
        address: va,
        error: base_error | extra,
    };
    let (mut table, mut rw, mut user, mut executable, mut wb) = (c.cr3, true, true, true, true);
    // Match the selected QEMU ordering: nonleaf A before descent, leaf A/D after permissions.
    for level in (1..=4).rev() {
        let at = table + ((va >> (12 + 9 * (level - 1))) & 511) * 8;
        let value = memory.read(at).ok_or(Fault::Unsupported)?;
        if value & 1 == 0 {
            return Err(pf(0));
        }
        let large = level == 2 && value & 128 != 0;
        if value & 0x000f_ff00_0000_0000 != 0
            || (c.nxe == 0 && value >> 63 != 0)
            || (level >= 3 && value & 128 != 0)
            || (large && value & 0x1fe000 != 0)
        {
            return Err(pf(9));
        }
        rw &= value & 2 != 0;
        user &= value & 4 != 0;
        executable &= value >> 63 == 0;
        wb &=
            value & 0x18 == 0 && !(level == 1 && value & 128 != 0) && !(large && value & 4096 != 0);

        if level == 1 || large {
            if (c.cpl == 3 && !user)
                || (access == Access::Write && !rw && (c.cpl == 3 || c.wp != 0))
                || (access == Access::Fetch && (!executable || (c.cpl == 0 && c.smep != 0 && user)))
            {
                return Err(pf(1));
            }
            let next = value | ACCESSED | if access == Access::Write { DIRTY } else { 0 };
            if next != value && !memory.write(at, next) {
                return Err(Fault::Unsupported);
            }
            let size = if large { 0x20_0000 } else { 4096 };
            return Ok(Translation {
                gpa: (value & PA_MASK & !(size - 1)) | (va & (size - 1)),
                leaf_bytes: size,
                writable: rw || (c.cpl == 0 && c.wp == 0),
                user,
                executable,
                dirty: value & DIRTY != 0 || access == Access::Write,
                wb,
            });
        }
        if value & ACCESSED == 0 && !memory.write(at, value | ACCESSED) {
            return Err(Fault::Unsupported);
        }
        table = value & PA_MASK;
    }
    Err(Fault::Unsupported)
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Ram([u64; 2048]);
    impl Tables for Ram {
        fn read(&mut self, p: u64) -> Option<u64> {
            self.0.get((p / 8) as usize).copied()
        }
        fn write(&mut self, p: u64, v: u64) -> bool {
            if let Some(x) = self.0.get_mut((p / 8) as usize) {
                *x = v;
                true
            } else {
                false
            }
        }
    }
    fn setup() -> (Ram, Control) {
        let mut m = Ram([0; 2048]);
        m.0[0] = 0x1007;
        m.0[512] = 0x2007;
        m.0[1024] = 0x3007;
        m.0[1536 + 256] = 0x5007;
        (
            m,
            Control {
                cr3: 0,
                cpl: 0,
                wp: 1,
                nxe: 1,
                smep: 0,
            },
        )
    }
    #[test]
    fn read_then_write_commits_accessed_dirty() {
        let (mut m, c) = setup();
        let r = walk(&mut m, c, 0x100000, Access::Read).unwrap();
        assert_eq!(r.gpa, 0x5000);
        assert!(!r.dirty);
        assert_eq!(m.0[1792], 0x5027);
        assert!(walk(&mut m, c, 0x100000, Access::Write).unwrap().dirty);
        assert_eq!(m.0[1792], 0x5067);
    }
    #[test]
    fn parent_permissions_and_wp() {
        let (mut m, mut c) = setup();
        m.0[512] &= !2;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Write),
            Err(Fault::Page {
                address: 0x100000,
                error: 3
            })
        );
        c.wp = 0;
        assert!(walk(&mut m, c, 0x100000, Access::Write).is_ok());
        c.cpl = 3;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Write),
            Err(Fault::Page {
                address: 0x100000,
                error: 7
            })
        );
    }
    #[test]
    fn reserved_nx_smep_and_nonpresent() {
        let (mut m, mut c) = setup();
        m.0[1792] |= 1 << 40;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Read),
            Err(Fault::Page {
                address: 0x100000,
                error: 9
            })
        );
        m.0[1792] &= !1;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Read),
            Err(Fault::Page {
                address: 0x100000,
                error: 0
            })
        );
        m.0[1792] = 0x5007 | (1 << 63);
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Fetch),
            Err(Fault::Page {
                address: 0x100000,
                error: 17
            })
        );
        c.nxe = 0;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Read),
            Err(Fault::Page {
                address: 0x100000,
                error: 9
            })
        );
        m.0[1792] = 0x5007;
        c.smep = 1;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Fetch),
            Err(Fault::Page {
                address: 0x100000,
                error: 17
            })
        );
    }
    #[test]
    fn large_leaf_and_route_are_not_guest_faults() {
        let (mut m, c) = setup();
        m.0[1024] = 0x200087;
        let t = walk(&mut m, c, 0x101234, Access::Read).unwrap();
        assert_eq!((t.gpa, t.leaf_bytes), (0x301234, 0x200000));
        m.0[1024] |= 0x2000;
        assert!(matches!(
            walk(&mut m, c, 0x101234, Access::Read),
            Err(Fault::Page { error: 9, .. })
        ));
        let (mut m, c) = setup();
        m.0[0] = 0x9007;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Read),
            Err(Fault::Unsupported)
        );
        assert_eq!(
            walk(&mut m, c, 0x800000000000, Access::Read),
            Err(Fault::Noncanonical)
        );
    }
}
