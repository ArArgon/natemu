# INV-01-08 pre-execution review thread

## Workflow migration review — 2026-09-05

Reviewer: `/root/investigations`, separate context from runner author
`/root/smp_shared`. Scope: migration runner changes only; this is not full
native-method clearance and creates no research evidence. The current scientific
conclusion and remaining work stay in [INV-01-08](../investigations/INV-01-08.md).

**Verdict: accept the runner migration within this scope.** No unresolved
migration blocker was found. Historical F08-2 and platform/SMP obligations remain.

Inspected the changes to `experiments/INV-01-08/README.md`, `run.sh`,
`src/supervisor.py`, `src/evidence.py`, `src/check.py`, `src/histogram.py`,
`src/launch_guard.h`, the native build changes, and focused fixtures. Also inspected
the retained standalone `INV-01-07/c1/clock_evidence.py` interface. Review documents,
review IDs, HEAD and clean-tree conditions no longer control execution. Preparation
copies source and local dependencies, compiles inside that snapshot, retains build
commands/SDK/toolchain and artifact checksums, and reexecutes the snapshot runner.

The v3 raw result and v2 build/campaign/supervisor formats bind the compiled source,
binary and common run identity. Each normal result requires the complete exact
control matrix with canonical member locations and independently checked control
outcomes. Cohort declarations are compared with the actual producing build and host;
a declared compatibility justification cannot conceal mixed builds. Historical input
paths remain explicit and cannot become new F08-2 evidence by omitting fields.
Missing clock evidence preserves independently checked correctness and prints timing
unqualified. A factual clock context match remains a candidate for human applicability
review, not acceptance of timing conclusions.

Validation: `sh validate.sh` in `experiments/INV-01-08` passed. It compiled and
inspected binaries and exercised pure checker/histogram/fallback fixtures, including
missing control members, mixed run/build/schema, tampered binaries, output boundaries,
non-overwrite, missing-clock classification, and real supervised dummy children with
a forced timeout. No native research campaign or performance sample was run.
Build/disassembly scratch artifacts were outside Git. No load-bearing runner edits
were authored by this reviewer.

Final preparation-failure return: the same reviewer inspected the small outer
exception handler that writes a failed-format build record when setup fails before
the compiler starts. An independently injected SDK lookup `OSError` produced the
explicit failed manifest and propagated the error in both runners. This preserves
partial-failure interpretation; the acceptance above remains unchanged.


## Full F08-2 native review — 2026-09-05

Reviewer: `/root/ordering_native_review`, an independent context from the F08-2
and migrated-runner authors. Applied the ponytail skill to keep remedies scoped
to defects affecting execution, interpretation or reproduction. No native probe
or research control was executed, and no load-bearing method/code change was
authored by this reviewer.

**Verdict: return for one bounded-execution correction.** The implemented
fallback oracle, native failure handling and retained-build/control identity are
suitable for the declared bounded correctness question. Native clearance remains
withheld until the campaign matrix and aggregate resource limits below agree in
the investigation, runner and operation instructions. Timing remains unqualified
without independently accepted applicable Q-01-07 evidence.

Inspected the research workflow, complete F08-2 method in INV-01-08, experiment
README, Makefile/run.sh/validate.sh, litmus.c, fallback.h, launch_guard.h,
fallback.py, supervisor.py, evidence.py, and checker/histogram/control fixtures.
The five implemented campaign children are full normal, sequential negative,
omitted store, injected lock error, and unexpected fault. The full normal includes
12 litmus arms, 6 LSE arms at 2/4/8 workers, 6 alignment probes, the four-worker
fallback and both 20,000-handshake QoS arms. It is not a three-repeat campaign.

### B1 — campaign scope and aggregate bounds are not yet executable

Evidence: the investigation opening next action says a fixed
`3-child-per-cell standalone/contended/handshake` matrix, whereas its F08-2
Collection section and supervisor.campaign specify exactly the five children
above. Per-child native watchdogs total 1,325 seconds (600 + 600 + 60 + 60 + 5).
They do not bound the complete campaign: preparation, environment capture,
classification and SDK/LSE feature lookup occur outside those watchdogs; the
SDK and LSE lookups currently have no timeout. No aggregate output cap is
specified or enforced. This prevents a reproducible declared execution envelope
and leaves potentially blocking preparation outside the stated stopping rule.

Smallest remedy: the author should state the actual five-child correctness
matrix consistently, declare total wall/output limits, bound remaining subprocess
lookups, and enforce campaign stopping/retained failure records without retries
or excluding a failed cell. A concrete review proposal is 45 minutes and 128 MiB
per campaign, totaling 90 minutes and 256 MiB for author plus independently
prepared replication; these are conservative operational ceilings, not expected
runtime or statistical sample sizes. The native child subtotal is 22 minutes
5 seconds; preparation has a 60-second compiler limit, eight environment
commands each have 10-second limits per capture, each child performs before/after
captures and codesign/context checks. The author may choose tighter justified
bounds. Proposed values in this review are not an accepted author declaration.

Closure condition: independently inspect the author's explicit matrix/budget
and supervision change, exercise timeout and output-limit failure with synthetic
children, and verify those failures terminate/reap the launched work, retain
partial diagnostics and suppress acceptance. This does not require another
historical review receipt or source-document hash.

### Native method and control assessment

The fresh fallback allocation and neighboring canaries eliminate the historical
alignment-probe contamination. Every target access participates in the same
mutex; the producer records actual writes, final value, per-worker API results,
readiness, completion and lifetime. The omitted-store control has an exact
999,999-write/value oracle. The lock-error control verifies the injected EIO
before any protected access and permits only consistent scheduler-dependent
peer progress. Create failure releases waiters through atomic abort. Failed join
exits without inspecting still-owned worker storage; unknown unlock ownership
cannot become a successful observation and is externally timeout bounded.

The signal handler's unexpected branch uses only `_exit(90)`. Expected recovery
requires an active probe window and fault address inside the four-byte target;
its address guard is statically always-lock-free. The separate PROT_NONE control
keeps the probe-window flag false and must exit 90 without raw result or normal
output. This control tests the unexpected branch, not every possible signal
interleaving or general recovery correctness.

Source snapshots include local checker/clock dependencies and are the actual
build inputs. Raw, invocation, build, binary, supervisor and exact five-control
packet identities are cross-checked. New evidence cannot enter the historical
reader by omission. Native correctness remains contingent on real normal/control
outcomes, independent findings review and independently prepared replication.
The litmus histogram/exposure rules retain inconclusive outcomes; a successful
campaign does not dispose of KA-06 or establish full guest TSO, mixed-access
compatibility, oversubscription, P1 applicability or useful performance.

Validation: independent `sh experiments/INV-01-08/validate.sh` completed with
`INV-01-08 preflight PASS`. It rebuilt optimized/verification binaries and
assembly, checked required instruction/handler symbols and exercised the
historical checker, histogram and fallback/control/identity fixtures. Compilation
used scratch output outside Git. This is pre-execution evidence only.

## Same-reviewer B1 return — 2026-09-05

Reviewer: `/root/ordering_native_review`; aggregate-supervision author:
`/root/mach_native_review`. **Verdict: accept the full bounded native F08-2
correctness campaign. B1 is closed; no substantive pre-execution blocker remains.**
Timing qualification remains a separate applicability/finding question, and
replication preparation still needs independent review before its execution.

Inspected the author's supervisor.py/evidence.py changes, new synthetic controls,
README and investigation declaration. The actual five-child matrix is now
consistent. A waiting outer supervisor starts before snapshot preparation and
owns the build, native probes and checking in one process group. Its 2,700-second
wall threshold and 128 MiB recursive output threshold are enforced, with 20 ms
polling and explicit retention of possible overshoot/final failure metadata.
SDK/LSE lookups now have 10-second timeouts. Final successful admission checks
elapsed time and bytes including the proposed final receipt. Timeout/output
failure kills the group and reaps its direct child; subsequent members remain
not-run. The full controls are validated within the outer budget.

The consumer requires the same-run v3 aggregate success receipt, complete five-mode
state, budget domains and retained checksums in addition to existing exact
build/child/control bindings. The private pre-finalization packet check cannot
qualify a result without the final outer receipt. This preserves partial-failure
interpretation and adds no review-document, HEAD or clean-tree condition.

After the coordinator/replication author confirmed C1 native handle 99740 terminal
exit 0, independently ran four focused Controls tests in fallback-fixtures.py:
aggregate dummy success/group timeout/output threshold, retained partial/not-run
schedule, missing/failed aggregate receipt refusal, and campaign run/build mixing.
All four passed. The dummy descendant heartbeat stopped after group termination.
These tests produced no native experimental evidence. The author's full
compile/disassembly and fixture validation also passed. No load-bearing patch
was authored by this reviewer.
