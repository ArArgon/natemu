//! Performance phase, first chunk (`docs/restructure/perf-plan.md` §2): a timing log of a run, off unless the embedder
//! calls [`enable`] (the test driver's `boot-linux` does when `NATEMU_PERF_LOG=FILE` is set). Off, the service loop
//! only tests whether its vCPU has a [`Recorder`]; nothing is timed or written.
//!
//! FILE gets one line per process phase (`mark NAME NS`, [`mark`]) and one JSON line per vCPU at its end (`vcpu {..}`:
//! host time by part and exit kind, the request parts, thread CPU time, its fexc server's CPU time from `wait4`, and the
//! resident's buckets when the resident was built with its `perf` feature). FILE.vN gets one line per translation request
//! of vCPU N ([`Recorder::request`]). Times are nanoseconds of `CLOCK_UPTIME_RAW`, which is `mach_absolute_time`: the
//! guest's CNTVCT (every vCPU's virtual timer offset is 0) and the resident's ticks are on the same clock.
use crate::Result;
use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

static LOG: OnceLock<(PathBuf, Mutex<File>)> = OnceLock::new();

unsafe extern "C" {
    fn clock_gettime_nsec_np(clock: u32) -> u64;
}
/// `mach_absolute_time` in nanoseconds.
pub fn now() -> u64 {
    const CLOCK_UPTIME_RAW: u32 = 8;
    unsafe { clock_gettime_nsec_np(CLOCK_UPTIME_RAW) }
}
/// The calling thread's CPU time (user and system), nanoseconds.
pub fn thread_cpu() -> u64 {
    const CLOCK_THREAD_CPUTIME_ID: u32 = 16;
    unsafe { clock_gettime_nsec_np(CLOCK_THREAD_CPUTIME_ID) }
}

/// Turns the log on for this process (once; FILE is created or truncated).
pub fn enable(path: impl Into<PathBuf>) -> Result<()> {
    let path = path.into();
    let file = File::create(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    LOG.set((path, Mutex::new(file))).map_err(|_| "the performance log is already on".to_string())
}
pub fn on() -> bool {
    LOG.get().is_some()
}
/// Writes that failed (FILE's lines and every FILE.vN's), reported once on standard error as they start and in each
/// vCPU's line ([`failed`]).
static FAILED: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
fn write_failed(e: &std::io::Error) {
    if FAILED.fetch_add(1, std::sync::atomic::Ordering::Relaxed) == 0 {
        eprintln!("natemu: performance log write failed (later failures only counted): {e}");
    }
}
/// The log's failed writes so far.
pub fn failed() -> u64 {
    FAILED.load(std::sync::atomic::Ordering::Relaxed)
}
/// Appends a line to FILE (when on).
pub fn line(text: &str) {
    if let Some((_, file)) = LOG.get() {
        if let Err(e) = writeln!(file.lock().unwrap_or_else(|e| e.into_inner()), "{text}") {
            write_failed(&e);
        }
    }
}
/// The most each FILE.vN holds; later request lines are counted, not written (`dropped`).
pub const REQUEST_BYTES: u64 = 1 << 30;
/// A process phase: `mark NAME NS`.
pub fn mark(name: &str) {
    if on() {
        line(&format!("mark {name} {}", now()));
    }
}

/// Host-side parts of a vCPU thread's time between its body's start and the end of its service loop, in loop order:
/// the cancel test and the watchdog/stop bookkeeping around each entry (`signal`), `hv_vcpu_run` (`run`), reading the
/// exit and the records (`exit`), then serving each request kind (the record written back included).
pub const SIGNAL: usize = 0;
pub const RUN: usize = 1;
pub const EXIT: usize = 2;
pub const TRANSLATE: usize = 3;
pub const STEP: usize = 4;
pub const DEVICE: usize = 5;
pub const PAGE: usize = 6;
pub const PORT: usize = 7;
pub const IRQ: usize = 8;
pub const ENTROPY: usize = 9;
/// Writing the request's line (the log's own cost).
pub const LOG_LINE: usize = 10;
const HOST: [&str; 11] = ["signal", "run", "exit", "translate", "step", "device", "page", "port", "irq", "entropy", "log"];
/// Parts of a translation request: the guest bytes' copy, the in-memory cache's lookup, the persistent cache's get (the
/// request body and its SHA-256 key included), fexc's round trip (its server's start included), the persistent put (a
/// send to its writer thread), admission, a system instruction's decode, staging (the object and its link points), and
/// keeping a new answer in memory (its bytes copied, a no-code report parsed), and (perf chunk 7) relocating an answer kept
/// for another RIP (`admit` is then the relocated object's admission alone; not in the request line, which predates it).
pub const FETCH: usize = 0;
pub const LOOKUP: usize = 1;
pub const DISK_GET: usize = 2;
pub const FEXC: usize = 3;
pub const DISK_PUT: usize = 4;
pub const ADMIT: usize = 5;
pub const DECODE: usize = 6;
pub const STAGE: usize = 7;
pub const KEEP: usize = 8;
pub const RELOCATE: usize = 9;
const PARTS: [&str; 10] = ["fetch", "lookup", "disk_get", "fexc", "disk_put", "admit", "decode", "stage", "keep", "relocate"];

/// One vCPU's log while it runs (`Compiler::perf`).
pub struct Recorder {
    vcpu: usize,
    requests: BufWriter<File>,
    /// Bytes written to FILE.vN, and request lines past [`REQUEST_BYTES`] not written.
    written: u64,
    dropped: u64,
    start: u64,
    cpu: u64,
    /// When the record was made (the laps start there) and the last lap.
    created: u64,
    last: u64,
    first: bool,
    host: [u64; HOST.len()],
    exits: [u64; HOST.len()],
    parts: [u64; PARTS.len()],
    /// The request being served: its parts, its answers by source (memory, disk, fexc) and the bytes its answer read.
    part: [u64; PARTS.len()],
    pub(crate) sources: [u32; 3],
    pub(crate) read: (u64, u32),
    /// Perf chunk 6: the request's block could be the resident's reuse template (design: not a step, no address or
    /// halving cut, the answer read neither to the end of its bytes nor beyond the block's pages, not `unsupported_form`);
    /// `halved`: its compile was retried with fewer instructions.
    pub(crate) template: bool,
    pub(crate) halved: bool,
    /// Compile-ahead measurement: an admitted block's guest bytes, whether its last instruction is a call, and its exit
    /// candidates (link targets, and a call's return site) with whether each lies on a page the request fetched.
    pub(crate) block: (u32, bool),
    pub(crate) candidates: Vec<(u64, bool)>,
    results: [u64; 5],
    by_source: [u64; 3],
}
impl Recorder {
    /// For vCPU `vcpu`, whose body started at `start` (now on the thread's clock).
    pub fn new(vcpu: usize, start: u64) -> Result<Option<Box<Self>>> {
        let Some((path, _)) = LOG.get() else { return Ok(None) };
        let path = PathBuf::from(format!("{}.v{vcpu}", path.display()));
        let file = File::create(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        let t = now();
        Ok(Some(Box::new(Self {
            vcpu,
            requests: BufWriter::with_capacity(1 << 20, file),
            written: 0,
            dropped: 0,
            start,
            cpu: thread_cpu(),
            created: t,
            last: t,
            first: true,
            host: [0; HOST.len()],
            exits: [0; HOST.len()],
            parts: [0; PARTS.len()],
            part: [0; PARTS.len()],
            sources: [0; 3],
            read: (0, 0),
            template: false,
            halved: false,
            block: (0, false),
            candidates: Vec::new(),
            results: [0; 5],
            by_source: [0; 3],
        })))
    }
    /// Charges the time since the last lap to host part `k`.
    pub(crate) fn lap(&mut self, k: usize) {
        let t = now();
        self.host[k] += t - self.last;
        self.exits[k] += 1;
        self.last = t;
        if self.first && k == SIGNAL {
            self.first = false;
            mark(&format!("first_entry{}", self.vcpu));
        }
    }
    /// Adds the time since `since` to the current request's part `k`.
    pub(crate) fn add(&mut self, k: usize, since: u64) {
        self.part[k] += now() - since;
    }
    /// The served request's line: vCPU, CR3, RIP, guest-physical page, result (B block, S system decode, N next page,
    /// F fetch fault, P park), answers from memory/disk/fexc, bytes read and their hash, then nanoseconds of each part
    /// ([`PARTS`] order) and of the whole request; then (perf chunk 6) the resident's cache flushes before it
    /// (`State::generation`), the code context, 1 if the block could be a reuse template ([`Self::template`]) else 0, and
    /// the fetch's second guest-physical page (`ffffffffffffffff`: none); then (compile-ahead measurement) the request's
    /// start (ns), 1 if a step request else 0, the admitted block's guest bytes (0: none), 1 if it ends in a call else 0,
    /// and its exit candidates `target:F` (F = 1 when on a page the request fetched), comma-separated, or `-`.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn request(&mut self, cr3: u64, rip: u64, gpa: u64, reply: u64, began: u64, step: bool, generation: u64, context: u64, gpa2: u64) {
        let total = now() - began;
        use natemu_abi::execution as exec;
        let result = match reply {
            exec::REPLY_ADMITTED => 0,
            exec::REPLY_SYSTEM => 1,
            exec::REPLY_NEXT_PAGE => 2,
            exec::REPLY_FETCH_FAULT => 3,
            _ => 4,
        };
        self.results[result] += 1;
        let s = self.sources;
        let source = if s[2] > 0 { 2 } else if s[1] > 0 { 1 } else { 0 };
        if s.iter().any(|&n| n > 0) {
            self.by_source[source] += 1;
        }
        let p = self.part;
        let candidates = match self.candidates.is_empty() {
            true => "-".to_string(),
            false => self.candidates.iter().map(|&(t, on)| format!("{t:x}:{}", u8::from(on))).collect::<Vec<_>>().join(","),
        };
        let line = format!(
            "{} {cr3:x} {rip:x} {gpa:x} {} {} {} {} {} {:x} {} {} {} {} {} {} {} {} {} {total} {generation} {context:x} {} {gpa2:x} {began} {} {} {} {candidates}",
            self.vcpu, ["B", "S", "N", "F", "P"][result], s[0], s[1], s[2], self.read.1, self.read.0, p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8],
            u8::from(self.template), u8::from(step), self.block.0, u8::from(self.block.1)
        );
        if self.written + line.len() as u64 + 1 > REQUEST_BYTES {
            self.dropped += 1;
        } else if let Err(e) = writeln!(self.requests, "{line}") {
            write_failed(&e);
        } else {
            self.written += line.len() as u64 + 1;
        }
        for (t, q) in self.parts.iter_mut().zip(p) {
            *t += q;
        }
        (self.part, self.sources, self.read, self.template, self.halved, self.block) = ([0; PARTS.len()], [0; 3], (0, 0), false, false, (0, false));
        self.candidates.clear();
    }
    /// The vCPU's line: `end` = its service loop's end; `fexc` = its server's (user, system) CPU ns and maximum resident
    /// bytes; `resident` = the resident's record and the K1 statistics (`execution::k1_stats`).
    /// Returns the log's failed writes so far (this line's included) and the request lines this vCPU dropped at the cap.
    pub(crate) fn finish(mut self, end: u64, fexc: Option<(u64, u64, u64)>, resident: &natemu_abi::execution::perf::Perf, wakes: &natemu_abi::execution::perf::Wakes, k1: &str, state: &str) -> (u64, u64) {
        if let Err(e) = self.requests.flush() {
            write_failed(&e);
        }
        let (cpu, done) = (thread_cpu() - self.cpu, now());
        let pairs = |names: &[&str], ns: &[u64], n: &[u64]| -> String {
            names.iter().zip(ns).zip(n).map(|((k, t), c)| format!("\"{k}\":[{t},{c}]")).collect::<Vec<_>>().join(",")
        };
        let parts: Vec<String> = PARTS.iter().zip(self.parts).map(|(k, t)| format!("\"{k}\":{t}")).collect();
        let fexc = fexc.map_or("null".into(), |(u, s, rss)| format!("{{\"user_ns\":{u},\"sys_ns\":{s},\"max_rss\":{rss}}}"));
        use natemu_abi::execution::perf as rp;
        let ticks: Vec<u64> = resident.ticks.iter().map(|&t| crate::execution::ticks_ns(t)).collect();
        let buckets = pairs(&rp::NAMES, &ticks, &resident.count);
        // Perf chunk 14: halt wake-ups, [count, total ns, max ns, [bins]] by kind.
        let ns = crate::execution::ticks_ns;
        let wakes = (0..rp::WAKE_KINDS)
            .map(|k| format!("\"{}\":[{},{},{},{:?}]", rp::WAKE_NAMES[k], wakes.count[k], ns(wakes.ticks[k]), ns(wakes.max[k]), wakes.bins[k]))
            .collect::<Vec<_>>()
            .join(",");
        line(&format!(
            "vcpu {{\"vcpu\":{},\"start\":{},\"created\":{},\"last\":{},\"end\":{end},\"done\":{done},\"cpu_ns\":{cpu},\"host\":{{{}}},\"parts\":{{{}}},\"results\":{:?},\"sources\":{:?},\"fexc\":{fexc},\"resident\":{{\"magic\":{},\"since\":{},\"current\":{},\"buckets\":{{{buckets}}},\"wakes\":{{{wakes}}}}},\"k1\":{k1},\"state\":{state},\"log\":{{\"failed\":{},\"dropped\":{}}}}}",
            self.vcpu, self.start, self.created, self.last, pairs(&HOST, &self.host, &self.exits), parts.join(","), self.results, self.by_source,
            resident.magic == rp::MAGIC, crate::execution::ticks_ns(resident.since), resident.current, failed(), self.dropped
        ));
        (failed(), self.dropped)
    }
}
