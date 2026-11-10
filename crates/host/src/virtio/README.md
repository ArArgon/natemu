# Copy-first virtio owner

Code map: `mod.rs` holds the MMIO transport (register offsets in `reg`, status
bits in `status`), the shared lifecycle state, the owner loop and virtio-rng;
`queue.rs` the queue registers and the checked parse of owned metadata copies;
`block.rs` virtio-blk, its disk workers and the block owner loop; `net.rs` the
network frontend and `PacketPort`; `testing.rs` the test driver's disk-call
schedules. Entropy and network end a request through `Virtio::complete`; block
completes answered requests in batches. Either way a window publishes whole
requests: payload and status, used elements, one used index, then a read of the
available ring's flags and index.

The first production consumer is modern MMIO virtio-rng: VERSION_1 only, one
8-entry direct split queue, one request in flight, at most 256 returned bytes.
Larger valid writable chains receive a legal short completion. Indirect and
EVENT_IDX are not negotiated. Resource discovery and registry placement share
`RNG_RESOURCE`. The registered MMIO adapter owns no guest pointers.

`Virtio::run` runs on a separate scoped thread. Boot must stop and join it before
IRQ detachment or VM destruction. Queue notification retains an IRQ busy guard
until the owner drains available work. Ordinary register access, ACK, physical
IRQ level and lifecycle state are serialized under one short state mutex.
That mutex is never held during a RAM window or entropy syscall. The state
records the incarnation of every request in flight. Deferred reset closes
admission, cancels the owner's pending `CopyPermit`, and waits until no request
of the old incarnation remains in flight and no window for it is open. Conflicting MMIO accesses queue by token, with
at most one outstanding access per vCPU. Queue disable preserves configuration;
full device reset clears it. Neither can reuse a live request's resources.

The owner copies the entire bounded descriptor table and available ring in one
window. Parsing occurs afterwards against a private `GuestMemoryMmap`, using
rust-vmm's checked traversal; this allocation contains only owned metadata and
does not map natemu guest RAM. The complete chain is collected before any backend
effect. Only GPA range validation uses natemu's mapping metadata, without
reading, retaining, or dereferencing the returned pointer. Entropy goes into a
256-byte owned array. A second window writes payload extents in descriptor order,
then the used entry and used index, then reads the available ring's flags and
index (notification suppression, and for block whether more work was published).
Owner indices advance once after successful commit. Malformed requests fail the
device with NEEDS_RESET and a configuration interrupt, without completing a
valid prefix. A coordinator operational failure cancels the VM.

Inside a granted write window, the memory coordinator posts invalidations for
written physical pages through the existing resident code-page records. After
release each resident drains them before another acknowledgement or guest access.
Writes to previously observed guest page-table pages also invalidate global fetch
mappings. Those page-table marks are never cleared, so a guest that recycles
former page-table pages as I/O buffers makes more windows flush every vCPU's
translations; the performance log's `io` line (`--perf-log`) counts granted
windows, write windows and table-write windows to show it. HLT-sleeping residents may remain quiescent across multiple windows;
on wake they compare the latest table-change epoch with their previously serviced
epoch, so a later read-only window cannot hide an earlier table write. The ordered
HLT promise permits admission without waking that CPU; AP-mailbox waits still
acknowledge each request. Unaffected compiled objects survive. Native backend calls still use owned buffers outside
the window; this change does not introduce zero-copy or relax exclusion.

## Dependency identity

The crates come from the owner's fork `ArArgon/natemu-rust-vmm` (branch
`natemu`) as a Cargo git dependency pinned at `5ede8fee` ("virtio-queue: add
checked descriptor-chain traversal") in `crates/host/Cargo.toml`; `Cargo.lock`
records the same commit and `cargo fetch` downloads it. That commit is upstream
`671e367124cf7717d832b21de86b62eccf33ac27` plus the checked traversal patch and
its tests. To work on the patch locally, override the source for one command
(`cargo --config 'patch."https://github.com/ArArgon/natemu-rust-vmm".virtio-queue.path="../rust-vmm/vm-virtio/virtio-queue"' ...`,
and the same for `vm-memory`) or in a git-ignored `.cargo/config.toml`.

Selected upstream crates: virtio-queue 0.18.0 (Apache-2.0 AND BSD-3-Clause),
vm-memory 0.18.0 with `backend-mmap` and default `rawfd` (Apache-2.0 OR
BSD-3-Clause), virtio-bindings 0.2.7 (BSD-3-Clause OR Apache-2.0), and
vmm-sys-util 0.15.0 (BSD-3-Clause). The host library now has registry dependencies
through these crates; resident and ABI remain dependency-free. Exact registry
versions/checksums are in the workspace Cargo.lock. Darwin's new direct
transitive packages include libc, log, bitflags and thiserror (MIT OR Apache-2.0),
with thiserror's proc-macro dependency tree shared with the existing host CLI.
Windows-only winapi entries are lockfile resolution, not Darwin runtime code.

## Verification limits

Focused tests cover invalid full-width selectors/features/geometry, malformed
chain suffixes/cycles/indirect rejection, index wrap, deferred-reset cancellation
and access ordering, QueueReady readback and IRQ ACK state. They do not replace
live Linux tests or demonstrate disk/network backend shutdown. The entropy
syscall is synchronous on the independent owner and has no callback; disk and
packet lifetime and failure handling are described below. The native packet
backend still awaits its deployment choice and implementation.

## Raw disk and shutdown

`--disk FILE` opens an existing nonempty writable regular file with sector-aligned
capacity, locks it for cooperating natemu launches and holds its identity/capacity
for the device lifetime. Other writers or truncators must be excluded; the lock
cannot enforce that against unrelated software. Block uses the same modern
transport, with queue size 128, 4096-byte data segments, 32 segments and 128 KiB
per request. SIZE_MAX/SEG_MAX/VERSION_1 are required; BLK_SIZE/FLUSH are offered.
IN/OUT/FLUSH/GET_ID operate on complete logical byte streams, including split
headers and combined data/status descriptors. Used length covers only the
initialized writable prefix. A failed IN publishes no partial data and len=0.

Block keeps up to min(queue size, 32) requests and 4 MiB of owned buffers in
flight (IN buffers and the gathered header/OUT stream are reserved at admission;
admission pauses at either bound). The owner admits every published head the
bounds allow from one metadata snapshot (after a notification, a reused
snapshot's exhaustion triggers one fresh capture in the same round), advancing
the consumed index at admission, and gathers the new requests' headers and OUT payloads in as few
windows as MAX_BYTES (1 MiB) and MAX_SPANS allow. Their disk calls go to a fixed
pool of four worker threads sharing the file; each finished job rings the owner's
one wake source (the bell that guest notifications, resets, power drain and stop
also ring), so the owner never blocks on one job. Answered requests complete in
answering order: each window takes the next used slots, writes one used index and
reads the available ring's flags last; the last window also refreshes the
metadata snapshot when it fits. A malformed chain fails the device; requests
already admitted still complete visibly and nothing is admitted after.

The workers own only the file, their jobs' buffers and a result board. They run
pread/pwrite (`FileExt::read_at`/`write_at`) and F_FULLFSYNC (`File::sync_all`,
which uses F_FULLFSYNC on Apple platforms and retries it on EINTR) in any order;
a FLUSH covers every write completed before it (virtio 1.2 §5.2.6). The
launch lock is `File::try_lock` (flock exclusive, nonblocking). Positive short calls advance their exact
suffix; zero/error (including EINTR) reports IOERR without replay. Negotiated
FLUSH permits writeback; otherwise each successful OUT includes full-sync.
The stable ID derives from the opened file's device/inode and survives reset.

The power-triggering port instruction commits before the resident enters a
nonreturning wait that services copy windows. Quiesce closes new admission and
completes the requests already in flight, then performs a final full-sync on a
worker and joins the workers. Only successful retirement permits PowerOff.

A native disk call has a ten-second deadline from when a worker starts it;
shutdown has a thirty-second poll deadline. A stalled native syscall is not
forcibly canceled: its owned file/buffers remain alive in detached workers (jobs
still queued never run), with no guest RAM, IRQ or Machine reference. The VM fails and the normal CLI exits its per-VM process. Library
embedders must treat this as worker-process fatal, terminate/restart the worker,
and not repeatedly create VMs in that failed process. An advisory file lock can
remain held until the syscall retires. No failed return promises rollback or
successful durable drain.

## Network frontend

`Virtio::network` constructs a modern RX/TX device and an owned-packet `PacketPort`.
Its caller must admit the actual backend before boot, service the port on an
independent backend owner, then stop/join native callbacks under its supervisor.
No backend launch or privilege acquisition occurs here. The normal Boot/CLI
network option awaits the deployment decision; this constructor also supports
the direct-guest frontend fixture.

Queues 0/1 have a maximum of 256 direct descriptors. VERSION_1 is required;
MAC, MTU and STATUS are offered, with MTU1500 and no offloads. Frames exclude
FCS and are at most 1518 bytes including one VLAN tag. One accepted TX and one
RX packet bound storage; headers are the modern 12-byte form even without merged
buffers. Descriptor boundaries may split headers or payloads. RX writes one
chain with num_buffers=1; TX used length is zero. Unknown TX flag bits and inactive
checksum/header hints are ignored; unnegotiated offload requests fail the device.

Queue-only disable cancels only the matching operation and preserves other-queue
indices, notifications and shared pending IRQs. Full reset invalidates both queue
identities. The single owner alternates eligible queues; RX arrival drives its
scan without needing a new guest kick. A full or inactive RX slot drops the frame.
A separate endpoint-lifetime busy guard keeps an idle VM alive while the backend
can deliver future packets; it retires when that endpoint is dropped after shutdown.
A valid chain too short for an incoming frame also drops it without consuming
the head. All routes and directions are checked before any packet effect.

TX enqueue and reset admission share the short state lock. A full endpoint drops
that packet; a submitted send is attempted once and its callback result retires
before used publication or reset. Transient loss is an ordinary backend success;
terminal loss cancels the VM. A ten-second timeout may leave an owned-only backend
callback alive and is process-fatal, not permission to replay. PacketPort closure
is terminal until the device is stopped/drained. Backend failure and successful
drain publication share the state lock so S5 cannot hide an earlier failure.
