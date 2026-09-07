# INV-01-32 — Source and observed findings review

**Final observed verdict, 2026-09-07 07:47:24 UTC: accept CLM-01-32-08 and
Q-01-32 as a scoped yes** for the finite producer-06 mechanism set on the
recorded M4 at two/four vCPUs, with checkpoint-time SPI setup. All four
required primary pairs and both independent replicas pass full checking;
the original failures remain retained and their internal cause is not
claimed. The exact final disposition follows the earlier source and partial
reviews below.

- **Screening:** (a) QEMU TCG also models guest interrupt eligibility and
  boundaries; same-ISA VMs obtain instruction semantics natively. (b) Documented
  Intel, Arm and Apple mechanisms do not establish their cross-ISA composition.
  (c) This review concerns the thin EL1 shim and its stated translator metadata
  contract, not every emulator or a platform-wide feasibility conclusion.
- **Reviewer and independent context:** `/root/q34_sources`, delegated by
  `/root`; authored no Q32 investigation or experiment code.
- **Material inspected:** complete `INV-01-32.md`, seven proposed claims,
  metadata table, twelve scenario rows, exclusions, growth rule and budgets.
  Independently read the cited Intel revision-092 volumes, Arm guides and
  installed macOS 26.4 SDK declarations. Reused this reviewer's independently
  fetched Vol. 3A/2B and exception-guide copies from the Q31 review; independently
  downloaded Vol. 2A and opened the GIC guide and Apple API documentation.
- **Date and initial verdict:** 2026-09-06 — **revise one fixture assumption**
  below. Remaining source claims and preparation are supported within their
  stated limits. No experiment code was submitted; this is neither execution
  approval nor a runtime finding or Q32 resolution.

## Source and method checks

[Intel Vol. 3A revision 092](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf)
§§7.5–7.9 support boundary recognition and the distinction between IF and
interrupt inhibition. Table 7-2 places eligible maskable interrupts before
next-instruction fetch faults, and expressly excludes faults during execution
or delivery. §§7.12/7.14 support the gate/frame obligations. §§13.8.3–13.8.5
support vector ranking, strictly higher-class preemption, the TPR/ISR-derived
threshold, IRR coalescing and highest-ISR EOI. The proposed 0x41/0x4f/0x71
ordering and TPR=0x50 control agree. Native GIC priority and guest APIC priority
must remain separately modeled. Section 11.1.3 supports the synchronized
modified-code controls; an unsynchronized race cannot demand a particular
new-code successor.

[Intel Vol. 2B](https://cdrdv2-public.intel.com/922481/253667-092-sdm-vol-2b.pdf),
MOV p. 4-29, STI pp. 4-673–4-674 and REP pp. 4-563–4-564 support the stated
shadow termination and iteration progress. Old IF=1 receives no promise of
the old-IF=0 STI delay. The consecutive-SS-load and LSS distinctions agree with
Vol. 3A §7.8.3. A fault in the following instruction must traverse actual
exception delivery, not simply clear a shadow counter. REP interruption retains
instruction RIP and completed-prefix progress; the RAM-only scope avoids
claiming repeated I/O side-effect recovery.

[Intel Vol. 2A](https://cdrdv2-public.intel.com/922480/253666-092-sdm-vol-2a.pdf),
HLT p. 3-439 supports CPL0, per-logical-CPU halt and the post-HLT saved RIP
when an enabled interrupt resumes execution. The masked fixed-interrupt control
does not invent a guest wake. CLI pp. 3-148–3-149 and STI's decision table
require the privilege-fixture clarification below.

The [Arm exception guide v1.3](https://documentation-service.arm.com/static/63a065c41d698c4dc521cb1c)
§§5.1–5.3 supports saved native state, software preservation, ERET and the
asynchronous preferred return address. That Arm address does not independently
identify a committed x86 boundary.
The [GICv3/4 guide v3.0](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/Learn%20the%20Architecture/GICv3_v4_overview.pdf)
§§4.1–4.2, 6.4 and 7 support native interrupt types, pending/active state,
EOImode-dependent deactivation, affinity-list/all-except-self routing and one
pending SGI instance per target/INTID. Section 7.2 explicitly contrasts GICv2's
sender banking. Case 6 therefore correctly preserves payload sequences outside
the notification and does not infer handler count from sender count.

The local SDK identifies itself as macOS 26.4. Hypervisor.framework headers
`hv_gic.h` and `hv_gic_parameters.h` confirm macOS 15 availability, GICv3,
the queried reserved INTIDs/SPI range, and
[`hv_gic_set_spi`](https://developer.apple.com/documentation/hypervisor/hv_gic_set_spi(_:_:))
level/edge behavior. These declarations establish available controls, not a
working host interrupt route. The later snapshot must retain affinity topology,
configured native groups/EOImode and actual API results as already required.

Claims 06–07 are defensible inferences. Mid-operation native interruption needs
pending-state preservation and a bounded route to a legal commit point; recovery
metadata cannot undo visible effects. Halt publication/recheck and code-generation
lifetime are real composition requirements, still unimplemented here. All three
native sources, interior arrival, four recipient states, priority/coalescing,
REP and same-page write/execute appear in the finite method. Actual IRQ ELR
interval evidence and separate oracle/negative controls prevent software enqueue
or a printed expected tuple from masquerading as interrupt delivery.

Exact variants, IDT/TSS frames, delivery-time faults, halt synchronization,
resource limits and oracle code still require concrete pre-execution review.
The stated finite attempts and missing-coverage rule must govern difficult
arrival rendezvous; conveniently timed retries cannot silently replace failures.
Acceptance of preparation would not waive Q30/Q31 readiness or authorize their
mechanisms to be disabled. No timing or cross-generation conclusion follows.

## Substantive blockers

1. **Pin the privilege fixture's virtualization flags.** Case 3 requires CPL3
   CLI/STI with insufficient IOPL to produce #GP. The cited instruction tables
   also have PVI/VIF alternatives; the inherited Q31 feature list does not
   explicitly fix CR4.PVI. An unspecified input can invalidate the expected
   result. Smallest remedy: declare CR4.PVI=0 for these initial fixtures, with
   virtual-8086 mode inactive in their 64-bit scope, and qualify the case's
   #GP oracle accordingly. Closure: inspect that declaration and affected claim
   or case wording; no additional experiment is needed for this correction.

## Editorial edits and limits

Only this review file was authored. No investigation, code, archived repository
history or VM was changed or executed. Primary reading stayed within the
30-minute/12-document source budget. Local PDF/text copies are navigation aids,
not experimental artifacts. No other substantive blocker identified for the
scoped source findings and method preparation.

## Returns

The retained author was sent blocker 1 with the exact instruction references.
Keep this review thread for the correction and updated scoped verdict.

### Return 1 — accepted

Same independent reviewer, 2026-09-06. Inspected the author's new assumption
fixing CR4.PVI=0, CR4.VME=0 and RFLAGS.VM=0, and case 3's matching qualified
#GP oracle. These inputs remove the PVI/VIF alternative from the selected
privilege fixture. **Blocker 1 closed; accept scoped claims 01–07 and method
preparation.** No substantive blocker remains for that source-only scope.
This verdict does not accept experiment code, authorize execution, establish
runtime feasibility or resolve Q32. The Q30/Q31 foundation, concrete fixtures,
independent code review and runtime replication remain required as stated.

## Delivery-time exception refinement — independent source/method acceptance

2026-09-07, `/root/track01_understanding_check`: **accept the scoped source
contracts, explicit inference and diagnostic case-9 method** in the new
“Delivery-time exception source preparation” subsection. This is a separate
independent review context; the source author was
`/root/q31_independent_replication`. I authored no Q32 producer, oracle or
source claim. Review stayed within the assigned 15-minute bound and the two
retained Intel revision-092 volumes. Publisher opens failed; I independently
read the relevant retained primary text at
`/private/tmp/natemu-q31-sources/intel2a.txt` and `intel3a.txt`, without claiming
a new independent download. No VM or archived history was accessed.

Screening remains unchanged: QEMU also models event eligibility/delivery,
ordinary same-ISA VMs supply guest instruction semantics, and Intel contracts
do not establish this cross-ISA shim's composition. The candidate tests its
specific boundary, paging, exception and virtual-APIC obligations.

[Vol. 2A revision 092, INT Operation pp. 3-469 and
3-472–3-474](https://cdrdv2-public.intel.com/922480/253666-092-sdm-vol-2a.pdf)
explicitly applies to external interrupts and specifies non-delivery of the
original event when a nested exception occurs. Its code-descriptor read
precedes both privilege-transfer selection and architectural stack/CS/RIP
assignments. The absent second GDT page therefore provides the required early
fault, without borrowing a late-frame rollback rule. Selector `0x1008` addresses
`V+0x11008` within the inherited GDT limit; the first-page #PF descriptor,
IDT/TSS and IST1 stack remain independently accessible. IRET restores the
saved context; its operation supplies no APIC EOI or external-vector replay.

[Vol. 3A revision 092, §§13.8.4–13.8.5](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf)
places the selected vector in ISR before dispatch to the core and identifies
EOI as the distinct completion operation. Retaining ISR across this failed
entry is therefore a defensible inference for the declared single fixed-edge
arrival, with no EOI, reset or second arrival. Table 7-4 includes INTR in the
benign class; Table 7-5 gives serial handling with a subsequent #PF. That
classification does not establish automatic redelivery of the original IRQ.
Implicit GDT reads are supervisor accesses (§4.2); absence yields #PF(0),
with CR2 at the descriptor address. Selector EXT encoding cannot be added to
the separate page-fault error layout.

The declared frame is consistent with §§7.12.1.3/7.14.2–7.14.5 and the RF
rule expressly shared with IDT delivery in §8.3.2.1: saved boundary RIP,
CS=`0x1b`, flags=`0x10202`, unaligned old RSP and SS=`0x23`, plus error zero,
at IST1 frame base `V+0x9fc0`; live CS/SS/flags are `0x08`/0/`0x2`.
This uses the #PF entry contract, not FRED rollback.

The diagnostic endpoint is an appropriate bounded case-9 result: one completed
#PF entry, zero IRQ-handler entry/EOI/requeue, and retained accepted ISR state.
An explicit fixture reset afterward is not guest recovery. The method does
not claim that returning from #PF services or automatically restarts the IRQ;
recovery software, level-triggered interactions and faults after transfer
remain outside its scope. Its proposed negative controls directly challenge
the wrong alternatives. Other ordinary IRQ cases must still execute handlers,
EOI and IRET.

No substantive source/method return remains. This acceptance does not review
the current producer/oracle code, the enlarged memory/log budget, or execution
readiness. Subsequent code review must establish the actual Q30 descriptor
read, preserved pre-transfer state, #PF frame and acceptance-state endpoint;
a manufactured fault tuple would not satisfy this source contract.

## Observed runtime findings — independent review in progress

`/root/track01_understanding_check`, 2026-09-07, separate bounded window
**07:22:42–07:42:42 UTC**. This reviewer authored no producer/oracle code
and executes no additional VM or full acceptance check. The review uses the
frozen producer 06, oracle 01 and supervisor 01 already accepted in the
pre-execution thread. **Partial disposition: the first corrected two-CPU
pair supports its declared applicable mechanisms; Q32 remains unaccepted
pending all required runs and the parent's final scoped claim.**

For `q32-interrupts-02` and `q32-interrupts-02-check-01`, independently
recomputed all **77/10 complete inventory hashes** and compared all retained
producer, oracle and supervisor sources to the accepted inputs. The full
checker passes with no unsupported entries. The unchanged Q30 checkpoint
contains 354,464 records / 28,992 operations; the Q31 checkpoint contains
758,166 / 54,284; the final Q32 state contains 1,420,227 / 110,252. There are
592 global cases, 1,088 applicable CPU-cases, 96 explicit not-applicable
CPU-cases and 640 actual native-work arrival cases. The two-sender collapsed
case and both four-target broadcasts still require the four-CPU evidence.
Supervisor completion is 47.01 seconds, peak observed group RSS
4,681,072,640 bytes under the accepted limit; these are resource observations,
not timing qualification.

Direct record inspection, independently of the PASS summary, finds these
CPU0/repetition 0 witnesses in that run:

| Mechanism | Retained observation |
| --- | --- |
| Interior XADD (2/0) | Native arrival record 804595 precedes commit 804609; boundary 804617 and entry state retain committed RIP `0x10003554` and old operand result RAX `0x60c000`. Its actual five-slot frame saves ordinary flags `0x202`. |
| Halt after recheck (7/2) | Native ELR is `0x4000193c`, the retained post-WFE label; stage is halt-wait. The guest frame saves post-HLT RIP and ordinary return proceeds. |
| Masked HLT (7/3) | Actual timer arrival remains separate from guest eligibility. CONTROL 14/2 retain IF=0, halted, pending vector and matching host request/ack; no IRQ handler or IRET is invented for this endpoint. |
| Repair/tracking/exception work (8/0–2) | Actual native arrivals have stages 2/3/4. Tracking arrival 1172694 lies in exclusive gate phase 1 owned by CPU0; exception-work arrival precedes actual #PF delivery and later IRQ. |
| Early delivery GDT fault (9/2) | Record 1230190 reports the actual descriptor failure at `0x10011008`. Six frame stores retain error 0, RIP `0x10003500`, CS `0x1b`, flags `0x10202`, old RSP `0x10005ff9`, SS `0x23`, with new RSP `0x10009fc0`. The explicit context endpoint follows, not original IRQ completion. |
| REP interruption (10/0–1) | Progress reaches RCX=4 at the same REP RIP `0x10003570`, with corresponding forward/reverse RSI/RDI. Actual frame stores save `0x10202` / `0x10602`, preserving direction and setting saved RF. Forward execution subsequently traverses the actual page-fault repair/IRET path. |
| Code write while pinned (12/0) | Native arrival 1371409 has stage 6 inside CPU0's exclusive write epoch. IRQ recovery still names pinned generation 97 after mutation; handler writes to the owned page advance later generations. Old pin release precedes actual CPUID epoch 23964 and the fresh generation-102 dispatch. The checker verifies native results 42 then 43 and complete pin retirement. |

The retained corruption controls challenge state continuity, saved RF,
coherent frame loads, duplicate handler effects, native helper/critical
intervals, code lifetime, external HLT observations and applicability. Their
synthetic nature remains explicit. The full raw path additionally replays
native paging and all coherent descriptor/frame/IRET accesses, so those
controls do not substitute for actual runtime evidence.

The original run and readback-only diagnostic remain failures in the
campaign. Their SPI became pending while enabled, correctly grouped and
configured, but did not become active. The only subsequent producer change
moved unchanged SPI initialization to the all-stopped Q31 checkpoint after
all vCPUs had run with final affinities. The corrected first full run succeeds.
This is evidence for this initialization discipline in this fixture; it
does not expose the virtual GIC's internal cause or establish a platform-wide
bug. The ordinary native host/GIC integration issue is distinguishable from
the cross-ISA boundary/frame composition being tested.

Outstanding evidence is concrete and already planned: remaining required
primary pairs, especially four-CPU collapsed/broadcast/rotating-role native
coverage, both independently prepared build-06 runtime replicas, and the
parent's final observed claim/results table. No extra experiment or broader
claim is requested by this partial review.

**Partial pass stopped 07:28:23 UTC**, before the 07:42:42 allowance. A final
host/trace cross-check also confirms that both owning-thread Q31 checkpoint
records precede both SPI-initialized readbacks, and those precede both Q32
`enter_run=3` records. The readable initialized/pre-assert values are the
same as the failed diagnostic, while actual records 760527/760777 now show
host-source INTIDs 32/33 arriving on CPUs 0/1 and being accepted before guest
dispatch. This supports the scoped setup intervention; the internal routing
cause remains an inference. No final observed verdict is issued in this pass.

## Final observed findings acceptance

`/root/track01_understanding_check`, **07:40:59–07:47:24 UTC**, within the
assigned fifteen-minute final review: **accept CLM-01-32-08 as written in
the proposed observed-resolution section, and resolve Q-01-32 as a scoped
yes**. The reviewer authored no producer/oracle/supervisor code and ran no
additional VM or full acceptance check. The prior code/method review, all
retained results, independent replication, direct native witnesses and
final claim support this disposition; no substantive return remains.

All six successful raw/check pairs have independently recomputed complete
**77/10-entry inventories** and matching accepted producer 06, oracle 01
and supervisor 01 source files. Every corrected primary's ten compiled
artifacts equal build 06; each replica's corresponding artifacts equal its
independently accepted preparation 02. The full Q30, Q31 and Q32 results are
PASS with unsupported `[]` throughout. The investigator's cumulative
checkpoint/result tables match the actual reports, including these finals:

| Complete pair | vCPUs | Final records | Final operations |
| --- | ---: | ---: | ---: |
| `q32-interrupts-02` / `-check-01` | 2 | 1,420,227 | 110,252 |
| `q32-interrupts-03` / `-check-01` | 2 | 1,420,360 | 110,332 |
| `q32-interrupts-04` / `-check-01` | 2 | 1,419,410 | 110,192 |
| `q32-interrupts-05` / `-check-01` | 4 | 2,969,653 | 226,532 |
| `q32-interrupts-replica-01` / `-check-01` | 2 | 1,420,012 | 110,292 |
| `q32-interrupts-replica-02` / `-check-01` | 4 | 2,969,906 | 226,672 |

Every pair includes the same running VM's 864 Q30 scenarios, 544 Q31
scenarios and 592 Q32 global scenarios, retaining the native MMU, coherent
paging, precise-fault, timer/SGI/vector, TSO and code-generation mechanisms.
The two-CPU scope has 1,088 applicable CPU-cases and 96 declared non-
applicabilities. Both four-CPU runs cover all 2,368 CPU-cases with none
excluded. Native-work arrival counts are 640/1,472 at two/four CPUs; per-CPU
native-work/guest-handler totals are respectively 320/544 and 368/592.
These are distinct counters, not interchangeable notifications or evidence
that every scenario contains exactly one handler.

In addition to the earlier direct frame, REP, halt and code-lifetime
inspection, the reviewer scanned the actual primary-05 and replica-02
records for all four-CPU collapsed and broadcast cases. In **each** run:

- All 64 collapsed CPU-cases retain both required sender completions before
  unmasking, then one native acknowledgement carrying pending work accepts
  both mailbox sequences. The second acceptance sees the vector already
  pending. This does not require the total raw/empty native IRQ count to be
  one. Primary CPU0/repetition 0 records 2245658–2245661 and replica records
  2246693–2246696 are concrete examples.
- Both routing modes cover all 128 broadcast CPU-cases. Each route's send
  marker precedes native arrivals on all four targets, and all four arrivals
  precede the first guest dispatch. The target-list example uses word
  `0x0200000e` plus explicit self `0x02000001`; all-except-self uses IRM
  plus the same explicit self operation.
- All 32 rotated tracking targets receive their native notification while
  their own exclusive paging gate is in phase 1. The other target roles
  retain stages 0/1/5 for leader, IF-clear and halt-wait. The IF-clear target
  subsequently executes actual STI then NOP before guest dispatch. Thus the
  native arrival overlap and delayed guest eligibility are both observed;
  this does not prove simultaneous placement on four physical cores.
- All 64 declared level-SPI quiet intervals retain an unchanged raw host-IRQ
  count over the specified two timer increments after deassertion. The
  claim remains about that measured finite interval, not an unbounded
  absence of future interrupts.

The original run and readback diagnostic also have independently recomputed
77-entry inventories and source equality to their original builds 02/04;
both remain failed outcomes. The accepted correction moves unchanged SPI
setup to the existing all-stopped Q31 checkpoint. All six complete pairs use
that discipline. Together with the diagnostic's correct readable register
values but pending/inactive SPI, this supports the scoped initialization
choice without asserting an undocumented virtual-GIC cause or unrestricted
Apple-platform defect. The failures consumed real campaign slots and were
not treated as successful samples or erased.

Both complete campaign inventories were independently verified. The primary
inventory hash is
`91c2b05bc02b2bf618c26d815af86d56ea79635535599dcded30540ca55dd4cd`;
the independent replication inventory hash is
`9a1070f9f53669192dfd2816bc7336da392f00f1a1de7b5895e25e5db33e19a2`.
The primary used exactly six VMs including the two failures and four full
checks, finishing at 07:31:28 UTC within its original campaign. Independent
replication used exactly two VMs/two checks from its separately rebuilt
preparation, finishing 07:37:30 UTC within 07:31:47–09:11:47, without failure,
retry or post-hoc exclusion. All admission, output, wall and sampled RSS
limits were respected. The largest sampled checker RSS was 5,050,957,824
bytes; resource durations remain diagnostics rather than performance data.

The retained `sysctl`, `sw_vers`, SDK and compiler outputs establish
Mac16,10 / Apple M4, macOS 26.6.2 build 25G83, SDK 26.4 and clang 21.0.0. The
claim correctly concerns handwritten arm64 blocks meeting the metadata
contract, the finite masks/priority/boundary/frame/IRET/REP/code cases and
the explicit EOI fixture operation. It makes no full ISA/APIC, translator,
stock Linux, controlled placement, performance or cross-generation claim.
Masked HLT's host acknowledgement terminates its held diagnostic context;
the early GDT failure retains accepted ISR state at its endpoint without
claiming original IRQ service. Existing broader-scope owners remain as
recorded; this review creates no additional backlog or IDs.


## After-WFE return closure — September 7, 21:05 UTC

Screening (a): native arrival placement and complete guest service are shared
VM/emulator observation obligations. Screening (b): documented native controls
supply the interrupt; no documented or observed timer-placement guarantee is
inferred. Screening (c): this return concerns the finite Q32 after-WFE fixture
inside the growing Q33 composition on M4 at two and four vCPUs.

Independent reviewer `/root/findings_review`, with no producer/oracle/runtime
authorship, inspected Q33's existing diagnosis, exact-input and final-primary
reviews, retained producer 05 `halt_case`, oracle 04 `halt_trial_semantics`, and
both refreshed replica runs/checks. Full inventory, identity, resource and
native-link/copy review is recorded in the final entry of
[Q33's findings thread](INV-01-33-findings.md). It verified 86 raw/11 check files
per replica, every 21 runtime source/12 build outputs against replica-build 03,
and exact oracle 04/supervisor 01. No VM or full oracle was executed by this
reviewer, and no load-bearing code was edited.

The original contradiction is retained: Q33 run 04's native timer arrived at
WFE itself, before the required after-WFE return PC. The correction keeps the
exact PC requirement and services every early guest interrupt through actual
frame/handler/EOI/IRET before an encoded EB FD retry and fresh HLT. The eighth
early trial is fully serviced and then fails; the diagnostic exhaustion path
cannot yield raw acceptance. Source review and independently forced synthetic
controls cover retry ordering, canonical restoration, actual opcode bytes,
trial accounting and the cap. This is a bounded fixture with retained failures,
not a hidden retry-until-success sample rule.

The final primaries 05/06 and independent replicas 01/02 all pass the complete
Q32 checkpoint. Independent direct scan of replica raw records confirms
all 32/64 group 7/variant 2 CPU-cases contain physical INTID30, stage 5, native
ELR `_q32_wait_after=0x400019e8`, matching trial 1, then actual dispatch, EOI
and IRET. The two-CPU checkpoint has 110,332 operations/1,420,773 records;
the four-CPU checkpoint has 226,592/2,968,930. Both preserve earlier Q30/Q31
mechanisms and all accepted finite applicability requirements. No return PC
was broadened and no failed attempt was removed.

Primary 05's retained native early trial followed by complete service and a
qualifying second trial is one observed recovery, previously inspected directly
by the independent primary reviewer. That timing event was not repeated in
the new replicas. It is not used to claim a reliability rate, guaranteed
placement, native cap exhaustion or arbitrary schedule coverage. The scoped
feasibility conclusion rests on reproduced mandatory coverage and the
independently reviewed source/forced synthetic retry semantics, consistent
with the pre-execution review's explicit claim boundary.

**ACCEPT reclosure of CLM-01-32-08's affected coverage and restore Q-01-32's
scoped yes.** Original source findings and other finite interrupt findings
remain accepted. No substantive Q32 blocker remains; this does not accept a
stock guest, general APIC/ISA implementation, WFI-to-host behavior, qualified
timing, or an additional host. Q34 and the final integrated findings/breadth
review must retain the corrected mechanism. The reviewer updated the owning
investigation's stale current summary and added the correction disposition;
all original positive and negative results remain recorded.
