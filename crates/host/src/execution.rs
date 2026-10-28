//! Host side of block execution (`natemu_abi::execution`): the execution mode's
//! private address space (code cache areas included), and the service loop that
//! answers the resident's translation requests, which arrive only on a cache miss:
//! fetch the guest bytes at RIP through the guest walk, compile one block with fexc
//! (`--profile slice1`, a persistent server), admit it and stage it. A step request
//! (self-modifying code) is the same with a one-instruction block. A watchdog
//! stops a vCPU that runs too long without a host exit.
use super::*;
use crate::admission;
use natemu_abi::execution::{self as exec, Cpu, State, cache};
use natemu_abi::guest_memory::{GuestRam, GuestTables};
use natemu_abi::paging::{self, Access, Fault};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU64, Ordering::SeqCst};
use std::time::Duration;

/// Guest-physical RAM of the execution mode, through the host's alias.
pub(crate) fn guest_ram(memory: &Memory) -> GuestRam {
    assert!(memory.1 >= exec::BACKING_BYTES);
    let host = memory.0 as u64 + exec::GUEST_RAM_OFFSET as u64;
    unsafe { GuestRam::new(0, exec::GUEST_RAM_BYTES as u64, host) }
}

/// The execution mode's backing for `n` vCPUs. Block 0 holds vCPU 0's window, guest RAM, its lookup area, the global
/// area, its metadata arena and its code area (the `exec`/`cache` offsets); block v (`v * stride` further) holds vCPU
/// v's own window, lookup area, metadata arena and code area at the same offsets and leaves the ranges of guest RAM
/// and the global area unused (never mapped, so never backed): every vCPU maps the shared ones of block 0.
pub(crate) struct Backing {
    pub(crate) memory: Memory,
    pub(crate) n: usize,
    stride: usize,
}
impl Backing {
    /// vCPU `v`'s view.
    pub(crate) fn vcpu(&self, v: usize) -> Area<'_> {
        assert!(v < self.n);
        Area { memory: &self.memory, vcpu: v, base: v * self.stride }
    }
}
/// One vCPU's own part of the backing: offsets are those of block 0 (`exec`/`cache`), relocated to its block.
#[derive(Clone, Copy)]
pub(crate) struct Area<'a> {
    pub(crate) memory: &'a Memory,
    pub(crate) vcpu: usize,
    /// Backing offset of its block (its window).
    pub(crate) base: usize,
}
impl Area<'_> {
    pub(crate) fn read_record<T: Copy>(&self, offset: usize) -> T {
        self.memory.read_record(self.base + offset)
    }
    pub(crate) fn write_record<T>(&self, offset: usize, value: T) {
        self.memory.write_record(self.base + offset, value)
    }
    pub(crate) fn read<const N: usize>(&self, offset: usize) -> [u8; N] {
        self.memory.read(self.base + offset)
    }
    pub(crate) fn copy(&self, offset: usize, bytes: &[u8]) {
        self.memory.copy(self.base + offset, bytes)
    }
}

/// Backing for the execution mode with `n` vCPUs (1..=`cache::MAX_VCPUS`): the resident loaded into each vCPU's
/// window, each vCPU's private address space and shared record in place; guest RAM is zero. Each vCPU's code cache
/// holds `code_bytes` of generated code (at most `cache::CODE_BYTES_MAX`).
pub(crate) fn prepare(resident: &str, code_bytes: usize, n: usize) -> Result<Backing> {
    if !(1..=cache::MAX_VCPUS).contains(&n) {
        return Err(format!("{n} vCPUs: the execution mode runs 1 to {}", cache::MAX_VCPUS));
    }
    if !(cache::MAX_BLOCK_BYTES..=cache::CODE_BYTES_MAX).contains(&code_bytes) {
        return Err(format!(
            "code cache of {code_bytes} bytes: it must hold the largest block ({} bytes) and at most {} bytes",
            cache::MAX_BLOCK_BYTES,
            cache::CODE_BYTES_MAX
        ));
    }
    let code_mapped = code_bytes.next_multiple_of(cache::BLOCK);
    let stride = exec::BACKING_BYTES + code_mapped;
    let memory = Memory::with_size(n * stride)?;
    load_resident(&memory, resident)?;
    for v in 0..n {
        let base = v * stride;
        if v > 0 {
            memory.replicate_resident_text(base);
        }
        let mut spans = resident_text(base);
        let own = |offset, bytes| Span::data(offset, bytes, base + offset);
        let shared = |offset, bytes| Span::data(offset, bytes, offset);
        spans.extend([
            own(SHARED_OFFSET, 4096),
            own(exec::STATE_OFFSET, 4096),
            own(exec::FRAME_OFFSET, exec::frame::BYTES),
            Span::text(exec::SLOT_OFFSET, exec::SLOT_BYTES, base + exec::SLOT_OFFSET),
            Span::data((exec::SLOT_WRITE_VA - PRIVATE) as usize, exec::SLOT_BYTES, base + exec::SLOT_OFFSET),
            own((STACK_TOP - PRIVATE) as usize - exec::STACK_BYTES, exec::STACK_BYTES),
            own((EXCEPTION_STACK_TOP - PRIVATE) as usize - 0x4000, 0x4000),
            own(exec::STAGING_OFFSET, exec::STAGING_BYTES),
            shared(exec::GUEST_RAM_OFFSET, exec::GUEST_RAM_BYTES),
            Span::text((cache::CODE_VA - PRIVATE) as usize, code_mapped, base + cache::CODE_OFFSET),
            Span::data((cache::CODE_WRITE_VA - PRIVATE) as usize, code_mapped, base + cache::CODE_OFFSET),
            Span::data((cache::META_VA - PRIVATE) as usize, cache::META_BYTES, base + cache::META_OFFSET),
            Span::data((cache::LOOKUP_VA - PRIVATE) as usize, cache::LOOKUP_BYTES, base + cache::LOOKUP_OFFSET),
            Span::data((cache::GLOBAL_VA - PRIVATE) as usize, cache::GLOBAL_BYTES, cache::GLOBAL_OFFSET),
        ]);
        private_space(&memory, base, &spans, &[], true);
        // SAFETY: Control is plain integers; the resident owns everything but the capacity.
        let mut control: cache::Control = unsafe { std::mem::zeroed() };
        control.code_bytes = code_bytes as u64;
        memory.write_record(base + cache::LOOKUP_OFFSET + cache::CONTROL, control);
        // SAFETY: Shared is plain integers; all-zero is its empty value.
        let mut shared: Shared = unsafe { std::mem::zeroed() };
        shared.mode = exec::MODE;
        memory.write_record(base + SHARED_OFFSET, shared);
    }
    Ok(Backing { memory, n, stride })
}
/// Sets the vCPU's initial architectural state (before it first runs).
pub(crate) fn set_cpu(area: Area, cpu: Cpu) {
    area.write_record(exec::STATE_OFFSET, State::new(area.vcpu as u64, cpu));
}
/// Enters the vCPU at the resident in its window, its tables tagged with its own ASID (its number + 1; ASID 0 is the
/// untagged world of the regression families).
pub(crate) fn enter(cpu: &Vcpu, area: Area) -> Result<()> {
    cpu.enter_resident_with_asid(area.base, natemu_abi::shadow::TCR_BOTH, area.vcpu as u8 + 1)
}

/// The global area word at `offset` (shared by all vCPUs; atomic access only).
fn global_u64(memory: &Memory, offset: usize) -> &AtomicU64 {
    let at = cache::GLOBAL_OFFSET + offset;
    assert!(at % 8 == 0 && at + 8 <= memory.1);
    unsafe { AtomicU64::from_ptr(memory.0.add(at).cast()) }
}
/// vCPU `v`'s invalidation record: `pending`, or `stale` word `i`.
fn record_word(memory: &Memory, v: usize, i: Option<usize>) -> &AtomicU64 {
    let record = cache::RECORDS + v * size_of::<cache::Record>();
    global_u64(memory, record + i.map_or(0, |i| std::mem::offset_of!(cache::Record, stale) + 8 * i))
}
/// Guest RAM was written by the host (only while every vCPU is stopped): each vCPU drops its cached code before it
/// next runs guest code.
pub(crate) fn guest_ram_written(memory: &Memory) {
    for v in 0..cache::MAX_VCPUS {
        record_word(memory, v, None).fetch_or(cache::FLUSH, SeqCst);
    }
}
/// The host wrote guest-physical `gpa` (a fetch walk's A bit) while vCPUs may run: clears every vCPU's code bit of
/// the page and posts the page to each that had one (as the resident does for its own writes).
fn page_written(memory: &Memory, gpa: u64) {
    let page = (gpa >> 12) as usize;
    if page >= exec::GUEST_PAGES {
        return;
    }
    let mask = unsafe { AtomicU8::from_ptr(memory.0.add(cache::GLOBAL_OFFSET + cache::CODE_PAGES + page)) };
    let holders = mask.swap(0, SeqCst);
    for v in (0..cache::MAX_VCPUS).filter(|v| holders & 1 << v != 0) {
        record_word(memory, v, Some(page / 64)).fetch_or(1 << (page % 64), SeqCst);
        record_word(memory, v, None).fetch_or(cache::PAGES, SeqCst);
    }
}

/// The vCPU's placed blocks, in placement order (read from its metadata arena).
pub(crate) fn placed_blocks(area: Area) -> Vec<cache::Block> {
    let control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    (0..control.blocks as usize)
        .map(|n| {
            let meta = u32::from_le_bytes(area.read::<4>(cache::LOOKUP_OFFSET + cache::INDEX + 4 * n));
            area.read_record(cache::META_OFFSET + meta as usize)
        })
        .collect()
}
/// The link record `i` (written by the resident's linker).
pub(crate) fn link_record(area: Area, i: usize) -> cache::LinkRecord {
    area.read_record(cache::LOOKUP_OFFSET + cache::LINKS + i * size_of::<cache::LinkRecord>())
}

/// Self-check of the vCPU's cache after a run (the contract's rule (d), independent of the resident): every link point of
/// every placed block holds its admitted word (`ldr x0, L`, L holding its target) or `b` to the entry of a placed block
/// keyed (target, the block's context), and the resident's link records describe exactly the linked ones. Returns
/// (link points, linked).
pub(crate) fn check_links(area: Area) -> Result<(u64, u64)> {
    let lookup = |offset: usize| cache::LOOKUP_OFFSET + offset;
    let control: cache::Control = area.read_record(lookup(cache::CONTROL));
    let word = |offset: u64| u32::from_le_bytes(area.read::<4>(cache::CODE_OFFSET + offset as usize));
    let blocks = placed_blocks(area);
    let mut linked = HashMap::new();
    let mut points = 0;
    for b in &blocks {
        for p in b.links.iter().filter(|p| p.offset != 0) {
            points += 1;
            let at = u64::from(b.code) + u64::from(p.offset);
            let w = word(at);
            let bad = || format!("link point {:#x} of the block at {:#x}: word {w:#010x}", p.offset, b.rip);
            if w & 0xff00_001f == 0x5800_0000 {
                let literal = at.wrapping_add((((w << 8) as i32 >> 13) as i64 * 4) as u64);
                let value = u64::from(word(literal)) | u64::from(word(literal + 4)) << 32;
                if value != p.target || literal < u64::from(b.code) || literal + 8 > u64::from(b.code + b.code_size) {
                    return Err(bad());
                }
            } else if w & 0xfc00_0000 == 0x1400_0000 {
                let to = at.wrapping_add((((w << 6) as i32 >> 6) as i64 * 4) as u64);
                match blocks.iter().position(|t| u64::from(t.code) + u64::from(exec::ENTRY_OFFSET) == to) {
                    Some(t) if blocks[t].rip == p.target && blocks[t].context == b.context => linked.insert((b.number, p.offset), t),
                    _ => return Err(bad()),
                };
            } else {
                return Err(bad());
            }
        }
    }
    let records: Vec<cache::LinkRecord> = (0..control.links as usize).map(|i| link_record(area, i)).collect();
    // Each record also holds the point's original `ldr x0, L`, whose literal L (inside the block) holds the target.
    let described = records.iter().all(|r| {
        let Some(b) = blocks.get(r.source as usize) else { return false };
        let at = u64::from(b.code) + u64::from(r.offset);
        let literal = at.wrapping_add((((r.original << 8) as i32 >> 13) as i64 * 4) as u64);
        let target = u64::from(word(literal)) | u64::from(word(literal + 4)) << 32;
        linked.get(&(r.source, r.offset)) == Some(&(r.target as usize))
            && r.original & 0xff00_001f == 0x5800_0000
            && literal >= u64::from(b.code)
            && literal + 8 <= u64::from(b.code + b.code_size)
            && blocks.get(r.target as usize).is_some_and(|t| t.rip == target)
    });
    if !described || records.len() != linked.len() {
        return Err(format!("{} link records for {} linked points", records.len(), linked.len()));
    }
    Ok((points, linked.len() as u64))
}

/// Why execution stopped.
#[derive(Debug)]
pub(crate) enum Stop {
    /// RIP reached the caller's stop address at a block boundary.
    Reached,
    /// The instruction fetch at RIP faults; the bank holds the state at RIP (no IDT delivery yet).
    FetchFault(Fault),
    /// fexc translated nothing at RIP; its end reason (e.g. `system_instruction`).
    NoCode(String),
    /// The resident stopped the VM: `State::stop` (0 for a non-execution stop) and the shared status/detail.
    Resident { stop: u64, status: u64, detail: u64 },
    /// The watchdog forced the vCPU out (imprecise: the bank is not current).
    Watchdog,
    /// A peer's failure cancelled the run (imprecise when the vCPU was forced out).
    Cancelled,
}

/// One `fexc --serve` process (protocol in tools/fexc/fexc.cpp): length-prefixed requests and replies on its stdin/stdout.
struct Server {
    child: Child,
    stdin: ChildStdin,
    stdout: ChildStdout,
}
impl Server {
    fn start(fexc: &Path) -> Result<Self> {
        let mut child = Command::new(fexc)
            .arg("--serve")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .map_err(|e| format!("fexc --serve: {e}"))?;
        let (stdin, stdout) = (child.stdin.take().unwrap(), child.stdout.take().unwrap());
        Ok(Self { child, stdin, stdout })
    }
    /// The server's reply to one request body: (fexc's exit code for it, JSON report or error text, object).
    fn request(&mut self, body: &[u8]) -> std::io::Result<(i32, Vec<u8>, Vec<u8>)> {
        self.stdin.write_all(&(body.len() as u32).to_le_bytes())?;
        self.stdin.write_all(body)?;
        let mut word = [0; 4];
        self.stdout.read_exact(&mut word)?;
        let mut reply = vec![0; u32::from_le_bytes(word) as usize];
        self.stdout.read_exact(&mut reply)?;
        let field = |at: usize| u32::from_le_bytes(reply[at..at + 4].try_into().unwrap()) as usize;
        if reply.len() < 12 || reply.len() != 12 + field(4) + field(8) {
            return Err(std::io::Error::other("malformed fexc reply"));
        }
        let (code, json_len) = (field(0) as i32, field(4));
        let object = reply.split_off(12 + json_len);
        let json = reply.split_off(12);
        Ok((code, json, object))
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// What fexc answered for one request (errors are not kept).
enum Compiled {
    Object(Vec<u8>),
    NoCode(String),
}

/// fexc for one vCPU's feature set: one persistent `fexc --serve` process (started at the first compile) and a cache of its
/// answers keyed by the whole request (arguments: RIP, profile, block limit, features; and the guest bytes), so a repeated
/// request is not compiled again. Objects are byte-identical to the one-shot CLI's.
pub(crate) struct Compiler {
    fexc: PathBuf,
    server: Option<Server>,
    supported: u64,
    /// Block size limit in guest instructions (fexc's own limit when `None`).
    max_instructions: Option<u32>,
    cache: HashMap<Vec<u8>, Compiled>,
    /// Requests that reached fexc.
    pub(crate) compiles: u64,
    /// Requests answered from the cache.
    pub(crate) hits: u64,
    /// Test drivers only: rewrites each admitted object before it is staged, to check the resident's run-time defences
    /// against code admission refuses.
    pub(crate) tamper: Option<fn(&mut [u8])>,
}
impl Compiler {
    pub(crate) fn new(fexc: &Path, cpu: &Vcpu, max_instructions: Option<u32>) -> Result<Self> {
        let mut id = [0; admission::ID_REGISTERS.len()];
        for (value, (name, reg)) in id.iter_mut().zip(admission::ID_REGISTERS) {
            *value = cpu.sys(reg).map_err(|e| format!("{name}: {e}"))?;
        }
        Ok(Self::with_features(fexc, admission::supported_features(&id), max_instructions))
    }
    fn with_features(fexc: &Path, supported: u64, max_instructions: Option<u32>) -> Self {
        Self { fexc: fexc.into(), server: None, supported, max_instructions, cache: HashMap::new(), compiles: 0, hits: 0, tamper: None }
    }
    /// The admitted block for `bytes` at `rip` (only its first instruction if `step`), or fexc's end reason when it
    /// produced no code.
    fn compile(&mut self, bytes: &[u8], rip: u64, step: bool) -> Result<std::result::Result<admission::Admitted, String>> {
        let mut args = vec!["--profile".to_string(), "slice1".into(), "--rip".into(), format!("{rip:#x}")];
        if let Some(n) = if step { Some(1) } else { self.max_instructions } {
            args.extend(["--max-insts".into(), n.to_string()]);
        }
        for feature in admission::feature_overrides(self.supported) {
            args.extend(["--feature".into(), feature]);
        }
        let args: Vec<u8> = args.iter().flat_map(|a| a.bytes().chain([0])).collect();
        let mut body = (args.len() as u32).to_le_bytes().to_vec();
        body.extend(args);
        body.extend(bytes);
        if !self.cache.contains_key(&body) {
            if self.server.is_none() {
                self.server = Some(Server::start(&self.fexc)?);
            }
            let (code, json, object) = self.server.as_mut().unwrap().request(&body).map_err(|e| format!("fexc: {e}"))?;
            self.compiles += 1;
            let compiled = match code {
                0 => Compiled::Object(object),
                1 => {
                    let json = crate::json::parse(&String::from_utf8_lossy(&json))?;
                    Compiled::NoCode(end_reason(&json).ok_or("fexc report without guest.end.reason")?.into())
                }
                _ => return Err(format!("fexc failed at {rip:#x}: {}", String::from_utf8_lossy(&json))),
            };
            // ponytail: wholesale reset at a size cap; a real eviction policy when runs outgrow it
            if self.cache.len() >= 1 << 16 {
                self.cache.clear();
            }
            self.cache.insert(body.clone(), compiled);
        } else {
            self.hits += 1;
        }
        match &self.cache[&body] {
            Compiled::Object(object) => {
                let mut admitted = admission::admit(object, rip, bytes, self.supported)?;
                if let Some(tamper) = self.tamper {
                    tamper(&mut admitted.object);
                }
                Ok(Ok(admitted))
            }
            Compiled::NoCode(reason) => Ok(Err(reason.clone())),
        }
    }
}
/// `guest.end.reason` of fexc's report.
fn end_reason(json: &crate::json::Value) -> Option<&str> {
    json.get("guest")?.get("end")?.get("reason")?.as_str()
}

/// Guest bytes fetched at a RIP, the (linear, physical) pages they came from, and the
/// fetch fault of the next page when that ended them.
struct Fetched {
    bytes: Vec<u8>,
    pages: Vec<[u64; 2]>,
    next_fault: Option<Fault>,
}

/// The fetch walk's A-bit writes: guest-physical addresses written (a write to a page holding translations is a
/// write to code, which no vCPU may keep running, see [`page_written`]).
struct Writes(Vec<u64>);
impl natemu_abi::guest_memory::Observer for Writes {
    fn write(&mut self, gpa: u64, _: u64, _: u64) -> bool {
        self.0.push(gpa);
        true
    }
}

/// Fetches lost to peers' A/D races in a row before the host gives up on a request (a livelock the guest causes: x86
/// re-walks without bound, but a host thread must not spin forever; each fetch already re-walks `WALK_ATTEMPTS` times).
const FETCH_ATTEMPTS: usize = 100_000;

/// Guest bytes at `rip` up to the end of its page, continuing into the next
/// page when that is fetchable, and never past `limit`. A fetch whose walk (of either page) loses its A/D
/// races to peers is repeated whole (x86 re-walks), until `cancelled` or [`FETCH_ATTEMPTS`] (then an error).
fn fetch(memory: &Memory, cpu: &Cpu, rip: u64, limit: Option<u64>, cancelled: &AtomicBool) -> Result<std::result::Result<Fetched, Fault>> {
    let ram = guest_ram(memory);
    let control = cpu.control().ok_or("bank is not in 4-level long mode")?;
    for _ in 0..FETCH_ATTEMPTS {
        let mut tables = GuestTables::observed(ram, Writes(Vec::new()));
        let fetched = fetch_with(&mut tables, ram, control, rip, limit);
        for gpa in tables.observer.0 {
            page_written(memory, gpa);
        }
        match fetched {
            Ok(Err(Fault::Contended)) if !cancelled.load(SeqCst) => {}
            _ => return fetched,
        }
    }
    Err(format!("fetch at {rip:#x}: page walks lost {FETCH_ATTEMPTS} A/D races in a row (livelock)"))
}
fn fetch_with<O: natemu_abi::guest_memory::Observer>(
    tables: &mut GuestTables<O>,
    ram: GuestRam,
    control: paging::Control,
    rip: u64,
    limit: Option<u64>,
) -> Result<std::result::Result<Fetched, Fault>> {
    let (mut bytes, mut pages, mut va, mut next_fault) = (Vec::new(), Vec::new(), rip, None);
    for _ in 0..2 {
        let page_end = (va | 4095).checked_add(1);
        let end = match (limit, page_end) {
            (Some(l), Some(e)) if l > va && l < e => l,
            (Some(l), None) if l > va => l,
            (_, Some(e)) => e,
            (_, None) => 0, // the last page: up to 2^64, wrapping is the guest's problem
        };
        let t = match paging::walk(tables, control, va, Access::Fetch) {
            Ok(t) if t.wb => t,
            Ok(_) if pages.is_empty() => return Err(format!("code at {va:#x} is not write-back memory")),
            Ok(_) => break,
            // Either page's walk lost its races: the whole fetch is repeated (an instruction may straddle into the
            // next page, which must not look undecodable).
            Err(f @ Fault::Contended) => return Ok(Err(f)),
            Err(f) if pages.is_empty() => return Ok(Err(f)),
            Err(f) => {
                next_fault = Some(f);
                break;
            }
        };
        let mut chunk = vec![0; end.wrapping_sub(va) as usize];
        if !ram.copy_out(t.gpa, &mut chunk) {
            return Err(format!("code at {va:#x} is outside guest RAM"));
        }
        bytes.extend(chunk);
        pages.push([va & !4095, t.gpa & !4095]);
        if Some(end) != page_end {
            break;
        }
        va = end;
    }
    Ok(Ok(Fetched { bytes, pages, next_fault }))
}

/// Host exits of one [`run`].
#[derive(Default)]
pub(crate) struct Requests {
    /// Translation requests answered with a block, and the distinct RIPs among them (single-step requests not included).
    pub(crate) blocks: u64,
    pub(crate) rips: std::collections::HashSet<u64>,
    /// Test drivers only: called with the vCPU stopped, the block staged and the reply not yet written, until it returns
    /// true (it corrupted something); then `tampered` is set. Checks the resident's run-time defences.
    pub(crate) tamper: Option<fn(Area, &mut State) -> bool>,
    pub(crate) tampered: bool,
}

/// Runs the vCPU (entered at the resident, see [`enter`]) until it stops; `stop_at` is a RIP at
/// which execution stops at a block boundary (blocks are fetched up to it, and
/// never cached at it, so reaching it is a miss). A vCPU that stays in the guest
/// for `watchdog` without a host exit is forced out ([`Stop::Watchdog`]); host
/// work between exits (compiles) is not counted. `cancelled` (a peer failed) ends the
/// run at the next host exit ([`Stop::Cancelled`]; the supervisor forces the vCPU out).
pub(crate) fn run(
    cpu: &Vcpu,
    area: Area,
    compiler: &mut Compiler,
    stop_at: Option<u64>,
    watchdog: Duration,
    requests: &mut Requests,
    cancelled: &AtomicBool,
) -> Result<Stop> {
    // `true`: the vCPU enters the guest; `false`: it is back in the host. Dropping the sender ends the watchdog.
    let (running, events) = std::sync::mpsc::channel::<bool>();
    let id = cpu.id();
    let dog = std::thread::spawn(move || {
        use std::sync::mpsc::RecvTimeoutError::Timeout;
        while let Ok(true) = events.recv() {
            let mut limit = watchdog;
            // Repeated while the vCPU stays in: a request racing its entry into `hv_vcpu_run` might not stop it.
            while events.recv_timeout(limit) == Err(Timeout) {
                exit_vcpus(&mut [id]);
                limit = Duration::from_millis(100);
            }
        }
    });
    let stop = serve(cpu, area, compiler, stop_at, requests, &running, cancelled);
    // ponytail: a cancel the watchdog sent just as the guest exited stays pending on the vCPU and would end its next
    // `hv_vcpu_run` at once; harmless while a vCPU serves one run. Drain it (a zero-length run) if vCPUs get reused.
    drop(running);
    let _ = dog.join();
    stop
}
fn serve(
    cpu: &Vcpu,
    area: Area,
    compiler: &mut Compiler,
    stop_at: Option<u64>,
    requests: &mut Requests,
    running: &std::sync::mpsc::Sender<bool>,
    cancelled: &AtomicBool,
) -> Result<Stop> {
    // The host's own stop, once decided: the vCPU was told to park (it drops its shadows first).
    let mut parked = None;
    loop {
        if cancelled.load(SeqCst) {
            return Ok(Stop::Cancelled);
        }
        let _ = running.send(true);
        let exit = cpu.run_until_exit();
        let _ = running.send(false);
        let Some(exit) = exit? else {
            return Ok(if cancelled.load(SeqCst) { Stop::Cancelled } else { Stop::Watchdog });
        };
        match exit {
            0x49 => {
                let s: Shared = area.read_record(SHARED_OFFSET);
                let state: State = area.read_record(exec::STATE_OFFSET);
                return match parked.take() {
                    Some(stop) if state.stop == exec::stop::PARKED => Ok(stop),
                    _ if state.stop == exec::stop::PARKED => Err("vCPU parked unasked".into()),
                    _ => Ok(Stop::Resident { stop: state.stop, status: s.status, detail: s.detail }),
                };
            }
            exec::TRANSLATE_HVC if parked.is_none() => {}
            other => return Err(format!("unexpected HVC immediate {other:#x}")),
        }
        let mut state: State = area.read_record(exec::STATE_OFFSET);
        let step = match state.request {
            exec::REQUEST_TRANSLATE => false,
            exec::REQUEST_STEP => true,
            _ => return Err("translation exit without a request".into()),
        };
        match translation(area, compiler, &mut state, step, stop_at, requests, cancelled)? {
            Some(stop) => {
                parked = Some(stop);
                state.reply = exec::REPLY_PARK;
            }
            None => state.reply = exec::REPLY_ADMITTED,
        }
        area.write_record(exec::STATE_OFFSET, state);
    }
}
/// Serves one request: stages the admitted block and fills `state`'s reply fields, or returns the vCPU's stop.
fn translation(
    area: Area,
    compiler: &mut Compiler,
    state: &mut State,
    step: bool,
    stop_at: Option<u64>,
    requests: &mut Requests,
    cancelled: &AtomicBool,
) -> Result<Option<Stop>> {
    let rip = state.cpu.rip;
    if Some(rip) == stop_at {
        return Ok(Some(Stop::Reached));
    }
    let Fetched { bytes, pages, next_fault } = match fetch(area.memory, &state.cpu, rip, stop_at, cancelled)? {
        Ok(fetched) => fetched,
        Err(Fault::Contended) => return Ok(Some(Stop::Cancelled)),
        Err(fault) => return Ok(Some(Stop::FetchFault(fault))),
    };
    let block = match compiler.compile(&bytes, rip, step)? {
        Ok(block) => block,
        Err(reason) => {
            return Ok(Some(match next_fault {
                // The instruction at RIP continues into a page whose fetch faults.
                Some(fault) if reason == "partial_decode" => Stop::FetchFault(fault),
                _ => Stop::NoCode(reason),
            }));
        }
    };
    // A step reply is exactly one instruction (the resident refuses anything else too).
    if step && (block.guest_insns != 1 || block.guest_bytes > 15) {
        return Err(format!("step at {rip:#x}: {} instructions, {} bytes", block.guest_insns, block.guest_bytes));
    }
    area.copy(exec::STAGING_OFFSET, &block.object);
    if !step {
        requests.blocks += 1;
        requests.rips.insert(rip);
    }
    state.source_gpa = [u64::MAX; 2];
    state.link_points = [exec::LinkPoint::NONE; exec::MAX_LINK_POINTS];
    state.link_points[..block.links.len()].copy_from_slice(&block.links);
    // Source pages are those the translated bytes reach.
    let end = rip.wrapping_add(u64::from(block.guest_bytes) - 1);
    let used = pages.into_iter().filter(|&[va, _]| va == rip & !4095 || va == end & !4095);
    for (i, [_, gpa]) in used.enumerate() {
        state.source_gpa[i] = gpa;
    }
    if let Some(tamper) = requests.tamper {
        if tamper(area, state) {
            (requests.tamper, requests.tampered) = (None, true);
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    /// With `FEXC` set: a repeated request is answered from the cache, a different RIP, bytes or block limit is compiled.
    #[test]
    fn compiler_caches_exact_requests() {
        let Some(fexc) = std::env::var_os("FEXC") else { return };
        let mut c = super::Compiler::with_features(std::path::Path::new(&fexc), u64::MAX, None);
        let code = [0x48, 0x01, 0xd8, 0xc3];
        let first = c.compile(&code, 0x1000, false).unwrap().unwrap().object;
        assert_eq!((c.compiles, c.hits), (1, 0));
        assert_eq!(c.compile(&code, 0x1000, false).unwrap().unwrap().object, first);
        assert_eq!((c.compiles, c.hits), (1, 1));
        c.compile(&code, 0x2000, false).unwrap().unwrap();
        c.compile(&[0x48, 0x29, 0xd8, 0xc3], 0x1000, false).unwrap().unwrap();
        c.max_instructions = Some(1);
        c.compile(&code, 0x1000, false).unwrap().unwrap();
        assert_eq!((c.compiles, c.hits), (4, 1));
    }

    #[test]
    fn one_to_four_vcpus() {
        for n in [0, cache::MAX_VCPUS + 1] {
            let e = super::prepare("/nonexistent", cache::CODE_BYTES, n).err().unwrap();
            assert!(e.contains(&format!("{n} vCPUs")), "{e}");
        }
        assert_eq!(cache::MAX_VCPUS, 4);
    }

    #[test]
    fn a_write_to_a_code_page_is_posted_to_its_holders() {
        let memory = Memory::with_size(exec::BACKING_BYTES).unwrap();
        memory.copy(cache::GLOBAL_OFFSET + cache::CODE_PAGES + 5, &[0b101]);
        page_written(&memory, 0x5123);
        assert_eq!(memory.read::<1>(cache::GLOBAL_OFFSET + cache::CODE_PAGES + 5), [0]);
        for v in 0..cache::MAX_VCPUS {
            let held = v == 0 || v == 2;
            assert_eq!(record_word(&memory, v, None).load(SeqCst), if held { cache::PAGES } else { 0 });
            assert_eq!(record_word(&memory, v, Some(0)).load(SeqCst), if held { 1 << 5 } else { 0 });
        }
        page_written(&memory, 0x6000); // no holder: nothing posted
        assert_eq!(record_word(&memory, 1, None).load(SeqCst), 0);
        guest_ram_written(&memory);
        assert!((0..cache::MAX_VCPUS).all(|v| record_word(&memory, v, None).load(SeqCst) & cache::FLUSH != 0));
    }

    /// A second page whose walk loses its A/D races is a contended fetch of the whole range, not a fetch that ends at the
    /// first page with a fault of the next: [`fetch`] repeats it, and the repeat sees both pages.
    #[test]
    fn a_contended_second_page_makes_the_whole_fetch_contended() {
        // Guest-physical 0x1000.. is this array: PML4, PDPT, PD, PT, then the code pages at 0x5000 and 0x6000.
        let mut backing = [0u64; 0x6000 / 8];
        for (i, v) in [(0, 0x2007), (512, 0x3007), (1024, 0x4007), (1537, 0x5007), (1538, 0x6007)] {
            backing[i] = v;
        }
        let ram = unsafe { GuestRam::new(0x1000, 0x6000, backing.as_mut_ptr() as u64) };
        let control = paging::Control { cr3: 0x1000, cpl: 3, wp: 1, nxe: 1, smep: 0 };
        // Every A-bit write to the second page's PTE (0x4010) finds an unrelated bit (AVL) changed.
        struct Race(GuestRam);
        impl natemu_abi::guest_memory::Observer for Race {
            fn write(&mut self, gpa: u64, old: u64, _: u64) -> bool {
                if gpa == 0x4010 {
                    assert!(self.0.write::<u64>(gpa, old ^ 0x200));
                }
                true
            }
        }
        let lost = fetch_with(&mut GuestTables::observed(ram, Race(ram)), ram, control, 0x1ff0, None);
        assert!(matches!(lost, Ok(Err(Fault::Contended))));
        let f = fetch_with(&mut GuestTables::observed(ram, Writes(Vec::new())), ram, control, 0x1ff0, None).unwrap().unwrap();
        assert_eq!((f.bytes.len(), f.pages, f.next_fault), (0x10 + 0x1000, vec![[0x1000, 0x5000], [0x2000, 0x6000]], None));
    }

    #[test]
    fn end_reason_is_read_from_fexc_json() {
        let json = r#"{"guest":{"detail":{"reason":"no"},"end":{"rip":"0x10","reason":"system_instruction","detail":"hlt"}}}"#;
        let json = crate::json::parse(json).unwrap();
        assert_eq!(super::end_reason(&json), Some("system_instruction"));
        assert_eq!(super::end_reason(&crate::json::parse("{}").unwrap()), None);
    }
}
