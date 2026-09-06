# M21-1 lifecycle instrument — preparation only

Research status and the accepted method live in
[INV-01-21](../../docs/research/investigations/INV-01-21.md). Complete independent
code/pre-execution review is required before native collection. Compiling this
directory is not a campaign and does not induce transitions. No M21 native run
has been performed by the instrument author.

The producing executable is Objective-C/C using public Foundation/AppKit,
pthreads, Mach clocks and POSIX clocks. `core.h` owns the actual bounded
reservation, callback admission/drain, checked arithmetic and nanosleep retry
logic. `core_test.c` runs those helpers with fabricated inputs and synchronized
fixture threads; it reads no clock and invokes no timer or notification API.
`serializer_test.m` calls the actual producer serialization function on fabricated
records. Its compile-time pause hook is absent from the producing executable.
`tests.py` checks the separate consumer and kills/reaps dummy processes, including
that serializer before flush and after its first real output line.

## Build only

Use absolute resolved paths. The raw root must exist outside the repository and
cannot be `/` or an ancestor of the repository. Outputs must not already exist.

```sh
M21_REPO=/Users/liaozping/orca/natemu NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B experiments/INV-01-21/run.py build --output /Users/liaozping/orca/natemu-raw/m21-build-CHOOSE-FRESH-NAME
```

The command copies all sources/inputs into `snapshot/`, compiles that snapshot
with warnings-as-errors, ad-hoc signs/disassembles the producer, compiles/runs the
non-lifecycle core/serializer fixtures, runs the synthetic Python checks, and
retains toolchain, SDK, commands, exit streams, build identity and SHA-256 hashes.
It does not launch `lifecycle`. Failures leave the retained build and failed status.
Do not use Python `-O`: validation assertions in synthetic fixtures must remain on.

## Actual campaign interface (not executed during preparation)

After complete independent review and explicit exclusive native-slot clearance:

```sh
M21_REPO=/Users/liaozping/orca/natemu NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B /ABS/BUILD/snapshot/run.py campaign --build /ABS/BUILD --output /ABS/RAW/FRESH-CAMPAIGN
```

The first campaign preparation marker starts its 36-hour sleep-inclusive ceiling,
before build/input verification/copy and before operator waits. Build-only author
development is outside the eventual campaign. The verified build is copied into
the campaign so its sources, logs and binary all count toward campaign storage.
The full 96-row denominator is written before the first child: table order
T1/T2/T3a/T3b/T4/T5/C1/C2, ascending N=1/2/4/8 and seed21001/21002/21003. Seeds
only permute worker launch order. No automatic retry, extra slot or replenishment.
The instrument currently uses that exact order; it does not implement optional
operator deferral/reordering. Unavailable original slots remain not-measured.

T1/T3a/T3b/T4 require an operator `ready` or `unavailable` input for that exact
slot. Waiting consumes the same 36-hour ceiling. Operator input is immutable
JSON under `operator/`; the supervisor assigns authoritative receipt order when
it consumes each input. Do not use external timestamps to numerically align clocks.
To enqueue a marker (this command itself changes no host setting):

```sh
M21_REPO=/Users/liaozping/orca/natemu python3 -B /ABS/BUILD/snapshot/run.py mark --campaign /ABS/CAMPAIGN --slot 0 --kind ready --evidence /ABS/operator-input.json
```

The evidence JSON is an object. Its optional `files` object maps short labels to
actual absolute file paths; consumed files are copied and hashed in the child
directory with the original input retained. Original external times, methods,
context and association belong in this evidence object. Inputs never substitute
for actually performing or independently witnessing a transition.

For T1, wait for the child ARMED/parent `phase-pre` marker, perform the reviewed
screen control and restore the display. After the 120-second pre-control cutoff,
only `phase-await-induction` permits an `induction-start` marker. Its evidence
must include `files.assertions` (actual `pmset -g assertions` output) and `method`
equal to `sudo pmset sleepnow` or `Apple-menu Sleep`. Wait for the parent to retain
that marker before issuing the one actual sleep request. Do not retry induction.
Use an independent device to arrange wake within the original 15-minute allowance.

After actual resume, submit `wake-confirmed` with `files.independent` and
`files.pmset-log`; `entry`, `wake`, `records_agree` and `associated` are booleans
describing those real observations. Missing/disagreeing records do not confirm
the transition. The parent advances from this independent marker regardless of
didWake receipt. After its 60-second target cutoff, `phase-post` requests the
second screen control within 120 seconds. The original 30-minute awake window
dominates every phase; a late wake cannot extend it. A first failed pre-screen
control stops that slot before induction; the second failed T1 screen control
stops the campaign. A phase marker records actual parent receipt, not callback
generation time. Source-local reservation order never supplies temporal ordering.

T3a/b `transition-confirmed` evidence requires actual `before` and `after` files,
`route: "desktop Energy settings"`, and confirmed/associated booleans. The
independent record must identify the off→on or on→off direction for the original
arm. NSProcessInfo is under test and cannot establish induction itself. T4 uses
opportunistic independent evidence only; no deliberate thermal intervention.
`unintended-transition` retains confirmed external confounders. T2 has no App Nap
observer and remains not-measured for App Nap; no visible window/audio is created.
T5 requests self-demotion after ARMED; C1 requests one activity token after ARMED
and ends it at shutdown. Neither proves an OS-originated transition or effective
service/sleep suppression. C2 is only the original five-minute short baseline.

## Records, ownership and limits

Each child has N busy worker pthreads, one persistent timer pthread and a main
run-loop controller. The timer requests relative 100 ms nanosleeps, retries valid
EINTR remainders, polls stop/demotion before and after each call and emits exactly
one fire per successful request. No alternate timer, clock-ID or leeway is selected:
metadata is `nanosleep-relative` / `API-defined` / `not-exposed`. Dedicated actors
begin with checked USER_INITIATED requests; T5 actors demote themselves once.
Main retains USER_INITIATED; framework callback actors are uncontrolled and their
actual thread identity and QoS readback are retained.

The five-clock order is Mach absolute, Mach continuous, CLOCK_MONOTONIC,
CLOCK_MONOTONIC_RAW, CLOCK_UPTIME_RAW. They are sequential, not simultaneous.
Workers compare every actual continuous read before the 100 ms density gate;
the first decrease and checked anomaly count survive ordinary sampling. Timer
reads have their own ordered witness. Concurrent notification reads are not
merged into a fabricated ordered clock stream. Raw ticks, timebase, resolution
and per-clock diagnostics are retained; reported resolution is not accuracy.

There are N+9 fixed arrays of 32768 records. The current compile-time record
layout is 120 bytes including commit metadata; at N8 the arrays occupy 63.75 MiB.
The method's conservative 68 MiB array allowance remains unchanged. Controller
receipts are capped at 4096 records of 256 bytes, and anomaly metadata is below
1 MiB. Native allocation accounting reserves 2 MiB for ancillary buffers (including
the 1 MiB committed-notification index queue) and
1 MiB per dedicated thread stack, within the 128 MiB application-owned cap.
System/framework allocations and total RSS are not bounded by this declaration.
Worker heartbeats do no file I/O. Capacity/dropped-counter exhaustion invalidates
the cell; no overwriting or wrapping is permitted.

Admission and active callbacks share one mutex. An admitted callback reserves a
unique slot, writes its payload, release-commits it, then decrements active.
The callback gives each completed record a distinct receipt sequence while holding
the same mutex and publishes its index after release-committing the payload. The
main thread forwards acquire-committed notification identities in that receipt
sequence, preserving actual synchronized completion order across sources and
out-of-order source reservations. Parent receipt order is the classifier's phase
input; no callback generation-time guarantee is claimed.
At shutdown admission closes, observers are removed, active callbacks drain,
dedicated threads join and only then raw private records serialize. Context and
tokens remain allocated until exit; observer removal alone proves no drain.

The supervisor retains stdout/phase/operator/exit records incrementally. A killed
child loses any unflushed private records; an existing serialization prefix is
retained and hashed, never repaired or interpreted as a complete trace. The real
serializer fixture tests both kill positions. A drain/join/serialization overrun
is not normal completion and supplies no finding from lost data.

## Budgets and interpretation

Original awake windows are 30/15/10/10/20/5/5/5 minutes. Each slot has at most
five extra awake minutes including setup/warm-up/teardown/checking, warm-up at
most 60 awake seconds, and shutdown at most 60 awake seconds. T1 adds at most
15 sleep-inclusive minutes; other arms add none. Parent CLOCK_UPTIME_RAW and
CLOCK_MONOTONIC enforce the distinct awake/sleep-inclusive operational thresholds.
After actual resume, thresholds are checked before continuing; the sleeping host
cannot kill itself while asleep. The 120-minute shared allowance and 36-hour
campaign ceiling are separate from individual slot windows.

Thresholds are 256 MiB per file, 512 MiB per slot, 8 GiB for the whole campaign
and 2 GiB free reserve. They include copied build/input/log/failed artifacts and
are checked at service points, with actual overrun retained. They are not kernel
quotas. First resource/build/host integrity failure, crash or unexplained kill
stops further launches. More than two supervisor timeouts in an arm, the second
invalid T1 screen control, or first T5/C1 self-control failure also stops.

The streaming checker requires complete footer, all actor summaries, intact
sequence/counters/commit-derived rows, registration/removal records and exact
notification receipt-to-raw association. An observed continuous decrease stops
further launches for investigation; it is not self-acceptance of a host failure.
300 ms is only a raw diagnostic bin; identical gaps never identify a hidden
scheduling cause. A fully contained independent induction/wake receipt bracket
can annotate transition-overlap; uncertain alignment remains unexplained. Warm-up
records are retained, with normal-window gap/endpoint metrics using the supervisor
UPTIME_RAW window. End silence is censored. Frozen remains unreachable and every
timing result remains unqualified pending interval-specific independent review.

No full lifecycle policy, default-center delivery guarantee, headless/signing/P1
applicability, guest/vCPU composition, long-window matched control or public
containment conclusion follows from synthetic fixtures or a completed campaign.

## Complete-code review return M21-C1–C5

`test_returns.py` retains the five independent review reproductions and positive
boundary cases. The build runs these in addition to the original eight groups.
Every pure phase change queues exactly one announcement; the actual supervisor
drains those announcements through one path for periodic, callback and operator
service. A callback at a cutoff cannot enter the old target bucket, and neither
operator-facing control-phase marker can disappear because receipt handling
advanced the state first.

The consumer now verifies main-QoS → registration → ARMED → actual stop →
admission closure → completion, plus arm-specific request/token edges. Admitted
notification receipts may follow closure but must precede completion. A normal
window end, failed pre-screen control and observed continuous-reversal stop keep
their distinct conditions. C1's token end follows closure and precedes completion.

T5 records the parent command request before sending it. The child publication
receipt must occur within the admitted window; each actor's BACKGROUND read-back
must have a CLOCK_UPTIME_RAW sample from that request through the original
observation cutoff. The raw read-back exactly at the cutoff is admitted because
it bounds the already completed QoS read-back; one after it is not. Publication
receipt exactly at the cutoff is too late to establish the in-window request.
Teardown samples are retained but cannot repair a missing in-window acknowledgement.
These are operational admission boundaries, not an effective-QoS/latency claim.

Ordered per-row clock witnesses have exactly one read per emitted row and match
the retained adjacent decreases and first pair. The worker pre-gate count covers
its initial read, all emitted snapshot reads and at least its last retained loop
index. For the timer, with R fires and I EINTR returns, it is 1+3R+2I plus zero
or one final stop-poll read. Reversal counts/first-pair indexes are checked against
these domains. Notification actors retain zero ordered-stream witnesses; their
concurrent timestamps are not sorted into an invented ordered clock stream.
Any retained ordered clock decrease stops future launches. Only continuous-clock
decreases retain the method's separately reviewed scientific falsifier scope.
