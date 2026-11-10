//! virtio-blk over one owned regular file: modern request parsing, and one ordered native lane
//! (a thread) that performs the file calls outside every lock and copy window.
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

pub(super) struct Disk {
    #[cfg(feature = "testing")]
    pub control: Mutex<Option<Arc<super::testing::Control>>>,
    /// Taken by the lane when the owner starts.
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
    pub fn start(&self) -> Result<Lane> {
        let file = self
            .file
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take()
            .ok_or("disk lane already started")?;
        #[cfg(feature = "testing")]
        if let Some(control) = self
            .control
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
        {
            return Lane::spawn(super::testing::Controlled {
                native: Native(file),
                control,
                phase: 0,
            });
        }
        Lane::spawn(Native(file))
    }
}

/// One native operation for the lane.
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
/// The spawned closure owns no VM state. A fatal timeout detaches it until the syscall retires
/// or the per-VM CLI process exits. Embedders must treat that error as worker-process fatal.
pub(super) struct Lane {
    tx: Option<SyncSender<Job>>,
    rx: Receiver<Done>,
    thread: Option<JoinHandle<()>>,
}
impl Lane {
    fn spawn(mut native: impl Calls + Send + 'static) -> Result<Self> {
        let (tx, work) = mpsc::sync_channel(1);
        let (result, rx) = mpsc::sync_channel(1);
        let thread = std::thread::Builder::new()
            .name("natemu-disk".into())
            .spawn(move || {
                while let Ok(job) = work.recv() {
                    let done = execute(&mut native, job);
                    // Exactly one job in flight; a result slot was reserved before dispatch.
                    if result.try_send(done).is_err() {
                        break;
                    }
                }
            })
            .map_err(|e| format!("disk lane: {e}"))?;
        Ok(Self {
            tx: Some(tx),
            rx,
            thread: Some(thread),
        })
    }
    pub fn submit(&mut self, job: Job) -> Result<()> {
        self.tx
            .as_ref()
            .ok_or("closed disk lane")?
            .try_send(job)
            .map_err(|e| format!("disk lane submission: {e}"))
    }
    /// Wait for the job's result. Cancellation or a 10 s timeout abandons the lane for good.
    pub fn wait(&mut self, canceled: impl Fn() -> bool) -> Result<Done> {
        let start = Instant::now();
        loop {
            match self.rx.recv_timeout(Duration::from_millis(10)) {
                Ok(done) => return Ok(done),
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err("disk lane disconnected".into());
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
            if canceled() || start.elapsed() > Duration::from_secs(10) {
                // The eventual native completion cannot enqueue more work.
                self.tx.take();
                return Err("disk operation abandoned: VM worker must terminate; file may have partial effects".into());
            }
        }
    }
    /// Close and join the lane. Called only with no outstanding operation.
    pub fn finish(mut self) -> Result<()> {
        self.tx.take();
        self.thread
            .take()
            .expect("joined once")
            .join()
            .map_err(|_| "disk lane panicked".into())
    }
}

/// The lane's native file calls; replaced by scripted calls in tests and test schedules.
pub(super) trait Calls {
    fn read(&mut self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize>;
    fn write(&mut self, bytes: &[u8], offset: u64) -> std::io::Result<usize>;
    fn sync(&mut self) -> std::io::Result<()>;
}
/// One positioned read or write per call (pread/pwrite: a short count is returned as is, EINTR
/// is an error); sync is F_FULLFSYNC (`File::sync_all` on Apple platforms).
pub(super) struct Native(File);
impl Calls for Native {
    fn read(&mut self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize> {
        self.0.read_at(bytes, offset)
    }
    fn write(&mut self, bytes: &[u8], offset: u64) -> std::io::Result<usize> {
        self.0.write_at(bytes, offset)
    }
    fn sync(&mut self) -> std::io::Result<()> {
        self.0.sync_all()
    }
}

/// Run one job to completion or its first error. A short transfer continues with only the
/// unconfirmed suffix; an error (EINTR included) or zero progress is never retried, since its
/// effect is uncertain. A stable write syncs after the last byte.
fn execute(native: &mut impl Calls, job: Job) -> Done {
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

impl Virtio {
    pub(super) fn block_request(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        disk: &Disk,
        lane: &mut Lane,
        snapshot: &mut Snapshot,
        cancel: &AtomicBool,
    ) -> Result<Request> {
        let streams = match Streams::new(chain.descriptors) {
            Ok(s) => s,
            Err(e) => return Ok(Request::Invalid(e)),
        };
        let command = if streams.readable_len() > MAX_DATA + HEADER {
            Command::Status(S_IOERR)
        } else {
            let mut bytes = vec![0; streams.readable_len()];
            let extents = streams.readable.iter().map(|e| (e.gpa, e.len));
            if !self.copy(guest, chain.id, &mut gather(extents, &mut bytes))? {
                return Ok(Request::Canceled);
            }
            streams.decode(&bytes, disk.capacity, self.lock().features)
        };
        let inbound = matches!(command, Command::Read(..));
        let job = match command {
            Command::Read(offset, len) => Job::Read(offset, len),
            Command::Write(offset, bytes, stable) => Job::Write(offset, bytes, stable),
            Command::Flush => Job::Flush,
            Command::Id => {
                return self
                    .block_complete(guest, chain, snapshot, &streams, &disk.id, S_OK, inbound);
            }
            Command::Status(status) => {
                return self.block_complete(guest, chain, snapshot, &streams, &[], status, inbound);
            }
        };
        {
            let s = self.lock();
            if !s.current(chain.id) {
                return Ok(Request::Canceled);
            }
            // Submission and reset admission are ordered, but no native call holds this lock.
            lane.submit(job)?;
        }
        let done = lane.wait(|| cancel.load(SeqCst) || self.lock().stopped)?;
        match done.error {
            Some(e) => {
                eprintln!("virtio-block: {e}");
                self.block_complete(guest, chain, snapshot, &streams, &[], S_IOERR, inbound)
            }
            None => {
                self.block_complete(guest, chain, snapshot, &streams, &done.data, S_OK, inbound)
            }
        }
    }
    /// Publish `data`, the status byte and the used element. A failed read reports no bytes.
    fn block_complete(
        &self,
        guest: Guest<'_>,
        chain: &Chain<'_>,
        snapshot: &mut Snapshot,
        streams: &Streams,
        data: &[u8],
        status: u8,
        inbound: bool,
    ) -> Result<Request> {
        let status = [status];
        let mut writes = Vec::with_capacity(streams.writable.len() + 4);
        let mut len = streams.completion(data, &status, &mut writes);
        if inbound && status[0] != S_OK {
            len = 0;
        }
        Ok(
            match self.complete(guest, chain, len, writes, Some(snapshot))? {
                Some(used) => Request::Committed {
                    pending: used.more_published(chain.queue),
                },
                None => Request::Canceled,
            },
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    struct Script {
        returns: VecDeque<std::io::Result<usize>>,
        calls: Vec<(u64, usize)>,
        syncs: usize,
        fail_sync: bool,
    }
    impl Calls for Script {
        fn read(&mut self, b: &mut [u8], o: u64) -> std::io::Result<usize> {
            b.fill(0xa5);
            self.write(b, o)
        }
        fn write(&mut self, b: &[u8], o: u64) -> std::io::Result<usize> {
            self.calls.push((o, b.len()));
            self.returns.pop_front().unwrap()
        }
        fn sync(&mut self) -> std::io::Result<()> {
            self.syncs += 1;
            if self.fail_sync {
                Err(std::io::Error::other("sync failed"))
            } else {
                Ok(())
            }
        }
    }
    fn script(returns: Vec<std::io::Result<usize>>) -> Script {
        Script {
            returns: returns.into(),
            calls: vec![],
            syncs: 0,
            fail_sync: false,
        }
    }
    #[test]
    fn canceled_blocked_lane_retains_only_owned_resources_until_it_retires() {
        struct Blocked {
            started: mpsc::Sender<()>,
            release: Receiver<()>,
            retired: mpsc::Sender<()>,
        }
        impl Drop for Blocked {
            fn drop(&mut self) {
                let _ = self.retired.send(());
            }
        }
        impl Calls for Blocked {
            fn read(&mut self, b: &mut [u8], _: u64) -> std::io::Result<usize> {
                self.started.send(()).unwrap();
                self.release.recv().unwrap();
                Ok(b.len())
            }
            fn write(&mut self, _: &[u8], _: u64) -> std::io::Result<usize> {
                unreachable!()
            }
            fn sync(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let (started, wait) = mpsc::channel();
        let (release, blocked) = mpsc::channel();
        let (retired, ended) = mpsc::channel();
        let mut lane = Lane::spawn(Blocked {
            started,
            release: blocked,
            retired,
        })
        .unwrap();
        lane.submit(Job::Read(0, 512)).unwrap();
        wait.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(lane.wait(|| true).is_err());
        drop(lane);
        assert!(ended.try_recv().is_err()); // cancellation did not free a native call's storage
        release.send(()).unwrap();
        ended.recv_timeout(Duration::from_secs(1)).unwrap(); // no blocked completion send
    }
    #[test]
    fn quiesce_requires_successful_final_sync_and_lane_retirement() {
        for failed in [false, true] {
            let device = super::super::tests::device();
            let mut native = script(vec![]);
            native.fail_sync = failed;
            let lane = Lane::spawn(native).unwrap();
            assert!(!device.quiesce().unwrap());
            let canceled = AtomicBool::new(false);
            assert_eq!(device.drain(Some(lane), &canceled).is_err(), failed);
            assert_eq!(device.lock().drained, !failed);
        }
    }
    #[test]
    fn shorts_advance_only_the_confirmed_suffix_then_sync() {
        let mut native = script(vec![Ok(2), Ok(3), Ok(3)]);
        let done = execute(&mut native, Job::Write(512, vec![7; 8], true));
        assert!(done.error.is_none());
        assert!(done.data.is_empty());
        assert_eq!(native.calls, [(512, 8), (514, 6), (517, 3)]);
        assert_eq!(native.syncs, 1);
    }
    #[test]
    fn zero_eintr_eio_and_overcount_never_retry_or_publish_partial_reads() {
        for error in [
            Ok(0),
            Err(std::io::Error::from_raw_os_error(4)),
            Err(std::io::Error::from_raw_os_error(5)),
            Ok(99),
        ] {
            let mut native = script(vec![Ok(2), error]);
            let done = execute(&mut native, Job::Read(0, 8));
            assert!(done.error.is_some());
            assert!(done.data.is_empty());
            assert_eq!(native.calls, [(0, 8), (2, 6)]);
        }
    }
    #[test]
    fn flush_and_stable_write_errors_are_reported_without_replay() {
        let mut native = script(vec![Ok(8)]);
        native.fail_sync = true;
        assert!(
            execute(&mut native, Job::Write(0, vec![0; 8], true))
                .error
                .is_some()
        );
        assert_eq!(native.calls.len(), 1);
        assert_eq!(native.syncs, 1);
        assert!(execute(&mut native, Job::Flush).error.is_some());
        assert_eq!(native.syncs, 2);
        let mut native = script(vec![Ok(8)]);
        assert!(
            execute(&mut native, Job::Write(0, vec![0; 8], false))
                .error
                .is_none()
        );
        assert_eq!(native.syncs, 0);
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
