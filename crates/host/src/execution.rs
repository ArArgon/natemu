//! Host side of block execution (`natemu_abi::execution`): the execution mode's
//! private address space, and the service loop that answers the resident's
//! translation requests: fetch the guest bytes at RIP through the guest walk,
//! compile one block with fexc (`--profile slice1`), admit it and stage it.
use super::*;
use crate::admission;
use natemu_abi::execution::{self as exec, Cpu, State};
use natemu_abi::guest_memory::{GuestRam, GuestTables};
use natemu_abi::paging::{self, Access, Fault};
use std::path::{Path, PathBuf};
use std::process::Command;

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
        own((STACK_TOP - PRIVATE) as usize - 0x4000, 0x4000),
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
    /// The instruction fetch at RIP faults (delivery is S1e).
    FetchFault(Fault),
    /// fexc translated nothing at RIP; its end reason (e.g. `system_instruction`).
    NoCode(String),
    /// The resident stopped the VM: `State::stop` (0 for a non-execution stop) and the shared status/detail.
    Resident { stop: u64, status: u64, detail: u64 },
}

/// fexc for one vCPU's feature set; every compile is a fresh process.
pub(crate) struct Compiler {
    fexc: PathBuf,
    work: PathBuf,
    supported: u64,
    /// Block size limit in guest instructions (fexc's own limit when `None`).
    max_instructions: Option<u32>,
    pub(crate) compiles: u64,
}
impl Compiler {
    pub(crate) fn new(fexc: &Path, work: &Path, cpu: &Vcpu, max_instructions: Option<u32>) -> Result<Self> {
        let mut id = [0; admission::ID_REGISTERS.len()];
        for (value, (name, reg)) in id.iter_mut().zip(admission::ID_REGISTERS) {
            *value = cpu.sys(reg).map_err(|e| format!("{name}: {e}"))?;
        }
        fs::create_dir_all(work).map_err(|e| e.to_string())?;
        Ok(Self {
            fexc: fexc.into(),
            work: work.into(),
            supported: admission::supported_features(&id),
            max_instructions,
            compiles: 0,
        })
    }
    /// The admitted block for `bytes` at `rip`, or fexc's end reason when it produced no code.
    fn compile(&mut self, bytes: &[u8], rip: u64) -> Result<std::result::Result<admission::Admitted, String>> {
        let file = |name: &str| self.work.join(name);
        let (raw, object, json) = (file("block.bin"), file("block.obj"), file("block.json"));
        let _ = fs::remove_file(&object);
        fs::write(&raw, bytes).map_err(|e| e.to_string())?;
        let mut command = Command::new(&self.fexc);
        command.args(["--profile", "slice1", "--rip", &format!("{rip:#x}")]);
        command.arg("--file").arg(&raw).arg("--object").arg(&object).arg("--out").arg(&json);
        if let Some(n) = self.max_instructions {
            command.args(["--max-insts", &n.to_string()]);
        }
        for feature in admission::feature_overrides(self.supported) {
            command.args(["--feature", &feature]);
        }
        let output = command.output().map_err(|e| format!("fexc: {e}"))?;
        self.compiles += 1;
        match output.status.code() {
            Some(0) => {
                let object = fs::read(&object).map_err(|e| e.to_string())?;
                Ok(Ok(admission::admit(&object, rip, bytes, self.supported)?))
            }
            Some(1) => {
                let json = crate::json::parse(&fs::read_to_string(&json).map_err(|e| e.to_string())?)?;
                Ok(Err(end_reason(&json).ok_or("fexc report without guest.end.reason")?.into()))
            }
            _ => Err(format!("fexc failed at {rip:#x}: {}", String::from_utf8_lossy(&output.stderr))),
        }
    }
}
/// `guest.end.reason` of fexc's report.
fn end_reason(json: &crate::json::Value) -> Option<&str> {
    json.get("guest")?.get("end")?.get("reason")?.as_str()
}

/// Guest bytes at `rip` up to the end of its page, continuing into the next
/// page when that is fetchable, and never past `limit`; with the
/// (linear, physical) pages they came from.
fn fetch(ram: GuestRam, cpu: &Cpu, rip: u64, limit: Option<u64>) -> Result<std::result::Result<(Vec<u8>, Vec<[u64; 2]>), Fault>> {
    let control = cpu.control().ok_or("bank is not in 4-level long mode")?;
    let mut tables = GuestTables::new(ram);
    let (mut bytes, mut pages, mut va) = (Vec::new(), Vec::new(), rip);
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
            Err(_) => break,
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
    Ok(Ok((bytes, pages)))
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
        let (bytes, pages) = match fetch(ram, &state.cpu, rip, stop_at)? {
            Ok(fetched) => fetched,
            Err(fault) => return Ok(Stop::FetchFault(fault)),
        };
        let block = match compiler.compile(&bytes, rip)? {
            Ok(block) => block,
            Err(reason) => return Ok(Stop::NoCode(reason)),
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
    #[test]
    fn end_reason_is_read_from_fexc_json() {
        let json = r#"{"guest":{"detail":{"reason":"no"},"end":{"rip":"0x10","reason":"system_instruction","detail":"hlt"}}}"#;
        let json = crate::json::parse(json).unwrap();
        assert_eq!(super::end_reason(&json), Some("system_instruction"));
        assert_eq!(super::end_reason(&crate::json::parse("{}").unwrap()), None);
    }
}
