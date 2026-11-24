//! virtio-console (virtio 1.2 §5.3) on the [`Model`] interface: device ID 3, a single port (queue 0 receiveq, queue 1
//! transmitq, 256 entries each, as QEMU's virtio-serial), no feature bits (QEMU offers MULTIPORT and EMERG_WRITE by
//! default; this device offers neither), and 12 bytes of configuration that read as zero (cols, rows, max_nr_ports,
//! emerg_wr: valid without F_SIZE and F_MULTIPORT). The guest's first such device is `/dev/hvc0`.
//!
//! Backends, one per console, each on the console's own thread:
//! - an output file ([`Console::file`]): what the guest transmits is appended to it; nothing is received.
//! - a Unix-domain socket natemu listens on ([`Console::socket`]), one client at a time (a second connection while
//!   one is open is closed at once). What the client sends goes to receiveq as `ModelPort::deliver` takes it: the
//!   backend reads at most [`INPUT`] bytes ahead of the guest and then stops reading, so a fast client waits in its
//!   writes. What the guest transmits goes to the client, or is discarded and counted while none is connected.
//!
//! The model holds at most [`TX_BUFFER`] transmitted bytes its backend has not taken yet; beyond that a request is
//! `Busy` until the backend catches up (QEMU throttles its chardev the same way), after which it resumes transmitq.
use super::model::{Direction, Model, ModelPort, QueueSpec, Reply};
use crate::{
    Result,
    irq::{Busy, Irqs},
    pci::configuration::PciClassCode,
};
use std::{
    fs::OpenOptions,
    io::{ErrorKind, Read, Write},
    os::{
        fd::AsRawFd,
        unix::{
            fs::FileTypeExt,
            net::{UnixListener, UnixStream},
        },
    },
    path::{Path, PathBuf},
    sync::{
        Arc, Condvar, Mutex, MutexGuard,
        atomic::{AtomicBool, Ordering::SeqCst},
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};

/// virtio 1.2 §5: the console's device ID.
const DEVICE_ID: u32 = 3;
/// Both queues' size (QEMU's virtio-serial: 256).
const QUEUE: u16 = 256;
/// The receive queue (port 0's receiveq).
const RECEIVEQ: u16 = 0;
/// The transmit queue (port 0's transmitq).
const TRANSMITQ: u16 = 1;
/// cols, rows (16 bits each), max_nr_ports, emerg_wr (32 bits each).
const CONFIG: usize = 12;
/// The most transmitted bytes the model holds for its backend (accepted and not yet written).
pub(super) const TX_BUFFER: usize = 1 << 20;
/// How long the power-off drain waits for the backend before it discards what is left.
const GRACE: Duration = Duration::from_secs(2);
/// The most client bytes the socket backend holds that the guest has not taken.
const INPUT: usize = 64 * 1024;
/// `sun_path`'s size on macOS: a socket path is shorter.
const SUN_PATH: usize = 104;

/// A virtio console on an output file ([`Console::file`]) or a Unix socket ([`Console::socket`]).
pub(crate) struct Console {
    queues: [QueueSpec; 2],
    shared: Arc<Shared>,
    /// The socket backend's path (removed with the console) and thread (joined).
    socket: Option<(PathBuf, JoinHandle<()>)>,
}
/// What the model and its backend thread share.
struct Shared {
    tx: Mutex<Tx>,
    /// The file writer waits on it for bytes (or the console's end).
    wake: Condvar,
    /// The socket backend's doorbell.
    bell: Option<Bell>,
}
/// A socket pair's write end the socket backend polls the other end of; written only while it may sleep.
struct Bell {
    ring: UnixStream,
    /// Set by the backend before its last look at the shared state, cleared by the first ring after it.
    armed: AtomicBool,
}
#[derive(Default)]
struct Tx {
    /// Accepted bytes the backend has not taken yet.
    pending: Vec<u8>,
    /// Bytes the backend has taken and not yet written.
    writing: usize,
    /// A request was answered `Busy`: the backend resumes transmitq once it has written what it took.
    blocked: bool,
    /// The current incarnation's port (none before DRIVER_OK and after a reset).
    port: Option<ModelPort>,
    /// DRIVER_OKs so far: the socket backend registers its doorbell with each new port.
    starts: u64,
    /// The power-off drain's end of grace, from its first poll that found bytes held.
    deadline: Option<Instant>,
    /// The drain counted `writing` as discarded: what the backend's write in progress took after all is uncounted, and
    /// the rest of what it took is dropped.
    cut: bool,
    /// Bytes thrown away: not taken within the drain's grace, after the output failed, or with no client connected.
    discarded: u64,
    /// The console is gone: the file writer writes what it holds and ends; the socket backend ends.
    closed: bool,
}

const QUEUES: [QueueSpec; 2] = [
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
];

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
        let shared = Arc::new(Shared::new(None));
        let writer = shared.clone();
        std::thread::Builder::new()
            .name("virtio-console".into())
            .spawn(move || writer.write_out(output))
            .map_err(|e| format!("virtio-console: writer thread: {e}"))?;
        Ok(Arc::new(Self {
            queues: QUEUES,
            shared,
            socket: None,
        }))
    }
    /// A console on a Unix socket natemu listens on at `path` (removed when the console ends). A stale socket there
    /// (nobody listening) is replaced; any other file there is refused. The backend holds an `irqs` busy guard for its
    /// whole life (a client may connect and send at any time): a guest waiting for its input is not ended as all-halted.
    pub(crate) fn socket(path: &Path, irqs: Irqs) -> Result<Arc<Self>> {
        let fail = |e: String| format!("{}: {e}", path.display());
        let listener = listen(path).map_err(fail)?;
        let pair = UnixStream::pair().and_then(|(ring, bell)| {
            ring.set_nonblocking(true)?;
            bell.set_nonblocking(true)?;
            Ok((ring, bell))
        });
        let started = pair.and_then(|(ring, bell)| {
            let shared = Arc::new(Shared::new(Some(Bell {
                ring,
                armed: AtomicBool::new(false),
            })));
            let backend = Socket {
                shared: shared.clone(),
                listener,
                bell,
                _arrival: irqs.busy(),
                client: None,
                input: Vec::new(),
                delivered: 0,
                out: Vec::new(),
                written: 0,
                port: None,
                starts: 0,
            };
            let thread = std::thread::Builder::new()
                .name("virtio-console".into())
                .spawn(move || backend.serve())?;
            Ok((shared, thread))
        });
        let (shared, thread) = started.map_err(|e| {
            let _ = std::fs::remove_file(path);
            fail(format!("virtio-console: {e}"))
        })?;
        Ok(Arc::new(Self {
            queues: QUEUES,
            shared,
            socket: Some((path.to_owned(), thread)),
        }))
    }
    /// Bytes discarded so far (power-off grace expired, the output failed, or no client).
    #[cfg_attr(not(test), expect(dead_code))]
    fn discarded(&self) -> u64 {
        self.shared.lock().discarded
    }
}
impl Drop for Console {
    fn drop(&mut self) {
        self.shared.lock().closed = true;
        self.shared.signal();
        if let Some((path, thread)) = self.socket.take() {
            // The last device reference can be the backend's own (a delivery's): it ends after this returns.
            if thread.thread().id() != std::thread::current().id() {
                let _ = thread.join();
            }
            let _ = std::fs::remove_file(path);
        }
        let discarded = self.shared.lock().discarded;
        if discarded > 0 {
            eprintln!("virtio-console: {discarded} bytes of guest output were discarded");
        }
    }
}

/// A listening socket at `path`, non-blocking.
fn listen(path: &Path) -> std::result::Result<UnixListener, String> {
    let len = path.as_os_str().len();
    if len >= SUN_PATH {
        return Err(format!(
            "a Unix socket path is at most {} bytes; this one has {len}",
            SUN_PATH - 1
        ));
    }
    match std::fs::symlink_metadata(path) {
        Ok(m) if !m.file_type().is_socket() => return Err("exists and is not a socket".into()),
        Ok(_) => match UnixStream::connect(path) {
            Ok(_) => return Err("another program listens on this socket".into()),
            // A socket an earlier run left behind.
            Err(e) if e.kind() == ErrorKind::ConnectionRefused => {
                std::fs::remove_file(path).map_err(|e| e.to_string())?
            }
            Err(e) => return Err(e.to_string()),
        },
        Err(e) if e.kind() == ErrorKind::NotFound => {}
        Err(e) => return Err(e.to_string()),
    }
    let listener = UnixListener::bind(path).map_err(|e| e.to_string())?;
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    Ok(listener)
}

impl Shared {
    fn new(bell: Option<Bell>) -> Self {
        Self {
            tx: Mutex::new(Tx::default()),
            wake: Condvar::new(),
            bell,
        }
    }
    fn lock(&self) -> MutexGuard<'_, Tx> {
        self.tx.lock().unwrap_or_else(|e| e.into_inner())
    }
    /// Wake the backend: never blocks (also under the device's state lock).
    fn signal(&self) {
        self.wake.notify_all();
        if let Some(bell) = &self.bell {
            if bell.armed.swap(false, SeqCst) {
                let _ = (&bell.ring).write(&[1]);
            }
        }
    }
    /// The file writer thread: takes what is pending, writes it without the lock, and resumes a `Busy` transmitq after.
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
                if std::mem::take(&mut tx.cut) {
                    // The drain counted these bytes; they reached the file after all.
                    if !failed {
                        tx.discarded = tx.discarded.saturating_sub(bytes.len() as u64);
                    }
                } else if failed {
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

/// The socket backend's thread: everything non-blocking, then `poll(2)` on the doorbell, the listener and the client.
/// It calls the port without the console's lock (`started` and `reset` take that lock under the device's).
struct Socket {
    shared: Arc<Shared>,
    listener: UnixListener,
    /// The doorbell's read end.
    bell: UnixStream,
    /// A listening socket can still deliver data (a client may connect at any time): all-HLT is not VM completion.
    _arrival: Busy,
    client: Option<UnixStream>,
    /// Client bytes read, of which the guest has taken `delivered`.
    input: Vec<u8>,
    delivered: usize,
    /// Transmitted bytes taken from `Tx::pending`, of which `written` reached the client.
    out: Vec<u8>,
    written: usize,
    /// The port as last seen, and `Tx::starts` then.
    port: Option<ModelPort>,
    starts: u64,
}
impl Socket {
    fn serve(mut self) {
        loop {
            self.bell().armed.store(true, SeqCst);
            let Some(mut progress) = self.sync() else {
                return;
            };
            progress |= self.accept();
            progress |= self.write();
            progress |= self.read();
            progress |= self.deliver();
            let mut fds = vec![
                poll_fd(self.bell.as_raw_fd(), libc::POLLIN),
                poll_fd(self.listener.as_raw_fd(), libc::POLLIN),
            ];
            if let Some(client) = &self.client {
                let mut events = 0;
                if self.room() > 0 {
                    events |= libc::POLLIN;
                }
                if self.written < self.out.len() {
                    events |= libc::POLLOUT;
                }
                // With neither, a hang-up is noticed by the next read.
                if events != 0 {
                    fds.push(poll_fd(client.as_raw_fd(), events));
                }
            }
            let timeout = if progress { 0 } else { -1 };
            // SAFETY: `fds` is a live array of `fds.len()` entries of open descriptors.
            unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, timeout) };
            self.bell().armed.store(false, SeqCst);
            let mut ring = [0; 64];
            while matches!((&self.bell).read(&mut ring), Ok(n) if n > 0) {}
        }
    }
    fn bell(&self) -> &Bell {
        self.shared
            .bell
            .as_ref()
            .expect("a socket console has a doorbell")
    }
    /// The model's side: the port, a power-off cut, the next transmitted bytes (discarded with no client). None once
    /// the console is gone; else whether anything changed.
    fn sync(&mut self) -> Option<bool> {
        let shared = self.shared.clone();
        let (register, resume, progress) = {
            let mut tx = shared.lock();
            if tx.closed {
                return None;
            }
            let mut progress = false;
            let mut resume = self.settle(&mut tx, 0);
            let register = (tx.starts != self.starts)
                .then(|| tx.port.clone())
                .flatten();
            self.starts = tx.starts;
            self.port = tx.port.clone();
            if self.out.is_empty() && !tx.pending.is_empty() {
                self.out = std::mem::take(&mut tx.pending);
                self.written = 0;
                tx.writing = self.out.len();
                progress = true;
            }
            if self.client.is_none() && !self.out.is_empty() {
                resume = resume.or(self.discard(&mut tx));
                progress = true;
            }
            let progress = progress || resume.is_some();
            (register, resume, progress)
        };
        if let Some(port) = register {
            let shared = self.shared.clone();
            port.on_ready(move || shared.signal());
        }
        if let Some(port) = resume {
            port.resume(TRANSMITQ);
        }
        Some(progress)
    }
    /// After `wrote` more bytes reached the client (under the console's lock): the port to resume, once all it took
    /// is written or the drain cut it.
    fn settle(&mut self, tx: &mut Tx, wrote: usize) -> Option<ModelPort> {
        if std::mem::take(&mut tx.cut) {
            // The drain counted all of `out` not written before; `wrote` of it reached the client after all.
            tx.discarded = tx.discarded.saturating_sub(wrote as u64);
            return self.finish(tx);
        }
        self.written += wrote;
        tx.writing -= wrote;
        if !self.out.is_empty() && self.written == self.out.len() {
            return self.finish(tx);
        }
        None
    }
    /// Drop what is left of `out`, counting it.
    fn discard(&mut self, tx: &mut Tx) -> Option<ModelPort> {
        tx.discarded += (self.out.len() - self.written) as u64;
        self.finish(tx)
    }
    fn finish(&mut self, tx: &mut Tx) -> Option<ModelPort> {
        self.out = Vec::new();
        self.written = 0;
        tx.writing = 0;
        if std::mem::take(&mut tx.blocked) {
            tx.port.clone()
        } else {
            None
        }
    }
    /// Take new connections: the first while none is open becomes the client; others are closed at once.
    fn accept(&mut self) -> bool {
        let mut progress = false;
        while let Ok((stream, _)) = self.listener.accept() {
            progress = true;
            if self.client.is_some() || !client_ready(&stream) {
                continue;
            }
            self.client = Some(stream);
        }
        progress
    }
    /// Write what is left of `out` to the client.
    fn write(&mut self) -> bool {
        let Some(client) = &mut self.client else {
            return false;
        };
        if self.written == self.out.len() {
            return false;
        }
        match client.write(&self.out[self.written..]) {
            Ok(n) => {
                let resume = self.settle(&mut self.shared.clone().lock(), n);
                if let Some(port) = resume {
                    port.resume(TRANSMITQ);
                }
                true
            }
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::Interrupted) => false,
            Err(_) => {
                self.gone();
                true
            }
        }
    }
    /// Read client bytes while fewer than [`INPUT`] wait for the guest.
    fn read(&mut self) -> bool {
        let room = self.room();
        let Some(client) = &mut self.client else {
            return false;
        };
        if room == 0 {
            return false;
        }
        self.input.drain(..self.delivered);
        self.delivered = 0;
        let start = self.input.len();
        self.input.resize(start + room, 0);
        let read = client.read(&mut self.input[start..]);
        self.input.truncate(start + *read.as_ref().unwrap_or(&0));
        match read {
            Ok(0) => {
                self.gone();
                true
            }
            Ok(_) => true,
            Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::Interrupted) => false,
            Err(_) => {
                self.gone();
                true
            }
        }
    }
    /// Offer the guest the client bytes it has not taken; it takes what fits (the rest waits for `on_ready`).
    fn deliver(&mut self) -> bool {
        let Some(port) = &self.port else {
            return false;
        };
        if self.delivered == self.input.len() {
            return false;
        }
        let n = port.deliver(RECEIVEQ, &self.input[self.delivered..]);
        self.delivered += n;
        if self.delivered == self.input.len() {
            self.input.clear();
            self.delivered = 0;
        }
        n > 0
    }
    fn room(&self) -> usize {
        INPUT - (self.input.len() - self.delivered)
    }
    /// The client hung up or failed: what it did not take is discarded; its bytes already read still go to the guest.
    fn gone(&mut self) {
        self.client = None;
        let resume = {
            let shared = self.shared.clone();
            let mut tx = shared.lock();
            let resume = self.settle(&mut tx, 0);
            if self.out.is_empty() {
                resume
            } else {
                resume.or(self.discard(&mut tx))
            }
        };
        if let Some(port) = resume {
            port.resume(TRANSMITQ);
        }
    }
}
fn poll_fd(fd: i32, events: i16) -> libc::pollfd {
    libc::pollfd {
        fd,
        events,
        revents: 0,
    }
}
/// A new client non-blocking, and writes to it after it hung up an error instead of SIGPIPE.
fn client_ready(stream: &UnixStream) -> bool {
    let on: libc::c_int = 1;
    // SAFETY: an open socket and an int option value of its size.
    let nosigpipe = unsafe {
        libc::setsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_NOSIGPIPE,
            (&raw const on).cast(),
            size_of::<libc::c_int>() as libc::socklen_t,
        )
    };
    nosigpipe == 0 && stream.set_nonblocking(true).is_ok()
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
    /// transmitq: the piece joins what the backend holds, unless that would pass [`TX_BUFFER`] (then `Busy`, unless
    /// the backend holds nothing: a piece of up to `MAX_BYTES`, 1 MiB, always fits an empty buffer).
    fn request(&self, queue: u16, readable: &[u8], _last: bool, _writable: usize) -> Reply {
        debug_assert_eq!(queue, TRANSMITQ, "receiveq takes no requests");
        let mut tx = self.shared.lock();
        let held = tx.pending.len() + tx.writing;
        if held > 0 && held + readable.len() > TX_BUFFER {
            tx.blocked = true;
            return Reply::Busy;
        }
        tx.pending.extend_from_slice(readable);
        drop(tx);
        self.shared.signal();
        Reply::Done(None)
    }
    fn started(&self, port: ModelPort) {
        let mut tx = self.shared.lock();
        tx.port = Some(port);
        tx.starts += 1;
        drop(tx);
        self.shared.signal();
    }
    /// Accepted bytes still go to the backend; a `Busy` request was dropped with the incarnation.
    fn reset(&self) {
        let mut tx = self.shared.lock();
        tx.port = None;
        tx.blocked = false;
        drop(tx);
        self.shared.signal();
    }
    /// True once the backend holds nothing; after [`GRACE`] of polls finding bytes held, what is left is discarded and
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
        tx.pending.clear();
        tx.cut = tx.writing > 0;
        tx.writing = 0;
        tx.discarded += held as u64;
        tx.deadline = None;
        drop(tx);
        self.shared.signal();
        eprintln!(
            "virtio-console: at power-off the backend had not taken {held} bytes after {GRACE:?}; they are discarded"
        );
        true
    }
}

#[cfg(test)]
mod tests {
    use super::super::model::tests::{Rig, until};
    use super::super::{model::BACKLOG, reg, tests::device};
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
            // 1 MiB is held: read under the lock, since `drain` would start its 2 s grace and a slow run could pass it.
            let tx = rig.model.shared.lock();
            assert_eq!(tx.pending.len() + tx.writing, 4 * CHAIN, "1 MiB is held");
            drop(tx);
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

    /// A short private folder for sockets (`sun_path` holds 104 bytes on macOS), removed with everything in it.
    struct Dir(std::path::PathBuf);
    impl Dir {
        fn new() -> Self {
            use std::os::unix::fs::DirBuilderExt;
            static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
            let base = std::env::temp_dir();
            let base = if base.as_os_str().len() > 64 {
                "/tmp".into()
            } else {
                base
            };
            let n = N.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let dir = base.join(format!("nm-{}-{n}", std::process::id()));
            std::fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
            Self(dir)
        }
        fn path(&self, name: &str) -> std::path::PathBuf {
            self.0.join(name)
        }
    }
    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    /// A client of the console's socket (reads time out after 5 s).
    fn connect(path: &Path) -> UnixStream {
        let c = UnixStream::connect(path).unwrap();
        c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        c
    }
    /// A socket console in `dir`, started, and the interrupt source its backend's busy guard counts on.
    fn socket_rig(dir: &Dir) -> (Rig<Console>, std::path::PathBuf, Irqs) {
        let path = dir.path("c");
        let irqs = device().irqs.clone();
        let rig = Rig::with(Console::socket(&path, irqs.clone()).unwrap());
        (rig, path, irqs)
    }
    /// Transmit `bytes` as one chain (from `at`) and wait for it to complete.
    fn transmit(rig: &Rig<Console>, at: u64, bytes: &[u8]) {
        let used = rig.used(1);
        rig.put(at, bytes);
        rig.post(1, &[(at, bytes.len() as u32, false)]);
        rig.kick(1);
        until("transmitted", || rig.used(1) == used.wrapping_add(1));
    }
    fn read_exactly(c: &mut UnixStream, n: usize) -> Vec<u8> {
        let mut got = vec![0; n];
        c.read_exact(&mut got).unwrap();
        got
    }

    /// What a client sends reaches receiveq. The backend holds one busy guard for its whole life, with or without a
    /// client (a guest waiting for input is not ended as all-halted: a client may connect at any time), and lets go
    /// when the console ends.
    #[test]
    fn socket_client_bytes_reach_receiveq() {
        let dir = Dir::new();
        let (rig, path, irqs) = socket_rig(&dir);
        rig.run(|rig| {
            assert_eq!(irqs.live(), 1, "listening with no client holds the guard");
            let mut client = connect(&path);
            client.write_all(b"hello, guest\n").unwrap();
            rig.post_rx(0, 64);
            rig.post_rx(1, 64);
            rig.kick(0);
            until("received", || rig.received() == b"hello, guest\n");
            assert_eq!(irqs.live(), 1, "one guard with a client");
            drop(client);
            transmit(rig, 0x60000, b"gone");
            until("the hang-up seen", || rig.model.discarded() == 4);
            assert_eq!(irqs.live(), 1, "the guard outlives the client");
        });
        drop(rig);
        assert_eq!(irqs.live(), 0, "released when the console ends");
    }

    /// Client bytes beyond what the guest has posted and the receive backlog wait in the backend (and the socket)
    /// until the guest posts more: every byte arrives once, in order.
    #[test]
    fn socket_input_respects_deliver_count() {
        const TOTAL: usize = 192 * 1024;
        /// The guest's receive buffers: 16 of 4 KiB, reposted as they are used.
        const BUFFERS: u16 = 16;
        const AT: u64 = 0x10000;
        let dir = Dir::new();
        let (rig, path, _irqs) = socket_rig(&dir);
        let data: Vec<u8> = (0..TOTAL).map(|i| (i * 13 + i / 4093) as u8).collect();
        let mut client = connect(&path);
        let (bytes, sent) = (
            data.clone(),
            Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        );
        let counted = sent.clone();
        let writer = std::thread::spawn(move || {
            for chunk in bytes.chunks(4096) {
                client.write_all(chunk).unwrap();
                counted.fetch_add(chunk.len(), std::sync::atomic::Ordering::SeqCst);
            }
            client
        });
        rig.run(|rig| {
            // No buffer posted yet: the receive backlog fills, and the client sends well beyond it (into what the
            // backend reads ahead), so `deliver` was offered bytes it did not take before the guest posts any.
            until("the client is ahead of the backlog", || {
                sent.load(std::sync::atomic::Ordering::SeqCst) >= BACKLOG + 48 * 1024
            });
            let held: usize = rig.d.lock().received[0]
                .iter()
                .map(|(_, a)| match a {
                    super::super::Arrival::Bytes(b) => b.len(),
                    super::super::Arrival::Packet(_) => 0,
                })
                .sum();
            assert_eq!(held, BACKLOG, "the backlog is full");
            let mut got = Vec::new();
            let (mut posted, mut seen) = (0u16, 0u16);
            while got.len() < TOTAL {
                let used = rig.used(0);
                for slot in seen..used {
                    let len = rig.element(0, slot).1 as usize;
                    got.extend(rig.get(AT + u64::from(slot % BUFFERS) * 0x1000, len));
                }
                seen = used;
                while posted != seen.wrapping_add(BUFFERS) {
                    rig.post(
                        0,
                        &[(AT + u64::from(posted % BUFFERS) * 0x1000, 0x1000, true)],
                    );
                    posted = posted.wrapping_add(1);
                }
                rig.kick(0);
                if got.len() < TOTAL {
                    until("more input", || rig.used(0) != seen);
                }
            }
            assert!(
                got == data,
                "every byte once, in order ({} bytes)",
                got.len()
            );
        });
        drop(writer.join().unwrap());
    }

    /// With no client, what the guest transmits completes at once and is discarded and counted (no `Busy` stall).
    #[test]
    fn transmit_without_client_is_counted() {
        const CHAIN: usize = 512 * 1024;
        let dir = Dir::new();
        let (rig, _path, _irqs) = socket_rig(&dir);
        rig.put(0x100000, &vec![0x5a; 3 * CHAIN]);
        rig.run(|rig| {
            for k in 0..3 {
                rig.post(1, &[(0x100000 + (k * CHAIN) as u64, CHAIN as u32, false)]);
            }
            rig.kick(1);
            until("all three used", || rig.used(1) == 3);
            until("counted", || rig.model.discarded() == 3 * CHAIN as u64);
            assert!(rig.model.drain(), "nothing held");
            assert!(!rig.needs_reset());
        });
    }

    /// After the client goes, guest writes keep completing (discarded, counted); a new client gets later output.
    #[test]
    fn client_reconnect() {
        let dir = Dir::new();
        let (rig, path, _irqs) = socket_rig(&dir);
        rig.run(|rig| {
            let mut a = connect(&path);
            a.write_all(b"a").unwrap();
            rig.post_rx(0, 64);
            rig.kick(0);
            until("a's input", || rig.received() == b"a");
            transmit(rig, 0x60000, b"one");
            assert_eq!(read_exactly(&mut a, 3), b"one");
            drop(a);
            transmit(rig, 0x61000, b"lost!");
            until("discarded", || rig.model.discarded() == 5);
            let mut b = connect(&path);
            b.write_all(b"b").unwrap();
            rig.post_rx(1, 64);
            rig.kick(0);
            until("b's input", || rig.received() == b"ab");
            transmit(rig, 0x62000, b"two");
            assert_eq!(
                read_exactly(&mut b, 3),
                b"two",
                "later output, and not the discarded bytes"
            );
            assert_eq!(rig.model.discarded(), 5);
        });
    }

    /// A second connection while one is open is closed at once; the first keeps working.
    #[test]
    fn one_client_at_a_time() {
        let dir = Dir::new();
        let (rig, path, _irqs) = socket_rig(&dir);
        rig.run(|rig| {
            let mut a = connect(&path);
            a.write_all(b"a").unwrap();
            rig.post_rx(0, 64);
            rig.post_rx(1, 64);
            rig.kick(0);
            until("a's input", || rig.received() == b"a");
            let mut b = connect(&path);
            let mut buf = [0; 16];
            assert_eq!(
                b.read(&mut buf).unwrap(),
                0,
                "the second client is closed at once"
            );
            transmit(rig, 0x60000, b"x");
            assert_eq!(read_exactly(&mut a, 1), b"x");
            assert_eq!(rig.received(), b"a");
            assert_eq!(rig.model.discarded(), 0);
        });
    }

    /// A connected client that stops reading: its input is still served, and the power-off drain still ends within
    /// its grace (the device quiet, so S5 ends with Power), the bytes the client did not take counted exactly.
    #[test]
    fn poweroff_with_stalled_client() {
        const CHAIN: usize = 256 * 1024;
        let dir = Dir::new();
        let (rig, path, _irqs) = socket_rig(&dir);
        let shared = rig.model.shared.clone();
        let data: Vec<u8> = (0..2 * CHAIN).map(|i| (i * 7 + i / 4099) as u8).collect();
        rig.put(0x100000, &data);
        let mut client = connect(&path);
        rig.run(|rig| {
            client.write_all(b"a").unwrap();
            rig.post_rx(0, 64);
            rig.post_rx(1, 64);
            rig.kick(0);
            until("connected", || rig.received() == b"a");
            // Far more than the socket buffers hold; the client reads nothing.
            for k in 0..2 {
                rig.post(1, &[(0x100000 + (k * CHAIN) as u64, CHAIN as u32, false)]);
            }
            rig.kick(1);
            until("both chains accepted", || rig.used(1) == 2);
            // Its input still arrives: the backend is not stuck in a write.
            client.write_all(b"b").unwrap();
            until("input while stalled", || rig.received() == b"ab");
            let start = Instant::now();
            until("quiet", || rig.d.quiesce().unwrap());
            let took = start.elapsed();
            assert!(took >= GRACE && took < GRACE * 2, "{took:?}");
        });
        drop(rig);
        assert!(!path.exists(), "the socket is removed with the console");
        let mut got = Vec::new();
        client.read_to_end(&mut got).unwrap();
        let discarded = shared.lock().discarded;
        assert!(discarded > 0);
        assert_eq!(
            got.len() as u64 + discarded,
            data.len() as u64,
            "every byte taken or counted"
        );
        assert!(
            got == data[..got.len()],
            "what the client got is the output's start"
        );
    }

    /// The socket path: an existing file that is not a socket is refused and kept; a stale socket (nobody listening)
    /// is replaced; one another listener answers on is refused; the console removes its socket when it ends.
    #[test]
    fn socket_path_is_checked_and_removed() {
        let dir = Dir::new();
        let irqs = device().irqs.clone();
        let file = dir.path("file");
        std::fs::write(&file, b"keep").unwrap();
        assert!(Console::socket(&file, irqs.clone()).is_err());
        assert_eq!(std::fs::read(&file).unwrap(), b"keep");
        let path = dir.path("c");
        drop(std::os::unix::net::UnixListener::bind(&path).unwrap());
        assert!(path.exists(), "a stale socket");
        let console = Console::socket(&path, irqs.clone()).unwrap();
        assert!(Console::socket(&path, irqs.clone()).is_err(), "in use");
        drop(UnixStream::connect(&path).unwrap());
        drop(console);
        assert!(!path.exists());
    }
}
