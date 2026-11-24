//! EL0 sampler, the host's side (docs/restructure/el0-sampler-plan.md section 3; the records and files are
//! [`natemu_abi::sample`]'s): a run with `--profile NAME` rings each vCPU's sample bell every `--profile-us` microseconds
//! while sampling is on ([`Sampler::ring`]), drains the vCPUs' rings into `NAME.samples` every 250 ms ([`Sampler::drain`]),
//! and writes every answer it serves to `NAME.objects` ([`Objects`]), from the start of the run.
//!
//! **Ringing.** One thread, under the time-constraint policy, makes a pass every interval. Before each vCPU it reads the
//! on/off flag again (a pass in flight across a stop rings no vCPU after it), skips a vCPU whose resident does not sample
//! (no `Head::magic`: a resident built without its `profile` feature), and does not ring a halted one (`Events::halted`
//! set: a bell would end its halt and change its halt polling), recording a [`sample::HALTED`] sample instead. For the
//! others it stores its ring time (CNTVCT) in the vCPU's head, then rings. A pass that ran across a toggle, or the first
//! after one, is logged ([`sample::PASS`]).
//!
//! **Start and stop.** Sampling is on from the run's start, or (`--profile-start guest`) off until the guest's first start
//! through the control port ([`crate::profile_port`]); SIGUSR1 toggles it ([`on_signal`]). Each start or stop bumps a
//! sequence number and is logged ([`sample::TOGGLE`]).
use crate::execution::Blocks;
use natemu_abi::execution::cache;
use natemu_abi::sample::{self, HostRecord};
use std::collections::HashSet;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::SeqCst};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// How a run samples ([`crate::linux::Boot::profile`]).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Options {
    /// The files' common name: `NAME.samples` and `NAME.objects`.
    pub name: PathBuf,
    /// The ringing interval (default [`Options::INTERVAL_US`]).
    pub interval_us: u64,
    /// Sampling is on from the start (default); off: until the guest's first start through the control port.
    pub from_start: bool,
    /// Every answer served is written to `NAME.objects` (default); off: the file holds its header only (a measurement of
    /// what writing it costs).
    pub objects: bool,
}
impl Options {
    /// 4 kHz.
    pub const INTERVAL_US: u64 = 250;
    pub fn new(name: impl Into<PathBuf>) -> Self {
        Options {
            name: name.into(),
            interval_us: Self::INTERVAL_US,
            from_start: true,
            objects: true,
        }
    }
    /// `NAME.EXT`, the extension added to the whole name.
    fn file(&self, ext: &str) -> PathBuf {
        let mut s = self.name.clone().into_os_string();
        s.push(".");
        s.push(ext);
        s.into()
    }
}

/// How often the rings are drained.
const DRAIN: Duration = Duration::from_millis(250);

/// A run's sampler: the on/off flag the ringing thread reads, the host's records waiting for the next drain, the files.
pub struct Sampler {
    on: AtomicBool,
    seq: AtomicU64,
    interval_us: u64,
    n: usize,
    /// Host records ([`sample::HALTED`], [`sample::MARK`], [`sample::TOGGLE`], [`sample::PASS`]) for the next drain.
    pending: Mutex<Vec<[u64; 4]>>,
    samples: Mutex<Out>,
    pub(crate) objects: Arc<Objects>,
    /// [`Options::objects`]: the compiler writes the answers it serves to `objects`.
    pub(crate) objects_on: bool,
    /// Per vCPU: bells rung, halted samples recorded, records drained.
    rings: Vec<AtomicU64>,
    halted: Vec<AtomicU64>,
    drained: Vec<AtomicU64>,
    drains: AtomicU64,
    /// The control port's accesses, and those whose value it ignored.
    pub(crate) port_accesses: AtomicU64,
    pub(crate) port_ignored: AtomicU64,
    /// The performance log's snapshot, for a mark's `snap` line.
    snap: Option<Blocks>,
}
/// A file written in order, its first write error kept (reported once at the end).
struct Out {
    file: BufWriter<File>,
    path: PathBuf,
    error: Option<String>,
}
impl Out {
    fn create(path: PathBuf) -> crate::Result<Self> {
        let file = File::create(&path).map_err(|e| format!("--profile {}: {e}", path.display()))?;
        Ok(Out {
            file: BufWriter::with_capacity(1 << 20, file),
            path,
            error: None,
        })
    }
    fn write(&mut self, bytes: &[u8]) {
        if self.error.is_none() {
            if let Err(e) = self.file.write_all(bytes) {
                self.error = Some(e.to_string());
            }
        }
    }
    fn record(&mut self, w: &[u64; 4]) {
        self.write(&sample::to_bytes(w));
    }
    fn flush(&mut self) {
        if self.error.is_none() {
            if let Err(e) = self.file.flush() {
                self.error = Some(e.to_string());
            }
        }
    }
}

impl Sampler {
    /// The files created (`NAME.samples` with its header, `NAME.objects` with its), sampling on or off as `options` says.
    /// `snap`: the vCPUs' blocks, for a mark's `snap` line when the performance log is on.
    pub(crate) fn new(options: &Options, n: usize, snap: Option<Blocks>) -> crate::Result<Arc<Self>> {
        if options.interval_us == 0 {
            return Err("--profile-us: at least 1".into());
        }
        let mut samples = Out::create(options.file("samples"))?;
        samples.record(&sample::file_header(
            crate::clock::frequency(),
            n as u32,
            options.interval_us.min(u64::from(u32::MAX)) as u32,
        ));
        let objects = Objects::create(options.file("objects"))?;
        let counters = || (0..n).map(|_| AtomicU64::new(0)).collect::<Vec<_>>();
        Ok(Arc::new(Sampler {
            on: AtomicBool::new(options.from_start),
            seq: AtomicU64::new(0),
            interval_us: options.interval_us,
            n,
            pending: Mutex::default(),
            samples: Mutex::new(samples),
            objects: Arc::new(objects),
            objects_on: options.objects,
            rings: counters(),
            halted: counters(),
            drained: counters(),
            drains: AtomicU64::new(0),
            port_accesses: AtomicU64::new(0),
            port_ignored: AtomicU64::new(0),
            snap,
        }))
    }
    pub fn on(&self) -> bool {
        self.on.load(SeqCst)
    }
    #[cfg(test)]
    pub(crate) fn pending_for_tests(&self) -> Vec<[u64; 4]> {
        self.pending.lock().unwrap().clone()
    }
    fn host(&self, r: HostRecord) {
        self.pending.lock().unwrap_or_else(|e| e.into_inner()).push(r.encode());
    }
    /// Turns sampling on or off (`cause`: [`sample::BY_PORT`] and the writing vCPU, or [`sample::BY_SIGNAL`]): the flag,
    /// then the sequence number, logged with the time of the store.
    pub(crate) fn set(&self, on: bool, cause: u32, vcpu: u32) {
        self.on.store(on, SeqCst);
        let seq = self.seq.fetch_add(1, SeqCst) + 1;
        let time = crate::clock::now();
        self.host(HostRecord {
            kind: sample::TOGGLE,
            time,
            value1: seq,
            value2: u64::from(on),
            value3: cause,
            value4: vcpu,
        });
    }
    /// A mark the guest wrote (number `n`, vCPU `vcpu`, at the host's CNTVCT `time`), and with the performance log on a
    /// `snap` line of the perf buckets, so they split by phase too.
    pub(crate) fn mark(&self, vcpu: usize, n: u8, time: u64) {
        self.host(HostRecord {
            kind: sample::MARK,
            time,
            value1: u64::from(n),
            value3: vcpu as u32,
            ..HostRecord::default()
        });
        if let Some(blocks) = self.snap.as_ref().filter(|_| crate::perf::on()) {
            crate::perf::line(&blocks.snap());
        }
    }
    /// Before the vCPUs run: each one's bell INTID in its head (`intid(v)`), the first clock pair and the starting state.
    pub(crate) fn begin(&self, blocks: &Blocks, intid: impl Fn(usize) -> u32) {
        for v in 0..self.n {
            blocks
                .word(v, study(sample::HEAD_BELL))
                .store(u64::from(intid(v)), SeqCst);
        }
        self.clock(&mut self.samples.lock().unwrap_or_else(|e| e.into_inner()));
        self.host(HostRecord {
            kind: sample::TOGGLE,
            time: crate::clock::now(),
            value1: 0,
            value2: u64::from(self.on()),
            value3: sample::BY_START,
            value4: u32::MAX,
        });
    }
    fn clock(&self, out: &mut Out) {
        let wall = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos() as u64);
        out.record(
            &HostRecord {
                kind: sample::CLOCK,
                time: crate::clock::now(),
                value1: wall,
                value2: self.drains.fetch_add(1, SeqCst),
                ..HostRecord::default()
            }
            .encode(),
        );
    }
    /// The ringing thread's loop, until `done`: one pass per interval while sampling is on.
    pub(crate) fn ring(&self, blocks: &Blocks, bell: crate::vm::Bell, done: &AtomicBool) {
        crate::clock::time_constraint();
        let period = Duration::from_micros(self.interval_us);
        let (mut next, mut signals, mut logged) = (Instant::now(), SIGNALS.load(SeqCst), u64::MAX);
        while !done.load(SeqCst) {
            let s = SIGNALS.load(SeqCst);
            if s != signals {
                signals = s;
                self.set(!self.on(), sample::BY_SIGNAL, u32::MAX);
            }
            self.pass(blocks, |v| bell.ring(v).is_ok(), &mut logged);
            next += period;
            let now = Instant::now();
            match next.checked_duration_since(now) {
                Some(wait) => std::thread::sleep(wait),
                None => next = now,
            }
        }
    }
    /// One ringing pass (`ring` rings vCPU v's bell; `logged`: the sequence number the last logged pass ended under).
    fn pass(&self, blocks: &Blocks, mut ring: impl FnMut(usize) -> bool, logged: &mut u64) {
        let (seq, start) = (self.seq.load(SeqCst), crate::clock::now());
        let mut rung = 0;
        for v in 0..self.n {
            if !self.on() {
                break;
            }
            if blocks.word(v, study(sample::HEAD_MAGIC)).load(SeqCst) != sample::MAGIC {
                continue;
            }
            if crate::execution::park_state(blocks.memory(), v).0 != 0 {
                self.halted[v].fetch_add(1, SeqCst);
                self.host(HostRecord {
                    kind: sample::HALTED,
                    time: crate::clock::now(),
                    value1: seq,
                    value3: v as u32,
                    ..HostRecord::default()
                });
                continue;
            }
            blocks
                .word(v, study(sample::HEAD_RING_TIME))
                .store(crate::clock::now(), SeqCst);
            if ring(v) {
                self.rings[v].fetch_add(1, SeqCst);
                rung += 1;
            }
        }
        let end_seq = self.seq.load(SeqCst);
        if end_seq != seq || seq != *logged {
            self.host(HostRecord {
                kind: sample::PASS,
                time: start,
                value1: crate::clock::now(),
                value2: seq,
                value3: end_seq as u32,
                value4: rung,
            });
        }
        *logged = end_seq;
    }
    /// The drain thread's loop: every 250 ms until `done`, then the last drain and the counters.
    pub(crate) fn drain_until(&self, blocks: &Blocks, done: &AtomicBool) {
        let mut next = Instant::now() + DRAIN;
        while !done.load(SeqCst) {
            let now = Instant::now();
            if now >= next {
                self.drain(blocks);
                next = now + DRAIN;
            }
            std::thread::sleep(Duration::from_millis(10).min(next.saturating_duration_since(now)));
        }
        self.drain(blocks);
        self.finish(blocks);
    }
    /// One drain: a clock pair, each vCPU's new records (after a [`sample::VCPU`] record), then the host's own.
    pub(crate) fn drain(&self, blocks: &Blocks) {
        let mut out = self.samples.lock().unwrap_or_else(|e| e.into_inner());
        self.clock(&mut out);
        let mut records = Vec::new();
        for v in 0..self.n {
            if blocks.word(v, study(sample::HEAD_MAGIC)).load(SeqCst) != sample::MAGIC {
                continue;
            }
            // SAFETY: vCPU v's study part holds its head and ring (`sample::BYTES`); this thread is its only reader.
            let ring = unsafe { sample::Ring::at(blocks.ptr(v, study(0), sample::BYTES)) };
            records.clear();
            let n = ring.drain(|r| records.push(r));
            if n == 0 {
                continue;
            }
            self.drained[v].fetch_add(n, SeqCst);
            let head = ring.head();
            out.record(
                &HostRecord {
                    kind: sample::VCPU,
                    time: crate::clock::now(),
                    value1: head.lost,
                    value2: head.write,
                    value3: v as u32,
                    value4: n as u32,
                }
                .encode(),
            );
            for r in &records {
                out.record(r);
            }
        }
        self.host_records(&mut out);
        out.flush();
    }
    /// The host's records made since the last drain, in time order.
    fn host_records(&self, out: &mut Out) {
        let mut host = std::mem::take(&mut *self.pending.lock().unwrap_or_else(|e| e.into_inner()));
        host.sort_by_key(|r| r[0]);
        for r in &host {
            out.record(r);
        }
    }
    /// The run's counters ([`sample::COUNT`]), and what went wrong on standard error.
    fn finish(&self, blocks: &Blocks) {
        let mut out = self.samples.lock().unwrap_or_else(|e| e.into_inner());
        let mut count = |v: u32, counter: u32, value: u64| {
            out.record(
                &HostRecord {
                    kind: sample::COUNT,
                    time: crate::clock::now(),
                    value1: value,
                    value3: v,
                    value4: counter,
                    ..HostRecord::default()
                }
                .encode(),
            )
        };
        let mut not_sampling = 0;
        for v in 0..self.n {
            // SAFETY: as in `drain`.
            let head = unsafe { sample::Ring::at(blocks.ptr(v, study(0), sample::BYTES)) }.head();
            if head.magic != sample::MAGIC {
                not_sampling += 1;
                continue;
            }
            let x = v as u32;
            count(x, sample::C_RINGS, self.rings[v].load(SeqCst));
            count(x, sample::C_HALTED, self.halted[v].load(SeqCst));
            count(x, sample::C_WRITTEN, head.write);
            count(x, sample::C_LOST, head.lost);
            count(x, sample::C_BELLS, head.bells);
            count(x, sample::C_BELLS_IN_WAIT, head.bells_in_wait);
            count(x, sample::C_UNKNOWN_CALLS, head.unknown_calls);
            count(x, sample::C_DRAINED, self.drained[v].load(SeqCst));
        }
        count(u32::MAX, sample::C_PORT_ACCESSES, self.port_accesses.load(SeqCst));
        count(u32::MAX, sample::C_PORT_IGNORED, self.port_ignored.load(SeqCst));
        count(u32::MAX, sample::C_NOT_SAMPLING, not_sampling);
        out.flush();
        if not_sampling != 0 {
            eprintln!(
                "natemu: --profile: {not_sampling} vCPU(s) never rung: their resident does not sample (built without its `profile` feature)"
            );
        }
        if let Some(e) = &out.error {
            eprintln!("natemu: --profile {}: write failed: {e}", out.path.display());
        }
        self.objects.finish();
    }
}
/// The offset of the sample head's field at `offset` in a vCPU's block.
fn study(offset: usize) -> usize {
    cache::LOOKUP_OFFSET + cache::PCID_STUDY + offset
}

/// SIGUSR1s taken ([`on_signal`]); the ringing thread toggles sampling on each.
static SIGNALS: AtomicU64 = AtomicU64::new(0);
/// The vCPU threads block SIGUSR1 ([`crate::execution`]'s `block_inspect_signal`), so it never lands in `hv_vcpu_run`.
pub(crate) static SIGNAL_TOGGLES: AtomicBool = AtomicBool::new(false);
pub(crate) const SIGUSR1: i32 = 30;
/// SIGUSR1 toggles sampling while the VM runs (the handler only counts). For a process's command-line entry; once.
pub fn on_signal() {
    extern "C" fn toggled(_: i32) {
        SIGNALS.fetch_add(1, SeqCst);
    }
    unsafe extern "C" {
        fn signal(signal: i32, handler: extern "C" fn(i32)) -> usize;
    }
    SIGNAL_TOGGLES.store(true, SeqCst);
    unsafe { signal(SIGUSR1, toggled) };
}

/// `NAME.objects`: every distinct answer served, once ([`natemu_abi::sample`] gives the layout).
pub struct Objects {
    inner: Mutex<(Out, HashSet<(u64, u64, u64, u64)>)>,
}
impl Objects {
    fn create(path: PathBuf) -> crate::Result<Self> {
        let mut out = Out::create(path)?;
        out.record(&[
            sample::OBJECTS_MAGIC,
            sample::VERSION | (sample::OBJECT_HEADER_BYTES as u64) << 32,
            0,
            0,
        ]);
        Ok(Objects {
            inner: Mutex::new((out, HashSet::new())),
        })
    }
    /// The answer `object` served to vCPU `vcpu` for `rip` (a step's when `step`) in `context`, translated from `guest`
    /// fetched at the guest-physical pages `fetch` (`State::fetch`): written unless the same (RIP, frame, context, bytes)
    /// was already.
    pub(crate) fn served(&self, vcpu: usize, step: bool, rip: u64, fetch: [u64; 2], context: u64, object: &[u8], guest: &[u8]) {
        let hash = sample::hash(object);
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if !inner.1.insert((rip, fetch[0], context, hash)) {
            return;
        }
        let length = (sample::OBJECT_HEADER_BYTES + object.len() + guest.len()).next_multiple_of(8);
        let h = sample::ObjectHeader {
            length: length as u32,
            source: if step { sample::SERVED_STEP } else { sample::SERVED },
            vcpu: vcpu as u8,
            reserved: 0,
            rip,
            frame0: fetch[0],
            frame1: fetch[1],
            context,
            hash,
            object_bytes: object.len() as u32,
            guest_bytes: guest.len() as u32,
        };
        let mut record = Vec::with_capacity(length);
        record.extend_from_slice(&h.length.to_le_bytes());
        record.extend_from_slice(&[h.source, h.vcpu]);
        record.extend_from_slice(&h.reserved.to_le_bytes());
        for w in [h.rip, h.frame0, h.frame1, h.context, h.hash] {
            record.extend_from_slice(&w.to_le_bytes());
        }
        record.extend_from_slice(&h.object_bytes.to_le_bytes());
        record.extend_from_slice(&h.guest_bytes.to_le_bytes());
        debug_assert_eq!(record.len(), sample::OBJECT_HEADER_BYTES);
        record.extend_from_slice(object);
        record.extend_from_slice(guest);
        record.resize(length, 0);
        inner.0.write(&record);
    }
    fn finish(&self) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.0.flush();
        if let Some(e) = &inner.0.error {
            eprintln!("natemu: --profile {}: write failed: {e}", inner.0.path.display());
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use natemu_abi::sample::Sample;

    fn temp(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("natemu-sampler-{name}-{}", std::process::id()))
    }

    /// The samples file's records, decoded as host records (the resident's are compared word by word).
    fn records(path: &std::path::Path) -> Vec<[u64; 4]> {
        std::fs::read(path)
            .unwrap()
            .chunks(32)
            .map(|c| std::array::from_fn(|k| u64::from_le_bytes(c[8 * k..8 * k + 8].try_into().unwrap())))
            .collect()
    }

    /// The host's side over two fake vCPU blocks, one with a sampling resident: `begin` gives each its bell's INTID; a pass
    /// rings the sampling one only (the other has no magic), records a halted one instead of ringing it, and is logged;
    /// each drain writes a VCPU record and that ring's records in order across the wrap, a full ring's lost count; the end
    /// writes the counters (rings, halted, written, lost, drained, the vCPU that never sampled).
    #[test]
    fn the_rings_are_drained_and_counted() {
        const BLOCK: usize = 2 << 20;
        let stride = (cache::GLOBAL_OFFSET + cache::GLOBAL_BYTES).next_multiple_of(BLOCK);
        let memory = Arc::new(crate::memory::Memory::with_size(2 * stride).unwrap());
        let blocks = Blocks::for_tests(memory.clone(), 2, stride);
        let name = temp("rings");
        let s = Sampler::new(&Options::new(&name), 2, None).unwrap();
        s.begin(&blocks, |v| 100 + v as u32);
        assert_eq!([0, 1].map(|v| blocks.word(v, study(sample::HEAD_BELL)).load(SeqCst)), [100, 101]);
        blocks.word(0, study(sample::HEAD_MAGIC)).store(sample::MAGIC, SeqCst); // vCPU 0's resident samples
        let (mut rung, mut logged) = (Vec::new(), u64::MAX);
        s.pass(&blocks, |v| {
            rung.push(v);
            true
        }, &mut logged);
        assert_eq!(rung, [0]);
        assert_ne!(blocks.word(0, study(sample::HEAD_RING_TIME)).load(SeqCst), 0, "the ring time before the ring");
        crate::execution::set_halted(&memory, 0, 1);
        s.pass(&blocks, |v| {
            rung.push(v);
            true
        }, &mut logged);
        assert_eq!(rung, [0], "a halted vCPU is not rung");
        crate::execution::set_halted(&memory, 0, 0);
        // The resident's side: three quarters of a ring, drained; then a lap and two more, which the full ring loses.
        let ring = unsafe { sample::Ring::at(blocks.ptr(0, study(0), sample::BYTES)) };
        let rec = |k: u64| Sample { time: k, rip: k, kind: sample::EL0, place: sample::BLOCK, ..Sample::default() }.encode();
        let first = (3 * sample::CAPACITY / 4) as u64;
        (0..first).for_each(|k| assert!(ring.push(rec(k))));
        s.drain(&blocks);
        (first..first + sample::CAPACITY as u64 + 2).for_each(|k| {
            ring.push(rec(k));
        });
        s.drain_until(&blocks, &AtomicBool::new(true)); // the last drain and the counters
        let r = records(&Options::new(&name).file("samples"));
        assert_eq!(r[0], sample::file_header(crate::clock::frequency(), 2, 250));
        let host = |w: &[u64; 4]| HostRecord::decode(*w);
        let vcpu: Vec<usize> = (0..r.len()).filter(|&i| sample::kind(&r[i]) == sample::VCPU).collect();
        assert_eq!(vcpu.len(), 2, "one per drain with new records");
        let (a, b) = (host(&r[vcpu[0]]), host(&r[vcpu[1]]));
        assert_eq!((a.value3, a.value4, a.value1, a.value2), (0, first as u32, 0, first));
        assert_eq!((b.value3, b.value4, b.value1), (0, sample::CAPACITY as u32, 2));
        let drained: Vec<u64> = vcpu
            .iter()
            .flat_map(|&i| r[i + 1..i + 1 + host(&r[i]).value4 as usize].iter().map(|w| Sample::decode(*w).time))
            .collect();
        assert_eq!(drained, (0..first + sample::CAPACITY as u64).collect::<Vec<_>>(), "in order, across the wrap");
        let kinds = |k: u8| r.iter().filter(|w| sample::kind(w) == k).count();
        assert_eq!((kinds(sample::HALTED), kinds(sample::TOGGLE)), (1, 1));
        assert!(kinds(sample::PASS) >= 1 && kinds(sample::CLOCK) >= 3);
        let count = |v: u32, c: u32| {
            r.iter().map(host).find(|h| h.kind == sample::COUNT && h.value3 == v && h.value4 == c).map(|h| h.value1)
        };
        assert_eq!(count(0, sample::C_RINGS), Some(1));
        assert_eq!(count(0, sample::C_HALTED), Some(1));
        assert_eq!(count(0, sample::C_WRITTEN), Some(first + sample::CAPACITY as u64));
        assert_eq!(count(0, sample::C_LOST), Some(2));
        assert_eq!(count(0, sample::C_DRAINED), Some(first + sample::CAPACITY as u64));
        assert_eq!(count(1, sample::C_RINGS), None, "vCPU 1 never sampled");
        assert_eq!(count(u32::MAX, sample::C_NOT_SAMPLING), Some(1));
        for ext in ["samples", "objects"] {
            let _ = std::fs::remove_file(Options::new(&name).file(ext));
        }
    }

    /// Each distinct (RIP, frame, context, bytes) is written once, in the abi's layout: a repeat is dropped, and an answer
    /// differing in any key part (its bytes included) is its own record.
    #[test]
    fn objects_are_written_once_each() {
        let name = temp("objects");
        let s = Sampler::new(&Options::new(&name), 1, None).unwrap();
        let o = &s.objects;
        let (object, guest) = (vec![7u8; 13], vec![0x90u8; 3]);
        o.served(0, false, 0x40_1000, [0x5000, u64::MAX], 3, &object, &guest);
        o.served(1, false, 0x40_1000, [0x5000, u64::MAX], 3, &object, &guest); // the same answer on another vCPU
        o.served(0, false, 0x40_1000, [0x6000, u64::MAX], 3, &object, &guest); // another frame
        o.served(0, false, 0x40_1000, [0x5000, u64::MAX], 4, &object, &guest); // another context
        o.served(0, true, 0x40_1000, [0x5000, u64::MAX], 3, &[8u8; 13], &guest); // other bytes (a step's)
        o.finish();
        let bytes = std::fs::read(Options::new(&name).file("objects")).unwrap();
        assert_eq!(bytes[..8], *b"NATEMUOB");
        let mut at = 32;
        let mut seen = Vec::new();
        while at < bytes.len() {
            let u32_at = |o: usize| u32::from_le_bytes(bytes[at + o..at + o + 4].try_into().unwrap());
            let u64_at = |o: usize| u64::from_le_bytes(bytes[at + o..at + o + 8].try_into().unwrap());
            let (length, n, g) = (u32_at(0) as usize, u32_at(48) as usize, u32_at(52) as usize);
            assert_eq!(length, (56 + n + g).next_multiple_of(8));
            let body = &bytes[at + 56..at + 56 + n];
            assert_eq!(u64_at(40), sample::hash(body));
            assert_eq!(&bytes[at + 56 + n..at + 56 + n + g], &guest[..]);
            seen.push((bytes[at + 4], bytes[at + 5], u64_at(8), u64_at(16), u64_at(24), u64_at(32)));
            at += length;
        }
        assert_eq!(at, bytes.len());
        assert_eq!(
            seen,
            [
                (sample::SERVED, 0, 0x40_1000, 0x5000, u64::MAX, 3),
                (sample::SERVED, 0, 0x40_1000, 0x6000, u64::MAX, 3),
                (sample::SERVED, 0, 0x40_1000, 0x5000, u64::MAX, 4),
                (sample::SERVED_STEP, 0, 0x40_1000, 0x5000, u64::MAX, 3),
            ]
        );
        for ext in ["samples", "objects"] {
            let _ = std::fs::remove_file(Options::new(&name).file(ext));
        }
    }

    /// The samples file starts with its header; toggles and marks reach it at the next drain, in time order, with a
    /// sequence number per toggle.
    #[test]
    fn toggles_and_marks_reach_the_samples_file() {
        let name = temp("samples");
        let mut options = Options::new(&name);
        options.from_start = false;
        let s = Sampler::new(&options, 2, None).unwrap();
        assert!(!s.on());
        s.set(true, sample::BY_PORT, 1);
        assert!(s.on());
        s.mark(1, 42, crate::clock::now());
        s.set(false, sample::BY_SIGNAL, u32::MAX);
        // A drain without vCPU blocks: the clock pair and the host's records.
        let mut out = s.samples.lock().unwrap();
        s.clock(&mut out);
        s.host_records(&mut out);
        out.flush();
        drop(out);
        let bytes = std::fs::read(options.file("samples")).unwrap();
        let records: Vec<[u64; 4]> = bytes
            .chunks(32)
            .map(|c| std::array::from_fn(|k| u64::from_le_bytes(c[8 * k..8 * k + 8].try_into().unwrap())))
            .collect();
        assert_eq!(records[0], sample::file_header(crate::clock::frequency(), 2, 250));
        let kinds: Vec<u8> = records.iter().map(sample::kind).collect();
        assert_eq!(kinds, [sample::FILE, sample::CLOCK, sample::TOGGLE, sample::MARK, sample::TOGGLE]);
        let on = HostRecord::decode(records[2]);
        assert_eq!((on.value1, on.value2, on.value3, on.value4), (1, 1, sample::BY_PORT, 1));
        let mark = HostRecord::decode(records[3]);
        assert_eq!((mark.value1, mark.value3), (42, 1));
        let off = HostRecord::decode(records[4]);
        assert_eq!((off.value1, off.value2, off.value3), (2, 0, sample::BY_SIGNAL));
        assert!(records[2][0] <= records[3][0] && records[3][0] <= records[4][0]);
        for ext in ["samples", "objects"] {
            let _ = std::fs::remove_file(options.file(ext));
        }
    }
}
