//! Live protected32 paging-off execution of the complete retained FEX body.
use crate::translation::{read_packet, run_worker, write_packet};
use super::*;
use natemu_abi::{paging_off32 as off, recovery as recover, shadow, validation};
use std::path::Path;

pub fn run(args: &[String]) -> Result<()> {
    if args[6] != "1" {
        return Err("I4-V paging-off profile admits one vCPU".into());
    }
    let mutation: i64 = if args.len() == 10 {
        let value: usize = args[9]
            .parse()
            .map_err(|_| "invalid paging-off reply mutation")?;
        if value >= 48 {
            return Err("paging-off reply mutation must be below48".into());
        }
        value as i64
    } else {
        -1
    };
    let scenario = args[4].as_str();
    let scenario_id = match scenario {
        "cold" => 0,
        "readonly" => 1,
        "warm" => 2,
        _ => return Err("unknown paging-off scenario".into()),
    };
    let rip = if let Some(hex) = args[5].strip_prefix("0x") {
        u64::from_str_radix(hex, 16)
    } else {
        args[5].parse()
    }
    .map_err(|_| "invalid paging-off RIP")?;
    if !matches!(rip, 0x102000 | 0x102080) {
        return Err("unsupported paging-off RIP".into());
    }
    let vm_nonce = new_vm_nonce()?;
    let memory = Memory::new()?;
    load_resident(&memory, &args[1])?;
    single_owner_space(&memory, true);
    memory.copy(
        recover::SOURCE_OFFSET + (rip - natemu_abi::translation::SOURCE_VA) as usize,
        &off::SOURCE,
    );
    let (mut frame, bank, _) = seed(3);
    for &native in &natemu_abi::pressure::ENTRY[..8] {
        frame.x[native] &= u32::MAX as u64;
    }
    let data = [0xa5u8; 4096];
    memory.copy(DATA_OFFSET, &data);
    memory.copy(BANK_OFFSET, &bank);
    let mut guest = recover::GuestState::seed(&frame);
    for (role, &native) in natemu_abi::pressure::ENTRY[..8].iter().enumerate() {
        guest.gpr[natemu_abi::pressure::GPR_ORDER[role]] = frame.x[native];
    }
    guest.rip = rip;
    guest.control[0] = 0x10001;
    guest.control[3] = 0x600;
    guest.control[4] = 0;
    guest.segments[0] = [8, 0, 0xffff_ffff, 0xc09b];
    for segment in &mut guest.segments[1..] {
        *segment = [0x10, 0, 0xffff_ffff, 0xc093];
    }
    let mut recovery_state = recover::Recovery::new(0);
    recovery_state.native_fpcr = frame.fpcr;
    recovery_state.native_fpsr = frame.fpsr;
    recovery_state.control.nxe = 0;
    memory.copy(recover::TABLE_OFFSET, &[0xa4; 16384]);
    if scenario_id != 0 {
        let read_only = if scenario_id == 1 { 1 << 7 } else { 0 };
        memory.word(
            shadow::LOWER_LEAF + 256 * 8,
            (IPA + DATA_OFFSET as u64) | 3 | 1 << 10 | 3 << 8 | 3 << 53 | read_only,
        );
    }
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
    let upper_root_before = table_words(&memory, shadow::UPPER_ROOT);
    let upper_leaf_before = table_words(&memory, shadow::UPPER_LEAF);
    let lower_leaf_before = table_words(&memory, shadow::LOWER_LEAF);
    let upper_links_before = i4v::upper_links(&memory);
    let code_before = memory.read::<{ off::CODE_BYTES }>(CODE_OFFSET);
    memory.write_record(recover::GUEST_OFFSET, guest);
    memory.write_record(recover::STATE_OFFSET, recovery_state);
    memory.write_record(validation::STATE_OFFSET, state);
    memory.write_record(
        off::STATE_OFFSET,
        off::State {
            a20: 1,
            scenario: scenario_id,
            transfers: 0,
            fetch_gpa: 0,
            repair_gpa: 0,
            data_before_retry: [0; 16],
            requests: [[0; 16]; 3],
            replies: [[0; 16]; 3],
            // SAFETY: the prefix record is plain integers; all-zero is valid.
            prefix: unsafe { core::mem::zeroed() },
        },
    );
    memory.write_record(SHARED_OFFSET, shared);
    let vm = Vm::new(memory)?;
    vm.cpu.enter_resident(0, shadow::TCR_BOTH)?;
    let mut event = vm.cpu.run_to_hvc()?;
    let mut directory = None;
    for chunk in 0..3 {
        if event == 0x49 && chunk == 0 {
            break;
        }
        if event != 0x4a {
            return Err(format!("unexpected paging-off transfer event {event:#x}"));
        }
        let resident = vm.shared();
        if !natemu_abi::resident::contains(resident.service_pc, 4)
            || vm.cpu.reg(PC)? != resident.service_pc
        {
            return Err("paging-off request outside service boundary".into());
        }
        let mut request = [0; 16];
        for (i, word) in request.iter_mut().enumerate() {
            *word = vm.cpu.reg(i as u32)?;
        }
        if request != off::request(vm_nonce, rip, chunk) {
            return Err("paging-off request changes compile identity/chunk order".into());
        }
        if chunk == 0 {
            directory = Some(run_worker(
                &args[7],
                &args[3],
                Path::new(&args[8]),
                &request,
                0,
            )?);
        }
        let directory = directory
            .as_ref()
            .ok_or("paging-off worker was not started")?;
        write_packet(&directory.join(format!("request-{chunk}.bin")), &request)?;
        let mut reply = read_packet(&directory.join(format!("reply-{chunk}.bin")))?;
        if Some(reply) != off::reply(&request) {
            return Err("paging-off reply differs from request/generated chunk".into());
        }
        if mutation >= 0 && mutation as u64 / 16 == chunk {
            reply[mutation as usize % 16] ^= 1;
        }
        write_packet(
            &directory.join(format!("reply-delivered-{chunk}.bin")),
            &reply,
        )?;
        for (i, word) in reply.into_iter().enumerate() {
            vm.cpu.set_reg(i as u32, word)?;
        }
        event = vm.cpu.run_to_hvc()?;
        if mutation >= 0 && mutation as u64 / 16 == chunk {
            if event != 0x49 {
                return Err("mutated paging-off reply did not terminate".into());
            }
            break;
        }
    }
    if event != 0x49 {
        return Err("paging-off execution did not reach terminal HVC".into());
    }
    let result = vm.shared();
    let final_state = vm
        .memory
        .read_record::<validation::State>(validation::STATE_OFFSET);
    let off_state = vm.memory.read_record::<off::State>(off::STATE_OFFSET);
    let ttbr1 = vm.cpu.sys(0xc101)?;
    let code_after = vm.memory.read::<{ off::CODE_BYTES }>(CODE_OFFSET);
    let memory_after = vm.memory.read::<4096>(DATA_OFFSET);
    let body_hex = off::BODY
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    println!(
        "{{\"case\":\"fex_moffs_load32\",\"scenario\":\"{scenario}\",\"rip\":{rip},\"live\":1,\"requests\":{},\"request\":{:?},\"reply\":{:?},\"vm_nonce\":{vm_nonce},\"service_pc\":{},\"reply_mutation\":{mutation},\"code_before\":{code_before:?},\"code_after\":{code_after:?},\"body_hex\":\"{body_hex}\",\"initial\":{},\"entered\":{},\"final_frame\":{},\"status\":{},\"detail\":{},\"current_el\":{},\"sctlr\":{},\"actlr\":{},\"tcr\":{},\"ttbr1\":{ttbr1},\"completed\":{},\"operand\":{},\"width\":{},\"stage\":{},\"memory_before\":{data:?},\"memory_after\":{memory_after:?},\"data_page_before\":{data:?},\"data_page_after\":{memory_after:?},\"bank_before\":{bank:?},\"recovery\":{},\"upper_root_before\":{upper_root_before:?},\"upper_root_after\":{:?},\"upper_leaf_before\":{upper_leaf_before:?},\"upper_leaf_after\":{:?},\"lower_leaf_before\":{lower_leaf_before:?},\"lower_leaf_after\":{:?},\"upper_links_before\":{upper_links_before:?},\"upper_links_after\":{:?},\"off\":{{\"a20\":{},\"scenario\":{},\"transfers\":{},\"fetch_gpa\":{},\"repair_gpa\":{},\"data_before_retry\":{:?},\"requests\":{:?},\"replies\":{:?},\"prefix\":{}}}}}",
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
        table_words(&vm.memory, shadow::UPPER_ROOT),
        table_words(&vm.memory, shadow::UPPER_LEAF),
        table_words(&vm.memory, shadow::LOWER_LEAF),
        i4v::upper_links(&vm.memory),
        off_state.a20,
        off_state.scenario,
        off_state.transfers,
        off_state.fetch_gpa,
        off_state.repair_gpa,
        off_state.data_before_retry,
        off_state.requests,
        off_state.replies,
        guest_json(&off_state.prefix)
    );
    if result.current_el != 4
        || result.sctlr & 0x1005 != 0x1005
        || result.actlr & 2 != 2
        || result.tcr != shadow::TCR_BOTH
        || ttbr1 != IPA + shadow::UPPER_ROOT as u64
    {
        return Err("paging-off runtime machine profile mismatch".into());
    }
    if result.status == 0 || result.status == PANIC {
        return Err(format!(
            "paging-off resident failure status={} detail={}",
            result.status, result.detail
        ));
    }
    Ok(())
}
