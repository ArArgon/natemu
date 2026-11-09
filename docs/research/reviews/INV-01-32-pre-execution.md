# INV-01-32 — Pre-execution review

**Latest return, 2026-09-07 07:15:13 UTC: accept producer
`q32-interrupts-build-06` with oracle 01/supervisor 01 for the four remaining
required fresh pairs, in 2,2,2,4 order, within the original campaign limits.**
Its single setup-timing change configures SPIs at the all-stopped Q31
checkpoint. This tests a live hypothesis; it does not declare the host-SPI
failure fixed. The first unexplained failure stops execution for review.

**Latest return, 2026-09-07 07:08:28 UTC: accept one two-CPU readback-only
diagnostic using `q32-interrupts-build-04`, within the original stopped
campaign's existing slot/deadline limits.** The first host-SPI failure remains
unresolved. This does not resume the primary success campaign; the diagnostic
outcome requires review before another experimental action.

**Current verdict, 2026-09-07 06:57:43 UTC: accept the complete Q32
preparation for the declared finite primary campaign, using producer
`q32-interrupts-build-02`, oracle `q32-interrupts-oracle-01` and supervisor
`q32-interrupts-supervisor-01`.** Independent replication retains its own
preparation review and separate finite activation. This is execution-input
acceptance, not an observed Q32 feasibility conclusion. The final disposition
and exact input verification are recorded at the end of this thread.

## Q30/Q31 prefix recount and candidate geometry — partial preparation acceptance

2026-09-07, `/root/track01_understanding_check`, independent of Q32 producer
and oracle authors: **accept this tighter prefix arithmetic against the frozen
Q31 build-03 sources, and the candidate mapping geometry only**. This bounded
15-minute pass is not complete code, resource-envelope or execution acceptance.
The source screening from the findings review remains applicable: the native
experiment must establish the cross-ISA composition, while a source-derived
budget establishes only room for declared successful execution paths.

Inspected every `q31.c` case branch, delivery/IRET/access/fixture helper,
repetition multiplicity and the relevant `q30.c` logging, role, pin and epoch
paths against `natemu-raw/q31-faults-build-03/source`.
The original Q30 component remains 1,924,051 records. The tighter Q31 tables
sum to 846 accesses, 1,825 other events, 63 context transitions and 62
invalidations per complete variant set per CPU. Multiplication by four CPUs
and sixteen repetitions gives 54,144 accesses, 116,800 other events, 4,032
contexts and 3,968 invalidations. These are inspected branch bounds, not
measured access counts.

The tight paths have sufficient allowances. Ordinary delivery uses nine
accesses or ten with a stack switch; IRET uses at most seven, six for the
nested NULL-SS return. Fetch restart includes its previously fetched prefix
and new fetch; XADD includes the successful prior store and failed write/read
branch. Group 6/2 allows all four first attempts to fail: each round costs at
most 27 accesses including both baseline/denial/success observation pairs,
peer write, delivery, repair, IRET and retry, plus the final mapping gives
109. REP includes eight successful byte pairs and one failed pair before
repair. Early descriptor case 9h costs at most 48 accesses including both
deliveries/returns; the nested-handler case costs 46. The group-11 bound 34
uses the larger peer fault/real-retry path. Case 12 includes its tracked write,
stale rejection and successful follow-up.

Other-event counts include seven chunks per architectural snapshot, every
attempt/delivery/return, REP progress, diagnostic/control markers and CPU
case-end; the global CASE pair is charged separately. Contexts include
fixture transitions even where the helper returns early. The four split
repair rounds receive nine invalidations. Setup retains 96 mutation slots
per CPU-case, above the source's 93; only the first Q31 root activation changes
table reachability. Later changes are leaf mappings or CPL transitions.

The per-access ceiling retains Q30's native-fault/write/ACK allowance plus
Q31 recipe/STEP and a code-generation event. A second WAIT on fetch
prevalidation remains below that ceiling. Split operations retain their
extra 24-event double charge for twelve operations per CPU/repetition.
The 8,192 separate role/code allowance remains ample: track enable/disable,
all pin/unpin and stale-pin lock attempts cost at most 4,592 events at four
CPUs/sixteen repetitions, plus at most 40 initial table-role changes. It does
not need observed low contention to fit.

Independently ran the arithmetic copy at
`/private/tmp/natemu-q31-budget-recount/check_budget.py`; its three guarded C
inputs equal frozen build 03, and its counting tables equal the worktree
candidate. **PASS: 3,614,355 prefix records.** That copy uses the old 512 MiB
capacity; the worktree's changed bottom section proposes 832 MiB and 8,388,608
records. Current worktree source hashes are intentionally not refreshed, so
this verdict cannot certify newly integrated Q32 code from the old hashes.
No observed raw count was used as proof, and no VM/full oracle was run.

The proposed identity region is `[0x40000000, 0x74000000)`: 416 two-MiB
regions in `l2[0..415]`, with the alias at index 416 / VA `0x74000000`.
Both occupy the existing L1 index 1; device mappings remain at index 0.
Thus the relocated alias does not overwrite an identity entry. The proposed
log ends at byte offset 822,089,608 within 872,415,232 bytes of RAM, leaving
50,325,624 bytes. Its larger page-table array occupies 416 pages; the final
linked image still must fit the separate 2 MiB image reservation.

Full preparation remains pending: inspect Q32's additive successful-path
bound and any prefix hook events, final source/hash inventory, complete
image/link layout, relocated-alias and expanded-mapping oracle checks,
832 MiB checkpoint/final output handling and corresponding checker RSS/disk
limits. This partial acceptance does not authorize execution or establish
that the entire Q30/Q31/Q32 program fits its final finite resource envelope.

## Initial concrete producer review — returns, full verdict pending

2026-09-07, `/root/track01_understanding_check`: bounded initial 20-minute
read-only pass over `q32.c`'s 37 variants and helpers, Q31 delivery/return
exports, Q30 arrival hooks, native hold/wait assembly and the landing host
SPI/checkpoint path. The independent oracle and host/native integration were
still being authored. **No full code or execution acceptance is issued.**
This reviewer edited no producer/oracle source, executed no VM or full oracle,
and did not treat the author's model traces as native evidence.

Three concrete returns were sent to the parent:

1. **REP saved RF.** Initial `q31_interrupt` preserved RF=0 for IRQs after four
   completed REP iterations. The accepted shared-IDT rule requires RF=1 in
   that partial-progress frame. The inspected remedy adds an explicit
   partial-progress input to delivery and uses it for forward/reverse REP,
   preserving canonical pre-entry flags while setting saved RF. Producer-side
   remedy is adequate; matching independent frame/IRET checks and a corruption
   control still require final review. Ordinary boundary IRQs must preserve
   their original RF rule.
2. **Mid-handler IF change.** Initial nested-priority setup used a bare IF
   assignment inside the executing handler. The inspected remedy executes
   contiguous modeled STI, checks its inhibited boundary, then executes NOP
   before higher-class selection. The handler also gains an actual short
   branch to its modeled EOI operation. Producer-side remedy is adequate;
   final opcode/commit/return and negative-control validation remains pending.
3. **SPI line sequencing.** The initial worker asserted a newer request before
   processing an older deassert request. If the line was still high, the new
   edge could be lost and then cleared by the old deassert. Require completion
   of the previous line deassert before a new assertion, with per-sequence
   host evidence; polling intervals or intervening cases do not establish that
   order. Closure requires inspecting the final worker and its checker.

Other inspected mechanisms are suitable for completing preparation. Native
arrival hooks retain the enclosing operation while native handlers publish
pending work; the guest selects at explicit boundaries. Eligibility combines
IF/shadow with strictly higher APIC priority class than TPR/ISR. A collapsed
notification drains per-sender sequence mailboxes, so guest deliveries need
not equal hardware edges. Broadcast publishes every target payload before its
GIC writes and rotates active, IF-clear, halted and tracking recipients. The
native wait has a predicate recheck plus IRQ-issued SEV, covering IRQ arrival
between the check and WFE; masked guest halt remains a diagnostic stop. The
level-SPI fixture waits for deassertion and tests a separate quiet interval
using all raw SPI acknowledgements, including empty notifications.

The accepted case-9 endpoint checks actual early descriptor failure and #PF
entry with the IRQ still in ISR, then explicitly terminates that modeled
context. It must not be reported as successful IRQ service or guest recovery.
Guest EOI presently uses a tagged MOV to the fixture's RAM-backed EOI slot
and an explicit software ISR update; acceptance must not imply a complete
xAPIC address/MSR/MMIO decoder. Likewise the two-sender test permits an extra
empty native acknowledgement if the second SGI write arrives later; retained
raw counters must support any hardware-coalescing claim.

Full review still requires stable corrected producer/native/host paths,
independent oracle acceptance semantics and controls, source-derived complete
resource bound, strict full image/link/layout checks and concrete retained
inputs. Prefix geometry acceptance above does not waive these requirements.

## Complete candidate budget and execution limits — partial acceptance

2026-09-07, `/root/track01_understanding_check`: independently reviewed the
current additive Q32 call paths and “Concrete cumulative and execution budget
submission” within the assigned 15-minute pass. **Accept the candidate
successful-path arithmetic and proposed finite campaign limits**, subject to
final source/hash and complete code/oracle review. No VM or complete oracle
was invoked, and no measured model count was used as the bound.

The Q32 fixture performs seventeen accesses per CPU-case: six gate writes,
ten fixed data/opcode writes and the final program write. A completed CPL0
IRQ uses at most 24 accesses, including its two branch-byte fetches and
seven return/descriptor reads. The table includes all three priority IRQs,
privileged CLI/STI fault frames, the repaired STI-following load, current-fault
precedence and early IRQ-descriptor failure. Forward REP's 102-access case
includes sixteen initialization writes, eight successful pairs, one failed
pair, the IRQ, #PF repair/return and final byte observations. Reverse and
zero-count paths need 82 and 42. Local/remote code paths receive 59/60,
including each CPU's additional controller write. Broadcast charges every
recipient the largest role's extra opcode or mapping write.

The independently recomputed result is 116,672 accesses, plus 1,802,048 fixed
Q32 events. The 512-other-event allowance accommodates the nested-priority
path: three IRQs, optional STI/NOP sequences, extra boundary snapshots,
returns and restored-register progress snapshots remain below 400 before
the separately charged access/code events. REP and code paths are smaller.
New sent-sequence/partial-progress/diagnostic control events fit that margin;
the 8-context/8-invalidation allowances exceed the finite paths. Setup stays
at the inherited 96-patch ceiling. Root reachability remains unchanged.

Standalone Q32 code role/pin epochs cost at most 3,968 events under the current
local/remote cases, below the separate 8,192 allowance; access-associated
generation increments are already charged. Non-timer empty notifications have
a checked cumulative per-source/per-CPU ceiling, giving 32,768. The 4,096
global allowance covers CASE pairs and startup controls. Together with the
accepted 3,614,355 prefix, the recomputed total is **8,144,915 / 8,388,608**,
leaving **243,693 records**. This arithmetic assessment does not bypass the
intentionally stale source guards or certify future source changes.

The primary 270-minute campaign / at most six VMs and eight full checks, and
independent 100-minute / two-VM/two-check campaign, are acceptable with their
recorded activation, required orders, failure stops and no silent renewal.
The new-pair reservation is exactly 20×30 + 1,800 + 300 = 2,700 seconds;
same-RAM rechecks reserve 35 minutes. Per-run watchdog/output and sampled
8 GiB checker-RSS limits are explicit. Three 832 MiB RAM snapshots plus the
768 MiB paging image account for 3,264 MiB per run before small artifacts;
24 GiB primary and 10 GiB replica free-disk reservations provide reasonable
finite headroom for the specified campaigns and bounded checker outputs.
Resource-limit exhaustion remains a retained failure, not permission to
increase limits or exclude a run.

SPI return 3 is closed on the inspected producer side: the worker now applies
an outstanding deassert first and admits a new assertion only when the prior
sent sequence has been deasserted. The new masked-HLT request/ack also has
an adequate source-level endpoint: the external host verifies IF=0, halted,
pending vector and current case while the guest holds that state, then
acknowledges explicit fixture termination. This is no guest wake/IRET and
requires no whole-VM restart. Final oracle/host-log binding remains pending.
The collapsed-SGI fixture additionally waits for both sender publications
made after their actual MSR/ISB before unmasking; final native evidence must
still distinguish sent, accepted and empty notifications.

The earlier compile-only build predates these fixes. Final full image size,
expanded mapping and checkpoint checks, complete oracle/controls and frozen
input inventories remain necessary before any execution acceptance.

## Independent oracle and final producer return pass — in progress

`/root/track01_understanding_check`, 2026-09-07: a separate bounded review
window began **06:41:30 UTC**, with a hard stop **07:01:30 UTC**. This pass
reviews the current oracle, producer return deltas and retained supervisor;
it does not authorize a VM or issue a final verdict on unfrozen inputs.

The raw callback has the required three-snapshot composition: it revalidates
the Q30 checkpoint within the Q31 checkpoint, retains the exact old log
prefixes and replays the complete final log, native mappings/faults and code
ownership. The 85-word Q32 CPU decoder matches the producer ABI, including
mailboxes, physical send publications, raw/empty native acknowledgements and
external diagnostic request/ack. Final host SPI transition order and masked
HLT observations are bound to retained per-CPU records. These checks cover
the explicit fixture endpoint; they do not turn masked HLT into guest wake
or the early GDT exception into completed original IRQ handling.

Three oracle returns have adequate inspected remedies. The collapsed-SGI
case required native stage 1, whereas its initial predicate required 0.
The native-fault dictionary was overwritten by a guest-fault list in group
8/2 and would fail at a later repair repetition. Both are corrected, with
shared native-path positive/negative controls and the repeated exception-to-
repair transition. The reviewer independently ran the current self-check:
**15 corruptions rejected**, including wrong critical stage, missing native
fault and missing exclusive gate. Finally, an arbitrary canonical change
between scalar commit and interrupt boundary was initially unchecked. The
new predicate requires equality to the preceding canonical snapshot. A
reviewer-created coherent RBX change after actual model XADD commit, carried
through every later boundary/handler/IRET state, is rejected; the selected
unmodified case passes. An earlier whole-model attempt used an older trace
without the latest broadcast-send controls and failed that coverage check;
it is preparation-version mismatch, not hardware evidence or a VM failure.

Two further returns remain with the oracle author at this checkpoint:
require exactly one actual handler-effect store per normal external delivery,
including nested handlers, and require the local code-write native arrival
to lie inside its real exclusive write operation rather than relying only
on stage and generation metadata. Final model controls and the complete
native-route acceptance predicates remain under review.

Sparse host output is adequate: source RAM is a separate `hv_vm_allocate`
allocation, so truncating output cannot invalidate it. Truncate-to-zero then
exact-length creation removes stale data, skipped all-zero chunks read as
zeros, and partial/EINTR writes plus fsync preserve bytes. The retained
self-check must include trailing-zero and previously-nonzero-to-all-zero
rewrites. A separate producer return to disable/quiesce an SPI before changing
its trigger configuration is still being authored and must be reviewed.

The reviewer recomputed all four complete inventory entries in
`q32-interrupts-supervisor-01`, verified its source equals the current
`run_check.py`, and inspected retained self-check PASS. This supervisor input
is accepted: explicit `--q32` selects the five checker sources, 1,800-second
wall limit, 8 GiB process-group RSS sampled every second, and 256 MiB output
file limit. This is an observed RSS ceiling, not a hard instantaneous memory
reservation. No VM or full raw acceptance run was performed by this reviewer.

**06:53 UTC return closure:** the last two oracle remedies are adequate.
Total successful C100 stores must equal normal external dispatch count,
while each nested entry retains its own first-effect check. The local code
arrival must be inside an operation with the exact tracked store address,
width/value, real native fault and mutation, and its gate must remain at
phase 1 with this CPU owning it. Independent current self-check now passes
**18 rejected corruptions**, including a completed-write arrival and a missing
exclusive epoch; the author also added the duplicate-operation model control.
CONTROL 12 now equals actual REP boundary RIP/count/source/destination.

The host trigger-configuration delta is adequate for this fixed fixture:
after the previous line is deasserted, it disables the selected SPI, observes
no active state, clears obsolete pending state, changes ICFGR, reenables and
asserts the new request. An active previous notification postpones this work
under the existing watchdog. The register offsets are declared by SDK 26.4.
The reviewer independently opened
[Arm GICv3/4 overview, version 3.0, §5.2.4 p23](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/Learn%20the%20Architecture/GICv3_v4_overview.pdf?revision=65f91645-cd52-4795-952b-f01095ff5ef8),
which supports disabling an interrupt before reconfiguration. This is a
source/API applicability check; execution must still establish the virtual
GIC behavior on the declared machine. Sparse retention and trigger changes
add no guest log operations. The refreshed guarded budget independently
passes at **8,144,915 / 8,388,608**. All substantive source returns from this
pass are closed; complete retained producer/oracle inventories and final
controls still precede execution acceptance.

**06:55 UTC retained producer input acceptance:** independently recomputed all
107 complete checksum entries of `q32-interrupts-build-02` and verified all
17 source files equal the reviewed worktree. Its 19 build and 14 preparation
commands passed. All seven linked guest objects independently equal their
strict `-Werror` preparation copies; the final 1,851,392-byte image fits the
2 MiB reservation. The retained runner, guarded budget, host checkpoint-
timeout and sparse-output controls pass. Synthetic two/four-CPU runs pass
55,968/116,640 accesses with at most 93 setup patches, explicitly without
native paging/interrupt evidence. The exact Q32 source is SHA256
`6dec464a65d8012891ec5ba7af92222ae242372d05f02c32e5625b896993d2f4`;
host source is
`e436081dc8b965cdee6a8914ff47639133fb2e871ee7763c6f0448e708e97b6a`.
This accepts producer build 02 and the earlier supervisor 01 as concrete
inputs. Final full preparation acceptance still awaits the frozen oracle
and its final real-pattern controls. No experimental run has been admitted
by this verdict.

## Final exact-input pre-execution acceptance

2026-09-07 **06:57:43 UTC**, `/root/track01_understanding_check`, independent
of producer and oracle authors: **accept** the final method, source and
finite execution preparation. This closes the separate review window begun
06:41:30 UTC before its 07:01:30 hard stop. All substantive returns above
are closed. No extra VM or full raw acceptance run was executed by this
reviewer; the reviewer authored no producer or oracle implementation.

The concrete inputs under `natemu-raw` are:

| Input | Independent retained verification |
| --- | --- |
| `q32-interrupts-build-02` | All 107 complete inventory hashes recomputed; 17 source files equal reviewed workspace; 19 build and 14 preparation commands pass; seven linked objects equal strict compilation; final image 1,851,392 bytes. |
| `q32-interrupts-oracle-01` | All 19 complete inventory hashes recomputed; all five sources equal reviewed workspace; six retained check commands pass with empty stderr; all three named model-input hashes independently verified. |
| `q32-interrupts-supervisor-01` | All four complete inventory hashes recomputed; source equals reviewed workspace; retained supervisor self-check passes. |

The exact Q32 checker hash is
`d609fd088c6bd790f42686487b62b1b75896c3954075f9e3eb3eedd8a0caed19`.
Its final self-check independently reran successfully with 20 rejected
corruptions, including the new raw ABI/unused-slot controls. Retained
two/four-CPU model checks each cover 592 global cases and reject 21 real-
pattern corruptions, at 55,968/116,640 accesses. Applicability is explicit:
two CPUs cover 1,088 CPU-cases and mark 96 not applicable, comprising the
two-sender collapsed case and both four-target broadcasts; four CPUs cover
all 2,368 CPU-cases. The accepted raw path independently requires actual
native sources, critical arrival intervals and code/paging epoch evidence
that these model traces cannot establish. Q30's 45-control regression and
Q31's 544-case/49,840-access/28-control regression pass in the retained suite.

The final source-based capacity of 8,144,915 records fits 8,388,608, with
243,693 spare; the guarded script passes against these frozen inputs. The
832 MiB native identity/alias geometry, three exact-size same-VM snapshots,
1 GiB producer files and 256 MiB checker outputs are covered. Accept the
declared 270-minute primary campaign, four required fresh pairs in 2,2,2,4
order, at most two reviewed diagnostic VM slots and eight full checks,
45-minute fresh-pair and 35-minute same-RAM admission reservations, 24 GiB
disk reserve, 10-second watchdog, 30-second producer children and
1,800-second/8 GiB observed-RSS checker. The separate replication remains
100 minutes, two fresh 2/4-CPU pairs and two checks, 45-minute admission and
10 GiB disk reserve, following independent preparation acceptance. First
unexplained failure stops the campaign for review; no limits, exclusions or
activation periods renew implicitly.

The source screening remains scoped: APIC priority, native GIC delivery and
ordinary VM device configuration are existing mechanisms. The experiment
must demonstrate their composition with coherent cross-ISA memory,
instruction recovery, guest frames/IRET, halt races and code metadata in
the same running VM. It may establish only the declared M4/SDK 26.4 two/four-
CPU finite mechanism set. It must not promote modeled timing, all-core
placement, full APIC/ISA coverage or diagnostic context termination into
stronger claims. In particular, early GDT delivery failure retains the
accepted ISR without claiming original IRQ completion; masked HLT's host
acknowledgement terminates that held fixture context without guest wake.
Observed findings, adversarial coverage and independent runtime replication
still require their own findings review before a Q32 scoped yes.

## Independent replication preparation acceptance

2026-09-07 **07:00:59 UTC**, `/root/track01_understanding_check`: **accept**
`natemu-raw/q32-interrupts-replica-prep-01` for the
separately activated, already bounded two/four-CPU replication campaign.
This new review began 07:00:08 UTC and ends here, within its ten-minute
allowance. Primary runtime execution is owned by the parent; this verdict
reviews only independent preparation and makes no observed-result claim.

Independently recomputed all 119 complete inventory hashes. The inventory's
own SHA256 is
`4110c4c2d3fec48a4f8b47d8c9e808b5e1f531ca60cb6472ba874a52738c0833`.
All 17 producer source files and ten compiled artifacts independently equal
accepted build 02; all seven strict guest objects equal the replica's linked
objects. The final guest image is 1,851,392 bytes. The 19 build commands,
15 strict/synthetic preparation commands and four frozen-oracle checks all
passed. Build stderr contains the expected linker-version and successful
codesign output; strict and oracle checks have no diagnostic failures.

The independent runner/budget/host/supervisor controls pass. Two/four-CPU
fixture models pass 55,968/116,640 accesses with at most 93 patches. Its own
four-CPU trace passes the accepted oracle over all 592 global / 2,368 CPU
cases, rejecting 21 semantic corruptions. Q32's 20-control and Q30's
45-control checks and Q31 self-check pass. The referenced accepted
producer/oracle/supervisor inventory hashes match; SDK 26.4 and arm64 match
the declared preparation scope. Preparation finished 06:58:26 UTC, before
its 07:25:05 deadline, without executing a VM.

The replication agent discloses authorship of the separately reviewed early-
descriptor source/method refinement. It authored no producer, oracle or
supervisor code; that disclosed research contribution does not invalidate
this independent build/runtime replication role. Fresh replica runs and
full retained checking remain necessary. Activation must record its own
100-minute deadline, at most two VM/check pairs in 2,4 order, 45-minute
admission reserve, 10 GiB disk reserve and existing failure stop rules;
this preparation acceptance supplies no extra slots or automatic renewal.

## First host-SPI failure — single diagnostic input acceptance

`/root/track01_understanding_check`, bounded review **07:05:22–07:08:28 UTC**,
within the assigned fifteen minutes: **accept one fresh two-CPU diagnostic
VM using frozen `q32-interrupts-build-04/source`**. Oracle 01 and supervisor
01 remain unchanged. No VM was executed by this reviewer.

The reviewer independently decoded the failed run's Q32 snapshot and read
its host outcome: failure 6, two completed cases, CPU 0's first host payload
emitted/requested/sent but not accepted or deasserted, with timer and peer
variants completed. The host watchdog stopped the remaining CPU. This does
not establish a cause. The ordinary-VM host-device/GIC path is implicated;
the evidence does not yet disqualify cross-ISA guest interrupt boundaries.

Compared with accepted/failed producer 02, **only host.c changes**. The
reviewed helper sequentially reads CTLR, group, enable, pending, active,
priority, trigger configuration and route at initialization, before/after
assertion and timeout. It changes no register write or guest case. These
SDK-declared read operations cannot acknowledge a pending interrupt. A
reviewer return moved timeout collection after the existing `hv_vcpus_exit`
request, preserving the stop path before diagnostic work. All readings are
explicitly non-atomic; post-assert pending may already be cleared by an
acknowledgement. A readable correct route cannot exclude cached routing
derived from initialization before vCPU topology existed. Such an outcome
must remain unresolved rather than silently proving or rejecting that
hypothesis. Added host timing is diagnostic instrumentation, not a timing-
qualified or observationally identical run.

Independently recomputed all 107 complete retained inventory hashes,
verified all 17 sources equal the reviewed worktree, and confirmed that only
host.c differs from producer 02. Guest bytes and all seven linked guest
objects equal producer 02; strict objects equal their linked counterparts.
All 19 build and 14 preparation commands pass, including runner, budget,
host timeout/sparse controls and two/four-CPU synthetic fixture checks. The
exact diagnostic host source SHA256 is
`ca5219c80d47c3c71d648a1054d3201e8ff73439c9c961fa859c212342810485`.
Build 03 remains retained and superseded because it preceded the watchdog
ordering return.

The campaign record remains anchored at **06:59:26.670824 UTC**, deadline
**11:29:26.670824 UTC**, with at most six VM runs/eight full checks and the
existing 45-minute fresh-pair admission reservation. This diagnostic uses
one existing slot and preserves the original failure; it does not renew
the campaign or imply another retry. Guest log capacity, 832 MiB RAM,
10-second watchdog, 30-second producer child and 1 GiB output limits are
unchanged. No corrective route rewrite, bitmap-semantics change, extra VM,
or successful-feasibility conclusion is accepted by this diagnostic review.

## Pending but inactive SPI — setup-timing candidate acceptance

`/root/track01_understanding_check`, **07:12:00–07:15:13 UTC**, within the
assigned fifteen-minute review: **accept frozen build 06 as the next
controlled full-suite producer**, with unchanged oracle 01 and supervisor
01. No VM was executed by this reviewer.

Independent reading of `q32-interrupts-diagnostic-01` confirms initialization
and pre-assert CTLR 0x52, group/enable bitmap 3, priority word 0x8080 and
routes 0/1. SPI32's configuration becomes 2; pending changes 0→1 after
assertion and remains 1 with active 0 through timeout. This rules out the
specific lost-enable/group/trigger bitmap account for the observed first
SPI. It does not prove general host-setter semantics, eliminate every
masking explanation or expose cached routing state. No platform-wide GIC
bug or cross-ISA design failure follows from these observations.

The one intervention moves the unchanged initial SPI group/priority/route/
enable writes and initialized readbacks from pre-vCPU main setup to the
existing all-vCPU HVC34 rendezvous. Every owner has then actually run with
its final MPIDR affinity and is stopped; initialization precedes checkpoint
dump and readiness publication, so no CPU can enter Q32 early. The original
startup barrier is unchanged, as are all guest bytes, native delivery steps,
register values and diagnostic readbacks. GIC creation still precedes vCPU
creation. This is compatible with the SDK 26.4 `hv_gic_create` discussion of
affinity routing and final topology once vCPUs run. The SDK does not promise
that pre-topology writes are defective: setup timing remains the hypothesis
being tested. Build 05's pre-first-entry placement is retained and superseded;
it is not an accepted runtime input.

The reviewer recomputed all 107 complete inventory hashes in
`q32-interrupts-build-06`, verified all 17 sources equal the inspected
workspace and confirmed that only host.c differs from diagnostic build 04.
Its source SHA256 is
`ba81d95f527ded9b9c332a1f649c52a81b4f74016fe1e5740a69061d35e8688f`.
All 19 build and 14 preparation commands pass. Guest bytes/seven linked
objects are unchanged and equal strict copies; runner, guarded budget,
checkpoint/sparse-output and synthetic two/four-CPU checks pass. The source
event bound therefore remains 8,144,915 / 8,388,608.

Two of the original six VM slots are consumed, leaving exactly four for
the required fresh 2,2,2,4 primary pairs. The campaign retains deadline
**11:29:26.670824 UTC**, at most eight full checks, 45-minute fresh-pair
admission, original disk reserve, 10-second watchdog and 30-second producer
children. Execute the complete checker only after complete producer success;
stop on the first unexplained failure and retain it. This is no extra
diagnostic allowance or automatic campaign renewal. Independent replicas
must obtain preparation acceptance against build 06; their earlier build-02
preparation cannot support a claim about this changed host setup.

## Updated independent replica preparation for build 06

`/root/track01_understanding_check`, **07:19:03–07:19:16 UTC**, within the
assigned ten-minute return: **accept** immutable
`q32-interrupts-replica-prep-02` for the separate bounded build-06 two/four-
CPU replication campaign. The reviewer recomputed all **117 complete
inventory entries**; `SHA256SUMS` itself hashes to
`0dff3223d853b4c36bacee5c36eddc71e0e0f74390e386b67607b9566aba9642`.
All 17 sources and ten compiled artifacts independently equal accepted build
06, and all seven strict guest objects equal this replica's linked objects.
The 1,851,392-byte guest image is unchanged. Referenced producer 06, oracle
01, supervisor 01 and superseded preparation 01 inventories remain unchanged.

All 19 build and 19 preparation commands pass. These include runner,
guarded capacity, host timeout/sparse retention, two/four-CPU synthetic
fixtures, supervisor and Q30/Q31/Q32 self-checks. The frozen Q32 oracle
rejects 20 self-check corruptions and accepts the replica's own four-CPU
model trace over 592 global / 2,368 CPU-cases, 116,640 accesses and 21
rejected semantic corruptions. Preparation finished 07:17:17 UTC within
its 07:15:39–07:45:39 allowance, without VM execution or experimental
failures. The previously disclosed source/method contribution is unchanged;
the replicator remains independent of producer/oracle/supervisor code.

This preparation supersedes preparation 01 only for future build-06
replication; no retained inputs are rewritten. It permits the separately
recorded 100-minute, two-VM/two-check 2,4 campaign with the existing
45-minute admission and 10 GiB disk reserves and failure stop. It does not
accept the current primary runtime findings, prove the setup-timing cause,
or supply additional VM slots. Findings remain pending full primary checks
and independent runtime replication.


## Independent after-wait coverage correction preparation — September 7, 10:01 UTC

Screening (a): arranging an asynchronous arrival relative to a wait instruction
is a shared VM/emulator test obligation. Screening (b): the retained failure
shows ordinary timer delivery before WFE, not a missing platform capability.
Screening (c): this method return concerns only group 7/variant 2's placement
fixture. All interrupt dimensions, native WFE mechanism, guest HLT semantics
and prior Q30/Q31/Q33 dependencies remain required. Reviewer
`/root/track01_understanding_check` authored none of the Q32/Q33 producer or
oracle changes. The exact run 04 diagnosis is in INV-01-33-findings.md.

The one-millisecond arm-to-wait interval cannot guarantee after-WFE delivery
under host scheduling; increasing that constant does not establish ordering.
A masked native WFI alternative is source-supported by [Arm's Critical
Interrupt Prioritization, page 7](https://documentation-service.arm.com/static/5ed6547eca06a95ce53f9321?token=):
PSTATE-masked pending interrupts can wake WFI, unlike sources masked by GIC
priority. This could separate completed native wait from later exception
entry, but it changes the wait instruction/masking protocol and does not prove
physical blocked residency. Masking the existing WFE is not justified by that
WFI-specific source. This remains an alternative, not the selected submission.

The parent's selected candidate A is **admissible for preparation**: at most
8 explicit subtrials in each original group 7/variant 2 CPU-case, retaining every
native arrival and completing its actual guest IRQ/frame/EOI/IRET. For an early
arrival, execute the actual `F4 EB FD` HLT/short-JMP stream: IRET returns to the
JMP after HLT, whose fetched displacement branches back to a fresh HLT. No
architectural-state reset, discarded IRR, omitted guest service or unrecorded
retry is allowed. Trials are contiguous, numbered 1–8, and stop at the first
exact native post-WFE witness. If all 8 fail placement, the eighth IRQ is fully
serviced before the retained controlled failure. A successful case still
requires the original exact post-WFE PC; earlier trials are distinct retained
arrival placements, not excluded performance samples.

CONTROL 15 must bind each ordinal and reported result to that trial's actual
native source/count/ELR and post-HLT RIP. The oracle must check each trial's
HALT publication/recheck/wake records, IRQ/frame/EOI/IRET and, only on a retry,
actual JMP bytes/target and new HLT. It must reject skipped ordinals, more than 8
trials, unserviced arrivals, false post flags, continuation after an earlier
qualifying witness and an incorrect final PC. It must not infer correctness
from marker counts alone. Synthetic producer paths should cover 1 early trial
then success, 7 early trials then success on 8, and 8 early trials ending in the
controlled failure; the model remains no native scheduling evidence.

At four CPUs/16 repetitions the cap adds at most 448 trials and 448 two-byte
JMP instructions. The final source-derived budget must count the complete
additional guest IRQ, HLT, JMP and trace effects, and prove the unchanged
cumulative RAM/file/campaign limits still hold. The original native failure
and reopened Q32 repeatability question remain retained. Exact code, independent
oracle, corruption controls, frozen inputs and replication are still required;
this method verdict authorizes no VM, no relaxed success predicate and no
campaign renewal.
