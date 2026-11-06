# INV-03-03 — containment pre-execution review

Reviewer `/root`, independently reviewing `/root/machine`'s September 20
containment preparation. Material: the method in INV-03-03 and
`experiments/track03/containment/{probe.c,worker.sb,entitlements.plist,run.py,README.md}`.
The inherited source/API basis is covered by findings Return 12.

## Initial review — revise

The finite matrix addresses the missing composition: the exact custom restriction
with HVF operations, preopened resources and selected peer denials. Ordinary VM
mechanisms and general sandbox documentation do not answer this. The profile is
fixed, both setup orders are represented, no user disk or external endpoint is
used, and unsupported task access remains inconclusive when its control fails.
Timer configuration is not timer delivery. Numeric Mach-port diagnostics are not
proof of complete capability isolation. Even a successful run cannot establish
full worker containment or a production allowlist.

The C source dynamically loads HVF, validates a literal MOV/HVC guest exit and
records post-restriction map/protect/unmap and destruction. The runner snapshots
build inputs, reuses the retained frozen helper without changing Track 01,
uses fresh external output and owns/kills its process groups. Source inspection
shows finite output and six workers, with per-worker and overall time bounds.

**Substantive return:** a control worker can exit zero after logging a bad
resource/value result. The runner currently stops on its process status but
only evaluates the positive-operation oracle after the entire matrix. Thus
restricted cases can run despite an invalid own-file/channel/guest control,
contrary to the declared stop rule. Evaluate mandatory positive controls and
required peer-file/signal/loopback controls before starting restricted cells;
keep baseline-denied task access explicitly inconclusive. Add a synthetic case
where the worker exits zero but the control oracle fails. Acceptance requires
that this case stops admission, not merely adds a final failure label.

Earlier drafting returns are incorporated: retain a restricted HVF failure and
continue the other fixed setup order; observe the peer signal marker; check
channel bytes; copy/import retained supervision helpers. No probe executed in
this review. Final acceptance and the exact reviewed execution scope follow
after the remaining return is resolved and self-checks are rerun.

## Return 1 — accept the finite method

The author extracted `positive_ok`/`control_ok` and calls the latter immediately
after each unrestricted cell, before admitting restricted workers. It requires
exact own-file bytes, full-sync, both channel directions, native exit/value,
HVF/timer API results and positive peer-file/signal/loopback controls. Baseline
task-port denial remains inconclusive. I inspected that call site and reran
`python3 -B experiments/track03/containment/run.py --self-check`; the synthetic
zero-exit/full-sync and zero-exit/HVC failures are rejected. The source's fixed
marker is 16 bytes including its terminator. The manifest now labels execution
as decision evidence and build-only work as preparation. The return is closed.

**Accepted execution scope:** one fresh retained six-worker matrix on the M4
macOS 26.6.2/SDK 26.4 host, with the exact reviewed source/profile/entitlements,
two repetitions of the three declared modes, one disposable peer, and the stated
20-second worker/240-second execution bounds. No policy edits, automatic retries
or other host targets are covered. An outer tool sandbox may make the standalone
policy inapplicable; record the launch context and use the tool's approval path
for a bounded unsandboxed launch if required, rather than changing the profile.
This scope is within the standing research goal; it is not product implementation.

Runtime failure must be retained and interpreted before another campaign. An
independent rebuild/repetition and findings review are still required for
load-bearing observed conclusions. Method acceptance establishes no runtime
or containment result. No C/profile change was authored by this reviewer.

## Run disposition — original scope exhausted

`containment-reviewed-01` stopped on its first unrestricted control timeout;
no restricted worker ran. The reviewed runner set `RLIMIT_CPU=(10,10)` for VM
workers. This conflicts with the applicable launch condition documented in the
current Track 01 reproduction README and INV-01-30's controlled CPU-limit
diagnosis. I had read that requirement and missed the mismatch in review.
The failure is preserved; it establishes no custom-profile behavior.

Do not reuse Return 1 as permission for a changed or repeated matrix. The next
submission is exactly one unrestricted control, changing only VM-worker CPU
limit handling to require and record inherited unlimited limits, with the same
C, profile, entitlement and wall-time/process-group controls. That diagnostic
still needs code/method review below before execution. Its result may test the
known launch dependency; the present timeout alone does not prove the cause.

## Return 2 — accept one launch diagnostic

I reviewed `child_limits(False)` at the VM-worker `Popen`, the separate unchanged
peer/build limits, the inherited-unlimited admission check and recorded limits.
`--control-only` selects one unrestricted worker and a 60-second execution phase,
with the existing 20-second worker deadline and immediate positive oracle. It
cannot fall through to restricted cases. I reran the synthetic self-check and
compared C/profile/entitlement bytes with the failed retained snapshot: all
three are unchanged. No guest initialization or policy adjustment is bundled.

**Accept exactly one fresh run** `containment-cpu-limit-control-01`, using the
same external raw root and launch context, with `--execute --control-only`.
Stop and review its outcome either way. This tests a narrow launch hypothesis;
it authorizes neither a resumed six-worker matrix nor a containment conclusion.
The source and method correction were authored by the machine agent; this
review adds no experimental code. No substantive return remains for this scope.

## Return 3 — accept the restored finite matrix

The one-control diagnostic completed with return zero, expected HVC syndrome
and X0, successful post-run unmap/remap/destruction and required positive resource
controls. I independently verified its retained checksums, outputs and unchanged
C/profile/entitlements against the failed snapshot. Its manifest records inherited
unlimited CPU limits and no setter. This is sufficient fixture readiness for the
next method step, not final acceptance of a causal diagnosis or containment.

I rechecked the unchanged six-cell path in the amended runner: two repetitions,
control before restricted cases, the same immediate oracle and stop conditions,
240-second execution phase and 20-second workers. Only the previously reviewed
VM-worker CPU-limit correction differs from the original matrix preparation.
Peer/build limits and all resource targets remain as reviewed.

**Accept one fresh matrix** `containment-reviewed-02` under the same raw root,
using `--execute` without `--control-only`. No profile change or retry is covered.
Stop after its matrix or declared failure. Load-bearing observations still require
independent replication and findings review; this is execution readiness only.

## Return 4 — independent replication scope

The restored matrix finished all six cells. I verified its retained checksums
and source equality with the reviewed worktree. The positive operations passed
in both setup orders; peer-file open, signal and new loopback connection were
denied with positive controls. Task-port access was already denied in the control
and remains inconclusive. These are submitted observations pending replication
and findings review, not accepted complete containment.

Accept one independent rebuild/matrix by `/root/memory`, fresh identity
`containment-replication-01`, with the same fixed six-cell method and limits.
Before launch, independently check each producing source and frozen helper
against `containment-reviewed-02/source`; refuse a mismatch. Build from the new
retained snapshot of those identical inputs and retain all output/failures. No
patch, profile change, additional target or retry is included. The root reviewer
will inspect the independent run and resulting scoped finding; the replication
author does not accept its own new observation.

## Return 5 — sanitized-launch comparison method

September 20, independent reviewer `/root/memory`. **Accept the corrected method
and code for the proposed one fresh `containment-launch-reviewed-01` comparison**:
ordinary CONTROL, ordinary BEFORE, sanitized BEFORE, once each, with
`--launch-audit --execute` and the stated 100-second execution/20-second worker
limits. No AFTER cell, repeat, policy widening, additional target or replacement
run is included. Execution requires the parent's applicable authorization; this
review supplies the method verdict and adds no runtime observation.

I read the concrete submission, all producing code and the retained preparation.
All **25** entries in `containment-launch-preparation-02/SHA256SUMS` match after
independent recomputation. All six producing inputs match the current worktree,
including the frozen retention helper. Preparation has `execute_requested=false`
and no cells. Reviewed input identities are:

| Input | SHA-256 |
| --- | --- |
| launch.c | `70afd07ac0e021907223481cd3b198cd6925a136b40e57facce9bd31106e4b9e` |
| probe.c | `0b703e041d12bffc2798bdbca9ef6ef311359f1695c98d6851467397706c1b67` |
| run.py | `9142e816281df116fc6691c7fcc14d5fcdfd8e05957a25c3f2979cc740e48b2d` |
| worker.sb | `5368006507e8bb4be0a547edb307417601ddbf25c6a499b335b56075da7dbbda` |
| entitlements.plist | `70d65ae13874e183526aa48e55bb330f2156da94effca24f89b5e2f940262723` |
| retention.py | `4f007c5121bc8e5cb5ec4ecf732e2a34542bd570c66daa893bb353f6249fe3dd` |

The profile/entitlements and full HVF load/setup/exercise function region are
byte-identical to `containment-reviewed-02`'s snapshot: guest bytes, register
initialization and native oracle are unchanged. The CPU-limit correction remains
intact: the SETEXEC adapter/VM worker inherit checked unlimited limits and omit
the CPU setter; build CPU60 and peer CPU10 are separate. Other child limits,
process-group kill/reap and retained-failure behavior remain. The launcher uses
the same supervised process rather than an unsupervised second worker.

**Substantive return resolved before submission:** the first draft cleared and
checked task exception ports only. XNU's inspected exec exception action targets
the task; its thread exception channel is separate, and ordinary exec is not a
general `ipc_thread_reset` guarantee. I required an explicit main-thread treatment.
The corrected launcher clears current-thread exception actions, clears registered
ports and requests NULL bootstrap/task-exception spawn actions. The probe then
checks bootstrap, registered, task-exception and main-thread-exception state
before profile or guest admission. Any query failure or nonempty sanitized
postcondition exits 26. It does not claim to clear host/task-access protection,
every Mach right, future thread state or later acquired authority.

Control ordering is fail-closed: ordinary CONTROL must satisfy the existing
positive/peer oracles and launch-query checks before the first restricted cell;
ordinary BEFORE must preserve positive operations and valid queries before the
sanitized cell. Failed profile initialization, inherited outer sandbox, bad launch
postconditions or timeout stop the run. A sanitized HVF incompatibility is retained
as failure evidence, not cured with extra permissions. Missing control evidence
does not become a policy PASS.

READ and CONTROL use distinct return conventions and record actual acquired
rights; the inspected XNU READ syscall supplies errno-style failures and SDK 26.4
exports the symbol. The selected peer keeps the sole HVF entitlement without
debugging grants. A baseline-denied READ or CONTROL query is explicitly inconclusive
for that policy claim. The fixture does not access peer memory using a returned
right or infer complete existing-right containment from query failures.

I reran only `run.py --self-check`: synthetic assessment checks pass, including
nonempty launch channels, incomplete comparison and separate positive/baseline-
denied READ controls. I did not sign or launch a binary, install a sandbox, create
a VM or execute a guest during review. No outstanding substantive return remains.
A changed source, profile, launch action or limit requires re-review; findings and
independent replication remain separate from this method acceptance.

## Return 6 — sanitized-launch independent replication, September 20, root

The author's one authorized three-cell run completed. Root independently checked
all 47 retained checksums, all six input identities against reviewed preparation
02, raw child launch-state/guest/task-query records and the assessment. The
sanitized cell reports all four tested launch channels empty and preserves the
finite positive operations. READ and CONTROL are baseline-denied in all cells;
neither supplies custom-policy task-access evidence. Ordinary exception counts
were already zero, so the result does not demonstrate clearing nonzero exception
handlers. Findings remain submitted, with no complete containment acceptance.

Root inspected the stable launcher, child readback, runner admission and
assessment paths, and the concrete method. Accept **one independent rebuild and
three-cell replication** by `/root/memory`, fresh identity
`containment-launch-replication-01`, under the existing external raw root. Before
launch, compare all six producing inputs against the original run snapshot and
the hashes in Return 5; refuse any mismatch. Build from the fresh retained copy.
Use exactly `--launch-audit --execute`, the same signing/profile/guest, inherited
unlimited VM CPU limits, 20-second worker and 100-second execution limits.
No source changes, new targets, retries or permission widening are covered.
Stop after that sequence or its declared failure. Root will review the retained
replica and scoped finding separately; this is execution readiness only.

## Return 7 — two-entry filter method, September 20, memory

**Accept the submitted bounded method/code as ready for a separately authorized
execution. No execution budget is granted by this verdict.** Root must authorize
the proposed `containment-task-filter-reviewed-01` run before it starts. This
review covers exactly `--task-filter --execute`, at most three sanitized workers
(unrestricted, base-profile BEFORE, amended-profile BEFORE), one sequence,
20 seconds per worker and 100 seconds for the phase, with no retry or widening.
Stopping after either unsuccessful baseline is the intended inconclusive result.

Independently read the concrete method, launcher, probe, runner and both profiles.
Verified all **26** retained hashes in build-only
`/private/tmp/natemu-track03-raw/containment-task-filter-preparation-02` and all
seven producing inputs against the worktree. Reviewed SHA-256 identities:

| Input | SHA-256 |
| --- | --- |
| probe.c | `33a1c53d2da1ce653d022a013613e967544074028442041a06d14220b52396dd` |
| run.py | `a211f1d3ef527b345fc2d02b6b4fdf0f7cf69ba6fd91ae8e440c3c48eab33e9e` |
| worker-task-filter.sb | `9a7e1a03394747b4e1e4394388c36f52c98a532a7c4422967a6ec1c52a781a56` |
| worker.sb | `5368006507e8bb4be0a547edb307417601ddbf25c6a499b335b56075da7dbbda` |
| launch.c | `70afd07ac0e021907223481cd3b198cd6925a136b40e57facce9bd31106e4b9e` |
| entitlements.plist | `70d65ae13874e183526aa48e55bb330f2156da94effca24f89b5e2f940262723` |
| retention.py | `4f007c5121bc8e5cb5ec4ecf732e2a34542bd570c66daa893bb353f6249fe3dd` |

The base profile, launcher and entitlements match the accepted sanitized-launch
snapshot exactly; the full HVF setup/exercise source region is unchanged. The
amended profile is the exact base followed only by denials for Mach trap 45 and
BSD syscall 539. The guest, one-vCPU geometry, inherited unlimited CPU-limit rule,
resource descriptors, process-group supervision and retention are preserved.
This does not establish that the new SBPL vocabulary parses or enforces on this
host: that is the declared question, and parsing failure stops without widening.

Admission requires launch postconditions, ordinary positive operations and all
four SDK/direct self CONTROL/READ acquisitions in **both** preceding cells. Each
must return success and a valid right, and the self task facility must work.
Failure records `task_filter_inconclusive_before_amendment` and returns before
the amended cell. A zero runner exit in that path is not evidence of enforcement.
The unrestricted peer file/signal/loopback controls remain mandatory. All cells
use sanitized launch; no change in launch context confounds the policy comparison.

The direct-entry shim supplies negative Mach trap / positive BSD numbers in x16,
records BSD carry/errno separately from Mach results, initializes the output
right and deallocates acquired rights. A review concern was resolved before
submission: ordinary task/PID values are now computed before binding fixed x0/x1,
so a intervening getpid call is not relied on to preserve a bound argument.
Retained XNU arm64 pre-dispatch filter branches support the claimed route boundary;
they do not establish the proprietary profile compiler's binding or prove every
failure code is a policy denial. The method retains exact Mach errors and requires
matched controls; READ needs EPERM/EACCES. Success, missing output, death,
profile failure, timeout and lost positive functionality cannot pass as denials.

I reran only the synthetic `--self-check`, which passed. No binary launch, task
query, sandbox application, signing or VM execution occurred in this review.
No substantive return remains. Changed inputs require affected re-review;
findings review and independent replication remain separate. Even a successful
run would establish only the measured two-entry distinction plus its stated
source argument, not all acquisition routes, held-right isolation or complete
production containment.

**Return 7 metadata-only recheck:** root identified the missing explicit manifest
format version before execution. Independently compared preparation 03 with 02:
the sole producing-source change is adding
`schema: natemu.track03.containment.v2` to the record. The new run.py SHA-256 is
`fcaa6f2e33e921512572dbf9338d29a4aae0fe93e935815cf9d1ae14da96466a`;
the other six reviewed input hashes above remain unchanged. Verified all 26
preparation-03 checksums, all seven inputs against the worktree and the actual
manifest schema field; reran the synthetic self-check successfully. Earlier
unversioned artifacts remain retained rather than rewritten. Accept this narrow
revision under Return 7's same method/scope; runtime still awaits root's separate
directive. No worker, sandbox or VM was launched in this recheck.


## Return 8 — READ-only attribution method, September 20, root

**Accept the proposed method for code preparation, not execution.** Root read
claim 42, the proposed three-cell table and the current runner/profile paths.
A sole BSD-539 policy delta and fresh positive original-profile READ controls
distinguish the proposed attribution from the stopped combined experiment.
Keep the unchanged CONTROL boundary exact: API and direct CONTROL must report
Mach error 5 and no right in both restricted cells, matching claim 42. Other
errors, missing output and process death are not interchangeable denial evidence.
The existing broad wording “fail without rights” must be tightened accordingly
in the prepared method and checker. Unrestricted controls still require success.

The current all-four-positive gate cannot implement this method. Prepare the
separate profile/mode and finite synthetic cases, including unrelated CONTROL
errors and positive-operation failure. Keep the old mode's meaning intact and
retain the changed schema/inputs in a fresh build-only snapshot. Root will review
the exact changes before execution. No sandbox/VM run, policy effectiveness,
peer exclusion, or replication is accepted by this method review.


### Return 8 code closure — READ preparation 02

Root independently inspected the new READ-only profile, runner mode, gates,
classifier and synthetic cases. The sole policy addition is BSD syscall 539;
probe, launcher, original profiles, entitlement and retention helper remain
byte-identical to the prior reviewed inputs. All 27 preparation checksums and
eight current source identities match preparation 02; its schema is v3, builds
passed, and no worker or VM ran. Runner SHA-256:
`46f1cc86c463eddf2879399a47e031636ced3d33f82c0fc8c7b9413d8229fa36`.

Review found and closed a classifier defect before execution: repeating the
unrestricted cell as the third cell could falsely report attribution. The
corrected classifier requires the exact control/base, restricted/base and
restricted/amended sequence with sanitized launch. Root reran synthetic checks
and replayed claim 42's retained baseline: both real baseline cells qualify,
while replacing the amended cell with the unrestricted control is rejected.
Exact Mach-5 CONTROL and READ minus-one/EPERM-or-EACCES checks, absent rights,
positive operations and self-task facility are enforced. Historical preparation
01 remains superseded, not rewritten.

**Accept preparation 02 for one fresh decision-evidence run** named
`containment-read-filter-reviewed-01`, using `--read-filter --execute`. This
explicit budget is at most three workers, one peer, one native vCPU/16 KiB per
worker, 20-second worker/100-second phase limits, inherited unlimited VM CPU
limits with no setter, and existing kill/reap and retention rules. Stop on the
reviewed baseline or environment failure; no retry, widening, changed signing
or automatic replication. Findings review must distinguish collected output
from accepted policy binding. This verdict accepts no observed outcome.


### READ independent replication preparation — root acceptance

Memory independently inspected the method, exact source inputs and gates,
verified all 49 original-run hashes and eight current-input identities, reran
synthetic checks and confirmed inherited unlimited CPU limits and a fresh run
identity. Root's independent original-run/input checks agree; no code, profile,
signing or method change is proposed. The original result supports replicating
the same discriminating comparison, not widening its scope.

**Accept exactly one independent fresh rebuild/run by memory:**
`containment-read-filter-replication-01 --read-filter --execute`, external raw
root `/private/tmp/natemu-track03-raw`. The runner snapshots unchanged inputs
and rebuilds both native binaries. Retain the same at-most-three-worker, one-peer,
20-second worker/100-second phase, one-vCPU/16-KiB and resource/cleanup bounds.
No retry, rule change, target-right transfer or signing change. Stop on any
reviewed invalid baseline/environment condition. Findings acceptance remains
separate from this execution budget.

### Mach-96 preparation 01 — root independent acceptance

Root independently inspected the source/method in claim 45, null-target branch
in pinned `kern_proc.c`, pre-dispatch filter in `bsd_arm64.c`, current probe,
runner predicates/supervision, sole added profile rule, synthetic negatives and
retained native assembly. All 30 preparation checksums and nine current input
identities match `containment-mach96-preparation-01`; schema v4 records successful
builds and zero executed cells. Both emitted calls set x0 to null, x1 to the
precomputed PID, x2 to the writable stack word and x16 to -96 immediately before
svc 0x80. Output sentinels are never deallocated as rights. Root reran the pure
synthetic check successfully. No producer change was made by this review.

Runner SHA-256 is
`7f11493d3df2bdbe491131f1716cbec11168b6cf4bd6f659c523825b8a6515f5`;
probe SHA-256 is
`7952fc0adf8cc47bcb9c5602231930406f9c283b80129e86ea259cb2a00bd3e9`.
The exact control/base, restricted/base, restricted/amended identities, two
Mach-5 copyout discriminators, writable-word checks and positive launch/HVF/
resource controls are enforced. Invalid baseline stops before the amendment.
The remaining design-specific uncertainty is whether the Mach-96 policy rule
suppresses this handler entry on the named host. General VM execution and the
already accepted BSD READ effect do not answer it.

**Accept one fresh decision-evidence run by machine**, named
`containment-mach96-reviewed-01 --mach96-filter --execute`, beneath
`/private/tmp/natemu-track03-raw`. Rebuild from a fresh snapshot of these unchanged
inputs. Budget: at most three workers, one peer plus one worker concurrently,
one 16-KiB/one-vCPU guest per worker, 20 seconds per worker and 100 seconds total
execution, inherited unlimited VM CPU with no setter and existing file/descriptor,
kill/reap and retention bounds. No retry, profile widening, additional target
operation, changed signing or automatic replication. Findings review remains
required; this accepts no runtime outcome or general containment claim.

### Fixed-KERN_DENIED method — independent root preparation review

Root inspected the complete runner delta against Mach-96 preparation 01 and
replayed its synthetic checks. All 30 hashes and nine source identities match
`containment-mach96-denied-preparation-01`. Only the runner changed; native probe,
profile delta, launcher and signing inputs are unchanged. Schema v5 explicitly
names `fixed-denied53-v1` versus `legacy-exact5-v1`; the two CLI modes are mutually
exclusive. The new mode requires exact 53 and unchanged sentinels only for the
amended cell; both baselines still require exact 5/zero output. It preserves all
identity/launch/resource gates. Cross-method synthetic checks reject 5 under the
new amended criterion and 53 under the original one. Root independently verified
claim 47's source basis; this is a newly declared method, not a rescore of claim 46.

**Accept preparation, no execution budget yet.** The shared native target remains
null and own-PID only, with no acquired debug right. A subsequent fresh bounded
run and independent replication are needed before accepting a sampled rule effect.

### Fixed-KERN_DENIED primary execution budget

The separate memory matrix is terminal, so authorize exactly one fresh rebuild
and run by machine, `containment-mach96-denied-reviewed-01
--mach96-denied-filter --execute`, using the unchanged accepted preparation.
Retain schema v5 and fixed-denied53-v1 identity, at most three workers/one peer,
one 16-KiB/one-vCPU guest per worker, 20 seconds per worker/100 seconds total,
inherited unlimited VM CPU with no setter and the existing resource/supervision
bounds. External raw root remains `/private/tmp/natemu-track03-raw`. Stop on
any invalid baseline/environment condition. No retry, widening, changed signing
or automatic replication. The original exact-5 run remains unchanged.

### Fixed-KERN_DENIED independent replication budget

Translator's independent preparation review (findings Return 26) verified the
primary's 52 hashes/nine inputs, source/method applicability, current/retained
synthetics and cross-method replay. Root's independent checks agree. The native
handoff diagnostic is terminal; no overlapping VM experiment remains.

Authorize exactly one fresh independent rebuild/run by translator:
`containment-mach96-denied-replication-01 --mach96-denied-filter --execute`, under
the existing external raw root and unchanged reviewed inputs. Retain the same
three-worker/one-peer, one-vCPU/16-KiB, 20-second-worker/100-second-phase bounds,
unlimited inherited VM CPU/no setter, exact controls, signing and cleanup.
No retry, profile/method change, extra acquisition target or further replication.
Findings acceptance remains separate; the original exact-5 result is untouched.

### MIG205 preparation 02 — root independent acceptance and primary budget

Root independently reviewed claim 54, the native probe delta, exact profile
addition, runner admission/stop predicates, source snapshot and synthetic cases.
All 31 retained hashes match preparation02; current producing inputs match the
snapshot. Replaying its retained `--self-check` passed without native execution.
The native HVF body, launcher, base profile and signing inputs are unchanged.
Nonzero process exit (including new exit 27) fails `positive_ok`; baseline failure
cannot admit the amended cell. Error observations are explicitly not denial
acceptance. Non-root identity, valid host, both live-right baseline acquisitions,
cleanup and HVF/resource/launch controls are checked. No IOKit service is invoked.

The shared VM machinery and source dispatch contract are already established;
the remaining decision uncertainty is this installed policy rule's acquisition
effect and compatibility with the required HVF operations. This finite contrast
addresses it without claiming absence of preexisting or alternative capabilities.

**Accept one fresh primary rebuild/run by root:**
`containment-iomain-reviewed-01 --iomain-filter --execute`, external raw root
`/private/tmp/natemu-track03-raw`, unchanged preparation02 inputs and v6 method.
Budget: at most three fresh workers and one harmless peer, 20 seconds per worker,
100 seconds execution phase, one 16-KiB/one-vCPU guest per worker, inherited
unlimited VM CPU with no setter; existing resource, kill/reap and retention bounds.
Stop on invalid controls/environment, retain every outcome, no retry or widening.
Findings review and separately reviewed independent replication remain required
before accepting load-bearing observations. This authorizes no production change.

### MIG205 independent replication budget

Memory's independent findings Return33 verified all53 primary hashes, ten
producing inputs against preparation02/current files, fixed classifier and raw
controls, and prepared the unchanged method for replication. Root's independent
checks agree. The primary is terminal; no other native experiment is active.

Authorize exactly one independent fresh rebuild/run by memory:
`containment-iomain-replication-01 --iomain-filter --execute`, beneath
`/private/tmp/natemu-track03-raw`. Retain v6/mig205-observation-v1, unchanged source,
profile/signing/launcher, three workers/one harmless peer, 20-second worker and
100-second execution bounds, one16-KiB/one-vCPU guest per worker, inherited
unlimited CPU without a setter and existing cleanup/retention rules. No retry,
new target or widening. Findings review remains separate from runtime collection.

Replication launcher correction: memory's first invocation omitted the required
`NATEMU_RAW_ROOT` environment and failed in `retention.allocate` before creating
its destination, building or starting any worker. Preserve that executor error;
no native sample exists to retry. Root authorizes one corrected invocation with
`NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw`, the same fresh destination and
unchanged bounds. This correction does not authorize repeating an executed cell.
