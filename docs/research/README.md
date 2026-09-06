# Research workflow

This is the authoritative workflow for new research. Read it, then the assigned
[feasibility charter](tracks/01-feasibility.md) and the investigation it assigns.
That investigation owns its current question, conclusion, applicability, blockers,
and next action. Consult a
historical review only for a specific unresolved finding, contradiction, or audit.

## Decision and product envelope

Determine whether an unmodified x86-64 Linux system emulator on Apple Silicon,
using documented Apple silicon capabilities, can credibly be materially better than QEMU TCG in
technical and economic terms, and which architecture deserves planning.

The envelope remains:

- Apple silicon under macOS 26; the M4 is the host of record and an M2 Pro/Max is
  the second generation when available;
- unmodified modern x86-64 Linux guests, direct boot, and an SMP baseline;
- headless timer/APIC, serial, block, network, entropy, and shutdown support;
- diverse workloads, with UEFI and Windows implications analyzed but not implemented;
- isolation and crash containment retained, with other security work deprioritized.

The detailed accepted scope remains in [Track 00](tracks/00-scope-and-success.md).
Material at or before preservation commit `a121702`, including inherited probes
and results, is provenance only and cannot serve as evidence. It may seed questions.
Research makes no binding implementation decisions. Final synthesis separately
selects **stop**, **continue targeted research**, or **authorize planning**.
Implementation requires completed, approved planning and separate authorization.
This workflow migration neither completes Track 01 nor adds experimental evidence.

## Conduct an investigation

1. Open one question in a self-contained [investigation](templates/investigation.md).
   State its decision consequence and perform a breadth check of product assumptions,
   adjacent systems, alternatives, negative/boundary cases, host/guest matrix, SMP,
   falsifiers, and measurement failure modes. Classify discoveries as current
   blockers, downstream questions, or retained unknowns. Record each once in its
   owning investigation or the [question backlog](question-backlog.md); if there
   are none, say so. Existing identifiers are retained. Every kill assumption and
   finding names the design it applies to.
2. Explain the relevant source contracts, assumptions, method, controls, and limits.
   For experiments, declare inputs, correctness oracles, sample/exclusion rules,
   resource/time budgets, failure behavior, and stopping conditions. Every open
   investigation has one bounded next step, the decision it could change, and
   its stopping condition. A missing execution budget is explicit work to finish
   before execution, not permission for an unlimited campaign.
3. Obtain **pre-execution review** from an independent reviewer of the question,
   relevant sources, method, controls, and code before experimental execution.
   The review must answer three screening items before anything else: (a) does
   QEMU TCG or an ordinary VM on this platform share the problem; (b) does a
   documented platform capability already answer it; (c) which design does the
   finding apply to. A question failing (a) or (b) is reframed, not executed. Authors may prepare code, compile, and perform synthetic validation
   beforehand. Record these self-checks briefly with the submitted work; do not
   create separate self-review documents. Source-only work proceeds directly to
   findings review.
4. Run the reviewed experiment with retained inputs and artifacts as below.
   Correctness failures invalidate affected performance samples. Preserve negative
   outcomes, partial failures, exclusions, and contradictions; a checker PASS is
   not acceptance of a scientific conclusion. Report correctness independently
   from timing qualification. Missing applicable clock evidence leaves timing
   unqualified and does not by itself prevent independently justified correctness work.
5. Obtain **findings review** of results or source-only findings, applicability,
   alternative explanations, replication, and adversarial challenges before
   accepting conclusions. Load-bearing observed findings require independent
   replication whose preparation and code were also independently checked.
   Record the accepted finding and remaining substantive issues in the investigation.
   A contradiction reopens the affected claim and any conclusion relying on it
   until resolved or withdrawn; migration and previous approval cannot resolve it.

At track completion, perform a breadth check from product, architecture,
correctness, and methodology perspectives. Carry unresolved implications and
disagreements to their owners and synthesis. Track 01 answers its questions inside
one growing, disposable test program: each question's test is added on top of the
previous questions' working result with every earlier mechanism left on, so the
mechanisms are exercised together without a prototype deliverable. Mechanism
viability establishes neither performance nor a design.
A track closes only when its questions have accepted scoped resolutions or
accepted kills and the completion breadth check is addressed. A family kill
removes that family; a project kill routes to synthesis, not automatic track completion.

## Review practice

Use one [review thread](templates/critique.md) per investigation and review step,
created when needed: `reviews/INV-<track>-<nn>-pre-execution.md` or
`reviews/INV-<track>-<nn>-findings.md`. Keep returns in that thread; no new global
review number is needed per exchange. Record the reviewer identity/context,
material inspected, evidence, verdict, and return disposition.

An independent reviewer is a different author working in a separately recorded
context and able to challenge the submission. Use subagents for independent
reviews and replication by default. Keep that reviewer through returns, inspecting
changed material and affected assumptions. If the reviewer authors a load-bearing
change, another independent reviewer must examine it.

Block only on defects threatening conclusions, execution validity, or reproduction.
For each blocker state its consequence, evidence, smallest remedy, and closure
condition; hand over reusable reproductions and fixtures. Fix editorial issues
directly and disclose the edit. Pending review is work status, not a defect.
Judge claims against evidence rather than prior approval. An accept verdict has
no unresolved substantive blockers and satisfies the required independence.
Synthesis review is a separate project decision, not an extra per-investigation gate.
Files under `notes/` are outside the review workflow and inadmissible as support
for any claim; they hold candidate ideas only.

## Evidence and direct references

Identify substantive claims as **Source-backed** (direct primary support),
**Observed** (controlled measured result), **Inferred** (explicit reasoning from
identified evidence), or **Hypothesis** (unestablished proposition). These describe
evidence type, not a universal ranking: an observation cannot prove a platform-wide
contract and a document cannot prove runtime behavior. Every load-bearing claim
requires independent findings review.

Explain the contract or idea relied upon beside a direct supporting citation.
Primary specifications, official documentation, upstream source, and research
papers support load-bearing claims; secondary material may locate them. Cite
names, sections, paths and symbols for routine man pages and SDK interfaces.
Record versions where availability, semantics, applicability, or recoverability
requires them. Replace authoritative references when the supported meaning is
unchanged; reassess substantive contradictions rather than byte changes.
Routine document/source hashes, internal line-number references, and transitive
approval references are unnecessary. Retain source snapshots only when needed
to recover material supporting a finding. Historical source IDs remain valid
provenance, but new claims cite their supporting material directly.

State host generation/model, macOS/SDK, guest, vCPU count and placement, workload,
and relevant signing/API context explicitly. One-host results stay one-host
results. Additional hosts widen applicability incrementally as available;
unavailable-host replication must precede platform-wide synthesis use. SMP
results do not follow from single-thread tests, and placement requests do not
establish actual core placement.

## Retained experiment runs

Write new raw data outside Git beneath `NATEMU_RAW_ROOT`. Runners refuse absent
or repository-contained output roots, overwrites, and mixed-run/build evidence.
Keep checksums for retained artifacts. Each evidence run retains a small snapshot
of its experiment sources, local dependencies, and build inputs and **builds from
that snapshot**. Record toolchain/SDK versions, flags, inputs, environment,
execution outcome, and one run identity associating the producing build, controls,
and results. Record failed/partial runs as well as successful ones.

Repository HEAD and a clean tree are not approval conditions. Runtime checks
validate factual context and data integrity, not review prose or approval receipts.
Reviewers assess clock applicability in prose. Runners retain output boundaries,
non-overwrite behavior, child supervision, timeout/resource limits, partial-failure
records, and substantive correctness/control checks. Changed formats use explicit
new versions; missing historical approval fields never imply new-format success.

## Navigation and historical records

Two historical bodies of work live only in git history and are not evidence or
current direction: the August 2026 corpus at commit `a121702`, and the September
2026 Track 01 process-design investigations, reviews, manifests, ledgers and
experiments at tag `track01-archive-2026-09-06`. Do not read them unless auditing
a specific old finding. Other tracks retain their existing question content; their
new work follows this workflow.

## Identifiers

Keep existing `Q-<track>-<nn>`, `INV-<track>-<nn>`, and `CLM-<track>-<nn>-<nn>`
identifiers. An investigation answers the question with the same number.
Historical `SRC-<nnn>`, `REV-<nnn>`, `MAN-<track>-<nn>-<nn>`, `BP-<track>-<nn>-start`
and `BP-<track>-end` identifiers are recoverable at tag `track01-archive-2026-09-06`
and are never reassigned. Withdrawn identifiers remain recorded.

## Tracks

1. [Scope and success](tracks/00-scope-and-success.md)
2. [Feasibility](tracks/01-feasibility.md)
3. [Correctness](tracks/02-correctness.md)
4. [Architecture options](tracks/03-architecture-options.md)
5. [Performance estimation](tracks/04-performance-estimation.md)
6. [Benchmarking and validation](tracks/05-benchmarking-and-validation.md)
7. [Research synthesis](tracks/06-research-synthesis.md)
