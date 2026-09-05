# INV-01-18 research instrument

Code author: `/root/register_02`. This is an experiment, not an emulator
architecture choice. Governing method: M18-2 / independent REV-180; pressure
supplement M18-P1 / REV-214; accessible-control clarification M18-N1 / REV-216.
Independent code/model/oracle review REV-200 is required before **any native
invocation**, including runtime preflight. A build or an abstract fixture is
not host evidence. The separate cost prerequisite is not assumed satisfied.

The source packet is every file in this directory, excluding Python cache
files. `run.py plan` prints its canonical SHA-256 and complete coverage
manifest without executing native code. Build/sign/disassembly artifacts and
all runtime output must be fresh and outside Git. No inherited probe or raw
result is used.

## Files and gates

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
- `build.py`: exact SDK26.4 compile, A/B/C local ad-hoc signing, captured actual
  entitlements, disassembly and hashed build manifest. It does not run binaries.
- `run.py`: source/build/signature/method-result prerequisites, external raw
  manifests, fresh children, 30-second watchdog, partial-output preservation,
  cause classification, and per-cell control/correctness qualification.

Accepted method input identities (historical, before later gate integration):

| Input | SHA-256 |
| --- | --- |
| M18-2 reviewed subject | `f2f9ba7879abe1148cc57e4b246ef7eebb6637f798da4c207d61ed1613bc06bd` |
| REV-180 | `4e18ec53d9f9930a774ec091b598cec09b78681e0d2f85c93e315b5069a692b1` |
| M18-P1 reviewed subject | `78893591eded7eaa999a5a369de892d670af7bab938c55772309e823fc74f966` |
| REV-214 | `2a7c81aedcac9876d9e12a5fbe1a3874327237da18f294ea860f372ae8c0f705` |
| M18-N1 reviewed subject | `6684fdab773666e9cf9be3bace7f76555231978886251400dead7ebf591f5452` |
| REV-216 | `9e215ee324f649e79005406ba79796d7d18a3beb41685f125fcc073bce395e66` |

Primary documentary premises remain INV18 claims04–13, accepted narrowly by
REV-154/173. Their Apple, SDK, Arm and C source limits remain operative. Public
callback/cache/ISB composition, thread migration and whole-JIT protection
restoration are experimental candidates, not source-proved complete protocols.

## Protocol and instruction details

I uses only SC entry/hazard accesses: load, advertise, revalidate, ISB/call,
clear. A protected superseded call may finish. A pre-advertisement stale load
retries; it cannot dereference code before revalidation. I retirement requires
an actual empty matching-token scan. S/M use release STOP, exact acquired
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
set, removed, acknowledged, retired and retained. Near-full scratch
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

Quantitative execution needs an independently result-accepted valid INV07
packet for the exact model/OS plus local calibration. The adapter JSON must
contain `status: accepted-valid`, exact `model` and `os_build`, and its exact
SHA-256 must appear in the independent result review. This is an explicit
required input, not a placeholder fabricated from failed timer data.

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

Native commands require an accepted independent code review pinning **both**
the source-packet SHA-256 and the exact build/signature/disassembly-manifest
SHA-256. The full experiment source must be committed before runtime. Runtime
checks actual binary signatures/entitlements and artifact hashes. Correctness
requires full preflight/hook/negative/protection qualification on the same
source/build/host. Cost also requires the full correctness campaign. Unavailable
or failed cells remain visible as blocked/not-run, never removed or passed.

Safe authoring checks from this directory:

```sh
make syntax model fixtures
PYTHONDONTWRITEBYTECODE=1 python3 run.py plan
```

Build-only, with an explicitly set external `NATEMU_RAW_ROOT` and fresh output:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 build.py --output "$NATEMU_RAW_ROOT/build-INV18"
```

After independent code acceptance, `run.py run --mode preflight|hooks|negative|
protection` takes `--binary-dir` and `--code-review`. `run.py qualify` takes one
full `--campaign` for each required stage and a fresh `--output`. Pass that
artifact as `--qualification` for correctness. Qualify again including its
full correctness campaign before cost, which also needs `--timer-packet` and
`--timer-review`. No outcome-driven campaign replacement or sample replenishment
is implemented. All runs retain the entire frozen schedule and a final hash
manifest; exact-child watchdog kills preserve partial stdout/stderr/status.

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
