//! The qcow2 backend's tests: the opener's refusals and the backing policy, locks, the write order seen by a recording
//! file layer and simulated crashes, concurrency against a raw image, a differential run against raw with qemu-img,
//! the 64 GiB write-zeroes timing and a real kill. qemu-img (for checks and fixtures) is optional: those parts say
//! "skipped" when it is absent.
use super::*;
use crate::disk::{DiskBackend, open};
use natemu_qcow::file::FileLayer;
use std::{
    collections::HashMap,
    os::unix::fs::FileExt,
    process::Command,
    sync::{Arc, Mutex, atomic::AtomicUsize},
    time::{Duration, Instant},
};

// ---------------------------------------------------------------------------------------------------------------
// Helpers.

const SECTOR: usize = 512;

/// A scratch folder, deleted on drop.
struct Dir(PathBuf);
impl Dir {
    fn new(tag: &str) -> Self {
        static N: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "natemu-qcow-{tag}-{}-{}",
            std::process::id(),
            N.fetch_add(1, SeqCst)
        ));
        std::fs::create_dir_all(&path).unwrap();
        Self(std::fs::canonicalize(&path).unwrap())
    }
    fn at(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
}
impl Drop for Dir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// qemu-img, if installed.
fn qemu_img() -> Option<PathBuf> {
    ["/opt/homebrew/bin/qemu-img", "/usr/local/bin/qemu-img"]
        .iter()
        .map(PathBuf::from)
        .find(|p| p.exists())
}
fn skipped(what: &str) {
    eprintln!("skipped: {what}: qemu-img not found (install qemu to run it)");
}
/// Run qemu-img; panics on a failed start, returns (success, output text).
fn qemu(args: &[&str]) -> (bool, String) {
    let out = Command::new(qemu_img().unwrap())
        .args(args)
        .output()
        .unwrap();
    let text =
        String::from_utf8_lossy(&out.stdout).into_owned() + &String::from_utf8_lossy(&out.stderr);
    (out.status.success(), text)
}
/// `qemu-img check` must report a clean image.
fn check_clean(path: &Path) {
    if qemu_img().is_none() {
        return;
    }
    let (ok, text) = qemu(&["check", "-q", path.to_str().unwrap()]);
    assert!(ok, "qemu-img check {}: {text}", path.display());
}

/// A sector's bytes: `tag` (8 bytes) and the sector number, repeated.
fn sector(tag: u32, kind: u32, s: u64) -> Vec<u8> {
    let mut word = [0u8; 16];
    word[..4].copy_from_slice(&kind.to_le_bytes());
    word[4..8].copy_from_slice(&tag.to_le_bytes());
    word[8..].copy_from_slice(&s.to_le_bytes());
    word.repeat(SECTOR / 16)
}
const BASE: u32 = 0xba5e;
const GUEST: u32 = 0x6e57;
fn base_bytes(sectors: u64) -> Vec<u8> {
    (0..sectors).flat_map(|s| sector(0, BASE, s)).collect()
}

/// A fresh qcow2 v3 image at `path` (header and empty tables, written by the vendored header code), naming `backing`
/// (name, backing-format extension) if given. Its refcounts are built at its first writable open.
fn create(path: &Path, size: u64, bits: u32, backing: Option<(&str, Option<&str>)>) {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(path)
        .unwrap();
    let mut header =
        QcowHeader::create_for_size_path_and_cluster_bits(3, size, backing.map(|b| b.0), bits)
            .unwrap();
    let format = backing.and_then(|b| b.1);
    if format.is_some() {
        header.backing_file_offset = 136;
    }
    header
        .write_to(&ImageFile::new(file.try_clone().unwrap()))
        .unwrap();
    // write_to leaves the file one byte short of its refcount table; a writable open's rebuild fixes that, a
    // read-only open (a backing file) cannot read the table without it.
    let len = file.metadata().unwrap().len();
    file.set_len(len.next_multiple_of(1 << bits)).unwrap();
    if let Some(format) = format {
        // The backing-format extension (0xe2792aca) then the end extension, between the header and the name.
        let mut ext = 0xe279_2acau32.to_be_bytes().to_vec();
        ext.extend((format.len() as u32).to_be_bytes());
        ext.extend(format.as_bytes());
        ext.resize(ext.len().next_multiple_of(8) + 8, 0);
        assert!(112 + ext.len() <= 136);
        file.write_all_at(&ext, 112).unwrap();
    }
}
/// [`create`], then one writable open (refcounts built) and a clean close.
fn create_ready(path: &Path, size: u64, bits: u32, backing: Option<(&Path, Option<&str>)>) {
    create(
        path,
        size,
        bits,
        backing.map(|(b, f)| (b.to_str().unwrap(), f)),
    );
    let image = open(path, false, backing.map(|b| b.0)).unwrap();
    image.backend.sync(true).unwrap();
}
fn error(path: &Path, read_only: bool, backing: Option<&Path>) -> String {
    open(path, read_only, backing).err().expect("refused")
}
fn read(b: &dyn DiskBackend, offset: u64, len: usize) -> Vec<u8> {
    let mut buf = vec![0; len];
    assert_eq!(b.read(&mut buf, offset).unwrap(), len);
    buf
}
fn write(b: &dyn DiskBackend, offset: u64, data: &[u8]) {
    assert_eq!(b.write(data, offset).unwrap(), data.len());
}
fn set_bits(path: &Path, offset: u64, bits: u64) {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    let mut word = [0; 8];
    file.read_exact_at(&mut word, offset).unwrap();
    file.write_all_at(&(u64::from_be_bytes(word) | bits).to_be_bytes(), offset)
        .unwrap();
}
const INCOMPATIBLE: u64 = 72;

/// A tiny deterministic generator (xorshift64*).
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

// ---------------------------------------------------------------------------------------------------------------
// A file layer that records every write, length change, barrier and sync (for the crash tests), with hooks.

#[derive(Clone, Debug)]
enum Op {
    Write(u64, Vec<u8>),
    SetLen(u64),
    Barrier,
    /// A plain fsync: durable across a natemu or macOS crash, but on macOS neither durable nor ordered
    /// across a power loss (the drive cache).
    Fsync,
    /// F_FULLFSYNC: durable and ordered in every model.
    Sync,
}
#[derive(Default)]
struct Log {
    live: Vec<u8>,
    ops: Vec<Op>,
    /// Run (once) inside the next fsync, before it is recorded: what a worker does while a flush's fsync runs.
    on_sync: Option<Box<dyn FnOnce() + Send>>,
    /// Run (once) inside the next punch, before it.
    on_punch: Option<Box<dyn FnOnce() + Send>>,
    fail_punch: bool,
    punches: usize,
    longest_punch: u64,
    fail_writes: bool,
}
#[derive(Clone)]
struct Recording(Arc<Mutex<Log>>);
impl std::fmt::Debug for Recording {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Recording")
    }
}
impl Recording {
    fn new(bytes: Vec<u8>) -> Self {
        Self(Arc::new(Mutex::new(Log {
            live: bytes,
            ..Default::default()
        })))
    }
    fn log(&self) -> std::sync::MutexGuard<'_, Log> {
        self.0.lock().unwrap()
    }
    fn write_op(&self, buf: &[u8], offset: u64) -> io::Result<usize> {
        let mut log = self.log();
        if log.fail_writes {
            return Err(io::Error::from_raw_os_error(libc::EIO));
        }
        let end = offset as usize + buf.len();
        if log.live.len() < end {
            log.live.resize(end, 0);
        }
        log.live[offset as usize..end].copy_from_slice(buf);
        log.ops.push(Op::Write(offset, buf.to_vec()));
        Ok(buf.len())
    }
}
impl FileLayer for Recording {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> io::Result<usize> {
        let log = self.log();
        let start = (offset as usize).min(log.live.len());
        let n = buf.len().min(log.live.len() - start);
        buf[..n].copy_from_slice(&log.live[start..start + n]);
        Ok(n)
    }
    fn write_at(&self, buf: &[u8], offset: u64) -> io::Result<usize> {
        self.write_op(buf, offset)
    }
    fn file_len(&self) -> io::Result<u64> {
        Ok(self.log().live.len() as u64)
    }
    fn set_len(&self, size: u64) -> io::Result<()> {
        let mut log = self.log();
        log.live.resize(size as usize, 0);
        log.ops.push(Op::SetLen(size));
        Ok(())
    }
    fn sync(&self, kind: SyncKind) -> io::Result<()> {
        if kind == SyncKind::Barrier {
            self.log().ops.push(Op::Barrier);
            return Ok(());
        }
        let hook = self.log().on_sync.take();
        if let Some(hook) = hook {
            hook();
        }
        self.log().ops.push(if kind == SyncKind::Full {
            Op::Sync
        } else {
            Op::Fsync
        });
        Ok(())
    }
    fn punch_hole(&self, offset: u64, length: u64) -> io::Result<()> {
        let hook = self.log().on_punch.take();
        if let Some(hook) = hook {
            hook();
        }
        {
            let mut log = self.log();
            log.punches += 1;
            log.longest_punch = log.longest_punch.max(length);
        }
        if self.log().fail_punch {
            return Err(io::Error::from_raw_os_error(libc::EIO));
        }
        self.write_op(&vec![0; length as usize], offset).map(|_| ())
    }
    fn is_writable(&self) -> bool {
        true
    }
    fn try_clone_layer(&self) -> io::Result<Box<dyn FileLayer>> {
        Ok(Box::new(self.clone()))
    }
}

/// The backend on a recording copy of the image at `top` over the raw `base`.
fn recorded(top: &Path, base: Option<&Path>) -> (Arc<Qcow>, Recording) {
    let rec = Recording::new(std::fs::read(top).unwrap());
    let backing =
        base.map(|b| BackingFile::raw(ImageFile::with_path(File::open(b).unwrap(), b)).unwrap());
    let qcow = Qcow::open(ImageFile::from_layer(Box::new(rec.clone())), backing, false).unwrap();
    (Arc::new(qcow), rec)
}

// ---------------------------------------------------------------------------------------------------------------
// Basic behaviour.

#[test]
fn a_qcow2_image_reads_writes_syncs_and_reports_its_offer() {
    let dir = Dir::new("basic");
    let path = dir.at("a.qcow2");
    create_ready(&path, 4 << 20, 16, None);
    let image = open(&path, false, None).unwrap();
    assert!(!image.read_only && image.discard && image.write_zeroes && image.capacity == 4 << 20);
    let id = image.id;
    let b = &*image.backend;
    assert!(read(b, 0, 4 << 20).iter().all(|&x| x == 0));
    write(b, 100 * 512, &[7; 3000]);
    b.sync(false).unwrap();
    assert_eq!(read(b, 100 * 512, 3000), vec![7; 3000]);
    b.write_zeroes(100 * 512, 1000).unwrap();
    assert!(read(b, 100 * 512, 1000).iter().all(|&x| x == 0));
    assert_eq!(read(b, 100 * 512 + 1000, 2000), vec![7; 2000]);
    // A discard frees whole clusters only: the partial ones keep their data.
    write(b, 0, &[9; 1 << 17]);
    b.discard(512, 1 << 17).unwrap();
    assert_eq!(read(b, 0, 1 << 16), vec![9; 1 << 16]);
    assert!(read(b, 1 << 16, 1 << 16).iter().all(|&x| x == 0));
    drop(image);
    let again = open(&path, true, None).unwrap();
    assert!(again.read_only && !again.discard && !again.write_zeroes && again.id == id);
    drop(again);
    check_clean(&path);
}

#[test]
fn writes_survive_a_sync_and_reopen_and_an_exit_without_power_off() {
    let dir = Dir::new("visible");
    let path = dir.at("a.qcow2");
    create_ready(&path, 1 << 20, 16, None);
    let image = open(&path, false, None).unwrap();
    write(&*image.backend, 4096, &[1; 4096]);
    image.backend.sync(false).unwrap();
    write(&*image.backend, 1 << 16, &[2; 512]);
    // No sync and no power-off: the drop writes the tables (no fsync) and leaves the dirty bit set.
    drop(image);
    let mut word = [0; 8];
    File::open(&path)
        .unwrap()
        .read_exact_at(&mut word, INCOMPATIBLE)
        .unwrap();
    assert_eq!(
        u64::from_be_bytes(word) & 1,
        1,
        "dirty bit kept after an unclean exit"
    );
    let image = open(&path, false, None).unwrap();
    assert_eq!(read(&*image.backend, 4096, 4096), vec![1; 4096]);
    assert_eq!(read(&*image.backend, 1 << 16, 512), vec![2; 512]);
    image.backend.sync(true).unwrap();
    drop(image);
    File::open(&path)
        .unwrap()
        .read_exact_at(&mut word, INCOMPATIBLE)
        .unwrap();
    assert_eq!(u64::from_be_bytes(word) & 1, 0, "a power-off clears it");
    check_clean(&path);
}

#[test]
fn a_write_after_a_power_off_sync_sets_the_dirty_bit_again_first() {
    let dir = Dir::new("redirty");
    let path = dir.at("a.qcow2");
    create_ready(&path, 1 << 20, 16, None);
    let (q, rec) = recorded(&path, None);
    q.sync(true).unwrap();
    let dirty = |rec: &Recording| rec.log().live[INCOMPATIBLE as usize + 7] & 1;
    assert_eq!(dirty(&rec), 0);
    let before = rec.log().ops.len();
    q.write(&[5; 512], 0).unwrap();
    assert_eq!(dirty(&rec), 1);
    // The bit's write and its sync come before the guest data.
    let ops = rec.log().ops[before..].to_vec();
    assert!(
        matches!(&ops[0], Op::Write(INCOMPATIBLE, _)) && matches!(ops[1], Op::Sync),
        "{ops:?}"
    );
}

#[test]
fn a_dirty_image_is_rebuilt_only_when_opened_writable() {
    let dir = Dir::new("dirty");
    let path = dir.at("a.qcow2");
    create_ready(&path, 1 << 20, 16, None);
    let image = open(&path, false, None).unwrap();
    write(&*image.backend, 0, &[3; 1 << 16]);
    drop(image);
    let bytes = std::fs::read(&path).unwrap();
    let ro = open(&path, true, None).unwrap();
    assert_eq!(read(&*ro.backend, 0, 1 << 16), vec![3; 1 << 16]);
    drop(ro);
    assert!(
        std::fs::read(&path).unwrap() == bytes,
        "a read-only open wrote the image"
    );
    let rw = open(&path, false, None).unwrap();
    assert_eq!(read(&*rw.backend, 0, 1 << 16), vec![3; 1 << 16]);
    rw.backend.sync(true).unwrap();
    drop(rw);
    check_clean(&path);
}

#[test]
fn a_failed_table_write_fails_later_writes_and_syncs_but_not_reads() {
    let dir = Dir::new("fail");
    let path = dir.at("a.qcow2");
    create_ready(&path, 1 << 20, 16, None);
    let (q, rec) = recorded(&path, None);
    q.write(&[4; 512], 0).unwrap();
    rec.log().fail_writes = true;
    assert!(q.sync(false).is_err());
    rec.log().fail_writes = false;
    for e in [
        q.write(&[1; 512], 4096).unwrap_err(),
        q.sync(false).unwrap_err(),
        q.write_zeroes(0, 512).unwrap_err(),
        q.discard(0, 1 << 16).unwrap_err(),
    ] {
        assert!(e.to_string().contains("earlier qcow2 table write"), "{e}");
    }
    let mut buf = [0; 512];
    q.read(&mut buf, 0).unwrap();
    assert_eq!(buf, [4; 512]);
}

#[test]
fn reads_and_writes_do_not_wait_for_a_flush_fsync() {
    let dir = Dir::new("unlocked");
    let path = dir.at("a.qcow2");
    create_ready(&path, 1 << 20, 16, None);
    let (q, rec) = recorded(&path, None);
    q.write(&[4; 512], 0).unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    let other = q.clone();
    rec.log().on_sync = Some(Box::new(move || {
        // Another worker reads and writes while the fsync runs; with the table lock held it would block.
        std::thread::spawn(move || {
            let mut buf = [0; 512];
            other.read(&mut buf, 0).unwrap();
            other.write(&[6; 512], 1 << 17).unwrap();
            tx.send(buf).unwrap();
        });
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(5))
                .expect("blocked behind the fsync"),
            [4; 512]
        );
    }));
    q.sync(false).unwrap();
}

#[test]
fn a_read_only_image_refuses_changes_and_syncs_without_a_call() {
    let dir = Dir::new("ro");
    let path = dir.at("a.qcow2");
    create_ready(&path, 1 << 20, 16, None);
    let bytes = std::fs::read(&path).unwrap();
    let image = open(&path, true, None).unwrap();
    for e in [
        image.backend.write(&[1; 512], 0).unwrap_err(),
        image.backend.discard(0, 1 << 16).unwrap_err(),
        image.backend.write_zeroes(0, 512).unwrap_err(),
    ] {
        assert_eq!(e.kind(), io::ErrorKind::PermissionDenied);
    }
    image.backend.sync(false).unwrap();
    image.backend.sync(true).unwrap();
    drop(image);
    assert!(std::fs::read(&path).unwrap() == bytes);
}

// ---------------------------------------------------------------------------------------------------------------
// Locks.

#[test]
fn locks_exclusive_for_a_writer_shared_for_readers_and_backing_files() {
    let dir = Dir::new("locks");
    let (base, top, top2) = (
        dir.at("base.raw"),
        dir.at("top.qcow2"),
        dir.at("top2.qcow2"),
    );
    std::fs::write(&base, base_bytes(2048)).unwrap();
    create_ready(&top, 1 << 20, 16, Some((&base, Some("raw"))));
    create_ready(&top2, 1 << 20, 16, Some((&base, Some("raw"))));
    let a = open(&top, false, Some(&base)).unwrap();
    assert!(error(&top, false, Some(&base)).contains("held by another process"));
    assert!(error(&top, true, Some(&base)).contains("held by another process"));
    // Two overlays share one base; a writer of the base is refused while they hold it.
    let b = open(&top2, false, Some(&base)).unwrap();
    assert!(error(&base, false, None).contains("held by another process"));
    assert!(open(&base, true, None).is_ok());
    drop((a, b));
    let r1 = open(&top, true, Some(&base)).unwrap();
    let r2 = open(&top, true, Some(&base)).unwrap();
    assert!(error(&top, false, Some(&base)).contains("held by another process"));
    drop((r1, r2));
    assert!(open(&base, false, None).is_ok());
}

// ---------------------------------------------------------------------------------------------------------------
// The strict opener: hand-made refusals (no qemu-img needed).

#[test]
fn version_2_corrupt_and_unknown_bits_are_refused_as_the_design_says() {
    let dir = Dir::new("strict");
    // Unknown incompatible bit (patched header): refused for reading and writing.
    let unknown = dir.at("unknown.qcow2");
    create_ready(&unknown, 1 << 20, 16, None);
    set_bits(&unknown, INCOMPATIBLE, 1 << 10);
    for ro in [false, true] {
        let e = error(&unknown, ro, None);
        assert!(
            e.contains("unknown.qcow2")
                && e.contains("unknown feature bit 10")
                && !e.contains('\n'),
            "{e}"
        );
    }
    // Corrupt bit: read-only opens, writable is refused with what to do.
    let corrupt = dir.at("corrupt.qcow2");
    create_ready(&corrupt, 1 << 20, 16, None);
    set_bits(&corrupt, INCOMPATIBLE, 2);
    assert!(error(&corrupt, false, None).contains("marked corrupt"));
    assert!(open(&corrupt, true, None).is_ok());
    // A virtual size that is not a multiple of 512.
    let odd = dir.at("odd.qcow2");
    create(&odd, 1000, 16, None);
    assert!(error(&odd, true, None).contains("not a nonzero multiple of 512"));
}

#[test]
fn an_out_of_bounds_l2_entry_sets_the_corrupt_bit_on_read_and_on_write() {
    for writing in [false, true] {
        let dir = Dir::new("oob");
        let path = dir.at("a.qcow2");
        create_ready(&path, 1 << 20, 16, None);
        let image = open(&path, false, None).unwrap();
        write(&*image.backend, 0, &[1; 512]);
        image.backend.sync(true).unwrap();
        drop(image);
        // L1[0] -> the L2 table; point its first entry far past the refcount table's reach.
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .unwrap();
        let mut word = [0; 8];
        file.read_exact_at(&mut word, 40).unwrap();
        let l1 = u64::from_be_bytes(word);
        file.read_exact_at(&mut word, l1).unwrap();
        let l2 = u64::from_be_bytes(word) & 0x00ff_ffff_ffff_fe00;
        file.write_all_at(&((1u64 << 63) | (1 << 50)).to_be_bytes(), l2)
            .unwrap();
        drop(file);
        let image = open(&path, false, None).unwrap();
        let e = if writing {
            image.backend.write(&[2; 512], 0).unwrap_err()
        } else {
            image.backend.read(&mut [0; 512], 0).unwrap_err()
        };
        assert_eq!(e.raw_os_error(), Some(libc::EIO), "{e}");
        drop(image);
        assert!(error(&path, false, None).contains("marked corrupt"));
    }
}

#[test]
fn free_clusters_are_reused_and_none_is_stranded_after_reopen() {
    let dir = Dir::new("free");
    let path = dir.at("a.qcow2");
    create_ready(&path, 8 << 20, 16, None);
    let image = open(&path, false, None).unwrap();
    let b = &*image.backend;
    for i in 0..8u64 {
        write(b, i << 16, &[i as u8 + 1; 1 << 16]);
    }
    b.sync(false).unwrap();
    let grown = std::fs::metadata(&path).unwrap().len();
    b.discard(0, 8 << 16).unwrap();
    b.sync(false).unwrap();
    // Freed by a completed flush: the next allocations take them, the file does not grow.
    for i in 0..8u64 {
        write(b, (16 + i) << 16, &[0x40 + i as u8; 1 << 16]);
    }
    b.sync(false).unwrap();
    // (At most the relocated refcount block's cluster and one L2 move are new.)
    assert!(
        std::fs::metadata(&path).unwrap().len() <= grown + (2 << 16),
        "freed clusters were not reused"
    );
    drop(image);
    let image = open(&path, false, None).unwrap();
    assert_eq!(
        read(&*image.backend, 17 << 16, 1 << 16),
        vec![0x41; 1 << 16]
    );
    image.backend.sync(true).unwrap();
    drop(image);
    // qemu-img counts leaked clusters as an error with -q... it reports them; require none.
    if qemu_img().is_some() {
        let (ok, text) = qemu(&["check", path.to_str().unwrap()]);
        assert!(ok && !text.contains("leaked"), "{text}");
    }
}

#[test]
fn a_failed_punch_is_reported_and_its_cluster_stays_reserved() {
    let dir = Dir::new("punch");
    let path = dir.at("a.qcow2");
    create_ready(&path, 1 << 20, 16, None);
    let (q, rec) = recorded(&path, None);
    q.write(&[1; 1 << 16], 0).unwrap();
    q.sync(false).unwrap();
    rec.log().fail_punch = true;
    assert!(q.discard(0, 1 << 16).is_err());
    rec.log().fail_punch = false;
    q.sync(false).unwrap();
    // The host cluster holding the 1s (the punch failed, so they are still there).
    let at = rec
        .log()
        .live
        .chunks(1 << 16)
        .position(|c| c.iter().all(|&x| x == 1))
        .unwrap();
    for c in 2..6 {
        q.write(&[2; 1 << 16], c << 16).unwrap();
    }
    let live = rec.log().live.clone();
    assert!(
        live[at << 16..][..1 << 16].iter().all(|&x| x == 1),
        "the cluster of the failed punch was handed out again"
    );
    assert!(read(&*q, 0, 1 << 16).iter().all(|&x| x == 0));
}

#[test]
fn a_new_table_never_lands_on_a_cluster_whose_punch_is_pending() {
    // Upstream issue 8624: a flush and an L2 allocation run while a discard's punch is pending.
    let dir = Dir::new("stale");
    let path = dir.at("a.qcow2");
    create_ready(&path, 1 << 30, 16, None);
    let (q, rec) = recorded(&path, None);
    q.write(&[1; 1 << 16], 0).unwrap();
    q.sync(false).unwrap();
    let other = q.clone();
    rec.log().on_punch = Some(Box::new(move || {
        // A flush, then a write 512 MiB away (a new L2 table and data cluster).
        other.sync(false).unwrap();
        other.write(&[7; 1 << 16], 1 << 29).unwrap();
        other.sync(false).unwrap();
    }));
    q.discard(0, 1 << 16).unwrap();
    q.sync(false).unwrap();
    assert_eq!(read(&*q, 1 << 29, 1 << 16), vec![7; 1 << 16]);
    let bytes = rec.log().live.clone();
    drop(q);
    let copy = dir.at("copy.qcow2");
    std::fs::write(&copy, bytes).unwrap();
    let image = open(&copy, false, None).unwrap();
    assert_eq!(read(&*image.backend, 1 << 29, 1 << 16), vec![7; 1 << 16]);
    assert!(read(&*image.backend, 0, 1 << 16).iter().all(|&x| x == 0));
    image.backend.sync(true).unwrap();
    drop(image);
    check_clean(&copy);
}

// ---------------------------------------------------------------------------------------------------------------
// Backing files and the backing policy.

#[test]
fn an_overlay_reads_its_base_until_written_and_past_a_shorter_base_reads_zeros() {
    let dir = Dir::new("over");
    let (base, top) = (dir.at("base.raw"), dir.at("top.qcow2"));
    // The base (100 KiB + 512) is shorter than the overlay (1 MiB): reads past its end are zeros.
    std::fs::write(&base, base_bytes(201)).unwrap();
    create_ready(&top, 1 << 20, 16, Some((&base, None)));
    let image = open(&top, false, Some(&base)).unwrap();
    let b = &*image.backend;
    let want = base_bytes(201);
    assert_eq!(read(b, 0, want.len()), want);
    // A read spanning the base's end.
    let span = read(b, 200 * 512, 4096);
    assert_eq!(&span[..512], &want[200 * 512..]);
    assert!(span[512..].iter().all(|&x| x == 0));
    assert!(read(b, 1 << 19, 4096).iter().all(|&x| x == 0));
    // A sub-cluster write keeps the base around it (copy-on-write); a whole-cluster one replaces it.
    write(b, 1000, &[1; 100]);
    let mut expect = want[..1 << 16].to_vec();
    expect[1000..1100].fill(1);
    assert_eq!(read(b, 0, 1 << 16), expect);
    write(b, 1 << 16, &[2; 1 << 16]);
    assert_eq!(read(b, 1 << 16, 1 << 16), vec![2; 1 << 16]);
    // Zeros and discards over the base read as zeros, not as the base.
    b.write_zeroes(0, 512).unwrap();
    assert!(read(b, 0, 512).iter().all(|&x| x == 0));
    assert_eq!(read(b, 512, 488), expect[512..1000]);
    b.discard(1 << 16, 1 << 16).unwrap();
    assert!(read(b, 1 << 16, 1 << 16).iter().all(|&x| x == 0));
    image.backend.sync(true).unwrap();
    drop(image);
    check_clean(&top);
}

#[test]
fn the_embedded_backing_name_is_followed_only_when_the_caller_names_that_file() {
    let dir = Dir::new("policy");
    let (base, other, top, plain) = (
        dir.at("base.raw"),
        dir.at("other.raw"),
        dir.at("top.qcow2"),
        dir.at("plain.qcow2"),
    );
    std::fs::write(&base, base_bytes(2048)).unwrap();
    std::fs::write(&other, base_bytes(2048)).unwrap();
    create_ready(&top, 1 << 20, 16, Some((&base, Some("raw"))));
    create_ready(&plain, 1 << 20, 16, None);
    // --disk top.qcow2 alone: say what to add.
    let e = error(&top, false, None);
    let b = base.display();
    assert!(
        e.contains(&format!(
            "top.qcow2 is based on {b}; add backing = \"{b}\" to this disk"
        )),
        "{e}"
    );
    // Another file: refused, by identity.
    let e = error(&top, false, Some(&other));
    assert!(
        e.contains(&format!(
            "is based on {b}, not on the backing {}",
            other.display()
        )),
        "{e}"
    );
    // A backing for an image without one, and for a raw image.
    assert!(error(&plain, false, Some(&base)).contains("has no backing file"));
    assert!(error(&other, false, Some(&base)).contains("a backing file needs a qcow2 image"));
    // The same file by another name (a symbolic link, a path with ..) is the same file.
    let link = dir.at("link.raw");
    std::os::unix::fs::symlink(&base, &link).unwrap();
    assert!(open(&top, false, Some(&link)).is_ok());
    std::fs::create_dir(dir.at("sub")).unwrap();
    assert!(open(&top, false, Some(&dir.at("sub/../base.raw"))).is_ok());
}

#[test]
fn a_missing_overlay_is_created_over_its_base_and_an_existing_file_never_overwritten() {
    use std::os::unix::fs::PermissionsExt;
    let dir = Dir::new("create");
    let (base, qbase) = (dir.at("base.raw"), dir.at("base.qcow2"));
    std::fs::write(&base, base_bytes(4096)).unwrap();
    create_ready(&qbase, 3 << 20, 16, None);
    for (b, format, size) in [(&base, "raw", 2 << 20), (&qbase, "qcow2", 3 << 20)] {
        let top = dir.at(&format!("top-{format}.qcow2"));
        // Through a relative-looking path: the header records the canonical absolute one.
        let image = open(
            &top,
            false,
            Some(&dir.at(&format!(
                "sub/../{}",
                b.file_name().unwrap().to_str().unwrap()
            ))),
        );
        assert!(image.is_err(), "sub does not exist");
        assert!(!top.exists(), "nothing left behind");
        let image = open(&top, false, Some(b)).unwrap();
        assert_eq!(image.capacity, size);
        if format == "raw" {
            assert_eq!(read(&*image.backend, 0, 8192), base_bytes(16));
        }
        write(&*image.backend, 4096, &sector(1, GUEST, 8));
        image.backend.sync(true).unwrap();
        drop(image);
        assert_eq!(
            std::fs::metadata(&top).unwrap().permissions().mode() & 0o777,
            0o600
        );
        check_clean(&top);
        if qemu_img().is_some() {
            let (ok, info) = qemu(&["info", top.to_str().unwrap()]);
            assert!(
                ok && info.contains(&format!("backing file: {}", b.display()))
                    && info.contains(&format!("backing file format: {format}")),
                "{info}"
            );
        }
        // Reopened (exists now): the data is there; a read-only disk opens it without creating anything.
        let image = open(&top, true, Some(b)).unwrap();
        assert_eq!(read(&*image.backend, 4096, 512), sector(1, GUEST, 8));
    }
    assert_eq!(
        std::fs::read(&base).unwrap(),
        base_bytes(4096),
        "the base is never written"
    );
    // An existing file is opened, never replaced: a raw one with a backing is refused and left as it was.
    let raw = dir.at("existing.raw");
    std::fs::write(&raw, base_bytes(8)).unwrap();
    assert!(error(&raw, false, Some(&base)).contains("a backing file needs a qcow2 image"));
    assert_eq!(std::fs::read(&raw).unwrap(), base_bytes(8));
    // A read-only overlay is never created; a base of a refused kind makes no overlay.
    let none = dir.at("none.qcow2");
    assert!(error(&none, true, Some(&base)).contains("never created"));
    let vhdx = dir.at("x.vhdx");
    let mut bytes = vec![0u8; 4096];
    bytes[..8].copy_from_slice(b"vhdxfile");
    std::fs::write(&vhdx, bytes).unwrap();
    assert!(error(&none, false, Some(&vhdx)).contains("looks like a VHDX image"));
    assert!(!none.exists());
}

#[test]
fn an_overlay_appears_whole_or_not_at_all_and_one_of_two_creators_wins() {
    let dir = Dir::new("publish");
    let (base, top) = (dir.at("base.raw"), dir.at("top.qcow2"));
    std::fs::write(&base, base_bytes(2048)).unwrap();
    let names = || {
        std::fs::read_dir(&dir.0)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .filter(|n| n.starts_with(".top.qcow2.natemu-new-"))
            .collect::<Vec<_>>()
    };
    // A crash after the temporary file is written: nothing at the real path, a stale temporary name only.
    let temp = write_temp(&top, &base).unwrap();
    assert!(!top.exists() && names().len() == 1, "{:?}", names());
    check_clean(&temp);
    // The real path existing (here: written meanwhile) is never overwritten.
    std::fs::write(&top, b"someone else's").unwrap();
    assert_eq!(
        publish(&temp, &top).unwrap_err().kind(),
        io::ErrorKind::AlreadyExists
    );
    assert_eq!(std::fs::read(&top).unwrap(), b"someone else's");
    std::fs::remove_file(&temp).unwrap();
    std::fs::remove_file(&top).unwrap();
    // Two creators at once: exactly one wins, the other is told; no temporary file is left.
    for _ in 0..20 {
        let gate = std::sync::Barrier::new(2);
        let results: Vec<_> = std::thread::scope(|s| {
            let racers: Vec<_> = (0..2)
                .map(|_| {
                    s.spawn(|| {
                        gate.wait();
                        super::create(&top, &base)
                    })
                })
                .collect();
            racers.into_iter().map(|r| r.join().unwrap()).collect()
        });
        assert_eq!(
            results.iter().filter(|r| r.is_ok()).count(),
            1,
            "{results:?}"
        );
        let e = results.into_iter().find_map(|r| r.err()).unwrap();
        assert!(
            e.contains(&top.display().to_string())
                && e.contains("another launch created this overlay first"),
            "{e}"
        );
        assert!(names().is_empty(), "{:?}", names());
        check_clean(&top);
        std::fs::remove_file(&top).unwrap();
    }
}

#[test]
fn relative_backing_names_resolve_against_the_referring_image_and_missing_ones_are_named() {
    let dir = Dir::new("relative");
    std::fs::create_dir(dir.at("sub")).unwrap();
    let (base, top) = (dir.at("base.raw"), dir.at("sub/top.qcow2"));
    std::fs::write(&base, base_bytes(2048)).unwrap();
    create(&top, 1 << 20, 16, Some(("../base.raw", Some("raw"))));
    let image = open(&top, false, Some(&base)).unwrap();
    assert_eq!(read(&*image.backend, 0, 4096), base_bytes(8));
    drop(image);
    // A header naming a missing file: the error gives the resolved path.
    let gone = dir.at("sub/gone.qcow2");
    create(&gone, 1 << 20, 16, Some(("../missing.raw", Some("raw"))));
    let e = error(&gone, false, Some(&base));
    assert!(
        e.contains(&format!(
            "is based on {}",
            dir.at("sub/../missing.raw").display()
        )),
        "{e}"
    );
}

#[test]
fn the_config_named_base_format_is_its_extension_or_its_magic() {
    let dir = Dir::new("format");
    let (raw_base, q_base, vmdk) = (
        dir.at("base.raw"),
        dir.at("base.qcow2"),
        dir.at("base.vmdk"),
    );
    std::fs::write(&raw_base, base_bytes(2048)).unwrap();
    // A qcow2 base with data, and an overlay on it without a format extension: detected as qcow2.
    create_ready(&q_base, 1 << 20, 16, None);
    let image = open(&q_base, false, None).unwrap();
    write(&*image.backend, 0, &[8; 4096]);
    image.backend.sync(true).unwrap();
    drop(image);
    let top = dir.at("top.qcow2");
    create_ready(&top, 1 << 20, 16, Some((&q_base, None)));
    assert_eq!(
        read(&*open(&top, true, Some(&q_base)).unwrap().backend, 0, 4096),
        vec![8; 4096]
    );
    // The extension wins over the magic: "raw" shows the qcow2 file's bytes.
    let as_raw = dir.at("as-raw.qcow2");
    create(
        &as_raw,
        1 << 20,
        16,
        Some((q_base.to_str().unwrap(), Some("raw"))),
    );
    assert_eq!(
        &read(&*open(&as_raw, true, Some(&q_base)).unwrap().backend, 0, 4)[..],
        b"QFI\xfb"
    );
    // Another container's magic is refused.
    let mut bytes = vec![0; 1 << 16];
    bytes[..4].copy_from_slice(b"KDMV");
    std::fs::write(&vmdk, bytes).unwrap();
    let over_vmdk = dir.at("over-vmdk.qcow2");
    create(
        &over_vmdk,
        1 << 20,
        16,
        Some((vmdk.to_str().unwrap(), None)),
    );
    assert!(error(&over_vmdk, true, Some(&vmdk)).contains("looks like a VMDK image"));
}

#[test]
fn deeper_links_need_a_format_a_regular_file_no_symlink_no_loop_and_depth_16() {
    let dir = Dir::new("deep");
    let raw = dir.at("bottom.raw");
    std::fs::write(&raw, base_bytes(2048)).unwrap();
    // Three layers: bottom.raw <- mid.qcow2 (raw, by extension) <- top.qcow2.
    let (mid, top) = (dir.at("mid.qcow2"), dir.at("top.qcow2"));
    create(&mid, 1 << 20, 16, Some(("bottom.raw", Some("raw"))));
    create(&top, 1 << 20, 16, Some(("mid.qcow2", Some("qcow2"))));
    let image = open(&top, false, Some(&mid)).unwrap();
    assert_eq!(read(&*image.backend, 0, 1 << 16), base_bytes(128));
    write(&*image.backend, 512, &[1; 512]);
    drop(image);
    // A link without a format extension.
    let (mid2, top2) = (dir.at("mid2.qcow2"), dir.at("top2.qcow2"));
    create(&mid2, 1 << 20, 16, Some(("bottom.raw", None)));
    create(&top2, 1 << 20, 16, Some(("mid2.qcow2", Some("qcow2"))));
    let e = error(&top2, true, Some(&mid2));
    assert!(
        e.contains(&format!(
            "backing file {}: named without a backing format",
            raw.display()
        )),
        "{e}"
    );
    // A link that is a symbolic link, one that is not a regular file (a FIFO: opened without blocking), a missing one.
    std::os::unix::fs::symlink(&raw, dir.at("sym.raw")).unwrap();
    let fifo = dir.at("fifo.raw");
    assert_eq!(
        unsafe {
            libc::mkfifo(
                std::ffi::CString::new(fifo.to_str().unwrap())
                    .unwrap()
                    .as_ptr(),
                0o600,
            )
        },
        0
    );
    for (name, why) in [
        ("sym.raw", "a symbolic link"),
        ("fifo.raw", "not a regular file"),
        ("nothing.raw", "No such file"),
    ] {
        let (m, t) = (
            dir.at(&format!("m-{name}.qcow2")),
            dir.at(&format!("t-{name}.qcow2")),
        );
        create(&m, 1 << 20, 16, Some((name, Some("raw"))));
        create(&t, 1 << 20, 16, Some((m.to_str().unwrap(), Some("qcow2"))));
        let e = error(&t, true, Some(&m));
        assert!(
            e.contains(why) && e.contains(&dir.at(name).display().to_string()),
            "{name}: {e}"
        );
    }
    // A loop: a base whose link names the top again, and a base naming itself.
    let (la, lb) = (dir.at("la.qcow2"), dir.at("lb.qcow2"));
    create(&la, 1 << 20, 16, Some(("lb.qcow2", Some("qcow2"))));
    create(&lb, 1 << 20, 16, Some(("la.qcow2", Some("qcow2"))));
    assert!(error(&la, true, Some(&lb)).contains("loops"));
    let selfish = dir.at("self.qcow2");
    create(&selfish, 1 << 20, 16, Some(("self.qcow2", Some("qcow2"))));
    assert!(error(&selfish, true, Some(&selfish)).contains("its own backing file"));
    // Depth: 16 backing files open, 17 are refused.
    let mut below = raw.clone();
    for i in 1..=17 {
        let p = dir.at(&format!("d{i}.qcow2"));
        let format = if i == 1 { "raw" } else { "qcow2" };
        create(
            &p,
            1 << 20,
            16,
            Some((below.file_name().unwrap().to_str().unwrap(), Some(format))),
        );
        below = p;
    }
    let (t16, t17) = (dir.at("t16.qcow2"), dir.at("t17.qcow2"));
    create(&t16, 1 << 20, 16, Some(("d15.qcow2", Some("qcow2"))));
    create(&t17, 1 << 20, 16, Some(("d16.qcow2", Some("qcow2"))));
    assert_eq!(
        read(
            &*open(&t16, true, Some(&dir.at("d15.qcow2")))
                .unwrap()
                .backend,
            0,
            512
        ),
        base_bytes(1)
    );
    assert!(error(&t17, true, Some(&dir.at("d16.qcow2"))).contains("deeper than 16"));
}

#[test]
fn links_below_the_configured_base_stay_inside_the_referring_images_folder() {
    let dir = Dir::new("escape");
    let outside = Dir::new("outside");
    let secret = outside.at("secret.raw");
    std::fs::write(&secret, base_bytes(2048)).unwrap();
    std::fs::create_dir(dir.at("sub")).unwrap();
    std::fs::write(dir.at("sub/inner.raw"), base_bytes(2048)).unwrap();
    std::fs::write(dir.at("bottom.raw"), base_bytes(2048)).unwrap();
    std::os::unix::fs::symlink(&outside.0, dir.at("linkdir")).unwrap();
    let secret_name = secret.to_str().unwrap().to_owned();
    let refused = [
        secret_name.as_str(),    // absolute
        "../outside/secret.raw", // ..
        "sub/../bottom.raw",     // .. inside the folder
        "./bottom.raw",          // .
        "sub//inner.raw",        // an empty component
        "linkdir/secret.raw",    // a symbolic link among the folders, pointing outside
    ];
    let mut let_through = Vec::new();
    for (i, name) in refused.iter().enumerate() {
        let (base, top) = (
            dir.at(&format!("b{i}.qcow2")),
            dir.at(&format!("t{i}.qcow2")),
        );
        create(&base, 1 << 20, 16, Some((name, Some("raw"))));
        create(
            &top,
            1 << 20,
            16,
            Some((base.to_str().unwrap(), Some("qcow2"))),
        );
        let want = format!(
            "b{i}.qcow2 names {name} as its backing file; backing files below the configured base must be \
             relative paths inside b{i}.qcow2's folder"
        );
        if !open(&top, true, Some(&base))
            .err()
            .is_some_and(|e| e.contains(&want))
        {
            let_through.push(*name);
        }
    }
    assert!(let_through.is_empty(), "not refused: {let_through:?}");
    // A plain relative name into a subfolder is followed.
    let (base, top) = (dir.at("ok.qcow2"), dir.at("tok.qcow2"));
    create(&base, 1 << 20, 16, Some(("sub/inner.raw", Some("raw"))));
    create(
        &top,
        1 << 20,
        16,
        Some((base.to_str().unwrap(), Some("qcow2"))),
    );
    assert_eq!(
        read(&*open(&top, true, Some(&base)).unwrap().backend, 0, 512),
        base_bytes(1)
    );
    // The configured base itself may be anywhere (the caller named it): a top over the outside file opens.
    let over = dir.at("over-outside.qcow2");
    create(
        &over,
        1 << 20,
        16,
        Some((secret_name.as_str(), Some("raw"))),
    );
    assert!(open(&over, true, Some(&secret)).is_ok());
}

// ---------------------------------------------------------------------------------------------------------------
// qemu-img fixtures: each refused kind, and the other formats' files.

#[test]
fn qemu_img_fixtures_of_each_refused_kind_are_refused_with_a_clear_message() {
    if qemu_img().is_none() {
        return skipped("qcow2 refusal fixtures");
    }
    let dir = Dir::new("fixtures");
    let p = |n: &str| dir.at(n).to_str().unwrap().to_owned();
    let make = |args: &[&str]| {
        let (ok, text) = qemu(args);
        assert!(ok, "qemu-img {args:?}: {text}");
    };
    make(&[
        "create",
        "-f",
        "qcow2",
        "--object",
        "secret,id=s0,data=natemu",
        "-o",
        "encrypt.format=luks,encrypt.key-secret=s0",
        &p("luks.qcow2"),
        "1M",
    ]);
    make(&[
        "create",
        "-f",
        "qcow2",
        "--object",
        "secret,id=s0,data=natemu",
        "-o",
        "encrypt.format=aes,encrypt.key-secret=s0",
        &p("aes.qcow2"),
        "1M",
    ]);
    make(&[
        "create",
        "-f",
        "qcow2",
        "-o",
        "extended_l2=on",
        &p("ext-l2.qcow2"),
        "1M",
    ]);
    make(&[
        "create",
        "-f",
        "qcow2",
        "-o",
        &format!("data_file={}", p("data.raw")),
        &p("data-file.qcow2"),
        "1M",
    ]);
    make(&[
        "create",
        "-f",
        "qcow2",
        "-o",
        "compression_type=zstd",
        &p("zstd.qcow2"),
        "1M",
    ]);
    for (name, why) in [
        ("luks.qcow2", "encryption method: 2"),
        ("aes.qcow2", "encryption method: 1"),
        ("ext-l2.qcow2", "extended L2"),
        ("data-file.qcow2", "external data file"),
        ("zstd.qcow2", "compression type"),
    ] {
        for ro in [true, false] {
            let e = error(&dir.at(name), ro, None);
            assert!(
                e.contains(name) && e.contains(why) && !e.contains('\n'),
                "{name}: {e}"
            );
        }
    }
    // Version 2: read-only only. Internal snapshots: read-only only.
    make(&[
        "create",
        "-f",
        "qcow2",
        "-o",
        "compat=0.10",
        &p("v2.qcow2"),
        "1M",
    ]);
    assert!(
        error(&dir.at("v2.qcow2"), false, None).contains("version 2 image opens read-only only")
    );
    let v2 = open(&dir.at("v2.qcow2"), true, None).unwrap();
    assert!(v2.read_only && !v2.write_zeroes && !v2.discard);
    assert_eq!(
        v2.backend.write_zeroes(0, 512).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    assert!(read(&*v2.backend, 0, 4096).iter().all(|&x| x == 0));
    drop(v2);
    make(&["create", "-f", "qcow2", &p("snap.qcow2"), "1M"]);
    make(&["snapshot", "-c", "s1", &p("snap.qcow2")]);
    assert!(error(&dir.at("snap.qcow2"), false, None).contains("internal snapshots"));
    assert!(open(&dir.at("snap.qcow2"), true, None).is_ok());
    // Compressed clusters: the image opens, a compressed cluster reads as an I/O error (never as zeros: the check
    // for compression comes before the zero flag), an uncompressed one reads.
    std::fs::write(dir.at("src.raw"), base_bytes(2048)).unwrap();
    make(&[
        "convert",
        "-c",
        "-f",
        "raw",
        "-O",
        "qcow2",
        &p("src.raw"),
        &p("comp.qcow2"),
    ]);
    let c = open(&dir.at("comp.qcow2"), true, None).unwrap();
    assert_eq!(
        c.backend.read(&mut [0; 512], 0).unwrap_err().raw_os_error(),
        Some(libc::EIO)
    );
    drop(c);
    // A copy-on-write into a compressed cluster fails without marking the image corrupt.
    let c = open(&dir.at("comp.qcow2"), false, None).unwrap();
    assert!(c.backend.write(&[1; 512], 0).is_err());
    drop(c);
    assert!(
        open(&dir.at("comp.qcow2"), false, None).is_ok(),
        "the image was marked corrupt"
    );
}

#[test]
fn other_container_formats_made_by_qemu_img_are_refused_by_name() {
    if qemu_img().is_none() {
        return skipped("container format fixtures");
    }
    let dir = Dir::new("formats");
    for (format, opts, name) in [
        ("vmdk", "", "VMDK"),
        ("vhdx", "", "VHDX"),
        ("vdi", "", "VDI"),
        ("vpc", "", "VHD"),
        ("vpc", "subformat=fixed", "VHD"),
    ] {
        let path = dir.at(&format!("x-{format}-{}.img", opts.len()));
        let mut args = vec!["create", "-f", format];
        if !opts.is_empty() {
            args.extend(["-o", opts]);
        }
        args.extend([path.to_str().unwrap(), "4M"]);
        let (ok, text) = qemu(&args);
        assert!(ok, "{text}");
        let e = open(&path, true, None)
            .err()
            .unwrap_or_else(|| panic!("{format} {opts}: opened as raw"));
        assert!(
            e.contains(&format!("looks like a {name} image")),
            "{format} {opts}: {e}"
        );
    }
    // DMG: hdiutil makes a compressed UDIF image (trailer "koly"; a plain read-write one is raw bytes).
    let dmg = dir.at("x.dmg");
    std::fs::create_dir(dir.at("src")).unwrap();
    std::fs::write(dir.at("src/a.txt"), "natemu").unwrap();
    if Command::new("hdiutil")
        .args([
            "create",
            "-srcfolder",
            dir.at("src").to_str().unwrap(),
            "-format",
            "UDZO",
            "-quiet",
        ])
        .arg(&dmg)
        .status()
        .is_ok_and(|s| s.success())
    {
        assert!(error(&dmg, true, None).contains("looks like a DMG image"));
    } else {
        eprintln!("skipped: DMG fixture: hdiutil failed");
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Gate: 64 GiB of WRITE_ZEROES on a fresh overlay, timed, with a reader running.

#[test]
fn write_zeroes_of_64_gib_on_a_fresh_overlay_is_table_only_and_fast() {
    let dir = Dir::new("zeroes");
    let (base, top) = (dir.at("base.raw"), dir.at("top.qcow2"));
    std::fs::write(&base, base_bytes(1 << 12)).unwrap();
    create_ready(&top, 64 << 30, 16, Some((&base, Some("raw"))));
    let image = open(&top, false, Some(&base)).unwrap();
    let b: Arc<dyn DiskBackend> = image.backend.into();
    let stop = Arc::new(AtomicBool::new(false));
    let reader = {
        let (b, stop) = (b.clone(), stop.clone());
        std::thread::spawn(move || {
            let mut worst = Duration::ZERO;
            let mut buf = vec![0; 4096];
            let mut i = 0u64;
            while !stop.load(SeqCst) {
                let t = Instant::now();
                b.read(&mut buf, (i * 7919 % (1 << 24)) << 12).unwrap();
                worst = worst.max(t.elapsed());
                i += 1;
            }
            worst
        })
    };
    let mut slowest = Duration::ZERO;
    let start = Instant::now();
    // 32 ranges of 2 GiB: one request's worth.
    for r in 0..32u64 {
        let t = Instant::now();
        b.write_zeroes(r << 31, 1 << 31).unwrap();
        slowest = slowest.max(t.elapsed());
    }
    let total = start.elapsed();
    let t = Instant::now();
    b.sync(false).unwrap();
    let sync = t.elapsed();
    stop.store(true, SeqCst);
    let worst_read = reader.join().unwrap();
    eprintln!(
        "64 GiB write-zeroes: {total:?} in all, slowest 2 GiB range {slowest:?}, sync {sync:?}, \
         slowest concurrent 4 KiB read {worst_read:?}, image {} bytes",
        std::fs::metadata(&top).unwrap().len()
    );
    assert!(total < Duration::from_secs(5), "{total:?}");
    assert!(
        read(&*b, 0, 4096).iter().all(|&x| x == 0)
            && read(&*b, (64 << 30) - 4096, 4096).iter().all(|&x| x == 0)
    );
    assert!(std::fs::metadata(&top).unwrap().len() < 64 << 20);
}

// ---------------------------------------------------------------------------------------------------------------
// Gate: DISCARD and WRITE_ZEROES over allocated clusters stay inside the call limit.

#[test]
fn a_discard_of_contiguous_clusters_is_a_few_punches() {
    let dir = Dir::new("punches");
    let path = dir.at("a.qcow2");
    create_ready(&path, 256 << 20, 16, None);
    let (q, rec) = recorded(&path, None);
    // 3000 clusters (about 190 MiB) written in order: allocated one after another at the file end.
    for c in 0..3000u64 {
        q.write(&[c as u8 | 1; 512], c << 16).unwrap();
    }
    q.sync(false).unwrap();
    q.discard(0, 3000 << 16).unwrap();
    let (punches, longest) = {
        let log = rec.log();
        (log.punches, log.longest_punch)
    };
    // Merged, but no punch longer than 128 MiB (one punch must not hold a worker long).
    assert!(
        (2..=6).contains(&punches) && longest <= 128 << 20,
        "{punches} punch calls, the longest {longest} bytes, for 3000 contiguous clusters"
    );
    assert!(read(&*q, 0, 3000 << 16).iter().all(|&x| x == 0));
    // The freed clusters are reused after the next flush.
    q.sync(false).unwrap();
    let len = rec.log().live.len();
    for c in 0..64u64 {
        q.write(&[7; 1 << 16], (600 + c) << 16).unwrap();
    }
    assert_eq!(rec.log().live.len(), len, "freed clusters were not reused");
}

#[test]
fn discard_and_write_zeroes_of_4_gib_of_allocated_clusters_are_timed() {
    let dir = Dir::new("bigdiscard");
    let path = dir.at("a.qcow2");
    create_ready(&path, 8 << 30, 16, None);
    let image = open(&path, false, None).unwrap();
    let b = &*image.backend;
    // The first 2 GiB fully written (2 GiB on the host), then one sector in each cluster of the next 2 GiB.
    let full = vec![1u8; 1 << 20];
    for m in 0..2048u64 {
        b.write(&full, m << 20).unwrap();
    }
    for c in (1u64 << 15)..(1 << 16) {
        b.write(&[1; 512], c << 16).unwrap();
    }
    b.sync(false).unwrap();
    let mut slowest = Duration::ZERO;
    for (what, offset, len, zero) in [
        ("discard, 1 GiB fully written", 0u64, 1u64 << 30, false),
        ("write-zeroes, 1 GiB fully written", 1 << 30, 1 << 30, true),
        (
            "discard, 2 GiB of one-sector clusters",
            2 << 30,
            2 << 30,
            false,
        ),
    ] {
        let t = Instant::now();
        if zero {
            b.write_zeroes(offset, len).unwrap();
        } else {
            b.discard(offset, len).unwrap();
        }
        let took = t.elapsed();
        slowest = slowest.max(took);
        eprintln!(
            "{what}: {took:?}, {:.0} ms per GiB",
            took.as_secs_f64() * 1000.0 / (len as f64 / (1u64 << 30) as f64)
        );
    }
    assert!(slowest < Duration::from_secs(3), "{slowest:?}");
    assert!(
        read(b, 0, 1 << 16).iter().all(|&x| x == 0)
            && read(b, 3 << 30, 1 << 16).iter().all(|&x| x == 0)
    );
    b.sync(true).unwrap();
    drop(image);
    check_clean(&path);
}

#[test]
fn an_image_whose_refcount_table_cannot_cover_it_is_refused_for_writing() {
    if qemu_img().is_none() {
        return skipped("refcount table coverage fixture");
    }
    let dir = Dir::new("reftable");
    let (big, small) = (dir.at("big.qcow2"), dir.at("small.qcow2"));
    // qemu-img gives both a one-cluster refcount table: 4 GiB of file at 4 KiB clusters.
    for (path, size) in [(&big, "64G"), (&small, "1G")] {
        let (ok, text) = qemu(&[
            "create",
            "-f",
            "qcow2",
            "-o",
            "cluster_size=4096",
            path.to_str().unwrap(),
            size,
        ]);
        assert!(ok, "{text}");
    }
    let e = error(&big, false, None);
    assert!(
        e.contains("big.qcow2") && e.contains("refcount table covers") && !e.contains('\n'),
        "{e}"
    );
    assert!(open(&big, true, None).is_ok(), "read-only never allocates");
    assert!(open(&small, false, None).is_ok());
}

// ---------------------------------------------------------------------------------------------------------------
// Gate: whole-cluster writes over a backing file racing reads of the same clusters (the deviation from upstream:
// the guest data is the new cluster's fill, so a reader never sees the unwritten cluster).

#[test]
fn whole_cluster_writes_racing_reads_over_a_backing_file_never_show_zeros() {
    const CLUSTERS: u64 = 64;
    const SECTORS: u64 = CLUSTERS * 128;
    let dir = Dir::new("whole");
    let (base, top, raw) = (dir.at("base.raw"), dir.at("top.qcow2"), dir.at("same.raw"));
    std::fs::write(&base, base_bytes(SECTORS)).unwrap();
    for round in 0..8u64 {
        let _ = std::fs::remove_file(&top);
        create_ready(&top, SECTORS * 512, 16, Some((&base, Some("raw"))));
        std::fs::write(&raw, base_bytes(SECTORS)).unwrap();
        let q: Arc<dyn DiskBackend> = open(&top, false, Some(&base)).unwrap().backend.into();
        let stop = Arc::new(AtomicBool::new(false));
        let readers: Vec<_> = (0..4u64)
            .map(|t| {
                let (q, stop) = (q.clone(), stop.clone());
                std::thread::spawn(move || {
                    let mut rng = Rng(0x77 + t + round * 10);
                    let mut reads = 0u64;
                    while !stop.load(SeqCst) {
                        let s = rng.below(SECTORS);
                        let got = read(&*q, s * 512, 512);
                        let tag = u32::from_le_bytes(got[4..8].try_into().unwrap());
                        assert!(
                            got == sector(0, BASE, s) || got == sector(tag, GUEST, s),
                            "round {round} sector {s}: neither base nor new data: {:?}",
                            &got[..16]
                        );
                        reads += 1;
                    }
                    reads
                })
            })
            .collect();
        // 4 writers, each its own clusters, each cluster written whole once (the allocating write).
        let writers: Vec<_> = (0..4u64)
            .map(|t| {
                let q = q.clone();
                std::thread::spawn(move || {
                    for c in (t..CLUSTERS).step_by(4) {
                        let data: Vec<u8> = (c * 128..(c + 1) * 128)
                            .flat_map(|s| sector(c as u32 + 1, GUEST, s))
                            .collect();
                        q.write(&data, c << 16).unwrap();
                    }
                })
            })
            .collect();
        writers.into_iter().for_each(|w| w.join().unwrap());
        stop.store(true, SeqCst);
        let reads: u64 = readers.into_iter().map(|r| r.join().unwrap()).sum();
        assert!(reads > 0);
        let raw_file = OpenOptions::new().write(true).open(&raw).unwrap();
        for c in 0..CLUSTERS {
            let data: Vec<u8> = (c * 128..(c + 1) * 128)
                .flat_map(|s| sector(c as u32 + 1, GUEST, s))
                .collect();
            raw_file.write_all_at(&data, c << 16).unwrap();
        }
        let want = std::fs::read(&raw).unwrap();
        assert!(
            read(&*q, 0, want.len()) == want,
            "round {round}: qcow2 and raw differ"
        );
        q.sync(true).unwrap();
        drop(q);
        check_clean(&top);
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Gate: concurrent sub-cluster writes and reads to the same unallocated clusters over a backing file, with discards
// and flushes, compared against the same operations on a raw image.

/// Thread `t` of `threads` owns the sectors `s` with `s % threads == t`: in the shared region (clusters every thread
/// writes into) and in its own discard region (clusters it alone owns, freed and reallocated).
#[test]
fn concurrent_writers_readers_discards_and_flushes_over_a_backing_file_match_raw() {
    const THREADS: u64 = 8;
    const CLUSTER: u64 = 1 << 16;
    const SHARED: u64 = 16; // clusters
    const OWN: u64 = 4; // clusters per thread
    let sectors = (SHARED + THREADS * OWN) * CLUSTER / 512;
    let dir = Dir::new("concurrent");
    let (base, top, raw) = (dir.at("base.raw"), dir.at("top.qcow2"), dir.at("same.raw"));
    std::fs::write(&base, base_bytes(sectors)).unwrap();
    for round in 0..6u64 {
        // A fresh overlay each round: every first write races to allocate.
        let _ = std::fs::remove_file(&top);
        std::fs::write(&raw, base_bytes(sectors)).unwrap();
        create_ready(&top, sectors * 512, 16, Some((&base, Some("raw"))));
        let image = open(&top, false, Some(&base)).unwrap();
        let q: Arc<dyn DiskBackend> = image.backend.into();
        let flushes = Arc::new(Mutex::new(())); // the device serialises syncs (Sticky)
        let workers: Vec<_> = (0..THREADS)
            .map(|t| {
                let (q, flushes) = (q.clone(), flushes.clone());
                std::thread::spawn(move || {
                    let mut rng = Rng(0x9e37_79b9 ^ (t + 1) * (round + 7));
                    let mut ops = Vec::new(); // (sector, Some(tag) | None = zeros), replayed on raw
                    let own = |rng: &mut Rng| {
                        let region = if rng.below(3) == 0 {
                            SHARED * CLUSTER / 512 + (t * OWN + rng.below(OWN)) * CLUSTER / 512
                        } else {
                            rng.below(SHARED) * CLUSTER / 512
                        };
                        let s = region + rng.below(CLUSTER / 512);
                        s - s % THREADS + t
                    };
                    for i in 0..400u32 {
                        match rng.below(10) {
                            0..5 => {
                                let s = own(&mut rng);
                                let tag = (round as u32) << 24 | (t as u32) << 16 | i;
                                q.write(&sector(tag, GUEST, s), s * 512).unwrap();
                                ops.push((s, Some(tag)));
                            }
                            5..8 => {
                                // Any sector of the shared clusters: base, zeros (a discarded own cluster), or a
                                // write of its owner; never anything else.
                                let s = rng.below(sectors);
                                let got = read(&*q, s * 512, 512);
                                let tagged = got[..4] == GUEST.to_le_bytes()
                                    && got
                                        == sector(
                                            u32::from_le_bytes(got[4..8].try_into().unwrap()),
                                            GUEST,
                                            s,
                                        );
                                let owner = (u32::from_le_bytes(got[4..8].try_into().unwrap())
                                    >> 16
                                    & 0xff) as u64;
                                assert!(
                                    got == sector(0, BASE, s)
                                        || got.iter().all(|&x| x == 0)
                                        || (tagged && owner == s % THREADS),
                                    "sector {s}: {:?}",
                                    &got[..16]
                                );
                            }
                            8 => {
                                let c = SHARED + t * OWN + rng.below(OWN);
                                q.discard(c * CLUSTER, CLUSTER).unwrap();
                                // The whole cluster reads zeros; only this thread writes in it.
                                for s in c * CLUSTER / 512..(c + 1) * CLUSTER / 512 {
                                    ops.push((s, None));
                                }
                            }
                            _ => {
                                let _held = flushes.lock().unwrap();
                                q.sync(false).unwrap();
                            }
                        }
                    }
                    ops
                })
            })
            .collect();
        let ops: Vec<_> = workers
            .into_iter()
            .flat_map(|w| w.join().unwrap())
            .collect();
        // The same operations on raw (each sector has one owner, so their order across threads does not matter).
        let raw_file = OpenOptions::new().write(true).open(&raw).unwrap();
        for (s, tag) in &ops {
            let data = tag.map_or(vec![0; 512], |tag| sector(tag, GUEST, *s));
            raw_file.write_all_at(&data, s * 512).unwrap();
        }
        let want = std::fs::read(&raw).unwrap();
        assert!(
            read(&*q, 0, want.len()) == want,
            "round {round}: qcow2 and raw differ"
        );
        q.sync(true).unwrap();
        drop(q);
        let again = open(&top, true, Some(&base)).unwrap();
        assert!(
            read(&*again.backend, 0, want.len()) == want,
            "round {round}: differ after reopen"
        );
        drop(again);
        check_clean(&top);
    }
    if qemu_img().is_some() {
        let (ok, text) = qemu(&[
            "compare",
            "-F",
            "raw",
            top.to_str().unwrap(),
            raw.to_str().unwrap(),
        ]);
        assert!(ok, "{text}");
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Gate: random write/zero/discard/read sequences on qcow2 and raw; qemu-img check and compare.

#[test]
fn random_sequences_match_raw_and_pass_qemu_img_check_and_compare() {
    if qemu_img().is_none() {
        return skipped("differential test (qemu-img check and compare)");
    }
    for (seed, bits, backed) in [
        (1u64, 16u32, true),
        (2, 16, false),
        (3, 9, true),
        (4, 12, true),
        (5, 9, false),
    ] {
        let dir = Dir::new("diff");
        let cluster = 1u64 << bits;
        let size = 4u64 << 20;
        let (base, top, raw) = (dir.at("base.raw"), dir.at("top.qcow2"), dir.at("same.raw"));
        let start = if backed {
            base_bytes(size / 512)
        } else {
            vec![0; size as usize]
        };
        std::fs::write(&base, &start).unwrap();
        std::fs::write(&raw, &start).unwrap();
        create_ready(&top, size, bits, backed.then_some((&*base, Some("raw"))));
        let (qi, ri) = (
            open(&top, false, backed.then_some(&*base)).unwrap(),
            open(&raw, false, None).unwrap(),
        );
        let (q, r) = (&*qi.backend, &*ri.backend);
        let mut rng = Rng(seed * 0x1234_5678_9abc);
        for i in 0..600u32 {
            let s = rng.below(size / 512);
            let n = 1 + rng.below((3 * cluster / 512).min(size / 512 - s));
            match rng.below(10) {
                0..5 => {
                    let data: Vec<u8> = (s..s + n).flat_map(|x| sector(i, GUEST, x)).collect();
                    write(q, s * 512, &data);
                    write(r, s * 512, &data);
                }
                5 => {
                    q.write_zeroes(s * 512, n * 512).unwrap();
                    r.write_zeroes(s * 512, n * 512).unwrap();
                }
                6 => {
                    // Whole clusters (both images then read zeros there).
                    let c = s * 512 / cluster;
                    let k = 1 + rng.below(3).min(size / cluster - c - 1);
                    q.discard(c * cluster, k * cluster).unwrap();
                    r.write_zeroes(c * cluster, k * cluster).unwrap();
                }
                7 => {
                    q.sync(false).unwrap();
                }
                _ => assert!(
                    read(q, s * 512, n as usize * 512) == read(r, s * 512, n as usize * 512),
                    "seed {seed} op {i}"
                ),
            }
        }
        q.sync(true).unwrap();
        r.sync(true).unwrap();
        // Close both before qemu-img reads them.
        drop((qi, ri));
        check_clean(&top);
        let (ok, text) = qemu(&[
            "compare",
            "-F",
            "raw",
            top.to_str().unwrap(),
            raw.to_str().unwrap(),
        ]);
        assert!(ok, "seed {seed}: {text}");
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Gate: crashes that see the write order. The recording layer keeps every write, barrier and sync; a simulated crash
// at a point keeps everything before the last sync, everything before each barrier that a later kept write depends
// on, and a selection (random, or a fixed case) of the other writes, each write torn into 4 KiB pieces. The image is
// then reopened writable (refcount rebuild) and checked: every sector holds its value at the last completed flush or
// a value written after it, base data is never lost, qemu-img check is clean, and every cluster can be written.

/// What the guest did, in order: writes (zeros for WRITE_ZEROES and DISCARD) and syncs, with their log positions.
enum Guest {
    Write {
        sector: u64,
        data: Vec<u8>,
        at: usize,
    },
    Sync {
        done: usize,
    },
}

struct Run {
    dir: Dir,
    base: PathBuf,
    initial: Vec<u8>,
    sectors: u64,
    cluster: u64,
    history: Vec<Guest>,
    q: Arc<Qcow>,
    rec: Recording,
    /// The crash model: false, a natemu or macOS crash (an fsync is durable); true, a power loss on macOS (only
    /// F_FULLFSYNC is durable, only F_BARRIERFSYNC and F_FULLFSYNC order).
    power: std::cell::Cell<bool>,
}

impl Run {
    fn new(tag: &str, size: u64, bits: u32) -> Self {
        let dir = Dir::new(tag);
        let (base, top) = (dir.at("base.raw"), dir.at("top.qcow2"));
        std::fs::write(&base, base_bytes(size / 512)).unwrap();
        create_ready(&top, size, bits, Some((&base, Some("raw"))));
        let initial = std::fs::read(&top).unwrap();
        let (q, rec) = recorded(&top, Some(&base));
        Self {
            dir,
            base,
            initial,
            sectors: size / 512,
            cluster: 1 << bits,
            history: Vec::new(),
            q,
            rec,
            power: std::cell::Cell::new(false),
        }
    }
    fn at(&self) -> usize {
        self.rec.log().ops.len()
    }
    fn write(&mut self, sector: u64, data: Vec<u8>) {
        let at = self.at();
        self.q.write(&data, sector * 512).unwrap();
        self.history.push(Guest::Write { sector, data, at });
    }
    fn zero(&mut self, sector: u64, n: u64, discard: bool) {
        let at = self.at();
        if discard {
            self.q.discard(sector * 512, n * 512).unwrap();
        } else {
            self.q.write_zeroes(sector * 512, n * 512).unwrap();
        }
        self.history.push(Guest::Write {
            sector,
            data: vec![0; n as usize * 512],
            at,
        });
    }
    fn sync(&mut self) {
        let slot = self.history.len();
        self.history.push(Guest::Sync { done: usize::MAX });
        self.q.sync(false).unwrap();
        let done = self.at();
        self.history[slot] = Guest::Sync { done };
    }
    /// The log index of every guest sync's final fsync (a crash just before it: tables written, not yet durable).
    fn sync_points(&self) -> Vec<usize> {
        let ops = self.rec.log().ops.clone();
        self.history
            .iter()
            .filter_map(|g| match g {
                Guest::Sync { done, .. } => {
                    ops[..*done].iter().rposition(|o| matches!(o, Op::Fsync))
                }
                _ => None,
            })
            .collect()
    }
    /// Whether an op is a durability point in the current crash model.
    fn durable(&self, op: &Op) -> bool {
        matches!(op, Op::Sync) || matches!(op, Op::Fsync) && !self.power.get()
    }

    /// The image bytes after a crash at log position `p`, keeping `keep`'s choice of the unsynced pieces.
    fn crashed(
        &self,
        ops: &[Op],
        p: usize,
        keep: &mut dyn FnMut(&Op, u64, u64) -> bool,
    ) -> Vec<u8> {
        let last_sync = ops[..p]
            .iter()
            .rposition(|o| self.durable(o))
            .map_or(0, |i| i + 1);
        let mut image = self.initial.clone();
        let apply = |image: &mut Vec<u8>, op: &Op, from: u64, to: u64| match op {
            Op::Write(offset, data) => {
                let (start, end) = (from as usize, to as usize);
                if image.len() < end {
                    image.resize(end, 0);
                }
                image[start..end]
                    .copy_from_slice(&data[start - *offset as usize..end - *offset as usize]);
            }
            Op::SetLen(len) => image.resize(*len as usize, 0),
            _ => {}
        };
        for op in &ops[..last_sync] {
            if let Op::Write(o, d) = op {
                apply(&mut image, op, *o, o + d.len() as u64);
            } else {
                apply(&mut image, op, 0, 0);
            }
        }
        // The unsynced pieces: writes torn at 4 KiB boundaries, length changes whole.
        let mut pieces = Vec::new();
        for (i, op) in ops.iter().enumerate().take(p).skip(last_sync) {
            match op {
                Op::Write(o, d) => {
                    let end = o + d.len() as u64;
                    let mut from = *o;
                    while from < end {
                        let to = ((from / 4096 + 1) * 4096).min(end);
                        pieces.push((i, from, to));
                        from = to;
                    }
                }
                Op::SetLen(_) => pieces.push((i, 0, 0)),
                _ => {}
            }
        }
        let mut kept: Vec<bool> = pieces
            .iter()
            .map(|&(i, f, t)| keep(&ops[i], f, t))
            .collect();
        // Barrier closure: a kept piece keeps every piece before the last barrier preceding it.
        if let Some(latest) = pieces
            .iter()
            .zip(&kept)
            .filter(|(_, k)| **k)
            .map(|(p, _)| p.0)
            .max()
        {
            if let Some(b) = ops[last_sync..latest]
                .iter()
                .rposition(|o| matches!(o, Op::Barrier))
            {
                let b = last_sync + b;
                for (k, &(i, _, _)) in kept.iter_mut().zip(&pieces) {
                    *k |= i < b;
                }
            }
        }
        for (&(i, f, t), k) in pieces.iter().zip(kept) {
            if k {
                apply(&mut image, &ops[i], f, t);
            }
        }
        image
    }

    /// Reopen `image` writable and check it against the guest history for a crash at `p`.
    fn verify(&self, image: Vec<u8>, p: usize, label: &str, qemu_check: bool) {
        // The last flush whose fsync completed before the crash; none survives a power loss (fsync only).
        let last = self
            .history
            .iter()
            .rposition(|g| matches!(g, Guest::Sync { done, .. } if *done <= p))
            .filter(|_| !self.power.get());
        let mut flushed = base_bytes(self.sectors);
        let mut later: HashMap<u64, Vec<Vec<u8>>> = HashMap::new();
        for (k, g) in self.history.iter().enumerate() {
            let Guest::Write { sector, data, at } = g else {
                continue;
            };
            if last.is_some_and(|l| k < l) {
                flushed[*sector as usize * 512..*sector as usize * 512 + data.len()]
                    .copy_from_slice(data);
            } else if *at < p {
                for (j, chunk) in data.chunks(512).enumerate() {
                    later
                        .entry(sector + j as u64)
                        .or_default()
                        .push(chunk.to_vec());
                }
            }
        }
        let path = self.dir.at("crashed.qcow2");
        std::fs::write(&path, image).unwrap();
        let reopened =
            open(&path, false, Some(&self.base)).unwrap_or_else(|e| panic!("{label}: reopen: {e}"));
        let b = &*reopened.backend;
        let got = read(b, 0, self.sectors as usize * 512);
        for s in 0..self.sectors {
            let one = &got[s as usize * 512..][..512];
            let ok = one == &flushed[s as usize * 512..][..512]
                || later.get(&s).is_some_and(|v| v.iter().any(|c| c == one));
            assert!(
                ok,
                "{label}: sector {s} holds {:?}, not its flushed value or a later write",
                &one[..16]
            );
        }
        // The clusters the guest wrote still take a write (a cluster counted twice would refuse it); at most 64 of
        // them, spread out (each allocation at the file end is a slow ftruncate).
        let mut written: Vec<u64> = self
            .history
            .iter()
            .filter_map(|g| match g {
                Guest::Write { sector, .. } => Some(sector * 512 / self.cluster),
                _ => None,
            })
            .collect();
        written.sort_unstable();
        written.dedup();
        let step = written.len().div_ceil(64).max(1);
        for &c in written.iter().step_by(step) {
            b.write(&[0x5a; 512], c * self.cluster)
                .unwrap_or_else(|e| panic!("{label}: cluster {c}: {e}"));
        }
        b.sync(true).unwrap();
        drop(reopened);
        if qemu_check {
            check_clean(&path);
        }
    }
}

/// Header fields of the image bytes: the L1 table's range.
fn l1_range(image: &[u8]) -> (u64, u64) {
    let be = |at: usize, n: usize| {
        image[at..at + n]
            .iter()
            .fold(0u64, |a, &b| a << 8 | b as u64)
    };
    (be(40, 8), be(36, 4) * 8)
}
/// The L2 tables the image's L1 points at.
fn l2_tables(image: &[u8]) -> Vec<u64> {
    let (offset, len) = l1_range(image);
    image[offset as usize..(offset + len) as usize]
        .chunks(8)
        .map(|c| u64::from_be_bytes(c.try_into().unwrap()) & 0x00ff_ffff_ffff_fe00)
        .filter(|&a| a != 0)
        .collect()
}
fn overlaps(op: &Op, from: u64, to: u64, start: u64, len: u64) -> bool {
    matches!(op, Op::Write(..)) && from < start + len && start < to
}

/// Random guest sequences; a crash at each flush's fsync with random, L1-only and in-place-L2-only selections, and at
/// random points.
fn crash_seeds(bits: u32, size: u64, seeds: u64, ops: u32) {
    let seeds = std::env::var("NATEMU_QCOW_SEEDS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(seeds);
    let mut checks = 0;
    for seed in 1..=seeds {
        let mut run = Run::new("crash", size, bits);
        let mut rng = Rng(seed * 0x51_7cc1_b727_220a ^ bits as u64);
        let (sectors, per) = (run.sectors, run.cluster / 512);
        for i in 0..ops {
            let s = rng.below(sectors);
            match rng.below(10) {
                0..6 => {
                    let n = 1 + rng.below((3 * per).min(sectors - s));
                    let data = (s..s + n).flat_map(|x| sector(i + 1, GUEST, x)).collect();
                    run.write(s, data);
                }
                6 => {
                    let n = 1 + rng.below((2 * per).min(sectors - s));
                    run.zero(s, n, false);
                }
                7 => {
                    let c = s / per;
                    run.zero(c * per, per, true);
                }
                _ => run.sync(),
            }
        }
        let log = run.rec.log().ops.clone();
        let first = log.iter().position(|o| matches!(o, Op::Sync)).unwrap() + 1;
        let mut points: Vec<usize> = run.sync_points();
        points.extend((0..8).map(|_| first + rng.below((log.len() - first) as u64) as usize));
        points.push(log.len());
        for (n, &p) in points.iter().enumerate() {
            let label = format!("bits {bits} seed {seed} crash at {p}/{}", log.len());
            let durable = run.crashed(&log, p, &mut |_, _, _| false);
            let l1 = l1_range(&durable);
            let tables = l2_tables(&durable);
            let c = run.cluster;
            let mut r = Rng(rng.next() | 1);
            run.verify(
                run.crashed(&log, p, &mut |_, _, _| r.below(2) == 0),
                p,
                &format!("{label} random"),
                n % 6 == 0,
            );
            run.verify(
                run.crashed(&log, p, &mut |op, f, t| overlaps(op, f, t, l1.0, l1.1)),
                p,
                &format!("{label} L1 kept, the rest dropped"),
                false,
            );
            run.verify(
                run.crashed(&log, p, &mut |op, f, t| {
                    tables.iter().any(|&a| overlaps(op, f, t, a, c))
                }),
                p,
                &format!("{label} in-place L2 kept, the rest dropped"),
                false,
            );
            // A power loss: no guest flush is durable (fsync only), but nothing may be corrupt.
            run.power.set(true);
            let mut r = Rng(rng.next() | 1);
            run.verify(
                run.crashed(&log, p, &mut |_, _, _| r.below(2) == 0),
                p,
                &format!("{label} power loss, random"),
                false,
            );
            let l1 = l1_range(&run.initial);
            run.verify(
                run.crashed(&log, p, &mut |op, f, t| !overlaps(op, f, t, l1.0, l1.1)),
                p,
                &format!("{label} power loss, L1 writes dropped"),
                false,
            );
            run.power.set(false);
            checks += 2;
            checks += 3;
        }
    }
    eprintln!(
        "crash test: cluster {} bytes, {seeds} seeds, {checks} crash images checked",
        1u64 << bits
    );
}

#[test]
fn crashes_keep_every_flushed_sector_with_64k_clusters() {
    crash_seeds(16, 4 << 20, 10, 150);
}

#[test]
fn crashes_keep_every_flushed_sector_with_512_byte_clusters_and_l2_evictions() {
    // 4 MiB of 512-byte clusters is 128 L2 tables: more than the 100-table cache, so dirty tables are evicted.
    crash_seeds(9, 4 << 20, 6, 400);
}

/// Fixed case: data written, then the L2 table that points at it changed in place (a discard dirties the table
/// without moving it), then a crash keeping that table's write and dropping everything it does not depend on.
#[test]
fn crash_keeping_an_in_place_l2_write_keeps_the_data_it_points_at() {
    let mut run = Run::new("inplace", 1 << 20, 16);
    let per = run.cluster / 512;
    run.write(0, (0..per).flat_map(|x| sector(1, GUEST, x)).collect());
    run.sync();
    run.zero(0, per, true); // the clean L2 table changed in place
    run.write(2 * per + 3, sector(2, GUEST, 2 * per + 3)); // a new data cluster, its entry in that same table
    run.sync();
    let log = run.rec.log().ops.clone();
    let p = *run.sync_points().last().unwrap();
    let durable = run.crashed(&log, p, &mut |_, _, _| false);
    let tables = l2_tables(&durable);
    assert_eq!(tables.len(), 1);
    let image = run.crashed(&log, p, &mut |op, f, t| {
        overlaps(op, f, t, tables[0], 1 << 16)
    });
    run.verify(image, p, "in-place L2 kept", true);
}

/// Fixed case, the eviction path: a table changed in place and given a new data cluster is evicted from the L2 cache
/// (more than 100 tables in use) before any flush; a crash right after the eviction keeps that table's write.
#[test]
fn crash_keeping_an_evicted_in_place_l2_keeps_the_data_it_points_at() {
    // 512-byte clusters: one sector per cluster, 64 per L2 table; 512 tables against a 100-table cache.
    let mut run = Run::new("evict", 16 << 20, 9);
    for t in 0..512 {
        run.write(t * 64 + 1, sector(1, GUEST, t * 64 + 1));
    }
    run.sync();
    let tables = l2_tables(&run.rec.log().live);
    let start = run.at();
    let in_place = |op: &Op, f: u64, t: u64| tables.iter().any(|&a| overlaps(op, f, t, a, 512));
    // Each table in turn: changed in place (a zero flag over the base), then given a new data cluster. The cache
    // holds 100 tables, so loading the next ones evicts dirty ones: the first such write is the crash point.
    for t in 0..512 {
        run.zero(t * 64 + 1, 1, true);
        run.write(t * 64 + 5, sector(3, GUEST, t * 64 + 5));
    }
    let log = run.rec.log().ops.clone();
    let first = (start..log.len()).find(|&i| match &log[i] {
        Op::Write(at, d) => in_place(&log[i], *at, at + d.len() as u64),
        _ => false,
    });
    let p = first.expect("no dirty table was evicted") + 1;
    let image = run.crashed(&log, p, &mut |op, f, t| in_place(op, f, t));
    run.verify(image, p, "evicted in-place L2 kept", true);
}

/// Fixed case: a flush moves an L2 table (its old cluster is freed) and, while its fsync runs, another worker
/// allocates a cluster; a crash before that fsync keeps the worker's data and drops the new L1. The freed cluster
/// must not have been reused: the durable L1 still points at it.
#[test]
fn crash_before_the_fsync_of_a_flush_that_freed_a_cluster_finds_it_unreused() {
    let mut run = Run::new("reuse", 1 << 20, 16);
    let per = run.cluster / 512;
    run.write(0, (0..per).flat_map(|x| sector(1, GUEST, x)).collect());
    run.sync();
    // The table is clean: this write moves it to a new cluster and frees the old one.
    run.write(
        per,
        (per..2 * per).flat_map(|x| sector(2, GUEST, x)).collect(),
    );
    let q = run.q.clone();
    let y = (4 * per..5 * per)
        .flat_map(|x| sector(3, GUEST, x))
        .collect::<Vec<u8>>();
    let hook_data = y.clone();
    run.rec.log().on_sync = Some(Box::new(move || {
        q.write(&hook_data, 4 * per * 512).map(|_| ()).unwrap()
    }));
    let at = run.at();
    let slot = run.history.len();
    run.history.push(Guest::Sync { done: usize::MAX });
    run.history.push(Guest::Write {
        sector: 4 * per,
        data: y,
        at,
    });
    run.q.sync(false).unwrap();
    let done = run.at();
    run.history[slot] = Guest::Sync { done };
    let log = run.rec.log().ops.clone();
    let p = log[..done]
        .iter()
        .rposition(|o| matches!(o, Op::Fsync))
        .unwrap();
    let l1 = l1_range(&run.initial);
    let image = run.crashed(&log, p, &mut |op, f, t| !overlaps(op, f, t, l1.0, l1.1));
    run.verify(image, p, "worker data kept, new L1 dropped", true);
}

/// Fixed case, a power loss: a flush moves an L2 table (freeing its old cluster) and completes with its fsync; the
/// next write reuses that cluster. On macOS the fsync orders nothing against a power loss, so only the barrier after
/// it keeps the new L1 ahead of the guest data in the reused cluster. The crash keeps that data and drops the L1.
#[test]
fn power_loss_after_a_flush_that_freed_a_cluster_never_shows_guest_data_as_a_table() {
    let mut run = Run::new("reuse-power", 1 << 20, 16);
    let per = run.cluster / 512;
    run.write(0, (0..per).flat_map(|x| sector(1, GUEST, x)).collect());
    run.sync();
    run.write(
        per,
        (per..2 * per).flat_map(|x| sector(2, GUEST, x)).collect(),
    );
    run.sync(); // the table moved; its old cluster is free after this flush
    run.write(
        4 * per,
        (4 * per..5 * per)
            .flat_map(|x| sector(3, GUEST, x))
            .collect(),
    );
    let log = run.rec.log().ops.clone();
    let p = log.len();
    let l1 = l1_range(&run.initial);
    run.power.set(true);
    let image = run.crashed(&log, p, &mut |op, f, t| !overlaps(op, f, t, l1.0, l1.1));
    run.verify(
        image,
        p,
        "power loss: data in the reused cluster kept, new L1 dropped",
        true,
    );
}

// ---------------------------------------------------------------------------------------------------------------
// A real kill: only the unclean-exit refcount rebuild path (a kill keeps the page cache, so it cannot see ordering).

#[test]
#[ignore = "the child of kill_leaves_an_image_the_next_writable_open_rebuilds"]
fn kill_child() {
    let Ok(path) = std::env::var("NATEMU_QCOW_KILL_CHILD") else {
        return;
    };
    let image = open(Path::new(&path), false, None).unwrap();
    let b = &*image.backend;
    let mut out = std::io::stdout();
    for i in 0u64.. {
        let c = i % 256;
        b.write(&sector(i as u32, GUEST, c).repeat(128), c << 16)
            .unwrap();
        if i % 4 == 3 {
            b.sync(false).unwrap();
            use std::io::Write;
            writeln!(out, "synced {i}").unwrap();
            out.flush().unwrap();
        }
    }
}

#[test]
fn kill_leaves_an_image_the_next_writable_open_rebuilds() {
    use std::io::BufRead;
    let dir = Dir::new("kill");
    let path = dir.at("a.qcow2");
    create_ready(&path, 16 << 20, 16, None);
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "disk::qcow::tests::kill_child",
            "--ignored",
            "--nocapture",
            "--test-threads=1",
        ])
        .env("NATEMU_QCOW_KILL_CHILD", &path)
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let mut last = None;
    // The pipe stays open until the kill (the child must die by SIGKILL, not by a closed pipe).
    let mut lines = std::io::BufReader::new(child.stdout.take().unwrap()).lines();
    for line in lines.by_ref() {
        let line = line.unwrap();
        if let Some(n) = line.strip_prefix("synced ") {
            last = Some(n.parse::<u64>().unwrap());
            if last.unwrap() >= 600 {
                break;
            }
        }
    }
    child.kill().unwrap();
    child.wait().unwrap();
    drop(lines);
    let last = last.expect("the child synced");
    let t = Instant::now();
    let image = open(&path, false, None).unwrap();
    eprintln!(
        "kill test: rebuilt after {} writes in {:?}",
        last + 1,
        t.elapsed()
    );
    // Every cluster holds its newest synced write or a later one (a kill keeps the page cache).
    for c in 0..256u64 {
        let newest = (0..=last).rev().find(|j| j % 256 == c).unwrap();
        let got = read(&*image.backend, c << 16, 512);
        let tag = u32::from_le_bytes(got[4..8].try_into().unwrap()) as u64;
        assert!(
            tag >= newest && tag % 256 == c && got == sector(tag as u32, GUEST, c),
            "cluster {c}"
        );
    }
    image.backend.sync(true).unwrap();
    drop(image);
    check_clean(&path);
}
