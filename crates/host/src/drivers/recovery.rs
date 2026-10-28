use super::*;
use natemu_abi::recovery::*;
/// The recovery family's shared seed (its "cold" scenario, number 0, as the families' runtime reads it): four-level
/// tables mapping the data pages, CPL0, and the guest state seeded from `frame`.
pub fn prepare(memory: &Memory, frame: &NativeFrame) -> GuestState {
    let mut state = Recovery::new(0);
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
    let mut guest = GuestState::seed(frame);
    guest.control[0] = (guest.control[0] & !(1 << 16)) | (state.control.wp << 16);
    guest.control[2] = state.control.cr3;
    guest.control[3] |= state.control.smep << 20;
    memory.write_record(STATE_OFFSET, state);
    memory.write_record(GUEST_OFFSET, guest);
    guest
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
