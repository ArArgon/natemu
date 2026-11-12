//! Test-driver-only backend schedules. Observations never contain guest pointers or alter replies.
//! `calls` counts wrapper attempts, including injected errors without a syscall. The held barrier
//! is before entering a native call. `commit` records one request's RAM publication, before
//! IRQ/index retirement.
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
    /// Among concurrent requests: the first call at file offset `BATCH_OFFSET` (read or write) is
    /// short (256 bytes), its continuation fails with EIO; every other call is normal.
    BatchError,
    /// The first plain sync (a guest FLUSH or write-through) fails with EIO; the pool then fails every later sync
    /// without a call (block.rs `Sticky`).
    FlushError,
    /// Hold the first disk call at file offset 0 until another request has been published.
    OutOfOrder,
    /// Hold every disk call at file offset 0 until a device reset is deferred behind them.
    ResetMixed,
    /// Hold the owner's first completion until `BATCH` requests are answered, then until a device
    /// reset is deferred behind them (the window covering them is then never granted).
    CancelBatch,
    /// Hold the first disk call until the test driver armed its publication hook
    /// (`publish-armed`), so that hook catches this request's completion.
    HoldPublish,
}
/// The requests a CancelBatch completion covers.
const BATCH: usize = 4;
/// The file offset of BatchError's failing request.
const BATCH_OFFSET: u64 = 1024;
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
            "batch-error" => Self::BatchError,
            "flush-error" => Self::FlushError,
            "out-of-order" => Self::OutOfOrder,
            "reset-mixed" => Self::ResetMixed,
            "cancel-batch" => Self::CancelBatch,
            "hold-publish" => Self::HoldPublish,
            _ => return None,
        })
    }
    fn repeated(self) -> bool {
        matches!(self, Self::CycleReset | Self::CycleDisable)
    }
    /// Whether event `name` releases this schedule's hold.
    fn releases(self, name: &str) -> bool {
        match self {
            Self::HoldReset | Self::CycleReset | Self::ResetMixed | Self::CancelBatch => {
                name == "reset-deferred"
            }
            Self::HoldDisable | Self::CycleDisable => name == "disable-deferred",
            Self::HoldPower | Self::PowerSyncError => name == "power-poll",
            Self::OutOfOrder => name == "commit",
            Self::HoldPublish => name == "publish-armed",
            _ => false,
        }
    }
}

/// Selects a finite native-call schedule; absent from the ordinary CLI's library build.
pub struct Control {
    mode: Mode,
    entered: AtomicBool,
    /// The one held call (or completion) of a single-hold schedule was claimed.
    claimed: AtomicBool,
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
            claimed: AtomicBool::new(false),
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
    /// Whether this schedule holds a disk call at `offset`.
    fn holds_call(&self, offset: u64) -> bool {
        match self.mode {
            Mode::Normal
            | Mode::ReadError
            | Mode::WriteError
            | Mode::BatchError
            | Mode::FlushError
            | Mode::CancelBatch => false,
            // Alternate held old-generation reads and ordinary fresh-generation recovery reads.
            Mode::CycleReset | Mode::CycleDisable => self.calls.load(SeqCst) % 2 == 0,
            Mode::ResetMixed => offset == 0,
            Mode::OutOfOrder => offset == 0 && !self.claimed.swap(true, SeqCst),
            _ => !self.claimed.swap(true, SeqCst),
        }
    }
    /// Before a native call: wait (at most 3 s) for this schedule's release event, if it holds.
    fn hold(&self, offset: u64) -> std::io::Result<()> {
        if !self.holds_call(offset) {
            // A cycle's recovery call leaves `entered` to the held call's retirement.
            if !self.mode.repeated() && self.mode != Mode::CancelBatch {
                self.entered.store(true, SeqCst);
            }
            return Ok(());
        }
        if self.mode.repeated() {
            self.gate.lock().unwrap_or_else(|e| e.into_inner()).0 = false;
        }
        self.wait("backend")
    }
    /// Record `{what}-held`, publish `entered`, then wait (at most 3 s) for the release.
    fn wait(&self, what: &str) -> std::io::Result<()> {
        self.event(&format!("{what}-held"));
        self.entered.store(true, SeqCst);
        let gate = self.gate.lock().unwrap_or_else(|e| e.into_inner());
        let (gate, _) = self
            .wake
            .wait_timeout_while(gate, Duration::from_secs(3), |g| !g.0)
            .unwrap_or_else(|e| e.into_inner());
        let released = gate.0;
        drop(gate);
        if released {
            self.event(&format!("{what}-released"));
            Ok(())
        } else {
            self.event("barrier-timeout");
            Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "test barrier not released",
            ))
        }
    }
    /// Before the owner completes `answered` requests: whether it may now. CancelBatch defers
    /// until `BATCH` are answered, then holds that one completion until its release.
    pub(super) fn completion_allowed(&self, answered: usize) -> bool {
        if self.mode != Mode::CancelBatch || self.claimed.load(SeqCst) {
            return true;
        }
        if answered < BATCH {
            return false;
        }
        self.claimed.store(true, SeqCst);
        // A timeout is in the trace; the completion then proceeds.
        let _ = self.wait("batch");
        true
    }
    /// Whether event `name` was recorded.
    pub fn seen(&self, name: &str) -> bool {
        let gate = self.gate.lock().unwrap_or_else(|e| e.into_inner());
        gate.1.iter().any(|e| e == name)
    }
    /// The schedule's report: whether a call entered, the calls and the event trace.
    pub fn report(&self) -> ControlReport {
        ControlReport {
            entered: self.entered(),
            calls: self.calls.load(SeqCst),
            events: self
                .gate
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .1
                .clone(),
        }
    }
}
#[derive(serde::Serialize)]
pub struct ControlReport {
    entered: bool,
    calls: usize,
    events: Vec<String>,
}
pub(super) struct Controlled {
    native: Native,
    control: Arc<Control>,
    /// Calls of a ReadError/WriteError schedule so far (BatchError: 1 after its short call, 2 after its EIO).
    phase: AtomicUsize,
}
impl Controlled {
    pub fn new(native: Native, control: Arc<Control>) -> Self {
        Self {
            native,
            control,
            phase: AtomicUsize::new(0),
        }
    }
    fn begin(&self, write: bool, offset: u64, len: usize) -> std::io::Result<usize> {
        self.control.hold(offset)?;
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
        if self.control.mode == Mode::BatchError {
            // Phase 1 once the short first call was made: only its continuation fails.
            let next = match offset {
                BATCH_OFFSET => 1,
                o if o == BATCH_OFFSET + 256 => 2,
                _ => 0,
            };
            if next != 0
                && self
                    .phase
                    .compare_exchange(next - 1, next, SeqCst, SeqCst)
                    .is_ok()
            {
                if next == 1 {
                    return Ok(len.min(256));
                }
                self.control.event("injected-eio");
                return Err(std::io::Error::from_raw_os_error(5));
            }
        }
        if self.control.mode == scripted {
            match self.phase.fetch_add(1, SeqCst) + 1 {
                1 => return Ok(len.min(256)),
                2 => {
                    self.control.event("injected-eio");
                    return Err(std::io::Error::from_raw_os_error(5));
                }
                _ => {}
            }
        }
        Ok(len)
    }
}
impl Calls for Controlled {
    fn read(&self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize> {
        let len = self.begin(false, offset, bytes.len())?;
        let result = self.native.read(&mut bytes[..len], offset);
        if self.control.mode.repeated() {
            self.control.entered.store(false, SeqCst);
        }
        self.control.event(&format!("read-result:{result:?}"));
        result
    }
    fn write(&self, bytes: &[u8], offset: u64) -> std::io::Result<usize> {
        let len = self.begin(true, offset, bytes.len())?;
        let result = self.native.write(&bytes[..len], offset);
        self.control.event(&format!("write-result:{result:?}"));
        result
    }
    fn sync(&self, full: bool) -> std::io::Result<()> {
        self.control.event("sync-enter");
        let result = if full && self.control.mode == Mode::PowerSyncError
            || !full
                && self.control.mode == Mode::FlushError
                && !self.control.claimed.swap(true, SeqCst)
        {
            Err(std::io::Error::from_raw_os_error(5))
        } else {
            self.native.sync(full)
        };
        self.control.event(if result.is_ok() {
            "sync-ok"
        } else {
            "sync-error"
        });
        result
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn json_matches_the_former_format() {
        let control = super::Control::new("normal").unwrap();
        let error: std::io::Result<usize> = Err(std::io::Error::from_raw_os_error(5));
        for name in [
            "backend-held",
            &format!("read-result:{error:?}"),
            "q\"b\\c\u{1}",
        ] {
            control.event(name);
        }
        let new = serde_json::to_value(control.report()).unwrap();
        // The former `{:?}` of the events wrote a control character as `\u{1}`, not JSON: compare without it.
        let mut gate = control.gate.lock().unwrap();
        gate.1.pop();
        let old = format!(
            "{{\"entered\":{},\"calls\":{},\"events\":{:?}}}",
            control.entered(),
            control.calls.load(super::SeqCst),
            gate.1
        );
        let old: serde_json::Value = serde_json::from_str(&old).unwrap();
        let mut expected = old;
        expected["events"]
            .as_array_mut()
            .unwrap()
            .push("q\"b\\c\u{1}".into());
        assert_eq!(new, expected);
    }
}
