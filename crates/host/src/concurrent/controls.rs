//! Concurrent control programs share RAM while retaining owner-private execution state.
use super::*;
use crate::i4v_controls as single;
use natemu_abi::{controls as control, modes, pae, recovery as recover, validation};

fn owner_mappings(memory: &Memory, id: usize) {
    let base = id * RAM_SIZE;
    tables(memory, base, true);
    mixed::private_code(memory, base);
    let attrs = 3 | 1 << 10 | 3 << 8 | 3 << 53;
    for offset in (control::RECORD_OFFSET..0x90000).step_by(4096) {
        memory.word(
            base + 0x14000 + offset / 4096 * 8,
            (IPA + (base + offset) as u64) | attrs,
        );
    }
    for offset in (0x93000..0xa0000).step_by(4096) {
        memory.word(
            base + 0x14000 + offset / 4096 * 8,
            (IPA + offset as u64) | attrs,
        );
    }
    memory.word(base + shadow::LOWER_LEAF + 256 * 8, 0);
}

fn legacy_tables() -> [u8; 16384] {
    let mut bytes = [0; 16384];
    for (offset, word) in [
        (0, 0x2007u32),
        (0x1000 + 256 * 4, 0x100007),
        (0x1000 + 258 * 4, 0x102007),
        (0x2000, 0x4007),
        (0x3000 + 256 * 4, 0x101007),
        (0x3000 + 258 * 4, 0x102007),
    ] {
        bytes[offset..offset + 4].copy_from_slice(&word.to_le_bytes());
    }
    bytes
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn relocated_owner_programs_and_metadata_remain_disjoint_and_mirrored() {
        let mut pages = [0xa5; 8192];
        pages[4096..].fill(0x5a);
        for id in 0..4 {
            let state = mode_fixture(22, id, &mut pages);
            assert_eq!(state.gdtr, [cc::gdt(id as u64), 0x37]);
            let start = (state.gdtr[0] - DATA_VA) as usize;
            assert_eq!(
                &pages[start..start + 0x38],
                &pages[4096 + start..4096 + start + 0x38]
            );
            assert_eq!(
                &pages[start + 0x40..start + 0x4a],
                &pages[4096 + start + 0x40..4096 + start + 0x4a]
            );
            let (source, len) = cc::source_bytes(22, id as u64, 1).unwrap();
            assert!(len < 256);
            let mut at = cc::source(id as u64);
            let (instructions, count) = cc::program(22).unwrap();
            for instruction in &instructions[..count] {
                let offset = (at - cc::source(id as u64)) as usize;
                assert!(cc::instruction(22, id as u64, 1, at).is_some());
                if instruction.bytes[0] == 0xea {
                    let target =
                        u32::from_le_bytes(source[offset + 1..offset + 5].try_into().unwrap())
                            as u64;
                    assert!((cc::source(id as u64)..cc::source(id as u64) + 256).contains(&target));
                }
                at += instruction.len as u64;
            }
            let (frame, _, guest) = seed_owner(22, id);
            assert_eq!(frame.x[6], cc::data(id as u64));
            assert_eq!(frame.x[10], cc::pointer(id as u64));
            assert_eq!(guest.rip, cc::source(id as u64));
            assert_ne!(guest.rflags & 0x200, 0);
        }
    }
    #[test]
    fn controls_own_code_visits_and_context_but_share_only_guest_ram_and_coordination() {
        let memory = Memory::with_size(4 * RAM_SIZE).unwrap();
        for id in 0..4 {
            owner_mappings(&memory, id);
            let base = id * RAM_SIZE;
            let leaf = |offset| {
                u64::from_le_bytes(memory.read::<8>(base + 0x14000 + offset / 4096 * 8))
                    & 0x0000_ffff_ffff_f000
            };
            for offset in [
                CODE_OFFSET,
                CODE_OFFSET + 4096,
                control::OFFSET,
                pae::OFFSET,
                modes::OFFSET,
                0x26000,
                0x27000,
                control::RECORD_OFFSET,
                0x8f000,
            ] {
                assert_eq!(leaf(offset), IPA + (base + offset) as u64);
            }
            for offset in [
                DATA_OFFSET,
                recover::SOURCE_OFFSET,
                recover::TABLE_OFFSET,
                pae::TABLE_OFFSET,
                modes::TABLE_OFFSET,
                COORD_OFFSET,
                0x93000,
                0x9f000,
            ] {
                assert_eq!(leaf(offset), IPA + offset as u64);
            }
            assert_eq!(
                memory.read::<8>(base + shadow::LOWER_LEAF + 256 * 8),
                [0; 8]
            );
            let operand = cc::data(id as u64);
            assert_eq!(
                shadow::leaf_offset(operand & !4095),
                Some(shadow::LOWER_LEAF + 256 * 8)
            );
            assert_eq!(shadow::leaf_offset(operand).is_some(), id == 0);
        }
    }
}

use natemu_abi::{control_concurrent as cc, maintenance as maintenance_abi};
const CODE_BYTES: usize = control::MAX_VISITS * control::SLOT_BYTES;
macro_rules! fields {
    ($record:ident; $($field:ident),* $(,)?) => {{
        let mut out=Vec::<String>::new();
        $(out.push(format!("\"{}\":{:?}",stringify!($field),$record.$field));)*
        out
    }};
}
fn record<T: Copy>(memory: &Memory, offset: usize) -> T {
    unsafe { ptr::read_volatile(memory.0.add(offset).cast()) }
}
fn snapshot_json(s: &cc::Snapshot) -> String {
    format!("{{{}}}",fields!(s; control,cs,tr,hidden,hidden_generation,context,source_generation,rip,completed,gpr,route).join(","))
}
fn owner_json(s: &cc::Owner) -> String {
    let mut f = fields!(s; enabled,ttbr1,incarnation,source_generation,admitted,route,writable,detached,local_cut,peer_reply_cut,peer_reply_executed,local_controls,source_rejections,cached_before_source,source_entry_rejected,binding_rejections,retained_code,reuse_blocked,released_code,reclaimed,maintenance_intervals, maintenance_acks, interval_visits, semantic_admissions,source_expected,source_reply,peer_expected,peer_reply,metadata_gpas,code_before_retire,code_after_reclaim);
    for (name, value) in [
        ("initial", &s.initial),
        ("before_local", &s.before_local),
        ("after_local", &s.after_local),
        ("before_peer_reply", &s.before_peer_reply),
        ("after_peer_reply", &s.after_peer_reply),
    ] {
        f.push(format!("\"{name}\":{}", snapshot_json(value)));
    }
    format!("{{{}}}", f.join(","))
}
fn concurrent_json(s: &cc::Shared) -> String {
    let mut f = fields!(s; n,profile,actor,phase,ready,held,local_done,drained,peer_compiling,second_commit,peer_executed,final_ready,source_generation,source_acknowledgements,finished,pae_ready,pae_acknowledgements,pae_published, semantic_active, maintenance_started, maintenance_leaves);
    for (name, values) in [("before", &s.before), ("after", &s.after)] {
        f.push(format!(
            "\"{name}\":[{}]",
            values
                .iter()
                .map(snapshot_json)
                .collect::<Vec<_>>()
                .join(",")
        ));
    }
    format!("{{{}}}", f.join(","))
}
pub(super) fn coordinator_json(c: &Coordinator) -> String {
    let mut f = fields!(c; participants,scenario,alias_profile,stack_profile,mixed_profile);
    macro_rules! atomic {($($name:ident),*)=>{$(f.push(format!("\"{}\":{}",stringify!($name),c.$name.load(Ordering::Acquire)));)*};}
    atomic!(
        mixed_compiled,
        ready,
        finished,
        lock,
        next_ticket,
        phase,
        active,
        queued,
        head,
        length,
        servicing,
        completed,
        retained,
        generation,
        acknowledgements,
        new_entries,
        new_completed,
        new_references,
        reclaimed,
        old_eligible,
        published,
        stale_replies,
        stale_entries,
        reuse_blocked,
        translation_wait,
        trace_length
    );
    for (name, values) in [
        ("queue", &c.queue),
        ("event_pending", &c.event_pending),
        ("event_notifications", &c.event_notifications),
    ] {
        f.push(format!(
            "\"{name}\":{:?}",
            values
                .iter()
                .map(|v| v.load(Ordering::Acquire))
                .collect::<Vec<_>>()
        ));
    }
    f.push(format!(
        "\"trace\":{:?}",
        c.trace[..(c.trace_length.load(Ordering::Acquire) as usize).min(TRACE_CAPACITY)]
            .iter()
            .map(|v| v.load(Ordering::Acquire))
            .collect::<Vec<_>>()
    ));
    format!("{{{}}}", f.join(","))
}
pub(super) fn maintenance_json(s: &maintenance_abi::State) -> String {
    let mut f = fields!(s; n,ready,serial,head,length,pending,submitted,completed,epoch,acknowledgements,total,batch,next_ordinary,ordinary,intervals,ordinary_total,closing,finished,duplicate_rejections,maximum_depth,leaf_history,compose_leaf);
    let request =
        |r: &maintenance_abi::Request| format!("{{\"cpu\":{},\"serial\":{}}}", r.cpu, r.serial);
    f.push(format!("\"current\":{}", request(&s.current)));
    for (name, values) in [
        ("queue", &s.queue[..]),
        ("request_history", &s.request_history[..]),
    ] {
        f.push(format!(
            "\"{name}\":[{}]",
            values.iter().map(request).collect::<Vec<_>>().join(",")
        ));
    }
    format!("{{{}}}", f.join(","))
}
fn interval_json(s: &maintenance_abi::Interval) -> String {
    let mut f = fields!(s; ordinal,heads,epoch,gpa,before,after,faults,esr,far,pc,spsr);
    for (name, frame) in [
        ("initial", &s.initial),
        ("captured", &s.captured),
        ("dirty_frame", &s.dirty_frame),
    ] {
        f.push(format!("\"{name}\":{}", frame_json(frame)));
    }
    format!("{{{}}}", f.join(","))
}
fn mode_fixture(profile: u64, id: usize, data: &mut [u8; 8192]) -> modes::State {
    let mut s = modes::State::ZERO;
    if profile < 22 {
        return s;
    }
    s.gdtr = [cc::gdt(id as u64), 0x37];
    s.tr = [0x28, 0, 0x67, 0x8b];
    s.pointer = cc::pointer(id as u64);
    let mut target = cc::source(id as u64);
    let mut at = target;
    let (program, count) = cc::program(profile).unwrap();
    for instruction in &program[..count] {
        if instruction.bytes[..instruction.len] == [0x48, 0xff, 0x2e] {
            target = at + instruction.len as u64;
            break;
        }
        at += instruction.len as u64;
    }
    for page in [0, 4096] {
        let gdt = page + (s.gdtr[0] - DATA_VA) as usize;
        let pointer = page + (s.pointer - DATA_VA) as usize;
        data[gdt..gdt + 0x38].fill(0);
        for (offset, value) in [
            (8, 0x00cf9b000000ffffu64),
            (0x10, 0x00cf93000000ffff),
            (0x18, 0x00af9b000000ffff),
            (0x28, 0x00008b0000000067),
        ] {
            data[gdt + offset..gdt + offset + 8].copy_from_slice(&value.to_le_bytes());
        }
        data[pointer..pointer + 8].copy_from_slice(&target.to_le_bytes());
        data[pointer + 8..pointer + 10].copy_from_slice(&8u16.to_le_bytes());
    }
    s
}
fn seed_owner(profile: u64, id: usize) -> (NativeFrame, [u8; 512], recover::GuestState) {
    let (mut f, mut bank, _) = seed(id);
    for &native in &natemu_abi::pressure::ENTRY[..8] {
        f.x[native] &= u32::MAX as u64;
    }
    f.x[6] = cc::data(id as u64);
    if profile >= 22 {
        f.x[7] = 0xc0000080;
        f.x[5] = 0;
        f.x[10] = cc::pointer(id as u64);
    }
    for b in &mut bank {
        *b ^= id as u8;
    }
    let mut g = recover::GuestState::seed(&f);
    for (role, &native) in natemu_abi::pressure::ENTRY[..8].iter().enumerate() {
        g.gpr[natemu_abi::pressure::GPR_ORDER[role]] = f.x[native];
    }
    g.rip = cc::source(id as u64);
    g.control[0] = 0x10011;
    g.control[2] = if profile >= 13 { pae::ROOT } else { 0x1000 };
    g.control[3] = 0x600;
    g.control[4] = 0;
    if profile == 24 {
        g.control[2] = modes::ROOT;
        g.control[3] = 0x620;
        g.control[4] = 0x100;
    }
    g.segments[0] = [8, 0, 0xffff_ffff, 0xc09b];
    for segment in &mut g.segments[1..] {
        *segment = [0x10, 0, 0xffff_ffff, 0xc093];
    }
    (f, bank, g)
}
fn owner_report(
    memory: &Memory,
    id: usize,
    seed: &(NativeFrame, [u8; 512], recover::GuestState),
    requests: &[wire::Packet],
) -> String {
    let base = id * RAM_SIZE;
    let shared: Shared = record(memory, base + SHARED_OFFSET);
    let state: control::State = record(memory, base + control::OFFSET);
    let validation: validation::State = record(memory, base + validation::STATE_OFFSET);
    let owner: cc::Owner = record(memory, base + cc::OWNER_OFFSET);
    let pae: pae::State = record(memory, base + pae::OFFSET);
    let modes: modes::State = record(memory, base + modes::OFFSET);
    let guest: recover::GuestState = record(memory, base + recover::GUEST_OFFSET);
    let recovery: recover::Recovery = record(memory, base + recover::STATE_OFFSET);
    let event: EventRecord = record(memory, base + EVENT_OFFSET);
    let mut f = fields!(shared; status,detail,current_el,sctlr,actlr,tcr,service_pc,capture_pc,esr,far,elr,spsr);
    f.extend(fields!(validation; live,requests,request,reply,completed,operand,width,stage));
    macro_rules! add {
        ($name:literal,$value:expr) => {
            f.push(format!(concat!("\"", $name, "\":{:?}"), $value));
        };
    }
    add!("id", id);
    add!("case", "controls32");
    add!("profile", state.profile);
    add!("rip", cc::source(id as u64));
    add!("vm_nonce", shared.lifecycle.vm);
    add!(
        "compiler_requests",
        requests
            .iter()
            .filter(|p| p[3] == id as u64)
            .copied()
            .collect::<Vec<_>>()
    );
    add!("bank_before", seed.1);
    add!("bank_after", memory.read::<512>(base + BANK_OFFSET));
    add!("code_before", [0u8; CODE_BYTES]);
    add!("code_after", memory.read::<CODE_BYTES>(base + CODE_OFFSET));
    add!(
        "lower_leaf_after",
        i4v::words(memory, base + shadow::LOWER_LEAF)
    );
    add!(
        "upper_root_after",
        i4v::words(memory, base + shadow::UPPER_ROOT)
    );
    add!(
        "upper_leaf_after",
        i4v::words(memory, base + shadow::UPPER_LEAF)
    );
    f.push(format!("\"initial\":{},\"entered\":{},\"final_frame\":{},\"guest_before\":{},\"guest_after\":{},\"controls\":{},\"pae\":{},\"modes\":{},\"owner\":{}",frame_json(&seed.0),frame_json(&shared.initial),frame_json(&shared.captured),crate::recovery::guest_json(&seed.2),crate::recovery::guest_json(&guest),single::state_json(&state),single::pae_json(&pae),single::modes_json(&modes),owner_json(&owner)));
    let visits = (0..control::MAX_VISITS)
        .map(|i| {
            single::visit_json(&record(
                memory,
                base + control::RECORD_OFFSET + i * core::mem::size_of::<control::Visit>(),
            ))
        })
        .collect::<Vec<_>>()
        .join(",");
    f.push(format!("\"visits\":[{visits}]"));
    let mut r = fields!(recovery; scenario,native_fpcr,native_fpsr,faults,walks,retries,vector,error,linear,rip);
    r.push(format!(
        "\"control\":{:?}",
        [
            recovery.control.cr3,
            recovery.control.cpl,
            recovery.control.wp,
            recovery.control.nxe,
            recovery.control.smep
        ]
    ));
    let traces=recovery.trace[..(recovery.faults as usize).min(recover::MAX_FAULTS)].iter().map(|t|format!("{{\"frame\":{},\"esr\":{},\"far\":{},\"elr\":{},\"spsr\":{},\"outcome\":{},\"error\":{}}}",frame_json(&t.frame),t.esr,t.far,t.elr,t.spsr,t.outcome,t.error)).collect::<Vec<_>>().join(",");
    r.push(format!("\"traces\":[{traces}]"));
    f.push(format!("\"recovery\":{{{}}}", r.join(",")));
    let mut e = fields!(event; consumed,deliveries,boundary,blocked,wait_checks,shadow,priority);
    e.push(format!(
        "\"packet\":{}",
        crate::recovery::guest_json(&event.packet)
    ));
    f.push(format!("\"event\":{{{}}}", e.join(",")));
    let intervals = (0..maintenance_abi::MAX_INTERVALS)
        .map(|i| {
            interval_json(&record(
                memory,
                base + maintenance_abi::RECORD_OFFSET + i * 4096,
            ))
        })
        .collect::<Vec<_>>()
        .join(",");
    f.push(format!("\"intervals\":[{intervals}]"));
    format!("{{{}}}", f.join(","))
}
pub fn run(args: &[String]) -> Result<()> {
    let profile: u64 = args[4]
        .parse()
        .map_err(|_| "invalid concurrent control profile")?;
    let n: usize = args[5].parse().map_err(|_| "invalid control vCPU count")?;
    let actor: usize = args[6].parse().map_err(|_| "invalid control actor")?;
    let concurrent = cc::Shared::new(n, profile, actor)
        .ok_or("unsupported concurrent controls/profile/count/actor")?;
    let mut coordinator = Coordinator::new(n).unwrap();
    coordinator.scenario = 5;
    let mut host_limit = 0;
    unsafe {
        hv(
            hv_vm_get_max_vcpu_count(&mut host_limit),
            "read control vCPU limit",
        )?;
    }
    if n > host_limit as usize {
        return Err("control count exceeds host vCPU limit".into());
    }
    let memory = Memory::with_size(super::progress::backing_size(n))?;
    load_elf(&memory, &read_input(&args[1], 2 * 1024 * 1024)?)?;
    let mut vm_nonce = 0u64;
    if unsafe { getentropy((&mut vm_nonce as *mut u64).cast(), 8) } != 0 || vm_nonce == 0 {
        return Err("unable to generate concurrent control VM identity".into());
    }
    let mut data_before = [0xa5; 8192];
    data_before[4096..].fill(0x5a);
    let mut source_before = [0xcc; 4096];
    let mut seeds = Vec::new();
    for id in 0..n {
        let base = id * RAM_SIZE;
        if id != 0 {
            for offset in (0..RAM_SIZE).step_by(4096) {
                if resident::executable_offset(offset) {
                    memory.copy(base + offset, &memory.read::<4096>(offset));
                }
            }
        }
        owner_mappings(&memory, id);
        let (source, len) = cc::source_bytes(profile, id as u64, 1).unwrap();
        source_before[id * 256..id * 256 + len].copy_from_slice(&source[..len]);
        data_before[id * 256..id * 256 + 4]
            .copy_from_slice(&(0x11220000u32 + id as u32).to_le_bytes());
        data_before[4096 + id * 256..4100 + id * 256]
            .copy_from_slice(&(0x33440000u32 + id as u32).to_le_bytes());
        let modes = mode_fixture(profile, id, &mut data_before);
        let seeded = seed_owner(profile, id);
        let (frame, bank, guest) = seeded;
        memory.copy(base + BANK_OFFSET, &bank);
        let mut object = Object::new(LIVE_LOAD);
        object.code_length = CODE_BYTES as u64;
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
            mode: validation::MODE,
            lifecycle: wire::Lifecycle::new(vm_nonce),
        };
        let mut state = control::State::ZERO;
        state.profile = profile;
        state.generation = 1;
        state.initial = frame;
        state.current = frame;
        state.old_controls = guest.control;
        let v = validation::State {
            guest_rip: guest.rip,
            case: control::CASE,
            completed: 0,
            operand: 0,
            width: 0,
            stage: 0,
            live: 1,
            requests: 0,
            request: [0; 16],
            reply: [0; 16],
        };
        let mut recovery = recover::Recovery::new(0);
        recovery.native_fpcr = frame.fpcr;
        recovery.native_fpsr = frame.fpsr;
        recovery.control.nxe = 0;
        let mut event = EventRecord::new(&frame, id);
        event.packet = guest;
        unsafe {
            ptr::write(memory.0.add(base + SHARED_OFFSET).cast(), shared);
            ptr::write(memory.0.add(base + control::OFFSET).cast(), state);
            ptr::write(memory.0.add(base + validation::STATE_OFFSET).cast(), v);
            ptr::write(memory.0.add(base + recover::GUEST_OFFSET).cast(), guest);
            ptr::write(memory.0.add(base + recover::STATE_OFFSET).cast(), recovery);
            ptr::write(memory.0.add(base + pae::OFFSET).cast(), pae::State::ZERO);
            ptr::write(memory.0.add(base + modes::OFFSET).cast(), modes);
            ptr::write(
                memory.0.add(base + cc::OWNER_OFFSET).cast(),
                cc::Owner::new(),
            );
            ptr::write(memory.0.add(base + ROUND_OFFSET).cast(), Round::ZERO);
            ptr::write(memory.0.add(base + EVENT_OFFSET).cast(), event);
        }
        seeds.push(seeded);
    }
    let tables_before = legacy_tables();
    let pae_tables_before = single::pae_tables(profile);
    let mode_tables_before = single::mode_tables(profile);
    memory.copy(DATA_OFFSET, &data_before);
    memory.copy(recover::SOURCE_OFFSET, &source_before);
    memory.copy(recover::TABLE_OFFSET, &tables_before);
    memory.copy(pae::TABLE_OFFSET, &pae_tables_before);
    memory.copy(modes::TABLE_OFFSET, &mode_tables_before);
    unsafe {
        ptr::write(memory.0.add(COORD_OFFSET).cast(), coordinator);
        ptr::write(memory.0.add(cc::SHARED_OFFSET).cast(), concurrent);
        ptr::write(
            memory.0.add(maintenance_abi::OFFSET).cast(),
            maintenance_abi::State::new(n).unwrap(),
        );
    }
    fs::create_dir(&args[8]).map_err(|e| e.to_string())?;
    unsafe {
        sys_icache_invalidate(memory.0.cast(), memory.1);
        hv(hv_vm_create(ptr::null()), "create concurrent control VM")?;
    }
    let _machine = Machine;
    unsafe {
        hv(
            hv_vm_map(memory.0.cast(), IPA, memory.1, 7),
            "map concurrent control backing",
        )?;
    }
    let execution = run_owners(
        n,
        shadow::TCR_BOTH,
        0,
        memory.0 as usize,
        None,
        0,
        Some((
            args[3].clone().into(),
            args[7].clone().into(),
            args[8].clone().into(),
        )),
        None,
        None,
    );
    let (requests, containment_error) = match execution {
        Ok(requests) => (requests, None),
        Err(error) if containment::configuration().is_some() => (Vec::new(), Some(error)),
        Err(error) => {
            // run_owners has joined every owner; failure snapshots cannot race native writes.
            let concurrent: cc::Shared = record(&memory, cc::SHARED_OFFSET);
            let pressure: maintenance_abi::State = record(&memory, maintenance_abi::OFFSET);
            let coordinator = unsafe { &*memory.0.add(COORD_OFFSET).cast::<Coordinator>() };
            eprintln!("concurrent failure state: {}", concurrent_json(&concurrent));
            eprintln!(
                "coordinator failure state: {}",
                coordinator_json(coordinator)
            );
            eprintln!("maintenance failure state: {}", maintenance_json(&pressure));
            for id in 0..n {
                let shared: Shared = record(&memory, id * RAM_SIZE + SHARED_OFFSET);
                let state: control::State = record(&memory, id * RAM_SIZE + control::OFFSET);
                let owner: cc::Owner = record(&memory, id * RAM_SIZE + cc::OWNER_OFFSET);
                eprintln!(
                    "owner {id} status={} detail={} visits={} completed={} local_cut={} peer_reply_cut={} peer_reply_executed={} admitted={} detached={}",
                    shared.status,
                    shared.detail,
                    state.visits,
                    state.completed,
                    owner.local_cut,
                    owner.peer_reply_cut,
                    owner.peer_reply_executed,
                    owner.admitted,
                    owner.detached
                );
            }
            return Err(error);
        }
    };
    let owners = (0..n)
        .map(|id| owner_report(&memory, id, &seeds[id], &requests))
        .collect::<Vec<_>>()
        .join(",");
    let concurrent: cc::Shared = record(&memory, cc::SHARED_OFFSET);
    let maintenance: maintenance_abi::State = record(&memory, maintenance_abi::OFFSET);
    let coordinator = unsafe { &*memory.0.add(COORD_OFFSET).cast::<Coordinator>() };
    let program_revision = cc::PROGRAM_REVISION;
    println!(
        "{{\"case\":\"controls-concurrent\",\"program_revision\":{program_revision},\"profile\":{profile},\"configured_vcpus\":{n},\"tested_vcpus\":{n},\"actor\":{actor},\"vm_nonce\":{vm_nonce},\"concurrent\":{},\"coordinator\":{},\"maintenance\":{},\"owners\":[{owners}],\"source_before\":{source_before:?},\"source_after\":{:?},\"data_before\":{data_before:?},\"data_after\":{:?},\"tables_before\":{tables_before:?},\"tables_after\":{:?},\"pae_tables_before\":{pae_tables_before:?},\"pae_tables_after\":{:?},\"mode_tables_before\":{mode_tables_before:?},\"mode_tables_after\":{:?}}}",
        concurrent_json(&concurrent),
        coordinator_json(coordinator),
        maintenance_json(&maintenance),
        memory.read::<4096>(recover::SOURCE_OFFSET),
        memory.read::<8192>(DATA_OFFSET),
        memory.read::<16384>(recover::TABLE_OFFSET),
        memory.read::<{ pae::TABLE_BYTES }>(pae::TABLE_OFFSET),
        memory.read::<{ modes::TABLE_BYTES }>(modes::TABLE_OFFSET)
    );
    if containment::configuration().is_some() {
        if let Err(error) = containment::destroy(_machine) {
            eprintln!("secondary containment destruction failure: {error}");
        }
        return Err(
            containment_error.unwrap_or_else(|| "containment unexpectedly completed".into())
        );
    }
    for id in 0..n {
        let s: Shared = record(&memory, id * RAM_SIZE + SHARED_OFFSET);
        if s.current_el != 4
            || s.sctlr & 0x1005 != 0x1005
            || s.actlr & 2 != 2
            || s.tcr != shadow::TCR_BOTH
        {
            return Err(format!("control owner{id} machine profile mismatch"));
        }
        if s.status != SUCCESS {
            return Err(format!(
                "control owner{id} failed status={} detail={}",
                s.status, s.detail
            ));
        }
    }
    Ok(())
}
