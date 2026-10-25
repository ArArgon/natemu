# Emulator implementation plan

Grow one working emulator through bounded, tested increments. The owner decisions
of October 25, 2026 are: Rust for most new code, host-side FEX first, ABI and
producer/runtime development together, early performance feedback, and configurable
N-vCPU design with progressively wider tests. These are implementation directions. I1 now has a finite working path; this
does not establish general correctness or superiority over QEMU.

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
The first C consumer/header belongs with the I2 producer bridge.

Ordinary local checks/builds, from the repository root:

```sh
./tools/with-env cargo test --offline --locked
./tools/with-env cargo build --offline --locked --release -p natemu-runtime --target aarch64-unknown-none
python3 -B -m unittest discover -s tools -p 'test_*.py'
python3 -B tools/check_i1.py --self-test
```

The retained development runner snapshots sources, builds and signs the host,
checks the emitted image, then supervises each native cell and runs the independent
oracle. Use a fresh name; existing output directories are refused:

```sh
./tools/with-env python3 -B tools/run_i1.py \
  --name "i1-local-$(date +%Y%m%d-%H%M%S)" \
  --fragment ../natemu-raw/i1-fex-fragment-20261025-02/live_load.arm64
```

The fragment path above is this machine's durable reproduced input; supply its
actual location when using a different raw root. Its exact producer, extraction,
bytes and manual site maps are in `fixtures/i1/live_load.json`. A missing fragment
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

Next is I2: connect a real host-side FEX request/generate/publish/resume loop to
the existing runtime; retain I1 as regression coverage. Changed input/context,
stale replies, cancellation and finite capacity must be observable checks. At assignment
start record a realistic total work/resource bound, checks and stop condition in
INV-03-01; existing direction authorizes routine choices without another approval
round. Stop at a demonstrated slice, its bound, or a concrete blocking failure,
and report the smallest next change. Missing FEX inputs can block its execution
while independent Rust bring-up proceeds. Do not resurrect historical allowances.

For each increment record implemented behavior, passed/failed/untested checks,
tested N, material interface changes, measurements with qualification, and the
next runnable step. Review consequential code/contracts with a separate author;
formal experimental acceptance retains independent review/replication. Fix
editorial errors directly. A substantive blocker must name its consequence,
evidence and smallest closure condition; it blocks only affected work.
