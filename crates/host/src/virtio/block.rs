//! One owned regular file, one ordered native lane, and modern block stream parsing.
use super::*;
use std::{fs::{File, OpenOptions}, os::{fd::AsRawFd, unix::fs::MetadataExt}, path::Path,
    sync::mpsc::{self, Receiver, SyncSender}, thread::JoinHandle, time::Instant};
use virtio_queue::desc::split::Descriptor;

pub(super) const MAX_DATA: usize = 128 * 1024;
pub(super) const FEATURES: u64 = VERSION_1 | (1 << 1) | (1 << 2) | (1 << 6) | (1 << 9);
pub(super) const REQUIRED: u64 = VERSION_1 | (1 << 1) | (1 << 2);
pub(super) const FLUSH: u64 = 1 << 9;
const IOERR: u8 = 1;
const UNSUPP: u8 = 2;

pub(super) struct Disk {
    #[cfg(feature = "testing")]
    pub control: Mutex<Option<Arc<super::testing::Control>>>,
    file: Mutex<Option<File>>, pub capacity: u64, pub id: [u8; 20] }
impl Disk {
    pub fn open(path: &Path) -> Result<Self> {
        let file = OpenOptions::new().read(true).write(true).open(path).map_err(|e| format!("disk {}: {e}", path.display()))?;
        let info = file.metadata().map_err(|e| e.to_string())?;
        if !info.is_file() || info.len() == 0 || info.len() % 512 != 0 || info.len() > i64::MAX as u64 {
            return Err("disk must be a nonempty regular file with sector-aligned capacity".into());
        }
        unsafe extern "C" { fn flock(fd: i32, operation: i32) -> i32; }
        // Advisory exclusion among cooperating launches; users must exclude other writers/truncators.
        if unsafe { flock(file.as_raw_fd(), 2 | 4) } != 0 { return Err(format!("disk lock: {}", std::io::Error::last_os_error())); }
        let id = format!("{:08x}{:012x}", info.dev() as u32, info.ino() & 0xffff_ffff_ffff).into_bytes().try_into().unwrap();
        Ok(Self {
            #[cfg(feature = "testing")]
            control: Mutex::new(None),
            file: Mutex::new(Some(file)), capacity: info.len(), id })
    }
    pub fn config(&self) -> [u8; 24] {
        let mut c = [0; 24];
        c[..8].copy_from_slice(&(self.capacity / 512).to_le_bytes());
        c[8..12].copy_from_slice(&4096u32.to_le_bytes());
        c[12..16].copy_from_slice(&32u32.to_le_bytes());
        c[20..24].copy_from_slice(&512u32.to_le_bytes());
        c
    }
    pub fn start(&self) -> Result<Lane> {
        let file = self.file.lock().unwrap_or_else(|e| e.into_inner()).take().ok_or("disk lane already started")?;
        #[cfg(feature = "testing")]
        if let Some(control) = self.control.lock().unwrap_or_else(|e| e.into_inner()).clone() {
            return Lane::spawn(super::testing::Controlled { native: Native(file), control, phase: 0 });
        }
        Lane::start(file)
    }
}

pub(super) enum Job { Read(u64, usize), Write(u64, Vec<u8>, bool), Flush }
pub(super) struct Done { pub data: Vec<u8>, pub error: Option<String> }
/// The spawned closure owns no VM state. A fatal timeout detaches it until the syscall retires
/// or the per-VM CLI process exits. Embedders must treat that error as worker-process fatal.
pub(super) struct Lane { tx: Option<SyncSender<Job>>, rx: Receiver<Done>, thread: Option<JoinHandle<()>> }
impl Lane {
    fn start(file: File) -> Result<Self> { Self::spawn(Native(file)) }
    fn spawn(mut native: impl Calls + Send + 'static) -> Result<Self> {
        let (tx, work) = mpsc::sync_channel(1);
        let (result, rx) = mpsc::sync_channel(1);
        let thread = std::thread::Builder::new().name("natemu-disk".into()).spawn(move || {
            while let Ok(job) = work.recv() {
                let done = execute(&mut native, job);
                // Exactly one job in flight; a result slot was reserved before dispatch.
                if result.try_send(done).is_err() { break; }
            }
        }).map_err(|e| format!("disk lane: {e}"))?;
        Ok(Self { tx: Some(tx), rx, thread: Some(thread) })
    }
    pub fn submit(&mut self, job: Job) -> Result<()> {
        self.tx.as_ref().ok_or("closed disk lane")?.try_send(job).map_err(|e| format!("disk lane submission: {e}"))
    }
    pub fn wait(&mut self, canceled: impl Fn() -> bool) -> Result<Done> {
        let start = Instant::now();
        loop {
            match self.rx.recv_timeout(Duration::from_millis(10)) {
                Ok(done) => return Ok(done),
                Err(mpsc::RecvTimeoutError::Disconnected) => return Err("disk lane disconnected".into()),
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
            if canceled() || start.elapsed() > Duration::from_secs(10) {
                self.tx.take(); // eventual native completion cannot enqueue more work
                return Err("disk operation abandoned: VM worker must terminate; file may have partial effects".into());
            }
        }
    }
    pub fn finish(mut self) -> Result<()> {
        // Called only with no outstanding operation, after receiving the final sync result.
        self.tx.take();
        self.thread.take().unwrap().join().map_err(|_| "disk lane panicked".into())
    }
}
pub(super) trait Calls {
    fn read(&mut self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize>;
    fn write(&mut self, bytes: &[u8], offset: u64) -> std::io::Result<usize>;
    fn sync(&mut self) -> std::io::Result<()>;
}
pub(super) struct Native(File);
impl Calls for Native {
    fn read(&mut self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize> {
        unsafe extern "C" { fn pread(fd: i32, bytes: *mut u8, count: usize, offset: i64) -> isize; }
        let n = unsafe { pread(self.0.as_raw_fd(), bytes.as_mut_ptr(), bytes.len(), offset as i64) };
        if n < 0 { Err(std::io::Error::last_os_error()) } else { Ok(n as usize) }
    }
    fn write(&mut self, bytes: &[u8], offset: u64) -> std::io::Result<usize> {
        unsafe extern "C" { fn pwrite(fd: i32, bytes: *const u8, count: usize, offset: i64) -> isize; }
        let n = unsafe { pwrite(self.0.as_raw_fd(), bytes.as_ptr(), bytes.len(), offset as i64) };
        if n < 0 { Err(std::io::Error::last_os_error()) } else { Ok(n as usize) }
    }
    fn sync(&mut self) -> std::io::Result<()> {
        unsafe extern "C" { fn fcntl(fd: i32, command: i32, ...) -> i32; }
        if unsafe { fcntl(self.0.as_raw_fd(), 51) } == 0 { Ok(()) } else { Err(std::io::Error::last_os_error()) }
    }
}
fn execute(native: &mut impl Calls, job: Job) -> Done {
    let (offset, mut data, write, stable) = match job {
        Job::Read(o, n) => (o, vec![0; n], false, false),
        Job::Write(o, b, stable) => (o, b, true, stable),
        Job::Flush => return Done { data: Vec::new(), error: native.sync().err().map(|e| e.to_string()) },
    };
    let mut done = 0;
    let mut error = None;
    while done < data.len() {
        let result = if write { native.write(&data[done..], offset + done as u64) }
            else { native.read(&mut data[done..], offset + done as u64) };
        match result {
            Ok(n) if n > 0 && n <= data.len() - done => done += n,
            Ok(_) => { error = Some("native disk call made zero or invalid progress".into()); break; }
            Err(e) => { error = Some(e.to_string()); break; } // including EINTR: never replay uncertain effects
        }
    }
    if error.is_none() && write && stable { error = native.sync().err().map(|e| e.to_string()); }
    if write || error.is_some() { data.clear(); }
    Done { data, error }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Extent { pub gpa: u64, pub len: usize }
/// Descriptor boundaries do not define header/status boundaries; slice the logical byte streams.
fn slice(extents: &[Extent], mut offset: usize, mut len: usize) -> Vec<Extent> {
    let mut result = Vec::new();
    for e in extents {
        let skip = offset.min(e.len); offset -= skip;
        let take = (e.len - skip).min(len);
        if take != 0 { result.push(Extent { gpa: e.gpa + skip as u64, len: take }); len -= take; }
    }
    assert_eq!(len, 0);
    result
}
fn length(e: &[Extent]) -> usize { e.iter().map(|e| e.len).sum() }
fn data_valid(e: &[Extent]) -> bool { e.len() <= 32 && e.iter().all(|e| e.len <= 4096) && length(e) <= MAX_DATA }
pub(super) struct Streams { pub readable: Vec<Extent>, pub writable: Vec<Extent>, pub status: u64 }
impl Streams {
    pub fn new(chain: &[Descriptor]) -> Result<Self> {
        let (mut readable, mut writable) = (Vec::new(), Vec::new());
        let mut write = false;
        for d in chain {
            if !d.is_write_only() && write { return Err("block readable descriptor follows writable descriptor".into()); }
            write |= d.is_write_only();
            if d.len() != 0 {
                let e = Extent { gpa: d.addr().0, len: d.len() as usize };
                if write { writable.push(e); } else { readable.push(e); }
            }
        }
        let last = writable.last().ok_or("block chain has no status destination")?;
        let status = last.gpa + last.len as u64 - 1;
        Ok(Self { readable, writable, status })
    }
    pub fn readable_len(&self) -> usize { length(&self.readable) }
    pub fn decode(&self, bytes: &[u8], capacity: u64, features: u64) -> Command {
        let bad = || Command::Status(IOERR);
        if bytes.len() < 16 { return bad(); }
        let kind = u32::from_le_bytes(bytes[..4].try_into().unwrap());
        let sector = u64::from_le_bytes(bytes[8..16].try_into().unwrap());
        let writable = length(&self.writable) - 1;
        match kind {
            0 | 1 => {
                let data = if kind == 0 { slice(&self.writable, 0, writable) } else { slice(&self.readable, 16, bytes.len() - 16) };
                let n = length(&data);
                let Some(offset) = sector.checked_mul(512) else { return bad(); };
                if !data_valid(&data) || n % 512 != 0 || offset.checked_add(n as u64).is_none_or(|end| end > capacity)
                    || kind == 0 && bytes.len() != 16 { return bad(); }
                if kind == 0 { Command::Read(offset, n) } else { Command::Write(offset, bytes[16..].to_vec(), features & FLUSH == 0) }
            }
            4 if features & FLUSH != 0 => if bytes.len() == 16 { Command::Flush } else { bad() },
            8 => if bytes.len() == 16 && writable >= 20 { Command::Id } else { bad() },
            _ => Command::Status(UNSUPP),
        }
    }
    /// Writes are ordered: payload, tail status, then transport completion. Only the contiguous
    /// initialized prefix counts, even when a valid GET_ID buffer has a gap before status.
    pub fn completion<'a>(&self, data: &'a [u8], status: &'a [u8; 1], writes: &mut Vec<Transfer<'a>>) -> u32 {
        let mut offset = 0;
        for e in slice(&self.writable, 0, data.len()) {
            writes.push(Transfer::Write { gpa: e.gpa, bytes: &data[offset..offset + e.len] }); offset += e.len;
        }
        writes.push(Transfer::Write { gpa: self.status, bytes: status });
        (data.len() + usize::from(data.len() == length(&self.writable) - 1)) as u32
    }
}
pub(super) enum Command { Read(u64, usize), Write(u64, Vec<u8>, bool), Flush, Id, Status(u8) }

impl Virtio {
    pub(super) fn block_request(&self, backing: &Backing, machine: &Machine, stops: &Stops, q: Queue, generation: Identity,
        head: u16, chain: &[Descriptor], disk: &Disk, lane: &mut Lane, cancel: &AtomicBool) -> Result<Request> {
        let streams = match Streams::new(chain) { Ok(s) => s, Err(e) => return Ok(Request::Invalid(e)) };
        let command = if streams.readable_len() > MAX_DATA + 16 { Command::Status(IOERR) } else {
            let mut bytes = vec![0; streams.readable_len()];
            let mut tail = bytes.as_mut_slice();
            let mut reads = Vec::with_capacity(streams.readable.len());
            for e in &streams.readable {
                let (part, rest) = tail.split_at_mut(e.len); tail = rest;
                reads.push(Transfer::Read { gpa: e.gpa, bytes: part });
            }
            if !self.copy(backing, machine, stops, generation, &mut reads)? { return Ok(Request::Canceled); }
            streams.decode(&bytes, disk.capacity, self.lock().features)
        };
        let inbound = matches!(command, Command::Read(..));
        let (data, status) = match command {
            Command::Id => (disk.id.to_vec(), 0),
            Command::Status(status) => (Vec::new(), status),
            cmd => {
                let job = match cmd {
                    Command::Read(o, n) => Job::Read(o, n),
                    Command::Write(o, bytes, stable) => Job::Write(o, bytes, stable),
                    Command::Flush => Job::Flush,
                    _ => unreachable!(),
                };
                {
                    let s = self.lock();
                    if !s.current(generation) { return Ok(Request::Canceled); }
                    // Submission and reset admission are ordered, but no native call holds this lock.
                    lane.submit(job)?;
                }
                let done = lane.wait(|| cancel.load(SeqCst) || self.lock().stopped)?;
                if let Some(e) = done.error { eprintln!("virtio-block: {e}"); (Vec::new(), IOERR) }
                else { (done.data, 0) }
            }
        };
        let status = [status];
        let mut writes = Vec::with_capacity(streams.writable.len() + 4);
        let mut len = streams.completion(&data, &status, &mut writes);
        if inbound && status[0] != 0 { len = 0; }
        let mut used = [0; 8];
        used[..4].copy_from_slice(&u32::from(head).to_le_bytes()); used[4..].copy_from_slice(&len.to_le_bytes());
        let index = q.next_used.wrapping_add(1).to_le_bytes();
        let mut avail = [0; 4];
        writes.push(Transfer::Write { gpa: q.used + 4 + u64::from(q.next_used % q.size) * 8, bytes: &used });
        writes.push(Transfer::Write { gpa: q.used + 2, bytes: &index });
        // Observe suppression and further work after publication, including aliased completion writes.
        writes.push(Transfer::Read { gpa: q.avail, bytes: &mut avail });
        if !self.copy(backing, machine, stops, generation, &mut writes)? { return Ok(Request::Canceled); }
        #[cfg(feature = "testing")]
        self.observe("commit");
        let mut s = self.lock();
        if s.current(generation) && u16::from_le_bytes(avail[..2].try_into().unwrap()) & 1 == 0 {
            s.interrupt |= 1; self.irq(&s);
        }
        Ok(Request::Committed { pending: u16::from_le_bytes(avail[2..].try_into().unwrap()) != q.next_avail.wrapping_add(1) })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    struct Script { returns: VecDeque<std::io::Result<usize>>, calls: Vec<(u64, usize)>, syncs: usize, fail_sync: bool }
    impl Calls for Script {
        fn read(&mut self, b: &mut [u8], o: u64) -> std::io::Result<usize> { b.fill(0xa5); self.write(b, o) }
        fn write(&mut self, b: &[u8], o: u64) -> std::io::Result<usize> { self.calls.push((o, b.len())); self.returns.pop_front().unwrap() }
        fn sync(&mut self) -> std::io::Result<()> { self.syncs += 1; if self.fail_sync { Err(std::io::Error::other("sync failed")) } else { Ok(()) } }
    }
    fn script(returns: Vec<std::io::Result<usize>>) -> Script { Script { returns: returns.into(), calls: vec![], syncs: 0, fail_sync: false } }
    #[test]
    fn canceled_blocked_lane_retains_only_owned_resources_until_it_retires() {
        struct Blocked { started: mpsc::Sender<()>, release: Receiver<()>, retired: mpsc::Sender<()> }
        impl Drop for Blocked { fn drop(&mut self) { let _ = self.retired.send(()); } }
        impl Calls for Blocked {
            fn read(&mut self, b: &mut [u8], _: u64) -> std::io::Result<usize> {
                self.started.send(()).unwrap(); self.release.recv().unwrap(); Ok(b.len())
            }
            fn write(&mut self, _: &[u8], _: u64) -> std::io::Result<usize> { unreachable!() }
            fn sync(&mut self) -> std::io::Result<()> { Ok(()) }
        }
        let (started, wait) = mpsc::channel(); let (release, blocked) = mpsc::channel(); let (retired, ended) = mpsc::channel();
        let mut lane = Lane::spawn(Blocked { started, release: blocked, retired }).unwrap();
        lane.submit(Job::Read(0, 512)).unwrap(); wait.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(lane.wait(|| true).is_err()); drop(lane);
        assert!(ended.try_recv().is_err()); // cancellation did not free a native call's storage
        release.send(()).unwrap(); ended.recv_timeout(Duration::from_secs(1)).unwrap(); // no blocked completion send
    }
    #[test]
    fn quiesce_requires_successful_final_sync_and_lane_retirement() {
        for failed in [false, true] {
            let device = super::super::tests::device();
            let mut native = script(vec![]); native.fail_sync = failed;
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
        assert!(done.error.is_none()); assert!(done.data.is_empty());
        assert_eq!(native.calls, [(512, 8), (514, 6), (517, 3)]); assert_eq!(native.syncs, 1);
    }
    #[test]
    fn zero_eintr_eio_and_overcount_never_retry_or_publish_partial_reads() {
        for error in [Ok(0), Err(std::io::Error::from_raw_os_error(4)), Err(std::io::Error::from_raw_os_error(5)), Ok(99)] {
            let mut native = script(vec![Ok(2), error]);
            let done = execute(&mut native, Job::Read(0, 8));
            assert!(done.error.is_some()); assert!(done.data.is_empty());
            assert_eq!(native.calls, [(0, 8), (2, 6)]);
        }
    }
    #[test]
    fn flush_and_stable_write_errors_are_reported_without_replay() {
        let mut native = script(vec![Ok(8)]); native.fail_sync = true;
        assert!(execute(&mut native, Job::Write(0, vec![0; 8], true)).error.is_some());
        assert_eq!(native.calls.len(), 1); assert_eq!(native.syncs, 1);
        assert!(execute(&mut native, Job::Flush).error.is_some()); assert_eq!(native.syncs, 2);
        let mut native = script(vec![Ok(8)]);
        assert!(execute(&mut native, Job::Write(0, vec![0; 8], false)).error.is_none()); assert_eq!(native.syncs, 0);
    }
    fn desc(gpa: u64, len: u32, write: bool) -> Descriptor { Descriptor::new(gpa, len, if write { 2 } else { 0 }, 0) }
    fn header(kind: u32, sector: u64) -> Vec<u8> {
        let mut b = vec![0; 16]; b[..4].copy_from_slice(&kind.to_le_bytes()); b[8..].copy_from_slice(&sector.to_le_bytes()); b
    }
    #[test]
    fn split_header_and_combined_payload_status_follow_byte_streams() {
        let s = Streams::new(&[desc(100, 7, false), desc(900, 9 + 512, false), desc(5000, 1, true)]).unwrap();
        let mut bytes = header(1, 2); bytes.extend(vec![0xa5; 512]);
        match s.decode(&bytes, 4096, FEATURES) { Command::Write(1024, b, false) => assert_eq!(b, vec![0xa5; 512]), _ => panic!("OUT layout refused") }
        let s = Streams::new(&[desc(100, 16, false), desc(900, 513, true)]).unwrap();
        assert_eq!(s.status, 1412);
        assert!(matches!(s.decode(&header(0, 0), 4096, FEATURES), Command::Read(0, 512)));
        let bytes = vec![5; 512]; let status = [0]; let mut transfers = Vec::new();
        assert_eq!(s.completion(&bytes, &status, &mut transfers), 513);
        assert_eq!(transfers.len(), 2);
    }
    #[test]
    fn status_tail_gaps_never_inflate_used_prefix_and_errors_write_no_data() {
        let s = Streams::new(&[desc(0, 16, false), desc(100, 32, true)]).unwrap();
        assert!(matches!(s.decode(&header(8, 0), 4096, FEATURES), Command::Id));
        let id = [1; 20]; let status = [0]; let mut writes = Vec::new();
        assert_eq!(s.completion(&id, &status, &mut writes), 20);
        let mut writes = Vec::new(); let status = [IOERR];
        assert_eq!(s.completion(&[], &status, &mut writes), 0); assert_eq!(writes.len(), 1);
        assert!(Streams::new(&[desc(0, 1, true), desc(0, 16, false)]).is_err());
        assert!(Streams::new(&[desc(0, 16, false)]).is_err());
    }
    #[test]
    fn block_limits_features_and_offsets_are_checked_before_native_effects() {
        let s = Streams::new(&[desc(0, 16, false), desc(100, 8193, true)]).unwrap();
        assert!(matches!(s.decode(&header(0, 0), 65536, FEATURES), Command::Status(IOERR)));
        let s = Streams::new(&[desc(0, 16, false), desc(100, 513, true)]).unwrap();
        for sector in [8, u64::MAX] { assert!(matches!(s.decode(&header(0, sector), 4096, FEATURES), Command::Status(IOERR))); }
        assert!(matches!(s.decode(&header(4, 0), 4096, FEATURES & !FLUSH), Command::Status(UNSUPP)));
        assert!(matches!(s.decode(&header(123, 0), 4096, FEATURES), Command::Status(UNSUPP)));
    }
}
