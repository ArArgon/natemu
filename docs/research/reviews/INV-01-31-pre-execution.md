# INV-01-31 — pre-execution review

**Current verdict: build 03 accepted for the bounded execution campaign, 2026-09-07.**
See the final retained-input verdict below; earlier pending entries preserve
the review history. This is not an observed-result acceptance.

- **Screening first:** (a) QEMU TCG also reconstructs guest state, but an ordinary
  same-ISA VM already executes the guest instruction semantics. The experiment
  concerns the cross-ISA shim's combination of native fault recovery, shadow
  classification and x86 delivery, including simultaneous faults. (b) Intel's
  delivery/restart contracts and Arm's native exception state establish the
  obligations; neither documents their successful composition in this shim.
  (c) Findings apply to the thin EL1 shim and the explicitly tested handwritten
  translator-shaped blocks with their metadata contract, not all translators.
- **Reviewer/context:** `/root/track01_independent_read`, independent of Q31
  runtime, fixture, host/runner and oracle authors. Retained independent Q30
  pre-execution and findings reviewer; authored no load-bearing experiment or
  checker change. This thread retains that role for Q31 returns.
- **Material inspected:** current workflow and feasibility charter;
  `INV-01-31.md` including claims 01–10, all twelve case groups, revised eleven
  case-9 variants and the integration seam; the specific case-9 source-review
  return and closure in `INV-01-31-findings.md`; current `guest.c` exception and
  terminal paths, `q30.c:q30_run`, and the in-progress host checkpoint path.
- **Date/status:** 2026-09-07 — **preparation review pending**. The question
  passes screening and the proposed integration is suitable for concrete code
  preparation. This is not an accept verdict or VM execution authorization.
  The submitted bounded preparation is 04:05–05:35 UTC, with no VM execution.

## Integration and source assumptions to preserve

The checkpoint belongs after every CPU completes the Q30 cases and their final
barrier, before `guest_main` masks IRQs and writes terminal foundation state.
Its RAM image is a nonterminal observation: Q30's per-CPU `done=1` marks the
completed prefix, while foundation `done`/`tso_end` and the final trace remain
unset. An explicit new format must validate that state directly. No fabricated
terminal fields or reset/rebootstrap of the working paging mechanism is an
acceptable substitute. All CPUs must remain stopped for the complete snapshot,
then resume the same VM with its tables, role tracking, epochs, timers, GIC and
TSO state. The full oracle must also replay paging activity after the checkpoint,
through the final Q31 state; a correct prefix alone cannot certify integration.
Checkpoint dump time is part of the declared whole-run watchdog budget, and its
failure or an incomplete rendezvous must terminate without continuation.

The accepted case-9 source preparation fixes concrete architectural expectations,
not an implementation. In particular, 9h's supervisor descriptor read at
`V+0x11008` must fail before segment/stack transfer. The earlier proposed
stack-push rollback oracle was returned and is not restored by reusing a generic
fault-restart helper. Descriptor, TSS, frame and IRET memory accesses must use
Q30 translation with the specified access privilege; a delivered-result tuple
or direct physical-pointer frame construction would not exercise the claim.
The #DF cases deliberately assert no original-RIP restart or defined saved
program state. The nested handler case instead preserves an actual executed
handler context and independently saved CR2.

The default x86 profile remains the finite source-reviewed one: 64-bit four-level
paging, NXE and WP enabled, specified CPL0/3 contexts, and the listed optional
features disabled. Native execution remains EL1; logical x86 privilege must
therefore be supplied consistently to every access and delivery substep. Arm
syndrome bits do not supply the x86 error code or restore speculative guest
register/flag state. The metadata and actual saved native frame must connect the
faulting instruction/substep to its guest attempt, restart state and commit
boundary. The existing `q30_access` status is an input to that mechanism, not
itself guest exception delivery.

Code-page writes require actual changed guest bytes, byte/generation-selected
successors and the stated synchronization, while retained old metadata remains
usable for an overlapping old block's fault. Tracking-only writes must remain
separate from guest #PF delivery and from Q30 page-table writes. This scoped
scenario does not establish Q33 host code publication or a general translator.

## Substantive blockers

None identified in the integration proposal alone. This is an interim review;
missing completed artifacts are pending work, not defects. Concrete execution
readiness still requires the actual twelve groups and eleven case-9 variants,
metadata/native-frame implementation, independent full and checkpoint oracle,
negative controls, exact operation/event/file bounds and reviewed runtime budget.
Those artifacts have not yet been submitted for a final pre-execution verdict.

## Editorial edits and limits

Created only this review thread. No experiment, source finding, oracle, raw run
or archived history was changed or executed. The current host checkpoint is
in progress; inspection here does not freeze or accept its final implementation.
The accepted Q30 build-04/oracle-02 evidence remains a separate immutable base.

## Returns

The next return will review the concrete complete implementation and its retained
compile-only snapshot before any VM execution can be accepted.

### Separate independent review context — 2026-09-07

**Screening first:** (a) QEMU TCG shares guest-state reconstruction, while an
ordinary same-ISA VM obtains instruction semantics natively; the question is
the cross-ISA composition with shadow paging. (b) The documented Intel/Arm
contracts establish the required behavior, not that composition's working
implementation. (c) This review applies to the thin EL1 shim and the finite
handwritten blocks and metadata tested here. The question passes screening.

`/root/track01_understanding_check` is a newly assigned independent reviewer in
a separate recorded context. The previous reviewer is not available in the
current live agent tree. This reviewer has authored no producer, fixture,
native recovery, or oracle code; the only owned file is this review thread.
Read the workflow, charter, complete Q31 investigation and source-review
returns, then the current guest/assembly, Q30 paging path, Q31 header,
host/runner, budget check and independent Q31 oracle preparation.

**Status: preparation review pending.** Missing in-progress implementation
is pending work, not a defect. No complete code submission or retained build
has yet been inspected in this context; no execution is accepted. The same
04:05–05:35 UTC preparation bound remains in force. In particular, the
cumulative successful-path log bound must include Q31: the existing Q30
bound leaves 173,101 records of its 2,097,152 capacity. Frame accesses,
coherence epochs and new state snapshots cannot be budgeted from observed
prefix sparsity alone. This is an implementation-review prerequisite, not a
finding that the completed candidate exceeds its eventual bound.

No VM, experiment, archived history, or load-bearing code was changed or
executed in this pass. A concrete submission will receive a return below.

### Parent paging/integration core — interim return

`/root/track01_understanding_check`, 2026-09-07: inspected the submitted v3
changes in `q30.c`, `q30.h`, `q31.h`, `guest.c` and `guest.S`. The original
screening and independence above still apply. **No substantive defect found
in this core subset; complete pre-execution review remains pending.**

The byte-fetch helper checks logical x86 execute permissions under the writer
lock even if an Arm-readable shadow already exists. It restores the active
handshake before releasing that lock. The split-access path retains its
prevalidation-to-native-completion exclusion and now records the denied
second page's first byte. Native exception recognition constrains helper PC,
request kind/width, exception class and read/write direction before accepting
the fault; unexpected sites or classes remain internal failures. The proposed
Q31 validation hook itself is not yet reviewed.

Code-role changes use the existing exclusive change/eviction protocol. Guest
writes to either tracked role, including mixed code/table pages and their A/D
updates, advance code generation under that protocol. Aggregate user counts
and per-CPU ownership prevent role removal while pinned. A pin retains that
role/user lifetime; it does **not** freeze guest bytes, stop generation changes,
or supply an immutable metadata snapshot. Correct generation/byte selection
and retaining the old recovery metadata remain concrete Q31 callsite
obligations, awaiting its submission. The guest HVC is after the Q30 final
barrier and before terminal masking; continuation calls Q31 in the same VM.

If the eventual cumulative bound requires 512 MiB RAM, that exact size ends at
`ALIAS_VA=0x60000000` and the current table loop stops before its alias slot.
A larger size would collide with that slot. Enlarging RAM still changes the
foundation's identity-map extent and retained checkpoint layout: independently
check the enlarged table coverage, guest image's 2 MiB limit, explicit format
and file limits, combined host/checker memory budget and checkpoint I/O within
the whole-run watchdog. This is conditional review guidance, not approval of
an unsubmitted capacity or relaxed deadline. No VM or synthetic experiment was
run by this reviewer; producer reports its strict compile checks separately.

### Full producer and cumulative bound — review in progress

`/root/track01_understanding_check`, 2026-09-07: read all 34 variant bodies in
`q31.c`, the synthetic `check_cases.c` substitutions, the expanded host/runner,
the independent oracle's current metadata/code replay, and `run_check.py`.
The oracle still explicitly refuses raw acceptance while its remaining
architectural checks are being authored. This is **not a final verdict**.

Independently ran only `python3 experiments/track01/check_budget.py`: the
then-current hash-guarded arithmetic passes with 5,128,019 records against
5,242,880 capacity. Its per-variant access ceilings, extra split-operation
charges, 256 direct events per CPU-case, context/invalidate allowances and
96-write setup ceiling cover the inspected finite branches. The starting
root retirement and four new four-page roots fit the initial-role allowance.
This establishes a manual successful-path bound, not runtime timing or a proof
against defective loops. Subsequent producer changes require recounting.
The submitted layout is now 512 MiB RAM with a 1 GiB producer per-file limit;
the v3 foundation oracle checks the extended identity map. The checker
supervisor selects a 1,800-second wall budget and samples its 6 GiB process-group
RSS ceiling once per second; its output-file ceiling remains 256 MiB.

Two code-scenario coverage issues were returned to the producer author:

1. The initially inspected remote-writer case pinned old metadata across a
   write/fault region, but did not order any reader fault after the writer's
   generation change. A schedule running the writer last could avoid the
   recovery-after-invalidation requirement. Force that ordering for a declared
   control or make independent coverage reject its absence; retain the old pin
   through delivery/restart. Closure needs a trace-checkable generation change
   preceding an old-generation fault, not merely an overlapping pin interval.
2. The initially inspected local/remote synchronization boundary was only
   `q30_invalidate(..., 0x33)` and a harness barrier. Identify and record the
   modeled guest jump/serialization and remote publication sequence, with the
   ensuing byte/generation-selected dispatch. An internal page invalidation
   alone does not identify which x86 synchronization requirement was exercised.

The author is addressing these alongside complete retries. Their final code,
updated bound, independent coverage checks and retained compile-only submission
have not yet received closure here. Synthetic fixture PASS uses serialized
ordinary memory and the x86 walker, substitutes timer progress, and bypasses
native Arm fault hooks and the Q30 cache/TLB protocol; it is preparation only.

### Code-scenario returns closed for the producer

`/root/track01_understanding_check`, 2026-09-07: independently inspected the
revised `code_case`, `serialize_code`, `cross_case` and fixed CPUID bytes.
**Both producer-side returns above are closed.** Oracle coverage and final
submission review remain pending; this is not execution acceptance.

The remote writer now completes its tracked store before a rendezvous releases
readers to fault. Those readers keep their old-generation pins through actual
delivery, mapping repair, IRET and a new attempt that completes the original
MOV. They release the pin only afterwards. Thus a successful declared path
cannot put all old-block faults before the generation change. The split-store
race also now performs an actual retry after peers finish their revocations.

The code boundary fetches fixed `0F A2` bytes from a separate unchanged page,
models the CPUID leaf-0 instruction and commits its explicit outputs before
fresh-generation dispatch. Its writer-lock/quiescence path completes previous
native accesses and evicts stale shadow mappings. The readiness rendezvous is
the finite test's publication control; it is not evidence for arbitrary guest
ordering. No writer runs during either declared byte-selection interval.

Independently checked retained Intel revision 092 primary text: [Vol. 2A,
CPUID, p. 3-203](https://cdrdv2-public.intel.com/922480/253666-092-sdm-vol-2a.pdf)
supports the opcode, serialization, four output registers, unchanged flags and
cleared high register halves; [Vol. 3A §§11.1.3 and
11.3](https://cdrdv2-public.intel.com/922487/253668-092-sdm-vol-3a.pdf) explicitly
permit CPUID for local code modification and require publication plus
executing-CPU serialization for cross modification. Publisher opens failed;
this pass used the retained primary text, not a new independent download.
The chosen `NatemuProbe!` vendor/leaf output is a declared finite fixture, not
a product CPUID/feature contract or stock-kernel compatibility finding.

The updated manual bound raises the per-case invalidation allowance to ten:
four competing rounds can each need a fault repair and a forced restoration,
plus the final restore. Together with the retained access/split/direct-event
allowances, the arithmetic is 5,167,187 of 5,242,880 records, leaving 75,693.
The source hash guard must be refreshed by the author after this inspected
change and checked in the final retained compile-only snapshot; this reviewer
did not replace a producer hash or change any producer/oracle source.

### Oracle readiness returns — in progress

The same independent reviewer returned these concrete checks to the oracle
author while its raw acceptance mode remained disabled:

1. `concurrent_semantics` initially required native Arm faults on both a split
   store and its competing page-table writer. Q30 prevalidates a split store:
   denied operations skip native execution, while permitted operations install
   both mappings and pin them. Requiring a native fault on that store made the
   declared coverage impossible. Require its operation/validation interval to
   overlap a native trapped revocation of that same CPU's mapping; do not count
   another writer targeting an unrelated third CPU. The author has revised the
   predicate; focused positive/negative control review is still pending.
2. `delivery_semantics` initially adopted each successive fault snapshot as
   its entry baseline after checking only CR2. For 9h this could bless an
   invented architectural state change during the early descriptor read, then
   accept a frame consistent with that wrong baseline. Require the original
   pre-transfer state across the contributory-to-page-fault transition, with
   only the specified CR2 update, and the original-context return/new #GP
   attempt. A control changing the second snapshot and its frame consistently
   must still fail. This is the accepted source return's substantive invariant.
3. Bind the modeled CPUID to its actual completed quiescence/invalidation
   epoch inside the attempt before retirement. Opcode bytes, vendor registers
   and placement between pins alone could survive deleting the serialization
   operation. A missing or misplaced `0x33` invalidation/epoch must fail.

These are execution-readiness returns, not observed guest failures. They must
be closed in the final oracle review; their discovery does not reopen the
separate accepted source findings or the producer-side code-scenario fixes.

### Further producer controls inspected

`/root/track01_understanding_check`, 2026-09-07: inspected the subsequent
case-12 stale-pin control and case-6 boundary-word observations. **No new
producer defect found.** Both negative metadata variants now finish with a
new successful MOV. The generation variant additionally performs a tracked
code write, calls the actual pin API with the old generation and requires
rejection without changing users, then accepts/releases a current pin.

Split-store checks read full boundary words through stable aliases to the two
physical data pages before the attempt, immediately after a denial and before
delivery/repair, and after a committed retry. The expected byte merges match
the eight-byte value at page offset 4092; untouched neighboring halves remain
checked. The caller retains the original denied `access_result`, so successful
observation reads cannot replace the exception's status/error/address. Peer
revocations target the original second-page mapping, not the observation alias.

The synthetic harness now invokes the real Q31 metadata predicate with each
access's expected helper PC and rejects unknown PC 1. It still substitutes
the memory/protocol implementation and establishes no native-fault result.
Independently reran the refreshed source-guarded budget: **PASS, 5,167,187 /
5,242,880 records**, with these additions inside the declared allowances.
Producer compile-only build 01 is superseded; final review must inspect build
02's retained sources and build inputs. Full oracle readiness remains pending.

### Build 02 retained-input check

The reviewer independently verified all 77 inventory checksums in
`/Users/liaozping/orca/natemu-raw/q31-faults-build-02`, its 18 passed build
commands and six passed preparation checks. All inspected producer sources
match that retained source directory. `guest.bin` is 1,179,648 bytes, within
the 2 MiB reservation; the manifest is explicitly build-only and no VM host
invocation occurred. Build 01 remains preserved and superseded.

The reviewer also ran the current independent oracle's `--model-trace` mode
against build 02's retained `q31-model-4.jsonl`: 544 cases and 49,960 modeled
operations passed, with 17 semantic corruptions rejected, including partial
split-store bytes, forged 9h pre-transfer state and missing valid follow-up.
That mode explicitly omits native paging/ACK/A-D replay, actual concurrency
and the raw envelope. The later code/concurrency controls and final retained
oracle still require review before a complete pre-execution verdict.

### Final retained-input pre-execution verdict — accepted

`/root/track01_understanding_check`, 2026-09-07, independent of producer and
oracle authors: **accept pre-execution** for the concrete inputs below under
the recorded finite campaign. No VM was executed by this reviewer or in the
preparation evidence. This accepts execution readiness, not INV-01-31's result,
a general translator, or Track 01 completion.

The screening at the start of this thread still applies: QEMU has the shared
state-reconstruction problem, ordinary same-ISA virtualization supplies native
guest semantics, and the documented Intel/Arm obligations do not establish
this shim's composition. The proposed native experiment remains necessary for
the explicitly scoped hand-block/metadata design on M4 with 2/4 vCPUs.

Concrete retained inputs, all under `/Users/liaozping/orca/natemu-raw`:

- `q31-faults-build-02`: all 77 inventory checksums independently verified;
  18 build commands and six preparation checks passed. Reviewed `q31.c` SHA256
  is `5cbb2403950a0e09499c737d9381a256cf39010f894d3ac41270a84fcac5bb50`.
- `q31-faults-oracle-01`: all 11 inventory checksums independently verified,
  inventory complete and four source files byte-equal to the reviewed/tested
  workspace. `check_q31.py` SHA256 is
  `dd0318fd47041274651cef0c816bf1a9785d65c018e3b6090f9c584882c7e033`;
  `check_q30.py` is
  `7c3edd6200a23a7c68421a7c9bc166e77a6288991d7e4c47c598aa9b0562204a`.
- `q31-faults-supervisor-01`: all four inventory checksums independently
  verified, source equal to the reviewed workspace, retained supervisor
  self-check passed. `run_check.py` SHA256 is
  `c6db5527abbe77540a1c3a1ef6491881d457373865d2ca1662b636233465ef1c`.

All three oracle readiness returns are closed. The shared concurrency
predicate accepts a prevalidated split request with no native fault when its
interval overlaps the correct peer's trapped revocation; missing overlap,
missing writer trap and an unrelated third-CPU target fail. The 9h early
fault path preserves the original defined architectural state except CR2;
a consistently forged second snapshot/frame fails. Undefined pending-#DF
state is excluded from that comparison. CPUID retirement now requires its
actual target/reason-0x33 invalidation and completed quiescence epoch between
opcode fetch and commit; removing either fails. The full Q30 replay checks
ACK/quiescence and prohibits native entry after an exclusive code-role or
generation effect until gate completion.

Independently ran final Q31 and Q30 self-checks and build 02's model trace:
PASS, 544 cases / 49,960 modeled operations, 28 semantic corruptions rejected;
the Q30 regression rejects 45 corruptions. The final mixed table/code control
was also inspected: one actual guest mutation must count both a table trap
and code write, and deleting its generation event fails full synthetic
replay. Retained check outputs match these results. Synthetic checks do not
establish native faults, cache/TLB ordering, real concurrency or raw-run
acceptance; those remain the purpose of the VM campaign. The enabled raw
acceptance path checks the preserved Q30 checkpoint and full event replay,
foundation state, native fault symbols/classification, Q31 architectural and
code semantics, coverage, and retained input/output envelope.

The proposed execution limits are acceptable: a recorded 270-minute primary
campaign, at most six VM invocations (2/2/2/4-vCPU primaries and at most two
diagnostics), at most eight full checks including same-RAM rechecks, a
45-minute admission reservation for each new VM/check pair and 35 minutes
for a same-RAM recheck. The separately recorded independent replication may
use at most two VMs and two checks in 100 minutes with the same 45-minute
admission reservation, after its independent preparation context is recorded.
Neither campaign silently renews its deadline. Stop on the first unexplained
failure or missing control; changed code or run method requires renewed
independent review before further execution.

The retained per-run ceilings remain 10-second VM watchdog, 30-second producer
children, 512 MiB guest RAM, 1 GiB producer output files, and the inspected
5,167,187-record bound below capacity 5,242,880. Full checks have 1,800 seconds,
a 6 GiB observed process-group RSS ceiling sampled each second, and 256 MiB
output files. The recorded 16 GiB disk reserve and finite command-count
admission arithmetic cover the proposed campaign. This review establishes no
performance claim or second-Apple-Silicon-generation result. Observed findings
and independent replication still require their own review before resolution.

### Build 03 ordering refinement — accepted, supersedes build 02

`/root/track01_understanding_check`, 2026-09-07: independently inspected the
complete retained-source difference from build 02. Only `q31.c` and its
`check_budget.py` source hash changed. In group 6 variant 2, the unchanged pair
of baseline observations now precedes the peer PTE write; that write is
followed directly by the own-CPU split attempt. The baseline remains valid:
peers change page-table entries, while only the owning CPU changes these C/D
data bytes. Immediate denied-store and committed-store observations remain
intact. This removes avoidable helper work from the intended contention
window without adding operations or changing the required overlap criterion.
It does not itself establish that overlap will occur on hardware.

**Accept build 03 in place of build 02**, with the unchanged oracle 01,
supervisor 01, scope and campaign limits in the preceding verdict. The
concrete producer directory is
`/Users/liaozping/orca/natemu-raw/q31-faults-build-03`; `q31.c` SHA256 is
`ef41873a618e2457d07b638fae21f4b38e0d1ad64dfbac672afd763da5448a5e`.
All 77 retained inventory checksums and inventory completeness were
independently verified, along with 18 passed build commands, six passed
preparation checks and changed-source equality. The refreshed static budget
independently passes unchanged at 5,167,187 / 5,242,880 records.

The reviewer ran the frozen oracle 01 against build 03's retained model:
PASS, 544 cases / 49,840 modeled operations, all 28 corruptions rejected.
The varying operation count reflects bounded modeled race outcomes. Build 02
remains preserved and superseded. This was a preparation refinement; no VM
had started, and no native-concurrency or observed-result claim is made.

### Independent replica preparation — accepted

`/root/track01_understanding_check`, 2026-09-07: **accept the independent
replica preparation** at
`/Users/liaozping/orca/natemu-raw/q31-faults-replica-prep-01` for the already
reviewed separate replication method. The preparing agent is
`/root/q31_independent_replication`, whose retained context states that it
neither authored producer/oracle/supervisor changes nor supplied primary-run
results. This review concerns its preparation only; it does not review any
primary findings or accept a replication result.

Independently verified all 88 inventory entries and complete inventory;
`SHA256SUMS` SHA256 is
`135b7b79fce9b48324f43315de2f92f7c72b36b8e35dcb8a73974c3425bd151c`.
All 15 sources equal accepted build 03, and the freshly compiled host,
`guest.bin` and six object files equal build 03 byte for byte. Rechecked the
accepted build 03 / oracle 01 / supervisor 01 inventories and their recorded
identities. The fresh build manifest has 18 passed commands and explicitly
sets build-only. Strict model compilation and all nine corrected preparation
checks passed, including the frozen oracle's 544-case / 49,920-operation
model replay with 28 rejected corruptions and Q30's 45 negative controls.

Two preparation mistakes are retained, diagnosed and adequately corrected:
Python 3.9 lacks `hashlib.file_digest`, so read-only hashing was repeated with
`sha256`; a wrapper's 256 MiB hard file limit prevented the unchanged runner
from installing its documented 1 GiB child limit. The original traceback is
preserved, and repeating with the accepted 1 GiB wrapper ceiling passed. The
latter corrects the preparation wrapper to the reviewed producer limit; it
does not raise the independent oracle's 256 MiB output ceiling or alter any
producer, oracle or supervisor source. No unexplained preparation failure
remains.

Preparation completed at 05:29:50 UTC with no VM execution. Replication may
proceed only within its separately recorded activation and 100-minute bound,
at most two VM runs in 2/4-vCPU order and two full checks, retaining the
45-minute admission reservation, per-child limits, disk-reserve check and
stop-on-first-unexplained-failure rule. These same-machine preparations do
not expand the accepted M4/macOS/SDK scope, establish real concurrency, or
supply a second-hardware-generation result. Observed replication findings
remain subject to their separate review.
