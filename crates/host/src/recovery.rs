use super::*;
use natemu_abi::recovery::*;
pub const CASES: [&str; 12] = [
    "cold",
    "load_np",
    "store_ro",
    "parent_ro",
    "wp_off",
    "user_denied",
    "reserved",
    "fetch_nx",
    "smep",
    "carry",
    "overflow",
    "large",
];
pub fn prepare(
    memory: &Memory,
    frame: &NativeFrame,
    name: &str,
    pressure: bool,
) -> Result<GuestState> {
    let scenario = CASES
        .iter()
        .position(|&s| s == name)
        .ok_or("unknown paging scenario")? as u64;
    let mut state = Recovery::new(scenario);
    state.native_fpcr = frame.fpcr;
    state.native_fpsr = frame.fpsr;
    for (off, value) in [
        (0, 0x2007),
        (0x1000, 0x3007),
        (0x2000, 0x4007),
        (0x3000 + 256 * 8, DATA_VA | 7),
        (0x3000 + 257 * 8, (DATA_VA + 4096) | 7),
        (0x3000 + 258 * 8, (DATA_VA + 8192) | 7),
    ] {
        memory.word(TABLE_OFFSET + off, value);
    }
    match name {
        "cold" | "carry" | "overflow" => {}
        "large" => memory.word(TABLE_OFFSET + 0x2000, 0x87),
        "load_np" => memory.word(TABLE_OFFSET + 0x3000 + 256 * 8, u64::MAX - 1),
        "store_ro" => memory.word(TABLE_OFFSET + 0x3000 + 256 * 8, DATA_VA | 5),
        "parent_ro" => memory.word(TABLE_OFFSET + 0x2000, 0x4005),
        "wp_off" => {
            state.control.wp = 0;
            memory.word(TABLE_OFFSET + 0x3000 + 256 * 8, DATA_VA | 5);
        }
        "user_denied" => {
            state.control.cpl = 3;
            memory.word(TABLE_OFFSET + 0x3000 + 256 * 8, DATA_VA | 3);
        }
        "reserved" => memory.word(TABLE_OFFSET + 0x3000 + 256 * 8, DATA_VA | 7 | (1 << 40)),
        "fetch_nx" => memory.word(
            TABLE_OFFSET + 0x3000 + 258 * 8,
            (DATA_VA + 8192) | 7 | (1 << 63),
        ),
        "smep" => state.control.smep = 1,
        _ => return Err("unsupported paging scenario".into()),
    }
    let mut guest = GuestState::seed(frame);
    if pressure {
        for (role, &native) in natemu_abi::pressure::ENTRY.iter().enumerate() {
            guest.gpr[natemu_abi::pressure::GPR_ORDER[role]] = frame.x[native];
        }
        for i in 0..16 {
            guest.ymm[i][..16].copy_from_slice(&frame.q[16 + i]);
        }
    }
    guest.control[0] = (guest.control[0] & !(1 << 16)) | (state.control.wp << 16);
    guest.control[2] = state.control.cr3;
    guest.control[3] |= state.control.smep << 20;
    if state.control.cpl == 3 {
        guest.segments[0] = [0x1b, 0, 0xffff_ffff, 0xa0fb];
        for segment in &mut guest.segments[1..6] {
            *segment = [0x23, 0, 0xffff_ffff, 0xc0f3];
        }
    }
    memory.write_record(STATE_OFFSET, state);
    memory.write_record(GUEST_OFFSET, guest);
    Ok(guest)
}
pub(super) fn guest_json(g: &GuestState) -> String {
    format!(
        "{{\"gpr\":{:?},\"rip\":{},\"rflags\":{},\"ymm\":{:?},\"mxcsr\":{},\"x87\":[{},{},{},{},{},{}],\"x87_regs\":{:?},\"control\":{:?},\"segments\":{:?},\"reserved\":{}}}",
        g.gpr,
        g.rip,
        g.rflags,
        g.ymm,
        g.mxcsr,
        g.x87_control,
        g.x87_status,
        g.x87_tag,
        g.x87_opcode,
        g.x87_ip,
        g.x87_dp,
        g.x87_regs,
        g.control,
        g.segments,
        g.reserved
    )
}
pub fn report(vm: &Vm, before: &GuestState, tables_before: &[u8; 16384]) -> String {
    let r = vm.memory.read_record::<Recovery>(STATE_OFFSET);
    let g = vm.memory.read_record::<GuestState>(GUEST_OFFSET);
    let mut traces = Vec::new();
    for t in &r.trace[..(r.faults as usize).min(MAX_FAULTS)] {
        traces.push(format!("{{\"frame\":{},\"esr\":{},\"far\":{},\"elr\":{},\"spsr\":{},\"outcome\":{},\"error\":{}}}",frame_json(&t.frame),t.esr,t.far,t.elr,t.spsr,t.outcome,t.error));
    }
    format!(
        "{{\"capture_pc\":{},\"guest_before\":{},\"guest_after\":{},\"control\":[{},{},{},{},{}],\"faults\":{},\"walks\":{},\"retries\":{},\"vector\":{},\"error\":{},\"linear\":{},\"rip\":{},\"traces\":[{}],\"tables_before\":{:?},\"tables_after\":{:?},\"memory_after\":{:?},\"bank_after\":{:?}}}",
        vm.shared().capture_pc,
        guest_json(before),
        guest_json(&g),
        r.control.cr3,
        r.control.cpl,
        r.control.wp,
        r.control.nxe,
        r.control.smep,
        r.faults,
        r.walks,
        r.retries,
        r.vector,
        r.error,
        r.linear,
        r.rip,
        traces.join(","),
        tables_before,
        vm.memory.read::<16384>(TABLE_OFFSET),
        vm.memory.read::<256>(DATA_OFFSET),
        vm.memory.read::<512>(BANK_OFFSET)
    )
}

pub fn allocated_report(vm: &Vm, initial: &NativeFrame, final_frame: &NativeFrame) -> String {
    format!(
        "{{\"initial\":{},\"final_frame\":{},\"spills\":{:?},\"export\":{:?},\"native_fp_restored\":[{},{}]}}",
        frame_json(initial),
        frame_json(final_frame),
        vm.memory.read::<3072>(0x24000),
        vm.memory.read::<1168>(0x23000),
        vm.cpu.reg(32).unwrap_or(u64::MAX),
        vm.cpu.reg(33).unwrap_or(u64::MAX)
    )
}

pub fn helper_report(vm: &Vm) -> String {
    let h = vm.memory.read_record::<HelperState>(0x25000);
    format!(
        "{{\"calls\":{},\"outputs\":{},\"before\":{},\"staged\":{},\"poisoned\":{}}}",
        h.calls,
        h.outputs,
        frame_json(&h.before),
        frame_json(&h.staged),
        frame_json(&h.poisoned)
    )
}

pub fn partial_report(vm: &Vm) -> String {
    let p = vm.memory.read_record::<PartialState>(0x26000);
    let t = &p.trace;
    format!(
        "{{\"reads\":{},\"writes\":{},\"faults\":{},\"retries\":{},\"phase\":{},\"value\":{},\"read_values\":{:?},\"store_pc\":{},\"at_fault_counts\":{:?},\"at_fault\":{},\"prefix\":{},\"before\":{},\"trace\":{{\"frame\":{},\"esr\":{},\"far\":{},\"elr\":{},\"spsr\":{},\"outcome\":{},\"error\":{}}},\"written\":{:?}}}",
        p.reads,
        p.writes,
        p.faults,
        p.retries,
        p.phase,
        p.value,
        p.read_values,
        p.store_pc,
        p.at_fault_counts,
        guest_json(&vm.memory.read_record::<GuestState>(0x27000)),
        guest_json(&p.prefix),
        guest_json(&p.before),
        frame_json(&t.frame),
        t.esr,
        t.far,
        t.elr,
        t.spsr,
        t.outcome,
        t.error,
        vm.memory.read::<2>(DATA_OFFSET + 4095)
    )
}
