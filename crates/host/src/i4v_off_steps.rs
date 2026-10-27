//! Live protected32 paging-off execution of the separately compiled FEX instructions.
use super::i4v_stack::{read_packet, start_worker_numbered, write_packet};
use super::*;
use natemu_abi::{paging_off_steps as off, recovery as recover, shadow, validation};
use std::path::Path;

pub fn run(args: &[String]) -> Result<()> {
    if args[5] != "1" {
        return Err("I4-V paging-off profile admits one vCPU".into());
    }
    let mutation: i64 = if args.len() == 9 {
        let value: usize = args[8]
            .parse()
            .map_err(|_| "invalid paging-off reply mutation")?;
        if value >= 25 {
            return Err("paging-off reply mutation must be below25".into());
        }
        value as i64
    } else {
        -1
    };
    let scenario = args[4].as_str();
    let scenario_id = [
        "segments",
        "a20-off",
        "ds-limit",
        "cs-before-load",
        "cs-inside-load",
        "cs-first",
        "ds-readonly",
        "device",
        "hole",
        "linear-wrap",
    ]
    .iter()
    .position(|name| *name == scenario)
    .ok_or("unknown instruction scenario")? as u64;
    let rip = 0x2000;
    let vm_nonce = new_vm_nonce()?;
    let memory = Memory::new()?;
    load_resident(&memory, &args[1])?;
    single_owner_space(&memory, true);
    let mut source_a = [0xcc; 4096];
    let mut source_b = [0x33; 4096];
    // Only the A20-selected physical page contains the admitted program.
    // Reading through the wrong physical route must fail source validation.
    let active_source = if scenario_id == 1 {
        &mut source_b
    } else {
        &mut source_a
    };
    active_source[..37].copy_from_slice(&natemu_abi::paging_off32::SOURCE);
    memory.copy(0x72000, &source_a);
    memory.copy(0x73000, &source_b);
    let data_pages_before = [[0xa5; 4096], [0x5a; 4096], [0x3c; 4096]];
    for (offset, page) in [0x70000, 0x71000, 0x74000]
        .into_iter()
        .zip(&data_pages_before)
    {
        memory.copy(offset, page);
    }
    let (mut frame, bank, _) = seed(3);
    for &native in &natemu_abi::pressure::ENTRY[..8] {
        frame.x[native] &= u32::MAX as u64;
    }
    memory.copy(BANK_OFFSET, &bank);
    let mut guest = recover::GuestState::seed(&frame);
    for (role, &native) in natemu_abi::pressure::ENTRY[..8].iter().enumerate() {
        guest.gpr[natemu_abi::pressure::GPR_ORDER[role]] = frame.x[native];
    }
    guest.rip = rip;
    guest.control[0] = 0x10001;
    guest.control[3] = 0x600;
    guest.control[4] = 0;
    guest.segments[0] = [8, 0x300000, 0xffff_ffff, 0xc09b];
    for segment in &mut guest.segments[1..] {
        *segment = [0x10, 0, 0xffff_ffff, 0xc093];
    }
    guest.segments[off::DS][1] = match scenario_id {
        7 => 0x400000,
        8 => 0x500000,
        9 => 0xfff00000,
        _ => 0x200000,
    };
    match scenario_id {
        2 => {
            guest.segments[off::DS][2] = 0xfffff;
            guest.segments[off::DS][3] &= !0x8000;
        }
        3..=5 => {
            guest.segments[0][2] = [0x2010, 0x2013, 0x1fff][scenario_id as usize - 3];
            guest.segments[0][3] &= !0x8000;
        }
        6 => guest.segments[off::DS][3] = 0xc091,
        _ => {}
    }
    let mut recovery_state = recover::Recovery::new(0);
    recovery_state.native_fpcr = frame.fpcr;
    recovery_state.native_fpsr = frame.fpsr;
    recovery_state.control.nxe = 0;
    memory.copy(recover::TABLE_OFFSET, &[0xa4; 16384]);
    let state = validation::State {
        guest_rip: rip,
        case: off::CASE,
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
    object.code_length = off::CODE_BYTES as u64;
    let shared = Shared {
        schedule: 0,
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
    let code_before = memory.read::<{ off::CODE_BYTES }>(CODE_OFFSET);
    let mut step_state: off::State = unsafe { core::mem::zeroed() };
    step_state.profile = scenario_id;
    step_state.a20 = u64::from(scenario_id != 1);
    step_state.device_value = 0x12345678;
    memory.write_record(recover::GUEST_OFFSET, guest);
    memory.write_record(recover::STATE_OFFSET, recovery_state);
    memory.write_record(validation::STATE_OFFSET, state);
    memory.write_record(off::STATE_OFFSET, step_state);
    memory.write_record(SHARED_OFFSET, shared);
    let vm = Vm::new(memory)?;
    vm.cpu.enter_resident(0, shadow::TCR_BOTH)?;
    let mut event = vm.cpu.run_to_hvc()?;
    for step in 0..9 {
        if event == 0x49 {
            break;
        }
        if event != 0x4a {
            return Err(format!("unexpected instruction transfer event {event:#x}"));
        }
        let resident = vm.shared();
        if !natemu_abi::resident::contains(resident.service_pc, 4)
            || vm.cpu.reg(PC)? != resident.service_pc
        {
            return Err("instruction request outside service boundary".into());
        }
        let mut request = [0; 16];
        for (i, word) in request.iter_mut().enumerate() {
            *word = vm.cpu.reg(i as u32)?;
        }
        if Some(request) != off::request(vm_nonce, scenario_id, step) {
            return Err("instruction request changes stored profile/source/addresses/order".into());
        }
        let directory =
            start_worker_numbered(&args[6], &args[3], Path::new(&args[7]), &request, step)?;
        let mut reply = read_packet(&directory.join("reply.bin"))?;
        if Some(reply) != off::reply(&request) {
            return Err("instruction reply differs from actual compile/request".into());
        }
        let flip = if (0..16).contains(&mutation) && step == 8 {
            Some(mutation as usize)
        } else if mutation >= 16 && step == mutation as usize - 16 {
            Some(0)
        } else {
            None
        };
        if let Some(index) = flip {
            reply[index] ^= 1;
        }
        write_packet(&directory.join("reply-delivered.bin"), &reply)?;
        for (i, word) in reply.into_iter().enumerate() {
            vm.cpu.set_reg(i as u32, word)?;
        }
        event = vm.cpu.run_to_hvc()?;
        if flip.is_some() {
            if event != 0x49 {
                return Err("mutated instruction reply did not terminate".into());
            }
            break;
        }
    }
    if event != 0x49 {
        return Err("instruction execution did not reach terminal HVC".into());
    }
    let result = vm.shared();
    let final_state = vm
        .memory
        .read_record::<validation::State>(validation::STATE_OFFSET);
    let steps = vm.memory.read_record::<off::State>(off::STATE_OFFSET);
    let ttbr1 = vm.cpu.sys(0xc101)?;
    let code_after = vm.memory.read::<{ off::CODE_BYTES }>(CODE_OFFSET);
    let data_pages_after = [
        vm.memory.read::<4096>(0x70000),
        vm.memory.read::<4096>(0x71000),
        vm.memory.read::<4096>(0x74000),
    ];
    let source_pages_before = [source_a, source_b];
    let source_pages_after = [
        vm.memory.read::<4096>(0x72000),
        vm.memory.read::<4096>(0x73000),
    ];
    let prefixes = steps
        .prefixes
        .iter()
        .map(frame_json)
        .collect::<Vec<_>>()
        .join(",");
    let step_report = format!(
        "{{\"profile\":{},\"a20\":{},\"transfers\":{},\"step\":{},\"entries\":{:?},\"requests\":{:?},\"replies\":{:?},\"fetch_linear\":{:?},\"fetch_gpa\":{:?},\"data_linear\":{:?},\"data_gpa\":{:?},\"prefixes\":[{prefixes}],\"guest_prefixes\":{:?},\"fault_steps\":{:?},\"data_before_retry\":{:?},\"service_count\":{},\"services\":{:?},\"device_value\":{}}}",
        steps.profile,
        steps.a20,
        steps.transfers,
        steps.step,
        steps.entries,
        steps.requests,
        steps.replies,
        steps.fetch_linear,
        steps.fetch_gpa,
        steps.data_linear,
        steps.data_gpa,
        steps.guest_prefixes,
        steps.fault_steps,
        steps.data_before_retry,
        steps.service_count,
        steps.services,
        steps.device_value
    );
    println!(
        "{{\"case\":\"fex_moffs_load32_steps\",\"scenario\":\"{scenario}\",\"rip\":{rip},\"live\":1,\"requests\":{},\"request\":{:?},\"reply\":{:?},\"vm_nonce\":{vm_nonce},\"service_pc\":{},\"reply_mutation\":{mutation},\"code_before\":{code_before:?},\"code_after\":{code_after:?},\"initial\":{},\"entered\":{},\"final_frame\":{},\"status\":{},\"detail\":{},\"current_el\":{},\"sctlr\":{},\"actlr\":{},\"tcr\":{},\"ttbr1\":{ttbr1},\"completed\":{},\"operand\":{},\"width\":{},\"stage\":{},\"data_pages_before\":{data_pages_before:?},\"data_pages_after\":{data_pages_after:?},\"source_pages_before\":{source_pages_before:?},\"source_pages_after\":{source_pages_after:?},\"bank_before\":{bank:?},\"recovery\":{},\"upper_root_before\":{upper_root_before:?},\"upper_root_after\":{:?},\"upper_leaf_before\":{upper_leaf_before:?},\"upper_leaf_after\":{:?},\"lower_leaf_before\":{lower_leaf_before:?},\"lower_leaf_after\":{:?},\"upper_links_before\":{upper_links_before:?},\"upper_links_after\":{:?},\"steps\":{step_report}}}",
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
        i4v::upper_links(&vm.memory)
    );
    if result.current_el != 4
        || result.sctlr & 0x1005 != 0x1005
        || result.actlr & 2 != 2
        || result.tcr != shadow::TCR_BOTH
        || ttbr1 != IPA + shadow::UPPER_ROOT as u64
    {
        return Err("instruction runtime machine profile mismatch".into());
    }
    if result.status == 0 || result.status == PANIC {
        return Err(format!(
            "instruction resident failure status={} detail={}",
            result.status, result.detail
        ));
    }
    Ok(())
}
