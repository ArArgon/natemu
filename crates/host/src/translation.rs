use super::*;
use natemu_abi::translation as wire;
use std::{
    fs::File,
    io::Write,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
};

struct Producer {
    child: Child,
    directory: PathBuf,
    request: wire::Packet,
}
impl Producer {
    fn finish(mut self) -> Result<wire::Packet> {
        if !self.child.wait().map_err(|e| e.to_string())?.success() {
            return Err(format!("FEX worker failed: {}", self.directory.display()));
        }
        let raw = read_input(
            self.directory
                .join("reply.bin")
                .to_str()
                .ok_or("non-UTF8 reply path")?,
            128,
        )?;
        bind_reply(&self.request, &raw)
    }
}
impl Drop for Producer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn bind_reply(request: &wire::Packet, raw: &[u8]) -> Result<wire::Packet> {
    if raw.len() != 128 {
        return Err("wrong reply packet size".into());
    }
    let mut packet = [0; 16];
    for (i, word) in packet.iter_mut().enumerate() {
        *word = u64::from_le_bytes(raw[i * 8..i * 8 + 8].try_into().unwrap());
    }
    let mut body = [0; 44];
    wire::get_bytes(&packet[7..13], &mut body);
    let expected = wire::reply(request, &body).map_err(|_| "unsupported worker output")?;
    if packet != expected {
        return Err("worker reply differs from its original request identity/source".into());
    }
    Ok(packet)
}

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
        let pc = self.vm.read_reg(31)?;
        if s.service_pc < PRIVATE || s.service_pc >= PRIVATE + 0x10000 || pc != s.service_pc {
            return Err(format!(
                "not at transfer service: PC={pc:#x} expected={:#x}",
                s.service_pc
            ));
        }
        let mut p = [0; 16];
        for (i, v) in p.iter_mut().enumerate() {
            *v = self.vm.read_reg(i as u32)?;
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
            self.vm.reg(i as u32, v)?;
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
        if request[0] != wire::MISS {
            return Err("compiler requested without a resident miss".into());
        }
        let directory = self
            .output
            .join(format!("request-{:03}", self.requests.len()));
        fs::create_dir(&directory).map_err(|e| e.to_string())?;
        let mut file =
            File::create_new(directory.join("request.bin")).map_err(|e| e.to_string())?;
        for word in request {
            file.write_all(&word.to_le_bytes())
                .map_err(|e| e.to_string())?;
        }
        let child = Command::new("python3")
            .arg("-B")
            .arg(&self.worker)
            .arg("--request-dir")
            .arg(&directory)
            .arg("--producer")
            .arg(&self.producer)
            .stdin(Stdio::null())
            .stdout(File::create_new(directory.join("worker.stdout")).map_err(|e| e.to_string())?)
            .stderr(File::create_new(directory.join("worker.stderr")).map_err(|e| e.to_string())?)
            .spawn()
            .map_err(|e| e.to_string())?;
        self.requests.push(request);
        Ok(Producer {
            child,
            directory,
            request,
        })
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
    load_elf(&memory, &read_input(&args[1], 2 * 1024 * 1024)?)?;
    page_tables(&memory);
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
    let mut cookie = 0u64;
    if unsafe { getentropy((&mut cookie as *mut u64).cast(), 8) } != 0 || cookie == 0 {
        return Err("VM incarnation entropy unavailable".into());
    }
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
    unsafe {
        ptr::write(memory.0.add(SHARED_OFFSET).cast::<Shared>(), shared);
        sys_icache_invalidate(memory.0.cast(), RAM_SIZE);
    }
    let vm = Vm::new(memory)?;
    if (vm.read_sys(0xc030)? >> 52) & 15 == 0 {
        return Err("FlagM required".into());
    }
    let tcr = 16 | 1 << 8 | 1 << 10 | 3 << 12 | 1 << 23 | 2 << 32;
    vm.reg(31, IPA)?;
    vm.reg(34, 0x3c5)?;
    vm.reg(0, IPA + 0x10000)?;
    vm.reg(1, tcr)?;
    vm.reg(2, 0xff)?;
    vm.sys(0xc080, 0x30d00980)?;
    vm.sys(0xc081, 2)?;
    vm.sys(0xc082, 3 << 20)?;
    vm.sys(0xdf19, 0)?;
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
        session.vm.reg(i as u32, v)?;
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

#[cfg(test)]
mod tests {
    use super::*;
    fn bytes(p: wire::Packet) -> Vec<u8> {
        p.into_iter().flat_map(u64::to_le_bytes).collect()
    }
    #[test]
    fn old_worker_cannot_impersonate_replacement_request() {
        let mut state = wire::Lifecycle::new(9);
        state.dispatch().unwrap();
        let old = state.packet();
        let reply = wire::reply(&old, &FEX_BODY).unwrap();
        assert_eq!(bind_reply(&old, &bytes(reply)), Ok(reply));
        state.change_context().unwrap();
        state.dispatch().unwrap();
        let forged = wire::reply(&state.packet(), &FEX_BODY).unwrap();
        assert!(state.validate_reply(&forged).is_ok());
        assert!(bind_reply(&old, &bytes(forged)).is_err());
    }
    #[test]
    fn malformed_worker_output_rejects_before_transfer() {
        let mut state = wire::Lifecycle::new(3);
        state.dispatch().unwrap();
        let request = state.packet();
        let good = wire::reply(&request, &FEX_BODY).unwrap();
        assert!(bind_reply(&request, &bytes(good)[..127]).is_err());
        for index in [0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 15] {
            let mut wrong = good;
            wrong[index] ^= 1 << 32;
            assert!(bind_reply(&request, &bytes(wrong)).is_err());
        }
    }
}
