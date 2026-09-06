# Feasibility

This track establishes whether the shim design can be built at all on the
supported hosts. It is a roadmap, not a finding. It does not establish how fast
the design is, which variant to build, or whether the guest behaves correctly;
those belong to [Performance estimation](04-performance-estimation.md),
[Architecture options](03-architecture-options.md), and
[Correctness](02-correctness.md). Work on it follows the
[research workflow](../README.md).

## Design under test

A thin shim running at EL1 inside a Hypervisor.framework VM that uses the
native stage-1 page tables, hardware exceptions, the virtual interrupt
controller and timer, and the TSO switch, with translation performed on the
host. The reference is QEMU TCG on the same hosts, measured under
[Benchmarking and validation](05-benchmarking-and-validation.md).

## Working assumptions

Owner decisions of 2026-09-05/06. Each carries a verification item; none is a
question of this track.

- Translation runs on the host, adapted later from an existing x86-to-arm64 code
  generator chosen in [Architecture options](03-architecture-options.md). This
  track uses no translator; where translated code is needed, tests use
  hand-written arm64 blocks shaped like translator output.
- The first CPU enters the guest kernel in 64-bit mode. Secondary CPUs are
  started by the mechanism established under `Q-01-34`. Real mode is out of
  scope pending that question.
- The ISA ceiling is AVX2. Floating point behaves as the eventual code
  generator provides; nothing floating-point is hand-written.
- August 2026 code and results (commit `a121702`) are not reused.
- The host of record is the M4 under macOS 26. An M2 Pro/Max under macOS 26 is
  the second generation, available later; every question is replicated there
  when it is. A "no" on every available generation is a kill; a "no" on one
  narrows applicability (`Q-00-11`).

## Identifiers

`Q-01-01` to `Q-01-23` are retired, never reassigned, and recoverable at tag
`track01-archive-2026-09-06`. This track's questions are `Q-01-30` to `Q-01-34`.

## Questions

Multi-core is a required dimension inside every question. Each states what
must be shown true; no mechanism is prescribed.

1. **`Q-01-30` — Guest paging under SMP.** Inside the VM, with several vCPUs,
   the guest's x86 page tables can be honored by the hardware such that: every
   guest table change the guest is entitled to see take effect does take effect
   by the time x86 requires it; a shootdown from one vCPU leaves no stale
   translation on any other after their invalidates complete; concurrent
   activity leaves the guest's view consistent; and pages that stop being page
   tables stop being treated as such.
2. **`Q-01-31` — Precise fault delivery.** A fault raised by translated code on
   one vCPU, while other vCPUs also fault, reaches the guest as the exact x86
   exception with correct error information, exactly once, restartable; and
   the shim can tell a mirror miss from a genuine guest fault. The record names
   the per-instruction metadata a translator must provide.
3. **`Q-01-32` — Asynchronous interrupt injection.** Interrupts from the
   per-vCPU timer, from another vCPU, and from the host's device models are
   delivered to the guest at an instruction boundary, obeying x86 masking and
   priority rules, including to vCPUs that are mid-block, halted, inside the
   shim, or being broadcast to; a long string instruction is interruptible.
4. **`Q-01-33` — Host-produced code executes in the VM.** Code produced by the
   host becomes safely executable on every vCPU, block-to-block linking works
   while other vCPUs run, and a vCPU can obtain a translation it lacks and
   resume.
5. **`Q-01-34` — Secondary CPUs start without real mode.** With the target
   stock distribution kernels, secondary CPUs can be brought up in 64-bit mode
   with no real-mode code executed. If a target kernel cannot, the no-real-mode
   assumption falls and a trampoline interpreter reopens as a question. Its one
   consequence for the record is a minimum guest kernel version for the
   compatibility boundary (`Q-00-02`).

## How they are answered

One growing, disposable test program inside the VM.

- **Starting point** (under `Q-01-30`): a minimal EL1 payload with a way to see
  inside the VM (a trace ring, crash state, and timings written to memory the
  host can read), and the bring-up facts recorded per host: 4 KiB stage-1
  translation, own exception vectors, the virtual GIC, the per-vCPU timer, the
  `ACTLR_EL1` TSO switch set and read back, IPA granule and size limits,
  guest-EL2 availability, and a usable address-space layout.
- **Growth rule**: each later question's test is added on top of the previous
  questions' working result with every earlier mechanism left on. No test may
  disable a mechanism an earlier question established. By the last question
  the mechanisms are exercised together.
- **Required scenario**: code-page handling (writes to pages holding
  translations, including the same-page write-and-execute pattern) is exercised
  under `Q-01-31` and `Q-01-32` as a correctness scenario.
- **Status of the program**: a scratch pad. It is thrown away when the track
  closes. No architecture decision is made in it; expedient choices are recorded
  as such. What carries forward is knowledge in the investigation records.

Candidate mechanisms discussed while framing this track are collected in
[architecture candidates](../notes/architecture-candidates.md), which is
outside review and inadmissible as support. The test-program author may start
from any of it or none of it.

## Exit criteria

The track is complete when each of the five questions has an accepted
resolution or accepted kill on at least one available host with per-host
applicability recorded, the final test runs with all established mechanisms
on, the end-of-track breadth check is recorded, and all surfaced questions are
filed in the [question backlog](../question-backlog.md).

## Filed to other tracks

Cost verdicts (`Q-04-03` to `Q-04-10`), the translator comparison (`Q-03-02`),
in-shim versus host device handling (`Q-03-03`), the paging and code-page
mechanism choice (`Q-03-04`), the machine layout and clock contract
(`Q-02-03`), the QEMU baseline (`Q-05-09`), and the adaptation effort stop
rule (`Q-06-06`). Parked so they are not lost: 32-bit compatibility mode and
legacy floating-point precision policy (scope and correctness), host-side crash
containment of the translator and device models (`Q-02-04`), and host
preemption, sleep and thermal behavior (shared with every VM; performance).
