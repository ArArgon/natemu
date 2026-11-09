# INV-01-30 — pre-execution review

## Screening

1. **Shared problem:** ordinary arm64 VMs already require native paging,
   exceptions, GIC and timers. QEMU TCG implements x86 paging in software.
   Accordingly this submission is limited to instrumentation/foundation
   readiness; it neither opens a generic platform kill nor resolves the
   design-specific x86-to-Arm shadow-coherence question.
2. **Documented answer:** Apple SDK controls already describe TSO, IPA
   configuration and GIC/timer availability. Execution records this payload's
   configured behavior on the available host, without re-testing those
   capabilities as speculative feasibility questions.
3. **Applicable design:** the charter's EL1 shim in Hypervisor.framework, host
   translation, native stage-1 paging and TSO. The immediate subject is a
   handwritten arm64 foundation on M4/macOS 26 with two/four vCPUs; no x86
   translator or Linux guest is present.

## Submission and verdict

- Reviewer: agent `/root/track01_independent_read`, independent of the parent
  guest/host author and `/root/retained_runner` runner author. Reviewed in a
  separate context; no experiment implementation authored here.
- Date: 2026-09-06. **Verdict: revise before VM execution.**
- Material: workflow, charter, investigation, all six `experiments/track01`
  source/build files, review template, SDK 26.4 `hv_vm_config.h`,
  `hv_vcpu_types.h`, `hv_vm.h`, `hv_gic.h`, `hv_gic_parameters.h`; direct Apple,
  Arm, Intel and upstream Linux references cited in the investigation.
- Retained submission: `natemu-raw/q30-foundation-build-01`.
  Reviewer verified all SHA256SUMS, all six retained/submitted source matches,
  `built` status, SDK 26.4, bounded Mach-O segments and build-only inputs.
  Reviewer independently ran `python3 experiments/track01/run.py --self-check`:
  PASS for output guards, failure/timeout retention and Mach-O extraction.
  No reviewer VM execution occurred.

## Substantive blockers

### B1 — Redistributor wake precondition is absent

**Consequence:** interrupt readiness can fail because the test never establishes
that each redistributor is awake before configuring its CPU interface.
**Evidence:** `host.c:cpu` sets group/enable/priority registers, while
`guest.c:guest_main` immediately configures ICC; neither reads or writes
GICR_WAKER. Arm's [GICv3/4 Software Overview, version 3.0, §5.2.1](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/Learn%20the%20Architecture/GICv3_v4_overview.pdf)
requires clearing ProcessorSleep and waiting for ChildrenAsleep to clear before
CPU-interface configuration. No Apple exception to that precondition is cited.
**Smallest remedy:** establish/read back that state per vCPU before ICC setup,
with a bounded wait and retained failure state, or substantiate an applicable
documented platform guarantee. **Closure:** inspect the changed sequence and
its bounded failure path, then a fresh build before execution.

### B2 — Multi-instruction inline assembly loses instructions in this build

**Consequence:** required context synchronization/TLBI completion and interrupt
unmasking cannot be inferred from the C source spelling.
**Evidence:** author disclosed inspection of build-01 with `otool`: semicolon
separators in this Apple assembler are comments. `BARRIER()` and the sequences
in `mmu()` and `guest_main()` therefore omit instructions after the first.
The reviewer also inspected the retained disassembly. In particular, the
`isb; msr daifclr, #2` source does not establish IRQ unmasking.
**Smallest remedy:** use newline separators for every multi-instruction inline
assembly string. **Closure:** inspect all changed strings and a fresh linked
disassembly showing the intended barriers, TLBI and DAIF clear.

## Scope, controls and limitations

The independent sentinel plus adjacent missing 4 KiB page is a meaningful
foundation control. Per-vCPU fault counters, TSO/configuration readbacks,
timer/SGI counters and host exit logs support only the finite stated case.
The explicitly selected EL1 physical timer is distinct from the untested
virtual-timer API path. Stage-1 granule is kept distinct from IPA granule.

Source snapshot builds, refusal of overwrites/repository-contained roots,
retained failed commands, external process-group timeout and file-backed RAM
provide adequate retention for this bounded step. The host watchdog uses wall
clock and is backed by the runner's independent timeout; neither clock is
qualified for performance conclusions. No timing acceptance is implied.

The six-run ceiling, stop-on-unexplained-failure rule and re-review of executable
changes are explicit. Full x86 paging cases remain in the investigation for
later method review. No Linux correctness, Q30 resolution, host-wide ordering,
performance or M2 applicability follows from a foundation PASS. Load-bearing
observations still need findings review and independently checked replication.

## Editorial edits and limits

Only this review thread was written. No author code or investigation content
was changed. No archive or repository git history was read. Research and
Ponytail skills were applied to primary-source inspection and minimal remedies.

## Returns

### Return 1 — 2026-09-06

Same independent reviewer inspected the differences between retained build-01
and `natemu-raw/q30-foundation-build-02`, the current
investigation, and linked build-02 disassembly. All retained checksums and
changed/current source matches verified; manifest remains `built`, with no VM
execution. Runner and assembly vector source are unchanged.

- **B1 closed:** guest reads WAKER, clears ProcessorSleep, bounds the
  ChildrenAsleep poll with the existing diagnostic counter deadline, requires
  both bits clear and retains before/after fields before configuring ICC.
  The host also checks/logs the final state. The external timeout still covers
  a stalled guest counter or trapped MMIO.
- **B2 closed:** all three multi-instruction C inline-assembly strings use
  newlines. Reviewer disassembly confirms DSB/ISB pairs, TLBI followed by DSB
  and ISB, and `DAIFClr #0x2` after the SGI operation. The affected instruction
  synchronization and interrupt-enable assumptions now match the submission.

**Updated verdict: accept pre-execution for the stated foundation-only batch.**
No substantive blockers remain for this bounded step. Run from retained
build-02 sources (or a byte-identical new retained snapshot); any executable
change returns here before execution. This is permission under the research
method to collect foundation evidence, not acceptance of a finding or Q30
resolution. Findings review and independent replication remain required.

### Return 2 — 2026-09-06, diagnostic cancellation registers

Same reviewer inspected retained `q30-foundation-01` manifest, stdout/stderr,
RAM prefix and checksums. It failed after the watchdog canceled both vCPUs:
guest progress was zero and loaded payload bytes matched `guest.bin`.
These facts do not distinguish a setup bug from a payload bug and do not
support a platform or design kill. Screening and foundation-only scope remain
unchanged.

Inspected the sole code change in `q30-foundation-build-03`: cancellation
handling reads PC, CPSR, ESR_EL1, ELR_EL1, FAR_EL1 and SP_EL1 on the stopped
vCPU's owning thread, logs them and preserves the prior termination behavior.
Guest code, runner, inputs and limits are unchanged. Verified all build-03
checksums, six current/retained source matches and build-only status. The
investigation records the failure, diagnostic alternatives, stopping condition
and five remaining slots from the original six-run budget.

**Verdict: accept one further diagnostic run from build-03 sources.** The new
register readout can locate early failure without changing the guest mechanism
under observation. Stop and return with that result before further changed
execution; no successful foundation fact or feasibility conclusion is accepted.

### Return 3 — 2026-09-06, allocator diagnostic submission

Inspected `q30-foundation-02` logs and the complete build-03/build-05 source
delta. Both canceled PCs equal the entry address, with zero SP/ESR/ELR/FAR.
This supports no visible guest progress, not proof that entry was attempted:
SDK `hv_vcpu.h::hv_vcpus_exit` also describes cancellation before entry.
The investigation correctly narrows that inference.

Build-05 replaces file-backed RAM with `hv_vm_allocate`/`hv_vm_deallocate`
and explicit RAM dumps, and adds immediately-before-run plus execution-time
diagnostics. SDK `hv_vm_allocate.h` documents anonymous memory suitable for
guest mapping and its page-size/deallocation conditions. Guest/GIC/TSO code
is unchanged. All build-05 hashes, six current source matches and build-only
status verified. Four diagnostic slots remain, and one trial is requested.

**B3 — Final RAM-dump errors do not prevent success.** `dump_ram()` returns
void after `pwrite` failure and ignores `fsync` failure; the normal path can
then report a successful run with incomplete raw RAM. This threatens retained
evidence validity. Smallest remedy: report/check the final stopped-VM dump's
success, including sync failure, before successful completion. An atexit dump
may remain explicitly best-effort/partial with a retained error message.
Closure requires inspecting the corrected path and fresh build.

**Verdict: revise for B3 before the single allocator trial.** No objection to
the backing substitution or added entry diagnostics themselves. The documented
hard-kill and concurrent-atexit dump limits are explicit and do not support
correctness findings from those partial files.

### Return 4 — 2026-09-06, B3 corrected

Same reviewer inspected the build-05/build-06 delta and verified all build-06
checksums, six source matches and build-only status. `dump_ram` now propagates
both write and sync failure, and the final stopped-VM dump gates overall
success with an explicit completion record. The separate atexit wrapper logs
its best-effort partial outcome. **B3 closed.** Guest, GIC/TSO configuration,
runner and execution limits remain unchanged.

**Verdict: accept one allocator diagnostic trial from
`q30-foundation-build-06` sources.** Inspect its outcome before any further
trial. This changes the RAM allocation mechanism only, alongside already
reviewed host observation/retention changes; it accepts no foundation or
feasibility finding. Unchanged PC still cannot independently prove a platform
entry failure, and partial dumps remain inadmissible as success evidence.

### Return 5 — 2026-09-06, one stack-sampling diagnostic

Reviewer inspected `q30-foundation-03` manifest, logs and all retained hashes.
Both before-run markers appeared, both canceled PCs remained at entry, and
both cumulative execution-time values remained zero. Final stopped RAM dump
reported complete; the run failed after the watchdog. Switching to the
documented guest allocator did not resolve the observed symptom and cannot
currently explain it. No guest or platform feasibility finding follows.

**Verdict: accept one unchanged build-06 source run with one second of host
stack sampling.** The question is where the host threads wait while guest
progress remains absent. Select the exact run's host PID (not a name-only
match), record the process identity and command, and use
`/usr/bin/sample PID 1 10 -mayDie -file RUN/sample.txt`, with a five-second
external bound for sampling. Retain sampling exit status/stdout/stderr and
the report, including failure or target disappearance; complete sampling
before the run's final checksum inventory. Preserve the existing VM watchdog,
outer execution bound, guest/GIC/TSO settings and allocation.

Primary contract inspected: local macOS `sample(1)`, Arguments and Description.
The utility suspends/resumes its target to collect stacks: this is an observer
perturbation, not a nonintrusive timing measurement. The report can locate a
recurring wait but cannot prove no other path ran. An instrumented success
alone does not repair the unexplained earlier failures. Stop after this one
diagnostic and review the result/correction before further execution. Three
slots existed before this run; it consumes one, with no budget expansion.

### Return 6 — 2026-09-06, conditional guest-EL2 configuration trial

Reviewer inspected `q30-foundation-04` stack report, command identity/status and
all hashes. Sampled vCPU-thread frames include `Hv::Vcpu::run`, `hv_trap` and
framework GIC-state synchronization; they do not show those vCPU threads waiting
on the application's condition variable. This localizes observed activity to
the framework but does not establish the cause of its behavior.

Inspected build-06/build-08 changes and verified all build-08 hashes, all six
current source matches and build-only status. The code queries the newly created
configuration's default EL2-enabled value, sets false, reads/logs selection,
and returns 3 before `hv_vm_create` when the default was already false. When
the default was true, it continues with the explicit EL1-only configuration
and adds initial PFR0/SCTLR/ACTLR readbacks on the owning vCPU threads. Guest,
GIC/TSO setup, allocator and time/resource limits otherwise remain unchanged.

**Verdict: accept this one conditional diagnostic invocation from build-08
sources.** A false default ends the hypothesis without VM execution and does
not consume a VM-run slot. A true default permits one trial within the two
remaining slots, followed by result review. Retain return 3 as the declared
diagnostic outcome, not a foundation PASS. The SDK documents query/set semantics
but its create declaration does not supply the default; the branch avoids an
uninformative repeat. No platform, foundation or Q30 conclusion is accepted.

### Return 7 — 2026-09-06, immediate-entry HVC control with GIC

Inspected build-08/build-10 changes in host, runner and guest assembly; other
experiment sources are unchanged. Verified all build-10 checksums, all six
current source matches, build-only status, and instruction word `0xd4000642`
at the retained `_start` entry (HVC immediate 0x32). The conditional EL2-default
early return is removed for this different diagnostic; explicit EL2=false
remains. The runner records/passes an explicit `entry_control` input.

In `--entry-control gic`, both vCPUs retain GIC creation/configuration, host-set
TSO with checked readback, allocator and initial EL1 state. The host requires
the HVC exception class/immediate and one exit per vCPU, stops before executing
the foundation payload, logs final PC/CPSR/ACTLR/execution counters, and gates
`diagnostic_entry_pass` on stopped RAM-dump success. It emits no foundation
success verdict in control mode. The plain variant omits GIC setup but is not
approved for execution in this return.

**Verdict: accept one two-vCPU `--entry-control gic` run from build-10 sources.**
This tests the first-instruction boundary of the failing foundation, not a new
native-platform feasibility question. Inspect outcome before deciding the last
slot. No normal foundation run or without-GIC run is authorized here. Neither
the new marker nor a possible entry PASS establishes that any later mechanism
works, and no previous accepted mechanism is being switched off.

### Return 8 — 2026-09-06, final original-slot control without GIC

Reviewer inspected `q30-entry-gic-01` manifest/logs, all hashes and all six
source matches against build-10/current source. The two-vCPU with-GIC entry
control timed out: initial ACTLR was 2 on each vCPU, the HVC marker was unseen,
both final PCs remained at entry, and reported execution counters were zero.
Stopped RAM dump completed. Starting with HVC instead of DAIF manipulation
did not remedy the symptom; no failure of the DAIF instruction is established.

**Verdict: accept one two-vCPU `--entry-control plain` run from the same
build-10 sources, consuming the sixth and final original VM-run slot.** This
omits only GIC creation and associated host setup; the first HVC, TSO readback,
allocator, explicit EL2=false, initial EL1 state and bounds remain the same.
The already reviewed control oracle emits only diagnostic entry success.

Inspect and record the result before any new execution budget/method. Success
would localize the failing setup to the GIC-associated difference, without
proving a framework defect or excluding an initialization mistake. Failure
would leave the cause broader. Neither result resolves Q30, establishes a
working foundation, or accepts a design kill.

### Batch outcome — 2026-09-06

Reviewer inspected `q30-entry-plain-01` manifest/stdout/stderr and verified all
retained hashes. With GIC omitted, both vCPUs still timed out with entry HVC
unseen, PC at entry, zero reported execution ticks and ACTLR 2 before/after;
the stopped RAM dump completed. Thus GIC omission did not remedy the observed
failure. The PFR0 GIC feature field differs as expected between the controls;
this is recorded configuration, not guest execution.

The original batch contains six failed two-vCPU VM invocations: four foundation
diagnostics and the two entry controls. The EL2-default invocation returned
before VM creation and is separate. No four-vCPU run or independent replication
executed. Negative outcomes and diagnostic observer limitations are retained;
none is excluded or converted into a feasibility kill.

**Reviewed outcome: unresolved test/host entry defect; no accepted foundation,
Q30 resolution, TSO ordering finding, or design/platform kill.** This review
accepts the limited diagnostic disposition and exhausted-budget accounting,
not a load-bearing mechanism conclusion. The investigation's next action is
bounded source/retained-state diagnosis for 30 minutes, followed by a concrete
falsifiable correction/control with a new finite method/budget for review.
No further VM execution is approved by this thread's original batch.

### Return 9 — 2026-09-06, one initial-TSO entry diagnostic

Screening first: ordinary VMs share the need to enter a vCPU; documented
ACTLR_EL1.EnTSO already answers availability, so this is diagnosis of this
unaccepted test's initialization, not a new feasibility question. Applicability
is the failing two-vCPU M4/macOS entry harness only. A control with TSO cleared
cannot satisfy the charter foundation or establish a design kill.

Reviewer `/root/track01_independent_read` inspected the batch-2 submission,
build-10/build-11 source delta, control exit/oracle and runner bounds; verified
all 41 build-11 artifact hashes and six current source matches. Manifest records
build-only status. Only host initial ACTLR selection/readback and the control
name/parser change; all guest bytes and other setup remain unchanged. The SDK
`hv_vcpu_types.h` documents ACTLR bit 1 as the TSO selector. Initial readback
is enforced; final ACTLR is retained for independent result inspection. The
HVC EC/immediate must be observed once per vCPU, watchdog must not fire, and
stopped RAM dump must complete. A printed diagnostic PASS is not acceptance
unless both retained final ACTLR values also have bit 1 clear.

**Verdict: accept exactly one new two-vCPU invocation from build-11 sources,
`--entry-control plain-no-tso`, named `q30-entry-plain-no-tso-01`.** Bounds remain
10 s internal watchdog, 30 s command wall time, 60 s child CPU, 64 MiB/file,
16 MiB RAM. Stop after the result; retain failure without exclusion or repeat.
This is a new one-run diagnostic budget after the exhausted six-run batch.
No normal foundation execution, replication, SCTLR control, or subsequent run
is approved. Success would identify a configuration dependency needing further
explanation; failure rejects clearing EnTSO as a sufficient remedy. Neither
outcome establishes TSO ordering or resolves a Track 01 question.

### Batch-2 outcome — 2026-09-06

Reviewer verified all 45 retained hashes for `q30-entry-plain-no-tso-01`, six
source matches to reviewed build-11, manifest inputs and command logs. Both
initial and final ACTLR values are zero. Both vCPUs retain entry PC, zero
reported execution ticks, and no entry HVC; internal watchdog fired after
10 seconds. Stopped RAM dump completed and the command returned 1.

**Reviewed diagnostic disposition: clearing initial EnTSO was not a sufficient
remedy for this harness's entry symptom.** It does not establish that TSO is
unrelated under every configuration, nor that the platform cannot execute.
The one-run batch-2 budget is exhausted; no further VM run is approved until
a separately submitted finite control receives review. Foundation and all
Track 01 question resolutions remain unaccepted.

### Return 10 — 2026-09-06, one initial-SCTLR entry diagnostic

Screening first: initial native-vCPU entry is shared with ordinary VMs; no
platform capability question is being reopened. The documented controls and
upstream source supply an initialization candidate, not evidence that this
harness's current SCTLR value is illegal. Applicability remains this unresolved
M4 entry harness; the control cannot resolve Q30 or establish a design kill.

Reviewer inspected the submitted one-run method and build-11/build-12 delta,
verified all 41 build-12 hashes, six current source matches and build-only
status. `plain-sctlr` retains initial ACTLR=2 and selects SCTLR=0x30900180;
relative to the failed TSO-enabled plain control, only this initial register
value and its parser selector differ. Guest bytes, absence of GIC, allocator,
owning threads, first-HVC oracle and run bounds are unchanged.

The direct [QEMU v10.0.0 source](https://raw.githubusercontent.com/qemu/qemu/v10.0.0/target/arm/hvf/hvf.c),
`hvf_arm_get_host_cpu_features`, supplies this reset value from an M1-derived
base plus SPAN. The four differing bits are ITD[7], SED[8], EOS[11], EIS[22],
also named in [Apple XNU's SCTLR definitions](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/osfmk/arm64/proc_reg.h).
These references justify a falsifiable comparison, not an M4 validity claim.

**Verdict: accept exactly one new two-vCPU `--entry-control plain-sctlr` run
from build-12 sources, named `q30-entry-plain-sctlr-01`.** Enforce existing
HVC/no-timeout/stopped-dump oracle; independently inspect both initial SCTLR
readbacks equal 0x30900180 and initial/final ACTLR bit 1 set. Retain all outcomes
and stop after one invocation under the unchanged 10 s watchdog/30 s command,
60 s CPU, 64 MiB/file and 16 MiB RAM limits. No repeat, normal foundation run
or replication is approved. Success identifies a configuration dependency;
failure rejects this SCTLR value as a sufficient remedy for this symptom.

### SCTLR diagnostic outcome — 2026-09-06

Reviewer verified all 45 hashes for `q30-entry-plain-sctlr-01`, six source
matches to build-12, manifest and terminal logs. Both initial SCTLR readbacks
are 0x30900180 and initial/final ACTLR values are 2. Both CPUs again show
entry PC, zero reported execution ticks and no HVC. The watchdog fired,
stopped RAM dump completed, and host returned 1.

**Reviewed diagnostic disposition: this alternate SCTLR initialization was
not a sufficient remedy.** No register-validity, platform or design kill is
established. Its one-run budget is exhausted; another concrete method and
finite budget require pre-execution review before execution.

### Return 11 — 2026-09-06, one host CPU-limit launch diagnostic

Screening first: launch conditions affect ordinary VMs as well as this test;
there is no new design feasibility question. No documented platform contract
establishes the proposed RLIMIT_CPU interaction, which remains a hypothesis
about the unresolved harness. Applicability is this launch path on this M4.
Neither outcome can resolve Q30 or establish a platform/design kill.

Reviewer inspected the concrete submission and build-12/build-14 delta;
verified all 41 build-14 hashes, six current source matches and build-only
status. Only run.py changes. Host and guest sources remain unchanged. The
new option requires plain control and unlimited parent soft/hard CPU limits
before allocating a run. It omits the CPU-limit setter only for the host child;
compiler/signing children retain 60-second limits. Core/file limits, setsid,
process-group cleanup, internal watchdog and command wall bound remain.
Format v2 records the selected/inherited CPU limits per command and distinguishes
host/build limits. Native-child synthetic checks compare explicit and inherited
limits. The manifest records the inherited parent limit used by Popen; this is
not a new in-host getrlimit observation, and no stronger claim is needed here.

**Verdict: accept exactly one new two-vCPU invocation from build-14 sources,
`--entry-control plain --host-inherit-cpu-limit`, named
`q30-entry-plain-cpu-limit-01`.** Require unlimited inherited CPU limits in
retained metadata, original SCTLR=0x30d00800, initial/final TSO bit 1 set on both
CPUs, the first-HVC/no-timeout oracle and completed stopped RAM dump. Enforce
10 s watchdog and 30 s outer wall timeout with process-group kill, 64 MiB/file,
16 MiB RAM; host CPU budget is deliberately inherited unlimited for this one
bounded invocation. Retain every outcome and stop for review. No repeat,
normal foundation run or other launch control is approved. Success localizes
a launch dependency needing confirmation; failure rejects omitting this setter
as a sufficient remedy.

### CPU-limit diagnostic outcome — 2026-09-06

Reviewer verified all retained hashes and six source matches to build-14 for
`q30-entry-plain-cpu-limit-01`, inspected manifest/command metadata and decoded
the actual exit/register logs. The host command inherited unlimited CPU limits
and returned 0. Both CPUs produced exactly one exception exit with EC=0x16,
immediate=0x32, PC=0x40000804, initial SCTLR=0x30d00800, and initial/final
ACTLR=2. Reported execution ticks are nonzero (71 and 124). No watchdog fired;
stopped RAM dump completed.

**Reviewed diagnostic observation: this invocation reached the first HVC on
both CPUs when the host CPU-limit setter was omitted.** This positive result,
contrasted with the retained capped baseline, identifies a candidate launch
dependency. It does not establish the framework's internal cause, accept a
platform-wide RLIMIT_CPU rule, or prove later guest mechanisms. A single
successful control does not meet independent replication requirements for a
load-bearing conclusion. The one-run budget is exhausted. Restoring the full
foundation with the concrete launch correction needs its own reviewed finite
method before execution; no normal run is approved by this outcome alone.

### Return 12 — 2026-09-06, restore full foundation with corrected launch

Screening first: this restores the previously reviewed starting-point readiness
work after a harness-launch diagnostic. Native paging, GIC/timer and entry are
shared with ordinary VMs; documented capabilities answer their availability.
The scoped question remains whether this concrete EL1 test foundation exercises
them together with TSO before x86-semantic investigation. No design question
is resolved by a successful native foundation.

Reviewer inspected build-14/build-15 delta, all 41 build-15 artifact hashes,
six current source matches and build-only status. Only the runner changes:
VM execution requires unlimited inherited CPU limits before run allocation,
every VM host child omits the CPU-limit setter, and the superseded diagnostic
flag is removed. Build CPU limits remain 60 seconds; host wall/process-group,
core/file and memory bounds remain intact. Metadata records host inheritance.
No host/guest implementation changed. Re-read guest initialization, tables,
fault handler, IRQ/timer/SGI flow, entry-HVC resume and normal completion oracle.
Normal mode continues at entry+4 and restores all original mechanisms.

**Verdict: accept exactly one new two-vCPU normal run from build-15 sources,
`q30-foundation-05`, with no entry-control selector.** Require each CPU's full
previously reviewed foundation oracle, original initial SCTLR, TSO readbacks
including final host ACTLR, GIC creation, complete stopped RAM dump, no timeout,
and retained unlimited host CPU limits. Inspect exact fault/trace/shared data
rather than treating the printed PASS alone as acceptance. Limits remain
10 s internal watchdog, 30 s command wall time/process-group kill, 64 MiB/file
and 16 MiB RAM, with build CPU capped and host CPU inherited unlimited.
Stop after this invocation. No repeat, four-vCPU run or replication is included.
Success permits independently checked replication preparation; failure requires
a specific reviewed correction. Foundation findings and Q30 remain unaccepted
until the required independent evidence/review is complete.

### Primary full-foundation outcome — 2026-09-06

Reviewer independently inspected `q30-foundation-05`: all 45 artifact hashes,
six source matches to build-15, v2 manifest, actual command and complete logs.
Normal mode created GIC, inherited unlimited host CPU limits and returned 0
without watchdog or stderr. Each CPU exited at the entry HVC and completion
HVC (EC=0x16, immediates 0x32 and 0x30), with initial/final host ACTLR=2.

Independent Python/struct decoding checked the 3824-byte shared file against
RAM offset 0x800000 in the complete 16 MiB dump. Header, both CPU slots and
logged fields agree. Each CPU records ready/done=1, failure=0, guest TSO reads=2,
MMU/cache bits set, exact 4 KiB TCR, supported 4 KiB granule, one synchronous
fault, one SGI and three physical-timer interrupts. Fault EC=0x25, DFSC=7,
read direction, FAR=0x60001000 and ELR exactly match the retained `_fault_site`
symbol. Each seven-event trace contains alias, fault, SGI 1, three timer 30
interrupts and completion, without failure events; local timestamps are ordered
but remain unqualified for timing claims. Redistributor sleep bits are clear.

Raw descriptor checks resolved symbols from the retained binary's nm output:
root/table pointers, all 4096 identity-mapped RAM leaf entries, the alias table
and sentinel, its adjacent absent page, and GIC device mappings match the
submitted method. Host logs show two exits per CPU and no virtual-timer exits
for this physical-timer payload. These checks did not rely on printed PASS.

**Reviewed primary observation: the finite two-vCPU native foundation completed
with its declared mechanisms together in this run.** The launch correction
allows this primary run; no framework-internal causal rule is established.
Independent replication is still pending, so this is not final foundation
acceptance or any Q30 resolution. The one-run restoration budget is exhausted.
Checked replication preparation and a separately bounded execution method are
the next step; no further VM execution is approved by this outcome.

### Independent replication preparation review — 2026-09-06

Reviewer `/root` reviews the separately authored replication preparation and
oracle of `/root/q34_sources`. The reviewer authored the original host/guest
and launch correction, but authored neither this independent checker nor the
replication method. The original experiment's independent reviewer remains
`/root/track01_independent_read`; this return does not self-approve that code.

Screening: native bring-up is shared with ordinary VMs; documented capabilities
establish ingredients, while this finite composition still needs runtime
evidence. Applicability is the exact disposable foundation on the recorded
M4/macOS/SDK, not x86 semantics or platform-wide viability.

Inspected the complete checker, current shared layout/guest/host, source-freeze
method, artifact boundaries, cases, failure/overflow policy, and three-run
budget. Checker hash: `439c864749cf10c035f8414918d5e5e6147241e10dd0ed0cad0d0ff9646d10e9`.
Independently reran its synthetic 2/4-vCPU positives and all 30 corruptions,
and read-only primary-run checking; all passed with no VM execution.
The oracle derives symbols directly from Mach-O, checks raw shared/RAM tables,
exact exception and IRQ traces, HVC instruction/exit identity, stopped state,
source/checksum coverage and v2 CPU/wall limits. It ignores producer PASS
booleans. One requested remedy was implemented and inspected: contradictory
per-vCPU JSON counters now fail against raw data, with a negative self-check.
No substantive preparation/oracle blocker remains.

**Verdict: accept exactly the submitted sequential replica-01 (2 vCPUs),
replica-02 (2), replica-03 (4) rebuild/runs from primary-05/source.** Preserve
the checked oracle source and its outputs separately from sealed run artifacts.
Inspect execution and independent oracle result before each next run. Stop at
the first failure/timeout/artifact mismatch or after three runs, with no
exclusion or repeat. Keep existing watchdogs and all foundation mechanisms on.
This approves replication execution only; findings acceptance follows results.

### Full-Q30 interim return — immutable fetch fixtures and epoch acknowledgement

Reviewer `/root/track01_independent_read`, 2026-09-06, independent code/source
review only. Screening: instruction-cache publication is an ordinary native-VM
requirement; this return addresses validity of Q30's fixed permission/fetch
fixtures, not a new platform capability or Q33 result. The applicability is
immutable test instruction pages in this disposable foundation.

**Source/method judgment: the proposed cache sequence is sufficient for the
fixed low/high executable aliases, subject to the stated mapping and immutability
conditions.** CPU 0 cleans every cache line covering guest IPAs 0x40a04000 and
0x40a05000 (4096 bytes each) using DC CVAU and CTR_EL0.DminLine, then DSB ISH;
all CPUs rendezvous; each issues IC IALLUIS, DSB ISH, ISB; all rendezvous before
any fixture execution. The executable aliases and cleaning alias must reach
the same backing with compatible Normal WB Inner Shareable attributes. Host
initialization must finish before VM execution; no fixture may rewrite those
instruction pages after publication. Retain the initial/final instruction bytes
and publication progress in the completed submission.

Direct support: [Arm's cache-maintenance explanation](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/caches-self-modifying-code-implementing-clear-cache)
specifies clean-to-PoU, completion, I-cache invalidation, completion and ISB,
including line-size derivation from CTR_EL0. [Arm's multi-core explanation](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/caches-self-modifying-code-working-with-threads)
explains why executing CPUs each need their own context synchronization.
For virtual aliases, [Linux v6.12 `sync_icache_aliases`](https://raw.githubusercontent.com/torvalds/linux/v6.12/arch/arm64/mm/flush.c)
uses data clean followed by whole-I-cache invalidation for aliasing caches.
Its [cacheflush.h](https://raw.githubusercontent.com/torvalds/linux/v6.12/arch/arm64/include/asm/cacheflush.h)
implements `icache_inval_all_pou` with IC IALLUIS and DSB ISH;
[cache.S](https://raw.githubusercontent.com/torvalds/linux/v6.12/arch/arm64/mm/cache.S)
implements the data clean via DC CVAU. Whole-I-cache maintenance covers the
transformed low/high instruction aliases without enumerating their VAs.
This supports the proposed method; no actual HVF execution is inferred here.

Inspected updated `park()` and `begin_change()`: owner reads/writes are atomic,
closing the earlier plain-access race. Park captures the epoch before its local
DSB/ISB, then revalidates epoch and gate=2 before recording/storing that epoch's
acknowledgement. A writer cannot complete the unacknowledged epoch while this
CPU owes that acknowledgement. Observing a later epoch through acquire ordering
also excludes acknowledging it using a prior completed epoch's stale gate=2.
This closes the identified delayed-observer acknowledgement issue at source
level; full contention/trace validation remains necessary.

No implementation edits or VM execution by this reviewer. The concrete cache
loop, immutable bytes, fixtures, host integration, independent oracle and finite
execution budget still need final pre-execution review. This interim method
judgment authorizes no VM run and accepts no Q30 or Q33 mechanism finding.

### Full-Q30 code/fixture/budget preparation return — 2026-09-06

Reviewer `/root/track01_independent_read` retained independence from the
implementation and budget-helper author. Screening: QEMU TCG shares x86
translation interpretation, while native VMs do not need this x86-to-Arm shadow
composition. Documented paging/invalidation controls supply ingredients but do
not settle semantic coherence. This review applies to the finite EL1 eager
invalidation candidate, not all emulator designs.

Inspected current q30.c/q30.h, x86.c/x86.h, guest vector/MMU integration,
q30_cases.c, host retention/completion path, runner limits, and check_budget.py.
No implementation edits or VM execution. This return is preparation review;
the independent full-Q30 checker is still being completed and has not received
this reviewer's final execution-readiness verdict.

Earlier concrete findings are closed in the inspected source: owner uses atomic
accesses and park revalidates epoch/gate after synchronization; barriers separate
group-1 shared sentinel reads from writes, group-6 read-only D checks from first
writes, and dormant-root checks from activation. Immutable code publication
matches the separately reviewed clean/invalidate/per-CPU-ISB sequence and has
recorded progress. The two code pages have fixed first/last instruction pairs.

Fixture repairs now include actual peer walks beside group-7 writes/atomics,
with preset target A/D avoiding a spurious equality expectation; five competing
group-8 variants cover same/disjoint leaves, nonleaf/root replacement and R/W/A/D
changes with native table-data reads. Canonical last-word reads and last-pair
fetches, changed 2 MiB write values with readback, both halves of a denied
cross-page write, and post-shootdown fetch/remapping/NX controls are present.
Group-8 loop boundaries now record the live physical-timer counter. ACK/WAIT
records retain operation/fault state, and active-enter/leave records expose
native-access intervals. These additions enable coverage checking; they do not
prove that a particular execution will exhibit required overlap/progress.

Independently checked the budget arithmetic and refreshed source guards without
editing either: 59,424 declared four-vCPU accesses; 1,922,387 bounded records
below capacity 2,097,152 (174,765 spare), with 224 MiB RAM and 256 MiB/file.
Address-prefix enumeration yields exactly 23 child tables plus each root;
two roots plus both SPARE pages bound the table-page union at 50. Recursive use
adds roles to existing pages. Group-8 leaf variants change no table role; PT
swaps change at most two pages and root-subtree swaps at most six. Outside those
variants, charging 50 ROLE events per rescan is conservative. Reviewed the
13/20 scalar-event coefficients and separately charged cross-page paths.
Recovery restores active under the writer lock before unlock/ERET, excluding
another maintenance eviction before scalar completion; this supports the
one-handled-fault successful-path bound. Defects/repeated faults remain retained
failures and cannot justify accepting an overflow. The bound is arithmetic,
not a runtime-duration guarantee.

**Preparation disposition: no remaining definite defect found in these reviewed
code paths or successful-path storage bound. No VM execution is approved.**
Before execution the completed independent oracle and its adversarial controls
must be reviewed, a retained build must match the final source, and the exact
finite campaign must receive execution approval. In particular, require real
faulting/lock-waiting peer overlap, ordered maintenance acknowledgements,
continued timer progress and all comparison flags/destination effects; absent
coverage cannot become a PASS through aggregate group-completed bits. All
accepted foundation mechanisms remain required, and full Q30 remains unresolved.

Retained compile-only follow-up: independently verified all 57 SHA-256 entries
in `natemu-raw/q30-paging-build-02`. All 13 retained source
files match the current workspace, including the budget helper and fixture
model. The manifest is `built`, `build_only: true`; all 17 recorded commands
passed and none invokes the VM host. This closes the current build-provenance
check only. Independent full-oracle review remains in progress; no VM execution
is approved by this follow-up.

### Full-Q30 independent oracle and retirement return — 2026-09-06

Reviewer `/root/track01_independent_read` independently read the full raw checker,
its independent walker, finite-case predicates, native-interval/gate/ACK replay,
A/D visibility bounds, final physical-memory comparison, reachable-shadow
inventory, continued-foundation checks and retained-run provenance checks. No
VM was executed. The 864 fixed signatures supplement semantic replay; they do
not replace it. Unmodeled overlapping native effects remain INCOMPLETE.

The initial checker submission's 23 rejection controls did not exercise the
new semantic/contention predicates: its case signatures used `contention=False`.
The author extracted the actual shared predicates and supplied focused controls
for paging-level permissions, atomic mismatch A/D, retirement/relink and alias
GPAs, atomic/peer-native overlap, fault/writer-wait acknowledgement and active
interval overlap. Independently reran the expanded self-check: 35 deliberately
corrupted cases rejected, including concurrent A/D new-then-old and stale
post-replacement-ACK outcomes. This closes that control-path omission.

The zero-to-zero ordinary-data writes after final table retirement were a narrow
coverage gap: no trap and a zero role did not independently demonstrate a
changed native write effect on this transition. The author changed the retired
ALT store to `0x1100`, added a rendezvous and native read through the other alias,
then changed the reverse-direction store to `0x2200` with the same checks.
No-table-trap and relink assertions remain. Independently inspected the exact
delta and checked the added 128 four-vCPU reads: 1,664 additional bounded
records, **1,924,051 total of 2,097,152**, with 173,101 spare. Request totals are
28,992 for two CPUs and 59,552 for four. The helper's count/hash refresh matches
this delta; its current arithmetic check passes.

Independently verified `q30-paging-build-03`: exact inventory, all 57 artifact
hashes, all 13 current source files and all 17 successful recorded commands.
Manifest status is `built`, `build_only: true`, with no host invocation. This
supersedes build-02 as the reviewed experiment snapshot.

One concrete checker gap remains in the inspected intermediate hash
`fd745e8420d7590f08ff5dc4756504ad0a8278fe6621cf43ee76a26caf980ba8`:
first-to-last MARK8 timer growth allows all interrupts to occur between
contention loops. Independently reproduced this using the checker's own positive
contention fixture with equal start/end counts in each loop and increasing
counts between loops; the predicate accepted it. Require at least one positive
within-loop timer delta per CPU, preserving monotonicity/bounds checks, and
reject this exact negative control. The author has the reproducer.

The later read-only oracle budget is 30 minutes wall time and a planning ceiling
of 4 GiB RSS, sampled each second with termination on an observed exceedance;
it is not an instantaneous hard memory cap. Retain its command, outputs, status,
peak observed RSS and checker hash outside the sealed VM run. Oracle timeout or
INCOMPLETE leaves an executed VM run unclassified and stops the batch, without
rerunning that VM. The concrete supervisor still requires implementation and
independent review before campaign execution.

**Current disposition: experiment code, retirement fix, static storage bound
and build-03 provenance accepted for preparation; no VM execution approved.**
Close the timer predicate/control, freeze and retain the final oracle, and
review the bounded supervisor before the next separately reviewed execution
step. The proposed four-primary/two-replica campaign is not automatic permission
for six arbitrary diagnostics: every failure stops it, and a named diagnostic
requires its own concrete reviewed return. Full Q30 remains unresolved.

Final oracle return before the preparation-pass cutoff: independently inspected
frozen checker SHA-256
`f350dbb0ceb026c600151fab2750dbf59c9064c4513212e24a0e46952ccc403b`.
The actual contention predicate now additionally requires `any(end > start)`
within a marked loop for each CPU. Its negative control gives equal start/end
counts within every loop while increasing counts between loops; this is the
independently reproduced counterexample above. Independently reran the final
self-check: **36 corruptions rejected**, including that counterexample, with
positive raw/A-D/retirement/semantic/contention fixtures still passing. The timer
coverage hold is closed. The evidence remains finite fixture/control preparation,
not an observed VM result.

Independently verified the exact inventory and all six checksums in
`natemu-raw/q30-paging-oracle-01`; its three frozen checker
sources match the reviewed workspace files. Retained self-check command,
sixty-second bound, zero exit, empty stderr and PASS stdout are consistent with
the preparation manifest and reviewed build-03 association.

**Final preparation verdict: experiment build-03 and this frozen independent
oracle are accepted for the stated finite method. No VM execution is authorized
in this pass.** The remaining next step is the concrete 30-minute/observed-RSS
oracle supervisor and its independent review, followed by a separately recorded
bounded execution step. Any source change must receive the corresponding review
return. Q30 remains open until actual primary results, independent replication
and findings review establish the scoped claim.

### Full-Q30 supervisor and bounded primary execution acceptance — 2026-09-06

Independent reviewer `/root/track01_independent_read` reviewed the complete
`run_check.py` and its final changes: canonical output guards for both sealed
inputs, sampling deadlines, copied-source integrity and descendant cleanup.
Primary local sources are `/usr/share/man/man1/ps.1` and the current SDK's
`usr/share/man/man2/{setsid,kill}.2`. They establish RSS units of 1024 bytes,
PGID selection, new-session group identity and group signaling. Using `ps -A`
with numeric PGID filtering avoids the documented legacy-mode ambiguity of
`ps -g`. The target one-second sampling interval, bounded ps calls and summed
RSS support the declared observed ceiling; they do not establish a hard
instantaneous physical-memory cap.

Independently ran the final synthetic suite in the required execution context:
PASS/exit disagreement, INCOMPLETE, wall/RSS/monitor termination, fresh-output
and checksum guards, and normal-exit descendant cleanup all passed. A separate
reviewer control launched a child and grandchild, observed both in the summed
RSS samples, imposed the short wall limit and confirmed no live descendant
remained. Neither control executes a VM. Sandbox ps EPERM is a retained setup
failure, not a platform feasibility result.

Verified the exact inventory and all four artifact hashes in
`natemu-raw/q30-paging-supervisor-01`. Its source matches
the reviewed workspace and has SHA-256
`2953d19e8ea4a85e45f2e0c2df1f6b543654b19225923fdff2aecba96c1e2bfd`.
The retained command exited zero, stdout reports self-check PASS and stderr is
empty. Build-03 and oracle-01 remain the reviewed inputs; the latter's checker
hash is `f350dbb0ceb026c600151fab2750dbf59c9064c4513212e24a0e46952ccc403b`.
The method correctly distinguishes the oracle's input-byte verification from
the supervisor's before/after SHA256SUMS-file and copied-source checks.

**Accepted for bounded primary execution:** the two exact first-run/check
commands in INV-01-30's “Bounded oracle supervision and first full-Q30 campaign
submission,” from the repository with the declared raw root, producing
`q30-paging-01` and `q30-paging-check-01`. If and only if that host run and its
complete frozen-oracle check pass, the same frozen inputs may produce `-02`
and `-03` at two vCPUs and `-04` at four, each with a fresh correspondingly
numbered check directory and inspection before the next VM. Thus this return
permits at most four sequential primary VM invocations, not unrestricted
retries. Preserve the current step's time bound and do not start further work
after its budget is exhausted.

The ten-second host watchdog, thirty-second outer host limit, bounded RAM/log
and 256-MiB files remain in force. The checker has its separate 1,800-second
wall bound and observed 4-GiB RSS ceiling. Any host failure, timeout, overflow,
context mismatch, oracle failure/INCOMPLETE or missing observed coverage stops
the campaign immediately; retain the failed/unclassified run and seek a named
concrete diagnostic review before another VM. Checker failure does not itself
justify rerunning a completed VM. Independent two/four-vCPU replication still
requires separate preparation review after primary results. This execution
acceptance supplies no Q30 finding and does not change the open feasibility
status.

Primary-01 structural/raw inspection while its semantic oracle was still
running: independently verified all 63 inventory hashes and exact build-03
source equality in `q30-paging-01`; all 18 recorded commands passed. Raw state
contains 354,059 contiguous records, 864 complete case BEGIN/END pairs, 28,992
matched operation pairs and epoch 15,163. Both CPUs are complete, inactive and
ACKed to that epoch, with zero global failure and all twelve group bits set.
The entire separate Q30 dump and shared dump equal their RAM slices. Each CPU
has 80 complete group-8 loop-marker pairs; seven pairs on each CPU show actual
within-loop timer growth. The host retains TSO=2 before/after, the entry and
completion HVCs, one foundation synchronous fault and SGI per CPU, physical
timer counts 3 to 17 during Q30, no host virtual-timer exits and a complete RAM
dump. These are independently checked structural/bring-up observations only;
full semantic-oracle classification and independent replication remain pending.
No next VM is justified by this partial audit alone.

Primary-01 semantic-oracle outcome: `q30-paging-check-01` terminated with
`oracle_failed`, exit 1, reporting no observed peer-native overlap with atomic
requests through both table aliases. Independently verified all eight retained
check-output hashes. Wall time was 9.96 seconds and peak observed RSS
1,020,461,056 bytes; no supervisor resource limit fired. This is missing required
coverage, so the primary campaign stops before `q30-paging-02`; the host PASS
does not override it.

Independent raw interval inspection found 128 group-7 variant-0 atomic requests,
64 through each alias, and 1,152 peer-native reads. Neither alias has even a
peer request interval overlapping an atomic request. A plausible fixture cause
is that each peer finishes its sixteen reads during the writer's preceding
MOV/byte writes, before the two atomic requests. This is a diagnosis hypothesis,
not a corrected-run result. A bounded candidate remedy is four synchronized
writer-operation phases with four peer reads per phase, preserving the existing
operation order/counts while placing peer work beside the atomic phases. Any
implementation and execution of that remedy requires a new concrete review;
do not relax the overlap predicate or rerun the VM unchanged.

### Single group-7 scheduling diagnostic acceptance — 2026-09-06

Reviewer `/root/track01_independent_read`; author remains `/root`. Screening:
this is a diagnostic of missing concurrent inputs in the already reviewed
x86-shadow/table-alias experiment, not a new question about generic VM barriers
or Arm capability. Ordinary-VM scheduling facts and documented synchronization
primitives do not establish the required atomic/peer overlap for this candidate.
Any result applies to the finite global-lock/quiescence shadow implementation
and this fixture on the recorded host, not QEMU TCG or the platform generally.

Compared the complete build-04 source snapshot with reviewed build-03. Only
`q30_cases.c` and the budget helper changed. Group 7 now has four phases, each
starting with the existing barrier; the designated writer performs its original
MOV64, byte store, successful CAS and mismatching CAS in order. Peers perform
four reads in each phase, preserving sixteen reads per round. Addresses,
values, comparisons, flags assertions and final barrier/read remain. Target
A/D bits are preset, so intervening peer walks cannot change the expected CAS
comparison value. Group 8 and the paging/fault/maintenance implementation are
unchanged. The frozen oracle still requires observed overlap and can reject
this diagnostic; phase alignment alone is not evidence of overlap.

Independently checked the barrier/event implication. Barriers add no epochs,
access requests or direct log events. Their park path can acknowledge an
existing epoch at most once per CPU, already charged in the bound. The helper's
only substantive delta is documenting this fact and refreshing the exact fixture
hash, `b0a346ee6500bbe2922670235aa1c953be7824ffd0ff0d03ba41d8abf773ed94`.
The existing totals remain 28,992/59,552 requests and 1,924,051 bounded records
of 2,097,152; the helper passes. Synthetic fixture checks remain preparation,
not a substitute for the missing observed overlap.

Independently verified build-04's exact inventory, all 57 hashes, all thirteen
workspace/source matches and seventeen successful compile-only commands. The
manifest is built/build-only and invokes no VM host. Oracle-01 and supervisor-01
retain their previously accepted hashes and are unchanged.

**Accept exactly one named two-vCPU diagnostic VM** using the two submitted
commands in INV-01-30's “Primary-01 coverage failure and single diagnostic
submission”: build-04/source produces `q30-paging-diagnostic-01`; frozen
supervisor-01 and oracle-01 produce `q30-paging-diagnostic-check-01`. Preparation
is bounded to the declared 45-minute step starting 03:17 UTC. Preserve the
existing ten/thirty-second VM limits, 224-MiB RAM, 256-MiB file ceiling and the
checker's separate 1,800-second/observed-4-GiB bounds. This consumes one named
diagnostic slot; inspect host success before invoking the checker.

Any failure, timeout, missing overlap or INCOMPLETE stops without another VM.
Keep primary-01 and its failed check unchanged. Even complete success permits
only findings submission and separately reviewed fresh primary/replication
preparation; it does not automatically resume the stopped primary campaign or
accept Q30. The reviewer authored no implementation change and ran no VM.

Diagnostic-01 result inspection: independently verified its 63 artifact hashes,
exact build-04 sources and eighteen successful producer commands. The complete
Q30/shared dumps equal RAM. It contains 354,433 contiguous records, 28,992
complete operations, 864 complete case instances and final epoch 15,179; both
CPUs are quiescent and ACKed. All 128 group-7 atomic requests now overlap a
peer-native interval, 64 per alias. Each CPU retains seven group-8 loops with
within-loop timer growth. Thus the specific missing-input scheduling hypothesis
has supporting observed evidence, without accepting full Q30.

The frozen check terminates **INCOMPLETE**, exit 2, after 10.02 seconds with
peak observed RSS 1,020,772,352 bytes. Its unsupported effect is overlapping
native data read/write; consequently final physical-memory equality remains
unclassified. Independently verified all eight check-output hashes. No further
VM is permitted by the single-diagnostic acceptance.

A read-only instrumented pass of the unchanged frozen replay identified 1,858
entries into that unsupported-effect branch, all group-8 table-data reads versus
one-fault peer writes to currently tracked PT/PD/root pages. Counts by variant
0/2/3/4 are 428/438/399/593. At reader completion 1,857 peer mutations are still
pending and one CAS mutation is already recorded. The generic branch appears
to treat a serialized trapped table write as an additional unlogged native
write, including a mismatching CAS's unused proposed value. This is an oracle
diagnosis hypothesis. A correction must classify effects from validated
mutation/role/quiescence history, retain genuine unlogged-data uncertainty and
adversarial checks, and be independently reviewed before rechecking these same
retained VM bytes. Do not turn INCOMPLETE into PASS by deleting the condition
or expanding allowed values without that proof. No oracle source was edited by
the reviewer.

### Oracle-02 serialized-write classification and same-RAM recheck acceptance

Independent reviewer `/root/track01_independent_read`; oracle author
`/root/track01_session_check`. Screening: this corrects the evidence classifier
for the existing design-specific Q30 experiment. It asks no new VM capability
question and requires no new VM execution. Applicability is limited to the
reviewed tracked-write recovery path and finite trace format.

Compared the complete checker against frozen oracle-01. The only runtime change
pre-indexes operation IDs named by GUEST mutation records and excludes those
operations from speculative *unlogged native* peer-write values. This is
supported by `q30_exception`: after one tracked mutation it sets the saved PC
to the native helper's return label, skipping a second store/CAS. The existing
replay still validates every indexed mutation's CPU/operation ownership,
recognized fault, GPA, role, gate/quiescence, old/new bytes and unique commit.
A forged future record cannot establish PASS: full replay must reach and
validate it, and all operations must complete. Successful tracked CAS mismatch
commits old-to-old; its unused proposed replacement is not another effect.
The ordinary unlogged-data uncertainty path is unchanged.

Independently reran the complete synthetic suite: 864 signatures and 45 rejected
corruptions pass. New actual-replay positives cover deferred trapped MOV and
both CAS outcomes, a peer read before commit, a post-ACK read and final bytes.
Future-value reads, forged operation IDs and missing mutations reject in each
case. A pending unequal ordinary native store still produces INCOMPLETE.
These controls exercise the specific diagnosis without widening accepted load
values or weakening role/quiescence checks.

Verified oracle-02's exact inventory and all six hashes, reviewed source identity,
unchanged walker/foundation dependencies, retained zero exit/PASS stdout and
empty stderr. Frozen checker SHA-256 is
`cba2fd774b69947559996cbce94c001562bb4947ab60fd1ee2f0bf7ee46ec07c`.

**Accept exactly one read-only recheck** of existing
`q30-paging-diagnostic-01` with frozen supervisor-01/oracle-02, producing fresh
`q30-paging-diagnostic-check-02`, using the exact command in INV-01-30. Preserve
oracle-01 and the original INCOMPLETE check. The existing 1,800-second wall,
observed 4-GiB RSS, output/checksum guards and stop-on-non-PASS policy apply,
within the declared 45-minute preparation step starting 03:24 UTC. No VM
execution or automatic primary campaign follows from this return. Any PASS
classifies only these retained finite-case bytes and awaits results review and
separately reviewed primary/replication work before a Q30 finding.

### Same-RAM PASS verification and corrected primary campaign acceptance

Independent reviewer `/root/track01_independent_read` verified all eight hashes
and the exact inventory of `q30-paging-diagnostic-check-02`. Its copied inputs
match reviewed oracle-02 and supervisor-01; the VM inventory is unchanged and
stderr is empty. The checker exited zero with complete PASS, 864 cases,
28,992 operations, 354,433 records, epoch 15,179 and no unsupported effects.
Consequently its required final physical-memory equality and reachable-shadow
checks completed. Supervision reports 9.95 seconds and peak observed RSS
1,021,100,032 bytes. This classifies the retained diagnostic under the corrected
oracle; it does not erase oracle-01's retained INCOMPLETE outcome or establish
a Q30 finding without primary/replication and findings review.

Reviewed the exact “Conditional corrected primary campaign and replication
preparation” submission. Screening remains unchanged: the experiment exercises
x86 paging obligations in the candidate EL1 shadow design under SMP, beyond
generic native-VM capabilities; its results cannot answer performance or
platform-wide feasibility. No runtime, fixture or protocol changed from
reviewed build-04, and oracle-02/supervisor-01 are frozen reviewed inputs.

**Accept the corrected primary campaign:** exactly `q30-paging-02`, `-03` and
`-04` with two CPUs, then `q30-paging-05` with four, each freshly rebuilt from
build-04/source and checked in `q30-paging-check-02` through `-05` using frozen
oracle-02 and supervisor-01. Inspect terminal producer success and complete
oracle PASS before each next VM. The 130-minute outer bound begins at this
acceptance. Admission of another run must account for the bounded producer
build/query/host commands as well as the separate 1,800-second oracle: the
author clarified a minimum 39-minute remaining-time admission check. Artifact
inspection also consumes the outer bound; do not budget only the host invocation. Keep the existing per-command, VM, observed
RSS, trace/RAM/file, output and checksum limits.

Any build, host, integrity, semantic, coverage, monitor, timeout or INCOMPLETE
outcome stops the entire campaign, with no retries or exclusions. A source
change requires a concrete reviewed return. Existing failed primary and
original diagnostic classifications remain part of the evidence. Four primary
PASS results permit further findings/replication work, not acceptance of Q30.

Read the separately disclosed replica preparation for agent `/root/q34_sources`:
fresh build-04 two/four-CPU runs, the same reviewed oracle/supervisor, separate
run/check names, per-result inspection and the amended 80-minute outer bound
with 39-minute admission per run. Reusing the
reviewed independently authored full checker is explicit. Replica preparation
is concrete, but **replica execution remains pending verified primary results
and its separate acceptance**; this return authorizes only the four primaries.
No VM was executed by the reviewer.


## Independent full-Q30 replication preparation review

Screening: (a) QEMU TCG also implements x86 table semantics, while ordinary
same-ISA VMs supply them natively; the question is this shadow composition.
(b) Documented native mapping/TLBI/TSO/GIC controls do not themselves establish
that composition. (c) This replication concerns the finite eager-quiescence
EL1 shim on the recorded M4, not all emulator architectures.

Reviewer `/root` is separate from replication preparer/executor
`/root/q34_sources`. The reviewer authored the runtime; this is review of
replication preparation, not self-acceptance of its resulting scientific claim.
The retained independent findings reviewer must assess all results. The
replicator authored the earlier foundation checker and pure x86 walker, but
no full-Q30 host/guest/runner. It reuses the independently authored/reviewed
complete oracle and does not claim an independently authored second full oracle.

Inspected the complete retained build-04 runner, supervisor controls and the
replicator's exact source/bundle/command proposal recorded in INV-01-30. Verified
all 57 build-04 hashes, six oracle-02 hashes and four supervisor-01 hashes with
exact inventories. Oracle-02 checker is
`cba2fd774b69947559996cbce94c001562bb4947ab60fd1ee2f0bf7ee46ec07c`;
supervisor-01 is
`2953d19e8ea4a85e45f2e0c2df1f6b543654b19225923fdff2aecba96c1e2bfd`.
No runtime/oracle byte changes are part of this replication.

**Preparation accepted, execution conditional on the retained reviewer's
primary-readiness verdict.** After that verdict, exactly one independent fresh
build-04 rebuild/run at two CPUs may produce `q30-paging-replica-01`. Only its
host PASS permits supervisor-01/oracle-02 to produce fresh
`q30-paging-replica-check-01`; only complete oracle PASS permits the corresponding
four-CPU `q30-paging-replica-02` and `q30-paging-replica-check-02`. Commands and
absolute source paths are the ones in INV-01-30's replication submission.
The executing agent must inspect actual terminal manifests, host state/dumps,
source/checksum identity, supervised zero exit and complete PASS with no
unsupported outcomes before the next VM. All original failures remain retained.

The accepted outer budget is 80 minutes from execution release, with at least
39 minutes remaining before each build/VM: eighteen producer commands each
bounded to 30 seconds plus a separate 1,800-second oracle. Inspection and
artifact retention also consume the outer budget. VM internal watchdog remains
10 seconds; host CPU limits are inherited unlimited, compiler/query CPU limits
60 seconds, core dumps disabled, files at most 256 MiB, RAM 224 MiB and the
reviewed 2,097,152-record capacity retained. Oracle supervision retains the
observed 4-GiB process-group RSS ceiling and one-second sample target, with
monitor/process-group cleanup on all terminal/error paths.

Any failure, missing coverage, inconsistent source/context/checksum, timeout,
monitor failure or INCOMPLETE stops the remaining replication without retries
or exclusions. Checker correction requires review and same-RAM rechecking,
not a replacement VM. Both outcomes go to independent findings review; this
preparation does not close Q30 or any later question. No VM was executed by
the preparation reviewer.

### Corrected primary results: readiness for independent replication

Reviewer `/root/track01_independent_read` independently verified all four
corrected primary runs and checks. Each producer has an exact inventory with
63 valid hashes and exact build-04 source identity; each check has eight valid
hashes, reviewed oracle-02/supervisor-01 source identity and the correct unchanged
VM-inventory association. All producer commands and complete oracle checks
passed with empty checker stderr and no unsupported effects.

| Primary | vCPUs | Operations | Records | Final epoch | Per-CPU Q30 timer counts |
| --- | --- | --- | --- | --- | --- |
| `q30-paging-02` | 2 | 28,992 | 354,279 | 15,171 | 3 to 16 |
| `q30-paging-03` | 2 | 28,992 | 354,291 | 15,176 | 3 to 17 |
| `q30-paging-04` | 2 | 28,992 | 354,557 | 15,166 | 3 to 17 |
| `q30-paging-05` | 4 | 59,552 | 726,351 | 27,214 | 3 to 36 |

Every run completes all 864 case instances and every CPU is done, inactive,
not waiting and ACKed to the final epoch. The four-CPU run has four distinct
per-CPU native roots/ASIDs with the declared geometry, exact entry/completion
HVCs, TSO=2, one foundation synchronous fault and SGI per CPU and no host
virtual-timer exits. Complete oracle PASS includes all required concurrency
coverage, A/D/permissions, retained native mappings and final guest-RAM equality.
Observed checker time/RSS stayed below the declared limits; no exclusion or
extra retry was found. The declared sequential starts and result inspection
fit the outer admission rules. These facts are primary readiness evidence,
not an accepted scientific conclusion or qualified performance data.

**Accept these four primary results as satisfying the prerequisite for the
submitted independent replication.** The corrected primary campaign is complete
and its four-run budget is consumed. The independent replicator's concrete
preparation may now receive its separate execution acceptance: exactly one
fresh two-vCPU and one four-vCPU rebuild/run, with frozen build-04/oracle-02/
supervisor-01, separate retained checks, the amended 80-minute outer cap and
39-minute admission check, and stop on the first non-PASS. This primary-readiness
verdict does not itself execute or substitute for that separate replica review.
No full Q30 finding is accepted yet; original primary-01 failure and both
original diagnostic classifications remain retained.
