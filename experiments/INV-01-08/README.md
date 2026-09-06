# INV-01-08 — arm64 memory model, atomics and cross-thread probe

The [investigation](../../docs/research/investigations/INV-01-08.md) owns the
question, conclusion, limitations and next action. Follow the
[research workflow](../../docs/research/README.md) before execution.

This capability probe selects no emulator design. QoS does not control core
placement. The platform, OS, topology and SMP limitations belong to each run.

## Arms

| Group | Arms |
| --- | --- |
| Litmus | Store-buffering (SB) and message-passing (MP), 2 threads, in three variants: plain `LDR`/`STR`, `LDAR`/`STLR`, and `STR` + `DMB ISH` + `LDR`; each at user-interactive and background QoS. 10^6 iterations per arm by default. Every access is inline assembly so the compiler cannot reorder, fold, or elide it; `make disasm` writes the disassembly pre-execution reviewers inspect. Iterations are fenced by a three-phase sense-reversing barrier to coordinate iteration phases so the scoring pass cannot race the peer's next iteration; it does not measure simultaneous execution. |
| LSE atomics | `ldadd` and `cas` on one shared counter at 2, 4, and 8 threads, 10^6 operations per thread. The counter's final value is an integrity check: a lost update means the arm is broken. |
| Alignment | 32-bit `ldaddal` at an aligned address (control), at a misaligned address inside one 16-byte granule, across a 16-byte boundary, and across a 64-byte cache line; plus plain `LDR` and `LDAR` across a cache line for contrast. A `SIGBUS`/`SIGSEGV` handler with `siglongjmp` records signal number and `si_code` per case. Outside the active probe window the amended handler only calls `_exit(90)`. Within the window, an always-lock-free address guard must also match the4-byte probed range; otherwise it exits90. Expected recovery still uses the prior bounded mechanism. |
| Fallback | Four host workers, one4-byte crossing address and one mutex candidate. Historical raw lacks final-value/mutex-error oracles, so its19.06ns/op is an unqualified diagnostic, not correct-fallback cost or a floor. F08-2 retains correctness/cost testing and all guest compatibility obligations without selecting a strategy. |
| Round trip | Cross-thread "invalidation" handshake — one thread bumps an epoch, the peer acknowledges after `DMB ISH` — as the host analogue of a cross-vCPU shootdown. 20 000 samples per QoS class. |
| Affinity | `thread_policy_set(THREAD_AFFINITY_POLICY, tag=1)` return code, feeding the `Q-05-06` placement-stratum question. |

QoS arms sample core placement; they do not control it. No output of this
probe is a placement observation.


## Operation and retained inputs

Compilation, disassembly and synthetic validation do not run the native probe:

```sh
sh validate.sh
make BUILD=/tmp/inv0108-preflight
make BUILD=/tmp/inv0108-preflight disasm
```

After pre-execution review, use an existing external `NATEMU_RAW_ROOT` and a
fresh output directory whose parent already exists:

```sh
export NATEMU_RAW_ROOT=/path/outside/repository
./run.sh --directory "$NATEMU_RAW_ROOT/new-campaign" --campaign
# Optional factual clock evidence:
./run.sh --directory "$NATEMU_RAW_ROOT/another-campaign" --campaign \
  --clock "$NATEMU_RAW_ROOT/clock-campaign/campaign.json"
```

The campaign retains one source snapshot, including local checker dependencies,
and builds both normal and disassembly-verification binaries from it. The
build manifest records source checksums, exact commands/flags, toolchain/SDK,
platform and outcome. One run identity binds producing build and all controls.
README edits and unrelated commits do not invalidate retained runs.

The five children are normal, sequential-negative, omitted-store, lock-error
and unexpected-fault, with watchdog limits 600/600/60/60/5 seconds (1,325 seconds
native subtotal). An outer supervisor owns the full build/execution/checking process
group with a 45-minute wall ceiling and 128 MiB recursive output stop threshold.
It polls every 20 ms, kills the group at either limit, and reaps its direct child.
This is an operational threshold, not a filesystem quota: inter-poll output
overshoot and terminal metadata remain retained failure diagnostics. SDK/LSE
lookups each have a 10-second timeout. A failed
child stops the campaign without retry, replacement or exclusion. Raw/partial
raw, stdout/stderr, exit/timeout, invocation, environment and checksums are
retained. The final aggregate receipt records partial checksums and each of the
five modes as completed, invalid, interrupted or not-run. Inner normal output cannot
qualify evidence before the outer supervisor succeeds. Every artifact stays outside Git. Outputs are exclusive and no-follow;
canonical raw roots must neither contain nor lie inside the repository.

`--mode normal|negative|fallback-only|fallback-drop-write|fallback-lock-error|unexpected-fault`
selects a standalone child and fresh sibling build. A standalone normal run
has no complete campaign qualification. Controls never establish timing.

## Controls and interpretation

F08-2 preserves four fallback workers × 250000 iterations, a fresh four-byte
target at offset 78, neighboring canaries, checked mutex/thread API outcomes,
per-worker completion and actual-write counts, and final-value verification.
The omitted-store control must show exactly one omitted write; the lock-error
control must preserve abort/lifetime accounting. Unexpected faults must exit 90
without a normal result. Sequential negatives must fail exactly the intended
concurrency rules while preserving histogram integrity.

A new result uses `INV-01-08-result-v3`; build, invocation, supervisor,
execution manifest use explicit `v2` formats. Campaign, schedule and aggregate
supervisor use `v3`; a successful same-run aggregate receipt is mandatory. The checker verifies
all five controls, retained build and per-child run identity before reporting
campaign-qualified output. Correctness failure suppresses performance output.
Missing or mismatched clock evidence yields **timing unqualified**, while
independently justified correctness work can proceed. A factual clock-context
match is a candidate for findings review, never automatic acceptance.

```sh
python3 src/check.py /external/campaign/normal/result.json
python3 src/check.py --historical /external/original/result.json
```

Historical artifacts keep their original interpretation. `--historical` handles
only the original schema-free result, and cannot bypass new integrity checks.
Old F08-2 v2 results, v1 campaigns and cohorts require their original Git readers;
no absent approval field upgrades them. The pinned independent replication
script is historical reproduction code, not the current runner.

Histogram reporting retains exact count-weighted fractions, inclusive
[10/3, 30] indeterminate boundaries, symmetric zero cases and matching-plain
exposure. COMPARABLE alone is not corroboration; finite zero is not prohibition.
An explicit `INV-01-08-cohort-v2` JSON contains a `cohort` with id,
compatibility_justification and at least two runs, each with path, sha256,
retained manifest identity and equal context fields defined in
`src/histogram.py:CONTEXT_FIELDS`. Use `--cohort PATH`; applicability remains
findings-review work. There are no review-file or review-ID inputs.
