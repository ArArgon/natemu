//! The persistent compile cache (P3): fexc's raw answers on disk, shared by runs and by concurrent natemu processes.
//!
//! An entry is keyed by SHA-256 over the fexc binary's own SHA-256 (its build: FEX pin, patch series, shim, fexc.cpp) and
//! the request body (arguments: profile, RIP, block limit, features, gating; then the guest bytes) with the RIP replaced by
//! its half and page offset (perf chunk 4), and holds the RIP it was compiled at and what fexc answered (code, extent,
//! arguments read, the JSON for an answer without code, the object). A hit is handled exactly as a reply from the server,
//! relocated to the requested RIP when it was compiled at another (`Compiler`, `relocation`): the object goes through
//! admission's mandatory validation like a fresh one, so the cache is
//! no more trusted than fexc's pipe (see `Compiler`). Each entry also carries SHA-256(key, payload): a damaged one is a miss.
//!
//! Only CPL-3 requests use it (perf chunk 27, `Compiler::cpl3`): kernel code moves with KASLR every boot, so its entries
//! rarely hit again, and writing them cost more than the hits saved; user programs are position-independent and the key
//! ignores where they load.
//!
//! Layout: `DIR/*.pack`, immutable files written whole under a temporary name and renamed (atomic, so concurrent
//! processes only ever see complete packs): a magic, the payloads, then an index (key, offset, length, sum per entry), its
//! count and a magic. A pack's name starts with the first 8 bytes of its fexc identity (`IDENTITY-PID-NANOS-N.pack`; any
//! other name, such as the `k-`/`u-` packs of the builds that also cached kernel code, is another build's). A process reads
//! the index of every pack of its own identity at start; a writer thread hashes its new entries (each key once, off the
//! compile path) into one pending pack, written when it reaches [`PACK_BYTES`], when its oldest entry is [`PACK_AGE`] old (a
//! run killed by a signal loses at most that), and when the cache is dropped. Cap: at start, once the total exceeds 3/4 of
//! the cap, other builds' packs go (they can never hit), then this build's least recently used, until it is at most 3/4
//! of the cap, so every run has a quarter of the cap to write into; during a run, nothing more is written once the total
//! reaches the cap. A pack's modification time is when it was last used: written, or hit by a process (the writer thread
//! sets it to the time of the process's first hit in that pack, once per pack and process, off the compile path), so
//! the packs a desktop's sessions keep hitting outlive newer ones nothing hit again.
//!
//! In memory, the index keys an entry by the first 8 bytes of its key and keeps the first 8 bytes of its sum (33 bytes a
//! table slot, against 89 for the whole key and sum; sized once for every pack's entries at open): a hit checks the sum,
//! computed with the lookup's whole key, so a key sharing only those 8 bytes, or a damaged payload, is a miss but for a
//! chance of 2^-64.
//!
//! Configuration: [`Config`], by [`Config::new`] from the entries' `--compile-cache DIR|off` (default
//! `~/Library/Caches/natemu/fexc`) and `--compile-cache-mb N` (the cap; default [`default_cap`], a tenth of the cache
//! volume's free space within 1 to 4 GiB), whose fallbacks are the environment's `NATEMU_COMPILE_CACHE` and
//! `NATEMU_COMPILE_CACHE_MB`. Only the boot entry opens it ([`shared`]: `natemu`, `natemu-host boot-linux`); the test
//! drivers never do. Why these numbers: `docs/restructure/disk-cache-cap-plan.md`.

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::hash::{BuildHasherDefault, Hasher};
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Weak};

const HEAD: &[u8; 8] = b"NFXPACK1";
const TAIL: &[u8; 8] = b"NFXPEND1";
/// One index record: key, offset, length, sum.
const RECORD: usize = 32 + 8 + 4 + 32;
/// A pending pack is written once its payloads reach this size, or its oldest entry this age.
pub(crate) const PACK_BYTES: usize = 4 << 20;
pub(crate) const PACK_AGE: std::time::Duration = std::time::Duration::from_secs(2);

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

/// SHA-256's compression of whole 64-byte `blocks` into `h`, portable.
fn compress_portable(h: &mut [u32; 8], blocks: &[u8]) {
    for b in blocks.chunks_exact(64) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes(b[4 * i..4 * i + 4].try_into().unwrap());
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = *h;
        for i in 0..64 {
            let t1 = hh
                .wrapping_add(e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25))
                .wrapping_add((e & f) ^ (!e & g))
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let t2 = (a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22))
                .wrapping_add((a & b) ^ (a & c) ^ (b & c));
            (hh, g, f, e, d, c, b, a) = (g, f, e, d.wrapping_add(t1), c, b, a, t1.wrapping_add(t2));
        }
        for (x, y) in h.iter_mut().zip([a, b, c, d, e, f, g, hh]) {
            *x = x.wrapping_add(y);
        }
    }
}

/// The same with the Armv8 SHA-256 instructions (FEAT_SHA256), four rounds per `sha256h`/`sha256h2` pair.
#[target_feature(enable = "sha2")]
unsafe fn compress_sha2(h: &mut [u32; 8], blocks: &[u8]) {
    use std::arch::aarch64::*;
    unsafe {
        let (mut abcd, mut efgh) = (vld1q_u32(h.as_ptr()), vld1q_u32(h.as_ptr().add(4)));
        for b in blocks.chunks_exact(64) {
            let (abcd0, efgh0) = (abcd, efgh);
            let word =
                |i: usize| vreinterpretq_u32_u8(vrev32q_u8(vld1q_u8(b.as_ptr().add(16 * i))));
            let mut m = [word(0), word(1), word(2), word(3)];
            for i in 0..16 {
                let wk = vaddq_u32(m[i % 4], vld1q_u32(K.as_ptr().add(4 * i)));
                if i < 12 {
                    m[i % 4] = vsha256su1q_u32(
                        vsha256su0q_u32(m[i % 4], m[(i + 1) % 4]),
                        m[(i + 2) % 4],
                        m[(i + 3) % 4],
                    );
                }
                let a = abcd;
                abcd = vsha256hq_u32(abcd, efgh, wk);
                efgh = vsha256h2q_u32(efgh, a, wk);
            }
            (abcd, efgh) = (vaddq_u32(abcd, abcd0), vaddq_u32(efgh, efgh0));
        }
        vst1q_u32(h.as_mut_ptr(), abcd);
        vst1q_u32(h.as_mut_ptr().add(4), efgh);
    }
}

/// SHA-256 (FIPS 180-4) of the concatenated `parts`, with the Armv8 instructions when the CPU has them.
pub(crate) fn sha256(parts: &[&[u8]]) -> [u8; 32] {
    static SHA2: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    let hw = *SHA2.get_or_init(|| std::arch::is_aarch64_feature_detected!("sha2"));
    let compress: fn(&mut [u32; 8], &[u8]) = if hw {
        |h, b| unsafe { compress_sha2(h, b) }
    } else {
        compress_portable
    };
    sha256_with(parts, compress)
}

fn sha256_with(parts: &[&[u8]], compress: fn(&mut [u32; 8], &[u8])) -> [u8; 32] {
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let (mut buf, mut len) = ([0u8; 128], 0u64);
    let mut used = 0;
    for p in parts {
        len += p.len() as u64;
        let mut p = *p;
        if used > 0 {
            let n = p.len().min(64 - used);
            buf[used..used + n].copy_from_slice(&p[..n]);
            (used, p) = (used + n, &p[n..]);
            if used < 64 {
                continue;
            }
            compress(&mut h, &buf[..64]);
        }
        let whole = p.len() / 64 * 64;
        compress(&mut h, &p[..whole]);
        buf[..p.len() - whole].copy_from_slice(&p[whole..]);
        used = p.len() - whole;
    }
    buf[used] = 0x80;
    let end = if used < 56 { 64 } else { 128 };
    buf[used + 1..end].fill(0);
    buf[end - 8..end].copy_from_slice(&(len * 8).to_be_bytes());
    compress(&mut h, &buf[..end]);
    let mut out = [0; 32];
    for (o, x) in out.chunks_exact_mut(4).zip(h) {
        o.copy_from_slice(&x.to_be_bytes());
    }
    out
}

/// An entry in the in-memory index: the first 8 bytes of its sum, and where its payload is.
struct Slot {
    sum: u64,
    pack: u32,
    offset: u32,
    len: u32,
}
/// The index's key: the first 8 bytes of an entry's key.
fn prefix(bytes: &[u8; 32]) -> u64 {
    u64::from_le_bytes(bytes[..8].try_into().unwrap())
}
/// Hashes the index's keys as they are: they are bytes of SHA-256, already uniform.
#[derive(Default)]
struct Uniform(u64);
impl Hasher for Uniform {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 = self.0.rotate_left(8) ^ u64::from(b);
        }
    }
    fn write_u64(&mut self, x: u64) {
        self.0 = x;
    }
}
type Index = HashMap<u64, Slot, BuildHasherDefault<Uniform>>;
/// The pending pack.
#[derive(Default)]
struct Pack {
    data: Vec<u8>,
    index: Vec<([u8; 32], u64, u32, [u8; 32])>,
    since: Option<std::time::Instant>,
}
/// The writer thread's state: new entries go to disk off the compile path, and hits to the packs' times.
struct Writer {
    dir: PathBuf,
    identity: [u8; 32],
    cap: u64,
    /// The packs read at start (the hits' stamps).
    packs: Arc<Vec<File>>,
    pack: Pack,
    /// Keys stored by this process (vCPUs compile the same blocks).
    stored: HashSet<[u8; 32]>,
    /// Bytes on disk: the packs kept at start and those written since.
    bytes: u64,
    written: u32,
}
/// What the writer thread is sent: a new entry (what it is stored under, payload), or the first hit in a pack read at start.
enum Msg {
    Put(Stored, Vec<u8>),
    Hit(u32),
}
/// What a new entry is stored under: the key the lookup that missed computed ([`DiskCache::key`]), or the request body when no
/// lookup was made (a cold cache: the writer thread hashes it).
pub(crate) enum Stored {
    Key([u8; 32]),
    Body(Vec<u8>),
}

pub struct DiskCache {
    identity: [u8; 32],
    packs: Arc<Vec<File>>,
    /// Per pack, whether this process has hit it (its time is set once).
    hit: Vec<AtomicBool>,
    index: Index,
    writer: Mutex<Option<std::sync::mpsc::Sender<Msg>>>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
    /// Bytes of the packs kept at start (after eviction), and their entries.
    #[allow(dead_code)] // read only by the tests (eviction and entry counts) since the open line went
    pub(crate) bytes: u64,
    #[allow(dead_code)]
    pub(crate) entries: usize,
}

/// The part of a pack's name that says which fexc build wrote it.
fn tag(identity: &[u8; 32]) -> String {
    format!(
        "{:016x}",
        u64::from_be_bytes(identity[..8].try_into().unwrap())
    )
}

fn key(identity: &[u8; 32], body: &[u8]) -> [u8; 32] {
    sha256(&[b"natemu-fexc-cache-2\0", identity, body])
}

impl DiskCache {
    /// Opens (creating) the cache in `dir` for answers of the fexc binary at `fexc`, evicting down to `cap` bytes.
    pub(crate) fn open(dir: &Path, fexc: &Path, cap: u64) -> Result<Self, String> {
        let identity = sha256(&[&fs::read(fexc).map_err(|e| format!("{}: {e}", fexc.display()))?]);
        fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        let (mut packs, tag) = (vec![], tag(&identity));
        for e in fs::read_dir(dir)
            .map_err(|e| format!("{}: {e}", dir.display()))?
            .flatten()
        {
            let (path, name) = (e.path(), e.file_name().to_string_lossy().into_owned());
            if let Some(pid) = name
                .strip_suffix(".tmp")
                .and_then(|n| n.split('-').next())
                .and_then(|p| p.parse::<i32>().ok())
            {
                if !alive(pid) {
                    let _ = fs::remove_file(&path); // a pack its writer never finished
                }
            } else if name.ends_with(".pack") {
                if let Ok(m) = e.metadata() {
                    let own = name.strip_prefix(&tag).is_some_and(|n| n.starts_with('-'));
                    packs.push((
                        own,
                        m.modified().unwrap_or(std::time::UNIX_EPOCH),
                        m.len(),
                        path,
                    ));
                }
            }
        }
        // Other builds' packs, then this build's, each least recently used first (a pack's time is its last write or
        // hit): the eviction order.
        packs.sort();
        let mut total: u64 = packs.iter().map(|p| p.2).sum();
        // Down to 3/4 of the cap as soon as the total is above it, not only above the cap: a cache left just under the cap
        // would let nothing more be written (see `Writer::put`) and never be evicted.
        while total > cap / 4 * 3 && !packs.is_empty() {
            let (_, _, size, path) = packs.remove(0);
            let _ = fs::remove_file(path);
            total -= size;
        }
        let mut bytes = 0;
        // Each kept pack of this build, open, with its entry count: the index is sized for all of them at once.
        let mut opened = vec![];
        for (own, _, size, path) in packs {
            bytes += size; // on disk and counted, read or not
            // Another build's pack is not read (its keys are another build's), nor is one that cannot be opened (out of
            // descriptors, say): it is left alone.
            if let Some(file) = own.then(|| File::open(&path).ok()).flatten() {
                opened.push((file, size, path));
            }
        }
        let mut index = Index::with_capacity_and_hasher(
            opened.iter().map(|(f, size, _)| count(f, *size).unwrap_or(0) as usize).sum(),
            Default::default(),
        );
        let mut files = Vec::with_capacity(opened.len());
        for (file, size, path) in opened {
            let Some(records) = read_index(&file, size) else {
                let _ = fs::remove_file(path); // complete by construction, so damaged
                bytes -= size;
                continue;
            };
            let pack = files.len() as u32;
            files.push(file);
            for (key, offset, len, sum) in records {
                index.insert(
                    prefix(&key),
                    Slot {
                        sum: prefix(&sum),
                        pack,
                        offset,
                        len,
                    },
                );
            }
        }
        let files = Arc::new(files);
        let mut w = Writer {
            dir: dir.into(),
            identity,
            cap,
            packs: files.clone(),
            pack: Pack::default(),
            stored: HashSet::new(),
            bytes,
            written: 0,
        };
        let (tx, rx) = std::sync::mpsc::channel::<Msg>();
        let thread = std::thread::Builder::new()
            .name("compile-cache".into())
            .spawn(move || {
                loop {
                    match rx.recv_timeout(PACK_AGE) {
                        Ok(Msg::Put(Stored::Key(k), payload)) => w.put(k, &payload),
                        Ok(Msg::Put(Stored::Body(body), payload)) => {
                            w.put(key(&w.identity, &body), &payload)
                        }
                        // The pack's last use, for the eviction order (a failure only costs it its place).
                        Ok(Msg::Hit(pack)) => {
                            let _ = w.packs[pack as usize].set_modified(std::time::SystemTime::now());
                        }
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                        Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => return w.write(),
                    }
                    if w.pack.since.is_some_and(|t| t.elapsed() >= PACK_AGE) {
                        w.write();
                    }
                }
            })
            .map_err(|e| format!("compile cache writer: {e}"))?;
        let entries = index.len();
        Ok(Self {
            identity,
            hit: (0..files.len()).map(|_| AtomicBool::new(false)).collect(),
            packs: files,
            index,
            writer: Mutex::new(Some(tx)),
            thread: Mutex::new(Some(thread)),
            bytes,
            entries,
        })
    }

    /// The key of request `body`.
    pub(crate) fn key(&self, body: &[u8]) -> [u8; 32] {
        key(&self.identity, body)
    }
    /// Whether nothing was found at start (a cold cache: [`DiskCache::get`] always misses).
    pub(crate) fn is_empty(&self) -> bool {
        self.index.is_empty()
    }

    /// The payload stored under `key`, if any and intact. The first hit in a pack has the writer thread stamp it.
    pub(crate) fn get(&self, key: &[u8; 32]) -> Option<Vec<u8>> {
        let slot = self.index.get(&prefix(key))?;
        let mut payload = vec![0; slot.len as usize];
        self.packs[slot.pack as usize]
            .read_exact_at(&mut payload, u64::from(slot.offset))
            .ok()?;
        if prefix(&sha256(&[key, &payload])) != slot.sum {
            return None;
        }
        if !self.hit[slot.pack as usize].swap(true, Ordering::Relaxed) {
            self.send(Msg::Hit(slot.pack));
        }
        Some(payload)
    }

    /// Stores `payload` for a request (once per process), on the writer thread.
    pub(crate) fn put(&self, at: Stored, payload: Vec<u8>) {
        self.send(Msg::Put(at, payload));
    }

    fn send(&self, msg: Msg) {
        if let Some(tx) = self
            .writer
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
        {
            let _ = tx.send(msg);
        }
    }
}

impl Writer {
    fn put(&mut self, key: [u8; 32], payload: &[u8]) {
        // The pending pack as it will be written (framing and index records included), so the total stays within the cap.
        let k = &self.pack;
        let pending = (HEAD.len() + k.data.len() + RECORD * k.index.len() + 16) as u64;
        if self.bytes + pending + (payload.len() + RECORD + HEAD.len() + 16) as u64 > self.cap
            || !self.stored.insert(key)
        {
            return;
        }
        let k = &mut self.pack;
        let offset = (HEAD.len() + k.data.len()) as u64;
        k.data.extend_from_slice(payload);
        k.index
            .push((key, offset, payload.len() as u32, sha256(&[&key, payload])));
        k.since.get_or_insert_with(std::time::Instant::now);
        if k.data.len() >= PACK_BYTES {
            self.write();
        }
    }

    /// Writes the pending entries as one pack: a temporary file, renamed.
    fn write(&mut self) {
        let p = &mut self.pack;
        if p.index.is_empty() {
            return;
        }
        let mut f = Vec::with_capacity(HEAD.len() + p.data.len() + RECORD * p.index.len() + 16);
        f.extend_from_slice(HEAD);
        f.extend_from_slice(&p.data);
        for (key, offset, len, sum) in &p.index {
            f.extend_from_slice(key);
            f.extend_from_slice(&offset.to_le_bytes());
            f.extend_from_slice(&len.to_le_bytes());
            f.extend_from_slice(sum);
        }
        f.extend_from_slice(&(p.index.len() as u64).to_le_bytes());
        f.extend_from_slice(TAIL);
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos());
        let name = format!("{}-{nanos}-{}", std::process::id(), self.written);
        let tmp = self.dir.join(format!("{name}.tmp"));
        let pack = self
            .dir
            .join(format!("{}-{name}.pack", tag(&self.identity)));
        if fs::write(&tmp, &f)
            .and_then(|_| fs::rename(&tmp, pack))
            .is_err()
        {
            let _ = fs::remove_file(&tmp); // a full disk costs the entries, not the run
        }
        *p = Pack::default();
        self.written += 1;
        self.bytes += f.len() as u64;
    }
}

impl Drop for DiskCache {
    /// Ends the writer thread once it has written what is pending.
    fn drop(&mut self) {
        drop(
            self.writer
                .get_mut()
                .unwrap_or_else(|e| e.into_inner())
                .take(),
        );
        if let Some(t) = self
            .thread
            .get_mut()
            .unwrap_or_else(|e| e.into_inner())
            .take()
        {
            let _ = t.join();
        }
        crate::perf::mark("disk_written");
    }
}

/// A pack's entry count as its tail says (a bound for sizing: [`read_index`] checks it).
fn count(f: &File, size: u64) -> Option<u64> {
    let mut end = [0; 16];
    f.read_exact_at(&mut end, size.checked_sub(16)?).ok()?;
    let count = u64::from_le_bytes(end[..8].try_into().unwrap());
    (&end[8..] == TAIL && count <= size / RECORD as u64).then_some(count)
}

/// A pack's index, if its framing is intact (offsets within 4 GiB, as every pack's are: packs are written at 4 MiB).
fn read_index(f: &File, size: u64) -> Option<Vec<([u8; 32], u32, u32, [u8; 32])>> {
    let mut end = [0; 16];
    f.read_exact_at(&mut end, size.checked_sub(16)?).ok()?;
    let count = u64::from_le_bytes(end[..8].try_into().unwrap());
    let records = (count.checked_mul(RECORD as u64)?).checked_add(16)?;
    let start = size.checked_sub(records)?;
    let mut head = [0; 8];
    f.read_exact_at(&mut head, 0).ok()?;
    if &end[8..] != TAIL || &head != HEAD || start < HEAD.len() as u64 {
        return None;
    }
    let mut raw = vec![0; (records - 16) as usize];
    f.read_exact_at(&mut raw, start).ok()?;
    raw.chunks_exact(RECORD)
        .map(|r| {
            let (offset, len) = (
                u64::from_le_bytes(r[32..40].try_into().unwrap()),
                u32::from_le_bytes(r[40..44].try_into().unwrap()),
            );
            let offset = u32::try_from(offset).ok()?;
            (offset >= HEAD.len() as u32
                && u64::from(offset)
                    .checked_add(u64::from(len))
                    .is_some_and(|end| end <= start))
            .then(|| {
                (
                    r[..32].try_into().unwrap(),
                    offset,
                    len,
                    r[44..].try_into().unwrap(),
                )
            })
        })
        .collect()
}

fn alive(pid: i32) -> bool {
    unsafe extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }
    let found = unsafe { kill(pid, 0) } == 0;
    found || std::io::Error::last_os_error().raw_os_error() != Some(3) // ESRCH
}

/// Where the persistent cache lives and its cap in bytes; `dir: None` is no cache ([`Config::OFF`]).
pub struct Config {
    pub dir: Option<PathBuf>,
    pub cap: u64,
}
impl Config {
    pub const OFF: Self = Self { dir: None, cap: 0 };
    /// The configuration callers opt into: `dir` a directory or `off` (default `~/Library/Caches/natemu/fexc`), `mb` the
    /// cap in MiB (default [`default_cap`] of the free space of the volume `dir` is on).
    pub fn new(dir: Option<&std::ffi::OsStr>, mb: Option<u64>) -> Result<Self, String> {
        let dir = match dir {
            Some(v) if v == "off" => None,
            Some(v) => Some(PathBuf::from(v)),
            None => std::env::var_os("HOME")
                .map(|h| PathBuf::from(h).join("Library/Caches/natemu/fexc")),
        };
        let cap = match (mb, &dir) {
            (Some(mb), _) => mb
                .checked_mul(1 << 20)
                .ok_or_else(|| format!("compile cache cap {mb} MiB: too large"))?,
            (None, Some(dir)) => default_cap(free_bytes(dir)),
            (None, None) => 0,
        };
        Ok(Self { dir, cap })
    }
}

/// The default cap's bounds. The ceiling holds a desktop's sessions and apps (a KDE session alone writes 592 MiB before
/// any app starts); the floor keeps that session across opens (3/4 of it, 768 MiB, is kept at each).
pub const CAP_FLOOR: u64 = 1 << 30;
pub const CAP_CEILING: u64 = 4 << 30;
/// The default cap for a cache volume with `free` bytes free (`None`: unknown): a tenth of it within [`CAP_FLOOR`] and
/// [`CAP_CEILING`], rounded down to 256 MiB (so it is the ceiling on any volume with 40 GiB free, and moves in steps).
pub fn default_cap(free: Option<u64>) -> u64 {
    let step = 256 << 20;
    free.map_or(CAP_CEILING, |f| (f / 10).clamp(CAP_FLOOR, CAP_CEILING)) / step * step
}

/// The bytes free to an unprivileged writer on the volume `dir` is on (or would be on: its nearest existing ancestor's).
fn free_bytes(dir: &Path) -> Option<u64> {
    let dir = dir.ancestors().find(|d| d.exists())?;
    let path = std::ffi::CString::new(std::os::unix::ffi::OsStrExt::as_bytes(dir.as_os_str())).ok()?;
    let mut st: libc::statfs = unsafe { std::mem::zeroed() };
    (unsafe { libc::statfs(path.as_ptr(), &mut st) } == 0)
        .then(|| u64::from(st.f_bavail).saturating_mul(u64::from(st.f_bsize)))
}

/// The process's cache for answers of `fexc` (one, shared by every vCPU's compiler; written out when the last drops), or
/// `None` when `config` has no directory or it cannot be opened (the run goes on without it, with a warning). While one
/// is open, a request for another fexc, directory or cap is an error (the open one would answer it).
pub fn shared(fexc: &Path, config: &Config) -> Result<Option<Arc<DiskCache>>, String> {
    static SHARED: Mutex<(Weak<DiskCache>, Option<(PathBuf, PathBuf, u64)>)> =
        Mutex::new((Weak::new(), None));
    let Some(dir) = &config.dir else {
        return Ok(None);
    };
    let (wanted, cap) = ((fexc.to_path_buf(), dir.clone(), config.cap), config.cap);
    let mut shared = SHARED.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(cache) = shared.0.upgrade() {
        return match &shared.1 {
            Some(open) if *open == wanted => Ok(Some(cache)),
            open => Err(format!(
                "compile cache {open:?} is open in this process; another one ({wanted:?}) cannot be"
            )),
        };
    }
    match DiskCache::open(dir, fexc, cap) {
        Ok(cache) => {
            crate::perf::mark("disk_open");
            let cache = Arc::new(cache);
            *shared = (Arc::downgrade(&cache), Some(wanted));
            Ok(Some(cache))
        }
        Err(e) => {
            eprintln!("natemu: code cache: disk off ({e})"); // corrects the startup line, printed before the open
            Ok(None)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_vectors() {
        let hex = |d: [u8; 32]| d.iter().map(|b| format!("{b:02x}")).collect::<String>();
        assert_eq!(
            hex(sha256(&[])),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            hex(sha256(&[b"abc"])),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        let long = b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
        assert_eq!(
            hex(sha256(&[long])),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
        assert_eq!(
            sha256(&[&long[..3], &long[3..50], &long[50..]]),
            sha256(&[long])
        ); // split anywhere
        assert_eq!(
            hex(sha256(&[&[b'a'; 1000][..]; 1000])),
            "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"
        );
    }

    /// The Armv8 instructions and the portable code agree at every length to 600 bytes and across part boundaries.
    #[test]
    fn sha256_instructions_match_portable() {
        if !std::arch::is_aarch64_feature_detected!("sha2") {
            return;
        }
        let data: Vec<u8> = (0..1200u32)
            .map(|i| (i.wrapping_mul(2654435761) >> 13) as u8)
            .collect();
        let hw: fn(&mut [u32; 8], &[u8]) = |h, b| unsafe { compress_sha2(h, b) };
        for n in 0..=600 {
            assert_eq!(
                sha256_with(&[&data[..n]], hw),
                sha256_with(&[&data[..n]], compress_portable),
                "{n}"
            );
            let (a, b) = data[..n].split_at(n / 3);
            assert_eq!(
                sha256_with(&[a, b, &data[600..600 + n % 97]], hw),
                sha256_with(&[a, b, &data[600..600 + n % 97]], compress_portable)
            );
        }
    }

    /// Entries survive a reopen; a damaged entry or pack is a miss; another fexc build sees nothing, nor does a pack named as
    /// the builds that kept kernel and user packs named theirs; each key is stored once; the cap stops writing during a run
    /// and evicts the oldest packs first at the next open.
    #[test]
    fn packs_round_trip() {
        let dir = std::env::temp_dir().join(format!("natemu-cc-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let (fexc, other) = (dir.join("fexc"), dir.join("other"));
        fs::write(&fexc, b"build 1").unwrap();
        fs::write(&other, b"build 2").unwrap();
        let store = dir.join("store");
        let packs = || {
            fs::read_dir(&store)
                .unwrap()
                .flatten()
                .filter(|e| e.file_name().to_string_lossy().ends_with(".pack"))
                .count()
        };
        {
            let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
            assert!(c.is_empty());
            c.put(Stored::Body(b"request 1".to_vec()), b"answer 1".to_vec());
            c.put(Stored::Body(b"request 1".to_vec()), b"answer 1 again".to_vec()); // a second vCPU's copy: not stored
            assert_eq!(c.get(&c.key(b"request 1")), None); // pending until written
        }
        {
            let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
            c.put(Stored::Key(c.key(b"request 2")), b"answer 2".to_vec()); // under the key a lookup made
        }
        assert_eq!(packs(), 2);
        let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
        let (k1, k2, k3) = (
            c.key(b"request 1"),
            c.key(b"request 2"),
            c.key(b"request 3"),
        );
        assert_eq!(
            (c.get(&k1), c.get(&k2), c.get(&k3), c.entries),
            (
                Some(b"answer 1".to_vec()),
                Some(b"answer 2".to_vec()),
                None,
                2
            )
        );
        let o = DiskCache::open(&store, &other, 1 << 30).unwrap();
        assert_eq!(o.get(&o.key(b"request 1")), None);
        let mut pack = fs::read_dir(&store)
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "pack"))
            .collect::<Vec<_>>();
        pack.sort_by_key(|p| {
            !fs::read(p)
                .unwrap()
                .starts_with(&[&HEAD[..], b"answer 1"].concat())
        }); // request 1's first
        let (pack, second) = (pack[0].clone(), pack[1].clone());
        let old = store.join(format!(
            "k-{}",
            second.file_name().unwrap().to_string_lossy()
        ));
        fs::rename(&second, &old).unwrap();
        assert_eq!(DiskCache::open(&store, &fexc, 1 << 30).unwrap().entries, 1); // the old naming: another build's
        fs::rename(&old, &second).unwrap();
        let mut bytes = fs::read(&pack).unwrap();
        bytes[HEAD.len()] ^= 1; // the payload
        fs::write(&pack, &bytes).unwrap();
        let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
        assert_eq!((c.get(&k1), c.get(&k2)), (None, Some(b"answer 2".to_vec())));
        fs::write(&pack, &bytes[..bytes.len() - 1]).unwrap(); // truncated: the pack is dropped
        assert_eq!(DiskCache::open(&store, &fexc, 1 << 30).unwrap().entries, 1);
        assert!(!pack.exists());
        fs::remove_dir_all(&store).unwrap();
        // Cap 3 packs' worth: the fourth is not written. At an open over the cap the oldest go first.
        let cap = 3 * (PACK_BYTES as u64 + 4096);
        {
            let c = DiskCache::open(&store, &fexc, cap).unwrap();
            for i in 0..4u8 {
                c.put(Stored::Body(vec![i]), vec![i; PACK_BYTES]);
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
        }
        assert_eq!(packs(), 3);
        let c = DiskCache::open(&store, &fexc, 2 * PACK_BYTES as u64 + 8192).unwrap(); // over: down to 3/4 of it
        assert_eq!(
            [0u8, 1, 2].map(|i| c.get(&c.key(&[i])).is_some()),
            [false, false, true]
        );
        fs::remove_dir_all(&dir).unwrap();
    }

    /// A small pack: `put`s of `n` payloads of 1000 bytes by build `fexc`, each batch its own open.
    fn fill(store: &Path, fexc: &Path, n: u8) {
        fill_keys(store, fexc, 0..n);
    }
    /// The same for requests `keys` (request `[i]`, payload 1000 bytes of `i`).
    fn fill_keys(store: &Path, fexc: &Path, keys: std::ops::Range<u8>) {
        let c = DiskCache::open(store, fexc, 1 << 30).unwrap();
        for i in keys {
            c.put(Stored::Body(vec![i]), vec![i; 1000]);
        }
        drop(c);
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    fn scratch(name: &str) -> (PathBuf, PathBuf, PathBuf) {
        let dir = std::env::temp_dir().join(format!("natemu-cc-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let fexc = dir.join("fexc");
        fs::write(&fexc, b"build 1").unwrap();
        let store = dir.join("store");
        (dir, fexc, store)
    }
    fn dir_bytes(store: &Path) -> u64 {
        fs::read_dir(store)
            .unwrap()
            .flatten()
            .map(|e| e.metadata().unwrap().len())
            .sum()
    }

    /// A cache left just under the cap (the writer refuses to pass it, and an open evicts only above it) gets room at the next open.
    #[test]
    fn cache_just_under_the_cap_gets_room() {
        let dir = std::env::temp_dir().join(format!("natemu-cc-room-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let fexc = dir.join("fexc");
        fs::write(&fexc, b"build 1").unwrap();
        let store = dir.join("store");
        fill(&store, &fexc, 2);
        let cap = dir_bytes(&store) + 100; // full: another pack does not fit
        {
            let c = DiskCache::open(&store, &fexc, cap).unwrap();
            assert!(c.bytes <= cap / 4 * 3, "{} of {cap}", c.bytes);
            c.put(Stored::Body(b"new request".to_vec()), vec![9; 1000]);
        }
        let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
        assert_eq!(c.get(&c.key(b"new request")), Some(vec![9; 1000]));
        fs::remove_dir_all(&dir).unwrap();
    }

    /// Packs of another fexc build can never hit, so they go before the current build's, even when newer.
    #[test]
    fn other_builds_packs_go_first() {
        let dir = std::env::temp_dir().join(format!("natemu-cc-other-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let (fexc, other) = (dir.join("fexc"), dir.join("other"));
        fs::write(&fexc, b"build 1").unwrap();
        fs::write(&other, b"build 2").unwrap();
        let store = dir.join("store");
        fill(&store, &fexc, 2); // oldest: a pack of this build
        fill(&store, &other, 2); // newest: the same of the other
        let cap = dir_bytes(&store) * 2 / 3 + 64; // 3/4 of it is just over half: the other pack goes, no more
        let c = DiskCache::open(&store, &fexc, cap).unwrap();
        assert_eq!(c.entries, 2);
        assert_eq!(fs::read_dir(&store).unwrap().count(), 1);
        assert!(c.get(&c.key(&[0])).is_some() && c.get(&c.key(&[1])).is_some());
        fs::remove_dir_all(&dir).unwrap();
    }

    /// A pack hit since it was written outlives a newer one nothing hit: eviction is by last use, not by write time.
    #[test]
    fn hit_packs_outlive_newer_untouched_ones() {
        let (dir, fexc, store) = scratch("lru");
        fill_keys(&store, &fexc, 0..2); // older
        fill_keys(&store, &fexc, 10..12); // newer
        {
            let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
            assert_eq!(c.get(&c.key(&[0])), Some(vec![0; 1000])); // the older pack's first hit: stamped
            assert_eq!(c.get(&c.key(&[1])), Some(vec![1; 1000])); // its second: not again
        } // the writer thread has stamped it before the drop returns
        let cap = dir_bytes(&store) * 2 / 3 + 64; // 3/4 of it is just over one pack: one goes
        let c = DiskCache::open(&store, &fexc, cap).unwrap();
        assert_eq!(
            [0u8, 1, 10, 11].map(|i| c.get(&c.key(&[i])).is_some()),
            [true, true, false, false]
        );
        fs::remove_dir_all(&dir).unwrap();
    }

    /// A pack of this build that cannot be opened is kept on disk (and counted), not taken for a damaged one.
    #[test]
    fn unopenable_pack_is_kept() {
        use std::os::unix::fs::PermissionsExt;
        let (dir, fexc, store) = scratch("unopenable");
        fill(&store, &fexc, 2);
        let pack = fs::read_dir(&store).unwrap().flatten().next().unwrap().path();
        fs::set_permissions(&pack, fs::Permissions::from_mode(0)).unwrap();
        let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
        assert_eq!((c.entries, c.bytes), (0, fs::metadata(&pack).unwrap().len()));
        drop(c);
        assert!(pack.exists());
        fs::set_permissions(&pack, fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(DiskCache::open(&store, &fexc, 1 << 30).unwrap().entries, 2);
        fs::remove_dir_all(&dir).unwrap();
    }

    /// The index keys entries by their keys' first 8 bytes: a lookup whose key shares only those is a miss (the sum is
    /// computed with the whole key).
    #[test]
    fn a_key_sharing_the_index_prefix_misses() {
        let (dir, fexc, store) = scratch("prefix");
        fill(&store, &fexc, 1);
        let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
        let mut k = c.key(&[0]);
        assert!(c.get(&k).is_some());
        k[31] ^= 1;
        assert_eq!(c.get(&k), None);
        fs::remove_dir_all(&dir).unwrap();
    }

    /// The default cap: a tenth of the free space, within 1 to 4 GiB, in 256 MiB steps; the ceiling when unknown.
    #[test]
    fn default_cap_bounds() {
        const G: u64 = 1 << 30;
        assert_eq!(default_cap(None), 4 * G);
        assert_eq!(default_cap(Some(0)), G);
        assert_eq!(default_cap(Some(5 * G)), G); // the floor
        assert_eq!(default_cap(Some(20 * G)), 2 * G);
        assert_eq!(default_cap(Some(13 * G)), 5 * G / 4); // 1.3 GiB, down to a step
        assert_eq!(default_cap(Some(40 * G)), 4 * G);
        assert_eq!(default_cap(Some(2000 * G)), 4 * G); // the ceiling
        assert_eq!(default_cap(Some(u64::MAX)), 4 * G);
    }

    /// `--compile-cache-mb` wins over the default; without it the cap is the default of the cache volume's free space, also
    /// for a folder not made yet; `off` has no cap.
    #[test]
    fn config_cap() {
        let (dir, _, store) = scratch("config");
        let os = |p: &Path| p.as_os_str().to_owned();
        let c = Config::new(Some(&os(&store)), Some(64)).unwrap();
        assert_eq!((c.dir.as_deref(), c.cap), (Some(store.as_path()), 64 << 20));
        let free = free_bytes(&store.join("not/made")).unwrap();
        assert!(free > 0 && free < 1 << 50, "{free}");
        let c = Config::new(Some(&os(&store)), None).unwrap();
        assert!(c.cap >= CAP_FLOOR && c.cap <= CAP_CEILING, "{}", c.cap);
        assert_eq!(c.cap, default_cap(Some(free)));
        let off = Config::new(Some(std::ffi::OsStr::new("off")), None).unwrap();
        assert_eq!((off.dir, off.cap), (None, 0));
        assert!(Config::new(None, Some(u64::MAX)).is_err());
        fs::remove_dir_all(&dir).unwrap();
    }
}
