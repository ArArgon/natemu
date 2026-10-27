//! Generated live dependencies cross one local fence completion boundary.
use super::*;
use natemu_abi::fence as f;
macro_rules! fields {
    ($s:ident; $($field:ident),* $(,)?) => {{
        let mut out = Vec::<String>::new();
        $(out.push(format!("\"{}\":{:?}", stringify!($field), $s.$field));)* out
    }};
}
fn trial_json(t: &f::Trial) -> String {
    let mut out = fields!(t;epoch,case,completed,fence_completed,sink_count,sink_output,mxcsr_before,mxcsr,actlr,continuation);
    for (name, frame) in [
        ("initial", &t.initial),
        ("sink", &t.sink),
        ("captured", &t.captured),
    ] {
        out.push(format!("\"{name}\":{}", frame_json(frame)));
    }
    format!("{{{}}}", out.join(","))
}
fn owner_json(memory: &Memory, id: usize, bank: &[u8; 512], requests: &[wire::Packet]) -> String {
    let base = id * RAM_SIZE;
    let s: Shared = memory.read_record(base + SHARED_OFFSET);
    let owner = unsafe { &*memory.0.add(base + f::OWNER_OFFSET).cast::<f::Owner>() };
    let mut out = fields!(owner;id,trial,completed,expected,requests,replies,transfers,walks,table_reads,table_writes,routes,code,code_length,sink_pc,return_pc);
    out.extend(fields!(s;status,detail,current_el,sctlr,actlr,tcr,capture_pc,service_pc));
    for (name, frame) in [
        ("initial", &owner.initial),
        ("current", &owner.current),
        ("captured", &s.captured),
    ] {
        out.push(format!("\"{name}\":{}", frame_json(frame)));
    }
    out.push(format!("\"bank_before\":{bank:?},\"bank_after\":{:?},\"published_code\":{:?},\"final_native_leaf\":{:?},\"compiler_requests\":{:?}",memory.read::<512>(base+BANK_OFFSET),memory.read::<{f::CODE_BYTES}>(base+CODE_OFFSET),i4v::words(memory,base+shadow::LOWER_LEAF),requests.iter().filter(|p|p[3]==id as u64).collect::<Vec<_>>()));
    let trials = (0..f::TRIALS)
        .map(|i| {
            trial_json(unsafe {
                &*memory
                    .0
                    .add(base + f::RECORD_OFFSET + i * core::mem::size_of::<f::Trial>())
                    .cast::<f::Trial>()
            })
        })
        .collect::<Vec<_>>()
        .join(",");
    out.push(format!("\"trials\":[{trials}]"));
    format!("{{{}}}", out.join(","))
}
pub fn run(args: &[String]) -> Result<()> {
    let case = args[4].parse().map_err(|_| "invalid fence case")?;
    let n: usize = args[5].parse().map_err(|_| "invalid fence vCPU count")?;
    let config = f::Config::new(n as u64, case).ok_or("unsupported fence configuration")?;
    require_vcpus(n)?;
    let memory = Memory::with_size((n + 1) * RAM_SIZE)?;
    load_resident(&memory, &args[1])?;
    let vm = new_vm_nonce()?;
    let mut banks = Vec::new();
    let mut source_before = [0xcc; 4096];
    for id in 0..n {
        let base = id * RAM_SIZE;
        if id != 0 {
            memory.replicate_resident_text(base);
        }
        ordering::owner_mappings(&memory, id, n);
        let (mut frame, mut bank, _) = seed(id);
        for b in &mut bank {
            *b ^= id as u8;
        }
        frame.x[11] = f::RESULTS + id as u64 * 128;
        frame.fpcr = 0;
        frame.fpsr = 0;
        memory.copy(base + BANK_OFFSET, &bank);
        let mut object = Object::new(LIVE_LOAD);
        object.code_length = f::CODE_BYTES as u64;
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
            mode: f::MODE,
            lifecycle: wire::Lifecycle::new(vm),
        };
        let mut owner = f::Owner::ZERO;
        owner.id = id as u64;
        owner.initial = frame;
        memory.write_record(base + SHARED_OFFSET, shared);
        memory.write_record(base + f::OWNER_OFFSET, owner);
        source_before[id * 256..id * 256 + 29].copy_from_slice(&f::source());
        banks.push(bank);
    }
    let tables_before = ordering::guest_tables();
    memory.copy(f::RAM_OFFSET + 8192, &source_before);
    memory.copy(f::TABLE_OFFSET, &tables_before);
    memory.write_record(f::COMMON_OFFSET, f::Common::new(config));
    fs::create_dir(&args[7]).map_err(|e| e.to_string())?;
    let _machine = Machine::create(&memory)?;
    let ram = memory.0 as usize;
    let service = |id: usize| -> Result<Option<Service>> {
        let base = ram + id * RAM_SIZE;
        let output = owner_output(&args[7], id)?;
        Ok(checked(
            fence_producer::Compiler::new(
                args[3].clone().into(),
                args[6].clone().into(),
                output,
                id as u64,
                config,
            ),
            move |_| unsafe {
                ptr::read_volatile((base + f::OWNER_OFFSET) as *const f::Owner).expected
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
    let requests = execution.as_ref().map(Vec::as_slice).unwrap_or(&[]);
    let owners = (0..n)
        .map(|id| owner_json(&memory, id, &banks[id], requests))
        .collect::<Vec<_>>()
        .join(",");
    let images = (0..f::TRIALS)
        .map(|i| {
            format!(
                "\"{}\"",
                memory
                    .read::<{ f::IMAGE_BYTES }>(n * RAM_SIZE + i * f::IMAGE_BYTES)
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>()
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    let common = unsafe { &*memory.0.add(f::COMMON_OFFSET).cast::<f::Common>() };
    let ready = common
        .ready
        .each_ref()
        .map(|g| g.value.load(Ordering::Relaxed));
    let done = common
        .done
        .each_ref()
        .map(|g| g.value.load(Ordering::Relaxed));
    println!(
        "{{\"case\":\"fence\",\"profile\":{case},\"n\":{n},\"configured_vcpus\":{n},\"tested_vcpus\":{n},\"trial_count\":{},\"vm_nonce\":{vm},\"owners\":[{owners}],\"images\":[{images}],\"source_before\":{source_before:?},\"source_after\":{:?},\"tables_before\":{tables_before:?},\"tables_after\":{:?},\"common\":{{\"compiled\":{},\"epoch\":{},\"ready\":{ready:?},\"done\":{done:?},\"stopped\":{}}}}}",
        f::TRIALS,
        memory.read::<4096>(f::RAM_OFFSET + 8192),
        memory.read::<16384>(f::TABLE_OFFSET),
        common.compiled.value.load(Ordering::Relaxed),
        common.epoch.value.load(Ordering::Relaxed),
        common.stopped.value.load(Ordering::Relaxed)
    );
    execution?;
    for id in 0..n {
        let s: Shared = memory.read_record(id * RAM_SIZE + SHARED_OFFSET);
        if s.status != SUCCESS
            || s.current_el != 4
            || s.sctlr & 0x1005 != 0x1005
            || s.actlr & 2 != 2
            || s.tcr != shadow::TCR_BOTH
        {
            return Err(format!(
                "fence owner{id} status {} detail {}",
                s.status, s.detail
            ));
        }
    }
    Ok(())
}
