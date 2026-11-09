# INV-01-33 — Pre-execution review

## Preliminary preparation review, September 7

**Screening, before the technical verdict:** (a) QEMU TCG also publishes and
links translated code, and ordinary VMs load executable guest memory. The
distinct question is the host-to-EL1 translation split composed with the
established paging, recovery and SMP protocols. (b) Documented register-transfer
and cache-maintenance facilities supply ingredients; they do not establish
whole-request ownership, guest continuation validity or live-link lifetime.
The independently reviewed register candidate in source claim 09 avoids the
specific concurrent host-store/guest-load staging premise. (c) This review
applies to the finite host-emitted templates and EL1 shim contract, with pinned
immutable storage, not arbitrary translators, direct instruction patching,
reclamation, Linux boot, performance or a platform-wide coherency guarantee.

Reviewer `/root/track01_understanding_check` is separately delegated by `/root`
and authored no Q33 producer, model bridge, oracle or supervisor code. This
reviewer previously reviewed Q31/Q32 and retains that context; the earlier Q33
source-extension reviewer was a different agent. Read the research workflow
and current charter first, then INV-01-33 and its source-findings review,
especially claim 09 and its six concrete closure obligations. Inspected the
current `q33.c`, `q33.h`, transfer/vector assembly in `guest.S`, foundation
permissions in `guest.c`, relevant Q30 gate/pin/fault paths, Q31 metadata and
new expected-generation acquisition, Q32 phase/IRQ integration, and the
parent's `check_cases.c` callback/transport bridge. Source was actively being
prepared, not frozen. No VM, experiment, full oracle or archived history was
read or run. Only this review document was authored.

The bounded window began **07:51:55 UTC**, with a hard stop of **08:11:55 UTC**;
the completed early pass stopped at **07:58:27 UTC**.
This preliminary pass does not accept execution: the full case driver, host
exit integration, independent oracle, cumulative resource budget and retained
inputs were not yet submitted. Pending submission is work status, not a new
defect. Concrete returns below were sent directly to the producer and parent
as found.

### Concrete returns and inspected corrections

1. **Descriptor-generation acquisition race — code correction inspected,
   adversarial control pending.** Initially `execute()` checked descriptor
   generation N and then called `q31_code_acquire()`, whose `pin_code()` read
   the current generation again. A peer update between them could pin N+1 and
   give old descriptor N falsely fresh recovery metadata. The author added
   `q31_code_acquire_expected(va, page, generation)`: it passes the descriptor's
   expected generation to the existing locked `q30_code_pin()` and records
   that same generation in Q31 local metadata only on success. `execute()`
   uses this API and rejects on failure. Inspection closes the identified
   reread defect; retain a stale-at-acquisition control before final acceptance.

2. **Mapping validation versus pinning — substantive return open.** The
   subsequently added `source_read()` checks each fetched byte's actual GPA
   against the requested source page, and publication checks CR3, source bytes
   and generation. However, entry currently performs `source_read()` and then
   calls the pin API. That API can park and acknowledge a peer's completed
   remap from source page P to Q. P's code generation can remain unchanged,
   allowing the old descriptor to enter after the remap completion. Smallest
   remedy: validate the complete source-read/acquisition interval against
   concurrent mapping changes, release and reject on a changed interval, and
   leave no acknowledgement/service point between final validation and the
   represented guest effect. An epoch-spanning check is one candidate, subject
   to inspection of its exact boundaries. Closure requires a remap injected
   between source validation and acquisition, plus successful current-source
   continuation. Retain the descriptor's CR3/mode identity or explicitly justify
   the finite fixture's equivalent-address-space scope.

3. **Load callback changes an input operand — substantive return open for
   the submitted block ABI.** `generated_enter()` currently assigns guest RDI
   to `base()+0xd000` for kind 2 before executing source opcode `48 8b 07`
   (`MOV RAX,[RDI]`). This instruction must use and preserve incoming RDI.
   Move fixture initialization before entry and have the callback consume the
   canonical operand unchanged; otherwise the callback can hide operand/state
   contamination. Closure requires the independent state oracle to connect
   pre-entry RDI and the actual memory request, and preserve it through fault,
   repair and retirement. Input RAX/RIP likewise need binding to the declared
   continuation, not merely agreement between producer callbacks.

### Checks supporting continued preparation

The transfer helper exports sixteen 64-bit registers, with four little-endian
payload words per chunk and three chunks for the 96-byte immutable request.
Its 32-byte native stack frame saves X19/X20/LR; caller-clobbered payload
registers are returned to the packet through the retained X19 pointer. The
existing exception vector saves/restores X0–X30 and ELR/SPSR, including the
payload and packet pointer, so an immediate native IRQ has a concrete
preservation path. This is source inspection, not an observed interrupted
register transfer. The final cases must demonstrate pending-event service
after copying, without interpreting partial response registers as x86 state.

The host emitter's fixed prologue/tail uses descriptor callbacks at offsets
80/88, preserves native X19/X20 and frame/LR, and computes the finite ADD in
the emitted instruction body. The first callback's recorded return PC is
entry+40; the final callback's is entry+length-12. The ADR calculations target
the block entry. The padding is executable NOP instructions; the guest copies
response bytes rather than calling the emitter. The model instead validates
bytes using the shared emitter and composes callbacks in C, so it cannot
establish native execution, independent byte semantics, cache publication or
actual callback-PC recovery. Those remain obligations of the separate oracle
and later VM evidence. Kind 2 performs its faulting load through the existing
Q31 access helper called from generated code; claims must describe that route.

### Remaining submission obligations

- **Guest permission/publication contract:** the current pool inherits the
  foundation's writable/executable identity mapping, while `publish()` performs
  rounded CTR-derived DC/IC maintenance and release-publishes validity. The
  reviewed method says stage-1 permission sealing. The author must implement
  that transition or submit an explicit narrower argument for unreachable,
  immutable storage and its permission limits. A cache barrier is not a page
  permission transition. Peers must synchronize before entering the same
  shared object; private-copy execution does not cover shared live links.
- **Stopped-owner progress:** `exchange()` polls before and after transport and
  requires no active Q30 access/inflight request. The complete host path must
  avoid waiting for a peer operation whose gate completion requires this
  stopped owner's acknowledgement. A bounded WAIT response can return the
  owner to shim polling. Trace actual peer progress and the source/mapping
  change interval, not just a requested host delay.
- **Exact host ownership and failure:** validate the specific HVC helper and
  exit/resume PC, retain all getter/setter outcomes, and stop before reentry
  after any partial setter failure. Canceled/unrelated exits must not consume
  chunks. Bind host emission, complete packets, copy-before-ACK, fresh storage,
  publication and actual PCs/effects in the independent oracle. Complete the
  partial, duplicate, reordered, cross-CPU, stale and producer-failure controls.
- **Live links and continuation lifetime:** the current callback path can
  recursively enter a successor in the VM, but replacement/acknowledgement
  cases must exclude an already-loaded old descriptor before declaring the
  replacement effective. Pinned old allocations remain needed for native
  epilogues and recovery even after their last guest effect. Source aliases,
  invalidation during saved fault/IRQ continuations, true fetch faults versus
  translation misses, finite exhaustion and termination remain required.
- **Integrated submission:** retain exact producer/oracle/supervisor inputs,
  all native and synthetic controls, and a source-derived cumulative bound
  covering register exits, code-pool consumption and records. Q30–Q32 must stay
  active. A later independent exact-input review is required before any VM.

Preliminary verdict: **continue preparation with the two open substantive
returns above; no execution acceptance**. No new backlog identifiers or
platform claims are inferred from incomplete preparation.


## Independent integrated preparation pass — September 7, 08:37–09:07 UTC

Screening **(a)**: QEMU TCG shares generated-code cache publication, source
invalidation and live-link lifetime problems; an ordinary VM shares native
interrupt delivery. This candidate's stopped-vCPU register transport into an
EL1 code pool and its composition with mirrored x86 accesses is the additional
question. **(b)**: documented register getters/setters, cache instructions and
GIC/timer facilities supply ingredients, not this composition's correctness or
progress result. **(c)**: the review applies to the finite host-emitted A 64
ADD/MOV templates, register-copy ABI, immutable RWX native pool, pinned source
metadata and retained Q30–Q32 shim foundation. It does not establish a general
translator, full Linux execution, hardware W^X, reclamation or performance.

Reviewer `/root/track01_understanding_check` began at 08:37:39 UTC, with a hard
stop of 09:07 UTC. This reviewer authored no Q33 implementation, oracle or
supervisor. Separately authored Q34 preparation is excluded from this review.
Inspected current producer, host/assembly, independent Q33 and shared oracles,
1600-MiB geometry and source-derived budget. Compared Q30/Q31/Q32 sources
against immutable Q32 build 06 directly; no git history or VM was consulted.

The earlier expected-generation pin, source-fetch-to-pin mapping epoch and
incoming-RDI returns now have concrete implementations and forced peer-change
cases. The generated LOAD consumes the incoming RDI, and old source bytes are
independently frozen at pin acquisition for the old-generation fault/retry.
The native call retains immutable code storage after guest-effect unpin, which
is sufficient for its remaining native epilogue because storage is never reused.
The explicit narrower RWX/software-immutability method resolves the earlier
permission-sealing mismatch for this finite design. Code is fetched, copied and
independently decoded; links retire actual fetched x86 short JMPs. Per-CPU
acknowledgements follow return from old calls before fresh link traversals.

### Concrete returns in this pass

1. The native IRQ deliberately interrupts a QT_WAIT response, which originally
   emitted no RECEIVE payload. CONTROL 2 recorded only operation, transaction,
   offset and status; corruption of copied WAIT X4 could be discarded while
   the host's complete response transcript still passed. Require observation
   of all sixteen copied words immediately after the helper and before guest
   delivery, joined to the exact host response, plus a copied-word-only negative
   control. The author has added four CONTROL 11 records; independent binding
   and retained controls remain to be inspected.
2. The native witness originally accepted IRQ ELR after one or more response
   STP pairs had already copied registers. To substantiate all sixteen values
   being live during IRQ, require resume+4 <= ELR <= first response STP
   (resume+72 for the decoded helper), and reject a second-STP arrival even
   when the final packet matches. The oracle author agreed to this tightening.
3. The new cross-CPU epoch validation must use the established acquire load
   for its before/after reads, matching Q30's atomic release increments. A
   narrow LD replacement was requested; incidental diagnostic reads are not
   part of this return.

### Geometry and resource assessment

The source maps native IPA 0x40000000–0xa3ffffff exactly: L1 entries 1/2 point
to two contiguous L2 banks, eight L3 tables retain the first 16 MiB at 4 KiB,
and 792 L2 blocks cover the rest. The alias at 0xa4000000 is bank 1/index 288,
the next unused slot, with only its first 4 KiB mapped. Code, state, x86 RAM and
shadow tables remain in the first 16 MiB. The independent foundation checker
checks every 1024 L2 slot and every retained L3 entry, including attributes;
its synthetic bank/alias/extra-map/permission corruptions reject.

Source-path counting supports the proposed 191,488 Q33-access ceiling and
1,536 fixed records per CPU-case. In particular, the 134-chunk large object
costs 967 production records, while group 8 explicitly allows all 16 source
validation attempts for both instructions. Target-rotated actor work is
charged alongside target work. Pool bounds 2960 objects/655360 bytes include
rejected allocations and agree with the source variants. The copied-WAIT
return adds four fixed records to a group 6 case and remains inside this
allowance. Prefix Q30–Q32 paths are unchanged except phase admission and new
helpers called only by Q33. The proposed cumulative 16,071,443 records fit
16,777,216 slots, with 705,773 spare. These are inspected finite successful-path
bounds, not measured counts or a C proof. Overflow remains a retained failure.
The source guard correctly rejected the changed q33.c during this pass and
must be refreshed only after the narrow returns are recounted.

The supervisor's Q33 branch selects 1800 seconds, 10 GiB sampled group RSS at 1 second,
with 256 MiB checker output files; producer RAM is 1600 MiB with 2 GiB per-file limit.
The larger bound is not a completion guarantee on a 16 GiB host. A concrete
campaign wall/admission/disk budget must supersede the old six-additional-
diagnostics prose before execution. Exact frozen producer/oracle/supervisor
inventories, full enabled oracle and final controls remain required.

Interim status at 08:46 UTC: **continue preparation; no execution acceptance**.
The reviewer ran the current independent primitive self-check:31 controls
rejected, explicitly synthetic/INCOMPLETE. No VM or full raw acceptance ran.
A closing disposition will follow within this bounded review window if the
complete retained submission is ready.


At 08:49 UTC the current producer closes returns 1/3 by source inspection:
four CONTROL 11 records follow the actual helper before guest delivery, and the
mapping guard acquire-loads both epochs, retaining the compared value in the
failure record. The oracle now joins CONTROL 11 to its exact helper interval,
compares all 16 words with the host response, and limits native IRQ ELR to the
first response STP inclusive. Its copied-WAIT and second-STP negative controls
are present; retained results remain pending. The refreshed budget guard passes
16,071,443 <=16,777,216.

The new 270-minute primary and 100-minute independent campaign proposals have
finite slot/check/admission limits and stop on unexplained failure. One resource
return remains: host.c also writes q30.bin (about 1.5 GiB at maximum capacity),
beyond four 1600 MiB RAM images. Six worst-case runs therefore consume 46.5 GiB
before margin, exceeding the proposed 44 GiB activation reserve; two consume
15.5 GiB before margin. Requested explicit inclusion of q30.bin with sufficient
reserve/per-run admission, or a separately justified source-derived sparse
allocated-byte ceiling. Observed sparsity is not a bound.


### Retained producer/supervisor closure, 08:57 UTC

Independently verified all 96 entries of external `q33-code-build-03`, inventory
SHA256 `7ceaadc58f8062e5b686d42aa7edbe5cc39e683c3dcc1504fbc6cec9ea491c7d`.
Its 26 retained commands (20 strict build, 6 preparation) pass. Two/four-CPU
models report 67043/132732 accesses and at most 93 setup patches. The 196608-byte
guest image is below 2 MiB. The reviewer independently ran the strict Mach-O
symbol reader on this exact image (153 symbols) and the Q33 native transfer/
cache-site decoder: PASS. The local q32_fixture rename only disambiguates
retained symbols; earlier Q32 behavior is unchanged. Producer q33.c SHA256 is
`55b7797e9f3884e8f454d060f44f3fa816c151c02caf6595f21d6a8948b5b3c2`;
host.c is `c1cec69085cb82fe607c241f1fcd2a8735ad14f26dacfe54358207dbab6aff17`.
An initial metadata read assumed a separate preparation.json; the submission
instead records preparation in manifest.json. Inventory verification succeeded
before that read-only path error; all commands were then inspected there.

Independently verified all 4 entries of `q33-code-supervisor-01`, inventory
SHA256 `dd5bd418491081e96dde8c8cdd8ba5bc455de6fa86c8dbfe4f7d41d1aabc1db5`.
Its retained self-check passes. Exact source selection includes the whole
Q30–Q33 oracle chain, using 1800 seconds/10 GiB/1 second and 256 MiB output files.

The disk return is closed by the explicit conditional diagnostic budget:
44 GiB prepays the four required VM/check pairs (31 GiB maximum for their five
large files, including q30.bin, plus 13 GiB margin). Two optional diagnostic
slots are not prepaid or guaranteed. Every new VM requires a fresh 10 GiB
admission and sufficient remaining time; otherwise stop and retain evidence.
Independent replication reserves 20 GiB for two pairs and uses the same 10 GiB
admission. The prepared, inactive campaign names build 03/supervisor 01 and these
limits. No sparse-allocation saving is assumed in this acceptance.

Two final oracle-only requirements were returned: each live-link ACK must
follow that CPU's old effect with no active generated call, and the old-pinned
LOAD denial in group 9/1 must have a real native FAULT record when native
coverage is asserted. Source callsites already implement both routes; negative
controls must reject an early ACK or missing native fault witness. Final frozen
oracle verification and exact-input execution disposition remain pending.


### Final exact-input pre-execution verdict — 09:01:34 UTC

**ACCEPT the bounded Q33 execution method using only the following retained
inputs.** This is preparation acceptance, not an observed feasibility finding.

- Producer `q33-code-build-03/source`, complete 96-entry inventory SHA256
  `7ceaadc58f8062e5b686d42aa7edbe5cc39e683c3dcc1504fbc6cec9ea491c7d`.
- Independent oracle `q33-code-oracle-01`, complete 20-entry inventory SHA256
  `a5b30ef85eff0093c163b4cd7d2fcecfc3eded32ce3f4754c6db88437b695089`;
  check_q33.py SHA256
  `401b769029800648d5996c43d478c7425d6a879d2f9bafacf397fed42eeb12b7`.
- Supervisor `q33-code-supervisor-01`, complete 4-entry inventory SHA256
  `dd5bd418491081e96dde8c8cdd8ba5bc455de6fa86c8dbfe4f7d41d1aabc1db5`.

All three inventories were independently recomputed. The frozen oracle's six
sources equal the reviewed source; its six retained synthetic command outputs
pass with empty stderr. Both models cover 448 global cases, 896/1792 CPU cases,
67043/132732 accesses and 21 rejected trace corruptions. The frozen primitive
self-check was independently rerun:33 corruptions reject. Prior Q30/Q31/Q32
self-checks and the exact compiled native-helper/cache decoder pass. These are
synthetic and compile-only results; no native runtime or raw acceptance was
performed by this reviewer.

All substantive returns are closed. In particular, the oracle now observes
all 16 interrupted WAIT response words, restricts the arrival to before any
response pair is copied, excludes active old calls at individual LINK3 ACKs,
and requires the old-pinned generated LOAD fault's native witness. Negative
controls exercise each condition. Expected-generation pins, acquire-loaded
mapping epochs, incoming RDI and old-generation recovery recipes are retained.
The full raw CLI invokes native finite coverage, all earlier checkpoint/replay
checks, actual compiled PCs/register packets and final state validation; it
requires an empty unsupported replay set before returning PASS. Synthetic
models cannot supply its stopped-owner/native witnesses.

The accepted primary campaign is 2,2,2,4 vCPUs with 16 repetitions of every
variant, maximum 6 VM runs/8 full checks within 270 minutes of recorded activation,
45-minute new-pair and 35-minute same-RAM-check admission. Extra diagnostic
slots require a named reviewed return and fresh admission. Four required pairs
are reserved by 44 GiB activation storage; each new VM needs 10 GiB free. Independent
replication needs separate reviewed preparation, two VM/check pairs 2,4 within
100 minutes, 45-minute admission, 20 GiB activation and 10 GiB per new VM. Preserve
failed/partial artifacts; stop on the first unexplained failure or missing
coverage. No automatic renewal, exclusions or additional VM slots are accepted.
The 10-second VM watchdog, 30-second producer child limit, 1600 MiB RAM, 2 GiB
producer files and 1800-second/10 GiB sampled-RSS checker remain mandatory.
The 16,071,443-record and 2960-object/655360-byte source bounds fit their retained
capacities. Resource limits bound failure; they do not promise completion.

The actual execution must still establish native source pending 0→1 while its
owner is stopped, pre-copy IRQ arrival and preservation, overlapping misses,
shared live-link replacement/ACKs, real fault/retry and generation composition,
and every retained Q30–Q32 mechanism in the same VM. Any failed coverage or
runtime result is retained and reviewed before changing the candidate. A final
findings review and independent replication remain required before accepting
Q33's scoped conclusion. This verdict applies to the immutable sources above,
not later Q34 worktree changes. No new backlog identifiers or broader platform,
full-ISA, Linux, isolation or performance conclusions are inferred.

Review completed at 09:01:34 UTC, inside the 08:37–09:07 window. No VM was started by
this reviewer, and no Q33 producer/oracle implementation was authored here.


## Replica preparation 01 and first native return — September 7

Screening(a/b/c) remains the finite register-transport composition above. The
new failure is a producer bookkeeping omission; documented hardware behavior
is not its cause. This return review began 09:06:53 UTC with a 15-minute window,
ending 09:21:53 UTC unless completed earlier. No additional VM is authorized by
this partial review.

Replica preparation received 09:04:59 UTC was independently verified:38-entry
`q33-code-replica-prep-01` inventory SHA256
`93654e05e4327cd0ff072652920e4956c9f934420c84cf4ddb72216ead3f2357`, and 96-entry
`q33-code-replica-build-01` SHA256
`d116a79ae36c1602251bda7338d6bc2943960e534c9cef47d289aa6bbe94e283`.
Twenty-one retained source files and 12 compiled artifacts equal build 03;
the unused Q34 drafts in that source inventory are not compiled. The six
oracle sources and supervisor equal accepted oracle 01/supervisor 01. All 26
rebuild/preparation commands and seven additional frozen checks pass. Replica
models report 896/1792 CPU cases, 67048/132857 accesses and 21 rejected corruptions.
The reviewer independently reran the compiled helper/cache decoder:153 symbols,
PASS. The context discloses prior oracle authorship and independence from
producer and primary runtime; this reproduces with the same reviewed oracle,
not a second oracle implementation. Before an activation verdict, primary 01
failed. **Preparation 01 is verified but superseded, with no execution
acceptance; retain it unchanged and review fresh corrected preparation.**

The new producer source adds only r->gpa=walk.gpa in successful Q_FETCH8
prevalidation, while holding the existing writer lock and before releasing
active=1. Its cached native read therefore retains the same pinned physical
identity as its faulting path. It changes no access/event count, permission,
cache or guest architectural effect. The v5 oracle separately requires known
successful FETCH8 GPA equal to its independently walked end address. Earlier
v3/v4 semantics remain unchanged. Positive warmed-shadow byte reads and missing/
wrong-GPA negatives exercise the actual replay path; the reviewer independently
reran Q30 self-check, now 47 rejected controls. Exact corrected snapshots and
source-hash budget guard remain required before the next VM.


### Corrected exact-input return verdict — 09:18:17 UTC

**ACCEPT producer build 04 + oracle 02 + supervisor 01 for the remaining bounded
Q33 campaign.** This supersedes build 03/oracle 01 execution acceptance after
its retained native failure; it does not erase that failure or accept findings.

Verified all 112 entries of `q33-code-build-04`, inventory SHA256
`6f2920ba0022b609725297f2a36683b0ea52c1b299beb50e82153e7ffdc9c749`.
Source differences from build 03 are exactly q30.c's successful execute-walk
GPA assignment, the model's explicit missing-second-byte-GPA injection, and the
budget hash/comment. All 30 commands pass:20 strict build and 10 preparation.
The expected injection fails with exit 1/reason 13082/status 0/error 0. Native
strict -O2 assembly differs by exactly a load of walk.gpa and a store to the
request result before active publication; the retained diff is
fetch-gpa-assembly.diff. The other seven guest objects and signed host are
byte-identical to build 03. Image size remains 196608 bytes. The reviewer again
ran the strict 153-symbol reader and actual helper/cache opcode decoder: PASS.
The refreshed source bound is unchanged at 16,071,443 records; no operation,
epoch, record, pool or failure-limit multiplicity changed.

Verified all 15 entries of `q33-code-oracle-02`, inventory SHA256
`ecf02247f42eab7a094c3449a65f5c3f9a7fd164420003953079a6472ec7682e`.
Only check_q30.py changes, SHA256
`bf16242004b82f7a7b5b4f098780b79579880309c627050b83e0e80e0e9fefba`;
the other five sources equal oracle 01. Its four refreshed self-checks pass,
including 47 Q30 controls and the unchanged 33 Q33 controls. New v5 validation
requires successful FETCH8's known physical result to equal the independent
walk, including a cached native shadow. This strengthens the result contract;
no native coverage or semantic expectation was weakened.

The reviewer also ran this exact oracle 02 against build 04's four-CPU model:
448 cases/1792 CPU cases/132815 accesses/3024 transactions/2192 objects,
21 rejected trace corruptions, empty stderr. The producer's two-CPU model has
67051 accesses. Review output is
`/private/tmp/q33-review-build04-model4.stdout`, SHA256
`e868b24c34cef66e3a90c89bf2d1948d7c0461d4c2b697d6623f4fd79453cb73`.
This injected failure/model replay does not emulate the native shadow cache;
the compiled correction and next reviewed native run supply that evidence.
Supervisor 01 remains the previously verified exact four-entry input with
inventory SHA256 `dd5bd418491081e96dde8c8cdd8ba5bc455de6fa86c8dbfe4f7d41d1aabc1db5`.

Resume only within the original six-VM/eight-check ceiling and
13:33:22.062654 UTC deadline. One failed VM is already consumed; four corrected
primary pairs 2,2,2,4 remain required, with at most one additional conditional
slot after them. The 45/35-minute time admissions, fresh 10 GiB new-VM storage
admission and all earlier watchdog/RSS/file limits remain unchanged. No
unexplained failure, missing native coverage, automatic retry or post-hoc
exclusion is authorized. Update campaign input identities before admission.
Replica build 03 preparation is superseded; corrected replica preparation must
be independently accepted before its separate 2/4 campaign. Final primary and
replica findings review remains open.

Completed at 09:18:17 UTC, within the 09:06:53–09:21:53 return window. This reviewer
changed only the existing review threads and started no VM.


## Corrected independent replica preparation 02 — 09:19:54 UTC

Screening(a/b/c) is unchanged: this independently rebuilds and reproduces the
same finite register-transfer composition on the same M4, using the same
separately reviewed oracle. The replica author explicitly discloses prior
oracle authorship and independence from producer and primary runtime. This is
not an independent second oracle implementation. Reviewer
`/root/track01_understanding_check` received preparation 02 at 09:19:02 UTC and
completed this bounded review at 09:19:54 UTC, before its 30-minute deadline.

**ACCEPT the corrected replication preparation. Runtime activation remains
on the parent's explicit hold until the first corrected primary's full-check
outcome is reviewed.** No replica VM has run, and this verdict starts no clock
or VM itself. A new native failure requires a fresh return before duplication.

Verified 33 inventory entries of `q33-code-replica-prep-02`, SHA256
`437cf5b0b8d9f949ec81c86e8d4325b9ad055c8d01b30b73002600883eeadcba`, and 112 entries
of `q33-code-replica-build-02`, SHA256
`6609ddc65aa59b481245fab687b245199cc67af72e16f3796a8ba8119a7bf2da`.
The inventory convention omits the copied nested oracle/SHA256SUMS; its bytes
were separately verified identical to accepted oracle 02's inventory. All 21
runtime source files, 12 compiled artifacts and three regression helpers equal
accepted build 04. The two included Q34 draft files remain uncompiled. All six
oracle sources equal oracle 02 and the supervisor equals supervisor 01. The only
preparation.py delta replaces mutable helper input paths with build 04's retained
identical copies; it changes no check or runtime behavior.

All 30 rebuild/preparation commands and seven additional checks pass. The own
2/4-CPU models cover 896/1792 CPU cases with 67064/132812 accesses and 21 rejected
trace corruptions each. Q30's47, Q32's20 and Q33's33 controls and the Q31 and
supervisor checks pass with empty check stderr. The reviewer independently
checked the rebuilt 153-symbol native helper/cache instruction decoder: PASS.
The expected missing-GPA negative and strict two-instruction assembly delta
are retained. These are compile/synthetic preparation results, not native
runtime evidence.

Once the parent releases the hold, replication uses the exact corrected source,
oracle 02 and supervisor 01 for two pairs 2,4 vCPUs, maximum 2 VM runs/2 full checks,
100 minutes from recorded activation, 45-minute admission, 20 GiB free at activation
and 10 GiB before each new VM. VM/check execution remains sequential, with the
accepted 10-second watchdog/30-second child/1800-second checker/10 GiB sampled RSS
and file limits. Stop at the first failure or missing coverage; preserve every
outcome. Preparation 01 stays retained superseded. Final runtime findings remain
unaccepted pending all required primary and replica evidence.


## Independent timer-oracle delta review — September 7, 09:40–09:43 UTC

Screening (a): timer condition and controller pending state are separate
observations in emulators and ordinary VMs as well. Screening (b): the change
uses documented architectural status and the actual subsequent interrupt,
without assuming an internal Hypervisor latch implementation. Screening (c):
the return changes only the stopped-timer witness for Q33's controlled WAIT
response; the producer, native interval and every earlier mechanism remain.
Reviewer `/root/track01_understanding_check` authored no Q33 implementation or
oracle code. The source/method rationale and direct failed-run evidence are
recorded in the existing findings thread.

The exact candidate is `/private/tmp/q33-timer-oracle-preparation-03`, whose
27 inventory entries independently verified; inventory SHA256
`c117cf6ca030611f7926686b1000852d6a7804f83bbc5b9c1e0d16a1383779e9`.
Only check_q33.py differs from oracle 02, SHA256
`76dd6455513e0f944580edcfbd2d198d054ffb3859a558857e49d1dd81ae58fc`;
the other five oracle sources are byte-identical. The delta adds a shared
`delayed_source_witness` on the actual full-raw acceptance path. It binds the
unique CONTROL 8 arm inside the transfer to the accepted source sequence and
live foundation timer INTID. The timer requires CTL low bits 1→5 and unchanged
CVAL equal that arm, while retaining quiet initial GIC state, inactive state,
raw-bit consistency, owner/transaction and masked stopped-vCPU checks. Only
the timer's after-pending bit may be either 0 or 1. SGI/SPI still require 0→1.
The exact native source/owner and pre-first-STP ELR, all 16 actual copied words,
source invalidation, guest frame/IRET and fresh continuation remain mandatory.

Independently read [Arm Architecture Registers, CNTP_CTL_EL0 pages 229–230 and
CNTP_CVAL_EL0 page 234](https://documentation-service.arm.com/static/6023d5362cb3723f202089b8).
Enabled ISTATUS reports the condition independently of IMASK; a satisfied,
unmasked condition asserts the timer interrupt, whereas disabled ISTATUS is
unknown. This supports the precise status predicate, not an assertion about
when a separate stopped GICR read must report pending. No undocumented timing
or internal platform cause is inferred.

The reviewer independently reran all 44 primitive negative controls and the
focused native evidence check:96 positive WAIT/source witnesses, including 32
timer-expired/GIC-unlatched and 64 SGI/SPI, a synthetic timer-latched positive,
and 15 real-pattern corruptions rejected. Those include status before/after,
CVAL/arm binding, accepted sequence, coherent wrong timer INTID, late ELR,
changed copied X4 with unchanged host response, and missing SGI/SPI pending.
The package retains the deterministic frozen 02 failure, these controls and
producer 04 model 2/4 regressions (67,051/132,815 operations, 21 corruptions each).
Models remain synthetic; the focused check is not a full raw oracle invocation.

Verdict: **exact code/method candidate accepted for sealing and subsequent
same-RAM recheck preparation.** A copied immutable oracle 03 inventory must be
bound below before the parent admits the full recheck of retained run 02.
Producer 04 and supervisor 01 remain unchanged. Failed oracle 02 and its report
remain immutable. No VM or full recheck was run by this reviewer. Replication
stays held pending the recheck outcome, and observed Q33 acceptance still
requires the complete primary/replica campaign under its original budgets.

### Retained exact-input disposition — 09:43:36 UTC

**ACCEPT for one same-RAM full recheck:** independently verified all 27 files
of `natemu-raw/q33-code-oracle-03`, byte-identical to the
reviewed candidate, including inventory SHA256
`c117cf6ca030611f7926686b1000852d6a7804f83bbc5b9c1e0d16a1383779e9`.
Use unchanged producer 04/runtime `q33-code-02` and supervisor 01 for new retained
`q33-code-02-check-02`. This is a new check identity; the earlier failed check
is preserved. The original eight-check cap, 35-minute same-RAM admission,
13:33:22.062654 UTC campaign deadline, 1800-second checker/10 GiB sampled RSS and
output limits remain in force. This authorizes no additional VM and does not
release the replica hold before the recheck outcome. No Q33 observed finding
is accepted by this pre-execution verdict.


## Independent replica oracle-substitution preparation — September 7, 09:48 UTC

Screening (a): independent rebuild/reproduction shares ordinary emulator and
VM provenance obligations. Screening (b): this substitution introduces no new
hardware capability. Screening (c): this is preparation acceptance for the
same-host Q33 reproduction using the separately reviewed oracle 03, not a
second oracle implementation or an observed replication result.

Reviewer `/root/track01_understanding_check` independently verified all 37
entries of `q33-code-replica-prep-03`, inventory SHA256
`58194fd02ecc5297620cf23d6c4eec9a7b6da34cfc78186395ee261b0ac5ffdc`.
The entire nested oracle directory, including its separately pinned inventory,
is byte-identical to accepted oracle 03. The unchanged replica-build 02 still
matches producer 04 across 21 source files and 12 compiled artifacts; its 112
inventory entries and prior prep 02's33 entries were independently rehashed.
The disclosed preparation-helper path adaptation and prior accepted compiler,
Q30–Q32 and supervisor checks remain valid because their inputs are unchanged.

New Q33 self-check 44 and replica model 2/4 checks passed with 67,064/132,812
operations, 896/1792 CPU cases and 21 rejected trace corruptions each; stderr is
empty. Retained metadata reports the 153-symbol native helper decoder check.
The replicator explicitly discloses Q33 oracle authorship and independence
from producer and primary-runtime authorship. This is an independent retained-
source rebuild and reproduction using the same reviewed oracle.

**ACCEPT this preparation substitution**, binding accepted replica-build 02,
producer 04, oracle 03 and supervisor 01. The later campaign remains two sequential
VM/check pairs 2,4, maximum 2 VM/2 checks, 100 minutes from recorded activation,
45-minute admission, 20 GiB activation reserve and 10 GiB before each VM. Existing
watchdog/child/checker/RSS/file limits and first-failure stop remain unchanged.
Same-RAM primary 02 now has full PASS under oracle 03, but this verdict does not
start the replica campaign or override the parent's serial primary scheduling.
No VM was run by the reviewer or during this preparation.


## Q32 bounded-trial producer/model review — September 7, 10:05–10:20 UTC window

Screening (a): retaining and checking arrival placement is a shared emulator/VM
fixture obligation. Screening (b): the correction relies on the existing WFE,
timer and guest interrupt paths; no new hardware capability is assumed.
Screening (c): the finite after-WFE placement case gains a bounded explicit
retry protocol. It does not relax the final native witness or establish timing
or failure-rate claims. Reviewer `/root/track01_understanding_check` authored
none of these producer, harness or oracle changes.

Initial concrete review inspected `/private/tmp/natemu-q33-wait-fix/source`
against accepted producer 04. Only group 7/variant 2 installs the actual
`F4 EB FD` stream and loops 1–8. Each trial retains HLT/recheck/native/control/wake
records, then `service()` completes guest delivery, handler, EOI and IRET.
The first exact post-WFE result returns; after an early result, trial 8 fails 53
only after service. Otherwise two successful FETCH8 requests verify EB FD at
post-HLT RIP0x3501 and `q31_retire_at(rip-1)` implements its signed branch back
to 0x3500 while preserving defined state. Other halt variants retain their
previous order. Q32 version 2 declares the added CONTROL 15 contract with the
same 85-word per-CPU ABI; native helper assembly and timer delay are unchanged.

The model injection changes only the synthetic native PC for the selected
Q32 group 7/variant 2, with a per-CPU counter reset by case serial. Actual modeled
notification/acceptance and all guest instruction paths still execute. Direct
trace inspection found 32 CPU-cases with 1 trial for early0,2 for early1,8 for
early7, and 64 eight-trial CPU-cases at four CPUs. Ordinals are contiguous and
only the last trial qualifies. The new failure-only trace helper emits an
explicit incomplete format, real final CPU state and all existing records;
it neither appends a fake END nor changes ordinary model/native paths.
The forced-eight trace contains 8 early markers followed by 8 actual RETURN
records; final halted/depth/IRR/ISR are all zero before failure 12853. This
confirms the source's post-service exhaustion path synthetically.

The additional source bound independently checks:64 CPU-cases ×7 extra
trials=448, each at most 24 interrupt accesses plus 2 retry FETCH8 accesses.
The nonaccess count 131 (HLT, boundaries, delivery, handler instructions,
restoration/IRET, native/APIC/control records) is below the 192 allowance.
No new context/role/topology/invalidation operations occur in these episodes.
Including 64 first-trial CONTROL 15 records adds 353,984, giving 16,425,427 of
16,777,216 records, 351,789 spare. The guarded budget script passes. This is a
source-path bound; observed model access counts merely corroborate it.

The in-progress independent oracle uses one shared trial checker in model and
raw paths. Inspection found it binds every native arrival, HLT, dispatch, EOI,
IRET and actual retry JMP; matches CONTROL 15 to native ELR/count/ordinal; checks
complete restored architecture; and stops at the first qualifying witness.
Version 1 retains its single exact post-WFE criterion; final version 2 inputs
must use the new contract. Final corruption controls, complete oracle snapshot,
compiled producer snapshot and exact retained-input verdict remain pending.
No new VM or full oracle is accepted by this preliminary review.

### Sampling disposition for the original campaign

The two successful primary pairs 02/03 remain valid Q33 evidence under their
recorded producer 04/Q32-version 1/oracle 03 identities: each had every required
post-WFE witness and the complete Q33 mechanism. The failed 04 outcome remains
retained and reopens the affected Q32 claim. The source correction preserves
successful first-trial guest semantics and the Q33 mechanism, while adding a
new bounded path for early arrivals. The workflow requires each run's intact
source/build/evidence association; it does not require all four feasibility
pairs to have byte-identical snapshots across independently reviewed returns.

It is therefore admissible to use remaining primary 05(two CPUs) and 06(four
CPUs) under reviewed final producer/oracle inputs, followed by independent 2/4
reproduction under those same final inputs, within the original six-VM cap.
The final results table must show the mixed input identities explicitly;
never describe the older pairs as executing the new retry path or pool their
timing into a reliability/performance sample. Any native early-arrival recovery
claim needs an actual retained multi-trial native witness; synthetic controls
alone cannot supply it. Final source/evidence review still determines whether
all scoped obligations are met. This disposition renews no campaign budget.


### Retained producer 05 inspection — 10:13 UTC

All 128 entries of `q33-code-build-05` independently verified, inventory SHA256
`c776f0e2a024f335eb6b4924dbacae753463f7064937e6e691faeab1ffa4fab2`.
The only source differences from producer 04 are q32.c, q32.h, check_budget.py
and check_cases.c. Guest assembly and Q33 implementation sources are unchanged.
The retained manifest has 33 passed commands and a 196,608-byte guest image.
It includes forced early0/1/7 model traces, four-CPU early7, the genuine partial
exhaustion trace, original failure control and cached-FETCH regression.
The producer-specific return is closed for this snapshot; complete oracle 04
and exact execution-input disposition remain necessary before a new VM.

The reviewer also independently executed the candidate oracle's two-CPU
early7 model check:592 global cases/61,792 operations PASS with 37 rejected
semantic corruptions, including all new trial-order/state/JMP/cap controls.
This is synthetic confirmation, not native early-arrival recovery evidence.
If final native runs all qualify on their first trial, the bounded retry path
must remain explicitly source-reviewed/inferred plus synthetic, rather than
being reported as observed native recovery. That unclaimed observation does
not by itself require an extra forced hardware path for the scoped feasibility
question; all mandatory native post-WFE and guest-return cases still must pass.


### Exact oracle 04 candidate review — September 7, 10:16–10:18 UTC

Screening (a): complete accounting for bounded arrival trials is a shared VM
and emulator oracle obligation. Screening (b): the candidate checks retained
native execution and documented guest state without assuming new capability.
Screening (c): only the version 2 after-WFE fixture changes; the previous source
and timer-return constraints, Q30–Q33 composition and finite applicability stay
in force. Reviewer `/root/track01_understanding_check` authored no changed code.

All 49 entries of `/private/tmp/q33-hlt-oracle-preparation-04` independently
verified, inventory SHA256
`62c673259397b4efb0f3c161622641242c08451ec0529a462010bb58d90388cc`.
Only check_q32.py changes from oracle 03, SHA256
`0eba3cb20181fae8217e9a1150597458f68c480669cbd5fc0b4a3aef672246a5`;
five other oracle sources are byte-identical. All 15 retained model/native input
hashes and 12 passed command outcomes were inspected. Q33's full raw callback
continues through Q32's complete checkpoint checker with version 5, which now
requires Q32 version 2. The version 1 route retains its exact single post-WFE
native witness. New models explicitly require their version 2 header.

The shared trial checker validates each native/HLT/emit/dispatch/EOI/IRET,
canonical restoration and actual EB FD retry, with the first exact post-WFE
trial as the only successful final trial. All eight early trials remain a
failure in successful model/raw paths. Only a separate diagnostic script
passes `diagnostic_exhausted=True`; it requires the explicit failed partial
format, no fabricated CASE END, completed real operations, 8 serviced IRQs,
7 retry branches and the actual final canonical state/counters. It cannot
produce raw acceptance. The reviewer independently reran this diagnostic and
its 3 negative controls, plus Q32's23 primitive controls, including legacy
pre-WFE rejection and refusal of version 2 without trial records.

Retained producer 05 Q32 models early0/1/7 at two CPUs and early7 at four CPUs
pass 55,968/56,800/61,792/128,288 operations with 30/35/37/37 rejected semantic
corruptions. Q33 models pass 67,056/132,857 operations with 21 controls each.
The unchanged Q30/Q31/Q33 self-checks and 153-symbol compiled helper decoder
also pass. No diagnostic outcome is promoted to full correctness evidence;
all native outcomes remain to be tested under the accepted campaign.

Verdict: **ACCEPT the exact candidate for immutable sealing.** Once the copied
inventory is verified below, producer 05/oracle 04/supervisor 01 may execute the
remaining reviewed primary 2/4 pairs under the unchanged campaign deadline,
slot/file/RSS limits and first-failure stop. Independent replica preparation
must be refreshed from producer 05 and oracle 04. This is no observed Q32 or
Q33 resolution and no automatic extension beyond the original six VM slots.


### Retained execution-input acceptance — 10:18:12 UTC

**ACCEPT producer 05 / oracle 04 / supervisor 01 for the remaining primary
05(two CPUs), then 06(four CPUs), each followed by its full check.** All 49 sealed
oracle 04 files at `natemu-raw/q33-code-oracle-04` were
independently rehashed and compared byte-for-byte with the reviewed candidate;
inventory `62c673259397b4efb0f3c161622641242c08451ec0529a462010bb58d90388cc`
is unchanged. Producer 05's128-entry inventory and corrected source/budget were
accepted above. Supervisor 01 is unchanged.

Four VM slots and three checks are already consumed; these are the final two
VM slots of the original six-slot campaign, not a renewal. The original
13:33:22.062654 UTC deadline, 45-minute pair admission, fresh 10 GiB disk floor,
10-second guest watchdog/30-second producer child, 1800-second checker,
10 GiB sampled process-group RSS and existing file caps remain. Stop at the
first failure, missing coverage or resource breach. Record every actual trial;
first-trial success does not become an observed native retry claim. Replica
activation requires its refreshed independent preparation review. Final Q32
return closure and Q33 feasibility acceptance remain findings-review decisions
after the required new primary and independent replica evidence.


### Refreshed independent replica preparation 04 — 10:26 UTC

Screening (a): reproducible build identity, complete bounded trials and retained
failures are shared VM/emulator obligations. Screening (b): this preparation
uses the already reviewed native capability and synthetic checks; it adds no
platform capability claim. Screening (c): the exact producer 05 / oracle 04 /
supervisor 01 composition and all previous finite applicability limits apply.
Reviewer `/root/track01_understanding_check` authored no Q33 producer or oracle.
The replicator explicitly discloses oracle authorship and independence from
producer and primary runtime; this is a separate rebuild/reproduction using
the same reviewed oracle, not a second oracle implementation.

Independently verified all 128 replica-build 03 inventory entries, SHA256
`48f997b243f2d73a4ac44b12c12acedde886c616113c510588bf5274779b63a9`,
and all 77 replica-prep 04 entries, SHA256
`4fd2743600d3bf24587a2d1ce7a288a932a3e3cf73b6ebc2b69b1ded1c31d6af`.
The 21 source files and 12 compiled artifacts equal accepted producer 05. The
whole nested oracle, including its separately pinned inventory, equals
oracle 04; the supervisor equals supervisor 01. The preparation-script delta
changes only helper input paths to retained producer 05 copies, preserving the
baseline, negative controls and their contents. All 33 build/preparation
commands and 12 subsequent checker commands passed, with empty checker stderr.

The new Q33 models pass 67,048/132,847 operations at 2/4 CPUs, 21 corruption
controls each. Q32 early0/1/7 two-CPU and early7 four-CPU models pass
55,968/56,800/61,792/128,288 operations and 30/35/37/37 controls. Actual partial
exhaustion retains eight serviced IRQs/seven branches and rejects three
corruptions; it is not successful raw evidence. Q30/Q31/Q32/Q33 and supervisor
self-checks pass; the compiled helper decoder covers the same 153 symbols.
Preparation ran 10:19:23–10:19:56 UTC with no VM.

Verdict: **ACCEPT preparation 04 for the proposed independent 2/4 replication**
from replica-build 03 with oracle 04/supervisor 01. Activation remains held for
parent's explicit serial-runtime handoff after primary 06's full-check result.
The 100-minute deadline begins only at recorded activation; at most 2 VMs and
2 full checks, 45-minute pair admission, 20 GiB activation reserve and 10 GiB
per-new-VM floor remain. The 10-second guest/30-second producer-child limits,
2 GiB producer files, 1800-second checker, 10 GiB process-group RSS sampled each
second and 256 MiB checker files remain. Stop on the first failure, missing
coverage, changed input or resource breach; preserve all outcomes. Old
preparations stay immutable and superseded. This verdict is no findings
acceptance or native activation by the reviewer.
