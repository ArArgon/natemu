//! Hidden legacy-PAE roots are semantic state, independent of native shadows.
use crate::{
    controls::Error,
    paging::{Access, Control, Fault, PA_MASK, Stop, Tables, Translation, applied, retrying},
};

pub const OFFSET: usize = 0x24000;
pub const TABLE_OFFSET: usize = 0x75000;
pub const TABLE_BYTES: usize = 5 * 4096;
pub const ROOT: u64 = 0x1020;

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Hidden {
    pub entries: [u64; 4],
    pub generation: u64,
}
impl Hidden {
    pub const ZERO: Self = Self {
        entries: [0; 4],
        generation: 0,
    };
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Visit {
    pub before: Hidden,
    pub after: Hidden,
    pub load_addresses: [u64; 4],
    pub attempts_before: u64,
    pub attempts_after: u64,
    pub reads_before: u64,
    pub reads_after: u64,
}
#[repr(C)]
#[derive(Clone, Copy)]
pub struct State {
    pub hidden: Hidden,
    pub attempts: u64,
    pub reads: u64,
    pub walk_reads: u64,
    pub walk_writes: u64,
    pub invlpg: u64,
    pub evictions: u64,
    pub mutations: u64,
    pub handled_faults: u64,
    pub visits: [Visit; crate::controls::MAX_VISITS],
}
impl State {
    pub const ZERO: Self = unsafe { core::mem::zeroed() };
}
const _: () = assert!(core::mem::size_of::<State>() <= 4096);

pub fn active(controls: &[u64; 8]) -> bool {
    controls[0] & (1 << 31) != 0 && controls[3] & 32 != 0 && controls[4] & (1 << 8) == 0
}
pub fn reload(before: &[u64; 8], after: &[u64; 8], cr: usize) -> bool {
    active(after)
        && (cr == 3
            || (cr == 0 && (before[0] ^ after[0]) & 0xe0000000 != 0)
            || (cr == 4 && (before[3] ^ after[3]) & 0x1000b0 != 0))
}

/// Read every candidate first; a rejected load cannot partially replace Hidden.
pub fn load<T: Tables>(tables: &mut T, cr3: u64) -> Result<[u64; 4], Error> {
    if cr3 > u32::MAX as u64 {
        return Err(Error::Unsupported);
    }
    let base = cr3 & 0xffff_ffe0;
    let mut entries = [0; 4];
    for (i, entry) in entries.iter_mut().enumerate() {
        *entry = tables.read(base + i as u64 * 8).ok_or(Error::Unsupported)?;
    }
    // Selected Intel present-only validation, with the exposed 40-bit PA width.
    if entries
        .iter()
        .any(|e| e & 1 != 0 && e & !(PA_MASK | 0xe19) != 0)
    {
        return Err(Error::GeneralProtection);
    }
    Ok(entries)
}

pub fn walk<T: Tables>(
    tables: &mut T,
    hidden: &Hidden,
    c: Control,
    address: u64,
    access: Access,
) -> Result<Translation, Fault> {
    retrying(|| walk_once(tables, hidden, c, address, access))
}

fn walk_once<T: Tables>(
    tables: &mut T,
    hidden: &Hidden,
    c: Control,
    address: u64,
    access: Access,
) -> Result<Translation, Stop> {
    if hidden.generation == 0
        || address > u32::MAX as u64
        || !matches!(c.cpl, 0 | 3)
        || c.wp > 1
        || c.nxe > 1
        || c.smep > 1
    {
        return Err(Fault::Unsupported.into());
    }
    let error = u64::from(access == Access::Write) * 2
        | u64::from(c.cpl == 3) * 4
        | u64::from(access == Access::Fetch && (c.nxe != 0 || c.smep != 0)) * 16;
    let pf = |extra| Fault::Page {
        address,
        error: error | extra,
    };
    let root = hidden.entries[(address >> 30) as usize];
    if root & 1 == 0 {
        return Err(pf(0).into());
    }
    let (mut base, mut rw, mut user, mut executable, mut wb) =
        (root & PA_MASK, true, true, true, root & 0x18 == 0);
    for level in (1..=2).rev() {
        let at = base + ((address >> (12 + 9 * (level - 1))) & 511) * 8;
        let entry = tables.read(at).ok_or(Fault::Unsupported)?;
        if entry & 1 == 0 {
            return Err(pf(0).into());
        }
        let large = level == 2 && entry & 128 != 0;
        if entry & 0x7fff_ff00_0000_0000 != 0
            || (c.nxe == 0 && entry >> 63 != 0)
            || (large && entry & 0x1fe000 != 0)
        {
            return Err(pf(9).into());
        }
        rw &= entry & 2 != 0;
        user &= entry & 4 != 0;
        executable &= entry >> 63 == 0;
        wb &=
            entry & 0x18 == 0 && !(level == 1 && entry & 128 != 0) && !(large && entry & 4096 != 0);
        if level == 1 || large {
            if (c.cpl == 3 && !user)
                || (access == Access::Write && !rw && (c.cpl == 3 || c.wp != 0))
                || (access == Access::Fetch && (!executable || (c.cpl == 0 && c.smep != 0 && user)))
            {
                return Err(pf(1).into());
            }
            let next = entry | 32 | if access == Access::Write { 64 } else { 0 };
            if next != entry {
                applied(tables.write(at, entry, next))?;
            }
            let bytes = if large { 1 << 21 } else { 4096 };
            return Ok(Translation {
                gpa: (entry & PA_MASK & !(bytes - 1)) | (address & (bytes - 1)),
                leaf_bytes: bytes,
                writable: rw || (c.cpl == 0 && c.wp == 0),
                user,
                executable,
                dirty: entry & 64 != 0 || access == Access::Write,
                wb,
            });
        }
        if entry & 32 == 0 {
            applied(tables.write(at, entry, entry | 32))?;
        }
        base = entry & PA_MASK;
    }
    Err(Fault::Unsupported.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Memory {
        words: [u64; 3072],
        reads: usize,
    }
    impl Tables for Memory {
        fn read(&mut self, at: u64) -> Option<u64> {
            self.reads += 1;
            self.words.get(at as usize / 8).copied()
        }
        fn write(&mut self, at: u64, _: u64, value: u64) -> crate::paging::Update {
            if let Some(word) = self.words.get_mut(at as usize / 8) {
                *word = value;
                crate::paging::Update::Written
            } else {
                crate::paging::Update::Refused
            }
        }
    }
    fn fixture() -> (Memory, Hidden, Control) {
        let mut m = Memory {
            words: [0; 3072],
            reads: 0,
        };
        m.words[ROOT as usize / 8] = 0x2001;
        m.words[0x2000 / 8] = 0x3007;
        m.words[0x3000 / 8 + 256] = 0x100007;
        let entries = load(&mut m, ROOT | 0x1b).unwrap();
        (
            m,
            Hidden {
                entries,
                generation: 1,
            },
            Control {
                cr3: ROOT,
                cpl: 0,
                wp: 1,
                nxe: 0,
                smep: 0,
            },
        )
    }
    #[test]
    fn shadow_misses_use_hidden_roots_and_failed_candidate_is_not_published() {
        let (mut m, old, c) = fixture();
        assert_eq!(m.reads, 4);
        m.words[ROOT as usize / 8] = 0x4001;
        m.words[ROOT as usize / 8 + 3] = 0x21;
        assert_eq!(load(&mut m, ROOT), Err(Error::GeneralProtection));
        assert_eq!(m.reads, 8);
        let start = m.reads;
        let t = walk(&mut m, &old, c, 0x100000, Access::Write).unwrap();
        assert_eq!(t.gpa, 0x100000);
        assert_eq!(m.reads - start, 2);
        assert_eq!(old.entries, [0x2001, 0, 0, 0]);
        assert_eq!(m.words[ROOT as usize / 8], 0x4001);
        assert_eq!(m.words[0x2000 / 8], 0x3027);
        assert_eq!(m.words[0x3000 / 8 + 256], 0x100067);
        m.words[ROOT as usize / 8 + 3] = 0;
        assert_eq!(load(&mut m, ROOT).unwrap()[0], 0x4001);
    }
    #[test]
    fn nonpresent_root_does_not_walk_and_reserved_leaf_faults_before_ad_update() {
        let (mut m, mut h, c) = fixture();
        h.entries[1] = u64::MAX - 1;
        let count = m.reads;
        assert_eq!(
            walk(&mut m, &h, c, 0x40000000, Access::Read),
            Err(Fault::Page {
                address: 0x40000000,
                error: 0
            })
        );
        assert_eq!(m.reads, count);
        m.words[0x3000 / 8 + 256] |= 1 << 52;
        assert_eq!(
            walk(&mut m, &h, c, 0x100000, Access::Fetch),
            Err(Fault::Page {
                address: 0x100000,
                error: 9
            })
        );
        assert_eq!(m.words[0x3000 / 8 + 256] & 32, 0);
        m.words[ROOT as usize / 8] = u64::MAX - 1;
        assert_eq!(load(&mut m, ROOT).unwrap()[0], u64::MAX - 1);
    }
    #[test]
    fn reload_triggers_separate_wp_shadow_and_pge_changes() {
        let mut a = [0; 8];
        a[0] = 0x80010011;
        a[2] = ROOT;
        a[3] = 0x620;
        assert!(reload(&a, &a, 3));
        assert!(!reload(&a, &a, 0));
        let mut b = a;
        b[0] ^= 1 << 16;
        assert!(!reload(&a, &b, 0));
        b = a;
        b[3] ^= 128;
        assert!(reload(&a, &b, 4));
        b[0] &= !(1 << 31);
        assert!(!reload(&a, &b, 4));
    }

    #[test]
    fn large_page_alignment_nx_and_supervisor_write_protection() {
        let (mut m, h, mut c) = fixture();
        m.words[0x2000 / 8] = 0x200085; // 2-MiB user/read-only leaf.
        assert_eq!(
            walk(&mut m, &h, c, 0x1234, Access::Write),
            Err(Fault::Page {
                address: 0x1234,
                error: 3
            })
        );
        assert_eq!(m.words[0x2000 / 8] & 0x60, 0);
        c.wp = 0;
        let t = walk(&mut m, &h, c, 0x1234, Access::Write).unwrap();
        assert_eq!((t.gpa, t.leaf_bytes, t.dirty), (0x201234, 1 << 21, true));
        c.nxe = 1;
        m.words[0x2000 / 8] |= 1 << 63;
        assert_eq!(
            walk(&mut m, &h, c, 0x1234, Access::Fetch),
            Err(Fault::Page {
                address: 0x1234,
                error: 17
            })
        );
        m.words[0x2000 / 8] = 0x202087;
        assert_eq!(
            walk(&mut m, &h, c, 0x1234, Access::Read),
            Err(Fault::Page {
                address: 0x1234,
                error: 9
            })
        );
    }
}
