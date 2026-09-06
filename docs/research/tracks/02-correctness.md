# Correctness

This track defines what research must demonstrate about emulation fidelity
before any performance claim can stand. It is a roadmap, not a finding. Work
on it follows the [research workflow](../README.md).

## Correctness surface

The track must define, and then demonstrate coverage of, the guest-visible
behavior inside the compatibility boundary set by
[Scope and success](00-scope-and-success.md). Each area becomes questions in
the [question backlog](../question-backlog.md), resolved through the
applicable gates:

- **ISA.** Instruction semantics, including flags, edge operands, and
  undefined-versus-defined behavior distinctions the specification makes.
- **CPU modes and privileged state.** Mode transitions, privilege levels,
  segmentation as modern Linux uses it, and control-register semantics.
- **CPUID and MSRs.** A defined, internally consistent feature surface the
  guest can probe, and faithful behavior for every exposed MSR.
- **Exceptions and restartability.** Correct fault classification, priority,
  error codes, and delivery, with faulting instructions restartable so guest
  fault handling (demand paging above all) works.
- **Paging.** Page-table walks, permission checks, accessed/dirty behavior,
  invalidation semantics, and self-modifying or newly mapped code.
- **Atomics and memory ordering.** Guest atomic operations and the guest
  memory model as observed by multiple vCPUs on a host whose memory model
  may differ from the guest's.
- **Interrupts and timers.** Interrupt controller behavior, masking,
  priority, inter-processor interrupts, and timer semantics the guest kernel
  depends on for scheduling and timekeeping.
- **Boot.** The direct-boot path from entry to a fully booted SMP guest
  userspace.
- **Scoped devices.** The minimal headless device set, to its governing
  interface specifications.

## Required verification methods

For each area the track must apply, with approved methods and experiment code:

- **Specification-derived tests**, traceable to the primary specification
  clause they check, not to another emulator's behavior alone;
- **independent oracles** — at least one reference implementation or
  hardware-derived oracle that did not inform the implementation under test;
- **fault injection**, deliberately driving faulting and boundary paths
  rather than only the happy path;
- **property-based testing and fuzzing** over instruction encodings, state,
  and inputs; and
- **SMP litmus, invariant, and stress tests** for atomics, ordering,
  cross-vCPU interrupts, and shared page-table mutation.

## Relationship to performance

Correctness is a precondition for the performance tracks: measurements taken
from incorrect samples cannot support performance conclusions. Any
correctness defect found in a configuration invalidates performance evidence
collected in that configuration until the defect is fixed and the
measurements are redone.

## Exit criteria

The track is complete when the correctness surface is fully enumerated, each
area has accepted evidence of coverage by the required methods, known gaps
are recorded as explicit applicability limits, the end-of-track breadth pass
is recorded, and all surfaced questions are filed in the question backlog.
