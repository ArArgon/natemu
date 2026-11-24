//! Publication into the shared template pool (cross-vCPU reuse plan, chunk 2). The pool's format, ordering protocol and
//! reader live in [`natemu_abi::pool`]; this is its only writer: after admission of a template the request's vCPU puts the
//! admitted block's code, sites, packed relocations and link points into the ring, under one lock, once per answer. The
//! resident does not read it yet (chunk 3).
use crate::execution::GATING;
use crate::memory::Memory;
use natemu_abi::code_object::{Header, Reloc};
use natemu_abi::execution::{LinkPoint, MAX_LINK_POINTS, cache::MAX_RELOCS, pack_reloc};
use natemu_abi::pool::{self, Layout, Parts, Pool, Skip, Writer};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};

/// Why the pool was switched off ([`Publisher::disable`]); the performance log names it.
pub(crate) mod off {
    /// One context's requests carried different gating values (the pool's key leaves them to the context).
    pub const GATING: u8 = 1;
    /// A vCPU presents other CPU features than the run's first compiler (it keeps its own answers).
    pub const FEATURES: u8 = 2;
}

/// What a request's block is, for publication: the admitted block (`block.object` is what the host stages) and the reply
/// fields the host sets beside it.
pub(crate) struct Template<'a> {
    pub rip: u64,
    pub context: u64,
    /// The guest bytes the answer read (at least the block's).
    pub prefix: &'a [u8],
    pub block: &'a crate::admission::Admitted,
    /// The mask of link points that are the block's REP self-exits.
    pub rep_exits: u64,
}

/// What [`Publisher::publish`] did.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Published {
    /// At this position.
    Done(u64),
    /// Kept in the ring still, from an earlier publication under this context.
    Already,
    /// Kept in the ring still, but published under another context: the key includes the context, so it is not found for
    /// this one, and it is not published again (two contexts using one answer would ping-pong). Counted.
    OtherContext,
    /// Not published.
    Skipped(Why),
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Why {
    /// The pool is off.
    Off,
    /// A relocation that cannot be packed, too many of them, or an object that is no admitted object.
    Unpackable,
    /// The entry would exceed the staging area (or the ring).
    TooBig,
    /// What the pool's decoder would refuse.
    Malformed,
}

/// An answer's publication cell: the position it was last published at (below 2^46) and, above bit 48, a hash of the context it
/// was published under.
const CELL_POSITION: u64 = (1 << 48) - 1;
fn context_tag(context: u64) -> u64 {
    context.wrapping_mul(0x9e37_79b9_7f4a_7c15) >> 48 << 48
}
/// [`context_tag`] for the host's tests of the key's width.
#[cfg(test)]
pub(crate) fn context_tag_of(context: u64) -> u64 {
    context_tag(context)
}
/// What the cell `before` says of a template under `context`, with the ring's `tail`: `None` when it is not in the ring.
fn in_ring(before: u64, tail: u64, context: u64) -> Option<Published> {
    let position = before & CELL_POSITION;
    (position != 0 && position >= tail).then(|| {
        if before & !CELL_POSITION == context_tag(context) {
            Published::Already
        } else {
            Published::OtherContext
        }
    })
}

#[derive(Default)]
struct Counts {
    published: AtomicU64,
    bytes: AtomicU64,
    already: AtomicU64,
    other_context: AtomicU64,
    off: AtomicU64,
    unpackable: AtomicU64,
    too_big: AtomicU64,
    malformed: AtomicU64,
    gating_mismatches: AtomicU64,
    /// Nanoseconds in [`Publisher::publish`] (lock wait included), counted only while the performance log is on.
    ns: AtomicU64,
    off_reason: std::sync::atomic::AtomicU8,
}

struct State {
    writer: Writer,
    scratch: Vec<u8>,
    /// Per context, the first gating vector a block request carried ([`Publisher::note`]).
    gating: HashMap<u64, [u64; GATING]>,
    relocs: Vec<u32>,
}

/// The one writer of a run's pool, shared by its vCPUs' compilers.
pub struct Publisher {
    /// Keeps the span mapped.
    _memory: Arc<Memory>,
    off: AtomicBool,
    /// The span as a reader sees it (the tail, without the lock).
    pool: Pool,
    state: Mutex<State>,
    counts: Counts,
}
impl Publisher {
    /// The writer of the pool at backing offset `offset` of `memory` (whose span is zero), switched on.
    #[cfg(test)]
    pub(crate) fn new(memory: Arc<Memory>, offset: usize) -> Self {
        Self::with_layout(memory, offset, pool::PRODUCTION)
    }
    pub(crate) fn with_layout(memory: Arc<Memory>, offset: usize, layout: Layout) -> Self {
        assert!(layout.valid() && offset % 64 == 0);
        assert!(
            offset
                .checked_add(layout.span_bytes())
                .is_some_and(|end| end <= memory.1),
            "the pool lies outside the backing"
        );
        // SAFETY: the span is inside the mapping `memory` keeps alive for as long as this publisher, 64-byte aligned (the
        // mapping is page aligned), zero (nothing else wrote it) and read by no one before the run; this is its only writer.
        let writer = unsafe { Writer::new(Pool::new(memory.0.add(offset), layout)) };
        Self {
            _memory: memory,
            off: AtomicBool::new(false),
            pool: writer.pool(),
            state: Mutex::new(State {
                writer,
                scratch: vec![0; pool::MAX_ENTRY_BYTES],
                gating: HashMap::new(),
                relocs: Vec::new(),
            }),
            counts: Counts::default(),
        }
    }
    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// The first gating vector a block request of `context` carried (tests).
    #[cfg(test)]
    pub(crate) fn gating_of(&self, context: u64) -> Option<[u64; GATING]> {
        self.state().gating.get(&context).copied()
    }
    /// The pool as a reader sees it (tests; the resident maps the same span).
    #[cfg(test)]
    pub(crate) fn pool(&self) -> Pool {
        self.pool
    }
    /// The span's layout (the resident is told it: [`natemu_abi::execution::cache::Control::pool_layout`]).
    pub(crate) fn layout(&self) -> Layout {
        self.pool.layout()
    }
    pub fn is_off(&self) -> bool {
        self.off.load(Relaxed)
    }
    /// Switches the pool off for good (every lookup reads its switch first; the entries already published are never used).
    pub(crate) fn disable(&self, reason: u8) {
        let state = self.state();
        state.writer.disable();
        self.off.store(true, Relaxed);
        let _ = self
            .counts
            .off_reason
            .compare_exchange(0, reason, Relaxed, Relaxed);
    }
    /// A block request under `context` carried `gating`: false when another request of that context carried other values
    /// (the pool is then off, and counted).
    pub(crate) fn note(&self, context: u64, gating: &[u64; GATING]) -> bool {
        let same = *self.state().gating.entry(context).or_insert(*gating) == *gating;
        if !same {
            self.counts.gating_mismatches.fetch_add(1, Relaxed);
            self.disable(off::GATING);
        }
        same
    }
    /// Publishes `t`, unless `once` (the answer's own cell: the position it was published at) says it is in the ring still.
    pub(crate) fn publish(&self, once: &AtomicU64, t: &Template) -> Published {
        let started = if crate::perf::on() {
            crate::perf::now()
        } else {
            0
        };
        let result = self.publish_once(once, t);
        if started != 0 {
            self.counts
                .ns
                .fetch_add(crate::perf::now() - started, Relaxed);
        }
        let c = &self.counts;
        match result {
            Published::Done(_) => {}
            Published::Already => drop(c.already.fetch_add(1, Relaxed)),
            Published::OtherContext => drop(c.other_context.fetch_add(1, Relaxed)),
            Published::Skipped(Why::Off) => drop(c.off.fetch_add(1, Relaxed)),
            Published::Skipped(Why::Unpackable) => drop(c.unpackable.fetch_add(1, Relaxed)),
            Published::Skipped(Why::TooBig) => drop(c.too_big.fetch_add(1, Relaxed)),
            Published::Skipped(Why::Malformed) => drop(c.malformed.fetch_add(1, Relaxed)),
        }
        result
    }
    fn publish_once(&self, once: &AtomicU64, t: &Template) -> Published {
        if self.is_off() {
            return Published::Skipped(Why::Off);
        }
        // Still in the ring: published at a position not below the tail (the usual case of a hit: no lock, no encoding). Checked
        // again under the lock, where another vCPU's publication of the same answer may have happened meanwhile.
        if let Some(seen) = in_ring(once.load(Relaxed), self.pool.tail(), t.context) {
            return seen;
        }
        let o = &t.block.object;
        let Some(h) = crate::relocation::read::<Header>(o, 0) else {
            return Published::Skipped(Why::Unpackable);
        };
        let slice = |offset: u32, bytes: usize| {
            let at = offset as usize;
            o.get(at..at.checked_add(bytes)?)
        };
        let (Some(code), Some(sites), true) = (
            slice(h.code_offset, h.code_size as usize),
            slice(
                h.sites_offset,
                (h.sites_count as usize).saturating_mul(pool::SITE_BYTES),
            ),
            t.block.links.len() <= MAX_LINK_POINTS && h.relocs_count <= MAX_RELOCS,
        ) else {
            return Published::Skipped(Why::Unpackable);
        };
        let mut state = self.state();
        let State {
            writer,
            scratch,
            relocs,
            ..
        } = &mut *state;
        // The entry is still in the ring: published at a position not below the tail.
        if let Some(seen) = in_ring(once.load(Relaxed), writer.tail(), t.context) {
            return seen;
        }
        // The packed relocations, in increasing order as the resident's copies keep them (`cache::place`); one that cannot be
        // packed ([`pack_reloc`]: `None`) makes the object no template for the resident.
        relocs.clear();
        for i in 0..h.relocs_count as usize {
            let at = h.relocs_offset as usize + i * size_of::<Reloc>();
            match crate::relocation::read::<Reloc>(o, at).and_then(|r| pack_reloc(&r, h.code_size))
            {
                None => return Published::Skipped(Why::Unpackable),
                Some(packed) => relocs.extend(packed),
            }
        }
        relocs.sort_unstable();
        let mut links = [LinkPoint::NONE; MAX_LINK_POINTS];
        links[..t.block.links.len()].copy_from_slice(&t.block.links);
        let parts = Parts {
            rip: t.rip,
            context: t.context,
            guest_bytes: t.block.guest_bytes,
            sti_first: t.block.sti_first,
            links,
            dispatch_exit: t.block.dispatch_exit.unwrap_or(0),
            rep_exits: t.rep_exits as u32,
            exit_forms: t.block.exit_forms,
            code,
            sites,
            relocs,
            prefix: t.prefix,
        };
        match writer.publish(&parts, scratch) {
            Ok(position) => {
                once.store(position | context_tag(t.context), Relaxed);
                self.counts.published.fetch_add(1, Relaxed);
                self.counts
                    .bytes
                    .fetch_add(parts.entry_bytes().unwrap_or(0) as u64, Relaxed);
                Published::Done(position)
            }
            Err(Skip::Off) => Published::Skipped(Why::Off),
            Err(Skip::TooBig | Skip::Exhausted) => Published::Skipped(Why::TooBig),
            Err(Skip::Malformed(_)) => Published::Skipped(Why::Malformed),
        }
    }
    /// The performance log's line for the pool.
    pub fn report(&self) -> String {
        #[derive(serde::Serialize)]
        struct Report {
            published: u64,
            bytes: u64,
            already: u64,
            other_context: u64,
            skipped_off: u64,
            skipped_unpackable: u64,
            skipped_too_big: u64,
            skipped_malformed: u64,
            gating_mismatches: u64,
            off_reason: u8,
            publish_ns: u64,
            tail: u64,
            ring_bytes: usize,
        }
        let c = &self.counts;
        let state = self.state();
        let report = Report {
            published: c.published.load(Relaxed),
            bytes: c.bytes.load(Relaxed),
            already: c.already.load(Relaxed),
            other_context: c.other_context.load(Relaxed),
            skipped_off: c.off.load(Relaxed),
            skipped_unpackable: c.unpackable.load(Relaxed),
            skipped_too_big: c.too_big.load(Relaxed),
            skipped_malformed: c.malformed.load(Relaxed),
            gating_mismatches: c.gating_mismatches.load(Relaxed),
            off_reason: c.off_reason.load(Relaxed),
            publish_ns: c.ns.load(Relaxed),
            tail: state.writer.tail(),
            ring_bytes: state.writer.pool().layout().ring_bytes,
        };
        format!("pool {}", serde_json::to_string(&report).unwrap())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::admission::Admitted;
    use natemu_abi::code_object as object;
    use natemu_abi::execution::RELOC_MOVE;
    use natemu_abi::pool::{Key, LEAD};

    const LAYOUT: Layout = Layout {
        index_slots: 64,
        ring_bytes: 64 << 10,
        control_bytes: 128,
    };
    fn publisher(layout: Layout) -> Publisher {
        Publisher::with_layout(
            Arc::new(Memory::with_size(layout.span_bytes()).unwrap()),
            0,
            layout,
        )
    }
    fn reloc(offset: u32, kind: u8, register: u8, size: u8, value: u64) -> Reloc {
        Reloc {
            offset,
            kind,
            register,
            size,
            reserved: 0,
            value,
        }
    }
    /// An object as the host stages it: its header, `code`, `sites` site records and `relocs`.
    fn object(code: &[u8], sites: usize, relocs: &[Reloc]) -> Vec<u8> {
        // SAFETY: a header is plain integers; zero is a value of each.
        let mut h: Header = unsafe { std::mem::zeroed() };
        let code_at = size_of::<Header>();
        let sites_at = code_at + code.len();
        let relocs_at = sites_at + sites * pool::SITE_BYTES;
        h.code_offset = code_at as u32;
        h.code_size = code.len() as u32;
        h.sites_offset = sites_at as u32;
        h.sites_count = sites as u32;
        h.relocs_offset = relocs_at as u32;
        h.relocs_count = relocs.len() as u32;
        let mut o = vec![0; relocs_at + relocs.len() * size_of::<Reloc>()];
        // SAFETY: the records are plain integers written inside `o`.
        unsafe {
            std::ptr::write_unaligned(o.as_mut_ptr().cast::<Header>(), h);
            for (i, r) in relocs.iter().enumerate() {
                std::ptr::write_unaligned(
                    o.as_mut_ptr()
                        .add(relocs_at + i * size_of::<Reloc>())
                        .cast::<Reloc>(),
                    *r,
                );
            }
        }
        o[code_at..sites_at].copy_from_slice(code);
        for (i, b) in o[sites_at..relocs_at].iter_mut().enumerate() {
            *b = (i * 7) as u8;
        }
        o
    }
    fn block(object: Vec<u8>) -> Admitted {
        Admitted {
            object,
            guest_bytes: 3,
            guest_insns: 1,
            end_reason: 2,
            links: vec![LinkPoint {
                offset: 16,
                word: 0xd280_0000,
                target: 0x2000,
            }],
            dispatch_exit: Some(8),
            rep_self: Some(0x2000),
            sti_first: true,
            bulk: false,
            last: 0,
            exit_forms: 0,
        }
    }
    const PREFIX: [u8; 6] = [0x48, 0x01, 0xd8, 0xc3, 0x90, 0x90];
    fn template<'a>(b: &'a Admitted, rip: u64, prefix: &'a [u8]) -> Template<'a> {
        Template {
            rip,
            context: 3,
            prefix,
            block: b,
            rep_exits: 1,
        }
    }
    fn code() -> Vec<u8> {
        (0..64u8).collect()
    }
    fn relocs() -> Vec<Reloc> {
        vec![
            reloc(24, object::RELOC_GUEST_RIP_MOVE, 5, 0, 0x1234),
            reloc(0, object::RELOC_NAMED_SYMBOL_LITERAL, 0, 8, 0),
            reloc(8, object::RELOC_GUEST_RIP_LITERAL, 0, 8, 0x1003),
            reloc(12, object::RELOC_DISPATCH_BRANCH, 0, 0, 0),
        ]
    }
    fn report(p: &Publisher) -> serde_json::Value {
        serde_json::from_str(p.report().strip_prefix("pool ").unwrap()).unwrap()
    }

    #[test]
    fn a_template_is_published_with_packed_sorted_relocations_and_found_by_content() {
        let p = publisher(LAYOUT);
        let b = block(object(&code(), 2, &relocs()));
        let once = AtomicU64::new(0);
        let Published::Done(at) = p.publish(&once, &template(&b, 0x7fff_0000_1000, &PREFIX)) else {
            panic!()
        };
        assert_eq!(once.load(Relaxed) & CELL_POSITION, at);
        let pool = p.pool();
        let key = Key::new(3, 0x1000, &PREFIX, LEAD);
        assert!(pool.probe(&key).contains(&at));
        let mut staging = vec![0; pool::MAX_ENTRY_BYTES];
        let e = pool.fetch(at, &mut staging).unwrap();
        assert_eq!(
            (e.rip, e.context, e.guest_bytes, e.sti_first),
            (0x7fff_0000_1000, 3, 3, true)
        );
        assert_eq!(e.code, &code()[..]);
        assert_eq!(e.sites_count(), 2);
        let sites_at = size_of::<Header>() + code().len();
        assert_eq!(e.site(1), unsafe {
            std::ptr::read_unaligned(
                b.object[sites_at + pool::SITE_BYTES..]
                    .as_ptr()
                    .cast::<object::Site>(),
            )
        });
        assert_eq!(
            (0..e.relocs_count())
                .map(|i| e.reloc(i))
                .collect::<Vec<_>>(),
            [2 << 6, 6 << 6 | RELOC_MOVE | 5]
        );
        assert_eq!(
            (e.links[0].offset, e.links[0].target, e.links[1]),
            (16, 0x2000, LinkPoint::NONE)
        );
        assert_eq!(
            (e.dispatch_exit, e.rep_exits, e.prefix),
            (8, 1, &PREFIX[..])
        );
    }

    #[test]
    fn an_answer_is_published_once_and_again_after_the_ring_passes_it() {
        let p = publisher(LAYOUT);
        let b = block(object(&code(), 2, &relocs()));
        let once = AtomicU64::new(0);
        let Published::Done(first) = p.publish(&once, &template(&b, 0x1000, &PREFIX)) else {
            panic!()
        };
        assert_eq!(
            p.publish(&once, &template(&b, 0x2000, &PREFIX)),
            Published::Already
        );
        // Others fill the ring until the tail passes `first`.
        let mut n = 0;
        while p.state().writer.tail() <= first {
            let other = AtomicU64::new(0);
            assert!(matches!(
                p.publish(&other, &template(&b, 0x3000 + n, &PREFIX)),
                Published::Done(_)
            ));
            assert_eq!(
                p.publish(&other, &template(&b, 0x3000 + n, &PREFIX)),
                Published::Already
            );
            n += 1;
        }
        let Published::Done(again) = p.publish(&once, &template(&b, 0x2000, &PREFIX)) else {
            panic!()
        };
        assert!(again > first && once.load(Relaxed) & CELL_POSITION == again);
        let r = report(&p);
        assert_eq!(r["published"], n + 2);
        assert_eq!(r["already"], n + 1);
    }

    #[test]
    fn what_the_resident_would_not_keep_is_not_published() {
        let p = publisher(LAYOUT);
        let once = AtomicU64::new(0);
        let skipped = |b: &Admitted, prefix: &[u8], why| {
            assert_eq!(
                p.publish(&once, &template(b, 0x1000, prefix)),
                Published::Skipped(why)
            );
            assert_eq!(once.load(Relaxed), 0);
        };
        // A relocation of a kind no template has, a damaged object, sites past its end, more relocations than a block records.
        let odd = object(&code(), 0, &[reloc(8, object::RELOC_OTHER, 0, 0, 0)]);
        skipped(&block(odd), &PREFIX, Why::Unpackable);
        skipped(&block(vec![0; 10]), &PREFIX, Why::Unpackable);
        let mut cut = object(&code(), 2, &[]);
        cut.truncate(cut.len() - 1);
        skipped(&block(cut), &PREFIX, Why::Unpackable);
        let many: Vec<Reloc> = (0..=MAX_RELOCS)
            .map(|i| reloc(4 * i % 56, object::RELOC_NAMED_SYMBOL_LITERAL, 0, 8, 0))
            .collect();
        skipped(&block(object(&code(), 0, &many)), &PREFIX, Why::Unpackable);
        // An entry over the staging area; a prefix shorter than the block's guest bytes; a link point outside the code.
        let ok = block(object(&code(), 0, &[]));
        skipped(&ok, &vec![0x90; pool::MAX_ENTRY_BYTES], Why::TooBig);
        skipped(&ok, &PREFIX[..2], Why::Malformed);
        let mut links = block(object(&code(), 0, &[]));
        links.links[0].offset = 64;
        skipped(&links, &PREFIX, Why::Malformed);
        let r = report(&p);
        assert_eq!(r["published"], 0);
        assert_eq!(
            [
                r["skipped_unpackable"].as_u64(),
                r["skipped_too_big"].as_u64(),
                r["skipped_malformed"].as_u64()
            ],
            [Some(4), Some(1), Some(2)]
        );
    }

    /// An answer published under one context and used under another is not published again, and is counted.
    #[test]
    fn an_answer_used_under_another_context_is_counted_not_published() {
        let p = publisher(LAYOUT);
        let b = block(object(&code(), 0, &[]));
        let once = AtomicU64::new(0);
        let (mut a, mut other) = (template(&b, 0x1000, &PREFIX), template(&b, 0x1000, &PREFIX));
        other.context = 4;
        assert!(matches!(p.publish(&once, &a), Published::Done(_)));
        assert_eq!(p.publish(&once, &other), Published::OtherContext);
        a.context = 3;
        assert_eq!(p.publish(&once, &a), Published::Already);
        let r = report(&p);
        assert_eq!(
            (r["published"].as_u64(), r["other_context"].as_u64()),
            (Some(1), Some(1))
        );
    }

    /// One gating vector per context; a request of the context with another turns the pool off for good.
    #[test]
    fn a_context_whose_gating_values_differ_turns_the_pool_off() {
        let p = publisher(LAYOUT);
        let b = block(object(&code(), 0, &[]));
        let once = AtomicU64::new(0);
        let Published::Done(at) = p.publish(&once, &template(&b, 0x1000, &PREFIX)) else {
            panic!()
        };
        let (key, pool) = (Key::new(3, 0x1000, &PREFIX, LEAD), p.pool());
        assert!(pool.probe(&key).contains(&at));
        let mut g = [0; GATING];
        assert!(p.note(1, &g) && p.note(1, &g) && !p.is_off());
        g[9] = 1;
        assert!(p.note(2, &g), "another context may have other values");
        assert!(!p.note(1, &g));
        assert!(p.is_off() && !pool.enabled());
        assert_eq!(pool.probe(&key), [0; pool::WAYS]);
        let other = AtomicU64::new(0);
        assert_eq!(
            p.publish(&other, &template(&b, 0x1000, &PREFIX)),
            Published::Skipped(Why::Off)
        );
        let r = report(&p);
        assert_eq!(
            (r["gating_mismatches"].as_u64(), r["off_reason"].as_u64()),
            (Some(1), Some(1))
        );
    }

    /// Publication cost per answer, unit level (the production layout, an entry of about 500 bytes: 200 of code, two sites,
    /// three relocations, a 40-byte prefix): `cargo test -p natemu --release publication_cost -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn publication_cost() {
        let layout = pool::PRODUCTION;
        let p = publisher(layout);
        let code: Vec<u8> = (0..200u8).collect();
        let rs = [
            reloc(24, object::RELOC_GUEST_RIP_MOVE, 5, 0, 0x1234),
            reloc(8, object::RELOC_GUEST_RIP_LITERAL, 0, 8, 0x1003),
            reloc(100, object::RELOC_GUEST_RIP_MOVE, 7, 0, 0x1234),
        ];
        let b = block(object(&code, 2, &rs));
        let prefix = [0x90u8; 40];
        let n = 1_000_000;
        let cells: Vec<AtomicU64> = (0..n).map(|_| AtomicU64::new(0)).collect();
        let (mut started, mut last) = (std::time::Instant::now(), 0);
        for (i, cell) in cells.iter().enumerate() {
            if i % 250_000 == 0 && i > 0 {
                let ns = started.elapsed().as_nanos() as f64 / (i - last) as f64;
                eprintln!(
                    "answers {last}..{i}: {ns:.0} ns each (ring laps so far {})",
                    p.state().writer.tail() / layout.ring_bytes as u64
                );
                (started, last) = (std::time::Instant::now(), i);
            }
            let t = Template {
                rip: 0x7fff_0000_1000 + (i as u64 % 4096) * 16,
                context: i as u64 % 5,
                prefix: &prefix,
                block: &b,
                rep_exits: 0,
            };
            assert!(matches!(p.publish(cell, &t), Published::Done(_)));
        }
        let ns = started.elapsed().as_nanos() as f64 / (n - last) as f64;
        let bytes = p.counts.bytes.load(Relaxed) / n as u64;
        eprintln!("answers {last}..{n}: {ns:.0} ns each; {bytes} bytes per entry");
        // A template request whose answer is in the ring already (most of them: a hit).
        let t = Template {
            rip: 0x7fff_0000_1000,
            context: 0,
            prefix: &prefix,
            block: &b,
            rep_exits: 0,
        };
        let cell = AtomicU64::new(0);
        assert!(matches!(p.publish(&cell, &t), Published::Done(_)));
        let started = std::time::Instant::now();
        for _ in 0..n {
            assert_eq!(p.publish(&cell, &t), Published::Already);
        }
        let ns = started.elapsed().as_nanos() as f64 / n as f64;
        eprintln!("already published: {ns:.0} ns each");
    }
}
