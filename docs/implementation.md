# Emulator implementation plan

Grow one working emulator through bounded, tested increments. The owner decisions
of October 25, 2026 are: Rust for most new code, host-side FEX first, ABI and
producer/runtime development together, early performance feedback, and configurable
N-vCPU design with progressively wider tests. These are implementation
directions. I1–I4 and the post-I4 validation pass now
have independently reproduced finite working paths; this does not establish
general correctness or superiority over QEMU.

This is the implementation order and setup entry point. The
[workflow](research/README.md) governs development and evidence; [INV-03-01](research/investigations/INV-03-01.md#current-direction--build-first-october-25-2026)
owns current results, blockers and the next assignment. Its
[decision inventory](research/investigations/INV-03-01.md#current-design-choices-and-evidence-ownership)
supplies accepted contracts when a slice needs them. Read historical claims only
for a named premise or contradiction.

## Execution and language split

```text
Host side: Rust VM manager + adapted FEX compiler (C++)
    translation request -> ARM64 machine code + explicit metadata
VM: assembly entry/capture gateways + Rust resident runtime
    admit -> execute generated code -> helper/fault/dispatch -> next request
```

FEX itself does not run inside the emulated machine. A pinned FEX fragment is
generated ARM64 bytes, with exact producer revision, x86 input and settings.
Pre-generated fragments bootstrap the interface; they are not a live translation
service. FEX's application runtime is not our system runtime: privilege, paging,
exception and partial-effect obligations must survive its adaptation.

| Part | Initial implementation choice |
| --- | --- |
| Host VM management, loading and orchestration | Rust with `std`; a small checked FFI layer for Hypervisor.framework. |
| Resident dispatch, paging, recovery, events and lifetime | Rust with `no_std`; start with preallocated bounded storage. |
| Entry, exception landing, register capture/restore and system instructions | Small ARM64 assembly routines with Rust wrappers after state has been captured. |
| FEX compiler/allocator/exporter adaptation | Retain C++; use a narrow C-compatible bridge or bounded worker protocol. |
| ABI records | Explicit versioned fixed-width layouts, generated C/assembly views and checked size/alignment/offsets. No Rust-owned collections or references in exchanged records. |
| Devices and tests | Rust for new implementation where useful; retain applicable C libraries, independent models and existing Python tooling. |

Rust supports [macOS ARM64](https://doc.rust-lang.org/rustc/platform-support/apple-darwin.html)
and [bare-metal AArch64](https://doc.rust-lang.org/rustc/platform-support/aarch64-unknown-none.html).
These target contracts do not establish our EL1t bring-up. Pin the toolchain when
the first build is admitted, provide a linker layout/startup/panic path, and check
emitted gateway code. Capture live GPR/vector/flags/control state before ordinary
Rust code can clobber it; `no_std` does not make a function preserve-all.

Use [explicit layouts and calling conventions](https://doc.rust-lang.org/nomicon/ffi.html)
at language seams. Prevent Rust unwinding and C++ exceptions from crossing native
execution/FFI seams; represent expected failures as results. A resident panic must
produce bounded diagnostics and contained failure. Guest RAM accessed by vCPUs or
backends is shared external memory: encapsulate raw access and synchronization;
do not manufacture exclusive Rust references over concurrently modified RAM.

## Executable increments

The rows are priorities and demonstrable results, not whole-phase gates. Work
may overlap on independent paths. Keep one evolving runtime and its regressions;
do not accumulate unrelated throwaway demonstrations as a substitute for integration.

| Increment | Runnable result and acceptance check |
| --- | --- |
| I1 — First execution | Build the Rust host/resident image and assembly gates; publish one actual pinned FEX fragment. Independent expected registers/memory and preserved-state sentinels match. Check malformed object/target rejection and contained failure. A hand-written control may bring up entry first, but cannot satisfy FEX fit. |
| I2 — Live producer loop | A runtime miss requests translation of admitted x86 bytes/context; an actual FEX worker generates a new object; the host validates, publishes and resumes it. Change source/context and reject the stale reply. Exercise cancellation and finite request capacity. This is not replay of a fixed export. |
| I3 — Recovery and paging | Add live helpers, real x86-table-driven shadow misses versus guest faults, allocation-pressure maps with spills/overwritten destinations, partial-effect completion and an admitted FP-status path. Check exact current state and effect counts. Manual maps are permitted for finite bring-up fixtures; they do not satisfy automated exporter coverage. |
| I4 — Concurrent composition | Exercise two vCPUs, then three/four before broad ISA expansion: queued faults, remap/shootdown, source invalidation, pending events and retained references. Force adverse schedules and stalled peers. The same ownership/progress rules apply to every configured participant. |
| I4-V — Integrated validation | Start with executable kernel-address, paging-off and mode-transition slices, including N-vCPU interactions; then deepen memory-model, translation, recovery and lifetime coverage across the integrated producer/runtime. Adapt upstream tests, add independent models and adversarial schedules, and retain reproducible counterexamples. This bounded checkpoint qualifies the tested profile for dependent I5 work; it does not establish whole-emulator correctness. |
| I5 — Boot and small workloads | Select an exact stock kernel and external initramfs with `/init` and small test programs. Add required system semantics, exception delivery/return, startup32, timer/APIC and serial toward uniprocessor then SMP boot. Observe boot milestones and successful userspace outputs; kernel boot alone is not application coverage. |
| I6 — Broader compatibility and devices | Grow instruction coverage and required block/network/entropy/shutdown paths; retain prior tests. Add workloads and device errors as paths become available. Resolve product-specific compatibility/numerical tradeoffs before accepting them. |

I2 and I3 should develop together where faultable code needs producer metadata;
every admitted path must already obey its recovery contract. Start I4 as soon as
the relevant shared path exists; its label does not postpone all concurrency work
until I3 is complete. Unimplemented instructions and routes reject explicitly.
Before executing a wider subset, add its semantic and recovery support; do not
turn a valid guest instruction into silent success or an invented guest exception.

The [ABI coverage matrix](research/specs/arm64-execution-abi.md#7-experiment-and-independent-oracle)
is the minimum retained behavior target, reached incrementally. New producer and
N-vCPU cases extend it. Passing some cells reports partial coverage, not milestone
completion or universal ABI compatibility. The ABI representation may change
alongside consumers; state ownership, precise effects, isolation, event eligibility
and lifetime guarantees remain explicit. Reconsider FEX for a demonstrated
compatibility, recovery, cost or maintenance problem, not a hypothetical second adapter.

## Post-I4 integrated validation

I4-V is the first deep integrated validation pass. Add regression tests as each
I4 mechanism is built; this pass systematically combines them before relying on
them for boot. Record its selected coverage, work/resource bound and stopping
condition in INV-03-01 at execution start. Independent I5 preparation may overlap;
defects block the paths that depend on them. Continue expanding conformance tests
during I5/I6 rather than waiting for an exhaustive architecture proof.

### Early address-space and execution-mode slices

Make kernel-space addresses, paging-off execution and mode transitions explicit
early I4-V work. They extend I3's memory/recovery paths and I4's coordination
paths; their absence from the completed finite profiles is not evidence that
userspace assumptions are safe for a system emulator. Implement the missing
paths alongside their tests, before dependent I5 boot execution. Full kernel
startup, general exception delivery and devices remain I5 work.

Keep guest privilege, address range and paging mode separate. A high address
does not grant supervisor access; kernel code can use low addresses too. Paging
off bypasses the guest x86 page-table walk, while the native ARM MMU and guest
physical ownership checks remain active. Follow the existing
[mode/permission contract](research/investigations/INV-03-04.md#mode-permission-and-cache-publication-composition)
and [ABI paging interface](research/specs/arm64-execution-abi.md#paging-interface).

| Slice | Executable behavior and decisive checks | Concurrent extension |
| --- | --- | --- |
| V1 — Kernel-space addresses | Execute actual translated code with upper-canonical guest RIP and high-address data/stack operands; guest RIP remains distinct from the native code-cache address. Exercise native upper-address shadow fills, low/high aliases of the same GPA, CPL0/CPL3 permissions, CR0.WP, NX/SMEP and precise fault addresses. Reject noncanonical/private-gap and invalid boundary-spanning accesses with their proper fault/effect behavior. Audit FEX decode, lookup, helper and address formation assumptions for high bits or accidental host-pointer use. | Repeat remap/shootdown and source invalidation through low/high aliases while another CPU retains a fault/continuation or code reference. Delay acknowledgements; check old-route completion, new accesses using the current mapping and no stale code entry or early reuse. |
| V2 — Paging-off execution | Run an admitted protected-mode 32-bit fragment with guest paging disabled through the live producer/runtime. Apply required segmentation, address-size/wrap and A20 policy, then resolve linear-to-guest-physical identity routing into owned RAM or bounded device/hole service fixtures. Check fetch/read/write, private-backing exclusion, exact effects and fault classification; translation must not read guest page tables, fabricate PTE A/D updates or invent paging faults. A missing native mapping may still need internal repair. | Run paged and paging-off CPUs together, including aliases to shared physical code/data. Interleave a retained access, source write and translation request; preserve per-CPU mode/context identity and reject stale compiler replies. Mapping/code retirement and capacity rules apply to both paths. |
| V3 — Mode and context transitions | Execute legal bounded guest control sequences through the semantic adapter/runtime: paging off/on, selected legacy32/PAE/IA-32e transitions, CR3 switches and same-value reloads, and relevant permission/privilege changes. Check CR0/CR4/EFER and required segment state together, including instruction/address/stack widths and the next fetch. Validate before committing illegal transitions and preserve their exact exception and permitted prior effects. For legacy PAE, check hidden PDPTE load/reload lifetime separately from shadow eviction/INVLPG. | Transition one CPU while peers retain old-context routes/code or compile old work. Check source/context generations, required shadow invalidation, stale reply/entry rejection and retained-object lifetime. A local CR3/CPL/mode change must not change a peer's architectural state; shared backing/source changes and guest-requested shootdowns still receive their required coordination. |

Bring each slice up at N=1, then run targeted N=2 and combined N=3/4 schedules
before accepting it. Force pauses before/after transition commit, native
invalidation acknowledgement, publication and reference release. Combine these
with queued faults, pending-event eligibility, a delayed peer and saturated
request storage; preserve the at-most-eight-head ordinary-work fairness rule.
Check guest-visible state, table bytes, exact fault packets/effect counts and
progress, including stale native mapping and mode/context-cache reuse attempts.
A host-only walker or state-machine test supplements actual generated execution;
it cannot satisfy these slice acceptance checks.

Make the concurrent cases concrete in each slice's acceptance matrix:

- **V1:** cover instruction fetch, ordinary data and stack accesses separately.
  Suspend a generated stack access after an earlier instruction has committed,
  remap or revoke its backing from a peer, then resume and check the permitted
  prefix, RSP, fault and exactly-once effects. Low/high alias tests must include
  differing parent/leaf permissions as well as shared permissions; equal physical
  backing does not justify copying one alias's access decision to the other.
- **V2:** use the same physical code/data through paged and paging-off CPUs while
  delaying a compiler reply and a retained access. Check each CPU's fetch and
  access rules independently, shared-source invalidation, and absence of implicit
  guest page-table walk/A/D effects on the paging-off path, including native
  miss/retry. Deliberate guest data writes to table backing remain ordinary writes.
- **V3:** record the old state, commit or rejection boundary and first subsequent
  fetch/access for each legal or illegal control sequence. Pause old compilation
  across that point, force an old-context cache lookup, and check peer state and
  pending-event eligibility. Include a transition back to a previously used
  mode/address-space identity so stale work cannot pass merely because numerical
  control values match again. Declare the permitted outcome of retained work
  on each side of commit before executing the schedule.

For each case record the address/mode/privilege profile, initial state, forced
interleaving, expected effects or fault, tested N and actual producer/runtime
path. Load-only concurrency does not discharge stack coverage, and host-side
context-field mutation does not discharge guest control-instruction execution.

Build V1/V2 first where independent, then V3 and their combined schedules. Record
the exact supported modes/control sequences, vectors, oracles and per-slice bounds
in INV-03-01 before execution. Unsupported cases reject explicitly and remain
visible obligations. The early I4-V coverage matrix must include all three slices;
a budget stop or an unimplemented slice reports partial coverage, not I4-V
completion. This does not reopen the scoped I3/I4 acceptance or make full boot a
validation prerequisite. Independent I5 preparation can continue throughout.

Use many targeted cases and generated variations, with coverage measured by
distinct contracts and interactions rather than a test-count quota. Keep a fast
deterministic regression subset and a broader bounded campaign. Select cases
from the ABI coverage matrix and current decision inventory, including:

| Area | Required validation focus within the admitted profile |
| --- | --- |
| Memory ordering and atomicity | Guest x86 ordering litmus tests, supported atomics/fences, alignment and page-boundary behavior; combine with faults and retries to detect duplicated or reordered effects. Separately check native synchronization/publication contracts. |
| Translation coherence | Leaf and parent table mutations, aliases/shared tables, permissions, A/D updates and clearing, supported page sizes, remap/shootdown and delayed acknowledgements. Check guest-visible table bytes and allowed translation behavior across invalidation boundaries. |
| Recovery and lifetime | Fault/helper/partial-effect state, suspended accesses during mutation, stale compiler replies and code entry, retained maps/code and reclamation. Force interruptions at supported effect and publication phases. |
| Progress and containment | N=1/2/3/4 as admitted, simultaneous faults, delayed peers, queue/pool exhaustion and pending events. Check eventual progress under declared healthy scheduling and bounded diagnostics/containment on failure. |

Keep semantic tests independent of shadow-table layout, dirty-tracking strategy,
native register allocation and other replaceable mechanisms. Check guest-visible
state, effects, faults, ordering and lifetime guarantees. Label exact native-byte,
spill-location and internal fault-count checks as implementation/profile checks;
update those when an implementation changes without weakening semantic coverage.

### Reuse existing tests and author the missing interactions

Start with the retained pinned FEX checkout; select small cases that the admitted
profile can execute. These are candidate sources, not suites already imported or
passed:

| Source | Intended adaptation and limits |
| --- | --- |
| [FEX assembly tests](https://github.com/FEX-Emu/FEX/tree/48d71752e2a363d5535181f803b0b0e9666c1825/unittests/ASM) | Reuse instruction sequences and expected register/memory results for supported instructions, flags and FP behavior. Adapt setup/termination to our runner. FEX's own execution is not an independent oracle for our FEX-based producer, and these cases do not establish system paging or ABI recovery. |
| [QEMU TCG tests](https://github.com/qemu/qemu/tree/master/tests/tcg) | Select x86 regression cases for a second corpus; identify syscall, library and system-mode harness requirements before adapting. Defer cases needing unimplemented services explicitly. |
| [kvm-unit-tests x86 access tests](https://github.com/kvm-unit-tests/kvm-unit-tests/blob/master/x86/access.c) | Adapt selected permission, paging and fault cases into finite fixtures. Full privileged test execution needs system/exception support; do not make its complete harness a prerequisite to pre-boot validation. |
| [herdtools7](https://github.com/herd/herdtools7) | Use model-checked litmus expectations and generated cases for guest x86 ordering and, separately, native Arm synchronization. Pin the model and its applicability; Linux's kernel memory model is not the x86 architectural model. |

For each adapted case retain upstream revision/path, applicable license notices
and attribution, original expectation, adaptation rationale and required guest
features. Pin sources before importing; preserve meaningful checks and mark
unsupported cases as skipped/unimplemented, never passed. An upstream harness
assumption must not silently become an emulator requirement or a weakened test.

Author our ABI-specific fault, mutation, event and lifetime scenarios alongside
these imports. Use independently derived expectations and small reference/state
models; compare defined outcomes with native x86 where available and a pinned
QEMU configuration as supporting evidence. Mask only architecturally undefined
results under an explicit guest profile, and investigate disagreements rather
than treating either emulator as authoritative. Keep test oracles separate from
implementation helpers, and include deliberately corrupted outputs to check that
the validators can detect violations.

Combine controlled interleavings with seeded randomized schedules and stress.
Retain seeds, traces, source/build/model pins and minimized failing cases under
the durable raw root; rerunning until a failure disappears is not acceptance.
Stress passing cannot prove absence of a forbidden outcome: review synchronization
and lifetime arguments alongside model results and real generated-code execution.

Exit with a contract-to-test matrix recording passed, failed, unimplemented and
untested cells, tested N and configurations, reproducible defects, and independent
review/replication of load-bearing claims. Fix correctness failures in the profile
needed by the next increment; retain unrelated future obligations explicitly.
Reaching the time/resource bound reports partial coverage and a next bounded
slice, not a pass. No new global research gate or universal correctness claim is
introduced by I4-V.

## N-vCPU design and scaling

Use configured N throughout identifiers, acknowledgement sets, queues, per-vCPU
frames/stacks and reference accounting. Admission validates N against actual
host/API/resource limits and reserves required capacity before execution. Fixed
pool sizes are declared profile limits, not an implicit two-vCPU architecture.
Never assume one peer, two-bit ownership or simultaneous host scheduling.

Use one CPU for bring-up, two for initial interleavings, then three/four to combine
mutation, retained continuation and new entry/publication. Include a delayed peer,
multiple pending faults, queue/capacity exhaustion and a reference retained after
invalidation. Check that every participant eventually progresses under healthy
scheduling and that no acknowledgement precedes its covered effects. A host exit
is not a quiescence acknowledgement. Report tested counts separately from the
configured maximum; unsupported counts must fail admission.

Expand performance runs through 1/2/4/8/... within supported host/resource bounds.
Reserve scheduling headroom for host compilation, devices and macOS; a core-count
cap is not a promise of dedicated or equal-speed cores. Measure coordinator wait,
drain latency, queue depth, compilation backlog, retained memory and throughput.
Keep the simple shared coordinator initially; partition it only when measurements
justify the added coordination complexity. Correctness for admitted N is required
throughout; maximum-core tuning is not an upfront prerequisite.

## Performance alongside construction

| Available execution | Useful measurement |
| --- | --- |
| I1 fragment/helper paths | Body, gate and helper costs; spill/reload bytes and native versus host exits. |
| I2 live translation | Compile latency, dispatch, cache reuse and stale/cancelled work. |
| I3/I4 faults and shared paths | Recovery/walk costs, queue/drain behavior, fairness and vCPU scaling. |
| I5 boot and userspace | Repeatable boot milestones, then CPU/memory/syscall workloads; distinguish cold compilation from warmed execution. |
| I6 device paths | Storage/network throughput, latency and error-path behavior. |

Correctness qualifies the timed path. Development counters and timings guide
implementation; accepted comparisons need the [measurement controls](research/tracks/05-benchmarking-and-validation.md)
and independent evidence review. Compare with QEMU under matched guest/workload,
host resources and disclosed cache/build conditions. Attribute codegen, runtime
and I/O costs separately where possible; boot time is not a general compiler score.

A minimal initramfs can run `/init` and test programs without a disk-backed root
filesystem ([Linux documentation](https://docs.kernel.org/filesystems/ramfs-rootfs-initramfs.html)).
This brings application measurements before block/network completion. It does
not remove the project's required device set or establish a diverse workload result.

## Local setup and retained inputs

From the repository root, run:

```sh
./tools/with-env python3 -B tools/check_setup.py
```

This read-only diagnostic checks known local host/Rust prerequisites and the raw
root. It installs nothing and launches no VM or container. Exit 1 identifies
missing prerequisites; exit 0 is not build success, valid entitlements, usable
FEX inputs or runtime correctness. Its own regression checks are:

```sh
python3 -B -m unittest discover -s tools -p 'test_check_setup.py'
```

The host of record is M4/macOS26, with SDK26.4 in inherited evidence. New Rust
builds must record/admit their actual SDK/toolchain; the frozen Track01 checker
retains its exact original SDK requirement. The workspace now contains `crates/host`, `crates/runtime` and `crates/abi`.
Rust 1.97.0 and the bare-metal target are pinned in `rust-toolchain.toml`; Cargo
has no registry dependencies. Assembly offsets come from the shared Rust layout.
I2 uses a versioned 128-byte little-endian register/worker packet, with Rust and
Python validation. FEX consumes its pinned compile-only input format; no direct
Rust/C++ ABI bridge or C header is required by this worker topology.

Ordinary local checks/builds, from the repository root:

```sh
./tools/with-env cargo test --offline --locked
./tools/with-env cargo build --offline --locked --release -p natemu-runtime --target aarch64-unknown-none
python3 -B -m unittest discover -s tools -p 'test_*.py'
```

The general execution path's suites (each runner's docstring gives its options; the slice contracts give what they
check): `tools/run_slice1_vm.py` (fixtures and seeded corpora vs QEMU), `tools/run_slice1_faults.py` (precise faults and
paging cells), `tools/run_slice2_vm.py` (control flow), `tools/run_slice2_faults.py` (loop faults),
`tools/run_slice2_probes.py` (code cache, linking, self-modifying code), `tools/fexc/upstream_tests/run_asm.py` (FEX's
ASM suite) and `tools/run_slice3_vm.py` (N vCPUs: corpora, smp, limits, kicks, system, coherence, sharedpt, litmus,
contain). Each takes `--fexc FEXC --out DIR` and runs under `./tools/with-env`.

**Retired (S3i, October 28, 2026):** every specialized I1–I4/I4-V path. The runtime/abi regression families, their host
drivers and the resident compiler-service gateway are deleted, as are `run_i1.py`, `run_i4v.py`, `run_i4v_qemu.py`, their
checkers, workers, producers, tests and fixtures; the independent models moved to `tools/models/`. The sections
below record what those runs established; their commands no longer run. Coverage of each old check by the general
path, or its retirement reason: [slice-3 contract](restructure/slice3-contract.md) §3, S3a2 and S3h tables.

**Retired (S3a2, October 27, 2026):** the general execution path covers the I1 entry cells, the I2 live-translation
scenarios and the I3 paging/allocated cells, and their host drivers, runner options and I3 worker are deleted
([slice-3 contract](restructure/slice3-contract.md#s3a2-covering-and-deleting-the-i1i3-address-and-stack-paths) maps
every check). `run_i1.py` now runs only the I4 cells below; this section records what those runs established.

The retained development runner snapshots sources, builds and signs the host,
checks the emitted image, then supervises each native cell and runs the independent
oracle. Use a fresh name; existing output directories are refused.

The `--fragment` path in the I4 command below is this machine's durable reproduced input; supply its
actual location when using a different raw root. Its exact producer, extraction,
bytes and manual site maps were in `fixtures/i1/live_load.json` (deleted at S3i). A missing fragment
is an acquisition requirement, not permission to substitute handwritten code.
Use `--snapshot PATH_TO_RETAINED_SOURCE` to rebuild the same checked sources;
invoke that snapshot's runner if its runner/helper versions differ from the
working tree. Runs retain manifests, source/build outputs, disassembly, per-cell
JSON, supervision records and checksums. Build overrides are controlled and
Cargo state/output isolated under the retained run.

**I1 scope:** one immutable object per fresh VM, configured/admitted/tested N=1,
1MiB guest backing and fixed private native mappings. Runtime text/generated code
are RX; private state/stacks and guest data are NX. Startup removes its temporary
low identity alias before admitting guest addresses. Generated code runs EL1t;
exceptions use SP_EL1. Entry/return/exception gates cover all 31 GPRs, 32 Q registers,
SP, NZCV, FPCR and FPSR before ordinary Rust. The 512-byte state bank is a preserved
opaque sentinel region, not a complete x86 architectural state implementation.

The finite descriptor admits exact control/FEX/fault bytes and fixed entry/data
addresses, rejects other profiles/counts/targets, and appends only RET to the
unchanged 44-byte FEX computation. Four ADD inputs cover zero, carry, signed
overflow and ordinary results. Eight malformed object/address cases, an undefined
instruction fault and unsupported N=2 supplement the positive cases. A native
fault stops this VM with captured diagnostics; it does not recover or inject an
invented x86 exception. Each executed cell finishes with one HVC to the host.
This is development coverage of I1, not the full ABI coverage matrix.

### I2 live producer checks

Retired with I1 (above). The runner's I2 scenarios required the retained ARM64 Linux toolchain image identified in
`tools/fex_worker.py` (deleted at S3i) and the exact pinned producer binary. The runner verifies
and copies that binary into each fresh run; the earlier producer run retains its
producing sources/build. Each runtime request supplies fresh x86 bytes to a new
compile-only invocation. New `input.bin`, raw objects, logs and replies remain
associated with that request. Nothing downloads or recompiles FEX during this
command. Docker supplies a host-side development worker, outside the captive VM;
its costs do not represent native-macOS FEX integration.

**I2 contract:** one pending request and one reusable code slot for the admitted
vCPU. A miss snapshots actual owned guest-source bytes at the fixed fixture RIP,
with VM/request/source/context identity. The host exchanges only x0–x15 at the
explicit resident service stop; it never writes mapped VM RAM after boot. Each
worker reply must match both its original request and the current resident request.
The resident also checks the source and exact admitted native shape, copies into
an unreachable RW/NX slot, performs cache/TLB maintenance, changes the slot to RX,
then publishes and enters. Completed state is captured before further service.

Source/context control commands invalidate old entry and pending eligibility.
A replacement request can occupy the slot before a late old reply arrives; that
reply cannot consume the replacement. Cancellation is logical retirement of the
request; its bounded compiler invocation may finish and its late reply is rejected.
The worker is not restarted or retried until a case passes. Capacity exhaustion
returns busy without changing the outstanding request. Cache hits reuse the same
object and authoritative captured guest state without invoking FEX again.

The six scenarios cover ordinary compilation/cache reuse; source-stale and
context-stale deliveries after replacement; cancellation/late delivery; finite
capacity/recovery; and corrupt reply identity/code. They include deliberate
invalid transport injections to check the resident guard separately from normal
host rejection. All request files, packed compiler input, generated body and reply
are cross-checked against the runtime trace. Independent semantic checks cover
all native registers, flags, memory and cross-execution state continuity.

The admitted source family is the original PXOR/ADD/load/SETZ/store fixture and
its POR variant, with finite manual bindings and exact native shape checks. These
are actual compilations, not a fixed-export lookup, but not general translator
coverage. The harness explicitly redispatches the same fixture RIP. Source and
flat-64 context changes are control-plane operations at a resident stop, not
implementation of general x86 self-modification, control registers or paging.
Code replacement occurs only after complete capture, with no retained continuation
into the old slot. Those wider protocols, helper/fault recovery and SMP remain
I3/I4 work.

### I3 recovery, paging and allocated maps

Retired with I1 (above). The finite I3 matrix added 12 paging scenarios and 10 allocated-map/FP/helper/partial-effect scenarios.
The runner snapshots and rebuilds sources, binds both producer binaries, retains
fresh per-request inputs/outputs and supervises the combined execution bundle
for at most20 minutes. Both compiler container families use run-specific cleanup.
Its producer package (now deleted; only the shared
FEX export patch remained, until S3i) recorded exact inputs, adaptation and allocation maps.

**Implemented finite behavior:** the runtime distinguishes missing ARM shadows
from x86 #PF using a four-level, 48-bit virtual/40-bit physical walker, 4KiB/2MiB
leaves, aggregate permissions, WP, NX, SMEP and A/D updates. Read fills stay
read-only until a write walk establishes permission and dirty state. Faults
capture all native state and resume only the faulting instruction. Guest faults
materialize RIP/CR2/error and earlier effects; actual IDT delivery is later work.
Independent checks cover nonpresent/reserved/permission faults, carry/overflow,
source-fetch denial and complete unchanged banks. The typed state catalog covers
GPRs, RIP/RFLAGS, full YMM storage, MXCSR/x87, controls and segment fields.

Fresh FEX compilations now export two35-root maps, with24 real spill slots in a
768-byte frame and an overwritten load destination. The runtime consumes the
actual supplied maps. The finite admission profile compares code/maps with
machine-produced pinned artifacts; it does not accept arbitrary FEX output.
Request-bound chunks transport the1168-byte export without host writes to VM RAM.
Full-register ERET entry and direct-branch return preserve allocated x16/x30 roots;
separate-stack capture preserves spills. This intentionally conservative recovery
profile is not a measured optimal register-allocation policy.

A resident SETZ helper stages overlapping outputs, deliberately clobbers every
native GPR/Q register plus flags/FP controls, then restores the caller and commits
only its declared outputs. The admitted ADDPS case checks RN rounding and native
inexact status becoming guest MXCSR.PE, with caller native FP controls restored.
A separate resident REP MOVSB fixture uses a counted, changed-on-read device:
its second byte-store faults after the read, retains that value, and resumes only
the store. Exact fault-time progress and two reads/two writes are checked.
The REP input is explicitly admitted through the fixture control interface; it
is not FEX-generated REP coverage or a complete device backend.

**Remaining scope:** immutable guest tables during each admitted execution,
aligned same-page generated accesses, one pending object and N=1. General
instruction coverage, paging/control transitions and invalidation, split accesses,
code/route changes during retained operations, concurrent composition, complete
FP/x87 semantics, guest exception delivery, boot and workload performance remain
open. These finite I3 checks do not complete the full ABI milestone or Track03.

### I4 concurrent composition

Retired at S3i (above); the complete finite I4 matrix ran with:

```sh
./tools/with-env python3 -B tools/run_i1.py \
  --name "i4-local-$(date +%Y%m%d-%H%M%S)" \
  --fragment ../natemu-raw/i1-fex-fragment-20261025-02/live_load.arm64 \
  --producer ../natemu-raw/i3-independent-20261025-01/CodeSizeValidation \
  --i4-pressure
```

`--i4-pressure` includes entry, queued faults, remap, live source publication and
pending-event profiles at N=1/2/3/4. Smaller `--i4-entry`, `--i4-faults`,
`--i4-remap`, `--i4-compose` and `--i4-events` options retain earlier applicable
profiles. I4 rejects N=0/5 before
creating the VM. Host/API admission and per-owner backing precede execution.

The pressure profile performs nine protected page-table changes per CPU. Every
head receives all local invalidation acknowledgements before completion or slot
reuse; one rotating peer acknowledges last. At most eight heads precede a real
ordinary FEX interval, granted round-robin to an owner with no pending operation.
Dirty-fault repair completes that admitted route while closure waits. Duplicate
requests at full occupancy preserve the original FIFO identities. This tests
saturation and duplicate rejection, not an independent reachable `FULL` branch.

One resident coordinator serializes maintenance. Ordered ticket handoff prevents
hot polling owners from overtaking waiting peers. Source mutation, stale replies,
new entry and delayed old-reference release share that authority. Code reuse is
checked before overwriting the retired slot. Event tests use a durable identified
level request with repeated notifications, IF/shadow/priority eligibility and
canonical handoff; fixture control changes do not implement architectural shadow
expiry. Tests verify actual generated-code effects and precise captured frames.

**Finite limits:** four admitted CPUs, 1MiB backing per CPU, two shared code slots,
one outstanding maintenance request per CPU, 512 trace records, four recorded
pressure intervals per CPU and 16KiB native stacks. Compiler-backed scenarios
retain a 120-second supervisor bound and a 100-second host cancellation deadline.
Native IRQ/forced-exit delivery, sleeping waits, full APIC/IDT delivery, arbitrary
concurrent guest programs, broad memory-ordering conformance and larger-N scaling
remain later work. I4-V deepens integrated validation before dependent boot work.
The runner's increment-check flag reports execution coverage; independent review
and acceptance remain recorded in INV-03-01.

### I4-V validation runs

Retired at S3i (above). The runs used the pinned I4-V producer (`tools/i4v_producer`, deleted), rather than the
earlier I1/I3 executables, for the new profiles. A focused development check was:

```sh
./tools/with-env python3 -B tools/run_i4v.py \
  --name "i4v-local-$(date +%Y%m%d-%H%M%S)" \
  --producer ../natemu-raw/i4v-producer-20261026-02/build/Bin/CodeSizeValidation \
  --progress --focus progress-stack-n4-seed3
```

The focus selects one native case while retaining the runner's build and unit
checks. It cannot establish full I4-V coverage. Omit `--focus` and use the following
flag groups in separate, sequential runs with unique names for the full native
campaign; each group stays within the existing20-minute bundle bound.

| Flag group | Manifest cells (the 44 address baseline cells each group once repeated, and the `--stack` slice, are retired: S3a2) |
| --- | --- |
| `--progress` | 72 seeded stack/control/coherence/atomic/partial-helper cases. |
| `--containment` | 25 declared failures; each failure also requires a separate fresh-control execution. |
| `--coherence --fence --ordering` | 234 translation, ordering and fence cases. |
| `--aliases --concurrent-stack --mixed-aliases --paging-off --paging-off-steps --mixed-modes --controls --concurrent-controls --stack-boundaries` | 268 alias, stack-composition and mode/control cases. |
| `--address-forms` | 60 protected32 address cases. |

Also run the legacy `run_i1.py --i4-pressure` matrix above
with its original producer pins. Do not run the groups concurrently or combine
all flags into one bundle that exceeds the declared limit. Count repeated baselines and fresh controls
separately from distinct coverage. A bundle's `full_i4v_complete: false` does not
claim aggregate milestone acceptance; that judgment belongs to the
[contract matrix and independent review](research/reviews/INV-03-01-i4v-coverage.md).
The separate QEMU comparison (`fixtures/i4v-qemu`, deleted at S3i) bound passed native
reports and has its own producing snapshot, checksums and bounded execution.

Use `./tools/with-env COMMAND ...` for project builds, checks and runs. It supplies
the durable sibling `../natemu-raw` as `NATEMU_RAW_ROOT`, independently of the
caller's working directory, and preserves a nonempty explicit override. It
creates no directories and changes no parent/global shell settings. Runners and
the diagnostic still enforce their output-root checks. To use tools directly
instead, from the repository root:

```sh
export NATEMU_RAW_ROOT="$PWD/../natemu-raw"
```

If a workspace sandbox reports that this external directory is unwritable, run
the check or retained-output command with authorized external-directory access.
The sibling root is deliberately outside Git; do not substitute temporary
storage merely to avoid the sandbox boundary.

New retained runs use unique identities beneath that root, never `/private/tmp`
as the sole copy. Retain producer pins, dependency inputs, source/build snapshot,
code/metadata bytes, outputs and failures with checksums for experimental evidence.
Ordinary local builds/regressions follow the lighter development workflow.

The existing [FEX exporter](../experiments/track03/codegen-comparison/README.md#historical-preparation)
uses an ARM64 Linux build environment. Recheck exact source/dependency/toolchain
availability; the old temporary export paths are not a dependency cache. Recover
or re-export the pinned fixture and keep the resulting bundle under the durable
root. Do not silently switch upstream revisions to fix acquisition.

Native macOS FEX integration is unestablished. I2 must establish an actual
host-side compiler path. The existing Linux environment may supply a bounded
development worker while a native bridge is brought up; record that topology
and its transport costs explicitly. Such a worker is outside the emulated
machine, and cannot establish native-macOS compilation latency. Pick the smallest
working route during I2; do not postpone the producer loop until the ABI is complete.

## Next assignment and reporting

I4 is complete within its declared finite N=1/2/3/4 profiles, with independent
review and a fresh87-job rebuild including all I1–I3 regressions. The
[completion audit](research/investigations/INV-03-01.md#i4-completed-development-increment--october-26-2026-utc)
records exact evidence and unsupported behavior. Keep legacy I1–I3 N=1 profiles
separate; arbitrary concurrent guest code and the full ABI remain unestablished.

The [I4-V validation pass](#post-i4-integrated-validation) is complete within its
declared finite profiles. Independent final-source rebuilds preserve all 688
preceding native cases and add 72 seeded recovery/progress cases, giving 760
distinct healthy cases. All 25 intentional failure cases and their separate-process
fresh controls pass containment checks, and the 87-job legacy suite passes.
The five native bundles execute 986 cases including repeated baselines and fresh
controls; these repeats do not inflate distinct coverage. Both independent
reviews accept the source/contracts, producing artifacts and complete finite
scope with no unresolved substantive blocker. The separate six-profile
pinned-QEMU comparison also passes fresh independent reproduction with exact
full-flags checks. The
[contract-to-test matrix](research/reviews/INV-03-01-i4v-coverage.md) records
configurations, explicit exclusions and retained evidence.

**Next: native macOS code generation and VM refactoring in parallel.** The
[restructure plan](vm-restructure.md) owns this pass and the orchestrator's
responsibility for overall quality, coherence and engineering judgment. Build
FEX's compiler subset without a Linux VM/container while separating reusable VM
mechanisms from scenario drivers. Connect runnable slices early and develop the
ABI with both consumers; neither a finished refactor nor a complete ABI is a gate.
Existing tests require applicability checks as their mechanisms change, not blind
preservation. I5 starts when its required general execution path is usable.

**I5 (as its execution dependencies become ready), boot and small workloads.** Select an exact stock kernel
and external initramfs containing `/init` and small test programs. Define the
first bounded direct-boot slice using the accepted startup and machine contracts;
implement required semantics, exception delivery/return, startup32, timer/APIC
and serial as the boot path reaches them. Bring up uniprocessor boot and userspace
outputs, then SMP, retaining applicable I1–I4-V regressions throughout. Kernel boot alone
is not application coverage or a code-generator performance verdict.
[INV-03-01](research/investigations/INV-03-01.md#current-direction--build-first-october-25-2026)
owns current results and assignments. No whole-project research phase is a
prerequisite to that next executable slice.

The finite validation does not establish arbitrary guest programs, general
ISA/FP or exception delivery, production devices, larger-N scaling, M2 parity,
performance or whole-emulator correctness. The ABI remains provisional outside
its tested profiles; dependent work must preserve their contracts and add its
required semantics and tests.

For each increment record implemented behavior, passed/failed/untested checks,
tested N, material interface changes, measurements with qualification, and the
next runnable step. Review consequential code/contracts with a separate author;
formal experimental acceptance retains independent review/replication. Fix
editorial errors directly. A substantive blocker must name its consequence,
evidence and smallest closure condition; it blocks only affected work.
