# INV-01-18 research instrument

The [investigation](../../docs/research/investigations/INV-01-18.md) owns the
question, conclusions, limitations and next action. Follow the
[research workflow](../../docs/research/README.md) before native execution.
This experiment selects no emulator architecture. A build or synthetic fixture
is not host evidence; inherited probes/results are excluded.

`run.py plan` prints the source inventory and coverage without executing native
code. Build/sign/disassembly and runtime outputs are fresh external artifacts.

## Files

- `src/protocol.c`: actual I/S/E/V/M worker protocols, generated instructions,
  immutable metadata, bounded trace collector, deterministic holds and
  injections, pressure setup, sacrificial protection children, calibration
  and same-coordinator cost instruments.
- `coverage.py`: deduplicated core/one-factor cells and every applicable/NA
  hook/identity row. No two non-core factors are silently Cartesianized.
- `model.py`: independent abstract SC hazard schedules, epoch and serialized
  invalidation checks, exhaustive mixed-immediate-field oracle, and 56 pressure
  arm/topology/layout cases. It imports neither the C code nor the checker.
- `check.py`: two-pass streaming raw validator. Actor-local sequence order is
  not a global SC order or cross-thread clock. Per-call results are checked.
- `tests.py`: synthetic positive/mutation fixtures for traces, clocks,
  cause-specific negatives and N1 protection controls. Never host evidence.
- `validate.py`: captures syntax/model/fixture checks in a fresh external
  manifest; it never invokes the native instrument.
- `build.py`: selected SDK compile, A/B/C local ad-hoc signing, captured actual
  entitlements, disassembly and hashed build manifest. It does not run binaries.
- `run.py`: source/build/signature integrity, external raw
  manifests, fresh children, 30-second watchdog, partial-output preservation,
  cause classification, and per-cell control/correctness qualification.


## Protocol and instruction details

I uses only SC entry/hazard accesses: load, advertise, revalidate, ISB/call,
clear. A protected superseded call may finish. A pre-advertisement stale load
retries; it cannot dereference code before revalidation. I retirement requires
an actual empty matching-token scan, checked in-process at admission time
(`protocol.c:410`) and by per-page version floors. `check.py` verifies each
admission's own recorded state and floor but has no cross-actor invariant
tying a `retire` record to a later admission of the same token, so it cannot
by itself detect a native reader that raced ahead of the retiring store; see
`check.py`'s module docstring. S/M use release STOP, exact acquired
ACKs, mutation, entry installation, matching release RESUME and fresh admission.
E uses publication then exact acquired epochs with no artificial RESUME. V
constructs and executes each slot on its designated reader; a waiting builder
services epochs without retaining its prior code pointer. Only the coordinator
owns final version comparison, installation and complete invalidation. Two
producers queue overlapping intents but receive serialized exclusive grants.

Blocks are 64/256/4096 bytes. A bounded hook-only prologue saves/restores LR
and calls an ordinary host helper that reads the atomic release flag; no
unreviewed machine-code atomic is invented. Unhooked blocks have no helper.
Eight MOVZ/MOVK immediate fields encode two 64-bit returns, followed by explicit
NOP padding and RET. The boundary layout accounts for the hook prologue so
the changed fields, not merely the prologue, straddle the 64-byte/host-page
boundary. Actual generated words are recorded for initial templates; the
checker independently reconstructs them. The abstract fixture enumerates all
256 old/new-field mixtures for each size/hook form (1,536 cases), rejecting
every mixed pair. It is not a proof of arbitrary torn-instruction detection.

Generation and epoch counters/entries are nonwrapping 64-bit. A checked 32-bit
descriptor index is a bounded storage representation, not a wrapping generation
counter. Each immutable descriptor is exactly 24 bytes: 64-bit version,
32-bit slot, 16-bit owner/page, 32-bit nonce and alignment padding. The nonce
is chosen before publication so every one of the eight 16-bit fields differs
from the physically reused generation. Descriptors survive until all joins.
Call-floor counters begin after warmup: pressure/setup/warmup calls cannot
satisfy the required post-warmup 16,384 calls per reader (128 in diagnostic
children). Their raw records are nevertheless retained and checked.
Requests are immutable 16-byte floor/page/stop records. C static assertions
and raw allocation records pin these sizes.

The exact preflight adds all full-count descriptors/requests, actual H setup
generations, applicable two scratch generations and one I/E/V replacement,
up to 2H+2 setup epochs, baseline-only pair arrays, slot bookkeeping and bounded
trace rings. A conservative 1 MiB reserve covers coordinator clock/sort arrays,
stdio and other fixed owned metadata. The 256 MiB limit concerns owned
metadata/trace buffers, not RSS or OS stack reservations. The arena remains
one 16 MiB MAP_JIT mapping. If the calibrated full plan does not fit, the child
emits `capacity-unresolved` before evidence; it does not recycle descriptors,
reduce counts or choose a smaller favorable batch.

## Controls and pressure distinctions

Hook children wait for the actual selected reader/writer state and recorded
competing attempt, not a timed sleep. For protected I/rate64 holds, the writer
must replace the held entry and record a scan retaining that exact token.
Before-ACK holds require all other readers' exact acknowledgements. Writer
holds before publication permit valid old-current calls. V's same-worker
builder cannot concurrently be an old-code reader; its coordinator build-wait
attempt is not reported as that impossible old-reader witness. Unhooked stress
remains separate. Seed-selected zero-to-three dispatcher yields are recorded;
they are schedule perturbations, not latency bounds.

Each held pressure slot is genuinely published, called by its intended reader
set, removed, acknowledged, retired and retained. This retirement is the same
in-process/version-floor property described above, not an additional
cross-actor guarantee the streamed checker independently verifies. Near-full scratch
reservations are explicitly BUILDING→DISCARDED without execution rights; they
never count as retirement or executable reuse. I/E/V additionally require an
actual no-free→H release→new-token publication/call chain and exact retained-set
swap. Scratch capacity is the cause of that prefix's no-free state; it is not
a held-reader claim. S/M retain separate allocator rejection and actual
in-place executable reuse. The original executable held-reader hooks are still
mandatory. Setup, scratch, replacement, warmup and stress are separately tagged.

Deterministic negatives include complete wrong-generation code, return
corruption, stale/unowned installation, stale/lost ACK, double grant, protected
early retirement, ABA and trace removal/truncation/duplication. A durable
cause-specific stderr witness and the expected state failure/status are
required. An unrelated crash or timeout cannot satisfy a negative. Dangerous
grant/retirement/install paths fail their state oracle before an unsafe store;
no C data race or arbitrary UAF is counted as a successful negative control.
Cache omission, ISB omission and early cache publication are sensitivity arms;
a clean finite run is INCONCLUSIVE, never a disproof of cache obligations.

Protection controls run before any thread creation, in separate sacrificial
children. Data neighbor access is at the boundary outside the full scheduled
one/four-page protected extent, within the original owned control mapping.
Whole-arena JIT accessibility executes an actual noinline AOT function reading
owned normal data before/after PROT_NONE, with both addresses outside the arena.
It makes no physical JIT-neighbor claim. Forbidden accesses require a durable
arm marker and SIGBUS/SIGSEGV; restored JIT execution additionally records the
normal-context empty approved callback, cache/ISB path and returned pair.
The threaded JIT-protection arm also requests explicit normal-context empty
callback restoration from both producer threads, when present, and waits for
their acknowledgements; every executing reader restores before its next call.
No fault handler resumes modified code. C never calls the toggle API. Success
of mprotect plus empty callback restoration is not assumed from documentation.

## Cost and qualification

Optional `--timer-packet` names a retained INV-01-07 C1 v2 campaign. The
runner checks its artifacts and factual host/OS/architecture/timebase context.
Missing or mismatched evidence leaves timing unqualified; correctness remains
separately classified. Reviewers judge applicability and accepted conclusions.

Each cost child retains 10,000 pre/post pairs per worker/coordinator. All
coordinator zeros participate in p99; an all-zero set is unresolved, while
backward/overflow is invalid. Exactly 64 no-op handshake batches at each
1/16/256/4096 size select the smallest whose every interval exceeds
`max(100,20*pre_p99)`. Exactly 1,000 full-count evidence batches are retained.
Every evidence batch and reported phase must exceed the final pre/post
threshold or the whole quantitative result is unresolved, without filtering
or retuning. Warmup/setup phases are not timed or included. No cross-thread
timestamp subtraction, half RTT or baseline subtraction is used.

Phase IDs: 1 publication/grant-completion envelope; 2 request-to-all-ACK;
3 actual mapping call envelope; 4 RESUME-to-all-first-return; 5 retirement
request-to-reusable; 6 installation-to-intended-reader return; 7 all-ACK through
complete mutation/cache work; 8 immediate writer-grant/request-to-acquired-
completion (or the local writer operation). Phase1 is the larger publication
envelope and is not substituted for phase8. Intervals are nested, include trace overhead,
and use the same coordinator. Throughput uses a same-coordinator interval and
checked call/publication counts; retention records actual slot states.
Baselines are separately identified data indirection (real immutable pair-array
loads) and metadata-only protocol handshakes with no code/mapping operation.
Neither is pure API latency. Single-reader immutable I is the diagnostic base.

Runtime checks actual binary signatures/entitlements and artifact checksums.
Correctness requires complete preflight/hook/negative/protection controls on
the same retained source/build/host. Cost additionally requires the complete
correctness campaign. Unavailable or failed cells remain blocked/not-run.

### Proposed remaining preflight stage

`--mode preflight-remaining` retains exactly the four original seed18001
schedule entries after M-jit-protect: M-data-protect, M-data-map, V, I, in
that order. Its purpose and pre-execution review status are in INV-01-18.
The earlier partial campaigns and failed MAP_JIT attempt remain immutable;
this stage cannot replace a complete `preflight` in control qualification.
There is no native producer change or altered per-child sample count.

The four native ceilings sum to120s; the existing900s stage,2GiB output
threshold,128MiB per-file limit and1GiB free reserve apply. The first
unavailable/invalid correctness outcome stops admission and retains the rest
as not-run. Timing remains unqualified without applicable evidence. Synthetic
checks compare all four complete job records to the original shuffled suffix
and confirm that even four positive results cannot qualify a full preflight.
This stage is proposed preparation until independent review accepts it.

Safe authoring checks from this directory:

```sh
make syntax model fixtures
PYTHONDONTWRITEBYTECODE=1 python3 run.py plan
```

Build-only, with an explicitly set external `NATEMU_RAW_ROOT` and fresh output:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 build.py --output "$NATEMU_RAW_ROOT/build-INV18"
```

After pre-execution review, run a stage with:

```sh
python3 -B run.py run --mode preflight --binary-dir "$NATEMU_RAW_ROOT/build-INV18"
```

Other stages are hooks, negative, protection, sensitivity, correctness and cost.
`run.py qualify` takes one full `--campaign` per required stage and a fresh
`--output`; pass that artifact as `--qualification` for correctness. Qualify
again including its full correctness campaign before cost. Optional
`--timer-packet` supplies a retained C1 v2 campaign for factual timing context.
There are no review-file, review-ID, self-review or current-HEAD parameters.

Build v2 retains the experiment sources, checker/coverage code, entitlements,
Makefile and local clock checker; compilation/signing use those retained files.
Build commands, flags, SDK/compiler and outcome are recoverable in the build
manifest. Runtime and qualification execute the retained runner. Campaign,
child, invocation, qualification and build records use explicit v2 schemas;
old records require their original Git readers and are never silently upgraded.
One campaign identity associates children and the exact producing build.
README edits and unrelated repository commits cannot invalidate that build.

All runs retain the frozen schedule and checksum manifest. Exact-child watchdogs
preserve partial stdout/stderr/status. A campaign never replenishes samples or
replaces unfavorable controls. Raw and build directories are exclusive beneath
external `NATEMU_RAW_ROOT`. No native campaign is part of migration validation.

The current covering design has 482 cells: 42,876 hook children, 36 negatives,
1,944 protection children, 1,446 sensitivity children, 2,410 correctness children
and 7,230 cost children including baselines, plus seven ordinary preflights.
Planning estimates explicitly state minimum-call/record-size assumptions and
all four possible calibration batch sizes; they are not disk or speed bounds.
Extra dispatcher calls can enlarge raw output. Unknown failures stop campaigns.

Scope stays the available Mac16,10/recorded OS/SDK and local A/B/C capability
cells, not the exact required P2 model, P1, Developer ID or notarization.
Placement, inherited launcher sandbox restrictions and migration applicability
are not inferred. Multiple non-core interactions, executable remapping, write-
region faults, cancellation, lifecycle and real guest semantics remain debt.
Neither a finite pass nor a timeout completes the full question or selects
an architecture.

## Bounded first native stage — 2026-09-05

Only the existing complete `preflight` stage has an execution budget in this
return: seven children, one each for I/S/E/V/M-data-map/M-data-protect/
M-jit-protect, with unchanged one-reader/rate-one/signing-A cell selection,
seed 18001, four warmups, 32 publications and 128 minimum calls. This stage
can expose a basic instrument/protocol failure; successful preflight alone
qualifies no stress/cost cell and establishes no shared-code protocol finding.

The per-child ceiling remains 30 s (210 s aggregate child ceilings). A 900 s
parent wall timer covers the stage loop, per-job environment capture, native
waiting and checking. Initial source/build/signature checks precede this timer;
their platform commands each have 10 s timeouts. Each native output file has
an inherited 128 MiB RLIMIT_FSIZE ceiling, so stdout and stderr total at most
256 MiB per child. Seven such pairs total at most 1792 MiB, leaving 256 MiB
for metadata inside the 2 GiB campaign output threshold. The runner monitors
total output and a 1 GiB free-disk reserve while waiting and between children.
Polling thresholds are stop conditions, not filesystem quotas; terminal
failure metadata and partial artifacts are retained even after a cutoff.

Any invalid, timed-out, unavailable or other unqualified preflight outcome
stops later jobs, which remain explicitly not-run. A deadline/output/reserve
cutoff kills and reaps the owned child process group. Failure records and
checksums remain; there are no replacement children. Campaign v3 requires
a matching stage budget; historical v2 campaigns keep their original readers.

Full plan expansion still totals 55,949 children and 1,678,470 s (19.43 days)
of child timeout ceilings. Planning estimates for hooks/correctness/cost are
not storage caps, and extra dispatcher calls can exceed those estimates. All
other full stages retain their original cells/counts but refuse execution until
their aggregate resource schedules are explicitly budgeted and reviewed.
Cost also awaits applicable accepted clocks and controls. This staged return
does not resolve those outstanding obligations or complete INV-01-18.

Author checks: existing 34 trace, 22 protection, six clock and three negative
classification fixtures pass, along with retained-identity/qualification checks
and synthetic child success, wall deadline, output threshold, kernel file cap
and low-disk-reserve fixtures. No native experiment ran during preparation.

## R18-F1 failure-path correction and fixed follow-up

The first preflight stopped on M-jit-protect's exit78/errno13 before its queued
mapping records drained. Its immutable artifact remains incomplete. A new
synchronous `INV-01-18-mprotect-failure-v1` stderr record now retains address,
length, both requested protections, first-call result/errno and whether the
restore was attempted. A skipped restore has null result/errno; it is never
fabricated as a failed actual call. The reporter runs before terminal exit.
The consumer requires matching producer run/build identity and selected
JIT/data extent/protection before attaching a failure witness. Exit78 cannot
overwrite an invalid producer identity.

Synthetic reporter fixtures fabricate first-call and restore-call failures,
with no mprotect invocation, and reject missing fields, wrong extent/protection
and confused attempted/skipped restore. Real dummy-child consumer fixtures
show wrong/missing run/build headers remain INVALID despite a plausible witness.
The complete existing fixture suite also remains required before execution.

Proposed follow-up: one newly built seven-child preflight, identical frozen
counts/order/cells and the same 900s/2GiB limits, after independent review of
this correction. Its purpose is to distinguish the previously unrecorded syscall
outcome, not replace the original partial campaign or recover its timings.
Stop on the first unavailable/invalid result; preserve all unrun jobs. No further
repeat or replica follows automatically from this diagnostic collection.

The proposed full36-child negative stage now has explicit1,800-second/2GiB
aggregate thresholds,128MiB per output file and1GiB free reserve. Its inputs
and controls are unchanged; independent pre-execution acceptance is still
required as recorded by the investigation. Other unbudgeted stages still refuse
execution, and a completed negative stage alone cannot qualify stress/cost.
