//! M2 task D, diagnostics: a vCPU's report (where it was, the guest RIP, mode and CPL, the instruction bytes, the
//! registers and the code identity), printed on standard error at a Linux boot's stops ([`Compiler::report`]), when a guest
//! hang ends the run (the watchdog forcing a vCPU out of the guest), when the host-side progress check finds a vCPU waiting
//! in the host (reported, the run goes on), and when the process is asked to inspect the VM ([`inspect_on_signal`]: SIGINFO, Ctrl-T on a terminal), after
//! which the vCPUs run on. Nothing here is on a vCPU's path between exits.
use super::*;
use natemu_abi::code_object::Site;

/// Where the vCPU was when its report was taken.
pub(crate) enum At<'a> {
    /// At an instruction boundary (a stop at a host exit or a resident stop): the bank is the guest state; not `exact` for a
    /// resident stop outside execution (stop 0: the bank is as the resident last wrote it).
    Stopped { exact: bool },
    /// Forced out of the VM (watchdog, cancel or inspection), read on the vCPU's own thread: in translated code (EL0; the
    /// guest registers are in the Arm registers, the RIP is mapped back through the block's access sites) or in the resident
    /// (EL1; the bank is as of its last entry).
    Forced(&'a Vcpu),
    /// In the host, serving `request` (the resident's request word) for `secs`; the bank is as of that exit.
    Host { request: u64, secs: f64 },
}

/// The inspection requests so far (SIGINFO), and whether a handler is installed.
pub(crate) static INSPECT: AtomicU64 = AtomicU64::new(0);
pub(crate) static INSPECTING: AtomicBool = AtomicBool::new(false);
/// How often each vCPU's watchdog thread looks for an inspection request while one can come.
pub(crate) const INSPECT_POLL: u64 = 100_000_000;

/// From now on SIGINFO (macOS; Ctrl-T on a terminal) makes every vCPU of a run report where it is, on standard error,
/// and run on: a vCPU in the guest is forced out and resumed unchanged, one in the host is reported as it is. The
/// handler only counts; each vCPU's watchdog thread acts on it within [`INSPECT_POLL`]. Once per process.
pub fn inspect_on_signal() {
    extern "C" fn asked(_: i32) {
        INSPECT.fetch_add(1, SeqCst);
    }
    unsafe extern "C" {
        fn signal(signal: i32, handler: extern "C" fn(i32)) -> usize;
    }
    unsafe { signal(SIGINFO, asked) };
    INSPECTING.store(true, SeqCst);
}
const SIGINFO: i32 = 29;
/// Blocks SIGINFO on the calling thread (a vCPU's, and the watchdog thread it starts), so the signal never lands during
/// `hv_vcpu_run`; the process's other threads take it.
pub(crate) fn block_inspect_signal() {
    unsafe extern "C" {
        fn pthread_sigmask(how: i32, set: *const u32, old: *mut u32) -> i32;
    }
    const SIG_BLOCK: i32 = 1;
    let set: u32 = 1 << (SIGINFO - 1); // Darwin's sigset_t: one bit per signal
    unsafe { pthread_sigmask(SIG_BLOCK, &set, std::ptr::null_mut()) };
}

/// The request word's name.
fn request(r: u64) -> &'static str {
    match r {
        exec::REQUEST_TRANSLATE => "a translation",
        exec::REQUEST_STEP => "a single step",
        exec::REQUEST_DEVICE => "a device access",
        exec::REQUEST_DEVICE_POLL => "a deferred device access",
        exec::REQUEST_ENTROPY => "an RDRAND refill",
        exec::REQUEST_PORT => "a port access",
        exec::REQUEST_PAGE => "a non-RAM page read",
        exec::REQUEST_IRQ => "an interrupt controller access",
        exec::REQUEST_POWER_POLL => "the power-off drain",
        _ => "no request",
    }
}

/// vCPU `area.vcpu`'s report: `what` (its stop, or why it was asked), then where it was and its guest state.
pub(crate) fn report(area: Area, what: &str, at: At) -> String {
    let state: State = area.read_record(exec::STATE_OFFSET);
    let mut cpu = state.cpu;
    let v = area.vcpu;
    let (place, rip_note) = match at {
        At::Stopped { exact: true } => ("stopped at an instruction boundary".to_string(), "exact".to_string()),
        At::Stopped { exact: false } => ("stopped outside execution".to_string(), "as of the bank".to_string()),
        At::Host { request: r, secs } => (
            format!("in the host for {secs:.1} s, serving {} (request {r:#x})", request(r)),
            "exact, as of that exit".to_string(),
        ),
        At::Forced(vcpu) => {
            let (pc, cpsr) = (vcpu.reg(PC).unwrap_or(0), vcpu.reg(CPSR).unwrap_or(0));
            if cpsr & 0xf == 0 {
                for (i, r) in exec::SRA[..16].iter().enumerate() {
                    cpu.gpr[i] = vcpu.reg(u32::from(*r)).unwrap_or(0);
                }
                let note = match locate(area, pc) {
                    Some((rip, note)) => {
                        cpu.rip = rip;
                        note
                    }
                    None => "unknown: the PC is in no block; the RIP is the last bank's".into(),
                };
                (format!("in translated code at native pc {pc:#x} (forced out; rflags are the last bank's)"), note)
            } else {
                let halted = event_word(area.memory, v, std::mem::offset_of!(cache::Events, halted)).load(SeqCst);
                (
                    format!(
                        "in the resident (EL1) at native pc {pc:#x}{} (forced out)",
                        if halted != 0 { ", halted" } else { "" }
                    ),
                    "as of the resident's last entry".into(),
                )
            }
        }
    };
    // (Not `natemu: `: runners read each such line as a stop.)
    let p = format!("natemu vCPU {v}");
    let mut out = format!("{p}: {what}\n{p}: {place}\n");
    out += &format!("{p}: rip {:#x} ({rip_note}), {}, CPL {}\n", cpu.rip, mode(&cpu), cpu.cpl);
    out += &format!("{p}: bytes at rip: {}\n", bytes(area, &cpu));
    const NAMES: [&str; 16] = [
        "rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi", "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15",
    ];
    for row in NAMES.iter().zip(cpu.gpr).collect::<Vec<_>>().chunks(4) {
        let line: Vec<String> = row.iter().map(|(n, x)| format!("{n:<3} {x:#018x}")).collect();
        out += &format!("{p}: {}\n", line.join("  "));
    }
    let s = |i: usize| cpu.seg[i].selector;
    out += &format!(
        "{p}: rflags {:#x} cr0 {:#x} cr2 {:#x} cr3 {:#x} cr4 {:#x} efer {:#x}\n",
        cpu.rflags, cpu.cr0, cpu.cr2, cpu.cr3, cpu.cr4, cpu.efer
    );
    out += &format!(
        "{p}: cs {:#x} ss {:#x} ds {:#x} es {:#x} fs {:#x} gs {:#x} fs.base {:#x} gs.base {:#x}\n",
        s(exec::SREG_CS),
        s(exec::SREG_SS),
        s(3),
        s(exec::SREG_ES),
        s(4),
        s(5),
        cpu.seg[exec::SREG_FS].base,
        cpu.seg[exec::SREG_GS].base
    );
    out += &format!("{p}: code identity {:#x}\n", cpu.code_context());
    out
}

/// The processor mode the bank describes.
fn mode(c: &Cpu) -> &'static str {
    let cs = c.seg[exec::SREG_CS].attributes;
    if c.cr0 & exec::CR0_PE == 0 {
        "real mode"
    } else if c.efer & exec::EFER_LMA == 0 {
        if cs & exec::ATTR_D != 0 { "32-bit protected mode" } else { "16-bit protected mode" }
    } else if cs & (exec::ATTR_L | exec::ATTR_D) == exec::ATTR_L {
        "64-bit mode"
    } else if cs & exec::ATTR_D != 0 {
        "compatibility mode, 32-bit"
    } else {
        "compatibility mode, 16-bit"
    }
}

/// Up to 15 bytes at the bank's RIP, through a read-only walk of its page tables (A/D bits untouched): CS.base + EIP with the
/// 32-bit wrap outside 64-bit mode.
fn bytes(area: Area, c: &Cpu) -> String {
    let cs = c.seg[exec::SREG_CS];
    let long = c.efer & exec::EFER_LMA != 0 && cs.attributes & (exec::ATTR_L | exec::ATTR_D) == exec::ATTR_L;
    let mut out = Vec::new();
    for i in 0..15u64 {
        let va = if long { c.rip.wrapping_add(i) } else { cs.base.wrapping_add(c.rip).wrapping_add(i) & 0xffff_ffff };
        match physical(area, c, va).and_then(|gpa| area.ram.read::<u8>(gpa)) {
            Some(b) => out.push(format!("{b:02x}")),
            None => break,
        }
    }
    if out.is_empty() { "(not readable: unmapped or not guest RAM)".into() } else { out.join(" ") }
}

/// The guest-physical address of linear `va` (4-level paging, or none), read only.
fn physical(area: Area, c: &Cpu, va: u64) -> Option<u64> {
    if c.cr0 & exec::CR0_PG == 0 {
        return Some(va);
    }
    if c.efer & exec::EFER_LMA == 0 {
        return None; // ponytail: 32-bit paging is not walked here; M2 task 3 adds it with 32-bit code
    }
    const PA: u64 = 0x000f_ffff_ffff_f000;
    let mut table = c.cr3 & PA;
    for level in (1..=4u32).rev() {
        let e: u64 = area.ram.read(table + (va >> (12 + 9 * (level - 1)) & 511) * 8)?;
        if e & 1 == 0 {
            return None;
        }
        if level == 1 || (level <= 3 && e & 0x80 != 0) {
            let size = 1u64 << (12 + 9 * (level - 1));
            return Some(e & PA & !(size - 1) | va & (size - 1));
        }
        table = e & PA;
    }
    None
}

/// The guest RIP of native `pc` in translated code, and how exact it is: the block holding it (the single-step slot or a
/// placed block, newest first) and the access sites recorded for it. Exact inside an instruction's translation up to its
/// access; between sites, a range of guest instructions.
fn locate(area: Area, pc: u64) -> Option<(u64, String)> {
    let offset = pc.checked_sub(cache::CODE_VA)?;
    if offset < cache::SLOT_BYTES as u64 {
        let b: cache::Block = area.read_record(cache::LOOKUP_OFFSET + cache::STEP_RECORD);
        return (b.flags & cache::STEP != 0).then(|| (b.rip, "exact: the single-step block".into()));
    }
    let control: cache::Control = area.read_record(cache::LOOKUP_OFFSET + cache::CONTROL);
    let (b, sites_at) = (0..control.blocks as usize).rev().find_map(|n| {
        let meta = u32::from_le_bytes(area.read::<4>(cache::LOOKUP_OFFSET + cache::INDEX + 4 * n)) as usize;
        let b: cache::Block = area.read_record(cache::META_OFFSET + meta);
        let own = u64::from(b.start()) <= offset && offset < u64::from(b.code) + u64::from(b.code_size);
        (b.code_size != 0 && (own || hot(&b, offset))).then_some((b, cache::META_OFFSET + meta + size_of::<cache::Block>()))
    })?;
    let native = if hot(&b, offset) {
        match b.trampolines().find(|&(_, t)| t == offset) {
            Some((e, _)) => e - u64::from(b.code),
            None => offset - b.hot_body(),
        }
    } else {
        offset.wrapping_sub(u64::from(b.code))
    };
    // A position-independent translation runs for the page its guest-state slot names (`cache::running_rip`).
    let base = if b.flags & cache::PIC != 0 {
        let slot = u64::from_le_bytes(area.read::<8>(exec::FRAME_OFFSET + exec::frame::PAGE_BASE as usize));
        slot & !0xfff | b.rip & 0xfff
    } else {
        b.rip
    };
    let sites: Vec<Site> =
        (0..b.sites_count as usize).map(|i| area.read_record(sites_at + i * size_of::<Site>())).collect();
    let end = base.wrapping_add(u64::from(b.guest_bytes));
    let block = format!("in the block at {base:#x} ({} guest bytes)", b.guest_bytes);
    let at = |s: &Site| base.wrapping_add(u64::from(s.guest_offset));
    // Check stubs sit after the body (their native offset in the cold tail, their instruction's start in the body): a PC at
    // or past the first one is in the stub it follows, which stands for its instruction with the state before it.
    let (stubs, inline): (Vec<&Site>, Vec<&Site>) =
        sites.iter().partition(|s| natemu_abi::code_object::kind::is_stub_kind(s.kind));
    if let Some(s) = stubs.iter().filter(|s| u64::from(s.native_offset) <= native).max_by_key(|s| s.native_offset) {
        return Some((at(s), format!("exact: in the check stub of this instruction, {block}")));
    }
    // In the body: the instruction is the one with the latest translation start at or before the PC; exact when one of its
    // inline sites is at or after the PC (it is still before that access).
    let from = inline.iter().filter(|s| u64::from(s.insn_native_offset) <= native).max_by_key(|s| s.insn_native_offset);
    if let Some(f) = from
        && inline
            .iter()
            .any(|s| s.insn_native_offset == f.insn_native_offset && native <= u64::from(s.native_offset))
    {
        return Some((at(f), format!("exact: in this instruction, before its memory access, {block}")));
    }
    let to = inline.iter().filter(|s| u64::from(s.insn_native_offset) > native).min_by_key(|s| s.insn_native_offset);
    let (low, high) = (from.map_or(base, |s| at(s)), to.map_or(end, |s| at(s)));
    let between = if sites.is_empty() { "the block has no access sites" } else { "between access sites" };
    Some((low, format!("approximate: an instruction in [{low:#x}, {high:#x}), {between}, {block}")))
}

/// Code-area offset `x` is in split block `b`'s hot copy (the resident's `cache::in_hot`).
fn hot(b: &cache::Block, x: u64) -> bool {
    b.hot_size != 0 && u64::from(b.hot) + 4 <= x && x < b.hot_body() + u64::from(b.hot_size) + 4
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A block of two access sites: a PC inside an instruction's translation up to its access is exact; anywhere else in
    /// the block it is the range between the sites around it.
    #[test]
    fn native_pc_maps_through_the_sites() {
        let memory = Memory::with_size(exec::BACKING_BYTES + (2 << 20)).unwrap();
        let ram = unsafe { GuestRam::new(0, 2 << 20, memory.0 as u64 + exec::BACKING_BYTES as u64) };
        let area = Area { memory: &memory, vcpu: 0, vcpus: 1, epoch: 0, base: 0, ram };
        let at = cache::LOOKUP_OFFSET + cache::CONTROL;
        let mut control: cache::Control = area.read_record(at);
        control.blocks = 1;
        area.write_record(at, control);
        area.write_record(cache::LOOKUP_OFFSET + cache::INDEX, 0u32);
        let mut b: cache::Block = unsafe { std::mem::zeroed() };
        (b.rip, b.code, b.code_size, b.sites_count, b.guest_bytes) = (0x5000, 0x2_0000, 0x100, 3, 12);
        area.write_record(cache::META_OFFSET, b);
        // Two inline sites, and the check stub of the first instruction in the tail (sorted by native offset).
        let stub = natemu_abi::code_object::kind::GUARD;
        for (i, (insn, native, guest, kind)) in [(0x10, 0x18, 2, 0), (0x40, 0x48, 7, 0), (0x10, 0xc0, 2, stub)].into_iter().enumerate() {
            let mut s: Site = unsafe { std::mem::zeroed() };
            (s.insn_native_offset, s.native_offset, s.guest_offset, s.kind) = (insn, native, guest, kind);
            area.write_record(cache::META_OFFSET + size_of::<cache::Block>() + i * size_of::<Site>(), s);
        }
        let pc = |o: u64| cache::CODE_VA + 0x2_0000 + o;
        for (o, rip) in [(0x14, 0x5002), (0x44, 0x5007), (0xc0, 0x5002), (0xc8, 0x5002)] {
            let (r, note) = locate(area, pc(o)).unwrap();
            assert!(r == rip && note.starts_with("exact"), "{o:#x}: {note}");
        }
        // (0x60: past the second instruction's access, before the stub: not exact, though the stub's instruction starts before.)
        for (o, low, high) in [(0x8, 0x5000, 0x5002), (0x30, 0x5002, 0x5007), (0x60, 0x5007, 0x500c)] {
            let (rip, note) = locate(area, pc(o)).unwrap();
            assert!(rip == low && note.contains(&format!("[{low:#x}, {high:#x})")), "{o:#x}: {note}");
        }
        assert!(locate(area, pc(0x100)).is_none());
    }
}
