# Benchmarking and validation

This track supplies fair baselines and measurement methods for the design
questions in [Track 03](03-architecture-options.md) and estimates in
[Track 04](04-performance-estimation.md). QEMU TCG remains the required baseline.
Work follows the [research workflow](../README.md).

## Measurement choices

Choose the smallest experiment that can change the decision. Capability checks,
microbenchmarks, subsystem tests and integrated experiments are tools, not a
mandatory sequence. Each method states its actual evidence prerequisites,
correctness checks, timing qualification, budget and stopping condition. Design
experiments can run within Track 03 before architecture selection. Full product
implementation retains its separate planning and authorization requirements.
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
1/2/4/8+ vCPU ladder, and compute, memory, fault/syscall, I/O and concurrency
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
