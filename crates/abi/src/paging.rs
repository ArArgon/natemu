//! Bounded four-level x86 translation for the admitted 48/40-bit profile.
//! The caller owns page-table mutation and physical-route resolution.
pub const PA_MASK: u64 = 0x0000_00ff_ffff_f000;
pub const ACCESSED: u64 = 1 << 5;
pub const DIRTY: u64 = 1 << 6;

pub fn canonical(address: u64) -> bool {
    address >> 47 == 0 || address >> 47 == 0x1ffff
}

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
    /// The walk lost `WALK_ATTEMPTS` A/D races in a row: no translation this time; the access re-walks later.
    Contended,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Translation {
    pub gpa: u64,
    pub leaf_bytes: u64,
    pub writable: bool,
    pub user: bool,
    pub executable: bool,
    pub dirty: bool,
    /// The leaf's PAT index, 4*PAT + 2*PCD + PWT (SDM Vol.3 5.9.2, Table 14-11 "Selection of PAT Entries"): PAT is bit 7 of a
    /// PTE and bit 12 of a 2 MiB PDE. Non-leaf PWT/PCD (and CR3's) select the type of the next table's read only; the walker
    /// reads tables from coherent RAM, so they change nothing here ([`crate::cpu::memory_type`] gives the page's type).
    pub pat: u8,
    /// The leaf's G bit (a global translation only with CR4.PGE, which the caller knows).
    pub global: bool,
}
/// Outcome of replacing a table entry the walker has just read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Update {
    /// The entry now holds the new value (possibly written by another agent).
    Written,
    /// Another agent changed the entry since it was read; the walk restarts.
    Changed,
    /// The memory refused the update (bounds or instrumentation).
    Refused,
}
pub trait Tables {
    fn read(&mut self, gpa: u64) -> Option<u64>;
    /// Replace `old`, the value just read at `gpa`, with `new`.
    fn write(&mut self, gpa: u64, old: u64, new: u64) -> Update;
}

/// x86 re-walks after losing an A/D race without bound; one walk gives up after this
/// many with `Fault::Contended` (the caller retries later) instead of spinning.
pub const WALK_ATTEMPTS: usize = 8;

/// Why one walk attempt stopped.
pub(crate) enum Stop {
    Fault(Fault),
    Restart,
}
impl From<Fault> for Stop {
    fn from(f: Fault) -> Self {
        Self::Fault(f)
    }
}
pub(crate) fn applied(update: Update) -> Result<(), Stop> {
    match update {
        Update::Written => Ok(()),
        Update::Changed => Err(Stop::Restart),
        Update::Refused => Err(Fault::Unsupported.into()),
    }
}
/// Repeat `attempt` while it restarts, at most `WALK_ATTEMPTS` times.
pub(crate) fn retrying<R>(mut attempt: impl FnMut() -> Result<R, Stop>) -> Result<R, Fault> {
    for _ in 0..WALK_ATTEMPTS {
        match attempt() {
            Ok(r) => return Ok(r),
            Err(Stop::Fault(f)) => return Err(f),
            Err(Stop::Restart) => {}
        }
    }
    Err(Fault::Contended)
}

pub fn walk<T: Tables>(
    memory: &mut T,
    c: Control,
    va: u64,
    access: Access,
) -> Result<Translation, Fault> {
    retrying(|| walk_once(memory, c, va, access))
}

fn walk_once<T: Tables>(
    memory: &mut T,
    c: Control,
    va: u64,
    access: Access,
) -> Result<Translation, Stop> {
    if !matches!(c.cpl, 0 | 3) || c.wp > 1 || c.nxe > 1 || c.smep > 1 || c.cr3 & !PA_MASK != 0 {
        return Err(Fault::Unsupported.into());
    }
    if !canonical(va) {
        return Err(Fault::Noncanonical.into());
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
    let (mut table, mut rw, mut user, mut executable) = (c.cr3, true, true, true);
    // Match the selected QEMU ordering: nonleaf A before descent, leaf A/D after permissions.
    for level in (1..=4).rev() {
        let at = table + ((va >> (12 + 9 * (level - 1))) & 511) * 8;
        let value = memory.read(at).ok_or(Fault::Unsupported)?;
        if value & 1 == 0 {
            return Err(pf(0).into());
        }
        let large = level == 2 && value & 128 != 0;
        if value & 0x000f_ff00_0000_0000 != 0
            || (c.nxe == 0 && value >> 63 != 0)
            || (level >= 3 && value & 128 != 0)
            || (large && value & 0x1fe000 != 0)
        {
            return Err(pf(9).into());
        }
        rw &= value & 2 != 0;
        user &= value & 4 != 0;
        executable &= value >> 63 == 0;

        if level == 1 || large {
            if (c.cpl == 3 && !user)
                || (access == Access::Write && !rw && (c.cpl == 3 || c.wp != 0))
                || (access == Access::Fetch && (!executable || (c.cpl == 0 && c.smep != 0 && user)))
            {
                return Err(pf(1).into());
            }
            let next = value | ACCESSED | if access == Access::Write { DIRTY } else { 0 };
            if next != value {
                applied(memory.write(at, value, next))?;
            }
            let size = if large { 0x20_0000 } else { 4096 };
            return Ok(Translation {
                gpa: (value & PA_MASK & !(size - 1)) | (va & (size - 1)),
                leaf_bytes: size,
                writable: rw || (c.cpl == 0 && c.wp == 0),
                user,
                executable,
                dirty: value & DIRTY != 0 || access == Access::Write,
                pat: ((if large { value >> 12 } else { value >> 7 } & 1) << 2 | value >> 3 & 3) as u8,
                global: value & 256 != 0,
            });
        }
        if value & ACCESSED == 0 {
            applied(memory.write(at, value, value | ACCESSED))?;
        }
        table = value & PA_MASK;
    }
    Err(Fault::Unsupported.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Ram([u64; 2048]);
    impl Tables for Ram {
        fn read(&mut self, p: u64) -> Option<u64> {
            self.0.get((p / 8) as usize).copied()
        }
        fn write(&mut self, p: u64, _: u64, v: u64) -> Update {
            if let Some(x) = self.0.get_mut((p / 8) as usize) {
                *x = v;
                Update::Written
            } else {
                Update::Refused
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
    /// SMEP (SDM Vol.3 4.6.1): a supervisor-mode fetch faults (#PF P | I/D, I/D also without NXE, 4.7) from a linear
    /// address whose user-mode access is allowed, i.e. U/S = 1 at every level of the walk, 4 KiB or 2 MiB leaf; one
    /// supervisor entry at any level lets it run. Data accesses and CPL 3 fetches are not affected; SMEP off: no fault.
    #[test]
    fn smep_needs_user_at_every_level() {
        for large in [false, true] {
            let (mut m, mut c) = setup();
            if large {
                m.0[1024] = 0x20_0000 | 0x87; // the PDE of 0x100000: a 2 MiB leaf at 0x200000
            }
            let levels: &[usize] = if large { &[0, 512, 1024] } else { &[0, 512, 1024, 1792] };
            let fault = Err(Fault::Page { address: 0x100000, error: 0x11 });
            for (smep, nxe) in [(0, 1), (1, 1), (1, 0)] {
                (c.smep, c.nxe) = (smep, nxe);
                assert_eq!(walk(&mut m, c, 0x100000, Access::Fetch).is_ok(), smep == 0, "large {large} smep {smep}");
                if smep != 0 {
                    assert_eq!(walk(&mut m, c, 0x100000, Access::Fetch), fault);
                }
                assert!(walk(&mut m, c, 0x100000, Access::Read).is_ok() && walk(&mut m, c, 0x100000, Access::Write).is_ok());
                for &at in levels {
                    m.0[at] &= !4;
                    assert!(walk(&mut m, c, 0x100000, Access::Fetch).is_ok(), "large {large} U/S clear at {at}");
                    m.0[at] |= 4;
                }
                c.cpl = 3;
                assert!(walk(&mut m, c, 0x100000, Access::Fetch).is_ok());
                c.cpl = 0;
            }
        }
    }
    /// M2: SDM Vol.3 5.9.2 and Table 14-11: the page's PAT index is 4*PAT + 2*PCD + PWT of the entry that maps it (PAT: bit 7 of
    /// a PTE, bit 12 of a 2 MiB PDE); the PWT/PCD of the entries above select only the type of the next table's read.
    #[test]
    fn pat_index_comes_from_the_leaf_alone() {
        for upper in [0, 0x8, 0x10, 0x18] {
            for i in 0..8u64 {
                let (mut m, c) = setup();
                for at in [0, 512, 1024] {
                    m.0[at] |= upper;
                }
                m.0[1792] |= (i & 3) << 3 | (i >> 2) << 7;
                let t = walk(&mut m, c, 0x100000, Access::Read).unwrap();
                assert_eq!((t.gpa, t.pat), (0x5000, i as u8));
                let (mut m, c) = setup();
                for at in [0, 512] {
                    m.0[at] |= upper;
                }
                m.0[1024] = 0x200087 | (i & 3) << 3 | (i >> 2) << 12;
                let t = walk(&mut m, c, 0x101234, Access::Write).unwrap();
                assert_eq!((t.gpa, t.leaf_bytes, t.pat), (0x301234, 0x200000, i as u8));
            }
        }
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
