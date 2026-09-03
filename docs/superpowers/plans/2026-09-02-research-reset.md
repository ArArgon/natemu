# Natemu Research Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the untrusted experimental checkout with a small research-phase workspace that defines scope, evidence standards, discovery duties, critique gates, and the path to a planning decision.

**Architecture:** Root files describe the lifecycle and constrain future executors. `docs/research/` owns current-phase policy, ledgers, templates, and seven ordered tracks. Old prose and probes remain available only from preservation commit `a121702`; generated artifacts stay outside Git.

**Tech Stack:** Markdown, Git, POSIX shell validation

---

### Task 1: Establish research-phase entry points

**Files:**
- Create: `README.md`
- Create: `AGENTS.md`
- Modify: `.gitignore`

- [ ] **Step 1: Verify the entry points are absent**

Run: `test -f README.md && test -f AGENTS.md`

Expected: non-zero exit because neither file exists.

- [ ] **Step 2: Create the root documents**

`README.md` must identify natemu as an unvalidated research project, state the decision question, link `docs/research/README.md`, explain the research → planning → implementation lifecycle, require research authorization before planning, and require completed and approved planning plus separate implementation authorization before implementation begins. It must point to `a121702` for untrusted historical material.

`AGENTS.md` must require executors to read the research policy, reject inherited probes and results as evidence, perform breadth passes, add discovered gaps, use primary sources, pass method and harness reviews before experiments, reject incorrect performance samples, obtain independent review for load-bearing findings, keep raw data outside Git, and treat SMP and platform dimensions explicitly.

- [ ] **Step 3: Extend generated-output ignores**

Add these exact lines to `.gitignore`:

```gitignore
# Research outputs are external artifacts, not repository content
artifacts/
results/
downloads/
scratch/
```

- [ ] **Step 4: Validate and commit**

Run:

```bash
test -f README.md
grep -q 'research phase' README.md
grep -q 'Correctness failures invalidate performance samples' AGENTS.md
grep -q '^artifacts/$' .gitignore
git add README.md AGENTS.md .gitignore
git commit -m "docs: establish research phase"
```

Expected: all checks pass and the commit succeeds.

### Task 2: Replace the old corpus with research governance

**Files:**
- Create: `docs/research/README.md`
- Create: `docs/research/evidence-policy.md`
- Create: `docs/research/executor-guide.md`
- Delete: existing contents of `docs/research/`

- [ ] **Step 1: Remove the old research corpus**

Run:

```bash
git rm -r docs/research
mkdir -p docs/research/tracks docs/research/templates
```

Expected: old numbered reports and raw dossiers are staged for deletion.

- [ ] **Step 2: Create the research index**

The index must state the decision question and product envelope, warn that nothing inherited is verified, link the evidence policy and executor guide, link all ledgers and templates, list these seven tracks in order — `00-scope-and-success`, `01-feasibility`, `02-correctness`, `03-architecture-options`, `04-performance-estimation`, `05-benchmarking-and-validation`, and `06-research-synthesis` — list the eight review gates, and define the three synthesis outcomes: stop, continue research, or authorize planning.

- [ ] **Step 3: Create the evidence policy**

Define `Source-backed`, `Observed`, `Inferred`, and `Hypothesis`. Restrict Source-backed evidence to primary specifications, official documentation, versioned upstream source, or peer-reviewed papers; secondary sources may locate evidence but cannot carry load-bearing claims. Prohibit unlabelled claims and require claim-level sources, applicability statements, and external immutable raw artifacts. Define applicable gates without making synthesis an individual-finding prerequisite:

```text
All questions use the question gate. Source-backed evidence uses the source gate. Experiments use method, harness, and result gates. Load-bearing observed findings use independent replication, and all load-bearing candidate findings use adversarial review. Eligible reviewed findings may enter candidate synthesis. The synthesis gate approves, revises, or rejects the final decision packet. A contradiction reopens the finding and dependent decisions.
```

- [ ] **Step 4: Create the executor guide**

Define this workflow:

```text
Select question → breadth pass → question gate → source gate → method proposal → method gate → harness implementation → harness gate → collect raw data → result gate → independent replication → adversarial critique → synthesis gate
```

Treat this as a superset workflow: source applies to Source-backed claims;
method, harness, and result apply to experiments; replication applies to
load-bearing Observed findings; adversarial applies to all load-bearing
candidates; and synthesis applies only to the final decision packet. Record an
inapplicable gate as `not applicable` with its reason rather than silently
skipping it.

Require an auditable breadth record and question-backlog updates, including `no new issues found` when true. Cover product assumptions, adjacent systems, negative cases, boundaries, host and guest matrices, multicore interactions, alternatives, falsifiers, and harness failure modes. Require and record a second end-of-track breadth pass from product, architecture, correctness, and methodology perspectives. Replication independently prepares and reviews its harness; platform-wide claims declare their platform matrix.

- [ ] **Step 5: Validate and commit**

Run:

```bash
grep -q 'Source-backed' docs/research/evidence-policy.md
grep -q 'adversarial' docs/research/executor-guide.md
grep -q 'Architecture options' docs/research/README.md
git add docs/research
git commit -m "docs: define research governance"
```

Expected: all checks pass and the commit succeeds.

### Task 3: Add living ledgers and templates

**Files:**
- Create: `docs/research/question-backlog.md`
- Create: `docs/research/source-ledger.md`
- Create: `docs/research/review-ledger.md`
- Create: `docs/research/templates/investigation.md`
- Create: `docs/research/templates/critique.md`

- [ ] **Step 1: Create the ledgers**

The question backlog defines `proposed`, `gated`, `investigating`, `review`, `accepted`, `rejected`, and `blocked`. Entries capture ID, question, relevance, dependencies, downstream decisions, applicability, resolution method, evidence, reviews, and newly surfaced questions.

The source ledger captures ID, classification, title, publisher, revision/date, URL or commit, inspected sections, supported claims, applicability limits, contradictions, reviewer, and verdict. It begins with no inherited accepted sources.

The review ledger defines `question`, `source`, `method`, `harness`, `result`, `replication`, `adversarial`, and `synthesis`. Entries capture reviewer/session, independence, artifacts, findings, changes, verdict, and date. Missing entry means the gate has not passed.

- [ ] **Step 2: Create the templates**

The investigation template contains question, relevance, assumptions, breadth pass, sources, hypothesis and falsifier, applicability matrix, method, controls, confounders, metrics, sample plan, exclusions, harness validation, artifact location, results, alternative explanations, surfaced questions, limitations, and review links.

The critique template challenges scope, sources, method, harness, statistics, correctness, multicore behavior, integration assumptions, alternatives, contradictions, and confidence. Verdicts are `accept`, `revise`, or `reject`.

- [ ] **Step 3: Validate and commit**

Run:

```bash
grep -q 'newly surfaced' docs/research/question-backlog.md
grep -q 'applicability' docs/research/source-ledger.md
grep -q 'replication' docs/research/review-ledger.md
grep -q 'Confounders' docs/research/templates/investigation.md
grep -q 'reject' docs/research/templates/critique.md
git add docs/research
git commit -m "docs: add research ledgers and templates"
```

Expected: all checks pass and the commit succeeds.

### Task 4: Define scope, feasibility, and correctness tracks

**Files:**
- Create: `docs/research/tracks/00-scope-and-success.md`
- Create: `docs/research/tracks/01-feasibility.md`
- Create: `docs/research/tracks/02-correctness.md`

- [ ] **Step 1: Define scope and success**

Use the initial target: public macOS APIs, unmodified modern x86-64 Linux, direct boot, SMP, minimal headless devices, at least two silicon generations and at least two supported macOS releases. Require user/workload definition, compatibility boundary, fair baselines, measurable “materially better,” non-goals, and stop conditions.

- [ ] **Step 2: Define feasibility**

Cover platform facts, architecture families, address spaces, translation, executable memory, paging, invalidation, exceptions, timers, interrupts, multicore coordination, devices, toolchain, licensing, and kill assumptions. Require an independently reviewed spike combining translation, paging, exceptions, interrupts, and at least two vCPUs.

- [ ] **Step 3: Define correctness**

Cover ISA, CPU modes, privileged state, CPUID/MSRs, exceptions, restartability, paging, atomics, memory ordering, interrupts, timers, boot, and scoped devices. Require specification-derived tests, independent oracles, fault injection, properties/fuzzing, and SMP litmus/invariant/stress tests. Incorrect samples cannot support performance.

- [ ] **Step 4: Validate and commit**

Run:

```bash
grep -q 'materially better' docs/research/tracks/00-scope-and-success.md
grep -q 'two vCPUs' docs/research/tracks/01-feasibility.md
grep -q 'cannot support performance' docs/research/tracks/02-correctness.md
git add docs/research/tracks
git commit -m "docs: define scope feasibility and correctness"
```

Expected: all checks pass and the commit succeeds.

### Task 5: Complete the research tracks

**Files:**
- Create: `docs/research/tracks/03-architecture-options.md`
- Create: `docs/research/tracks/04-performance-estimation.md`
- Create: `docs/research/tracks/05-benchmarking-and-validation.md`
- Create: `docs/research/tracks/06-research-synthesis.md`

- [ ] **Step 1: Define architecture options**

Compare translators, host/guest partitioning, MMU and code-cache strategies, interrupt and device models, dependencies, licensing, maintenance, portability, correctness burden, multicore implications, failures, and falsifiers. Output a recommendation and rejected-option record; planning owns implementation details.

- [ ] **Step 2: Define performance estimation**

Require ranges and sensitivity analysis. Separate translation, memory, faults, exits, compilation, exceptions, interrupts, devices, I/O, and concurrency. Prohibit multiplying overlapping speedups. Cover scaling, heterogeneous cores, contention, oversubscription, invalidation, atomics, concurrent I/O, and pressure.

- [ ] **Step 3: Define benchmarking and synthesis**

Benchmarking progresses from capability checks through microbenchmarks, subsystems, integrated spikes, and end-to-end workloads. Require multiple hosts and guests, 1/2/4/8+ vCPUs, diverse workload classes, randomized repetitions, controls, uncertainty, manifests, external raw data, and correctness status.

Synthesis compares surviving options against approved criteria, records rejected alternatives and unknowns, scans dependencies and contradictions, requires independent critique, and produces only: stop, continue targeted research, or authorize planning. It excludes production implementation details.

- [ ] **Step 4: Validate and commit**

Run:

```bash
grep -q 'planning owns' docs/research/tracks/03-architecture-options.md
grep -q 'overlapping speedups' docs/research/tracks/04-performance-estimation.md
grep -q '8+' docs/research/tracks/05-benchmarking-and-validation.md
grep -q 'authorize planning' docs/research/tracks/06-research-synthesis.md
git add docs/research/tracks
git commit -m "docs: complete research roadmap"
```

Expected: all checks pass and the commit succeeds.

### Task 6: Remove historical tooling from the current checkout

**Files:**
- Delete: `probes/`
- Delete: `tools/`
- Delete: `.agents/`
- Delete: `skills-lock.json`

- [ ] **Step 1: Confirm historical recovery**

Run:

```bash
git show a121702:docs/research/00-index.md >/dev/null
git show a121702:probes/qemu_baseline/runvm.py >/dev/null
git show a121702:tools/g2_quad_measure.py >/dev/null
```

Expected: all commands exit zero.

- [ ] **Step 2: Delete the preserved tooling**

Run:

```bash
git rm -r probes tools .agents
git rm skills-lock.json
git commit -m "chore: reset checkout to research roadmap"
```

Expected: the new research workspace remains; historical source is recoverable from `a121702`.

### Task 7: Verify the reset

**Files:**
- Modify only to correct verification defects.

- [ ] **Step 1: Check cleanliness and forbidden artifacts**

Run:

```bash
git status --short --ignored
git ls-files | grep -E '(^|/)(results|artifacts|downloads|scratch)(/|$)|\.(log|so|cpio|ckpt|jsonl|pyc)$'
grep -R -n -E '/Users/|/private/tmp/|M2 Max|M2 Pro|macOS 26\.5\.2|VERIFIED-LOCAL' README.md AGENTS.md docs/research
```

Expected: clean status; artifact and personal-path searches produce no output.

- [ ] **Step 2: Check all relative Markdown links**

Extract Markdown link targets from `README.md`, `AGENTS.md`, and `docs/**/*.md`, ignore HTTP(S) targets and anchors, resolve each target relative to its containing file, and assert that every target exists.

Expected: zero broken links.

- [ ] **Step 3: Recheck historical recovery**

Run:

```bash
git show a121702:docs/research/26-guest-rates-and-footprint.md >/dev/null
git show a121702:probes/smp_scaling/smp_scaling.c >/dev/null
```

Expected: both commands exit zero.

- [ ] **Step 4: Measure checkout and push payload**

Run:

```bash
du -sh README.md AGENTS.md .gitignore docs
git bundle create /tmp/natemu-research-reset.bundle --all
du -sh /tmp/natemu-research-reset.bundle
git count-objects -vH
```

Expected: current content is small text; the bundle is far below the deleted 2.4 GB.

- [ ] **Step 5: Review acceptance criteria**

Compare the current tree with every acceptance criterion in `docs/superpowers/specs/2026-09-02-research-reset-design.md`. Correct defects, rerun affected checks, and commit corrections if any.
