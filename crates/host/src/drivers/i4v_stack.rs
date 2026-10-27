//! Live stack-fragment validation with one compile and three bounded replies.
use super::*;
use natemu_abi::{recovery as recover, shadow, families::stack, families::validation};
use crate::translation::{read_packet, run_worker, write_packet};
use std::path::Path;

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
    let vm_nonce = new_vm_nonce()?;
    let memory = Memory::new()?;
    load_resident(&memory, &args[1])?;
    single_owner_space(&memory, true);
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
    let mut recovery_state = memory.read_record::<recover::Recovery>(recover::STATE_OFFSET);
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
    let code_before = memory.read::<{ stack::CODE_BYTES }>(CODE_OFFSET);
    memory.write_record(recover::GUEST_OFFSET, guest);
    memory.write_record(recover::STATE_OFFSET, recovery_state);
    memory.write_record(validation::STATE_OFFSET, state);
    memory.write_record(stack::STATE_OFFSET, stack::State::ZERO);
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
            return Err(format!("unexpected stack transfer event {event:#x}"));
        }
        let resident = vm.shared();
        if !natemu_abi::resident::contains(resident.service_pc, 4)
            || vm.cpu.reg(PC)? != resident.service_pc
        {
            return Err("stack request outside service boundary".into());
        }
        let mut request = [0; 16];
        for (i, word) in request.iter_mut().enumerate() {
            *word = vm.cpu.reg(i as u32)?;
        }
        if request != stack::request(vm_nonce, rip, chunk) {
            return Err("stack request changes compile identity/chunk order".into());
        }
        if chunk == 0 {
            directory = Some(run_worker(
                &args[7],
                &args[3],
                Path::new(&args[8]),
                &request,
                1,
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
            vm.cpu.set_reg(i as u32, word)?;
        }
        event = vm.cpu.run_to_hvc()?;
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
    let final_state = vm
        .memory
        .read_record::<validation::State>(validation::STATE_OFFSET);
    let stack_state = vm.memory.read_record::<stack::State>(stack::STATE_OFFSET);
    let prefixes = stack_state
        .prefixes
        .iter()
        .map(frame_json)
        .collect::<Vec<_>>()
        .join(",");
    let ttbr1 = vm.cpu.sys(0xc101)?;
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
        table_words(&vm.memory, shadow::UPPER_ROOT),
        table_words(&vm.memory, shadow::UPPER_LEAF),
        table_words(&vm.memory, shadow::LOWER_LEAF),
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
