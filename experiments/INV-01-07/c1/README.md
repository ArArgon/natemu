# M07-C1 clock-instrument implementation

Status: method accepted by REV-194; the producer/runner/gate packet was
self-reviewed at REV-195 (closing REV-196 R2) and then returned independently
at REV-219 (`revise`; five required changes R1-R5). This packet closes
R1-R5 and is self-reviewed at REV-236. Independent return review REV-237
against REV-219 must accept before native execution is authorized.
**No native execution or evidence collection is authorized by this
directory.** These Python modules, the compiled-but-unexecuted producer, and
synthetic tests are not a clock finding, an accepted instrument, or full Q07
completion. No historical timer entry point is reused.

## Implemented layers

- `core.py`: exact arithmetic, sample scans, fixed noise floor, containment,
  serialized global/per-worker trace checks.
- `cells.py`: complete six-clock/two-QoS and 1/2/4/8-worker/two-QoS matrices,
  strict types, API/QoS/count/oracle validation, exact summary comparisons,
  transactionally returned per-child findings and strict T1 by QoS.
- `artifact.py`: read-only adapter for fixed binary arrays and separate
  producer/process records. Hashes and sizes are checked before interpretation.
- `check.py`: structural-check CLI with exact JSON fractions and all-or-nothing
  output. It explicitly emits `evidence_approval: false`; it is not a runner.
- `producer.c`: all20 native cells, explicit raw records, checked API/pthread
  paths, exact fraction summaries, six fixed negative seams and direct-launch
  binding/external-root enforcement. No signal handler is installed.
- `runner.py`: build-only pipeline and separately gated fixed3-normal/6-negative
  campaign with300s child watchdog, exact-child kill/reap, environment capture,
  source/build/review binding, exclusive lock and durable partial inventory.
- `test_core_independent.py`: independently authored arithmetic tests; expected
  values are hand-derived without calling the tested helpers as an oracle.
- `test_cells_independent.py`: independent fixed-cardinality complete-cell
  fixtures and malformed inputs, including a late required-cell rejection.
- `test_artifact.py` and `test_binary_integration.py`: author-side parser and
  complete80-file binary/real-CLI round trip with a late summary corruption
  and hostile RuntimeError/OverflowError during final rendering.
- `test_negative_routing.py`: labelled injections reach real raw oracles;
  an undetected injection cannot return a positive finding.
- `test_runner.py`: separate-author synthetic supervision, control identity,
  review binding (including the v2 gate's independent-return requirement),
  lifecycle, resource and transactional-output tests. Native process/compiler
  operations are mocked in these tests.
- `test_producer_accounting.py`: compiles a throwaway sizeof/arithmetic probe
  against the real producer.c struct layouts and independently re-derives the
  named-local-buffer accounting table below; fails if `named_stack` ever
  under-counts it. No producer execution or `NATEMU_RAW_ROOT` use.

A real `xcrun clang` build of `producer.c` (the exact reviewed flags) and its
`otool -tvV` disassembly were inspected for this packet: the per-cell timed
reader (`reader_mach`/`reader_nsec_np`/`reader_generic`, resolved once per
cell before the timed loop by `resolve_reader`) and the contended
`pthread_mutex_lock`/`mach_absolute_time` loop each keep one real `bl` call
per iteration (no vectorization, hoisting, removed barriers, or cell/mode
classification inside the timed path); see REV-236 for the exact instruction
ranges and file hash.

No module asserts that gate approvals or manifest claims
are authentic merely because they occur in JSON. Structural PASS is not review
acceptance. Three complete normal children, separate native controls, result,
independent replication and adversarial gates remain unfulfilled.

## Record layout for the producer implementation

This fixes serialization for the approved method, not a change to sampling.
All arrays are headerless little-endian uint64 records. Their enclosing JSON
descriptors specify exactly `path`, `sha256`, `records`, `columns`, and
`encoding` (`little-endian-uint64`). Paths are unique simple `.bin` basenames.
Every record retains an explicit zero-based ID. Duplicate/missing IDs fail.

| Array | Columns in file | Records per cell/phase |
| --- | --- | --- |
| Mach or nsec_np readings | ID, raw value | 200001 |
| clock_gettime readings | ID, tv_sec, tv_nsec, converted ns, API status | 200001 |
| Cost batches | ID, start Mach tick, end Mach tick | 200 |
| Pre/post noise | ID, start Mach tick, end Mach tick | 10000 each |
| Pre/post containment | ID, M0, U0, M1, M2, U1, M3 | 256 each |
| Contention | global acquisition ID, worker ID, Mach tick | workers × 200000 |

Failed APIs terminate with separate failure records; negative/error values
must never be silently cast to uint64 and accepted. Resolution records retain
API status and original timespec. No fake getres record is allowed for Mach.
Cell summary fractions are reduced `{numerator, denominator}` objects, never
floating-point classifications. The checker derives summaries independently.

The native producer emits `record.json` without a terminal claim. The external
supervisor creates `process.json` from its actual child wait/timeout result;
fields are `exit_status`, `signal`, `timeout`, `complete`. Normal completed
records require 0/null/false/true. Incomplete or injected children cannot
produce a positive normal-child report. The runner retains the
actual failure/signal/timeout and partial files, not fabricate normal terminal
records. Metadata files are bounded at 1 MiB each.

Across twelve clock and eight contention cells, the complete array payload is
226962048 bytes (below 512 MiB), independent of seed. Native allocations must
pass independent review of the separate256MiB probe-buffer cap before collection.
The draft's maximum heap/global buffers are39710880 bytes. A separate1MiB
reserve conservatively covers probe-owned automatic arrays/records (large
named buffers plus64KiB of small scratch/copy allowance must fit that reserve),
giving a40759456-byte planned upper bound. Preflight and final resource
records keep the measured heap/global allocation count distinct from this
conservative automatic-buffer allowance. Neither is a claim about total
pthread stack mappings, allocator/library overhead, or native RSS.
Reading code allocates binary payload bytes separately from derived Python
values; the native allocation cap is not a claim about total checker RSS.

### Stack-buffer accounting table (closes REV-196 R2's preliminary note)

R2's preliminary accounting note observed that `maximum_owned_buffer_bytes`
counted only heap/global buffers (`sizeof metadata` plus `allocate()`d
arrays) and omitted simultaneous local C buffers: main's/preflight's own
`struct json out`, and clock_cell's `elapsed[200]`. `preflight()` now also
sums a `named_stack` term, over every named local declared anywhere in
`producer.c` (summed as if simultaneous, even across functions whose scopes
cannot actually overlap, so no call-graph reasoning can shrink it), and
requires `named_stack <= STACK_BUFFER_RESERVE` (1 MiB) before any array is
allocated. `maximum_owned_buffer_bytes` is `sizeof metadata + STACK_BUFFER_RESERVE
+ max(single clock_cell allocation, single contention_cell allocation)`, i.e.
every named local is covered by the reserve term, not folded into the
per-cell heap arithmetic. Real Apple-Silicon sizes (`clang -O2`, this SDK):
`sizeof(struct json)=65544`, `sizeof(struct quality)=24`,
`sizeof(struct scan)=32`, `sizeof(struct contention)=744`, `PATH_MAX=1024`.

This table is an **independent enumeration**, not `named_stack` itself: it is
the real sum of every named local >= 8 bytes that a plain reading of
`producer.c` finds, using real compiled `sizeof()` values, not the
producer's own `named_stack` expression or its 64 KiB fudge allowance.
`sizeof(struct stat)=144`, `sizeof(struct timespec)=16`, `sizeof(va_list)=8`
on this ABI.

| Local buffer | Function | Bytes |
| --- | --- | --- |
| `struct json out` (×2: main's and preflight's own) | `main`, `preflight` | 131088 |
| `received`, `expected` (`PATH_MAX+256` each) | `authorize` | 2560 |
| `root`, `repo`, `parent`, `resolved`, `identity` (`PATH_MAX` each) | `open_output` | 5120 |
| `elapsed[BATCHES]` (`uint64_t`) | `clock_cell` | 1600 |
| `temporary[4]` (`uint64_t`) | `clock_cell` | 32 |
| `struct quality quality` | `clock_cell` | 24 |
| `uptime_resolution`, `resolution` (`struct timespec`, ×2) | `clock_cell` | 32 |
| `struct contention shared` (includes `worker[8]`) | `contention_cell` | 744 |
| `global`, `workers[8]` (`struct scan`, ×9) | `contention_cell` | 288 |
| `previous[8]`, `counts[8]`, `transitions` (`uint64_t`, ×17) | `contention_cell` | 136 |
| `buffer[1024]` | `die` | 1024 |
| `record[256]` | `clock_failure` | 256 |
| `digest[32]`, `hex[65]`, `name[96]` | `array` | 193 |
| `text[40]` | `integer128` | 40 |
| `order[CELLS]` (`unsigned`) | `main` | 80 |
| `info`, `s` (`struct stat`, ×2) | `authorize`, `open_output` | 288 |
| `tag[32]` | `phase_json` | 32 |
| `name[32]` | `main` | 32 |
| `value` (`struct timespec`) | per-cell timed reader | 16 |
| `arguments` (`va_list`) | `add` | 8 |
| **Total (independent enumeration)** | | **143593** |

This 143593-byte figure is exactly what `test_producer_accounting.py`
independently re-derives from the real ABI: it does not read this table, it
recomputes each term itself and requires the two sums to agree. It then
requires the producer's own `named_stack` expression, compiled from
`producer.c:586-589`, to dominate that sum. On this toolchain
`named_stack` is 208625, which is not the sum of this table's rows — it is a
separately conservative formula that also folds in all eight worker
`quality` records (only one is live outside `struct contention`), both
`struct json` instances even though `preflight()` returns before `main`'s is
declared, and its own 65536-byte allowance for small/scratch buffers this
enumeration does not itemize. `named_stack` (208625) exceeds this
independent enumeration (143593) by 65032 bytes and comfortably clears
`STACK_BUFFER_RESERVE=1048576`.

The adapter requires canonical absolute external-root, child and repository
paths, rejects internal roots, symlink payload files, aliased descriptors,
wrong hashes/sizes/encodings and nonregular files. It neither creates nor
rewrites artifacts. Actual code/build/SDK/host/environment/review identities,
clean committed revision, direct-binary raw-root enforcement, no overlapping
native benchmark, watchdog kill/reap and all prescribed negative seams belong
to the producer/supervisor's independent code gate, not structural checker
acceptance. The parent pipe record prevents accidental unsupervised launch;
it is not authentication against a hostile parent. The lock does not detect
arbitrary external benchmarks, so an explicit operator no-overlap assertion
is also required and retained.

Cell IDs0–11 are clocks(q=id/6,clock=id%6);12–19 are contention
(q=(id-12)/4,workers=1<<((id-12)%4)). Complete cells are permuted by fixed
Fisher–Yates using uint32 state=seed and state=1664525*state+1013904223 modulo
2^32, drawing once per descending index19..1. `execution.json` records the
actual completed order; partial `cell-XX.json` records survive a failure.
Backwards/omitted clock controls target cell0; zero-nsec targets cell1;
pthread-error/global-inversion/omitted-contention target cell13. Negative
metadata retains its actual mode; an undetected injection never receives T1.
`failure.json` retains the named error; clock failures additionally retain
the distinct actual POSIX return and errno in `api-failure.json`.

The build-only subcommand requires clean committed C1 sources, compiles with
recorded-O2 flags/resolved SDK, signs ad hoc, captures disassembly/header
hashes and runs only synthetic tests. It never executes the producer.

### Gate schema `INV-01-07-clock-c1-gate-v2`

REV-196 is a preliminary independent code critique, not an acceptance: its
own verdict is `revise` and it says so explicitly ("No final196 acceptance is
issued for an incomplete packet"). It stays `revise` in this repository even
after this packet closes R2 — returns get new review numbers here, not an
edited verdict on the original critique. The v1 gate schema wrongly required
`REV-196` itself to read `Verdict: accept`, which REV-196 can never satisfy.

The v2 gate loader (`runner.load_gate`) instead requires, as separate
fields:

- `reviews.193`, `.194`, `.195`: the fixed self-method/independent-method/
  self-code reviews, each still required to read `accept` (self/independent/
  self respectively).
- `reviews.196`: the fixed independent critique, required to read `revise`
  (`Independence: independent`) — present as the record R2 is closing
  against, not as an acceptance.
- `independent_return`: `{review_id, file, sha256, expected_verdict,
  independence}`, supplied by the gate JSON itself because the review number
  that finally accepts the packet is assigned only when that independent
  return is written (this packet reserves `REV-219` for it). The loader
  independently re-verifies every one of those fields against the real file:
  `review_id` must not reuse one of the four fixed numbers above; `file` must
  be exactly `docs/research/reviews/REV-<review_id>.md`; the review at that
  path must have `Independence: independent` and `Verdict: accept`; and its
  text must mention `REV-196` (the critique it returns against). A gate
  lacking `independent_return`, one whose return review is still `revise`,
  or one whose "return" is independence `self` all refuse to load
  (`gate-fields`, `review-not-accepted`, `review-independence`
  respectively) — see `test_gate_requires_accepted_independent_return_against_critique196`
  in `test_runner.py`. The C1 source/build/method-hash receipts that used to
  live in REV-196 now must appear in the independent return review instead,
  since 196 never receives them.

Campaign execution additionally requires genuine accepted current self193/195
and independent194 records, the independent196 critique present at its
pinned revise hashes, an accepted independent return against it, exact
code/build/method receipts and committed clean scope. No accepted gate
envelope ships with this code. Reviewers must establish actual independence
and closed changes; matching bytes alone do not.

## Development checks

Run synthetic development checks only:

```sh
python3 -B -m unittest discover -s experiments/INV-01-07/c1 -p 'test_*.py' -v
```

These tests collect no host timings and write no raw evidence. Adapter and
binary tests create synthetic files only in fresh external temporary
directories, then remove those directories. The full binary test visits the
prescribed arrays through the real CLI, first accepting the synthetic record
structurally and then rejecting a corrupted final required summary with no
finding/T1/PASS prefix. `test_producer_accounting.py` compiles a throwaway
probe (real `xcrun clang`, no `NATEMU_RAW_ROOT`) but launches no producer.
These tests do not establish native runtime behavior. Fixture authors cannot
independently approve their own fixture code in REV196.

Historical development checkpoint `d49a95b`: the then-current48 tests passed
in58.528s (inspected as-is at REV-196). The draft had grown to70 tests before
REV-195's packet; that packet added the v2 gate's independent-return fixtures
and `test_producer_accounting.py`, for72 tests passing in95.117s. Independent
critique REV-219 found five further required changes (R1-R5: a per-QoS timed
seam, the stack-accounting table's own arithmetic and enumeration claims, and
two gate-binding gaps); this return closes all five and adds a bypass fixture
for each of R4 and R5, for73 tests passing in95.983s. REV-236 (self code
return) pins the exact per-file SHA-256. Independent return REV-237 against
REV-219 must accept before native execution is authorized.
