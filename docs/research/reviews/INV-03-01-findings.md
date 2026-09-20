# INV-03-01 — source findings review

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
