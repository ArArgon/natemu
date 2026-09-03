# Natemu Research Reset Design

**Date:** 2026-09-02  
**Status:** Approved direction, pending written-spec review  
**Preservation checkpoint:** `a121702`

## Context

Natemu is a research repository for a possible x86-64 system emulator on Apple Silicon. The existing corpus contains extensive prose, experimental probes, and locally captured results, but no integrated emulator. Several load-bearing conclusions were derived from narrow or noisy experiments, synthetic models, single-machine observations, or probe implementations that may themselves contain bugs and machine-specific assumptions.

The preservation checkpoint retains the old documentation and source code without generated outputs. It is historical reference only. Nothing in that checkpoint is considered verified.

## Goals

- Replace the current working layout with a fresh validation roadmap.
- Treat all previous local measurements, source code, and architecture choices as unverified.
- Retain only source-backed findings that can be traced to primary specifications, official documentation, upstream source at a named revision, or peer-reviewed papers.
- Make correctness a first-class research track and a gate for performance work.
- Make multicore behavior a required dimension wherever mechanisms can interact across vCPUs.
- Keep future checkouts small by excluding generated data, downloads, binaries, and caches.
- Preserve the old material through Git history rather than an attic in the current checkout.

## Non-goals

- Rehabilitating, porting, or fixing the existing probes.
- Carrying forward old measurements as baselines.
- Selecting FEXCore, QEMU components, shadow paging, a helper VM, or any other architecture in advance.
- Producing new feasibility or performance conclusions during this cleanup.
- Performing a full security study. Basic host isolation and crash safety remain prerequisites for future execution, but security is de-prioritized.
- Rewriting Git history.

## Trust model

Every future claim must have one of these labels:

- **Source-backed:** directly supported by a primary specification, official API documentation, upstream source at a recorded revision, or a peer-reviewed paper.
- **Observed:** produced by a reproducible experiment with its code, environment manifest, raw output, and analysis recorded together.
- **Inferred:** derived from source-backed or observed inputs with the derivation shown.
- **Hypothesis:** plausible but unverified; paired with a falsifiable question and acceptance criteria.

Unlabelled claims are not admissible. Results from the preservation checkpoint may suggest hypotheses but may not become evidence without independent reimplementation and rerun.

## Target layout

The cleaned checkout will contain:

```text
README.md
.gitignore
docs/
  source-backed-findings.md
  roadmap/
    00-scope-and-success.md
    01-feasibility.md
    02-correctness.md
    03-implementation-ideation.md
    04-performance-estimation.md
    05-benchmarking-and-validation.md
  superpowers/
    specs/
      2026-09-02-research-reset-design.md
    plans/
      2026-09-02-research-reset.md
```

The current `probes/`, `tools/`, `docs/research/`, `.agents/`, and `skills-lock.json` paths will be removed from the working tree. They remain recoverable from commit `a121702`.

## Roadmap structure

### Scope and success

The roadmap starts by choosing supported guests, boot methods, CPU modes and extensions, Apple Silicon generations, macOS versions, use cases, compatibility boundaries, and measurable go/no-go thresholds. Product decisions such as “patched Linux only” versus “arbitrary x86 operating systems” must be settled before architecture estimates are compared.

### Feasibility

Feasibility work will:

- recheck platform capabilities against current primary sources;
- compare multiple candidate architectures without a preferred incumbent;
- identify kill assumptions and test the cheapest, highest-impact ones first;
- distinguish API availability from behavioral suitability;
- test relevant mechanisms across target machine and OS combinations;
- include single-vCPU and multicore configurations where concurrency changes the answer;
- finish with explicit evidence requirements and go/no-go gates.

### Correctness

Correctness is a standalone track that precedes performance conclusions. It covers:

- x86 instruction semantics, CPU modes, privileged state, CPUID and MSRs;
- exceptions, restartability, paging, invalidation, interrupts, timers, atomics, and memory ordering;
- device, storage, networking, timekeeping, and boot behavior within the selected scope;
- specification-derived conformance tests;
- differential testing against multiple independent implementations;
- property-based testing and fuzzing for decoding, translation, and fault behavior;
- litmus tests, invariants, stress tests, and outcome distributions for SMP behavior;
- explicit declarations for unsupported behavior.

Single-vCPU trace comparison may be used where determinism is valid. Multicore correctness must not depend on instruction-by-instruction deterministic replay because serialization changes the concurrency being tested. A run that fails its correctness gate cannot contribute performance evidence.

### Implementation ideation

Implementation ideation will compare candidate architectures and reusable components. Each proposal must state its dependencies, unsupported surface, failure modes, correctness burden, multicore implications, portability assumptions, and experiments that could falsify it. The first implementation milestone is the smallest end-to-end vertical slice, not a collection of disconnected optimized probes.

### Performance estimation

Performance estimates will use transparent cost models with ranges and sensitivity analysis. Inputs must be labelled by provenance. The model will keep translation, memory, exits, compilation, exceptions, interrupts, devices, I/O, and concurrency costs separate. Speedups from different systems or overlapping mechanisms must not be multiplied into a headline projection.

Single-core latency and throughput are insufficient. Estimates must include vCPU scaling, shared-resource contention, P-core/E-core placement, oversubscription, TLB shootdowns, atomic contention, interrupt delivery, concurrent I/O, and memory pressure.

### Benchmarking and validation

Benchmarking is the validation method shared by every track. It proceeds from capability checks to microbenchmarks, subsystem tests, and end-to-end workloads. The minimum matrix includes:

- multiple Apple Silicon generations and supported macOS releases;
- multiple guest kernels, distributions, libc implementations, CPU feature profiles, and memory sizes;
- 1, 2, 4, 8, and higher vCPU counts where the host permits;
- heterogeneous core placement and oversubscription;
- compute, memory, process, JIT, storage, network, concurrent-I/O, and mixed workloads;
- warm and cold conditions where relevant;
- repeated randomized trials, controls, uncertainty reporting, and predefined exclusion rules.

Every result must include the experiment revision, machine manifest, toolchain and dependency versions, configuration, raw output, analysis revision, and correctness status. Raw results are immutable artifacts stored outside Git or in a deliberately configured artifact store.

## Blind-spot register

The roadmap must explicitly track, rather than silently omit:

- the lack of an end-to-end prototype;
- multicore startup, memory ordering, atomics, invalidation, interrupts, and scaling;
- P-core/E-core scheduling and oversubscription;
- PCID/ASID mapping and exhaustion;
- page-table races, aliases, page sizes, memory pressure, and large guests;
- dynamic code and JIT-heavy workloads;
- firmware, boot modes, CPU feature exposure, and guest diversity;
- device and I/O behavior under contention;
- lifecycle behavior such as host sleep, guest suspend, teardown, and time discontinuities;
- oracle independence, test-harness validation, and experimental falsification;
- competing translator and architecture choices;
- platform and API changes across machines, OS releases, and SDK revisions.

Security remains recorded as deferred rather than silently assumed complete.

## Repository cleanup

The first preservation commit has already removed tracked result directories, logs, shared objects, initramfs images, checkpoints, and other generated outputs. It also removed ignored downloads, binaries, caches, and browser scratch data from the local working tree.

The reset commit will remove the remaining historical source and prose from the current checkout after replacing their useful role with the roadmap and trust policy. `.gitignore` will continue to reject result directories, build products, downloaded work trees, caches, logs, binary images, and operating-system metadata.

## Verification and recovery

Before the reset is reported complete:

1. Confirm the only current files match the target layout.
2. Confirm no tracked file is binary or output-like.
3. Confirm no ignored or untracked generated artifact remains.
4. Confirm no personal absolute filesystem path remains in the current checkout.
5. Confirm all Markdown links resolve locally or point to an explicit external source.
6. Confirm the previous docs and representative source files are readable from `a121702` with `git show`.
7. Create a temporary Git bundle and report its packed size as an upper-bound transfer check.
8. Report the current checkout size separately from `.git` history size.

If historical material is needed, it should be inspected or selectively restored from `a121702`; the cleaned checkout must not grow an informal attic.

## Acceptance criteria

- The current checkout describes an unvalidated concept and contains no old conclusions presented as current truth.
- The roadmap gives feasibility, correctness, implementation ideation, performance estimation, and benchmarking distinct responsibilities.
- Correctness gates precede performance evidence.
- Multicore coverage is explicit in feasibility, correctness, estimation, and benchmarking.
- Generated data cannot be accidentally committed through the documented paths and extensions.
- The preservation checkpoint remains recoverable.
- The repository is small enough to transfer without any of the deleted 2.4 GB local artifact tree.
