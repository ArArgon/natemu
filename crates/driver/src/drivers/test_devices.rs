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

/// Mapped device memory's test device (docs/restructure/mapped-device-memory-plan.md, `run-program --mapped-at GPA,LEN`
/// and `--mapped-control-at GPA`): the control page of region 0. 8-byte registers: `MOVE` (+0, write: a new base for
/// region 0, published into the region table from this handler, so the writing vCPU's exit spans the publish and its
/// resident runs the remap shootdown; 0 disables the region; a base the driver does not allow, see
/// [`MappedControl::allowed`], is refused), `SUM` (+8, read: the wrapping 64-bit sum of the region's words, read through a
/// second, read-only mapping of the object, [`natemu::pci::memory::View`], never the guest's) and `FILL` (+16, write: the
/// host fills the region, word k = [`fill_word`]`(value, k)`). Any other access is refused.
pub struct MappedControl {
    pub memory: Arc<natemu::pci::DeviceMemory>,
    view: natemu::pci::memory::View,
    /// The region table, region 0's alias and the vCPUs' halt hints (and their number), set once the machine exists (the
    /// device is registered before it).
    pub target: Arc<std::sync::OnceLock<(natemu::execution::MappedTable, u64, natemu::execution::io::HaltedView, usize)>>,
    /// `MOVE`s published.
    pub publishes: Arc<std::sync::atomic::AtomicU64>,
    /// The vCPUs parked in a HLT when the last `MOVE` was published (`halted_peer_drops_on_wake` checks that its peers
    /// were: else its shootdown met running peers only).
    pub halted_at_publish: Arc<std::sync::Mutex<Vec<usize>>>,
    /// Whether region 0 may be enabled at a base (every page clear of RAM, ROM, the driver's devices and this page).
    pub allowed: Box<dyn Fn(u64) -> bool + Send>,
}
impl MappedControl {
    pub(crate) const MOVE: u64 = 0;
    pub(crate) const SUM: u64 = 8;
    pub(crate) const FILL: u64 = 16;
    pub fn new(memory: Arc<natemu::pci::DeviceMemory>, allowed: Box<dyn Fn(u64) -> bool + Send>) -> natemu::Result<Self> {
        let view = memory.view()?;
        Ok(Self {
            memory,
            view,
            target: Default::default(),
            publishes: Default::default(),
            halted_at_publish: Default::default(),
            allowed,
        })
    }
    /// The sum of the region's words through the view.
    pub fn sum(&self) -> u64 {
        region_sum(&self.view, self.memory.len())
    }
}
/// The wrapping sum of `len` bytes' words of `view`.
pub fn region_sum(view: &natemu::pci::memory::View, len: u64) -> u64 {
    let mut words = vec![0u64; 4096];
    let mut sum = 0u64;
    for at in (0..len).step_by(8 * words.len()) {
        view.read_words(at, &mut words);
        sum = words.iter().fold(sum, |a, &w| a.wrapping_add(w));
    }
    sum
}
/// `FILL`'s pattern: word `k` of the region for the value written.
pub fn fill_word(value: u64, k: u64) -> u64 {
    value ^ k.wrapping_mul(0x9e37_79b9_7f4a_7c15)
}
impl Device for MappedControl {
    fn read(&mut self, _vcpu: usize, offset: u64, size: u64) -> Option<u64> {
        (offset == Self::SUM && size == 8).then(|| self.sum())
    }
    fn write(&mut self, _vcpu: usize, offset: u64, size: u64, value: u64) -> bool {
        match (offset, size) {
            (Self::FILL, 8) => {
                let words: Vec<u64> = (0..self.memory.len() / 8).map(|k| fill_word(value, k)).collect();
                self.memory.write_words(0, &words);
                true
            }
            (Self::MOVE, 8) if value == 0 || (self.allowed)(value) => {
                let Some((table, alias, halted, n)) = self.target.get() else {
                    return false;
                };
                *self.halted_at_publish.lock().unwrap() = (0..*n).filter(|&v| halted.halted(v)).collect();
                // The single writer: this device, under its registry lock (the driver's publishes precede every vCPU).
                table.publish(
                    0,
                    natemu_abi::execution::cache::MappedRegion {
                        base: value,
                        len: self.memory.len(),
                        alias: *alias,
                        enabled: u32::from(value != 0),
                    },
                );
                self.publishes.fetch_add(1, SeqCst);
                true
            }
            _ => false,
        }
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
/// Port 600H reads whether the schedule entered its backend call (or, with `held`, whether the
/// driver's publication hook holds a completion); 601H answers 1 after 300 ms, a host exit long
/// enough to outlast a windowless copy's drain deadline.
pub struct IoControl(
    pub Arc<natemu::virtio::testing::Control>,
    pub Option<Arc<std::sync::atomic::AtomicBool>>,
);
impl Device for IoControl {
    fn read(&mut self, _: usize, offset: u64, size: u64) -> Option<u64> {
        match (offset, size) {
            (0, 1) => Some(u64::from(match &self.1 {
                Some(held) => held.load(SeqCst),
                None => self.0.entered(),
            })),
            (1, 1) => {
                std::thread::sleep(std::time::Duration::from_millis(300));
                Some(1)
            }
            _ => None,
        }
    }
    fn write(&mut self, _: usize, _: u64, _: u64, _: u64) -> bool {
        false
    }
}
