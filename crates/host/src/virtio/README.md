# Copy-first virtio owner

Code map: `mod.rs` holds the MMIO transport (register offsets in `reg`, status
bits in `status`), the shared lifecycle state, the owner loop and virtio-rng;
`queue.rs` the queue registers and the checked parse of owned metadata copies;
`block.rs` virtio-blk, its disk workers and the block owner loop; `net.rs` the
network frontend and `PacketPort`; `testing.rs` the test driver's disk-call
schedules. Entropy and network end a request through `Virtio::complete`; block
completes answered requests in batches. Either way a copy batch publishes whole
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
window. Parsing occurs afterwards on that owned copy, with natemu's own checked
walk (`queue.rs`: every index in range, at most queue-size descriptors, only the
NEXT and WRITE flags, total length within u32); it never reads natemu guest RAM.
The complete chain is collected before any backend
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
mappings. That window clears the written pages' page-table marks (every vCPU
drops all its translations before guest access; a later walk marks the page
again), so a former page-table page recycled as an I/O buffer costs one window,
not one per write (before November 11 marks were never cleared, and a fio buffer
on such a page sent every write through a window: sequential reads fell to a
quarter or less). The performance log's `io` line (`--perf-log`) counts granted
windows, write windows and table-write windows. HLT-sleeping residents may remain quiescent across multiple windows;
on wake they compare the latest table-change epoch with their previously serviced
epoch, so a later read-only window cannot hide an earlier table write. The ordered
HLT promise permits admission without waking that CPU; AP-mailbox waits still
acknowledge each request. Unaffected compiled objects survive. Native backend calls still use owned buffers outside
the window; this change does not introduce zero-copy or relax exclusion.

Devices now copy through `Backing::copy_guest_windowless` (November 9 design
"Windowless copies with targeted invalidation" in `docs/restructure/io-plan.md`):
no vCPU pauses in the usual case. The batch is validated whole; if any written
page carries a page-table mark the whole batch takes the window above
(`table_fallbacks`). Otherwise the permit is granted before the first write (a
reset cancels before it or waits for the call, as for a granted window), the
written pages' code marks are cleared and posted to their holders, which are
rung; the body (payload, status, used elements, metadata reads) is copied, a
full barrier follows, and the marks are rechecked (a mark taken during the copy
is posted too). The used index is written only after every posted vCPU (and every vCPU that
still owes a drain of a written page from an earlier writer) has
drained its post, is halted in HLT (it drains on withdrawal, before guest
access) or has ended; after 50 ms the used index and the reads after it go
through a window instead (`drain_expiries`). Then a full barrier, the available
ring's flags and index, and any snapshot refresh. Fields of up to 16 bytes move
as whole aligned accesses; each read is followed by an acquire barrier, and the
snapshot reads the available index first, then its entries, then the descriptor
table. Windowless batches take no epoch lock. An error after the grant is fatal to the VM. The `io` perf line adds
`windowless`, `windowless_posted`, `recheck_holders`, `drain_waits`,
`drain_us`, `drain_max_us`, `drain_expiries` and `table_fallbacks`; `windows`
now counts only fallback windows and the test drivers' own windows.

## Dependency identity

The virtio code has no registry dependency of its own. It used rust-vmm's
`virtio-queue` and `vm-memory` (from the owner's fork) until November 12, only for
checked descriptor-chain traversal over an owned copy; that walk is now natemu's
(`queue.rs`), with the same or stricter checks, tested in `queue.rs`, and the fork is no longer a
dependency.

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
also ring). The one exception: a lone request in flight whose call is a read or a
write without sync is called by the owner itself, outside the lock, saving two
thread hand-offs; kicks stay suppressed meanwhile and the completion re-reads the
ring. A reset, disable or power-off arriving then waits for that call as it would
for a worker's. Answered requests complete in
answering order: each window takes the next used slots, writes one used index and
reads the available ring's flags last; the last window also refreshes the
metadata snapshot when it fits. A malformed chain fails the device; requests
already admitted still complete visibly and nothing is admitted after.

The workers own only the file, their jobs' buffers and a result board. They run
pread/pwrite (`FileExt::read_at`/`write_at`) and syncs in any order; a FLUSH
covers every write completed before it (virtio 1.2 §5.2.6). A guest FLUSH (and
write-through) is a plain fsync(2), as QEMU's: it survives a natemu crash, not a
host power loss or macOS crash (owner decision, November 10). The power-off
drain's final sync is F_FULLFSYNC (`File::sync_all`, which retries on EINTR, as
the fsync call does). The
launch lock is `File::try_lock` (flock exclusive, nonblocking). Positive short calls advance their exact
suffix; zero/error (including EINTR) reports IOERR without replay. Negotiated
FLUSH permits writeback; otherwise each successful OUT includes an fsync.
A failed read or write does not affect later FLUSHes. A failed host sync is
sticky (`Sticky` in `block.rs`, as QEMU's file-posix): every later FLUSH,
write-through OUT and the power-off full sync then fail without a call, for the
rest of the launch; a device reset does not clear it. Syncs are serialised (as
QEMU's per-device flushes), so one racing a failing sync fails too; the refusal
is logged once. A guest reboot (reset register, or FEH to port 64H) ends the run
with `Power(2)` after the same drain and final sync as S5.
The stable ID derives from the opened file's device/inode and survives reset.

The power-triggering port instruction commits before the resident enters a
nonreturning wait that services copy windows. Quiesce closes new admission and
completes the requests already in flight, then performs a final full-sync on a
worker and joins the workers. Only successful retirement permits PowerOff.

A native disk call has a ten-second deadline from when it starts (an owner's
inline call is watched by a scoped watchdog thread, which cancels the VM);
shutdown has a thirty-second poll deadline. A stalled native syscall is not
forcibly canceled: its owned file/buffers remain alive in detached workers (jobs
still queued never run), with no guest RAM, IRQ or Machine reference; the VM
fails and the normal CLI exits its per-VM process. An owner's inline call is
different: it cannot be detached. At the deadline the watchdog prints the
VM-fatal reason (call kind, file offset, elapsed time) and cancels the VM, but
the owner thread, and with it the run's teardown and the process, stays until
that syscall returns, however long that is. Library
embedders must treat this as worker-process fatal, terminate/restart the worker,
and not repeatedly create VMs in that failed process. An advisory file lock can
remain held until the syscall retires. No failed return promises rollback or
successful durable drain.

## Network frontend

`Virtio::network` constructs a modern RX/TX device and an owned-packet `PacketPort`.
Its caller must admit the actual backend before boot, service the port on an
independent backend owner, then stop/join native callbacks under its supervisor.
No backend launch or privilege acquisition occurs here. `PacketPort::on_ready`
gives the backend a doorbell, rung (under the device lock, so it must only signal)
when a TX packet is queued, when an RX request finishes, and at drain and stop.

## Network backend (`--net`)

`slirp.rs` is user-mode NAT on libslirp (owner, November 12; QEMU's `-netdev user`),
through hand-written bindings for libslirp 4.9 (config version 6, layout checked by a
unit test), linked from Homebrew by `crates/host/build.rs`. No root, entitlement or
host interface. The guest sees 10.0.2.0/24: gateway 10.0.2.2, DNS 10.0.2.3 (forwarded
to the host's resolvers), DHCP lease 10.0.2.15, MAC 52:54:00:12:34:56. IPv4 only, and
the guest cannot reach the host's loopback (as with VZ NAT); reach host services on
the host's LAN address. No port forwarding.

`Slirp::new` starts the stack before boot (a failure refuses the launch). One thread
(`Slirp::run`, in `Prepared::run`'s scope, joined after the devices stop) owns it:
it feeds queued guest TX frames to `slirp_input`, hands slirp's output frames to the
guest in order through `PacketPort::receive` (up to 4096 waiting, then new ones drop;
a refused frame waits for the next doorbell or at most 100 ms), and `poll(2)`s slirp's
sockets plus the doorbell. It feeds at most 64 TX packets per pass. The queue is deep
on purpose: under a full guest upload the device takes one RX frame per TX packet, so
the upload's ACKs queue with any download's data (8 streams each way peaked at 718),
and slirp's TCP recovers from a dropped segment only by timeout. With `--perf-log` the
backend writes `net {"backend_dropped":N,"guest_lost":M}` at exit (frames dropped at
that cap, and frames the device dropped because the guest had no RX buffer). It ends when the device stops or drains, after giving any accepted TX packet
its result. Slirp's state survives device resets, as in QEMU.

Checked by `tools/run_net_linux.py` (suite step `net-linux`): stock Debian Linux at
1/2/4 vCPUs gets its lease, pings the gateway, downloads a host file byte-exact over
HTTP, runs iperf3 TCP both ways and UDP, survives a driver unbind/rebind under a
receive stream (three times), ends cleanly at guest reboot, powers off with the
driver never loaded, and exits promptly on SIGINT under traffic; with the internet, DNS
and a CA-verified HTTPS download. `--cases stress` (not in the suite): 60 s of 8 streams
each way, 100 parallel streams, 500 short connections 64 at a time, UDP floods both
ways, then the network still works. Not yet covered: IPv6/ICMPv6, port forwarding,
a separate contained process, combined disk+network load.

Throughput (November 12, 2 vCPUs, iperf3 to the host, `natemu-raw/net-slirp-20261112-01/vmm-compare-01`):
upload ~3.5 Gbit/s, download ~5.7 Gbit/s, 8 streams each way ~3.4/2.75 Gbit/s (checksum
offloads on: upload +10%, 8 streams each way +10%/+30%, download unchanged); QEMU 11.1
x86 TCG with the same libslirp: ~0.77/1.7/0.55/0.48 Gbit/s. Before the pipelined TX
queue, held RX frames, EVENT_IDX and interrupt holding: 0.81/1.37/0.46/0.44 (the
guest was idle 93% of an upload, waiting on one synchronous packet at a time).

Known gaps: with 32-100 parallel guest uploads slirp's TCP (Reno, no SACK) loses
segments internally (measured before the pipelining: 2-4% fast retransmits). Idle ping
to the gateway averages 4-5 ms. A UDP flood toward the guest beyond what it takes is
dropped at the backend's 4096-frame queue. Every RX frame and TX packet is still its
own request (one capture, copy and publication each).

Queues 0/1 have a maximum of 256 direct descriptors. VERSION_1 is required;
MAC, MTU, STATUS and EVENT_IDX are offered, with MTU1500, plus the checksum offloads the
backend declares (`Offloads`): CSUM when `PacketPort::transmit` may complete a TX checksum
the guest left partial (NEEDS_CSUM: csum_start/offset must lie inside the frame, else the
device fails), GUEST_CSUM when every backend frame already has valid checksums (the device
then marks RX frames DATA_VALID). No segmentation offloads are offered yet: a backend must
take large frames first. Frames exclude
FCS and are at most 1518 bytes including one VLAN tag. Up to 256 queued TX packets and
64 held RX frames bound storage; headers are the modern 12-byte form even without merged
buffers. Descriptor boundaries may split headers or payloads. RX writes one
chain with num_buffers=1; TX used length is zero. Unknown TX flag bits and inactive
checksum/header hints are ignored; unnegotiated offload requests (NEEDS_CSUM without CSUM,
any GSO type, DATA_VALID on TX) fail the device.

Queue-only disable cancels only the matching operation and preserves other-queue
indices, notifications and shared pending IRQs. Full reset invalidates both queue
identities. The single owner alternates eligible queues; RX arrival drives its
scan without needing a new guest kick. With 64 frames held or the RX queue inactive,
`PacketPort::receive` refuses (the backend keeps the frame). An RX request that finds
no buffer puts its frame back and holds every frame until the driver notifies RX:
with EVENT_IDX it writes avail_event to ask for that notification and reads the
available index again (Dekker with the driver's index store and avail_event read);
a notification that arrives during that request is remembered, so the hold cannot
outlive it. RX completions otherwise ask for no notification (avail_event half the index space
ahead): RX buffers alone are not work. TX asks for one at the next unconsumed buffer,
and the owner looks again when the available index it reads after each completion shows more.

Interrupts: with EVENT_IDX one is due when the used index passes the driver's
used_event (read after the used index, behind a full barrier). The owner holds a due
interrupt while it keeps working, raising it when it goes idle, after 32 due
completions or 100 µs, or when RX starves (the driver refills only after seeing its
completions). With faster RX delivery, 8/20 µs left the guest more interrupts per packet
and cost 8 streams each way ~8%; 32/100 µs recovered it.

The backend hands every queued frame it can over under one lock
(`PacketPort::receive_from`). Frames live in heap buffers
that move between the queues without copying and return to a shared pool
(`PacketPort::buffers`), so neither thread allocates per packet.
A separate endpoint-lifetime busy guard keeps an idle VM alive while the backend
can deliver future packets; it retires when that endpoint is dropped after shutdown.
A valid chain too short for an incoming frame also drops it without consuming
the head. All routes and directions are checked before any packet effect.

TX enqueue and reset admission share the short state lock. A TX packet completes to
the guest once queued for the backend, like a NIC that has fetched it: the device does
not wait for the send, and a reset does not recall queued packets (the backend sends
them in order, once). A full queue holds the device's TX until there is room (outside
the lock; 10 s of that is process-fatal). Transient loss is an ordinary backend
success; terminal loss fails the device and cancels the VM. PacketPort closure
is terminal until the device is stopped/drained. Backend failure and successful
drain publication share the state lock so S5 cannot hide an earlier failure.
