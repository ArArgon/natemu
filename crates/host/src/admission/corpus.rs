//! The recorded admission corpus (`tests/admission-corpus`, built by `tools/fexc/admission_corpus.py`: `boot.corpus` from one
//! recorded boot's requests, `gather.corpus` from blocks of AVX2 gathers compiled inline under `--bulk`, at CPL 3 and CPL 0,
//! which the boot never ran): admission's every result on it, digested, must be the digest kept beside it. So a change meant to
//! leave admission's answers alone (a faster form of the same checks) is shown to: each object is admitted under the modes
//! admission tells apart (the code-shape check off, as in production, and on; for 32-bit code the CPL-3 and limited-FS/GS
//! variants), and so is each of a set of mutants of it (bytes of its header, tables and code and of its guest bytes
//! changed, PC-table and site fields moved, a literal slot fallen or branched into or moved into the code region; for an
//! object with an inline gather also its element loads' marks, widths and words changed, the mark put on another load and
//! the gather's guest bytes made another form or no gather), most of them refused: a result is the admitted form (object,
//! link points, the bulk flag that keeps the object from a request without `--bulk`, and every other field) or the
//! refusal's message.
//!
//! A change meant to alter what admission admits or refuses, or how it says so, reseeds the digests:
//! `NATEMU_ADMISSION_CORPUS_RESEED=1 cargo test -p natemu-host --lib recorded_corpus` rewrites the files (and fails, so a
//! reseed is never a silent pass); the change says why in its commit.
use super::{Admitted, User32, admit_user, record};
use natemu_abi::code_object::{self as object, Header, PcEntry, Reloc, Site};
use std::io::Read;

const DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/admission-corpus");
/// The corpus files (`NAME.corpus`, digests in `NAME.digests`).
const CORPORA: [&str; 2] = ["boot", "gather"];

struct Entry {
    flags: u8,
    rip: u64,
    source: Vec<u8>,
    object: Vec<u8>,
}

fn entries(name: &str) -> Vec<Entry> {
    let packed = std::fs::read(format!("{DIR}/{name}.corpus")).unwrap();
    let mut b = Vec::new();
    flate2::read::ZlibDecoder::new(&packed[..]).read_to_end(&mut b).unwrap();
    assert_eq!(&b[..8], b"NATADMC1");
    let u32_at = |o: usize| u32::from_le_bytes(b[o..o + 4].try_into().unwrap()) as usize;
    let (n, mut o) = (u32_at(8), 12);
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        let (flags, rip) = (b[o], u64::from_le_bytes(b[o + 1..o + 9].try_into().unwrap()));
        let (s, j) = (u32_at(o + 9), u32_at(o + 13));
        o += 17;
        out.push(Entry { flags, rip, source: b[o..o + s].to_vec(), object: b[o + s..o + s + j].to_vec() });
        o += s + j;
    }
    assert_eq!(o, b.len());
    out
}

/// FNV-1a, 64 bits: a digest that never changes with the toolchain.
fn fnv(h: &mut u64, bytes: &[u8]) {
    for &x in bytes {
        *h = (*h ^ u64::from(x)).wrapping_mul(0x0100_0000_01b3);
    }
}

fn digest(h: &mut u64, r: &Result<Admitted, String>) {
    match r {
        Ok(a) => {
            // (Every field, by name: a field added to Admitted must be added here.)
            let Admitted { object, guest_bytes, guest_insns, end_reason, links, dispatch_exit, rep_self, sti_first, bulk, last } = a;
            fnv(h, b"ok");
            fnv(h, object);
            let rest = format!("{guest_bytes} {guest_insns} {end_reason} {links:?} {dispatch_exit:?} {rep_self:?} {sti_first} {bulk} {last}");
            fnv(h, rest.as_bytes());
        }
        Err(e) => {
            fnv(h, b"refused");
            fnv(h, e.as_bytes());
        }
    }
}

/// A small deterministic generator (the mutants depend on the object alone).
struct Rng(u64);
impl Rng {
    fn below(&mut self, n: usize) -> usize {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        ((self.0 >> 33) as usize) % n.max(1)
    }
}

/// The mutants of `e`: (object, source) pairs.
fn mutants(e: &Entry) -> Vec<(Vec<u8>, Vec<u8>)> {
    let mut seed = 0xcbf2_9ce4_8422_2325;
    fnv(&mut seed, &e.object);
    let mut rng = Rng(seed);
    let o = &e.object;
    let mut out = Vec::new();
    if o.len() < size_of::<Header>() {
        return out;
    }
    let h: Header = record(o, 0);
    let (code, code_end) = (h.code_offset as usize, (h.code_offset + h.code_size) as usize);
    let flip = |o: &[u8], at: usize, rng: &mut Rng| {
        let mut m = o.to_vec();
        m[at] ^= (rng.below(255) + 1) as u8;
        m
    };
    // Bytes anywhere in the header, the code and the tables after it.
    let mut regions = vec![(0, size_of::<Header>()), (code, code_end), (code_end, o.len()), (code_end, o.len())];
    regions.retain(|&(lo, hi)| lo < hi && hi <= o.len());
    for (lo, hi) in regions {
        for _ in 0..2 {
            let at = lo + rng.below(hi - lo);
            out.push((flip(o, at, &mut rng), e.source.clone()));
        }
    }
    // A field of a PC-table entry or a site moved by a small amount (the PC table and the sites' offsets, which the scans
    // and searches over them read).
    let table = |offset: u32, count: u32, size: usize| (offset as usize, count as usize, size);
    for (offset, count, size) in [table(h.pcs_offset, h.pcs_count, size_of::<PcEntry>()), table(h.sites_offset, h.sites_count, size_of::<Site>())] {
        if count == 0 || offset + count * size > o.len() {
            continue;
        }
        for _ in 0..3 {
            let at = offset + rng.below(count) * size + 4 * rng.below(size.min(16) / 4);
            let mut m = o.clone();
            let v = u32::from_le_bytes(m[at..at + 4].try_into().unwrap());
            let delta = [1u32, 4, u32::MAX, u32::MAX - 3][rng.below(4)];
            m[at..at + 4].copy_from_slice(&v.wrapping_add(delta).to_le_bytes());
            out.push((m, e.source.clone()));
        }
    }
    // The word before a literal slot (data in the code region, never decoded) made a `nop` falling into it, or a `b` to it:
    // both refused (fall-through into data, a branch to no instruction), so the slot's words must read as no instruction.
    let relocs = h.relocs_offset as usize..h.relocs_offset as usize + h.relocs_count as usize * size_of::<Reloc>();
    if relocs.end <= o.len() {
        let slots = (0..h.relocs_count as usize)
            .map(|i| record::<Reloc>(o, relocs.start + i * size_of::<Reloc>()))
            .filter(|r| r.kind == object::RELOC_GUEST_RIP_LITERAL && r.offset >= 4 && code + r.offset as usize + 4 <= code_end);
        for r in slots.take(2) {
            for word in [natemu_abi::execution::NOP, 0x1400_0001] {
                let (mut m, at) = (o.clone(), code + r.offset as usize - 4);
                m[at..at + 4].copy_from_slice(&word.to_le_bytes());
                out.push((m, e.source.clone()));
            }
        }
        // FEX puts the literal slot after the code region; moved inside it (its 8 bytes zeroed there, as a slot is before
        // admission fills it), the slot's words are data among the region's words: the rules around it (what falls or
        // branches into it, the words decoded) run on them.
        let region = h.code_region_end as usize;
        let first = (0..h.relocs_count as usize).find(|&i| record::<Reloc>(o, relocs.start + i * size_of::<Reloc>()).kind == object::RELOC_GUEST_RIP_LITERAL);
        if let Some(i) = first.filter(|_| region >= 16) {
            for _ in 0..2 {
                let k = 4 * (1 + rng.below(region / 4 - 3));
                let mut m = o.clone();
                m[code + k..code + k + 8].fill(0);
                let at = relocs.start + i * size_of::<Reloc>();
                m[at..at + 4].copy_from_slice(&(k as u32).to_le_bytes());
                out.push((m, e.source.clone()));
            }
        }
    }
    // A guest byte changed (branch targets, the CLI/STI/VERW/RDTSC forms, a 32-bit block's FS/GS operands).
    if !e.source.is_empty() {
        let mut s = e.source.clone();
        let at = rng.below(s.len().min(h.guest_bytes as usize).max(1));
        s[at] ^= (rng.below(255) + 1) as u8;
        out.push((o.clone(), s));
    }
    out.extend(gather_mutants(e, &h));
    out
}

/// Gather plan (2.5): for an object with an inline gather's element loads ([`object::gather_element`]), the first and the
/// last of them each changed in one of these ways: its mark dropped, its width the other element width (both still valid
/// sites), its `tbz` skipping past the `ldr` or testing another bit, its `umov` into w1|x1; the mark put on another load of
/// the block;
/// and the first gather's guest bytes another form (VEX.L flipped: another element count) or no gather (an FMA's opcode,
/// the same length). An object without one has none of these.
fn gather_mutants(e: &Entry, h: &Header) -> Vec<(Vec<u8>, Vec<u8>)> {
    let o = &e.object;
    let (size, code) = (size_of::<Site>(), h.code_offset as usize);
    let table = h.sites_offset as usize..h.sites_offset as usize + h.sites_count as usize * size;
    if table.end > o.len() {
        return Vec::new();
    }
    let sites: Vec<(usize, Site)> = table.clone().step_by(size).map(|at| (at, record::<Site>(o, at))).collect();
    let elements: Vec<&(usize, Site)> = sites.iter().filter(|(_, s)| object::gather_element(s)).collect();
    let Some(&&(_, first)) = elements.first() else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let field = |at: usize, bytes: &[u8]| {
        let mut m = o.clone();
        m[at..at + bytes.len()].copy_from_slice(bytes);
        m
    };
    let word = |at: usize| u32::from_le_bytes(o[at..at + 4].try_into().unwrap());
    let (g2, width) = (std::mem::offset_of!(Site, guest_offset2), std::mem::offset_of!(Site, width));
    for &(row, s) in [elements[0], elements[elements.len() - 1]] {
        out.push(field(row + g2, &object::NONE32.to_le_bytes()));
        out.push(field(row + width, &[12 - s.width]));
        let at = code + s.native_offset as usize;
        if s.native_offset >= 8 && at + 4 <= o.len() {
            let (umov, tbz) = (word(at - 8), word(at - 4));
            out.push(field(at - 4, &(tbz.wrapping_add(1 << 5)).to_le_bytes()));
            out.push(field(at - 4, &(tbz.wrapping_sub(1 << 19)).to_le_bytes()));
            out.push(field(at - 8, &(umov | 1).to_le_bytes()));
        }
    }
    if let Some(&(row, _)) = sites.iter().find(|(_, s)| s.kind == object::kind::LOAD && !object::gather_element(s) && s.guest_offset2 == object::NONE32) {
        out.push(field(row + g2, &object::GATHER_ELEMENT.to_le_bytes()));
    }
    let mut out: Vec<(Vec<u8>, Vec<u8>)> = out.into_iter().map(|m| (m, e.source.clone())).collect();
    let g = first.guest_offset as usize;
    if g + 4 <= e.source.len() {
        let (mut other_l, mut fma) = (e.source.clone(), e.source.clone());
        other_l[g + 2] ^= 4;
        fma[g + 3] = 0x98;
        out.extend([(o.clone(), other_l), (o.clone(), fma)]);
    }
    out
}

/// The digest of every result of admission for `e` and its mutants; and whether `e` itself is admitted in production.
/// `other`, another entry, is admitted before `e` and before each mutant (its results are its own entry's): so whatever one
/// object could leave behind for the next one is there when `e` is admitted, from an object of another layout.
fn results(e: &Entry, other: &Entry) -> (u64, bool) {
    let (bits32, cpl0) = (e.flags & 1 != 0, e.flags & 2 != 0);
    let recorded = User32 { cpl3: true, limited: [e.flags & 4 != 0, e.flags & 8 != 0] };
    let users: Vec<User32> = if bits32 {
        vec![recorded, User32 { cpl3: true, limited: [true; 2] }, User32 { cpl3: true, limited: [false; 2] }, User32 { cpl3: false, limited: [false; 2] }]
    } else {
        vec![recorded]
    };
    let mut h = 0xcbf2_9ce4_8422_2325;
    let mut admitted = false;
    let mut all = |object: &[u8], source: &[u8], h: &mut u64, first: bool| {
        for shape in [false, true] {
            for &u in &users {
                // (A test build checks arithmetic overflow: admission as the corpus was seeded overflows on some mutants, in the x87
                // stub length it computes before it checks the stub, which a release build wraps and then never reads. A
                // panic is a result like the others: the same one must stay where it was.)
                let r = std::panic::catch_unwind(|| admit_user(object, e.rip, source, u64::MAX, shape, cpl0, bits32, u))
                    .unwrap_or_else(|_| Err("panicked".into()));
                if first && !shape && u == recorded {
                    admitted = r.is_ok();
                }
                digest(h, &r);
            }
        }
    };
    let other = || {
        let bits32 = other.flags & 1 != 0;
        let _ = std::panic::catch_unwind(|| admit_user(&other.object, other.rip, &other.source, u64::MAX, false, other.flags & 2 != 0, bits32, User32 { cpl3: true, limited: [false; 2] }));
    };
    other();
    all(&e.object, &e.source, &mut h, true);
    for (object, source) in mutants(e) {
        other();
        all(&object, &source, &mut h, false);
    }
    (h, admitted)
}

#[test]
fn recorded_corpus() {
    let corpora: Vec<Vec<Entry>> = CORPORA.iter().map(|name| entries(name)).collect();
    // (One sequence of every corpus's entries, each admitted after the next one in it: the last boot object after the
    // first gather one, the last gather object after the first boot one.)
    let all: Vec<&Entry> = corpora.iter().flatten().collect();
    // (Quiet while admission's own panics are caught, then the default hook again for this test's verdict.)
    std::panic::set_hook(Box::new(|_| {}));
    let got: Vec<(u64, bool)> = (0..all.len()).map(|i| results(all[i], all[(i + 1) % all.len()])).collect();
    drop(std::panic::take_hook());
    let reseed = std::env::var_os("NATEMU_ADMISSION_CORPUS_RESEED").is_some();
    let mut start = 0;
    let mut failures = Vec::new();
    for (name, entries) in CORPORA.iter().zip(&corpora) {
        let got = &got[start..start + entries.len()];
        start += entries.len();
        let text: String = got.iter().enumerate().map(|(i, (d, _))| format!("{i} {d:016x}\n")).collect();
        let path = format!("{DIR}/{name}.digests");
        if reseed {
            std::fs::write(&path, &text).unwrap();
            continue;
        }
        // (The corpus is real code: nearly all of it is admitted as recorded, in production.)
        let admitted = got.iter().filter(|(_, a)| *a).count();
        if admitted * 100 < entries.len() * 95 {
            failures.push(format!("{name}: only {admitted} of {} recorded objects admitted", entries.len()));
        }
        let want = std::fs::read_to_string(&path).unwrap();
        let differ: Vec<&str> = text.lines().zip(want.lines()).filter(|(a, b)| a != b).map(|(a, _)| a).collect();
        if !differ.is_empty() || text.lines().count() != want.lines().count() {
            failures.push(format!(
                "{name}: admission's results differ from the recorded ones for {} of {} corpus entries (index, digest now): {:?}",
                differ.len(),
                entries.len(),
                &differ[..differ.len().min(10)]
            ));
        }
    }
    if reseed {
        panic!("reseeded {DIR}/{{{}}}.digests ({} entries): run again without NATEMU_ADMISSION_CORPUS_RESEED", CORPORA.join(","), all.len());
    }
    assert!(failures.is_empty(), "{}", failures.join("; "));
}
