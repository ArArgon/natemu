//! virtio-blk over one owned regular file: modern request parsing, a fixed pool of native disk
//! workers that make the file calls outside every lock and copy window, and the block owner,
//! which keeps several requests in flight and batches their copy windows.
use super::*;
use std::{
    fs::{File, OpenOptions, TryLockError},
    os::unix::fs::{FileExt, MetadataExt},
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
/// A native call running longer abandons the workers, and the VM fails.
const CALL_LIMIT: Duration = Duration::from_secs(10);
/// The power-off drain's final full sync, outside every in-flight slot.
const FINAL_SYNC: usize = usize::MAX;

pub(super) struct Disk {
    #[cfg(feature = "testing")]
    pub control: Mutex<Option<Arc<super::testing::Control>>>,
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
    Flush,
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
    running: Vec<(usize, Instant)>,
    finished: Vec<(usize, Done)>,
    /// The owner gave up on the pool: queued jobs are dropped, never run.
    abandoned: bool,
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
}
impl Pool {
    fn spawn(calls: impl Calls + 'static, bell: Arc<Bell>) -> Result<Self> {
        let calls = Arc::new(calls);
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
    /// Some native call has run longer than the call limit.
    pub fn stalled(&self) -> bool {
        let board = board(&self.board);
        board.running.iter().any(|(_, t)| t.elapsed() > CALL_LIMIT)
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
/// One worker: run jobs until the pool closes or is abandoned.
fn work(calls: &impl Calls, queue: &Mutex<Receiver<Ticket>>, shared: &Mutex<Board>, bell: &Bell) {
    loop {
        let ticket = queue.lock().unwrap_or_else(|e| e.into_inner()).recv();
        let Ok((slot, job)) = ticket else {
            return;
        };
        {
            let mut board = board(shared);
            if board.abandoned {
                return;
            }
            board.running.push((slot, Instant::now()));
        }
        let done = execute(calls, job);
        {
            let mut board = board(shared);
            board.running.retain(|(s, _)| *s != slot);
            board.finished.push((slot, done));
        }
        bell.ring();
    }
}

/// The workers' native file calls; replaced by scripted calls in tests and test schedules.
/// Calls run concurrently on one file.
pub(super) trait Calls: Send + Sync {
    fn read(&self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize>;
    fn write(&self, bytes: &[u8], offset: u64) -> std::io::Result<usize>;
    fn sync(&self) -> std::io::Result<()>;
}
/// One positioned read or write per call (pread/pwrite: a short count is returned as is, EINTR
/// is an error); sync is F_FULLFSYNC (`File::sync_all` on Apple platforms).
pub(super) struct Native(File);
impl Calls for Native {
    fn read(&self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize> {
        self.0.read_at(bytes, offset)
    }
    fn write(&self, bytes: &[u8], offset: u64) -> std::io::Result<usize> {
        self.0.write_at(bytes, offset)
    }
    fn sync(&self) -> std::io::Result<()> {
        self.0.sync_all()
    }
}

/// Run one job to completion or its first error. A short transfer continues with only the
/// unconfirmed suffix; an error (EINTR included) or zero progress is never retried, since its
/// effect is uncertain. A stable write syncs after the last byte.
fn execute(native: &impl Calls, job: Job) -> Done {
    let (offset, mut data, write, stable) = match job {
        Job::Read(offset, len) => (offset, vec![0; len], false, false),
        Job::Write(offset, bytes, stable) => (offset, bytes, true, stable),
        Job::Flush => {
            return Done {
                data: Vec::new(),
                error: native.sync().err().map(|e| e.to_string()),
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
        error = native.sync().err().map(|e| e.to_string());
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
    pub fn decode(&self, bytes: &[u8], capacity: u64, features: u64) -> Command {
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
                    Command::Write(offset, bytes[HEADER..].to_vec(), stable)
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

impl Virtio {
    /// The block owner: admit every published request the bounds allow, gather their headers and
    /// OUT payloads, hand their disk calls to the workers, and complete answered requests in
    /// batches. It never blocks on one job: finished jobs ring its bell.
    pub(super) fn block_owner(
        &self,
        guest: Guest<'_>,
        disk: &Disk,
        cancel: &AtomicBool,
    ) -> Result<()> {
        let mut snapshot = Snapshot::new(self.profile.queue_max())?;
        let mut pool = disk.start(self.wake.clone())?;
        let mut flight = Flight::default();
        loop {
            let (stopped, quiescing) = {
                let s = self.lock();
                (s.stopped || cancel.load(SeqCst), s.quiescing)
            };
            if stopped {
                return if flight.on_disk() {
                    Err(pool.abandon())
                } else {
                    pool.finish()
                };
            }
            for (slot, done) in pool.take() {
                flight.answer(slot, done);
            }
            if pool.stalled() {
                return Err(pool.abandon());
            }
            if self.complete_answered(guest, disk, &mut flight, &mut snapshot)? {
                continue;
            }
            if quiescing && flight.is_empty() {
                return self.drain_disk(pool, cancel);
            }
            if self.admit_block(guest, disk, &pool, &mut flight, &mut snapshot)? {
                continue;
            }
            self.wake.wait(Duration::from_millis(10));
        }
    }
    /// Power-off drain once nothing is in flight: a final full sync on a worker (it covers every
    /// completed write), the workers' join, then a quiet device.
    pub(super) fn drain_disk(&self, mut pool: Pool, cancel: &AtomicBool) -> Result<()> {
        pool.submit(FINAL_SYNC, Job::Flush)?;
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

    /// Admit every published request the bounds allow from one metadata snapshot (captured now
    /// unless the last completion window refreshed it), gather their headers and OUT payloads,
    /// and start their disk calls. True if any request was admitted.
    fn admit_block(
        &self,
        guest: Guest<'_>,
        disk: &Disk,
        pool: &Pool,
        flight: &mut Flight,
        snapshot: &mut Snapshot,
    ) -> Result<bool> {
        let (q, id) = {
            let mut s = self.lock();
            let (q, id) = (s.queues[0], s.identity(0));
            let work = s.notified[0] || snapshot.reusable(q, id);
            let open = s.admission_open() && s.accepting(0) && !s.stopped;
            if !work || !open || flight.len() >= request_limit(q.size) {
                return Ok(false);
            }
            s.notified[0] = false;
            (q, id)
        };
        let ram = &guest.backing.ram;
        let mut invalid = self.check_rings(q, ram).err();
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
                inbound: false,
                reply: None,
            });
            next.next_avail = next.next_avail.wrapping_add(1);
            (count, reserved) = (count + 1, reserved + bytes);
        }
        if !self.commit_admission(id, next, admitted.len(), invalid, snapshot) {
            return Ok(false);
        }
        self.start_requests(guest, disk, pool, flight, id, admitted)?;
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
    /// every later one) retires unanswered.
    fn start_requests(
        &self,
        guest: Guest<'_>,
        disk: &Disk,
        pool: &Pool,
        flight: &mut Flight,
        id: Identity,
        mut admitted: Vec<Admitted>,
    ) -> Result<()> {
        let mut readable: Vec<Vec<u8>> = admitted
            .iter()
            .map(|r| vec![0; r.streams.gathered_len()])
            .collect();
        let gathered = self.gather_streams(guest, id, &admitted, &mut readable)?;
        let features = self.lock().features;
        let starts: Vec<Start> = admitted[..gathered]
            .iter_mut()
            .zip(&readable)
            .map(|(request, bytes)| {
                let command = if request.streams.gathered_len() == request.streams.readable_len() {
                    request.streams.decode(bytes, disk.capacity, features)
                } else {
                    Command::Status(S_IOERR)
                };
                request.inbound = matches!(command, Command::Read(..));
                match command {
                    Command::Read(offset, len) => Start::Call(Job::Read(offset, len)),
                    Command::Write(offset, bytes, stable) => {
                        Start::Call(Job::Write(offset, bytes, stable))
                    }
                    Command::Flush => Start::Call(Job::Flush),
                    Command::Id => Start::Answer(Reply {
                        data: disk.id.to_vec(),
                        status: [S_OK],
                    }),
                    Command::Status(status) => Start::Answer(Reply::without_data(status)),
                }
            })
            .collect();
        let mut s = self.lock();
        // Submission and reset admission are ordered, but no native call holds this lock.
        let live = s.current(id);
        let mut requests = admitted.into_iter();
        for (start, mut request) in starts.into_iter().zip(requests.by_ref()) {
            if !live {
                s.retire(id);
                continue;
            }
            let job = match start {
                Start::Call(job) => Some(job),
                Start::Answer(reply) => {
                    request.reply = Some(reply);
                    None
                }
            };
            let slot = flight.insert(request);
            if let Some(job) = job {
                pool.submit(slot, job)?;
            }
        }
        for request in requests {
            s.retire(request.id);
        }
        self.retire_reset(&mut s);
        self.settle(&mut s);
        Ok(())
    }
    /// Read each request's gathered stream into `readable` in packed windows. Returns how many
    /// leading requests were gathered: all, unless a reset overtook a window.
    fn gather_streams(
        &self,
        guest: Guest<'_>,
        id: Identity,
        admitted: &[Admitted],
        readable: &mut [Vec<u8>],
    ) -> Result<usize> {
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
        let mut gathered = 0;
        for window in pack(&costs, Cost::default()) {
            let mut spans: Vec<Transfer<'_>> =
                reads.by_ref().take(window.len()).flatten().collect();
            if !spans.is_empty() && !self.copy(guest, id, &mut spans)? {
                break;
            }
            gathered = window.end;
        }
        Ok(gathered)
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
        let batch = flight.take_answered();
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
        // The used index and the available ring's flags and index.
        let overhead = Cost { spans: 2, bytes: 6 };
        let windows = pack(&costs, overhead);
        let last = windows.len() - 1;
        let mut completions = completions.into_iter();
        for (i, window) in windows.into_iter().enumerate() {
            let q = self.lock().queues[id.queue];
            let heads = batch[window.clone()].iter().map(|r| r.head);
            let (writes, lens): (Vec<_>, Vec<_>) = completions.by_ref().take(window.len()).unzip();
            let mut used = Used::new(q, heads.zip(lens));
            let mut transfers: Vec<Transfer<'_>> = writes.into_iter().flatten().collect();
            used.publish(q, &mut transfers);
            let refresh = i == last
                && Cost::of(&transfers)
                    .plus(Cost::of(&snapshot.reads(q)))
                    .fits();
            if refresh {
                transfers.extend(snapshot.reads(q));
            }
            let granted = self.copy(guest, id, &mut transfers)?;
            drop(transfers);
            match (granted, refresh) {
                (true, true) => snapshot.captured(id),
                (false, _) => snapshot.discard(),
                (true, false) => {}
            }
            self.retire_window(id, &used, granted, snapshot);
        }
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
        syncs: AtomicUsize,
        fail_sync: bool,
    }
    impl Script {
        fn calls(&self) -> Vec<(u64, usize)> {
            self.calls.lock().unwrap().clone()
        }
        fn syncs(&self) -> usize {
            self.syncs.load(SeqCst)
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
        fn sync(&self) -> std::io::Result<()> {
            self.syncs.fetch_add(1, SeqCst);
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
            syncs: AtomicUsize::new(0),
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
            fn sync(&self) -> std::io::Result<()> {
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
    fn quiesce_requires_successful_final_sync_and_worker_retirement() {
        for failed in [false, true] {
            let device = super::super::tests::device();
            let mut native = script(vec![]);
            native.fail_sync = failed;
            let pool = Pool::spawn(native, device.wake.clone()).unwrap();
            assert!(!device.quiesce().unwrap());
            let canceled = AtomicBool::new(false);
            assert_eq!(device.drain_disk(pool, &canceled).is_err(), failed);
            assert_eq!(device.lock().drained, !failed);
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
            fn sync(&self) -> std::io::Result<()> {
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
        pool.submit(2, Job::Flush).unwrap();
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
        assert_eq!(native.syncs(), 1);
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
        assert_eq!(native.syncs(), 1);
        assert!(execute(&native, Job::Flush).error.is_some());
        assert_eq!(native.syncs(), 2);
        let native = script(vec![Ok(8)]);
        assert!(
            execute(&native, Job::Write(0, vec![0; 8], false))
                .error
                .is_none()
        );
        assert_eq!(native.syncs(), 0);
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
        match s.decode(&bytes, 4096, FEATURES) {
            Command::Write(1024, b, false) => assert_eq!(b, vec![0xa5; 512]),
            _ => panic!("OUT layout refused"),
        }
        let s = Streams::new(&[desc(100, 16, false), desc(900, 513, true)]).unwrap();
        assert_eq!(s.status, 1412);
        assert!(matches!(
            s.decode(&header(0, 0), 4096, FEATURES),
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
            s.decode(&header(8, 0), 4096, FEATURES),
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
            s.decode(&header(0, 0), 65536, FEATURES),
            Command::Status(S_IOERR)
        ));
        let s = Streams::new(&[desc(0, 16, false), desc(100, 513, true)]).unwrap();
        for sector in [8, u64::MAX] {
            assert!(matches!(
                s.decode(&header(0, sector), 4096, FEATURES),
                Command::Status(S_IOERR)
            ));
        }
        assert!(matches!(
            s.decode(&header(4, 0), 4096, FEATURES & !F_FLUSH),
            Command::Status(S_UNSUPP)
        ));
        assert!(matches!(
            s.decode(&header(123, 0), 4096, FEATURES),
            Command::Status(S_UNSUPP)
        ));
    }
}
