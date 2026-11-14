//! The shared template pool (cross-vCPU reuse plan, section 3): admitted template translations the host publishes into a
//! read-only span every vCPU's resident maps, found by content. One writer (the host, under one lock), any number of
//! readers; no reader depends on another vCPU's state, and nothing is ever freed under a reader.
//!
//! Layout of the span: a control block, then the index (4-way sets of u64 slots), then the ring of entries. A position is
//! a monotonic byte count of everything ever published (the first entry's is [`FIRST_POSITION`], so a zero slot is empty);
//! an entry's place in the ring is its position modulo the ring's size, and an entry never straddles the ring's end (the
//! writer skips to the next lap; the skipped bytes are never named by a slot). A slot names a full position, never only a
//! place (review finding B1: a slot left over from an earlier lap would name the middle of a newer entry, whose
//! guest-chosen prefix bytes could pass as a header).
//!
//! Publication (review finding B2: a release store of the tail would not order the data stores after it): the tail
//! (the lowest position whose entry is still whole) is stored relaxed, then a release fence, then the entry's bytes, then
//! the slot with a release store. A reader loads the slot with acquire, copies the entry into private memory, executes an
//! acquire fence and loads the tail: it accepts only an entry whose copied header repeats the slot's position and which
//! is not below the tail. If any copied byte came from a later entry, the tail store that preceded it is visible to that
//! load, so the entry is refused; every check then reads the private copy. Nothing in the resident admits code again:
//! these two checks are what containment of the pool's code rests on.
use crate::code_object::Site;
use crate::execution::{
    LinkPoint, MAX_LINK_POINTS, STAGING_BYTES,
    cache::{MAX_BLOCK_BYTES, MAX_RELOCS},
};
use core::sync::atomic::{AtomicU64, Ordering::*, fence};

/// Entries start at multiples of this and are padded to a multiple of it.
pub const ALIGN: usize = 64;
/// The first position ever published (position 0 is reserved: a zero slot is empty).
pub const FIRST_POSITION: u64 = ALIGN as u64;
/// An entry's header, then its code, sites, packed relocations and prefix.
pub const HEADER_BYTES: usize = 128;
/// The largest entry (the resident's staging area holds its private copy).
pub const MAX_ENTRY_BYTES: usize = STAGING_BYTES;
/// Most guest bytes a block's answer reads: two pages.
pub const MAX_PREFIX: usize = 8192;
pub const SITE_BYTES: usize = core::mem::size_of::<Site>();
const _: () = assert!(SITE_BYTES == 40 && HEADER_BYTES == 64 + 16 * MAX_LINK_POINTS);
/// Slots per set: the set is one probe's cache line (32 bytes, 32-byte aligned).
pub const WAYS: usize = 4;
/// Leading prefix bytes a key holds (the host's `LEAD`); a prefix shorter than this is keyed by all of it.
pub const LEAD: usize = 4;
/// A slot is its entry's position / 64 above [`TAG_BITS`] and a tag of the key's hash below.
const TAG_BITS: u32 = 24;
const TAG_MASK: u64 = (1 << TAG_BITS) - 1;
/// Positions up to 2^46 (64 TiB of publication) fit a slot.
const POSITION_LIMIT: u64 = 1 << (64 - TAG_BITS + 6);

/// `Entry::flags`.
pub const FLAG_RELOCATABLE: u32 = 1;
pub const FLAG_STI_FIRST: u32 = 2;

/// Where things lie in a pool's span.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Layout {
    /// A power of two, a multiple of [`WAYS`] and at least 8.
    pub index_slots: usize,
    /// A multiple of [`ALIGN`], at least two entries' worth of the largest ([`MAX_ENTRY_BYTES`]) in production.
    pub ring_bytes: usize,
    /// Before the index: the control block's mapping granule (a multiple of 64; only its first 128 bytes are used).
    pub control_bytes: usize,
}
/// The production pool: a 2 MiB mapping granule for the control block (one page used), a 16 MiB index and a 256 MiB ring.
pub const PRODUCTION: Layout = Layout {
    index_slots: 1 << 21,
    ring_bytes: 256 << 20,
    control_bytes: 2 << 20,
};
impl Layout {
    pub const fn index_offset(&self) -> usize {
        self.control_bytes
    }
    pub const fn ring_offset(&self) -> usize {
        self.control_bytes + self.index_slots * 8
    }
    /// The span's bytes.
    pub const fn span_bytes(&self) -> usize {
        self.ring_offset() + self.ring_bytes
    }
    pub const fn valid(&self) -> bool {
        self.index_slots.is_power_of_two()
            && self.index_slots >= 8
            && self.ring_bytes % ALIGN == 0
            && self.ring_bytes >= 4 * ALIGN
            && self.control_bytes % ALIGN == 0
            && self.control_bytes >= 2 * ALIGN
    }
}
const _: () = assert!(PRODUCTION.valid());
const _: () = assert!(PRODUCTION.ring_bytes >= 2 * MAX_ENTRY_BYTES);

/// Control block: the switch (nonzero: lookups may use the pool; the host clears it for good, [`Writer::disable`]) and,
/// on its own line, the tail.
const ENABLED: usize = 0;
const TAIL: usize = 64;

/// What a reader refuses, for its counters.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The pool is switched off.
    Off,
    /// A slot position that no entry can have, or a copied header that does not repeat its slot's position.
    Position,
    /// The entry is below the tail: some of it may have been overwritten.
    Overwritten,
    /// A length that no entry has, or one beyond the staging area or the ring's end.
    Length,
    /// The copy decodes to no entry (a count or offset out of bounds).
    Malformed(&'static str),
}

/// The leading bytes' hash input: the RIP's half and page offset (the host's key), the context and up to [`LEAD`] bytes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Key {
    context: u64,
    place: u64,
    lead: u32,
    len: u8,
}
impl Key {
    /// The key of an entry for `rip` under `context` whose prefix starts with `lead`'s first `len` bytes (at most
    /// [`LEAD`]).
    pub fn new(context: u64, rip: u64, lead: &[u8], len: usize) -> Key {
        let len = len.min(LEAD).min(lead.len());
        let mut v = 0u32;
        for &b in &lead[..len] {
            v = v << 8 | u32::from(b);
        }
        Key {
            context,
            place: rip & (1 << 63 | 0xfff),
            lead: v,
            len: len as u8,
        }
    }
    /// Multiply-xorshift of the four words; the guest chooses bytes and RIPs but a collision only costs a probe.
    fn hash(&self) -> u64 {
        let mut h = self
            .context
            .wrapping_add(0x9e37_79b9_7f4a_7c15)
            .wrapping_mul(0xbf58_476d_1ce4_e5b9);
        h = (h ^ self.place ^ h >> 31).wrapping_mul(0x94d0_49bb_1331_11eb);
        h = (h ^ (u64::from(self.lead) | u64::from(self.len) << 32) ^ h >> 29)
            .wrapping_mul(0xd6e8_feb8_6659_fd93);
        h ^ h >> 32
    }
    fn set(&self, sets: usize) -> usize {
        self.hash() as usize & (sets - 1)
    }
    fn tag(&self) -> u64 {
        self.hash() >> 40 & TAG_MASK
    }
}

/// One entry's parts, as the writer takes them: `relocs` packed ([`crate::execution::pack_reloc`]) and in increasing
/// order, `sites` the raw [`Site`] records.
pub struct Parts<'a> {
    pub rip: u64,
    pub context: u64,
    pub guest_bytes: u32,
    pub sti_first: bool,
    pub links: [LinkPoint; MAX_LINK_POINTS],
    pub dispatch_exit: u32,
    pub rep_exits: u32,
    pub code: &'a [u8],
    pub sites: &'a [u8],
    pub relocs: &'a [u32],
    pub prefix: &'a [u8],
}
impl Parts<'_> {
    /// The entry's bytes, padded: `None` beyond [`MAX_ENTRY_BYTES`].
    pub fn entry_bytes(&self) -> Option<usize> {
        let total = HEADER_BYTES
            .checked_add(self.code.len())?
            .checked_add(self.sites.len())?
            .checked_add(self.relocs.len().checked_mul(4)?)?
            .checked_add(self.prefix.len())?;
        let n = total.checked_next_multiple_of(ALIGN)?;
        (n <= MAX_ENTRY_BYTES).then_some(n)
    }
    /// The key it is published under.
    pub fn key(&self) -> Key {
        Key::new(self.context, self.rip, self.prefix, LEAD)
    }
    /// Writes the entry for `position` into `out` and returns its length; what [`decode`] would refuse is refused here, so
    /// no published entry is one a reader refuses.
    pub fn encode(&self, position: u64, out: &mut [u8]) -> Result<usize, Refusal> {
        if self.sites.len() % SITE_BYTES != 0 {
            return Err(Refusal::Malformed("sites"));
        }
        let n = self.entry_bytes().ok_or(Refusal::Length)?;
        let out = out.get_mut(..n).ok_or(Refusal::Length)?;
        out.fill(0);
        let put64 =
            |out: &mut [u8], at: usize, v: u64| out[at..at + 8].copy_from_slice(&v.to_le_bytes());
        let put32 =
            |out: &mut [u8], at: usize, v: u32| out[at..at + 4].copy_from_slice(&v.to_le_bytes());
        put64(out, 0, position);
        put64(out, 8, self.rip);
        put64(out, 16, self.context);
        put32(out, 24, n as u32);
        put32(out, 28, self.code.len() as u32);
        put32(out, 32, self.guest_bytes);
        put32(out, 36, self.prefix.len() as u32);
        put32(out, 40, (self.sites.len() / SITE_BYTES) as u32);
        put32(out, 44, self.relocs.len() as u32);
        put32(
            out,
            48,
            FLAG_RELOCATABLE | if self.sti_first { FLAG_STI_FIRST } else { 0 },
        );
        put32(out, 52, self.dispatch_exit);
        put32(out, 56, self.rep_exits);
        for (i, l) in self.links.iter().enumerate() {
            put32(out, 64 + 16 * i, l.offset);
            put32(out, 68 + 16 * i, l.word);
            put64(out, 72 + 16 * i, l.target);
        }
        let mut at = HEADER_BYTES;
        out[at..at + self.code.len()].copy_from_slice(self.code);
        at += self.code.len();
        out[at..at + self.sites.len()].copy_from_slice(self.sites);
        at += self.sites.len();
        for (i, r) in self.relocs.iter().enumerate() {
            out[at + 4 * i..at + 4 * i + 4].copy_from_slice(&r.to_le_bytes());
        }
        at += 4 * self.relocs.len();
        out[at..at + self.prefix.len()].copy_from_slice(self.prefix);
        decode(out).map(|_| n)
    }
}

/// A decoded entry: its fields by value, its parts as slices of the copy it was decoded from (every one inside it).
#[derive(Clone, Copy, Debug)]
pub struct Entry<'a> {
    pub position: u64,
    pub rip: u64,
    pub context: u64,
    pub guest_bytes: u32,
    pub sti_first: bool,
    pub links: [LinkPoint; MAX_LINK_POINTS],
    pub dispatch_exit: u32,
    pub rep_exits: u32,
    pub code: &'a [u8],
    sites: &'a [u8],
    relocs: &'a [u8],
    pub prefix: &'a [u8],
}
impl Entry<'_> {
    pub fn sites_count(&self) -> usize {
        self.sites.len() / SITE_BYTES
    }
    /// Site `i` (`i < sites_count`).
    pub fn site(&self, i: usize) -> Site {
        // SAFETY: in bounds by the slice; a `Site` is plain integers, so every byte pattern is one (`read_unaligned`).
        unsafe {
            core::ptr::read_unaligned(self.sites[i * SITE_BYTES..][..SITE_BYTES].as_ptr().cast())
        }
    }
    pub fn relocs_count(&self) -> usize {
        self.relocs.len() / 4
    }
    /// Packed relocation `i` (`i < relocs_count`): in increasing order, each inside the code.
    pub fn reloc(&self, i: usize) -> u32 {
        u32::from_le_bytes(self.relocs[4 * i..4 * i + 4].try_into().unwrap())
    }
    /// The key it was published under.
    pub fn key(&self) -> Key {
        Key::new(self.context, self.rip, self.prefix, LEAD)
    }
}

/// The entry `bytes` hold, every count and offset checked against `bytes` and the largest block (nothing here reads
/// outside `bytes`, whatever they are): the header's own length is exactly `bytes.len()`, the parts follow it in order
/// and fill it but for the padding, the code is a multiple of four bytes up to [`MAX_BLOCK_BYTES`], at most
/// [`MAX_RELOCS`] relocations in increasing order each inside the code, each link point and the computed exit's word
/// inside the code, REP exits only at link points, and the prefix covers the block's guest bytes.
pub fn decode(bytes: &[u8]) -> Result<Entry<'_>, Refusal> {
    let bad = Refusal::Malformed;
    if bytes.len() < HEADER_BYTES || bytes.len() > MAX_ENTRY_BYTES || bytes.len() % ALIGN != 0 {
        return Err(Refusal::Length);
    }
    let u64_at = |at: usize| u64::from_le_bytes(bytes[at..at + 8].try_into().unwrap());
    let u32_at = |at: usize| u32::from_le_bytes(bytes[at..at + 4].try_into().unwrap());
    let (len, code, guest_bytes, prefix) = (u32_at(24), u32_at(28), u32_at(32), u32_at(36));
    let (sites, relocs, flags) = (u32_at(40), u32_at(44), u32_at(48));
    let (dispatch_exit, rep_exits) = (u32_at(52), u32_at(56));
    if len as usize != bytes.len() {
        return Err(Refusal::Length);
    }
    if flags & !(FLAG_RELOCATABLE | FLAG_STI_FIRST) != 0 || flags & FLAG_RELOCATABLE == 0 {
        return Err(bad("flags"));
    }
    if code == 0 || code as usize > MAX_BLOCK_BYTES || code % 4 != 0 {
        return Err(bad("code size"));
    }
    if guest_bytes == 0 || prefix > MAX_PREFIX as u32 || prefix < guest_bytes {
        return Err(bad("prefix"));
    }
    if relocs > MAX_RELOCS || sites as usize > STAGING_BYTES / SITE_BYTES {
        return Err(bad("counts"));
    }
    // Each part's end, in order (no overflow: every count is bounded above).
    let code_at = HEADER_BYTES;
    let sites_at = code_at + code as usize;
    let relocs_at = sites_at + sites as usize * SITE_BYTES;
    let prefix_at = relocs_at + 4 * relocs as usize;
    let end = prefix_at + prefix as usize;
    if end.next_multiple_of(ALIGN) != bytes.len() {
        return Err(bad("parts"));
    }
    let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
    for (i, l) in links.iter_mut().enumerate() {
        *l = LinkPoint {
            offset: u32_at(64 + 16 * i),
            word: u32_at(68 + 16 * i),
            target: u64_at(72 + 16 * i),
        };
        if l.offset != 0 && (l.offset % 4 != 0 || l.offset as usize + 4 > code as usize) {
            return Err(bad("link point"));
        }
    }
    if dispatch_exit != 0 && (dispatch_exit % 4 != 0 || dispatch_exit as usize + 4 > code as usize)
    {
        return Err(bad("computed exit"));
    }
    if rep_exits >> MAX_LINK_POINTS != 0
        || (0..MAX_LINK_POINTS).any(|i| rep_exits >> i & 1 != 0 && links[i].offset == 0)
    {
        return Err(bad("REP exits"));
    }
    let mut last = None;
    for i in 0..relocs as usize {
        let r = u32::from_le_bytes(bytes[relocs_at + 4 * i..][..4].try_into().unwrap());
        if (r >> 6) as usize * 4 + 4 > code as usize || last.is_some_and(|l| l >= r) {
            return Err(bad("relocation"));
        }
        last = Some(r);
    }
    Ok(Entry {
        position: u64_at(0),
        rip: u64_at(8),
        context: u64_at(16),
        guest_bytes,
        sti_first: flags & FLAG_STI_FIRST != 0,
        links,
        dispatch_exit,
        rep_exits,
        code: &bytes[code_at..sites_at],
        sites: &bytes[sites_at..relocs_at],
        relocs: &bytes[relocs_at..prefix_at],
        prefix: &bytes[prefix_at..end],
    })
}

/// The pool's span, as the writer (the host) and the readers (the residents) see it. Copyable: the writer's exclusivity
/// is [`Writer`]'s.
#[derive(Clone, Copy)]
pub struct Pool {
    base: *mut u8,
    layout: Layout,
}
// SAFETY: every access to the span is an atomic, so sharing the pointer between threads is sound.
unsafe impl Send for Pool {}
unsafe impl Sync for Pool {}
impl Pool {
    /// # Safety
    /// `base` is 8-byte aligned and valid for reads and writes of `layout.span_bytes()` (reads only, for a reader mapped
    /// read-only: it never calls [`Writer::new`]) for as long as the value or any copy is used; `layout.valid()`.
    pub const unsafe fn new(base: *mut u8, layout: Layout) -> Self {
        Self { base, layout }
    }
    pub fn layout(&self) -> Layout {
        self.layout
    }
    fn word(&self, offset: usize) -> &AtomicU64 {
        // SAFETY: `new`'s guarantee: in the span, 8-aligned (every offset used is a multiple of 8).
        unsafe { &*self.base.add(offset).cast::<AtomicU64>() }
    }
    fn slot(&self, set: usize, way: usize) -> &AtomicU64 {
        self.word(self.layout.index_offset() + 8 * (WAYS * set + way))
    }
    fn sets(&self) -> usize {
        self.layout.index_slots / WAYS
    }
    /// The switch every lookup reads first.
    pub fn enabled(&self) -> bool {
        self.word(ENABLED).load(Acquire) != 0
    }
    /// The lowest position whose entry is whole (a lookup's check loads it itself, after its copy).
    pub fn tail(&self) -> u64 {
        self.word(TAIL).load(Relaxed)
    }
    /// The positions the set of `key` names under its tag (0: no slot, or a slot of another tag), each loaded with acquire:
    /// the entry's bytes are visible once its slot is. All zero while the pool is off.
    pub fn probe(&self, key: &Key) -> [u64; WAYS] {
        let mut found = [0; WAYS];
        if !self.enabled() {
            return found;
        }
        let (set, tag) = (key.set(self.sets()), key.tag());
        for (way, f) in found.iter_mut().enumerate() {
            let s = self.slot(set, way).load(Acquire);
            if s != 0 && s & TAG_MASK == tag {
                *f = (s >> TAG_BITS) * ALIGN as u64;
            }
        }
        found
    }
    /// Copies the `n` bytes at ring `place` into `out` word by word (relaxed atomic loads: the writer may be storing).
    fn copy_out(&self, place: usize, out: &mut [u8]) {
        debug_assert!(place % 8 == 0 && out.len() % 8 == 0);
        let at = self.layout.ring_offset() + place;
        for (i, w) in out.chunks_exact_mut(8).enumerate() {
            w.copy_from_slice(&self.word(at + 8 * i).load(Relaxed).to_le_bytes());
        }
    }
    /// The entry published at `position` (a slot's), copied into `staging` and decoded from the copy: its header first,
    /// then as many bytes as its length says (refused if that exceeds `staging` or [`MAX_ENTRY_BYTES`] or the ring's
    /// end), then the acquire fence and the tail. Accepted only if the copied header repeats `position` and `position` is
    /// not below the tail, so every byte copied was written as part of the entry published there and none was
    /// overwritten during the copy.
    pub fn fetch<'s>(&self, position: u64, staging: &'s mut [u8]) -> Result<Entry<'s>, Refusal> {
        let ring = self.layout.ring_bytes as u64;
        if position < FIRST_POSITION || position % ALIGN as u64 != 0 || position >= POSITION_LIMIT {
            return Err(Refusal::Position);
        }
        let place = (position % ring) as usize;
        let cap = staging.len().min(MAX_ENTRY_BYTES) / ALIGN * ALIGN;
        if cap < HEADER_BYTES {
            return Err(Refusal::Length);
        }
        self.copy_out(place, &mut staging[..HEADER_BYTES]);
        let len = u32::from_le_bytes(staging[24..28].try_into().unwrap()) as usize;
        if len < HEADER_BYTES || len > cap || len % ALIGN != 0 || place + len > ring as usize {
            // The header may be a later entry's, torn or not: below the tail it is just overwritten.
            fence(Acquire);
            return Err(if position < self.tail() {
                Refusal::Overwritten
            } else {
                Refusal::Length
            });
        }
        self.copy_out(place + HEADER_BYTES, &mut staging[HEADER_BYTES..len]);
        fence(Acquire);
        let tail = self.tail();
        if position < tail {
            return Err(Refusal::Overwritten);
        }
        let entry = decode(&staging[..len])?;
        if entry.position != position {
            return Err(Refusal::Position);
        }
        Ok(entry)
    }
}

/// Why [`Writer::publish`] published nothing.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Skip {
    /// Switched off.
    Off,
    /// Larger than [`MAX_ENTRY_BYTES`] or than the scratch or the ring.
    TooBig,
    /// What [`decode`] would refuse.
    Malformed(Refusal),
    /// The positions a slot holds are used up.
    Exhausted,
}

/// The only writer of a pool.
pub struct Writer {
    pool: Pool,
    /// The next position to use (before any skip to the ring's start).
    next: u64,
    tail: u64,
}
impl Writer {
    /// Makes `pool` empty and on.
    ///
    /// # Safety
    /// As [`Pool::new`], writable, and the span is all zero or no reader is looking at it; at most one writer exists.
    pub unsafe fn new(pool: Pool) -> Self {
        pool.word(TAIL).store(0, Relaxed);
        pool.word(ENABLED).store(1, Release);
        Self {
            pool,
            next: FIRST_POSITION,
            tail: 0,
        }
    }
    pub fn pool(&self) -> Pool {
        self.pool
    }
    /// The tail as last stored: an entry published at a position below it has been (or may be) overwritten.
    pub fn tail(&self) -> u64 {
        self.tail
    }
    /// Switches the pool off for good: every lookup reads this word first.
    pub fn disable(&self) {
        self.pool.word(ENABLED).store(0, Release);
    }
    /// The position an entry of `n` bytes (a multiple of [`ALIGN`]) would be published at now.
    pub fn peek(&self, n: usize) -> u64 {
        let ring = self.pool.layout.ring_bytes as u64;
        let place = self.next % ring;
        if place + n as u64 > ring {
            self.next + (ring - place)
        } else {
            self.next
        }
    }
    /// Publishes `parts` (encoded in `scratch`, at least [`MAX_ENTRY_BYTES`] for any entry): returns its position.
    pub fn publish(&mut self, parts: &Parts, scratch: &mut [u8]) -> Result<u64, Skip> {
        if !self.pool.enabled() {
            return Err(Skip::Off);
        }
        let n = parts.entry_bytes().ok_or(Skip::TooBig)?;
        if n > scratch.len() || n > self.pool.layout.ring_bytes {
            return Err(Skip::TooBig);
        }
        let position = self.peek(n);
        if position + n as u64 >= POSITION_LIMIT {
            return Err(Skip::Exhausted);
        }
        parts.encode(position, scratch).map_err(Skip::Malformed)?;
        let place = self.begin(position, n);
        self.store(place, &scratch[..n]);
        self.link(&parts.key(), position);
        Ok(position)
    }
    /// The tail first (relaxed), then the release fence that orders it before every data store that follows (an entry of
    /// `n` bytes at `position` overwrites the positions `[position - ring, position + n - ring)`). Returns its place.
    fn begin(&mut self, position: u64, n: usize) -> usize {
        let ring = self.pool.layout.ring_bytes as u64;
        self.tail = (position + n as u64).saturating_sub(ring);
        self.pool.word(TAIL).store(self.tail, Relaxed);
        fence(Release);
        self.next = position + n as u64;
        (position % ring) as usize
    }
    /// The entry's bytes, word by word, into the ring at `place`.
    fn store(&self, place: usize, bytes: &[u8]) {
        let at = self.pool.layout.ring_offset() + place;
        for (i, w) in bytes.chunks_exact(8).enumerate() {
            self.pool
                .word(at + 8 * i)
                .store(u64::from_le_bytes(w.try_into().unwrap()), Relaxed);
        }
    }
    /// The slot, last, with a release store: in `key`'s set, the way holding the lowest position (an empty one first, else
    /// the oldest entry, the likeliest to be below the tail already).
    fn link(&self, key: &Key, position: u64) {
        let set = key.set(self.pool.sets());
        let way = (0..WAYS)
            .min_by_key(|&w| self.pool.slot(set, w).load(Relaxed) >> TAG_BITS)
            .unwrap();
        self.pool
            .slot(set, way)
            .store((position / ALIGN as u64) << TAG_BITS | key.tag(), Release);
    }
}

#[cfg(test)]
mod tests;
