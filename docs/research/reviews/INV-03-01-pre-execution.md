# INV-03-01 pre-execution reviews

## Host compiler-channel spin budget — November 7, 2026

**Decision:** whether reducing the host's 30-µs reply spin saves CPU without a
repeatable elapsed-time penalty on M4. FEX's 20-µs request spin and the resident
remain fixed. This does not decide a server-spin change or explain the earlier
capacity installer difference.

Root authored the four constant-only snapshots and retained runners in
`natemu-raw/compiler-spin-20261107-01`. Independent reviewer
`/root/review_vector` inspected the source, wait4 accounting, CPU controls,
builds, signing, benchmark validation and bounded method. The
[review record](../../../../natemu-raw/compiler-spin-20261107-01/independent-preexecution-review.json)
and [method](../../../../natemu-raw/compiler-spin-20261107-01/method.json) retain details.

**Evidence validity: accepted before VM execution.** Six author and six
independent subprocess controls cover sleep, direct CPU, reaped grandchild CPU,
nonzero exit, interrupt and timeout/kill. The runner reaps the actual VM with
wait4; it adds no wrapper process and does not add compiler usage twice. Every
cell must have valid usage, guest verification, immutable inputs and no foreign
VM. Wall read brackets remain in the records. Whole-launch CPU cannot be
attributed separately to cold and warm phases.

Preflight corrections: the reviewer caught an import before the benchmark
docstring, preventing startup; the import was moved below the docstring and `--help`
passed. Root caught shared Cargo output reuse by identical variant hashes;
those artifacts are retained as rejected build v1. Isolated target directories
produce four distinct variants. All 448 source files match merged main except
their declared spin constants; signed binary identities were independently
checked. No VM measurements preceded these corrections.

**Decision relevance: accepted within the bound.** Thirty-six counterordered
screening boots select at most one candidate, with practical absolute-second
thresholds in the method. They are not fully position balanced and cannot accept
an optimization. If candidates are indistinguishable, prefer the higher spin
budget. At most 40 fresh ABBA/BAAB confirmation boots and eight independent
repetitions follow. Preserve and investigate invalid cells; no claimed speedup
from CPU savings alone. Findings review remains required.

### Normal-CLI applicability control

The screen and eight independent repetitions completed with valid results and no
qualifying candidate. During findings review, `/root/review_vector` identified
that building CLI and driver together unifies the `testing` feature. Those 44
results retain that qualification. They are not exact normal-CLI artifact timings.

Root added eight normal-CLI controls (52 total boots, within the original maximum
of 84); the untriggered 40-boot candidate confirmation remains unnecessary. This
checks the specific applicability question, rather than accepting a build-feature
assumption. The reviewer authored `independent/prod-build.py` and
`independent/prod-replicate.py`; root inspected both before execution and accepted
their evidence validity and decision relevance. Each 30/10-µs variant has a
separate target directory and builds only `natemu-cli`; the feature tree must
exclude `testing`. The signed 30-µs CLI must exactly match the retained normal
capacity CLI. Runtime/FEX are unchanged. Two ABBA blocks cover build N4/2 GiB
and N10/8 GiB, with the existing immutable inputs, CPU accounting, serial checks,
foreign-VM rejection and exclusive lock. New results stay separate under
`production-runs`; the producing scripts and
[method amendment](../../../../natemu-raw/compiler-spin-20261107-01/normal-cli-method.json)
are pinned. Any build mismatch or invalid cell stops the control for investigation.

## Loaded I/O and UART repair — November 8, 2026

Independent reviewer `/root/io_scope_review` inspected the production UART/timer
change, focused fixtures, console-drain correction and diagnostic methods in
`natemu-raw/io-load-repair-20261108-01`. These are source/method judgments;
final selected-build qualification and independent execution remain pending.

The UART policy preserves acceptance of every THR write, including REP output.
Its bounded 16-byte host staging buffer spills synchronously; it is explicitly
not a physical FIFO overflow model. THRE/TEMT/THRI readiness is delayed after the
last accepted byte/spill. Polling services due events and a registered timer can
publish readiness without further guest accesses. Pending work owns an IRQ Busy
guard through publication. FCR/mode transitions preserve already accepted output;
loopback remains separate. First sink errors remain observable. Shutdown disables
further writes/IRQ publication and drains staging before results are inspected.
The stop flag and shutdown are serialized under the device mutex, so concurrent
halt callers cannot return before the first drain. Registration failure/drop
retains timer teardown. Arbitrary blocking sinks still preclude a universal
bounded-shutdown claim.

The 50-us candidate and isolated 20-us experiment are accepted for bounded tests,
not as universal scheduling guarantees. Timer `GAP_US=20` spaces timer-thread
turns only: register accesses can service already-due events without that spacing.
Shortening readiness may expose another readiness edge within a guest burst
interrupted by host scheduling. Both payload sizes must pass exact output/hash,
error, S5 and the common finite per-CPU heartbeat-gap criterion of at most two
seconds. `serial-oracle-05` tests the 2.00/2.01-second boundary and rejudges prior
baseline/50-us observations under that same rule. Experimental inherited 50-us
prose must be updated if 20 us becomes the selected production policy.

Focused `uart-focused-oracle-02` preserves six differential launches (UART and
string fixtures, QEMU plus native default/block1), one inherited REP stress
launch and one real IRQ4/HLT launch. The latter requires 128 exact IRQs, no other
interrupt/exception counters, exact output and post-HLT progress. It measures
executed HLT/interrupt progress, not directly observed host WFI residency. Empty
event hooks and the 128-round bound correct unexecuted preparation defects.
The concurrent-halt unit test exercises draining but does not by itself force
the precise former pre-lock stop-flag race; acceptance also rests on source review.

Final matrix/soak console drains capture `stty -g` and restore those same settings.
The retained exact BusyBox source sets `STTY_require_set_attr` for recovery and
calls `tcsetattr(TCSADRAIN)` even when attributes are unchanged. Checked drain
follows the PASS marker and precedes poweroff; drain failure enters a FAIL path
with best-effort drain, without recursive failure handling. Judges still reject
FAIL after PASS. This drains guest tty bytes not yet accepted by the emulated
UART; host UART shutdown separately drains accepted staging. Workloads and
integrity rules are unchanged, but new init/input hashes must be retained.
Earlier truncated-output/RCU failures remain failures.

The normal final comparison is baseline/selected/QEMU soak pairs using identical
drain-aware inputs and workload checks, plus a selected N4 matrix pair. QEMU's
CPU/backend/durability differences prevent subtracting its loaded penalty as
emulator overhead. Independent qualification will freshly build the selected
snapshot and replay original/threefold serial payloads, soak main/restart and
native N4 under an exclusive lease, retaining failures without automatic retries.
No diagnostic timing substitutes for normal-build evidence.

The additional `lazy-kick-error-01/change.patch` is source-accepted: replacing
`ok_or(format!(...))` with `ok_or_else(|| format!(...))` avoids constructing an
unused error on successful participant lookup. The missing-vCPU error, mutex,
ended check, admitted-participant check and actual ring path are unchanged for
both ordinary kicks and I/O admission. It is included in the combined candidate;
combined timing cannot isolate its benefit without a separate experiment.

The final-build-01 freeze is source-accepted for normal comparison: its 927-entry
manifest differs from safety native-build-02 in exactly the nine reviewed UART,
registration, allocation and fixture paths. No resident/ABI/rust-vmm change is
introduced. Linux differs only in UART registration; rejected QoS02 is absent.
UART matches the isolated 20-us candidate with its inherited prose corrected.
The author reports both 20-us serial sizes passing exact output, errors, S5 and
maximum 1.04-second heartbeat gaps: 29.88/90.31 seconds versus 38.85/117.18 for
50 us. This justifies selecting 20 us for final qualification, not a universal
fairness claim or completed independent acceptance. The reviewer prepared a
fresh frozen-source build and five-VM replay recipe, without executing it before
the exclusive lease; original and threefold serial payloads, normal soak/restart
and normal N4 native stress remain the independent execution scope.

An additional primary/independent loaded-serial pair is source-accepted before
execution. Three distinct pinned stress-ng int64 workers run for 180 seconds
while the same threefold payload remains unpaced. Actual group/PID/start identity
and positive CPU progress must bracket the entire output interval, with two
pre-observations, a post-observation and consecutive samples at most three seconds
apart. Exact successful worker/metrics summaries and bounded off-wire logs are
required. The loaded-only init now performs checked TCSADRAIN after the payload
and before its completion timestamp; without that correction, worker overlap
would not cover the final guest-queued tail. Normal INIT/heartbeat literals remain
byte-identical to serial-oracle-05. This adds CPU-loaded serial coverage, not
memory pressure or universal fairness. A payload outlasting the workers is a
coverage failure. The shared 240-second cap and existing exact-output/two-second
heartbeat/error/S5 checks remain. The reviewed source is
`2ca630682d2522146bc716a9749a190c7f60418fa0e0429da5c53daeea20b7db`;
self-check, frozen inputs and actual primary/independent outcomes remain pending.
The independent allocation is now six VMs, with this extra case separately bound
from the unchanged compilation snapshot.

Before independent qualification, the fresh normal candidate pair shows similar
reads but approximately 7–8% lower writes than the fresh C3 control. Older C3
variation does not dismiss that observation. The reviewed contingency repurposes
two planned primary native-repeat slots for one unchanged C3 soak/restart pair
after the independent candidate, if that write deficit persists. This yields
baseline→candidate→independent candidate→baseline order with the same drain-aware
inputs and corrected oracle, without randomized inference. Compare all rates,
tails, fio overlap, actual stress work/CPU and whole-process time; equal stress
duration does not mean equal competing work. Retain any persistent write tradeoff.
The omitted primary N2/N4 repeats must not be represented as newly executed;
fresh independent final N4×512 and prior N2 evidence remain the native coverage.
The two CPU-loaded serial slots are retained because they test a different
interaction. No extra run beyond the declared campaign allowance is implied.

The first loaded-serial run remains rejected: cpu0/cpu2 each omit one observer
row, although surrounding gaps are 2.33/2.35 seconds with positive CPU progress
and stable identities. The reviewer independently confirmed these exact failures.
The method retains its strict consecutive-row rule instead of relaxing it after
the failure. `loaded-serial-method-03` changes only the observer and self-check:
explicit per-path AWK `getline` tolerates a disappearing unrelated `/proc` file,
but emits no row if the actual worker is missing. The extracted-body host test
covers both cases; pinned BusyBox's nonfatal explicit-getline behavior is separately
source-backed. This does not retrospectively establish why the old rows vanished.
Source hash `244d3f8b5210fe57976a0a35b7a5fcecb39f662353ea6bed03f0ea33c106feab`
is accepted for a new producing run, with unchanged payload/drain/worker/oracle
requirements. The revised independent allocation is five VMs: omit the original
unloaded onefold repetition, retain its longer threefold prefix-containing case,
soak/restart, native N4 and the corrected loaded case. Primary onefold evidence
is retained; it is not presented as independently repeated in this increment.

Root independently inspected the replica author's complete recipe before
execution: all 927 source hashes are checked before copying into a new checkout,
the target directory must be absent, normal CLI/driver/resident builds are signed
and stack-checked, and frozen serial/soak/native oracles retain their own bounds,
shared lock and failure handling. The five VM launches have distinct retained
outputs, no automatic retries and no outer deadline interrupting runner cleanup.
The inherited 2-GiB free-space floor and 4-GiB campaign retention cap apply.
Root accepts this independent preparation method; results remain pending.
