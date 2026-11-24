extern crate std;
use super::*;
use crate::execution::RELOC_MOVE;
use std::{
    collections::HashMap,
    sync::{
        Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    thread, vec,
    vec::Vec,
};

struct Span {
    memory: Vec<AtomicU64>,
    layout: Layout,
}
impl Span {
    fn new(layout: Layout) -> Self {
        Self::with_guard(layout, 0)
    }
    /// With `guard` words after the span, set to a sentinel: a reader that runs past the ring's end reads them.
    fn with_guard(layout: Layout, guard: usize) -> Self {
        assert!(layout.valid());
        let words = layout.span_bytes() / 8;
        Self {
            memory: (0..words + guard)
                .map(|i| AtomicU64::new(if i < words { 0 } else { 0x6a6a_6a6a_6a6a_6a6a }))
                .collect(),
            layout,
        }
    }
    fn pool(&self) -> Pool {
        // SAFETY: the span is 8-aligned, as long as `layout` says, and outlives every use in these tests.
        unsafe { Pool::new(self.memory.as_ptr() as *mut u8, self.layout) }
    }
    fn writer(&self) -> Writer {
        // SAFETY: zero memory, one writer.
        unsafe { Writer::new(self.pool()) }
    }
}
const TINY: Layout = Layout {
    index_slots: 16,
    ring_bytes: 2048,
    control_bytes: 128,
};

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() >> 11) as usize % n
    }
    fn bytes(&mut self, n: usize) -> Vec<u8> {
        (0..n).map(|_| self.next() as u8).collect()
    }
}

/// An entry's parts, owned. The last eight code bytes hold a checksum of everything else (test builds only: the format has
/// none), so a reader's accepted entry can be told whole from torn or forged.
#[derive(Clone)]
struct Owned {
    rip: u64,
    context: u64,
    guest_bytes: u32,
    sti_first: bool,
    links: [LinkPoint; MAX_LINK_POINTS],
    dispatch_exit: u32,
    rep_exits: u32,
    code: Vec<u8>,
    sites: Vec<u8>,
    relocs: Vec<u32>,
    prefix: Vec<u8>,
}
fn fnv(h: &mut u64, bytes: &[u8]) {
    for &b in bytes {
        *h = (*h ^ u64::from(b)).wrapping_mul(0x100_0000_01b3);
    }
}
#[allow(clippy::too_many_arguments)]
fn checksum(
    rip: u64,
    context: u64,
    guest_bytes: u32,
    sti: bool,
    links: &[LinkPoint],
    dispatch: u32,
    rep: u32,
    code: &[u8],
    sites: &[u8],
    relocs: &[u8],
    prefix: &[u8],
) -> u64 {
    let mut h = 0xcbf2_9ce4_8422_2325;
    for v in [
        rip,
        context,
        u64::from(guest_bytes),
        u64::from(sti),
        u64::from(dispatch),
        u64::from(rep),
    ] {
        fnv(&mut h, &v.to_le_bytes());
    }
    for l in links {
        fnv(&mut h, &l.offset.to_le_bytes());
        fnv(&mut h, &l.word.to_le_bytes());
        fnv(&mut h, &l.target.to_le_bytes());
    }
    for part in [&code[..code.len() - 8], sites, relocs, prefix] {
        fnv(&mut h, &(part.len() as u64).to_le_bytes());
        fnv(&mut h, part);
    }
    h
}
impl Owned {
    fn parts(&self) -> Parts<'_> {
        Parts {
            rip: self.rip,
            context: self.context,
            guest_bytes: self.guest_bytes,
            sti_first: self.sti_first,
            links: self.links,
            dispatch_exit: self.dispatch_exit,
            rep_exits: self.rep_exits,
            exit_forms: 0,
            code: &self.code,
            sites: &self.sites,
            relocs: &self.relocs,
            prefix: &self.prefix,
        }
    }
    fn sum(&self) -> u64 {
        let relocs: Vec<u8> = self.relocs.iter().flat_map(|r| r.to_le_bytes()).collect();
        checksum(
            self.rip,
            self.context,
            self.guest_bytes,
            self.sti_first,
            &self.links,
            self.dispatch_exit,
            self.rep_exits,
            &self.code,
            &self.sites,
            &relocs,
            &self.prefix,
        )
    }
    fn seal(mut self) -> Self {
        let n = self.code.len();
        let s = self.sum();
        self.code[n - 8..].copy_from_slice(&s.to_le_bytes());
        self
    }
    fn bytes(&self) -> usize {
        self.parts().entry_bytes().unwrap()
    }
}
/// The checksum an accepted entry should carry, and the one it does.
fn sums(e: &Entry) -> (u64, u64) {
    let n = e.code.len();
    (
        checksum(
            e.rip,
            e.context,
            e.guest_bytes,
            e.sti_first,
            &e.links,
            e.dispatch_exit,
            e.rep_exits,
            e.code,
            e.sites,
            e.relocs,
            e.prefix,
        ),
        u64::from_le_bytes(e.code[n - 8..].try_into().unwrap()),
    )
}
/// A random valid entry for `key` (its context, RIP and four lead bytes), at most `max` bytes.
fn random(rng: &mut Rng, key: (u64, u64, [u8; 4]), max: usize) -> Owned {
    loop {
        let code_len = 8 * (2 + rng.below(40));
        let words = code_len / 4;
        let mut relocs: Vec<u32> = (0..rng.below(7))
            .map(|_| {
                let w = rng.below(words) as u32;
                if rng.below(2) == 0 {
                    w << 6
                } else {
                    w << 6 | RELOC_MOVE | rng.below(31) as u32
                }
            })
            .collect();
        relocs.sort();
        relocs.dedup_by_key(|r| *r >> 6);
        let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
        for l in links.iter_mut().take(rng.below(3)) {
            *l = LinkPoint {
                offset: 4 * (1 + rng.below(words - 1)) as u32,
                word: rng.next() as u32,
                target: rng.next(),
            };
        }
        let mut rep_exits = 0;
        for (i, l) in links.iter().enumerate() {
            rep_exits |= u32::from(l.offset != 0 && rng.below(2) == 0) << i;
        }
        let guest_bytes = 1 + rng.below(40) as u32;
        let far = if rng.below(2) == 0 { 300 } else { 1000 };
        let extra = rng.below(far);
        let mut prefix = rng.bytes(guest_bytes as usize + extra);
        let lead = 4.min(guest_bytes as usize);
        prefix[..lead].copy_from_slice(&key.2[..lead]);
        let site_count = rng.below(4);
        let code = rng.bytes(code_len);
        let sites = rng.bytes(SITE_BYTES * site_count);
        let o = Owned {
            rip: key.1,
            context: key.0,
            guest_bytes,
            sti_first: rng.below(2) == 0,
            links,
            dispatch_exit: if rng.below(2) == 0 {
                0
            } else {
                4 * rng.below(words) as u32
            },
            rep_exits,
            code,
            sites,
            relocs,
            prefix,
        }
        .seal();
        if o.bytes() <= max {
            return o;
        }
    }
}
fn key_of(o: &Owned) -> Key {
    o.parts().key()
}
/// An entry of exactly `n` bytes (192 or more, a multiple of 64) under `(context, rip)` with lead bytes `lead`.
fn sized(n: usize, context: u64, rip: u64, lead: [u8; 4]) -> Owned {
    let mut prefix = vec![0xa5; n - HEADER_BYTES - 8];
    prefix[..4].copy_from_slice(&lead);
    Owned {
        rip,
        context,
        guest_bytes: 1,
        sti_first: false,
        links: [LinkPoint::NONE; MAX_LINK_POINTS],
        dispatch_exit: 0,
        rep_exits: 0,
        code: vec![0x1f; 8],
        sites: vec![],
        relocs: vec![],
        prefix,
    }
    .seal()
}
const SCRATCH: usize = MAX_ENTRY_BYTES;

fn same(e: &Entry, o: &Owned, position: u64) {
    assert_eq!((e.position, e.rip, e.context), (position, o.rip, o.context));
    assert_eq!((e.guest_bytes, e.sti_first), (o.guest_bytes, o.sti_first));
    assert_eq!(
        (e.links, e.dispatch_exit, e.rep_exits),
        (o.links, o.dispatch_exit, o.rep_exits)
    );
    assert_eq!((e.code, e.prefix), (&o.code[..], &o.prefix[..]));
    assert_eq!(e.sites_count() * SITE_BYTES, o.sites.len());
    assert_eq!(e.sites, &o.sites[..]);
    assert_eq!(
        (0..e.relocs_count())
            .map(|i| e.reloc(i))
            .collect::<Vec<_>>(),
        o.relocs
    );
    assert_eq!(e.key(), key_of(o));
}

#[test]
fn encode_decode_round_trip() {
    let mut rng = Rng(1);
    let mut buffer = vec![0u8; SCRATCH];
    for i in 0..3000 {
        let o = random(
            &mut rng,
            (i, 0x7fff_0000_0000 + (i % 4096), [1, 2, 3, i as u8]),
            MAX_ENTRY_BYTES,
        );
        let n = o.parts().encode(64 * (1 + i), &mut buffer).unwrap();
        assert_eq!(n, o.bytes());
        same(&decode(&buffer[..n]).unwrap(), &o, 64 * (1 + i));
    }
    // And through a pool: publish, probe at the key's length, fetch.
    let span = Span::new(Layout {
        index_slots: 64,
        ring_bytes: 64 << 10,
        control_bytes: 128,
    });
    let (pool, mut w) = (span.pool(), span.writer());
    let (mut staging, mut scratch) = (vec![0u8; MAX_ENTRY_BYTES], vec![0u8; SCRATCH]);
    for i in 0..500 {
        let o = random(
            &mut rng,
            (i % 7, 0xffff_8000_0000_0000 + 16 * i, [9, 8, 7, 6]),
            8192,
        );
        let p = w.publish(&o.parts(), &mut scratch).unwrap();
        assert!(pool.probe(&key_of(&o)).contains(&p));
        let e = pool.fetch(p, &mut staging).unwrap();
        same(&e, &o, p);
        assert_eq!(sums(&e), (o.sum(), o.sum()));
    }
}

/// A prefix shorter than four bytes is keyed by its own length; a longer one is probed at each length.
#[test]
fn keys_by_lead_length() {
    let k = |len| Key::new(1, 0x1234, &[1, 2, 3, 4, 5], len);
    assert_eq!(k(9), k(4));
    assert_ne!(k(3), k(4));
    assert_eq!(
        Key::new(1, 0x1234, &[1, 2], 4),
        Key::new(1, 0x1234, &[1, 2, 3], 2)
    );
    // The RIP's half and page offset, not its page.
    assert_eq!(
        Key::new(1, 0x7000_1234, &[], 4),
        Key::new(1, 0x5000_1234, &[], 4)
    );
    assert_ne!(
        Key::new(1, 0x7000_1234, &[], 4),
        Key::new(1, 0xffff_8000_7000_1234, &[], 4)
    );
    assert_ne!(Key::new(1, 0x1234, &[], 4), Key::new(2, 0x1234, &[], 4));
}

fn invariants(bytes: &[u8], e: &Entry) {
    let inside = |s: &[u8]| {
        let (b, s) = (bytes.as_ptr_range(), s.as_ptr_range());
        s.start >= b.start && s.end <= b.end
    };
    assert!(
        [e.code, e.sites, e.relocs, e.prefix]
            .into_iter()
            .all(inside)
    );
    assert!(e.code.len() <= MAX_BLOCK_BYTES && e.code.len() % 4 == 0 && !e.code.is_empty());
    assert!(
        e.prefix.len() >= e.guest_bytes as usize
            && e.guest_bytes > 0
            && e.prefix.len() <= MAX_PREFIX
    );
    assert!(e.relocs_count() <= MAX_RELOCS as usize);
    let mut last = None;
    for i in 0..e.relocs_count() {
        let r = e.reloc(i);
        assert!((r >> 6) as usize * 4 + 4 <= e.code.len() && last.is_none_or(|l| l < r));
        last = Some(r);
    }
    for l in e.links.iter().filter(|l| l.offset != 0) {
        assert!(l.offset % 4 == 0 && l.offset as usize + 4 <= e.code.len());
    }
    assert!(e.dispatch_exit == 0 || e.dispatch_exit as usize + 4 <= e.code.len());
    assert!(e.rep_exits >> MAX_LINK_POINTS == 0);
    for i in 0..e.sites_count() {
        let _ = e.site(i);
    }
    assert!(bytes.len() <= MAX_ENTRY_BYTES && bytes.len() % ALIGN == 0);
}

#[test]
fn decoder_on_arbitrary_bytes_is_an_error_or_within_bounds() {
    let mut rng = Rng(2);
    let mut buffer = vec![0u8; SCRATCH];
    let (mut ok, mut total) = (0, 0);
    let mut check = |bytes: &[u8]| {
        total += 1;
        if let Ok(e) = decode(bytes) {
            ok += 1;
            invariants(bytes, &e);
        }
    };
    // Random bytes of every length up to a few entries, and a few larger and unaligned ones.
    for len in (0..1100).chain([4096, 4097, MAX_ENTRY_BYTES, MAX_ENTRY_BYTES + 64]) {
        for _ in 0..40 {
            check(&rng.bytes(len));
        }
    }
    // Valid entries with header words replaced by extremes or random values, and bytes changed anywhere.
    for i in 0..60_000 {
        let o = random(&mut rng, (i, 0x1000 + i % 4096, [1, 2, 3, 4]), 4096);
        let n = o.parts().encode(64, &mut buffer).unwrap();
        let mut bytes = buffer[..n].to_vec();
        for _ in 0..1 + rng.below(3) {
            match rng.below(3) {
                0 => {
                    let at = 4 * rng.below(HEADER_BYTES / 4 - 2);
                    let v = [
                        0,
                        1,
                        4,
                        u32::MAX,
                        u32::MAX - 3,
                        0x8000_0000,
                        rng.next() as u32,
                    ][rng.below(7)];
                    bytes[at..at + 4].copy_from_slice(&v.to_le_bytes());
                }
                1 => {
                    let at = rng.below(n);
                    bytes[at] = rng.next() as u8;
                }
                _ => {
                    let at = 4 * rng.below(n / 4);
                    bytes[at..at + 4].copy_from_slice(&u32::MAX.to_le_bytes());
                }
            }
        }
        check(&bytes);
    }
    assert!(ok > 1000 && ok < total, "{ok} of {total}");
    // Through a pool whose ring holds corrupted entries, fetched at every position over three laps and with staging areas
    // of several sizes: whatever is accepted lies inside the staging area.
    let span = Span::new(Layout {
        index_slots: 16,
        ring_bytes: 8192,
        control_bytes: 128,
    });
    let (pool, mut w) = (span.pool(), span.writer());
    let mut scratch = vec![0u8; SCRATCH];
    for i in 0..200 {
        let o = random(&mut rng, (1, 0x1000, [1, 2, 3, i as u8]), 1024);
        w.publish(&o.parts(), &mut scratch).unwrap();
        for _ in 0..2 {
            let word = rng.below(8192 / 8);
            span.memory[(span.layout.ring_offset() / 8) + word]
                .store(rng.next(), Ordering::Relaxed);
        }
    }
    let mut staging = vec![0u8; MAX_ENTRY_BYTES];
    let mut accepted = 0;
    let end = w.peek(64);
    for position in (end.saturating_sub(3 * 8192)..end)
        .step_by(8)
        .chain([u64::MAX, 1 << 60])
    {
        for cap in [MAX_ENTRY_BYTES, 1000, 128, 100] {
            if let Ok(e) = pool.fetch(position, &mut staging[..cap]) {
                accepted += 1;
                assert_eq!(e.position, position);
                let n =
                    HEADER_BYTES + e.code.len() + e.sites.len() + e.relocs.len() + e.prefix.len();
                // SAFETY: the entry's parts lie in the staging area, its header (128 bytes) before the code.
                let bytes = unsafe {
                    core::slice::from_raw_parts(
                        e.code.as_ptr().sub(HEADER_BYTES),
                        n.next_multiple_of(ALIGN),
                    )
                };
                invariants(bytes, &e);
            }
        }
    }
    assert!(accepted > 0);
}

#[test]
fn decoder_refuses_each_bound() {
    let mut rng = Rng(3);
    let mut o = random(&mut rng, (1, 0x1000, [1, 2, 3, 4]), 4096);
    // One of each: a link point, a REP exit at it, a computed exit, and relocations.
    o.links = [LinkPoint::NONE; MAX_LINK_POINTS];
    o.links[0] = LinkPoint {
        offset: 8,
        word: 1,
        target: 2,
    };
    o.rep_exits = 1;
    o.dispatch_exit = 4;
    o.code = vec![0; 64];
    o.sites = vec![];
    o.relocs = vec![1 << 6, 2 << 6 | RELOC_MOVE];
    let o = o.seal();
    let mut buffer = vec![0u8; SCRATCH];
    let n = o.parts().encode(64, &mut buffer).unwrap();
    assert!(decode(&buffer[..n]).is_ok());
    let patched = |at: usize, v: u32| {
        let mut b = buffer[..n].to_vec();
        b[at..at + 4].copy_from_slice(&v.to_le_bytes());
        decode(&b).err()
    };
    let (len, code, guest, prefix, sites, relocs, flags, dispatch, rep) =
        (24, 28, 32, 36, 40, 44, 48, 52, 56);
    for (what, at, v) in [
        ("length", len, n as u32 + 64),
        ("length", len, n as u32 - 64),
        ("code 0", code, 0),
        (
            "code over the block limit",
            code,
            MAX_BLOCK_BYTES as u32 + 4,
        ),
        ("code not a word multiple", code, 66),
        ("guest bytes 0", guest, 0),
        ("guest bytes over the prefix", guest, 1_000_000),
        ("prefix over two pages", prefix, MAX_PREFIX as u32 + 1),
        ("sites count", sites, u32::MAX),
        ("sites count", sites, 1000),
        ("relocation count", relocs, MAX_RELOCS + 1),
        ("relocation count", relocs, u32::MAX),
        ("flags", flags, 0),
        ("flags", flags, 7),
        ("computed exit outside", dispatch, 64),
        ("computed exit outside", dispatch, u32::MAX),
        ("computed exit unaligned", dispatch, 6),
        ("REP exit without a link point", rep, 2),
        ("REP exit bit", rep, 1 << 4),
        ("link offset outside", 64, 64),
        ("link offset outside", 64, u32::MAX - 3),
        ("link offset unaligned", 64, 10),
    ] {
        assert!(patched(at, v).is_some(), "{what}: {v}");
    }
    // Relocations out of order, repeated, or outside the code.
    let reloc_at = HEADER_BYTES + 64;
    for (a, b) in [
        (2u32 << 6, 1u32 << 6),
        (1 << 6, 1 << 6),
        (1 << 6, 16 << 6),
        (1 << 6, u32::MAX),
    ] {
        let mut x = buffer[..n].to_vec();
        x[reloc_at..reloc_at + 4].copy_from_slice(&a.to_le_bytes());
        x[reloc_at + 4..reloc_at + 8].copy_from_slice(&b.to_le_bytes());
        assert!(decode(&x).is_err(), "{a:#x} {b:#x}");
    }
    assert!(decode(&buffer[..n - 64]).is_err() && decode(&[]).is_err());
    // More relocations than a block records, all in order and inside the code, in a header whose length is right.
    let mut many = o.clone();
    many.code = vec![0; 4 * (MAX_RELOCS as usize + 8)];
    many.links = [LinkPoint::NONE; MAX_LINK_POINTS];
    many.rep_exits = 0;
    many.dispatch_exit = 0;
    many.relocs = (0..=MAX_RELOCS).map(|w| w << 6).collect();
    assert!(matches!(
        many.seal().parts().encode(64, &mut buffer),
        Err(Refusal::Malformed("counts"))
    ));
}

/// Review finding B1: a slot left over from an earlier lap names a position whose place now lies inside a newer entry,
/// whose prefix holds a well-formed header repeating exactly that position. The tail refuses it.
#[test]
fn forged_header_inside_a_newer_entrys_prefix_is_refused() {
    let span = Span::new(TINY);
    let (pool, mut w) = (span.pool(), span.writer());
    let mut scratch = vec![0u8; SCRATCH];
    let k = (7, 0x1000, [1, 2, 3, 4]);
    let a = sized(192, k.0, k.1, k.2);
    let set = key_of(&a).set(pool.sets());
    let filler = |c| sized(192, c, 0x2000, [0; 4]);
    // Fillers in other sets than the real entry's, so its slot stays.
    let off_set: Vec<u64> = (100..)
        .filter(|&c| key_of(&filler(c)).set(pool.sets()) != set)
        .take(8)
        .collect();
    assert_eq!(w.publish(&filler(off_set[0]).parts(), &mut scratch), Ok(64));
    let old = w.publish(&a.parts(), &mut scratch).unwrap();
    assert_eq!(old, 256);
    for &c in &off_set[1..7] {
        w.publish(&filler(c).parts(), &mut scratch).unwrap();
    }
    let mut staging = vec![0u8; MAX_ENTRY_BYTES];
    // Before it is overwritten the real entry is served.
    same(&pool.fetch(old, &mut staging).unwrap(), &a, old);
    // The newer entry starts the next lap (the ring's end cannot hold it) and carries, 120 bytes into its prefix, a header
    // of its own making for position `old`: a whole entry in the guest-chosen bytes.
    let mut forged = [0u8; 192];
    sized(192, k.0, k.1, k.2)
        .parts()
        .encode(old, &mut forged)
        .unwrap();
    let mut e = sized(512, 99, 0x3000, [5, 5, 5, 5]);
    let prefix_at = HEADER_BYTES + e.code.len();
    let forged_at = (old as usize % TINY.ring_bytes) - prefix_at;
    e.prefix[forged_at..forged_at + 192].copy_from_slice(&forged);
    let e = e.seal();
    let p = w.publish(&e.parts(), &mut scratch).unwrap();
    assert_eq!(p % TINY.ring_bytes as u64, 0);
    assert!(
        pool.probe(&key_of(&a)).contains(&old),
        "the stale slot is still there"
    );
    // The forgery is what a reader would decode at that place, were it not refused.
    let mut raw = vec![0u8; 192];
    assert!(pool.copy_out(old as usize % TINY.ring_bytes, &mut raw));
    assert_eq!(decode(&raw).unwrap().position, old);
    assert_eq!(
        pool.fetch(old, &mut staging).err(),
        Some(Refusal::Overwritten)
    );
    same(&pool.fetch(p, &mut staging).unwrap(), &e, p);
}

/// A slot naming a position the data at its place does not carry (the publication order's slot store before the data would
/// make one visible): the copied header repeats another position, so it is refused, though the position is not below the
/// tail and the old entry there is whole.
#[test]
fn a_slot_whose_entry_repeats_another_position_is_refused() {
    let span = Span::new(TINY);
    let (pool, mut w) = (span.pool(), span.writer());
    let mut scratch = vec![0u8; SCRATCH];
    let a = sized(192, 1, 0x1000, [1, 2, 3, 4]);
    assert_eq!(
        w.publish(&sized(192, 5, 0x2000, [0; 4]).parts(), &mut scratch),
        Ok(64)
    );
    assert_eq!(w.publish(&a.parts(), &mut scratch), Ok(256));
    // Fill the lap; the next entry (256 bytes) starts the next lap, covering places 0..256 and leaving `a` whole.
    while w.peek(256) != 2048 {
        w.publish(&sized(192, 10, 0x2000, [0; 4]).parts(), &mut scratch)
            .unwrap();
    }
    assert_eq!(
        w.publish(&sized(256, 6, 0x2000, [0; 4]).parts(), &mut scratch),
        Ok(2048)
    );
    let k = sized(192, 8, 0x4000, [4, 3, 2, 1]);
    let p = w.peek(192);
    assert_eq!(p, 2304); // place 256: where `a` is
    // The first half of a publication: the tail and the slot, but not the data yet.
    let place = w.begin(p, 192);
    w.link(&key_of(&k), p);
    assert!(p >= pool.tail());
    let mut staging = vec![0u8; MAX_ENTRY_BYTES];
    assert!(pool.probe(&key_of(&k)).contains(&p));
    assert_eq!(pool.fetch(p, &mut staging).err(), Some(Refusal::Position));
    // The data lands: now the entry is served.
    let mut buffer = vec![0u8; 192];
    k.parts().encode(p, &mut buffer).unwrap();
    w.store(place, &buffer);
    same(&pool.fetch(p, &mut staging).unwrap(), &k, p);
}

#[test]
fn slots_no_entry_can_have_are_refused() {
    let span = Span::new(TINY);
    let pool = span.pool();
    let mut w = span.writer();
    let mut scratch = vec![0u8; SCRATCH];
    let p = w
        .publish(&sized(192, 1, 0x1000, [1, 2, 3, 4]).parts(), &mut scratch)
        .unwrap();
    let mut staging = vec![0u8; MAX_ENTRY_BYTES];
    for bad in [0, 1, 32, p + 8, u64::MAX, 1 << 60] {
        assert_eq!(
            pool.fetch(bad, &mut staging).err(),
            Some(Refusal::Position),
            "{bad}"
        );
    }
    // A staging area too small for the header or the entry.
    assert_eq!(
        pool.fetch(p, &mut staging[..100]).err(),
        Some(Refusal::Length)
    );
    assert_eq!(
        pool.fetch(p, &mut staging[..191]).err(),
        Some(Refusal::Length)
    );
    assert!(pool.fetch(p, &mut staging[..192]).is_ok());
    // Too big for the pool, the scratch or an entry's limit; malformed parts are not published.
    let big = sized(512, 1, 0x1000, [1, 2, 3, 4]);
    assert_eq!(
        w.publish(&big.parts(), &mut scratch[..256]),
        Err(Skip::TooBig)
    );
    let mut huge = big.clone();
    huge.prefix = vec![0; MAX_ENTRY_BYTES];
    assert_eq!(w.publish(&huge.parts(), &mut scratch), Err(Skip::TooBig));
    let mut bad = big.clone();
    bad.relocs = vec![5 << 6, 1 << 6];
    assert!(matches!(
        w.publish(&bad.parts(), &mut scratch),
        Err(Skip::Malformed(_))
    ));
    let mut bad = big.clone();
    bad.sites = vec![0; 41];
    assert!(matches!(
        w.publish(&bad.parts(), &mut scratch),
        Err(Skip::Malformed(_))
    ));
}

#[test]
fn the_ring_wraps_without_straddling_and_the_tail_follows() {
    let span = Span::new(TINY);
    let (pool, mut w) = (span.pool(), span.writer());
    let mut scratch = vec![0u8; SCRATCH];
    let mut rng = Rng(4);
    let mut staging = vec![0u8; MAX_ENTRY_BYTES];
    let (mut last, mut laps) = (0, 0);
    for i in 0..400 {
        let n = 192 + 64 * rng.below(11);
        let o = sized(n, i, 0x1000, [1, 2, 3, 4]);
        let p = w.publish(&o.parts(), &mut scratch).unwrap();
        assert!(p >= last.max(FIRST_POSITION) && p % 64 == 0);
        assert!((p % 2048) as usize + n <= 2048, "never straddles");
        assert_eq!(w.tail(), (p + n as u64).saturating_sub(2048));
        assert_eq!(pool.tail(), w.tail());
        laps = laps.max(p / 2048);
        // The entry just published is whole; the first lap's tail saturates at 0.
        same(&pool.fetch(p, &mut staging).unwrap(), &o, p);
        if p + (n as u64) <= 2048 {
            assert_eq!(pool.tail(), 0);
        }
        last = p + n as u64;
    }
    assert!(laps > 100);
}

#[test]
fn the_switch_turns_lookups_off() {
    let span = Span::new(TINY);
    let (pool, mut w) = (span.pool(), span.writer());
    let mut scratch = vec![0u8; SCRATCH];
    let o = sized(192, 1, 0x1000, [1, 2, 3, 4]);
    assert!(pool.enabled());
    let p = w.publish(&o.parts(), &mut scratch).unwrap();
    assert!(pool.probe(&key_of(&o)).contains(&p));
    w.disable();
    assert!(!pool.enabled());
    assert_eq!(pool.probe(&key_of(&o)), [0; WAYS]);
    assert_eq!(w.publish(&o.parts(), &mut scratch), Err(Skip::Off));
}

/// A full set gives up its lowest position first.
#[test]
fn a_full_set_replaces_its_oldest_slot() {
    let span = Span::new(Layout {
        index_slots: 8,
        ring_bytes: 64 << 10,
        control_bytes: 128,
    });
    let (pool, mut w) = (span.pool(), span.writer());
    let mut scratch = vec![0u8; SCRATCH];
    let k = sized(192, 1, 0x1000, [1, 2, 3, 4]);
    let ps: Vec<u64> = (0..6)
        .map(|_| w.publish(&k.parts(), &mut scratch).unwrap())
        .collect();
    let mut got: Vec<u64> = pool
        .probe(&key_of(&k))
        .into_iter()
        .filter(|&p| p != 0)
        .collect();
    got.sort();
    assert_eq!(got, ps[2..]);
}

/// Writer and readers on separate threads with the production fences, a tiny ring and entries of every size, so slots from
/// earlier laps are left naming places in the middle of newer entries (some carrying forged whole entries for exactly those
/// positions). Every entry a reader accepts must be whole (its checksum), the one published at that position (the writer's
/// record of position and checksum) and for the key probed.
#[test]
fn readers_never_accept_a_torn_or_forged_entry() {
    const KEYS: usize = 24;
    let layout = Layout {
        index_slots: 32,
        ring_bytes: 4096,
        control_bytes: 128,
    };
    let span = Span::new(layout);
    let (pool, mut w) = (span.pool(), span.writer());
    let keys: Vec<(u64, u64, [u8; 4])> = (0..KEYS as u64)
        .map(|i| (i % 5, 0x1000 + 16 * i, [i as u8, 2, 3, 4]))
        .collect();
    let published: Mutex<HashMap<u64, u64>> = Mutex::default();
    let done = AtomicBool::new(false);
    let count = || AtomicUsize::new(0);
    let (accepted, forgeries, overwritten, positions, others) =
        (count(), count(), count(), count(), count());
    thread::scope(|scope| {
        for r in 0..3u64 {
            let (keys, done, published) = (&keys, &done, &published);
            let (accepted, overwritten, positions, others) =
                (&accepted, &overwritten, &positions, &others);
            scope.spawn(move || {
                let mut rng = Rng(100 + r);
                let mut staging = vec![0u8; MAX_ENTRY_BYTES];
                while !done.load(Ordering::Relaxed) {
                    let k = keys[rng.below(KEYS)];
                    let key = Key::new(k.0, k.1, &k.2, LEAD);
                    for p in pool.probe(&key).into_iter().filter(|&p| p != 0) {
                        match pool.fetch(p, &mut staging) {
                            Ok(e) => {
                                if e.key() != key {
                                    others.fetch_add(1, Ordering::Relaxed);
                                    continue;
                                }
                                let (want, has) = sums(&e);
                                assert_eq!(want, has, "torn or forged entry accepted at {p}");
                                assert_eq!(
                                    published.lock().unwrap().get(&e.position),
                                    Some(&has),
                                    "an entry nobody published at {} accepted",
                                    e.position
                                );
                                accepted.fetch_add(1, Ordering::Relaxed);
                            }
                            Err(Refusal::Overwritten) => {
                                overwritten.fetch_add(1, Ordering::Relaxed);
                            }
                            Err(_) => {
                                positions.fetch_add(1, Ordering::Relaxed);
                            }
                        }
                    }
                }
            });
        }
        let mut rng = Rng(7);
        let mut scratch = vec![0u8; SCRATCH];
        let mut history: Vec<(u64, usize)> = vec![];
        for _ in 0..40_000 {
            let ki = rng.below(KEYS);
            let mut o = random(&mut rng, keys[ki], 1500);
            // Forge, in the prefix, whole entries for stale positions whose places the new entry covers.
            let p = w.peek(o.bytes());
            let place = (p % 4096) as usize;
            let prefix_at = HEADER_BYTES + o.code.len() + o.sites.len() + 4 * o.relocs.len();
            let mut taken = 0;
            for &(q, kj) in history.iter().rev().take(40) {
                let at = (q % 4096) as usize;
                if at >= place + prefix_at + taken && at + 192 <= place + prefix_at + o.prefix.len()
                {
                    let (c, rip, lead) = keys[kj];
                    let mut forged = [0u8; 192];
                    sized(192, c, rip, lead)
                        .parts()
                        .encode(q, &mut forged)
                        .unwrap();
                    o.prefix[at - place - prefix_at..][..192].copy_from_slice(&forged);
                    taken = at + 192 - place - prefix_at;
                    forgeries.fetch_add(1, Ordering::Relaxed);
                }
            }
            let o = o.seal();
            published.lock().unwrap().insert(p, o.sum());
            assert_eq!(w.publish(&o.parts(), &mut scratch), Ok(p));
            history.push((p, ki));
            if history.len() > 64 {
                history.remove(0);
            }
        }
        done.store(true, Ordering::Relaxed);
    });
    let n = |c: &AtomicUsize| c.load(Ordering::Relaxed);
    std::eprintln!(
        "accepted {}, forged entries placed {}, refused overwritten {}, other refusals {}, other key {}",
        n(&accepted),
        n(&forgeries),
        n(&overwritten),
        n(&positions),
        n(&others)
    );
    assert!(
        n(&accepted) > 1000 && n(&forgeries) > 500 && n(&overwritten) > 1000,
        "the run did not exercise the protocol"
    );
}

/// The ring-end bound: a slot (stale, or a defect) whose place lies near the ring's end, where the bytes after it are not the
/// ring's: the header's length would run past the end, or the header itself does. The bytes there form, together with the
/// guard after the span, a whole valid entry for exactly that position, so only the bound refuses it.
#[test]
fn an_entry_never_extends_past_the_rings_end() {
    let span = Span::with_guard(TINY, 2048);
    let (pool, _writer) = (span.pool(), span.writer());
    let mut staging = vec![0u8; MAX_ENTRY_BYTES];
    let mut buffer = vec![0u8; SCRATCH];
    let ring = TINY.ring_bytes;
    // (entry size, its place: the header fits before the ring's end or it does not, the entry never does)
    for (n, place, want) in [
        (1024, ring - 128, Refusal::Length),
        (192, ring - 128, Refusal::Length),
        (192, ring - 64, Refusal::Position),
        (1024, ring - 64, Refusal::Position),
    ] {
        let o = sized(n, 1, 0x1000, [1, 2, 3, 4]);
        let position = place as u64; // lap 0, and the tail is 0
        o.parts().encode(position, &mut buffer).unwrap();
        let first = (TINY.ring_offset() + place) / 8;
        for (i, w) in buffer[..n].chunks_exact(8).enumerate() {
            span.memory[first + i]
                .store(u64::from_le_bytes(w.try_into().unwrap()), Ordering::Relaxed);
        }
        assert!(decode(&buffer[..n]).is_ok());
        assert_eq!(
            pool.fetch(position, &mut staging).err(),
            Some(want),
            "{n} bytes at place {place}"
        );
    }
}
