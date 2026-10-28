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
use std::time::Duration;

/// Guest-physical RAM of the execution mode, through the host's alias.
pub(crate) fn guest_ram(memory: &Memory) -> GuestRam {
    assert!(memory.1 >= exec::BACKING_BYTES);
    let host = memory.0 as u64 + exec::GUEST_RAM_OFFSET as u64;
    unsafe { GuestRam::new(0, exec::GUEST_RAM_BYTES as u64, host) }
}

/// Backing for the execution mode with the resident loaded and its private
/// address space and shared record in place; guest RAM is zero. The code cache
/// holds `code_bytes` of generated code (at most `cache::CODE_BYTES_MAX`).
pub(crate) fn prepare(resident: &str, code_bytes: usize) -> Result<Memory> {
    if !(cache::MAX_BLOCK_BYTES..=cache::CODE_BYTES_MAX).contains(&code_bytes) {
        return Err(format!(
            "code cache of {code_bytes} bytes: it must hold the largest block ({} bytes) and at most {} bytes",
            cache::MAX_BLOCK_BYTES,
            cache::CODE_BYTES_MAX
        ));
    }
    let code_mapped = code_bytes.next_multiple_of(cache::BLOCK);
    let memory = Memory::with_size(exec::BACKING_BYTES + code_mapped)?;
    load_resident(&memory, resident)?;
    let mut spans = resident_text(0);
    let own = |offset, bytes| Span::data(offset, bytes, offset);
    spans.extend([
        own(SHARED_OFFSET, 4096),
        own(exec::STATE_OFFSET, 4096),
        own(exec::FRAME_OFFSET, exec::frame::BYTES),
        Span::text(exec::SLOT_OFFSET, exec::SLOT_BYTES, exec::SLOT_OFFSET),
        Span::data((exec::SLOT_WRITE_VA - PRIVATE) as usize, exec::SLOT_BYTES, exec::SLOT_OFFSET),
        own((STACK_TOP - PRIVATE) as usize - exec::STACK_BYTES, exec::STACK_BYTES),
        own((EXCEPTION_STACK_TOP - PRIVATE) as usize - 0x4000, 0x4000),
        own(exec::STAGING_OFFSET, exec::STAGING_BYTES),
        own(exec::GUEST_RAM_OFFSET, exec::GUEST_RAM_BYTES),
        Span::text((cache::CODE_VA - PRIVATE) as usize, code_mapped, cache::CODE_OFFSET),
        Span::data((cache::CODE_WRITE_VA - PRIVATE) as usize, code_mapped, cache::CODE_OFFSET),
        Span::data((cache::META_VA - PRIVATE) as usize, cache::META_BYTES, cache::META_OFFSET),
        Span::data((cache::LOOKUP_VA - PRIVATE) as usize, cache::LOOKUP_BYTES, cache::LOOKUP_OFFSET),
        Span::data((cache::GLOBAL_VA - PRIVATE) as usize, cache::GLOBAL_BYTES, cache::GLOBAL_OFFSET),
    ]);
    private_space(&memory, 0, &spans, &[], true);
    // SAFETY: Control is plain integers; the resident owns everything but the capacity.
    let mut control: cache::Control = unsafe { std::mem::zeroed() };
    control.code_bytes = code_bytes as u64;
    memory.write_record(cache::LOOKUP_OFFSET + cache::CONTROL, control);
    // SAFETY: Shared is plain integers; all-zero is its empty value.
    let mut shared: Shared = unsafe { std::mem::zeroed() };
    shared.mode = exec::MODE;
    memory.write_record(SHARED_OFFSET, shared);
    Ok(memory)
}
/// Sets the vCPU's initial architectural state (before it first runs).
pub(crate) fn set_cpu(memory: &Memory, cpu: Cpu) {
    memory.write_record(exec::STATE_OFFSET, State::new(cpu));
}
/// Guest RAM was written by the host (only while the vCPU is stopped): the vCPU drops its cached code before it
/// next runs guest code.
pub(crate) fn guest_ram_written(memory: &Memory) {
    let at = cache::GLOBAL_OFFSET + cache::QUEUES;
    let mut queue: cache::Queue = memory.read_record(at);
    queue.flush = cache::FLUSH;
    memory.write_record(at, queue);
}

/// The vCPU's placed blocks, in placement order (read from its metadata arena).
pub(crate) fn placed_blocks(memory: &Memory) -> Vec<cache::Block> {
    let control: cache::Control = memory.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    (0..control.blocks as usize)
        .map(|n| {
            let meta = u32::from_le_bytes(memory.read::<4>(cache::LOOKUP_OFFSET + cache::INDEX + 4 * n));
            memory.read_record(cache::META_OFFSET + meta as usize)
        })
        .collect()
}
/// The link record `i` (written by the resident's linker).
pub(crate) fn link_record(memory: &Memory, i: usize) -> cache::LinkRecord {
    memory.read_record(cache::LOOKUP_OFFSET + cache::LINKS + i * size_of::<cache::LinkRecord>())
}

/// Self-check of the vCPU's cache after a run (the contract's rule (d), independent of the resident): every link point of
/// every placed block holds its admitted word (`ldr x0, L`, L holding its target) or `b` to the entry of a placed block
/// keyed (target, the block's context), and the resident's link records describe exactly the linked ones. Returns
/// (link points, linked).
pub(crate) fn check_links(memory: &Memory) -> Result<(u64, u64)> {
    let lookup = |offset: usize| cache::LOOKUP_OFFSET + offset;
    let control: cache::Control = memory.read_record(lookup(cache::CONTROL));
    let word = |offset: u64| u32::from_le_bytes(memory.read::<4>(cache::CODE_OFFSET + offset as usize));
    let blocks = placed_blocks(memory);
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
    let records: Vec<cache::LinkRecord> = (0..control.links as usize).map(|i| link_record(memory, i)).collect();
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

/// The fetch walk's A-bit writes: one to a page the vCPU holds translations from is a host write to code, which
/// the vCPU must not keep running (see [`guest_ram_written`]).
struct CodePageWrites<'a>(&'a Memory, bool);
impl natemu_abi::guest_memory::Observer for CodePageWrites<'_> {
    fn write(&mut self, gpa: u64, _: u64, _: u64) -> bool {
        let page = (gpa >> 12) as usize;
        if page < exec::GUEST_PAGES {
            self.1 |= self.0.read::<1>(cache::GLOBAL_OFFSET + cache::CODE_PAGES + page)[0] != 0;
        }
        true
    }
}

/// Guest bytes at `rip` up to the end of its page, continuing into the next
/// page when that is fetchable, and never past `limit`.
fn fetch(memory: &Memory, cpu: &Cpu, rip: u64, limit: Option<u64>) -> Result<std::result::Result<Fetched, Fault>> {
    let ram = guest_ram(memory);
    let control = cpu.control().ok_or("bank is not in 4-level long mode")?;
    let mut tables = GuestTables::observed(ram, CodePageWrites(memory, false));
    let fetched = fetch_with(&mut tables, ram, control, rip, limit);
    if tables.observer.1 {
        guest_ram_written(memory);
    }
    fetched
}
fn fetch_with(
    tables: &mut GuestTables<CodePageWrites>,
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
    pub(crate) tamper: Option<fn(&Memory, &mut State) -> bool>,
    pub(crate) tampered: bool,
}

/// Runs the vCPU (entered at the resident) until it stops; `stop_at` is a RIP at
/// which execution stops at a block boundary (blocks are fetched up to it, and
/// never cached at it, so reaching it is a miss). A vCPU that stays in the guest
/// for `watchdog` without a host exit is forced out ([`Stop::Watchdog`]); host
/// work between exits (compiles) is not counted.
pub(crate) fn run(vm: &Vm, compiler: &mut Compiler, stop_at: Option<u64>, watchdog: Duration, requests: &mut Requests) -> Result<Stop> {
    // `true`: the vCPU enters the guest; `false`: it is back in the host. Dropping the sender ends the watchdog.
    let (running, events) = std::sync::mpsc::channel::<bool>();
    let id = vm.cpu.id();
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
    let stop = serve(vm, compiler, stop_at, requests, &running);
    // ponytail: a cancel the watchdog sent just as the guest exited stays pending on the vCPU and would end its next
    // `hv_vcpu_run` at once; harmless while a vCPU serves one run. Drain it (a zero-length run) if vCPUs get reused.
    drop(running);
    let _ = dog.join();
    stop
}
fn serve(vm: &Vm, compiler: &mut Compiler, stop_at: Option<u64>, requests: &mut Requests, running: &std::sync::mpsc::Sender<bool>) -> Result<Stop> {
    loop {
        let _ = running.send(true);
        let exit = vm.cpu.run_until_exit();
        let _ = running.send(false);
        let Some(exit) = exit? else {
            return Ok(Stop::Watchdog);
        };
        match exit {
            0x49 => {
                let s = vm.shared();
                let state: State = vm.memory.read_record(exec::STATE_OFFSET);
                return Ok(Stop::Resident { stop: state.stop, status: s.status, detail: s.detail });
            }
            exec::TRANSLATE_HVC => {}
            other => return Err(format!("unexpected HVC immediate {other:#x}")),
        }
        let mut state: State = vm.memory.read_record(exec::STATE_OFFSET);
        let step = match state.request {
            exec::REQUEST_TRANSLATE => false,
            exec::REQUEST_STEP => true,
            _ => return Err("translation exit without a request".into()),
        };
        let rip = state.cpu.rip;
        if Some(rip) == stop_at {
            return Ok(Stop::Reached);
        }
        let Fetched { bytes, pages, next_fault } = match fetch(&vm.memory, &state.cpu, rip, stop_at)? {
            Ok(fetched) => fetched,
            Err(fault) => return Ok(Stop::FetchFault(fault)),
        };
        let block = match compiler.compile(&bytes, rip, step)? {
            Ok(block) => block,
            Err(reason) => {
                return Ok(match next_fault {
                    // The instruction at RIP continues into a page whose fetch faults.
                    Some(fault) if reason == "partial_decode" => Stop::FetchFault(fault),
                    _ => Stop::NoCode(reason),
                });
            }
        };
        // A step reply is exactly one instruction (the resident refuses anything else too).
        if step && (block.guest_insns != 1 || block.guest_bytes > 15) {
            return Err(format!("step at {rip:#x}: {} instructions, {} bytes", block.guest_insns, block.guest_bytes));
        }
        vm.memory.copy(exec::STAGING_OFFSET, &block.object);
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
            if tamper(&vm.memory, &mut state) {
                (requests.tamper, requests.tampered) = (None, true);
            }
        }
        state.reply = exec::REPLY_ADMITTED;
        vm.memory.write_record(exec::STATE_OFFSET, state);
    }
}

#[cfg(test)]
mod tests {
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
    fn end_reason_is_read_from_fexc_json() {
        let json = r#"{"guest":{"detail":{"reason":"no"},"end":{"rip":"0x10","reason":"system_instruction","detail":"hlt"}}}"#;
        let json = crate::json::parse(json).unwrap();
        assert_eq!(super::end_reason(&json), Some("system_instruction"));
        assert_eq!(super::end_reason(&crate::json::parse("{}").unwrap()), None);
    }
}
