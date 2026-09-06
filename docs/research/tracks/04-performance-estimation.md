# Performance estimation

This track defines how research must estimate, before full measurement,
whether the surviving options carried forward by
[Architecture options](03-architecture-options.md) — its
recommendation, plus any family whose rejection a recorded falsifier has
reopened — can plausibly meet the "materially better" definition from
[Scope and success](00-scope-and-success.md); the track estimates only that
set. It is a roadmap, not a finding, and it makes no performance claims.
Work on it follows the [research workflow](../README.md).

## Estimation rules

Every estimate produced under this track must be a range with an explicit
sensitivity analysis over its inputs, never a point claim. Each input carries
its evidence label, and the estimate inherits the weakest label among its
load-bearing inputs, per the label ordering declared in the
[research workflow](../README.md). Combining estimates must not
multiply overlapping speedups: cost components that share a mechanism, a
bottleneck, or a workload phase may not be composed as if independent, and
every composition must state its independence argument. Estimates from
configurations whose correctness status is unresolved cannot support
conclusions, per the precondition in
[Correctness](02-correctness.md).

## Cost components

The track must model each component separately, for each option under
estimation, before any composition. A component an option family cannot
exhibit is recorded `not-applicable` with a reason rather than silently
skipped, so the component set favors no family in advance. Each component
becomes questions in the
[question backlog](../question-backlog.md), resolved with labelled evidence
through the applicable gates:

- **Translated execution.** The steady-state cost of executing translated
  guest code.
- **Compilation.** The cost of producing translations, including warm-up and
  retranslation.
- **Memory.** Guest memory access costs, including any software-implemented
  or hardware-assisted address translation.
- **Faults and exceptions.** The cost of detecting, classifying, and
  delivering guest faults restartably.
- **Exits.** The cost of leaving and re-entering guest execution for any
  reason.
- **Interrupts and timers.** Delivery latency and throughput for
  asynchronous guest events.
- **Devices and I/O.** The cost of the scoped device set under realistic
  request patterns.
- **Concurrency.** Cross-vCPU coordination, atomics, and memory-ordering
  enforcement costs.

## Scaling questions

Whether and how each component's costs change with scale must be estimated
explicitly, covering: vCPU scaling; heterogeneous host cores and placement;
contention between vCPUs and between the emulator and other host load;
oversubscription of host cores; invalidation storms, including code-cache
and TLB-style invalidation under self-modifying and shared-page workloads;
atomics-heavy workloads; concurrent I/O alongside compute; and memory
pressure on both host and guest.

## Exit criteria

The track is complete when every component and scaling question has an
accepted range with sensitivity analysis or a recorded `not-applicable`,
compositions carry accepted
independence arguments, estimates are mapped to the stop conditions from
Scope and success, the end-of-track breadth pass is recorded, and all
surfaced questions are filed in the question backlog. Estimates guide where
[Benchmarking and validation](05-benchmarking-and-validation.md) spends
effort; they decide nothing by themselves.
