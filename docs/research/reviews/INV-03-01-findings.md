# INV-03-01 — source findings review

## Restructure plan review — October 26, 2026 owner direction

**Independent reviewers:** `/root/plan_owner_review` and
`/root/plan_engineering_review`, in separate read-only agent contexts; `/root`
authored the revision. Both inspected `docs/vm-restructure.md`, the current
implementation/investigation handoffs and the contract-inventory introduction.

**Evidence validity: ACCEPT the plan within its stated scope.** It distinguishes
native compilation, executable ABI integration and general system semantics;
it makes no new runtime or performance claim and does not certify old tests.

**Decision relevance: ACCEPT.** Both reviews found the parallel workstreams,
early joint ABI integration, substantial selective refactor, orchestrator quality
responsibility, justified incompleteness and evidence-backed pivots consistent
with owner direction. Neither found an upfront audit/ABI/refactor gate or a
substantive blocker. No changes were requested.

**Disposition:** two focused document reviews completed; no implementation,
experiment or reproduction was performed. This accepts the execution direction,
not existing-code correctness or a total implementation-time estimate.

## Final acceptance — complete finite I4-V integrated pass, October 26, 2026 UTC

**ACCEPT I4-V within the original implementation plan's admitted finite scope.**
`/root/i4v_final_review` independently reviewed the implementation, oracles,
primary artifacts, final contract matrix and all six reproduction bundles,
and independently executed the supporting QEMU comparison. No substantive
correctness, execution or reproduction defect remains open in that scope.
This verdict supersedes the pending final-candidate status below. It completes
I4-V; it does not complete I5, the full ABI milestone, Track03 or the emulator.

### Requirement-by-requirement judgment

| Original required area | Final evidence and judgment |
| --- | --- |
| V1 address domains, permissions and boundaries | **Pass in the admitted profiles.** Actual generated low/high fetch, data and stack paths retain full guest addresses, independently resolve parent/leaf permissions and exact faults, preserve committed stack/split-store prefixes and reject private/noncanonical accesses. Final-source reproduction preserves the earlier cases; seeded stack runs bind the real rotated actor, compiler/source writer, delayed peer and event roles. |
| V2 paging-off and mixed owners | **Pass in the admitted protected32 profiles.** Segment/address-size/wrap/A20 fixtures, bounded RAM/device/hole effects and exact nonpaging faults reproduce. Mixed paged/off owners retain their separate fetch/access/context rules while sharing physical source/data; native miss/retry does not fabricate guest page-table reads or A/D effects. |
| V3 control transitions and peer independence | **Pass in the declared transition profiles.** Actual control instructions and far transfers commit or reject precisely, preserve transactional hidden PAE state and next-fetch semantics, and reject stale work across ABA/context/source changes. Fifty actor rotations and seeded control compositions retain peer architectural state, routes and pending-event cuts. |
| Guest ordering, supported atomics and fences | **Pass as finite conformance evidence.** The 154 ordering/atomic cells and twenty live-dependency LFENCE cells reproduce against independent expectations and producing artifacts. Postcommit XCHG recovery resumes only the saved store; it does not repeat the exchange. Finite outcome histograms do not prove universal ordering. |
| Translation coherence | **Pass in the sixty declared parent/A-D/page-size profiles and preserved alias cases.** Independent full-state checks cover table bytes, cached versus invalidated A/D behavior, 4KiB/2MiB routes, delayed repair/acknowledgement and old-route completion. Actual code clearing remains distinct from unimplemented physical table/data recycling. |
| Recovery, partial effects and lifetime | **Pass in the supported compositions.** The eight partial-helper cases retain changed-on-read values and the already committed first write across real second-store faults; no resampling, duplicate write or premature mutation/release is accepted. Stack/control/coherence/atomic cases preserve canonical/native prefixes, stale reply/entry exclusion and retained references; actual fault sites bind the producing ELF. |
| Healthy N-vCPU progress and events | **Pass for N1/2/3/4.** All 72 seeded cases replay exact retained decisions against independently reconstructed eligibility and native actions. Actual reduced maintenance pools exercise identity-preserving FULL/re-admission in stack/control N≥2, with bounded fairness and event eligibility. N1 FULL is explicitly inapplicable. Offline replay is established; saved-ledger native playback is unimplemented and unclaimed. |
| Terminal failure containment | **Pass for the 25 predeclared injections plus their separate-host/new-VM controls.** Actual worker exit, malformed reply, held-authority panic, withheld acknowledgement and cancellation retain the intended gate/prefix and original cause. Every owner is joined/destroyed and actual compiler resources are cleaned. Abandoned locks/references remain failed state; healthy/setup failures are never relabeled as successful injections. N3 containment remains untested. |
| Native synchronization, publication and reclamation | **Accept the reviewed finite code/contract argument.** Ticket-lock release/acquire authority, waits outside that authority, closed admission before mutation, completed mapping/cache maintenance before acknowledgement/publication, guarded entry and reference-aware retirement remain bound to the final sources/emitted paths. These conclusions are not inferred from guest litmus frequencies or scheduler counts. |
| Upstream reuse, independent expectations and supporting comparison | **Pass.** Pinned FEX and kvm-unit-tests adaptations retain provenance/licenses and independent expectations. Final strict QEMU primary03 and independent02 reproduce six sequential defined outcomes with exact full RFLAGS and no masked output bits. QEMU adds no privileged paging, retry, concurrent ordering, fence-completion or native-synchronization qualification. No complete upstream suite or native-x86 comparison is claimed. |
| Reproduction and preservation | **Pass.** The exact earlier 688 native case definitions and 87 legacy jobs are preserved on final sources; new coverage, repeated baselines and fresh controls remain separately counted. Producing code, compiler/packet associations, ELF gates, resource receipts, checksums and failed development diagnostics are retained and independently reviewed. |

### Final producing evidence

The five fresh native bundles `i4v-progress-independent-20261026-01` through
`-05` preserve **760 distinct healthy cases = 688 prior + 72 new**, plus **25
intentional failure cases**. Their 961 manifest executions include 176 repeated
baselines; the 25 additional fresh-control executions bring actual native
executions to 986. `i4v-progress-regression-independent-20261026-01` separately
preserves the exact 87-job legacy list. This reviewer independently verifies
every cell definition against the prior accepted matrices, all 1,308 outer jobs
as passed/reaped/quiescent, 203,545 native checksums and 3,428 legacy checksums.

All five native source sets contain the same 811 producing paths as primary14;
all 132 legacy source paths match their frozen inputs, with exact older fragment
and producer pins. All six rebuilt runtime ELFs have SHA-256
`0e8c0a5bcd03bfdc02f4bb0acbdb8ad6e93bb35c18384b700cac13928ca3b945`.
The separate replicator's full semantic/artifact audits verify 6,512 native
compiler exports and 6,572 report/export associations. Its ownership audits
cover 6,522 native cleanup records plus 67 legacy owned processes and 57 legacy
containers; no remaining container is recorded. Those receipts and scripts are
retained with exact hashes in this review's manifest, not represented as a second
execution by this reviewer.

Native execution was serialized. Bundle04 finishes within its unchanged
1,200-second limit at 1,178 seconds; each other bundle also stays within its bound.
The conservative upper edge of second-rounded containment completion timestamps
places every reproduced outer teardown within 4,338 ms of the actual gate, below
10 seconds; the prefix oracle separately enforces the 3-second detection bound.
Resource receipts retain per-job caps and observed outer-group RSS. They are
not an aggregate process-tree memory measurement. Aggregate agent-minutes were
not authoritatively measured and are not claimed. Passing counts or time bounds
alone are not the basis for the semantic acceptance above.

Final QEMU evidence is `i4v-qemu-dev-20261026-03` and reviewer-executed
`i4v-qemu-independent-20261026-02`, both after legacy06. Each passes all six cases,
134 checksums, actual setup/capture/ELF/listing/instruction-log bindings and **420
actual-output corruption controls**, including all 64 RFLAGS bits in every case
(384 flags controls plus 36 others). The newly assembled programs and outputs
match exactly between the runs. Both use final-source resident reports, complete
exact-ID container/volume cleanup, and retain exact RFLAGS `0x202`. Load/stack
canonical flags match that value; ordering's full QEMU flags remain QEMU-only
while the native ordering report establishes NZCV preservation. The historical
masked comparator runs remain unchanged and are not substituted for this pair.

### Independence, durable review and limits

The final reviewer authored none of the runtime or production oracle changes.
Runtime/host/producer/oracle authorship is separated throughout the earlier
reviews. `/root/i4v_contract_review` authored the atomic-progress oracle and
expanded partial ELF validator; root reviewed those changes independently, and
this reviewer reviewed the final combined candidate. That agent is not described
as an independent author-reviewer of its own files. `/root/i4v_upstream` authored
the QEMU comparator; this reviewer reviewed it and owned fresh reproduction.

The [final durable audit](../../../../natemu-raw/i4v-final-scope-review-20261026-01/README.md)
retains independent scope/flags/artifact scripts, actual logs and hashed links
to the separate native replication audits. Its `SHA256SUMS` digest is
`add8b54903166c140dc92d207ea89edf6687d9d75f20393e2cf642007a1f4cef`.
The strict QEMU independent bundle's checksum-index digest is
`969ac275d77e9b89985d5298b23ded939d04c5b5c492ecfde706f6ab46b159c3`;
its [execution/audit receipt](../../../../natemu-raw/i4v-qemu-independent-review-20261026-02/manifest.json)
has digest `4538b0aa5804d2a66a5d9ae4df1324c9f75b6e420f0dc0b15ab6ebbddbc5e5a3`.

The final matrix's contract scope matches the original implementation plan.
Root and the replicator reconciled the draft/pending labels and superseded QEMU
references in the matrix, current investigation, README and implementation plan
to these final receipts; this required no more execution or expanded scope.
Preserve the explicit unimplemented/untested cells: general ISA/FP and exception
delivery, boot/devices, arbitrary guest concurrency, other atomic widths/routes,
dynamic A20/general segmentation, physical table/data recycling, native saved-ledger
playback, other host generations and performance/code-generator selection.
These finite M4 results do not narrow the eventual emulator target or establish
universal correctness.

## Pre-execution tightening — full defined QEMU flags, October 26, 2026 UTC

Root requires exact full RFLAGS for these fixed scalar profiles, consistent with
the implementation plan's rule that only architecturally undefined results may
be masked. `/root/i4v_final_review` independently accepts the narrow comparator
delta before the new primary/independent comparison pair. **Historical passed
outputs remain valid observations, but final comparison closure will use the
two fresh bundles under this stricter validator.** Native source inputs and
native reproduction requirements are unchanged.

All six profiles have defined preserved flags and produce full RFLAGS `0x202`.
The comparator now requires exact equality, removing the earlier `0xcd5` mask.
Load/stack also require the same complete canonical value in the resident report;
ordering's complete QEMU flags remain QEMU-only, while native ordering claims
only recorded NZCV preservation. No arbitrary CPL3 IF-restoration claim is added.
The assembly/setup and container execution code are unchanged. The reviewed
runner SHA-256 is
`3baff0fc1d64c9f39b9bbaf3535454bae0f85628c28e0228f13a2c5fbf2397ef`,
and model SHA-256 is
`6dfa0832077b5ff0e5c66a447ff8b5b495908e534165535a7ec99d14bc671a75`.
All seven focused tests pass independently. Applying the strict checker to all
twelve actual historical outputs passes and rejects all 768 single-flag-bit
corruptions, covering every bit in each output, including IF/control/reserved/high
bits. Those read-only checks are not represented as fresh comparison execution.
The new comparison pair waits for the native05/legacy06 slot to finish.

The [durable strict-validator review](../../../../natemu-raw/i4v-qemu-strict-review-20261026-01/README.md)
retains the reviewed sources, exact delta-audit script and passing 768-corruption
log, plus the updated independent artifact auditor. That auditor adds every
flags bit explicitly to the fresh checks: 384 flags controls and 36 other output
controls across six cases. The review `SHA256SUMS` digest is
`eaf1cd06ff2547b9cdd6bfb1ad96ef907fd4720b7472faaf572a5722f151331f`.

## Accepted supporting comparison — independent QEMU reproduction, October 26, 2026 UTC

`/root/i4v_final_review`, independent of comparator author `/root/i4v_upstream`,
prepared and executed fresh `i4v-qemu-independent-20261026-01` after root's explicit
exclusive-slot handoff. The reviewed orchestration was also checked by root before
execution. **Accept the plan's bounded pinned-QEMU supporting comparison in its
six declared scalar profiles. This closes the comparison evidence gap identified
in the final-candidate review; full I4-V acceptance still awaits the remaining
native/legacy replicas and aggregate review.** No native x86 result is claimed.

Fresh preparation binds the final-source stack report from native independent04,
ordering reports from independent03 and the load report from primary14. All source
files match the reviewed primary QEMU snapshot before preparation; execution uses
the new bundle's retained runner and container source. Preparation, execution and
independent artifact audit each exit 0 with their host process reaped. The complete
review runs at 17:44:14–17:44:16 UTC, after native04's terminal pass and before the
slot returns for native05/legacy06. Runtime execution and exact-ID container/volume
cleanup complete successfully; the container identity differs from primary02.

All 134 fresh checksums and all nine review-artifact checksums verify. Each of the
six newly assembled ELFs and captured binary outputs is byte-identical to primary02,
despite independently binding the new final-source stack report. The independent
byte-level setup/capture, ELF, listing, instruction-log, pinned-binary, resident
projection and full native-validator checks pass again. All 42 actual-output
corruption controls are rejected. No broader semantic claim follows from this
agreement: load/stack canonical registers and admitted memory, aligned DWORD XCHG
with result store, and scalar LFENCE/SFENCE/MFENCE preservation are the evidence.
Concurrent atomicity/ordering, privileged faults, retries, fence completion and
native synchronization retain their separate native contracts and tests.

The [fresh comparison](../../../../natemu-raw/i4v-qemu-independent-20261026-01/manifest.json)
has `SHA256SUMS` digest
`5022aef9667a432880a3c42d5528cf948ad43ef76af66390a6d4f7f29dcee87d`.
The [independent execution/audit receipt](../../../../natemu-raw/i4v-qemu-independent-review-20261026-01/manifest.json)
retains orchestration, command PIDs/timestamps, stdout/stderr and the independent
audit script; its `SHA256SUMS` digest is
`917707c64abdf93f50255587bb849a85f68737a2600a806ac547a69ea2aabfab`.
Primary failed run01 remains unchanged and excluded; the version-format correction,
passing primary02 and fresh reproduction are separately recorded below.

## Findings review — primary QEMU supporting comparison, October 26, 2026 UTC

`/root/i4v_final_review` independently accepts the retained artifact bindings and
six finite scalar outcomes in `i4v-qemu-dev-20261026-02`. **The supporting QEMU
obligation still awaits fresh independent execution with the final-source stack
report; this primary audit alone does not complete I4-V.** The reviewer ran only
read-only artifact checks, not QEMU, compilation or container operations.

All 134 checksums pass. The actual package/static binary, QEMU 7.2.22 Debian
version, `qemu64` CPU option, NASM 2.16.03, Python 3.13.5 and pinned container image
bind the retained invocation. Independent byte encoding verifies the complete
initial-register setup and sixteen-register save sequence, safe stack switch,
PUSHFQ capture and LEA-derived actual end PC in every emitted ELF. NASM listing
bytes and QEMU instruction-log bytes agree with that ELF; logged coverage includes
every guest, setup and capture byte. Exact final GPRs, admitted flags, end PC and
memory pass the independent scalar expectations and resident projections.
Forty-two in-memory corruptions of the actual outputs are rejected.

The original resident reports match their retained checksums and pass the full
native validators from the audited final native source snapshot. Load binds
primary14, ordering binds fresh independent03, and stack explicitly binds the
earlier accepted `i4v-coherence-independent-20261026-02`. The latter is not
relabeled as a final-source run. The created immutable container identity agrees
with the successful exact-ID cleanup receipt; the retained create command
shadows the image's inherited PostgreSQL volume with tmpfs, and the reviewed
removal includes anonymous volumes. No cleanup error is hidden.

The durable [audit bundle](../../../../natemu-raw/i4v-qemu-review-20261026-01/README.md)
contains the independently authored read-only audit script and compact results.
Its `SHA256SUMS` digest is
`54b8e1f81b7c500ac454815bb8fb0f1f0017bccb8b3695b225f1ac36754b0019`.
The successful comparison supports these sequential defined outcomes only:
it adds no evidence for privileged paging, fault retries, concurrent ordering,
atomic indivisibility across participants, fence completion or native publication.
The final completion matrix must preserve those boundaries.

## Pre-execution review — bounded QEMU supporting comparison, October 26, 2026 UTC

`/root/i4v_final_review`, independent of comparator author `/root/i4v_upstream`,
accepts the method and source for the six declared scalar comparisons before
execution. The reviewed runner SHA-256 is
`4291adde0173f6df4908e7c755e9eab99b854d3f2546568b2a592393d74c41bb`;
the independent scalar model is
`58d12e56d1f105859998b4610c5925d91769b1e53274ea95fdc3695a8a76a6c5`.
All seven focused Python tests pass independently. No native, compiler or
container operation was performed by this reviewer. **This is pre-execution
method acceptance, not acceptance of outcomes or closure of the QEMU evidence
gap.**

**Pre-execution cleanup addendum:** root identified the pinned image's inherited
`/var/lib/postgresql/data` volume. This reviewer accepts the author's narrow fix:
an explicit bounded 1 MiB tmpfs shadows that destination, and exact-ID removal
uses `--force --volumes`. Thus the known inherited destination does not allocate
anonymous persistent storage, while removal also covers defensive anonymous
volume cleanup without deleting the bind-mounted comparison directory. The
new accepted runner SHA-256 is
`fba6b829c3452b01915d06369164465ff42c3e27ccb15c8740a0ed8de4a9256a`.
The model, container driver and tests are unchanged; all seven Python tests pass
again independently. No comparison execution preceded this correction.

**First execution correction:** `i4v-qemu-dev-20261026-01` remains failed before
assembly or guest execution. The retained version line is exactly
`NASM version 2.16.03` followed by a newline; the initial predicate required a
space after the version. This reviewer verifies all retained checksums, that
specific traceback, absence of assembled harnesses and successful exact-ID
container removal. The sole source correction strips surrounding whitespace
then requires exact equality with the pinned version, without accepting other
versions. All seven Python tests pass again. The accepted container-driver
SHA-256 is `e5e6d77b02667c3a5107bcfeb320d6b824566c6addd0165f235386aa83546b1a`.
Fresh run02 is authorized under the already reviewed method; failed run01 is
not repaired or counted as comparison evidence.

The selected programs are the existing load, five-instruction PUSH/MOV sequence,
aligned DWORD XCHG/result store and three scalar fence-preservation profiles.
Literal source bytes, initial state, final registers, final RIP and memory are
compared against independently specified effects and actual retained resident
results. Load/stack expose all sixteen canonical GPRs; ordering exposes the eight
actual allocated roots. Additional ordering registers and load memory are labeled
QEMU-only canaries. The selected instructions preserve defined flags; the Linux
user comparison mask `0xcd5` covers CF/PF/AF/ZF/SF/OF/DF while recording and
excluding privilege-dependent control bits. Native ordering NZCV preservation
is not mislabeled as a canonical RFLAGS comparison.

All sixteen GPRs are saved before scratch use, including a passive noncanonical
RSP. MOV switches to mapped harness stack before PUSHFQ; no flag-changing
instruction intervenes. LEA records the actual capture label after flags are
saved, replacing the initial hardcoded expected-PC output. The ELF mapping and
exact guest bytes are checked, with emitted capture inspection still required
after assembly. The package, static QEMU binary, source archive, Debian patches
and source descriptor are pinned; the executed container image and CPU profile
are explicit. Time and resource limits, lack of network and immutable container
identity constrain the run.

Review corrections reserve an ownership name/token before container creation,
retain its CID file and recover the exact token-owned object after a creation
error, preventing the prior `cid=None` cleanup omission. Absence matching handles
actual lowercase Docker diagnostics while unrelated inspection errors fail.
Offline audit now checks exactly six cases, pinned binaries, retained resident
bindings, producing payload/ELF identity and exact QEMU invocation, rather than
accepting a partial case set or checking output alone. Original failures remain
failed and cleanup failure prevents acceptance.

Primary comparison may use the previously accepted stack report before the
final-source stack replica is available; its original bundle identity must remain
visible. The independent fresh comparison must bind the final-source stack
report alongside final-source load/ordering evidence. Both comparison bundles,
actual assembly/output checks and owned cleanup must pass before this supporting
obligation closes. This method supplies no privileged paging, fault retry,
concurrent atomicity, fence ordering or native publication qualification.

## Final-candidate review — primary progress/containment campaign, October 26, 2026 UTC

`/root/i4v_final_review` independently audited unchanged
`i4v-progress-dev-20261026-14` against the current Rust/Python implementation and
the original I4-V requirements. This reviewer authored none of the implementation
or validators and ran no build, native execution or compiler job. **The complete
primary development campaign passes its declared scope. Final I4-V acceptance
remains pending the six independent fresh-source reproduction bundles and the
plan's bounded pinned-QEMU supporting comparison.** This review does not promote
exploratory executions into independently reproduced decision evidence.

The durable [review bundle](../../../../natemu-raw/i4v-final-review-20261026-01/README.md)
retains the original primary audit script, its frozen-import replay, reconstructed
focused run11/run13 checks, fresh logs and a compact command manifest. All three
offline commands pass at 17:12 UTC, without modifying the producing runs. The
review `SHA256SUMS` digest is
`157d6e806bc4903db4ffca85d8a00aed6830ce18b80e33672afd2076f1785775`.
The focused repeat includes seven corruption checks; it does not claim to recreate
the original inline transcript or every earlier corruption test. Its integrity
log preserves the expected older partial-host source differences in run11/run13
and the exact 811-path match for run14.

All 57,053 retained checksum entries verify. The 807 captured inputs under
crates/tools/fixtures/experiments plus the four root build inputs (`.cargo/config.toml`,
`Cargo.lock`, `Cargo.toml`, `rust-toolchain.toml`) match the workspace: 811 producing
source paths in total. The runtime SHA-256 remains
`0e8c0a5bcd03bfdc02f4bb0acbdb8ad6e93bb35c18384b700cac13928ca3b945`.
Revalidation of its emitted capture/restore/vector/boot/helper gates binds every
populated partial-helper fault to the actual STRB site `0x8000400aecb4`.

The independently enumerated cell names exactly match 72 healthy seeded cases,
25 intentional failures and 44 baseline cases; all 25 separate fresh controls
also replay successfully. Configuration, actor, actual request/source/compiler
owner, delayed peer, event role and native protocol effects are checked rather
than inferred from runner counts. The healthy ledgers contain 5,132 decisions:
1,240 stack, 1,472 controls, 960 coherence, 1,360 atomic and 100 partial. Actual
maintenance FULL/re-admission occurs 216 times in stack and 206 in controls;
other families correctly claim none. The partial helper preserves its sampled
second value and committed first write; the atomic continuation resumes only
the saved post-exchange store. Their fault sites and native/canonical prefixes
remain bound to the producing programs and ELF.

All 1,900 manifest compilation associations exactly equal independently verified
artifact exports and actual packet deliveries, including malformed-reply origins
and completed failure prefixes. All 1,910 ownership journals have distinct tokens,
cover the exact retained registry set and acknowledge complete child/container
cleanup. Standalone compiler records bind their external supervisor receipts;
ordinary jobs retain their owned process-group and resource identities. All 213
outer jobs passed, reaped their child and recorded a quiescent group. The 122
healthy/failure/fresh host identities are distinct. Each failure retains its
specified actual gate and original cause, successful owner/VM destruction, and
a subsequent independently started control with a new VM incarnation.

Maximum observed gate-relative failure detection is 2,814 ms and joined stop is
2,815 ms. Every outer gate PID matches its failed host. Even using the
conservative upper edge of second-rounded completion timestamps, every whole
outer job finishes within 4,066 ms of its gate, below the 10-second limit. These
are observed finite bounds, not a guarantee for arbitrary future workloads.

The candidate contract-to-test matrix preserves the admitted V1/V2/V3 profiles,
finite ordering/atomic/fence checks, translation/lifetime invariants and explicit
unsupported paths. Reviewed release/acquire, authority, publication/acknowledgement
and reclamation arguments remain necessary alongside the finite observations;
healthy histograms cannot prove universal ordering. Offline exact ledger replay
satisfies the retained-seed/trace obligation and is distinguished from unimplemented
native recorded-decision playback. The original 688-case baseline and 87 legacy
jobs still require the pending final-source reproduction; the primary campaign's
44 repeated baselines do not substitute for them.

**Substantive completion gap:** [the original plan](../../implementation.md#post-i4-integrated-validation)
separately requires comparison of defined outcomes with native x86 where
available **and a pinned QEMU configuration**. Candidate status of the upstream
QEMU test corpus does not waive this later supporting-comparison requirement.
Neither reviewer found retained applicable I4-V QEMU outcome evidence or an
explicit owner waiver. The smallest faithful correction is a bounded comparison
of selected already-admitted instruction outcomes, retaining exact QEMU revision,
configuration, input/output and defined-state masks, checked against resident
results and independent expectations. It need not import a complete upstream
suite or claim privileged paging, retry, concurrency or native synchronization
qualification. The contract reviewer has corrected the draft matrix to retain
this obligation. No additional substantive runtime/oracle defect was found in
the complete primary artifact audit.

## Development review — partial-helper cancellation setup, October 26, 2026 UTC

`/root/i4v_final_review` independently reviewed the containment-only output-parent
creation in `concurrent/partial_progress.rs`, the unchanged failed run12 report,
and focused `i4v-progress-dev-20261026-13`. **The setup integration defect is
closed at source and focused development execution; complete campaign acceptance
and independent native reproduction remain pending.** No build, compiler or
native execution was launched by this reviewer.

Run12 correctly remains failed after 21 completed containment/fresh-control
cells. Its partial cancellation stopped before the injection gate with the
original ENOENT cause, witness readiness 0, both native status fields 0, successful
destruction of both owners and the VM, and no fresh-control execution. The output
parent was absent. Replaying that unchanged report with the current oracle
rejects the missing Release-published witness. The fix creates the parent only
for containment, before VM/owner startup, matching the control fixture's existing
non-overwrite setup; the per-owner compiler still owns its child directory.
Healthy partial-helper execution creates no compiler work or output root.

**Evidence validity:** all 2,158 run13 checksums verify and all 47 jobs are terminal
passed. Of 807 captured code/tool/fixture inputs, the sole workspace difference is
the two-line module comment clarifying that containment adds compile-only work;
the executable source is unchanged. The actual failed prefix retains two device
reads and one committed write per owner, second-subaccess samples 110/142, one
native fault each, retained references and no release, second write, remap or
fabricated completion. Its 27-operation trace, saved routes, canonical/native
state and pending events pass the independent prefix and scheduling replay.

The single actor 0 compiler request binds the fetched compile-only source and
current VM identity. The live creation-hold marker, ownership token and immutable
container ID agree with the registered resource that was removed; the worker
group is acknowledged gone and its leader reaped. No completed compiler export
is claimed. The observed gate and original cancellation detection occur at 282 ms;
all owners are joined at 422 ms. Both native-owner destruction calls and VM
destruction succeed. The outer supervised job completes in 0.721 seconds with its
gate bound to host PID 25720 and records process reaping and a quiescent group.
The native command retains its required failure exit 1.

The subsequent healthy partial control runs in PID 25773 with a new VM nonce
`11897847499402808037`, distinct from failed nonce `14382727715825048732`. Its four
helper elements, two post-remap probes and ten scheduling decisions pass, with
no compiler exports or inherited worker state. Revalidating the actual producing
ELF and binding all populated failed/fresh helper fault PCs confirms the STRB
site `0x8000400aecb4`. Its runtime SHA-256 is
`0e8c0a5bcd03bfdc02f4bb0acbdb8ad6e93bb35c18384b700cac13928ca3b945`.
Six in-memory corruptions of the actual failed report—missing gate, extra read,
changed sample, fabricated second write, premature release and substituted
setup-error cause—are rejected; retained artifacts were not modified.

**Decision relevance:** this observation now exercises the intended partial
helper cancellation and clean subsequent control, rather than counting setup
failure as the injection. No substantive blocker remains for that correction.
It supports proceeding with the bounded combined campaign; it does not accept
all 72 healthy / 25 containment cells, preserved regressions, independent native
reproduction or full I4-V. Offline evidence replay is not a new native run.

## Development review — actual stack actor rotation, October 26, 2026 UTC

`/root/i4v_final_review` independently reviewed the current changes against
`59d461a`, then rechecked the corrected Rust role bindings, Python oracles and
unchanged retained run `i4v-progress-dev-20261026-11`. The reviewer authored none
of the implementation or oracle changes and launched no build, compiler or
native job. **The stack actor-rotation source and demonstrated native coverage
gap is closed for this development checkpoint; full I4-V acceptance remains
pending the complete campaign, regressions and independent reproduction.**

The initial review found that stack scheduling reported a seed-rotated actor
while execution still fixed compilation/mutation to owner0 and the delayed peer
to N−1. The correction binds compiler allocation, resident request identity,
source initialization/mutation/publication and IF-masked event role to the actual
configured actor. The delayed peer and relative shadow/priority/event roles
rotate with it. Physical source/code/table backing remains shared at its original
location; private roots, frames, data spans and event vectors retain actual CPU
identities. The independent oracle checks these effects and producer deliveries,
not merely the actor labels. Ordinary unscheduled fixtures retain actor0 and
delayed N−1 defaults. No substantive source/oracle defect remains in this recheck.

**Evidence validity:** all2,236 retained checksums verify, all807 captured
code/tool/fixture inputs match the workspace at audit time, and all46 recorded
jobs are terminal passed. The current independent oracle accepts the unchanged
N4/seed-ordinal3 native report: compiler request owners are `[3,3,3]`, actual
source writer and publication owner are3, and the delayed owner is2. All160
scheduling decisions and21 distinct FULL/re-admitted identities replay against
the actual protocol trace and queue states. Three actual producer exports and
all nine delivered chunks bind the resident requests/replies; each owned worker
and container journal acknowledges completed cleanup. All four native owners
report success, completion, join and successful destruction, with no cancellation
or supervisor error; the joined report elapsed time is1,238ms.

The producing runtime SHA-256 is
`0e8c0a5bcd03bfdc02f4bb0acbdb8ad6e93bb35c18384b700cac13928ca3b945`.
Eight in-memory corruptions of the actual report—actor/delayed labels, request
and reply owners, source writer, actor IF state, publication owner and delayed
fault owner—are all rejected. The retained report and artifacts were not changed.
This audit replays existing evidence; it is not an independent native rebuild or
re-execution, nor a claim of universal ordering or all actor/seed combinations.

The separate Docker absence-diagnostic correction also passes source review:
case-insensitive explicit absence permits cleanup acknowledgement, while daemon,
permission and timeout errors remain failures. Six independent read-only mocked
responses verify that distinction; unacknowledged creation, immutable identity
and ownership-label checks remain intact. Failed run09 stays failed and is not
relabelled by this correction.

**Decision relevance:** the corrected path now supplies actual rotated stack
composition for the declared progress campaign, instead of counting labels as
coverage. Run11 supports continuing the bounded combined validation. The full
72 healthy /25 containment cells and fresh controls, preserved regressions,
independent reproduction and final contract-to-test review remain required;
no result is inferred from the currently running combined campaign.

## Development review — containment prefix admission, October 26, 2026 UTC

At 15:57 UTC, `/root/i4v_contract_review` independently reviewed the
producer-authored containment oracle, root-authored host/runner integration and
the actual locked-panic diagnostic `i4v-progress-dev-20261026-08`. No native,
compiler or build job was launched by the reviewer. **This is development
evidence and approval of the corrected checker wiring, not containment or
I4-V acceptance.** The retained diagnostic remains failed because its captured
public checker deliberately rejected all acceptance; it did not run the fresh
control VM.

The actual frozen prefix and all 11 compiler exports/deliveries passed the
independent audit; all 2,415 retained checksums matched. The N1 actor stopped
with the intended resident panic detail 880, its real code reference and pending
event retained, no fabricated completion/reclamation, and the ticket lock still
held at lock 121 / next-ticket 122. Native-owner destruction and VM destruction
both reported success. Gate observation, cause detection and joined-stop times
were all 3,155 ms after execution began. The truncated scheduling ledger still
checks the seeded draws, independently derived eligibility and consumed native
actions; it does not bypass these checks for failure reports.

Review found and verified two evidence corrections before the next snapshot:

- The containment prefix now applies the healthy family's finite EL, SCTLR,
  ACTLR and TCR admission plus resident capture/service-PC checks. All six
  zero-corruptions of those actual08 fields are now rejected. The partial
  family checks its reported store span without inventing a service-PC field.
- The runner now binds populated partial-helper fault PCs and `store_pc` to the
  validated producing ELF in the failed containment report, its separate fresh
  control and ordinary healthy partial runs. The one- and two-fault controls
  reject either an incorrect helper address or a changed populated fault site.

Ten containment tests and the runner binding test passed. The corrected public
checker accepts actual08's frozen prefix, and actual export/cleanup association
still returns 11 exports. Returning that audited prefix summary is sound only
with the runner's mandatory artifact/ownership verification and separate-host,
new-incarnation healthy control; the summary itself is not the complete
containment decision. The joined-stop timestamp precedes VM destruction, so the
successful gate-relative outer-supervisor result must also bound the whole host
process, including final teardown. The external supervisor's revised PID-lease
implementation requires its own retained evidence. The full 72 healthy / 25
containment campaign, current-source regressions and independent reproduction
remain pending; no result is inferred from the currently running campaign.

## Development review — progress exception paths and stack envelope, October 26, 2026 UTC

At 15:22 UTC, `/root/i4v_contract_review` independently inspected the emitted
runtime from `i4v-progress-dev-20261026-04` without rebuilding or executing a VM.
The exact ELF SHA-256 is
`f218c13c95cc232f1d31492f88481513c7b42ec3c6b41dc7441db3f7ddd33de8`.
**This is a developmental static review, not checkpoint acceptance.** Run04
remains an immutable failed diagnostic: its native partial-helper execution
completed, but its captured oracle omitted the expected `pending_events` field.
Run05 has newer inputs and requires its own emitted-code and evidence checks.

The earlier disassembly audit and an additional scan covering generic symbols
and all downward SP adjustments agree on the largest conservative direct chain:
**15,040 bytes of the 16,384-byte stack**, leaving 1,344 bytes. The chain is
capture (800), `finish` (32), `concurrent::completed` (32), `queued` (1,744),
`maintenance::finish` (2,528), `maintenance::drive` (8,224), `stack::start` (16),
and `stack::enter_step` (1,664). Stack-reset gates terminate this accounting;
tail transfers conservatively retain their caller frame. The existing
profile-18/19 exception continuation is bounded by its one-shot handled flag,
and `source_bytes` generation two can recurse into generation one. Accounting
for those bounded paths does not raise this ceiling. The scan found SP
assignments only in the known boot/entry/restore gates; indirect calls are the
formatting callbacks outside these admitted execution paths. This is a finite
emitted-path envelope, not a general proof for arbitrary future dispatch or
exception nesting.

The new paths fit comfortably within that existing maximum:

| Path in the producing ELF | Conservative bytes |
| --- | ---: |
| Partial-helper initialization, including `runtime_main` | 3,600 |
| Generic exception capture through partial finish/deferred validation | 2,560 |
| Atomic ordering finish subtree | 560 |
| Atomic recovery subtree | 464 |
| Atomic trial-entry subtree | 384 |
| Containment deferred-gate subtree | 1,280 |
| Containment witness subtree | 192 |

Both actual partial owners saved SP `0x800040053450` at both faults: exactly
2,992 bytes below the normal stack top, consistent with the emitted retained
helper continuation. The ELF's two RX loads contain 168 and 223,724 bytes,
within the separate boot-page and 256-KiB resident-text reservations.

The compressed partial-N2 regression fixture exactly matches the immutable
run04 report, and both recorded report/ELF hashes verify. The corrected
standalone partial oracle and its five tests pass, including read/sample/route,
canonical-prefix, and missing/duplicate blocked-event corruptions. Current
full scheduling-schema validation rejects that historical report because it
predates the newly required `supervision.host_pid`; no field was synthesized
to turn it into current-schema evidence.

The reviewer separately **authored** the expanded `--partial` ELF validator:
all 47 generic exception-capture instructions, all 16 vector entry branches,
the boot path installing VBAR and both stacks, the exact STRB/RET helper, and
the existing complete ERET restore. Its 11 tests pass and it accepts the exact
run04 ELF; every reported helper fault PC binds to that producing instruction.
Root independently reviews this new validator. Authorship of that extension
is kept separate from the independent emitted-stack review above; neither
unit results nor this diagnostic establish completion of the new progress or
containment campaign.

## Intermediate I4-V return — translation coherence, October 26, 2026 UTC

`/root/i4v_upstream` authored the coherence ABI/runtime and the preceding Python
translation reference; root authored the host/compiler/runner integration and
actual producer probes. `/root/i4v_producer` authored the separate native-report
replay and corruption controls. `/root/i4v_contract_review` reviewed the model,
implementation and oracle, audited actual compiler artifacts and emitted code,
and owned four serialized fresh reproduction/regression campaigns. **ACCEPT
this finite parent/A-D/page-size coherence checkpoint. Full I4-V remains
incomplete.** The report oracle is independently authored; the earlier reference
model and resident implementation share an author and are not represented as
two independently authored implementations.

Primary `i4v-coherence-dev-20261026-04` passes 142 jobs/104 native cases:
60 coherence cases and 44 preserved baselines. Independent audit verifies all
386 actual exports, 398 report associations, 360 coherence deliveries, 8,891
checksums and 776 producing inputs. The fresh reviewer-owned runs are:

| Retained run | Jobs | Native cases or legacy reports | Verified checksums | Seconds |
| --- | ---: | ---: | ---: | ---: |
| `i4v-coherence-independent-20261026-01` | 316 | 278 cases | 18,907 | 424 |
| `i4v-coherence-independent-20261026-02` | 432 | 394 cases | 45,163 | 837 |
| `i4v-coherence-independent-20261026-03` | 142 | 104 cases | 23,611 | 424 |
| `i4v-coherence-regression-independent-20261026-01` | 87 | 70 legacy reports | 2,551 | 50 |

The three I4-V case lists have an exact union of **688 distinct native cases:
all 628 earlier cases plus 60 new coherence cases**. Their 776 executions include
44 identical baselines in each bundle, hence 88 repeated baseline executions.
The final bundle preserves the exact 87-job legacy list and its count-rejection
checks. Every process was polled to successful termination before the next
campaign began; execution was serialized from 14:10:49 to 14:40:41 UTC.

All 90,232 replica checksums verify. The I4-V bundles contain 4,586 actual exports
and 4,622 checked report associations, including exact resident request,
compiler-source/mode/RIP and delivered-reply bindings. Each bundle's 776 inputs
matches the final primary and workspace at audit time. The legacy snapshot has
119 inputs: 95 shared inputs match the producing primary and 24 legacy-only
inputs match both the workspace and preceding legacy snapshot. Its fragment and
two producer binaries match the specified durable pins exactly. No FEX rebuild
is claimed. All four fresh runtime ELFs equal SHA256
`b02eb91194a6820a2c9fd8fa4bf0422d83346f9df9c6fc87e1940529b99d1cde`.

The six profiles run N=1/2/3/4 with every mutation-actor position. Across the
60 cases, 180 owner instances perform 2,880 visits, 2,520 commits, 2,160 native
faults, 1,800 retries, including 720 retained-route retries, and 360 actual
compilations. Replay checks 1,400 full memory/table snapshots, 6,840 ordered
walks, 20,460 recorded table reads and 650 table writes. Counts support the
individual full-state checks; matching aggregate counts alone cannot pass.

The parent profile makes PT-A/PT-B/A route changes observable by swapping two
data pages, then restricts parent write permission. A/D profiles cover legacy32,
legacy PAE and four-level paging: warm dirty translations execute cached writes
after guest A/D clearing without a data rewalk; explicit invalidation clears the
recorded native history, fresh reads establish A without D, and subsequent first
writes establish D. Page-size profiles promote 4KiB leaves to a 2MiB leaf,
demote them and restrict a large leaf. Both native 4KiB subpages and low/high
aliases are checked where admitted. High aliases execute actual 64-bit-address
DWORD FEX instructions; separately retained compile-only probes verify four
exports and all 46 checksums, without relabeling protected32 producer output.

All owners detach real native faults before FIFO retained-route service. The
old operation completes exactly once through its saved physical route before
table mutation. Delayed final acknowledgements reject entry until required
local invalidations complete, including an N1 self-delay control. PAE backing
PDPT memory changes to a different valid route after the hidden snapshot is
loaded; every later walk retains the old snapshot, with exactly one four-entry
load per owner. Source bytes remain immutable and independently fetched through
the current guest translation before each generated entry.

Review found missing private upper-table mappings and TTBR1 links; the author
fixed and tested all four pages and owner-specific links before the first native
diagnostic. Review also replaced hard-coded starting code/leaf observations with
actual host captures and added a terminal reclamation rejection with execution
finished, all acknowledgements complete and only the actor's final code pin
held. Event 75 and unchanged code bytes establish this rejection separately from
the earlier unfinished-program guard. After the final release, actual private
code bytes are cleared and cached entry is rejected. Guest RAM/table backing
remains owned: physical recycling of that backing is not claimed.

The three exploratory diagnostics retain their original failed manifests because
their captured public checkers were closed. The first predates the terminal pin
control. Corrected N4 PAE-size and N3 four-level A/D diagnostics were independently
replayed with their actual compiler deliveries before public enablement. The
final strict checker always binds requested profile/count/actor and requires the
complete replay, schemas, terminal pin test and full state. Its 28 oracle tests
and 14 reference tests include corrupted routes, hidden roots, page sizes,
A/D histories, native syndromes, preserved frames, compiler identities, FIFO
selection, delayed acknowledgements and premature release/reclamation. The
reviewer independently reran all 28 oracle tests and the actual strict AD CLI.

The producing ELF has RX spans of 168 and 201,308 bytes within the split resident
windows. Reviewed emitted stack remains bounded by 14,992/16,384 bytes; coherence
initialize's direct call chain is 1,248 bytes and finish's is 928 before the outer
capture gate. Across the four replicas, peak observed RSS is 543,539,200 bytes
in the 8GiB build/test class and 444,727,296 in the 512MiB worker class. Total
retained replica size is 1,062,915,928 bytes; every bundle meets its 20-minute
bound. Every compiler removal record verifies, the legacy manifest reports no
remaining containers, and the final independent global `natemu-i4v-` query is
empty. No reviewer process remained when the execution slot was returned.

This evidence qualifies the declared finite WB-memory profiles. It does not
claim every legal paging-cache policy, physical guest-backing recycling, seeded
adversarial progress, failure containment, broader partial-helper composition or
whole-emulator correctness. The next reviewed 72 healthy/25 containment proposal
must distinguish N1 capacity coverage, use actor rotation independent of seed
residues, bind eligible seeded decisions to real protocol cuts, preserve original
failure causes and bound cleanup. It must not fabricate normal lock/reference
drain in a VM abandoned after a resident panic. That next proposal and isolated
preparation code are not accepted by this coherence result; final I4-V coverage
inventory and integrated review remain required.

## Intermediate I4-V return — native ordering and LFENCE completion, October 26, 2026 UTC

`/root/i4v_producer` and root authored the retained FEX probes, ABI/runtime/gates
and host/worker/runner integration in their assigned modules;
`/root/i4v_upstream` authored the independent TSO and architectural report models.
`/root/i4v_contract_review` independently reviewed these components, reproduced
consequential validator defects, checked emitted code and artifacts, then owned
four serialized fresh builds/execution campaigns. **ACCEPT this finite native
ordering, aligned XCHG32 recovery and live-result LFENCE checkpoint. Broader I4-V
remains incomplete.** This acceptance supersedes the preliminary status below.

The final primary `i4v-fence-dev-20261026-02` passes 253 jobs/218 cases. The
reviewer verifies all 526 actual exports, 538 report associations, 650 ordering
and fence deliveries, 12,508 checksums and 720 producing inputs. Independent
reproduction then uses the unchanged executable inputs in these fresh bundles:

| Retained run | Jobs | Native cases or legacy reports | Verified checksums | Seconds |
| --- | ---: | ---: | ---: | ---: |
| `i4v-fence-independent-20261026-01` | 253 | 218 cases | 12,508 | 298 |
| `i4v-fence-independent-20261026-02` | 429 | 394 cases | 45,064 | 834 |
| `i4v-fence-independent-20261026-03` | 139 | 104 cases | 23,512 | 394 |
| `i4v-fence-regression-independent-20261026-01` | 87 | 70 legacy reports | 2,526 | 49 |

The three I4-V case lists have an exact union of **628 distinct cases: all 454
earlier cases plus 174 new cases**. Their 716 executions include the same 44
baseline cases in each bundle, hence 88 repeated baseline executions; these are
not additional distinct cases. The fourth bundle preserves the exact earlier
87-job legacy list, including its native reports and count-rejection checks.
Every process exited successfully before the next campaign started. The sequence
ran 13:11:46–13:38:58 UTC without overlapping build/native/compiler campaigns.

All 83,610 replica checksums verify. The three I4-V bundles contain 4,226 actual
compiler exports and 4,262 checked report associations; every resident request
and delivered chunk is tied to its producing artifact. Each has 720 inputs
identical to the final primary and workspace at audit time. The legacy snapshot
has 116 inputs: 92 shared inputs match the final primary, while its 24 additional
legacy tools/fixtures match both the workspace and preceding legacy snapshot.
Its fragment and two producer binaries match the specified durable pins exactly.
No upstream FEX rebuild is claimed. All four freshly built runtime ELFs equal
SHA256 `2ed0ca0a4df66b7046d3ff916c8060cbe4a31cfedc8ff14bf9c839b2a95bb56c`.

The 154 ordering cases cover all 77 declared profile/count/role-rotation
configurations at seeds 1 and 37: 450 owner compilations, 14,400 whole-block
entries, 1,280 designated native faults, 1,920 exchange chains and 5,760 raw
fence-control entries. The independent FIFO TSO model checks the seven litmus
families. The reviewer verifies private trial records, warmed ordinary RAM,
explicit LSE encoding 2 admission, preserved generated bodies and surrounding
DMB SY pairs for the admitted exchanges. Ready/reset/done synchronization stays
outside the participant blocks. The precommit permission fault retries SWPAL;
the postcommit absent-result-page fault resumes only the later STLR. Complete
native frames, unique-token chains and final physical images check exact-once
effects without assuming peers leave the shared word unchanged.

The 20 live LFENCE cases cover N=1/2/3/4 and five input rotations, yielding 50
actual compilations, 200 delivered body chunks and 800 generated entries,
completion sinks and continuations. All five input cases occur within every
owner's 16 trials. The original 120-byte body is preserved around two direct
branches. Full native stores consume the live vector result, sum, deferred AF/PF
sources, NZCV, FPSR and preserved roots before DSB SY/ISB. ERET restores the full
frame, including x16/x30, and resumes the original LAHF/SETO/store suffix. The
final CFINV representation is checked separately. Actual scalar, flag and vector
stores, all output canaries, the pre-suffix poison image and initial/sink/final
frames verify the boundary. The separately recorded semantic STMXCSR preserves
guest sticky status while adding the computed inexact status; it is not described
as FEX-generated STMXCSR. No peer rendezvous occurs inside this local fence.

The emitted-gate validator binds the actual producing ELF, full sink capture,
DSB/ISB, handler continuation, full ERET restoration and allocated return capture.
Every reported sink/return/capture PC is bound to those ELF symbols. Review found
that its first conditional-edge scan omitted TBZ/TBNZ; an in-memory ELF mutation
reproduced acceptance of a bypass. The author rejected that family, BC.cond and
unexpected register/exception transfers and added corruption controls. The
reviewer repeated the original bypass mutations and verified rejection, seven
gate tests and the real emitted ELF. Earlier reserved-LSE admission was also
corrected. The originally closed-checker diagnostics remain failed records;
none was relabeled as an accepted native campaign.

The final ELF occupies RX spans of 168 and 181,860 bytes within the split
resident windows. Conservative emitted stack depth is 14,944/16,384 bytes;
the LFENCE sink/handler needs 832 bytes and initialize needs 1,184. Across the
four fresh bundles, peak observed RSS is 537,968,640 bytes in the 8GiB build/test
class and 442,875,904 in the 512MiB worker class. The bundles retain 891,909,203
bytes in total and individually remain within the 20-minute limit. All compiler
cleanup records verify; the final legacy manifest and independent global query
show no remaining `natemu-i4v-` compiler containers.

This is bounded normal-memory evidence on the named host, not universal TSO or
ISA proof. In replica01, store buffering observed only `(1,0)` and `(0,1)`;
the permitted `(0,0)` outcome was not observed or required. IRIW and WRC exhibit
multiple allowed outcomes, and no forbidden outcome is accepted. Model reasoning,
actual emitted ordering and observed trials remain distinct evidence. LFENCE
qualification covers the admitted positive-normal masked round-to-nearest
exact/inexact inputs and already-stored sticky state; broader FP, device/service,
locked-family, width, alignment and route semantics are not inferred. The earlier
454 cases retain their existing finite limits. Translation-coherence expansion
and remaining integrated recovery/progress obligations still belong to I4-V.

## Preliminary I4-V return — native ordering and atomic recovery, October 26, 2026 UTC

Historical preliminary return; its pending-acceptance status is superseded by
the completed independent return above.

`/root/i4v_contract_review` independently reviewed the ordering ABI, resident
runtime, host mappings/compiler protocol, retained FEX bodies and bounded TSO
model/report oracle. **No remaining static blocker to the exploratory native
campaign; native acceptance remains pending.** This return does not accept the
new checkpoint or complete I4-V.

All 24 retained source/body pairs match the ABI literals, and all 323 checksums
in `i4v-ordering-export-20261026-01` verify. The seven litmus role/pointer mappings
match the independent FIFO store-buffer model, including youngest same-location
forwarding, own-buffer drain before fence/XCHG and indivisible exchange. Its
weakened-ordering controls and the report oracle's forbidden-outcome, full-frame,
RAM, packet, retry and corruption checks are relevant to the finite claims.
The report checker initially remained fail-closed pending actual native/artifact
audit; synthetic coverage of all 77 declared configurations is not native
evidence. It was enabled after the retained positive, precommit and postcommit
diagnostic audits, before primary04 below.

Runtime inspection confirms that ready/release and done/reset synchronization
surround whole participant blocks. Owner0 awaits every completion before the
physical image snapshot and next reset. Trial records are private; ordinary
data, tables and collected images use explicitly shared backing. Unexpected
faults reject ordinary litmus trials. The adapted XCHG permission fault retries
SWPAL at code offset 4; the later absent result-store fault resumes STLR at
offset 16 without replaying SWPAL. Unique-token chain checks do not assume the
shared word remains unchanged while peers execute.

Review identified overbroad admission of reserved Atomic feature encodings.
The author corrected admission to exactly encoding 2 and added all 16 field
controls; the independent atomic oracle rejects the other 15 encodings. The
pre-fix emitted ELF inspected here had RX loads 168/178684 bytes and a conservative
maximum stack of 14992/16384 bytes; ordering initialize needed 1408 bytes and its
finish path 304 plus the 832-byte capture/dispatcher prefix. The reviewer then
checked the corrected producing ELF from focus01 and primary04: both are identical,
SHA256 `2d57608a982671c9389081a97f59c670b3e58990ce38c8644af4c1e050b3f340`,
with the same RX spans and stack bound. Emitted instructions compare the masked
Atomic field to `0x200000` and reject inequality.

The completed `i4v-ordering-dev-20261026-04` primary passes the independent
read-only audit of all 229 jobs/198 native reports, 476 actual compiler exports,
488 report-to-export associations and 11,005 artifact checksums. All 691 retained
inputs equal the workspace at audit time. The exact new matrix is all 77 declared
profile/count/rotation configurations at each seed 1 and 37: 154 ordering cases
plus 44 existing baseline cases. It contains 450 owner compilations and delivered
ordering replies, 14,400 whole-block entries, 1,280 designated native faults,
1,920 serialization chains and 5,760 raw fence-control entries. Baseline eager,
live and mutated-reply reports and their producing associations also verify.

The primary ran 12:34:32–12:38:54 UTC (262 seconds), retaining 298,663,544 bytes.
Classifying resource use by each job's declared limit gives 511,524,864 bytes peak
for the 8GiB build/test class and 201,900,032 bytes for the 512MiB worker class.
The previously reported 454,279,168-byte job was Rust tests in the 8GiB class,
not a 512MiB worker. Every current compiler cleanup record verifies. A subsequent
root global check found one Created/Pid0 container belonging to the old failed
`i4v-address-concurrent-dev-20261026-02` RSS-limit diagnostic. The inspected
`i4v-ordering-cleanup-20261026-01` record retains its identity, successful removal
and empty final global compiler list. This corrects historical residue reporting;
it does not change current runtime evidence or relabel the failed diagnostic.

**The primary development result is supported; checkpoint acceptance remains
pending LFENCE implementation, the preserved 454 native/87 legacy regression
scope and independent fresh rebuild/execution.** The reviewer performed no new
compiler, build or native run for this primary audit.

Raw fence-body controls and their finite capture/completion path do not establish
the full LFENCE live-dependency contract. Finite absence of forbidden outcomes is
not universal memory-model proof, and LSE is reviewed on its own emitted sequence
rather than inheriting the earlier exclusive-loop qualification. All earlier
454 native cases and 87 legacy checks remain required alongside the new campaign.

Follow-up compile-only LFENCE candidate review verifies all 38 checksums in
`i4v-lfence-producer-20261026-01` and identical 120-byte bodies at both retained
RIPs. Before DMB LD at offset 16, live dependencies include q16's FADD result,
x7/x26's sum and parity source, x27's auxiliary-carry source, NZCV and FPSR.
A private full-native-frame sink can consume these dependencies, provided its
direct branch/continuation preserves all roots and restores status exactly before
resuming the original suffix. In particular, ordinary call/entry conventions
must not silently overwrite x30/x16 or deferred flags. LAHF/SETO exposes all six
arithmetic flags; the final CFINV at offset 116 changes their native representation
and must be modeled separately from the boundary capture. A separately admitted
semantic STMXCSR suffix must combine the captured FP status with guest control
and sticky state, without being described as generated STMXCSR. This is a viable
finite implementation seam, **not implemented/native LFENCE acceptance**.

## Intermediate I4-V return — protected32 address formation, October 26, 2026 UTC

`/root/i4v_producer` authored the retained instruction probes and address
ABI/runtime; root authored host/compiler/runner integration; `/root/i4v_upstream`
authored the independent architectural replay. `/root/i4v_contract_review`
reviewed code, emitted stack/layout, oracle and actual artifacts, then rebuilt
and executed fresh retained replicas. **ACCEPT this finite protected32 address
checkpoint. Broader V2 and I4-V validation remains incomplete.**

The primary05 campaign passes 482 jobs/454 native cases, with 3,674 actual
compiler exports, 3,686 report associations, 4,228 delivered records and 65,838
verified checksums. Its 1,145-second wall time is close to the unchanged
1,200-second bundle limit, so independent replication uses two fresh retained
snapshots/builds from exactly the same producing source:

- `i4v-address-concurrent-independent-20261026-01`: 132 jobs/104 cases,
  comprising all 60 new address cases plus 44 baseline cases; 1,306 exports,
  1,318 report associations, 1,280 delivered records and 23,266 checksums.
- `i4v-address-concurrent-independent-20261026-02`: 422 jobs/394 earlier cases;
  2,394 exports, 2,406 report associations, 2,948 delivered records and 44,818
  checksums.

The reviewer verifies exact case-list equality against the selected primary
subsets: their union is all 454 distinct cases and their intersection is exactly
the declared 44 baseline cases. The resulting 498 executions are not described
as 498 distinct cases. Every report and actual compiler/transfer association
passes; all 68,084 replica checksums verify. Both sets of 565 retained inputs
match primary05 and the workspace at audit time. The producer matches the
existing pin and every compiler cleanup record passes. This is fresh generation
from the pinned producer, not another upstream FEX build.

The exact new 60-case matrix covers subpage CS/DS bases, fixed A20-off, linear-base
wrap, address-size16 wrap, terminal DS and terminal CS at N=1/2/3/4 and every
actor position. Across 180 owners it records 1,960 visits,1,940 committed guest
instructions, 1,280 real native faults and checked services,1,280 actual
compilations, 6,116 legacy walks/12,232 table reads/2,034 table writes,
1,620 maintenance heads,240 ordinary programs and 180 canonical event handoffs.
Trace use totals 20,246 events, at most 504/768 per case. All 60 paging-off actors
have zero walks; paged peers use legacy32, not relabeled IA-32e compatibility.

The replay independently separates truncated effective offset, segment-derived
linear address, raw paged/identity GPA, final A20 masking and owned backing.
It checks the actual selected source against the poisoned inactive source page,
per-instruction EIP/context/source-generation identity, complete native/canonical
frames and physical memory/table images. Address16 accesses use the emitted
truncated scratch address and preserve upper BX/SI bits. The terminal actor
commits its ECX-copy prefix, reports #GP(0) at EIP 0x2002 with CR2 unchanged, and
keeps that packet while all peers drain their saved store/load routes. Source
mutation follows old completion; actual deferred COPY replies reject on source
generation even though their body is unchanged. Private old code remains intact
until its own reference release, then is physically cleared. Delayed owner
acknowledgements, FIFO maintenance, eight-head progress, ordinary intervals and
coalesced events retain their required state predicates.

Every memory access in this selected slice deliberately encounters an unmapped
native operand and uses checked service, then resumes after the inspected actual
STLR/LDAR instruction. The native address calculation and continuation execute;
this does not establish a general native shadow mapping for arbitrary subpage
segment bases. The maximum-limit DS #GP is the declared deterministic policy,
not a universal hardware result. Dynamic A20 changes, A20-crossing accesses,
expand-down segments and concurrent device effects remain outside this slice.

The strict checker binds requested profile/N/actor, rejects unknown or malformed
transport fields and validates all reported compiler deliveries against retained
actual exports. Its 23 tests include literal subpage/address16/CS diagnostics,
an explicitly synthetic DS variation, state/syndrome/context/route corruptions,
early source mutation, premature terminal fault/service/stop and strict identity
controls. Review also verifies actual captured pre-run shadow leaves rather than
constant substituted snapshots. Development01/03/04 remain failed diagnostic
bundles because their captured checker was closed. Development02 remains the
preserved RSS-limit failure; the producer now admits only two expensive compiler
lifecycles through shared file locks while up to eight lightweight requests may
remain pending. Lock release does not depend on deferred reply delivery.

Primary and both independent bundles share runtime SHA-256
`70e96c495713ddc03c30427483af8fe246a651f01693b5d93914f8c34b9f3337`.
The emitted RX windows contain 168 boot bytes and 171,964 main bytes; the boot
ADR is in range and conservative stack accounting remains 14,944/16,384 bytes.
Bundle01 runs 11:31:31–11:37:25 UTC (354 seconds), retaining 158,257,566 bytes
with peak build/worker RSS of 517,046,272/388,202,496 bytes. Bundle02 starts only
after its predecessor exits, runs 11:37:36–11:50:24 UTC (768 seconds), and retains
317,257,434 bytes with peaks of 519,028,736/438,501,376 bytes. Each bundle stays
within the unchanged 20-minute and memory limits. Both owning processes exited 0
before the execution slot was released; no compiler containers remain.

Root's separately serialized `i4v-address-concurrent-regression-20261026-01`
passes 87 jobs at 11:29:28–11:30:16UTC. The reviewer independently verifies all 2,477
checksums, the matching runtime and empty remaining-container list. No legacy
rerun or overlapping native/build campaign was needed. Remaining integrated
memory-ordering, coherence, recovery/lifetime and progress obligations retain
their full I4-V scope; canonical event handoff is not guest interrupt delivery,
and these finite execution modes do not establish full kernel boot.

## Intermediate I4-V return — terminal stack faults and split stores, October 26, 2026 UTC

`/root/i4v_producer` authored the boundary ABI/runtime, root authored host and
runner integration, and `/root/i4v_upstream` authored the independent replay.
`/root/i4v_contract_review` reviewed the implementation, oracle, emitted code
and retained artifacts, then independently rebuilt and ran primary04's snapshot.
**ACCEPT this finite V1 checkpoint. The broader V1/V2/V3 and I4-V campaign remains
incomplete; this is not general cross-page native-store or full-emulator proof.**

Independent `i4v-stack-boundary-independent-20261026-01` passes420 jobs and
394 native cells:50 new cases plus344 prior cases. Independent replay and
artifact checks validate all394 reports,2,394 actual compiler exports,2,406
report associations,2,948 delivered transfers and44,724 checksums. The exact
case/job lists and all509 retained producing inputs match primary04; producing
inputs also match the workspace at audit time. The pinned compiler binary,
compiler cleanup records and actual exported-source/body bindings pass; no
compiler containers remain. This is fresh generation from the existing pinned
FEX producer, not a new upstream FEX build.

The exact new matrix is five outcomes at N=1/2/3/4 and every actor position:
late #SS, late #GP, checked split-store success, second-fragment absence and
second-fragment read-only denial. The declared parity assignment selects26 high
and24 low source forms; this does not imply a100-case source-address cross-product.
Across150 owners the boundary wave commits590 guest instructions, records130
native store faults and30 checked-service attempts, of which10 succeed. It uses
1,415 trace events, at most38 per wave. Each case retains three actual source
compilations and nine delivered chunks, totaling150/450 for the new matrix.

The replay validates the explicitly captured before-wave state and the actual
final state separately, including full physical memory, page tables, canonical
and native registers, code and per-owner lower/upper shadow leaves. The late
#SS(0)/#GP(0) packets preserve the committed prefix and CR2. Split readiness
preserves preexisting A/D and lawful new A effects, defers new D and operand
writes until both fragments are valid, and preserves bytes/RSP when the second
fragment fails. Success resumes the unchanged generated continuation after the
checked store service; it does not claim that the original native split store
completed. Retained peers resume their saved routes in FIFO order, keep whole
program grants, and complete before remap, delayed acknowledgements, reference
release and actual code reclamation. Terminal actor state is not overwritten by
maintenance cleanup. Requested profile/N/actor/source form and actual compiler
requests/replies bind every report.

Review found missing explicit S1PTW/CM/FnV rejection in direct-store abort
admission. The runtime and oracle now reject each flag, with negative controls
for actor and peer reports. Deferred-dirty unit controls also check two initially
clean leaves and rejection when the second staged PTE changes before commit.
Development01–03 remain failed diagnostic bundles because their captured
checkers were deliberately closed; successful retrospective inspection does not
rewrite those manifests. Fresh primary04 and the independent campaign use the
completed strict checker and final guard.

Primary04, the fresh independent replica and legacy regression share runtime
SHA-256 `b9370bb261695da4ffd397b1a6399c83a59ec994613eb0a2a9c456c4d92b2095`.
The two RX loads occupy168 boot bytes and154,044 main bytes within reserved
windows; the boot ADR remains valid. Conservative emitted-call accounting is
14,944/16,384 stack bytes. The independent campaign runs10:22:26–10:34:55UTC
(749 seconds), retains312,932,246 bytes and observes peak build/worker RSS of
563,265,536/438,550,528 bytes, within the declared limits. Its owning process
exited0 before the native/build slot was released. Primary04 runs748 seconds;
all campaigns are serialized.

The reviewer separately verified root's prior serialized
`i4v-stack-boundary-regression-20261026-01`:87 passed jobs,2,450 checksums,
10:20:17–10:21:04UTC, identical runtime and no remaining containers. No reviewer
legacy rerun or overlapping native/build campaign was needed. Remaining V2
address formation and broader ordering, coherence, recovery/lifetime and progress
coverage remain required. The canonical event handoff is still a fixture, not
architectural guest interrupt delivery.

## Plan review — protected32 address formation, October 26, 2026 UTC

`/root/i4v_contract_review` independently reviewed the next60-case plan.
**ACCEPT as a bounded implementation plan; no new execution coverage is claimed.**
It preserves prior394 native cases and87 legacy jobs, shared source/data and
retained peer lifetimes, precise committed prefixes, stale reply/entry rejection,
events, delayed acknowledgements, nine-head pressure and ordinary-work fairness.
Actual per-instruction FEX emission must establish recovery sites. Offset,
segment-derived linear address and final physical address remain separate;
fixed A20 masking must be observable for both paging-off actors and paged peers.
Terminal CS fetch reports #GP(0) at the second instruction with CR2 unchanged
before any operand access. The maximum-limit DS #GP(0) policy is correctly
separated from mandatory finite-limit faults: independent inspection of
[Intel SDM087 Vol3A §6.3, pp.6-4–6-5](https://cdrdv2-public.intel.com/850978/253668-087-sdm-vol-3a.pdf)
confirms implementation-specific behavior at effective limit0xffffffff.
The plan explicitly keeps general A20 transitions/split accesses, expand-down
segments and concurrent device effects outside this finite slice while retaining
the broader I4-V obligations. No substantive blocker remains.

## Intermediate I4-V return — concurrent control transactions, October 26, 2026 UTC

Root authored ABI/runtime, coordination and runner changes;
`/root/i4v_producer` authored host/compiler integration and
`/root/i4v_upstream` authored the independent architectural replay.
`/root/i4v_contract_review` reviewed implementation, emitted code, oracle and
artifacts, then independently rebuilt and executed the retained primary10
snapshot. **ACCEPT this finite concurrent-control checkpoint. V3 and I4-V remain
incomplete; this does not establish general guest transition correctness.**

Independent `i4v-control-concurrent-independent-20261026-01` passes369 jobs and
344 native cells:50 new rotations plus294 prior cases. The reviewer rechecked
all344 reports,2,244 actual compilations,2,256 report associations,2,498 delivered
transfers and40,886 checksums. All504 retained producing inputs match primary10
and the workspace at audit time. The producer matches its existing pin; compiler
cleanup records pass and no compiler containers remain. This is fresh generation
from the pinned producer, not a fresh upstream FEX build.

The new matrix covers five families at N=1/2/3/4 and every actor position:
legacy ABA, hidden-PAE ABA, IA-32e ABA, direct paging-off/IA-32e transitions and
rejected CR0. Across150 owners it records1,980 visits,1,950 completed guest
instructions,1,290 native entries,480 native faults,1,500 compilations,
4,546 walks,90 hidden-root loads/360 root reads,2,780 lower-table reads,
340 lower-table writes and180 physical metadata reads. The legal families add
1,080 maintenance heads,160 actual generated progress intervals and150 event
handoffs across all families. N1 is the no-peer control; the rejected supplement
does not claim maintenance pressure. Maximum trace use is586/768; replica trace
total15,779 versus primary15,781 reflects permitted scheduling differences.

The replay checks actual local commits across retained peer accesses, unchanged
peer state, still-current peer replies executing after actor transitions,
context-only ABA rejection, shared-table effects with private hidden roots,
positive current-entry eligibility followed by source-only rejection, and old
reference release before code reclamation. It also checks full canonical/native
frames, physical memory images, FIFO identities, the eight-head progress bound,
round-robin intervals and event eligibility at the actual handoff. Program
revision1 and requested family/N/actor bind the report; every compiler delivery
binds its resident request and actual exported body.

Review corrected two oracle gaps: rejecting actual semantic commits made before
peers detach, and recomputing eligibility when an event is handed off. A later
legal N3 schedule exposed a distinction between another waiter selecting the
FIFO head and that owner actually resuming. The oracle now permits preselection
but rejects execution before the owner's isolation check. All18 checker tests
pass, including the reproduced corruptions. Preserved development05–07 record
the unaligned page-base lookup panic and diagnostics;08 verifies its fix.
Development09 remains a failed partial campaign, not retroactive acceptance.

Primary, independent replica and legacy regression share runtime SHA-256
`e181033a9c36f5d3e4899e8a599a53b1c64a8819b80591a4b2291a78e773ed5d`.
The two RX loads occupy168 boot bytes and143,260 main bytes within their reserved
windows, with a valid boot ADR to vectors. Conservative emitted-call accounting
remains14,992/16,384 stack bytes. Replica execution runs09:21:22–09:32:32UTC
(670 seconds), retains269,570,577 bytes and observes peak build/worker RSS of
590,872,576/438,501,376 bytes, within declared limits. Its process exited0 before
the execution slot was released. Root subsequently ran
`i4v-control-concurrent-regression-20261026-01`,09:32:54–09:33:41UTC; the reviewer
verified its passed87-job manifest and identical ELF. No builds/native campaigns
overlapped, and the reviewer did not duplicate the legacy execution.

Remaining V1 terminal/split-access concurrency, V2 address forms and broader
memory-model, coherence and lifetime validation remain in the full I4-V scope.
The event mechanism is a canonical-state handoff fixture, not guest IDT delivery;
bounded modes, operand mappings and descriptors do not imply full kernel boot.

## Plan review — terminal stack faults and split stores, October 26, 2026 UTC

`/root/i4v_contract_review` independently reviewed the next bounded V1 contract.
**ACCEPT as an implementation plan; no new execution result is claimed.**
It preserves committed prefixes and precise #SS(0)/#GP(0) state, including
unchanged CR2, while peers drain and maintenance uses saved routes without
mutating the terminal actor's canonical state. Both split-store fragments share
one admitted context/lifetime; readiness preserves existing A/D, admits lawful
new A effects and defers new D effects until both fragments are ready. The
checked-service success/fault contract is explicitly separate from uncertain
native cross-page partial-store behavior. Disjoint backing and expectations
derived from prior memory effects prevent fixture resets from hiding errors.

Five outcomes across all N/actor positions form50 cases; low/high source forms
must be reported per case, without implying a100-case address cross-product.
Existing344 native cases and87 legacy jobs remain regressions, and the broader
I4-V obligations remain active. No substantive plan blocker remains after the
state-preservation and shared-lifetime clarifications.

## Development review — concurrent source lifetime, October 26, 2026 UTC

`/root/i4v_contract_review` independently audited retained
`i4v-control-concurrent-dev-20261026-04`, the N=3 legacy-control program with
actor1, without another build or native run. All three owners report runtime
success and17 completed instructions. All42 actual compiler requests, exported
bodies and delivered replies bind to the resident records;2,070 retained
checksums pass. **This is development evidence, not checkpoint acceptance:**
the retained manifest remains failed because the unfinished full oracle fails
closed. Maintenance, events and the complete role/profile matrix remain pending.

The source-only rejection now has a positive control: each owner's current
context/body is published in the same final visit slot and is eligible before
mutation. Its source-generation change then rejects both that entry and the
actual old reply. Trace ordering places all source acknowledgements before
reference release and all old-reference releases before any private code clear.
The recorded cleared images are zero; fresh generation2 bodies actually execute
MOV17 followed by STORE17 in each owner's data span. Only the expected source
immediate byte changes in each256-byte source span. Peer local-control and held
reply snapshots remain equal across the actor's commits. Old and new reference
masks both finish zero; new admissions no longer repopulate old references.

The audited ELF SHA-256 is
`b951f88909c8f16983fe84d4d681a124579b3bacee50f20d0365e86c7b511acb`.
Its two RX loads occupy168 boot bytes and137,468 main bytes within the declared
windows; the boot ADR reaches the actual vectors. Conservative emitted-call
accounting remains14,992 bytes against the16,384-byte stack. The new source-cut
frame is224 bytes and publication is outlined. No substantive blocker was found
in this development cut; fresh complete-oracle replication and legacy regression
are still required before accepting the concurrent checkpoint.

## Plan review — concurrent guest control transitions, October 26, 2026 UTC

`/root/i4v_upstream` independently checked the recorded
[next-slice contract](../investigations/INV-03-01.md#next-i4-v-slice--concurrent-guest-control-transitions)
against the proposed schedule and existing concurrency boundaries.
**ACCEPT as an implementation plan; no new execution evidence is claimed.**

The contract separates a retained peer access across a local control commit
from coordinated shared-source/table mutation after route drain. It requires
unchanged peer architectural state, exactly-once retained completion, admission
and actual execution of a still-current peer reply, and context-only stale-reply
rejection after actor ABA. A separate pending compilation crosses source mutation;
the plan does not replay a committed store to demonstrate peer admission.

Per-owner route derivation preserves hidden PAE lifetime; remappable descriptor
and far-pointer metadata requires both backing copies and actual read GPAs.
Role rotation, N=1 limits, pending events, real ordinary-instruction progress,
bounded fairness and retained-reference reclamation remain explicit. Rejected
CR0 profile3 supplements the legal families at N=1/2/3/4 without claiming the
short rejection program supplies the longer maintenance schedule. Direct
paging-off-to-IA-32e remains primary; PAE composition is separate.

No substantive correction remains in this plan review. Implementation,
independent semantic review and fresh execution replication are still required.
Remaining V1/V2 boundaries and broader memory-model validation stay within the
full I4-V objective; this entry does not complete that objective or V3.

## Intermediate I4-V return — long-mode transactions, October 26, 2026 UTC

Root authored ABI/runtime and runner changes, `/root/i4v_producer` authored
host/compiler integration, and `/root/i4v_upstream` authored independent expected
effects. `/root/i4v_contract_review` checked the architectural transaction rules,
implementation, oracle and emitted code, then independently rebuilt and executed
primary02's retained snapshot. **ACCEPT the finite N=1 IA-32e transaction
checkpoint; broader V3 and I4-V remain incomplete.**

Independent `i4v-longmode-independent-20261026-01` passes318 jobs and294 native
cells, retaining277 prior cells and adding17 programs. The reviewer independently
rechecked all294 reports,744 actual compilations,756 report-to-compile
associations,998 delivered transfers and16,495 checksums. All498 retained inputs
match primary02 and the workspace at audit time. The reused FEX binary matches
its pin and no compiler containers remain. Subsequent documentation updates do
not change execution evidence.

The17 new programs contain169 visits,155 completed guest instructions,81 native
entries,13 native faults,71 guest walks and73 context invalidations. All82 new
actual compilations and delivered replies bind source, EIP, context and decode
width:76 use32-bit decoding and six use64-bit decoding. Reports check15 descriptor
reads, four pointer reads and two explicit fixture mutations. Fourteen terminal
guest faults comprise ten#GP, one#NP, one#UD and two#PF.

Direct entry follows paging-off → IA-32e compatibility →64-bit execution →
compatibility → paging-off. The separate legacy-PAE composition retains its
loaded hidden roots without treating PAE execution as a boot prerequisite.
Generated64-bit loads return0xa5a5a5a511220000; subsequent32-bit loads return
0x11220000. The return-to-context case revisits the same numerical controls and
source PC with context5, rejecting context1's entry and its actual delayed reply,
including a copy with only the serial rebound. Native bodies execute actual FEX
output in their requested width; privileged controls and far transfers belong to
the resident semantic owner.

The resident reads the actual far pointer and GDT descriptor through the current
guest translation before committing CS/RIP/context. Pointer#PF retains CS64 and
the old RIP; target-fetch#PF after successful transfer retains committed CS64 and
target RIP. Noncanonical-target#GP preserves CR2 and the earlier generated load.
The finite direct-code checks distinguish selector-limit/type/privilege and L/D
errors from nonpresence, and target errors from selector errors. Immediate far
jump in64-bit mode produces#UD. MOV-CR/WRMSR checks preserve state on prohibited
LME/PG/PAE combinations,16-bit-TSS activation and other admitted rejection cases.
These rules were independently checked against
[Intel SDM Volume2A, JMP](https://cdrdv2-public.intel.com/922480/253666-092-sdm-vol-2a.pdf),
[Volume2B, MOV Control Registers](https://cdrdv2-public.intel.com/922481/253667-092-sdm-vol-2b.pdf)
and [Volume3A §5.1.2](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf).

Review found one oracle priority error before the full run: combined L=D=1/P=0
must produce#GP(selector) before the presence test can produce#NP. The author
corrected the independent model and added a combined test preserving CS/RIP/
context. All17 checker methods pass independently. Focused
`i4v-longmode-dev-20261026-01` passes with1,506 checksums and eight actual
compilation/delivery associations independently verified. Its valid-descriptor
trace is unaffected by the later oracle correction; full primary02 and the fresh
replica contain that correction and supply acceptance evidence.

The specification accurately describes the finite capture policy: outside64-bit
mode, the first four canonical GPR roots receive their low32 bits on native
capture. This is a deterministic policy for undefined upper halves, not a
preservation promise across compatibility execution. Full64-bit capture and
32-bit destination results are checked separately. Legal descriptor forms outside
flat, accessed, nonconforming admission remain Unsupported. Descriptor accessed-
bit writes, full guest IDT delivery and general all-address invalidation are not
claimed. Pinned KVM `test_ljmp` contributes the memory far-pointer expectation;
its retained excerpt, license/hash and adaptation explanation explicitly separate
that source from project-authored mode changes and rejection schedules.

Primary02, the independent rebuild and legacy rebuild share runtime SHA256
`0253d559816b9c497747282315034c26e32fab144ce87e54d85512f0e7d5b98b`.
The two RX loads contain168 boot bytes and117,100 main-image bytes. Boot ADR
resolves the aligned vectors inside the main window; added state/table regions
remain disjoint. Conservative maximum return/exception stack use remains14,992
of16,384 bytes, leaving1,392. New far-transfer/read helpers use176/48-byte frames;
controls finish uses176 and dispatch/compile use1,872/1,232. Root's serialized
`i4v-longmode-regression-20261026-01` passes87 jobs from07:29:27–07:30:10UTC;
the reviewer inspected its terminal manifest and matching ELF without claiming
a second independent legacy execution.

The independent run lasted287 seconds,07:24:25–07:29:12UTC, retaining171,101,816
bytes. Peak observed build/worker process-group RSS was457,310,208/388,415,488
bytes, within the limits. No substantive blocker remains for this finite scope.
Concurrent mode/hidden-state transitions, remaining V1/V2 address and boundary
cases, broader descriptor/exception behavior and the integrated memory-ordering/
coherence campaign remain required. This is neither full V3/I4-V completion nor
a performance result.

## Intermediate I4-V return — hidden PAE state, October 26, 2026 UTC

Root authored ABI/runtime and runner changes, `/root/i4v_producer` authored
host/compiler integration, and `/root/i4v_upstream` authored the independent
expected-effects model. `/root/i4v_contract_review` reviewed the transaction,
walker, compiler bindings and emitted stack usage, then independently rebuilt
and executed primary02's retained snapshot. **ACCEPT the finite N=1 hidden-PAE
checkpoint; V3 and I4-V remain incomplete.**

Independent `i4v-pae-independent-20261026-01` passes301 jobs and277 native cells,
preserving268 prior cells and adding nine PAE programs. The reviewer independently
rechecked all277 reports,662 actual compilations,674 report-to-compile
associations,916 delivered transfers and15,022 checksums. All470 retained inputs
match primary02 exactly; only the producer README differs in the subsequent
workspace. The reused FEX executable matches its pin and no compiler containers
remain. Specification, README and current-investigation wording consistently
separate the finite result from required later coverage.

The nine programs contain78 visits,74 completed guest instructions,49 native
entries and20 native faults, including two permission faults after read-only
native fills. All50 new actual compilations and delivered replies bind EIP,
source, mode and context. The reports independently establish16 hidden-load
attempts,13 successful publications,64 root reads,116 lower-table reads,35 A/D
writes,seven fixture mutations,one guest INVLPG and three explicit native
evictions. No root A/D write is fabricated. The root at0x1020 verifies32-byte
alignment independently of page alignment; nonpresent root payload is accepted
at load and faults at subsequent fetch without following a directory.

The four candidate entries are staged and validated before controls, hidden
snapshot and generations commit. Invalid fourth-entry activation leaves the old
mode and prior store intact. Failed active CR3 and CR4 reloads preserve the old
snapshot and control/context state. An explicit one-shot handler fixture records
#GP, skips the rejected MOV without counting it completed, evicts the disposable
native operand mapping and resumes actual generated code. Its next load faults
natively and returns the earlier stored0x10011 from GPA0x100000, although root
memory now points to the alternative directory and has an invalid fourth entry.
This tests old-root recovery without claiming guest IDT delivery.

The distinction follows accepted claim41's staged hidden-state contract. Intel
§5.4.1 specifies that an invalid present candidate causes#GP and is not loaded;
old-snapshot preservation is the selected inference from that rule and restartable
fault semantics, not a quotation asserting hidden validity-bit behavior. This
clarifies the earlier review phrase “leaves the PDPTE set unloaded”: it describes
rejection of the candidate, not clearing a previously loaded set. Native cache
contents need not survive the failure. Exact four-read ordering is the runtime's
policy, not a claim about hardware bus transactions.
[Intel SDM Volume3A §§5.4.1 and7.4](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf).

INVLPG, internal eviction and WP changes retain the hidden snapshot; required
same-value CR3 and PGE changes reload it. The A/B/A program returns to the same
source PC and numerical controls with contexts3/4/5, rejects the old eligible
entry, and rejects an actual delayed reply both originally and with only its
serial rebound. Mode2 remains a distinct compiler identity while FEX decodes
ordinary instructions at32-bit width. Existing instruction/permission fixtures
retain their attribution; hidden-root schedules and the handler are authored
project tests, not claimed imported upstream PAE tests.

Before collection, review tightened the control-state extent assertion to
protect adjacent PAE state at0x24000. A fourth ABI test covers2-MiB translation,
reserved alignment, NX fetch denial and supervisor WP denial/bypass. Native
coverage remains4-KiB/NXE-off. The checker correctly distinguishes permission
faults from missing-native-mapping faults and has negative controls for both;
all13 checker test methods also pass independently. Focused
`i4v-pae-dev-20261026-01` passes with all1,448 checksums and seven actual
compilation/delivery associations independently verified.

Primary02, the independent rebuild and serialized legacy rebuild share runtime
SHA256 `0c5bc1dfd93894537d5e993190acf95e2576dc557040f21d3e811e6c35999211`.
The two RX loads contain168 boot bytes and105,100 main-image bytes; boot ADR
resolves the aligned vector table inside the main window. Conservative maximum
return/exception stack use remains14,992 of16,384 bytes, leaving1,392. The
controls dispatch/compile/exception frames use1,872/1,232/48 bytes; the bounded
handler admits only one extra dispatch activation and remains below that limit.
Root's `i4v-pae-regression-20261026-01` passes87 jobs from06:44:09–06:44:52UTC;
the reviewer inspected its terminal manifest and matching ELF, without claiming
another independent legacy execution.

The independent run lasted255 seconds,06:39:33–06:43:48UTC, and retained
152,554,029 bytes. Observed peak build/worker process-group RSS was
445,136,896/409,042,944 bytes, within the limits. No substantive blocker remains
within this scope. IA-32e and far transitions, concurrent control/hidden-state
schedules, general address/invalidation forms, remaining V1 boundaries and the
broader ordering/coherence campaign remain required. No full V3/I4-V or
performance acceptance follows.

## Intermediate I4-V return — first guest control transitions, October 26, 2026 UTC

Root authored ABI/runtime and runner changes, `/root/i4v_producer` authored
host/compiler integration, and `/root/i4v_upstream` authored independent expected
effects. `/root/i4v_contract_review` reviewed contracts, code and emitted stack
usage, then independently rebuilt and executed primary02's retained snapshot.
**ACCEPT the finite N=1 protected32/legacy32 control-transition checkpoint;
V3 and I4-V remain incomplete.**

Independent `i4v-controls-independent-20261026-01` passes292 jobs and268 native
cells, preserving255 prior cells and adding thirteen control programs. The
reviewer independently rechecked all268 reports,612 actual compilations,
624 report-to-compile associations,866 delivered transfers and14,115 checksums.
All444 retained inputs match primary02 exactly; only the producer README received
a subsequent workspace update. The reused FEX executable matches its pin and
no compiler containers remain.

The thirteen programs contain79 visits,71 completed guest instructions,55 native
entries,21 native faults and16 successful control invalidations. All56 new actual
compilations and delivered replies bind the requested EIP, source, mode and context.
The resident semantic owner executes fetched MOV-to-CR0/CR3/CR4; the surrounding
ordinary MOV-immediate/load/store instructions execute actual FEX output. Four
#GP and two #UD cases preserve prior generated stores and the rejected control's
old state. WP denial produces #PF3 without undoing its earlier permitted store;
the next-fetch #PF0 retains the successfully committed CR3=0x3000. Full guest
tables, source/data pages, canonical state and native frames are independently
checked at the recorded boundaries.

The A/B/A/off roundtrip returns to the original source PC and numerical controls
with context5, rejects the original context1 cache entry, and rejects an actual
deferred reply even after its serial alone is rebound to the current request.
Same-value CR3 reload is observable: an explicit fixture writer changes the PTE
while the old native leaf remains warm, and the post-reload load reaches the new
physical page. PGE and WP changes use the same admission/commit/invalidation
boundary. This is actual guest control-instruction execution through a semantic
adapter, not reuse of FEX's userspace privileged-instruction behavior.

Review corrected the architectural CR4 mask before native collection: already
exposed FSGSBASE/OSXSAVE controls must return Unsupported while unimplemented,
rather than a fabricated reserved-bit #GP. Separate tests preserve the old state
and generation on rejection. MOV-CR's ignored ModR/M.mod and non-64-bit operand
width are exercised; arithmetic-flag preservation is expressly a deterministic
implementation policy because those flags are architecturally undefined on
successful MOV CR. These instruction premises follow
[Intel SDM Volume2B, MOV to/from Control Registers](https://cdrdv2-public.intel.com/922481/253667-092-sdm-vol-2b.pdf).
CR3 low ignored bits are excluded from root selection; this checkpoint does not
claim MOV-from-CR3 readback equivalence.

The initial focused `i4v-controls-dev-20261026-01` remains failed provenance,
with all1,500 checksums verified. Its checker incorrectly expected the common
initializer's RW/NX source alias to be absent; the correction requires its exact
descriptor before/after and rejects missing, erased, misrouted and executable
variants. The runner also needed to compare the request list with
`compiler_requests`, separately checking the numeric request count. The retained
trace rechecks successfully, including13 actual compilation/delivery associations;
fresh primary02 and the independent run provide acceptance evidence. No runtime
change was needed for these two corrections. All nine checker test methods pass
independently.

Invalidation covers the admitted operand leaf. The static source alias is not
reachable by admitted generated memory operands, and every source fetch checks
the current guest translation before reading private backing. This finite result
does not establish general all-address shadow invalidation. The ABI and README
clarifications preserve that limit and distinguish control rejection from a later
fetch failure after successful commit.

Primary02, the independent rebuild and subsequent legacy rebuild share runtime
SHA256 `39f33e5c7c630805acfc6eae5602aa6a3d636ad9c8012702a702923d78f0a8ab`.
The two RX segments contain168 boot bytes and101,188 main-image bytes. Conservative
maximum return/exception stack use remains14,992 of16,384 bytes, leaving1,392;
the controls completion subtree uses3,920 bytes before capture/common callers.
Root's serialized `i4v-controls-regression-20261026-01` passes87 jobs from
06:15:13–06:15:56UTC. The reviewer inspected its terminal manifest and matching
ELF, without claiming another independent legacy execution.

The independent run lasted236 seconds, from06:11:00–06:14:56UTC, and retained
142,189,661 bytes. Observed peak build/worker process-group RSS was
434,552,832/350,519,296 bytes, within the limits. No substantive blocker remains
within this finite scope. Hidden-PAE load/reload/failure lifetime, IA-32e and far
transitions, concurrent transition/ABA schedules, wider address forms and the
remaining V1/broader validation campaign remain required. No full V3/I4-V or
performance acceptance follows.

## Intermediate I4-V return — mixed paging-mode concurrency, October 26, 2026 UTC

Root authored ABI/runtime and runner changes, `/root/i4v_producer` authored
host/compiler integration and `/root/i4v_upstream` authored the independent
oracle. `/root/i4v_contract_review` reviewed source, contracts and emitted code,
then independently rebuilt and executed primary02's retained snapshot.
**ACCEPT the finite flat protected32/compatibility32 mixed-mode checkpoint;
V2 and I4-V remain incomplete.**

Independent `i4v-mixed-modes-independent-20261026-01` passes277 jobs and255 native
cells, preserving247 prior cells and adding both mode arrangements at N=1/2/3/4.
The reviewer independently rechecked all255 reports,556 actual compilations,
568 report-to-compile associations,810 delivered transfers and12,952 checksums.
All310 retained inputs match primary02 exactly; subsequent workspace differences
are limited to the producer and fixture READMEs. The reused FEX executable matches
its pin, and no compiler containers remain.

The eight new cases cover20 CPU instances: ten paging-off owners perform zero
guest walks, while ten paged owners perform348 checked walks. They execute68
programs with68 native store faults,180 maintenance heads and60 contextual
compilations/deliveries. The oracle derives complete physical memory histories,
native frames, canonical event/final state and shared guest-table effects from
the declared instructions and mode assignment. It retains the FIFO, capacity,
delayed-acknowledgement and at-most-eight-head fairness replay. Paging-off owners
retain identity routing while paged owners follow shared leaf remaps; deliberate
maintenance writes to guest PTEs are fixture actions, distinct from implicit
walk A/D updates. Native read-only identity mappings produce internal repair
faults without inventing guest dirty bits or guest page faults.

Compatibility32 uses the existing four-level walker only after exact control,
flat-segment and VM-wide A20-on admission. CS.L=0/CS.D=1 changes decode defaults
while retaining IA-32e paging, as specified in
[Intel SDM Volume3A section11.8.5.3](https://cdrdv2-public.intel.com/850978/253668-087-sdm-vol-3a.pdf).
Each owner compiles the shared physical source with its own mode/context identity
and private native code slots. All admitted old stores complete before source
mutation; actual deferred replies are rejected against replacement identity,
every replacement publishes before release, and private old-code reuse is blocked
until its owner's retained reference is released. Wrong-owner/mode replies leave
the replacement unpublished. Source writes remain explicit runtime fixture
writes, not translated self-modifying guest instructions.

Review found and corrected three issues before native collection: TAKE/POLL used
the replacement identity instead of the deferred job's original identity;
ordinary-interval faults omitted canonical materialization of the committed MOV
prefix; and the early private-code reuse control did not exercise a guarded
reclamation helper. The corrected implementation drains the actual old request,
materializes EIP+2 and ECX before retry, and checks unchanged old bytes after a
rejected reuse attempt. Compiler resource wording now matches the bounded
per-owner worker topology. The independent reviewer also ran all six mixed-oracle
test methods successfully.

Primary02, the independent rebuild and serialized legacy rebuild share runtime
SHA256 `66221dfc86504704edd1bdf52b8c96c37e360ab918e9e3c0a07ad79630c9bb00`.
The two RX segments contain168 boot bytes and87,940 main-image bytes; emitted
return/exception stack usage is conservatively bounded at14,992 of16,384 bytes,
leaving1,392. Root's `i4v-mixed-modes-regression-20261026-01` passes87 legacy jobs
from05:34:07–05:34:49UTC; the reviewer inspected its manifest and matching ELF,
without claiming another independent legacy execution.

The independent run lasted213 seconds, from05:30:15–05:33:48UTC, and retained
130,546,128 bytes. Observed peak build/worker process-group RSS was
461,291,520/385,138,688 bytes, within the declared limits. No substantive blocker
remains within this finite scope. Mixed nonflat segmentation, A20-off and wider
address/service profiles, actual guest mode/control transitions, context-generation
reuse and remaining V1/broader validation remain obligations. This checkpoint
does not establish full V2/I4-V correctness or performance.

## Intermediate I4-V return — protected32 instruction boundaries, October 26, 2026 UTC

Root authored ABI/runtime and runner changes, `/root/i4v_producer` authored
producer/host integration and `/root/i4v_upstream` authored independent expected
effects. `/root/i4v_contract_review` reviewed the contracts, source and emitted
code, then independently rebuilt and executed primary02's retained snapshot.
**ACCEPT the finite N=1 protected32 per-instruction checkpoint; V2 and I4-V
remain incomplete.**

Independent `i4v-off-steps-independent-20261026-01` passes267 jobs and247 native
cells, preserving212 prior cells and adding35: ten address/service profiles and
25 malformed-reply controls. The reviewer rechecked every report,496 actual
compilations,508 cell-to-compile associations,750 delivered transfers and11,841
checksums. All269 retained inputs match primary02; only the producer README
subsequently changed in the workspace. The reused FEX executable matches its
pin and no compiler containers remain. The separate27-compilation discovery and
disassembly bundle also has224 verified checksums; the native campaign freshly
compiles each requested instruction at its actual architectural EIP.

The new cells contain246 instruction compilations/deliveries,221 committed
prefixes and76 native faults:70 owned-RAM repairs and six device/hole services.
The independent oracle checks all native and canonical prefixes, three complete
data pages, both complete source pages, unchanged poisoned table backing and
zero guest walks. DS-limit and read-only denial preserve two instructions at
EIP0x200a; both later CS-limit cases preserve four at0x2011; first-fetch denial
preserves none at0x2000. Each reports #GP/error0 with CR2 unchanged. Late rejected
replies preserve earlier code/effects and leave the rejected slot unpublished.

Nonzero CS/DS bases, A20 masking and32-bit segment-base addition wrapping use
distinct owned physical backings. Review found that initially identical source
bytes in both A20 source pages would not expose a wrong backing selection. Before
native collection, the host seeded only the selected source page and poisoned
the inactive page; the independent oracle checks both pages and rejects swapped,
duplicated or incorrectly addressed source data. Native FAR remains the generated
operand offset, while recorded linear/GPA identities reflect segment/A20 rules.
Matching native/physical page offsets is an explicit admission condition.

Each original guest instruction is separately compiled; no whole-block recovery
boundaries are guessed. For device/hole cases, the bounded service completes only
the faulting memory subaccess and resumes at the next native instruction, so the
original generated AX merge still runs. The device retains the declared dword
value; the declared hole discards writes and returns all ones. These fixtures
establish the service boundary, not a complete device implementation.

The reviewed address contract checks segment rights and width-aware limits before
base addition and physical A20 masking. The selected limit cases avoid ambiguous
maximum-limit crossings; broader wrapping behavior remains explicitly untested.
These premises follow [Intel SDM Volume3A, sections3.4,6.3.1 and10.7](https://cdrdv2-public.intel.com/850978/253668-087-sdm-vol-3a.pdf).
The ABI clarification correctly distinguishes architectural EIP, linear fetch,
source GPA and declared native operand form.

The direct common-frame copy is safe for the three capture gates: each source is
a stable native/exception-stack frame disjoint from `Shared.captured`. It removes
the full-frame temporary, reducing the common completion frame to32 bytes.
Primary02, independent replica and subsequent legacy rebuild share runtime SHA256
`897032afde16460ef3055fbe5b0b9dda6be7f3aeb2670b746056075107335e8c`.
The two RX segments contain168 boot bytes and78,932 main-image bytes; the
conservative return/exception stack bound is15,024 of16,384 bytes, leaving1,360.
Root's serialized `i4v-off-steps-regression-20261026-01` passes87 legacy jobs from
05:04:31–05:05:13UTC. The reviewer inspected its manifest and matching ELF,
without claiming a second independent legacy execution.

The independent run lasted193 seconds and retained121,970,202 bytes; observed
peak build/worker process-group RSS was411,353,088/96,272,384 bytes. No substantive
correctness or reproduction blocker remains within this finite scope. Mixed
paged/paging-off N-vCPU execution, general address-size wrapping, maximum-limit
crossings, subpage segment bases, other segment forms, V3 transitions and remaining
V1/broader coverage remain obligations. No V2/I4-V completion or performance
acceptance follows.

## Intermediate I4-V return — first paging-off32 execution, October 26, 2026 UTC

Root authored the ABI/runtime and runner, `/root/i4v_producer` authored host and
worker integration, and `/root/i4v_upstream` authored independent expectations.
`/root/i4v_contract_review` reviewed the complete path and independently rebuilt
and executed primary02's retained snapshot. **ACCEPT the finite N=1 flat
protected32/A20-on checkpoint; V2 and I4-V remain incomplete.**

Independent `i4v-off32-independent-20261026-01` passes230 jobs and212 native
cells, preserving158 prior cells and adding54 paging-off cells: cold, native
read-only and warm mappings at two guest RIPs, plus48 corrupted reply controls.
The reviewer independently rechecked every report,250 actual compilations,
262 cell-to-compile associations,504 observed stack/paging-off transfers and
7,534 checksums. The new slice contributes54 compilations and114 transfers.
All173 retained inputs match primary02. Executable inputs still match the
workspace; the producer README alone received a subsequent documentation update.
The reused FEX executable matches its pin, and no compiler containers remain.

Each live request carries all37 source bytes and complete admitted identity,
including protected32 mode and actual guest RIP. Three chunks are checked before
the complete64-byte FEX body plus RET is published. The body executes without
inserted guest-instruction returns. The independent architectural model checks
all nine instructions, including the16-bit AX merge, while a separate native
model checks scratch roots and complete frames. Sparse upstream PC metadata is
used only for the admitted first-store recovery, not invented later boundaries.

The four cold/read-only executions fault before the first store commits. Their
canonical prefix contains the earlier MOV effects, RAX=0x42 and RDX=0x100000,
at RIP+10; flags and CR2 remain unchanged. All16 bytes captured immediately before
retry retain their initial contents. Identity physical routing repairs the
native mapping and retries the exact store once. The two warm executions need
no repair. All successful executions produce RAX/RCX=0xffff0042 and RDX=0x42,
with the exact four-byte data write and all other page bytes preserved. All
16KiB of poisoned guest table backing remains unchanged and guest walk counts
stay zero. Corrupting any word in any reply chunk leaves all68 code bytes blank
and produces no guest effects, including after earlier chunks were staged.

Source, data and native mapping ownership remain enforced despite disabled guest
paging. Exact mode/segment/A20 admission rejects unsupported profiles rather than
inventing a guest #PF. The ABI clarification correctly separates this physical
path and current I4-V coverage from the original P1–P8 paging experiment.

Primary02, the independent replica and subsequent legacy rebuild have identical
runtime SHA256
`403465e488c3dcd9a9cbc3dc69803fb0987ad573d9c391f88bdd29d7b7214142`.
The two RX segments contain168 boot bytes and72,564 main-image bytes. Emitted
return/exception stack usage is conservatively bounded at15,888 of16,384 bytes,
leaving496 bytes; the paging-off initialization/completion helpers stay outlined.
Root's serialized `i4v-off32-regression-20261026-01` passes all87 legacy jobs
from04:37:06–04:37:46UTC. The reviewer inspected its manifest and matching ELF,
without claiming a second independent legacy execution.

The independent campaign lasted122 seconds and retained97,735,167 bytes; observed
peak build/worker process-group RSS was339,738,624/96,239,616 bytes. No substantive
correctness or reproduction blocker remains in this finite scope. Nonflat
segmentation, wrapping, A20-off, device/hole routing, mixed paged/paging-off
concurrency, V3 transitions and remaining V1/broader coverage remain obligations.
This is neither general protected-mode conformance nor V2/I4-V completion or
performance acceptance.

## Intermediate I4-V return — mixed-permission aliases, October 26, 2026 UTC

Root authored ABI/runtime, layout and runner changes; `/root/i4v_producer`
authored host/loader bindings and `/root/i4v_upstream` authored independent
expectations. `/root/i4v_contract_review` reviewed these changes read-only and
performed a fresh serial rebuild and native replication from primary03's
retained snapshot. **ACCEPT the finite mixed-permission alias and resident-layout
checkpoint; V1 and I4-V remain incomplete.**

Independent `i4v-mixed-independent-20261026-01` passes 174 jobs and 158 native
cells: the prior 142 cells plus 16 parent/leaf permission compositions at
N=1/2/3/4 and low/high source RIP. The reviewer rechecked every report, all
196 actual compilations, 208 cell-to-compile associations, 390 observed stack
transfers and 5,962 checksums. All 157 retained inputs match primary03 and the
reviewed workspace. The reused FEX executable matches its pin; all compiler
artifacts and container cleanup verify. No compiler containers remain running.

The new matrix covers 40 owners, 1,120 paired-route acknowledgements, 360
maintenance heads and 56 ordinary generated programs. Both aliases are walked
independently before repair/acknowledgement: shared GPA identity does not transfer
parent or leaf write permission. All previous prefix, full-frame, physical-memory,
source/chunk identity, events, lifetime and fairness checks remain active. After
maintenance, each owner explicitly rebinds its fixture RSP to the read-only alias
and executes the actual generated first PUSH. All 40 probes take a native
permission fault, then guest #PF/error3, without committing that PUSH or changing
the previous memory effects. Native descriptors, guest ancestor/leaf entries,
CR2/RIP and complete precommit state match independent expectations. The code
reference and active grant remain owned through guest-fault materialization.

Resident code outgrew the original 64KiB reserve. The replacement layout has two
disjoint RX ELF segments: a 168-byte boot entry within the first private page and
a 68,308-byte main image within private offsets 0xa0000–0xe0000. Existing state,
stacks and generated-code slots retain their addresses. Loader admission rejects
spans crossing the gap and non-RX segments; host mapping and continuation checks
use the same two windows, while independent checkers retain their own predicate.
Emitted boot ADR resolves exactly to aligned vectors at offset0xa4800 and BL to
runtime_main at0xa7b78. Primary03, independent replica and the subsequent legacy
rebuild have identical runtime SHA256:
`924329f9a4d864646ea58ecc5364b8251fea2235042c98b1acd5816462c6df0b`.

The failed first mixed focus remains retained with PANIC175. Independent emitted
inspection found that changed inlining had enlarged the shared `finish` frame,
creating an 18,144-byte call chain on a 16,384-byte stack. Explicitly outlining
pressure/recovery completion helpers restores the shared frame to832 bytes.
The accepted ELF's conservative return/exception call-chain bound is15,824 bytes,
leaving560 bytes; runtime entry, dispatch and helper entry bounds are lower.
Future generated-code changes must recheck stack bounds as well as text extents.
Corrected focused02, primary03 and the independent full replica pass.

The independent run lasted95 seconds and retained79,086,730 bytes. Observed peak
build/worker process-group RSS was343,195,648/96,403,456 bytes. Builds and native
campaigns were serialized. Root then ran `i4v-mixed-regression-20261026-01`,
passing all87 legacy jobs from04:22:11–04:22:50UTC; the reviewer inspected its
manifest and matching ELF, without claiming another independent legacy run.
No substantive correctness or reproduction blocker remains within this scope.

Source mutation remains an explicit runtime fixture write; parent-RO profiles
use the writable low source alias. Terminal permission probes run after pressure
completion and do not establish revocation during an in-flight instruction.
Concurrent cross-page/terminal-prefix coverage, remaining V1 boundaries, V2
paging-off execution, V3 transitions and the broader campaign remain obligations.
These are finite correctness results, not full V1/I4-V or performance acceptance.

## Plan re-review — address and mode concurrency, October 26, 2026 UTC

At the owner's plan-update request, `/root/i4v_contract_review` independently
rechecked the current I4-V section and ending status in `docs/implementation.md`,
INV-03-01's current direction and the Track03 charter. **ACCEPT the plan;
evidence validity and decision relevance both pass, with no substantive blocker.**

V1 requires actual high-address fetch/data/stack execution and differing alias
permissions; V2 requires paging-off execution with mixed paged/paging-off peers;
V3 requires actual legal/illegal guest control sequences, precise commit/fault
boundaries and return to a previously used context. Each includes retained work,
source/context identity, stale replies/entries, events and N=1/2/3/4 schedules.
Independent expectations, upstream provenance, bounded slices and explicit
unfinished coverage preserve build-first execution without a whole-kernel gate.

Root corrected the stale ending that said no I4-V coverage had been collected.
It now distinguishes independently accepted finite V1 checkpoints from remaining
V1/V2/V3 and broader campaign obligations, consistent with INV-03-01. This return
reviews the plan only: it adds no runtime result, implementation acceptance or
claim that V1/I4-V is complete.

## Intermediate I4-V return — concurrent stack prefixes, October 26, 2026 UTC

Root authored the ABI/runtime and runner, `/root/i4v_producer` authored host and
compiler bindings, and `/root/i4v_upstream` authored the independent stack oracle.
`/root/i4v_contract_review` reviewed these changes read-only and independently
rebuilt and ran the corrected primary's retained snapshot, with no overlapping
build. **ACCEPT this finite N=1/2/3/4 concurrent-stack checkpoint; V1 and I4-V
remain incomplete.**

Independent `i4v-stack-concurrent-independent-20261026-01` passes 158 jobs and
142 native cells: 44 load, 82 N=1 stack, eight alias compositions and eight
concurrent-stack compositions at low/high source RIP. The reviewer rechecked
every report, all 148 actual compilations, 160 cell-to-compile associations and
246 observed stack chunk transfers. All 4,717 checksums verify; all 156 retained
source inputs match both corrected primary02 and the reviewed workspace. The
reused FEX executable matches its pin, and all compiler containers were removed.
This is a fresh host/runtime rebuild and fresh compilation campaign, not another
FEX build. The rebuilt runtime ELF exactly matches primary02.

The new eight compositions cover 20 owners, 560 paired-alias acknowledgements,
180 maintenance heads and 28 ordinary generated programs. Each old program
commits its first push, faults on its second, retains its original grant, route
and code through the remaining instructions, and completes before replacement.
Later programs retain a continuously descending RSP across physical remaps;
RDX stays fixed, so the oracle independently models loads from the original
positions in the current physical page. Full native fault frames, five precise
prefixes, all 8,192 bytes of both physical pages, events, FIFO capacity and
eight-head fairness are checked. Actual same-page unaligned stores execute with
alignment checking disabled. Three stale chunks are rejected, a wrong-owner
reply is rejected, and all replacement chunks are validated before publication.
The runner binds all nine transferred packets to the resident observations.

Review fixed missing bank/reserved admission checks and reset canonical RIP at
each whole-program entry. The first complete development run preserves a legacy
alias timeout after 126 prior cells passed. Focused debug01 and emitted-code
inspection identified a native-stack overflow caused by inlining stack-prefix
temporaries into the common completion path. Separate non-inlined helpers and
borrowing the initial frame remove that growth. In the accepted replica, the
conservative emitted call-chain bound is 15,808 bytes on each 16,384-byte native
stack, leaving 576 bytes; the resident image occupies 63,804 of 65,536 reserved
bytes. Future code growth must recheck both bounds. Focused debug03 preserves an
oracle failure: the inactive alias alone was prewarmed, while its expectation
incorrectly required both aliases warm. The corrected oracle checks inactive RO
and active absent, with corruption controls. Temporary diagnostics and shortened
deadlines are absent from the accepted campaign.

The independent run lasted 75 seconds and retained about70MB. Observed peak
build/worker process-group RSS was below348MB/97MB; declared limits and two-job
builds remained in force. These are correctness runs, not performance evidence.
No substantive correctness or reproduction blocker remains in this finite scope.

This fixture retains shared alias permissions and bounded per-owner stack spans;
source mutation is a protected runtime write, not translated guest self-modifying
code. Concurrent terminal/cross-page faults, mixed-permission aliases, V2
paging-off execution, V3 mode transitions and the broader I4-V campaign remain
obligations. No full V1/I4-V acceptance follows.

## Intermediate I4-V return — concurrent address aliases, October 26, 2026 UTC

Root authored the ABI/runtime and runner, `/root/i4v_producer` authored host and
compiler bindings, and `/root/i4v_upstream` authored the independent alias checker.
`/root/i4v_contract_review` reviewed the changes read-only and independently ran
the corrected primary's retained source snapshot, with no overlapping build.
**ACCEPT the finite same-permission alias checkpoint at N=1/2/3/4; V1 and I4-V
remain incomplete.**

Independent `i4v-alias-independent-20261026-01` passes 149 jobs and 134 native
cells, including all prior load/stack cells and eight low/high-source alias
compositions. The reviewer rechecked every report, all 124 actual compilations,
136 cell-to-compile associations and 174 stack transfers. All 4,044 checksums
verify; all 134 retained source inputs match both the corrected primary snapshot
and reviewed workspace. The reused FEX executable matches its pin; this is a
fresh host/runtime rebuild and fresh compilation campaign, not another FEX build.
No I4-V compiler containers remain running.

Each owner prewarms its inactive alias with the old physical route. The campaign
checks 560 per-owner acknowledgements containing both exact replacement native
descriptors, 180 maintenance heads and 28 actual generated ordinary intervals.
Local shadow repair/TLBI precedes acknowledgements; generated accesses alternate
aliases. The unchanged I4 checker still validates retained routes/code, delayed
acknowledgements, source-stale replies, events, capacity and eight-head fairness
after the alias checker first validates every address it normalizes. Full native
roots and leaves remain unnormalized. All eight compositions reject an actual
replacement reply with the opposite guest RIP, preserving pending identity and
blank replacement code before publishing the valid reply.

Review found that the initial alias record at 0x2a000 overlapped maintenance
interval storage. `i4v-alias-dev-20261026-01` preserves the resulting first-alias
failure, status4/detail153, after 126 prior cells passed. The record moved to
0x2f000; compile-time assertions exclude the entire interval reserve, baseline
extent and code page. Corrected primary02 and the independent replica pass.
No substantive correctness or reproduction blocker remains in this scope.

The independent run retained about65MB and finished in63 seconds under its
20-minute bundle limit. Observed peak build/worker process-group RSS was below
386MB/96MB, respectively; declared per-job limits and two-job builds remained
in force. Root's subsequent serial `i4v-alias-regression-20261026-01` reports all
87 legacy jobs passed; the reviewer inspected that manifest, without claiming a
second independent legacy execution. These are correctness runs, not performance
measurements.

The aliases deliberately share all guest permissions and lower tables; reusing
one walk for both mappings is scoped to that fixture. Source mutation performs
a protected runtime write through the actual opposite alias, not a translated
guest self-modifying instruction. Mixed-permission aliases, concurrent stack
prefix/recovery paths, V2 paging-off execution, V3 transitions and the broader
I4-V campaign remain explicit obligations. No full V1/I4-V acceptance follows.

## Plan follow-up review — I4-V concurrent address and mode coverage

Root amended the implementation plan and INV-03-01 next action. Fresh independent
`/root/i4v_plan_followup_review` reviewed them read-only against the workflow,
Track03 scope, ABI paging/control interface and INV-03-04 mode contract.
**ACCEPT the plan; no substantive blockers.**

**Evidence validity:** the plan requires actual generated execution, separate
fetch/data/stack checks, independent expectations, precise effects/faults and
N=1 then N=2/3/4 schedules. It distinguishes local architectural transitions
from shared-source invalidation and preserves hidden-PAE lifetime requirements.
No runtime success follows from this review.

**Decision relevance:** explicit concurrent stack-prefix recovery, aliases with
different permissions, mixed paged/paging-off CPUs and return to a previously
used context close acceptance ambiguities. Stale replies/entries, delayed
acknowledgements, retained lifetimes and pending events remain required. Bounded
slices and independent I5 preparation preserve build-first execution.

Root applied the reviewer's two nonblocking wording clarifications: illegal
transitions have a rejection boundary, and paging-off excludes implicit walk/A/D
effects without excluding deliberate data writes to table backing. Documentation
whitespace checks pass. Runtime implementation and native campaigns were outside
this plan review; existing finite results retain their original scope.

## Intermediate I4-V return — live stack prefixes, October 26, 2026 UTC

Root authored the ABI/runtime and runner, `/root/i4v_producer` authored the host
and worker, and `/root/i4v_upstream` authored independent expectations/checks.
`/root/i4v_contract_review` reviewed these changes read-only and then performed
a fresh, serial retained host/runtime rebuild and execution.
**ACCEPT this finite N=1 stack checkpoint; V1 and I4-V remain incomplete.**

Independent `i4v-stack-independent-20261026-01` passes 139 jobs and 126 native
cells: the prior 44 load cells plus 34 stack scenario/address combinations and
48 corrupted stack-reply deliveries. The reviewer independently rechecked every
report, all 100 actual compilations, 112 cell-to-compile associations and 174
observed stack chunk transfers. All 3,559 checksums and 91 retained source inputs
match; the producer binary matches its existing pin. No compiler containers remain
running. This is a fresh host/runtime build and fresh compilation campaign using
the retained FEX executable, not another FEX rebuild.

The unchanged generated push/store/load instructions execute in five slots with
resident returns only between guest instructions. Due-access guards preserve
earlier effects. Real preindexed-store fault frames retain the old guest RSP while
containing the generated immediate in scratch x20. Both complete physical data
pages, canonical state, native prefixes, A/D bytes and shadow permissions match
independent expectations. Tests distinguish supervisor CR0.WP override from CPL3
read-only denial, exercise source NX/SMEP and parent permissions, and preserve
the committed prefix across later #SS, #GP and #PF. In particular, late #SS retains
one push and RSP=HIGH_BASE; late #GP retains three pushes. Both preserve CR2.

One actual resident compile request produces three identity-bound chunks. Runtime
publication waits for all chunks, the complete emitted body and source recheck.
All 48 per-word corruption controls reject before publication, including failures
after one or two valid chunks. No substantive correctness or reproduction blocker
remains for this checkpoint; review required no additional execution-code fix.

Instruction-boundary returns are an explicit fixture mechanism, not evidence for
unsegmented-block execution or performance. Live generations remain immutable.
N=2/3/4 aliases/remap/source/retained-event/fairness composition, broader address
and access boundaries, V2 paging-off execution, V3 mode transitions and the wider
I4-V validation campaign remain required. No full V1/I4-V or emulator correctness
claim follows from these results.

## Intermediate I4-V return — first live address slice, October 26, 2026 UTC

Root authored the ABI/runtime and retained runner; `/root/i4v_producer` authored
the bounded FEX adapter and host worker, and `/root/i4v_upstream` independently
authored the semantic expectations and checker. `/root/i4v_contract_review`
reviewed these changes without authoring execution code, then performed a fresh
retained host/runtime rebuild and execution.
**ACCEPT this finite N=1 load checkpoint; V1 and I4-V remain incomplete.**

Independent `i4v-live-independent-20261026-01` passes 55 jobs and 44 native cells:
14 eager, 14 live-mode and 16 deliberately corrupted reply deliveries.
The reviewer rechecked every report, all 26 actual compilations, 38 cell-to-compile
associations, all 1,350 retained checksums and 84 exact source inputs. The pinned
producer binary and adapter-input hashes match. This replica rebuilds host/runtime
and performs fresh FEX compilations with the retained producer; it does not
independently rebuild FEX. No I4-V compiler containers remain running.

The tested load executes with actual low/high guest RIP and upper-address data,
using a distinct TTBR1 root and owned guest-physical backing. Checks cover a real
high native fault/install/retry, low alias access, source NX/SMEP faults, data
nonpresence/user denial, and private-gap #GP before native entry. Full seeded
guest/native state, table A effects, fault packets and unchanged memory are
checked. The live path starts with blank native code, binds the resident request
to actual source/RIP compilation and publishes only after an exact reply. All
16 word-corruption controls reach resident rejection without publication or
guest execution. Context/source generations are immutable in this checkpoint;
these controls do not establish concurrent stale-work exclusion.

Review corrected logical guest-pointer arithmetic and unchecked resolver failure
in the producer adaptation, missing tracking of required compiler fixture logs,
and required explicit live-mode association at the runner boundary. No substantive
correctness or reproduction blocker remains for this checkpoint. Primary eager
evidence was also independently rechecked: 14 cells, 24 jobs and 763 checksums.

Resource qualification: the independent and legacy-regression builds briefly
overlapped around 02:37:20–21 UTC. Each build used two-job configuration and its
declared supervision, but an aggregate two-job ceiling is not established for
that interval. Serialize later build jobs when applying that aggregate bound.
These runs make no performance claim.

Remaining V1 work includes actual stack operations, writes/CR0.WP, boundary and
additional permission cases, and N=2/3/4 remap/source/retained-event/fairness
composition. V2 paging-off execution, V3 mode transitions and the broader ordering,
coherence, recovery and progress campaign remain required. This checkpoint does
not establish full kernel execution, general guest programs or whole-emulator
correctness.

## Plan review — I4-V address and mode coverage, October 25, 2026

Root authored the implementation-plan and current-next-action changes. Independent
`/root/i4v_modes_plan_review` reviewed the exact two-file diff against the ABI
paging interface, INV-03-04's mode/permission contract and the workflow.
**ACCEPT; no substantive blockers.**

**Evidence validity:** V1/V2/V3 separate guest privilege, address range and paging
mode; guest paging-off execution retains native mapping and physical ownership
checks. The plan requires legal control sequences, precise failed-transition
state and hidden-PAE lifetime checks. Local architectural changes are distinguished
from shared source/backing invalidation. Actual generated execution and independent
expectations are required; host-only models cannot establish the promised paths.

**Decision relevance:** the three early slices cover the owner's kernel-address,
paging-off and mode-transition requirements, including N=2/3/4 retained-access,
stale-code/reply, acknowledgement, pending-event and fairness tests. Partial-result
rules keep unfinished slices visible. Missing implementation grows with its tests;
full boot remains I5 and independent I5 preparation can proceed. The update does
not reopen finite I3/I4 acceptance or establish new runtime correctness.

Documentation checks: `git diff --check` and new relative-link targets pass.
No runtime code changed, no upstream tests were imported and no runtime tests were
executed for this plan update. Execution vectors and per-slice resource bounds
remain work to declare when starting I4-V, as the plan requires.


## Current return — I4 concurrent composition, October 26, 2026 UTC

Root authored runtime/host integration. Independent `/root/i4_contract_review`
reviewed coordination and wrote the FIFO/state/effect oracles; separate
`/root/i4_events_review` checked pending-event semantics and its retained native
results. Fresh `/root/i4_final_review` reviewed the complete implementation and
oracles without authoring execution changes, then performed independent rebuilds.
**ACCEPT finite I4 completion; no substantive blockers remain.**

**Evidence validity:** full retained `i4-independent-20261026-01` passes87 jobs,
including all I1–I3 regressions,21 Rust tests,113 I4 corruption controls and every
I4 profile at N=1/2/3/4. Reviewer and root verified2119 checksums and67 exact source
inputs. Reviewer independently rechecked40 producer-backed cells and67 compiler
associations, including36 I4 associations. Pressure completes9/18/27/36 actual
PTE mutations and2/3/4/5 generated ordinary intervals with FIFO identities,
all-participant acknowledgements, dirty retry during closure and final reference
release. An entry-only fresh rebuild passes34 jobs and766 checksums without FEX
inputs. Both runs leave no compiler containers.

Review corrected incomplete snapshot dependencies and added an isolated retained
import test. Initial pressure run01's four-CPU100-second deadline failure remains
retained. Ordered ticket handoff fixes overtaking by hot polling owners; a
contended-grant test checks the order. Primary run02 and the independent replica
then pass pressure cells in about one second with unchanged deadlines. Those
elapsed times are diagnostics, not a qualified performance claim. The final
emitted stack audit bounds the largest inspected chain to15040/16384 bytes after
moving baseline temporaries out of the live dispatch chain.

**Decision relevance:** evidence satisfies the I4 row and configured-N composition
requirements in the implementation plan, as mapped in INV-03-01's completion
audit. No whole-ABI, whole-emulator or generator-selection conclusion follows.
Broad memory-ordering conformance, arbitrary programs, native IRQ/forced-exit
architectural delivery, sleeping waits, full APIC/IDT, boot and larger-N scaling
remain explicit later work. I4-V remains a separate bounded deep validation pass.
The event fixture changes its controls explicitly; it does not claim x86 shadow
expiry. Queue saturation plus duplicate rejection does not cover a separately
reachable FULL branch. Only result/setup documentation changed after replication.


## Intermediate I4 return — October 26, 2026 UTC

Root authored per-vCPU host/runtime integration and coordinator/source paths.
Independent `/root/i4_contract_review` reviewed contracts/code and authored
`tools/check_i4.py`, keeping semantic expectations separate from runtime logic.
**ACCEPT the finite entry/fault/remap/source development scope; I4 incomplete.**
Review fixed failed-owner startup handling, omitted object admission guards,
an initial shared-code publication/read race, and a negative reuse assertion that
did not originally exercise the real overwrite guard. Per-owner setup precedes
native entry; owner IDs remain alive through cancellation. External process
supervision is the final bound for panic-abort or failed hypervisor cancellation.

The reviewer independently rechecked `i4-compose-dev-20261026-01`: N=1/2/3/4
requested profiles, semantic/fault/coherence/lifetime checks,12 actual compiler
invocations with complete request/input/native-code/reply associations, all1174
checksums,54 passed jobs and matching reviewed runtime/host/ABI sources. No
remaining containers were recorded. The independent checker has65 corruption
controls. This is evidence inspection, not an independent rebuild.

Root's subsequent `i4-compose-regression-20261026-01` passes all79 jobs, including
the full I1–I3 matrix and current I4 profiles. No source/publication behavior is
inferred from a host-only model: these paths execute in the resident runtime with
real FEX code and actual native faults. Pending eligible events, sustained queue
fairness/capacity, remaining combined schedules and final independent review and
reproduction remain required before accepting full I4. I4-V stays separate.

## Plan review — post-I4 integrated validation, October 25, 2026

Independent `/root/post_i4_plan_review` reviewed root's I4-V plan addition and
current-summary/workflow links. **ACCEPT; no substantive blockers.** The plan
keeps continuous I4 testing and independent I5 progress, bounds the integrated
pass, separates semantic and implementation checks, and covers memory ordering,
translation coherence, recovery/lifetime and progress. Upstream reuse retains
pins, attribution, expectations and harness limits; FEX/QEMU execution alone is
not an authoritative oracle. Documentation checks pass. No tests were imported
or executed for this update, and no new architectural correctness is established.

## Current return — I3 recovery and paging, October 25, 2026

Root authored the Rust host/ABI/runtime, assembly gates and integration. Separate
`/root/i1_fex_inputs` reproduced actual FEX allocation output and adapted its
exporter/worker; `/root/i3_paging_oracle` authored independent expected-state,
walker/table, native instruction, helper and partial-effect checks. Independent
`/root/i1_code_review` inspected consequential source/contract changes, emitted
code, bounds and retained results without authoring runtime changes.

**Verdict: ACCEPT bounded I3 implementation increment; no substantive blocker.**
Review corrected missing CR4.OSFXSR for admitted SSE, a proposed return trampoline
that destroyed x30's RDI root, in-range but incorrect map acceptance, and container
cleanup that initially covered only I2 labels. Full ERET entry preserves x16/R12;
direct branch return preserves x30/RDI; SP_EL1 capture preserves live producer
spills. Both code and allocation-map admission artifacts match machine-produced
fresh exports. BL/x30-only-link draft rules were updated to the actual profile.
These are implementation-driven ABI changes, not a final generator selection.

**Independent reproduction:** `i3-independent-20261025-01`,23:34:47–23:35:05UTC,
under the durable raw root. All56 jobs passed, including15 Rust tests, independent
oracle controls,14 I1 native cells plus N2 rejection, six I2 scenarios,12 paging
scenarios and10 pressure/FP/helper/REP cases. Reviewer verified all1312 checksums
and56 snapshot files against the workspace,31 fresh compiler invocations, exact
runtime-request/input/body/map/reply association, and admission artifacts against
actual allocated maps. Both live Docker label queries were empty. Root separately
checked the final manifest, all1312 checksums and executable-source identity.
After reproduction, only the captured producer README changed, to describe the
already-tested direct-branch return and admission-artifact provenance; the other
55 captured inputs remain byte-identical. Project result/spec documents were
updated after the verdict. These editorial updates do not change execution.

**Evidence validity:** these independently reproduced exploratory development
results establish the declared one-vCPU finite behaviors. Complete guest/native
state and earlier effects are checked, including direct/inverted carry, all35
allocated roots, initialized spills, overwritten RAX, unchanged banks, helper
output staging, FP sticky status and counted REP progress. The partial fixture
faults after its second device read and resumes only the pending STRB; both
fault-time progress and final counts/bytes are independently constrained.
Final emitted stack maxima were12,496 bytes on SP_EL0 and9,920 on SP_EL1, within
16KiB each. Maximum observed supervised group RSS was312.8MB; retained replica
size35.6MB. These diagnostics do not qualify a performance comparison.

**Decision relevance:** I3 is complete as a bounded implementation increment and
I4 can develop concurrent composition on the same runtime. The pressure strategy
is deliberately conservative; maps are automatic but only exact finite profiles
are admitted. REP is a resident fixture, not FEX-generated REP. No general paging
mutation, code/route change during a retained operation, full FP/x87, IDT delivery,
SMP, boot or full ABI/architecture conformance follows. Native macOS FEX integration
and performance selection remain open. Review/reproduction stayed inside I3's
allowance; historical failures remain retained and unpromoted.


## Current return — I2 live producer, October 25, 2026

Root authored the versioned request lifecycle, host worker binding, resident
service/publication path, producer worker and retained runner integration.
Separate `/root/i1_fex_inputs` reproduced fresh XOR/OR compiler inputs and authored
the semantic and lifecycle/continuity oracles. Independent `/root/i1_code_review`
reviewed source, SDK/assembly contracts, emitted stack use, oracle coverage and
producing artifacts, without authoring implementation changes.

**Verdict: ACCEPT finite I2 development; no unresolved substantive blocker.**
The reviewer found one host-boundary gap: a reply was checked against current
resident state but not the exact original request issued to its worker. Root
added immutable per-worker request binding and regressions, including an old
worker attempting to impersonate a newer context/request. Reviewer rechecked
and accepted the fix before the final replica. Actual guest-source snapshot reads
and runtime-request/producer-input/output associations were also checked.

**Independent replica:** `i2-independent-20261025-01`, 22:44:02–22:44:10UTC,
under the declared external wall/RSS and container limits. All 31 jobs pass:
nine Rust tests, worker/producer tests, independent oracle controls, builds and
signing, 14 I1 native cells plus unsupported-N rejection, and six I2 scenarios.
The reviewer verified 11 fresh FEX invocations in distinct clean containers,
both XOR/OR native variants, ten I2 executions, all 749 retained checksums and
25 frozen source files matching the workspace. It rechecked all result oracles,
request/input/object/reply bindings and the emitted 10,464-byte maximum reviewed
return/capture path within the 16KiB stack. No replica compiler container remains.
Root independently verified the final manifest, source equality and checksums.
The primary development run remains separately retained as
`i2-development-20261025-01`; producer input replication is
`i2-producer-probe-20261025-01`. All are under the durable sibling raw root.

**Evidence validity:** independently reproduced exploratory one-vCPU results
support I2's declared development behavior. Cancellation and changed source/
context cannot let a late reply consume an occupied replacement slot; the fresh
reply succeeds afterward. Cache reuse preserves current captured guest state and
performs no new compile. Invalid host-boundary and resident-transfer controls
remain distinct. Actual generated computation is preserved inside the admitted
44-byte leaf; producer prefixes/dispatcher suffix and their relocations are excluded.

**Decision relevance:** I2 is complete and I3 can add recovery/paging to this same
runtime. The selected worker runs on ARM64 Linux outside the captive VM; it does
not establish native-macOS compilation latency. Source/context controls and explicit
fixture redispatch are not general x86 control-flow, self-modification or paging.
General FEX coverage, automatic recovery maps, helper transitions, concurrency,
boot and qualified performance remain unestablished. The full ABI milestone and
Track03 remain open. Review and reproduction stayed within the I2 allowance.


## Current return — I1 first execution, October 25, 2026

Root authored the Rust host/shared ABI/resident runtime, assembly gates and
retained runner. Independent `/root/i1_fex_inputs` reproduced the pinned FEX
fragment and authored the semantic oracle from the x86 operation and producer
bindings. A third context, `/root/i1_code_review`, reviewed the implementation,
oracle, emitted gateways, SDK FFI widths, mapping permissions and run outputs.
It did not author source changes.

**Verdict: ACCEPT for finite I1 development; no unresolved substantive blocker.**
Two findings were corrected and rechecked: expected entry state must come from
the preserved host seed rather than VM-writable memory, and builds must pin the
compiler/control inherited overrides rather than merely report a pinned version.
The final runner also isolates Cargo state/output and verifies snapshot driver/
helper identity. Reviewer confirmed capture precedes Rust and emitted Rust stack
frames fit the declared stacks. Editorial formatting was handled directly.

**Independent rebuild:** `/root/i1_code_review` executed
`i1-independent-20261025-01` under the declared external wall/RSS supervision,
22:22:46–22:22:49UTC, from a fresh durable source snapshot. All23 jobs passed:
four Rust tests, four worker tests, oracle controls, build/sign/disassembly,
14 native cells and unsupported-N rejection. The reviewer verified all446
retained checksums, equality of frozen sources with the workspace and exact
correspondence of the44-byte FEX body to producer `outputs/002.native[12:56]`.
It independently rechecked all14 result JSONs. Root rechecked the final manifest,
source equality and all checksums. Producing files are under
`/Users/liaozping/orca/natemu-raw/i1-independent-20261025-01`; earlier developmental
run `i1-development-20261025-01` is retained separately. Frozen Track01 files were
reused unchanged, not edited.

**Evidence validity:** the finite one-vCPU development results are reproducible
on M4/macOS26.6.2, SDK26.4, Rust1.97.0. They remain exploratory development results;
this review does not promote them to general architecture or performance evidence.
The contained fault is a UDF control, not a generated-load recovery test. Preserved
bank bytes are opaque sentinels, not a complete x86 state implementation.
**Decision relevance:** I1 is complete and I2 can connect the actual producer
loop to this working path. No generator winner, general recovery, paging, helper
restoration, concurrent execution, boot or speed claim follows. The broader ABI
milestone and Track03 remain open. Work and replication stayed within the I1 bound.


## Current return — implementation plan and setup, October 25, 2026

The owner requested project setup changes reflecting the subsequent Rust,
host-side FEX/live-producer, early performance and N-vCPU decisions, followed by
reviews without editorial blockers. Root authored the documentation; separate
`/root/setup_check_author` authored the read-only diagnostic and regression tests.
The implementation order/setup now lives in `docs/implementation.md`; the current
investigation inventory has moved ahead of historical campaigns. Historical
observations and the frozen Track01 sources remain unchanged.

- **Technical reviewer `/root/plan_technical_review`: ACCEPT, no substantive
  blocker for I1.** Inspected AGENTS, workflow, implementation plan, current
  investigation/inventory, Tracks01/03/05, full ABI draft, setup code/tests and
  FEX fixture/exporter documentation. The Rust capture/FFI/shared-memory contract,
  actual host producer loop, allocated-site recovery, N-dependent capacity and
  early adverse schedules are sufficient requirements for incremental work.
  Diagnostic versus qualified timing and Linux-worker versus native-macOS costs
  remain distinguished. No history, edits or runtime experiments were performed.
- **Setup reviewer `/root/setup_workflow_review`: ACCEPT, no substantive
  blocker.** Independently inspected the entry points, current summary/inventory,
  Tracks01/03/04/05, ABI opening/sections7–9 and diagnostic source/tests. Ran all
  four regression tests successfully and the actual diagnostic, which returned1
  for missing bare-metal Rust target and unset raw root. Checked bounded queries,
  disabled implicit Rust installation, symlink-resolved output containment and
  explicit unverified capabilities. A fresh executor can start I1 without
  reconstructing historical campaigns or a whole-research gate. No edits made.

**Evidence validity:** accepted for plan/setup adequacy only. Neither review
establishes actual Rust/EL1t capture, general recovery-exporter fit, native-macOS
FEX integration, N-vCPU runtime correctness, Linux boot or performance. The local
diagnostic checks known prerequisites and its tests check diagnostic behavior.
**Decision relevance:** proceed with I1 and the early I2 producer loop; retain
scoped correctness tests and independent claim acceptance while allowing ordinary
development. Concrete failures block affected paths only. No substantive returns
or editorial blockers remain; no prior experimental allowance is activated.

**Validation:** root also ran the four tests, the diagnostic and whitespace checks.
The diagnostic leaves toolchain installation, environment configuration, builds,
VMs and containers untouched. Runtime compilation/admission belongs to I1.

## Current return — adequacy and build-first iteration, October 25, 2026

The owner requested a fresh adequacy assessment, build-first development and an
assessment of the provisional ABI under FEX-first incremental integration.
Root authored the current workflow/investigation revisions; two separate fresh
contexts supplied read-only assessments, without history searches or experiments.

- `/root/fresh_adequacy` inspected the workflow, Track03/Track01 charters,
  current twelve-question inventory, supporting investigation summaries, ABI
  fragment method and checkpoint limitations. Its conclusion is that sufficient
  context exists to start incremental construction; no identified research
  unknown blocks the first executable slice. It distinguishes selected contracts
  awaiting implementation, missing inputs, integration uncertainties and owner
  product choices. Complete guest correctness, FEX fit and economics remain open.
  Its targeted recheck accepted the revised adequacy summary and workflow for
  documentation adequacy and decision relevance, with no substantive correction.
  The optional wording clarification to say "Before each increment" in the
  charter was incorporated.
- `/root/abi_iteration_review` independently inspected the ABI, current direction,
  charter interactions and fragment/checkpoint bindings. It found a coherent
  provisional seam, with actual recovery/exporter fit and combined runtime
  behavior still unvalidated. It recommended early real FEX execution, fault/helper/
  paging checks, then two-vCPU adverse interactions and automatic recovery-map
  tests. It performed a targeted recheck of the revised workflow, current
  investigation/progression, charter and ABI iteration note: **accept for this
  scoped workflow/design update, with no substantive correction required**.

**Evidence validity:** existing scoped findings support starting implementation;
this review adds no runtime, general-exporter, full-fit or speed observation.
Manual fixture maps do not establish automatic recovery metadata. The ABI's
state/effect/isolation/lifetime requirements and full T1–T12 coverage target remain.
**Decision relevance:** proceed in executable increments with FEX first, allowing
the interface to evolve. Whole-track completion, a universal ABI and final
generator selection are not prerequisites to ordinary development. Fresh reviewed
evidence is still required for architectural acceptance; partial coverage stays
partial. No historical runtime budget is activated by this review.

Earlier returns below retain their original scope and chronology.

- **Reviewer and independent context:** `/root/machine`; independent of author
  `/root`. Reviewer authored INV-03-03 and therefore does not accept its findings
  or the combined machine proposal in this review. No changes to INV-03-01 made.
- **Material inspected:** INV-03-01 in full; current workflow and Track 03
  charter; INV-01-31 metadata claims 05–11 and scoped conclusions; INV-01-33
  publication/staging claims 01–10 and scoped conclusion; directly retrieved
  upstream QEMU translator-internals page; current scope and backlog references.
- **Date and verdict:** September 19, 2026 — **accept the scoped source/inference
  findings CLM-03-01-01, 02 and 03 only. Architecture acceptance and Track 03
  completion are not established.** No substantive blocker remains against those
  three narrowly stated findings. No experiment or replication was performed.

## Claim assessment

1. **CLM-03-01-01:** accepted as an inference about applicability. INV-01-31
   requires recoverable flags/register/control state, per-substep commit and
   exception selection; INV-01-33 expressly leaves shared staging and general
   reclamation unestablished. Finite two/four-vCPU M4 publication is not promoted
   to a general translator or new host/guest queue contract. Read the referenced
   ranges as material containing findings, not acceptance of each numbered
   historical hypothesis: INV-01-33-06 remains explicitly unestablished.
2. **CLM-03-01-02:** accepted. [QEMU translator internals](https://www.qemu.org/docs/master/devel/tcg.html),
   reporting version 11.1.50 when independently retrieved September 19,
   supports state-dependent TB reuse, mandatory interrupt reconsideration on
   relevant state changes and per-instruction deferred-state recovery. Those
   are upstream mechanisms; root correctly does not claim their relocated
   implementation already works. Moving documentation requires rechecking
   material semantic changes when selecting a pinned generator release.
3. **CLM-03-01-03:** accepted as a necessary address-domain distinction, not a
   complete relocation design. A host pointer's numeric value does not imply a
   valid VM helper/code/state reference. QEMU's documented tagged TB pointers
   and PC-based exception lookup give concrete examples requiring audit. Later
   design must also distinguish x86 linear from physical addresses and native
   virtual from IPA addresses; the present three-domain argument does not claim
   these subdomains are interchangeable.

## Architecture gaps and smallest next remedies

These are blockers to a future architecture acceptance, **not reasons to reject
accurately bounded source findings**. The record already acknowledges them;
this review makes the closure evidence concrete.

| Gap / consequence | Evidence and smallest remedy | Closure condition |
| --- | --- | --- |
| No chosen protected execution/address layout: generated runtime state can remain accessible to guest-derived operands | Root's EL0/EL1 alternatives each leave access generation and private-state placement unspecified. Choose one translator access ABI and draw its concrete mapping/privilege layout; enumerate ordinary, helper, stack, literal and indirect-branch accesses. | Independently checked argument shows guest-controlled addresses/control targets cannot select private state/code, including noncanonical and wrapping ranges, without changing x86 fault semantics. Architectural tests target any remaining platform-specific uncertainty. |
| Recovery imported by analogy can miss generated instruction substeps | The accepted Q31 fixture has handwritten blocks; QEMU documentation describes its own exception machinery, not relocated execution. Select a pinned generator and audit one ordinary load, one helper fault, one interruptible operation and deferred flags/FP state across the proposed ABI. | Every live state component and irreversible effect has a recovery/commit owner; metadata generation and helper return domains are demonstrably compatible with that owner. |
| Service progress while a vCPU waits is unspecified | Root names the circular-wait counterexample, but identifiers and lifetime checking do not establish progress. Specify who services each notification while a CPU is running, halted, in shim, exited or waiting for translation. | No protocol waits for an action that can only run after the waiter resumes; independently reviewed lock/wait graph and event recheck sequence cover the enumerated states. |
| Product compatibility is not concrete enough to select a CPU/machine profile | Missing historical scope files cannot establish which guest/workloads a candidate preserves. Record the first target stock images and workloads, including what unmodified means for initrd/device discovery; inspect required configuration/features. | Candidate CPUID, modes, firmware and devices cover that matrix; exclusions are explicit scope decisions rather than silently inferred from fixture coverage. |
| All-writer coherence is an actual protocol dependency | Host aliases/kernel I/O bypass CPU write traps; source generations alone can accept torn or stale work if captured incoherently. Resolve INV-03-04 with the machine author's proposed device-commit path. | CPU, helper and backend writes share a specified linearization/coherence authority with source capture, remapping, fault completion and retirement. |

Material alternatives and costs in INV-03-02/03/04 still need integration before
selection. This reviewer has not independently accepted its own INV-03-03 device,
time or containment proposal. A different context must review those claims and
any final combination that relies on them.

## Scope, budget and methodology assessment

The initial audit budget is bounded and its stop condition produces decision
questions; it is not an experimental authorization or artificial completion gate.
The user authorized completing Track 03, so successive bounded source work and
necessary reviewed architecture experiments remain within the research task.
No return to the user is needed merely because the initial integration timebox
ends. A material new resource budget or compatibility tradeoff needs its own
concrete handling under the workflow; the current document makes neither.

No evidence yet shows cross-generation applicability, host-core placement,
end-to-end Linux behavior or speedup. Their absence does not undermine the three
accepted source/inference claims. It does prevent using this review as evidence
for a working or independently accepted architecture.

## Editorial edits and limits

No direct edits. No git history or archived Track 01 material inspected. No
experimental artifacts were rerun: this review relies only on current accepted
investigations for inherited applicability and directly retrieved upstream
source contracts for the new source claim.

## Returns

No return needed for CLM-03-01-01–03. Re-review substantive changes to their
assumptions or any proposed architecture acceptance in this same thread.

### Return 1 — exact kernel configuration supplement

September 19, reviewer `/root/machine`: **accept CLM-03-01-04 and its bounded
transport-prerequisite inference.** Independently hashed both complete `.deb`
files under `/private/tmp/natemu-track03-stock-audit`: Ubuntu
`15bacf92d51a0ea971d9c1369c6385e8867cd4973eede8f8931fbaf99f3dd173`, Debian
`0ab746a3d854ad0e666923147842d1e629cdb3e29a97b76ae25b4911285174e4`.
Both match the official package identities and accepted hashes in INV-01-34
claim 08. Independently extracted each archive's unique `.config` with `ar`,
decompression and Python tar reading, without installing/executing package
code; compared it byte-for-byte with root's reading copy and inspected every
symbol in the new table. All table values match.

The working Ubuntu kernel has built-in MMIO/block/network support; Debian
requires modules for those drivers and lacks command-line MMIO enumeration.
Root correctly distinguishes that route from ACPI enumeration and does not
infer actual initrd contents, root mount, required exposure of every configured
device, or compatibility of every distribution release. No substantive return.
This supplements accepted source findings; architecture completion remains open.

### Return 2 — guarded EL1 execution candidate

September 19, reviewer `/root/machine`: **accept the conditional address-partition
argument as an explicitly limited inference; do not accept implemented isolation
or the guarded-EL1 architecture.** Read the entire new candidate and the associated
memory supplement, independently consulting Arm Memory Management v1.0 §§5/7.
The proposed lower/upper 48-bit native regions can distinguish the x86 48-bit
canonical halves from the stated private interval. The argument properly depends
on disabled top-byte ignoring and full guest-derived operand validation, not on
x86 noncanonicality itself causing an Arm fault.

The candidate explicitly separates guest physical addresses from native IPAs:
a guest PTE cannot select shim backing solely by naming its numeric IPA.
Only runtime-owned native roots reach private allocations. Guest CPL and CR0.WP
must determine shadow policy independently of EL1 authority; INV-03-04 now names
those in context identity and excludes stale direct links after mode changes.
PG-off protected32 routes through guest physical resolution rather than raw IPA
access. Those are necessary coherent boundaries, not evidence that every
emitted access already respects them.

Remaining acceptance premises are substantive and already retained: complete
emitter/helper access classification, instruction-specific span/fault precedence,
correct shadow switching, immutable helper/control targets, and private object
bounds. Trusted-runtime field offsets require checked object extents, not merely
membership of the whole private interval; guest-controlled lengths and indexes
cannot become unchecked runtime offsets. Root's validated-fixed-offset/checked-
allocation-handle rule covers that at the proposed interface level. The next
selected-generator audit must demonstrate that callers actually obey it.

W^X requires removing all writable aliases and completing native maintenance
before entry and before reuse. Its inclusion is a new obligation; the candidate
correctly excludes the old RWX fixture as proof. Native branch reach/veneers may
be construction choices only when relocations and targets preserve this partition.
No additional source defect found. Hardware configuration and generated-code
premises remain open; this review accepts no arbitrary-native-code confinement
inside an EL1 VM and no host worker containment claim.

### Return 3 — native W^X scope correction

September 19, reviewer `/root/machine`: read the revised guarded-EL1 allocation
paragraph. **Accept the scope correction:** native W^X is optional hardening for
this trusted-EL1-runtime candidate. The charter requires guest/private-state
exclusion and containment, not a specific native page-permission mechanism. An
EL1 runtime controls its tables; W^X would not independently contain a compromised
runtime. Keeping it mandatory would add an unsupported architecture gate.

The revised baseline retains the necessary conditions: guest-derived operands and
control flow cannot select private mappings; guest RAM is native non-executable;
admitted code bytes are immutable by protocol; only trusted publication/retirement
may write runtime-only code allocations; publication and all continuation lifetimes
remain mandatory. RWX does not prove those conditions, and root explicitly retains
the unproved emitter/helper guards and separates the narrower Track 01 result.
My Return 2 statements about W^X maintenance apply only if that optional mechanism
is selected. This correction accepts neither actual guard completeness nor an
architecture/containment result. No author-file edits.

### Return 4 — CPU model/accelerator and clock source supplement

September 19, **separate reviewer `/root/translator`**, independent of CPU-profile
supplement author `/root`. Previous returns remain owned by `/root/machine`.
I authored INV-03-02, not this CPU-profile supplement, and did not edit the
architecture record. **Accept CLM-03-01-05's scoped source facts and the explicit
conditional priority for a Haswell-v2-derived audit; no realized CPU profile,
guest compatibility or architecture acceptance.**

Read the whole updated architecture record and earlier review returns, then
inspected the pinned `target/i386/cpu.c` at revision
`c1c18d1e640b64292859ce9f30f3c344edfb0294`, independently retrieving it from
upstream and verifying it matches root's local reading copy. Relevant paths:
`builtin_x86_defs` Haswell/version table, TCG feature masks,
`x86_cpu_filter_features`, model-loading/expansion/filtering contract,
`cpu_x86_cpuid` and CPUID level adjustment. Web cached retrieval failed; the
read-only direct upstream fetch succeeded. No QEMU CPU object was created or
realized and no guest code ran.

The base definition has Intel family 6/model 60 and basic level 0xD; its version 2
alias disables HLE/RTM and changes stepping to 1. The requested AVX2/FMA/F16C and
XSAVE/XSAVEOPT feature families are present. PCID and TSC-deadline bits in the
model request are absent from the system-TCG support mask; user-only builds
explicitly add them as kernel-only features, so that build distinction is
load-bearing. INVPCID is similarly excluded from the system-TCG leaf 7 mask.
`x86_cpu_filter_features` checks requested versus accelerator-supported words,
and the source explicitly says `query-cpu-model-expansion` does not do that
filtering. Thus the source supports the distinction between a named model,
an expanded request and a realized profile.

I independently checked the dynamic OSXSAVE condition (XSAVE feature plus
CR4.OSXSAVE), leaf 0xD component/size derivation and topology inputs. A final
feature manifest must name machine/topology/version and filtering conditions;
it cannot be just the Haswell-v2 name or a pasted expansion response. Level
adjustment can also depend on topology and enabled features, so acceptance of
the base definition’s 0xD value is not a claim about every realized configuration.
The inspected CPUID implementation has no 0x15 frequency case. A query above the
reported limit falls back to that limit's leaf rather than supplying a frequency
contract. The proposed clock model must therefore audit a platform calibration
route or deliberately add/review a new frequency leaf. This agrees with the
author's stop condition and does not independently establish which timer to use.

Keeping AVX2 as the current candidate preserves the full vector/state/atomicity
questions instead of eliminating them with a scalar fixture profile. The record
explicitly retains x87, mode transitions, feature/control-state dependencies,
MSRs, PAT/MTRR and enabled permission/debug behavior as audit work; upstream
TCG's support mask is not proof of the modified VM runtime. Four-level/no-PCID/
no-1GiB-page proposals and workload classes are not declared a finalized
compatibility boundary. There is no unsupported-profile acceptance to retract.

No substantive return is required for claim 05. Final acceptance remains blocked
on an enumerated realized feature/behavior contract, selected guest/workload
requirements, all enabled helper/recovery/memory paths, clock discovery and
correct unsupported instruction/MSR responses. This review neither audits all
those paths nor waives the separately recorded inherited FP discrepancy. No
editorial edits or runtime evidence.

### Return 6 — reference-clock source alternative

September 19, machine reviewer: independently read the relevant upstream v6.8
and v6.12 `arch/x86/kernel/tsc.c` paths, v6.12 `tsc_msr.c` model table and
`drivers/clocksource/acpi_pm.c`, using root's versioned reading copies under
`/private/tmp/natemu-track03-stock-audit`. No runtime experiment.

**Verdict: accept claim 06 and its conditional alternative as scoped source
findings.** Both TSC implementations reject native CPUID calibration below leaf
0x15; no-PIC quick calibration returns zero and the full PIT routine waits using
five early `udelay` calls then reports `ULONG_MAX`. The enclosing routine still
samples HPET/PM before and after that interval, rejects unchanged or disturbed
samples, and can return a valid reference result despite failed PIT calibration.
The v6.12 MSR table supplies Atom models, not Haswell; the PM-clock PIT rate check
is compiled out for CONFIG_X86_64. Therefore an absent frequency leaf alone does
not prove PIT is required.

This is not a complete no-PIC machine argument. Firmware must select that branch,
early delay must make sufficient progress, and the selected reference must be
discovered and implement the required rate/read behavior. LAPIC calibration,
interrupts, wallclock and exact vendor initialization remain separate. The author
keeps all of these premises explicit. No substantive return, full timer selection,
stock-boot finding or architecture acceptance follows.

### Return 7 — access outcomes and generated success continuation

September 19, machine reviewer: independently read the three proposed interface
traces and claim 07, then inspected the pinned AArch64 backend in the previously
independently downloaded `/private/tmp/natemu-track03-review-tcg-target.c.inc`.
The inspected revision is `c1c18d1e640b64292859ce9f30f3c344edfb0294`; no code ran.

**Verdict: accept claim 07 as a scoped source finding and the three outcomes as
a conditional interface argument.** `tgen_qemu_ld/st` emit the direct operation
then capture `raddr` when a slow-path descriptor exists. The slow load path calls
its helper, emits helper-result restoration and jumps there; the store helper
path jumps there after the effect. This establishes a reusable generator-owned
success edge. It does not establish the adapted trap ABI, complete descriptors
for accesses that lacked upstream slow labels, or VM-valid helper pointers.

The distinction resolves two concrete protocol errors: retry after a serviced
store repeats an effect; advancing after a mapping-only load repair discards the
load. Source data/result locations and operation identity must come from the
pinned generated expansion/frame, including signed/zero extension and register
width where applicable, rather than the guest opcode or a generic PC increment.
The proposed result contract and retained frame explicitly carry that burden.
A single aligned scalar native access supplies a useful bounded case; the text
does not extrapolate it to native pairs/exclusive loops or device side effects.

The tracked-store trace correctly holds native code/metadata until its success
edge and generated checkpoint finish, despite invalidating future block entry.
The REP trace retains the already-read source temporary and updates pointers/
count only after destination completion. A repeat service notification must not
repeat the store or advance the phase; the immutable identity/phase gate is
therefore load-bearing. Likewise a second fault during drain must yield to the
same coordinator instead of recursively acquiring it. These are stated premises,
not verified implementation properties. Fault reconstruction must preserve prior
legal partial effects and abandon the success edge, as the third outcome says.

No substantive return is needed for this interface scope. Full acceptance still
requires precise continuation/result metadata, complete frame restoration and
exclusive-service ordering, generated normal/REP checkpoints and VM-local fault
exit. RF/interrupt-shadow semantics, split accesses, atomics and MMIO retain their
own obligations. This review does not turn scalar examples into architecture or
all-instruction acceptance and makes no author-file edits.


### CPU inventory return — CLM-03-01-08

September 19, independent reviewer `/root/translator` (continuing CPU claim05
review): **accept claim08's scoped core-feature source intersection, realization
dependencies, permissive MSR default and TSC-helper seam.** No CPU realization,
complete CPUID/MSR profile or unsupported-feature policy is accepted.

Independently inspected the pinned Haswell model/version2 overrides, system TCG
feature masks, TCG vme default, feature expansion/filtering and CPUID realization
code. The seven-word table agrees with the requested model intersected with
those masks plus explicitly described dynamic/default additions. SMEP, RDRAND,
MCE/MCA and PAT/MTRR survive; PCID/TSC deadline/INVPCID do not. The omission of
unrequested VMX despite raw capability initializers, topology-dependent HT,
dynamic OSXSAVE/save sizing, PMU-off default and physical-width dependency are
correctly distinguished from an observed final CPUID dump. No extra reductions
follow merely from calling the model Haswell-v2.

Inspected system misc_helper.c's complete RDMSR/WRMSR switches and machine-check
bank defaults. Unhandled reads become zero and writes are ignored, while
selected invalid cases explicitly raise #GP. The document correctly treats this
as an unresolved virtual-CPU policy seam, not proof that all absent MSRs should
fault or an assertion of an upstream QEMU bug. Defined behavior must be selected
against the intended profile and primary architectural/initialization contracts.

Confirmed MSR_IA32_TSC is 0x10 in cpu.h, absent from those system switches, and
gen_RDMSR/gen_WRMSR call the corresponding helpers. Separately helper_rdtsc uses
cpu_get_tsc plus tsc_offset. The concrete conclusion follows: unchanged reuse of
these particular paths cannot provide the candidate's coherent TSC instruction/
MSR interface. An explicit shared clock/offset adapter is required. This is not
a runtime result or a claim about other QEMU accelerators and their MSR routing.

No substantive return. The next complete admitted-MSR/rejection manifest remains
a design obligation; actual realization and instruction tests later validate
conformance. No author record edited and no guest/source execution.


### MSR policy return — CLM-03-01-09

September 19, independent reviewer `/root/translator`: **accept claim09's scoped
primary-source rules, FS_BASE counterexample and conditional validation-layer
rationale.** No complete field/reset/MSR manifest or realized CPU acceptance.

Read the retained official Intel Volumes2B/2D/4 revision092 (June2026) RDMSR,
WRMSR and relevant register definitions; independently compared the pinned
system helper cases and gen_WRMSR. WRMSR's canonical-address check explicitly
includes FS_BASE. For the proposed 48-bit linear profile, 0000800000000000 has
bit47 set without the required high sign extension. The helper directly stores
that value and the frontend adds no value validator. Thus delayed rejection at
a later FS-relative access cannot satisfy precommit validation of this WRMSR.
The source-derived discrepancy is established without execution; it does not
claim all helper paths or accelerators share the same behavior.

The primary instruction contracts support privilege/index/reserved-field checks,
MTRR invalidation including global translations, and register-specific
serialization distinctions. The submitted validator preserves register-specific
read-only/reserved behavior instead of universally masking bits. TSC_ADJUST's
feature predicate and TSC_AUX's reserved upper32 field agree with the register
table. The finite family list is properly a work partition rather than a falsely
complete supported-MSR manifest; unsupported-index #GP requires completing the
admitted rows first.

Read both cited Linux Intel initialization files and v6.12 common.c. Family6
model60 reaches the non-safe MISC_ENABLE read controlling fast-string/ERMS
capability handling. The named MISC_FEATURES_ENABLES and PLATFORM_INFO probes
use safe wrappers and conditional failure handling. These source paths support
the distinction between required compatibility state and optional absence; they
do not establish every boot-driver MSR access. SYSENTER/syscall initialization
and the Intel CSTAR skip agree with the submission. A safe wrapper alone is not
a promise that all downstream functionality tolerates every absent register.

No substantive return. The smallest correction remains admission/value checks
before mutation or host side effects, retaining inherited handlers behind the
validated interface and routing TSC through the shared clock owner. Complete
bit/reset/effect rows and remaining initialization call sites are design work;
later implementation tests validate compliance. No source execution, guest
execution or author-file changes occurred in this review.


**MSR return extension — sibling FSGSBASE write paths:** independently rechecked
the appended claim09 sibling paragraph against Intel092 Vol2D pp6-6–7 and the
pinned decoder/emitter. **Accept the scoped extension.** WRxxBASE entries require
FSGSBASE, 64-bit mode and F3, with zextT0 for operand sizing; gen_WRxxBASE checks
CR4.FSGSBASE then directly moves the value into the FS/GS base global. The
specification requires #GP for a noncanonical effective source, so an MSR-only
canonicality validator leaves this exposed mutation path open. Sharing the
canonicality predicate is a coherent minimal direction, provided 32-bit source
zero extension happens first and the instruction-specific feature/mode/prefix
checks remain. No CPL0 gate should be inherited from WRMSR: these are different
instruction contracts. No broad address-mutation audit, runtime result or author
edit; the bounded extension has no substantive return.

## Return — control-state claim 10, September 20, translator reviewer

**Accept the scoped source counterexample and conditional precommit remedy.**
I independently inspected the pinned `gen_WRMSR`, system `helper_wrmsr` EFER
case and `cpu_load_efer`, plus the locally retained Intel 253668-092US §5.1.2
and 335592-092US EFER table. The frontend updates PC/CC and ends the block but
performs no compensating transition validation. With LM exposed, the helper's
update mask includes LME and excludes LMA. The submitted old active-mode state
and requested LME-clear value therefore produce LME=0/LMA=1 in that source,
where Intel requires #GP(0). This is a concrete source trace, not a runtime
reproduction or claim about other accelerators.

The proposed test must precede state mutation and compare requested versus old
LME; preserving identical-LME writes correctly avoids banning unrelated writable
EFER changes while paging is enabled. Distinguishing trusted state loading from
guest WRMSR validation is appropriate. NXE permission/reserved-bit interpretation
and redispatch/shadow coherence remain obligations beyond this predicate. I also
checked the local Linux v6.12 `intel_unlock_cpuid_leafs` bit-22 clearing and CPUID
refresh, and the bounded MISC_ENABLE source observations; raw storage is not
accepted as a complete register model.

**Small source clarification, no counterexample to the claim:** revision-092
Volume 3A §5.1.1 footnote 2 explicitly says WRMSR ignores source bit 10 (LMA).
The author can close that particular read-only-bit write disposition from the
already inspected primary text rather than leaving it generically open. This
does not close other fields, reserved writes or the coupled control-state rows.
There is no substantive return against the submitted EFER mismatch or smallest
remedy, and no acceptance of a completed CPU profile or implementation.

**Closure:** the author incorporated the explicit ignored-LMA-source-bit rule;
I rechecked that correction. Claim 10 has no outstanding review return.

## Return — twelve-question inventory cleanup, September 20

Translator reviewer; consistency review of existing records only, no new source
research. **Accept the cleanup in scope.** The main record now distinguishes the
fixed host-codegen/captive-shim architecture from open component choices and
maps all twelve charter questions to shared requirements, evidence owners and
closure work. ISA/state, memory types, exact recovery, all-observer atomicity,
code lifetime, event progress, clocks, direct boot, device completion and
containment remain required; none is silently discharged by a conditional source
finding. The active cleanup and unstarted common-interface pass are distinguished,
and row-specific closure work is not twelve new simultaneous work orders.

Claims 05/08 retain their upstream inventory while withdrawing its authority to
set our independent CPU profile. Claim 07 and its access traces explicitly depend
on TCG state/continuation conventions. The common outcomes survive a generator
change, but those proofs must be re-established. Other source findings and their
review history remain scoped; no new generator ranking or complete architecture
acceptance is asserted.

Two editorial returns were corrected and rechecked: the remaining CPU exposure
imperatives are explicitly confined to the historical candidate, and the
retrospective now preserves the exact six inference/scope IDs plus eight
additional proof-artifact IDs after audit deduplication. Its counts distinguish
attached reasoning from false source facts and do not claim percentage effort
saved. The CF_PCREL requirement is also expressly conditional on the TCG adapter.
There is no outstanding cleanup return. Rebuilding the comparison and closing
component-dependent proofs remain substantive work; cleanup is not that closure.

## Return — common-codegen architectural dependencies, September 20, machine reviewer

**Accept the scoped requirement derivation and comparison boundary; no component
selection or architecture acceptance.** Independently read the added
“Common codegen contract: architectural dependencies” section, active-action
status, twelve-question inventory, guarded-EL1/state-communication proposal and
INV-03-02's current common-interface direction. This was a bounded read-only
architecture challenge using current records; no new primary-source research.

The addition correctly treats CPU profile, privilege/layout, walker and semantic
helper provenance as independently owned inputs. Comparing generators against
the same explicit hypothesis does not accept that hypothesis for the product.
Its state/recovery obligation allows register-resident/deferred state with exact
recipes, rather than forcing CPUX86State, spill-all boundaries or upstream
runtime conventions. The three access results express distinct effect outcomes;
their names, wire format and nonnesting/permanent-landing realization are not
universal codegen requirements. Resource/reference bounds must be derived for
the actual adapter, so the TCG-specific link/frame constants are not imported.

The performance paragraph preserves the intended native paging/resident-shim
advantage: common correctness does not mandate a host transaction, service call
or full spill per access/instruction. Actual synchronization and recovery remain
required at the applicable effect/visibility boundaries. In particular, the
older coordinated-service traces do not make all-observer exclusion an intrinsic
requirement for every ordinary shadow hit or local repair. A cheaper candidate
must still establish the authority/visibility needed for its case; no relaxation
of x86 effects or private-state exclusion follows.

Translation identity, relocated resident addresses and cancellation lifetimes
follow the already stated execution/publication requirements without selecting
transport or serial artifact format prematurely. “Replacement” and “unknown”
are correctly not numeric effort estimates or rankings. Current action accurately
reflects the resumed bounded interface pass, and this integration does not
create another independent research campaign.

No substantive return. The forthcoming INV-03-02 adapter artifact needs its own
review; this verdict does not preaccept its generator-specific mappings or prove
that any candidate implements the common obligations. No author-file edits,
runtime execution or measured performance conclusion in this review.

## Return — translation request ownership (claim 11)

**Reviewer:** machine investigator, independent of root author. **Verdict:**
accept the scoped conditional ownership/protocol choice; no substantive return.
Inspected the new “Translation request ownership” section and its surrounding
state/communication and common-codegen requirements, with the already reviewed
INV-03-04 bounded request/lifetime construction. This is an architecture argument
review, not a new source audit, experiment or acceptance of a concrete transport.

The proposal identifies the correct publication authority. Snapshot capture and
final admission share the relevant invalidation serialization, while compilation
uses immutable copies without holding that authority. Registering dependencies
before reachability prevents the check-then-publish race only under the expressly
required shared ordering: an invalidation during installation must either revoke
publication or revoke the registered entry before affected execution. Cache
maintenance alone would not establish that ordering; the text does not claim it
does. Direct links and saved continuations are explicitly included, preventing a
cache-entry-only interpretation. Completeness of dependency capture and the
actual coordinator protocol remain premises rather than assumed source facts.

Cancellation addresses both logical staleness and physical lifetime. VM lifetime,
slot incarnation and request identity prevent a numerically matching late reply
from becoming current, while acknowledgement/draining protects the storage before
any stale callback writes. The alternative identity-resolved destination retains
a reference across the copy; checking only at completion is explicitly rejected.
A published code object transfers to its separate lifetime owner, so cancelling a
waiting demand must not be interpreted as immediately freeing code pinned by an
unrelated execution. The described lifetime distinction supports that reading.
Full namespace drain before wrap covers queued replies and is a sound bounded
resource alternative to keeping all historical requests forever.

Guest event handling preserves the guest-state owner and legal delivery boundary.
Cancelling the old demand when an event changes its continuation, then dispatching
again, avoids resuming a reply's obsolete RIP. A reusable compiled object and a
particular waiting continuation are consequently distinct: even if an object is
still valid for future dispatch, a late reply grants no permission to resume the
old continuation. The proposal requires this property without forcing every event
to discard all potentially reusable code.

The progress rule is conditional but appropriately concrete. Required resident
coordination remains serviceable during compilation waits; reserve pressure blocks
new work while admitted work drains, with permanent service capacity retained.
A permanently hung producer cannot be made safe by a timeout followed by slot
reuse; contained termination plus actual delivery-path retirement is the specified
failure alternative. The rule does not prove bounded compile latency, starvation
freedom under unlimited source mutation, or a specific queue capacity, and its
closure scope does not claim those results. No additional host call, full spill or
global rendezvous is imposed on every guest access or instruction.

The remaining implementation premises—coherent captures, concrete entry/cache
publication, recovery, reference counts and bounded adapter outputs—are explicitly
owned elsewhere. They limit this acceptance to selecting ownership and required
transitions, not proving an implemented state machine or completing charter
questions 3/7/8/12 or the combined architecture. No author-file edits were made.

### Claim 11 follow-up — typed source-fetch interaction

**Verdict:** accept the two added “Integration with typed source capture”
paragraphs in conditional protocol scope. Independently read the addition against
INV-03-04 claim 16's source-byte admission contract; no new source or runtime
claim is introduced.

The correction matters: compilation on an immutable copy has no guest effect,
but obtaining that copy may already have performed typed, side-effecting fetch
traffic. Cancelling a compiler request cannot erase that traffic or permit a
late response to repeat it. The resident fetch record retains fragment/effect
identity, while a genuine fault/event abandonment still uses architectural
restart rules. This is consistent with the prior distinction between a waiting
demand, a produced object and their separate lifetimes.

Continuation requests grant no direct guest-memory authority to the host. They
are admitted only for the live fetch/current instruction under mode, length and
memory-type constraints. The trusted decoder still supplies the instruction's
need for further bytes; this is not an acceptance of an arbitrary host-selected
range or a requirement to build a second full resident decoder. Binding completed
input/dependencies before publication preserves claim 11's invalidation ordering.
Ordinary side-effect-free cacheable lookahead remains allowed by claim 16, so this
interaction does not impose single-instruction blocks or per-byte host round trips
on the fast path. Actual adapter continuation granularity, fetch lifetime bounds
and publication implementation remain unproved. No substantive return.

## Return — coupled control-state transaction (claim 12), memory reviewer

**Independent scoped source/design acceptance.** Read the complete submitted
transaction and independently checked the retained Intel092 Vol.2B control-MOV
entry (pp.4-32–34), Vol.2D XSETBV (pp.6-70–71), and Vol.3A mode-transition,
legacy-PAE and translation-invalidation rules (§§5.1, 5.4.1, 5.10.4.1). Also
inspected pinned QEMU `cpu_x86_update_cr0/cr3/cr4`, `helper_xsetbv` and
`helper_clts`. This review performs no build or native execution.

The instruction-specific distinctions survive scrutiny: ignored CR0 low reserved
bits are distinct from reserved high-bit faults; CR3 interpretation is mode
specific; invalid control-register encodings are not generic MSR faults; CR8's
nonserializing contract is explicit. XSETBV retains its unsupported/OSXSAVE #UD
conditions and value/privilege #GP checks, with additional component dependencies
conditioned on the exposed profile. The document does not claim a complete
instruction-exception priority catalog or impose MOV's undefined flags on LMSW.

The PAE distinction is load-bearing and correct. Section5.4.1 names the exact
CR0/CR4 reload triggers, the new-root MOV CR3 load and task-switch path. Its
present-plus-reserved test gives #GP and leaves the PDPTE set unloaded. Selecting
present-entry-only validation is consistent with the separately documented
optional nonpresent-entry check. These retained hidden values are guest semantic
state, despite Intel calling the implementation registers non-architectural;
discarding native shadows or over-invalidating TLB entries cannot silently fetch
new PDPTE values. The source-backed permission for additional cache invalidation
therefore supports the selected eviction policy without supporting arbitrary
reload of this hidden state.

The combined commit preserves the relevant ownership boundaries: validate/stage
before publishing the control tuple; reconcile native mappings and compatible
code-entry keys before next admission; keep old instruction recovery from undoing
a committed transition; and retain independently allowed memory effects rather
than pretending a control fault rolls back all prior work. Shared shadow
invalidation does not change another vCPU's control state. State-only TS/XCR0
changes do not acquire a needless global memory flush. The contained-failure
branch applies to failed host synchronization, not to routine legal guest mode
changes or missing legacy walkers.

Pinned helpers support reuse of state derivation but not wholesale validator or
runtime reuse: `cpu_x86_update_cr3` has the cited legacy-PAE validation TODO;
`cpu_x86_update_cr0` even has an exception TODO for invalid long-mode activation.
The explicit resident validator and alternate LMSW/CLTS/reset/task-switch seams
are therefore necessary. Loader/task-switch changes retain their own validation
and partial-progress contracts rather than inheriting guest MOV admission.

Accept the selected resident admission/commit boundary and conservative cache
policy under the stated profile and existing synchronization premises. This
closes that architectural integration choice; it does not accept a final CPU
profile, every walker/segment/task-switch path, FP save/restore, or actual native
shadow implementation. Those are correctly identified independent obligations.
No substantive return or new experiment requirement.

## Return — coupled TSC/crystal/LAPIC frequency (claim13), memory reviewer

**Independent scoped source/design acceptance.** Inspected Intel253666-082US
CPUID Table3-8 leaf15, and the retained Linuxv6.12 `tsc.c`/`apic.c` paths named
in the claim. The selected ratio is internally exact:
24,000,000 ×125/3 =1,000,000,000Hz. ECX is crystal Hz, not CPU throughput;
EDX remains zero. Extending the maximum basic leaf requires the consistent
intervening-leaf dispatch already called out, and does not inherit a physical
Haswell CPUID image or require leaf16.

Linux's actual adjacent effect is correctly included: `native_calibrate_tsc`
checks Intel vendor/leaf availability/nonzero denominator and numerator, marks
a nonzero crystal as known frequency, and sets `lapic_timer_period` from that
crystal under CONFIG_X86_LOCAL_APIC. With the selected24MHz crystal its return
is exactly1,000,000kHz. `__setup_APIC_LVTT` programs divide16 on this path and
uses `clocks/APIC_DIVISOR`; the known-frequency branch of `calibrate_APIC_clock`
removes the dummy-clockevent flag on the explicit always-running-timer premise.
The author's24MHz LAPIC input plus guest-programmed divisor and continued
counting through HLT/descheduling therefore closes a real dependency, rather
than treating CPUID15 as an isolated TSC report. Intel's crystal field alone is
not a universal LAPIC-rate promise; this is the selected compatible virtual
platform construction bound to the inspected Linux path.

`detect_art` separately rejects hypervisor environments and requires nonstop
TSC, TSC_ADJUST and no asynchronous resets before its ART setup. The claim's
unadvertised adjustment/deadline features do not accidentally acquire those MSR
contracts by exposing leaf15. The shared exact host-time rational, per-vCPU TSC
write offset and independent LAPIC countdown preserve the selected epoch
ownership; late native wakeups recheck a virtual deadline instead of advancing
the clock. Whole-VM pause and host-sleep policy are inherited explicit choices,
not new physical-crystal claims.

Accept the numeric frequency/discovery seam under its Intel-vendor and common
time-owner premises. No substantive return. Full CPUID dispatch, timer register
semantics, native scheduling/interrupt conformance and boot remain separately
identified obligations. No runtime calibration result, throughput claim or
additional experiment requirement is implied.


## Question7 closure review — claim14, root

**Reviewer:** root, independent of machine's synthesis, September20.
**Verdict:** accept the selected code identity/publication/invalidation/lifetime
architectural contract under its named cross-question interfaces.

Read claim14 together with claim11's source/request lifecycle, claim02-37's
entry/continuation construction and claim04-12's capacity/reference argument,
plus the subsequent all-writer/REP/maintenance composition. Rechecked Intel092
§§7.6/11.1.3: synchronized new execution and internal runtime continuation are
distinct; preserving an admitted REP across an invisible pause is not authority
to execute stale following instructions after synchronization.

The first proposal constrained objects to single basic blocks/two exits and
omitted return caching to fit old capacity arithmetic. Review required removal
of that unnecessary coupling. The revision admits bounded CFGs with E external
links and bounded return predictions targeting guarded external entries. E*C
bounds detachable edge storage, not the retained allocation union. Quiescent
cache detachment and the census of already loaded targets close the pre-pin
veneer lifetime window. Native inter-object tail transfer, permanent helpers
and one admitted handover retain the old/destination/origin bound independently
of CFG size; no recursive generated ancestry or uncounted second handover is
permitted. These are adapter rules, not claims that unchanged FEX fits them.

The combined ordering closes question7's required races: dependency registration
and revalidation share the writer mutex, so publication cannot escape mutation;
code/metadata become reachable only after complete installation and executor
synchronization; warmed links still guard entry; invalidation preserves an
already admitted effect but requires dispatch before a stale suffix; cancelled
producer/delivery references drain before storage reuse. Source/route/context
identities distinguish physical aliases, permitted cached translations and
specialized CPU state. Side-effecting source fetch does not acquire arbitrary
reusable cache eligibility. Hashes do not substitute for identity comparison.

Reclamation removes eligibility edges under closed admission and frees only
outside the actual continuation/reference union. Reserved output/successor/drain
capacity prevents cache pressure from requiring REP completion or another blocked
coordinator acquisition. Retaining a native fault/helper PC retains its object;
unknown phases or timeout never authorize reclamation. No whole-arena reset is
selected while live references exist.

No substantive return remains. This accepts a complete code-lifetime design
contract, not a finished generator, full CPU/memory/event semantics, numerical
capacity sizing or performance. Those named owners must supply the stated
interfaces; incompatible later choices reopen this acceptance. Combined Track03
acceptance remains outstanding. Another mock is not required to choose this
contract; planning must demonstrate actual emitter/allocator conformance.


## Inventory status reconciliation — September 20

Root reconciled the current twelve-question inventory with the owning summaries.
Independent read-only reviewer `/root/machine` checked the inventory against
INV-03-02/03/04 and identified stale status wording; root applied those editorial
corrections. Accepted scoped evidence now includes codegen claim40 and memory
claims38–39; memory claim40 and serial claim60 remain pending independent review.
The memory inventory points to the accepted question7 contract in INV-03-01
claim14. Historical preparation and superseded next-step wording no longer
override subsequent findings. No new source research, experiment, generator
ranking or architectural acceptance results from this reconciliation.


## Guarded-EL1 composition review — claim15, memory

**Reviewer:** memory, independent of root's author submission, September20.
**Provisional disposition:** address/control/state/transport construction is
supportable; one wake-authority clarification remains before closure.

Read claim15 against accepted claims11/14, INV03-02's retained-frame/IRQ-scratch
and helper-result interfaces, and INV01-33 claim10's finite register-transfer
publication/WAIT findings and limits. Checked the current SDK26.4 hv_vcpu.h
run/forced-exit contracts. This is source/interface review, no native execution.

The unsigned full-span predicate is sufficient to exclude the private gap and
wraparound on its direct route. Its rejection correctly invokes the semantic
owner, not a universal #GP, and does not erase legal subaccess/partial effects.
Physical resolution separately excludes private backing; native NX of guest RAM
and classification of guest versus resident targets prevent a guest numeric RIP
from acquiring native EL1 authority. Implicit/helper/stack access coverage is an
explicit adapter requirement, not a hidden native-pointer exception. This remains
a trusted compiler/runtime interface, not containment against malicious emitted
native code.

Live guest roots remain vCPU-owned. The distinct resident entry frame/IRQ scratch,
pinned recovery metadata and permanent landing preserve arbitrary native stop
state without pretending it is a canonical guest boundary. Claim14 supplies
entry/return/lifetime identities; claim11 supplies immutable source requests and
cancellation. Register transport requires resident-chosen destinations and retained
roots before lane reuse, consistent with the inherited finite mechanism. No shared
host queue, full-ISA exporter correctness or performance is inferred from it.

**R15-1:** the blanket statement that only the owning thread performs vCPU API
operations must distinguish owner-only run/get/set from the cross-thread force-
exit control path. SDK hv_vcpu.h states hv_vcpu_run blocks until exit/cancellation;
hv_vcpus_exit forces running CPUs out and latches an exit for the next run when
already stopped, without the run/get/set owning-thread restriction. Otherwise a
compiler-response/control thread has no specified way to notify an owner blocked
in native execution under the literal rule. Requested an explicit bounded wake
request plus pending-identity recheck (or a concrete bounded WAIT/poll route),
without treating the forced native PC stop as canonical guest state, overwriting
live registers, or requiring guest IF. This is a narrow progress-interface return;
it does not require a whole generator implementation or a new experiment.


**R15-1 recheck and final verdict:** author amended claim15 to distinguish owner-
only run/state operations from bounded control-thread hv_vcpus_exit, publishing
request identity/epoch before the kick and rechecking after exits/before reentry.
The SDK's latched-next-run exit handles the stopped/running transition; durable
request state handles stale/coalesced notifications. Arbitrary stopped native
roots remain retained, and only the permanent service stop permits transfer-lane
reuse. Existing control/rendezvous and WAIT progress do not depend on guest IF or
compiler completion. I independently re-read the amendment; R15-1 is closed.

Accept claim15's scoped question3 architectural composition. No substantive
return remains. Named memory, recovery, event and lifetime owners still supply
their stated interfaces; contradictory adapters reopen the choice. This accepts
neither an unwritten emitter nor complete guest fault/event semantics, transport
performance, malicious-native containment or combined Track03 correctness.


## CPU exposure review — claim16, root

**Reviewer:** root, independent of memory authorship, September20.
**Initial verdict:** return three concrete discovery inconsistencies before
accepting the scoped profile proposal. Full question1 remains open on the stated
compatibility-register seam independently of these returns.

Read the submitted feature/topology/cache/XSAVE/MSR table against Intel
253666-082US CPUID descriptions and253668-092US APIC/control contracts. The
virtual cache geometry,40-bit physical/48-bit linear widths, one-thread-per-core
IDs and standard832-byte extended-state layout are coherent choices. Explicit
MCE/MCA, XSAVEOPT and ERMS exclusions leave the stated x87/SSE/AVX2 surface
intact and must remain visible as profile compatibility limits, not evidence
that any code generator is incapable. No physical Haswell-equivalence or stock
boot result is inferred.

- **R16-1:** CPUID.1 EDX’s enabled list omits MTRR although42 and16 select
  MTRRCAP/DEF_TYPE/range registers. Intel CPUID bit12 explicitly enumerates that
  facility. Align discovery with the selected supported mechanism.
- **R16-2:** Intel082’s80000001 SYSCALL/SYSRET footnote enumerates the flag as0
  outside64-bit mode. State this execution-mode-dependent bit; LMA alone includes
  compatibility mode and is insufficient. This complements, rather than replaces,
  the selected Intel compatibility-mode instruction rejection.
- **R16-3:** Intel092 §13.4.3 states that clearing IA32_APIC_BASE[11] also clears
  CPUID.1 EDX.APIC. Derive discovery from the authoritative global-enable state;
  do not conflate it with SVR software disable.

These require table/state-coupling corrections, not a new experiment. The
remaining MISC_ENABLE/microcode/platform register contract must still settle
its actual allowed fields and effects, including CPUID limiting where selected;
a named Haswell identity or permissive upstream MSR default cannot settle it.

**Recheck and final verdict:** accept the amended scoped CPU exposure/state
proposal; R16-1–3 are closed. Re-read the actual table: MTRR is enumerated,
SYSCALL discovery uses64-bit execution rather than LMA alone, and APIC discovery
follows global enable independently of SVR. OSXSAVE remains tied to its committed
control bit. Cache fields now explicitly distinguish the WBINVD scope flag;
geometry arithmetic and the standard XSAVE576/832-byte boundary agree with
the primary CPUID definitions. No physical-cache or instruction-throughput
claim follows from those virtual geometry values.

This accepts the profile’s chosen fields and architectural dependencies, not
question1 closure or every binary built for physical Haswell. The explicit
optional-feature differences and required full x87/SSE/AVX2 state remain in
force. MISC_ENABLE/microcode/platform register fields, their Linux call sites
and their effect on discovery still need the named finite compatibility pass.
No substantive return remains for16’s stated scope; runtime/boot, complete
semantic-component conformance and generator ranking remain unestablished.


## Compatibility-register review — claim17, root

**Reviewer:** root, independent of memory's authorship. **Verdict:** accept17
and the scoped question1 virtual CPU exposure/state/control contract composed
with12/13/16 and INV03-04 claim42.

Read the complete proposal and independently inspected Intel Vol4 Table2-20's
MISC_ENABLE fields, generic BIOS_SIGN_ID and CPUID-limit definitions, Intel
Vol2A082 CPUID visibility/out-of-range rules, and retained official Linux v6.12
intel.c, microcode.h and microcode/core.c. Linux's mandatory revision helper
writes8B, executes CPUID1 and reads8B even though the hypervisor bit disables
the microcode loader. The proposed latch supplies that path; rejecting an update
trigger does not replace the required identity interface. Safe platform/misc
feature probes may fault without forcing fictitious successful values.

The write mask agrees with the declared virtual register: only0/22 change,
read-only status is reinstated, and unsupported set bits fault before mutation.
CPUID limiting and per-vCPU context generation are coupled. The model-specific
reserved low bits are not replaced by generic thermal-control storage.

XD-disable34 is an explicit omitted BIOS control, not an assertion that physical
Haswell lacks it. This keeps the selected NX feature and EFER.NXE behavior;
it does not infer an unsupported active-NXE transition. Likewise revision0,
platform ID0 and no update engine are declared virtual identities, not promises
of physical Haswell equivalence or firmware patch compatibility. Acceptance is
for this exposed virtual profile and the selected Linux paths. A requirement
for those omitted physical-model controls would reopen the profile.

The finite profile now states discovery, architectural state, control transitions,
time coupling and compatibility-register effects. Question2 still owns semantic
component adaptation; question5 owns complete fault/partial-effect behavior;
question8 owns event reset/reception composition; question10 owns actual loader
and lifecycle integration. These remain substantive architecture work, not
claims of implementation conformance hidden inside profile acceptance. No stock
Linux boot, full opcode correctness, performance or generator ranking follows.

## Inventory reconciliation check — translator, September20

**Verdict:** editorial consistency check passes; no architecture acceptance or
new source finding. Re-read the current summary, all twelve ordered charter rows
and the common dependency table against the owning current records and cited
review dispositions. Previously returned stale references are corrected:
guarded EL1 is selected; the unbuilt FEX preparation is not a TCG gate; selected
semantic evidence includes43–50; production containment is distinguished from
accepted question12; and72/73 with root Returns51–52 close the scoped question11
contract. Both accepted-question lists include11 and leave2/5 open.

Question5 explicitly retains claim51/Return41's provisional DR6 fusion ordering;
accepted delivery and retirement ownership do not silently accept that semantic
choice. No codegen cost/speed ranking or complete implementation follows from
these statuses. The table has exactly rows1–12 without omissions or duplicates.
The dedicated retrospective link resolves to
`docs/retrospectives/track03-architecture-drift.md` and is labeled history, not
evidence or an alternate inventory. No remaining stale item from this return
is identified; the combined architecture remains unaccepted as stated.

## Claim18 integration review — translator, September20

**Verdict:** accept the conditional integration argument, not the completed
architecture or questions2/5. Reviewer is independent of root's claim18 authorship.
Used existing accepted records only; no new source or runtime evidence. No
load-bearing contradiction found in the ten stated interactions.

Checked the actual composition against01 claims11/14/15,02's current semantic
owners and53 refinement,03's69–73 device/authority/lifecycle contracts, and04's
coordinated misses, ordering and backend completion contracts. In particular:

- Source registration and mutation share one publication ordering point; host
  compilation is outside that exclusion. Operation-bound UC fetch inputs retain
  claim14's restriction and completed effects rather than becoming reusable WB
  snapshots merely because the host sees immutable bytes.
- Coordinator ownership spans an admitted walk/access and its bookkeeping, not
  external disk/network completion. Device MMIO registers provide bounded local
  transaction results while real I/O proceeds from staged work. This preserves
  the distinction between a completed register access and completed external I/O
  and supplies no callback-to-held-RAM-grant wait cycle.
- Memory-quiescent acknowledgement does not release code/frame/return pins. The
  landing captures roots before reuse/clobber, and typed continuations retain
  successful effects across pauses. Incarnation retirement does not fabricate
  cancellation, roll back disk effects or permit old callbacks to target new IDs.
- The resident guarded-EL1 compiler/helper trust premise is separate from host
  process containment. The explicit trusted supervisor/ICMP/DNS endpoints are
  not claimed protected from their own malicious policy implementation. The
  fallible network worker retains only its admitted TCP/UDP authority and staged
  channels; no network feature supplies guest RAM, disk or peer task rights.
- Boot/table/device identity and active-time/timer ownership are coupled to the
  same admitted manifest and epoch. Coordination pauses retain the accepted time
  policy, and IRQ delivery follows admitted payload/ring visibility.

This review does not reaccept individual components authored by this reviewer.
It accepts their stated interface composition, conditional on their own reviewed
premises. Named-M4 aligned128 behavior, private OS enforcement/service admission
and inherited numerical conformance remain explicit assumptions; no finite probe
is promoted to universal platform correctness. Costs remain unmeasured.

The open compound-progress and DR6 fusion mapping under question5, and remaining
semantic/numerical boundary under question2, are not supplied by a generic owner
record. Those gaps remain as claim18 states. Once their disposition changes,
recheck only affected interactions rather than infer that this conditional
integration already authorized planning or completed Track03.

## Bounded completion-breadth inventory audit — translator, September20

**Scope and verdict:** independent inventory/breadth check of the current Track03
charter, four owning summaries, combined18 and existing downstream records.
No new source campaign, runtime or acceptance of this reviewer's prior component
claims. Final Track03 acceptance remains open while questions2/5 and the final
composition disposition remain open. No additional architecture-invalidating
counterexample was identified by this bounded inventory check.

| Breadth area | Finding and smallest remaining closure |
| --- | --- |
| Product | Modern unmodified Linux/direct boot/SMP and required devices remain explicit;69 records image admission and optional lifecycle exclusions. Existing records do not supply an extant accepted concrete diverse workload portfolio or quantitative success threshold merely because historical INV00 rows say accepted. Current Q00-01/02/03/04 must bind real records or resolve missing inputs before their use in synthesis. This is not permission to narrow the architecture to a benchmark or single-vCPU milestone. |
| Architecture | All12 charter rows and ten18 interactions are present. Guarded EL1, TCG/adapted semantic policy, resident ownership, source lifetime, coordinated memory and staged devices are distinguished from complete upstream runtimes. FEX/Box, EL0, transport/placement variants and conditional vmnet remain recorded alternatives without invented rankings. Questions2/5 retain their precise semantic/recovery issues; generic accepted interface types do not answer them. |
| Correctness | Source constructions, named-platform premises, finite experiments and future conformance are distinguished. M4 aligned128 and private OS policy assumptions remain visible; M2 parity and every-kernel-bug proof are not invented requirements. Numerical/status and provisional DR6 fusion remain with02. Changes to those owners require rechecking only affected18 interactions; no new all-opcode or full production implementation gate is inferred. |
| Methodology/economics | Previous bounded failures, independent reviews/replicas and exploratory limitations remain retained. No stock boot, universal runtime correctness or speedup follows. Guarding, register transport, coordinator misses, fences, copying, trusted endpoint and numerical adaptation costs are disclosed but unmeasured. Q04-01 and its mechanism questions own ranges/sensitivity; Q05-01 owns fair qualified measurements; Q06-03/06 own lifecycle effort and bounded adaptation before implementation. These inputs remain necessary for economic/synthesis claims, not a reason to fabricate performance findings here. |

**UEFI/Windows and licenses.** Existing03 claim69 and its alternatives section
supply the required implication analysis: the Linux direct-loader/mailbox/device
contract does not supply UEFI services/runtime/Secure Boot or Windows loader,
driver and AP-startup compatibility. No implementation or Windows impossibility
is implied. No additional survey is required for that stated implication.
The existing licensing warning covered QEMU/FEX/Box but omitted the newly selected
Bochs numerical slice. Added an explicit exact-file/dependency/notice inventory
warning in02, without inventing its license terms or creating a selection veto.
Planning must include this extraction in its provenance/license work.

**Editorial corrections made:** added a narrow backlog navigation disclaimer
that unavailable historical INV00 acceptance is not current evidence, linked the
main scope handling and current Q00/04/05/06 owners; preserved the historical rows.
Removed the unsupported fixed “microseconds per exit” assertion from Q04-08 and
clarified Q06-06's single-vCPU shell as a delivery milestone, not the product
scope. Root owns contemporaneous01/03 summary reconciliation; those regions were
not edited here. `git diff --check` passes.

**Remaining closure:** resolve the explicitly owned question2/5 matters, then
reconcile the conditional integration and obtain final architecture/breadth
acceptance. Before synthesis makes a material-benefit or authorize-planning
recommendation, bind its actual scope/workload/threshold/cost/budget inputs to
available reviewed records. This audit adds no new indefinite research task and
does not convert downstream implementation checks into architectural blockers.

## Current-direction reconciliation — September 20 owner corrections

Reviewer `/root/direction_review`, independent of root's edits, inspected the
workflow, Track01/Track03 charters, INV03-01..04 active summaries/inventories,
investigation/review templates and the framing/decision-displacement retrospectives.
No archived history was inspected. Review used one inspection and one final
changed-material review; no experiment was run.

**Evidence verdict: accept as documentation reconciliation only.** Scoped
findings, unresolved semantic claims and historical reviews remain intact. No
new correctness, performance or whole-architecture acceptance is asserted.

**Decision/direction verdict: supports the corrected next step.** Active records
withdraw the TCG/QEMU/Bochs preference and automatic native-Intel dependency.
Performance comparison covers viable generators and separately chosen numerical
helpers, with the owner's x87 cost tradeoff explicit. The existing workflow now
requires selective lesson retrieval, in-place current-status corrections and
separate evidence/relevance judgments. The work areas contribute to one bounded
comparison-method pass; experiment budgets still need to be specified before
runs. No substantive blocker or unresolved return remains for this correction.

Comparative justification remains unfinished technical work in INV-03-01/02.
This review does not turn the documentation fix into resolution of the recurrence.

## Performance-comparison method review — September 20

**Reviewer:** `/root/direction_review`, independent of the method and source-entry
authors. Inspected the current workflow and Track01/03 charters, INV03-01's
performance-led method, INV03-02's emission-route appendix, and the two relevant
retrospectives. One review and one correction verification within the shared
30-minute reviewer allowance; no archived history, build or execution inspected.

**Evidence verdict: accept as a bounded exploratory method.** The method keeps
actual artifacts, unimplemented adaptation estimates and captive timing distinct.
This verdict does not independently accept every appendix source claim, approve
unwritten export patches or establish buildability, performance or correctness.
The proposed patches still require their recorded preparation/inspection. Source
availability and successful compilation cannot rank candidates.

**Decision/direction verdict: supports the proposed build/emission step.** The
three generators receive the same fixture/feature requirements and bounded effort;
their own state and helper conventions remain visible. Obtaining full upstream
output is a finite prerequisite to a valid execution comparison, not another
recovery-exporter campaign. Six MOV/live-state fixtures test export readiness,
not representative performance. Later selection requires actual captive timing
of useful work and accounting for missing adapter costs. Numerical-helper costs
and the owner's x87 accuracy tradeoff are independent of generator preference.

**Returns resolved:** the first draft bounded wall time but left diagnostic and
review effort implicit and could read as automatically opening another campaign.
The corrected method caps the entire follow-on exploration at both180 wall
minutes and180 aggregate agent-minutes. Shared setup, each candidate's export
patch/inspection/build/correction, and analysis all consume that allowance.
Retained artifacts plus an execution proposal or insufficiency end the campaign;
an extension requires the owner's additional-budget decision. The separately
bounded method preparation does not reset either allowance.

Container memory/jobs, transfer and disk ceilings, snapshot builds, failure
retention and container cleanup are explicit method requirements, not verified
runner behavior. Candidate-specific failure remains an unknown rather than a
performance loss. No substantive blocker remains for this method; no component
winner, execution-code approval or Track03 completion is implied.

## Compile-only exploration findings — September20

Independent reviewers inspected fresh retained outputs, not just prior claims.
`/root/direction_review` checked FEX/Box64 (about4 minutes), including exact input
bytes/counts, object bounds, relevant hashes and preserved failures.
`/root/comparison_sources` checked TCG (22:41:24–22:42:29UTC), all12,401 hashes,
native/disassembly byte agreement and independently decoded search records.
Each reviewer excluded the exporter they authored from their independent verdict.

**Evidence validity: accept exploratory acquisition only.** All three generators
built and exported seven fixtures. No translated guest execution or timing took
place. FEX's expected-count/cache mismatch exit remains visible and is not counted
as an upstream test pass. TCG's direct-memory Linux-user context is not a captive
system-semantics proof. Failures and corrections have separate retained identities.

**Decision relevance: acquisition uncertainty resolved; selection remains open.**
Raw sizes use different boundaries, ordering and state conventions. FEX includes
CI traps/linker data; Box64 excludes table metadata; TCG omits a shared prologue.
Neither instruction counts nor NOP subtraction establish relative useful-work
cost. Both reviewers support bounded captive body execution with explicit state,
entry/return and excluded-cost accounting; residual adapter/helper/reuse costs
may reverse a fixture result. No generic recovery-field audit is needed before
that experiment. The completed campaign ends with a concrete proposal, not an
automatic extension or component choice.

Final current-record check by `/root/direction_review` confirmed the artifact
matrix and proposal limits, and caught one stale inventory-row instruction to
rerun completed emission work. Root replaced that instruction with the additional
execution-budget decision and changed prospective preparation wording to recorded
status. This correction illustrates the direction check; it adds no evidence.

## Captive paired-body exploration review — September20

`/root/comparison_sources` checked root's execution integration and caught unequal
FEX input flag encoding before execution. It independently verified the focused
host-counter correction in the actual binary, all hardware-profile slice hashes,
exact instruction changes and shifted fault sites. `/root/decision_audit` authored
the independent complete-output oracle, including native fault partial effects,
retained-input/profile/hash admission, schedules and separate clock sanity; root
checked it against real disassembly. `/root/direction_review` independently checked
paired run hashes, reran its retained oracle read-only and recomputed timings and
scaling. It did not self-accept its hardware-profile patch. This is review of
exploratory evidence, not independent experimental replication.

**Evidence validity: accept the scoped exploratory observations.** Retained
`execution-paired-01` passes288 admission and756 longer output checks. Long clock
sanity passes, short admission timing does not. The earlier host-counter defect
is preserved in `execution-software-01`; its one-line correction produced fresh
`-02` and was verified in machine code. No complete captive recovery, stock boot,
formal timing qualification or whole architecture acceptance follows.

**Decision relevance:** FEX leads the inspected live cached-state fixtures after
removable software-ordering costs are addressed. The former TCG/Bochs preference
still lacks performance justification. The next consequential uncertainty is
whether guard/boundary/materialization costs erase that lead; the approximately
0.84ns/body FEX–TCG difference gives an explicit break-even test. Source familiarity,
raw object lengths, rare Intel quirks and perfect x87 accuracy cannot replace it.
Broader control flow, translation/reuse and helper costs remain selection limits.
No winner is accepted and no generic recovery audit is requested.

The final report narrows the sealed checker's overbroad “no adapted-TSO” wording:
a specific hardware-dependent lowering profile was tested, while full adaptation
and general TSO correctness were not. Reporting correction only; raw data remain
sealed. The campaign closed inside its recorded allowance; further work has its
own explicit proposed budget and decision consequence.

## Guard/resident-boundary results review — September20

Reviewer `/root/direction_review` inspected the current method and retained
`boundary-execution-01` manifest, relevant producer/checker/output hashes, oracle
contracts and actual output records. The reviewer authored the guard construction,
but not the helper adapters or result checker; this review does not independently
reaccept that guard. No experiment or oracle execution was repeated. Four agent
minutes are charged to the current60-minute campaign.

**Evidence validity: accept the scoped exploratory result.** The retained phases
report612 admission and1260 longer correctness checks passing. All90 reported
long-batch medians were independently recomputed from actual counter differences
and match. Both clock frequencies are24MHz; long host-clock ratio is
1.000–1.001744. Short admission timing fails its sanity criterion and is not timing
evidence. Fourfold iteration increases give median total-time ratios3.62–4.21
across configurations, with individual variation; this remains exploratory timing,
not independent replication, formal clock qualification or confidence intervals.

**Decision relevance: the prior FEX lead survives some boundaries, not all.**
Across all three live fixtures, FEX remains fastest by the reported medians with
guards alone, with the read-only resident view at every tested K, and with forced
canonical-record publication at K=4/16/64. At forced publication every body
(K=1), that ordering reverses: FEX is7.13–7.26ns/body, versus Box64's4.65–4.97
and TCG's4.81–5.04. The relatively small Box64/TCG differences in that regime
are not a robust universal ranking. The substantial FEX reversal is a concrete
boundary-contract/frequency sensitivity, not evidence against cached-state
residency or permission to mandate spills every block.

This answers the campaign's reversal question and supports stopping it. The next
selection-sensitive missing input is whether an actual required resident helper
and representative block sequence require the expensive publication/clobber
contract often enough to enter the reversal regime. A separately bounded next
comparison should replace the synthetic digest with one concrete required helper
and its declared live-root contract, measuring representative boundary frequency
and useful control flow. Keep cached-root preservation available; do not impose
an every-block canonical image by construction. Another repetition of the same
synthetic sweep or a rare-instruction source audit would not answer that question.
Broader workloads, compilation/reuse, true dispatch and numerical-helper tradeoffs
still limit final generator selection. No automatic follow-on is authorized by
this review, and no component winner or whole-emulator speedup is accepted.


### September20 — required-boundary proposal direction review

`/root/comparison_sources`, independent of the proposal author; bounded review
started23:33:53UTC, charged four agent-minutes within the ten-minute preparation
allowance. No code changes or execution. This reviews direction, not a completed
method or generator selection.

**Accept the proposed next discriminator.** INV-03-01 claim14 requires actual
per-guest-commit stop checkpoints and external-entry admission/pin handover;
INV-03-02 claim37 requires the warmed target veneer to preserve qualified native
roots while validating admission under the coordinator. An uncontended admission
transaction plus those checkpoints measures required work omitted by the digest
probe. Preserve native/deferred roots on success; do not invent a mandatory
canonical snapshot. Include the mutex, complete declared identities and pin
handover, rather than substitute a pending-bit read for admission. Rejection must
preserve the prior execution ownership and guest state. Object sizes1/4/16/64
are controlled sensitivity inputs, not established workload frequencies. One vCPU
can price the uncontended path, not contention or reclamation closure.

Per-cut recovery is the critical admission condition. Current sealed
`fex-hardware-01/outputs/fex.log` lines66–74 show vector EOR, ADD, scalar load,
SETZ and vector store in guest order, with CFINV only after that final store.
Post-ADD checkpoints therefore need the direct-carry flag recipe until CFINV,
not the final-body inverted-carry recipe everywhere. An earlier review message
incorrectly suggested FEX placed vector EOR after ADD; the retained log refutes
that recollection. Box64 and TCG likewise require actual intermediate roots,
not assumed final memory slots. The proposal correctly stops timing a candidate
without valid prefix cuts instead of placing arbitrary checks at its tail.
Use the actual entry identity for next guest RIP: INV-03-02 claim12 explains why
stored EIP may be stale on direct chaining. Slow semantic event delivery stays
explicitly outside this narrow stop/admission measurement. The proposed finite
budget, run limit and consolidated remaining comparison scope are appropriate;
this review does not authorize a new execution or supply independent acceptance
of previously authored FEX binding code.

### September20 — claim19 queued-fault protocol review

`/root/decision_audit`, independent of root's claim19 authorship; eight agent-minutes
charged to the bounded source/protocol review, including challenge and returns.
Read the current owner challenge, claim14's execution/lifetime construction,
INV03-04 rendezvous revisions1/2, admitted-route/remap rules, and the complete
claim19 proposal and returned edits. No code or experiment was authored or run.

**Accept claim19 as a conditional architectural revision.** No remaining
substantive blocker requires a universal hot per-instruction stop poll. Peers may
finish bounded old-view execution before mutation; a faulting requester retains
its admitted instruction and cold-completes only that instruction before current-
source dispatch. This reuses revision2 PAUSED/DRAIN_SERVICE rather than adding a
second state machine or rediscovering native-state retention.

Two concrete returns are closed in the inspected final text:

- **Admitted route is stronger than an inactive frame.** CPU0 can admit route R
  and shadow-fault before its access while CPU1 requests R→S. Merely dropping an
  active grant would permit incorrect rebinding or stale retry after remap.
  Claim19 now retains a separate completion obligation: bounded old-route service
  or semantically permitted effect-free cancellation precedes the conflicting
  remap, acknowledgement and reuse. Only an unadmitted next phase can rebind.
- **Mutex release is not guest progress.** Releasing a lock between indefinitely
  queued backend requests could leave ordinary admission closed forever. The
  final bounded-batch rule admits an actual round-robin ordinary interval after
  at most eight heads, under the stated healthy-host premise. New requests queue;
  faults remain serviceable. This closes algorithmic starvation without a
  per-instruction polling requirement.

The simultaneous-fault schedule is coherent only with safely detachable phases,
no drain-needed lock retained, actual memory-completion acknowledgements, and
bounded dependency service rather than recursive coordinator acquisition.
Self-modifying A cannot resume stale following B; the saved decode and completed
MMIO identities must survive every queue/revalidation step. Finite code size does
not bound an internal spin CFG, REP or helper: their bounded stop opportunities
remain load-bearing. Synchronous debug/trap and interrupt-shadow obligations keep
their precise boundaries through specialized lowering or cold exits; ordinary
asynchronous-event deferral does not absorb them.

Pinned native frames, code/metadata, routes and in-flight targets remain lifetime
references. Existing finite per-vCPU and service reserves must cover them before
admission; inactivity is not reclaimability. The accepted change is the protocol
and its explicit premises, not proof that an arbitrary emitter supplies the cold
completion recipes, full-ISA correctness, latency bound or speedup. A lowering
that cannot preserve an admitted phase is a counterexample to that lowering,
not permission to use routine VM termination or to assert blanket polling is
unavoidable. Synchronize claim14 and the active INV03-04 direction to this scoped
revision; no experimental or generator-selection conclusion follows.

### September20 — numerical-02 independent result and method review

`/root/decision_audit`, independent of numerical producer author
`/root/comparison_sources`; six agent-minutes charged within the ten-minute
subtask cap and shared consolidated-review budget. Inspected retained
`numerical-02/source/numerical.cpp`, `numerical.py`, `numerical.sh`, inherited
FEX/SoftFloat/Cephes source bindings, manifest, platform/compiler/library records,
all output rows, linked executable disassembly and analysis beneath
`/private/tmp/natemu-track03-comparison-raw`. Recomputed the numerical oracle and
timing checks locally without invoking either timed executable or a VM.

**Accept the finite exploratory account and its direction, not a generator or
whole-emulator performance conclusion.** All37 retained artifact checksums match.
The producing C++ is unchanged in the working tree; the subsequent Python change
only suppresses bytecode files and does not alter the retained computation.
`numerical-02` records successful bounded container execution and cleanup. The
failed container-creation/cleanup attempt remains separately recorded in
`numerical-01`; it is not a numerical result.

The independent calculation used exact rational decoding and a rational sine
series with the next-term remainder below10^-110, rather than the producer's
120-digit Decimal calculation or the tested Cephes/libm implementations. All26
reference values and both absolute-error columns agree within10^-108. The twelve
nearest-rounding cancellation cases give `(2^p+1)-2^p=1` in the tested extended
kernel for p=52..63; native binary64 returns1 only at52 and0 at53..63. At the
fourteen sine points, the retained extended outputs lie within half an extended
ulp of that independent reference (largest nonzero case approximately0.4561ulp).
This is a finite sample, not a numerical bound or Intel x87 conformance proof.

**Input precision is part of one control.** Sine case13 feeds `1+2^-60` to the
extended path and its rounded binary64 value1 to libm. Its reported binary64
error therefore includes input rounding; it must not be presented as libm's
approximation error for an identical input. The other listed sine arguments are
exactly representable in both formats. No exception/status, nonfinite, extreme
argument-reduction, subnormal or other rounding-mode contract was qualified.

All56 timing records have the declared batch/order/length/variant identity, and
their cyclic-output checksums independently match the26 point results. In the
retained executable, the binary64 cancellation kernel contains actual FADD then
FSUB. Both length loops call the selected kernels on indexed inputs and consume
each output; neither cancellation nor sine has been folded into constants or
hoisted out of its loop. The extended cancellation calls the retained add/sub
kernels; extended sine performs the retained ext80→f128/Cephes/f128→ext80 path.
The native sine wrapper tail-calls dynamically linked glibc libm. Thus these are
indexed kernel/call/checksum throughput measurements, not isolated operation
latencies or timings of FEX's full x87 frontend/state/profile adapter.

| Kernel | Long-batch median ns/call | Observed long-batch range |
| --- | --- | --- |
| Native binary64 cancellation pair |0.770105|0.763020–3.295833|
| Inherited extended cancellation pair |14.490938|14.451978–29.085833|
| glibc binary64 sine |3.570825|3.415625–3.686450|
| Inherited extended/Cephes sine path |228.171875|227.829175–229.530200|

The medians reproduce the author's report. Fourfold loop lengths give median
total-time ratios4.0333,4.0346,3.9176 and3.9208 respectively. Preserve the large
cancellation outliers; seven alternating-order batches do not establish precise
ratios or confidence intervals. Timing uses CLOCK_MONOTONIC_RAW inside the
recorded OrbStack Linux ARM64 container, with CPU quota and uncontrolled
placement/scheduling. It is not a captive-EL1 timing, qualified cross-clock
measurement or independent timing replication. Timing does not include per-call
x87 state admission/status reset; the SoftFloat flags are sticky in the loop.

The supported direction is to keep numerical precision/cost as an explicit
workload-sensitive component choice: this sample demonstrates both a substantial
cost difference and a concrete information-loss case. It supplies no reason to
mandate extended precision everywhere, no proof that binary64 satisfies all
required x87 behavior, and no ranking of FEX versus TCG or Box64. Required numeric
behavior and how often these paths execute remain separate inputs to selection.

### Consolidated branch/FP/DIV retained-result audit — 2026-09-21

**Verdict: ACCEPT the scoped exploratory result and performance consequence;
no generator selection or timing qualification.** Reviewer `decision_audit`
used the remaining 5 agent-minutes of its 20-minute oracle allowance. Together
with the preceding numerical review, this charges 26 agent-minutes against the
shared 30-minute review cap. This reviewer authored `check_comparison.py`; it
therefore does not independently approve that checker. The root reviewer
separately inspected its input models, preservation rules and recurrences before
execution, and reviewed the linked helper implementation and compiled adapters.
This follow-up independently audits retained output identities, guest-result
projections and statistics without importing the checker or producer.

For `/private/tmp/natemu-track03-comparison-raw/comparison-execution-01`, all 114
retained SHA256 entries match. Both native executions returned successfully with
168 admission and 294 timing samples. Independent schedule reconstruction gives
exact case/variant/length/order coverage and matches teardown API accounting.
Direct decoding of all 462 output records independently reproduces the integer
results, branch counts and final path, total completed bodies, final guest RIP,
vector parity and finite FP values/high-lane sentinels. FPCR remains 6 and FPSR
remains zero. These are projections checked independently of the full-state
checker; the root-reviewed checker supplies the broader preserved-state checks.

All 42 short/long medians independently reproduce the manifest. At the retained
24 MHz counter frequency, the long-batch medians convert as follows (ns per
completed fixture, including its declared wrapper/adapters):

| Fixture | Box64 | FEX | TCG |
| --- | ---: | ---: | ---: |
| Empty body/control | 0.925382 | 0.927130 | 0.936190 |
| Zero branch | 1.719316 | 0.659307 | 2.232393 |
| Carry branch | 1.096249 | 0.731786 | 1.922925 |
| Scalar FP add/sub | 2.953688 | 0.956535 | 37.587802 |
| Scalar FP multiply/divide | 4.782359 | 3.172239 | 42.527199 |
| Scalar FP square root | 2.944787 | 2.951463 | 23.304303 |
| Integer divide | 1.382987 | 0.494639 | 1.683235 |

Long timing clock ratios span 1.000000–1.013049. Short correctness admission
ratios reach 1.409091, so admission timing sanity fails while correctness passes;
that failure must remain visible. Fourfold-length median total-time ratios for
the six substantive fixtures span approximately 3.77–4.32, with individual
outliers retained. Some substantive bodies run faster than the empty control,
which reinforces that NOP subtraction and additive isolated-instruction latency
claims are invalid here. No confidence intervals, scheduling control, independent
runtime repetition, or qualified clock evidence were added.

The supported performance consequence is material: the inherited TCG scalar FP
helper path is substantially more expensive on these exact normal inputs than
the retained Box64 and FEX native FP leaves. Its actual helper calls and state
work are included; it was not replaced with handwritten native FP. FEX also has
lower measured medians for these branch and restricted divide fixtures. Square
root gives almost identical Box64/FEX medians, so this sample does not support a
useful ordering between those two there. These observations justify treating
component cost and adapter shape as real selection evidence rather than awarding
TCG a provenance preference.

The tested branch-zero stream alternates outcomes, while branch-carry takes only
the first iteration for one variant; it is not a general branch distribution.
DIV always has a zero high dividend and a nonzero divisor. FP arguments are
finite and exact, with default rounding and AFP controls; NaNs, exceptions,
subnormals and other rounding modes were not exercised. Translation, dispatch,
helper cold paths, complete recovery and full guest workloads remain outside the
measurement. The result supports a cheaper native normal path with required
cold semantics supplied separately; it does not establish that any candidate
already implements the complete product contract or wins overall.

### September21 — consolidated comparison direction and finite selection boundary

`/root/direction_review`; independent of the consolidated VM producer/oracle,
but author of the TCG helper binding, earlier ordering-profile preparation and
compile instrumentation. This is a direction review, not independent acceptance
of those authored artifacts. Inspected the current workflow, Track03 charter,
INV03-01/02 summaries and inventory, retained `comparison-execution-01` manifest,
case bindings and guest oracle, the prior boundary verdict, and the independently
reviewed `numerical-02` account. No new experiment or download.

**Evidence validity:** the latest manifest records168 admission and294 longer
correctness passes. Long clock sanity passes (ratio1..1.01305); admission timing
sanity does not. This remains exploration, with no fresh decision-evidence run
or independent timing replication. The scalar-FP oracle uses exactly representable
normal values and exact results; it does not qualify exceptional operands,
rounding/status, or the complete SSE contract. Some branch length ratios are far
from4, so the clock-sanity boolean does not establish precision of their timing
estimates. Actual linked QEMU helper execution removes the prior unbound-call
uncertainty for these paths; it does not qualify exceptional recovery. The x87
kernel account separately demonstrates concrete binary64 information loss and
large sampled extended-path cost, under its Linux-worker context; it cannot rank
whole generators or set a universal accuracy tolerance.

**Decision relevance:** this is meaningful comparative progress. The ordinary
live-state, branch and native scalar-FP results motivate FEX as the default
component for the resident-state design. Neither the older TCG/Bochs preference
nor unconditional full80 fidelity follows from the evidence. Numerical machinery
is a separate choice; selection of a generator need not settle every optional
numerical profile at the same time.

The dense canonical/clobber/reload reversal is a real sensitivity result, but
claim19 removes universal canonical materialization as an architectural premise.
It must not become an unavoidable cost charged to FEX. Similarly, absence of
measured whole-workload distributions is not by itself a reason to keep component
selection open indefinitely. A design choice can be an explicit inference under
stated intended-use assumptions; it need not assert universal dominance or prove
the correctness of unwritten emitter code.

A finite route to component acceptance has three parts, assessed together:

1. Establish by reviewed pinned-source/interface reasoning that the selected
   emitter can preserve live state while providing the per-instruction recovery,
   bounded control flow and system-mode interception required by claim19. Name
   any unavoidable hot-path additions. A requirement that the future compiler
   export metadata is a design contract; a source restriction that prevents that
   metadata or requires hot rollback is a substantive counterexample. Actual
   implementation conformance remains planning/validation work once the design
   is shown constructible. The concurrently assigned candidate-binding review
   addresses this question; do not create another generic field audit.
2. Obtain one fresh, independently reviewed finite comparison and independent
   replication of its load-bearing performance observations. Freeze the current
   combined ordinary/boundary/branch/helper scope, measurement controls and
   exclusions before execution. Qualify its clock/uncertainty claims; do not
   promote exploratory files or retry until a ranking appears. If numerical
   fast-path repairs required by the selected SSE contract would change measured
   costs, include their bounded accounting or leave that performance premise
   unaccepted. This does not require full ISA implementation.
3. Record the component preference as an inference for resident cached execution,
   with explicit falsifiers: an unavoidable hot spill/guard/recovery cost that
   erases the advantage, insufficient translation reuse, or a required semantic
   repair that changes the ordering. Compile timings currently have unmatched
   cache/diagnostic contexts; they supply no numeric cold-compile ranking or
   crossover. Keep that term explicit rather than assigning arbitrary weights.
   Unknown distributions do not invalidate a conditional design preference;
   an identified required regime in which the alternative wins does require
   reconsideration. A cold/JIT-heavy performance promise needs its own support.

These parts constitute one decision package, not a queue of per-cell campaigns.
Cap any proposed completion package once, including source corrections, fresh
execution, independent replication and findings review; stop with acceptance of
its exact component claim or named substantive insufficiency. No new execution
is authorized by this review. A FEX default may therefore become a justified
architectural selection without a whole-emulator speedup claim. Today its
performance support is exploratory and its recovery binding is under review,
so the present verdict is a preferred hypothesis, not accepted selection.

Track03 closure additionally requires acceptance of the selected recovery
composition and the charter's combined breadth check. Missing implementation
coverage is a later validation obligation; an unresolved architectural
contradiction is not. This distinction gives the investigation a finite endpoint
without weakening its behavior requirements or requiring an endless workload
survey. Charged8 agent-minutes within the allocated12-minute direction review.

**Concrete proposed binding clarification:** a FEX normal generator with a
permanent checked semantic service for REP/partial effects, privileged/startup32
operations and unsupported FP cases is a coherent component proposal, conditional
on the concurrent binding review. TCG frontend/helper semantics may supply that
service independently of generator choice. The required numerical boundary must
be explicit: masking SSE exceptions does not remove MXCSR status/rounding or
DAZ/FTZ and special-value obligations. Even normal inexact arithmetic can require
status updates. Native lowering must preserve the selected SSE contract, or its
admission/repair/service cost must be included; the owner's x87 accuracy tradeoff
does not silently relax SSE. The latest exact normal-result fixtures establish
neither that broader numerical contract nor its repair cost. Root's proposed
single reviewed fresh primary plus single independent replica can consume the
remaining consolidated allowance after binding acceptance, without restarting
the clock or expanding fixtures. Failure to finish yields the exact remaining
insufficiency, not another automatic campaign.

### September21 — final source correction and campaign stopping verdict

`/root/direction_review`, four additional agent-minutes, completing the12-minute
direction allocation. Read the new claim19 adaptation and numerical/before-image
addendum in `consolidated-bindings.md`; directly checked retained QEMU
`fpu/softfloat.c:231`, `include/fpu/softfloat-types.h`, FEX
`OpcodeDispatcher/Vector.cpp:GetMXCSR/RestoreMXCSRState`, helper initialization
bytes, and existing INV03-02 claim28. No execution or new producer change.

**Evidence:** accept the two factual corrections. QEMU's `can_use_fpu` requires
sticky `float_flag_inexact` (0x10) and nearest-even rounding, unless hardfloat is
disabled entirely. The retained environment begins with zero exception flags,
and these exact normal operations never set inexact. Their repeated execution
therefore stays on the software path. The observed roughly37/42ns add-sub/mul-div
body costs are costs of this initialized state and path; they are not general
QEMU helper costs. This correction does not itself measure the alternative path.
FEX GetMXCSR and RestoreMXCSRState explicitly mask with0xFFC0, discarding sticky
status bits. Exact normal-result PASS does not test that mismatch. Neither source
fact establishes Box64 conformance or a TCG performance preference.

**Existing bounded architectural resolution:** claim28 already selects an
allocation-visible value-root/native-site contract for FEX MOV recovery; claim19
provides queued cold completion, and the new adaptation notes identify a bounded
semantic-service route for REP. There is no need to rediscover these interfaces
or require a completed system translator. However, claim28 explicitly does not
bound added spills, and its safe lifetime construction can itself add avoidable
cost. The latest FP status/before-image requirement extends beyond the accepted
MOV case. Cold native-frame retention cannot resurrect an overwritten value.
These records resolve how an adapter could be constructed, not the remaining
matched cost of the required FP/recovery paths.

**Direction verdict:** accept closing this consolidated exploration now with
explicit selection insufficiency. Withdraw the earlier suggestion that simply
repeating the frozen exact fixtures as a reviewed primary and replica would
complete the decision package: those runs cannot resolve the now-identified
FP status and recovery-liveness burden, or QEMU's unmeasured alternative FP path.
FEX remains a concrete performance-led candidate; no generator is selected by
source familiarity, full80 fidelity, or the unequal numerical paths just tested.
The next action is an owner-visible choice about the intended semantic/service
scope and an additional total resource allowance if that concrete comparison is
worth pursuing. Show the existing contract, missing cost and possible selection
consequence together. Do not start another automatic fixture campaign, restart
the current clock, or reinterpret x87 accuracy priority as permission to drop SSE
status. The current scoped results and architectural contracts remain useful;
this stopping verdict does not imply a technical impossibility or a project kill.

### September21 — combined architecture and completion breadth, integrated package

`/root/direction_review`, independent combined-design review. Inspected current
INV03-01/02 summaries and twelve-question inventory, claim18 composition,
claim19's queued revision, proposed02claim56 native eligibility/root envelope,
accepted02claim57 numerical contract, and the relevant existing state/recovery,
capacity and virtual-CPU contracts. No new experiment or broad source reaudit.
This reviewer authored the earlier helper-build tooling and reviewed claim57;
this section does not independently accept that helper implementation or any
experimental result. The separate method/findings/replication reviews own those.

**Evidence validity:** the accepted queued protocol distinguishes active memory
authority, retained references and admitted-route completion obligations. A
PAUSED native frame releases only detachable active authority. A conflicting
remap cannot commit until the recorded admitted-route obligation completes or
is permissibly cancelled. Code/metadata/stack pins remain until their last
consumer disappears. Reconstructing only from canonical slots would violate
this design, but claim56 explicitly preserves current register/spill/constant
bindings and before-images. Its35 current roots are the fields the admitted
native domain may change, not the whole guest state. Upper vectors, control,
segment, x87 and other unchanged state retain their authoritative backing owner.

No inspected new condition requires universal canonical publication or pending
polling at every ordinary instruction. Bounded internal cycles and checked
external entries permit closure before mutation; precise debug, single-step,
synchronous faults and control transitions keep their special boundaries.
A management pause does not retire an instruction or freeze guest time. Retained
code identity prevents stale following instructions after cold completion. These
are coherent interactions with the accepted memory/event/lifetime owners,
conditional on the actual lowering obeying the selected contracts.

**One actual architectural gap identified, with a concrete proposed resolution:**
'checked semantic service' alone did not select an execution engine for the large
native-declined domain. Many QEMU frontend semantics are generated sequences,
not directly callable resident helpers. Root's proposed hybrid resolves that
choice: adapted host QEMU frontend/TCG AArch64 generation produces checked service
objects for captured instruction/context, with resident system/numerical helpers
and the same state/access/effect/lifetime owners. No FEX-to-TCG IR translation or
upstream CPU-loop ownership is required. REP retains admitted decode and partial
progress through bounded service steps; required startup32/system/AVX behavior
is routed there rather than removed from the guest profile. Full implementation
of those bindings is later conformance work, not a prerequisite to selecting this
specific engine and interface.

The following details must be explicit in that source-reviewed service proposal:

- Compilation/miss waits occur outside EXCLUSIVE with no active RAM grant.
  Native admission already reserves executable completion paths and storage for
  every potentially queued suffix. Drain completion cannot discover a dependency
  on a host compilation that it must wait for. REP/service instructions pin the
  code/helper/phase continuations needed to finish admitted effects before entry.
- Service transitions materialize the actual current state once, and return all
  modified fields/context to their authoritative bindings. A35-root ordinary
  map is not permission to lose a service's upper-vector, control, status or
  segment changes. A completed memory effect is never reconstructed by replay.
- Service objects and continuations join claim14's reference census and finite
  object/storage bounds. Both compiler providers share the finite admitted-job
  bound. A second generator does not create an uncounted allocation/pin class.

**Decision relevance and finite completion requirements:** a conditional FEX
ordinary-generator default with this TCG service engine can close architecture
selection without proving whole-emulator speedup or implementing the full ISA.
It still needs accepted comparative justification for that preference and an
explicit numerical-provider/policy disposition. Neither hypothetical universal
materialization nor source familiarity establishes the winner. Dual compiler
maintenance/footprint, state bridges, service frequency and compile misses are
real costs and preference falsifiers. A finite performance claim remains scoped
to its independently reviewed and replicated experiment; the architecture choice
is a separately identified inference under its stated intended-use assumptions.

Complete the service proposal and claim56 source review, settle the x87 provider/
policy question without silently relaxing SSE, then assess that one finite
comparison and update current composition/inventory. Claim18's original TCG/Bochs
binding is historical and needs a current recomposition paragraph linking the
new choices. Clarify claim56's one-access limit as applying to each admitted
**guest instruction's lowering**, not separately to each AArch64 instruction.
No additional generic correctness survey, native Intel acquisition, M2 gate,
stock-boot prototype or full-ISA implementation is required by this breadth pass.

Product applicability remains the selected virtual CPU/machine and named M4
platform. Discovery and service coverage must agree; reduced native eligibility
cannot reduce exposed features. The retained exact-Intel semantic uncertainties
remain scoped compatibility questions, not evidence for an Intel-machine gate.
The actual loader, complete field/site bindings, emitted guards, service handlers,
resource admission and combined guest tests remain implementation-validation
obligations. An incompatible interface or new wait/reference cycle would reopen
architecture; missing unwritten code alone does not. No unconditional Track03
completion verdict is given before the named selection/service/policy items close.

**Claim58 composition return — accepted conditionally, preference still pending.**
Read the actual appended02claim58 and the corrected56 native eligibility wording.
The proposed service engine is now concrete: the existing QEMU system frontend
and TCG AArch64 backend produce resident service objects, with the previously
specified access/state/exception adapters. Service generation happens outside
active grants/EXCLUSIVE; completion code and storage are reserved before effects.
Pre-effect handoff releases the FEX execution/return reference, queued FEX suffix
completion uses permanent handlers, and TCG continuations stay in the admitted
allocation or permanent code. Both producers share the existing finite job slots
and reference census. Full modified state/context is returned to its owner.
These changes address the named execution-engine, drain-wait and capacity returns
without introducing an IR bridge, second guest owner or nested service-object
stack. Claim56 now restricts each guest instruction's native lowering and sends
other mode contexts to the service rather than dropping them from exposure.

**Evidence validity:** accept this conditional architectural composition against
claims14/19 and the existing state/memory/event contracts. The proposal supplies
an engine and finite ownership/control-flow construction; it does not claim that
all opcode handlers, adapter fields or generated phase maps are implemented.
No new remap-obligation, stale-code, clock/event or teardown contradiction remains
from this review. An actual lowering that cannot provide the required detachable
phase/bounded completion is an implementation-discovered counterexample that
reopens its admission, not permission to violate the contract.

**Decision relevance:** accepted as a concrete candidate architecture. Generator
preference still needs the separately reviewed finite comparative evidence and
stated cost assumptions; the service's cost, dual compiler maintenance and
low-reuse cases remain explicit falsifiers. This review does not accept a speed
ranking or x87 policy. Once those decisions close, updating claim18's current
composition to these accepted bindings and carrying their applicability into the
twelve-question inventory can complete this breadth return. No additional
architecture experiment or generic source pass is requested. Actual complete
emitter/service conformance remains implementation validation.

Total charge16 agent-minutes, including this return, at the allocation limit.

### September21 — integrated finite comparison preexecution review

`/root/comparison_sources`, independent of the method/producer author and oracle
and helper-blob authors. Reviewed the current “Integrated package: reviewed
finite comparison method”, `comparison.py`, COMPARISON/GUARDED_FP paths in
`execution.c`, `check_comparison.py`, and retained `tcg-helpers-03` producing
code/linked layout. This review does not independently accept my claim56 or
my earlier source-binding propositions; root owns their review. It assesses
this finite integration and inference before the new primary/replica execute.

**Accept for the scoped primary and independently rebuilt replica, with the
observation scope stated precisely.** The producer uses actual frozen emitted
words, declared edge/helper relocations, full resident status inputs and timed
FP pre/post checks with staged destination/rollback. The scalar six-instruction
guard preserves NZCV; FP predicates save it in declared scratch. FEX rollback
restores the retained low vector, Box64 stages results in d24 before INS/store,
and negative controls return a registered contained rejection. These negatives
exercise rejection before the first tested instruction commits, not every
second-instruction prefix or completed semantic fallback.

Actual helper calls use the retained QEMU implementations and closed link blob,
not handwritten arithmetic replacements. Verified six helper artifact hashes
against sealed SHA256SUMS and exact ELF-to-raw-blob bytes; the10556-byte blob
occupies0x40008000..0x4000a93b and contains code/read-only data with explicit
contained exception/assertion stubs. QEMU's can_use_fpu requires sticky inexact
and nearest rounding; those controls are present. The finite positive operands
exercise its native path. All code, tables,32KiB state and stack regions fit the
recorded256KiB layout, and helper calls preserve the x29 loop count through the
ordinary ABI. The source checker admits all21 combinations and the executable
self-check passes2800 semantic cases,144 guarded cases and3960 rejected mutants.

The frozen-source rebuild verifies every primary source file and its manifest,
requires byte-identical driver code, and rebuilds without importing newer emitter
artifacts. Correctness admission precedes timing; counter frequencies, register
controls, outcome identities and teardown are checked. Seven randomized paired
batches at two lengths and a separately rebuilt run support the method's finite
relative-cost inference, provided the declared clock and stability conditions
hold. They do not support NOP subtraction, universal cost ratios, service
frequency estimates, recovery conformance or a whole-generator ranking.

One wording correction was sent to the author: the current producer/checker
observes the complete32KiB state and a512-byte DATA window, not all4096 input
DATA bytes. The finite bodies access that window, but the method must name it
rather than claim full guest-memory observation. This scope correction does
not require a new experiment. No other blocking integration defect found in
this bounded review. Eight review agent-minutes are charged; the remaining12
of the assigned20 cover the independently rebuilt replica and result review.

### September21 — independent frozen-source replica and finite-cost acceptance

`/root/comparison_sources` executed the approved replica from
`comparison-guarded-primary-01` using the byte-identical driver and
`--guarded-fp --purpose decision-evidence --rebuild-from ... --execute`.
Replica: `/private/tmp/natemu-track03-comparison-raw/comparison-guarded-replica-01`.
This is the one independently rebuilt replica authorized by the reviewed method;
no rerun, new fixture or emitter acquisition occurred. Replica work began00:49:27UTC.
Three review agent-minutes are charged for rebuilding, execution and results
review, in addition to the eight preexecution minutes:11 of the20 allocated.
The source-author work on claim56 is separately accounted, not accepted here.

**Evidence validity: accept the reviewed finite comparison.** Both primary and
replica completed204 correctness-admission and294 timing samples with all
outcomes passing. Every one of150 retained file hashes per run was verified;
the121-file source sets are byte-identical. The replica independently compiles,
signs/verifies and executes its new binary; all commands terminate successfully.
Both builds report43324 bytes of guest code in the same bounded layout and
41185 accounted API calls for each294-sample timing phase. The observed state is
the complete32KiB resident state and512-byte data window specified by the corrected
method, plus the native/stack observation contract. These checks do not imply
unobserved memory or full-ISA conformance.

Both runs report24MHz host/guest counter frequency and the same admitted TSO/AFP/
FPCR controls. Long-phase host-clock ratios are1.000–1.001832 in primary and
1.000–1.001984 in replica. Short admission timing fails its aggregate sanity
criterion and is excluded from timing inference; its correctness remains valid.
All294 timing samples in each run were retained. Medians below were independently
recomputed from raw counter differences, without NOP subtraction or exclusions.
Ranges contain the four medians (two lengths, two independently built runs), in
nominal ns per body at the observed frequency.

| Fixture | Box64 | FEX | TCG |
| --- | ---: | ---: | ---: |
| branch_zero |1.459–1.752|0.624–0.660|2.194–2.252|
| branch_carry |1.010–1.085|0.695–0.745|1.793–1.945|
| guarded add/sub |2.243–2.473|1.239–1.354|6.221–6.290|
| guarded mul/div |4.037–4.260|3.143–3.299|7.732–7.997|
| guarded SQRT |2.916–3.097|2.948–3.087|1.546–1.609|
| restricted unsigned DIV |1.346–1.471|0.481–0.495|1.612–1.693|

**Decision relevance:** FEX's lower finite cost for both branches, guarded FP
add/sub and mul/div, and restricted integer DIV holds at both lengths in both
runs. For those comparisons, even the observed individual-sample ranges do not
overlap the competing candidates' ranges. TCG's SQRT advantage is likewise
separated. FEX versus Box64 SQRT overlaps and changes order; no winner is assigned
there. The finite native guard/before-image costs therefore do not erase all of
FEX's earlier advantage, and actual helper lowering can win a particular
operation. This supports a conditional, mixed component direction instead of
an assumed all-operation winner.

These are costs of the retained dependency patterns, controls, native code,
adapters and actual linked helpers. They do not price the frequency of native
eligibility, cold semantic fallback, queued recovery, startup32, compilation,
SMP or realistic block distributions. Negative cases prove the specified
first-instruction guard rejection controls, not completed cold service or all
FP exception paths. Claim56's implementation obligations remain with its source/
architecture review. No whole-emulator speed ratio or general FEX conformance is
accepted by this finite-cost result. The primary/replica experiment is complete;
unused review allowance does not authorize another run.

### Lazy x87 C1 source-construction review — 2026-09-21

**Verdict: conditional ACCEPT of the supplied design's source-level
constructibility, not runtime acceptance or a performance conclusion.**
Independent reviewer `decision_audit`; allocation capped at 10 agent-minutes from
the integrated package's reserve. This review examines the root author's stated
claim59 proposal and retained primary sources. Final claim text was not yet
present when these findings were prepared, so this is not approval of unseen
wording. The following premises are load-bearing.

Intel089 Vol1 §8.1.3.2 and Table8-1 distinguish instruction-written C1 from the
sticky numeric exception flags (§8.1.3.3). A single pending C1 producer is
therefore sufficient if each following instruction explicitly preserves,
replaces, clears or defines that bit. There is no growing arithmetic history:
the record copies the actual original ext80 operand value(s), instruction-specific
provider ID and original FCW. Two 16-byte operand slots plus control/identity
metadata fit the proposed 64-byte cap. A later numerical instruction captures
its actual architectural operands, even when those are earlier approximate
results; it must not reconstruct a hypothetical all-extended computation chain.
The immutable value record requires no old code, guest RAM or retirement pin.

Retained Bochs `softfloat.h` exposes `RAISE_SW_C1` and rounding-up set/clear
operations; its extended packer clears earlier-stage hints when the final
rounding decision becomes authoritative. A cold replay can therefore obtain the
same C1 that the previously selected status-capable primitive would produce for
the captured original operands/control. Replay needs fresh local status, the
exact instruction-specific primitive and captured FCW. Merge **only C1** into
current state; discard numerical results and other replay flags, and do not
repeat stack actions, guest memory accesses, pending-exception delivery or
instruction metadata updates. FSIN/FCOS must not substitute for FSINCOS: the
retained Bochs trig source explicitly distinguishes these computations and their
final-rounding status. Existing uncertainty about that provider's exact silicon
transcendental behavior is inherited, not resolved by deferral.

The approximate committed value and deferred C1 deliberately refer to different
numerical calculations: the value uses the owner-permitted approximation, while
C1 comes from the original80 operands under captured FCW. This must be explicit;
it is not C1 for the rounded binary64 candidate. The proposal does not need a
status waiver if the agreed value-only relaxation permits this separation. It
must not silently reinterpret that relaxation as permission to discard C1.

For canonical normal ext80 inputs that convert to finite normal binary64,
ordinary add/subtract/multiply/divide results lie far inside the extended
exponent range. Even cancellation is bounded by the finite 64-bit input
significands; there is no route from these operands to extended underflow merely
because the exact difference is small. Nonzero normal divisors exclude divide
by zero; normal operand classification excludes denormal/unsupported/NaN cases.
With all exceptions masked and PE already sticky, a newly inexact arithmetic
result adds no numeric exception bit. The native candidate still needs its
stated post-classification and staged commit. For actual80
`2^-100 <= abs(x) <= 1`, sin/tan are nonzero and safely normal, cos is bounded
away from zero, and the input is inside the x87 C2 range. The inherited QEMU
helpers provide the existing double/libm numerical routes. This establishes a
narrow status-domain argument, not broad numeric conformance or measured speed.
Stack admission, existing pending-exception checks, TOP changes, FIP/FDP/FOP and
trig C2 clearing remain owner operations. Thus “other status unchanged” can refer
to the numeric exception bits, not every bit of FSW.

All status observers must materialize first, including **FSTENV/FNSTENV** as
well as FSTSW/FNSTSW, FXSAVE/XSAVE, FSAVE/FNSAVE and canonical host exports.
Intel's retained environment description explicitly includes FSW. Restore paths
(FLDENV/FRSTOR/FXRSTOR/XRSTOR), reset paths and known C1 writers replace/invalidate
the descriptor at their architectural commit. Per-instruction undefined C1
behavior must also have a deliberate policy; blanket preservation is not a
substitute for this classification. A save attempt that faults must not perform
its completion/reset actions. Pure C1 materialization before such a fault is
semantically harmless, provided the old architectural value remains available.
Control changes do not alter the captured replay control, and exception/fault
handlers may observe the record only through the same materialization boundary.

Acceptance requires these observer and transition obligations in the final
proposal, a permanent bounded replay handler without compilation/queue
self-dependency, and ordinary resource accounting for the fixed record. Neither
the hot descriptor-write cost nor the cold observation frequency is measured.
This design removes a need to calculate full extended arithmetic eagerly solely
to produce an otherwise unobserved C1; it does not demonstrate that deferral wins
for workloads that observe status after every operation.

Primary sources inspected locally: Intel combined SDM089 extraction at
`/private/tmp/natemu-track03-compound-01/intel089.txt`; pinned QEMU
`target/i386/tcg/fpu_helper.c` at
`/private/tmp/natemu-translator-audit/qemu`; and pinned Bochs status/rounding/trig
files retained in `/private/tmp/natemu-track03-fp49` and
`/private/tmp/natemu-track03-fp48`. No new download, experiment or VM run.

**Final-text review:** the subsequently appended CLM-03-02-59 was inspected
within the same allowance. It incorporates the required observer list,
transactional restore/save behavior, instruction-specific C1 disposition with
checked fallback for missing entries, captured-control/fresh-status replay,
original80 C1 versus approximate value distinction, numeric-exception-only
invariance, stack/pending-exception ownership, bounded lifetime and unmeasured
costs. **Final verdict: ACCEPT claim59 as the scoped source construction stated.**
Implementation conformance and performance remain unproved; the earlier
qualification about unseen final wording is now discharged. Total review charge:
10 agent-minutes, no additional research or experiment.

### September21 — claim20 selection and final charter breadth review

`/root/comparison_sources`, independent of claim20's author. Reviewed claim20,
its current claim18 recomposition, the twelve-question inventory and Track03
completion criteria. For claim56 I rely on root's recorded independent review,
not my own authorship; claims57/58 and59 rely on their separate accepted source/
composition reviews. The finite matched primary/replica evidence was independently
reviewed and replicated above. No new source acquisition or experiment.

**Evidence validity: ACCEPT the corrected claim20 inference at its stated
scope.** The preference is supported by repeated matched advantages across
several ordinary shapes with actual guards, staged FP/status checks and linked
competing helpers. It is not based merely on FEX being constructible, source
familiarity, or the availability of fallback. The accepted recovery construction
removes the false premise that every normal boundary must publish a dense
canonical image; it still charges real root preservation and service transitions.
The concrete TCG service engine preserves exposed behavior outside native
eligibility, rather than renaming an unimplemented generic library as coverage.

One material correction was requested and verified in the final claim20 text:
TCG's measured SQRT advantage applies within its tested state/entry binding.
It cannot justify an unconditional FEX-to-TCG handoff, whose materialization,
admission and reload can cost more than the~1.4ns body gain. The corrected text
retains TCG SQRT in the service binding, permits the native path and makes any
cross-producer choice include its bridge costs. No substantive selection return
remains from this review.

**Decision relevance: ACCEPT questions2/5 and the recomposed architecture as a
conditional implementation direction.** FEX ordinary execution plus concrete
TCG system/compound service is a justified place to invest implementation effort;
it is not a demonstrated optimum for an unspecified workload. Claim20 explicitly
states the gain-versus-adaptation/service/reuse condition and small finite branch
saving thresholds. Native shapes that fail admission earn no assumed saving.
Low-reuse, service-heavy or status-observing workloads can favor another path.
These are retained economic/implementation falsifiers for synthesis, not hidden
claims of workload-wide dominance. No weighted application speedup or uniform
FP winner is inferred.

The lazy-C1 x87 policy is accepted through the separate claim59 source review and
the owner's explicit accuracy-versus-cost direction. It retains instruction-
specific status/stack/exception ownership while allowing the stated approximate
committed values. The original-operand C1 versus approximate-result distinction,
loss of cancellation residuals and strict-provider option remain disclosed.
The earlier primitive measurements motivate the policy but do not measure its
conversions, guards, descriptor writes, cold resolution or application tolerance.
No claim that the complete lazy-C1 adapter is faster is accepted here; synthesis
must preserve those uncertainty/cost terms and the numerical applicability limit.

### Charter breadth disposition

| Breadth | Review outcome |
| --- | --- |
| Product | The selected virtual CPU/machine, required startup32, kernel/userspace, SMP, JIT/source mutation and required devices remain covered by their accepted owners. Restricting native eligibility does not remove guest instructions; checked service supplies the other paths. x87 approximation is an explicit owner-directed numerical policy, not a waiver of SSE status or arbitrary guest behavior. |
| Architecture | The current selection binds questions2/5 to the accepted execution/state, memory, ordering, lifetime, event/time, boot/device and containment contracts of questions1,3,4,6–12. Claim18's ten interaction rows are recomposed through56–59 and claim19. Both producers share one resident authority, finite job/reference census and nonnesting service; reserved completion machinery prevents a drain-time compilation dependency. No new unowned execution engine or wait/reference cycle remains in the reviewed construction. |
| Correctness | Current-root/phase maps, staged effects, admitted-route completion, exact event ownership and full changed-state handoff have concrete conditional contracts. Actual field/site mappings, emitted guards and complete service implementations still require conformance validation. Those are unwritten-code obligations, not permission to assume native frames recover lost values. Retained exact-Intel uncertainties and platform premises retain their stated compatibility/applicability scopes and reopen their owners if a concrete target dependence arises; they do not require a Haswell machine by default. |
| Methodology | The scoped comparative premise uses actual code and independent controls/rebuild, distinguishes primary evidence from architecture inference and standalone numerical costs, and records alternatives, costs and falsifiers. No NOP subtraction, unsupported native coverage, unmatched compilation ranking or repeated microcampaign is used to manufacture selection. |

**Final Track03 judgment:** the coherent architecture and implementation direction
can close under the charter's explicitly conditional architectural scope. No
remaining issue identified in this review invalidates the selected construction
or the limited comparative reason for the preference. This does not close later
implementation conformance or economic/workload acceptance, prove end-to-end
performance, or authorize implementation. Missing workload weights must remain
missing, not be invented to turn the direction into a universal winner.

Before publishing completion, synchronize the current summaries, twelve-question
inventory and claim18 supersession labels: their old “no selected generator” and
“questions2/5 open” entries are now stale. Preserve historical insufficiency
records with their original scope. This is editorial reconciliation of the
accepted result, not another research gate. Five decision-review agent-minutes
are charged within the remaining nine; total independent method/replica/decision
review charge is16 of20. No further experiment is recommended by this verdict.

## ABI draft 0 — independent design/method review, September 28, 2026

**Reviewer/context:** `/root/abi_review`, independent of root's specification
authorship and `/root/abi_contract_audit`'s supporting extraction. Inspected the
workflow, Track03 milestone, inherited Track01 scope, INV03-01 current summary
and inventory, claims14/15/19, and the complete
[ARM64 execution ABI draft](../specs/arm64-execution-abi.md). This is a source/design
review; no code execution, replication or historical archive search was performed.

**Decision and alternatives:** the host-translator/captive-VM architecture and
ABI-first sequence are fixed. The decision here is whether the readable contract,
technical interface and finite validation proposal are concrete enough to present
to the owner and guide a subsequent bounded runtime assignment. Live roots and
resident gateways are compared with universal publication, unrestricted native
calls and a generic interface; the draft discloses their costs without selecting
FEX/TCG or claiming runtime correctness or speedup.

**One consolidated return:** two defects threatened the promised live-state
contract. First, installing a destination entry map without moving values could
silently reinterpret registers or release their old spill frame. The minimum
remedy was an explicit bounded map conversion preserving copy-cycle inputs and
both maps/references until handover. Second, committing a helper result before
restoring its old saved destination could erase that result. The minimum remedy
was outcome-specific restoration and output installation, retaining staged values
and phase metadata until the post-call map becomes authoritative. Closure required
matching adverse controls inside existing T2, not new experiments or test cells.
An editorial clarification identified SP_EL0 as the interrupted stack after
hardware selects SP_EL1 on exception entry.

**Targeted recheck: closed.** Section4 now explicitly reconciles entry maps,
rejects unsupported conversions before transfer and retains map/frame references.
Its gateway restores non-output roots before installing permitted outputs; failed
outputs preserve their prior authority, and staged results survive intermediate
capture. T2 now includes register swaps/vector-spill movement and output/register
overlap. The exception-stack wording is corrected. No further substantive return.

**Evidence validity: ACCEPT as a provisional design and method proposal.** The
draft consistently distinguishes current roots from backing slots, native stops
from guest boundaries, retry from suffix completion, and inactive frames from
reclaimable storage. It preserves admitted-route completion before remap, cold
dispatch after source mutation, bounded queue fairness and forced-exit ownership.
The T1–T11 mapping covers the declared milestone behaviors and negative cases.
The finite fragment test uses actual producer bytes or reports acquisition failure;
hand-derived maps and an inserted helper explicitly do not prove an automated
exporter or upstream helper integration. The 27-cell bundle, independent oracle,
fresh primary/replica, retention and failure rules are internally consistent.
Exact code, inputs, schedules and barrier bindings still require their declared
pre-execution review; this verdict is not that approval.

**Decision relevance: ACCEPT delivery of this self-contained draft.** The proposed
six-working-hour/480-agent-minute runtime ceiling includes all six stages, review,
correction and acquisition, with finite build/run/storage limits and explicit
incomplete outcomes. Its adequacy is unmeasured; exhaustion must produce a scoped
insufficiency, never fixture substitution or a self-renewing review campaign.
Readability and declared limitations support an owner decision without requiring
the owner to audit register maps. Runtime milestone completion, generator choice,
full x86 semantics, stock boot, performance and Track03 closure remain unestablished.

**Bounded disposition:** one consolidated review and one targeted recheck complete.
Charge10 active reviewer minutes, including context reading and this record, within
the15-minute reviewer cap and the assignment's90 aggregate minutes. The proposed
runtime allowance has not started. No new campaign or blocking follow-up is opened;
only synchronize the draft/current-summary review status and deliver the spec.

### Owner-identified paging omission — focused amendment review

**Reviewer/context:** `/root/abi_review`, continuing the same independent draft
review. The owner correctly identified that the initial document and its review
did not make the mandatory x86 paging interface or its experimental coverage
explicit. Inherited memory contracts and ARM remap tests did not make that draft
self-contained on guest virtual-to-physical translation. This return corrects
the earlier acceptance scope; it does not claim the omission was already covered.

Inspected the new readable paging explanation, section2's paging interface,
T12/P1–P8, the amended35-cell method and limits, and INV03-04 claims41/42. No
specification authorship, new architecture selection, execution or replication.
The decision is whether the amendment now specifies and tests the actual guest
paging/runtime boundary, without turning the finite experiment into all-mode
walker implementation.

**Evidence validity: ACCEPT the amendment as proposed design/method.** The
interface distinguishes guest linear address, x86 table walk, GPA/owned physical
route and resident ARM shadow. Context includes architectural permission state
and lifetime generations; access requests preserve intent, privilege, span and
progress. The runtime's committed state controls admission. Miss serialization
extends from first paging read through A/D and access/fault/shadow publication;
read fills cannot bypass first-write D requirements. Internal repair is distinct
from guest #PF/CR2 effects, and ACCESS_COMPLETED applies to one subaccess rather
than erasing an instruction's remaining effects. All writers, CR3/INVLPG/control
changes, admitted old routes and native identity reuse share the stated authority.
Guest fetch permissions do not make x86 RAM native executable. Non-direct routes
remain typed service; hidden PAE state remains architectural rather than a cache.

P1–P8 now require actual guest paging entries to drive the walker and observable
native accesses, table/A-D changes and exception packets. They cover hit versus
miss, write admission, permissions/reserved faults, alias mutation, context reuse,
two-vCPU shootdown, partial/service effects and fragmented large leaves. Manual
ARM remapping expressly cannot satisfy T12. Exact vectors and source/native
maintenance bindings remain required in the actual-code pre-execution submission;
none is being accepted as observed behavior here. Legacy modes and full type
semantics remain inherited obligations with explicitly narrower tested coverage.

**Decision relevance: ACCEPT the corrected self-contained draft for delivery.**
The27 original cells plus eight paging cells total35. Page-pool/run limits and
the proposed six-hour/480-agent-minute ceiling remain finite and internally
consistent. Budget adequacy is unmeasured; the amendment explicitly forbids
omitting paging or silently extending work to make completion fit. No generator,
runtime-conformance, performance or full-emulator conclusion follows.

**Disposition:** no substantive correction or targeted recheck is needed for
this focused return. Charge6 active reviewer minutes within its8-minute allowance;
the runtime budget remains proposed and inactive. Synchronize amendment status
and deliver the corrected contract; no additional review campaign is opened.

### October 26, 2026 — containment watchdog development correction

**Independent reviewer:** `/root/i4v_upstream`; reviewed the root-authored outer
watchdog and host gate integration. This is a development review, not acceptance
of the containment matrix or full I4-V.

The prior watchdog could report `passed` after its gate-relative deadline when a
successful but slow process sampler returned after the child had exited. An inert
reproduction used a 100 ms deadline, child exit at approximately 150 ms and sampler
return at 250 ms; the old loop accepted the late exit. The correction checks the
gate and deadline before `child.poll()` on every iteration, including the final
iteration after sampling. Independent recheck passes the new late-success
negative, the sampler-timeout-at-deadline case and the invalid-future-gate case
(three tests). These checks use inert children and mocked sampling; no VM, build,
compiler or Docker execution was performed by this reviewer. The separate test
using real `ps` could not run in this reviewer's sandbox; the author reports all
four supervisor tests passing with that access.

**Disposition:** the reproduced false-success race is corrected. The host's
withheld-ack cause text also differed from the independent oracle's exact expected
text; that integration correction was reported to both authors and is not claimed
verified here. Complete failure injection, owned-resource cleanup, fresh-control
VM evidence and the remaining I4-V acceptance requirements remain outstanding.

## Host compiler-channel spin comparison — November 7, 2026

**Scope and decision:** retain the current 30-µs host reply-spin budget or reduce
it while holding FEX's 20-µs request spin, runtime and workloads fixed. M4,
macOS26.6.2, SDK26.4, Rust1.97.0; build N4/2 GiB and N10/8 GiB, installer N1
screen. Cache off, no per-request profiler. No new production optimization.

Root authored the constant-only source snapshots, CPU-accounting adapter,
screening runner and analysis. Independent reviewer `/root/review_vector`
inspected those sources, independently repeated the six CPU-accounting controls,
checked all source/binary identities and performed isolated rebuilds and fresh
ABBA repetitions. Root reviewed the reviewer's reproduction scripts before their
execution; builds and VM measurements did not overlap. The
[pre-execution thread](INV-03-01-pre-execution.md) records method corrections and
the normal-CLI amendment.

**Evidence validity: accepted.** All 52 boots are valid, with raw guest markers,
exit status, input/output pins, whole-process wait4 CPU and serial read bounds
audited: 36 screening boots, eight independent repeats and eight additional normal
CLI controls. No failed, skipped, capped, contended or pending cells remain.
Build preflight failures were retained and corrected before VM measurements.
The first 44 boots use a combined CLI/driver build: build workloads use the
test-enabled CLI, while installer boots use the driver. They remain labeled as
such. The last eight build only the CLI, verify that the
testing feature is absent, and reproduce the normal 30-µs CLI byte for byte.
Screening is counterordered, not fully position balanced. Each independent set
uses one ABBA block per build configuration. Read bounds are observer uncertainty,
not confidence intervals or a statistical guarantee.

**Decision relevance: accepted for retaining 30 µs.** All shorter budgets fail
the screen's combined CPU/elapsed criterion. The 30/10-µs normal-CLI control
shows N4 total CPU 34.9856 → 36.1373 seconds and cold wall 5.5300 → 5.8305
seconds. N10 total CPU falls 58.0403 → 56.5173 seconds, but cold wall rises
6.3310 → 6.6875 seconds. This explicit N10 tradeoff prevents a universal CPU-cost
claim. Its CPU direction differs from the test-enabled runs; build configuration
versus run variation was not isolated as the cause. System CPU rises about half
a second in both normal-CLI configurations. Exact wait/wake counts and FEX's
spin contribution remain unmeasured.

The normal-CLI cold differences exceed serial read bounds in both configurations;
their small sample establishes a bounded comparison, not global optimality.
The predeclared criterion still rejects 10 µs. Keep the production constant,
deprioritize fixed-budget shortening, and do not extend into adaptive/server-spin
work without a new hypothesis. The conditional 40-boot candidate confirmation
was not triggered. Total execution was 52 boots within the original maximum of
84; no additional campaign is needed for this decision.

Evidence: [screen audit](../../../../natemu-raw/compiler-spin-20261107-01/independent/screen-review.json),
[independent repetitions](../../../../natemu-raw/compiler-spin-20261107-01/independent/independent-runs-review.json),
[normal-CLI audit](../../../../natemu-raw/compiler-spin-20261107-01/independent/production-runs-review.json),
and [final independent verdict](../../../../natemu-raw/compiler-spin-20261107-01/independent/final-verdict.json).
The [performance record](../../restructure/perf-phase2-plan.md#host-compiler-channel-spin-comparison-november-7)
owns the absolute times, limitations and backlog disposition. The prior capacity
installer difference remains unexplained; this experiment does not attribute it.

## I/O scope and Rust reuse review — November 7, 2026

**Authors/context:** `/root` authored `docs/restructure/io-plan.md` and the
networking additions to `io-rust-reuse.md`; `/root/rust_io_research` authored its
queue/transport/block survey. Independent `/root/io_scope_review` inspected both
in a separate read-only context, following its initial current-code scope review.
The owner selected persistent disk plus basic networking, with full device/network
requirements retained as later coverage. No implementation or execution occurred.

**Evidence validity: accepted for the source-only evaluation direction.** The
review checked current device, RAM-write, IRQ and teardown interfaces and the
accepted device/memory contracts. It independently verified cited upstream paths:
block parsing assumes dedicated header/data/status descriptors; the optional
stdio executor accesses guest memory during I/O, uses fsync and imports a
Linux-only utility; MMIO defaults need notification/lifecycle integration;
libkrun retains worker guest-memory access and declines reset on non-Windows;
vmnet documents entitlement/elevation and macOS26 requirements. The survey
explicitly separates moving/archived source observations from a pinned build.
No macOS build, platform admission, integration or performance result is claimed.

**Decision relevance: accepted, no substantive blocker.** Evaluate rust-vmm
queue reuse and the memory adapter before authoring a new queue implementation;
retain device profiles and assess transport helpers separately. RNG then block
then network is an appropriate sequence. Preserve truthful flush behavior and
teardown from the first writable-disk slice, actual installed-root admission,
and independent concurrency/coherence tests. Basic network success does not
complete the full IPv4/IPv6/ICMP contract. The network backend and a host-library
dependency-policy change remain explicit choices rather than silently adopted.

**Return disposition:** incorporated the review's clarification that IO4 must
establish worker/capability containment before external network exposure. This
makes an existing requirement explicit; it adds no experiment or approval gate.
The bounded planning/source pass stops here. Follow-on execution budgets and
independent implementation acceptance belong to the selected executable slice.

## IO0 queue/transport assessment review — November 7, 2026

**Authors/context:** `/root` authored the isolated probe and
[evaluation](../../restructure/io0-rust-vmm-evaluation.md);
`/root/rust_io_research` authored the
[broader assessment](../../restructure/rust-vmm-assessment.md).
Independent `/root/io_scope_review` reviewed upstream source, the harness and
both sets of claims, then performed one copied-source, fresh-target offline
rebuild. The owner authorized this exploratory queue/transport evaluation;
broader adoption and possible contributions remain assessment only.

**Evidence validity: accepted for exploratory API observations.** The pinned
upstream is clean, original source checksums match, and the replica probe is
identical to the successful author attempt. All 16 cases pass in both runs;
several deliberately reproduce unsuitable upstream behavior. Four total
build/test invocations include the retained author import-path failure.
Source/build retention is about 225 MiB within the 1 GiB bound. The reviewer
sent its preparation script before running, but root inspected it only afterward:
this is exploratory replication, not pre-execution-reviewed decision evidence.
The [retained independent report](/Users/liaozping/orca/natemu-raw/rust-vmm-io0-20261107-01/replica-io-scope-review-01/review.md)
links source identity, logs and outcome. No substantive correction was required.

**Decision relevance: accepted for selective reuse investigation.** Queue
primitives are a plausible reuse point with explicit chain/feature checks,
owned snapshots and prevalidated completion publication. Generic MMIO handling
requires queue-disable, register-validation, feature-policy and IRQ integration.
Suppression behavior is an efficiency/selected-contract concern, not automatically
a mandatory-spec violation. The broader review supports retaining natemu's
execution/coherence engine, HVF ownership and current chipset; no migration payoff
or upstream maintainer acceptance is asserted.

**Limits/return:** private synthetic RAM and callback fixtures do not establish
resident exclusion, concurrent coherence, reset/drain, backend containment or a
security boundary. No VM, real disk/network workload, performance test or
production dependency change occurred. IO0's concrete completion contract remains
open. Findings and review qualifications are incorporated in the evaluation;
no additional execution is needed for the requested assessment.

## Copy-first virtio plan review — November 8, 2026

Owner request: digest the I/O discussion, record notes and plan persistent disk
plus basic networking with native features, current-runtime compatibility and
future direct-payload options. Root consolidated [the plan](../../restructure/io-plan.md),
with independent read-only reviews by `/root/io_scope_review` (runtime/concurrency)
and `/root/rust_io_research` (native backends). Both accept the corrected plan
as an implementation direction, not integrated correctness evidence.

Corrections incorporated: immutable ring snapshots and separate readable-stream
capture include device headers; parsing/host waits stay outside RAM windows;
queue and slot bounds preserve accepted profiles; MMIO lifecycle gains a proposed
completed/deferred/refused result with progress-capable safe recovery and ordered
readback; IRQ publication serializes with ACK; S5 drains before existing teardown.
The spec's QueueReady last-written/readback rule excludes a synthetic busy value.
Native review retains vmnet entitlement/worker admission and callback retirement
as gates, and checksum/GSO header/size constraints as later work. The proposed
vmnet choice does not silently replace the accepted libslirp contract.

No code/dependency changes, builds, VM runs or disk/network operations occurred.
Concrete ABI/participant/budget mapping is the next proposed IO0 assignment;
finite adversarial execution belongs to IO1 after implementation authorization.
The suggested segment-helper discrepancy deferral remains distinct from formal
acceptance or an ISA fix, and is not a new I/O milestone blocker.

## I/O window and checked queue foundation — November 8, 2026

Owner authorized local implementation in natemu and the supplied rust-vmm
checkout. Authors: `/root/io_window_author` (ABI/coordinator/resident),
`/root/rust_io_research` (checked queue and live driver/runner). Independent
reviewer `/root/io_scope_review` inspected the actual source, tests, lifecycle
and retained execution, and performed fresh builds/replays. Root integrated
the work and resolved review feedback without changing the authors' algorithms.

**Evidence validity: accepted within the development scope.** Queue tests pass
56 existing, ten new public-API cases and four doctests in both author and fresh
offline replica. Seven coordinator host tests pass; synthetic acknowledgements
qualify host protocol/error handling only. Retained-source native checks pass at
N=1/2/4, each with 64 designated snapshots while crossing-16/cacheline locked
operations execute, per-vCPU progress, ordered overlap, previously executed code
patch visibility and clean teardown. The reviewer independently rebuilt host and
resident, passed the stack/gateway audit, and repeated N=4 successfully. Its
[retained review](/Users/liaozping/orca/natemu-raw/io1-20261108-01/windows-replica-01/review.md)
links commands, hashes, outputs and limitations. The author runner's region-size
notation was corrected before any VM execution; both versions are retained.

**Decision relevance: accept the runnable foundation for continued virtio
integration.** No unresolved substantive blocker in this scope. Review raised
two corrections: the polling deadline cannot bound native doorbell scheduling,
and a 256-descriptor net chain needs more than 256 spans once ring bookkeeping
is included. Documentation now distinguishes the deadline, and the bound is
512 spans / 256 KiB. Each resident flushes before guest reentry; the host release
does not claim all flushes have finished. Paused residents still consume the
native-run watchdog budget. Author/reviewer source ownership stayed separate.

Queue checks used two of six invocations; live checks used four of 24 launches,
with no retry or VM failure. These are development/exploratory results, not a
performance or architecture acceptance campaign. Deferred MMIO, asynchronous
device ownership, queue incarnation/IRQ/reset/shutdown integration and real
backends remain unimplemented. Native HLT/AP, high RAM, PTE aliases and timeout
interleavings are not established by the live fixture. The next assignment is
the common asynchronous completion path followed by virtio-rng; IO1 and Track03
remain open.


## Virtio-rng Linux integration — November 8, 2026

Authors: `/root/rust_io_research` implemented checked snapshot traversal and the
virtio-MMIO RNG owner; `/root/io_window_author` implemented deferred MMIO and
cancelable RAM-window admission and authored the guest runner; root integrated
Boot/ACPI/CLI and shutdown ordering. Independent `/root/io_scope_review` reviewed
source, the runner/oracles and retained results. `/root/io_window_author` made a
fresh-target retained-source N=4 rebuild/replay, independent of the RNG/Boot
authors but not of its own RAM-window/deferred implementation or guest oracle.
That distinction limits the independence claim; the earlier window replication
and this separate source/method review remain applicable.

**Evidence validity: accepted for the finite development baseline.** Retained
`natemu-raw/io2-rng-20261108-01/build-02` contains natemu and sibling rust-vmm
sources and produces the tested binaries. Host tests pass 154 with two ignored;
resident build and stack/gateway audit pass. ACPICA checks N=1..4 include the
existing DSDT roundtrip plus an independently compiled symbolic RNG Device AML
comparison. The latter checks resource encoding; guest discovery is checked
separately. Earlier missing cpu_profile.json test input and unsuitable DSL-token
oracle failures remain retained in build-01; they were corrected before live
execution rather than excluded from the record.

Author Linux runs N=1/2 and the fresh-target replica N=4 all pass without retries
or timeout. The unchanged Debian 6.12.107 kernel/modules and static BusyBox use
normal ACPI discovery, find the expected MMIO resource and virtio-rng binding,
read four distinct nonzero 4096-byte payloads across platform unbind/rebind,
observe delivered GSI18 interrupts, and exit through normal S5 poweroff. The
runner checks CPU/kernel identity, exact markers, payload lengths/hashes, binding,
interrupt counts, kernel diagnostics and host return status; negative parser
controls and shell syntax checks pass. Sources, input/artifact hashes, commands,
full logs and outcomes are retained in `linux-01` and `replica-01`; the replica
records 715 unchanged source inputs and its new build targets. Three of twelve
120-second VM launches are used, with approximately 543 MiB of the 4 GiB storage
bound consumed. Runtime durations are diagnostic, not performance measurements.

**Decision relevance: accept real RNG integration as the baseline for block and
network work.** Source review found no unresolved blocker in deferred access
identity/recovery, permit cancellation versus grant, copied queue parsing,
ordered completion publication, serialized IRQ/ACK, feature/geometry validation
or owner join before VM destruction. Corrections incorporated before execution
include completion-time suppression reads, ready/status-gated rescheduling,
queue geometry preservation on disable, and closing admission before a vCPU is
marked ended. Checked rust-vmm traversal is now a production snapshot consumer.

**Limits/return:** unbind/rebind exercises the Linux lifecycle but does not prove
an adversarial reset arriving at every grant/backend/IRQ phase. Neither payload
hash differences nor successful reads establish entropy quality. These runs do
not establish high-RAM/PTE-alias behavior, native timeout races, persistent disk,
packet delivery, durability, new S5 drain behavior, native backend containment or
I/O performance. The first RNG baseline uses the then-current immediate S5 stop;
the subsequently edited power-wait/quiesce path is not covered by these results.
Full IO1 lifecycle coverage and the persistent disk/network/combined goal remain
open. Next: ordered block backend and durability with progress-capable S5 drain,
followed by basic networking and combined acceptance.


## Block and S5 source review before live IO3 — November 8, 2026

Author `/root` implemented shared Virtio transport, the ordered raw-file lane,
block stream parser, Boot/CLI wiring and S5 control wait. Independent reviewer
`/root/io_scope_review` inspected those implementations and the subsequently
added lifecycle tests and `tools/run_l4b.py` assertions. No production code was
changed by the reviewer, and no reviewer build, VM or disk experiment was run.

**Evidence validity: source design and finite unit-test coverage accepted.** The
review covers arbitrary descriptor splits of the logical header/data/status
streams, selected segment/request limits, validated completion spans and
conservative used-prefix lengths; immutable OUT snapshots, failed IN discard,
positive-short suffix progression and no automatic EINTR replay; serialized
reset/grant/IRQ state; and a single native lane retaining only its owned file
and buffers on fatal abandonment. S5 closes dequeue admission while preserving
the active request, then runs final full-sync and joins the idle lane before
publishing drained with IRQ cleanup under the state lock. The requesting port
instruction, or one REP element, commits before entering a nonreturning resident
wait that acknowledges RAM windows. Host polls do not wait on native I/O.

Review requested and the author added tests for quiesce versus later reset/notify,
final-sync success/failure and cancellation while the worker is blocked. The
worker seam runs the same production channel/execute loop with test-only Calls
implementations; release and destructor observations verify retained ownership
and that an abandoned result cannot block eventual retirement. These are host
simulations, not induced native filesystem hangs or full resident concurrency
proofs. Retained `natemu-raw/io3-block-20261108-01/build-01/step-0.log` reports
163 passing host tests and two ignored, including fifteen virtio tests. The new
L4b scalar S5 oracle checks RIP after OUT; the REP OUTSW oracle checks RIP stays
on the instruction with RCX=1 and RSI advanced two bytes. Their source matches
the fixture's three-byte REP OUTSW encoding; live outcomes remain pending.

The reviewer independently checked the current macOS 26.4 SDK selected by
`xcrun --show-sdk-path`: `usr/include/sys/fcntl.h:259` defines F_FULLFSYNC=51,
matching the native call, and line 605 declares the variadic fcntl signature.
`usr/include/unistd.h:587,589` declares pread/pwrite with the positioned offsets
used by the lane. The bundled `usr/share/man/man2/fcntl.2` F_FULLFSYNC entry says
it performs fsync followed by the drive flush, ignores the argument, and can take
a long time; it lists APFS support. This supports the selected API/ABI and the
need for containment. It does not establish support on every backing mount or
physical power-loss behavior. No weaker sync fallback is used on failure.

**Decision relevance: no unresolved source blocker to bounded live IO3
validation.** The normal CLI is the per-VM process boundary. A fatal timeout may
leave only file/buffer storage in a detached native lane until it returns or that
process terminates; no guest-memory authority survives. Embedders must treat it
as worker-process fatal, not retry indefinitely while accumulating lanes. The
author corrected the return-lifetime documentation and placed drained publication
after IRQ cleanup. Installed-root boot/write/shutdown/relaunch, actual full-sync
outcomes, live power-instruction commits and combined RNG/block regressions are
still required. This review does not accept IO3 completion, network capability,
performance or the full copy-first plan.


## IO3 installed-root independent replay — November 8, 2026

Independent `/root/io_scope_review` verified and copied the producing
`io3-block-20261108-01/build-01` natemu/rust-vmm inputs, retained the original
oracle files, then overlaid the separately reviewed frozen `oracle-01` runner
with recorded hashes. All 905 final source inputs remained unchanged. It built
into a fresh target, using offline locked Rust 1.97.0 on macOS 26.6.2/SDK 26.4:
163 host library tests pass (two ignored), the ordinary CLI built alone without
`testing`, resident build/signing and stack/gateway audit pass, and the runner
self-check passes. An initial wrong-package `--lib` invocation failed before
compilation and remains retained; correcting the command was the only retry.

**Evidence validity: accepted for the finite development baseline.** Exactly
two new N4 processes run the installed ext4-root write/shutdown and read-only
relaunch checks, with no VM retries or timeouts. Both exit normally through S5;
file data, partial overwrite, receipt, rename/deletion and offline e2fsck checks
pass. The fresh read-only boot leaves the entire post-write image hash unchanged.
It also reads entropy, and its log shows delivered block and RNG interrupts.
Producing inputs, commands, binaries, full logs and results are retained in
`io3-block-20261108-01/replica-01`; the
[reviewer report](/Users/liaozping/orca/natemu-raw/io3-block-20261108-01/replica-01/review.md)
records hashes, qualifications and the retained setup failure. The reviewer is
independent of both production block/S5 and fixture authors; the execution uses
the same separately source-reviewed guest oracle. Together with the six author
Linux launches, this uses eight of twelve; four of eight port checks were
reported by root. Retained IO3 storage is approximately 770 MiB of 4 GiB.

**Decision relevance: accept installed-root persistence at the tested N=1/2/4
baseline, with fresh N4 reproduction.** This is a meaningful disk/RNG capability
increment, not full IO3 or plan completion. Guest sync/remount before poweroff
does not force S5 while native I/O remains outstanding. Deterministic active-job
reset/quiesce/error publication, broader IO1 alias/participant/IRQ cases,
networking, concurrent combined acceptance and matched performance work remain
open. Actual successful full-sync is supported by the executed native return
path; physical power loss and every backing filesystem/hardware combination are
not established. No additional execution is needed to repeat this same baseline.


## Deterministic block lifecycle source and oracle review — November 8, 2026

Authors: `/root` implemented the testing-only native-call seam and driver
integration; `/root/io_window_author` authored `tools/run_virtio_lifecycle.py`.
Independent `/root/io_scope_review` inspected both and the corrected oracle.
The reviewer changed no production or fixture code and ran no VM for this review.
The frozen-source build is being retained under
`natemu-raw/io3-lifecycle-20261108-01`; the development bound is sixteen VM
launches, 120 seconds each, and 1 GiB retained. No lifecycle VM had run at this
source-review return.

**Evidence validity: source and method accepted for the first bounded run.**
The ordinary CLI has no testing feature. The driver installs a finite Calls
wrapper in the same production disk lane: real positioned reads/writes execute
on its owned File/private buffers; the wrapper may hold before a call, cap the
first actual call to 256 bytes, or inject the next EIO without issuing another
native call. It neither supplies guest completions nor bypasses parsing, queue
state, CopyPermit, RAM windows, IRQ logic or S5 drain. Reset/disable release the
barrier only after the actual control operation has closed its incarnation and
become deferred. Power release follows the real REQUEST_POWER_POLL, after the
resident commits OUT or one REP element. The metadata fixture port only reports
backend-entry state. Trace locking has no wait on guest/backend progress;
only the backend's bounded condition-variable hold waits. The driver stops and
joins device owners before VM teardown and reads final state/RAM only afterward.

The nine guests cover reset and QueueReady disable during accepted backend work
with fresh-queue recovery; scalar and REP S5 with an accepted OUT; failed final
sync; partial IN/OUT error propagation and recovery; an out-of-RAM descriptor
suffix followed by reset/recovery; and all 128 descriptors occupied by 64 FLUSH
chains. The last is a full descriptor table, not 128 available heads or index
wrap. Expected file contents and the complete 128 KiB declared RAM image are
constructed before execution. Guest code uses ordinary virtio MMIO and port I/O;
no reply tampering is enabled. Oracles check exact disk/RAM effects, architectural
RIP/register progress, status/used entries, interrupt-status ACK behavior, trace
ordering/counts, bounded schedules, process outcome and clean teardown.

Review found that the failed-sync branch initially skipped RAM comparison.
The corrected driver emits bounded post-VM `io-memory` records even on errors,
with a 4 MiB aggregate admission limit; every case now compares its complete
expected RAM image. Missing/duplicate/corrupt records are rejected. The FLUSH-only
case correctly expects no positioned-call entry and disables/readbacks the queue
before its final ACK to avoid racing the last IRQ offer. The author reports nine
guests assemble and 77 negative oracle controls pass; this source review checks
the control logic but does not independently promote those self-checks into live
evidence. The runner retains commands, source/artifact hashes, inputs, logs and
results, refuses output-directory reuse, serializes VMs under the shared lock,
stops on the first failure and does not retry cases.

**Decision relevance: no unresolved source/oracle blocker to the first live
lifecycle development run.** The trace's `calls` counts positioned-call wrapper
attempts, including an injected error that makes no syscall; sync calls have
separate events. `backend-held` is a pre-syscall barrier, not a suspended kernel
call. `commit` records completed RAM/used publication before IRQ offer and owner
retirement. These distinctions are required when interpreting results.

**Limits/return:** N=1 only; IF=0 polls interrupt status and ACK, rather than
establishing delivered guest IRQ races. Reset is forced during backend work,
not during a pending CopyPermit closing window. Injected errors validate their
propagation, not macOS failure atomicity or actual stalled-kernel containment.
No SMP reset/power peers, high-RAM/PTE aliases, old compiler replies, descriptor
mutation races, networking or performance are established. Actual live outcomes
and independent reproduction remain pending; full IO1/IO3 and combined acceptance
remain open.


## Deterministic lifecycle findings and independent replay — November 8, 2026

All nine authored N1 cases pass on their first live run under
`io3-lifecycle-20261108-01/live-01`. Independent reviewer
`/root/io_scope_review` verified the retained producing source manifest, copied
natemu/rust-vmm into a new replica, and built the testing host driver and resident
into a fresh target with offline locked Rust 1.97.0. Signing, stack/gateway audit
and the nine-guest/77-negative-control self-check pass. Sources remain unchanged;
no build or VM retry was needed. The ordinary CLI build without this test feature
was qualified separately by the installed-root baseline.

**Evidence validity: accepted for the finite N1 lifecycle development cases.**
The independent run repeats reset, REP power, failed final sync and failed IN,
all successfully. Each compares the full declared 128 KiB RAM image and 2 MiB
file, including the intentionally failed shutdown. Retained final records also
confirm old reset status remains 0xa5 with old used index zero, REP RCX=1/RSI+2
with RIP still at the instruction, and failed IN's IOERR/zero used-prefix length.
The final-sync failure exits 1 rather than reporting normal power success; all
four cases join/destroy their vCPU and VM cleanly, without timeout. Trace orders
match the reviewed barriers and production effect sequence. The
[retained independent report](/Users/liaozping/orca/natemu-raw/io3-lifecycle-20261108-01/replica-01/review.md)
links producing inputs, script, command logs, hashes and detailed qualifications.
The guest oracle is shared with the author run and was separately source reviewed.

**Decision relevance: accept this finite lifecycle/error increment for continued
integration.** A stale backend result is suppressed across full reset; accepted
OUT work drains through S5 after one REP element; failed final sync does not
masquerade as clean shutdown; and failed staged IN publishes truthful error
status without partial payload. The author's other cases additionally cover
queue disable/recovery, scalar S5, partial OUT recovery/flush, malformed-range
recovery and 64 FLUSH chains filling all 128 descriptors. These are development
results, not performance or universal durability evidence. Thirteen of sixteen
launches are used; retained storage is approximately 377 MiB of 1 GiB.

The preceding source-review limits remain: N1 and polled IRQ status; pre-syscall
barriers and injected errors; no forced reset during CopyPermit closing, real
stalled kernel calls, SMP lifecycle races, delivered IRQ/ACK races, pending
compiler replies, network or combined workload acceptance. This result does not
close the complete IO1 matrix or the owner's full I/O goal. No further repetition
of these same cases is needed for the scoped conclusion.


## Extended RAM-window findings and independent replay — November 8, 2026

`/root/rust_io_research` authored `tools/run_io_window_cases.py`, reusing its
previous window worker. Independent `/root/io_scope_review` reviewed the source,
then repeated all four N2 cases after the root's four first-attempt passes. Both
runs use the unchanged production window implementation. The reviewer reused
its freshly built lifecycle host/resident only after verifying both binaries
and all 907 original source inputs against the retained author manifest. The
new Python runner (sha256 aa1ad54d3040a500e571e9034dda6107cfe3311afbfe43c0c3b6c685655e9954)
is an append-only uncompiled input with a separate manifest; no compiled input
or previous manifest was changed and no new build was needed.

**Evidence validity: accepted for the stated development cases.** High RAM,
PTE, HLT and AP cases all pass in the independent run without retries/timeouts,
using 71/71/72/204 windows respectively, each with 64 designated snapshots,
per-CPU split atomics/progress, ordered overlap, code-patch visibility and clean
teardown. High RAM puts both copied data and executed/patched code above 4 GiB.
Both CPUs in the PTE case observe the expected old/new values after a copied byte
changes the mapped PFN. Each HLT participant records 67 halts and 68 interrupts;
the AP records mailbox waiting and a successful wake acknowledgement. Producing
inputs, logs/results and checksums are retained in
`io1-20261108-01/windows-extended-01` and
`io1-20261108-01/windows-extended-replica-01`; the
[independent report](/Users/liaozping/orca/natemu-raw/io1-20261108-01/windows-extended-replica-01/review.md)
records source identity and the detailed limits. Total IO1 live launches are
12 of 24, with approximately 900 MiB of its 4 GiB retained-storage bound used.

**Decision relevance: accept these concrete high-bank, mapping-outcome and
progress observations; do not broaden them into forced-interleaving claims.**
The PTE was initially read, but earlier write windows may reset its cached
translation before the final PTE-changing copy; the fixture does not guarantee
that entry is hot at the final write. HLT/AP waits coexist with windows but no
specific grant is forced and identified while a participant occupies that wait.
Pending compiler replies remain excluded. The same guest/oracle is shared with
the author and has separate source review. These results do not establish SMP
device reset/IRQ races, networking, combined-workload acceptance or performance.
No further repetition of these same cases is needed for this scoped conclusion.


## Forced HLT and power-wait cooperation — November 8, 2026

Independent `/root/io_scope_review` reviewed the `hold-power-halt` metadata-only
barrier and N2 `power-rep-halt` guest/oracle before execution. The author run
passes in `io3-lifecycle-20261108-01/live-02`. The reviewer verified all 908
retained `build-02` inputs, copied them into `replica-02`, rebuilt the testing
host driver and resident in a fresh target using locked offline Rust 1.97.0,
and passed signing, stack/gateway audit and the ten-guest/96-negative-control
self-check. Exactly one independent N2 replay passes, without retry or timeout;
source hashes remain unchanged.

**Evidence validity: accepted for the forced N2 development schedule.** The
trace orders actual power poll and helper parked-state observation before
backend release, positioned write, completion commit, successful full-sync and
lane join. Both complete RAM images and the full disk match. The initiator
retains REP RIP with RCX=1 and RSI advanced two bytes; the helper retains its
post-HLT RIP, marker and unchanged forbidden-progress register, records one
halt and zero interrupts, and ends cancelled. Both vCPUs join/destroy cleanly.
The hook supplies neither a guest result nor a window acknowledgement: the
subsequent completion requires resident acknowledgements through the parked
helper and committed power-wait initiator. Producing hashes, command logs,
results and qualifications are in the
[independent report](/Users/liaozping/orca/natemu-raw/io3-lifecycle-20261108-01/replica-02/review.md).

**Decision relevance: closes this specific forced HLT-park/power-wait completion
and S5-drain gap.** It does not prove native WFI had already executed when the
park state was sampled, general SMP reset/IRQ safety, forced AP/compile waits,
reset during CopyPermit closing, hot PTE mutation, real stalled kernel calls,
networking, combined-workload acceptance or performance. The barrier is before
the native call and the independently reviewed oracle is shared with the author.
Cumulative lifecycle use is 15 of 16 launches and approximately 694 MiB of 1 GiB.
No additional repetition is needed for this scoped finding.


## Network frontend and fio preparation source review — November 8, 2026

Independent `/root/io_scope_review` inspected the two-queue owner in
`crates/host/src/virtio/mod.rs`, owned packet endpoint and SG path in `net.rs`,
and the block request conversion to explicit device/queue identity. This is
source review before live network execution; no backend admission, packet
exchange, unit-test outcome or VM result is inferred. No production code was
edited by the reviewer.

**Evidence validity: one substantive shutdown correction required.** At review,
`owner` entered quiescence before checking `backend_error`, and `drain` published
`drained` without rechecking that error under its final state lock. Terminal
endpoint failure recorded just before or during quiescence could therefore be
reported as clean S5 drain. The requested correction is to inspect failure
before entering drain and atomically check failure with final drained
publication; test both already-recorded failure and the final-publication race.
Intentional endpoint drop after stopped/drained remains benign. This finding
was sent to the production author; closure requires inspection of the fix and
focused test evidence.

The inspected queue-specific reset/generation predicates preserve the other
queue's request, indices, pending work and shared IRQ status. RX arrival reserves
its bounded frame and identity under the same state lock as reset, schedules a
scan independently of a second guest kick, and refuses arrivals while RX is
active/full. TX uses owned bytes and waits for the backend result before guest
completion; reset cannot replay a submitted packet. Complete SG/range validation
precedes effects, and a valid RX chain too small for an incoming frame drops the
frame without consuming the head. Source review found no further substantive
defect in these paths. External backend supervision must still stop/join its
work; bounded channels alone do not establish native callback retirement.

**Decision relevance:** the concrete packet port is a suitable implementation
seam once the shutdown error race is fixed and tested. It does not choose a
native/user-space backend or establish the full network, integrated workload or
performance goal. Runtime queue/reset/IRQ and packet-boundary coverage remain
needed for the declared executable slice.

The reviewer separately inspected `io-benchmark-prep-20261108-01/README.md` and
all eleven fio profiles, verified seventeen README/profile/source checksums,
and checked retained QEMU source for segment limits and flush behavior.
**Preparation evidence is valid within its static scope:** finite CRC verification
and clean restart are separate from timed workloads; the 64 MiB pilot is
explicitly warm host-cache; Q1/Q16 means requested fio depth, not achieved native
concurrency; and stock QEMU's flush path is not labelled equivalent to
F_FULLFSYNC. The reader/loader package closure and downloaded package provenance
were not independently rerun in this review. No target binary, fio job or VM
was executed.

**Decision relevance:** accept these profiles as inputs to the next separately
bounded guest parse/correctness pilot, not as performance results or ready-to-run
measurement acceptance. Before scoring, specify untimed flush/drain and
restore/precondition behavior between paired cases so dirty tail work cannot
contaminate the next case or be included asymmetrically. Actual io_uring support,
package/dlopen execution, negotiated queue topology, durability conditions and
metrics still require the declared executable checks.

### Network shutdown correction return

The same independent reviewer re-read the correction: `owner` now checks
`backend_error` before entering quiescence, and `drain` checks it under the same
final state lock that publishes `drained`. `PacketPort::fail` takes that lock, so
failure before publication prevents clean success; intentional endpoint closure
after publication is ignored. The added
`terminal_backend_failure_cannot_become_successful_poweroff` test covers failure
recorded before quiescence, failure recorded between quiescence and final drain,
normal post-drain closure, and unexpected endpoint drop. This exercises both
sides of the publication boundary deterministically, not a threaded race. The
author reports all twenty virtio tests pass; the reviewer inspected the test
source but did not independently execute it.

**Disposition: shutdown blocker closed by source review and focused author test
evidence.** The lock establishes the required failure-versus-success ordering.
Out-of-profile incoming frames now report a normal drop (`false` in the final
boolean receive API), so they cannot accidentally be propagated as terminal
endpoint loss by a conventional adapter. The frontend is
ready for its bounded direct-guest integration fixture review; network runtime,
backend admission and full I/O acceptance remain open.


## Network direct-guest fixture pre-execution review — November 8, 2026

Independent `/root/io_scope_review` inspected `tools/run_virtio_net.py`,
`program_net.rs`, and `program.rs` integration before any live frontend run.
The four N1 cases exercise split/combined 12-byte headers and legal ignored
fields, maximum single-VLAN frame with untouched RX padding, large-packet drop
followed by a fitting packet using the same short RX head, alternating queue
disable/reconfiguration with preserved peer indices, full reset, and malformed
TX direction/out-of-RAM suffix followed by recovery.

**Method validity: accepted after one correction.** The original queue-preserve
helper read the write-only QueueDescLow register; the reviewer reported that
execution defect and the author removed the read/assertion without changing
production behavior. QueueReady readback, peer used index and later successful
completion through the original peer queue establish the intended preservation.
The author reports the four-guest/54-negative-control self-check passes again;
no live outcome is inferred from those synthetic checks.

The echo endpoint sees only owned packet bytes, attempts RX once before TX
acknowledgement and never edits guest results. Together with the production
owner's alternating selection, this forces the oversized short-RX frame to be
processed before the next TX supplies the fitting frame. The exact 384 KiB RAM
oracle includes unchanged bytes and all used entries/lengths. Malformed cases
require no native packet callback or used publication before reset/recovery.
Exact packet bytes/counts, receive admissions, final architecture, duplicate or
missing output records and owner/vCPU teardown are checked; full logs, frozen
inputs, hashes and no-retry outcomes are retained. The driver bounds frames at
64 and endpoint service at 45 seconds, with joins before VM destruction; the
runner adds its 120-second process-group bound and shared VM lock.

**Decision relevance:** proceed with the declared four authored runs, then
fresh independent headers/queues reproduction within IO4's eight-launch/1 GiB
bound. This validates only the direct frontend slice if it passes. Queue
disables follow completed requests: the fixture does not force a disable while
the other queue is active/pending, nor SMP reset/IRQ races. IRQ status is polled
with interrupts disabled. No native networking, privilege/admission, S5 network
shutdown, full Linux network stack, throughput or combined-workload conclusion
follows. No VM or fio job was run by this review.


## Network frontend findings and independent replay — November 8, 2026

All four authored N1 cases pass on their first live attempt in
`io4-frontend-20261108-01/live-01`. Independent `/root/io_scope_review` verified
all 911 retained source inputs, copied them into `replica-01`, and rebuilt the
testing host driver and resident into a fresh target using locked offline
Rust 1.97.0. Signing, stack/gateway audit and the four-guest/54-negative-control
self-check pass. Exactly two independent live cases, headers and queues, pass
without retries or timeouts; source hashes remain unchanged.

**Evidence validity: accepted for the finite direct frontend development slice.**
Both independent cases compare complete 384 KiB RAM images, exact packet
bytes/counts and endpoint admissions/close, guest architectural completion and
clean teardown. Headers emits 64-byte and 1518-byte frames with used indices
2/2, including split headers, ignored fields and RX tail preservation. Queues
emits four 64-byte frames, preserving old/new used indices 1/2/2/1/1/1 across
alternating disable/reconfiguration and full reset. Both finish with R15 zero,
stop reached and one vCPU joined/destroyed. The author's short-RX and malformed
recovery cases also pass; their outcomes were inspected but not independently
rerun. The shared fixture received separate independent source review. See the
[retained independent report](/Users/liaozping/orca/natemu-raw/io4-frontend-20261108-01/replica-01/review.md)
for exact input/binary hashes, commands, logs and reproduction limits.

**Decision relevance: accept this frontend increment for continued backend
integration.** Queue disables follow completed requests; active peer-queue
reset, SMP lifecycle and delivered IRQ/ACK races remain untested. Status is
polled with IF clear. No native network admission, Linux network stack, network
S5 drain, protocol connectivity, combined-workload or performance result follows
from an owned-packet echo. The complete I/O goal remains open. Six of eight IO4
launches and approximately 333 MiB of 1 GiB are used; no additional repetition
of these same cases is required for this scoped conclusion.


## Pending compiler window method review — November 8, 2026

Independent `/root/io_scope_review` inspected the proposed retained
`io1-20261108-01/pending-compiler-method.md` against `Server::request`,
`Compiler::compile_once`, resident `find_from`/`interrupted`/`io_checkpoint`, and
the actual RAM-window admission loop. No implementation or VM was run.

**Method validity: useful overlap test with one required oracle correction.**
A testing observer after the real channel post and before response consumption
can force a pending host compiler service. The compiler process may already
have finished; the correct claim is response consumption held, not FEX still
computing. A stable exact epoch with CPU1 acknowledged, CPU0 unacknowledged,
release below that epoch, unchanged written epoch and a still-pending copy
positively establishes the intended gate dependency. The timeout must be the
announced epoch's acknowledgement timeout, not prevalidation/coordinator failure.
Use no competing window during that observation and bound/release the observer
on every error path; its own hold cannot rely solely on a deadline inside the
not-yet-entered reply reader.

The proposed mandatory first-store marker value of one is not justified.
`find_from` does not checkpoint again after successful ordinary block placement,
but `interrupted` explicitly dispatches immediately when a pending doorbell
arrives at an ordinary block's entry, before its first instruction. Thus a
safely banked pre-instruction boundary may acknowledge the copy with marker
zero. Requiring one would falsely reject that path. Retain the exact-epoch gate
oracle, allow a truthful zero marker at that boundary with eventual guest
progress, or separately justify a route with an already-owed instruction; do
not change production dispatch to satisfy this fixture. This correction was
sent to root before execution.

**Decision relevance:** timeout/no-effects followed by release/exact-epoch
progress and observed patched code can close the finite delayed compiler-service
coverage gap. It does not establish compiler-process busy time, universal
instruction completion before a safe checkpoint, or arbitrary pending reply
races. Final fixture/source review remains required after the author implements
and corrects the oracle; no live acceptance is recorded here.

### fio installed-root admission harness — November 8, root independent review

Author `/root/rust_io_research`; reviewer `/root`, separate from the harness and
package-preparation author. Inspected the new `tools/run_fio_admission.py`, imported
block/rng helpers, fixed profiles, signed-index/package and ELF checks, cloned
root creation, bounded process/log supervision and full guest-output/persistence
oracles. Root reran the shell/symlink/negative-result self-checks.

A reproduction defect was fixed before preparation: log count alone could accept
a missing output replaced by an unexpected name. The final checker requires the
exact 27 names and complete BEGIN/END framing; renamed/missing/stray/duplicate
controls pass. Frozen oracle SHA-256:
`7285f523371fc07389618d47733cc153637a9e90f046c02c86950218fda3953c`.

**Evidence validity:** accept the method for bounded functional execution, not
an observed fio pass. The genuine package, complete dependency tree and existing
stock kernel/bootstrap are retained; only a separate test root/init changes.
The ordinary IO4 CLI build is reused with hashes matched against IO3 linux-03,
and the new oracle is separately retained rather than relabelled as build input.

**Decision relevance:** establish dynamic-loader/io_uring/fio and clean-restart
verification capability before using those jobs for performance. Four N2 launches
of 240 seconds and 2 GiB are the full primary/independent bound. No scored timing,
QEMU parity, power-loss durability or network capability follows. Retained method
and results belong under `natemu-raw/io-fio-admission-20261108-01`.


### Pending compiler method correction return

Root accepted the marker correction. At root's request, the independent reviewer
amended only the retained method document: marker zero or one at grant is
permitted, marker one is required eventually after release, the held tuple must
be bracketed by the same request epoch, and the timeout must specifically be
acknowledgement timeout for the announced epoch. The observer has an independent
finite bound and cancellation release. The method now explicitly permits an
ordinary block-entry interrupt before its first instruction. **Method blocker
closed; implementation and fixture review remain pending.** No runtime/fixture
code was edited and no VM was run by this return.


### Pending compiler implementation review — observation ordering correction

The independent reviewer inspected `program_pending.rs`,
`run_io_pending_compiler.py`, the testing compiler-post observer and read-only
`WindowStatus` accessor. The implementation uses real channel publication, a
finite five-second releasable observer, exact target request identity, production
copy permits, timeout/no-effects safe reads, legal marker zero/one followed by
eventual one, and final per-CPU code/progress/whole-region checks. Normal compiler
reply bytes and resident dispatch are unchanged.

**One method blocker remains:** the status accessor initially loaded `released`
before acknowledgement words and bracketed only `request`. A timeout could
release the epoch after that early load, followed by a late CPU1 acknowledgement.
The mixed tuple could then claim an unreleased epoch and matching peer ack even
though those conditions never coexisted. The reviewer requested a release-word
load after the acknowledgements, required still below the same epoch in both
the Rust observation and Python oracle, plus a negative control. The existing
SeqCst ordering then establishes the positive overlap without changing the
coordinator. This is a measurement defect, not an observed production gate
failure. No VM was run; closure awaits the observer/oracle correction.

### Network external-arrival lifetime review

Root identified that `PacketPort` can admit future RX with no queued request,
while the existing all-halted termination rule requires every such external
source to retain an `Irqs::busy` guard. The independent reviewer confirmed the
contract in `irq.rs` and the existing UART input owner. A separate endpoint
lifetime guard acquired before boot and dropped only after endpoint shutdown
is the smallest compatible fix; empty RX buffers still do not count as active
queue work. A constructor/stop-or-drain/drop counter test establishes ownership,
not live IRQ delivery from an all-HLT guest. The previously retained frontend
runs spin and remain scoped evidence for their frozen source. No live-HLT or
native-network claim follows from this source review.


### Pending compiler observation correction return

The independent reviewer inspected the final `WindowStatus` load order: both
request words bracket the sample, and `released_after` is loaded after the
acknowledgement words. Rust admission observation and Python judging require
that release still be below the same epoch, the peer acknowledgement match,
the held CPU acknowledgement differ, written remain unchanged, and the copy
worker remain unfinished. The successful attempt repeats those conditions for
the same epoch after at least 100 ms immediately before releasing the held
consumer. The focused negative controls now reject late release, a finished
worker, changed epoch/written state and premature held-CPU acknowledgement.
The author reports 22 negative controls pass; this return inspected source
without executing them.

**Disposition: positive-overlap oracle blocker closed; accepted for the one
bounded N2 primary run from frozen inputs.** The post-acknowledgement release
load closes the identified mixed-tuple timeout race; the second held sample
and unfinished-worker checks prevent a completed copy from being presented
as pending merely because its thread is joined later. Marker zero/one at grant
remains legal, with eventual one and per-CPU patched-code progress required.
This is pre-execution source/oracle acceptance only; observed results and fresh
independent reproduction remain outstanding. No coordinator/resident behavior
was changed by these observation corrections.

The reviewer also verified the actual network `_arrival: Busy` field is acquired
in `Virtio::network` and lives in `PacketPort` until drop. Its focused test
checks live=1 with no pending queue work, retains live=1 after drain while the
endpoint remains owned, and obtains live=0 after drop. **Endpoint guard source
correction accepted.** The test proves guard ownership, not all-HLT packet
arrival/IRQ delivery, and does not retroactively alter the frozen IO4 evidence.


## Pending compiler findings and independent replay — November 8, 2026

The authored N2 run passes in `io1-20261108-01/pending-live-01`. Independent
`/root/io_scope_review` verified and copied all 914 retained inputs into
`pending-replica-01`, rebuilt host driver and resident into a fresh target with
locked offline Rust 1.97.0, and passed signing, stack/gateway audit and the
22-negative-control self-check. Exactly one independent N2 replay passes
without retry or timeout; source hashes remain unchanged. The primary host
test log separately records 170 passed and two ignored.

**Evidence validity: accepted for the forced held-response development case.**
The independent timeout attempt announces epoch 5 with ack [4,5], both release
loads 4, written 4 and unfinished copy. It returns acknowledgement timeout with
release 5, written still 4 and CPU0 still unacknowledged. Later safe reads
confirm unchanged sentinel/function bytes. The successful attempt observes
epoch 10 with ack [9,10], both release loads 9, written 9 and unfinished copy,
then observes the same conditions again after 102 ms. Releasing the host
consumer yields successful copy with ack [10,10], release/written 10. The
marker read at grant is zero, as permitted for an ordinary block-entry stop;
eventual markers [1,1], code [2,2], 64 split-atomic snapshots, increasing
per-CPU counters and exact final RAM all pass. Both CPUs reach their expected
ends and join/destroy cleanly. See the
[independent report](/Users/liaozping/orca/natemu-raw/io1-20261108-01/pending-replica-01/review.md)
for producing hashes, commands, traces and reproduction limits.

**Decision relevance: closes this finite delayed compiler-service/window
coverage gap.** Real compiler requests were posted and host response
consumption was held; no assertion is made that FEX remained busy. The result
does not require an instruction not yet begun to complete before acknowledgement
and does not establish arbitrary reply/crash races, networking, performance
or complete I/O acceptance. The independently reviewed guest/oracle is shared
with the author; build and execution are independently reproduced. IO1 uses
14 of 24 launches and approximately 1218 MiB of 4 GiB. No further repetition
of this same case is needed for the scoped conclusion.


## fio progress diagnostic findings review — November 8, 2026

Independent `/root/io_scope_review` inspected the original failed admission,
the retained `diagnostic-method.md` and root's method/source review, then the
completed `io-fio-admission-20261108-01/diagnostic-live-01` phase records, raw
serial/proc observations, framed cumulative fio JSON, host process accounting
and six native sample reports. The reviewer launched no VM, build or profiler
and made no production changes. This is findings review of one diagnostic,
not independent execution or performance qualification.

**Evidence validity: accepted as bounded progress/location observations.**
The diagnostic stops at its observation deadline after 152.751 seconds, exit 2;
fio had started but had no exit, final verification, sync/remount/S5 or restart.
`admission_pass` remains false. The original 240-second failed image remains
separate; its changed hash and an unrecovered journal do not establish
filesystem corruption or completed fio bytes. Version and eleven parses had
returned zero in that original attempt, even though timeout prevented final
export/oracle completion.

The diagnostic's host-received version markers occur at 7.727 and 32.112
seconds; fio-start occurs at 32.529 seconds. Thus roughly 24.4 seconds precede
the workload inside the version command interval and must not be charged to
block verification work. This diagnostic omits the original repeated library
hash/parse startup, changes warmup and adds monitoring; its duration is not
comparable baseline throughput.

The reviewer independently decoded all five complete base64 snapshot frames,
verified their hashes and parsed their complete JSON objects. They contain
zero/four/ten/fifteen/twenty cumulative objects respectively, with twenty
distinct observations after deduplication. Reported write `io_bytes` advances
monotonically from 937,984 to 19,218,432, `total_ios` from 246 to 4,708, and write
runtime from 4,704 to 100,817 ms; every observed job error is zero and read
`io_bytes` remains zero. These fields are retained as reported, not summed
across retransmitted cumulative history or treated as a final job result.
Independent guest counters also advance: worker write_bytes reaches 22,622,208
in the final partial heartbeat; block completed writes/sectors rise from
82/15,160 in the first heartbeat to 5,260/57,272 in the last block record.
Block counters include filesystem/journal work. This rules out a total I/O
stall over the observed progress intervals; it does not establish completed
verification or clean persistence.

Host `ps` at 151.640 seconds reports cumulative CPU 301.40 seconds for natemu
and 1.12/0.69 seconds for its two actual FEX children. In the two native sample
sets, the disk lane is waiting for a channel job in 167 of 168 and 169 of 170
thread observations; the second set has one fcntl observation. FEX reports show
wait/poll stacks, and its process CPU changes little after startup. vCPU stacks
contain both Hypervisor execution and repeated
`Compiler::reuse -> answered -> admission` work. For example, the second set
has 43 and 52 observations on that dominant reuse branch for vCPU0/vCPU1
respectively, out of 170 per thread, alongside 95/90 hv_trap observations.
These are sampled stack locations, not exclusive CPU-cost percentages. Host
sampling cannot resolve guest code versus resident work beneath hv_trap.

**Decision relevance: prioritize a narrow investigation of execution/reuse/
readmission and coherence costs, without claiming the cause is proven.**
Observed progress contradicts a sustained global completion/IRQ stall over
these intervals. Predominantly idle disk-lane samples and quiet FEX children
provide no support for a continuously blocked native disk syscall or expensive
fresh compilation as the dominant sampled activity. Repeated host admission
is concrete evidence of work to inspect, but does not alone prove RAM-window
flushes cause the overall delay, establish the share spent copying, or justify
a correctness-affecting optimization. Write preparation/CRC CPU work may still
contribute; the verification-read phase has not begun in the retained fio
objects. A discriminating next step should identify why cached code repeatedly
requires admission and relate that path to actual window invalidations before
choosing a fix.

The guest monitor's complete heartbeat starts are 32.12, 37.56, 62.91, 90.05
and 115.61 guest uptime seconds despite a five-second sleep, so each multi-PID
record is sequential and potentially expensive, not a simultaneous five-second
snapshot. Native samples cover only two brief windows; status/monitor/profiler
overhead is unqualified. No throughput, product-speedup, durability, causal
optimization or full I/O acceptance follows. The unchanged original admission
with clean shutdown and read-only restart remains required after diagnosis.
This diagnostic consumed one additional VM (two of the four fio admission/
diagnostic launches used); no new launch was requested by this review.


### fio host-accounting diagnostic pre-execution return

Independent `/root/io_scope_review` compared the proposed diagnostic runner
with its retained predecessor and inspected the existing CLI/logger/cancellation
paths. The changes select the existing `--perf-log`, record its use in the
manifest and choose a 60- or 120-second post-start observation limit. Production
binaries, resident, prepared image and exact workload remain unchanged.
**Accepted for the third exploratory launch**, with 180 seconds total, 60 after
fio-start and the existing log budget; no performance or causal conclusion is
pre-approved. No VM/build was run by this review.

The top-level size accounting includes `host-perf.log` and `.v0`/`.v1`. However,
its 60 MiB polling threshold plus 4 MiB reserve is a soft operational bound:
two 1 MiB request buffers can flush during shutdown, and the process may keep
logging during its two-second SIGTERM grace. Record the final total including
all performance files and classify any 64 MiB overrun. Retain rather than
truncate an over-budget result.

Final vCPU log records are written after compiler cleanup; SIGKILL after the
two-second grace can prevent them. Aggregate conclusions therefore require
exactly two distinct vCPU final records, no failed writes and no dropped request
lines. Missing statistics are unavailable evidence, not zero costs. The normal
resident does not enable its optional perf buckets: false perf magic and zero
buckets cannot be interpreted as measured zero resident time. K1 flush counts
and flush_us are unconditional but include boot/version/workload together.
Per-request start and generation fields can relate repeated admissions to
flush generations; they do not alone identify why a flush happened or establish
that avoiding it is safe. These source limits were sent to root before execution.


Root added and the reviewer inspected final evidence checks: a separate
`host-perf-summary.json` requires exactly two distinct vCPU IDs 0/1 and zero
failed/dropped log counts, retaining parse/missing-file errors and an explicit
incomplete flag. The result records all non-image output bytes before its own
write and a 4096-byte final-result reserve against 64 MiB. These checks address
the identified teardown/statistics and final-size reporting gaps. Source
acceptance for the frozen exploratory run is unchanged; incomplete/over-budget
outputs remain retained limitations rather than fabricated zero observations.


### fio host-accounting diagnostic outcome — boot-only log limit

Independent `/root/io_scope_review` inspected retained
`io-fio-admission-20261108-01/diagnostic-live-02` after execution. The run stops
after 6.322 seconds at the 60 MiB live-log threshold, exit 2. No diagnostic
phase marker or fio-start appears; serial ends during boot after block-device
detection. `admission_pass` is false. The final pre-result output total is
64,791,306 bytes and the reported 64 MiB bound check passes; the two request
logs account for 36,056,879 and 28,037,240 bytes. This is a retained observation
failure to reach the target workload, not a completed fio diagnostic.

Both distinct final vCPU records were preserved, with zero failed writes and
zero dropped request lines. The normal resident's perf magic is false. K1
records 39 flushes for each CPU, flush_us 62,814/60,415, and zero capacity
flushes. Whole-launch host admission totals are 228,515,393/184,987,316 ns,
while compiler hit counters are 143,248/136,499. These are boot-only cumulative
observations; they neither isolate I/O-caused flushes nor explain fio's later
slow progress. Disabled resident buckets remain unavailable.

**Evidence validity:** accept the stop/resource/completeness observations and
their explicitly boot-only counter scope. **Decision relevance:** the per-request
logger exhausts this diagnostic budget before the workload; the run does not
test the fio cache-flush/readmission hypothesis and provides no optimization
or performance qualification. Do not extrapolate a fio speedup or change the
workload to manufacture completion. The previous advancing-write diagnostic
remains the relevant workload observation. No fourth repetition is planned;
three of four fio campaign launches are used. No VM/build was performed by
this review. Any production invalidation change requires its own correctness
review/tests, and any claimed fio improvement requires fresh original admission
and subsequently qualified measurements.


### Targeted DMA invalidation — independent fresh-build replication

Independent reviewer `/root/io_scope_review` reproduced the accepted targeted
invalidation slice on November 8, 2026, without editing production or fixture
sources. Raw evidence is retained at
`/Users/liaozping/orca/natemu-raw/io1-20261108-01/targeted-replica-01`.
Production design/code review was performed separately by `/root/io_window_author`;
this entry establishes independent build/input verification and bounded replay,
not a second exhaustive production review.

**Inputs and method.** All 915 files in `targeted-build-02/source-sha256.json`
were verified before copying into a new replica directory. Its original manifest
hash is `280113b2c42c3f00f3a5963c57c87c992eff7fd25dfb0af7e25c40a2e8adaabd`.
All 379 oracle02 hashes were also verified. Exactly two Python fixtures differ
from the producing build snapshot: `run_io_windows.py` and
`run_io_window_cases.py`; all shared compiled inputs match. Original tool copies,
the original manifest, a separate overlay record and the resulting 915-file
manifest are retained. The final input-manifest hash is
`5459f2f6a89cb73f5da3adde9e1aebbf29bd30219c8ef72ce8878aca1f0ad81c`.
Every input was rechecked after building and running.

A fresh target directory built `natemu-host`, then the ordinary `natemu-cli` in
a separate Cargo invocation, then the resident, using Rust 1.97.0 with locked,
offline release builds. Host executables were signed with the retained
entitlements; the resident stack audit passed. No full host-test repeat was
needed for this replication; the author's reported 172 passing/2 ignored tests
remain author evidence. The driver, ordinary CLI and resident SHA256 values are,
respectively, `d3432c896d7556c0cd8a52ff6fe76cf07e828f4bf4b18fd9d67e504d140e2ba9`,
`3c26461da1684505340f320c19d4036494af11bbbb2d5c0b4f1652325a4cc173`, and
`2eac27aab329574ef99357109e90c0a113af40456fb58726ee59fde86b718fd6`.
The existing pinned FEX artifact was hash-verified, not rebuilt:
`3ccb420ccc8d4dc0b6f81366985ddedbbfe68e52f69a8d591f586dbe82d9d2f4`.
An initial local script syntax-check command could not write Python bytecode to
macOS's cache directory; an in-memory syntax check succeeded before any build
or VM. This did not consume a VM or alter inputs.

The root checked the exact commands before execution. Three serial VMs ran
under the shared nonblocking lock, each with a 120-second runner bound: N4
`run_io_windows.py --cpus 4 --preserve-code`, then N2 `pte-code` and `pde-code`
through `run_io_window_cases.py`. All passed on their first attempt, with no
retry or timeout and clean vCPU/compiler/VM teardown. Each reported 71 windows,
64 snapshots, ordered overlap, code value 2, and progress for every participant.
The N4 split counts were 4420/3396/2907/2480. Full-cache flush counts were exactly
one per CPU in all three runs, satisfying the initialization-only allowance.

Both PTE CPUs observed executable value `0x1122334455667788` before the host leaf
edit and `0x8877665544332211` afterward. Both PDE CPUs observed those same old/new
values for code and for data within the global 2-MiB mapping. The fixture enables
PGE and reuses VA 0 after the shared function returns before publishing value 2;
the PDE case additionally reuses the data address. Its wait flag starts at 2.
These details avoid a stale initial observation or premature completion being
mistaken for the intended remapping schedule. The guest performs no intervening
INVLPG for the final host edit. Logs, generated programs, commands, individual
verdicts and output hashes are retained in `windows/`, `cases/`, `completion.json`
and the step records.

**Evidence validity:** accepted for these concrete schedules: split-atomic
snapshot coherence and progress, targeted code patch visibility, executable
4-KiB/global 2-MiB mapping changes, and absence of additional whole-cache flushes
in a small cache-fitting fixture. This is an independent fresh-source rebuild
and replay of separately reviewed author fixtures; it is not an independently
invented oracle, a fresh FEX build, or proof that every translation remained
resident at the exact final-write instant. The full-flush counter does not by
itself measure individual object retention or elapsed invalidation cost.

**Decision relevance:** supports the scoped correctness/retention acceptance of
the targeted invalidation change. It does not establish fio causality, improved
throughput, Linux filesystem regression behavior after this change, or broad
performance acceptance. Those require their separate unchanged-workload runs.
This replication consumes exactly three launches, taking IO1 to 22/24. Cumulative
retained IO1 storage measured approximately 1667 MiB (replica approximately
82 MiB), within the 4 GiB bound. No further VM/build is part of this review.


### Targeted DMA invalidation — source and executable-remap method review

Reviewer `/root/io_window_author` independently reviewed the new targeted
invalidation change and root-authored executable-remap fixtures on November 8,
2026. This reviewer authored the earlier RAM-window/deferred-access foundation,
but did not author this invalidation patch or the two new remap cases. This is
source/method review, not independent execution; the fresh-build replay above
was performed by a separate reviewer.

**Source contract: accept.** In `crates/host/src/execution/io.rs`,
`invalidate_page` uses the same physical-page ownership layout and atomic
stale/summary/PAGES publication as the resident. `copy_with_permit` validates
all spans, obtains all exact resident acknowledgements and the final one-use
grant, and publishes every touched write page before the first RAM copy.
Existing pending invalidations are OR-preserved. Cancellation before grant
changes neither ownership records nor RAM. Configured participants are bounded
to 1..64; bit 63 and high-RAM physical-page indices use the existing ABI layout.
Unaligned and crossing-page spans cover both endpoints, and only checked,
installed RAM ranges reach the copy. The private test backing now includes the
complete global metadata layout; the crossing-page/owner/pending/cancel checks
exercise actual record locations rather than a smaller substitute map.

Keeping `written` for **every** write is necessary: a destination with no code
owner can contain a guest PTE/PDE. Pre-ack shadow reset alone does not invalidate
compiled-fetch mapping currency. On release, `io_checkpoint` drains pending
page invalidations and calls the global `tlb(s, true)` wrapper before another
acknowledgement or guest access. That wrapper conservatively resets shadows
again; this is safe even though pre-ack reset already removed them. Existing
`cache::invalidate`/`cut` remove stale objects and their incoming/outgoing links;
`current` revalidates surviving objects against changed mapping currency.
Retained local links rely on the unchanged source-page subset invariant.
All-vCPU acknowledgement still excludes pending host compilation/publication;
no host acknowledgement is synthesized. No checkpoint was added while a
selected step/block, active memory operation, or raw completion metadata is
live. The uncached step path therefore retains its prior exclusion premise.
No correctness blocker was found in these connected paths.

The page-iteration comment was clarified after review: counting visits rather
than unique pages, many short crossing spans can each touch two pages. This
was a documentation bound correction, not a runtime defect. Retained producing
source and later comment-only workspace revision remain distinguishable.

**Oracle scope and corrections.** `run_io_windows.py --preserve-code` rejects
more than one whole-cache flush per CPU. This tiny fixture fits the cache and
has one initialization flush from the loader, so the allowance is meaningful:
the retained old implementation had four. Existing split-atomic snapshots,
ordered overlapping copies, code-patch observations and per-CPU progress remain
required. The flag is now recorded in manifests/results. This proves the
absence of additional full flushes in that finite fixture, not universal object
residency, invalidation latency, or a fio bottleneck.

Two substantive defects in the initially unexecuted `pte-code`/`pde-code`
method were corrected before freeze/execution. The large-PDE wait byte starts
at 2, so its loop now compares against 2 rather than prematurely completing on
a nonzero byte. Both cases now rewarm executable VA 0 **after** calling the
shared function and **before** publishing its saved result; the PDE case also
rewarms data VA 0x1000. Observing the patched shared-function result on both
CPUs therefore establishes rewarming after the preceding code-patch window,
without imposing a false requirement that an object must remain resident at
every subsequent scheduling instant. A prelude-only warmup, or a warmup before
the function call, would not establish that ordering.

The final host byte edit changes a global executable 4-KiB PTE from physical
16 MiB to 16 MiB + 64 KiB, or a global executable 2-MiB PDE from physical 32 MiB
to 16 MiB. PGE is enabled, code/stacks/page tables remain mapped independently,
and the guest performs no intervening INVLPG for the final edit. Both CPUs must
observe old/new code constants; the large case additionally checks old/new data
constants. Scratch bytes occupy unused table entries. Both cases retain the
full-flush allowance. Corrected oracle02 was accepted for bounded execution;
oracle01 remains retained and unrun. Broader permission changes, aliases and
link combinations remain outside this finite method.

### fio original-admission continuation — independent method review

Reviewer `/root/io_window_author` accepts the continuation method after the
separate targeted-invalidation review and fresh-build replication. No build or
VM was performed during this review. The original campaign's three consumed
launches remain failures/diagnostics; extending its declared limit to seven
launches and 3 GiB leaves exactly two N2 write/relaunch pairs. Stop at the first
failure in either pair, retain it, and reassess rather than retrying.

The frozen `io-fio-admission-20261108-01/oracle-01/natemu/tools/` runner hashes
match the original prepared input manifest and the unchanged workspace copies:
`run_fio_admission.py` SHA256
`7285f523371fc07389618d47733cc153637a9e90f046c02c86950218fda3953c`,
`run_virtio_block.py` SHA256
`93e5d8233c4795000aa2d03f3513d7437fde885acdd4f76328c1724b03ab77b3`, and
`run_virtio_rng.py` SHA256
`f7103db0b326cb6f2bd45d6a2c0986286911b9a803236ade41f8ac930c8fc9f7`.
Original `inputs-01/inputs.json` SHA256 is
`59429f6adf4b0e00dc7829de72d77e5acd458eeab49e6a8bebc774f5c2859cd2`.
The primary's `io1-20261108-01/targeted-build-02/fio-build-manifest.json`
correctly binds all three supplied binaries, the source-manifest hash and eight
retained build-result hashes; these were recomputed and matched. The independent
pair needs its own equivalent binding to its independently rebuilt binaries.

Each pair clones the pristine 512 MiB root and checks its hash, rather than
reusing a timed-out or previously written image. The unchanged runner checks
prepared inputs and binaries before execution and records its exact oracle and
command. Both boots keep N2, 2 GiB guest RAM, compile-cache off, the original
240-second limit and a bounded log size. `NATEMU_PERF_LOG` is removed from the
child environment. There are no diagnostic workload or parsing shortcuts.
The original guest verifies its library/profile hashes, parses all eleven jobs,
and executes the genuine 64 MiB, 4-KiB, direct io_uring depth-16 CRC workload.
The write job keeps verification and end_fsync; the restart job cannot create
a missing file. Admission requires all bytes read/verified, clean guest status,
remount-read-only, S5, matching whole-file hash after relaunch, an unchanged
whole disk during that read-only boot, and offline read-only fsck. Native-tool
timeouts are retained and converted into failures; any first-phase failure
prevents the second launch.

**Decision relevance:** this is eligible continuation of the original functional
admission criterion with a separately reviewed production revision. No reduced
workload or relaxed oracle was substituted. Passing would qualify this concrete
application/workload and restart path; it would not establish the cause of the
previous timeout, a measured speedup, sustained throughput, latency, or comparative
durability. Those conclusions require separate qualified measurements.


### Original fio admission — independent normal-CLI replay after targeted invalidation

Independent `/root/io_scope_review` reproduced the original two-boot functional
fio admission on November 8, 2026. Evidence is retained at
`/Users/liaozping/orca/natemu-raw/io-fio-admission-20261108-01/replica-01`, with
separate `replica-01-build-manifest.json`, command, reproduction script and run
records in the campaign directory. Root reviewed the exact binding/command
before execution and released the shared lock after its primary pair and the
separate frontend regressions completed.

The normal CLI and resident came from the independent fresh-target build in
`io1-20261108-01/targeted-replica-01`, described above. The CLI was built separately
from the testing driver; the guest ran through ordinary CLI/Boot/block/S5 paths.
All 915 final build-input hashes were reverified, as were CLI, resident and
pinned FEX hashes against that independent build record. No new compiler build
was needed. The original oracle's three files, all prepared output hashes and
native filesystem-tool hashes were verified. The runner remained exactly
`7285f523371fc07389618d47733cc153637a9e90f046c02c86950218fda3953c`.
Prepared-input manifest hash remained
`59429f6adf4b0e00dc7829de72d77e5acd458eeab49e6a8bebc774f5c2859cd2`.
The original image/workload and its 64 MiB length were not reduced or rewritten;
a fresh image clone was made for this pair. These checks establish immutable
input reuse, not an independent reconstruction of the Debian package/image
preparation or independently invented oracle.

Exactly two serial N2 boots ran under the nonblocking shared VM lock, with the
unchanged 240-second per-VM and 64 MiB live-log limits, no retry. Both passed:

- Write boot: genuine fio 3.39 loaded its dependencies; all 11 profiles parsed;
  the `io_uring`, direct=1, iodepth=16, 4-KiB, CRC32C verification job reported
  error 0, 67,108,864 bytes written and 67,108,864 bytes read back.
- Read-only restart: all version/profile checks passed again; verification
  reported error 0, 67,108,864 bytes read and zero bytes written. The persisted
  file hash matched the write boot, and the entire disk image was unchanged.
- Both guests remounted root read-only and reached `Power(1)`; both native
  `e2fsck -fn` runs completed all five passes with exit 0. Neither boot timed out
  or exceeded the log limit. Recorded phase elapsed times were 105.471 and
  98.010 seconds, used here only to establish compliance with the execution bound.

The within-pair file SHA256 is
`80e7cf1242d8b625db127243a84f3ba77fd0306c61fd9523daf53f932af0d6ed`.
The post-write/pre-verify/post-verify image SHA256 is
`86b58ea9bf2ca7a206551129f726cae097bd1e8f5a8b4a1cc967d87146585a37`.
Cross-run equality with the author's separately generated file is not required
by this oracle; persistence is checked across this replica's own relaunch.

**Evidence validity:** accept functional admission of the unmodified installed
fio workload and its required userspace/kernel interfaces through the production
copy-first block path, including full CRC verification, successful S5, clean
filesystem and read-only persistent restart. Guest-reported achieved fio depth
is retained; it does not imply that the backend processed 16 native jobs
concurrently. This is an independent execution using independently built normal
CLI/resident binaries and the original reviewed oracle/inputs. The FEX binary
and image preparation are shared retained inputs.

**Decision relevance:** the unchanged original admission now completes in both
author and independent executions after the targeted invalidation change.
This qualifies the functional slice and a post-change integrated Linux/block
regression. It does not isolate the cause of the earlier timeout, quantify an
optimization speedup, qualify benchmark scores, compare QEMU, establish host
cache bypass from guest direct I/O, or replace remaining network/combined-load
acceptance. The original timeout and diagnostic failures remain retained.
The reviewed extension is exhausted at 7/7 campaign launches, including those
failures; cumulative allocated storage measured approximately 1775 MiB, within
3 GiB. No additional build or VM is included in this review.


### Network endpoint all-HLT lifetime — independent review and replication

Independent `/root/io_scope_review` reviewed the root-authored arrival fixture,
idle observation and positive/negative oracles before execution, then rebuilt
and replayed the positive case from frozen inputs. Evidence is retained under
`/Users/liaozping/orca/natemu-raw/io4-halt-20261108-01`, including the independent
`replica-01` and both negative attempts. No production or fixture source was
edited by the reviewer.

**Source/method review.** The existing idle observer requires all configured
participants to be nonended, equal double-read park states and an unchanged
idle tuple across at least 100 ms. It reads the existing liveness metadata after
the park reads and requires live=1, kickers=0 and io_active=false. The observation
adds no veto, Busy guard, doorbell or guest-RAM access. Its trace precedes release
of the packet worker's atomic latch. The worker owns bytes, attempts receive
once, and exits normally when the port closes. The guest issues no QueueNotify
or TX; a configured RX chain alone does not schedule owner work. Thus the
PacketPort arrival guard is the remaining asynchronous liveness source.

The guest programs level-triggered GSI17 through the I/O APIC to LAPIC vector
0x40, then STI/HLT. The handler records InterruptStatus 1 before ACK and 0 after,
increments its marker, and performs LAPIC EOI. Exact 384 KiB RAM comparisons
cover the RX header/payload, unchanged padding, descriptors/rings, IDT and
handler status/marker bytes. Final interrupt count must be exactly one; the
positive stop is poweroff at the post-OUT RIP with forbidden post-power R15
still zero. The negative oracle requires the specific resident HALTED completion,
zero interrupts, unchanged initial RAM and only the closed endpoint trace.
Timeout, cancellation and arbitrary resident exits do not satisfy it.

**Primary and isolated negative evidence.** The primary positive run observed
101,214 us at tuple [1,0], followed by exactly arrival:true and closed, one HLT,
one IRQ and clean poweroff. The isolated negative build's source manifest differs
from the positive build only at `crates/host/src/virtio/net.rs`: the endpoint's
real `device.irqs.busy()` guard is replaced with a Busy holding an unconnected
atomic counter initialized to 1. Its precise diff is retained; workspace
production was not mutated. This removes the liveness veto without removing
the endpoint or changing guest/oracle inputs.

The first negative attempt (`negative-live-01`) remains **FAIL**: the expected
underlying behavior occurred, but the original oracle expected stop label
`halted`. Actual serialization is stop=`resident`, reason=`halted status=1
detail=32`, as `program.rs` serializes `Stop::Resident`. Its RIP was 0x100246,
halts=1, interrupts=0, RAM unchanged and trace only closed. This source/oracle
mismatch was missed in the initial review and was corrected only in the test
oracle, retaining the failed attempt. The revised oracle requires the exact
reason as well as the resident label and adds a wrong-reason negative control.
A fresh corrected negative run (`negative-live-02`) passed with that same
specific HALTED/no-packet/no-IRQ/no-RAM-effect outcome. Neither attempt is
rewritten or retroactively promoted.

**Independent reproduction.** All 915 frozen `build-01` file hashes were verified
and copied into a new replica with a fresh target. Original source-manifest hash:
`f5f13a301eea5146163803effd5483927ce673e37a7de2b88b36eff284420535`.
Only the separately frozen corrected `run_virtio_net.py` was overlaid, hash
`bb4a32960cb09c93cc5f46405c1e7680f8ac9f1573c45f99d5d972962b031c9e`;
original tool and original/final manifests plus overlay record are retained.
The final manifest hash is
`e714aba041b40221fd764624abdff88c7076b3cad29b09d3d0d318602558c1b0`.
Locked/offline Rust 1.97.0 release builds of the program driver and resident,
host signing, resident stack audit and five-guest/77-negative-control self-check
all passed. Driver SHA256 is
`1c371d2fcb19cb983212c6bef4b24e4bd5cb54841a96f86779678017a7effab9`;
resident is `2eac27aab329574ef99357109e90c0a113af40456fb58726ee59fde86b718fd6`.
The existing pinned FEX binary was hash-verified, not rebuilt. Inputs were
rechecked unchanged after building and execution.

One positive N1 replay ran under the shared nonblocking VM lock with the
120-second bound and no retry. It passed on the first attempt: held_us=100723,
first=last=[[1,0]], live=1, kickers=0, io_active=false, followed by exactly
arrival:true and closed. The guest ended poweroff at 0x1002d8 with R15=0,
halts=1 and interrupts=1; exact RAM and complete vCPU/compiler/VM teardown
checks passed. The post-ACK bytes are zero, marker and prior status are one,
and RX used.idx=1/used.len=76. No native backend was launched.

**Evidence validity:** accept independent reproduction of this concrete N1
all-HLT endpoint-liveness schedule, owned RX arrival, production queue/DMA path,
real GSI/I/O-APIC/LAPIC interrupt delivery and ACK, and successful completed-RX
S5 shutdown/endpoint joining. The separately inspected isolated negative gives
specific evidence that the fixture detects the missing lifetime guard. The
reviewer independently built/replayed the positive; the negative build/runs
remain root-authored evidence. The oracle and pinned FEX are shared inputs.

**Decision relevance and limits:** closes the frontend's concrete asynchronous
arrival-after-all-HLT gap. Stable resident park metadata is not proof that a
particular ARM WFI was executing at the observation instant. S5 follows RX
completion, so this does not test shutdown with an RX transaction still active.
It does not establish native networking, Linux network behavior, SMP packet
wakeups, backend privilege admission, combined workload behavior or performance.
The four-launch campaign is exhausted at 4/4, including the retained oracle
failure; allocated storage measured approximately 245 MiB, below 1 GiB. No
additional build or VM is part of this review.

### Active network TX lifecycle — pre-execution independent review

Independent `/root/io_scope_review` reviewed the root-authored `program_net.rs`
Hold/HoldPort seam, program-driver registration/power hook and the
`active-other-queue`/`active-power` fixtures and exact RAM/trace oracles before
any live run in `io4-active-20261108-01`. No production changes, builds or VMs
were performed by this review. **Accepted for the declared four-launch,
120-second-per-VM, 1 GiB campaign**, subject to retaining frozen inputs and
primary/independent outcomes; this is method acceptance, not observed execution.

The first callback holds already-owned TX bytes. Its entered flag is published
under the hold mutex; the guest waits for that flag and verifies both used
indices remain zero. The condition-variable wait releases its mutex and has a
five-second deadline, below the production TX-retirement deadline. It neither
holds a RAM window nor creates guest-memory authority. The callback still returns
through the production PacketPort completion channel and normal queue owner.

For the other-queue case, reaching the guest release port requires RX disable
and readback to finish while TX remains held, with the TX queue still ready and
used index zero. The callback's first loopback receive must then be rejected.
A second packet uses the same TX queue and a newly configured RX queue; exact
RAM checks bind old-RX non-effects, two distinct TX completions and fresh RX
completion. For the power case, only the existing `REQUEST_POWER_POLL` hook
releases TX. The preceding power-port service has already called device quiesce,
and resident scalar OUT commitment precedes that poll. The test therefore
forces S5 entry while the first native callback is held, rejection of its later
RX arrival, and accepted TX completion before successful shutdown. Exact
post-OUT RIP/R15, whole 384 KiB RAM, ordered packet/release/closed trace and
clean teardown constrain the result. No guest completion or IRQ is fabricated
by the fixture.

One nonblocking metadata correction was reported before freeze: both active
cases issue zero RX QueueNotify writes, whereas the generic `rx_notifications`
field defaulted to one. The effect oracle does not depend on that field; record
zero for these cases. Source acceptance does not establish live success,
backend failure behavior, same-queue active reset, SMP scheduling, real network
connectivity or performance. The guest-control case specifically tests disabling
the idle RX peer queue while TX is active; the power case tests one accepted TX
transaction, not arbitrary concurrent traffic.


### Active network TX lifecycle — independent fresh-build results

Independent `/root/io_scope_review` verified and reproduced both accepted active
TX schedules. Evidence is retained at
`/Users/liaozping/orca/natemu-raw/io4-active-20261108-01/replica-01`.
All 915 original `build-01` hashes and all 915 `oracle-02` hashes were checked;
the sole difference is the reviewed RX-notification metadata correction in
`tools/run_virtio_net.py`, consistent with `binding.json`. Original and final
manifests and binding hashes are retained. The original manifest SHA256 is
`ee04c270ab5c27e59de128f38919e453efa7d21109c785bb638abca3f5a4bae3`;
the oracle/final manifest is
`2326d32f53a0a2f05f4127db95665cbda4fcaca7a73fd4bcbd907086f7492b24`.
No reviewer production or fixture edits were made.

A fresh target built the driver and resident from copied oracle02 sources plus
sibling rust-vmm using locked/offline Rust 1.97.0 release builds. Host signing,
resident stack audit and the seven-guest/111-negative-control fixture self-check
passed. Driver SHA256 is
`7d0ed4413b57717c6e15e0aaa8a0586642707925891477758d342cca0b0c8206`;
resident is `2eac27aab329574ef99357109e90c0a113af40456fb58726ee59fde86b718fd6`.
The existing pinned FEX artifact was verified, not rebuilt. Source hashes were
rechecked unchanged after building and running.

Exactly two N1 VMs ran serially under the shared nonblocking lock, bounded to
120 seconds each, with no retry. Both passed on their first attempt:

- `active-other-queue`: exact trace shows first TX held, guest release after RX
  disable/readback, rejected first loopback RX, then a second TX with accepted
  fresh RX, and closed endpoint. Independent inspection of all 393,216 RAM
  bytes matches the expected image. The original RX's entire 64 KiB region is
  unchanged; old-RX/TX/new-RX used indices are 0/2/1, and TX used entries are
  heads 0 and 4 with length zero. The guest reaches its expected final RIP with
  R15 zero. This observes peer-queue disable while the first callback is active,
  followed by continued use of that same TX queue.
- `active-power`: exact trace shows first TX held, release by the real power
  poll hook, rejected loopback RX after quiescence, then closed endpoint. Exact
  RAM matches, original RX remains unchanged, and TX used.idx is one with
  head 0/length zero. The guest stops poweroff at post-OUT RIP 0x10023f with R15
  zero, so the forbidden following marker did not execute. Both cases report
  complete vCPU/compiler/VM teardown.

**Evidence validity:** accepted for these concrete active-TX schedules and
independent fresh-build reproduction. The root-authored primary outcomes were
also inspected and have the same exact queue/RAM effects. Source review binds
port release to completed peer disable and power release to committed S5;
callbacks use owned bytes and return through production completion paths.
The fixture and pinned FEX remain shared inputs rather than independently
invented/rebuilt components.

**Decision relevance and limits:** closes the specified peer-queue preservation
and accepted-TX S5-drain frontend gaps. It does not qualify same-queue active
reset, SMP interleavings, terminal native callback failures, Linux networking,
real connectivity/backend privileges or performance. The corrected notification
count is metadata only and changes no compiled path. The campaign now uses
4/4 launches with approximately 172.5 MiB allocated, within 1 GiB. No further
build or VM is part of this review.


### Network tool inputs — independent static preparation review

Reviewer `/root/io_window_author` accepts the retained preparation at
`/Users/liaozping/orca/natemu-raw/io-network-tools-20261108-01` as static inputs
for later guest admission. No material preparation defect was found. This
review did not author the preparation scripts, download packages, execute guest
binaries, start a packet service or launch a VM. The independent verifier and
result are retained in `review-01/`; that later review directory is explicitly
outside the original input manifest. `verify.py` SHA256 is
`638e2f975a3603516861ecb4507c961168036aad83d5ddedae50c14b337f1127`.

The reviewer reran native `gpgv`: all three signatures validate against the
expected retained primary fingerprints. Copied InRelease, amd64 index, keyring
and public-key files match the earlier signed fio preparation. The index hash
and byte count match the signed InRelease entry. This is the selected signed
snapshot, not a claim of current package/security freshness. All 66 selected
archives independently match signed package names, versions, architecture,
filenames, byte counts and SHA256. All 162 Depends/Pre-Depends edges satisfy
their recorded version alternatives, and the complete package set is reachable
from the four requested roots. The recorded 49 reused and 17 fetched archives
are accounted for; no Recommends closure is claimed.

All 2016 original file/link bindings match `input-files.json`, whose SHA256 is
`ac32d7e5ac46f21e0e1986f7809b675da12817603cdacb395561109d57202700`.
An independent archive traversal reconstructed expected regular-file contents,
permissions and symlink/hardlink results. The 1937 staged root files/links match
exactly, including the two declared loader aliases and generated certificate
bundle. Source inspection confirms regular files are extracted before guest
symlinks, hardlinks precede symlinks, archive traversal paths are rejected, and
no maintenance scripts or target executables run. The copied preparation log,
package records and producer scripts remain bound by the original manifest.

The bundle is exactly 150 packaged PEM roots, ordered by path with normalized
trailing newlines, all enabled. Every root matches its archive and the bundle
hash is `714d457d580922dbf1d0be8bd35ba236a842b50b0072ae791582a19adef772a5`.
Native OpenSSL successfully parsed all 150 certificates. This explicit CA-file
policy does not reproduce a locally customized Debian trust selection or create
OpenSSL's hashed CA directory; neither capability is claimed by preparation.

An independent ELF64/x86-64 parser checked all five entry points (`iperf3`,
`curl`, `netperf`, `netserver`, `openssl`), their interpreter and DT_NEEDED edges,
content hashes and guest-relative symlink resolution. Exactly 41 objects are
reachable, with none missing and no RPATH/RUNPATH in this concrete closure.
Default Debian library-directory resolution matches the recorded targets.
Static closure does not test symbol-version loading, runtime dlopen/providers,
NSS/DNS, TLS configuration, command-line options or package post-install state.
Those remain guest admission tasks, along with real connectivity, the native
backend decision and every network/combined performance claim. Preparation
remains within its declared 128-package/512-MiB/zero-VM scope; this review adds
no runtime acceptance or public-endpoint evidence.


### Native/QEMU fio baselines — independent source review and reproduction

Independent `/root/io_scope_review` reviewed the exploratory method and runner
before execution, then freshly rebuilt native fio and replayed one QEMU pair
and one native pair. Raw evidence is under
`/Users/liaozping/orca/natemu-raw/io-fio-baselines-20261108-01` in
`replica-build-01`, `replica-qemu-01` and `replica-native-01`. No source/workload
changes, downloads or automatic retries were made by the reviewer. Root held
other builds/benchmarks while this replication ran, and the shared lock serialized
both backends. The independent runs consumed the remaining two guest launches
and two native jobs.

**Reviewed method.** The runner uses the unchanged original Linux kernel,
bootstrap and 512 MiB ext4 root. CONFIG_VIRTIO_PCI is built into this kernel;
the earlier attempted module preparation failed before a VM and its unused
outputs remain retained. QEMU is pinned 11.1.2, i440fx-11.1, TCG multi-threaded,
CPU=max, two vCPUs/2 GiB, modern virtio-blk PCI, one queue of 128, writeback and
threaded native AIO; indirect/event-index/packed/reset/discard/write-zeroes are
disabled. This still differs from natemu's MMIO device, advertised segment
limits, CPUID and one-active-request backend. Guest queue parameters and full
commands are retained rather than claimed equal.

The original admission oracle checks all 11 profile parses, genuine fio3.39,
full CRC32C bytes, clean S5, offline filesystem checks and unchanged whole image
on read-only relaunch. Native profiles differ only in filename and
ioengine=posixaio, retaining direct=1, 4 KiB, depth16, 64 MiB, CRC32C and final
fsync on the write job. The native file is recreated for each pair and checked
unchanged by the second process. The reviewer requested and verified fixes for
malformed timing-record retention, native fio.json size accounting, transitive
runner-source hashes and the native build-manifest binding before primary runs.
The frozen baseline runner hash is
`c5f2b22a343102a5b71e82eacc9c2ff26e8e190484b332ac5f9c08b9e0b63c77`.

**Independent inputs/build.** All four frozen oracle02 hashes, prepared-output
hashes and original input-manifest binding were verified. QEMU binary hash
matches the primary manifest:
`a1b97665fb9f341a65acf9b0e7dc4c7235972aae61182545ed1aacf5ffca87fa`.
Native fio was extracted into a new directory from the retained upstream3.39
archive, SHA256
`e2f4ff137061b44ceb83a55eb9ca8856fe188db6d9b00cb59f8629c9162afe0a`,
then built with default `./configure` and `make -j4 fio`. All 693 extracted source
files remain unchanged. Version/architecture checks pass; the independent
config-host.h equals the primary default-build configuration. Independent arm64
binary hash is
`7e7cd449798e9f6e6e98046ac766bcc7a06f53e1bccdc9644a0db723469ea2fc`.
Its separate binary manifest is checked by the runner. The earlier portable
primary binary remains retained but was not used in these observations.

**Observed correctness.** Both independent pairs passed first attempt, without
timeout or log limit. Each write job reports error0, 67,108,864 write bytes and
67,108,864 CRC-verification read bytes; each subsequent read job reports error0,
67,108,864 read bytes and zero write bytes. Both QEMU boots pass all 11 parses,
reach powerdown with exit0 and pass all five e2fsck phases. The replica QEMU file
hash is `5b498bf27e9f64899929ba9552e8309dc169f83539f054da507304b4f7ef86d1`;
the post-write/restart image hash stays
`e359c44c640e2a829d9477fee2c9f2ce631c5d8e76713211e2daaf1178aa3fb3`.
The replica native file hash stays
`9b2bc4dfa4867dd2c8743d1aafbb086a8f74deac185213bc54dd6dd0b06d471c`
across its pair. Cross-run generated-file equality is not required; preservation
is checked within each pair.

**Timing observations, seconds.** The ranges below are the two raw observations
(primary and independent), not confidence intervals or repeated benchmark
qualification. Job columns are fio's reported job_runtime; the write job includes
its verification phase. Do not derive a phase total by summing independently
rounded read/write runtime fields.

| Configuration | Write + verification job | Restart/read-only job | Write process wall | Read process wall |
| --- | ---: | ---: | ---: | ---: |
| QEMU TCG, two observations | 1.898–2.041 | 0.823–0.831 | 9.492–9.883 | 8.615–8.835 |
| Native arm64, two observations | 0.220–0.221 | 0.042–0.045 | 0.596–0.601 | 0.343–0.346 |
| Natemu matching primary pair, one observation | 68.928 | 40.661 | unavailable separately | unavailable separately |

The natemu admission runner's recorded 125.075/97.897-second durations include
post-exit image hashing and fsck, so they are whole-phase durations rather than
strict process-wall equivalents. Its full byte/CRC/S5/fsck checks also pass.
These finite observations show natemu substantially slower on the tested
configuration; they do not isolate why. Both guest configurations report about
99.908% at requested fio depth16. Native reports only 15.277–15.323% at depth16
for the write/verify job and 7.208–7.489% for the read job, spending most operations
at depth8. Guest fio depth is not native-backend concurrency.

**Source comparability limits.** Retained native source implements direct mode
through F_NOCACHE (`os/os-mac.h`), and its enabled POSIX-AIO sync path calls
`aio_fsync(O_SYNC)`. Natemu's block sync calls fcntl command51/F_FULLFSYNC;
QEMU's threaded raw-file flush calls `qemu_fdatasync`, whose upstream implementation
uses fdatasync when configured and fsync otherwise, rather than F_FULLFSYNC.
See [QEMU raw-file flush](https://github.com/qemu/qemu/blob/v11.1.2/block/file-posix.c#L1601)
and [the sync wrapper](https://github.com/qemu/qemu/blob/v11.1.2/util/osdep.c#L581). Native APFS/arm64/posixaio, guest
ext4/x86/io_uring, CPU=max versus natemu's CPUID profile, cache behavior, segment
limits, transport and backend concurrency all differ. The native destination is
on the host Data volume, confirmed APFS by mount output. No host caches were
purged, no cold-storage or sustained-SSD claim is made, and process times include
startup. CRC work itself can use different ISA implementations. The native
source build is independent; QEMU, guest image/preparation and the reviewed
runner are shared retained inputs.

**Evidence validity/relevance:** accept these bounded correctness-passing
end-to-end baseline observations and their independent reproduction. They fill
the requested missing reference/native measurements and motivate targeted
profiling; they are not product-wide performance qualification or causal
attribution to copying, invalidation, emulation, engine or storage alone. No
connectivity/backend-privilege work occurred. The campaign is exhausted at
6/6 guest launches and 4/4 native jobs; cumulative allocated storage measured
approximately 1075 MiB, within3 GiB. Further runs require a new bounded assignment.

### Selective DMA mapping invalidation: independent source/method review — November 8, 2026

**Scope and disposition:** accept the candidate for the bounded, uninstrumented
execution campaign; no remaining substantive source blocker found. This is a
source review, not an independent runtime or performance result. The author
reports candidate builds, focused tests and initial ordinary/PTE/PDE passes;
independent reproduction remains assigned separately.

The review followed the actual fetch, data-fault and segment/helper walkers
(`runtime/src/execution.rs` and `execution/segments.rs`) into
`GuestTables::read`. All three production consumers use `CodeWrites`, whose
required `CodePages::table_read` hook now marks the physical page before loading
an entry. Failed and repeated reads also mark; no current host-side page-table
walker bypass was found. The host compiler consumes resident-resolved fetch
GPAs. This coverage must be preserved if another walker is introduced.

The global bitmap is conservative and monotonic for the VM lifetime. It covers
the physical span, including high RAM, rather than compact installed-RAM indices;
cache/shadow resets and physical-page reuse do not erase dependencies. The host
checks it only after every exact resident acknowledgement and the one-use permit
grant. Every write still publishes `written` and physical code invalidations;
only table-page overlap publishes `tables_written`. Residents drain code posts
and, where required, invalidate global fetch currency before acknowledging a
later request or re-entering guest code. Keeping shadow reset before each
acknowledgement preserves existing split-access progress and data mapping
coherence. Cancellation before grant cannot publish either write epoch. A later
successful epoch cannot overtake an unconsumed prior write on one resident,
because it needs that resident's new acknowledgement. Monotonic epoch values
avoid losing an owed table invalidation across canceled or adjacent windows.

The appended bitmap and window field leave compiler-visible code/lookup/global
virtual bases and existing mask/record offsets unchanged. Host backing offsets,
stride and mappings derive from the enlarged global extent; layout assertions
bound it. FEX slice-one exits enter the resident linker/dispatcher and do not
address the appended state. Host and resident must be rebuilt together; this
review does not claim arbitrary mixed-version ABI compatibility.

**Resolved method defect:** the first helper revision required zero global
mapping flushes for PTE/PDE fixtures as well as ordinary writes. That would reject
correct intentional mapping invalidation. The reviewed correction makes
`ordinary_writes=True` explicit only for the ordinary/code-write preservation
fixture. PTE/PDE fixtures retain their actual changed execution/data-route
oracles and no-full-code-cache-flush checks. The focused source tests additionally
cover mark-before-load, failed/repeated reads, a page-crossing write, a mark
published by the final participant, read windows, page reuse and canceled grant.

**Relevance and limits:** selective invalidation removes a demonstrated source
of unnecessary global currency/L1 work without relaxing copy-first quiescence or
source-code coherence. It does not eliminate read-window shadow resets, and
historical table-page reuse can conservatively reduce its benefit over time.
The original complete fio write/CRC/relaunch workload, durability policy,
timeout and correctness oracle remain unchanged. The declared campaign is
bounded to 16 VM launches and 4 GiB, with isolated instrumentation and independent
fresh-build replay. No speedup or causal fraction is accepted from source review;
unchanged-workload final binaries and independently checked effects are required.

### Retaining ordinary DMA shadows: independent source/method review — November 8, 2026

The second candidate removes unconditional shadow reset from `io_checkpoint`,
retains the full barrier before its exact acknowledgement, serves only
`take_shadows`/`reset_shadows` while release is withheld, and unconditionally
drains invalidations afterward. Tracked table writes still invoke global
translation invalidation. Source review finds no substantive blocker: parked
residents cannot access guest RAM, ordinary physical writes do not change their
mapping routes, and holding/writable bits remain accurate until an actual reset.
A peer asking after the acknowledgement can therefore finish its split or code
mark operation and acknowledge the window. An ask racing release is handled by
the post-release drain or the existing holding/kick/reset handshake. No generated
continuation may be live at this checkpoint, as before.

The proposed isolated host-only late-SHADOWS control is accepted in principle,
pending inspection of its concrete source/oracle. After all exact acknowledgements
and before grant, it records reset counters, posts real SHADOWS requests, and
withholds release until counters advance and the request bits clear. Retain the
epoch, all acknowledgement values, unreleased state and before/after counters;
check cancellation and the existing finite admission deadline. Use the same
instrumented host with a resident differing only by removal of the parked
service block for the negative control. That run must show the named late-SHADOWS
admission timeout, no copy/write publication, release and joined teardown;
a generic watchdog timeout is not evidence of the intended dependency. This
control observes the actual shared protocol and does not require guest-RAM
access, a fabricated reset count or a production testing API.

No build, VM or performance run was performed by this reviewer for this stage.
The first selective-invalidation replica was deliberately held before copying or
compilation while the author evaluates the next candidate. Ordinary/PTE/PDE,
reset/network and full unchanged fio acceptance still require final uninstrumented
binaries and independent retained-source reproduction. Source acceptance does
not establish that the remaining performance gap has been resolved.

The concrete late-SHADOWS control was reviewed before launch at
`io-perf-repair-20261108-01/late-shadow-build-02`, with wrapper
`/private/tmp/natemu-late-shadow-run.py`. An initial combined cancellation/deadline
branch could label cancellation as the intended timeout; it was corrected before
execution to record both causes, with the negative oracle requiring
`canceled=false deadline_expired=true`. The final source checks reset advancement
and cleared request bits while release is withheld. The wrapper requires the
unchanged healthy-window oracle for positive, preserves the negative raw failure,
and produces a separate expected-negative verdict checking the exact admission
error, no service event, no outer timeout and joined teardown. Accepted for two
bounded control launches. Bindings must retain both producing source manifests
and the isolated diff: the inner runner's candidate04 source manifest alone does
not describe the injected host binary. This paragraph records pre-execution
method acceptance only.

### Bounded acknowledgement spin: independent source/method review — November 8, 2026

The candidate adds one 30-microsecond spin budget after the host posts all kicks.
Every acknowledgement-poll iteration still checks cancellation and the original
admission deadline before requiring exact acknowledgements from every participant;
the one-use grant CAS remains after that loop. Once the budget expires the owner
yields, and coordinator-lock contention continues to yield. No substantive source
blocker found. This is a bounded scheduling policy candidate, not an established
optimal duration or a high-vCPU-count performance result.

Accept the proposed unchanged original fio comparison with `/usr/bin/time -lp`
applied to the same whole runner for both spin-on and spin-off pairs. Report
user/system CPU and fio job durations separately: runner CPU includes boot,
children, hashing and fsck and is not the device owner's isolated CPU cost.
Keep byte/CRC/durability/relaunch checks unchanged, serialize runs, retain failures
and independently rebuild/replay the selected final candidate. Source review
alone does not establish improved efficiency, and short owner-stack samples are
a hypothesis for this policy rather than a measured causal fraction. No build or
VM was executed by this reviewer during the comparison.

### Owned descriptor-prefix reuse: independent source review — November 8, 2026

The concrete `Snapshot`/owner change is accepted for bounded testing. It reuses
only already-captured metadata while its available index remains ahead of
`next_avail` in the same queue/device incarnation. Exhaustion or identity change
forces a new capture; Empty, Canceled and Invalid dispositions discard the cache.
Each subsequent chain still receives complete checked traversal and physical
range validation. Payload capture, backend submission, completion publication
and the fresh post-publication suppression read remain unchanged. Queue geometry
is stable under the existing ready/active register-write guard; disable/reset
advances checked generation counters. The owner still admits only one request
and checks quiescence before selecting another, so prefetch does not expand the
S5 accepted-work frontier.

[Virtio 1.2 §3.3.1](https://docs.oasis-open.org/virtio/virtio/v1.2/virtio-v1.2.html)
prohibits changing exposed, not-yet-used queue entries; §4.2.3 prohibits queue
geometry access while QueueReady is nonzero. Permitting metadata prefetch is an
inference from those ownership rules and the absence of a required reread, not
an explicit normative endorsement of this cache. A prior completion aliasing
later captured descriptor bytes retains the captured interpretation; freshly
captured payloads and per-request validation still bound effects. Document this
prefix-snapshot interpretation explicitly rather than attributing the precise
alias outcome to the specification.

The new unit tests cover captured-prefix use, exhaustion/new publication,
incarnation mismatch/discard, malformed later traversal and index wrap. The
`live_table` clone test checks owned-snapshot semantics; it does not execute an
actual DMA alias schedule or prove owner notification progress. A focused live
valid-first/malformed-second publication is recommended to verify reuse through
the production owner with one backend effect/used entry, NEEDS_RESET and untouched
second destinations. Existing fullqueue, reset/disable and network interleaving
checks remain relevant. No build or VM was run by this reviewer for this change;
no performance benefit is yet accepted.

The added live `malformed-later` lifecycle fixture was independently reviewed
before execution. It exposes valid read head0 and malformed read head3 together;
head3 contains an out-of-RAM suffix after a valid writable prefix. The guest
requires used index1, first status0, NEEDS_RESET/IRQ3 and untouched second status,
then resets. The host oracle requires exactly one native call and commit, the
valid 512-byte read, complete 128 KiB expected RAM, whole unchanged disk, exact
terminal RIP/assertion marker and joined teardown. This is accepted as a bounded
production-owner check of later captured-head validation. It does not force a
live metadata alias or independently observe which snapshot path was taken;
that linkage comes from the reviewed owner source. The author reports assembly
and synthetic oracle checks; this reviewer has not executed the case.

### I/O performance repair: final independent rebuild and replay — November 8, 2026

**Disposition:** accept candidate06's bounded correctness and measured improvement,
with no unresolved source blocker found. This does not establish completion of
the broader performance objective, native networking or combined workload scope.
The selected production change comprises monotonic table-dependency tracking,
servicing shadow-drop requests while preserving otherwise valid parked mappings,
and owned descriptor-prefix reuse. The 30-microsecond host spin experiment is
excluded: its faster fio job came with worse complete-run wall and CPU cost.

**Independent reproduction:** all 916 frozen inputs from
`io-perf-repair-20261108-01/candidate-build-06` were hash-verified, copied to
`replica-build-01` and rebuilt into a fresh target. Separate offline/locked Rust
1.97 release builds produced the normal CLI, testing driver and uninstrumented
resident; both host binaries were signed and the resident stack audit passed.
No source or oracle was edited for the replay. The source manifest SHA-256 is
`fc83da9133e682b63801689c9045cac352f17d1954d909e614706f4e24f3f3c7`.
Final artifact hashes are:

- Normal CLI: `69589dd9fd3ec7218b35c01fe35b3a7a78144d201985fdd09faa1eb8c3a75250`.
- Testing driver: `20abda81939c597109f8158d165861b65925de7cdcc739e822c9c587c16f5211`.
- Resident: `6f59db8b88a573f20233efb3e8497cb2671cebd81f3ebd1a69628a535fddce79`.
- Shared pinned FEX: `3ccb420ccc8d4dc0b6f81366985ddedbbfe68e52f69a8d591f586dbe82d9d2f4`.

Exactly four independently launched VMs passed first attempt, without retries.
The N2 PTE/PDE cases each completed 64 snapshots and 72/71 total windows, changed
code result from `0x1122334455667788` to `0x8877665544332211` on both CPUs, and
preserved progress. The PDE case also changed the data route to the expected
value. Each CPU reported three global mapping invalidations, one initialization
full-cache flush and one killed patched block. This preserves intentional mapping
invalidation while retaining unrelated code. The test warms relevant mappings
but does not observe exact hardware TLB residency at the final write instant.

The original fio oracle (`7285f523371fc07389618d47733cc153637a9e90f046c02c86950218fda3953c`)
and original prepared image were reused, with their transitive sources/tool/input
hashes verified. The write boot wrote and CRC-verified exactly 67,108,864 bytes;
the restart boot verified that amount with zero writes. Both report fio error 0,
16,384 reads, and 99.908447% achieved depth 16. All eleven profile parsing checks,
library checks, clean S5 and offline e2fsck pass. The persisted file hash is
`27be87e98cb8f76a0bd1a5e60fc5f785e406314630cba2415700f85da5d8d88a`;
the complete image hash remains
`4b3823b04c57a5b00f9bc2138c6a7c9ff3632c7b9aa023abfd2abee90dff9ccc`
across read-only relaunch. File hashes need not match another write run: fio
creates the verified file anew; persistence is checked within each pair.

| Observation | Write + CRC job | Inline verification read | Restart verification job | Complete write/verify phases |
| --- | ---: | ---: | ---: | ---: |
| Primary candidate06 | 3.122 s | 1.004 s | 5.399 s | 35.718 / 28.970 s |
| Independent candidate06 | 3.050 s | 1.143 s | 1.184 s | 36.256 / 33.487 s |

Independent write-only runtime is 1.906 seconds; restart read runtime is 1.183
seconds. `/usr/bin/time -lp` around the entire original pair reports 70.56 seconds
real, 111.55 user and 18.87 system (130.42 CPU seconds). These include boot, child
processes, hashing and fsck; they are not isolated device CPU costs or strict
per-VM wall times. The independent complete verification phase is longer despite
its shorter fio job. The primary's large inline-versus-restart read difference
is mostly absent in the replica. Placement, warm execution, scheduling and
storage explanations remain hypotheses; these two observations do not isolate
the cause or justify selecting only the faster restart result.

The write/CRC job improved from the retained 68.928-second baseline to 3.050–3.122
seconds, about 22.1–22.6 times faster for this finite workload. Against earlier
QEMU observations 1.898–2.041 seconds it is still about 1.49–1.64 times slower.
Restart verification ranges 1.184–5.399 seconds versus QEMU 0.823–0.831 seconds;
that variability prevents a stable near-parity claim. The earlier documented
CPU/transport/backend/cache/durability differences remain, including natemu's
F_FULLFSYNC and the reference's weaker flush path. These are exploratory complete
correctness-passing observations, not a broad or sustained-storage score.

**Additional evidence reviewed, not independently rerun here:** the late-SHADOWS
positive control records exact epoch 1 acknowledgements, release 0 and reset
counts `[0,0]`→`[1,1]` while release remains withheld. The isolated no-service
resident instead records the named pre-grant deadline with canceled=false,
released 1 and unchanged write epochs, and joined teardown; its raw healthy-window
FAIL remains separate from the expected-negative PASS. The author also reports
final reset/disable/fullqueue/malformed-later, contained sync failure, held compiler,
high-RAM and network lifecycle/IRQ regressions. Their shared fixtures and primary
execution remain separate from the four fresh-build independent replays above.

All source and binary hashes were rechecked after execution. Producing commands,
logs, manifests and 96 retained output hashes are in `replica-build-01`,
`replica-tables-01` and `replica-fio-01`; `review-results.json` records machine-readable
results. The campaign uses 39/40 launches and approximately 3085 MiB of 4 GiB.
No additional run is assigned. Shared guest preparation, original Python oracle
and pinned FEX limit independence; source build and the four executions are fresh.


### I/O wait-loop doorbell ordering — independent forced reproduction

`/root/io_window_author` independently built and exercised an isolated forced
schedule for both production wait loops on November 8, 2026. This reviewer
authored the earlier RAM-window foundation but neither the new production
ordering fix nor the normal fio timing run. `/root/io_scope_review` reviewed
the instrumentation and final oracle before execution. Evidence is retained at
`/Users/liaozping/orca/natemu-raw/io-perf-pipeline-20261108-01/forced-controls-02`.
No workspace production source was changed by this reproduction.

**Construction.** Both variants derive from the verified `candidate-build-01`
source and retained sibling dependencies. Four files change only in isolated
copies: ABI private observation words, host observation/release methods,
resident observation hooks, and a replacement for the test driver's existing
window worker. Across the complete old/fixed source manifests, the only differing
file is `runtime/src/execution/events.rs`, where the two wait-loop call orders
differ. Old is checkpoint then doorbells; fixed is doorbells then checkpoint.
The same bounded latch is immediately before doorbells in each variant. It
never touches guest RAM, synthesizes an acknowledgement or enters a host call.
The host runs real `copy_guest`, observes its published epoch and a real pending
GIC kick via `Stops::ringing`, and only then releases the private latch.

Both hosts/residents were freshly built offline with locked dependencies, signed
where required, and passed resident stack audits. Original pinned FEX was
hash-verified, not rebuilt. Post-run source hashes still match. Old/fixed source
manifest SHA256 values are respectively
`093a41c8feef22e1e1e9913ab647b465a5492fb5b09e5779950cddecd1e8a18f` and
`a98bbaf60446bdeae1c8fa95d25c73e86f2d38d69b7219a7c6d1f0b14fe18763`.
Binary bindings, exact commands, patches, generated programs and output hashes
are retained. An earlier preparation script rejected an incorrect source anchor;
`forced-controls-01` retains that failure, with zero builds/VMs. The corrected
source derivation is distinct. A pre-execution oracle field-format correction
was also completed before freezing the final runner.

**Method.** Four N1 launches cover old/fixed HLT and old/fixed firmware-mailbox
wait, under the shared VM lock and 120-second per-process limit. HLT uses CLI,
HLT and a forbidden following register write, with no timer/interrupt setup.
The mailbox case starts the sole vCPU directly in the production firmware wait
path with a zero command/vector page; it is not an SMP AP-startup test. Private
counters record acknowledged doorbells, wait-path entry and return. The old-order
oracle requires pending epoch 1, acknowledgement 0, unreleased/unwritten state,
a real kick changing from pending to cleared, and wait entry without a recorded
return. These must hold at two observations separated by a 100-ms host delay,
while the copy thread remains unfinished. Its only accepted failure is the
normal 700-ms I/O acknowledgement deadline. The host read buffer must remain
0xee, and full post-teardown guest RAM must retain the initial 0xa5 byte.

Fixed-order cases use the same forced publication/kick schedule and require a
successful real copy: the read returns 0xa5, acknowledgement/written/released
all reach epoch 1, and guest RAM contains 0x5a. Every case checks the full
0x308-byte target region; mailbox cases also check all 4096 mailbox bytes stay
zero. Precise post-HLT or never-executed mailbox RIP, the untouched forbidden
register, normal halted stop, intact frame/link checks and joined VM/compiler
teardown are required. Thirty-six negative parser controls passed before launch.
The final runner SHA256 is
`585a5d365e75a2cdac79b6c7fabc94e0cf8b4c19c8fd5c782a36d09ce38ba7f3`.

**Outcome: reproduced and corrected in the forced schedule.** All four cases
pass on their first launch, without retry, process timeout or hook timeout.
Both old cases observe exactly one drained kick and one wait entry with zero
returns, remain unacknowledged at both positive observations, then report the
specific acknowledgement timeout with no copy effect. Both fixed cases grant
and publish the exact expected bytes. All four terminate `halted status=1
detail=32`, with RIP 0x100002 for HLT or 0x100000 for the unstarted mailbox
vCPU, and clean teardown. A later end-of-test kick may legitimately acknowledge
the already released old epoch; the pending/final worker records precede that
teardown kick and distinguish it from a successful copy.

**Limits and decision relevance.** Pre-wait/return counters establish wait-path
entry without an observed return, not instruction-level WFI PC sampling. This
adversarial schedule demonstrates the lost-doorbell mechanism and that checking
the request after draining its wake fixes these concrete paths. It does not
measure the race's natural frequency, establish general SMP firmware behavior,
or isolate its contribution to fio performance. Four reserved VM launches were
consumed; the retained fixture is approximately 147 MiB. The exclusive build/VM
slot was released before independent normal timing resumed.

### Lost I/O wake: independent source review, control review and replay — November 8, 2026

**Disposition:** accept the two wait-loop ordering changes and their finite
correctness/performance evidence. The only differing input among the 916 frozen
candidate01 and prior candidate06 source manifests is
`crates/runtime/src/execution/events.rs`: HLT and firmware-mailbox loops now drain
doorbells before checking the I/O request. No metadata batching, payload pipeline,
extra backend concurrency, spin policy or guest-memory authority change entered
this candidate. There is no unresolved substantive review blocker.

**Mechanism:** old ordering can check an empty request, consume a newly posted
request's sole kick, then enter its wait without acknowledging the request.
New ordering checks the work after draining its wake. A kick arriving later
remains pending to wake the wait. The audit found the relevant WFI sites in
`lapic::wait` and the mailbox loop; the LAPIC helper does not drain the GIC.
Deferred MMIO and power waits poll through HVC rather than draining then sleeping.
No guest-memory access moved before the checkpoint.

**Forced evidence, independently reviewed but executed by the separate fixture
author:** `forced-controls-02` retains four source-derived, instrumented controls.
Old/fixed variants differ only in the checkpoint placement around identical
private-metadata latch/drain hooks. The host starts real `copy_guest`, observes
request 1/ACK 0/release 0 and a pending GIC kick before releasing the latch. For both
old HLT and old mailbox, the kick then becomes nonpending, the drain and pre-wait
counters increment, and two observations including a 100 ms recheck show no wait
return or acknowledgement. Both hit the specific 700 ms ACK deadline, release 1
with written 0/ACK 0, unchanged destination and untouched read buffer 0xee. Fixed
HLT and mailbox acknowledge 1, return the original byte 0xa5, write 0x5a and release
successfully. Complete target memory and the all-zero AP mailbox are checked;
all four end with exact `halted status=1 detail=32`, expected RIP, unchanged
forbidden marker and joined teardown. Source/oracle hashes and actual records
were reverified. The earlier preparation-anchor error is retained with zero VMs.

These controls reproduce the consumed-wake path, not a statistical incidence or
performance sample. Pre-wait/no-return counters do not sample an exact WFI PC;
the AP0 fixture tests the firmware-mailbox path on one vCPU, not SMP startup.
The standalone control oracle's 36 synthetic negative checks are author evidence.

**Independent build and execution:** candidate01's 916 inputs were hash-verified,
copied to `io-perf-pipeline-20261108-01/replica-build-01` and built into a fresh
target with separate offline/locked Rust 1.97 normal-CLI and uninstrumented-resident
release builds, signing and resident stack audit. The source-manifest hash is
`a6bc68fbc8d62901a1a0bc1b54d258c5f02f1a1ee9d47be411a41903a89b51f8`;
normal CLI is `fdacd5a29cd647f87835a9c58e0aa76867c40b431ed2cd8fc1dc3e990ce72441`,
resident is `33d7b6fe80d42112d95406212bd38089782214cc380a4af3b18915595cba2bf4`.
The pinned FEX, original prepared image and original fio oracle remain unchanged.
The contemporaneous old baseline uses the previously independently built
candidate06 binaries, whose producing source and binary hashes were rechecked.

Exactly four independent VM launches ran serially under the shared lock: the
old pair followed by the fresh fixed pair. Both pairs passed first attempt,
without retries: all profiles/libraries, exact 64 MiB write and CRC readback,
64 MiB restart read with zero writes, fio error 0, clean S5/e2fsck, persisted file
hash and byte-identical read-only image. Each read has 16,384 I/Os and achieved
depth16 is 99.908447%. The final fixed persisted file is
`5c46ac543b65173f0953069e50400f65dbda262ea516fa344e32677e01376c8f`;
the unchanged restart image is
`7932aaf071b43ca6b287b34427d6cf9276932ffcbabf31cd988a946b16ef39f8`.

| Observation | Write + CRC job | Restart verification job | Full write/verify phases | Whole pair real / CPU |
| --- | ---: | ---: | ---: | ---: |
| Contemporaneous retained old baseline | 4.072 s | 10.326 s | 76.970 / 26.450 s | 104.24 / 198.33 s |
| Fixed primary | 1.619 s | 0.989 s | 9.113 / 7.998 s | 17.97 / 25.02 s |
| Fresh fixed independent | 1.662 s | 0.992 s | 9.152 / 7.973 s | 17.94 / 24.84 s |

Fresh fixed inline readback takes 0.542 seconds and writing 1.119 seconds; restart
read runtime is 0.990 seconds. Its whole wrapper records 22.45 user and 2.39 system
CPU seconds. The identical `/usr/bin/time -lp` wrappers include boot, hashing,
postprocessing, offline checks and child processes, not isolated I/O CPU cost.
No competing VM/build ran during the timing pairs.

The contemporaneous pair shows about 5.81 times lower complete wall time and
7.98 times lower CPU time. The old baseline varies substantially from its earlier
65.55/70.56-second observations; the fixed primary and independent observations
agree closely, but two fixed samples and a fixed old→new run order do not
establish a stable general speedup distribution. Earlier QEMU write/CRC jobs
were 1.898–2.041 seconds and restart reads 0.823–0.831 seconds. The fixed write job
is faster in these finite observations, while restart verification is slower;
previous filesystem/CPU/device/durability qualifications remain. This is not
product-wide QEMU superiority, an SSD score, or proof that every earlier delay
was this race. Broader workloads and native networking remain separate work.

Nine additional normal-driver regression results were inspected, not rerun by
this reviewer: HLT/AP, N4 split/cache preservation, reset, disable, power with
halted peer, malformed-later, fullqueue and network halt/IRQ all pass. Source and
binary hashes were checked again after independent execution; 179 output hashes,
`review-results.json`, build commands/logs and the exact production diff are
retained beside this review. Campaign accounting is 21/24 VMs, four launched by
this reviewer, with approximately 1362 MiB of the 4 GiB cap at review time. No more
VMs are assigned. Shared guest preparation/oracle/FEX limit independence; the
fixed source build and four comparison executions are fresh.


### Block completion rearming — independent forced regression controls

`/root/io_window_author` authored and ran six isolated N1 controls for the new
completion-time avail-index observation and retirement logic. The production
increment was authored by `/root`; `/root/io_scope_review` reviewed the hooks,
complete oracles and final narrow revision before execution. This fixture author
previously authored the RAM-window foundation, but not this queue-retirement
change. Final evidence is retained at
`/Users/liaozping/orca/natemu-raw/io-perf-notify-20261108-01/forced-controls-03`.
No workspace production source was edited for these controls.

**Construction and provenance.** Four isolated variants derive from the verified
`candidate-build-01` source and sibling dependencies: production behavior with
observation hooks, plus three explicit counterfactuals. The counterfactuals
respectively replace notification-preserving OR with assignment, remove the
owned-prefix fallback, or force the completion's pending result false. They do
not change native data buffers, queue validation, copy windows or guest replies.
The same guest program is used for each positive/negative pair. New test-control
ports release metadata-only holds; all queue publication and notifications are
ordinary guest instructions. Hooks log fresh captured indices, owned snapshot
indices and retirement inputs. No hook accesses guest RAM directly.

A post-copy hold is before retirement/IRQ publication; the pre-copy hold is
before the completion window grant. Neither holds `Virtio.state`; the isolated
observer clones its Control Arc and releases `disk.control` before blocking.
Owner-idle observations are made under the actual state lock and require no
active request/notification, equal next_avail/next_used, the same live identity,
a ready queue, DRIVER_OK without NEEDS_RESET/failure, and no closing, stopped or
quiescing state. Its trace is appended before the phase becomes guest-visible,
within the metadata gate. The guest records the observed phase before sending
any separately traced rescue notification.

All four hosts built and were signed; one fresh shared resident passed its stack
audit, with identical resident/ABI inputs verified across variants. Existing FEX
was hash-verified rather than rebuilt. All producing source hashes were rechecked
after execution. The good source-manifest SHA256 is
`2b0180cfe8b0bcff75077c7cacb809b45ae61acf081b0d94311e8f681411d8ba`.
Final runner SHA256 is
`f2cce8ae55eb9afbf5924255a427180f161ced639e97c1fdb9210d8e91cfb4d5`;
51 negative parser controls passed. Exact commands, variant patches, manifests,
guest assembly, full expected bytes and result hashes are retained.

**Schedules and observed distinctions.** All six VM launches pass on their first
VM attempt, without process/hook timeouts or retries:

- After-capture notification: the first completion observes avail.idx 1 and
  pending=false, then pauses. The guest publishes head 2 and QueueNotify before
  retirement. Both variants record `retire:1:false:true:false`: no fresh-index
  work or owned successor, but a real intervening notification. Production
  drains head 2; the assignment counterfactual records idle at index 1 before
  the traced rescue notification permits that request to run.
- Owned-prefix alias: the first snapshot owns two heads. Its first IN payload
  aliases live avail.idx to 1, so fresh pending is false while the captured
  prefix still contains head 2. Both record `retire:1:false:false:true`.
  Production drains the owned successor; the no-prefix counterfactual reaches
  idle 1 and needs the explicit rescue. The second aliased payload sets live
  avail.idx to 2, allowing a clean exhausted-queue observation afterward.
- Before-capture publication: the guest publishes head 2 during the pre-copy
  hold without a second QueueNotify. The completion sees avail.idx 2 and
  pending=true. Production records `retire:1:true:false:false` and drains it;
  the forced-false counterfactual records `retire:1:false:false:false`, reaches
  idle 1, and requires rescue. This tests this implementation's fresh-index
  guarantee, not a general permission for virtio drivers to omit notification.

Every positive reaches idle 2 without rescue. Every negative must positively
record idle 1, then the rescue marker and actual QueueNotify, before the second
native read; a timeout or unexplained lack of progress cannot pass. All six then
publish a third request after the observed idle decision and complete it through
ordinary notification. This checks publication after idle, not that the owner
had necessarily entered its native condition-variable sleep.

Each case performs exactly three successful 512-byte native reads at offsets
0, 512 and 1024, retires the three expected used entries/statuses, and retains the
same device/queue incarnation in idle observations. The complete 128-KiB guest
RAM image and whole 2-MiB disk match their byte oracles; the latter remains
unchanged. Full post-VM records agree with normal reports, guest checks finish
normally, and all vCPU/compiler/VM teardown checks pass. Final queue disable and
IRQ acknowledgement are ordinary guest accesses after idle 3.

**Corrections and accounting.** `forced-controls-01` was built but never run:
review found that its idle phase preceded the trace append. Separate revision
02 fixes that ordering. Its first process invocation was rejected by Clap's
unchanged test-schedule allow-list before VM creation; command, stderr and
failure remain retained. Revision 03 changes only that test-driver allow-list
relative to 02, and its runner changes only the retained-root path. Guest inputs
and oracle logic remain identical. All new modes were verified in native help
output and the narrow delta reviewed before execution. Accounting is six actual
VM launches plus one rejected CLI process with zero VM launches; the unrun and
help invocations add no VM launches. The exclusive slot was released immediately
after the six results for independent normal timing.

**Decision relevance:** these controls support the three specific rearming
premises and demonstrate that each counterfactual loses work until rescue. They
are finite N1 correctness evidence, not performance samples, arbitrary alias
liveness guarantees, SMP/reset coverage or a replacement for the separate
integrated fio and reverse-order fresh-build comparison.

### Completion-index scheduling — independent review and reverse replay, November 8, 2026

**Disposition:** accept this small scheduling change and its scoped correctness
evidence. No substantive source or reproduction blocker remains. It removes an
unnecessary empty queue scan when a block completion observes no further work;
it does not change backend concurrency, request admission, guest-memory authority,
durability, reset or S5 ordering. Write/CRC job improvement appears in both run
orders, while the evidence does **not** establish a substantial whole-workflow
speedup or a reliable restart-read improvement.

**Source:** the 916-entry frozen candidate differs from the prior wake-order
candidate only in `host/src/virtio/{mod,block,net}.rs`. Block completion reads
four available-ring bytes after all payload/status/used publication writes in
the same granted window. Flags retain their existing IRQ meaning; the index is
compared against wrapped `next_avail + 1`. The actual owner calls `retire_request`,
which retires once and OR-preserves both a late notification and an already-owned
snapshot successor under the existing incarnation, ready, status and quiescing
gates. Reset retirement follows. RNG/network retain their previous rearming
behavior. The two new unit tests cover 16 truth-table combinations, seven lifecycle
gates and one stale snapshot; their temporal coverage is supplied separately below.
Notification progress assumes conforming queue ownership. The code does not
promise liveness for arbitrary aliases that corrupt device-owned `used.flags`.

**Forced schedules:** the separate fixture author executed all six cases in
`forced-controls-03`; this reviewer inspected source, hooks, oracle and actual
records independently, and reconstructed the complete final 128 KiB declared
RAM from initial memory plus the three native disk sectors. All exact effects
match. Each case performs exactly three native reads and three committed used
entries, retains an unchanged 2 MiB disk, and joins/destroys cleanly.

The first schedule publishes and notifies after the first completion observation
but before retirement, then publishes again after the second idle retirement.
Replacing OR with assignment strands the second request. The second schedule
captures two heads whose input payloads overwrite the live available index;
removing the captured-prefix fallback strands the successor. The third publishes
before the completion window; forcing pending=false strands work that the fresh
index detects. Each negative records actual owner-idle at index one with the
correct incarnation and no active, notified, closing, stopped or quiescing work,
then a separately traced rescue notification; positives reach idle two without
rescue. Exact event order, hook timeout rejection, guest markers, IRQ/status and
full memory checks are enforced. The third case tests this implementation's
fresh-index guarantee, not permission for arbitrary drivers to omit required
notifications. These are single-vCPU instrumented correctness controls, not
performance samples or exhaustive schedule exploration.

The reviewer caught a trace-order race: phase publication preceded recording
owner-idle. It was corrected by recording the event before exposing the phase
under the same control mutex. `forced-controls-01` was built but unrun. The first
`forced-controls-02` process was rejected by the CLI allowlist before any VM was
created; that command/error is retained. The isolated `-03` correction adds only
three testing mode names to the CLI allowlist in each variant; guest programs,
oracle logic and expected effects remain unchanged. All six actual VM schedules
passed first launch, with no hook timeout. The 51 synthetic oracle controls are
author evidence, not independently rerun by this reviewer.

**Independent production reproduction:** all 916 candidate inputs and the prior
baseline source were hash-verified. A fresh target built the ordinary CLI and
uninstrumented resident separately, offline and locked with Rust 1.97; signing
and stack audit passed. The producing manifest is
`62669f3c8ad72f19febc5d0706e4fdb110a39afa4ad113fd57aa4cee7fca36bd`.
Fresh CLI SHA-256 is
`4c0a8b7f3ffd8750773e53a13474d2b77b783c5b93c0c7b8c35b42a2980319e1`;
resident is unchanged
`33d7b6fe80d42112d95406212bd38089782214cc380a4af3b18915595cba2bf4`.
Original prepared inputs, fio oracle and pinned FEX were reverified unchanged.

The author ran old→new; this reviewer ran new→old under the shared VM lock,
with no competing build or VM during timing. All four independent launches
passed first attempt, no retries: exact 64 MiB writes/readback, 64 MiB restart
read with zero writes, CRC, library/profile checks, persisted file identity,
clean S5/fsck and byte-identical read-only whole-image restart. Each read has
16,384 I/Os; depth16 remains 99.908447% in the independent jobs.

| Observation | Write + CRC job | Restart verification job | Whole pair real / CPU |
| --- | ---: | ---: | ---: |
| Author baseline, first | 1.653 s | 0.568 s | 17.27 / 24.40 s |
| Author candidate, second | 1.356 s | 0.935 s | 17.37 / 23.85 s |
| Independent fresh candidate, third | 1.393 s | 0.883 s | 17.15 / 23.55 s |
| Independent baseline, fourth | 1.894 s | 0.925 s | 17.62 / 25.08 s |

Fresh candidate full write/restart phases are 8.589/7.746 seconds; baseline
phases are 8.893/7.912. Fresh candidate writing takes 0.916 seconds and inline
readback 0.476 seconds; restart read runtime is 0.882 seconds. Baseline values
are 1.109, 0.783 and 0.924 seconds. Whole-wrapper CPU includes boot, guest work,
hashing, offline checks and children. Reverse ordering reduces one ordering
confound but four pairs are still finite observations without cache purging or
a controlled latency distribution. The mixed restart results and nearly flat
integrated time must remain visible. Earlier QEMU/native comparisons retain
their platform, topology, cache and durability qualifications.

Eight later normal-driver regressions were inspected, not rerun by this reviewer:
reset, disable, halted-peer poweroff, read error, write error, malformed successor,
full queue and network halt/IRQ all pass their oracles. Four VMs were executed by
this reviewer; campaign accounting is 24/24 actual VMs including the six forced
controls, eight regressions and final two diagnostic phases, plus the retained
pre-VM CLI rejection. Final allocation was about 2.45 GiB within 4 GiB. `replica-build-01/review-results.json`,
219 output hashes, source/binary bindings, build/run commands and full logs retain
reproduction details. No further VM is assigned. Native networking and broader
combined-capability acceptance remain outside this performance increment.


**Final mechanism diagnostic:** the two additional author-executed phases passed
the full original oracles and produced complete, distinct vCPU0/1 performance
records with zero failed/dropped logs. This reviewer checked their actual records
and the earlier post-wake diagnostic. Last periodic write/read window counters
fall from 132096/67584 to 101376/52224; empty scans fall from 30545/15416 to 137/82.
The last native-job periodic counters are 33792/17408 in both diagnostics.
These support removal of redundant empty scans and their memory windows.
Counters omit final partial 1024-event intervals, and different counter families
are not atomically sampled; matching periodic native counts are not proof of exact
whole-run request-count equality. Instrumented timings are not normal benchmark
scores and do not alter the mixed restart-read or nearly flat integrated-time
conclusions above. The campaign's 24 command records include these two phases;
no further build or VM is requested of this reviewer.

### Restart investigation — final kick-elision source and independent full-pair review, November 8, 2026

**Disposition:** accept final `io-perf-restart-20261108-01/candidate-build-02`
for its narrow correctness-preserving change. A vCPU already acknowledged for
this exact unreleased I/O epoch does not need another doorbell. The candidate
still checks that participant's terminal state under the existing stop mutex;
it skips only the native ring. There is no unresolved substantive source or
replication blocker. Restart measurements remain variable, so this review does
not establish a broad or stable performance improvement.

**Source/lifecycle review:** `copy_with_permit` invokes its participant callback
for every vCPU with an exact-epoch acknowledgement flag. Production dispatches
to `Stops::kick_if_needed`, which checks `ended` under the existing mutex before
its optional ring. Ordinary `kick` delegates with `needed=true`. The callback
flag cannot become stale within this serialized, unreleased epoch: resident DSB
precedes acknowledgement, and guest access cannot resume before release. Old or
late acknowledgements cannot satisfy a later epoch. Per-peer cancellation, exact
all-ACK admission, final permit grant and release-on-error remain unchanged.

The first provisional implementation skipped the entire callback and therefore
also skipped its ended check. Although the current device owners cancel pending
permits before participant departure, that narrowed the public copy API's
terminal guarantee. Independent review required the final unconditional callback
and optional ring. Earlier candidate01 timings are provisional evidence, not
qualification of the corrected candidate02. The final unit test checks an
exact-ACK-but-ended peer is observed and rejected with zero guest-write effects,
written epoch zero and release epoch one. The two-epoch test also checks that
prior-epoch ACKs do not suppress later rings. Tests use the generic seam; the
production stop-lock ordering was independently source-reviewed.

The restart-only runner was separately reviewed. It validates a prior successful
read-only restart, file hash and actual unchanged image; each run clones and
rehashes the same seed. Default full-pair behavior and all original CRC,
library/profile, S5, fsck, persisted-file and whole-image checks are preserved.
The declared A B B A B A A B ordering balances run positions and removes image
content/layout as a between-build difference, but does not control every host
scheduling/cache effect or identify a cause by itself.

**Independent reproduction:** all 916 frozen final inputs were verified and
copied to a fresh target; normal CLI and unchanged resident were separately built
offline/locked with Rust 1.97, signed and stack-audited. Relative to the prior
notify candidate, only `host/src/execution.rs`, `host/src/execution/io.rs` and the
separately reviewed restart-runner source differ. Producing manifest SHA-256:
`3f780aa2d91e3b09acdb81d28ff4f7cfb925d3a7a15c3c67f299f5a711f32b8c`.
Fresh CLI: `891b541c906d490c3f72bdd35b1c762646a3e98c456d1a58375700592bd2e30c`.
Resident: `33d7b6fe80d42112d95406212bd38089782214cc380a4af3b18915595cba2bf4`.
The original full-pair oracle, prepared inputs and pinned FEX were reverified.

Exactly two independent normal VMs ran under the shared lock, without competing
build/VM work or retry. Both passed the unchanged original write→restart oracle:
64 MiB written plus CRC readback, 64 MiB restart read with zero writes, full
profile/library checks, clean S5/fsck, persistent file identity and unchanged
read-only whole image. The actual image was independently rehashed after run.
File SHA-256 is `cb6bf0efd4783348f7a7188a55649582343287bdca25ce9e6cdc621aa139d311`;
unchanged restart-image SHA-256 is
`2dad4abe8329e29c0a6c32661eb841ec09ebc53b8d1a996026c303ef362ab41e`.

The write/CRC job takes 1.375 seconds (0.899 writing, 0.475 inline readback);
restart verification takes 0.488 seconds (read runtime 0.487). Full phases take
8.987/7.587 seconds; the whole wrapper takes 17.40 seconds real and 23.85 CPU
seconds (21.98 user, 1.87 system). Wrapper costs include boot, hashing, checks and
child processes. These are finite observations, not isolated backend latency.

The author's final same-seed C B B C comparison also passes: corrected candidate
restart jobs are 0.476/0.749 seconds versus prior candidate 0.909/0.506 seconds.
Both builds still vary; integrated full-restart times overlap. Four normal-driver
regression oracle records were inspected, not rerun by this reviewer: N4 split
atomics/code preservation, pending-compiler timeout/resume, active reset and HLT
cooperation all pass. Shared guest preparation/oracle/FEX limit independence;
producing source verification, compilation and the two full-pair executions are
independent.

Evidence is retained under `replica-build-01` and `replica-fio-01`, including
`review-results.json`, 80 output hashes, exact commands, full logs and build/source
bindings. The campaign finished all 32/32 authorized VM launches successfully, including
provisional comparisons, final comparisons, four regressions, four diagnostics
and two full pairs. The initial replica-review allocation was approximately
1.64 GiB within the 4 GiB cap; later successful identical read-only images were
deduplicated with paths and hashes retained. No more VMs are
assigned to this reviewer. Native networking and broader combined-capability
acceptance remain separate work.


**Final primary and diagnostic follow-up:** the primary final-candidate full pair
also passed first attempt: write/CRC 1.331 seconds, restart 0.768 seconds, full
phases 8.517/7.703 seconds, wrapper 17.03 seconds real and 23.60 CPU seconds.
The independent full pair above remains a fresh production build/execution,
not a replay of the profile binary.

This reviewer separately inspected all four completed baseline-profile runs.
Every extent classifier reports exactly 16,384 unique admissions and successful
completions, invalid zero; both natural-HVC boundaries exist for both vCPUs.
Each log has exactly two terminal vCPU records, with failed/dropped zero. Their
fio jobs take 0.499, 0.500, 0.916 and 0.789 seconds; the classified first-admission
to last-publication envelopes take 0.483, 0.484, 0.900 and 0.774 seconds. These
are distinct boundaries, not interchangeable timers.

In the two faster observations, QueueNotify accesses come from vCPU0 and IRQ-ACK
accesses from vCPU1. The slower third observation records both on vCPU1; vCPU0
has 876.5 ms in the resident halted bucket, but 413.7 ms of host-thread CPU time.
The fourth observation has mixed notification placement (1876 on vCPU0, 14507 on
vCPU1). Native execution stays at 18.3–19.0 ms and the copy-window copy portion
at 6.8–7.4 ms, while total window time varies from 212.9 to 420.5 ms. The source
supports interpreting this as an association between vCPU work/coordination
placement and restart variability; it does not show exact fio-thread identity,
prove placement causes the difference, or isolate every guest/host contribution.

The halted bucket includes resident `io_checkpoint` activity within the HLT path,
so it is not pure host sleep. Natural-exit snapshots are not simultaneous with
the phase boundaries; CPU0 skew reaches 2.815 ms at the third begin and 4.331 ms
at its end, and 4.079 ms at the fourth end. Host/resident categories and native/
lane-wait times overlap and must not be added as disjoint costs. Boundary counter
arrays and MMIO access arrays are separately sampled. This profiler uses the
notify baseline with unconditional kicks, not selected candidate02; its results
do not measure the selected optimization's causal effect. Instrumented timings
are diagnostic, not new benchmark scores. No general restart speedup or blanket
performance-completion claim is added by these observations.
## Installed-root I/O matrix — independent review and N4 replication, November 8, 2026

**Disposition: accepted as bounded functional and workload evidence.** All six
primary pairs and both independent N4 pairs pass: 16/20 authorized VM launches,
no retries. This review changed no production source, workload or guest options.
The matrix covers installed ext4 root operations, nine measured profiles,
four 256 MiB files, full CRC sweeps and fresh read-only relaunches. It does not
complete native networking or establish general storage/performance acceptance.

The independent replicas used the final production CLI/resident previously
fresh-built by this reviewer from the restart candidate02 snapshot. All 916
retained source hashes, binary/FEX bindings, five frozen oracle files, QEMU
binary and prepared inputs were verified again. Rebuilding unchanged inputs
was unnecessary; these are independent executions with independently built
natemu binaries, not a newly authored workload or independently authored oracle.
QEMU N4 ran before natemu N4, under the shared VM lock, with no concurrent
benchmark/build. Both pairs passed on their first attempts within the declared
600-second main / 120-second verify limits.

Evidence lives in
`/Users/liaozping/orca/natemu-raw/io-matrix-20261108-01/replica-review-01`:
`binding.json`, exact commands, `reproduce.py`, wrapper logs, `review.py`,
`review-results.json` and 816 retained output hashes. Raw independent pair
records are `qemu-n4-replica-01` and `natemu-n4-replica-01` beside that directory.
The review reparsed all eight pairs from raw serial using the frozen oracle,
checked those results against saved JSON, independently recomputed all 72
workload rows, and checked primary table aggregates. Every phase has zero
process/oracle errors, successful offline fsck, complete 1 GiB CRC sweep and
four complete file hashes. Each read-only relaunch preserves the four hashes
and whole-image SHA; both replica final image hashes were independently
recomputed after execution. Ordinary create/patch/copy/rename/unlink checks and
clean S5 are present. The campaign occupies approximately 10.72 GiB of its
12 GiB allowance at review completion.

The aggregation is valid for these records: bytes and completed I/Os are summed
over the common-clock union from earliest `job_start` to latest
`job_start + job_runtime`; all concurrent jobs have positive overlap. fio3.39
emits both values in milliseconds. Per-job p50/p95/p99 values are retained,
not averaged or presented as aggregate percentiles. VM elapsed/CPU totals sum
only the timed VM processes and children; they exclude outer image cloning,
hashing, Python and offline fsck. Mounted-root times are host observations
polled at 250 ms, not exact instruction-boundary boot times.

N4 primary/independent observations:

| Workload or resource | natemu | QEMU |
| --- | ---: | ---: |
| 4 KiB random read Q16 | 8.78–9.24 kIOPS | 17.14–19.10 kIOPS |
| Four-job 4 KiB random read, Q16 each | 18.27–18.98 kIOPS | 54.70–55.65 kIOPS |
| Worst per-job p99 in that four-job row | 10.42–12.91 ms | 4.29–4.49 ms |
| 128 KiB sequential read Q16 | 971.55–983.12 MiB/s | 156.90–159.25 MiB/s |
| 128 KiB sequential write Q16 | 709.82–733.10 MiB/s | 142.18–143.14 MiB/s |
| Both VM phases, elapsed | 190.63–191.25 s | 195.19–197.29 s |
| Both VM phases, CPU | 593.97–599.78 s | 253.51–256.18 s |

These observations support a concrete scaling limitation, not a blanket win:
natemu's primary single-job random Q16 falls from 27.20 kIOPS at N1 to 22.77 at
N2 and 8.78 at N4; the N4 replica is similarly low. Its N2 four-job aggregate
33.39 kIOPS exceeds QEMU's 28.36, but worst per-job p99 is 24.25 versus 6.39 ms.
The N4 pair nearly matches QEMU elapsed time while consuming over twice its CPU.
Other rows vary materially: natemu N4 random Q64 is 9.08–12.60 kIOPS, so increased
requested depth is not an established throughput gain. These are finite
observations, not confidence intervals or evidence identifying the cause.

The fsync row records exactly 64 data writes and 63 periodic sync samples;
required final `end_fsync` is outside fio's reported sync sample accounting.
N4 mean periodic sync latency is 9.00–9.10 ms for natemu versus 0.130–0.176 ms for
QEMU. Durability differs: natemu uses `F_FULLFSYNC`, QEMU its fsync path, so this
is not an equivalent-durability performance ratio. The final sync remains
required and an explicit failure is rejected.

Pure timed reads perform inline CRC checking. Mixed reads do not; the later
sweep checks stored data after mixed writes, not the bytes previously returned
by mixed reads. All figures are checksum-bearing whole-system workloads with
five-second timed rows and a 1 GiB dataset. No host cache purge occurred; guest
`direct=1` does not make them physical SSD measurements. QEMU PCI/TCG/max-CPU
and backend scheduling differ from natemu MMIO/one active native request.
Shared guest preparation, kernel, fio and oracle limit evidence independence.
N1/N2 have one primary pair each; N4 has two observations per backend. The
results justify reporting real capability and prioritizing SMP/random-I/O,
CPU-cost and latency work, without attributing those gaps to a particular
mechanism or modifying synchronization/durability to improve a score.

**Final documentation check:** the benchmark matrix, implementation summary and
INV current result preserve the finite scope and open compatibility/performance
work. Every primary/repeat throughput cell, random-read p99 cell and periodic
fsync mean/p99 cell in the published benchmark tables was independently checked
against raw results. No substantive claim-consistency defect remains. The
benchmark section links this review; the raw review remains beside its bindings.
## N4 I/O diagnosis — independent profiler replay, November 8, 2026

**Evidence validity: accepted for diagnostic attribution, not performance scores.**
The reviewer checked the isolated baseline profiler's counter mapping and
lifetime boundaries, then verified all 917 instrumented source hashes, three
binary bindings and six frozen oracle files. One independent N4 read-only probe
was executed using the same author-built profiler, without a new build, sampling,
production changes or retries. The shared VM lease excluded competing builds and
benchmarks. The probe passed within 26.741 seconds; all original two-profile
options, inline CRC, four full file hashes, clean S5, offline fsck and unchanged
read-only image checks passed. The final image was rehashed and only then
hardlinked to the immutable byte-identical prepared image; future runs clone it.
Failed or active images were not modified.

The primary and independent runs each contain four distinct valid terminal
resident records, four complete host logger records with failed/dropped zero,
and one final owner-drained aggregate. The independent audit reconciles window,
phase, ring/skip and per-vCPU epoch totals; native job and lane-wait counts match,
and all window/kick/native/lane errors are zero. No late SHADOWS service occurs
in either diagnostic, so these observations do not replace its forced control.

| Whole-probe diagnostic | Primary | Independent |
| --- | ---: | ---: |
| Completed nonempty windows | 366,726 | 348,524 |
| Actual I/O doorbell rings | 1,083,870 | 1,063,688 |
| Host ACK wait | 6.720 s | 6.390 s |
| Host kick phase | 1.739 s | 1.677 s |
| Grant/invalidation/copy/release phase | 0.224 s | 0.233 s |
| Native lane execution | 0.326 s | 0.318 s |
| Summed resident I/O waits | 21.832 s | 19.899 s |
| Summed I/O waits entered from HLT | 7.730 s | 8.852 s |
| HLT-context I/O epochs / all CPU epochs | 571,569 / 1,466,904 | 661,021 / 1,394,096 |
| VM elapsed / CPU seconds | 27.00 / 64.23 | 26.71 / 61.97 |

These are cumulative whole-probe counters, including boot, both timed profiles,
full-file hashing and shutdown. Per-CPU durations are summed across overlapping
CPUs, not additional elapsed time. Host ACK/kick/copy phases, resident waits and
native/lane timings overlap and must not be added as independent costs. The
copy phase includes invalidation and barriers, not just memcpy. Periodic family
loads are nontransactional; final publication follows native final-sync/join.
Resident wait accounting starts after ACK publication and ends when release is
observed, includes late shadow-reset work if present, and excludes the pre-ACK
DSB and post-release invalidation work. Zero-spin epochs still count.

Real division and both reuse attribution sites were redirected to other buckets;
no ABI dimensions changed. HLT attribution identifies the prior resident bucket,
not pure host sleep or guest userspace placement. It includes checkpoints on
initial HLT entry, so it is not an exact count of occasions eligible for a future
quiet-before-WFI fast path. Non-HLT includes AP and other resident contexts.
The measured HLT share is 35.4% / 44.5% of summed I/O waits; it is not a proven
recoverable wall-time percentage.

The instrumented Q16 rows vary from 11.25 to 9.14 kIOPS and the four-job rows
from 18.21 to 18.41 kIOPS, with worst per-job p99 13.30 / 10.16 ms. These numbers
identify the executed workload; they are not new normal-build benchmark scores.
Shared profiler/harness/binaries limit independence to source/method review and
fresh execution, not independent profiler authorship or rebuilding.

**Decision relevance:** the repeated profile supports investigating unnecessary
coordination of HLT-context participants before copying or native I/O throughput.
It neither proves the specific source of late ACKs nor validates a quiet-admission
protocol. Skipped-epoch table currency, clear-quiet/request publication ordering,
late SHADOWS, terminal checks and AP mailbox wake behavior remain load-bearing
requirements for any such change. The existing exact-ACK path remains the reference;
no production adoption or performance-completion claim follows from this replay.

Evidence: `natemu-raw/io-smp-fix-20261108-01/profile-replica-binding.json` and
`probe-profile-replica-01/{review.py,review-results.json,review-output-sha256.json,review.md}`,
including 47 output hashes, full logs, commands, counter records and dedup metadata.
This replay consumed one VM, bringing the declared campaign to 7/24 at this point.
## HLT quiescence — independent source review and forced replay, November 8, 2026

**Evidence validity:** candidate03's production handshake is accepted at source
level and its forced skipped-epoch/resume behavior is independently reproduced.
This is not yet acceptance of a performance improvement; full-matrix comparisons
remain pending. The reviewer verified and copied all 919 frozen candidate inputs,
rebuilt normal CLI and test driver separately into a fresh target, rebuilt the
normal resident, signed both host binaries and passed the resident stack audit.
The resident SHA matches the producer's
`0fa888b7926545377cb34ee0e2974831665c5ce15a6593f4f0d9f870a7e71b74`.
No spin/read-first experiment is part of candidate03. The source review checks
DSB before quiet publication, clear+DSB before the next request check, the
host publication barrier, terminal callback preservation and final grant CAS.
Only the private HLT wait publishes quiet; AP/compile/MMIO/power waits retain
ordinary acknowledgement. Existing ABI offsets stay fixed; the new array fits
the reserved area. Table invalidation compares against the prior serviced epoch,
so a later read-only request cannot conceal an earlier skipped table write.

The reviewer found and closed a fixture oracle gap: final acknowledgements were
not checked. The corrected oracle requires both exact final ACKs and rejects a
synthetic stale-final-ACK report. It is retained separately in
`replica-quiet-oracle-01`, with dependency hashes and its patch; producing compiled
sources remain the untouched frozen candidate snapshot. Rejudging saved primary
outputs with that oracle yields positive PASS and exactly two stale-r13 errors
for the isolated old-table-test negative, all other checks passing. No original
failure was relabeled as an ordinary successful VM.

One independent positive VM passed first attempt in 0.327 seconds. Both CPUs
first read data A and execute cached function1, then reach actual quiet=[1,1].
Three granted windows leave their ACKs=[0,0]: warm read1, PTE/code write2,
read-only3. The pre-canceled request announces no epoch and causes no effect.
An explicit interrupt wake yields data B, function2 and one handler execution on
each CPU, followed by clean end, quiet=[0,0], ACK=[3,3], and joined/destroyed
teardown. The test does not force cancellation after announcing a window, every
clear-quiet/publication interleaving, or late-SHADOWS service; those are separate
source obligations/regressions. The negative mutant was rejudged, not rerun by
this reviewer. Shared fixture/FEX limit independence; fresh source verification,
compilation and positive execution are independent.

**Decision relevance:** the replay validates the load-bearing skipped-table-epoch
repair and real HLT admission/resume for this schedule, rather than only host
unit-test state. It supports proceeding to unchanged full-workload comparison;
it does not establish all-ISA/platform coverage or performance completion.
Evidence is retained beneath `natemu-raw/io-smp-fix-20261108-01` in
`replica-build-01`, `replica-quiet-oracle-01`, and `quiet-replica-01`. This is
campaign VM11/24, replacing the generic HLT regression allocation. No full-matrix
replica has been launched yet.
## HLT quiescence — independent full N4 matrix replay, November 8, 2026

The independent candidate03 full N4 matrix pair passes on its first attempt,
using the reviewer's fresh 919-input production build, unchanged frozen original
matrix oracle/inputs, pinned FEX and the shared VM lock. Source, artifact and
oracle hashes were verified again after build-intermediate cleanup. No build,
competing VM, native benchmark, guest-affinity/cache adjustment or workload
change accompanied the measured pair. The normal CLI and resident are distinct
from the diagnostic profiler. All nine rows execute with their original options.

Both phases pass inline/readback checks, full 1 GiB CRC sweeps, four persisted
file hashes, ordinary filesystem checks, clean S5 and offline fsck. The read-only
relaunch preserves the complete image hash; the reviewer independently rehashed
that final image after execution. The final five completed campaign pairs were
reparsed and all 45 workload rows independently recomputed, retaining 510 output
hashes under `io-smp-fix-20261108-01/replica-review-01`. Fresh-build/source bindings
and the exact wrapper/command are retained separately. This execution completes
VM23–24 of the originally declared 24; a separately recorded extension for a
matched N1 regression check is pending and is not part of this result.

| N4 observation | Fresh baseline | Candidate primary | Independent candidate |
| --- | ---: | ---: | ---: |
| Random read4KiB Q16, kIOPS | 9.228 | 36.618 | 36.983 |
| Four-job random read4KiB Q16 each, kIOPS | 18.437 | 46.318 | 44.958 |
| Q16 p99 completion, ms | 2.474 | 0.569 | 0.518 |
| Four-job worst per-job p99, ms | 15.270 | 12.780 | 8.159 |
| Combined VM elapsed, seconds | 193.76 | 113.07 | 114.73 |
| Combined VM CPU, seconds | 599.43 | 215.55 | 205.15 |
| Sequential write128KiB Q16, MiB/s | 689.53 | 792.11 | 453.54 |
| Mean periodic fsync, ms | 8.425 | 8.506 | 10.175 |

**Evidence validity:** the selected N4 random-read and integrated CPU/elapsed
improvement is independently reproduced with correctness retained. These are
checksum-bearing whole-system workloads; VM totals exclude outer clone/hash/fsck
work. Per-job p99s are not aggregate percentiles. The result does not show that
every row improves: sequential write throughput varies substantially and the
replica fsync row worsens. N2 four-job tails and the primary N1 sequential-write
row also require explicit reporting. No slower run is replaced by the repeat.

The current-source N4 late-SHADOWS positive record was inspected, not rerun by
this reviewer: all four exact ACKs precede the post with release0, all four reset
counters advance0→1 and pending bits clear before release. The ordinary split/AP
regressions and forced quiet replay serve different obligations. Production
source safety remains accepted as reviewed; finite tests do not enumerate every
withdrawal interleaving.

**Decision relevance:** this is strong scoped evidence for the intended N4
coordination improvement. Final cross-profile acceptance remains pending the
bounded fresh matched N1 baseline/candidate comparison, because its initial
501.16MiB/s write result was materially below the earlier808.32MiB/s baseline.
The independent N4 write variation reinforces the need to retain all rows and
avoid inferring a cause. A small host-context check after the replica reports
96% APFS data-volume usage with approximately10.0GiB available; timed stderr
reports zero swaps in the inspected baseline/candidate runs. Neither observation
identifies the write-variation mechanism. No additional VM was launched by this
reviewer, and the lease was returned before the N1 comparison.

## HLT quiescence — final independent acceptance, November 8, 2026

**Disposition:** accept the selected candidate03 as a targeted N4 read/CPU
coordination repair. No substantive source, execution or reproduction blocker
remains in this increment. This closes the pending matched-N1 assessment above;
it does not qualify every workload or complete the broader I/O plan.

The final audit reparsed all seven full pairs with the unchanged frozen matrix
oracle, independently recomputed all 63 workload rows and pair timing totals,
and retained 714 output hashes in
`natemu-raw/io-smp-fix-20261108-01/replica-review-01`. Every row and pair total
matches canonical `summary.json`. All 135 rendered numeric cells were separately
checked against phase results, including the explicitly earlier QEMU/N2
references and periodic-fsync p99s. The retained report matches the benchmark
document. All seven pairs pass CRC/file hashes, clean S5, fsck and unchanged
read-only relaunch images; the independent N4 image was independently rehashed.
This extends the previous five-pair audit, without launching additional VMs.

The fresh matched N1 baseline/candidate write results are 436.446/711.971 MiB/s,
with combined VM elapsed 126.52/126.60 seconds and CPU 154.66/156.02 seconds.
The unchanged baseline also falls below the earlier 808.32 MiB/s observation.
A consistent candidate-specific write regression therefore does not reproduce;
the cause of the variation remains unknown. The first candidate's 501.16 MiB/s,
independent N4's 453.54 MiB/s, worse replica fsync tails and N2 four-reader p99
remain visible. Host swap usage and capacity snapshots do not establish a cause
or absence of memory pressure; process rusage swaps are a different measure.

The independently reproduced N4 Q16 gain (9.228k to 36.618k/36.983k IOPS),
four-reader gain (18.437k to 46.318k/44.958k), and CPU reduction
(599.43 to 215.55/205.15 seconds) support selection. Four-reader throughput and
worst per-job p99 still trail the earlier QEMU reference. These short,
checksum-bearing, warm-cache observations do not establish uniform write gains,
statistical bounds, storage-device bandwidth or general QEMU parity.

Final source review confirms the ordered HLT-only promise, withdrawal checkpoint,
previously-serviced table epoch, unchanged terminal/cancellation checks and
exact-ACK AP path. The source contract in `io-plan.md` and the virtio README
matches these obligations. The selected patch excludes the withdrawn shadow-read
and spin experiments. Workspace production source bindings match frozen C3;
the rebuilt resident hash matches the independently built resident. Primary unit
and live regressions remain author executions; the fresh build, forced quiet
positive and full N4 pair are independent executions. Shared fixtures/FEX and
finite schedules limit independence and coverage; the late-SHADOWS positive and
stale-table negative were independently inspected/rejudged, not rerun here.

The campaign closes at 28/28 launches: 27 healthy runs and one intentional
stale-table negative. The separately declared N1 extension enlarged 24/8 GiB
to 28/11 GiB; approximately 10.14 GiB remains. No unexpected VM failure or retry
is hidden. Removal of reproducible build intermediates retained bound binaries,
source, recipes and results. Final benchmark, implementation and investigation
claims are consistent with this scoped acceptance. Full distro/partition/reboot
and recovery coverage, native networking, write variability and four-reader
fairness/tails remain separate work.

## I/O safety stress — independent pre-execution review, November 8, 2026

The native, lifecycle and Linux-soak methods are accepted for bounded execution,
not yet as completed coverage. Authors and reviewer remain separate. The reviewer
performed source/oracle inspection only in this preparation; no VM or build.

The native fixture uses normal, uninstrumented N2/N4 repeated cycles separately
from six compile-time-only scheduling hooks. Each normal cycle includes mixed
active/HLT and all-active read windows, real split LOCK NOT operations, global
PTE and cached-code changes, quiet/read-only successors and IRQ wake. Review
added quiet-boundary parity against stable per-CPU operation counts: a zero/all-
ones check alone detects tearing but misses lost toggles. Parity still cannot
detect every even compensating error. Forced hooks retain actual pending epochs,
ACK/quiet observations, cancellation outcomes, late-SHADOWS/reset service and
timeouts; they never manufacture production protocol acknowledgements. Strong
hook atomics establish selected schedules, not the necessity or sufficiency of
production barriers. Normal runs remain necessary empirical coverage.

Repeated reset/disable fixtures check old sentinels and fresh status/used-entry
metadata plus both complete payloads before reusing queue RAM in every generation.
Review caught missing intermediate fresh metadata checks that final RAM alone
would not detect. A cycle-7 guest corruption must fail the unchanged positive
oracle at its expected assertion with clean teardown, rather than count arbitrary
failure as a successful falsifier. Two initial executions remain failures of the
fixture: absolute MMIO displacement sign extension, then unsupported memory CMP.
The corrected closure uses MOV through R12+0x44 and register TEST. The reviewer
accepted the address correction before noticing the separate CMP-form issue;
both attempts remain retained and consume launches. No production defect or
retroactive pass is inferred from them.

The soak uses disjoint 64 MiB files: pure random reads verified inline and random
writes with CRC headers followed by full verification. fio3.39 ordinary mixed
randrw reads do not acquire verification merely from the CRC option; the design
was corrected accordingly. Review strengthened common fio-job overlap, actual
worker PID/starttime/CPU observations, readiness, interval-log parsing and failed-
process-group cleanup. The loaded comparison is fixed-order, not causal or a
matrix-equivalent benchmark. The 256 MiB verified memory worker is bounded load,
not near-capacity pressure; sampled progress is not continuous observation, and
its final timeout-interrupted pass need not complete. Full CRC before
repreconditioning and final hash/fsck/read-only persistence checks remain required.

These methods address specific previously uncovered schedules and longer combined
load. Source and oracle acceptance does not establish successful live completion,
all interleavings, cross-platform memory ordering or general I/O compatibility.

Before freeze, the native author added a second shared cross-16-byte LOCK INC
counter after each NOT, publishing the local loop count only after both complete.
The final reviewed oracle compares its exact quiet-boundary increase with checked
sums of all per-CPU completed loops, alongside the NOT tearing/parity checks.
This closes the parity-only even-loss gap for the counter operation. Both
baselines come from one quiet snapshot; normal baselines must be zero. Final RAM,
counter ranges, rate arithmetic and request/grant/canceled-epoch/release/ACK
accounting are checked. Forced-prelude operations are explicitly outside the
subsequent rebaselined cycle counts. A controller-local scope guard releases all
latches before implicit thread joins on errors or panic. These final changes are
accepted for freeze; independent fresh normal/forced builds and six replay
launches are planned only after the execution lease is granted.

The normal native-build-01 resident differs from C3 as an ELF file, but independent
section comparison finds identical `.boot` and entire `.text` bytes and addresses.
All 41 changed `.rodata` bytes decode as diagnostic source-line numbers, with
identical filename/column and identical referenced source statements. Normal
symbols contain no `io_test`; the forced binary contains its hook function.
The reproducible comparison and artifact hashes are retained in campaign
`replica-binary-review-01`. This establishes executable hook absence without
claiming byte-identical complete binaries.

The first Linux soak remains rejected: an RCU stall occurred while exporting
large serial logs after workload execution. A separately reviewed correction
uses bounded, paced gzip/base64 framing with exact decoded lengths and SHA256,
retaining the original CSV bytes and workload checks. Malformed/truncated frames,
extra streams, excessive expansion and serial/decoded kernel errors remain fatal.
No printk/error suppression or workload reduction was introduced. The source
revision is accepted for a new run; the original failure is not promoted to a
pass, and neither the UART/runtime cause nor a production fix is established.
Whole-VM timing includes changed export overhead, unlike separately retained fio
workload metrics. The final reviewed source hash is
`8ca35332d1620ebb7ddb02abe2ab7a951de799eeacd6e114e52a12df3e2472b5`
(the final edit says the stall occurred during export, without assigning cause).

The first forced-scope failure diagnostic is also accepted: it releases latches
before joining, retains both the primary failure and exact copy error, and logs
actual epoch/service/timeout evidence. An isolated missing-CPU0-wait-service
mutant must reach the real-peer-SHADOWS schedule and fail with an acknowledgement
timeout and unchanged late-service counter, zero hook timeouts and clean teardown.
The unchanged outer drain may legitimately advance reset counters after release;
that is not evidence of the missing in-window service occurring.

### Independent fresh-build replay and final disposition

Accepted for the stated finite safety/load coverage. The reviewer independently
verified all 926 frozen source hashes, copied those inputs into
`io-safety-stress-20261108-01/replica-build-01`, and rebuilt the normal CLI alone,
normal driver/resident, and separately featured driver/resident into fresh
targets. Both resident stack audits, signing and all three runner self-checks
passed. The source manifest is
`c3fb5990ffb01adf7d6ebf3af66614ee0ea196b07be84fbb380379cbf28a1e35`.
Build commands, logs, source/oracle/input and binary bindings remain retained;
the normal resident hash `27b7abeb…` matches the independently inspected binary.
No production or fixture edits were made by the reviewer.

Seven authorized replica launches completed without retries: normal N4 with 512
cycles, forced N2 with 64 cycles, reset and disable with 64 generations each,
the missing-service negative control, and the Linux soak main/restart pair.
The six positive VMs passed. The negative VM remains an ordinary oracle failure,
with a separate precise expected-negative verdict. Its driver was freshly built;
its mutant resident and the FEX compiler are shared pinned inputs, not independently
rebuilt components. No extra launch was needed.

Independent post-run recomputation checked every native cycle's cumulative exact
increment count and NOT parity: normal N4 completed 22,276 operations from zero;
forced N2 completed 3,291 operations after its five-operation prelude baseline.
Both final RAM records agree. The forced run also retains 64 distinct increasing
announced canceled epochs and complete final accounting. Each lifecycle run has
exactly 128 native reads and 64 published commits, ordered old-generation
cancellation/fresh recovery and complete RAM/disk agreement; both active peers
record thousands of actual split operations. The mutant reaches real peer
SHADOWS with ACK `[2,1]`, times out specifically waiting for acknowledgement,
keeps late-service count zero and all hook timeout counts zero, releases epoch2
without a written/table epoch, and tears down both CPUs. Post-release reset
counter advancement is retained rather than misclassified.

The reviewer reparsed both successful soak pairs' serial framing and original
decoded logs, checked all fio/stress/interval/worker oracles, recomputed rates
from bytes/I/Os and actual direction runtime, and verified fsck, full 128 MiB CRC
sweeps, persistent file hashes and unchanged read-only image hashes. The replica
final 512 MiB image was separately rehashed. Common job overlap was 60.020 seconds
unloaded and 300.000 seconds loaded. Replica observations are:

| Context | Read IOPS | Write IOPS | Read p99, ms | Write p99, ms |
| --- | ---: | ---: | ---: | ---: |
| Unloaded | 27,123.54 | 16,924.77 | 0.872448 | 1.892352 |
| Loaded | 11,618.05 | 8,019.59 | 6.127616 | 7.766016 |

Replica main/restart host process times were 483.16/11.25 seconds and
1,787.97/13.10 CPU-seconds. The longest retained loaded sampling gap was 1.270
seconds; incomplete histogram tails remain explicitly qualified. Each of the
three stress processes passed one worker with zero failed/skipped workers.
Primary benchmark table cells, whole-process timing and method qualifications
were independently checked against retained data and agree. These fixed-order
observations support contention/progress under this workload, not a randomized
performance comparison or physical-disk score.

The reproducible independent audit, 130 output hashes, exact effects, numerical
summary and review are retained in
`io-safety-stress-20261108-01/replica-review-01`. The seven replica launches bring
the campaign to 24 of its 32-launch allowance; measured storage after replication
was 2,427,852 KiB (about 2.315 GiB), within 4 GiB. Earlier two lifecycle fixture
failures, the original RCU-stall soak and deliberate negatives remain visible.
The reviewed plan/benchmark conclusions preserve the unresolved bulk-UART stall,
sampled rather than continuous worker observation, bounded memory pressure,
forced-hook ordering limitations and lack of a universal concurrency proof.
No substantive acceptance blocker remains for this scoped coverage increment.

## Loaded I/O diagnostic and UART repair review — November 8, 2026

Reviewer `/root/io_scope_review` independently inspected the retained diagnostic
sources/analyzers and recomputed host phase deltas and per-window means from
stderr. The corrected baseline analysis is in
`io-load-repair-20261108-01/instrumented-profile-01b-analysis-02`; the owner-QoS
comparison is in `instrumented-profile-02-analysis`. Their final accounting,
four-vCPU logger validity, full-pair correctness and read-only-image invariants
pass. This review executed no VM or build.

The baseline main phase retires 20,014,866 successful windows, 80,059,464
participant checks and 8,469,134 native jobs. Interior unloaded/loaded mean
window elapsed time is 5.073/13.271 us; kick/check elapsed time is 1.351/6.899 us,
ACK wait is 3.421/5.709 us and post-ACK copy is 0.134/0.312 us. Resident elapsed
park shares rise from approximately 14–21% to 24–33% per vCPU. These are not CPU
percentages: parked duration includes descheduling and late-SHADOWS service,
excludes quiet-skipped epochs and pre-ACK/post-release work, and cannot be added
to CPU/perf buckets. Native execution overlaps inclusive lane wait. Kick timing
includes all participant checks/barriers/skips, not just native ring calls.

Counters use separate nontransactional interior brackets for each family.
The 500-ms margins are around observed marker timestamps, not a guaranteed bound
on guest phase-edge error; scheduling can exceed the nominal 250-ms observation
cadence. Corrected markers use the same native CLOCK_UPTIME_RAW API as host
counters. The first diagnostic's log-cap failure and incompatible Python clock
origin remain retained; the corrected version disables only the old 5-ms live
perf reader while preserving final owned-vCPU counters. The baseline guest
clock-watchdog readback/skipped-skew warning remains visible despite passing I/O
oracles. The retained audit now resolves its sibling analyzer instead of a
temporary-file dependency; corrected analysis values are unchanged.

The QoS comparison's source manifests differ only in `linux.rs`; the resident
binary is unchanged. Queries succeed and report owner DEFAULT (21) becoming
USER_INTERACTIVE (33), vCPUs staying 33 and the disk lane staying 21. They do not
report effective overrides or physical-core placement. Loaded elapsed window
time is 13.271→13.296 us, kick/check 6.899→6.961 us, ACK 5.709→5.697 us and copy
0.312→0.312 us. Diagnostic read/write throughput is 43.384→41.599 and
30.348→29.342 MiB/s. Native elapsed/job is 5.803→8.330 us and inclusive lane wait
10.696→13.038 us. These sequential instrumented runs show no useful positive
signal for the owner-only change; they do not establish a statistical regression
or priority-inversion cause. The reviewer recommends dropping QoS02, rather than
expanding scheduler-policy changes on this evidence. Parent reports its exact
reversal; final selected-source binding remains part of subsequent qualification.

UART implementation, readiness experiments, focused regressions, terminal-drain
controls and the remaining normal/independent qualification are covered by the
[pre-execution review](INV-03-01-pre-execution.md#loaded-io-and-uart-repair--november-8-2026).
Source/method acceptance does not yet accept a final UART policy, universal
fairness, a combined performance improvement or completion of this repair campaign.

### Stress metrics-summary validator correction

During final qualification the author found that `stress_result` searched for
`metrics-untrustworthy:` while the pinned stress-ng emits `metrics untrustworthy:`.
Its `all(...)` over an empty match list therefore did not enforce the intended
trustworthiness check. This is a real validator gap, not evidence that earlier
worker logs reported untrustworthy metrics. Existing worker pass/fail/skip and
positive-work checks are separate.

The reviewer source-accepts `soak-metrics-oracle-01/change.patch`, independently
verifying its before/after hashes and AST. Only `stress_result`, its synthetic
sample and self-check change; init, jobs, thresholds and runner remain identical.
The correction requires exactly one complete zero-valued summary, accepting
either separator spelling. Missing, duplicate, nonzero and mixed-spelling
duplicates are rejected by added controls; trailing garbage cannot pass as zero.
No self-check or VM was run by the reviewer during the active timed pair.

Corrected oracle results must remain separate supplementary judgments over the
retained decoded logs, binding old results/logs and the new oracle; original
results are not rewritten. The independent build will retain its original
frozen compilation inputs and bind the corrected soak oracle separately.
Successful supplementary rejudgment, rather than this source review alone, is
required before retaining earlier stress-metrics acceptance claims.

### Independent final UART/combined-build qualification — November 9

**Correctness acceptance: accepted for the stated finite scope. Performance
disposition: reverse-order control still pending.** The independent reviewer
verified and copied all 927 frozen final-build-01 source entries, then built the
normal CLI alone, driver and normal resident into a fresh target. Signing, the
resident stack audit, serial self-check, corrected soak self-check (212 negative
controls) and native-stress self-check passed. The compilation manifest is
`29b8b032bef2d4a2af565f3b554a81f314c630d8d5d4a6ea52094008c11e64a6`.
CLI/driver hashes are independently built artifacts (`49aa4010…`/`2b93eaaa…`),
not claimed byte-identical to the producing host binaries. Resident `27b7abeb…`
matches the selected artifact; FEX `3ccb420c…` remains a shared pinned input.
Corrected soak oracle02 and loaded-serial oracle07/input02 are separately bound;
they do not mutate the frozen compilation snapshot.

One preparation-only attempt used oracle06's manifest filename/schema for
oracle07, which instead has `bindings.json/oracle_sha256`. It failed before
source copying, building or VM execution. The empty attempt, recipe and failure
are retained in `replica-prepare-attempt-01`; correcting that binding lookup did
not change production or test behavior. No VM was retried.

Five authorized independent launches pass: threefold unpaced serial, soak
main/read-only restart, normal native N4×512 and CPU-loaded threefold serial.
The onefold independent repetition was deliberately omitted by the reviewed
budget reallocation; primary onefold evidence and the independently repeated
longer payload are distinguished. The first loaded primary remains FAIL for
missing observer samples. Its corrected fresh primary and this fresh independent
run pass the unchanged strict consecutive-observation oracle; neither result
retroactively changes the failure.

Independent unloaded serial emits exactly 14,144,544 wire bytes in 89.42 guest
seconds, with maximum heartbeat gap 1.04 seconds and clean S5. Whole-process
time is 98.29 seconds and 98.94 CPU-seconds. CPU-loaded serial emits those same
bytes over the drained interval 8.93–110.45 (101.52 seconds); its maximum heartbeat
gap is 1.15 seconds. All three distinct actual worker identities have 96
consecutive observations spanning 5.92–112.21, maximum observation gap 1.22
seconds, increasing CPU ticks, 180-second verified work, zero exit/failure/skip
and exactly one zero trustworthiness summary. Whole-process time is 185.94
seconds and 620.42 CPU-seconds. This is finite CPU-loaded output/progress
coverage, not memory-pressure coverage or a universal fairness guarantee.

Native N4 uses the normal, uninstrumented resident for 512 seeded cycles. Its
22,546 split operations agree with the exact increment counter and final NOT
parity; the existing full state/RAM/teardown oracle passes. Whole-process time is
1.64 seconds and 4.35 CPU-seconds. No new forced-hook or N2 VM is claimed here.

The strict soak pair passes full 128-MiB CRC sweeps, both persistent file hashes,
clean S5 and offline fsck. The reviewer independently rehashes the final 512-MiB
image as `219283d74544fac8e8e060f1d6caeb2165be825330fffffbfb822608dfc53513`,
equal to both read-only phase image hashes. Recomputed rates use actual direction
bytes/I/Os/runtime, with job overlap 59.995/299.986 seconds:

| Context | Read MiB/s | Write MiB/s | Read p99, ms | Write p99, ms |
| --- | ---: | ---: | ---: | ---: |
| Unloaded | 103.671 | 59.242 | 0.880640 | 2.408448 |
| Loaded | 43.827 | 31.772 | 6.258688 | 7.634944 |

Main/restart process times are 481.81/11.42 seconds and 1,783.97/14.31 CPU-seconds.
The three soak workers complete 335,995 / 334,593 / 49,815,552 operations with
strictly valid summaries. Compared with the fresh C3 control, writes remain
approximately 4.7% lower unloaded and 6.8% lower loaded. Older variation does not
dismiss this observation. The agreed reverse-order C3 pair is warranted; no
combined throughput gain or absence of regression is accepted yet.

The source review accepts the UART readiness/staging/lifetime/shutdown repair
and lazy error formatting: all-THR/REP acceptance and sink-error visibility are
preserved, pending TX owns liveness through IRQ publication, teardown drains
under the serialized timer halt, and allocation removal changes neither error
nor synchronization semantics. Source, focused controls and the five fresh runs
support this correctness disposition. Lazy formatting has no isolated gain claim.
Arbitrary blocking sinks and the accelerated nonphysical timing model remain
explicit limitations.

The reproducible small-result/worker audit and bindings are retained in
`io-load-repair-20261108-01/replica-review-01`; final image/resource proof is in
`replica-build-01/final-image-resource-audit.json`. At lease release the campaign
contains 4,208,185,344 allocated bytes (about 3.919 GiB), within 4 GiB, with
4,982,435,840 bytes free. The five replicas bring the campaign to 38 VMs before
the reserved reverse-order pair. No further reviewer execution is authorized by
this disposition. Current benchmark tables match retained primary rates/tails
and times; the strict-metrics section correctly separates supplementary worker
rejudgment from historical whole-run oracles. The GIC sample paragraph correctly
distinguishes framework wake/trap observations from natemu mutex blocking and
keeps batching as a future hypothesis, not an adopted synchronization change.

### Reverse-order control and qualified disposition — November 9

The final C3 control in `final-reverse-baseline-soak01` passes both phases,
strict worker summaries, fsck, persistent file hashes and unchanged read-only
image checks. The reviewer independently recomputes rates from direction bytes
and runtime and checks that its artifact map exactly matches the first C3
control. All four normal natemu pairs use the same drain-aware input manifest.
No production or workload change occurs between these comparisons.

| Reverse C3 context | Read MiB/s | Write MiB/s | Read p99, ms | Write p99, ms |
| --- | ---: | ---: | ---: | ---: |
| Unloaded | 98.971 | 60.516 | 0.856064 | 2.023424 |
| Loaded | 45.054 | 31.121 | 6.062080 | 7.766016 |

Loaded candidate writes (31.327–31.772 MiB/s) fall within the two C3 observations
(31.121–34.077). This resolves the claim that the observed loaded deficit is
consistent across these controls; it does not prove equal performance or identify
the source of variation. Unloaded candidate writes remain below both C3 results:
57.641–59.242 versus 60.516–62.159 MiB/s, an observed cross-range cost of
2.1–7.3%. Unloaded write p99 is also higher: 2.408–2.474 versus 2.007–2.023 ms.
These costs remain unresolved and must not be presented as fixed or dismissed
using older measurements. The reverse control completes 340,567 / 343,451 /
52,609,024 stress operations; competing work still differs between observations.

The qualified conclusion is accepted: the sustained-console correctness repair
and finite progress/lifecycle coverage are established by the reviewed source,
controls and independent replay; an overall I/O performance improvement is not.
The fixed-order observations do not establish a statistical causal regression
either. No isolated gain is assigned to lazy error formatting, no synchronization
is removed, and native-network/VZ alternatives remain outside this implementation.
Future performance work should retain the unloaded write cost and tails as
explicit comparison targets rather than weakening workload or correctness checks.
