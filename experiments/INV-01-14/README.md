# INV-01-14 experiment — per-instance resource-budget capability probe

Measures, on this host/OS/SDK, which public resource-limit mechanisms
`Q-01-14`/`KA-13` can rely on: whether `RLIMIT_NOFILE`, `RLIMIT_CPU`, and
`RLIMIT_AS`/`RLIMIT_DATA` (`setrlimit(2)`, `SRC-035`) are actually enforced;
whether a `DISPATCH_SOURCE_TYPE_MEMORYPRESSURE` dispatch source can be
registered; and what `task_info(TASK_VM_INFO)` reports — including
`phys_footprint` and `region_count` — before and after touching 1 GiB of
anonymous memory.

Method reference: `../../docs/research/investigations/INV-01-14.md`.

## Layout

- `src/probe.c` — the C probe. Apple clang only, SDK headers only, no
  third-party dependency. Emits one JSON object to stdout per invocation:
  lowers `RLIMIT_NOFILE` to 8 and opens `/dev/null` until `EMFILE`; sets
  `RLIMIT_CPU` to 1 s and spins under a `SIGXCPU` handler; sets
  `RLIMIT_AS`/`RLIMIT_DATA` to 64 MiB and attempts a 256 MiB `mmap`/`malloc`
  (touched with `memset`, not just reserved); registers a memory-pressure
  dispatch source; and snapshots `task_info(TASK_VM_INFO)` before and after
  first-touching a 1 GiB anonymous mapping.
- `src/check.py` — Python 3 standard-library checker. Validates a probe
  output against the invariants a truthful run must satisfy; used both as
  the negative-control check and, later, as an independent sanity check on
  Observed results (it does not itself decide claim acceptance).
- `build.sh [build-dir]` — compiles `src/probe.c` into the requested build
  directory and ad-hoc signs it (no entitlement is needed for any check
  here). `run.sh` places that directory under the external run artifact.
- `run.sh` — the evidence run. Refuses without `NATEMU_RAW_ROOT` set to an
  existing directory outside the repository; writes nothing under
  `experiments/`. Produces a normal run and a `--negative` control, a
  `check.py` pass/fail record for each, and an environment capture
  (`sw_vers`, `sysctl`, `clang --version`, `git rev-parse HEAD`,
  `codesign -d --entitlements -`, SDK path), all under
  `$NATEMU_RAW_ROOT/INV-01-14/<UTC-timestamp>-<git-short-rev>/`, plus a
  `SHA256SUMS.txt` of every file written.

## Negative control

`run.sh` always includes one run with `--negative`, which deliberately
reports `rlimit_cpu.sigxcpu_delivered` as `false` regardless of whether the
process actually caught `SIGXCPU` after exceeding its 1-second `RLIMIT_CPU`.
`setrlimit(2)` (`SRC-035`) documents `RLIMIT_CPU` as delivering `SIGXCPU` on
the process when the soft limit is exceeded, so a truthful run under a busy
loop must eventually observe delivery; `check.py` asserts
`rlimit_cpu.sigxcpu_delivered == true` unconditionally. The negative run must
FAIL that check, and its `negative.check.txt` output is retained as proof the
checker detects the injected defect. It establishes no claim.

The predicted `phys_footprint` and `region_count` responses are reported as
`MEET`/`MISS` decision thresholds. A miss is a result for the result gate, not
a measurement-integrity failure.

## Known limitation carried into the method, not hidden here

The `RLIMIT_AS`/`RLIMIT_DATA` checks observe only reservation-time behavior
(the return value of `mmap`/`malloc`, with the `malloc` case touched via
`memset` to force real commit). Neither check drives an anonymous `mmap`
region incrementally to the point of first-touch failure, because catching a
mid-touch `SIGSEGV`/`SIGBUS` safely and resuming to produce JSON would need a
`sigsetjmp`/`longjmp` fault-recovery harness this probe does not implement.
`INV-01-14.md` records this as an open limitation, not a resolved absence of
first-touch enforcement.

## Build and run

```sh
./build.sh
NATEMU_RAW_ROOT=/path/outside/repo ./run.sh
```

Development smoke tests (compiling and running `build/probe` directly, or
`python3 src/check.py <file>` against a scratch-directory output) are not
evidence runs and must not write under `$NATEMU_RAW_ROOT`.
