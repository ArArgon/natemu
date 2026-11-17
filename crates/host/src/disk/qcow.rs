//! The qcow2 backend: the vendored Cloud Hypervisor code (`natemu-qcow`) behind the [`DiskBackend`] seam, with the
//! write order, locks, strict opener and backing-file policy of docs/m1-config-and-qcow2-design.md, section 4.
//!
//! Write order, one rule everywhere (a guest FLUSH, a cache eviction, an exit): (1) data, (2) F_BARRIERFSYNC, (3) dirty
//! L2 tables and refcount blocks, (4) F_BARRIERFSYNC, (5) L1 table, refcount table and header, (6) fsync for a guest
//! FLUSH or F_FULLFSYNC at power-off. Steps 2-5 run under the table lock inside `natemu-qcow`, step 6 outside it, and
//! clusters freed by the tables of steps 3-5 are reused only after step 6 returns. The dirty bit is set and synced at
//! a writable open and stays set until a clean power-off, so refcounts need no ordering: any other end (a crash, a
//! kill, an exit without power-off) is repaired by a refcount rebuild at the next writable open.
use super::{DiskBackend, Format, Image, detect, raw};
use crate::Result;
use natemu_qcow::file::{ImageFile, SyncKind};
use natemu_qcow::{BackingFile, ImageType, QcowDisk, QcowHeader};
use std::{
    fs::{File, OpenOptions},
    io,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::{
        RwLock, RwLockReadGuard,
        atomic::{AtomicBool, Ordering::SeqCst},
    },
};

/// The longest backing chain followed (backing files below the top image).
const MAX_DEPTH: usize = 16;

/// The qcow2 image on `file` (opened and locked by [`raw::inspect`]: exclusive when writable, shared when read-only)
/// at `path`, its backing chain opened by the policy in [`chain`].
pub(super) fn image(
    file: File,
    path: &Path,
    read_only: bool,
    backing: Option<&Path>,
) -> Result<Image> {
    let info = file
        .metadata()
        .map_err(|e| format!("disk {}: {e}", path.display()))?;
    let top = ImageFile::with_path(file, path);
    let header = QcowHeader::new(&top).map_err(|e| refused(path, &e))?;
    check(&header, path, read_only)?;
    let seen = vec![(info.dev(), info.ino())];
    let chain = chain(path, &header, backing, seen)?;
    let qcow =
        Qcow::open(top, chain, read_only).map_err(|e| format!("disk {}: {e}", path.display()))?;
    Ok(Image {
        backend: Box::new(qcow),
        capacity: header.size,
        read_only,
        discard: !read_only,
        write_zeroes: !read_only,
        id: raw::id(&info),
    })
}

/// The strict opener's refusals that the vendored parser does not make, or makes without saying what to do. Encryption,
/// an external data file, extended L2 entries, a compression type other than zlib and unknown incompatible bits are
/// refused by [`QcowHeader::new`] already.
fn check(header: &QcowHeader, path: &Path, read_only: bool) -> Result<()> {
    let p = path.display();
    if header.size == 0 || !header.size.is_multiple_of(512) {
        return Err(format!(
            "disk {p}: qcow2 virtual size {} is not a nonzero multiple of 512",
            header.size
        ));
    }
    if read_only {
        return Ok(());
    }
    if header.version == 2 {
        return Err(format!(
            "disk {p}: a qcow2 version 2 image opens read-only only (no zero flag, no dirty bit); \
             to write it, upgrade it with qemu-img amend -o compat=1.1 {p}"
        ));
    }
    if header.is_corrupt() {
        return Err(format!(
            "disk {p}: the image is marked corrupt; open it read-only, or repair it with qemu-img check -r all {p}"
        ));
    }
    if header.nb_snapshots != 0 {
        return Err(format!(
            "disk {p}: the image has internal snapshots, which natemu does not write; open it read-only, \
             or copy it without them (qemu-img convert -O qcow2)"
        ));
    }
    // The vendored code never grows the refcount table, so allocation fails once the file outgrows what it covers
    // (qemu-img gives a small image a one-cluster table: about 4 GiB of file at 4 KiB clusters). Refuse up front
    // when the table cannot cover everything the image can need: every data cluster, its L2 tables twice (a moved
    // table and its old copy until a flush), the L1 table and header, the refcount blocks twice, and some slack.
    let cluster = 1u64 << header.cluster_bits;
    let per_block = cluster * 8 / (1u64 << header.refcount_order);
    let covered = (u64::from(header.refcount_table_clusters) * cluster / 8)
        .saturating_mul(per_block)
        .saturating_mul(cluster);
    let data = header.size.div_ceil(cluster);
    let l2 = data.div_ceil(cluster / 8);
    let clusters = data + 2 * l2 + l2.div_ceil(cluster / 8) + 64;
    let need = (clusters + 2 * clusters.div_ceil(per_block)).saturating_mul(cluster);
    if covered < need {
        return Err(format!(
            "disk {p}: its refcount table covers {covered} bytes of file, less than the {need} this image can grow to, \
             and natemu cannot grow it; copy the image (qemu-img convert -O qcow2 -o cluster_size=65536 {p} ...)"
        ));
    }
    Ok(())
}

/// One line naming the file: the vendored error and its causes.
fn refused(path: &Path, error: &dyn std::error::Error) -> String {
    format!("disk {}: qcow2: {}", path.display(), causes(error))
}

/// The error and its causes on one line (feature names in the message come from the image).
fn causes(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut cause = error.source();
    while let Some(e) = cause {
        text = format!("{text}: {e}");
        cause = e.source();
    }
    text.chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect()
}

/// A backing name from `referrer`'s header, resolved as qcow2 does: relative to the referring image's folder.
fn resolve(referrer: &Path, name: &str) -> PathBuf {
    let name = Path::new(name);
    let path = if name.is_absolute() {
        name.to_path_buf()
    } else {
        referrer.parent().unwrap_or(Path::new("")).join(name)
    };
    std::path::absolute(&path).unwrap_or(path)
}

/// The top image's backing chain. Its embedded backing name is followed only when it is the file the caller's
/// `backing` names (same device and inode: a downloaded image could otherwise name any host file); the backing is
/// then read through the descriptor opened from `backing`, never through the header's name. Its format is the
/// header's backing-format extension, or else its detected magic (a file the caller chose).
fn chain(
    path: &Path,
    header: &QcowHeader,
    backing: Option<&Path>,
    mut seen: Vec<(u64, u64)>,
) -> Result<Option<BackingFile>> {
    let name = path
        .file_name()
        .map_or(path.to_string_lossy(), |n| n.to_string_lossy());
    let (named, backing) = match (&header.backing_file, backing) {
        (None, None) => return Ok(None),
        (None, Some(b)) => {
            return Err(format!(
                "disk {}: the image has no backing file, but backing {} is given",
                path.display(),
                b.display()
            ));
        }
        (Some(c), None) => {
            let named = resolve(path, &c.path);
            return Err(format!(
                "disk {}: {name} is based on {n}; add backing = \"{n}\" to this disk (or --set disk.ID.backing={n})",
                path.display(),
                n = named.display()
            ));
        }
        (Some(c), Some(b)) => (c, b),
    };
    let resolved = resolve(path, &named.path);
    let (file, head, tail) = raw::inspect(backing, true)?;
    let info = file
        .metadata()
        .map_err(|e| format!("disk {}: {e}", backing.display()))?;
    let id = (info.dev(), info.ino());
    // The header's name is only compared, by identity (stat follows a symbolic link to the same file).
    let same = std::fs::metadata(&resolved).is_ok_and(|m| (m.dev(), m.ino()) == id);
    if !same {
        return Err(format!(
            "disk {}: {name} is based on {}, not on the backing {}",
            path.display(),
            resolved.display(),
            backing.display()
        ));
    }
    let format = match named.format {
        Some(format) => format,
        None => match detect(&head, &tail) {
            Some(Format::Qcow2) => ImageType::Qcow2,
            None => ImageType::Raw,
            Some(Format::Refused(kind)) => {
                return Err(format!(
                    "disk {}: the backing file looks like a {kind} image; natemu reads raw and qcow2",
                    backing.display()
                ));
            }
        },
    };
    if seen.contains(&id) {
        return Err(format!(
            "disk {}: the image is its own backing file",
            path.display()
        ));
    }
    seen.push(id);
    open_backing(file, backing, format, &mut seen, 1).map(Some)
}

/// Open one backing file (`depth` below the top image) as `format`, following the links of a qcow2 one. The names
/// below the config-named base come from image headers nobody configured, so each link must ([`link_target`]) be a
/// relative name inside the referring image's own folder, carry a backing-format extension (a format is never
/// guessed from a file's contents), be a regular file reached without a final symbolic link, and not repeat a file
/// of the chain. A later config key could let a user opt in to other files: an allow-list by device and inode in
/// the config, not a looser rule here.
fn open_backing(
    file: File,
    path: &Path,
    format: ImageType,
    seen: &mut Vec<(u64, u64)>,
    depth: usize,
) -> Result<BackingFile> {
    let image = ImageFile::with_path(file, path);
    if format == ImageType::Raw {
        return BackingFile::raw(image).map_err(|e| refused(path, &e));
    }
    let header = QcowHeader::new(&image).map_err(|e| refused(path, &e))?;
    let next = match &header.backing_file {
        None => None,
        Some(link) => {
            let named = link_target(path, &link.path)?;
            let at = || format!("disk {}: backing file {}", path.display(), named.display());
            let Some(format) = link.format else {
                return Err(format!(
                    "{}: named without a backing format; natemu follows such a link only with one \
                     (qemu-img rebase -u -F raw|qcow2 -b ... {})",
                    at(),
                    path.display()
                ));
            };
            if depth >= MAX_DEPTH {
                return Err(format!(
                    "{}: the backing chain is deeper than {MAX_DEPTH}",
                    at()
                ));
            }
            let file = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                .open(&named)
                .map_err(|e| match e.raw_os_error() {
                    Some(libc::ELOOP) => format!("{}: a symbolic link (not followed)", at()),
                    _ => format!("{}: {e}", at()),
                })?;
            let info = file.metadata().map_err(|e| format!("{}: {e}", at()))?;
            if !info.is_file() {
                return Err(format!("{}: not a regular file", at()));
            }
            if seen.contains(&(info.dev(), info.ino())) {
                return Err(format!("{}: the backing chain loops", at()));
            }
            seen.push((info.dev(), info.ino()));
            raw::lock(&file, &named, true)?;
            Some(open_backing(file, &named, format, seen, depth + 1)?)
        }
    };
    BackingFile::qcow2(image, next).map_err(|e| refused(path, &e))
}

/// Where a link below the config-named base may lead: `name` must be relative, made of plain components (no `..`,
/// no `.`, not empty), and lead into the referring image's own folder or a folder below it after every directory
/// component is resolved (realpath), so neither `..` nor a symbolic link among the folders can leave it. The final
/// component is then opened with O_NOFOLLOW by the caller.
/// ponytail: the folders are resolved before the open, so a host process swapping a folder for a symbolic link in
/// between could still redirect it; per-component openat would close that if host-side races matter.
fn link_target(referrer: &Path, name: &str) -> Result<PathBuf> {
    let file = referrer
        .file_name()
        .map_or(referrer.to_string_lossy(), |n| n.to_string_lossy());
    let refuse = || {
        format!(
            "disk {}: {file} names {name} as its backing file; backing files below the configured base must be \
             relative paths inside {file}'s folder",
            referrer.display()
        )
    };
    let relative = Path::new(name);
    let plain = !name.is_empty()
        && relative
            .components()
            .all(|c| matches!(c, std::path::Component::Normal(_)))
        && !name
            .split('/')
            .any(|c| c.is_empty() || c == "." || c == "..");
    if !plain {
        return Err(refuse());
    }
    let folder = referrer
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let folder = std::fs::canonicalize(folder)
        .map_err(|e| format!("disk {}: its folder: {e}", referrer.display()))?;
    let joined = folder.join(relative);
    let (Some(parent), Some(last)) = (joined.parent(), joined.file_name()) else {
        return Err(refuse());
    };
    let parent = std::fs::canonicalize(parent).map_err(|e| {
        format!(
            "disk {}: backing file {}: {e}",
            referrer.display(),
            joined.display()
        )
    })?;
    if !parent.starts_with(&folder) {
        return Err(refuse());
    }
    Ok(parent.join(last))
}

/// The backend: one opened qcow2 image. The vendored code does its own table locking; see the module notes for the
/// write order.
struct Qcow {
    disk: QcowDisk,
    read_only: bool,
    cluster: u64,
    /// A table write or a sync failed: every later write and sync of the disk fails; reads go on.
    failed: AtomicBool,
    /// A power-off sync cleared the dirty bit: the next change sets it again first.
    clean: AtomicBool,
    /// Held shared by every change for its whole call and exclusively by the power-off sync, so no change can land
    /// after `close_clean` cleared the dirty bit. (The device also drains only once nothing is in flight; this
    /// keeps the backend safe without relying on that.)
    dirtying: RwLock<()>,
}

const FAILED: &str = "an earlier qcow2 table write or sync of this disk failed";

impl Qcow {
    /// Open `top` (a writable one gets the dirty bit, synced, and a refcount rebuild if it was found dirty) with the
    /// backing file its header names, already opened.
    fn open(top: ImageFile, backing: Option<BackingFile>, read_only: bool) -> io::Result<Self> {
        let disk = QcowDisk::open(top, backing)
            .map_err(|e| io::Error::other(format!("qcow2: {}", causes(&e))))?;
        Ok(Self {
            cluster: disk.cluster_size(),
            disk,
            read_only,
            failed: AtomicBool::new(false),
            clean: AtomicBool::new(false),
            dirtying: RwLock::new(()),
        })
    }
    /// Refuse a change to a read-only or failed image; set the dirty bit again after a clean close. The guard is
    /// held for the whole change.
    fn changing(&self) -> io::Result<RwLockReadGuard<'_, ()>> {
        if self.read_only {
            return Err(io::Error::from(io::ErrorKind::PermissionDenied));
        }
        if self.failed.load(SeqCst) {
            return Err(io::Error::other(FAILED));
        }
        loop {
            let held = self.dirtying.read().unwrap_or_else(|e| e.into_inner());
            if !self.clean.load(SeqCst) {
                return Ok(held);
            }
            drop(held);
            let _only = self.dirtying.write().unwrap_or_else(|e| e.into_inner());
            if self.clean.load(SeqCst) {
                self.disk
                    .mark_dirty()
                    .inspect_err(|_| self.failed.store(true, SeqCst))?;
                self.clean.store(false, SeqCst);
            }
        }
    }
    /// An error of a call that may have written tables (an eviction): a failed table write fails the disk.
    fn noted(&self, e: io::Error) -> io::Error {
        if self.disk.tables_failed() {
            self.failed.store(true, SeqCst);
        }
        e
    }
}

impl DiskBackend for Qcow {
    fn read(&self, buf: &mut [u8], offset: u64) -> io::Result<usize> {
        self.disk
            .read_exact_at(offset, buf)
            .map_err(|e| self.noted(e))?;
        Ok(buf.len())
    }
    fn write(&self, buf: &[u8], offset: u64) -> io::Result<usize> {
        let _held = self.changing()?;
        self.disk
            .write_all_at(offset, buf)
            .map_err(|e| self.noted(e))?;
        Ok(buf.len())
    }
    /// A guest FLUSH (fsync) or the power-off drain (F_FULLFSYNC, then the dirty bit cleared and synced again).
    fn sync(&self, full: bool) -> io::Result<()> {
        if self.read_only {
            return Ok(());
        }
        if self.failed.load(SeqCst) {
            return Err(io::Error::other(FAILED));
        }
        let result = if full {
            let _only = self.dirtying.write().unwrap_or_else(|e| e.into_inner());
            self.disk
                .close_clean()
                .map(|()| self.clean.store(true, SeqCst))
        } else {
            self.disk.sync(SyncKind::Fsync)
        };
        result.inspect_err(|_| self.failed.store(true, SeqCst))
    }
    /// The whole clusters inside the range are freed (and then read as zeros, also over a backing file); partial
    /// clusters at the ends are left as they are (a hint).
    /// Upstream race, kept: a write mapped to a cluster just before an overlapping discard frees it can land after
    /// the cluster is reused for another address (it needs a flush in between). Only overlapping in-flight requests
    /// can do that, and virtio-blk promises nothing for them.
    fn discard(&self, offset: u64, len: u64) -> io::Result<()> {
        let _held = self.changing()?;
        let (start, end) = (
            offset.next_multiple_of(self.cluster),
            (offset + len) / self.cluster * self.cluster,
        );
        if start >= end {
            return Ok(());
        }
        self.disk
            .deallocate(start, (end - start) as usize, false)
            .map_err(|e| self.noted(e))
    }
    /// Whole clusters get the zero flag (table updates only), partial ones zeros written.
    fn write_zeroes(&self, offset: u64, len: u64) -> io::Result<()> {
        let _held = self.changing()?;
        self.disk
            .deallocate(offset, len as usize, true)
            .map_err(|e| self.noted(e))
    }
}

#[cfg(test)]
#[path = "qcow_tests.rs"]
mod tests;
