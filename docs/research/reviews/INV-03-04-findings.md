# INV-03-04 — source findings review

- **Reviewer and independent context:** `/root/machine`, independent of author
  `/root/memory` and root's substantive constraints on the candidate. Reviewer
  authored INV-03-03, not this record or its protocols.
- **Material inspected:** full INV-03-04, current workflow/charter, current
  INV-01-30/31/33 scoped findings; Intel revision 092 §§5.8, 5.10.4 and 11.1
  in the directly downloaded PDF's local text; directly retrieved Arm memory
  guide, Arm publication article, Linux v6.12 `tlbflush.h`, QEMU MTTCG 11.1.50
  documentation and VIRTIO 1.3 CSD01 sections cited by the author.
- **Date/verdict:** September 19, 2026 — **accept CLM-03-04-01–06 as bounded
  source/inherited findings.** No accepted all-writer, atomicity, reclamation or
  isolation protocol follows. Architecture acceptance remains unestablished.
  No experiment executed and no new runtime evidence accepted.

## Source findings

The Intel text supports the invalidation-relaxation distinction, cached A/D
qualifications, ordinary atomicity including the AVX-dependent aligned 16-byte
forms, locked A/D updates, split-lock behavior, and self/cross-modification
synchronization. The partial-store statement is permissive, not a requirement
to reproduce every possible hardware decomposition. Native/guest instruction
width correspondence still needs the selected lowering and exposed CPU profile.

The Linux `flush_tlb_all` sequence matches the claim. The Arm guide supports the
address-region and ASID distinction; it is not a substitute for exact TCR,
shareability, BBM and HVF maintenance semantics. The Arm publication article
supports the need for executing-core synchronization and absence of a generic
time-based reclamation argument. QEMU MTTCG describes global safe work, incoming
link cleanup and serialized atomic fallback. It does not prove our VM/native
boundary. VIRTIO 1.3 CSD01 is correctly labeled a draft; its used-ring order and
reset requirements support the scoped source claim. The selected transport is
VIRTIO 1.2 in the machine proposal, so final integration must cite that version
for normative requirements rather than silently advancing versions.

The inherited Track 01 findings are accurately bounded to the M4 two/four-vCPU
fixtures. In particular, immutable live-target replacement is not executable
allocation reuse. Nothing here independently revalidates the old experiments or
turns handwritten fixtures into generated-ISA correctness evidence.

## Concrete unresolved protocol blockers

These block acceptance of the proposed mechanism, not the source findings above.
The next state-machine pass should address them, then return in this thread.

| Blocker and consequence | Smallest remedy | Closure evidence |
| --- | --- | --- |
| Two CPUs can request global quiescence concurrently, including while one is faulting on a tracked write and another holds service state needed by the first | Give one coordinator explicit request arbitration and define each participant state/acknowledgement, including the request originator | Enumerate dual-request, halted, helper, host-exit and translation-wait schedules; prove progress or specified bounded termination without relying on guest IF |
| A forced stop at a native PC can leave a partially committed guest operation or a live return into retired code | Specify canonical boundary conversion versus pinning for each generated operation class | Instruction/substep metadata supplies a concrete resumable state or retains every referenced object until its last possible use; pending faults and helper returns covered |
| Pinned mappings do not alone prove atomic exclusion or source snapshot coherence | Define admission closure and the linearization point for guest accesses, shim writes, A/D updates, host reads and bounce-buffer commits | No observer can enter after closure, every earlier observer/effect drains, and snapshot/commit/reuse occurs only under that invariant |
| Noncanonical-address exclusion can conflict with the protected32 startup requirement if treated uniformly | Define mode-specific effective-address formation and distinguish x86 linear/physical from native VA/IPA | Startup32 segmentation/wrap, long-mode canonical checks, full-range and indirect-target validation preserve the selected x86 rules and exclude private regions |
| Memory-type and invalidation semantics are not closed by eager tracking | Add the exposed CPUID/control-register profile and PAT/MTRR/cache-operation case matrix, including rejected configurations | Each permitted type/alias has a supported ordering/attribute argument; unsupported behavior is discoverable or faults correctly; no fixture-WB-only inference |
| Ordinary unaligned or AVX-width accesses may require global fallback frequently | Tie each exposed atomicity promise to actual emitted instructions or an all-observer fallback | Independently reviewed width/alignment/cache-line cases; qualified Track 04 cost follows separately, with no performance claim from correctness |

**Requested wording clarification, not a source-claim blocker:** the candidate
says not to prevalidate away a *permitted* partial effect. Architecture permits
some partial effects without requiring that exact outcome. State explicitly that
required instruction-specific progress is preserved and the chosen implementation
may select an allowed outcome; already-visible effects cannot be rolled back or
replayed. This avoids accidentally demanding hardware-specific splitting where
an architecturally valid simpler implementation exists.

The proposed no-PCID/no-LA57/4-KiB-and-2-MiB profile is a hypothesis, not an
accepted compatibility narrowing. Required mode/page-size behavior must be checked
against actual stock kernels and startup32. A source proof cannot select away
required scope merely because it resembles Track 01's finite coverage.

## Budget and independence limits

The bounded 90-minute no-VM state-machine pass is appropriate concrete progress.
Stopping at an unsatisfied invariant means recording and resolving that question,
not killing the entire candidate or claiming track completion. An abstract
model can expose scheduling errors but cannot supply Apple cache/TLBI/TSO
observations or generated-instruction atomicity evidence.

No author-file edits were made. The Intel PDF text was read from
`/private/tmp/natemu-track03-memory-sdm.txt`, the author's direct-source download
extraction, with its sections and revision identified; all normative references
remain the public versioned sources in INV-03-04. No archived history inspected.
Reviewer involvement in the device proposal means any future new all-writer
protocol jointly authored with this reviewer needs another independent context.

## Returns

No return required for CLM-03-04-01–06. Recheck substantive source changes and the
completed protocol in this thread before accepting its design conclusion.

### Return 1 — explicit rendezvous state machine

September 19, reviewer `/root/machine`: inspected the new state-machine
supplement and edited partial-effect/guarded-EL1 sections. **Accept the abstract
exclusion argument conditional on its stated premises; actual protocol/design
acceptance remains open.** No experimental observation is claimed.

The admission mutex supplies a single closure linearization point; already
registered but not yet entered owners remain counted. A single host coordinator
removes the dual-election ambiguity. FIFO requests do not carry implicit authority
to commit stale mappings. Host readers, descriptor snapshots and device commits
are included, not only writers. Canonical acknowledgements exclude generated
returns, helper locks and unaccounted visible effects; native exit alone is not an
acknowledgement. Separate named maintenance grants avoid treating ordinary work
as an exception to admission closure. These definitions establish the stated
abstract non-overlap property if every actual access and acknowledgement satisfies
them. Memory-barrier efficacy and generated-code recovery remain unproved premises.

The earlier review's optional-partial-effect wording clarification is addressed:
required progress must survive; an implementation may choose an allowed fault
outcome without emulating every hardware decomposition. Protected32 has explicit
segment/address-size handling. CPL/control-state roots and W^X transitions are
now named obligations, without inferring their native correctness from Track 01.

**Concrete next challenge:** define the prepared operation descriptor in the
three requested generator traces. Suppose CPU A queues an uncommitted tracked
write, CPU B changes its instruction bytes or mapping first, and CPU A later
commits. The descriptor must identify which instruction has already been decoded,
which architectural operands/progress are authoritative, which permissions may
be reevaluated, and which source changes require redispatch versus retaining that
in-flight instruction. Revalidating a generation is insufficient if the implementation
silently changes the identity of the pending instruction or repeats its effects.
Canonicalization must not discard the only metadata that defines it. This is a
concrete recovery/admission premise, not a counterexample to the abstract token
argument. Smallest remedy: include immutable pending-operation content and its
lifetime in the load/write/REP traces; independently review each race outcome.

Also retain the platform publication premise: an acknowledgement's abstract
release/acquire does not establish completion of VM stores, TLBI or cache
maintenance across HVF by itself. A coordination timeout can contain exceptional
failure but must not become the routine path for valid guest operations. The
new text makes both limits explicit. No author-file edits made, no actual
architecture acceptance granted; review the next substantive traces here.

### Return 2 — native-pause repair and atomic-read source

September 19, same independent reviewer: read rendezvous revision 2, claim 07
and current guarded-EL1/W^X alignment. Independently downloaded the exact pinned
QEMU `tcg/aarch64/tcg-target.c.inc` at commit
`c1c18d1e640b64292859ce9f30f3c344edfb0294` and compared it byte-for-byte with
the author's reading copy. Inspected `tcg_out_qemu_ldst_i128` and its alignment,
`h.aa.atom`, `have_lse2`, LDXP/STXP and LDP branches. Also independently inspected
QEMU v10.0.0 `include/qemu/atomic128.h` and AArch64 atomic128 load/store helpers
as supporting context, without substituting that release for the pinned emitter.

**Verdict: accept CLM-03-04-07's source finding and unchanged-lowering
counterexample; accept revision 2's conditional dependency-cycle remedy. Actual
recovery protocol, atomics and architecture remain unaccepted.** The emitter's
applicable atomic read performs a native writeback. A native write fault cannot
therefore be classified solely by its Arm syndrome as a guest write or dirty-bit
update. The selected execution environment changes a load-bearing assumption;
no actual read-only-shadow experiment is claimed or needed to establish that
source-level mismatch.

Revision 2 corrects the concrete repair/park cycle: a memory-quiescent retained
native continuation can admit one exclusive drain service before it becomes a
canonical reclaimable park. Captured temporaries, metadata, native code and mapping
references stay pinned. Strong retirement and covered invalidation acknowledgement
wait for stale accesses to become impossible. Source changes invalidate future
eligibility, while a forced next-instruction/REP-boundary exit prevents a retained
operation from continuing into newly invalid old instructions. This addresses
Return 1's pending-instruction identity concern at the abstract protocol level.
The emitter's ability to supply those exact boundaries, complete continuation
capture, and bounded service still needs proof. A block-entry poll or PC-to-RIP
map is not that proof.

Remaining concrete challenges: exact native-memory completion at PAUSED, locking
and reentrant fault handling in DRAIN_SERVICE, generated temporary liveness after
a second-half fault, reserved resources needed to drain under memory pressure,
and holding or draining stale routes before shootdown acknowledgement. The text
retains these as premises and does not convert fail-stop into ordinary instruction
support. Native W^X now consistently remains optional; guest exclusion, immutable
admitted bytes, publication and retirement remain required.

The proposed fallback needs one explicit boundary before design acceptance:
ordinary reads under exclusion apply to RAM, not arbitrary MMIO. MMIO requires its
specified width and nonreplayed side effects. For RAM, do not merely skip a failed
STXP and retain a potentially torn LDXP pair: discard the uncommitted internal
load result and perform the whole guest atomic read under exclusion, preserving
guest read permission and no guest dirty update. This is a clarification requested
of the remedy, not an unresolved defect in the accepted source counterexample.
No author-file edits or new experiment by this reviewer.

Return 2 clarification disposition: author revised claim 07's proposed remedy;
reviewer reread it. It now explicitly limits ordinary-read fallback to RAM,
discards unvalidated LDXP temporaries, rereads the complete operand under
all-observer exclusion and preserves MMIO's separate width/side-effect contract.
The requested boundary is addressed. Actual fallback implementation and its
platform/recovery premises remain to be established; no change to scoped verdict.

### Return 3 — forced guest-instruction boundary findings

September 19, same independent reviewer: read claims 08/09 and Hypotheses A/B,
then independently inspected pinned QEMU `tcg.c` instruction-start code generation,
liveness/conditional-branch/global synchronization and register allocation;
`translator.c` entry polling/count loop/exit emission; `translate-all.c` count
selection; `cpu-exec-common.c::curr_cflags`; and x86 `gen_eob`/`i386_tr_tb_stop`.
Reading copies are the pinned source tree under `/private/tmp/natemu-translator-audit/qemu`
and `/private/tmp/natemu-memory-translator.c`. No downloaded code executed.

**Verdict: accept source claims 08/09 and the A-then-stale-B counterexample as
scoped findings.** Metadata has no executable stop check; block-entry polling
cannot retroactively insert a checkpoint after a partially completed instruction.
This disproves that preservation argument for the new protocol, not QEMU's own
SMC handling. Count-one generation is prospective, and ordinary count-one mode
is distinct from debugger single step; it is not an automatically selected
architecture or a cure for internal REP/helper progress.

A real conditional TCG branch participates in the liveness and allocation
synchronization pipeline. Precise source caveat: `tcg_reg_alloc_cbranch` and
`sync_globals` mostly assert that memory coherence has already been established;
`liveness_pass_1` uses `la_bb_sync`, argument liveness carries `SYNC_ARG`, and
register allocation emits required `temp_sync` stores earlier. Calling the
checking function alone does not spill arbitrary native state. The proposed
checkpoint must enter the normal pipeline before optimization/code generation.
Author asked to clarify that wording; the overall source reuse claim is supported.

Hypothesis A is a concrete prospective frontend adaptation, not an implemented
checkpoint or accepted recovery ABI. Taken-edge next-RIP and lazy-CC selector,
RF/interrupt-shadow treatment, compile-time DisasContext mutation, and exact
REP phase remain explicit unresolved obligations. Existing `gen_eob` can perform
single-step/interrupt-shadow work and mutate translation state; a cold-path call
cannot be inserted blindly. Hypothesis B needs complete implicit-memory/helper
classification. Source gives no quantitative cost, but the proposed per-instruction
branch/spill work and block fragmentation must be included in later estimates.
No architecture acceptance or source justification for abandoning required
compatibility follows from this review. No author-file edits or experiment.

Return 3 clarification disposition: independently reread revised claim 09. It
now names the full COND_BRANCH liveness → TS_MEM/SYNC_ARG → temp_sync → coherence
assertion pipeline and explicitly rejects transplanting the assertion routine
alone. The wording issue is addressed; source claims 01–09 have scoped acceptance.
The actual emitted checkpoint and its state/recovery premises remain unaccepted.

### Return 4 — cold frontend context and ordinary/REP checkpoint composition

September 19, machine reviewer: independently read claim 10 and both conditional
checkpoint traces; inspected pinned `target/i386/tcg/translate.c` functions
`gen_update_cc_op`, `gen_update_eip_next/cur`, `gen_set/reset_hflag`, `gen_eob`,
`gen_jmp_rel/csize`, `gen_movs`, `do_gen_rep`, and the translation-end classifier.
Source revision remains `c1c18d1e640b64292859ce9f30f3c344edfb0294`; no code executed.

**Verdict: accept source claim 10 and the specified conditional checkpoint
argument.** These helpers really mutate frontend bookkeeping as well as emit
runtime operations. A cold-only selector store followed by a hot-path omission
is a concrete transformation error; the analogous `pc_save` and terminal-state
leaks follow directly. Separating the cold generation context addresses this
class of error without copying runtime guest state. The candidate explicitly
retains shared TCG branch/liveness processing and field/read-set completeness,
so it is not a claim that a bare C struct copy proves the generator correct.

For admitted ordinary DISAS_NEXT boundaries, forcing nonchaining and invoking
`gen_jmp_rel_csize` with zero displacement computes the following PC through
inherited code-size and PC-relative handling. `gen_eob` supplies inherited RF/
shadow completion behavior. The frontend's TF/inherited-shadow cases instead
become DISAS_EOB_NEXT; retaining them outside this ordinary insertion case is
correct. The proposed query must have real ordering/non-elision semantics;
calling its address private does not establish those semantics, and the record
correctly keeps them as a premise. Values live across the added branch must
retain valid TCG lifetime classes; the text explicitly excludes resurrecting
already dead temporaries.

The nonconditional REP MOVSB ordering agrees with source: load/store, indices,
architectural count, then CC synchronization precede intermediate progress exit.
The candidate preserves the loaded source on destination repair and distinguishes
performed access from retry. Its remaining-iteration context must be captured
inside `do_gen_rep`, before the ordinary post-translation `base.pc_next` update:
there `cur_insn_len` still identifies the REP instruction, and negative length
selects its original PC. The described internal-context snapshot does that.
RF suppression in local flags protects intermediate RF from `gen_eob`, while
completion uses inherited `last`/`done` handling. Keeping final/zero count and
TF/shadow nonloop cases on their inherited paths avoids inventing another
completion policy. Conditional REP insertion and same-instruction source mutation
are explicitly not proven by this MOVSB trace.

No substantive return is needed for the stated scope. This accepts a concrete
conditional source-based construction, not emitted code, a complete recovery ABI,
universal instruction coverage or architecture correctness. Non-elidable query,
exit/frame/result ABI, all specialized exits and preserved versus refetched REP
decode identity remain decisive open cases; the costs include query/branch and
additional synchronization traffic. No edits to the author investigation.

### Return 5 — retained REP continuation and per-allocation reclamation

September 19, machine reviewer: independently read revision 3/claim 11 and the
superseded iteration-exit interpretation. Reread Intel SDM Vol. 3A revision 092
§§7.6 and 11.1.3 in the direct primary-source reading copy, and the pinned QEMU
REP/relative-PC/exit functions already examined for Return 4. No execution.

**Verdict: accept claim 11's scoped source facts and revision 3's conditional
protocol argument.** Intel describes architectural restart and iteration-boundary
interrupt state; it does not make an invisible emulator rendezvous an interrupt.
Its synchronized code-modification rules do not establish a particular Haswell
outcome for arbitrary unsynchronized modification. Retaining the admitted REP
decode across internal stops is therefore a clearly identified emulator policy,
not a false physical-CPU source claim. Actual eligible event/fault abandonment
and subsequent validated fetch remain distinct, as required.

The revision closes the earlier semantic gap: inspectable iteration registers
are independent of native code/reference lifetime. Internal resume retains the
original loop and must not execute the architectural RF/event exit simply to
reach service. The explicit record-or-restore treatment of `env.eip` addresses
the pinned frontend's PC-relative anchor: changing it to the displayed guest RIP
and restoring only the other registers would corrupt later relative updates.
Cold architectural event exit and internal resume now have distinct owned routes.
Final completion releases references from permanent code, not while still running
inside the allocation being released. Current data mapping checks on each new
iteration prevent the retained decode from becoming an exemption to shootdowns.

Per-allocation reclamation is a substantive, coherent change from an arena reset.
The retained union includes native PCs/returns, helper/stack/metadata references
and callbacks; unlink/publisher/maintenance predicates must all hold before reuse.
An invalidated source identity can deny new entry while its allocation remains
immutable for the admitted operation. This avoids waiting for every healthy REP
to exhaust its remaining count before any reclamation or forward progress.
Admission reserves and backpressure are an explicit conditional remedy, not a
proved capacity bound. The author correctly treats systematic exhaustion on
ordinary valid long REP as a falsifier rather than a successful fail-stop policy.

No substantive return for this scope. A real bounded inventory of frame/helper/
callback dependencies, reserve capacity and reclamation metadata is still needed;
finite vCPU count alone does not prove that bound. Query visibility and actual
resume/exit ABIs remain open, as do conditional/string-I/O cases and complete
synchronized code-modification coverage. There is no allocator, implementation,
resource measurement or whole-architecture acceptance. No author-file edits.

### Return 6 — resident inventory and symbolic capacity

September 19, machine reviewer: independently read claim 12 and its complete
inventory/admission argument; inspected pinned `tcg.h`, `translation-block.h`,
AArch64 `tcg_out_goto_tb/goto_ptr`, prologue frame construction and `tcg_gen_code`
overflow/finalization paths. Header reading copies are the cited
`/private/tmp/natemu-memory-tcg.h` and `natemu-memory-translation-block.h`;
backend/source revision remains `c1c18d1e640b64292859ce9f30f3c344edfb0294`.
No code executed.

**Verdict: accept the scoped source facts and conditional capacity construction.**
The backend uses B/BR tail transfers rather than an accumulating generated-call
stack; its fixed prologue frame includes saved registers/static arguments/temps.
The quoted constants and heuristic status of the 4000-op threshold match source.
Final slow-path/pool output is additional to the main instruction loop, so the
text correctly does not promote instruction count or offset limits into a whole
object byte bound. Two outgoing link slots yield at most 2C corresponding link
entries across C objects; they do not bound a transitive reachable graph.

K=3 is an imposed ownership envelope, not a derived upstream theorem. It is
coherent with the submitted nonrecursive generated-execution regime: one current
operation/origin plus a bounded old/new handover, with permanent helper text and
no retained tail-transfer ancestry. Native PC/range lookup plus explicit origin
and handover records must cover an asynchronous stop on either side of a branch.
The author states this critical predicate and forbids starting another uncounted
handover before references are transferred. A native helper return into another
generated object, or recursively entering dispatch, would reopen the bound.

The reserve inequality counts execution pins, admitted outputs, successor slots
and spare capacity separately and conservatively. Under those predicates a paused
REP needs its existing object and permanent drain state, not fresh code per
iteration. Actual event abandonment releases its old native references from
permanent code before ordinary restart. Backpressure and reclaiming eligible
objects can therefore preserve progress without waiting for REP count zero.
Callbacks with slot/generation identities and bounded outstanding ownership avoid
an unbounded old-code or completed-effect history. A full direct-link graph cannot
be pinned as though it were one operation; the explicit unlink rule is essential.

The memory expression is a conditional construction budget, not a numeric bound
for the current implementation. F/A/J/Q/R and whole-object B still require the
enabled helper/accessor/compiler/backend inventory, including a bounded way to
complete each enabled single instruction and repair step. Rejecting a valid
instruction because its single-operation expansion exceeds an arbitrary B would
not satisfy compatibility; the author explicitly treats that as a falsifier.
Likewise reserving code alone cannot cure a shadow/refill wait that requires a
paused operation to release its own resources. Those resident reserves remain
load-bearing premises. No substantive return, implementation memory acceptance,
latency bound or whole-architecture acceptance. No author-file edits.

### Return 7 — A/D transaction, own-delta folding and route lifetime

September 19, machine reviewer: independently read claim 13 and all proposed
schedules; inspected Intel SDM revision 092 §§5.8/5.10, pinned walker
`ptw_translate`, `ptw_setl`, restart branches, and `probe_access_full_mmu` dirty
handling. Source confirms STORE-intent size-zero preparation, low-word
endian-adjusted conditional RAM update, and semantic compare-failure restart.
The earlier MMIO slow path remains a separate semantic gateway obligation.
No source, implementation or guest code executed.

**Source verdict: accept the stated source facts. Protocol return R7-1:** own-delta
folding needs an explicit pre-mutation validation baseline. Suppose another CPU
changes a dependency after a saved walk read but before exclusive entry; the
queued A/D update can still compare-match its particular low word. Assigning the
transaction's post-generation to every saved dependency would then erase an
older mismatch. Exclusion prevents competing writes during service but cannot
retroactively exclude this prior write. Smallest remedy: snapshot current
versions at exclusive entry, retain saved-versus-current mismatches before the
owned mutation, and fold only verified own A/D deltas without clearing that set.
A reusable shadow must not be published as current until those mismatches are
independently resolved. Any required later semantic read gets a new access
identity; prior device effects are not retransmitted to manufacture validation.
Author agreed and is revising this predicate. This return affects the conditional
fold/publication argument, not the accepted A/D source observation.

Other ordering is coherent conditionally. Source/table promotion participates in
the same admission domain; prior revocation, owned effect, generation update and
completion remain one closed transaction. A true later fault keeps earlier A/D
effects. The code-plus-PTE alias uses the existing admitted-instruction/REP rule
and next-boundary stop; Intel does not require identical immediate A/D-driven
self-modification detection on all CPUs, and the proposed conservative coherence
policy is distinguished from that source claim.

The route/code-pin distinction prevents remap deadlock and stale reuse: service
an already admitted route before its conflicting acknowledgement, release it once
no access/replay can use it, and resolve an unadmitted later phase separately.
A saved native retry cannot enter a substituted shadow; explicit completed-access
continuation is the alternative. Host-effect/result delivery must not reacquire
ordinary RAM admission inside its own exclusive service. These are concrete
conditional ordering rules, not a TLBI/barrier proof or universal MMIO-PTE result.
No author-file edits. Final protocol disposition follows R7-1 reread below.

R7-1 disposition: independently reread revised step 3 and own-fold paragraphs.
They now capture and compare the exclusive-entry baseline before mutation,
retain every preexisting mismatch, and limit folding to owned A/D deltas whose
address/permission/routing fields are unchanged. A mismatch prohibits current
reusable-shadow publication; an old route only authorizes its already admitted
access. Missing semantic revalidation transitions remain explicit case blockers,
not permission to replay device effects. **R7-1 is closed; accept claim 13's scoped
source facts and the revised conditional composition.** This does not accept
all raced-walk/MMIO semantics, barriers, checkpoint ABI or the full architecture.

### Return 8 — raced walk and one-access service policy

September 20, machine reviewer: independently read claim 14, retained walk/access
registration and the upper-entry/device-leaf schedule; reread Intel revision 092
§§5.10.2.2–5.10.3.1 and invalidation requirements, and pinned `mmu_translate` /
`x86_cpu_tlb_fill`. The source separates translation result from subsequent TLB
installation. Intel makes TLB retention optional and permits eligible upper
paging-structure entries to remain cached after software changes their backing,
subject to invalidation. This is narrower than permission to reuse arbitrary
stale walk components.

**Return R8-1 and disposition:** requested an explicit retained-value legality
predicate before generic SERVICE_ONLY continuation. Same access and absent
covering acknowledgement are necessary but do not prove legality for every entry
class, fault or control transition. Author added and reviewer reread that gate:
each retained component must remain usable for the paging mode/phase under both
architecture and selected walker; unclassified cases remain unresolved rather
than forced through by the publication bit. Fault/event abandonment and control
transition end the handle; matching numeric RIP/CR3 later does not revive it.
R8-1 is closed.

**Verdict: accept the source facts and revised conditional mechanism, including
the eligible upper-RAM-entry scalar schedule.** For an eligible valid/A-set
nonleaf, the cited cache behavior supplies a concrete reason the earlier child
selection may remain usable while a changed parent is not blessed for future
shadows. Prior device-leaf effects are consumed once under the explicitly chosen
virtual device policy, not claimed universal physical-MMIO behavior. The current
successful scalar access can finish through the service result/continuation;
future accesses get a new walk and no stale reusable shadow. Requested editorial
precision names a four-level PML4E for U so the example cannot be confused with
PAE PDPTE register semantics, which the manual distinguishes.

The prospective walk record closes a real acknowledgement gap: a walk with no
final physical route can still later produce an old translation, so route pins
alone are insufficient. Registering before its first PTE read and retaining it
through final service prevents covered invalidation from completing prematurely.
Each actual physical suboperation separately pins its backing; old walk identity
is not permission to dereference retired host memory. The coordinator must drain
or legally terminate those records, not merely observe no installed shadow.

SERVICE_ONLY is a cache-publication policy, not an exception-priority change or
atomicity recipe. Wider/split accesses still need their complete exact service
rules; true compare-failure restart can issue a new semantic device read with a
new serial, preserving previous effects. Arbitrary races, modes and device-PTE
semantics outside the classified predicate remain open. This is a scoped
architectural construction and legality argument, not a runtime result, a full
walker closure or whole-architecture acceptance. No author-file edits or code
execution.

## Return 9 — claim 15, September 20, machine reviewer

**Accept the scoped source chain, required memory-policy interface and explicitly
conditional realization; not a completed non-WB implementation or architecture.**
Independently inspected pinned `misc_helper.c` PAT/MTRR assignments,
`TranslateResult`, fill use of `cpu_get_mem_attrs` and its x86 definition. The
identified route lacks an effective-type result; the finding does not overclaim
that all QEMU backends ignore memory types. This is an adaptation obligation
at the captive runtime boundary, independent of generator selection.

Independently read Intel 253668-092 §§14.5.2.2, 14.11.6 and 14.12 and Arm
102376_0200_01 sections on speculation, alignment and aliasing. Table 14-7 is a
real combination table (including defined combinations formerly unspecified),
not numeric minimum; UC- must survive until combination. Intel's remapping rules
constrain ordering, write delay and write/readback behavior. Arm Normal-NC still
permits speculation; Device forbids unaligned accesses and incompatible aliases
are not a valid generic repair. The source-backed rejection of “MSR fields plus
ordinary shadow attributes plus TSO” is justified in the stated scope.

The candidate's private Normal-WB backing is defensible only as a virtual access
abstraction with all guest-observable operations mediated, not as an Intel rule
permitting native UC-to-WB remapping. The author says this explicitly. Its
conservative completion ordering must cover earlier/later WB operations and
backend observers, not only order the serviced thread; the all-observer admission
premise and existing coordinator provide the proposed boundary. No passthrough
or unsupervised noncoherent observer can be smuggled into that argument. Device
routing is separate from effective type, so an effective WB MMIO GPA cannot enter
the RAM direct path. Exact width, partial faults and atomic recipes remain needed
on service paths; global serialization alone cannot manufacture them.

Non-WB instruction fetch and paging traffic require their own access policy;
translation lookahead cannot issue device reads speculatively. Policy generation
must invalidate code/shadow eligibility as applicable, not only operand mappings.
The submitted access-class interface includes those obligations and expressly
leaves cache-control/source-fetch/type refinement open. Thus INVD/WBINVD/CLFLUSH
cannot be accepted as no-ops on the basis of this review, nor can the proposed
strong ordering alone close the memory contract. The explicit boot-state manifest
is necessary: disabled MTRRs must not accidentally grant WB fast-path eligibility.

No substantive return against this scoped claim. The next decisive refinement is
the recorded cache-control/fence/policy-transition source pass, including how
already cached translations and paging traffic obey policy changes. No author
file edits, code execution, native-attribute validation or numeric cost acceptance.

## Return 10 — inventory cleanup, September 20, machine reviewer

**Accept the inventory/status cleanup; no new source or architecture acceptance.**
Read the updated current summary, charter inventory, claim-group portability
matrix and historical-action dispositions against the standing captive-EL1/host
architecture. Charter questions 4–7 and the 8/12 interaction responsibilities are
mapped without treating their supporting source verdicts as completed designs.

The distinctions are accurate: claim 07's LDXP/STXP issue is a pinned TCG
lowering finding; claims 08–10 depend on that compiler's state and checkpoint
machinery; claim 11's retained REP policy needs equivalent source/loop identity
elsewhere; claim 12's imposed resource bound requires a generator-specific
reference inventory. Claims 13–14 depend on the chosen walker, not the chosen
code generator alone. Claim 15's memory-type requirement is architectural while
its inspected missing-type chain is QEMU-specific. The common coordinator and
result names do not transfer those proofs automatically.

I requested correction of the stale claim 07 pending-review label and local
next actions pointing to already completed PAT/MTRR and intervening passes.
Reread the corrections: claim 07 now names Return 2; old submission/actions are
visibly historical/completed; the end of claim 14 identifies the subsequently
completed claim 15. Claim 15 correctly names Return 9 and defers the unperformed
cache-control refinement during inventory-only authorization. No prior source
claim or review history was deleted. Current next action is this cleanup review,
not resumed research. Author may now mark that editorial review complete while
leaving the proposed research step unstarted.

No outstanding substantive return. No new source inspection, implementation,
runtime test, cost ranking or full architecture acceptance in this review.

## Return 11 — memory-policy refinement, September 20, translator reviewer

**Reviewer:** `/root/translator`, independent of `/root/memory`; no authorship of
claim 16. **Accept CLM-03-04-16 as a scoped primary-source finding and conditional
interface construction.** No substantive return. This accepts the stated
no-private-dirty-cache realization under its observer/ordering premises, not a
complete memory implementation, all exposed cache opcodes or runtime validation.

Read claim 15, its existing Return 9, the new claim 16 and current summary.
Independently inspected the retained primary text of Intel 253668-092US §§5.9.2,
5.9.3, 11.3, 14.3/14.3.3, 14.5.3/14.5.5 and 14.12.4; Intel 253666-082US
CLFLUSH and INVD; and Intel 334569-092US WBINVD. Copies were the named existing
PDF/text files, not a fresh independent download. No source modification, build,
guest execution or native cache experiment occurred in this review.

The paging-type distinction is material and correct: root-table selection uses
CR3 controls (or PAT index zero in the stated PCID case), lower tables use parent
controls, and the translated operand uses the leaf PAT bit as well. PAE PDPTE
register loading has a separate rule. Leaf WB cannot license speculative UC
paging traffic. The construction retains these mode distinctions rather than
turning a four-level formula into a startup32 restriction.

UC demand capture is a conservative selection, not the Intel fetch granularity.
Section 14.3.3 permits repeated cache-line fetches, consecutive instruction decode
without intervening fetch, and additional line fetches for a nonspeculative
instruction; it forbids speculative transitions/branch targets into UC code.
The proposal explicitly preserves that distinction. Reusing a native translation
only after newly admitted bytes/context match does not excuse required effects
on a side-effecting source route. Device fetch widths/results remain an explicit
machine contract; this review grants no permission to reject otherwise legal
code because a compiler wants a host pointer. The single-instruction boundary is
limited to the selected conservative UC path, not imposed on ordinary WB blocks.

The no-private-dirty-cache argument is conditional but substantive: early
writeback/nonretention can leave no dirty virtual copy for INVD to discard, while
native coherent caches implement the authoritative backing rather than a guest
cache array. This does not identify INVD with WBINVD. Intel serialization drains
pending/store-buffer effects but is distinct from dirty-cache writeback; the
proposed INVD/WBINVD rows maintain that distinction. If a component introduces a
separate dirty guest cache, a noncoherent observer, persistence completion domain
or cache-as-RAM guarantee, the argument must be reopened. Those are falsifiers,
not a general waiver of guest cache-control behavior. The stated machine already
uses mediated/coherent backing; the proposal does not remove ordinary INVD or
WBINVD from the guest instruction surface.

CLFLUSH requires its own maintenance intent. The primary instruction entry
supports byte-load checks with the execute-only-segment exception, A without D,
and independence from the target page's memory type. Resolving a line with those
checks must not consume an MMIO data value. The proposed result and retained
walk/A-update progress therefore address a real interface distinction. Its
ordering includes stores, locked operations and fences; no acceptance of an
optional CLFLUSHOPT/CLWB recipe follows from this row. SFENCE/LFENCE/MFENCE keep
their distinct ordering roles, including LFENCE execution ordering, without being
relabelled universal fetch serialization.

Per-instruction policy transactions avoid holding an internal rendezvous across
the guest's multi-CPU MTRR-update algorithm. Closing admission, draining captured
authority, committing only the issuing vCPU's registers, revoking old-policy
shadow/reply eligibility and redispatching compose at the stated interface.
PAT/MTRR guest synchronization obligations remain; the runtime cannot silently
rewrite other vCPUs' MSRs. No paused native continuation may be called reclaimable,
and no old unperformed service may survive an acknowledged revocation. These
conditions are retained rather than solved by an arbitrary cache flush.

The resulting interface choice is reviewable without implementing a complete
cache/MMU first. Its native attribute/barrier realization, general device-fetch
transaction widths, exact optional-opcode exposure recipes and the combined
architecture remain required work. This review accepts none of those by omission
and establishes no performance or generator preference.

## Return 12 — maintenance and policy-exit composition, September 20, translator reviewer

**Reviewer:** `/root/translator`, independent of `/root/memory`; no authorship of
claim 17. **Accept CLM-03-04-17 as scoped source constraints and conditional
interface composition for its two traces.** No substantive return. This does
not accept all walkers/opcodes, native barriers or any generator's complete
recovery implementation.

Read the final integration section, claim 16, INV-03-01 claim 11's admitted
source/compile-job separation, and the existing typed result/checkpoint contract.
Independently reread retained Intel 253666-082US CLFLUSH pp. 3-162–163,
253668-092US §11.3 and 334569-092US WRMSR pp. 6-8–9. Inspected the pinned
QEMU `gen_WRMSR` and `i386_tr_tb_stop` sources named by the author. These were
existing pinned/local copies, not fresh independent retrieval. No runtime work.

Trace A preserves the distinction between a maintenance operand and a data
load. CLFLUSH requires load-like permissions and A-bit effects, but no final
MMIO data read or D-bit update. The RAM A update can independently be a real
source-code mutation; folding it into existing all-writer authority while retaining
the decoded instruction avoids replay/redecode of an operation whose bytes just
changed. Completed maintenance is a typed success with no loaded value, not an
excuse to retry a now-completed access. Its exact-once continuation and forced
next-instruction checkpoint retain live code/native-state pins until references
end. The forced exit follows this trace's source invalidation, not an assertion
that every CLFLUSH serializes instruction fetch. Later faults retain permitted
earlier walk effects. Side-effecting PTE legality remains expressly separate.

Trace B correctly distinguishes MTRR from arbitrary MSR writes: the inspected
WRMSR entry specifies invalidation of TLB entries including globals for MTRR
writes, while serialization has identified exceptions. This trace uses a valid
cache-policy MTRR write and does not infer PAT/all-MSR TLB invalidation. The
issuing vCPU's architectural invalidation is distinct from shadow/code eligibility
revocation; neither updates peer MSRs. Per-instruction coordination leaves other
guest CPUs able to run their own software update/barrier sequence.

Commit, retirement and next-fetch admission are separate stages with one
retained completion continuation. A pause after successful commit cannot replay
WRMSR; a fault on the following fetch belongs to the following instruction and
cannot roll back the policy write. Current source authority is required even if
an older native successor already exists. The selected UC demand-capture policy
therefore composes with resident ownership of fetched fragments and separate
compiler-job cancellation: a cancelled job cannot erase or repeat completed
side-effecting fetches. No native-PC increment or host-owned architectural state
is smuggled into this argument.

The QEMU frontend's CC/PC publication and EOB selection are useful concrete seams,
but the author correctly leaves whole-pipeline materialization and captive policy
transaction adaptation conditional. FEX/Box64 do not inherit that proof. The
common outcome requires neither a shared IR nor host/shim calls for every ordinary
access. Conditional interface closure is sufficient to proceed with the stated
native attribute/barrier question; implementation is not demanded before accepting
these two composition arguments. General device-fetch widths, profile-specific
optional cache instructions and combined architecture correctness remain open.

## Return 14 — compatible aliases and native completion, claim 18

**Independent machine review, September 20: accept the scoped source argument
and conditional boundary recipe.** Read claim 18, retained Arm memory-barrier
102336_0100_01 sections 6–8 and memory-attribute alias discussion, and rechecked
the inherited-evidence clarification. No runtime or new platform fact is added.

DSB completion is relative to access types and observers; one coordinator DSB
cannot drain another participant's preceding accesses. The selected protocol
requires every resident participant to execute its own completion barrier before
acknowledgement, then excludes all admitted observers during service and completes
the service owner's effects before release. It explicitly preserves native repair
and retained continuations, rather than making guest IF or canonical retirement
a prerequisite. Full-system barriers are conservative boundary work, not a new
barrier on each ordinary WB instruction. Translation/code maintenance and exact
MMIO completion remain separate obligations.

Uniform resident native aliases avoid inventing a native Device/NC alias for
an x86 UC view. They do not prove effective stage-2/host attributes or native UC
equivalence; the non-WB behavior comes from the typed service contract. Existing
Track 01 same-backing observations retain their finite evidentiary scope. The
stronger mapping/ownership transition premise is correctly explicit, and no
barrier is presented as repairing an incompatible mapping.

The resident-copy fallback closes application ownership symmetrically: host
backend writes, descriptor reads, compilation snapshots and inspection all use
copied payloads during resident ownership. Device completion follows committed
RAM effects, not chunk receipt. Initial loading, OS memory activity and teardown
remain outside that narrowed exclusion proof. The finite register-code transfer
is not accepted as an already qualified bulk DMA implementation. Its additional
exits, staging and copies can invalidate its economic suitability; direct host
access remains a candidate requiring its own completion/attribute contract.

No substantive return. Accept this conditional service and ownership direction,
not complete native realization, a production transport choice, platform-wide
coherence or throughput. The next action must resolve the exact added mapping
premise using available capability/evidence, rather than treating another SDK
silence audit as progress.

## Return 15 — platform handoff premise and method, claim 19

**Independent machine review, September 20: accept the scoped source narrowing
and preparation method.** Independently inspected the retained pinned SPTM pmap,
proc_reg definitions, HV dispatch shim and QEMU mapping code. The source does
show COPYBACK/default Outer Shareable write-back construction, a separate inner
write-back branch and the direct host RAM-pointer HVF mapping precedent. The
record correctly avoids identifying those inspected branches with the running
private HVF implementation or deriving an effective alias mismatch from stage-1
shareability labels alone.

The decision question is now the extra two-way ownership handoff, not whether
inherited finite same-backing SMP observations must be discarded. The four-cell
comparison can qualify an operational contract for a named configuration without
claiming to read private stage-2 bits. Direct-host access remains viable; no
unsupported requirement to adopt register transport or add a host exit to normal
WB instructions is introduced.

The corrected copy control is symmetric: resident reads export immutable payloads
through registers, so phase 3 and subsequent round trips do not silently test
host backing access in the supposed copied route. Both directions distinguish
receipt, committed effect, drain acknowledgement and completion. Per-CPU DSB
acknowledgements and epoch checks matter; arbitrary stopped vCPUs and shared ready
words cannot replace them. The tracked-source subcase checks rejection of an old
reply after backend mutation without pretending that an NX data fixture validates
a full generated-code publication pipeline.

The method retains actual words, alias/CPU coverage, phase identity and native
progress, with synthetic rejection of missing or premature events. An ISH/OSH
outcome difference is explicitly correlation requiring diagnosis, not a cache
cause or repair proof. Passing cells cannot establish unspecified attributes,
arbitrary lifecycle behavior, UC/MMIO semantics or universal ordering. These
limits preserve rather than undermine the concrete handoff question.

No substantive return after rechecking the symmetric-copy and correlation
clarifications. Accept preparation of the bounded fixture/oracle, not execution
readiness or platform qualification: native sequences, host completion barriers,
control-channel ownership and producing code still require independent review.
No runtime, production transport choice or combined architecture acceptance is
provided by this review.

## Return 16 — first native-handoff run stopped at boot, claim 21

**Independent root review: accept the finite failure report, no handoff finding.**
Root verified all 44 retained hashes and seven inputs against preparation 03 in
`handoff-reviewed-01`. Only ISH/copy started. Command 16 terminated with exit 2,
without timeout, before boot readbacks or any payload epoch. Both reported HVC
exits have syndrome 0x5a000043 and PC 0x4000006c; the fixture expected the HVC
instruction at 0x40000068. No other cell ran, and no retry is credited.

The current frozen `experiments/track01/source/host.c` checkpoint implementation
explicitly states that Hypervisor.framework reports the return PC after HVC and
checks the instruction at PC minus four. This inherited premise should have been
carried into preparation and root review. The failure is a fixture identity
mismatch, not contrary evidence about handoff, shareability or transport. The
frozen source remains unchanged. The manifest/command failure and closed process
handle establish termination despite the stale per-cell `running` label.

Accept correction of the named instruction/resume-PC distinction and terminal
cell bookkeeping in a fresh versioned preparation, retaining this failed run
unchanged. Recheck the affected producer/oracle/runner before any new decision
execution. No runtime budget follows from this findings acceptance.

## Return 17 — corrected PC accepted, boot readback gate failed, claim 22

**Independent root review: accept finite failure reporting only.** Root verified
all 44 retained hashes of `handoff-reviewed-02`, inspected terminal command/cell
status and raw output. Only ISH/copy ran; it exited 2 without timeout. Both boot
HVC PC checks progressed to six successful register-read API calls, followed by
`FAIL stage1/TSO readback`. No boot-value row or payload epoch was retained. The
corrected runner now records the cell as failed with native return code 2.

The missing values prevent identification of which expected field differs. Do
not infer an ACTLR, MAIR, TCR, SCTLR or descriptor defect, nor reject a memory
transport. The observability gap is in the fixture: it checks before printing.
Accept minimal preparation to retain both CPUs' six actual values before that
same unchanged gate, stopping before payload operations on mismatch. This
requires fresh build retention and affected-method review, not relaxed expected
values or another unchanged matrix. No execution budget is granted here.

## Return 18 — actual boot readbacks retained, claim 23

**Independent root review: accept finite diagnostic reporting, no handoff result.**
Root verified all 44 run hashes and seven preparation-05 inputs for
`handoff-reviewed-03`. Only ISH/copy ran, ending with exit 2/no timeout at the
unchanged boot gate. Both retained boot rows report ACTLR 2, MAIR ff,
TCR 200803510, SCTLR 30d01985 and both descriptors 60000040010703 (hex).
All expected fields except SCTLR agree; XOR against the expected 30d01805 is
180, bits 7/8. There are no payload acknowledgements or epochs.

These values belong to this run only; they do not recover the missing values
in claim 22. No field-name guess establishes why the bits read as one. The
proposed primary-source check is the smallest useful next step before changing
any invariant. Do not mask arbitrary readback differences, infer a transport
failure or widen the test. No retry budget is granted by this acceptance.

## Return 19 — exact SCTLR setup invariant, claim 24

**Independent machine source/configuration review, September 20: accept the
scoped interpretation and proposed explicit setup.** Parsed the complete retained
SCTLR_EL1 JSON, independently matched it to complete record 681 in the truncated
register-package prefix, checked its stated hash and the HTML/README hashes,
and read both full SED/ITD conditions. No dependence on the missing package tail
or claim of direct Arm hosting is introduced.

The conditional JSON fields are bits 8/7, implemented under FEAT_AA32EL0 and
otherwise RES1. The Arm-authored prose identifies their AArch32 EL0 effects; it
also retains the optional ITD RAZ/WI case and the SED mixed-endian restriction.
These details prevent treating every implementation as having writable bits or
inferring feature absence merely from observed ones. Setting the requested bits
is compatible with the selected native AArch64 EL1 fixture without changing its
emulated x86 feature envelope. The exact whole-register expected value is a
named-host fixture invariant, not a universal Arm reset/readback guarantee.

The arithmetic is exact: initial 0x30d00980 plus the existing M/C/I enable bits
0x1005 yields 0x30d01985. The proposed change explicitly requests the two bits
and continues to reject every other readback difference; it neither masks an
unknown bit nor changes MAIR/TCR/ACTLR/data-descriptor requirements. Claim 23's
observations support the specific expected host readback but do not identify a
private HVF cause or prove absent AArch32 support. A different native regime or
host readback requires its own decision.

No substantive return. Accept the source-backed narrow configuration for code
preparation/review, not execution or successful handoff qualification. The exact
modified producing inputs and any fresh runtime budget remain separate checks;
all failed runs and their original predicates stay retained.

## Return 20 — finite native handoff primary and independent replication, claim 25

**Independent root acceptance, scoped operational qualification.** Root verified
all 50 retained hashes and seven producing identities of `handoff-reviewed-04`
against preparation 06, independently replayed each exact four-cell trace and
checked complete termination. Machine then independently prepared, rebuilt and
ran `handoff-replication-01` under pre-execution Return 5 and the separate budget.
Root also verified all 50 replica hashes/seven primary-input identities and
replayed every replica trace from retained stdout. Both runs have four zero-exit,
no-timeout cells with empty native stderr and complete teardown. Primary was
10:27:19–10:27:22 UTC; replica 10:31:34–10:31:36 UTC on the recorded M4/macOS host.

Both copies reproduce ISH/copy, ISH/direct, OSH/copy and OSH/direct, each for
64 epochs. Copied cells have 1,478 native exits; direct cells 1,350. Exact words,
epochs, CPU/alias coverage, native sequences, DSB/HVC completion boundaries,
source-model generation checks and operation order meet the reviewed oracle.
Both CPUs' actual boot controls/descriptors satisfy the exact current setup,
including SCTLR 0x30d01985. These counts are diagnostic coverage, not measured
costs. No copied-route host payload read is substituted for the register control.

Accept the finite bidirectional ownership handoff for both named native SH
configurations and both transfer routes under this exact native fixture and its
platform/backing assumptions. This qualifies the operational primitive tested;
it neither identifies private stage-2 descriptors nor guarantees arbitrary host/
OS versions, simultaneous guest/backend access, whole-payload atomicity, MMIO/UC,
vector/split guest semantics, production callback/lifecycle behavior or performance.
The source-generation subcase remains a host coordinator model, not a translated
code invalidation proof. No ISH-versus-OSH winner or production transport is
selected by the identical pass outcomes.

No substantive return for claim 25's finite observation. Failed runs 01–03 and
their original conditions remain preserved. Primary and replication budgets are
exhausted. The remaining architectural work is binding the qualified handoff to
the actual typed backend/source ownership interface and its semantic boundaries;
another identical matrix would not close those questions.

## Return 21 — direct host service-window interface, claim 26

**Independent machine design review, September 20: accept the scoped conditional
interface choice.** Read the complete final claim against claims 18/25, source
capture/continuation premises and machine claim 49's descriptor/broker boundary.
Independently inspected the cited Arm-authored LDRB/STRB unsigned-offset operation
definitions. No new runtime or code conformance is accepted.

Arbitrary bounded byte spans are supported by the proposed exact-span scalar
construction, not inferred solely from the four-word experiment. A byte operation
addresses the required byte; aligned wider operations are permitted only wholly
inside both validated extents. Exclusion composes those accesses for the admitted
unit without inventing payload atomicity or allowing a libc/vector overread.
The platform-handoff premise remains qualified to the tested configuration; a
specific compiled copier still needs to obey the architectural construction.
This supports the interface without imposing four-word restrictions on legal
backend operations. [Arm LDRB](https://finkmartin.com/aarch64/ldrb_imm.html),
[Arm STRB](https://finkmartin.com/aarch64/strb_imm.html).

All resources and completed external input are reserved before closing admission.
The window performs bounded memory work, not network/filesystem waits or decoder
callbacks. Per-participant native drain acknowledgements and permanent service
resources preserve the earlier PAUSED-versus-PARKED distinction; an admitted old
unit may complete before a conflicting remap acknowledges. This avoids making
canonical guest progress or guest locks prerequisites for the service itself.
An implementation with missing drain reachability or an unbounded leaf would
violate an explicit premise, not qualify through the small fixture.

The effect ordering is concrete: exact host access completes before resident
tracking/invalidation and commit; only then can affected guest execution or device
completion proceed. Source copies remain immutable and publication is separately
serialized against writes. Cancellation cannot free code/input/backing while an
old producer, copier or delivery still holds it. Subsequent transfer units resolve
new routes; an old pin is not perpetual authority. Unknown partial effects fail
contained rather than resuming with guessed metadata or replaying a unit.

Descriptor authority and RAM authority remain separate. A disk or transport FD
does not grant guest-RAM access or IRQ publication; the admitted extent view is
an internal trusted-service contract in one worker, not OS-enforced separation
between same-process components. The compiler receives copies, and typed non-WB,
MMIO, fetch and partial-instruction semantics do not become generic bulk copying.
The fallback retains the same effect/lifetime rules rather than restarting an
already completed side effect.

No substantive return. Accept direct bounded service windows as the conditional
baseline for the named ready-RAM operations with copied fallback, not unrestricted
host pointers, complete participant inventory, actual leaf conformance, production
containment, throughput or combined architecture completion. The proposed block-
completion/source-cancellation integration is the appropriate next bounded check;
another identical handoff matrix is unnecessary for this interface argument.

## Return 22 — staged block completion and source cancellation, claim 27

**Independent machine source/protocol review, September 20: source chain and
conditional composition accepted; one narrow driver-scope clarification requested.**
Independently verified all six files against the retained `sources.json` in
`/private/tmp/natemu-track03-virtio-completion-01`, then inspected request capture,
async read submission, completion/error handling, descriptor mapping, RAM/bounce
unmapping, split used-ring publication, suppression/IRQ and reset paths at QEMU
`c1c18d1e640b64292859ce9f30f3c344edfb0294`. No runtime or implementation conformance
is inferred.

The callback-only counterexample is real: submitted writable IOVs can mutate RAM
before completion. Direct status writes and cached ring writes also escape a
payload-only wrapper. Private asynchronous staging plus admitted payload, status,
ring and event accesses addresses these distinct paths. Captured identities and
checked arithmetic replace reusable guest pointers; ordered alias writes remain
writes even when they overwrite another part of the request. Committing the union
of source dependencies before observers resume or IRQ publication closes the
otherwise exposed source-generation interval. The bounded reserved transaction
is a necessary premise; larger requests and other negotiated paths are expressly
outside this trace. [Pinned block component](https://github.com/qemu/qemu/blob/c1c18d1e640b64292859ce9f30f3c344edfb0294/hw/block/virtio-blk.c),
[pinned queue component](https://github.com/qemu/qemu/blob/c1c18d1e640b64292859ce9f30f3c344edfb0294/hw/virtio/virtio.c).

REPORT cannot infer valid payload bytes from the failed callback. Discarding only
private staging and recording zero written prefix avoids inventing either valid
data or guest rollback. Virtio permits underestimation, but its separate driver
rule does not promise consumption beyond that prefix. **R22-1:** qualify the tail
IOERR argument accordingly. Independent inspection of Linux v6.12 shows
`virtblk_done` retrieves `len` without using it to gate completion, while
`virtblk_request_done` obtains the tail status through `virtblk_vbr_status`.
This supports that named driver path, not universal drivers or a tested boot.
[Virtio 1.3 §§2.7.8.2–3 and 5.2.6](https://docs.oasis-open.org/virtio/virtio/v1.3/virtio-v1.3.html),
[Linux v6.12 block driver](https://github.com/torvalds/linux/blob/v6.12/drivers/block/virtio_blk.c#L306).

Reset preserves progress by closing new admission without holding the RAM window
or blocking the only callback consumer. Already admitted units settle before
acknowledgement; canceled ungranted jobs cannot later obtain RAM authority.
Staging, route and callback lifetimes survive cancellation until their users end,
and old IRQ/result identities cannot target a new queue incarnation. This matches
the requirement that successful reset end old queue execution and notification;
it does not assume synchronous upstream drain can be transplanted unchanged.

Source publication and fetch cancellation also compose: stale immutable compiler
results lose publication authority while retaining storage lifetime; repeated
refill transport returns the recorded outcome rather than repeating device
fetches. A real abandoned architectural continuation differs from an internal
coordination stop. Existing fetch-legality checks still govern mixed fragments.
No additional substantive return. Acceptance is limited to this split-ring,
unmerged incoming READ and its stated error/reset/source interactions, not packed
or IN_ORDER coverage, complete backend adaptation, performance or the combined
architecture.

**R22-1 recheck:** the author now explicitly separates the permitted zero-prefix
policy from driver consumption, cites the independently checked Linux v6.12
completion path and retains other-driver/boot limits. Rechecked the exact added
paragraph and feature-selection scope. The correction closes R22-1; claim 27's
source facts and conditional composition are accepted with no outstanding return.
The proposed feature/command selection is unstarted and does not silently require
all optional upstream features. No new runtime or source research in this recheck.

## Return 23 — concrete Linux block profile, claim 28

**Independent machine source/design review, September 20: accept the scoped
conditional profile and negotiated-path mapping.** Verified the four retained
Linux files against `linux-profile-sources.json`, and inspected their feature
list, queue/limit selection, MMIO finalization/reset, split-ring selection,
GET_ID, cache-mode and flush sequencing. Rechecked the reused QEMU feature,
GET_ID and flush branches and relevant Virtio 1.3 requirements. No native run,
boot, completed implementation or durability experiment is accepted.

The selected Linux v6.12 paths support one queue without MQ, direct descriptors
without indirect support, and split rings without packed negotiation. Both
limit bits occur in the modern block feature table; the driver consumes their
configuration values. The 32-by-4096 data limit bounds an admitted request to
128 KiB. The 128-element/16-MiB staging bound is conservative, conditional on
retained slots, parser checks and backpressure; it is not permission to allocate
from unchecked guest lengths or pop unbounded work. Modern descriptor layouts
still require correct header/data parsing. The initial feature exclusions have
explicit compatibility/performance costs and do not require unrelated optional
upstream functionality. [Linux block driver](https://github.com/torvalds/linux/blob/v6.12/drivers/block/virtio_blk.c),
[Linux ring implementation](https://github.com/torvalds/linux/blob/v6.12/drivers/virtio/virtio_ring.c).

Rejecting a driver that declines either limit is a deliberate exception to the
subset recommendation, with failed FEATURES_OK as the specified rejection
mechanism. It is accurately distinguished from a mandatory standard dependency.
The conditional compatibility claim is for the named driver, not arbitrary
Virtio drivers. Crucially, declining FLUSH does not get the same rejection:
the proposed full-sync-before-write-completion branch preserves the stable-write
obligation when FLUSH was offered but not negotiated. This is separate from the
negotiated writeback/explicit-flush branch. [Virtio 1.3 §§2.2.2 and 5.2.6.2](https://docs.oasis-open.org/virtio/virtio/v1.3/virtio-v1.3.html).

IN, immutable OUT snapshots, FLUSH and GET_ID account for the selected command
surface. GET_ID needs its own admitted result path despite lacking asynchronous
disk I/O. Unsupported/reportable errors use tracked status/ring completion;
unreportable corruption cannot proceed through unchecked accesses. OUT failure
retains possible disk effects rather than inventing rollback. Linux's cache
configuration and PREFLUSH/DATA/POSTFLUSH policy support the intended barrier
composition without native FUA, but do not establish physical persistence or
QEMU's use of F_FULLFSYNC. Explicit backend binding remains necessary.
[Linux flush sequencing](https://github.com/torvalds/linux/blob/v6.12/block/blk-flush.c).

The QEMU feature allowlist must override component-added features, as recorded.
Without EVENT_IDX the normal suppression/IRQ contract remains; without RING_RESET
ordinary QueueReady and device reset still remain. Linux clears QueueReady and
reads it back before freeing its queue. Keeping the reset MMIO operation pending
while callbacks drain outside RAM exclusion preserves that release boundary;
returning success before old access obligations end would violate this interface.
The proposed single-owner state machine retains progress and avoids transplanting
a synchronous drain that blocks its own completion consumer.
[Linux MMIO implementation](https://github.com/torvalds/linux/blob/v6.12/drivers/virtio/virtio_mmio.c).

No substantive return. This closes the initial profile's source/design choices
and named command/lifetime mapping conditionally, not image driver availability,
parser conformance, full-sync backend conformance, stock boot, performance or
combined machine architecture acceptance. No further runtime budget follows.

## Return 24 — owned-file backend and blocking-call lifetime, claim 29

**Independent machine source/design review, September 20: accept the conditional
backend binding.** Verified all five retained inputs in
`/private/tmp/natemu-track03-file-backend-01/sources.json`; inspected positioned
read/write entry, retained-file paths, residual/error handling and F_FULLFSYNC
dispatch, and checked the cited Apple API documentation. No storage operation,
new runtime observation or physical durability test was performed.

The proposed per-disk lane owns blocking file work and private buffers while the
device owner remains able to process admission, reset and completions. Reserving
result storage and preserving accepted order across snapshot readiness prevents
a later flush/write from overtaking an earlier admitted OUT. Positive short
returns advance only the confirmed suffix of the same ordered job. The byte
bound makes the count of positive-progress calls finite, without claiming that
any individual filesystem call has a time bound. Zero/nonempty and error returns
settle as errors rather than spinning or automatically replaying effects.

Pinned `fp_readv`/`fp_writev` only suppress the selected interruption/would-block
errors after progress; other errors can survive alongside changed residuals.
The ordinary userspace error result is not an exact final progress report. The
author correctly retains known positive prefixes and unknown additional failed-
call effects, including EIO, instead of inferring rollback. Discarding partial
IN staging has no guest payload effect; failed OUT may affect later reads or
flushes. These are different effect domains.
[XNU positioned I/O and residual handling](https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/bsd/kern/sys_generic.c).

The single lane also provides the selected overlapping-write and flush order.
Negotiated writeback permits data completion before durability; declined FLUSH
requires the recorded full-sync step before successful OUT completion. Explicit
FLUSH goes through the same order after earlier work settles. The actual command
is F_FULLFSYNC with truthful errors, not a substituted fdatasync/close operation.
The kernel dispatches it to filesystem ioctl; neither that dispatch nor the SDK
constant proves a future mount/device honors the media request. The declared
platform/backing limitation and absence of a power-loss proof remain correct.
[XNU full-sync dispatch](https://github.com/apple-oss-distributions/xnu/blob/xnu-12377.1.9/bsd/kern/kern_descrip.c#L3860),
[Apple fcntl documentation](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fcntl.2.html).

Cancellation invalidates future authority, not the lifetime of a running call.
Waiting for call return before buffer/FD reuse, stopping further suffix calls,
suppressing obsolete completion and settling already granted RAM units preserves
the prior reset contract. Reset cannot hold RAM exclusion or the sole completion
consumer while waiting. If the call does not return, contained failure replaces
successful reset; a new incarnation cannot race the old call. Orderly teardown's
last flush, join/drain and final close preserve ownership, while close error or
forced termination cannot be relabeled a successful durability operation.

No substantive return. Accept the regular-file, exclusive-writer deployment
premise, dedicated-lane choice and specified completion/cancellation semantics.
No advisory-lock enforcement against unrelated writers, implementation of all
FD aliases, actual filesystem conformance, throughput or full machine correctness
is established. The existing guest-RAM transaction remains independently required;
this backend does not acquire permission to mutate guest status/rings directly.

## Return 25 — split LOCK CMPXCHG, claim 30

**Independent machine source/protocol review, September 20: accept the scoped
constraints and conditional ordinary-RAM service; own-walk overlap remains open.**
Verified all six retained PDF/text identities in
`/private/tmp/natemu-track03-split-cmpxchg-01/sources.json`. Read Intel Volume 2A
CMPXCHG operation/faults, Volume 3A lock integrity, A/D and page-fault state, and
the explicitly older AMD A/D discussion. No native observation or emitted
implementation is accepted.

Intel specifies writeback in the comparison-failure case. Both pages therefore
need write-intent validation even when a preliminary read would predict mismatch;
software cannot replace this with a read-only access. Locked integrity covers
the complete operand despite misalignment, subject to the stated alignment and
split-lock feature conditions. The service's exclusion of all relevant observers
and drain of prior effects is the load-bearing atomicity construction, not an
assumption that two fragment-sized host atomics compose. Its global exceptional-
access cost remains explicit.
[Intel CMPXCHG](https://cdrdv2-public.intel.com/812383/253666-sdm-vol-2a.pdf),
[Intel lock/paging rules](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf).

Full operand validation before result/store publication preserves the ordinary
fault boundary. Per-fragment STORE helpers cannot be assumed safe preflight when
they already perform D maintenance. RAX/flags are preserved on the fault path;
write-intent #PF data and fault-owner actions remain distinct from the result
of an unsuccessful comparison. Lawful walk A effects are not rolled back and
must pass through source/dependency retirement even if the operand instruction
faults. The text correctly distinguishes explicit operand stores from an alias
changed by a page-walk update, and retains the decoded operation and continuation
storage until their users end.

After successful non-overlap validation, retained routes prevent halfway remap.
A mismatch still writes back the old operand and satisfies the applicable dirty
obligation. Identical bytes do not erase the write access, although a legal dirty
translation cache can avoid a new physical D update. Code or unrelated-PTE
writes commit source/shadow consequences before observer release and continue
through ACCESS_COMPLETED; retrying the native instruction would repeat an
already committed RMW. A post-preflight uncertain native failure cannot be
invented as a guest second-page fault after half an operand store.

The decisive limitation is correctly left unresolved: when the operand overlaps
the very entry updated by its own walk, D-before-read can change compare outcome,
while D-after-store can change the written result. Excluding other writers does
not select a legal order for this self-induced effect. General A/D and locked-
operand statements, and AMD corroboration, do not establish an exact Intel
Haswell outcome. The proposed order is labelled a candidate rather than accepted
by assuming eventual D=1. The guest instruction is not prohibited to conceal
this gap. This limits the present service's accepted applicability; it does not
require another host-byte-transport experiment.

No substantive return. Accept ordinary non-self-overlap permission/fault/commit
composition under its explicit paging, WB, feature and participant premises;
not self-overlapping A/D semantics, all atomics/MMIO, complete profile controls,
generator conformance or combined architecture completion. The remaining source
question is precise enough for a separate bounded architectural resolution.

## Return 26 — root independent review of claim 31

**Accept scoped source inference and geometry correction.** Root inspected the
retained Intel092 §§5.8, 5.10.2.2–3 and 5.10.4.3 and independently checked the
4-KiB offset/index arithmetic. Original0x1ffc fragments cannot reach the low byte
of either own leaf PTE at offsets8/16. Corrected0x1ffffc reaches PTE0 in the
second fragment; its D bit is operand bit38. This is a real generalized case,
not an unresolved D effect for the original address.

The explicit permission to cache translations before an actual access, with A
set first, supports the selected successful-valid A-before-operand construction.
Retained routes must satisfy real translation-cache validity and invalidation
rules, as the claim states. Fault-prefix permissions remain separate; this does
not authorize invalid-entry walks or speculative D updates. Subsequent operand
clearing of A does not require an immediate second A update on the retained
translation. Source/shadow mutation accounting remains mandatory.

No substantive return. Accept narrowing to the required own-D/operand overlap
question; the original ordinary and own-A cases need no added runtime gate.
No conclusion about unique hardware sequencing, all locked instructions or the
remaining D order is accepted.

## Return 27 — root independent acceptance of claim 32's required-D construction

Root inspected retained Intel092§5.8, §§5.10.2.2–3/5.10.4.3 and
§§11.1.2.1–2, and the CMPXCHG write-on-both-outcomes rule already inspected in
claim30. The combination supports the selected serial construction: actual
required locked paging maintenance first, then the indivisible explicit operand
RMW. The specification distinguishes locked A/D maintenance from explicit operand
locking and permits page-table accesses to pass locked instructions. The choice
neither inserts maintenance midway through the operand RMW nor invents speculative
D or an earlier guest write. Full permissions and remaining fault checks precede
this guaranteed-write path. This is a permitted-order inference, not a claim
that every physical implementation produces an identical value.

Independently checked the corrected D-bit38 examples: RAX=V compares against Vd
and fails with RAX=Vd; RAX=Vd succeeds and can store a value clearing D. Held
current routes and separately tracked maintenance/operand writes prevent the
store from retargeting itself. No post-store cleanup rewrite is required by the
cited cache contract; future eager invalidation may cause a subsequent new walk.
The fault path retains its separate A/effect accounting and never enters this
success-only maintenance phase. The assist advisory is corroborative only and
is unnecessary to the acceptance argument.

**Accept the scoped ordinary-RAM split-CMPXCHG choice, including own-D overlap.**
No substantive return. Matching one undocumented hardware microsequence is not
an outstanding architecture obligation. Implementation conformance to pinned
routes, all-observer exclusion, preflight, result/flags and coherence belongs to
planning. Other atomic instructions, non-WB/MMIO, the full CPU profile and total
memory architecture remain separate open obligations; this acceptance does not
silently broaden to them or establish speed.

## Return 28 — root independent acceptance of RAM atomicity allocation, claim 33

Root verified retained Arm DDI0487H.a PDF/text identities and inspected B2.2.1,
B2.2.2, the AT field definition and full-system barrier scope, alongside Intel092
11.1.1–2 and the already inspected exchange/compare-exchange contracts. The
coverage preserves the AVX-dependent aligned128-bit MOV guarantee, which cannot
be weakened to two observable64-bit transfers. Ordinary256-bit vectors do not
thereby acquire a universal256-bit atomicity requirement.

Accept the conditional native/scalar, exclusive-RMW and coordinated-service
allocation for ordinary RAM. Baseline aligned widths and successful exclusive
pairs have distinct guarantees. An unvalidated LDXP result is not accepted as an
atomic read; guest read intent cannot use writeback to gain atomicity. The named
LSE2 paired fast path requires actual captive capability and effective compatible
WB attributes; neither a host feature flag nor the finite handoff implies it.
The explicit fallback retains correctness without establishing economic adequacy
for ordinary128-bit vector traffic.

The family table preserves implicit XCHG locking, bit-string address selection,
carry capture, differing flags and CMPXCHG write intent on compare failure.
Failed exclusive attempts commit no guest result and can fall back finitely;
a successful store changes the recovery phase so pause/reentry cannot repeat it.
Full-system barrier brackets plus compiler ordering are a conservative selected
ordering recipe, not permission to drop them merely because an opcode is atomic.
All relevant observers remain included in exceptional service, and MMIO keeps
its component-owned exactly-once effects rather than inheriting RAM decomposition.

No substantive return. Accept mechanism coverage in this scope; actual emitter
conformance, multi-access instruction semantics, MMIO contracts and the
architecture's economic selection remain open. Qualifying captive LSE2 is a
concrete next platform fact; no per-mnemonic experiment is demanded by this review.

## Return 29 — primary captive LSE2 enumeration, claim35

**Memory independent findings review: accept the scoped primary observation;
independent replication prepared, not executed.** Reviewed the recorded primary
budget and `lse2-api-read-reviewed-01` under `/private/tmp/natemu-track03-raw`.
All29 retained hashes verify; all five producing inputs are byte-identical to
preparation03 and the current worktree, including the unchanged frozen retention
helper. Independently replayed the retained pure classifier/self-check without
Hypervisor calls and inspected the complete raw probe stdout/stderr and lifecycle.

At11:57:38 UTC on September20, both the default configuration and its created
vCPU returned MMFR2=`0x1001001102001011`, with successful API statuses. Independent
decoding of bits35:32 yields1 for each, identifying FEAT_LSE2 in the retained Arm
field definition. Configuration creation/release and VM/vCPU creation/destruction
are complete; worker exit0, empty stderr, no timeout. The single worker used
inherited unlimited CPU without a setter. Its runtime was approximately0.305s.
There was no mapping, guest instruction, feature-register write or vCPU run;
the source and retained command list match the finite reviewed method.

This establishes a successful matching captive API enumeration, not merely a
host feature sysctl. It does not measure concurrent128-bit atomic behavior or
prove effective inner/outer WB attributes: those remain separate prerequisites
of claim33's paired fast path. No universal Apple model/OS assertion, performance
claim or full native-path acceptance follows. No substantive return for this
primary finding; independent replication remains required before load-bearing
observed closure.

**Replication preparation:** current probe, runner, README, entitlement and
retention helper exactly match the primary retained snapshot and preparation03.
`/private/tmp/natemu-track03-raw/lse2-api-read-replication-01` does not exist.
The prepared independent method is one fresh rebuilt invocation of the unchanged
runner with `--name lse2-api-read-replication-01 --execute`, explicit
`NATEMU_RAW_ROOT=/private/tmp/natemu-track03-raw`, one default config/VM/vCPU,
one read per view, zero guest bytes/runs,20-second worker and30-second execution
bounds. No repetition or MRS fallback is proposed. This preparation is not an
execution budget; no native replication was performed during the review.

### Return29 collection addendum — replica factual result

Memory performed the separately budgeted independent fresh rebuild once:
`lse2-api-read-replication-01`,12:00:12 UTC. Both successful MMFR2 reads are
`0x1001001102001011` (AT1); all lifecycle operations complete, worker/runner0,
no timeout/stderr, no guest mappings or execution. Verified29 hashes/five inputs
matching primary. No retries or source changes. Collection is terminal and its
budget consumed; root's independent findings acceptance remains separate.


## Return 30 — independent LSE2 replica acceptance

**Reviewer:** root, independent of memory's replica execution, September20.
**Verdict:** accept claim35's replicated named-host feature enumeration.

Independently verified all29 SHA256SUMS entries in
`/private/tmp/natemu-track03-raw/lse2-api-read-replication-01`, all five source
identities against the primary, fresh retained-source compile/sign commands,
terminal command outcomes and the raw eight-operation lifecycle. Replayed the
retained classifier and its synthetic checks without invoking Hypervisor.
Both configuration and created-vCPU reads succeed with MMFR2
`0x1001001102001011`, AT=1; cleanup succeeds, worker stderr is empty, and no
timeout occurs. The inherited CPU limit is unchanged. The reviewed program
maps no guest memory and executes no guest instructions.

Together with memory Return29's independent primary acceptance, this establishes
replicated captive LSE2 enumeration for M4 Mac16,10/macOS26.6.2/SDK26.4 using
the default configuration. It establishes neither effective WB memory attributes
nor observed128-bit atomicity or speed. No M2 generalization is accepted.
Both execution budgets are consumed; no retry or additional execution is implied.


## Return 31 — effective-attribute source boundary and next evidence route

**Reviewer:** root, independently inspecting memory's claim36, September20.
**Verdict:** accept the scoped source boundary and preparation direction.

Checked the retained xnu-12377.1.9 SPTM pmap constructor and stage2 predicate:
the exposed path sets is_stage2=false and the predicate returns false. The
host COPYBACK mapping result therefore lacks an identified transition into
HVF's actual stage2 descriptor. Checked Arm DDI0487H.a D5.5, including the
FWB=0 qualification and independent inner/outer table: a stage2 NC selection
would defeat an inference from stage1 WB alone. Rechecked the available kernel
collection's file identity: it is x86_64, not applicable ARM mapping evidence.
These facts establish an inference limit, not absence of runtime WB.

A finite concurrent paired-access qualification is a distinct, useful next
preparation direction. It must separately demonstrate load/store interference,
progress and a working torn-pair control. Acceptance here authorizes no native
execution and accepts no unwritten method or anticipated result. Future clean
samples cannot prove descriptor bits, universal atomicity, all mixed-width
interactions or an unqualified production fast path. The experiment's eventual
scope and residual platform premise must be reviewed before selection; an
applicable mapping source/attribute observation remains an alternative route.

No substantive return remains for this scoped claim. The paired fast path
remains provisional, and the fallback's performance cost remains unresolved.


## Return 32 — primary paired-access observation and replication preparation

**Reviewer:** memory, independently collecting root's claim38 primary run,
September20. **Verdict:** accept the finite primary observation; replication
and final operational disposition remain pending. I authored the fixture, so
this review verifies root's independently collected result against the previously
root-reviewed method; it does not replace independent method review.

Verified all48 sealed hashes at
`/private/tmp/natemu-track03-raw/pair-atomicity-reviewed-01`, all seven producer
identities against preparation01 and the current worktree, and each retained
build/sign/worker command's successful terminal status. The guest object bytes
are identical to preparation01. Disassembly instruction bodies are identical;
an initial literal whole-output comparison differed only in the printed absolute
object pathname, not instructions. LDP remains offset0xdc, STP0x174, with the
reviewed split stores and exact HVC resume labels. No producer repair or native
rerun was performed during review.

Replayed the retained pure oracle on all three raw outputs and reran its synthetic
checks. Independently read boot tuples and terminal register exports. The control
reports exactly `(1,~0)`, one read/change/torn and one writer completion. Writer0's
target records8,000,000 stores,4,455,552 reads and171,132 changes with zero torn
pairs; writer1's records8,000,000 stores,3,394,058 reads and142,734 changes with zero
torn pairs. Both target readers finish with lastlow8,000,000 and zero bad-pair
fields. Each CPU/cell has MMFR2=0x1001001102001011, AT1, expected ACTLR/MAIR/TCR/
SCTLR/descriptors, exact terminal HVC resume-PC/ESR, and successful lifecycle.
All native stderr is empty, all workers return0 without timeout, and workers
inherit unlimited CPU without a setter. The observed run fits the separately
recorded three-cell budget; the primary budget is consumed.

This supports joint paired-load/store behavior for the tested named platform,
not separate load/store attribution, hidden effective-WB bits, all memory
interactions, universal atomicity or performance. No substantive factual return
remains for claim38's primary result.

**Replication preparation only:** confirmed all seven producing inputs still
match the reviewed primary snapshot, including the frozen retention helper; the
fresh destination `/private/tmp/natemu-track03-raw/pair-atomicity-replication-01`
is absent. Proposed independent replication uses the same unchanged runner with
explicit `NATEMU_RAW_ROOT`, that fresh name and `--execute`, rebuilding from its
own retained snapshot. It preserves the same three-cell order, oneVM/two vCPUs/
144KiB,8,000,000 stores per target,20seconds per cell and100seconds execution
ceiling, no retry or automatic follow-up. No replication execution occurred;
a separate recorded budget is required before invocation.


## Return 33 — independent paired-access replica acceptance

**Reviewer:** root, independent of memory's replica execution, September20.
**Verdict:** accept claim38's replicated finite joint-path observation.

Independently checked all48 hashes in `pair-atomicity-replication-01`, all seven
input identities against primary, identical retained guest object, fresh build/
execution commands, terminal outcomes and empty worker stderr. Replayed the
retained oracle and its pure mutations against each raw cell; boot tuples,
checkpoint identities, lifecycle and counts qualify. Control reports the exact
forced torn pair. Targets report4,050,013/3,169,443 reads and192,363/107,529
changes, respectively, with8,000,000 stores each and no torn observations.
Both progress thresholds are exceeded. No timeout or retry occurred.

Memory Return32 independently accepted the root primary. The separate fresh
replica therefore satisfies the required replication for these finite sampled
observations on M4 Mac16,10/macOS26.6.2/SDK26.4, uncontrolled placement. Clean
samples establish neither private WB descriptor bits nor universal or mixed-width
atomicity. The observed joint path does not isolate load versus store causes.
No duration is accepted as a performance metric. Any selection using this as
an operational platform premise must retain those limits and assess the intended
access set; the result is not automatic acceptance of every vector lowering.
No substantive return remains for the stated finite observation. Both budgets
are consumed and all earlier source/preparation limits remain recorded.


## Return 34 — named-platform aligned128 mechanism selection

**Reviewer:** root, independent of memory claim39 authorship, September20.
**Verdict:** accept the scoped architectural choice with its explicit operational
platform premise; do not promote it to a documented effective-WB contract.

Rechecked claim33's required aligned128 access set and Arm B2.2.1 paired-GPR
mechanism against the independently accepted feature and primary/replica records.
The proposed path uses the exact tested opcode class, alignment, VM/mapping and
TSO regime. It supplies a read without writeback, retains normal permission and
A/D semantics, and separates the memory event from vector/GPR representation.
The same-page geometry is correct. Fault recovery must retain pre-access state;
the claim correctly avoids assuming both native destinations survive a fault.

The central inference is explicit: single-copy atomicity for other ordinary
coherent peers and placements is broader than the one-reader/one-writer samples.
The matching architectural mechanism, actual captive feature advertisement and
replicated interfering path with detecting control support adopting that property
as a named-platform operational premise. This is an empirical architecture
selection under the project's admitted Apple-capability policy, not deduction
of the private attribute bits. Its remaining risk must accompany synthesis and
portability decisions; a host/OS/mapping change or contrary applicable evidence
reopens the choice. Merely repeating the same sample count would not resolve
that broader inference.

The scope boundary is material: no unaligned LSE2 forms, Q-register opcodes,
256-bit atomicity, mixed-width experimental coverage or M2 parity are accepted.
Native exclusives and x86 ordering retain their separate contracts. Host/backend
windows exclude native peers, so selection does not smuggle in concurrent host
memcpy atomicity. The fallback preserves required behavior for ineligible
accesses, without imposing a coordination round trip on every ordinary vector
access. Register-transfer/spill cost remains unmeasured and generator-specific.

No substantive return remains for this mechanism choice. Full charter question6
and combined architecture acceptance still require their complete instruction/
observer/ordering composition; no bespoke probe per instruction is required.


## Return 35 — question6 composition and local fence completion

**Reviewer:** root, independent of memory claim40 authorship, September20.
**Initial disposition:** return the LFENCE completion argument for correction;
question6 is not accepted by this review yet.

Inspected claim40, claim33’s access/family allocation, claim39’s platform premise,
the existing observer/drain contracts, Intel253668-092US §11.2.5 and its LFENCE
footnote, and ArmDDI0487H.a B2.3 plus Glossary11510–11511. Native fenced locks
and exclusive serviced locks have a coherent insertion argument: each native
participant drains its prefix, service commits under one authority, and release
starts the suffix. Device transactions expressly retain exact effects and route
authority, with endpoint-specific register contracts owned by question11.

The remaining source-to-recipe gap is specific: Intel requires prior instructions
to complete locally before LFENCE, including independent register/FP computation.
The cited DSB contract completes prior memory effects; ISB bars later instruction
functionality and synchronizes context. The submission has not shown that these
clauses alone force every prior live nonmemory result to complete. This is a gap
in the justification, not an observed failure of DSB/ISB on M4.

Smallest remedy: supply an applicable native guarantee or an explicit dependency
completion construction covering live architectural results and deferred-state
dependencies before admitting later work. Do not resurrect eliminated dead
computations or call a conservative spill recipe the minimum adaptation cost.
Keep any extra LFENCE work separate from SFENCE/MFENCE’s actual requirements,
and retain precise state/effect ownership across native interruption. Closure
requires independent recheck of the amended argument, not a timing microbenchmark
or a rerun of the paired-atomicity experiment.

**Recheck and final verdict:** accept amended claim40 as the question6
architectural mechanism/interaction contract under its named platform, semantic
and endpoint premises. The author added a LFENCE-only non-elidable dependency
sink into reserved resident scratch before DSB SY/ISB. Completion of a store
requires its actual data; completion of all relevant sink stores therefore
completes their live producers. Deferred representations remain valid without
forced canonicalization, but their live inputs and FP/status results must be
covered. Prior faults/helpers must have resolved before fence admission. The
construction adds no guest memory access and no global park per fence. The
original pure-barrier justification is superseded, not retroactively accepted.

The review rechecked ordinary/locked/split classes, compare-failure write intent,
validated128-bit exclusives versus ordinary paired loads, TSO versus host
observer ordering, all-participant drains, and exactly-once device phases.
The accepted aligned128 mechanism retains claim39’s operational M4 premise and
its generalization risk. Device widths/errors and instruction-specific partial
effects remain explicit inputs owned by other rows; an incompatible endpoint
reopens the choice rather than being dismissed as implementation work.

No substantive return remains for this scoped question6 resolution. It is not
acceptance of a complete CPU/memory/device architecture, actual emitter or
coordinator conformance, M2 applicability, or performance. Sink/barrier/fallback
costs remain unmeasured; this conservative construction establishes neither
minimum codegen overhead nor a comparative generator preference.


## Return 36 — coordinated miss walks and hidden PAE roots

**Reviewer:** root, independent of memory claim41 authorship, September20.
**Verdict:** accept the two scoped selections; full question4 remains open.

Rechecked claim41 against the existing admission/drain/access-result rules,
INV03-01 claim12, the pinned QEMU mmu_translate legacy-PAE branch, and Intel
253668-092US §§5.4.1–2/5.10.4. The branch actually rereads the root through
ptw_ldq and attempts an A update. Intel’s selected PAE contract instead uses
one of four loaded internal PDPTE registers; present loads reserve bits8:5,
and a table-memory edit alone does not update the loaded value. The proposed
root substitution is therefore concrete and necessary for this Intel contract.
It preserves lower-level semantics separately and does not accuse every QEMU
profile or imply the root change completes an adapted walker.

Moving miss admission ahead of the first paging read removes the baseline’s
unclassified foreign-mutation race. Existing participants can acknowledge PAUSED
in permanent code without first acquiring that same walker authority, so the
choice does not reintroduce the earlier fault/park deadlock. Exact completed
suboperations survive service interruption; no backend wait, reset drain or
reentrant guest execution is permitted while retaining a RAM grant. Own-A/D
alias effects remain real mutations with their accepted ordering and lifetime
rules. SERVICE_ONLY is not promoted into general permission to reuse stale
entries. Native shadow hits remain concurrent.

Cache eviction versus hidden-state reload is correctly distinguished. Tag and
backing reuse require prior-access drain and native maintenance, not just a
changed software generation. The remaining width/reserved-bit and mode/type/
permission contract is explicitly an architectural input, not mislabeled later
implementation testing. No substantive return remains for these two choices.
Miss serialization can materially affect SMP cost; acceptance establishes
neither economic superiority nor an optimal/minimum-cost walk design.


## Return 37 — concrete paging and memory-type contract

**Reviewer:** root, independent of memory claim42 authorship, September20.
**Verdict:** accept the scoped question4 architecture contract with its explicit
CPU, instruction and machine-map dependencies.

Independently checked Intel253668-092US §§5.3–5.9, including legacy reserved-bit
rules, hidden PAE formats, permission/error-code definitions and table/leaf type
selection, plus Tables14-5/14-7/14-11/14-12 and MTRR enable/range rules. The
40-bit guest physical namespace is a virtual CPU choice; it does not require
40 bits of populated RAM or private HVF IPA identity. PSE36’s split40-bit base,
PAE62:40 reservations and four-level51:40 reservations are kept distinct.
Higher ignored fields are not incorrectly rejected wholesale.

The PAT/MTRR matrix matches the primary table, including UC+WC and WP+UC-
producing WC. Disabled-MTRR UC is an explicit ordinary-access override; the
hidden-PAE load separately selects §5.9.2’s WB classification rather than a
fabricated PAT lookup. No-fill remains separate from the underlying type, and
the selected P4-and-later CD/NW behavior matches the table’s note. PAT hardware
reset versus INIT retention is correct. Undefined overlap outcomes are labeled
chosen conservative behavior, not falsely represented as specified hardware UC.

The finite result carries permissions, exact intent/fault information, A/D
progress, routes, type/cache mode and lifetime, addressing the missing upstream
result interface. Shadow extents stop at routing/type boundaries even inside a
large guest leaf. Hidden state is not a disposable translation cache; eager
eviction and tag reuse preserve the previously reviewed access/drain rules.
Instruction-specific address formation, competing faults and partial effects
remain explicit consumers of this contract, not accepted by naming a walker.

No substantive return remains for42’s selected memory/shadow composition with
claims13–19/26/30–33/40–41 and INV03-01 control/lifetime contracts. Actual
walker extraction, native mapping/guard conformance and costs are unestablished.
The CPU profile must advertise these selected fields consistently, and the
machine must define RAM/device holes and backend responses; incompatible later
choices reopen the relevant contract. This is not whole-Track03 acceptance.
