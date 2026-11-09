# rust-vmm reuse for I/O

November 7-8, 2026. One record for whether and how natemu reuses rust-vmm code
for its copy-first virtio I/O. It merges the earlier source-only survey with the
owner-authorized isolated evaluation. Results and the recommendation come first,
then the survey material, then retention, limits and independent review.

The evaluation compiled the pinned crates on Darwin and ran synthetic host-only
tests; it made no production dependency change, ran no natemu VM and measured no
performance. The [workflow](../research/README.md), [current boot
handoff](boot-handoff.md), and device/memory contracts in INV-03-03/04 still
govern integration. The separate [broader assessment](rust-vmm-assessment.md)
maps adoption and possible upstream contributions without committing to either;
this record owns the I/O decisions. The [native-backend
assessment](io-native-backends.md) covers vmnet batching, checksum/segmentation
offload and virtio-header capabilities; it recommends prioritizing a direct vmnet
evaluation without selecting it yet. The plan is in [io-plan.md](io-plan.md).

## Evaluation result

**Observed:** `virtio-queue` 0.18.0, `virtio-device` 0.1.0 and `vm-memory` 0.18.0
with the mmap feature compile unchanged on this M4, macOS26.6.2, SDK26.4,
Rust1.97.0. The source is rust-vmm monorepo commit
`671e367124cf7717d832b21de86b62eccf33ac27`, not a mixture of archived and moving
repositories. The isolated application lockfile resolves all dependencies.
This establishes this build, not all feature combinations or upstream Darwin CI.

**Observed:** 16 synthetic host-only tests pass, including tests deliberately
asserting the gaps below. A passing gap reproduction does not mean that behavior
is suitable for natemu. The permission-window example uses private test RAM;
no natemu VM, resident rendezvous or real device backend is involved.

**Inferred recommendation:** retain the queue as a reuse candidate. Add checked
descriptor traversal and transaction-scoped memory integration. Treat the MMIO
implementation as a reference or a patch candidate; its default register/lifecycle
handling is not ready to use unchanged. Do not migrate unrelated working modules
as part of this evaluation. The [broader assessment](rust-vmm-assessment.md) is background only.

## What worked

- Building the queue/transport and transitive utility/memory dependencies required
  no Darwin source patch. The earlier optional block-executor concern does not
  imply that the core queue's utility dependency fails to build on Darwin.
- A borrowed `GuestMemory` adapter can restrict the operations used by the queue
  to selected read/write spans without replacing the backing allocator. The
  example denies out-of-span/direction/overflow accesses and returns no direct
  physical-memory handle. Snapshot bytes remain owned after the window ends.
- One synthetic completion writes payload, then the used element, then the used
  index; the index store is Release in [upstream source][queue]. High guest
  physical addresses, ring holes, overrun detection and used-index wrap are
  exercised. This checks API behavior, not x86/Arm cross-domain ordering.
- Discovery and the ordinary feature/status sequence work. Full reset calls an
  application hook, whose test implementation resets the configuration.

## Concrete gaps and remedies

| Area | Source-backed behavior, reproduced where stated | Required natemu work |
| --- | --- | --- |
| Malformed chains | [Chain iteration][chain] yields a valid-looking prefix and then `None` for an invalid next index or exhausted cycle budget. `pop_descriptor_chain` also turns an available-ring error into `None`. Reproduced. | Preserve error versus normal exhaustion. Prefer `iter` to convenience pop and a checked traversal extension/adaptation; reject the entire malformed request before effects. Avoid two independent copies of the same traversal logic. |
| Feature enforcement | Chain traversal follows an indirect table without a negotiated-feature argument and yields its inner descriptors. Reproduced without an indirect feature being selected. | Reject unoffered indirect descriptors before traversal follows them. Device-specific direction, total-length, segment and capacity limits remain the caller's responsibility. |
| Snapshot ownership | Descriptors are read lazily; mutating a later descriptor changes what the existing iterator returns. Reproduced sequentially. | Traverse and snapshot under admitted memory authority. Retain owned bytes/typed request state across host work, never the live chain/memory handle. This is deliberate integration work, not an upstream snapshot promise. |
| Partial completion failure | `add_used` writes the ring element and advances its local indices before writing the guest used index. Denying the latter leaves an error, the element and advanced local state. Reproduced. | Admit and pin the complete destination set before any write. Define contained failure for an unexpected commit error; do not retry the whole operation blindly. No rollback is provided by this method. |
| Notification suppression | Without EVENT_IDX, `needs_notification` returns true even with the driver's no-interrupt flag set. Reproduced. | Implement the selected suppression policy in the device owner. Virtio calls suppression a SHOULD and requires handling spurious notifications; this is an efficiency/contract gap, not evidence of corruption. |
| Queue disable | [MMIO queue-field updates][mmio] are refused after DRIVER_OK; writing QueueReady=0 leaves it 1. Reproduced. | Implement the required stop/readback/lifetime protocol while active; drain admitted work before reusing queue state. Optional RING_RESET remains unoffered. |
| Register narrowing | QueueSel=0x10000 selects queue0 and QueueNum=0x10004 becomes4 through `u16` casts. Reproduced. | Validate the full-width values before narrowing. An invalid selector must not alias a real queue. Invalid guest input is containment work even when a conforming driver would not issue it. |
| Required feature subsets | The [generic status handler][status] rejects unoffered bits but accepts FEATURES_OK with no features accepted, even if VERSION_1 is offered. Reproduced. | Apply natemu's selected required-feature policy before accepting FEATURES_OK: VERSION_1 and, for block, SIZE_MAX/SEG_MAX. The library's generic subset check is not this policy. |
| Notifications and IRQs | QueueNotify defaults to a no-op; InterruptACK clears atomic status without an external IRQ callback. ACK status change is reproduced; hook absence is source-backed. | Implement bounded queue work scheduling and synchronize the level GSI with interrupt status, including masking/EOI/reset and new completions racing acknowledgements. |
| Activation and full reset | Activation/reset are application callbacks. The harness deliberately supplies minimal callbacks. | Validate all queue resources before activation; reset must retire work, callback and IRQ references. The harness does not establish those obligations or arbitrary status-transition conformance. |

The [Virtio 1.2 specification][virtio] §§2.7.7 and4.2.2–4.2.3 supplies the
notification and MMIO interpretation. In particular, QueueReady stop/readback
and deasserting the dedicated interrupt according to status are transport
obligations; they are not dependent on implementing optional packed rings or
per-queue reset. The full selected profile remains in INV-03-03/04.

## Planning recommendation (source survey)

**Inferred:** evaluate rust-vmm's `virtio-queue` first, with a small host-side
adapter to natemu's admitted guest-memory snapshots and commits. Consider its
`virtio-device` MMIO helpers after the lifecycle audit below. Keep disk jobs,
coherence, interrupt publication and failure containment in natemu. There is
useful reusable Rust here, but the inspected block executor is not an unchanged
macOS backend for this design.

This changes the next task from writing a virtqueue implementation to checking
one real candidate against natemu's interface. It does not select a complete
external VMM or reopen the chosen modern virtio-MMIO guest interface.

## Production fork assessment — November 8

**Recommendation:** expect a small upstreamable virtio-queue patch for the
selected snapshot/owner design, and keep it in a fork of the current
[rust-vmm/rust-vmm monorepo](https://github.com/rust-vmm/rust-vmm), not the archived
vm-virtio repository. Upstream's README says future development of migrated
crates belongs in the monorepo. No fork, patch, dependency change or upstream
submission was performed for this assessment.

At the evaluated pin, `DescriptorChain::next` collapses out-of-range indices, budget exhaustion,
memory/address failures, indirect-table errors and length overflow into `None`. It switches to
an indirect table before returning a descriptor, without a negotiated-feature policy. A wrapper
around yielded items alone therefore cannot reject every unnegotiated indirect table before
following it. [Pinned chain
implementation](https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-queue/src/chain.rs)

The proposed patch is an additive checked traversal interface that:

- distinguishes clean end from malformed/truncated chains and returns explicit
  errors rather than a valid-looking prefix;
- checks indirect permission before following a table (initial natemu policy:
  disabled), while retaining bounded traversal/length validation;
- permits traversal from a validated snapshot/head without forcing mutable
  upstream queue ownership. `DescriptorChain::new` is currently crate-private;
  provide a checked public entry point or explicitly use Queue::iter on private
  scratch queue state. Reuse one traversal implementation internally and retain
  existing public behavior where compatibility is required.

A checked iterator can still yield descriptors before a later error; natemu must
collect and validate the whole chain before effects. Captured available-ring
indices/heads should be validated by the existing device-owner snapshot logic:
the pinned AvailIter also hides a head-read error behind iterator termination.
Only add a checked available iterator if we actually reuse that path; it is not
necessary for the selected owner-managed snapshot design.

A fork is **not technically mandatory**: public `desc::split::Descriptor`
accessors allow one small bounded direct-chain walker in natemu. Choose that only
if it is the sole actual walker and less work than adapting upstream; do not
prevalidate with a complete second parser then traverse again through the old
iterator. The preferred patch avoids owning a duplicate generic implementation.

No current requirement calls for modifying vm-memory. Natemu supplies its own
owned-snapshot/admitted-memory adapter, staged backends, coherence and deferred
control lifecycle. Use a local MMIO transport rather than patching numerous
default methods solely to impose natemu ownership. General register narrowing
or queue-disable fixes can be separate upstream work if we later reuse that
transport. The inspected virtio-blk parser/executor is not part of this proposed
dependency set, so its layout/Darwin/full-sync gaps do not expand the initial fork.

Pin the fork revision and keep the diff narrow with upstream-compatible tests;
the synthetic fit harness remains a regression input, not integrated acceptance.
Recheck the chosen upstream base before implementation in case it has gained a
checked interface. Independent source review confirms this scope and the viable
no-fork alternative; no compile/runtime claim follows from the API proposal.

## The main remaining integration work

### What adopting `vm-memory` actually requires

For the pinned queue implementation, consume the existing `vm-memory` crate and
provide a local `GuestMemory` implementation over an admitted view of natemu's
existing RAM. This does not mean reimplementing the crate, replacing natemu's
allocator or making `GuestMemoryMmap` the RAM owner. The generic MMIO register
helpers themselves do not require a RAM argument; queue operations do.

At this revision, the access trait requires `check_range` and `get_slices`, with
associated `PhysicalMemory: GuestMemoryBackend` and `Bitmap` types.
`physical_memory()` may remain the default `None`. A suitable backing/region
representation is still needed; the synthetic harness uses `GuestMemoryMmap`.
The crate also offers non-owning raw mmap wrappers, but any production wrapper
must preserve natemu's mapping lifetime, holes and private-memory exclusions.
These are implementation options, not a selected allocation migration.
See the [pinned memory trait][memory] and [raw mmap wrapper][mmap].

The intended lifetime is an admitted snapshot view, followed by owned host work,
followed by a separately admitted completion view. A trait implementation alone
does not create that admission protocol or prevent trusted native callers from
misusing raw slices.

The test `Window` proves that the current memory trait can express a restricted
view used by these queue operations. It does **not** establish a security boundary:
trusted native code can bypass an internal pointer interface, and returned
volatile slices themselves are not read-only capability types. The production
adapter must only exist within an admitted transaction and must not escape it.

We have not implemented an adapter over natemu's `GuestRam`, resident admission, all-vCPU
observer exclusion, code/PTE alias handling, stale compiler rejection, split-atomic interaction,
VM-generation cancellation or event wakeups. These remain integration implementation work.
Memory address validation and dirty bitmaps cannot supply them. The single-threaded copy/order
probe cannot validate concurrent x86 semantics.

A minimal next implementation would connect one admitted completion path to
the existing MMIO/GSI interfaces, exercise it with a deterministic backend, then
use entropy as the first real consumer. Choose a checked queue extension and a
small explicit MMIO adapter; decide patch versus local register implementation
after counting the actual override work. No production dependency or upstream
submission is selected here.

## Candidate inventory

Versions below are the package versions in the inspected upstream source,
not a claim that every package is published or a selected dependency lock.
The [rust-vmm monorepo README](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/README.md)
lists `vm-virtio` and `vm-memory` as migrated; future development belongs there.
Some detailed source reads below came from the previous repository because the
new URL was unavailable through the browser. Recheck those files at the exact
candidate revision before adoption; this survey does not assert byte identity.

| Candidate | Source-backed contents | Fit and recommendation (inferred) |
| --- | --- | --- |
| `virtio-queue` 0.18.0 | Split virtqueue implementation, descriptor chains, ring iteration and notification decisions. Queue notification transport is supplied by the VMM. [README](https://github.com/rust-vmm/vm-virtio/blob/main/virtio-queue/README.md), [manifest](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-virtio/virtio-queue/Cargo.toml). | Best first reuse candidate: it implements the selected ring format without imposing KVM IRQ delivery. Retain natemu's feature/resource limits and memory authority. |
| `virtio-device` 0.1.0 | Common device status/features and MMIO v2 read/write helpers, with application activation/reset callbacks. [manifest](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-virtio/virtio-device/Cargo.toml), [traits](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-virtio/virtio-device/src/lib.rs). | Potential transport reuse. The default methods are not a complete device and need the specific lifecycle/IRQ review below. |
| `virtio-blk` 0.1.0 | Request parser plus optional `backend-stdio` executor. [manifest](https://raw.githubusercontent.com/rust-vmm/vm-virtio/main/virtio-blk/Cargo.toml), [module split](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-virtio/virtio-blk/src/lib.rs). | Selective reuse/reference; neither parser coverage nor executor semantics match natemu unchanged. Do not choose the entire executor just because its interface accepts a file. |
| `vm-memory` 0.18.0 | Guest-address/memory-access traits, optional mapping/atomic/bitmap backends. The core traits separate accessing memory from owning its allocation. [manifest](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-memory/Cargo.toml), [memory interface](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-memory/src/guest_memory.rs). | Implement a narrow access adapter over natemu-owned memory; replacing natemu's RAM allocation is unnecessary. The adapter must be scoped to an admitted transaction, not globally available to backend workers. |

## Integration traps found in source

**Queue traversal is not natemu's whole validation policy.** In the inspected
[`DescriptorChain`](https://raw.githubusercontent.com/rust-vmm/vm-virtio/main/virtio-queue/src/chain.rs),
iteration retains a memory handle and reads descriptors as it advances. It bounds traversal, but
malformed traversal can end with `None`; it also follows indirect descriptors without receiving
natemu's negotiated-feature mask. Admission must enforce unoffered indirect descriptors,
complete-chain validity, bounds and directions, then retain an owned request snapshot rather
than a live iterator across asynchronous work. These are adapter obligations, not findings that
the library promises to perform them and fails.

**MMIO needs real notification and reset integration.** The inspected
[`VirtioMmioDevice`](https://raw.githubusercontent.com/rust-vmm/vm-virtio/main/virtio-device/src/mmio.rs)
defaults `queue_notify` to doing nothing. IRQ acknowledgement clears its atomic status but does
not drive natemu's GSI line. Its common queue-field helper allows changes only before
`DRIVER_OK`, including `QueueReady`; natemu must support the selected queue-disable/reset
lifecycle. Queue selector/size writes also narrow to `u16`. Audit or override these paths, and
define invalid register behavior. The shared [device-status
implementation](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-virtio/virtio-device/src/lib.rs)
calls application `activate`/`reset`; their success alone cannot establish that old callbacks,
RAM commits or interrupts have retired.

**The block parser has a narrower layout assumption.** Its [`Request::parse`
documentation](https://raw.githubusercontent.com/rust-vmm/vm-virtio/main/virtio-blk/src/request.rs)
requires one descriptor for the header, separate data descriptors and one for status. Natemu's
[accepted
profile](../research/investigations/INV-03-04.md#concrete-linux-block-feature-profile-and-negotiated-path-coverage)
explicitly retains valid requests that combine/split these elements. Therefore using this parser
unchanged would narrow the contract. A bounded parser adapter or upstream change is needed;
compare its size with using the queue library and parsing the resulting readable/writable byte
streams directly.

**The block executor bypasses the required staged-I/O protocol.** The inspected
[`StdIoBackend`](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-virtio/virtio-blk/src/stdio_executor.rs)
seeks and transfers between the file and guest memory during execution, writes status itself,
and implements FLUSH with `FileSync::fsync`. It does not supply natemu's immutable outgoing
snapshot, private incoming staging, admitted commit or `F_FULLFSYNC` durability contract. It
also imports `write_zeroes` unconditionally when enabled; that module is in the [Linux-specific
utility exports](https://raw.githubusercontent.com/rust-vmm/vmm-sys-util/main/src/linux/mod.rs),
whose enclosing module is
[Linux/Android-gated](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vmm-sys-util/src/lib.rs).
Thus source inspection identifies a macOS portability obstacle for this optional executor,
independently of its memory-contract mismatch. No build was attempted.

**Memory safety helpers do not supply emulated-CPU coherence.** `vm-memory` tracks
addressability and exposes volatile/atomic access; its [memory-map
snapshot](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-memory/src/guest_memory.rs)
is not a coherent snapshot of guest data bytes. Natemu must still coordinate source/PTE aliases,
pending compilation and split-atomic observers. Its current
[`guest_ram_written`](../../crates/host/src/execution.rs) requires every vCPU stopped. A stock
`GuestMemoryMmap` handed to an asynchronous device worker would not satisfy the new transaction
contract.

## Dependencies, licensing and portability limits

The [workspace versions](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/Cargo.toml)
include `virtio-bindings` 0.2.7 and `vmm-sys-util` 0.15.0. Queue reuse brings `vm-memory`,
`vmm-sys-util`, `virtio-bindings` and `log`; `vm-memory` also brings `thiserror` and optional
dependencies selected by its features. Avoid enabling mapping/Xen/IOMMU features solely because
examples use them. Core source exposes 64-bit and platform feature guards, not a blanket
Linux-only restriction ([vm-memory
exports](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vm-memory/src/lib.rs),
[utility
exports](https://raw.githubusercontent.com/rust-vmm/rust-vmm/main/vmm-sys-util/src/lib.rs)).
This makes a Darwin compile probe worthwhile; it does not establish supported macOS CI,
successful compilation or correct execution.

The queue manifest says **Apache-2.0 AND BSD-3-Clause**; vm-memory says
**Apache-2.0 OR BSD-3-Clause**. The device/block manifests say
**Apache-2.0 OR MIT**, while inspected source headers say
**Apache-2.0 OR BSD-3-Clause**. Preserve per-file notices and resolve that
metadata discrepancy for the selected revision; this is a licensing integration
warning, not a technical rejection.

Natemu's [Cargo policy](../../Cargo.toml) currently permits registry dependencies
for host binaries while keeping the host library, ABI and resident free of them.
Adopting these crates in the host library changes that recorded policy. The
planning recommendation is to allow narrowly selected, pinned host-only I/O
dependencies while preserving the dependency-free resident/ABI; this is a proposed
policy choice, not an edit made by this survey.

## Smallest evaluation slice

Before implementing disk/network backends, pin one compatible rust-vmm revision
or published version set and check it builds on the current Darwin toolchain.
Then exercise one split queue with a transaction-scoped memory adapter: snapshot
a request, drop memory authority, produce private bytes, reacquire authority,
publish payload/used state, and signal/deassert the existing GSI. Use the intended
entropy device as the first integrated consumer and a deterministic backend for
fault cases. Check invalid/indirect chains, queue-disable/full reset, pending
completion after reset, descriptor mutation and data/code aliases. Stop at that
one completion contract; expand to block only after it fits without exposing
guest-backed buffers to host I/O. This is the proposed next evaluation, not work
performed here.

## Networking and complete-device candidates

The author inspected these additional primary sources. These are source-backed
capabilities followed by inferred fit, not measured rankings.

| Candidate | What exists | Natemu assessment |
| --- | --- | --- |
| [libkrun](https://github.com/libkrun/libkrun) | Rust VMM with macOS/ARM64 HVF support and block/net/rng devices. Its main branch is an evolving 2.0 API; upstream recommends stable release branches for production. | Best macOS-oriented reference/extraction candidate. Importing its complete VMM is outside this task; its device package is coupled to other libkrun modules and its RAM/lifecycle interfaces need changes. |
| [`vmnet` 0.7.0](https://docs.rs/crate/vmnet/0.7.0) / [upstream](https://github.com/edigaryev/vmnet) | MIT Rust bindings to Apple's vmnet.framework, including Shared NAT, packet reads and event callbacks. The release is dated September 28, 2026; upstream assumes macOS26+ and requires the networking entitlement or elevated privilege. | Strong candidate to evaluate the already-recorded vmnet alternative. Keep native callbacks outside guest RAM and use bounded owned packet buffers. No entitlement, launch or protocol conformance was tested. Selecting it would change the initial adapted-libslirp direction and requires that tradeoff to be recorded. |
| [`libslirp` 4.3.2](https://docs.rs/crate/libslirp/4.3.2/source/Cargo.toml) | Rust wrapper over the C library through `libslirp-sys` 4.2.1. The optional helper enables systemd/D-Bus dependencies; those are separate from the core wrapper. | Useful FFI reuse for the existing direction, not an all-Rust network stack. Evaluate the core wrapper with selected features; do not import the Linux-oriented helper. The recorded ICMP/DNS/IPv6 and containment adaptations remain work. |
| [smoltcp](https://github.com/smoltcp-rs/smoltcp) | Pure Rust TCP/IP stack with documented protocol coverage and omissions. | A stack building block, not a ready replacement for a VM NAT/backend service. Using it would add host-socket proxying, DHCP/DNS service and lifecycle work. No evidence in this survey justifies preferring that extra implementation for the first milestone. |

**Concrete libkrun adaptation cost:** the inspected [device
package](https://raw.githubusercontent.com/libkrun/libkrun/main/src/devices/Cargo.toml) is
`krun-devices` `0.1.0-2.0.0-dev`, Apache-2.0, with dependencies on libkrun `arch`, `utils`,
`polly`, macOS `hvf`, `vm-memory` and `imago`. Its [network
device](https://raw.githubusercontent.com/libkrun/libkrun/main/src/devices/src/virtio/net/device.rs)
passes `GuestMemoryMmap` into its worker, adds EVENT_IDX to advertised features, and returns
`false` from `reset` on non-Windows builds. Those inspected paths cannot be imported unchanged
into natemu's restricted feature set, admitted DMA and required reset contract. This is a
specific interface mismatch, not a claim that libkrun is incorrect for its own lifecycle.
Reinspect a pinned stable source before extraction; cached moving-branch directory listings and
raw files were not consistently retrievable.

Libkrun's [krunkit usage](https://github.com/libkrun/krunkit/blob/main/docs/usage.md)
also demonstrates the useful separation between virtio-net and a Unix-socket
packet backend, with integrations for gvproxy, passt and vmnet helpers. That is
an alternative packaging seam, not proof these external backends meet natemu's
full protocol/authority contract or that they are written in Rust. TSI is not
the selected ordinary virtio-net interface for unmodified stock guests.

[Softnet](https://github.com/openai/softnet) is another Rust/macOS reference around
vmnet, but its documented privileged setup, DHCP system changes and packet policy
make the smaller vmnet crate the first binding candidate here. No helper was
installed and no host network configuration was changed.

## Effect on the phase plan

See [the I/O scope and sequence](io-plan.md). The recommended first decision is
**which existing queue/transport code fits**, followed by coherent completion,
entropy, persistent block, and basic network integration. Keep the selected
guest device profiles and current serial/APIC/timer code. The network backend
remains a separate choice: evaluate vmnet access/capability alongside the work
needed by the existing libslirp direction before selecting it. Source familiarity
and Rust implementation language alone do not establish a better backend.

The GitHub connector returned an unavailable-tool error; primary upstream web
pages and published crate source were used instead. No build, platform admission,
runtime test, installation, dependency change or performance comparison was made.

## Retention, attempts and limits

Sources, lockfile, fetched dependency sources, environment manifest, input copies,
logs and SHA-256 inventory are retained under
`natemu-raw/rust-vmm-io0-20261107-01`.
The upstream checkout is unmodified. The probe source is
`natemu-raw/rust-vmm-io0-20261107-01/probe/src/lib.rs`.

| Author attempt | Outcome |
| --- | --- |
| Check: `cargo +1.97.0 check` | PASS, unchanged queue/transport build and initial dependency resolution. |
| Probe, first try: locked offline tests | Harness compile failure: two import paths were wrong. Retained, then corrected; no upstream edit. |
| Probe, second try: locked offline tests | 16/16 PASS. Non-test library compilation emits unused/dead-code warnings for the test helpers. |

Author retained footprint was 128 MiB; the independent copy adds 97 MiB, inside
the 1 GiB bound. No benchmark timings are inferred from build/test durations.
No standalone upstream full-suite, Kani/fuzz campaign,
real device workload, network admission or full block-executor build was run.

## Independent review and replay

An independent reviewer verified the clean source pin, every original
SHA-256 entry and the successful probe snapshot, then copied the sources/cache
and rebuilt with a fresh target. One locked offline test invocation passed all
16 cases. Together with the author's attempts, this is four build/test invocations.
The retained review (`natemu-raw/rust-vmm-io0-20261107-01/replica-io-scope-review-01/review.md`)
accepts the narrow source/API observations and their relevance to selective reuse;
it found no substantive correction. The author subsequently inspected its reproduction
script and report. Preparation was not independently reviewed before replay, so
this remains exploratory replication, not pre-execution-reviewed acceptance
evidence. No additional run is needed for this assessment.

The broader adoption assessment also passed independent source review. Neither
review selects a dependency or establishes resident DMA, reset/drain, concurrent
coherence or integrated architecture correctness. The concrete production
completion contract remains unfinished; integration implementation had not begun
when this was written.

[queue]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-queue/src/queue.rs
[chain]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-queue/src/chain.rs
[mmio]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-device/src/mmio.rs
[status]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-device/src/lib.rs
[virtio]: https://docs.oasis-open.org/virtio/virtio/v1.2/virtio-v1.2.html
[memory]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-memory/src/guest_memory.rs
[mmap]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-memory/src/mmap/unix.rs
