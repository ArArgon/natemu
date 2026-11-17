// Copyright © 2021 Intel Corporation
//
// Copyright 2026 The Cloud Hypervisor Authors. All rights reserved.
//
// Copyright (c) Meta Platforms, Inc. and affiliates.
//
// SPDX-License-Identifier: Apache-2.0 AND BSD-3-Clause

//! Shared helpers for QCOW2 read, write and deallocate paths.
//!
//! natemu: upstream's `cow_write_sync`, `scatter_read_sync` and `deallocate_range_result` took
//! `AsyncIoOperation` buffers and returned `AsyncIoError`/negative errnos. Here they take plain
//! slices and return `io::Result`; the logic is otherwise unchanged. The facade builds on them.

use std::cmp::min;
use std::io;
use std::os::unix::fs::FileExt;
use std::sync::Arc;

use super::decoder::Decoder;
use super::metadata::{BackingRead, ClusterReadMapping, DeallocAction, QcowMetadata};
use super::qcow_raw_file::QcowRawFile;

/// Decompress a full QCOW2 cluster from compressed data.
///
/// Returns a `cluster_size` byte buffer with the decompressed cluster
/// content. Fails if the decoder does not produce exactly `cluster_size`
/// bytes. natemu: the only decoder fails, so this is always EIO.
pub(super) fn decompress_cluster(
    compressed: &[u8],
    cluster_size: usize,
    decoder: &dyn Decoder,
) -> io::Result<Vec<u8>> {
    let mut decompressed = vec![0u8; cluster_size];
    let n = decoder
        .decode(compressed, &mut decompressed)
        .map_err(|_| io::Error::from_raw_os_error(libc::EIO))?;
    if n != cluster_size {
        return Err(io::Error::from_raw_os_error(libc::EIO));
    }
    Ok(decompressed)
}

/// Applies one deallocation action to the data file and refcount table.
pub(super) fn apply_dealloc_action(
    metadata: &QcowMetadata,
    data_file: &QcowRawFile,
    action: &DeallocAction,
) -> io::Result<()> {
    match action {
        DeallocAction::PunchHole {
            host_offset,
            length,
        } => {
            data_file.file().punch_hole(*host_offset, *length)?;
            metadata.complete_punch_hole(*host_offset);
            Ok(())
        }
        DeallocAction::WriteZeroes {
            host_offset,
            length,
        } => data_file
            .file()
            .write_zeroes_at(*host_offset, *length)
            .map(|_| ()),
    }
}

/// Deallocates a byte range, returning the first error after applying
/// every action.
pub(super) fn deallocate_range(
    metadata: &QcowMetadata,
    data_file: &QcowRawFile,
    offset: u64,
    length: usize,
    sparse: bool,
    write_zeroes: bool,
    backing_file: Option<&dyn BackingRead>,
) -> io::Result<()> {
    let actions = metadata.deallocate_bytes(
        offset,
        length,
        sparse,
        write_zeroes || backing_file.is_some(),
        backing_file,
    )?;
    let mut first_error = None;
    for action in &actions {
        if let Err(e) = apply_dealloc_action(metadata, data_file, action) {
            first_error.get_or_insert(e);
        }
    }
    first_error.map_or(Ok(()), Err)
}

/// Writes `data` to the data file cluster by cluster, allocating
/// and copying up backing data as needed. Writes are synchronous because
/// the host offset is only known after the metadata allocation.
pub(super) fn cow_write_at(
    address: u64,
    data: &[u8],
    metadata: &QcowMetadata,
    data_file: &QcowRawFile,
    backing_file: &Option<Arc<dyn BackingRead>>,
    cluster_size: u64,
) -> io::Result<()> {
    let total_len = data.len();
    let mut buf_offset = 0usize;

    while buf_offset < total_len {
        let curr_addr = address + buf_offset as u64;
        let intra_offset = curr_addr & (cluster_size - 1);
        let remaining_in_cluster = (cluster_size - intra_offset) as usize;
        let count = min(total_len - buf_offset, remaining_in_cluster);

        let chunk = &data[buf_offset..buf_offset + count];
        let whole = intra_offset == 0 && count == cluster_size as usize;
        // natemu: a whole-cluster write over a backing file passes its own data as the fill, so
        // a newly allocated cluster holds it (written under the table lock) before its L2 entry
        // exists. Upstream allocated it unwritten and wrote the data after the lock: a racing
        // read, or a crash in which a flush or eviction made that entry durable before the data,
        // then saw zeros, neither the old (backing) nor the new data.
        let backing_data = match backing_file {
            Some(_) if whole => None,
            Some(backing) => {
                let cluster_begin = curr_addr - intra_offset;
                let mut data = vec![0u8; cluster_size as usize];
                backing.read_at(cluster_begin, &mut data)?;
                Some(data)
            }
            None => None,
        };
        let fill = match backing_file {
            Some(_) if whole => Some(chunk),
            _ => backing_data.as_deref(),
        };

        let (host_offset, filled) = metadata.map_cluster_for_write(curr_addr, fill)?;
        if !(whole && filled) {
            data_file.file().write_all_at(chunk, host_offset)?;
        }
        buf_offset += count;
    }
    Ok(())
}

/// Reads cluster mappings synchronously into `out`, filling
/// holes, decompressing, and reading from the backing file as each
/// mapping requires.
pub(super) fn scatter_read(
    mappings: Vec<ClusterReadMapping>,
    out: &mut [u8],
    data_file: &QcowRawFile,
    backing_file: &Option<Arc<dyn BackingRead>>,
    cluster_size: u64,
    decoder: &dyn Decoder,
) -> io::Result<()> {
    let mut buf_offset = 0usize;
    for mapping in mappings {
        match mapping {
            ClusterReadMapping::Zero { length } => {
                out[buf_offset..buf_offset + length as usize].fill(0);
                buf_offset += length as usize;
            }
            ClusterReadMapping::Allocated {
                offset: host_offset,
                length,
            } => {
                let len = length as usize;
                data_file
                    .file()
                    .read_exact_at(&mut out[buf_offset..buf_offset + len], host_offset)?;
                buf_offset += len;
            }
            ClusterReadMapping::Compressed {
                host_offset,
                compressed_size,
                cluster_offset,
                length,
            } => {
                let mut compressed = vec![0u8; compressed_size];
                data_file
                    .file()
                    .read_exact_at(&mut compressed, host_offset)?;
                let decompressed = decompress_cluster(&compressed, cluster_size as usize, decoder)?;
                out[buf_offset..buf_offset + length]
                    .copy_from_slice(&decompressed[cluster_offset..cluster_offset + length]);
                buf_offset += length;
            }
            ClusterReadMapping::Backing {
                offset: backing_offset,
                length,
            } => {
                backing_file.as_ref().unwrap().read_at(
                    backing_offset,
                    &mut out[buf_offset..buf_offset + length as usize],
                )?;
                buf_offset += length as usize;
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use super::super::decoder::UnsupportedDecoder;
    use super::super::metadata::ClusterReadMapping;
    use super::super::qcow_raw_file::QcowRawFile;
    use super::{decompress_cluster, scatter_read};
    use crate::file::ImageFile;
    use crate::test_util::TempFile;

    // natemu: upstream compressed with flate2 and expected the data back; compression is
    // unsupported here, so the same call must fail with EIO.
    #[test]
    fn test_decompress_cluster_unsupported() {
        let err = decompress_cluster(&[1, 2, 3], 65536, &UnsupportedDecoder {}).unwrap_err();
        assert_eq!(err.raw_os_error(), Some(libc::EIO));
    }

    #[test]
    fn test_decompress_cluster_corrupt_input() {
        let corrupt = vec![0xffu8; 64];
        let err = decompress_cluster(&corrupt, 65536, &UnsupportedDecoder {}).unwrap_err();
        assert_eq!(err.raw_os_error(), Some(libc::EIO));
    }

    #[test]
    fn single_full_mapping_short_read_errors() {
        // The mapping claims 200 bytes but the file holds only 100.
        let src: Vec<u8> = (0..100u8).collect();
        let tmp = TempFile::new().unwrap();
        tmp.as_file().write_all(&src).unwrap();
        tmp.as_file().sync_all().unwrap();

        let aligned = ImageFile::new(tmp.as_file().try_clone().unwrap());
        let data_file = QcowRawFile::from(aligned, 65536, 16).unwrap();

        let mut out = vec![0u8; 200];
        let mappings = vec![ClusterReadMapping::Allocated {
            offset: 0,
            length: 200,
        }];

        assert!(
            scatter_read(
                mappings,
                &mut out,
                &data_file,
                &None,
                65536,
                &UnsupportedDecoder {}
            )
            .is_err()
        );
    }

    #[test]
    fn single_full_mapping_reads() {
        let cluster = 65536usize;
        let src: Vec<u8> = (0..cluster).map(|i| (i % 251) as u8).collect();
        let tmp = TempFile::new().unwrap();
        tmp.as_file().write_all(&src).unwrap();
        tmp.as_file().sync_all().unwrap();

        let aligned = ImageFile::new(tmp.as_file().try_clone().unwrap());
        let data_file = QcowRawFile::from(aligned, cluster as u64, 16).unwrap();

        let mut out = vec![0u8; cluster];
        let mappings = vec![ClusterReadMapping::Allocated {
            offset: 0,
            length: cluster as u64,
        }];

        scatter_read(
            mappings,
            &mut out,
            &data_file,
            &None,
            cluster as u64,
            &UnsupportedDecoder {},
        )
        .unwrap();
        assert_eq!(out, src);
    }
}
