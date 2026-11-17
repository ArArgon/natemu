//! Disk backends: the seam between the virtio-blk device (`virtio::block`) and the image formats. The device's
//! workers call a [`DiskBackend`] from several threads at once; [`open`] detects the image's format and returns an
//! [`Image`]: the backend plus what the device offers for it.
use crate::Result;
use std::path::Path;

mod qcow;
mod raw;

/// The workers' calls on an image. Every method may run on the four workers (and a lone read or write on the owner
/// thread) at once: a backend does its own locking.
pub(crate) trait DiskBackend: Send + Sync {
    /// One positioned read: a short count is returned as is.
    fn read(&self, buf: &mut [u8], offset: u64) -> std::io::Result<usize>;
    /// One positioned write: a short count is returned as is.
    fn write(&self, buf: &[u8], offset: u64) -> std::io::Result<usize>;
    /// `full`: to stable storage (F_FULLFSYNC, the power-off drain's); otherwise to the host OS (fsync, a guest FLUSH).
    fn sync(&self, full: bool) -> std::io::Result<()>;
    /// A hint: Ok when nothing was freed; freed ranges may then read as zeros.
    fn discard(&self, _offset: u64, _len: u64) -> std::io::Result<()> {
        Ok(())
    }
    /// Afterwards the range reads as zeros; the backend may deallocate it.
    fn write_zeroes(&self, offset: u64, len: u64) -> std::io::Result<()>;
    /// [`Self::write_zeroes`] where the backend's data writes go through `write` (a test wrapper that observes them).
    fn write_zeroes_with(
        &self,
        offset: u64,
        len: u64,
        _write: &dyn Fn(&[u8], u64) -> std::io::Result<usize>,
    ) -> std::io::Result<()> {
        self.write_zeroes(offset, len)
    }
}
impl DiskBackend for Box<dyn DiskBackend> {
    fn read(&self, buf: &mut [u8], offset: u64) -> std::io::Result<usize> {
        (**self).read(buf, offset)
    }
    fn write(&self, buf: &[u8], offset: u64) -> std::io::Result<usize> {
        (**self).write(buf, offset)
    }
    fn sync(&self, full: bool) -> std::io::Result<()> {
        (**self).sync(full)
    }
    fn discard(&self, offset: u64, len: u64) -> std::io::Result<()> {
        (**self).discard(offset, len)
    }
    fn write_zeroes(&self, offset: u64, len: u64) -> std::io::Result<()> {
        (**self).write_zeroes(offset, len)
    }
    fn write_zeroes_with(
        &self,
        offset: u64,
        len: u64,
        write: &dyn Fn(&[u8], u64) -> std::io::Result<usize>,
    ) -> std::io::Result<()> {
        (**self).write_zeroes_with(offset, len, write)
    }
}

/// An opened image and what the device offers for it.
pub(crate) struct Image {
    pub backend: Box<dyn DiskBackend>,
    /// Bytes: a nonzero multiple of 512, fixed for the launch.
    pub capacity: u64,
    /// Offer the read-only bit and no flush, discard or write-zeroes.
    pub read_only: bool,
    pub discard: bool,
    pub write_zeroes: bool,
    /// GET_ID's answer.
    pub id: [u8; 20],
}

/// qcow2's magic: "QFI" and 0xfb.
pub(crate) const QCOW2_MAGIC: [u8; 4] = *b"QFI\xfb";

/// An image format `head` (the first sector) and `tail` (the last) show; `None` is raw.
enum Format {
    Qcow2,
    /// Recognised but not read: the guest would see its container, not a disk.
    Refused(&'static str),
}
fn detect(head: &[u8], tail: &[u8]) -> Option<Format> {
    let name = if head.starts_with(&QCOW2_MAGIC) {
        return Some(Format::Qcow2);
    } else if head.starts_with(b"KDMV") || head.starts_with(b"# Disk DescriptorFile") {
        "VMDK"
    } else if head.starts_with(b"vhdxfile") {
        "VHDX"
    } else if head.get(64..68) == Some(&[0x7f, 0x10, 0xda, 0xbe]) {
        "VDI"
    } else if tail.starts_with(b"conectix") || head.starts_with(b"conectix") {
        "VHD"
    } else if tail.starts_with(b"koly") {
        "DMG"
    } else {
        return None;
    };
    Some(Format::Refused(name))
}

/// Open the image at `path` (`read_only`: shared lock, nothing offered that writes) by its detected format. `backing`
/// names a backing file; only qcow2 has one, and a qcow2 image that names one opens only when it is this file. With
/// `backing`, a writable `path` that does not exist is first created as a new qcow2 overlay over it; a read-only one
/// never is.
pub(crate) fn open(path: &Path, read_only: bool, backing: Option<&Path>) -> Result<Image> {
    open_disk("ID", path, read_only, backing)
}

/// [`open`] for the disk with id `id`, which its messages name (the `--set disk.ID.backing` hint).
pub(crate) fn open_disk(
    id: &str,
    path: &Path,
    read_only: bool,
    backing: Option<&Path>,
) -> Result<Image> {
    if let Some(base) = backing
        && std::fs::symlink_metadata(path).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound)
    {
        if read_only {
            return Err(format!(
                "disk {}: does not exist; a read-only overlay is never created (drop readonly to create it over {})",
                path.display(),
                base.display()
            ));
        }
        qcow::create(path, base)?;
    }
    let (file, head, tail) = raw::inspect(path, read_only)?;
    match detect(&head, &tail) {
        None if backing.is_some() => Err(format!(
            "disk {}: a backing file needs a qcow2 image",
            path.display()
        )),
        None => raw::image(file, read_only),
        Some(Format::Qcow2) => qcow::image(file, path, read_only, backing, id),
        Some(Format::Refused(name)) => Err(format!(
            "disk {}: this looks like a {name} image; natemu reads raw and qcow2 (qemu-img convert -O qcow2 ...)",
            path.display()
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn temp(name: &str, bytes: &[u8]) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!("natemu-disk-{name}-{}", std::process::id()));
        std::fs::write(&path, bytes).unwrap();
        path
    }
    fn image(len: usize, at: usize, magic: &[u8]) -> Vec<u8> {
        let mut bytes = vec![0; len];
        bytes[at..at + magic.len()].copy_from_slice(magic);
        bytes
    }
    fn error(bytes: &[u8], name: &str) -> String {
        let path = temp(name, bytes);
        let result = open(&path, false, None);
        std::fs::remove_file(&path).unwrap();
        result.err().expect("refused")
    }
    #[test]
    fn detection_refuses_other_containers() {
        for (bytes, name) in [
            (image(4096, 0, b"KDMV"), "VMDK"),
            (image(4096, 0, b"vhdxfile"), "VHDX"),
            (image(4096, 64, &[0x7f, 0x10, 0xda, 0xbe]), "VDI"),
            (image(4096, 4096 - 512, b"conectix"), "VHD"),
            (image(4096, 4096 - 512, b"koly"), "DMG"),
        ] {
            let e = error(&bytes, name);
            assert!(
                e.contains(&format!("looks like a {name} image")) && e.contains("qemu-img convert"),
                "{e}"
            );
        }
    }
    #[test]
    fn anything_else_is_raw_and_a_backing_file_needs_qcow2() {
        let path = temp("raw", &image(4096, 0, b"ISO9660 or anything"));
        let image = open(&path, false, None).unwrap();
        assert!(!image.read_only && image.discard && image.write_zeroes && image.capacity == 4096);
        drop(image);
        assert!(open(&path, false, Some(&path)).is_err());
        std::fs::remove_file(&path).unwrap();
    }
}
