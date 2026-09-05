# M07-C1 clock-instrument implementation

Status: method accepted by REV-194; complete producer/runner draft is being
built and inspected for self195 and independent196. **No native execution or evidence collection is authorized
by this directory.** These Python modules and synthetic tests are not a clock
finding, an accepted instrument, or full Q07 completion. No historical timer
entry point is reused.

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
  review binding, lifecycle, resource and transactional-output tests. Native
  process/compiler operations are mocked in these tests.

The complete build/disassembly and independent code review remain required.
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
hashes and runs only synthetic tests. It never executes the producer. Campaign
execution additionally requires genuine accepted current self193/195 and
independent194/196 records, exact code/build/method receipts and committed
clean scope. No accepted gate envelope ships with this code. Reviewers must
establish actual independence and closed changes; matching bytes alone do not.

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
finding/T1/PASS prefix. These tests do not establish native runtime behavior.
Fixture authors cannot independently approve their own
fixture code in REV196.

Historical development checkpoint `d49a95b`: the then-current48 tests passed
in58.528s. The expanded draft has70 tests; its exact full build/validation
packet belongs in self195 and independent196, not an inferred runtime result.
