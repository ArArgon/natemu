# INV-01-23 — pre-execution review

## M23-L1 initial independent method return — 2026-09-05

Reviewer: Codex `/root/ordering_native_review`, independently reviewing root's
method. The reviewer authored no Q23 method, protocol or code. Inspected the
research workflow, complete current INV-01-23 with proposed M23-L1, accepted
source review REV-158, local SDK26.4 `pthread.h` CPU-number contract,
`pthread/qos.h` requested-QoS contract and `pthread_cond_wait.3`. Also checked the
[Open Group condition-wait specification](https://pubs.opengroup.org/onlinepubs/9799919799/functions/pthread_cond_clockwait.html)
for atomic mutex release/wait and permitted spurious returns. No code, native
experiment, signal/timer execution or load test exists in this review.

**Verdict: revise method preparation at the three concrete gaps below.**
The bounded host-dependency question and much of the method are sound. Missing
implementation itself is not a defect; acceptance of a corrected method would
permit matching code/synthetic preparation, not execution or guest-liveness
conclusions. The reviewer has not authored substantive method changes.

### L1-R1 — actual wait entry has no specified independent witness

**Consequence.** The lost-wakeup control could still be accepted on a trace
written before the call, while the owner was descheduled before entering the
API. Conversely, a valid spurious return could erase a timeout-only witness.
The method explicitly forbids both interpretations, but does not yet specify
what observation distinguishes those cases.

**Evidence and reusable trace challenge.** Both of these executions can share
`release-predicate-published → notification → owner-precall-record → timeout`:
(A) owner enters `pthread_cond_wait` after the notification; (B) owner is
stopped immediately after the record and never calls the API. That prefix
cannot establish actual wait entry. Merely requiring the implementation to
retain an actual call attempt leaves the observation protocol unresolved.
The SDK manual identifies atomic release of the associated mutex by the wait;
the Open Group contract also permits spurious return. Those are useful public
observables, unlike an inferred scheduler state or a log label alone.

**Smallest remedy.** Specify a synchronization/return witness tied to the real
wait operation, the actor observing it, and the no-alternate-path condition
needed to distinguish A from B. Preserve the true-predicate-before-call defect
and spurious-return handling; do not weaken the method into timeout detection
or reject the control before reaching the actual branch. Implementation choice
belongs to the author.

**Closure.** An explicit transition/observation rule distinguishes the two
prefixes and covers both a waiting owner and a spurious return. Later code and
synthetic validation must exercise that rule and retain its terminal evidence;
no native experiment is required to close method preparation.

### L1-R2 — watchdog progress and partial-trace publication are ambiguous

**Consequence.** The five-second no-progress limit can be reset indefinitely by
helper arithmetic, EBUSY/yield counters or repeated observations of the same
state while the owner/peer dependency is stuck. The independent 30-second cap
still bounds the child, but the declared no-progress classification is then
undefined. A trace available only after successful joins also cannot support
the required held-owner/frozen-peer diagnosis after the child is killed.

**Evidence and reusable trace challenge.** Compare (A) a held owner plus helpers
that continually increment work counts and (B) a released/completed owner with
one frozen peer plus other active actors. Under a generic "durable progress"
rule both may appear active until the whole-child ceiling, even though the
method requires distinct bounded incomplete-dependency outcomes. One-writer
logs avoid a writer race but do not specify how their committed prefix becomes
available to the watchdog or survives timeout before joins.

**Smallest remedy.** Define the finite actor/epoch state advances eligible to
reset the watchdog, its initial/reset point, and whether it tracks the current
required dependency or a cell-level completion milestone. Explicitly exclude
activity that does not advance that obligation. Specify a synchronized committed
prefix available after forced termination, and classify missing or inconsistent
witnesses as unresolved rather than an expected cause-specific timeout.

**Closure.** Hand-worked or synthetic traces show the declared five-second
classification despite unrelated helper/spin activity, distinguish intentional
held-owner from owner-completed/missing-peer cases, and preserve the relevant
prefix under a kill before joins. The exact storage representation can remain
an implementation choice for subsequent code review.

### L1-R3 — the 63 negative cells do not select their peer strategies

**Consequence.** The control matrix is not yet a reproducible input list. It
specifies seven controls × three N values × three seeds, background and H=C,
but the three-way strategy dimension is absent. The missing-yield control in
particular cannot test its stated call omission on blocking or pure-trylock
strategy. Expanding all controls over strategies would change the declared
sample count and budgets.

**Evidence.** The 216-positive formula includes strategy explicitly, whereas
neither the 63-control formula nor the seven-row control table assigns a
strategy to each control. The fixed schedule seed also does not say whether
controls are appended or included in the shuffled list, affecting reproducibility
and which controls remain not-run after a failure.

**Smallest remedy.** Declare one fixed strategy per negative-control row,
including yield for missing-yield, plus its epoch/victim selection and placement
of the negative list in the fixed campaign order. Preserve 63 controls unless
the author explicitly revises the matrix and aggregate budget. State whether
23001–23003 are distinct deterministic stimulus seeds or repetition identities;
no randomization is required merely to justify three repetitions.

**Closure.** The method expands unambiguously into exactly 216 positive and
63 negative inputs with no strategy/epoch/victim selected after observing a
result. Subsequent code review can compare that exact schedule with the runner.

### Clear method elements and retained limits

The arithmetic is correct: 4 worker counts × 3 strategies × 2 QoS × 3 helper
counts × 3 repetitions = 216 positives; 7 × 3 × 3 = 63 controls; 279 × 30 s =
8370 s per campaign. The independent-replication allocation is 558 children,
16,740 summed child seconds and 21,600 total stage seconds. The C≤64 declaration
bounds helpers at 128 and worker/controller/helper threads at 137, without
calling their mere existence proven continuous oversubscription. The 10,800 s
stage wall stop, 1 GiB aggregate output stop, 2 GiB free-space reserve, per-stream
32 MiB caps and per-child 256 MiB application-buffer ceiling are concrete.
The latter is not a total process RSS ceiling: thread stacks, libraries and
runtime memory remain additional. Code review must inspect actual buffer
arithmetic, cap enforcement, watchdog/supervisor independence, group cleanup
and partial-output retention. The method does not promise all cells finish
before those caps and permits no rescue replacements.

The ordinary target mutex remains owned while the owner waits on a distinct
controller predicate. Peer readiness must precede target-lock acquisition;
per-peer exact protected updates and acknowledgements gate the next epoch.
N=1 appropriately removes peer contention rather than inventing peer events.
All 20 epochs remain correctness observations; the first two are excluded only
from a future qualified interval summary. An injected 100 ms hold is an
intentional availability constraint, not measured scheduler preemption. Release
to owner receipt and receipt to unlock stay separate unqualified intervals;
no observed tail becomes a guaranteed bound.

The method correctly treats stale acknowledgement and deliberately stale
protected data as deterministic protocol/accounting controls, not proof of
weak-memory behavior. It distinguishes a frozen peer from an unreleased owner,
subject to R2's observer and prefix rule. It also correctly refuses to interpret
a malformed CPU record control as validation of all plausible in-range samples
or as P/E identification. For later implementation, the inspected SDK defines
`pthread_cpu_number_np` range using `sysctlbyname("hw.ncpu")`; record that bound
explicitly rather than assuming an unspecified "online" count is interchangeable.
Requested QoS and setter outcomes do not reveal active effective overrides,
actual placement, fairness or dispatch latency. No new platform guarantee is
inferred beyond REV-158.

Per-actor writer ownership, synchronization-edge reconstruction and exact
actor/epoch accounting are appropriate. Cross-thread timestamp sorting cannot
supply happens-before. Separate raw interval validation and applicable C1/local
floor assessment remain necessary before timing interpretations; correctness
can be independently considered without inventing calibrated timing. The F08
mutex fallback result does not certify this new harness or a guest memory model.
Q18 quiescence, Q19 handler composition, Q21 lifecycle, Q05-06 placement, Q22
guest time policy, real guest warning stimuli and P1 remain explicit obligations.
A same-host repetition cannot discharge unavailable-host applicability.

### Return conditions and review limits

Resolve L1-R1 through L1-R3 in the owning method and return to this same reviewer.
No experiment code or native result is needed for this method return. If the
method then has no substantive blockers, acceptance will cover preparation of
the exact instrument and its synthetic controls only. A later full independent
code/pre-execution return is still required before any native child. No guest
liveness, source guarantee, architecture/family disposition or Track 01 closure
is accepted here. No investigation or substantive source text was edited by
this reviewer; only this review thread was created.

## Same-reviewer method return — 2026-09-05

Reviewer: `/root/ordering_native_review`, unchanged independent context.
Reinspected the amended M23-L1 protocol, control schedule, wait-return oracle,
watchdog transition list, external record publication and original scope/budgets.
Also checked local SDK26.4 `pthread_cond_timedwait.3`: its absolute system-time
argument, mutex release/reacquisition and 0/ETIMEDOUT/invalid-argument results.
No experiment code, native execution or substantive author edit by this reviewer.

**Verdict: ACCEPT the bounded M23-L1 method for instrument and synthetic-control
preparation. L1-R1, L1-R2 and L1-R3 are closed at this method return.** This is
not execution clearance. The future implementation and all actual-branch
controls still require full independent code/pre-execution review.

**L1-R1 closed.** The control now pairs the true release predicate, observed
under the controller mutex, with an actual subsequent `pthread_cond_timedwait`
return of 0 or ETIMEDOUT. It declares checked CLOCK_REALTIME + 250 ms deadline
construction, normalized nanoseconds, time_t overflow handling and retained
original read/deadline/return. A missing return or other error stays unresolved.
This distinguishes the original false witness: an owner stopped between the
precall record and API invocation cannot produce the required real return.
If descheduling instead lets the deadline expire before invocation, ETIMEDOUT
still witnesses the unconditional call, without claiming it blocked. A spurious
successful return likewise does not remove the already-witnessed predicate-loop
violation. The specified interpretation is a protocol-control observation,
not evidence that an OS notification was lost or a timing/scheduler bound.
The independent parent limit remains applicable when real-time adjustment
changes the duration of the operational wait.

**L1-R2 closed.** Only new expected worker/controller actor/epoch/phase
transitions can reset the five-second no-progress timer. The finite list
excludes helpers, EBUSY retries, repeated first-EBUSY reports, duplicate
transitions, unrelated output and incomplete records. The fixed 30-second
whole-child bound never resets. The author now requires checked writes to
per-actor externally opened append files, increasing sequence identities,
complete-line prefix observation by the parent and retained partial suffixes
after kill/reap. Records precede their associated publication actions; later
reader/acknowledgement observations must establish completed synchronization
edges, rather than treating a prepublication record alone as proof that a store
executed. This distinction follows the method's explicit actual-state and
synchronization-edge requirements and belongs in the matching code checker.

The original trace challenges now have defined outcomes: once the held-owner
or frozen-peer case exhausts its finite eligible phase advances, unrelated
helper/spin activity cannot keep its no-progress clock alive. Expected
withheld-owner classification still requires the held/withheld witnesses;
expected missing-peer classification additionally requires observed owner
release, receipt and unlock. Missing required cause records remain unresolved.
A child killed before joins leaves checked external complete prefixes instead
of requiring access to lost local buffers; an incomplete final record is
preserved verbatim and supplies no progress or inferred event. This promises
process-kill retention, explicitly not host-power-loss persistence. Actual
write/overflow/error handling and parent tailing will be validated in code.

**L1-R3 closed.** The negative controls now use park except missing-yield,
which uses yield. Their order is the existing seven-row table, then ascending
N=2/4/8, then repetition identities 23001–23003, appended after the 216
positives shuffled by schedule seed 23000. The repetition identities introduce
no unclaimed randomized native stimuli. Each control injects once at epoch 2,
the first checked epoch after two warmups. Owner0 is assigned to withheld
release, protected-data corruption and the erroneous release wait; peer1 to
yield omission, stale acknowledgement, CPU record corruption and frozen worker.
Missing-yield requires peer1's actual first EBUSY before controller release;
a missing exposure is unresolved and stops admission. This expands to exactly
63 fixed controls, with no post-result strategy or victim selection.

The 216 positives plus 63 controls, 20 epochs, 30-second child ceiling,
10,800-second stage ceiling, 1-GiB output threshold and replication allocation
remain unchanged. Extending the 32-MiB per-file cap to actor traces and retaining
retry/yield aggregates at phase boundaries makes the proposed logging scale
explicit; exact buffer/record arithmetic remains a code-review obligation.
Timing fields remain unqualified absent interval-specific evidence. No outcome
supplies observed preemption, continuous helper occupancy, P/E classification,
free-running timestamp comparability, actual guest warnings or closure of the
Q18/Q19/Q21/Q22/Q05-06 and P1 dependencies.

The next bounded action is authoring the smallest matching instrument and
adversarial traces, followed by independent review of the actual producer,
checker, runner, machine code, artifact boundaries and controls before any
native child. This method acceptance creates no runtime findings or
architecture/guest-liveness disposition.

## Independent full code review — 2026-09-05

Reviewer/context: `/root/mach_native_review`, separate from method author
`/root` and code author `/root/ordering_native_review`. The latter was the
method reviewer and changed roles before implementing this packet, so this
is the required new independent full code review. Read the accepted method,
prior L1-R1–R3 returns, producer, raw checker, runner and fixtures. No M23 native
liveness child was executed; permitted source/synthetic review ran while the
separately reviewed T2 replica occupied the exclusive native slot.

**Verdict: RETURN for four concrete checker/watchdog corrections below.**
The method's bounded aim and279-child schedule remain appropriate, but this
code packet does not yet establish its declared evidence oracles. No M23
native execution is cleared. These are reproduced defects, not pending-review
status or additional platform-scope requirements.

Inspected immutable author build
`/private/tmp/m23-author-return-29q8b36l/producing-build`, ID
`0b12665f0bf741bd87a7c68105189f5b`; all31 artifact hashes verify. All eight
fabricated/dummy tests independently pass. Retained optimized code contains
the actual mutex lock/trylock, sched_yield, condition wait and timed-wait calls;
the source's missing-yield branch bypasses the real call exactly once. The
new counterexamples below are absent from that suite.

### L1-C1 — contradictory synchronization observations pass as normal

`check.inspect` checks phase presence/order but leaves meaningful returned
predicate fields unchecked. Through actual `load`, `progress_keys` and
`validate`, each separate epoch2 mutation still returns STRUCTURAL_PASS:
owner `release_receipt.a=0`, controller `owner_done_observed.a=0`, owner
`held_publish.a=0`, or peer `held_observed.b=0`. These records contradict the
claimed true release, completed owner and retained target-lock ownership.
A receiving observation cannot prove its synchronization edge if its recorded
value is ignored.

Smallest remedy: bind each required publisher/receiver field to its declared
predicate, exact epoch, actor and mask; retain actual publication-versus-receipt
distinctions. Cover the analogous ready/release/owner-done and wait/deadline
fields in the same pass, rather than adding only four special cases. Closure:
the four isolated contradictions reject specifically while valid N=1/2/4/8
traces and legitimate per-actor interleavings continue to pass.

### L1-C2 — partial controls can pass without their actual cause records

The checker fully reconstructs only epochs with `epoch_done`. The failing
control epoch necessarily lacks that marker for held/stale-ack/ordering, and
its separate negative predicates are insufficient. Each of these deletions
still produces EXPECTED_CONTROL_FAILURE through the complete loader and live
progress consumer:

- Held: remove every owner and peer epoch2 record. Controller held/withheld
  labels alone remain, without actual owner lock/held/wait or peer-ready evidence.
- Stale-ack: remove peer1's entire epoch2 prefix. The controller's stale slot
  read is accepted without the actual protected operation and stale publication.
- Ordering: remove the owner's entire epoch2 prefix. A peer's bad read is
  accepted without the declared owner publication/unlock cause.

Smallest remedy: validate each control's required incomplete-epoch prefix,
including the actual actor/operation/value and receiving observations specified
by the method. Do not require an impossible complete epoch after intentional
termination, or use cross-thread timestamp sorting as a happens-before oracle.
Apply the same missing-cause challenges to frozen, missing-yield and lost-wakeup.
Closure: every deleted cause rejects as unresolved/missing evidence; fully
witnessed declared controls still classify at N=2/4/8.

### L1-C3 — exit lifecycle and timeout terminal identity are incomplete

Moving the owner's `exit` record to immediately after its startup CPU record,
before all20 epochs, still yields STRUCTURAL_PASS after sequence/ticks are
renumbered through the loader. `inspect` requires an exit somewhere but does
not make it terminal in that actor's lifecycle; joins likewise need their
completed-actor semantics rather than presence alone.

Separately, a held fixture with `no_progress=true` and actual SIGTERM instead
of parent SIGKILL still yields EXPECTED_CONTROL_FAILURE. Source `supervise`
uses `process.kill()` for its live native child, so an unrelated signal winning
that race must not be called the expected watchdog termination.

Smallest remedy: require each actual exit to finish its actor's obligations
and be the final actor record; require correct create/join identities and
lifecycle. Bind expected no-progress containment to the declared parent kill,
with no total-time/resource/error outcome. Preserve missing/partial prefixes
as unresolved where required evidence is absent. Closure: early exits/joins
and unrelated exit/signal categories reject; legitimate killed prefixes remain
recoverable without fabricated joins or cleanup.

### L1-C4 — unexpected transitions can renew the no-progress clock

`progress_keys` skips validation of start/join/exit roles and accepts increasing
epoch/rank values without required predecessor states. Starting from only the
valid start record of each actor, each of these additional records adds a new
eligible key: controller `join` with target999, peer `join` with target1, or
peer `ack_publish` at epoch19 before any held/ready/attempt operation. Thus
invalid activity can postpone the five-second dependency watchdog until final
checking or the30-second cap; the method requires unexpected transitions to
stop as invalid rather than renew progress.

Smallest remedy: validate role, epoch, join target/lifecycle and required
per-actor phase prerequisites before granting progress. Keep legitimate
optional EBUSY records and normal wait/API logging interleavings admissible.
Closure: these three prefixes reject immediately; a small dummy trace with
unrelated output or invalid join/ack activity cannot extend the intended
no-progress interval. Helper work and partial bytes must remain ineligible.

### Reproduction and retained sound scope

Reusable `/private/tmp/m23-review-adversarial.py` imports the immutable author
snapshot, builds the supplied valid fixtures, applies the nine mutations for
C1–C3, writes actual actor files, normalizes sequence/ticks and runs `load`,
`progress_keys`, then `validate`. Every contradiction currently false-passes.
For C4, create `prefix={a:[deepcopy(rs[0])] for a,rs in rows.items()}` from
`tests.fixture()`, then append a copy of that actor's start record with seq1
and the phase/target/epoch fields described above. `progress_keys` currently
adds one key for each independent case. These reproductions were sent to the
code author; this reviewer authored no production correction.

The actual source uses separate target/control mutexes and predicate loops,
checks thread creation/join and API outcomes, retains externally written actor
prefixes, and builds the exact shuffled216-positive plus63-control schedule.
The timed-wait control records its actual return after checked realtime deadline
construction. Frozen-peer observation follows the branch whose wait releases
the controller mutex. Requested QoS/CPU fields are not presented as P/E proof.
The137-actor maximum and per-role raw-record caps fit the declared buffer bound;
the whole campaign process group and child timeout preserve partial files and
not-run outcomes. These sound elements do not close the counterexamples above.
No added native sample, changed count, host sleep/power operation, stronger
clock claim or guest implementation is requested. Return corrected consumers,
focused regressions and a fresh retained compile-only packet to this same
independent reviewer before execution. Timing, actual preemption, placement,
guest warnings and Q18/Q19/Q21/Q22/P1 implications remain unresolved as declared.

## L1-C1–C4 same-reviewer correction return — accepted, 2026-09-05

Reviewer `/root/mach_native_review`, unchanged independent context. Inspected
all changed producer/consumer/supervisor code, the revised ordinary-wait
interleavings and cause fixtures, and retained final packet
`/private/tmp/m23-c1-c4-final-5zy_ort6/producing-build`, build
`12c01ea5a5cc41a9a63dac9df8aea775`. All32 artifact hashes verify; retained
C/Python sources equal the submitted current implementation. No M23 native
liveness or timing experiment ran during review.

**Verdict: ACCEPT the complete M23-L1 instrument for exactly the reviewed
279-child campaign after coordinator confirmation of native exclusivity.**
L1-C1 through C4 are closed at this return. There is no remaining substantive
blocker to the declared structural/correctness collection; this verdict does
not accept observed liveness, timing, guest behavior or preemption attribution.

C1 now binds the recorded publication/receipt predicates, exact epoch/mask,
actor fields, API outcomes and ordinary-wait accounting. The realtime identity
is pinned by the retained SDK declaration and native compile-time assertion;
its deadline is checked independently without granting clock precision.
C2 reconstructs the actual required actor prefix even when the control epoch
cannot complete, then checks publisher/receiver evidence after reap. Missing
owner held/data publication or peer ready/ack publication no longer becomes an
expected negative. The frozen case preserves the legitimate earlier first
ordinary wait pair: the distinct frozen marker plus controller observation
under the mutex establish the later wait path; a second compressed ordinary
pair is not fabricated. An unrecorded required cause stays unresolved.

C3 requires the actor's complete program order, final exit and exact joins
associated with the joined actor's retained exit. Held/frozen termination
requires actual SIGKILL, a recorded parent kill request and no-progress as its
sole stopping condition; unrelated signals, total timeout, resource and observer
errors reject. C4 runs the shared local prefix/value validator before granting
progress. Wrong-role/target joins and future acknowledgements fail immediately;
helpers and ordinary wait activity cannot renew the watchdog. Final cross-actor
cause checks are deliberately separate from live local-prefix checks, since
successive file reads do not form a simultaneous snapshot. Live join checks
are consistent with controller-first file reading and exit-before-successful-
join logging. No cross-thread timestamp sort supplies synchronization order.

Independently reran all nine original review mutations against the final
retained snapshot: every case now rejects its predicate/cause/lifecycle/timeout
contradiction. All13 retained synthetic/dummy tests pass, including invalid
progress termination, exact control causes at2/4/8 workers, legal try/yield and
compressed wait paths, and process-group containment. Additionally passed every
cut of every actor file in the eight n=8 normal/control fixtures through the
prefix validator:9,538 admissible partial-prefix checks. This specifically
challenges accidental rejection of retained incomplete output, while the
negative fixtures challenge impossible prefixes. These are synthetic results,
not native scheduler observations.

The native diff adds only the realtime-identity compile assertion; the actual
mutex/condition/yield/control producer branches and their reviewed disassembly
remain unchanged. The return retains the exact216 shuffled positives and63
ordered controls,20 epochs with only the first two excluded from future
qualified interval summaries, fixed seeds23001–23003/schedule seed23000, N=
1/2/4/8, requested UI/background QoS and H=0/C/2C with C from hw.ncpu in1..64.
No extra repetition, removed adverse sample or control replacement is admitted.
The controls are deliberately injected dependencies/protocol faults; some
missing-cause races can remain unresolved and must stop, rather than being
rescued with permissive terminal categories.

Bounds remain30 seconds per child, five seconds without a new valid eligible
transition,32MiB per native output file,256MiB declared application-buffer
ceiling (not total RSS),10,800-second complete stage,1GiB recursive operational
output threshold,2GiB free reserve and10-second preparation commands. The outer
process group covers build/check/native work; polling overshoot and failed
partial receipts cannot count as success. Stop on the first unexpected,
unresolved, integrity or resource outcome, retain partial prefixes/suffixes and
not-run entries, and use no outcome-driven replacement. Root coordinates the
exclusive slot; the independently running T2 replica must first be terminal.
A separate same-scope replication preparation and findings review remain due
before accepting new observations. Actual OS preemption, sustained helper
occupancy, P/E placement, clock/latency bounds, real guest warnings, P1/P2 and
Q18/Q19/Q21/Q22/Q05-06 implications remain open exactly as declared.
