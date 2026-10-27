//! Mixed32 fixture setup and per-owner observations; common coordination stays shared.
use super::*;
use natemu_abi::{families::mixed32, recovery as recover};

pub fn seed_owner(frame: &mut NativeFrame, data: &mut [u8; 256], id: usize) {
    frame.x[4] = 0x10203000 + id as u64;
    frame.x[7] = 0x50607000 + id as u64;
    frame.x[6] = DATA_VA + id as u64 * 256;
    frame.x[5] = frame.x[6] + 128;
    frame.x[11] = DATA_VA + 128;
    data.fill(0xa5);
    data[128..132].copy_from_slice(&(0x11220000u32 + id as u32).to_le_bytes());
}
pub fn replacement(id: usize) -> [u8; 256] {
    let mut bytes = [0x5a; 256];
    bytes[128..132].copy_from_slice(&(0x33440000u32 + id as u32).to_le_bytes());
    bytes
}
pub fn guest(frame: &NativeFrame, id: usize, profile: u64) -> recover::GuestState {
    let mode = mixed32::owner_mode(profile, id as u64).unwrap();
    let mut g = recover::GuestState::seed(frame);
    for (role, &native) in natemu_abi::pressure::ENTRY[..8].iter().enumerate() {
        g.gpr[natemu_abi::pressure::GPR_ORDER[role]] = frame.x[native] as u32 as u64;
    }
    g.segments[0][3] = 0xc09b;
    if mode == 0 {
        g.control[0] = 0x10001;
        g.control[3] = 0x600;
        g.control[4] = 0;
    }
    if id == 0 {
        g.rflags &= !0x200;
    }
    g
}
/// This owner's own code pages, instead of the shared ones.
pub fn private_code(memory: &Memory, base: usize) {
    map_private(
        memory,
        base,
        &[Span::text(CODE_OFFSET, 8192, base + CODE_OFFSET)],
    );
}
pub fn state(memory: &Memory, base: usize) -> mixed32::State {
    memory.read_record(base + mixed32::OFFSET)
}
pub fn report(memory: &Memory, id: usize, profile: u64, frame: &NativeFrame) -> String {
    let base = id * RAM_SIZE;
    let m = state(memory, base);
    let g = memory.read_record::<recover::GuestState>(base + recover::GUEST_OFFSET);
    let r = memory.read_record::<recover::Recovery>(base + recover::STATE_OFFSET);
    let s = memory.read_record::<Shared>(base + SHARED_OFFSET);
    let traces=r.trace[..(r.faults as usize).min(recover::MAX_FAULTS)].iter().map(|t|format!("{{\"frame\":{},\"esr\":{},\"far\":{},\"elr\":{},\"spsr\":{},\"outcome\":{},\"error\":{}}}",frame_json(&t.frame),t.esr,t.far,t.elr,t.spsr,t.outcome,t.error)).collect::<Vec<_>>().join(",");
    format!(
        "{{\"id\":{id},\"mode\":{},\"incarnation\":{},\"context\":{},\"a20\":{},\"expected\":{:?},\"requests\":{:?},\"replies\":{:?},\"stale_replies\":{},\"wrong_owner\":{},\"wrong_mode\":{},\"blank_before_publish\":{},\"fetch_gpa\":{:?},\"routes\":{:?},\"source_before\":{:?},\"source_after\":{:?},\"initial\":{},\"old_memory_after\":{:?},\"new_memory_before\":{:?},\"new_memory_after\":{:?},\"guest_before\":{},\"guest_after\":{},\"current_el\":{},\"sctlr\":{},\"actlr\":{},\"tcr\":{},\"service_pc\":{},\"old_code\":{:?},\"new_code\":{:?},\"lower_leaf\":{:?},\"walks\":{},\"retries\":{},\"faults\":{},\"vector\":{},\"error\":{},\"linear\":{},\"rip\":{},\"traces\":[{traces}]}}",
        m.mode,
        m.incarnation,
        m.context,
        m.a20,
        m.expected,
        m.requests,
        m.replies,
        m.stale_replies,
        m.wrong_owner,
        m.wrong_mode,
        m.blank_before_publish,
        m.fetch_gpa,
        m.routes,
        m.source_before,
        m.source_after,
        frame_json(&m.initial),
        m.old_memory_after,
        m.new_memory_before,
        m.new_memory_after,
        guest_json(&guest(frame, id, profile)),
        guest_json(&g),
        s.current_el,
        s.sctlr,
        s.actlr,
        s.tcr,
        s.service_pc,
        memory.read::<24>(base + CODE_OFFSET),
        memory.read::<24>(base + CODE_OFFSET + 4096),
        table_words(memory, base + shadow::LOWER_LEAF),
        r.walks,
        r.retries,
        r.faults,
        r.vector,
        r.error,
        r.linear,
        r.rip
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mixed_code_is_private_while_data_and_guest_tables_remain_shared() {
        let memory = Memory::with_size(2 * RAM_SIZE).unwrap();
        for id in 0..2 {
            let base = id * RAM_SIZE;
            super::super::tables(&memory, base, false);
            private_code(&memory, base);
            let leaf = |offset| {
                u64::from_le_bytes(memory.read::<8>(base + 0x14000 + offset / 4096 * 8))
                    & 0x0000_ffff_ffff_f000
            };
            for offset in [CODE_OFFSET, CODE_OFFSET + 4096] {
                assert_eq!(leaf(offset), IPA + (base + offset) as u64);
            }
            for offset in [DATA_OFFSET, recover::SOURCE_OFFSET, recover::TABLE_OFFSET] {
                assert_eq!(leaf(offset), IPA + offset as u64);
            }
        }
    }
    #[test]
    fn same_decode_width_retains_distinct_owner_modes_and_read_data() {
        for id in 0..4 {
            let (mut frame, _, mut data) = super::super::seed(id);
            seed_owner(&mut frame, &mut data, id);
            for profile in 1..=2 {
                let g = guest(&frame, id, profile);
                assert_eq!(
                    mixed32::mode(&g, 1),
                    mixed32::owner_mode(profile, id as u64)
                );
                assert!(g.gpr[..8].iter().all(|v| *v <= u32::MAX as u64));
            }
            assert_ne!(data[128..132], replacement(id)[128..132]);
        }
    }
}
