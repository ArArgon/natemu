//! Actual guest control instructions around freshly generated ordinary instructions.
use super::*;
use natemu_abi::{controls as control, pae, recovery as recover, shadow, validation};
use std::{
    path::PathBuf,
    time::{Duration, Instant},
};
mod producer;
const CODE_BYTES: usize = control::MAX_VISITS * control::SLOT_BYTES;
const PAE_TABLE_OFFSET: usize = pae::TABLE_OFFSET;
const PAE_TABLE_BYTES: usize = pae::TABLE_BYTES;

fn pae_tables(profile: u64) -> [u8; PAE_TABLE_BYTES] {
    let mut bytes = [0; PAE_TABLE_BYTES];
    if (13..=21).contains(&profile) {
        for (gpa, value) in [
            (
                0x1020,
                if profile == 20 {
                    0xffff_ffff_ffff_fffeu64
                } else {
                    0x2001
                },
            ),
            (0x1038, if profile == 17 { 0x21 } else { 0 }),
            (0x2000, 0x3007),
            (0x3000 + 256 * 8, 0x100007),
            (0x3000 + 258 * 8, 0x102007),
            (0x4000, 0x5007),
            (0x5000 + 256 * 8, 0x101007),
            (0x5000 + 258 * 8, 0x102007),
        ] {
            let offset = gpa - 0x1000;
            bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
        }
    }
    bytes
}

macro_rules! fields {
    ($record:ident; $($field:ident),* $(,)?) => {{
        let mut out = Vec::<String>::new();
        $(out.push(format!("\"{}\":{:?}", stringify!($field), $record.$field));)*
        out
    }};
}
fn visit_json(v: &control::Visit) -> String {
    let mut fields = fields!(v; rip,bytes,kind,source,fetch_gpa,before,after,generation_before,generation_after,gpr_before,gpr_after,flags_before,flags_after,exception,request,reply,entries,faults,gpa,memory_before,memory_after,vector,error,linear);
    fields.push(format!("\"frame\":{}", frame_json(&v.frame)));
    fields.push(format!("\"fault_frame\":{}", frame_json(&v.fault_frame)));
    format!("{{{}}}", fields.join(","))
}
fn state_json(s: &control::State) -> String {
    let mut fields = fields!(s; profile,cpl,generation,visits,completed,transfers,serial,walks,invalidations,mapping,fixture_mutated,warm_before_reload,stale_reply,stale_entry,expected,deferred,late_reply,rebound_reply,old_slot_context,old_controls);
    fields.push(format!("\"initial\":{}", frame_json(&s.initial)));
    fields.push(format!("\"current\":{}", frame_json(&s.current)));
    format!("{{{}}}", fields.join(","))
}
fn hidden_json(hidden: &pae::Hidden) -> String {
    format!("{{{}}}", fields!(hidden; entries,generation).join(","))
}
fn pae_json(state: &pae::State) -> String {
    let mut fields = fields!(state; attempts,reads,walk_reads,walk_writes,invlpg,evictions,mutations,handled_faults);
    fields.push(format!("\"hidden\":{}", hidden_json(&state.hidden)));
    let visits = state.visits.iter().map(|visit| {
        let mut fields = fields!(visit; load_addresses,attempts_before,attempts_after,reads_before,reads_after);
        fields.push(format!("\"before\":{},\"after\":{}",hidden_json(&visit.before),hidden_json(&visit.after)));
        format!("{{{}}}",fields.join(","))
    }).collect::<Vec<_>>().join(",");
    fields.push(format!("\"visits\":[{visits}]"));
    format!("{{{}}}", fields.join(","))
}
fn read_state(memory: &Memory) -> control::State {
    unsafe { ptr::read_volatile(memory.0.add(control::OFFSET).cast()) }
}
pub fn run(args: &[String]) -> Result<()> {
    if args[5] != "1" {
        return Err("control profile requires one vCPU".into());
    }
    let profile: u64 = args[4].parse().map_err(|_| "invalid control profile")?;
    let (program, program_len) = control::source(profile).ok_or("unsupported control profile")?;
    let cpl = if profile == 6 { 3 } else { 0 };
    let mut vm_nonce = 0u64;
    if unsafe { getentropy((&mut vm_nonce as *mut u64).cast(), 8) } != 0 || vm_nonce == 0 {
        return Err("unable to generate control VM identity".into());
    }
    let memory = Memory::new()?;
    load_elf(&memory, &read_input(&args[1], 2 * 1024 * 1024)?)?;
    i4v::prepare_shadow(&memory);
    for offset in (control::RECORD_OFFSET..0x90000).step_by(4096) {
        memory.word(
            0x14000 + offset / 4096 * 8,
            (IPA + offset as u64) | 3 | 1 << 10 | 3 << 8 | 3 << 53,
        );
    }
    let mut source_before = [0xcc; 4096];
    source_before[..program_len].copy_from_slice(&program[..program_len]);
    memory.copy(recover::SOURCE_OFFSET, &source_before);
    let mut data_before = [0xa5; 8192];
    data_before[4096..].fill(0x5a);
    data_before[..4].copy_from_slice(&0x11220000u32.to_le_bytes());
    data_before[4096..4100].copy_from_slice(&0x33440000u32.to_le_bytes());
    memory.copy(DATA_OFFSET, &data_before);
    let mut tables_before = [0u8; 16384];
    for (offset, word) in [
        (0, 0x2007u32),
        (
            0x1000 + 256 * 4,
            0x100000 | if profile == 2 { 5 } else { 7 },
        ),
        (0x1000 + 258 * 4, 0x102007),
        (0x2000, 0x4007),
        (0x3000 + 256 * 4, 0x101007),
        (0x3000 + 258 * 4, if profile == 7 { 0 } else { 0x102007 }),
    ] {
        tables_before[offset..offset + 4].copy_from_slice(&word.to_le_bytes());
    }
    memory.copy(recover::TABLE_OFFSET, &tables_before);
    let pae_tables_before = pae_tables(profile);
    memory.copy(PAE_TABLE_OFFSET, &pae_tables_before);
    let (mut frame, bank, _) = seed(3);
    for &native in &natemu_abi::pressure::ENTRY[..8] {
        frame.x[native] &= u32::MAX as u64;
    }
    frame.x[6] = DATA_VA;
    memory.copy(BANK_OFFSET, &bank);
    let mut guest = recover::GuestState::seed(&frame);
    for (role, &native) in natemu_abi::pressure::ENTRY[..8].iter().enumerate() {
        guest.gpr[natemu_abi::pressure::GPR_ORDER[role]] = frame.x[native];
    }
    guest.rip = control::RIP;
    guest.control[0] = 0x10011;
    guest.control[2] = if profile >= 13 { 0x1020 } else { 0x1000 };
    guest.control[3] = 0x600;
    guest.control[4] = 0;
    guest.segments[0] = [8 | cpl, 0, 0xffff_ffff, 0xc09b | (cpl << 5)];
    for segment in &mut guest.segments[1..] {
        *segment = [0x10 | cpl, 0, 0xffff_ffff, 0xc093 | (cpl << 5)];
    }
    let mut recovery_state = recover::Recovery::new(0);
    recovery_state.native_fpcr = frame.fpcr;
    recovery_state.native_fpsr = frame.fpsr;
    recovery_state.control.nxe = 0;
    let mut state = control::State::ZERO;
    state.profile = profile;
    state.cpl = cpl;
    state.generation = 1;
    state.initial = frame;
    state.current = frame;
    state.old_controls = guest.control;
    let validation_state = validation::State {
        guest_rip: control::RIP,
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
    let mut object = Object::new(LIVE_LOAD);
    object.code_length = CODE_BYTES as u64;
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
    let code_before = memory.read::<CODE_BYTES>(CODE_OFFSET);
    let lower_leaf_before = i4v::words(&memory, shadow::LOWER_LEAF);
    unsafe {
        ptr::write(memory.0.add(recover::GUEST_OFFSET).cast(), guest);
        ptr::write(memory.0.add(recover::STATE_OFFSET).cast(), recovery_state);
        ptr::write(
            memory.0.add(validation::STATE_OFFSET).cast(),
            validation_state,
        );
        ptr::write(memory.0.add(control::OFFSET).cast(), state);
        ptr::write(memory.0.add(pae::OFFSET).cast(), pae::State::ZERO);
        ptr::write(memory.0.add(SHARED_OFFSET).cast(), shared);
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
    let output = PathBuf::from(&args[7]);
    fs::create_dir(&output).map_err(|e| e.to_string())?;
    let mut compiler =
        producer::Compiler::new(PathBuf::from(&args[3]), PathBuf::from(&args[6]), output);
    let started = Instant::now();
    loop {
        if started.elapsed() > Duration::from_secs(110) {
            return Err("control compiler service deadline exceeded".into());
        }
        let event = vm.run_any()?;
        if event == 0x49 {
            break;
        }
        if event != 0x4a {
            return Err(format!("unexpected control event {event:#x}"));
        }
        let shared = vm.shared();
        if !natemu_abi::resident::contains(shared.service_pc, 4)
            || vm.read_reg(31)? != shared.service_pc
        {
            return Err("control service outside resident boundary".into());
        }
        let mut packet = [0; 16];
        for (i, word) in packet.iter_mut().enumerate() {
            *word = vm.read_reg(i as u32)?;
        }
        if packet != read_state(&vm.memory).expected {
            return Err("control service request differs from stored identity".into());
        }
        let reply = compiler.exchange(packet)?;
        for (i, word) in reply.into_iter().enumerate() {
            vm.reg(i as u32, word)?;
        }
    }
    let pae_state =
        unsafe { ptr::read_volatile(vm.memory.0.add(pae::OFFSET).cast::<pae::State>()) };
    let result = vm.shared();
    let state = read_state(&vm.memory);
    let v = unsafe {
        ptr::read_volatile(
            vm.memory
                .0
                .add(validation::STATE_OFFSET)
                .cast::<validation::State>(),
        )
    };
    let guest_after = unsafe {
        ptr::read_volatile(
            vm.memory
                .0
                .add(recover::GUEST_OFFSET)
                .cast::<recover::GuestState>(),
        )
    };
    let visits = (0..control::MAX_VISITS)
        .map(|i| {
            let visit = unsafe {
                ptr::read_volatile(
                    vm.memory
                        .0
                        .add(control::RECORD_OFFSET + i * core::mem::size_of::<control::Visit>())
                        .cast::<control::Visit>(),
                )
            };
            visit_json(&visit)
        })
        .collect::<Vec<_>>()
        .join(",");
    let ttbr1 = vm.read_sys(0xc101)?;
    let mut report = fields!(result; status,detail,current_el,sctlr,actlr,tcr,service_pc,capture_pc,esr,far,elr,spsr);
    report.extend(fields!(v; live,requests,request,reply,completed,operand,width,stage));
    macro_rules! add {
        ($name:literal,$value:expr) => {
            report.push(format!(concat!("\"", $name, "\":{:?}"), $value));
        };
    }
    add!("case", "controls32");
    add!("profile", profile);
    add!("rip", control::RIP);
    add!("vm_nonce", vm_nonce);
    add!("ttbr1", ttbr1);
    add!("compiler_requests", compiler.requests);
    add!("source_before", source_before);
    add!(
        "source_after",
        vm.memory.read::<4096>(recover::SOURCE_OFFSET)
    );
    add!("data_before", data_before);
    add!("data_after", vm.memory.read::<8192>(DATA_OFFSET));
    add!("pae_tables_before", pae_tables_before);
    add!(
        "pae_tables_after",
        vm.memory.read::<PAE_TABLE_BYTES>(PAE_TABLE_OFFSET)
    );
    add!("tables_before", tables_before);
    add!(
        "tables_after",
        vm.memory.read::<16384>(recover::TABLE_OFFSET)
    );
    add!("code_before", code_before);
    add!("code_after", vm.memory.read::<CODE_BYTES>(CODE_OFFSET));
    add!("lower_leaf_before", lower_leaf_before);
    add!(
        "lower_leaf_after",
        i4v::words(&vm.memory, shadow::LOWER_LEAF)
    );
    add!("bank_before", bank);
    report.push(format!("\"controls\":{},\"visits\":[{}],\"initial\":{},\"entered\":{},\"final_frame\":{},\"guest_before\":{},\"guest_after\":{},\"recovery\":{}",state_json(&state),visits,frame_json(&frame),frame_json(&result.initial),frame_json(&result.captured),recovery::guest_json(&guest),recovery::guest_json(&guest_after),recovery::report(&vm,&guest,&tables_before)));
    report.push(format!("\"pae\":{}", pae_json(&pae_state)));
    println!("{{{}}}", report.join(","));
    if result.current_el != 4
        || result.sctlr & 0x1005 != 0x1005
        || result.actlr & 2 != 2
        || result.tcr != shadow::TCR_BOTH
        || ttbr1 != IPA + shadow::UPPER_ROOT as u64
    {
        return Err("control runtime machine profile mismatch".into());
    }
    if result.status == 0 || result.status == PANIC {
        return Err(format!(
            "control resident failure status={} detail={}",
            result.status, result.detail
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pae_fixture_uses_distinct_pages_and_nonzero_pdpt_offset() {
        let bytes = pae_tables(13);
        let word = |gpa: usize| {
            u64::from_le_bytes(bytes[gpa - 0x1000..gpa - 0x1000 + 8].try_into().unwrap())
        };
        assert_eq!(word(0x1000), 0);
        assert_eq!(word(0x1020), 0x2001);
        assert_eq!(word(0x2000), 0x3007);
        assert_eq!(word(0x4000), 0x5007);
        assert_eq!(word(0x3000 + 256 * 8), 0x100007);
        assert_eq!(word(0x5000 + 256 * 8), 0x101007);
        assert_eq!(word(0x3000 + 258 * 8), word(0x5000 + 258 * 8));
        assert!(PAE_TABLE_OFFSET + PAE_TABLE_BYTES <= recover::TABLE_OFFSET);
        assert_eq!(pae_tables(12), [0; PAE_TABLE_BYTES]);
        let invalid = pae_tables(17);
        assert_eq!(
            u64::from_le_bytes(invalid[0x38..0x40].try_into().unwrap()),
            0x21
        );
        let absent = pae_tables(20);
        assert_eq!(
            u64::from_le_bytes(absent[0x20..0x28].try_into().unwrap()),
            0xffff_ffff_ffff_fffe
        );
    }
}
