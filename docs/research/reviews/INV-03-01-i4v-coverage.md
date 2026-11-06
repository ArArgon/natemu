# I4-V contract-to-test matrix — final reproduced coverage

**Verdict: the declared bounded I4-V pass is supported by independently
reproduced evidence. No required implementation or validation cell in that
declared pass remains pending.** Prepared October 26, 2026 by
`/root/i4v_contract_review` against the
[implementation plan](../../implementation.md#post-i4-integrated-validation),
[current investigation](../investigations/INV-03-01.md#current-direction--build-first-october-25-2026)
and [independent findings](INV-03-01-findings.md). The owning investigation
retains current results and assignments; this document does not supersede it.
The verdict concerns the finite pre-boot pass, not the complete emulator or a
universal architectural proof. The exclusions below remain later work.

The exact final union preserves **all 688 prior distinct healthy native cases**
and adds **72 healthy cases plus 25 intentional-failure containment cells**.
Five fresh bundles execute 986 native VMs: 760 distinct healthy cases, 25
intentional failures, 176 repeated baseline executions and 25 fresh healthy
controls already represented in the healthy matrix. A sixth fresh bundle passes
the **87 ordered legacy jobs** separately. All six rebuild the same runtime.

Failed developmental runs remain retained and excluded from accepted counts.
Their compiler/report/seed/machine-admission, actor-role and cleanup-parser
defects were corrected before final primary14 and the fresh reproductions. For
example, run09 stopped at cancellation/N1 after twenty contained failures and
fresh controls; its Docker-absence parser failure is not relabeled a pass.
Focused run10 and rotated-stack run11 informed the correction; only the complete
final primary and independent bundles establish the final campaign result.

## Required contracts and evidence boundaries

Rows overlap: they map requirements to tests and must not be added as independent
case counts. “Accepted finite” denotes the scoped contract and now includes its
fresh final-source regression reproduction. Later exclusions are not silently
counted as passed.

| Contract | Executed profile and decisive evidence | N / status | Explicit limit or remaining obligation |
| --- | --- | --- | --- |
| V1 guest address domains | Real FEX high/low guest RIP distinct from host source buffer and native code cache; high data/stack accesses; TTBR1 fills; full guest/native fault frames and source binding. Load and stack oracles check actual generated effects. | N1 then N1–4 compositions; accepted finite | General high-address ISA/helper forms remain unsupported; high address does not imply supervisor privilege. |
| V1 permissions and precise boundaries | CPL0/CPL3, WP, NX/SMEP, noncanonical/private-gap rejection; low/high aliases with shared and independently checked parent/leaf permissions; late #SS/#GP prefixes and checked split stores with unchanged failed-store effects and preserved A/D. | N1–4, all actor positions in the fifty boundary cases; accepted finite | Split-store service is the declared finite operation, not arbitrary split/unaligned atomic or device behavior. |
| V1 concurrent stack/source lifetime | Actual generated stack prefix survives retained fault; old route completes before remap; delayed acknowledgements, source change, stale reply/entry controls and code-pin retirement; full physical pages and canonical/native frames checked. | N1–4; accepted finite; corrected native actor-role rotation reproduced | Fixed program/metadata boundaries; no claim that every compiler-generated instruction has recovery metadata. |
| V2 paging-off execution | Protected32 real producer execution; no implicit guest-table reads/A/D/#PF; segmented fetch/data including subpage bases, address-size16/32, admitted wrap and A20 routing; owned RAM, bounded device/hole service and late segment faults. | N1 instruction cases plus sixty N1–4 protected32 address cases; accepted finite | Finite segment/base/address-form combinations, fixed fixture A20 contexts and limited segment types; dynamic A20 changes/general segmentation/device backends are not established. |
| V2 mixed paged/off owners | Shared physical source/data, independently resolved fetch/access rules, actual retained old routes, context/source-bound delayed compilation, stale rejection, publication and retirement. | N1–4 finite mixed profiles; accepted finite | Off-path ordinary writes to table backing are not forbidden; the asserted absence concerns implicit page-walk/A/D effects. Arbitrary mixtures remain later coverage. |
| V3 actual control semantics | Fetched MOV CR0/3/4, WRMSR EFER and far transfers; legal off/legacy32/PAE/IA-32e paths, same-value CR3 invalidation and ABA, illegal precommit rejection, exact next fetch and prior effects. | N1 finite cases; accepted finite | Unsupported controls/descriptors explicitly reject. Preserved or undefined flag/register policies are scoped; these are not all control transitions. |
| V3 hidden PAE state | All-four transactional PDPTE load; backing mutation versus INVLPG/reload; failed candidate load and preserved prior hidden snapshot under the recorded source interpretation; independent table/model checks. | N1 and concurrent families; accepted finite | Native cases are the declared 4KiB/NXE0 profile; additional large-page/NX unit checks are not native coverage. |
| V3 local transitions with peers | Fifty family/N/actor rotations retain peer accesses/code and pending real compiles across local commit/rejection; peer state equality, source-only and context-only stale controls, return to previous numerical controls, protected maintenance and canonical event cuts. | N1–4; accepted finite; seeded PAE/long ABA reproduced | Local control mutation cannot mutate a peer's architectural state. General asynchronous guest control programs remain untested. |
| Guest ordering and atomics | Complete generated SB/MP/LB/WRC/IRIW/fenced participants, independent bounded FIFO-buffer model, observed allowed outcomes; aligned DWORD XCHG token chains, precommit and postcommit exact-PC recovery. | Declared role counts through N4; 154 ordering cells accepted finite | Finite absence of forbidden outcomes is not a universal ordering proof. Unsupported atomic widths, alignment, memory types and devices remain excluded. |
| Fence completion/live roots | Twenty LFENCE dependency cells retain GPR/Q/status roots, deferred flags and masked sticky FP status across a complete capture, completion barrier and exact restore. Producing ELF gates and corruption controls checked. | N1–4; accepted finite | Raw FEX DMB observations alone are not x86-fence qualification; arbitrary FP/vector/dependency programs remain outside the fixture. |
| Translation coherence | Sixty parent/A/D/page-size profiles: cached versus invalidated A/D clearing, independent aliases and both subpages, 4KiB/2MiB transitions, hidden PAE backing, retained old routes and all-owner acknowledgements. | N1–4, every actor position; accepted finite | Actual private code clearing is exercised. Physical page-table/data backing recycling is exclusion-only, not an implemented poison/reuse test. |
| Native synchronization/publication | Ticket-lock authority, release/acquire rendezvous, completed native cache/TLB maintenance before publication/acknowledgement, immutable compiler objects and guarded entry; see argument below. | Final-source finite-path review and actual remap/publication/retry observations reproduced | Guest litmus outcomes cannot prove host/resident synchronization. Hardware/cache premises and each additional writer/observer still require their own binding. |
| Seeded healthy composition | Four families × N1–4 × four seeds: stack/source, PAE/long ABA, parent/page-size coherence, postcommit XCHG drain/remap. Actual reduced maintenance pool at N≥2, identity-preserving FULL/readmission, eligible-owner choices and bounded fairness/event predicates. | All 64 corrected cells independently reproduced | Pool exhaustion is claimed for stack/control maintenance only. N1 maintenance FULL is not applicable; compiler capacity is separate. Rotated actor metadata is bound to actual native roles. |
| Partial-service recovery | Changed-on-read sampled value retained across the second real STRB fault; first effect remains committed, no repeated sample/store, competing mutation denied, FIFO retained route, event boundary; ELF-bound generic exception/restore/helper sites. | All N1–4 × two seeds =8 cells independently reproduced | This is a semantic partial helper, not generated REP or a complete device model. |
| Intentional containment | Worker exit, malformed reply, panic with authority held, withheld acknowledgement and cancellation with deferred work; immutable architectural prefix and seeded ledger, original cause, owner joins/destruction, owned child/container cleanup and separate-host/new-VM healthy control. | All 25 cells plus fresh controls independently reproduced: N1 actor0, N2 actors0/1, N4 actors0/3 per failure | N3 containment untested. An abandoned held lock/reference is preserved as failed state, not fabricated repaired state. Healthy failures are not relabeled as intentional. |
| Evidence/validator independence | Actual source/body/request/reply binding, retained pins/checksums and raw failures, independent semantic models, validator corruptions and fresh serialized rebuild/reproduction. Six pinned-QEMU scalar comparisons pass independent fresh execution including full defined RFLAGS checks. | Full final-source native regression and bounded supporting comparison independently reproduced | Atomic progress oracle and expanded partial gate validator were authored by this reviewer and independently reviewed by root; do not describe this reviewer as independent author-reviewer for those files. |

## Native synchronization, publication and lifetime argument

This is a finite code/contract argument checked against the implementation, not
a proof obtained from stress counts. The final producing sources and emitted
paths must retain these premises.

1. **Authority handoff.** `runtime/coordinator.rs` acquires a ticket with a
   relaxed increment, waits for its turn with an acquire load, and releases the
   lock with a release increment. Mutable coordinator state, guarded source
   mutation, queue service and seeded decisions occur under that authority.
   Relaxed accesses inside that critical section rely on this handoff, not on
   incidental host scheduling or guest TSO. Owner-local frames remain local;
   rendezvous fields observed outside the lock use their declared release/acquire
   protocol. Future unguarded writers would invalidate the argument.
2. **No wait-induced authority cycle.** Scheduler refusal returns immediately;
   callers drop the guard before polling. Compiler waits and actual generated
   execution occur outside the coordinator lock. Selection retains a designated
   service owner, while completion is separately recorded before the next
   request or mutation. The deliberately locked panic is a containment case,
   not an exception to healthy progress requirements.
3. **Closed admission before mutation.** Entry acquires an active/retained
   lifetime while OPEN. A fault detaches active execution but preserves the
   retained route/code. CLOSING prohibits new admission; EXCLUSIVE service drains
   the saved operations before an incompatible mapping/source change. Atomic
   composition retains only the post-exchange store: replaying the exchange
   would violate the observed token chain. Partial service similarly preserves
   the sampled value and next subaccess rather than restarting the operation.
4. **Native mapping acknowledgements are completed work.** Mapping replacement
   performs the admitted break-before-make sequence, DSB/TLBI/DSB/ISB on the
   owning CPU. Peer acknowledgements follow their actual local repairs; reopening
   requires the complete owner set. Guest permission/A/D decisions are resolved
   independently per alias. A recorded acknowledgement bit alone is insufficient:
   oracles also inspect native leaves, walks and subsequent generated accesses.
5. **Publication precedes eligibility.** Source/code publication validates the
   full pending identity and source/context under stable authority, stages
   complete reply chunks, writes code through the declared non-executable
   writable mapping, performs DC/IC maintenance and barriers, and installs the
   executable mapping before making the object eligible. Shared-code consumers
   perform their local instruction synchronization before entry; private-code
   paths publish on their owning CPU. Initial release/acquire publication and
   later guarded publication are distinct handoffs. No claim is made that a
   release store alone publishes instructions or invalidates a TLB.
6. **Entry and reclamation use different conditions.** Source/context change
   disables old entry even if the numerical RIP/control tuple is reused. Pending
   compilation retains VM/incarnation/owner/source/context/request identity.
   Old code remains physically intact until old active/retained references are
   gone and required acknowledgements/publication hold. The finite campaigns
   exercise rejection while the final pin alone remains, followed by actual
   code clearing after release. Physical table/data recycling is not inferred
   from that code-slot result.
7. **Events and failure observation.** Repeated notifications preserve one
   durable pending request; actual handoff rechecks phase, active/completed
   boundary and masking/priority and copies canonical state only at the allowed
   boundary. This is event-boundary transport, not IDT/APIC delivery. Failure
   witnesses publish with release and are acquired by the host. Original causes
   survive cleanup. Joined-stop timing precedes VM destruction, so successful
   whole-process gate-relative supervision is also mandatory for the final
   teardown bound.

Relevant implementation seams include `runtime/coordinator.rs`,
`runtime/concurrent/{source,maintenance,events,aliases}.rs`,
`runtime/{schedule,ordering,atomic_progress,partial_progress,containment}.rs`,
the per-family code publishers and host owned-process/container journals. Their
paths are tested under immutable finite source profiles and validated native
machine settings. This argument does not extend automatically to arbitrary DMA,
debugger writers, hot-plug owners, memory types or concurrent guest programs.

The reviewer independently disassembled the corrected run10 producing ELF
`0e8c0a5bcd03bfdc02f4bb0acbdb8ad6e93bb35c18384b700cac13928ca3b945`.
Its RX segments occupy 168 and 224,340 bytes. Recomputed conservative emitted
stack depth remains 15,040 of 16,384 bytes: capture 800 → finish 32 → concurrent
completion 32 → queued 1,744 → maintenance finish 2,528 → drive 8,224 → stack
start 16 → enter-step 1,664. Known stack-reset gates terminate the path; finite
handled-exception/source-generation recursion and formatting-only indirect calls
retain the qualifications documented in the earlier stack review. All six final
replicas reproduced this exact ELF, so this finite emitted-path audit applies to
their producing binary; it is not an assumed bound for later binaries.

## Replay, upstream reuse and deferred coverage

**Exact offline replay is implemented.** The retained decision ledger contains
PRNG version/state/draw, cut, independently reconstructed cohort/eligibility,
chosen owner and draw/claim positions in the actual native trace. The oracle
replays those exact recorded operations and rejects wrong choices, early or
missing actions and unconsumed grants. Failure prefixes use exact replay-derived
completion masks, not blanket exemptions from healthy scheduling checks.

**Recorded-decision native playback is unimplemented.** The runtime accepts a
seed and makes fresh decisions at native cuts; it does not ingest a previous
decision ledger. Re-executing a seed is not evidence of the same global native
interleaving. The owning investigation now explicitly defines the remaining
pass's exact replay as offline semantic/schedule replay and identifies native
saved-ledger playback as unimplemented and unclaimed. This resolves the earlier
ambiguous wording without changing the implementation plan's requirement for
retained seeds/traces, controlled/seeded native executions and independent
reproduction; the plan does not specify a native playback engine.

FEX and kvm-unit-tests adaptations retain pinned revisions, source paths,
licenses, original expectations and adaptation rationale in `fixtures/i4v` and
its manifests. Their instruction inputs do not make FEX an independent oracle.
New memory/order/protocol models and ABI-specific scenarios supply independently
derived expectations. Importing a QEMU TCG corpus or adopting herdtools7 remains
candidate work, not a suite claimed imported or passed. Separately, the plan's
instruction to compare defined outcomes with a pinned QEMU configuration is an
evidence obligation, which the fresh final reviewer identified as missing from
the earlier draft. That gap is now covered by final strict primary
`i4v-qemu-dev-20261026-03` and independently executed
`i4v-qemu-independent-20261026-02`: six defined scalar outcomes, pinned QEMU
7.2.22/`qemu64`, actual executed bytes and output checks, fresh resident
03/04 bindings, full defined RFLAGS equality without a mask, 420 corruption
controls and exact container/volume cleanup. The separate findings own the
independent comparison audit. This is neither a complete imported QEMU
suite nor universal differential conformance, and it adds no ordering, MMU,
privileged-mode or concurrency claim. No native-x86 comparison is claimed where
it was unavailable. Undefined state masking follows the named finite profile
rather than masking disagreements wholesale.

Full kernel boot/userspace, general ISA/FP/exception delivery, production
devices/interrupts/timers, arbitrary segmentation or dynamic A20 transitions,
all atomic widths/alignments/memory types, table/data recycling, host-generation
parity, performance selection and final code-generator choice remain visible
later work. They are not newly added pre-boot acceptance gates. Unsupported
profiles must reject explicitly and cannot reduce the eventual emulator scope.

## Final reproduction and measured bounds

The durable [review-audit bundle](../../../../natemu-raw/i4v-progress-review-audit-20261026-01/README.md)
retains the independent audit scripts, per-bundle receipts, exact union check,
frozen legacy input hashes and manifest/checksum-index bindings. The producing
bundles remain unchanged. The fresh final reviewer separately rechecked the
union, source/ELF identity, every checksum and outer-process cleanup.

| Fresh bundle | Scope | Jobs | Native executions | Wall seconds | Peak build/test group MiB | Peak worker outer group MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `i4v-progress-independent-20261026-01` | 72 healthy + 44 baseline | 163 | 116 | 467 | 521.3 | 81.8 |
| `...-02` | 25 failures + 25 fresh controls + 44 baseline | 141 | 94 | 609 | 538.7 | 82.6 |
| `...-03` | Coherence/fences/ordering + baseline | 325 | 278 | 579 | 527.1 | 82.2 |
| `...-04` | Stack/address/mode/concurrent regression | 441 | 394 | 1,178 | 531.9 | 82.1 |
| `...-05` | Protected32 address forms + baseline | 151 | 104 | 575 | 537.8 | 82.0 |
| `i4v-progress-regression-independent-20261026-01` | Exact ordered legacy suite | 87 | Separate legacy accounting | 62 | 545.8 | 27.9 |

All 1,308 outer jobs terminated successfully and were reaped with the recorded
group cleanup/quiescence proof. The five I4-V bundles retain 6,512 exported
compilation records, 6,572 report/export associations and 6,522 owned-cleanup
records, including non-exporting intentional failures. The legacy audit also
checks 67 owned process records and 57 removed container records and the empty
remaining-container result. All **206,973 checksums** match: 203,545 in the five
I4-V bundles and 3,428 in legacy. All five retain the same **811 producing
inputs** as final primary14: 807 code/tool/fixture files plus four root build
inputs. Legacy retains 132 frozen current inputs, including 23 additional
legacy-only files; it does not rebuild an old source snapshot.

The common runtime SHA-256 is
`0e8c0a5bcd03bfdc02f4bb0acbdb8ad6e93bb35c18384b700cac13928ca3b945`.
The six bundles total 1,533,862,158 bytes, below their individual 2GiB I4-V /
1GiB legacy limits. This total excludes separate developmental and QEMU bundles.
Every bundle remained within 1,200 seconds without widening or restart; bundle04
used 1,178 seconds, leaving only 22 seconds of margin. Future growth must be
bounded separately rather than inferred safe from that small margin.

Builds retained the two-job/8GiB class; worker jobs retained the 512MiB class and
native backing the declared finite limits. RSS figures above are the monitored
outer process groups, **not aggregate memory across separately owned groups and
containers**; compiler container caps are separately configured and retained.
Containment's largest observed gate-to-detection and gate-to-joined-stop deltas
were both 2,830ms, within 3,000ms and 10,000ms respectively. Successful external
gate-relative supervision additionally covers final host/VM teardown.

Native reproduction began at 16:52:07 UTC and ended at 17:56:21 UTC, with the
coordinated QEMU windows between bundles and no overlapping native/build/compiler
campaigns. Native reproduction therefore finished 231 minutes 21 seconds after
the remaining pass's 14:05 UTC start, inside its 360-minute wall bound. At the
17:59:54 UTC final audit checkpoint, elapsed wall time was 234 minutes 54 seconds.
**Aggregate agent minutes were not authoritatively measured**; the declared
720-agent-minute bound is not presented as a measured result.

## Exit evidence (satisfied)

- The corrected 72 healthy cases and 25 intentional failures passed with their
  mandatory fresh controls. Real actor roles, effects, faults, exact seeded
  decisions, FULL/readmission and event predicates were checked.
- Every injection fired with its specified retained work. Original failures were
  preserved and the 3-second detection / 10-second outer limits enforced
  from the observed gate. Joining, destruction, reaping and owned-container
  removal completed before each separate-process/new-incarnation control started.
- Fresh builds reproduced all 688 prior distinct native cases and 87
  legacy jobs against the same final runtime and frozen producing inputs. Counts
  distinguish repeated baseline/control executions from distinct coverage.
- Independent audits verified real compiler/delivery associations, source/build/model
  pins, checksums, emitted gates/stack/layout, resource limits and cleanup. They keep
  historical failed diagnostics unchanged and link the corrections.
- The independently reproduced six-case pinned-QEMU supporting
  comparison and its exact scope are retained; candidate corpus status does not silently
  waive the separate comparison requirement.
- Substantive final-review findings were resolved. This matrix records
  passed, historical failed, unimplemented and untested coverage with its configurations,
  replay interpretation and exclusions explicitly. A time/resource
  bound or a large test count does not itself establish completion.
