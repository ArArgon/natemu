# INV-01-08 — findings review thread

## Independent replication author preparation — 2026-09-05

Author/context: `/root/ordering_native_review`, now acting as the independent
replication author, separate from original collection author `/root` and runner
patch author `/root/mach_native_review`. This context previously reviewed F08
method/code but authored no load-bearing F08 implementation/checker change.
**It will not independently approve this preparation or its own replica findings.**
A different reviewer must check this preparation before native execution and
adjudicate findings afterward. No replica native execution has occurred.

Read the research workflow, current INV-01-08 F08-2 method and accepted full
pre-execution/B1 return. The first campaign is
`/Users/liaozping/orca/natemu-raw/f08-run-20260905-track01-a`, with run identity
`a6ca1c3a9aab4dbcbbf4903b7364261d` and retained source-bundle identity
`c3ad115e8d12237c8ab73deaac6bb0c530ba6367ab066359893f9109d97a0621`.
Its aggregate manifest reports completed/exit 0, no timeout/output limit and
all five modes completed. The coordinator must still confirm its live handle
58783 terminal before another native launch; an artifact alone is insufficient.

Independently recomputed all 17 retained build-artifact hashes, every source
snapshot hash and all 66 aggregate campaign-artifact hashes. The source snapshot
contains the actual Makefile/native source/headers, Python runner/checkers and
clock dependency; SDK/toolchain/commands and disassemblies are retained. The
repository's clock dependency currently matches the accepted copied input.
No build or source mutation is proposed for replication.

Read the five actual child receipts and raw control counts: normal and sequential
negative both exit 0 with 1,000,000 completed/write/final increments and four
created/joined fallback workers; omitted-store exits 1 with exactly 999,999
writes/final value and 1,000,000 completed operations; injected lock error exits 1
with consistent three completed peer writes in this particular run; unexpected
fault exits 90. All report no timeout. Every timing classification is unqualified.
These are preparation observations, not accepted findings or a substitute for
fresh independent measurement. No contradiction or failed sample was found to
rescue or replace.

### Exact fresh replica plan

The fresh output destination is absent:
`/Users/liaozping/orca/natemu-raw/f08-run-20260905-track01-replica-a`.
Invoke the retained author's reviewed supervisor to create its own new source
snapshot, fresh binary/build manifest and distinct run identity inside this new
campaign. This uses the accepted runner's normal preparation path, not a copied
binary or handcrafted producing-build record. The explicit repository environment
preserves original output exclusion and local clock-dependency lookup.

```sh
NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw NATEMU_F08_REPO=/Users/liaozping/orca/natemu python3 -B /Users/liaozping/orca/natemu-raw/f08-run-20260905-track01-a/producing-build/snapshot/src/supervisor.py --directory /Users/liaozping/orca/natemu-raw/f08-run-20260905-track01-replica-a --campaign
```

No `--clock` is supplied: correctness is independently checked; timing remains
unqualified. Preserve exactly one full normal child, sequential negative,
omitted-store, injected lock-error and unexpected-fault, in that order. The full
normal includes all 12 two-thread litmus arms, six LSE arms at 2/4/8 workers, six
alignment cases, four fallback workers ×250,000 increments and two QoS handshake
arms ×20,000 attempts. No reduced-count, repeated, substituted or discarded cell.

Preserve native child watchdogs 600/600/60/60/5 seconds (1,325-second subtotal)
and the outer 2,700-second/128 MiB operational thresholds over fresh build,
execution and checking. The 20 ms output polling can retain overshoot as explicit
failure diagnostics; it cannot become a successful result. The first child,
control, build, integrity, aggregate-budget or contextual failure stops the
campaign, retaining partial checksums and not-run modes. No retries or
replacement campaign are authorized by this preparation.

The reviewer must verify this exact execution/check path and retained inputs,
then the coordinator confirms no overlapping native experiment before launch.
The parent—not this author's earlier reviewer role—controls sequencing against
M18/other native work. A completed replica must retain the actual before/after
platform, API/signing/QoS/load state and compare it explicitly with the author.
One-host 2/4/8-thread evidence establishes no full guest TSO, mixed/overlapping
access protocol, oversubscription, P1 applicability, KA-06 pass or cost advantage.

**Preparation ready for independent review.** Native replica not run.

Preparation finalization: the coordinator has now polled first-campaign handle
58783 and confirmed terminal exit 0 and no active native experiment. After that
confirmation, independently ran the retained source snapshot's `src/check.py`
on the author's normal result. It returned exit 0 with I1–I14 passing, full
same-build five-control/aggregate binding accepted, and `TIMING UNQUALIFIED`.
The plain SB controls recorded 27 user-interactive and 3,054 background relaxed
outcomes. This is offline checking of retained evidence, not a replica launch.
The exact command, fresh destination and unchanged limits above are final for
independent preparation review; no native replica has yet run.

## Independent replication preparation review — 2026-09-05

Reviewer/context: Codex `/root`, distinct from replication executor
`/root/ordering_native_review`. I authored the first collection/report and am
therefore reviewing only the independent executor's preparation here, not
accepting my own first-campaign findings. Final scientific findings require a
reviewer independent of both collection authors.

Inspected the exact proposed command, retained supervisor outer/inner build
path, source lookup/boundary behavior and v3 consumer admission. Independently
verified all 66 first-campaign and 17 build artifact hashes and every retained
source hash. The local clock dependency matches the retained source; no input
change is proposed. The fresh replica path does not exist. The existing
runner builds a fresh snapshot/binary/run identity inside the 2700-second and
128 MiB aggregate envelope and checks exactly the fixed five children with
1325 seconds of native child ceilings. No external clock packet is supplied,
so timing remains unqualified. Accepted code reviews and synthetic controls
apply unchanged to this execution path; no new build-copy mechanism is used.

The first campaign's process handle 58783 is terminal exit 0; its aggregate
outcome has all five modes completed, no timeout/output cutoff and immutable
partial/control records. M18's preflight is also terminal. No native research
campaign is active at this review point. The independent executor will stop
on the first unexpected outcome without replacements and preserve actual
context and all failures. No unresolved execution-validity or reproduction
blocker was found. **Accept this exact replication preparation.** This verdict
accepts no F08 finding, timing magnitude, general TSO property or KA-06 disposition.

## Independent replica executor report — 2026-09-05

Executor: `/root/ordering_native_review`; this is author reporting, **not a
findings verdict**. Following the distinct reviewer's preparation acceptance and
coordinator no-overlap confirmation, ran the exact retained-source command above.
An initial filesystem sandbox refusal created no destination or native process;
the unchanged command launched only after external-write escalation. Live handle
65104 subsequently returned terminal exit 0. No other native experiment was run
by this executor, and no outcome-driven retries/replacements occurred.

[MAN-01-08-05](../manifests/MAN-01-08-05.md) retains replica run
`2e5c34da754c4b79877fb4505bb2cc58` at the planned fresh external path. The
fresh producing build has the same reviewed source-bundle identity as the author,
with 17 retained build-artifact hashes and 66 campaign-artifact hashes verified.
All five actual children passed their designated normal/control classifications;
no timeout, aggregate output stop or incomplete/not-run member remains. The
outer operational interval was about 14.26 seconds, not a timing finding.

The executor reran the replica's retained check.py against its normal result:
exit 0, I1–I14 PASS, exact campaign/build/control binding accepted, timing
unqualified. Normal fallback completion/write/final counts are 1,000,000 with
four successful workers and unchanged canaries. The omitted-store control has
exactly 999,999 writes/final value. Lock-error control has three scheduler-dependent
peer increments in both runs, with worker 0's actual injected branch preserved;
this agreement is not a scheduling guarantee. Unexpected-fault exits 90 without
normal raw output. Both 20,000-attempt handshake arms complete with zero timeouts,
and all LSE 2/4/8-worker counters retain expected values.

The independently prepared replica reproduces the author's fallback correctness,
failure-path and categorical alignment observations on the same local host/OS
context. Plain-SB exposure counts differ: author 27/3,054 versus replica 6/4,642
(user-interactive/background). These rates are harness outcomes, not stable host
constants. Strengthened forbidden counts remain zero in each run, but that alone
does not establish full TSO or supply missing histogram-comparability evidence.
No cohort pooling, causally attributed ordering-strategy effect or timing
qualification is proposed here. The exact raw differences remain retained.

Independent reviewer `/root/publication_native_review` must inspect both author
and replica findings and adjudicate their bounded conclusion. This executor's
prior native-method review supplies no acceptance of its own replica findings.

## Independent findings review — 2026-09-05

Reviewer: `/root/publication_native_review`, independent of both collection
and replication authors and of the F08 implementation. Read the workflow,
current investigation/method, pre-execution return, both author manifests and
reports. Inspected the retained native fallback, fault handler/control,
histogram and evidence consumers, actual five-child raw records, producing
commands and before/after context. Independently recomputed all 66 campaign
and 17 build artifact hashes for **each** campaign and reran each retained
checker offline: exit 0, I1–I14 PASS, timing unqualified. This review launched
no native experiment.

**Accept the bounded correctness/count and categorical findings below from
[MAN-01-08-04](../manifests/MAN-01-08-04.md) and its independently prepared
[MAN-01-08-05](../manifests/MAN-01-08-05.md) replica.** Distinct run identities
bind fresh builds to the same retained source bundle. Both contain exactly the
five prescribed children in order, with no timeout, output cutoff, replacement
or excluded member. The 2,700-second/128 MiB aggregate receipt and child
600/600/60/60/5-second ceilings apply; actual aggregate operational elapsed
intervals are 14,345,389,125 and 14,259,708,333 ns, with 653,446 and 653,789
pre-receipt bytes. These figures verify bounded execution only.

The actual fallback is one mutex protecting a four-byte WB target at offset
78, with four joined workers and explicit per-worker lock/unlock/error/write
accounting. Both normal collections have 250,000 completed writes per worker,
1,000,000 total completed operations/writes/final value, successful mutex/thread
lifecycle records and intact canaries. The sequential negative triggers exactly
the intended concurrency integrity failures while retaining full fallback
counts. The omitted-write control exits 1 with 1,000,000 completed operations
but exactly 999,999 writes/final value. The injected lock error exits 1 with
worker 0's first lock returning injected EIO before target access; the three
peer writes are consistently accounted in both runs, but that peer count is
scheduler-dependent. These controls exercise actual native branches and raw
accounting, rather than merely assigning expected labels.

The unexpected-fault control maps owned PROT_NONE memory, installs the same
handler with `g_in_probe=0`, and actually loads that mapping. Exit 90 with no
normal result is consistent with the handler's unexpected-fault branch; the
source's fall-through exit is 92, so ordinary completion cannot produce the
accepted control outcome. Alignment recovery is restricted to an armed
four-byte address range. All six cases agree between collections: aligned
LDADD and offset-65 within-granule LDADD complete; offset-78 and offset-126
LDADD and offset-126 LDAR produce SIGBUS/BUS_ADRALN; ordinary offset-126 LDR
completes. This is observed behavior for these particular instructions,
addresses and platform, not a general unaligned-access guarantee.

All twelve litmus arms retain their full million outcomes, the six LSE
operation/worker-count arms retain the expected 2/4/8-million final values,
and both QoS handshake arms complete 20,000 attempts with zero recorded
timeouts in each campaign. Plain SB relaxed outcomes are 27/3,054 in the
author and 6/4,642 in the replica (user-interactive/background). All strengthened
SB/MP forbidden counts are zero. Recomputed the predeclared histogram
comparisons **separately for each campaign**, without pooling or replacing a
cohort:

| Comparison | Author | Replica | Admissible interpretation |
| --- | --- | --- | --- |
| Background MP, LDAR/STLR | COMPARABLE; plain exposure 51, strengthened 0 | COMPARABLE; plain exposure 39, strengthened 0 | Bounded corroboration under the declared exposure rule |
| Background MP, DMB | INDETERMINATE; ratio 92358/3637 | INCONCLUSIVE; ratio 92227/2749 | Classification differs; neither establishes a strategy effect |
| User-interactive MP, LDAR/STLR | COMPARABLE, plain exposure 0 | COMPARABLE, plain exposure 0 | No plain forbidden exposure, so no eligible corroboration |
| All four SB comparisons; user-interactive MP DMB | INCONCLUSIVE | INCONCLUSIVE | Retain finite observations only |

No stable exposure rate, forbidden-outcome probability bound, impossibility
proof or causal ordering advantage follows from these results. Both captures
are on the same Mac16,10/M4 arm64 host, macOS 26.6.2/build 25G83, SDK 26.4 and
clang 21, with the retained ad-hoc build/signing context and requested QoS.
Physical core placement and inherited application restrictions are not
established. Matching host context is replication here, not broader platform
coverage. No applicable clock packet is present: **every timing magnitude,
percentile and timing-based threshold remains unqualified**, regardless of
positive interval/checker fields.

There is no unresolved findings blocker for this bounded F08-2 result. Full
guest ordering, mixed or overlapping accesses, nonparticipating accesses,
oversubscription and P1 applicability remain outside it; KA-06 and the
investigation's broader question remain open. Incorporating these accepted
findings must preserve those obligations and the exposure-classification
difference above. Any later timing claim needs applicable clock evidence;
any broader fallback/guest protocol requires its own reviewed method and
independent execution evidence.

## Independent F08-WB1 source findings review — 2026-09-05

Reviewer/context: `/root/track01_inventory_check`, separate from F08-WB1 author
`/root/mach_baseline_replication`, and with no authorship of the F08 native
instrument, its replica, or this source argument. Read the research workflow,
INV-01-08 current summary and complete F08-WB1 return, the accepted F08-2
findings above, and INV-01-12's current applicability and controls. This is a
source/inference review; no experiment or native executable was launched and
no author investigation text was edited.

**Return F08-WB1 for one source/inference correction, F08-WB1-R1 below.**
The conditional fault-free fragment argument and participant/CMODX obligation
map check out. The submitted partial-fault counterexample overstates its source,
however, and is part of the packet's stated stopping condition. No complete
software-serialized guest implementation, KA-06, performance result, production
design, or follow-up execution is accepted by this review.

Independently inspected the retained Intel SDM Volume 3A revision 092US
§§11.1.1–11.1.2 and 11.2.3, and decoded the retained Arm DDI0487M.c B2.2.1,
B2.8.2 and B2.2.5 JSON `content` fields. Checked the current primary C11 draft,
POSIX synchronization clauses and original x86-TSO paper. The direct POSIX open
request failed, but its primary publisher's indexed page supplied the actual
4.15.2 text and mutex-function contract; no secondary summary substituted for
that contract. These sources support the following adjudications:

- **Serialized fragment.** Successful acquisition/release of one mutex orders
  participating byte accesses and supplies the host synchronization edge.
  The byte representation avoids the proposed overlapping-atomic-object and
  unaligned typed-access hazards, conditional on every actual access following
  the gate and on stable object/mapping lifetime. C11's race-free interleaving
  note has the restrictions the author states; it does not certify an arbitrary
  pthread implementation. The public POSIX contract supplies the missing API
  premise. Error returns must remain checked; robust-owner-death handling is
  not established by ordinary success-path reasoning.
  [C11 N1570 §§5.1.2.4, 6.5, 7.26.4.3](https://www.open-std.org/jtc1/sc22/wg14/www/docs/n1570.pdf),
  [POSIX §4.15.2](https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/V1_chap04.html#tag_04_15_02).
- **Allowed x86-TSO execution, with a narrow domain.** Reconstructing the
  argument against §3.1's transitions succeeds: immediately propagate each
  ordinary write, use memory reads with empty buffers, and bracket a locked
  instruction with the model lock and its required empty-buffer boundaries.
  Nonlocked read/modify/write events may interleave. This witnesses an allowed
  strongly ordered behavior, rather than every allowed behavior or its
  probability. The paper expressly excludes the exceptional, mixed-size,
  misaligned, non-temporal, self-modifying and page-table cases named in the
  return. The broad participant list is therefore a future invariant, not a
  theorem extending the paper to those cases.
  [Sewell et al., introduction and §3.1](https://www.cl.cam.ac.uk/~pes20/weakmemory/cacm.pdf).
- **F08-WB1-R1 — permitted partial effects do not refute every rollback model.**
  Intel §11.1.1 does allow completed pieces of some
  wide x87/SSE stores to remain visible when another piece faults. This defeats
  a claimed universal architectural guarantee of whole-instruction rollback.
  It does **not**, by itself, mandate a partial effect for every such fault or
  prove an implementation choosing an allowed no-partial-effect behavior
  invalid. The submitted paragraph instead calls it a concrete counterexample
  to treating universal prevalidation plus rollback as a complete ISA
  implementation, then reuses that counterexample as a stopping-condition
  finding. This matters because the fragment argument correctly permits an
  implementation to choose a subset of allowed memory behaviors; the same
  refinement distinction cannot disappear at faults.

  **Smallest remedy:** change the author argument and stopping condition to
  distinguish (a) absence of a universal architectural no-partial-store
  guarantee, (b) a conservative no-partial-effect implementation when that
  behavior is allowed by the specific instruction contract, and (c) mandatory
  effects or fidelity to a selected concrete execution, which need separate
  support. Do not claim that this permissive paragraph proves (b) invalid.
  Keep required partial effects, exception priority, restart and paging side
  effects in follow-up 3. **Closure:** the same reviewer checks all three
  affected author passages and confirms permission is never a required-effect
  oracle. No native run or new experiment is needed for this return.
  [Retained Intel SDM §11.1.1](../../../artifacts/research/track01/source-snapshots/2026-09-03/intel-sdm-vol3a.txt).
- **Mixed accesses, DMA and host code.** One ungated reader can observe an
  intermediate byte copy and an ungated writer can interfere with a fallback
  RMW; neither stronger atomics elsewhere nor a barrier enrolls it in this
  gate. The private-buffer device adapter is appropriately conditional on its
  device contract. Separately, holding the memory gate does not stop an
  executing translation or synchronize that PE's instruction stream. Arm's
  restricted concurrent-modification cases, cache maintenance and executing-PE
  synchronization support the author's CMODX exclusion. An immutable-code
  interpreter removes only host-code modification, leaving guest fetch and
  modification semantics owed.
  [Arm DDI0487M.c B2.2.1/B2.8.2/B2.2.5](https://developer.arm.com/documentation/ddi0487/mc/).

Adversarial applicability checks found no transfer of F08-2's one-address
million-update witness into a range protocol, of Q12's finite restart cases
into arbitrary instruction semantics, or of same-host contention into lock
fairness and platform portability. In particular, a stalled gate owner can
stop other guest memory operations even while vCPUs compute concurrently;
this argument supplies no liveness or economic feasibility bound. The current
F08/Q12 controls and host/signing limits remain intact.

The five follow-ups provide bounded preparation scopes and named owners.
Before any reference-model or native execution, they still need concrete input
values/event semantics, controls and execution/resource budgets appropriate to
that method. No unrun check is accepted here. The useful next action is the
finite admitted-access/profile audit in follow-up 1, stopping at its first
missing primary contract. Preserve the fault qualification above in any
promotion of this packet after R1 is closed. Full guest TSO, CMODX, two/four/eight-vCPU composition,
P1 and scheduling remain open; all F08 timing remains unqualified.

### Same-reviewer F08-WB1-R1 closure — 2026-09-05

Reviewer: `/root/track01_inventory_check`, unchanged and independent of the
author. Reinspected the corrected primary-contract paragraph, fault/commit
argument, obligation table, follow-up 3 and final stopping condition against
the Intel clause already checked above. All three affected passages now
distinguish permitted partial effects, required instruction/profile effects,
and fidelity to a selected concrete execution. They explicitly state that the
permissive clause refutes a universal rollback guarantee, not every rollback
implementation. The missing whole-profile proof and bounded instruction audit
remain open; no source permission is promoted to a required-effect oracle.

**F08-WB1-R1 is closed; accept the corrected F08-WB1 conditional fragment
argument and obligation map.** No other substantive blocker remains to this
source-only packet. The source checks and applicability boundaries above
continue to apply. This accepts no complete guest-memory implementation,
KA-06 disposition, fairness or cost claim, platform-wide result, new Q12
composition, or execution of a proposed follow-up. No native work was done.
