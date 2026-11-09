//! Bounded owned-packet loopback for the program driver; no guest-memory authority.
use natemu::{Result, virtio::PacketPort};
use std::{sync::{Arc, Condvar, Mutex, atomic::{AtomicBool, Ordering::SeqCst}}, time::{Duration, Instant}};

#[derive(Default)]
pub(super) struct Trace(Mutex<Vec<String>>);
impl Trace {
    fn event(&self, value: String) { self.0.lock().unwrap_or_else(|e| e.into_inner()).push(value); }
    pub(super) fn json(&self) -> String {
        format!("{{\"events\":{:?}}}", self.0.lock().unwrap_or_else(|e| e.into_inner()))
    }
}

/// A fixture packet released by the supervisor's existing idle observer. The endpoint owns
/// only bytes and this latch, never a Backing/VM/IRQ handle. The observer adds no liveness veto.
pub(super) struct Arrival { bytes: Vec<u8>, ready: AtomicBool }
impl Arrival {
    pub(super) fn new(bytes: Vec<u8>) -> Result<Self> {
        if !(14..=1518).contains(&bytes.len()) { return Err("arrival fixture frame must be 14..1518 bytes".into()); }
        Ok(Self { bytes, ready: AtomicBool::new(false) })
    }
    pub(super) fn release(&self, held: Duration, first: &[[u64; 2]], last: &[[u64; 2]], trace: &Trace) {
        if !self.ready.load(SeqCst) {
            trace.event(format!("all-halted:{{\"held_us\":{},\"first\":{first:?},\"last\":{last:?},\"live\":1,\"kickers\":0,\"io_active\":false}}", held.as_micros()));
            self.ready.store(true, SeqCst);
        }
    }
}

/// Hold only the first owned TX callback. Guest port and power hook exchange metadata, not RAM.
pub(super) struct Hold {
    state: Mutex<(bool, bool)>, wake: Condvar, power: bool, trace: Arc<Trace>,
}
impl Hold {
    pub(super) fn new(power: bool, trace: Arc<Trace>) -> Self {
        Self { state: Mutex::new((false, false)), wake: Condvar::new(), power, trace }
    }
    fn wait(&self) -> Result<()> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        self.trace.event("tx-held".into()); state.0 = true;
        state = self.wake.wait_timeout_while(state, Duration::from_secs(5), |s| !s.1)
            .unwrap_or_else(|e| e.into_inner()).0;
        if !state.1 { return Err("packet fixture TX hold deadline".into()); }
        self.trace.event("tx-released".into()); Ok(())
    }
    pub(super) fn release(&self, power: bool) -> bool {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if self.power != power || !state.0 || state.1 { return false; }
        self.trace.event(format!("release:{}", if power { "power" } else { "guest" }));
        state.1 = true; self.wake.notify_one(); true
    }
}
pub(super) struct HoldPort(pub Arc<Hold>);
impl natemu::devices::Device for HoldPort {
    fn read(&mut self, _: usize, offset: u64, size: u64) -> Option<u64> {
        (offset == 0 && size == 1).then(|| u64::from(self.0.state.lock().unwrap_or_else(|e| e.into_inner()).0))
    }
    fn write(&mut self, _: usize, offset: u64, size: u64, value: u64) -> bool {
        offset == 0 && size == 1 && value == 1 && self.0.release(false)
    }
}

pub(super) fn echo(port: PacketPort, trace: &Trace, arrival: Option<&Arrival>, hold: Option<&Hold>) -> Result<()> {
    let start = Instant::now();
    let mut frames = 0;
    let mut arrived = false;
    loop {
        if port.closed() { trace.event("closed".into()); return Ok(()); }
        if start.elapsed() > Duration::from_secs(45) {
            let error = "packet fixture endpoint deadline".to_string();
            port.fail(error.clone()); return Err(error);
        }
        if let Some(arrival) = arrival.filter(|a| !arrived && a.ready.load(SeqCst)) {
            arrived = true;
            let accepted = port.receive(&arrival.bytes);
            trace.event(format!("arrival:{accepted}"));
            if !accepted { return Err("fixture arrival was dropped".into()); }
        }
        let sent = port.transmit(|bytes| {
            frames += 1;
            if frames > 64 { return Err("packet fixture exceeded 64 frames".into()); }
            let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
            trace.event(format!("tx:{hex}"));
            if frames == 1 { if let Some(hold) = hold { hold.wait()?; } }
            // One arrival attempt, before TX acknowledgement. A rejected arrival stays dropped.
            trace.event(format!("rx:{}", port.receive(bytes)));
            Ok(())
        })?;
        if !sent { std::thread::sleep(Duration::from_millis(1)); }
    }
}
