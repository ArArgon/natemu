# BP-00-01-start — initial scope and success breadth pass

## Metadata

- Breadth-pass ID: `BP-00-01-start`
- Primary question: `Q-00-01`
- Date: 2026-09-03
- Executor/session: Codex `/root`, goal thread
- Purpose: bootstrap the question backlog and challenge the initial product
  envelope before resolving who the emulator is for and which workloads must
  represent them.

This is the repository's first breadth pass. The backlog cannot contain the
primary question until this pass seeds it, while the normal filename rule
assumes an existing question. For this bootstrap only, the pass reserves
`Q-00-01`, records that reservation here, and creates the question and all
other surfaced questions atomically. Later passes follow the ordinary
select-question-then-breadth-pass sequence.

This pass organizes questions. It establishes no evidence or conclusion.

## Product assumptions

- The phrase "unmodified modern x86-64 Linux" does not identify kernel
  versions, distributions, userspace ABI, CPU feature profile, boot protocol,
  or device expectations. `Q-00-02` must make this boundary testable.
- The decision question names technical and economic credibility, but the
  roadmap defines no economic threshold. `Q-00-07` must define one before
  synthesis can answer the decision question.
- "Materially better" could mean throughput, latency, startup, resource use,
  compatibility, operational simplicity, or a combination. `Q-00-04` must
  precommit metrics, aggregation, and failure rules before measurement.
- Intended users and jobs-to-be-done are unstated. `Q-00-01` must select a
  diverse workload portfolio based on explicit user classes rather than on
  benchmark convenience.
- UEFI and Windows are analysis-only, but the required depth and decision use
  of that analysis are unstated. `Q-00-09` records the gap.

## Adjacent systems and alternatives

- QEMU TCG is mandatory, but a fair comparison may also require Rosetta-based
  Linux-container workflows, remote x86 machines, or other public-API
  emulators where they solve the same user job. `Q-00-03` must define a
  relevance test rather than assume every adjacent system is comparable.
- Interpretation, static or dynamic translation, and hardware-assisted
  families must be screened under verified host-platform facts before any
  family is preferred. `Q-01-02` owns that feasibility question and
  `Q-03-01` owns the later evidence-based comparison.
- Native arm64 execution is not x86-64 emulation, but can be a useful ceiling
  or diagnostic control. `Q-00-03` must say whether and how such controls are
  reported without presenting them as fair substitutes.

## Negative and boundary cases

- Guests may depend on unsupported CPUID features, MSRs, privileged modes,
  page sizes, self-modifying code, precise exceptions, or device behavior.
  `Q-00-02` must define exclusions and observable rejection behavior;
  `Q-02-01` must define correctness coverage.
- Isolation and crash containment remain required even though broader
  security work is deferred. `Q-00-08` must define the minimum bar and the
  boundary of the deferral.
- Resource ceilings, host memory pressure, oversubscription, heterogeneous
  cores, and background host load may turn an otherwise viable mechanism into
  a product failure. `Q-04-01` and `Q-05-01` own estimation and measurement.
- Licensing or a non-public host dependency can kill an architecture family
  even if it works technically. `Q-01-01` and `Q-01-11` own these falsifiers.

## Host and guest matrix

- Evidence must distinguish Apple Silicon generation, exact macOS and SDK,
  machine memory, performance/efficiency core placement, thermal and power
  state, and entitlement or signing configuration. `Q-01-01` establishes the
  supported host facts and `Q-05-02` establishes measurement coverage.
- Guest dimensions include kernel and distribution, boot path, CPU feature
  profile, memory size, page-table mode, device configuration, and vCPU count.
  `Q-00-02` owns the compatibility boundary; `Q-05-02` owns sampled coverage.
- Platform-sensitive findings apply only to measured host/OS combinations
  until replicated. Any synthesis-wide applicability claim needs the coverage
  required by the research governance.

## Multicore interactions

- SMP is a baseline, so no uniprocessor result can establish product
  feasibility. Host/guest memory-order differences, atomics, shared page-table
  mutation, invalidation, cross-vCPU interrupts, vCPU placement, and
  oversubscription require explicit questions. `Q-01-08`, `Q-02-02`, and
  `Q-05-02` own those layers.
- The mandatory feasibility prototype must combine translation, paging,
  exceptions, interrupts, and at least two concurrently executing vCPUs.
  `Q-01-12` owns the integrated capability question; method and code review
  must pass before it runs.

## Falsifiers and stop paths

- Project-level kills include inability to use only public APIs, inability to
  meet the accepted compatibility or SMP boundary, inability to pass
  correctness gates, performance below the precommitted threshold, economic
  failure, or unavailable required host coverage. `Q-00-06` must turn these
  into checkable stop conditions and map each to evidence.
- Family-level kills must remain distinct from project-level kills so one
  rejected architecture does not silently stop all research. `Q-01-02` and
  `Q-03-01` own the distinction.
- A contradiction or correctness failure reopens dependent findings and
  invalidates affected performance samples. `Q-06-01` must make dependency
  and contradiction handling auditable in synthesis.

## Measurement failure modes

- Benchmark selection, tuning asymmetry, cross-machine comparison, warm-up,
  caching, host scheduling, thermal state, order effects, insufficient
  repetition, bare means, survivor bias, and post-hoc success thresholds can
  all create false performance conclusions. `Q-00-03`, `Q-00-04`, `Q-05-01`,
  and `Q-05-02` divide ownership of those controls.
- An incorrect configuration cannot produce valid performance evidence.
  `Q-02-01` owns the coverage status that every sample must reference.
- A microbenchmark-shaped win may not transfer to representative workloads.
  `Q-00-01` must define workload diversity and `Q-05-01` must enforce staged
  progression from capability through end-to-end measurement.

## Backlog updates

This pass creates the following questions:

- Track 00: `Q-00-01` through `Q-00-09`.
- Track 01: `Q-01-01`, `Q-01-02`, `Q-01-08`, `Q-01-11`, and `Q-01-12`.
- Track 02: `Q-02-01` and `Q-02-02`.
- Track 03: `Q-03-01`.
- Track 04: `Q-04-01`.
- Track 05: `Q-05-01` and `Q-05-02`.
- Track 06: `Q-06-01`.

No claim, source, method, or result work was performed during this pass.
