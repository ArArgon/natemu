//! The EL0 sampler (docs/restructure/el0-sampler-plan.md section 3; the resident's `profile` feature, chunk 0): the per-vCPU
//! sample ring, its 32-byte records, and the two files the host writes from it. Everything a reader of those files needs is
//! here; the record layouts are pure functions, unit-tested on the host.
//!
//! **The ring.** Each vCPU's study part (`cache::PCID_STUDY` of its lookup area: resident VA `LOOKUP_VA + PCID_STUDY`, host
//! offset `LOOKUP_OFFSET + PCID_STUDY` in the vCPU's block of the backing) starts with a [`Head`] page, then [`CAPACITY`]
//! records of [`RECORD_BYTES`] from [`RING`] (1 MiB). In sampling builds the study part holds nothing else (the hot-block
//! table of the November experiments is gone); in other builds it is untouched. The resident is the only writer of records,
//! `write`, `lost`, `resume_time` and its counters; the host writes `bell` before the vCPU first runs, `ring_time` before each
//! ring and `read` after each drain. A record is stored before `write` is published with release ordering; the host loads
//! `write` with acquire, copies the records up to it and publishes `read` (release). When the ring is full a record is not
//! written and `lost` counts it.
//!
//! **A record** is four little-endian u64 words (see [`Sample`] for the resident's kinds, [`HostRecord`] for the host's):
//!
//! | Bytes | Field |
//! | --- | --- |
//! | 0-7 | `time`: CNTVCT (the host's `mach_absolute_time`, the same clock: the vCPUs' virtual offset is 0) |
//! | 8-15 | resident kinds: bits 0-47 `rip` (sign-extended from bit 47), 48-59 `flushes` (low 12 bits), 60-63 `flags`; host kinds: `value1` |
//! | 16-23 | resident kinds: bits 0-39 `context`, 40-55 `at`, 56-59 `place`; host kinds: bits 0-55 `value2`. Bits 60-63: `kind`, for every record |
//! | 24-27 | resident kinds: `frame`; host kinds: `value3` |
//! | 28-31 | resident kinds: `since`; host kinds: `value4` |
//!
//! **NAME.samples** (the host's `--profile NAME.samples`): a [`FILE_MAGIC`] header record (kind 0: word 0 the magic, word 1
//! [`VERSION`] | [`RECORD_BYTES`] << 32, word 2 the counter frequency in Hz, word 3 vCPUs | ring interval in us << 32), then
//! records. Each drain (every 250 ms, and once at the end) writes a [`CLOCK`] record, then for each vCPU with new records a
//! [`VCPU`] record followed by that many of its ring's records in ring order, then the host's own records made since the last
//! drain ([`HALTED`], [`MARK`], [`TOGGLE`], [`PASS`]). The end of the run adds [`COUNT`] records. Records are in time order
//! within a vCPU's batches; across batches and vCPUs a reader sorts by `time`.
//!
//! **NAME.objects** (beside it): an [`OBJECTS_MAGIC`] header (word 0 the magic, word 1 [`VERSION`] | [`OBJECT_HEADER_BYTES`]
//! << 32, words 2-3 zero), then one record per distinct admitted answer the host served in the run (from its start, whether or
//! not sampling was on), keyed by (RIP, frame0, context, hash) and written once: an [`ObjectHeader`], the object's bytes as
//! admitted and staged (a `natemu_abi::code_object` file: its `Header` gives the code, sites and relocations), the guest bytes
//! it translates, zero padding to a multiple of 8. A sample's block (`rip`, `frame`, `context`) finds its object by RIP, frame
//! (`frame0 >> 12 == frame`) and context; a block placed from a template at another RIP of the same page offset (the
//! resident's templates, the shared pool, an instance) has the same object bytes as the answer it came from, so a reader that
//! finds no exact RIP takes the object of the same frame, page offset and context. The pool's templates need no record of
//! their own: the host publishes into the pool only the answer it has just served (`Compiler::publish`), written here then.
use core::sync::atomic::{AtomicU64, Ordering::{Acquire, Release}};

/// [`Head::magic`] once a sampling resident has started: the host rings only a vCPU whose head says so.
pub const MAGIC: u64 = u64::from_le_bytes(*b"EL0SAMPL");
pub const RECORD_BYTES: usize = 32;
/// The ring's records start at this offset in the study part, after the head's page.
pub const RING: usize = 4096;
/// 1 MiB of records: 8 s at 4 kHz; the host drains every 250 ms.
pub const CAPACITY: usize = 32 * 1024;
pub const BYTES: usize = RING + CAPACITY * RECORD_BYTES;
const _: () = assert!(BYTES <= crate::execution::cache::PCID_STUDY_BYTES && size_of::<Head>() <= RING);

/// The study part's first page in a sampling resident.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Head {
    /// [`MAGIC`], written by the resident at start.
    pub magic: u64,
    /// Records written so far (record k at slot k % [`CAPACITY`]); published with release ordering after the record.
    pub write: u64,
    /// Records the host has drained (it publishes it with release ordering after copying them).
    pub read: u64,
    /// Records not written: the ring was full.
    pub lost: u64,
    /// The sample bell's INTID (`spi + MAX_VCPUS + v`), written by the host before the vCPU first runs; 0: no bell.
    pub bell: u64,
    /// CNTVCT the host stored just before its last ring of this vCPU's bell.
    pub ring_time: u64,
    /// CNTVCT `resume_exception` stored at the last return to generated code (`gates.S`, before its `eret`).
    pub resume_time: u64,
    /// Bells taken from generated code (each wrote a record or counted one lost).
    pub bells: u64,
    /// Bells taken in a wait (`lapic::wait`, the INIT and mailbox waits, `acknowledge`): no record, no wake counted.
    pub bells_in_wait: u64,
    /// Dispatcher samples whose call test is [`CALL_UNKNOWN`].
    pub unknown_calls: u64,
    /// An INTID a wait's bell test acknowledged that is not a bell, held for `acknowledge` (INTID + 1; 0: none).
    pub held: u64,
}
pub const HEAD_MAGIC: usize = core::mem::offset_of!(Head, magic);
pub const HEAD_WRITE: usize = core::mem::offset_of!(Head, write);
pub const HEAD_READ: usize = core::mem::offset_of!(Head, read);
pub const HEAD_LOST: usize = core::mem::offset_of!(Head, lost);
pub const HEAD_BELL: usize = core::mem::offset_of!(Head, bell);
pub const HEAD_RING_TIME: usize = core::mem::offset_of!(Head, ring_time);
pub const HEAD_RESUME_TIME: usize = core::mem::offset_of!(Head, resume_time);

/// Record kinds (bits 60-63 of word 2). The resident writes [`EL0`] and [`RESIDENT`]; the host the others.
pub const FILE: u8 = 0;
/// The bell was rung after generated code was last resumed: the vCPU was at `place` (word fields as [`Sample`]).
pub const EL0: u8 = 1;
/// The bell was rung before the last resume: it was pending while the vCPU was in the resident or the host; `since` is the
/// ticks from the ring to the interrupt, the other fields 0.
pub const RESIDENT: u8 = 2;
/// The host found the vCPU halted (`Events::halted` set: HLT, the mailbox or SIPI wait) and did not ring it:
/// `time` the host's look, `value1` the ringing pass's sequence number, `value3` the vCPU.
pub const HALTED: u8 = 3;
/// A mark written to the control port (F9H): `time` the host's CNTVCT at the port exit, `value1` the mark's number,
/// `value3` the vCPU that wrote it.
pub const MARK: u8 = 4;
/// A clock pair, at the start and at each drain: `time` CNTVCT, `value1` the wall clock (ns since the UNIX epoch), `value2`
/// the drain's number (0 at the start).
pub const CLOCK: u8 = 5;
/// The next `value4` records are vCPU `value3`'s ring records; `value1` its `Head::lost` then, `value2` its `Head::write`.
pub const VCPU: u8 = 6;
/// At the end: vCPU `value3`'s (u32::MAX: the run's) counter `value4` ([`COUNTERS`]) is `value1`.
pub const COUNT: u8 = 7;
/// Sampling was turned on or off: `time` the flag's store, `value1` the sequence number after it, `value2` 1 on / 0 off,
/// `value3` the cause ([`BY_START`], [`BY_PORT`], [`BY_SIGNAL`]), `value4` the writing vCPU (port) or u32::MAX.
pub const TOGGLE: u8 = 8;
/// A ringing pass that ran across a toggle, or the first after one: `time` its start, `value1` its end (CNTVCT), `value2` the
/// sequence number read at its start, `value3` the one read at its end, `value4` the bells it rang. Other passes are not
/// logged: every pass between two logged ones rang under the earlier one's end sequence number.
pub const PASS: u8 = 9;
pub const BY_START: u32 = 0;
pub const BY_PORT: u32 = 1;
pub const BY_SIGNAL: u32 = 2;

/// [`COUNT`] counters.
pub const C_RINGS: u32 = 0;
pub const C_HALTED: u32 = 1;
pub const C_WRITTEN: u32 = 2;
pub const C_LOST: u32 = 3;
pub const C_BELLS: u32 = 4;
pub const C_BELLS_IN_WAIT: u32 = 5;
pub const C_UNKNOWN_CALLS: u32 = 6;
pub const C_DRAINED: u32 = 7;
/// The run's (vCPU u32::MAX): every access to the control port, and those whose value was ignored (another value at F8H,
/// a write at FAH-FBH; reads included in the first only).
pub const C_PORT_ACCESSES: u32 = 8;
pub const C_PORT_IGNORED: u32 = 9;
/// vCPUs the host never rang: their resident does not sample (no [`MAGIC`]).
pub const C_NOT_SAMPLING: u32 = 10;
pub const COUNTERS: [&str; 11] = [
    "rings", "halted", "written", "lost", "bells", "bells_in_wait", "unknown_calls", "drained", "port_accesses",
    "port_ignored", "not_sampling",
];

/// Where an [`EL0`] sample's PC was (bits 56-59 of word 2), and what `at` and `frame` hold there.
/// In a block's code: `at` the native offset (i16, from the block's code: the object's code byte 0; a [`crate::execution::cache::PIC`]
/// block's full entry is at -4), `frame` its first source frame (`source_gpa[0] >> 12`).
pub const BLOCK: u8 = 1;
/// In a shared probe of a page-crossing store: charged to the store's block (its fields, as [`BLOCK`]), `at` the native
/// offset its probe returns to.
pub const PROBE: u8 = 2;
/// In the dispatcher or a dispatcher slot: `rip` the target (x0), `at` the PC's offset from `cache::DISPATCH`, `context`
/// the slot's context and `frame` the target block's first source frame when the slot has a context and a current block
/// holds the target (else 0); the call test in `flags`.
pub const DISPATCH: u8 = 3;
/// In the single-step slot's block (fields as [`BLOCK`]).
pub const STEP: u8 = 4;
/// In the code area's head but none of the above, in no block, or outside the code area: `frame` the PC's offset from
/// `CODE_VA` (low 32 bits), the rest 0.
pub const HEAD: u8 = 5;
pub const NO_BLOCK: u8 = 6;
pub const OUTSIDE: u8 = 7;

/// `flags`: the PC was in the block's hot copy; the block was dead (its code still ran).
pub const HOT: u8 = 1;
pub const DEAD: u8 = 2;
/// `flags` bits 2-3 of a [`DISPATCH`] sample: the bytes before the target end a call (the target is a return address),
/// they do not, or the test could not be made (no current block for the target, the bytes cross into another page, no
/// RAM there).
pub const CALL_SHIFT: u32 = 2;
pub const CALL_YES: u8 = 1;
pub const CALL_NO: u8 = 2;
pub const CALL_UNKNOWN: u8 = 3;

/// A resident record ([`EL0`], [`RESIDENT`]), decoded.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Sample {
    /// The host's ring time ([`Head::ring_time`] read at the interrupt).
    pub time: u64,
    pub rip: u64,
    /// The vCPU's cache flush count (`Control::flushes`), low 12 bits.
    pub flushes: u16,
    pub flags: u8,
    /// The block's code context (`Cpu::code_context`), 40 bits.
    pub context: u64,
    pub at: u16,
    pub place: u8,
    pub kind: u8,
    pub frame: u32,
    /// [`EL0`]: ticks from the last resume to the interrupt; [`RESIDENT`]: from the ring to the interrupt (saturated).
    pub since: u32,
}
pub const CONTEXT_BITS: u32 = 40;
const RIP_BITS: u32 = 48;
impl Sample {
    pub const fn encode(&self) -> [u64; 4] {
        [
            self.time,
            self.rip & ((1 << RIP_BITS) - 1) | (self.flushes as u64 & 0xfff) << 48 | (self.flags as u64 & 0xf) << 60,
            self.context & ((1 << CONTEXT_BITS) - 1)
                | (self.at as u64) << 40
                | (self.place as u64 & 0xf) << 56
                | (self.kind as u64 & 0xf) << 60,
            self.frame as u64 | (self.since as u64) << 32,
        ]
    }
    pub const fn decode(w: [u64; 4]) -> Self {
        Sample {
            time: w[0],
            rip: ((w[1] << (64 - RIP_BITS)) as i64 >> (64 - RIP_BITS)) as u64,
            flushes: (w[1] >> 48 & 0xfff) as u16,
            flags: (w[1] >> 60) as u8,
            context: w[2] & ((1 << CONTEXT_BITS) - 1),
            at: (w[2] >> 40) as u16,
            place: (w[2] >> 56 & 0xf) as u8,
            kind: (w[2] >> 60) as u8,
            frame: w[3] as u32,
            since: (w[3] >> 32) as u32,
        }
    }
}
/// A host record (kinds [`HALTED`] and up), decoded.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct HostRecord {
    pub kind: u8,
    pub time: u64,
    pub value1: u64,
    /// 56 bits.
    pub value2: u64,
    pub value3: u32,
    pub value4: u32,
}
impl HostRecord {
    pub const fn encode(&self) -> [u64; 4] {
        [
            self.time,
            self.value1,
            self.value2 & ((1 << 56) - 1) | (self.kind as u64 & 0xf) << 60,
            self.value3 as u64 | (self.value4 as u64) << 32,
        ]
    }
    pub const fn decode(w: [u64; 4]) -> Self {
        HostRecord {
            kind: (w[2] >> 60) as u8,
            time: w[0],
            value1: w[1],
            value2: w[2] & ((1 << 56) - 1),
            value3: w[3] as u32,
            value4: (w[3] >> 32) as u32,
        }
    }
}
/// The kind of any record (bits 60-63 of word 2).
pub const fn kind(w: &[u64; 4]) -> u8 {
    (w[2] >> 60) as u8
}
pub fn to_bytes(w: &[u64; 4]) -> [u8; RECORD_BYTES] {
    let mut b = [0; RECORD_BYTES];
    for (k, x) in w.iter().enumerate() {
        b[8 * k..8 * k + 8].copy_from_slice(&x.to_le_bytes());
    }
    b
}

pub const FILE_MAGIC: u64 = u64::from_le_bytes(*b"NATEMUSA");
pub const OBJECTS_MAGIC: u64 = u64::from_le_bytes(*b"NATEMUOB");
pub const VERSION: u64 = 1;
/// The samples file's first record.
pub const fn file_header(frequency: u64, vcpus: u32, interval_us: u32) -> [u64; 4] {
    [FILE_MAGIC, VERSION | (RECORD_BYTES as u64) << 32, frequency, vcpus as u64 | (interval_us as u64) << 32]
}

/// An objects-file record's header, then the object's bytes, the guest bytes and padding to a multiple of 8.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ObjectHeader {
    /// The whole record's bytes (this header included, padded).
    pub length: u32,
    /// [`SERVED`] or [`SERVED_STEP`].
    pub source: u8,
    /// The vCPU it was first served to.
    pub vcpu: u8,
    pub reserved: u16,
    pub rip: u64,
    /// The guest-physical page of `rip` the host copied the bytes from (`State::fetch[0]`), and the next page's when the
    /// bytes cross into it (`u64::MAX`: none).
    pub frame0: u64,
    pub frame1: u64,
    pub context: u64,
    /// [`hash`] of the object's bytes.
    pub hash: u64,
    pub object_bytes: u32,
    pub guest_bytes: u32,
}
pub const OBJECT_HEADER_BYTES: usize = size_of::<ObjectHeader>();
const _: () = assert!(OBJECT_HEADER_BYTES == 56);
pub const SERVED: u8 = 1;
/// A single-step answer (one instruction, placed in the step slot).
pub const SERVED_STEP: u8 = 2;
/// FNV-1a, 64 bits.
pub const fn hash(bytes: &[u8]) -> u64 {
    let mut h = 0xcbf2_9ce4_8422_2325u64;
    let mut i = 0;
    while i < bytes.len() {
        h = (h ^ bytes[i] as u64).wrapping_mul(0x0100_0000_01b3);
        i += 1;
    }
    h
}

/// Bytes before a dispatcher target the call test reads ([`call_before`]): the longest x86 instruction.
pub const BEFORE: usize = 15;
/// The bytes before a return target end a near call: `E8 rel32`, or `FF /2` in its register and memory forms (ModRM, SIB,
/// disp8 or disp32; a REX prefix before the `FF` changes nothing at the end). `b` ends just before the target. Under 64-bit
/// and 32-bit addressing the lengths are the same; 16-bit addressing (`67` in 32-bit code) is not looked for.
pub const fn call_before(b: &[u8; BEFORE]) -> bool {
    if b[BEFORE - 5] == 0xe8 {
        return true;
    }
    let lengths = [2usize, 3, 4, 6, 7];
    let mut k = 0;
    while k < lengths.len() {
        let n = lengths[k];
        let at = BEFORE - n;
        let sib = if at + 2 < BEFORE { b[at + 2] } else { 0 };
        if b[at] == 0xff && b[at + 1] >> 3 & 7 == 2 && ff_length(b[at + 1], sib) == n {
            return true;
        }
        k += 1;
    }
    false
}
/// The length of `FF modrm ...` from its ModRM and the byte after it (its SIB when there is one).
const fn ff_length(modrm: u8, sib: u8) -> usize {
    let (md, rm) = (modrm >> 6, modrm & 7);
    if md == 3 {
        return 2;
    }
    let sib_bytes = if rm == 4 { 1 } else { 0 };
    let disp = match md {
        0 if rm == 5 => 4,
        0 if rm == 4 && sib & 7 == 5 => 4,
        0 => 0,
        1 => 1,
        _ => 4,
    };
    2 + sib_bytes + disp
}
/// The call test of a target at `rip` whose page's guest bytes `page` gives (its first source frame's page), or
/// [`CALL_UNKNOWN`] when the bytes before it lie in the page before.
pub fn call_test(rip: u64, page: Option<&[u8]>) -> u8 {
    let o = (rip & 0xfff) as usize;
    match page {
        Some(p) if o >= BEFORE && p.len() >= o => {
            let mut b = [0; BEFORE];
            b.copy_from_slice(&p[o - BEFORE..o]);
            if call_before(&b) { CALL_YES } else { CALL_NO }
        }
        _ => CALL_UNKNOWN,
    }
}

/// A ring at `base` (its [`Head`], then its records): the resident's writer side and the host's reader side.
pub struct Ring {
    base: *mut u8,
}
impl Ring {
    /// # Safety
    /// `base` points at [`BYTES`] bytes, 8-aligned, valid while the value is used; one writer and one reader.
    pub const unsafe fn at(base: *mut u8) -> Self {
        Ring { base }
    }
    fn word(&self, offset: usize) -> &AtomicU64 {
        // SAFETY: inside the head (`at`'s contract), aligned.
        unsafe { AtomicU64::from_ptr(self.base.add(offset).cast()) }
    }
    fn slot(&self, k: u64) -> *mut u64 {
        // SAFETY: inside the ring.
        unsafe { self.base.add(RING + (k % CAPACITY as u64) as usize * RECORD_BYTES).cast() }
    }
    /// The writer: stores `r` unless the ring is full (then counts it in `lost`). Whether it was stored.
    pub fn push(&self, r: [u64; 4]) -> bool {
        let w = self.word(HEAD_WRITE).load(core::sync::atomic::Ordering::Relaxed);
        if w.wrapping_sub(self.word(HEAD_READ).load(Acquire)) >= CAPACITY as u64 {
            let lost = self.word(HEAD_LOST);
            lost.store(lost.load(core::sync::atomic::Ordering::Relaxed) + 1, core::sync::atomic::Ordering::Relaxed);
            return false;
        }
        let p = self.slot(w);
        for (k, x) in r.into_iter().enumerate() {
            // SAFETY: the slot's words; the reader reads them only after the release below.
            unsafe { p.add(k).write_volatile(x) };
        }
        self.word(HEAD_WRITE).store(w + 1, Release);
        true
    }
    /// The reader: every record written since the last drain, in order, to `each`; how many.
    pub fn drain(&self, mut each: impl FnMut([u64; 4])) -> u64 {
        let (r, w) = (self.word(HEAD_READ).load(core::sync::atomic::Ordering::Relaxed), self.word(HEAD_WRITE).load(Acquire));
        for k in r..w {
            let p = self.slot(k);
            // SAFETY: published by the writer's release, not reused until `read` passes it.
            each(core::array::from_fn(|i| unsafe { p.add(i).read_volatile() }));
        }
        self.word(HEAD_READ).store(w, Release);
        w - r
    }
    pub fn head(&self) -> Head {
        // SAFETY: the head's words, each read on its own.
        unsafe { self.base.cast::<Head>().read_volatile() }
    }
}

#[cfg(test)]
mod tests {
    extern crate std;
    use super::*;
    use std::vec::Vec;

    /// Every field of a resident record survives encode and decode at its limits (a kernel RIP sign-extended from bit 47, a
    /// 40-bit context, the 12-bit flush count wrapping), and the kind sits where host records keep theirs.
    #[test]
    fn records_encode_and_decode() {
        let s = Sample {
            time: u64::MAX - 5,
            rip: 0xffff_ffff_8100_1234,
            flushes: 0xabc,
            flags: HOT | CALL_UNKNOWN << CALL_SHIFT,
            context: (1 << 39) | 0x3_0000_0005,
            at: 0xfffc,
            place: DISPATCH,
            kind: EL0,
            frame: 0x89ab_cdef,
            since: 77,
        };
        let w = s.encode();
        assert_eq!(Sample::decode(w), s);
        assert_eq!(kind(&w), EL0);
        assert_eq!(to_bytes(&w)[23] >> 4, EL0, "the kind is the top nibble of byte 23");
        // A user RIP stays positive; flushes keep their low 12 bits only.
        let u = Sample { rip: 0x7fff_ffff_f000, flushes: 0x1fff & 0xfff, ..s };
        assert_eq!(Sample::decode(u.encode()).rip, 0x7fff_ffff_f000);
        assert_eq!(Sample::decode(Sample { flushes: 0x1001, ..s }.encode()).flushes, 1);
        let h = HostRecord { kind: MARK, time: 9, value1: 255, value2: (1 << 56) - 1, value3: 3, value4: u32::MAX };
        assert_eq!(HostRecord::decode(h.encode()), h);
        assert_eq!(kind(&h.encode()), MARK);
        assert_eq!(kind(&file_header(24_000_000, 4, 250)), FILE);
        assert_eq!(file_header(24_000_000, 4, 250)[0].to_le_bytes(), *b"NATEMUSA");
    }

    /// Records in order across the ring's wrap; a full ring counts each refused record lost and keeps the older ones; a
    /// drain frees room again.
    #[test]
    fn the_ring_wraps_and_counts_what_it_loses() {
        let mut memory = std::vec![0u64; BYTES / 8];
        let ring = unsafe { Ring::at(memory.as_mut_ptr().cast()) };
        let rec = |k: u64| [k, k + 1, k + 2, k + 3];
        let mut got = Vec::new();
        // Three quarters, drained; then the rest of a lap and more: the slots wrap.
        for k in 0..(3 * CAPACITY / 4) as u64 {
            assert!(ring.push(rec(k)));
        }
        assert_eq!(ring.drain(|r| got.push(r)), (3 * CAPACITY / 4) as u64);
        for k in (3 * CAPACITY / 4) as u64..(3 * CAPACITY / 4 + CAPACITY) as u64 {
            assert!(ring.push(rec(k)), "{k}");
        }
        // Full: refused and counted.
        assert!(!ring.push(rec(1 << 40)));
        assert!(!ring.push(rec(1 << 41)));
        assert_eq!(ring.head().lost, 2);
        assert_eq!(ring.drain(|r| got.push(r)), CAPACITY as u64);
        assert_eq!(got.len(), 3 * CAPACITY / 4 + CAPACITY);
        assert!(got.iter().enumerate().all(|(k, r)| *r == rec(k as u64)), "in order, nothing overwritten");
        assert!(ring.push(rec(5)));
        assert_eq!(ring.head().write, (3 * CAPACITY / 4 + CAPACITY + 1) as u64);
        assert_eq!(ring.drain(|_| {}), 1);
    }

    /// The call test on synthetic bytes: each call form ends a call (E8 rel32; FF /2 with a register, through a register,
    /// with a SIB, disp8, disp32, RIP-relative, SIB with no base, a REX before it), and the look-alikes do not (FF /4 is a
    /// jmp, an E8 that is not 5 bytes back, FF /2 whose length does not end at the target).
    #[test]
    fn bytes_before_a_target_end_a_call() {
        let ends = |tail: &[u8]| {
            let mut b = [0x90u8; BEFORE];
            b[BEFORE - tail.len()..].copy_from_slice(tail);
            call_before(&b)
        };
        for call in [
            &[0xe8, 1, 2, 3, 4][..],
            &[0xff, 0xd0],                         // call rax
            &[0x41, 0xff, 0xd3],                   // call r11
            &[0xff, 0x10],                         // call [rax]
            &[0xff, 0x14, 0x24],                   // call [rsp]
            &[0xff, 0x50, 0x08],                   // call [rax+8]
            &[0xff, 0x54, 0x24, 0x08],             // call [rsp+8]
            &[0xff, 0x90, 1, 2, 3, 4],             // call [rax+disp32]
            &[0xff, 0x15, 1, 2, 3, 4],             // call [rip+disp32]
            &[0xff, 0x14, 0xc5, 1, 2, 3, 4],       // call [rax*8+disp32]
            &[0xff, 0x94, 0xc8, 1, 2, 3, 4],       // call [rax+rcx*8+disp32]
        ] {
            assert!(ends(call), "{call:x?}");
        }
        for other in [
            &[0xe8, 1, 2, 3][..],                  // an E8 four bytes back
            &[0xff, 0xe0],                         // jmp rax
            &[0xff, 0x20],                         // jmp [rax]
            &[0xff, 0x50],                         // call [rax+disp8] cut short
            &[0xff, 0x15, 1, 2, 3],                // rip-relative cut short
            &[0xc3],
        ] {
            assert!(!ends(other), "{other:x?}");
        }
        // The test on a page: a target fewer than 15 bytes into its page, or no RAM there, is unknown.
        let mut page = std::vec![0x90u8; 4096];
        page[0x100 - 5] = 0xe8;
        assert_eq!(call_test(0x7000_0100, Some(&page)), CALL_YES);
        assert_eq!(call_test(0x7000_0200, Some(&page)), CALL_NO);
        assert_eq!(call_test(0x7000_000e, Some(&page)), CALL_UNKNOWN);
        assert_eq!(call_test(0x7000_000f, Some(&page)), CALL_NO);
        assert_eq!(call_test(0x7000_0100, None), CALL_UNKNOWN);
    }

    #[test]
    fn hashes_are_fnv1a() {
        assert_eq!(hash(b""), 0xcbf2_9ce4_8422_2325);
        assert_eq!(hash(b"a"), 0xaf63_dc4c_8601_ec8c);
    }
}
