# Benchmarking and validation

This track is a roadmap only. It defines how separately authorized research
must measure the surviving options against the fair baselines defined by
[Scope and success](00-scope-and-success.md), QEMU TCG required among them;
it records no findings and no results. Work on this track runs only under
separate research authorization and follows the
[research governance](../README.md), the
[evidence policy](../evidence-policy.md), and the
[executor guide](../executor-guide.md).

## Measurement progression

Measurement proceeds in order; a later stage may not start before the
earlier stage's evidence for the same mechanism is accepted. Each stage
becomes questions in the [question backlog](../question-backlog.md),
resolved with labelled evidence through the applicable gates (see
[gate completion](../evidence-policy.md#gate-completion)):

1. **Capability checks.** Whether a mechanism works at all under the
   approved harness, before any timing of it is meaningful.
2. **Microbenchmarks.** Isolated costs of single mechanisms, matched to the
   cost components in
   [Performance estimation](04-performance-estimation.md).
3. **Subsystem benchmarks.** Combined costs of one subsystem — for example
   memory, faults, or interrupt delivery — under controlled load.
4. **Integrated spikes.** Mechanisms combined across subsystems, extending
   the feasibility spike from [Feasibility](01-feasibility.md) with
   measurement under an approved method.
5. **End-to-end workloads.** The diverse workload set from Scope and
   success, run against the fair baselines under identical conditions.

## Required coverage

Every reported comparison must cover: at least two host machines and at
least two guest configurations from the supported envelope; a vCPU ladder of
1, 2, 4, and 8+ vCPUs; and workload classes spanning compute, memory,
fault- and syscall-heavy, I/O, and concurrency-dominated behavior, so no
single benchmark-shaped workload carries a conclusion.

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
