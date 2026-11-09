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
    Page {
        address: u64,
        error: u64,
    },
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

/// Arm SB (FEAT_SB; the host requires it when the barriers are on, `natemu::execution::Boot::prepare`): no later
/// instruction executes, even speculatively, until every earlier one (the checks' branches among them) is no longer
/// speculative. The resident only; host builds (unit tests) emit nothing.
#[inline(always)]
pub fn speculation_barrier() {
    #[cfg(all(target_arch = "aarch64", target_os = "none"))]
    // SAFETY: a barrier; no other effect. `.inst`: SB's encoding (the assembler's default features lack FEAT_SB).
    unsafe {
        core::arch::asm!(".inst 0xd50330ff", options(nostack, preserves_flags))
    };
}

/// Bits that must be clear in a non-leaf page-table entry for [`descend_base`] to keep its frame: the reserved bits above
/// MAXPHYADDR (51:40) and PS, bit 7 (PCID plan P1, review B1). PS is 0 in every architectural level-4/3/2-non-large
/// descent (only levels 2 and 1 define it, and a level-2 PS = 1 is a 2 MiB leaf taken through the leaf branch, not here),
/// so masking it never changes the real path; it stops a speculative read that mispredicted the `level == 1 || large`
/// branch from using a 2 MiB leaf's frame field (kernel data) as the next table and branching on what it reads there.
const DESCEND_RESERVED: u64 = 0x000f_ff00_0000_0080;

/// The next-level table base a non-leaf entry gives (PCID plan P1, the RDCL_NO audit): `value & PA_MASK` when the entry
/// is present (bit 0) with [`DESCEND_RESERVED`] (51:40 and PS) clear, else 0, produced by a conditional select and
/// followed by a CSDB. A speculative path that mispredicted this level's present/reserved/PS check can then only make the
/// next table read (and the current level's A-bit RMW, whose slot this gates) use guest-physical 0, never a frame field
/// taken from a not-present, reserved or leaf entry (Arm's data-dependent masking barrier, as Linux's
/// `array_index_nospec`; cheaper than SB, one per level). The nxe reserved term keeps its architectural #PF above and
/// points only at a page table, so it needs no mask. The host and resident forms share [`DESCEND_RESERVED`] and the same
/// select; the asm keeps the compiler from folding the condition away.
#[inline(always)]
fn descend_base(value: u64) -> u64 {
    #[cfg(all(target_arch = "aarch64", target_os = "none"))]
    {
        let base: u64;
        // SAFETY: arithmetic and a CSDB speculation barrier; reads only its inputs, no memory, clobbers flags.
        unsafe {
            core::arch::asm!(
                "and {base}, {value}, {pa}",     // candidate next base
                "tst {value}, {rsv}",            // Z = 1 iff the reserved (51:40) and PS (7) bits are clear
                "cset {ok}, eq",                 // ok = reserved-and-PS-clear
                "and {ok}, {ok}, {value}",       // bit 0 = (reserved-and-PS-clear) AND present
                "tst {ok}, #1",
                "csel {base}, {base}, xzr, ne",  // base unless not (present and reserved-and-PS-clear)
                "csdb",
                value = in(reg) value,
                pa = in(reg) PA_MASK,
                rsv = in(reg) DESCEND_RESERVED,
                ok = out(reg) _,
                base = out(reg) base,
                options(nostack),
            );
        }
        base
    }
    #[cfg(not(all(target_arch = "aarch64", target_os = "none")))]
    {
        // The same select, for host unit tests (`descend_base_masks_leaf_and_absent`); shares DESCEND_RESERVED.
        if value & 1 != 0 && value & DESCEND_RESERVED == 0 {
            value & PA_MASK
        } else {
            0
        }
    }
}

/// RDCL_NO (PCID plan P1, the resident's speculation audit): the walk is the one place that turns a guest linear address
/// into a guest-physical one through its own loads, so with the barriers on (`B`) no load the walk itself issues through
/// the guest-RAM alias runs speculatively before the paging check that permits it has resolved: each non-leaf level masks
/// the next table's base after its present/reserved/PS check (`descend_base`: a conditional select to 0 plus CSDB), and
/// the leaf's paging permission check (present, U/S, R/W, XD/SMEP) ends in an Arm SB before the A/D write and the return.
/// Scope: this covers the walk's own descent and A/D loads. A caller reading `Translation::gpa` from an `Err` result under
/// a mispredicted `Ok`/`Err` branch, and the non-paging limit checks in `segments`/`ports` (Spectre-v1-shaped, older than
/// P1), are outside RDCL's paging definition and are recorded as a correctness-pass gap (plan 2.4; boot-handoff 6a).
/// `barriers` (PCID plan P1, owner November 5: off by default; the resident passes its `Control`'s flag, set by
/// the host at VM start) chooses the `B = true` or `B = false` monomorphisation once per walk (not per level), so the two
/// come from one source and cannot drift. `walk_once` is not inlined, so `tools/check_resident_barriers.py` can find each
/// `B = true` instance in the resident and check it (and that the `B = false` ones carry no barrier). RDCL_NO is
/// advertised only when `barriers` is on (and the host has FEAT_CSV3): the host couples the two, so a guest that reads
/// "not susceptible to Meltdown" is always running the barriers.
pub fn walk<T: Tables>(
    memory: &mut T,
    c: Control,
    va: u64,
    access: Access,
    barriers: bool,
) -> Result<Translation, Fault> {
    if barriers {
        retrying(|| walk_once::<T, true>(memory, c, va, access))
    } else {
        retrying(|| walk_once::<T, false>(memory, c, va, access))
    }
}

#[inline(never)]
fn walk_once<T: Tables, const B: bool>(
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
            // The leaf's paging permission check (present, U/S, R/W, XD/SMEP) ends in an Arm SB before the A/D write and
            // the returned frame: no alias load issues speculatively past it (PCID plan P1; the non-leaf levels use the
            // cheaper `descend_base` mask, the leaf a full SB since its frame is the one the caller then loads from).
            if B {
                speculation_barrier();
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
                pat: ((if large { value >> 12 } else { value >> 7 } & 1) << 2 | value >> 3 & 3)
                    as u8,
                global: value & 256 != 0,
            });
        }
        // The mask (and its CSDB) come before the A-bit RMW below: that RMW reads this level's own slot (`at`, from the
        // previous level's already-masked base), and the mask gates the next level's read of `table`. `next` equals
        // `value & PA_MASK` on the architectural path (present, reserved and PS all checked above). Off: no barrier.
        let next = if B {
            descend_base(value)
        } else {
            value & PA_MASK
        };
        if value & ACCESSED == 0 {
            applied(memory.write(at, value, value | ACCESSED))?;
        }
        table = next;
    }
    Err(Fault::Unsupported.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// PCID plan P1 (review B1, item 5): the non-leaf mask keeps the frame only for a present entry with the reserved
    /// (51:40) and PS (bit 7) bits clear, else yields 0. The host fallback is the same select as the resident's asm.
    #[test]
    fn descend_base_masks_leaf_absent_and_reserved() {
        let frame = 0x12_3000;
        assert_eq!(descend_base(frame | 0x7), frame); // present, writable, user: kept
        assert_eq!(descend_base(frame | 0x6), 0); // not present (bit 0 clear): masked
        assert_eq!(
            descend_base(frame | 0x87),
            0,
            "PS set (a 2 MiB leaf mispredicted as a descent): masked"
        );
        assert_eq!(
            descend_base(frame | 0x7 | 1 << 48),
            0,
            "a reserved bit (48) set: masked"
        );
        assert_eq!(
            descend_base(frame | 0x7 | 1 << 40),
            0,
            "a reserved bit (40) set: masked"
        );
        assert_eq!(DESCEND_RESERVED & 1 << 7, 1 << 7, "PS is in the mask (B1)");
        assert_eq!(
            DESCEND_RESERVED & 0x000f_ff00_0000_0000,
            0x000f_ff00_0000_0000,
            "51:40 in the mask"
        );
    }
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
        let r = walk(&mut m, c, 0x100000, Access::Read, true).unwrap();
        assert_eq!(r.gpa, 0x5000);
        assert!(!r.dirty);
        assert_eq!(m.0[1792], 0x5027);
        assert!(
            walk(&mut m, c, 0x100000, Access::Write, true)
                .unwrap()
                .dirty
        );
        assert_eq!(m.0[1792], 0x5067);
    }
    #[test]
    fn parent_permissions_and_wp() {
        let (mut m, mut c) = setup();
        m.0[512] &= !2;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Write, true),
            Err(Fault::Page {
                address: 0x100000,
                error: 3
            })
        );
        c.wp = 0;
        assert!(walk(&mut m, c, 0x100000, Access::Write, true).is_ok());
        c.cpl = 3;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Write, true),
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
            walk(&mut m, c, 0x100000, Access::Read, true),
            Err(Fault::Page {
                address: 0x100000,
                error: 9
            })
        );
        m.0[1792] &= !1;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Read, true),
            Err(Fault::Page {
                address: 0x100000,
                error: 0
            })
        );
        m.0[1792] = 0x5007 | (1 << 63);
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Fetch, true),
            Err(Fault::Page {
                address: 0x100000,
                error: 17
            })
        );
        c.nxe = 0;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Read, true),
            Err(Fault::Page {
                address: 0x100000,
                error: 9
            })
        );
        m.0[1792] = 0x5007;
        c.smep = 1;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Fetch, true),
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
            let levels: &[usize] = if large {
                &[0, 512, 1024]
            } else {
                &[0, 512, 1024, 1792]
            };
            let fault = Err(Fault::Page {
                address: 0x100000,
                error: 0x11,
            });
            for (smep, nxe) in [(0, 1), (1, 1), (1, 0)] {
                (c.smep, c.nxe) = (smep, nxe);
                assert_eq!(
                    walk(&mut m, c, 0x100000, Access::Fetch, true).is_ok(),
                    smep == 0,
                    "large {large} smep {smep}"
                );
                if smep != 0 {
                    assert_eq!(walk(&mut m, c, 0x100000, Access::Fetch, true), fault);
                }
                assert!(
                    walk(&mut m, c, 0x100000, Access::Read, true).is_ok()
                        && walk(&mut m, c, 0x100000, Access::Write, true).is_ok()
                );
                for &at in levels {
                    m.0[at] &= !4;
                    assert!(
                        walk(&mut m, c, 0x100000, Access::Fetch, true).is_ok(),
                        "large {large} U/S clear at {at}"
                    );
                    m.0[at] |= 4;
                }
                c.cpl = 3;
                assert!(walk(&mut m, c, 0x100000, Access::Fetch, true).is_ok());
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
                let t = walk(&mut m, c, 0x100000, Access::Read, true).unwrap();
                assert_eq!((t.gpa, t.pat), (0x5000, i as u8));
                let (mut m, c) = setup();
                for at in [0, 512] {
                    m.0[at] |= upper;
                }
                m.0[1024] = 0x200087 | (i & 3) << 3 | (i >> 2) << 12;
                let t = walk(&mut m, c, 0x101234, Access::Write, true).unwrap();
                assert_eq!((t.gpa, t.leaf_bytes, t.pat), (0x301234, 0x200000, i as u8));
            }
        }
    }
    #[test]
    fn large_leaf_and_route_are_not_guest_faults() {
        let (mut m, c) = setup();
        m.0[1024] = 0x200087;
        let t = walk(&mut m, c, 0x101234, Access::Read, true).unwrap();
        assert_eq!((t.gpa, t.leaf_bytes), (0x301234, 0x200000));
        m.0[1024] |= 0x2000;
        assert!(matches!(
            walk(&mut m, c, 0x101234, Access::Read, true),
            Err(Fault::Page { error: 9, .. })
        ));
        let (mut m, c) = setup();
        m.0[0] = 0x9007;
        assert_eq!(
            walk(&mut m, c, 0x100000, Access::Read, true),
            Err(Fault::Unsupported)
        );
        assert_eq!(
            walk(&mut m, c, 0x800000000000, Access::Read, true),
            Err(Fault::Noncanonical)
        );
    }
}
