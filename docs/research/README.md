# Research governance

## Decision question

The question is whether it is technically and economically credible to build an
unmodified x86-64 Linux system emulator on Apple Silicon using public macOS APIs
that is materially better than QEMU TCG, and, if so, which architecture deserves
planning.

This research starts from an intentionally narrow product envelope:

- two Apple Silicon generations and two supported macOS releases;
- unmodified modern x86-64 Linux guests, with direct boot and an SMP baseline;
- headless timer/APIC, serial, block, network, entropy, and shutdown support;
- a diverse workload set, not a single benchmark-shaped workload; and
- UEFI and Windows implications analyzed, but not implemented.

Security is deprioritized beyond isolation and crash containment for this phase.

Nothing inherited in this repository is verified evidence, and this organization
change has run no research. The preservation checkpoint is provenance only; it
can seed questions, not conclusions.

## Boundaries

Research creates and evaluates evidence against approved questions. Planning
turns an accepted synthesis outcome into an architecture and delivery proposal.
Implementation begins only after planning is authorized. Organizing this
workspace is none of those activities: it authorizes no browsing, source study,
probes, experiments, benchmarks, or implementation decisions.

The governing documents are [the evidence policy](evidence-policy.md) and [the
executor guide](executor-guide.md). Future work will record questions in the
[question backlog](question-backlog.md), sources in the
[source ledger](source-ledger.md), and reviews in the
[review ledger](review-ledger.md). Use the [investigation template](templates/investigation.md)
and [critique template](templates/critique.md) when those files are introduced.

## Tracks

The tracks are read in this order; their links are reserved for later,
separately authorized work.

1. [Architecture options](tracks/01-architecture-options.md)
2. [Host execution surface](tracks/02-host-execution-surface.md)
3. [Translation and execution](tracks/03-translation-execution.md)
4. [Memory virtualization](tracks/04-memory-virtualization.md)
5. [System platform](tracks/05-system-platform.md)
6. [Correctness and SMP](tracks/06-correctness-and-smp.md)
7. [Performance and economics](tracks/07-performance-and-economics.md)

## Review gates

1. Question gate
2. Source review
3. Method gate
4. Harness gate
5. Result review
6. Independent replication review
7. Adversarial critique
8. Synthesis review

Synthesis outcomes: stop / continue targeted research / authorize planning.
