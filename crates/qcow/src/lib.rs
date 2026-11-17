// Copyright 2026 The Cloud Hypervisor Authors. All rights reserved.
//
// SPDX-License-Identifier: Apache-2.0

//! QCOW2 disk image format, vendored from Cloud Hypervisor (see README.md).
//!
//! Provides [`QcowDisk`], the opened image: parsed metadata, the data file and an optional
//! backing file. Unlike upstream it has no async engine; the read, write and deallocate drivers
//! are the slice-based helpers in `common.rs` (the facade is added in a later step).

mod backing;
mod common;
mod decoder;
mod error;
pub mod file;
mod header;
mod metadata;
mod overlay;
mod parser;
mod qcow_raw_file;
mod refcount;
#[cfg(test)]
mod test_util;
mod util;
mod vec_cache;

#[cfg(test)]
use std::fs::File;
use std::io;
#[cfg(test)]
use std::path::Path;
use std::sync::Arc;

pub use error::{BlockError, BlockErrorKind, BlockResult, ErrorContext, ErrorOp};
pub use overlay::{create_overlay, growth_bound, refcount_coverage};
pub use parser::{
    BackingFile, BackingFileConfig, CompressionType, Error, ImageType, IncompatFeatures,
    MissingFeatureError, QcowHeader,
};
use thiserror::Error as ThisError;

use self::backing::shared_backing_from;
use self::file::{ImageFile, SyncKind};
use self::metadata::{BackingRead, QcowMetadata};
use self::parser::parse_qcow_with;
#[cfg(test)]
use self::parser::{MAX_NESTING_DEPTH, parse_qcow};
use self::qcow_raw_file::QcowRawFile;

/// Errors from [`QcowDisk`] operations that have no qcow-specific cause.
#[derive(Debug, ThisError)]
pub enum DiskFileError {
    #[error("Failed to clone the image file")]
    Clone(#[source] io::Error),
    #[error("Failed to resize the image")]
    ResizeError(#[source] io::Error),
    #[error("Failed to sync metadata")]
    SyncMetadata(#[source] io::Error),
}

/// An opened QCOW2 image.
///
/// Holds the in memory QCOW2 metadata, the data file, and an optional backing file. The metadata
/// is wrapped in an `Arc` so several users (worker threads) can share it; the backing file is
/// likewise shared.
pub struct QcowDisk {
    metadata: Arc<QcowMetadata>,
    backing_file: Option<Arc<dyn BackingRead>>,
    sparse: bool,
    data_raw_file: QcowRawFile,
    /// natemu: the punching time left to deallocations (see `common::PunchBudget`).
    punch_budget: common::PunchBudget,
}

impl std::fmt::Debug for QcowDisk {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("QcowDisk")
            .field("sparse", &self.sparse)
            .field("has_backing", &self.backing_file.is_some())
            .finish_non_exhaustive()
    }
}

impl QcowDisk {
    /// natemu: open `file` with the backing file its header names, which the caller opened
    /// ([`BackingFile::raw`], [`BackingFile::qcow2`]); `None` when the header names none. A
    /// writable file gets the dirty bit (synced) before this returns, and a rebuild of its
    /// refcounts if it was found dirty; a read-only one is never written.
    pub fn open(file: ImageFile, backing: Option<BackingFile>) -> BlockResult<Self> {
        let (inner, backing_file, sparse) =
            parse_qcow_with(file, backing, true).map_err(|e| e.with_op(ErrorOp::Open))?;
        Self::from_state(inner, backing_file, sparse)
    }

    fn from_state(
        inner: metadata::QcowState,
        backing_file: Option<parser::BackingFile>,
        sparse: bool,
    ) -> BlockResult<Self> {
        let data_raw_file = inner
            .raw_file
            .try_clone()
            .map_err(|e| BlockError::new(BlockErrorKind::Io, parser::Error::CloneBackingFile(e)))?;
        Ok(QcowDisk {
            metadata: Arc::new(QcowMetadata::new(inner)),
            backing_file: backing_file.map(shared_backing_from).transpose()?,
            sparse,
            data_raw_file,
            punch_budget: common::PunchBudget::new(),
        })
    }

    /// Upstream's opener: `backing_files` allows following backing files (to
    /// `MAX_NESTING_DEPTH`) by the paths in their headers. natemu: tests only.
    #[cfg(test)]
    pub fn new(file: File, backing_files: bool, sparse: bool) -> BlockResult<Self> {
        Self::from_image_file(ImageFile::new(file), backing_files, sparse)
    }

    /// Like [`QcowDisk::new`], recording the `path` the file was opened from.
    #[cfg(test)]
    pub fn with_path(
        file: File,
        path: &Path,
        backing_files: bool,
        sparse: bool,
    ) -> BlockResult<Self> {
        Self::from_image_file(ImageFile::with_path(file, path), backing_files, sparse)
    }

    /// Open on an arbitrary file layer.
    #[cfg(test)]
    pub fn from_image_file(
        raw_file: ImageFile,
        backing_files: bool,
        sparse: bool,
    ) -> BlockResult<Self> {
        let max_nesting_depth = if backing_files { MAX_NESTING_DEPTH } else { 0 };
        let (inner, backing_file, sparse) = parse_qcow(raw_file, max_nesting_depth, sparse)
            .map_err(|e| {
                let e = if !backing_files && matches!(e.kind(), BlockErrorKind::Overflow) {
                    e.with_kind(BlockErrorKind::UnsupportedFeature)
                } else {
                    e
                };
                e.with_op(ErrorOp::Open)
            })?;
        Self::from_state(inner, backing_file, sparse)
    }

    pub fn logical_size(&self) -> u64 {
        self.metadata.virtual_size()
    }

    pub fn physical_size(&self) -> io::Result<u64> {
        self.data_raw_file.physical_size()
    }

    pub fn resize(&mut self, size: u64) -> BlockResult<()> {
        if self.backing_file.is_some() {
            return Err(BlockError::new(
                BlockErrorKind::UnsupportedFeature,
                DiskFileError::ResizeError(io::Error::other(
                    "resize not supported with backing files",
                )),
            )
            .with_op(ErrorOp::Resize));
        }
        self.metadata.resize(size).map_err(|e| {
            BlockError::new(BlockErrorKind::Io, DiskFileError::ResizeError(e))
                .with_op(ErrorOp::Resize)
        })
    }

    #[cfg(test)]
    pub fn sync_metadata(&self) -> BlockResult<()> {
        self.sync(SyncKind::Fsync)
            .map_err(|e| BlockError::new(BlockErrorKind::Io, DiskFileError::SyncMetadata(e)))
    }

    /// natemu: a guest FLUSH (`Fsync`) or a full sync, in the write order of the design (section
    /// 4): data, barrier, L2 tables and refcount blocks, barrier, L1 and refcount tables (all
    /// under the table lock), then `kind` with the lock released, so reads and writes go on
    /// meanwhile. Clusters freed by those tables return to the free list only after the sync
    /// succeeds and, after a plain fsync, an F_BARRIERFSYNC: on macOS fsync orders nothing
    /// against a power loss, so without the barrier guest data written into a reused cluster
    /// could reach the disk before the tables that stopped pointing at it. Callers serialise syncs.
    pub fn sync(&self, kind: SyncKind) -> io::Result<()> {
        let freed = self.metadata.write_tables()?;
        self.data_raw_file.file().sync(kind)?;
        if !freed.is_empty() && kind == SyncKind::Fsync {
            self.data_raw_file.file().barrier_sync()?;
        }
        self.metadata.release(freed);
        Ok(())
    }

    /// natemu: the clean close of a power-off: the tables, F_FULLFSYNC, the dirty bit cleared,
    /// F_FULLFSYNC. Any other exit (drop) writes the tables without a sync and keeps the dirty
    /// bit. A later change must call [`Self::mark_dirty`] first.
    pub fn close_clean(&self) -> io::Result<()> {
        self.metadata.close_clean()
    }

    /// natemu: set the dirty bit again (synced) after [`Self::close_clean`].
    pub fn mark_dirty(&self) -> io::Result<()> {
        self.metadata.mark_dirty()
    }

    /// natemu: whether a table write failed; the image's on-disk tables are then unknown.
    pub fn tables_failed(&self) -> bool {
        self.metadata.tables_failed()
    }

    /// The cluster size in bytes.
    pub fn cluster_size(&self) -> u64 {
        self.metadata.cluster_size()
    }

    /// Synchronous guest-offset write (copy-on-write over a backing file as needed).
    pub fn write_all_at(&self, offset: u64, data: &[u8]) -> io::Result<()> {
        common::cow_write_at(
            offset,
            data,
            &self.metadata,
            &self.data_raw_file,
            &self.backing_file,
            self.metadata.cluster_size(),
        )
    }

    /// Synchronous guest-offset read; holes and zero clusters read as zeros.
    pub fn read_exact_at(&self, offset: u64, buf: &mut [u8]) -> io::Result<()> {
        let mappings =
            self.metadata
                .map_clusters_for_read(offset, buf.len(), self.backing_file.is_some())?;
        common::scatter_read(
            mappings,
            buf,
            &self.data_raw_file,
            &self.backing_file,
            self.metadata.cluster_size(),
            &*self.metadata.decoder(),
        )
    }

    /// Deallocate or zero `[offset, offset + length)`.
    pub fn deallocate(&self, offset: u64, length: usize, write_zeroes: bool) -> io::Result<()> {
        common::deallocate_range(
            &self.metadata,
            &self.data_raw_file,
            offset,
            length,
            self.sparse,
            write_zeroes,
            self.backing_file.as_deref(),
            &self.punch_budget,
        )
    }

    #[cfg(test)]
    fn read_all_at(&self, offset: u64, len: usize) -> Vec<u8> {
        let mut buf = vec![0u8; len];
        self.read_exact_at(offset, &mut buf).unwrap();
        buf
    }

    #[cfg(test)]
    fn metadata(&self) -> &QcowMetadata {
        &self.metadata
    }
}

/// Writes a fresh qcow2 layout with an explicit cluster size into `file`.
#[cfg(test)]
pub(crate) fn create_image_with_cluster_bits(
    file: &File,
    virtual_size: u64,
    backing_config: Option<&BackingFileConfig>,
    cluster_bits: u32,
) -> BlockResult<()> {
    let path = backing_config.map(|cfg| cfg.path.as_str());
    let mut header =
        QcowHeader::create_for_size_path_and_cluster_bits(3, virtual_size, path, cluster_bits)
            .map_err(|e| BlockError::new(BlockErrorKind::Io, e))?;
    if let Some(cfg) = backing_config
        && let Some(backing_file) = &mut header.backing_file
    {
        backing_file.format = cfg.format;
    }
    let raw = ImageFile::new(
        file.try_clone()
            .map_err(|e| BlockError::new(BlockErrorKind::Io, DiskFileError::Clone(e)))?,
    );
    header
        .write_to(&raw)
        .map_err(|e| BlockError::new(BlockErrorKind::Io, e))?;
    let (inner, _backing, _sparse) = parse_qcow(raw, MAX_NESTING_DEPTH, true)?;
    // Flush dirty caches and clear the dirty bit
    QcowMetadata::new(inner).close_clean()?;
    Ok(())
}

/// Helper struct to create a new qcow2 image in a temporary file.
#[cfg(test)]
pub(crate) struct QcowTempDisk {
    tmp: test_util::TempFile,
    disk: QcowDisk,
}

#[cfg(test)]
impl QcowTempDisk {
    /// Creates a new qcow2 image in a temporary file with optional backing file.
    pub fn new(
        virtual_size: u64,
        backing_config: Option<&BackingFileConfig>,
        sparse: bool,
    ) -> BlockResult<Self> {
        Self::new_with_cluster_bits(
            virtual_size,
            backing_config,
            sparse,
            header::DEFAULT_CLUSTER_BITS,
        )
    }

    /// Creates a new qcow2 image with an explicit cluster size.
    pub fn new_with_cluster_bits(
        virtual_size: u64,
        backing_config: Option<&BackingFileConfig>,
        sparse: bool,
        cluster_bits: u32,
    ) -> BlockResult<Self> {
        let tmp = test_util::TempFile::new()?;
        create_image_with_cluster_bits(tmp.as_file(), virtual_size, backing_config, cluster_bits)?;
        let file = tmp
            .as_file()
            .try_clone()
            .map_err(|e| BlockError::new(BlockErrorKind::Io, DiskFileError::Clone(e)))?;
        let disk = QcowDisk::with_path(file, tmp.as_path(), backing_config.is_some(), sparse)?;
        Ok(Self { tmp, disk })
    }

    pub fn path(&self) -> &Path {
        self.tmp.as_path()
    }

    pub fn as_file(&self) -> &File {
        self.tmp.as_file()
    }

    pub fn disk(&self) -> &QcowDisk {
        &self.disk
    }

    /// Drops the disk handle and returns the underlying TempFile.
    pub(crate) fn into_tempfile(self) -> test_util::TempFile {
        self.tmp
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::FileExt;

    use super::*;

    const TEST_SIZE: u64 = 0x5566_7788;

    fn make_qcow_file() -> File {
        QcowTempDisk::new(TEST_SIZE, None, true)
            .unwrap()
            .into_tempfile()
            .into_file()
    }

    fn dirty_bit_is_set(file: &File) -> bool {
        let mut buf = [0u8; 8];
        file.read_exact_at(&mut buf, header::V2_BARE_HEADER_SIZE as u64)
            .unwrap();
        u64::from_be_bytes(buf) & IncompatFeatures::DIRTY.bits() != 0
    }

    #[test]
    fn new_returns_correct_size() {
        let file = make_qcow_file();
        let disk = QcowDisk::new(file, false, true).unwrap();
        assert_eq!(disk.logical_size(), TEST_SIZE);
    }

    #[test]
    fn metadata_clone_does_not_clear_dirty_bit() {
        let file = make_qcow_file();
        let disk = QcowDisk::new(file, false, true).unwrap();
        let cloned = Arc::clone(&disk.metadata);

        drop(cloned);

        assert_ne!(
            disk.metadata().header().incompatible_features & IncompatFeatures::DIRTY.bits(),
            0
        );
    }

    // natemu: upstream cleared the dirty bit when the last owner dropped; here only a clean
    // close does (the power-off drain), and a drop leaves it set.
    #[test]
    fn dirty_bit_cleared_only_by_a_clean_close() {
        let file = make_qcow_file();
        let inspect = file.try_clone().unwrap();
        let disk = QcowDisk::new(file, false, true).unwrap();
        let other_owner = Arc::clone(&disk.metadata);

        drop(disk);
        assert!(dirty_bit_is_set(&inspect));
        drop(other_owner);
        assert!(dirty_bit_is_set(&inspect));

        let disk = QcowDisk::new(inspect.try_clone().unwrap(), false, true).unwrap();
        disk.write_all_at(0, &[1; 512]).unwrap();
        disk.close_clean().unwrap();
        assert!(!dirty_bit_is_set(&inspect));
        disk.mark_dirty().unwrap();
        assert!(dirty_bit_is_set(&inspect));
        disk.close_clean().unwrap();
        drop(disk);
        assert!(!dirty_bit_is_set(&inspect));
    }

    #[test]
    fn physical_size_less_than_logical() {
        // make_qcow_file() writes no guest data, so the file on disk
        // only contains QCOW2 headers and metadata tables.
        let file = make_qcow_file();
        let disk = QcowDisk::new(file, false, true).unwrap();
        assert!(disk.physical_size().unwrap() < disk.logical_size());
    }

    // natemu-written tests for the read/write/deallocate drivers and backing files; they stand in
    // for the upstream engine_sync tests, which were dropped with the engine (see README.md).
    const CL: u64 = 1 << header::DEFAULT_CLUSTER_BITS;

    fn pattern(len: usize, seed: u8) -> Vec<u8> {
        (0..len)
            .map(|i| (i as u8).wrapping_mul(7).wrapping_add(seed))
            .collect()
    }

    fn raw_backing(dir: &test_util::TempDir, name: &str, data: &[u8]) -> std::path::PathBuf {
        let path = dir.as_path().join(name);
        std::fs::write(&path, data).unwrap();
        path
    }

    fn overlay_in(
        dir: &test_util::TempDir,
        name: &str,
        size: u64,
        backing: &str,
        format: ImageType,
    ) -> File {
        let path = dir.as_path().join(name);
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        let cfg = BackingFileConfig {
            path: backing.to_string(),
            format: Some(format),
        };
        create_image_with_cluster_bits(&file, size, Some(&cfg), header::DEFAULT_CLUSTER_BITS)
            .unwrap();
        file
    }

    #[test]
    fn raw_backing_cow_keeps_unwritten_bytes() {
        let dir = test_util::TempDir::new().unwrap();
        let base = pattern(4 * CL as usize, 1);
        let bpath = raw_backing(&dir, "base.raw", &base);
        let f = overlay_in(
            &dir,
            "o.qcow2",
            4 * CL,
            bpath.to_str().unwrap(),
            ImageType::Raw,
        );
        let disk = QcowDisk::new(f, true, true).unwrap();
        assert_eq!(disk.read_all_at(0, base.len()), base);
        disk.write_all_at(CL + 100, b"0123456789abcdef").unwrap();
        let mut expect = base.clone();
        expect[CL as usize + 100..CL as usize + 116].copy_from_slice(b"0123456789abcdef");
        assert_eq!(disk.read_all_at(0, base.len()), expect);
    }

    #[test]
    fn write_zeroes_over_backing_reads_zero_and_partial_write_keeps_it() {
        let dir = test_util::TempDir::new().unwrap();
        let base = vec![0xA5u8; 4 * CL as usize];
        let bpath = raw_backing(&dir, "base.raw", &base);
        let f = overlay_in(
            &dir,
            "o.qcow2",
            4 * CL,
            bpath.to_str().unwrap(),
            ImageType::Raw,
        );
        let disk = QcowDisk::new(f, true, true).unwrap();
        disk.deallocate(CL, CL as usize, true).unwrap();
        assert!(disk.read_all_at(CL, CL as usize).iter().all(|&b| b == 0));
        // A partial write into the zeroed cluster must not bring the backing data back.
        disk.write_all_at(CL + 10, b"xy").unwrap();
        let got = disk.read_all_at(CL, CL as usize);
        assert_eq!(&got[10..12], b"xy");
        assert!(got[..10].iter().all(|&b| b == 0) && got[12..].iter().all(|&b| b == 0));
        assert!(disk.read_all_at(0, CL as usize).iter().all(|&b| b == 0xA5));
        // Discard (not write-zeroes) over a backing file must also read back as zeros.
        disk.deallocate(2 * CL, CL as usize, false).unwrap();
        assert!(
            disk.read_all_at(2 * CL, CL as usize)
                .iter()
                .all(|&b| b == 0)
        );
    }

    #[test]
    fn relative_backing_resolves_against_image_directory_only_with_a_path() {
        let dir = test_util::TempDir::new().unwrap();
        let base = pattern(2 * CL as usize, 3);
        raw_backing(&dir, "base.raw", &base);
        // Header only: creating through create_image would open the backing file by bare name.
        let opath = dir.as_path().join("o.qcow2");
        let f = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&opath)
            .unwrap();
        let mut h = QcowHeader::create_for_size_and_path(3, 2 * CL, Some("base.raw")).unwrap();
        h.backing_file.as_mut().unwrap().format = Some(ImageType::Raw);
        h.write_to(&ImageFile::new(f.try_clone().unwrap())).unwrap();
        // Open once through the path to give the image its tables, as a real image has.
        let disk = QcowDisk::with_path(f.try_clone().unwrap(), &opath, true, true).unwrap();
        drop(disk);
        let disk = QcowDisk::with_path(f.try_clone().unwrap(), &opath, true, true).unwrap();
        assert_eq!(disk.read_all_at(0, base.len()), base);
        // Without a path the name is resolved against the working directory and is missing.
        assert!(QcowDisk::new(f, true, true).is_err());
    }

    #[test]
    fn qcow2_backing_chain_reads_through() {
        let dir = test_util::TempDir::new().unwrap();
        let base = QcowTempDisk::new(4 * CL, None, true).unwrap();
        let data = pattern(CL as usize, 9);
        base.disk().write_all_at(CL, &data).unwrap();
        base.disk().sync_metadata().unwrap();
        let bpath = base.path().to_str().unwrap().to_string();
        let f = overlay_in(&dir, "o.qcow2", 4 * CL, &bpath, ImageType::Qcow2);
        let disk = QcowDisk::new(f, true, true).unwrap();
        assert_eq!(disk.read_all_at(CL, CL as usize), data);
        assert!(disk.read_all_at(0, CL as usize).iter().all(|&b| b == 0));
        // Without backing_files=true a backing file is refused, not ignored.
        let f2 = overlay_in(&dir, "o2.qcow2", 4 * CL, &bpath, ImageType::Qcow2);
        assert!(QcowDisk::new(f2, false, true).is_err());
    }
}
