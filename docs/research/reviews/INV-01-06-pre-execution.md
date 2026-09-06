# INV-01-06 — pre-execution review thread

- Reviewer/context: Codex `/root`, independent of runner author `/root/clock_mach`.
- Date: 2026-09-05.
- Scope: workflow migration of `experiments/INV-01-06/m1`; snapshot/MIG builds,
  external output, child supervision, build/run association and failure records.
  This is not full native-method clearance or a delivered-Mach findings review.
- Material inspected: build/run diffs, retained checker/coverage, source snapshot
  and dummy-child fixtures; the native server was not executed.
- Verdict: accept for the migration scope; native-method clearance remains open.

## Migration corrections and preserved limits

The runner now uses the retained source build and executes its retained runner,
records one campaign identity and producing build, verifies scheduled child inputs
and process completion, and retains explicit incomplete/UNRESOLVED outcomes.
Historical formats cannot satisfy the new build loader. Exact SDK byte pinning is
replaced by selected SDK/toolchain recording, retained MIG input and generated wire
layout `_Static_assert` checks; changed semantics still fail the layout checks.

Source snapshot copying still occurred before the build failure handler, so a copy
failure could leave partial inputs without a recorded outcome. Smallest remedy:
include preparation in failure handling and retain partial checksums. Closure is
an inspectable failure artifact for a failed snapshot/build.

The original checker does not prove all required lifecycle properties, negative
and terminal oracles remain incomplete, and the native binary does not implement
the full signing-cell selection. These are substantive research limitations,
retained in the investigation; the migrated runner explicitly declines to report
complete native validation. This review does not resolve those defects by approval.

## Returns

Same-reviewer return, 2026-09-05: preparation now falls inside the failure
handler, and the v2 failure record retains checksummed partial artifacts.
Independently injected a source-copy failure after writing a partial file and
verified the preserved checksum and hashing-complete flag. Both focused tests
passed on independent rerun: retained build tampering/historical rejection and
dummy-child wrong-exit/wrong-input/non-overwrite/control outcomes. Inspection
confirms the compiler uses the retained C source, MIG uses the retained defs,
and subsequent campaigns execute the retained runner/checker. The author also
reported a successful build-only SDK/MIG/static-assert/compile/disassembly run.
No reviewer-authored code change, Mach experiment or performance measurement
was performed. The substantive native limitations above remain unresolved.

## Native implementation review — 2026-09-05

- Reviewer/context: Codex `/root/mach_native_review`, separate from the native
  implementation and migration authors; assigned an independent pre-execution
  review for the goal of completing Track 01. No implementation changes authored.
- Inspected: research workflow, complete current M06-M1 method in INV-01-06,
  REV-188's accepted method obligations, relevant REV-228 validation claims,
  current `m1/{build,run,coverage,check,test_runner}.py`, complete
  `m1/src/state_server.c`, and freshly generated MIG server/client/headers,
  layout assertions, compiler log and disassembly.
- Independent self-check evidence: build-only completed successfully at
  `/private/tmp/natemu-m06-review-74tzruv4/build`; retained build manifest
  `7eca54b7b49d47738b9637362968e8bb`, SDK 26.4, Apple clang
  21.0.0 (clang-2100.0.123.102), MIG bootstrap_cmds-138. All three
  `python3 -B -m unittest discover -s experiments/INV-01-06/m1 -p 'test_*.py' -v`
  tests passed. These are build and synthetic observations only. No Mach
  exception experiment, JIT execution, native fault, or performance run occurred.
- **Verdict: return for changes. No native matrix is accepted for execution.**
  Earlier migration acceptance remains confined to its stated scope. Pending
  REV-229 is not a defect; the concrete defects below block execution validity.

### N1 — lifecycle prevents forwarding completion and valid restoration

**Evidence / consequence (inferred from inspected code).**
`perworker_server_thread` serves `events` front requests and then `events`
prior requests, while `prior_server_thread` already serves those same prior
requests. Only `events` prior requests are produced: the two consumers require
`2 * events`, so a successful positive forwarding run cannot join both.
The serial branch passes `&serial_a`, a block-local object whose lifetime ends
before the server is joined. Every worker returns before main joins workers
and calls `teardown_worker`; the intended restoration must occur while the
worker thread is still alive. `restoration_proof` is an unconditional success
log, runs before teardown, and produces no restored-prior event. Registration
never installs the owned prior before the front or captures it as the saved
handler. These paths cannot establish the method's forwarding/restoration claim.

**Smallest remedy.** Delete the duplicate prior receive loop; retain the serial
argument through join; keep workers parked, completed and disarmed until their
port restoration and the final restored-prior event finish. Install/snapshot
the owned prior before installing the front, then restore the initial state
after the proof event. Restore on the living worker or retain a controller
handshake that proves it remains alive.

**Closure.** Synthetic lifecycle/queue checks reject a second consumer,
expired server argument, restoration after worker exit, and a success record
without the actual post-restore event. Reinspect the concrete implementation
before the corresponding native positive/restore arms run.

### N2 — attribution and resumed-state assertions are not implemented

`catch_mach_exception_raise_state` checks exception/count/flavor and a shared
outstanding flag, but never reads the delivered PC, SP, register cookie,
nonce, fault address or error category. `sp_lo/sp_hi` are never populated;
assembly never installs the declared cookie/nonce. Reading the descriptor's
nonce back is not comparing it with independent delivered state. The worker
always reports `sentinels_ok:true` and sets `sp_ok=1`. `expected_pc` is written
after release-publishing the outstanding event. Logs retain summary booleans
instead of the request/decoded-state/reply material the method requires.
Consequently an unrelated helper fault can be misclassified and preservation
can be reported without measurement.

**Smallest remedy / closure.** Use a fixed auditable assembly wrapper that
installs cookie and event nonce, exports the exact fault label, checks saved
sentinels/SP after retry, and supplies a complete descriptor before release
publication. Compare SDK accessor PC/SP, registers, code category/address and
nonce before repair; retain enough decoded/raw state for independent equality
checks. Synthetic mismatches for each field must fail individually; an
unchanged-state valid fixture must pass. Compile/disassembly must corroborate
the actual fault labels and register checks.

### N3 — positive checker accepts false evidence

Independent synthetic observations: all six mutations below return `[]`
(no errors), using `check.good_trace_rows()`:

```python
import sys
sys.path.insert(0, "experiments/INV-01-06/m1")
import check
r = check.good_trace_rows()
fixtures = {
    "summary-only": [r[0], r[-1]],
    "missing delivery": [x for x in r if x["kind"] not in
        ("receive", "identity", "repair", "reply", "teardown_restore")],
    "duplicate commit": check._duplicate(r, "commit"),
    "invalid identity": check._mutate(r, "identity", valid=False),
    "failed disposal": check._mutate(r, "disposal", kr=5),
    "bad resumed state": check._mutate(r, "resume",
        sp_ok=False, sentinels_ok=False),
}
for name, rows in fixtures.items():
    print(name, check.two_pass_check(check._rows_to_text(rows)))
```

`run_child` adds matching metadata/summary counts but does not repair those
event-level omissions; a summary-only matching fixture remains insufficient
evidence. Events with duplicate kinds overwrite earlier dictionary entries;
receive/reply records have no event key, and global counts do not establish
one reply per receive.

**Smallest remedy / closure.** Add explicit worker/event/role identities to
every stage, require the complete ordered state transition for each scheduled
event, reject duplicates/orphans/missing workers, verify actual state and
restoration comparisons plus successful complete ownership disposal. Derive
counts from records, then compare the summary. Preserve these six fixtures
as rejection checks alongside the method's remaining named mutations and a
complete valid trace. No need to introduce a new checker framework.

### N4 — negative and terminal arms are placeholders or violate the method

`corrupted-repair-result` flips `new_state[0]` and returns success, violating
the reviewed prohibition on injecting arbitrary raw thread state. Corrupt
the owned data result instead. `unexpected-helper-fault` only assigns a local
pointer and never accesses it. `server-stall`, `wrong-event-descriptor`,
`receive-right-destruction`, `reply-send-failure` and `prior-server-failure`
have no native seams. `prior-handler-unsupported` installs no decoy.
`run_terminal_diagnostic` only logs a sentence and exits zero without a
predeclared fault or failed reply. `native_args` schedules restoration and
prior-server-failure with `--path direct`, making their intended subjects
absent. Returning KERN_FAILURE on unexpected identity/repair failure is not
the required terminal child exit policy; logs alone do not end the child.

**Smallest remedy / closure.** Implement each named seam at its real lifecycle
point, choose the forwarding path where required, and give every arm a
specific trigger, last durable record and allowed terminal outcome oracle.
A positive run with an ignored negative flag must fail control validation; a
crash/timeout without reaching the seam cannot count as the expected negative.
Keep terminal observations distinct from successfully resumed events. Reinspect
these implementations and synthetic trigger/outcome fixtures before execution.

### N5 — holds and failure supervision do not implement the bounded method

The enumerated plan has 321 executable children and 32 excluded cell rows,
with **zero `--holds` invocations**. The hold implementation pairs two callers
of the same point rather than holding one actor until a competitor's recorded
attempt. With one worker or one serial server there is no second caller at
several points. There is no hold before right disposal, no controller evidence
of unrelated per-worker progress, and no rotated-versus-released-together
admission schedule. The parent enforces a 30-second total timeout but not
5-second no-progress. Worker page-allocation failure returns without setting
`registration_done`; pthread creation and several protection/port-set API
results are ignored. These failures may spin or wait until the total ceiling
instead of being classified at their cause.

**Smallest remedy / closure.** Schedule each applicable hold identity and
repeat explicitly; use controller/actor handshakes with recorded competitor
attempt and release, not same-point rendezvous. Check creation/protection/
registration outcomes before admission, set completion on every setup exit,
and implement the declared no-progress watchdog plus output/resource bounds.
Test these controls synthetically and expose the complete finite child matrix,
aggregate maximum and stopping behavior in the method/manifest. No native
run is needed to reveal the current zero-hold coverage.

### N6 — signing cells are labels on one unverified binary

`build.py` produces a single linker-signed executable and does not construct
or verify the Hardened Runtime plus allow-jit cell. `native_args` changes no
binary with the scheduled signing value. `coverage.cell_eligible` excludes
non-hardened immutable-JIT by assertion rather than an applicable capability
result or source basis for that exclusion. The requested matrix is therefore
not what would execute.

**Smallest remedy / closure.** Produce and retain independently inspected
signatures/entitlements for the two local signing cells, bind each scheduled
cell to its actual binary, and distinguish a capability exclusion from a
method limitation. Review any change to the signing matrix explicitly.
Developer ID, notarization and absent hosts remain retained applicability
limits, not reasons to mislabel the local binary.

### N7 — generated layout checks do not close message/right ownership

Fresh generation and `_Static_assert` validation succeeded. The SDK-generated
state demux checks simple message shape, bounded counts and exact variable
length. This supports those narrow wire checks. It does **not** provide the
method's exact-NDR check: the generated body only conditionally converts
`int_rep` and otherwise ignores the NDR record. `serve_n` passes the generated
union maximum to `mach_msg_server_once` with no reviewed wrapper for strict
routine/NDR/trailer rejection or complete receive/send disposition tracing.
Logging a reply inside a catch routine precedes actual IPC send and cannot
prove send completion. No per-path ownership table is supplied.
`thread_get_exception_ports` snapshots (`old_ports` and later `p2`) have no
explicit acquired-reference disposal path; the unknown-prior early return
also skips worker right/page cleanup. Task exception ports are never captured.

**Smallest remedy / closure.** Audit the selected public helper's exact
receive/reply/error ownership behavior against its supporting source and
record an acquisition/transfer/disposal table for normal, rejected, failed
send/receive, saved snapshot, restored snapshot and early-return paths. Add
the narrow strict pre-demux validation/trace wrapper where the helper does
not expose required checks, retaining the SDK-generated variable-layout
parser. Synthetic wire fixtures must cover routine, complex bits, NDR,
truncation/counts and every disposition; valid input must remain accepted.
Add task-port capture with owned-reference disposal and actual snapshot
records. This is an unresolved implementation proof obligation; no claim that
the public helper itself leaks or that the platform fails is made.

### Return disposition and scope

N1–N7 are open substantive implementation/validation blockers. The next action
is author remediation, with the same reviewer rechecking changed material
and affected controls. The accepted source/method hypothesis remains bounded
unchanged-state load/store retry on owned worker data pages with controlled
one-hop forwarding. No source-backed guarantee of arbitrary prior handlers,
server-death recovery, JIT permission preservation, platform-wide support or
performance follows from compilation or this review. An accepted native
pre-execution return still requires subsequent observations, independent
replication and findings review before Q19 can consume delivered-Mach evidence.

## Same-reviewer native return — six-child first tranche, 2026-09-05

Reviewer/context: `/root/mach_native_review`, unchanged from N1–N7 and
independent of return author `/root/publication_native_review`. Inspected the
rewritten native source, v3 checker, staged runner, coverage, signing/build
code, updated method/README and resource/right table. No load-bearing code
change or Mach experiment was authored or executed by this reviewer.

**Verdict: accept pre-execution collection for the exact six-child first
tranche only. Full M06-M1 native-method completion is not accepted.**
This is an explicitly reviewed staged amendment, not dropping the original
coverage. It tests whether the basic owned direct unchanged-state path can
produce the required trace before extending into the remaining mechanisms.
The six children are n=1, plain local ad-hoc, non-JIT, serial server, direct
path, load/store × seeds60601–60603; each retains10 warmup +100 checked
events with all warmup failures counting. They are schedule indices
0/1/2 and12/13/14. No negative, forwarded, JIT, multi-worker, held or
restoration-proof child is cleared by this verdict. The code retains all
other original cells as not-run obligations. No intermediate sample may be
promoted to the full baseline or consumed by Q19 before the required runtime
controls, broader coverage, independent replication and findings review.

### Substantive return assessment

- **N1, first-tranche lifecycle: closed.** Workers publish registration before
  admission and remain alive through their own selected-mask restoration.
  Main waits for completed/disarmed events and joins the front server before
  authorizing restoration. Server arguments now live through joins. The
  duplicate prior consumer and fabricated proof were removed; those broader
  paths have improved source but remain outside this tranche's acceptance.
- **N2, baseline attribution/state: closed.** Inspected the AOT assembly and
  generated disassembly: x9/x10 carry cookie/nonce, x19–x22 preserve independent
  sentinels and SP across exactly exported LDR/STR fault labels. All descriptor
  fields precede release arming. The server acquire-loads the outstanding
  event, validates exact PC through the SDK accessor, SP bounds, independent
  registers, protection category/address and exact count/flavor, and rejects
  a repeated receive. It copies all68 general-state words unchanged. The
  worker measures data, sentinels and SP equality, and waits for the server's
  actual successful IPC-send completion before commit/disarm and descriptor
  reuse. A worker may resume before the server logs send completion; the
  checker correctly permits that ordering while requiring send before commit.
- **N3, first-tranche checker: closed after one return.** The initial rewrite
  still accepted receive/reply before arm, disposal before restoration and a
  parked marker before event completion. Independent moved-record fixtures
  reproduced all three false passes. The author added arm-before-wire,
  final-disarm-before-park, restoration-before-owned-disposal-before-exit and
  snapshot-inspection-before-reference-disposal checks. Reinspection and
  independent rerun now reject the three witnesses as SERVER_ORDER,
  DISPOSAL_LIFETIME and PARKED_BEFORE_COMPLETION. The original six false-pass
  fixtures and other identity/wire/state mutations also reject. Full
  forwarded/multi-worker/held valid/adversarial fixtures are still owed.
- **N4: still open for the full method.** Real negative seams have replaced
  several placeholders and corrupted-result now alters owned data rather
  than arbitrary raw thread state. Their runtime trigger/outcome oracles and
  the unknown-prior decoy remain incomplete. No expected-negative success or
  safe server-death fallback is accepted, and this stage cannot discharge them.
- **N5: baseline containment closed; holds remain open.** The parent enforces
 5s no-output-progress and30s total child ceilings, kills/reaps its child
  process group, preserves partial output and stops admitting baseline jobs
  on failure. Six native ceilings sum180s; the stage loop has900s wall
  supervision, 64MiB kernel per-file stdout/stderr caps, 1GiB stage stop
  threshold and1GiB free-space reserve. Build/environment/signature commands
  are separately timeout-bounded;900s is not a claim about every preparation
  or final-inventory operation. No unimplemented hold is executed or counted.
- **N6, plain baseline signing: closed.** Build produces separate plain
  ad-hoc and hardened+allow-jit binaries and inspects actual signatures and
  entitlements. Baseline selection binds the plain binary checksum to its
  job. No JIT capability or hardened runtime observation is accepted here.
- **N7, admitted direct message/right path: closed.** The wrapper admits
  only the fixed336-byte routine2406 request, native NDR, two codes/68 words,
  a simple received header with a valid send-once reply right and the requested
  format0/null trailer. The generated MIG variable-layout parser remains in
  use. Full request bytes, decoded general state, prepared reply and actual
  send outcome are distinct. Successful MOVE_SEND_ONCE consumes the reply
  right; rejected/failed paths terminate the owned child without guessing
  consumption or reporting successful restoration. Initial/restored/task
  snapshot references and handler/control/receive/port-set resources have
  explicit successful disposal records after their users finish. No public
  guarantee for arbitrary foreign handlers or server failure follows.

### Independent validation and producing snapshot

Independently verified author build4's26 retained artifacts and inspected
its AOT wrapper/load/store and catch disassembly; the five current synthetic
unittests passed, including actual dummy-child no-progress kill and preserved
partial output. No dummy child executed Mach or generated instructions.
After the checker return, independently reran23 destructive checker fixtures
and one valid trace. Independently expanded the valid fixture into the actual
110-event,10-warmup/100-checked shape for each of the six access/seed
combinations; all six pass. This checks event cardinality/nonce/lifecycle
handling and does not pretend the synthetic values are native observations.

Fresh independent build-only snapshot:
`/private/tmp/natemu-m06-return-review-jxv9ho22/build`, build identity
`2e2f304a87c340d0b539901d512bdfae`,26 retained artifacts, SDK26.4,
Apple clang21.0.0, MIG bootstrap_cmds-138. MIG generation, layout assertions,
compile/link, separate signature inspection and disassembly succeeded. This
snapshot includes the final checker correction. Any actual evidence build
must retain those same reviewed source/control semantics; author build4
precedes the final checker edits and is not that final input snapshot.

The inherited `retry_path_pc_unchanged` build field is false because its
whitespace-sensitive source substring no longer matches. It is an obsolete
lexical heuristic, not evidence that PC was changed. This review relies on
the actual inspected copy, generated code and retained full-state equality,
and grants that metadata flag no scientific authority. Removing the unused
heuristic later is a nonblocking cleanup.

Execution stops on the first invalid/unresolved/resource outcome without
replacement. Successful collection remains a bounded preliminary packet;
full N4/N5 and remaining N1/N3/N6/N7 applicability/control work still return
to this same review thread before their stages execute.


## Same-reviewer N4/N5 return — 2026-09-05

Reviewer/context: `/root/mach_native_review`, independent of instrument author
`/root/publication_native_review`; unchanged reviewer through the earlier
returns and six-child findings review. Inspected the complete current native
source, controls/holds consumers, full normal checker, runner, coverage,
fixtures/tests, README and proposed stage amendment. No native execution or
production/control-code authorship by this reviewer.

**Verdict: return for corrections. Neither the proposed 33-child controls
stage nor the 96-child holds stage is cleared for execution.** These are
instrument/control defects, not evidence against Mach delivery. Accepted
CLM-01-06-24/25 and their retained six-child baseline/replica are unaffected:
those actual packets were checked separately, including direct raw decoding;
the new defects concern unexecuted controls and forwarded/held obligations.

### What was verified

The retained build `/private/tmp/natemu-m06-controls-holds-build3`, identity
`7edb367232e94a26ab4d189ba04405c6`, has all 31 artifact hashes intact. Its
retained C/Python inputs match the inspected workspace return. The retained
compile, generated wire-layout, signing and disassembly preparation is
recoverable; no resulting executable was launched. Independently ran all
12 synthetic unittest methods successfully, including the 23 inherited
negative fixtures and dummy-process runner tests. Additional independent
mutations below nevertheless pass incorrectly.

The complete schedule remains 384 positive + 33 auxiliary + 11,520 held
children, 11,937 total. The proposed hold selector yields exactly 96:
21 direct and 27 forwarded children per topology, n=2/plain/non-JIT/load,
held worker 0, three unchanged seeds. The six-child baseline selector is
unchanged. The 1,800/3,600-second stage-loop budgets, 30-second child and
5-second no-output-progress watchdogs, per-file 64 MiB bounds and operational
1 GiB stage/free-reserve checks remain explicit. Preparation commands have separate timeouts; these stage-loop timers do not
cover preparation or final inventory.

The revised controller does release the trace lock before waiting and uses
release/acquire handshakes. Event holds admit the selected worker first; main
consumes the teardown request while the event is outstanding; per-worker
service requires a peer disarm before release. Serial service is allowed to
queue an attempted peer. These are attempted/admitted operations, not proof
of simultaneous physical execution. Before-repair is correctly on the prior
for forwarding; front-before-reply and prior-before-reply are distinct.
Workers stay alive through restoration and retained-resource disposal.
Teardown holds observe main's join attempt before release. These substantive
improvements do not resolve the defects below.

### R1 — idle receiver can terminate the stall control before its watchdog

**Evidence/consequence.** `run.native_args` selects n=2/perworker for
`server-stall`. Main starts both receiver threads, each using a 5,000 ms
`MACH_RCV_TIMEOUT`, before admitting only worker 0. Worker 0 reaches the stall
trigger and pauses; main remains in its completion wait, so worker 1 is never
admitted. Worker 1's idle receive can expire and call
`fail("receive failure", mr)` / `_exit(70)` before the parent's five seconds
of no output measured from the later trigger (and polled every 50 ms). The
consumer requires SIGKILL/no-progress, so this ordinary execution can produce
an unrelated fatal result and stop the stage. This follows directly from
the two timeout epochs; no Mach experiment is needed to demonstrate it.

**Smallest remedy/closure.** Make internal receive waits unable to preempt
the parent's reviewed total/no-progress containment, or start/tie each wait
to actual admission with an explicit valid idle policy. Retain cause-specific
receive failures. Add a small scaled dummy-process scheduling test with an
idle second receiver and delayed stall trigger that proves the parent
watchdog, rather than the idle receiver, owns the expected stall termination.
Do not loosen the consumer to call unrelated exit 70 a successful stall.

### R2 — fatal consumers admit unrelated termination and output exhaustion

**Evidence/consequence.** Starting from either `control_fixture('terminal')`
or `control_fixture('receive-right-destruction')`, append an explicit
`terminal` row with reason `unrelated failed setup` and change the parent
return code to 70: `check_control` returns `[]`. Both also return `[]` for
`-signal.SIGXFSZ`. Thus the actual kernel per-file resource limit can be
classified as the desired control when `stop_reason` is absent, and an
explicit unrelated instrument failure is accepted despite the cause-specific
consumer claim. Recording a downstream status is legitimate; counting these
as checked controls is not.

Receive-right destruction also destroys the right before logging its action.
An already-blocked receiver can wake, log `receive failure` and terminate the
whole process before the worker records the successful destruction. The
current consumer then either loses its required action or, if it is present,
accepts any fatal reason. This is an instrument race, not a guarantee of a
particular downstream signal.

**Smallest remedy/closure.** Distinguish and preserve the intended downstream
category, cause-attributed receive-port-death, unrelated instrument failure,
resource termination and unresolved partial output. Reject resource kills and
unrelated explicit terminal reasons. Coordinate/log the destructive action
and receiver's observed result so the child's own fatal path cannot erase
the required cause evidence. Keep the parent's categorical downstream
signal/exit/timeout record without predicting a particular signal or claiming
successful fatal cleanup. The two mutations above must reject, and valid
expected-category fixtures must remain accepted.

### R3 — forwarded/restoration checker does not enforce the full lifetime

**Evidence/consequence.** Three independent moved-record witnesses on
`valid_trace(path='forward')` still return `[]` from `two_pass_check`:

- Move `restored-initial` before the proof event's arm. The selected prior
  route can be recorded as removed before the event meant to prove it.
- Move `restoration_proof` immediately after the proof arm, before any
  delivery/reply/resume/commit/disarm. The proof marker need not follow proof.
- Move `forward_call` before the first front wire record. A successful prior
  round trip is not ordered within the actual front/prior exchange.

The native source currently orders these operations correctly, but the new
controls/held consumers rely on this checker to qualify retained forwarded
traces and disposal. This was outside the accepted direct baseline.

**Smallest remedy/closure.** Require front attribution before prior delivery,
prior reply before successful forwarding return, forwarding return before
front reply; allow actual send completion to be logged after a resumed
recipient as the existing method correctly permits. Require completed proof
commit/disarm before its proof marker, then initial-route restoration and
retained saved-prior/resource disposal after that marker. Add these exact
moved-record regressions and valid interleavings. Do not impose false
send-log-before-recipient-resume ordering.

### R4 — after-receive hold misses the declared before-validation boundary

**Evidence/consequence.** The method names “server after receive before
validation.” In `serve`, the hold is reached only after strict message
envelope, NDR/count and trailer validation and the `wire` record reporting
`ndr_exact:true`. It precedes the MIG callback/identity checks, but not the
full declared validation boundary. `holds.py` only checks
wire < hold < decoded receive, so its fixtures cannot expose this difference.

**Smallest remedy/closure.** Place the hold after a successful bounded receive
and the minimum safe owned-port attribution needed to select the actor, but
before strict envelope/NDR/count/trailer validation. Retain distinct receive
and validation evidence so the consumer verifies the actual boundary. A
synthetic moved-validation fixture must reject validation-before-release.
Do not silently narrow the named point to after envelope validation.

### R5 — raw delivered PC can contradict the asserted control identity

**Evidence/consequence.** For skipped-repair, wrong-descriptor and server-stall
fixtures, change the actual raw ARM64 PC word pair to `0xdeadbeec`, update
received/prepared state words consistently, but leave the human-readable
identity PC unchanged. `check_control` returns `[]` for all three. The normal
checker also does not bind decoded PC/SP to the raw PC/SP words. The synthetic
base fixture itself leaves raw PC/SP zero while asserting nonzero decoded
values. The accepted baseline was independently decoded directly from its
actual raw bytes, so this does not contradict those observations. For these
new control packets, however, the claimed exact wrong-descriptor/valid-state
oracle does not yet independently reject contradictory raw state.

**Smallest remedy/closure.** For this explicitly non-opaque arm64 producing
build, bind the retained decoded PC/SP to their actual raw layout, or retain
and validate an equally direct build-specific decoding result. Correct
synthetic valid fixtures accordingly. Keep authenticated/opaque pointer-state
configurations deferred rather than inventing a pointer mask. The raw-PC
contradictions must reject while valid actual-layout fixtures pass.

### Reusable synthetic reproduction

Run from the repository root; this reads/imports Python fixtures only and
executes no Mach/native experiment:

```sh
python3 -B - <<'PY'
import signal, struct, sys
sys.path.insert(0, 'experiments/INV-01-06/m1')
from check import by_kind, _rows_to_text, two_pass_check
from controls import check_control
from test_controls import control_fixture
from trace_fixtures import valid_trace

for name in ('terminal', 'receive-right-destruction'):
    rows, status = control_fixture(name)
    rows.append(dict(kind='terminal', reason='unrelated failed setup', code=5))
    print(name, 'unrelated fatal', check_control(
        _rows_to_text(rows), name, dict(status, returncode=70)))
    rows, status = control_fixture(name)
    print(name, 'resource signal', check_control(
        _rows_to_text(rows), name, dict(status, returncode=-signal.SIGXFSZ)))

for mutation in ('initial-before-proof', 'premature-proof', 'early-forward'):
    rows = valid_trace(path='forward')
    if mutation == 'initial-before-proof':
        moved = next(r for r in by_kind(rows, 'teardown_restore')
                     if r['label'] == 'restored-initial')
        target = by_kind(rows, 'arm')[-1]; offset = 0
    elif mutation == 'premature-proof':
        moved = by_kind(rows, 'restoration_proof')[0]
        target = by_kind(rows, 'arm')[-1]; offset = 1
    else:
        moved = by_kind(rows, 'forward_call')[0]
        target = by_kind(rows, 'wire')[0]; offset = 0
    rows.remove(moved); rows.insert(rows.index(target) + offset, moved)
    print(mutation, two_pass_check(_rows_to_text(rows)))

for name in ('skipped-repair-nominal-success', 'wrong-event-descriptor',
             'server-stall'):
    rows, status = control_fixture(name)
    for wire in by_kind(rows, 'wire'):
        raw = bytearray.fromhex(wire['raw'])
        struct.pack_into('<Q', raw, 64 + 32 * 8, 0xdeadbeec)
        wire['raw'] = raw.hex()
    for row in by_kind(rows, 'receive') + by_kind(rows, 'reply'):
        row['state'][64:66] = [0xdeadbeec, 0]
    print(name, 'raw PC contradiction',
          check_control(_rows_to_text(rows), name, status))
PY
```

Every printed failure list was empty on the reviewed build3 source. Closure
requires the listed contradictory fixtures to fail, actual valid interleavings
to pass, and same-reviewer reinspection of the native timeout/receive/hold
changes before either proposed stage. Future successful observations still
need independent replication and findings review; full signing/JIT/topology
coverage, wire-spoofing preflight and Q19 composition remain separate work.


## R1–R5 correction return — same reviewer, 2026-09-05

Reviewer `/root/mach_native_review` remains independent. Root authored these
corrections after the original author context could not be resumed because of
the collaboration thread-limit failure. The reviewer authored no load-bearing
M06 change and performed no native experiment. Reinspected every changed
source/consumer/fixture and affected native ordering/ownership assumption.

**Verdict: accept pre-execution preparation for the exact proposed 33-child
controls stage and 96-child first holds stage. R1–R5 are closed for these
bounded stages.** Run controls first; any unexpected classification or resource
stop ends admission without replacement or automatic continuation. The holds
stage is the fixed next tranche after successful controls, not an alternate
way to bypass a failed control. This is no full-M06, Q19, signing/JIT, arbitrary
prior-handler or findings acceptance. The accepted six-child observations
remain unchanged, and the rest of the original full matrix remains owed.

### Closure evidence

- **R1 closed.** Internal receive timeout is 60 seconds, beyond the parent's
  30-second total and five-second no-output-progress limits. The idle peer
  receiver can no longer supply the prior five-second timeout that preempted
  the intended stall watchdog. The scaled delayed-trigger/idle-receiver dummy
  test independently passes with parent no-progress SIGKILL and preserved
  trigger output. No new native run duration is admitted by the longer
  internal wait; the existing parent ceiling still owns containment.
- **R2 closed after one small consumer return.** On the selected destruction
  seam, the receiver waits on an acquire load of `receive_destroyed`; the
  worker release-publishes it only after recording the actual successful
  receive-right decrement. The receiver can then record only the specific
  owned-port-death reason for MACH_RCV_PORT_DIED or MACH_RCV_INVALID_NAME.
  Verified these constants directly against SDK26.4 `mach/message.h`. An
  explicit unrelated fatal record, SIGXFSZ, SIGXCPU, unrequested SIGKILL or
  SIGTERM no longer qualifies. Memory-fault signal, declared watchdog
  containment and exact receive-port-death remain distinct observed categories,
  with other downstream outcomes unresolved; none promises a fallback,
  restoration or particular kernel signal. Initial reinspection found that
  destruction's trigger could still appear after terminal, and the unhandled
  trigger before its delivered identity. The final return now requires
  arm < trigger < destructive action < owned-port-death terminal, and
  identity < unhandled trigger < any send. Both exact moved-trigger witnesses
  independently reject as CONTROL_ORDER, while valid categories pass.
- **R3 closed.** The consumer requires front attribution < prior exchange/
  reply < successful forwarding return < front reply. Completed proof disarm
  precedes its proof marker and initial restoration; the existing resource
  rules retain saved-prior references through that restoration. All three
  original moved-record witnesses now reject. A valid prior send-completion
  log after recipient resumption but before commit remains accepted, preserving
  the actual IPC scheduling freedom rather than inventing a global log order.
- **R4 closed.** A selected after-receive hold now records bounded
  `received_message` bytes after successful receive, minimum owned-port lookup
  and buffer bounds, then waits before strict envelope/NDR/count/trailer
  validation. Mutable event access follows the outstanding flag's acquire
  load, preserving descriptor publication. The consumer requires matching raw
  bytes and the validated wire record after hold exit. Moving validation
  before release rejects. This fixes the actual native boundary; it is not
  merely a renamed after-validation marker.
- **R5 closed.** Native compilation explicitly asserts non-opaque arm64
  thread state. Both consumers now bind decoded PC/SP to raw word pairs
  32/31 in that declared layout. Valid fixtures populate those fields, and
  each original raw-PC contradiction rejects as CONTROL_RAW_PC_SP. The
  expanded fixtures also challenge raw SP. Opaque/authenticated state remains
  outside this method, with no guessed pointer masks.

Independently ran the complete 15-test suite successfully after the native
R1–R5 changes, then the four focused control tests after the final two ordering
predicates. Reran the original review's self-contained reproduction: every
formerly empty contradictory result now contains its expected failure. All
12 immutable author/replica baseline traces also pass this stronger checker;
there is no contradiction to the directly decoded accepted baseline findings.

Final inspected retained compile/signature/layout/disassembly packet:
`/private/tmp/natemu-m06-r1-r5-return-20260905-c`, build identity
`2bafd0f9765d4451855dc98ac4febf87`, SDK26.4/Apple clang21. All 31 artifact
hashes and retained C/Python source bytes match the final workspace return.
The preceding final-b packet was independently verified too; final-c differs
only in the last consumer predicates and their regressions. Earlier no-suffix
and build3 packets predate parts of the fixes and are not the final producing
inputs for these stages. A fresh evidence build must retain the final reviewed
source/control semantics and its own actual signatures and run association.

### Exact accepted bounds and remaining scope

Controls: all 33 original auxiliary children, unchanged eight negatives plus
terminal/restoration/prior-refusal, each with seeds60601–60603; 990 seconds
summed native ceilings and 1,800 seconds for the stage loop. Holds: exactly
96 children, n=2/plain local ad-hoc/non-JIT/load, both paths and topologies,
selected worker0, all applicable named points, three unchanged seeds and
10 warmup +100 checked events per worker; 2,880 seconds summed native
ceilings and 3,600 seconds for the stage loop. Both keep 30-second child,
five-second no-output-progress, kernel 64 MiB per stdout/stderr file, 1 GiB
aggregate operational output threshold and 1 GiB free reserve. Preparation
commands have separate timeouts; final inventory is operational overhead,
not a realtime or filesystem-quota claim.

No native stage was run during this review. Root coordinates exclusive native
execution separately. Stop at the first invalid/unresolved/resource outcome,
retain partial checksums and all remaining not-run rows, and use no outcome-
driven replacements. A held timeout is invalid, never a hold pass. Subsequent
observations require independently checked replication preparation and
findings review before any new claim is accepted. Full 384-positive/
33-auxiliary/11,520-held coverage, remaining signing/JIT/worker identities,
transport-spoofing preflight fixtures, additional hosts, state completeness,
Q19 composition and all quantitative timing conclusions remain open.

## Receive-error attribution return — same reviewer, 2026-09-05

Reviewer/context: `/root/mach_native_review`, independent of correction author
`/root`; this reviewer also examined the actual MAN-01-06-05 failed packet.
No Mach receive, destruction, fault experiment or timing collection executed
in this review. The compiled reporter fixture invokes only the production
error-reporting routine with fabricated arguments.

**Verdict: accept this correction for a fresh full33-control campaign, with
exact96holds conditional on all33 controls succeeding.** The preceding bounds,
fixed seeds/counts, first-failure stop and full-method limitations still apply.
MAN05 remains immutable and INVALID; this author return does not reinterpret
its unidentified receiver or accept its generic terminal/SIGBUS combination.

Inspected `receive_failure`, its sole actual receive-error call site,
registration lookup/publication and the worker's destructive API/action/release
publication; compared the complete native diff against final R1–R5 candidate-c.
The helper identifies the attempted receive port through the registered owned
port table. Only the declared worker0/front destruction seam and exact
INVALID_NAME/PORT_CHANGED/PORT_DIED results wait for acquire-observed successful
destruction publication. It emits the explicitly versioned witness before its
specific fatal record. Unknown ownership/code remains a generic failure with
attribution, never an eligible owned-error category. Registration acquires
immutable ownership fields; the new path does not read mutable event state
before the outstanding flag. The earlier strict wire-before-validation hold,
restoration, prior-proof, reference-disposal and timeout fixes are unchanged.

The consumer binds the witness to the registered scheduled worker/front port,
exact schema/code and true destruction publication; it enforces
arm<trigger<successful action<witness and witness<matching terminal if present.
It requires no delivery/repair/reply/resume/commit. Exit70 needs the matching
terminal; SIGBUS/SIGSEGV can follow a precise witness with no terminal or the
matching terminal. This establishes only bounded termination after an
attributable receive error, not signal causation or completed explicit exit.
An additional foreign receive error, generic/mismatched terminal, duplicate
witness, resource stop, unknown code or unrequested termination rejects.
The older no-terminal downstream containment forms retain their narrower
interpretation; they establish no received Mach error. The obsolete owned-death
record remains readable for old packets but is no longer produced by this helper.

Independently verified candidate
`/private/tmp/natemu-m06-receive-return-20260905-b`, build
`ac2267c87f224405921650f8658d7be0`: all32 artifact hashes and every retained
C/Python source byte match the submitted workspace code. All16 retained
synthetic/dummy tests pass; the actual compiled reporter's five exact cases
and unpublished-state kill/reap pass. Independently moved witness/terminal
records across prior stages and injected duplicate/foreign-error/resource
records: all19 additional contradictions reject. Rechecking immutable
MAN05 child396 with this candidate still returns
`UNHANDLED_UNRESOLVED_OR_UNRELATED`. No unresolved substantive defect was found
in this bounded return.

A fresh producing snapshot/build and distinct full33-control denominator are
required; this is no replacement of the failed campaign. Controls retain
1,800 seconds stage-loop/990 seconds summed child ceilings; the conditional
96holds retain3,600/2,880 seconds. Both retain30-second total/five-second
no-output child containment,64MiB per stdout/stderr,1GiB operational aggregate
output threshold and1GiB free reserve, with the preceding preparation/final
inventory qualifications. Root must coordinate native exclusivity. Any invalid,
unresolved or resource outcome stops the stage and prevents holds; all partial
and not-run entries stay retained. New findings still require independent
replication and review; the accepted exact-six baseline and full11,937-job,
JIT/signing, other-host, Q19 and timing limits remain unchanged.

## Reply-right-loss consumer return — same reviewer, 2026-09-05

Reviewer `/root/mach_native_review`, independent of author `/root`.
Inspected MAN-01-06-06, the proposed scoped interpretation and candidate
`/private/tmp/natemu-m06-send-return-20260905-a`, build
`07cab65c409343f58ab7648aa290ce66`. All32 hashes and current source bytes
verify; native C equals the second campaign's producing C. All six focused
control tests pass. No M06 native experiment ran; T2 remains exclusive.

**Initial verdict: return one concrete ordering correction.** Allowing only
exit70/SIGBUS/SIGSEGV after the fully attributable dropped reply right, exact
MACH_SEND_INVALID_DEST and matching terminal can support the stated bounded
contained-termination category. The proposed consumer does not yet enforce
its complete declared causal order. Each separate mutation of retained
`test_controls.control_fixture('reply-send-failure')` still returns no failures:
move `negative_trigger` immediately after `negative_action`; move that action
immediately after `arm`, before actual receipt; or move `repair` immediately
after `arm`, before delivered attribution. Thus an impossible acquisition/drop
or attribution/repair ordering is accepted despite the stronger terminal code.

Smallest remedy: for this arm require identity<repair<reply<trigger<successful
drop<failed send<matching terminal, retaining the existing raw wire→receive→
identity checks. Add those three isolated moved-record regressions and return
the consumer-only correction. The actual trace and fresh complete33-control/
conditional96-hold proposal stay separately retained; MAN05/06 keep their
producing INVALID outcomes and no accepted findings are inferred here.

### Final reply-right-loss ordering return — accepted, 2026-09-05

Same independent reviewer `/root/mach_native_review`; author `/root`.
**Verdict: ACCEPT the final consumer correction for a fresh full 33-control
campaign, with the original 96 holds conditional on all 33 controls succeeding.**
The three outstanding ordering reproductions are closed. No M06 native
experiment was launched by this review; T2 replica handle3368 is terminal0.
Root must confirm native exclusivity when launching the new M06 stage.

Final inspected candidate:
`/private/tmp/natemu-m06-send-return-20260905-b`, build
`5b6b007bdb754a72af9d0a66b994b4e5`. Independently verified all 32 artifact
hashes and every retained C/Python source byte against the submitted return.
The native producer remains byte-identical to MAN-01-06-06's producing source.
The sole changed consumer line adds identity<repair, reply<trigger and
trigger<drop; existing predicates complete
identity<repair<reply<trigger<successful drop<failed send<matching terminal.
The source's actual receive/attribution and reply-right ownership checks remain.

Independently reran each original moved-trigger/action/repair reproduction:
all three now reject with CONTROL_ORDER. All six focused control test groups
pass, including exact error/right/role binding, absent or mismatched terminal,
unrelated fatal/signal/resource rejection and the narrowed permitted outcomes.
This change does not relax other controls' explicit-exit requirements.

Replaying MAN06 child399 with this candidate satisfies the new scoped oracle;
its immutable producing INVALID result remains unchanged. MAN05's generic
unattributed child396 still rejects. This is pre-execution acceptance of a
future consumer interpretation, not findings acceptance of either partial
campaign. The eligible category remains contained termination after witnessed
reply-right loss and exact failed send, with no claim of signal causation,
completed explicit exit, successful restoration or safe fallback.

Keep the unchanged full denominators and seeds/counts: 33 controls under
1800-second stage/990-second summed child ceilings, then only after complete
success 96 holds under3600/2880 seconds. Both retain30-second total and
five-second no-output child watchdogs,64MiB per stdout/stderr file,1GiB
operational aggregate output threshold and1GiB free reserve, with the earlier
preparation/final-inventory qualifications. Stop at the first invalid,
unresolved or resource outcome and retain all partial/not-run records without
replacement. Independent replication and findings review remain required;
the accepted exact-six baseline, full11,937-job obligation, remaining
JIT/signing/host scope, Q19 and timing limitations are unchanged.
