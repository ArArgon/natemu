# INV-01-18 findings review thread

## Four-cell replica preparation check — root, 2026-09-05

Independently verified all25 artifacts and all12 source/input bytes in
`m18-build-20260905-track01-replica-b` against author build `...-c`, including
the complete matching code packet. The separately prepared replica below
uses the exact accepted suffix and unchanged native producer/counts/budgets.
Preparation ACCEPT. Native execution clearance is **still pending** until
the coordinator's currently running T2 campaign terminates; elapsed time does
not supply that clearance. No M18 replica has executed at this checkpoint.

## First seven-child preflight: preliminary return — 2026-09-05

Reviewer: Codex `/root/publication_native_review`, independent of executor
`/root` and the native/runner authors. This reviewer supplied the preceding
pre-execution review, authored no native or supervisor change, and ran no
native replication. Material inspected: [MAN-01-18-01](../manifests/MAN-01-18-01.md),
the complete retained run/build identity, source snapshot, environment,
schedule/results/budget and first three child artifacts at
`/Users/liaozping/orca/natemu-raw/INV-01-18/20260905T213215.036860Z-preflight`.
The retained producing build is `m18-build-20260905-track01-a` beneath that raw
root. The reviewer independently ran that snapshot's `verified_campaign()`
and reran its checker over both complete positive traces. All producing
artifact checksums, identities and frozen-stage association passed.

**Verdict: provisional partial-run inventory is supported; return for failure
instrument correction and independent replication before accepting a
load-bearing protocol or availability finding.** This record accepts no
family kill, architecture decision, full preflight completion or Q01-18
resolution. Missing replication remains substantive work, not adverse host
evidence.

### What the retained evidence supports

- **Observed, execution inventory:** on Mac16,10/M4, macOS 26.6.2/25G83,
  SDK 26.4, local A ad-hoc signing, one reader, normal pressure, 256-byte
  blocks and rate one, E and S exited zero and their complete traces passed
  the specified preflight checker. E recorded 674 calls, including 565 after
  warmup; S recorded 335, including 256 after warmup. Each observed 38
  generations and 36 checked reuse transitions. These are finite instrument
  observations, with the retirement-oracle limits retained from pre-execution
  review, not an independently replicated safety guarantee.
- **Observed, negative process outcome:** the third child, M-jit-protect,
  exited 78 without timeout or signal and wrote
  `INV18 UNAVAILABLE mprotect roundtrip errno=13` to retained stderr. Its raw
  trace has no `mapping` or `unavailable` record and ends among initial
  `code-word` records. Its checker therefore reports INCOMPLETE. The only
  admissible API statement is that this instrument's attempted round trip
  reported errno 13 at an unspecified constituent call in this exact cell.
  It does not establish which requested protection transition was denied or
  the mechanism/restriction responsible.
- **Observed, containment/accounting:** all four subsequent scheduled jobs
  stayed not-run; no sample replacement occurred. Retained supervision shows
  about 0.48 seconds of stage operation and 7,674,220 payload bytes before
  terminal inventory, within the reviewed operational limits. These values
  are not qualified protocol latency or cost observations. The absent clock
  packet is correctly recorded as unqualified timing.

### R18-F1 — the terminal path loses the failing syscall identity

**Consequence.** The retained failure cannot discriminate PROT_NONE denial
from restoration denial or support a reproducible per-call capability
conclusion. Nor does it retain all return/errno/extent details required by
M18-2. This is an instrument evidence defect, not evidence that the complete
protocol fails on the platform.

**Evidence and explanation.** In the retained `mapping_change()`, `a` is the
PROT_NONE call and `b` is restoration only when `a` succeeds. Its `mapping`
record is queued to an asynchronous ring. `unavailable()` queues a second
record, prints only a combined label/selected errno, then `_exit(78)` before
collector drainage. Independent inspection confirms neither queued record
exists in this third trace. The ambiguous stderr is preserved honestly;
missing events must not be reconstructed as observations from source logic.

**Smallest remedy.** Before terminal exit, synchronously retain a bounded
structured failure witness with call identity, exact owned address/length,
requested protection, whether restoration was attempted, each actual return
and captured errno, plus the existing run/build association. Use a separate
failure record or dedicated stderr channel rather than concurrent writes
into the collector's actor stream. Preserve the partial stdout and errno
separately. The consumer must validate that witness and classify the outcome
as a failed/unsupported candidate operation, never complete correctness.
Do not wait on stopped readers or invent a graceful protocol recovery merely
to flush an error record.

**Closure fixtures and next step.** Inject protection-call failure and
restoration-call failure in synthetic/build preparation, verify the two
retained witnesses differ and retain exact attempted/skipped flags, and verify
a missing/malformed witness remains unresolved. This reviewer's pre-execution
return must inspect the instrument change before another native child. Then
preregister one bounded diagnostic/replication step aimed at this distinction,
preserving the original negative campaign and all not-run jobs. Independent
replication requires separately prepared/reviewed replication work; rerunning
the same executor's campaign alone does not discharge that requirement.

### Decision consequence and alternatives retained

The E/S outputs justify continued controlled investigation of those precise
cells, after the remaining required controls and replication. The M-jit-protect
outcome justifies correcting failure observability and investigating this
specific public-API composition. It cannot kill data protection/remapping,
ordinary code publication, E/S/V/I alternatives, another signing cell, SMP,
another host/macOS release or Track 01 as a whole. Clock qualification, full
hooks/negatives/protection, stress, pressure, 2/4/8-reader coverage, other hosts,
signing/distribution and Q19 write-fault composition remain open. No claim of
generic mprotect/JIT restoration availability is admitted from this result.

## R18-F1 diagnostic findings review — 2026-09-05

Independent reviewer `/root/publication_native_review` inspected
[MAN-01-18-02](../manifests/MAN-01-18-02.md) and its actual retained stderr,
trace header, jobs, checks, terminal status and seven outcomes. Recomputed all
31 campaign and 25 producing-build hashes and reran the retained E/S checker
and exact failure-witness consumer offline. No native experiment was launched
by this reviewer. Run `32b2244da8e0434c8a00474dfe4bc982` and build
`2cf232f7872549ceb912610516b540b8` match their retained producer header.

The new instrument resolves the historical ambiguity **for this new observed
attempt only**: `mprotect` of the owned 16,777,216-byte MAP_JIT arena at
4388569088 to PROT_NONE returned -1 with errno 13 (EACCES). Restoration to
protection 7 was not attempted, and its result/errno are null. This is durable
synchronous caller evidence from the exact inspected call site. The earlier
MAN-01-18-01 remains ambiguous and is not recast as observing this same cause.
There is no remaining R18-F1 instrument blocker for interpreting the new
call-level record.

E and S retain complete preflight invariants: E has 1,161 total/863 post-warmup
calls and S 425/341, with 38 generations and 36 reuses each. The third child
exits 78/unavailable; the remaining four are not run. None of these counts
completes a protocol family's control, hold, pressure, topology or cost scope.
The unchanged bounded campaign stopped as specified, with no timing claim or
replacement of failed observations.

**The bounded diagnostic observation is supported; independent replication
is still required before promoting it into a load-bearing availability or
family disposition.** The next independent executor must read the exact
retained source/build/context, prepare the same instrumented seven-job
preflight under its existing budgets, have that preparation reviewed by a
distinct context and preserve the first unavailable result and all not-run
rows. A repeat should compare exact syscall, extent, protection, errno,
signing/API/host context and skipped/attempted restoration, not merely match
exit 78. No automatic retries or source edits are part of that replication.
Even matching EACCES observations would establish only this local owned
MAP_JIT PROT_NONE attempt; they would not kill data-map/data-protect, immutable
publication, MAP_JIT generally or the full Track 01 design. Timing, broader
signing/host applicability and all remaining families remain open.

## Independent diagnostic replica preparation — 2026-09-05

Executor: Codex `/root/mach_baseline_replication`, separate from M18 authors,
first executor `/root`, and reviewer `/root/publication_native_review`. This
executor authored none of the M18 instrument, runner or preceding reviews.
Read the research workflow, INV-01-18 including R18-F1, experiment README,
pre-execution R18-F1 acceptance, this findings thread and MAN-01-18-01/02.
Inspected the retained build/runner and exact native synchronous reporter,
two-syscall call site, producer-identity gate and failure-witness consumer.

Fresh build-only snapshot:
`/Users/liaozping/orca/natemu-raw/m18-build-20260905-track01-replica-a`,
build ID `6b6703d4e5b54cd593e3f4355032d034`. It was built using:

```sh
INV18_ORIGINAL_REPO=/Users/liaozping/orca/natemu NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B /Users/liaozping/orca/natemu-raw/m18-build-20260905-track01-b/snapshot/build.py --output /Users/liaozping/orca/natemu-raw/m18-build-20260905-track01-replica-a
```

`INV18_ORIGINAL_REPO` points the existing build helper at its required local
clock dependency; that file was independently verified byte-identical to the
retained MAN-01-18-02 dependency before building. All 12 resulting retained
source/input entries and the complete source packet match MAN-01-18-02
exactly; no instrument change or current-tree amendment was introduced.
All 25 build artifact hashes verify. Fresh build manifest SHA-256:
`315a6308226edc656d494e92ea1e2088b44a119bce8084090b68f4262d6ef814`;
plain A binary SHA-256:
`a949dedb4109239294ff355701910e551391cc9fa5f8597ffcf413babca8cc70`.
Compile/sign/disassembly succeeded under SDK26.4/Apple clang21.0.0;
build ID differs intentionally and is embedded in the new binary.

The retained complete synthetic suite passed: 34 trace checks, 22 protection
checks, six clock cases, three negative classifications, source/identity and
budget fixtures, plus R18-F1 fabricated reporter and real dummy-child consumer
tests. Those fixtures do not execute the publication/protection protocol.
Independently verified MAN-01-18-02's 25 build and 31 campaign artifact hashes,
ran `verified_campaign()` against the exact source packet, reran the E/S raw
checkers, and decoded its durable witness. The results match the preceding
review: E 1,161 total/863 post-warmup calls; S425/341; each38 generations and
36 reuses; M-jit-protect's 16MiB PROT_NONE call returned -1/EACCES13 with
restoration to protection7 explicitly unattempted/null.

Proposed exact native command, **not yet executed** and awaiting root's
separate preparation acceptance and exclusive native-slot clearance:

```sh
INV18_ORIGINAL_REPO=/Users/liaozping/orca/natemu NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B /Users/liaozping/orca/natemu-raw/m18-build-20260905-track01-replica-a/snapshot/run.py run --mode preflight --binary-dir /Users/liaozping/orca/natemu-raw/m18-build-20260905-track01-replica-a
```

The runner creates one fresh timestamped campaign under raw `INV-01-18/`.
No timer packet or qualification packet is provided. The identical frozen
seed18001 schedule is E,S,M-jit-protect,M-data-protect,M-data-map,V,I, with
one reader/producer, rate1, signingA, 256-byte blocks, normal pressure and
the unchanged four-warmup/32-publication/128-call floors. Bounds remain
900s stage loop,2GiB aggregate output threshold,1GiB free reserve,
30s each child and128MiB each native output file. Stop on the first
incorrect/unavailable/invalid correctness outcome, preserve its partial evidence and
all remaining not-run rows, and perform no replacement or repeat.

After execution, compare exact syscall, owned extent, requested protections,
actual result/errno, attempted/skipped restoration and signing/API/host
context. Matching exit78 alone is insufficient. Finite E/S counts may differ;
only their original preflight invariants are replication targets. Same-host
results cannot widen applicability or dispose of data-map/data-protect,
immutable publication, MAP_JIT generally, other signing/host cells, full SMP,
remaining controls or costs. This preparation grants no findings acceptance.

### Independent replica preparation acceptance — 2026-09-05

Reviewer `/root`, distinct from replica executor. Independently compared all12
retained source/input bytes and complete code-packet metadata to MAN-01-18-02,
verified all25 new producing-build hashes, and inspected the fresh compile,
signature commands, source association and unchanged retained runner budgets.
The existing R18-F1 pre-execution review applies to identical instrument code.
The proposed command uses the new snapshot and new binary identity, with fresh
timestamped output and no clock packet. Preparation ACCEPT for exactly this
seven-job campaign, stopping on the first unavailable/invalid correctness
outcome with no replacement. Timing remains unqualified even in completed
correctness-qualified children; it is not itself a correctness stop. Corrected
the preceding preparation wording to make that distinction explicit.

Exclusive native slot is clear: all other actual campaigns are terminal and
other active agents are preparing code. Clearance covers only this one
replication, not later cells or prospective findings.

## Independent native diagnostic replica result — 2026-09-05

Executor `/root/mach_baseline_replication` ran the exact cleared command once,
with no native overlap or instrument modification. It terminated exit 1 at
`/Users/liaozping/orca/natemu-raw/INV-01-18/20260905T220724.577605Z-preflight`,
run ID `9845fb07cd5b42879a36745e21fca42d`, producing build
`6b6703d4e5b54cd593e3f4355032d034`. The native slot is released; there is
no live replica process. [MAN-01-18-03](../manifests/MAN-01-18-03.md) retains
the exact invocation, evidence identities, outcomes and comparison.

E/S exited 0 and passed fresh raw-checker invocations: E 552 total/450
post-warmup calls; S 372/280; each 38 generations and 36 reuse transitions.
M-jit-protect exited 78/unavailable, with an INCOMPLETE asynchronous trace.
The durable synchronous witness identifies the owned 16 MiB arena at 4381638656,
PROT_NONE result -1/errno 13, intended restore protection 7, and explicitly
unattempted restoration with null result/errno. Every witness field except
the allocated address matches MAN-01-18-02. This is an exact call-level
comparison, not merely matching terminal exit codes.

All four later jobs remain not-run, with no replacement/retry. No timeout,
signal or resource cutoff occurred. The source packet, all 25 producing-build
hashes, all 31 campaign hashes and `verified_campaign()` identity/schedule
association verify. Same M4/Mac16,10, 26.6.2/25G83/SDK 26.4, plain signing A,
one-reader/producer context is retained; timing remains unqualified.

Submitted executor conclusion: the local precise failure and finite E/S
preflight invariants reproduced. No generic MAP_JIT, restoration, full protocol,
second-host, signing/SMP or family disposition follows. MAN-01-18-01 remains
ambiguous; all omitted controls and unrun rows remain owed. This executor
makes no findings acceptance; `/root/publication_native_review` remains the
separate reviewer for this return.


## Independent diagnostic and replica findings acceptance — 2026-09-05

Reviewer: Codex `/root/mach_native_review`, independent of the M18 instrument,
runner amendment, original executor `/root` and replica executor
`/root/mach_baseline_replication`. Reviewer change is explicit: the original
reviewer `/root/publication_native_review` could not be resumed because repeated
collaboration calls returned `agent thread limit reached`. This reviewer read
the full current pre-execution and findings threads, including R18-F1, its
correction and original oracle limits, before inspecting the retained packets.
No native experiment or load-bearing code edit was performed by this reviewer.

**Verdict: accept the bounded call-level diagnostic and same-host independent
replication in MAN-01-18-02/03, and their finite E/S preflight observations.**
No unresolved substantive defect was found in those exact submitted claims.
This does not accept a complete preflight, full protocol safety, a family
disposition, cost, platform-wide availability or completion of Q01-18.

Independently ran the producing snapshot's `verified_campaign()` on both
packets, verified all 25 build and 31 campaign artifacts in each, and compared
the complete retained source packets. Both use packet
`0cc21c80199920a8e05b842801b0f27450bf7d30398d6cd483ae814eb217eca3`; the new
build identities and embedded raw producer headers correctly differ. Job,
invocation, signature, build/run and frozen seven-entry schedule associations
match. The four successful E/S raw traces pass fresh retained-checker calls
with results exactly equal after JSON normalization to their stored summaries.
A separate direct raw pass verified contiguous actor sequences, actual call
counts, called generations, reuse counts and every returned pair against its
retained built descriptor. This actor-local audit creates no global ordering
claim.

| Packet | E total / post-warmup calls | S total / post-warmup calls | Generations / reuses per child |
| --- | ---: | ---: | ---: |
| MAN-01-18-02 | 1,161 / 863 | 425 / 341 | 38 / 36 |
| MAN-01-18-03 | 552 / 450 | 372 / 280 | 38 / 36 |

The different call counts are compatible with the specified finite call floors;
distribution identity is not a replication target. E/S actual generated-call
results and reuse traces support only their admitted preflight invariants.
The previously accepted retirement limitation remains load-bearing: actor-local
trace order and admission life/page-floor checks cannot prove the absence of
a cross-actor retirement race. The abstract model, actual held-reader controls,
negative cases and larger topology work remain required before broader lifetime
or protocol conclusions.

Independently inspected `mapping_change()` and the synchronous
`mprotect_failure()` reporter, then decoded both actual stderr witnesses using
the retained consumer and compared them directly. At the owned arena addresses
4,388,569,088 and 4,381,638,656 respectively, each first call requested
PROT_NONE over 16,777,216 bytes and returned -1 / errno 13 (EACCES). The intended
restoration protection is 7; `restore_attempted` is false and its result/errno
are null in both. All witness fields except the allocation address agree.
Actual return codes are 78, with no signal, timeout or resource cutoff. The
partial asynchronous traces remain INCOMPLETE; they are not complete correctness
evidence. No restoration attempt is inferred from the source branch or a label.
MAN-01-18-01's lost call identity remains ambiguous and is not reconstructed
from these later observations.

Both campaigns stopped at original index 2, retained indices 3–6 as not-run
and supplied no replacement or retry. Same Mac16,10/M4, macOS26.6.2/25G83,
SDK26.4/clang21, 16 KiB pages, local plain ad-hoc A signing, one reader/producer,
normal pressure and uncontrolled placement apply. No clock packet was supplied;
timing remains unqualified. Inherited launcher restrictions were not independently
measured, so neither the reporter nor matching EACCES observations identify a
general macOS policy mechanism. No other host, signing/distribution, guest,
2/4/8-reader, MAP_JIT-general or data-map/data-protect conclusion follows.

The accepted consequence is to retain this precise local failed candidate
operation and continue review of the separate, originally unrun cells. It
does not kill immutable publication, data mapping/protection, an architecture
family or Track 01. Full controls, hooks, pressure, SMP, cost and Q19 composition
remain open.

## Independent four-cell continuation replica preparation — 2026-09-05

Executor `/root/mach_baseline_replication`, separate from continuation author/
executor `/root` and final reviewer `/root/mach_native_review`. Read the
accepted original-four-job suffix amendment and its pre-execution review,
current investigation method and accepted diagnostic findings. Inspected the
retained continuation runner's exact selection/order, budget and full-stage
qualification guards. No instrument or source edit was made by this executor.

Fresh compile-only build:
`/Users/liaozping/orca/natemu-raw/m18-build-20260905-track01-replica-b`,
build ID `ff721207674c4c42a9831f3dfe528931`. It was built from the reviewed
`m18-build-20260905-track01-c/snapshot/build.py`, with
`INV18_ORIGINAL_REPO=/Users/liaozping/orca/natemu` and the external raw root.
The required local clock dependency matched retained bytes before building.
All 12 retained source/input entries and code packet match the producing
author build exactly:
`4a357ea54dc5bd64b3fd4f9646d9e31481e52fda628dd490d8d15dad0aab0069`.
All 25 build hashes verify. Build manifest SHA-256:
`453705ffe6a4645e363a0e0479b9c62bb16522824ec68e0239799b17e1da3984`;
plain A binary SHA-256:
`247728f3ae02b625978cf538d4d4113fa20c70149acce93044fe270ddd92af0d`.
Compile/sign/disassembly succeeded under SDK 26.4/Apple clang 21.0.0.
The full retained synthetic suite passed, including 34 trace checks,
22 protection checks, six clock cases, three negative classifications,
resource/identity/failure-reporting fixtures and exact-suffix qualification
challenges. These fixtures execute no native publication/protection protocol.

Independently verified the author's terminal run
`0af49bf0232245e5ac6e87fe672ed9c6` at raw
`INV-01-18/20260905T222306.905615Z-preflight-remaining`: all 39 artifact hashes,
`verified_campaign()` source/build/identity association and exact ordered suffix
of the original seed18001 seven-job schedule pass. All four native exits are
zero, without timeout/resource failure. Fresh raw-checker calls yield:

| Arm | Total / post-warmup calls | Observed generations / reuses |
| --- | --- | --- |
| M-data-protect | 148 / 128 | 34 / 36 |
| M-data-map | 148 / 129 | 33 / 36 |
| V | 511 / 426 | 38 / 36 |
| I | 528 / 423 | 38 / 36 |

This is independent inspection of the first run, not native replication.
Observed generation counts refer to called generations under the admitted
schedule and do not replace the complete publication/reuse oracles.

Proposed exact native replica command, **not executed during preparation**:

```sh
INV18_ORIGINAL_REPO=/Users/liaozping/orca/natemu NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B /Users/liaozping/orca/natemu-raw/m18-build-20260905-track01-replica-b/snapshot/run.py run --mode preflight-remaining --binary-dir /Users/liaozping/orca/natemu-raw/m18-build-20260905-track01-replica-b
```

The runner creates a fresh timestamped directory under raw `INV-01-18/`.
Exactly four fresh children run in original suffix order: M-data-protect,
M-data-map, V, I. Complete job records are unchanged: seed18001, one
reader/producer, rate1, signingA, 256-byte blocks, normal pressure and original
four-warmup/32-publication/128-call floors. No clock packet is provided.
Keep 30 s each child, 900 s stage, 128 MiB each stdout/stderr file, 2 GiB
aggregate threshold and 1 GiB free reserve; stop at first unavailable/invalid
or resource outcome, retain partial evidence and later not-run rows, and
perform no replacement. Combined author/replica ceilings remain 240 native
seconds, 1,800 stage seconds and 4 GiB operational output thresholds.

Root must independently accept this preparation and release the exclusive
native slot after its current T2 campaign terminates. That campaign is still
under root supervision; this executor will not overlap it. Matching results
would support only the four finite preflight cells on the same local host,
with existing retirement-oracle limitations. Neither suffix success nor its
combination with earlier E/S observations erases the unavailable MAP_JIT arm,
qualifies a full all-positive preflight, resolves required controls/SMP/host/
signing/pressure/cost, or completes Q01-18. Findings acceptance belongs to the
separate reviewer; MAN-01-18-05 will be written only after actual execution.

## Four-cell continuation independent executor return — 2026-09-05

Executor `/root/mach_baseline_replication`. Root confirmed independent preparation
acceptance and explicitly released the native slot after T2's actual terminal
exit 0. The exact retained command above ran once, exclusively, and returned
terminal exit 0 at raw `INV-01-18/20260905T225432.661157Z-preflight-remaining`,
run ID `41f1c47c87ff44e294c2b8f1b56eef9b`. The slot was released immediately
after terminal notification; no further native run was made. This is an executor
report, not findings acceptance. [MAN-01-18-05](../manifests/MAN-01-18-05.md)
records the command, producing build, hashes, raw counts and limits.

Fresh retained `verified_campaign()` passes all 25 build hashes, 39 campaign
hashes, exact original four-job suffix/order, budget and producing identities.
Fresh raw `check()` calls reproduce each retained correctness/count object
after JSON-key normalization; clock qualification is a separate runner field.
All four native children exit 0, with no signal, timeout or budget failure;
all stderr files are empty. No suffix member was omitted, replaced or retried.

| Arm | Author calls / post-warmup | Replica calls / post-warmup | Author and replica called generations / reuses |
| --- | --- | --- | --- |
| M-data-protect | 148 / 128 | 143 / 128 | 34 / 36 |
| M-data-map | 148 / 129 | 147 / 129 | 33 / 36 |
| V | 511 / 426 | 389 / 326 | 38 / 36 |
| I | 528 / 423 | 540 / 440 | 38 / 36 |

The same retained finite preflight oracles pass; call counts depend on the
actual schedule and are not required to be identical. The original seed18001,
one reader/producer, signing A, normal pressure and all original floors remain.
Same local host/OS/SDK/page-size and uncontrolled placement apply. Operational
elapsed 0.647948083 s and 7,863,789 payload bytes fit the unchanged caps; they
are not protocol timing measurements. Clock evidence is absent throughout.

The four-cell suffix remains ineligible to qualify a full all-positive preflight;
the unavailable original MAP_JIT cell is unchanged. Asynchronous trace order and
admission checks still do not prove the absence of cross-actor retirement races.
Full controls, actual held-reader cases, pressure, P1/signing, 2/4/8-vCPU guest
composition, CMODX and qualified cost remain owed. Independent final findings
review belongs to `/root/mach_native_review`.

## Four-cell continuation findings acceptance — 2026-09-05

Reviewer `/root/mach_native_review`, continuing the preceding diagnostic and
suffix-method review, independent of first executor `/root`, replica executor
`/root/mach_baseline_replication` and M18 instrument authors. Inspected
MAN-01-18-04/05, replica preparation/executor reports, exact retained source,
builds, signatures, jobs, invocations, raw traces, results and budgets. No native
experiment or load-bearing code edit was performed by this reviewer.

**Verdict: accept the observed original four-cell preflight continuation and
its same-host independent replication, within the previously declared oracle
limits.** No substantive contradiction was found. This is no full preflight,
protocol safety, retirement-race, guest CMODX, Q19, platform or cost acceptance.

Independently ran the retained `verified_campaign()` for author run
`0af49bf0232245e5ac6e87fe672ed9c6` / build
`63e9b75e0bf143cbad7378130360b4f0` and replica run
`41f1c47c87ff44e294c2b8f1b56eef9b` / build
`ff721207674c4c42a9831f3dfe528931`. All25 build and39 campaign artifacts in
both packets verify; all12 source/input entries match the reviewed source
packet `4a357ea54dc5bd64b3fd4f9646d9e31481e52fda628dd490d8d15dad0aab0069`.
The native producer matches the preceding accepted diagnostic producer bytes.
Both schedules preserve exactly M-data-protect, M-data-map, V, I in original
suffix order, with identical seed18001 and complete original job arguments.
All eight child exits are zero, with empty stderr and no signal, timeout,
resource failure, omitted suffix member or replacement.

Fresh retained-checker calls reproduce all eight stored correctness/count
objects after JSON-key normalization. Separate direct raw inspection verifies
contiguous actor sequence numbers, actual call/generation/reuse counts and
every returned pair against its retained built descriptor. These checks do
not turn actor-local order into a global retirement proof.

| Arm | Author total / post-warmup calls | Replica total / post-warmup calls | Called generations / reuses in each |
| --- | ---: | ---: | ---: |
| M-data-protect | 148 /128 | 143 /128 | 34 /36 |
| M-data-map | 148 /129 | 147 /129 | 33 /36 |
| V | 511 /426 | 389 /326 | 38 /36 |
| I | 528 /423 | 540 /440 | 38 /36 |

Independently inspected `mapping_change()` and all raw M mapping records.
Each M-data-protect child retains38 successful PROT_NONE→PROT_READ round trips
on its exact owned16KiB data extent, with zero recorded errors and matching
resumed data witnesses. Each M-data-map child retains38 successful replacements
of its owned16KiB data window, alternating backing offsets16384 and0 with
versions2 and1. Every offset/address/extent record matches the owned window;
actual reader witnesses include both versions and match their expected values.
This distinguishes the submitted result from a mere API-success label or a
remap that never changes observed contents. It does not test accessibility
while the data window is protected, arbitrary executable remapping, broad JIT
restoration or all protection controls. V/I returned pairs and finite lifecycle/
publication/reuse invariants reproduce; differing call counts are admitted
schedule effects, not failed replication or identical-performance evidence.

Applicability remains Mac16,10/M4, macOS26.6.2/25G83, SDK26.4/clang21,
16KiB host pages, local plain ad-hoc A signing, one reader/producer,
256-byte blocks, rate1, normal pressure, uncontrolled placement and no guest.
Inherited launcher restrictions are not independently measured. Budgets are
the reviewed four30s child ceilings,900s stage,128MiB per output file,2GiB
operational aggregate threshold and1GiB free reserve. The retained accounting
fits them. Timing remains explicitly unqualified; operational elapsed/output
receipts are not protocol latency measurements.

The accepted consequence is that these separate local data-protection,
data-remapping and V/I preflight operations produced their bounded expected
observations in two independently prepared campaigns. The precise unavailable
M-jit-protect attempt in MAN-01-18-02/03 remains unavailable; MAN-01-18-01
remains historically ambiguous. Combining successful suffix rows with earlier
E/S rows cannot manufacture an all-positive original preflight or satisfy the
full-stage qualification consumer. Missing held-reader, negative, pressure,
SMP, signing/host and cost coverage remains missing. In particular, actor-local
retirement/admission observations still cannot rule out the disclosed cross-actor
retirement race. Full Q01-18 and Q19 composition remain open.
