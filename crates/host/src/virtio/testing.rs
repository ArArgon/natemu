//! Test-driver-only backend schedules. Observations never contain guest pointers or alter replies.
//! `calls` counts wrapper attempts, including injected errors without a syscall. The held barrier
//! is before entering a native call. `commit` records RAM publication, before IRQ/index retirement.
use super::{
    block::{Calls, Native},
    *,
};
use std::sync::atomic::AtomicUsize;

/// A named native-call schedule (the test driver's `--io-schedule`).
#[derive(Clone, Copy, PartialEq, Eq)]
enum Mode {
    Normal,
    /// Hold the first disk call until a device reset (`cycle-*`: every other call, repeatedly)
    /// or a queue disable is deferred behind it.
    HoldReset,
    HoldDisable,
    CycleReset,
    CycleDisable,
    /// Hold the first disk call until power-off polls (`-halt`: and a peer vCPU then halts).
    HoldPower,
    HoldPowerHalt,
    /// As HoldPower, and the final full sync fails.
    PowerSyncError,
    /// The first read (write) is short, the second fails with EIO.
    ReadError,
    WriteError,
}
impl Mode {
    fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "normal" => Self::Normal,
            "hold-reset" => Self::HoldReset,
            "hold-disable" => Self::HoldDisable,
            "cycle-reset" => Self::CycleReset,
            "cycle-disable" => Self::CycleDisable,
            "hold-power" => Self::HoldPower,
            "hold-power-halt" => Self::HoldPowerHalt,
            "power-sync-error" => Self::PowerSyncError,
            "read-error" => Self::ReadError,
            "write-error" => Self::WriteError,
            _ => return None,
        })
    }
    fn repeated(self) -> bool {
        matches!(self, Self::CycleReset | Self::CycleDisable)
    }
    fn holds(self) -> bool {
        !matches!(self, Self::Normal | Self::ReadError | Self::WriteError)
    }
    /// Whether event `name` releases this schedule's hold.
    fn releases(self, name: &str) -> bool {
        match self {
            Self::HoldReset | Self::CycleReset => name == "reset-deferred",
            Self::HoldDisable | Self::CycleDisable => name == "disable-deferred",
            Self::HoldPower | Self::PowerSyncError => name == "power-poll",
            _ => false,
        }
    }
}

/// Selects a finite native-call schedule; absent from the ordinary CLI's library build.
pub struct Control {
    mode: Mode,
    entered: AtomicBool,
    power: AtomicBool,
    peer: AtomicBool,
    calls: AtomicUsize,
    /// Whether the hold is released, and the event trace.
    gate: Mutex<(bool, Vec<String>)>,
    wake: Condvar,
}
impl Control {
    pub fn new(mode: &str) -> Result<Arc<Self>> {
        let mode = Mode::parse(mode).ok_or("unknown I/O schedule")?;
        Ok(Arc::new(Self {
            mode,
            entered: AtomicBool::new(false),
            power: AtomicBool::new(false),
            peer: AtomicBool::new(false),
            calls: AtomicUsize::new(0),
            gate: Mutex::new((false, Vec::with_capacity(512))),
            wake: Condvar::new(),
        }))
    }
    pub fn entered(&self) -> bool {
        self.entered.load(SeqCst)
    }
    /// Record `name` (power polls and peer halts once each) and release a hold it ends.
    pub fn event(&self, name: &str) {
        if name == "power-poll" && self.power.swap(true, SeqCst) {
            return;
        }
        if name == "peer-halted" && self.peer.swap(true, SeqCst) {
            return;
        }
        let mut gate = self.gate.lock().unwrap_or_else(|e| e.into_inner());
        let limit = if self.mode.repeated() { 4095 } else { 511 };
        if gate.1.len() < limit {
            gate.1.push(name.into());
        } else if gate.1.len() == limit {
            gate.1.push("trace-overflow".into());
        }
        let halted_after_power =
            self.mode == Mode::HoldPowerHalt && name == "peer-halted" && self.power.load(SeqCst);
        if self.mode.releases(name) || halted_after_power {
            gate.0 = true;
            self.wake.notify_all();
        }
    }
    /// Before a native call: wait (at most 3 s) for this schedule's release event, if it holds.
    fn hold(&self) -> std::io::Result<()> {
        if self.mode.repeated() {
            // Alternate held old-generation reads and ordinary fresh-generation recovery reads.
            if self.calls.load(SeqCst) % 2 == 1 {
                return Ok(());
            }
            self.gate.lock().unwrap_or_else(|e| e.into_inner()).0 = false;
        } else if self.entered.load(SeqCst) {
            return Ok(());
        }
        if !self.mode.holds() {
            self.entered.store(true, SeqCst);
            return Ok(());
        }
        self.event("backend-held");
        self.entered.store(true, SeqCst);
        let gate = self.gate.lock().unwrap_or_else(|e| e.into_inner());
        let (gate, _) = self
            .wake
            .wait_timeout_while(gate, Duration::from_secs(3), |g| !g.0)
            .unwrap_or_else(|e| e.into_inner());
        let released = gate.0;
        drop(gate);
        if released {
            self.event("backend-released");
            Ok(())
        } else {
            self.event("barrier-timeout");
            Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "test barrier not released",
            ))
        }
    }
    pub fn json(&self) -> String {
        let gate = self.gate.lock().unwrap_or_else(|e| e.into_inner());
        format!(
            "{{\"entered\":{},\"calls\":{},\"events\":{:?}}}",
            self.entered(),
            self.calls.load(SeqCst),
            gate.1
        )
    }
}
pub(super) struct Controlled {
    pub native: Native,
    pub control: Arc<Control>,
    pub phase: usize,
}
impl Controlled {
    fn begin(&mut self, write: bool, offset: u64, len: usize) -> std::io::Result<usize> {
        self.control.hold()?;
        self.control.calls.fetch_add(1, SeqCst);
        self.control.event(&format!(
            "{}:{offset}:{len}",
            if write { "write" } else { "read" }
        ));
        let scripted = if write {
            Mode::WriteError
        } else {
            Mode::ReadError
        };
        if self.control.mode == scripted {
            self.phase += 1;
            if self.phase == 1 {
                return Ok(len.min(256));
            }
            if self.phase == 2 {
                self.control.event("injected-eio");
                return Err(std::io::Error::from_raw_os_error(5));
            }
        }
        Ok(len)
    }
}
impl Calls for Controlled {
    fn read(&mut self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize> {
        let len = self.begin(false, offset, bytes.len())?;
        let result = self.native.read(&mut bytes[..len], offset);
        if self.control.mode.repeated() {
            self.control.entered.store(false, SeqCst);
        }
        self.control.event(&format!("read-result:{result:?}"));
        result
    }
    fn write(&mut self, bytes: &[u8], offset: u64) -> std::io::Result<usize> {
        let len = self.begin(true, offset, bytes.len())?;
        let result = self.native.write(&bytes[..len], offset);
        self.control.event(&format!("write-result:{result:?}"));
        result
    }
    fn sync(&mut self) -> std::io::Result<()> {
        self.control.event("sync-enter");
        let result = if self.control.mode == Mode::PowerSyncError {
            Err(std::io::Error::from_raw_os_error(5))
        } else {
            self.native.sync()
        };
        self.control.event(if result.is_ok() {
            "sync-ok"
        } else {
            "sync-error"
        });
        result
    }
}
