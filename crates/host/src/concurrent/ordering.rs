//! Whole generated participant blocks with synchronization outside guest accesses.
use super::*;
use natemu_abi::{atomic_progress as a, ordering as o};

macro_rules! fields {
    ($s:ident; $($field:ident),* $(,)?) => {{
        let mut out = Vec::<String>::new();
        $(out.push(format!("\"{}\":{:?}", stringify!($field), $s.$field));)*
        out
    }};
}
fn atomic_config() -> Option<natemu_abi::schedule::Config> {
    progress::configuration().filter(|c| c.family == natemu_abi::schedule::ATOMIC)
}
fn image_bytes() -> usize {
    if atomic_config().is_some() {
        a::IMAGE_BYTES
    } else {
        o::IMAGE_BYTES
    }
}
pub(super) fn owner_mappings(memory: &Memory, id: usize, n: usize) {
    let base = id * RAM_SIZE;
    tables(memory, base, false);
    mixed::private_code(memory, base);
    let attrs = 3 | 1 << 10 | 3 << 8 | 3 << 53;
    for offset in (o::RECORD_OFFSET..o::COMMON_OFFSET).step_by(4096) {
        memory.word(
            base + 0x14000 + offset / 4096 * 8,
            (IPA + (base + offset) as u64) | attrs,
        );
    }
    for offset in (o::COMMON_OFFSET..o::TABLE_OFFSET + 16384).step_by(4096) {
        memory.word(
            base + 0x14000 + offset / 4096 * 8,
            (IPA + offset as u64) | attrs,
        );
    }
    for offset in (0..o::TRIALS * image_bytes()).step_by(4096) {
        memory.word(
            base + 0x14000 + (o::IMAGE_VA_OFFSET + offset) / 4096 * 8,
            (IPA + (n * RAM_SIZE + offset) as u64) | attrs,
        );
    }
    for entry in 256..=258 {
        memory.word(base + shadow::LOWER_LEAF + entry * 8, 0);
    }
}
pub(super) fn guest_tables() -> [u8; 16384] {
    let mut bytes = [0; 16384];
    for (offset, value) in [
        (0, 0x2027u64),
        (4096, 0x3027),
        (8192, 0x4027),
        (12288 + 256 * 8, 0x100067),
        (12288 + 257 * 8, 0x101067),
        (12288 + 258 * 8, 0x102027),
    ] {
        bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
    }
    bytes
}
fn trial_json(s: &o::Trial) -> String {
    let mut f = fields!(s; epoch,token,completed,faults,actlr,fence_completed);
    f.push(format!("\"captured\":{}", frame_json(&s.captured)));
    let faults = s
        .fault
        .iter()
        .map(|fault| {
            let mut f = fields!(fault; esr,far,pc,spsr,resume);
            f.push(format!("\"frame\":{}", frame_json(&fault.frame)));
            format!("{{{}}}", f.join(","))
        })
        .collect::<Vec<_>>()
        .join(",");
    f.push(format!("\"fault\":[{faults}]"));
    format!("{{{}}}", f.join(","))
}
fn owner_json(memory: &Memory, id: usize, bank: &[u8; 512], requests: &[wire::Packet]) -> String {
    let base = id * RAM_SIZE;
    let s: Shared = memory.read_record(base + SHARED_OFFSET);
    let owner = unsafe { &*memory.0.add(base + o::OWNER_OFFSET).cast::<o::Owner>() };
    let mut f = fields!(owner; id,variant,trial,completed,expected,request,reply,walks,table_reads,table_writes,routes,code,code_length,isar0);
    f.extend(fields!(s; status,detail,current_el,sctlr,actlr,tcr,capture_pc,service_pc));
    f.push(format!(
        "\"initial\":{},\"current\":{},\"captured\":{}",
        frame_json(&owner.initial),
        frame_json(&owner.current),
        frame_json(&s.captured)
    ));
    f.push(format!("\"bank_before\":{bank:?},\"bank_after\":{:?},\"published_code\":{:?},\"final_native_leaf\":{:?},\"compiler_requests\":{:?}",memory.read::<512>(base+BANK_OFFSET),memory.read::<{o::CODE_BYTES}>(base+CODE_OFFSET),i4v::words(memory,base+shadow::LOWER_LEAF),requests.iter().filter(|p|p[3]==id as u64).collect::<Vec<_>>()));
    let trials = (0..o::TRIALS)
        .map(|i| {
            trial_json(
                &memory.read_record(base + o::RECORD_OFFSET + i * core::mem::size_of::<o::Trial>()),
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    f.push(format!("\"trials\":[{trials}]"));
    format!("{{{}}}", f.join(","))
}
fn atomic_json(memory: &Memory, n: usize) -> String {
    let state = unsafe {
        &*memory
            .0
            .add(n * RAM_SIZE + a::BACKING_OFFSET)
            .cast::<a::State>()
    };
    let mut out = fields!(state; magic,n,actor,writer,parked,requested,drained,mutated,acknowledged,blocked,opened,delivered,snapshot_count,tables_before,tables_after);
    let owners=state.owners.iter().map(|v| {
        let mut f=fields!(v; saved_gpa,saved_leaf,fault_pc,resume_pc,queued,resumed,completed,ack_leaf,new_leaf);
        f.push(format!("\"fault_frame\":{}",frame_json(&v.fault_frame)));
        f.push(format!("\"before_guest\":{},\"completed_guest\":{}",crate::recovery::guest_json(&v.before_guest),crate::recovery::guest_json(&v.completed_guest)));
        let e=&v.event;
        let mut event=fields!(e; consumed,deliveries,boundary,blocked,wait_checks,shadow,priority);
        event.push(format!("\"packet\":{}",crate::recovery::guest_json(&e.packet)));
        let w=&v.walk;
        f.push(format!("\"event\":{{{}}},\"walk\":{{{}}}",event.join(","),fields!(w; address,gpa,leaf_bytes,writable,dirty,read_count,writes,reads).join(",")));
        format!("{{{}}}",f.join(","))
    }).collect::<Vec<_>>().join(",");
    let cuts = state
        .cuts
        .iter()
        .map(|b| {
            format!(
                "\"{}\"",
                b.iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>()
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    out.push(format!(
        "\"owners\":[{owners}],\"cuts\":[{cuts}],\"coordinator\":{}",
        controls::coordinator_json(&state.coordinator)
    ));
    format!("{{{}}}", out.join(","))
}

pub fn run(args: &[String]) -> Result<()> {
    let profile = args[4].parse().map_err(|_| "invalid ordering profile")?;
    let n: usize = args[5].parse().map_err(|_| "invalid ordering vCPU count")?;
    let rotation = args[6].parse().map_err(|_| "invalid ordering rotation")?;
    let trial_seed = args[7].parse().map_err(|_| "invalid ordering seed")?;
    let config = o::Config::new(n as u64, profile, rotation, trial_seed)
        .ok_or("unsupported ordering configuration")?;
    require_vcpus(n)?;
    let memory = Memory::with_size((n + 1) * RAM_SIZE)?;
    load_resident(&memory, &args[1])?;
    let vm_nonce = new_vm_nonce()?;
    let mut banks = Vec::new();
    let mut source_before = [0xcc; 4096];
    for id in 0..n {
        let base = id * RAM_SIZE;
        if id != 0 {
            memory.replicate_resident_text(base);
        }
        owner_mappings(&memory, id, n);
        let (mut frame, mut bank, _) = seed(id);
        for b in &mut bank {
            *b ^= id as u8;
        }
        let pointers = config.pointers(id as u64);
        frame.x[4] = config.token(id as u64, 0);
        frame.x[6] = pointers[0];
        frame.x[5] = pointers[1];
        frame.x[10] = pointers[2];
        memory.copy(base + BANK_OFFSET, &bank);
        let mut object = Object::new(LIVE_LOAD);
        object.code_length = o::CODE_BYTES as u64;
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
            mode: o::MODE,
            lifecycle: wire::Lifecycle::new(vm_nonce),
        };
        let mut owner = o::Owner::ZERO;
        owner.id = id as u64;
        owner.variant = config.variant(id as u64) as u64;
        owner.initial = frame;
        memory.write_record(base + SHARED_OFFSET, shared);
        memory.write_record(base + o::OWNER_OFFSET, owner);
        let bytes = o::source(config.variant(id as u64)).unwrap();
        source_before[id * 256..id * 256 + bytes.len()].copy_from_slice(bytes);
        banks.push(bank);
    }
    let tables_before = guest_tables();
    memory.copy(o::RAM_OFFSET + 8192, &source_before);
    memory.copy(o::TABLE_OFFSET, &tables_before);
    memory.write_record(o::COMMON_OFFSET, o::Common::new(config));
    if let Some(scheduled) = atomic_config() {
        if profile != 9 || rotation != scheduled.actor || scheduled.n != n as u64 {
            return Err("atomic progress fixture mismatch".into());
        }
        let frames = core::array::from_fn(|id| {
            if id < n {
                unsafe {
                    (*memory
                        .0
                        .add(id * RAM_SIZE + o::OWNER_OFFSET)
                        .cast::<o::Owner>())
                    .initial
                }
            } else {
                NativeFrame::ZERO
            }
        });
        let supplemental = n * RAM_SIZE + a::BACKING_OFFSET;
        let attrs = 3 | 1 << 10 | 3 << 8 | 3 << 53;
        for id in 0..n {
            for page in (0..a::BYTES).step_by(4096) {
                memory.word(
                    id * RAM_SIZE + 0x14000 + (a::OFFSET + page) / 4096 * 8,
                    (IPA + (supplemental + page) as u64) | attrs,
                );
            }
        }
        memory.write_record(
            supplemental,
            a::State::new(scheduled, &frames).ok_or("atomic state admission")?,
        );
    }
    fs::create_dir(&args[9]).map_err(|e| e.to_string())?;
    let _machine = Machine::create(&memory)?;
    let execution = run_owners(
        n,
        shadow::TCR_BOTH,
        0,
        memory.0 as usize,
        None,
        0,
        None,
        None,
        Some((
            args[3].clone().into(),
            args[8].clone().into(),
            args[9].clone().into(),
            NativeProfile::Ordering(config),
        )),
    );
    let requests = execution.as_ref().map(Vec::as_slice).unwrap_or(&[]);
    let owners = (0..n)
        .map(|id| owner_json(&memory, id, &banks[id], requests))
        .collect::<Vec<_>>()
        .join(",");
    let images = (0..o::TRIALS)
        .map(|i| {
            format!(
                "\"{}\"",
                unsafe {
                    core::slice::from_raw_parts(
                        memory.0.add(n * RAM_SIZE + i * image_bytes()),
                        image_bytes(),
                    )
                }
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    let common = unsafe { &*memory.0.add(o::COMMON_OFFSET).cast::<o::Common>() };
    let ready = common
        .ready
        .each_ref()
        .map(|g| g.value.load(Ordering::Relaxed));
    let done = common
        .done
        .each_ref()
        .map(|g| g.value.load(Ordering::Relaxed));
    let atomic = if atomic_config().is_some() {
        format!(
            ",\"image_bytes\":{},\"atomic\":{}",
            a::IMAGE_BYTES,
            atomic_json(&memory, n)
        )
    } else {
        String::new()
    };
    println!(
        "{{\"case\":\"ordering\",\"n\":{n},\"configured_vcpus\":{n},\"tested_vcpus\":{n},\"profile\":{profile},\"rotation\":{rotation},\"seed\":{trial_seed},\"trial_count\":{},\"vm_nonce\":{vm_nonce},\"owners\":[{owners}],\"images\":[{images}],\"source_before\":{source_before:?},\"source_after\":{:?},\"tables_before\":{tables_before:?},\"tables_after\":{:?},\"common\":{{\"compiled\":{},\"epoch\":{},\"ready\":{ready:?},\"done\":{done:?},\"stopped\":{}}}{atomic}}}",
        o::TRIALS,
        memory.read::<4096>(o::RAM_OFFSET + 8192),
        memory.read::<16384>(o::TABLE_OFFSET),
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
                "ordering owner{id} status {} detail {}",
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
    fn trial_records_are_private_and_images_shared_outside_owner_backing() {
        let memory = Memory::with_size(5 * RAM_SIZE).unwrap();
        for id in 0..4 {
            owner_mappings(&memory, id, 4);
            for offset in [o::OWNER_OFFSET, o::RECORD_OFFSET, 0x8f000, CODE_OFFSET] {
                let pte = u64::from_le_bytes(
                    memory.read::<8>(id * RAM_SIZE + 0x14000 + offset / 4096 * 8),
                );
                assert_eq!(
                    pte & 0x0000_ffff_ffff_f000,
                    IPA + (id * RAM_SIZE + offset) as u64
                );
            }
            for offset in [o::COMMON_OFFSET, o::RAM_OFFSET, o::TABLE_OFFSET] {
                let pte = u64::from_le_bytes(
                    memory.read::<8>(id * RAM_SIZE + 0x14000 + offset / 4096 * 8),
                );
                assert_eq!(pte & 0x0000_ffff_ffff_f000, IPA + offset as u64);
                assert_eq!(pte & (3 << 53), 3 << 53);
            }
            let pte = u64::from_le_bytes(
                memory.read::<8>(id * RAM_SIZE + 0x14000 + o::IMAGE_VA_OFFSET / 4096 * 8),
            );
            assert_eq!(pte & 0x0000_ffff_ffff_f000, IPA + 4 * RAM_SIZE as u64);
        }
    }
}
