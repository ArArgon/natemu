# Track 03 charter revision — independent review

- Reviewer: `/root/method_review`, a separate context from the author `/root`.
  The reviewer supplied prior advisory critiques but did not edit the charter
  or its alignment changes.
- Date: September 19, 2026.
- Subject: owner-authorized architecture/correctness workflow revision, not
  scientific findings or an experiment submission.
- Inspected: root `README.md`; research `README.md` and `question-backlog.md`;
  Tracks 00, 02, 03, 04, 05 and 06, including their working-tree changes;
  the current Track 01 charter for inherited scope and carryforward obligations.
- Initial verdict: **revise**, for the coverage issue below. The main Track 03
  charter and Track 02 redirect have no substantive blocker.

## Assessment

The twelve decisions and their interaction checks cover the major retained
guest-behavior obligations: CPU/state, translation, execution boundaries,
paging, recovery, SMP ordering, executable-code lifetime, events, clocks,
boot, devices and containment. Their interaction list includes writers that
bypass guest traps, source mutation, state-dependent dispatch, MMIO side
effects, metadata lifetime, wait/progress dependencies and teardown races.
This is adequate charter-level coverage, not a claim that every future
implementation case is enumerated or already correct.

The revision allows coupled decisions and bounded experiments without a
whole-track waterfall or twelve new gates. Correctness uncertainty keeps the
affected choice provisional. The owner policies on undocumented capabilities,
feature-dependent M2 consideration and licensing warnings are reflected.
Planning/implementation authorization remains separate. Synthesis distinguishes
reviewed projections from measured product superiority and cannot defer an
uncertainty that could invalidate the selected architecture merely by renaming it.
Track 02 retirement preserves its obligations and creates no accepted evidence.

## Substantive return

**R1 — Preserve existing broad-performance coverage requirements.** The first
Track 05 coverage rewrite removed the minimum two guest configurations,
1/2/4/8+ vCPU ladder and named workload classes along with the general
two-generation prerequisite. Only the host-generation policy and research
staging were changed by the owner; dropping the other coverage criteria would
silently weaken broad performance claims. Retain those criteria for broad
claims while allowing narrowly scoped design experiments and conditional M2
work. Closure requires inspecting that correction in Track 05.

## Limits and edits

No authored charter was edited by this reviewer. No experiments, archived
history or raw evidence were inspected for this review. Candidate source notes
were not treated as accepted support. This review accepts or returns document
requirements and consistency only; it establishes no design correctness,
platform capability, performance result or experimental authorization.

## Returns

**September 19 — R1 closed; final verdict: accept.** Reinspected Track 05's
Required coverage and Exit criteria. Product-wide qualification explicitly
retains two guest configurations, the 1/2/4/8+ vCPU ladder and the five workload
classes, while design experiments can support narrower claims. This preserves
the existing performance bar without reinstating a research waterfall or
general M2 parity requirement. Also inspected the retained backlog correctness
anchor, Q-05-01's claim-specific measurement wording and Q-06-01's direct
evidence dependencies. `git diff --check` passed. No substantive blocker remains
in the inspected revision; the acceptance has the limits stated above.

## Exploration amendment review

**September 19 — independent amendment verdict: accept.** The same reviewer
inspected the research workflow's Conduct an investigation, Review practice and
Retained experiment runs sections, and Track 03's How work proceeds section,
against the owner's exploration/decision-evidence distinction. This verdict
reviews the subsequent amendment separately from the charter acceptance above.

Bounded exploration can proceed without independent pre-execution review, with
a brief question/assumptions/budget/stopping record and retained producing
sources, inputs, outputs and failures. Its results can change working hypotheses
and designs, but cannot be relabeled into accepted correctness, definitive
rejection or performance evidence. Experimental acceptance requires a fresh
reviewed run, findings review and independent replication of load-bearing
observations. Accepted applicable evidence and reviewed source contracts can
still settle a choice without redundant execution. Contrary exploratory
observations must be investigated rather than ignored because of their status.

Local choices explicitly carry surrounding assumptions and relevant interaction
obligations; changes require reconsideration. Local acceptance does not establish
combined architecture correctness. The amendment introduces no additional
approval gate or separate runner framework and retains bounded execution and
producing-artifact provenance. No substantive contradiction or soundness bypass
was found in the inspected amendment. No main document was edited by this
reviewer, and no run, design finding or implementation was approved by this
document review.

## Independent final consistency audit

**September 19 — reviewer `/root/external_research`; final verdict: accept.**
This separate reviewer inspected the current working diff and research workflow,
Tracks 00/02/03/04/05/06, backlog, repository entry point and investigation/review
templates. The audit challenged exploration versus decision evidence, fresh-run
requirements versus source-only findings, optional measurement stages, acceptance
boundaries and consistency of adjacent instructions. No archived history or
experiments were inspected. This reviewer authored none of the governing changes.

Three substantive initial issues were returned and are now closed:

- Track 05 still required approved code/reviewed methods for every capability
  or integrated experiment and statistics for every reported result. The final
  text applies pre-execution review to decision evidence, statistics to
  decision-bearing performance results, and explicitly permits exploratory and
  correctness-only records appropriate to their claims.
- The workflow entry point still routed new work to the completed feasibility
  charter. It now directs readers to the assigned track and owning investigation,
  using Track 01 findings only where applicable.
- The re-review rule named methods/assumptions but left changed experimental
  code and controls ambiguous. It now includes substantive claims, code,
  controls, methods and assumptions, and requires experimental changes to be
  rechecked before further decision-evidence execution.

The reviewer also inspected the final Track 06 correction permitting unreviewed
contrary observations as unresolved concerns without promoting them to accepted
evidence, and the investigation template's explicit brief-exploration instruction.
Both are consistent with the workflow. Source-only acceptance does not require a
fresh experiment; exploratory runs cannot be promoted by relabeling. Local-choice
acceptance retains interaction assumptions, and architecture acceptance does not
claim correctness of unwritten code. No substantive blocker remains in the
inspected revision. `git diff --check` passed independently.

This acceptance concerns document requirements and consistency only; it accepts
no experimental result, platform capability, architecture finding or implementation.
Only this review record was edited by this reviewer.

## ABI-first workflow alignment — September 28, 2026

- Reviewer: `/root/workflow_review`, an independent context from author `/root`.
  The reviewer provided critique but authored none of the governing changes.
- Subject: the owner's workflow/track alignment request, including readable
  checkpoints and Track 03 ownership of a provisional ABI and bounded experimental
  runtime. The fixed architecture remains host code generation plus captive-VM
  execution; generator selection is deferred and performance-led. This review
  decides readiness to draft the ABI and its method, not component selection.
- Inspected: the full working diff for `AGENTS.md`, root/research READMEs,
  Tracks 00/03/04/05/06, the backlog, investigation template and current sections
  of INV-03-01/03/04; surrounding Track 01/02 charters and INV-03-02 summary and
  inventory. The twelve architecture questions and interactions remain intact.
- **Evidence validity: revise for R1 below.** Existing findings retain their
  original scope; stale current architecture-closure and synthesis instructions
  are corrected without inventing results. The exploration/decision-evidence
  distinction, fresh-run requirements and independent replication remain intact.
  The milestone completion wording needs the acceptance distinction below.
- **Decision relevance: accept.** The next step is one readable behavior
  contract, provisional spec and bounded method. Track 04 cost sensitivity and
  Track 05 controls contribute to that same milestone, not separate campaigns.
  The new runtime budget is explicitly unassigned; closed comparison allowances
  do not fund it. Owner visibility covers material behavior, scope and budget
  choices while routine internals remain autonomous. Track 00's clarification
  permits predeclared scoped component comparisons without weakening the product
  success criterion or the workload/QEMU baseline obligations. Synthesis and
  production authorization remain separate.

**R1 — Assessed checks do not establish milestone completion.** Track 03's
completion paragraph requires a “reviewed scoped ABI” and runtime checks and
generated-fragment fit “assessed.” Review or assessment alone can include a failed
core behavior or an incompatible producer; that must not count as a successful
runtime/ABI milestone. Require independent acceptance against the declared
behavior and generated-fragment criteria, with no unresolved defect invalidating
the milestone scope. Retain partial-result reporting at the budget stop and the
distinction from whole-generator fit and Track 03 closure. Closure requires
reinspection of that paragraph and its budget-stop consequence.

No experiments, external sources, raw artifacts or archived history were read or
executed. `git diff --check` passed. No platform behavior, ABI design, experiment
budget, generator choice, completed architecture or production work is accepted
by this document review. Only this review record was edited by this reviewer.

**September 28 — R1 closed; final evidence-validity and decision-relevance
verdicts: accept.** Reinspected Track 03's milestone completion and budget-stop
paragraphs. Independent findings acceptance now requires the declared runtime
behaviors and representative generated-fragment fit to meet predeclared criteria,
with no unresolved defect invalidating the scope. Failed core behavior or
unestablished fit leaves the milestone incomplete; revision stays within budget
and material changes use owner alignment. Track 04 also now calls the native
execution advantage potential rather than established. `git diff --check` passed
again. No substantive blocker remains for the next drafting step. This accepts
the workflow's readiness and consistency, with all review limits above; it does
not make the as-yet unwritten ABI or unbudgeted experiment ready to execute.
