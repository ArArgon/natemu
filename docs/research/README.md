# Research governance

## Decision question

The question is whether it is technically and economically credible to build an
unmodified x86-64 Linux system emulator on Apple Silicon using public macOS APIs
that is materially better than QEMU TCG, and, if so, which architecture deserves
planning.

This research starts from an intentionally narrow product envelope:

- at least two Apple Silicon generations and at least two supported macOS releases;
- unmodified modern x86-64 Linux guests, with direct boot and an SMP baseline;
- headless timer/APIC, serial, block, network, entropy, and shutdown support;
- a diverse workload set, not a single benchmark-shaped workload; and
- UEFI and Windows implications analyzed, but not implemented.

Security is deprioritized beyond isolation and crash containment for this phase.

Nothing inherited in this repository is verified evidence, and this organization
change has run no research. The preservation checkpoint is provenance only; it
can seed questions, not conclusions.

## Boundaries

Research creates and evaluates evidence against approved questions. Planning,
once research has authorized it, turns an accepted synthesis outcome into an
architecture and delivery proposal. Implementation begins only after research
authorizes planning, planning is completed and approved, and implementation is
separately authorized. Organizing this workspace is none of those activities:
it authorizes no browsing, source study, probes, experiments, benchmarks, or
implementation decisions.

The governing documents are [the evidence policy](evidence-policy.md) and [the
executor guide](executor-guide.md). Future work will record questions in the
[question backlog](question-backlog.md), sources in the
[source ledger](source-ledger.md), and reviews in the
[review ledger](review-ledger.md). Use the [investigation template](templates/investigation.md)
and [critique template](templates/critique.md) during separately authorized work.

## Tracks

The tracks are read in this order; their links are reserved for later,
separately authorized work.

1. [Scope and success](tracks/00-scope-and-success.md)
2. [Feasibility](tracks/01-feasibility.md)
3. [Correctness](tracks/02-correctness.md)
4. [Architecture options](tracks/03-architecture-options.md)
5. [Performance estimation](tracks/04-performance-estimation.md)
6. [Benchmarking and validation](tracks/05-benchmarking-and-validation.md)
7. [Research synthesis](tracks/06-research-synthesis.md)

## Review gates

1. Question gate
2. Source gate
3. Method gate
4. Harness gate
5. Result gate
6. Replication gate
7. Adversarial gate
8. Synthesis gate

Synthesis outcomes: stop / continue targeted research / authorize planning.
