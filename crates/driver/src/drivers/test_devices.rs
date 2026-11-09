//! The test driver's devices (moved from the library at L0): the M3 test device page (`run-program`'s `mmio` items and
//! `--device-at`) and the L4a asynchronous interrupt source (`--irq-source`). Registered through the library's ordinary
//! device registry ([`Devices`]); the library knows neither.
use natemu::devices::{Device, mask};
use natemu::irq::{Busy, Irqs};
use natemu_abi::execution::cache::MAX_VCPUS;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering::SeqCst};

/// A test device for the M3 checks: one page. `COUNTER` (8 bytes) reads as the number of earlier reads of it (each read
/// counts, whatever its size); `SEQUENCE` (8 bytes) checks order: the n-th write to it by a vCPU must carry n (else an
/// order error), and it reads as that vCPU's next n. The last 8 bytes refuse every access (`Device`'s refusal). Every
/// other byte is plain memory (an access straddling into the
/// registers from below is memory too). Every access is counted per vCPU and logged (the first `LOG` of them).
pub struct TestDevice {
    pub bytes: [u8; 4096],
    counter: u64,
    sequence: [u64; MAX_VCPUS],
    pub reads: [u64; MAX_VCPUS],
    pub writes: [u64; MAX_VCPUS],
    pub order_errors: u64,
    /// (vCPU, offset, size, 1 for a write, value).
    pub log: Vec<[u64; 5]>,
}
impl TestDevice {
    pub(crate) const COUNTER: u64 = 0xf00;
    pub(crate) const SEQUENCE: u64 = 0xf08;
    /// Accesses reaching the page's last 8 bytes are refused (uncounted, unlogged).
    pub(crate) const REFUSE: u64 = 0xff8;
    const LOG: usize = 4096;
    pub fn new() -> Self {
        Self {
            bytes: [0; 4096],
            counter: 0,
            sequence: [0; MAX_VCPUS],
            reads: [0; MAX_VCPUS],
            writes: [0; MAX_VCPUS],
            order_errors: 0,
            log: vec![],
        }
    }
    fn register(offset: u64, size: u64) -> Option<u64> {
        let reg = offset & !7;
        (matches!(reg, Self::COUNTER | Self::SEQUENCE) && (offset & 7) + size <= 8).then_some(reg)
    }
    fn logged(&mut self, entry: [u64; 5]) {
        if self.log.len() < Self::LOG {
            self.log.push(entry);
        }
    }
}
impl Device for TestDevice {
    fn read(&mut self, vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        if offset + size > Self::REFUSE {
            return None;
        }
        self.reads[vcpu] += 1;
        let shift = 8 * (offset & 7);
        let value = match Self::register(offset, size) {
            Some(Self::COUNTER) => {
                self.counter += 1;
                (self.counter - 1) >> shift
            }
            Some(_) => self.sequence[vcpu] >> shift,
            None => (0..size).rev().fold(0, |v, i| {
                v << 8 | u64::from(self.bytes[(offset + i) as usize])
            }),
        } & mask(size);
        self.logged([vcpu as u64, offset, size, 0, value]);
        Some(value)
    }
    fn write(&mut self, vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        if offset + size > Self::REFUSE {
            return false;
        }
        self.writes[vcpu] += 1;
        self.logged([vcpu as u64, offset, size, 1, value]);
        match Self::register(offset, size) {
            Some(Self::SEQUENCE) => {
                if value != self.sequence[vcpu] & mask(size) {
                    self.order_errors += 1;
                }
                self.sequence[vcpu] += 1;
            }
            Some(_) => {}
            None => {
                for i in 0..size {
                    self.bytes[(offset + i) as usize] = (value >> (8 * i)) as u8;
                }
            }
        }
        true
    }
}

/// A test source (`run-program --irq-source PORT`), natemu only: an asynchronous device on 8 ports. A 32-bit write at
/// +0 asks for line bits 7:0 to fire after bits 31:16 microseconds, from a host thread: an edge (0 -> 1 -> 0) with bit 8
/// clear, else the line is asserted and held; with bit 9 set, a pulse on the NMI input (LINT1) instead. A write at +4
/// deasserts line bits 7:0 at once (the handler's acknowledgement of a level-triggered request). A 32-bit read at +0 gives
/// the requests fired so far. Each request holds a [`Busy`] guard until it fired.
pub struct TestSource {
    jobs: Option<std::sync::mpsc::Sender<(std::time::Instant, usize, bool, Busy)>>,
    fired: Arc<AtomicUsize>,
    worker: Option<std::thread::JoinHandle<()>>,
    irqs: Irqs,
    /// `--irq-source-no-veto` (a negative control of the halted rule): requests hold no guard.
    pub veto: bool,
}
impl TestSource {
    pub fn new(irqs: &Irqs) -> Self {
        let (jobs, queue) = std::sync::mpsc::channel::<(std::time::Instant, usize, bool, Busy)>();
        let (lines, fired) = (irqs.clone(), Arc::new(AtomicUsize::new(0)));
        let count = fired.clone();
        let worker = std::thread::spawn(move || {
            let mut pending = Vec::new();
            loop {
                // The earliest due job, or wait for one.
                pending.sort_by_key(|j: &(std::time::Instant, usize, bool, Busy)| {
                    std::cmp::Reverse(j.0)
                });
                let timeout = pending
                    .last()
                    .map(|j| j.0.saturating_duration_since(std::time::Instant::now()));
                let next = match timeout {
                    Some(t) => queue.recv_timeout(t),
                    None => queue
                        .recv()
                        .map_err(|_| std::sync::mpsc::RecvTimeoutError::Disconnected),
                };
                match next {
                    Ok(job) => pending.push(job),
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                        let (_, line, level, _busy) = pending.pop().unwrap();
                        if line == NMI_LINE {
                            lines.nmi();
                        } else {
                            lines.line(line, true);
                            if !level {
                                lines.line(line, false);
                            }
                        }
                        count.fetch_add(1, SeqCst);
                    }
                    Err(_) => return,
                }
            }
        });
        Self {
            jobs: Some(jobs),
            fired,
            worker: Some(worker),
            irqs: irqs.clone(),
            veto: true,
        }
    }
}
/// The test source's job for the NMI input.
const NMI_LINE: usize = usize::MAX;
impl Drop for TestSource {
    fn drop(&mut self) {
        self.jobs = None;
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}
impl Device for TestSource {
    fn read(&mut self, _vcpu: usize, offset: u64, _size: u64) -> Option<u64> {
        Some(if offset == 0 {
            self.fired.load(SeqCst) as u64
        } else {
            0
        })
    }
    fn write(&mut self, _vcpu: usize, offset: u64, _size: u64, value: u64) -> bool {
        let line = if value & 0x200 != 0 {
            NMI_LINE
        } else {
            (value & 0xff) as usize
        };
        match offset {
            0 => {
                let busy = if self.veto {
                    self.irqs.busy()
                } else {
                    Busy(Arc::new(AtomicUsize::new(1)))
                };
                let due = std::time::Instant::now()
                    + std::time::Duration::from_micros(value >> 16 & 0xffff);
                let _ = self
                    .jobs
                    .as_ref()
                    .map(|j| j.send((due, line, value & 0x100 != 0, busy)));
            }
            4 => {
                self.irqs.line(line, false);
            }
            _ => {}
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use natemu::devices::{Devices, Shared};
    use natemu_abi::execution::mmio;
    use std::sync::Mutex;

    #[test]
    fn registry_and_test_device() {
        let mut d = Devices::new(4 << 20);
        let t: Shared = Arc::new(Mutex::new(TestDevice::new()));
        assert!(d.register(0xfeb0_0000, 4096, t.clone()).is_ok());
        assert!(d.register(0xfeb0_0000, 4096, t.clone()).is_err()); // overlaps
        assert!(d.register(0x1000, 4096, t.clone()).is_err()); // guest RAM
        assert!(d.register(0x9_f000, 0x2000, t.clone()).is_err()); // RAM, then the hole
        assert!(d.register(0xb_8000, 0x8000, t.clone()).is_ok()); // inside the legacy hole (M1)
        assert!(d.register(0xfeb0_1800, 4096, t.clone()).is_err()); // unaligned
        assert_eq!(d.find(0xfeb0_0f00).map(|(_, o)| o), Some(0xf00));
        assert!(d.find(0xfeb0_1000).is_none());
        let mut t = TestDevice::new();
        assert_eq!(
            [0, 1, 2].map(|_| t.read(1, TestDevice::COUNTER, 4)),
            [Some(0), Some(1), Some(2)]
        );
        assert!(t.write(2, TestDevice::SEQUENCE, 8, 0) && t.write(2, TestDevice::SEQUENCE, 8, 5));
        assert!(t.write(0, 0x10, 4, 0xdead_beef));
        assert_eq!((t.read(0, 0x11, 2), t.order_errors), (Some(0xadbe), 1));
        assert_eq!(
            (&t.reads[..4], &t.writes[..4]),
            (&[1, 3, 0, 0][..], &[1, 0, 2, 0][..])
        );
        assert!(t.reads[4..].iter().chain(&t.writes[4..]).all(|&n| n == 0));
    }

    #[test]
    fn port_bus_and_unassigned_rule() {
        let mut d = Devices::new(4 << 20);
        let t = Arc::new(Mutex::new(TestDevice::new()));
        assert!(d.register_ports(0x100, 4, t.clone()).is_ok());
        assert!(d.register_ports(0x102, 4, t.clone()).is_err()); // overlaps
        assert!(d.register_ports(0xfffe, 4, t.clone()).is_err()); // past FFFFH
        assert_eq!(d.port(0, 0x100, 4, Some(0x4433_2211)), Ok(0));
        assert_eq!(d.port(0, 0x101, 2, None), Ok(0x3322)); // inside the range: one call
        assert_eq!(d.port(0, 0x102, 4, None), Ok(0xffff_4433)); // ends outside: byte by byte, the rest all-ones
        assert_eq!(
            (
                d.port(0, 0x200, 1, None),
                d.port(0, 0x200, 2, None),
                d.port(0, 0x200, 4, None)
            ),
            (Ok(0xff), Ok(0xffff), Ok(0xffff_ffff))
        );
        assert_eq!(d.port(0, 0x200, 4, Some(0)), Ok(0)); // ignored
        assert_eq!(d.port(0, 0xffff, 2, None), Ok(0xffff)); // wraps to port 0
        assert_eq!(t.lock().unwrap().reads[0], 3); // 0x101 once, 0x102 and 0x103 bytes
        assert!(
            serde_json::to_string(&d.report())
                .unwrap()
                .contains("{\"port\":\"0x200\",\"reads\":3,\"writes\":1,\"unassigned\":true}")
        );
        d.strict = true;
        assert_eq!(
            (d.port(0, 0x200, 1, None), d.port(0, 0x103, 2, Some(0))),
            (Err(mmio::UNASSIGNED), Err(mmio::UNASSIGNED))
        );
        assert_eq!(d.port(0, 0x100, 1, None), Ok(0x11));
    }
}

/// Metadata-only fixture port: lets the guest wait for a native call to enter its test barrier.
pub struct IoControl(pub Arc<natemu::virtio::testing::Control>);
impl Device for IoControl {
    fn read(&mut self, _: usize, offset: u64, size: u64) -> Option<u64> {
        (offset == 0 && size == 1).then(|| u64::from(self.0.entered()))
    }
    fn write(&mut self, _: usize, _: u64, _: u64, _: u64) -> bool {
        false
    }
}
