# Unsolved CPU/RAM correctness backlog

Status as of **November 7, 2026**, checked against the current `correctness` working tree, including its
uncommitted repairs and frame-address randomization. This is the consolidated backlog of known remaining causes
and evidence gaps, not a claim that the entire x86 architecture has been audited. The
[implementation plan](../implementation.md) still owns execution order; this list does not schedule every item.
The [current results](correctness-cpu-ram.md) own test counts and retained artifacts.

Scope is x64 CPU execution and ordinary RAM. As requested, I/O and device problems are not enumerated here.
An unsupported valid instruction is an implementation gap even when it now raises a contained #UD instead of
stopping the VM. Conversely, a correct architectural exception is not a defect.

**Statuses:** **Open** = known implementation gap; **Needs oracle** = behavior not yet adjudicated;
**Validation gap** = missing evidence, not a demonstrated defect; **Deferred** = retained limitation outside the
current work or accepted tradeoff; **Won’t fix under current contract** = intentionally excluded behavior or an
inapplicable test expectation. Deferred does not mean fixed. IDs remain stable when status changes.

## Known implementation gaps

| ID | Status | Remaining cause and effect | Next action / closure condition |
| --- | --- | --- | --- |
| COR-01 | **Mitigated; full fix deferred** | The FEX frame has an EL0 read/write alias. Ordinary guest accesses to its selected noncanonical x86 address succeed instead of faulting. Randomization chooses one of 2²⁷ pages once per VM; it does not prevent access if the address is found. | Owner accepted randomization after the full address guard's measured cost. Reopen for a cheaper isolation design or a changed correctness requirement; closure requires selected-page accesses to fault while trusted frame accesses work. [Code](../../crates/host/src/memory.rs), [mitigation and costs](correctness-cpu-ram.md#correctness-question-follow-up-and-profiler-repair). |
| COR-04 | **Open** | FBLD/FBSTP packed-BCD forms are refused. | Implement BCD conversion, invalid input, rounding, store and pop with precise fault/exception ordering. [Admission](../../tools/fexc/shim/fexc_slice1.inc). |
| COR-07 | **Open** | x87 C1 does not report rounding up for affected arithmetic/conversion forms; stack-fault C1 handling is implemented. | Track the round-up result where architecturally defined, including stores and integer conversion. [FEX stack pass](../../third_party/FEX/FEXCore/Source/Interface/IR/Passes/x87StackOptimizationPass.cpp). |
| COR-08 | **Open** | FLDPI/FLDL2T/FLDL2E/FLDLG2/FLDLN2 use fixed nearest-double constants irrespective of FCW.RC. | Specify and test directed rounding separately from the accepted reduced significand; implement appropriate constants/state representation. [FEX constants](../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/X87F64.cpp). |
| COR-09 | **Open** | Generated x87 arithmetic uses the Arm first-NaN choice for two NaNs rather than x87's selection rule. Resident helpers already have a separate `nan2` implementation; this is not a claim that every NaN path is wrong. | Unify the affected generated forms with the selected x87 rule; test operand order, signaling/quiet NaNs and payload ties. [Recorded I6 semantics](boot-features-plan.md), [resident helpers](../../crates/abi/src/x87.rs). |
| COR-10 | **Open** | FLDENV/FRSTOR retain only empty/nonempty tags; FNSTENV/FNSAVE reconstruct the full tag word from doubles. An explicitly restored full tag word is lost. | Preserve full tags and their instruction-specific updates; test environment and register-image round trips independently of numeric precision. [Transactions and `ftw`](../../crates/runtime/src/execution/x87.rs). |

## Accepted or out-of-scope limitations retained as deferred

These are visible limitations, not reasons to label every failing public assertion correct.

| ID | Status / reason | Remaining behavior | Reopen / completion criterion |
| --- | --- | --- | --- |
| COR-11 | **Deferred — owner accepted reduced x87** | ST values/intermediates are binary64 regardless of FCW.PC: reduced precision and exponent range, early rounding of m80 and large integers, lost encodings/payload bits, and consequent result, comparison, conversion and exception-flag differences. Resident transcendental helpers use double libm. | Reopen if full long-double compatibility is required. Close only with an agreed precision/range/encoding and transcendental contract plus public and focused tests. [Conversions/helpers](../../crates/abi/src/x87.rs), [current failure analysis](correctness-cpu-ram.md). |
| COR-12 | **Deferred — state-image redesign outside repaired batch** | MMX/x87 alias images are not bit-exact: MMX writes and 80-bit saves/restores cannot preserve all shared-register bits through double conversion. | Preserve raw register images and MMX tag/exponent rules; rerun the two currently classified MMX/x87 interaction cases. [ASM classifications](../../tools/fexc/upstream_tests/run_asm.py), [save/restore](../../crates/runtime/src/execution/xsave.rs). |
| COR-13 | **Deferred — metadata tracking outside repaired batch** | x87 last instruction/data pointers and opcode are unmodeled; environment/FXSAVE/XSAVE images contain zero metadata. | Add instruction-appropriate tracking and save/restore semantics, including fault cases. [x87](../../crates/runtime/src/execution/x87.rs), [XSAVE legacy image](../../crates/runtime/src/execution/xsave.rs). |
| COR-14 | **Deferred — broader control-transfer support** | 64-bit call gates are unimplemented: CPL3 gets #GP(selector), CPL0 stops as unsupported. This is a long-mode CPU feature, not a compatibility-mode issue. | Reopen when the CPU contract includes call gates; implement descriptor, privilege/stack transfer and precise-fault semantics. [`far_target`](../../crates/runtime/src/execution/segments.rs). |
| COR-15 | **Deferred — x64 scope** | General real/32-bit/compatibility execution and CPL1/2 are outside the current contract. CPL3 compatibility transfers are contained with #GP; boot/AP setup support does not imply general 32-bit execution. | Reopen for compatibility workloads; define the mode/privilege contract and add execution tests before admitting transfers. [Current handoff](boot-handoff.md), [segments](../../crates/runtime/src/execution/segments.rs). |
| COR-16 | **Deferred — profile/capacity expansion** | Features outside the configured CPU profile and platform bounds are not implementation failures of that profile: examples include AVX-512/AMX/CET, PCID/LA57/1-GiB pages, more than four vCPUs or RAM above 3 GiB, and CPU offlining. | Reopen with a profile/platform expansion. Any feature advertised but implemented incorrectly is instead an open defect. [CPU profile](../../tools/models/cpu_profile.json), [feature plan](boot-features-plan.md). |
| COR-17 | **Deferred — speculation hardening** | Remaining paging-result consumer and descriptor-table bounds hardening needs review. These are side-channel hardening gaps, not observed architectural CPU/RAM failures. | Reopen with the hardening workstream; validate generated barriers and the affected paths before changing security claims. [Current decision inventory](../research/investigations/INV-03-01.md), [`descriptor`](../../crates/runtime/src/execution/segments.rs). |

## Unresolved evidence and coverage

| ID | Status | Question / missing evidence | Next action / closure condition |
| --- | --- | --- | --- |
| COR-18 | **Needs oracle** | RMW page-fault ordering: QEMU and natemu differ on read/write error bits and sometimes CR2, including ordinary ADD, nonzero shifts and mixed-protection page crossings. All retained natemu faults preserve state; these are not stalls. | Run the retained 26 probes on native x86 with the required harness; settle the selected contract before changing write-intent preflight. Native reference is currently unreachable. [Results](correctness-cpu-ram.md). |
| COR-19 | **Needs oracle** | MASKMOVQ simultaneous-fault priority follows the resident policy but lacks fresh native confirmation. The instruction is implemented and its focused matrix passes. | Native priority probes for competing faults; change only demonstrated mismatches. [Results](correctness-cpu-ram.md), [implementation](../../crates/runtime/src/execution/maskmov.rs). |
| COR-20 | **Validation gap** | MOV SS → watchpoint → ICEBP: the stall and stale second #DB are fixed. Current manual analysis supports discarding the deferred cause; QEMU delivers the watched MOV SS trap early and cannot settle this exact case. No remaining DR6-merge defect is established. | Retain a native confirmation task for the exact sequence, with DR6 and delivery timing captured. [Latest analysis](correctness-cpu-ram.md), [focused harness](../../tools/run_icebp.py). |
| COR-21 | **Validation gap** | Public x87/libm failures are grouped by causes, but historical capped assertion output is not completely classified. Extrema/subnormal narrowing cases do not prove a separate flag-commit bug. | Rerun affected public selections with complete retained output; classify each assertion and isolate bounded-range controls. Current 332 libm failures per configuration are not 332 independent bugs. [Analysis](correctness-cpu-ram.md), [wcstod case](correctness-cpython-string.md). |
| COR-22 | **Validation gap** | Seven i8b programs have whole-program QEMU two-NaN exceptions, hiding judgment of later instructions; MIN/MAX under DAZ is not covered by that differential generator. This is separate from COR-09's x87 defect. | Add NaN-free twins or instruction-level judgments and focused DAZ cases in the project harness. [Audit](boot-handoff.md#6a-known-issues-carried-to-the-next-phase), [I8 evidence](boot-features-plan.md). |
| COR-23 | **Validation gap** | The complete litmus run has no forbidden outcomes, but does not systematically cover string/LOCK-XADD, unaligned, crossing, mixed-size or resident-emulated SMP accesses. Existing functional regressions do not prove ordering for these domains. | Find existing public executable suites first; use unchanged litmus7 where capable. Do not maintain a patched public suite without a demonstrated capability gap. Finite-run missing allowed outcomes alone are not failures. [Coverage](correctness-x86-litmus.md). |
| COR-24 | **Validation gap** | Some model rules and `QEMU_DIFFERS` judgments lack a retained source/page reference; broader differential fuzzing remains future coverage work. | Audit segment/syscall/delivery model rules and exception judgments against primary evidence; retain reproducible differential corpora and independently adjudicate mismatches. [Integrity audit](boot-handoff.md), [model sources](../../tools/models). |
| COR-25 | **Validation gap — implemented forms** | Native Intel ENTER partial-store survival and VCVTPS2PH competing-fault/overflow policies lack direct confirmation. Fault-time far-transfer selectors and IRET16 RF preservation lack direct end-to-end observation. No failure is demonstrated by these missing observations. | Retain the selected contracts; obtain the specific native/observation checks without treating QEMU disagreements as automatic defects. [Implemented forms and exact limits](correctness-instruction-forms.md). |

## Won’t fix under the current contract

These dispositions prevent non-bugs from returning as open defects; a changed contract can reopen them.

| Disposition | Reason / evidence |
| --- | --- |
| Undefined or reserved instruction behavior | Do not promise results for undefined 16-bit SHLD/SHRD counts above 16, remaining refused undocumented x87 aliases or reserved prefix combinations. Defined counts 0–16 and FFREEP are implemented. Reserved-prefix refusals must not be used to hide the valid forms in COR-02–06. [Admission](../../tools/fexc/shim/fexc_slice1.inc). |
| Correct architectural alignment exceptions | An instruction that requires alignment may correctly fault; this is not the repaired Arm-alignment stop on an otherwise valid ordinary-RAM access. A VM stall or wrong exception on such a case remains a defect. [Repaired classes](correctness-cpu-ram.md#implemented). |
| Expectations for another CPU or execution environment | Selected Intel prefix behavior need not match AMD REL16 expectations; FEX-specific FXAM/VSTMXCSR expectations and user-mode selector assumptions are not the system-mode contract. Judge each against the selected architecture, not blanket oracle agreement. [Explicit ASM categories](../../tools/fexc/upstream_tests/run_asm.py). |
| Invalid stress-ng nosplit operation targets | The unchanged stressor overruns its allocation above the diagnosed bound. Project selections now use the validated 500,000-operation limit; historical crash/apparent-pass results remain invalid evidence. Do not change emulator semantics or patch the public test to make them pass. [Diagnosis](correctness-cpu-ram.md). |
| Public timer assertions whose budget elapsed | Retained CPython timerfd probes account for the short remaining interval through elapsed guest time; no timer-semantics defect was demonstrated. Performance work can revisit cost, but correctness work must not invent time or weaken public assertions. [Diagnosis](correctness-cpython-string.md). |

## Closed items are not backlog

The repaired vector RAM completion, split-page protection race, BT access width, INSERTPS source read, memory
carry rotates, defined 16-bit double shifts, PUSHF16/LEAVE16, MASKMOVQ implementation, x87 flags-before-store-fault,
ICEBP hang/stale trap, profiler decline-counter collision, `/dev/fd`/CPython fixture gaps and string-output contract
are excluded. See [CPU/RAM results](correctness-cpu-ram.md) and [CPython/string results](correctness-cpython-string.md).
Unmasked #MF/#XM delivery and x87 ES/B are implemented; old I6 limitations are superseded.

COR-02/03/05/06 are implemented and removed from the open list: memory-destination VCVTPS2PH,
all ENTER nesting/operand widths, address32 register-index BT operations, and prefixed near/16-bit far transfers.
The [instruction-form report](correctness-instruction-forms.md) records 1,960 focused judgments, 968 independent
replay judgments, the full suite and unchanged public tests. Remaining observation limits are COR-25, not unresolved
instruction refusal bugs.

The latest **136/136 accepted suite steps**, including corrected Cargo reruns and the permitted x3 stress rerun,
establish results for that plan, not closure of this backlog. The first x3 watchdog failure remains recorded and
unexplained in the report. All 88 planned performance boots also completed with valid outputs; the report retains per-workload costs and limits.
