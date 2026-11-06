# INV-01-31 — Source and observed findings review

**Current verdict: accept scoped observed claim CLM-01-31-11 and Q31 feasibility
resolution, 2026-09-07.** The final independent runtime verdict below includes
the four primaries and separate 2/4-vCPU replication; earlier review stages
remain preserved.

- **Screening:** (a) QEMU TCG also needs guest-state recovery; an ordinary
  same-ISA VM has native guest instruction semantics. Cross-ISA recovery plus
  shadow classification is the design-specific issue. (b) Documented Intel and
  Arm contracts establish obligations, not their composition in this shim.
  (c) The claims concern the thin EL1 shim with a translator honoring the stated
  metadata contract, not every translator or emulator family.
- **Reviewer and independent context:** `/root/q34_sources`, independently
  delegated by `/root`; authored no Q31 investigation, fixture, or code.
- **Material inspected:** complete `INV-01-31.md`, claims `CLM-01-31-01`–`07`,
  the recovery metadata table, all 12 proposed scenario rows, feature exclusions,
  growth rule, stopping/replication budgets, and unresolved code prerequisites.
  Independently fetched Intel Vol. 3A/2B revision 092 and both Arm PDFs from
  the cited publisher URLs, then extracted and read their relevant text.
- **Date and verdict:** 2026-09-06 — **accept the scoped source findings and
  method preparation**. The source obligations and explicit inferences are
  supported; the method is a viable preparation for later code review.
  This is not experimental execution approval, a runtime result, or closure
  of Q31. No Q31 code or successful Q30 foundation was available for this review.

## Source and adversarial checks

The [Intel manual index](https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html)
identifies downloadable revision 092. Independently checked
[Vol. 3A](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf)
§§7.5–7.6, §§7.14.2–7.14.3, §7.15 Events 8 and 14, §5.7/Figure 5-12,
§§5.6/5.8, §5.10.4.4, and §§11.1.1/11.1.3. Chapter numbering in the
submission matches this revision; the conventional older chapter-6 exception
references would not be correct here.

Claims 01–02 correctly separate fault restart from traps/aborts, use guest
CR2/access context rather than Arm syndrome positions, identify all listed #PF
bits, and retain the SMEP-or-PAE/NXE condition for instruction-fetch reporting.
For the stated fixtures the proposed codes 0, 6, 7, 5, 3, 9, and 17 are
consistent with the declared access type and feature conditions. Before code
execution, fixture inputs must make the assumed mappings, privilege, reserved
address bit relative to exposed physical-address width, and disabled alternative
fault causes concrete; the preparation already requires this expansion.

The frame/IRET requirement is substantive: 64-bit delivery stores SS/RSP,
RFLAGS, CS/RIP and the relevant error code, with stack alignment and privilege
transition rules. Writing a diagnostic tuple would not satisfy delivery.
Event 14 explicitly permits CR2 overwrite by a second #PF even when it occurs
during delivery and leads to #DF. Event 8 Table 7-5 distinguishes delivery-time
fault combinations from faults in an already running handler. The submission
correctly leaves exact IDT/TSS/stack/DF fixtures as a required pre-code-review
step instead of treating a recursion flag as implementation.

[Vol. 2B `REP`, pp. 4-563–4-564](https://cdrdv2-public.intel.com/922481/253667-092-sdm-vol-2b.pdf)
supports claim 03's per-iteration count/address progress and restart RIP, and
the REP INS side-effect warning. Zero count, reverse direction and a fault
after a nonzero prefix are meaningful controls. The ordinary eight-byte MOV
fixture must not be broadened to x87/SSE stores larger than a quadword:
Vol. 3A §11.1.1 and §5.10.4.4 explicitly permit partial visibility for those
operations, including delayed-invalidation cases. The submission's vector,
MMIO and instruction-specific exclusions preserve this distinction. Failure
atomicity is also distinct from atomic visibility to another CPU during a
successful unaligned store.

The [Arm exception guide v1.3](https://documentation-service.arm.com/static/63a065c41d698c4dc521cb1c)
supports claim 04's exception-class context, software preservation, ELR/SPSR
and ERET points for the selected synchronous faults. The
[Arm known-issues document M.a issue 03, C23292](https://documentation-service.arm.com/static/69aac74fe79f9a1d642aa91f)
is specifically a correction to ESR_EL2 ISV reporting and contains
instruction/fault/feature restrictions; it is not a universal ESR_EL1 operand
decoder contract. The investigation uses it conservatively to reject reliance
on complete decoded operand information, and does not assert identical ISV
conditions at every EL. Unexpected/external abort recovery remains excluded.

Claims 05–06 are supported as inferences: PC-to-RIP mapping alone loses live
state and partial progress; metadata cannot reverse externally visible effects;
validation without coherence through commit has a remap race. Guest fault,
shadow repair and code/table tracking require separate outcomes and counters.
A retry without repair is a new faulting attempt, not a duplicate to suppress.
The proposed immutable generation and retained old metadata address overlapping
fault/reclamation hazards, while the simultaneous-fault cases challenge shared
CR2/frame contamination. None of these inferences proves a chosen mechanism.

Claim 07 matches §11.1.3: local modification needs a specified jump or
serialization sequence; cross-modification needs publication followed by
serialization on the executing CPU. The source does not permit an exact-new-code
oracle for an unsynchronized race. Byte/generation-selected handwritten
successors, side exit and alias cases test a real correctness obligation;
phase-selected successors would not. Keeping this separate from Q33 host code
publication is appropriate.

## Substantive blockers

None for acceptance of the submitted scoped source findings and method
preparation. Missing Q30 readiness, exact variant enumeration, concrete nested
delivery fixtures, experiment/oracle code and independent execution review are
explicit prerequisites for the next phase; this review does not waive them.
No source claim is accepted as an observed implementation result. The finite
cases do not prove the full ISA, any floating-point implementation, stock Linux
boot, timing, or applicability beyond subsequently measured hosts.

## Editorial edits and limits

No investigation or experiment files edited. The phrase “proposed code
submission” in Method means method preparation; no code was actually submitted.
This wording does not change the explicit execution prohibition. Local review
copies `/tmp/natemu-q31-review{3a,2b,-arm,-issues}.pdf` and derived `.txt` files
are navigation aids; primary publisher references above own the support.
No archived Track 01 history, VM runtime, or live guest was accessed.

## Returns

No return required for these source findings. Keep this reviewer/thread for
source corrections; experimental code requires its own pre-execution review.

## Case 9 expansion: independent source and method review

- **Reviewer/context:** `/root/q34_sources`, retained independent Q31 source
  reviewer; the addition was authored by `/root/retained_runner`. I did not
  author its claims or fixtures. My earlier foundation-oracle and independent
  paging-replication work is separate from this source review.
- **Date/material:** 2026-09-07; claims `CLM-01-31-08`–`10`, the fixed
  IDT/GDT/TSS/stack state, eleven variants 9a–9i, negative controls and limits.
  Read the current workflow and charter, then the relevant retained Intel SDM
  revision 092 Vol. 3A and 2A PDF text under `/tmp/natemu-q31-sources`.
  Bounded to 20 minutes and those two primary volumes. Publisher web opens
  failed, so this pass uses the retained primary documents; no new source
  version or independently downloaded copy is claimed.
- **Verdict:** **accept the scoped source contracts in claims 08–10; return
  the exact recovery oracle for variant 9h**. Earlier acceptance of claims
  01–07 is unchanged. This is neither code/execution approval nor a runtime
  finding or resolution of Q31.

### Verified contracts and cases

[Intel Vol. 3A revision 092](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf)
§§7.14.1–7.14.5 and Figure 7-9 support the 16-byte gates, 64-bit target code,
eight-byte zero-extended frame stores, unconditional saved SS:RSP, alignment,
RSP0/IST selection and NULL live SS on a stack switch. The three stated
error-frame addresses follow from aligning the selected pointer down and
subtracting 48. The old unaligned RSP must be saved unchanged. IST selection
applies without a CPL change; ordinary CPL0/IST0 retains its SS. These rules
support 9a–9c and the successful initial delivery in 9i.

Vol. 3A §7.12.1.3 and §8.3.2.1 support the distinction between saved and live
flags: the latter section expressly identifies the fault RF rule as shared with
IDT delivery. For the fixed inputs, saved `0x10302`, interrupt live `0x2` and
trap live `0x202` are consistent. This does not import FRED delivery semantics.
The flag-only variants stop before a TF-enabled resumed instruction, avoiding
an undeclared single-step-handler requirement.

[Intel Vol. 2A revision 092](https://cdrdv2-public.intel.com/922480/253666-092-sdm-vol-2a.pdf),
IRET operation and 64-bit exception list, pp. 3-493–3-498, independently resolve
the malformed NULL-SS sentence in Vol. 3A: the nested CPL0 return may restore
NULL SS=0; a CPL3 return needs the valid user SS specified here. Same-CPL
IRETQ restores SS:RSP, and the handler must first remove the error slot.
The saved RF/TF and IF restoration assertions hold for a CPL0 handler with
the declared IOPL. Case 9i deliberately faults in an executed handler data
instruction, so it is a new #PF with that instruction's RIP, not a delivery
failure. Saving CR2 in software before the nested fault is required by Vol. 3A
Event 14, including its CR2-overwrite footnote.

Vol. 3A §7.13 and Vol. 2A INT operation, pp. 3-469–3-472, support both 9g
intermediate errors: IDT vector 14 uses `(14 << 3) | 2 | 1 = 0x73`, despite
the gate itself occupying 16 bytes; GDT selector `0x08` with EXT=1 gives
`0x09`. The original hardware #PF sets EXT for the nested descriptor error.
These are retained intermediate contexts; the delivered #DF has error zero.
They must not be exposed as a successfully delivered #NP frame.

Vol. 3A Event 8 Tables 7-4/7-5 support the ordered classification in claim 10:
#PF followed by #PF or contributory fault becomes #DF; contributory followed
by #PF is serial. The undefined #DF saved state and no-restart limitation are
correctly retained in 9f/9g. The good independent #DF selector/IST prevents the
chosen defect from also sabotaging its handler. Deliberate failed-#DF shutdown
and handler-prefetch edge cases remain outside this finite set.

The TSS fields/limits and implicit supervisor descriptor accesses agree with
Vol. 3A §10.7/Figure 10-11 and §4.2. Frame/handler/return accesses still require
Q30 translation; using a host pointer would bypass the condition being tested.
The per-vCPU layout, eleven variants, explicit flags, execution-phase records,
repeat-attempt identity, and proposed corruptions are otherwise sufficient
preparation for a concrete code submission. They do not establish a complete
event-delivery model or stock-kernel behavior.

### Return 1: do not infer a stack-delivery recovery frame from serial classification

**Consequence:** variant 9h currently requires an exact IRET-to-original-CPL3
retry after #PF during the first #GP frame push, without an independently
established saved CPL/SS:RSP/CS:RIP context for that intermediate phase. A
checker could accept an invented rollback policy as architectural behavior.

**Evidence:** Table 7-5 establishes the ordered exception classification, not
the recovery frame. Vol. 2A INT operation pp. 3-474–3-475 assigns the new
RSP/SS and CS:RIP before the frame pushes in its inter-privilege sequence.
That pseudocode alone cannot be treated as either an externally visible
partial-state contract or a proof of complete rollback. The inspected IDT
sections do not explicitly settle that ambiguity. Vol. 3A §8.3.4 does specify
undoing register changes for **FRED** delivery, but this fixture disables FRED.
Generic instruction-fault restart wording in §§7.5–7.6 does not independently
pin this delivery-internal saved-state oracle.

**Smallest remedy/closure:** supply direct IDT support for the precise recovery
state, or move this serial-pair control's second fault to an explicitly earlier
descriptor read before architectural state changes, with a concrete independent
frame/return oracle. Alternatively retain only its sourced classification and
mark exact stack-delivery recovery as an unresolved prerequisite before code
execution; do not count that deferred recovery assertion as covered. The other
ten fixed variants and accepted source contracts do not depend on this return.

No investigation, experiment code, VM or archived history was changed/read in
this review. Only this retained review thread was appended.

### Return 1 closure: early descriptor-read variant accepted

2026-09-07, `/root/q34_sources`: independently reviewed `/root`'s replacement
of 9h and its accompanying layout/limit and scope changes. **Return 1 is
closed; accept the revised eleven-variant source/method preparation.** The
parent authored this correction; I made no load-bearing investigation change.

Re-read Vol. 2A INT operation pp. 3-472–3-475 and Vol. 3A §4.2. Selector
`0x1008` references an eight-byte code descriptor at GDT base plus `0x1008`;
with base `V+0x10000`, its address is `V+0x11008`. Its final byte at GDT offset
`0x100f` is inside limit `0x103f`. Making only the second GDT page absent
therefore produces the proposed implicit supervisor-read #PF(0), rather than
a selector-limit #GP, while the first-page #PF CS, original user CS/SS and
other required descriptors remain accessible.

The descriptor read precedes the target type/presence checks and dispatch to
the inter-privilege transfer path. The assignments to RSP/SS and CS:RIP occur
later in that path. Thus this selected failure needs no assumption about
rollback of an already constructed frame. The original CPL3 state remains
the input to serial #PF delivery; IST1 produces the stated `V+0x9fc0` frame,
saving the original RIP, CS=`0x1b`, SS=`0x23` and unaligned RSP. After coherent
mapping repair, IRETQ restores that context. Re-executing the noncanonical
data instruction creates another #GP attempt; its now-readable target
descriptor selects CS=`0x1008`. An automatically queued original handler
entry is not part of this oracle.

The relocation stays inside each disjoint per-vCPU region. It leaves the
other ten variants, particularly 9f's #PF-stack-failure/#DF control, intact.
Exact recovery after a later #GP-delivery stack fault is explicitly retained
under Q-02-01; this review neither proves nor refutes that untested behavior.
No substantive blocker remains in this bounded source/method addition.
Concrete instruction bytes, delivery/access code, independent oracle,
integration limits and pre-execution review are still required. This closure
does not review the newly appended integration seam, authorize a VM run, or
accept any observed Q31 result.

## Observed findings review — primary evidence inspected, replication pending

2026-09-07, `/root/track01_understanding_check`: a separate independent
review context, continuing this agent's Q31 pre-execution review. I authored
no producer, oracle, supervisor or investigation claims. This pass inspects
proposed `CLM-01-31-11`, its primary table, the accepted frozen build 03 /
oracle 01 / supervisor 01, and retained primary runs 01–04. The source
screening at the start of this thread remains applicable: native same-ISA
semantics and cross-ISA reconstruction are different obligations; documented
ISA contracts do not alone demonstrate the composed shim. **Final findings
acceptance is pending independent runtime replication.**

Independently rehashed complete primary and checker inventories, checked each
checker binding to its VM inventory, and compared each run's source directory
to accepted build 03. All four retained complete-check reports pass with empty
unsupported sets. The submitted checkpoint/final operation and record counts
match those reports. Separate read-only raw-event inspection bound all Q31
operations to their metadata and recovered every 544-case set and per-CPU
counter; it was not an additional full checker or VM invocation. Full paging
and intermediate physical-memory validation remain supplied by the retained
independently authored replay, not by treating a cached operation's diagnostic
GPA field as a complete walk result.

Concrete raw witnesses in `q31-faults-04/ram.bin` include:

- Group 6 variant 2 repetition 0: CPU 0 split operation records
  1,006,469–1,006,555 overlap CPU 3's native-trapped revocation at
  1,006,406–1,006,480 of CPU 0's second-page mapping. The split itself has no
  native fault, as permitted by its prevalidation protocol. Group 8 has two
  native-faulting guest operations overlapping at CPU 0 records
  1,193,222–1,193,238 and CPU 1 records 1,193,224–1,193,280. Each of the three
  two-CPU primaries also contains both required overlap types. This establishes
  observed operation intervals, not uncontrolled hardware instruction overlap
  or an exhaustive scheduling proof.
- REP group 7/0/0 CPU 0 retains RIP `0x10003000` after four completed bytes,
  RCX=4 and the next source/destination addresses at the page fault. IRET
  restores that progress; a new attempt transfers the remaining four bytes,
  then commits RIP+2 and clears retirement RF. Zero-count and reverse variants
  remain covered by the full finite checker.
- Case 9h CPU 0 preserves original CPL3 CS/SS and unaligned RSP across the
  early descriptor-read #PF at `0x10011008`. Actual frame writes at
  `0x10009fc0` contain error 0, RIP `0x10003000`, CS `0x1b`, flags `0x10202`,
  RSP `0x10005ff9`, SS `0x23`; corresponding IRET reads recover that frame.
  A new noncanonical-load attempt then delivers #GP through CS `0x1008`.
  The nested handler case separately shows a second frame and inner/outer
  returns. No exact later stack-delivery rollback or undefined #DF saved
  program state is inferred from these observations.
- Remote code case 11/0/0 pins generation 33, then CPU 0's tracked write
  publishes generation 34 at record 1,522,707. CPU 1 delivers/restarts its
  old-generation load and performs a real new load attempt before releasing
  the old pin at 1,523,428. CPUID follows, and fresh dispatch uses generation
  34. The stale-pin control records actual old-generation rejection, fresh
  pin acceptance and zero remaining users. All final code roles/users retire.

The reviewed negative controls challenge forged restart/frame state, exposed
partial split stores, stale metadata, missing generation/CPUID quiescence and
wrong-target concurrency. These supplement raw success; they do not establish
a general translator, full ISA/feature coverage, Linux boot, performance,
MMIO/vector effects or other Apple Silicon generations. The proposed claim
and primary table are supported within their stated scope. No substantive
primary finding return is open at this stage; final acceptance awaits the
replica evidence and final submitted claim.

## Final independent observed verdict — accepted

2026-09-07, `/root/track01_understanding_check`: **accept scoped observed
claim `CLM-01-31-11` and the resulting Q-01-31 feasibility resolution**.
Reviewed the final proposed primary claim/table, its clarified #DF exclusion,
and `/root/q31_independent_replication`'s observed subsection, in addition to
the producer/oracle review and concrete primary witnesses above. Neither
this reviewer nor the replication agent authored the load-bearing producer
or oracle. No substantive finding return remains open.

Both replicas independently reproduce the finite result with the accepted
source and checker inputs. I rehashed each complete producer/checker
inventory (70 and nine entries respectively), checked source equality to
build 03 and checker-to-VM inventory bindings, and inspected their actual
raw metadata, counters and overlap witnesses. Replica 01 has 757,657 final
records / 54,264 total operations; replica 02 has 1,574,615 / 109,992. Their
retained complete checks both pass with empty unsupported sets. Every vCPU
completes the 544 scenarios and 592 commits with advancing native timers.
The reported checkpoint counts, CPU delivery counts and scope match the
artifacts.

For example, replica 02's group-6 CPU-0 split interval
1,007,035–1,007,087 overlaps CPU 3's correctly targeted, native-trapped
revocation at 1,007,043–1,007,115. Its group-8 CPU-0/CPU-1 native-faulting
operations overlap at 1,190,886–1,190,905 and 1,190,877–1,190,889. Replica 01
also contains both required overlap types. These observations independently
exclude a serial-only successful fixture as the explanation of the result;
they remain finite operation-window evidence, not all possible schedules.

The primary campaign retained four runs/four checks, with no diagnostic,
failed, excluded or repeated VM. Replication retained exactly two runs/two
checks in 2/4-vCPU order, admitted with the required time/disk reserves and
completed at 05:50:09 UTC before its fixed 07:25:25 deadline. The preparation
and pre-activation tooling mistakes remain separately diagnosed and did not
run a VM or change accepted code. I invoked no additional VM or complete
checker during findings review.

The evidence demonstrates that the composed paging, precise-state recovery,
finite exception delivery/restart and code-metadata lifetime mechanism can
work in this thin EL1 shim on the tested M4 at two and four vCPUs. The
independently checked initial-to-final memory/protocol replay, actual frame
accesses, snapshot semantics, adversarial controls and separate execution
replication support that feasibility decision; compilation or hardware fault
precision alone would not. The recorded metadata contract remains an
obligation for an eventual translator.

Acceptance does not extend to arbitrary translated instructions, unmodeled
feature combinations, vector/MMIO effects, later delivery-stack rollback,
undefined #DF saved program state, general unsynchronized code races,
stock-kernel boot, performance, other Apple Silicon generations or completion
of the remaining Track 01 questions. The investigation's existing downstream
owners retain those limits. No source finding is promoted beyond its stated
contract, and no unsynchronized exact-new-code outcome is claimed.
