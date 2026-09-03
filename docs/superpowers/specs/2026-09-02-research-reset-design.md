# Natemu Research Reset Design

**Date:** 2026-09-02
**Status:** Revised after written-spec review
**Preservation checkpoint:** `a121702`

## Context

Natemu is a research repository for a possible x86-64 system emulator on Apple Silicon. The existing corpus contains extensive prose, experimental probes, and locally captured results, but no integrated emulator. Several load-bearing conclusions were derived from narrow or noisy experiments, synthetic models, single-machine observations, or probe implementations that may themselves contain bugs and machine-specific assumptions.

The preservation checkpoint retains the old documentation and source code without generated outputs. It is historical reference only. Nothing in that checkpoint is considered verified.

## Goals

- Replace the current working layout with a fresh validation roadmap.
- Make the cleaned checkout explicitly represent the research phase; planning and implementation will have separate future workspaces.
- Treat all previous local measurements, source code, and architecture choices as unverified.
- Retain only source-backed findings that can be traced to primary specifications, official documentation, upstream source at a named revision, or peer-reviewed papers.
- Make correctness a first-class research track and a gate for performance work.
- Make multicore behavior a required dimension wherever mechanisms can interact across vCPUs.
- Keep future checkouts small by excluding generated data, downloads, binaries, and caches.
- Preserve the old material through Git history rather than an attic in the current checkout.
- Require executors to discover and record questions outside the initial roadmap rather than merely completing a fixed checklist.
- Require systematic methodology, correctness, and adversarial critique before findings or decisions are promoted.

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

## Lifecycle boundaries

Natemu has three distinct phases:

1. **Research:** establish scope, source-backed constraints, feasibility, correctness requirements, candidate architectures, performance models, and validated observations. This cleanup creates the research workspace only.
2. **Planning:** convert reviewed research findings into an implementation specification, milestones, dependencies, and acceptance tests. Planning begins only after the research exit gate passes and will live under `docs/planning/`.
3. **Implementation:** build and test the selected system under normal source and test directories, with implementation documentation under `docs/implementation/` when needed.

Research notes must not masquerade as an implementation plan. Candidate designs remain alternatives until a reviewed research decision promotes one. The planning and implementation directories will not be created as empty placeholders during this cleanup.

Research decides what must be true and which approaches remain viable. Planning makes the binding choice of what to build, including module boundaries, interfaces, data structures, dependencies, milestones, and task ordering. The research exit packet may recommend an architecture and reject disproven alternatives, but it does not make detailed implementation decisions.

## Recommended research scope

The research phase exists to answer one decision:

> Is it technically and economically credible to build an unmodified x86-64 Linux system emulator on Apple Silicon that is materially better than QEMU TCG, and, if so, which architectural direction deserves planning?

The initial product envelope is:

- Apple Silicon hosts using public macOS APIs;
- at least two silicon generations and two supported macOS releases;
- unmodified, modern x86-64 Linux guests;
- direct kernel boot for the initial validation target;
- SMP as a baseline requirement rather than a later extension;
- headless operation with timer/APIC, serial, block, network, entropy, and shutdown support sufficient for meaningful workloads;
- compute, memory, process, JIT, storage, network, concurrent-I/O, and mixed workloads;
- UEFI and Windows implications analyzed for architectural compatibility but not implemented during research;
- security de-prioritized beyond safe host isolation and crash containment.

The scope must be revisited at the question gate. If the intended product is patched Linux, arbitrary firmware, Windows, or broad legacy-x86 compatibility, that change reopens architecture and correctness conclusions rather than being absorbed silently.

## Target layout

The cleaned checkout will contain:

```text
README.md
AGENTS.md
.gitignore
docs/
  research/
    README.md
    evidence-policy.md
    executor-guide.md
    question-backlog.md
    source-ledger.md
    review-ledger.md
    tracks/
      00-scope-and-success.md
      01-feasibility.md
      02-correctness.md
      03-architecture-options.md
      04-performance-estimation.md
      05-benchmarking-and-validation.md
      06-research-synthesis.md
    templates/
      investigation.md
      critique.md
  superpowers/
    specs/
      2026-09-02-research-reset-design.md
    plans/
      2026-09-02-research-reset.md
```

The current `probes/`, `tools/`, `.agents/`, and `skills-lock.json` paths will be removed from the working tree. The existing contents of `docs/research/` will be replaced by the research-phase workspace above. The removed material remains recoverable from commit `a121702`.

`README.md` explains the phase model and current status. `AGENTS.md` gives future automated executors the mandatory research and review workflow. `docs/research/README.md` is the human entry point. The ledgers make questions, sources, findings, dependencies, and reviews inspectable rather than burying them in narrative reports.

## Executor discovery mandate

The initial roadmap is a seed, not a closed task list. Before answering a research question, the executor must perform a breadth pass that asks:

- What assumptions does the question make about product scope, host, guest, workload, concurrency, or dependencies?
- Which adjacent subsystem can change the answer when integrated?
- What negative cases, boundary conditions, failure modes, and counterexamples are absent?
- Which target machines, OS versions, guest configurations, CPU modes, and vCPU counts could produce a different answer?
- What would falsify the current framing rather than merely refine a number?
- Which primary sources, competing implementations, and prior-art alternatives have not been examined?
- What might make the measurement harness report a plausible but wrong answer?

Each investigation must add newly surfaced items to `question-backlog.md`, including why they matter, what they depend on, how they can be resolved, and whether they block another question. An executor may not mark an assigned item complete without recording the discovery pass, even when it found no new issues.

At the end of each research track, the executor performs a second breadth pass from four perspectives: product-scope skeptic, architecture skeptic, correctness specialist, and experimental-method critic. This can be performed by independent people, agents, or sessions, but the author may not impersonate independence when reviewing a load-bearing result.

## Roadmap structure

### Scope and success

The roadmap starts by validating or revising the recommended product envelope, then choosing CPU modes and extensions, use cases, compatibility boundaries, comparison baselines, and measurable go/no-go thresholds. It must define what “materially better than QEMU TCG” means before architecture estimates are compared. Rosetta may inform user-mode translation limits but is not a system-emulation baseline.

### Feasibility

Feasibility work will:

- recheck platform capabilities against current primary sources;
- compare multiple candidate architectures without a preferred incumbent;
- identify kill assumptions and test the cheapest, highest-impact ones first;
- distinguish API availability from behavioral suitability;
- test relevant mechanisms across target machine and OS combinations;
- include single-vCPU and multicore configurations where concurrency changes the answer;
- require an integrated feasibility spike that exercises translation, paging, exceptions, interrupts, and at least two vCPUs together;
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

### Architecture options

Architecture research will compare candidate architectures, translators, host/guest partitioning, MMU strategies, code-cache designs, interrupt models, device approaches, and reusable components. Each proposal must state its dependencies, unsupported surface, failure modes, correctness burden, multicore implications, portability assumptions, licensing and maintenance implications, and experiments that could falsify it. Research prototypes are disposable evidence-generating instruments; they do not become production foundations automatically. The output is a reviewed recommendation and rejected-option record, not a detailed implementation design.

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

### Research synthesis

The final track compares surviving architecture options against the agreed scope, correctness requirements, feasibility evidence, and performance bounds. It records rejected alternatives, remaining unknowns, accepted risks, and the confidence of each recommendation. Its reviewed decision packet recommends one of three outcomes: stop, continue targeted research, or authorize the planning phase.

## Review and critique gates

Research items advance through explicit gates. A review records reviewer identity or session, evidence inspected, findings, required changes, and one of `accept`, `revise`, or `reject` in `review-ledger.md`.

1. **Question gate:** Check that the question is decision-relevant, scoped, falsifiable, and not already answered by a primary source. Identify dependencies and downstream decisions.
2. **Source gate:** Check primary-source coverage, versions and revisions, contradictory sources, applicability boundaries, and citation accuracy. Secondary sources may locate evidence but may not carry a load-bearing claim by themselves.
3. **Method gate:** Review the proposed experiment before implementation. Require controls, confounders, workload and concurrency dimensions, sample plan, metrics, exclusion rules, expected failure signatures, and stop criteria.
4. **Harness gate:** Review experiment code and configuration independently before collecting evidence. Validate the harness against known behavior, include negative and fault-injection controls, and check that instrumentation does not erase the phenomenon under study.
5. **Result gate:** Audit raw-data completeness, provenance, statistical treatment, anomalous runs, alternative explanations, and agreement between the recorded method and the executed method. Correctness failures invalidate performance samples.
6. **Replication gate:** Repeat load-bearing observations on an independently prepared harness and, when the claim is platform-sensitive, across the declared machine and OS matrix. A single-machine observation remains `Observed` with a narrow applicability statement; it cannot close a platform-wide gate.
7. **Adversarial critique gate:** Assign an independent reviewer to construct the strongest counterargument, search for missing interactions and circular reasoning, and attempt to falsify the conclusion. The author responds with evidence or downgrades the claim.
8. **Synthesis gate:** Before a finding affects architecture or planning, scan dependencies and the rest of the research corpus for contradictions. A new contradiction reopens the finding and every dependent decision.

Self-review is required at every gate but is not sufficient for load-bearing findings. No author or single agent session may approve its own method, harness, result, replication, and adversarial gates. Reviews should be small and frequent; a late omnibus audit is not a substitute.

The research phase exits only when:

- product scope, comparison baselines, and performance thresholds are explicit;
- blocking questions have accepted evidence;
- at least one architecture has survived feasibility and adversarial review;
- an integrated multicore spike has validated its critical mechanisms together;
- correctness requirements and the validation strategy are credible and reviewable;
- performance bounds use correctness-qualified evidence and expose their uncertainty;
- platform-sensitive claims cover the declared host matrix or carry narrow applicability labels;
- rejected alternatives and residual unknowns are recorded;
- no unknown blocker is hidden inside a schedule or performance estimate;
- independent reviewers approve the research decision packet.

Exit approval creates the input to the separate planning phase. It does not approve production APIs, data structures, module boundaries, dependency pins, milestones, or task sequencing.

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

This register is not exhaustive. `question-backlog.md` is the living gap register, and review gates must add newly discovered blind spots instead of forcing them into the original categories.

## Repository cleanup

The first preservation commit has already removed tracked result directories, logs, shared objects, initramfs images, checkpoints, and other generated outputs. It also removed ignored downloads, binaries, caches, and browser scratch data from the local working tree.

The reset commit will remove the remaining historical source and prose from the current checkout after replacing their useful role with the roadmap and trust policy. `.gitignore` will continue to reject result directories, build products, downloaded work trees, caches, logs, binary images, and operating-system metadata.

## Verification and recovery

Before the reset is reported complete:

1. Confirm the only current files match the research-phase target layout.
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
- Research, planning, and implementation are separate phases, and only the research workspace exists in the cleaned checkout.
- The roadmap gives scope, feasibility, correctness, architecture options, performance estimation, benchmarking, and synthesis distinct responsibilities.
- Correctness gates precede performance evidence.
- Multicore coverage is explicit in feasibility, correctness, estimation, and benchmarking.
- Executors must perform and record breadth passes that can expand the initial question set.
- Question, source, method, harness, result, replication, adversarial, and synthesis gates are defined with recorded verdicts.
- Load-bearing findings cannot be approved solely by their author or originating agent session.
- Generated data cannot be accidentally committed through the documented paths and extensions.
- The preservation checkpoint remains recoverable.
- The repository is small enough to transfer without any of the deleted 2.4 GB local artifact tree.
