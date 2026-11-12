//! Split-ring queue registers and checked traversal of owned metadata copies; no parser can
//! reach the VM's mapping.
use super::Identity;
use crate::Result;
use crate::execution::io::Transfer;
use natemu_abi::guest_memory::GuestRam;

/// VIRTQ_DESC_F_NEXT and VIRTQ_DESC_F_WRITE: the only descriptor flags with no feature to
/// negotiate (indirect descriptors are not offered).
const NEXT: u16 = 1;
const WRITE: u16 = 2;
const PLAIN_FLAGS: u16 = NEXT | WRITE;

/// One split-ring descriptor (virtio 1.3 §2.7.5), decoded from an owned table copy.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct Descriptor {
    addr: u64,
    len: u32,
    flags: u16,
    next: u16,
}
impl Descriptor {
    pub fn new(addr: u64, len: u32, flags: u16, next: u16) -> Self {
        Self {
            addr,
            len,
            flags,
            next,
        }
    }
    /// Descriptor `index` of a little-endian table copy.
    fn read(table: &[u8], index: u16) -> Self {
        let d = &table[usize::from(index) * 16..][..16];
        Self::new(
            u64::from_le_bytes(d[..8].try_into().unwrap()),
            u32::from_le_bytes(d[8..12].try_into().unwrap()),
            u16::from_le_bytes(d[12..14].try_into().unwrap()),
            u16::from_le_bytes(d[14..].try_into().unwrap()),
        )
    }
    pub fn addr(&self) -> u64 {
        self.addr
    }
    pub fn len(&self) -> u32 {
        self.len
    }
    pub fn flags(&self) -> u16 {
        self.flags
    }
    pub fn is_write_only(&self) -> bool {
        self.flags & WRITE != 0
    }
    pub fn has_next(&self) -> bool {
        self.flags & NEXT != 0
    }
}

/// One queue's registers and the device's progress through it.
#[derive(Clone, Copy, Default, Debug)]
pub(crate) struct Queue {
    pub size: u16,
    pub ready: bool,
    pub desc: u64,
    pub avail: u64,
    pub used: u64,
    pub next_avail: u16,
    pub next_used: u16,
}
impl Queue {
    /// Bytes of the descriptor table, available ring and used ring (virtio 1.3 §2.7).
    fn table_bytes(&self) -> u64 {
        u64::from(self.size) * 16
    }
    fn avail_bytes(&self) -> u64 {
        4 + u64::from(self.size) * 2
    }
    fn used_bytes(&self) -> u64 {
        4 + u64::from(self.size) * 8
    }
    /// A power-of-two size within `max`, aligned rings, and no ring wrapping the address space.
    pub fn valid(&self, max: u16) -> bool {
        self.size != 0
            && self.size <= max
            && self.size.is_power_of_two()
            && self.desc % 16 == 0
            && self.avail % 2 == 0
            && self.used % 4 == 0
            && self.desc.checked_add(self.table_bytes()).is_some()
            && self.avail.checked_add(self.avail_bytes()).is_some()
            && self.used.checked_add(self.used_bytes()).is_some()
    }
    /// Every ring lies wholly in installed guest RAM (checked only; nothing is dereferenced).
    pub fn rings_in(&self, ram: &GuestRam) -> bool {
        ram.range(self.used, self.used_bytes()).is_some()
            && ram.range(self.desc, self.table_bytes()).is_some()
            && ram.range(self.avail, self.avail_bytes()).is_some()
    }
}

/// Owned copies of a queue's descriptor table and available ring, and the incarnation they were
/// captured in. Chains are parsed only from these copies.
pub(crate) struct Snapshot {
    pub table: Vec<u8>,
    pub avail: Vec<u8>,
    captured: Option<Identity>,
}
impl Snapshot {
    pub fn new(max: u16) -> Self {
        Self {
            table: vec![0; max as usize * 16],
            avail: vec![0; 4 + max as usize * 2],
            captured: None,
        }
    }
    /// The captured available ring's index (the driver's next free slot).
    fn avail_index(&self) -> u16 {
        u16::from_le_bytes([self.avail[2], self.avail[3]])
    }
    /// Published, not-yet-used entries belong to the device. Reuse only the captured prefix in
    /// this queue generation; exhaustion must resnapshot even without another guest notification.
    pub(super) fn reusable(&self, q: Queue, id: Identity) -> bool {
        self.captured == Some(id) && self.avail_index() != q.next_avail
    }
    /// Reads of `q`'s available ring and descriptor table into this snapshot, for one batch:
    /// the ring's flags and index first (whole fields), then its entries, then the table. A
    /// windowless copy orders each read after the previous one (acquire), so every entry and
    /// descriptor the driver published before the index read is current.
    pub(super) fn reads(&mut self, q: Queue) -> [Transfer<'_>; 3] {
        let table_len = usize::from(q.size) * 16;
        let (header, ring) = self.avail[..4 + usize::from(q.size) * 2].split_at_mut(4);
        [
            Transfer::Read {
                gpa: q.avail,
                bytes: header,
            },
            Transfer::Read {
                gpa: q.avail + 4,
                bytes: ring,
            },
            Transfer::Read {
                gpa: q.desc,
                bytes: &mut self.table[..table_len],
            },
        ]
    }
    pub(super) fn captured(&mut self, id: Identity) {
        self.captured = Some(id);
    }
    pub(super) fn discard(&mut self) {
        self.captured = None;
    }
    /// Return an entire validated direct chain or an error; never an accepted prefix.
    pub fn chain(&self, q: Queue) -> Result<Option<(u16, Vec<Descriptor>)>> {
        let pending = self.avail_index().wrapping_sub(q.next_avail);
        if pending > q.size {
            return Err("virtio available index exceeds queue capacity".into());
        }
        if pending == 0 {
            return Ok(None);
        }
        let slot = 4 + usize::from(q.next_avail % q.size) * 2;
        let head = u16::from_le_bytes([self.avail[slot], self.avail[slot + 1]]);
        // The whole chain or an error: every index in the table, at most `size` descriptors (no
        // loop), only negotiated flags (no indirect), and a total length that fits in u32.
        let (mut chain, mut index, mut total) = (Vec::new(), head, 0u32);
        loop {
            if index >= q.size {
                return Err("virtio chain: descriptor index out of range".into());
            }
            if chain.len() == usize::from(q.size) {
                return Err("virtio chain: longer than the queue (a loop)".into());
            }
            let d = Descriptor::read(&self.table, index);
            if d.flags() & !PLAIN_FLAGS != 0 {
                return Err("virtio unnegotiated descriptor flags".into());
            }
            total = total
                .checked_add(d.len())
                .ok_or("virtio chain: total length overflows")?;
            chain.push(d);
            if !d.has_next() {
                break;
            }
            index = d.next;
        }
        Ok(Some((head, chain)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn put(s: &mut Snapshot, i: usize, addr: u64, len: u32, flags: u16, next: u16) {
        let d = &mut s.table[i * 16..i * 16 + 16];
        d[..8].copy_from_slice(&addr.to_le_bytes());
        d[8..12].copy_from_slice(&len.to_le_bytes());
        d[12..14].copy_from_slice(&flags.to_le_bytes());
        d[14..].copy_from_slice(&next.to_le_bytes());
    }
    #[test]
    fn malformed_suffix_and_indirect_never_become_partial_requests() {
        let mut s = Snapshot::new(8);
        let q = Queue {
            size: 8,
            ..Default::default()
        };
        s.avail[2] = 1;
        put(&mut s, 0, 0x1000, 4, 3, 8);
        assert!(s.chain(q).is_err());
        put(&mut s, 0, 0x1000, 4, 7, 0);
        assert!(s.chain(q).is_err());
        put(&mut s, 0, 0x1000, 4, 3, 0);
        assert!(s.chain(q).is_err());
        put(&mut s, 0, 0x1000, 4, 2, 0);
        assert_eq!(s.chain(q).unwrap().unwrap().1.len(), 1);
    }
    #[test]
    fn loops_bad_heads_and_overlong_totals_are_refused() {
        let mut s = Snapshot::new(8);
        let q = Queue {
            size: 8,
            ..Default::default()
        };
        let refused = |s: &Snapshot, why: &str| {
            let e = s.chain(q).unwrap_err().to_string();
            assert!(e.contains(why), "{e:?} lacks {why:?}");
        };
        s.avail[2] = 1;
        // A two-descriptor loop: refused once the walk exceeds the queue size.
        put(&mut s, 0, 0x1000, 4, 1, 1);
        put(&mut s, 1, 0x2000, 4, 1, 0);
        refused(&s, "loop");
        // A head outside the queue.
        s.avail[4] = 8;
        refused(&s, "out of range");
        // Lengths summing past u32.
        s.avail[4] = 0;
        put(&mut s, 0, 0x1000, u32::MAX, 1, 1);
        put(&mut s, 1, 0x2000, 1, 2, 0);
        refused(&s, "overflows");
        // An unknown, non-indirect flag bit.
        put(&mut s, 0, 0x1000, 4, 8, 0);
        refused(&s, "unnegotiated");
        // A full-length legal chain (8 descriptors) is accepted, readable then writable.
        for i in 0..8u16 {
            let last = i == 7;
            put(
                &mut s,
                i as usize,
                0x1000 * (u64::from(i) + 1),
                4,
                if last { 2 } else { 1 },
                i + 1,
            );
        }
        let (head, chain) = s.chain(q).unwrap().unwrap();
        assert_eq!((head, chain.len()), (0, 8));
        assert!(chain[7].is_write_only() && !chain[0].is_write_only());
    }
    #[test]
    fn index_wrap_and_capacity_are_checked_before_head() {
        let mut s = Snapshot::new(8);
        let q = Queue {
            size: 8,
            next_avail: u16::MAX,
            ..Default::default()
        };
        put(&mut s, 0, 0x1000, 4, 2, 0);
        assert!(s.chain(q).unwrap().is_some());
        s.avail[2] = 8;
        assert!(s.chain(q).is_err());
    }

    #[test]
    fn captured_prefix_survives_live_alias_writes_but_exhaustion_requires_new_capture() {
        let mut s = Snapshot::new(8);
        let id = Identity {
            queue: 0,
            device: 1,
            generation: 1,
        };
        let mut q = Queue {
            size: 8,
            ..Default::default()
        };
        s.avail[2] = 2;
        s.avail[6] = 1;
        put(&mut s, 0, 0x1000, 4, 2, 0);
        put(&mut s, 1, 0x2000, 4, 2, 0);
        s.captured(id);
        assert_eq!(s.chain(q).unwrap().unwrap().1[0].addr(), 0x1000);
        // A completion or hostile writer can alias live queue RAM. It does not change this
        // already-owned metadata snapshot, and does not constitute admission of the next request.
        let mut live_table = s.table.clone();
        live_table[16..24].copy_from_slice(&0x3000u64.to_le_bytes());
        q.next_avail = 1;
        assert!(s.reusable(q, id));
        assert_eq!(s.chain(q).unwrap().unwrap().1[0].addr(), 0x2000);
        q.next_avail = 2;
        assert!(!s.reusable(q, id));
        // Legal reuse and a new publication appear on the next capture, with no new kick required.
        s.table = live_table;
        s.avail[2] = 3;
        s.avail[8] = 1;
        s.captured(id);
        assert_eq!(s.chain(q).unwrap().unwrap().1[0].addr(), 0x3000);
        for changed in [
            Identity { queue: 1, ..id },
            Identity { device: 2, ..id },
            Identity {
                generation: 2,
                ..id
            },
        ] {
            assert!(!s.reusable(q, changed));
        }
        s.discard();
        assert!(!s.reusable(q, id));
    }

    #[test]
    fn malformed_later_head_is_not_admitted_by_capture() {
        let mut s = Snapshot::new(8);
        let id = Identity {
            queue: 0,
            device: 1,
            generation: 1,
        };
        let mut q = Queue {
            size: 8,
            ..Default::default()
        };
        s.avail[2] = 2;
        s.avail[6] = 1;
        put(&mut s, 0, 0x1000, 4, 2, 0);
        put(&mut s, 1, 0x2000, 4, 3, 8);
        s.captured(id);
        assert!(s.chain(q).unwrap().is_some());
        q.next_avail = 1;
        assert!(s.reusable(q, id));
        assert!(s.chain(q).is_err());
        q.next_avail = u16::MAX;
        s.avail[2..4].copy_from_slice(&0u16.to_le_bytes());
        assert!(s.reusable(q, id));
        q.next_avail = 0;
        assert!(!s.reusable(q, id));
    }
}
