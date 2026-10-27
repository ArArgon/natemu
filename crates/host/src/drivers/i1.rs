//! I1: one resident-admitted native object (pinned FEX fragment, NOP or UDF)
//! over a seeded input vector, with optional rejection mutations of the object.
use super::*;

pub(super) fn run(a: &[String]) -> Result<()> {
    let configured: usize = a[6].parse().map_err(|_| "invalid vCPU count")?;
    if configured != 1 {
        return Err("I1 admits exactly one vCPU; N-vCPU execution is not tested".into());
    }
    let kind = match a[3].as_str() {
        "control" => CONTROL,
        "live_load" => LIVE_LOAD,
        "fault" => FAULT,
        _ => return Err("unknown case".into()),
    };
    let index: usize = a[4].parse().map_err(|_| "invalid vector")?;
    if index >= 4 {
        return Err("unknown input vector".into());
    }
    let fex = read_input(&a[2], 44)?;
    if fex != FEX_BODY {
        return Err("FEX artifact does not match pinned I1 producer bytes".into());
    }
    let memory = Memory::new()?;
    load_resident(&memory, &a[1])?;
    single_owner_space(&memory, false);
    let mut code = match kind {
        CONTROL => NOP.to_vec(),
        LIVE_LOAD => fex,
        FAULT => UDF.to_vec(),
        _ => unreachable!(),
    };
    code.extend_from_slice(&RET);
    let (frame, bank, data) = seed(index);
    memory.copy(BANK_OFFSET, &bank);
    memory.copy(DATA_OFFSET, &data);
    let mut frames = vec![frame; configured];
    let initial = &mut frames[0];
    let mut object = Object::new(kind);
    match a[5].as_str() {
        "none" => {}
        "version" => object.version += 1,
        "profile" => object.profile += 1,
        "size" => object.size -= 1,
        "target" => object.code_address = DATA_VA,
        "entry" => object.entry_offset = u64::MAX,
        "code" => code[0] ^= 1,
        "address" => initial.x[6] = PRIVATE,
        "vcpu" => object.vcpu_id = 1,
        _ => return Err("unknown rejection mutation".into()),
    }
    memory.copy(CODE_OFFSET, &code);
    let shared = Shared {
        schedule: 0,
        object,
        initial: *initial,
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
        mode: 0,
        lifecycle: natemu_abi::translation::Lifecycle::new(0),
    };
    memory.write_record(SHARED_OFFSET, shared);
    let vm = Vm::new(memory)?;
    let isar0 = vm.cpu.sys(ID_AA64ISAR0_EL1)?;
    let tcr = SINGLE_ROOT_TCR;
    vm.cpu.enter_resident(0, tcr)?;
    vm.run(0x49)?;
    let result = vm.shared();
    if result.status == SUCCESS || result.status == NATIVE_FAULT {
        let mut expected_initial = shared.initial;
        expected_initial.x[16] = CODE_VA;
        expected_initial.x[30] = result.capture_pc;
        if result.initial != expected_initial {
            return Err("resident modified seeded entry state".into());
        }
    }
    let case = if a[5] == "none" {
        a[3].as_str()
    } else {
        "reject"
    };
    println!(
        "{{\"case\":\"{case}\",\"status\":{},\"detail\":{},\"initial\":{},\"captured\":{},\"memory_base\":{DATA_VA},\"memory_before\":{data:?},\"memory_after\":{:?},\"bank_before\":{bank:?},\"bank_after\":{:?},\"object_pc\":{CODE_VA},\"capture_pc\":{},\"fault\":{{\"esr\":{},\"far\":{},\"elr\":{},\"spsr\":{}}},\"profile\":{{\"configured_vcpus\":{configured},\"tested_vcpus\":1,\"current_el\":{},\"sctlr\":{},\"actlr\":{},\"tcr\":{},\"isar0\":{isar0}}}}}",
        result.status,
        result.detail,
        frame_json(&shared.initial),
        frame_json(&result.captured),
        vm.memory.read::<256>(DATA_OFFSET),
        vm.memory.read::<512>(BANK_OFFSET),
        result.capture_pc,
        result.esr,
        result.far,
        result.elr,
        result.spsr,
        result.current_el,
        result.sctlr,
        result.actlr,
        result.tcr
    );
    if result.current_el != 4
        || result.sctlr & 0x1005 != 0x1005
        || result.actlr & 2 != 2
        || result.tcr != tcr
    {
        return Err("runtime machine profile mismatch".into());
    }
    if result.status == 0 || result.status == PANIC {
        return Err(format!(
            "resident failure status={} detail={}",
            result.status, result.detail
        ));
    }
    Ok(())
}
