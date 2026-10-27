//! Retained native observations for shared x86 table mutation and shootdown.
use super::*;
use natemu_abi::families::coherence as c;

macro_rules! fields {
    ($s:ident; $($field:ident),* $(,)?) => {{
        let mut out = Vec::<String>::new();
        $(out.push(format!("\"{}\":{:?}", stringify!($field), $s.$field));)* out
    }};
}
fn record<T>(memory: &Memory, offset: usize) -> &T {
    unsafe { &*memory.0.add(offset).cast::<T>() }
}
fn leaves(memory: &Memory, base: usize) -> [u64; 4] {
    [
        shadow::LOWER_LEAF + 256 * 8,
        shadow::LOWER_LEAF + 257 * 8,
        shadow::UPPER_LEAF + 256 * 8,
        shadow::UPPER_LEAF + 257 * 8,
    ]
    .map(|offset| u64::from_le_bytes(memory.read::<8>(base + offset)))
}
fn owner_mappings(memory: &Memory, id: usize, n: usize) {
    ordering::owner_mappings(memory, id, n);
    let base = id * RAM_SIZE;
    map_private(
        memory,
        base,
        &[Span::data(
            shadow::UPPER_ROOT,
            shadow::UPPER_LEAF + 4096 - shadow::UPPER_ROOT,
            base + shadow::UPPER_ROOT,
        )],
    );
    for (at, target) in [
        (shadow::UPPER_ROOT + 256 * 8, shadow::UPPER_L1),
        (shadow::UPPER_L1, shadow::UPPER_L2),
        (shadow::UPPER_L2, shadow::UPPER_LEAF),
    ] {
        memory.word(base + at, (IPA + (base + target) as u64) | 3);
    }
    map_private(
        memory,
        base,
        &[
            Span::data(
                COORD_OFFSET,
                c::TABLE_OFFSET + c::TABLE_BYTES - COORD_OFFSET,
                COORD_OFFSET,
            ),
            Span::data(
                c::IMAGE_VA_OFFSET,
                c::MAX_SNAPSHOTS * c::SNAPSHOT_BYTES,
                n * RAM_SIZE,
            ),
        ],
    );
    for leaf in [shadow::LOWER_LEAF, shadow::UPPER_LEAF] {
        for index in 256..=258 {
            memory.word(base + leaf + index * 8, 0);
        }
    }
}
fn walk_json(w: &c::Walk) -> String {
    format!(
        "{{{}}}",
        fields!(w; address,intent,gpa,leaf_bytes,writable,dirty,user,executable,wb,
        vector,error,read_count,write_count,reads,writes)
        .join(",")
    )
}
fn visit_json(v: &c::Visit) -> String {
    let mut out = fields!(v; index,stage,page,kind,address,rip,admitted,completed,faults,retries,
        esr,far,pc,spsr,resume_pc,saved_gpa,saved_writable,saved_leaf_bytes,vector,error,cr2_before,cr2_after,
        native_before,native_after,memory_before,memory_after,walk_count);
    for (name, frame) in [
        ("initial", &v.initial),
        ("fault", &v.fault),
        ("captured", &v.captured),
    ] {
        out.push(format!("\"{name}\":{}", frame_json(frame)));
    }
    out.push(format!(
        "\"walks\":[{}]",
        v.walks.iter().map(walk_json).collect::<Vec<_>>().join(",")
    ));
    format!("{{{}}}", out.join(","))
}
fn owner_json(
    memory: &Memory,
    id: usize,
    bank: &[u8; 512],
    code_before: &[u8; c::CODE_BYTES],
    native_before: &[u64; 4],
    requests: &[wire::Packet],
) -> String {
    let base = id * RAM_SIZE;
    let s: &Shared = record(memory, base + SHARED_OFFSET);
    let o: &c::Owner = record(memory, base + c::OWNER_OFFSET);
    let mut out = fields!(o; id,visit,completed,faults,retries,expected,requests,replies,transfers,
        hidden_reads,hidden_loads,code,code_before_clear,code_after_clear,return_pc,
        acknowledgements,ack_leaves,entry_blocked,reuse_blocked,released,entry_rejected,
        mapping_gpa,mapping_writable,mapping_leaf_bytes,terminal_reuse_blocked,code_while_held);
    out.extend(fields!(s; status,detail,current_el,sctlr,actlr,tcr,capture_pc,service_pc));
    for (name, frame) in [
        ("initial", &o.initial),
        ("current", &o.current),
        ("captured", &s.captured),
    ] {
        out.push(format!("\"{name}\":{}", frame_json(frame)));
    }
    for (name, guest) in [
        ("guest_before", &o.guest_before),
        ("guest_after", &o.guest_after),
    ] {
        out.push(format!("\"{name}\":{}", guest_json(guest)));
    }
    for (name, hidden) in [
        ("hidden_before", &o.hidden_before),
        ("hidden_after", &o.hidden_after),
    ] {
        out.push(format!(
            "\"{name}\":{{\"entries\":{:?},\"generation\":{}}}",
            hidden.entries, hidden.generation
        ));
    }
    out.push(format!(
        "\"compile_walks\":[{}]",
        o.compile_walks
            .iter()
            .map(walk_json)
            .collect::<Vec<_>>()
            .join(",")
    ));
    out.push(format!("\"bank_before\":{bank:?},\"bank_after\":{:?},\"code_before\":{code_before:?},\"published_code\":{:?},\"native_before\":{native_before:?},\"native_leaves\":{:?},\"compiler_requests\":{:?}",
        memory.read::<512>(base + BANK_OFFSET),
        memory.read::<{c::CODE_BYTES}>(base + CODE_OFFSET), leaves(memory,base),
        requests.iter().filter(|p|p[3] == id as u64).collect::<Vec<_>>()));
    let visits = (0..c::VISITS)
        .map(|i| {
            visit_json(record(
                memory,
                base + c::RECORD_OFFSET + i * core::mem::size_of::<c::Visit>(),
            ))
        })
        .collect::<Vec<_>>()
        .join(",");
    out.push(format!("\"visits\":[{visits}]"));
    format!("{{{}}}", out.join(","))
}
fn common_json(common: &c::Common) -> String {
    let mut out = fields!(common; n,profile,actor,initialized,compiled,index,prepared,done,mutations,
        acknowledgements,blocked,released,reclaimed,pdpt_changed,reuse_blocked,snapshot_count);
    let snapshots = common
        .snapshots
        .iter()
        .map(|s| {
            format!(
                "{{{}}}",
                fields!(s; kind,visit,mutation,
        generation,active,queued,servicing,retained,acknowledgements)
                .join(",")
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    out.push(format!("\"snapshots\":[{snapshots}]"));
    format!("{{{}}}", out.join(","))
}
pub fn run(args: &[String]) -> Result<()> {
    let profile = args[4].parse().map_err(|_| "invalid coherence profile")?;
    let n: usize = args[5]
        .parse()
        .map_err(|_| "invalid coherence vCPU count")?;
    let actor = args[6].parse().map_err(|_| "invalid coherence actor")?;
    let config =
        c::Config::new(n as u64, profile, actor).ok_or("unsupported coherence configuration")?;
    require_vcpus(n)?;
    let memory = Memory::with_size((n + 1) * RAM_SIZE)?;
    load_resident(&memory, &args[1])?;
    let vm = new_vm_nonce()?;
    let mut banks = Vec::new();
    let mut code_before = Vec::new();
    let mut native_before = Vec::new();
    let mut source_before = [0xcc; 4096];
    for id in 0..n {
        let base = id * RAM_SIZE;
        if id != 0 {
            memory.replicate_resident_text(base);
        }
        owner_mappings(&memory, id, n);
        let (frame, mut bank, _) = seed(id);
        for byte in &mut bank {
            *byte ^= id as u8;
        }
        memory.copy(base + BANK_OFFSET, &bank);
        let mut object = Object::new(LIVE_LOAD);
        object.code_length = c::CODE_BYTES as u64;
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
            mode: c::MODE,
            lifecycle: wire::Lifecycle::new(vm),
        };
        let mut owner = c::Owner::ZERO;
        owner.id = id as u64;
        owner.initial = frame;
        owner.current = frame;
        owner.guest_before = config.guest(&frame);
        owner.guest_before.rip = c::RIP + id as u64 * 256;
        owner.guest_after = owner.guest_before;
        memory.write_record(base + SHARED_OFFSET, shared);
        memory.write_record(base + c::OWNER_OFFSET, owner);
        for kind in 0..2 {
            let offset = id * 256 + kind * 16;
            source_before[offset..offset + 2].copy_from_slice(&c::source(kind).unwrap());
        }
        banks.push(bank);
        code_before.push(memory.read::<{ c::CODE_BYTES }>(base + CODE_OFFSET));
        native_before.push(leaves(&memory, base));
    }
    let tables_before = c::tables(config);
    let mut data_before = [0xa5; c::DATA_BYTES];
    data_before[4096..].fill(0x5a);
    memory.copy(c::RAM_OFFSET, &data_before);
    memory.copy(c::RAM_OFFSET + c::DATA_BYTES, &source_before);
    memory.copy(c::TABLE_OFFSET, &tables_before);
    memory.write_record(COORD_OFFSET, Coordinator::new(n).unwrap());
    memory.write_record(c::COMMON_OFFSET, c::Common::new(config));
    fs::create_dir(&args[8]).map_err(|e| e.to_string())?;
    let _machine = Machine::create(&memory)?;
    let ram = memory.0 as usize;
    let service = |id: usize| -> Result<Option<Service>> {
        let base = ram + id * RAM_SIZE;
        let output = owner_output(&args[8], id)?;
        Ok(checked(
            crate::drivers::i4v_controls::producer::Compiler::new_coherence(
                args[3].clone().into(),
                args[7].clone().into(),
                output,
                id as u64,
                config,
            ),
            move |_| unsafe {
                ptr::read_volatile((base + c::OWNER_OFFSET) as *const c::Owner).expected
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
        .map(|id| {
            owner_json(
                &memory,
                id,
                &banks[id],
                &code_before[id],
                &native_before[id],
                requests,
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    let common: &c::Common = record(&memory, c::COMMON_OFFSET);
    let images = (0..(common.snapshot_count as usize).min(c::MAX_SNAPSHOTS))
        .map(|i| {
            let bytes = memory.read::<{ c::SNAPSHOT_BYTES }>(n * RAM_SIZE + i * c::SNAPSHOT_BYTES);
            format!(
                "\"{}\"",
                bytes.iter().map(|b| format!("{b:02x}")).collect::<String>()
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    println!(
        "{{\"case\":\"coherence\",\"n\":{n},\"configured_vcpus\":{n},\"tested_vcpus\":{n},\"profile\":{profile},\"actor\":{actor},\"vm_nonce\":{vm},\"owners\":[{owners}],\"images\":[{images}],\"source_before\":{source_before:?},\"source_after\":{:?},\"data_before\":{data_before:?},\"data_after\":{:?},\"tables_before\":{tables_before:?},\"tables_after\":{:?},\"common\":{},\"coordinator\":{}}}",
        memory.read::<4096>(c::RAM_OFFSET + c::DATA_BYTES),
        memory.read::<{ c::DATA_BYTES }>(c::RAM_OFFSET),
        memory.read::<{ c::TABLE_BYTES }>(c::TABLE_OFFSET),
        common_json(common),
        controls::coordinator_json(record(&memory, COORD_OFFSET))
    );
    execution.map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn full_tables_and_snapshots_share_backing_without_exposing_private_records() {
        let memory = Memory::with_size(5 * RAM_SIZE).unwrap();
        for id in 0..4 {
            owner_mappings(&memory, id, 4);
            let pte = |offset| {
                u64::from_le_bytes(memory.read::<8>(id * RAM_SIZE + 0x14000 + offset / 4096 * 8))
            };
            for offset in (shadow::UPPER_ROOT..shadow::UPPER_LEAF + 4096).step_by(4096) {
                assert_eq!(
                    pte(offset) & 0x0000_ffff_ffff_f000,
                    IPA + (id * RAM_SIZE + offset) as u64
                );
                assert_eq!(pte(offset) & (3 << 53), 3 << 53);
            }
            for (at, target) in [
                (shadow::UPPER_ROOT + 256 * 8, shadow::UPPER_L1),
                (shadow::UPPER_L1, shadow::UPPER_L2),
                (shadow::UPPER_L2, shadow::UPPER_LEAF),
            ] {
                assert_eq!(
                    u64::from_le_bytes(memory.read::<8>(id * RAM_SIZE + at)),
                    (IPA + (id * RAM_SIZE + target) as u64) | 3
                );
            }
            for offset in [c::OWNER_OFFSET, c::RECORD_OFFSET, 0x8f000, CODE_OFFSET] {
                assert_eq!(
                    pte(offset) & 0x0000_ffff_ffff_f000,
                    IPA + (id * RAM_SIZE + offset) as u64
                );
            }
            for offset in [
                COORD_OFFSET,
                c::COMMON_OFFSET,
                c::RAM_OFFSET,
                c::TABLE_OFFSET,
                c::TABLE_OFFSET + c::TABLE_BYTES - 4096,
            ] {
                assert_eq!(pte(offset) & 0x0000_ffff_ffff_f000, IPA + offset as u64);
                assert_eq!(pte(offset) & (3 << 53), 3 << 53);
            }
            let last = (c::MAX_SNAPSHOTS - 1) * c::SNAPSHOT_BYTES;
            assert_eq!(
                pte(c::IMAGE_VA_OFFSET + last) & 0x0000_ffff_ffff_f000,
                IPA + (4 * RAM_SIZE + last) as u64
            );
            assert_eq!(leaves(&memory, id * RAM_SIZE), [0; 4]);
        }
    }
}
