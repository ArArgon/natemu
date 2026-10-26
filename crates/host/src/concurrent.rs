//! VM lifetime belongs to this caller; every vCPU belongs to one scoped thread.
use super::*;
use natemu_abi::concurrency::*;
use natemu_abi::translation as wire;
use natemu_abi::{alias, shadow, stack, stack_concurrent};
mod address;
mod ordering;
pub use ordering::run as run_ordering;
mod boundary;
mod controls;
mod mixed;
mod mixed_producer;
mod producer;
mod stack_producer;
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
                    0x49 => {
                        let s = ptr::read_volatile(shared_address as *const Shared);
                        if s.status == PANIC {
                            return Err(format!("resident owner panic detail={}", s.detail));
                        }
                        return Ok(());
                    }
                    0x4a => {
                        // Only this owner writes its private record, and is
                        // stopped at the explicit register-transfer gateway.
                        let s = ptr::read_volatile(shared_address as *const Shared);
                        if !resident::contains(s.service_pc, 4) || pc != s.service_pc {
                            return Err("compiler service outside explicit gateway".into());
                        }
                        let mut packet = [0; 16];
                        for (r, v) in packet.iter_mut().enumerate() {
                            hv(hv_vcpu_get_reg(self.id, r as u32, v), "read service packet")?;
                        }
                        let expected = if compiler.as_ref().is_some_and(|c| c.is_ordering()) {
                            (*((shared_address - SHARED_OFFSET + natemu_abi::ordering::OWNER_OFFSET)
                                as *const natemu_abi::ordering::Owner))
                                .expected
                        } else if compiler.as_ref().is_some_and(|c| c.is_address()) {
                            (*((shared_address - SHARED_OFFSET
                                + natemu_abi::address_concurrent::OWNER_OFFSET)
                                as *const natemu_abi::address_concurrent::Owner))
                                .expected
                        } else if compiler.as_ref().is_some_and(|c| c.is_controls()) {
                            ptr::read_volatile(
                                (shared_address - SHARED_OFFSET + natemu_abi::controls::OFFSET)
                                    as *const natemu_abi::controls::State,
                            )
                            .expected
                        } else if compiler.as_ref().is_some_and(|c| c.is_mixed()) {
                            ptr::read_volatile(
                                (shared_address - SHARED_OFFSET + natemu_abi::mixed32::OFFSET)
                                    as *const natemu_abi::mixed32::State,
                            )
                            .expected
                        } else if compiler.as_ref().is_some_and(|c| c.is_stack()) {
                            ptr::read_volatile(
                                (shared_address - SHARED_OFFSET + stack_concurrent::OFFSET)
                                    as *const stack_concurrent::State,
                            )
                            .expected
                        } else {
                            let mut expected = s.lifecycle.packet();
                            expected[0] = packet[0];
                            expected
                        };
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
fn tables(memory: &Memory, base: usize, aliases: bool) {
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
    if aliases {
        word(shadow::UPPER_ROOT + 256 * 8, table(shadow::UPPER_L1 as u64));
        word(shadow::UPPER_L1, table(shadow::UPPER_L2 as u64));
        word(shadow::UPPER_L2, table(shadow::UPPER_LEAF as u64));
    }
    for offset in (0..RAM_SIZE).step_by(4096) {
        let text = resident::executable_offset(offset)
            || (CODE_OFFSET..CODE_OFFSET + 8192).contains(&offset);
        let writable = (0x10000..if aliases { 0x1b000 } else { 0x17000 }).contains(&offset)
            || (SHARED_OFFSET..SHARED_OFFSET + 0x10000).contains(&offset)
            || (BANK_OFFSET..BANK_OFFSET + 0x2000).contains(&offset)
            || (0x70000..0x84000).contains(&offset)
            || (0x50000..0x54000).contains(&offset)
            || (0x60000..0x64000).contains(&offset)
            || (COORD_OFFSET..0x93000).contains(&offset);
        if text || writable {
            let common = (CODE_OFFSET..CODE_OFFSET + 8192).contains(&offset)
                || (COORD_OFFSET..0x93000).contains(&offset)
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
    compiler_config: Option<(std::path::PathBuf, std::path::PathBuf, u64, bool)>,
    mixed_profile: u64,
    controls_config: Option<(std::path::PathBuf, std::path::PathBuf, std::path::PathBuf)>,
    address_config: Option<(
        std::path::PathBuf,
        std::path::PathBuf,
        std::path::PathBuf,
        natemu_abi::address_concurrent::Config,
    )>,
    ordering_config: Option<(
        std::path::PathBuf,
        std::path::PathBuf,
        std::path::PathBuf,
        natemu_abi::ordering::Config,
    )>,
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
            let controls_config = controls_config.clone();
            let address_config = address_config.clone();
            let ordering_config = ordering_config.clone();
            let compiler_config = if id == 0 || mixed_profile != 0 {
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
                        if let Some((binary, worker, output, config)) = ordering_config {
                            compiler = Some(producer::Compiler::new_ordering(
                                binary,
                                worker,
                                output.join(format!("cpu-{id}")),
                                id as u64,
                                config,
                            )?);
                        } else if let Some((binary, worker, output, config)) = address_config {
                            compiler = Some(producer::Compiler::new_address(
                                binary,
                                worker,
                                output.join(format!("cpu-{id}")),
                                id as u64,
                                config,
                            )?);
                        } else if let Some((binary, worker, output)) = controls_config {
                            compiler = Some(producer::Compiler::new_controls(
                                binary,
                                worker,
                                output.join(format!("cpu-{id}")),
                                id as u64,
                            )?);
                        } else if let Some((binary, output, profile, is_stack)) = compiler_config {
                            compiler = Some(if mixed_profile != 0 {
                                producer::Compiler::new_mixed(
                                    binary,
                                    output.join(format!("cpu-{id}")),
                                    id as u64,
                                    natemu_abi::mixed32::owner_mode(mixed_profile, id as u64)
                                        .unwrap(),
                                )?
                            } else if is_stack {
                                producer::Compiler::new_stack(
                                    binary,
                                    output,
                                    alias::source_rip(profile),
                                )?
                            } else if profile == 0 {
                                producer::Compiler::new(binary, output)?
                            } else {
                                producer::Compiler::new_alias(
                                    binary,
                                    output,
                                    alias::source_rip(profile),
                                )?
                            });
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
                > Duration::from_secs(if compiler_config.is_some() || controls_config.is_some() || address_config.is_some() || ordering_config.is_some() {
                    100
                } else {
                    10
                })
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
        for (id, h) in owners.into_iter().enumerate() {
            match h.join() {
                Ok(Ok(mut r)) => requests.append(&mut r),
                Ok(Err(e)) => {
                    eprintln!("owner {id}: {e}");
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

// Keep profile admission explicit: source address selection and permission setup
// are independent fixture inputs, not values inferred from compiler replies.
fn profiles(name: &str) -> Result<(u64, u64, u64)> {
    Ok(match name {
        "entry" => (0, 0, 0),
        "faults" => (1, 0, 0),
        "remap" => (2, 0, 0),
        "compose" => (3, 0, 0),
        "events" => (4, 0, 0),
        "pressure" => (5, 0, 0),
        "alias-low" => (5, 1, 0),
        "alias-high" => (5, 2, 0),
        "stack-low" => (5, 1, 1),
        "stack-high" => (5, 2, 1),
        "stack-parent-low" => (5, 1, 2),
        "stack-parent-high" => (5, 2, 2),
        "stack-leaf-low" => (5, 1, 3),
        "stack-leaf-high" => (5, 2, 3),
        _ => return Err("unsupported I4 scenario".into()),
    })
}

pub fn run_controls(args: &[String]) -> Result<()> {
    controls::run(args)
}
pub fn run_address(args: &[String]) -> Result<()> {
    address::run(args)
}

pub fn run(args: &[String]) -> Result<()> {
    let n: usize = args[4].parse().map_err(|_| "invalid I4 vCPU count")?;
    let boundary_config = boundary::Config::parse(args, n)?;
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
    let mixed_profile = if args[2] == "i4v-mixed" {
        match args[3].as_str() {
            "off-first" => 1,
            "paged-first" => 2,
            _ => return Err("unsupported mixed arrangement".into()),
        }
    } else {
        0
    };
    let (scenario_id, alias_profile, stack_kind) = if let Some(config) = &boundary_config {
        (5, config.alias_profile, 1)
    } else if mixed_profile != 0 {
        (5, 0, 0)
    } else {
        profiles(&args[3])?
    };
    coord.mixed_profile = mixed_profile;
    let stack_profile = stack_kind != 0;
    coord.stack_profile = stack_kind;
    coord.scenario = scenario_id;
    coord.alias_profile = alias_profile;
    let source_rip = alias::source_rip(alias_profile);
    let compose = scenario_id >= 3;
    if compose && mixed_profile == 0 {
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
    let body = if stack_profile || mixed_profile != 0 {
        Vec::new()
    } else if compose {
        FEX_BODY.to_vec()
    } else {
        read_input(&args[5], 44)?
    };
    if !stack_profile && mixed_profile == 0 && body != FEX_BODY {
        return Err("I4 requires pinned FEX fragment".into());
    }
    let memory = Memory::with_size(n * RAM_SIZE)?;
    load_elf(&memory, &read_input(&args[1], 2 * 1024 * 1024)?)?;
    if !stack_profile && mixed_profile == 0 {
        memory.copy(CODE_OFFSET, &body);
        memory.copy(CODE_OFFSET + 44, &RET);
    } else {
        memory.copy(DATA_OFFSET, &[0xa5; 8192]);
    }
    if mixed_profile != 0 {
        memory.copy(DATA_OFFSET + 4096, &[0x5a; 4096]);
    }
    if compose {
        memory.copy(
            natemu_abi::recovery::SOURCE_OFFSET,
            if mixed_profile != 0 {
                &natemu_abi::mixed32::SOURCE[..]
            } else if stack_profile {
                &stack::SOURCE[..]
            } else {
                &wire::XOR[..]
            },
        );
    }
    unsafe {
        ptr::write(memory.0.add(COORD_OFFSET).cast::<Coordinator>(), coord);
    }
    let mut vm_id = 0u64;
    if unsafe { getentropy((&raw mut vm_id).cast(), 8) } != 0 || vm_id == 0 {
        return Err("VM identity entropy unavailable".into());
    }
    let pressure = scenario_id == 5;
    if pressure {
        unsafe {
            ptr::write(
                memory
                    .0
                    .add(natemu_abi::maintenance::OFFSET)
                    .cast::<natemu_abi::maintenance::State>(),
                natemu_abi::maintenance::State::new(n).unwrap(),
            );
        }
    }
    let mut seeds = Vec::new();
    let mut new_data = Vec::new();
    for id in 0..n {
        let base = id * RAM_SIZE;
        if id != 0 {
            for offset in (0..RAM_SIZE).step_by(4096) {
                if resident::executable_offset(offset) {
                    memory.copy(base + offset, &memory.read::<4096>(offset));
                }
            }
        }
        tables(&memory, base, alias_profile != 0);
        if boundary_config.is_some() {
            boundary::prepare_owner(&memory, id);
        }
        if mixed_profile != 0 {
            mixed::private_code(&memory, base);
        }
        let (mut frame, mut bank, mut data) = seed(id);
        for b in &mut bank {
            *b ^= id as u8;
        }
        frame.x[6] = if stack_profile {
            stack_concurrent::initial_rsp_for(stack_kind, id as u64) - 0x80
        } else {
            alias::data_address(alias_profile, id as u64, 0)
        };
        frame.x[11] = frame.x[6] + 128;
        if stack_profile {
            frame.x[8] = stack_concurrent::initial_rsp_for(stack_kind, id as u64);
            frame.x[5] = frame.x[8];
            data.fill(0xa5);
        }
        if mixed_profile != 0 {
            mixed::seed_owner(&mut frame, &mut data, id);
        }
        memory.copy(base + BANK_OFFSET, &bank);
        memory.copy(DATA_OFFSET + id * 256, &data);
        let mut replacement = data.map(|b| b ^ 0x5a);
        if mixed_profile != 0 {
            replacement = mixed::replacement(id);
        } else if stack_profile {
            replacement.fill(0xa5);
        } else {
            replacement[..8].copy_from_slice(&(0xd00d_9876_5432_1000u64 + id as u64).to_le_bytes());
        }
        memory.copy(DATA_OFFSET + 4096 + id * 256, &replacement);
        new_data.push(replacement);
        let mut object = Object::new(LIVE_LOAD);
        if mixed_profile != 0 {
            object.code_length = 24;
        }
        if stack_profile {
            object.code_length = stack::CODE_BYTES as u64;
        }
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
            lifecycle: if alias_profile != 0 {
                natemu_abi::translation::Lifecycle::new_at(vm_id, source_rip)
            } else {
                natemu_abi::translation::Lifecycle::new(vm_id)
            },
        };
        unsafe {
            ptr::write(memory.0.add(base + SHARED_OFFSET).cast::<Shared>(), shared);
            if mixed_profile != 0 {
                let mode = natemu_abi::mixed32::owner_mode(mixed_profile, id as u64).unwrap();
                let mut m = natemu_abi::mixed32::State::ZERO;
                m.mode = mode;
                m.incarnation = 1;
                m.context = mode + 1;
                m.a20 = 1;
                m.initial = frame;
                ptr::write(
                    memory
                        .0
                        .add(base + natemu_abi::mixed32::OFFSET)
                        .cast::<natemu_abi::mixed32::State>(),
                    m,
                );
            }
            if stack_profile {
                ptr::write(
                    memory
                        .0
                        .add(base + stack_concurrent::OFFSET)
                        .cast::<stack_concurrent::State>(),
                    stack_concurrent::State::ZERO,
                );
            }
            if alias_profile != 0 {
                ptr::write(
                    memory.0.add(base + alias::OFFSET).cast::<alias::Record>(),
                    alias::Record::ZERO,
                );
            }
        }
        seeds.push((frame, bank, data));
    }
    if args[3] != "entry" {
        recovery::prepare(&memory, &seeds[0].0, "cold", false)?;
        if alias_profile != 0 {
            memory.word(
                natemu_abi::recovery::TABLE_OFFSET + 256 * 8,
                if stack_kind == 2 { 0x2005 } else { 0x2007 },
            );
            if stack_kind == 3 {
                // A distinct high leaf aliases the writable low GPA but denies writes.
                memory.word(
                    natemu_abi::recovery::TABLE_OFFSET + 0x3000 + 257 * 8,
                    DATA_VA | 5,
                );
            }
        }
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
                if scenario_id >= 4 {
                    let mut guest = if mixed_profile != 0 {
                        mixed::guest(&seeds[id].0, id, mixed_profile)
                    } else {
                        natemu_abi::recovery::GuestState::seed(&seeds[id].0)
                    };
                    guest.rip = source_rip;
                    if stack_profile {
                        guest.gpr[4] = seeds[id].0.x[8];
                    }
                    if id == 0 {
                        guest.rflags &= !0x200;
                    }
                    ptr::write(
                        memory
                            .0
                            .add(id * RAM_SIZE + natemu_abi::recovery::GUEST_OFFSET)
                            .cast::<natemu_abi::recovery::GuestState>(),
                        guest,
                    );
                    let mut event = EventRecord::new(&seeds[id].0, id);
                    event.packet.rip = source_rip;
                    if mixed_profile != 0 {
                        event.packet = guest;
                    }
                    if stack_profile {
                        event.packet.gpr[4] = seeds[id].0.x[8];
                    }
                    ptr::write(
                        memory
                            .0
                            .add(id * RAM_SIZE + EVENT_OFFSET)
                            .cast::<EventRecord>(),
                        event,
                    );
                }
            }
        }
    }
    if let Some(config) = &boundary_config {
        boundary::prepare_shared(&memory, config, n);
    }
    let mixed_pages_before = if mixed_profile != 0 {
        memory.read::<8192>(DATA_OFFSET).to_vec()
    } else {
        Vec::new()
    };
    let mixed_tables_before = if mixed_profile != 0 {
        memory
            .read::<16384>(natemu_abi::recovery::TABLE_OFFSET)
            .to_vec()
    } else {
        Vec::new()
    };
    if mixed_profile != 0 {
        fs::create_dir(&args[6]).map_err(|e| e.to_string())?;
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
    let tcr = if alias_profile != 0 {
        shadow::TCR_BOTH
    } else {
        16 | 1 << 8 | 1 << 10 | 3 << 12 | 1 << 23 | 2 << 32
    };
    let execution = run_owners(
        n,
        tcr,
        delay,
        memory.0 as usize,
        if compose {
            Some((
                args[5].clone().into(),
                args[6].clone().into(),
                alias_profile,
                stack_profile,
            ))
        } else {
            None
        },
        mixed_profile,
        None,
        None,
        None,
    );
    let requests = match execution {
        Ok(requests) => requests,
        Err(error) => {
            if let Some(config) = &boundary_config {
                // run_owners joins all owners before returning an error.
                eprintln!(
                    "boundary failure state: {}",
                    boundary::report(&memory, config, n, "null")
                );
            }
            return Err(error);
        }
    };
    // Every owner has exited and destroyed its vCPU before host reads guest RAM.
    let c = unsafe { &*memory.0.add(COORD_OFFSET).cast::<Coordinator>() };
    let boundary_shared = boundary_config.as_ref().map(|_| boundary::shared(&memory));
    let mut cells = Vec::new();
    let mut faults = Vec::new();
    let mut new_cells = Vec::new();
    let mut event_records = Vec::new();
    let mut alias_records = Vec::new();
    let mut stack_records = Vec::new();
    let mut mixed_records = Vec::new();
    for (id, (frame, bank, data)) in seeds.iter().enumerate() {
        let base = id * RAM_SIZE;
        let boundary_owner = boundary_config
            .as_ref()
            .map(|_| boundary::owner(&memory, id));
        let s = unsafe { ptr::read(memory.0.add(base + SHARED_OFFSET).cast::<Shared>()) };
        if s.status != SUCCESS || s.current_el != 4 || s.actlr & 2 != 2 || s.tcr != tcr {
            return Err(format!("CPU{id}: status={} detail={}", s.status, s.detail));
        }
        let mut entry = *frame;
        entry.x[16] = CODE_VA;
        entry.x[30] = s.capture_pc;
        if scenario_id < 2 && s.initial != entry {
            return Err(format!("CPU{id}: entry state changed"));
        }
        let r = unsafe { ptr::read(memory.0.add(base + ROUND_OFFSET).cast::<Round>()) };
        if mixed_profile != 0 {
            mixed_records.push(mixed::report(&memory, id, mixed_profile, frame));
        }
        if scenario_id >= 4 {
            let e = unsafe { ptr::read(memory.0.add(base + EVENT_OFFSET).cast::<EventRecord>()) };
            event_records.push(format!("{{\"id\":{id},\"pending\":{},\"token_state\":{},\"notifications\":{},\"consumed\":{},\"deliveries\":{},\"boundary\":{},\"blocked\":{},\"wait_checks\":{},\"shadow\":{},\"priority\":{},\"vector\":{},\"packet\":{}}}",
                u64::from(c.event_pending[id].load(Ordering::Acquire)==1),c.event_pending[id].load(Ordering::Acquire),c.event_notifications[id].load(Ordering::Acquire),
                e.consumed,e.deliveries,e.boundary,e.blocked,e.wait_checks,e.shadow,e.priority,0x40+id,
                recovery::guest_json(&e.packet)));
        }
        if alias_profile != 0 {
            let a =
                unsafe { ptr::read(memory.0.add(base + alias::OFFSET).cast::<alias::Record>()) };
            alias_records.push(format!("{{\"id\":{id},\"current_el\":{},\"sctlr\":{},\"actlr\":{},\"tcr\":{},\"ttbr1\":{},\"warm\":{:?},\"ack_count\":{},\"acks\":{:?},\"wrong_rip_rejections\":{},\"blank_before_publish\":{},\"source_address\":{},\"source_gpa\":{},\"source_leaf_before\":{},\"source_leaf_after\":{},\"source_before\":{:?},\"source_after\":{:?},\"upper_root\":{:?},\"upper_l1\":{:?},\"upper_l2\":{:?},\"upper_leaf\":{:?},\"lower_leaf\":{:?}}}",
                s.current_el,s.sctlr,s.actlr,s.tcr,a.ttbr1,a.warm,a.ack_count,a.acks,
                a.wrong_rip_rejections,a.blank_before_publish,a.source_address,a.source_gpa,a.source_leaf_before,a.source_leaf_after,a.source_before,a.source_after,
                i4v::words(&memory,base+shadow::UPPER_ROOT),i4v::words(&memory,base+shadow::UPPER_L1),
                i4v::words(&memory,base+shadow::UPPER_L2),
                boundary_owner.as_ref().map_or_else(|| i4v::words(&memory,base+shadow::UPPER_LEAF), |o| o.upper_before),
                boundary_owner.as_ref().map_or_else(|| i4v::words(&memory,base+shadow::LOWER_LEAF), |o| o.lower_before)));
        }
        if stack_profile {
            let state = unsafe {
                ptr::read(
                    memory
                        .0
                        .add(base + stack_concurrent::OFFSET)
                        .cast::<stack_concurrent::State>(),
                )
            };
            let mut before = natemu_abi::recovery::GuestState::seed(frame);
            before.rip = source_rip;
            before.gpr[4] = frame.x[8];
            if id == 0 {
                before.rflags &= !0x200;
            }
            let after = boundary_owner.as_ref().map_or_else(
                || unsafe {
                    ptr::read(
                        memory
                            .0
                            .add(base + natemu_abi::recovery::GUEST_OFFSET)
                            .cast::<natemu_abi::recovery::GuestState>(),
                    )
                },
                |o| o.before_guest,
            );
            let programs = state
                .programs
                .iter()
                .map(|p| {
                    format!(
                        "{{\"entries\":{},\"completed\":{},\"prefixes\":{:?}}}",
                        p.entries, p.completed, p.prefixes
                    )
                })
                .collect::<Vec<_>>()
                .join(",");
            let p = &state.probe;
            let probe = format!(
                "{{\"initial\":{},\"captured\":{},\"guest_before\":{},\"guest_after\":{},\"pair\":{:?},\"tables_before\":{:?},\"tables_after\":{:?},\"vector\":{},\"error\":{},\"address\":{},\"rip\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{},\"done\":{}}}",
                frame_json(&p.initial),
                frame_json(&p.captured),
                recovery::guest_json(&p.guest_before),
                recovery::guest_json(&p.guest_after),
                p.pair,
                p.tables_before,
                p.tables_after,
                p.vector,
                p.error,
                p.address,
                p.rip,
                p.esr,
                p.far,
                p.pc,
                p.spsr,
                p.done
            );
            stack_records.push(format!("{{\"id\":{id},\"step\":{},\"pass\":{},\"code\":{},\"expected\":{:?},\"compile_requests\":{:?},\"replies\":{:?},\"programs\":[{programs}],\"probe\":{probe},\"stale_chunks\":{},\"wrong_owner_rejections\":{},\"blank_before_publish\":{},\"source_before\":{:?},\"source_after\":{:?},\"guest_before\":{},\"guest_after\":{}}}",
                state.step,state.pass,state.code,state.expected,state.compile_requests,state.replies,state.stale_chunks,state.wrong_owner_rejections,state.blank_before_publish,
                state.source_before,state.source_after,recovery::guest_json(&before),recovery::guest_json(&after)));
        }
        if args[3] != "entry" {
            faults.push(format!("{{\"id\":{id},\"faults\":{},\"old_gpa\":{},\"frame\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{}}}",
                r.faults,r.old_gpa,frame_json(&r.fault_frame),r.fault_esr,r.fault_far,r.fault_pc,r.fault_spsr));
        }
        let baseline = if pressure {
            Some(unsafe {
                ptr::read(
                    memory
                        .0
                        .add(base + natemu_abi::maintenance::BASELINE_OFFSET)
                        .cast::<natemu_abi::maintenance::Baseline>(),
                )
            })
        } else {
            None
        };
        let second_frame = baseline.as_ref().map_or(&s.captured, |b| &b.frame);
        let mut second_memory = baseline.as_ref().map_or_else(
            || memory.read::<256>(DATA_OFFSET + 4096 + id * 256),
            |b| b.new_memory,
        );
        let mut first_memory = baseline.as_ref().map_or_else(
            || memory.read::<256>(DATA_OFFSET + id * 256),
            |b| b.old_memory,
        );
        let mut second_before = new_data[id];
        if mixed_profile != 0 {
            let observed = mixed::state(&memory, base);
            first_memory = observed.old_memory_after;
            second_before = observed.new_memory_before;
            second_memory = observed.new_memory_after;
        }
        if scenario_id >= 2 {
            if !pressure && s.initial != r.new_initial {
                return Err("new entry state changed".into());
            }
            let code = if compose { CODE_NEXT } else { CODE_VA };
            let case = if mixed_profile != 0 {
                "fex_mixed32_changed"
            } else if stack_profile {
                "fex_push_immediates64_changed"
            } else if compose {
                "live_or"
            } else {
                "live_load"
            };
            let dirty = format!(
                "{{\"frame\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{}}}",
                frame_json(&r.dirty_frame),
                r.dirty_esr,
                r.dirty_far,
                r.dirty_pc,
                r.dirty_spsr
            );
            new_cells.push(format!("{{\"dirty_fault\":{dirty},\"id\":{id},\"dirty_faults\":{},\"case\":\"{case}\",\"status\":{},\"initial\":{},\"captured\":{},\"memory_base\":{},\"memory_before\":{:?},\"memory_after\":{:?},\"bank_before\":{bank:?},\"bank_after\":{:?},\"object_pc\":{code},\"capture_pc\":{}}}",
                r.dirty_faults,s.status,frame_json(&r.new_initial),frame_json(second_frame),if stack_profile {stack_concurrent::initial_rsp_for(stack_kind,id as u64)-0x80} else {r.new_initial.x[6]},
                second_before,second_memory,memory.read::<512>(base+BANK_OFFSET),s.capture_pc));
        }
        let old_frame = if scenario_id >= 2 {
            &r.first_frame
        } else {
            &s.captured
        };
        let case = if mixed_profile != 0 {
            "fex_mixed32_old"
        } else if stack_profile {
            "fex_push_immediates64"
        } else {
            "live_load"
        };
        cells.push(format!("{{\"id\":{id},\"case\":\"{case}\",\"status\":{},\"initial\":{},\"captured\":{},\"memory_base\":{},\"memory_before\":{data:?},\"memory_after\":{:?},\"bank_before\":{bank:?},\"bank_after\":{:?},\"object_pc\":{CODE_VA},\"capture_pc\":{}}}",
            s.status,frame_json(frame),frame_json(old_frame),frame.x[6],
            first_memory,memory.read::<512>(base+BANK_OFFSET),s.capture_pc));
    }
    let trace: Vec<_> = c
        .trace
        .iter()
        .take(c.trace_length.load(Ordering::Acquire) as usize)
        .map(|v| v.load(Ordering::Relaxed))
        .collect();
    let scenario = boundary_config.as_ref().map_or(args[3].as_str(), |config| {
        if config.alias_profile == 1 {
            "stack-low"
        } else {
            "stack-high"
        }
    });
    let source_summary = format!(
        "{{\"old_eligible\":{},\"published\":{},\"stale_replies\":{},\"stale_entries\":{},\"reuse_blocked\":{},\"bytes\":{:?},\"old_code\":{:?},\"new_code\":{:?}}}",
        c.old_eligible.load(Ordering::Acquire),
        c.published.load(Ordering::Acquire),
        c.stale_replies.load(Ordering::Acquire),
        c.stale_entries.load(Ordering::Acquire),
        c.reuse_blocked.load(Ordering::Acquire),
        if mixed_profile != 0 {
            memory
                .read::<6>(natemu_abi::recovery::SOURCE_OFFSET)
                .to_vec()
        } else if stack_profile {
            memory
                .read::<20>(natemu_abi::recovery::SOURCE_OFFSET)
                .to_vec()
        } else {
            memory
                .read::<17>(natemu_abi::recovery::SOURCE_OFFSET)
                .to_vec()
        },
        if mixed_profile != 0 {
            memory.read::<24>(CODE_OFFSET).to_vec()
        } else if stack_profile {
            memory.read::<{ stack::CODE_BYTES }>(CODE_OFFSET).to_vec()
        } else {
            memory.read::<48>(CODE_OFFSET).to_vec()
        },
        if mixed_profile != 0 {
            memory.read::<24>(CODE_OFFSET + 4096).to_vec()
        } else if let Some(b) = boundary_shared {
            b.code_before.to_vec()
        } else if stack_profile {
            memory
                .read::<{ stack::CODE_BYTES }>(CODE_OFFSET + 4096)
                .to_vec()
        } else {
            memory.read::<48>(CODE_OFFSET + 4096).to_vec()
        }
    );
    let maintenance = if pressure {
        Some(unsafe {
            ptr::read(
                memory
                    .0
                    .add(natemu_abi::maintenance::OFFSET)
                    .cast::<natemu_abi::maintenance::State>(),
            )
        })
    } else {
        None
    };
    let final_leaf = boundary_shared.map_or_else(
        || {
            u64::from_le_bytes(
                memory.read::<8>(natemu_abi::recovery::TABLE_OFFSET + 0x3000 + 256 * 8),
            )
        },
        |b| u64::from_le_bytes(b.tables_before[0x3800..0x3808].try_into().unwrap()),
    );
    let compose_leaf = maintenance.as_ref().map_or(final_leaf, |m| m.compose_leaf);
    let maintenance_json = if let Some(m) = maintenance {
        let mut intervals = Vec::new();
        for id in 0..n {
            if m.intervals[id] > natemu_abi::maintenance::MAX_INTERVALS as u64 {
                return Err("interval reserve exceeded".into());
            }
            let state =
                unsafe { ptr::read(memory.0.add(id * RAM_SIZE + SHARED_OFFSET).cast::<Shared>()) };
            for index in 0..m.intervals[id] as usize {
                let r = unsafe {
                    ptr::read(
                        memory
                            .0
                            .add(
                                id * RAM_SIZE
                                    + natemu_abi::maintenance::RECORD_OFFSET
                                    + index * 4096,
                            )
                            .cast::<natemu_abi::maintenance::Interval>(),
                    )
                };
                let case = if mixed_profile != 0 {
                    "fex_mixed32_changed"
                } else if stack_profile {
                    "fex_push_immediates64_changed"
                } else {
                    "live_or"
                };
                intervals.push(format!("{{\"id\":{id},\"ordinal\":{},\"heads\":{},\"epoch\":{},\"gpa\":{},\"case\":\"{case}\",\"status\":1,\"initial\":{},\"captured\":{},\"memory_base\":{},\"memory_before\":{:?},\"memory_after\":{:?},\"bank_before\":{:?},\"bank_after\":{:?},\"object_pc\":{CODE_NEXT},\"capture_pc\":{},\"dirty_faults\":{},\"dirty_fault\":{{\"frame\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{}}}}}",
                    r.ordinal,r.heads,r.epoch,r.gpa,frame_json(&r.initial),frame_json(&r.captured),if stack_profile {stack_concurrent::initial_rsp_for(stack_kind,id as u64)-0x80} else {r.initial.x[6]},
                    r.before,r.after,seeds[id].1,memory.read::<512>(id*RAM_SIZE+BANK_OFFSET),state.capture_pc,
                    r.faults,frame_json(&r.dirty_frame),r.esr,r.far,r.pc,r.spsr));
            }
        }
        let request_history: Vec<[u64; 2]> = m.request_history[..(m.total as usize).min(36)]
            .iter()
            .map(|r| [r.cpu, r.serial])
            .collect();
        format!(
            "{{\"serial\":{},\"request_history\":{request_history:?},\"ready\":{},\"submitted\":{:?},\"completed\":{:?},\"pending\":{:?},\"length\":{},\"current\":[{},{}],\"epoch\":{},\"total\":{},\"batch\":{},\"next_ordinary\":{},\"ordinary\":{},\"ordinary_total\":{},\"interval_counts\":{:?},\"finished\":{},\"duplicate_rejections\":{},\"maximum_depth\":{},\"leaf_history\":{:?},\"leaf_final\":{final_leaf},\"intervals\":[{}]}}",
            m.serial,
            m.ready,
            m.submitted,
            m.completed,
            m.pending,
            m.length,
            m.current.cpu,
            m.current.serial,
            m.epoch,
            m.total,
            m.batch,
            m.next_ordinary,
            m.ordinary,
            m.ordinary_total,
            m.intervals,
            m.finished,
            m.duplicate_rejections,
            m.maximum_depth,
            &m.leaf_history[..(m.total as usize).min(36)],
            intervals.join(",")
        )
    } else {
        "null".into()
    };
    let summary = format!(
        "{{\"generation\":{},\"acknowledgements\":{},\"active\":{},\"new_entries\":{},\"new_completed\":{},\"new_references\":{},\"reclaimed\":{},\"leaf\":{}}}",
        c.generation.load(Ordering::Acquire),
        c.acknowledgements.load(Ordering::Acquire),
        c.active.load(Ordering::Acquire),
        c.new_entries.load(Ordering::Acquire),
        c.new_completed.load(Ordering::Acquire),
        c.new_references.load(Ordering::Acquire),
        c.reclaimed.load(Ordering::Acquire),
        compose_leaf
    );
    let mixed_pages_after = if mixed_profile != 0 {
        memory.read::<8192>(DATA_OFFSET).to_vec()
    } else {
        Vec::new()
    };
    let mixed_tables_after = if mixed_profile != 0 {
        memory
            .read::<16384>(natemu_abi::recovery::TABLE_OFFSET)
            .to_vec()
    } else {
        Vec::new()
    };
    let mixed_compiled = c.mixed_compiled.load(Ordering::Acquire);
    let mixed_json = mixed_records.join(",");
    let stack_flag = stack_kind;
    let stack_memory_before = if stack_profile {
        vec![0xa5u8; 8192]
    } else {
        Vec::new()
    };
    let stack_memory_after = if stack_profile {
        boundary_shared.map_or_else(
            || memory.read::<8192>(DATA_OFFSET).to_vec(),
            |b| b.data_before.to_vec(),
        )
    } else {
        Vec::new()
    };
    let prefix = format!(
        "{{\"mixed_profile\":{mixed_profile},\"mixed_compiled\":{mixed_compiled},\"mixed\":[{mixed_json}],\"mixed_pages_before\":{mixed_pages_before:?},\"mixed_pages_after\":{mixed_pages_after:?},\"mixed_tables_before\":{mixed_tables_before:?},\"mixed_tables_after\":{mixed_tables_after:?},\"stack_profile\":{stack_flag},\"stacks\":[{}],\"stack_memory_before\":{stack_memory_before:?},\"stack_memory_after\":{stack_memory_after:?},\"alias_profile\":{alias_profile},\"source_rip\":{source_rip},\"aliases\":[{}],\"maintenance\":{maintenance_json},\"events\":[{}],\"source\":{source_summary},\"requests\":{requests:?},\"coherence\":{summary},\"new_cells\":[{}],\"profile\":\"i4-{scenario}\",\"trace\":{trace:?},\"faults\":[{}],\"retained\":{},\"configured_vcpus\":{n},\"tested_vcpus\":{n},\"delayed_owner_ms\":{delay},\"ready\":{},\"finished\":{},\"cells\":[{}]}}",
        stack_records.join(","),
        alias_records.join(","),
        event_records.join(","),
        new_cells.join(","),
        faults.join(","),
        c.retained.load(Ordering::Acquire),
        c.ready.load(Ordering::Acquire),
        boundary_shared.map_or_else(|| c.finished.load(Ordering::Acquire), |b| b.finished_before),
        cells.join(",")
    );
    if let Some(config) = &boundary_config {
        println!("{}", boundary::report(&memory, config, n, &prefix));
    } else {
        println!("{prefix}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_owner_maps_its_extended_resident_text_read_only() {
        let memory = Memory::with_size(2 * RAM_SIZE).unwrap();
        for id in 0..2 {
            let base = id * RAM_SIZE;
            tables(&memory, base, true);
            for offset in (0..RAM_SIZE).step_by(4096) {
                let pte = u64::from_le_bytes(memory.read::<8>(base + 0x14000 + offset / 4096 * 8));
                if resident::executable_offset(offset) {
                    assert_eq!(
                        pte,
                        (IPA + (base + offset) as u64) | 3 | 1 << 10 | 3 << 8 | 1 << 7
                    );
                } else if (resident::BOOT_END..0x10000).contains(&offset) {
                    assert_eq!(pte, 0);
                } else if pte != 0 && !(CODE_OFFSET..CODE_OFFSET + 8192).contains(&offset) {
                    assert_eq!(pte & (3 << 53), 3 << 53);
                }
            }
        }
    }

    #[test]
    fn mixed_permissions_are_admitted_only_as_full_pressure_profiles() {
        for (name, stack_kind, alias_kind) in [
            ("stack-parent-low", 2, 1),
            ("stack-parent-high", 2, 2),
            ("stack-leaf-low", 3, 1),
            ("stack-leaf-high", 3, 2),
        ] {
            assert_eq!(profiles(name).unwrap(), (5, alias_kind, stack_kind));
            for id in 0..4 {
                assert_eq!(
                    stack_concurrent::initial_rsp_for(stack_kind, id),
                    DATA_VA + id * 256 + 0x80,
                );
            }
        }
        for unsupported in [
            "stack-parent",
            "stack-leaf",
            "stack-parent-low-extra",
            "stack-leaf-middle",
        ] {
            assert!(profiles(unsupported).is_err());
        }
        assert_eq!(profiles("stack-low").unwrap(), (5, 1, 1));
        assert_eq!(profiles("stack-high").unwrap(), (5, 2, 1));
        assert_eq!(profiles("pressure").unwrap(), (5, 0, 0));
    }
}
