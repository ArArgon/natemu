//! Live stack-fragment validation with one compile and three bounded replies.
use super::*;
use natemu_abi::{recovery as recover, shadow, stack, validation};
use std::{
    fs::OpenOptions,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

fn write_packet(path: &Path, packet: &[u64; 16]) -> Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| e.to_string())?;
    for word in packet {
        file.write_all(&word.to_le_bytes())
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn start_worker(
    worker: &str,
    producer: &str,
    output: &Path,
    request: &[u64; 16],
) -> Result<PathBuf> {
    fs::create_dir_all(output).map_err(|e| e.to_string())?;
    let directory = output.join("request-001");
    fs::create_dir(&directory).map_err(|e| e.to_string())?;
    write_packet(&directory.join("request.bin"), request)?;
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
    let status = Command::new("python3")
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
    if !status.success() {
        return Err(format!("I4-V stack worker failed: {}", directory.display()));
    }
    Ok(directory)
}

fn read_packet(path: &Path) -> Result<[u64; 16]> {
    let raw = read_input(path.to_str().ok_or("non-UTF8 packet path")?, 128)?;
    if raw.len() != 128 {
        return Err("stack reply packet length differs".into());
    }
    Ok(core::array::from_fn(|i| {
        u64::from_le_bytes(raw[i * 8..i * 8 + 8].try_into().unwrap())
    }))
}

pub fn run(args: &[String]) -> Result<()> {
    if args[6] != "1" {
        return Err("I4-V stack profile admits one vCPU".into());
    }
    let mutation: i64 = if args.len() == 10 {
        let value: usize = args[9]
            .parse()
            .map_err(|_| "invalid stack reply mutation")?;
        if value >= 48 {
            return Err("stack reply mutation must be below48".into());
        }
        value as i64
    } else {
        -1
    };
    let scenario = args[4].as_str();
    if ![
        "cold",
        "low_stack",
        "ro_first",
        "ro_after_first",
        "np_after_first",
        "wp_off",
        "user_ro",
        "user_denied",
        "user_ok",
        "parent_ro",
        "fetch_nx",
        "smep",
        "ss_first",
        "ss_after_first",
        "ss_span",
        "gp_after_pushes",
        "read_np_after_pushes",
    ]
    .contains(&scenario)
    {
        return Err("unknown I4-V stack scenario".into());
    }
    let rip = if let Some(hex) = args[5].strip_prefix("0x") {
        u64::from_str_radix(hex, 16)
    } else {
        args[5].parse()
    }
    .map_err(|_| "invalid stack RIP")?;
    let mut vm_nonce = 0u64;
    if unsafe { getentropy((&mut vm_nonce as *mut u64).cast(), 8) } != 0 || vm_nonce == 0 {
        return Err("unable to generate stack VM identity".into());
    }
    let memory = Memory::new()?;
    load_elf(&memory, &read_input(&args[1], 2 * 1024 * 1024)?)?;
    i4v::prepare_shadow(&memory);
    memory.word(shadow::LOWER_LEAF + 257 * 8, 0);
    memory.copy(recover::SOURCE_OFFSET, &stack::SOURCE);
    let (mut frame, bank, _) = seed(3);
    frame.x[8] = match scenario {
        "low_stack" => DATA_VA + 0x1008,
        "ss_first" => PRIVATE + 8,
        "ss_after_first" => shadow::HIGH_BASE + 8,
        "ss_span" => shadow::HIGH_BASE + 4,
        _ => shadow::HIGH_BASE + DATA_VA + 0x1008,
    };
    frame.x[5] = match scenario {
        "gp_after_pushes" => PRIVATE + 8,
        "read_np_after_pushes" => shadow::HIGH_BASE + DATA_VA + 0x3008,
        _ => frame.x[8],
    };
    let data = [0xa5u8; 8192];
    memory.copy(DATA_OFFSET, &data);
    memory.copy(BANK_OFFSET, &bank);
    let mut guest = recovery::prepare(&memory, &frame, "cold", false)?;
    guest.rip = rip;
    guest.gpr[4] = frame.x[8];
    memory.word(recover::TABLE_OFFSET + 256 * 8, 0x2007);
    let mut recovery_state = unsafe {
        ptr::read(
            memory
                .0
                .add(recover::STATE_OFFSET)
                .cast::<recover::Recovery>(),
        )
    };
    let leaf = |index: usize| recover::TABLE_OFFSET + 0x3000 + index * 8;
    match scenario {
        "ro_first" => memory.word(leaf(257), (DATA_VA + 4096) | 5),
        "ro_after_first" => memory.word(leaf(256), DATA_VA | 5),
        "np_after_first" => memory.word(leaf(256), 0),
        "wp_off" | "user_ro" => {
            memory.word(leaf(256), DATA_VA | 5);
            memory.word(leaf(257), (DATA_VA + 4096) | 5);
            recovery_state.control.wp = 0;
        }
        "user_denied" => memory.word(leaf(257), (DATA_VA + 4096) | 3),
        "parent_ro" => memory.word(recover::TABLE_OFFSET + 0x2000, 0x4005),
        "fetch_nx" => memory.word(leaf(258), (DATA_VA + 8192) | 7 | (1 << 63)),
        "smep" => recovery_state.control.smep = 1,
        "ss_after_first" => memory.word(leaf(0), DATA_VA | 7),
        _ => {}
    }
    if matches!(scenario, "user_ro" | "user_denied" | "user_ok") {
        recovery_state.control.cpl = 3;
        guest.segments[0] = [0x1b, 0, 0xffff_ffff, 0xa0fb];
        for segment in &mut guest.segments[1..6] {
            *segment = [0x23, 0, 0xffff_ffff, 0xc0f3];
        }
    }
    guest.control[0] = (guest.control[0] & !(1 << 16)) | (recovery_state.control.wp << 16);
    guest.control[3] = (guest.control[3] & !(1 << 20)) | (recovery_state.control.smep << 20);
    let state = validation::State {
        guest_rip: rip,
        case: stack::CASE,
        completed: 0,
        operand: 0,
        width: 0,
        stage: 0,
        live: 1,
        requests: 0,
        request: [0; 16],
        reply: [0; 16],
    };
    let mut object = Object::new(LIVE_LOAD);
    object.code_length = stack::CODE_BYTES as u64;
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
    let upper_root_before = i4v::words(&memory, shadow::UPPER_ROOT);
    let upper_leaf_before = i4v::words(&memory, shadow::UPPER_LEAF);
    let lower_leaf_before = i4v::words(&memory, shadow::LOWER_LEAF);
    let upper_links_before = i4v::upper_links(&memory);
    let code_before = memory.read::<{ stack::CODE_BYTES }>(CODE_OFFSET);
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
                .add(recover::STATE_OFFSET)
                .cast::<recover::Recovery>(),
            recovery_state,
        );
        ptr::write(
            memory
                .0
                .add(validation::STATE_OFFSET)
                .cast::<validation::State>(),
            state,
        );
        ptr::write(
            memory.0.add(stack::STATE_OFFSET).cast::<stack::State>(),
            stack::State::ZERO,
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
    let mut event = vm.run_any()?;
    let mut directory = None;
    for chunk in 0..3 {
        if event == 0x49 && chunk == 0 {
            break;
        }
        if event != 0x4a {
            return Err(format!("unexpected stack transfer event {event:#x}"));
        }
        let resident = vm.shared();
        if !natemu_abi::resident::contains(resident.service_pc, 4)
            || vm.read_reg(31)? != resident.service_pc
        {
            return Err("stack request outside service boundary".into());
        }
        let mut request = [0; 16];
        for (i, word) in request.iter_mut().enumerate() {
            *word = vm.read_reg(i as u32)?;
        }
        if request != stack::request(vm_nonce, rip, chunk) {
            return Err("stack request changes compile identity/chunk order".into());
        }
        if chunk == 0 {
            directory = Some(start_worker(
                &args[7],
                &args[3],
                Path::new(&args[8]),
                &request,
            )?);
        }
        let directory = directory.as_ref().ok_or("stack worker was not started")?;
        write_packet(&directory.join(format!("request-{chunk}.bin")), &request)?;
        let mut reply = read_packet(&directory.join(format!("reply-{chunk}.bin")))?;
        if Some(reply) != stack::reply(&request) {
            return Err("stack reply differs from request/generated chunk".into());
        }
        if mutation >= 0 && mutation as u64 / 16 == chunk {
            reply[mutation as usize % 16] ^= 1;
        }
        write_packet(
            &directory.join(format!("reply-delivered-{chunk}.bin")),
            &reply,
        )?;
        for (i, word) in reply.into_iter().enumerate() {
            vm.reg(i as u32, word)?;
        }
        event = vm.run_any()?;
        if mutation >= 0 && mutation as u64 / 16 == chunk {
            if event != 0x49 {
                return Err("mutated stack reply did not terminate".into());
            }
            break;
        }
    }
    if event != 0x49 {
        return Err("stack execution did not reach terminal HVC".into());
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
    let stack_state =
        unsafe { ptr::read_volatile(vm.memory.0.add(stack::STATE_OFFSET).cast::<stack::State>()) };
    let prefixes = stack_state
        .prefixes
        .iter()
        .map(frame_json)
        .collect::<Vec<_>>()
        .join(",");
    let ttbr1 = vm.read_sys(0xc101)?;
    let code_after = vm.memory.read::<{ stack::CODE_BYTES }>(CODE_OFFSET);
    let memory_after = vm.memory.read::<8192>(DATA_OFFSET);
    let body_hex = stack::BODY
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    println!(
        "{{\"case\":\"fex_push_immediates64\",\"scenario\":\"{scenario}\",\"rip\":{rip},\"live\":1,\"requests\":{},\"request\":{:?},\"reply\":{:?},\"vm_nonce\":{vm_nonce},\"service_pc\":{},\"reply_mutation\":{mutation},\"code_before\":{code_before:?},\"code_after\":{code_after:?},\"body_hex\":\"{body_hex}\",\"initial\":{},\"entered\":{},\"final_frame\":{},\"status\":{},\"detail\":{},\"current_el\":{},\"sctlr\":{},\"actlr\":{},\"tcr\":{},\"ttbr1\":{ttbr1},\"completed\":{},\"operand\":{},\"width\":{},\"stage\":{},\"memory_before\":{data:?},\"memory_after\":{memory_after:?},\"bank_before\":{bank:?},\"recovery\":{},\"upper_root_before\":{upper_root_before:?},\"upper_root_after\":{:?},\"upper_leaf_before\":{upper_leaf_before:?},\"upper_leaf_after\":{:?},\"lower_leaf_before\":{lower_leaf_before:?},\"lower_leaf_after\":{:?},\"upper_links_before\":{upper_links_before:?},\"upper_links_after\":{:?},\"stack\":{{\"transfers\":{},\"entries\":{:?},\"completions\":{:?},\"requests\":{:?},\"replies\":{:?},\"prefixes\":[{prefixes}]}}}}",
        final_state.requests,
        final_state.request,
        final_state.reply,
        result.service_pc,
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
        i4v::words(&vm.memory, shadow::UPPER_ROOT),
        i4v::words(&vm.memory, shadow::UPPER_LEAF),
        i4v::words(&vm.memory, shadow::LOWER_LEAF),
        i4v::upper_links(&vm.memory),
        stack_state.transfers,
        stack_state.entries,
        stack_state.completions,
        stack_state.requests,
        stack_state.replies
    );
    if result.current_el != 4
        || result.sctlr & 0x1005 != 0x1005
        || result.actlr & 2 != 2
        || result.tcr != shadow::TCR_BOTH
        || ttbr1 != IPA + shadow::UPPER_ROOT as u64
    {
        return Err("stack runtime machine profile mismatch".into());
    }
    if result.status == 0 || result.status == PANIC {
        return Err(format!(
            "stack resident failure status={} detail={}",
            result.status, result.detail
        ));
    }
    Ok(())
}
