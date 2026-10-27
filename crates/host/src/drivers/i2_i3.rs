//! I2/I3: one vCPU driving the resident translation lifecycle through scripted
//! producer exchanges (stale/cancel/capacity/bad-reply scenarios, paging
//! recovery cases and allocated exports).
use super::*;
use crate::translation::{Producer, start_worker};
use natemu_abi::translation as wire;
use std::path::{Path, PathBuf};

struct Session {
    vm: Vm,
    initial: NativeFrame,
    bank: [u8; 512],
    data: [u8; 256],
    events: Vec<String>,
    executions: Vec<String>,
    requests: Vec<wire::Packet>,
    worker: PathBuf,
    producer: PathBuf,
    output: PathBuf,
}
impl Session {
    fn event(&mut self, expected: u64) -> Result<wire::Packet> {
        self.vm.run(0x4a)?;
        let s = self.vm.shared();
        let pc = self.vm.cpu.reg(PC)?;
        if !resident::contains(s.service_pc, 4) || pc != s.service_pc {
            return Err(format!(
                "not at transfer service: PC={pc:#x} expected={:#x}",
                s.service_pc
            ));
        }
        let mut p = [0; 16];
        for (i, v) in p.iter_mut().enumerate() {
            *v = self.vm.cpu.reg(i as u32)?;
        }
        if p != s.lifecycle.packet() || p[0] != expected {
            return Err(format!(
                "unexpected resident event: got {}, expected {expected}, state={:?}",
                p[0], s.lifecycle
            ));
        }
        self.record("resident", p[0], p);
        if p[0] == wire::EXECUTED {
            self.capture(s)?;
        }
        Ok(p)
    }
    fn record(&mut self, origin: &str, event: u64, packet: wire::Packet) {
        let s = self.vm.shared().lifecycle;
        self.events.push(format!("{{\"origin\":\"{origin}\",\"event\":{event},\"packet\":{packet:?},\"pending\":{},\"cached\":{},\"rejections\":{},\"busy\":{},\"cancellations\":{}}}",s.pending,s.cached,s.rejections,s.busy,s.cancellations));
    }
    fn send(&mut self, packet: wire::Packet, expected: u64) -> Result<wire::Packet> {
        // Only event() reaches this explicit service PC; never write mapped RAM.
        for (i, &v) in packet.iter().enumerate() {
            self.vm.cpu.set_reg(i as u32, v)?;
        }
        self.event(expected)
    }
    fn capture(&mut self, s: Shared) -> Result<()> {
        let case = match s.lifecycle.variant {
            0 => "live_load",
            1 => "live_or",
            2 => "allocated_xor",
            _ => "allocated_fp",
        };
        let after = self.vm.memory.read::<256>(DATA_OFFSET);
        let bank_after = self.vm.memory.read::<512>(BANK_OFFSET);
        self.executions.push(format!("{{\"case\":\"{case}\",\"status\":1,\"initial\":{},\"captured\":{},\"memory_base\":{DATA_VA},\"memory_before\":{:?},\"memory_after\":{after:?},\"bank_before\":{:?},\"bank_after\":{bank_after:?},\"object_pc\":{CODE_VA},\"capture_pc\":{},\"source_generation\":{},\"context_generation\":{}}}",frame_json(&self.initial),frame_json(&s.captured),self.data,self.bank,s.capture_pc,s.lifecycle.source_generation,s.lifecycle.context_generation));
        self.initial = s.captured;
        self.data = after;
        Ok(())
    }
    fn start_producer(&mut self, request: wire::Packet) -> Result<Producer> {
        let directory = self
            .output
            .join(format!("request-{:03}", self.requests.len()));
        let worker = start_worker(&self.worker, &self.producer, &directory, request)?;
        self.requests.push(request);
        Ok(worker)
    }
    fn deliver_export(&mut self, request: wire::Packet, terminal: u64) -> Result<()> {
        use natemu_abi::pressure::*;
        let mut worker = self.start_producer(request)?;
        if !worker.child.wait().map_err(|e| e.to_string())?.success() {
            return Err("allocated FEX worker failed".into());
        }
        let echoed = read_input(
            worker
                .directory
                .join("reply-request.bin")
                .to_str()
                .ok_or("reply path")?,
            128,
        )?;
        if echoed
            != request
                .iter()
                .flat_map(|x| x.to_le_bytes())
                .collect::<Vec<_>>()
        {
            return Err("export not bound to original request".into());
        }
        let bytes = read_input(
            worker
                .directory
                .join("export.bin")
                .to_str()
                .ok_or("export path")?,
            EXPORT_BYTES,
        )?;
        if bytes.len() != EXPORT_BYTES {
            return Err("bad export size".into());
        }
        let export = unsafe { ptr::read_unaligned(bytes.as_ptr().cast::<Export>()) };
        let source = self.vm.shared().lifecycle.source();
        export
            .validate(&source)
            .map_err(|e| format!("bad allocated export: {e}"))?;
        let packet = |command| {
            let mut p = wire::command(command);
            p[2..6].copy_from_slice(&request[2..6]);
            p
        };
        let mut begin = packet(BEGIN);
        begin[6] = EXPORT_BYTES as u64;
        self.send(begin, ACK)?;
        for (i, chunk) in bytes.chunks(72).enumerate() {
            let mut p = packet(CHUNK);
            p[6] = (i * 72) as u64;
            wire::put_bytes(&mut p[7..], chunk);
            self.send(p, ACK)?;
        }
        self.send(packet(COMMIT), terminal)?;
        Ok(())
    }
    fn deliver(&mut self, reply: wire::Packet, expected: u64) -> Result<()> {
        match self.vm.shared().lifecycle.validate_reply(&reply) {
            Ok(_) => {
                if expected != wire::EXECUTED {
                    return Err("expected rejection but host admitted reply".into());
                }
                self.send(reply, wire::EXECUTED)?;
            }
            Err(error) => {
                if error != expected {
                    return Err(format!("host rejection={error}, expected={expected}"));
                }
                self.record("host", error, self.vm.shared().lifecycle.packet());
            }
        }
        Ok(())
    }
    fn compile_and_deliver(&mut self, request: wire::Packet) -> Result<()> {
        let reply = self.start_producer(request)?.finish()?;
        self.deliver(reply, wire::EXECUTED)
    }
    fn source_change(&mut self) -> Result<()> {
        let mut p = wire::command(wire::SOURCE);
        p[6] = 17;
        wire::put_bytes(&mut p[7..10], &wire::OR);
        self.send(p, wire::CHANGED)?;
        Ok(())
    }
}

pub fn run(args: &[String]) -> Result<()> {
    let scenario = args[3].as_str();
    let allocated = args[2] == "i3-pressure"
        || args[2] == "i3-fp"
        || args[2] == "i3-helper"
        || args[2] == "i3-partial";
    let fp = args[2] == "i3-fp";
    let i3 = args[2] == "i3" || allocated;
    if !i3
        && ![
            "live",
            "source_stale",
            "context_stale",
            "cancel",
            "capacity",
            "bad_reply",
        ]
        .contains(&scenario)
    {
        return Err("unknown I2 scenario".into());
    }
    let output = Path::new(&args[6]).to_path_buf();
    fs::create_dir(&output).map_err(|e| e.to_string())?;
    let memory = Memory::new()?;
    load_resident(&memory, &args[1])?;
    single_owner_space(&memory, false);
    let (mut initial, bank, data) = seed(match scenario {
        "carry" => 1,
        "overflow" => 2,
        _ => 3,
    });
    if allocated {
        initial.x[4] = DATA_VA;
    }
    if fp {
        for i in 0..4 {
            initial.q[16][i * 4..i * 4 + 4].copy_from_slice(&0x3f800000u32.to_le_bytes());
            initial.q[17][i * 4..i * 4 + 4].copy_from_slice(&0x33000000u32.to_le_bytes());
        }
    }
    memory.copy(BANK_OFFSET, &bank);
    memory.copy(DATA_OFFSET, &data);
    let cookie = new_vm_nonce()?;
    let paging_before = if i3 {
        Some(super::recovery::prepare(
            &memory, &initial, scenario, allocated,
        )?)
    } else {
        None
    };
    if args[2] == "i3-partial" {
        memory.copy(natemu_abi::recovery::SOURCE_OFFSET + 256, &[0xf3, 0xa4]);
    }
    let tables_before = memory.read::<16384>(natemu_abi::recovery::TABLE_OFFSET);
    let shared = Shared {
        schedule: 0,
        object: Object::new(LIVE_LOAD),
        initial,
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
        mode: if args[2] == "i3-partial" {
            natemu_abi::recovery::PARTIAL_MODE
        } else if args[2] == "i3-helper" {
            natemu_abi::recovery::HELPER_MODE
        } else if allocated {
            if fp {
                natemu_abi::pressure::FP_MODE
            } else {
                natemu_abi::pressure::MODE
            }
        } else if i3 {
            natemu_abi::recovery::MODE
        } else {
            wire::MODE
        },
        lifecycle: wire::Lifecycle::new(cookie),
    };
    memory.write_record(SHARED_OFFSET, shared);
    let vm = Vm::new(memory)?;
    let tcr = SINGLE_ROOT_TCR;
    vm.cpu.enter_resident(0, tcr)?;
    let mut session = Session {
        vm,
        initial,
        bank,
        data,
        events: Vec::new(),
        executions: Vec::new(),
        requests: Vec::new(),
        worker: PathBuf::from(&args[4]),
        producer: PathBuf::from(&args[5]),
        output,
    };
    session.event(wire::READY)?;
    if i3 {
        let early = matches!(scenario, "fetch_nx" | "smep");
        let terminal = if matches!(scenario, "cold" | "wp_off" | "carry" | "overflow" | "large") {
            wire::EXECUTED
        } else {
            natemu_abi::recovery::PAGE_FAULT
        };
        let request = session.send(
            wire::command(wire::DISPATCH),
            if early { terminal } else { wire::MISS },
        )?;
        if !early {
            if allocated {
                session.deliver_export(request, terminal)?;
            } else {
                let reply = session.start_producer(request)?.finish()?;
                session
                    .vm
                    .shared()
                    .lifecycle
                    .validate_reply(&reply)
                    .map_err(|e| format!("I3 host reply admission: {e}"))?;
                session.send(reply, terminal)?;
            }
            if scenario == "cold" && !allocated {
                session.send(wire::command(wire::DISPATCH), wire::EXECUTED)?;
            }
        }
    } else {
        let first = session.send(wire::command(wire::DISPATCH), wire::MISS)?;
        match scenario {
            "live" => {
                session.compile_and_deliver(first)?;
                session.send(wire::command(wire::DISPATCH), wire::EXECUTED)?;
                session.source_change()?;
                let next = session.send(wire::command(wire::DISPATCH), wire::MISS)?;
                session.compile_and_deliver(next)?;
                session.send(wire::command(wire::CONTEXT), wire::CHANGED)?;
                let next = session.send(wire::command(wire::DISPATCH), wire::MISS)?;
                session.compile_and_deliver(next)?;
            }
            "source_stale" | "context_stale" | "cancel" => {
                let old = session.start_producer(first)?;
                if scenario == "source_stale" {
                    session.source_change()?;
                } else if scenario == "context_stale" {
                    session.send(wire::command(wire::CONTEXT), wire::CHANGED)?;
                } else {
                    let mut p = wire::command(wire::CANCEL);
                    p[2] = first[2];
                    p[5] = first[5];
                    session.send(p, wire::CANCELLED)?;
                }
                let next = session.send(wire::command(wire::DISPATCH), wire::MISS)?;
                let stale = old.finish()?;
                session.deliver(stale, wire::STALE)?;
                // Adversarial transport injection independently exercises resident guard.
                session.send(stale, wire::STALE)?;
                session.compile_and_deliver(next)?;
            }
            "capacity" => {
                session.send(wire::command(wire::DISPATCH), wire::BUSY)?;
                session.compile_and_deliver(first)?;
                session.send(wire::command(wire::DISPATCH), wire::EXECUTED)?;
            }
            "bad_reply" => {
                let good = session.start_producer(first)?.finish()?;
                let mut bad = good;
                bad[5] ^= 1;
                session.deliver(bad, wire::STALE)?;
                session.send(bad, wire::STALE)?;
                bad = good;
                bad[7] ^= 1;
                session.deliver(bad, wire::BAD_REPLY)?;
                session.send(bad, wire::BAD_REPLY)?;
                session.deliver(good, wire::EXECUTED)?;
            }
            _ => unreachable!(),
        }
    }
    if args[2] == "i3-partial" {
        session.send(
            wire::command(natemu_abi::recovery::PARTIAL_COMMAND),
            natemu_abi::recovery::PARTIAL_DONE,
        )?;
    }
    let p = wire::command(wire::SHUTDOWN);
    for (i, &v) in p.iter().enumerate() {
        session.vm.cpu.set_reg(i as u32, v)?;
    }
    session.vm.run(0x49)?;
    let s = session.vm.shared();
    if s.status != SUCCESS
        || s.lifecycle.pending != 0
        || s.current_el != 4
        || s.sctlr & 0x1005 != 0x1005
        || s.actlr & 2 != 2
        || s.tcr != tcr
    {
        return Err("bad final runtime state/profile".into());
    }
    let extra = if let Some(before) = paging_before {
        format!(
            ",\"paging\":{}",
            super::recovery::report(&session.vm, &before, &tables_before)
        )
    } else {
        String::new()
    };
    let extra = if allocated {
        format!(
            "{extra},\"allocated\":{}",
            super::recovery::allocated_report(
                &session.vm,
                &initial,
                if s.lifecycle.executions > 0 {
                    &session.initial
                } else {
                    &s.captured
                }
            )
        )
    } else {
        extra
    };
    let extra = if args[2] == "i3-helper" {
        format!(
            "{extra},\"helper\":{}",
            super::recovery::helper_report(&session.vm)
        )
    } else {
        extra
    };
    let extra = if args[2] == "i3-partial" {
        format!(
            "{extra},\"partial\":{}",
            super::recovery::partial_report(&session.vm)
        )
    } else {
        extra
    };
    let case = if args[2] == "i3-partial" {
        "i3-partial"
    } else if args[2] == "i3-helper" {
        "i3-helper"
    } else if allocated {
        if fp { "i3-fp" } else { "i3-pressure" }
    } else if i3 {
        "i3"
    } else {
        "i2"
    };
    println!(
        "{{\"case\":\"{case}\",\"scenario\":\"{scenario}\",\"vm\":{cookie},\"events\":[{}],\"executions\":[{}],\"requests\":{:?},\"final_pending\":{},\"final_executions\":{},\"final_status\":{}{extra}}}",
        session.events.join(","),
        session.executions.join(","),
        session.requests,
        s.lifecycle.pending,
        s.lifecycle.executions,
        s.status
    );

    Ok(())
}

