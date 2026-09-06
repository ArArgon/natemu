# INV-01-07 — findings review thread

## Independent replication author preparation — 2026-09-05

Author/context: Codex `/root/mach_native_review`, separate from C1 author
`/root` and the native pre-execution reviewer `/root/ordering_native_review`.
This context authored no C1 implementation or checker changes. Its earlier
M06 native review and F08 supervision authorship do not supply C1 findings
acceptance. **Replication preparation is pending independent review; no
replication native execution or findings acceptance is recorded here.**

Inspected the research workflow, INV-01-07's question, limitations and full
M07-C1 method, accepted current full pre-execution return, operation README,
retained runner's build/launch/schedule/check/inventory paths and checker API.
The first campaign's producing build is
`/Users/liaozping/orca/natemu-raw/c1-build-20260905-track01-a`, build identity
`b795126d1d8b41c5b3ce5638d01f749a`. Independently loaded and verified all
44 retained build artifacts. Every retained Python/C source and test is
byte-identical to the current C1 source; fresh replication still uses its
own separately built snapshot and build identity.

The first campaign is
`/Users/liaozping/orca/natemu-raw/c1-run-20260905-track01-a`, run identity
`49f8cac2d57d4170b9b851cf50d6ce0a`. Its terminal manifest reports three
STRUCTURAL_PASS and six EXPECTED_CONTROL_FAILURE children, no failure and
complete hashing. The coordinator polled its execution handle 78020 and
confirmed exit 0 before this author's compilation/synthetic/offline work.
This is an inspected terminal artifact and process confirmation, not an
accepted physical-clock conclusion. No contradiction was found at preparation.

### Fixed replication plan and stopping conditions

Use fresh external paths, with no overwriting or replacement samples:

- Build: `/Users/liaozping/orca/natemu-raw/c1-build-20260905-track01-replica-a`.
- Campaign: `/Users/liaozping/orca/natemu-raw/c1-run-20260905-track01-replica-a`.

Run exactly three complete normal children, seeds 70701, 70702 and 70703,
then backward, zero-nsec, omit-clock, omit-contention, pthread-error and
global-inversion controls, each seed 70701. Independently compared this
schedule with the retained author plan and runner constant. Each normal
child retains all six clocks at both QoS levels, 200001 readings and 200
batches of 1000 calls per clock, both 10000-pair noise and 256-sextuple
containment phases, and both QoS strata of 1/2/4/8-worker mutex-serialized
contention with 200000 samples per worker. No warm-up exclusion, subset,
cross-child pooling, increased counts or outcome-driven replenishment.

Each native child has a 300-second group watchdog: 2700 seconds maximum
native occupancy. Native buffers retain the 256 MiB cap; complete arrays
are 226962048 bytes per child, below 512 MiB. The nine-complete-child
conservative payload is 2042658432 bytes, launch free-space requirement
4869586944 bytes and final retained-tree cap 5 GiB. Build/synthetic/checker
and environment time are outside the native subtotal, as already accepted;
this does not assert a 45-minute end-to-end bound or checker RSS cap.

The planned command uses the fresh build and retained runner/checkers:

```sh
NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B experiments/INV-01-07/c1/runner.py campaign /Users/liaozping/orca/natemu-raw/c1-run-20260905-track01-replica-a --build /Users/liaozping/orca/natemu-raw/c1-build-20260905-track01-replica-a --no-other-native-benchmarks
```

Do not execute until this preparation has independent reviewer clearance
and the coordinator confirms no overlapping native campaign. Retain
before/after host/OS/signing/QoS/load/power context and all outputs. The
first invalid, unresolved, timed-out or undetected-control outcome stops
the campaign and returns the specific contradiction to review. The first
author campaign is never rescued by a successful replica, and a failed
replica is never replenished. Host remains the local Mac16,10/arm64/25G83
cell unless factual capture says otherwise; altered context must be
reported, not pooled. No P1, timer/deadline, sleep, free-running cross-core
or KA-05 resolution follows from this clock-only replication.

### Preparation validation

The independent build-only command completed with exit 0 at the planned
replication build path. Fresh build identity `abf6d83045ca47e5821b7e542e34b32e`;
all 44 retained files passed `load_build` integrity inspection. The
retained Python/C inputs match the accepted first-build snapshot; the fresh
producing build identity is distinct. SDK 26.4, Apple clang 21.0.0
(clang-2100.0.123.102), C11/O2/debug/warnings/pthread flags and local ad-hoc
signature are retained in `build.json` and stage logs. The build ran its
complete retained synthetic suite successfully and never executed `clock_c1`.

Independently invoked the author's retained `runner.inspect_child` for all
nine completed children using each terminal process record. Each recomputed
result, including all raw-derived normal findings and each specific negative
classification, exactly matched the retained `check.json` after exact-fraction
serialization. The offline checking command completed exit 0. No fresh clock
measurement occurred. This checks reproducibility of the supplied raw packet;
it cannot replace fresh independent measurement.

**Preparation ready for independent examination.** Native replica remains
not run. Reviewer `/root/ordering_native_review` is asked to inspect this
plan, fresh retained build, no-overlap/stopping obligations and independent
checker rerun before permitting the planned bounded execution.

## Independent replication-preparation review — 2026-09-05

Reviewer: `/root/ordering_native_review`, separate from original C1 author
`/root` and replication author `/root/mach_native_review`; same native-method
reviewer as the full pre-execution thread. **Verdict: accept the fixed
replication preparation.** This permits the declared fresh campaign after the
coordinator confirms no overlapping native experiment. It does not accept
findings or substitute for successful independent measurement.

Independently inspected the preparation above and the two retained build
manifests. Recomputed all 44 listed artifact hashes in each build, verified
their distinct build identities, successful retained 73-test validation logs
and `native_executed:false`, and compared every C/Python source and checker/test
between snapshots byte-for-byte. Both preserve the reviewed common zero guard.
The replica destination is absent and fresh. The declared seeds, exact nine
children, unchanged fixed matrix, resource bounds, child-group watchdog,
no-overlap requirement and no-retry stopping rule match the accepted method.

Also independently recomputed every one of the 862 artifact hashes in the
first campaign inventory; its terminal record has three structural passes,
six named expected controls, no failure and complete hashing. The replication
author's retained-checker recomputation of all nine children is preparation
evidence, not replication. This reviewer has no contrary evidence concerning
that offline check, and will independently challenge/recompute the load-bearing
claims during findings review. No native experiment ran during this review.

## Independent replication executor report — 2026-09-05

Executor: `/root/mach_native_review`. After the independent preparation accept
above and coordinator confirmation of campaign exclusivity, executed exactly
the declared nine-child replica. Handle 99740 terminated exit 0.
[MAN-01-07-06](../manifests/MAN-01-07-06.md) records the fresh build/run
identities, terminal hashes, context and unpooled comparison with the first run.
All three normal children are STRUCTURAL_PASS; all six controls are their named
EXPECTED_CONTROL_FAILURE. No failed/omitted/replaced child or timeout occurred.
The terminal manifest's 862 artifact checksums were independently recomputed
by this executor.

Replica Mach wrapper p50 is 19/4 ns for user-interactive and 89/6 ns for
background in each seed. The first run's 70703 user-interactive p50 was
115/24 ns; other Mach p50 values agree. Each packet independently meets the
strict T1 screen; distribution identity is not claimed. Required monotonic
traces, serialized global/per-worker ordering, positive progress, containment
and batch-floor checks pass; no observed contradiction was identified.
This is an executor's provisional observed report, **not findings acceptance**.
The independent reviewer owns the verdict and downstream applicability.

## Independent findings and replication review — 2026-09-05

Reviewer/context: `/root/ordering_native_review`, separate from author `/root`
and replication executor `/root/mach_native_review`. Inspected the provisional
CLM-01-07-27/28 statements, MAN-01-07-05/-06 and executor report, both retained
producing builds and both complete campaign packets. **Verdict: accept
CLM-01-07-27 and CLM-01-07-28 at their stated local clock-only scope, with
independent same-host replication.** No substantive finding blocker remains
for these two claims. The full Q-01-07 investigation remains open.

Author run `49f8cac2d57d4170b9b851cf50d6ce0a` and independently prepared replica
`e7524e2701804d86a12fdb0d90889932` each contain the exact three normal children
and six expected negative outcomes. Independently recomputed all 862 campaign
artifact hashes in each, alongside the 44 artifact hashes in each distinct
producing build. Both retain successful 73-test build validations and the
accepted shared zero-return guard. The replica source/code and preparation were
reviewed before its authorized native launch; its terminal handle 99740 exited 0.
No excluded/replaced child or unreported terminal failure was identified.

The finding check went beyond rerunning the producer's checker. A reviewer-written
scratch audit imported no producer/checker helpers and independently decoded the
little-endian raw arrays for all six normal children. For every clock it checked
explicit IDs/cardinality, generic timespec/status/conversion, raw forward/zero/
backward counts and minimum positive step, both 10,000-pair noise phases, all
256 containment sextuples per phase with exact interval/slack/tightness tests,
and all 200 batches against the floor. It independently sorted exact rational
costs and selected indices 0/99/197/199, matching every emitted min/p50/p99/max.
For every 1/2/4/8-worker cell it recomputed acquisition/worker IDs and counts,
transitions, global and per-worker scans/progress directly from all timestamps.
All six complete raw audits passed; the audit process exited 0.

Independently checked each negative's raw mechanism in both campaigns: the
backward clock sample equals its predecessor minus one; omitted-clock and
omitted-contention arrays are exactly one designated row short with matching
byte sizes; the first second-worker sample in global-inversion is exactly one
tick below the preceding global sample; zero-nsec and pthread-error terminate
with the expected EINVAL/named failure and no complete normal record. The retained
accepted checker classifies them through their specific oracles, and the real
zero-return guard's synthetic mutation sensitivity was independently checked
before execution.

Observed agreement is bounded and specific:

| Campaign | Seeds | Mach p50, user-interactive (ns/read) | Background (ns/read) |
| --- | --- | --- | --- |
| Author | 70701 / 70702 / 70703 | 19/4 / 19/4 / 115/24 | 89/6 each |
| Independent replica | 70701 / 70702 / 70703 | 19/4 each | 89/6 each |

The exact Mach wrapper batch medians meet the strict <100 ns screen in both
QoS strata of every child; the slight seed-70703 difference is retained, not
pooled away. The observed minimum positive Mach step is one tick, with timebase
125/3 ns; every Mach batch exceeds the fixed 100-tick floor. Zero-quantized
readings remain present. All required monotonic, serialized acquisition and
per-worker traces have zero backward steps and positive progress; every required
multi-worker transition and non-vacuous containment condition is satisfied.

This supports local empirical clock-wrapper characterization and finite
mutex-serialized ordering on Mac16,10/M4, macOS 26.6.2/25G83, SDK 26.4,
clang 21 and ad-hoc signing. Source contracts plus these controlled observations
can inform a separately reviewed same-host clock instrument use. Neither label
nor artifact format automatically qualifies a downstream timing interval.
The sub-tick amortized batch medians do not measure an individually resolved
4.75 ns read; the fixed 100-tick batch screen is not a general accuracy or
error bound for every consumer. F08 handshake/sub-floor timing needs its own
measurement-floor interpretation; no historical timing is rehabilitated.

Alternative explanations and retained unknowns: wrapper overhead, loop/sink work,
scheduling, QoS effects and uncontrolled placement remain in these cost values;
no P/E-core attribution or isolated API latency follows. Serialized traces cannot
establish free-running cross-core skew or future monotonicity. The two wrappers
share the underlying time source, so containment checks conversion consistency,
not independently calibrated physical accuracy. One host/OS/signing context
supplies no P1/platform-wide claim. Timer/deadline/no-early delivery, sustained
SMP cadence, power/lifecycle behavior, guest policy, worst-case latency and KA-05
remain with their existing investigation owners. No architecture or performance
advantage is selected by this finding.

## T2 independent replication preparation — 2026-09-05

Replication author/executor context: `/root/mach_native_review`, separate from
T2 instrument author `/root/ordering_native_review`, first executor `/root`
and pre-execution reviewer `/root/mach_baseline_replication`. This context's
previous C1 replication is a different clock-only experiment; it authored no
T2 producer, bridge, checker or supervisor change. **Preparation is submitted
to `/root` for independent review; replica native execution and T2 findings
acceptance remain pending.** M06 control handle33263 is currently the sole
native campaign; this preparation performs compilation and synthetic/read-only
checks only.

Read the complete current T2 method, accepted T2-R1/R2 pre-execution return,
MAN-01-07-07 and producing runner. Independently rederived the first complete
campaign with its retained `runner.validate_campaign` and the required external
NATEMU_RAW_ROOT. All165 campaign and44 producing-build hashes verify; the exact
normal/dispatch-early/rtt-zero/cadence-duplicate order yields STRUCTURAL_PASS
and the three specified EXPECTED_CONTROL_FAILURE results. No source-byte
contradiction to the accepted current instrument was found. This offline
recomputation is preparation, not replication or findings acceptance.

Separately built and checked a compile-only packet from the accepted sources:

- Canonical build path:
  `/private/var/folders/7d/bdcp0n7x4tsdrt9d_5qyz09r0000gn/T/t2-independent-replication-prep-a_c6b1fk/producing-build`.
- Compile-only association: `3f4e3ac8acac4d44b6e5012643c557d1`;
  build manifest SHA256
  `91e1e73157ba47a363b19e7c035eb9b70a873f54fcc8a2ea8a1b658c7fd0cb7e`.
- All44 build artifacts verify. All eight snapshot source files equal the
  first campaign's producing snapshot and accepted workspace source bytes.
  Seven raw-checker tests, four dummy supervisor tests and the real compiled
  flag prepare/direct-handler-invoke/consume fixture pass. That fixture sends
  no signal and reads no timer. C/Swift compilation, ad-hoc signing, selected
  SDK declarations and disassembly are retained.

The first verification call used macOS's `/var` temporary-directory alias;
the strict canonical-path assertion rejected it. Resolving the same directory
to `/private/var` allowed all existing hashes to verify. The build had already
completed successfully; there was no rebuild, native run or altered evidence.
The actual external campaign path below is canonical and absent.

**Exact proposed replica.** One fresh campaign at
`/Users/liaozping/orca/natemu-raw/t2-run-20260905-track01-replica-a`, using the
same accepted C1 input
`/Users/liaozping/orca/natemu-raw/c1-run-20260905-track01-a/campaign.json`.
The outer runner will create its own fresh run identity, schedule and producing
snapshot/build at launch, with supervision starting before that build. This
compile-only packet will supply reviewed source inputs; it is not passed as
an already producing campaign build. No partial campaign directory or schedule
has been precreated.

```sh
NATEMU_T2_REPO=/Users/liaozping/orca/natemu \
NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw \
python3 -B /private/var/folders/7d/bdcp0n7x4tsdrt9d_5qyz09r0000gn/T/t2-independent-replication-prep-a_c6b1fk/producing-build/snapshot/runner.py \
  --directory /Users/liaozping/orca/natemu-raw/t2-run-20260905-track01-replica-a \
  --clock /Users/liaozping/orca/natemu-raw/c1-run-20260905-track01-a/campaign.json \
  --no-other-native-benchmarks
```

The fixed normal child has24 wakeup cells (four mechanisms ×100us/1ms/10ms
×UI/background), six cadence cells (2/4/8 workers ×UI/background) and six event
cells (condition/semaphore/spinning signal ×UI/background). Retain2000 rows
per wakeup/event cell and per cadence worker:116,000 normal rows, plus10,000
empty read pairs before and after. The three original full-cell controls follow
in their declared order; they require exact dispatch-early/rtt-positive/
cadence-deadline rejection. There are no seeds, warmup exclusions, reordered
cells, count reductions, additional children or outcome-driven replacements.

The unchanged outer2,700-second/128MiB polling bounds cover fresh build,
compiler cache, checks, metadata and all four children; child ceilings remain
1,800/300/60/60 seconds. SDK/host commands retain10-second and compile/check
commands60-second caps, with the accepted final-inventory/overshoot and non-RSS
qualifications. Any unexpected exit/oracle, incomplete sample matrix, timeout,
resource stop or instrumentation contradiction stops the replica and retains
partial checksums plus completed/interrupted/not-run modes. An adverse lateness
value is data. After successful preparation review, coordinator confirmation
that the native slot is free is still required immediately before launch.

The first campaign's shared-host compilation/synthetic overlap is preserved;
this replica will likewise record actual before/after context and no quiescent
or fixed-core-placement assumption. Clock input matching is factual context,
not timing acceptance. Compare distributions without pooling away the first
packet's28 adverse cadence medians. Empty-pair floors remain empirical analysis
policy, target-minus-sender remains an uncalibrated cross-thread diagnostic,
and no calibrated latency, KA-05, P1/lifecycle, sustained timely delivery or
full Q-01-07 conclusion follows from a structural pass. Submit the terminal
packet and comparison to an independent findings reviewer after the bounded
replica; this executor does not accept its own findings.

### Root independent T2 replication-preparation acceptance — 2026-09-05

Reviewer `/root`, separate from replica preparer/executor `/root/mach_native_review`.
Inspected its exact protocol, absent external destination, retained sourcebuild,
limits, snapshot launch command and qualification boundaries. Independently
verified all44build hashes and all eight source files byte-for-byte against
MAN-01-07-07's accepted producing snapshot. Inspected retained7checker,4dummy
supervisor and actual compiled flag-handler fixture pass logs; no new native
execution was performed by this preparation review. ManifestSHA256
`91e1e73157ba47a363b19e7c035eb9b70a873f54fcc8a2ea8a1b658c7fd0cb7e`
identifies the exact independently prepared packet above.

**Accept preparation for the unchanged complete four-mode T2 replica.** The
outer supervised launch will create its own fresh run/build association from
these exact sources, preserving all36normal cells/three negatives/counts and
2700-second/128MiB bounds. No stronger clock, threshold or findings acceptance
is implied. Root's latest native M06controls handle33263 is authoritatively
terminal exit1; no holds were launched, and no other native campaign is live.
The exclusive native slot is now released to this replica. Actual failure,
timeout or adverse lateness must remain retained; there is no replacement.

## T2 independent replication executor report — 2026-09-05

Executor `/root/mach_native_review`, following the independent preparation
acceptance above and root's explicit confirmation that M06 handle33263 was
terminal with no holds or other native campaign live. Executed the exact
prepared four-mode command, using the reviewed temporary snapshot runner and
a fresh supervised producing build. Replica handle3368 terminated exit0;
the coordinator was immediately notified and the native slot released.
No other native experiment was launched by this executor.

[MAN-01-07-08](../manifests/MAN-01-07-08.md) records association
`59b364f670c647e2887ccc8f9a012e00`, exact command, context, hashes and unpooled
comparison with MAN-01-07-07. Independently verified all165 campaign and44 build
artifact hashes, exact preparation/source identity, every invocation/process
association and the full retained `runner.validate_campaign` derivation.
The normal child is STRUCTURAL_PASS with all36 cells and116,000 rows; all three
controls return their specified EXPECTED_CONTROL_FAILURE. All native exits
are0, with no timeout, resource interruption, exclusion or replacement. The
outer elapsed time is1586.1041937500001seconds, and final retained directory
size is14,869,169bytes, within the unchanged2700-second/128MiB limits.

The replica reproduces the categorical diagnostic that every cadence worker
emits2000 events while all28 individual medians exceed the proposed250us error
comparison. It does not reproduce identical distributions: strict dispatch's
1ms/UI p99 changes from80.625us to505.208us, and all six replica event RTT medians
are2.25–3.00us, below the12500/3ns empirical floor. The first event medians were
4.5–6.67us. These differences are retained explicitly; no pooled estimate,
sub-tick C1 amortization or favorable count-only interpretation removes them.
A separate direct raw calculation reproduces normal counts, serial/pair
chronology and the reported per-worker median/gap/rate and sender RTT values.

The host remains M4/Mac16,10, macOS26.6.2/25G83, SDK26.4, ad-hoc signing,
requested UI/background QoS and unspecified physical placement. M06/M23 source
and short synthetic reviews overlapped on the shared host; no quiescence or
constant-load claim is made. The accepted C1 input supplies matching context,
not automatic timing qualification. All quantitative values remain unqualified
pending independent interval-specific findings review; below-floor event
medians in particular cannot be called calibrated latency. This executor
submits the complete replica and comparison and does not accept its own
findings, T2/T3 thresholds, KA-05, full Q07 or product/guest applicability.

## Independent full T2 findings and interval-applicability review — 2026-09-05

Reviewer/context: `/root/mach_baseline_replication`, the original independent
T2 pre-execution reviewer, separate from instrument author
`/root/ordering_native_review`, first executor `/root` and replica executor
`/root/mach_native_review`. I authored no T2 source, build or native result.
Read the research workflow, complete accepted T2 method and R1/R2 return,
MAN-01-07-07/08, producing code, manifests, environment/clock associations and
raw records. This review used read-only derivation and in-memory adversarial
mutations; it launched no native timer/event experiment and changed no instrument.

**Accept both packets as independently replicated, bounded structural and
converted-timestamp observations.** No unresolved blocker prevents accepting
that scope. **No calibrated latency, physical error bound, T2/T3 threshold
qualification, sustained timely SMP-delivery claim or KA-05 disposition is
accepted.** The interval-specific limits below are binding findings, not an
automatic clock license conferred by C1 or by a STRUCTURAL_PASS label.

### Identity, controls and independent derivation

Both retained `runner.validate_campaign()` calls pass with the required external
NATEMU_RAW_ROOT and original repository context. Independently recomputed all
**165 campaign and 44 producing-build artifact hashes per packet**, member
manifests, exact invocation/process/run associations, complete normal derivation
and the original control predicates. Associations are
`e6ee196e0e5540c09facb492a283708b` and
`59b364f670c647e2887ccc8f9a012e00`. All eight source files in the two producing
snapshots are byte-identical to each other and to the independently reviewed
R1/R2 build at `/private/tmp/t2-r1-r2-return-79annxn3/producing-build`.
The public Swift initializer/getter bridge and direct lock-free handler flag
therefore retain their accepted source/inspection context.

Actual outer and native exits are zero. The original four modes complete in
order, without timeout, resource stop, replacement, exclusion or added sample.
The fixed 2700-second/128 MiB outer and 1800/300/60/60-second child ceilings
are unchanged. These are four child modes, not 39 children. Normal contains
exactly 24 wakeup cells, six cadence cells and six event cells, in the original
source order: 48,000 wakeup rows, 56,000 per-worker cadence rows and 12,000 event
rows, totaling **116,000**. Every native child retains 10,000 pre- and 10,000
post-calibration pairs; every intended event/worker array has 2000 rows.

Direct raw challenges confirm exactly the declared negative predicates in
each packet: dispatch-early affects only row0's no-early predicate by at least
1000 represented ns; rtt-zero has exactly row0 end=begin; cadence-duplicate has
exactly worker0/row1's repeated represented deadline. All three reject with
`dispatch-early`, `rtt-positive` and `cadence-deadline` respectively. Those
intentionally modified timestamps are not performance samples. In-memory
mutations of the actual replica normal packet separately invert adjacent
wakeup, event and cadence chronology and a post-calibration pair; the retained
consumer rejects the first three with `row-chronology` and the fourth with
`calibration-chronology`. These recheck the original R1 defect against actual
data without modifying retained evidence.

The direct raw calculations agree with the retained checker and the replica
executor's unpooled comparison. All normal rows satisfy their declared integer
timestamp/deadline, requested-QoS, event-count and chronology predicates. This
is an observed instrument predicate, not proof of all future physical no-early
returns, placement, delivery bounds or free-running cross-core clock behavior.

### Replicated observations that must remain visible

Every one of the **28 cadence worker instances** emits 2000 events in each
packet, and every individual converted median is numerically above the proposed
250 us comparison. UI worker medians span 340.542–348.625 us initially and
340.876–349.875 us in the replica; background spans 11.899–20.750 ms initially
and 13.860–18.279 ms in the replica. The fixed common epoch and all 2000
per-worker deadlines verify; the instrument never resets the phase to now.
All retained missed-phase counts, catch-up bursts and maximum delivery gaps
remain part of this observation. Rates near 1000 events/s after catch-up do
not establish timely delivery or fairness, and neither pooled worker summaries
nor completed counts may suppress these adverse per-worker medians.

The 1 ms/UI single-wait converted medians are about 509–522 us for the three
non-strict mechanisms in both runs. Strict dispatch is 29.375 us initially and
26.833 us in the replica, while its p99 changes from 80.625 to 505.208 us.
Thus neither distribution identity nor a tail bound was replicated. The raw
arithmetic does not satisfy an all-four-mechanisms median-miss predicate;
that statement is not a qualified T2/KA-05 pass, and strict single-wait results
do not replace the separately adverse fixed-phase SMP observations.

All normal sender RTTs are positive under the retained direct-read conversion.
The first six event medians span 4.500–6.667 us; replica medians span 2.250–3.000 us.
All six replica medians are below the common **12500/3 ns** observational floor.
The change is not pooled away or interpreted as a causal speedup. In the order
condition UI/background, semaphore UI/background, spinning-signal UI/background,
the counts strictly below that floor out of each 2000 rows are:

| Packet | Condition UI / BG | Semaphore UI / BG | Signal UI / BG |
| --- | --- | --- | --- |
| First | 145 / 520 | 726 / 417 | 0 / 322 |
| Replica | 1870 / 1939 | 1956 / 1967 | 1978 / 1969 |

### Interval-specific qualification and alternatives

1. **Empty read pairs and floor.** All eight pre/post p99 values per campaign
   are one Mach tick. The fixed 100-tick term therefore sets each child floor
   at 12500/3 ns with timebase 125/3 ns. Zero differences remain abundant:
   the normal pre/post pairs have 6889/8185 zeros initially and 6880/8712 in
   the replica. This is compatible with quantized sampling, not a zero-cost
   clock read. The largest normal pre pair is **179 ticks** initially and
   **213 ticks** in the replica, both already above the 100-tick floor. The
   chosen p99-based policy does not even bound every observed empty pair,
   much less establish a universal error bound for another interval. No C1
   sub-tick amortized wrapper value supplies individually resolved T2 precision.
2. **Single waits/deadline residuals.** The actual represented deadline and
   retained wake timestamp support the stated arithmetic and source-level
   schedule comparison. Dispatch source construction/submission and scheduling
   are inside this experiment's lateness path; no setup estimate is subtracted.
   Whole-ns truncation and the 125/3 ns underlying tick remain explicit near
   zero. The empty-pair policy does not bound per-wait overhead, establish an
   independent containment relation, or certify callback-to-origin free-running
   clock comparability. These packets do not qualify the physical latency or
   threshold interpretation of even an above-floor quantile.
3. **Cadence.** Each worker's exact phase arithmetic, count, converted median,
   missed-boundary count and maximum gap are valid finite raw observations.
   The common epoch is generated on another thread; mutex-serialized C1
   observations do not prove every free-running relationship used here. The
   large numerical misses must remain visible, but no calibration/error theorem
   or causal attribution to an OS mechanism follows from their distance above
   the analysis floor. Requested QoS and uncontrolled scheduling remain part
   of the measurement context.
4. **Events.** Sender begin/end is the primary paired interval and is not
   halved. It includes the actual request/ack protocol and servicing work;
   semaphore return need not imply that the target was parked for every send.
   SIGUSR1 includes handler return and ordinary-thread flag polling, not a
   handler-entry timestamp. The below-floor replica medians cannot be called
   calibrated latency. Above-floor first medians do not independently qualify
   their overhead/error either. Target-minus-sender remains a signed cross-thread
   diagnostic. Its nonnegative values here cannot establish free-running
   synchronization or turn half-RTT into a one-way estimate.

Both packets concern Mac16,10/M4, macOS26.6.2/25G83, SDK26.4, local ad-hoc
signing, requested UI/background strata and unspecified placement. Native
campaigns were serialized, but source/compile and short synthetic work continued
on the shared host. Endpoint load/thermal captures and uninstrumented overlap
cannot establish constant load or identify the cause of changed RTTs/tails.
Fixed order, coalescing, servicing work, clock quantization and scheduling remain
alternatives. No uncontended-host, P/E-core or workload-general rate is inferred.

### Scoped consequence and remaining work

The structural experiment and independent replication are accepted at the
above scope, including the adverse cadence arithmetic and below-floor event
replica. **Every T2 quantitative performance/latency claim remains unqualified.**
A later load-bearing threshold return must specify and independently justify
the applicable per-interval clock-comparability, quantization, overhead and
containment/error treatment (or a reviewed alternative), preserving these
unpooled results. Reusing the C1 context match or an empty-pair floor alone
does not close that task. No new native campaign is authorized by this review.

KA-05, sustained timely per-vCPU SMP delivery, handler/product composition,
guest timer/APIC and time policy, host lifecycle, matched load, P1 and other
host/releases remain with their existing Q07/Q12/Q21/Q22/Q05-06 owners.
The bounded findings close neither the complete Q07 question nor Track 01.
