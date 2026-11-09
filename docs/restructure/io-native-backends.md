# Native Apple I/O and the rust-vmm memory seam

November 7, 2026. Source-only follow-up to the [queue evaluation](io0-rust-vmm-evaluation.md).
This note recommends an evaluation direction; it does not select a backend,
change the current device profile, or claim integrated correctness/performance.
Apple evidence is the installed macOS26.4 SDK plus current first-party documentation.

Follow-up: the [current atomic-access census](io-atomic-access-census.md) identifies
the exact locked-operation fallbacks that host I/O would need to coordinate with,
plus separate scalar-helper and native-access qualification gaps. It supersedes
any interpretation that all ordinary MOV/string operations require whole-operation
exclusion or that only locked operations need an access-atomicity audit.

## Answer and proposed boundary

**Recommendation:** use rust-vmm for reusable guest protocol machinery and
native Apple facilities for host I/O. Keep natemu responsible for RAM ownership,
coherent snapshot/commit, translated execution and interrupts. For networking,
evaluate **vmnet directly** as the preferred native candidate before investing
in the adapted-libslirp backend. Apple's native packet interface has more useful
capabilities than a simple Ethernet socket: batching, scatter/gather, segmentation
offload and a virtio-header mode. Selecting it still depends on launch admission,
the promised guest network behavior and packet-contract checks below.

There is no need to couple guest RAM or the resident execution model to Apple's
high-level VM object to use vmnet. A narrow backend boundary can remain explicitly
macOS-specific; it need not grow a speculative portable platform layer.

## What implementing vm-memory means here

**Source-backed:** the selected `virtio-queue` uses `vm_memory::GuestMemory`.
Consume the existing crate and implement a short-lived local access view over
natemu-owned RAM. Do not recreate the crate or replace the RAM allocator merely
to adopt queues. The generic `virtio-device` register helpers themselves do not
take RAM; queue processing does. At pinned rust-vmm commit
`671e367124cf7717d832b21de86b62eccf33ac27`, the trait requires `check_range` and
`get_slices`, associated `PhysicalMemory: GuestMemoryBackend` and `Bitmap` types;
`physical_memory()` defaults to `None`. A backing/region representation still
needs implementation or wrapping. [Memory trait][memory], [queue source][queue].

**Integration inference:** create the view only during admitted descriptor/payload
snapshot or completion commit. Host I/O operates on owned request/packet buffers
after the view ends. The earlier synthetic `Window` uses private `GuestMemoryMmap`;
it is not an implemented adapter over natemu `GuestRam`. Volatile slices, address
validation and dirty tracking do not supply resident coherence or lifetime admission.
See the [evaluation's integration limits](io0-rust-vmm-evaluation.md#the-main-remaining-integration-work).

## Which Apple framework supplies what

| Facility | Source-backed interface | Consequence for natemu (inference) |
| --- | --- | --- |
| Hypervisor.framework | HVF creates a process VM, maps host pages into its physical address space and exposes vCPU primitives. [SDK VM API][hv] | Continue owning the machine/device model around HVF; this API is not a ready block/network device implementation. |
| vmnet.framework | Standalone interface handles, network configuration, packet reads/writes and dispatch callbacks. [Apple overview][vmnet], [SDK header][vmnet-h] | Fits a custom HVF VMM and custom virtqueues without adopting VZVirtualMachine. |
| Virtualization.framework | `VZVirtualMachine` owns a complete same-architecture machine. Network/storage configurations belong to its configuration. [VM header][vz-vm], [configuration header][vz-config] | The inspected API exposes no way to attach a standalone VZ virtio device to natemu's existing HVF VM or supply its custom guest-memory/coherence interface. Using VZ as the machine owner is a separate architecture change. |
| VZ networking attachments | `VZFileHandleNetworkDeviceAttachment` feeds a VZ device through a connected datagram socket. macOS26 `VZVmnetNetworkDeviceAttachment` embeds a vmnet network in a VZ device. [Socket header][vz-fd], [vmnet attachment header][vz-vmnet] | Neither is an export of Apple's virtqueue engine for an existing HVF VM. Call vmnet directly for this design. |

The newer `VZCustomVirtioDevice` API does not change this conclusion: Apple's
[WWDC26 session][wwdc] identifies it as macOS27 and creates it through
`VZVirtualMachineConfiguration.customVirtioDevices`. It is outside the macOS26
baseline and remains attached to a VZ-owned VM. The same session places the new
DiskImageKit API in macOS27; do not assume its availability because ASIF itself
was introduced in26.

## Native networking capabilities worth using

The following contracts come directly from the [SDK26.4 vmnet header][vmnet-h]:

| Capability | Contract and design consequence |
| --- | --- |
| Packet batches and scatter/gather | `vmnet_read`/`write` take an array of `vmpktdesc`; each descriptor has an `iovec` array. Honor returned packet counts and maximum sizes. Read/write maximum-count keys exist since15.0. This can batch owned packet buffers; it does not promise zero-copy or authorize retaining guest iovecs. |
| Dispatch notifications | Event callbacks run on a supplied dispatch queue. Use callbacks to schedule bounded backend work, then deliver completion through the existing VM owner. Reset/teardown must retire callbacks and pending work. |
| TCP segmentation offload | `vmnet_enable_tso_key` exists since11.0. Enabling it requires accepting and potentially producing large64K TCP frames. Guest features, host buffers, RX chain capacity and fallback behavior must agree. |
| Checksum offload | Since12.0, IPv4 header and TCP/UDP over IPv4/IPv6 are supported. Invalid basic headers/checksums may be dropped. UDP/TCP checksums of fragmented IP packets remain the client's responsibility. This is not transparent handling of every Ethernet packet. |
| Virtio metadata | `vmnet_enable_virtio_header_key`, since15.4, enables virtio headers on all packets and references Virtio1.1§5.1.6. Buffers need `max_packet_size + 12` bytes. This key must not be specified together with `vmnet_enable_checksum_offload_key`. |
| Network objects | Since26.0, shared/host-only networks have configurable NAT44/NAT66, DHCP, DNS proxy, router advertisement, prefixes, MTU, reservations and port forwarding. Defaults enable NAT44/NAT66/DHCP/DNS/RA; these are service capabilities, not proof that every required guest protocol works. |

**Forwarding opportunity:** virtio-header mode may let natemu carry negotiated
guest checksum/GSO metadata to the host backend instead of implementing all
segmentation/checksum work itself. This is a reason to design the internal packet
representation to retain validated metadata. Moving this work out of translated
x86 can use native host software; these APIs do not establish physical-NIC
acceleration, zero-copy or a performance gain. It is not evidence for blindly
forwarding guest headers: check negotiated feature masks, lengths, offsets, GSO
type/size, RX capacity and the exact Apple/header semantics first. The current
no-offload guest profile can be a correctness control while adding features.
TSO cannot be enabled unchanged with the current 2048-byte packet slots: large
frames need a revised buffer/RX profile and matching negotiated guest features.

**Common-path performance hypothesis:** batch descriptor snapshots, backend work
and completion publication within explicit byte/time bounds; reuse owned buffers
and suppress unnecessary notifications. This can amortize resident coordination,
host calls and IRQ delivery. Avoid waiting for a full batch on an idle connection.
Measure vCPU pause time, throughput, tail latency and CPU cost together before
increasing batch sizes. A backend offload cannot eliminate natemu's coherence
obligations, and scatter/gather alone is not guest-memory zero-copy.
Its current 2048-byte packet slot/1500-byte MTU profile cannot simply accept the
large frames required by TSO; buffer and RX-chain policy must change with it.

**Specific uncertainty:** the SDK documentation for `vmnet_interface_start_with_network`
lists TSO/checksum/isolation/MAC attributes but omits `vmnet_enable_virtio_header_key`.
The general key exists, yet this source pass does not establish that it works
with the new26 network-object start path. Check both start APIs explicitly before
combining the topology and virtio-metadata recommendations.

**Admission remains untested:** Apple's [networking entitlement documentation][entitlement]
describes `com.apple.vm.networking` as restricted to virtualization developers;
the framework page requires it for sandboxed use. A newer
[`com.apple.developer.networking.vmnet` page][new-entitlement] also exists but lacks
enough explanation to establish its replacement semantics. Verify the actual
signed launch/package model; neither SDK availability nor a Rust binding proves
permission to create interfaces. No privileged helper is proposed by this note.

## Native persistent storage

**Source-backed:** VZ's disk-image attachment exposes cache and synchronization
modes, but it attaches to VZ-owned storage devices. Its full mode promises permanent
storage synchronization; its fsync mode explicitly excludes flushing a drive's
internal cache. [Disk attachment header][vz-disk]. The native file API exposes
`F_FULLFSYNC` (fsync plus drive flush), `F_PREALLOCATE`, `F_NOCACHE` and advisory
read controls. [SDK fcntl header][fcntl].

**Recommendation:** keep a concrete native file backend with bounded owned
buffers and an ordered execution lane. Use the selected full-flush operation for
the advertised durability contract and handle its errors. Cache/preallocation/
read-ahead controls are later measured policy choices; native availability alone
does not establish a throughput improvement. VZ's storage attachment does not
remove natemu's guest request parser, queue completion or durability obligations.

## I/O threads and processes: what the references establish — November 8

**QEMU's thread pool is distinct from an IOThread.** An IOThread is an additional
event loop with its own `AioContext`, explicitly configured with
`-object iothread`; it is not a synonym for every worker performing disk I/O.
[QEMU IOThread documentation](https://www.qemu.org/docs/master/devel/multiple-iothreads.html).
Our [reference command](../../tools/run_fio_baselines.py), reused by the matrix
and soak, selects `aio=threads`, writeback caching and one 128-entry virtqueue,
with EVENT_IDX/indirect/packed disabled. It configures no extra IOThread and no
external block-device process. One virtqueue does **not** imply one outstanding
backend request.

In pinned QEMU v11.1.2, `raw_co_prw` passes the request's iovec to
`raw_thread_pool_submit` → `thread_pool_submit_co`. The worker uses positioned
`pread`/`pwrite` for one buffer, tries `preadv`/`pwritev` for suitable vectors,
and can allocate/copy a bounce buffer for alignment or unsupported-vector paths.
Thus this is asynchronous native host work through a pool, not evidence that
every request is copy-free.
[file-posix.c](https://github.com/qemu/qemu/blob/v11.1.2/block/file-posix.c#L1729-L1795),
[coroutine submission](https://github.com/qemu/qemu/blob/v11.1.2/block/file-posix.c#L2348-L2492).

The virtio-blk frontend drains available requests, constructs external iovecs,
can merge contiguous requests within backend iovec/size limits, submits through
`blk_aio_preadv`/`blk_aio_pwritev`, and publishes completion in callbacks. Reset
drains outstanding block work. This permits overlap without assigning a thread
to each guest request; the actual worker count/concurrency in our measured runs
requires profiling, not inference from queue depth.
[virtio-blk.c](https://github.com/qemu/qemu/blob/v11.1.2/hw/block/virtio-blk.c#L205-L331),
[queue handling/reset](https://github.com/qemu/qemu/blob/v11.1.2/hw/block/virtio-blk.c#L940-L1084).
Separate-process devices are an optional architecture: vhost-user shares queues
with an external backend; QEMU's multiprocess document explicitly describes a
design whose implementation may differ. Neither is enabled in our reference.
[vhost-user](https://www.qemu.org/docs/master/interop/vhost-user.html#introduction),
[multiprocess design](https://www.qemu.org/docs/master/devel/multi-process.html).

**Apple's public queues do not reveal its built-in worker topology.** The SDK's
`VZVirtualMachine.queue` serializes API calls and callbacks; it does not specify
a disk worker count or process model. [SDK VM contract][vz-vm]. The newer custom
Virtio API has a serial `deviceQueue`, with a delegate provider running in the
same process as the `VZVirtualMachine` object. That describes custom-device
callbacks, not the internals of Apple's built-in block backend.
[Custom device](https://developer.apple.com/documentation/virtualization/vzcustomvirtiodevice),
[delegate provider](https://developer.apple.com/documentation/virtualization/vzcustomvirtiodevicedelegateprovider).
Apple introduces this API for macOS27, outside our macOS26 baseline, and its
example dequeues, processes and returns elements from a VZ-owned VM.
[WWDC26/224][wwdc]. Built-in VZ worker/process topology remains unestablished by
this source review; profiling a concrete VZ workload would be separate evidence.

**Consequence for natemu (design inference):** its current device owner and
single native lane already separate guest protocol from native I/O, but admit
one active request and copy through all-observer snapshot/commit windows.
QEMU's request iovecs and memory model cannot be transplanted around natemu's
split-atomic exclusion, code/PTE invalidation and staged-read-failure contract.
Bounded request pipelining or combined capture/completion batches are future
hypotheses for amortizing coordination. They need explicit flush ordering,
generation/reset retirement, bounded memory and cancellation ownership, plus
throughput/tail/CPU evidence. More threads or processes alone do not remove those
obligations. This research changes neither the current repair nor the copy-first
contract, and supplies no new runtime benchmark.

### Resident proxy to VZ-owned devices — assessment only

The proposed path is architecturally plausible:
`x86 virtio frontend → ARM resident virtio driver → Apple VZ block/network device → native backend`.
VZ would own the outer ARM machine, with natemu implementing the x86 machine
inside its resident. This is a different machine ownership boundary, not a
standalone Apple device attached to today's HVF VM. Apple's built-in virtio block
and network configurations already exist on the macOS26 baseline (both SDK
declarations date to macOS11); this proxy idea does **not** require macOS27 custom
Virtio APIs. [Block SDK](</Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZVirtioBlockDeviceConfiguration.h>),
[network SDK](</Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZVirtioNetworkDeviceConfiguration.h>).

**Potential benefit, not established performance:** resident-side request handling
could avoid some current host coordination transitions while reusing Apple's
device/backend implementation. It also adds an ARM driver, second queue protocol
and proxy completion path. Neither the number of copies nor the resulting latency
is established by the public API.

The consequential unanswered compatibility questions are:

- **Boot and execution control.** `VZLinuxBootLoader` documents a Linux kernel,
  and Apple's sample requires an architecture-matching kernel (aarch64 on Apple
  silicon). Neither establishes that the existing bare resident can be booted
  unchanged. Its EL1 entry, shadow mappings, exceptions, IRQ/timer ownership and
  debug facilities need a concrete VZ-compatible route.
  [Linux boot sample](https://developer.apple.com/documentation/virtualization/running-linux-in-a-virtual-machine),
  [boot-loader SDK](</Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZLinuxBootLoader.h>).
- **Memory ordering.** Today's host explicitly writes `ACTLR_EL1 = 2` through
  HVF, and the resident checks that TSO bit at entry. VZ's VM-level configuration
  does not establish an equivalent control contract. Whether the resident can
  obtain and retain the required regime is unresolved, not proof of impossibility.
  [Current setup](../../crates/host/src/vm.rs#L367),
  [resident check](../../crates/runtime/src/main.rs#L57), [VZ VM SDK][vz-vm].
- **Host services and lifetime.** The current host FEX compiler, shared memory,
  HVC request/reply, debug inspection, cancellation and vCPU teardown require
  replacements, not just a new block driver. VZ virtio sockets offer a documented
  host/guest data channel; they do not themselves preserve the existing shared-
  memory or precise exit/cancellation contracts.
  [Socket SDK](</Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZVirtioSocketDevice.h>).

A staged resident-owned DMA pool would be the conservative candidate: snapshot
x86 payloads coherently, let the outer VZ device access only that pool, and commit
incoming data through natemu's observer/code/PTE protocol. Passing x86 guest RAM
directly to Apple does not remove serviced split-atomic exclusion or invalidation
requirements. Queue reset, flush ordering, errors and shutdown must propagate
across both device layers. This remains a source-based assessment; no pivot,
prototype, implementation or new runtime qualification is selected or authorized
by this note.

## Smallest next evaluation

### Zero-copy and the current contract

The [I/O plan](io-plan.md) and accepted
[INV-03-04 claim26](../research/investigations/INV-03-04.md#qualified-handoff-bound-to-backend-and-source-ownership)
explicitly require immutable outgoing snapshots and incoming host-owned storage
before admitted RAM commit. Filesystem/network waits and arbitrary callbacks
cannot run inside a granted RAM window. Claims27–29 additionally depend on staged
read failure leaving guest payload untouched. Thus a backend retaining live
guest payload pointers across I/O is excluded by the selected design. Earlier
references to later zero-copy optimization mean a possible design revision,
not permission to bypass these requirements.

Avoiding redundant copies between owned host stages is compatible. Direct mapped
guest-memory views also avoid an extra register transport, but the selected
snapshot/commit still copies payload bytes. Removing that application copy would
not itself establish that the kernel, filesystem or vmnet performs no copies.

**Architecture does not inherently forbid a revised direct path.** It would need
to account for all affected observers and accesses, including:

- Host readers as well as writers: outgoing reads can observe an intermediate
  state of a serviced split-atomic operation if they bypass the coordinator.
  Incoming writes can bypass source/PTE tracking and stale-translation rejection.
- Explicit host/resident ordering: the selected guest TSO regime does not make
  every native host thread or syscall follow that regime. Data effects and
  tracking must precede status/used-index publication and IRQs. Barriers alone
  do not retire an asynchronous backend operation.
- Legal unaligned/split buffers: byte access is possible, but ordinary Arm
  unaligned execution is not an x86 atomicity proof. Avoid overaccess and invalid
  Rust references; keep metadata atomics aligned. DMA need not make an entire
  payload atomic, but cannot violate the selected observer/CPU contracts.
- All aliases and mapping lifetimes: backing pins prevent reclamation, not
  guest mutation or promotion into code/page-table use. Any protection/lease
  scheme must account for guest and host protection granularities and every
  access path, including host aliases.
- Partial failure and cancellation: direct incoming I/O can alter guest RAM
  before returning an error. That invalidates the current staged-read error
  policy. Reset/timeout cannot revoke a pointer still used by a syscall or callback.
- Memory class: the selected coherent ordinary-RAM backing does not authorize
  raw MMIO/non-WB operations or physical passthrough/noncoherent DMA. Those are
  separate contracts; neither pinning nor a memory trait supplies them.

Potential alternatives are a narrowly bounded direct operation with a proven
access/lifetime contract, or a concurrent range-ownership scheme with explicit
coordination. Neither is selected. Holding the existing all-observer window
across a potentially blocking syscall is not an acceptable shortcut. Protocol
buffer ownership may help a correct driver, but is not enforcement of arbitrary
guest behavior; a revised device need not promise an immutable payload snapshot
if it defines a different correct concurrent-access contract. No new performance
or correctness evidence was collected for these alternatives.

### Proposed page-gated ownership — November 8 assessment

The owner's mutex/rwlock plus page-table proposal is a plausible alternative to
staging for selected buffers, not an adopted design. TSO does not supply mutual
exclusion, whole-transfer atomicity or storage lifetime. Current vCPU setup enables
TSO; this pass does not establish a host-thread TSO setting, its kernel-I/O scope
or any need to rely on one. Native host atomicity is operation-specific too:
an atomic CAS and a bulk copy have different guarantees.

Linux distinguishes coherent control memory, which can be accessed concurrently
under a protocol, from streaming payload ownership/synchronization. Coherence
still requires barriers. Virtio publishes descriptor ownership through ring
state; CPU virtual-page permissions are not automatically revoked when a buffer
is offered. Correct drivers obey the protocol, but the emulator must retain
containment and valid memory access under guest violations.
[Linux DMA guide][dma-guide], [DMA interface][dma-api], [Virtio][virtio].

A candidate ownership sequence is guest-owned → closing/draining → device access
→ completing → guest-owned. A genuinely read-only backend could share payload
reads while excluding all writers; an incoming writer could require exclusive
payload access. Rings/control data need their separate shared-memory protocol.
The lock protects the ownership records. Shadow permissions plus acknowledged
TLB invalidation enforce restrictions on native EL0 accesses; refill checks must
prevent a new mapping from evading the lease. Use emulator-owned shadows, not
changes to the guest OS's page tables. Permission faults caused by a lease are
internal waits/retries, not fabricated guest page faults.

There is a current implementation precedent:
[split-page exclusion](../../crates/runtime/src/cache.rs#L1350) revokes peer
shadows and gates refills around serviced split atomics. It is a short,
vCPU-owned, two-page operation, not an asynchronous reader/writer lease manager.
Resident accesses, walkers/A-D updates, compiler source reads and host/kernel
access lifetimes would all need to participate. Prevent code/PTE promotion and
conflicting aliases during a lease; handle failure/reset before releasing it.

Page granularity can stall valid accesses to unrelated bytes sharing a page.
In particular, a blocked completion ring, guest lock or page-table page can make
progress depend on the very I/O completion that needs it. Keep completion and
fault-service progress independent of leased payload pages; determine a fallback
before dispatching an operation whose pointers cannot safely be revoked. A
future evaluation should start with eligible ordinary payload pages and measure
lease/shootdown/fault costs against copying, not assume the lock wins.

### Split-lock reuse, eligibility and expected cost — November 8 assessment

Reuse the revoke/acknowledge/refill-gate mechanism, not the current split lock's
whole lifetime. `split_begin` has one vCPU owner, two pages and exclusive access;
its waits serve short shadow-reset transactions. I/O needs independently owned,
bounded leases with request/VM generations, backend retirement and waits that
permit completion/reset progress. TX could share readers if the backend truly
only reads; RX needs exclusive ownership under the proposed policy. No existing
function can simply be held across an arbitrary disk/network operation.

Start any candidate direct path with ordinary payload pages whose complete
backing spans, backend constraints, alias set and overlap with active requests
are validated. Exclude queue/status/control, tracked code/table and other
progress-sensitive overlaps initially. Full-page payload coverage is a useful
conservative initial eligibility rule, not a Virtio requirement. A submission-time
classification is insufficient: conflicting refills, helper/walker accesses,
source capture and future promotion to code/page-table use must obey the lease
until it ends. Existing mappings must be revoked and acknowledged first.

Choose staging before granting a direct pointer when any predicate fails.
After direct I/O starts, neither a fault nor a timeout permits replay through
staging or immediate backing reuse: actual backend access must retire and partial
effects need their recorded outcome. The staged-read no-payload-change-on-error
policy would need revision for direct RX. Keep small queue/header bookkeeping
under its checked protocol even when payload copying is avoided.

**Cost hypothesis:** current `split_begin` requests a reset from every peer marked
as holding shadows, and that reset removes **all** of that peer's shadows in both
CPL spaces. It does not target only the leased pages. Costs therefore include
kicks/acknowledgements, TLB maintenance, unrelated working-set refaults, lease
bookkeeping and guest stalls during the backend lifetime. Extending the lease
alone does not make this cheap. Existing per-page writable tracking also does
not supply the reader inventory required for RX exclusion.

Compare complete paths: staging already requires admitted snapshots/commits and
both paths need metadata/completion ordering. A useful differential model is:

`direct − staged = extra coordination + extra contention/refills + other path differences − eliminated copy cost`

This must be evaluated separately for CPU consumption, throughput and tail latency;
overlapping costs cannot simply be summed as wall time. Expected direction:
per-packet MTU1500 and small4KiB requests are poor initial candidates for broad
shadow resets; larger64–128KiB requests/batches on uncontended pages are more
plausible. More vCPUs, fragmented ranges and hot shared pages work against direct
access. Targeted invalidation and amortized lease admission could change that
tradeoff. These are qualitative hypotheses, not measured thresholds.

Linux's [MSG_ZEROCOPY documentation][zerocopy] independently illustrates the
general exchange of per-byte copying for page accounting and completion overhead.
Its stated size threshold applies to that Linux implementation, not natemu,
Darwin or vmnet. Likewise, eliminating a natemu copy makes no guarantee about
copies internal to the host backend.

The next useful evidence would compare staged and leased paths with identical
backend behavior, transfer sizes/batches, vCPU counts and buffer reuse patterns;
count copies, acknowledgements, shadow refaults and blocked guest time alongside
throughput/CPU/tail latency. No such experiment was assigned or run here.
Independent `/root/io_scope_review` checked current code and these reuse/cost
premises; no implementation or design adoption follows from this assessment.

### Alignment is a layered contract

Split-ring metadata has required alignment (descriptor table 16 bytes, available
ring 2, used ring 4). This does not impose sector/page alignment on ordinary
payload addresses. For the selected 512-byte logical block profile, IN/OUT disk
offsets are `sector * 512` and the total data length is a multiple of 512;
individual scatter/gather fragments need not each start at a 512-byte-aligned
guest address. The 4096-byte segment maximum is a length limit, not an address
alignment requirement. [Virtio 1.2 §§2.7 and 5.2.6][virtio]

For example, a 512-byte disk read into guest address `0x1fff` crosses a guest
page boundary but is not inherently invalid. The memory adapter must validate
the entire span, reject holes/private ranges/overflow, and copy across valid
backing fragments. Use byte/slice access for payload and decode copied fields;
do not create misaligned Rust references or assume an atomic API accepts an
unaligned pointer. Ring-index atomics retain their alignment requirements.

Owned staging separates guest-buffer alignment from host I/O constraints. The
ordinary cached-file backend does not require 4-KiB-aligned guest buffers, and
vmnet's inspected iovec contract adds no page-alignment requirement. If a later
native fast path needs stricter alignment, use suitable aligned staging or a
valid fallback; do not reject a guest request merely because it misses that
fast path. Scatter/gather and header offload do not grant arbitrary guest pointers
native-backend lifetime or coherence authority.

An unaligned DMA payload is not a translated x86 instruction and does not acquire
whole-transfer atomicity from `memcpy`. Commit through the existing resident
protocol so that split-atomic handling, code/PTE tracking and completion ordering
remain correct. These are design obligations, not newly tested behavior. Add
unaligned payload/header, page/backing-boundary, SG-fragment and overlapping
coherence cases to the eventual integration matrix; no extra experiment ran here.

### Evaluation order

1. Preserve the queue/memory integration slice: deterministic host backend,
   admitted snapshot/commit, cancellation, completion ordering and GSI lifecycle.
2. Source-check the Rust vmnet wrapper against required raw APIs; probe interface
   admission, returned limits and teardown before selecting packaging.
3. Validate bounded plain-frame batches plus DHCP/DNS/TCP/UDP/ICMP and the explicitly
   selected IPv6/port-forwarding behavior. Keep failures and unsupported behavior visible.
4. Exercise virtio-header mode with both start paths and targeted checksum/GSO/
   fragmentation/large-RX cases. Only then add matching advertised guest features.
5. Compare the resulting native backend with the required behavior and measured
   cost of the libslirp candidate. No experiment or performance comparison was run here.

## Source review

`/root/rust_io_research` authored this note; root inspected it against the pinned
memory trait and installed SDK. Independent `/root/io_scope_review` separately
checked the memory-adapter, native-offload and VZ-boundary premises. Its key
qualifications—mutually exclusive checksum/header modes, fragmented-packet
limits, large-frame requirements and unmeasured performance—are incorporated.
This is assessment only; the current no-offload milestone and backend selection
remain unchanged.

The November 8 I/O-thread comparison and resident-proxy assessment were authored separately by
`/root/io_scope_review`; root checked its pinned QEMU request/submission/reset
paths, actual benchmark arguments and Apple's queue/provider contracts against
the linked primary sources. It supplies design context, not measured worker
placement or an adopted concurrency change.

[memory]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-memory/src/guest_memory.rs
[queue]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-queue/src/queue.rs
[hv]: /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Hypervisor.framework/Headers/hv_vm.h
[vmnet]: https://developer.apple.com/documentation/vmnet
[vmnet-h]: /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/vmnet.framework/Headers/vmnet.h
[vz-vm]: /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZVirtualMachine.h
[vz-config]: /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZVirtualMachineConfiguration.h
[vz-fd]: /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZFileHandleNetworkDeviceAttachment.h
[vz-vmnet]: /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZVmnetNetworkDeviceAttachment.h
[vz-disk]: /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Virtualization.framework/Headers/VZDiskImageStorageDeviceAttachment.h
[fcntl]: /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/include/sys/fcntl.h
[wwdc]: https://developer.apple.com/videos/play/wwdc2026/224/
[entitlement]: https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.vm.networking
[new-entitlement]: https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.networking.vmnet
[virtio]: https://docs.oasis-open.org/virtio/virtio/v1.2/virtio-v1.2.html
[dma-guide]: https://docs.kernel.org/core-api/dma-api-howto.html
[dma-api]: https://docs.kernel.org/core-api/dma-api.html
[zerocopy]: https://docs.kernel.org/networking/msg_zerocopy.html
