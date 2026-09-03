# Benchmarking and validation

This track is a roadmap only. It defines how separately authorized research
must measure the surviving options carried forward by
[Architecture options](03-architecture-options.md) — the same set
[Performance estimation](04-performance-estimation.md) estimates — against
the fair baselines defined by
[Scope and success](00-scope-and-success.md), QEMU TCG required among them;
it records no findings and no results. Work on this track runs only under
separate research authorization and follows the
[research governance](../README.md), the
[evidence policy](../evidence-policy.md), and the
[executor guide](../executor-guide.md).

## Measurement progression

Measurement proceeds in order, and each stage unlocks the next: a mechanism
may be microbenchmarked only after its capability check is accepted; a
subsystem benchmark may start only with accepted microbenchmark evidence for
every mechanism the subsystem combines; an integrated prototype only when every
subsystem it spans has accepted subsystem evidence; and an end-to-end
workload only when every mechanism the measured configuration exercises has
accepted evidence at all prior stages. Each stage
becomes questions in the [question backlog](../question-backlog.md),
resolved with labelled evidence through the applicable gates (see
[gate completion](../evidence-policy.md#gate-completion)):

1. **Capability checks.** Whether a mechanism works at all under the
   approved experiment code, before any timing of it is meaningful.
2. **Microbenchmarks.** Isolated costs of single mechanisms, matched to the
   cost components in
   [Performance estimation](04-performance-estimation.md).
3. **Subsystem benchmarks.** Combined costs of one subsystem — for example
   memory, faults, or interrupt delivery — under controlled load.
4. **Integrated prototypes.** Mechanisms combined across subsystems, extending
   the feasibility prototype from [Feasibility](01-feasibility.md) with
   measurement under an approved method.
5. **End-to-end workloads.** The diverse workload set from Scope and
   success, run against the fair baselines under identical conditions.

## Required coverage

Every reported comparison must cover: at least two host machines spanning at
least two Apple Silicon generations, and at least two guest configurations
from the supported envelope; a vCPU ladder of
1, 2, 4, and 8+ vCPUs; and workload classes spanning compute, memory,
fault- and syscall-heavy, I/O, and concurrency-dominated behavior, so no
single benchmark-shaped workload carries a conclusion.

Host machines need not be available at the same time. Matrix coverage may be
collected sequentially, one machine at a time, provided each run's manifest
identifies its machine and OS and every comparison is made within one machine
under identical conditions; cross-machine numbers are never compared
directly.

## Result validity

Every reported result must record: randomized repetition order with enough
repetitions to state uncertainty; documented experimental controls,
including host state and disclosed tuning on both sides of a comparison;
uncertainty intervals, never bare means; a raw-data manifest referencing
immutable external raw artifacts, as required by the
[evidence policy](../evidence-policy.md); and the correctness status of the
measured configuration per [Correctness](02-correctness.md) — a result from
a configuration with an unresolved or failing correctness status cannot
support a performance conclusion.

## Exit criteria

The track is complete when each stage has accepted evidence across the
required coverage, results are mapped to the "materially better" definition
and stop conditions from Scope and success, the end-of-track breadth pass is
recorded, and all surfaced questions are filed in the question backlog. The
results feed [Research synthesis](06-research-synthesis.md); they decide
nothing by themselves.
