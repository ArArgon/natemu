# INV-01-08 — arm64 memory-model, atomics-granularity, and cross-thread probe

What it measures, for `Q-01-08` and kill assumption `KA-06`. Method:
[`docs/research/investigations/INV-01-08.md`](../../docs/research/investigations/INV-01-08.md),
"Method" and "Metrics". Manifests: `docs/research/manifests/MAN-01-08-*.md`
(none yet — no evidence run has been performed).

This is a capability probe. It compares against no baseline, produces no
performance result, and selects no design. It **corroborates** architectural
statements cited from the Arm ARM (DDI 0487 M.c) and the Intel SDM Vol 3A; it
never establishes them. Observing no relaxed outcome bounds a probability; it
does not show that the architecture forbids one.

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
it meaningless. `src/check.py` must report `RESULT: FAIL` (rules `I1` and
`I2`) and exit non-zero.

## Checker

`src/check.py result.json` separates two kinds of judgement:

- **Integrity rules (`I*`)** exit non-zero on failure. The load-bearing one is
  `I1`, the positive control: the plain SB litmus must observe the relaxed
  outcome at least once, or the harness cannot expose weak behaviour at all.
  `I4` treats a relaxed outcome under `LDAR`/`STLR` or `DMB ISH` as a failure
  requiring investigation — either the harness is broken or the observation
  contradicts the cited architecture, and both must be resolved before any
  claim rests on the run.
- **Decision thresholds (`T*`)** are the predeclared `KA-06`-adjacent bars.
  They are reported as MEET/MISS and never fail the checker; a MISS is a
  finding the result gate adjudicates.

MP is deliberately not an integrity rule. It is far harder to expose than SB
under a lockstep barrier, and a zero count there is reported, not treated as
either a defect or a prohibition.
