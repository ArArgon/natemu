//! The EL0 sampler (docs/restructure/el0-sampler-plan.md section 3; the resident's `profile` feature, chunk 0): the per-vCPU
//! sample ring, its 32-byte records, and the two files the host writes from it. Everything a reader of those files needs is
//! here; the record layouts are pure functions, unit-tested on the host.
//!
//! **The ring.** Each vCPU's study part (`cache::PCID_STUDY` of its lookup area: resident VA `LOOKUP_VA + PCID_STUDY`, host
//! offset `LOOKUP_OFFSET + PCID_STUDY` in the vCPU's block of the backing) starts with a [`Head`] page, then [`CAPACITY`]
//! records of [`RECORD_BYTES`] from [`RING`] (1 MiB). In sampling builds the study part holds nothing else (the hot-block
//! table of the November experiments is gone); in other builds it is untouched. The resident is the only writer of records,
//! `write`, `lost`, `resume_time`, `trips`, `stretches` and its counters; the host writes `bell` before the vCPU first runs, `ring_time` before each
//! ring and `read` after each drain. A record is stored before `write` is published with release ordering; the host loads
//! `write` with acquire, copies the records up to it and publishes `read` (release). When the ring is full a record is not
//! written and `lost` counts it.
//!
//! **What a sample's time can and cannot say.** `ring_time` is one word per vCPU: a bell still pending when the next pass
//! stores its own ring time (the vCPU stayed in the resident or the host longer than an interval, the two rings merging on
//! the edge-triggered SPI) is recorded with the later time, so its `since` is understated, and when that later store falls
//! after the vCPU's resume a bell pending through the resident is placed at EL0 on the resume PC (a window of about one
//! interval after a long stay outside generated code). The host counts the bells it rings ([`C_RINGS`]); a ring to a vCPU
//! that has ended without `Events::halted` set (its run over, the others still running) is counted but never taken, so the
//! rings and the bells taken ([`C_BELLS`], [`C_BELLS_IN_WAIT`]) are compared as a bound, never exactly.
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
    /// Trips out of generated code so far ([`Ring::left`]).
    pub trips: u64,
    /// The last [`STRETCHES`] stretches of generated code, trip k's at `k % STRETCHES`: when it was resumed
    /// (`resume_time`, as the trip out found it) and when it was left (CNTVCT at the start of `finish`, the first Rust the
    /// gates call: after the gate's register save, a few tens of nanoseconds after the exception). A bell taken from
    /// generated code finds its ring time among them ([`Stretches::class`]).
    pub stretches: [[u64; 2]; STRETCHES],
}
/// Stretches of generated code [`Head::stretches`] keeps: the bell is taken 1.5-2 us after its ring, and between them a vCPU
/// can make several trips (about 2.5 us apart on average in a configure step, much closer in bursts).
pub const STRETCHES: usize = 16;
pub const HEAD_MAGIC: usize = core::mem::offset_of!(Head, magic);
pub const HEAD_WRITE: usize = core::mem::offset_of!(Head, write);
pub const HEAD_READ: usize = core::mem::offset_of!(Head, read);
pub const HEAD_LOST: usize = core::mem::offset_of!(Head, lost);
pub const HEAD_BELL: usize = core::mem::offset_of!(Head, bell);
pub const HEAD_RING_TIME: usize = core::mem::offset_of!(Head, ring_time);
pub const HEAD_RESUME_TIME: usize = core::mem::offset_of!(Head, resume_time);
pub const HEAD_TRIPS: usize = core::mem::offset_of!(Head, trips);
pub const HEAD_STRETCHES: usize = core::mem::offset_of!(Head, stretches);

/// Record kinds (bits 60-63 of word 2). The resident writes [`EL0`], [`RESIDENT`] and [`EL0_UNKNOWN`]; the host the others.
pub const FILE: u8 = 0;
/// The bell was rung after generated code was last resumed: the vCPU was at `place` (word fields as [`Sample`]).
pub const EL0: u8 = 1;
/// The bell was rung before the last resume, between two stretches of generated code: it was pending while the vCPU was in
/// the resident or the host; `since` is the ticks from the ring to the interrupt, `flags` [`OLD`] when the ring is older
/// than every stretch kept (where the vCPU was is not known; counted here, as version 1 did), the other fields 0.
pub const RESIDENT: u8 = 2;
/// `flags` of a [`RESIDENT`] sample: rung before the oldest of the [`STRETCHES`] stretches kept.
pub const OLD: u8 = 1;
/// The host found the vCPU halted (`Events::halted` set: HLT, the mailbox or SIPI wait) and did not ring it:
/// `time` the host's look, `value1` the ringing pass's sequence number, `value3` the vCPU.
pub const HALTED: u8 = 3;
/// A mark written to the control port (F9H): `time` the host's CNTVCT at the port exit, `value1` the mark's number,
/// `value3` the vCPU that wrote it.
pub const MARK: u8 = 4;
/// A clock pair, at the start and at each drain: `time` CNTVCT, `value1` the wall clock (ns since the UNIX epoch), `value2`
/// the drain's number (0 at the start).
pub const CLOCK: u8 = 5;
/// The next `value4` records are vCPU `value3`'s ring records; `value1` its `Head::lost`, `value2` its `Head::write`, both
/// read after the drain: `write` may already be ahead of the records drained (the resident kept writing), and `lost` counts
/// up to then.
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
/// The bell was rung in an earlier stretch of generated code than the one it interrupted (version 2): the vCPU was in
/// generated code then, but left it before the interrupt was taken (the bell arrives 1.5-2 us after the ring), so where it
/// was is not known: the PC the interrupt saw is a later stretch's. EL0 time with no place: `since` the ticks from the ring
/// to the interrupt, `frame` from the ring to the end of its stretch (saturated), `at` how many stretches before the
/// interrupted one, the other fields 0.
pub const EL0_UNKNOWN: u8 = 10;
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
/// In a block's code: `at` the native offset plus [`AT_BIAS`] (the offset from the block's code, the object's code byte 0; a
/// [`crate::execution::cache::PIC`] block's full entry word is at -4, so `at` 0), saturated at `u16::MAX` with [`FAR`] set
/// outside that range; `frame` its first source frame (`source_gpa[0] >> 12`).
pub const BLOCK: u8 = 1;
/// In a shared probe of a page-crossing store: charged to the store's block (its fields, as [`BLOCK`]), `at` the native
/// offset its probe returns to (as [`BLOCK`]'s).
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
/// Indirect plan step 1: in the words the resident appended after a block's code for one exit (`cache::Block::tail`): `rip`
/// the target (x0), `context` and `frame` the block's (its first source frame), `at` the word's role ([`TAIL_WORD`] bits:
/// 0 the `bl` to the lookup form, 1 the `br`/`blr`, 2 the pad; [`TAIL_CALL`]: the exit is a call; [`TAIL_POINT`]: a
/// link point's, a checked link of shared code, not the computed exit's), [`DEAD`] in `flags` for a dead block's.
pub const TAIL: u8 = 8;
pub const TAIL_WORD: u16 = 3;
pub const TAIL_CALL: u16 = 4;
pub const TAIL_POINT: u16 = 8;
/// Return plan: in a block's call pair (`cache::Block::pair`): word 0 its `bl`, 1 its pad, with [`TAIL_CALL`]; `rip` is x0
/// (at the pad, the return address).
pub const TAIL_PAIR: u16 = 16;

/// `flags` of a sample in a block ([`BLOCK`], [`PROBE`], [`STEP`]): the PC was in the block's hot copy; the block was dead
/// (its code still ran); its native offset did not fit `at` (saturated).
pub const HOT: u8 = 1;
pub const DEAD: u8 = 2;
pub const FAR: u8 = 4;
/// `at` of a block's word: its native offset + 4.
pub const AT_BIAS: i64 = 4;
/// `at` and the [`FAR`] flag of native offset `offset`.
pub const fn at(offset: i64) -> (u16, u8) {
    let x = offset.saturating_add(AT_BIAS);
    if x >= 0 && x < u16::MAX as i64 {
        (x as u16, 0)
    } else if x < 0 {
        (0, FAR)
    } else {
        (u16::MAX, FAR)
    }
}
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
    /// [`EL0`]: ticks from the last resume to the interrupt; [`RESIDENT`], [`EL0_UNKNOWN`]: from the ring to the interrupt
    /// (saturated).
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
/// 2: the resident writes [`EL0_UNKNOWN`] and [`OLD`] (version 1 counted every sample rung before the last resume
/// [`RESIDENT`]); the layout is unchanged.
pub const VERSION: u64 = 2;
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
/// The call test of a dispatcher target at `rip` whose first source page is the guest-physical page `page`: the bytes
/// before it, read by `read(gpa, out)` (the resident: through its RAM alias, never a guest walk, which sets A bits and can
/// fault), or [`CALL_UNKNOWN`] when they lie in the page before or cannot be read. The resident's one implementation.
pub fn call_test(page: u64, rip: u64, read: impl FnOnce(u64, &mut [u8; BEFORE]) -> bool) -> u8 {
    let o = rip & 0xfff;
    if o < BEFORE as u64 {
        return CALL_UNKNOWN;
    }
    let mut b = [0; BEFORE];
    if !read(page & !0xfff | (o - BEFORE as u64), &mut b) {
        return CALL_UNKNOWN;
    }
    if call_before(&b) { CALL_YES } else { CALL_NO }
}

/// What the resident knows of the block holding a PC (`cache::block_at`, `cache::native_offset`).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct BlockView {
    pub rip: u64,
    pub context: u64,
    /// Its first source page (`source_gpa[0]`).
    pub frame: u64,
    /// The PC's native offset in its whole code (from its code's first byte: -4 is a PIC block's full entry).
    pub offset: i64,
    pub hot: bool,
    pub step: bool,
    pub dead: bool,
}
/// The resident's code cache and guest RAM as [`record`] asks them.
pub trait Places {
    /// The block whose code holds `pc` (the step slot's block included), dead ones too.
    fn block(&self, pc: u64) -> Option<BlockView>;
    /// The block whose appended words hold `pc` (dead ones too; `offset` unused) and the word's role ([`TAIL`]'s `at`).
    fn tail(&self, pc: u64) -> Option<(BlockView, u16)>;
    /// Dispatcher slot `k`'s code context (`Control::slot_contexts`; `cache::NO_CONTEXT`: none).
    fn slot_context(&self, k: usize) -> u64;
    /// The first source page of the block current for (`rip`, `context`), found with no walk (`cache::current_at`).
    fn target(&self, rip: u64, context: u64) -> Option<u64>;
    /// [`BEFORE`] bytes of guest RAM at `gpa` (false: not RAM).
    fn read(&self, gpa: u64, out: &mut [u8; BEFORE]) -> bool;
}
/// The vCPU's last stretches of generated code ([`Head::trips`], [`Head::stretches`]), as a bell reads them.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stretches {
    pub trips: u64,
    pub at: [[u64; 2]; STRETCHES],
}
/// Where a ring time falls ([`Stretches::class`]).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Class {
    /// In the stretch the bell interrupted (its resume time).
    Here(u64),
    /// Between two stretches; `old`: before every stretch kept.
    Outside { old: bool },
    /// In the stretch `back` stretches before the interrupted one, `to_end` ticks before it ended.
    Earlier { back: u64, to_end: u64 },
}
impl Stretches {
    /// The stretch or the gap a ring at `rung` fell in. The last trip recorded is the bell's own: its stretch is the one the
    /// bell interrupted. A ring at a resume is in that stretch, at an exit outside it; before the first stretch there was
    /// none (outside, not old).
    pub fn class(&self, rung: u64) -> Class {
        let n = self.trips;
        if n == 0 {
            return Class::Outside { old: true };
        }
        let at = |k: u64| self.at[(k % STRETCHES as u64) as usize];
        let resumed = at(n - 1)[0];
        if rung >= resumed {
            return Class::Here(resumed);
        }
        let mut k = n - 1; // rung < stretch k's resume
        loop {
            if k == 0 {
                return Class::Outside { old: false };
            }
            if n - k >= STRETCHES as u64 {
                return Class::Outside { old: true };
            }
            k -= 1;
            let [r, e] = at(k);
            if rung >= e {
                return Class::Outside { old: false };
            }
            if rung >= r {
                return Class::Earlier { back: n - 1 - k, to_end: e - rung };
            }
        }
    }
}
/// The record of a bell taken from generated code before `elr` at CNTVCT `t0` (x0 and d0 its frame's), the host having rung
/// at `rung`, the vCPU's last stretches of generated code `st` (the bell's own trip out last); `flushes` the vCPU's cache
/// flush count. Rung in the stretch it interrupted, it says where generated code was ([`EL0`]): a block (a shared probe
/// charged to its store's), the dispatcher (its target and the call test), or elsewhere in the code area. Rung between two
/// stretches, it was pending while the vCPU was outside generated code ([`RESIDENT`]). Rung in an earlier stretch, the vCPU
/// was in generated code, at a place this interrupt cannot see ([`EL0_UNKNOWN`]).
pub fn record(rung: u64, st: &Stretches, t0: u64, flushes: u64, elr: u64, x0: u64, d0: u64, p: &impl Places) -> Sample {
    use crate::execution::cache::{
        CODE_BYTES_MAX, CODE_VA, DISPATCH_BYTES, DISPATCH_SLOT_BYTES, DISPATCH_VA, HEAD_BYTES, NO_CONTEXT,
    };
    let since = |from: u64| t0.saturating_sub(from).min(u32::MAX as u64) as u32;
    let mut s = Sample { time: rung, ..Sample::default() };
    let resumed = match st.class(rung) {
        Class::Here(resumed) => resumed,
        Class::Outside { old } => {
            (s.kind, s.since, s.flags) = (RESIDENT, since(rung), if old { OLD } else { 0 });
            return s;
        }
        Class::Earlier { back, to_end } => {
            s.kind = EL0_UNKNOWN;
            (s.since, s.frame, s.at) = (since(rung), to_end.min(u32::MAX as u64) as u32, back.min(u16::MAX as u64) as u16);
            return s;
        }
    };
    (s.kind, s.since, s.flushes) = (EL0, since(resumed), (flushes & 0xfff) as u16);
    let x = elr.wrapping_sub(CODE_VA);
    if x >= CODE_BYTES_MAX as u64 {
        (s.place, s.frame) = (OUTSIDE, x as u32);
        return s;
    }
    if (DISPATCH_VA..DISPATCH_VA + DISPATCH_BYTES as u64).contains(&elr) {
        (s.place, s.at, s.rip) = (DISPATCH, (elr - DISPATCH_VA) as u16, x0);
        // Slot 0 (the dispatcher itself) serves every context without a slot: the target's context is not known there.
        let k = (elr - DISPATCH_VA) as usize / DISPATCH_SLOT_BYTES;
        let context = if k == 0 { NO_CONTEXT } else { p.slot_context(k) };
        let page = if context == NO_CONTEXT { None } else { p.target(x0, context) };
        let call = match page {
            Some(page) => {
                (s.context, s.frame) = (context, (page >> 12) as u32);
                call_test(page, x0, |gpa, out| p.read(gpa, out))
            }
            None => CALL_UNKNOWN,
        };
        s.flags = call << CALL_SHIFT;
        return s;
    }
    if let Some((b, role)) = p.tail(elr) {
        (s.place, s.rip, s.context, s.frame, s.at) = (TAIL, x0, b.context, (b.frame >> 12) as u32, role);
        s.flags = if b.dead { DEAD } else { 0 };
        return s;
    }
    let probe = crate::execution::probe::at(elr).is_some();
    let pc = crate::execution::probe::returns_to(elr, x0, d0);
    let Some(b) = p.block(pc) else {
        (s.place, s.frame) = (if x < HEAD_BYTES as u64 { HEAD } else { NO_BLOCK }, x as u32);
        return s;
    };
    s.place = if probe {
        PROBE
    } else if b.step {
        STEP
    } else {
        BLOCK
    };
    (s.rip, s.context, s.frame) = (b.rip, b.context, (b.frame >> 12) as u32);
    let (at, far) = at(b.offset);
    s.at = at;
    s.flags = far | if b.hot { HOT } else { 0 } | if b.dead { DEAD } else { 0 };
    s
}

/// The GIC CPU interface as the bell protocol uses it (the resident: ISR_EL1.I, ICC_IAR1_EL1, ICC_EOIR1_EL1).
pub trait Gic {
    /// An interrupt is signalled (ISR_EL1.I).
    fn signalled(&mut self) -> bool;
    /// Acknowledges the highest pending one: its INTID (1020-1023: spurious).
    fn acknowledge(&mut self) -> u64;
    /// Ends `intid`.
    fn end(&mut self, intid: u64);
}
/// The vCPU's bell state ([`Head::bell`], [`Head::held`], [`Head::bells_in_wait`]).
pub trait Bells {
    /// The bell's INTID (0: none).
    fn bell(&self) -> u64;
    /// Takes the INTID held for [`next`], if any.
    fn held(&mut self) -> Option<u64>;
    fn hold(&mut self, intid: u64);
    /// A bell taken in a wait.
    fn in_wait(&mut self);
}
/// `intid` is the sample bell `bell` (0: none).
#[inline(always)]
pub const fn is_bell(bell: u64, intid: u64) -> bool {
    bell != 0 && intid == bell
}
/// In a wait, with an interrupt signalled: ends every pending sample bell (counted, no wake). True when nothing else was
/// pending; false when another interrupt was, acknowledged here and held for [`next`] (while it is active no interrupt of
/// its priority is signalled, so [`next`] takes it first).
pub fn bells_only(g: &mut impl Gic, b: &mut impl Bells) -> bool {
    loop {
        if !g.signalled() {
            return true;
        }
        let intid = g.acknowledge();
        if intid >= 1020 {
            return true;
        }
        if !is_bell(b.bell(), intid) {
            b.hold(intid);
            return false;
        }
        g.end(intid);
        b.in_wait();
    }
}
/// The next interrupt `acknowledge` serves (the caller ends it): a held one first, else one acknowledged now; sample bells
/// on the way are ended and counted as taken in a wait. `None` when nothing (or only a spurious INTID) is pending.
#[inline(always)]
pub fn next(g: &mut impl Gic, b: &mut impl Bells) -> Option<u64> {
    loop {
        let intid = match b.held() {
            Some(intid) => intid,
            None => {
                if !g.signalled() {
                    return None;
                }
                g.acknowledge()
            }
        };
        if intid >= 1020 {
            return None;
        }
        if is_bell(b.bell(), intid) {
            g.end(intid);
            b.in_wait();
            continue;
        }
        return Some(intid);
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
    /// The head, word by word (each an atomic load: the resident and the host write its words while it is read).
    pub fn head(&self) -> Head {
        let w = |offset: usize| self.word(offset).load(Acquire);
        Head {
            magic: w(HEAD_MAGIC),
            write: w(HEAD_WRITE),
            read: w(HEAD_READ),
            lost: w(HEAD_LOST),
            bell: w(HEAD_BELL),
            ring_time: w(HEAD_RING_TIME),
            resume_time: w(HEAD_RESUME_TIME),
            bells: w(core::mem::offset_of!(Head, bells)),
            bells_in_wait: w(core::mem::offset_of!(Head, bells_in_wait)),
            unknown_calls: w(core::mem::offset_of!(Head, unknown_calls)),
            held: w(core::mem::offset_of!(Head, held)),
            trips: w(HEAD_TRIPS),
            stretches: self.stretches().at,
        }
    }
    /// The stretches, word by word.
    pub fn stretches(&self) -> Stretches {
        let w = |offset: usize| self.word(offset).load(Acquire);
        Stretches {
            trips: w(HEAD_TRIPS),
            at: core::array::from_fn(|k| core::array::from_fn(|i| w(HEAD_STRETCHES + 16 * k + 8 * i))),
        }
    }
    /// The resident, at the start of each trip out of generated code (at CNTVCT `now`): the stretch it ends, from the last
    /// resume to `now`, is kept.
    #[inline(always)]
    pub fn left(&self, now: u64) {
        use core::sync::atomic::Ordering::Relaxed;
        let n = self.word(HEAD_TRIPS).load(Relaxed);
        let slot = HEAD_STRETCHES + 16 * (n % STRETCHES as u64) as usize;
        self.word(slot).store(self.word(HEAD_RESUME_TIME).load(Relaxed), Relaxed);
        self.word(slot + 8).store(now, Relaxed);
        self.word(HEAD_TRIPS).store(n + 1, Relaxed);
    }
}

#[cfg(test)]
mod tests {
    extern crate std;
    use super::*;
    use std::vec::Vec;

    use crate::execution::cache::{CODE_VA, DISPATCH_SLOT_BYTES, DISPATCH_VA, HEAD_BYTES, NO_CONTEXT};
    use crate::execution::probe;

    /// A code cache of two blocks (one in the step slot), slot contexts and one page of guest RAM.
    struct Fake {
        page: Vec<u8>,
    }
    const BLOCK_PC: u64 = CODE_VA + HEAD_BYTES as u64 + 0x1000;
    const STEP_PC: u64 = CODE_VA + 0x40;
    const TAIL_PC: u64 = BLOCK_PC + 0x100;
    const TARGET: u64 = 0x40_1123;
    impl Places for Fake {
        fn block(&self, pc: u64) -> Option<BlockView> {
            let view = BlockView { rip: 0xffff_ffff_8100_0040, context: 0x5, frame: 0x12_3000, ..BlockView::default() };
            match pc {
                p if (BLOCK_PC - 4..BLOCK_PC + 0x100).contains(&p) => {
                    Some(BlockView { offset: p.wrapping_sub(BLOCK_PC) as i64, hot: p >= BLOCK_PC + 0x80, ..view })
                }
                p if p == STEP_PC => Some(BlockView { rip: 0x7000, step: true, dead: true, ..view }),
                _ => None,
            }
        }
        fn tail(&self, pc: u64) -> Option<(BlockView, u16)> {
            let view = BlockView { rip: 0x40_0000, context: 0x6, frame: 0x45_6000, dead: true, ..BlockView::default() };
            (TAIL_PC..TAIL_PC + 12).contains(&pc).then(|| (view, ((pc - TAIL_PC) / 4) as u16 | TAIL_CALL))
        }
        fn slot_context(&self, k: usize) -> u64 {
            if k == 2 { 0x9 } else { NO_CONTEXT }
        }
        fn target(&self, rip: u64, context: u64) -> Option<u64> {
            (rip & !0xff == TARGET & !0xff && context == 0x9).then_some(0x77_7000)
        }
        fn read(&self, gpa: u64, out: &mut [u8; BEFORE]) -> bool {
            if gpa & !0xfff != 0x77_7000 {
                return false;
            }
            out.copy_from_slice(&self.page[(gpa & 0xfff) as usize..][..BEFORE]);
            true
        }
    }
    fn fake() -> Fake {
        let mut page = std::vec![0x90u8; 4096];
        page[(TARGET & 0xfff) as usize - 5] = 0xe8; // a call before the target: a return address
        Fake { page }
    }

    /// Where `record` places a bell: rung before the last resume it is the resident's (since from the ring); otherwise in
    /// a block (offset biased, the hot copy flagged; a PIC full entry at 0), in a probe charged to its store's block through
    /// x0 then d0, in a dispatcher slot with the target and its call test (unknown in slot 0, in a slot without a context
    /// and without a current block), in the step slot's block (dead flagged), in the head, in no block, outside the area.
    #[test]
    fn a_bell_is_placed_where_generated_code_was() {
        let p = fake();
        let st = stretches(&[(500, 700), (900, 1100)]);
        let at = |elr: u64, x0: u64, d0: u64| record(1000, &st, 1100, 0x1abc, elr, x0, d0, &p);
        let r = record(800, &st, 1100, 7, BLOCK_PC, 0, 0, &p);
        assert_eq!(r, Sample { time: 800, kind: RESIDENT, since: 300, ..Sample::default() });
        let b = at(BLOCK_PC + 0x10, 0, 0);
        assert_eq!((b.kind, b.place, b.since, b.time, b.flushes), (EL0, BLOCK, 200, 1000, 0xabc));
        assert_eq!((b.rip, b.context, b.frame, b.at, b.flags), (0xffff_ffff_8100_0040, 5, 0x123, 0x14, 0));
        let hot = at(BLOCK_PC + 0x90, 0, 0);
        assert_eq!((hot.at, hot.flags), (0x94, HOT));
        assert_eq!(at(BLOCK_PC - 4, 0, 0).at, 0, "a PIC block's full entry");
        // A probe: at its first word the store's block is x0's, after it d0's.
        let thunk = CODE_VA + probe::offset(probe::index(false, 3, 8).unwrap());
        let first = at(thunk, BLOCK_PC + 0x20, 0);
        assert_eq!((first.place, first.at, first.rip), (PROBE, 0x24, 0xffff_ffff_8100_0040));
        let later = at(thunk + 8, 0, BLOCK_PC + 0x30);
        assert_eq!((later.place, later.at), (PROBE, 0x34));
        let lost = at(thunk + 8, BLOCK_PC, 0x1234);
        assert_eq!((lost.place, lost.frame), (HEAD, (thunk + 8 - CODE_VA) as u32), "d0 names no block");
        // The dispatcher: slot 2 has a context and a current block for the target, whose bytes end a call.
        let slot = DISPATCH_VA + 2 * DISPATCH_SLOT_BYTES as u64 + 4;
        let d = at(slot, TARGET, 0);
        assert_eq!((d.place, d.rip, d.at, d.context, d.frame), (DISPATCH, TARGET, (slot - DISPATCH_VA) as u16, 9, 0x777));
        assert_eq!(d.flags >> CALL_SHIFT, CALL_YES);
        assert_eq!(at(slot, TARGET + 0x10, 0).flags >> CALL_SHIFT, CALL_NO, "not after a call");
        for (elr, x0) in [(DISPATCH_VA + 4, TARGET), (slot + DISPATCH_SLOT_BYTES as u64, TARGET), (slot, 0x40_2000)] {
            let u = at(elr, x0, 0);
            assert_eq!((u.place, u.flags >> CALL_SHIFT, u.context, u.frame), (DISPATCH, CALL_UNKNOWN, 0, 0), "{elr:#x}");
        }
        let s = at(STEP_PC, 0, 0);
        assert_eq!((s.place, s.rip, s.flags), (STEP, 0x7000, DEAD));
        assert_eq!(at(CODE_VA + probe::offset(0) + 4, 0, 0).place, HEAD, "an empty probe slot of the head");
        // Indirect plan step 1: a block's appended words, the target in x0, the word's role in `at`.
        let t = at(TAIL_PC + 4, TARGET, 0);
        assert_eq!((t.place, t.rip, t.context, t.frame, t.at, t.flags), (TAIL, TARGET, 6, 0x456, 1 | TAIL_CALL, DEAD));
        let none = at(BLOCK_PC + 0x1000, 0, 0);
        assert_eq!((none.place, none.frame), (NO_BLOCK, (BLOCK_PC + 0x1000 - CODE_VA) as u32));
        assert_eq!(at(CODE_VA - 4, 0, 0).place, OUTSIDE);
    }

    /// Stretches of generated code (resume, exit), oldest first; the last is the bell's own trip out.
    fn stretches(list: &[(u64, u64)]) -> Stretches {
        let mut st = Stretches { trips: list.len() as u64, ..Stretches::default() };
        for (k, &(r, e)) in list.iter().enumerate() {
            st.at[k % STRETCHES] = [r, e];
        }
        st
    }
    /// Three stretches, 100-300, 500-700 and the interrupted one from 900; the interrupt at 1100.
    fn class(rung: u64) -> Sample {
        record(rung, &stretches(&[(100, 300), (500, 700), (900, 1100)]), 1100, 7, BLOCK_PC + 0x10, 0, 0, &fake())
    }
    /// The regions of a ring time (el0-sampler-findings section 1). In the interrupted stretch (a ring at its resume
    /// included): EL0 at the PC.
    #[test]
    fn a_ring_in_the_interrupted_stretch_is_el0_at_the_pc() {
        for rung in [900, 950, 1099] {
            let s = class(rung);
            assert_eq!((s.kind, s.place, s.since), (EL0, BLOCK, 200), "{rung}");
        }
    }
    /// Between two stretches (a ring at an exit included), and before the first: the resident's or the host's.
    #[test]
    fn a_ring_between_stretches_is_resident() {
        for rung in [700, 800, 899, 300, 450, 499, 0, 99] {
            assert_eq!(class(rung), Sample { time: rung, kind: RESIDENT, since: (1100 - rung) as u32, ..Sample::default() }, "{rung}");
        }
    }
    /// In an earlier stretch: EL0 with no place, whatever the PC; `frame` the ticks to that stretch's end, `at` how many
    /// stretches back.
    #[test]
    fn a_ring_in_an_earlier_stretch_is_el0_with_no_place() {
        for (rung, end, back) in [(500, 700, 1), (600, 700, 1), (699, 700, 1), (100, 300, 2), (299, 300, 2)] {
            let s = class(rung);
            assert_eq!(s, Sample { time: rung, kind: EL0_UNKNOWN, since: (1100 - rung) as u32, frame: (end - rung) as u32,
                at: back, ..Sample::default() }, "{rung}");
        }
        let far = stretches(&[(0, u32::MAX as u64 + 10), (u32::MAX as u64 + 15, u32::MAX as u64 + 20)]);
        assert_eq!(record(1, &far, u32::MAX as u64 + 20, 7, BLOCK_PC, 0, 0, &fake()).frame, u32::MAX, "saturated");
    }
    /// Only the last STRETCHES are kept: a ring before the oldest is resident, flagged OLD; the oldest kept still counts.
    #[test]
    fn a_ring_older_than_every_stretch_kept_is_old() {
        let list: std::vec::Vec<(u64, u64)> = (0..STRETCHES as u64 + 4).map(|k| (100 * k + 10, 100 * k + 60)).collect();
        let st = stretches(&list);
        let oldest = list[list.len() - STRETCHES];
        let s = record(oldest.0, &st, 10_000, 7, BLOCK_PC, 0, 0, &fake());
        assert_eq!((s.kind, s.at), (EL0_UNKNOWN, STRETCHES as u16 - 1));
        let gap = record(oldest.0 - 1, &st, 10_000, 7, BLOCK_PC, 0, 0, &fake());
        assert_eq!((gap.kind, gap.flags), (RESIDENT, OLD));
        let before = record(list[list.len() - STRETCHES - 1].0, &st, 10_000, 7, BLOCK_PC, 0, 0, &fake());
        assert_eq!((before.kind, before.flags), (RESIDENT, OLD));
        assert_eq!(Stretches::default().class(5), Class::Outside { old: true }, "no trip yet");
    }

    /// Each trip out keeps the stretch it ends: the last resume and its own time, at its trip's slot.
    #[test]
    fn each_trip_out_keeps_the_stretch_it_ends() {
        let mut buf = std::vec![0u64; BYTES / 8];
        // SAFETY: `buf` holds BYTES, 8-aligned.
        let ring = unsafe { Ring::at(buf.as_mut_ptr().cast()) };
        let resume = |t: u64| ring.word(HEAD_RESUME_TIME).store(t, core::sync::atomic::Ordering::Relaxed);
        for k in 0..STRETCHES as u64 + 2 {
            resume(1000 * k);
            ring.left(1000 * k + 400);
        }
        let st = ring.stretches();
        assert_eq!(st.trips, STRETCHES as u64 + 2);
        assert_eq!(st.at[0], [16_000, 16_400]);
        assert_eq!(st.at[1], [17_000, 17_400]);
        assert_eq!(st.at[2], [2_000, 2_400]);
        assert_eq!(ring.head().stretches, st.at);
    }

    /// `at` holds a native offset + 4; one that does not fit is saturated and flagged, never wrapped.
    #[test]
    fn a_native_offset_past_at_saturates() {
        assert_eq!(at(-4), (0, 0));
        assert_eq!(at(0x7ff0), (0x7ff4, 0));
        assert_eq!(at(0xfffa), (0xfffe, 0));
        assert_eq!(at(0xfffb), (u16::MAX, FAR));
        assert_eq!(at(0x1_0004), (u16::MAX, FAR));
        assert_eq!(at(-8), (0, FAR));
    }

    /// A GIC with pending INTIDs (in priority order), as the resident's registers show them.
    #[derive(Default)]
    struct G {
        pending: Vec<u64>,
        ended: Vec<u64>,
        active: Option<u64>,
    }
    impl Gic for G {
        fn signalled(&mut self) -> bool {
            // While one is active (not ended) no interrupt of its priority is signalled.
            self.active.is_none() && !self.pending.is_empty()
        }
        fn acknowledge(&mut self) -> u64 {
            if !self.signalled() {
                return 1023;
            }
            let i = self.pending.remove(0);
            self.active = Some(i);
            i
        }
        fn end(&mut self, intid: u64) {
            assert_eq!(self.active.take(), Some(intid), "ends only the active one");
            self.ended.push(intid);
        }
    }
    /// The bell state of a vCPU whose bell is `bell`.
    #[derive(Default)]
    struct B {
        bell: u64,
        held: Option<u64>,
        in_wait: u64,
    }
    impl Bells for B {
        fn bell(&self) -> u64 {
            self.bell
        }
        fn held(&mut self) -> Option<u64> {
            self.held.take()
        }
        fn hold(&mut self, intid: u64) {
            self.held = Some(intid);
        }
        fn in_wait(&mut self) {
            self.in_wait += 1;
        }
    }
    const BELL: u64 = 200;
    /// `acknowledge`'s loop as the resident runs it: each INTID `next` gives is served, then ended by the caller.
    fn acknowledge(g: &mut G, b: &mut B) -> Vec<u64> {
        let mut served = Vec::new();
        while let Some(i) = next(g, b) {
            served.push(i);
            g.end(i);
        }
        served
    }

    /// A wait takes bells without a wake: with only bells pending all are ended and counted; a doorbell behind a bell is
    /// held (acknowledged, not ended) and `acknowledge` serves it first, then ends the bell after it and serves nothing
    /// twice; nothing pending is no wake; without a bell (0) every INTID is a doorbell, as in a build without the sampler.
    #[test]
    fn waits_take_bells_and_hold_the_rest() {
        let (mut g, mut b) = (G { pending: std::vec![BELL, BELL], ..G::default() }, B { bell: BELL, ..B::default() });
        assert!(bells_only(&mut g, &mut b));
        assert_eq!((g.ended.as_slice(), b.in_wait, b.held), (&[BELL, BELL][..], 2, None));
        let (mut g, mut b) = (G { pending: std::vec![BELL, 40, BELL], ..G::default() }, B { bell: BELL, ..B::default() });
        assert!(!bells_only(&mut g, &mut b), "a doorbell is a wake");
        assert_eq!((g.ended.as_slice(), b.held, g.active), (&[BELL][..], Some(40), Some(40)));
        assert!(!g.signalled(), "the held doorbell is active: nothing else is signalled");
        assert_eq!(acknowledge(&mut g, &mut b), [40]);
        assert_eq!((g.ended.as_slice(), b.in_wait, g.pending.len(), b.held), (&[BELL, 40, BELL][..], 2, 0, None));
        let (mut g, mut b) = (G::default(), B { bell: BELL, ..B::default() });
        assert!(bells_only(&mut g, &mut b));
        assert!(acknowledge(&mut g, &mut b).is_empty());
        // No bell: `next` is the plain loop and the bell's INTID is served like any other.
        let (mut g, mut b) = (G { pending: std::vec![BELL, 40], ..G::default() }, B::default());
        assert_eq!(acknowledge(&mut g, &mut b), [BELL, 40]);
        assert_eq!(b.in_wait, 0);
        assert!(!is_bell(0, 0) && is_bell(BELL, BELL) && !is_bell(BELL, 40));
    }

    /// The resume after a spurious INTID or a bell arms a wanted stop in the block the PC runs on in: the store's block for
    /// a probe PC (x0, then d0), the PC's own elsewhere; nothing without a stop wanted.
    #[test]
    fn a_wanted_stop_is_armed_in_the_probe_store_s_block() {
        let thunk = CODE_VA + probe::offset(probe::index(true, 7, 4).unwrap());
        assert_eq!(probe::rearm_at(true, thunk, BLOCK_PC, 1), Some(BLOCK_PC));
        assert_eq!(probe::rearm_at(true, thunk + 4, 1, BLOCK_PC + 8), Some(BLOCK_PC + 8));
        assert_eq!(probe::rearm_at(true, BLOCK_PC + 12, 1, 2), Some(BLOCK_PC + 12));
        assert_eq!(probe::rearm_at(false, thunk, BLOCK_PC, 1), None);
        // The armed word's block is one: a probe PC itself would find none (the resume would halt BAD_STOP).
        let p = fake();
        assert!(p.block(probe::rearm_at(true, thunk + 4, 1, BLOCK_PC + 8).unwrap()).is_some());
        assert!(p.block(thunk + 4).is_none());
    }

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
        // The test on a page (the resident's own): read at the target block's frame and the target's offset, 15 bytes
        // back; a target fewer than 15 bytes into its page, or no RAM there, is unknown.
        let mut page = std::vec![0x90u8; 4096];
        page[0x100 - 5] = 0xe8;
        let read = |gpa: u64, out: &mut [u8; BEFORE]| {
            assert_eq!(gpa & !0xfff, 0x5_5000, "the target block's frame");
            out.copy_from_slice(&page[(gpa & 0xfff) as usize..][..BEFORE]);
            true
        };
        assert_eq!(call_test(0x5_5000, 0x7000_0100, read), CALL_YES);
        assert_eq!(call_test(0x5_5000, 0x7000_0200, read), CALL_NO);
        assert_eq!(call_test(0x5_5000, 0x7000_000e, read), CALL_UNKNOWN);
        assert_eq!(call_test(0x5_5000, 0x7000_000f, read), CALL_NO);
        assert_eq!(call_test(0x5_5000, 0x7000_0100, |_, _| false), CALL_UNKNOWN);
    }

    #[test]
    fn hashes_are_fnv1a() {
        assert_eq!(hash(b""), 0xcbf2_9ce4_8422_2325);
        assert_eq!(hash(b"a"), 0xaf63_dc4c_8601_ec8c);
    }
}
