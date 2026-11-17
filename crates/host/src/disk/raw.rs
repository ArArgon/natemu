//! The raw backend: one regular file, the guest's disk byte for byte.
use super::{DiskBackend, Image, QCOW2_MAGIC};
use crate::Result;
use std::{
    fs::{File, OpenOptions, TryLockError},
    os::{
        fd::AsRawFd,
        unix::fs::{FileExt, MetadataExt},
    },
    path::Path,
};

const SECTOR: u64 = 512;
/// The host block size: DISCARD and WRITE_ZEROES free whole blocks only.
const HOLE: u64 = 4096;

/// Open `path` for the checks and the format detection: the file, its first sector and its last. A writable image
/// takes the exclusive lock, a read-only one a shared lock (see [`image`]).
pub(super) fn inspect(path: &Path, read_only: bool) -> Result<(File, Vec<u8>, Vec<u8>)> {
    let file = OpenOptions::new()
        .read(true)
        .write(!read_only)
        .open(path)
        .map_err(|e| format!("disk {}: {e}", path.display()))?;
    let info = file.metadata().map_err(|e| e.to_string())?;
    let len = info.len();
    if !info.is_file() || len == 0 || len % SECTOR != 0 || len > i64::MAX as u64 {
        return Err("disk must be a nonempty regular file with sector-aligned capacity".into());
    }
    // Advisory exclusion among cooperating launches; users must exclude other writers/truncators. Read-only images
    // share, so launches can share a base or an ISO and a writer is refused while a reader holds it.
    let locked = if read_only {
        file.try_lock_shared()
    } else {
        file.try_lock()
    };
    match locked {
        Ok(()) => {}
        Err(TryLockError::WouldBlock) => return Err("disk lock: held by another process".into()),
        // A file system without flock (a network share): a reader goes on, a writer is refused.
        Err(TryLockError::Error(e)) if read_only && e.kind() == std::io::ErrorKind::Unsupported => {
            eprintln!(
                "disk {}: no file locking here; opened read-only without a lock",
                path.display()
            );
        }
        Err(TryLockError::Error(e)) => return Err(format!("disk lock: {e}")),
    }
    let (mut head, mut tail) = (vec![0; SECTOR as usize], vec![0; SECTOR as usize]);
    file.read_exact_at(&mut head, 0)
        .map_err(|e| format!("disk {}: {e}", path.display()))?;
    file.read_exact_at(&mut tail, len - SECTOR)
        .map_err(|e| format!("disk {}: {e}", path.display()))?;
    Ok((file, head, tail))
}

/// The raw image on a file [`inspect`] opened and locked.
pub(super) fn image(file: File, read_only: bool) -> Result<Image> {
    let info = file.metadata().map_err(|e| e.to_string())?;
    let id = format!(
        "{:08x}{:012x}",
        info.dev() as u32,
        info.ino() & 0xffff_ffff_ffff
    )
    .into_bytes()
    .try_into()
    .expect("8 + 12 hex digits");
    Ok(Image {
        backend: Box::new(Raw::new(file, read_only)),
        capacity: info.len(),
        read_only,
        discard: !read_only,
        write_zeroes: !read_only,
        id,
    })
}

/// One positioned read or write per call (pread/pwrite: a short count is returned as is, EINTR
/// is an error). A full sync is F_FULLFSYNC (`File::sync_all` on Apple platforms); a plain one is
/// fsync(2), as QEMU's guest flush: it survives a natemu crash but not a host power loss or macOS
/// crash (std's `sync_data` is F_FULLFSYNC on Apple platforms too, so fsync is called directly).
/// A read-only image refuses every write and its syncs succeed without a call.
struct Raw {
    file: File,
    read_only: bool,
}
impl Raw {
    fn new(file: File, read_only: bool) -> Self {
        Self { file, read_only }
    }
    /// Deallocate whole host blocks (`F_PUNCHHOLE`): they then read as zeros.
    fn hole(&self, offset: u64, len: u64) -> std::io::Result<()> {
        /// `struct fpunchhole` of <sys/fcntl.h>.
        #[repr(C)]
        struct Hole {
            flags: u32,
            reserved: u32,
            offset: i64,
            length: i64,
        }
        const F_PUNCHHOLE: i32 = 99;
        unsafe extern "C" {
            fn fcntl(fd: i32, cmd: i32, ...) -> i32;
        }
        let hole = Hole {
            flags: 0,
            reserved: 0,
            offset: offset as i64,
            length: len as i64,
        };
        loop {
            if unsafe { fcntl(self.file.as_raw_fd(), F_PUNCHHOLE, &hole as *const Hole) } == 0 {
                return Ok(());
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }
    /// The range's whole host blocks become holes (partial blocks stay as they are; a file system without holes is
    /// no error). With `zero` the partial ones at the ends are written with zeros, or without holes the whole range.
    /// ponytail: a zeroed range without host holes is one synchronous write of up to 2 GiB, inside the call limit only
    /// on a fast disk; split it into jobs if a host file system without holes matters.
    fn punch(&self, offset: u64, len: u64, zero: bool) -> std::io::Result<()> {
        if self.read_only {
            return Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied));
        }
        let end = offset + len;
        let (first, last) = (offset.next_multiple_of(HOLE), end / HOLE * HOLE);
        let holed = first < last
            && match self.hole(first, last - first) {
                Ok(()) => true,
                Err(e)
                    if matches!(e.kind(), std::io::ErrorKind::Unsupported)
                        || e.raw_os_error() == Some(45) =>
                {
                    false
                }
                Err(e) => return Err(e),
            };
        if !zero {
            return Ok(());
        }
        if holed {
            self.zeros(offset, first)?;
            self.zeros(last, end)
        } else {
            self.zeros(offset, end)
        }
    }
    /// Writes zeros over [`from`, `to`).
    fn zeros(&self, mut from: u64, to: u64) -> std::io::Result<()> {
        const CHUNK: [u8; 1 << 16] = [0; 1 << 16];
        while from < to {
            let n = (to - from).min(CHUNK.len() as u64) as usize;
            match self.file.write_at(&CHUNK[..n], from)? {
                0 => return Err(std::io::Error::other("native disk call made zero progress")),
                w => from += w.min(n) as u64,
            }
        }
        Ok(())
    }
}
impl DiskBackend for Raw {
    fn read(&self, bytes: &mut [u8], offset: u64) -> std::io::Result<usize> {
        self.file.read_at(bytes, offset)
    }
    fn write(&self, bytes: &[u8], offset: u64) -> std::io::Result<usize> {
        if self.read_only {
            return Err(std::io::Error::from(std::io::ErrorKind::PermissionDenied));
        }
        // First-sector guard: a guest write that would make the first four bytes the qcow2 magic fails, as QEMU's
        // raw format does; the next launch would otherwise detect qcow2 and follow a backing file name the guest chose.
        // ponytail: not atomic against another worker's write to the same four bytes; a lock if that ever matters.
        if offset < 4 && !bytes.is_empty() {
            let mut first = [0; 4];
            let n = self.file.read_at(&mut first, 0)?;
            first[n..].fill(0);
            let at = offset as usize;
            let n = bytes.len().min(4 - at);
            first[at..at + n].copy_from_slice(&bytes[..n]);
            if first == QCOW2_MAGIC {
                return Err(std::io::Error::other(
                    "a write that would make the image look like qcow2",
                ));
            }
        }
        self.file.write_at(bytes, offset)
    }
    fn sync(&self, full: bool) -> std::io::Result<()> {
        if self.read_only {
            return Ok(());
        }
        if full {
            return self.file.sync_all();
        }
        unsafe extern "C" {
            fn fsync(fd: i32) -> i32;
        }
        // As `sync_all`, retry on EINTR.
        loop {
            if unsafe { fsync(self.file.as_raw_fd()) } == 0 {
                return Ok(());
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }
    fn discard(&self, offset: u64, len: u64) -> std::io::Result<()> {
        self.punch(offset, len, false)
    }
    fn write_zeroes(&self, offset: u64, len: u64) -> std::io::Result<()> {
        self.punch(offset, len, true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::MetadataExt;
    fn temp(name: &str, bytes: &[u8]) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!("natemu-raw-{name}-{}", std::process::id()));
        std::fs::write(&path, bytes).unwrap();
        path
    }
    #[test]
    fn plain_and_full_syncs_succeed_on_a_file() {
        let path = temp("sync", &[]);
        let native = Raw::new(
            File::options().read(true).write(true).open(&path).unwrap(),
            false,
        );
        std::fs::remove_file(&path).unwrap();
        assert_eq!(native.write(&[0xa5; 512], 0).unwrap(), 512);
        native.sync(false).unwrap();
        native.sync(true).unwrap();
    }
    #[test]
    fn punched_ranges_read_as_zeros_and_free_whole_blocks() {
        let path = temp("punch", &[]);
        let file = File::options().read(true).write(true).open(&path).unwrap();
        let native = Raw::new(file.try_clone().unwrap(), false);
        assert_eq!(native.write(&[0xa5; 1 << 16], 0).unwrap(), 1 << 16);
        native.sync(false).unwrap();
        let blocks = || file.metadata().unwrap().blocks();
        let before = blocks();
        // WRITE_ZEROES over unaligned ends: zeros exactly there, the whole blocks inside become holes.
        native.write_zeroes(1000, 20000).unwrap();
        // DISCARD: whole blocks only; the partial block at its start keeps its bytes.
        native.discard(40000, 16384).unwrap();
        native.sync(false).unwrap();
        let mut data = vec![0; 1 << 16];
        assert_eq!(native.read(&mut data, 0).unwrap(), 1 << 16);
        std::fs::remove_file(&path).unwrap();
        let zero = |r: std::ops::Range<usize>| data[r].iter().all(|&b| b == 0);
        let kept = |r: std::ops::Range<usize>| data[r].iter().all(|&b| b == 0xa5);
        assert!(
            kept(0..1000)
                && zero(1000..21000)
                && kept(21000..40960)
                && zero(40960..53248)
                && kept(53248..65536)
        );
        assert!(
            blocks() < before,
            "no host blocks freed: {} -> {}",
            before,
            blocks()
        );
    }
    #[test]
    fn read_only_open_shares_the_lock_refuses_writers_and_offers_nothing_that_writes() {
        let path = temp("ro", &[7; 4096]);
        let a = crate::disk::open(&path, true, None).unwrap();
        let b = crate::disk::open(&path, true, None).unwrap();
        assert!(a.read_only && !a.discard && !a.write_zeroes && b.read_only);
        assert!(
            crate::disk::open(&path, false, None).is_err(),
            "a writer opened beside readers"
        );
        let mut data = [0; 512];
        assert_eq!(a.backend.read(&mut data, 512).unwrap(), 512);
        assert_eq!(data, [7; 512]);
        for e in [
            a.backend.write(&[1; 512], 0).unwrap_err(),
            a.backend.discard(0, 4096).unwrap_err(),
            a.backend.write_zeroes(0, 4096).unwrap_err(),
        ] {
            assert_eq!(e.kind(), std::io::ErrorKind::PermissionDenied);
        }
        a.backend.sync(false).unwrap();
        a.backend.sync(true).unwrap();
        drop((a, b));
        let w = crate::disk::open(&path, false, None).unwrap();
        assert!(
            crate::disk::open(&path, true, None).is_err(),
            "a reader opened beside a writer"
        );
        drop(w);
        std::fs::remove_file(&path).unwrap();
    }
    #[test]
    fn first_sector_guard_refuses_the_qcow2_magic_only_on_writable_images() {
        let path = temp("guard", &[0; 4096]);
        let w = crate::disk::open(&path, false, None).unwrap().backend;
        // The whole magic, and a part of it completing what is there.
        assert!(w.write(b"QFI\xfb", 0).is_err());
        assert!(
            w.write(&[0xfb], 3).is_ok_and(|n| n == 1),
            "3 of 4 bytes is not the magic"
        );
        assert!(
            w.write(b"QFI", 0).is_err(),
            "the last byte is already there"
        );
        assert!(w.write(&[0; 4], 0).is_ok());
        assert!(w.write(&[b'Q', b'F'], 0).is_ok() && w.write(b"I\xfb", 2).is_err());
        // Elsewhere and at other bytes is fine.
        assert!(w.write(b"QFI\xfb", 512).is_ok() && w.write(b"QFI\xfc", 0).is_ok());
        let mut head = [0; 4];
        w.read(&mut head, 0).unwrap();
        assert_eq!(&head, b"QFI\xfc");
        drop(w);
        std::fs::remove_file(&path).unwrap();
    }
}
