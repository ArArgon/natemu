// Copyright 2026 natemu contributors. All rights reserved.
//
// SPDX-License-Identifier: Apache-2.0 AND BSD-3-Clause
//
// natemu-written replacement for Cloud Hypervisor's `AlignedFile` (block/src/aligned_file.rs), the
// vmm-sys-util `WriteZeroesAt` / `PunchHole` / `FileSync` traits and block/src/sparse.rs.

//! The file layer under every qcow2 table and data access.
//!
//! [`ImageFile`] is what the vendored code reads and writes through. It wraps a [`FileLayer`]
//! trait object so a test can later substitute a recording or fault-injecting file; [`StdFile`]
//! (a `std::fs::File`) is the real layer. There is no O_DIRECT handling.

use std::fmt::Debug;
use std::fs::File;
use std::io;
use std::os::unix::fs::FileExt;
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

/// How hard a [`FileLayer::sync`] call must push data to stable storage.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SyncKind {
    /// `fsync(2)`: the guest FLUSH class. On macOS this does not flush the drive cache.
    Fsync,
    /// `F_BARRIERFSYNC` on macOS (write ordering barrier); `fsync` elsewhere.
    Barrier,
    /// `F_FULLFSYNC` on macOS (flush the drive cache); `fsync` elsewhere.
    Full,
}

/// Minimal positional-I/O file interface the qcow code needs. All methods take `&self`.
pub trait FileLayer: Send + Sync + Debug {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> io::Result<usize>;
    fn write_at(&self, buf: &[u8], offset: u64) -> io::Result<usize>;
    fn file_len(&self) -> io::Result<u64>;
    fn set_len(&self, size: u64) -> io::Result<()>;
    fn sync(&self, kind: SyncKind) -> io::Result<()>;
    /// After `Ok`, `[offset, offset + length)` reads as zeros; blocks may be freed.
    fn punch_hole(&self, offset: u64, length: u64) -> io::Result<()>;
    /// After `Ok`, `[offset, offset + length)` reads as zeros.
    fn write_zeroes(&self, offset: u64, length: u64) -> io::Result<()> {
        write_zero_range(|b, o| self.write_at(b, o), offset, length)
    }
    fn is_writable(&self) -> bool;
    fn try_clone_layer(&self) -> io::Result<Box<dyn FileLayer>>;
}

/// Write `length` zero bytes at `offset` with positional writes of at most 64 KiB.
pub fn write_zero_range(
    write_at: impl Fn(&[u8], u64) -> io::Result<usize>,
    mut offset: u64,
    mut length: u64,
) -> io::Result<()> {
    static ZEROS: [u8; 65536] = [0; 65536];
    while length > 0 {
        let n = length.min(ZEROS.len() as u64) as usize;
        let written = write_at(&ZEROS[..n], offset)?;
        if written == 0 {
            return Err(io::ErrorKind::WriteZero.into());
        }
        offset += written as u64;
        length -= written as u64;
    }
    Ok(())
}

/// `FileLayer` over a plain `std::fs::File`.
#[derive(Debug)]
pub struct StdFile(pub File);

impl FileLayer for StdFile {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> io::Result<usize> {
        self.0.read_at(buf, offset)
    }

    fn write_at(&self, buf: &[u8], offset: u64) -> io::Result<usize> {
        self.0.write_at(buf, offset)
    }

    fn file_len(&self) -> io::Result<u64> {
        Ok(self.0.metadata()?.len())
    }

    fn set_len(&self, size: u64) -> io::Result<()> {
        self.0.set_len(size)
    }

    fn sync(&self, kind: SyncKind) -> io::Result<()> {
        sync_fd(self.0.as_raw_fd(), kind)
    }

    fn punch_hole(&self, offset: u64, length: u64) -> io::Result<()> {
        punch_hole_fd(&self.0, offset, length)
    }

    fn is_writable(&self) -> bool {
        // SAFETY: fcntl(F_GETFL) does not modify the descriptor.
        let flags = unsafe { libc::fcntl(self.0.as_raw_fd(), libc::F_GETFL) };
        if flags < 0 {
            return false;
        }
        let access_mode = flags & libc::O_ACCMODE;
        access_mode == libc::O_WRONLY || access_mode == libc::O_RDWR
    }

    fn try_clone_layer(&self) -> io::Result<Box<dyn FileLayer>> {
        Ok(Box::new(StdFile(self.0.try_clone()?)))
    }
}

fn fcntl_sync(fd: libc::c_int, cmd: libc::c_int) -> io::Result<()> {
    // SAFETY: fcntl with a no-argument sync command on a valid descriptor.
    if unsafe { libc::fcntl(fd, cmd) } < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

fn fsync_retry_eintr(fd: libc::c_int) -> io::Result<()> {
    loop {
        // SAFETY: fsync on a valid descriptor.
        if unsafe { libc::fsync(fd) } == 0 {
            return Ok(());
        }
        let e = io::Error::last_os_error();
        if e.kind() != io::ErrorKind::Interrupted {
            return Err(e);
        }
    }
}

/// Sync `fd` as asked. On macOS `Barrier` is `F_BARRIERFSYNC` and `Full` is `F_FULLFSYNC`; a file
/// system that rejects either (ENOTSUP/EINVAL, e.g. some network shares) gets plain `fsync`.
pub fn sync_fd(fd: libc::c_int, kind: SyncKind) -> io::Result<()> {
    #[cfg(target_os = "macos")]
    {
        let cmd = match kind {
            SyncKind::Fsync => return fsync_retry_eintr(fd),
            SyncKind::Barrier => libc::F_BARRIERFSYNC,
            SyncKind::Full => libc::F_FULLFSYNC,
        };
        match fcntl_sync(fd, cmd) {
            Err(e) if matches!(e.raw_os_error(), Some(libc::ENOTSUP | libc::EINVAL)) => {
                fsync_retry_eintr(fd)
            }
            r => r,
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (kind, fcntl_sync as fn(_, _) -> _);
        fsync_retry_eintr(fd)
    }
}

/// `F_BARRIERFSYNC` (macOS) or `fsync` on the file.
pub fn barrier_fsync(file: &File) -> io::Result<()> {
    sync_fd(file.as_raw_fd(), SyncKind::Barrier)
}

/// `F_FULLFSYNC` (macOS) or `fsync` on the file.
pub fn full_fsync(file: &File) -> io::Result<()> {
    sync_fd(file.as_raw_fd(), SyncKind::Full)
}

/// `struct fpunchhole` from <sys/fcntl.h> (the libc crate does not define it).
#[cfg(target_os = "macos")]
#[repr(C)]
struct FPunchHole {
    fp_flags: u32,
    reserved: u32,
    fp_offset: libc::off_t,
    fp_length: libc::off_t,
}

/// Make `[offset, offset + length)` read as zeros, freeing blocks where the OS can.
///
/// macOS: `fcntl(F_PUNCHHOLE)` on the block-aligned interior, positional zero writes for the
/// unaligned head and tail, and zero writes for everything if the call is refused (APFS needs
/// 4 KiB alignment; other file systems may not support it). Linux: `fallocate(PUNCH_HOLE |
/// KEEP_SIZE)` with the same zero-write fallback.
pub fn punch_hole_fd(file: &File, offset: u64, length: u64) -> io::Result<()> {
    let end = offset
        .checked_add(length)
        .filter(|&e| i64::try_from(e).is_ok())
        .ok_or_else(|| io::Error::from_raw_os_error(libc::EINVAL))?;
    let zero = |o: u64, l: u64| write_zero_range(|b, p| file.write_at(b, p), o, l);
    if length == 0 {
        return Ok(());
    }
    // Only "not supported here" falls back to zero writes (as upstream sparse.rs); any other
    // error from the punch propagates.
    let unsupported =
        |e: &io::Error| matches!(e.raw_os_error(), Some(libc::EOPNOTSUPP | libc::ENOTSUP));
    #[cfg(target_os = "macos")]
    {
        const ALIGN: u64 = 4096;
        let a_start = offset.next_multiple_of(ALIGN);
        let a_end = end & !(ALIGN - 1);
        if a_start < a_end {
            let args = FPunchHole {
                fp_flags: 0,
                reserved: 0,
                fp_offset: a_start as libc::off_t,
                fp_length: (a_end - a_start) as libc::off_t,
            };
            // SAFETY: F_PUNCHHOLE reads one fpunchhole through the pointer.
            let r = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_PUNCHHOLE, &args) };
            if r == 0 {
                zero(offset, a_start - offset)?;
                return zero(a_end, end - a_end);
            }
            let e = io::Error::last_os_error();
            if !unsupported(&e) {
                return Err(e);
            }
        }
        zero(offset, length)
    }
    #[cfg(target_os = "linux")]
    {
        // SAFETY: fallocate on a valid descriptor.
        let r = unsafe {
            libc::fallocate(
                file.as_raw_fd(),
                libc::FALLOC_FL_PUNCH_HOLE | libc::FALLOC_FL_KEEP_SIZE,
                offset as libc::off_t,
                length as libc::off_t,
            )
        };
        if r == 0 {
            return Ok(());
        }
        let e = io::Error::last_os_error();
        if unsupported(&e) {
            zero(offset, length)
        } else {
            Err(e)
        }
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = (end, unsupported);
        zero(offset, length)
    }
}

/// The file handle the vendored qcow code uses everywhere `AlignedFile` was used upstream.
#[derive(Debug)]
pub struct ImageFile {
    layer: Box<dyn FileLayer>,
    /// Path of the file, supplied by the caller (upstream read `/proc/self/fd/N`, which macOS
    /// lacks). Used to resolve relative backing file names and in warnings.
    path: Option<PathBuf>,
}

impl ImageFile {
    pub fn new(file: File) -> Self {
        Self::from_layer(Box::new(StdFile(file)))
    }

    /// Wrap `file`, recording the `path` it was opened from.
    pub fn with_path(file: File, path: impl AsRef<Path>) -> Self {
        Self {
            layer: Box::new(StdFile(file)),
            path: Some(path.as_ref().to_path_buf()),
        }
    }

    pub fn from_layer(layer: Box<dyn FileLayer>) -> Self {
        Self { layer, path: None }
    }

    /// The path supplied at construction, if any.
    pub fn path(&self) -> Option<&Path> {
        self.path.as_deref()
    }

    pub fn try_clone(&self) -> io::Result<Self> {
        Ok(Self {
            layer: self.layer.try_clone_layer()?,
            path: self.path.clone(),
        })
    }

    pub fn file_len(&self) -> io::Result<u64> {
        self.layer.file_len()
    }

    pub fn set_len(&self, size: u64) -> io::Result<()> {
        self.layer.set_len(size)
    }

    /// `fsync` class (see [`SyncKind::Fsync`]).
    pub fn sync_all(&self) -> io::Result<()> {
        self.layer.sync(SyncKind::Fsync)
    }

    /// Same as [`Self::sync_all`]: macOS has no `fdatasync`.
    pub fn sync_data(&self) -> io::Result<()> {
        self.layer.sync(SyncKind::Fsync)
    }

    pub fn barrier_sync(&self) -> io::Result<()> {
        self.layer.sync(SyncKind::Barrier)
    }

    pub fn full_sync(&self) -> io::Result<()> {
        self.layer.sync(SyncKind::Full)
    }

    pub fn is_writable(&self) -> bool {
        self.layer.is_writable()
    }

    pub fn punch_hole(&self, offset: u64, length: u64) -> io::Result<()> {
        self.layer.punch_hole(offset, length)
    }

    pub fn write_zeroes_at(&self, offset: u64, length: usize) -> io::Result<usize> {
        self.layer.write_zeroes(offset, length as u64)?;
        Ok(length)
    }

    pub fn write_all_zeroes_at(&self, offset: u64, length: usize) -> io::Result<()> {
        self.write_zeroes_at(offset, length).map(|_| ())
    }

    /// Plain `fsync` (upstream `FileSync::fsync`).
    pub fn fsync(&mut self) -> io::Result<()> {
        self.layer.sync(SyncKind::Fsync)
    }
}

impl FileExt for ImageFile {
    fn read_at(&self, buf: &mut [u8], offset: u64) -> io::Result<usize> {
        self.layer.read_at(buf, offset)
    }

    fn write_at(&self, buf: &[u8], offset: u64) -> io::Result<usize> {
        self.layer.write_at(buf, offset)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_util::TempFile;

    #[test]
    fn write_zeroes_and_punch_hole_read_back_zero() {
        let tmp = TempFile::new().unwrap();
        let f = ImageFile::new(tmp.as_file().try_clone().unwrap());
        f.write_all_at(&vec![0xAA; 20000], 0).unwrap();
        f.write_all_zeroes_at(100, 300).unwrap();
        // Unaligned head and tail plus an aligned interior page.
        f.punch_hole(1000, 12000).unwrap();
        let mut buf = vec![0xFFu8; 20000];
        f.read_exact_at(&mut buf, 0).unwrap();
        assert!(buf[..100].iter().all(|&b| b == 0xAA));
        assert!(buf[100..400].iter().all(|&b| b == 0));
        assert!(buf[400..1000].iter().all(|&b| b == 0xAA));
        assert!(buf[1000..13000].iter().all(|&b| b == 0));
        assert!(buf[13000..].iter().all(|&b| b == 0xAA));
        assert_eq!(f.file_len().unwrap(), 20000);
    }

    #[test]
    fn syncs_succeed_and_clone_shares_data() {
        let tmp = TempFile::new().unwrap();
        let f = ImageFile::new(tmp.as_file().try_clone().unwrap());
        f.write_all_at(b"abc", 0).unwrap();
        f.sync_all().unwrap();
        f.barrier_sync().unwrap();
        f.full_sync().unwrap();
        let g = f.try_clone().unwrap();
        let mut b = [0u8; 3];
        g.read_exact_at(&mut b, 0).unwrap();
        assert_eq!(&b, b"abc");
        assert!(f.is_writable());
        barrier_fsync(tmp.as_file()).unwrap();
        full_fsync(tmp.as_file()).unwrap();
    }
}
