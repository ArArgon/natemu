# INV-01-07 — pre-execution review thread

- Reviewer/context: Codex `/root`, independent of runner author `/root/clock_mach`.
- Date: 2026-09-05.
- Scope: workflow migration of `experiments/INV-01-07/c1`; retained source builds,
  runtime provenance, factual clock references, compatibility and failure behavior.
  This review does not close the full C1 native-method review or accept timing evidence.
- Material inspected: runner/artifact/check/producer diffs, factual clock reader,
  focused runner and clock fixtures; existing numerical/control validation remains
  in its original scope.
- Verdict: accept for the migration scope; full native pre-execution review remains open in the investigation.

## Migration corrections

1. **Retain the executing runner/checker.** A later code edit could make a run use
   a checker absent from the producing snapshot. Remedy: execute the retained
   runner and dependencies, while preserving the original repository output
   exclusion. Inspection confirms the CLI now verifies the v2 build and executes
   `sources/runner.py`; original-checkout existence is no longer required.
2. **Retain complete clock evidence.** A campaign inventory containing only
   metadata could claim a factual context match after raw arrays were removed.
   Remedy: require all scheduled controls, their terminal outcomes and referenced
   arrays in the checked inventory. Closure: missing-array/control and mixed-child
   fixtures fail qualification. The author added those checks and fixtures.
3. **Reject incomplete producing builds.** The clock reader accepted a v2 build
   with an empty `files` object, as its positive fixture demonstrated. Such a
   reference lacks recoverable producing inputs. Require success flags and the
   binary/source/runtime dependency inventory; removal or historical versions
   must leave timing unqualified.
4. **Retain partial build failures.** Source copying occurred outside the failure
   handler and the C1 failure record omitted artifact checksums. Retain a versioned
   failure outcome and checksums for recoverable partial inputs and command output.
   Closure: interrupted/failed preparation preserves an inspectable failure record.
5. **Malformed clock evidence must leave timing unqualified.** Independent
   reproduction: write `[]` to an external clock JSON file and call
   `clock_evidence.validate(path, {"host_model": "synthetic"})`; this raised
   `AttributeError`, causing consuming correctness work to fail during timing
   qualification. Require object/domain validation and safe malformed-input
   handling. Closure: malformed clock shapes return unqualified without hiding
   a consuming experiment's independent correctness result.

## Returns

Same-reviewer return, 2026-09-05: all five migration corrections are closed.
Inspected the retained-runner exec path and source-context boundary; complete v2
build validation now requires the producing binary, runtime dependencies and
successful validation flags. The clock reader checks all nine scheduled outcomes,
80 retained arrays per completed child, expected process/control identity and
matching build/run references. Object/duplicate-key checks and malformed-input
handling return unqualified rather than interrupt correctness qualification.

Independently reran 18 focused supervisor/clock tests: all passed, including
editorial-state independence, wrong build bytes, missing arrays/controls, historical
formats, malformed clock inputs, interruption, timeout, resource and output guards.
Independently injected a source-copy failure after writing a partial source file:
the versioned failure record retained that file's checksum and hashing-complete
flag. Build source inspection confirms compilation uses `sources/producer.c`;
SDK versions/flags and selected declaration snapshots remain recoverable inputs.
The author also performed the full synthetic suite and build-only pipeline,
reported with the migration submission. No reviewer-authored code change was needed.
Existing C1 timed-path bias/allocation-accounting questions remain in the
investigation for full pre-execution review. No native campaign ran in this review.

Editorial disclosure: the reviewer removed an old review-closure label from the
README accounting heading and renamed its remaining code-gate reference to
pre-execution review; the accounting/control meaning is unchanged.


## Full M07-C1 pre-execution return — 2026-09-05

Reviewer/context: `/root/ordering_native_review`, independent of the producer,
checker and migrated runner authors. Applied the ponytail skill for a focused
correctness/method review. Inspected INV-01-07's complete M07-C1 method,
REV-219 R1–R3, current producer.c, runner.py, core.py, cells.py, artifact.py,
check.py, README accounting and independent fixture generators, plus the
migration return above. No native clock experiment or campaign control ran.

**Verdict: accept the current native M07-C1 method/code for its fixed campaign.**
No unresolved substantive pre-execution defect was found. This verdict concerns
clock-instrument preparation only: it accepts no observed clock finding, timer
result, KA-05 disposition, P1 transfer or downstream timing qualification.
Findings review and independently prepared replication remain required.

### Timed path and REV-219 R1

R1 is closed in the inspected implementation. `resolve_reader` selects a reader
once per cell before the readings or batches. Both QoS strata of the nsec API
then take the same normal reader path; its timed path has no injected-mode,
cell-ID or strcmp test. Mach and generic readers remain separate wrappers,
so their observed cost includes their own validation, serialization, indirect
call and volatile sink work. Results must retain this wrapper-level meaning.

The author's fresh zero-return correction is substantive and valid. Previously
reporting an error directly from reader selection would only exercise reporting.
Now `reader_zero_nsec` supplies literal zero to the same noinline `checked_nsec`
used by the real `clock_gettime_nsec_np` return. Independently compiled optimized
assembly shows both readers tail-branch to `_checked_nsec`; its `cbz x1` precedes
the store and returns through `_clock_failure` only on zero. The normal path
retains exactly one real nsec API call. This shared validation has uniform
normal-path overhead across the two nsec QoS cells.

Fresh assembly with the runner's C11/O2/debug/warnings/pthread/SDK flags also
shows one indirect reader call per raw iteration, and exactly 1,000 such calls
inside each of 200 separately bracketed batches. The volatile sink remains.
The contention worker has one real mutex lock, one Mach read while locked, and
one unlock per acquisition; the negative selection is outside its steady loop.
The 1 ms containment busy wait retains actual ordered reads and no clock cell
uses `CLOCK_MONOTONIC_RAW` for the same-clock containment comparison.

Source support was rechecked directly in the installed SDK's
`usr/share/man/man3/clock_gettime.3`, clock-ID descriptions: `CLOCK_UPTIME_RAW`
has the same value as appropriately converted Mach absolute time, whereas the
MONOTONIC_RAW description excludes comparison with unrelated clock sources.
`usr/include/mach/mach_time.h` provides the public absolute/timebase interfaces.
This is a local API-contract check, not an observed cross-core or sleep result.

### Allocation accounting and REV-219 R2/R3

R2/R3's material accounting defects are closed. The README now distinguishes
the independently enumerated 143,593-byte buffer/record subtotal from the
producer's 208,625-byte conservative formula and its separate 1,048,576-byte
reserve. The formula dominates that subtotal. Metadata is 1,310,880 bytes;
maximum native heap/global sample storage is 39,710,880 bytes and the declared
upper bound including the reserve is 40,759,456 bytes, below 256 MiB.

The author corrected the documentation/test phrase `every named local >= 8 bytes`
to enumerated major automatic buffers/records plus the explicit conservative
allowance. This closes the nonblocking wording issue identified during review:
smaller unlisted records/scalars (for example clock_cell's scan and
get_resolution's timespec) are covered by the allowance, not individually
itemized. The correction preserves the cap, its margin and scientific scope.
No scientific inference should rest on an exact total for every compiler stack
slot. The cap excludes pthread stack mappings, allocator/library overhead and
checker RSS, as the README explicitly states.

### Oracles, failure behavior and bounded scope

The independent Python arithmetic preserves unsigned domains, exact Fraction
conversion, nearest-rank p50/p99, retained zero pairs, strict batch floor and
strict T1 threshold. Every monotonic clock's backward reads fail; wall time is
retained as a separate diagnostic and cannot satisfy T1. All-flat traces and
all-wide containment remain unresolved. The containment minimum interval and
500 ns bracket/slack limits prevent a vacuous conversion check. Per-worker and
global acquisition order are checked independently, including a global-only
inversion with unchanged worker identities/counts. Full-child reporting remains
transactional and no failed late cell may retain a threshold prefix.

The fixed campaign is three complete normal children (70701–70703) plus six
separate named negatives (seed 70701). All nine have 300-second native watchdogs:
2,700 seconds maximum native-child occupancy, excluding environment capture,
build and checker work. Each complete child has 226,962,048 array bytes, so the
nine-child conservative array subtotal is 2,042,658,432 bytes before negative
early exits. Per-child payload and 5 GiB final campaign-inventory caps remain
explicit; disk checks precede launch. The runner captures timeout/exit and kills
and reaps the child process group on failure/interruption, stops without replacement
samples and retains partial records. All environment/build command calls have
finite timeouts. Native campaign exclusivity is still required.

The checker holds the fixed raw arrays and expands finite cell-local Python
values; its RSS is not represented by the native buffer cap. No claim that
1/2/4/8 serialized workers ran simultaneously on distinct cores is supported.
A missing inter-worker transition, absent forward progress, failed resolution
screen or watchdog timeout is a legitimate stopping result, not permission to
increase counts, retry for better data or narrow the prescribed matrix.

Runtime identities, source snapshots, exact schedule and recovered arrays remain
subject to the migration checks above. Retired review-receipt/clean-tree mechanics
from REV-219 R4/R5 are not scientific defects or new native gates under the
current research workflow.

### Independent validation

Independently rebuilt compiler assembly in external scratch storage and inspected
the reader, shared zero guard, batch loops and contention worker as described
above. Independently ran `python3 -B -m unittest test_core_independent
 test_producer_accounting` from c1: 24 tests passed, including exact conversion,
quantile/floor/containment/order challenges and the compiled sizeof-only probe.
The author reported a 72-test full synthetic pass; that report is supplementary,
not substituted for this reviewer's inspection of the changed reader. No native
clock data or experimental control was collected by this reviewer.


Same-reviewer final return: inspected the author's common-guard regression and
its producer mutation, narrowed accounting wording, shared-wrapper overhead
statement and explicit aggregate budget clarification. Independently reran the
two accounting/guard tests: both passed. The synthetic harness checks a positive
value, then dispatches the zero-mode reader without calling a host clock API;
the production guard exits 2 and writes its API-failure artifact, whereas removing
that guard returns 99 and omits the artifact. This demonstrates that the negative
seam detects a missing real guard rather than merely calling the reporter.
The full pre-execution accept verdict above is unchanged and has no unresolved
substantive blocker. The retained producing build must include these final
source/fixture changes before native campaign execution.

## Full M07-T2 pre-execution review — 2026-09-05

Reviewer: Codex `/root/mach_baseline_replication`, a distinct context from T2
author `/root/ordering_native_review`. The earlier C1 reviewer is now the T2
author and does not approve this return. Inspected the T2 method in INV-01-07,
README, complete producer/Swift bridge/checker/runner/tests, legacy R1 diff
and regression script, and author retained build
`/private/tmp/t2-author-final-xicomirw/producing-build` (all 39 artifact hashes
verified; retained inputs equal inspected current sources). No native timer
or event experiment ran in this review. No substantive instrument fix was
authored by the reviewer.

**Verdict: revise before native T2 execution.** Two concrete blockers follow.
The unresolved interval qualification is separately adjudicated below; closing
these code/oracle blockers does not supply calibrated latency or KA-05 evidence.

### T2-R1 — serial timestamp chronology is not checked

**Consequence.** Impossible same-thread observations can pass the complete
36-cell checker and contribute to latency/cadence/noise distributions. C1
does not justify overlooking a backward timestamp in this new producer.

**Reusable reproduction:** `python3 -B /private/tmp/t2-review-adversarial.py`.
The independent fixture constructs a full chronological baseline, which passes.
It then independently mutates the first cadence, wakeup or event row's end
past the next row's begin/end, preserving within-row inequalities. All three
currently return all 36 findings. A fourth mutation moves the first empty
calibration pair after the next pair; it also passes. The cadence mutation
can create a negative inter-delivery gap without invalidating the result.
These are synthetic checker defects, not observations of a backward host clock.

**Smallest remedy.** Require each sequential sender/worker row's beginning
to be at or after the preceding row's completion, and each sequential empty
pair's beginning to be at or after the preceding pair's end. Keep explicit
worker start/end containment, and do not impose sender/target calibrated
one-way ordering under this same-thread rule. Update the formerly repeated
synthetic timestamps to a chronological valid baseline and retain the four
destructive cases. Closure requires the valid fixture to pass, all impossible
chronologies to fail their intended predicate, and the original three native
negative controls to retain their intended exact rejection codes.

### T2-R2 — the handler reads a non-atomic static pointer

**Consequence.** The claimed lock-free-only signal handler still reads the
ordinary static `active_event` pointer. Checking `e.signal_seen` does not make
this separate pointer object an admissible signal-handler object under the
declared C11 contract. A native pass cannot validate undefined behavior.

**Evidence.** `producer.c` declares `static struct event *active_event`; the
handler reads it to select the target flag. The inspected optimized `_handler`
loads that global pointer and then stores the flag with STLR.
[WG14 N1570](https://www.open-std.org/jtc1/sc22/wg14/www/docs/n1570.pdf),
§7.14.1.1 paragraph 5, restricts handler references to static/thread-storage
objects to its stated lock-free-atomic/volatile-sig-atomic cases. This ordinary
pointer satisfies neither. Installing it before the requested signal does not
change its C type or supply a documented platform exception to that rule.

**Smallest remedy.** Eliminate the plain static access: for example, use a
lock-free atomic pointer with explicit publication/load and a lock-free check
before use, or directly own a lock-free static signal flag so the handler needs
no pointer lookup. Preserve object lifetime until the target joins and clear
the published reference only afterward. Closure requires source/optimized
inspection that every handler-accessed static object is covered, plus a small
synthetic/build regression for the intended publication/flag path; no native
signal trial is needed before re-review.

### Source and mechanism assessment

The public Swift `DispatchTime(uptimeNanoseconds:)`, `uptimeNanoseconds` and
`rawValue` declarations are present in retained SDK26.4 Dispatch.swiftinterface.
Inspected bridge disassembly invokes those public operations on the same value;
the producer passes that returned token to `dispatch_source_set_timer` and
retains its represented deadline separately. It does not treat opaque C bits
as nanoseconds. The local SDK `dispatch/source.h` timer contract identifies
uptime-based scheduling, the one-shot FOREVER interval and strict's best-effort
leeway behavior; `dispatch/time.h` retains the opaque C type. The initializer's
epoch rationale remains tied to its cited Apple public contract, not token
decoding. The exact request and represented value are appropriate raw inputs
for the intended empirical no-early check, with quantization limits retained.

The 24 wakeup, six cadence and six event cells preserve the stated mechanisms,
target intervals and UI/background dimensions. There are 116,000 normal rows
and three fixed full-cell negative children. Ceil Mach deadlines and the common
future cadence epoch preserve phase rather than resetting deadlines after
late wakes. Per-worker gaps, late-phase counts and achieved-rate denominators
are retained; a recovered average rate is not timely sustained delivery.
Dispatch cancellation completes before captured stack storage is released.
Condition/semaphore/signal request/ack paths retain sender RTTs rather than
half-RTT; the new signal sample denotes target observation after handler return.
Actual requested/effective QoS records do not establish physical core placement.

Legacy R1 adds strict dispatch to I6 and positive ordered integer RTT domains
to I10b. Independently reran the valid aggregate plus five isolated regressions;
each expected rejection suppresses threshold output. Historical missing timer
records remain unrepaired and inadmissible.

### Clock applicability disposition

C1's retained host/timebase context and empirical clock-wrapper findings are
relevant prerequisites. They do not establish a deterministic per-event error
bound for T2's direct Mach reads or calibrate target-minus-sender observations.
The pre/post empty-pair p99 floor is an observational analysis policy; it cannot
be treated as a bound that all other intervals satisfy. The code correctly
keeps zero pairs, rejects within-pair backward time and labels timing pending;
T2-R1 extends that integrity check to serial chronology.

After both blockers close, **structural/correctness collection of this full
fixed T2 stage can be justified**, retaining distributions as unqualified
diagnostics until independent interval-specific findings review. No additional
per-wait containment requirement is imposed merely to collect those diagnostics.
Their later use for T2/T3 or per-worker median comparisons requires explicit
scope: compare the measured intervals/residuals against the actual local floor,
report near-floor/quantized values without claimed calibrated precision, and
retain the free-running cross-thread caveat. If the author seeks stronger
calibrated or error-bounded threshold claims, that is a substantive method
return requiring suitable independent instrument evidence, not automatic C1
transfer. No T1–T4 or KA-05 approval follows from a structural checker pass.

### Retention, budgets and validation scope

The retained-source runner associates source/build/raw/checks with one run,
records exact normal/three-control schedule, and executes its retained checker.
The outer supervisor covers build, metadata, native children and derivation;
2,700 s/128 MiB polling thresholds and 1,800/300/60/60 s native ceilings are
explicit. The process-group kill/reap path preserves partial evidence and
completed/interrupted/not-run modes. Polling overshoot, final inventory costs
and non-RSS memory limits are disclosed; these limits cannot prove latency.
No broader stress, placement, host-lifecycle or guest-time scope is accepted.

Independently reran six raw-checker tests and four real dummy supervisor tests,
including descendant heartbeat cessation and output cutoff; all passed. These
tests missed T2-R1's new counterexamples. Inspected the author retained build,
optimized handler/bridge and exact source hashes. Independent compile-only
retained build `/private/tmp/t2-independent-review-41omod1p/producing-build`
also completed successfully; all 39 artifact hashes verify. All review
executions to this point are compilation or synthetic/dummy validation only.

### T2-R1/R2 same-reviewer correction return — accepted, 2026-09-05

Reviewer `/root/mach_baseline_replication` reinspected the author changes,
affected signal lifetime and timestamp assumptions, revised fixtures and fresh
retained build `/private/tmp/t2-r1-r2-return-79annxn3/producing-build`.
All 44 retained artifact hashes verify; its source inputs equal the inspected
current files. Build manifest SHA-256:
`450b2cb02b13df1a18bff2a16d265d7977c89af5fa87b312fa756fd58504fdd0`.

**T2-R1 closed.** The shared raw-row reader now requires each preceding
sender/worker end to precede or equal the next beginning. Both calibration
phases impose the corresponding adjacent-pair constraint. Independently reran
the original `/private/tmp/t2-review-adversarial.py`: the chronological normal
baseline passes; cadence, wakeup and event reversals reject as `row-chronology`,
and the empty-pair inversion rejects as `calibration-chronology`. All seven
author checker tests also pass, including both calibration phases and the
three original native negatives' unchanged exact rejection predicates.
The change does not impose calibrated cross-thread target/sender ordering.

**T2-R2 closed.** `active_event` is removed. The handler accesses only one
directly owned static `atomic_int signal_seen`, whose lock-free property is
checked before `sigaction` installation. Each sequential event arm resets it
atomically before creating its target; the previous target has joined before
the next arm. Static storage avoids the former pointer/object-lifetime path.
The target consumes the flag through an acquire exchange and timestamps in
ordinary thread context. Reinspected optimized `_handler`: address formation,
constant one, STLR and return; no plain pointer load or function call remains.
Independently compiled and executed the real production prepare/handler/consume
fixture, verifying initial clear, publication, consumption/reset and coalescing.
It invokes the handler as an ordinary function and performs no signal delivery,
clock reading or timer experiment. All four dummy supervisor tests also pass.

**Updated verdict: accept pre-execution preparation for exactly the complete
four-mode T2 campaign**: one normal child with 36 cells and three specified
negative children, under unchanged 2,700 s/128 MiB outer and
1,800/300/60/60 s native limits. No unresolved substantive blocker remains for
this declared structural/correctness collection. Native exclusivity must still
be established by the coordinator; this review launches no experiment.

The clock-applicability disposition above is unchanged. Retained timer/cadence/
RTT distributions remain empirical diagnostics with interval-specific timing
qualification pending independent findings review. This pre-execution verdict
does not accept calibrated latency, threshold precision, physical placement,
sustained timely delivery, KA-05, the full investigation or a host-wide bound.
Any future stronger timing claim needs its own supporting evidence and review.
No reviewer-authored substantive change or prospective findings acceptance is
part of this return.
