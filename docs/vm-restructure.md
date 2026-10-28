# Emulator restructure and general code generation

Status: owner direction, October 26, 2026. Build in small runnable increments;
native macOS compilation and VM refactoring start in parallel.

## North star and responsibility

**A self-contained, well-structured emulator whose design and code are
validated by tests but not shaped around them.** Passing demonstrations is not
the deliverable. New programs within the supported subset must work through
the same interface without program-specific implementation changes.

The orchestrator owns the quality, coherence and usability of the whole
deliverable, including delegated work. Apply ordinary engineering judgment:
understand the affected execution path, keep responsibilities and state ownership
clear, maintain readable code, and assess integration and maintenance costs.
Subagent completion reports and green tests do not discharge that responsibility.

Reject fixes that recognize test programs, guess expected results, script guest
progress or add unjustified special paths, even when they pass. When a difficult
case fails, fix the responsible general mechanism. If that cannot be completed
within the assignment's bound, report the justified unsupported capability and
its consequence; do not silently narrow a capability while claiming completion.

## Constraints and pivots

- Host-side compilation; generated code executes inside the captive VM under
  the guarded EL1 resident runtime. Native macOS compilation removes the Linux
  VM/container used to build and run the compiler, not the guest execution VM.
- Start with FEX's decoder, IR, optimizer, allocator and ARM64 emitter. FEX is
  the implementation default, not an established full-adapter performance winner.
  Exclude Linux application emulation; explicitly adapt compiler dependencies
  and generated references to state, helpers and dispatch.
- Reuse useful existing code and established components. Refactor incrementally;
  replace scenario-bound machinery where extraction would preserve the problem.
  Neither preserving all I4-V code nor a wholesale rewrite is the objective.
  Do not build a new code generator, IR, optimizer or assembler from scratch.
- A pivot needs concrete evidence and an engineering-sound alternative assessed
  for correctness, reuse, integration, performance and maintenance. A demonstrated
  FEX incompatibility may justify another established compiler; local difficulty
  alone does not. Explain the reason and consequence, and bring changes to the
  fixed design or product scope to the owner before dependent work. Routine
  implementation choices proceed without another approval round.
- FEX compiles unprivileged guest code. A translated block ends before a system
  instruction (control/descriptor/MSR/segment loads, interrupt delivery and
  return, port I/O, halt, privileged TLB/cache maintenance), which the resident
  runtime executes as a semantic transaction. Guest exceptions, paging and
  segmentation are runtime obligations even where FEX's application model
  assumes otherwise. Change this split only with evidence and owner agreement.
- Early slices may return to the host for each translation, but the interface
  must not preclude a VM-resident code cache, block linking and dispatch: the
  product goal is lower emulation cost than QEMU's software translator.

## Steps

**1. Native macOS compiler.** Build the necessary FEX compiler subset as a local
executable or library. Remove Linux application-runtime dependencies and replace
only platform utilities compilation needs. Accept supplied x86 bytes, guest PC
and supported CPU context; export ARM64 code, relocations and available metadata.
Remove fixture selection and expected-body matching from this compilation path.
Done for this increment when new supported inputs compile locally without Docker
or a Linux VM. This establishes compilation, not safe execution or complete
recovery metadata.

**2. VM refactor, in parallel with step 1.** Organize by responsibility: host/VM
lifecycle, CPU state, memory, execution, coordination and diagnostics. Consolidate
duplicated setup and low-level operations; use clear names, explicit state and
coherent functions. Separate architectural state and semantic logic from transport,
fixture initialization and native-register reconstruction. Keep mode-specific
semantics where they differ.

Start with lifecycle, CPU state and memory. Reshape recovery, native-register
reconstruction and dispatch together with the first connected slice (step 3),
because their form depends on the recovery information the compiler can produce;
until then keep them working, not redesigned.

Move test programs, expected results, scripted mutations and workload termination
into test drivers. Turn fixed-page memory adapters and fixed-count scenario queues
into reusable operations over explicit machine state. Preserve useful walkers,
capture gates, synchronization and publication primitives with their limits.
Work module by module; a refactored mechanism is ready when callers and tests use
its ordinary operations without passing a test identity. Moving the same scenario
logic into better-named files does not satisfy this step.

End state for this step: emulator crates organised by responsibility (lifecycle,
CPU state, memory, execution, coordination, diagnostics), with no module named
after a test campaign. Test programs, expectations and mutations live in test
drivers outside the emulator crates; the `i4v_*`/family modules are dissolved
into those two places, or deleted where the general path covers them. Migration
order: [VM map §6](restructure/vm-map.md).

**3. Connect early and evolve the ABI together.** Begin once the compiler and
the relevant VM mechanisms are ready; do not wait for all refactoring. Use the
existing execution ABI as a provisional starting contract, not proof of an
implemented interface. Agree only the next slice's code-object, state-ownership,
entry/exit and helper requirements, then run compile → admit → execute → dispatch
through them. Refactoring needs internal VM contracts too; neither stream waits
for a complete ABI specification.

First integrate a small supported subset with explicit limits. The first slice
is straight-line integer and ordinary load/store code in 64-bit mode with guest
paging on, one vCPU, ending at a branch, system instruction or unsupported form;
a guest-table fault retries precisely or is delivered with exact prior effects.
Then two vCPUs, then control flow and block chaining. Independent new
programs must run without fixture recognition or hand-written per-program maps.
Admit faultable execution only with its required recovery support. Extend the
producer and consumer together to cover native-site identity, current guest
values, access intent, committed effects, retry/completion and retained lifetimes.
Recovery is a compiler-wide obligation; allocator locations alone are insufficient.

**4. Expand general execution and retire specialized paths.** Add automatic
recovery, memory-access handling, helper/dispatch integration and system semantics
through that same interface. Replace FEX's application assumptions wherever the
supported guest behavior requires it. Reuse applicable I4/I4-V mechanisms and
checks; delete a specialized path when its replacement establishes the required
behavior. Keep the old implementation as reference during migration, without
requiring every obsolete path or internal layout to survive. Start dependent
stock-kernel boot work when the needed general path is usable; complete cleanup
and full ISA coverage are not prerequisites.

## Phase boundary (owner, October 27)

This restructuring pass stops before stock-kernel boot (I5), which is the next phase.
It ends when: slice 2 (VM-resident code cache, in-VM dispatch and block linking,
control flow, first speed numbers against QEMU TCG) works; up to four vCPUs run on the
general execution path; the general path covers the behaviour the old specialized
paths established (paging modes, mode/control transitions, ordering and coherence), so
those paths are retired or kept as thin regression drivers over it; and the handoff to
the boot phase is documented. Broad ISA, devices and exception delivery belong to the
next phase except where these items need them.
Owner, October 28: 32-bit and paging-off modes are out of this phase too (64-bit Linux on the planned boot path does
not use them); the old 32-bit paths retire with that reason and the [S3g survey](restructure/s3g-plan.md) seeds the
boot phase if its kernel needs them.

Performance (owner, October 27): no performance tuning in this phase. Build the
architectural pieces (code cache, dispatch, linking) and report measurements against
the agreed yardstick (qemu-system-x86_64 TCG natively on the host; first targets
integer loop ≤ 1.0×, memory loop ≤ 0.7× QEMU time), but optimisations are deferred and
the targets do not gate this phase.

## Correctness and execution discipline

Use short implementation assignments with a concrete runnable result, an explicit
agent-effort/resource bound including verification, and a stopping condition.
Record these in the existing investigation/task context. At the bound, report
actual progress and remaining limits; do not manufacture completion or expand
the assignment into an open-ended study. These steps overlap and are not
project-wide approval gates.

For the mechanism being changed, use the [contract inventory](restructure/contracts.md),
the [current program inventory](restructure/programs.md) and prior failures to
locate useful checks. Existing tests are candidate evidence,
not automatically correct or applicable. Establish expectations from the relevant
architectural requirements and explicit design decisions; keep, correct or replace
the affected checks with an explanation. Investigate discrepancies instead of
blindly satisfying or discarding old tests. There is no whole-suite certification
phase before implementation.

Test VM mechanisms directly and compiler output independently; integration must
also satisfy independent architectural expectations. For general programs the
reference is pinned QEMU (currently the existing pinned container image; the
no-Linux-VM rule applies to compilation, not to the reference), comparing final guest
registers, flags and memory; use the existing independent models for properties
QEMU cannot observe (paging faults, ordering, recovery, lifetime). Expected
results are never hand-written per program. Retain guest-state, memory,
fault and progress checks, plus traces, invariants and binary checks where needed
for isolation, ordering and lifetime. Test instrumentation observes or controls
real synchronization/failure sites; it does not supply instruction semantics or
expected guest results. Run focused applicable regressions during changes and the
full applicable suite at integration checkpoints.

Use independent agents for consequential reviews and evidence replication under
the [workflow](research/README.md). At runnable integration checkpoints, reviewers
check expectations, correctness, readability and the whole execution path—not
only reported passes. They supply new programs within the claimed subset and
challenge special-case fixes. Keep authors and reviewers separate. Review and
testing accompany implementation; a complete test audit, finished refactor or
final ABI is not a prerequisite to making progress.

## Next phase scope (owner, October 28)

Build every feature a stock-kernel boot needs — interrupt and exception delivery (IDT, IRET, local APIC, timer,
IPIs), the instruction coverage Linux uses (LOCK forms, string/REP, SSE, x87 at reduced precision, CPUID, RDTSC),
the system registers (FS/GS bases, syscall MSRs, PAT, CR2, Linux's CR0/CR4 bits), larger guest memory, a loader and
a serial console — each checked on the general path as in this phase. Booting a kernel end to end (integration) is
not part of it. x87 may trade accuracy for speed (FEX reduced-precision mode).
