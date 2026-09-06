# INV-01-06 — M06-M1 findings and independent replication

This thread covers the six-child direct baseline supplement only. The
[investigation](../investigations/INV-01-06.md) retains the full M06-M1 scope.
The independent findings reviewer is `/root/mach_native_review`; the replica
executor below does not accept its own evidence or close the supplement.

## Independent replica preparation — 2026-09-05

Executor: Codex `/root/mach_baseline_replication`, separate from the M06-M1
implementation author `/root/publication_native_review`, first-run executor
`/root`, and pre-execution/findings reviewer `/root/mach_native_review`.
This executor authored none of the instrument, runner or existing reviews.

Read the research workflow, INV-01-06, the M06-M1 README and the native return
in [pre-execution review](INV-01-06-pre-execution.md). Inspected the retained
`src/state_server.c`, build/runner/checker, fresh AOT disassembly, signature
metadata, source identity and author run artifacts. Reused the reviewed
retained code without incorporating later working-tree control amendments.

Fresh compile-only build:
`/Users/liaozping/orca/natemu-raw/m06-build-20260905-track01-replica-a`,
build ID `8349be2f83e14617a91541d8922e3afc`. Command:

```sh
NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B /Users/liaozping/orca/natemu-raw/m06-build-20260905-track01-a/sources/build.py --output /Users/liaozping/orca/natemu-raw/m06-build-20260905-track01-replica-a
```

Every retained source byte matches author build
`528484bffa9e4369a3a6f4cf46a89360`; all 26 build-artifact hashes verify.
MIG generation, generated-header layout assertions, compile/link, separate
local signature checks and disassembly succeeded with SDK 26.4, Apple clang
21.0.0 and MIG bootstrap_cmds-138. Plain binary SHA-256:
`c1f429f22e0a521a97dd90c0c6f47115c6ce8d5293c4b6a44eab12157cbe9755`.
The obsolete lexical `retry_path_pc_unchanged=false` flag has the same
non-authoritative meaning recorded by the native reviewer; the source copies
all state words unchanged and AOT wrapper uses exact LDR/STR fault labels.
All five retained synthetic unittests passed, including 23 destructive
checker fixtures, a valid fixture and dummy-child watchdog/partial retention.
No native Mach/JIT/fault replica ran during preparation.

Proposed native command, awaiting root's independent preparation check and
exclusive execution clearance:

```sh
NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B /Users/liaozping/orca/natemu-raw/m06-build-20260905-track01-replica-a/sources/run.py run --build /Users/liaozping/orca/natemu-raw/m06-build-20260905-track01-replica-a --output /Users/liaozping/orca/natemu-raw/m06-run-20260905-track01-replica-a
```

This admits exactly original jobs 0,1,2,12,13,14: one worker, non-JIT, plain
local ad-hoc signing, direct path, serial server, load/store × seeds
60601–60603, ten warmup and 100 checked events each. No warmup correctness
failure is excluded. Stage budget remains 900 s and 1 GiB output threshold,
1 GiB free-space reserve, child 30 s total/5 s no-output-progress watchdog,
64 MiB each stdout/stderr file. Stop admitting children at first failed,
unknown or resource outcome; preserve partial outputs and every remaining
full-matrix job as not-run. Build/signature preparation is separately bounded.

### Independent inspection of author baseline

Author run `fec0b9d0da9548c4a99ecc859e9fed85` at
`/Users/liaozping/orca/natemu-raw/m06-run-20260905-track01-a` has 54 retained
artifact hashes, independently verified. All six complete traces pass a
fresh retained-checker invocation, have exit 0/no watchdog or resource stop,
and each contains 110 complete event lifecycles: 100 checked plus ten warmup,
one wire/receive/identity/repair/reply/send/resume/commit/disarm per event.
The campaign records six checked children and 11,931 not-run entries. This
independent artifact inspection is not a second native execution.

A separate direct raw-byte audit, without importing the retained checker,
also checked all 660 events: decoded x9/x19 cookie and x10/x20 nonce equality,
x21 sentinel, x22/SP/before-after SP equality, raw PC versus exact declared
fault PC in this non-opaque arm64 build, all 68 received/prepared state words,
successful repair/send and independently calculated load/store values. Every
assertion passed. This does not generalize the raw PC representation to other
ABI or pointer-authentication configurations.

Observed author applicability is Mac16,10 (M4), macOS 26.6.2/25G83, SDK 26.4,
one worker and one front server without a guest or requested core placement.
The recorded initial worker handler is absent, restored selected-mask handler
is null, and task snapshots retain a null handler. Thus these runs do not
exercise disposal of a non-null snapshot reference or foreign handler
coexistence. Only owned handler/control/receive/mapping/port-set disposal is
observed here. All forwarding, restored-prior proof, JIT/signing, multi-worker,
negative, held-actor, second-host and broader method obligations remain open.
No timing magnitude, translator restart discipline or Q19 Mach composition
conclusion follows from this baseline.

### Independent preparation check and execution clearance

Reviewer: `/root`, 2026-09-05, separate from replica executor. Compared every
retained source byte against the accepted author snapshot, independently
verified all 26 replica build hashes, inspected the producing build commands,
actual plain/hardened signature selection and retained runner's admission and
budget code, and confirmed the proposed output does not exist. The prior
independent pre-execution acceptance applies unchanged to these six jobs.
Preparation ACCEPT: execute exactly the command and six-child limits above,
with no other native campaign overlapping. All root native runs have terminal
outcomes; other authors are preparing code only. Preserve any failure without
replacement. This clearance accepts preparation, not prospective findings.

## Independent native replica result — 2026-09-05

Executor `/root/mach_baseline_replication` ran the exact cleared command after
root's independent check and exclusive-slot clearance. The campaign terminated
with exit 0; there is no live replica process. Run
`d2b7b96a12604e8298aa571366a70d5c`, producing build
`8349be2f83e14617a91541d8922e3afc`, is retained in
`/Users/liaozping/orca/natemu-raw/m06-run-20260905-track01-replica-a` and
documented by [MAN-01-06-04](../manifests/MAN-01-06-04.md).

All six children completed with exit 0 and no watchdog/resource/campaign
failure: 600 checked and 60 warmup events, 660 commits, one complete delivery,
unchanged prepared state, successful repair/send, measured resume and commit
per event. The retained checker and the separate direct raw-register/data
audit described above both pass every replica trace. All 26 build and 54 run
artifact hashes verify. No samples were replaced or excluded. All remaining
11,931 campaign entries remain not-run; the final status explicitly says
`baseline-complete-full-method-incomplete`.

The captured environment matches the author run's M4/Mac16,10,
26.6.2/25G83/SDK26.4, plain signing, non-JIT one-worker scope. All snapshot
handler ports are again null, so this replica adds no non-null snapshot-right
or foreign-handler observation. It reproduces the bounded baseline behavior
on the same host without widening applicability. Timing is unqualified and
the broader M06-M1 method, controls and Q19 prerequisite remain incomplete.
These are executor findings submitted to `/root/mach_native_review`; no
self-acceptance or supplement-completion verdict is made here.

## Independent six-child findings review — 2026-09-05

Reviewer/context: `/root/mach_native_review`, separate from native instrument
author `/root/publication_native_review`, first executor `/root` and replica
executor `/root/mach_baseline_replication`. This reviewer performed the
pre-execution review and authored no producing source or replica. No native
experiment ran during findings review. Later working-tree N4/N5 changes were
not treated as producing code: inspection and checking used the retained
reviewed snapshots for these exact packets.

**Verdict: accept the bounded observed six-child direct baseline and its
same-host independent replication.** On the recorded Mac16,10/M4, macOS
26.6.2/25G83, SDK26.4, plain ad-hoc/non-Hardened Runtime, non-JIT,
one-worker/one-serial-front-server cell, the predeclared load/store accesses
on an owned PROT_NONE page generated attributable state-only Mach deliveries;
page repair and unchanged general-state replies were followed by the measured
access result, preserved tested registers/SP and exactly one commit. The
selected worker handler was restored while the worker remained alive and the
recorded owned resources were disposed successfully. All initial worker/task
handler entries were null; task configuration remained null/unchanged.
This accepts no timing measurement, full M06-M1 completion or Q19 baseline.

### Evidence independently checked

Inspected [MAN-01-06-03](../manifests/MAN-01-06-03.md) and
[MAN-01-06-04](../manifests/MAN-01-06-04.md), their exact commands, terminal
records, preparation independence and preceding six-child clearance. In both
external packets, independently verified every26-file build inventory and
54-file run inventory. Every retained C/Python source byte agrees with the
final reviewed baseline snapshot
`/private/tmp/natemu-m06-return-review-jxv9ho22/build/sources`; the replica
has a separate producing build identity. Initial and replica build IDs are
`528484bffa9e4369a3a6f4cf46a89360` and
`8349be2f83e14617a91541d8922e3afc`; run IDs are
`fec0b9d0da9548c4a99ecc859e9fed85` and
`d2b7b96a12604e8298aa571366a70d5c`.

Independently matched each job, identity, invocation and actual plain binary
checksum to its producing build and original schedule. Both schedules admit
only indices0/1/2/12/13/14: load/store × seeds60601–60603, n=1,
non-JIT/direct/serial/plain,10 warmup and100 checked events per child.
All six children in each packet exit0 with no timeout or resource stop.
All other11931 schedule entries are explicitly not-run. Campaigns report
`baseline-complete-full-method-incomplete`, no campaign exception and no
replacement or excluded warmup failures.

Reran the retained checker on each of the12 traces. Separately audited all
1320 events directly from JSONL and binary request bytes without using its
state/oracle helpers. Required exactly one arm/wire/receive/identity/repair/
reply/send/resume/commit/disarm per event and110 event IDs per child.
Decoded the336-byte request and all68 ARM general-state words; verified
x9/x19 cookie, x10/x20 nonce, x21 sentinel, x22/raw-SP/SDK-SP and
before-after SP equality, and raw PC against both the SDK-decoded PC and the
exact declared fault label. Raw-PC decoding is confined to this reviewed
non-opaque arm64 build, not generalized to authenticated-pointer configurations.
All received state words match the prepared reply. Recomputed load results as
0x1111111111111111 and store results as0x2222222222222222 XOR(event+1);
all agree with actual resumed data and the declared expected value. Repairs
and sends return success. The retained checker independently verifies the
required stage ordering, send-before-commit, disarm/park/restoration/living
worker order, semantic snapshots and complete successful disposal.

### Alternatives and applicability

An API-success-only explanation does not cover the exact fault PC/address,
register cookie/nonce, unchanged raw reply and resumed data evidence. Skipping
the load leaves its address argument instead of the required value; skipping
the store leaves the distinct initial page value. The inspected AOT wrapper
contains one tested access and one return; repeated classified delivery is
terminal and duplicate commits fail the checker. These controls support the
bounded host retry observation, not translator guest-instruction commit or
restart discipline. Both executions reproduce the categorical result; no
latency distribution, performance advantage or probability bound is inferred.

Non-null saved-handler reference disposal, arbitrary prior-handler coexistence,
forwarding/restored-prior proof, generated or mutable JIT code, hardened signing,
multiple faulting workers, deterministic holds and runtime negative-terminal
controls remain unobserved in this packet. Their absence is retained scope,
not silently counted as passing. The full method still requires those
prepared/reviewed controls, broader observations and replication. In
particular, this baseline does not establish server-death recovery, JIT grant
preservation, a guest machine, platform-wide behavior or Q19 composition.

No substantive contradiction was found in the supplied scoped claims. The
investigation may record this accepted bounded observation while keeping
M06-M1 and its remaining N4/N5/coverage obligations open.

## Controls campaign stop: independent diagnostic review — 2026-09-05

Reviewer/context: `/root/mach_native_review`, independent of correction author
and executor `/root`; same reviewer as the accepted R1–R5 method return.
This is a retained-packet and primary-source review, with no native execution.

**Verdict: preserve the campaign as incomplete and return the receive-error
instrument for correction.** The first receive-right-destruction child is
INVALID under its producing consumer. This is neither an accepted destruction
control nor a platform mechanism kill. No held-actor execution is cleared by
this partial campaign; the previously accepted exact-six baseline is unchanged.

Independently verified all31 producing build hashes in
`m06-build-20260905-track01-b` (build ID
`348fccff5fa8403eb2e80907210eb462`) and all110 campaign hashes in
`m06-controls-20260905-track01-a` (run ID
`0280d32060654bea85290be07fea4cf2`). Reran the retained `controls.check_control`
on all13 executed children: indices384–395 reproduce their checked results,
and396 reproduces `UNHANDLED_UNRESOLVED_OR_UNRELATED`. The first four control
kinds have three checked seeds each; these partial checks are not independently
replicated findings acceptance. The schedule retains11924 not-run entries,
including20 remaining auxiliary controls and every hold; there was no replacement.

**Observed retained diagnostic.** Child396 is n=2, perworker/direct, non-JIT,
plain-signing load, seed60601, zero warmup and one scheduled event. Its trace
registers worker0 receive port6659 and worker1 port2819; worker0 is armed,
then publishes the destruction trigger and successful receive-right decrement
for port6659 (`kr=0`). The next record is the generic terminal
`receive failure`, code268451846. The parent observes SIGBUS (`returncode=-10`),
no timeout/resource stop, and no delivery, repair, resume or commit. The error
record contains neither attempted receive port nor owning receiver identity.

**Source-backed interpretation.** SDK26.4 `mach/message.h` names
0x10004006 `MACH_RCV_PORT_CHANGED`, although its comment calls the result
obsolete. Apple's published XNU `ipc_mqueue_receive_results` maps
`THREAD_RESTART` to this result; `ipc_mqueue_receive_on_thread` returns that
wait result when it finds a destroyed wait queue, and `ipc_mqueue_changed`
wakes receivers with it. Its TODO explicitly leaves moved versus destroyed
rights undifferentiated. Thus the numeric result alone does not identify a
particular destruction. See [Apple XNU, osfmk/ipc/ipc_mqueue.c](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/ipc/ipc_mqueue.c),
inspected2026-09-05. Independently, Apple's
[libdispatch, src/mach.c, synchronous reply receive error handling](https://github.com/apple-oss-distributions/libdispatch/blob/main/src/mach.c)
groups PORT_CHANGED with INVALID_NAME and PORT_DIED for a canceled channel's
destroyed reply port. These published source paths support admitting the
specific code as a possible owned-port destruction outcome; neither source
is established as the identity-matched implementation of macOS25G83.

**Inferred instrument defect and unresolved alternative.** The producing C
special-cases only PORT_DIED/INVALID_NAME, so PORT_CHANGED necessarily takes
its generic failure branch. `fail()` logs before `fsync` and `_exit(70)`;
the worker can concurrently proceed to its protected access after publishing
destruction. A fault signal winning that interval is a plausible explanation
for terminal-log plus SIGBUS, not observed proof of the signal's cause. The
missing receiver identity also prevents establishing from raw records which
of the two receivers produced this error.

**Smallest remedy and closure conditions.** Retain this packet unchanged and
add a precise receive-error witness to future producing code: attempted port,
registered owner/role, numeric result, and acquire-observed successful
owned-destruction publication. Bind the consumer to the scheduled worker and
registered port, with arm→trigger→successful action→witness ordering and no
accepted delivery/repair/resume/commit. PORT_CHANGED may join the exact
source-supported INVALID_NAME/PORT_DIED set only behind that causal witness.
A witness followed by a declared contained signal can describe termination
after an attributable receive error; it cannot establish that error caused
the signal or that the explicit fatal exit completed. Generic or unrelated
terminal/error records, wrong-port witnesses, absent destruction publication,
unknown errors and resource stops must remain invalid. Challenge those cases
with reusable synthetic mutations and review the resulting source/build and
fixed next execution schedule before any further M06 native work. A changed
consumer does not retroactively qualify the old generic record.

## Second controls stop: independent diagnostic review — 2026-09-05

Same independent reviewer `/root/mach_native_review`; no M06 native execution.
Independently checked [MAN-01-06-06](../manifests/MAN-01-06-06.md), all134
campaign and32 producing-build hashes, and replayed all16 attempted children
with their retained producing consumer. Indices384–398 reproduce15 checked
controls; index399 reproduces INVALID/CONTROL_TERMINAL_OUTCOME. The incomplete
campaign,17 remaining auxiliary controls and unlaunched holds remain as retained.
These partial checks are not independent replication or findings acceptance.

The received reply-right witness in399 differs materially from MAN05's generic
unidentified receive error: raw state attribution, successful repair, prepared
reply, actual drop of received right6147, front send result268435459 and matching
specific terminal are all retained. SDK26.4 `mach/message.h` defines that result
as MACH_SEND_INVALID_DEST. Actual SIGBUS, no timeout/resource stop, and no resume
or commit are recorded. Source permits termination to race the logged fatal
path before `_exit(70)`; the signal's causal provenance remains inferred.

**Disposition: preserve the producing INVALID result.** A future consumer may
support the narrower observation of contained termination after witnessed reply
right loss and an exact failed send, subject to full attribution and causal
ordering review. It cannot establish clean explicit exit, safe fallback,
signal causation or completed lifecycle. The proposed consumer-only return
and its initial ordering counterexamples are recorded in the existing
pre-execution thread. No full-control/hold conclusion or modification of the
accepted six-child baseline follows from this diagnostic review.
