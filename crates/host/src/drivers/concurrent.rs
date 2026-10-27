//! VM lifetime belongs to this caller; every vCPU belongs to one scoped thread.
use super::*;
use crate::owners::{Owners, Service};
use natemu_abi::concurrency::*;
use natemu_abi::translation as wire;
use natemu_abi::{alias, shadow, stack, stack_concurrent};
mod address;
mod coherence;
pub(crate) mod containment;
mod fence;
mod fence_producer;
mod partial_progress;
mod progress;
pub use coherence::run as run_coherence;
pub use fence::run as run_fence;
pub use progress::run as run_progress;
mod ordering;
pub use ordering::run as run_ordering;
mod boundary;
mod controls;
mod mixed;
mod mixed_producer;
mod producer;
mod stack_producer;
use producer::{checked, owner_output};
use std::{
    path::PathBuf,
    sync::atomic::Ordering,
    time::{Duration, Instant},
};

fn stack_roles(n: usize) -> stack_concurrent::Roles {
    stack_concurrent::Roles::for_fixture(
        n as u64,
        progress::configuration().filter(|c| c.family == natemu_abi::schedule::STACK),
    )
    .expect("validated concurrent stack roles")
}

// Each root has its own boot alias and private state/stacks. The immutable
// native object, guest data and coordinator deliberately share physical pages.
/// Owner `base / RAM_SIZE`'s private tables. Code pages, the coordinator and
/// common pages, and guest data map the shared window 0; the rest is the owner's.
fn tables(memory: &Memory, base: usize, aliases: bool) {
    let own = |offset, bytes| Span::data(offset, bytes, base + offset);
    let common = |offset, bytes| Span::data(offset, bytes, offset);
    let mut spans = resident_text(base);
    spans.extend([
        Span::text(CODE_OFFSET, 8192, CODE_OFFSET),
        own(SHARED_OFFSET, 0x10000),
        own(BANK_OFFSET, 0x2000),
        own(0x50000, 0x4000),
        own(0x60000, 0x4000),
        common(DATA_OFFSET, 0x84000 - DATA_OFFSET),
        common(COORD_OFFSET, 0x93000 - COORD_OFFSET),
    ]);
    private_space(memory, base, &spans, &[(DATA_VA, DATA_OFFSET)], aliases);
}

/// Runs `owners` with the opt-in scheduling and containment overlays attached:
/// their records are installed before any owner starts and reported after join.
fn run_owners(
    owners: Owners,
    service: &(dyn Fn(usize) -> Result<Option<Service>> + Sync),
) -> Result<Vec<wire::Packet>> {
    let (memory, n) = (owners.memory, owners.n);
    unsafe {
        progress::prepare(memory, n)?;
        containment::prepare(memory, n)?;
    }
    let mut observation = containment::Observation::default();
    let s = owners.run(
        service,
        &mut || unsafe { observation.poll(memory, n) },
        &containment::latch,
    )?;
    unsafe {
        progress::report(
            memory,
            n,
            s.completed,
            &s.destroyed,
            &s.cancellations,
            s.error.as_deref(),
            s.began.elapsed(),
        );
        observation.report(memory, n, s.began);
    }
    s.error.map_or(Ok(s.requests), Err)
}

// Keep profile admission explicit: source address selection and permission setup
// are independent fixture inputs, not values inferred from compiler replies.
fn profiles(name: &str) -> Result<(u64, u64, u64)> {
    Ok(match name {
        "entry" => (0, 0, 0),
        "faults" => (1, 0, 0),
        "remap" => (2, 0, 0),
        "compose" => (3, 0, 0),
        "events" => (4, 0, 0),
        "pressure" => (5, 0, 0),
        "alias-low" => (5, 1, 0),
        "alias-high" => (5, 2, 0),
        "stack-low" => (5, 1, 1),
        "stack-high" => (5, 2, 1),
        "stack-parent-low" => (5, 1, 2),
        "stack-parent-high" => (5, 2, 2),
        "stack-leaf-low" => (5, 1, 3),
        "stack-leaf-high" => (5, 2, 3),
        _ => return Err("unsupported I4 scenario".into()),
    })
}

pub fn run_controls(args: &[String]) -> Result<()> {
    controls::run(args)
}
pub fn run_address(args: &[String]) -> Result<()> {
    address::run(args)
}

pub fn run(args: &[String]) -> Result<()> {
    let n: usize = args[4].parse().map_err(|_| "invalid I4 vCPU count")?;
    let boundary_config = boundary::Config::parse(args, n)?;
    let mut coord = Coordinator::new(n).ok_or("I4 admits 1..=4 vCPUs")?;
    require_vcpus(n)?;
    let mixed_profile = if args[2] == "i4v-mixed" {
        match args[3].as_str() {
            "off-first" => 1,
            "paged-first" => 2,
            _ => return Err("unsupported mixed arrangement".into()),
        }
    } else {
        0
    };
    let (scenario_id, alias_profile, stack_kind) = if let Some(config) = &boundary_config {
        (5, config.alias_profile, 1)
    } else if mixed_profile != 0 {
        (5, 0, 0)
    } else {
        profiles(&args[3])?
    };
    coord.mixed_profile = mixed_profile;
    let stack_profile = stack_kind != 0;
    let stack_roles = stack_roles(n);
    coord.stack_profile = stack_kind;
    coord.scenario = scenario_id;
    coord.alias_profile = alias_profile;
    let source_rip = alias::source_rip(alias_profile);
    let compose = scenario_id >= 3;
    if compose && mixed_profile == 0 {
        coord.published.store(0, Ordering::Relaxed);
    }
    let delay: u64 = if compose {
        50
    } else {
        args[6].parse().map_err(|_| "invalid owner delay")?
    };
    if delay > 1000 {
        return Err("owner delay exceeds 1000ms".into());
    }
    let body = if stack_profile || mixed_profile != 0 {
        Vec::new()
    } else if compose {
        FEX_BODY.to_vec()
    } else {
        read_input(&args[5], 44)?
    };
    if !stack_profile && mixed_profile == 0 && body != FEX_BODY {
        return Err("I4 requires pinned FEX fragment".into());
    }
    let memory = Memory::with_size(progress::backing_size(n))?;
    load_resident(&memory, &args[1])?;
    if !stack_profile && mixed_profile == 0 {
        memory.copy(CODE_OFFSET, &body);
        memory.copy(CODE_OFFSET + 44, &RET);
    } else {
        memory.copy(DATA_OFFSET, &[0xa5; 8192]);
    }
    if mixed_profile != 0 {
        memory.copy(DATA_OFFSET + 4096, &[0x5a; 4096]);
    }
    if compose {
        memory.copy(
            natemu_abi::recovery::SOURCE_OFFSET,
            if mixed_profile != 0 {
                &natemu_abi::mixed32::SOURCE[..]
            } else if stack_profile {
                &stack::SOURCE[..]
            } else {
                &wire::XOR[..]
            },
        );
    }
    memory.write_record(COORD_OFFSET, coord);
    let vm_id = new_vm_nonce()?;
    let pressure = scenario_id == 5;
    if pressure {
        memory.write_record(
            natemu_abi::maintenance::OFFSET,
            natemu_abi::maintenance::State::new(n).unwrap(),
        );
    }
    let mut seeds = Vec::new();
    let mut new_data = Vec::new();
    for id in 0..n {
        let base = id * RAM_SIZE;
        if id != 0 {
            memory.replicate_resident_text(base);
        }
        tables(&memory, base, alias_profile != 0);
        if boundary_config.is_some() {
            boundary::prepare_owner(&memory, id);
        }
        if mixed_profile != 0 {
            mixed::private_code(&memory, base);
        }
        let (mut frame, mut bank, mut data) = seed(id);
        for b in &mut bank {
            *b ^= id as u8;
        }
        frame.x[6] = if stack_profile {
            stack_concurrent::initial_rsp_for(stack_kind, id as u64) - 0x80
        } else {
            alias::data_address(alias_profile, id as u64, 0)
        };
        frame.x[11] = frame.x[6] + 128;
        if stack_profile {
            frame.x[8] = stack_concurrent::initial_rsp_for(stack_kind, id as u64);
            frame.x[5] = frame.x[8];
            data.fill(0xa5);
        }
        if mixed_profile != 0 {
            mixed::seed_owner(&mut frame, &mut data, id);
        }
        memory.copy(base + BANK_OFFSET, &bank);
        memory.copy(DATA_OFFSET + id * 256, &data);
        let mut replacement = data.map(|b| b ^ 0x5a);
        if mixed_profile != 0 {
            replacement = mixed::replacement(id);
        } else if stack_profile {
            replacement.fill(0xa5);
        } else {
            replacement[..8].copy_from_slice(&(0xd00d_9876_5432_1000u64 + id as u64).to_le_bytes());
        }
        memory.copy(DATA_OFFSET + 4096 + id * 256, &replacement);
        new_data.push(replacement);
        let mut object = Object::new(LIVE_LOAD);
        if mixed_profile != 0 {
            object.code_length = 24;
        }
        if stack_profile {
            object.code_length = stack::CODE_BYTES as u64;
        }
        object.vcpu_id = id as u32;
        object.configured_vcpus = n as u32;
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
            lifecycle: if alias_profile != 0 {
                natemu_abi::translation::Lifecycle::new_at(vm_id, source_rip)
            } else {
                natemu_abi::translation::Lifecycle::new(vm_id)
            },
        };
        memory.write_record(base + SHARED_OFFSET, shared);
        if mixed_profile != 0 {
            let mode = natemu_abi::mixed32::owner_mode(mixed_profile, id as u64).unwrap();
            let mut m = natemu_abi::mixed32::State::ZERO;
            m.mode = mode;
            m.incarnation = 1;
            m.context = mode + 1;
            m.a20 = 1;
            m.initial = frame;
            memory.write_record(base + natemu_abi::mixed32::OFFSET, m);
        }
        if stack_profile {
            memory.write_record(
                base + stack_concurrent::OFFSET,
                stack_concurrent::State::ZERO,
            );
        }
        if alias_profile != 0 {
            memory.write_record(base + alias::OFFSET, alias::Record::ZERO);
        }
        seeds.push((frame, bank, data));
    }
    if args[3] != "entry" {
        recovery::prepare(&memory, &seeds[0].0, "cold", false)?;
        if alias_profile != 0 {
            memory.word(
                natemu_abi::recovery::TABLE_OFFSET + 256 * 8,
                if stack_kind == 2 { 0x2005 } else { 0x2007 },
            );
            if stack_kind == 3 {
                // A distinct high leaf aliases the writable low GPA but denies writes.
                memory.word(
                    natemu_abi::recovery::TABLE_OFFSET + 0x3000 + 257 * 8,
                    DATA_VA | 5,
                );
            }
        }
        for id in 0..n {
            memory.write_record(id * RAM_SIZE + ROUND_OFFSET, Round::ZERO);
            memory.write_record(
                id * RAM_SIZE + natemu_abi::recovery::STATE_OFFSET,
                natemu_abi::recovery::Recovery::new(0),
            );
            if scenario_id >= 4 {
                let mut guest = if mixed_profile != 0 {
                    mixed::guest(&seeds[id].0, id, mixed_profile)
                } else {
                    natemu_abi::recovery::GuestState::seed(&seeds[id].0)
                };
                guest.rip = source_rip;
                if stack_profile {
                    guest.gpr[4] = seeds[id].0.x[8];
                }
                if id as u64 == stack_roles.actor {
                    guest.rflags &= !0x200;
                }
                memory.write_record(id * RAM_SIZE + natemu_abi::recovery::GUEST_OFFSET, guest);
                let mut event =
                    EventRecord::new(&seeds[id].0, stack_roles.event_role(id as u64) as usize);
                event.packet.rip = source_rip;
                if mixed_profile != 0 {
                    event.packet = guest;
                }
                if stack_profile {
                    event.packet.gpr[4] = seeds[id].0.x[8];
                }
                memory.write_record(id * RAM_SIZE + EVENT_OFFSET, event);
            }
        }
    }
    if let Some(config) = &boundary_config {
        boundary::prepare_shared(&memory, config, n);
    }
    let mixed_pages_before = if mixed_profile != 0 {
        memory.read::<8192>(DATA_OFFSET).to_vec()
    } else {
        Vec::new()
    };
    let mixed_tables_before = if mixed_profile != 0 {
        memory
            .read::<16384>(natemu_abi::recovery::TABLE_OFFSET)
            .to_vec()
    } else {
        Vec::new()
    };
    if mixed_profile != 0 {
        fs::create_dir(&args[6]).map_err(|e| e.to_string())?;
    }
    let _machine = Machine::create(&memory)?;
    let tcr = if alias_profile != 0 {
        shadow::TCR_BOTH
    } else {
        SINGLE_ROOT_TCR
    };
    let ram = memory.0 as usize;
    let service = |id: usize| -> Result<Option<Service>> {
        if !compose || (id as u64 != stack_roles.actor && mixed_profile == 0) {
            return Ok(None);
        }
        let base = ram + id * RAM_SIZE;
        let binary = PathBuf::from(&args[5]);
        Ok(if mixed_profile != 0 {
            let mode = natemu_abi::mixed32::owner_mode(mixed_profile, id as u64).unwrap();
            let output = owner_output(&args[6], id)?;
            checked(
                mixed_producer::Compiler::new(binary, output, id as u64, mode),
                move |_| unsafe {
                    ptr::read_volatile(
                        (base + natemu_abi::mixed32::OFFSET) as *const natemu_abi::mixed32::State,
                    )
                    .expected
                },
            )
        } else if stack_profile {
            fs::create_dir(&args[6]).map_err(|e| e.to_string())?;
            checked(
                stack_producer::Compiler::new(
                    binary,
                    args[6].clone().into(),
                    source_rip,
                    id as u64,
                ),
                move |_| unsafe {
                    ptr::read_volatile(
                        (base + stack_concurrent::OFFSET) as *const stack_concurrent::State,
                    )
                    .expected
                },
            )
        } else {
            let output = args[6].clone().into();
            let compiler = if alias_profile == 0 {
                producer::Compiler::new(binary, output)?
            } else {
                producer::Compiler::new_alias(binary, output, source_rip)?
            };
            // The request names the identity the resident published for this VM.
            checked(compiler, move |packet| {
                let s = unsafe { ptr::read_volatile((base + SHARED_OFFSET) as *const Shared) };
                let mut expected = s.lifecycle.packet();
                expected[0] = packet[0];
                expected
            })
        })
    };
    let execution = run_owners(
        Owners {
            n,
            tcr,
            memory: ram,
            delayed: Some((stack_roles.delayed as usize, Duration::from_millis(delay))),
            deadline: Duration::from_secs(if compose { 100 } else { 10 }),
        },
        &service,
    );
    let requests = match execution {
        Ok(requests) => requests,
        Err(error) => {
            if let Some(config) = &boundary_config {
                // run_owners joins all owners before returning an error.
                eprintln!(
                    "boundary failure state: {}",
                    boundary::report(&memory, config, n, "null")
                );
            }
            return Err(error);
        }
    };
    // Every owner has exited and destroyed its vCPU before host reads guest RAM.
    let c = unsafe { &*memory.0.add(COORD_OFFSET).cast::<Coordinator>() };
    let boundary_shared = boundary_config.as_ref().map(|_| boundary::shared(&memory));
    let mut cells = Vec::new();
    let mut faults = Vec::new();
    let mut new_cells = Vec::new();
    let mut event_records = Vec::new();
    let mut alias_records = Vec::new();
    let mut stack_records = Vec::new();
    let mut mixed_records = Vec::new();
    for (id, (frame, bank, data)) in seeds.iter().enumerate() {
        let base = id * RAM_SIZE;
        let boundary_owner = boundary_config
            .as_ref()
            .map(|_| boundary::owner(&memory, id));
        let s = memory.read_record::<Shared>(base + SHARED_OFFSET);
        if s.status != SUCCESS || s.current_el != 4 || s.actlr & 2 != 2 || s.tcr != tcr {
            return Err(format!("CPU{id}: status={} detail={}", s.status, s.detail));
        }
        let mut entry = *frame;
        entry.x[16] = CODE_VA;
        entry.x[30] = s.capture_pc;
        if scenario_id < 2 && s.initial != entry {
            return Err(format!("CPU{id}: entry state changed"));
        }
        let r = memory.read_record::<Round>(base + ROUND_OFFSET);
        if mixed_profile != 0 {
            mixed_records.push(mixed::report(&memory, id, mixed_profile, frame));
        }
        if scenario_id >= 4 {
            let e = memory.read_record::<EventRecord>(base + EVENT_OFFSET);
            event_records.push(format!("{{\"id\":{id},\"pending\":{},\"token_state\":{},\"notifications\":{},\"consumed\":{},\"deliveries\":{},\"boundary\":{},\"blocked\":{},\"wait_checks\":{},\"shadow\":{},\"priority\":{},\"vector\":{},\"packet\":{}}}",
                u64::from(c.event_pending[id].load(Ordering::Acquire)==1),c.event_pending[id].load(Ordering::Acquire),c.event_notifications[id].load(Ordering::Acquire),
                e.consumed,e.deliveries,e.boundary,e.blocked,e.wait_checks,e.shadow,e.priority,0x40+id,
                guest_json(&e.packet)));
        }
        if alias_profile != 0 {
            let a = memory.read_record::<alias::Record>(base + alias::OFFSET);
            alias_records.push(format!("{{\"id\":{id},\"current_el\":{},\"sctlr\":{},\"actlr\":{},\"tcr\":{},\"ttbr1\":{},\"warm\":{:?},\"ack_count\":{},\"acks\":{:?},\"wrong_rip_rejections\":{},\"blank_before_publish\":{},\"source_address\":{},\"source_gpa\":{},\"source_leaf_before\":{},\"source_leaf_after\":{},\"source_before\":{:?},\"source_after\":{:?},\"upper_root\":{:?},\"upper_l1\":{:?},\"upper_l2\":{:?},\"upper_leaf\":{:?},\"lower_leaf\":{:?}}}",
                s.current_el,s.sctlr,s.actlr,s.tcr,a.ttbr1,a.warm,a.ack_count,a.acks,
                a.wrong_rip_rejections,a.blank_before_publish,a.source_address,a.source_gpa,a.source_leaf_before,a.source_leaf_after,a.source_before,a.source_after,
                table_words(&memory,base+shadow::UPPER_ROOT),table_words(&memory,base+shadow::UPPER_L1),
                table_words(&memory,base+shadow::UPPER_L2),
                boundary_owner.as_ref().map_or_else(|| table_words(&memory,base+shadow::UPPER_LEAF), |o| o.upper_before),
                boundary_owner.as_ref().map_or_else(|| table_words(&memory,base+shadow::LOWER_LEAF), |o| o.lower_before)));
        }
        if stack_profile {
            let state =
                memory.read_record::<stack_concurrent::State>(base + stack_concurrent::OFFSET);
            let mut before = natemu_abi::recovery::GuestState::seed(frame);
            before.rip = source_rip;
            before.gpr[4] = frame.x[8];
            if id as u64 == stack_roles.actor {
                before.rflags &= !0x200;
            }
            let after = boundary_owner.as_ref().map_or_else(
                || {
                    memory.read_record::<natemu_abi::recovery::GuestState>(
                        base + natemu_abi::recovery::GUEST_OFFSET,
                    )
                },
                |o| o.before_guest,
            );
            let programs = state
                .programs
                .iter()
                .map(|p| {
                    format!(
                        "{{\"entries\":{},\"completed\":{},\"prefixes\":{:?}}}",
                        p.entries, p.completed, p.prefixes
                    )
                })
                .collect::<Vec<_>>()
                .join(",");
            let p = &state.probe;
            let probe = format!(
                "{{\"initial\":{},\"captured\":{},\"guest_before\":{},\"guest_after\":{},\"pair\":{:?},\"tables_before\":{:?},\"tables_after\":{:?},\"vector\":{},\"error\":{},\"address\":{},\"rip\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{},\"done\":{}}}",
                frame_json(&p.initial),
                frame_json(&p.captured),
                guest_json(&p.guest_before),
                guest_json(&p.guest_after),
                p.pair,
                p.tables_before,
                p.tables_after,
                p.vector,
                p.error,
                p.address,
                p.rip,
                p.esr,
                p.far,
                p.pc,
                p.spsr,
                p.done
            );
            stack_records.push(format!("{{\"id\":{id},\"step\":{},\"pass\":{},\"code\":{},\"expected\":{:?},\"compile_requests\":{:?},\"replies\":{:?},\"programs\":[{programs}],\"probe\":{probe},\"stale_chunks\":{},\"wrong_owner_rejections\":{},\"blank_before_publish\":{},\"source_before\":{:?},\"source_after\":{:?},\"guest_before\":{},\"guest_after\":{}}}",
                state.step,state.pass,state.code,state.expected,state.compile_requests,state.replies,state.stale_chunks,state.wrong_owner_rejections,state.blank_before_publish,
                state.source_before,state.source_after,guest_json(&before),guest_json(&after)));
        }
        if args[3] != "entry" {
            faults.push(format!("{{\"id\":{id},\"faults\":{},\"old_gpa\":{},\"frame\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{}}}",
                r.faults,r.old_gpa,frame_json(&r.fault_frame),r.fault_esr,r.fault_far,r.fault_pc,r.fault_spsr));
        }
        let baseline = if pressure {
            Some(memory.read_record::<natemu_abi::maintenance::Baseline>(
                base + natemu_abi::maintenance::BASELINE_OFFSET,
            ))
        } else {
            None
        };
        let second_frame = baseline.as_ref().map_or(&s.captured, |b| &b.frame);
        let mut second_memory = baseline.as_ref().map_or_else(
            || memory.read::<256>(DATA_OFFSET + 4096 + id * 256),
            |b| b.new_memory,
        );
        let mut first_memory = baseline.as_ref().map_or_else(
            || memory.read::<256>(DATA_OFFSET + id * 256),
            |b| b.old_memory,
        );
        let mut second_before = new_data[id];
        if mixed_profile != 0 {
            let observed = mixed::state(&memory, base);
            first_memory = observed.old_memory_after;
            second_before = observed.new_memory_before;
            second_memory = observed.new_memory_after;
        }
        if scenario_id >= 2 {
            if !pressure && s.initial != r.new_initial {
                return Err("new entry state changed".into());
            }
            let code = if compose { CODE_NEXT } else { CODE_VA };
            let case = if mixed_profile != 0 {
                "fex_mixed32_changed"
            } else if stack_profile {
                "fex_push_immediates64_changed"
            } else if compose {
                "live_or"
            } else {
                "live_load"
            };
            let dirty = format!(
                "{{\"frame\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{}}}",
                frame_json(&r.dirty_frame),
                r.dirty_esr,
                r.dirty_far,
                r.dirty_pc,
                r.dirty_spsr
            );
            new_cells.push(format!("{{\"dirty_fault\":{dirty},\"id\":{id},\"dirty_faults\":{},\"case\":\"{case}\",\"status\":{},\"initial\":{},\"captured\":{},\"memory_base\":{},\"memory_before\":{:?},\"memory_after\":{:?},\"bank_before\":{bank:?},\"bank_after\":{:?},\"object_pc\":{code},\"capture_pc\":{}}}",
                r.dirty_faults,s.status,frame_json(&r.new_initial),frame_json(second_frame),if stack_profile {stack_concurrent::initial_rsp_for(stack_kind,id as u64)-0x80} else {r.new_initial.x[6]},
                second_before,second_memory,memory.read::<512>(base+BANK_OFFSET),s.capture_pc));
        }
        let old_frame = if scenario_id >= 2 {
            &r.first_frame
        } else {
            &s.captured
        };
        let case = if mixed_profile != 0 {
            "fex_mixed32_old"
        } else if stack_profile {
            "fex_push_immediates64"
        } else {
            "live_load"
        };
        cells.push(format!("{{\"id\":{id},\"case\":\"{case}\",\"status\":{},\"initial\":{},\"captured\":{},\"memory_base\":{},\"memory_before\":{data:?},\"memory_after\":{:?},\"bank_before\":{bank:?},\"bank_after\":{:?},\"object_pc\":{CODE_VA},\"capture_pc\":{}}}",
            s.status,frame_json(frame),frame_json(old_frame),frame.x[6],
            first_memory,memory.read::<512>(base+BANK_OFFSET),s.capture_pc));
    }
    let trace: Vec<_> = c
        .trace
        .iter()
        .take(c.trace_length.load(Ordering::Acquire) as usize)
        .map(|v| v.load(Ordering::Relaxed))
        .collect();
    let scenario = boundary_config.as_ref().map_or(args[3].as_str(), |config| {
        if config.alias_profile == 1 {
            "stack-low"
        } else {
            "stack-high"
        }
    });
    let source_summary = format!(
        "{{\"old_eligible\":{},\"published\":{},\"stale_replies\":{},\"stale_entries\":{},\"reuse_blocked\":{},\"bytes\":{:?},\"old_code\":{:?},\"new_code\":{:?}}}",
        c.old_eligible.load(Ordering::Acquire),
        c.published.load(Ordering::Acquire),
        c.stale_replies.load(Ordering::Acquire),
        c.stale_entries.load(Ordering::Acquire),
        c.reuse_blocked.load(Ordering::Acquire),
        if mixed_profile != 0 {
            memory
                .read::<6>(natemu_abi::recovery::SOURCE_OFFSET)
                .to_vec()
        } else if stack_profile {
            memory
                .read::<20>(natemu_abi::recovery::SOURCE_OFFSET)
                .to_vec()
        } else {
            memory
                .read::<17>(natemu_abi::recovery::SOURCE_OFFSET)
                .to_vec()
        },
        if mixed_profile != 0 {
            memory.read::<24>(CODE_OFFSET).to_vec()
        } else if stack_profile {
            memory.read::<{ stack::CODE_BYTES }>(CODE_OFFSET).to_vec()
        } else {
            memory.read::<48>(CODE_OFFSET).to_vec()
        },
        if mixed_profile != 0 {
            memory.read::<24>(CODE_OFFSET + 4096).to_vec()
        } else if let Some(b) = boundary_shared {
            b.code_before.to_vec()
        } else if stack_profile {
            memory
                .read::<{ stack::CODE_BYTES }>(CODE_OFFSET + 4096)
                .to_vec()
        } else {
            memory.read::<48>(CODE_OFFSET + 4096).to_vec()
        }
    );
    let maintenance = if pressure {
        Some(memory.read_record::<natemu_abi::maintenance::State>(natemu_abi::maintenance::OFFSET))
    } else {
        None
    };
    let final_leaf = boundary_shared.map_or_else(
        || {
            u64::from_le_bytes(
                memory.read::<8>(natemu_abi::recovery::TABLE_OFFSET + 0x3000 + 256 * 8),
            )
        },
        |b| u64::from_le_bytes(b.tables_before[0x3800..0x3808].try_into().unwrap()),
    );
    let compose_leaf = maintenance.as_ref().map_or(final_leaf, |m| m.compose_leaf);
    let maintenance_json = if let Some(m) = maintenance {
        let mut intervals = Vec::new();
        for id in 0..n {
            if m.intervals[id] > natemu_abi::maintenance::MAX_INTERVALS as u64 {
                return Err("interval reserve exceeded".into());
            }
            let state = memory.read_record::<Shared>(id * RAM_SIZE + SHARED_OFFSET);
            for index in 0..m.intervals[id] as usize {
                let r = memory.read_record::<natemu_abi::maintenance::Interval>(
                    id * RAM_SIZE + natemu_abi::maintenance::RECORD_OFFSET + index * 4096,
                );
                let case = if mixed_profile != 0 {
                    "fex_mixed32_changed"
                } else if stack_profile {
                    "fex_push_immediates64_changed"
                } else {
                    "live_or"
                };
                intervals.push(format!("{{\"id\":{id},\"ordinal\":{},\"heads\":{},\"epoch\":{},\"gpa\":{},\"case\":\"{case}\",\"status\":1,\"initial\":{},\"captured\":{},\"memory_base\":{},\"memory_before\":{:?},\"memory_after\":{:?},\"bank_before\":{:?},\"bank_after\":{:?},\"object_pc\":{CODE_NEXT},\"capture_pc\":{},\"dirty_faults\":{},\"dirty_fault\":{{\"frame\":{},\"esr\":{},\"far\":{},\"pc\":{},\"spsr\":{}}}}}",
                    r.ordinal,r.heads,r.epoch,r.gpa,frame_json(&r.initial),frame_json(&r.captured),if stack_profile {stack_concurrent::initial_rsp_for(stack_kind,id as u64)-0x80} else {r.initial.x[6]},
                    r.before,r.after,seeds[id].1,memory.read::<512>(id*RAM_SIZE+BANK_OFFSET),state.capture_pc,
                    r.faults,frame_json(&r.dirty_frame),r.esr,r.far,r.pc,r.spsr));
            }
        }
        let request_history: Vec<[u64; 2]> = m.request_history[..(m.total as usize).min(36)]
            .iter()
            .map(|r| [r.cpu, r.serial])
            .collect();
        format!(
            "{{\"serial\":{},\"request_history\":{request_history:?},\"ready\":{},\"submitted\":{:?},\"completed\":{:?},\"pending\":{:?},\"length\":{},\"current\":[{},{}],\"epoch\":{},\"total\":{},\"batch\":{},\"next_ordinary\":{},\"ordinary\":{},\"ordinary_total\":{},\"interval_counts\":{:?},\"finished\":{},\"duplicate_rejections\":{},\"maximum_depth\":{},\"leaf_history\":{:?},\"leaf_final\":{final_leaf},\"intervals\":[{}]}}",
            m.serial,
            m.ready,
            m.submitted,
            m.completed,
            m.pending,
            m.length,
            m.current.cpu,
            m.current.serial,
            m.epoch,
            m.total,
            m.batch,
            m.next_ordinary,
            m.ordinary,
            m.ordinary_total,
            m.intervals,
            m.finished,
            m.duplicate_rejections,
            m.maximum_depth,
            &m.leaf_history[..(m.total as usize).min(36)],
            intervals.join(",")
        )
    } else {
        "null".into()
    };
    let summary = format!(
        "{{\"generation\":{},\"acknowledgements\":{},\"active\":{},\"new_entries\":{},\"new_completed\":{},\"new_references\":{},\"reclaimed\":{},\"leaf\":{}}}",
        c.generation.load(Ordering::Acquire),
        c.acknowledgements.load(Ordering::Acquire),
        c.active.load(Ordering::Acquire),
        c.new_entries.load(Ordering::Acquire),
        c.new_completed.load(Ordering::Acquire),
        c.new_references.load(Ordering::Acquire),
        c.reclaimed.load(Ordering::Acquire),
        compose_leaf
    );
    let mixed_pages_after = if mixed_profile != 0 {
        memory.read::<8192>(DATA_OFFSET).to_vec()
    } else {
        Vec::new()
    };
    let mixed_tables_after = if mixed_profile != 0 {
        memory
            .read::<16384>(natemu_abi::recovery::TABLE_OFFSET)
            .to_vec()
    } else {
        Vec::new()
    };
    let mixed_compiled = c.mixed_compiled.load(Ordering::Acquire);
    let mixed_json = mixed_records.join(",");
    let stack_flag = stack_kind;
    let stack_memory_before = if stack_profile {
        vec![0xa5u8; 8192]
    } else {
        Vec::new()
    };
    let stack_memory_after = if stack_profile {
        boundary_shared.map_or_else(
            || memory.read::<8192>(DATA_OFFSET).to_vec(),
            |b| b.data_before.to_vec(),
        )
    } else {
        Vec::new()
    };
    let role_report =
        if progress::configuration().is_some_and(|c| c.family == natemu_abi::schedule::STACK) {
            format!(
                "\"actor\":{},\"delayed_owner\":{},",
                stack_roles.actor, stack_roles.delayed
            )
        } else {
            String::new()
        };
    let prefix = format!(
        "{{{role_report}\"mixed_profile\":{mixed_profile},\"mixed_compiled\":{mixed_compiled},\"mixed\":[{mixed_json}],\"mixed_pages_before\":{mixed_pages_before:?},\"mixed_pages_after\":{mixed_pages_after:?},\"mixed_tables_before\":{mixed_tables_before:?},\"mixed_tables_after\":{mixed_tables_after:?},\"stack_profile\":{stack_flag},\"stacks\":[{}],\"stack_memory_before\":{stack_memory_before:?},\"stack_memory_after\":{stack_memory_after:?},\"alias_profile\":{alias_profile},\"source_rip\":{source_rip},\"aliases\":[{}],\"maintenance\":{maintenance_json},\"events\":[{}],\"source\":{source_summary},\"requests\":{requests:?},\"coherence\":{summary},\"new_cells\":[{}],\"profile\":\"i4-{scenario}\",\"trace\":{trace:?},\"faults\":[{}],\"retained\":{},\"configured_vcpus\":{n},\"tested_vcpus\":{n},\"delayed_owner_ms\":{delay},\"ready\":{},\"finished\":{},\"cells\":[{}]}}",
        stack_records.join(","),
        alias_records.join(","),
        event_records.join(","),
        new_cells.join(","),
        faults.join(","),
        c.retained.load(Ordering::Acquire),
        c.ready.load(Ordering::Acquire),
        boundary_shared.map_or_else(|| c.finished.load(Ordering::Acquire), |b| b.finished_before),
        cells.join(",")
    );
    if let Some(config) = &boundary_config {
        println!("{}", boundary::report(&memory, config, n, &prefix));
    } else {
        println!("{prefix}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_owner_maps_its_extended_resident_text_read_only() {
        let memory = Memory::with_size(2 * RAM_SIZE).unwrap();
        for id in 0..2 {
            let base = id * RAM_SIZE;
            tables(&memory, base, true);
            for offset in (0..RAM_SIZE).step_by(4096) {
                let pte = u64::from_le_bytes(memory.read::<8>(base + 0x14000 + offset / 4096 * 8));
                if resident::executable_offset(offset) {
                    assert_eq!(
                        pte,
                        (IPA + (base + offset) as u64) | 3 | 1 << 10 | 3 << 8 | 1 << 7
                    );
                } else if (resident::BOOT_END..0x10000).contains(&offset) {
                    assert_eq!(pte, 0);
                } else if pte != 0 && !(CODE_OFFSET..CODE_OFFSET + 8192).contains(&offset) {
                    assert_eq!(pte & (3 << 53), 3 << 53);
                }
            }
        }
    }

    #[test]
    fn rotated_writer_roots_share_code_and_source_but_keep_private_page_tables() {
        let memory = Memory::with_size(4 * RAM_SIZE).unwrap();
        let address_mask = (1u64 << 48) - 4096;
        for id in 0..4 {
            let base = id * RAM_SIZE;
            tables(&memory, base, true);
            for offset in [
                CODE_OFFSET,
                CODE_OFFSET + 4096,
                natemu_abi::recovery::SOURCE_OFFSET,
            ] {
                let entry =
                    u64::from_le_bytes(memory.read::<8>(base + 0x14000 + offset / 4096 * 8));
                assert_eq!(entry & address_mask, IPA + offset as u64);
            }
            let private = u64::from_le_bytes(memory.read::<8>(base + 0x14000 + 0x14000 / 4096 * 8));
            assert_eq!(private & address_mask, IPA + (base + 0x14000) as u64);
        }
    }
    #[test]
    fn mixed_permissions_are_admitted_only_as_full_pressure_profiles() {
        for (name, stack_kind, alias_kind) in [
            ("stack-parent-low", 2, 1),
            ("stack-parent-high", 2, 2),
            ("stack-leaf-low", 3, 1),
            ("stack-leaf-high", 3, 2),
        ] {
            assert_eq!(profiles(name).unwrap(), (5, alias_kind, stack_kind));
            for id in 0..4 {
                assert_eq!(
                    stack_concurrent::initial_rsp_for(stack_kind, id),
                    DATA_VA + id * 256 + 0x80,
                );
            }
        }
        for unsupported in [
            "stack-parent",
            "stack-leaf",
            "stack-parent-low-extra",
            "stack-leaf-middle",
        ] {
            assert!(profiles(unsupported).is_err());
        }
        assert_eq!(profiles("stack-low").unwrap(), (5, 1, 1));
        assert_eq!(profiles("stack-high").unwrap(), (5, 2, 1));
        assert_eq!(profiles("pressure").unwrap(), (5, 0, 0));
    }
}
