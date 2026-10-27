//! Native semantic helper composition; containment adds compile-only work.
//! The helper itself is not FEX-generated REP.
use super::*;
use natemu_abi::{partial_progress as p, schedule as sc};
macro_rules! fields {
    ($s:ident;$($name:ident),* $(,)?)=>{{let mut out=Vec::<String>::new();
        $(out.push(format!("\"{}\":{:?}",stringify!($name),$s.$name));)*out}};
}
fn record<T>(m: &Memory, offset: usize) -> &T {
    unsafe { &*m.0.add(offset).cast::<T>() }
}
fn walk_json(w: &natemu_abi::coherence::Walk) -> String {
    format!(
        "{{{}}}",
        fields!(w;address,intent,gpa,leaf_bytes,writable,dirty,user,executable,wb,
        vector,error,read_count,write_count,reads,writes)
        .join(",")
    )
}
fn owner_json(m: &Memory, id: usize, bank: &[u8; 512]) -> String {
    let base = id * RAM_SIZE;
    let o: &p::Owner = record(m, base + p::OWNER_OFFSET);
    let s: &Shared = record(m, base + SHARED_OFFSET);
    let mut out = fields!(o;id,phase,reads,writes,read_values,sampled,subaccess,source_route,destination_route,
        destination_writable,retained,released,faults,retries,probe_writes,probe_route,
        notifications,pending,event_attempts,event_blocked,delivered,delivery_state,walk_count,store_pc,store_bytes);
    out.extend(fields!(s;status,detail,current_el,sctlr,actlr,tcr));
    out.push(format!(
        "\"initial\":{},\"captured\":{}",
        frame_json(&o.initial),
        frame_json(&s.captured)
    ));
    for (name, g) in [
        ("before", &o.before),
        ("held", &o.held),
        ("resumed", &o.resumed),
        ("after", &o.after),
    ] {
        out.push(format!("\"{name}\":{}", crate::recovery::guest_json(g)));
    }
    out.push(format!(
        "\"walks\":[{}]",
        o.walks.iter().map(walk_json).collect::<Vec<_>>().join(",")
    ));
    out.push(format!(
        "\"fault\":[{}]",
        o.fault
            .iter()
            .map(|f| {
                let mut fields = fields!(f;esr,far,pc,spsr,resume_pc,value,gpa);
                fields.push(format!("\"frame\":{}", frame_json(&f.frame)));
                format!("{{{}}}", fields.join(","))
            })
            .collect::<Vec<_>>()
            .join(",")
    ));
    out.push(format!(
        "\"bank_before\":{bank:?},\"bank_after\":{:?},\"native_leaf\":{:?}",
        m.read::<512>(base + BANK_OFFSET),
        i4v::words(m, base + shadow::LOWER_LEAF)
    ));
    format!("{{{}}}", out.join(","))
}
pub fn run(args: &[String]) -> Result<()> {
    let n: usize = args[5].parse().map_err(|_| "invalid partial owner count")?;
    let ordinal: u64 = args[6]
        .parse()
        .map_err(|_| "invalid partial seed ordinal")?;
    let config = sc::Config::new(sc::PARTIAL, n as u64, ordinal)
        .ok_or("unsupported partial configuration")?;
    if containment::configuration().is_some() {
        // The partial fixture normally has no compiler. Containment adds one
        // whose per-owner output needs this parent before any owner starts.
        fs::create_dir(&args[8]).map_err(|e| format!("create partial compiler output: {e}"))?;
    }
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
        let (frame, mut bank, _) = seed(id);
        for b in &mut bank {
            *b ^= id as u8;
        }
        memory.copy(base + BANK_OFFSET, &bank);
        let mut object = Object::new(LIVE_LOAD);
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
            mode: p::MODE,
            lifecycle: wire::Lifecycle::new(vm),
        };
        memory.write_record(base + SHARED_OFFSET, shared);
        memory.write_record(base + p::OWNER_OFFSET, p::Owner::new(id as u64, frame));
        source_before[id * 256..id * 256 + 2].copy_from_slice(&p::SOURCE);
        banks.push(bank);
    }
    let tables_before = p::tables();
    if let Some(c) = containment::configuration() {
        let at = c.actor as usize * 256 + 16;
        source_before[at..at + 3].copy_from_slice(&natemu_abi::containment::PARTIAL_SOURCE);
    }
    let mut data_before = [0xa5; 8192];
    data_before[4096..].fill(0x5a);
    memory.copy(p::RAM_OFFSET, &data_before);
    memory.copy(p::SOURCE_OFFSET, &source_before);
    memory.copy(p::TABLE_OFFSET, &tables_before);
    memory.write_record(COORD_OFFSET, Coordinator::new(n).unwrap());
    memory.write_record(p::COMMON_OFFSET, p::Common::new(config).unwrap());
    let _machine = Machine::create(&memory)?;
    let ram = memory.0 as usize;
    // Only a contained cell's actor compiles; it must request the containment witness.
    let service = |id: usize| -> Result<Option<Service>> {
        let Some(config) = containment::configuration().filter(|c| c.actor == id as u64) else {
            return Ok(None);
        };
        let output = owner_output(&args[8], id)?;
        Ok(checked(
            crate::i4v_controls::producer::Compiler::new_owner(
                args[3].clone().into(),
                args[7].clone().into(),
                output,
                id as u64,
                1,
            ),
            move |_| unsafe { (*containment::state(ram, config.n as usize)).expected },
        ))
    };
    let execution = run_owners(
        Owners {
            n,
            tcr: shadow::TCR_BOTH,
            memory: ram,
            delayed: None,
            deadline: Duration::from_secs(if containment::configuration().is_some() {
                100
            } else {
                10
            }),
        },
        &service,
    );
    let owners = (0..n)
        .map(|id| owner_json(&memory, id, &banks[id]))
        .collect::<Vec<_>>()
        .join(",");
    let common: &p::Common = record(&memory, p::COMMON_OFFSET);
    let common_json=fields!(common;n,actor,ordinal,ready,mutation_blocked,helper_done,released,mutated,
        acknowledgements,admission_blocked,opened,probes_done,delivered,finished,images,source_generation,destination_generation,pending_events).join(",");
    let images = (0..p::IMAGES)
        .map(|i| {
            format!(
                "\"{}\"",
                memory
                    .read::<8192>(n * RAM_SIZE + i * 8192)
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>()
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    println!(
        "{{\"case\":\"partial-progress\",\"n\":{n},\"ordinal\":{ordinal},\"actor\":{},\"vm_nonce\":{vm},\"owners\":[{owners}],\"common\":{{{common_json}}},\"images\":[{images}],\"coordinator\":{},\"data_before\":{data_before:?},\"data_after\":{:?},\"source_before\":{source_before:?},\"source_after\":{:?},\"tables_before\":{tables_before:?},\"tables_after\":{:?}}}",
        config.actor,
        controls::coordinator_json(record(&memory, COORD_OFFSET)),
        memory.read::<8192>(p::RAM_OFFSET),
        memory.read::<4096>(p::SOURCE_OFFSET),
        memory.read::<16384>(p::TABLE_OFFSET)
    );
    if containment::configuration().is_some() {
        let destruction = containment::destroy(_machine);
        return execution.map(|_| ()).and(destruction);
    }
    execution.map(|_| ())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn helper_records_are_private_and_routes_are_shared() {
        let m = Memory::with_size(5 * RAM_SIZE).unwrap();
        for id in 0..4 {
            ordering::owner_mappings(&m, id, 4);
            let mapped = |offset| {
                u64::from_le_bytes(m.read::<8>(id * RAM_SIZE + 0x14000 + offset / 4096 * 8))
                    & 0x0000_ffff_ffff_f000
            };
            assert_eq!(
                mapped(p::OWNER_OFFSET),
                IPA + (id * RAM_SIZE + p::OWNER_OFFSET) as u64
            );
            for at in [
                p::COMMON_OFFSET,
                p::RAM_OFFSET,
                p::SOURCE_OFFSET,
                p::TABLE_OFFSET,
                p::TABLE_OFFSET + 12288,
            ] {
                assert_eq!(mapped(at), IPA + at as u64);
            }
            for i in 0..p::IMAGES {
                assert_eq!(
                    mapped(p::IMAGE_OFFSET + i * 8192),
                    IPA + (4 * RAM_SIZE + i * 8192) as u64
                );
            }
        }
    }
}
