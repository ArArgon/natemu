//! First I4-V harness: eager controls and resident-requested high/low guest loads.
use super::*;
use natemu_abi::{recovery as recover, shadow, validation};
use std::{
    fs::OpenOptions,
    io::Write,
    path::Path,
    process::{Command, Stdio},
};

const BODY: [u8; 8] = [0xc4, 0xfc, 0xdf, 0xc8, 0x1f, 0x20, 0x03, 0xd5];

pub(super) fn words(memory: &Memory, offset: usize) -> [u64; 512] {
    let bytes = memory.read::<4096>(offset);
    core::array::from_fn(|i| u64::from_le_bytes(bytes[i * 8..i * 8 + 8].try_into().unwrap()))
}

fn word(memory: &Memory, offset: usize) -> u64 {
    u64::from_le_bytes(memory.read::<8>(offset))
}

pub(super) fn upper_links(memory: &Memory) -> [u64; 3] {
    [
        word(memory, shadow::UPPER_ROOT + 256 * 8),
        word(memory, shadow::UPPER_L1),
        word(memory, shadow::UPPER_L2),
    ]
}

pub(super) fn prepare_shadow(memory: &Memory) {
    page_tables(memory);
    let descriptor = |offset: usize| IPA + offset as u64 + 3;
    memory.word(shadow::UPPER_ROOT + 256 * 8, descriptor(shadow::UPPER_L1));
    memory.word(shadow::UPPER_L1, descriptor(shadow::UPPER_L2));
    memory.word(shadow::UPPER_L2, descriptor(shadow::UPPER_LEAF));
    // The runtime can update its own page tables through private, non-executable aliases.
    for offset in (shadow::UPPER_ROOT..shadow::UPPER_LEAF + 4096).step_by(4096) {
        memory.word(
            0x14000 + offset / 4096 * 8,
            (IPA + offset as u64) | 3 | 1 << 10 | 3 << 8 | 3 << 53,
        );
    }
    memory.word(shadow::LOWER_LEAF + 256 * 8, 0);
}

pub fn run(args: &[String]) -> Result<()> {
    let live = args[2] == "i4v-live";
    let reply_mutation: i64 = if args.len() == 10 {
        let index: usize = args[9]
            .parse()
            .map_err(|_| "invalid reply mutation index")?;
        if index >= 16 {
            return Err("reply mutation index must be below sixteen".into());
        }
        index as i64
    } else {
        -1
    };
    if args[6] != "1" {
        return Err("this I4-V profile admits one vCPU".into());
    }
    let scenario = args[4].as_str();
    let base_scenario = match scenario {
        "cold" | "load_np" | "user_denied" | "fetch_nx" | "smep" => scenario,
        "low_operand"
        | "noncanonical"
        | "noncanonical_operand"
        | "private_operand"
        | "noncanonical_rip" => "cold",
        _ => return Err("unknown I4-V load scenario".into()),
    };
    let rip = if let Some(hex) = args[5].strip_prefix("0x") {
        u64::from_str_radix(hex, 16)
    } else {
        args[5].parse()
    }
    .map_err(|_| "invalid guest RIP")?;
    let mut native = vec![0; BODY.len() + RET.len()];
    if !live {
        let body = read_input(&args[3], BODY.len())?;
        if body != BODY {
            return Err("I4-V load body differs from the admitted FEX output".into());
        }
        native[..BODY.len()].copy_from_slice(&body);
        native[BODY.len()..].copy_from_slice(&RET);
    }
    let mut vm_nonce = 0u64;
    if live {
        if unsafe { getentropy((&mut vm_nonce as *mut u64).cast(), 8) } != 0 || vm_nonce == 0 {
            return Err("unable to generate live VM identity".into());
        }
    }
    let memory = Memory::new()?;
    load_elf(&memory, &read_input(&args[1], 2 * 1024 * 1024)?)?;
    prepare_shadow(&memory);
    memory.copy(CODE_OFFSET, &native);
    memory.copy(recover::SOURCE_OFFSET, &[0x48, 0x8b, 0x03]);

    let (mut frame, bank, mut data) = seed(3);
    data[..8].copy_from_slice(&[0xaa, 0xbb, 0xcc, 0xdd, 0, 0, 0, 0]);
    frame.x[6] = match scenario {
        "low_operand" => DATA_VA,
        "noncanonical" | "noncanonical_operand" => 0x0000_8000_0010_0000,
        "private_operand" => PRIVATE,
        _ => shadow::HIGH_BASE + DATA_VA,
    };
    frame.x[8] = shadow::HIGH_BASE + DATA_VA + 0x1800;
    memory.copy(BANK_OFFSET, &bank);
    memory.copy(DATA_OFFSET, &data);
    let mut guest = recovery::prepare(&memory, &frame, base_scenario, false)?;
    guest.rip = rip;
    guest.gpr[4] = frame.x[8];
    // Both guest canonical halves intentionally alias the same guest page tables.
    memory.word(recover::TABLE_OFFSET + 256 * 8, 0x2007);
    let state = validation::State {
        guest_rip: rip,
        case: 0,
        completed: 0,
        operand: frame.x[6],
        width: 8,
        stage: 0,
        live: u64::from(live),
        requests: 0,
        request: [0; 16],
        reply: [0; 16],
    };
    let mut object = Object::new(LIVE_LOAD);
    object.code_length = native.len() as u64;
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
        mode: validation::MODE,
        lifecycle: natemu_abi::translation::Lifecycle::new(vm_nonce),
    };
    let tables_before = memory.read::<16384>(recover::TABLE_OFFSET);
    let upper_root_before = words(&memory, shadow::UPPER_ROOT);
    let upper_leaf_before = words(&memory, shadow::UPPER_LEAF);
    let upper_links_before = upper_links(&memory);
    let lower_data_leaf_before = word(&memory, shadow::LOWER_LEAF + 256 * 8);
    let code_before = memory.read::<12>(CODE_OFFSET);
    unsafe {
        ptr::write(
            memory
                .0
                .add(recover::GUEST_OFFSET)
                .cast::<recover::GuestState>(),
            guest,
        );
        ptr::write(
            memory
                .0
                .add(validation::STATE_OFFSET)
                .cast::<validation::State>(),
            state,
        );
        ptr::write(memory.0.add(SHARED_OFFSET).cast::<Shared>(), shared);
        sys_icache_invalidate(memory.0.cast(), RAM_SIZE);
    }
    let vm = Vm::new(memory)?;
    vm.reg(31, IPA)?;
    vm.reg(34, 0x3c5)?;
    vm.reg(0, IPA + shadow::LOWER_ROOT as u64)?;
    vm.reg(1, shadow::TCR_BOTH)?;
    vm.reg(2, 0xff)?;
    vm.sys(0xc080, 0x30d00980)?;
    vm.sys(0xc081, 2)?;
    vm.sys(0xc082, 3 << 20)?;
    vm.sys(0xdf19, 0)?;
    let event = vm.run_any()?;
    if live && event == 0x4a {
        let resident = vm.shared();
        if !natemu_abi::resident::contains(resident.service_pc, 4)
            || vm.read_reg(31)? != resident.service_pc
        {
            return Err("I4-V request did not exit at the resident service PC".into());
        }
        let mut request = [0; 16];
        for (index, value) in request.iter_mut().enumerate() {
            *value = vm.read_reg(index as u32)?;
        }
        if request != validation::request(vm_nonce, rip) {
            return Err("I4-V resident request differs from the expected identity/source".into());
        }
        let mut reply = compile_request(&args[7], &args[3], Path::new(&args[8]), request)?;
        if Some(reply) != validation::reply(&request) {
            return Err("I4-V compiler reply differs from its original request/body".into());
        }
        if reply_mutation >= 0 {
            reply[reply_mutation as usize] ^= 1;
        }
        // At the explicit service boundary only registers transfer ownership.
        for (index, value) in reply.into_iter().enumerate() {
            vm.reg(index as u32, value)?;
        }
        vm.run(0x49)?;
    } else if event != 0x49 {
        return Err(format!("unexpected I4-V HVC immediate {event:#x}"));
    }
    let result = vm.shared();
    let final_state = unsafe {
        ptr::read_volatile(
            vm.memory
                .0
                .add(validation::STATE_OFFSET)
                .cast::<validation::State>(),
        )
    };
    let ttbr1 = vm.read_sys(0xc101)?;
    let live = u64::from(live);
    let requests = final_state.requests;
    let request = final_state.request;
    let reply = final_state.reply;
    let service_pc = result.service_pc;
    let code_after = vm.memory.read::<12>(CODE_OFFSET);
    println!(
        "{{\"scenario\":\"{scenario}\",\"rip\":{rip},\"live\":{live},\"requests\":{requests},\"request\":{request:?},\"reply\":{reply:?},\"vm_nonce\":{vm_nonce},\"service_pc\":{service_pc},\"reply_mutation\":{reply_mutation},\"code_before\":{code_before:?},\"code_after\":{code_after:?},\"body_hex\":\"c4fcdfc81f2003d5\",\"initial\":{},\"entered\":{},\"final_frame\":{},\"status\":{},\"detail\":{},\"current_el\":{},\"sctlr\":{},\"actlr\":{},\"tcr\":{},\"ttbr1\":{ttbr1},\"completed\":{},\"operand\":{},\"width\":{},\"stage\":{},\"memory_before\":{data:?},\"bank_before\":{bank:?},\"recovery\":{},\"upper_root_before\":{upper_root_before:?},\"upper_root_after\":{:?},\"upper_leaf_before\":{upper_leaf_before:?},\"upper_leaf_after\":{:?},\"upper_links_before\":{upper_links_before:?},\"upper_links_after\":{:?},\"lower_data_leaf_before\":{lower_data_leaf_before},\"lower_data_leaf_after\":{}}}",
        frame_json(&frame),
        frame_json(&result.initial),
        frame_json(&result.captured),
        result.status,
        result.detail,
        result.current_el,
        result.sctlr,
        result.actlr,
        result.tcr,
        final_state.completed,
        final_state.operand,
        final_state.width,
        final_state.stage,
        recovery::report(&vm, &guest, &tables_before),
        words(&vm.memory, shadow::UPPER_ROOT),
        words(&vm.memory, shadow::UPPER_LEAF),
        upper_links(&vm.memory),
        word(&vm.memory, shadow::LOWER_LEAF + 256 * 8),
    );
    if result.current_el != 4
        || result.sctlr & 0x1005 != 0x1005
        || result.actlr & 2 != 2
        || result.tcr != shadow::TCR_BOTH
        || ttbr1 != IPA + shadow::UPPER_ROOT as u64
    {
        return Err("I4-V runtime machine profile mismatch".into());
    }
    if result.status == 0 || result.status == PANIC {
        return Err(format!(
            "I4-V resident failure status={} detail={}",
            result.status, result.detail
        ));
    }
    Ok(())
}

fn compile_request(
    worker: &str,
    producer: &str,
    output: &Path,
    request: [u64; 16],
) -> Result<[u64; 16]> {
    fs::create_dir_all(output).map_err(|e| e.to_string())?;
    let directory = output.join("request-001");
    fs::create_dir(&directory).map_err(|e| e.to_string())?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join("request.bin"))
        .map_err(|e| e.to_string())?;
    for value in request {
        file.write_all(&value.to_le_bytes())
            .map_err(|e| e.to_string())?;
    }
    drop(file);
    let stdout = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join("worker.stdout"))
        .map_err(|e| e.to_string())?;
    let stderr = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join("worker.stderr"))
        .map_err(|e| e.to_string())?;
    let result = Command::new("python3")
        .arg("-B")
        .arg(worker)
        .arg("--request-dir")
        .arg(&directory)
        .arg("--producer")
        .arg(producer)
        .stdin(Stdio::null())
        .stdout(stdout)
        .stderr(stderr)
        .status()
        .map_err(|e| e.to_string())?;
    if !result.success() {
        return Err(format!("I4-V worker failed: {}", directory.display()));
    }
    let raw = read_input(
        directory
            .join("reply.bin")
            .to_str()
            .ok_or("non-UTF8 worker path")?,
        128,
    )?;
    if raw.len() != 128 {
        return Err("I4-V worker reply length differs from packet".into());
    }
    Ok(core::array::from_fn(|i| {
        u64::from_le_bytes(raw[i * 8..i * 8 + 8].try_into().unwrap())
    }))
}
