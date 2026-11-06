# INV-03-04 — Native handoff preparation review

## Return 1 — four-cell preparation 03

September 20, independent reviewer `/root`; author `/root/memory`. Root inspected
claim 19's reviewed premise, the complete current native assembly, owner-thread
probe, independent trace-model oracle, retained-run supervisor and README.
The remaining uncertainty is the named-host two-way ownership handoff between
host backing access and two native participants. Ordinary HVF/QEMU mapping use
and inherited finite code transfer do not alone qualify that precise handoff.
Private stage-2 descriptors or universal coherence are not the claim.

**Accept the corrected preparation for its finite method and build scope. No VM
execution budget is granted in this return.** Root verified all 36 retained
checksums and seven current input identities in
`/private/tmp/natemu-track03-raw/handoff-preparation-03`. Its status is
`built_not_executed`, execute flag false, with zero cells. Strict C/link and
assembly commands pass; root reran the oracle's four synthetic models and 96
rejected mutants. Those synthetic checks are not hardware observations.

The reviewed code uses two creator-owned vCPUs and the main thread as backend,
fresh allocation/VM per cell, both aliases, alternating resident writers and
64 repeated bidirectional epochs. Both participants return actual register data
through specific HVC sites after native DSB SY. Direct host transfers occur only
after completed drains and have compiler-clobber DSB SY before and after access;
copied cells use register payloads/exports after initial host initialization.
The ordered oracle checks actual words, CPU/sequence/epoch/phase/alias, both drain
participants, completion order, API lifecycle and teardown. Failure or timeout
stops the matrix; the retained helper kills/reaps the process group. VM workers
inherit unlimited CPU limits with no setter. The source-generation subcase is
only the stated coordinator fixture, not a translator race proof.

Review found an incorrect distinct-cache-line claim at 64-byte spacing. The
corrected code uses offsets 0/128/256/384 and records/validates host line size
64 or 128 before entry; preparation records 128. Retained disassembly confirms
those accesses, DSB before boot/reply HVCs at offsets 0x68/0xb0, readback of actual
stage-1 descriptors and controls, and explicit resume labels. Preparations 01
(environment-read failure) and 02 (superseded spacing) remain separate records;
03 does not rewrite either.

This does not accept runtime control-register behavior, operational handoff,
copy cost, transport selection, effective stage-2 attributes, arbitrary vector/
unaligned/MMIO access or performance. A separately stated one-matrix execution
budget may use these unchanged inputs; substantive changes require recheck.
Any load-bearing positive observation still needs findings review and independent
fresh-build replication. No additional framework or general coherence campaign
is warranted by this preparation.

### Execution budget — primary matrix

Root revalidated preparation 03 and the current summary on the next goal
continuation. Authorize exactly one fresh rebuild and four-cell primary matrix
by memory, `handoff-reviewed-01 --execute`, under
`/private/tmp/natemu-track03-raw`, using the unchanged seven reviewed inputs.
At most one VM at a time, two vCPUs, 144 KiB backing and 64 epochs per cell;
20 seconds per cell and 100 seconds for the whole matrix. Inherited unlimited
VM CPU limits, no setter; retain the reviewed helper's resource limits,
process-group termination, partial records and checksum rules. Stop at the
first environment/API/identity/oracle error. No retry, expanded matrix, code or
signing-policy changes, or automatic replication. This authorizes collection,
not a runtime finding or transport selection. Root independently reviews the
result before any further execution budget.

## Return 2 — corrected checkpoint identity, preparation 04

Root independently compared all changed producer/oracle/runner lines with
preparation 03, rechecked the inherited post-HVC PC rule in frozen host.c,
verified all 36 checksums and seven current inputs of `handoff-preparation-04`,
and reran four synthetic models/104 rejected mutants. Native assembly and the
payload/ownership protocol are unchanged. Schema v2 names both instruction and
resume PCs; both native checkpoint checks require the exact named resume label,
while the oracle rejects instruction-PC responses at either site. Failure-path
bookkeeping now sets terminal cell status and retains native return/timeout data.
The old failed run is preserved, not reclassified. No substantive return remains
for this correction; runtime applicability is still to be observed.

**Authorize one fresh corrected primary matrix by memory:**
`handoff-reviewed-02 --execute`, unchanged preparation-04 inputs, external raw
root, four fixed cells once, one VM at a time/two vCPUs/144 KiB/64 epochs,
20 seconds per cell and 100 seconds total. Keep all prior resource, inherited
CPU-limit, supervision and failure-retention conditions. Stop on first failure;
no retry, widened configuration or automatic replication. This replaces no
historical evidence and accepts no runtime result.

## Return 3 — retain boot values before checking, preparation 05

Root inspected the exact producer delta and verified all 36 hashes/seven input
identities in `handoff-preparation-05`. The two owner threads now retain their
readbacks, and main prints both complete boot rows before the identical six-field
checks and before any payload command. Lock/condition ownership still parks both
participants during this step. Native assembly, expectations, passing trace
schema, oracle and runner are unchanged; strict build and synthetic checks pass.
This fixes diagnostic loss without weakening the condition that failed.

**Authorize one fresh collection by memory**, `handoff-reviewed-03 --execute`,
using these unchanged inputs and the same four-cell/one-VM/two-vCPU/144-KiB/
64-epoch/20-second-cell/100-second-total bounds. Its immediate purpose is to
identify the failed boot readback; it must stop at that unchanged gate on any
mismatch. If the gate unexpectedly passes, the already reviewed finite matrix
and all its oracles remain in force; no widening or retry is allowed. Preserve
all actual boot values and failures. No value is accepted merely because it was
observed, and no root cause or handoff behavior is inferred before review.

## Return 4 — explicit SCTLR setup, preparation 06

Machine's independent findings Return 19 accepts claim 24's source-derived
configuration: SED/ITD are unused by this AArch64 EL1 fixture, with the stated
conditional/RES1/optional readback limits. Root independently inspected the
complete conditional register fields and descriptive text, then compared the
producer/oracle delta against preparation 05. The initial request now explicitly
sets bits 7/8 (`0x30d00980`), with exact enabled readback `0x30d01985`. No masks,
other-field changes, native assembly or payload-protocol changes were introduced.
This is a named-host fixture invariant, not a universal Arm whole-word value.

Root verified all 36 hashes and seven current inputs of preparation 06 and reran
four synthetic models/108 rejected mutants, including rejection of the old
SCTLR value. Strict retained builds pass; no worker has run from these inputs.
**Accept the affected preparation with no substantive return.**

Mach replication is terminal. Authorize exactly one fresh corrected primary
matrix by memory, `handoff-reviewed-04 --execute`, unchanged preparation-06
inputs under the external raw root. Four fixed cells, one VM at a time/two
vCPUs/144 KiB/64 epochs, 20 seconds per cell/100 seconds total; retain inherited
unlimited VM CPU/no setter and all existing supervision/resource conditions.
Stop at the first failure, preserving raw values. No retry, changed expectations,
widening or automatic replication. Findings and any replication remain separate.

## Return 5 — independent replication readiness, machine reviewer

Machine independently read the current C owner/coordinator code, native assembly,
standalone oracle, runner, method and Returns 1–4. Verified all 50 retained hashes
of `handoff-reviewed-04`, and all seven inputs against preparation 06 and current
sources. Replayed the exact four raw cells: each passed the 64-epoch oracle,
including both CPUs' complete boot values, exact SCTLR 0x30d01985, requested
ISH/OSH descriptors, all returned words, lifecycle and teardown. Copy cells
contain 1,478 native exits; direct cells contain 1,350. Independently reran four
synthetic models and 108 rejected mutants. These replays are checks of existing
evidence, not an independent runtime replication.

Owner-thread API confinement, mutex/condition handoff and native DSB-before-HVC
acknowledgements compose for this finite schedule. Both participants remain at
validated completion boundaries during host backing accesses. The copied route
uses register outputs in both directions, with no payload alias access after
initial publication; actual returned words feed the oracle. Source-generation
rejection is a deliberately finite host protocol model, not full translator
publication. No concurrency timing, payload atomicity, private stage-2 attributes,
MMIO/UC, arbitrary lifecycle or transport performance is inferred.

A separate fresh build-only `handoff-replication-preparation-01` completed from
these unchanged seven inputs. All 36 hashes verify; status is
`built_not_executed`, zero cells, with no signing or native worker execution.
Assembly/disassembly retains exact named HVC resume sites and barriers. No code
changes were made. Historical README wording about older failures is provenance;
it does not change these source inputs or assert a new result.

**Accept replication readiness with no substantive return.** Proposed fresh run
identity is `handoff-replication-01 --execute`, using the same four cells once,
one VM at a time, two vCPUs, 144 KiB and 64 epochs per cell, 20 seconds per cell
and 100 seconds total; inherited unlimited VM CPU/no setter and the existing
resource/group-termination/failure-retention rules. Stop at the first error,
without retries, profile changes or widened expectations. This review itself
provides no execution budget; await root authorization. Fresh findings still
require root review, and successful replication would qualify only this finite
named-host ownership trace.

### Independent replication execution budget

Root checked Return 5 and independently verified all 36 replication-preparation
hashes/seven producing identities against the primary. No input or method change
is proposed; the fresh run identity is absent and all other VM experiments are
terminal. Authorize exactly one independent fresh rebuild/run by machine:
`handoff-replication-01 --execute`, under `/private/tmp/natemu-track03-raw`.
Keep all four fixed cells once, one VM at a time/two vCPUs/144 KiB/64 epochs,
20 seconds per cell/100 seconds total and inherited unlimited VM CPU/no setter.
All reviewed resource, retained-build and process-group cleanup rules apply.
Stop at the first error, no retry or changes. Root findings review must assess
the actual result before accepting a replicated handoff claim.

## LSE2 API-read preparation03 — root independent acceptance and primary budget

Root independently inspected claim34, SDK26.4 feature/system-register declarations,
Arm MMFR2.AT definition, complete probe/runner/classifier, the unchanged retention
helper and preparation03. All23 retained hashes and five current-input identities
match. Replaying the retained pure classifier checks passed without Hypervisor
calls. The probe creates one default config/VM/owner-thread vCPU, queries each
feature view once, destroys created objects, and releases config on both VM-create
failure and normal exit. It maps no RAM and executes no guest instructions.

The remaining decision-changing fact is whether the default captive vCPU exposes
LSE2. Both SDK query APIs exist; no guest-MRS program is needed merely to obtain
this feature fact. API errors, inconsistent full values, unknown AT and lifecycle
failure remain explicit inconclusive/failed outcomes. The classifier does not
convert enumeration into effective-WB or128-bit atomicity evidence. Inherited
unlimited CPU is checked, with no CPU-limit setter for the native worker.

**Accept one fresh primary rebuild/run by root:**
`lse2-api-read-reviewed-01 --execute`, using unchanged preparation03 inputs and
`NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw`. Budget: one worker/defaultVM/
vCPU, one read per view, zero mappings/guest bytes/vCPU runs,20-second worker and
30-second signing/execution phase, one compile job/60-second build timeout,
existing process-group kill/reap and retained-outcome rules. No retry, feature
write, guest-MRS fallback or widening. The expected native outcome is not assumed.
Independent findings review and separately prepared replication remain required
before load-bearing observed acceptance. No other native budget is active.

### LSE2 API-read independent replication budget

Memory's independent findings Return29 checked29 primary hashes, all five
current/preparation03 input identities, both raw feature values, lifecycle/status
records and classifier replay. The fresh replica destination is absent and no
source or method change is proposed. Root's independent checks agree; the primary
is terminal and no other native experiment is active.

Authorize one fresh independent rebuild/run by memory with explicit environment:
`NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw python3 -B
experiments/track03/lse2-capability/run.py --name lse2-api-read-replication-01
--execute`. Preserve the reviewed one-worker/defaultVM/vCPU, one-read-per-view,
zero guest-memory/run,20-second worker/30-second signing-and-execution phase,
one-build-job/60-second compile, unlimited inherited CPU/no setter and existing
retention/cleanup bounds. No retries, feature writes, guest-MRS fallback or new
attribute inference. Findings acceptance remains separate from collection.


## Return 6 — paired-access preparation01, independent root review

**Reviewer:** root, independent of memory's method/code authorship, September20.
**Verdict:** accept the exact claim37 method and preparation for a bounded run.

The shared VM/coherent-backing setup is already covered in the named claim25
scope; claim35 establishes captive feature enumeration. The unresolved question
is joint paired-access behavior under actual interference in this mapping, not
another feature query or host word handoff. Arm's conditional paired-access
contract motivates the method but does not supply missing private attributes.
The finite outcome can inform named-platform applicability; no universal proof
or cache-attribute inference is authorized.

Read all six fixture files and reused retention behavior. Independently verified
all36 preparation hashes and seven current/retained input identities. All build
commands are terminal successful; no signing/native execution occurred. Inspected
disassembly at load0xdc/store0x174, split stores0x140/0x158 and resume0x74/0x19c.
The two virtual addresses map the same16-byte-aligned physical pair. Host access
stops before sample loops; owner threads retain all vCPU APIs. The forced half
store remains pending until reader acknowledgement, supplying a deterministic
positive torn control. Target loops have no per-sample handshake and require
both reader changes and the complete writer count before a clean classification.
Role reversal is a separate fresh process. A detected mismatch terminates further
cells; missing progress is inconclusive with no retry. Replayed the retained
pure oracle models/mutations; they pass without Hypervisor calls.

The supervisor retains partial failure, fresh-source build, output identity,
timeouts and reap. VM workers inherit unlimited CPU without a setter. No native
loop timing is promoted to performance evidence. No substantive return remains.
Findings review and independent fresh-build replication remain required before
load-bearing observation acceptance.

### Separate primary execution budget — paired-access matrix

Authorize root to run exactly one fresh unchanged matrix named
`pair-atomicity-reviewed-01` under `/private/tmp/natemu-track03-raw`, using the
reviewed runner with `--execute`. Maximum three sequential worker processes,
one VM/two vCPUs/144KiB backing each,20seconds per worker and100seconds for the
execution phase; two bounded compile jobs of60seconds each and separately bounded
sign/verify commands. Run the forced control first, then both fixed target roles;
stop on first invalid/torn/inconclusive result, native error or timeout. No retry,
parameter expansion, fallback or automatic replica. Retain every failure.
This budget is consumed by that invocation; collection does not accept findings.


### Separate independent replica budget — paired-access matrix

After memory's independent primary acceptance in findings Return32 and its
replica preparation check, authorize memory to execute exactly one unchanged
fresh-source/build matrix named `pair-atomicity-replication-01` beneath
`/private/tmp/natemu-track03-raw`. The seven inputs match the accepted primary;
reviewed Return6 method/code still apply. Maximum three sequential workers,
one VM/two owner-thread vCPUs/144KiB each,20seconds per cell/100seconds execution
phase, two60-second bounded build commands, separately bounded signing, inherited
unlimited CPU/no setter. Force the torn control before both role assignments;
stop on first invalid, torn, inconclusive, error or timeout. No retry, changed
parameters, added cell or subsequent experiment. Keep all artifacts/failures.
Root independently reviews the replica; collection is not self-acceptance.
