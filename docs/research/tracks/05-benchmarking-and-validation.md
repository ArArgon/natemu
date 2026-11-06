# Benchmarking and validation

This track supplies fair baselines and measurement methods for the design
questions in [Track 03](03-architecture-options.md) and estimates in
[Track 04](04-performance-estimation.md). QEMU TCG remains the required baseline.
Work follows the [research workflow](../README.md).

## Contribution to the ABI-first milestone

Q-05-01 supplies methods and controls for
[Track 03's current milestone](03-architecture-options.md#current-milestone--execution-abi-and-experimental-runtime).
Its next contribution is independent expected outcomes for the next runnable
slice, using the ABI's core T1–T12 and live-producer/Rust/N-vCPU extensions.
Implement adverse cases alongside admitted paths. Two-vCPU bring-up expands to
three/four-vCPU interactions before broad ISA work, using the same growing system.
Keep the method and run bounds with INV03-01 rather than start a separate campaign.

Follow [early measurement](../../implementation.md#performance-alongside-construction):
body/helper checks before boot, compile/dispatch after live integration, faults
and coordination as implemented, then kernel/initramfs userspace workloads.
Measure cold/warm behavior separately. A Linux-based compiler worker's costs do
not establish native-macOS FEX latency. Small initramfs workloads need no disk-backed
root but cannot substitute for device benchmarks or a diverse product portfolio.
Correctness results can stand without qualified timings. Accepted performance
comparisons use the controls below; ordinary diagnostic timings remain provisional.
Q-05-09 owns fair QEMU comparisons as paths become measurable and eventual broader
workload qualification. Its absence does not block unrelated ABI implementation.

## Measurement choices

Choose the smallest experiment that can change the decision. Capability checks,
microbenchmarks, subsystem tests and integrated experiments are tools, not a
mandatory sequence. Each method states its actual evidence prerequisites,
correctness checks, timing qualification, budget and stopping condition. Design
experiments and incremental implementation can run within Track 03 before final
architecture selection, under the current build-first workflow.
The workflow's exploration/decision-evidence distinction applies to every method
below: exploratory probes need no pre-execution review, while experiments used
to settle a choice do. Exploratory results remain provisional.

1. **Capability checks.** Whether the configured mechanism works at all;
   performance conclusions require correctness of the behavior measured.
2. **Microbenchmarks.** Isolated costs of single mechanisms, matched to the
   cost components in
   [Performance estimation](04-performance-estimation.md).
3. **Subsystem benchmarks.** Combined costs of one subsystem — for example
   memory, faults, or interrupt delivery — under controlled load.
4. **Integrated experiments.** Combine mechanisms to test interactions or
   costs. Reuse applicable feasibility evidence; the
   frozen Track 01 program remains retired from development.
5. **End-to-end workloads.** The diverse workload set from Scope and
   success, run against the fair baselines under identical conditions.

## Required coverage

A comparison is reported for the host it ran on; its manifest names the
machine and OS, and both sides of the comparison run on that machine under
identical conditions. Cross-machine numbers are never compared directly.

Select host, guest, vCPU and workload coverage for the claim. M4 is the host of
record; investigate M2/M4 differences when a key feature affects the design.
Broader applicability needs corresponding evidence, not extrapolation from one
host. SMP and scaling claims must cover the concurrency they assert. Product-wide
performance qualification retains at least two guest configurations, the
1/2/4/8+ vCPU ladder within host/API/resource bounds, and compute, memory, fault/syscall, I/O and concurrency
workloads. Research experiments may support narrower claims. Missing cells narrow
the conclusion rather than blocking unrelated architectural work.

## Result validity

Decision-bearing performance results must record: randomized repetition order with enough
repetitions to state uncertainty; documented experimental controls,
including host state and disclosed tuning on both sides of a comparison;
uncertainty intervals, never bare means; a raw-data manifest referencing
immutable external raw artifacts, as required by the
[research workflow](../README.md); and the correctness status of the
behavior measured under [Track 03](03-architecture-options.md). Quarantine
potentially affected results when correctness fails; establish impact, correct
the defect and rerun affected measurements. Exploratory observations use the
workflow's brief record and retained-run rules; logging diagnostic timings does
not make them performance evidence. Correctness-only findings use the controls
and oracles appropriate to their claim, without irrelevant statistical demands.

## Exit criteria

The research measurement work is complete when the measurements needed for the
synthesis decision have accepted results with applicability limits, mapped to
success and stop criteria, and the breadth check is addressed. Explicitly hand
later implementation qualification to planning; this does not establish product
performance before integrated evidence exists. Results feed
[Research synthesis](06-research-synthesis.md).
