# INV-01-30 — findings review

- Reviewer: `/root/track01_independent_read`, independent retained context;
  authored neither the foundation implementation nor replication checker/method.
- Runtime author: `/root`; independent replicator and foundation-checker/pure-walker
  author: `/root/q34_sources`; full-Q30 oracle author: `/root/track01_session_check`.
  Root separately reviewed replication preparation; this retained reviewer
  independently reviewed runtime, full oracle and supervisor changes.
- Date: 2026-09-06 local (retained replica timestamps are 2026-09-07 UTC).
- **Current verdict: accept a scoped Q-01-30 feasibility resolution on the
  recorded M4 at two and four vCPUs, as specified in the final full-Q30 findings
  below.** Earlier foundation/source-method returns are preserved in sequence.

## Material and independence

Read the workflow, charter and investigation; retained the original pre-execution
review context through diagnostic returns and corrections. Reviewed primary
`q30-foundation-05` independently against raw RAM and logs before replication.
Inspected the separately authored checker, reviewed preparation, source freeze,
commands, resource bounds, stopping rules and three replication results.

All raw paths below are beneath `/Users/liaozping/orca/natemu-raw`:

| Evidence | vCPUs | Result |
| --- | --- | --- |
| `q30-foundation-05` | 2 | Primary raw inspection passed |
| `q30-foundation-replica-01` | 2 | Fresh independent rebuild/run passed |
| `q30-foundation-replica-02` | 2 | Fresh independent rebuild/run passed |
| `q30-foundation-replica-03` | 4 | Fresh independent rebuild/run passed |

Replicas freeze `q30-foundation-05/source`; source inventories and bytes match.
The oracle record is `q30-foundation-replication-oracle-01`, containing its
reviewed checker, preparation, exact checker argv/status, stdout/stderr, results
and checksums. Checker SHA256 is
`439c864749cf10c035f8414918d5e5e6147241e10dd0ed0cad0d0ff9646d10e9`.
Verified its retained hashes and reran the read-only checker separately on all
three replicas; all passed. Independently decoded every replica CPU's raw
completion, TSO, fault and IRQ-ring fields as an additional cross-check.
No VM was executed by this reviewer. Retained timestamps show sequential runs;
the replicator reports checking each before starting the next. Exactly three
replication VM invocations were submitted, with no failures or exclusions.

## Accepted scoped findings

**Observed:** the disposable EL1 foundation completes with its native mechanisms
together at two and four vCPUs on Apple M4 Mac16,10, macOS 26.6.2 build 25G83,
SDK 26.4. Placement is uncontrolled. The checked launch inherits unlimited host
CPU limits; build commands retain 60-second CPU limits. The host has a 10-second
watchdog and 30-second outer command bound with process-group cleanup.

The complete 16 MiB RAM dumps and 3824-byte shared records agree. Every CPU
records successful completion, no failure, one deliberate synchronous fault,
one cyclic SGI and three physical-timer IRQs. Independent checks cover:

- 4 KiB stage-1 table pointers and all 4096 RAM identity leaf descriptors;
  the native alias reaches the sentinel and its adjacent page is absent;
  GIC device descriptors use the submitted attributes.
- Own exception vectors recover the intentional read fault with EC=0x25,
  DFSC=7, FAR=0x60001000 and ELR exactly at the binary's fault-site symbol.
- Distinct redistributors have sleep bits cleared; each seven-event ring
  records alias, fault, SGI 1, three timer interrupts at INTID 30, and completion.
- ACTLR.EnTSO is set in both host and guest initial/final readbacks. MMU/cache
  bits and the exact submitted TCR agree with the raw state.
- Actual host exits are the entry HVC 0x32 and completion HVC 0x30, with matching
  instruction bytes and PCs. No virtual-timer exit appears in this physical-timer
  payload. Dumps complete without timeout; producer PASS booleans are not the
  independent oracle.

**Observed bring-up configuration:** queried maximum IPA size is 40 bits,
default and selected size 36 bits, and default/selected IPA granule is 16 KiB
(enum 1); host pages are 16 KiB. Guest EL2 is reported available and explicitly
disabled. These are distinct from the demonstrated guest stage-1 4 KiB granule.
The exercised layout uses RAM 0x40000000–0x40ffffff, shared state 0x40800000,
data 0x40900000, alias 0x60000000, GIC distributor 0x10000000 and redistributors
beginning 0x11000000. These choices establish a usable test layout, not a
binding product architecture.

## Alternatives, limitations and remaining work

The earlier negative runs remain valid diagnostic records. Guest allocation,
GIC omission, initial TSO clearing and alternate SCTLR did not individually
remedy the entry symptom. Omitting the host CPU-limit setter yielded entry;
the corrected launch then supported primary and independently replicated full
foundation runs. This accepts a working local launch path. It does not establish
the framework's internal cause or an OS-wide incompatibility with RLIMIT_CPU;
a controlled reversal of that dependency was not independently replicated.

Native alias/fault evidence is not x86 paging coherence. Full Q-01-30 still
requires actual x86-format tables and independent walk results, required update
visibility and permission/A-D semantics, completed SMP invalidation/shootdown,
concurrent activity, and pages ceasing to be page tables. Precise x86 exception
translation, asynchronous x86 interrupt semantics, host-produced code visibility
and stock-kernel secondary startup remain separate unaccepted questions.

TSO readback is configuration evidence, not an ordering litmus. Diagnostic
timestamps have no qualified performance or latency interpretation. Finite
2/4-vCPU native success does not establish arbitrary SMP scale, x86 correctness,
performance, economics or complete isolation properties. M2 Pro/Max replication
is unavailable and remains required before broader cross-generation claims.
No platform-wide viability or design kill follows from this review.

## Substantive blockers and disposition

No unresolved substantive blocker to the **finite foundation** finding remains.
Required independent replication and its preparation review are present. This
acceptance does not close the investigation, Q30 or Track 01. The next action is
to prepare a concrete bounded Q30 x86-semantic extension retaining all working
foundation mechanisms, then obtain independent pre-execution review. All prior
execution budgets are exhausted; this findings verdict authorizes no new VM run.

## Editorial edits and inspection limits

Created this findings thread only; no implementation or run artifact edits.
Inspected current material and retained current-run evidence, without archived
Track 01 material or git history. Future substantive contradictions reopen the
affected accepted finding and conclusions depending on it.

## Full Q30 source/method review — separate scope

- **Screening:** (a) QEMU TCG also models x86 translation and invalidation;
  same-ISA VMs supply those instruction semantics natively. (b) Native mapping
  and TLBI tools do not establish an x86 shadow protocol. (c) This review
  concerns the proposed eager-eviction EL1 shim, not every emulator design.
- **Reviewer and independence:** `/root/q34_sources`, delegated by `/root`,
  separately from this method's author `/root/retained_runner`. The reviewer
  authored the earlier foundation replication checker, but no part of this
  x86 paging proposal or implementation. The accepted foundation finding above
  is preserved and is not being reviewed anew.
- **Material:** the subsection “Full Q30 method proposal — x86 tables, native
  accesses and SMP coherence,” claims 07–10, visibility matrix, six mechanism
  steps, twelve case groups, fixture mask, controls and budgets. Independently
  read relevant Intel revision-092 text and cited Arm maintenance source;
  checked upstream Linux feature fallbacks for the scope boundary.
- **Date and initial verdict:** 2026-09-06 — **revise two method details**
  below. Source claims 07–09 are supported; claim 10 remains an unestablished
  candidate, not an accepted working protocol. No experimental code or VM run
  is approved by this review, and it cannot close Q30.

### Source findings and scope checks

[Intel Vol. 3A revision 092](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf)
§§5.5.2/5.5.4 and 5.6–5.8 support the walk formats, cumulative permissions,
NXE/WP conditions and distinct guest A/D maintenance. §§5.10.2.2–5.10.5 support
eager eviction, no persistent negative cache, qualified relaxation faults,
optional A/D-clear invalidation, nonleaf-cache hazards, page-size transitions
and completed remote invalidation before reuse. The proposal preserves these
qualifications. Required local INVLPG/CR3/PGE effects cannot be replaced by an
early notification. Speculative A updates and delayed invalidation prevent
unqualified zero-bit or immediate-new-only oracles. Sections 11.1.1/11.1.2.1
support atomic aligned scalar/byte operations and locked A/D updates; this
does not make arbitrary multi-access instructions atomic.

[Intel Vol. 2A, CMPXCHG pp. 3-194–3-195](https://cdrdv2-public.intel.com/922480/253666-092-sdm-vol-2a.pdf)
supplies the missing method detail in blocker 2: comparison failure writes
back the old destination, updates the accumulator and sets comparison flags.
It is not a read-only instruction because its new memory value equals its old
value. The fixture needs write-permission and mapping-leaf dirty-bit behavior
in both comparison outcomes, separate from the targeted PTE's value.

[Linux v6.12 arm64 `tlbflush.h`](https://raw.githubusercontent.com/torvalds/linux/v6.12/arch/arm64/include/asm/tlbflush.h)
has the stated ISHST/TLBI/ISH/ISB sequence in `flush_tlb_all`, and distinguishes
local flushing, last-level invalidation and walk-cache flushing. This supports
the implementation-example claim only. Concrete descriptor transitions,
shareability, break-before-make and per-target synchronization still require
their own code argument. In particular, quiescence acknowledgement and final
invalidation completion are different protocol phases.

The PCIDE=0/PDPE1 GB=0 fixture is a legitimate limited CPU envelope, not an
implicit prohibition on modern Linux. Upstream
[v6.8 `tlb.c`](https://raw.githubusercontent.com/torvalds/linux/v6.8/arch/x86/mm/tlb.c)
`choose_new_asid` supplies a no-PCID flush path; `build_cr3` gates PCID use on
the CPU feature.
[v6.8 `init.c`](https://raw.githubusercontent.com/torvalds/linux/v6.8/arch/x86/mm/init.c)
gates the 1 GiB page-size mask on `direct_gbpages` and `X86_FEATURE_GBPAGES`.
These paths support treating the features as optional for this initial method;
they do not prove exact stock-package boot or every distribution configuration.
The proposal explicitly routes broader coverage to existing owners. Its required
low/high canonical native accesses cannot be replaced with Python walks:
unavailable native addresses remain missing coverage, not a full Q30 result.

### Concurrency and oracle checks

The proposed global lock can serialize the mechanism without serializing every
test input. Independent native reader/writer loops and recorded overlap are
necessary. A faulting or lock-waiting peer must participate in quiescence before
waiting for the writer, and native IRQ handlers must not wait for an interrupted
lock owner. Those deadlock constraints are stated but remain to be implemented.
The concrete review must follow every lock/IRQ/rendezvous path and ensure that
the coordinator cannot install late walker results or acknowledge an epoch
before all relevant targets have completed invalidation.

Physical page roles, aliases and levels must be derived from all active roots
and in-flight uses. Dormant-root activation needs rediscovery before use;
shared/recursive references must retain tracking until their last use ends.
Empty child tables and pre-existing writable aliases are meaningful controls.
Permanent historical table classification would fail the retirement obligation.

The separate raw-table decoder, mutation log and epoch/operation traces can
support an independent oracle. Final RAM alone cannot reconstruct concurrent
reads. Separate instructions are not an atomic snapshot; precise race intervals
and allowed result sets must be enumerated. Native data effects and fetch
permissions are distinct tests. Q31 still owns actual x86 frame delivery;
recording fault classification here must not be promoted into that result.

All twelve groups address relevant Q30 obligations. Finite repeated cases,
observed overlap, negative controls and retained failures are appropriate
preparation. Exact guest/RMW state, A/D linearization, native high-VA layout,
privilege representation, pool limits and concurrent decoder code are explicitly
pending. None is waived by the accepted native foundation or this source review.

### Substantive returns for this scope

1. **One write commit point.** Step 2 currently says to perform the entitled
   update, before step 3's lock/quiescence, while step 4 applies it again.
   Literal execution can mutate twice or use stale decoded memory/rights.
   Smallest remedy: step 2 captures/decodes and provisionally checks only;
   step 4 revalidates current access and destination under lock after quiescence,
   then performs the sole atomic decision and once-only architectural commit.
   Closure: inspect the corrected sequence and exactly-once statement.
2. **Complete CMPXCHG outcome oracle.** “Failed CMPXCHG cannot change the
   entry” constrains resulting bytes but omits writeback semantics, write
   permission, mapping D, accumulator and CF/PF/AF/ZF/SF/OF. An emulation that
   treats mismatch as a read could pass that incomplete oracle. Smallest remedy:
   cite the instruction contract and require success/mismatch register/flags,
   write-access/D controls, including a mismatch against read-only memory that
   faults. Distinguish mapping A/D changes from the targeted entry's value.
   Closure: inspect these method/case refinements; no VM experiment is needed
   to close the source-method return.

Only this review thread was edited. Primary reading stayed within the delegated
30-minute budget. No archived material, experiment implementation or VM was
accessed. Both narrow returns were sent to the retained method author; no
other substantive blocker identified for scoped source/method acceptance.

### Full Q30 return 1 — scoped acceptance

Same independent reviewer, 2026-09-06. Inspected corrected mechanism steps 2
and 4, new source claim `CLM-01-30-11`, and expanded case group 7.

- **Return 1 closed:** step 2 now only captures/decodes and provisionally checks;
  step 4 revalidates access/translation/current destination after lock and
  quiescence, owns the sole atomic decision/write and commits register/flag
  results once. An access denial commits no instruction result.
- **Return 2 closed:** claim 11 cites the checked CMPXCHG instruction contract;
  the method and case explicitly cover equality/mismatch, writeback, RAX and
  CF/PF/AF/ZF/SF/OF. Fresh A/D mapping entries are separate from the targeted
  entry, and a read-only mismatch must fault as a write rather than succeed
  as a read. These refinements close the incomplete oracle.

**Updated verdict: accept scoped source claims 07–09 and 11, and the full-Q30
method preparation.** Claim 10 is accepted only as an unestablished candidate
inference to investigate, not a working coherence protocol. No substantive
source/method blocker remains. The accepted native foundation above is
unchanged; full Q30 still requires concrete code/fixture/independent-oracle
review, runtime evidence and independently reviewed replication. This source
verdict authorizes no VM execution and does not establish the proposed layout,
ASID scheme, lock/rendezvous, A/D or retirement implementation.

## Full Q30 runtime findings — scoped resolution

Reviewer `/root/track01_independent_read`, retained independent context,
2026-09-06 local / 2026-09-07 UTC. This reviewer authored no runtime, oracle or
supervisor implementation and executed no VM. The review covers the concrete
code/fixture and 45 adversarial oracle controls, bounded execution and failures,
corrected primary results, independent replication and the submitted Q30 breadth
check. It does not reopen the accepted foundation/source contracts above.

**Screening:** QEMU TCG also implements x86 paging semantics, while a same-ISA
VM supplies them natively. Documented Arm tables, invalidation and Apple VM
controls do not establish this x86-shadow composition. The finding applies to
the reviewed EL1 shim with global writer serialization, quiescence, eager native
shadow eviction, explicit x86 A/D updates and tracked-page recovery.

### Evidence and independent replication

All paths are beneath `/Users/liaozping/orca/natemu-raw`. Every row is a fresh
rebuild from `q30-paging-build-04/source`, with all earlier foundation mechanisms
enabled. Each complete frozen oracle-02 check passed with no unsupported effects
before the next VM started.

| Evidence | Role | vCPUs | Operations | Records | Final epoch |
| --- | --- | --- | --- | --- | --- |
| `q30-paging-02` | Primary | 2 | 28,992 | 354,279 | 15,171 |
| `q30-paging-03` | Primary | 2 | 28,992 | 354,291 | 15,176 |
| `q30-paging-04` | Primary | 2 | 28,992 | 354,557 | 15,166 |
| `q30-paging-05` | Primary | 4 | 59,552 | 726,351 | 27,214 |
| `q30-paging-replica-01` | Independent replica | 2 | 28,992 | 354,729 | 15,187 |
| `q30-paging-replica-02` | Independent replica | 4 | 59,552 | 725,795 | 27,229 |

Each run completes all 864 instances: 54 variants across twelve groups, sixteen
repetitions each. Corresponding `q30-paging-check-02` through `-05` and
`q30-paging-replica-check-01`/`-02` retain the complete checks. Independently
verified exact inventories, all 63 producer and eight check hashes per pair,
frozen source identity, command outcomes and VM/check associations. The full
checker is independently authored/reviewed but shared between primaries and
replicas; replication does not claim a second independent full oracle.

The reviewed checker is frozen in `q30-paging-oracle-02` at
`cba2fd774b69947559996cbce94c001562bb4947ab60fd1ee2f0bf7ee46ec07c`;
its supervisor is `q30-paging-supervisor-01` at
`2953d19e8ea4a85e45f2e0c2df1f6b543654b19225923fdff2aecba96c1e2bfd`.
Both replicas obeyed their separately reviewed preparation and sequential
admission rules. No corrected-primary or replica failure, exclusion or retry
occurred; their finite invocation budgets are consumed.

Independently decoded raw final states and additional trace properties. All
CPUs are complete, inactive, not waiting and ACKed to their final epoch. The
four-CPU replica has distinct native roots/ASIDs 1–4, 59,552 complete operations,
29,013 native-map records and complete case/active-interval pairing. All 256
group-7 atomic requests overlap a peer-native interval, 128 through each alias.
Each CPU has eighty complete group-8 loop intervals and actual within-loop
timer growth. Its separate full Q30/shared dumps equal their RAM slices.
These checks supplement the full semantic oracle rather than producer booleans.

### Accepted finding and applicability

**Observed:** on Apple M4 Mac16,10, macOS 26.6.2 (25G83), SDK 26.4, with
uncontrolled placement, this disposable EL1 implementation successfully honors
the exercised x86 paging obligations at two and four vCPUs:

- Actual native loads/stores/fetches use 4-KiB shadow leaves derived from real
  x86-format four-level tables, including low/high canonical boundaries,
  cumulative R/W/U/S/NX rules, CPL/WP/NXE/PGE contexts, 4-KiB/2-MiB transitions
  and the declared unsupported/reserved cases. Successful A/D and both locked
  CMPXCHG outcomes have their required byte, accumulator and flag effects.
- Required post-invalidation/shootdown accesses observe the new translations
  or permissions. Actual native reads and fetches, old/new physical bytes,
  completed gate/ACK sequences and final reachable mappings agree with the
  independent contract-derived oracle. Eager eviction supplies a stronger
  visibility policy where x86 permits it; no claim requires stale translations
  to persist before a guest invalidate.
- Competing same/disjoint leaf, nonleaf, root and R/W/A/D updates execute in
  free-running group-8 loops, with observed native overlap and faulting,
  writer-waiting peer acknowledgements. Both aliases receive real peer activity
  alongside atomic requests. The quiescence mechanism completes with timer
  service continuing; it does not establish every possible interleaving.
- Last-reference retirement removes table classification. Changed ordinary
  data writes through one retired alias are read through the other without
  table-write trapping, and relinking restores tracking. Shared, empty,
  dormant-root and recursive/multilevel roles receive their declared treatment.

The accepted native foundation remains enabled: own exception vectors and
4-KiB stage-1 mappings, virtual GIC/SGIs, per-vCPU physical timer and TSO control.
Host and guest readbacks retain TSO=2, and actual entry/completion HVCs establish
execution. Replica timer counts advance from 3 to 16 at two CPUs and 3 to 38
at four; no host virtual-timer exits occur in this physical-timer fixture.
This is configuration/bring-up evidence, not an x86 ordering litmus or interrupt
injection finding.

**Inferred scoped resolution:** this constructive, independently replicated
result supports **yes for Q-01-30's mechanism feasibility on this host and
finite profile**. The candidate in CLM-01-30-10 now has observed support within
that boundary. No substantive blocker remains to this scoped resolution.
It establishes neither arbitrary x86 correctness nor selection of the expedient
global lock/eviction mechanism as the product architecture.

### Negative evidence, breadth and remaining owners

All earlier native-entry failures remain retained with their previously reviewed
local-launch interpretation. Primary-01 exposed missing atomic/peer coverage;
it was not accepted or excluded. Build-04 changed only fixture scheduling and
had to demonstrate actual overlap. The first diagnostic check remains INCOMPLETE.
Its independently reviewed oracle correction distinguishes validated serialized
GUEST writes from unlogged native effects; malformed commits and wrong reads
still reject, and genuine unmodeled ordinary-data interleavings stay INCOMPLETE.
Oracle-02 rechecked the identical diagnostic RAM before fresh primary and replica
runs. This corrects a demonstrated classifier limitation without replacing the
sample or weakening coverage.

Accepted applicability is four-level, 48-bit canonical guest addressing with
36-bit physical addresses, 4 MiB of fixture guest RAM, ordinary WB attributes,
4-KiB/2-MiB pages and the selected scalar/byte/CMPXCHG forms. Optional PCID,
1-GiB pages and additional permission/ISA features are outside this profile.
Handwritten native blocks and logical x86 privilege contexts are not a translator
or stock Linux boot. Wider CPU/ISA/fault coverage stays with Q-02-01/Q-02-03;
translator recovery metadata with Q-01-31/Q-03-02. Q31–34 remain unaccepted.

The submitted product/architecture/correctness/methodology breadth check is
adequate for this question. Cost and scaling remain Q-04-03–05, mechanism
selection Q-03-04, ordering/stress Q-02-02, isolation Q-02-04 and wider-host
coverage Q-05-02. M2 Pro/Max is unavailable; no cross-generation or platform-wide
claim is accepted. Placement, arbitrary SMP scale, clock qualification,
performance and economics remain unresolved. The current backlog has these
owners; this review found no additional question needing a new identifier.

**Disposition:** accept the scoped Q30 resolution and preserve this working
build as the base for the next reviewed question, retaining every established
mechanism. This findings verdict authorizes no additional VM run or product
implementation. Track 01 remains open until Q31–34 and its final breadth check
are resolved. Later contrary evidence reopens the affected accepted claim.
