//! Test-driver-only backend schedules. Observations never contain guest pointers or alter replies.
//! `calls` counts wrapper attempts, including injected errors without a syscall. The held barrier
//! is before entering a native call. `commit` records RAM publication, before IRQ/index retirement.
use super::{block::{Calls, Native}, *};
use std::sync::atomic::AtomicUsize;

/// Selects a finite native-call schedule; absent from the ordinary CLI's library build.
pub struct Control {
    mode: String,
    entered: AtomicBool,
    power: AtomicBool,
    peer: AtomicBool,
    calls: AtomicUsize,
    gate: Mutex<(bool, Vec<String>)>,
    wake: Condvar,
}
impl Control {
    pub fn new(mode: &str) -> Result<Arc<Self>> {
        if !["normal", "hold-reset", "hold-disable", "cycle-reset", "cycle-disable", "hold-power", "hold-power-halt", "power-sync-error", "read-error", "write-error"].contains(&mode) {
            return Err("unknown I/O schedule".into());
        }
        Ok(Arc::new(Self { mode: mode.into(), entered: AtomicBool::new(false), power: AtomicBool::new(false), peer: AtomicBool::new(false), calls: AtomicUsize::new(0),
            gate: Mutex::new((false, Vec::with_capacity(512))), wake: Condvar::new() }))
    }
    pub fn entered(&self) -> bool { self.entered.load(SeqCst) }
    fn repeated(&self) -> bool { matches!(self.mode.as_str(), "cycle-reset" | "cycle-disable") }
    pub fn event(&self, name: &str) {
        if name == "power-poll" && self.power.swap(true, SeqCst) { return; }
        if name == "peer-halted" && self.peer.swap(true, SeqCst) { return; }
        let mut gate = self.gate.lock().unwrap_or_else(|e| e.into_inner());
        let limit = if self.repeated() { 4095 } else { 511 };
        if gate.1.len() < limit { gate.1.push(name.into()); }
        else if gate.1.len() == limit { gate.1.push("trace-overflow".into()); }
        if matches!((self.mode.as_str(), name), ("hold-reset" | "cycle-reset", "reset-deferred") | ("hold-disable" | "cycle-disable", "disable-deferred")
            | ("hold-power" | "power-sync-error", "power-poll"))
            || self.mode == "hold-power-halt" && name == "peer-halted" && self.power.load(SeqCst) {
            gate.0 = true; self.wake.notify_all();
        }
    }
    fn hold(&self) -> std::io::Result<()> {
        if self.repeated() {
            // Alternate held old-generation reads and ordinary fresh-generation recovery reads.
            if self.calls.load(SeqCst) % 2 == 1 { return Ok(()); }
            self.gate.lock().unwrap_or_else(|e| e.into_inner()).0 = false;
        } else if self.entered.load(SeqCst) { return Ok(()); }
        if !matches!(self.mode.as_str(), "hold-reset" | "hold-disable" | "cycle-reset" | "cycle-disable" | "hold-power" | "hold-power-halt" | "power-sync-error") {
            self.entered.store(true, SeqCst); return Ok(());
        }
        self.event("backend-held");
        self.entered.store(true, SeqCst);
        let gate = self.gate.lock().unwrap_or_else(|e| e.into_inner());
        let (gate, _) = self.wake.wait_timeout_while(gate, Duration::from_secs(3), |g| !g.0).unwrap_or_else(|e| e.into_inner());
        let released = gate.0; drop(gate);
        self.event(if released { "backend-released" } else { "barrier-timeout" });
        if released { Ok(()) } else { Err(std::io::Error::new(std::io::ErrorKind::TimedOut, "test barrier not released")) }
    }
    pub fn json(&self) -> String {
        let gate = self.gate.lock().unwrap_or_else(|e| e.into_inner());
        format!("{{\"entered\":{},\"calls\":{},\"events\":{:?}}}", self.entered(), self.calls.load(SeqCst), gate.1)
    }
}
pub(super) struct Controlled { pub native: Native, pub control: Arc<Control>, pub phase: usize }
impl Controlled {
    fn begin(&mut self, write: bool, offset: u64, len: usize) -> std::io::Result<usize> {
        self.control.hold()?;
        self.control.calls.fetch_add(1, SeqCst);
        self.control.event(&format!("{}:{offset}:{len}", if write { "write" } else { "read" }));
        let scripted = self.control.mode == if write { "write-error" } else { "read-error" };
        if scripted {
            self.phase += 1;
            if self.phase == 1 { return Ok(len.min(256)); }
            if self.phase == 2 { self.control.event("injected-eio"); return Err(std::io::Error::from_raw_os_error(5)); }
        }
        Ok(len)
    }
}
impl Calls for Controlled {
    fn read(&mut self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize> {
        let len = self.begin(false, offset, bytes.len())?;
        let result = self.native.read(&mut bytes[..len], offset);
        if self.control.repeated() { self.control.entered.store(false, SeqCst); }
        self.control.event(&format!("read-result:{result:?}")); result
    }
    fn write(&mut self, bytes: &[u8], offset: u64) -> std::io::Result<usize> {
        let len = self.begin(true, offset, bytes.len())?;
        let result = self.native.write(&bytes[..len], offset);
        self.control.event(&format!("write-result:{result:?}")); result
    }
    fn sync(&mut self) -> std::io::Result<()> {
        self.control.event("sync-enter");
        let result = if self.control.mode == "power-sync-error" { Err(std::io::Error::from_raw_os_error(5)) } else { self.native.sync() };
        self.control.event(if result.is_ok() { "sync-ok" } else { "sync-error" }); result
    }
}
