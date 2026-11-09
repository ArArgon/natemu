# IO0 — rust-vmm queue and transport evaluation

November 7, 2026. The owner authorized this evaluation while keeping broader
rust-vmm adoption an assessment only. **Exploratory fit/gap results**, not
acceptance of a dependency, integrated device, DMA architecture or performance.

## Result

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
as part of this evaluation. The separate [broader assessment](rust-vmm-assessment.md)
maps adoption and possible upstream contributions without committing to either.

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

We have not implemented an adapter over natemu's `GuestRam`, resident admission,
all-vCPU observer exclusion, code/PTE alias handling, stale compiler rejection,
split-atomic interaction, VM-generation cancellation or event wakeups. These
remain IO1 work. Memory address validation and dirty bitmaps cannot supply them.
The single-threaded copy/order probe cannot validate concurrent x86 semantics.

A minimal next implementation would connect one admitted completion path to
the existing MMIO/GSI interfaces, exercise it with a deterministic backend, then
use entropy as the first real consumer. Choose a checked queue extension and a
small explicit MMIO adapter; decide patch versus local register implementation
after counting the actual override work. No production dependency or upstream
submission is selected here.

## Retention, attempts and limits

Sources, lockfile, fetched dependency sources, environment manifest, input copies,
logs and SHA-256 inventory are retained under
[`rust-vmm-io0-20261107-01`](/Users/liaozping/orca/natemu-raw/rust-vmm-io0-20261107-01).
The upstream checkout is unmodified. The probe source is
[`probe/src/lib.rs`](/Users/liaozping/orca/natemu-raw/rust-vmm-io0-20261107-01/probe/src/lib.rs).

| Author attempt | Outcome |
| --- | --- |
| `01-check`: `cargo +1.97.0 check` | PASS, unchanged queue/transport build and initial dependency resolution. |
| `02-probe`: locked offline tests | Harness compile failure: two import paths were wrong. Retained, then corrected; no upstream edit. |
| `03-probe`: locked offline tests | 16/16 PASS. Non-test library compilation emits unused/dead-code warnings for the test helpers. |

Author retained footprint was 128 MiB; the independent copy adds 97 MiB, inside
the 1 GiB bound. No benchmark timings are inferred from build/test durations.
No standalone upstream full-suite, Kani/fuzz campaign,
real device workload, network admission or full block-executor build was run.

## Independent review and replay

Independent `/root/io_scope_review` verified the clean source pin, every original
SHA-256 entry and the successful probe snapshot, then copied the sources/cache
and rebuilt with a fresh target. One locked offline test invocation passed all
16 cases. Together with the author's attempts, this is four build/test invocations.
The [retained review](/Users/liaozping/orca/natemu-raw/rust-vmm-io0-20261107-01/replica-io-scope-review-01/review.md)
accepts the narrow source/API observations and their relevance to selective reuse;
it found no substantive correction. Root subsequently inspected its reproduction
script and report. Preparation was not independently reviewed before replay, so
this remains exploratory replication, not pre-execution-reviewed acceptance
evidence. No additional run is needed for this assessment.

The broader adoption assessment also passed independent source review. Neither
review selects a dependency or establishes resident DMA, reset/drain, concurrent
coherence or integrated architecture correctness. IO0's concrete production
completion contract remains unfinished; IO1 implementation has not begun.

[queue]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-queue/src/queue.rs
[chain]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-queue/src/chain.rs
[mmio]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-device/src/mmio.rs
[status]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-virtio/virtio-device/src/lib.rs
[virtio]: https://docs.oasis-open.org/virtio/virtio/v1.2/virtio-v1.2.html
[memory]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-memory/src/guest_memory.rs
[mmap]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-memory/src/mmap/unix.rs
