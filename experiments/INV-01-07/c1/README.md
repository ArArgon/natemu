# M07-C1 clock experiment

Read [INV-01-07](../../../docs/research/investigations/INV-01-07.md) for the
question, applicability, unresolved findings and next action; follow the
[research workflow](../../../docs/research/README.md) for independent review.
This README describes operation. A structural pass does not accept a finding.

`producer.c` emits six clocks at two QoS levels and 1/2/4/8-worker contention
cells. `core.py`, `cells.py`, and `artifact.py` check arithmetic, completeness,
array checksums and controls. `runner.py` builds and supervises the fixed
three-normal/six-negative schedule, retaining actual exit/timeout records.
`clock_evidence.py` checks factual context and retained v2 campaign integrity
for consumers; it never interprets review documents.

## Build and run

Choose a new canonical directory below an existing external `NATEMU_RAW_ROOT`:

```sh
export NATEMU_RAW_ROOT=/absolute/external/raw
python3 -B experiments/INV-01-07/c1/runner.py build "$NATEMU_RAW_ROOT/c1-build"
# After pre-execution review, when an investigation authorizes its bounded campaign:
python3 -B experiments/INV-01-07/c1/runner.py campaign "$NATEMU_RAW_ROOT/c1-run" --build "$NATEMU_RAW_ROOT/c1-build" --no-other-native-benchmarks
```

Build copies local Python/C sources and repository-boundary context into
`sources/`, compiles **that snapshot**, runs its synthetic tests, signs the
binary ad hoc, and retains flags, compiler/SDK versions, selected SDK headers,
disassembly and checksums. Build executes no clock producer. Campaign execs
the retained runner and uses its retained checkers. Editorial edits and unrelated
commits do not invalidate this build; changes to retained build bytes do.

Output paths must be canonical, external and unused. The project lock and
operator assertion prevent overlapping project campaigns; they do not detect
arbitrary external benchmarks. The 300-second child watchdog kills and reaps
the process group. Fixed count, resource, mode, seed and negative-oracle checks
remain mandatory. Failures stop the campaign without replacement samples;
partial files, process records, diagnostics and a checksummed inventory remain.

`build.json` uses `INV-01-07-clock-c1-build-v2`; `plan.json` and `campaign.json`
use `INV-01-07-clock-c1-campaign-v2`. A run ID and producing build ID bind every
child, schedule, check and result. Raw producer/array formats are unchanged.
The v2 reader rejects historical build/campaign formats rather than inventing
missing approval fields. Historical gate JSON and reviews retain their original
meaning and can be read using their producing code revision from Git.

Clock consumers may call `clock_evidence.validate(campaign_json, context)`.
Missing, incomplete, corrupted or mismatched evidence returns `unqualified`;
a factual context match remains subject to findings review for instrument,
resolution and applicability. It is not automatic timing qualification.

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
pass independent review of the separate 256 MiB probe-buffer cap before collection.
The draft's maximum heap/global buffers are 39710880 bytes. A separate 1 MiB
reserve conservatively covers probe-owned automatic arrays/records (large
named buffers plus 64 KiB of small scratch/copy allowance must fit that reserve),
giving a 40759456-byte planned upper bound. Preflight and final resource
records keep the measured heap/global allocation count distinct from this
conservative automatic-buffer allowance. Neither is a claim about total
pthread stack mappings, allocator/library overhead, or native RSS.
Reading code allocates binary payload bytes separately from derived Python
values; the native allocation cap is not a claim about total checker RSS.

### Stack-buffer accounting

R2's preliminary accounting note observed that `maximum_owned_buffer_bytes`
counted only heap/global buffers (`sizeof metadata` plus `allocate()`d
arrays) and omitted simultaneous local C buffers: main's/preflight's own
`struct json out`, and clock_cell's `elapsed[200]`. `preflight()` now also
sums a `named_stack` term, over the enumerated major local buffers/records in
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
a sum of the enumerated major local buffers/records that a plain reading of
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
rewrites artifacts. Actual code/build/SDK/host/environment identities,
direct-binary raw-root enforcement, no overlapping
native benchmark, watchdog kill/reap and all prescribed negative seams belong
to the producer/supervisor's pre-execution review, not structural checker
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

## Development validation and historical provenance

```sh
python3 -B -m unittest discover -s experiments/INV-01-07/c1 -p 'test_*.py' -v
```

Synthetic tests cover fixed numeric/control oracles, array parsing, output
boundaries, non-overwrite, build identity, missing/mixed raw data, timeouts and
partial failures. The accounting test compiles and runs only a small sizeof
probe. No test collects clock findings or launches the native clock producer.

The pre-migration README's uncommitted correction recorded that REV-219
returned five changes and named REV-237 as the subsequent return (with
REV-236 self review). It also clarified the old gate's type/subject parsing.
That provenance is preserved here; current substantive dispositions live in
the investigation. The gate parser, review parameters and document-hash
receipts have been retired. Historical REV-196/219/236/237 records, where
present, remain at their original locations and are not updated by runners.

## Native review return preparation — 2026-09-05

Normal clock readers resolve once per cell, with no per-read mode/cell/string
branch. Both nsec-np QoS cells share the same reader and non-inlined zero-value
validator; its call and guard overhead is included in the measured wrapper cost.
The zero-nsec negative selects a synthetic-zero reader that calls that same
validator. It no longer calls the failure reporter directly. A synthetic C
fixture supplies 42 and zero without invoking a clock API; the zero case exits
with the API-failure record, while removing the real guard makes the negative
return normally and fail the fixture. This tests control sensitivity independently
of the native experiment.

The buffer enumeration is intentionally conservative rather than exhaustive:
small scalar/local records and compiler copies are covered by the separate
64 KiB allowance within the 1 MiB reserve. The table and ABI test agree on
143593 enumerated bytes, below the 208625 formula bound and 1 MiB reserve.

The fixed campaign has nine children: three normal and six negative. Its total
native child ceiling is 2700 seconds; a complete-child payload upper estimate
for all nine is 2042658432 bytes, with at least 4869586944 bytes of free disk
required by the runner before launch. The retained-tree check caps campaign
files at 5 GiB. Python checking and environment capture are outside the native
child ceiling; this is not a claim of a 45-minute end-to-end wall bound.
