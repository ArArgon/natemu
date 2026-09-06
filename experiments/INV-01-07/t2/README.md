# M07-T2 proposed timer/event return

Instrument prepared 2026-09-05. The first full campaign completed; its live
research status and pending replication/findings review belong to the investigation. The owning
[investigation](../../../docs/research/investigations/INV-01-07.md#timer-stage-m07-t2--proposed-pre-execution-return-2026-09-05)
contains the method, fixed matrix, controls, budgets and unresolved qualification.
Independent pre-execution review accepted this method/code before first collection.
Any changed method/code needs its applicable independent review.

`producer.c` retains raw rows; `deadline.swift` supplies an opaque dispatch token
and its public represented uptime deadline from one `DispatchTime` value.
`check.py` independently derives distributions and achieved cadence.
`runner.py` builds the retained snapshot in a fresh external campaign directory,
then executes that snapshot under a process-group supervisor. It reuses the
unchanged C1 `clock_evidence.py` solely for factual clock-packet context checks.
Checksums identify input/output bytes; they neither parse nor grant review approval.

The proposed command, only after separate pre-execution clearance and confirmation
that no other native benchmark is active, is:

```sh
NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B experiments/INV-01-07/t2/runner.py \
  --directory /Users/liaozping/orca/natemu-raw/t2-run-20260905-track01-a \
  --clock /Users/liaozping/orca/natemu-raw/c1-run-20260905-track01-a/campaign.json \
  --no-other-native-benchmarks
```

The output path must be absent. There is no retry, reduced-count or replacement
mode. The fixed schedule is normal, dispatch-early, rtt-zero, cadence-duplicate.
An unexpected exit/oracle, timeout or budget exhaustion preserves partial output
and stops all remaining modes. A native negative exits zero after producing the
intentionally defective raw row; only rejection by its specific independent
checker predicate qualifies that control.

For build-free synthetic validation:

```sh
python3 -B experiments/INV-01-07/t2/test_check.py
python3 -B experiments/INV-01-07/t2/test_runner.py
python3 -B experiments/INV-01-07/t2/test_signal.py
python3 -B experiments/INV-01-07/tests/rev184-regressions.py
```

Offline campaign derivation must import `runner.validate_campaign` from the
campaign's `producing-build/snapshot`, not the live checkout, and set
`NATEMU_RAW_ROOT` to the retained external root. It requires the
successful outer supervisor and recomputes the raw distributions and exact
control failures. `check.py` alone is a raw-structure check; it cannot establish
a complete campaign, producing-build identity, clock applicability or evidence
acceptance. Native memory/timing behavior is not tested by these Python fixtures.

The old `src/timers.c` remains historical. T2 does not rewrite its raw evidence,
reconstruct missing dispatch deadlines or convert nominal rates into observations.

The T2-R1/R2 author return adds sequential sender/worker and empty-pair
chronology checks, including the reviewer's four destructive examples.
`test_signal.py` compiles the unchanged production flag initialization, handler
and consumer and invokes them directly; it sends no signal and reads no timer.
The handler's only static object is now its checked lock-free atomic flag.
Independent reinspection accepted these corrections before first native collection.
