# Track 01 document migration — findings review

Reviewer: `/root/smp_shared`, independent of the investigation author
`/root/investigations` and shared-workflow author `/root`. Date: 2026-09-05.
Subject: migration of all 19 Track 01 investigation notes and relocation of
backlog/end-breadth implications. This is a migration review, not acceptance of
new experimental findings or permission to execute the pending instruments.
The reviewer authored INV-01-08/18 runner changes; those changes are excluded
from this review and require their separate independent review threads.

## Comparison and applicability

Compared each note against its pre-migration Git HEAD content, its current
opening summary, claim inventory, results, limitations and changed substantive
passages. Compared the removed BP-01-end tables and Track 01 backlog rows with
their new investigation/downstream homes. Inspected the pertinent outstanding
reviews (REV-200, REV-219, REV-225, REV-226 and preserved untracked REV-231),
together with the accepted corrections referenced below. No native experiment,
raw-data reinterpretation, new source claim or platform extension was performed.

All original full claim identifiers and raw-manifest references remain.
A mechanical comparison of claim-row assertion text and evidence labels found
only two changes: INV-01-19's `only if` became the narrower source-supported `if`,
and INV-01-22's `gated host capabilities` became `reviewed host capabilities`.
Neither promotes a claim. Historical reviews/manifests remain at their paths.

| Investigation | Preserved conclusion and consequential limit |
| --- | --- |
| INV-01-01 | Bounded public API observations and documentary F5 kill; one-host/signing scope, affinity failure, 64-vCPU ceiling versus actual parallelism, untested F3 mechanism and P1 remain. |
| INV-01-02 | Finite F1–F4 survival/F5 kill/F6 exclusion and accepted granule interpretation remain; survival is no full-matrix admission. KA-05/06 and F3 composition/distribution remain open. |
| INV-01-03 | Reservation versus 2 GiB backing, aliasing and 16 KiB boundaries remain; no fully backed 8 GiB guest, VM-map SMP result or usable mincore dirty mechanism is inferred. |
| INV-01-04 | Original generated-code capability/per-thread state observations remain in the ad-hoc non-hardened cell; distributed signing, callback writing, live patching and larger SMP remain untested; timing replication limits remain. |
| INV-01-05 | Protection/software-check mechanisms remain distinct from excluded mincore; discarded slowdown magnitude, negative marginal and un-emitted sign-consistency field remain visible. |
| INV-01-06 | Signal chosen-PC/skip result remains distinct from exactly-once restart and API-only Mach success. Delivered-Mach supplement remains unobserved, with pending implementation/ownership/negative-control obligations. |
| INV-01-07 | Two I16 failures and MAN03's defect-limited historical PASS are not accepted timing. Strict-deadline/positive-RTT flaws, pooled SMP misses, absent achieved-rate/per-thread dimensions and clock-only C1 scope remain. |
| INV-01-08 | KA-06 remains open; background MP/LDAR-STLR bounded corroboration, zero UI exposure and inconclusive SB remain. Historical fallback/T3 timing is unqualified; accepted code preparation does not become a fresh result or replication. D1 below repairs a conflicting historical zero-case sentence. |
| INV-01-09 | Socket/pty/block/entropy observations and vmnet failure/SIGKILL remain; EINVAL discrepancy, absent vmnet positive control and sockets' lack of L2 contract remain. |
| INV-01-10 | Partial tool inventory and MAN03 provenance correction remain; absent tools, nonfunctional xctrace and unmeasured tool fields are not upgraded. |
| INV-01-11 | Conditional Model A/B/C compatibility remains conditional; zero universal incompatibilities does not settle Intel/AMD/Arm notices, bundled libslirp copyright or an actual transitive dependency tree. |
| INV-01-12 | Bounded two-vCPU F2 composition/exactly-once exercised instruction results remain; stochastic negative miss, missing deterministic controls, aligned same-page envelope, no Linux boot and no F3 screening remain. |
| INV-01-13 | Source-only guest contract options remain unselected; exact stock configuration, PCI/ACPI, APIC-ID, wall-clock/timer and networking composition unknowns remain. |
| INV-01-14 | NOFILE/CPU enforcement and failed AS/DATA lowering remain distinct; one-GiB accounting is no full guest containment budget. Memory supervisor sufficiency, 8 GiB/two-peer capacity and larger SMP remain open. |
| INV-01-18 | Documentary distinctions and reviewed method supplements remain distinct from unobserved protocol/cost findings. Allocation, retirement oracle, actual refusal and pressure-fixture properties require independent return; larger composition and signing/P1 scope remain open. |
| INV-01-19 | P/D separation, slot re-entry, stack/depth rules and staged budgets remain. REV-231's closure of R1–R6 is preserved without changing its historical revise verdict; its stale internal citation is corrected editorially. No JIT-state preservation or Mach/protocol observation is inferred. |
| INV-01-21 | Source packet remains bounded and method corrections remain independently unchecked. Diagnostic-only arms, T1-only detectability, unreachable frozen, 2-of-3/300-ms stalled and mach_continuous_time-only backward falsifier remain. |
| INV-01-22 | Four time policy families remain unranked/unadmitted, with exact guest and host/lifecycle inputs unresolved; KVM material remains risk/alternative input only. |
| INV-01-23 | QoS/affinity/CPU samples remain distinct from placement persistence/fairness/liveness; deterministic schedule, censoring, limits and execution budget are still missing. |

Specific cross-record corrections checked: INV-01-02 KA-07 now excludes
CLM-01-03-14 mincore consistently with the already accepted exclusion in
INV-01-05's kill-assumption discussion. KA-13 now reports the already Observed
failed AS/DATA lowering in INV-01-14 rather than stale `enforceability open`
wording. Neither correction adds evidence or declares the overall assumption
satisfied. INV-01-19's `if` direction matches the actual source wording quoted
and evaluated in REV-231; shared-stack nesting remains an experimental
assumption rather than the unsupported converse contract.

The substantive BP-01-end implications remain in their owning notes: sockets
and network licensing; host RAM/two-peer containment; conditional distribution
optionality; signing/single-region/shared-code scope; F3 and public-API kill
wording; timer and SMP measurement gaps; restart/granularity limits; placement
versus denominator tension; uncaptured load 2.43; timing replication limits;
and review-context concentration. Downstream question content outside Track 01
is retained. Track 01 indices contain titles/links, and the old roll-up no
longer supplies competing live approval state. Track completion still requires
breadth checking and is not claimed by this change.

## Return D1 — contradictory zero-case interpretation

Consequence: the new INV-01-08 retained-breadth paragraph could tell a fresh
reader to classify zero/zero both-observed fractions COMPARABLE, contradicting
the operative amended method's INCONCLUSIVE rule and changing what can be
claimed from empty overlap observations.

Evidence: the migration-added sentence `A zero both-observed count in both
arms can yield COMPARABLE`; INV-01-08 Method's explicit zero rule and the
retained reporting fixture for `classify(0, 0)` establish the contradiction.
The old breadth critique was historical, not an exception to that amendment.

Smallest remedy: mark COMPARABLE as the pre-amendment historical concern and
state the current zero/zero INCONCLUSIVE rule; preserve I2 as its independent
positive-control obligation. Author notified in this thread's review context.
Closure condition: reread the changed paragraph and operative method and find
one consistent current rule, without losing the historical concern.

D1 return inspected in the same reviewer context: the changed paragraph now
labels zero/zero COMPARABLE as pre-amendment historical behavior, states current
zero/zero INCONCLUSIVE and retains independent I2. This matches the operative
method without expanding any evidence. D1 closed.

Verdict: accept the document migration within this review's scope. No unresolved
migration blocker remains. All original scientific blockers above retain their
own investigation status; this review does not close them. The new INV-01-06/07
migration-validation links name only bounded runner checks and explicitly leave
native research open; they are no scientific-acceptance upgrade. Reviewer editorial edits after acceptance: replaced INV-01-01's retired policy/guide
locator with the authoritative workflow; removed INV-01-02's obsolete claim-annotation
instructions and duplicated approval/adoption status tables, retaining the Hypothesis
reasoning, exact named supporting claims, bounded source/prototype findings and
scientific dependency limits. Replaced the remaining prospective eight-gate wording
with independently reviewed evidence. These edits change navigation/workflow language,
not evidence assertions or their applicability; no additional reviewer is needed for
a load-bearing authored change because none was made.
