// SPDX-License-Identifier: Apache-2.0

//! natemu: the writer of a new overlay (a v3 image over a backing file). The vendored header code writes a backing
//! name but no backing-format extension and no refcounts, so a fresh image of its own is only usable after a rebuild;
//! this writes the complete layout `qemu-img create -f qcow2 -b BASE -F FMT` writes: the header cluster (header,
//! backing-format extension, end of extensions, backing name), the refcount table, one refcount block counting the
//! metadata clusters, then the L1 table, all zero entries. Feature-name tables are optional and left out.

use std::fs::File;
use std::io;
use std::os::unix::fs::FileExt;

use crate::ImageType;
use crate::header::{DEFAULT_CLUSTER_BITS, DEFAULT_REFCOUNT_ORDER, HEADER_EXT_BACKING_FORMAT};

/// File bytes an image of `size` bytes can grow to while natemu writes it: every data cluster, its L2 tables twice
/// (a moved table and its old copy until a flush), the L1 table and header, the refcount blocks twice, and some
/// slack. The vendored code never grows the refcount table, so the table must cover this much file.
pub fn growth_bound(size: u64, cluster_bits: u32, refcount_order: u32) -> u64 {
    let cluster = 1u64 << cluster_bits;
    let per_block = cluster * 8 / (1u64 << refcount_order);
    let data = size.div_ceil(cluster);
    let l2 = data.div_ceil(cluster / 8);
    let clusters = data + 2 * l2 + l2.div_ceil(cluster / 8) + 64;
    (clusters + 2 * clusters.div_ceil(per_block)).saturating_mul(cluster)
}

/// File bytes a refcount table of `clusters` clusters covers.
pub fn refcount_coverage(clusters: u64, cluster_bits: u32, refcount_order: u32) -> u64 {
    let cluster = 1u64 << cluster_bits;
    let per_block = cluster * 8 / (1u64 << refcount_order);
    (clusters * cluster / 8)
        .saturating_mul(per_block)
        .saturating_mul(cluster)
}

/// Write a new v3 overlay of virtual `size` bytes (a nonzero multiple of 512) into the empty `file`: 64 KiB clusters,
/// 16-bit refcounts, `backing` (the base's name, recorded as given) of format `format`, and a refcount table large
/// enough for [`growth_bound`]. The caller syncs.
pub fn create_overlay(file: &File, size: u64, backing: &str, format: ImageType) -> io::Result<()> {
    let bits = DEFAULT_CLUSTER_BITS;
    let order = DEFAULT_REFCOUNT_ORDER;
    let cluster = 1u64 << bits;
    let invalid = |m: String| io::Error::new(io::ErrorKind::InvalidInput, m);
    if size == 0 || !size.is_multiple_of(512) {
        return Err(invalid(format!(
            "virtual size {size} is not a nonzero multiple of 512"
        )));
    }
    let l1_size = size.div_ceil(cluster * (cluster / 8));
    let l1_size32 =
        u32::try_from(l1_size).map_err(|_| invalid(format!("size {size} is too large")))?;
    let l1_clusters = (l1_size * 8).div_ceil(cluster);
    let need = growth_bound(size, bits, order);
    let mut rt_clusters = 1u64;
    while refcount_coverage(rt_clusters, bits, order) < need {
        rt_clusters += 1;
    }
    let metadata = 1 + rt_clusters + 1 + l1_clusters;
    if metadata > cluster * 8 / (1 << order) {
        return Err(invalid(format!("size {size} is too large")));
    }
    let (rt, rb, l1) = (
        cluster,
        (1 + rt_clusters) * cluster,
        (2 + rt_clusters) * cluster,
    );

    // The header (104 bytes), the compression type and padding to 112, then the extensions.
    let format = format.to_string();
    let mut h = Vec::with_capacity(512);
    h.extend(0x5146_49fbu32.to_be_bytes()); // magic
    h.extend(3u32.to_be_bytes()); // version
    let name_at_slot = h.len();
    h.extend(0u64.to_be_bytes()); // backing_file_offset, below
    h.extend((backing.len() as u32).to_be_bytes());
    h.extend(bits.to_be_bytes());
    h.extend(size.to_be_bytes());
    h.extend(0u32.to_be_bytes()); // crypt_method
    h.extend(l1_size32.to_be_bytes());
    h.extend(l1.to_be_bytes());
    h.extend(rt.to_be_bytes());
    h.extend((rt_clusters as u32).to_be_bytes());
    h.extend(0u32.to_be_bytes()); // nb_snapshots
    h.extend(0u64.to_be_bytes()); // snapshots_offset
    h.extend([0u8; 24]); // incompatible, compatible, autoclear features
    h.extend(order.to_be_bytes());
    h.extend(112u32.to_be_bytes()); // header_length
    h.extend([0u8; 8]); // compression type zlib, padding
    h.extend(HEADER_EXT_BACKING_FORMAT.to_be_bytes());
    h.extend((format.len() as u32).to_be_bytes());
    h.extend(format.as_bytes());
    h.resize(h.len().next_multiple_of(8), 0);
    h.extend([0u8; 8]); // end of extensions
    let name_at = h.len() as u64;
    h[name_at_slot..name_at_slot + 8].copy_from_slice(&name_at.to_be_bytes());
    h.extend(backing.as_bytes());
    if backing.is_empty() || backing.len() > 1023 || h.len() as u64 > cluster {
        return Err(invalid(format!(
            "backing name of {} bytes does not fit in the header",
            backing.len()
        )));
    }

    file.write_all_at(&h, 0)?;
    file.write_all_at(&rb.to_be_bytes(), rt)?;
    file.write_all_at(&vec![0, 1].repeat(metadata as usize), rb)?;
    // The L1 table's zero entries: the file ends after them, as qemu-img's does.
    file.set_len(l1 + l1_size * 8)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::file::ImageFile;
    use crate::test_util::TempDir;
    use crate::{BackingFile, QcowDisk, QcowHeader};
    use std::fs::OpenOptions;

    fn qemu_img(args: &[&str]) -> Option<(bool, String)> {
        let exe = ["/opt/homebrew/bin/qemu-img", "/usr/local/bin/qemu-img"]
            .into_iter()
            .find(|p| std::path::Path::new(p).exists())?;
        let out = std::process::Command::new(exe).args(args).output().unwrap();
        Some((
            out.status.success(),
            String::from_utf8_lossy(&out.stdout).into_owned()
                + &String::from_utf8_lossy(&out.stderr),
        ))
    }

    #[test]
    fn an_overlay_reads_its_base_takes_writes_and_qemu_img_accepts_it() {
        let dir = TempDir::new().unwrap();
        let base = dir.as_path().join("base.raw");
        let base_bytes: Vec<u8> = (0..3 << 16).map(|i| (i % 251) as u8).collect();
        std::fs::write(&base, &base_bytes).unwrap();
        let top = dir.as_path().join("top.qcow2");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&top)
            .unwrap();
        create_overlay(&file, 3 << 16, base.to_str().unwrap(), ImageType::Raw).unwrap();
        assert_eq!(
            file.metadata().unwrap().len(),
            3 * 65536 + 8,
            "qemu-img's layout"
        );

        let header = QcowHeader::new(&ImageFile::new(file.try_clone().unwrap())).unwrap();
        let named = header.backing_file.as_ref().unwrap();
        assert_eq!(
            (named.path.as_str(), named.format),
            (base.to_str().unwrap(), Some(ImageType::Raw))
        );
        assert_eq!(
            (
                header.size,
                header.cluster_bits,
                header.refcount_table_clusters
            ),
            (3 << 16, 16, 1)
        );
        if let Some((ok, text)) = qemu_img(&["check", "-q", top.to_str().unwrap()]) {
            assert!(ok, "{text}");
            let (_, info) = qemu_img(&["info", top.to_str().unwrap()]).unwrap();
            assert!(
                info.contains(base.to_str().unwrap()) && info.contains("backing file format: raw"),
                "{info}"
            );
        }

        let backing = BackingFile::raw(ImageFile::new(File::open(&base).unwrap())).unwrap();
        let disk = QcowDisk::open(ImageFile::new(file), Some(backing)).unwrap();
        let mut buf = vec![0; base_bytes.len()];
        disk.read_exact_at(0, &mut buf).unwrap();
        assert!(buf == base_bytes, "reads the base");
        disk.write_all_at(70000, b"overlay").unwrap();
        disk.close_clean().unwrap();
        drop(disk);
        assert_eq!(
            std::fs::read(&base).unwrap(),
            base_bytes,
            "the base is never written"
        );
        if let Some((ok, text)) = qemu_img(&["check", "-q", top.to_str().unwrap()]) {
            assert!(ok, "{text}");
        }
    }

    #[test]
    fn a_large_overlay_gets_a_refcount_table_covering_its_growth() {
        let dir = TempDir::new().unwrap();
        let top = dir.as_path().join("big.qcow2");
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&top)
            .unwrap();
        let size = 64u64 << 40;
        let base = dir.as_path().join("base.raw");
        std::fs::write(&base, [0u8; 512]).unwrap();
        create_overlay(&file, size, base.to_str().unwrap(), ImageType::Raw).unwrap();
        let header = QcowHeader::new(&ImageFile::new(file)).unwrap();
        assert!(header.refcount_table_clusters > 1);
        assert!(
            refcount_coverage(header.refcount_table_clusters.into(), 16, 4)
                >= growth_bound(size, 16, 4)
        );
        assert!(
            refcount_coverage((header.refcount_table_clusters - 1).into(), 16, 4)
                < growth_bound(size, 16, 4),
            "no larger than needed"
        );
        if let Some((ok, text)) = qemu_img(&["check", "-q", top.to_str().unwrap()]) {
            assert!(ok, "{text}");
        }
        for bad in [0, 511] {
            let f = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(true)
                .open(&top)
                .unwrap();
            assert!(create_overlay(&f, bad, "b", ImageType::Raw).is_err());
        }
        let f = OpenOptions::new()
            .read(true)
            .write(true)
            .truncate(true)
            .open(&top)
            .unwrap();
        assert!(create_overlay(&f, 512, &"x".repeat(1024), ImageType::Raw).is_err());
    }
}
