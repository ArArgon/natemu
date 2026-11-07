# CPU/RAM correctness fixes — November 7, 2026 UTC

Original CPU/RAM batch status: implementation, independent review, integrated project/compiler validation and the 56-boot public campaign
are complete. Supplemental split-lock regressions and failure diagnosis are complete. All 144 planned performance
boots and 18 separately retained outlier-confirmation boots are complete, with verified outputs and no caps or contention.

The baseline is the final no-stop worktree at `129784c2` with its twelve uncommitted tracked fixes and the reviewed
ICEBP correction. The original worktree is preserved. Evidence, frozen baseline/final binaries, patches and source
hashes are under `natemu-raw/cpu-ram-fixes-20261107-01/`. FEX is pinned locally to
`a4689cef16e26618c3697719a2777cbf20cf91d6`; `fex-cpu-ram.bundle` preserves the commits after the old `8d812cadd` pin.
No public test source or litmus7 source was changed.

## Implemented

| Cause | Change | Focused evidence before integration |
| --- | --- | --- |
| Valid vector RAM accesses under x86 alignment checking stopped on an Arm alignment abort | Complete the affected 16/32-byte accesses in the resident, with full preflight, register/writeback and fault/watchpoint handling | 158 checks pass; reviewer independently replayed 36 crossing/fault configurations |
| Waiting on a second split-lock page could discard protection of the first | Recheck the entire page set after servicing resets or waits; shared by vector completion and existing page-access paths | Three protocol-model tests plus a three-vCPU stress test at both block sizes; reviewer replayed both |
| BT/BTS/BTR/BTC accessed only the selected byte | Access the full 16/32/64-bit unit, including LOCK width, page/canonical boundaries and watchpoint footprint | Included in 2,134-case instruction-form matrix |
| INSERTPS/VINSERTPS could omit a required source read when its zero mask discarded the inserted lane | Always evaluate the memory source | Included in the instruction-form matrix |
| Legal memory carry rotates and some 16-bit forms were refused | Execute RCL/RCR dynamic/zero counts and 16-bit SHLD/SHRD CL counts 0–16; admit 16-bit PUSHF and LEAVE | Matrix 2,134/2,134; independent replay 310/310 |
| MASKMOVQ was refused | Resident masked store with MMX gating, pending exception checks, physical-register access, fault rollback and successful TOP/tag updates | 85/85 at each block size; independent 85/85; existing masked-move checks 564/564 |
| Masked x87 exception flags became visible before a later memory fault; stack overflow could gain a lower-priority denormal flag | Keep flags pending until successful instruction completion, including the all-masked path | 27 focused cases pass after 17 baseline failures; I6 302 and I5c 255 pass at each block size; independent replay confirms raw status words |

The vector synchronization defect was found during review and fixed before acceptance. Review also caught a
`66 + REX.W` double-shift classification mistake; the final implementation keeps those operations 64-bit.
The public ASM adapter now executes newly supported forms. It classifies a dynamic 16-bit double shift as undefined
only after observing its actual masked CL count above 16 and the corresponding resident #UD.

## Evidence limits and remaining work

The [correctness backlog](correctness-backlog.md) consolidates current unsolved causes, evidence gaps and explicit
deferred/won’t-fix dispositions. Fixed failures are excluded; the detailed results below remain the evidence record.

The native x86 reference machine remains unreachable. Fresh probes establish that the read-modify-write page-fault
discrepancy also affects nonzero shifts and ordinary ADD, not only zero counts: QEMU reports a read/not-present
fault where natemu's existing write-intent preflight reports a write fault. Mixed-protection page crossings can
also disagree on the fault address. The primary manuals consulted do not settle the tested access-order choice;
this remains an oracle discrepancy, not a proved defect in either emulator. Details follow below.
MASKMOVQ simultaneous-fault priority follows the existing resident policy and is not newly native-verified.

The following are still implementation gaps, not x86 architectural limits: the EL0 alias of the FEX frame page;
memory-destination VCVTPS2PH (conversion flags must not precede a destination fault); nested ENTER and 16-bit ENTER
(partial stack effects, width and final stack probe); FBLD/FBSTP (BCD helpers and precise rounding/store/pop behavior);
address-size-prefixed BT with a register bit index; and operand-size-prefixed indirect CALL/JMP/RET. The latter two
retain their refusals pending explicit address/target/stack-width evidence. Broad x87 representation, MMX image and
metadata redesign remain outside this batch. Undefined double-shift counts above the operand width retain the
existing refusal policy. I/O and compatibility mode are outside this CPU/RAM validation scope.

## Correctness-question follow-up and profiler repair

November 7 follow-up evidence is under `natemu-raw/correctness-questions-20261107-01/`. No public source was patched.

**Guest-state access is an explicit alias, not an EL1 permission bypass.** The resident `State` at `STATE_VA`
remains EL1-only. A separate page at `FRAME_OFFSET` holds the FEX frame/guest-state fields. It has both the private
EL1 mapping `FRAME_VA` and an EL0 read/write mapping (originally `GUEST_STATE_VA = 0xffff400000001000`), used through generated
code's `x28`. `memory::guest_state_alias` maps that same physical frame page in both supervisor and user shadow
roots, with execution disabled. Ordinary translated guest accesses can reach the alias because it is mapped under
Arm's address rules despite being noncanonical for x86. Two fresh load/store probes reproduce incorrect success;
nine boundary controls correctly raise #GP. The mapping unit test passes and independent review confirms that
the exposed page is the FEX frame, not the whole private `State` (`guest-state/`).

**Measured mitigation follow-up:** evidence is under `natemu-raw/frame-alias-guard-20261107-01/`.
A two-instruction check of each dynamic guest memory address fixes the tested alias accesses, including atomics,
vectors and bulk stores, but all 72 public workload boots show a repeatable cost. Across six workloads, the
paired-median geometric means are +8.73% cold/+9.17% warm at one vCPU and +10.69%/+14.07% at four;
four-vCPU locks and build warm medians are +26.58% and +24.35%. All 132 standing steps have results; obsolete
expectations of successful alias access caused eight failing steps. The six event/privilege/K2 groups and the
41 affected fault cases at each block size were corrected and rerun successfully; both full fault sweeps already
had results for all 7,917 cases, with only the two obsolete alias expectations failing in each.
The full guard source, binaries and reversible patch remain archived, separate from the selected fallback.

The owner accepted randomization if the guard was expensive. The current implementation selects one of all
134,217,728 pages in `0xffff400000000000..0xffff408000000000` using OS entropy once per VM, with the same VA
and separate physical frame backing for each vCPU. It adds no per-memory-access check. **The chosen page remains
guest-accessible:** this is an accepted collision mitigation, not architectural correctness or an isolation boundary.
The legacy address is still eligible; tests must inspect or deliberately select the actual page. Test-driver-only
overrides exercise selected-page deviations, neighboring faults and trusted internal frame accesses explicitly.
Randomization's fresh full suite completed **134/134 accepted steps**, with no suite reruns. Both 7,917-case
fault sweeps match, Python checks are 346/346, and public FEX ASM retains exactly 2,077 passes, 48 precision
deviations and 145 unsupported outcomes at each block size. One QEMU coherence reference timed out on its
first attempt; its retry succeeded and all 40 coherence comparisons completed. The two default-ignored host
fixture checks were executed separately and passed. No natemu case was omitted because of a stall or failure.
An initial isolated unit run lacked the existing `cpu_profile.json` fixture; the retained rerun with that
byte-identical fixture passes (host 123, ABI 67, driver 6), without an implementation change.
Focused randomization checks pass 41/41 at each block size; independent first/last-slot and N4 replication
passes, and 509 original frame-field/containment judgments pass with the actual legacy alias selected.
The separate **24/24-boot comparison** completed with valid cold/warm outputs, no caps or contention, and
unchanged frozen inputs. It uses the same unguarded compiler for both variants, matched original/randomized
host and resident binaries, fresh OS entropy, and two balanced pairs per workload/vCPU count. Changes in elapsed
time below are paired medians; positive means slower:

| Workload | N1 cold | N1 warm | N4 cold | N4 warm |
| --- | ---: | ---: | ---: | ---: |
| mem | +0.17% | +0.04% | +0.03% | +3.31% |
| locks | +0.77% | +1.91% | +1.04% | +3.26% |
| build | +0.35% | +0.12% | +3.02% | −0.71% |

Across these three workloads, geometric means are +0.43%/+0.69% at N1 and +1.36%/+1.94% at N4 (cold/warm).
For the **same three workloads**, the earlier guard comparison measured +8.01% N1 warm and +17.84% N4 warm;
the six-workload guard means above describe a larger set. Randomization is the selected lower-cost mitigation.
It is not free: N4 memory warm rose 2.80–3.83%, locks warm 1.49–5.03%, and build cold 2.22–3.82% across the two
orders; guest timers corroborate the signal. Two pairs do not isolate its cause or prove negligible overhead.
CPU endpoint samples were frequently unavailable/late and are not used for attribution. All 96 originally
bounded workload boots have results; the unused guard-confirmation/profile reserve was explicitly reassigned
to the randomization comparison, not silently omitted. Independent audits and raw results are retained.

After measurement, stale blanket fault/isolation comments and the compiler's `guest_state_at_fault` description
were corrected. The final compiler in `randomization/final-metadata/bin/fexc` passes 38 compiler tests; all 70 fixed
requests produce byte-identical objects/statuses/extents in both report and lean modes (140 comparisons).
Only the intended description differs in report JSON; lean JSON is identical. Frozen measurement binaries remain
unchanged. The standing build instructions now select this corrected-report compiler.

**MOV SS/watchpoint → ICEBP has no confirmed remaining DR6-merge defect.** Twenty focused executions still pass
without stalls. Intel's INT1 description says it does not set DR6 bits; the pending-data-breakpoint loss rule and
privileged-software-exception injection rule support the current discard behavior. The previously considered KVM
native reproducer actually uses TF + MOV SS + CPUID, not ICEBP. Current QEMU 11.1.2 delivers the watched-MOV-SS
trap before the following opcode, so it cannot establish correctly deferred cause merging. Eight corrected QEMU
controls confirm that limitation. Eight initial attempts plus one trace had a diagnostic dump/watchpoint problem
and remain explicitly invalid oracle attempts. Exact native ICEBP confirmation remains unavailable.
See [Intel SDM](https://cdrdv2-public.intel.com/671200/325462-sdm-vol-1-2abcd-3abcd.pdf), October 2025,
INT1 p.3-460, §19.3.1.2 p.19-10 and §28.7.3 p.28-24; exact evidence is `debug-rmw/ANALYSIS.txt`.

**RMW fault ordering remains unresolved against hardware.** Twenty-six QEMU system-mode cases and 52 natemu
executions (default/one-instruction blocks) complete without stalls or caps. Fifteen case definitions differ;
eleven agree. An absent CPL3 operand gives error 4 (read) in QEMU versus 6 (write) in natemu, including ordinary
ADD. A mapped read-only operand gives error 7 in both. Across a read-only first page and absent second page,
tested RCL/SHLD/SHL forms report the second-page read fault in QEMU but the first-page write-protection fault in
natemu. All natemu fault cases preserve original registers/flags. These tests locate the discrepancy in the
existing read/write preflight policy; they do not justify changing it to match QEMU without native evidence.
The next useful oracle is these same fault-record probes on native Intel/AMD (`debug-rmw/rmw-results.json`).

**Retained FP-flag failures now have narrower explanations.** Ten QEMU references and twenty natemu executions
reproduce five extended-representation failures and pass five bounded controls at both block sizes. Tiny extended
inputs become zero before arithmetic (missing inexact/underflow); large finite inputs become infinity (missing
overflow or extra invalid); `1 + 2^-63` becomes `1` (lost nexttoward direction). All 82 retained unique narrowing
flag-failure clauses were independently re-extracted and involve extended extrema/subnormals. The glibc 2.41
[nextafterl implementation](https://raw.githubusercontent.com/bminor/glibc/glibc-2.41/sysdeps/i386/fpu/s_nextafterl.c)
deliberately performs arithmetic to raise range flags, which this earlier information loss changes. No separate
bounded-range flag-publication defect was isolated. Synthesizing arithmetic flags at FLD m80 would be incorrect;
the underlying representation needs fixing. Capped historical excerpts leave unretained assertions unclassified,
and hardware was unavailable (`fp/analysis.json`).

**Profiler counter collision repaired.** Fast SYSCALL/SYSRET rejection reasons now have dedicated counters and
appear as `k1.systems.declined.{not_current,boundary,other,after}`; instruction counts retain `systems.kinds`.
The regression passes and fails when the old aliased increment is restored. ABI tests: 67 pass; host tests:
122 pass, two existing fixture-dependent tests ignored. Initial sandbox-only shared-memory failures disappear
when rerun with the required permissions. Matched host and plain/perf resident builds pass. Two N4 locks boots
verify cold/warm results; the profiled boot records 3,981 declines separately and zero false 0x7c–0x7f instruction
counts. Ten focused live checks preserve the real MASKMOVQ/ROTATE/DOUBLE_SHIFT16 counts. Independent source and
live review pass. Evidence: `natemu-raw/cpu-ram-perf-diagnosis-20261107-01/profiler-counters/`. Existing records
remain immutable; mixed old counters cannot be recovered from their totals. This is functional validation,
not a new performance comparison or a full public-suite rerun.

## Integrated validation

The full 130-step project suite now meets its acceptance rules. The first run had 120 accepted steps and ten failures:
eight steps used the obsolete selected-byte BT model (130 failing cell executions), and two lacked the generated
64-program reference file in the new worktree. The model was corrected against the architecture and existing QEMU
fault records, with negative controls that reject the old byte model. The reference file was restored after proving
its programs byte-identical. All ten affected steps passed fresh reruns, together with release and Python checks
(12/12). No original fault case was removed; four previously skipped BT crossing probes and one newly exposed
page-permission case were added. All twelve serial steps passed on their first attempts.

Python checks: 335/335. Compiler unit checks: 48/48; pipe/channel and both server modes pass, each server mode comparing
1,978 requests in both orders. Static site verification: 64 programs, 792 sites, zero errors. Precision verification:
64 fixtures, 1,622 compiled matrix forms and 37 adversarial programs, zero violations. All remaining 66 matrix forms
return explicit resident-completion boundaries: 42 zero-count and 24 CL-count memory RCL/RCR forms, including FS/GS.
They are covered by the passing runtime forms matrix; none lacks a compiler response. The existing seven QEMU two-NaN departures
at each block size match exactly the suite's established exception list.

Unchanged FEX ASM at default and one-instruction blocks: each run has 2,270 outcomes — 2,077 pass, 48 documented x87
precision deviations, 145 explicit unsupported cases, zero unexpected failures. The adapter's old successful exit
after printing failures was fixed and its exit behavior checked before these runs. The two normally ignored host
checks were also executed: 14 admission fixtures and a ten-request current-compiler pipe/channel replay in three
server configurations pass. The deleted historical installer/build streams were not reproduced.

The normal main-workspace build succeeds. Host, stub and a clean compiler rebuild are byte-identical to frozen
binaries; the runtime's entry and loadable segments are identical, with only unallocated symbol metadata differing.
Original no-stop dirty-diff SHA-256 remains `c7d4346c036e437fee18b49c714cc8afdcd4f5f34fe70910532c9d7d144b580f`.

Related public campaign: rerun unchanged gcc torture and glibc math at one/four vCPUs, all 25 Linux x86 selftest binaries separately
at both vCPU counts, and all 1,318 existing litmus7 tests at four vCPUs. Old hang skip lists are removed from the
project-owned initramfs overlay. Report completion separately from test verdicts, with every failure/cap/skip visible.

The 54 non-litmus boots are complete, with exactly 10,664 expected per-test results and no missing/duplicate outputs,
VM stalls, caps or contention. At each of one/four vCPUs: gcc torture has 3,440 passes and the same eight historical
x87 failures; glibc math has 1,473 passes, the same 332 failures and 54 AVX-512 profile skips; x86 selftests have
20 successful programs, two contained compatibility-mode failures and three unavailable-profile outcomes. MOV SS →
ICEBP succeeds at both vCPU counts. All eight old gather exclusions execute and pass at both counts.

The 332 math failures are not all independent function failures: 290 stop in common extended-subnormal setup before
their named function body; 30 narrowing programs report result/errno/FP-flag incompatibilities; the other twelve cover
one infinity classifier, three extended-range/nextafter cases, two nexttoward direction cases, three gamma-accuracy
cases, two nonnormal-encoding preservation cases and one NaN-payload case. These mechanisms are detailed per program
in `independent-audit/libm-failure-classes.json`. At batch acceptance the FP-flag assertions in narrowing and nextafter
tests were not individually attributed. The follow-up above explains retained range-loss mechanisms with targeted
probes and bounded controls; it does not establish every unretained assertion or native result. These incompatibilities
are distinct from this batch's fixed faulting-memory flag publication. Retained failure excerpts match the baseline
at both vCPU counts. The old
excerpts are capped at sixty lines per program, so this does not establish equality of every internal assertion.

All 56 public boots and 11,982 expected per-test results are present, with no duplicate/missing results, caps or
contention. All 1,318 litmus tests completed with zero skips, execution failures or forbidden states. Independent
checks confirm every histogram/work count and the unchanged oracle. Of 13,403 allowed states, 334 were not observed
across 250 tests; 210 of 340 allowed exists-conditions were not observed. These are coverage limits, not forbidden
outcomes. The public test sources and litmus7 remain unchanged.

Supplemental regression ran all seven existing stress-ng split-lock/misaligned selections separately at one/four
vCPUs (14 boots), preserving the original image and operation targets. Every planned result is present, with no
VM stalls, caps or contention. Twelve are valid full-target passes. The two original `lockbus --lockbus-nosplit`
selections exceed the public stressor's allocation: N1 crashes with SIGSEGV; N4 reports success after writing beyond
the allocation into adjacent writable mappings, so its apparent pass is also unsuitable correctness evidence.

Diagnosis retained in `lockbus-diagnosis/` proves this is the unchanged stress-ng 0.19.02 workload's pointer overrun,
not a new natemu regression. Frozen baseline and final reproduce the crash. A validated standalone ptrace observer
shows natemu and QEMU fault on the same locked-add instruction (`0x69802e`), crossing into a read-only mapping after
leaving the 16 MiB buffer. Source and disassembly show both misaligned pointers advance 32 bytes per iteration
without wrapping. The first out-of-allocation access occurs in iteration 524,288; adjacent mappings delay the actual
fault. The original N4 target also exceeds that bound per worker. Missing bogo metrics reflect failed-worker metric
suppression, not zero work.

Four separate controls use the unchanged public binary with 500,000 total operations: baseline/final at one/four
vCPUs all complete their full targets with every worker passing and zero skips/failures. The project-owned selection
list and its calibration tool now enforce this allocation-safe bound; recalibrating against the retained native
output preserves 500,000 for nosplit and the lower split count. No public source is patched. Original results remain unchanged and are
not relabeled as passes. The historical 240-second stressor timeout, 400-second outer kill and 450-second boot cap
remain. These supplemental results do not alter the completed 56-boot public plan.

Performance uses the existing eleven fixed-work benchmarks plus unchanged stress-ng long-double add/multiply/divide,
at one/four vCPUs, three alternating baseline/final rounds: 144 boots, each cold and warm. Outputs must verify;
compile caching is off; timed boots run exclusively with no builds or other VMs. The x87 workload checks repeated
result consistency, not native 80-bit accuracy. It specifically measures the added x87 flag-commit bookkeeping.
Baseline sizing is excluded from paired results. Every planned cell must have exactly one valid output before
reporting a complete campaign.

Host: Apple M4, ten CPU cores, 16 GiB RAM. All 144 planned boots completed exactly once with both runs verified, no caps or contention, and unchanged binary
hashes. The following changes are elapsed time after/before (positive is slower). General-workload figures are the
equal-weight geometric mean of each of the eleven workloads' median paired ratios over three rounds. The x87 row is
the dedicated workload's median paired ratio. Cold and warm results remain separate.

| Workloads | N1 cold | N1 warm | N4 cold | N4 warm |
| --- | ---: | ---: | ---: | ---: |
| Eleven general workloads | −0.10% | −0.18% | +2.48% | −0.32% |
| x87 add/multiply/divide | +0.07% | +0.70% | +0.16% | −1.27% |

The original N4 cold medians for hackbench, locks and memory were +5.05%, +6.42% and +6.09%; their warm medians were
+1.10%, +0.30% and −5.61%. These observations are retained, not discarded. A separately reviewed twelve-boot
confirmation of hackbench/locks gave cold medians +2.84%/+2.54%, with paired ranges −4.07..+3.60% and −2.04..+9.31%;
warm medians were −2.97%/+0.76%. This shows substantial variability and does not establish a stable 5–6% regression.
The memory outlier appeared after that two-workload plan was frozen. Its separate six-boot confirmation gave −2.18%
cold (range −4.35..+0.30%) and +3.64% warm (−0.77..+4.11%), reversing the original direction. None of the initial
5–6% cold outliers reproduced at that magnitude in its separate confirmation; the original N4 aggregate remains
+2.48% cold. These short, three-pair comparisons establish observed variation, not a universal zero-cost claim.
No confirmation samples are pooled into the original aggregate. Complete per-workload samples and ranges are in
`perf/summary.json`; the confirmation runs remain in `perf-confirm/` and `perf-confirm-mem/`.

Profiling follow-up (November 7): forty additional verified boots included runs with profiling enabled and disabled,
using the existing host request profiler, resident time buckets, 5 ms snapshots and process CPU/instruction/P-core
counters. Twelve pilot boots covered
hackbench/locks/memory with profiling on/off; sixteen locks controls separated host, resident and compiler changes;
twelve more boots tested only the BT lowering change. The original 162 samples remain separate. Compilation
service time does not explain the locks increase. A three-pair compiler-only contrast showed +1.15..+2.73% cold
time and +2.14..+3.63% instructions, but the six BT-only pairs did not establish a stable penalty: median +0.54%
cold (range −6.34..+2.40%), +0.72% warm (−5.43..+1.75%), and +0.085% cold instructions. In all six cold pairs,
the second run was slower regardless of which compiler ran second, with matching CPU/instruction signs.
The whole-boot syscall-count direction also changed between profiling comparisons. These observations expose
variable executed work in this contended benchmark; they do not identify a patch-level cause or prove zero overhead.
No correctness fix was reverted in the accepted build.

The old observer recorded timestamps before process queries and serial reads; its uncertainty was not bounded by
the nominal 50 ms polling interval. The diagnostic observer records read brackets separately. Resident buckets
include descheduling, warm compiler CPU observations can disappear at process exit, and system counters
0x7c–0x7e in these frozen logs overlap decline reasons and cannot identify the newly supported forms. The subsequent
counter repair above changes new logs only. One component-control profile
has a retained snapshot monotonicity anomaly. Matching runtime loadable bytes, compiler control reproduction,
input hashes, per-run outputs and independent reviews are retained in
`natemu-raw/cpu-ram-perf-diagnosis-20261107-01/` (`controls-review.json`, `ablation-review.json`,
`analyzer-provenance.json`). A specific CPU/RAM patch responsible for the original aggregate remains unproven.
The project runner `tools/bench/run.py` now brackets serial reads, consumes complete lines and captures final-drain
markers. Existing point-time fields remain, with additional marker and elapsed-time bounds; the 50 ms polling
interval and process scans still contribute uncertainty. Three focused regressions and two further real N4
locks/memory boots pass (both cold/warm outputs, timing bounds and memory phases); independent review and live
artifacts are in `observer-review.json` and `observer-check/`. Public sources and CPU runtime code are unchanged
by this profiling follow-up.

Independent final audit accepts all 162 timed boots and all forty frozen input hashes, the complete public/oracle
results and the bounded split-lock controls. Acceptance is retained in `independent-audit/final-review.txt` and
`final-audit-manifest.json`; the integrated project-suite audit is `review-suite-audit.json`. All planned cases have
outputs. Known precision departures, unsupported forms, public-profile exclusions and the unsafe stressor inputs
remain classified above rather than being counted as ordinary passing cases.
