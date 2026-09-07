# INV-01-33 — Source findings and method preparation review

- **Screening:** (a) QEMU TCG publishes and links host-executed code; ordinary
  VMs also load executable guest memory. The distinct issue is the host-to-EL1
  split composed with guest recovery and SMP. (b) Mapping/cache interfaces do
  not establish this composition's publication, identity or lifetime protocol.
  (c) Findings apply to the thin EL1 shim and stated translator contract, not
  every code generator or emulator design.
- **Reviewer and independent context:** `/root/q34_sources`, delegated by
  `/root`; authored no Q33 investigation or experiment code.
- **Material inspected:** complete `INV-01-33.md`, seven claims, six method
  steps, ten finite cases, metadata requirements, controls, exclusions and
  budgets; the primary materials discussed below.
- **Date and verdict:** 2026-09-06 — **accept scoped source findings and method
  preparation**. Claims 01–04 have the stated source support; 05 and 07 are
  defensible inferences. Claim 06 remains an **unestablished hypothesis**:
  acceptance means it is a suitably bounded candidate to prepare, not that its
  protocol works. No substantive blocker remains for this source-only scope.
  No experimental execution is approved and Q33 remains unresolved at runtime.

## Source checks

Independently retrieved Apple's
[Hypervisor overview](https://developer.apple.com/documentation/hypervisor)
and [`hv_vm_map` documentation](https://developer.apple.com/documentation/hypervisor/hv_vm_map(_:_:_:_:))
through their publisher Markdown endpoints. They support process/thread
ownership, the entitlement, mapped host storage, unmapped-access exits and
reentry. The mapping page explicitly requires page alignment and one host VM
region. Independently read macOS 26.4 SDK Hypervisor.framework `hv_vm.h`,
`hv_types.h` and `hv_vcpu.h`: R/W/X flags, permission changes and owning-thread
execution match claim 01. `hv_vcpus_exit` also affects the next run of a stopped
vCPU. None of these declarations certifies a guest safepoint, metadata retirement
or cross-domain publication ordering.

Arm's [Implementing __clear_cache](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/caches-self-modifying-code-implementing-clear-cache),
dated 2025-01-20, supplies the stated DC CVAU, DSB, IC IVAU, DSB, ISB sequence,
separate CTR-derived line sizes and rounded starts. Its IDC/DIC optimization
does not remove all synchronization. Claim 02 correctly retains the full
operations and declines to infer host-to-guest staging visibility from them.
Concrete maintenance addresses, attributes, permissions and instruction ordering
must still be checked in the later code.

Arm's [Working with Threads](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/caches-self-modifying-code-working-with-threads),
dated 2025-01-21, supports claim 03's executor-side context synchronization,
control-flow exclusion during rewriting and non-broadcast ISB. The restricted
concurrent-instruction-exchange discussion permits mixed old/new combinations
and supplies no elapsed-time retirement guarantee. Its direct-branch publication
example does not justify arbitrary indirect links or multiword patches; the
candidate instead explicitly synchronizes entry into immutable targets. The
article itself cites L.a §B2.2.5, while the cache article cites L.a §B2.7.4.2.
This review accepts their scoped explanations, not a purported complete audit
of the current Arm ARM or all context-synchronization features.

The publisher-indexed text of
[DDI0487 M.c §B2.11](https://developer.arm.com/documentation/ddi0487/mc/-Part-B-The-AArch64-Application-Level-Architecture/-Chapter-B2-The-AArch64-Application-Level-Memory-Model/-B2-11-Mismatched-memory-attributes?lang=en)
was independently retrieved by targeted search; direct opening returned no
body. It identifies memory type, shareability, cacheability and applicable XS
attributes, describes possible coherency/ordering loss, and recommends compatible
aliases. It also describes constrained exceptions and software management, so
the source is not a universal prohibition on every mismatch. Claim 04 does not
make that stronger assertion. Matching attributes in the actual host/stage-2/
stage-1 composition remain to be justified, as the submission acknowledges.

Re-read [Intel SDM revision 092 Vol. 3A §11.1.3](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf)
from this reviewer's independently fetched primary copy. Its local and
cross-modification sequences support claim 07's distinction between retained
allocation lifetime and valid guest-source semantics. An optimization-equivalent
old target is different from code translating bytes the guest has validly
replaced. Arm cache synchronization does not discharge the x86 requirement.

## Method and adverse-case checks

Claims 05–07 preserve the necessary distinctions between code bytes, execution
metadata, request identity and source generation. Stale-response rejection,
copy-before-acknowledgement and pinned lifetime address meaningful hazards.
They do not yet prove an ownership or ordering protocol. In particular, a
post-copy cache operation cannot repair a torn staging read, and a release
store alone cannot establish that every peer has passed a retirement point.

The ten cases cover the three Q33 obligations: new host-emitted bytes execute
in the VM, resolved links transfer in the VM during peer activity, and missing
translations return to matching guest continuations. Multi-line/page coverage,
late/duplicate replies, interrupted requests, genuine fetch faults, partial
publication, source aliases and exhaustion are relevant adverse controls.
Actual block-PC traces plus an independent guest-effect oracle avoid accepting
producer-computed results or hashes as semantic execution evidence.

Case 3's acknowledgement must eventually name a concrete per-vCPU boundary
that excludes already-loaded old descriptors; an atomic pointer replacement
alone is insufficient. The submission already requires exact acknowledgement
ownership, link lifetime and synchronized invalidation before execution.
Likewise, interrupts injected while a vCPU is stopped for host work must be
distinguished from native handler execution after reentry. Exact race traces
and overlap coverage remain required, not presumed from request timing.

The finite pool/no-reuse limit is explicit and does not establish reclamation.
Missing overlap remains missing coverage; omitted hardware barriers are rightly
rejected as reliable negative controls. Bounded campaigns, retained failures,
independent snapshot rebuilds and preservation of Q30–Q32 mechanisms are
appropriate preparation. No timing, stock boot, general translator, direct
host-to-live-code patching or cross-generation conclusion is accepted.

## Substantive blockers

None for accepting these scoped sources, inferences and candidate preparation.
The declared unresolved memory-attribute/ordering argument, exact protocol,
experiment/oracle code, Q30–Q32 readiness and independent pre-execution review
remain prerequisites. This verdict does not waive or settle any of them.

## Editorial edits and limits

Only this review was authored. No experiment, VM or archived repository history
was accessed. Reading stayed within 30 minutes and at most twelve primary
documents. Apple Markdown copies under `/tmp/natemu-q33-review-*.md` and the
reviewer's earlier Intel PDF are navigation aids, not retained runtime evidence.
No full-current-Arm-ARM inspection is claimed; the M.c section's access method
and the articles' L.a basis are stated above.

## Returns

No return required for this source-only submission. Retain this reviewer for
corrections; concrete experimental code requires separate pre-execution review.

## Register-transfer source extension review — 2026-09-07

- **Screening:** (a) ordinary VMs share guest executable-memory and SMP cache
  publication requirements; QEMU TCG usually avoids this particular host-to-EL1
  response transfer. (b) documented register get/set and owning-thread run
  operations supply a possible transfer primitive, but do not supply a chunk,
  code-publication or retirement protocol. (c) this finding applies to the
  proposed host-translator/EL1-shim register-transfer candidate, not to all
  Hypervisor mappings or all emulator designs.
- **Reviewer/context:** `/root/track01_session_check`, separately delegated by
  `/root`; authored no Q33 proposal or producer code. Prior Q30–Q32 oracle work
  is disclosed; this is an independent review of the Q33 author's extension.
- **Material inspected:** the workflow, charter, full investigation and prior
  review; proposed claims 08–09; macOS 26.4 SDK allocation, mapping, permission,
  register and run declarations; the Arm sources and current foundation
  functions identified below. No Q33 code, VM, experiment or archived history
  was used. Review was bounded to 15 minutes, beginning at 07:22 UTC.
- **Verdict:** **accept claim 08 as the scoped source finding stated, and claim
  09 as an inference supporting candidate preparation only.** Register transfer
  can remove the response-byte staging dependency from a concrete submission;
  it does not close claim 06, establish runtime feasibility, or approve
  experimental execution. Claims 01–07 retain their previous statuses. No
  substantive blocker remains for this source-only verdict. The concrete
  method must address the obligations below before pre-execution acceptance.

### Independent source checks

Read `/Library/Developer/CommandLineTools/SDKs/MacOSX26.4.sdk/`:
`System/Library/Frameworks/Hypervisor.framework/Headers/hv_vm_allocate.h`,
`hv_vm.h`, `hv_vm_types.h`, `hv_vcpu.h`, `hv_vcpu_types.h`, and
`usr/include/arm64/hv/hv_kern_types.h`. Allocation offers
`HV_ALLOCATE_DEFAULT`, page-sized anonymous storage and default VM permissions.
Mapping and protection name IPA storage and R/W/X permissions. These inspected
interfaces do not state the host/stage-2 cacheability, shareability or an atomic
host/guest publication contract. Apple's public
[memory-management overview](https://developer.apple.com/documentation/hypervisor/memory-management)
and [mapping description](https://developer.apple.com/documentation/hypervisor/hv_vm_map(_:_:_:_:))
also describe mappings and permissions. This supports the limited wording of
claim 08, not a conclusion that the API is incoherent or that no higher-level
Apple contract could settle a particular use. A successful checksum experiment
would not establish unspecified platform-wide attributes either.

The same SDK's `hv_vcpu.h` documents 64-bit register values, owning-thread
`hv_vcpu_get_reg`/`hv_vcpu_set_reg`, and a blocking `hv_vcpu_run` that returns on
exit or cancellation. `hv_vcpu_types.h` supplies the individual AArch64 GPR IDs.
Thus one owning thread can receive a controlled exit, inspect a request, set
response registers and only then call run again. The stopped vCPU cannot
consume a partly completed series of setters while that thread remains outside
run. This is the specific inference accepted for claim 09; no multi-register
atomic API guarantee is claimed. The SDK also says `hv_vcpus_exit` can make the
next run return without guest entry when the vCPU was already stopped. An API
return is therefore not a guest-consumption acknowledgement. See the public
[register setter](https://developer.apple.com/documentation/hypervisor/hv_vcpu_set_reg(_:_:_:))
and [vCPU management](https://developer.apple.com/documentation/hypervisor/vcpu-management)
entry points; their JavaScript-only bodies were not treated as additional
inspected contract text.

Independently read Arm's [memory model guide v1.0, §§9, 13–14](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/Learn%20the%20Architecture/Armv8-A%20memory%20model%20guide.pdf?revision=58b1dd0a-3800-4218-b21a-f95a0332034c).
It distinguishes access permissions from cache/shareability attributes, warns
about incompatible aliases, and describes stage-1/stage-2 combination including
HCR_EL2.FWB. The current `experiments/track01/guest.c` `tables()`/`mmu()` uses
Attr0 `0xff` and shareable RAM descriptors; these are stage-1 inputs, not an
inspection of stage 2. Register transfer removes the newly proposed concurrent
host-store/guest-load response edge. It leaves the established guest RAM and
SMP premises in place; it supplies no new proof of arbitrary effective memory
attributes.

Arm's [cache-publication sequence](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/caches-self-modifying-code-implementing-clear-cache)
and [full synchronization across threads](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/caches-self-modifying-code-working-with-threads)
remain applicable after the guest stores the received instruction bytes. The
transfer API neither cleans those subsequent guest stores nor synchronizes
other executors. In particular, host reentry precedes the new guest stores and
cannot replace their later publication sequence. Retain explicit executor ISB
and exact maintained ranges in the initial method. This accepts the articles'
scoped explanation, not an audit of every Arm ARM feature or a hardware result.

### Required concrete-method changes and closure conditions

1. **Replace the staging-specific path explicitly.** The register candidate
   must revise method step 3 and finite case 7 rather than silently pass their
   shared-ready-word checks. Transfer the request's source bytes, dependencies
   and response bytes through the declared register ABI. Sending a pointer and
   then having the host read concurrently changing guest RAM would reintroduce
   the unresolved transfer premise. Retain byte packing/endian rules, finite
   chunk counts, entry/relocation bounds, and exact emitted instruction bytes.
   The guest may assemble/copy them, but may not regenerate the requested block
   or accept a host-computed answer. Closure requires the independent checker
   to connect the exported request, host emission, received guest bytes, actual
   block PCs and guest effects.
2. **Define whole-request and chunk ownership.** A source snapshot must remain
   coherent across request chunks; responses must bind the vCPU, unique
   transaction, offset, length, status and source identity/generation. Reject
   omitted, overlapping, duplicated, reordered, cross-vCPU or stale chunks
   without publishing executable storage or repeating guest effects. A register
   buffer being reusable after copy is different from its complete block being
   semantically valid. Closure requires explicit transitions, copy-before-ACK,
   immutable or rejected partial objects, and stale revalidation at publication
   and subsequent entry under the existing invalidation protocol.
3. **Cover failed setters and non-consuming exits.** Record every API outcome.
   A setter failure after earlier setters succeeded cannot lead to ordinary
   guest reentry with a mixed reply; the simplest permitted response is a
   retained controlled stop. Cancellation or an unrelated exit must not advance
   chunk ownership. Pin the exact helper instruction, exit classification and
   reported/resumed PC rule; never infer consumption from return-code success
   or advance an arbitrary exception PC. Closure requires synthetic rejection
   controls for these paths and a bounded failure policy in the code submission.
4. **Preserve recovery on immediate reentry.** Specify the helper's saved
   native registers, flags, stack effects and canonical guest continuation.
   A pending native IRQ can run before response registers are copied; it must
   preserve the payload and recover the guest at the declared boundary.
   Malformed-response handling and guest copy faults must likewise not expose
   partial host payload as guest architectural state. A finite protected
   register-consumption interval is allowed only with an explicit pending-event
   service rule that retains Q32. Closure requires actual before/after-reentry
   cases, correct native-frame preservation and exact guest frame/IRET checks.
5. **Preserve SMP progress without a stopped-owner cycle.** Inspecting current
   `experiments/track01/q30.c` `begin_change()`/`end_change()` shows why this
   matters: global changes wait for active-state quiescence and per-vCPU ACKs.
   A host request must not leave its owner holding the writer lock or an
   incomplete exclusive epoch. Host production must not wait for a peer action
   that itself requires the stopped owner's guest ACK. A bounded return-to-shim
   or other concrete protocol must let the owner discharge pending maintenance
   before claiming completion; the host cannot fabricate its TLBI/ISB ACK.
   Closure requires a dependency argument and measured peer progress while a
   real owner is stopped, including a peer source/mapping change and bounded
   recovery. Serializing all vCPUs for every transfer does not cover the required
   live-peer linking and miss scenarios.
6. **Keep publication and lifetime obligations separate from transport.**
   Received storage stays unreachable until byte/metadata validation,
   permission/TLB work and cache publication finish. Every peer entering the
   shared generated block needs the prescribed synchronization; merely giving
   each CPU a private copy does not establish shared live-link publication.
   Link acknowledgement must exclude previously loaded old descriptors, while
   source invalidation must reject obsolete semantics and preserve pinned fault
   or IRQ metadata. Closure requires all original live-link, late-response,
   source-alias, saved-continuation and exhaustion cases with explicit overlap.

These are requirements for the forthcoming concrete method and oracle, not
newly observed failures. Stopped-vCPU register delivery is sufficient to pursue
that bounded preparation without first resolving concurrent shared staging.
Shared staging remains an open alternative; register transfer's cost, general
reclamation, translator integration and platform-wide applicability remain
outside this source verdict. Only this review file was changed.


## Independent first-native-run review — September 7, 09:06 UTC

Screening(a): emulators share the obligation to retain complete helper results;
this observed omission is in the finite shim's metadata, not a novel native
platform limitation. Screening(b): no undocumented hardware capability is
needed to explain it. Screening(c): the return applies to Q33's use of Q30
byte-fetch physical identity in accepted producer build 03 and its synthetic
model; it makes no platform, translator or performance claim.

Reviewer `/root/track01_understanding_check` independently read sealed
`q33-code-01`, its manifest/logs and binary trace, and verified all 86 inventory
entries, SHA256 `3891ea57be431b43c95a59233d845375c50c12cdbe981fb36ddd638137570a91`.
The native host failed after 2.513253209 seconds with Q33failure26 and zero host
code requests/entries/effects. Q32 checkpoint host checks reported 1420424
records; no full independent raw check was run for this failed candidate.

CPU0 first byte completed at END1420999 with value 0x48, GPA2678816 and one
native fault. Its next BEGIN1421005 at RIP+1 completed at END1421010 with
value 0x05/status 0/no native fault but GPA~0. REJECT 1421016 then reported reason 5
with only 0x48 assembled. This is the exact source path: q30_access initializes
GPA~0; its successful execute prewalk did not store walk.gpa, whereas the fault
handler did. The cached second byte therefore returned correct data with
missing physical identity, and source_read rejected it before host production.
The model always populated the walk GPA and did not expose this native-path
omission. The proposed under-lock assignment and independent v5 cached-fetch
replay controls address that discrepancy without weakening expected results.

Verdict: **retain the failed run and return for the minimal corrected-source
review; no Q33 observed finding is accepted.** The campaign correctly stopped
after one of six VM slots, with zero full checks admitted and its original
13:33:22.062654 UTC deadline unchanged. Prior accepted Q30–Q32 guest semantics
are not contradicted by missing optional cached-read metadata; the new Q33
composition was not yet accepted. Fresh corrected primaries, independent
replica preparation/runtime and final findings review remain necessary.

## Independent corrected-run timer witness return — September 7, 09:32 UTC

Screening (a): an emulator or ordinary VM also distinguishes a timer's asserted
condition from an interrupt-controller register snapshot. Screening (b): Arm
documents the timer status needed here; this return does not infer an internal
Hypervisor implementation or a platform defect. Screening (c): the distinction
applies to Q33's stopped-owner observation and immediate response-copy interval,
with Q30–Q32 and the complete Q33 continuation obligations still required.

Reviewer `/root/track01_understanding_check`, independent of Q33 producer and
oracle authorship, inspected the retained native evidence during the bounded
09:21:39–09:41:39 UTC window. All 86 inventory entries of `q33-code-02` verified
(inventory SHA256 `ec3edb4697c63397b1838a939ba8231727ff1183ca0f63197a1d031913e7091b`),
as did all 11 entries of failed `q33-code-02-check-01`
(`ef5840c53e111ea10e98ceb9fc9ad81cc38071450f271155c0f82a7c80cc7958`).
The producer 04/oracle 02/supervisor 01 run reached 448 global cases and 2,210,493
records with host PASS after 4.873967292 seconds. Its full checker correctly
returned failure under its submitted predicate at 09:22:04 UTC: the timer was
not observed as GIC pending 0→1. The campaign stopped and replication stayed
inactive; this host PASS is not an accepted Q33 result.

Direct inspection found all 32 stopped timer pairs had GIC pending 0→0 and
active 0→0, but CNTP_CTL_EL0 changed 1→5 with the same comparator. All 32 SGI
and 32 SPI pairs had pending 0→1 and active 0→0. All 96 interrupted WAIT replies
had native ELR exactly `_q33_transfer_resume + 4`, before the first response
STP at +72, followed by four CONTROL 11 records whose 16 words exactly matched
the host-set response. For example, CPU0 transaction 353 had native record 1750266
and first copied-word audit 1750268; CTL changed 1→5 with CVAL820595528960.
This is evidence of actual early delivery and preservation of these WAIT
responses. Their X4–X7 data lanes are zero; it is not a direct arbitrary
nonzero code-payload interruption test.

Arm's [Generic Timer guide, ARM062-1010708621-30, §§3.3–3.4, pages 9–10](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/Learn%20the%20Architecture/Generic%20Timer.pdf?revision=c710e7a7-9f52-4901-8c9d-91b19f44f9c7)
defines timer condition assertion by the counter reaching CVAL; ENABLE and
IMASK control interrupt generation, while ISTATUS reports that condition when
enabled. [Arm's control-field reference](https://arm-software.github.io/CMSIS_5/Core_A/html/unionCNTP__CTL__Type.html)
identifies ENABLE, IMASK and ISTATUS as bits 0,1,2. Thus CTL1→5 supplies the
missing *timer-condition* transition. Neither this contract nor these
sequential snapshots proves when Hypervisor updates a stopped vCPU's GICR
pending view. The subsequent exact native interrupt supplies separate delivery
evidence; the claim must not say its GIC pending bit was already latched while
stopped.

The smallest defensible remedy is a reviewed timer-specific observation rule:
require CTL low three bits 1 before and 5 after, unchanged CVAL equal to the
actual guest CONTROL 8 arm, the same stopped/masked owner and transaction,
before pending 0, both active 0 and consistent raw GIC bits. The timer's after
pending bit may be 0 or 1. SGI/SPI retain their pending 0→1 requirement. Keep
the exact source/INTID, pre-first-STP ELR, all 16-word comparison, source
invalidation, frame/IRET and successful fresh generated continuation checks.
Closure needs positive timer controls for both after-pending values and
negative controls for already-expired, disabled, masked or nonexpired timers,
changed/unbound comparator, wrong source/INTID and late delivery, together
with existing SGI/SPI and copied-response controls. Exact revised oracle inputs
must receive pre-execution review before a budgeted same-RAM recheck. No
producer change or extra VM is justified by this mismatch alone.

Independent inspection also confirmed real live-link overlap in all 32 group 3
cases: the peer entered the old target before replacement, finished its old
effect after replacement but before its own ACK, then used the new target
after all ACKs (256 checked post-ACK traversals). Case(3,0,0) witnessed records
1515430 < 1515431 < 1515447 < 1515553, with final ACK1515554. Inspection details
are in `/private/tmp/q33-review-primary02-witness.json` and
`/private/tmp/q33-review-primary02-links.json`; the sealed raw files remain the
authoritative evidence.

Verdict: **source/method remedy acceptable for preparation; return the exact
oracle delta for independent review, and withhold observed Q33 acceptance.**
No implementation was edited, no VM or full recheck was run by this reviewer,
and the original campaign deadline and consumed slots remain unchanged.


## Same-RAM timer-return closure — September 7, 09:48 UTC

Screening (a): the resolved discrepancy concerns the chosen evidence predicate,
which emulators and ordinary VMs must also bind to their observed source.
Screening (b): documented timer condition plus actual native delivery supplies
that evidence; internal GIC latch timing is not inferred. Screening (c): this
accepts the narrow timer return and one composed primary result, not Q33's
final feasibility finding or a broader interrupt/payload guarantee.

Independent reviewer `/root/track01_understanding_check` verified all 11 entries
of `q33-code-02-check-02`, inventory SHA256
`744211dddd7f3b1032dbb7cef77dff130bef2206f67c67073ea3e8dfc730466b`,
its complete PASS report, supervisor return 0 and exact oracle 03 source copies.
All 86 raw run 02 entries and all 11 earlier failed-check entries were rehashed
unchanged. The supervised check finished 09:46:16 UTC in 95.360155375 seconds,
peak sampled process-group RSS6,084,050,944 bytes, within 1800 seconds/10 GiB.
It reports 448 global/896 CPU Q33 cases, 177,332 cumulative operations and
2,210,493 records, with unsupported[]. Nested Q30/Q31/Q32 checkpoints report
PASS 864/544/592 finite cases and unsupported[]; the two-CPU Q32 applicability
limits remain explicitly declared, not newly excluded cases.

The narrow timer return is **resolved**: the source-supported oracle 03 accepts
exactly the same retained RAM after independent delta review and rejection
controls. Producer 04 did not change, and neither failed evidence nor its
original oracle was overwritten. Earlier direct checks of 96 actual WAIT
responses/early ELRs and 32 live-link overlaps corroborate the full report.
This closes the identified observation mismatch without claiming GIC timer
pending was latched while stopped.

**ACCEPT resumption of the remaining required primary 2,2,4 pairs** using
producer 04/oracle 03/supervisor 01, within the original campaign's six-VM/eight-
check caps, 13:33:22.062654 UTC deadline, 45-minute pair admission and fresh 10 GiB
per-VM disk floor. Two VM slots and two full checks are already consumed.
Stop again on a failure or missing coverage. Replica preparation may be
accepted separately, but its activation remains with the parent. Final Q33
findings acceptance still requires the remaining primaries and independent
2/4-vCPU reproduction; this reviewer ran no additional VM or full check.


## Independent run 04 prefix failure diagnosis — September 7, 09:55–09:57 UTC

Screening (a): asynchronous arrival relative to a native wait instruction is a
fixture/evidence obligation shared by emulators and ordinary VMs. Screening
(b): the observed native timer delivery requires no undocumented capability;
this trace does not identify a platform failure. Screening (c): the return
concerns Q32's retained after-WFE halt-wake case within the Q33 composed suite.
It is upstream of all Q33 production, and cannot be ignored because prior Q33
pairs passed.

Independent reviewer `/root/track01_understanding_check` verified all 85 entries
of `q33-code-04`, inventory SHA256
`9ce9be6f6ae1062f873d54b06969f29fb15afeccc3b36235b20199d059d7b72e`.
The retained source and native bytes identify Q32 group 7/variant 2/repetition 4,
CPU0, with 338 global cases already complete. Q32 failure 53 maps through its
check helper to Q30 failure 0x3235 and foundation failure 0x303235. Q33 remains
phase 0 with no cases, requests, code allocations or entries.

The final native sequence is decisive: records 1130536/37 publish HLT and the
eligibility recheck;1130538 emits the timer payload sequence 67;1130539 records
actual physical INTID30 at stage 5/native-count 147, ELR0x400019e4;
1130540 accepts source sequence 66→67 into the virtual IRR. The native word at
0x400019e4 is 0xd503205f, the WFE instruction. `_q32_wait_after` is 0x400019e8.
The interrupt therefore arrived before that WFE rather than with the required
post-WFE return PC. `receive()` accepted the interior location, exact native
count and stage; `halt_case(2)` then failed its stricter exact-PC assertion 53.
The independent frozen checker has the same post-WFE requirement, so accepting
this earlier PC would discard the declared coverage obligation.

CPU0's HVC#49 failure exit and final state precede CPU1's later watchdog
cancellation. CPU0 already had the asserted failure, while CPU1 remained live
and accumulated further timer ticks. The timeout is a consequence of the
stopped peer and incomplete suite, not evidence that a missing timer or lost
wake caused the original assertion. Earlier repetitions 0–3 in this same run
contain eight proper post-WFE native witnesses; the ninth, CPU0 repetition 4,
is the first mismatch. That history demonstrates intermittent fixture arrival
placement, not a reason to exclude the failing observation. Direct diagnostic
extraction is retained in `/private/tmp/q33-review-run04-halt.json`.

**Return: retain this failure and keep the campaign/replication stopped.**
The known cause is missing required after-WFE coverage from the current
one-millisecond asynchronous timer arrangement; the trace does not establish
why its scheduling reached the earlier instruction on this invocation.
A revised candidate must make the desired wait/notification ordering explicit
and retain a concrete native witness, or explicitly return the unestablished
case. Merely broadening the accepted PC, retrying until a favorable sample,
or calling this a watchdog-only failure is insufficient. No specific code
remedy, revised budget or new execution is accepted by this diagnosis.
The affected Q32 composed-repeatability conclusion is reopened; prior retained
positive runs remain evidence within their actual scope. Q33 remains unresolved.
No VM, full oracle or implementation change was performed by this reviewer.


## Corrected primary evidence and actual HLT recovery — September 7, 10:33 UTC

Screening (a): retaining early arrivals and complete guest service is a shared
VM/emulator correctness obligation. Screening (b): the observed native PCs,
stopped source state and executed code use the reviewed capabilities without
inferring a platform scheduling guarantee. Screening (c): all conclusions here
remain finite Q30–Q33 composition on the recorded M4; independent replication
and the final claim review are still pending. The reviewer authored no Q33
producer or oracle and ran no new VM/full checker.

All 86 raw and 11 check inventory entries were independently rehashed for each
of primary 03,05,06. The exact inventory identities are:

| Run | Raw inventory SHA256 | Check inventory SHA256 |
| --- | --- | --- |
|03|`124e45dda332741a3d1ae55f56f06a318fb93c524c31a225b106729529d1e967`|`17b353d7c08f30ba6eb29d27dbd00cff615bfee4457199eefaf78979bc3b744f`|
|05|`c85876afb17f0fdbc4505b3a87451945b55d0bfff61585b10f6f2dbc644d2fca`|`58f01be25c330a9d40183c4e21b35c75e979992eba3d0e27bbdb06e49bcf444b`|
|06|`fa75f51d49673a6aeabbc14b81aa697d739086968d1cdfc9c79a493929fe4fda`|`2e5e05d3d7a5e53d59026a45cf665959f08c08c7ca12e35695e61764d5962f53`|

All three complete reports and their nested Q30/Q31/Q32 checkpoints report
PASS/unsupported[]. Primary 03 and 05 each cover 448 global/896 CPU Q33 cases;
primary 06 covers 448/1792. Full records/operations are 2,209,438/177,272,
2,211,018/177,382 and 4,591,481/359,482. The four-CPU Q32 checkpoint covers all
2368 CPU cases with no declared non-applicability. Actual supervised checker
elapsed times 94.5314/94.4475/169.7533 seconds and sampled RSS peaks
6,092,324,864/6,092,996,608/7,366,787,072 bytes satisfy the accepted limits.
Parent's slightly larger diagnostic wrapper times are not performance claims.

Direct mmap inspection confirms primary 05 group 7/variant 2/repetition 2 CPU1
contains two complete HLT trials. Native record 1124678 has physical timer
INTID30, stage 5, count 139 and early ELR0x400019cc; CONTROL 15 at 1124680
classifies it early against unchanged post-WFE0x400019e8. Actual dispatch
1124763, EOI1124851 and IRET1124913 complete before retry. All 21 canonical
words in restored snapshot 9397 equal committed HLT snapshot 9386. Actual
FETCH8 operations 1124938/1124944 return EB/FD; retry attempt 2694 commits
at 1124949 with only RIP0x10023501→0x10023500 changed. Fresh HLT2695 then
emits the next timer; native 1124975/count 140 and CONTROL 15 at 1124977
qualify exactly at 0x400019e8. Its distinct dispatch 1125050, EOI1125138 and
IRET1125200 complete, restoring the same canonical state. No first arrival
or service was discarded. Across primary 05 there are 33 trials in 32 CPU
cases; primary 06 has 64 trials in 64 CPU cases, all first-trial success.

This establishes **one observed native early-arrival recovery** through the
source-reviewed bounded sequence. It does not establish the frequency of
early arrivals, guaranteed timer placement, success within eight under every
schedule, or native exhaustion coverage. The retained eight-early synthetic
negative remains the evidence for cap-exhaustion service and failure. The
original run 04 failure remains retained and correctly diagnosed.

The four-CPU Q33 trace independently corroborates 96 actual old-peer
entry<replacement<old-effect<own-ACK overlaps and 512 post-all-ACK traversals
to the replacement target. Example group 3/variant 0/repetition 0 CPU1 has
3161544<3161545<3161594<3161921. For all 192 delayed-response cases, 64 each
timer/SGI/SPI, actual native ELR is transfer-resume+4, before the first
response STP, and all 16 copied words equal the exact host response. Timer
CTL1→5 retains the same CVAL; SGI/SPI show pending 0→1; all begin quiet and
inactive. Example CPU0 transaction 353 has native 3640879 before copy 3640881.
These are the declared WAIT words, including zero payload lanes, not arbitrary
nonzero code payload interrupted at every copy instruction. Direct extraction
records are `/private/tmp/q33-review-primary-hlt-summary.json` and
`/private/tmp/q33-review-primary06-witness.json`; immutable raw artifacts remain
authoritative.

The corrected primary evidence supports closure of the identified HLT return
within its finite scope, conditional on the required independent 2/4 results.
The previously accepted mixed-input sampling disposition applies:02/03 use
producer 04/oracle 03;05/06 use 05/04. No failed sample is excluded or original
six-VM campaign renewed. **No final Q32 reclosure or Q33 ACCEPT yet:** inspect
independent replication and the parent's explicit final claim first.


The reviewer additionally compared every 21 runtime source files and 12 compiled
artifacts in 03/05/06 with their designated frozen 04/05 builds, and every
checker source with its designated oracle 03/04 plus supervisor 01: all are
byte-identical. CLM-01-33-10's submitted scope matches the primary evidence
and explicitly excludes shared staging, reclamation, W^X, arbitrary code/ISA,
stock boot, other hosts and performance. Its exact WAIT-register and timer
condition limits are necessary and accurately retained. Final acceptance
continues to await independent replica outcomes.


## Final independent findings acceptance — September 7, 21:05 UTC

Screening (a): QEMU TCG and ordinary VMs share executable publication and
lifetime obligations; this question isolates their composition across the
host-translator/EL1 boundary. Screening (b): documented mapping, register and
cache controls supply components, while the finite protocol and continuation
correctness require the retained observation. Screening (c): the verdict
applies to the register-transfer EL1-shim mechanism on the recorded M4 at two
and four vCPUs, not the shared-staging hypothesis or all emulator designs.

Reviewer `/root/findings_review` is a fresh independent context, with no
producer, oracle, supervisor, primary or replica authorship. The reviewer read
the workflow, charter, Q32/Q33 investigations and existing findings and final
pre-execution returns, inspected the retained corrected halt producer/oracle,
and independently checked the final replica evidence. Prior reviewer
`/root/track01_understanding_check` supplies the earlier substantive primary
and source inspections in this same thread. No new VM or full acceptance
checker was executed; read-only hashes and focused direct trace extraction
were performed. No archived material was consulted.

All 86 artifacts in each replica and all 11 in each checker rehash correctly:

| Replica | Raw inventory SHA256 | Check inventory SHA256 |
| --- | --- | --- |
|01|`10aa4e81be6d1c80d43d92e09258c9dbe8b586c5b68cb855a3d6756500f128c5`|`714f4ea70d4a1cf1eecae6a52ccda01d742ffa6ad759e216ccfb1eabd2429722`|
|02|`f2025cac960facccc8c460fbfa14129325f680f9929854f7f61af79e415519ce`|`47be5778ea18b5f69dc86d1b8af88ba793d404997a71c5edc7424294fde0995a`|

Their 21 runtime sources and 12 build outputs (including the entry record)
match the independently prepared replica-build 03 byte-for-byte. All checker
sources match oracle 04 and supervisor 01. This is independent rebuild/runtime
replication with the same independently reviewed oracle; the replicator's
oracle authorship does not become an independent second oracle implementation.
Preparation 04 was separately accepted before activation, as recorded in the
pre-execution thread.

Both complete reports are PASS with unsupported[] at Q33 and every nested
Q30/Q31/Q32 checkpoint. Each covers 448 Q33 global cases, with 896/1792 CPU cases
at two/four CPUs, 177,388/359,499 cumulative operations and
2,210,834/4,590,876 records. The Q32 checkpoints retain 592 global cases,
1,088 applicable plus 96 explicitly non-applicable CPU-cases at two CPUs and
all 2,368 at four. This is the declared host/vCPU matrix, not an exclusion of
unfavorable cases. Supervision binds the verified VM inventories, actual
oracle outputs and return 0; elapsed 94.478/172.870 seconds and sampled RSS
6,133,907,456/8,681,504,768 bytes remain inside 1800 seconds/10 GiB. Durations are
diagnostics only.

The 10-entry activation inventory also rehashes to
`0351f6d26300035752f2e65efd23b8a8ddf360976d16be9e0d0f681b96f88e66`.
Its four command records show exactly two VM/check pairs, 10:30:20–10:35:10 UTC,
all return 0 with adequate disk and admission time. The copied limits retain
preparation-era wording "not activated"; the top-level passed status and actual
command chronology establish execution. This stale descriptive field does
not invalidate runtime facts and was not edited in the immutable artifact.
The original primary campaign's failed 01/04 and failed 02-check-01 retain their
previous reviewed dispositions; mixed producer 04/oracle 03 and 05/04 successful
primary identities are explicitly preserved, with no reliability pooling.

Direct inspection independently corroborates every 32/64 replica group 7/variant 2
CPU-case: physical timer INTID30 at stage 5 has native ELR exactly
`_q32_wait_after=0x400019e8`, followed by matching trial 1 classification,
actual guest dispatch, EOI and IRET. Example replica 01 CPU0/repetition 0
sequences are 1117553 <1117555 <1117638 <1117726 <1117788; replica 02's are
2317417 <2317419 <2317502 <2317590 <2317652. Neither replica contains an early
trial. The mandatory witness is independently reproduced, and Q32's coverage
return is closed in its owning findings thread. Primary 05's single native
early-arrival recovery remains explicitly unreplicated as a timing event;
independent source and forced synthetic checks establish the bounded retry
semantics without asserting a frequency or schedule guarantee.

The reviewer also inspected and reran the existing focused four-CPU raw
witness extraction. It confirms 96 live old-peer entry<replacement<old-effect<
own-ACK overlaps and 512 replacement traversals after all ACKs. One example is
3161699 <3161729 <3161777 <3162106 for CPU1/group 3/variant 0/repetition 0.
All 192 delayed-response CPU-cases (64 each timer/SGI/SPI) retain actual native
ELR at transfer-resume+4, preceding the first packet STP, and all 16 copied
words equal the host WAIT response. Timer CTL1→5 has unchanged CVAL;
SGI/SPI pending changes 0→1, with the owner initially quiet and inactive.
CPU0/transaction 353 has native 3640827 before copy 3640829. These corroborate
the independently reviewed full oracle; they do not replace its broader
source, mapping, emitted-byte, canonical-state and continuation checks.

**ACCEPT CLM-01-33-10 as written and resolve Q-01-33 as a scoped yes.** The
claim's actual WAIT-register contents, timer-condition distinction, finite
immutable lifetime and generated ADD/MOV templates match the evidence. It
establishes neither arbitrary nonzero payload interruption at every copy PC,
shared staging, memory reclamation, W^X, a general translator/ISA, stock Linux
boot, performance, deterministic placement, nor other-generation support.
All load-bearing feasibility observations have the required independently
reviewed reproduction. The one primary-only early-recovery observation is
corroborative and does not support an unreproduced broader claim. No substantive
Q33 return remains; named downstream owners and Q34/final breadth work remain.
The reviewer updated only Q32/Q33 findings and investigation summaries to make
the accepted current state explicit; historical failure records were preserved.
