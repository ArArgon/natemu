//! virtio-console (virtio 1.2 §5.3) on the [`Model`] interface: device ID 3, a single port (queue 0 receiveq, queue 1
//! transmitq, 256 entries each, as QEMU's virtio-serial), no feature bits (QEMU offers MULTIPORT and EMERG_WRITE by
//! default; this device offers neither), and 12 bytes of configuration that read as zero (cols, rows, max_nr_ports,
//! emerg_wr: valid without F_SIZE and F_MULTIPORT). The guest's first such device is `/dev/hvc0`.
//!
//! Backend: an output file. What the guest transmits is appended to it by the console's own thread; nothing is
//! received. The model holds at most [`TX_BUFFER`] bytes the file has not taken yet; beyond that a request is `Busy`
//! until the writer catches up (QEMU throttles its chardev the same way), after which the writer resumes transmitq.
use super::model::{Direction, Model, ModelPort, QueueSpec, Reply};
use crate::{Result, pci::configuration::PciClassCode};
use std::{
    fs::OpenOptions,
    io::Write,
    path::Path,
    sync::{Arc, Condvar, Mutex, MutexGuard},
    time::{Duration, Instant},
};

/// virtio 1.2 §5: the console's device ID.
const DEVICE_ID: u32 = 3;
/// Both queues' size (QEMU's virtio-serial: 256).
const QUEUE: u16 = 256;
/// The transmit queue (port 0's transmitq).
const TRANSMITQ: u16 = 1;
/// cols, rows (16 bits each), max_nr_ports, emerg_wr (32 bits each).
const CONFIG: usize = 12;
/// The most transmitted bytes the model holds for its backend (accepted and not yet written).
pub(super) const TX_BUFFER: usize = 1 << 20;
/// How long the power-off drain waits for the backend before it discards what is left.
const GRACE: Duration = Duration::from_secs(2);

/// A virtio console on an output file ([`Console::file`]).
pub(crate) struct Console {
    queues: [QueueSpec; 2],
    shared: Arc<Shared>,
}
/// What the model and its writer thread share.
struct Shared {
    tx: Mutex<Tx>,
    /// The writer waits on it for bytes (or the console's end).
    wake: Condvar,
}
#[derive(Default)]
struct Tx {
    /// Accepted bytes the writer has not taken yet.
    pending: Vec<u8>,
    /// Bytes the writer is writing now.
    writing: usize,
    /// A request was answered `Busy`: the writer resumes transmitq once it has written what it holds.
    blocked: bool,
    /// The current incarnation's port (none before DRIVER_OK and after a reset).
    port: Option<ModelPort>,
    /// The power-off drain's end of grace, from its first poll that found bytes held.
    deadline: Option<Instant>,
    /// Bytes thrown away: not written within the drain's grace, or after the output failed.
    discarded: u64,
    /// The console is gone: the writer writes what it holds and ends.
    closed: bool,
}

impl Console {
    /// A console whose transmitted bytes are appended to `path` (created when missing).
    pub(crate) fn file(path: &Path) -> Result<Arc<Self>> {
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(|e| format!("{}: {e}", path.display()))?;
        Self::with_output(Box::new(file))
    }
    /// A console writing to `output` on its own thread.
    fn with_output(output: Box<dyn Write + Send>) -> Result<Arc<Self>> {
        let shared = Arc::new(Shared {
            tx: Mutex::new(Tx::default()),
            wake: Condvar::new(),
        });
        let writer = shared.clone();
        std::thread::Builder::new()
            .name("virtio-console".into())
            .spawn(move || writer.write_out(output))
            .map_err(|e| format!("virtio-console: writer thread: {e}"))?;
        Ok(Arc::new(Self {
            queues: [
                QueueSpec {
                    max: QUEUE,
                    direction: Direction::DeviceWrites,
                    stream: true,
                },
                QueueSpec {
                    max: QUEUE,
                    direction: Direction::DeviceReads,
                    stream: false,
                },
            ],
            shared,
        }))
    }
    /// Bytes discarded so far (power-off grace expired, or the output failed).
    #[cfg_attr(not(test), expect(dead_code))]
    fn discarded(&self) -> u64 {
        self.shared.lock().discarded
    }
}
impl Drop for Console {
    fn drop(&mut self) {
        self.shared.lock().closed = true;
        self.shared.wake.notify_all();
    }
}

impl Shared {
    fn lock(&self) -> MutexGuard<'_, Tx> {
        self.tx.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// The writer thread: takes what is pending, writes it without the lock, and resumes a `Busy` transmitq after.
    fn write_out(&self, mut output: Box<dyn Write + Send>) {
        let mut failed = false;
        loop {
            let bytes = {
                let mut tx = self.lock();
                while tx.pending.is_empty() && !tx.closed {
                    tx = self.wake.wait(tx).unwrap_or_else(|e| e.into_inner());
                }
                if tx.pending.is_empty() {
                    return;
                }
                let bytes = std::mem::take(&mut tx.pending);
                tx.writing = bytes.len();
                bytes
            };
            if !failed {
                if let Err(e) = output.write_all(&bytes).and_then(|()| output.flush()) {
                    eprintln!("virtio-console: output: {e}; later output is discarded");
                    failed = true;
                }
            }
            let resume = {
                let mut tx = self.lock();
                if failed {
                    tx.discarded += bytes.len() as u64;
                }
                tx.writing = 0;
                // The port is called without this lock: `started` and `reset` take it under the device's.
                if std::mem::take(&mut tx.blocked) {
                    tx.port.clone()
                } else {
                    None
                }
            };
            if let Some(port) = resume {
                port.resume(TRANSMITQ);
            }
        }
    }
}

impl Model for Console {
    fn device_id(&self) -> u32 {
        DEVICE_ID
    }
    fn features(&self) -> u64 {
        0
    }
    fn accepts(&self, _: u64) -> bool {
        true
    }
    fn config(&self) -> Vec<u8> {
        vec![0; CONFIG]
    }
    fn queues(&self) -> &[QueueSpec] {
        &self.queues
    }
    /// 07H (communication), subclass 80H (other), as QEMU's virtio-serial-pci.
    fn pci_class(&self) -> (PciClassCode, u8) {
        (PciClassCode::SimpleCommunicationController, 0x80)
    }
    /// transmitq: the piece joins what the writer holds, unless that would pass [`TX_BUFFER`] (then `Busy`, unless the
    /// writer holds nothing: a piece of up to `MAX_BYTES`, 1 MiB, always fits an empty buffer).
    fn request(&self, queue: u16, readable: &[u8], _last: bool, _writable: usize) -> Reply {
        debug_assert_eq!(queue, TRANSMITQ, "receiveq takes no requests");
        let mut tx = self.shared.lock();
        let held = tx.pending.len() + tx.writing;
        if held > 0 && held + readable.len() > TX_BUFFER {
            tx.blocked = true;
            return Reply::Busy;
        }
        tx.pending.extend_from_slice(readable);
        self.shared.wake.notify_one();
        Reply::Done(None)
    }
    fn started(&self, port: ModelPort) {
        self.shared.lock().port = Some(port);
    }
    /// Accepted bytes still go to the output; a `Busy` request was dropped with the incarnation.
    fn reset(&self) {
        let mut tx = self.shared.lock();
        tx.port = None;
        tx.blocked = false;
    }
    /// True once the writer holds nothing; after [`GRACE`] of polls finding bytes held, what is left is discarded and
    /// counted.
    fn drain(&self) -> bool {
        let mut tx = self.shared.lock();
        let held = tx.pending.len() + tx.writing;
        if held == 0 {
            tx.deadline = None;
            return true;
        }
        let deadline = *tx.deadline.get_or_insert_with(|| Instant::now() + GRACE);
        if Instant::now() < deadline {
            return false;
        }
        // A write in progress may still finish; its bytes count as not taken in time.
        tx.pending.clear();
        tx.writing = 0;
        tx.discarded += held as u64;
        tx.deadline = None;
        eprintln!(
            "virtio-console: the output took nothing for {GRACE:?} at power-off; {held} bytes discarded"
        );
        true
    }
}

#[cfg(test)]
mod tests {
    use super::super::model::tests::{Rig, until};
    use super::super::{reg, tests::device};
    use super::*;
    use crate::{devices::Access, pci::Function};

    /// An output that takes bytes only while open, and records them.
    #[derive(Clone, Default)]
    struct Gate {
        state: Arc<(Mutex<(bool, Vec<u8>)>, Condvar)>,
    }
    impl Gate {
        fn open(&self) {
            self.state.0.lock().unwrap().0 = true;
            self.state.1.notify_all();
        }
        fn written(&self) -> Vec<u8> {
            self.state.0.lock().unwrap().1.clone()
        }
    }
    impl Write for Gate {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            let (lock, cv) = &*self.state;
            let mut s = cv.wait_while(lock.lock().unwrap(), |s| !s.0).unwrap();
            s.1.extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    fn temp(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("natemu-console-{}-{name}", std::process::id()))
    }

    #[test]
    fn console_offers_no_features_and_zero_config() {
        let path = temp("features");
        let console = Console::file(&path).unwrap();
        let d = super::super::Virtio::model(device().irqs.clone(), 18, console);
        let read = |offset, size| match d.access(0, offset, size, None) {
            Access::Completed(v) => v,
            other => panic!("read {offset:#x}: {other:?}"),
        };
        let write = |offset, value| {
            assert_eq!(d.access(0, offset, 4, Some(value)), Access::Completed(0));
        };
        assert_eq!(read(reg::DEVICE_ID, 4), 3);
        write(reg::DEVICE_FEATURES_SEL, 0);
        assert_eq!(
            read(reg::DEVICE_FEATURES, 4),
            0,
            "no F_SIZE, F_MULTIPORT or F_EMERG_WRITE"
        );
        write(reg::DEVICE_FEATURES_SEL, 1);
        assert_eq!(read(reg::DEVICE_FEATURES, 4), 1, "VERSION_1 alone");
        for (q, max) in [(0, 256), (1, 256), (2, 0)] {
            write(reg::QUEUE_SEL, q);
            assert_eq!(read(reg::QUEUE_NUM_MAX, 4), max, "queue {q}");
        }
        for at in (0..CONFIG as u64).step_by(4) {
            assert_eq!(read(reg::CONFIG + at, 4), 0, "config {at}");
        }
        assert_eq!(read(reg::CONFIG, 2), 0, "cols");
        assert_eq!(read(reg::CONFIG + 2, 2), 0, "rows");
        // A driver accepting F_SIZE (bit 0), which is not offered, does not get FEATURES_OK.
        write(reg::STATUS, 1);
        write(reg::STATUS, 3);
        write(reg::DRIVER_FEATURES_SEL, 0);
        write(reg::DRIVER_FEATURES, 1);
        write(reg::DRIVER_FEATURES_SEL, 1);
        write(reg::DRIVER_FEATURES, 1);
        write(reg::STATUS, 11);
        assert_eq!(read(reg::STATUS, 4), 3);
        let f = d.pci_with(1, Arc::new(|_, _| {}));
        assert_eq!(
            f.config_read(2),
            0x0780_0001,
            "class 07h, subclass 80h, revision 1"
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn transmit_appends_to_output_file() {
        let path = temp("append");
        std::fs::write(&path, b"before\n").unwrap();
        let rig = Rig::with(Console::file(&path).unwrap());
        rig.put(0x60000, b"hello, ");
        rig.put(0x61000, b"virtio ");
        rig.put(0x62000, b"console\n");
        rig.run(|rig| {
            // One chain of two buffers, then a chain of one.
            rig.post(1, &[(0x60000, 7, false), (0x61000, 7, false)]);
            rig.post(1, &[(0x62000, 8, false)]);
            rig.kick(1);
            until("both used", || rig.used(1) == 2);
            assert_eq!(rig.element(1, 1).1, 0, "no reply");
            until("drained", || rig.model.drain());
        });
        assert_eq!(
            std::fs::read(&path).unwrap(),
            b"before\nhello, virtio console\n"
        );
        assert_eq!(rig.model.discarded(), 0);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn transmit_backlog_turns_busy_at_1_mib() {
        let gate = Gate::default();
        let rig = Rig::with(Console::with_output(Box::new(gate.clone())).unwrap());
        // Five chains of 256 KiB (pattern bytes) while the output takes nothing: the first four fill the 1 MiB.
        const CHAIN: usize = 256 * 1024;
        let data: Vec<u8> = (0..5 * CHAIN).map(|i| (i * 7 + i / 4096) as u8).collect();
        rig.put(0x100000, &data);
        rig.run(|rig| {
            for k in 0..5 {
                rig.post(1, &[(0x100000 + (k * CHAIN) as u64, CHAIN as u32, false)]);
            }
            rig.kick(1);
            until("1 MiB accepted", || rig.used(1) == 4);
            std::thread::sleep(Duration::from_millis(50));
            assert_eq!(rig.used(1), 4, "the fifth chain waits while 1 MiB is held");
            assert!(rig.model.shared.lock().blocked, "answered Busy");
            assert!(!rig.model.drain(), "bytes are held");
            // The output catches up: the writer resumes transmitq, and the fifth chain completes.
            gate.open();
            until("the fifth chain", || rig.used(1) == 5);
            until("drained", || rig.model.drain());
            assert!(!rig.needs_reset());
        });
        assert!(gate.written() == data, "every byte once, in order");
        assert_eq!(rig.model.discarded(), 0);
    }

    /// A stalled output does not hold the power-off drain past its grace: what is left is discarded and counted.
    #[test]
    fn drain_gives_up_after_its_grace() {
        let gate = Gate::default();
        let console = Console::with_output(Box::new(gate.clone())).unwrap();
        assert!(console.drain(), "nothing held");
        assert!(matches!(
            console.request(TRANSMITQ, &[1; 100], true, 0),
            Reply::Done(None)
        ));
        let start = Instant::now();
        while !console.drain() {
            std::thread::sleep(Duration::from_millis(10));
        }
        let took = start.elapsed();
        assert!(took >= GRACE && took < GRACE * 2, "{took:?}");
        assert_eq!(console.discarded(), 100);
        assert!(console.drain(), "stays drained");
        gate.open();
    }
}
