# Disposable Track 03 containment probe

Results are recorded in [INV-03-03](../../../docs/research/investigations/INV-03-03.md).
The first matrix stopped at an invalid unrestricted control; the corrected
CPU-limit-only control passed. The restored `containment-reviewed-02` matrix and
independent fresh-build `containment-replication-01` each completed all six cells.
Findings Return 15 accepts their finite compatibility/controlled-denial results,
not complete production containment. All reviewed execution budgets are exhausted;
this README authorizes no repeat or profile change.
This is not product implementation and does not modify the frozen Track 01 program.

Preparation only (build retained snapshot; no signing/sandbox/VM):

```sh
NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw python3 experiments/track03/containment/run.py --name containment-prep-01
```

The separately approved one-control diagnostic used (authorization now exhausted):

```sh
NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw python3 experiments/track03/containment/run.py --name containment-cpu-limit-control-01 --execute --control-only
```

Existing names and repository-contained raw roots are rejected. The runner copies
the six producing files (including launch.c and the separate amended profile)
and the frozen retention helper into a fresh snapshot,
checks runner/helper equality, imports the retained helper, builds from retained
C source, and records host/SDK/toolchain/signing/commands, outputs and checksums.
It uses Python's standard library and Apple's system APIs only.

`worker.sb` is the exact profile used in the accepted finite fixture; it is not
a validated production capability policy. No App Sandbox inheritance,
extra debugging entitlement, arbitrary path parameter or automatic policy
widening is used. HVF is dynamically loaded so restriction-before-loading and
setup-before-restriction remain distinct cases. Build links only libSystem and
libsandbox. Own disk/channel descriptors are intentionally inherited; all other
nonstandard descriptors are closed by `Popen`.

The completed diagnostic used one unrestricted worker. Each separately reviewed
full matrix used six fresh workers, two repetitions of control/before/after, at most one worker
and one harmless peer concurrently. One 16-KiB native guest and one vCPU per
worker; only MOV/HVC executes. The peer handles SIGUSR1 with an async-safe marker.
No user disks, external network endpoints or guest-controlled inputs are used.
The loopback listener and socketpair are per-cell temporary fixtures.

Worker timeout is 20 seconds; execution phase 60 seconds for control-only (240 for a separately approved full matrix); peer readiness 5
seconds. VM workers require recorded inherited unlimited CPU limits and never call the
CPU-limit setter; the first failed run repeated the setter excluded by Track 01
guidance. Peer CPU remains 10 seconds and build CPU 60. No core dumps, file size
8 MiB and 64 FDs. Guest/profile/entitlements remain unchanged.
Finite diagnostic source emits at most 128 Mach-port entries plus fixed records;
port names/types are diagnostics, not a complete authority inventory. Runner
retains partial/failure output and kills/reaps owned process groups. A restricted
HVF failure is retained and the fixed after-setup cell still runs; invalid control,
profile initialization, outer sandbox, worker timeout or total budget stops the
matrix. There is no retry or widening policy.

`--self-check` runs only synthetic assessment cases, with no sandbox/VM calls.
The assessment distinguishes baseline denial, missing data and operation success;
it never declares complete containment. Accepted findings apply only to the
retained reviewed runs; new decision-evidence runs require applicable review.
Task-port denial without a successful control supplies no custom-policy evidence.
Timer tests cover configuration API access only, not expiration/delivery. This
probe does not establish arbitrary Mach-service denial, full capability inventory,
platform-wide enforcement, storage power-loss behavior or codegen correctness.


The `--launch-audit` mode received pre-execution Return 5 acceptance and ran once
as `containment-launch-reviewed-01`, with all three cells completing. Independent rebuild
`containment-launch-replication-01` matched; findings Return 17 accepts the finite
observations and conditional initialization consequence. Both execution budgets
are exhausted. Scope: ordinary
CONTROL, ordinary BEFORE, sanitized BEFORE, once each; execution phase cap 100
seconds. It adds a SETEXEC launcher and child launch-state readback while retaining
the guest/profile/entitlements. READ task acquisition is classified separately
from CONTROL. See INV-03-03's concrete submission for the method, finite limits,
retained build-only preparation, accepted method and claim 39's scoped accepted findings.


`--task-filter` received independent Return 7 acceptance and ran once as
`containment-task-filter-reviewed-01`. It stopped intentionally before amendment:
original-profile CONTROL self calls failed while READ succeeded. Findings Return 19 accepts only the finite baseline distinction and intentional
inconclusive stop; no amended-policy effect is established. Its execution budget
is exhausted. Its maximum three-cell scope is:
sanitized unrestricted, sanitized original profile, sanitized amended profile.
All four self-target API/direct-entry acquisitions must succeed in both first
cells or it stops inconclusive before the amendment. Guest, base profile,
entitlements and launcher remain unchanged. See INV-03-03's concrete filter
submission and claim 42's retained inconclusive stop. No baseline widening.


New manifests use schema `natemu.track03.containment.v2`. Prior unversioned raw
manifests are preserved historical formats and are not rewritten.


READ-only preparation now adds `--read-filter` and schema v3, with a separate
`worker-read-filter.sb` that appends only BSD syscall 539 denial to the base.
The existing two-entry profile remains unexecuted. Exact CONTROL Mach 5 denial
is a regression expectation; only READ has an original-profile positive control.
Pure predicates `read_cell_ok` / `assess_read_filter` enforce launch, HVF,
resource and self-facility positives as well as exact READ rc/errno/right results.
The synthetic self-check runs no native binary. The fresh build-only snapshot
`containment-read-filter-preparation-02` requires independent root review before
any separately authorized run. No current execution budget is available.


The separately authorized `containment-read-filter-reviewed-01` run completed
once (three cells, 2026-09-20 09:21:00–03 UTC). Its finite READ attribution result
is submitted as INV-03-03 claim 43, pending independent findings review. All
49 retained hashes and eight reviewed source inputs matched. The execution
budget is exhausted; no retry, replication or profile change is authorized.


Root findings Return 20 now accepts claim 43's finite result with memory's
independent rebuild replication `containment-read-filter-replication-01`
(09:23:19–22 UTC). Root verified both 49-file hash sets, eight inputs and matching
raw outcomes. Both budgets are exhausted. Remaining guard coverage and production
authority stay open; the next 30-minute source/design action is only proposed.

Mach-96 preparation adds `--mach96-filter`, schema v4, and a separate profile
with only trap 96 denial. The fresh `containment-mach96-preparation-01` snapshot
contains compiled binaries/disassembly but no signing or execution. Its two
null-target output-word calls never treat sentinels as rights. Exact baseline
copyout and amended suppression, writable-word checks, cell identity and worker
positives are required. Existing peer diagnostics retain their earlier scope.
Root code review is pending; no runtime budget exists.


The separately authorized `containment-mach96-reviewed-01` completed once at
10:01:54–56 UTC. Baseline copyout controls passed; the amendment preserved both
sentinels but returned 53, so the reviewed exact-5 oracle did not pass. Claim 46
records the raw result pending review; no oracle relaxation or retry occurred.
All 52 hashes and nine inputs matched. The run budget is exhausted.


Separate build-only preparation `containment-mach96-denied-preparation-01` adds
`--mach96-denied-filter` / fixed-denied53-v1 under schema v5. Only run.py changes;
probe/profile/launcher/signing stay identical. Legacy --mach96-filter still
requires exact 5. Both methods retain identical baseline/copyout/positive checks.
The new method predeclares amended exact 53; no arbitrary expected-return option
exists. Synthetic cross-method rejection passed. Root review is pending and no
runtime budget exists; the old inconclusive run is not rescored.


The separately authorized fixed-53 primary completed once at 10:17:04–06 UTC,
`containment-mach96-denied-reviewed-01`; claim 48 submits its finite oracle result
for independent review. All 52 hashes and nine inputs matched. No replica or
additional run is authorized by that completed budget; the old exact-5 failure
remains unchanged.


`--iomain-filter` is new **preparation only**, schema v6. It adds one separate
`worker-iomain-filter.sb` rule for `syscall-mig` / `host_get_io_master`, three
fresh sanitized workers with unrestricted/base/amended comparison, explicit
non-root EUID admission and acquired-send-right cleanup before unchanged HVF.
No runtime budget is active. Preparation02 retained 31 hashes/10 producing
inputs, compiled both binaries and passed retained synthetic checks; no codesign,
sandbox or VM invocation occurred. Preparation01's metadata denial is preserved.
See INV-03-03 claim54 for exact limits and predeclared oracle. Nonzero return/no
right is an observation requiring error/findings review, never automatic policy
acceptance. `iomain_cell_ok` / `assess_iomain_filter` reject invalid baselines,
cell identities, cleanup failures and missing required positives. Historical
modes and raw snapshots are preserved.
