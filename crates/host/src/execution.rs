//! Host side of block execution (`natemu_abi::execution`): the execution mode's
//! private address space, and the service loop that answers the resident's
//! translation requests: fetch the guest bytes at RIP through the guest walk,
//! compile one block with fexc (`--profile slice1`, a persistent server), admit it and stage it.
use super::*;
use crate::admission;
use natemu_abi::execution::{self as exec, Cpu, State};
use natemu_abi::guest_memory::{GuestRam, GuestTables};
use natemu_abi::paging::{self, Access, Fault};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

/// Guest-physical RAM of the execution mode, through the host's alias.
pub(crate) fn guest_ram(memory: &Memory) -> GuestRam {
    assert!(memory.1 >= exec::BACKING_BYTES);
    let host = memory.0 as u64 + exec::GUEST_RAM_OFFSET as u64;
    unsafe { GuestRam::new(0, exec::GUEST_RAM_BYTES as u64, host) }
}

/// Backing for the execution mode with the resident loaded and its private
/// address space and shared record in place; guest RAM is zero.
pub(crate) fn prepare(resident: &str) -> Result<Memory> {
    let memory = Memory::with_size(exec::BACKING_BYTES)?;
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
    ]);
    private_space(&memory, 0, &spans, &[], true);
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
        Self { fexc: fexc.into(), server: None, supported, max_instructions, cache: HashMap::new(), compiles: 0, hits: 0 }
    }
    /// The admitted block for `bytes` at `rip`, or fexc's end reason when it produced no code.
    fn compile(&mut self, bytes: &[u8], rip: u64) -> Result<std::result::Result<admission::Admitted, String>> {
        let mut args = vec!["--profile".to_string(), "slice1".into(), "--rip".into(), format!("{rip:#x}")];
        if let Some(n) = self.max_instructions {
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
            Compiled::Object(object) => Ok(Ok(admission::admit(object, rip, bytes, self.supported)?)),
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

/// Guest bytes at `rip` up to the end of its page, continuing into the next
/// page when that is fetchable, and never past `limit`.
fn fetch(ram: GuestRam, cpu: &Cpu, rip: u64, limit: Option<u64>) -> Result<std::result::Result<Fetched, Fault>> {
    let control = cpu.control().ok_or("bank is not in 4-level long mode")?;
    let mut tables = GuestTables::new(ram);
    let (mut bytes, mut pages, mut va, mut next_fault) = (Vec::new(), Vec::new(), rip, None);
    for _ in 0..2 {
        let page_end = (va | 4095).checked_add(1);
        let end = match (limit, page_end) {
            (Some(l), Some(e)) if l > va && l < e => l,
            (Some(l), None) if l > va => l,
            (_, Some(e)) => e,
            (_, None) => 0, // the last page: up to 2^64, wrapping is the guest's problem
        };
        let t = match paging::walk(&mut tables, control, va, Access::Fetch) {
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

/// Runs the vCPU (entered at the resident) until it stops; `stop_at` is a RIP at
/// which execution stops at a block boundary (blocks are fetched up to it).
pub(crate) fn run(vm: &Vm, compiler: &mut Compiler, stop_at: Option<u64>) -> Result<Stop> {
    let ram = guest_ram(&vm.memory);
    loop {
        match vm.cpu.run_to_hvc()? {
            0x49 => {
                let s = vm.shared();
                let state: State = vm.memory.read_record(exec::STATE_OFFSET);
                return Ok(Stop::Resident { stop: state.stop, status: s.status, detail: s.detail });
            }
            exec::TRANSLATE_HVC => {}
            other => return Err(format!("unexpected HVC immediate {other:#x}")),
        }
        let mut state: State = vm.memory.read_record(exec::STATE_OFFSET);
        if state.request != exec::REQUEST_TRANSLATE {
            return Err("translation exit without a request".into());
        }
        let rip = state.cpu.rip;
        if Some(rip) == stop_at {
            return Ok(Stop::Reached);
        }
        let Fetched { bytes, pages, next_fault } = match fetch(ram, &state.cpu, rip, stop_at)? {
            Ok(fetched) => fetched,
            Err(fault) => return Ok(Stop::FetchFault(fault)),
        };
        let block = match compiler.compile(&bytes, rip)? {
            Ok(block) => block,
            Err(reason) => {
                return Ok(match next_fault {
                    // The instruction at RIP continues into a page whose fetch faults.
                    Some(fault) if reason == "partial_decode" => Stop::FetchFault(fault),
                    _ => Stop::NoCode(reason),
                });
            }
        };
        vm.memory.copy(exec::STAGING_OFFSET, &block.object);
        state.source_va = [u64::MAX; 2];
        state.source_gpa = [u64::MAX; 2];
        // Source pages are those the translated bytes reach.
        let end = rip.wrapping_add(u64::from(block.guest_bytes) - 1);
        let used = pages.into_iter().filter(|&[va, _]| va == rip & !4095 || va == end & !4095);
        for (i, [va, gpa]) in used.enumerate() {
            state.source_va[i] = va;
            state.source_gpa[i] = gpa;
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
        let first = c.compile(&code, 0x1000).unwrap().unwrap().object;
        assert_eq!((c.compiles, c.hits), (1, 0));
        assert_eq!(c.compile(&code, 0x1000).unwrap().unwrap().object, first);
        assert_eq!((c.compiles, c.hits), (1, 1));
        c.compile(&code, 0x2000).unwrap().unwrap();
        c.compile(&[0x48, 0x29, 0xd8, 0xc3], 0x1000).unwrap().unwrap();
        c.max_instructions = Some(1);
        c.compile(&code, 0x1000).unwrap().unwrap();
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
