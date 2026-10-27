//! Protected32 owners share physical RAM while retaining distinct address formation.
use super::*;
use natemu_abi::{families::address_concurrent as a, families::maintenance as m, recovery as r};

macro_rules! fields {
    ($s:ident; $($field:ident),* $(,)?) => {{
        let mut out = Vec::<String>::new();
        $(out.push(format!("\"{}\":{:?}", stringify!($field), $s.$field));)*
        out
    }};
}
fn address_json(s: &a::Address) -> String {
    format!("{{{}}}", fields!(s; offset,linear,raw,gpa).join(","))
}
fn owner_json(s: &a::Owner) -> String {
    let mut f = fields!(s; enabled,id,mode,context,generation,serial,program,step,visits,current_visit,terminal,code_context,expected,deferred,stale_reply,walks,reads,writes,stale_rejected,wrong_owner_rejected,entry_rejected,retained_before_release,reclaimed,vector,error,fault_address,fault_rip,maintenance_acks,source_ack);
    for (name, frame) in [
        ("initial", &s.initial),
        ("current", &s.current),
        ("terminal_initial", &s.terminal_initial),
    ] {
        f.push(format!("\"{name}\":{}", frame_json(frame)));
    }
    for (name, guest) in [
        ("guest_before", &s.guest_before),
        ("guest_after", &s.guest_after),
    ] {
        f.push(format!("\"{name}\":{}", guest_json(guest)));
    }
    for (name, address) in [
        ("saved_store", &s.saved_store),
        ("saved_load", &s.saved_load),
    ] {
        f.push(format!("\"{name}\":{}", address_json(address)));
    }
    format!("{{{}}}", f.join(","))
}
fn visit_json(s: &a::Visit) -> String {
    let mut f = fields!(s; request,reply,program,step,generation,terminal,entered,completed,esr,far,pc,spsr,service,continuation,value,memory_before,memory_after);
    f.push(format!(
        "\"prefix\":{},\"fault\":{},\"fetch\":{},\"access\":{}",
        frame_json(&s.prefix),
        frame_json(&s.fault),
        address_json(&s.fetch),
        address_json(&s.access)
    ));
    format!("{{{}}}", f.join(","))
}
fn common_json(s: &a::Shared) -> String {
    let mut f = fields!(s; n,profile,actor,compiled,old_done,source_changed,acknowledgements,new_done,maintenance_started,terminal_ready,terminal_done,terminal_queued,terminal_queue,terminal_length,terminal_head,terminal_servicing,released,reclaimed,source_before,source_after,source_leaf_before,source_leaf_after,data_leaf_before,data_leaf_after,code_before,code_after,terminal_tables_before,terminal_data_before);
    f.push(format!(
        "\"source_write\":{}",
        address_json(&s.source_write)
    ));
    format!("{{{}}}", f.join(","))
}
fn owner_mappings(memory: &Memory, id: usize) {
    let base = id * RAM_SIZE;
    tables(memory, base, false);
    mixed::private_code(memory, base);
    map_private(
        memory,
        base,
        &[
            Span::data(
                a::RECORD_OFFSET,
                0x90000 - a::RECORD_OFFSET,
                base + a::RECORD_OFFSET,
            ),
            Span::data(
                a::SHARED_OFFSET,
                0xa0000 - a::SHARED_OFFSET,
                a::SHARED_OFFSET,
            ),
        ],
    );
    // Offset addresses must fault; their physical pages have different byte offsets.
    memory.word(base + shadow::LOWER_LEAF + 256 * 8, 0);
}
fn legacy_tables(config: a::Config) -> [u8; 16384] {
    let mut bytes = [0; 16384];
    for (offset, value) in [
        (4, 0x2007u32),
        (0x1008, 0x302007),
        (
            0x1000 + config.data_leaf() as usize * 4,
            config.data_raw(false) as u32 | 7,
        ),
    ] {
        bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
    }
    bytes
}
fn seed_owner(config: a::Config, id: usize) -> (NativeFrame, [u8; 512], r::GuestState) {
    let (mut f, mut bank, _) = seed(id);
    for &native in &natemu_abi::pressure::ENTRY[..8] {
        f.x[native] &= u32::MAX as u64;
    }
    f.x[4] = 0x11220000 + id as u64;
    f.x[7] = 0x55660000 + id as u64;
    f.x[6] = config.ebx(id as u64, false);
    f.x[5] = config.edx(id as u64);
    f.x[10] = config.esi(id as u64);
    for b in &mut bank {
        *b ^= id as u8;
    }
    let mut guest = r::GuestState::seed(&f);
    for (role, &native) in natemu_abi::pressure::ENTRY[..8].iter().enumerate() {
        guest.gpr[natemu_abi::pressure::GPR_ORDER[role]] = f.x[native];
    }
    guest.rip = a::EIP;
    guest.control[0] = if config.mode(id as u64) == 0 {
        0x10001
    } else {
        0x80010001
    };
    guest.control[2] = 0x1000;
    guest.control[3] = 0x600;
    guest.control[4] = 0;
    guest.segments[0] = config.cs(id as u64, false);
    guest.segments[1] = config.ds(id as u64);
    for segment in &mut guest.segments[2..] {
        *segment = [0x10, 0, 0xffff_ffff, 0xc093];
    }
    (f, bank, guest)
}
fn owner_report(
    memory: &Memory,
    id: usize,
    seed: &(NativeFrame, [u8; 512], r::GuestState),
    requests: &[wire::Packet],
    lower_before: &[u64; 512],
) -> String {
    let base = id * RAM_SIZE;
    let shared: Shared = memory.read_record(base + SHARED_OFFSET);
    let owner = unsafe { &*memory.0.add(base + a::OWNER_OFFSET).cast::<a::Owner>() };
    let event: EventRecord = memory.read_record(base + EVENT_OFFSET);
    let guest: r::GuestState = memory.read_record(base + r::GUEST_OFFSET);
    let mut f = fields!(shared; status,detail,current_el,sctlr,actlr,tcr,capture_pc,service_pc,esr,far,elr,spsr);
    f.push(format!("\"id\":{id},\"vm_nonce\":{},\"compiler_requests\":{:?},\"bank_before\":{:?},\"bank_after\":{:?},\"old_code\":{:?},\"new_code\":{:?}", shared.lifecycle.vm, requests.iter().filter(|p|p[3] == id as u64).collect::<Vec<_>>(),seed.1,memory.read::<512>(base+BANK_OFFSET),memory.read::<{a::CODE_BYTES}>(base+CODE_OFFSET),memory.read::<{a::CODE_BYTES}>(base+CODE_OFFSET+4096)));
    f.push(format!("\"initial\":{},\"entered\":{},\"final_frame\":{},\"guest_before\":{},\"guest_after\":{},\"owner\":{}",frame_json(&seed.0),frame_json(&shared.initial),frame_json(&shared.captured),guest_json(&seed.2),guest_json(&guest),owner_json(owner)));
    let visits = (0..a::MAX_VISITS)
        .map(|i| {
            visit_json(
                &memory.read_record(base + a::RECORD_OFFSET + i * core::mem::size_of::<a::Visit>()),
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    f.push(format!("\"visits\":[{visits}]"));
    let mut e = fields!(event; consumed,deliveries,boundary,blocked,wait_checks,shadow,priority);
    e.push(format!(
        "\"packet\":{}",
        guest_json(&event.packet)
    ));
    f.push(format!("\"event\":{{{}}}", e.join(",")));
    f.push(format!("\"lower_leaf_before\":{lower_before:?}"));
    for (name, offset) in [
        ("lower_leaf_after", shadow::LOWER_LEAF),
        ("upper_root_after", shadow::UPPER_ROOT),
        ("upper_leaf_after", shadow::UPPER_LEAF),
    ] {
        f.push(format!(
            "\"{name}\":{:?}",
            table_words(memory, base + offset)
        ));
    }
    format!("{{{}}}", f.join(","))
}
pub fn run(args: &[String]) -> Result<()> {
    let profile = args[4].parse().map_err(|_| "invalid address profile")?;
    let n: usize = args[5].parse().map_err(|_| "invalid address vCPU count")?;
    let actor = args[6].parse().map_err(|_| "invalid address actor")?;
    let config = a::Config::new(n as u64, profile, actor)
        .ok_or("unsupported address profile/count/actor")?;
    let mut coordinator = Coordinator::new(n).unwrap();
    coordinator.scenario = 5;
    let common = a::Shared::new(n as u64, profile, actor).unwrap();
    require_vcpus(n)?;
    let memory = Memory::with_size(n * RAM_SIZE)?;
    load_resident(&memory, &args[1])?;
    let vm_nonce = new_vm_nonce()?;
    let mut data_before = [0xa5; 8192];
    data_before[4096..].fill(0x5a);
    if profile != 3 {
        for id in 0..n {
            let at = (config.ds(id as u64)[1].wrapping_add(config.edx(id as u64)) & 4095) as usize;
            data_before[at..at + 4].copy_from_slice(&(0xa1b20000u32 + id as u32).to_le_bytes());
            data_before[4096 + at..4100 + at]
                .copy_from_slice(&(0xc3d40000u32 + id as u32).to_le_bytes());
        }
    }
    let mut source_before = [0xcc; 8192];
    for step in 0..3 {
        let bytes = a::source(profile, 1, step).unwrap();
        let at = 0x123 + a::offset(profile, step).unwrap() as usize;
        source_before[at..at + bytes.len()].copy_from_slice(bytes);
    }
    let tables_before = legacy_tables(config);
    let mut seeds = Vec::new();
    let mut lower_before = Vec::new();
    for id in 0..n {
        let base = id * RAM_SIZE;
        if id != 0 {
            memory.replicate_resident_text(base);
        }
        owner_mappings(&memory, id);
        lower_before.push(table_words(&memory, base + shadow::LOWER_LEAF));
        let seeded = seed_owner(config, id);
        let (frame, bank, guest) = seeded;
        memory.copy(base + BANK_OFFSET, &bank);
        let mut object = Object::new(LIVE_LOAD);
        object.code_length = a::CODE_BYTES as u64;
        object.configured_vcpus = n as u32;
        object.vcpu_id = id as u32;
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
            mode: MODE,
            lifecycle: wire::Lifecycle::new(vm_nonce),
        };
        let mut owner = a::Owner::ZERO;
        owner.enabled = 1;
        owner.id = id as u64;
        owner.mode = config.mode(id as u64);
        owner.context = config.context(id as u64, false);
        owner.generation = 1;
        let mut recovery = r::Recovery::new(0);
        recovery.native_fpcr = frame.fpcr;
        recovery.native_fpsr = frame.fpsr;
        let mut event = EventRecord::new(&frame, id);
        event.packet = guest;
        memory.write_record(base + SHARED_OFFSET, shared);
        memory.write_record(base + a::OWNER_OFFSET, owner);
        memory.write_record(base + r::GUEST_OFFSET, guest);
        memory.write_record(base + r::STATE_OFFSET, recovery);
        memory.write_record(base + ROUND_OFFSET, Round::ZERO);
        memory.write_record(base + EVENT_OFFSET, event);
        seeds.push(seeded);
    }
    memory.copy(DATA_OFFSET, &data_before);
    memory.copy(r::SOURCE_OFFSET, &source_before);
    memory.copy(r::TABLE_OFFSET, &tables_before);
    memory.write_record(COORD_OFFSET, coordinator);
    memory.write_record(a::SHARED_OFFSET, common);
    memory.write_record(m::OFFSET, m::State::new(n).unwrap());
    fs::create_dir(&args[8]).map_err(|e| e.to_string())?;
    let _machine = Machine::create(&memory)?;
    let ram = memory.0 as usize;
    let service = |id: usize| -> Result<Option<Service>> {
        let base = ram + id * RAM_SIZE;
        let output = owner_output(&args[8], id)?;
        Ok(checked(
            crate::drivers::i4v_controls::producer::Compiler::new_address(
                args[3].clone().into(),
                args[7].clone().into(),
                output,
                id as u64,
                config,
            ),
            move |_| unsafe {
                ptr::read_volatile((base + a::OWNER_OFFSET) as *const a::Owner).expected
            },
        ))
    };
    let execution = run_owners(
        Owners {
            n,
            tcr: shadow::TCR_BOTH,
            memory: ram,
            delayed: None,
            deadline: Duration::from_secs(100),
        },
        &service,
    );
    // All owners have joined before snapshots are read, including on failure.
    let common = unsafe { &*memory.0.add(a::SHARED_OFFSET).cast::<a::Shared>() };
    let coordinator = unsafe { &*memory.0.add(COORD_OFFSET).cast::<Coordinator>() };
    let maintenance: m::State = memory.read_record(m::OFFSET);
    let requests = execution.as_ref().map(Vec::as_slice).unwrap_or(&[]);
    let owners = (0..n)
        .map(|id| owner_report(&memory, id, &seeds[id], requests, &lower_before[id]))
        .collect::<Vec<_>>()
        .join(",");
    println!(
        "{{\"case\":\"address-concurrent\",\"profile\":{profile},\"configured_vcpus\":{n},\"tested_vcpus\":{n},\"actor\":{actor},\"vm_nonce\":{vm_nonce},\"address\":{},\"coordinator\":{},\"maintenance\":{},\"owners\":[{owners}],\"source_before\":{source_before:?},\"source_after\":{:?},\"data_before\":{data_before:?},\"data_after\":{:?},\"tables_before\":{tables_before:?},\"tables_after\":{:?}}}",
        common_json(common),
        controls::coordinator_json(coordinator),
        controls::maintenance_json(&maintenance),
        memory.read::<8192>(r::SOURCE_OFFSET),
        memory.read::<8192>(DATA_OFFSET),
        memory.read::<16384>(r::TABLE_OFFSET)
    );
    execution?;
    for id in 0..n {
        let s: Shared = memory.read_record(id * RAM_SIZE + SHARED_OFFSET);
        if s.current_el != 4
            || s.sctlr & 0x1005 != 0x1005
            || s.actlr & 2 != 2
            || s.tcr != shadow::TCR_BOTH
            || s.status != SUCCESS
        {
            return Err(format!(
                "address owner{id} profile/status mismatch: {} detail {}",
                s.status, s.detail
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn owners_share_only_physical_inputs_and_coordination() {
        let memory = Memory::with_size(4 * RAM_SIZE).unwrap();
        for id in 0..4 {
            owner_mappings(&memory, id);
            let base = id * RAM_SIZE;
            for offset in [
                a::OWNER_OFFSET,
                a::RECORD_OFFSET,
                0x8f000,
                CODE_OFFSET,
                CODE_OFFSET + 4096,
            ] {
                let pte = u64::from_le_bytes(memory.read::<8>(base + 0x14000 + offset / 4096 * 8));
                assert_eq!(pte & 0x0000_ffff_ffff_f000, IPA + (base + offset) as u64);
            }
            for offset in [
                DATA_OFFSET,
                r::SOURCE_OFFSET,
                r::SOURCE_OFFSET + 4096,
                r::TABLE_OFFSET,
                a::SHARED_OFFSET,
                0x9f000,
            ] {
                let pte = u64::from_le_bytes(memory.read::<8>(base + 0x14000 + offset / 4096 * 8));
                assert_eq!(pte & 0x0000_ffff_ffff_f000, IPA + offset as u64);
                assert_eq!(pte & (3 << 53), 3 << 53);
            }
        }
    }
    #[test]
    fn protected32_seeds_preserve_nonzero_address_halves_and_legacy_roots() {
        for profile in 0..6 {
            let config = a::Config::new(4, profile, 2).unwrap();
            let bytes = legacy_tables(config);
            assert_eq!(&bytes[4..8], &0x2007u32.to_le_bytes());
            for id in 0..4 {
                let (f, _, g) = seed_owner(config, id);
                assert_eq!(g.control[0] >> 31, u64::from(id != 2));
                assert_eq!(g.control[3], 0x600);
                assert_eq!(g.control[4], 0);
                assert_eq!(g.gpr[3], f.x[6]);
                assert_eq!(g.gpr[6], f.x[10]);
                assert_eq!(g.segments[0], config.cs(id as u64, false));
                if profile == 3 {
                    assert_ne!(f.x[6] >> 16, 0);
                    assert_ne!(f.x[10] >> 16, 0);
                }
            }
        }
    }
}
