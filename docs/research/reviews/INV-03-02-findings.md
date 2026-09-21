# INV-03-02 — findings review

- **Reviewer and independent context:** `/root/memory`, separate from author
  `/root/translator`. I authored INV-03-04, not this translator comparison or its
  implementation. I supplied a pointer to an existing FEX source comment during
  review; the author independently added its narrowly qualified interpretation.
- **Subject:** current [INV-03-02](../investigations/INV-03-02.md), claims 01–07,
  provisional tradeoff, falsifiers, licensing warnings and evidence limits.
- **Date and verdict:** September 19, 2026 local time: **accept the scoped source
  findings and explicitly conditional inferences only**. Translator selection,
  adapted-code correctness and whole-architecture acceptance remain open.

## Evidence inspected and disposition

I inspected the cited QEMU, FEX and Box64 source files in the author's downloaded
convenience tree, following the actual functions and surrounding control flow.
I also independently downloaded the pinned QEMU `fpu_helper.c` and inspected
`cpu_init_fp_statuses`; it agrees with the submitted current-revision account.
Attempts to refetch all other files independently failed through DNS/cache
errors; this limits independent retrieval, not inspection of those primary-source
contents. The immutable links in the investigation identify the source versions.
Official LLVM ORCv2 and Statepoints documents were independently opened.

| Claim | Independent assessment |
| --- | --- |
| 01, system semantics | Accept source presence and the system/frontend distinction. `CONFIG_USER_ONLY`, system-helper stubs, the frontend context and `i386_tr_insn_start` support the proposed audit priority. `seg_helper.c` actually calls `cpu_ld*`, `cpu_st*` and exception routines; its helpers are not freestanding functions over a state object. No startup32 execution result follows. |
| 02, recovery | Accept. `cpu_unwind_data_from_tb` decodes instruction metadata; `cpu_restore_state_from_tb` delegates target restoration; `cpu_restore_state` explicitly rejects PCs outside its generated-code region and identifies helper calls without `GETPC()` as unresolvable there. These mechanisms do not independently reconstruct arbitrary temporary state after changing lowering. |
| 03, execution-domain ABI | Accept source fact: `tcg_out_call_int` emits BL or address materialization plus BLR. Accept the inference that host production can target in-VM helpers only after relocation/state/exception dependencies are met. This is not an implemented ABI or assurance that every helper can be extracted cheaply. |
| 04, inherited FP | Accept the narrow current-source account. The SSE NaN rule TODO remains; the inspected code sets FTZ detection after rounding. Presence of a TODO is evidence of a known upstream discrepancy, not a complete instruction-by-instruction mismatch inventory. No floating-point compatibility waiver has been granted. |
| 05, FEX | Accept existence of JIT RIP metadata, Linux signal/state machinery, non-default reduced precision and the specifically inspected F80 exception handler. The code-tail comment describes accurate RIP correspondence while signal code contains the narrower caution; the record properly refuses to infer full-state recovery or universal imprecision from either alone. |
| 06, Box64 | Accept documented configuration and recovery machinery. Application-oriented defaults and library wrapping justify additional audit work; they do not establish that a system port is impossible or quantitatively more expensive. The comparison preserves this distinction. |
| 07, LLVM | Accept the limited contract comparison: JIT/linking and GC safepoints do not themselves specify x86 system semantics. No LLVM backend quality, impossibility or adaptation-cost conclusion is accepted. |

The source license statements are recorded as provenance warnings, with per-file
and vendored-component qualification. They supply no general legal conclusion.
The proposed QEMU-first ranking is acceptable **as the next investigation**, not
as an accepted implementation direction. It follows from the already-present
system semantics, while the record leaves portability, precision and actual cost
unresolved and retains FEX as a serious alternative.

## Adversarial challenges and substantive blockers

There is no unresolved blocker to the **limited source comparison**. The following
are substantive blockers to translator/architecture selection, already disclosed
in the investigation; this verdict does not close them:

1. **Helper closure across the execution boundary.** A helper with a valid guest
   target can still call host-only runtime state, locks or exception unwinding.
   `seg_helper.c`'s memory APIs and `GETPC()` users are concrete counterexamples
   to “relocate call addresses and the port is done.” Remedy: reachable-helper
   inventory with execution domain, memory API, exit/fault and commit contract.
   Closure requires the selected exposed profile's paths to fit that contract.
2. **Recovery after changed memory lowering.** Existing PC maps cannot prove
   vector temporaries, deferred flags, partial stores or MMIO replay are correct
   after moving faults into EL1. Remedy: trace the six named instruction families
   against a complete guest-state/commit model; classify instruction-specific
   required versus optional partial effects. Closure requires every selected
   recovery strategy to preserve the exposed behavior, including helper faults
   and interruptible long operations.
3. **Atomicity and CPU-profile coupling.** FEX configuration explicitly separates
   vector/memcpy TSO and split-lock policies. Apple TSO does not establish native
   atomicity or automatically validate deleting those mechanisms. The memory
   audit additionally identifies AVX-dependent aligned 16-byte guarantees.
   Remedy: inventory emitted access widths/alignment and ordinary-observer
   interactions; use independent source contracts or bounded evidence for the
   final lowering. Hiding AVX cannot hide baseline x87/SSE2 requirements.
4. **Inherited FP policy.** “Use upstream” does not decide the known SSE discrepancy
   or establish exception/save-restore behavior. Remedy: reconcile the actual
   inherited behavior with the exposed CPU and workload requirements. Closure
   needs a supported compatibility decision or a corrected inherited path with
   independent validation, not merely a conservative configuration label.
5. **Economic comparison.** A larger integration surface is a qualitative
   inference, not person-months or performance. Profile-matched cost evidence
   remains required before superior economics or definitive candidate rejection.

The next source audit is concrete and relevant. Its current review allocation is
bounded to two reviewer-hours; no runtime experiment is proposed or authorized.
No replication is required for these source-only findings. New load-bearing
runtime observations will require the workflow's reviewed method and independent
replication. Track 01's handwritten templates cannot discharge these blockers.

## Editorial edits and limits

No edits to INV-03-02 and no executable changes. I did not audit all helper
families, every license header, LLVM backend internals, a stock guest boot or any
runtime performance. Those omissions are explicit limits, not silent acceptance.
The temporary source-download failure adds no experimental negative result.

## Returns

None. Recheck substantive changes in this same thread before widening the verdict.

### Return 1 — Supplement A, helper/recovery source audit

**September 19, same independent reviewer: accept CLM-03-02-08/09 as scoped
source findings and the qualified transformation counterexample.** Inspected
new pinned-source copies for `include/tcg/tcg-opc.h`, `tcg/tcg.c`,
`target/i386/tcg/tcg-cpu.c`, `helper.h`, `access.c`, `excp_helper.c`,
`accel/tcg/cpu-exec-common.c`, and the named frontend/vector/REP/x87 expansions.
No implementation or architecture acceptance follows.

Operation flags and `sync_globals` substantiate the distinction between
faultable guest-memory IR and ordinary state-memory IR. The x86 recovery callback
restores PC and condition-code operation; it is not a general native-register
location decoder. The `mov` sequence is correctly labelled a counterexample
schema to preservation under the proposed IR weakening, not an observed compiler
trace. Retaining `qemu_ld/st` and their flags while modifying backend expansion
avoids this particular loss; it does not prove the new native fault ABI.

The six-family inventory is explicitly sampled. The inspected YMM load expansion
uses both temporary loads before CPU-state stores, unlike its two-memory-store
expansion. `gen_movs` updates indices after load/store; `do_gen_rep` publishes
count/CC state per completed iteration and bounds redispatch with `REP_MAX` under
its stated conditions. None proves a host/shim safepoint between arbitrary native
instructions or justifies replay of an irreversible prior MMIO effect.

`cpu_loop_exit_restore` restores from the supplied PC then calls `cpu_loop_exit`,
whose `siglongjmp` uses the CPU runtime context. Relocating only helper call
targets therefore cannot preserve an exception exit into an incompatible stack/
address domain. The proposed in-VM continuation is a required design mechanism,
not an assertion that a C jump facility is intrinsically unavailable in a VM.

The supplement follows its stop-at-first-counterexample budget, identifies a
specific unsafe transformation and revises the research direction without
claiming full closure. The subsequent fault-ABI protocol still needs independent
review and must compose with INV-03-04's drain rule: no `PARKED` acknowledgement
from a fault path with a live temporary, native return, or required incomplete
side effect. Scope of this acceptance does not include any later, uninspected
supplement or a final guarded-EL1 generator contract.

### Return 2 — Supplement B, proposed direct-access ABI

**September 19, same independent reviewer: accept CLM-03-02-10 only as a
conditional preservation argument for its sampled source ordering.** No native
fault-handler implementation or combined park protocol is accepted. The scalar
argument preserves the source's synchronization point; the YMM argument preserves
uncommitted first-half temporary state on repair and abandons it on a true guest
fault. Those are valid distinct cases under the stated no-MMIO/no-replay limits.
A native snapshot is not itself a canonical x86-state snapshot.

**New composition blocker:** suppose a vCPU faults on the second YMM memory read
while global closure is pending. It holds the first temporary half and a native
continuation. Supplement B correctly requires repair and native resumption;
INV-03-04's coordinator currently waits for canonical park before granting
exclusive maintenance. If installing the missing shadow needs that coordinator,
the vCPU cannot reach canonical park and the coordinator cannot service it.
A finite timeout contains this cycle but does not make valid guest execution
correct. Smallest remedy: define a retained-continuation quiescence/drain-service
protocol that can resolve this fault without reclaiming its code/metadata or
allowing conflicting observers. Closure requires the actual state transition
and a schedule covering simultaneous source mutation/queued write, not a renamed
acknowledgement. This blocks the combined ABI, not the narrow source finding10.

The proposed address-check clause also needs mode-specific qualification:
protected32 segment/address-size behavior must not be replaced by a blanket
long-mode canonicality check. This is already required by the combined candidate;
it should be explicit before that ABI clause is accepted.

### Return 3 — mode correction and Supplements C/D

**September 19, same independent reviewer:** mode qualification from return 2
is addressed: Supplement B now explicitly applies segmentation/canonicality only
where the current guest mode requires it. Its final paragraph preserves the
retained-native-state composition blocker. INV-03-04 revision 2 proposes a remedy;
its separate independent review governs that protocol, not this verdict.

**Accept CLM-03-02-11 as a source-derived single-case discrepancy.** I downloaded
Intel's cited Volume 1 PDF directly and verified its 253665-078US identity and
Table 4-7/footnote. I followed the pinned decoder/helper operand order,
`float64_gen2` exclusion of NaNs from the hardware arithmetic path,
`partsN(pick_nan)` and its x87 larger-fraction choice. Both proposed inputs are
quiet positive binary64 NaNs, so the second input's larger payload selects `...2`
under the inspected QEMU rule while the cited legacy SSE contract selects the
first operand `...1`. This is stronger evidence than a TODO alone but remains a
source derivation, not a hardware observation or runtime reproduction.

The existing `float_2nan_prop_ab` selector is a plausible inherited-machinery
remedy for this sampled path. Its availability does not prove globally changing
SSE status is sufficient: argument order and all enabled users must be audited.
The source's separate x87 status, FMA three-NaN rule and min/max comparison-based
selection must retain their own contracts. Neither a broad FP correction nor a
compatibility waiver is accepted here.

**Accept CLM-03-02-12 as scoped source facts and a conditional entry-poll
requirement.** I inspected `gen_jmp_rel`'s `goto_tb` before the non-PC-relative
EIP store, its PC-relative alternative, the edge flags, BB-end global handling
and `x86_cpu_synchronize_from_tb`. A linked target-entry poll cannot infer its
architectural RIP from stale `env.eip`; using target identity/CS or correctly
maintained PC-relative state is required. This supplies a source-supported
boundary-state direction, not an arbitrary interrupted-PC recovery proof.
Actual event eligibility, pending-state races, helper progress and the proposed
forced next-instruction exit for paused source mutation remain unestablished.

No changes to the author's investigation. No further runtime claims accepted.

### Return 4 — Supplement E, affected SSE NaN-policy families

**September 19, same independent reviewer: accept CLM-03-02-13 as the scoped
family partition and conditional selector-change argument.** No patch, runtime
result, complete FP contract or translator selection is accepted.

I inspected the pinned `ops_sse.h` arithmetic scalar/packed macros, horizontal
and add/sub helpers, `helper_dpps`/`helper_dppd`, min/max comparisons, reciprocal
paths and FMA macros; the corresponding SoftFloat two/three-NaN selectors and
finite fast-path guards; initialization/MXCSR status handling; and FMA frontend
operand permutations. I independently downloaded the cited Intel Volume 2A,
verified 253666-082US and read the DPPD/DPPS descriptions. Those descriptions
expressly leave horizontal NaN priority/position implementation-dependent.

The inspection supports the reported bounded family distinction:

- Arithmetic two-operand helpers preserve argument order into the existing
  two-NaN picker; `float_2nan_prop_ab` selects the first NaN, including a signaling
  first operand, with existing invalid/quieting behavior. It must not be confused
  with the signaling-priority `float_2nan_prop_s_ab` variant.
- Min/max selection and comparison results use their dedicated paths. Reciprocal
  operations have a finite constant numerator, so this selector change introduces
  no competing two-NaN payload choice there. This proves no broader approximation
  or exception behavior.
- FMA NaNs use the separate three-operand and infinity/zero policy. x87 and MMX
  statuses are separate objects. Changing only SSE's two-NaN initialization need
  not replace those policies.
- Dot-product masked products/grouped sums support the deterministic source
  result, but the primary specification does not promise a physical Haswell's
  exact horizontal payload selection. The limitation is correctly retained.

The source argument is adequate to retain the narrow inherited-selector remedy
for further design, without an owner waiver for the demonstrated ADDSD defect.
It does not establish exhaustive decoder reachability, every enabled instruction's
operand permutation, all rounding/exception modes or saved-state behavior. The
future implementation must validate both input orders and quiet/signaling cases,
including scalar/packed arithmetic and the excluded special families, against
independent expected outcomes. An unchanged-QEMU-only oracle would preserve the
known discrepancy. No such execution occurred in this review.

No author-file changes and no unresolved blocker to this scoped source finding.

### Return 5 — Supplement F, VM-local exceptional continuation

**September 19, same independent reviewer: accept claim 14's bounded source
observations and conditional sampled landing argument; no general helper ABI
acceptance.** I inspected pinned `cpu_exec_longjmp_cleanup`, the exit/restore
entrypoints and generated-code lookup, `raise_interrupt2` and page-fault payload
construction, `helper_divl_EAX`/its default declaration, and the non-null LLDT
descriptor-read sequence in `seg_helper.c`.

The source separates inner exit bookkeeping from outer page-lock/BQL cleanup.
The divide helper checks its two #DE conditions before committing EAX/EDX. LLDT's
shown second-descriptor-read fault leaves the old LDT uncommitted while native
first-word temporary state is still live. These support the submitted separation
of architectural origin from resumable native frame, and of repair/resume from
real guest-fault abandonment. Neither a helper PC without the originating token
nor unchanged architectural destination state proves a canonical/reclaimable
boundary.

A permanent nonreturning VM-local landing is a coherent conditional mechanism
for these paths if the record/pins/cleanup and stack-switch requirements hold.
The single mutable frame slot counterexample is valid when resumable native
nesting is possible. It does not prove a frame stack is mandatory if a resident,
nonnesting regime is independently established; the submission correctly leaves
that choice open. Cleanup authority and reference release must occur before
acknowledging a reclaimable park, and a guest fault must retain its architectural
origin independently of native repair activity.

Substantive remaining premises are preserved: enumerate cleanup effects for the
enabled helper/runtime set; establish nested trap/interrupt behavior and frame
capacity; ensure the landing itself does not fault on ordinary guest input; and
compose with coordinator phase ownership and partial-effect records. The scalar,
divide and LLDT traces do not cover MMIO descriptors, all exception priority,
exception-delivery recursion or arbitrary helpers. Later Supplement G is not
covered by this verdict.

### Return 6 — Supplement G, resident repair and walker ownership

**September 19, same independent reviewer: accept claim 15's bounded source
finding and conditional sampled nonnesting regime; no complete repair ABI or
runtime acceptance.** I independently downloaded the pinned
[`system/excp_helper.c`](https://github.com/qemu/qemu/blob/c1c18d1e640b64292859ce9f30f3c344edfb0294/target/i386/tcg/system/excp_helper.c)
and byte-compared it with the cited audit copy. I inspected `PTETranslate`,
`ptw_translate`, both PTE read widths, fast/slow A/D updates, walker restart
labels and `x86_cpu_tlb_fill`.

The source confirms the proposed extraction seam and its limitation. Translation
can produce an explicit error record before the later raise boundary, but slow
PTE loads still call memory accessors. `ptw_setl_slow` leaves CPU execution,
acquires QEMU exclusion, reads and conditionally writes through accessors with
return address zero, then releases exclusion and reenters execution. Its own
MMIO RMW question remains unresolved. These are concrete reasons why unchanged
walker reuse is not a proof of resident nonnesting repair under the candidate's
existing DRAIN_SERVICE authority. This is a composition counterexample, not an
assertion that QEMU's own exclusion protocol is inherently incorrect.

Separate retained guest/helper state and a fixed transient IRQ frame resolve the
earlier overwrite schedule conditionally: an IRQ can return to resident service
without destroying LLDT's first-word temporary or its guest origin. Sequential
guest-access faults need not imply recursive retained frames. This relies on the
submitted restrictions actually holding: no guest-shadow access in repair,
checked stable backing, resident metadata, no recursive guest helper, no second
coordinator acquisition and explicit device results. Unexpected private runtime
faults cannot be used to classify ordinary guest faults or valid MMIO as fatal.

The corrected finish-access table matches the parent access-outcome ABI.
`ACCESS_COMPLETED` must install the declared result/status and use a prepublished
emitter/accessor success continuation; it cannot retry a device read or committed
store. `RETRY_ACCESS` requires that the access remain unperformed. A true guest
fault abandons through the exceptional landing with original instruction origin
and earlier effects preserved. An arbitrary native PC cannot manufacture the
missing continuation/result layout.

Remaining premises are material and explicitly open: the complete invoked-call
and cleanup inventory, all A/D writers' tracking and invalidation, non-RAM PTE
semantics, shadow maintenance/publication, frame and reserve bounds, and actual
native IRQ/return critical sections. Host refill waits must not depend on the
paused operation releasing the very resources needed to resume it. Retaining
ordinary partial progress does not justify blindly replaying a walk that already
performed device effects. The conditional preference for resident nonnesting over
a generic frame stack is reasonable for these sampled paths, but establishes no
universal helper coverage or finite memory budget. No implementation or runtime
experiment occurred; no author-file changes or source-level return are required.

### Return 7 — Supplement H, walker and direct gateway inventory

**September 19, same independent reviewer: accept claim 16's scoped source
inventory and conditional extraction boundaries.** I reread the full pinned
walker and independently fetched `accel/tcg/cputlb.c`; inspected its probe,
route installation, dirty tracking, MMIO preparation and dispatch-result paths;
and cross-checked the submitted accessor wrappers, `tb-maint.c`, memory dispatch
and x86 TCG hook initializer. The inventory's declared completion is direct
walker/gateway coverage, not recursive coverage of every host callback or all
enabled helpers. The submission preserves that distinction.

The new source counterexample is confirmed. The STORE-intent, size-zero PTE
probe can run `notdirty_write` for size one and invalidate translated source
before returning a writable alias. The invalidation's current-TB recognition
requires a nonzero native return address; this probe supplies zero. Consequently,
neither treating the probe as pure lookup nor importing it unchanged establishes
this port's all-writer and next-boundary-stop contracts. The explicit preparation
boundary must cover every A/D mutation through every eligible alias while keeping
the admitted instruction's code/metadata alive. Conservatively preparing a page
does not mean a guest-visible A/D write necessarily occurred.

The readonly/ROMD distinction is also supported: backing with a host address can
still require discard-write or device-write handling. A private writable alias
cannot turn it into ordinary RAM merely because lookup found bytes. TLB resize
and victim-entry movement support the stated prohibition on retaining raw
`CPUTLBEntryFull` pointers across service. A stable owned routing capability must
carry the topology/lifetime authority needed for the requested phase.

The inspected generic dispatch initializes an unassigned read to zero and returns
decode error. `io_failed` only invokes an installed transaction-failure hook, and
the pinned x86 initializer has none. This supports the narrow inherited result
policy: bus status alone must not synthesize x86 #PF; the returned PTE may then
fail architectural walk checks. It does not specify arbitrary physical Haswell
bus behavior or resolve the inherited MMIO-PTE RMW question. Retaining host
memory-region dispatch preserves its routing/width/result seam conditionally,
with phase and exact-once ownership still required across waits.

No substantive source return. The finite next contracts are correctly identified:
route/result and conditional update semantics; A/D preparation with retained
origin and forced boundary; guest debug behavior distinct from optional tooling;
and cleanup/capacity. In particular, bounded page depth does not prove bounded
generic helper stack or host callback recursion; the chosen nonnesting service
boundary must replace those paths. This review accepts no numeric memory budget,
whole-helper closure, runtime behavior or architecture. No author-file changes
and no execution occurred.

### Return 8 — Supplement I, PTE interface and debug stop

**September 19, same independent reviewer: accept claim 17's conditional finite
walker/result/cleanup contract with the INV-03-04 baseline predicate below;
accept claim 18's scoped source/specification discrepancy. No complete guest
debug or combined architecture acceptance.** I compared the submitted interface
against the prior independently inspected walker, physical routes and bus-result
paths, and the concurrent exclusion/retention contract. I independently inspected
the pinned breakpoint/watchpoint handlers, CPU debug-interrupt path and frontend
SS-load/exit logic, and downloaded Intel 253668-085US, October 2024, confirming
§7.8.3's delayed data-breakpoint rule in the cited primary document.

Claim 17 distinguishes suboperation outcomes from outer access completion,
and device/discard semantics from actual changed RAM bytes. Its route reference
survives a published request without retaining topology/BQL locks; an unperformed
later phase resolves separately. Request IDs and phase consumption prevent a
service pause from repeating MMIO effects. The explicit cleanup categories cover
the declared walker gateway scope, not every enabled helper's native stack.
Omitting optional host tooling together with its runtime hooks does not excuse
guest DR/TF/RF semantics, and the submission correctly keeps them mandatory.

**Composition qualification:** INV-03-04's independent reviewer found that own-
generation folding must not overwrite a preexisting stale dependency. The corrected
contract compares saved expectations to the exclusive-entry baseline before the
owned A/D update, preserves the mismatch set, and folds only verified own deltas
on matching dependencies with unchanged address/permission/routing fields.
A mismatch prevents labeling a reusable shadow current; semantic revalidation
must not become a blind replay of completed device requests. Claim 17 delegates
exact mutation admission and validation to that corrected contract. This review
does not accept an unconditional assignment of new generations to all saved walk
reads. The author added this cross-record predicate explicitly; I rechecked
the corrected wording and have no outstanding return for this scope.

For claim 18, `cpu_check_watchpoint` requests CPU_INTERRUPT_DEBUG after the
reexecuted instruction. The CPU loop recognizes that request separately from
ordinary external-interrupt eligibility, and the target `breakpoint_handler`
raises the guest #DB without an inhibit-shadow check. `gen_movl_seg`/`gen_eob`
sets the MOV/POP-SS shadow and suppresses immediate TF, but does not provide the
missing delayed data-breakpoint ownership in that handler. Intel's cited rule
requires the MOV/POP-SS data breakpoint to wait through the next instruction;
the matched memory-selector example with a successful SS load is therefore a
concrete source-based counterexample to copying the handler as a complete event
contract, not merely an inference from its TODO. No execution was needed or
performed to establish that narrower claim.

The proposed pending-debug record is an appropriate next design obligation.
TF suppression, instruction-breakpoint suppression, data-breakpoint delay and
termination of the shadow by an intervening exception are distinct rules; a
single generic INHIBIT flag is not their complete specification. No claim here
settles ICEBP, software-interrupt or fault-priority composition, every REP debug
case, physical Haswell behavior, or a working implementation. The source finding
does not invalidate the reviewed finite PTE-result API; it prevents claiming the
whole guest event boundary is already closed.

### Return 9 — Supplement J, pending debug disposition

**September 20, same independent reviewer: accept claim 19's scoped primary-source
rules, conditional pending-record/event-kind interface and stated ICEBP stop.
No complete guest-debug contract acceptance.** I independently downloaded Intel
253669-079US (March 2023), read §18.3.1.2/4 and Table 18-2, and cross-checked the
previously inspected Volume 3A SS-load rules and Volume 2A INT entry. I also read
the pinned `helper_icebp` and `gen_INT1` producer, alongside the breakpoint
handler's delayed-SS/fusion TODO. No execution occurred.

The primary text explicitly distinguishes the two outcomes the interface needs:
an intervening fault loses the deferred MOV/POP-SS data breakpoint, while
INT n/INT3/INTO retains it through successful software-handler transition. The
resulting #DB can therefore arrive at a different CPL/stack. Immediate delivery
at the original SS-load TB exit, or dropping the record on every exception-vector
request, would not implement those rules. Actual guest event classification is
required; a native shadow fault or coordinator pause is not exception delivery.

Separating captured causes from delivery eligibility and architectural DR6 state
is coherent. One bounded record with accumulated cause information avoids a
per-iteration queue; it does not by itself settle simultaneous-cause DR6 rules.
An explicit successful-software-transfer hook avoids presenting the pending trap
before the handler-entry state exists. Delivery faults must follow the fault
path instead of treating a failed INT transfer as successful. These statements
are interface obligations, not proof of a working event-delivery implementation.

The ICEBP stop is appropriately narrow. The inspected instruction rules make
INT1 a trap that does not itself set DR6 condition bits; the QEMU helper marks
it separately from software INT, and the TODO calls for fusion with deferred
#DB. Those facts do not independently establish the combined B0–B3 report for
`mov ss,[selector]; icebp`. The author correctly avoids promoting the TODO into
architectural evidence or deriving discard solely from the generic exception
rule. The combined case remains unresolved rather than silently choosing an
outcome.

No substantive return for this scoped submission. Full REP/SS behavior,
simultaneous-event priority and the exact fusion case remain explicit obligations.
The next evidence must address that combined case directly; this review neither
authorizes an x86 execution study nor requires implementation before source
contract closure. No author-file changes were made.

### Return 10 — Supplement K, captive code-generator comparison

**September 20, same independent reviewer: accept claim 20's bounded source
facts and conditional interface comparison; no generator ranking or working
extraction acceptance.** I inspected the pinned FEX `LoadMemTSO`, scalar AFP
selection and SVE-specific vector branches, dispatcher entry/compile/return paths,
and named relocation construction. I independently fetched the pinned
`Source/Common/HostFeatures.cpp` and byte-compared it with the author's copy.
The TCG side was cross-checked against the already reviewed IR/global-state,
helper-exit and chained-PC source findings.

`SupportsTSOImm9` is assigned from LRCPC2, and the load path selects LDAPUR,
LDAPR or LDAR using the corresponding capabilities. This is not Apple's TSO
enable state. The vector load path's barrier is conditional on its runtime
vector-atomic-TSO setting; SVE256 branches assert that capability. AFP selection
also changes the emitted scalar/vector handling. These support inspecting a
captive capability profile and its non-SVE paths; they do not establish available
VM features, numerical FP equivalence or throughput on M4.

The dispatcher and relocation examples materially support the replacement
boundary: they use state-frame pointer fields, returning-stack state, host
compilation entrypoints and resolved thunk addresses. Relocation metadata alone
does not make those addresses meaningful inside the VM. The inspected dispatcher
also describes reentrant thunk callbacks, which cannot be imported unchanged
into the proposed nonrecursive resident-service regime. That runtime adaptation
does not prove the FEX emitter cannot be used under a different interface.

The comparison now applies the owner's standing captive-EL1 architecture to all
generator candidates. It correctly separates reusable system semantic sources
from a requirement to import a QEMU CPU loop/MMU, and separates useful FEX
lowerings from Linux-user signal/address-space assumptions. Sharing a QEMU
semantic helper with FEX requires a real state/flags/vector ABI bridge; choosing
TCG likewise does not preserve system correctness after replacing its memory,
dispatch and exception assumptions unless the new contracts establish it.

Box64 remains insufficiently inventoried for an adaptation-cost ranking or
elimination, as stated. Source inspection supports the listed work boundaries,
not relative porting cost, runtime cost or a completed implementation. The common
codegen input/output contract is a sensible next bounded artifact. No substantive
return, author-file changes, build or runtime experiment; no whole-runtime choice
or architecture acceptance follows from this review.

## Architecture-framing audit — September 20, machine reviewer

**Scope:** parent requested an independent audit of claims 01–20 against the
already fixed host-codegen/captive-EL1 architecture. Read the current owning
record and its source/protocol arguments, without new source research, runtime
work or author-file changes. This audit assesses applicability and misleading
selection inferences; it does not confer the still-pending source acceptance of
claim 20 or rerun prior findings reviews. No archived history was consulted.

**Count: 20 unique numbered claims audited; 17 retain, 3 qualify, 0 reopen, 0
withdraw at whole-claim level.** There is one selection rationale to redo:
upstream system-semantics coverage cannot select an end-to-end QEMU bundle or
establish lower generator adaptation cost. The three qualifications below are
instances of that same weighting error, not three independent failed runtime
findings. No upstream source fact was shown false by this framing audit.

| Claim | Disposition | Reason and smallest remedy |
| --- | --- | --- |
| 01 | Qualify | Source frontend/mode/segmentation machinery remains relevant. The heading “most directly relevant system semantics” must not stand in for a generator comparison. The body now correctly separates semantic provenance from selection. Rename the superlative to the actually supported system-frontend coverage, and assess extraction costs through the common interface. |
| 02 | Retain | Recovery requires spills, metadata and commit-state contracts; not a claim that upstream runtime recovery transfers automatically. |
| 03 | Retain | Explicitly separates host generation from resident helpers and requires relocated targets/marshalling. Directly consistent with the fixed design. |
| 04 | Retain | Source FP caveat applies if those semantic helpers are reused, independently of generator; no whole-runtime assumption. |
| 05 | Retain | FEX machinery and recovery caveats are scoped, with required captive integration and no binary64-only mischaracterization. |
| 06 | Qualify | Source Box64 configuration/recovery facts remain. The paragraph's “larger system-semantics integration surface” can describe missing upstream semantics, but not net captive port cost; its requirement that Box64 later demonstrate a “compelling advantage” and table's “secondary alternative” apply an asymmetric burden unsupported by the matched comparison. Replace with unranked candidate status pending equivalent interface inventory. |
| 07 | Qualify | ORC/statepoints do not supply x86 semantics: retain. LLVM backend selection and inventing a whole new x86 frontend are different choices, so combining them as “LLVM or a new minimal translator” and using full semantic burden to deprioritize both overstates the comparison. Separate reusable-frontend/LLVM lowering from new frontend and leave their cost unranked. |
| 08 | Retain | Counterexample is explicitly to weakening guest-memory IR, not to captive direct access. Preserving recovery-capable IR is a TCG-specific adaptation route, not a required choice for every generator. |
| 09 | Retain | Host nonlocal exits require a resident replacement; precisely an execution-boundary finding. Historical “QEMU-first direction” wording is superseded, not new correctness evidence. |
| 10 | Retain | Sampled scalar/vector preservation argument is conditional on changed lowering and excludes whole-ABI/runtime acceptance. |
| 11 | Retain | Specific source-derived SSE NaN mismatch remains relevant to optional helper reuse with any generator. |
| 12 | Retain | Chained-entry PC canonicalization applies to reuse of the identified chaining scheme; it is not imposed as FEX's exact mechanism. |
| 13 | Retain | Scoped semantic-helper NaN policy analysis, with separate FMA/minmax qualifications; no end-to-end solution inference. |
| 14 | Retain | Explicitly new permanent VM-local landing, separate origin/native records and cleanup obligations; upstream source supplies requirements, not the implementation. |
| 15 | Retain | Nonnesting resident repair is our runtime proposal and replaces upstream exclusivity/reentry; no unchanged-runtime assumption. |
| 16 | Retain | Walker inventory deliberately exposes host locks, pointers, dirty paths and bus interfaces needing replacement. Proposed QEMU dispatch reuse is a separate machine component choice, not a consequence of codegen selection. |
| 17 | Retain | Explicit finite service/result/ownership contract with conditional memory composition. Retained bus semantics are a separately chosen virtual-machine policy, not universal hardware x86 proof. |
| 18 | Retain | Debug producer/handler discrepancy is a semantic obligation regardless of generator. It specifically rejects copying host watchpoint/loop behavior as sufficient. |
| 19 | Retain | Bounded pending-event proposal and unresolved ICEBP case remain relevant to the common runtime; no generator advantage follows. |
| 20 | Retain framing | This is the needed common-boundary comparison: explicitly replaces both generators' runtime assumptions and declines a cost ranking. Source acceptance remains with its separate pending reviewer, not this audit. |

**Exact affected locations in the current author file:** claim 01 heading at
line 44; claim 06 inference at line 88 and Box64 “secondary” table row at line
104; claim 07 heading/inference at lines 90–94 and merged LLVM/new-generator row
at line 105. The September 20 correction at lines 96–109 and Supplement K at
1136 onward already replace much of the old selection framing. Those corrections
are not retroactive proof of lower adaptation cost. Historical local next-action
text such as “QEMU-first” near line 230 should be marked superseded to avoid
readers treating chronology as current selection.

**Redo scope:** retain the source inventory, explicit failure examples and
conditional resident protocols. Rework the comparative decision and affected
labels; do not repeat all 20 audits merely because the enclosing architecture
was restated. The substantive unfinished artifact is one shared generator
input/output contract with equal supported/replacement/unknown inventories for
TCG, FEX and Box64, and a separately named semantic-helper selection. LLVM-backed
lowering can be scoped independently rather than charged with a new frontend by
definition. That work can reuse claims 02–03, 08–10, 12, 14–17 and 20. It still
needs evidence of adaptation cost; no honest numeric fraction of total project
work or hours lost follows from these documents alone.

No architectural acceptance is reopened here because the owning record explicitly
has none. This audit withdraws the *selection implication* that system coverage
or upstream runtime maturity establishes the preferred captive generator; it
does not withdraw the underlying code facts or assert every proposed interface
is already complete.

## Return 11 — inventory/status cleanup consistency, September 20

Reviewer `/root/memory`; current records only, no new source research or runtime
work. **Accept the cleanup in scope.** The current summary and charter inventory
separate shared captive-runtime requirements from QEMU/TCG frontend, helper,
walker and recovery constructions. Changing the generator does not automatically
select or reject those separately reusable components, and source-specific
preservation arguments require revalidation when the responsible component changes.

The earlier QEMU preference from system coverage, relative Box64 integration
judgment and LLVM deprioritization are now expressly withdrawn as selection
inferences. Their source observations, claim identifiers and review chronology
remain. The current comparison is unranked and gives no generator a lower
adaptation-cost verdict. Claims 01–20 retain the scope of their existing findings
reviews; this editorial acceptance does not accept a generator or complete ABI.
Historical supplement banners and work-order labels make prior local next steps
subordinate to the current summary.

Requested and rechecked one architecture clarification: fixed host generation
and execution inside the captive VM with an EL1 shim do not fix generated-code
privilege. The lead now explicitly retains EL0 versus guarded EL1 as a candidate
choice, and the summary/Supplement K use the same boundary. The single proposed
45-minute common-interface pass is marked unstarted and not authorized by the
inventory cleanup. No outstanding cleanup return remains; no fresh research,
experiment, source claim, selection or architecture acceptance is added.

## Return 12 — common contract and symmetric adapter map, September 20

Reviewer `/root/memory`; bounded source/design review of Supplement L claims
21–22 and the revised common column of claim 20. **Accept in scope:** claim 21
as a proposed behavioral comparison contract, claim 22 as the declared source
inventory and conditional adapter obligations, and claim 20's clarification.
No substantive return remains. This does not accept a complete interface
implementation, extraction feasibility, generator selection or performance rank.

The separation is sound: profile/semantics, generated-code privilege, resident
ABI, memory policy and native VM capabilities are independent parameters. The
contract does not require common IR/state structures, a per-access service call,
full canonical spills on ordinary accesses, nonnesting repair or a particular
three-message encoding. It still requires declared effect ownership, recoverable
live state, source/context eligibility and reference lifetime at the boundaries
the selected protocol actually needs. A coarser invalidation protocol must prove
its own stale-execution limits; this text does not certify one. Nor does the
contract make every fault site independently reconstructible when the selected
repair instead preserves its native continuation.

I independently read the cited local pinned Box64 primary files, including
[`native_pass`](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/dynarec_native_pass.c)
(`addr`/`ip`, `PK` decode window, mode and block extensions),
[`x64emu` state](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/emu/x64emu_private.h),
[`ARM64 cache state`](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/arm64/dynarec_arm64_private.h),
[`geted`/`call_c`/dispatch](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/arm64/dynarec_arm64_helper.c),
[`block representation`](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/dynablock_private.h)
and [`pass-3 emission`](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/arm64/dynarec_arm64_pass3.h).
`call_c`'s saves/cache handling are conditional on its arguments, as the matrix's
"selected registers" wording permits; they do not prove arbitrary helper-fault
recovery. `TABLE64`/`TABLE64C` record relocations conditionally, while other literal
forms exist, so the explicit unknown for *all* embedded pointers is necessary.
The block's `sep`/`callrets` fields and `CALLRET_RET`/`CALLRET_LOOP` emission support
the concrete conclusion: INV-03-04's TCG two-link/tail-transfer premises cannot
be assigned to Box64 without replacing or bounding its selected transfer forms.
This is not proof of an unbounded stack or unsuitable generator.

I rechecked the pinned FEX JIT's IR/pass-manager input and its named-symbol,
thunk and guest-RIP relocation paths against the corresponding matrix cells.
The previously reviewed TCG/FEX memory, allocator, helper and recovery findings
remain their stated evidence base. Box64's signal `adjustregs`/`siglongjmp`
paths support the application-recovery versus captive-recovery distinction,
not complete recipes. E/R/U is used consistently as mechanism, crossed-contract
adaptation and audit unknown; no count of these labels is a cost estimate.

Provenance limitation: this review inspected the already downloaded primary
source copies at the declared pins. An attempted fresh network retrieval failed
(shell DNS failure; web cache misses), so no independent download or byte-match
is claimed. No new upstream version or source-absence inference is introduced.
No program was emitted or run. The proposed matched MOV-load/store comparison
is a next investigation, not evidence supplied by this review.

## Return 13 — matched MOV64 source fragments, September 20

Reviewer `/root/memory`; independent source review of Supplement M/claim 23.
**Accept the scoped source comparison and conditional adapter consequences.**
No substantive return. The matched bytes, CPL3/long-mode context, aligned WB
operand, incoming live flags/vector state and alias case are explicit. The
counts exclude address guards, allocation, spills and complete recovery paths;
complete adapted counts remain unknown for every generator. This is not an
emission observation, performance ranking or accepted fault recipe.

I read the cited pinned local primary files and checked the following branches:

- QEMU `tcg_gen_req_mo` intersects the requested ordering with `guest_mo` and the
  complement of `TCG_TARGET_DEFAULT_MO`; the cited x86/AArch64 defaults give the
  stated masks. `tcg_out_mb` maps isolated LD→LD to ISHLD and combined LD→ST/ST→ST
  to ISH. The MO_UQ/MO_64 direct-transfer cases each emit one native load/store.
  This verifies the stated emitter fragments, not their survival/placement in
  every optimized block or the removal of `prepare_host_addr` without adaptation.
  [Memory IR](https://github.com/qemu/qemu/blob/c1c18d1e640b64292859ce9f30f3c344edfb0294/tcg/tcg-op-ldst.c),
  [backend](https://github.com/qemu/qemu/blob/c1c18d1e640b64292859ce9f30f3c344edfb0294/tcg/aarch64/tcg-target.c.inc).
- FEX `MOVGPROp` uses its operand load/result routes; default force policy selects
  the context's GPR TSO setting. With both RCpc choices disabled, GPR64
  `LoadMemTSO` emits LDAR then NOP, and `StoreMemTSO` emits NOP then STLR.
  `SetHardwareTSOSupport` updates the emulation flags; its hardware-supported
  branch clears them. The claim retains ForceTSO/NonTSO qualifications and does
  not turn this software hook into evidence of captive Apple hardware behavior.
  [Selector](https://github.com/FEX-Emu/FEX/blob/48d71752e2a363d5535181f803b0b0e9666c1825/FEXCore/Source/Interface/Core/OpcodeDispatcher.h),
  [policy](https://github.com/FEX-Emu/FEX/blob/48d71752e2a363d5535181f803b0b0e9666c1825/FEXCore/Source/Interface/Context/Context.h),
  [emitter](https://github.com/FEX-Emu/FEX/blob/48d71752e2a363d5535181f803b0b0e9666c1825/FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp).
- Box64's base+0 `geted` branch returns the base register with zero offset;
  0x8B/0x89 use LDxw/STxw and the indicated strong-memory macros. With admitted
  `will_write`, `smwrite=0`, weakbarrier=0 and level at least STRONGMEM_QEMU,
  the native-pass WILLWRITE emits the first ISH. The store's SMWRITE emits a
  second ISH either through last-write SMEND or the QEMU-level nonlast branch.
  Thus the conditional three-instruction store fragment is supported; omitting
  the native-pass prelude would undercount it. Different incoming sequence state
  or policy is outside that count. SMREADLOCK(0) gives the stated ISHLD pre-load.
  [Opcodes](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/arm64/dynarec_arm64_00.c),
  [strong-memory macros](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/dynarec_helper.h),
  [prelude](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/dynarec_native_pass.c).

The recovery comparison preserves the important distinction: retaining a native
frame can repair an unperformed access, while delivering a guest fault requires
authoritative pre-MOV state, including earlier deferred flags/vector values.
For `mov rax,[rax]`, completed service must consume the result without reusing
the modified destination as a retry address. A single native transfer or PC map
does not establish that recipe for FEX/Box64, and TCG synchronization is retained
as conditional source machinery rather than free recovery. No MMIO/split/REP or
all-instruction proof follows. The proposed emission-method investigation remains
future work; no compiler, generated block, VM or timing run occurred in review.

## Return 14 — emission/recovery method preparation, September 20

Reviewer `/root/memory`; bounded independent review of Supplement N/claim 24.
**Accept the scoped source finding and proposed preparation method.** No
substantive return. This is not acceptance that the three harnesses are ready,
not pre-execution approval of an unspecified diagnostic patch, and not a source
or runtime result from the proposed 36-object exploration. The explicit next
readiness gate must resolve the remaining hooks and dependencies before a
concrete follow-on run can be assessed.

The byte table decodes consistently: `66 0f ef c1` is PXOR XMM0,XMM1,
`48 01 d1` is ADD RCX,RDX, `0f 94 c2` is SETZ DL and `f3 0f 7f 07` is
MOVDQU [RDI],XMM0. Each live-state fixture has five instructions; the three bare
and three live variants give six fixtures, hence 36 primary objects for two
configurations and three generators. The later store consumes the PXOR result;
SETZ consumes ZF. Requiring all ADD flags at the intervening guest-fault point
is correctly stronger than observing which flags a later instruction consumes.
These symbolic obligations expose, rather than assume away, dead-state and
deferred-flag reconstruction. NASM output must still be byte-compared at execution
time; I did not assemble or execute these fixtures.

I independently inspected the cited local pinned FEX
[`InstructionCountParser.py`](https://github.com/FEX-Emu/FEX/blob/48d71752e2a363d5535181f803b0b0e9666c1825/Scripts/InstructionCountParser.py)
and [`CodeSizeValidation/Main.cpp`](https://github.com/FEX-Emu/FEX/blob/48d71752e2a363d5535181f803b0b0e9666c1825/Source/Tools/CodeSizeValidation/Main.cpp).
`Features`, `Instructions` and `x86Insts`, NASM invocation and recorded instruction
count match the method. `CompileAndGetStats` copies bytes and invokes
`CompileRIPCount`; the inspected tool path does not execute the fixture. NOP
header subtraction and UDF-tail trimming make its normalized count unsuitable
as complete raw output. TSO defaults/feature overrides and forced atomics and
preserve-all ABI are real branches. The method correctly makes raw bytes,
effective settings and allocation/recovery diagnostics separate prerequisites.
The existing CLI does not promise all those diagnostics.

QEMU's pinned
[`tb_gen_code`](https://github.com/qemu/qemu/blob/c1c18d1e640b64292859ce9f30f3c344edfb0294/accel/tcg/translate-all.c)
reads the cflags instruction bound, asserts its applicable memory-lock contract,
uses initialized TCG state and can exit through CPU-loop machinery on allocation
failure. The record does not claim `-S` compiles the fixtures or that an arbitrary
thread may call this function. The exact initialized worker hook and safe exit
context remain readiness work, not established extraction.

Box64's pinned
[`CreateDBnoAlt`](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/dynablock.c)
wraps `FillBlock64(..., MAX_INSTS, 0, 1)` in signal/mutex handling;
[`FillBlock64`](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/dynarec/dynarec_native.c)
uses context/protection metadata. Most importantly,
[`initialize`](https://github.com/ptitSeb/box64/blob/454d5387571bb5223fe8b43a4624debde8b74647/src/core.c)
loads dependencies, relocates and runs deferred ELF initialization before the
main execution call. Thus stopping only before main `DynaRun` is insufficient;
the earlier diagnostic branch, minimal-input restrictions and execution-entry
failure guard are justified requirements, not a ready upstream CLI.

The inspected FEX build predicate rejects Darwin, and the tool target exists;
this supports a supported-worker prerequisite, not a measured porting disadvantage.
The author's transient PATH/socket observations remain reported readiness checks,
not independently replicated platform impossibility. Resource limits and artifact
requirements distinguish observed output, derived recovery and proposed adapter
changes. No build, fixture generation, emulator execution, fault injection or
timing measurement occurred in this review. Missing hooks or recovery metadata
must remain explicit failures/unknowns at the next gate, not zero-cost recipes.
## Return 15 — readiness and compile-only seams, September 20, root

**Accept claim 25 in its readiness/source scope.** Root independently inspected
the pinned QEMU `cpu_exec_step_atomic`, queued-work and MTTCG worker paths,
Box64 initialization ordering, and QEMU/FEX build dependencies. Local checks
reconfirmed the installed Docker/Orb tools, absent Docker sockets and missing
CMake/Ninja/Meson/NASM. Root did not repeat `orbctl --help`, which the author
found performs initialization writes. Existing storage is not a running worker;
the conclusion correctly leaves other possible workers open and transfer totals
unknown. No generator is ranked by this environment limitation.

One substantive method clarification was returned and corrected: the atomic-step
skeleton clears `CF_PARALLEL`, but serializing host compilation does not change
the intended guest SMP semantics. The proposal now explicitly retains the
fixture-selected parallel flag and replaces the whole instruction-count mask,
rather than OR-ing a count into existing bits. Normal queued work must release
BQL before the exclusive skeleton and restore it afterward; the actual patch
still needs exceptional-path review. Neither a paused CPU nor this source seam
proves an unwritten compile-only harness cannot execute guest instructions.

The existing-service exploration is a bounded possible next step, with no
implicit image download or workload changes. Complete source-derived allocation
and recovery reasoning remains a valid alternative. This verdict accepts the
readiness finding and corrected proposed seam, not an emission result, cost
ranking, provisioned worker or complete method implementation. No substantive
return remains.

## Return 16 — exploratory service-access disposition, September 20, root

**Accept claim 26 as an accurate exploratory work-status report; no promotion
to architectural decision evidence.** Root inspected the retained command records,
context, capture script and manifest, and independently verified all six hashes
and lengths. The unsandboxed `open` command failed with the recorded launchd
domain error; the subsequent Docker query reports an absent endpoint. The
bundle inspection prevents incorrectly interpreting the first sandboxed error
as proof that the executable is missing. No successful service start, worker
enumeration, provisioning, build or emission result appears in the record.

No independent launch retry is needed to make the safe next-work decision:
stop this access route and use the available source-recovery analysis. This
does not establish platform-wide unavailability, reject a generator or block
Track 03. Exploration status and its stopped budget remain explicit. There is
no substantive return; a future accessible worker may enable the recorded
emission study without making another discovery loop the present next action.

## Return — claim 19 oracle qualification (root addendum)

**Reviewer:** machine investigator, independent of root author. **Verdict:**
accept the scoped source/method qualification; no substantive return. This does
not resolve claim 19's DR6 fusion semantics. No runtime or author-file edits.

Independently fetched upstream [kvm-unit-tests debug.c](https://raw.githubusercontent.com/kvm-unit-tests/kvm-unit-tests/master/x86/debug.c)
on September 20. The MOV-SS/watchpoint/ICEBP case installs the small assembly
`handle_db_save_rip`, uses carry/rotate state and expects `n == 3`; the adjacent
INT1 software-interrupt case expects `n == 7`. The active handler does not read
DR6. The generic C handler does read it, but that handler is replaced for these
cases. This test therefore contributes a delivery expectation, not the missing
cause-bit oracle or a hardware result. The dated master reference is accurately
qualified rather than presented as an immutable revision.

Also independently opened Intel [793902 revision 016US](https://edc.intel.com/output/DownloadPdfDocument?id=10616),
verified its September 10, 2025 date, EMR95 on PDF page 46 and the affected-stepping
summary. It describes missing debug delivery or incorrect DR6 causes after MOV/POP
SS without a following explicit SP write. Its scope is the identified processor
family/steppings, not a replacement architecture rule.

The inferred method consequence is sound: a future oracle needs independent
count/RIP/DR6 observations and applicable errata accounting. Adding an SP write
changes the exact immediate-successor sequence, so that control cannot settle
the original ICEBP case. Conversely, an affected processor losing a cause does
not justify adopting discard semantics. No fusion selection or new campaign is
accepted by this review.

## Return 17 — matched live-state recovery, September 20, memory

**Accept claim 27's scoped source findings and conditional fixture recipes.**
Independent review read Supplement Q against the retained pinned QEMU emitter,
TCG liveness, FEX Core/dispatcher/allocator, and Box64 MOV, architecture metadata
and native-pass sources. No build, emission, execution or measured count was
performed. This is not complete fault-recovery acceptance or a generator ranking.

The inverse-ADD argument is valid for the exact live fixture: post-ADD RCX and
unchanged RDX determine the original first operand modulo 2^64 and all six ADD
arithmetic flags. Unsigned sum less than the second operand detects carry;
operand/result sign relations determine overflow, their XOR determines AF,
and the result determines ZF/SF/PF. SETZ has not executed at the marked fault.
Other flag bits still require the incoming state. Bare fixtures cannot use
this formula. This establishes a possible exceptional-path recipe, not evidence
that its physical inputs survive any arbitrary guard or allocator rewrite.

For TCG, `prepare_update2_cc` records the second operand and result;
`liveness_pass_1` marks global coherence at SIDE_EFFECTS, and the later machinery
implements that obligation. The clarification that `sync_globals` asserts
rather than emits those stores is correct. The gvec PXOR state-backed path and
PC/CC restoration preserve the stated canonical baseline, subject to retaining
its lowering and entry contracts. No counted spill total follows.

For FEX, Core's per-instruction `FlushRegisterCache(true)` and the dispatcher
implementation substantiate partial fixed-register publication and deferred-flag
processing. SRA-only does not mean a full canonical memory image. The allocator's
post-RA merge can remove an immediately consumed move and rewrite its consumer;
its temporary SSA bookkeeping is cleared. The submission correctly stops short
of asserting the frontend publication is an immutable native checkpoint. Its
next action targets the precise location/no-clobber interval instead of treating
this source fact as either free system recovery or impossibility.

For Box64, the base-only MOV forms use mapped guest GPRs; old RAX must survive
the faulting non-writeback alias load, and RCX/RDX remain the arithmetic recipe's
inputs. `adjust_arch` selects previous-completed-instruction metadata and restores
recorded vector registers from the native frame. This Linux consumer needs a
resident-frame adaptation, and the MOV prefix must preserve its mapping. The
instruction-count cutoff really calls `interblock_flags_needed`; hence five
instructions alone do not fix deferred flag-emission cost. The explicit tail
window/configuration requirement is warranted.

All three require the completed PXOR result, unaffected vector state and any
exposed upper lanes, precise original address, pre-access commit phase and
unmodified architectural state. A native frame for internal retry is insufficient
without their architectural mapping for guest fault delivery. The text preserves
that distinction and forbids replay of completed serviced stores. No substantive
return remains; exact FEX allocation, Box64 prefix/tail inputs and adapter
conformance remain named premises, not accepted by omission.

**Feature-scope clarification:** the conditional FEX normal-AArch64 fixed-register
recipe does not establish uninspected SVE/non-SVE or wider-vector allocation
branches. Unaffected exposed upper lanes must remain recoverable through any
cache eviction under the stated entry/allocation contract; naming v16 for lower
XMM0 is insufficient by itself. This remains part of the explicit unresolved
physical interval, not a demand for a new full-AVX audit in this fixture pass.

## Return 18 — FEX native-site recovery contract, September 20, memory

**Accept claim 28's scoped source constraints and conditional adapter choice.**
Independently inspected Supplement R and the pinned IR declarations, pass order,
flag elimination, allocator kill/fill/spill/merge paths and prior scalar-memory
lowering. No implementation, emission, native execution or cost measurement.

The naive before-access annotation has the stated lifetime defect: the backwards
RA scan marks a final argument use killed, and the forward scan frees killed
sources before destination assignment. A marker that merely names a register
before the fault does not prevent reuse. `GuestOpcode` contains origin, not the
architectural roots. The ordinary LoadMem versus side-effecting pair/store IR
contrast is accurate, and the submission correctly treats fault-effect retention
as adapter work rather than claiming these live-result fixtures were deleted.

The selected remedy closes that interface conditionally: retain explicit roots
through the fault-bearing access, capture their **actual pre-fault** locations,
and bind the exported immutable map to the precise native access/guard site.
Post-access liveness alone is insufficient without that location capture and
backend clobber rule. SpillReg creates initialized slots and frees registers;
SSAToReg alone is not proof of current occupancy. Capturing only at a later
marker could instead name a register filled after the access. The submitted
map rule excludes both mistakes and exports before scratch bookkeeping clears.

The final metadata-use clarification is necessary and correct: unchanged normal
argument handling fills spilled inputs before consuming them. RecoveryUse must
participate in liveness/next-use accounting while its adapted forward handling
consumes existing register/slot ownership without introducing a reload solely
for the nonexecuting annotation. This is an explicit allocator adaptation, not
an upstream zero-cost feature. Merge boundaries and remapping also need the
specified changes; absent those changes, current SRA publication is insufficient.

Old RAX/original alias address is separate from the successful load destination;
ADD sum/source and the completed low-XMM PXOR result remain named roots. The
entry binding table must preserve all unaffected exposed state, including upper
lanes, through fixed-register evictions and helper/link transitions. Explicit
native SVE/SVE256-off configuration does not reduce the guest ISA; missing upper
state must be published at entry rather than inferred from v16. Bare fixtures
retain incoming flags instead of using the ADD recipe. These premises prevent
a lower-XMM-only recipe from masquerading as full guest-state recovery.

The distinction between pre-access guest fault, retained native repair, completed
service and post-success asynchronous pause is preserved. A completed store is
not replayed and a post-success pause cannot use the pre-access fault map as
current architectural state. Non-writeback aligned scalar exception behavior,
reserved resident storage, truthful backend scratch declarations and complete
entry bindings remain premises. Wider partial operations, all-instruction/helper
coverage and actual adapter conformance are not accepted by this fixture proof.

There is no substantive return. This is a finite compiler/runtime contract that
can be chosen before full implementation. It establishes neither zero added
spills nor an instruction-count/performance advantage over TCG or Box64; extra
liveness pressure, site metadata and any required copies remain explicit costs.

**Return 18 cost-qualification recheck:** accept the added paragraph only. The
post-access metadata use is a sufficient conservative lifetime, not a proof of
minimum recovery cost. Under the already stated aligned non-writeback scalar
fault premise, a successful load may overwrite old RAX while a fault preserves
it; requiring that old value after success can therefore prevent otherwise safe
destination coalescing and cause avoidable copies/spills. A fault-edge-only
lifetime could remove that constraint, but needs the stated native fault and
pre-access clobber proof plus explicit allocator support. None is claimed as
already implemented. The accepted safe construction is unchanged; comparisons
must not call its potentially conservative overhead inherently mandatory for
FEX or precise recovery. No new source or runtime evidence was added.

## Return 19 — matched adapter decision, claim 29

**Independent reviewer: machine; September 20. Accept the bounded preparation
choice and scoped comparison.** Reviewed Supplement S against the charter and
accepted claims 27–28, including the conservative-lifetime qualification. This
pass adds no upstream source or execution finding.

The comparison keeps the fixed host-generator/captive-shim architecture intact:
canonical publication, root maps and instruction metadata are candidate adapters,
not three whole upstream runtimes. No ordinary RAM host exit is imposed. System
semantics, FP compatibility, privileged helpers and guest ISA remain separate
selection obligations. The table describes required changes, not measured port
cost or generated instruction counts.

The FEX pilot can change the next allocation of research effort: actual producer,
allocator and emitter changes can expose a nonlocal dependency, missing old value
or avoidable preservation cost in the proposed recovery seam. Its result cannot
establish a generator winner. Successful scalar extraction alone leaves the
broader semantic and helper burden unresolved; failure of the conservative
post-access lifetime does not reject a fault-edge construction. The explicit
opportunity-cost cap and unchanged TCG/Box64 alternatives make that distinction
reviewable rather than an implicit FEX selection.

The proposed 45-minute deliverable is a disposable, potentially partial patch
plus fixtures and a symbolic oracle. It neither claims that the partial tree
builds nor authorizes the later 24-object study. Requiring exact pinned missing
interfaces and regeneration of IR-generated headers prevents a speculative patch
from being represented as a conforming adapter. The oracle must bind required
values and widths without inventing physical allocation. The eventual paired
unmodified/annotated FEX diagnostic isolates only this adapter's cost; TCG and
Box64 require matched emitted artifacts before numerical comparison.

No substantive return. Accept preparation of the named bounded artifact, with
its stated dependency stop, not build readiness, recovery implementation,
performance or production generator selection.

## Return 20 — actual observation patch, September 20, memory

**Accept claim 30 as an accurate partial preparation report and static artifact
scope. This is not acceptance of an implemented checkpoint, successful build,
emission method readiness or recovery conformance.** Independently read all six
artifact files, the exact patch hunks and matching pinned allocator/tool/parser
sources. Ran only `check.py` against the retained FEX tree: original source
hashes/lengths, fixture shapes, requirements shape and `git apply --check` pass.
No patch was applied to the shared source, no NASM/compiler/code generator ran,
and no generated code or VM executed.

The two-file patch adds conditional stderr diagnostics. The first allocator
observation precedes the existing GuestOpcode/InlineConstant skip; the second
follows destination reservation and precedes trivial removal/post-RA merge.
Therefore it observes the named bookkeeping points, not a native access PC or
an initialized result in a newly reserved destination. Assigned spill slots are
not claimed to be valid at that point. The code/README correctly distinguish
register-class indexes from AArch64 register numbers. Raw INFO is copied before
ParseMessage normalization, but is still text rather than a code-byte export.
This is useful inspection instrumentation only; unbuilt logging is not proof
that a concrete build/output is unaffected.

The JSON contains the six declared MOV fixtures with the reviewed live prefix
and marked MOV indexes. The ADD encoding, alias base and PXOR/SETZ/MOVDQU sequence
match their symbolic recipes; bytes remain manually specified, not an assembler
observation. Upstream parser consumes Features/Instructions/x86Insts, ignores the
extra review fields and defaults omitted ExpectedInstructionCount to zero. That
default must not be treated as a meaningful count oracle. The TSO feature enables
the tool's software configuration, while RCpc/native SVE are disabled; forced
upstream options and the unimplemented hardware-TSO variant remain explicit.

The requirements file preserves old RAX/original address, incoming bare flags,
all unaffected/exposed upper state, valid pre-fault locations and no completed
store replay. It is not an executable map checker. Nothing in this artifact
implements root insertion, metadata-use liveness, actual initialized location
export, backend-PC binding or the annotated comparison variant. The source seam
is real: GuestOpcode is skipped before static-register eviction/merging and
cannot by itself establish claim 28's proposed recovery invariant.

No substantive reporting/static-scope return remains. The authorized preparation
ended with unfinished engineering, not a conforming adapter or external blocker.
The recorded next joint patch is still required before any annotated extraction
can be reviewed as ready. This finding grants no build/run budget and cannot be
used as evidence that the proposed checkpoint works or has a measured cost.

## Return 21 — joint FEX diagnostic patch, claim 31, September 20, memory

**Accept the corrected artifact as a scoped static preparation and accurately
reported joint source path. No successful C++ build, observed allocation/native
map, recovery conformance or performance result is accepted.** Independently
reviewed the six-target standalone patch against the pinned frontend, schema,
allocator, backend register bindings, memory emitter and CodeSizeValidation
sources. Re-ran only the static source/fixture/applicability checker. Inspected
the author's retained schema-generation outputs; did not run the FEX compiler,
allocator, native emitter, generated code or a VM.

The gated fixture is the exact `live_load` sequence, with the existing one-byte
NOP baseline control separately admitted. Post-prefix RCX/RDX, old RAX, RBX and
low XMM0 are captured before the MOV; the sixth SSA operand identifies its access
result rather than another pre-fault architectural root. The generated schema
contains the six-SSA overload and non-SSA descriptor fields. Core's preceding
register-cache flush and the selected scalar/native-non-SVE frontend path support
these source identities; they do not establish complete unaffected or upper
vector-state bindings.

The allocator's special metadata-use path snapshots all five locations before
consuming kill bits. Register ownership is checked against the SSA value; an
assigned spill slot is the other accepted representation. In this immediate
load/marker construction, spill insertion precedes the load and surviving roots
cannot be its newly reserved destination. The source-level next-use/kill handling
does not enter the ordinary fill loop. Clearing SSA arguments occurs after
capture. The previous-node/access checks and disabled load/checkpoint merge
conservatively reject unsupported motion or replacement. This is a review of the
proposed source path, not an observed optimizer/allocation invariant.

Backend register-class conversion uses the pinned GetReg/GetVReg helpers, and
spill offsets use the backend's slot stride. The SITE diagnostic precedes the
selected LDAR, rather than its following NOP. BEGIN/END and entry/access identity
can delimit future complete emission attempts, but the required trace validator,
immutable resident map and consumer do not yet exist. The new patch is separate
from the overlapping observation patch; it does not inherit that patch's raw
INFO export. The post-access lifetime remains conservative and may prevent
otherwise safe fault-only coalescing.

Two concrete review returns were corrected:

- **R21-1, retention:** the first record's manifest included a stale hash/length
  for itself. The reviewer observed the listed 2,609-byte hash
  `25e7cc9b04a3b81edec6feaa52adf09f19161963fb84b21494d74c9122f9a908`
  versus the actual 2,743-byte hash
  `c588cc442c6ec608b5376f0ef2aa047ac4d4ada6da8fbac293fc8c02d282a961`.
  The author had overwritten that manifest before receiving the return, so the
  failed original bytes were not retained. The correction reports that limit
  honestly; the old record is not retrospectively accepted. Independently
  verified all 16 listed file hashes/lengths in the distinct fresh record
  `/private/tmp/natemu-track03-checkpoint-static-20260920T094625Z`, whose manifest
  excludes itself. Its `input.patch` is byte-identical to the reviewed current
  patch: 13,507 bytes, SHA-256
  `e01b7655ef4c0507596fa24c9a1385f89619f1fd606f260eea71e977890319e1`.
  The retained generator result is exit zero with empty stdout/stderr.
- **R21-2, conditional assertions:** pinned `LogManager.h` compiles
  `LOGMAN_THROW_A_FMT` rejection out when `ASSERTIONS_ENABLED` is false. Thus the
  draft's fixture, association and location checks were not unconditional gates.
  Rechecked replacement of the newly added checks with explicit stderr/abort
  `NatemuRequire` calls and their required includes. Existing upstream assertions
  and generated type assertions remain baseline behavior, not evidence of an
  unconditional diagnostic gate.

No substantive return remains for this bounded static scope. The selected
root-fixture subset matches the gate; its omitted expected count still defaults
to zero and supplies no oracle. Full-state bindings, inverse-ADD recipe export,
code-byte export, map validation, fault reconstruction, other fixtures and
hardware-TSO configuration remain unfinished. Native SVE exclusion does not
remove guest upper-state obligations. Schema generation checks an API shape,
not C++ compilation or runtime correctness; this verdict grants no build or
execution budget and selects no production generator.

## Return 22 — non-SVE split AVX and upper-state binding, claim 32

**Independent machine review, September 20: accept the scoped source finding and
conditional binding/remedy.** Inspected the pinned decoder selection, AVX128
source/store/arithmetic helpers and dispatch, split CPUState, pair-memory helper,
context cache indexes/StoreContext and per-instruction flush. No emission or
runtime result is added.

The source chain establishes an existing non-SVE split path for the sampled
operations. VPADDD selects two i128 arithmetic values and vector memory uses a
pair or two i128 accesses; the isolated i256/SVE backend branch cannot support a
claim that all non-SVE AVX lowering must be invented. Guest feature exposure and
system state-control semantics remain separate decisions. This removes a specific
portability concern, not the broader AVX2/FP adaptation obligations.

For the legacy PXOR/ADD/MOV fixture, unchanged high halves can use initialized,
resident and preserved entry backing. That is a conditional binding rather than
a zero-state assumption, and it must remain truthful through entry and chaining.
The explicit warning that ordinary low fixed-register state need not already be
in CPUState prevents this high-half option from becoming a blanket memory-state
recovery claim. Full entry bindings are not exported by the existing diagnostic.

The dirty-high counterexample is correctly a failure of an inference, not an
observed upstream fault bug: StoreContext marks indexes 48–63 cached/written,
while the inspected partial flush omits them. This gives no proof of current
backing at the interior MOV boundary. Later transformations or stores could
change the emitted situation and require inspection; they cannot be assumed to
supply an absent recovery contract. The proposed unchanged/constant/dirty-root
classification closes that logical gap conditionally. Selective publication is
also valid but carries actual stores; mandatory global spilling is not inferred.

No substantive return. Accept continuing the bounded binding-contract work for
these fixtures; do not accept complete entry export, all-vector partial-effect
recovery, system XSAVE, startup32, generated costs or a production generator
selection. The dirty-high root must eventually survive the full optimizer and
allocator pipeline, with its real initialized slot/register location, before
implementation conformance can be asserted.

## Return 23 — complete conditional fixture field-map, claim 33

**Independent machine review, September 20: accept the conditional contract and
scoped RF correction.** Read Supplement W, the retained Intel Volume 3B 079
section 18.3.1.1, and pinned FEX flag reconstruction/carry metadata. No emitted
map, runtime, new source or completed profile inventory is accepted.

The contract now accounts for more than changed scalar roots: every profile
field must have a concrete disposition, while authoritative semantic banks and
live subsystem owners are retained rather than overwritten from an entry image.
Aliased MMX/x87, segment/MSR and CR8/TPR fields cannot acquire contradictory
owners. The versioned field catalog remains an explicit unsatisfied input where
the product profile is not complete; family names alone cannot publish a map.
This is a valid conditional interface, not a claim that the current patch has
implemented complete architectural state.

ENTRY is correctly provenance rather than a storage location. Working registers
are conservatively named roots; unchanged upper backing requires actual entry
currency and lifetime, and dirty high state requires a root or real publication.
Guards, allocator moves and chained entry can invalidate an untracked register
or stale bank. Exact native-site/phase identity, initialized spill storage and
pins must survive until consumption. The 32 roles plus relevant high/flag inputs
are not a minimum cost claim or a requirement to spill all fields on ordinary
memory accesses.

The inverse-ADD formulas use the completed sum and unchanged operand with explicit
64-bit wrapping. The incoming FEX flag recipe names NZCV, PF/AF inputs and carry
inversion, instead of assuming the end-of-block carry convention at an arbitrary
site. Those inputs require pending-flag calculation and survival across inserted
guards; their existing upstream reconstruction routine alone is not a captive
recovery implementation.

Intel's inspected text explicitly clears RF at instruction start after the named
early checks and separately sets the saved RF image for the relevant fault-class
exception. RF=0 at this admitted MOV data phase and saved #PF RF=1 therefore
belong to different transitions. The correction does not cover early instruction
breakpoints or resolve the separate debug-fusion issue. Completed prefix and
current event progress must not be replayed on native repair.

The four continuation outcomes preserve the key phase distinction: old-RAX
precommit recovery is valid for a guest fault, not after service has supplied the
load result; internal repair retains native continuation, and canonical landing
publishes a coherent field/phase state before guest execution resumes. Live time,
interrupt state and already committed memory/PTE effects are not rolled back.

No substantive return. Accept the two-fixture field/owner contract conditionally
on its enumerated catalog, entry and allocated-location proofs. This does not
accept a complete descriptor exporter, all ISA/FP recovery, measured register
pressure, generator selection or combined architecture completion.

## Return 24 — partial descriptor checker and trace consumer, claim 34

**Independent machine static review, September 20; correction pending.** Read the
new checker and README, claim 34 and the unchanged patch's actual BEGIN/SITE/ROOT/
END formats. Verified all 56 original retained file hashes and lengths. The parser
matches hexadecimal entry versus decimal offsets/IDs and native register naming;
it deliberately cannot publish a complete map from five roles. Model witnesses,
opaque owner banks and named recipes are not native allocation/execution proof.
The partial-deliverable characterization and concrete missing 27-role/authority
producer delta are accurate; this is not full exporter completion.

One substantive consistency issue was reproduced independently: deleting an
authority backing-version entry while setting the witness version to null passes
because null equals the missing dictionary default. The same hole affects semantic
bank versions. This contradicts the declared current-version witness requirement.
Require an explicitly present, valid version on both sides and add rejection
cases. The author independently identified this during freeze and is correcting
it; retain the original snapshot rather than overwrite it. The top docstring's
claim that the program never consumes FEX traces also needs editorial correction.
No native/runtime conclusion depends on these synthetic cases.

Other inspected boundaries preserve precommit/RF, dirty-high provenance, root
lifetime/occupancy, exact site and live-owner nonrollback distinctions. The model
catalog remains deliberately incomplete for a product profile. No changes to
native patches or broader compiler work are requested by this return. Final
scoped acceptance awaits the corrected fresh snapshot and version-rejection
recheck; no runtime or build is authorized by this review.

**Return 24 correction recheck — accepted scoped preparation.** Independently
verified all 59 hashes/lengths in the fresh `104356Z` snapshot and current script
identity. Exact authority catalogs and nonnegative integer authority versions now
reject both reproduced missing-authority/null-witness cases. The 34 retained
synthetic cases include both regressions and invalid authority typing; the module
docstring now accurately names the partial FEX text consumer. Original and
intermediate snapshots remain preserved. No substantive return remains.

Accept claim 34's partial descriptor-consumer/checker preparation and explicit
missing-producer finding, not a real complete exporter, observed allocation,
arithmetic recipe execution, full profile catalog or native conformance. The
current five-root trace remains unpublishable by design; synthetic witnesses
cannot fill the absent actual producer/owner interface.

## Return 25 — Claim 35 actual working32 producer preparation

Reviewer: memory, independent of root author, September 20. **Accept scoped
static preparation and its explicit missing-authority boundary.** No outstanding
substantive return; no C++ build, actual allocated trace, native execution,
complete recovery map, minimum spill cost or generator selection is accepted.

Reviewed the current six-target `fex-root-preparation.patch`, frontend fixture,
schema, allocator handler, native-site/role output and `recovery-map.py`, against
the pinned source and generated headers. Independently verified all **89** file
lengths/hashes in `/private/tmp/natemu-working32-20260920T110817Z/manifest.json`
and current artifact identities. Re-ran all **41** synthetic cases in a separate
temporary directory and regenerated both headers with the retained upstream
Python generator: outputs were byte-identical. These are static/Python checks,
not C++ compilation or emitted native-code observations.

The frontend names all 16 x86 GPR enums rather than assuming their numeric order,
and separately captures XMM0–15. Roots are taken after the completed PXOR/ADD
prefix and before the selected MOV load result replaces RAX. The unchanged
exact-byte/offset gate rejects the H fixture and other MOV inputs. At the pin,
`OpcodeDispatcher.h`'s `GetGuestVectorLength` is 128 bits when SVE256 is disabled,
so `LoadXMMRegister` under this gate names low 128-bit working values; the RA
handler additionally rejects any unexpected root width. It does not establish
upper-half currency or all exposed architectural state.

The generated packed metadata structure and emitter overload contain **33 SSA
arguments**: 32 working roots plus access-result identity. LocationKinds is
`uint64_t`, with two bits for each root; the largest shift is 62, so root 31's
kind fits rather than overflowing the old narrow representation. The generated
argument/RA tables reflect 33 operands. Non-SSA locations are copied through a
local array into scalar packed fields, preserving the earlier alignment fix.

The allocator still requires the immediately preceding tracked node and
`LastNode` to be the same LoadMemTSO. It snapshots all roots before consuming
kills, checks actual occupied registers or previously allocated spill slots,
and processes the complete next-use stream without invoking metadata-only fills.
The pinned spill allocator emits the spill operation before assigning its
nonzero slot record; descriptor construction does not fabricate an uninitialized
slot. Capture-before-kill matters for duplicate roots. The selected load bypasses
post-RA merging and the marker breaks merging across it. These establish the
intended source construction, not conformance of an observed optimized allocation
or a complete spill-lifetime/owner proof.

The backend maps register descriptors through the actual GetReg/GetVReg helpers,
prints the 32 role names/classes at the same access identity and retains SITE
immediately before the selected LDAR. BEGIN explicitly selects `working32-v2`
and AUTHORITY immediately reports `status=missing`. The parser requires that
boundary and the corresponding complete role set, rejects wrong classes and
invalid/missing authority, and never changes `publishable` to true. Historical
five-role parsing remains supported. Independently checked that separate
completed v1 and v2 attempts can coexist and remain unpublishable; the author's
claim wording now correctly limits mixed-format rejection to **within an
attempt**. No parser or producer alteration was needed for that clarification.

Also verified all **36** retained file hashes in the earlier
`/private/tmp/natemu-working32-20260920T110759Z` failure snapshot. Its failure
record identifies the post-generation checker looking for an unpacked struct
spelling, while generation produced a packed declaration. The old record is
preserved; the corrected snapshot is not represented as the original success.

The 27 additional working roles now exist in actual producer source. They are
not synthetic entry authority, actual native locations or a full-state exporter.
The explicit next gap—resident entry/bank currency, semantic/live-owner provenance
and lifetime bound to the site—remains correctly open. Broader H/ISA/FP coverage
and alternative-generator controls also remain necessary before selection.

## Return 26 — resident upper publication and entry ownership, claim 36

**Independent machine source/interface review, September 20: accept the scoped
conditional L-entry choice.** Inspected the pinned FEX `OpcodeDispatcher.h`,
`IR.json` and `MemoryOps.cpp` directly, including cache indices 48–63, ordinary
ExitFunction wrappers, full/SRA-only masks, zero specialization, StoreContext
side effects and native store lowering. No new patch, build or execution.

The source distinction is correct. Full flush processes dirty upper slots and
maps them to `avx_high`; SRA-only excludes them. Nonzero 128-bit values use
STATE-relative Q stores/pairs, while zero specialization can use the corresponding
pair of 64-bit zero stores. Ordinary ExitFunction wrappers request full flush.
That is a concrete publication construction, not proof that every entry/exit or
optimized path has actually completed it. In contrast, GPR and low FPR stores
move values to fixed native registers through StoreRegister. They do not establish
a canonical memory copy of the entire frame. Carry normalization at a full
boundary also does not supply the mid-block carry convention.
[Pinned dispatcher](https://github.com/FEX-Emu/FEX/blob/48d71752e2a363d5535181f803b0b0e9666c1825/FEXCore/Source/Interface/Core/OpcodeDispatcher.h#L1177),
[pinned lowering](https://github.com/FEX-Emu/FEX/blob/48d71752e2a363d5535181f803b0b0e9666c1825/FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp#L77).

The selected entry record therefore has a legitimate narrow role: resident
initialization or completed predecessor publication establishes upper backing,
then L's non-modifying prefix preserves it. Working roots still represent values
at the actual precommit MOV after its completed prefix. H's intervening VPADDD
invalidates the unchanged-backing argument and remains outside the current
producer. Neither static expected offsets nor a generated version number creates
that dynamic fact. The current AUTHORITY=missing refusal is appropriate.

The entry contract does not require a host round trip or a spill of all working
registers at every block. Qualified native chaining may bind the same backing
and working ABI after required publication; unqualified alternate, helper or
exception entries must normalize or establish equivalent validity. Internal IR
jumps are not automatically new qualified entries merely because ordinary
ExitFunction has a full-flush wrapper. The authored requirement for an actual
chain-entry binding and explicit alternate-route treatment covers that boundary.

Lifetime and event handling are correctly separate from numeric authority tags.
Code, spills, backing and entry identity stay pinned through repair; repair must
not replay the completed prefix to mint a fresh entry. A completed access changes
the disposition/result phase, so old-RAX fault recovery cannot stand in for the
successful continuation. Real landing consumes the old phase and binds current
semantic/live owners without rolling back memory, event or timer progress. A
helper mutation must invalidate or reconcile the affected admission.

No substantive return. Accept this interface construction and its source seam,
not an implemented resident record, full profile field catalog, native allocated
map, H recovery, all optimizer/entry conformance, measured overhead or generator
selection. The next adapter work must produce the missing resident facts and bind
them to actual chain/landing code; more supplied-witness JSON alone cannot do so.

## Return 27 — root independent review of L entry and dispositions, claim 37

Root verified seven retained input/source hashes and inspected pinned FEX
ExitFunctionLink, dispatcher fill/lookup paths, ExitFunction and LoadMemTSO.
The source supports the key distinction: a link callback does not execute on
each warmed entry, and loop-top dispatch is not always a canonical-state refill.
The selected target veneer therefore supplies an explicit entry seam without
inventing CPUState currency or forcing a host-dispatch visit.

Two integration issues were returned and corrected before acceptance. First,
entry bytes must already be protected before the target can acquire its own pin;
the revised text retains incoming references and in-flight transfers through the
existing parking/reclamation rule. Second, upstream live branch patching is not
our selected immutable-code policy. The revision explicitly uses immutable
transfer instructions and guarded runtime-owned data links, with atomic target
updates under the existing coordinator and lifetime rules. Upstream backpatching
is source motivation only. These corrections are architectural, not cosmetic.

Accept the scoped L entry/continuation construction. Repair retains the exact
precommit native context and load PC. Completed service restores the actual load
result destination and bound post-load sequence, or materializes committed MOV
state at guest+10 without replaying the suffix. Guest fault materializes old RAX
at guest+7. The full native continuation context is distinct from the32 guest
reconstruction roots; fixed upper backing still needs actual publication and
owner validity. Saved RF and live event/memory state are not rolled back.

No substantive return remains. This selects an adapter interface, not acceptance
of unwritten native code or the complete producer. AUTHORITY remains missing in
the diagnostic, appropriately. H, full profile/recovery coverage, actual build
conformance, measured costs and production generator selection remain open.


## Return 28 — matched x87 arithmetic/commit boundary

**Reviewer:** root, independent of machine's claim38 authorship, September20.
**Verdict:** accept the scoped source-backed component/interface choice.

Inspected the retained Intel 253665-078US §§8.5.1.2/Table8-10 and8.7.1,
QEMU helper_fsqrt/floatx80_sqrt/flag merge/waiting delivery, FEX decode,
full-precision stack result and fallback, and Box64's sampled ARM64 FSQRT/FWAIT
paths. All19 retained source hashes match. Intel requires unchanged source/TOP
for unmasked invalid and deferred #MF before the next waiting instruction.
QEMU's sampled helper assigns ST0 unconditionally; its pure arithmetic function
has a separable result/status interface. FEX's full80 path exists but its sampled
stack assignment and FWAIT no-op require adaptation. Box64's optional pending
check is correctly retained rather than described as universally absent.

The resident transaction preserves ST0/TOP/tags in this fixture, merges status
once, separates arithmetic metadata from waiting delivery, and binds cached
state to the same owner. Accepting that source construction requires no mock.
No whole-generator ranking, numerical certification, full x87 exception catalog,
NE=0 bridge closure or measured performance follows. Those explicit remaining
obligations do not contradict the bounded FSQRT(-1)/FWAIT result. No substantive
return remains.

## Return 29 — independent review of claim 39's successful state-image boundary

Reviewer: machine; author: root. September 20, 2026. **Accept scoped source
findings and the conditional resident-owner/field-encoding choice.** No
substantive return. This is not acceptance of all FXSAVE/FXRSTOR fault behavior,
XSAVE coverage or an implemented/native state-image adapter.

Independently inspected Intel 253666-082US FXSAVE/FXRSTOR text in retained
`/private/tmp/natemu-track03-split-cmpxchg-01/intel-vol2a.txt`, and Vol1
253665-078US §§10.5.1.2–10.5.3 at
`/private/tmp/natemu-track03-x87-01/natemu-review02-vol1.txt` lines14808–14864.
The mode-dependent pointer fields, physical abridged tags versus logical ST
slots, non-waiting behavior, software-owned bytes464–511, ignored MXCSR_MASK
and non64 XMM8–15 access exclusions support the stated field-level contract.
The OSFXSR=0 untouched-SSE choice is a documented permitted policy, not an
inference from a missing implementation. It must remain consistent with the
selected CPU/profile as the author states.

Read the actual pinned QEMU `do_xsave_fpu` and `do_xrstor_fpu`: save writes zero
instruction/data pointer fields and omits FOP; restore has no matching metadata
reads. Their TOP/tag/full80 operations are reusable fragments, not a complete
metadata owner. Read pinned FEX Vector.cpp `SaveX87State`, `RestoreX87State`,
`GetMXCSR` and `RestoreMXCSRState` from the retained non-SVE source tree. Layout
comments do not create FIP/FDP/FOP stores, and the actual `0xFFC0` masks discard
sticky status bits. The author's specific source claims are accurate without
asserting global absence across uninspected paths.

The selected resident owner composes with claim38: bind current cached/register
state before serialization, preserve all defined metadata/status, and update
or invalidate caches after restoration. Treating MXCSR_MASK as a CPU capability
rather than imported state is necessary. Field access exclusions prohibit
promoting a convenient complete512-byte buffer copy to architectural semantics.
Reserved-byte effects and faults on multiple accesses remain explicitly open;
that does not block accepting the successful ordinary-RAM encoding boundary.
No memory operation, code mutation or native execution was performed in review.

## Return 30 — independent review of claim 40's two state-image cases

Reviewer: machine; author: root. September20, source/interface only.
**Accept the ignored-suffix admission and valid-memory invalid-MXCSR cases in
their stated conditional scope.** No substantive return. No executed fixture,
complete FXSAVE/FXRSTOR fault-order proof or MMIO transaction acceptance follows.

Read Intel Vol1 253665-078US §10.5.1 at retained text lines14784–14789 and
§§10.5.1.2–10.5.3: bytes416–511 are unused; non64 XMM8–15 fields are explicitly
not accessed; MXCSR_MASK is ignored on restore. Independently checked the
geometry: 0xE60 is16-byte aligned, 0xE60+415=0xFFF, and unused offset416 begins
at0x1000. Excluding a suffix-only unmapped page is justified by the actual field
rule, not an assumption about conservative host probes. QEMU's inspected
helper_fxsave/helper_fxrstor use sizeof(X86LegacyXSaveArea) in access_prepare;
the pinned cpu.h build assertion fixes that size at512. The author accurately
distinguishes this upstream preparation extent from architectural field access.

For the second case, Vol1 §10.5.3 gives illegal MXCSR -> #GP and otherwise
loading state; Vol2A082 FXRSTOR supplies the reserved-bit fault. Read Vol3A092
§§7.5–7.6 including the explicit exception to generally restartable faults.
Private staging followed by validation before live install is a supported
construction for the stated fully available ordinary-RAM invalid-value case.
The author does not derive a universal extended-state rollback rule from that
general text. Old FP state/cache coherence and fault PC are preserved, while
walk effects remain separately owned. Competing memory faults and partial
external effects are expressly not settled, appropriately preventing this
acceptance from licensing whole-image reads or all-or-nothing MMIO behavior.
No code change or native execution was performed during review.

## Return 31 — independent review of claim41 alignment and fault scope

**Reviewer:** machine; author root. **Disposition:** accept the scoped source
facts and proposed alignment/FXSAVE priority choice. No substantive return;
complete FXRSTOR priority or state-image fault semantics are not accepted.
Read the retained Intel253666-082US text independently at
`/private/tmp/natemu-track03-split-cmpxchg-01/intel-vol2a.txt`, FXRSTOR
pp3-460–461 and FXSAVE pp3-468–469, including the implementation note.
[Primary Intel reference](https://cdrdv2-public.intel.com/812383/253666-sdm-vol-2a.pdf).
No new source campaign, code or execution.

Both exception lists explicitly permit #GP instead of #AC for misalignment
under enabled alignment checking. Selecting #GP(0) consistently for those two
instructions, while retaining other earlier checks and ordinary-instruction #AC,
is within that permission. The FXSAVE note expressly permits differing #GP/#PF
ordering between implementations; the stated early alignment check is therefore
a supported candidate for the case with preceding checks passed. It is not a
claim that operand translation already occurred or that every physical processor
uses that order. An implementation need not perform an operand walk simply to
learn alignment; already performed instruction-fetch effects remain distinct.

The author correctly declines to transfer FXSAVE's implementation note to the
FXRSTOR entry, which lacks it. The accepted fully accessible invalid-MXCSR case
in40 does not settle a competing restore page fault. The geometry inference is
also correct for the named4KiB regime: aligned16-byte slot starts cannot split
a16-byte XMM field or10-byte x87 payload across a page, while later used slots
may still occupy another page. This narrows the unresolved partial-save problem
without licensing a whole512-byte probe or discarding ignored-field rules.


## Return 32 — claim42 RAM staging and external-effect recovery

**Reviewer:** root, independent of memory's authorship. **Verdict:** accept the
scoped ordinary-RAM staging and completed-effect recovery contract; complete
FXSAVE/FXRSTOR and question5 remain open at the named competing-fault/UC seams.

Read the complete claim, Intel3A092 §§11.1.1/11.2.3.1/14.3 and pinned QEMU
access_prepare_mmu. Intel permits visible completed constituent stores on a
later fault; it does not require an explicit write before every such fault.
Pre-admission of the used spans therefore selects a permissible zero-payload-
write outcome for the stated single-fault RAM case. This is not a rollback
claim for an already issued store. The0xF00 image example places XMM6's first
byte on the next page correctly. Ignored suffixes remain excluded.

The distinction among register publication, image effects and walker A/D effects
is essential. Staged restore cannot erase prior walk writes, and guest-visible
fault/restart is distinct from internal repair of the same operation. Retained
route/identity/completion records prevent a completed endpoint beat from being
replayed merely because native coordination interrupted it. Upstream page
preparation and incremental live-state mutation are adaptation seams, not the
independent ISA oracle.

WB constituent-order freedom is not generalized to UC. The primary UC note
permits implementation variation, but42 intentionally does not accept an actual
beat sequence yet. Neither that note nor between-instruction Table7-2 settles
competing FXRSTOR operand checks. Those remain precise design decisions; no
full semantic implementation, broad all-instruction prevalidation requirement,
minimum cost or runtime result follows from this review.


## Return 33 — claim43 FX check precedence and UC sequence

**Reviewer:** root, independent of memory's authorship. **Verdict:** accept43
and the scoped FXSAVE/FXRSTOR design composed from39–43.

Independently read the retained Intel Vol3B079 §23.25.2: its implementation
variation rule concerns exceptions within fetch/decode/execution categories,
so it supplies the previously missing execution-priority basis. Rechecked
Vol2A082's FX instruction exceptions, including CR0.EM/TS, feature/LOCK #UD,
MXCSR rejection and permitted alignment variants. No cited instruction rule
contradicts the selected within-execution check order. The construction preserves
major categories and does not use Table7-2 to invent an operand-fault priority.

The selected all-used-span admission gives PF precedence over an otherwise
accessible invalid MXCSR on a later-page-fault case; alignment is checked before
operand walking. Successful admission does not suppress subsequent illegal
MXCSR detection. OSFXSR exclusions and unused bytes remain inherited from the
accepted field schema, rather than a whole512-byte access.

Intel3A092 §§11.1.1/14.3 support a multi-access, implementation-dependent UC
realization. The finite increasing-offset8/4/2/1 field-contained sequence is a
concrete virtual choice with no speculative extra accesses or erased effects.
It does not inherit WB ordering freedom as a UC rule. Endpoint-defined width
responses and exact-once continuation remain mandatory; backend failure is not
a fabricated page fault. Private staging and admitted routes compose with42.

This closes the stated FX design uncertainty without requiring a physical
micro-operation trace. Actual adapters are unverified, and XSAVE/XRSTOR, other
state-image instructions, broad FP semantics and full question5 remain separate.
No codegen ranking, execution or performance conclusion is accepted.


## Return 34 — claim44 standard XSAVE/XRSTOR state contract

**Reviewer:** root, independent of memory's authorship. **Verdict:** accept44
for the exposed standard three-component profile.

Read the full proposal and independently checked retained Intel Vol1
§§13.6–13.8/13.13 and Vol2D092 XSAVE/XRSTOR descriptions/operations. The
conservative always-in-use choice is explicitly permitted. XSAVE's header
read/modify/write remains even for an empty request; its formula reduces to
old_BV|R under that choice. XRSTOR's standard MXCSR exception applies to either
SSE or AVX request independently of component bitmap initialization. The
request mask, header validity and initialization mask are correctly distinct.

The initial x87/vector states and preservation of mode-inaccessible upper
registers agree with the primary descriptions. Section13.13 permits additional
component accesses without permitting modification of inaccessible state; the
selected sparse realization does not falsely turn FX access exclusions into
a universal XSAVE prohibition. Standard header checks remain distinct from
compacted/supervisor forms that this CPU does not expose.

Rechecked pinned do_xrstor: MXCSR is indeed under the SSE-request branch while
YMM handling is separate. The R=4 case therefore requires the stated adaptation;
matching header predicates alone cannot validate unchanged helper reuse. Header
and payload staging, captured input and completed-effect ownership compose with
42–43 without rereading a side-effecting header after an internal pause.

No substantive return remains for44. This accepts the state/effect design, not
an implemented adapter or all FP semantics. Legacy environment images, numeric
compatibility, precise delivery and generator selection retain their owners.
No new runtime or performance result is inferred.

## Return 35 — claim46 component-choice decision review

**Reviewer:** translator, independent of root's proposal. **Initial disposition:**
source/interface fit is supported; return one rationale qualification before
accepting the component choice. No runtime or additional comparative experiment
is requested.

Read46 with the accepted common-interface, matched-memory and recovery findings
21–23/27–29. Rechecked pinned QEMU `translate.c:3390–3402` globals/deferred-CC
bindings and `docs/devel/tcg-ops.rst:154–175` helper effects. They form an actual
frontend/IR/helper producer chain. The helper annotations are coarse global
read/write/exception contracts, not an already complete per-field captive effect
manifest. The proposal correctly retains that adaptation and rejects wholesale
CPU-object, softMMU, host-loop/longjmp and native pointer reuse. Captive native
RAM, TSO, resident recovery and boundary/lifetime rules remain architectural
requirements; upstream system coverage is not used as an automatic runtime fit.
No inherent requirement for per-instruction full spills or host exits is added.

**R35-1 — clarify the non-unique selection criterion.** Keeping an existing
frontend-to-IR producer relationship also describes FEX's own frontend/generator
and Box64's own lowering. Avoiding a new cross-IR bridge favors TCG *conditional
on selecting the QEMU frontend*; that conditional fact alone cannot establish
why the simultaneously selected frontend is preferable to the other coherent
combinations. The text already rejects measured cost/speed rankings and keeps
alternatives viable, but its stated preference can still be read as comparative
evidence that the first row uniquely satisfies coherence.

**Consequence:** readers could treat a circular semantic-component preference as
an independently established lower-integration-cost generator finding, reviving
the previously withdrawn system-runtime weighting under a different label.
This is a rationale issue, not a source finding that TCG is unsuitable.

**Smallest remedy:** explicitly label the tie-break as a reversible planning
policy to keep the selected QEMU semantic/helper provenance in its existing
producer chain, rather than a demonstrated integration advantage over coherent
FEX/Box alternatives. State the actual tradeoff: accept TCG's evidenced canonical
synchronization baseline and possible optimization headroom in exchange for
starting with that existing producer/helper relationship; changing semantic
frontend provenance could reverse the choice without contradicting the evidence.
Do not claim FEX necessarily needs a QEMU IR bridge, two authoritative frames or
per-instruction conversions. No extra source survey, numeric ranking or new
experiment is necessary to make that policy explicit and reviewable.

**Closure check:** re-read the revised criterion/table conclusion and confirm
that all three coherent alternatives can exploit the shim, while the chosen
first combination is an explicit policy under uncertainty. Full semantic
contracts, implementation conformance and end-to-end benefit remain separate.

**R35-1 recheck and final disposition:** root revised46's criterion and post-table
rationale. It now states a reversible QEMU-semantic-provenance policy, explicitly
recognizes the coherence of the other own-frontend combinations, limits the
cross-IR comparison to the QEMU-frontend hybrid, and names the canonical-sync/
optimization tradeoff. Independently re-read those changes with the mandatory
captive transformations. The return is closed.

**Accept46 as the first component combination to carry into planning, conditional
on the remaining semantic contracts and synthesis decision.** This is a legitimate
explicit integration policy supported by an existing producer relationship,
not source proof of the least adaptation cost or fastest generator. Native
paging/TSO/shim benefits remain available to every candidate; no whole upstream
runtime is selected. FEX/Box remain viable alternatives, and new cost/semantic
findings may reopen the policy. No implementation authority, complete question2,
full exporter correctness or demonstrated end-to-end advantage is accepted.

## Return 37 — independent claim47 binary NaN policy review

**Reviewer:** memory; author root. Source-only review; no producer modification,
FP execution or unmasked-helper conformance claim. Return36 is reserved for the
separate legacy-environment review.

**Disposition: accept claim47's scoped existing-library correction.** Independently
read Intel253665-078US §4.8.3.5/Table4-7 and footnote (retained
`/private/tmp/natemu-review02-vol1.txt`, lines5738–5777), plus pinned
`fpu/softfloat-parts.c.inc` `pick_nan`, `addsub`, `target/i386/ops_sse.h`
`SSE_HELPER_S`/`FPU_ADD`/MIN/MAX and `cpu_init_fp_statuses` in
`target/i386/tcg/fpu_helper.c`. These support both the payload result and the
status qualification; this is not acceptance based only on the upstream TODO.

- Table4-7 selects the first semantic source for two NaNs in the applicable
  SSE/AVX arithmetic cases, quieting it when necessary. The surrounding text
  separates masked-result behavior from unmasked destination suppression. The
  footnote supports the legacy-versus-VEX source-position distinction. Thus the
  proposal correctly retains argument identity rather than assuming numeric
  commutativity allows swapping operands.
- `pick_nan` raises invalid/signaling flags before selection if *either* input
  is signaling. `float_2nan_prop_ab` then chooses NaN `a`, otherwise `b`, and
  quiets a selected signaling NaN. First-QNaN/second-SNaN therefore returns the
  first payload while recording invalid; choosing the quiet input does not lose
  the second input's signaling condition. Default-NaN mode must be disabled as
  the proposal says. The add/sub path checks NaNs before ordinary arithmetic;
  the existing claim11 ADDSD source path remains applicable.
- The scalar-double helper passes `v` then `s` to `float64_add`; the adaptation
  must bind those to first and second architectural sources. This review accepts
  that explicit binding requirement, not that every actual adapter/IR transform
  already satisfies it. Existing MIN/MAX directly compare/select under their
  special second-operand rule, while initialization separately sets the three-NaN
  FMA policy. Restricting this correction to the two-NaN arithmetic policy and
  retaining x87/other instruction rules is necessary and correctly stated.

**Practical consequence:** the known ADDSD binary-NaN discrepancy has a concrete
correction using existing SoftFloat configuration; no handwritten FP algorithm
or payload-compatibility waiver is needed for that case. Result staging, sticky
status and suppression on an unmasked exception remain duties of the resident
owner; the helper's direct result assignment is not thereby accepted unchanged.
This does not certify all SSE/AVX reductions, FMA, conversions, numerical
accuracy or emitted native fast paths. No substantive return remains for47.


## Return 36 — claim45 legacy x87 environment/state-image family

**Reviewer:** root, independent of memory's authorship. **Verdict:** accept the
scoped owner, format-dispatch and completion/recovery interface. The explicit
protected14-byte unrepresented-FOP rule is still unresolved; full bit-level
legacy-image conformance is not accepted.

Read45 and independently inspected Intel Vol2A082 FLDENV/FRSTOR/FSAVE/FSTENV
entries, Vol3B079 §§23.18.4/23.18.6.9 and pinned helper_fstenv/helper_fldenv.
Waiting and nonwaiting forms preserve distinct instruction identities; save
completion precedes reset or exception masking. Empty/nonempty tags plus raw
full80 values have primary architectural support, including reconstruction of
nonempty classifications on save. Alignment checking of the environment cannot
be confused with native splitting of contiguous10-byte stack payloads.

The inspected source defects are concrete: FLDENV asks for store admission,
the restore helper omits metadata, and FNSTENV's helper lacks the required
post-save mask effect. These require adaptation, not a new floating-point
engine. Staged CPU publication remains permissible under the instruction's
fault-state limits; completed memory/device/walk effects remain separate.

The four format distinction is necessary; it does not authorize general
real-mode execution. The unresolved unrepresented opcode cannot be fabricated
from extra input bytes. Because the owner retains both old and staged metadata,
its eventual supported binding does not require another runtime architecture.
Resolve that precise rule before claiming complete bit-level behavior.

No other substantive return for this scoped family interface. The remaining
semantic categories are correctly separated from ordinary field/helper
conformance work. No runtime, complete FP correctness or generator ranking
is accepted by this review.
## Return 38 — claim48 FP effect owner and binary64 incompatibility

**Reviewer:** root, independent of memory's authorship. **Verdict:** accept the
scoped effect interface and source/mathematical counterexample; no blanket
numerical-engine acceptance follows.

Read Intel Vol1 §§8.3.10 and8.5.4–8.5.6 and the pinned QEMU FSIN/FCOS/FSINCOS
paths directly. For x=2^-29, the sine difference is between x^3/6-x^5/120 and
x^3/6. It exceeds5×2^-92 while the full80 spacing belowx is2^-93 and the
binary64 spacing is2^-82. Neither x nor any other binary64 output satisfies the
reduced-argument error bound. This is an exact representation argument, not a
libm experiment or a claim that all upstream arithmetic is wrong.

The specification distinguishes suppressed memory destinations from exponent-
biased unmasked register overflow/underflow results, and permits result
publication with unmasked precision. The proposed status/disposition owner
expresses those cases without erasing completed input effects or treating old
sticky status as a new exception. Adapting a helper's direct state assignments
remains necessary. The four trig replacements and ordinary arithmetic's scaled-
result API need concrete component bindings; claim49 is reviewed separately.
Protected14-byte FOP remains an exact metadata-binding uncertainty, not evidence
against a generator. No runtime, whole question2 or question5 closure is claimed.
## Return 39 — claim49 inherited numeric components and status bridge

**Reviewer:** root, independent of memory. **Verdict:** accept the scoped
component/interface choice under its explicit numerical-conformance premise.
This does not establish that premise or accept the whole FP implementation.

Read the retained Bochs fsincos core, constants, wrapper, software-status
declaration and extended round/pack. The core has full80 input/output, bounded
integer reduction and wider intermediates; its selected66-bit constant and
separate entry identities are concrete. The wrapper forces full extended
precision while retaining guest rounding. Exception masks reach the existing
0x6000 rebias path; ferr distinguishes memory suppression from scaled register
publication and unmasked precision. The resident owner can use these numerical
interfaces without importing Bochs CPU ownership. Explicit field/flag mapping
and isolated dependencies are necessary; a typedef cast is not a bridge.

The review requested a separate binding for ordinary register arithmetic:
replacing four trig entries alone would not answer claim48's scaled-result
requirement. The author identified QEMU's existing rebias controls. Root checked
float_status, partsN(uncanon_normal), the15-bit format's24576 bias and the full/
reduced-precision floatx80 packer. The amended per-operation mask binding reuses
that capability; memory conversions remain governed by suppression, not register
rebias. This closes the identified missing-API concern without migrating all
numeric operations or creating new arithmetic.

Suitable representations and status APIs do not prove transcendental error or
monotonicity bounds. The stated premise remains visible and replaceable, not an
observed result. FSCALE extremes, operation-specific C1/status and remaining
numeric compatibility are not certified by this review. No runtime, speed claim,
whole question2 acceptance or implementation authorization follows.
## Return 40 — claim50 event delivery, return and partial-frame refinement

**Reviewer:** root, independent of machine. **Verdict:** accept the scoped
delivery/return construction including its explicitly identified refinement
inference; whole question5 remains open on other recovery/debug obligations.

Read pinned seg_helper/excp_helper and Intel Vol3A §§7.7.2,7.10–7.14, event8,
the ordinary #PF state rule, Vol2A INT/IRET and Vol3C §28.1. The inspected QEMU
IRET clears NMI blocking only after its fallible return helper; the specification
requires it even when execution faults. The old_exception==#DF branch requests
whole-machine reset, whereas the primary table distinguishes benign serial
delivery from local shutdown and defines wake exceptions. Canonical gate-target
checks and EXT provenance cannot be replaced by later fetch faults or unqualified
selector errors. These are concrete adaptation corrections, not inherited
system-emulator coverage serving as correctness evidence.

The initial partial-frame gap now has a precise construction. Intel's indirect
event VM-exit boundary explicitly retains the old transfer tuple while allowing
completed stack writes and preserving acknowledgement/NMI/CR2 effects. Combined
with the ordinary fault state rule and ordered INT pushes, this supports the
selected staged-tuple/surviving-prefix refinement. This is not a claim that every
VMX rule describes ordinary delivery or that all CPUs expose the same prefix.
Root recomputed the0x2010 trace: SS/RSP stores at2008/2000 survive, the RFLAGS
store at1ff8 faults, and the next delivery begins with the original CPU tuple
and updated CR2. Task-switch commits remain explicitly separate.

Internal repair resumes only the unperformed access; actual guest faults abandon
the old delivery, retain effects and select the new pairing. IRQ consumption is
not rolled back. Ordered device beats use the already selected memory/device
contract without probing later pages ahead of effects or fabricating a page fault
for an uncertain backend failure. Code/PTE aliases stay tracked writes, and
native management remains possible through shutdown/repeated delivery failures.
The bounded record needs no host longjmp or growing undo history. Implementation
must bind actual access/state phases to these rules; no runtime or complete
architecture acceptance is inferred.
## Return 41 — claim51 retirement ownership; fusion semantics remain provisional

**Reviewer:** root, independent of machine. **Verdict:** the bounded retirement/
delivery interface is supported; the exact combined DR6 rule is not accepted as
established. Keep that semantic subchoice provisional for question5.

Read Intel3A §7.8.3, Vol3B §18.2.3/18.3.1, INT1, retained3C §27.7.3 and pinned
gen_INT1/helper_icebp/breakpoint_handler. SS data-match deferral, INT1's trap
identity, cause-specific DR6 ownership and the upstream unfinished path support
the proposed bounded record and explicit origin/start/next-RIP handoff. Native
repair and block boundaries must not independently consume the record; no
unconditional full-state spill or host exit follows from this requirement.

The combined B-bit conclusion remains an inference with an unresolved ordering
premise. The sources distinguish instruction execution, software-event delivery
and following-boundary observation;3C §26.5.2 explicitly places the MTF boundary
after INT1 delivery, while §27.7.3's privileged-software exclusions and subsequent
subcase do not unambiguously settle this DR6 combination. MTF is not itself a
counterexample to fusion, but ordinary retirement equivalence cannot simply be
assumed. The exact missing justification is that the deferred data condition is
included in INT1's reported #DB before delivery discards pending conditions.

QEMU's TODO, a test lacking the combined-bit oracle and the supplementary author
report do not independently establish that premise under this workflow. This
return does not reject the candidate rule or request an all-opcode campaign.
The smallest remaining resolution is direct semantic support for this ordering,
or separately reviewed decision evidence for this exact combination. Preserve
the candidate and evidence limits; do not mark question5 closed from the
interface's ability to express it. No experiment is authorized by this return.
### Return41 continuation — independent challenge and June2026 revision check

**Reviewer:** translator, independent of claim51 and root's Return41. The narrow
provisional disposition stands. Re-read retained3A §7.8.3,3B §§18.2.3/18.3.1 and
3C081 §§26.5.2/27.7.3. The data condition becomes reportable after the following
instruction; DR6 describes causes sampled when #DB is generated. Calling INT1 a
trap does not by itself establish whether that sampling includes the inhibited
condition before INT1 delivery clears pending conditions. The ordinary exception
loss rule's explicit software-preservation list omits INT1. This leaves fusion
coherent but not established; omission is not a proof of the opposite rule.
MTF's boundary after INT1 delivery challenges an assumed pre-vectoring retirement
boundary, but VMX-specific MTF is not equated with ordinary exception behavior.

Independently opened the official
[Intel3C092 June2026 PDF](https://cdrdv2-public.intel.com/922489/326019-092-sdm-vol-3c.pdf),
§29.7.3, PDF zero-based page95 (printed29-27). It still excludes pending debug
conditions for privileged-software injection, then includes privileged vector1
in a later subcase nested under software-interrupt/software-exception injection.
That subcase delegates to normal INT1-after-MOVSS behavior instead of specifying
its DR6 combination. Added FRED/IDT wording does not resolve that circularity.
The revision therefore does not close the exact ordinary ordering premise.
No cited rule explicitly grants an implementation choice between fusion and
loss. Keep the paper inference provisional; no new experiment, generic search
or implementation is requested by this continuation.

### Return41 continuation — bounded upstream discussion follow-up

**Reviewer:** translator. Acceptance unchanged; source follow-up only. Followed
January7's exact patch/cover and its May13–14 maintainer replies, with narrow
subject/test-continuation searches. No SDM resurvey or runtime. The
[January cover](https://www.spinics.net/lists/kernel/msg5991438.html) explicitly
enables TF in its reproducer, so its reported combined BS+B0 result is not the
requested TF=0 MOV-SS/ICEBP case.

[Sean Christopherson's May13 reply](https://www.spinics.net/lists/kernel/msg6204925.html)
rejects the proposed addition of BS for INT1, citing bare-metal and nonintercepted
VM observations of coincident single-step/INT1. He also rejects copying VMCS bit12
into reserved DR6 and moves pending-cause merging earlier so save/restore cannot
lose its payload. This is useful primary maintainer interpretation and a correction
to v1, not permission to adopt v1 unchanged or a native result for our exact case.
His [May14 continuation](https://www.spinics.net/lists/kernel/msg6206833.html)
reports that the revised change works and plans a v2 plus unit test; it does not
supply the requested TF=0/B0 initial state, final DR6 or exact native fixture.

No exact-case test/result or unambiguous maintainer ordering explanation was
located in this bounded continuation check. That is a search limit, not proof
that no such later work exists. These sources strengthen the pending-cause
implementation rationale but do not close51's exact fusion premise. They are
external author reports, not independently retained native observations from
this project. This finding does not make owning an Intel host a categorical
requirement: sufficiently specific primary semantic evidence could still settle
the choice; the inspected thread has not supplied it. Stop here with the virtual
policy provisional and return the decision to the root reviewer.

Direct retained HTML for both May replies and source URL/length/SHA256 records
are under `/private/tmp/natemu-debug-thread-followup/sources.json`. Web cache
access failed for the replies; the public pages were then fetched directly.
No archive reply, patch or reported result was executed or modified.

## Return 42 — claim52 selected-component gap reconciliation

**Reviewer:** root, independent of memory. **Verdict:** accept as an accurate
decision inventory, not whole question2 acceptance or new numerical evidence.

Compared the synthesis with46–50 and their scoped reviews. It correctly separates
the withdrawn unchanged trig path, a concrete inherited replacement, the still
unproved numerical-conformance premise, actual missing FSCALE/C1 bindings, and
future adapter conformance. A suitable effect-owner type does not supply a
missing numerical result; equally, selecting inherited arithmetic does not
require a new universal proof of every operation. The comparison remains a
reversible policy without a cost/speed ranking or a dependency on the unfinished
FEX diagnostic build. The stated bounded capability check is decision-relevant.

Claim51 has since received Return41: its interface is supported but fusion
semantics remain provisional. That status belongs to question5 and cannot be
closed by this reconciliation. No implementation, runtime or measurement
authorization follows from this inventory acceptance.
## Return 43 — claim53 FSCALE and rounded-x87 numerical boundary

**Reviewer:** root, independent of memory. **Verdict:** accept the explicit
component-boundary refinement and scoped capability binding, not universal
numerical conformance or whole question2 closure.

Read retained extF80_scale, extended round/pack, status helpers, integer rounding,
roundToInt and binary32/64 conversion packers, with their sampled arithmetic
callers. Finite scale magnitude at least65536 remains outside the representable
extended exponent range even after one24576 bias, including the smallest nonzero
subnormal operand. The bounded exponent representative therefore preserves the
massive terminal infinity/zero disposition; ordinary finite scales use their
truncated value. No out-of-range integer conversion or new arithmetic is needed.

The Bochs status exposes the required round-up indication at actual rounding
points, including carry and conversion paths. One engine can return candidate
value and C1 information to the resident owner. The proposed larger rounded-x87
slice is a real change from49's four-trig-only boundary and is accepted as such.
QEMU rebias remains valid evidence, but need not be forced into a duplicate
evaluation with ambiguous comparison across exponent rebias. Other C1 meanings
remain instruction-owner rules, and memory suppression/pop rules remain48's.

TCG/QEMU frontend and SSE/AVX selection are unchanged; isolated numerical
dependencies and field/status mapping still incur real adaptation cost. The
source inspection establishes suitable existing capabilities, not every output
or the disclosed transcendental accuracy premise. Actual call/flag/dependency
binding and numerical validation remain implementation obligations. No runtime
or comparative cost claim follows.

## Return 44 — question5 recovery inventory reconciliation

**Reviewer:** root, independent of machine's synthesis. **Verdict:** accept the
inventory as a bounded account of remaining work, not precise-recovery closure.

Compared its owners with the accepted memory, entry, state-image and delivery
contracts and earlier references to gather-like partial completion. The records
do not yet supply the selected compound-operation mapping named here. One
partial-destination path and one implicit multi-access path can test the shared
progress/roots contract without becoming an opcode catalogue. This is separate
from Return41's unresolved exact debug fusion. FSCALE/C1 capability binding has
since received Return43 and should no longer appear as an unbound numerical API.
Actual register maps, access-site annotations and unwritten adapters are not
new architecture blockers by themselves. No experiment is authorized.

## Return 45 — claim54 gather progress and ENTER internal repair

**Reviewer:** memory, independent of machine's claim54. **Initial disposition:**
the scoped construction is supported, with narrow qualification R45-1 below;
ENTER's expressly unresolved actual-fault rule is not accepted by this review.
No code, build or native execution was performed.

Verified all ten retained source identities in the compound manifest. Read
Intel089 VPGATHERDD VEX.256 description/pseudocode, Intel082 ENTER description
and operation, pinned QEMU gather helper/dispatch and gen_enter, and Bochs
ENTER64/tickle_write_linear with the stated width variants. The selected ascending
lane order satisfies right-to-left fault delivery. Whole-mask normalization is
present before fetches in the chosen primary pseudocode and absent in the sampled
QEMU loop; retaining its normalized unvisited lanes is a real adapter change.
Publishing each destination/mask pair through a retained intermediate phase
preserves completed loads without either rolling back earlier lanes or repeating
an internally completed device access. Prior-element trap/interrupt replacement
and RF need the separate event owner as stated; ordinary C-loop execution alone
is not proof of that behavior.

ENTER's finite phase/cursor, FrameTemp and loaded-value roots suffice for the
shown internal repair sequence without replay. The final-SP write-fault check
is explicit in Intel's description and absent from the inspected QEMU lowering.
Bochs supplies an actual no-data-write extent/check implementation, not primary
permission for its precise alignment, A/D or partial-fault effects. The claim
correctly leaves actual guest-fault register/memory/check ordering unresolved;
neither the generic fault definition nor accepted IDT frame-write semantics
silently closes that different instruction rule. This is a narrow remaining
semantic contract, not a demand to implement every helper before deciding.

**R45-1:** make the selected gather's entry and access qualifications explicit:
Intel disallows equal index/mask/destination register pairs and address size16,
and this instruction never raises #AC. Generic typed accesses must retain those
instruction-specific rules. The smallest remedy is a short qualification beside
the selected eight-lane contract; no new opcode/source campaign is needed.

**Recheck/final verdict:** R45-1 is closed. The added entry-before-effects and
no-#AC qualifications match the inspected primary instruction rules. Accept54's
scoped gather progress owner and ENTER internal-repair construction, including
the concrete unchanged-helper corrections and the precise remaining ENTER
actual-fault/check seam. There is no outstanding review return. This does not
accept that unresolved seam, whole question5, unsampled compound families or
actual emitted/helper conformance. No execution authorization follows.

## Return 46 — source-only debug observer preparation

**Reviewer:** root, independent of memory's method preparation. **Verdict:**
accept the bounded source-level feasibility argument, not an execution method,
observed result or claim51's semantic rule. No native experiment was run.

Read the retained Linux v6.12 `exc_debug_user`, `hw_breakpoint_handler`,
`ptrace_triggered`, `ptrace_get_debugreg`, breakpoint installation and the
upstream `mov_ss_trap.c` sequence. The proposed observation correctly distinguishes
physical debug slots from logical ptrace slots: the perf callback reconstructs
the logical B bit only for a triggered installed breakpoint. The user handler
preserves BS separately and still signals the zero-cause ICEBP case. Reading
post-F1 RIP and logical B0 at the first ptrace signal stop therefore distinguishes
the proposed outcomes under the stated observer conditions; si_code alone does
not. The existing selftest supplies sequence feasibility, not the missing DR6
measurement or a universal Intel contract.

The source mapping is conditional on the actual running kernel, no interfering
notifier/debug consumer, a successful own-child watchpoint setup and both baseline
controls. TF remains clear and CONT introduces no intentional intermediate
instruction boundary. Exact bytes, selector validity, fault classification,
processor/microcode/virtualization identity and model-specific errata remain
required inputs to a concrete producer. No host has yet been identified and no
sample/time budget or producer is approved. A finite host observation would not
by itself establish universal Intel behavior. Keep claim51 provisional; prepare
and review a bounded producer only when an applicable host is available.

## Return 47 — claim55 ENTER fault/check construction

**Reviewer:** memory, independent of machine. **Disposition:** revise the claimed
closure of ordinary-RAM prefix survival (R47-1); retain the supported bounded
construction and separate MMIO/final-check reasoning. Source-only review, no run.

Read retained Intel089 Vol1 §20.3.1 and ENTER, Intel082 ENTER description,
pseudocode and exception list, Intel3A092 §§7.5–7.6/5.8, and the Bochs recipe
already independently inspected in Return45. The MMIO paragraph positively
supports completed device effects surviving a later exception and repeating on
actual guest re-execution. It does not authorize replay during internal repair.
The claim correctly distinguishes those cases and correctly does not rely on
whole-frame non-atomicity alone.

**R47-1, substantive:** the cited rules do not yet establish the same conclusion
for ordinary WB RAM. Section7.5 says fault reporting restores machine state;
section7.6 describes saved registers/stack pointers but does not expressly make
that description exhaustive of the memory-effects guarantee. ENTER's successful
sequential algorithm establishes dependencies and successful results, but does
not by itself specify which preceding stores survive a later fault. A staged
RAM implementation could implement that success order too. The specific MMIO
allowance narrows the broad rollback reading for devices; importing it to WB RAM
requires another argument. Bochs/QEMU prefix behavior is implementation evidence,
not an independent architectural permission. This is not a finding that WB
prefix survival is forbidden, nor a request for exact physical microsequence
matching or global rollback. Smallest remedy: leave this exact WB fault-prefix
premise unresolved rather than report the whole ENTER contract closed, unless an
applicable positive source/composition argument settles it.

The final-check design can be assessed separately. Intel expressly requires
write-permission fault detection at final SP without specifying a target data
transaction. Operand-sized extent is a disclosed interpretation consistent with
the frame-pointer size and the independent inherited recipe, not a measured
width. The no-data-transfer construction supports no target MMIO/debug access;
write permission and actual target-store commit are distinct. Section5.8's D
requirement is triggered by a write, so selecting no new D for this non-writing
phase is consistent with the stated construction; it does not claim hardware
always behaves that way. Translation A effects and later real-store D duties
remain explicit. The ENTER exception list does not require #AC for this synthetic
check; copying Bochs's generic alignment branch is not justified by its existence.
These findings do not silently settle alignment effects of every actual frame
access, or fix the unresolved RAM fault-prefix semantics.

**Recheck/final verdict:** R47-1 is closed by the author's explicit scope
correction. Accept55's supported MMIO/re-execution distinction and selected
non-writing final permission-check interface, with its disclosed extent and
A/D interpretation. Ordinary-WB-RAM fault-prefix legality remains unresolved;
this is not full ENTER or question5 acceptance. No substantive review return
remains against the narrowed claim.

A discriminating next evidence route could use a ptraced own child with a fresh
writable stack page above a protected page: nesting0 ENTER with allocation4096
first stores old BP at oldSP−operandBytes, then places final SP in the protected
page. The parent inspects the first SIGSEGV stop's restart BP/SP/RIP and the
known writable oldSP−operandBytes slot before any child handler executes. A
sentinel distinct from old BP, a writable-final-page success control, and an
allocation0 control distinguish intended writes and setup faults. Exact bounds,
bytes, expected fault address and host applicability require method review;
the observation would establish a named implementation's fault-prefix behavior,
not a universal rollback/partial-write rule. This is a proposed route only, with
no producer or execution authorization.

## Return 48 — retained AMD/KVM exploratory account

**Reviewer:** memory, independent of root's execution/account. **Verdict:** the
retained artifacts support the stated exploratory observations and completed
controls. This is not decision-evidence acceptance, independent replication,
Intel semantic closure, or a universal Linux/hypervisor conclusion. No producer,
remote command or additional sample was executed during this review.

Verified all14 checksums in `coder-amd-01/amd-build-01` and all16 in
`coder-amd-01/amd-explore-01` under `/private/tmp/natemu-track03-raw`, plus each
manifest's four retained input identities. The first is schema1 build-only; the
second freshly builds schema2 with the separately reviewed `--explore` change.
They are not identical-producing-source runs or a replication pair. Direct diff
confirms the producer/wrapper change is the previously reviewed applicability/
purpose change; the exploration's producer, wrapper and retention helper match
the reviewed workspace. Current README differs only by the later outcome/status
paragraph, not by a retroactive mutation of retained sources.

Both recorded strict GCC15.2.0 builds and disassembly commands return0 with empty
stderr. Read both disassemblies: the exploration's exact sites are F1 at1800,
MOV SS/NOP at1802/1804, MOV SS/F1 at1806/1808, ENTER4096,0 at180a and ENTER0,0
at180f, with the CC markers at the expected boundaries. The actual run's ASLR
addresses match these relative offsets. The exploration is one45-second-bounded
`probe --explore` command, return0 in about0.215 seconds, with empty stderr;
manifest records inherited unlimited CPU values for that command and no retry.

Read command04's complete raw output, not merely `controls_valid`:

- Plain ICEBP stops at its post-F1 label with SIGTRAP, logical B0–B3=0 and BS=0.
- Watched MOV SS/NOP stops at its post-NOP label with DR7=0x70001,
  DR6=0xffff0ff1, B0=1 and BS=0. The watched MOV SS/F1 uses the same watch
  configuration and stops at its post-F1 label with DR6=0xffff0ff0, B0=0 and
  BS=0. Neither is the fallback CC-marker stop. Armed TF/RF are clear.
- Faulting ENTER stops at the ENTER start with SIGSEGV/si_code2, si_addr equal
  to final SP `0x7d9314bc4ff8`, and original BP/SP. Its initial sentinel
  `0xa5c3e18796b4d2f0` at the distinct writable frame slot becomes old BP
  `0x1122334455667788`. Saved RF is set on the fault, consistent with the raw
  RFLAGS difference; the producer does not erase it from the observation.
- Writable-final-page and allocation0 controls stop after their CC markers,
  contain saved old BP, and have their expected new BP/SP. All six cases record
  fresh arming/memory checks and terminal SIGKILL cleanup status0x9 before the
  final completed line. No child signal handler ran before inspection.

The retained context reports AuthenticAMD family25/model117/stepping2,
CPUID hypervisor=1, Ryzen7 8845HS branding, Ubuntu26.04.1 and Linux7.0.0-30-generic.
The wider run provenance identifies KVM; the producer alone establishes neither
physical CPU ownership nor which instructions the hypervisor intercepted.
PEEKUSER DR6 remains a Linux-reconstructed API value, not untouched hardware
DR6, and the reviewed v6.12 source chain was not a complete audit of this actual
kernel build. Thus the supported account is **B0 absent and an earlier ENTER
frame store visible in this AMD/KVM/Linux exploratory observation**. It neither
settles claim51's selected Intel rule nor supplies claim55's missing general
ordinary-RAM permission. Those limitations are material, not removed by passing
controls or by this independent artifact review.

## September 20 — bounded setup and Box64 export preparation review

**Reviewer:** `/root/comparison_sources`, separate context from root, who authored
`experiments/track03/codegen-comparison/{setup.py,setup.sh,check_setup.py,run.py,make-fixtures.py,box64-export.py,box64.sh}`.
Two preparation checks were capped at five minutes each with bounded correction
verification, within the owning setup/candidate allowances. This is preparation
review for exploration, not a review of observed generator output.

**Evidence validity: accepted in this scope after corrections.** Inspected the
retention helper reuse, external fresh-run allocation, source/package retention,
pinned image/context checks, Docker CPU/memory/swap controls and forced cleanup.
Found a broken awk URI matcher that bypassed package-size admission; root fixed
it and added checks of the actual expression. Reviewer reran `check_setup.py`:
success/failure retention, overwrite refusal and bounded/excess/malformed/empty
URI cases PASS. Root also changed container names to unique UUIDs and enables
cleanup before creation so a timed-out create is not omitted from cleanup.

For Box64, inspected the minimal ELF fixture producer and compile-only patch
against retained pinned initialization and emission source. The branch precedes
symbol export, preloads, needed-library loading and deferred ELF initialization;
the fixtures contain no interpreter, dependencies or constructors. The exact
instruction limit replaces only `CreateDBnoAlt`'s `FillBlock64` call, and emitted
length/instruction count are checked. Identified that an `emulate` guard alone
misses earlier guest callbacks. Root added guards to `DynaCall`, `DynaRun`,
`EmuRun` and interpreter `Run`. Reviewer read the complete pinned archive at
`/private/tmp/natemu-track03-comparison-raw/box64-source-01/source/upstream.tar.gz`
and independently checked all four exact guard anchors. Also inspected
`GetEntryPoint`: `maplib` is unused and the result is `h->entrypoint + h->delta`,
so this static ELF path does not require the later `AddSymbols` call.
No material preparation return remains in the inspected scope. Native byte
extent includes alignment padding and excludes following literal tables; it is
not a complete relocated executable object. Build success remains unobserved
by this review.

**Decision relevance: accepted only as bounded exploratory preparation.** These
changes make obtaining actual Box64 output and retaining failures possible within
the fixed host-codegen/captive-shim architecture. Performance remains the selection
criterion; neither setup success, source familiarity, byte counts nor this code
review selects a generator/helper. The next result is usable output or a concrete
build/export failure within the existing candidate budget. No new recovery audit,
unbounded build repair, captive execution authorization, performance acceptance
or whole-architecture conclusion follows from this verdict.

### September21 — native binary64 specialization and numerical-service source review

`/root/direction_review`, independent source review for the00:22UTC integrated
package. The reviewer authored the earlier TCG helper build, not this proposed
numerical contract; no execution or producer acceptance is supplied here.
Inspected the retained QEMU pin's `fpu/softfloat.c:can_use_fpu`, `float64_gen2`,
add/sub/mul/div pre/post predicates and `float64_sqrt`,
`include/fpu/softfloat-types.h`, `target/arm/tcg/vfp_helper.c` AFP behavior,
FEX `Vector.cpp:GetMXCSR/RestoreMXCSRState`, and existing claims47/48/53.

**Evidence validity — accept the conditional SSE specialization.** For scalar
binary64 ADD/SUB/MUL/DIV/SQRT, admit only all six guest exception masks set,
nearest-even rounding, DAZ/FTZ clear, already-sticky inexact, normal nonzero
inputs (exponent1..2046), and positive input for SQRT. Retain the operands and
old destination, compute through the actual inherited/native operation into
staged state, and commit only if the result exponent is2..2046. Other cases
use the checked inherited semantic service.

This is a conservative refinement of the pinned QEMU normal fast path.
`can_use_fpu` requires inexact already set and nearest-even; the proposed inputs
satisfy the operation-specific prechecks (including normal nonzero divisor).
The result range strictly excludes infinity and the `abs(result)<=DBL_MIN`
region in `float64_gen2` that may require software underflow handling. Invalid,
denormal operand and divide-by-zero are excluded by admission; overflow and
tininess are excluded by postclassification. The only remaining possible new
exception is inexact, which is already sticky and masked. Therefore preserving
the complete old MXCSR status is correct in this admitted domain. Positive
normal SQRT also reaches QEMU's native path; its guard is conservative.

FPCR6 supplies nearest rounding, FIZ/FZ clear and the AFP merging behavior used
by the retained scalar code. The inspected ARM AFP source distinguishes AH's
NaN, zero-times-infinity and tiny-result handling; the admitted domain excludes
those distinctions. Preserve the host AFP/readback applicability and exact
scalar upper-lane binding. This reasoning does not establish packed arithmetic,
FMA, comparisons, MIN/MAX, conversion or general nonfinite behavior.

Required details are part of acceptance: admission and commit occur per guest
instruction, including each member of a two-operation fixture; a late fallback
must not replay an earlier committed operation or an input memory read. Native
FPSR after a rejected trial is not guest MXCSR and cannot contaminate fresh
operation-local fallback status. The full-MXCSR owner bypasses FEX's status-bit
masking, preserves prior sticky bits, and stages suppressed destinations under
claim48. Unmasked operations cannot use an old sticky flag as a new fault.
Legacy upper-vector state remains preserved. Comparing TCG requires the same
initial sticky-inexact/control state, rather than the earlier forced-soft state.

**Decision relevance:** the contract supplies a concrete way to price the missing
status/recovery work without changing required SSE behavior or implementing new
floating-point arithmetic. Its ordinary costs include control/input/result
classification, scratch/result staging, extended operand lifetimes, control-flow
edges and code size; fallback frequency and service cost remain explicit. These
costs must be measured as part of the compared adapter, not called cold merely
because an exception is uncommon. No performance preference follows from the
source proof alone, and no execution is authorized by this review.

**x87 return — one correction required.** Retaining cheaper inherited binary64
libm transcendental behavior is consistent with the owner's accuracy/cost
priority if the numerical limitation is disclosed. It neither establishes full80
accuracy nor waives stack/control/status/exception effects. However, limiting
Bochs reuse only to scaled exceptional results omits an already recorded API
gap: claim53 identifies the missing generic QEMU round-up C1 result for rounded
arithmetic and conversions, as well as massive FSCALE handling. Cover these
with the existing status-capable primitive where required, or obtain an explicit
semantic-scope concession; the x87 numerical-accuracy instruction alone is not
that concession. This is a component-capability requirement, not a demand for
Bochs transcendentals, a full80 gate, a TCG generator preference, or an assertion
that one numerical provider is fastest. Once that boundary is explicit, the
existing staged owner supplies a finite architectural direction; implementation
conformance and measured costs remain separately scoped.

**Claim57 return — accept the actual source/design contract.** Read root's
appended claim57 in full. It incorporates per-instruction staging, complete
MXCSR ownership, equal competing status inputs, discard of rejected speculative
FPSR, and the unresolved x87 C1 boundary. The x87 return above is therefore
addressed as an explicit unresolved policy/provider question, not falsely closed.
The SSE construction is independently acceptable without waiting for that answer.

Also directly checked the parallel `float32_gen2`, operation predicates and
`float32_sqrt`: binary32 input exponents1..254 and result exponents2..254 supply
the same conservative argument using FLT_MIN. Accept that scalar binary32
source construction too; this adds no claim that binary32 emitted bindings were
built or exercised. The accepted operations are scalar ADD/SUB/MUL/DIV and
positive SQRT in the stated binary32/binary64 domain, with actual native precision
and lane behavior required by the eventual binding. Packed and other instruction
families remain service-bound as the text states.

**Paired verdict:** evidence validity accepted for this conditional inherited
arithmetic/status design; decision relevance accepted because it makes the
previously missing hot-path work concrete and comparable without an SSE waiver.
Neither component ranking nor experimental execution/implementation conformance
is accepted. No substantive source-contract return remains on claim57. Total
charge9 agent-minutes within this15-minute allocation; no execution or download.

### Root independent review — claim56 recovery construction, September21

**Evidence validity: ACCEPT within the stated native envelope.** Root reviewed
comparison_sources' source-authored claim56 and correction against pinned FEX
RegisterAllocationPass.cpp last-use/free-before-destination and spill allocation,
CPUBackend.h's32-byte maximum slot, Box64's actual instruction-prefix mapping,
and the retained original/live and consolidated branch fixtures. GuestOpcode
alone cannot keep a dead current root alive; metadata-only recovery uses must
participate in allocation and actual native-site validation. The39-value ceiling
is a count of named values, not extra registers or compulsory stores. The64-slot
cap is checked after allocation because allocation can consume fresh slots;
shortening/rejection, not assumed coloring, enforces the bound.

The author's correction properly distinguishes original ADD/PXOR-before-LDR
from consolidated ADD-or-XOR/LDR/PXOR: the latter needs incoming XMM0 at the LDR
fault. Root also checked the existing six-instruction scalar guard uses x1/x2
and leaves NZCV unchanged. It does not require the FP guard's NZCV and candidate
before-image treatment. Equal/backward-edge checks plus bounded lowering and
external veneers supply bounded stop opportunities without universal per-guest-
instruction polling. Non-admitted instructions retain checked semantic execution.

**Decision relevance: ACCEPT as source constructibility, not measured full-adapter
cost.** Already surviving current roots need no forced canonical publication;
local liveness repair and service frequency remain explicit cost terms. This
removes a universal-spill premise from comparison and supplies the actual
recovery construction required for a conditional component inference. No full
ISA native coverage, general emitter conformance or speedup is accepted here.

### Extension versus hybrid comparison — root source/findings review, September21

**Evidence validity: ACCEPT the scoped source comparison authored by
extension_comparison; no new measurements.** Root independently inspected pinned
FEX IRETOp, POPFOp (forcing IF), HLT handling and typed FallbackOpHandler guidance;
QEMU check_cpl0, emit.c.inc CR-write helper emission/block exit and misc_helper.c
CR/APIC/BQL/CPUState dependencies. These support the distinction between opcode
recognition, frontend semantics and resident helper execution. Borrowing selected
helpers does not require TCG, but preserving a complete instruction needs its
frontend checks and all effects. The fourth alternative's compile-time IR bridge
is a proposal, not an implemented or benchmarked backend binding.

**Decision relevance: ACCEPT comparative insufficiency, not a replacement
selection.** Both extended FEX and the hybrid can reuse the measured ordinary
FEX bodies. The prior comparison therefore cannot establish a hybrid preference
against extension. A second ABI has structural costs, but its count alone does
not prove more effort than adding the missing semantics to FEX. Actual semantic
delta, service frequency and bridge/adaptation costs remain unranked. Claim20's
preference and Track03 closure are withdrawn as current direction; retain its
candidate construction and finite observations. No new implementation, benchmark,
Intel acquisition or automatically renewed research campaign follows. The bounded
source comparison ends with this answer and synchronized current records.
