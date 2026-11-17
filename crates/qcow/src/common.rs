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
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

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

/// natemu: how long a disk may spend punching holes per [`PUNCH_WINDOW`]. Past it, freed clusters go to the
/// free list unpunched: their contents no longer matter (reads come from the tables, and a reused
/// cluster is written whole under the table lock), so only host space is reclaimed later. This
/// keeps a guest DISCARD or WRITE_ZEROES over many scattered clusters (32 ranges of 2 GiB in one
/// request) inside the device's 10 s call limit.
const PUNCH_TIME: Duration = Duration::from_millis(250);
/// The window [`PUNCH_TIME`] applies to: longer than the device's call limit, so no request (one
/// call or the up to 32 calls of one DISCARD or WRITE_ZEROES) can punch for longer than
/// `PUNCH_TIME` plus one punch, wherever its boundaries fall (the backend does not see them).
const PUNCH_WINDOW: Duration = Duration::from_secs(10);
/// The longest single punch, so one punch cannot hold a worker long.
const MAX_PUNCH: u64 = 128 << 20;

/// natemu: the punching time a disk has left in the current [`PUNCH_WINDOW`].
#[derive(Debug)]
pub(super) struct PunchBudget(Mutex<(Instant, Duration)>);

impl PunchBudget {
    pub(super) fn new() -> Self {
        Self(Mutex::new((Instant::now(), Duration::ZERO)))
    }

    fn available(&self) -> bool {
        let mut window = self.0.lock().unwrap_or_else(|e| e.into_inner());
        if window.0.elapsed() >= PUNCH_WINDOW {
            *window = (Instant::now(), Duration::ZERO);
        }
        window.1 < PUNCH_TIME
    }

    fn spend(&self, time: Duration) {
        self.0.lock().unwrap_or_else(|e| e.into_inner()).1 += time;
    }
}

/// Deallocates a byte range, returning the first error after applying
/// every action. natemu: contiguous punches are one call (upstream punched per cluster, about
/// 13 us each on APFS: 64 GiB of allocated clusters took about 14 s), with a time budget.
pub(super) fn deallocate_range(
    metadata: &QcowMetadata,
    data_file: &QcowRawFile,
    offset: u64,
    length: usize,
    sparse: bool,
    write_zeroes: bool,
    backing_file: Option<&dyn BackingRead>,
    budget: &PunchBudget,
) -> io::Result<()> {
    let actions = metadata.deallocate_bytes(
        offset,
        length,
        sparse,
        write_zeroes || backing_file.is_some(),
        backing_file,
    )?;
    let mut first_error = None;
    let mut i = 0;
    while i < actions.len() {
        match actions[i] {
            DeallocAction::PunchHole {
                host_offset,
                length,
            } => {
                let mut end = host_offset + length;
                let mut j = i + 1;
                while let Some(&DeallocAction::PunchHole {
                    host_offset: next,
                    length,
                }) = actions.get(j)
                    && next == end
                    && end + length - host_offset <= MAX_PUNCH.max(length)
                {
                    end += length;
                    j += 1;
                }
                let punched = if budget.available() {
                    let start = Instant::now();
                    let result = data_file.file().punch_hole(host_offset, end - host_offset);
                    budget.spend(start.elapsed());
                    result
                } else {
                    Ok(())
                };
                match punched {
                    // A failed punch keeps its clusters reserved (off both free lists).
                    Err(e) => {
                        first_error.get_or_insert(e);
                    }
                    Ok(()) => {
                        metadata.complete_punch_holes(actions[i..j].iter().map(|a| match a {
                            DeallocAction::PunchHole { host_offset, .. } => *host_offset,
                            DeallocAction::WriteZeroes { .. } => unreachable!(),
                        }))
                    }
                }
                i = j;
            }
            DeallocAction::WriteZeroes {
                host_offset,
                length,
            } => {
                if let Err(e) = data_file.file().write_zeroes_at(host_offset, length) {
                    first_error.get_or_insert(e);
                }
                i += 1;
            }
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
        // natemu: a partial write reads the backing cluster (outside the lock) only when the
        // cluster turns out to need it as a fill; upstream read it for every partial write, even
        // into a cluster the overlay already held.
        let mut backing_data = None;
        let (host_offset, filled) = loop {
            let fill = match backing_file {
                Some(_) if whole => Some(chunk),
                _ => backing_data.as_deref(),
            };
            if let Some(mapped) =
                metadata.map_cluster_for_write(curr_addr, fill, backing_file.is_some())?
            {
                break mapped;
            }
            let cluster_begin = curr_addr - intra_offset;
            let mut data = vec![0u8; cluster_size as usize];
            backing_file.as_ref().unwrap().read_at(cluster_begin, &mut data)?;
            backing_data = Some(data);
        };
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
