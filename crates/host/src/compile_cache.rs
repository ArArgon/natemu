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
//! Layout: `DIR/{k,u}-*.pack`, immutable files written whole under a temporary name and renamed (atomic, so concurrent
//! processes only ever see complete packs): a magic, the payloads, then an index (key, offset, length, sum per entry), its
//! count and a magic. `k` packs hold blocks at upper-half (kernel) RIPs, `u` packs the others; a pack's name carries the first
//! 8 bytes of its fexc identity (`k-IDENTITY-PID-NANOS-N.pack`; a name without one is another build's). A process reads the
//! index of every pack of its own identity at start; a writer thread hashes its new entries (each key once, off the
//! compile path) into one pending pack per half, written when it reaches [`PACK_BYTES`], when its oldest entry is [`PACK_AGE`] old (a run killed by a signal loses
//! at most that), and when the cache is dropped. Cap: at start, once the total exceeds 3/4 of the cap, other
//! builds' packs go (they can never hit), then the oldest `u` packs, then the oldest `k` ones, until it is at most 3/4 of the
//! cap (kernel code is the same every boot; user code at randomized addresses rarely is), so every run has a quarter of the
//! cap to write into; during a run, nothing more is written once the total reaches the cap.
//! Configuration: [`Config`], from the environment by [`Config::from_env`]: `NATEMU_COMPILE_CACHE` = a directory or `off`
//! (default `~/Library/Caches/natemu/fexc`), `NATEMU_COMPILE_CACHE_MB` = the cap (default 512). Only the boot entry opens it
//! ([`shared`]: `natemu`, `natemu-host boot-linux`); the test drivers never do.

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, Weak};

const HEAD: &[u8; 8] = b"NFXPACK1";
const TAIL: &[u8; 8] = b"NFXPEND1";
/// One index record: key, offset, length, sum.
const RECORD: usize = 32 + 8 + 4 + 32;
/// A pending pack is written once its payloads reach this size, or its oldest entry this age.
pub(crate) const PACK_BYTES: usize = 4 << 20;
pub(crate) const PACK_AGE: std::time::Duration = std::time::Duration::from_secs(2);

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be,
    0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa,
    0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85,
    0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f,
    0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
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
            w[i] = w[i - 16].wrapping_add(s0).wrapping_add(w[i - 7]).wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = *h;
        for i in 0..64 {
            let t1 = hh.wrapping_add(e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25)).wrapping_add((e & f) ^ (!e & g)).wrapping_add(K[i]).wrapping_add(w[i]);
            let t2 = (a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22)).wrapping_add((a & b) ^ (a & c) ^ (b & c));
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
            let word = |i: usize| vreinterpretq_u32_u8(vrev32q_u8(vld1q_u8(b.as_ptr().add(16 * i))));
            let mut m = [word(0), word(1), word(2), word(3)];
            for i in 0..16 {
                let wk = vaddq_u32(m[i % 4], vld1q_u32(K.as_ptr().add(4 * i)));
                if i < 12 {
                    m[i % 4] = vsha256su1q_u32(vsha256su0q_u32(m[i % 4], m[(i + 1) % 4]), m[(i + 2) % 4], m[(i + 3) % 4]);
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
    let compress: fn(&mut [u32; 8], &[u8]) = if hw { |h, b| unsafe { compress_sha2(h, b) } } else { compress_portable };
    sha256_with(parts, compress)
}

fn sha256_with(parts: &[&[u8]], compress: fn(&mut [u32; 8], &[u8])) -> [u8; 32] {
    let mut h: [u32; 8] = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
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

struct Slot {
    pack: usize,
    offset: u64,
    len: u32,
    sum: [u8; 32],
}
/// One half's pending pack.
#[derive(Default)]
struct Pack {
    data: Vec<u8>,
    index: Vec<([u8; 32], u64, u32, [u8; 32])>,
    since: Option<std::time::Instant>,
}
/// The writer thread's state: new entries go to disk off the compile path.
struct Writer {
    dir: PathBuf,
    identity: [u8; 32],
    cap: u64,
    /// Kernel half, user half.
    packs: [Pack; 2],
    /// Keys stored by this process (vCPUs compile the same blocks).
    stored: HashSet<[u8; 32]>,
    /// Bytes on disk: the packs kept at start and those written since.
    bytes: u64,
    written: u32,
}
/// A new entry: request body, kernel half, payload.
type Entry = (Vec<u8>, bool, Vec<u8>);

pub struct DiskCache {
    identity: [u8; 32],
    packs: Vec<File>,
    index: HashMap<[u8; 32], Slot>,
    writer: Mutex<Option<std::sync::mpsc::Sender<Entry>>>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
    /// Bytes of the packs kept at start (after eviction), and their entries.
    pub(crate) bytes: u64,
    pub(crate) entries: usize,
}

/// The part of a pack's name that says which fexc build wrote it.
fn tag(identity: &[u8; 32]) -> String {
    format!("{:016x}", u64::from_be_bytes(identity[..8].try_into().unwrap()))
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
        for e in fs::read_dir(dir).map_err(|e| format!("{}: {e}", dir.display()))?.flatten() {
            let (path, name) = (e.path(), e.file_name().to_string_lossy().into_owned());
            if let Some(pid) = name.strip_suffix(".tmp").and_then(|n| n.split('-').next()).and_then(|p| p.parse::<i32>().ok()) {
                if !alive(pid) {
                    let _ = fs::remove_file(&path); // a pack its writer never finished
                }
            } else if name.ends_with(".pack") {
                if let Ok(m) = e.metadata() {
                    let own = name.get(2..).is_some_and(|n| n.strip_prefix(&tag).is_some_and(|n| n.starts_with('-')));
                    packs.push((own, name.starts_with("k-"), m.modified().unwrap_or(std::time::UNIX_EPOCH), m.len(), path));
                }
            }
        }
        // Other builds' packs, then user packs, then kernel ones, each oldest first: the eviction order.
        packs.sort();
        let mut total: u64 = packs.iter().map(|p| p.3).sum();
        // Down to 3/4 of the cap as soon as the total is above it, not only above the cap: a cache left just under the cap
        // would let nothing more be written (see `Writer::put`) and never be evicted.
        // ponytail: oldest-written first within a half (no access times); an LRU needs a write per hit.
        while total > cap / 4 * 3 && !packs.is_empty() {
            let (_, _, _, size, path) = packs.remove(0);
            let _ = fs::remove_file(path);
            total -= size;
        }
        let (mut files, mut index, mut bytes) = (vec![], HashMap::new(), 0);
        for (own, _, _, size, path) in packs {
            if !own {
                bytes += size; // on disk and counted, but its keys are another build's: not read
                continue;
            }
            match File::open(&path).ok().and_then(|f| read_index(&f, size).map(|i| (f, i))) {
                Some((file, records)) => {
                    let pack = files.len();
                    files.push(file);
                    bytes += size;
                    for (key, offset, len, sum) in records {
                        index.insert(key, Slot { pack, offset, len, sum });
                    }
                }
                None => {
                    let _ = fs::remove_file(path); // complete by construction, so damaged
                }
            }
        }
        let mut w = Writer { dir: dir.into(), identity, cap, packs: Default::default(), stored: HashSet::new(), bytes, written: 0 };
        let (tx, rx) = std::sync::mpsc::channel::<Entry>();
        let thread = std::thread::Builder::new()
            .name("compile-cache".into())
            .spawn(move || loop {
                match rx.recv_timeout(PACK_AGE) {
                    Ok((body, kernel, payload)) => w.put(key(&w.identity, &body), kernel, &payload),
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                    Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                        w.write(0);
                        w.write(1);
                        return;
                    }
                }
                for half in 0..2 {
                    if w.packs[half].since.is_some_and(|t| t.elapsed() >= PACK_AGE) {
                        w.write(half);
                    }
                }
            })
            .map_err(|e| format!("compile cache writer: {e}"))?;
        let entries = index.len();
        Ok(Self { identity, packs: files, index, writer: Mutex::new(Some(tx)), thread: Mutex::new(Some(thread)), bytes, entries })
    }

    /// The key of request `body`.
    pub(crate) fn key(&self, body: &[u8]) -> [u8; 32] {
        key(&self.identity, body)
    }
    /// Whether nothing was found at start (a cold cache: [`DiskCache::get`] always misses).
    pub(crate) fn is_empty(&self) -> bool {
        self.index.is_empty()
    }

    /// The payload stored under `key`, if any and intact.
    pub(crate) fn get(&self, key: &[u8; 32]) -> Option<Vec<u8>> {
        let slot = self.index.get(key)?;
        let mut payload = vec![0; slot.len as usize];
        self.packs[slot.pack].read_exact_at(&mut payload, slot.offset).ok()?;
        (sha256(&[key, &payload]) == slot.sum).then_some(payload)
    }

    /// Stores `payload` for request `body` (once per process; in the kernel half's pack when `kernel`), on the writer thread.
    pub(crate) fn put(&self, body: Vec<u8>, kernel: bool, payload: Vec<u8>) {
        if let Some(tx) = self.writer.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
            let _ = tx.send((body, kernel, payload));
        }
    }
}

impl Writer {
    fn put(&mut self, key: [u8; 32], kernel: bool, payload: &[u8]) {
        // Each pending pack as it will be written (framing and index records included), so the total stays within the cap.
        let size = |k: &Pack| (HEAD.len() + k.data.len() + RECORD * k.index.len() + 16) as u64;
        let pending: u64 = self.packs.iter().map(size).sum();
        if self.bytes + pending + (payload.len() + RECORD + HEAD.len() + 16) as u64 > self.cap || !self.stored.insert(key) {
            return;
        }
        let half = usize::from(!kernel);
        let k = &mut self.packs[half];
        let offset = (HEAD.len() + k.data.len()) as u64;
        k.data.extend_from_slice(payload);
        k.index.push((key, offset, payload.len() as u32, sha256(&[&key, payload])));
        k.since.get_or_insert_with(std::time::Instant::now);
        if k.data.len() >= PACK_BYTES {
            self.write(half);
        }
    }

    /// Writes half `half`'s pending entries as one pack: a temporary file, renamed.
    fn write(&mut self, half: usize) {
        let p = &mut self.packs[half];
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
        let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_nanos());
        let name = format!("{}-{nanos}-{}", std::process::id(), self.written);
        let tmp = self.dir.join(format!("{name}.tmp"));
        let pack = self.dir.join(format!("{}-{}-{name}.pack", ["k", "u"][half], tag(&self.identity)));
        if fs::write(&tmp, &f).and_then(|_| fs::rename(&tmp, pack)).is_err() {
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
        drop(self.writer.get_mut().unwrap_or_else(|e| e.into_inner()).take());
        if let Some(t) = self.thread.get_mut().unwrap_or_else(|e| e.into_inner()).take() {
            let _ = t.join();
        }
        crate::perf::mark("disk_written");
    }
}

/// A pack's index, if its framing is intact.
fn read_index(f: &File, size: u64) -> Option<Vec<([u8; 32], u64, u32, [u8; 32])>> {
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
            let (offset, len) = (u64::from_le_bytes(r[32..40].try_into().unwrap()), u32::from_le_bytes(r[40..44].try_into().unwrap()));
            (offset >= HEAD.len() as u64 && offset.checked_add(u64::from(len)).is_some_and(|end| end <= start))
                .then(|| (r[..32].try_into().unwrap(), offset, len, r[44..].try_into().unwrap()))
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
    /// The configuration from the environment (callers opt in): `NATEMU_COMPILE_CACHE` = a directory or `off` (default
    /// `~/Library/Caches/natemu/fexc`), `NATEMU_COMPILE_CACHE_MB` = the cap (default 512; anything but a whole number of
    /// MiB is an error, not the default).
    pub fn from_env() -> Result<Self, String> {
        let dir = match std::env::var_os("NATEMU_COMPILE_CACHE") {
            Some(v) if v == "off" => None,
            Some(v) => Some(PathBuf::from(v)),
            None => std::env::var_os("HOME").map(|h| PathBuf::from(h).join("Library/Caches/natemu/fexc")),
        };
        let cap = match std::env::var_os("NATEMU_COMPILE_CACHE_MB") {
            None => 512 << 20,
            Some(v) => v.to_str().and_then(|v| v.parse::<u64>().ok()).and_then(|mb| mb.checked_mul(1 << 20)).ok_or_else(|| {
                format!("NATEMU_COMPILE_CACHE_MB={}: a whole number of MiB", v.to_string_lossy())
            })?,
        };
        Ok(Self { dir, cap })
    }
}

/// The process's cache for answers of `fexc` (one, shared by every vCPU's compiler; written out when the last drops), or
/// `None` when `config` has no directory or it cannot be opened (the run goes on without it, with a warning). While one
/// is open, a request for another fexc, directory or cap is an error (the open one would answer it).
pub fn shared(fexc: &Path, config: &Config) -> Result<Option<Arc<DiskCache>>, String> {
    static SHARED: Mutex<(Weak<DiskCache>, Option<(PathBuf, PathBuf, u64)>)> = Mutex::new((Weak::new(), None));
    let Some(dir) = &config.dir else { return Ok(None) };
    let (wanted, cap) = ((fexc.to_path_buf(), dir.clone(), config.cap), config.cap);
    let mut shared = SHARED.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(cache) = shared.0.upgrade() {
        return match &shared.1 {
            Some(open) if *open == wanted => Ok(Some(cache)),
            open => Err(format!("compile cache {open:?} is open in this process; another one ({wanted:?}) cannot be")),
        };
    }
    match DiskCache::open(dir, fexc, cap) {
        Ok(cache) => {
            crate::perf::mark("disk_open");
            eprintln!("compile cache: {} ({} entries, {:.1} MiB, cap {} MiB)", dir.display(), cache.entries, cache.bytes as f64 / 1048576.0, cap >> 20);
            let cache = Arc::new(cache);
            *shared = (Arc::downgrade(&cache), Some(wanted));
            Ok(Some(cache))
        }
        Err(e) => {
            eprintln!("compile cache off: {e}");
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
        assert_eq!(hex(sha256(&[])), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
        assert_eq!(hex(sha256(&[b"abc"])), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        let long = b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
        assert_eq!(hex(sha256(&[long])), "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1");
        assert_eq!(sha256(&[&long[..3], &long[3..50], &long[50..]]), sha256(&[long])); // split anywhere
        assert_eq!(hex(sha256(&[&[b'a'; 1000][..]; 1000])), "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
    }

    /// The Armv8 instructions and the portable code agree at every length to 600 bytes and across part boundaries.
    #[test]
    fn sha256_instructions_match_portable() {
        if !std::arch::is_aarch64_feature_detected!("sha2") {
            return;
        }
        let data: Vec<u8> = (0..1200u32).map(|i| (i.wrapping_mul(2654435761) >> 13) as u8).collect();
        let hw: fn(&mut [u32; 8], &[u8]) = |h, b| unsafe { compress_sha2(h, b) };
        for n in 0..=600 {
            assert_eq!(sha256_with(&[&data[..n]], hw), sha256_with(&[&data[..n]], compress_portable), "{n}");
            let (a, b) = data[..n].split_at(n / 3);
            assert_eq!(sha256_with(&[a, b, &data[600..600 + n % 97]], hw), sha256_with(&[a, b, &data[600..600 + n % 97]], compress_portable));
        }
    }

    /// Entries survive a reopen; a damaged entry or pack is a miss; another fexc build sees nothing; each key is stored
    /// once; the cap stops writing during a run and evicts user packs before kernel ones at the next open.
    #[test]
    fn packs_round_trip() {
        let dir = std::env::temp_dir().join(format!("natemu-cc-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let (fexc, other) = (dir.join("fexc"), dir.join("other"));
        fs::write(&fexc, b"build 1").unwrap();
        fs::write(&other, b"build 2").unwrap();
        let store = dir.join("store");
        let packs = || fs::read_dir(&store).unwrap().flatten().filter(|e| e.file_name().to_string_lossy().ends_with(".pack")).count();
        {
            let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
            assert!(c.is_empty());
            c.put(b"request 1".to_vec(), true, b"answer 1".to_vec());
            c.put(b"request 1".to_vec(), true, b"answer 1 again".to_vec()); // a second vCPU's copy: not stored
            c.put(b"request 2".to_vec(), false, b"answer 2".to_vec());
            assert_eq!(c.get(&c.key(b"request 1")), None); // pending until written
        }
        assert_eq!(packs(), 2); // one per half
        let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
        let (k1, k2, k3) = (c.key(b"request 1"), c.key(b"request 2"), c.key(b"request 3"));
        assert_eq!((c.get(&k1), c.get(&k2), c.get(&k3), c.entries), (Some(b"answer 1".to_vec()), Some(b"answer 2".to_vec()), None, 2));
        let o = DiskCache::open(&store, &other, 1 << 30).unwrap();
        assert_eq!(o.get(&o.key(b"request 1")), None);
        let pack = fs::read_dir(&store).unwrap().flatten().find(|e| e.file_name().to_string_lossy().starts_with("k-")).unwrap().path();
        let mut bytes = fs::read(&pack).unwrap();
        bytes[HEAD.len()] ^= 1; // the payload
        fs::write(&pack, &bytes).unwrap();
        let c = DiskCache::open(&store, &fexc, 1 << 30).unwrap();
        assert_eq!((c.get(&k1), c.get(&k2)), (None, Some(b"answer 2".to_vec())));
        fs::write(&pack, &bytes[..bytes.len() - 1]).unwrap(); // truncated: the pack is dropped
        assert_eq!(DiskCache::open(&store, &fexc, 1 << 30).unwrap().entries, 1);
        assert!(!pack.exists());
        fs::remove_dir_all(&store).unwrap();
        // Cap 3 packs' worth: the fourth is not written. At an open over the cap user packs go first.
        let cap = 3 * (PACK_BYTES as u64 + 4096);
        {
            let c = DiskCache::open(&store, &fexc, cap).unwrap();
            for i in 0..4u8 {
                c.put(vec![i], i % 2 == 0, vec![i; PACK_BYTES]);
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
        }
        assert_eq!(packs(), 3);
        let c = DiskCache::open(&store, &fexc, 2 * PACK_BYTES as u64 + 8192).unwrap(); // over: down to 3/4 of it
        assert_eq!([0u8, 1, 2].map(|i| c.get(&c.key(&[i])).is_some()), [false, false, true]); // the user pack, then the older kernel one
        fs::remove_dir_all(&dir).unwrap();
    }

    /// A directory of small packs: `put`s of `n` payloads of 1000 bytes by build `fexc`, one pack per half, each batch its own open.
    fn fill(store: &Path, fexc: &Path, n: u8) {
        let c = DiskCache::open(store, fexc, 1 << 30).unwrap();
        for i in 0..n {
            c.put(vec![i], i % 2 == 0, vec![i; 1000]);
        }
        drop(c);
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    fn dir_bytes(store: &Path) -> u64 {
        fs::read_dir(store).unwrap().flatten().map(|e| e.metadata().unwrap().len()).sum()
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
            c.put(b"new request".to_vec(), true, vec![9; 1000]);
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
        fill(&store, &fexc, 2); // oldest: a kernel and a user pack of this build
        fill(&store, &other, 2); // newest: the same of the other
        let cap = dir_bytes(&store) * 2 / 3 + 64; // 3/4 of it is just over half: the two other packs go, no more
        let c = DiskCache::open(&store, &fexc, cap).unwrap();
        assert_eq!(c.entries, 2);
        assert_eq!(fs::read_dir(&store).unwrap().count(), 2);
        assert!(c.get(&c.key(&[0])).is_some() && c.get(&c.key(&[1])).is_some());
        fs::remove_dir_all(&dir).unwrap();
    }
}
