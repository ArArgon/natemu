//! virtio-blk over one owned regular file: modern request parsing, a fixed pool of native disk
//! workers that make the file calls outside every lock and copy window, and the block owner,
//! which keeps several requests in flight and batches their copy windows.
//!
//! Durability, as QEMU: a guest FLUSH (or a write-through OUT) is fsync(2), which survives a natemu crash but not
//! a host power loss or macOS crash; the power-off drain ends with F_FULLFSYNC, so a clean shutdown is fully durable.
use super::profile;
use super::*;
use std::{
    fs::{File, OpenOptions, TryLockError},
    os::{
        fd::AsRawFd,
        unix::fs::{FileExt, MetadataExt},
    },
    path::Path,
    sync::mpsc::{self, Receiver, SyncSender},
    thread::JoinHandle,
    time::Instant,
};

/// Feature bits offered (virtio 1.3 §5.2.3).
const F_SIZE_MAX: u64 = 1 << 1;
const F_SEG_MAX: u64 = 1 << 2;
const F_BLK_SIZE: u64 = 1 << 6;
pub(super) const F_FLUSH: u64 = 1 << 9;
pub(super) const FEATURES: u64 = VERSION_1 | F_SIZE_MAX | F_SEG_MAX | F_BLK_SIZE | F_FLUSH;
/// The driver must honour the segment limits below.
pub(super) const REQUIRED: u64 = VERSION_1 | F_SIZE_MAX | F_SEG_MAX;

/// Request types (§5.2.6).
const T_IN: u32 = 0;
const T_OUT: u32 = 1;
const T_FLUSH: u32 = 4;
const T_GET_ID: u32 = 8;
/// Request status values.
const S_OK: u8 = 0;
const S_IOERR: u8 = 1;
const S_UNSUPP: u8 = 2;

const SECTOR: u64 = 512;
/// type, reserved, sector.
const HEADER: usize = 16;
const ID_BYTES: usize = 20;
/// Advertised size_max and seg_max, and their product's bound on one request's data.
const MAX_SEGMENT: usize = 4096;
const MAX_SEGMENTS: usize = 32;
pub(super) const MAX_DATA: usize = 128 * 1024;

/// Disk worker threads, sharing the one file descriptor.
const WORKERS: usize = 4;
/// At most this many requests in flight (and never more than the queue size), holding at most
/// `MAX_BUFFERED` bytes of owned buffers. Admission pauses at either bound.
const MAX_REQUESTS: usize = 32;
const MAX_BUFFERED: usize = 4 << 20;
/// A native call running longer abandons the workers, and the VM fails. The owner checks its workers' calls; a watchdog
/// thread checks the owner's own (inline) call every tenth of the limit.
const CALL_LIMIT: Duration = Duration::from_secs(10);
/// Bounded spins before blocking, after recent activity: the owner's on its bell after it admitted or completed work,
/// a worker's on the job queue after it finished a job. They trade host CPU for a condition-variable wake. Measured
/// (io-benchmarks.md, "Shorter depth-1 path"): 5–10 µs gained nothing measurable at depth 1, depth 16 or four readers;
/// 25 µs caught the next depth-1 kick but cost about 45% more host CPU per I/O, past the 20% bound; so both are off.
/// `NATEMU_IO_SPIN_US=OWNER,WORKER` sets them (calibration on other hosts).
const OWNER_SPIN_US: u64 = 0;
const WORKER_SPIN_US: u64 = 0;
fn spins() -> (Duration, Duration) {
    static SPINS: std::sync::OnceLock<(Duration, Duration)> = std::sync::OnceLock::new();
    *SPINS.get_or_init(|| {
        let us = std::env::var("NATEMU_IO_SPIN_US").ok().and_then(|v| {
            let (o, w) = v.split_once(',')?;
            Some((o.trim().parse().ok()?, w.trim().parse().ok()?))
        });
        let (o, w) = us.unwrap_or((OWNER_SPIN_US, WORKER_SPIN_US));
        (Duration::from_micros(o), Duration::from_micros(w))
    })
}
/// The power-off drain's final full sync, outside every in-flight slot.
const FINAL_SYNC: usize = usize::MAX;
/// How long the owner sleeps with kicks suppressed while requests are on disk before it clears the flag: heads the
/// driver publishes meanwhile wait at most this long (or for the next completion, which reads the ring).
const SUPPRESSED_WAIT: Duration = Duration::from_micros(50);
/// The used ring's flags with and without VIRTQ_USED_F_NO_NOTIFY.
const NO_NOTIFY: [u8; 2] = USED_NO_NOTIFY.to_le_bytes();
const NOTIFY: [u8; 2] = [0; 2];

pub(super) struct Disk {
    #[cfg(feature = "testing")]
    pub control: Mutex<Option<Arc<super::testing::Control>>>,
    /// Unit tests: native calls replaced by a script.
    #[cfg(test)]
    pub script: Mutex<Option<Arc<dyn Calls>>>,
    /// Unit tests: a shorter call limit.
    #[cfg(test)]
    pub call_limit: Mutex<Option<Duration>>,
    /// Taken by the workers when the owner starts.
    file: Mutex<Option<File>>,
    pub capacity: u64,
    /// GET_ID's answer: the file's device and inode, in hex.
    pub id: [u8; ID_BYTES],
}
impl Disk {
    pub fn open(path: &Path) -> Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .map_err(|e| format!("disk {}: {e}", path.display()))?;
        let info = file.metadata().map_err(|e| e.to_string())?;
        let len = info.len();
        if !info.is_file() || len == 0 || len % SECTOR != 0 || len > i64::MAX as u64 {
            return Err("disk must be a nonempty regular file with sector-aligned capacity".into());
        }
        // Advisory exclusion among cooperating launches; users must exclude other writers/truncators.
        match file.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => {
                return Err("disk lock: held by another process".into());
            }
            Err(TryLockError::Error(e)) => return Err(format!("disk lock: {e}")),
        }
        let id = format!(
            "{:08x}{:012x}",
            info.dev() as u32,
            info.ino() & 0xffff_ffff_ffff
        )
        .into_bytes()
        .try_into()
        .expect("8 + 12 hex digits");
        Ok(Self {
            #[cfg(feature = "testing")]
            control: Mutex::new(None),
            #[cfg(test)]
            script: Mutex::new(None),
            #[cfg(test)]
            call_limit: Mutex::new(None),
            file: Mutex::new(Some(file)),
            capacity: len,
            id,
        })
    }
    /// Configuration space: capacity (sectors), size_max, seg_max, geometry (unused), blk_size.
    pub fn config(&self) -> [u8; 24] {
        let mut c = [0; 24];
        c[..8].copy_from_slice(&(self.capacity / SECTOR).to_le_bytes());
        c[8..12].copy_from_slice(&(MAX_SEGMENT as u32).to_le_bytes());
        c[12..16].copy_from_slice(&(MAX_SEGMENTS as u32).to_le_bytes());
        c[20..24].copy_from_slice(&(SECTOR as u32).to_le_bytes());
        c
    }
    /// Start the workers on the file; each finished job rings `bell`.
    pub fn start(&self, bell: Arc<Bell>) -> Result<Pool> {
        let pool = self.spawn(bell)?;
        #[cfg(test)]
        if let Some(limit) = *self.call_limit.lock().unwrap() {
            board(&pool.board).limit = limit;
        }
        Ok(pool)
    }
    fn spawn(&self, bell: Arc<Bell>) -> Result<Pool> {
        let file = self
            .file
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
            .ok_or("disk workers already started")?;
        #[cfg(feature = "testing")]
        if let Some(control) = self.control() {
            return Pool::spawn(super::testing::Controlled::new(Native(file), control), bell);
        }
        #[cfg(test)]
        if let Some(script) = self.script.lock().unwrap_or_else(|e| e.into_inner()).take() {
            return Pool::spawn(Scripted(script), bell);
        }
        Pool::spawn(Native(file), bell)
    }
    #[cfg(feature = "testing")]
    fn control(&self) -> Option<Arc<super::testing::Control>> {
        self.control
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }
}

/// One native operation for a worker.
pub(super) enum Job {
    /// Read this many bytes at this file offset.
    Read(u64, usize),
    /// Write these bytes at this offset; `true` also syncs (no FLUSH negotiated: write-through).
    Write(u64, Vec<u8>, bool),
    /// Sync the file; `true`: a full sync (the power-off drain's), else a plain one (a guest FLUSH's).
    Flush(bool),
}
/// A job's result: the bytes read (empty for a write or on error) and the first error.
pub(super) struct Done {
    pub data: Vec<u8>,
    pub error: Option<String>,
}
/// A job tagged with its in-flight slot.
type Ticket = (usize, Job);

/// What the workers share with the owner: the calls in progress (for the call limit) and the
/// finished results, in finishing order.
#[derive(Default)]
struct Board {
    /// Slot, start, call kind and file offset.
    running: Vec<(usize, Instant, &'static str, u64)>,
    finished: Vec<(usize, Done)>,
    /// The owner gave up on the pool: queued jobs are dropped, never run.
    abandoned: bool,
    /// A call finished past the call limit (between two looks at `running`).
    overran: bool,
    /// The call limit; zero: [`CALL_LIMIT`].
    limit: Duration,
}
impl Board {
    fn limit(&self) -> Duration {
        if self.limit.is_zero() {
            CALL_LIMIT
        } else {
            self.limit
        }
    }
}
fn board(board: &Mutex<Board>) -> MutexGuard<'_, Board> {
    board.lock().unwrap_or_else(|e| e.into_inner())
}

/// A fixed pool of disk workers taking owned jobs in submission order and finishing them in any
/// order. A worker owns only the shared file, its jobs' buffers, the board and the bell: no VM
/// state. Abandonment detaches the workers until their syscalls retire or the per-VM CLI process
/// exits; embedders must treat that error as worker-process fatal.
pub(super) struct Pool {
    jobs: Option<SyncSender<Ticket>>,
    board: Arc<Mutex<Board>>,
    workers: Vec<JoinHandle<()>>,
    /// The owner's inline calls.
    calls: Arc<dyn Calls>,
}
impl Pool {
    fn spawn(calls: impl Calls + 'static, bell: Arc<Bell>) -> Result<Self> {
        let calls: Arc<dyn Calls> = Arc::new(calls);
        // Every in-flight request and the final sync can be queued at once.
        let (jobs, queue) = mpsc::sync_channel::<Ticket>(MAX_REQUESTS + 1);
        let queue = Arc::new(Mutex::new(queue));
        let shared = Arc::new(Mutex::new(Board::default()));
        let mut workers = Vec::with_capacity(WORKERS);
        for _ in 0..WORKERS {
            let (calls, queue, shared, bell) =
                (calls.clone(), queue.clone(), shared.clone(), bell.clone());
            let worker = std::thread::Builder::new()
                .name("natemu-disk".into())
                .spawn(move || work(&*calls, &queue, &shared, &bell))
                .map_err(|e| format!("disk worker: {e}"))?;
            workers.push(worker);
        }
        Ok(Self {
            jobs: Some(jobs),
            board: shared,
            workers,
            calls,
        })
    }
    pub fn submit(&self, slot: usize, job: Job) -> Result<()> {
        self.jobs
            .as_ref()
            .ok_or("closed disk workers")?
            .try_send((slot, job))
            .map_err(|e| format!("disk submission: {e}"))
    }
    /// The results finished since the last call, in finishing order.
    pub fn take(&self) -> Vec<(usize, Done)> {
        std::mem::take(&mut board(&self.board).finished)
    }
    /// Run one job on the calling thread (the owner's lone request), as a worker would, without ringing the bell.
    pub fn inline(&self, slot: usize, job: Job) {
        run(&*self.calls, &self.board, slot, job);
    }
    /// Some native call has run longer than the call limit.
    pub fn stalled(&self) -> bool {
        stalled(&self.board)
    }
    /// Give up on every outstanding call for good: queued jobs never run, running calls keep
    /// only owned storage until they retire. Returns the VM-fatal error.
    pub fn abandon(&mut self) -> String {
        board(&self.board).abandoned = true;
        self.jobs.take();
        self.workers.clear();
        "disk operation abandoned: VM worker must terminate; file may have partial effects".into()
    }
    /// Close and join the workers. Called only with no outstanding job.
    pub fn finish(mut self) -> Result<()> {
        self.jobs.take();
        for worker in self.workers.drain(..) {
            worker.join().map_err(|_| "disk worker panicked")?;
        }
        Ok(())
    }
}
/// Any exit other than `finish` (an owner error included) abandons the pool: no queued job runs
/// after its owner is gone.
impl Drop for Pool {
    fn drop(&mut self) {
        if !self.workers.is_empty() {
            self.abandon();
        }
    }
}
fn stalled(board: &Mutex<Board>) -> bool {
    stalled_call(board).is_some()
}
/// The first call past the call limit, described for the VM-fatal message.
fn stalled_call(board: &Mutex<Board>) -> Option<String> {
    let board = self::board(board);
    let limit = board.limit();
    match board.running.iter().find(|c| c.1.elapsed() > limit) {
        Some((_, start, kind, offset)) => Some(format!(
            "{kind} at file offset {offset:#x}, running for {:.1?}",
            start.elapsed()
        )),
        None => board
            .overran
            .then(|| format!("a call that finished past the {limit:?} limit")),
    }
}
/// One worker: run jobs until the pool closes or is abandoned. After a job it spins briefly for the next.
fn work(calls: &dyn Calls, queue: &Mutex<Receiver<Ticket>>, shared: &Mutex<Board>, bell: &Bell) {
    let mut spin = Duration::ZERO;
    loop {
        let ticket = receive(&queue.lock().unwrap_or_else(|e| e.into_inner()), spin);
        let Ok((slot, job)) = ticket else {
            profile::add(profile::WORKER_CPU_NS, crate::perf::thread_cpu());
            return;
        };
        if !run(calls, shared, slot, job) {
            return;
        }
        bell.ring();
        spin = spins().1;
    }
}
/// The next job, spinning up to `spin` before blocking.
fn receive(
    queue: &Receiver<Ticket>,
    spin: Duration,
) -> std::result::Result<Ticket, mpsc::RecvError> {
    if !spin.is_zero() {
        let until = Instant::now() + spin;
        loop {
            match queue.try_recv() {
                Ok(ticket) => return Ok(ticket),
                Err(mpsc::TryRecvError::Disconnected) => return Err(mpsc::RecvError),
                Err(mpsc::TryRecvError::Empty) if Instant::now() >= until => break,
                Err(mpsc::TryRecvError::Empty) => std::hint::spin_loop(),
            }
        }
    }
    queue.recv()
}
/// Run one job and post its result, unless the pool was abandoned (false: nothing ran).
fn run(calls: &dyn Calls, shared: &Mutex<Board>, slot: usize, job: Job) -> bool {
    let start = {
        let mut board = board(shared);
        if board.abandoned {
            return false;
        }
        let start = Instant::now();
        let (kind, offset) = match &job {
            Job::Read(offset, _) => ("read", *offset),
            Job::Write(offset, ..) => ("write", *offset),
            Job::Flush(true) => ("full sync", 0),
            Job::Flush(false) => ("sync", 0),
        };
        board.running.push((slot, start, kind, offset));
        start
    };
    let began = profile::taken_by_worker(slot);
    let done = execute(calls, job);
    profile::called(slot, began);
    let mut board = board(shared);
    board.overran |= start.elapsed() > board.limit();
    board.running.retain(|c| c.0 != slot);
    board.finished.push((slot, done));
    true
}

/// The workers' native file calls; replaced by scripted calls in tests and test schedules.
/// Calls run concurrently on one file.
pub(super) trait Calls: Send + Sync {
    fn read(&self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize>;
    fn write(&self, bytes: &[u8], offset: u64) -> std::io::Result<usize>;
    /// `full`: to stable storage (F_FULLFSYNC); otherwise to the host OS (fsync).
    fn sync(&self, full: bool) -> std::io::Result<()>;
}
/// One positioned read or write per call (pread/pwrite: a short count is returned as is, EINTR
/// is an error). A full sync is F_FULLFSYNC (`File::sync_all` on Apple platforms); a plain one is
/// fsync(2), as QEMU's guest flush: it survives a natemu crash but not a host power loss or macOS
/// crash (std's `sync_data` is F_FULLFSYNC on Apple platforms too, so fsync is called directly).
pub(super) struct Native(File);
impl Calls for Native {
    fn read(&self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize> {
        self.0.read_at(bytes, offset)
    }
    fn write(&self, bytes: &[u8], offset: u64) -> std::io::Result<usize> {
        self.0.write_at(bytes, offset)
    }
    fn sync(&self, full: bool) -> std::io::Result<()> {
        if full {
            return self.0.sync_all();
        }
        unsafe extern "C" {
            fn fsync(fd: i32) -> i32;
        }
        // As `sync_all`, retry on EINTR.
        loop {
            if unsafe { fsync(self.0.as_raw_fd()) } == 0 {
                return Ok(());
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }
}

/// Unit tests' scripted native calls.
#[cfg(test)]
struct Scripted(Arc<dyn Calls>);
#[cfg(test)]
impl Calls for Scripted {
    fn read(&self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize> {
        self.0.read(bytes, offset)
    }
    fn write(&self, bytes: &[u8], offset: u64) -> std::io::Result<usize> {
        self.0.write(bytes, offset)
    }
    fn sync(&self, full: bool) -> std::io::Result<()> {
        self.0.sync(full)
    }
}

/// Run one job to completion or its first error. A short transfer continues with only the
/// unconfirmed suffix; an error (EINTR included) or zero progress is never retried, since its
/// effect is uncertain. A stable write syncs after the last byte.
fn execute(native: &(impl Calls + ?Sized), job: Job) -> Done {
    let (offset, mut data, write, stable) = match job {
        Job::Read(offset, len) => (offset, vec![0; len], false, false),
        Job::Write(offset, bytes, stable) => (offset, bytes, true, stable),
        Job::Flush(full) => {
            return Done {
                data: Vec::new(),
                error: native.sync(full).err().map(|e| e.to_string()),
            };
        }
    };
    let mut done = 0;
    let mut error = None;
    while done < data.len() {
        let at = offset + done as u64;
        let result = if write {
            native.write(&data[done..], at)
        } else {
            native.read(&mut data[done..], at)
        };
        match result {
            Ok(n) if n > 0 && n <= data.len() - done => done += n,
            Ok(_) => {
                error = Some("native disk call made zero or invalid progress".into());
                break;
            }
            Err(e) => {
                error = Some(e.to_string());
                break;
            }
        }
    }
    if error.is_none() && write && stable {
        error = native.sync(false).err().map(|e| e.to_string());
    }
    if write || error.is_some() {
        data.clear();
    }
    Done { data, error }
}

/// A guest-physical byte range.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Extent {
    pub gpa: u64,
    pub len: usize,
}
/// The `len` bytes at `offset` of the byte stream `extents` describe. Descriptor boundaries do
/// not define header/status boundaries, so requests are sliced from the logical streams.
fn slice(extents: &[Extent], mut offset: usize, mut len: usize) -> Vec<Extent> {
    let mut result = Vec::new();
    for e in extents {
        let skip = offset.min(e.len);
        offset -= skip;
        let take = (e.len - skip).min(len);
        if take != 0 {
            result.push(Extent {
                gpa: e.gpa + skip as u64,
                len: take,
            });
            len -= take;
        }
    }
    assert_eq!(len, 0);
    result
}
fn length(e: &[Extent]) -> usize {
    e.iter().map(|e| e.len).sum()
}
/// Within the advertised seg_max, size_max and total-data bounds.
fn data_valid(e: &[Extent]) -> bool {
    e.len() <= MAX_SEGMENTS && e.iter().all(|e| e.len <= MAX_SEGMENT) && length(e) <= MAX_DATA
}

/// A chain split into its device-readable stream (header, then OUT data) and device-writable
/// stream (IN data, then the status byte at its very end).
pub(super) struct Streams {
    pub readable: Vec<Extent>,
    pub writable: Vec<Extent>,
    /// The status byte's address: the writable stream's last byte.
    pub status: u64,
}
impl Streams {
    pub fn new(chain: &[Descriptor]) -> Result<Self> {
        let (mut readable, mut writable) = (Vec::new(), Vec::new());
        let mut write = false;
        for d in chain {
            if !d.is_write_only() && write {
                return Err("block readable descriptor follows writable descriptor".into());
            }
            write |= d.is_write_only();
            if d.len() == 0 {
                continue;
            }
            let e = Extent {
                gpa: d.addr().0,
                len: d.len() as usize,
            };
            if write {
                writable.push(e);
            } else {
                readable.push(e);
            }
        }
        let last = writable
            .last()
            .ok_or("block chain has no status destination")?;
        let status = last.gpa + last.len as u64 - 1;
        Ok(Self {
            readable,
            writable,
            status,
        })
    }
    pub fn readable_len(&self) -> usize {
        length(&self.readable)
    }
    /// The readable stream's length when it is gathered; zero when it is longer than any valid
    /// request (which is then answered IOERR unread).
    pub fn gathered_len(&self) -> usize {
        let n = self.readable_len();
        if n <= MAX_DATA + HEADER { n } else { 0 }
    }
    /// The owned bytes this request may hold: its gathered stream and its IN data.
    pub fn reservation(&self) -> usize {
        self.gathered_len() + self.writable_data_len().min(MAX_DATA)
    }
    /// The writable stream's bytes before the status byte.
    fn writable_data_len(&self) -> usize {
        length(&self.writable) - 1
    }
    /// Decode the readable stream's bytes (header, then OUT data) against the disk's capacity
    /// and the negotiated features. A malformed or out-of-range request is an IOERR status.
    pub fn decode(&self, mut bytes: Vec<u8>, capacity: u64, features: u64) -> Command {
        let bad = || Command::Status(S_IOERR);
        if bytes.len() < HEADER {
            return bad();
        }
        let kind = u32::from_le_bytes(bytes[..4].try_into().unwrap());
        let sector = u64::from_le_bytes(bytes[8..16].try_into().unwrap());
        let header_only = bytes.len() == HEADER;
        match kind {
            T_IN | T_OUT => {
                let data = if kind == T_IN {
                    slice(&self.writable, 0, self.writable_data_len())
                } else {
                    slice(&self.readable, HEADER, bytes.len() - HEADER)
                };
                let n = length(&data);
                let Some(offset) = sector.checked_mul(SECTOR) else {
                    return bad();
                };
                let past_end = offset
                    .checked_add(n as u64)
                    .is_none_or(|end| end > capacity);
                if !data_valid(&data)
                    || n as u64 % SECTOR != 0
                    || past_end
                    || kind == T_IN && !header_only
                {
                    return bad();
                }
                if kind == T_IN {
                    Command::Read(offset, n)
                } else {
                    // Without FLUSH negotiated the driver expects write-through.
                    let stable = features & F_FLUSH == 0;
                    bytes.drain(..HEADER);
                    Command::Write(offset, bytes, stable)
                }
            }
            T_FLUSH if features & F_FLUSH != 0 => {
                if header_only {
                    Command::Flush
                } else {
                    bad()
                }
            }
            T_GET_ID => {
                if header_only && self.writable_data_len() >= ID_BYTES {
                    Command::Id
                } else {
                    bad()
                }
            }
            _ => Command::Status(S_UNSUPP),
        }
    }
    /// Writes are ordered: payload, tail status, then transport completion. Only the contiguous
    /// initialized prefix counts, even when a valid GET_ID buffer has a gap before status.
    /// Returns the used length.
    pub fn completion<'a>(
        &self,
        data: &'a [u8],
        status: &'a [u8; 1],
        writes: &mut Vec<Transfer<'a>>,
    ) -> u32 {
        let mut offset = 0;
        for e in slice(&self.writable, 0, data.len()) {
            writes.push(Transfer::Write {
                gpa: e.gpa,
                bytes: &data[offset..offset + e.len],
            });
            offset += e.len;
        }
        writes.push(Transfer::Write {
            gpa: self.status,
            bytes: status,
        });
        let status_follows_data = data.len() == self.writable_data_len();
        (data.len() + usize::from(status_follows_data)) as u32
    }
}
/// A decoded request.
pub(super) enum Command {
    Read(u64, usize),
    Write(u64, Vec<u8>, bool),
    Flush,
    Id,
    /// Answer with this status and no data.
    Status(u8),
}

/// The most requests in flight on a queue of `size` entries.
fn request_limit(size: u16) -> usize {
    MAX_REQUESTS.min(size.into())
}
/// Another request reserving `more` bytes fits the bounds while `count` requests holding
/// `reserved` bytes are in flight. One request always fits the byte bound.
fn within_bounds(count: usize, reserved: usize, more: usize, size: u16) -> bool {
    count < request_limit(size) && (count == 0 || reserved + more <= MAX_BUFFERED)
}

/// The checked streams of a chain whose every buffer lies in guest RAM.
fn chain_streams(ram: &GuestRam, descriptors: &[Descriptor]) -> Result<Streams> {
    if !descriptors_in(ram, descriptors) {
        return Err("descriptor outside RAM".into());
    }
    Streams::new(descriptors)
}

/// One admitted request: its incarnation, head and streams, the owned bytes it holds, and its
/// reply once answered.
struct Admitted {
    id: Identity,
    head: u16,
    streams: Streams,
    reserved: usize,
    /// An IN request: a failed one reports no bytes.
    inbound: bool,
    reply: Option<Reply>,
}
/// A request's IN data (or GET_ID answer) and status.
struct Reply {
    data: Vec<u8>,
    status: [u8; 1],
}
impl Reply {
    fn without_data(status: u8) -> Self {
        Self {
            data: Vec::new(),
            status: [status],
        }
    }
}
impl Admitted {
    /// The reply's payload and status writes, and the used length.
    fn completion(&self) -> (Vec<Transfer<'_>>, u32) {
        let reply = self.reply.as_ref().expect("completing an answered request");
        let mut writes = Vec::with_capacity(self.streams.writable.len() + 1);
        let len = self
            .streams
            .completion(&reply.data, &reply.status, &mut writes);
        let failed_read = self.inbound && reply.status[0] != S_OK;
        (writes, if failed_read { 0 } else { len })
    }
}

/// The block owner's in-flight requests by slot (a disk job's ticket), and the order in which
/// they were answered.
#[derive(Default)]
struct Flight {
    slots: Vec<Option<Admitted>>,
    answered: VecDeque<usize>,
    reserved: usize,
}
impl Flight {
    fn len(&self) -> usize {
        self.slots.iter().flatten().count()
    }
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
    /// Some request still waits for its disk call.
    fn on_disk(&self) -> bool {
        self.slots.iter().flatten().any(|r| r.reply.is_none())
    }
    /// Hold `request` in a free slot; an already answered one joins the answered order.
    fn insert(&mut self, request: Admitted) -> usize {
        self.reserved += request.reserved;
        let answered = request.reply.is_some();
        let slot = match self.slots.iter().position(Option::is_none) {
            Some(free) => free,
            None => {
                self.slots.push(None);
                self.slots.len() - 1
            }
        };
        self.slots[slot] = Some(request);
        if answered {
            self.answered.push_back(slot);
        }
        slot
    }
    /// Record a disk call's result as the reply of the request in `slot`.
    fn answer(&mut self, slot: usize, done: Done) {
        let request = self.slots[slot]
            .as_mut()
            .expect("a finished job's request is in flight");
        let status = match done.error {
            Some(e) => {
                eprintln!("virtio-block: {e}");
                S_IOERR
            }
            None => S_OK,
        };
        request.reply = Some(Reply {
            data: done.data,
            status: [status],
        });
        self.answered.push_back(slot);
    }
    /// Take every answered request, in answering order.
    fn take_answered(&mut self) -> Vec<Admitted> {
        let slots: Vec<usize> = self.answered.drain(..).collect();
        slots
            .into_iter()
            .map(|slot| {
                let request = self.slots[slot].take().expect("answered request");
                self.reserved -= request.reserved;
                request
            })
            .collect()
    }
}

/// A gathered request's disk call, or its immediate reply.
enum Start {
    Call(Job),
    Answer(Reply),
}

/// Decode a gathered request into its disk call or immediate answer. `bytes` is its gathered
/// readable stream; a stream too long to gather (empty `bytes`) is answered IOERR.
fn start(streams: &Streams, bytes: Vec<u8>, disk: &Disk, features: u64) -> Start {
    let command = if streams.gathered_len() == streams.readable_len() {
        streams.decode(bytes, disk.capacity, features)
    } else {
        Command::Status(S_IOERR)
    };
    match command {
        Command::Read(offset, len) => Start::Call(Job::Read(offset, len)),
        Command::Write(offset, bytes, stable) => Start::Call(Job::Write(offset, bytes, stable)),
        Command::Flush => Start::Call(Job::Flush(false)),
        Command::Id => Start::Answer(Reply {
            data: disk.id.to_vec(),
            status: [S_OK],
        }),
        Command::Status(status) => Start::Answer(Reply::without_data(status)),
    }
}

/// Clear `q`'s NO_NOTIFY, then (after the full barrier a copy puts between a write and a later read) read the
/// available ring's flags and index into `avail`.
fn clear_spans(q: Queue, avail: &mut [u8; 4]) -> [Transfer<'_>; 2] {
    [
        Transfer::Write {
            gpa: q.used,
            bytes: &NOTIFY,
        },
        Transfer::Read {
            gpa: q.avail,
            bytes: avail,
        },
    ]
}

impl Virtio {
    /// The block owner: admit every published request the bounds allow, gather their headers and
    /// OUT payloads, hand their disk calls to the workers, and complete answered requests in
    /// batches. Finished jobs ring its bell. It blocks on one job only when it makes a lone
    /// request's call itself (`start_requests`); a watchdog fails the VM if that call passes the
    /// call limit, as the owner does for a worker's.
    pub(super) fn block_owner(
        &self,
        guest: Guest<'_>,
        disk: &Disk,
        cancel: &AtomicBool,
    ) -> Result<()> {
        let pool = disk.start(self.wake.clone())?;
        let board = pool.board.clone();
        let watch = self::board(&board).limit() / 10;
        let (done, signal) = (Mutex::new(false), Condvar::new());
        std::thread::scope(|scope| {
            scope.spawn(|| {
                let mut done = done.lock().unwrap_or_else(|e| e.into_inner());
                while !*done {
                    if let Some(call) = stalled_call(&board) {
                        // The owner abandons the pool once its call returns (or at once, for a worker's). Say why now:
                        // an owner's own call cannot be detached, so the run ends only when that syscall returns.
                        eprintln!(
                            "virtio-block: VM fatal: native disk call past the call limit ({call}); the VM is canceled, \
                             and an owner's inline call keeps the process until the syscall returns"
                        );
                        crate::owners::cancel_owners(cancel);
                        self.wake.ring();
                        return;
                    }
                    done = signal
                        .wait_timeout(done, watch)
                        .unwrap_or_else(|e| e.into_inner())
                        .0;
                }
            });
            // Even a panicking owner ends the watchdog (the scope joins it before the panic goes on).
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                self.serve_block(guest, disk, cancel, pool)
            }));
            *done.lock().unwrap_or_else(|e| e.into_inner()) = true;
            signal.notify_one();
            result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
        })
    }
    fn serve_block(
        &self,
        guest: Guest<'_>,
        disk: &Disk,
        cancel: &AtomicBool,
        mut pool: Pool,
    ) -> Result<()> {
        let mut snapshot = Snapshot::new(self.profile.queue_max())?;
        let mut flight = Flight::default();
        // The last pass did work: the next wait spins first.
        let mut active = false;
        // Kick suppression (`State::suppressed`): an admission's gather sets VIRTQ_USED_F_NO_NOTIFY; the owner
        // clears it before it idles (in the completion that empties the flight, or here), before a power-off drain,
        // and before a reset of its incarnation retires. Each clear is followed by a full barrier and a read of the
        // available index (Dekker with the driver's index store, barrier and flags read), and new heads are admitted
        // instead of sleeping, so no kick is lost.
        loop {
            let (stopped, quiescing, retiring) = {
                let s = self.lock();
                let retiring = s.suppressed.filter(|&id| !s.current(id));
                (s.stopped || cancel.load(SeqCst), s.quiescing, retiring)
            };
            if stopped {
                return if flight.on_disk() {
                    Err(pool.abandon())
                } else {
                    pool.finish()
                };
            }
            // A reset closing the suppressed incarnation waits for this clear.
            if let Some(id) = retiring {
                self.unsuppress(guest, id)?;
                continue;
            }
            for (slot, done) in pool.take() {
                profile::result_taken(slot);
                flight.answer(slot, done);
            }
            if pool.stalled() {
                return Err(pool.abandon());
            }
            if self.complete_answered(guest, disk, &mut flight, &mut snapshot)? {
                active = true;
                continue;
            }
            let suppressed = self.lock().suppressed;
            if quiescing && flight.is_empty() {
                if let Some(id) = suppressed {
                    self.unsuppress(guest, id)?;
                }
                return self.drain_disk(pool, cancel);
            }
            if self.admit_block(guest, disk, &pool, &mut flight, &mut snapshot)? {
                active = true;
                continue;
            }
            let spin = if std::mem::take(&mut active) {
                spins().0
            } else {
                Duration::ZERO
            };
            if let Some(id) = suppressed {
                // Requests on disk ring the bell as they finish; their completion reads the ring.
                if !flight.is_empty() && self.wake.wait_spinning(SUPPRESSED_WAIT, spin) {
                    continue;
                }
                if self.unsuppress(guest, id)? {
                    continue;
                }
            }
            let waited = profile::start();
            self.wake.wait_spinning(Duration::from_millis(10), spin);
            profile::count(profile::WAITS);
            profile::since(profile::WAIT_NS, waited);
        }
    }
    /// Power-off drain once nothing is in flight: a final full sync on a worker (it covers every
    /// completed write), the workers' join, then a quiet device.
    pub(super) fn drain_disk(&self, mut pool: Pool, cancel: &AtomicBool) -> Result<()> {
        pool.submit(FINAL_SYNC, Job::Flush(true))?;
        let done = loop {
            if let Some((_, done)) = pool.take().pop() {
                break done;
            }
            if cancel.load(SeqCst) || self.lock().stopped || pool.stalled() {
                return Err(pool.abandon());
            }
            self.wake.wait(Duration::from_millis(10));
        };
        if let Some(error) = done.error {
            return Err(format!("disk shutdown full-sync: {error}"));
        }
        pool.finish()?;
        #[cfg(feature = "testing")]
        self.observe("lane-joined");
        self.drain()
    }

    /// Clear the used ring's NO_NOTIFY, then read the available index: true if heads wait (the queue is notified).
    /// Also while a reset closes `id`: the ring stays the device's until that reset retires, which waits for this.
    fn unsuppress(&self, guest: Guest<'_>, id: Identity) -> Result<bool> {
        let q = self.lock().queues[id.queue];
        let mut avail = [0; 4];
        let mut spans = clear_spans(q, &mut avail);
        let unretired = |s: &State, id: Identity| !s.stopped && s.identity(id.queue) == id;
        if !self.copy_while(guest, id, &mut spans, None, unretired)? {
            return Ok(false);
        }
        Ok(self.unsuppressed(id, &avail))
    }
    /// After a clear of NO_NOTIFY and a read of the available ring's flags and index into `avail`: retire the
    /// suppression (and a reset waiting for it), and notify the queue if the driver published heads not yet
    /// admitted. True if it did.
    fn unsuppressed(&self, id: Identity, avail: &[u8; 4]) -> bool {
        let mut s = self.lock();
        s.suppressed = None;
        let more = u16::from_le_bytes([avail[2], avail[3]]) != s.queues[id.queue].next_avail;
        let rearm = more && s.current(id) && s.accepting(id.queue);
        if rearm {
            s.notified[id.queue] = true;
            self.busy(&mut s);
        }
        self.retire_reset(&mut s);
        self.settle(&mut s);
        rearm
    }
    /// Admit every published request the bounds allow from one metadata snapshot (captured now
    /// unless the last completion window refreshed it), gather their headers and OUT payloads,
    /// and start their disk calls; the first gather window also suppresses kicks. True if any
    /// request was admitted.
    fn admit_block(
        &self,
        guest: Guest<'_>,
        disk: &Disk,
        pool: &Pool,
        flight: &mut Flight,
        snapshot: &mut Snapshot,
    ) -> Result<bool> {
        let (q, id, notified, flag) = {
            let mut s = self.lock();
            self.take_kicks(&mut s);
            let (q, id, notified) = (s.queues[0], s.identity(0), s.notified[0]);
            let flag = s.suppressed.is_none().then_some(q.used);
            let work = notified || snapshot.reusable(q, id);
            let open = s.admission_open() && s.accepting(0) && !s.stopped;
            if !work || !open || flight.len() >= request_limit(q.size) {
                return Ok(false);
            }
            s.notified[0] = false;
            (q, id, notified, flag)
        };
        let began = profile::start();
        if notified {
            profile::consumed();
        }
        let ram = &guest.backing.ram;
        let mut invalid = self.check_rings(q, ram).err();
        // A notification may announce heads published after a reused snapshot. With requests in
        // flight, their next completion window refreshes the snapshot and re-arms the queue, so a
        // second capture would only cost a window; with none in flight, capture once more here.
        let mut recapture = notified && snapshot.reusable(q, id) && flight.len() == 0;
        if invalid.is_none()
            && !snapshot.reusable(q, id)
            && !self.capture(guest, snapshot, q, id)?
        {
            self.settle(&mut self.lock());
            return Ok(false);
        }
        let mut admitted = Vec::new();
        let mut next = q;
        let (mut count, mut reserved) = (flight.len(), flight.reserved);
        while invalid.is_none() && count < request_limit(q.size) {
            let (head, descriptors) = match snapshot.chain(next) {
                Ok(Some(chain)) => chain,
                Ok(None) if recapture => {
                    recapture = false;
                    if !self.capture(guest, snapshot, next, id)? {
                        break;
                    }
                    continue;
                }
                Ok(None) => break,
                Err(e) => {
                    invalid = Some(e);
                    break;
                }
            };
            let streams = match chain_streams(ram, &descriptors) {
                Ok(streams) => streams,
                Err(e) => {
                    invalid = Some(e);
                    break;
                }
            };
            let bytes = streams.reservation();
            if !within_bounds(count, reserved, bytes, q.size) {
                break;
            }
            admitted.push(Admitted {
                id,
                head,
                streams,
                reserved: bytes,
                // Set from the decoded command when the request starts.
                inbound: false,
                reply: None,
            });
            next.next_avail = next.next_avail.wrapping_add(1);
            (count, reserved) = (count + 1, reserved + bytes);
        }
        if !self.commit_admission(id, next, admitted.len(), invalid, snapshot) {
            return Ok(false);
        }
        profile::count(profile::ADMISSIONS);
        profile::add(profile::ADMITTED, admitted.len() as u64);
        self.start_requests(guest, disk, pool, flight, id, admitted, flag)?;
        profile::since(profile::ADMIT_NS, began);
        Ok(true)
    }
    /// Make admission visible under the lock: consume the admitted heads and count them in
    /// flight, unless a reset or power-off overtook them (then none is admitted). A malformed
    /// head after them fails the device; they still complete. True if any was admitted.
    fn commit_admission(
        &self,
        id: Identity,
        next: Queue,
        count: usize,
        invalid: Option<String>,
        snapshot: &mut Snapshot,
    ) -> bool {
        let mut s = self.lock();
        let admitted = count > 0 && s.current(id) && s.accepting(id.queue);
        if admitted {
            s.queues[id.queue].next_avail = next.next_avail;
            s.in_flight.extend(std::iter::repeat_n(id, count));
            self.busy(&mut s);
        }
        if let Some(reason) = invalid {
            snapshot.discard();
            eprintln!("virtio: {reason}");
            if s.current(id) {
                self.fail(&mut s);
            }
        }
        self.settle(&mut s);
        admitted
    }
    /// Gather the admitted requests' readable streams (header, then OUT data) in as few windows
    /// as the window bounds allow, decode them and start their disk calls; a request answered
    /// without one is answered at once. A request whose gather window a reset overtook (and
    /// every later one) retires unanswered. With `flag` (the used ring), the first window also
    /// sets NO_NOTIFY there (recorded before the requests can retire, so a reset waits for its clear).
    #[allow(clippy::too_many_arguments)]
    fn start_requests(
        &self,
        guest: Guest<'_>,
        disk: &Disk,
        pool: &Pool,
        flight: &mut Flight,
        id: Identity,
        admitted: Vec<Admitted>,
        flag: Option<u64>,
    ) -> Result<()> {
        let mut readable: Vec<Vec<u8>> = admitted
            .iter()
            .map(|r| vec![0; r.streams.gathered_len()])
            .collect();
        let (gathered, flagged) = self.gather_streams(guest, id, &admitted, &mut readable, flag)?;
        let features = self.lock().features;
        // Each gathered buffer moves into its decoded command (an OUT job keeps its payload).
        let starts: Vec<Start> = admitted[..gathered]
            .iter()
            .zip(readable)
            .map(|(request, bytes)| start(&request.streams, bytes, disk, features))
            .collect();
        let mut s = self.lock();
        if flagged {
            s.suppressed = Some(id);
        }
        // Submission and reset admission are ordered, but no native call holds this lock.
        let live = s.current(id);
        let mut jobs = Vec::new();
        let mut requests = admitted.into_iter();
        for (start, mut request) in starts.into_iter().zip(requests.by_ref()) {
            if !live {
                s.retire(id);
                continue;
            }
            let job = match start {
                Start::Call(job) => {
                    request.inbound = matches!(job, Job::Read(..));
                    Some(job)
                }
                Start::Answer(reply) => {
                    request.reply = Some(reply);
                    None
                }
            };
            let slot = flight.insert(request);
            if let Some(job) = job {
                profile::submitted(slot);
                jobs.push((slot, job));
            }
        }
        // A lone request in flight with a short call (no sync) makes it here, outside the lock: no
        // hand-off to a worker and back. Kicks stay suppressed meanwhile; the completion re-reads
        // the ring. Like a worker's call, it runs even if a reset arrives before it starts.
        let lone =
            flight.len() == 1 && matches!(jobs[..], [(_, Job::Read(..) | Job::Write(_, _, false))]);
        if !lone {
            for (slot, job) in jobs.drain(..) {
                pool.submit(slot, job)?;
            }
        }
        for request in requests {
            s.retire(request.id);
        }
        self.retire_reset(&mut s);
        self.settle(&mut s);
        drop(s);
        if let Some((slot, job)) = jobs.pop() {
            profile::count(profile::INLINE);
            pool.inline(slot, job);
        }
        Ok(())
    }
    /// Read each request's gathered stream into `readable` in packed windows, the first one
    /// also writing NO_NOTIFY to `flag` (the used ring's flags). Returns how many leading
    /// requests were gathered (all, unless a reset overtook a window) and whether the flag was.
    fn gather_streams(
        &self,
        guest: Guest<'_>,
        id: Identity,
        admitted: &[Admitted],
        readable: &mut [Vec<u8>],
        mut flag: Option<u64>,
    ) -> Result<(usize, bool)> {
        let reads: Vec<Vec<Transfer<'_>>> = admitted
            .iter()
            .zip(readable.iter_mut())
            .map(|(request, bytes)| {
                let extents = slice(&request.streams.readable, 0, bytes.len());
                gather(extents.iter().map(|e| (e.gpa, e.len)), bytes)
            })
            .collect();
        let costs: Vec<Cost> = reads.iter().map(|r| Cost::of(r)).collect();
        let mut reads = reads.into_iter();
        let (mut gathered, mut flagged) = (0, false);
        // Every window leaves room for the flag write.
        let flag_cost = Cost { spans: 1, bytes: 2 };
        for window in pack(&costs, flag_cost) {
            let mut spans: Vec<Transfer<'_>> =
                reads.by_ref().take(window.len()).flatten().collect();
            let flagging = flag.take();
            if let Some(gpa) = flagging {
                spans.push(Transfer::Write {
                    gpa,
                    bytes: &NO_NOTIFY,
                });
            }
            if !spans.is_empty() && !self.copy(guest, id, &mut spans, None)? {
                break;
            }
            flagged |= flagging.is_some();
            gathered = window.end;
        }
        Ok((gathered, flagged))
    }
    /// Complete every answered request, in answering order, in as few windows as the window
    /// bounds allow. Each window publishes whole requests: their data and status, their used
    /// elements in the next used slots, one used index, then reads the available ring's flags
    /// and index; the last also reads the queue's metadata as the next admission's snapshot when
    /// it fits. True if any request was answered.
    fn complete_answered(
        &self,
        guest: Guest<'_>,
        disk: &Disk,
        flight: &mut Flight,
        snapshot: &mut Snapshot,
    ) -> Result<bool> {
        if flight.answered.is_empty() {
            return Ok(false);
        }
        #[cfg(feature = "testing")]
        if disk
            .control()
            .is_some_and(|c| !c.completion_allowed(flight.answered.len()))
        {
            return Ok(false);
        }
        #[cfg(not(feature = "testing"))]
        let _ = disk;
        let began = profile::start();
        let batch = flight.take_answered();
        profile::count(profile::COMPLETIONS);
        profile::add(profile::COMPLETED, batch.len() as u64);
        let id = batch[0].id;
        debug_assert!(
            batch.iter().all(|r| r.id == id),
            "one incarnation in flight"
        );
        let completions: Vec<_> = batch.iter().map(Admitted::completion).collect();
        let element = Cost { spans: 1, bytes: 8 };
        let costs: Vec<Cost> = completions
            .iter()
            .map(|(writes, _)| Cost::of(writes).plus(element))
            .collect();
        // The used index, the available ring's flags and index, and room for clearing NO_NOTIFY.
        let overhead = Cost { spans: 3, bytes: 8 };
        let windows = pack(&costs, overhead);
        let last = windows.len() - 1;
        let mut completions = completions.into_iter();
        for (i, window) in windows.into_iter().enumerate() {
            let q = self.lock().queues[id.queue];
            let heads = batch[window.clone()].iter().map(|r| r.head);
            let (writes, lens): (Vec<_>, Vec<_>) = completions.by_ref().take(window.len()).unzip();
            let mut used = Used::new(q, heads.zip(lens));
            let mut transfers: Vec<Transfer<'_>> = writes.into_iter().flatten().collect();
            // The completion emptying the flight clears NO_NOTIFY in its body: the publication's
            // read of the available index follows a full barrier, and `retire_window` rearms.
            let clear = i == last && flight.is_empty() && self.lock().suppressed == Some(id);
            if clear {
                transfers.push(Transfer::Write {
                    gpa: q.used,
                    bytes: &NOTIFY,
                });
            }
            let publish = used.publish(q, &mut transfers);
            let refresh = i == last
                && Cost::of(&transfers)
                    .plus(Cost::of(&snapshot.reads(q)))
                    .fits();
            if refresh {
                transfers.extend(snapshot.reads(q));
            }
            let granted = self.copy(guest, id, &mut transfers, Some(publish))?;
            drop(transfers);
            // Before the window's requests retire: a reset of `id` waits for whichever clear comes.
            if granted && clear {
                self.lock().suppressed = None;
            }
            match (granted, refresh) {
                (true, true) => snapshot.captured(id),
                (false, _) => snapshot.discard(),
                (true, false) => {}
            }
            self.retire_window(id, &used, granted, snapshot);
        }
        profile::since(profile::COMPLETE_NS, began);
        Ok(true)
    }
    /// Retire one completion window's requests. A granted window advances the used index, may
    /// interrupt, and rearms the queue if the driver published more than has been admitted (or
    /// the snapshot holds unadmitted heads); an ungranted one (a reset overtook it) only drops
    /// them.
    fn retire_window(&self, id: Identity, used: &Used, granted: bool, snapshot: &Snapshot) {
        #[cfg(feature = "testing")]
        if granted {
            for _ in 0..used.count() {
                self.observe("commit");
            }
        }
        let mut s = self.lock();
        if granted {
            let queue = &mut s.queues[id.queue];
            queue.next_used = queue.next_used.wrapping_add(used.count());
            let q = *queue;
            self.interrupt(&mut s, id, used);
            if s.current(id) && s.accepting(id.queue) {
                // Keep both notifications arriving after admission and already-captured heads,
                // even if completion writes aliased the live available index.
                let more = used.avail_index() != q.next_avail || snapshot.reusable(q, id);
                s.notified[id.queue] |= more;
            }
        }
        for _ in 0..used.count() {
            s.retire(id);
        }
        self.retire_reset(&mut s);
        self.settle(&mut s);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    struct Script {
        returns: Mutex<VecDeque<std::io::Result<usize>>>,
        calls: Mutex<Vec<(u64, usize)>>,
        /// Each sync's `full`.
        syncs: Mutex<Vec<bool>>,
        fail_sync: bool,
    }
    impl Script {
        fn calls(&self) -> Vec<(u64, usize)> {
            self.calls.lock().unwrap().clone()
        }
        fn syncs(&self) -> Vec<bool> {
            self.syncs.lock().unwrap().clone()
        }
    }
    impl Calls for Script {
        fn read(&self, b: &mut [u8], o: u64) -> std::io::Result<usize> {
            b.fill(0xa5);
            self.write(b, o)
        }
        fn write(&self, b: &[u8], o: u64) -> std::io::Result<usize> {
            self.calls.lock().unwrap().push((o, b.len()));
            self.returns.lock().unwrap().pop_front().unwrap()
        }
        fn sync(&self, full: bool) -> std::io::Result<()> {
            self.syncs.lock().unwrap().push(full);
            if self.fail_sync {
                Err(std::io::Error::other("sync failed"))
            } else {
                Ok(())
            }
        }
    }
    fn script(returns: Vec<std::io::Result<usize>>) -> Script {
        Script {
            returns: Mutex::new(returns.into()),
            calls: Mutex::default(),
            syncs: Mutex::default(),
            fail_sync: false,
        }
    }
    /// Wait (at most 2 s) until `pool` has finished `n` more jobs.
    fn results(pool: &Pool, bell: &Bell, n: usize) -> Vec<(usize, Done)> {
        let mut done = Vec::new();
        let start = Instant::now();
        while done.len() < n && start.elapsed() < Duration::from_secs(2) {
            done.extend(pool.take());
            bell.wait(Duration::from_millis(10));
        }
        assert_eq!(done.len(), n);
        done
    }
    #[test]
    fn abandoned_blocked_worker_retains_only_owned_resources_until_it_retires() {
        struct Blocked {
            started: Mutex<mpsc::Sender<()>>,
            release: Mutex<Receiver<()>>,
            retired: Mutex<mpsc::Sender<()>>,
        }
        impl Drop for Blocked {
            fn drop(&mut self) {
                let _ = self.retired.lock().unwrap().send(());
            }
        }
        impl Calls for Blocked {
            fn read(&self, b: &mut [u8], _: u64) -> std::io::Result<usize> {
                self.started.lock().unwrap().send(()).unwrap();
                self.release.lock().unwrap().recv().unwrap();
                Ok(b.len())
            }
            fn write(&self, _: &[u8], _: u64) -> std::io::Result<usize> {
                unreachable!()
            }
            fn sync(&self, _: bool) -> std::io::Result<()> {
                Ok(())
            }
        }
        let (started, wait) = mpsc::channel();
        let (release, blocked) = mpsc::channel();
        let (retired, ended) = mpsc::channel();
        let blocked = Blocked {
            started: Mutex::new(started),
            release: Mutex::new(blocked),
            retired: Mutex::new(retired),
        };
        let mut pool = Pool::spawn(blocked, Arc::default()).unwrap();
        pool.submit(0, Job::Read(0, 512)).unwrap();
        wait.recv_timeout(Duration::from_secs(1)).unwrap();
        pool.submit(1, Job::Read(512, 512)).unwrap(); // queued behind nothing, yet never run
        let _ = pool.abandon();
        drop(pool);
        assert!(ended.try_recv().is_err()); // abandonment did not free a native call's storage
        release.send(()).unwrap();
        ended.recv_timeout(Duration::from_secs(1)).unwrap(); // no blocked completion send
        assert!(wait.try_recv().is_err()); // the job queued at abandonment never started
    }
    #[test]
    fn a_pool_dropped_by_an_owner_error_runs_no_queued_job() {
        /// Every read blocks until the gate opens; every call is counted.
        #[derive(Default)]
        struct Gated {
            calls: AtomicUsize,
            open: Mutex<bool>,
            signal: Condvar,
        }
        impl Calls for Arc<Gated> {
            fn read(&self, b: &mut [u8], _: u64) -> std::io::Result<usize> {
                self.calls.fetch_add(1, SeqCst);
                let open = self.open.lock().unwrap();
                drop(self.signal.wait_while(open, |o| !*o).unwrap());
                Ok(b.len())
            }
            fn write(&self, b: &[u8], _: u64) -> std::io::Result<usize> {
                self.calls.fetch_add(1, SeqCst);
                Ok(b.len())
            }
            fn sync(&self, _: bool) -> std::io::Result<()> {
                Ok(())
            }
        }
        let gated = Arc::new(Gated::default());
        let pool = Pool::spawn(gated.clone(), Arc::default()).unwrap();
        for slot in 0..WORKERS {
            pool.submit(slot, Job::Read(0, 512)).unwrap();
        }
        let start = Instant::now();
        while gated.calls.load(SeqCst) < WORKERS && start.elapsed() < Duration::from_secs(1) {
            std::thread::yield_now();
        }
        // Every worker is busy, so this write stays queued.
        pool.submit(WORKERS, Job::Write(0, vec![1; 512], false))
            .unwrap();
        drop(pool); // as an owner's `?` exit does, without abandon or finish
        *gated.open.lock().unwrap() = true;
        gated.signal.notify_all();
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(
            gated.calls.load(SeqCst),
            WORKERS,
            "a queued write ran after the drop"
        );
    }
    #[test]
    fn native_plain_and_full_syncs_succeed_on_a_file() {
        let path = std::env::temp_dir().join(format!("natemu-sync-test-{}", std::process::id()));
        let native = Native(
            File::options()
                .create(true)
                .truncate(true)
                .read(true)
                .write(true)
                .open(&path)
                .unwrap(),
        );
        std::fs::remove_file(&path).unwrap();
        assert_eq!(native.write(&[0xa5; 512], 0).unwrap(), 512);
        native.sync(false).unwrap();
        native.sync(true).unwrap();
    }
    #[test]
    fn decoded_out_payload_moves_into_its_job() {
        let path = std::env::temp_dir().join(format!("natemu-block-test-{}", std::process::id()));
        std::fs::write(&path, vec![0; 4096]).unwrap();
        let disk = Disk::open(&path).unwrap();
        std::fs::remove_file(&path).unwrap();
        let out = Streams::new(&[
            desc(0, 16, false),
            desc(100, 512, false),
            desc(900, 1, true),
        ]);
        let mut bytes = header(1, 1);
        bytes.extend(vec![0xa5; 512]);
        let pointer = bytes.as_ptr();
        match start(&out.unwrap(), bytes, &disk, FEATURES) {
            Start::Call(Job::Write(512, data, false)) => {
                assert_eq!(data, vec![0xa5; 512]);
                assert_eq!(data.as_ptr(), pointer, "the gathered buffer was copied");
            }
            _ => panic!("OUT did not start a write"),
        }
        let read = Streams::new(&[desc(0, 16, false), desc(100, 513, true)]).unwrap();
        assert!(matches!(
            start(&read, header(0, 0), &disk, FEATURES),
            Start::Call(Job::Read(0, 512))
        ));
        // A guest FLUSH is a plain sync, not the power-off drain's full one.
        let flush = Streams::new(&[desc(0, 16, false), desc(900, 1, true)]).unwrap();
        assert!(matches!(
            start(&flush, header(4, 0), &disk, FEATURES),
            Start::Call(Job::Flush(false))
        ));
        // A readable stream too long to gather is answered IOERR without decoding.
        let long = Streams::new(&[
            desc(0, (MAX_DATA + HEADER + 1) as u32, false),
            desc(9, 1, true),
        ]);
        assert!(matches!(
            start(&long.unwrap(), Vec::new(), &disk, FEATURES),
            Start::Answer(Reply {
                status: [S_IOERR],
                ..
            })
        ));
    }
    #[test]
    fn quiesce_requires_successful_final_sync_and_worker_retirement() {
        for failed in [false, true] {
            let device = super::super::tests::device();
            let mut native = script(vec![]);
            native.fail_sync = failed;
            let native = Arc::new(native);
            let pool = Pool::spawn(Scripted(native.clone()), device.wake.clone()).unwrap();
            assert!(!device.quiesce().unwrap());
            let canceled = AtomicBool::new(false);
            assert_eq!(device.drain_disk(pool, &canceled).is_err(), failed);
            assert_eq!(device.lock().drained, !failed);
            // Power-off takes the one full sync.
            assert_eq!(native.syncs(), [true]);
        }
    }
    #[test]
    fn flush_failing_while_writes_are_in_flight_fails_only_itself() {
        /// Writes wait for the flush to have failed; the flush never waits.
        struct Ordered {
            flushed: Mutex<bool>,
            signal: Condvar,
        }
        impl Calls for Ordered {
            fn read(&self, _: &mut [u8], _: u64) -> std::io::Result<usize> {
                unreachable!()
            }
            fn write(&self, b: &[u8], _: u64) -> std::io::Result<usize> {
                let flushed = self.flushed.lock().unwrap();
                let (flushed, _) = self
                    .signal
                    .wait_timeout_while(flushed, Duration::from_secs(2), |f| !*f)
                    .unwrap();
                assert!(*flushed, "the flush waited for writes in flight");
                Ok(b.len())
            }
            fn sync(&self, _: bool) -> std::io::Result<()> {
                *self.flushed.lock().unwrap() = true;
                self.signal.notify_all();
                Err(std::io::Error::from_raw_os_error(5))
            }
        }
        let bell = Arc::new(Bell::default());
        let native = Ordered {
            flushed: Mutex::new(false),
            signal: Condvar::new(),
        };
        let pool = Pool::spawn(native, bell.clone()).unwrap();
        pool.submit(0, Job::Write(0, vec![1; 512], false)).unwrap();
        pool.submit(1, Job::Write(512, vec![2; 512], false))
            .unwrap();
        pool.submit(2, Job::Flush(false)).unwrap();
        // The writes ran only once the flush had failed (their calls assert it).
        let done = results(&pool, &bell, 3);
        for (slot, done) in done {
            assert_eq!(done.error.is_some(), slot == 2, "slot {slot}");
        }
        pool.finish().unwrap();
    }
    #[test]
    fn shorts_advance_only_the_confirmed_suffix_then_sync() {
        let native = script(vec![Ok(2), Ok(3), Ok(3)]);
        let done = execute(&native, Job::Write(512, vec![7; 8], true));
        assert!(done.error.is_none());
        assert!(done.data.is_empty());
        assert_eq!(native.calls(), [(512, 8), (514, 6), (517, 3)]);
        // Write-through is a plain sync.
        assert_eq!(native.syncs(), [false]);
    }
    #[test]
    fn zero_eintr_eio_and_overcount_never_retry_or_publish_partial_reads() {
        for error in [
            Ok(0),
            Err(std::io::Error::from_raw_os_error(4)),
            Err(std::io::Error::from_raw_os_error(5)),
            Ok(99),
        ] {
            let native = script(vec![Ok(2), error]);
            let done = execute(&native, Job::Read(0, 8));
            assert!(done.error.is_some());
            assert!(done.data.is_empty());
            assert_eq!(native.calls(), [(0, 8), (2, 6)]);
        }
    }
    #[test]
    fn flush_and_stable_write_errors_are_reported_without_replay() {
        let mut native = script(vec![Ok(8)]);
        native.fail_sync = true;
        assert!(
            execute(&native, Job::Write(0, vec![0; 8], true))
                .error
                .is_some()
        );
        assert_eq!(native.calls().len(), 1);
        assert_eq!(native.syncs(), [false]);
        assert!(execute(&native, Job::Flush(false)).error.is_some());
        assert_eq!(native.syncs(), [false, false]);
        let native = script(vec![Ok(8)]);
        assert!(
            execute(&native, Job::Write(0, vec![0; 8], false))
                .error
                .is_none()
        );
        assert!(native.syncs().is_empty());
    }
    /// An IN request reading `sectors` sectors (one 512-byte descriptor each), with head `head`.
    fn read_request(id: Identity, head: u16, sectors: usize) -> Admitted {
        let mut chain = vec![desc(0x1000, 16, false)];
        chain.extend((0..sectors).map(|i| desc(0x2000 + i as u64 * 512, 512, true)));
        chain.push(desc(0x9000 + u64::from(head), 1, true));
        let streams = Streams::new(&chain).unwrap();
        Admitted {
            id,
            head,
            reserved: streams.reservation(),
            streams,
            inbound: true,
            reply: None,
        }
    }
    fn ok(data: Vec<u8>) -> Done {
        Done { data, error: None }
    }
    /// The driver's side of a notification (Linux `virtqueue_kick_prepare` without EVENT_IDX): publish the available
    /// index, full barrier, then read the used ring's flags. Races the device's `clear` against it many times: the
    /// driver kicks (it saw the flag clear) or the device's read sees the new index; never neither.
    fn race_clear(clear: impl Fn(&crate::execution::Backing, Queue, u16) -> u16 + Sync) {
        use std::sync::{
            Barrier,
            atomic::{AtomicU16, Ordering::Relaxed, fence},
        };
        let b = crate::execution::io::tests::backing(1);
        let q = Queue {
            size: 8,
            ready: true,
            avail: 0x1000,
            used: 0x2000,
            ..Queue::default()
        };
        // SAFETY: two aligned ring fields of the fixture's RAM, accessed atomically by both sides.
        let field =
            |gpa: u64| unsafe { AtomicU16::from_ptr(b.ram.range(gpa, 2).unwrap() as *mut u16) };
        let (flags, index) = (field(q.used), field(q.avail + 2));
        let barrier = Barrier::new(2);
        let rounds = 20_000u16;
        std::thread::scope(|scope| {
            let driver = scope.spawn(|| {
                let mut kicks = Vec::with_capacity(rounds.into());
                for i in 0..rounds {
                    barrier.wait();
                    // Vary where the publication lands inside the device's copy.
                    for _ in 0..(u32::from(i) * 7919) % 2048 {
                        std::hint::spin_loop();
                    }
                    index.store(i.wrapping_add(1), Relaxed);
                    fence(SeqCst);
                    kicks.push(flags.load(Relaxed) & USED_NO_NOTIFY == 0);
                    barrier.wait();
                }
                kicks
            });
            let mut seen = Vec::with_capacity(rounds.into());
            for i in 0..rounds {
                flags.store(USED_NO_NOTIFY, Relaxed);
                index.store(i, Relaxed);
                barrier.wait();
                seen.push(clear(&b, q, i) != i);
                barrier.wait();
            }
            let kicks = driver.join().unwrap();
            let lost = kicks
                .iter()
                .zip(&seen)
                .filter(|(k, s)| !**k && !**s)
                .count();
            assert_eq!(lost, 0, "a head neither kicked nor seen");
            assert!(kicks.iter().any(|k| !k), "the race never suppressed a kick");
        });
    }
    #[test]
    fn clearing_the_flag_before_idling_loses_no_kick() {
        race_clear(|b, q, _| {
            let mut avail = [0; 4];
            let mut spans = clear_spans(q, &mut avail);
            crate::execution::io::tests::windowless(b, &mut spans, 2).unwrap();
            u16::from_le_bytes([avail[2], avail[3]])
        });
    }
    #[test]
    fn the_completion_emptying_the_flight_clears_the_flag_before_reading_the_ring() {
        race_clear(|b, q, i| {
            let q = Queue { next_avail: i, ..q };
            let mut used = Used::new(q, [(0, 0)]);
            // As `complete_answered` builds the window that empties the flight.
            let mut transfers = vec![Transfer::Write {
                gpa: q.used,
                bytes: &NOTIFY,
            }];
            let publish = used.publish(q, &mut transfers);
            crate::execution::io::tests::windowless(b, &mut transfers, publish).unwrap();
            drop(transfers);
            used.avail_index()
        });
    }
    #[test]
    fn a_reset_with_the_flag_set_waits_for_its_clear_and_schedules_nothing() {
        let more = [0, 0, 1, 0];
        // While current, heads seen after a clear are admitted instead of sleeping.
        let (r, id) = in_flight(0);
        r.lock().suppressed = Some(id);
        assert!(r.unsuppressed(id, &more));
        assert!(r.lock().suppressed.is_none());
        for reset in [reg::STATUS, reg::QUEUE_READY] {
            let (r, id) = in_flight(0);
            r.lock().suppressed = Some(id);
            // Nothing is in flight, but the ring still holds the flag: the reset (or disable) defers.
            let Access::Deferred(token) = r.access(0, reset, 4, Some(0)) else {
                panic!("reset with the flag set did not wait");
            };
            assert!(r.current(id) == false && r.lock().identity(0) == id);
            assert!(matches!(r.poll(0, token), Access::Deferred(_)));
            // The owner's clear (its incarnation's last guest write) retires it; nothing new is scheduled.
            assert!(!r.unsuppressed(id, &more));
            assert!(r.lock().identity(0) != id && !r.lock().pending());
            assert!(matches!(r.poll(0, token), Access::Completed(0)));
        }
    }
    #[test]
    fn admission_stops_at_the_request_and_byte_bounds() {
        assert_eq!(request_limit(8), 8);
        assert_eq!(request_limit(128), MAX_REQUESTS);
        assert!(within_bounds(7, 0, 0, 8) && !within_bounds(8, 0, 0, 8));
        assert!(!within_bounds(MAX_REQUESTS, 0, 0, 128));
        // One request always fits the byte bound; later ones only within it.
        assert!(within_bounds(0, 0, MAX_BUFFERED + 1, 128));
        assert!(within_bounds(1, MAX_BUFFERED - 4096, 4096, 128));
        assert!(!within_bounds(1, MAX_BUFFERED - 4095, 4096, 128));
        // A request reading 128 KiB reserves its readable header and its full IN data.
        let id = super::super::tests::device().lock().identity(0);
        let large = read_request(id, 0, 256);
        assert_eq!(large.reserved, HEADER + MAX_DATA);
        let mut flight = Flight::default();
        let mut admitted = 0;
        while within_bounds(flight.len(), flight.reserved, large.reserved, 128) {
            flight.insert(read_request(id, admitted, 256));
            admitted += 1;
        }
        assert_eq!(admitted as usize, MAX_BUFFERED / (HEADER + MAX_DATA));
        assert_eq!(flight.reserved, admitted as usize * (HEADER + MAX_DATA));
    }
    #[test]
    fn out_of_order_replies_take_used_slots_in_answering_order_across_wrap() {
        let device = super::super::tests::device();
        let id = device.lock().identity(0);
        let mut flight = Flight::default();
        let slots: Vec<usize> = (0..3)
            .map(|h| flight.insert(read_request(id, h * 3, 1)))
            .collect();
        flight.answer(slots[2], ok(vec![3; 512]));
        flight.answer(slots[0], ok(vec![1; 512]));
        assert!(flight.on_disk());
        let first = flight.take_answered();
        assert_eq!(first.iter().map(|r| r.head).collect::<Vec<_>>(), [6, 0]);
        assert_eq!(flight.len(), 1);
        let q = Queue {
            size: 8,
            used: 0x3000,
            avail: 0x2000,
            next_used: u16::MAX,
            ..Default::default()
        };
        let mut used = Used::new(q, first.iter().map(|r| (r.head, r.completion().1)));
        let mut transfers = Vec::new();
        used.publish(q, &mut transfers);
        let writes: Vec<(u64, Vec<u8>)> = transfers
            .iter()
            .filter_map(|t| match t {
                Transfer::Write { gpa, bytes } => Some((*gpa, bytes.to_vec())),
                Transfer::Read { .. } => None,
            })
            .collect();
        // Slot 7 (index 65535), then slot 0 after the wrap, then one used index.
        let element = |head: u32, len: u32| [head.to_le_bytes(), len.to_le_bytes()].concat();
        assert_eq!(
            writes,
            [
                (0x3000 + 4 + 7 * 8, element(6, 513)),
                (0x3004, element(0, 513)),
                (0x3002, 1u16.to_le_bytes().to_vec()),
            ]
        );
        assert!(matches!(
            transfers.last(),
            Some(Transfer::Read { gpa: 0x2000, .. })
        ));
    }
    #[test]
    fn a_failed_call_in_a_batch_reports_only_its_own_error() {
        let id = super::super::tests::device().lock().identity(0);
        let mut flight = Flight::default();
        let good = flight.insert(read_request(id, 0, 1));
        let bad = flight.insert(read_request(id, 3, 1));
        flight.answer(
            bad,
            Done {
                data: Vec::new(),
                error: Some("injected".into()),
            },
        );
        flight.answer(good, ok(vec![7; 512]));
        let batch = flight.take_answered();
        let (bad_writes, bad_len) = batch[0].completion();
        let (good_writes, good_len) = batch[1].completion();
        // The failed read publishes only its status, with used length 0.
        assert_eq!((bad_len, bad_writes.len()), (0, 1));
        assert!(matches!(
            bad_writes[0],
            Transfer::Write {
                gpa: 0x9003,
                bytes: [S_IOERR]
            }
        ));
        assert_eq!((good_len, good_writes.len()), (513, 2));
        assert!(matches!(
            good_writes[1],
            Transfer::Write {
                gpa: 0x9000,
                bytes: [S_OK]
            }
        ));
        assert_eq!(flight.reserved, 0);
    }
    /// A ready, live device whose queue 0 has `n` requests in flight.
    fn in_flight(n: usize) -> (Arc<Virtio>, Identity) {
        let device = super::super::tests::device();
        let id = {
            let mut s = device.lock();
            s.status = status::DRIVER_OK | status::FEATURES_OK;
            s.queues[0] = Queue {
                size: 8,
                ready: true,
                next_avail: n as u16,
                ..Default::default()
            };
            let id = s.identity(0);
            s.in_flight.extend(std::iter::repeat_n(id, n));
            id
        };
        (device, id)
    }
    fn published(q: Queue, n: u16, avail_index: u16) -> Used {
        let mut used = Used::new(q, (0..n).map(|h| (h, 1)));
        used.avail[2..].copy_from_slice(&avail_index.to_le_bytes());
        used
    }
    #[test]
    fn granted_window_advances_used_index_once_and_rearms_for_unadmitted_heads() {
        for more in [false, true] {
            let (device, id) = in_flight(3);
            let q = device.lock().queues[0];
            let snapshot = Snapshot::new(8).unwrap();
            device.retire_window(id, &published(q, 2, 3 + u16::from(more)), true, &snapshot);
            let s = device.lock();
            assert_eq!((s.queues[0].next_used, s.queues[0].next_avail), (2, 3));
            assert_eq!(s.in_flight.len(), 1);
            assert_eq!(s.interrupt, USED_BUFFER);
            assert_eq!(s.notified[0], more);
        }
    }
    #[test]
    fn reset_disable_and_power_off_wait_for_every_request_in_flight() {
        for reset in [reg::STATUS, reg::QUEUE_READY] {
            let (device, id) = in_flight(MAX_REQUESTS.min(8));
            assert!(matches!(
                device.access(0, reset, 4, Some(0)),
                Access::Deferred(_)
            ));
            let q = device.lock().queues[0];
            let snapshot = Snapshot::new(8).unwrap();
            // A granted window retires its requests even after the reset arrived; an ungranted
            // one (the reset overtook it) drops them unpublished. Neither raises an interrupt.
            device.retire_window(id, &published(q, 3, 8), true, &snapshot);
            device.retire_window(id, &published(q, 4, 8), false, &snapshot);
            {
                let s = device.lock();
                assert!(s.closing.is_some() && s.in_flight.len() == 1);
                assert_eq!((s.queues[0].next_used, s.interrupt), (3, 0));
            }
            device.retire_window(id, &published(q, 1, 8), false, &snapshot);
            let s = device.lock();
            assert!(s.closing.is_none() && s.in_flight.is_empty() && !s.queues[0].ready);
            assert_eq!(
                s.queues[0].next_used, 0,
                "the retired reset cleared the queue"
            );
        }
        let (device, id) = in_flight(4);
        assert!(!device.quiesce().unwrap());
        let q = device.lock().queues[0];
        let snapshot = Snapshot::new(8).unwrap();
        device.retire_window(id, &published(q, 4, 4), true, &snapshot);
        let s = device.lock();
        // Power-off still publishes accepted work, then admits nothing more.
        assert_eq!((s.queues[0].next_used, s.interrupt), (4, USED_BUFFER));
        assert!(s.in_flight.is_empty() && !s.notified[0] && !s.accepting(0));
    }
    #[test]
    fn a_malformed_head_fails_the_device_after_admitting_the_heads_before_it() {
        let (device, id) = in_flight(0);
        let mut snapshot = Snapshot::new(8).unwrap();
        snapshot.captured(id);
        let next = Queue {
            next_avail: 2,
            ..device.lock().queues[0]
        };
        assert!(device.commit_admission(id, next, 2, Some("bad chain".into()), &mut snapshot));
        let s = device.lock();
        assert_eq!((s.queues[0].next_avail, s.in_flight.len()), (2, 2));
        assert_ne!(s.status & status::NEEDS_RESET, 0);
        assert!(
            !s.accepting(0) && s.current(id),
            "admitted requests still complete"
        );
        assert!(!snapshot.reusable(s.queues[0], id));
        drop(s);
        // Admission overtaken by a reset admits nothing.
        let (device, id) = in_flight(0);
        device.lock().closing = Some(Reset::Device);
        assert!(!device.commit_admission(id, next, 2, None, &mut snapshot));
        assert!(device.lock().in_flight.is_empty());
    }
    fn desc(gpa: u64, len: u32, write: bool) -> Descriptor {
        Descriptor::new(gpa, len, if write { 2 } else { 0 }, 0)
    }
    fn header(kind: u32, sector: u64) -> Vec<u8> {
        let mut b = vec![0; 16];
        b[..4].copy_from_slice(&kind.to_le_bytes());
        b[8..].copy_from_slice(&sector.to_le_bytes());
        b
    }
    #[test]
    fn split_header_and_combined_payload_status_follow_byte_streams() {
        let s = Streams::new(&[
            desc(100, 7, false),
            desc(900, 9 + 512, false),
            desc(5000, 1, true),
        ])
        .unwrap();
        let mut bytes = header(1, 2);
        bytes.extend(vec![0xa5; 512]);
        match s.decode(bytes, 4096, FEATURES) {
            Command::Write(1024, b, false) => assert_eq!(b, vec![0xa5; 512]),
            _ => panic!("OUT layout refused"),
        }
        let s = Streams::new(&[desc(100, 16, false), desc(900, 513, true)]).unwrap();
        assert_eq!(s.status, 1412);
        assert!(matches!(
            s.decode(header(0, 0), 4096, FEATURES),
            Command::Read(0, 512)
        ));
        let bytes = vec![5; 512];
        let status = [0];
        let mut transfers = Vec::new();
        assert_eq!(s.completion(&bytes, &status, &mut transfers), 513);
        assert_eq!(transfers.len(), 2);
    }
    #[test]
    fn status_tail_gaps_never_inflate_used_prefix_and_errors_write_no_data() {
        let s = Streams::new(&[desc(0, 16, false), desc(100, 32, true)]).unwrap();
        assert!(matches!(
            s.decode(header(8, 0), 4096, FEATURES),
            Command::Id
        ));
        let id = [1; 20];
        let status = [0];
        let mut writes = Vec::new();
        assert_eq!(s.completion(&id, &status, &mut writes), 20);
        let mut writes = Vec::new();
        let status = [S_IOERR];
        assert_eq!(s.completion(&[], &status, &mut writes), 0);
        assert_eq!(writes.len(), 1);
        assert!(Streams::new(&[desc(0, 1, true), desc(0, 16, false)]).is_err());
        assert!(Streams::new(&[desc(0, 16, false)]).is_err());
    }
    #[test]
    fn block_limits_features_and_offsets_are_checked_before_native_effects() {
        let s = Streams::new(&[desc(0, 16, false), desc(100, 8193, true)]).unwrap();
        assert!(matches!(
            s.decode(header(0, 0), 65536, FEATURES),
            Command::Status(S_IOERR)
        ));
        let s = Streams::new(&[desc(0, 16, false), desc(100, 513, true)]).unwrap();
        for sector in [8, u64::MAX] {
            assert!(matches!(
                s.decode(header(0, sector), 4096, FEATURES),
                Command::Status(S_IOERR)
            ));
        }
        assert!(matches!(
            s.decode(header(4, 0), 4096, FEATURES & !F_FLUSH),
            Command::Status(S_UNSUPP)
        ));
        assert!(matches!(
            s.decode(header(123, 0), 4096, FEATURES),
            Command::Status(S_UNSUPP)
        ));
    }
}

/// The block owner's kick-suppression states end to end: a real owner thread over a fixture's RAM, a scripted disk
/// that can hold its calls, and a scripted copy that can make a guest register access (a reset or disable from another
/// vCPU) inside a copy, before its grant.
#[cfg(test)]
mod owner_tests {
    use super::*;
    use crate::execution::io::tests as io;
    use std::sync::{
        Condvar,
        atomic::{AtomicU16, AtomicUsize, fence},
    };

    const SIZE: u16 = 128;
    const DESC: u64 = 0;
    const AVAIL: u64 = 0x800;
    const USED: u64 = 0x1000;
    const HEADERS: u64 = 0x2000;
    const STATUS: u64 = 0x2800;
    const DATA: u64 = 0x3000;
    /// One readable buffer of a header and 128 KiB, shared by every large request.
    const BIG: u64 = 0x10000;
    const RAM: usize = 0x40000;

    /// Disk calls that wait while `held`; reads return 0x5a. Each call records whether a disk worker made it.
    #[derive(Default)]
    struct Held {
        held: Mutex<bool>,
        released: Condvar,
        calls: AtomicUsize,
        by_worker: Mutex<Vec<bool>>,
        /// Every call panics.
        panics: AtomicBool,
    }
    impl Held {
        fn hold(&self, on: bool) {
            *self.held.lock().unwrap() = on;
            self.released.notify_all();
        }
        fn call(&self) {
            let worker = std::thread::current().name() == Some("natemu-disk");
            self.by_worker.lock().unwrap().push(worker);
            self.calls.fetch_add(1, SeqCst);
            if self.panics.load(SeqCst) {
                std::panic::panic_any("injected disk panic");
            }
            let held = self.held.lock().unwrap();
            drop(self.released.wait_while(held, |h| *h).unwrap());
        }
        fn calls(&self) -> usize {
            self.calls.load(SeqCst)
        }
    }
    impl Calls for Held {
        fn read(&self, b: &mut [u8], _: u64) -> std::io::Result<usize> {
            self.call();
            b.fill(0x5a);
            Ok(b.len())
        }
        fn write(&self, b: &[u8], _: u64) -> std::io::Result<usize> {
            self.call();
            Ok(b.len())
        }
        fn sync(&self, _: bool) -> std::io::Result<()> {
            Ok(())
        }
    }

    type Hook = Box<dyn FnMut(&Rig, &[Transfer<'_>]) + Send>;
    struct Rig {
        b: crate::execution::Backing,
        r: Arc<Virtio>,
        disk: Arc<Held>,
        path: std::path::PathBuf,
        /// Called with every copy's spans before the copy (and its grant).
        hook: Mutex<Option<Hook>>,
        /// A deferred lifecycle access the hook made: (vCPU, token).
        deferred: Mutex<Option<(usize, u64)>>,
        published: Mutex<u16>,
        cancel: AtomicBool,
    }
    impl Drop for Rig {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
        }
    }
    fn until(what: &str, f: impl Fn() -> bool) {
        let start = Instant::now();
        while !f() {
            assert!(
                start.elapsed() < Duration::from_secs(5),
                "timed out: {what}"
            );
            std::thread::sleep(Duration::from_micros(200));
        }
    }
    /// The spans clear NO_NOTIFY (`bytes` 0) or set it (1).
    fn flag(spans: &[Transfer<'_>], value: u8) -> bool {
        spans.iter().any(
            |t| matches!(t, Transfer::Write { gpa: USED, bytes } if bytes.len() == 2 && bytes[0] == value),
        )
    }
    impl Rig {
        fn new(name: &str) -> Self {
            let path = std::env::temp_dir()
                .join(format!("natemu-owner-{}-{name}.raw", std::process::id()));
            File::create(&path).unwrap().set_len(1 << 20).unwrap();
            let irqs = super::super::tests::device().irqs.clone();
            let r = Virtio::block(irqs, BLOCK_RESOURCE.gsi as usize, &path).unwrap();
            let disk = Arc::new(Held::default());
            let Profile::Block(d) = &r.profile else {
                unreachable!()
            };
            *d.script.lock().unwrap() = Some(disk.clone());
            let rig = Self {
                b: io::backing_with(1, RAM),
                r,
                disk,
                path,
                hook: Mutex::new(None),
                deferred: Mutex::new(None),
                published: Mutex::new(0),
                cancel: AtomicBool::new(false),
            };
            for (offset, value) in [
                (reg::STATUS, 1),
                (reg::STATUS, 3),
                (reg::DRIVER_FEATURES_SEL, 0),
                (reg::DRIVER_FEATURES, F_SIZE_MAX | F_SEG_MAX),
                (reg::DRIVER_FEATURES_SEL, 1),
                (reg::DRIVER_FEATURES, 1),
                (reg::STATUS, 11),
                (reg::QUEUE_SEL, 0),
                (reg::QUEUE_NUM, SIZE.into()),
                (reg::QUEUE_DESC_LOW, DESC),
                (reg::QUEUE_DRIVER_LOW, AVAIL),
                (reg::QUEUE_DEVICE_LOW, USED),
                (reg::QUEUE_READY, 1),
                (reg::STATUS, 15),
            ] {
                assert_eq!(
                    rig.r.access(0, offset, 4, Some(value)),
                    Access::Completed(0)
                );
            }
            assert_eq!(rig.r.lock().status, 15);
            rig
        }
        fn put(&self, gpa: u64, bytes: &[u8]) {
            let at = self.b.ram.range(gpa, bytes.len() as u64).unwrap();
            // SAFETY: fixture RAM in bounds; the owner only reads driver-owned bytes.
            unsafe { std::ptr::copy_nonoverlapping(bytes.as_ptr(), at, bytes.len()) };
        }
        fn word(&self, gpa: u64) -> &AtomicU16 {
            // SAFETY: an aligned ring field of fixture RAM, accessed atomically on both sides.
            unsafe { AtomicU16::from_ptr(self.b.ram.range(gpa, 2).unwrap().cast()) }
        }
        fn flags(&self) -> u16 {
            self.word(USED).load(SeqCst)
        }
        fn used_index(&self) -> u16 {
            self.word(USED + 2).load(SeqCst)
        }
        fn desc(&self, i: u16, addr: u64, len: u32, flags: u16, next: u16) {
            let mut d = [0; 16];
            d[..8].copy_from_slice(&addr.to_le_bytes());
            d[8..12].copy_from_slice(&len.to_le_bytes());
            d[12..14].copy_from_slice(&flags.to_le_bytes());
            d[14..].copy_from_slice(&next.to_le_bytes());
            self.put(DESC + u64::from(i) * 16, &d);
        }
        /// Lay out request `k`: a 512-byte read of sector `k` (descriptors 3k..3k+2).
        fn read_request(&self, k: u16) -> u16 {
            let header = HEADERS + u64::from(k) * 16;
            let mut h = [0; 16];
            h[..4].copy_from_slice(&T_IN.to_le_bytes());
            h[8..].copy_from_slice(&u64::from(k).to_le_bytes());
            self.put(header, &h);
            let head = 3 * k;
            self.desc(head, header, 16, 1, head + 1);
            self.desc(head + 1, DATA + u64::from(k) * 512, 512, 3, head + 2);
            self.desc(head + 2, STATUS + u64::from(k), 1, 2, 0);
            head
        }
        /// Lay out request `k`: one readable buffer of a header and 128 KiB (a segment over size_max: gathered, then
        /// answered IOERR without a disk call), then its status (descriptors 2k, 2k+1).
        fn big_request(&self, k: u16) -> u16 {
            let mut h = [0; 16];
            h[..4].copy_from_slice(&T_OUT.to_le_bytes());
            self.put(BIG, &h);
            let head = 2 * k;
            self.desc(head, BIG, (MAX_DATA + HEADER) as u32, 1, head + 1);
            self.desc(head + 1, STATUS + u64::from(k), 1, 2, 0);
            head
        }
        /// Publish `head` in the available ring (no notification).
        fn publish(&self, head: u16) {
            let mut idx = self.published.lock().unwrap();
            self.put(AVAIL + 4 + u64::from(*idx % SIZE) * 2, &head.to_le_bytes());
            *idx = idx.wrapping_add(1);
            self.word(AVAIL + 2).store(*idx, SeqCst);
        }
        /// Linux's `virtqueue_kick_prepare` without EVENT_IDX: notify unless the device suppressed it.
        fn kick(&self) -> bool {
            fence(SeqCst);
            let kick = self.flags() & USED_NO_NOTIFY == 0;
            if kick {
                assert_eq!(
                    self.r.access(0, reg::QUEUE_NOTIFY, 4, Some(0)),
                    Access::Completed(0)
                );
            }
            kick
        }
        /// From the hook: a reset (`reg::STATUS`) or disable (`reg::QUEUE_READY`) by vCPU 1.
        fn close(&self, register: u64) {
            match self.r.access(1, register, 4, Some(0)) {
                Access::Deferred(token) => *self.deferred.lock().unwrap() = Some((1, token)),
                other => panic!("lifecycle access did not wait: {other:?}"),
            }
        }
        /// The deferred lifecycle access completed.
        fn retired(&self) -> bool {
            let Some((v, token)) = *self.deferred.lock().unwrap() else {
                return false;
            };
            self.r.poll(v, token) == Access::Completed(0)
        }
        fn on_copy(&self, hook: impl FnMut(&Rig, &[Transfer<'_>]) + Send + 'static) {
            *self.hook.lock().unwrap() = Some(Box::new(hook));
        }
        /// Run the owner while `body` drives the guest; then release the disk, stop, and require a clean owner exit.
        fn run(&self, body: impl FnOnce(&Rig)) {
            self.run_owner(|rig| {
                body(rig);
                rig.disk.hold(false);
                until("flight retired", || rig.r.lock().in_flight.is_empty());
            })
            .expect("block owner failed");
        }
        /// Run the owner while `body` drives the guest; then release the disk and stop: the owner's result.
        fn run_owner(&self, body: impl FnOnce(&Rig)) -> Result<()> {
            let copy = |spans: &mut [Transfer<'_>], publish: usize, permit: &CopyPermit| {
                if let Some(hook) = self.hook.lock().unwrap().as_mut() {
                    hook(self, spans);
                }
                io::windowless_permit(&self.b, spans, publish, permit)
            };
            let guest = Guest {
                backing: &self.b,
                copier: Copier::Test(&copy),
            };
            std::thread::scope(|scope| {
                let owner = scope.spawn(|| {
                    let Profile::Block(disk) = &self.r.profile else {
                        unreachable!()
                    };
                    self.r.block_owner(guest, disk, &self.cancel)
                });
                body(self);
                self.disk.hold(false);
                self.r.stop();
                owner
                    .join()
                    .unwrap_or_else(|panic| std::panic::resume_unwind(panic))
            })
        }
        fn by_worker(&self) -> Vec<bool> {
            self.disk.by_worker.lock().unwrap().clone()
        }
    }

    #[test]
    fn a_reset_or_disable_racing_the_idle_clear_cancels_it_and_retires_after_the_retry() {
        for register in [reg::STATUS, reg::QUEUE_READY] {
            let rig = Rig::new(&format!("idle-{register}"));
            rig.disk.hold(true);
            let mut injected = false;
            // The idle path's clear is a lone flag write and an available-ring read.
            rig.on_copy(move |rig, spans| {
                if !injected && spans.len() == 2 && flag(spans, 0) {
                    injected = true;
                    rig.close(register);
                }
            });
            rig.run(|rig| {
                // Two requests: on the workers, so the owner idles (a lone one would be called inline).
                rig.publish(rig.read_request(0));
                rig.publish(rig.read_request(1));
                assert!(rig.kick());
                until("disk calls", || rig.disk.calls() == 2);
                // The canceled clear did not end the owner; its retry ran while the reset closed.
                until("retrying clear", || {
                    rig.deferred.lock().unwrap().is_some() && rig.flags() == 0
                });
                assert!(!rig.retired(), "the reset waits for the request on disk");
                rig.disk.hold(false);
                until("reset retired", || rig.retired());
            });
            let s = rig.r.lock();
            let generation = if register == reg::STATUS {
                s.generation
            } else {
                s.generations[0]
            };
            assert_eq!(generation, 1);
            assert_eq!(
                rig.used_index(),
                0,
                "the overtaken request was not published"
            );
        }
    }

    #[test]
    fn a_reset_overtaking_the_completion_clear_retires_after_the_retiring_clear() {
        let rig = Rig::new("completion");
        let mut injected = false;
        rig.on_copy(move |rig, spans| {
            if !injected && spans.len() > 2 && flag(spans, 0) {
                injected = true;
                rig.close(reg::STATUS);
            }
        });
        rig.run(|rig| {
            rig.publish(rig.read_request(0));
            assert!(rig.kick());
            until("reset retired", || rig.retired());
        });
        assert_eq!(rig.disk.calls(), 1);
        assert_eq!((rig.flags(), rig.used_index()), (0, 0));
        assert_eq!(rig.r.lock().generation, 1);
    }

    #[test]
    fn a_quiet_wait_with_a_request_on_disk_clears_the_flag_and_admits_unkicked_heads() {
        let rig = Rig::new("quiet");
        rig.disk.hold(true);
        let mut published = false;
        // Just before the idle clear the flag is still set: a head published now gets no kick, and only the clear's
        // read of the available index can find it.
        rig.on_copy(move |rig, spans| {
            if !published && spans.len() == 2 && flag(spans, 0) {
                published = true;
                assert_eq!(rig.flags(), USED_NO_NOTIFY);
                rig.publish(rig.read_request(1));
            }
        });
        rig.run(|rig| {
            // Two requests on the workers (a lone one would be called inline, and the owner would not idle).
            rig.publish(rig.read_request(0));
            rig.publish(rig.read_request(2));
            assert!(rig.kick());
            until("unkicked head admitted", || rig.disk.calls() == 3);
            rig.disk.hold(false);
            until("all completed", || rig.used_index() == 3);
            until("flag cleared", || rig.flags() == 0);
        });
        assert_eq!(rig.r.lock().generation, 0);
        let mut data = [0; 3];
        for k in 0..3 {
            let at = rig.b.ram.range(STATUS + k, 1).unwrap();
            // SAFETY: fixture RAM in bounds; the owner has stopped.
            data[k as usize] = unsafe { *at };
        }
        assert_eq!(data, [S_OK; 3]);
    }

    #[test]
    fn power_off_drains_with_the_flag_clear() {
        let rig = Rig::new("power");
        rig.disk.hold(true);
        rig.run(|rig| {
            rig.publish(rig.read_request(0));
            assert!(rig.kick());
            until("disk call", || rig.disk.calls() == 1);
            assert!(!rig.r.quiesce().unwrap());
            rig.disk.hold(false);
            until("drained", || rig.r.quiesce().unwrap());
        });
        assert_eq!((rig.flags(), rig.used_index()), (0, 1));
        // The lone request's call was the owner's (the final sync is no read or write).
        assert_eq!(rig.by_worker(), [false]);
    }

    #[test]
    fn a_lone_request_is_called_inline_and_more_arriving_meanwhile_go_to_the_pool() {
        let rig = Rig::new("inline");
        rig.disk.hold(true);
        rig.run(|rig| {
            rig.publish(rig.read_request(0));
            assert!(rig.kick());
            until("inline call", || rig.disk.calls() == 1);
            // The owner is in the call: two more heads, unkicked (suppressed), wait for its completion's ring read.
            rig.publish(rig.read_request(1));
            rig.publish(rig.read_request(2));
            assert!(!rig.kick(), "kicks stay suppressed during the inline call");
            rig.disk.hold(false);
            until("two on the workers", || rig.used_index() == 3);
            // Nothing in flight again: a lone request is the owner's call once more.
            until("idle", || rig.flags() == 0);
            rig.publish(rig.read_request(3));
            assert!(rig.kick());
            until("inline again", || rig.used_index() == 4);
        });
        assert_eq!(rig.by_worker(), [false, true, true, false]);
    }

    #[test]
    fn a_reset_or_disable_during_an_inline_call_waits_for_it_and_later_accesses_complete() {
        for register in [reg::STATUS, reg::QUEUE_READY] {
            let rig = Rig::new(&format!("inline-{register}"));
            rig.disk.hold(true);
            rig.run(|rig| {
                rig.publish(rig.read_request(0));
                assert!(rig.kick());
                until("inline call", || rig.disk.calls() == 1);
                rig.close(register);
                // A register read by another vCPU waits behind the reset, as during a worker's call.
                let Access::Deferred(read) = rig.r.access(2, reg::STATUS, 4, None) else {
                    panic!("an access behind a closing reset did not wait");
                };
                std::thread::sleep(Duration::from_millis(20));
                assert!(!rig.retired(), "the reset waits for the call");
                assert!(matches!(rig.r.poll(2, read), Access::Deferred(_)));
                rig.disk.hold(false);
                until("reset retired", || rig.retired());
                let status = if register == reg::STATUS { 0 } else { 15 };
                until("read completed", || {
                    rig.r.poll(2, read) == Access::Completed(status)
                });
            });
            assert_eq!(rig.by_worker(), [false]);
            assert_eq!((rig.flags(), rig.used_index()), (0, 0));
            let s = rig.r.lock();
            assert_eq!(s.generation + s.generations[0], 1);
        }
    }

    #[test]
    fn an_owner_panicking_in_its_inline_call_ends_with_its_watchdog() {
        let rig = Rig::new("panic");
        rig.disk.panics.store(true, SeqCst);
        let ended = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            rig.run_owner(|rig| {
                rig.publish(rig.read_request(0));
                assert!(rig.kick());
                until("inline call", || rig.disk.calls() == 1);
            })
        }));
        // The owner's own panic, not the body's timeout, propagates instead of hanging.
        let panic = ended.expect_err("the owner's panic propagates");
        assert_eq!(panic.downcast_ref::<&str>(), Some(&"injected disk panic"));
        assert_eq!(rig.by_worker(), [false]);
    }

    #[test]
    fn an_inline_call_past_the_call_limit_fails_the_vm() {
        let rig = Rig::new("overrun");
        let limit = Duration::from_millis(500);
        let Profile::Block(d) = &rig.r.profile else {
            unreachable!()
        };
        *d.call_limit.lock().unwrap() = Some(limit);
        rig.disk.hold(true);
        let result = rig.run_owner(|rig| {
            rig.publish(rig.read_request(0));
            assert!(rig.kick());
            until("inline call", || rig.disk.calls() == 1);
            // The watchdog cancels the VM while the owner is still in its call.
            let start = Instant::now();
            while !rig.cancel.load(SeqCst) {
                assert!(start.elapsed() < limit * 2, "no watchdog");
                std::thread::sleep(Duration::from_millis(5));
            }
            assert!(start.elapsed() >= limit / 2);
            assert_eq!(rig.used_index(), 0);
        });
        assert!(result.unwrap_err().contains("abandoned"));
        assert_eq!(rig.used_index(), 0, "the overrun call is not published");
    }

    #[test]
    fn spins_are_bounded_and_a_ring_is_never_lost() {
        let bell = Bell::default();
        let start = Instant::now();
        assert!(!bell.wait_spinning(Duration::from_millis(20), Duration::from_millis(5)));
        let waited = start.elapsed();
        assert!(waited >= Duration::from_millis(20) && waited < Duration::from_secs(1));
        // A ring during the spin, during the sleep, and racing the switch from one to the other.
        let bell = &bell;
        std::thread::scope(|scope| {
            for i in 0..2000u32 {
                let ringer = scope.spawn(move || {
                    for _ in 0..(i * 7919) % 4096 {
                        std::hint::spin_loop();
                    }
                    bell.ring();
                });
                let spin = Duration::from_micros(u64::from(i % 3) * 2);
                assert!(
                    bell.wait_spinning(Duration::from_secs(2), spin),
                    "ring {i} lost"
                );
                ringer.join().unwrap();
            }
        });
        // A worker's spin ends at its bound, then it blocks until a job (or the pool's close).
        let (jobs, queue) = mpsc::sync_channel::<Ticket>(1);
        let start = Instant::now();
        std::thread::scope(|scope| {
            scope.spawn(|| {
                std::thread::sleep(Duration::from_millis(30));
                jobs.send((7, Job::Flush(false))).unwrap();
            });
            let (slot, _) = receive(&queue, Duration::from_millis(5)).unwrap();
            assert_eq!(slot, 7);
        });
        assert!(start.elapsed() >= Duration::from_millis(30));
        drop(jobs);
        assert!(receive(&queue, Duration::from_millis(1)).is_err());
    }

    #[test]
    fn a_reset_canceling_a_later_gather_window_waits_for_the_flag_set_in_the_first() {
        let rig = Rig::new("gather");
        let mut windows = 0;
        // Gather windows read the large buffer; the first one also sets the flag.
        rig.on_copy(move |rig, spans| {
            let gather = spans
                .iter()
                .any(|t| matches!(t, Transfer::Read { gpa: BIG, .. }));
            if gather {
                windows += 1;
                assert_eq!(flag(spans, 1), windows == 1);
                if windows == 2 {
                    rig.close(reg::STATUS);
                }
            }
        });
        rig.run(|rig| {
            for k in 0..8 {
                rig.publish(rig.big_request(k));
            }
            assert!(rig.kick());
            until("reset retired", || rig.retired());
        });
        assert_eq!(rig.disk.calls(), 0);
        assert_eq!((rig.flags(), rig.used_index()), (0, 0));
        assert_eq!(rig.r.lock().generation, 1);
    }
}
