# M07-C1 clock-instrument implementation

Status: method accepted by REV-194; implementation in progress for self195
and independent196. **No native execution or evidence collection is authorized
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
- `test_core_independent.py`: independently authored arithmetic tests; expected
  values are hand-derived without calling the tested helpers as an oracle.
- `test_cells_independent.py`: independent fixed-cardinality complete-cell
  fixtures and malformed inputs, including a late required-cell rejection.
- `test_artifact.py` and `test_binary_integration.py`: author-side parser and
  complete 80-file binary/real-CLI round trip with a late summary corruption.

The native producer, build/disassembly packet, supervising campaign/negative
runner, exhaustive artifact/CLI negative fixtures and complete independent code review remain
required. No module currently asserts that gate approvals or manifest claims
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
produce a positive normal-child report. The future runner must retain the
actual failure/signal/timeout and partial files, not fabricate normal terminal
records. Metadata files are bounded at 1 MiB each.

Across twelve clock and eight contention cells, the complete array payload is
226962048 bytes (below 512 MiB), independent of seed. Native allocations must
still independently prove the separate 256 MiB buffer cap before collection.
Reading code allocates binary payload bytes separately from derived Python
values; the native allocation cap is not a claim about total checker RSS.

The adapter requires canonical absolute external-root, child and repository
paths, rejects internal roots, symlink payload files, aliased descriptors,
wrong hashes/sizes/encodings and nonregular files. It neither creates nor
rewrites artifacts. Actual code/build/SDK/host/environment/review identities,
clean committed revision, direct-binary raw-root enforcement, no overlapping
native benchmark, watchdog kill/reap and all prescribed negative seams still
belong to the unimplemented producer/supervisor and independent code gate.

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
finding/T1/PASS prefix. This is not coverage of absent native producer or
supervisor behavior. Fixture authors cannot independently approve their own
fixture code in REV196.

Development checkpoint, 2026-09-05: the command above completed all48 tests
in58.528s with exit0. This is synthetic validation of the current Python
layers only, not code196 acceptance or a host performance observation.
