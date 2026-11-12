# Copy-first virtio integration plan

November 8, 2026. Consolidated from the owner's I/O discussion after early
performance/correctness work. **Implementation authorized November 8:** the owner
provided the sibling rust-vmm checkout and asked to begin, including local edits
there. Natemu uses `codex/virtio-io`; the rust-vmm patch lived on the
`natemu` branch of the owner's fork as a pinned Cargo git dependency until November 12, when
natemu's own checked walk replaced it (owner request). The owner
selected persistent disk plus basic networking. Both are in this milestone.
The [implementation plan](../implementation.md) owns overall build order;
[INV-03-01](../research/investigations/INV-03-01.md#current-direction--build-first-october-25-2026)
owns current results and the next assignment. This is the concrete I6 plan.

## Behavior and scope

The target is an integrated device capability usable by ordinary guest software,
not a collection of passing fixtures. Tests probe the implementation; they do not
define special execution paths. No guest behavior, runtime fast path or
correctness relaxation may depend on benchmark identities, fixed guest addresses,
known expected outputs or fixture-only signals. Optimizations target general
instruction/access patterns and checked conditions available to ordinary workloads.
Existing workload tools and realistic combined guest work supply performance
evidence; synthetic delayed/error cases supply fault coverage. Neither replaces
the other. Keep less favorable workloads/results and all failed attempts visible.
The [benchmark record](io-benchmarks.md) began as a shortlist of fio, iperf3,
netperf and the existing zstd build, with controlled downloads and persistence
checks. The matrix was not authorized when this plan was first written; it was
authorized later and has been run, and that file now holds the measured results.

An unmodified stock Linux kernel discovers modern virtio-MMIO devices through
ACPI LNRO0005, boots an installed ext4 root on an owned raw image, writes files,
powers off cleanly, and reads those files after a fresh launch. Basic networking
means one Ethernet interface, shared/NAT connectivity, DHCP, DNS, IPv4 TCP/UDP
and an HTTPS/package-download check. Preserve serial, timers, APIC, ACPI poweroff,
configured CPU/RAM capacity and high-RAM DMA. Bring up at N=1, then verify at
N=2/4 and selected larger N within actual HVF admission.

Networking is IO4, after the common completion path and disk. Its native-backend
admission check starts earlier so entitlement/deployment trouble is discovered
before the packet device is built. Virtio-rng is the small first real consumer.
IPv6 and fuller ICMP coverage remain required follow-on work under
[03-73](../research/investigations/INV-03-03.md#combined-required-device-choice-claim73);
a successful IPv4 download does not establish the full network contract.

Initial devices retain the accepted map: block at 0xf0000000/GSI16, net at
0xf0001000/GSI17, rng at 0xf0002000/GSI18; modern MMIO, split rings, one block
queue (maximum 128 entries), one RX/TX pair (maximum 256 entries each),
eight-entry rng queue, MTU1500 and no network offloads.
Generate discovery and device configuration from the same launch manifest.
Keep the exact accepted device feature/command tables; indirect descriptors,
packed rings, EVENT_IDX, multiqueue, hotplug and migration stay unoffered unless
explicitly added with their complete handling. The common VERSION_1 feature is
required. Block offers SIZE_MAX, SEG_MAX, BLK_SIZE and FLUSH (SIZE_MAX/SEG_MAX
must be accepted); network offers MAC, MTU and STATUS; rng has no device-specific
features. Use 512-byte block sectors, reject non-sector-multiple backing sizes,
and retain the complete unoffered-option lists in 03-56/57 and 04-28. IO0 freezes
these explicit masks in code; upstream defaults do not choose them.
Bridging, port forwarding, qcow2, automatic reboot and zero-copy are
outside this first implementation. Guest reboot is defined behaviour, not
supported: it ends the run (November 11). The FADT reset register and the
keyboard controller's reset pulse (FEH to port 64H) end it with the stop
`Power(2)` (exit 2) after the same disk drain and final full sync as S5; a triple
fault (Linux `reboot=t`) ends it with the resident stop SHUTDOWN, without a drain.

## Discussion notes and decisions

- (Superseded November 12: rust-vmm dropped; natemu walks descriptor chains itself.)
  Use rust-vmm selectively for virtio queue machinery. Broader onboarding remains
  an assessment, not a migration. The [isolated evaluation](rust-vmm-reuse.md)
  pinned `671e367124cf7717d832b21de86b62eccf33ac27`; virtio-queue 0.18.0,
  virtio-device 0.1.0 and vm-memory 0.18.0 built on Darwin and 16 synthetic cases
  were independently reproduced. This does not establish integrated correctness.
- Consume vm-memory, not a reimplementation of it. Keep natemu's RAM allocator
  and authority. An address/view trait supplies neither exclusion nor lifetime.
  Host backing access bypasses guest virtual paging; DMA uses checked guest
  physical ranges and does not synthesize guest PTE A/D updates.
- Copy payloads into owned staging before outgoing backend work; incoming backend
  work writes only owned staging. Direct host access during a short admitted copy
  is compatible with this plan; retaining guest pointers across I/O is not.
- TSO, volatile access and a host mutex do not establish whole-buffer atomicity or
  make resident software atomics atomic to a nonparticipating host observer.
  Naturally aligned native atomics differ from the crossing-16 fallback. See the
  [instruction census](io-atomic-access-census.md) for exact forms and caveats.
- Byte copies are valid for excluded/stable buffer transfers, but can weaken a
  guest scalar instruction's required indivisibility. Segment m16 resident
  helpers are a source-confirmed discrepancy candidate, with no reproduced tear
  or normal-I/O overlap. The owner suggested deferral; this plan does not schedule
  an ISA fix or claim formal acceptance of reduced x86 semantics. It is separate
  from the copy-first I/O milestone. Current normal STMXCSR/VSTMXCSR stores are
  FEX-generated; their legacy resident handlers are not evidence of normal use.
- Keep queue metadata/control synchronization correct in all later experiments.
  The discussed sequence is copy-first → optional experimental direct payload
  access relaxing atomicity → coordinated direct payload access → optimization.
  These later experiments are contingent on measured benefit and a new access
  contract; they are not enabled by this plan.

## Small interfaces and ownership

| Module | Responsibility and interface |
| --- | --- |
| Existing MMIO device registry plus virtio transport | Checked register reads/writes, immutable negotiated profile, queue configuration, status and IRQ state. QueueNotify queues bounded work and returns promptly. |
| Per-VM device owner | Serializes queue indices, request IDs, completion/reset disposition and interrupt status. Owns validated request metadata and buffers. Receives typed backend results; never lets callbacks write guest RAM. |
| Resident memory coordinator plus trusted host leaf copier | One finite, exclusive snapshot/commit window per VM; exact spans, pause epoch, backing identity, direction and commit result. Owns guest-memory/coherence authority. |
| Block/rng/packet backends | Consume or fill owned buffers and return typed IDs, lengths and outcomes. Know no guest pointers, live descriptors or guest interrupt authority. |

Use the current Device/MMIO, GSI, loader and cancellation machinery, extending
the immediate Device result for deferred lifecycle accesses as described below.
"Owner" means serialized state ownership: ordinary register reads and ACK may
use a brief shared register-state lock rather than synchronous worker RPC.
Avoid a new
async runtime or generic backend framework: the deterministic test backend and
real backends are sufficient variation. Native callbacks enqueue results and
return. Guest physical addresses stay u64 until checked conversion; validate
addition/multiplication, holes and private-memory exclusions before native access.
No Rust exclusive references cover concurrently accessible guest RAM.

### Rust-vmm fit and transport repairs

(Historical: since November 12 the checked traversal is natemu's own, `virtio/queue.rs`.)

Reuse ring/descriptor definitions and checked traversal against owned metadata
snapshots. Patch or extend traversal in one place to distinguish valid end from
bad-next/cycle/truncation; do not maintain two independent parsers. Feature and
direction/length checks remain the device's responsibility. Keep a small explicit
natemu MMIO implementation unless fixing the upstream transport demonstrably
requires less code. The evaluated transport cannot be used unchanged:

- Validate full-width selectors, queue sizes, alignment, resources and status
  transitions before narrowing or activation; enforce required VERSION_1 and
  the selected block feature subset.
- Implement QueueReady=0 stop/readback and full reset, QueueNotify scheduling,
  notification suppression and IRQ ACK/level recomputation. Optional RING_RESET
  is not a substitute for the required lifecycle.
- Avoid live lazy descriptor iterators across asynchronous work. Avoid upstream
  add_used's partial-failure/index-advance behavior on live guest RAM: prepare
  bounded completion writes first, then publish owner indices exactly once after
  a successful coordinated commit. Reuse primitives, not incompatible ownership.

Pin the resolved production packages/features and license inventory in IO0;
record the change to the existing host dependency policy then. The harness's mmap
feature is not a requirement to replace production RAM with GuestMemoryMmap.

**Fork assessment (November 8):** the recommended reuse route likely needs one small additive
virtio-queue patch: checked chain traversal that distinguishes malformed termination from normal
end, and rejects unnegotiated indirect tables before following them, plus a checked public
snapshot/head entry point if needed (`DescriptorChain::new` is currently crate-private).
Existing public descriptor accessors allow a local direct walker instead, so a fork is not
architecturally required. Prefer extending the existing traversal implementation over
duplicating it. The proposed fork target is the current rust-vmm/rust-vmm monorepo, with changes
isolated to the selected crate and pinned by commit; vm-memory needs no currently identified
patch. Natemu's memory/coherence coordinator, backend ownership and deferred MMIO/reset remain
local code, not extensions to rust-vmm's role. See the [fork
scope](rust-vmm-reuse.md#production-fork-assessment--november-8).

## Copy-first request and completion protocol

The selected baseline follows
[04-26–29](../research/investigations/INV-03-04.md#qualified-handoff-bound-to-backend-and-source-ownership).
It needs new pause/acknowledgement/release integration with today's runtime; `guest_ram_written`
and a host-visible stopped PC are insufficient.

1. **Reserve.** Before closing memory admission, reserve bounded request slots,
   metadata scratch, payload storage, result capacity and invalidation bookkeeping.
   Use VM/device/queue generations plus a request-slot incarnation. Backpressure
   leaves work pending; it never creates unbounded allocations or drops a block
   request as though it succeeded.
2. **Capture metadata.** Freeze queue configuration/incarnation for the request.
   At an acknowledged safe memory boundary, snapshot the
   bounded direct descriptor table and available-ring state into owned scratch
   using the queue configuration's checked ranges. No indirect chains initially.
   Include every slot the captured available index can name, validate index
   progress, and recheck incarnation before accepting work. Release the window,
   then perform checked traversal on the snapshot; parsing cannot read uncaptured
   guest memory. Reject indirect descriptors before following their addresses.
   The accepted immutable description is authoritative for that request; never
   follow guest mutations through a retained iterator. Reserve before consuming
   next_avail, and consume an accepted head once under the device owner.
   The owner may reuse this bounded metadata snapshot for its already-published
   prefix until the captured available index is exhausted. Entropy and network take
   one head from it at a time; block admits every head its in-flight bounds allow
   from one snapshot ([stage 2](#concurrent-block-requests-and-batched-windows-proposed-november-9)).
   Each head still undergoes complete validation, and reset/S5 wait for every
   admitted request of the old incarnation. Queue/device generation changes,
   switching queues, Empty/Canceled/Invalid dispositions and prefix exhaustion
   force a fresh capture. Geometry is immutable while ready/active; disable/reset
   advances the identity. A completion aliasing later metadata does not rewrite
   the owned captured interpretation. Payload is always captured afresh, and
   completion suppression flags are read afresh in each commit. Newly published
   heads are discovered at exhaustion without requiring another kick for block/TX.
   This reuse is an inference from the exposed-entry ownership requirement in
   [Virtio1.2 §3.3.1](https://docs.oasis-open.org/virtio/virtio/v1.2/virtio-v1.2.html),
   not a claim that the specification prescribes a particular hostile alias outcome.
3. **Prepare payload.** Validate the complete captured readable/writable SG stream
   and capture readable bytes, including block/network request headers, into
   owned storage under a separate admitted window. Decode command, sector and
   header fields outside exclusion; support legal headers split or combined
   across descriptors. Validate commands, capacities, arithmetic and status
   destinations before any external effect. For IN/RX/rng,
   backend data arrives into owned storage. Descriptor capture and payload capture
   are distinct epochs: there is no claim that the entire request was sampled at
   one instant. A conforming driver retains descriptor/payload ownership as
   required; violations must remain bounded and cannot redirect native pointers.
4. **Run backend.** Release RAM authority before disk/network/entropy calls.
   Backend results retain their buffers and typed identity. A completion callback
   cannot wait for a grant while holding a device/backend mutex.
5. **Prepare completion.** Outside exclusion, compute ordered payload/status/used
   writes and notification-state reads using captured queue/request identity.
   Recheck incarnation and reserve the complete commit. Resolve/pin all applicable
   guest backing spans before the first write; invalid destinations cause no
   partial guest-memory commit. Invalid chains with no safely reportable status
   fail the device under a defined transport policy; do not complete a valid prefix.
6. **Commit and publish.** In one finite window, copy successful incoming bytes in
   descriptor order, write device status, then used entry and used index with the
   required ordering. Preserve ordered overlapping writes; deduplicate only the
   invalidation union, not writes. Complete resident code/shadow/dependency work
   before guest reentry or an actionable IRQ. Publish owner indices once and
   retire the request only after all backend/copier/callback references end.

Initially one maximum block data unit is 128KiB (32 data segments of at most
4KiB), plus the 16-byte request header and bounded metadata/status/ring spans.
Start with one block request, one rng request (256-byte payload maximum), and
one TX plus one RX packet (2048-byte slot each) in flight, retaining queue-size
bounded metadata. These preserve the selected simple network/rng resource
profile and backpressure block work without narrowing its advertised request
size. Do not fragment a block
payload snapshot into unrelated epochs and call it one coherent unit. Start with
one request per RAM window; add bounded packet/request batches after measuring
pause time. *Amended November 9 (stage 2):* block now keeps up to min(queue size,
32) requests and 4 MiB of owned buffers in flight, and a window carries whole
requests up to 1 MiB and 1160 spans; each request's payload is still captured in
one window. Entropy and network keep the starting bounds. Queue capacity is not permission to allocate a maximum payload for
every possible descriptor without a configured global in-flight byte budget.
IO0 must freeze numeric request/extent/byte limits for every device and queue.

### What makes a RAM window safe

The coordinator closes admission and obtains matching epoch acknowledgements
or an ordered resident HLT-quiescence promise from every possible guest-memory
observer/writer: translated execution, resident
helpers/walkers, split-atomic service, source capture and publication, and other
host memory access. Quiescence includes native barriers and a safe continuation;
being in a host Device callback can still retain a live compiled-block return PC.
Complete the owed ordinary MMIO operation/recovery before parking. For a deferred
control operation, establish its explicit no-guest-memory pending state and
retained continuation instead. Do not invalidate/reclaim code then resume through
a stale continuation or replay an already accepted MMIO effect.

Reuse precise checkpoints, management doorbells and resident invalidation where
applicable, but do not reuse split_begin as an asynchronous I/O lock. Halting,
host translation waits and resident service must all acknowledge or defer safely.
The first implementation waits for existing host compilation/replies to finish
and the resident to reach a safe checkpoint before acknowledging. No compiler
result survives the granted window, including uncached step/system decodes.
Allowing compilation to continue over immutable source during a grant is a later
optimization requiring source-copy admission and stale-result rejection.
Permanent control capacity permits drain/reset
progress independently of guest IF, guest locks and backend completion queues.
For block requests, the granted completion copies read avail.flags and avail.idx
only after all payload/status/used writes. Owner retirement OR-preserves any
QueueNotify arriving after admission and any same-generation captured successor,
then uses the observed index to avoid a redundant empty scan. A false observed
pending bit must never overwrite a later notification or revoke owned work.
The normal ready/status/reset/quiesce gates still precede the next admission.
Notification progress assumes conforming ownership of device-written queue state;
arbitrary payload aliases to used.flags do not acquire a liveness guarantee.
HLT and AP-mailbox wait loops drain management doorbells **before** checking the
I/O request, then may sleep. Checking first can consume a newly arrived request's
only wake and sleep without acknowledging it. A kick drained before the check has
already published its request; a kick arriving after the drain remains pending
to wake the wait. Keep the checkpoint before any subsequent guest-memory access.
After publishing a window request and a native DSB SY, an exact current-epoch
acknowledgement or a resident HLT-quiescence promise permits omitting that
participant's doorbell. The resident completes outstanding guest accesses before
publishing the promise, and clears it with an ordered barrier before checking
requests on wake. This handshake ensures either the host observes withdrawal or
the resident observes the request before accessing guest RAM. Still check every
participant's terminal state under the stop mutex, and require every participant
to be acknowledged or quiet before grant. A previous epoch's acknowledgement
alone never permits skipping the wake or granting a new window.

Only the HLT sleep publishes this promise. AP-mailbox waits continue taking
ordinary acknowledgements because a DMA write can change their watched mailbox
without a guest interrupt. A quiet CPU may skip multiple windows, so on wake it
must drain code invalidations and compare the latest table-change epoch against
its previously serviced epoch, including when a table write was followed by a
read-only or canceled window. Quiet flags never synthesize acknowledgements.

Only a bounded exact-span leaf copy and resident commit run while drained: no
allocation, descriptor parsing, external syscall, arbitrary callback or compiler
wait. Apply the qualified host compiler boundaries/DSB SY and resident
acknowledge/resume barriers from 04-26; an upstream Release index store alone is
not the cross-domain handoff. All payload, status and ring writes participate in
code/source/shadow tracking, including writes to guest page-table backing. DMA
is not a guest virtual store; do not walk guest VA permissions or invent A/D bits.

Lock/progress rule: MMIO takes only its brief register-state lock, posts intent,
then returns. The device owner requests a window without holding locks that a
participant needs to acknowledge it. The copier cannot call the device or backend.
Completion/reset/ACK are serialized by the owner, with interrupt level recomputed
from current status. Serialize publication to the GSI too: computing a level
under a lock then issuing unordered stale updates after unlock is insufficient.
Keep lock order free of a chipset-to-device reverse acquisition. Never wait for
the backend or all-vCPU drain under that lock.
Register async work before a halted-idle decision so a pending completion keeps
the VM alive and wakes the appropriate owner/vCPU without a lost notification.

## Native backends and features

**Recommended networking route: vmnet directly, in a separate per-VM packet
worker, subject to the admission gate below.** Retain HVF and natemu's machine;
Virtualization.framework device attachments require a VZ-owned VM and do not
replace our queue/coherence work. The current accepted historical direction is
adapted libslirp (03-73); choosing vmnet is a proposed backend revision, not an
already validated production selection or silent fallback. The vmnet admission
decision is still pending. The Apple-framework, vmnet capability, storage,
threading and zero-copy assessments are owned by the
[native-backend assessment](io-native-backends.md); this section keeps only the
plan-level decisions.

| Backend | First implementation | Measured extensions |
| --- | --- | --- |
| Raw disk | Supervisor opens one owned regular file; fixed capacity and stable ID. A fixed pool of four workers on the shared descriptor uses native positioned I/O on owned buffers, asynchronously relative to vCPUs and in any order (stage 2; first built as one ordered lane). Preserve IN/OUT/FLUSH/GET_ID and 04-28's complete error profile. | Buffer reuse first; vectored I/O or cache/read-ahead/preallocation policy only when justified. A flush needs no ordering against other requests (it covers completed writes, §5.2.6). |
| vmnet shared/NAT | Native network services, dispatch notifications and scatter/gather over owned storage. Honor returned counts and limits. No virtio checksum/GSO features initially. | After the one-TX/one-RX baseline, explicitly increase bounded slot budgets and use native packet batches. Negotiate checksum/GSO and vmnet virtio-header mode together after header/size/fallback tests; increase RX/storage limits before enabling large TSO packets. |
| Entropy | Native entropy acquisition into initialized owned storage outside the window; commit through the common path. | Bounded batching if useful; no fabricated successful bytes on acquisition error. |

Plan-level network decisions: native does not mean zero-copy or physical-NIC
offload; the initial profile is plain frames, 2048-byte guest slots and MTU1500
(03-57), with no native segmentation/checksum mode the guest path cannot
interpret; oversize frames are dropped or rejected under a recorded bounded
policy, never overrunning guest slots. The header-stripping and receive-header
rules and the offload gates are in the
[packet contract](io-native-backends.md#packet-contract-and-admission-gate-for-vmnet).

**Native admission gate (before IO4 external traffic):** establish an actually
signed/entitled launch on the supported macOS version, sandbox/service access,
creation/read/write/stop callbacks and packet buffer/count limits. No automatic
root escalation or broader worker rights; the packet worker holds no guest RAM,
disk FD or task-memory authority, and the trusted device owner has no general
network-service authority. If admission fails, report the blocker and revisit
the documented backend choice. Entitlement and role-manifest details are in the
[admission section](io-native-backends.md#packet-contract-and-admission-gate-for-vmnet).

Disk durability is part of IO3, not a later optimization. Negotiated FLUSH allows
writeback; a successful FLUSH follows earlier completed writes and a successful
fsync(2). If FLUSH is declined, each successful OUT waits for fsync. The S5
drain ends with F_FULLFSYNC, so a clean shutdown is fully durable. Owner
decision (November 10), as QEMU: fsync rather than F_FULLFSYNC per guest flush
(about 0.02 ms against 4 ms on the host; F_FULLFSYNC made fsync-heavy guests
15-60x slower than QEMU). Trade-off: writes acknowledged to the guest survive a
natemu crash but can be lost on a host power loss or macOS crash before the
drive writes its cache.
Short positioned I/O advances only its completed prefix; zero progress or an
error, including EINTR, reports IOERR without replaying the issued request. Failed staged IN
copies no payload, reports IOERR and the selected conservative used length
(04-27's len=0 rule); verify the actual target Linux driver's status consumption.
Failed OUT may have changed the image: report failure, never promise rollback.
A FLUSH after a failed OUT (a synchronous pwrite error, already reported IOERR)
still succeeds: nothing completed is left unsynced (a flush covers completed
writes). A failed host sync is sticky for the launch (decision November 11, as
QEMU's file-posix `page_cache_inconsistent`): after any fsync or F_FULLFSYNC
error, every later FLUSH, write-through OUT and the S5 final sync fail without a
call, since the host may have dropped dirty pages of writes already reported
complete. A device reset does not clear it; only a new launch does.
No physical power-loss proof is claimed by successful full-sync tests.

## Reset, shutdown and failure

The original immediate `Device::read/write` interface cannot by itself express a control access
waiting for retirement. The RNG continuation extends the host device-service result to
distinguish completed, deferred and refused-with-no-effect. Ordinary register
accesses/QueueNotify remain prompt. Queue-disable/full-reset may close admission and return a
pending token to the host service loop, which releases every device lock and retains the access
identity/result/recovery state until retirement. The affected vCPU stays in a progress-capable
control wait; it must participate in coordinator drain without executing guest code or touching
guest RAM. It must still service pause/shadow/source control, or enter a new resident safe-park
state that discharges those holdings. Retaining code storage alone is insufficient: completion
must finish the owed instruction once and dispatch at a safe boundary rather than resume a block
tail made stale by DMA. IO0 must map that pending state and its safe completion/reentry to
actual resident recovery and source lifetime. The RNG continuation implements this safe wait;
the host `in_host` flag is not an acknowledgement.

Complete the deferred access exactly once. QueueReady readback returns the last written value
and synchronizes stop; it is not a synthetic busy register that returns 1 after a completed
write of 0. Serialize a conflicting read or reconfiguration behind the pending stop/reset; never
allow it to reuse live resources. Full status reset returns the reset state only after its
required retirement. [Virtio 1.2 MMIO register
rules](https://docs.oasis-open.org/virtio/virtio/v1.2/virtio-v1.2.html) and the actual Linux
driver's reset/deletion sequence are IO1 compatibility checks. Do not report refusal after
having applied a control effect.

Close queue/device admission, retain old incarnations and let already granted
commits finish their recorded disposition. Ungranted old completions cannot write
RAM or publish IRQs. Cancel their pending grant requests before waiting for
retirement; reset cannot wait for a completion grant that its own suspended vCPU
prevents. No RAM grant waits on a backend needed by reset. Generation mismatch
suppresses stale publication, but does
not cancel an in-flight syscall or release its buffer. Process backend results
while draining; never block the sole owner required to retire them. QueueReady
and device-status readback must reflect the required stop/reset semantics before
resources are reused. Do not assume old callbacks are gone because a reset was
requested.

Route S5 through a new quiesce/drain phase before the current immediate
cancellation/end path. Existing IRQ detachment and VM destruction are teardown
plumbing, not an implemented device drain. S5 stops new work, drains accepted
disk work, performs the required durability
barrier, retires callbacks/IRQ offers, stops workers and only then releases RAM,
FDs and VM state. vmnet_stop_interface returning success means its completion
handler was scheduled, not that all callbacks/buffers are retired; wait for stop
completion and drain application callback references. A stuck syscall/worker is
contained failure, not successful drain. Management termination remains possible
without freeing live storage or waiting indefinitely in an unkillable owner
thread; IO0 must map the disk workers to the per-VM process lifetime/termination
mechanism. Unexpected partial RAM-copy failure keeps admission closed and fails
the VM; do not retry the whole commit or resume with guessed coherence state.

## Runnable slices and acceptance

| Slice | Result and decisive checks |
| --- | --- |
| IO0 — Freeze integration contract | Existing Rust fit harness is complete. Select checked traversal/local transport approach, exact feature masks and budgets; map every pause/commit participant and reset/termination path to current code; review versioned ABI fields. Run native network admission separately when implementation is authorized. Stop at a reviewed implementable contract with explicit platform blockers. |
| IO1 — Deterministic asynchronous completion | Add bounded owner/coordinator and RAM windows with delayed/error backend. Prove no syscall holds a grant, no callback retains guest pointers, and the expected effect/IRQ occurs once at N=1/2/4. |
| IO2 — Entropy and discovery | Reuse IO1 for rng; generate ACPI from device configuration; stock Linux discovers all enabled resources and reads entropy. Test short/error/reset and idle wakeup. |
| IO3 — Persistent root disk | Native ordered lane, exact block profile, full-sync and shutdown drain. Boot an actual installed image with admitted kernel/initramfs drivers; filesystem workload, shutdown/relaunch, content hashes and injected short/error/flush failures. |
| IO4 — Basic network | Integrate admitted vmnet worker and virtio-net RX/TX; MTU1500/no offloads. Verify DHCP, DNS, TCP/UDP/download, packet loss/backpressure, returned counts/errors, reset and callback retirement. Partial batch counts are additional checks when increasing the slot budget for batching. Network admission is a prerequisite, disk work need not wait for it. |
| IO5 — Combined acceptance | SMP disk+network+CPU workload, high RAM, clean shutdown/relaunch and data verification. Independently review/reproduce bounded acceptance; retain applicable standing regressions and compare matched QEMU workloads. |

IO1's meaningful adversarial matrix covers malformed/cyclic/unnegotiated chains,
range overflow/holes, index wrap, queue reconfiguration/reset, duplicate and late
results, absent RX buffers, completion-vs-ACK, completion-vs-halt, every pause
participant, crossing-16 guest atomics concurrent with copies, code/PTE alias
writes, outstanding old compiler replies and overlap among payload/status/rings.
Check snapshots remain stable after release and no external result can choose a
new GPA. Use deterministic scheduling/fault injection before real backend tests.
Correctness acceptance is scoped to those cases, not universal ISA conformance.

Record numeric run/time/storage limits before executing each slice. Measure
throughput, latency distribution, host CPU, copied bytes, in-flight occupancy,
RAM-window wait/pause time, kicks/refaults and wakeups from the first working path.
Change one mechanism at a time; compare equal feature/durability profiles. No
new network or disk workload was run for the planning revision. The implementation
kickoff and its development checks are recorded below.

### Implementation kickoff — November 8

The first runnable increment establishes RAM snapshot/commit authority before
connecting a device that can DMA. Resident acknowledgements occur at dispatch
before block selection, HLT waits and AP startup waits. An active helper,
page-table walk, split atomic or MMIO recovery completes before acknowledging;
`in_host` is not authority. The initial protocol released shadow holdings before
the acknowledgement barrier so waiting peers could finish. The current repair
below retains mappings while parked and services peer shadow-drop requests until
release. Read-only snapshots need no code flush; writes invalidate affected code
and, for previously observed table pages, mapping currency before reentry.

Source mapping also corrects an earlier concern: translated `mmio::device`
already banks its instruction and returns through dispatch after completion.
It does not resume a generated block tail. Deferred operations still need a
control-only pending state and an owned completion/linear address; the current
post-HVC `site_address` must not retain metadata across an invalidating pause.

| Existing runtime path | Initial integration |
| --- | --- |
| `execution::find_from`, including precise doorbell stops | Check the pause before invalidation/event processing and before selecting a block. Decline `sys_fast` when a request is pending. |
| `events::park` and `wait_mailbox` | Check before guest-memory work on each wait iteration; host doorbells wake the native wait. |
| Shadow/source-mark waits, split-atomic service, `REQUEST_PAGE` recovery | Do not acknowledge with an operation or native continuation live. Finish it, servicing existing shadow requests, then reach the checkpoint. |
| Host `fetched`/compiler/staged reply, including `REQUEST_STEP` | Complete the current host request and resident instruction before acknowledging. Loader accesses occur before execution. Future independent host RAM observers must use the coordinator. |
| Translated `mmio::device` | Immediate completion already returns through dispatch. Deferred control participation was added in the RNG continuation below; no host callback may impersonate a resident acknowledgement. |

The initial `Backing::copy_guest` accepts owned-buffer reads/writes, bounded to 512 spans and
256 KiB total per window; it exposes no callback or guest pointer. The span budget includes a
full 256-descriptor network chain plus completion and notification bookkeeping, rather than
treating queue size as the whole commit's extent limit. Its caller is an independent owner and
may hold no lock needed by vCPU service. All configured vCPUs must still be participating. A
stopped/unstarted participant fails admission without copying. Range validation covers the
entire batch before mutation; overlapping writes retain caller order. Serialization and
monotonically increasing request/release epochs make canceled late acknowledgements harmless.
The initial write path flushed each resident's entire code cache after release and before guest
reentry; the host release is not a report that all flushes have already finished. That
conservative baseline is retained in earlier evidence. Targeted invalidation preserves unrelated
compiled objects. The first version rechecked global fetch mappings after every write; its
retention/global executable remap checks passed primary and independent execution, as did high
RAM and held compiler-response regressions. The current performance repair additionally tracks
every physical page read as a guest page table, at every level and including failed walks
and retries. Residents publish each mark before the entry load. The host checks these
marks only after all participants are admitted and copy grant; only overlapping writes publish
the separate table-change epoch requiring a global fetch-mapping invalidation. All writes still
post physical code-page invalidations. The current protocol retains shadows while parked,
preserving the full barrier before acknowledgement and the actual holding/writable bits. The
parked loop services peer shadow-drop requests so an in-flight split atomic or code mark can
finish before that peer acknowledges. After release, every window drains pending invalidations,
including canceled and read-only windows. A table-change epoch still resets all shadows and
fetch currency. Marks survive cache/CR3/vCPU/device resets; a table-change window clears the
marks of the pages it writes (November 11: never clearing them sent every write
into a recycled former page table through a window, collapsing fio reads). Zero-copy note: a direct host read
into a marked page cannot be windowed for its whole duration, so such requests would take the copy path; the
alternative, matching real DMA (no window for table pages, relying on the guest's TLB flushes), first needs an
audit that no resident cache outlives x86's invalidation rules. A table first used after release is read from
the new bytes. The bitmap adds about 1.094 MiB for the current 35 GiB physical span. Host
release does not await every resident's post-release work; residents drain pending invalidations
and the applicable table-change epoch before guest access or another acknowledgement. The
[repair results](io-benchmarks.md#copy-first-performance-repair--november-8) include original
fio pairs, targeted regressions and independent rebuild/replay; INV-03-01 owns their scope and
remaining performance limitations. Paused residents still count against the existing native-run
watchdog. Window admission deadlines must fit that watchdog; process supervision remains
necessary for stalled native calls. The polling deadline does not bound host scheduling or the
platform's doorbell call itself.

The sibling queue patch adds `CheckedDescriptorChain`, sharing traversal with the legacy
iterator and preserving its API. Its constructor accepts a captured table/head and an explicit
indirect permission; malformed termination is a terminal error. Callers must consume and
validate the whole result before any effect. The author run and independent fresh-target offline
replay both pass 56 existing tests, ten new external API cases and four doctests. Independent
source review found no blocker. The retained queue manifest and replica live in
`natemu-raw/io1-20261108-01/queue`. At the foundation boundary this was not yet a production
dependency; the subsequent RNG integration below connects it to the owned snapshot consumer.

Development bounds for this increment: queue package checks at most six
invocations of 180 seconds each and 1 GiB retained; native window development
checks at most 24 VM launches of 120 seconds each, plus host/resident builds,
within 4 GiB retained. Retain sources, inputs, outputs and unsuccessful attempts
under `natemu-raw/io1-20261108-01`. These are development/exploratory checks, not
an architectural correctness or performance acceptance campaign. Stop this
increment at a reviewed, runnable coordinated-copy path or a concrete blocking
defect. IO1 as a whole still requires the asynchronous owner, lifecycle and IRQ
matrix above; no disk/network capability is implied by this first increment.

**Development result:** the initial native path runs at N=1/2/4 with 64 steady
snapshots per case, while each vCPU executes cross-16/cross-cacheline locked
operations. All snapshots match complete alternating values; every vCPU makes
progress, sees a host patch to previously executed code and exits cleanly.
Ordered overlapping transfers also pass. The retained `windows-01` source
produces the host/resident binaries and passes the resident stack/gateway audit.
Independent source review found no unresolved blocker, and a fresh offline
host/resident rebuild repeated the N=4 case in `windows-replica-01`. Four live
launches were used, without retries; this is scoped development evidence.
Seven focused host tests cover batch rejection, ordered copies, exact participant
acknowledgements, cancellation/late acknowledgements, consecutive windows,
unwind/poison and resource bounds. These host simulations do not establish native
timeout interleavings. Native HLT/AP, PTE-alias writes, high RAM and deferred
MMIO/reset/IRQ are still untested in this increment. No disk, packet or entropy
backend is connected, and no I/O performance claim follows.

## RNG integration result — November 8

The bounded owner, deferred lifecycle MMIO, cancelable copy permits and checked rust-vmm
traversal over owned metadata snapshots are connected to the real virtio-rng device and the
normal boot, ACPI and command-line path. Stock Linux discovers the device, reads entropy across
driver unbind and rebind, takes the device interrupt and powers off cleanly at one, two and four
vCPUs. Source, method and the replica author's independence limits are in the [review
verdict](../research/reviews/INV-03-01-findings.md#virtio-rng-linux-integration--november-8-2026).

Limits that remain: these are development results, not entropy-quality or
performance evidence, and Linux unbind/rebind does not cover the full adversarial
reset, grant and interrupt matrix. This baseline used conservative whole-cache
write invalidation. Targeted invalidation was added afterwards: written physical
pages post through the existing code-invalidation records and drain after
release, and the global fetch-mapping invalidation is restricted to previously
observed page-table pages (the table-dependency repair above), so arbitrary DMA
writes to page tables stay covered while unrelated compiled objects survive. The
original fio admission workload then completes; no causal speedup or comparative
performance score is established (see the
[repair results](io-benchmarks.md#copy-first-performance-repair--november-8)).
Launch counts and retained evidence are under `natemu-raw/io2-rng-20261108-01`.

## Persistent disk and lifecycle results — November 8

`--disk FILE` exposes the accepted block profile through the shared transport
and an owned native positioned-I/O lane. The design held up under stock Debian
Linux: an installed ext4 root boots, files are written, overwritten, copied,
renamed and deleted, the filesystem syncs and remounts read-only, and power-off
occurs only after the final native full-sync and lane retirement. Fresh
relaunches verify exact persisted content, an unchanged image on the read-only
boot and clean offline fsck, at one, two and four vCPUs, with an independent
fresh build replaying the four-vCPU case. This is sequential disk and RNG
co-presence, not concurrent combined acceptance. Retained evidence is in
`natemu-raw/io3-block-20261108-01`.

Deterministic lifecycle cases through the production owner and ordinary guest MMIO confirmed the
reset, shutdown and failure design: active reset or disable, scalar and REP power-off,
final-sync failure, real positive read/write prefixes followed by injected EIO, malformed-chain
recovery, and a full 128-descriptor queue of FLUSH chains. Each checks its complete RAM and file
oracle, architectural progress, event ordering and teardown, and independent builds repeat the
main ones. A further two-vCPU case releases accepted work only after the initiating CPU's power
poll and its peer's HLT park state. Limit: these test-only controls hold before the native call
and inject errors before a later syscall, so they do not establish actual macOS failure
atomicity. Retained under `natemu-raw/io3-lifecycle-20261108-01`. Review verdicts: [lifecycle
findings](../research/reviews/INV-03-01-findings.md#deterministic-lifecycle-findings-and-independent-replay--november-8-2026).

Extended RAM-window cases (author and independent two-vCPU runs) confirmed the window design for
copied and executed addresses above 4 GiB, a page-table mapping change, HLT and interrupt
progress, and AP mailbox wait and wakeup
([verdict](../research/reviews/INV-03-01-findings.md#extended-ram-window-findings-and-independent-replay--november-8-2026)).
Limits: the page-table case does not guarantee a hot alias at the final write, and the
standalone HLT and AP cases do not identify a particular grant during waiting. A later
pending-response check passes primary and independent runs: a posted compiler request held
before response consumption prevents copy grant, timeout has no effects, and release permits
completion at the exact epoch with guest progress and code-patch visibility. It closes that
delayed-response timing check, not compiler-crash behavior or all interrupt/grant races. Further
interrupt/grant races, networking, combined workloads and matched performance checks remain
open.

**Next implementation:** finish the remaining scoped acceptance checks, basic
networking and combined acceptance. Native vmnet admission failed in the current
unprivileged launch; the deployment/backend decision is pending. Keep backends
on owned buffers; no live guest-RAM adapter or zero-copy path is enabled. The
owner's full disk/network objective remains unchanged; backend integration awaits
the launch choice described in the native admission gate.

### I/O safety stress — November 8

Three repeatable runners extend the single-event cases; their acceptance and execution
accounting are owned by the active safety entry in INV-03-01, the raw campaign is
`natemu-raw/io-safety-stress-20261108-01`, and the review verdict is in the [pre-execution
review](../research/reviews/INV-03-01-findings.md#io-safety-stress--independent-pre-execution-review-november-8-2026).

| Runner | Exercised behavior and failure oracle |
| --- | --- |
| `tools/run_io_stress.py` | N2/N4 seeded copy windows while guests perform crossing-16-byte LOCK NOT and LOCK INC, alternate active/HLT states, and consume changed global PTE mappings and compiled code. Every cycle checks known data/code, mixed and fully active snapshots, per-CPU progress, stable cumulative parity and an exact shared increment count; final RAM and epoch/ACK totals must agree. |
| Same runner with `--forced` | Separately built scheduling hooks hold real request/quiet/withdrawal/ACK/grant boundaries. A real split peer posts a late shadow-reset request (a peer vCPU servicing a shadow-reset request late, after parking) after another CPU's ACK. Cancellation is exercised both before and after grant, including announced canceled epochs following table/code writes. The oracle requires the reached schedule, effects, progress and zero hook timeouts. |
| `tools/run_io_lifecycle_stress.py --case reset` or `disable` | 64 generations each, with the backend paused before its native positioned read until reset/disable is deferred. Check the canceled queue's whole payload/status/used entry, fresh queue data and IRQ acknowledgement before reusing buffers. N4 includes one genuinely halted peer and two active split-atomic peers; final complete RAM, disk and syscall/event traces must match. |
| `tools/run_io_soak.py` | Stock Linux, fio 3.39 and verified stress-ng CPU/memory methods. Concurrent pure random reads from an immutable CRC file and CRC-tagged writes to a separate file; 60-second unloaded context, then 300 seconds under two CPU workers and a 256-MiB memory worker. Require sampled worker progress/overlap, full verification before overwrite and after load, hashes, S5, offline fsck and a fresh read-only restart with unchanged image. |

Normal builds omit the `io-test-hooks` feature. The forced driver and resident
are built with that feature into a separate target directory, and their elapsed
time is never used as production throughput. Test metadata occupies an existing
private gap without changing production offsets. The hook atomics prove the
selected schedules, not the necessity or sufficiency of memory barriers under all
executions. The normal resident's entire `.boot` and `.text` are byte-identical
to the selected HLT performance candidate; changed read-only bytes encode source
line numbers.

Oracle sensitivity was checked: a corrupt completion in the seventh generation
is rejected before the next generation can erase it, and an isolated resident
mutant that suppresses CPU0's late shadow-reset service must reach the actual
pending peer schedule and fail with an acknowledgement timeout, unchanged late
service count, zero hook timeouts, released epoch and clean teardown. A generic
timeout or arbitrary failed VM does not qualify as this negative control.

Retained failures: the first two lifecycle attempts used invalid fixture MMIO
forms (a sign-extended absolute address, then an unsupported direct CMP); the
corrected fixture uses MOV through its MMIO base register and checks assembled
forms. The first Linux soak completed its workloads and CRC sweeps, then
reported an RCU stall during multi-megabyte UART log export and remains failed.
Lossless compressed export with bounded decoding, byte counts, SHA256 and pacing
reduces evidence traffic, but does not establish that the bulk-serial stall is
fixed (the console repair below addresses that). Three deliberate negatives and
three unexpected failed attempts are all accounted for in the 24 launches.

Primary and independent execution pass the finite normal, forced, reset/disable and soak/restart
sets; exact counts, provenance and performance are in INV-03-01 and the [safety-stress
benchmarks](io-benchmarks.md#safety-stress-performance--november-8). These checks add finite
race, lifecycle and load coverage. They do not establish universal race freedom, crash or
power-loss durability, full distribution boot, guest reboot, native networking or zero-copy
safety. Performance observations stay in the benchmark record, separate from forced-schedule
timings and failed-run observations.

### Sustained console repair — November 8–9

The repair is done and accepted ([review
verdicts](../research/reviews/INV-03-01-findings.md#loaded-io-diagnostic-and-uart-repair-review--november-8-2026),
[final
qualification](../research/reviews/INV-03-01-findings.md#independent-final-uartcombined-build-qualification--november-9)).

Problem: the load campaign reproduced the UART failure independently of disk I/O.
A stock four-vCPU Linux guest continuously writing 4.7 MB kept its serial IRQ CPU
from running its heartbeat for about 26 seconds and emitted an RCU stall; pacing
the same bytes avoided it. The original model immediately reported THRE/TEMT and
reasserted the transmit interrupt after every accepted byte, so Linux's 8250
handler could transmit without a useful not-ready interval.

Design: the accelerated console still accepts every THR byte, including REP
OUTS, but stages at most 16 output bytes and spills full batches synchronously.
Each external byte clears transmit readiness; the existing timer service drains a
partial batch and publishes readiness after an inactivity interval measured from
the last byte, after any synchronous spill. Loopback stays instantaneous. Reset
and loopback transitions flush already accepted external bytes, and shutdown
drains them before the final output/error observation. The first sink error is
retained. Limits: this is an accelerated-console policy, not physical baud
timing, FIFO overflow emulation or a host-scheduling guarantee; arbitrary
blocking sinks can still block, as with the previous synchronous writes. A
20-microsecond readiness delay was selected over 50 for this finite scope (higher
serial throughput in single runs, nearly unchanged host CPU); neither delay is a
general fairness bound.

Acceptance: `tools/run_serial_stress.py` checks exact wire bytes, kernel diagnostics, one pinned
heartbeat per vCPU, clean S5 and heartbeat gaps of at most two seconds, on the original and a
three-times-longer payload, supplemented by focused register, REP OUTS, timer/interrupt and HLT
fixtures. A fresh independent build repeats the longer payload, and both builds pass a
CPU-loaded extension with verified stress-ng workers. Numbers, the retained failures and the
rejected tuning are in [the UART benchmark
record](io-benchmarks.md#sustained-uart-reproduction-and-readiness-tuning--november-8).

Retained failures and fixture changes: the fixture explicitly drains the Linux
console with `tcsetattr(TCSADRAIN)` before forced poweroff because one earlier
candidate run kept exact bytes and no stall but lost its final guest-queued
marker at poweroff; that run remains failed, and the unchanged baseline still
fails the stall check with the drain-aware fixture. The same drain is in the
ordinary-disk and soak inputs for every engine without changing fio jobs,
verification, error rejection or time limits. The first loaded attempt remains
failed for two missing observer samples. A plausible but unconfirmed cause is a
source-backed observer defect: a scan could abort when an unrelated `/proc` path
vanished. Nonfatal per-path reads fix it while keeping the strict
consecutive-sample oracle; synthetic checks accept unrelated disappearance and
reject missing actual workers. The original failure is not promoted to a pass.
Limit: this is CPU-loaded serial coverage, not a claim about arbitrary memory
pressure or scheduler fairness.

The repair does not change guest-memory exclusion or enable direct DMA sharing.
The normal disk matrix, soak/restart pairs and fresh independent native stress
cycles also pass with it. No overall throughput gain is established; the
[benchmark record](io-benchmarks.md#completed-normal-combined-comparison--november-89)
owns measured costs and rejected tuning.

### Network frontend continuation

The modern RX/TX frontend uses per-queue generations and notification state;
disabling one queue preserves the other's active operation, indices and IRQ.
`PacketPort` exchanges only bounded owned Ethernet frames (no FCS, MTU1500, at
most 1518 bytes including one VLAN tag). One owner alternates eligible queues.
RX arrival schedules work even without another guest kick; empty RX buffers alone
do not retain a busy guard. TX callback retirement precedes completion and reset.
Backend failure cannot publish a successful power drain.

The earlier short-RX failure proposal is corrected: a valid chain too small for
the received frame drops that frame without consuming the chain or writing a
prefix. The guest may supply smaller buffers for smaller packets; the spec's
1526-byte recommendation is not mandatory. Invalid routes, directions or chain
structure still fail the device, but an incoming packet cannot turn a
well-formed short chain into a malformed guest request.

Retained one-vCPU guests confirmed headers, short-RX, queue lifecycle and malformed-chain
recovery, with exact whole-RAM and packet oracles covering split and combined headers, unaligned
buffers, 64 and 1518-byte frames, used lengths, retained queue indices and clean teardown; an
independent fresh build repeats headers and queue lifecycle. The final two frontend slots pass
with targeted DMA invalidation and an endpoint-lifetime guard. A further two-vCPU installed-root
disk pair and REP/HLT shutdown regression pass with the shared transport build
([verdicts](../research/reviews/INV-03-01-findings.md#network-frontend-findings-and-independent-replay--november-8-2026)).
The echo endpoint does not establish external connectivity, native admission or ordinary
command-line networking, nor force an active other-queue reset race or network IRQ delivery.

A bounded HLT/IRQ check passes primary and independent fresh builds: with no TX or notification
work queued, the endpoint guard keeps the halted guest alive until one owned packet arrives,
giving a real GSI17 interrupt, correct acknowledgement and received bytes, then clean S5. An
isolated missing-guard control ends early with no packet, IRQ or RAM effects; its first
checker-label failure and the corrected fresh run are retained
([verdict](../research/reviews/INV-03-01-findings.md#network-endpoint-all-hlt-lifetime--independent-review-and-replication)).
This establishes finite resident-HLT arrival behavior and shutdown after RX only.

Two further one-vCPU lifecycle cases pass primary and independent fresh builds (retained under
`natemu-raw/io4-active-20261108-01`): a held owned TX callback survives disabling idle RX,
publishes once after release, and the same TX queue then works with fresh RX; separately, S5
commits while TX is held, then retires the callback and publishes its completion before
successful shutdown. Both rejected RX echoes leave the original RX region unchanged
([verdict](../research/reviews/INV-03-01-findings.md#active-network-tx-lifecycle--independent-fresh-build-results)).
The test driver adds only a bounded metadata barrier; production code is unchanged. These close
the two named schedules, not arbitrary SMP traffic, active same-queue reset or native
callback/service lifetimes.

**Backend (November 12).** A re-check of vmnet on macOS 26's new network API admitted a
non-root interface with Apple's virtualization entitlement but delivered no traffic
(no DHCP or ARP reply, no host bridge); socket_vmnet still runs that API as root
(`natemu-raw/io-vmnet26-admission-20261112-01`). The owner chose libslirp with
hand-written bindings. The `--net` option and backend
([README](../../crates/host/src/virtio/README.md#network-backend---net)) pass DHCP,
DNS, gateway ping, byte-exact HTTP, iperf3 TCP/UDP, HTTPS, a driver reset under a
receive stream and SIGINT under traffic at 1/2/4 vCPUs, with independent review
and replication (`natemu-raw/net-slirp-20261112-01`, `natemu-raw/net-replica-20261112-01`;
throughput is recorded, not yet benchmarked: about 775 Mbit/s guest-to-host and 1.3 Gbit/s
host-to-guest). This is the first scope of the network contract above, not all of it:
IPv6 and ICMPv6, the mixed-family resolver correction (03-03 claim 59), the contained
broker process (claim 70) and port forwarding remain follow-on work. Next: stress and
edge cases (soak, many connections, overload, lifecycle), then combined disk/network
acceptance and a benchmark round against QEMU user networking and VZ NAT.

The independent fio preparation is followed by accepted functional admission.
The first unmodified fio run parsed all eleven profiles but timed out during the
64 MiB verification workload; its failed image and logs are retained, no restart
verification ran, and diagnostics showed advancing writes with limited native
observations. After the reviewed targeted invalidation change, unchanged
original write/relaunch pairs pass in primary and independent builds (CRC
checks, profile parsing, input hashes, clean S5/fsck, persisted file hashes and
an unchanged image on read-only restart). No I/O performance result is accepted
from this entry; see the [benchmark record](io-benchmarks.md).

## Concurrent block requests and batched windows (proposed, November 9)

**Status: approved; stages 1 and 2 built (stage 2 result below).** It widens two bounds this
plan set as starting points ("one block request in flight", "one request per RAM window"),
which the plan said to revisit after measuring pause time. An independent review (November 9)
found the first draft unready; this version addresses it. If accepted, the earlier texts that
assume one active request (the protocol's reset/S5 rules, the backend table's "one ordered
blocking lane" and the sizing paragraph) are amended in the same change.

**Measured motivation.** At four vCPUs natemu reaches about 45% of Virtualization.framework at
queue depth 1 but 10–15% at depth 16 or with four readers
([references](io-benchmarks.md#native-and-same-architecture-virtualization-references--november-9)).
Window counters on an 89–99 s fio write run (`natemu-raw/window-cost-20261109-01/`):

| vCPUs | Windows | Window open, share of wall time | Mean window | Waiting for acknowledgements |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 4.96 M | 17% | 3.4 µs | 92% |
| 2 | 5.40 M | 24% | 4.0 µs | 93% |
| 4 | 5.29 M | 26% | 4.4 µs | 94% |

Copying is about 6% of window time and epoch-lock waits are negligible (0.13 s in total). At
about 35 k IOPS a request takes about 28 µs, of which its two or three windows take about 13 µs,
and requests are served one at a time. So fewer windows per request and more requests per window
both pay.

**Stages** (each separately reviewed and measured; block only, entropy and network unchanged):

1. **Next metadata in the completion window.** The completion window already reads the
   available ring's flags and index after publishing; it also reads the available ring and
   descriptor table (bounded, queue-sized) into the snapshot, so the next request needs no
   separate metadata window. The snapshot takes the identity current at grant; a canceled or
   ungranted completion window discards it; its extra bytes (about 4.5 KiB at 128 entries) count
   against MAX_BYTES. Expected: about one window fewer per request at every depth.
2. **Several requests in flight, opportunistic batched completion.**
   - *State.* `active: Option<Identity>` becomes an in-flight set per queue incarnation. Reset,
     disable and S5 retire when the old incarnation's in-flight count reaches zero; granted
     completions still retire, ungranted ones are dropped (generation check, as today).
   - *Ring bookkeeping.* `next_avail` advances at admission, under the owner. Used slots are
     assigned at commit, in completion order; a batch of n writes its elements, then one
     `used.idx = next_used + n`, then reads `avail.flags` last for interrupt suppression. The
     "more published" test compares against the current consumed index, not the admitted copy.
   - *Owner loop.* One wake source: workers, guest notifications, reset and power drain all
     signal the owner's condition variable; the owner never blocks on one job. The 10 s
     abandonment bound applies per backend call; any expiry is VM-fatal, as today.
   - *Workers.* A fixed pool of P = 4 threads on the one shared file descriptor, taking owned
     jobs. Bounds: K = min(queue size, 32) requests and B = 4 MiB of owned buffers in flight,
     IN buffers reserved at admission; admission pauses at either bound. Host threads become
     vCPUs + device owners + 4.
   - *Batched completion.* When the owner wakes, every finished request completes together,
     split into windows that fit MAX_BYTES (raised to 1 MiB) and MAX_SPANS (sized from K), each
     window publishing whole requests. No artificial delay.
   - *S5.* Stop admission, wait for the in-flight count to reach zero, run the final
     `F_FULLFSYNC`, then join the workers.
   - *Failure.* A malformed chain sets NEEDS_RESET for the device, as today. Requests already in
     flight still complete visibly (their buffers stay owned until they retire); nothing is
     admitted after the failure. This matches the current single-request behaviour and is
     checked against Linux virtio_blk, which does not recover from NEEDS_RESET.
   *Stage 1 result (November 9, approved and built): gate missed.* Windows per completed I/O
   fell from 2.99 / 2.96 / 2.84 to 2.81 / 2.74 / 2.65 at one, two and four vCPUs (6–9%), and
   window time per I/O from 10.1 / 12.0 / 12.6 µs to 8.6 / 10.5 / 11.5 µs (9–15%), against the
   30% gate (`natemu-raw/window-cost-stage1-20261109-01/`). Counting at four vCPUs: 60% of block
   requests still needed a metadata window, because with one request in flight the guest has
   usually not published its next request when the previous one completes. Stage 1 is kept as a
   small, correct gain (block, lifecycle, fio admission and normal/forced stress pass); its
   intended effect depends on overlapping requests, so stage 2 carries the windows-per-request
   target as well.
   *Stage 2 result (November 9): improvement, gates not met.* All correctness runs pass
   (host tests, 14 lifecycle cases, lifecycle stress, block, fio admission, normal and forced
   stress, soak and matrix persistence). At four vCPUs, against HEAD: four concurrent readers
   rise from 42–47 to 69–88 kIOPS (1.5–2x) with worst-job p99 falling from 6–11 to about 4–6 ms;
   the soak under CPU/memory load rose from 11.5/8.4 to 19.7/11.0 kIOPS read/write in the first
   measurement; a single depth-16 job stays at 34–39 kIOPS (about 1.0–1.1x); depth 1 is 2–5%
   lower (14.1–14.7 against 14.5–14.9). Windows per completed I/O fall from 2.84 to 2.19–2.21
   (22%). Gates missed: depth-16 2x, windows 30%, depth 1. Measurements are noisy: `fseventsd`
   held one host core near 100% during the later runs (load average 5–9), and repeated runs of
   one build differ by up to 20% (`natemu-raw/bench-stage2-20261109-01/`, `bench-stage2b-…`,
   `stage2-recapture-20261109-01/`). The review's recapture-after-notification fix added about
   13% more windows per I/O with no measured gain, so it now applies only when nothing is in
   flight (with requests in flight, their completion refresh re-arms the queue).

   Reading: concurrency helps when several guest jobs submit at once; a single submitting
   job is limited by its own vCPU, which windows pause for about a quarter of the time, and each
   window is 94% acknowledgement wait. Stage 3 (batched capture) cannot change that; the larger
   lever is cheaper windows.
3. **Batched capture**, only if the counters still show capture windows dominating: one
   metadata snapshot admits up to K chains; their headers and OUT payloads are gathered in as
   few windows as the byte and span bounds allow. *Built as part of stage 2* (the owner asked
   for admission of every published head from one snapshot in the same change).

   *Stage 2 build (November 9, branch `io-concurrency-stage2`; awaiting review and the
   main session's measurements).* As designed, with these specifics: the block device has its
   own owner loop (entropy and network keep the one-request loop, sharing the in-flight set,
   reset and window code); the owner's wake source is a sticky bell that guest notifications,
   resets, power drain, stop and disk workers ring; the 10 s limit is measured per native call
   from the moment a worker starts it; a job queued at abandonment never runs; IN buffers and
   the gathered header/OUT stream are reserved at admission (a request whose readable stream
   exceeds any valid request is answered IOERR unread and reserves nothing for it); the
   metadata refresh rides on the last completion window when it fits. A granted completion
   window counts each request as one `commit` in the test trace. Window bounds: MAX_BYTES
   1 MiB, MAX_SPANS 1160 (32 requests of 32 data segments plus header, status and used
   element, and the window's ring bookkeeping); a single request always fits, larger batches
   split. Forced schedules added: `out-of-order` (the sector-0 read is held until another
   request is published), `cancel-batch` (the completion of four answered requests is held
   until a device reset defers behind them, so its window is never granted) and `reset-mixed`
   (a reset defers behind completed, running and queued requests). The lifecycle guests now
   wait for every expected interrupt bit, because a concurrent completion may raise its bit
   after another's; `malformed-later` now fails the device at admission, before the valid
   first request completes.

**Flush.** Virtio 1.2 §5.2.6 requires a flush to cover writes that had *completed* before it.
A completed write's `pwrite` has returned, and the sync (`fsync` since November 10, see the
durability paragraph under IO3) covers everything the file has written, so a FLUSH runs on any
worker with no ordering against other requests, and later requests never wait for it. (Linux
already waits for data completion before issuing its flush.)
Writes without negotiated FLUSH stay write-through.

**Tests and acceptance.** Host tests for admission bounds, out-of-order completion and used
slots, batch splitting at the window bounds, partial failure, reset/disable/S5 with K requests
in flight, and a flush that fails while writes are in flight. The forced-schedule controls are
extended: hold the k-th disk call while others finish; cancel a completion window covering n
requests; reset with requests in every state. Then the full standing suite; lifecycle, stress,
soak and persistence runs; and the matrix at one, two and four vCPUs against natemu HEAD,
QEMU and the VZ reference, with the window counters. Thresholds, against HEAD
(`natemu-raw/io-accept-20261109-01/`, `perf-head-20261109-01/`), each measured over two matrix
runs per vCPU count, as HEAD was:

- *After stage 1:* windows per completed request at least 30% fewer; depth 1 at one, two and four
  vCPUs no worse than HEAD's slower run; every correctness run passing.
- *After stage 2:* windows per completed request at least 30% fewer than HEAD; random read depth 16 and four readers at four vCPUs at least 2x HEAD; window-open
  time per completed request no higher than HEAD's (about 13 µs at four vCPUs); depth 1 as above;
  serial heartbeat and timer stress results unchanged; every correctness run passing.

**Not in this step.** Cheaper windows themselves: about 94% of window time is waiting for running
vCPUs to reach a checkpoint, so a later step should measure acknowledgement latency per vCPU
and consider pausing only vCPUs that could touch the affected pages. Also out: network and
entropy batching and zero-copy. (The non-`F_FULLFSYNC` flush mode was later decided by the
owner, November 10: guest flushes use fsync, power-off F_FULLFSYNC.)

## Windowless copies with targeted invalidation (proposed, November 9)

**Status: accepted under the owner's November 9 delegation** ("optimize the usual cases and make
exceptions for edge cases"; avoid unnecessary stalls), revised after an independent review the
same day (two blocking gaps and four should-fixes, all addressed below).

**Why.** After stages 1–2, about 94% of every window is spent waiting for vCPUs to acknowledge,
and a single submitting job stays flat because windows pause its vCPU about a quarter of the time.

**Observation (read-only verification, November 9).** Every resident coordination protocol is
shared-memory atomics plus doorbells and does not depend on the writer being a vCPU. A resident's
own alias stores, which never fault, already follow an explicit sequence: if the page is
translated, `written` clears every vCPU's code mark for it and posts the stale page to each holder
(record bit, summary, `PAGES`, doorbell); then the store; then `rechecked` catches a holder that
marked the page in between. A translation in flight is rejected by `kept` when its source page has
lost the vCPU's mark. Page-table shadows behave like a TLB and are not tracked for guest stores;
`TABLE_PAGES` marks exist only for host writes; only a table-change window clears them.

**Design.** A copy batch no longer pauses vCPUs, except as listed:

| Target | Treatment |
| --- | --- |
| Reads of ring metadata and OUT payloads | No coordination. The device owns published chains and payloads; ring fields are aligned and the guest accesses them with single native loads/stores (verified for the selected FEX profile). An acquire barrier follows the `avail.idx` read. |
| Writes to pages with no translation and no table mark (the usual case) | Check marks, copy with width-aware or 16-byte-chunk copies, then a full barrier (`dmb ish`), then recheck the code mark, exactly as the resident's own `rechecked`. A vCPU that marked the page and compiled from torn bytes during the copy is a guest race (it executes a buffer the device owns); the recheck clears and posts it like the next row, so the stale block cannot survive completion. |
| Writes to pages with translations | Before the copy, clear the marks and post the page to each holder (stale bit, summary, `PAGES`), then kick it through its doorbell SPI (`Stops::kick_if_needed`), posting before kicking as `invalidate_page` does; after the copy, barrier and recheck as above. Before publishing the completion, wait until every vCPU posted by either step has cleared its stale bit for the page (its `drain`), is halted (HLT and mailbox waits drain before guest access), or has ended. Only those vCPUs, only this case. |
| Writes to pages with a page-table mark | Checked before any write: the whole batch uses today's window (all vCPUs, `tables_written`). Rare: 13 of 5.6 M windows. A page becoming a table during a copy is a guest race. |
| Guest atomics on a target page | No coordination. Split atomics keep no state outside guest RAM, so a guest atomic on a buffer it gave the device is a guest bug with no effect on natemu's own state. |

Publication order per completion: payload and status stores, `dmb ish`, used elements, `dmb ish`,
`used.idx` (release), a full barrier, then the `avail.flags`/`avail.idx` read for suppression and
re-arm (the device-side half of Linux's `virtio_mb` pairing; today a window hides the need). The
interrupt follows publication, as today.

The drain wait holds no device or VM lock (as `copy_guest` requires), skips ended vCPUs, and has a
deadline; expiry falls back to a full window for the completion rather than publishing early. A
holder returning from an MMIO exit may run the rest of one block before its doorbell stops it, and
admission keeps intra-block branches forward, so no linked loop runs without a boundary. Windowless
batches do not take the window epoch lock, so they never wait behind a fallback window whose
checkpoint their holders are parked in.

**Kept from copy-first.** Backends touch only owned buffers; the device owner alone copies; whole
batches are validated before any write; reset, disable and S5 still wait for in-flight work; the
window path remains for the table-page fallback and for the test hooks that exercise it.

The table-mark ordering argument (`mark_table_page`, which assumed host reads only inside a granted
window) is rewritten for the pre-copy check: a mark the host misses belongs to a walk that started
during the copy, which is a guest race.

**Edge cases and risks.** Table marks are permanent, so recycled page-table pages force fallback
windows; `table_windows` measures it (a clearing mechanism is a later step if it grows). The posted
invalidation applies at the holder's next block boundary rather than before any guest access; the
pre-completion wait keeps today's guarantee that the guest cannot observe completed data with stale
code. Host stores are not TSO-ordered; virtio needs only the publication barriers above.

*Build (November 9, branch `io-windowless`; awaiting review and measurements).* As designed,
with these specifics. `Backing::copy_guest_windowless` takes the index where the publication
(used index, then the available-ring read and any snapshot refresh) starts; every virtio copy
uses it, and the test drivers' own `copy_guest` calls keep the window. The permit is granted
after the table check and before the pre-copy posts; a table fallback grants inside its window
as before. Payload and used elements are not separated by a barrier: the guest reads both only
after the used index, which a full barrier precedes. The recheck after the body covers the
body's pages; a mark of the used index's page found after publication is posted and rung but
not awaited (executing the used ring is a guest race). Only the IoWindow `quiescent` word
counts as halted: `Events::halted` stays set until after the HLT loop's last drain, and AP
mailbox waits drain on each doorbell anyway. A drain-deadline fallback copies only the
publication through the window (the body is already in place). The metadata snapshot now
reads the available ring's flags and index, its entries, then the descriptor table, so the
acquire after the index read covers what the driver published before it. The deadline-expiry
count is also the drain-fallback count. Host test points: `AFTER_PRECHECK`, `BEFORE_RECHECK`,
`BEFORE_PUBLISH`. After review: the pre-copy check also awaits every vCPU that still owes a
drain of a written page (its stale bit is set because an earlier writer cleared the mark
first); halted vCPUs are not rung; an error after the grant is fatal to the VM, never a
cancellation; the device-poll and power-off waits in the resident now drain on each poll; the
lifecycle suite's `code-page` case shows a live post and drain before publication.

**Alternative kept as plan B.** A hidden device vCPU running the same protocol in a resident,
with guest vCPUs capped at host cores − 1, if host-side kicks or ordering prove inadequate.

**Evidence required.** Host tests for each row, using new host test points (after the pre-copy
check, between copy and recheck, before publication), including a mark raced during a copy and a
holder that must drain before publication; the existing window hooks keep covering fallback
windows. Counters: windowless batches, batches that posted, holders found by the recheck, drain
waits (count, total and maximum time, deadline expiries) and fallback windows by cause. Then the
full standing suite; lifecycle, stress, soak and
persistence runs; window counters split into windowless batches and fallback windows; the matrix
at one, two and four vCPUs against HEAD and the VZ reference. Targets at four vCPUs: share of wall
time with vCPUs paused below 2%, single-job depth 16 at least 2x HEAD, depth 1 no worse.

## Fewer exits on the device path (proposed, November 10)

**Status: built and accepted (November 10).** Independent review found a blocking race (a guest
reset during the owner's idle flag clear was treated as fatal), fixed with owner-loop tests that
fail on the old code. Interleaved A/B on one image at four vCPUs: depth 16 89.8 → 132.2 kIOPS
(exits per I/O 3.10 → 1.52), four readers 157 → 197 kIOPS, depth 1 unchanged; the page query is
gone; kicks per I/O 0.14–0.20 under load (`natemu-raw/io-exits-20261110-01/ab/`, `profile/`).
All targets met; standing suite 136/136, 20 lifecycle cases ×3 and every I/O runner pass.
Item 4 (EOI broadcast) remains: removing it needs IOAPIC pin state in shared words with a
barrier handshake; worth about one exit per I/O at depth 1. Motivated
by the [per-request profile](io-benchmarks.md#per-request-profile-after-windowless-copies--november-10):
five exits per request at depth 1 and about 3 at depth 16, about 2 µs each under load, and 12–14%
of vCPU time waiting on device mutexes with four readers.

1. **Kick suppression.** While the block owner is admitting or has requests in flight, it sets
   `VIRTQ_USED_F_NO_NOTIFY` in `used.flags` (virtio 1.2 §2.7.7; Linux checks it in
   `virtqueue_kick_prepare` without EVENT_IDX). Before the owner goes idle it clears the flag, issues
   a full barrier and re-reads `avail.idx`; new heads are admitted instead of sleeping, so no kick
   is lost. The flag store is an ordinary windowless write to the used ring's page. Entropy and
   network keep notifying.
2. **Cached device-page claims.** A read of a non-RAM page first asks the host whether a device
   claims it (`REQUEST_PAGE`), every time. Claims are fixed once the VM starts (no hotplug, no
   relocatable windows), so each resident caches claimed pages in a small table and skips the
   query on a hit. The host asserts the registry is frozen before the first vCPU runs; an
   unassigned page keeps today's behaviour (its all-ones mapping already avoids repeats).
3. **QueueNotify without the device-state mutex.** The common case (queue ready, driver OK, no
   reset or deferred access pending) becomes an atomic flag and a bell ring; anything else takes
   today's locked path. The outer per-device mutex is not held while waiting for the virtio state
   lock.
4. **EOI broadcasts** for the level-triggered line are investigated, not changed, in this step:
   removing that exit needs IOAPIC state outside the chipset lock.

**Invariants kept.** Reset, disable and S5 ordering; the deferred-access FIFO; interrupt
suppression semantics; strict-mode unassigned-page reporting.

**Evidence.** Host tests (lost-kick race around flag clearing; reset with the flag set; notify
fast/slow paths; page cache hit/miss and frozen-registry assertion); the standing suite; the I/O
runners and edge cases; the profile and matrix rerun. Targets at four vCPUs: exits per I/O at
depth 16 at most 2, depth 16 +20%, four readers +15%, depth 1 no worse.

## Shorter depth-1 path (proposed, November 10)

**Status: built (November 10); independent review accepted it after two fixes (the watchdog now
prints the VM-fatal reason; a test asserted the wrong panic).** Items 1 and 3 are in; item 2 is
built and measured, and off by default (it did not pay within the host-CPU bound). Interleaved
A/B on one image, four rounds, host page cache warm ([results](io-benchmarks.md#shorter-depth-1-path--november-10)):
depth 1 at one vCPU 20.9 → 27.3 kIOPS (+31%; median +26%, one round an outlier), at four
15.5 → 17.5 (+13%, short of +15%); depth 16 and four readers within run-to-run noise; host CPU per
I/O at depth 1 falls 17–28%. **Trade-off:** a pool call that hangs past the 10 s limit is
detached and the process exits; the owner's inline call cannot be, so a hung inline syscall keeps
the process alive (the VM canceled, the reason printed) until the syscall returns. Accepted for
reads and unsynced writes, the only calls made inline. The
remaining depth-1 gap at four vCPUs is the second halted-vCPU wake per request: Linux 6.12 routes
the IRQ with fixed delivery to one CPU and its scheduler then wakes fio's idle CPU by IPI;
retargeting the IRQ would depart from the programmed routing, so it is not done. The
original design follows. The
[per-request profile](io-benchmarks.md#per-request-profile-after-windowless-copies--november-10)
shows a depth-1 request spending about 31–33 µs from notify to the guest's ACK, of which about
9–10 µs are three host thread hand-offs (notify → owner, owner → worker, worker → owner) and
7–9 µs per halted-vCPU wake, with about two wakes per request at four vCPUs (the completion
interrupt, then a guest function-call IPI to fio's vCPU).

1. **Inline disk call for a lone request.** When admission leaves exactly one request in flight
   and nothing else is queued, the owner makes the native call itself instead of handing it to a
   worker; deeper queues keep the pool. Notifications that arrive meanwhile are kept (suppression
   and the completion's ring re-read already cover them).
2. **Bounded spin before sleeping.** The owner, and a worker with a just-finished job, spin
   briefly on their wake source before blocking (bounded, e.g. tens of µs, only after recent
   activity), trading some host CPU for the condition-variable wake. Measured against host CPU
   per I/O.
3. **Halted-vCPU wake.** Find and remove the 3–4 µs between an interrupt reaching a waiting vCPU
   and its leaving the park; investigate (not change) whether completion interrupts can target
   the submitting vCPU without departing from how the guest programmed IOAPIC routing.
4. **EOI broadcast** stays as recorded under "Fewer exits"; a separate step if still worthwhile.

**Targets** (interleaved A/B on one image, as for the exit work): depth 1 at one and four vCPUs
+15%, deeper queues and four readers no worse, host CPU per I/O at depth 1 no more than +20%.
Every correctness run passing, including lifecycle reset/disable/S5 with a lone in-flight request.

## Future direct-payload work

Keep immutable request metadata, backend ownership and ordered completion as
separate responsibilities; staging is the only implementation initially. If
measurements justify it, retain the copied baseline and add a disabled experimental
payload mode that relaxes atomicity only, retaining bounds, lifetime, reset and
control-path correctness. It provides an optimistic comparison, not a correctness
claim. A coordinated version needs its own eligible-page/access-lifetime protocol,
including resident helpers, walkers, compiler reads and future aliases/promotions.
A host mutex or existing two-page split lock is insufficient.

Keep rings/control/status copied or otherwise under their checked protocol.
Payload pages overlapping those structures or tracked code/tables are conservative
staging candidates. Page permissions must be emulator shadow permissions, not
edits to guest page tables. Revocation/acknowledgement/refill and contention costs
must be measured against eliminated copies. Partial direct RX effects invalidate
copy-first's no-payload-write-on-error rule; revise it explicitly. Once direct I/O
starts, a timeout is neither permission to reuse its pages nor to retry via staging.

## Review and evidence

The earlier [scope/reuse
review](../research/reviews/INV-03-01-findings.md#io-scope-and-rust-reuse-review--november-7-2026)
and reproduced IO0 fit tests retain their original scope. This consolidated revision was
independently reviewed by one reviewer for runtime, concurrency and lifecycle, and another for
native backends and profile compatibility. Both accept the corrected plan direction within
source evidence. The review added readable-header capture outside parsing, explicit
queue/resource bounds, deferred control MMIO with safe continuation and ordered readback, S5
drain integration, ordered IRQ publication and native offload gates. The main session checked
the QueueReady requirement against the specification and rejected a proposed busy-value
readback; the reviewer accepted the corrected deferred transaction design. The concrete
ABI/checkpoint and RNG continuation are implemented and have the finite development evidence
recorded above; full lifecycle coverage remains open. The initial unprivileged vmnet admission
probe was denied, so a working native network backend is not established. A reviewed plan is not
an implementation-correctness or performance result.
