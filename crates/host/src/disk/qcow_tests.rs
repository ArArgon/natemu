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
    sync::{Arc, atomic::AtomicUsize},
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
        self.log().ops.push(Op::Sync);
        Ok(())
    }
    fn punch_hole(&self, offset: u64, length: u64) -> io::Result<()> {
        let hook = self.log().on_punch.take();
        if let Some(hook) = hook {
            hook();
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
    let len = rec.log().live.len();
    q.write(&[2; 1 << 16], 1 << 17).unwrap();
    assert!(
        rec.log().live.len() > len,
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
