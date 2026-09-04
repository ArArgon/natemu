# INV-01-07 — host clock, timer, and cross-thread event probe

What it measures, for `Q-01-07` and kill assumption `KA-05`. Method:
[`docs/research/investigations/INV-01-07.md`](../../docs/research/investigations/INV-01-07.md),
"Method" and "Metrics". Manifests: `docs/research/manifests/MAN-01-07-*.md`
(none yet — no evidence run has been performed).

This is a capability probe. It compares against no baseline, produces no
performance result, and selects no design.

## Arms

| Group | Arms |
| --- | --- |
| Clocks | `mach_absolute_time`; `clock_gettime_nsec_np(CLOCK_UPTIME_RAW)`; `clock_gettime` with `CLOCK_MONOTONIC_RAW`, `CLOCK_MONOTONIC`, `CLOCK_UPTIME_RAW`, `CLOCK_REALTIME`. Per clock: `clock_getres`, read cost (p50/p99 ns over 200 batches of 1000 reads), smallest observed forward step, and backward-read count over 200 000 reads. |
| Cross-thread clock | 4 threads × 200 000 timestamps taken under one mutex; counts inversions and the worst backward step. Routed here by `BP-01-12-start` as the overlap-evidence comparability question. |
| Wakeup | {`mach_wait_until`, `nanosleep`, `dispatch_source` timer with leeway 0} × {100 µs, 1 ms, 10 ms} × {user-interactive, background} QoS. 2000 samples per arm. Reports min/p50/p99/max of (wake − deadline) and a count of samples that returned before the deadline. |
| Cross-thread events | {`pthread_cond_signal`, `dispatch_semaphore_signal`, `pthread_kill(SIGUSR1)` to a **spinning** thread} × {user-interactive, background}. 2000 samples per arm. The `pthread_kill` arm measures one documented per-thread signal mechanism against two parked-thread wake mechanisms; the source set does not establish that it is the only public running-thread interruption mechanism. |
| Affinity | `thread_policy_set(THREAD_AFFINITY_POLICY, tag=1)` return code, recorded so the P/E placement stratum question (`Q-05-06`) has a platform fact rather than an assumption. |

Core placement is not directly requested anywhere. QoS is the documented
lever, so the two QoS arms *sample* placement; they do not control it, and no
output of this probe is a placement observation.

## Build and run

```sh
make BUILD=/tmp/inv0107            # build outside the repo
export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw
./run.sh                           # evidence run
./run.sh --negative                # fault-injection run; the checker must FAIL
```

`run.sh` refuses to start unless `NATEMU_RAW_ROOT` is set, is an existing
directory, and lies outside the repository. It builds into the run directory
and writes `result.json`, `environment.txt`, `check.txt`, `build.log`, and
`sha256sums.txt` under `$NATEMU_RAW_ROOT/INV-01-07/<UTC>-<git short rev>/`.
Nothing is written under `experiments/`.

Expect roughly 30 minutes of wall time at the default 2000 samples per arm;
the 10 ms background arms dominate. `--n` lowers the sample count for
development only — the checker fails any arm below 2000.

## Negative control

`--negative` injects two defects: every wakeup sample is credited with a
quarter of its target interval (so deadline waits appear to return *before*
their deadline, which is impossible), and cross-thread delivery latency is
negated. `src/check.py` must report `RESULT: FAIL` and exit non-zero. A
checker that passes a `--negative` run has not been validated.

## Checker

`src/check.py result.json` separates two kinds of judgement:

- **Integrity rules (`I*`)** detect a defective measurement; any failure exits
  non-zero. They cover timebase sanity, clock monotonicity within and across
  threads, no wake before a requested deadline, no short `nanosleep`, sample
  size, non-negative delivery latency, and lost events.
- **Decision thresholds (`T*`)** are the predeclared `KA-05` bars from the
  investigation. They are reported as MEET/MISS and never fail the checker: a
  MISS is a finding about the host that the result gate adjudicates, not a
  code defect.
