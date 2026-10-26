//! Host setup and post-join evidence for the terminal stack wave.
use super::*;
use natemu_abi::{recovery as recover, stack_boundary as b};

pub(super) struct Config {
    pub profile: u64,
    pub actor: usize,
    pub alias_profile: u64,
    outcome: &'static str,
    region: &'static str,
}
impl Config {
    pub fn parse(args: &[String], n: usize) -> Result<Option<Self>> {
        if args[2] != "i4v-stack-boundary" {
            return Ok(None);
        }
        if args.len() != 9 {
            return Err(
                "boundary requires outcome, N, producer, output, actor and source region".into(),
            );
        }
        let (profile, outcome) = match args[3].as_str() {
            "ss" => (b::SS, "ss"),
            "gp" => (b::GP, "gp"),
            "split" => (b::SPLIT, "split"),
            "split-np" => (b::SPLIT_NP, "split-np"),
            "split-ro" => (b::SPLIT_RO, "split-ro"),
            _ => return Err("unsupported boundary outcome".into()),
        };
        let actor = args[7].parse().map_err(|_| "invalid boundary actor")?;
        if !(1..=4).contains(&n) || actor >= n {
            return Err("boundary requires 1..=4 owners and an in-range actor".into());
        }
        let (alias_profile, region) = match args[8].as_str() {
            "low" => (1, "low"),
            "high" => (2, "high"),
            _ => return Err("unsupported boundary source region".into()),
        };
        Ok(Some(Self {
            profile,
            actor,
            alias_profile,
            outcome,
            region,
        }))
    }
}

pub(super) fn prepare_owner(memory: &Memory, id: usize) {
    let base = id * RAM_SIZE;
    let attrs = 3 | 1 << 10 | 3 << 8 | 3 << 53;
    for offset in (b::OWNER_OFFSET..0x90000).step_by(4096) {
        memory.word(
            base + 0x14000 + offset / 4096 * 8,
            (IPA + (base + offset) as u64) | attrs,
        );
    }
    for offset in (b::SHARED_OFFSET..0xa0000).step_by(4096) {
        memory.word(
            base + 0x14000 + offset / 4096 * 8,
            (IPA + offset as u64) | attrs,
        );
    }
    unsafe {
        ptr::write(memory.0.add(base + b::OWNER_OFFSET).cast(), b::Owner::ZERO);
    }
    memory.word(base + b::ENABLE_OFFSET, 1);
}
pub(super) fn prepare_shared(memory: &Memory, config: &Config, n: usize) {
    unsafe {
        ptr::write(
            memory.0.add(b::SHARED_OFFSET).cast(),
            b::Shared::new(n, config.profile, config.actor).unwrap(),
        );
    }
}
pub(super) fn shared(memory: &Memory) -> &b::Shared {
    // Called only after every owner has joined.
    unsafe { &*memory.0.add(b::SHARED_OFFSET).cast() }
}
pub(super) fn owner(memory: &Memory, id: usize) -> b::Owner {
    unsafe { ptr::read(memory.0.add(id * RAM_SIZE + b::OWNER_OFFSET).cast()) }
}
macro_rules! fields {
    ($record:ident; $($field:ident),* $(,)?) => {{
        let mut out=Vec::<String>::new();
        $(out.push(format!("\"{}\":{:?}",stringify!($field),$record.$field));)*
        out
    }};
}
fn shared_json(s: &b::Shared) -> String {
    let mut f = fields!(s; n,profile,actor,ready,setup,actor_done,done,remapped,
        acknowledgements,released,reclaimed,reuse_blocked,entry_rejected,
        retained_before_release,trace_start,finished_before,old_page,other_page,
        configured_leaves,code_before,code_at_outcome,data_before,tables_before);
    f.push(format!("\"wave\":{}", controls::coordinator_json(&s.wave)));
    format!("{{{}}}", f.join(","))
}
fn owner_json(memory: &Memory, id: usize) -> String {
    let o = owner(memory, id);
    let base = id * RAM_SIZE;
    let final_state: Shared = unsafe { ptr::read(memory.0.add(base + SHARED_OFFSET).cast()) };
    let final_guest: recover::GuestState =
        unsafe { ptr::read(memory.0.add(base + recover::GUEST_OFFSET).cast()) };
    let mut f = fields!(o; lower_before,upper_before,entries,completed,step,esr,far,pc,spsr,
        faults,vector,error,address,rip,saved_page,saved_writable,native_pair_before,
        native_pair_after,service_attempts,service_completed,service_pc,service_resume_pc,
        data_before_service,data_after_service,dirty_before,dirty_after,ready_mask,
        retained_at_outcome,ack_pair,released);
    for (name, frame) in [
        ("before_frame", &o.before_frame),
        ("initial", &o.initial),
        ("current", &o.current),
        ("fault_frame", &o.fault_frame),
        ("final_frame", &final_state.captured),
        ("entered", &final_state.initial),
    ] {
        f.push(format!("\"{name}\":{}", frame_json(frame)));
    }
    for (name, guest) in [
        ("before_guest", &o.before_guest),
        ("initial_guest", &o.initial_guest),
        ("guest_after", &o.guest_after),
        ("final_guest", &final_guest),
    ] {
        f.push(format!("\"{name}\":{}", recovery::guest_json(guest)));
    }
    f.push(format!(
        "\"prefixes\":[{}]",
        o.prefixes
            .iter()
            .map(frame_json)
            .collect::<Vec<_>>()
            .join(",")
    ));
    let fragments = o
        .fragments
        .iter()
        .map(|v| {
            format!(
                "{{{}}}",
                fields!(v; linear,bytes,gpa,leaf,before,after,ready).join(",")
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    f.push(format!("\"fragments\":[{fragments}]"));
    f.extend(fields!(final_state; status,detail,capture_pc,current_el,sctlr,actlr,tcr));
    f.push(format!("\"id\":{id},\"final_lower_leaf\":{:?},\"final_upper_leaf\":{:?},\"final_code\":{:?},\"bank_after\":{:?}",
        i4v::words(memory,base+shadow::LOWER_LEAF),i4v::words(memory,base+shadow::UPPER_LEAF),
        memory.read::<{stack::CODE_BYTES}>(CODE_OFFSET+4096),memory.read::<512>(base+BANK_OFFSET)));
    format!("{{{}}}", f.join(","))
}
pub(super) fn report(memory: &Memory, config: &Config, n: usize, prefix: &str) -> String {
    let owners = (0..n)
        .map(|id| owner_json(memory, id))
        .collect::<Vec<_>>()
        .join(",");
    format!(
        "{{\"case\":\"stack-boundary\",\"profile\":{},\"outcome\":\"{}\",\"region\":\"{}\",\"actor\":{},\"n\":{n},\"configured_vcpus\":{n},\"tested_vcpus\":{n},\"prefix\":{prefix},\"boundary\":{},\"owners\":[{owners}],\"data_after\":{:?},\"tables_after\":{:?},\"source_after\":{:?}}}",
        config.profile,
        config.outcome,
        config.region,
        config.actor,
        shared_json(shared(memory)),
        memory.read::<8192>(DATA_OFFSET),
        memory.read::<16384>(recover::TABLE_OFFSET),
        memory.read::<20>(recover::SOURCE_OFFSET)
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn boundary_cli_rejects_invalid_owner_and_outcome_before_vm_creation() {
        let mut args: Vec<String> = [
            "host",
            "runtime",
            "i4v-stack-boundary",
            "split-ro",
            "4",
            "producer",
            "output",
            "3",
            "high",
        ]
        .map(String::from)
        .into();
        let config = Config::parse(&args, 4).unwrap().unwrap();
        assert_eq!(
            (config.profile, config.actor, config.alias_profile),
            (b::SPLIT_RO, 3, 2)
        );
        assert!(Config::parse(&args, 3).is_err());
        assert!(Config::parse(&args, 0).is_err());
        args[7] = "-1".into();
        assert!(Config::parse(&args, 4).is_err());
        args[7] = "0".into();
        args[3] = "unknown".into();
        assert!(Config::parse(&args, 4).is_err());
        args[3] = "split".into();
        args[8] = "unknown".into();
        assert!(Config::parse(&args, 4).is_err());
    }
    #[test]
    fn boundary_records_are_private_but_coordination_is_shared_and_nonexecutable() {
        let memory = Memory::with_size(4 * RAM_SIZE).unwrap();
        for id in 0..4 {
            let base = id * RAM_SIZE;
            tables(&memory, base, true);
            prepare_owner(&memory, id);
            for offset in (b::OWNER_OFFSET..0xa0000).step_by(4096) {
                let pte = u64::from_le_bytes(memory.read::<8>(base + 0x14000 + offset / 4096 * 8));
                let physical = IPA
                    + offset as u64
                    + if offset < COORD_OFFSET {
                        base as u64
                    } else {
                        0
                    };
                assert_eq!(pte & 0x0000_ffff_ffff_f000, physical);
                assert_eq!(pte & (3 << 53), 3 << 53);
            }
            assert_eq!(
                u64::from_le_bytes(memory.read::<8>(base + b::ENABLE_OFFSET)),
                1
            );
        }
    }
}
