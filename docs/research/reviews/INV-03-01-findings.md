# INV-03-01 — source findings review

## Current return — implementation plan and setup, October 25, 2026

The owner requested project setup changes reflecting the subsequent Rust,
host-side FEX/live-producer, early performance and N-vCPU decisions, followed by
reviews without editorial blockers. Root authored the documentation; separate
`/root/setup_check_author` authored the read-only diagnostic and regression tests.
The implementation order/setup now lives in `docs/implementation.md`; the current
investigation inventory has moved ahead of historical campaigns. Historical
observations and the frozen Track01 sources remain unchanged.

- **Technical reviewer `/root/plan_technical_review`: ACCEPT, no substantive
  blocker for I1.** Inspected AGENTS, workflow, implementation plan, current
  investigation/inventory, Tracks01/03/05, full ABI draft, setup code/tests and
  FEX fixture/exporter documentation. The Rust capture/FFI/shared-memory contract,
  actual host producer loop, allocated-site recovery, N-dependent capacity and
  early adverse schedules are sufficient requirements for incremental work.
  Diagnostic versus qualified timing and Linux-worker versus native-macOS costs
  remain distinguished. No history, edits or runtime experiments were performed.
- **Setup reviewer `/root/setup_workflow_review`: ACCEPT, no substantive
  blocker.** Independently inspected the entry points, current summary/inventory,
  Tracks01/03/04/05, ABI opening/sections7–9 and diagnostic source/tests. Ran all
  four regression tests successfully and the actual diagnostic, which returned1
  for missing bare-metal Rust target and unset raw root. Checked bounded queries,
  disabled implicit Rust installation, symlink-resolved output containment and
  explicit unverified capabilities. A fresh executor can start I1 without
  reconstructing historical campaigns or a whole-research gate. No edits made.

**Evidence validity:** accepted for plan/setup adequacy only. Neither review
establishes actual Rust/EL1t capture, general recovery-exporter fit, native-macOS
FEX integration, N-vCPU runtime correctness, Linux boot or performance. The local
diagnostic checks known prerequisites and its tests check diagnostic behavior.
**Decision relevance:** proceed with I1 and the early I2 producer loop; retain
scoped correctness tests and independent claim acceptance while allowing ordinary
development. Concrete failures block affected paths only. No substantive returns
or editorial blockers remain; no prior experimental allowance is activated.

**Validation:** root also ran the four tests, the diagnostic and whitespace checks.
The diagnostic leaves toolchain installation, environment configuration, builds,
VMs and containers untouched. Runtime compilation/admission belongs to I1.

## Current return — adequacy and build-first iteration, October 25, 2026

The owner requested a fresh adequacy assessment, build-first development and an
assessment of the provisional ABI under FEX-first incremental integration.
Root authored the current workflow/investigation revisions; two separate fresh
contexts supplied read-only assessments, without history searches or experiments.

- `/root/fresh_adequacy` inspected the workflow, Track03/Track01 charters,
  current twelve-question inventory, supporting investigation summaries, ABI
  fragment method and checkpoint limitations. Its conclusion is that sufficient
  context exists to start incremental construction; no identified research
  unknown blocks the first executable slice. It distinguishes selected contracts
  awaiting implementation, missing inputs, integration uncertainties and owner
  product choices. Complete guest correctness, FEX fit and economics remain open.
  Its targeted recheck accepted the revised adequacy summary and workflow for
  documentation adequacy and decision relevance, with no substantive correction.
  The optional wording clarification to say "Before each increment" in the
  charter was incorporated.
- `/root/abi_iteration_review` independently inspected the ABI, current direction,
  charter interactions and fragment/checkpoint bindings. It found a coherent
  provisional seam, with actual recovery/exporter fit and combined runtime
  behavior still unvalidated. It recommended early real FEX execution, fault/helper/
  paging checks, then two-vCPU adverse interactions and automatic recovery-map
  tests. It performed a targeted recheck of the revised workflow, current
  investigation/progression, charter and ABI iteration note: **accept for this
  scoped workflow/design update, with no substantive correction required**.

**Evidence validity:** existing scoped findings support starting implementation;
this review adds no runtime, general-exporter, full-fit or speed observation.
Manual fixture maps do not establish automatic recovery metadata. The ABI's
state/effect/isolation/lifetime requirements and full T1–T12 coverage target remain.
**Decision relevance:** proceed in executable increments with FEX first, allowing
the interface to evolve. Whole-track completion, a universal ABI and final
generator selection are not prerequisites to ordinary development. Fresh reviewed
evidence is still required for architectural acceptance; partial coverage stays
partial. No historical runtime budget is activated by this review.

Earlier returns below retain their original scope and chronology.

- **Reviewer and independent context:** `/root/machine`; independent of author
  `/root`. Reviewer authored INV-03-03 and therefore does not accept its findings
  or the combined machine proposal in this review. No changes to INV-03-01 made.
- **Material inspected:** INV-03-01 in full; current workflow and Track 03
  charter; INV-01-31 metadata claims 05–11 and scoped conclusions; INV-01-33
  publication/staging claims 01–10 and scoped conclusion; directly retrieved
  upstream QEMU translator-internals page; current scope and backlog references.
- **Date and verdict:** September 19, 2026 — **accept the scoped source/inference
  findings CLM-03-01-01, 02 and 03 only. Architecture acceptance and Track 03
  completion are not established.** No substantive blocker remains against those
  three narrowly stated findings. No experiment or replication was performed.

## Claim assessment

1. **CLM-03-01-01:** accepted as an inference about applicability. INV-01-31
   requires recoverable flags/register/control state, per-substep commit and
   exception selection; INV-01-33 expressly leaves shared staging and general
   reclamation unestablished. Finite two/four-vCPU M4 publication is not promoted
   to a general translator or new host/guest queue contract. Read the referenced
   ranges as material containing findings, not acceptance of each numbered
   historical hypothesis: INV-01-33-06 remains explicitly unestablished.
2. **CLM-03-01-02:** accepted. [QEMU translator internals](https://www.qemu.org/docs/master/devel/tcg.html),
   reporting version 11.1.50 when independently retrieved September 19,
   supports state-dependent TB reuse, mandatory interrupt reconsideration on
   relevant state changes and per-instruction deferred-state recovery. Those
   are upstream mechanisms; root correctly does not claim their relocated
   implementation already works. Moving documentation requires rechecking
   material semantic changes when selecting a pinned generator release.
3. **CLM-03-01-03:** accepted as a necessary address-domain distinction, not a
   complete relocation design. A host pointer's numeric value does not imply a
   valid VM helper/code/state reference. QEMU's documented tagged TB pointers
   and PC-based exception lookup give concrete examples requiring audit. Later
   design must also distinguish x86 linear from physical addresses and native
   virtual from IPA addresses; the present three-domain argument does not claim
   these subdomains are interchangeable.

## Architecture gaps and smallest next remedies

These are blockers to a future architecture acceptance, **not reasons to reject
accurately bounded source findings**. The record already acknowledges them;
this review makes the closure evidence concrete.

| Gap / consequence | Evidence and smallest remedy | Closure condition |
| --- | --- | --- |
| No chosen protected execution/address layout: generated runtime state can remain accessible to guest-derived operands | Root's EL0/EL1 alternatives each leave access generation and private-state placement unspecified. Choose one translator access ABI and draw its concrete mapping/privilege layout; enumerate ordinary, helper, stack, literal and indirect-branch accesses. | Independently checked argument shows guest-controlled addresses/control targets cannot select private state/code, including noncanonical and wrapping ranges, without changing x86 fault semantics. Architectural tests target any remaining platform-specific uncertainty. |
| Recovery imported by analogy can miss generated instruction substeps | The accepted Q31 fixture has handwritten blocks; QEMU documentation describes its own exception machinery, not relocated execution. Select a pinned generator and audit one ordinary load, one helper fault, one interruptible operation and deferred flags/FP state across the proposed ABI. | Every live state component and irreversible effect has a recovery/commit owner; metadata generation and helper return domains are demonstrably compatible with that owner. |
| Service progress while a vCPU waits is unspecified | Root names the circular-wait counterexample, but identifiers and lifetime checking do not establish progress. Specify who services each notification while a CPU is running, halted, in shim, exited or waiting for translation. | No protocol waits for an action that can only run after the waiter resumes; independently reviewed lock/wait graph and event recheck sequence cover the enumerated states. |
| Product compatibility is not concrete enough to select a CPU/machine profile | Missing historical scope files cannot establish which guest/workloads a candidate preserves. Record the first target stock images and workloads, including what unmodified means for initrd/device discovery; inspect required configuration/features. | Candidate CPUID, modes, firmware and devices cover that matrix; exclusions are explicit scope decisions rather than silently inferred from fixture coverage. |
| All-writer coherence is an actual protocol dependency | Host aliases/kernel I/O bypass CPU write traps; source generations alone can accept torn or stale work if captured incoherently. Resolve INV-03-04 with the machine author's proposed device-commit path. | CPU, helper and backend writes share a specified linearization/coherence authority with source capture, remapping, fault completion and retirement. |

Material alternatives and costs in INV-03-02/03/04 still need integration before
selection. This reviewer has not independently accepted its own INV-03-03 device,
time or containment proposal. A different context must review those claims and
any final combination that relies on them.

## Scope, budget and methodology assessment

The initial audit budget is bounded and its stop condition produces decision
questions; it is not an experimental authorization or artificial completion gate.
The user authorized completing Track 03, so successive bounded source work and
necessary reviewed architecture experiments remain within the research task.
No return to the user is needed merely because the initial integration timebox
ends. A material new resource budget or compatibility tradeoff needs its own
concrete handling under the workflow; the current document makes neither.

No evidence yet shows cross-generation applicability, host-core placement,
end-to-end Linux behavior or speedup. Their absence does not undermine the three
accepted source/inference claims. It does prevent using this review as evidence
for a working or independently accepted architecture.

## Editorial edits and limits

No direct edits. No git history or archived Track 01 material inspected. No
experimental artifacts were rerun: this review relies only on current accepted
investigations for inherited applicability and directly retrieved upstream
source contracts for the new source claim.

## Returns

No return needed for CLM-03-01-01–03. Re-review substantive changes to their
assumptions or any proposed architecture acceptance in this same thread.

### Return 1 — exact kernel configuration supplement

September 19, reviewer `/root/machine`: **accept CLM-03-01-04 and its bounded
transport-prerequisite inference.** Independently hashed both complete `.deb`
files under `/private/tmp/natemu-track03-stock-audit`: Ubuntu
`15bacf92d51a0ea971d9c1369c6385e8867cd4973eede8f8931fbaf99f3dd173`, Debian
`0ab746a3d854ad0e666923147842d1e629cdb3e29a97b76ae25b4911285174e4`.
Both match the official package identities and accepted hashes in INV-01-34
claim 08. Independently extracted each archive's unique `.config` with `ar`,
decompression and Python tar reading, without installing/executing package
code; compared it byte-for-byte with root's reading copy and inspected every
symbol in the new table. All table values match.

The working Ubuntu kernel has built-in MMIO/block/network support; Debian
requires modules for those drivers and lacks command-line MMIO enumeration.
Root correctly distinguishes that route from ACPI enumeration and does not
infer actual initrd contents, root mount, required exposure of every configured
device, or compatibility of every distribution release. No substantive return.
This supplements accepted source findings; architecture completion remains open.

### Return 2 — guarded EL1 execution candidate

September 19, reviewer `/root/machine`: **accept the conditional address-partition
argument as an explicitly limited inference; do not accept implemented isolation
or the guarded-EL1 architecture.** Read the entire new candidate and the associated
memory supplement, independently consulting Arm Memory Management v1.0 §§5/7.
The proposed lower/upper 48-bit native regions can distinguish the x86 48-bit
canonical halves from the stated private interval. The argument properly depends
on disabled top-byte ignoring and full guest-derived operand validation, not on
x86 noncanonicality itself causing an Arm fault.

The candidate explicitly separates guest physical addresses from native IPAs:
a guest PTE cannot select shim backing solely by naming its numeric IPA.
Only runtime-owned native roots reach private allocations. Guest CPL and CR0.WP
must determine shadow policy independently of EL1 authority; INV-03-04 now names
those in context identity and excludes stale direct links after mode changes.
PG-off protected32 routes through guest physical resolution rather than raw IPA
access. Those are necessary coherent boundaries, not evidence that every
emitted access already respects them.

Remaining acceptance premises are substantive and already retained: complete
emitter/helper access classification, instruction-specific span/fault precedence,
correct shadow switching, immutable helper/control targets, and private object
bounds. Trusted-runtime field offsets require checked object extents, not merely
membership of the whole private interval; guest-controlled lengths and indexes
cannot become unchecked runtime offsets. Root's validated-fixed-offset/checked-
allocation-handle rule covers that at the proposed interface level. The next
selected-generator audit must demonstrate that callers actually obey it.

W^X requires removing all writable aliases and completing native maintenance
before entry and before reuse. Its inclusion is a new obligation; the candidate
correctly excludes the old RWX fixture as proof. Native branch reach/veneers may
be construction choices only when relocations and targets preserve this partition.
No additional source defect found. Hardware configuration and generated-code
premises remain open; this review accepts no arbitrary-native-code confinement
inside an EL1 VM and no host worker containment claim.

### Return 3 — native W^X scope correction

September 19, reviewer `/root/machine`: read the revised guarded-EL1 allocation
paragraph. **Accept the scope correction:** native W^X is optional hardening for
this trusted-EL1-runtime candidate. The charter requires guest/private-state
exclusion and containment, not a specific native page-permission mechanism. An
EL1 runtime controls its tables; W^X would not independently contain a compromised
runtime. Keeping it mandatory would add an unsupported architecture gate.

The revised baseline retains the necessary conditions: guest-derived operands and
control flow cannot select private mappings; guest RAM is native non-executable;
admitted code bytes are immutable by protocol; only trusted publication/retirement
may write runtime-only code allocations; publication and all continuation lifetimes
remain mandatory. RWX does not prove those conditions, and root explicitly retains
the unproved emitter/helper guards and separates the narrower Track 01 result.
My Return 2 statements about W^X maintenance apply only if that optional mechanism
is selected. This correction accepts neither actual guard completeness nor an
architecture/containment result. No author-file edits.

### Return 4 — CPU model/accelerator and clock source supplement

September 19, **separate reviewer `/root/translator`**, independent of CPU-profile
supplement author `/root`. Previous returns remain owned by `/root/machine`.
I authored INV-03-02, not this CPU-profile supplement, and did not edit the
architecture record. **Accept CLM-03-01-05's scoped source facts and the explicit
conditional priority for a Haswell-v2-derived audit; no realized CPU profile,
guest compatibility or architecture acceptance.**

Read the whole updated architecture record and earlier review returns, then
inspected the pinned `target/i386/cpu.c` at revision
`c1c18d1e640b64292859ce9f30f3c344edfb0294`, independently retrieving it from
upstream and verifying it matches root's local reading copy. Relevant paths:
`builtin_x86_defs` Haswell/version table, TCG feature masks,
`x86_cpu_filter_features`, model-loading/expansion/filtering contract,
`cpu_x86_cpuid` and CPUID level adjustment. Web cached retrieval failed; the
read-only direct upstream fetch succeeded. No QEMU CPU object was created or
realized and no guest code ran.

The base definition has Intel family 6/model 60 and basic level 0xD; its version 2
alias disables HLE/RTM and changes stepping to 1. The requested AVX2/FMA/F16C and
XSAVE/XSAVEOPT feature families are present. PCID and TSC-deadline bits in the
model request are absent from the system-TCG support mask; user-only builds
explicitly add them as kernel-only features, so that build distinction is
load-bearing. INVPCID is similarly excluded from the system-TCG leaf 7 mask.
`x86_cpu_filter_features` checks requested versus accelerator-supported words,
and the source explicitly says `query-cpu-model-expansion` does not do that
filtering. Thus the source supports the distinction between a named model,
an expanded request and a realized profile.

I independently checked the dynamic OSXSAVE condition (XSAVE feature plus
CR4.OSXSAVE), leaf 0xD component/size derivation and topology inputs. A final
feature manifest must name machine/topology/version and filtering conditions;
it cannot be just the Haswell-v2 name or a pasted expansion response. Level
adjustment can also depend on topology and enabled features, so acceptance of
the base definition’s 0xD value is not a claim about every realized configuration.
The inspected CPUID implementation has no 0x15 frequency case. A query above the
reported limit falls back to that limit's leaf rather than supplying a frequency
contract. The proposed clock model must therefore audit a platform calibration
route or deliberately add/review a new frequency leaf. This agrees with the
author's stop condition and does not independently establish which timer to use.

Keeping AVX2 as the current candidate preserves the full vector/state/atomicity
questions instead of eliminating them with a scalar fixture profile. The record
explicitly retains x87, mode transitions, feature/control-state dependencies,
MSRs, PAT/MTRR and enabled permission/debug behavior as audit work; upstream
TCG's support mask is not proof of the modified VM runtime. Four-level/no-PCID/
no-1GiB-page proposals and workload classes are not declared a finalized
compatibility boundary. There is no unsupported-profile acceptance to retract.

No substantive return is required for claim 05. Final acceptance remains blocked
on an enumerated realized feature/behavior contract, selected guest/workload
requirements, all enabled helper/recovery/memory paths, clock discovery and
correct unsupported instruction/MSR responses. This review neither audits all
those paths nor waives the separately recorded inherited FP discrepancy. No
editorial edits or runtime evidence.

### Return 6 — reference-clock source alternative

September 19, machine reviewer: independently read the relevant upstream v6.8
and v6.12 `arch/x86/kernel/tsc.c` paths, v6.12 `tsc_msr.c` model table and
`drivers/clocksource/acpi_pm.c`, using root's versioned reading copies under
`/private/tmp/natemu-track03-stock-audit`. No runtime experiment.

**Verdict: accept claim 06 and its conditional alternative as scoped source
findings.** Both TSC implementations reject native CPUID calibration below leaf
0x15; no-PIC quick calibration returns zero and the full PIT routine waits using
five early `udelay` calls then reports `ULONG_MAX`. The enclosing routine still
samples HPET/PM before and after that interval, rejects unchanged or disturbed
samples, and can return a valid reference result despite failed PIT calibration.
The v6.12 MSR table supplies Atom models, not Haswell; the PM-clock PIT rate check
is compiled out for CONFIG_X86_64. Therefore an absent frequency leaf alone does
not prove PIT is required.

This is not a complete no-PIC machine argument. Firmware must select that branch,
early delay must make sufficient progress, and the selected reference must be
discovered and implement the required rate/read behavior. LAPIC calibration,
interrupts, wallclock and exact vendor initialization remain separate. The author
keeps all of these premises explicit. No substantive return, full timer selection,
stock-boot finding or architecture acceptance follows.

### Return 7 — access outcomes and generated success continuation

September 19, machine reviewer: independently read the three proposed interface
traces and claim 07, then inspected the pinned AArch64 backend in the previously
independently downloaded `/private/tmp/natemu-track03-review-tcg-target.c.inc`.
The inspected revision is `c1c18d1e640b64292859ce9f30f3c344edfb0294`; no code ran.

**Verdict: accept claim 07 as a scoped source finding and the three outcomes as
a conditional interface argument.** `tgen_qemu_ld/st` emit the direct operation
then capture `raddr` when a slow-path descriptor exists. The slow load path calls
its helper, emits helper-result restoration and jumps there; the store helper
path jumps there after the effect. This establishes a reusable generator-owned
success edge. It does not establish the adapted trap ABI, complete descriptors
for accesses that lacked upstream slow labels, or VM-valid helper pointers.

The distinction resolves two concrete protocol errors: retry after a serviced
store repeats an effect; advancing after a mapping-only load repair discards the
load. Source data/result locations and operation identity must come from the
pinned generated expansion/frame, including signed/zero extension and register
width where applicable, rather than the guest opcode or a generic PC increment.
The proposed result contract and retained frame explicitly carry that burden.
A single aligned scalar native access supplies a useful bounded case; the text
does not extrapolate it to native pairs/exclusive loops or device side effects.

The tracked-store trace correctly holds native code/metadata until its success
edge and generated checkpoint finish, despite invalidating future block entry.
The REP trace retains the already-read source temporary and updates pointers/
count only after destination completion. A repeat service notification must not
repeat the store or advance the phase; the immutable identity/phase gate is
therefore load-bearing. Likewise a second fault during drain must yield to the
same coordinator instead of recursively acquiring it. These are stated premises,
not verified implementation properties. Fault reconstruction must preserve prior
legal partial effects and abandon the success edge, as the third outcome says.

No substantive return is needed for this interface scope. Full acceptance still
requires precise continuation/result metadata, complete frame restoration and
exclusive-service ordering, generated normal/REP checkpoints and VM-local fault
exit. RF/interrupt-shadow semantics, split accesses, atomics and MMIO retain their
own obligations. This review does not turn scalar examples into architecture or
all-instruction acceptance and makes no author-file edits.


### CPU inventory return — CLM-03-01-08

September 19, independent reviewer `/root/translator` (continuing CPU claim05
review): **accept claim08's scoped core-feature source intersection, realization
dependencies, permissive MSR default and TSC-helper seam.** No CPU realization,
complete CPUID/MSR profile or unsupported-feature policy is accepted.

Independently inspected the pinned Haswell model/version2 overrides, system TCG
feature masks, TCG vme default, feature expansion/filtering and CPUID realization
code. The seven-word table agrees with the requested model intersected with
those masks plus explicitly described dynamic/default additions. SMEP, RDRAND,
MCE/MCA and PAT/MTRR survive; PCID/TSC deadline/INVPCID do not. The omission of
unrequested VMX despite raw capability initializers, topology-dependent HT,
dynamic OSXSAVE/save sizing, PMU-off default and physical-width dependency are
correctly distinguished from an observed final CPUID dump. No extra reductions
follow merely from calling the model Haswell-v2.

Inspected system misc_helper.c's complete RDMSR/WRMSR switches and machine-check
bank defaults. Unhandled reads become zero and writes are ignored, while
selected invalid cases explicitly raise #GP. The document correctly treats this
as an unresolved virtual-CPU policy seam, not proof that all absent MSRs should
fault or an assertion of an upstream QEMU bug. Defined behavior must be selected
against the intended profile and primary architectural/initialization contracts.

Confirmed MSR_IA32_TSC is 0x10 in cpu.h, absent from those system switches, and
gen_RDMSR/gen_WRMSR call the corresponding helpers. Separately helper_rdtsc uses
cpu_get_tsc plus tsc_offset. The concrete conclusion follows: unchanged reuse of
these particular paths cannot provide the candidate's coherent TSC instruction/
MSR interface. An explicit shared clock/offset adapter is required. This is not
a runtime result or a claim about other QEMU accelerators and their MSR routing.

No substantive return. The next complete admitted-MSR/rejection manifest remains
a design obligation; actual realization and instruction tests later validate
conformance. No author record edited and no guest/source execution.


### MSR policy return — CLM-03-01-09

September 19, independent reviewer `/root/translator`: **accept claim09's scoped
primary-source rules, FS_BASE counterexample and conditional validation-layer
rationale.** No complete field/reset/MSR manifest or realized CPU acceptance.

Read the retained official Intel Volumes2B/2D/4 revision092 (June2026) RDMSR,
WRMSR and relevant register definitions; independently compared the pinned
system helper cases and gen_WRMSR. WRMSR's canonical-address check explicitly
includes FS_BASE. For the proposed 48-bit linear profile, 0000800000000000 has
bit47 set without the required high sign extension. The helper directly stores
that value and the frontend adds no value validator. Thus delayed rejection at
a later FS-relative access cannot satisfy precommit validation of this WRMSR.
The source-derived discrepancy is established without execution; it does not
claim all helper paths or accelerators share the same behavior.

The primary instruction contracts support privilege/index/reserved-field checks,
MTRR invalidation including global translations, and register-specific
serialization distinctions. The submitted validator preserves register-specific
read-only/reserved behavior instead of universally masking bits. TSC_ADJUST's
feature predicate and TSC_AUX's reserved upper32 field agree with the register
table. The finite family list is properly a work partition rather than a falsely
complete supported-MSR manifest; unsupported-index #GP requires completing the
admitted rows first.

Read both cited Linux Intel initialization files and v6.12 common.c. Family6
model60 reaches the non-safe MISC_ENABLE read controlling fast-string/ERMS
capability handling. The named MISC_FEATURES_ENABLES and PLATFORM_INFO probes
use safe wrappers and conditional failure handling. These source paths support
the distinction between required compatibility state and optional absence; they
do not establish every boot-driver MSR access. SYSENTER/syscall initialization
and the Intel CSTAR skip agree with the submission. A safe wrapper alone is not
a promise that all downstream functionality tolerates every absent register.

No substantive return. The smallest correction remains admission/value checks
before mutation or host side effects, retaining inherited handlers behind the
validated interface and routing TSC through the shared clock owner. Complete
bit/reset/effect rows and remaining initialization call sites are design work;
later implementation tests validate compliance. No source execution, guest
execution or author-file changes occurred in this review.


**MSR return extension — sibling FSGSBASE write paths:** independently rechecked
the appended claim09 sibling paragraph against Intel092 Vol2D pp6-6–7 and the
pinned decoder/emitter. **Accept the scoped extension.** WRxxBASE entries require
FSGSBASE, 64-bit mode and F3, with zextT0 for operand sizing; gen_WRxxBASE checks
CR4.FSGSBASE then directly moves the value into the FS/GS base global. The
specification requires #GP for a noncanonical effective source, so an MSR-only
canonicality validator leaves this exposed mutation path open. Sharing the
canonicality predicate is a coherent minimal direction, provided 32-bit source
zero extension happens first and the instruction-specific feature/mode/prefix
checks remain. No CPL0 gate should be inherited from WRMSR: these are different
instruction contracts. No broad address-mutation audit, runtime result or author
edit; the bounded extension has no substantive return.

## Return — control-state claim 10, September 20, translator reviewer

**Accept the scoped source counterexample and conditional precommit remedy.**
I independently inspected the pinned `gen_WRMSR`, system `helper_wrmsr` EFER
case and `cpu_load_efer`, plus the locally retained Intel 253668-092US §5.1.2
and 335592-092US EFER table. The frontend updates PC/CC and ends the block but
performs no compensating transition validation. With LM exposed, the helper's
update mask includes LME and excludes LMA. The submitted old active-mode state
and requested LME-clear value therefore produce LME=0/LMA=1 in that source,
where Intel requires #GP(0). This is a concrete source trace, not a runtime
reproduction or claim about other accelerators.

The proposed test must precede state mutation and compare requested versus old
LME; preserving identical-LME writes correctly avoids banning unrelated writable
EFER changes while paging is enabled. Distinguishing trusted state loading from
guest WRMSR validation is appropriate. NXE permission/reserved-bit interpretation
and redispatch/shadow coherence remain obligations beyond this predicate. I also
checked the local Linux v6.12 `intel_unlock_cpuid_leafs` bit-22 clearing and CPUID
refresh, and the bounded MISC_ENABLE source observations; raw storage is not
accepted as a complete register model.

**Small source clarification, no counterexample to the claim:** revision-092
Volume 3A §5.1.1 footnote 2 explicitly says WRMSR ignores source bit 10 (LMA).
The author can close that particular read-only-bit write disposition from the
already inspected primary text rather than leaving it generically open. This
does not close other fields, reserved writes or the coupled control-state rows.
There is no substantive return against the submitted EFER mismatch or smallest
remedy, and no acceptance of a completed CPU profile or implementation.

**Closure:** the author incorporated the explicit ignored-LMA-source-bit rule;
I rechecked that correction. Claim 10 has no outstanding review return.

## Return — twelve-question inventory cleanup, September 20

Translator reviewer; consistency review of existing records only, no new source
research. **Accept the cleanup in scope.** The main record now distinguishes the
fixed host-codegen/captive-shim architecture from open component choices and
maps all twelve charter questions to shared requirements, evidence owners and
closure work. ISA/state, memory types, exact recovery, all-observer atomicity,
code lifetime, event progress, clocks, direct boot, device completion and
containment remain required; none is silently discharged by a conditional source
finding. The active cleanup and unstarted common-interface pass are distinguished,
and row-specific closure work is not twelve new simultaneous work orders.

Claims 05/08 retain their upstream inventory while withdrawing its authority to
set our independent CPU profile. Claim 07 and its access traces explicitly depend
on TCG state/continuation conventions. The common outcomes survive a generator
change, but those proofs must be re-established. Other source findings and their
review history remain scoped; no new generator ranking or complete architecture
acceptance is asserted.

Two editorial returns were corrected and rechecked: the remaining CPU exposure
imperatives are explicitly confined to the historical candidate, and the
retrospective now preserves the exact six inference/scope IDs plus eight
additional proof-artifact IDs after audit deduplication. Its counts distinguish
attached reasoning from false source facts and do not claim percentage effort
saved. The CF_PCREL requirement is also expressly conditional on the TCG adapter.
There is no outstanding cleanup return. Rebuilding the comparison and closing
component-dependent proofs remain substantive work; cleanup is not that closure.

## Return — common-codegen architectural dependencies, September 20, machine reviewer

**Accept the scoped requirement derivation and comparison boundary; no component
selection or architecture acceptance.** Independently read the added
“Common codegen contract: architectural dependencies” section, active-action
status, twelve-question inventory, guarded-EL1/state-communication proposal and
INV-03-02's current common-interface direction. This was a bounded read-only
architecture challenge using current records; no new primary-source research.

The addition correctly treats CPU profile, privilege/layout, walker and semantic
helper provenance as independently owned inputs. Comparing generators against
the same explicit hypothesis does not accept that hypothesis for the product.
Its state/recovery obligation allows register-resident/deferred state with exact
recipes, rather than forcing CPUX86State, spill-all boundaries or upstream
runtime conventions. The three access results express distinct effect outcomes;
their names, wire format and nonnesting/permanent-landing realization are not
universal codegen requirements. Resource/reference bounds must be derived for
the actual adapter, so the TCG-specific link/frame constants are not imported.

The performance paragraph preserves the intended native paging/resident-shim
advantage: common correctness does not mandate a host transaction, service call
or full spill per access/instruction. Actual synchronization and recovery remain
required at the applicable effect/visibility boundaries. In particular, the
older coordinated-service traces do not make all-observer exclusion an intrinsic
requirement for every ordinary shadow hit or local repair. A cheaper candidate
must still establish the authority/visibility needed for its case; no relaxation
of x86 effects or private-state exclusion follows.

Translation identity, relocated resident addresses and cancellation lifetimes
follow the already stated execution/publication requirements without selecting
transport or serial artifact format prematurely. “Replacement” and “unknown”
are correctly not numeric effort estimates or rankings. Current action accurately
reflects the resumed bounded interface pass, and this integration does not
create another independent research campaign.

No substantive return. The forthcoming INV-03-02 adapter artifact needs its own
review; this verdict does not preaccept its generator-specific mappings or prove
that any candidate implements the common obligations. No author-file edits,
runtime execution or measured performance conclusion in this review.

## Return — translation request ownership (claim 11)

**Reviewer:** machine investigator, independent of root author. **Verdict:**
accept the scoped conditional ownership/protocol choice; no substantive return.
Inspected the new “Translation request ownership” section and its surrounding
state/communication and common-codegen requirements, with the already reviewed
INV-03-04 bounded request/lifetime construction. This is an architecture argument
review, not a new source audit, experiment or acceptance of a concrete transport.

The proposal identifies the correct publication authority. Snapshot capture and
final admission share the relevant invalidation serialization, while compilation
uses immutable copies without holding that authority. Registering dependencies
before reachability prevents the check-then-publish race only under the expressly
required shared ordering: an invalidation during installation must either revoke
publication or revoke the registered entry before affected execution. Cache
maintenance alone would not establish that ordering; the text does not claim it
does. Direct links and saved continuations are explicitly included, preventing a
cache-entry-only interpretation. Completeness of dependency capture and the
actual coordinator protocol remain premises rather than assumed source facts.

Cancellation addresses both logical staleness and physical lifetime. VM lifetime,
slot incarnation and request identity prevent a numerically matching late reply
from becoming current, while acknowledgement/draining protects the storage before
any stale callback writes. The alternative identity-resolved destination retains
a reference across the copy; checking only at completion is explicitly rejected.
A published code object transfers to its separate lifetime owner, so cancelling a
waiting demand must not be interpreted as immediately freeing code pinned by an
unrelated execution. The described lifetime distinction supports that reading.
Full namespace drain before wrap covers queued replies and is a sound bounded
resource alternative to keeping all historical requests forever.

Guest event handling preserves the guest-state owner and legal delivery boundary.
Cancelling the old demand when an event changes its continuation, then dispatching
again, avoids resuming a reply's obsolete RIP. A reusable compiled object and a
particular waiting continuation are consequently distinct: even if an object is
still valid for future dispatch, a late reply grants no permission to resume the
old continuation. The proposal requires this property without forcing every event
to discard all potentially reusable code.

The progress rule is conditional but appropriately concrete. Required resident
coordination remains serviceable during compilation waits; reserve pressure blocks
new work while admitted work drains, with permanent service capacity retained.
A permanently hung producer cannot be made safe by a timeout followed by slot
reuse; contained termination plus actual delivery-path retirement is the specified
failure alternative. The rule does not prove bounded compile latency, starvation
freedom under unlimited source mutation, or a specific queue capacity, and its
closure scope does not claim those results. No additional host call, full spill or
global rendezvous is imposed on every guest access or instruction.

The remaining implementation premises—coherent captures, concrete entry/cache
publication, recovery, reference counts and bounded adapter outputs—are explicitly
owned elsewhere. They limit this acceptance to selecting ownership and required
transitions, not proving an implemented state machine or completing charter
questions 3/7/8/12 or the combined architecture. No author-file edits were made.

### Claim 11 follow-up — typed source-fetch interaction

**Verdict:** accept the two added “Integration with typed source capture”
paragraphs in conditional protocol scope. Independently read the addition against
INV-03-04 claim 16's source-byte admission contract; no new source or runtime
claim is introduced.

The correction matters: compilation on an immutable copy has no guest effect,
but obtaining that copy may already have performed typed, side-effecting fetch
traffic. Cancelling a compiler request cannot erase that traffic or permit a
late response to repeat it. The resident fetch record retains fragment/effect
identity, while a genuine fault/event abandonment still uses architectural
restart rules. This is consistent with the prior distinction between a waiting
demand, a produced object and their separate lifetimes.

Continuation requests grant no direct guest-memory authority to the host. They
are admitted only for the live fetch/current instruction under mode, length and
memory-type constraints. The trusted decoder still supplies the instruction's
need for further bytes; this is not an acceptance of an arbitrary host-selected
range or a requirement to build a second full resident decoder. Binding completed
input/dependencies before publication preserves claim 11's invalidation ordering.
Ordinary side-effect-free cacheable lookahead remains allowed by claim 16, so this
interaction does not impose single-instruction blocks or per-byte host round trips
on the fast path. Actual adapter continuation granularity, fetch lifetime bounds
and publication implementation remain unproved. No substantive return.

## Return — coupled control-state transaction (claim 12), memory reviewer

**Independent scoped source/design acceptance.** Read the complete submitted
transaction and independently checked the retained Intel092 Vol.2B control-MOV
entry (pp.4-32–34), Vol.2D XSETBV (pp.6-70–71), and Vol.3A mode-transition,
legacy-PAE and translation-invalidation rules (§§5.1, 5.4.1, 5.10.4.1). Also
inspected pinned QEMU `cpu_x86_update_cr0/cr3/cr4`, `helper_xsetbv` and
`helper_clts`. This review performs no build or native execution.

The instruction-specific distinctions survive scrutiny: ignored CR0 low reserved
bits are distinct from reserved high-bit faults; CR3 interpretation is mode
specific; invalid control-register encodings are not generic MSR faults; CR8's
nonserializing contract is explicit. XSETBV retains its unsupported/OSXSAVE #UD
conditions and value/privilege #GP checks, with additional component dependencies
conditioned on the exposed profile. The document does not claim a complete
instruction-exception priority catalog or impose MOV's undefined flags on LMSW.

The PAE distinction is load-bearing and correct. Section5.4.1 names the exact
CR0/CR4 reload triggers, the new-root MOV CR3 load and task-switch path. Its
present-plus-reserved test gives #GP and leaves the PDPTE set unloaded. Selecting
present-entry-only validation is consistent with the separately documented
optional nonpresent-entry check. These retained hidden values are guest semantic
state, despite Intel calling the implementation registers non-architectural;
discarding native shadows or over-invalidating TLB entries cannot silently fetch
new PDPTE values. The source-backed permission for additional cache invalidation
therefore supports the selected eviction policy without supporting arbitrary
reload of this hidden state.

The combined commit preserves the relevant ownership boundaries: validate/stage
before publishing the control tuple; reconcile native mappings and compatible
code-entry keys before next admission; keep old instruction recovery from undoing
a committed transition; and retain independently allowed memory effects rather
than pretending a control fault rolls back all prior work. Shared shadow
invalidation does not change another vCPU's control state. State-only TS/XCR0
changes do not acquire a needless global memory flush. The contained-failure
branch applies to failed host synchronization, not to routine legal guest mode
changes or missing legacy walkers.

Pinned helpers support reuse of state derivation but not wholesale validator or
runtime reuse: `cpu_x86_update_cr3` has the cited legacy-PAE validation TODO;
`cpu_x86_update_cr0` even has an exception TODO for invalid long-mode activation.
The explicit resident validator and alternate LMSW/CLTS/reset/task-switch seams
are therefore necessary. Loader/task-switch changes retain their own validation
and partial-progress contracts rather than inheriting guest MOV admission.

Accept the selected resident admission/commit boundary and conservative cache
policy under the stated profile and existing synchronization premises. This
closes that architectural integration choice; it does not accept a final CPU
profile, every walker/segment/task-switch path, FP save/restore, or actual native
shadow implementation. Those are correctly identified independent obligations.
No substantive return or new experiment requirement.

## Return — coupled TSC/crystal/LAPIC frequency (claim13), memory reviewer

**Independent scoped source/design acceptance.** Inspected Intel253666-082US
CPUID Table3-8 leaf15, and the retained Linuxv6.12 `tsc.c`/`apic.c` paths named
in the claim. The selected ratio is internally exact:
24,000,000 ×125/3 =1,000,000,000Hz. ECX is crystal Hz, not CPU throughput;
EDX remains zero. Extending the maximum basic leaf requires the consistent
intervening-leaf dispatch already called out, and does not inherit a physical
Haswell CPUID image or require leaf16.

Linux's actual adjacent effect is correctly included: `native_calibrate_tsc`
checks Intel vendor/leaf availability/nonzero denominator and numerator, marks
a nonzero crystal as known frequency, and sets `lapic_timer_period` from that
crystal under CONFIG_X86_LOCAL_APIC. With the selected24MHz crystal its return
is exactly1,000,000kHz. `__setup_APIC_LVTT` programs divide16 on this path and
uses `clocks/APIC_DIVISOR`; the known-frequency branch of `calibrate_APIC_clock`
removes the dummy-clockevent flag on the explicit always-running-timer premise.
The author's24MHz LAPIC input plus guest-programmed divisor and continued
counting through HLT/descheduling therefore closes a real dependency, rather
than treating CPUID15 as an isolated TSC report. Intel's crystal field alone is
not a universal LAPIC-rate promise; this is the selected compatible virtual
platform construction bound to the inspected Linux path.

`detect_art` separately rejects hypervisor environments and requires nonstop
TSC, TSC_ADJUST and no asynchronous resets before its ART setup. The claim's
unadvertised adjustment/deadline features do not accidentally acquire those MSR
contracts by exposing leaf15. The shared exact host-time rational, per-vCPU TSC
write offset and independent LAPIC countdown preserve the selected epoch
ownership; late native wakeups recheck a virtual deadline instead of advancing
the clock. Whole-VM pause and host-sleep policy are inherited explicit choices,
not new physical-crystal claims.

Accept the numeric frequency/discovery seam under its Intel-vendor and common
time-owner premises. No substantive return. Full CPUID dispatch, timer register
semantics, native scheduling/interrupt conformance and boot remain separately
identified obligations. No runtime calibration result, throughput claim or
additional experiment requirement is implied.


## Question7 closure review — claim14, root

**Reviewer:** root, independent of machine's synthesis, September20.
**Verdict:** accept the selected code identity/publication/invalidation/lifetime
architectural contract under its named cross-question interfaces.

Read claim14 together with claim11's source/request lifecycle, claim02-37's
entry/continuation construction and claim04-12's capacity/reference argument,
plus the subsequent all-writer/REP/maintenance composition. Rechecked Intel092
§§7.6/11.1.3: synchronized new execution and internal runtime continuation are
distinct; preserving an admitted REP across an invisible pause is not authority
to execute stale following instructions after synchronization.

The first proposal constrained objects to single basic blocks/two exits and
omitted return caching to fit old capacity arithmetic. Review required removal
of that unnecessary coupling. The revision admits bounded CFGs with E external
links and bounded return predictions targeting guarded external entries. E*C
bounds detachable edge storage, not the retained allocation union. Quiescent
cache detachment and the census of already loaded targets close the pre-pin
veneer lifetime window. Native inter-object tail transfer, permanent helpers
and one admitted handover retain the old/destination/origin bound independently
of CFG size; no recursive generated ancestry or uncounted second handover is
permitted. These are adapter rules, not claims that unchanged FEX fits them.

The combined ordering closes question7's required races: dependency registration
and revalidation share the writer mutex, so publication cannot escape mutation;
code/metadata become reachable only after complete installation and executor
synchronization; warmed links still guard entry; invalidation preserves an
already admitted effect but requires dispatch before a stale suffix; cancelled
producer/delivery references drain before storage reuse. Source/route/context
identities distinguish physical aliases, permitted cached translations and
specialized CPU state. Side-effecting source fetch does not acquire arbitrary
reusable cache eligibility. Hashes do not substitute for identity comparison.

Reclamation removes eligibility edges under closed admission and frees only
outside the actual continuation/reference union. Reserved output/successor/drain
capacity prevents cache pressure from requiring REP completion or another blocked
coordinator acquisition. Retaining a native fault/helper PC retains its object;
unknown phases or timeout never authorize reclamation. No whole-arena reset is
selected while live references exist.

No substantive return remains. This accepts a complete code-lifetime design
contract, not a finished generator, full CPU/memory/event semantics, numerical
capacity sizing or performance. Those named owners must supply the stated
interfaces; incompatible later choices reopen this acceptance. Combined Track03
acceptance remains outstanding. Another mock is not required to choose this
contract; planning must demonstrate actual emitter/allocator conformance.


## Inventory status reconciliation — September 20

Root reconciled the current twelve-question inventory with the owning summaries.
Independent read-only reviewer `/root/machine` checked the inventory against
INV-03-02/03/04 and identified stale status wording; root applied those editorial
corrections. Accepted scoped evidence now includes codegen claim40 and memory
claims38–39; memory claim40 and serial claim60 remain pending independent review.
The memory inventory points to the accepted question7 contract in INV-03-01
claim14. Historical preparation and superseded next-step wording no longer
override subsequent findings. No new source research, experiment, generator
ranking or architectural acceptance results from this reconciliation.


## Guarded-EL1 composition review — claim15, memory

**Reviewer:** memory, independent of root's author submission, September20.
**Provisional disposition:** address/control/state/transport construction is
supportable; one wake-authority clarification remains before closure.

Read claim15 against accepted claims11/14, INV03-02's retained-frame/IRQ-scratch
and helper-result interfaces, and INV01-33 claim10's finite register-transfer
publication/WAIT findings and limits. Checked the current SDK26.4 hv_vcpu.h
run/forced-exit contracts. This is source/interface review, no native execution.

The unsigned full-span predicate is sufficient to exclude the private gap and
wraparound on its direct route. Its rejection correctly invokes the semantic
owner, not a universal #GP, and does not erase legal subaccess/partial effects.
Physical resolution separately excludes private backing; native NX of guest RAM
and classification of guest versus resident targets prevent a guest numeric RIP
from acquiring native EL1 authority. Implicit/helper/stack access coverage is an
explicit adapter requirement, not a hidden native-pointer exception. This remains
a trusted compiler/runtime interface, not containment against malicious emitted
native code.

Live guest roots remain vCPU-owned. The distinct resident entry frame/IRQ scratch,
pinned recovery metadata and permanent landing preserve arbitrary native stop
state without pretending it is a canonical guest boundary. Claim14 supplies
entry/return/lifetime identities; claim11 supplies immutable source requests and
cancellation. Register transport requires resident-chosen destinations and retained
roots before lane reuse, consistent with the inherited finite mechanism. No shared
host queue, full-ISA exporter correctness or performance is inferred from it.

**R15-1:** the blanket statement that only the owning thread performs vCPU API
operations must distinguish owner-only run/get/set from the cross-thread force-
exit control path. SDK hv_vcpu.h states hv_vcpu_run blocks until exit/cancellation;
hv_vcpus_exit forces running CPUs out and latches an exit for the next run when
already stopped, without the run/get/set owning-thread restriction. Otherwise a
compiler-response/control thread has no specified way to notify an owner blocked
in native execution under the literal rule. Requested an explicit bounded wake
request plus pending-identity recheck (or a concrete bounded WAIT/poll route),
without treating the forced native PC stop as canonical guest state, overwriting
live registers, or requiring guest IF. This is a narrow progress-interface return;
it does not require a whole generator implementation or a new experiment.


**R15-1 recheck and final verdict:** author amended claim15 to distinguish owner-
only run/state operations from bounded control-thread hv_vcpus_exit, publishing
request identity/epoch before the kick and rechecking after exits/before reentry.
The SDK's latched-next-run exit handles the stopped/running transition; durable
request state handles stale/coalesced notifications. Arbitrary stopped native
roots remain retained, and only the permanent service stop permits transfer-lane
reuse. Existing control/rendezvous and WAIT progress do not depend on guest IF or
compiler completion. I independently re-read the amendment; R15-1 is closed.

Accept claim15's scoped question3 architectural composition. No substantive
return remains. Named memory, recovery, event and lifetime owners still supply
their stated interfaces; contradictory adapters reopen the choice. This accepts
neither an unwritten emitter nor complete guest fault/event semantics, transport
performance, malicious-native containment or combined Track03 correctness.


## CPU exposure review — claim16, root

**Reviewer:** root, independent of memory authorship, September20.
**Initial verdict:** return three concrete discovery inconsistencies before
accepting the scoped profile proposal. Full question1 remains open on the stated
compatibility-register seam independently of these returns.

Read the submitted feature/topology/cache/XSAVE/MSR table against Intel
253666-082US CPUID descriptions and253668-092US APIC/control contracts. The
virtual cache geometry,40-bit physical/48-bit linear widths, one-thread-per-core
IDs and standard832-byte extended-state layout are coherent choices. Explicit
MCE/MCA, XSAVEOPT and ERMS exclusions leave the stated x87/SSE/AVX2 surface
intact and must remain visible as profile compatibility limits, not evidence
that any code generator is incapable. No physical Haswell-equivalence or stock
boot result is inferred.

- **R16-1:** CPUID.1 EDX’s enabled list omits MTRR although42 and16 select
  MTRRCAP/DEF_TYPE/range registers. Intel CPUID bit12 explicitly enumerates that
  facility. Align discovery with the selected supported mechanism.
- **R16-2:** Intel082’s80000001 SYSCALL/SYSRET footnote enumerates the flag as0
  outside64-bit mode. State this execution-mode-dependent bit; LMA alone includes
  compatibility mode and is insufficient. This complements, rather than replaces,
  the selected Intel compatibility-mode instruction rejection.
- **R16-3:** Intel092 §13.4.3 states that clearing IA32_APIC_BASE[11] also clears
  CPUID.1 EDX.APIC. Derive discovery from the authoritative global-enable state;
  do not conflate it with SVR software disable.

These require table/state-coupling corrections, not a new experiment. The
remaining MISC_ENABLE/microcode/platform register contract must still settle
its actual allowed fields and effects, including CPUID limiting where selected;
a named Haswell identity or permissive upstream MSR default cannot settle it.

**Recheck and final verdict:** accept the amended scoped CPU exposure/state
proposal; R16-1–3 are closed. Re-read the actual table: MTRR is enumerated,
SYSCALL discovery uses64-bit execution rather than LMA alone, and APIC discovery
follows global enable independently of SVR. OSXSAVE remains tied to its committed
control bit. Cache fields now explicitly distinguish the WBINVD scope flag;
geometry arithmetic and the standard XSAVE576/832-byte boundary agree with
the primary CPUID definitions. No physical-cache or instruction-throughput
claim follows from those virtual geometry values.

This accepts the profile’s chosen fields and architectural dependencies, not
question1 closure or every binary built for physical Haswell. The explicit
optional-feature differences and required full x87/SSE/AVX2 state remain in
force. MISC_ENABLE/microcode/platform register fields, their Linux call sites
and their effect on discovery still need the named finite compatibility pass.
No substantive return remains for16’s stated scope; runtime/boot, complete
semantic-component conformance and generator ranking remain unestablished.


## Compatibility-register review — claim17, root

**Reviewer:** root, independent of memory's authorship. **Verdict:** accept17
and the scoped question1 virtual CPU exposure/state/control contract composed
with12/13/16 and INV03-04 claim42.

Read the complete proposal and independently inspected Intel Vol4 Table2-20's
MISC_ENABLE fields, generic BIOS_SIGN_ID and CPUID-limit definitions, Intel
Vol2A082 CPUID visibility/out-of-range rules, and retained official Linux v6.12
intel.c, microcode.h and microcode/core.c. Linux's mandatory revision helper
writes8B, executes CPUID1 and reads8B even though the hypervisor bit disables
the microcode loader. The proposed latch supplies that path; rejecting an update
trigger does not replace the required identity interface. Safe platform/misc
feature probes may fault without forcing fictitious successful values.

The write mask agrees with the declared virtual register: only0/22 change,
read-only status is reinstated, and unsupported set bits fault before mutation.
CPUID limiting and per-vCPU context generation are coupled. The model-specific
reserved low bits are not replaced by generic thermal-control storage.

XD-disable34 is an explicit omitted BIOS control, not an assertion that physical
Haswell lacks it. This keeps the selected NX feature and EFER.NXE behavior;
it does not infer an unsupported active-NXE transition. Likewise revision0,
platform ID0 and no update engine are declared virtual identities, not promises
of physical Haswell equivalence or firmware patch compatibility. Acceptance is
for this exposed virtual profile and the selected Linux paths. A requirement
for those omitted physical-model controls would reopen the profile.

The finite profile now states discovery, architectural state, control transitions,
time coupling and compatibility-register effects. Question2 still owns semantic
component adaptation; question5 owns complete fault/partial-effect behavior;
question8 owns event reset/reception composition; question10 owns actual loader
and lifecycle integration. These remain substantive architecture work, not
claims of implementation conformance hidden inside profile acceptance. No stock
Linux boot, full opcode correctness, performance or generator ranking follows.

## Inventory reconciliation check — translator, September20

**Verdict:** editorial consistency check passes; no architecture acceptance or
new source finding. Re-read the current summary, all twelve ordered charter rows
and the common dependency table against the owning current records and cited
review dispositions. Previously returned stale references are corrected:
guarded EL1 is selected; the unbuilt FEX preparation is not a TCG gate; selected
semantic evidence includes43–50; production containment is distinguished from
accepted question12; and72/73 with root Returns51–52 close the scoped question11
contract. Both accepted-question lists include11 and leave2/5 open.

Question5 explicitly retains claim51/Return41's provisional DR6 fusion ordering;
accepted delivery and retirement ownership do not silently accept that semantic
choice. No codegen cost/speed ranking or complete implementation follows from
these statuses. The table has exactly rows1–12 without omissions or duplicates.
The dedicated retrospective link resolves to
`docs/retrospectives/track03-architecture-drift.md` and is labeled history, not
evidence or an alternate inventory. No remaining stale item from this return
is identified; the combined architecture remains unaccepted as stated.

## Claim18 integration review — translator, September20

**Verdict:** accept the conditional integration argument, not the completed
architecture or questions2/5. Reviewer is independent of root's claim18 authorship.
Used existing accepted records only; no new source or runtime evidence. No
load-bearing contradiction found in the ten stated interactions.

Checked the actual composition against01 claims11/14/15,02's current semantic
owners and53 refinement,03's69–73 device/authority/lifecycle contracts, and04's
coordinated misses, ordering and backend completion contracts. In particular:

- Source registration and mutation share one publication ordering point; host
  compilation is outside that exclusion. Operation-bound UC fetch inputs retain
  claim14's restriction and completed effects rather than becoming reusable WB
  snapshots merely because the host sees immutable bytes.
- Coordinator ownership spans an admitted walk/access and its bookkeeping, not
  external disk/network completion. Device MMIO registers provide bounded local
  transaction results while real I/O proceeds from staged work. This preserves
  the distinction between a completed register access and completed external I/O
  and supplies no callback-to-held-RAM-grant wait cycle.
- Memory-quiescent acknowledgement does not release code/frame/return pins. The
  landing captures roots before reuse/clobber, and typed continuations retain
  successful effects across pauses. Incarnation retirement does not fabricate
  cancellation, roll back disk effects or permit old callbacks to target new IDs.
- The resident guarded-EL1 compiler/helper trust premise is separate from host
  process containment. The explicit trusted supervisor/ICMP/DNS endpoints are
  not claimed protected from their own malicious policy implementation. The
  fallible network worker retains only its admitted TCP/UDP authority and staged
  channels; no network feature supplies guest RAM, disk or peer task rights.
- Boot/table/device identity and active-time/timer ownership are coupled to the
  same admitted manifest and epoch. Coordination pauses retain the accepted time
  policy, and IRQ delivery follows admitted payload/ring visibility.

This review does not reaccept individual components authored by this reviewer.
It accepts their stated interface composition, conditional on their own reviewed
premises. Named-M4 aligned128 behavior, private OS enforcement/service admission
and inherited numerical conformance remain explicit assumptions; no finite probe
is promoted to universal platform correctness. Costs remain unmeasured.

The open compound-progress and DR6 fusion mapping under question5, and remaining
semantic/numerical boundary under question2, are not supplied by a generic owner
record. Those gaps remain as claim18 states. Once their disposition changes,
recheck only affected interactions rather than infer that this conditional
integration already authorized planning or completed Track03.

## Bounded completion-breadth inventory audit — translator, September20

**Scope and verdict:** independent inventory/breadth check of the current Track03
charter, four owning summaries, combined18 and existing downstream records.
No new source campaign, runtime or acceptance of this reviewer's prior component
claims. Final Track03 acceptance remains open while questions2/5 and the final
composition disposition remain open. No additional architecture-invalidating
counterexample was identified by this bounded inventory check.

| Breadth area | Finding and smallest remaining closure |
| --- | --- |
| Product | Modern unmodified Linux/direct boot/SMP and required devices remain explicit;69 records image admission and optional lifecycle exclusions. Existing records do not supply an extant accepted concrete diverse workload portfolio or quantitative success threshold merely because historical INV00 rows say accepted. Current Q00-01/02/03/04 must bind real records or resolve missing inputs before their use in synthesis. This is not permission to narrow the architecture to a benchmark or single-vCPU milestone. |
| Architecture | All12 charter rows and ten18 interactions are present. Guarded EL1, TCG/adapted semantic policy, resident ownership, source lifetime, coordinated memory and staged devices are distinguished from complete upstream runtimes. FEX/Box, EL0, transport/placement variants and conditional vmnet remain recorded alternatives without invented rankings. Questions2/5 retain their precise semantic/recovery issues; generic accepted interface types do not answer them. |
| Correctness | Source constructions, named-platform premises, finite experiments and future conformance are distinguished. M4 aligned128 and private OS policy assumptions remain visible; M2 parity and every-kernel-bug proof are not invented requirements. Numerical/status and provisional DR6 fusion remain with02. Changes to those owners require rechecking only affected18 interactions; no new all-opcode or full production implementation gate is inferred. |
| Methodology/economics | Previous bounded failures, independent reviews/replicas and exploratory limitations remain retained. No stock boot, universal runtime correctness or speedup follows. Guarding, register transport, coordinator misses, fences, copying, trusted endpoint and numerical adaptation costs are disclosed but unmeasured. Q04-01 and its mechanism questions own ranges/sensitivity; Q05-01 owns fair qualified measurements; Q06-03/06 own lifecycle effort and bounded adaptation before implementation. These inputs remain necessary for economic/synthesis claims, not a reason to fabricate performance findings here. |

**UEFI/Windows and licenses.** Existing03 claim69 and its alternatives section
supply the required implication analysis: the Linux direct-loader/mailbox/device
contract does not supply UEFI services/runtime/Secure Boot or Windows loader,
driver and AP-startup compatibility. No implementation or Windows impossibility
is implied. No additional survey is required for that stated implication.
The existing licensing warning covered QEMU/FEX/Box but omitted the newly selected
Bochs numerical slice. Added an explicit exact-file/dependency/notice inventory
warning in02, without inventing its license terms or creating a selection veto.
Planning must include this extraction in its provenance/license work.

**Editorial corrections made:** added a narrow backlog navigation disclaimer
that unavailable historical INV00 acceptance is not current evidence, linked the
main scope handling and current Q00/04/05/06 owners; preserved the historical rows.
Removed the unsupported fixed “microseconds per exit” assertion from Q04-08 and
clarified Q06-06's single-vCPU shell as a delivery milestone, not the product
scope. Root owns contemporaneous01/03 summary reconciliation; those regions were
not edited here. `git diff --check` passes.

**Remaining closure:** resolve the explicitly owned question2/5 matters, then
reconcile the conditional integration and obtain final architecture/breadth
acceptance. Before synthesis makes a material-benefit or authorize-planning
recommendation, bind its actual scope/workload/threshold/cost/budget inputs to
available reviewed records. This audit adds no new indefinite research task and
does not convert downstream implementation checks into architectural blockers.

## Current-direction reconciliation — September 20 owner corrections

Reviewer `/root/direction_review`, independent of root's edits, inspected the
workflow, Track01/Track03 charters, INV03-01..04 active summaries/inventories,
investigation/review templates and the framing/decision-displacement retrospectives.
No archived history was inspected. Review used one inspection and one final
changed-material review; no experiment was run.

**Evidence verdict: accept as documentation reconciliation only.** Scoped
findings, unresolved semantic claims and historical reviews remain intact. No
new correctness, performance or whole-architecture acceptance is asserted.

**Decision/direction verdict: supports the corrected next step.** Active records
withdraw the TCG/QEMU/Bochs preference and automatic native-Intel dependency.
Performance comparison covers viable generators and separately chosen numerical
helpers, with the owner's x87 cost tradeoff explicit. The existing workflow now
requires selective lesson retrieval, in-place current-status corrections and
separate evidence/relevance judgments. The work areas contribute to one bounded
comparison-method pass; experiment budgets still need to be specified before
runs. No substantive blocker or unresolved return remains for this correction.

Comparative justification remains unfinished technical work in INV-03-01/02.
This review does not turn the documentation fix into resolution of the recurrence.

## Performance-comparison method review — September 20

**Reviewer:** `/root/direction_review`, independent of the method and source-entry
authors. Inspected the current workflow and Track01/03 charters, INV03-01's
performance-led method, INV03-02's emission-route appendix, and the two relevant
retrospectives. One review and one correction verification within the shared
30-minute reviewer allowance; no archived history, build or execution inspected.

**Evidence verdict: accept as a bounded exploratory method.** The method keeps
actual artifacts, unimplemented adaptation estimates and captive timing distinct.
This verdict does not independently accept every appendix source claim, approve
unwritten export patches or establish buildability, performance or correctness.
The proposed patches still require their recorded preparation/inspection. Source
availability and successful compilation cannot rank candidates.

**Decision/direction verdict: supports the proposed build/emission step.** The
three generators receive the same fixture/feature requirements and bounded effort;
their own state and helper conventions remain visible. Obtaining full upstream
output is a finite prerequisite to a valid execution comparison, not another
recovery-exporter campaign. Six MOV/live-state fixtures test export readiness,
not representative performance. Later selection requires actual captive timing
of useful work and accounting for missing adapter costs. Numerical-helper costs
and the owner's x87 accuracy tradeoff are independent of generator preference.

**Returns resolved:** the first draft bounded wall time but left diagnostic and
review effort implicit and could read as automatically opening another campaign.
The corrected method caps the entire follow-on exploration at both180 wall
minutes and180 aggregate agent-minutes. Shared setup, each candidate's export
patch/inspection/build/correction, and analysis all consume that allowance.
Retained artifacts plus an execution proposal or insufficiency end the campaign;
an extension requires the owner's additional-budget decision. The separately
bounded method preparation does not reset either allowance.

Container memory/jobs, transfer and disk ceilings, snapshot builds, failure
retention and container cleanup are explicit method requirements, not verified
runner behavior. Candidate-specific failure remains an unknown rather than a
performance loss. No substantive blocker remains for this method; no component
winner, execution-code approval or Track03 completion is implied.

## Compile-only exploration findings — September20

Independent reviewers inspected fresh retained outputs, not just prior claims.
`/root/direction_review` checked FEX/Box64 (about4 minutes), including exact input
bytes/counts, object bounds, relevant hashes and preserved failures.
`/root/comparison_sources` checked TCG (22:41:24–22:42:29UTC), all12,401 hashes,
native/disassembly byte agreement and independently decoded search records.
Each reviewer excluded the exporter they authored from their independent verdict.

**Evidence validity: accept exploratory acquisition only.** All three generators
built and exported seven fixtures. No translated guest execution or timing took
place. FEX's expected-count/cache mismatch exit remains visible and is not counted
as an upstream test pass. TCG's direct-memory Linux-user context is not a captive
system-semantics proof. Failures and corrections have separate retained identities.

**Decision relevance: acquisition uncertainty resolved; selection remains open.**
Raw sizes use different boundaries, ordering and state conventions. FEX includes
CI traps/linker data; Box64 excludes table metadata; TCG omits a shared prologue.
Neither instruction counts nor NOP subtraction establish relative useful-work
cost. Both reviewers support bounded captive body execution with explicit state,
entry/return and excluded-cost accounting; residual adapter/helper/reuse costs
may reverse a fixture result. No generic recovery-field audit is needed before
that experiment. The completed campaign ends with a concrete proposal, not an
automatic extension or component choice.

Final current-record check by `/root/direction_review` confirmed the artifact
matrix and proposal limits, and caught one stale inventory-row instruction to
rerun completed emission work. Root replaced that instruction with the additional
execution-budget decision and changed prospective preparation wording to recorded
status. This correction illustrates the direction check; it adds no evidence.

## Captive paired-body exploration review — September20

`/root/comparison_sources` checked root's execution integration and caught unequal
FEX input flag encoding before execution. It independently verified the focused
host-counter correction in the actual binary, all hardware-profile slice hashes,
exact instruction changes and shifted fault sites. `/root/decision_audit` authored
the independent complete-output oracle, including native fault partial effects,
retained-input/profile/hash admission, schedules and separate clock sanity; root
checked it against real disassembly. `/root/direction_review` independently checked
paired run hashes, reran its retained oracle read-only and recomputed timings and
scaling. It did not self-accept its hardware-profile patch. This is review of
exploratory evidence, not independent experimental replication.

**Evidence validity: accept the scoped exploratory observations.** Retained
`execution-paired-01` passes288 admission and756 longer output checks. Long clock
sanity passes, short admission timing does not. The earlier host-counter defect
is preserved in `execution-software-01`; its one-line correction produced fresh
`-02` and was verified in machine code. No complete captive recovery, stock boot,
formal timing qualification or whole architecture acceptance follows.

**Decision relevance:** FEX leads the inspected live cached-state fixtures after
removable software-ordering costs are addressed. The former TCG/Bochs preference
still lacks performance justification. The next consequential uncertainty is
whether guard/boundary/materialization costs erase that lead; the approximately
0.84ns/body FEX–TCG difference gives an explicit break-even test. Source familiarity,
raw object lengths, rare Intel quirks and perfect x87 accuracy cannot replace it.
Broader control flow, translation/reuse and helper costs remain selection limits.
No winner is accepted and no generic recovery audit is requested.

The final report narrows the sealed checker's overbroad “no adapted-TSO” wording:
a specific hardware-dependent lowering profile was tested, while full adaptation
and general TSO correctness were not. Reporting correction only; raw data remain
sealed. The campaign closed inside its recorded allowance; further work has its
own explicit proposed budget and decision consequence.

## Guard/resident-boundary results review — September20

Reviewer `/root/direction_review` inspected the current method and retained
`boundary-execution-01` manifest, relevant producer/checker/output hashes, oracle
contracts and actual output records. The reviewer authored the guard construction,
but not the helper adapters or result checker; this review does not independently
reaccept that guard. No experiment or oracle execution was repeated. Four agent
minutes are charged to the current60-minute campaign.

**Evidence validity: accept the scoped exploratory result.** The retained phases
report612 admission and1260 longer correctness checks passing. All90 reported
long-batch medians were independently recomputed from actual counter differences
and match. Both clock frequencies are24MHz; long host-clock ratio is
1.000–1.001744. Short admission timing fails its sanity criterion and is not timing
evidence. Fourfold iteration increases give median total-time ratios3.62–4.21
across configurations, with individual variation; this remains exploratory timing,
not independent replication, formal clock qualification or confidence intervals.

**Decision relevance: the prior FEX lead survives some boundaries, not all.**
Across all three live fixtures, FEX remains fastest by the reported medians with
guards alone, with the read-only resident view at every tested K, and with forced
canonical-record publication at K=4/16/64. At forced publication every body
(K=1), that ordering reverses: FEX is7.13–7.26ns/body, versus Box64's4.65–4.97
and TCG's4.81–5.04. The relatively small Box64/TCG differences in that regime
are not a robust universal ranking. The substantial FEX reversal is a concrete
boundary-contract/frequency sensitivity, not evidence against cached-state
residency or permission to mandate spills every block.

This answers the campaign's reversal question and supports stopping it. The next
selection-sensitive missing input is whether an actual required resident helper
and representative block sequence require the expensive publication/clobber
contract often enough to enter the reversal regime. A separately bounded next
comparison should replace the synthetic digest with one concrete required helper
and its declared live-root contract, measuring representative boundary frequency
and useful control flow. Keep cached-root preservation available; do not impose
an every-block canonical image by construction. Another repetition of the same
synthetic sweep or a rare-instruction source audit would not answer that question.
Broader workloads, compilation/reuse, true dispatch and numerical-helper tradeoffs
still limit final generator selection. No automatic follow-on is authorized by
this review, and no component winner or whole-emulator speedup is accepted.


### September20 — required-boundary proposal direction review

`/root/comparison_sources`, independent of the proposal author; bounded review
started23:33:53UTC, charged four agent-minutes within the ten-minute preparation
allowance. No code changes or execution. This reviews direction, not a completed
method or generator selection.

**Accept the proposed next discriminator.** INV-03-01 claim14 requires actual
per-guest-commit stop checkpoints and external-entry admission/pin handover;
INV-03-02 claim37 requires the warmed target veneer to preserve qualified native
roots while validating admission under the coordinator. An uncontended admission
transaction plus those checkpoints measures required work omitted by the digest
probe. Preserve native/deferred roots on success; do not invent a mandatory
canonical snapshot. Include the mutex, complete declared identities and pin
handover, rather than substitute a pending-bit read for admission. Rejection must
preserve the prior execution ownership and guest state. Object sizes1/4/16/64
are controlled sensitivity inputs, not established workload frequencies. One vCPU
can price the uncontended path, not contention or reclamation closure.

Per-cut recovery is the critical admission condition. Current sealed
`fex-hardware-01/outputs/fex.log` lines66–74 show vector EOR, ADD, scalar load,
SETZ and vector store in guest order, with CFINV only after that final store.
Post-ADD checkpoints therefore need the direct-carry flag recipe until CFINV,
not the final-body inverted-carry recipe everywhere. An earlier review message
incorrectly suggested FEX placed vector EOR after ADD; the retained log refutes
that recollection. Box64 and TCG likewise require actual intermediate roots,
not assumed final memory slots. The proposal correctly stops timing a candidate
without valid prefix cuts instead of placing arbitrary checks at its tail.
Use the actual entry identity for next guest RIP: INV-03-02 claim12 explains why
stored EIP may be stale on direct chaining. Slow semantic event delivery stays
explicitly outside this narrow stop/admission measurement. The proposed finite
budget, run limit and consolidated remaining comparison scope are appropriate;
this review does not authorize a new execution or supply independent acceptance
of previously authored FEX binding code.

### September20 — claim19 queued-fault protocol review

`/root/decision_audit`, independent of root's claim19 authorship; eight agent-minutes
charged to the bounded source/protocol review, including challenge and returns.
Read the current owner challenge, claim14's execution/lifetime construction,
INV03-04 rendezvous revisions1/2, admitted-route/remap rules, and the complete
claim19 proposal and returned edits. No code or experiment was authored or run.

**Accept claim19 as a conditional architectural revision.** No remaining
substantive blocker requires a universal hot per-instruction stop poll. Peers may
finish bounded old-view execution before mutation; a faulting requester retains
its admitted instruction and cold-completes only that instruction before current-
source dispatch. This reuses revision2 PAUSED/DRAIN_SERVICE rather than adding a
second state machine or rediscovering native-state retention.

Two concrete returns are closed in the inspected final text:

- **Admitted route is stronger than an inactive frame.** CPU0 can admit route R
  and shadow-fault before its access while CPU1 requests R→S. Merely dropping an
  active grant would permit incorrect rebinding or stale retry after remap.
  Claim19 now retains a separate completion obligation: bounded old-route service
  or semantically permitted effect-free cancellation precedes the conflicting
  remap, acknowledgement and reuse. Only an unadmitted next phase can rebind.
- **Mutex release is not guest progress.** Releasing a lock between indefinitely
  queued backend requests could leave ordinary admission closed forever. The
  final bounded-batch rule admits an actual round-robin ordinary interval after
  at most eight heads, under the stated healthy-host premise. New requests queue;
  faults remain serviceable. This closes algorithmic starvation without a
  per-instruction polling requirement.

The simultaneous-fault schedule is coherent only with safely detachable phases,
no drain-needed lock retained, actual memory-completion acknowledgements, and
bounded dependency service rather than recursive coordinator acquisition.
Self-modifying A cannot resume stale following B; the saved decode and completed
MMIO identities must survive every queue/revalidation step. Finite code size does
not bound an internal spin CFG, REP or helper: their bounded stop opportunities
remain load-bearing. Synchronous debug/trap and interrupt-shadow obligations keep
their precise boundaries through specialized lowering or cold exits; ordinary
asynchronous-event deferral does not absorb them.

Pinned native frames, code/metadata, routes and in-flight targets remain lifetime
references. Existing finite per-vCPU and service reserves must cover them before
admission; inactivity is not reclaimability. The accepted change is the protocol
and its explicit premises, not proof that an arbitrary emitter supplies the cold
completion recipes, full-ISA correctness, latency bound or speedup. A lowering
that cannot preserve an admitted phase is a counterexample to that lowering,
not permission to use routine VM termination or to assert blanket polling is
unavoidable. Synchronize claim14 and the active INV03-04 direction to this scoped
revision; no experimental or generator-selection conclusion follows.

### September20 — numerical-02 independent result and method review

`/root/decision_audit`, independent of numerical producer author
`/root/comparison_sources`; six agent-minutes charged within the ten-minute
subtask cap and shared consolidated-review budget. Inspected retained
`numerical-02/source/numerical.cpp`, `numerical.py`, `numerical.sh`, inherited
FEX/SoftFloat/Cephes source bindings, manifest, platform/compiler/library records,
all output rows, linked executable disassembly and analysis beneath
`/private/tmp/natemu-track03-comparison-raw`. Recomputed the numerical oracle and
timing checks locally without invoking either timed executable or a VM.

**Accept the finite exploratory account and its direction, not a generator or
whole-emulator performance conclusion.** All37 retained artifact checksums match.
The producing C++ is unchanged in the working tree; the subsequent Python change
only suppresses bytecode files and does not alter the retained computation.
`numerical-02` records successful bounded container execution and cleanup. The
failed container-creation/cleanup attempt remains separately recorded in
`numerical-01`; it is not a numerical result.

The independent calculation used exact rational decoding and a rational sine
series with the next-term remainder below10^-110, rather than the producer's
120-digit Decimal calculation or the tested Cephes/libm implementations. All26
reference values and both absolute-error columns agree within10^-108. The twelve
nearest-rounding cancellation cases give `(2^p+1)-2^p=1` in the tested extended
kernel for p=52..63; native binary64 returns1 only at52 and0 at53..63. At the
fourteen sine points, the retained extended outputs lie within half an extended
ulp of that independent reference (largest nonzero case approximately0.4561ulp).
This is a finite sample, not a numerical bound or Intel x87 conformance proof.

**Input precision is part of one control.** Sine case13 feeds `1+2^-60` to the
extended path and its rounded binary64 value1 to libm. Its reported binary64
error therefore includes input rounding; it must not be presented as libm's
approximation error for an identical input. The other listed sine arguments are
exactly representable in both formats. No exception/status, nonfinite, extreme
argument-reduction, subnormal or other rounding-mode contract was qualified.

All56 timing records have the declared batch/order/length/variant identity, and
their cyclic-output checksums independently match the26 point results. In the
retained executable, the binary64 cancellation kernel contains actual FADD then
FSUB. Both length loops call the selected kernels on indexed inputs and consume
each output; neither cancellation nor sine has been folded into constants or
hoisted out of its loop. The extended cancellation calls the retained add/sub
kernels; extended sine performs the retained ext80→f128/Cephes/f128→ext80 path.
The native sine wrapper tail-calls dynamically linked glibc libm. Thus these are
indexed kernel/call/checksum throughput measurements, not isolated operation
latencies or timings of FEX's full x87 frontend/state/profile adapter.

| Kernel | Long-batch median ns/call | Observed long-batch range |
| --- | --- | --- |
| Native binary64 cancellation pair |0.770105|0.763020–3.295833|
| Inherited extended cancellation pair |14.490938|14.451978–29.085833|
| glibc binary64 sine |3.570825|3.415625–3.686450|
| Inherited extended/Cephes sine path |228.171875|227.829175–229.530200|

The medians reproduce the author's report. Fourfold loop lengths give median
total-time ratios4.0333,4.0346,3.9176 and3.9208 respectively. Preserve the large
cancellation outliers; seven alternating-order batches do not establish precise
ratios or confidence intervals. Timing uses CLOCK_MONOTONIC_RAW inside the
recorded OrbStack Linux ARM64 container, with CPU quota and uncontrolled
placement/scheduling. It is not a captive-EL1 timing, qualified cross-clock
measurement or independent timing replication. Timing does not include per-call
x87 state admission/status reset; the SoftFloat flags are sticky in the loop.

The supported direction is to keep numerical precision/cost as an explicit
workload-sensitive component choice: this sample demonstrates both a substantial
cost difference and a concrete information-loss case. It supplies no reason to
mandate extended precision everywhere, no proof that binary64 satisfies all
required x87 behavior, and no ranking of FEX versus TCG or Box64. Required numeric
behavior and how often these paths execute remain separate inputs to selection.

### Consolidated branch/FP/DIV retained-result audit — 2026-09-21

**Verdict: ACCEPT the scoped exploratory result and performance consequence;
no generator selection or timing qualification.** Reviewer `decision_audit`
used the remaining 5 agent-minutes of its 20-minute oracle allowance. Together
with the preceding numerical review, this charges 26 agent-minutes against the
shared 30-minute review cap. This reviewer authored `check_comparison.py`; it
therefore does not independently approve that checker. The root reviewer
separately inspected its input models, preservation rules and recurrences before
execution, and reviewed the linked helper implementation and compiled adapters.
This follow-up independently audits retained output identities, guest-result
projections and statistics without importing the checker or producer.

For `/private/tmp/natemu-track03-comparison-raw/comparison-execution-01`, all 114
retained SHA256 entries match. Both native executions returned successfully with
168 admission and 294 timing samples. Independent schedule reconstruction gives
exact case/variant/length/order coverage and matches teardown API accounting.
Direct decoding of all 462 output records independently reproduces the integer
results, branch counts and final path, total completed bodies, final guest RIP,
vector parity and finite FP values/high-lane sentinels. FPCR remains 6 and FPSR
remains zero. These are projections checked independently of the full-state
checker; the root-reviewed checker supplies the broader preserved-state checks.

All 42 short/long medians independently reproduce the manifest. At the retained
24 MHz counter frequency, the long-batch medians convert as follows (ns per
completed fixture, including its declared wrapper/adapters):

| Fixture | Box64 | FEX | TCG |
| --- | ---: | ---: | ---: |
| Empty body/control | 0.925382 | 0.927130 | 0.936190 |
| Zero branch | 1.719316 | 0.659307 | 2.232393 |
| Carry branch | 1.096249 | 0.731786 | 1.922925 |
| Scalar FP add/sub | 2.953688 | 0.956535 | 37.587802 |
| Scalar FP multiply/divide | 4.782359 | 3.172239 | 42.527199 |
| Scalar FP square root | 2.944787 | 2.951463 | 23.304303 |
| Integer divide | 1.382987 | 0.494639 | 1.683235 |

Long timing clock ratios span 1.000000–1.013049. Short correctness admission
ratios reach 1.409091, so admission timing sanity fails while correctness passes;
that failure must remain visible. Fourfold-length median total-time ratios for
the six substantive fixtures span approximately 3.77–4.32, with individual
outliers retained. Some substantive bodies run faster than the empty control,
which reinforces that NOP subtraction and additive isolated-instruction latency
claims are invalid here. No confidence intervals, scheduling control, independent
runtime repetition, or qualified clock evidence were added.

The supported performance consequence is material: the inherited TCG scalar FP
helper path is substantially more expensive on these exact normal inputs than
the retained Box64 and FEX native FP leaves. Its actual helper calls and state
work are included; it was not replaced with handwritten native FP. FEX also has
lower measured medians for these branch and restricted divide fixtures. Square
root gives almost identical Box64/FEX medians, so this sample does not support a
useful ordering between those two there. These observations justify treating
component cost and adapter shape as real selection evidence rather than awarding
TCG a provenance preference.

The tested branch-zero stream alternates outcomes, while branch-carry takes only
the first iteration for one variant; it is not a general branch distribution.
DIV always has a zero high dividend and a nonzero divisor. FP arguments are
finite and exact, with default rounding and AFP controls; NaNs, exceptions,
subnormals and other rounding modes were not exercised. Translation, dispatch,
helper cold paths, complete recovery and full guest workloads remain outside the
measurement. The result supports a cheaper native normal path with required
cold semantics supplied separately; it does not establish that any candidate
already implements the complete product contract or wins overall.

### September21 — consolidated comparison direction and finite selection boundary

`/root/direction_review`; independent of the consolidated VM producer/oracle,
but author of the TCG helper binding, earlier ordering-profile preparation and
compile instrumentation. This is a direction review, not independent acceptance
of those authored artifacts. Inspected the current workflow, Track03 charter,
INV03-01/02 summaries and inventory, retained `comparison-execution-01` manifest,
case bindings and guest oracle, the prior boundary verdict, and the independently
reviewed `numerical-02` account. No new experiment or download.

**Evidence validity:** the latest manifest records168 admission and294 longer
correctness passes. Long clock sanity passes (ratio1..1.01305); admission timing
sanity does not. This remains exploration, with no fresh decision-evidence run
or independent timing replication. The scalar-FP oracle uses exactly representable
normal values and exact results; it does not qualify exceptional operands,
rounding/status, or the complete SSE contract. Some branch length ratios are far
from4, so the clock-sanity boolean does not establish precision of their timing
estimates. Actual linked QEMU helper execution removes the prior unbound-call
uncertainty for these paths; it does not qualify exceptional recovery. The x87
kernel account separately demonstrates concrete binary64 information loss and
large sampled extended-path cost, under its Linux-worker context; it cannot rank
whole generators or set a universal accuracy tolerance.

**Decision relevance:** this is meaningful comparative progress. The ordinary
live-state, branch and native scalar-FP results motivate FEX as the default
component for the resident-state design. Neither the older TCG/Bochs preference
nor unconditional full80 fidelity follows from the evidence. Numerical machinery
is a separate choice; selection of a generator need not settle every optional
numerical profile at the same time.

The dense canonical/clobber/reload reversal is a real sensitivity result, but
claim19 removes universal canonical materialization as an architectural premise.
It must not become an unavoidable cost charged to FEX. Similarly, absence of
measured whole-workload distributions is not by itself a reason to keep component
selection open indefinitely. A design choice can be an explicit inference under
stated intended-use assumptions; it need not assert universal dominance or prove
the correctness of unwritten emitter code.

A finite route to component acceptance has three parts, assessed together:

1. Establish by reviewed pinned-source/interface reasoning that the selected
   emitter can preserve live state while providing the per-instruction recovery,
   bounded control flow and system-mode interception required by claim19. Name
   any unavoidable hot-path additions. A requirement that the future compiler
   export metadata is a design contract; a source restriction that prevents that
   metadata or requires hot rollback is a substantive counterexample. Actual
   implementation conformance remains planning/validation work once the design
   is shown constructible. The concurrently assigned candidate-binding review
   addresses this question; do not create another generic field audit.
2. Obtain one fresh, independently reviewed finite comparison and independent
   replication of its load-bearing performance observations. Freeze the current
   combined ordinary/boundary/branch/helper scope, measurement controls and
   exclusions before execution. Qualify its clock/uncertainty claims; do not
   promote exploratory files or retry until a ranking appears. If numerical
   fast-path repairs required by the selected SSE contract would change measured
   costs, include their bounded accounting or leave that performance premise
   unaccepted. This does not require full ISA implementation.
3. Record the component preference as an inference for resident cached execution,
   with explicit falsifiers: an unavoidable hot spill/guard/recovery cost that
   erases the advantage, insufficient translation reuse, or a required semantic
   repair that changes the ordering. Compile timings currently have unmatched
   cache/diagnostic contexts; they supply no numeric cold-compile ranking or
   crossover. Keep that term explicit rather than assigning arbitrary weights.
   Unknown distributions do not invalidate a conditional design preference;
   an identified required regime in which the alternative wins does require
   reconsideration. A cold/JIT-heavy performance promise needs its own support.

These parts constitute one decision package, not a queue of per-cell campaigns.
Cap any proposed completion package once, including source corrections, fresh
execution, independent replication and findings review; stop with acceptance of
its exact component claim or named substantive insufficiency. No new execution
is authorized by this review. A FEX default may therefore become a justified
architectural selection without a whole-emulator speedup claim. Today its
performance support is exploratory and its recovery binding is under review,
so the present verdict is a preferred hypothesis, not accepted selection.

Track03 closure additionally requires acceptance of the selected recovery
composition and the charter's combined breadth check. Missing implementation
coverage is a later validation obligation; an unresolved architectural
contradiction is not. This distinction gives the investigation a finite endpoint
without weakening its behavior requirements or requiring an endless workload
survey. Charged8 agent-minutes within the allocated12-minute direction review.

**Concrete proposed binding clarification:** a FEX normal generator with a
permanent checked semantic service for REP/partial effects, privileged/startup32
operations and unsupported FP cases is a coherent component proposal, conditional
on the concurrent binding review. TCG frontend/helper semantics may supply that
service independently of generator choice. The required numerical boundary must
be explicit: masking SSE exceptions does not remove MXCSR status/rounding or
DAZ/FTZ and special-value obligations. Even normal inexact arithmetic can require
status updates. Native lowering must preserve the selected SSE contract, or its
admission/repair/service cost must be included; the owner's x87 accuracy tradeoff
does not silently relax SSE. The latest exact normal-result fixtures establish
neither that broader numerical contract nor its repair cost. Root's proposed
single reviewed fresh primary plus single independent replica can consume the
remaining consolidated allowance after binding acceptance, without restarting
the clock or expanding fixtures. Failure to finish yields the exact remaining
insufficiency, not another automatic campaign.

### September21 — final source correction and campaign stopping verdict

`/root/direction_review`, four additional agent-minutes, completing the12-minute
direction allocation. Read the new claim19 adaptation and numerical/before-image
addendum in `consolidated-bindings.md`; directly checked retained QEMU
`fpu/softfloat.c:231`, `include/fpu/softfloat-types.h`, FEX
`OpcodeDispatcher/Vector.cpp:GetMXCSR/RestoreMXCSRState`, helper initialization
bytes, and existing INV03-02 claim28. No execution or new producer change.

**Evidence:** accept the two factual corrections. QEMU's `can_use_fpu` requires
sticky `float_flag_inexact` (0x10) and nearest-even rounding, unless hardfloat is
disabled entirely. The retained environment begins with zero exception flags,
and these exact normal operations never set inexact. Their repeated execution
therefore stays on the software path. The observed roughly37/42ns add-sub/mul-div
body costs are costs of this initialized state and path; they are not general
QEMU helper costs. This correction does not itself measure the alternative path.
FEX GetMXCSR and RestoreMXCSRState explicitly mask with0xFFC0, discarding sticky
status bits. Exact normal-result PASS does not test that mismatch. Neither source
fact establishes Box64 conformance or a TCG performance preference.

**Existing bounded architectural resolution:** claim28 already selects an
allocation-visible value-root/native-site contract for FEX MOV recovery; claim19
provides queued cold completion, and the new adaptation notes identify a bounded
semantic-service route for REP. There is no need to rediscover these interfaces
or require a completed system translator. However, claim28 explicitly does not
bound added spills, and its safe lifetime construction can itself add avoidable
cost. The latest FP status/before-image requirement extends beyond the accepted
MOV case. Cold native-frame retention cannot resurrect an overwritten value.
These records resolve how an adapter could be constructed, not the remaining
matched cost of the required FP/recovery paths.

**Direction verdict:** accept closing this consolidated exploration now with
explicit selection insufficiency. Withdraw the earlier suggestion that simply
repeating the frozen exact fixtures as a reviewed primary and replica would
complete the decision package: those runs cannot resolve the now-identified
FP status and recovery-liveness burden, or QEMU's unmeasured alternative FP path.
FEX remains a concrete performance-led candidate; no generator is selected by
source familiarity, full80 fidelity, or the unequal numerical paths just tested.
The next action is an owner-visible choice about the intended semantic/service
scope and an additional total resource allowance if that concrete comparison is
worth pursuing. Show the existing contract, missing cost and possible selection
consequence together. Do not start another automatic fixture campaign, restart
the current clock, or reinterpret x87 accuracy priority as permission to drop SSE
status. The current scoped results and architectural contracts remain useful;
this stopping verdict does not imply a technical impossibility or a project kill.

### September21 — combined architecture and completion breadth, integrated package

`/root/direction_review`, independent combined-design review. Inspected current
INV03-01/02 summaries and twelve-question inventory, claim18 composition,
claim19's queued revision, proposed02claim56 native eligibility/root envelope,
accepted02claim57 numerical contract, and the relevant existing state/recovery,
capacity and virtual-CPU contracts. No new experiment or broad source reaudit.
This reviewer authored the earlier helper-build tooling and reviewed claim57;
this section does not independently accept that helper implementation or any
experimental result. The separate method/findings/replication reviews own those.

**Evidence validity:** the accepted queued protocol distinguishes active memory
authority, retained references and admitted-route completion obligations. A
PAUSED native frame releases only detachable active authority. A conflicting
remap cannot commit until the recorded admitted-route obligation completes or
is permissibly cancelled. Code/metadata/stack pins remain until their last
consumer disappears. Reconstructing only from canonical slots would violate
this design, but claim56 explicitly preserves current register/spill/constant
bindings and before-images. Its35 current roots are the fields the admitted
native domain may change, not the whole guest state. Upper vectors, control,
segment, x87 and other unchanged state retain their authoritative backing owner.

No inspected new condition requires universal canonical publication or pending
polling at every ordinary instruction. Bounded internal cycles and checked
external entries permit closure before mutation; precise debug, single-step,
synchronous faults and control transitions keep their special boundaries.
A management pause does not retire an instruction or freeze guest time. Retained
code identity prevents stale following instructions after cold completion. These
are coherent interactions with the accepted memory/event/lifetime owners,
conditional on the actual lowering obeying the selected contracts.

**One actual architectural gap identified, with a concrete proposed resolution:**
'checked semantic service' alone did not select an execution engine for the large
native-declined domain. Many QEMU frontend semantics are generated sequences,
not directly callable resident helpers. Root's proposed hybrid resolves that
choice: adapted host QEMU frontend/TCG AArch64 generation produces checked service
objects for captured instruction/context, with resident system/numerical helpers
and the same state/access/effect/lifetime owners. No FEX-to-TCG IR translation or
upstream CPU-loop ownership is required. REP retains admitted decode and partial
progress through bounded service steps; required startup32/system/AVX behavior
is routed there rather than removed from the guest profile. Full implementation
of those bindings is later conformance work, not a prerequisite to selecting this
specific engine and interface.

The following details must be explicit in that source-reviewed service proposal:

- Compilation/miss waits occur outside EXCLUSIVE with no active RAM grant.
  Native admission already reserves executable completion paths and storage for
  every potentially queued suffix. Drain completion cannot discover a dependency
  on a host compilation that it must wait for. REP/service instructions pin the
  code/helper/phase continuations needed to finish admitted effects before entry.
- Service transitions materialize the actual current state once, and return all
  modified fields/context to their authoritative bindings. A35-root ordinary
  map is not permission to lose a service's upper-vector, control, status or
  segment changes. A completed memory effect is never reconstructed by replay.
- Service objects and continuations join claim14's reference census and finite
  object/storage bounds. Both compiler providers share the finite admitted-job
  bound. A second generator does not create an uncounted allocation/pin class.

**Decision relevance and finite completion requirements:** a conditional FEX
ordinary-generator default with this TCG service engine can close architecture
selection without proving whole-emulator speedup or implementing the full ISA.
It still needs accepted comparative justification for that preference and an
explicit numerical-provider/policy disposition. Neither hypothetical universal
materialization nor source familiarity establishes the winner. Dual compiler
maintenance/footprint, state bridges, service frequency and compile misses are
real costs and preference falsifiers. A finite performance claim remains scoped
to its independently reviewed and replicated experiment; the architecture choice
is a separately identified inference under its stated intended-use assumptions.

Complete the service proposal and claim56 source review, settle the x87 provider/
policy question without silently relaxing SSE, then assess that one finite
comparison and update current composition/inventory. Claim18's original TCG/Bochs
binding is historical and needs a current recomposition paragraph linking the
new choices. Clarify claim56's one-access limit as applying to each admitted
**guest instruction's lowering**, not separately to each AArch64 instruction.
No additional generic correctness survey, native Intel acquisition, M2 gate,
stock-boot prototype or full-ISA implementation is required by this breadth pass.

Product applicability remains the selected virtual CPU/machine and named M4
platform. Discovery and service coverage must agree; reduced native eligibility
cannot reduce exposed features. The retained exact-Intel semantic uncertainties
remain scoped compatibility questions, not evidence for an Intel-machine gate.
The actual loader, complete field/site bindings, emitted guards, service handlers,
resource admission and combined guest tests remain implementation-validation
obligations. An incompatible interface or new wait/reference cycle would reopen
architecture; missing unwritten code alone does not. No unconditional Track03
completion verdict is given before the named selection/service/policy items close.

**Claim58 composition return — accepted conditionally, preference still pending.**
Read the actual appended02claim58 and the corrected56 native eligibility wording.
The proposed service engine is now concrete: the existing QEMU system frontend
and TCG AArch64 backend produce resident service objects, with the previously
specified access/state/exception adapters. Service generation happens outside
active grants/EXCLUSIVE; completion code and storage are reserved before effects.
Pre-effect handoff releases the FEX execution/return reference, queued FEX suffix
completion uses permanent handlers, and TCG continuations stay in the admitted
allocation or permanent code. Both producers share the existing finite job slots
and reference census. Full modified state/context is returned to its owner.
These changes address the named execution-engine, drain-wait and capacity returns
without introducing an IR bridge, second guest owner or nested service-object
stack. Claim56 now restricts each guest instruction's native lowering and sends
other mode contexts to the service rather than dropping them from exposure.

**Evidence validity:** accept this conditional architectural composition against
claims14/19 and the existing state/memory/event contracts. The proposal supplies
an engine and finite ownership/control-flow construction; it does not claim that
all opcode handlers, adapter fields or generated phase maps are implemented.
No new remap-obligation, stale-code, clock/event or teardown contradiction remains
from this review. An actual lowering that cannot provide the required detachable
phase/bounded completion is an implementation-discovered counterexample that
reopens its admission, not permission to violate the contract.

**Decision relevance:** accepted as a concrete candidate architecture. Generator
preference still needs the separately reviewed finite comparative evidence and
stated cost assumptions; the service's cost, dual compiler maintenance and
low-reuse cases remain explicit falsifiers. This review does not accept a speed
ranking or x87 policy. Once those decisions close, updating claim18's current
composition to these accepted bindings and carrying their applicability into the
twelve-question inventory can complete this breadth return. No additional
architecture experiment or generic source pass is requested. Actual complete
emitter/service conformance remains implementation validation.

Total charge16 agent-minutes, including this return, at the allocation limit.

### September21 — integrated finite comparison preexecution review

`/root/comparison_sources`, independent of the method/producer author and oracle
and helper-blob authors. Reviewed the current “Integrated package: reviewed
finite comparison method”, `comparison.py`, COMPARISON/GUARDED_FP paths in
`execution.c`, `check_comparison.py`, and retained `tcg-helpers-03` producing
code/linked layout. This review does not independently accept my claim56 or
my earlier source-binding propositions; root owns their review. It assesses
this finite integration and inference before the new primary/replica execute.

**Accept for the scoped primary and independently rebuilt replica, with the
observation scope stated precisely.** The producer uses actual frozen emitted
words, declared edge/helper relocations, full resident status inputs and timed
FP pre/post checks with staged destination/rollback. The scalar six-instruction
guard preserves NZCV; FP predicates save it in declared scratch. FEX rollback
restores the retained low vector, Box64 stages results in d24 before INS/store,
and negative controls return a registered contained rejection. These negatives
exercise rejection before the first tested instruction commits, not every
second-instruction prefix or completed semantic fallback.

Actual helper calls use the retained QEMU implementations and closed link blob,
not handwritten arithmetic replacements. Verified six helper artifact hashes
against sealed SHA256SUMS and exact ELF-to-raw-blob bytes; the10556-byte blob
occupies0x40008000..0x4000a93b and contains code/read-only data with explicit
contained exception/assertion stubs. QEMU's can_use_fpu requires sticky inexact
and nearest rounding; those controls are present. The finite positive operands
exercise its native path. All code, tables,32KiB state and stack regions fit the
recorded256KiB layout, and helper calls preserve the x29 loop count through the
ordinary ABI. The source checker admits all21 combinations and the executable
self-check passes2800 semantic cases,144 guarded cases and3960 rejected mutants.

The frozen-source rebuild verifies every primary source file and its manifest,
requires byte-identical driver code, and rebuilds without importing newer emitter
artifacts. Correctness admission precedes timing; counter frequencies, register
controls, outcome identities and teardown are checked. Seven randomized paired
batches at two lengths and a separately rebuilt run support the method's finite
relative-cost inference, provided the declared clock and stability conditions
hold. They do not support NOP subtraction, universal cost ratios, service
frequency estimates, recovery conformance or a whole-generator ranking.

One wording correction was sent to the author: the current producer/checker
observes the complete32KiB state and a512-byte DATA window, not all4096 input
DATA bytes. The finite bodies access that window, but the method must name it
rather than claim full guest-memory observation. This scope correction does
not require a new experiment. No other blocking integration defect found in
this bounded review. Eight review agent-minutes are charged; the remaining12
of the assigned20 cover the independently rebuilt replica and result review.

### September21 — independent frozen-source replica and finite-cost acceptance

`/root/comparison_sources` executed the approved replica from
`comparison-guarded-primary-01` using the byte-identical driver and
`--guarded-fp --purpose decision-evidence --rebuild-from ... --execute`.
Replica: `/private/tmp/natemu-track03-comparison-raw/comparison-guarded-replica-01`.
This is the one independently rebuilt replica authorized by the reviewed method;
no rerun, new fixture or emitter acquisition occurred. Replica work began00:49:27UTC.
Three review agent-minutes are charged for rebuilding, execution and results
review, in addition to the eight preexecution minutes:11 of the20 allocated.
The source-author work on claim56 is separately accounted, not accepted here.

**Evidence validity: accept the reviewed finite comparison.** Both primary and
replica completed204 correctness-admission and294 timing samples with all
outcomes passing. Every one of150 retained file hashes per run was verified;
the121-file source sets are byte-identical. The replica independently compiles,
signs/verifies and executes its new binary; all commands terminate successfully.
Both builds report43324 bytes of guest code in the same bounded layout and
41185 accounted API calls for each294-sample timing phase. The observed state is
the complete32KiB resident state and512-byte data window specified by the corrected
method, plus the native/stack observation contract. These checks do not imply
unobserved memory or full-ISA conformance.

Both runs report24MHz host/guest counter frequency and the same admitted TSO/AFP/
FPCR controls. Long-phase host-clock ratios are1.000–1.001832 in primary and
1.000–1.001984 in replica. Short admission timing fails its aggregate sanity
criterion and is excluded from timing inference; its correctness remains valid.
All294 timing samples in each run were retained. Medians below were independently
recomputed from raw counter differences, without NOP subtraction or exclusions.
Ranges contain the four medians (two lengths, two independently built runs), in
nominal ns per body at the observed frequency.

| Fixture | Box64 | FEX | TCG |
| --- | ---: | ---: | ---: |
| branch_zero |1.459–1.752|0.624–0.660|2.194–2.252|
| branch_carry |1.010–1.085|0.695–0.745|1.793–1.945|
| guarded add/sub |2.243–2.473|1.239–1.354|6.221–6.290|
| guarded mul/div |4.037–4.260|3.143–3.299|7.732–7.997|
| guarded SQRT |2.916–3.097|2.948–3.087|1.546–1.609|
| restricted unsigned DIV |1.346–1.471|0.481–0.495|1.612–1.693|

**Decision relevance:** FEX's lower finite cost for both branches, guarded FP
add/sub and mul/div, and restricted integer DIV holds at both lengths in both
runs. For those comparisons, even the observed individual-sample ranges do not
overlap the competing candidates' ranges. TCG's SQRT advantage is likewise
separated. FEX versus Box64 SQRT overlaps and changes order; no winner is assigned
there. The finite native guard/before-image costs therefore do not erase all of
FEX's earlier advantage, and actual helper lowering can win a particular
operation. This supports a conditional, mixed component direction instead of
an assumed all-operation winner.

These are costs of the retained dependency patterns, controls, native code,
adapters and actual linked helpers. They do not price the frequency of native
eligibility, cold semantic fallback, queued recovery, startup32, compilation,
SMP or realistic block distributions. Negative cases prove the specified
first-instruction guard rejection controls, not completed cold service or all
FP exception paths. Claim56's implementation obligations remain with its source/
architecture review. No whole-emulator speed ratio or general FEX conformance is
accepted by this finite-cost result. The primary/replica experiment is complete;
unused review allowance does not authorize another run.

### Lazy x87 C1 source-construction review — 2026-09-21

**Verdict: conditional ACCEPT of the supplied design's source-level
constructibility, not runtime acceptance or a performance conclusion.**
Independent reviewer `decision_audit`; allocation capped at 10 agent-minutes from
the integrated package's reserve. This review examines the root author's stated
claim59 proposal and retained primary sources. Final claim text was not yet
present when these findings were prepared, so this is not approval of unseen
wording. The following premises are load-bearing.

Intel089 Vol1 §8.1.3.2 and Table8-1 distinguish instruction-written C1 from the
sticky numeric exception flags (§8.1.3.3). A single pending C1 producer is
therefore sufficient if each following instruction explicitly preserves,
replaces, clears or defines that bit. There is no growing arithmetic history:
the record copies the actual original ext80 operand value(s), instruction-specific
provider ID and original FCW. Two 16-byte operand slots plus control/identity
metadata fit the proposed 64-byte cap. A later numerical instruction captures
its actual architectural operands, even when those are earlier approximate
results; it must not reconstruct a hypothetical all-extended computation chain.
The immutable value record requires no old code, guest RAM or retirement pin.

Retained Bochs `softfloat.h` exposes `RAISE_SW_C1` and rounding-up set/clear
operations; its extended packer clears earlier-stage hints when the final
rounding decision becomes authoritative. A cold replay can therefore obtain the
same C1 that the previously selected status-capable primitive would produce for
the captured original operands/control. Replay needs fresh local status, the
exact instruction-specific primitive and captured FCW. Merge **only C1** into
current state; discard numerical results and other replay flags, and do not
repeat stack actions, guest memory accesses, pending-exception delivery or
instruction metadata updates. FSIN/FCOS must not substitute for FSINCOS: the
retained Bochs trig source explicitly distinguishes these computations and their
final-rounding status. Existing uncertainty about that provider's exact silicon
transcendental behavior is inherited, not resolved by deferral.

The approximate committed value and deferred C1 deliberately refer to different
numerical calculations: the value uses the owner-permitted approximation, while
C1 comes from the original80 operands under captured FCW. This must be explicit;
it is not C1 for the rounded binary64 candidate. The proposal does not need a
status waiver if the agreed value-only relaxation permits this separation. It
must not silently reinterpret that relaxation as permission to discard C1.

For canonical normal ext80 inputs that convert to finite normal binary64,
ordinary add/subtract/multiply/divide results lie far inside the extended
exponent range. Even cancellation is bounded by the finite 64-bit input
significands; there is no route from these operands to extended underflow merely
because the exact difference is small. Nonzero normal divisors exclude divide
by zero; normal operand classification excludes denormal/unsupported/NaN cases.
With all exceptions masked and PE already sticky, a newly inexact arithmetic
result adds no numeric exception bit. The native candidate still needs its
stated post-classification and staged commit. For actual80
`2^-100 <= abs(x) <= 1`, sin/tan are nonzero and safely normal, cos is bounded
away from zero, and the input is inside the x87 C2 range. The inherited QEMU
helpers provide the existing double/libm numerical routes. This establishes a
narrow status-domain argument, not broad numeric conformance or measured speed.
Stack admission, existing pending-exception checks, TOP changes, FIP/FDP/FOP and
trig C2 clearing remain owner operations. Thus “other status unchanged” can refer
to the numeric exception bits, not every bit of FSW.

All status observers must materialize first, including **FSTENV/FNSTENV** as
well as FSTSW/FNSTSW, FXSAVE/XSAVE, FSAVE/FNSAVE and canonical host exports.
Intel's retained environment description explicitly includes FSW. Restore paths
(FLDENV/FRSTOR/FXRSTOR/XRSTOR), reset paths and known C1 writers replace/invalidate
the descriptor at their architectural commit. Per-instruction undefined C1
behavior must also have a deliberate policy; blanket preservation is not a
substitute for this classification. A save attempt that faults must not perform
its completion/reset actions. Pure C1 materialization before such a fault is
semantically harmless, provided the old architectural value remains available.
Control changes do not alter the captured replay control, and exception/fault
handlers may observe the record only through the same materialization boundary.

Acceptance requires these observer and transition obligations in the final
proposal, a permanent bounded replay handler without compilation/queue
self-dependency, and ordinary resource accounting for the fixed record. Neither
the hot descriptor-write cost nor the cold observation frequency is measured.
This design removes a need to calculate full extended arithmetic eagerly solely
to produce an otherwise unobserved C1; it does not demonstrate that deferral wins
for workloads that observe status after every operation.

Primary sources inspected locally: Intel combined SDM089 extraction at
`/private/tmp/natemu-track03-compound-01/intel089.txt`; pinned QEMU
`target/i386/tcg/fpu_helper.c` at
`/private/tmp/natemu-translator-audit/qemu`; and pinned Bochs status/rounding/trig
files retained in `/private/tmp/natemu-track03-fp49` and
`/private/tmp/natemu-track03-fp48`. No new download, experiment or VM run.

**Final-text review:** the subsequently appended CLM-03-02-59 was inspected
within the same allowance. It incorporates the required observer list,
transactional restore/save behavior, instruction-specific C1 disposition with
checked fallback for missing entries, captured-control/fresh-status replay,
original80 C1 versus approximate value distinction, numeric-exception-only
invariance, stack/pending-exception ownership, bounded lifetime and unmeasured
costs. **Final verdict: ACCEPT claim59 as the scoped source construction stated.**
Implementation conformance and performance remain unproved; the earlier
qualification about unseen final wording is now discharged. Total review charge:
10 agent-minutes, no additional research or experiment.

### September21 — claim20 selection and final charter breadth review

`/root/comparison_sources`, independent of claim20's author. Reviewed claim20,
its current claim18 recomposition, the twelve-question inventory and Track03
completion criteria. For claim56 I rely on root's recorded independent review,
not my own authorship; claims57/58 and59 rely on their separate accepted source/
composition reviews. The finite matched primary/replica evidence was independently
reviewed and replicated above. No new source acquisition or experiment.

**Evidence validity: ACCEPT the corrected claim20 inference at its stated
scope.** The preference is supported by repeated matched advantages across
several ordinary shapes with actual guards, staged FP/status checks and linked
competing helpers. It is not based merely on FEX being constructible, source
familiarity, or the availability of fallback. The accepted recovery construction
removes the false premise that every normal boundary must publish a dense
canonical image; it still charges real root preservation and service transitions.
The concrete TCG service engine preserves exposed behavior outside native
eligibility, rather than renaming an unimplemented generic library as coverage.

One material correction was requested and verified in the final claim20 text:
TCG's measured SQRT advantage applies within its tested state/entry binding.
It cannot justify an unconditional FEX-to-TCG handoff, whose materialization,
admission and reload can cost more than the~1.4ns body gain. The corrected text
retains TCG SQRT in the service binding, permits the native path and makes any
cross-producer choice include its bridge costs. No substantive selection return
remains from this review.

**Decision relevance: ACCEPT questions2/5 and the recomposed architecture as a
conditional implementation direction.** FEX ordinary execution plus concrete
TCG system/compound service is a justified place to invest implementation effort;
it is not a demonstrated optimum for an unspecified workload. Claim20 explicitly
states the gain-versus-adaptation/service/reuse condition and small finite branch
saving thresholds. Native shapes that fail admission earn no assumed saving.
Low-reuse, service-heavy or status-observing workloads can favor another path.
These are retained economic/implementation falsifiers for synthesis, not hidden
claims of workload-wide dominance. No weighted application speedup or uniform
FP winner is inferred.

The lazy-C1 x87 policy is accepted through the separate claim59 source review and
the owner's explicit accuracy-versus-cost direction. It retains instruction-
specific status/stack/exception ownership while allowing the stated approximate
committed values. The original-operand C1 versus approximate-result distinction,
loss of cancellation residuals and strict-provider option remain disclosed.
The earlier primitive measurements motivate the policy but do not measure its
conversions, guards, descriptor writes, cold resolution or application tolerance.
No claim that the complete lazy-C1 adapter is faster is accepted here; synthesis
must preserve those uncertainty/cost terms and the numerical applicability limit.

### Charter breadth disposition

| Breadth | Review outcome |
| --- | --- |
| Product | The selected virtual CPU/machine, required startup32, kernel/userspace, SMP, JIT/source mutation and required devices remain covered by their accepted owners. Restricting native eligibility does not remove guest instructions; checked service supplies the other paths. x87 approximation is an explicit owner-directed numerical policy, not a waiver of SSE status or arbitrary guest behavior. |
| Architecture | The current selection binds questions2/5 to the accepted execution/state, memory, ordering, lifetime, event/time, boot/device and containment contracts of questions1,3,4,6–12. Claim18's ten interaction rows are recomposed through56–59 and claim19. Both producers share one resident authority, finite job/reference census and nonnesting service; reserved completion machinery prevents a drain-time compilation dependency. No new unowned execution engine or wait/reference cycle remains in the reviewed construction. |
| Correctness | Current-root/phase maps, staged effects, admitted-route completion, exact event ownership and full changed-state handoff have concrete conditional contracts. Actual field/site mappings, emitted guards and complete service implementations still require conformance validation. Those are unwritten-code obligations, not permission to assume native frames recover lost values. Retained exact-Intel uncertainties and platform premises retain their stated compatibility/applicability scopes and reopen their owners if a concrete target dependence arises; they do not require a Haswell machine by default. |
| Methodology | The scoped comparative premise uses actual code and independent controls/rebuild, distinguishes primary evidence from architecture inference and standalone numerical costs, and records alternatives, costs and falsifiers. No NOP subtraction, unsupported native coverage, unmatched compilation ranking or repeated microcampaign is used to manufacture selection. |

**Final Track03 judgment:** the coherent architecture and implementation direction
can close under the charter's explicitly conditional architectural scope. No
remaining issue identified in this review invalidates the selected construction
or the limited comparative reason for the preference. This does not close later
implementation conformance or economic/workload acceptance, prove end-to-end
performance, or authorize implementation. Missing workload weights must remain
missing, not be invented to turn the direction into a universal winner.

Before publishing completion, synchronize the current summaries, twelve-question
inventory and claim18 supersession labels: their old “no selected generator” and
“questions2/5 open” entries are now stale. Preserve historical insufficiency
records with their original scope. This is editorial reconciliation of the
accepted result, not another research gate. Five decision-review agent-minutes
are charged within the remaining nine; total independent method/replica/decision
review charge is16 of20. No further experiment is recommended by this verdict.

## ABI draft 0 — independent design/method review, September 28, 2026

**Reviewer/context:** `/root/abi_review`, independent of root's specification
authorship and `/root/abi_contract_audit`'s supporting extraction. Inspected the
workflow, Track03 milestone, inherited Track01 scope, INV03-01 current summary
and inventory, claims14/15/19, and the complete
[ARM64 execution ABI draft](../specs/arm64-execution-abi.md). This is a source/design
review; no code execution, replication or historical archive search was performed.

**Decision and alternatives:** the host-translator/captive-VM architecture and
ABI-first sequence are fixed. The decision here is whether the readable contract,
technical interface and finite validation proposal are concrete enough to present
to the owner and guide a subsequent bounded runtime assignment. Live roots and
resident gateways are compared with universal publication, unrestricted native
calls and a generic interface; the draft discloses their costs without selecting
FEX/TCG or claiming runtime correctness or speedup.

**One consolidated return:** two defects threatened the promised live-state
contract. First, installing a destination entry map without moving values could
silently reinterpret registers or release their old spill frame. The minimum
remedy was an explicit bounded map conversion preserving copy-cycle inputs and
both maps/references until handover. Second, committing a helper result before
restoring its old saved destination could erase that result. The minimum remedy
was outcome-specific restoration and output installation, retaining staged values
and phase metadata until the post-call map becomes authoritative. Closure required
matching adverse controls inside existing T2, not new experiments or test cells.
An editorial clarification identified SP_EL0 as the interrupted stack after
hardware selects SP_EL1 on exception entry.

**Targeted recheck: closed.** Section4 now explicitly reconciles entry maps,
rejects unsupported conversions before transfer and retains map/frame references.
Its gateway restores non-output roots before installing permitted outputs; failed
outputs preserve their prior authority, and staged results survive intermediate
capture. T2 now includes register swaps/vector-spill movement and output/register
overlap. The exception-stack wording is corrected. No further substantive return.

**Evidence validity: ACCEPT as a provisional design and method proposal.** The
draft consistently distinguishes current roots from backing slots, native stops
from guest boundaries, retry from suffix completion, and inactive frames from
reclaimable storage. It preserves admitted-route completion before remap, cold
dispatch after source mutation, bounded queue fairness and forced-exit ownership.
The T1–T11 mapping covers the declared milestone behaviors and negative cases.
The finite fragment test uses actual producer bytes or reports acquisition failure;
hand-derived maps and an inserted helper explicitly do not prove an automated
exporter or upstream helper integration. The 27-cell bundle, independent oracle,
fresh primary/replica, retention and failure rules are internally consistent.
Exact code, inputs, schedules and barrier bindings still require their declared
pre-execution review; this verdict is not that approval.

**Decision relevance: ACCEPT delivery of this self-contained draft.** The proposed
six-working-hour/480-agent-minute runtime ceiling includes all six stages, review,
correction and acquisition, with finite build/run/storage limits and explicit
incomplete outcomes. Its adequacy is unmeasured; exhaustion must produce a scoped
insufficiency, never fixture substitution or a self-renewing review campaign.
Readability and declared limitations support an owner decision without requiring
the owner to audit register maps. Runtime milestone completion, generator choice,
full x86 semantics, stock boot, performance and Track03 closure remain unestablished.

**Bounded disposition:** one consolidated review and one targeted recheck complete.
Charge10 active reviewer minutes, including context reading and this record, within
the15-minute reviewer cap and the assignment's90 aggregate minutes. The proposed
runtime allowance has not started. No new campaign or blocking follow-up is opened;
only synchronize the draft/current-summary review status and deliver the spec.

### Owner-identified paging omission — focused amendment review

**Reviewer/context:** `/root/abi_review`, continuing the same independent draft
review. The owner correctly identified that the initial document and its review
did not make the mandatory x86 paging interface or its experimental coverage
explicit. Inherited memory contracts and ARM remap tests did not make that draft
self-contained on guest virtual-to-physical translation. This return corrects
the earlier acceptance scope; it does not claim the omission was already covered.

Inspected the new readable paging explanation, section2's paging interface,
T12/P1–P8, the amended35-cell method and limits, and INV03-04 claims41/42. No
specification authorship, new architecture selection, execution or replication.
The decision is whether the amendment now specifies and tests the actual guest
paging/runtime boundary, without turning the finite experiment into all-mode
walker implementation.

**Evidence validity: ACCEPT the amendment as proposed design/method.** The
interface distinguishes guest linear address, x86 table walk, GPA/owned physical
route and resident ARM shadow. Context includes architectural permission state
and lifetime generations; access requests preserve intent, privilege, span and
progress. The runtime's committed state controls admission. Miss serialization
extends from first paging read through A/D and access/fault/shadow publication;
read fills cannot bypass first-write D requirements. Internal repair is distinct
from guest #PF/CR2 effects, and ACCESS_COMPLETED applies to one subaccess rather
than erasing an instruction's remaining effects. All writers, CR3/INVLPG/control
changes, admitted old routes and native identity reuse share the stated authority.
Guest fetch permissions do not make x86 RAM native executable. Non-direct routes
remain typed service; hidden PAE state remains architectural rather than a cache.

P1–P8 now require actual guest paging entries to drive the walker and observable
native accesses, table/A-D changes and exception packets. They cover hit versus
miss, write admission, permissions/reserved faults, alias mutation, context reuse,
two-vCPU shootdown, partial/service effects and fragmented large leaves. Manual
ARM remapping expressly cannot satisfy T12. Exact vectors and source/native
maintenance bindings remain required in the actual-code pre-execution submission;
none is being accepted as observed behavior here. Legacy modes and full type
semantics remain inherited obligations with explicitly narrower tested coverage.

**Decision relevance: ACCEPT the corrected self-contained draft for delivery.**
The27 original cells plus eight paging cells total35. Page-pool/run limits and
the proposed six-hour/480-agent-minute ceiling remain finite and internally
consistent. Budget adequacy is unmeasured; the amendment explicitly forbids
omitting paging or silently extending work to make completion fit. No generator,
runtime-conformance, performance or full-emulator conclusion follows.

**Disposition:** no substantive correction or targeted recheck is needed for
this focused return. Charge6 active reviewer minutes within its8-minute allowance;
the runtime budget remains proposed and inactive. Synchronize amendment status
and deliver the corrected contract; no additional review campaign is opened.
