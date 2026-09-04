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
  third-party dependency. Output is **newline-delimited JSON**, one
  self-contained object per line (a `"header"` line, then one line per
  check), not a single giant object: `RLIMIT_DATA`'s 256 MiB `memset` and
  `RLIMIT_CPU`'s busy loop are the hazards most likely to kill this process,
  and each line is flushed as soon as it is complete, so a process death
  partway through leaves a file of N valid lines rather than one truncated,
  unparseable object. Every `getrlimit`/`setrlimit` call site checks and
  records `getrlimit_before_ok`/`getrlimit_after_ok`, `soft_before`/
  `hard_before`, `set_ok`/`set_errno`, and the read-back `soft_after_set` —
  for `RLIMIT_NOFILE`, `RLIMIT_CPU`, `RLIMIT_AS`, and `RLIMIT_DATA` alike —
  so an `EINVAL` from `setrlimit` is interpretable rather than a bare
  boolean. An unrecognized flag exits 2 with a usage message, and every line
  carries `"experiment":"INV-01-14"`/is keyed under its own `"check"` name
  so this experiment's output cannot be interchanged with `INV-01-09`'s.
- `src/check.py` — Python 3 standard-library checker. Reads the NDJSON
  output line by line (a line that fails to parse is dropped, not raised),
  merges the checks, and reports any missing check by name rather than
  crashing on a truncated file. Validates a probe output against the
  invariants a truthful run must satisfy; used both as the negative-control
  check and, later, as an independent sanity check on Observed results (it
  does not itself decide claim acceptance). It asserts
  `rlimit_nofile.emfile_observed` (the descriptor-enforcement metric this
  record's Metrics table declares) and does not assert a specific outcome
  for `RLIMIT_AS`/`RLIMIT_DATA` enforcement, which is this investigation's
  own open question (`CLM-01-14-10`).
- `build.sh [build-dir]` — compiles `src/probe.c` into the requested build
  directory and ad-hoc signs it (no entitlement is needed for any check
  here). `run.sh` places that directory under the external run artifact.
- `run.sh` — the evidence run. Refuses without `NATEMU_RAW_ROOT` set to an
  existing directory outside the repository, and refuses unless the tree is
  committed; writes nothing under `experiments/`. Produces a normal run and
  a `--negative` control, a `check.py` pass/fail record for each, and an
  environment capture (`sw_vers`, `sysctl`, `clang --version`,
  `git rev-parse HEAD`, `codesign -d --entitlements -`, SDK path), all under
  `$NATEMU_RAW_ROOT/INV-01-14/<UTC-timestamp>-<git-short-rev>/`, plus a
  `SHA256SUMS.txt` of every file written. The negative arm is scored as a
  demonstrated fault injection only when the checker's exit status is
  exactly `1` and its output reports at least one violated rule.

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

## RLIMIT_CPU restore order

The probe restores the rlimit before restoring the default `SIGXCPU`
disposition: while the process would still be over a 1 s soft limit with the
default (terminating) disposition installed, the kernel could re-raise
`SIGXCPU`. Restoring the limit first removes that window.

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
evidence runs and must not write under `$NATEMU_RAW_ROOT`. `./validate.sh`
runs the fixture-based preflight check without touching the raw root.
