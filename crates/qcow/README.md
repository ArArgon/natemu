# natemu-qcow

qcow2 image code vendored from Cloud Hypervisor, adapted to build and test on macOS aarch64.

- Source: https://github.com/cloud-hypervisor/cloud-hypervisor, `block/src/formats/qcow/`
  (plus `block/src/error.rs`, and the ideas of `aligned_file.rs` and `sparse.rs`)
- Commit: `e9ea04e42a36ec6b7b1c84235d0abe11d6ef6ba2` (main, 2026-11-16; not a release tag, see
  the snapshot note in the design record)
- Vendored: 2026-11-16
- Licence: Apache-2.0 AND BSD-3-Clause per file header (some files derive from Chromium OS crosvm,
  BSD-3-Clause). Texts are in `LICENSES/`. Keep every file header; to re-vendor, diff against the
  commit above first.

## Dropped

- `engine_sync.rs`, `engine_uring.rs` (async engines; coupled to vm-memory, virtio-queue,
  vm-virtio, `AsyncIo`). Their 65 + 15 tests went with them.
- `DiskFile`/`AsyncDiskFile` wrappers in `mod.rs` and every `disk_file`/`async_io` trait.
- Compression: `flate2` and `zstd` are not dependencies. Compressed clusters fail with EIO on read
  and on copy-on-write; a header with compression type zstd (or any other non-zlib type) is
  refused (`UnsupportedCompressionType`). The zlib header value is still accepted so an image that
  merely declares the default opens; only reading a compressed cluster fails.
- O_DIRECT / `AlignedBuffer` bounce paths; `/proc/self/fd` lookups; block-device ioctls
  (`BLKDISCARD`, `BLKZEROOUT`, `BLKGETSIZE64`) and `query_device_size` (regular files only).
- `io_uring`, `test-utils` features, `vmm_sys_util` use (including `SeekHole`).
- Upstream's preadv fast path in `scatter_read_sync`.

## Replaced (behaviour of the copied logic is otherwise unchanged)

- `AlignedFile` -> `file::ImageFile` over a small `FileLayer` trait (`file.rs`); `StdFile` is the
  real layer over `std::fs::File`. A test can later supply a recording or fault-injecting layer.
  The file path is supplied by the caller (`ImageFile::with_path`, `QcowDisk::with_path`) and
  replaces the two `read_link("/proc/self/fd/N")` calls (relative backing file resolution, and a
  warning). Without a path a relative backing name resolves against the working directory.
- `BlockError`/`BlockResult`: kept as a local copy (`error.rs`), `io::Error` converts in. The
  facade converts to `io::Error`.
- `vmm_sys_util` `WriteZeroesAt` -> positional zero writes (64 KiB chunks); `PunchHole` ->
  `fcntl(F_PUNCHHOLE)` on the 4 KiB-aligned interior with zero writes for the edges, and zero
  writes for the whole range if the call is refused (Linux: `fallocate(PUNCH_HOLE|KEEP_SIZE)`
  with the same fallback).
- `FileSync::fsync` and the `sync_all`/`sync_data` flush points -> an explicit `SyncKind` at each
  point (`ImageFile::sync(kind)`, `barrier_sync`, `full_sync`; `F_BARRIERFSYNC`/`F_FULLFSYNC` on
  macOS, plain fsync elsewhere or when the file system refuses the fcntl). See "Write order".
- `mod.rs` -> `lib.rs`. `QcowDisk` keeps the opener (`new`, `with_path`, `from_image_file`),
  `resize`, `sync_metadata`, sizes, and gains plain-slice `write_all_at`, `read_exact_at` and
  `deallocate`, built on `common.rs` (`cow_write_at`, `scatter_read`, `deallocate_range`: upstream's
  `cow_write_sync`, `scatter_read_sync`, `deallocate_range_result` with `AsyncIoOperation`
  replaced by slices and errno codes by `io::Result`). These are not the final facade: no locks
  beyond the metadata lock, no flush ordering, no exclusive file lock.
- Test helpers: `vmm_sys_util` `TempFile`/`TempDir` -> small std versions in `test_util.rs`;
  `compress_allocated_clusters` emits deflate "stored" blocks instead of using flate2.

## Review fixes (after vendoring)

- A copy-on-write into a compressed cluster returns EIO but no longer sets the corrupt bit when the
  decoder says "unsupported" (upstream sets it for any decode failure; here that would flag a valid
  image and make it reopen read-only). Genuine size mismatches still flag corruption. Test:
  `write_into_compressed_cluster_is_eio_without_corrupt_bit`.
- `punch_hole_fd` falls back to zero writes only on EOPNOTSUPP/ENOTSUP (as upstream `sparse.rs`);
  other errors propagate. `offset + length` is checked and must fit `off_t`.
- No file-handle duplication per deallocate: `ImageFile` punch/zero methods take `&self`, and the
  panicking `Clone` impls (`ImageFile`, `QcowRawFile`) are gone; `try_clone` returns `io::Result`.
- Licence header references point at `LICENSES/BSD-3-Clause.txt`.

## Write order, dirty bit and backing files (the facade step)

The rules are the design record's (docs/m1-config-and-qcow2-design.md, section 4); the host facade
is `crates/host/src/disk/qcow.rs`.

- `sync_caches` (metadata.rs): a barrier (data before tables; needed for L2 tables changed in place),
  dirty L2 tables and refcount blocks, a barrier (upstream: `sync_all`), then the L1 and refcount
  tables. No final sync: `QcowDisk::sync(kind)` runs it with the table lock released, and only then
  returns the clusters those tables freed to the free list (`write_tables`/`release`; upstream moved
  them under the lock right after its sync). When tables are dirty, `QcowDisk::sync` first runs a
  barrier outside the lock: on APFS F_BARRIERFSYNC writes out the file's whole cache, which under the
  lock stalled every guest write for seconds. A dirty L2 evicted from the cache is written after a
  barrier. Resize/L1-growth `sync_all` points are barriers.
- The dirty, corrupt and autoclear header writes use `F_FULLFSYNC`.
- `Drop` writes the tables without a sync and keeps the dirty bit (upstream flushed and cleared it);
  only `close_clean` (the power-off drain) clears it, after an `F_FULLFSYNC`, with another;
  `mark_dirty` sets it again. A failed table write (flush or eviction) is reported by
  `tables_failed`.
- A whole-cluster write over a backing file passes its own data as the fill, so a new cluster holds
  it before its L2 entry exists (upstream left it unwritten until after the lock: a racing read, or a
  crash after a flush or eviction made the entry durable, saw zeros). Fills are borrowed slices.
- The parser no longer opens backing files: `QcowDisk::open(file, backing)` takes a chain the caller
  opened (`BackingFile::raw`, `BackingFile::qcow2`, read-only), and the header's backing presence must
  match. Upstream's path-following opener (`QcowDisk::new`/`with_path`, `parse_qcow`, format
  guessing) is kept for the vendored tests only (`cfg(test)`).

## Backing file paths (tests only now)

A relative backing name resolves against the directory of the referring image, as given by the path
passed to `with_path`. Callers must always pass `with_path`; without it the name resolves against the
working directory. A symlinked image resolves against the link's directory, unlike upstream's
`/proc/self/fd` lookup (which gave the target's directory). Path security policy is the facade's.

## Tests

`cargo test -p natemu-qcow`: 141 pass, 1 ignored, 0 fail (macOS aarch64).

Upstream tests changed or not run:

- Ignored: `metadata::tests::failed_l2_relocate_after_compressed_write_keeps_live_table` (needs
  zlib decompression).
- Adapted: `compression_type_requires_matching_feature_bit` (zstd+feature bit is now an error, not
  valid); `header_write_matches_qcow2_layout` (zstd half now asserts the refusal);
  `rebuild_refcounts_does_not_materialise_the_declared_table` (peak RSS via `getrusage`, not
  `/proc/self/status`); the `common.rs` decompress tests (expect EIO from the failing decoder;
  scatter-read tests use slices).
- Removed: `raw_backing_file_uses_direct_io` (O_DIRECT is gone); upstream `aligned_file`, `sparse`
  and `block/src/lib.rs` tests; the 8 `mod.rs` engine tests (4 re-written without the engine).
- Added (natemu): `file.rs` (2), `metadata.rs` (1, corrupt bit), `lib.rs` (4 backing-file and zero-over-backing tests that stand in
  for part of the dropped engine tests).
- Not ported: the 65 engine_sync tests. The behaviours they covered are rebuilt as facade tests in
  `crates/host/src/disk/qcow_tests.rs` (free-list reuse, out-of-bounds L2 entries, failed punch,
  stale punch vs a new L2, backing reads past the end, relative and missing backing names, a
  three-layer chain, concurrent multi-queue I/O, visibility after sync and reopen, v2, compressed).
- Changed: `dirty_bit_cleared_when_last_metadata_owner_drops` became
  `dirty_bit_cleared_only_by_a_clean_close`, and `dirty_bit_set_on_open_cleared_on_close_v3` closes
  cleanly before checking (a drop keeps the bit now).

## Dependencies and licences (checked by hand; `cargo-deny` is not configured)

| crate | version | licence |
|---|---|---|
| bitflags | 2.13.2 | MIT OR Apache-2.0 |
| libc | 0.2.191 | MIT OR Apache-2.0 |
| log | 0.4.35 | MIT OR Apache-2.0 |
| thiserror (+impl) | 2.0.21 | MIT OR Apache-2.0 |
| zerocopy (+derive) | 0.8.63 | BSD-2-Clause OR Apache-2.0 OR MIT |
| proc-macro2, quote, syn | 1.0.107, 1.0.47, 2.0.119 | MIT OR Apache-2.0 |
| unicode-ident | 1.0.27 | (MIT OR Apache-2.0) AND Unicode-3.0 |

`crc-any` (upstream block crate) is not used by the qcow code and is not a dependency. No async
runtime. `log`, `thiserror` were already workspace dependencies; `bitflags`, `libc`, `zerocopy`
(and its derive chain) are new in `Cargo.lock`.
