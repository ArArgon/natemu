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
| Wakeup | {`mach_wait_until`, `nanosleep`, `dispatch_source` timer with leeway 0, `dispatch_source` timer with `DISPATCH_TIMER_STRICT`} × {100 µs, 1 ms, 10 ms} × {user-interactive, background} QoS. 2000 samples per arm. Reports min/p50/p99/max of (wake − deadline) and a count of samples that returned before the deadline. The strict arm is required because the cited man page documents leeway 0 as leaving "the lower limit ... under the control of the system" and names `DISPATCH_TIMER_STRICT` as the documented opt-out; without it a threshold miss attributed to dispatch would be confounded with an unexercised mitigation. Each arm also emits its own dispatch setup cost and a timebase-conversion control measured on `CLOCK_UPTIME_RAW`, which needs no timebase arithmetic. |
| Cross-thread events | {`pthread_cond_signal`, `dispatch_semaphore_signal`, `pthread_kill(SIGUSR1)` to a **spinning** thread} × {user-interactive, background}. Each sample also records a **round trip** measured entirely on the sender's clock, because the one-way figure subtracts two threads' timestamps and no source in the inventory states that the clock is a system-wide comparable time base; half the round trip is reported as the assumption-free upper bound on one-way latency. 2000 samples per arm. The `pthread_kill` arm measures one documented per-thread signal mechanism against two parked-thread wake mechanisms. Within the mechanism set this investigation surveys it is the only member that reaches an executing rather than a parked target (`CLM-01-07-18`); the source set does not establish that it is the only such public mechanism overall, and no claim says so. |
| Concurrent wakeup | `mach_wait_until` at a 1 ms target, user-interactive, with 2, 4, and 8 concurrent timer threads, 2000 samples per thread. The declared guest requirement is a 1 kHz tick *per vCPU* at 2/4/8 vCPUs, so a single-thread arm cannot speak to the aggregate rate. Samples are pooled across threads: reporting per-thread separately would be a placement statement, which `CLM-01-07-08` forbids. |
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

## Fixed sample counts

`--n` scales the wakeup, signal, and concurrent-wakeup arms. These counts are
fixed in the source and unaffected by it, so a reduced-count development run
does not reduce them:

| Arm | Fixed count |
| --- | --- |
| Clock read cost | 200 batches x 1000 reads per clock |
| Clock granularity and monotonicity scan | 200 000 reads per clock |
| Cross-thread clock | 4 threads x 200 000 timestamps |

## Per-rule checker validation

`--negative` reaches only part of the checker, so the fixtures under `tests/`
cover the rest (`REV-045` required change 5). Every integrity rule is
validated by exactly one of them, and `validate.sh` fails if any fixture
passes:

| Rule | Validated by |
| --- | --- |
| `I0` experiment identity | `tests/negative-coverage.json` |
| `I1` timebase | `tests/negative-coverage.json` |
| `I2` exact clock coverage | `tests/negative-coverage.json` |
| `I3` single-thread monotonicity | `tests/negative-coverage.json` |
| `I4` cross-thread monotonicity | `--negative` mode |
| `I5` exact wakeup-arm coverage | `tests/negative-coverage.json` |
| `I6` no wake before deadline | `--negative` mode |
| `I7` nanosleep not short | `--negative` mode |
| `I8` wakeup sample size | `--negative` mode (reduced-count run) |
| `I9` exact signal-arm coverage | `tests/negative-coverage.json` |
| `I10` signal latency non-negative | `--negative` mode |
| `I11` no lost cross-thread events | `tests/negative-coverage.json` |
| `I12` signal sample size | `--negative` mode (reduced-count run) |
| `I13` exact concurrent-wakeup coverage | `tests/negative-coverage.json` |
| `I14` no concurrent wake before deadline | `--negative` mode |
| `I15` concurrent-wakeup sample size | `--negative` mode (reduced-count run) |
| `I16` timebase arithmetic agrees with an independent clock | `tests/negative-coverage.json` |
| `I17` one-way latency within the sender-only round trip | `tests/negative-coverage.json` |

## Checker

`src/check.py result.json` separates two kinds of judgement:

- **Integrity rules (`I0`–`I17`)** detect a defective measurement; any failure
  exits non-zero. They cover experiment identity, timebase sanity, arm
  coverage, clock monotonicity within and across threads, no wake before a
  requested deadline, no short `nanosleep`, sample size, non-negative delivery
  latency, lost events, the timebase-conversion control, and the round-trip
  bound on one-way latency.
- **Decision thresholds (`T1`–`T4`)** are the predeclared `KA-05` bars from the
  investigation, each printed with its derivation. They are reported as
  MEET/MISS and never fail the checker: a MISS is a finding about the host that
  the result gate adjudicates, not a code defect. They are suppressed entirely
  when any integrity rule has failed, so no threshold is ever reported MEET on
  a value from a defective run. `T1` is restricted to the monotonic clocks,
  because `CLM-01-07-11` is a claim about a monotonic clock and the settable
  wall clock must not be able to satisfy it. `T3` is evaluated on
  `roundtrip_p99_ns` against 200 µs, the quantity the record predeclares: the
  one-way p99 is printed beside it but is not a threshold, because it
  subtracts two threads' timestamps and no source in the inventory states that
  the clock is a system-wide comparable time base.
