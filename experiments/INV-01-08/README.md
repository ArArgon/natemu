# INV-01-08 — arm64 memory-model, atomics-granularity, and cross-thread probe

What it measures, for `Q-01-08` and kill assumption `KA-06`. Method:
[`docs/research/investigations/INV-01-08.md`](../../docs/research/investigations/INV-01-08.md),
"Method" and "Metrics". Manifests: `docs/research/manifests/MAN-01-08-*.md`
(none yet — no evidence run has been performed).

This is a capability probe. It compares against no baseline, produces no
performance result, and selects no design. It **corroborates** architectural
statements cited from the Arm ARM (DDI 0487 M.c) and the Intel SDM Vol 3A; it
never establishes them. A finite run that observes no relaxed outcome does not
show that the architecture forbids one.

## Arms

| Group | Arms |
| --- | --- |
| Litmus | Store-buffering (SB) and message-passing (MP), 2 threads, in three variants: plain `LDR`/`STR`, `LDAR`/`STLR`, and `STR` + `DMB ISH` + `LDR`; each at user-interactive and background QoS. 10^6 iterations per arm by default. Every access is inline assembly so the compiler cannot reorder, fold, or elide it; `make disasm` writes the disassembly the code gate checks. Iterations are fenced by a three-phase sense-reversing barrier so the two threads actually overlap and the scoring pass cannot race the peer's next iteration. |
| LSE atomics | `ldadd` and `cas` on one shared counter at 2, 4, and 8 threads, 10^6 operations per thread. The counter's final value is an integrity check: a lost update means the arm is broken. |
| Alignment | 32-bit `ldaddal` at an aligned address (control), at a misaligned address inside one 16-byte granule, across a 16-byte boundary, and across a 64-byte cache line; plus plain `LDR` and `LDAR` across a cache line for contrast. A `SIGBUS`/`SIGSEGV` handler with `siglongjmp` records signal number and `si_code` per case. The handler refuses to swallow any fault raised outside a probe window (`_exit(90)`), so it cannot mask an unrelated defect. |
| Fallback | Four threads doing a read-modify-write on the hardware-refused misaligned address under one global mutex, giving the cost a software fallback would carry. A single contended address cannot be helped by address striping, so striping is not measured; per-address striping is the upgrade path if a fallback ever has to cover many addresses. |
| Round trip | Cross-thread "invalidation" handshake — one thread bumps an epoch, the peer acknowledges after `DMB ISH` — as the host analogue of a cross-vCPU shootdown. 20 000 samples per QoS class. |
| Affinity | `thread_policy_set(THREAD_AFFINITY_POLICY, tag=1)` return code, feeding the `Q-05-06` placement-stratum question. |

QoS arms sample core placement; they do not control it. No output of this
probe is a placement observation.

## Build and run

```sh
make BUILD=/tmp/inv0108            # build outside the repo
make BUILD=/tmp/inv0108 disasm     # instruction-sequence evidence for the code gate
export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw
./run.sh                           # evidence run
./run.sh --negative                # fault-injection run; the checker must FAIL
```

`run.sh` refuses to start unless `NATEMU_RAW_ROOT` is set, is an existing
directory, and lies outside the repository. Outputs go to
`$NATEMU_RAW_ROOT/INV-01-08/<UTC>-<git short rev>/`. Nothing is written under
`experiments/`. Expect a few minutes of wall time at the default 10^6
iterations.

## Negative control

`--negative` runs the two litmus sides sequentially on one thread. No
interleaving is then possible, so the Arm-permitted relaxed outcome can never
appear — exactly the defect that would make every "not observed" result below
it meaningless. `src/check.py` must report `RESULT: FAIL` (rules `I2`, the
positive control, and `I3`, concurrency) and exit non-zero.

`--negative` reaches only part of the checker, so the fixtures under `tests/`
cover the rest. Every integrity rule is validated by exactly one of them:

| Rule | Validated by |
| --- | --- |
| `I1` exact litmus-arm coverage | `tests/negative-coverage.json` (arm removed) |
| `I2` positive control | `--negative` mode |
| `I3` litmus threads ran concurrently | `--negative` mode |
| `I4` litmus iteration count | `tests/negative.json` (reduced-count run) |
| `I5` no architecture-forbidden relaxed outcome | `tests/forbidden.json`, `tests/negative-coverage.json` |
| `I6` exact LSE-arm coverage | `tests/negative-coverage.json` |
| `I7` LSE atomics lose no updates | `tests/negative-coverage.json` |
| `I8` exact alignment-case coverage | `tests/negative-coverage.json` |
| `I9` aligned-atomic control does not fault | `tests/negative-coverage.json` |
| `I10` ordinary-load contrast does not fault | `tests/negative-coverage.json` |
| `I11` every fault carries a signal number | `tests/negative-coverage.json` |
| `I12` exact round-trip coverage | `tests/negative-coverage.json` |
| `I13` round trip always acknowledged | `tests/negative-coverage.json` |
| `I14` round-trip latency positive | `tests/negative-coverage.json` |

`validate.sh` runs all of them and fails if any fixture passes.

## Fixed sample counts

`--iters` scales the litmus arms only. These arms have counts fixed in the
source and are unaffected by it, so a reduced-count development run does not
reduce them:

| Arm | Fixed count |
| --- | --- |
| LSE atomics (6 arms) | 10^6 operations per thread |
| Software fallback | 250 000 operations per thread, 4 threads |
| Invalidation round trip | 20 000 handshakes per QoS class |
| Alignment | 6 cases, executed once each (the outcome is categorical) |

## Checker

`src/check.py result.json` separates two kinds of judgement:

- **Integrity rules (`I*`)** exit non-zero on failure. The load-bearing one is
  `I2`, the positive control: the plain SB litmus must observe the relaxed
  outcome at least once, or the harness cannot expose weak behaviour at all.
  `I5` rejects a relaxed outcome in any arm the cited architecture forbids:
  SB and MP under `DMB ISH`, and SB and MP under `STLR`/`LDAR`. SB under
  `STLR`/`LDAR` is forbidden per `SRC-108` (Pulte et al., POPL 2018), whose
  axiomatic model defines `bob = po; [dmb.full]; po | [L]; po; [A]` and states
  that a store-release before a load-acquire "is required to be observed in
  order by all threads". An earlier revision of this file and of the checker
  asserted the opposite on no source at all; `REV-043` and `REV-045` returned
  that, and it is corrected here.
- **Decision thresholds (`T*`)** are the predeclared `KA-06`-adjacent bars.
  They are reported as MEET/MISS and never fail the checker; a MISS is a
  finding the result gate adjudicates. They are suppressed entirely when any
  integrity rule has failed, so no threshold is ever reported MEET on a value
  from a defective run.
  `T4` is split: `T4a` uses `ns_per_attempt` (executed atomic instructions)
  and `T4b` uses `ns_per_op` (successful increments), because for `cas` under
  contention the two differ by retry amplification and charging retries to the
  instruction would misattribute the scaling.

MP is deliberately not an integrity rule. It is far harder to expose than SB
under a lockstep barrier, and a zero count there is reported, not treated as
either a defect or a prohibition.
