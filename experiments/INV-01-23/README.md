# M23-L1 lock-holder instrument — code author return

This implements the accepted bounded method in
[INV-01-23](../../docs/research/investigations/INV-01-23.md). It is research code,
not an emulator scheduling policy. `/root/ordering_native_review` changed from
method reviewer to implementation author; a different reviewer must perform
full code/pre-execution review. No native scheduling campaign has run.

`producer.c` has one controller (the initial thread), N workers including
owner0, and H private-arithmetic helpers. All synchronization is process-local
pthread mutex/condition state plus an atomic helper-stop flag. Every actor owns
an external append trace; checked complete writes precede publication, and
separate receiving observations establish that publication actually occurred.
The parent reads complete prefixes while the process is alive and retains
partial suffix bytes after termination. CPU/QoS records describe requested QoS
and instantaneous CPU numbers; they establish no core type or placement control.

`check.py` reconstructs exact actor/epoch operations and checks each control
independently of producer exit/status labels. `run.py` fixes the schedule, builds
from a retained source snapshot, re-executes that snapshot, supervises the
whole campaign and each native child, and keeps all outcomes outside Git.
A successful child checker is not a complete campaign or scientific acceptance.
Offline consumers must import `validate_campaign` from the retained snapshot;
it requires the final successful outer record and recomputes every child result.
No source hash, HEAD, clean-tree state or review prose is an authorization parser.

## Exact scope

There are 216 positives: N=1/2/4/8 × trylock/yield/park × UI/background ×
H=0/C/2C × repetition identities 23001–23003. C comes from `hw.ncpu`, must be
1..64, and cannot change. Schedule seed23000 shuffles only those positives.
Then append seven controls in the following order, each at N=2/4/8 then ascending
repetition identity, background/H=C: held, missing-yield, stale-ack, lost-wakeup,
placement, ordering, frozen. All use park except missing-yield, which uses yield.
There are exactly279 planned children and no retries or reduced-sample mode.

Each child has20 epochs. Every epoch is checked for correctness; epochs0/1 are
warmups solely for any future qualified timing summary. Injection occurs once
at epoch2, owner0 for held/ordering/lost-wakeup and peer1 otherwise. N=1 has no
peer event or contention inference. All outcomes retain their exact cell ID,
run/build identity, inputs and actual API results.

The owner retains the target mutex during the controller's intentional100ms
release delay. Peers publish ready independently of the target lock. Release,
owner receipt/unlock, exact protected epoch/update counts and observed peer
acknowledgements gate the next epoch. Missing-yield waits for peer1's actual
EBUSY observation before release; omission increments only on that real branch.
Stale-ack and ordering alter actual shared values, and ordinary common checks
terminate the native process on their observed contradiction. Lost-wakeup calls
`pthread_cond_timedwait` unconditionally after observing a true release predicate,
then retains its real0/ETIMEDOUT return and checked CLOCK_REALTIME+250ms deadline.
A missing return is unresolved, not successful fault exposure. Frozen-peer
publication is observed by the controller only after that peer releases the
controller mutex through its owned wait path. No timeout proves OS preemption
or loss of a correct OS notification.

Normal condition loops retain their first real call/return pair per actor and
epoch, plus checked aggregate attempted/returned counts at completion. Every
API error is retained and stops the child. Repeated spurious returns therefore
do not create an unbounded trace. The special lost-wakeup call/return and frozen
wait witness remain separate records. Retry/yield counts are retained at phase
boundaries, never once per spinning iteration. Direct Mach timestamps produce
separate release→receipt and receipt→unlock fields in ticks, explicitly
unqualified. The first is a cross-thread diagnostic. No C1 batch precision,
calibrated latency, T2/T3 threshold, guest warning or scheduling bound is inferred.

## Bounds and stopping

Native children have30s whole-child and5s without a new eligible worker/controller
phase transition. Helpers, retries, repeated records, byte activity and incomplete
lines never reset progress. The outer process group covers retained build,
metadata, all children and checking under10,800s and1GiB recursive-output stops,
with2GiB free-disk reserve. Polling is50ms; operational overshoot is retained and
cannot produce successful finalization. Each preparation command has a10s cap.
Native trace/stdout/stderr files have a kernel RLIMIT_FSIZE ceiling of32MiB each.
On failure, kill/reap before final retention; stop all remaining cells and retain
complete/interrupted/not-run states. Expected held/frozen timeout requires its
specific complete cause witnesses, not the generic whole-child timeout.

Maximum child actors:137. The native actor array includes two20-counter arrays
per actor; actor buffers plus one2048-byte formatter per actor remain below
256MiB by explicit arithmetic. Thread stacks and runtime memory are additional;
this is not a total RSS guarantee. The consumer independently caps complete
records at600 for controller,200 for owner,300 per peer and8 per helper, with
2048-byte record/partial-suffix limits. At8 workers/128 helpers, even padded
raw buffers total at most8,316,928 bytes, below8MiB; malformed larger files are
rejected before reading. The counts include first wait pairs, summaries, QoS/CPU,
create/join and control records. Complete JSON objects add ordinary parser memory.

One independently prepared replica would use the same279 inputs and limits;
the combined method allocation remains558 children,21,600 stage seconds and
2GiB output thresholds. It needs separate preparation review and sequential
native execution. Unavailable CPU/QoS cells are retained without substitution.

## Preparation and proposed command

Compilation and fabricated/dummy validation are permitted before code review:

```sh
python3 -B experiments/INV-01-23/tests.py
```

The thirteen tests exercise all seven control predicates at2/4/8 workers, positive
N=1/2/4/8 traces, exact schedule, absent/wrong witnesses, corrupt identity/time/
lifecycle/coverage, helper/duplicate progress, kill-surviving complete prefixes,
incomplete suffixes, required outer completion, dummy process-group cleanup and
byte exhaustion. These tests never invoke the native `liveness` executable.
Retained builds rerun twelve tests; the separate process-group dummy test stays
in author/reviewer validation so it cannot escape an enclosing campaign group.
Other build-test temporary files remain beneath the retained build directory.

The L1-C1–C4 return adds exact predicate/API fields and per-actor prefix
validation shared by progress and final checking. Final checking also requires
the actual publisher/operation behind each receiving observation, including
incomplete control epochs. Live scans use local actor order, with completed
exit evidence for joins; they do not sort cross-thread timestamps. Invalid
transitions stop supervision before renewing progress. Held/frozen acceptance
requires the parent's recorded SIGKILL request, actual SIGKILL exit, and sole
no-progress cause. Resource, total-time and unrelated signal outcomes reject.
Fixtures cover the original nine counterexamples, invalid join/future-ack
progress, missing causes for every control, and both placements of a compressed
first ordinary wait around the frozen branch. The checked realtime clock
identity is tied to the retained SDK `_time.h` and a compile-time assertion.

After a different reviewer clears the complete code and root confirms the native
slot is free, the proposed first campaign command is:

```sh
NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw python3 -B experiments/INV-01-23/run.py \
  --directory /Users/liaozping/orca/natemu-raw/m23-run-20260905-track01-a \
  --no-other-native-benchmarks
```

That output path must be absent. No native execution is authorized by this README.
The helper arithmetic, synthetic host mutex and forced availability constraints
leave real guest spinlocks, quiescence, RCU/soft-lockup warnings, lifecycle,
free-running clock comparability and P1/P2 product applicability unresolved.
