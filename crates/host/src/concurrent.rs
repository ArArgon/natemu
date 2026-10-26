//! VM lifetime belongs to this caller; every vCPU belongs to one scoped thread.
use super::*;
use natemu_abi::concurrency::*;
use natemu_abi::translation as wire;
mod producer;
use std::{
    sync::{atomic::Ordering, mpsc},
    thread,
    time::{Duration, Instant},
};

struct Machine;
impl Drop for Machine {
    fn drop(&mut self) {
        unsafe {
            hv_vm_destroy();
        }
    }
}
struct Cpu {
    id: u64,
    exit: *const HvExit,
}
impl Cpu {
    fn new() -> Result<Self> {
        let mut c = Self {
            id: 0,
            exit: ptr::null(),
        };
        unsafe {
            hv(
                hv_vcpu_create(&mut c.id, &mut c.exit, ptr::null()),
                "create owner vCPU",
            )?;
        }
        Ok(c)
    }
    fn reg(&self, r: u32, value: u64) -> Result<()> {
        unsafe { hv(hv_vcpu_set_reg(self.id, r, value), "set owner register") }
    }
    fn sys(&self, r: u16, value: u64) -> Result<()> {
        unsafe { hv(hv_vcpu_set_sys_reg(self.id, r, value), "set owner control") }
    }
    fn run(&self, shared_address: usize, compiler: &mut Option<producer::Compiler>) -> Result<()> {
        loop {
            unsafe {
                hv(hv_vcpu_run(self.id), "run owner vCPU")?;
                let e = &*self.exit;
                let mut pc = 0;
                hv(hv_vcpu_get_reg(self.id, 31, &mut pc), "read owner PC")?;
                if e.reason != 1 || e.exception.syndrome >> 26 != 0x16 {
                    return Err(format!(
                        "owner exit reason={} ESR={:#x} PC={pc:#x} VA={:#x}",
                        e.reason, e.exception.syndrome, e.exception.virtual_address
                    ));
                }
                match e.exception.syndrome & 0xffff {
                    0x49 => return Ok(()),
                    0x4a => {
                        // Only this owner writes its private record, and is
                        // stopped at the explicit register-transfer gateway.
                        let s = ptr::read_volatile(shared_address as *const Shared);
                        if s.service_pc < PRIVATE
                            || s.service_pc >= PRIVATE + 0x10000
                            || pc != s.service_pc
                        {
                            return Err("compiler service outside explicit gateway".into());
                        }
                        let mut packet = [0; 16];
                        for (r, v) in packet.iter_mut().enumerate() {
                            hv(hv_vcpu_get_reg(self.id, r as u32, v), "read service packet")?;
                        }
                        let mut expected = s.lifecycle.packet();
                        expected[0] = packet[0];
                        if packet != expected {
                            return Err("compiler request differs from resident identity".into());
                        }
                        let reply = compiler
                            .as_mut()
                            .ok_or("compiler service on non-owner")?
                            .exchange(packet)?;
                        for (r, v) in reply.iter().enumerate() {
                            self.reg(r as u32, *v)?;
                        }
                    }
                    _ => return Err("unexpected owner HVC".into()),
                }
            }
        }
    }
}
impl Drop for Cpu {
    fn drop(&mut self) {
        unsafe {
            hv_vcpu_destroy(self.id);
        }
    }
}

// Each root has its own boot alias and private state/stacks. The immutable
// native object, guest data and coordinator deliberately share physical pages.
fn tables(memory: &Memory, base: usize) {
    let word = |at, value| memory.word(base + at, value);
    let table = |offset| IPA + base as u64 + offset + 3;
    let attrs = 3 | 1 << 10 | 3 << 8;
    word(0x10000, table(0x11000));
    word(0x10000 + 256 * 8, table(0x12000));
    word(0x11008, IPA | 1 | 1 << 10 | 3 << 8);
    word(0x11000, table(0x15000));
    word(0x15000, table(0x16000));
    word(
        0x16000 + 256 * 8,
        (IPA + DATA_OFFSET as u64) | attrs | (3 << 53),
    );
    word(0x12008, table(0x13000));
    word(0x13000, table(0x14000));
    for offset in (0..RAM_SIZE).step_by(4096) {
        let text = offset < 0x10000 || (CODE_OFFSET..CODE_OFFSET + 8192).contains(&offset);
        let writable = (0x10000..0x17000).contains(&offset)
            || (SHARED_OFFSET..SHARED_OFFSET + 0x10000).contains(&offset)
            || (BANK_OFFSET..BANK_OFFSET + 0x2000).contains(&offset)
            || (0x70000..0x84000).contains(&offset)
            || (0x50000..0x54000).contains(&offset)
            || (0x60000..0x64000).contains(&offset)
            || offset == COORD_OFFSET;
        if text || writable {
            let common = (CODE_OFFSET..CODE_OFFSET + 8192).contains(&offset)
                || offset == COORD_OFFSET
                || (DATA_OFFSET..0x84000).contains(&offset);
            let pa = IPA + offset as u64 + if common { 0 } else { base as u64 };
            word(
                0x14000 + offset / 4096 * 8,
                pa | attrs | if text { 1 << 7 } else { 3 << 53 },
            );
        }
    }
}

fn prepare_cpu(id: usize, tcr: u64) -> Result<Cpu> {
    let cpu = Cpu::new()?;
    let mut isar0 = 0;
    unsafe {
        hv(
            hv_vcpu_get_sys_reg(cpu.id, 0xc030, &mut isar0),
            "read FlagM",
        )?;
    }
    if (isar0 >> 52) & 15 == 0 {
        return Err("FlagM required".into());
    }
    cpu.reg(31, IPA + (id * RAM_SIZE) as u64)?;
    cpu.reg(34, 0x3c5)?;
    cpu.reg(0, IPA + (id * RAM_SIZE + 0x10000) as u64)?;
    cpu.reg(1, tcr)?;
    cpu.reg(2, 0xff)?;
    cpu.sys(0xc080, 0x30d00980)?;
    cpu.sys(0xc081, 2)?;
    cpu.sys(0xc082, 3 << 20)?;
    cpu.sys(0xdf19, 0)?;
    Ok(cpu)
}

fn run_owners(
    n: usize,
    tcr: u64,
    delay: u64,
    memory: usize,
    compiler_config: Option<(std::path::PathBuf, std::path::PathBuf)>,
) -> Result<Vec<wire::Packet>> {
    thread::scope(|scope| {
        let (prepared_tx, prepared_rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        let mut starts = Vec::new();
        let mut releases = Vec::new();
        let mut owners = Vec::new();
        for id in 0..n {
            let (start_tx, start_rx) = mpsc::channel::<()>();
            let (release_tx, release_rx) = mpsc::channel::<()>();
            let prepared = prepared_tx.clone();
            let done = done_tx.clone();
            let compiler_config = if id == 0 {
                compiler_config.clone()
            } else {
                None
            };
            let owner = thread::Builder::new()
                .name(format!("vcpu-{id}"))
                .spawn_scoped(scope, move || -> Result<Vec<wire::Packet>> {
                    let cpu = std::panic::catch_unwind(|| prepare_cpu(id, tcr))
                        .unwrap_or_else(|_| Err("owner panicked during setup".into()));
                    prepared
                        .send(cpu.as_ref().map(|c| c.id).map_err(Clone::clone))
                        .map_err(|_| "setup receiver closed")?;
                    let cpu = cpu?;
                    // No native entry until *all* owners successfully prepare.
                    let mut compiler = None;
                    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        start_rx.recv().map_err(|_| "owner startup cancelled")?;
                        if id == n - 1 {
                            thread::sleep(Duration::from_millis(delay));
                        }
                        if let Some((binary, output)) = compiler_config {
                            compiler = Some(producer::Compiler::new(binary, output)?);
                        }
                        cpu.run(memory + id * RAM_SIZE + SHARED_OFFSET, &mut compiler)
                    }))
                    .unwrap_or_else(|_| Err("owner panicked during execution".into()));
                    let _ = done.send(result.clone());
                    // Keep IDs alive while the supervisor may cancel peers.
                    let _ = release_rx.recv();
                    result?;
                    Ok(compiler.map_or_else(Vec::new, |mut c| std::mem::take(&mut c.requests)))
                })
                .map_err(|e| e.to_string());
            match owner {
                Ok(h) => {
                    owners.push(h);
                    starts.push(start_tx);
                    releases.push(release_tx);
                }
                Err(e) => {
                    drop(starts);
                    drop(releases);
                    return Err(e);
                }
            }
        }
        drop(prepared_tx);
        drop(done_tx);
        let mut ids = Vec::new();
        for _ in 0..n {
            match prepared_rx.recv().map_err(|_| "owner setup disconnected")? {
                Ok(id) => ids.push(id),
                Err(e) => {
                    drop(starts);
                    drop(releases);
                    return Err(e);
                }
            }
        }
        let mut error = None;
        for start in starts {
            if start.send(()).is_err() {
                error.get_or_insert("owner disappeared before entry".into());
            }
        }
        let began = Instant::now();
        let mut completed = 0;
        while completed < n {
            match done_rx.recv_timeout(Duration::from_millis(20)) {
                Ok(result) => {
                    completed += 1;
                    if let Err(e) = result {
                        error.get_or_insert(e);
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    error.get_or_insert("owner completion disconnected".into());
                    break;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
            if began.elapsed()
                > Duration::from_secs(if compiler_config.is_some() { 100 } else { 10 })
            {
                error.get_or_insert("concurrent execution exceeded its deadline".into());
            }
            if error.is_some() && completed < n {
                // Repeat cancellation: an owner may still be in its declared
                // pre-entry delay when an earlier cancellation is issued.
                unsafe {
                    hv_vcpus_exit(ids.as_mut_ptr(), ids.len() as u32);
                }
            }
        }
        drop(releases);
        let mut requests = Vec::new();
        for h in owners {
            match h.join() {
                Ok(Ok(mut r)) => requests.append(&mut r),
                Ok(Err(e)) => {
                    error.get_or_insert(e);
                }
                Err(_) => {
                    error.get_or_insert("owner panicked".into());
                }
            }
        }
        error.map_or(Ok(requests), Err)
    })
}

pub fn run(args: &[String]) -> Result<()> {
    let n: usize = args[4].parse().map_err(|_| "invalid I4 vCPU count")?;
    let mut coord = Coordinator::new(n).ok_or("I4 admits 1..=4 vCPUs")?;
    let mut host_limit = 0;
    unsafe {
        hv(
            hv_vm_get_max_vcpu_count(&mut host_limit),
            "read host vCPU limit",
        )?;
    }
    if n > host_limit as usize {
        return Err("I4 count exceeds host vCPU limit".into());
    }
    coord.scenario = match args[3].as_str() {
        "entry" => 0,
        "faults" => 1,
        "remap" => 2,
        "compose" => 3,
        _ => return Err("unsupported I4 scenario".into()),
    };
    let compose = args[3] == "compose";
    if compose {
        coord.published.store(0, Ordering::Relaxed);
    }
    let delay: u64 = if compose {
        50
    } else {
        args[6].parse().map_err(|_| "invalid owner delay")?
    };
    if delay > 1000 {
        return Err("owner delay exceeds 1000ms".into());
    }
    let body = if compose {
        FEX_BODY.to_vec()
    } else {
        read_input(&args[5], 44)?
    };
    if body != FEX_BODY {
        return Err("I4 requires pinned FEX fragment".into());
    }
    let memory = Memory::with_size(n * RAM_SIZE)?;
    load_elf(&memory, &read_input(&args[1], 2 * 1024 * 1024)?)?;
    let image = memory.read::<0x10000>(0);
    memory.copy(CODE_OFFSET, &body);
    memory.copy(CODE_OFFSET + 44, &RET);
    if compose {
        memory.copy(natemu_abi::recovery::SOURCE_OFFSET, &wire::XOR);
    }
    unsafe {
        ptr::write(memory.0.add(COORD_OFFSET).cast::<Coordinator>(), coord);
    }
    let mut vm_id = 0u64;
    if unsafe { getentropy((&raw mut vm_id).cast(), 8) } != 0 || vm_id == 0 {
        return Err("VM identity entropy unavailable".into());
    }
    let mut seeds = Vec::new();
    let mut new_data = Vec::new();
    for id in 0..n {
        let base = id * RAM_SIZE;
        if id != 0 {
            memory.copy(base, &image);
        }
        tables(&memory, base);
        let (mut frame, mut bank, data) = seed(id);
        for b in &mut bank {
            *b ^= id as u8;
        }
        frame.x[6] += (id * 256) as u64;
        frame.x[11] += (id * 256) as u64;
        memory.copy(base + BANK_OFFSET, &bank);
        memory.copy(DATA_OFFSET + id * 256, &data);
        let mut replacement = data.map(|b| b ^ 0x5a);
        replacement[..8].copy_from_slice(&(0xd00d_9876_5432_1000u64 + id as u64).to_le_bytes());
        memory.copy(DATA_OFFSET + 4096 + id * 256, &replacement);
        new_data.push(replacement);
        let mut object = Object::new(LIVE_LOAD);
        object.vcpu_id = id as u32;
        object.configured_vcpus = n as u32;
        let shared = Shared {
            object,
            initial: frame,
            captured: NativeFrame::ZERO,
            status: 0,
            detail: 0,
            capture_pc: 0,
            esr: 0,
            far: 0,
            elr: 0,
            spsr: 0,
            current_el: 0,
            sctlr: 0,
            actlr: 0,
            tcr: 0,
            service_pc: 0,
            mode: MODE,
            lifecycle: natemu_abi::translation::Lifecycle::new(vm_id),
        };
        unsafe {
            ptr::write(memory.0.add(base + SHARED_OFFSET).cast::<Shared>(), shared);
        }
        seeds.push((frame, bank, data));
    }
    if args[3] != "entry" {
        recovery::prepare(&memory, &seeds[0].0, "cold", false)?;
        for id in 0..n {
            unsafe {
                ptr::write(
                    memory.0.add(id * RAM_SIZE + ROUND_OFFSET).cast::<Round>(),
                    Round::ZERO,
                );
                ptr::write(
                    memory
                        .0
                        .add(id * RAM_SIZE + natemu_abi::recovery::STATE_OFFSET)
                        .cast::<natemu_abi::recovery::Recovery>(),
                    natemu_abi::recovery::Recovery::new(0),
                );
            }
        }
    }
    unsafe {
        sys_icache_invalidate(memory.0.cast(), memory.1);
        hv(hv_vm_create(ptr::null()), "create concurrent VM")?;
    }
    let _machine = Machine;
    unsafe {
        hv(
            hv_vm_map(memory.0.cast(), IPA, memory.1, 7),
            "map concurrent backing",
        )?;
    }
    let tcr = 16 | 1 << 8 | 1 << 10 | 3 << 12 | 1 << 23 | 2 << 32;
    let requests = run_owners(
        n,
        tcr,
        delay,
        memory.0 as usize,
        if compose {
            Some((args[5].clone().into(), args[6].clone().into()))
        } else {
            None
        },
    )?;
    // Every owner has exited and destroyed its vCPU before host reads guest RAM.
    let c = unsafe { &*memory.0.add(COORD_OFFSET).cast::<Coordinator>() };
    let mut cells = Vec::new();
    let mut faults = Vec::new();
    let mut new_cells = Vec::new();
    for (id, (frame, bank, data)) in seeds.iter().enumerate() {
        let base = id * RAM_SIZE;
        let s = unsafe { ptr::read(memory.0.add(base + SHARED_OFFSET).cast::<Shared>()) };
        if s.status != SUCCESS || s.current_el != 4 || s.actlr & 2 != 2 || s.tcr != tcr {
            return Err(format!("CPU{id}: status={} detail={}", s.status, s.detail));
        }
        let mut entry = *frame;
        entry.x[16] = CODE_VA;
        entry.x[30] = s.capture_pc;
        if !matches!(args[3].as_str(), "remap" | "compose") && s.initial != entry {
            return Err(format!("CPU{id}: entry state changed"));
        }
        let r = unsafe { ptr::read(memory.0.add(base + ROUND_OFFSET).cast::<Round>()) };
        if args[3] != "entry" {
            faults.push(format!("{{\"id\":{id},\"faults\":{},\"old_gpa\":{},\"frame\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{}}}",
                r.faults,r.old_gpa,frame_json(&r.fault_frame),r.fault_esr,r.fault_far,r.fault_pc,r.fault_spsr));
        }
        if matches!(args[3].as_str(), "remap" | "compose") {
            if s.initial != r.new_initial {
                return Err("new entry state changed".into());
            }
            let code = if compose { CODE_NEXT } else { CODE_VA };
            let case = if compose { "live_or" } else { "live_load" };
            let dirty = format!(
                "{{\"frame\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{}}}",
                frame_json(&r.dirty_frame),
                r.dirty_esr,
                r.dirty_far,
                r.dirty_pc,
                r.dirty_spsr
            );
            new_cells.push(format!("{{\"dirty_fault\":{dirty},\"id\":{id},\"dirty_faults\":{},\"case\":\"{case}\",\"status\":{},\"initial\":{},\"captured\":{},\"memory_base\":{},\"memory_before\":{:?},\"memory_after\":{:?},\"bank_before\":{bank:?},\"bank_after\":{:?},\"object_pc\":{code},\"capture_pc\":{}}}",
                r.dirty_faults,s.status,frame_json(&r.new_initial),frame_json(&s.captured),DATA_VA+(id*256) as u64,
                new_data[id],memory.read::<256>(DATA_OFFSET+4096+id*256),memory.read::<512>(base+BANK_OFFSET),s.capture_pc));
        }
        let old_frame = if matches!(args[3].as_str(), "remap" | "compose") {
            &r.first_frame
        } else {
            &s.captured
        };
        cells.push(format!("{{\"id\":{id},\"case\":\"live_load\",\"status\":{},\"initial\":{},\"captured\":{},\"memory_base\":{},\"memory_before\":{data:?},\"memory_after\":{:?},\"bank_before\":{bank:?},\"bank_after\":{:?},\"object_pc\":{CODE_VA},\"capture_pc\":{}}}",
            s.status,frame_json(frame),frame_json(old_frame),DATA_VA+(id*256) as u64,
            memory.read::<256>(DATA_OFFSET+id*256),memory.read::<512>(base+BANK_OFFSET),s.capture_pc));
    }
    let trace: Vec<_> = c
        .trace
        .iter()
        .take(c.trace_length.load(Ordering::Acquire) as usize)
        .map(|v| v.load(Ordering::Relaxed))
        .collect();
    let scenario = &args[3];
    let source_summary = format!(
        "{{\"old_eligible\":{},\"published\":{},\"stale_replies\":{},\"stale_entries\":{},\"reuse_blocked\":{},\"bytes\":{:?},\"old_code\":{:?},\"new_code\":{:?}}}",
        c.old_eligible.load(Ordering::Acquire),
        c.published.load(Ordering::Acquire),
        c.stale_replies.load(Ordering::Acquire),
        c.stale_entries.load(Ordering::Acquire),
        c.reuse_blocked.load(Ordering::Acquire),
        memory.read::<17>(natemu_abi::recovery::SOURCE_OFFSET),
        memory.read::<48>(CODE_OFFSET),
        memory.read::<48>(CODE_OFFSET + 4096)
    );
    let summary = format!(
        "{{\"generation\":{},\"acknowledgements\":{},\"active\":{},\"new_entries\":{},\"new_completed\":{},\"new_references\":{},\"reclaimed\":{},\"leaf\":{}}}",
        c.generation.load(Ordering::Acquire),
        c.acknowledgements.load(Ordering::Acquire),
        c.active.load(Ordering::Acquire),
        c.new_entries.load(Ordering::Acquire),
        c.new_completed.load(Ordering::Acquire),
        c.new_references.load(Ordering::Acquire),
        c.reclaimed.load(Ordering::Acquire),
        u64::from_le_bytes(memory.read::<8>(natemu_abi::recovery::TABLE_OFFSET + 0x3000 + 256 * 8))
    );
    println!(
        "{{\"source\":{source_summary},\"requests\":{requests:?},\"coherence\":{summary},\"new_cells\":[{}],\"profile\":\"i4-{scenario}\",\"trace\":{trace:?},\"faults\":[{}],\"retained\":{},\"configured_vcpus\":{n},\"tested_vcpus\":{n},\"delayed_owner_ms\":{delay},\"ready\":{},\"finished\":{},\"cells\":[{}]}}",
        new_cells.join(","),
        faults.join(","),
        c.retained.load(Ordering::Acquire),
        c.ready.load(Ordering::Acquire),
        c.finished.load(Ordering::Acquire),
        cells.join(",")
    );
    Ok(())
}
