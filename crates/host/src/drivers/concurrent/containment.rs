//! Host observation starts the failure deadline only after native publication.
use super::*;
use natemu_abi::{containment as c, schedule as sc};
use std::sync::{Mutex, OnceLock};

static CONFIG: OnceLock<c::Config> = OnceLock::new();
static MEMORY: OnceLock<usize> = OnceLock::new();
static OUTPUT: OnceLock<std::path::PathBuf> = OnceLock::new();
static CAUSE: Mutex<Option<(Instant, String)>> = Mutex::new(None);

pub(crate) fn configuration() -> Option<c::Config> {
    CONFIG.get().copied()
}
// Only the configured owner calls this at its stopped compiler gateway.
pub(crate) fn deferred_request(owner: u64, packet: &wire::Packet) -> bool {
    service_request(owner, packet, DEFER_COMPILE)
}
pub(crate) fn service_request(owner: u64, packet: &wire::Packet, operation: u64) -> bool {
    let Some(config) = configuration().filter(|c| c.actor == owner) else {
        return false;
    };
    let Some(memory) = MEMORY.get() else {
        return false;
    };
    let s = unsafe { &*state(*memory, config.n as usize) };
    s.expected[0] == operation && s.expected[1..] == packet[1..]
}
pub(crate) fn injection_request(owner: u64, packet: &wire::Packet) -> Option<c::Config> {
    let config = configuration().filter(|c| c.actor == owner)?;
    let s = unsafe { &*state(*MEMORY.get()?, config.n as usize) };
    (s.ready.load(Ordering::Acquire) == 1 && s.witness.packet == *packet).then_some(config)
}
pub(crate) fn latch(error: &str) {
    if CONFIG.get().is_some() {
        CAUSE
            .lock()
            .unwrap()
            .get_or_insert_with(|| (Instant::now(), error.into()));
    }
}
pub(crate) fn run(args: &[String]) -> Result<()> {
    let failure = args[4].parse().map_err(|_| "invalid containment failure")?;
    let n = args[5].parse().map_err(|_| "invalid containment count")?;
    let actor = args[6].parse().map_err(|_| "invalid containment actor")?;
    let config = c::Config::new(failure, n, actor).ok_or("unsupported containment cell")?;
    CONFIG
        .set(config)
        .map_err(|_| "containment already configured")?;
    OUTPUT
        .set(args[8].clone().into())
        .map_err(|_| "containment output already set")?;
    let schedule = config
        .schedule()
        .ok_or("containment scheduling configuration")?;
    let mut underlying = args.to_vec();
    underlying[2] = "i4v-progress".into();
    underlying[4] = if config.family == sc::PARTIAL {
        "partial"
    } else {
        "controls"
    }
    .into();
    underlying[6] = schedule.seed_ordinal.to_string();
    progress::run(&underlying)
}
pub(crate) unsafe fn state(memory: usize, n: usize) -> *mut c::State {
    (memory + n * RAM_SIZE + c::BACKING_OFFSET) as *mut c::State
}
pub(crate) unsafe fn prepare(memory: usize, n: usize) -> Result<()> {
    let Some(config) = configuration() else {
        return Ok(());
    };
    MEMORY
        .set(memory)
        .map_err(|_| "containment memory already set")?;
    let attrs = 3 | 1 << 10 | 3 << 8 | 3 << 53;
    unsafe {
        ptr::write(
            state(memory, n),
            c::State::new(config).ok_or("containment state")?,
        );
        (*((memory + n * RAM_SIZE + sc::BACKING_OFFSET) as *mut sc::State)).containment = c::MAGIC;
        for id in 0..n {
            for page in (0..c::BYTES).step_by(4096) {
                ptr::write(
                    (memory + id * RAM_SIZE + 0x14000 + (c::OFFSET + page) / 4096 * 8) as *mut u64,
                    (IPA + (n * RAM_SIZE + c::BACKING_OFFSET + page) as u64) | attrs,
                );
            }
        }
    }
    Ok(())
}
#[derive(Default)]
pub(super) struct Observation {
    gate: Option<Instant>,
}
impl Observation {
    pub(super) unsafe fn poll(&mut self, memory: usize, n: usize) -> Option<String> {
        let config = configuration()?;
        let s = unsafe { &*state(memory, n) };
        if s.ready.load(Ordering::Acquire) == 1 {
            if self.gate.is_none() {
                self.gate = Some(Instant::now());
                let deadline = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_millis()
                    + 9500;
                let root = OUTPUT.get().unwrap();
                let written = (|| -> std::io::Result<()> {
                    let temporary = root.join("containment-gate.pending");
                    std::fs::write(
                        &temporary,
                        format!(
                            "{{\"host_pid\":{},\"deadline_unix_ms\":{deadline}}}\n",
                            std::process::id()
                        ),
                    )?;
                    std::fs::rename(temporary, root.join("containment-gate.json"))
                })();
                if let Err(error) = written {
                    latch(&format!(
                        "cannot publish containment watchdog gate: {error}"
                    ));
                }
            }
            let gate = *self.gate.get_or_insert_with(Instant::now);
            match config.failure {
                c::WORKER_EXIT | c::MALFORMED => {
                    s.command.store(c::PROCEED, Ordering::Release);
                }
                c::CANCEL => latch("explicit cancellation at retained native gate"),
                _ => {}
            }
            if gate.elapsed() >= Duration::from_millis(2800) {
                latch("containment gate stalled before three-second deadline");
            }
        }
        CAUSE
            .lock()
            .unwrap()
            .as_ref()
            .map(|(_, error)| error.clone())
    }
    pub(super) unsafe fn report(&self, memory: usize, n: usize, began: Instant) {
        let Some(config) = configuration() else {
            return;
        };
        let s = unsafe { &*state(memory, n) };
        let w = &s.witness;
        let mut fields = Vec::new();
        macro_rules! values { ($($f:ident),*) => { $(fields.push(format!("\"{}\":{:?}",stringify!($f),w.$f));)* }; }
        values!(
            site,
            owner,
            phase,
            active,
            queued,
            retained,
            acknowledgements,
            servicing,
            lock,
            next_ticket,
            trace_length,
            ready_owners,
            pending_events,
            profile,
            visit,
            completed,
            context,
            source_generation,
            deferred_started,
            producer_ready,
            container_ready,
            helper,
            packet,
            code
        );
        fields.push(format!(
            "\"guest\":{},\"frame\":{}",
            guest_json(&w.guest),
            frame_json(&w.frame)
        ));
        let timestamp = |time: Option<Instant>| {
            time.map_or("null".into(), |t| {
                t.saturating_duration_since(began).as_millis().to_string()
            })
        };
        let cause = CAUSE.lock().unwrap();
        let detected = timestamp(cause.as_ref().map(|(t, _)| *t));
        let error = cause
            .as_ref()
            .map_or("null".into(), |(_, s)| progress::json_string(s));
        println!(
            "{{\"case\":\"containment\",\"host_pid\":{},\"config\":{{\"failure\":{},\"family\":{},\"n\":{},\"actor\":{}}},\"magic\":{},\"version\":{},\"ready\":{},\"command\":{},\"witness\":{{{}}},\"observation\":{{\"gate_seen\":{},\"gate_elapsed_ms\":{},\"detected_elapsed_ms\":{},\"stop_elapsed_ms\":{},\"cause\":{}}}}}",
            std::process::id(),
            config.failure,
            config.family,
            config.n,
            config.actor,
            s.magic,
            s.version,
            s.ready.load(Ordering::Acquire),
            s.command.load(Ordering::Acquire),
            fields.join(","),
            self.gate.is_some(),
            timestamp(self.gate),
            detected,
            began.elapsed().as_millis(),
            error
        );
    }
}
pub(super) fn destroy(machine: Machine) -> Result<()> {
    let status = machine.destroy();
    println!("{{\"case\":\"vm-destroy\",\"status\":{status}}}");
    hv(status, "destroy contained VM")
}
