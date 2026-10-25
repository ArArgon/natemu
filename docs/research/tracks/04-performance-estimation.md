# Performance estimation

This track estimates costs and sensitivities for candidates being developed in
[Architecture and implementation direction](03-architecture-options.md), against
the success criteria in [Scope and success](00-scope-and-success.md). Estimates
inform architectural choices before a final selection. Work follows the
[research workflow](../README.md); projections are distinguished from measured
performance.

## Contribution to the ABI-first milestone

Support [Track 03's current milestone](03-architecture-options.md#current-milestone--execution-abi-and-experimental-runtime)
through Q-04-01 and the [implementation plan](../../implementation.md#performance-alongside-construction).
Start with counters/costs of the supported fragment and helper paths, then actual
compilation, dispatch, recovery and shared coordination. Measure during development
when the result changes a choice; boot is not a prerequisite to component costs.
Separate event cost from frequency, cold compilation from warmed execution and
codegen from runtime/backend effects. Explore N-vCPU sensitivity through
coordinator waits, drain latency, queue/backlog and retained memory. Use actual
observations when available and label unknown inputs rather than inventing rates.

The milestone can proceed without a completed whole-emulator cost model.
Q-04-01 still owns full component ranges and scaling analysis; the ABI experiment
does not settle them. Workload-wide or economic comparisons also need Track 00's
extant workload, success and stop criteria. Missing historical scope records must
be resolved by their owners before those criteria are used.

## Estimation rules

Every estimate produced under this track must be a range with an explicit
sensitivity analysis over its inputs, never a point claim. Each input carries its evidence type, applicability and uncertainty under the
[research workflow](../README.md). State which assumptions drive the estimate;
evidence types have no universal ranking. Combining estimates must not
multiply overlapping speedups: cost components that share a mechanism, a
bottleneck, or a workload phase may not be composed as if independent, and
every composition must state its independence argument. Conditional models may explore provisional designs with explicit assumptions.
They cannot establish measured speedup or acceptance of a design whose required
correctness is unresolved. Measurements must qualify the behavior measured under
[Track 03](03-architecture-options.md).

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
