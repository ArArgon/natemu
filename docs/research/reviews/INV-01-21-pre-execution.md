# INV-01-21 integrated pre-execution method review

## Independent complete-code replacement review — 2026-09-05

Reviewer `/root/ordering_native_review`, independent of M21's method authors
and code author `/root/mach_baseline_replication`. The coordinator reported
that reactivating the preceding reviewer `/root/track01_inventory_check` failed
with `agent thread limit reached`. This is a transparent replacement review,
not approval attributed to that reviewer and not author self-review. I read
the research workflow, full accepted integrated method, preceding method
returns, latest author return and all eight submitted experiment source files.
I authored no M21 implementation or method correction.

**RETURN for M21-C1–C5 below; no native execution clearance.** The inspected
callback ownership protocol and finite campaign remain useful, but the
supervisor/consumer presently violate concrete accepted observation and stopping
rules. Missing native samples are not the reason for this verdict. All remedies
are bounded source/synthetic work; no power transition, lifecycle/timer/clock
campaign or additional original slot is required to reproduce them.

Inspected immutable packet:
`/private/tmp/m21-author-return-_jzz7bal/producing-build`, build ID
`a2d0a85cdac4463d9457c5cc06540469`, build digest
`4b3c5558d70485d55ae7d3b15f2d3a9be70de095e6ffd6ed53db70cbc8a77561`.
All36 retained artifact hashes verify and all snapshot source bytes match the
submitted directory. The eight retained Python groups pass with the documented
`M21_REPO=/Users/liaozping/orca/natemu` environment, including the actual
serializer fixture; the compiled core fixture independently passes. No lifecycle
entry point was invoked. The additional reusable reproduction is
`/private/tmp/m21-review-adversarial.py`, importing that immutable snapshot.
It uses fabricated raw traces and, for C1, the actual supervisor with an injected
clock/resource oracle and a Python pipe dummy. It changes no host setting.

### M21-C1 — deadline-driven phase announcements can disappear

`run.supervise` calls `phase.receipt(...)` while handling notifications and
operator receipts. That method internally calls `advance(now)`. At the later
bottom-of-loop announcement check, `previous=phase.phase` already contains the
new phase, so the comparison detects no change. The required operator-visible
marker is lost if a receipt was the first service at/after the deadline.

The actual-supervisor fixture sends ARMED and a valid pre-screen pair at
awake0, then an unrelated notification at awake121. The internal phase becomes
`await-induction`, but the retained phase events are only `phase-pre` and later
`phase-window-ended`: **no `phase-await-induction` is emitted**. The operator
instructions require waiting for that marker before submitting induction.
The same path can omit `phase-post` when a callback first services the target
cutoff. Correct target bucket cutoffs alone do not repair the missing protocol
announcement.

Smallest remedy: give every phase transition one shared announcement path,
including transitions caused inside receipt processing, preserving the rule
that a deadline closes before admitting a callback at/after it. Check the
actual supervisor, not only the pure Phases class. Closure: callback-first and
operator-receipt-first servicing at both cutoffs each emits exactly the expected
phase marker; late target callbacks remain excluded and normal window ceilings
remain unchanged.

### M21-C2 — lifecycle presence is accepted without lifecycle order

`check.classify` counts registration, admission closure and completion but does
not validate their order relative to ARMED, normal stop and one another. Three
separate mutations of the otherwise accepted completed C2 fixture still return
`completed`: admission closed before registration/ARMED; completion before
registration/ARMED; or registration only after the whole window and normal stop.
Receipt ordinals and awake times remain valid and ordered in each reproduction.

These are instrument contradictions, including an absent admitted observer
window, rather than evidence of OS notification absence. The actual producer
already has a concrete lifecycle, so no new runtime protocol is needed.
Smallest remedy: validate that lifecycle and the arm-specific control edges in
the independent consumer. Preserve the legitimate trailing notification receipts
after admission closure: admitted callbacks are drained and forwarded before
serialization, so closure is not itself the final notification receipt. Closure:
all three mutations reject; valid late admitted callbacks, orderly drain,
normal/early-control/reversal stops, C1 token end and complete serialization
remain admissible in their actual order.

### M21-C3 — T5 accepts acknowledgements after its observation ceiling

`trace` adds actors to `background_seen` from every raw row before applying
the observation-window selection. `classify` uses that global set for the T5
self-control. A fixture with all actors initially USER_INITIATED and their
first BACKGROUND read-back at awake302 seconds passes
`self-request-acknowledged` for the300-second arm. Each row and summary is
otherwise coherent; the post-window demotion summaries are true.

The accepted method requires every worker/timer acknowledgement **at the
observation ceiling** and makes missing acknowledgements a self-control failure.
A genuine acknowledgement during shutdown does not satisfy that condition.
Smallest remedy: bind the publication and each actual API/read-back witness to
the admitted observation window using its declared clock/receipt boundaries;
keep teardown records as raw evidence. Closure: the302-second case rejects,
within-window acknowledgements pass, and exact-cutoff handling is explicit.
This concerns harness correctness, not a new latency or effective-QoS claim.

### M21-C4 — missing clock-read witnesses can become valid continuity evidence

Starting from the valid raw fixture, replace every actor's `continuous` witness
with seven zeroes and every per-clock witness with seven zeroes. Keep the
positive raw sample rows and reserved counts unchanged. `classify` still
returns `completed`, with advancing clock categories. The checker validates
the witness array shape but does not bind read counts to the actual rows or
require the ordered actor's pre-gate witness to exist.

This contradicts the required corruption/missing-witness oracle for the R5
repair: the producer's comparison of every actual read, including a potentially
unsampled reversal, cannot be treated as present when its retained read count
is zero. Smallest remedy: validate source-derived count and witness consistency
for ordered workers/timer, including nonzero pre-gate coverage, per-row clock
read counts, bounded reversal counts and coherent first-pair fields. Do not
invent a requirement that concurrent notification callbacks form an ordered
clock stream, or that every busy-loop read was emitted as a heartbeat.
Closure: the all-zero mutation and inconsistent reversal/count witnesses reject;
valid sparse heartbeats with extra pre-gate reads and the1000→900→1100 witness
continue to pass with their conservative categories.

### M21-C5 — a retained non-continuous reversal does not stop further launches

The accepted Exclusion and stopping rules say any observed raw reversal stops
further launches for independent instrument investigation. `classify` instead
sets `fatal` only from `clock_backward[1]`. Mutating one ordered worker's Mach
absolute samples from1,000,000,000 to900,000,000, with a matching per-clock
reversal witness, yields `clock_backward=[true,false,false,false,false]`,
`status=completed`, `fatal=false`; `stop_reason([result])` is None. The
campaign therefore continues despite the declared stop.

Smallest remedy: apply the accepted stop to retained reversals in the diagnostic
clocks too, without promoting them into the continuous-clock scientific
falsifier. Closure: one supported raw decrease in each clock independently
stops further launches and retains the observation; only the stated continuous
clock remains eligible for its separately reviewed host-failure inference.
If the author intended a different stopping rule, return that explicit method
change for independent examination rather than silently changing it in code.

### Inspected sound scope and return conditions

The actual callback gate closes admission under a mutex, keeps context/tokens
at process lifetime, drains admitted callbacks and joins dedicated writers
before serialization. Unique reservation is separate from release-committed
payload validity. The mutex-ordered receipt index plus acquire count lets the
main thread forward committed records in completion order even when source
slot0 pauses behind slot1. The inspected core fixture exercises that interleaving,
closed admission and drain. The source retains no freed callback context;
observer removal is not used as a lifetime barrier. No defect was found in
that protocol during this bounded review.

Rechecked SDK26.4 `mach/mach_time.h`, AppKit `NSWorkspace.h`, Foundation
`NSProcessInfo.h` and `nanosleep(2)` clauses used here: distinct notification
centers and uncontrolled global callback contexts remain explicit; continuous
time's sleep behavior, unknown/unsupported property fallbacks and nanosleep
interruption/longer-sleep semantics are not expanded into delivery, placement,
cadence or precision guarantees. Retained disassembly contains the actual
pthread and nanosleep operations. The timer's injected-remainder tests and
pre-density reversal helper tests pass.

Independently recomputed the unchanged96 rows,1,200 observation minutes,
480 per-slot overhead minutes and180 T1 sleep minutes:31 hours plus120 shared
minutes within the36-hour campaign ceiling. The120-byte record layout gives
63.75 MiB for17 arrays; the receipt queue, declared scratch and explicit thread
stacks fit the application-owned128 MiB bound. This is not an RSS bound.
File/slot/campaign/free-space thresholds, separate awake/continuous limits,
operator-dependent unavailable slots, no automatic retries and missing private
trace after kill remain stated and represented in code. The reviewed problems
require no schedule reduction, extra samples or stronger timing claim.

Return all five corrections, focused reusable regressions and a fresh retained
compile-only packet to this same reviewer. No native lifecycle, power, timer,
clock, notification or guest-policy findings are accepted here. The broader
default-center delivery, duration-matched baseline, external progress observer,
P1/headless/signing, guest composition and containment obligations stay open.

## Independent source/method return — 2026-09-05

Reviewer: Codex `/root/mach_native_review`, separate from M21-1 author
`claude/t01_author_21`. Read the research workflow, complete current
INV-01-21, REV-232 author return and REV-225 RR1–RR10. Inspected the supporting
SDK26.4 headers/man pages directly and the primary Apple documentation named
below. No experiment code was authored or executed; no sleep, display, power,
thermal or host setting was changed. Only this review thread was created.
The historical reserved REV-233 is not a new workflow requirement.

**Verdict: return the integrated method for the concrete corrections below.**
The return fixes several historical contradictions, but it does not yet define
a reproducible finite campaign or fully observable classification/record-lifetime
rules. Missing implementation is preparation status, not by itself a defect.
Authors may prepare code and synthetic validation under the current workflow;
this review clears no native execution and accepts no lifecycle finding.
The eight arms × N={1,2,4,8} × three planned seeds21001–21003, and their declared
per-arm observation windows, remain unchanged by this review.

## Primary-source assessment

The following exact documentary clauses are independently verified for the
method's assumptions. They supply no runtime observation or broader delivery,
rate, scheduling, placement or recovery guarantee. Routine source pin/range
history does not substitute for these supporting clauses.

| Supporting primary material | Verified meaning and limit |
| --- | --- |
| SDK26.4 `usr/include/mach/mach_time.h`, `mach_timebase_info`, `mach_absolute_time`, `mach_continuous_time`; `usr/share/man/man3/clock_gettime.3`, DESCRIPTION and resolution paragraph | Continuous Mach time is described as advancing during sleep. CLOCK_MONOTONIC includes sleep; CLOCK_UPTIME_RAW excludes it and is identified with converted mach_absolute_time. `clock_getres` returns a resolution value that may be smaller than actual clock precision. A converted raw tick and a reported resolution are representation/resolution facts, not independently measured sampling accuracy or latency qualification. |
| SDK26.4 AppKit `NSWorkspace.h`, `notificationCenter` and sleep/wake/screen/session declarations | Workspace observers must register on the workspace's own notification center. The declarations do not establish delivery deadlines, completeness, callback thread or an end-to-end wake acknowledgment. |
| SDK26.4 Foundation `NSProcessInfo.h`, thermal/power-state notification comments and state properties | Thermal and power-state notifications use the default notification center and are posted on a global dispatch queue. Nominal thermal/NO low-power values have documented unknown/unsupported fallbacks. Workspace screen delivery therefore does not validate these other notification paths. |
| SDK26.4 `pthread/qos.h`, `pthread_set_qos_class_self_np`, `pthread_get_qos_class_np` and override discussion | Set/get concern requested QoS; overrides need not appear in that read-back. T5 cannot infer actual scheduler placement, effective service or demotion from requested-class equality. |
| SDK26.4 `dispatch/source.h`, DISPATCH_TIMER_STRICT and `dispatch_source_set_timer` | The strict flag requests best effort; some delay remains possible, and the header cautions that it may override power-saving techniques. `start` selects the timer's clock: dispatch_time/DISPATCH_TIME_NOW uses uptime; dispatch_walltime uses wall time. Neither a requested 100 ms period nor strictness proves a fixed callback cadence or excludes ordinary scheduling delay. Apple's [strict-timer documentation](https://developer.apple.com/documentation/dispatch/dispatch_timer_strict) gives the same best-effort scope. |
| SDK26.4 `sys/sysctl.h`, `hw.nperflevels` and `hw.perflevelN.*`; `sysctlbyname(3)` | The selectors describe core types and current/max physical/logical counts; level0 is the highest-performance type. They provide topology inventory, not an actual executing-core placement observation. |
| SDK26.4 `pmset(1)`, GETTING/OTHER ARGUMENTS and settings sections | `sleepnow`, `displaysleepnow`, settings inspection and assertion summaries are documented. Assertion summaries identify possible inhibitors, not a future guarantee that sleep will occur. The inspected man page does not document the specific `lowpowermode` setting/transition parser the method proposes, so that command's exact induction/confirmation contract remains an explicit source gap. Apple's [Power Modes documentation](https://support.apple.com/en-us/101613), published June5,2026, documents the desktop Energy settings route; the method's Battery-only fallback is not the documented desktop route. |

These clauses close the historical reversed-clock and wrong-notification-center
source questions at their stated documentary scope. No IOKit surface was added.
No new clock is silently promoted into the method's existing backward falsifier,
which remains restricted to mach_continuous_time. T2 and T3a/T3b/T4 detectability
remain diagnostic; T5/C1 remain harness self-observation; frozen remains
unreachable. The new source review does not transform those limits into passes.

## Historical return disposition

| REV-225 item | Assessment of the current return |
| --- | --- |
| RR1 heartbeat capacity | Time-based admission repairs the iteration-rate explosion. 18,000 nominal records/30 minutes and a 32,768-slot allocation are arithmetically consistent for awake service without catch-up. Actual cadence, under-test-clock gating and complete byte accounting remain addressed below. |
| RR2 watchdog clock | Corrected to the sleep-excluding clock; missing T1 wall/sleep allowance is still material. |
| RR3 delivery center | Correctly narrowed to T1; default-center observations are diagnostic. A live center before/after is not proof of delivery throughout the intervening sleep boundary. |
| RR4 concurrent writes | Atomic slot reservation prevents two callbacks from choosing one slot. It does not yet define completion publication or shutdown/drain ownership. |
| RR5 pre/post control | Both controls and their intentional-confounder exception are declared. The absent-wake branch and finite classification cutoff remain undefined. |
| RR6 thresholds | One 300 ms threshold is now named, but numeric leeway, cause-exclusion evidence and the diagnostic-versus-decision rule remain inconsistent. |
| RR7 pending clocks | Conservative backward-falsifier restriction is explicit; no clock receives a stronger interpretation merely from a pending label. |
| RR8 outcomes | First/second delivery-control failures and disagreement→not-measured are now explicit. The separate unrestricted fresh-seed rerun clause still breaks the finite schedule. |
| RR9 topology | Mechanism and lack of placement observation are correctly separated. Unsupported selectors must remain unavailable fields at the future code review. |
| RR10 provenance | Current snapshot/build/actual binary/checker/run binding and stale-build fixtures correctly preserve the substantive concern without reinstating clean-tree or review-receipt checks. Implementation validation is still owed. |

## M21-R1 — aggregate/sleep budgets and control schedule are unresolved

**Evidence and consequence.** The declared ceilings sum to
30+15+10+10+20+5+5+5 =100 minutes for one repetition across all eight arms.
Four N values × three repeats therefore gives 1,200 minutes (20 hours) of
summed awake-window ceilings for the fixed 96 children, before warm-up,
pre/post controls, preparation/checking or T1 sleep. There are12 T1 children,
each with an awake-only30-minute limit but no maximum planned sleep duration
or total wall allowance. The phrase that this window includes sleep/wake
conflicts with its correctly specified sleep-excluding clock. There is no
aggregate wall/output limit or complete notification/control-buffer byte budget.

C2 is simultaneously a fixed five-minute arm and a control run for the same
window length as its matched arm. One such C2 child cannot be both five minutes
and the30-minute T1 match. The exclusion paragraph also permits crash, kill,
disk-loss or checksum failures to be rerun under fresh seeds outside the fixed
three, without a replacement count/budget. Thus neither the actual finite
schedule nor its maximum resource use is defined by the current96-row plan.

**Smallest remedy/closure.** Preserve all96 original rows/windows. Declare
whether the five-minute C2 is only a short baseline, with its resulting limit,
or whether separately enumerated supplemental matched controls are necessary;
do not silently replace its window or count extra controls as those original
rows. Give the T1 sleep allowance, aggregate sleep-aware wall accounting,
preparation/teardown/checking allowance, complete buffer/output limits and
failure preservation rules. A same-host supervisor cannot execute while the
host sleeps; specify overrun classification and stop on its first resumed
opportunity rather than promise an in-sleep kill. Remove automatic replenishment
or enumerate separately reviewed diagnostic work that cannot enter the original
three-repeat denominator. Code preparation can validate arithmetic and stop
semantics without inducing a transition. A full planned96-row manifest plus
explicit additional obligations must have one auditable finite bound.

## M21-R2 — callback reservation is not completed-record publication

**Evidence and consequence.** The per-source fetch_add reserves unique slots,
but the method says the post-run checker is the only reader and therefore the
array is correct regardless of callback thread. The child must first read and
serialize that array itself. There is no callback-admission stop, completed-slot
publication, observer removal/drain condition or producer join rule before
“closes its ring buffers, writes one raw trace file.” Concrete interleaving:
callbackA reserves slot0 and pauses; callbackB writes slot1; the window closes;
the serializer trusts write-index2 and reads unwritten/partial slot0; callbackA
then writes during or after finalization. No slot collision is needed.
The private in-memory-only worker buffers likewise have no specified recoverable
flush path when the supervisor kills a stuck child, although timeout is said to
preserve partial raw output.

**Smallest remedy/closure.** Specify ownership through final serialization:
stop admission, establish which callbacks/worker/timer writers have completed,
and consume only committed records with an explicit incomplete-record outcome.
Use a small serial/drain or completed-slot protocol; a reservation count alone
must not imply completeness or chronological order. Declare what raw evidence
survives an uncooperative child kill, and otherwise retain timeout plus missing
trace honestly as an instrument limit. Add the exact reserved-but-unwritten
slot during shutdown and kill-before-flush fixtures alongside the existing
two-concurrent-callback fixture. No framework or native transition is needed
to make this method rule concrete.

## M21-R3 — progress thresholds have no observable cause-exclusion oracle

**Evidence and consequence.** A requested100ms heartbeat/timer is an upper
bound on record density when serviced, not a guarantee of a100ms actual cadence.
A worker unable to run cannot execute the read/append merely because100ms has
elapsed. “Minimal explicit leeway” supplies no numeric leeway or selected start
clock, and the timer is alternatively a dispatch callback or nanosleep loop
with different thread/queue and T5-demotion semantics. Strict dispatch is only
best effort and can itself alter power-saving behavior. The exact timer actor,
queue/thread ownership and demotion polling point therefore matter to the
transition being observed.

Two traces with a400ms gap in two planned repetitions are consistent with both
ordinary scheduling delay and the claimed host liveness failure. The method
records no observation capable of separating those explanations; repetition
alone cannot exclude a systematic scheduler/coalescing cause. Yet the claim
and metric rows use the two-of-three rule to falsify, while the supplement
says attributable scheduling/coalescing does not falsify. Finally, the last
classification paragraph says recorded intervals never participate in a
threshold without Q07 qualification, while the same method uses300ms for a
load-bearing threshold without it. Calling a number qualitative does not
resolve these two rules.

**Smallest remedy/closure.** Freeze the actual timer backend, start clock,
numeric leeway and actor/QoS protocol. Define300ms as an explicitly bounded
operational gap category and retain its causal ambiguity, or supply a concrete
independent observable that distinguishes scheduler/coalescing from failure
before permitting the stronger falsifier. Keep the nominal period separate
from realized cadence, and make the interval/decision rule consistent. This
need not import a performance-latency claim or require Q07 to precede all
qualitative work. The reusable fixture is identical recorded400ms gaps under
two different hidden causes: absent additional evidence, the checker must not
assign a different causal result or silently rule either cause out.

## M21-R4 — absent-wake classification lacks a finite independent boundary

**Evidence and consequence.** T1's post-control step begins once the child
wakes and logs its didWake result, including a not-bracketed result. If that
notification is absent, the method gives no independent child/control marker
or deadline that decides absence and starts the post-control before the
observation window expires. “Before and/or after” and “correlates” also leave
missing-one-of-pair, late arrival, duplicate arrival and uncertain external
clock alignment without exact bucket rules. Pre/post screen notifications on
the same center establish availability at those observations only; they cannot
prove a particular wake observer remained registered or that a delayed wake
callback was deliverable during the intervening interval.

**Smallest remedy/closure.** Use the independent confirmed-wake record to
advance the protocol regardless of the notification under test. Declare
separate pre-control, induction/wake and post-control phases, bounded observation
cutoffs and ordinal/correlation rules; classify only after those independent
steps complete. State whether a partial pair is bracketed, incomplete or
unresolved and how late/duplicate events are assigned. Unless a stronger
surface contract is established, report notification-not-observed within the
admitted window rather than prove the path was dead or the host could never
notify. Reusable cases: confirmed wake with zero didWake callbacks but a valid
post-screen pair; a callback arriving after the cutoff; and both screen controls
passing with a missing target observer. They must have deterministic, honest
outcomes without waiting for the missing event to authorize the test of its
absence.

## M21-R5 — the worker's clock gate can suppress its own backward witness

**Evidence and consequence.** A worker records only when the current
mach_continuous_time is at least100ms beyond its last recorded heartbeat. With
last recorded value1000ms, an actual loop read900ms, then a later read1100ms,
the described gate omits900 and retains1000,1100. Its emitted stream appears
strictly increasing despite an observed backward read. Unsigned subtraction
instead risks wrapping the negative delta into an enormous elapsed interval;
the method declares no overflow/backward branch. This is a specific reachability
problem for that worker's claimed clock-failure test, not a demand that finite
sampling detect every unsampled transient. The current claim covers observed
decreases and does not disclose that this sampling gate excludes them.

**Smallest remedy/closure.** Either retain one bounded backward/invalid-clock
witness before applying the normal density gate, with checked conversion and
subtraction, or explicitly exclude that gated worker stream from the backward
oracle and retain the remaining observer limitations. Normal logging must stay
time-bounded. Add1000→900→1100 and conversion/subtraction-boundary fixtures;
the method must not classify the actual observed reversal as established
continuity solely because its own gate removed it.

## Disposition and next action

Return M21-R1–R5 with explicit amended rules and synthetic witness expectations
in INV-01-21; this same independent reviewer should inspect the changed method
and affected source assumptions. Preserve the original planned denominator,
not-measured/ambiguous outcomes, the absence of an App Nap observer, T1-only
detectability falsifier, same-M4 scope and lack of actual placement. Missing
source support for the proposed pmset lowpowermode command remains a named
input gap; the documented desktop UI route is available for an author return,
not an action performed in this review. Routine editorial references to pending
historical review numbers do not block progress under the current workflow.

No M21 lifecycle, timer-delivery, guest-progress, host-failure or platform-wide
claim is accepted here. No code/native stage is cleared by this method/source
review; implementation and its meaningful synthetic controls still require
pre-execution inspection after these substantive rules are resolved.

## Independent replacement review of integrated M21-R1–R5 return — 2026-09-05

Reviewer/context: `/root/track01_inventory_check`, independent of both the
original method author and the integrated-return author `/root`. The coordinator
reported that reactivating the original reviewer `/root/mach_native_review`
failed with the tool error `agent thread limit reached`; this replacement is
explicit rather than a claim that the original reviewer approved the return.
I read the workflow, full current investigation, original review and all five
returns above. I authored no M21 method or code. No native experiment, power
transition, UI action or host setting change was performed.

**Accept the integrated source/method return for code and synthetic preparation.
M21-R1–R5 are closed at that scope.** No substantive method blocker remains
to preparing the specified instrument. This is not complete pre-execution
approval: no implementation exists to inspect, and actual code, retained-build
path, supervision and meaningful synthetic controls require independent review
before native collection. No lifecycle, clock-failure, timer-delivery, guest
progress, KA disposition or platform-wide finding is accepted here.

### Source and inference checks

Independently inspected SDK26.4 `usr/share/man/man2/nanosleep.2` in full. It
supports a relative sleep by the calling thread, possible longer elapsed sleep,
success on elapsed request, interruption by an unmasked signal regardless of
SA_RESTART, and EINTR with remaining time. The API exposes neither a clock-ID
selection nor leeway. The fixed persistent pthread and explicit
`nanosleep-relative`/`API-defined`/`not-exposed` fields correctly avoid inventing
a deadline or cadence guarantee. Validated remainders, stop/demotion polling on
every return, permanent-error handling and one completed-request fire remain
implementation obligations, with the required synthetic cases listed.

Rechecked `mach/mach_time.h`, `clock_gettime(3)`, workspace notification-center
and sleep/wake declarations, and Foundation thermal/power property and
notification comments. They support the sleep-inclusive/sleep-exclusive split,
the distinct notification centers, concurrency possibility, and nominal/NO
unknown fallbacks. None supplies a notification deadline or proves effective
QoS, core placement, continuity of execution, or a guest time policy. Apple's
current [Power Modes documentation](https://support.apple.com/en-us/101613)
documents Energy settings on desktops; it does not validate an unretained
operator action. The amended T3 prerequisite correctly requires external
before/after state and leaves inaccessible slots not-measured. The undocumented
pmset lowpowermode parser is no longer an assumed induction oracle.

### Return disposition

| Return | Independent assessment |
| --- | --- |
| M21-R1 — finite resources and denominator | Recomputed all 96 table-order/N/seed slots: 1,200 observation minutes, 480 per-slot overhead minutes and 180 T1 sleep minutes give 1,860 minutes/31 hours. Adding 120 shared minutes gives 33 accounted hours inside the 36-hour campaign threshold; operator waits consume that same ceiling. Original windows are intact; C2 is explicitly only a five-minute baseline, and matched long-window controls remain owed. No automatic replenishment exists. Per-slot awake and sleep-inclusive ceilings, resumed-overrun handling, output thresholds and unrun remainder are explicit. |
| M21-R2 — publication and lifetime | Admission and active count share one mutex; unique reservations acquire validity only through release-committed payloads read with acquire semantics. Shutdown closes admission, retains captured context, drains admitted callbacks and joins dedicated writers before serializing. Observer removal alone is not the drain. An uncooperative child timeout honestly loses its private records and produces no finding from them; parent-owned prefixes/outcomes survive. The original slot-0-paused/slot-1-complete interleaving cannot justify normal serialization while slot 0's callback remains active. |
| M21-R3 — timer and causal scope | The backend and owning thread are fixed; no hidden dispatch/nanosleep alternative remains. A 300 ms gap is a raw operational category with unresolved scheduling explanations, not a calibrated failure bound. Identical 400 ms traces under different hidden causes cannot receive different causal conclusions. The prior two-repeat causal-stall rule is withdrawn while the underlying question remains open. |
| M21-R4 — absent-wake boundary | Confirmed external wake advances the protocol without didWake. The 120-second pre-control, independent wake marker, 60-second target cutoff and 120-second post-control define bounded phases within the unchanged observation window. Target absence, lone members, duplicates, reversal, late receipt and ambiguous association have distinct conservative buckets. Screen controls validate only their own observations; missing target registration is instrument-invalid. |
| M21-R5 — reversal reachability | The adjacent raw-read comparison precedes the 100 ms density gate. The first pair/index witness and checked counter preserve an actually read 1000→900→1100 reversal; ordinary sampling cannot erase it. Conversion/timebase/counter defects are instrument errors, and no claim covers unsampled reversals. Subsequent independent investigation and replication still precede a host finding. |

Independently checked record arithmetic: 17 arrays at N8 ×32,768×128 bytes
are 68 MiB; controller markers and anomaly storage add at most 2 MiB. The
70 MiB record allowance fits within the 128 MiB application-owned allocation
cap. This is not a total-process memory bound, which the method explicitly
disclaims. Actual structure sizes, framework/capture lifetimes, streaming
serialization scratch and every allocation must be checked with the code;
capacity failures remain invalid outcomes rather than overwritten evidence.
The 18,001 normal-window density estimate is not needed as a sharp total-run
bound: warm-up/overhead and a sleep boundary also consume retained slots, so
the implementation must account for those and enforce the fixed capacity.

### Remaining complete-code review obligations

The code must implement phase receipt order with real synchronization rather
than infer it from concurrent callback reservation numbers. Reservation order
is expressly not chronological. Likewise, clock reversal checks need an actual
ordered actor/read stream, not an arbitrary merge of concurrently sampled
notification timestamps. These are direct applications of the accepted method,
not new platform premises.

The original observation ceiling dominates all T1 phase deadlines: a wake
marker too late to finish the admitted target/post-control sequence must leave
the attempt unresolved or incomplete at window end; it cannot extend the
window or admit a truncated control. Include that boundary in the declared
phase fixtures. Kill-before-flush, paused callback during shutdown, late callback
with retained context, and partially serialized output must exercise the real
producer/consumer ownership path during code validation. Dummy-child process
fixtures must exercise actual kill/reap and full not-run accounting without
inducing native host transitions.

Matched long-window comparisons, default-center/target-delivery validation,
stronger external progress observers, public containment alternatives, headless
and signing behavior, other host/releases and guest composition remain owned
unresolved work. The finite campaign cannot establish absence of every public
detection path or make frozen reachable. It remains a 96-slot preparation
method whose eventual bounded outcomes need independent replication/findings
review, with timing qualification separately assessed.
