# INV-01-18 pre-execution review thread

## Workflow migration review — 2026-09-05

Reviewer: `/root/investigations`, separate context from runner author
`/root/smp_shared`. Scope: migration runner changes only; this is not full
native-method clearance and creates no research evidence. Current scientific
blockers and pending native-code review stay in
[INV-01-18](../investigations/INV-01-18.md).

Inspected `experiments/INV-01-18/build.py`, `run.py`, `check.py`, the native
`src/protocol.c` identity changes, README, coverage use and focused fixtures.
Preparation retains sources/dependencies/build inputs, compiles from the snapshot,
creates signing variants, and retains toolchain/SDK/commands/signing information.
Execution reuses that snapshot runner and checks the actual retained binaries and
signatures. Child and campaign schemas explicitly advance to v2, including common
campaign run identity and compiled producing-build identity. Boundaries, exclusive
output creation, process-group supervision, timeout/resource ceilings, failure
artifacts and correctness/control checks remain. Missing clock evidence leaves
costs unqualified while independently justified correctness can proceed.

### R18-1 — caller-declared eligibility bypassed retained controls

Initial verdict: revise. The correctness/cost consumer checked supplied references
but trusted `eligible_cells` and `correctness_stage_included`. A qualification packet
with the correct source/build/host, an empty `campaigns` list, arbitrary eligible
cells, and `correctness_stage_included=true` bypassed all prerequisite stages.
Consequence: native correctness/cost work could run without its required controls or
prior correctness stage. Evidence: the consumer's loop over supplied references had
no mandatory-stage check and never recomputed the derived eligibility.

Smallest remedy: derive qualification from verified campaigns through one shared
function; have the consumer recompute and compare the complete supplied object before
launch. Closure condition: omitted stages and caller-edited eligibility or correctness
flags are rejected before any child; genuine complete packets retain their behavior.
The reusable independent consumer fixture was handed to the author at
`/tmp/natemu-inv18-qualification-review.py`; it uses synthetic build/context metadata,
patches native execution to fail if reached, and calls the real `main()` consumer.

### Return and closure

The same reviewer inspected the author's return: `qualification(campaigns,sources)`
now verifies all referenced campaigns, requires the full frozen
preflight/hooks/negative/protection stages, derives eligibility and correctness-stage
presence, and returns the complete object. `main()` recomputes it and rejects any
inequality before launch. R18-1 is closed.

Independent validation after the return: the consumer fixture rejected empty
campaigns and edited eligibility/correctness declarations before any child. Existing
`make syntax model fixtures` passed (34 trace checks, 22 protection checks, six clock
cases, three negative-classification cases, plus campaign identity/coverage fixtures
and bounded abstract models). The syntax check compiled no research evidence; no
native campaign or performance measurement ran. Snapshot/build/context and historical
schema rejection paths were inspected. A clock context match only supplies a factual
candidate; reviewers still judge instrument applicability and conclusions.

**Final verdict: accept the runner migration within this scope.** No unresolved
migration blocker remains. This does not close historical native-method findings or
accept any new experimental conclusion. No load-bearing runner edits were authored
by this reviewer.

Final preparation-failure return: the same reviewer inspected the small outer
exception handler that writes a failed-format build record when setup fails before
the compiler starts. An independently injected SDK lookup `OSError` produced the
explicit failed manifest and propagated the error in both runners. This preserves
partial-failure interpretation; the acceptance above remains unchanged.

Regression retention: the independent consumer fixture is now preserved in
`experiments/INV-01-18/tests.py` as `qualification_consumer_fixtures()`. Codex
`/root` independently reran that persisted fixture after incorporation: empty
campaigns and edited eligibility/correctness flags were rejected before any child.

## Native preparation return — 2026-09-05

Reviewer: Codex `/root/publication_native_review`, a separate context from the
method/native authors and migration authors. Reviewed the complete current
`protocol.c`, runner/build/qualification path, streamed checker, coverage design,
abstract models, fixtures and README against M18-2/P1/N1 and the substantive
REV-200/REV-226 return. This reviewer authored no experiment or method change.
Only this review thread was edited. Historical review receipts and repository
HEAD are not execution requirements under the current workflow.

**Verdict: revise before native execution.** The substantive historical return
below is accepted within its stated bounds. R18-N1, the still-missing bounded
campaign/output plan, remains open. This is no native protocol, timing, host,
or Track 01 completion finding.

### Historical return disposition

- **REV-200 R1: accepted at the minimum remedy actually requested.** The
  checker independently derives immutable token and request counts from the
  sample/topology/pressure plan, compares against actual usage, cross-checks
  the two reported byte totals and rejects a descriptor/request byte
  undercount. The C allocation expression includes descriptor lifetime bytes,
  optional baseline pair storage, both slot arrays, retirement clocks,
  compiled trace/worker sizes and the 1 MiB reserve before allocating those
  arrays. This is not a byte-for-byte independent reconstruction of all
  compiled layouts: changing both reported byte totals together while leaving
  the lower bound satisfied can still escape the checker. The source/build
  inspection supports that remaining part of the accounting; a checker PASS
  alone must not be described as independently proving every allocation byte.
- **REV-200 R2: accepted only as the explicitly permitted limitation.** The
  code and README now disclose that admission checks its own acquired life
  state and page floor; actor-local trace order cannot prove global retirement
  order. The retained characterization fixture intentionally admits a trace
  whose independent coordinator retirement precedes later reader records.
  The SC hazard model and real held-reader hook obligations remain necessary.
  A clean trace establishes no general absence of retirement races. Findings
  must carry this limitation beside any lifetime conclusion; the full question
  remains open, and this return does not remove its lifetime requirement.
- **REV-200 R4: closed by source inspection.** `scratch_reserve()` invokes
  `find_eligible_slot()`, the same actual search invoked by `allocate_desc()`.
  It includes I's real retirement eligibility attempt and forbids the outer
  near-full release fallback. It must fail to find a slot before emitting
  `allocator-reject`. The subsequent I/E/V pressure replacement exercises the
  ordinary no-free/release/allocation/publication/call path. Scratch remains
  BUILDING then DISCARDED and cannot supply execution-right evidence.
- **REV-200 R5: accepted for the requested durable pressure branch coverage.**
  Positive near-full and fragmented traces and four pressure corruptions now
  live in `tests.py`. The S/M in-place and multiworker V owner-transfer checker
  branches still lack direct trace-fixture coverage; the separate abstract
  pressure model covers those arm/topology cases but is not their checker.
  Add such fixtures before using a return in those branches to claim wider
  regression coverage. This is a disclosed coverage limit, not an observed
  defect in those current native paths.
- Retained snapshot compilation, actual signatures/entitlements, producing
  build identity, exact source controls, partial failures and qualification
  derivation were reinspected. REV-200 R3/R6's old git/review-prose mechanics
  are retired, rather than blockers to reinstate.

`make syntax model fixtures` passed independently: 34 trace checks, 22
protection checks, six clock cases and three negative-classification cases;
the models enumerated 16,632 SC schedules and detected the broken retirement
variant, 960 epoch transitions, 1,536 instruction-field combinations and 56
pressure cases. Independent calls of the pressure fixtures reproduced both
positive results, token undercount rejection, inconsistent-byte rejection and
the missing actual-call-witness rejection. These are synthetic observations.

A fresh build from retained inputs at
`/private/tmp/natemu-inv18-review/build-native-review` compiled, signed A/B/C
and produced disassembly without executing a binary. The reviewer inspected
the generated reader/retirement/publication paths: entry/hazard and mailbox
operations use actual AArch64 atomic instructions, and the reader contains
the explicit ISB before its generated call. The callback uses the approved
callback API and cache maintenance remains in the writer path. This does not
establish cross-PE runtime cache coherence. No preflight, hook, protection,
negative, sensitivity, correctness or cost instrument was run.

### R18-N1 — aggregate schedule and output are not operationally bounded

**Consequence.** A permitted stage can fill the host's filesystem or consume
many days without a declared campaign stop. A 30-second child watchdog and
the native 256 MiB metadata/trace-buffer cap do not bound streamed disk output,
Python checker storage or total wall time including context capture/checking.
`byte_estimate()` explicitly says it is not a disk budget, and extra dispatcher
calls are not bounded by that estimate. `run_child()` currently writes directly
to files and waits without a byte ceiling; `main()` has no campaign deadline.

**Evidence.** Independent enumeration through `jobs(manifest(), stage)` gives:

| Stage | Children | Sum of 30-second child ceilings |
| --- | ---: | ---: |
| Preflight | 7 | 210 s |
| Hooks | 42,876 | 1,286,280 s |
| Negative | 36 | 1,080 s |
| Protection | 1,944 | 58,320 s |
| Sensitivity | 1,446 | 43,380 s |
| Correctness | 2,410 | 72,300 s |
| Cost including baselines | 7,230 | 216,900 s |
| Total | 55,949 | 1,678,470 s (19.43 days) |

The current planning estimates are 92,380,004,352 bytes for hooks,
251,424,931,840 for correctness and 553,974,988,800 through
368,811,127,603,200 for cost at the four possible batch sizes. These estimates
are not upper bounds. The current host has about 68 GiB free on the external
temporary-output volume, less than the hook estimate alone. This is a concrete
reason to stage execution; it does not justify silently shrinking coverage.

**Smallest proposed remedy for author review.** Start with the existing full
seven-child `preflight` stage only, whose predicted output is 4,071,424 bytes.
Preserve its exact jobs, four warmup rounds, 32 publication rounds and 128
post-warmup calls per reader. Proposed operational limits: 30 seconds per
child, 15 minutes for this entire stage, 256 MiB combined stdout/stderr per
child, 2 GiB for the complete campaign, and 1 GiB minimum remaining free space.
Enforce output limits while the child is live and reserve space for failure
records; stop with retained partial output and explicitly unexecuted jobs on
timeout, byte limit, low space or any unclassified invalidity. Bounded context
capture/checking must be included in the wall-time limit, not left as an
unbounded gap between supervised children. Record exact caps, planned count,
actual consumption and the stopping cause in retained run/schedule/results.
These numbers are a reviewer proposal, not an independently accepted
self-authored method amendment.

This first stage can expose an unavailable API or a concrete protocol/checker
failure at the original one-reader/A-signing baseline. A clean result only
advances preparation of the next frozen hook/negative/protection tranche. It
cannot qualify stress, discharge missing SMP/pressure/signing cells, or close
Q-01-18. All 55,949 original obligations remain recorded until accepted scoped
resolution or accepted kill; additional stages require justified aggregate
time/output budgets rather than an automatic launch of the expanded matrix.
Quantitative cost still waits for applicable accepted clock evidence and local
calibration.

**Closure condition and reusable checks.** The author incorporates a concrete
first-stage budget in the investigation and runner; this reviewer inspects the
return. Synthetic supervisor fixtures must force a child byte cap, cumulative
campaign cap, deadline and low-space stop, show the owned child terminates and
its partial artifacts/status survive, and show no later child starts. A tiny
valid synthetic child must still complete. Fixture schedules must demonstrate
that a partial tranche cannot satisfy full-stage qualification. No native
publication experiment is necessary to test these supervisory conditions.

### R18-N1 author return — same reviewer, 2026-09-05

The same independent reviewer inspected the author's `STAGE_BUDGETS`, live
process-group supervision, OS file limits, campaign wall timer, budget receipt
and v3 consumer, failure retention, README and investigation amendment.
Only the seven-job preflight stage has a budget. Other stages are refused
without removing their scheduled obligations. Each stdout/stderr file has a
kernel-enforced 128 MiB ceiling (at most 256 MiB combined per child); the
seven-child native-file maximum leaves room below the 2 GiB campaign threshold
for its fixed metadata. The 1 GiB free-space reserve and campaign byte count
are checked during child supervision and between children. Native unknown,
capacity, unavailable and invalid outcomes stop further preflight execution.

The 900-second wall timer encloses per-job context checks, child execution and
checking. Initial retained-build/signature/context preparation is explicitly
outside that stage timer, with bounded external commands. Terminal inventory
and filesystem cleanup are operational overhead, not an asserted realtime
guarantee. A wall or resource exception kills and waits for the owned child
group, records the child's terminal status and preserves the campaign failure
and unexecuted schedule. These are operational limits, not protocol liveness
or timing claims.

Independent validation: the author's synthetic budget fixtures passed for a
valid child, wall deadline, aggregate output, low disk reserve and actual OS
file-size refusal. An additional reviewer fixture exercised the real `main()`
campaign loop with a dummy Python child and a 0.2-second deadline. Exactly one
child launched and was killed; its partial trace and exit status survived;
all seven planned outcomes, failure and manifest were retained; no next child
launched. Reusable fixture:
`/private/tmp/natemu-inv18-main-budget-review.py`. The full-stage consumer
continues to reject a partial preflight schedule and refuses to derive stress
eligibility from preflight alone. No native protocol child ran in this review.

**Return verdict: accept execution preparation for the complete seven-child
preflight stage only; R18-N1 is closed for that stage.** The historical native
return's explicit oracle/accounting limitations remain. This accepts neither
the larger hook/pressure/SMP/stress/cost execution campaign nor a protocol
finding. Those retained stages still require their budgets and any applicable
pre-execution return. The reviewer authored no load-bearing implementation or
method change in this return.

## R18-F1 cause-specific failure witness return — initial review, 2026-09-05

Independent reviewer `/root/publication_native_review` inspected the native
synchronous `mprotect_failure` reporter, its exact two-call site, stderr
consumer and fabricated-outcome C fixture. The reporter correctly distinguishes
failed PROT_NONE from failed restoration and explicitly records a skipped
restore as null outcomes. The full synthetic suite passes (34 trace checks,
22 protection checks plus budget and new reporter fixtures). No native
experimental execution was performed by this review.

The initial return still needs one consumer correction before a fresh
preflight: exit 78 currently overwrites a `PRODUCER_IDENTITY` failure with
`unavailable`. An independent real dummy-child fixture with wrong run/build
header and otherwise shaped failure record reproduced top-level `unavailable`
with nested identity failure. The witness also accepts a 16 KiB JIT extent
although this call site protects the full 16 MiB arena. Preserve invalid
producer identity at top level and validate the witness's restore protection
and extent against its selected sub-arm/header, with runner-level missing/
wrong identity and swapped-context fixtures. These are evidence-attribution
and exact-call controls, not receipt/HEAD enforcement. The initial campaign
remains immutable, and its ambiguous failure is not repaired retrospectively.
This return is **pending those small corrections**, with unchanged seven-job
preflight limits and no broader stage permission implied.

### R18-F1 correction return — accepted, 2026-09-05

Reinspected the author's consumer correction. An explicit producer identity
boolean now gates unavailable classification; wrong/missing run/build identity
stays top-level INVALID. The failure witness admits only the selected M
protection sub-arm, expected restoration bits, exact full-arena or data-page
extent and aligned address. The native synchronous reporter and two-syscall
ordering are unchanged. No address authenticity beyond the inspected owned
allocation/call site is inferred from this diagnostic record.

Reran the complete synthetic suite successfully, including the actual compiled
reporter with fabricated protection-failure and restore-failure inputs, all
missing-field cases, attempted/skipped contradiction, historical ambiguous
stderr, wrong sub-arm protection/extent and the real dummy-child consumer for
valid/wrong-run/wrong-build/missing-header cases. The latter now retains invalid
identity; valid context distinguishes the exact failed syscall. These tests
perform no native publication or protection experiment. Existing wall/output/
reserve/process-group fixtures still pass.

**Accept this R18-F1 instrument correction for one fresh complete seven-job
preflight under the already reviewed 900-second/2 GiB stage budget and unchanged
per-child limits.** Use a fresh producing build and preserve the original failed
campaign; the new failure witness cannot repair historical missing observations.
Stop on the first unqualified/unavailable/invalid job and retain every remaining
not-run row. This is method/code acceptance only, with no findings acceptance,
retry-until-success policy, broader stage or full-family availability conclusion.


## Original four-job suffix amendment — independent return, 2026-09-05

Reviewer: Codex `/root/mach_native_review`, independent of amendment author
`/root` and all M18 native/runner authors. The original reviewer context
`/root/publication_native_review` was unavailable after repeated collaboration
`agent thread limit reached` failures. This reviewer read its complete prior
returns and unresolved oracle/coverage limits, independently accepted the
retained MAN-01-18-02/03 diagnostic findings in the findings thread, and then
inspected this amendment. No native execution or substantive implementation
authorship by this reviewer.

**Verdict: accept pre-execution preparation for one exact four-child
`preflight-remaining` continuation.** It is an explicit staged amendment
covering previously unexecuted separate-child operations, not a replacement
for E/S observations or the unavailable MAP_JIT attempt. The existing partial
campaigns remain immutable and cannot be merged into a claimed all-positive
preflight. No full protocol/cost qualification or question completion follows.

Compared the current source tree to the retained R18-F1 producing snapshot.
The native `src/protocol.c`, checker, coverage, build and local dependency
inputs are unchanged; substantive code changes are confined to the named
stage's budget/selection/order guard and corresponding tests. The new stage
selects exactly the original seed18001 shuffled entries 3–6:
M-data-protect, M-data-map, V, I, in that order. Every complete job record and
native argument is unchanged: signing A, one reader/producer, rate 1,
256-byte blocks, normal pressure and existing four-warmup/32-publication/
128-call floors. Native mode remains preflight while campaign evidence mode
is explicitly preflight-remaining. The producer continues to execute the
previously inspected owned data-window remap/protection and V/I paths; the
MAP_JIT failure does not imply that these distinct operations fail.

The runner skips a second shuffle only for this explicit suffix. The retained
campaign consumer requires its exact ordered schedule and existing complete
source/build/run association. The qualification consumer still requires the
full original preflight/hooks/negative/protection stages; four successful
suffix results cannot satisfy that preflight prerequisite. No old unavailable
result is turned into success or silently excluded from full-method coverage.

Reviewed bounds: four 30-second child ceilings, 120 seconds summed native
ceilings; 900-second stage-loop timer including per-job context/checker work;
128 MiB kernel ceiling on each native stdout/stderr file; 2 GiB aggregate
operational output threshold and 1 GiB free-space reserve. Initial retained
build/signature/context commands have separate timeouts; terminal inventory is
operational overhead, not a realtime guarantee. First unavailable/invalid or
resource outcome stops later admission and preserves partial evidence and
not-run jobs. The stage uses no clock packet and supplies no timing finding.
The method reserves a separately prepared and independently checked identical
replica, for combined ceilings of 240 native seconds, 1,800 stage seconds and
4 GiB operational output thresholds; this does not pre-accept its future
producing build or findings.

Independent validation: `make syntax model fixtures` passed, including
34 trace, 22 protection, six clock and three negative-classification cases,
source/qualification/resource fixtures, the fabricated C failure reporter and
dummy-child consumers. The abstract models retained 16,632 SC schedules,
960 epoch transitions, 1,536 instruction-field combinations and 56 pressure
cases. Verified the exact complete-job suffix comparison and rejection of
suffix-only qualification. An additional independent adaptation of the
previous reviewer's real-main deadline fixture used the new stage and a
0.2-second timer: exactly one dummy Python child launched and was killed,
its partial trace/exit and all four outcomes/failure/manifest survived, and no
later child launched. Reusable synthetic fixture:
`/private/tmp/natemu-inv18-remaining-budget-review.py`. No fixture ran the
native publication/protection protocol.

No new substantive blocker was found for this exact bounded continuation.
Execution requires a fresh retained build containing this reviewed runner,
source identity and unchanged native instrument, plus the usual exclusive
native-slot coordination. Stop without replacement on the first failure.
Later findings require independent review and the separately checked replica.
The prior retirement/accounting caveats, absent wider trace fixtures, full
hooks/controls/pressure/topology/signing/host obligations and aggregate budgets
for larger stages remain explicit; this suffix cannot discharge them.
