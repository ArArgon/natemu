# Configuration file, device list and disk backends: design (revision 2)

Drafted November 16, 2026, revised the same day after an independent review and the owner's
answers. Covers M1's "configuration format and device list, multi-disk with read-only disks"
and "qcow2 disk images". Owner decisions are marked as such; everything else is a proposal.
*(Unverified)* marks what I did not check.

## 1. The configuration file

```toml
# Paths in this file are relative to this file's folder.
[machine]                      # what the guest sees
ram = "4G"                     # bytes, 0x hex, or K/M/G suffix (as --ram)
vcpus = 2
transport = { disk = "pci", nic = "pci", rng = "pci" }

[serial]                       # COM1's host end
output = "stdout"              # or a file path
stdin = false

[engine]                       # how natemu runs it
fexc = "../build/fexc"
runtime = "../target/aarch64-unknown-none/release/natemu-runtime"   # optional
fp = "fast"                    # or "accurate"
compile_cache = "off"          # a folder, or "off"; absent = the user cache folder
compile_cache_mb = 512
speculation_barriers = false

[boot.kernel]
image = "vmlinuz-6.12.107"
initrd = "initrd.img-6.12.107"
cmdline = "console=ttyS0 root=/dev/vda1 ro"

[[disk]]                       # list order = the order the guest sees
id = "root"
path = "runs/r1/root.qcow2"
backing = "images/debian13-base.raw"

[[disk]]
id = "tools"
path = "images/tools.iso"
readonly = true

[[nic]]
id = "net0"
forward = ["tcp:2222:22"]
host_loopback = false

[[rng]]
id = "rng0"

[debug]
perf_log = "perf.log"
```

**Fields.**
- `[machine]`: `ram` (default 2G), `vcpus` (default 1), `transport` with keys `disk`, `nic`,
  `rng`, each `"mmio"` or `"pci"` (key `nic` matches the list: owner decision).
- `[serial]` (owner decision: its own section): `output` (default standard output), `stdin`
  (default false).
- `[engine]`: `fexc` (required), `runtime`, `fp`, `compile_cache`, `compile_cache_mb`,
  `speculation_barriers`; today's product flags with today's defaults.
- `[boot.kernel]`: `image` (required), `initrd`, `cmdline` (default empty).
- `[boot.uefi]`: reserved; its presence is refused with "UEFI boot is planned (M3) and not
  built yet".
- `[[disk]]`: `id`, `path` (both required), `readonly` (default false), `backing` (section 4).
  No `format` field (owner decision): the format is detected.
- `[[nic]]`: `id`, `forward` (today's `--net-forward` syntax), `host_loopback`. At most one in
  M1 (multi-NIC is optional work).
- `[[rng]]`: `id`. At most one in M1.
- `[debug]`: every experiment switch the CLI has today (`check_reuse`, `pool`,
  `page_currency`, `writable_on_read`, `perf_log`). Deletions are deferred (owner decision).

**Validation, all before a VM exists.** Every table rejects unknown keys (serde's "deny unknown
fields" on every struct; no flattening, which disables it). Errors are one line naming the
file, the key path and what is allowed:
- `vm.toml: unknown key disk[2].fromat; expected one of id, path, readonly, backing`
- `vm.toml: [boot] needs exactly one boot method, [boot.kernel] or [boot.uefi]; found none`
  (or "found both"). A field of another boot method is an unknown key of this one.
- `vm.toml: device id "root" is used twice (disk 1 and disk 3)`. Ids are unique across all
  kinds, match `[a-z0-9_-]+`, and are not guest-visible.
- `vm.toml: disks "root" and "copy" are the same file` (by device and inode, so the message
  is not the misleading "held by another process").
- `--set disk.boot.path: no disk with id "boot"` (an override cannot create a device).
- Device limits (section 3) and today's checks (vCPUs, RAM, FEAT_SB, forward syntax) as they are.

## 2. How the command line maps onto it

**One internal representation**: one typed configuration struct. The CLI fills it in layers,
lowest first: built-in defaults, the file (`--config FILE`), the ten environment variables the
CLI reads today (deprecated), then the command line. One function turns the result into the
library's launch description (`Boot`). `toml` is a CLI dependency only.

**Typed edits, not a text tree.** Each layer is applied as typed assignments to the struct:
- the file is deserialised (every field optional) and merged, its paths resolved against the
  file's folder at load time;
- old flags keep their clap parsers (`size`, `parse_fp`, `parse_page_currency`, `transports`,
  `port_forward`, the 1/0/yes/no/on/off boolean parser) and assign the parsed values; their
  paths resolve against the working directory;
- `--set key=value` and environment values look the key up in one key table that gives, per
  key, the same parser the matching old flag uses; string keys take the raw text (`--set
  boot.kernel.cmdline=3` stays a string), paths resolve against the working directory. An
  unknown key in `--set` is the same error as in the file.

So every path carries its base folder from the moment it is read, and every old spelling keeps
its old meaning. Setting one key twice on the command line (by `--set`, a flag, or both) is an
error.

**Old flags** map onto keys: `--kernel/--initrd/--cmdline` → `boot.kernel.*`; `--ram/--vcpus`
→ `machine.*`; `--serial/--serial-stdin` → `serial.output/stdin`; `--fexc`, `--runtime`,
`--fp`, `--compile-cache[-mb]`, `--speculation-barriers` → `engine.*`; `--virtio-transport` →
`machine.transport` with its old meaning (a partial spec such as `disk=pci` sets the unnamed
kinds to mmio, as today; `net=` maps to `nic`); `--disk FILE` → disk `{id="disk0"}` (its help
text changes: the image may now be qcow2); `--net` and its options → nic `{id="net0"}`;
`--rng` → rng `{id="rng0"}`; `--check-reuse`, `--pool`, `--page-currency`,
`--writable-on-read`, `--perf-log` → `debug.*`. `--disk`, `--net` and `--rng` are refused when
the file lists that kind. `--kernel` is no longer required by clap; "no boot method" replaces it.

**Transport defaults.** With a config file the default is PCI; without one (the ~70 existing
runners) it stays MMIO. The library's own default (`Boot::new`) stays MMIO, which the test
driver and unit tests rely on. The "no file means MMIO" rule is removed together with the old
flags, as the last separate step.

**Environment variables** (owner decision): only the ten the CLI reads are deprecated. When
clap reports that a value came from one, the CLI prints once `natemu: NATEMU_FP is deprecated;
use [engine] fp or --set engine.fp=...` to standard error. The runners checked read only the
last line or search for a fixed string, so a start-up warning breaks none of them *(not every
runner checked)*. The library's own environment reads stay untouched in M1.

## 3. Device placement and interrupt lines

Today each kind has a fixed PCI slot, MMIO page and line: disk 1 / 0xF000_0000 / 16, network
2 / 0xF000_1000 / 17, entropy 3 / 0xF000_2000 / 18. The interrupt controller has eight device
lines (16-23) and today holds one level per line, so no two devices can share one.

**Placement** (one function knows the rule):

| Device | PCI slot | MMIO page | MMIO line | MMIO ACPI uid |
| --- | --- | --- | --- | --- |
| disk 0 | 1 | 0xF000_0000 | 16 | 0 |
| nic | 2 | 0xF000_1000 | 17 | 1 |
| rng | 3 | 0xF000_2000 | 18 | 2 |
| disk k ≥ 1 | 3 + k | 0xF000_0000 + (2 + k) × 4 KiB | 18 + k | 2 + k |

**PCI interrupt lines (owner decision: shared, as real PCI).** A PCI function's fallback (INTx)
line becomes a shared level line, as on real boards where many slots share four lines. The
first three slots keep their old lines (slot 1 → 16, 2 → 17, 3 → 18), so old launches are
unchanged. Slots 4 and up take, round robin, the lines among 16-19 that no MMIO device in the
same launch holds. The ACPI routing table may route any slot to any line, so this is a platform
choice, not a guest-visible oddity. With MSI-X (the normal case) the line is never used.

**Limits.** PCI disks: up to 29 (31 usable slots less the NIC and entropy device). MMIO devices
keep exclusive lines: 8 in all, so at most 6 MMIO disks. M1 keeps one NIC and one entropy device.
Refusals: "at most 6 disks on mmio (interrupt lines 16-23); use pci for more", "PCI slots 1-31
are all used". Many MMIO devices also exceed the resident's eight-entry device-page cache: a
speed cost only, measured in the multi-disk gate.

**Order.** Disks get slots in list order and the guest names them in that order; kinds are
independent (removing the NIC moves no disk). Inserting a disk renumbers the later ones, like
moving a cable. No `slot` field in M1; revisit at M3, where UEFI boot entries record the boot
disk's PCI path. Under MMIO the order relies on Linux probing in ACPI node order *(unverified;
the multi-disk gate checks it)*.

**Unchanged old launches.** The pinned ACPI test hashes a different device order than a real
launch builds, so it proves nothing about old command lines. Lane A's first commit therefore
moves the table-building part of start-up into a pure function (no behaviour change) and pins
its output (MMIO list, PCI list, ACPI tables, memory map) for all 8 device subsets × {mmio,
pci, mixed}, before anything else changes. Every later step must keep those digests.

## 4. The disk-backend interface (the seam between the two lanes)

The workers' call trait in the block device is renamed and widened; the queue, owner thread,
worker pool, job types and sticky-failure wrapper stay. The pool already calls through a
dynamic trait object, so the seam adds no indirection.

```rust
pub(crate) trait DiskBackend: Send + Sync {
    fn read(&self, buf: &mut [u8], offset: u64) -> io::Result<usize>;
    fn write(&self, buf: &[u8], offset: u64) -> io::Result<usize>;
    /// full = false: guest FLUSH (fsync class). full = true: power-off only (F_FULLFSYNC).
    fn sync(&self, full: bool) -> io::Result<()>;
    /// A hint: Ok when nothing was freed; freed ranges may then read as zeros.
    fn discard(&self, offset: u64, len: u64) -> io::Result<()>;
    /// Afterwards the range reads as zeros; the backend may deallocate it.
    fn write_zeroes(&self, offset: u64, len: u64) -> io::Result<()>;
}

pub(crate) struct Image {
    pub backend: Box<dyn DiskBackend>,
    pub capacity: u64,        // bytes, nonzero multiple of 512, fixed for the launch
    pub read_only: bool,      // offer the read-only bit; no flush, discard, write-zeroes
    pub discard: bool,        // offer DISCARD
    pub write_zeroes: bool,   // offer WRITE_ZEROES
    pub id: [u8; 20],         // GET_ID answer
}

pub(crate) fn open(path: &Path, read_only: bool, backing: Option<&Path>) -> Result<Image>;
```

The device offers DISCARD and WRITE_ZEROES only when the image says so. Raw and qcow2 v3 offer
both; the flag is also the later fix for raw on a file system without holes.

**Errors**: `std::io::Error` as today; its text is the request's error and the guest sees an
I/O error. The vendored code's error type is converted at the facade. `Unsupported` from
discard is success.

**Threads**: every method takes `&self` and may run at once on the four workers and, for a lone
read or write, the owner thread. The backend does its own locking.

**Time**: every call returns within 10 seconds or the VM is stopped. A guest write-zeroes
request can cover 64 GiB (32 ranges of 2 GiB); qcow2 answers it with zero flags (table updates
only), and a gate times exactly that case on a fresh overlay. Opening (including a refcount
rebuild) is not under the limit.

**Durability**: guest FLUSH = fsync, F_FULLFSYNC only at power-off (owner's flush policy);
sticky sync failure per disk, unchanged.

**Read-only, device side**: offer the read-only bit, do not offer flush, discard or
write-zeroes. A write, discard or write-zeroes that arrives anyway gets I/O error; a flush gets
"unsupported", which is what the decoder already answers when flush is not offered (tested). A
read-only backend's `sync` succeeds without a call.

**Locks**: writable top image exclusive (today's flock); read-only images and backing files
shared, so VMs can share one base or ISO and a writer is refused while a reader holds it. On a
file system without flock (network shares return "unsupported"), a read-only image or backing
file opens with a warning; a writable one is refused as today. QEMU uses a different lock type,
so natemu's lock does not exclude QEMU (true today).

**The ten behaviours every backend keeps**: (1) short transfers returned as is, never zero
progress, errors never retried; (2) safe under four workers plus the owner; (3) fsync on guest
FLUSH, F_FULLFSYNC only at power-off; (4) sticky sync failure; (5) exclusive lock on the
writable image, shared on read-only and backing; (6) bounds checked before the backend; (7)
discard a hint, write-zeroes reads back as zeros; (8) every call under 10 s; (9) fixed capacity
and stable id; (10) the first error's text is the request's error.

**The raw backend** is today's code moved unchanged (open checks, exclusive lock, id from device
and inode, reads, writes, fsync, F_FULLFSYNC, hole logic behind `discard` and `write_zeroes`).
Additions: read-only open with a shared lock, and the first-sector guard below.

### Format detection

`open` reads the first bytes (and the last 512 for footer formats):
- qcow2 magic → qcow2 (strict opener below).
- VMDK, VHDX, VDI, VHD (footer "conectix") and DMG/UDIF (trailer "koly") → refused: "this looks
  like a VHDX image; natemu reads raw and qcow2 (qemu-img convert -O qcow2 ...)". My proposal
  for "must not silently boot as raw": refuse rather than warn, because a guest would only see
  garbage.
- anything else → raw (ISO files included).

**First-sector guard (owner decision).** On a writable image detected as raw, a guest write
that would make the first four bytes the qcow2 magic fails with an I/O error, as QEMU does.
Otherwise the next launch would follow a backing file name chosen by the guest and expose any
host file natemu can read. Consequence: a guest cannot store a qcow2 file directly on a whole
unpartitioned raw disk.

### qcow2: what the backend must guarantee

The base is Cloud Hypervisor's qcow code (owner decision; Apache-2.0 and BSD-3-Clause). What I
verified by reading it, at commit e9ea04e4 of November 16, 2026 (a shallow clone; release tags
not compared yet, that is Lane B's first step):
- All table changes happen under one reader-writer lock. A write to an unallocated cluster
  allocates it, writes the copy-on-write fill (or zeros) into it and updates the L2 entry, all
  under that lock; guest data is then written outside it. So a second writer to the same
  cluster finds it allocated (no double allocation, no leak), and a reader of another sector
  of that cluster sees the filled cluster, never unwritten space. Allocating writes are
  serialised, a speed cost the benchmark shows.
- Freed clusters are reused only after a flush.
- A writable v3 image gets the dirty bit, synced, at open; an image found dirty is repaired by a
  refcount rebuild, and only when opened writable. A read-only or backing image is never
  written; reads do not need refcounts.
- Its flush writes L2 tables and refcount blocks, syncs, then writes L1 and the refcount table.
  For an L2 table moved to a fresh cluster that order is right (the new table is durable before
  L1 points at it). For an L2 table changed in place it is not: the table is written before the
  data it now points at has been synced. Its sync calls are F_FULLFSYNC on macOS.

**Write ordering.** Data must reach the disk before any table entry that points at it, and an
L2 table before the L1 entry that points at it. Otherwise, after a host crash, a cluster whose
entry landed first reads as zeros (losing even base-image data in sectors the guest never wrote),
or an L1 entry points at an L2 table that never arrived (losing every mapping under it). Rule
for the facade, one order everywhere (flush, cache eviction, exit):
1. data (already written by the guest writes and copy-on-write fills);
2. `F_BARRIERFSYNC` (a cheap macOS ordering barrier);
3. dirty L2 tables and refcount blocks;
4. `F_BARRIERFSYNC`;
5. L1 table, refcount table and header;
6. `fsync(2)` for a guest FLUSH, F_FULLFSYNC at power-off.

An eviction of an in-place L2 table does steps 1-3 only (no L1 change is needed).
Further rules:
- **Dirty bit.** Refcount blocks need no ordering against L2: the dirty bit stays set while the
  image is open, so refcounts are rebuilt after any crash. Only a clean power-off drain clears
  it (after its F_FULLFSYNC, then another F_FULLFSYNC). Every other exit leaves it set.
- **Lock.** The table lock is held for steps 2-5 but not for the final fsync (Sticky already
  serialises syncs), so reads, the owner's inline ones included, do not stall behind an fsync.
- **Freed clusters.** Clusters freed since the last flush go back to the free list only after
  that flush's fsync returns. Upstream moves them under the lock right after writing the tables;
  with the fsync outside the lock, a reused cluster could otherwise be counted twice after a
  crash.
- **Failed table write.** After one, every later write and sync of that disk fails (it joins the
  sticky failure); reads continue.
- **Exits without power-off** (Ctrl-C, Ctrl-A x, watchdog, VM error) skip the power-off drain.
  The backend's drop runs steps 1-5 with no fsync and leaves the dirty bit set, so completed
  writes survive a natemu exit as they do on raw. Upstream's drop clears the dirty bit after its
  sync; that is changed. A hard kill also leaves the bit set; the next writable open rebuilds the
  refcounts.
- **Whole-cluster write over a backing file.** Upstream skips the copy-on-write fill, so the
  new cluster is unwritten until the guest's data lands outside the lock. A read of that cluster
  racing the write can see zeros, which are neither the old nor the new data. I propose to
  document this rather than serialise. Only overlapping in-flight read and write requests can
  observe it, and virtio-blk promises nothing for those. The crash side is covered by the
  ordering above.

**Strict opener.** Version 3 for writing; a v2 image opens read-only only (v2 has no zero flag and
no dirty bit). Refused: encryption, external data file, extended L2, a non-default compression
type, unknown incompatible bits; writing to an image with the corrupt bit or internal
snapshots. Compressed clusters are read as an I/O error (compression code dropped).

**Backing files.**
- (Owner decision.) A top image's embedded backing name is followed only when it is the file the
  config's `backing` names (same device and inode on the opened descriptor, not path strings).
  An image that names a backing the config does not is refused, since a downloaded qcow2 could
  otherwise name any host file. The error says what to add, for example `disk "disk0":
  overlay.qcow2 is based on /x/base.raw; add backing = "/x/base.raw" to this disk (or --set
  disk.disk0.backing=/x/base.raw)`. So `--disk overlay.qcow2` alone gets this error. Deeper links
  follow the rules below.
- Relative backing names resolve against the referring image's folder (the qcow2 rule).
- Depth capped at 16; loops detected by device and inode.
- The backing format comes from the header extension when present, otherwise from detection.
- Reads past the end of a shorter backing file return zeros.
- Backing files are opened read-only with a shared lock.
- **`backing` in the config**: if `path` does not exist, natemu creates a v3 overlay there
  (exclusive create, so it never overwrites), recording the base's absolute path and format; if
  it exists, it must be a qcow2 whose backing is that same file, else refused. *(Unverified:
  that the vendored header code creates an overlay with a backing name; if not, a small writer
  is added.)*

**Throw-away overlays** (placeholder, owner decision): an option to create the overlay in a
temporary folder and delete it after the launch would suit the test tiers. To be decided before
the runners move to qcow2.

## 5. Work split: two lanes

**Shared surface**: the block device file and the `open` signature. Lane B makes the small seam
change first and lands it on main; afterwards Lane B never edits `block.rs`, and Lane A edits only
its feature offer and request decoding (read-only).

**Merge order**: seam (B0) → Lane A → Lane B. B's steps 1-3 can land before A if ready; step 4
(overlays from `backing`) needs A's config keys. Cargo.lock is the only expected textual conflict.
Removal of old flags and the "no file means MMIO" rule is a later separate step.

### Lane B0: the seam (about 1-2 days)

Files: `virtio/block.rs` (trait, `Disk` built from an `Image`, hole logic moved out, offers
follow the image's flags); new `crates/host/src/disk/mod.rs` (trait, `Image`, `open`, detection;
qcow2 refused as "not built yet"); new `disk/raw.rs`; `virtio/mod.rs` (block constructor);
`virtio/testing.rs` (its controlled wrapper wraps the raw backend); the `virtio/pci.rs` test
constructor; `crates/driver/src/drivers/program.rs` (it builds a block device from a path).
Gate: block unit tests unchanged and passing; moved discard/write-zeroes tests; the block runner;
I/O coverage on MMIO and PCI.

### Lane A: config, devices, multi-disk, read-only, shared lines

Files: `crates/cli/src/main.rs`, new `crates/cli/src/config.rs`, `crates/cli/Cargo.toml` (toml);
`crates/host/src/linux.rs` (device lists, placement); `virtio/mod.rs` (constants kept as the
first-of-kind values, so the test driver's fixtures compile unchanged); `boot/acpi.rs` (routing
entries for shared lines, tests); `irq.rs` and `virtio/pci.rs` (shared lines); `block.rs`
(read-only offer and decoding); `crates/driver/src/drivers/linux.rs` and `program.rs` (adapt to
`Boot`); `tools/run_virtio_block.py` (new guest checks).

1. **Pin old launches.** Extract the table-building function, add the 8 × 3 digest test. Gate:
   unit tests; one boot smoke.
2. **Config loader, no behaviour change.** Types, file, typed layers, `--set`, validation, env
   warnings. Gate: CLI unit tests per validation rule and per old spelling (boolean words,
   numeric command lines, paths relative to the file versus the working directory), plus an
   equivalence test: a list of old command lines gives the same `Boot` as before.
3. **Device lists and placement.** Gate: the step-1 digests unchanged; boot smoke on MMIO and
   PCI; the block runner.
4. **Several disks.** Gate: three disks on MMIO, PCI and mixed; the guest checks that
   `/dev/vda..c` match the list order by size and serial; the MMIO case's speed noted.
5. **Read-only disks.** Gate: unit tests for the refusals; an ISO (made with `hdiutil
   makehybrid`) as a second disk mounts, reads, reports read-only, refuses a write; two VMs
   share one read-only image; a writer is refused while a reader holds it.
6. **Shared PCI interrupt lines (its own stage).** Per-line source bits kept inside the chipset
   state, under the chipset's existing mutex (no second lock). The line's level is the OR of
   its sources, and the existing line logic is driven only when that OR changes, as it already
   passes only changes. Only the PCI fallback path uses it; MSI-X and MMIO paths are untouched.
   The I/O APIC already redelivers a level line still high at end-of-interrupt (read in the
   code). Disks 7 and up need this stage: each device's line must be one of 16-23 (the device
   constructor asserts it), so without sharing there is no line to give them.
   Gates:
   - unit tests: two sources, interleaved raise and lower, end-of-interrupt with the line still
     high redelivers;
   - a VM test with eight PCI disks plus NIC and entropy under `pci=nomsi`, fio on all disks at
     once at 1 and 4 vCPUs, no I/O timeouts;
   - MSI-X turned off without a device reset (a driver unbind and rebind, or a kexec): the device
     raises its line at once from a stale interrupt status (documented in the PCI transport). On
     a shared line Linux may then disable the line for every device on it. The test checks that
     the other devices on that line keep working;
   - the existing `pci=nomsi` runs.

   The eight-disk gate will thrash the resident's eight-entry device-page cache; that is a speed
   note, not a failure. **Risk**: the interrupt code is shared by every device and every guest; a lost
   or stuck shared line hangs I/O, and only guests without MSI-X exercise it. Built in M1 (owner
   decision); old launches never share a line. **Fallback**, documented only, if the gate cannot
   be met: each PCI function keeps its own line, which caps PCI at 6 disks like MMIO; the cap
   is lifted later by landing this stage alone, since only the placement function and the line
   object change.
7. **File default PCI; no-file default MMIO**; usage text. Then the full suite once, before
   merge.

### Lane B: qcow2 behind the seam

Files: new workspace crate `crates/qcow` (vendored code with its licence headers, plus the
Apache-2.0 and BSD-3-Clause texts), keeping its dependencies apart from the library (whose
registry dependencies today are serde, serde_json, thiserror and log; the crate adds libc and
whatever else survives step 2); `crates/host/src/disk/qcow.rs` (facade); `disk/mod.rs`;
`crates/host/Cargo.toml`; `tools/run_io_matrix.py` and `tools/run_io5.py` (image format option);
`tools/run_suite.py` (a qcow2 I/O coverage step).

1. **Choose the snapshot.** Compare Cloud Hypervisor's latest release tag with main for the qcow
   folder: what changed recently, open qcow issues and fixes. Record the chosen commit and why.
   Read its concurrency paths again at that commit (the points listed in section 4) and record
   the result. Gate: a short written note; nothing built.
2. **Vendor and build on macOS.** Keep header, parser, metadata, refcount, raw-file, backing and
   caches; drop the async engines and compression; replace `/proc` path lookup with the caller's
   path, fallocate with F_PUNCHHOLE, the VMM utility crate with std and libc. The file layer
   becomes a small trait, so tests can run on a recording file. Gate: the crate's own tests pass
   on macOS.
3. **Facade, ordering and locks.** The read and write paths, flush order and the drop rule from
   section 4, locks, dirty bit. Gates:
   - unit tests; the 64 GiB write-zeroes timing on a fresh overlay;
   - **targeted concurrency test**: many threads issuing sub-cluster writes and reads to the same
     unallocated clusters over a backing file, compared with the same operations on raw;
   - **differential test**: a random write/zero/discard/read sequence on qcow2 and on raw, then
     `qemu-img check` clean and `qemu-img compare` identical;
   - **crash test that sees ordering**: a natemu kill keeps the page cache, so it cannot test
     ordering. Instead the recording file keeps every write and every barrier and sync. A
     simulated crash keeps everything before the last sync, everything before each barrier that
     a later kept write depends on, and a random subset of the other writes. The image is then
     reopened writable (rebuild) and checked: `qemu-img check` clean, every sector holds its last
     flushed value or a later write, and base-image data is never lost. Many seeds, and fixed
     cases among them: keep the L1 write and drop the L2 writes of the same flush; keep an
     in-place L2 write and drop the data it points at; reuse a freed cluster and crash before the
     fsync of the flush that freed it. A real kill test is kept only for the rebuild path.
4. **Strict opener and detection.** Gate: qemu-img images of each refused kind are refused with
   a clear message; the other formats' magic numbers are refused; a v2 image opens read-only;
   backing rules (relative names, depth, loops, mismatch with config) tested.
5. **Overlays from `backing`** (after Lane A). Gate: Debian boots from an overlay on the raw base,
   powers off, boots again and verifies; the base's hash is unchanged.
6. **Runner support.** The benchmark runners assume raw images: the fio matrix runner clones
   with `cp -c` and runs e2fsck and digests on the raw file; IO5 grows the file to 16 GiB and
   runs e2fsck and debugfs on it. The option prepares qcow2 inputs once (`qemu-img convert -O
   qcow2 -o preallocation=metadata`, because a plain convert leaves free space unallocated; or
   `qemu-img create -b base -F raw` for an overlay), grows with `qemu-img resize`, clones the
   prepared file per run, and converts back to a temporary raw file for the existing checks.
   Overlay runs launch with a config file (or `--set`) that names the base as the disk's
   `backing`, because the backing policy refuses an overlay whose base the config does not name.
   Then the full suite once, then the benchmark check.

### Performance check (end of Lane B only, under the VM lock)

The runners take the VM lock themselves, so one driver calls them one after another. Existing
suites only:
- **Noise first.** The run-to-run spread per fio job is measured from the raw runs (before and
  after the seam together). A difference inside that spread is noise, not loss.
- **Raw before vs after the seam**: fio matrix (2 vCPUs, PCI), main before B0 versus final, five
  runs each, interleaved.
- **qcow2**: fio matrix on (a) the preallocated qcow2 and (b) a fresh overlay on the raw base,
  three runs each, against the final raw runs above.
- **IO5**: one run from an overlay against the recorded raw run, comparing the solo-build make
  time (the combined make time tracks the side loops).

**Tolerance (owner decision)**, the same for raw before vs after the seam and for qcow2 vs raw
(preallocated and overlay alike):
- a loss under 10% beyond noise (geometric mean of per-job medians) passes;
- under 5% is the aim;
- any loss over 5% is reported with its cause;
- any single job more than 20% slower is flagged.
- The unclean-exit rebuild time is measured once on IO5's 16 GiB image.

That is 16 fio matrix runs and one IO5 run.

## 6. Risks and open questions for the owner

Decided since the first draft: the backing-name policy, the tolerances, and shared PCI lines in
M1. Still open:

1. **Throw-away overlays**: decide before the runners move to qcow2.

Risks to keep in view:

2. **Windows installers (M4)** may need an emulated CD-ROM or SATA controller rather than a
   read-only virtio disk *(unverified)*; the shared lines make such a device cheap to add later.
3. **Vendored code**: about 6,000 lines to review and own; upstream restructured it in 2026.
4. **Unclean exits** cost a refcount rebuild at the next writable open (time unmeasured).
5. **Disk-space payoff**: raw images are already sparse; measure where the 86 GB goes first.

## 7. Disagreements with the review

- **Concurrent allocation** (review defect 2): the hazards are real requirements, but the
  Cloud Hypervisor code already meets them. Allocation, copy-on-write fill and the L2 update
  happen together under its write lock, before any other thread can see the entry. So no
  in-flight allocation table is needed. I keep the review's targeted test as the gate, and I
  recheck this at the chosen snapshot.
- **Refcount-before-L2 ordering** (part of defect 1): not needed while the dirty bit is held for
  the whole open, because refcounts are rebuilt after any crash. Data before L2 and L2 before L1
  are needed and are in the design.
- The second review confirmed these three points.
- **Dirty read-only images** (risk 6): already handled upstream (no rebuild unless writable);
  kept as a test rather than new code.
- Editorial points accepted (clap does record argument positions; the duplicate-key rule stays
  anyway).
