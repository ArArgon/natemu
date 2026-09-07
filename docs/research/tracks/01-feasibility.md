# Feasibility

This track establishes whether the shim design can be built at all on the
supported hosts. It is a roadmap, not a finding. It does not establish how fast
the design is, which variant to build, or whether the guest behaves correctly;
those belong to [Performance estimation](04-performance-estimation.md),
[Architecture options](03-architecture-options.md), and
[Correctness](02-correctness.md). Work on it follows the
[research workflow](../README.md).

**Track 01 is complete for its recorded M4 scope as of September 7, 2026.**
All five questions have independently accepted scoped resolutions at two and
four vCPUs: paging, precise faults, interrupts, host-produced code and
secondary-CPU startup without real mode. The separate final four-vCPU run
passed the complete composite oracle and independent findings review. Completion
breadth is addressed, downstream questions are filed, and the working scratch
program is retired from development, with a frozen reproduction snapshot now
retained in the repository at the owner's request.

The host is Apple M4 Mac16,10, macOS 26.6.2 (25G83), SDK 26.4, with uncontrolled
core placement. These finite mechanism findings do not establish stock-kernel
boot, complete ISA correctness, performance or a production architecture.
Unavailable M2 replication remains with Q-05-02. Next work belongs to the named
downstream investigations; planning and implementation require their separate
authorization under the research workflow.

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
  started by the mechanism accepted under `Q-01-34`. Real mode remains out of
  scope; the audited Ubuntu startup path still requires a small 32-bit
  compatibility/protected-mode transition.
- The ISA ceiling is AVX2. Floating point behaves as the eventual code
  generator provides; nothing floating-point is hand-written.
- August 2026 code and results (commit `a121702`) are not reused.
- The host of record is the M4 under macOS 26. An M2 Pro/Max under macOS 26 is
  the second generation, available later; every question is replicated there
  when it is. A "no" on every available generation is a kill; a "no" on one
  narrows applicability (`Q-00-11`).
- The x86 memory model comes from the hardware TSO switch, and the design's
  correctness rests on it. The switch is a documented platform control whose
  stated use, Rosetta inside Linux VMs toggling it per guest thread, requires
  the hypervisor to preserve it across host scheduling; it is therefore not a
  question of this track. It is set in the starting point and stays on for
  every later question, so every multi-vCPU test in the track runs under it.
  Ordering evidence proper (litmus, stress) is filed under
  [Correctness](02-correctness.md) and runs on the same foundation.
- The virtual interrupt controller and per-vCPU timer are shared with every
  VM on macOS 15 and later. Whether a timer expiry exits to the host is
  recorded as a bring-up fact for the cost model, not asked as a question.
- Guest kernels that predate the 64-bit multiprocessor wakeup are out of
  scope. `Q-01-34` cites the upstream version where it landed as the minimum
  guest kernel; vendor backports are not audited. 16-bit support may be added
  later as a recorded scope widening.

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
- **Status of the program**: a research scratch pad, retired from development
  at closure. The owner subsequently requested repository preservation for
  reproduction; retain the accepted final source and checker snapshot under
  `experiments/track01`. No architecture decision is made in it; expedient
  choices are recorded as such. Findings remain in the investigation records.
  Timings the program records are diagnostic; they are not qualified and are
  not cited outside this track.

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

## Completion breadth — September 7

The completion check is **addressed**. Q30–Q34 findings and independent
replication are accepted, and the final integrated four-vCPU result received
independent acceptance after its full checker passed at 22:13:56 UTC. The
[Q34 findings thread](../reviews/INV-01-34-findings.md) records the final
correctness/methodology and combined-scope verdict. No substantive return remains.
`/root/retained_runner` reviewed product/architecture implications independently
of root's completion synthesis, but authored the Q34 integration and therefore
does not supply its independent correctness acceptance. Root inspected the
current charter, scope and backlog; neither context searched archived history.

The resumed product/architecture breadth review by `/root/q34_oracle` found
no additional Track 01 question or unowned implication. That context authored
the Q34 oracle and therefore reviews only the product/architecture scope here;
it does not supply independent correctness or method acceptance of its own
checker. It inspected the current scope, charter checklist, backlog and
accepted CLM-01-34-11, and confirmed the distinctions between source-indexed
startup and stock-image boot, mailbox feature floor and distribution support,
x86 AP startup and native vCPU creation, no-real-mode and startup32 handling,
resource guards and performance, and M4 evidence and later M2 coverage.
Independent reviewer `/root/q34_review` accepted combined correctness/
methodology breadth and the actual replica/final integration findings.

No sixth Track 01 question emerged. The five mechanism questions retain their
specific falsifiers and per-host scope. A fixture coverage failure requires
resolution or withdrawal of the affected observation; it is not automatically
an architecture kill. A scoped success establishes mechanism viability, not
stock boot, a production translator, speedup, reliability rate or a design
selection. The independent final review found no new contradiction, missing
mechanism or unowned implication; the prior negative outcomes remain retained.

The existing backlog owns the carryforward work: stock compatibility and
direct-boot/device/CPU contracts (`Q-00-02`, `Q-02-01/03`); translator recovery
metadata, startup32 handling, floating point, adaptation effort and cache
reclamation (`Q-03-02/04`, `Q-06-06`); SMP ordering and containment (`Q-02-02/04`);
qualified costs, clock and placement (`Q-04-*`, `Q-05-*`); and later M2 coverage
(`Q-05-02`). Linux 5.19 is the audited mailbox-feature floor, not an unconditional
distribution-compatibility promise. UEFI/Windows analysis remains `Q-00-09`.

Three backlog wording corrections align those owners with the current scope:
no second macOS-release requirement, fresh qualified Track 04 measurements
instead of reuse of Track 01 diagnostic timings, and actual chosen wait/exit
behavior instead of a WFI-to-host assumption inferred from a WFE fixture.
No new evidence or downstream resolution is created by those edits. Planning
and implementation remain separate later decisions under the research workflow.

## Completion evidence checklist

All exit criteria below are satisfied within the recorded host and fixture scope.
Each investigation and its findings review remain authoritative for its claims.

| Requirement | Current evidence and disposition |
| --- | --- |
| Q30 paging, including SMP | Accepted scoped M4 2/4-vCPU finding and independent replication in INV-01-30. |
| Q31 precise faults and restart | Accepted scoped M4 2/4-vCPU finding and independent replication in INV-01-31. |
| Q32 interrupt boundaries, masking and priority | Accepted scoped finding; later after-WFE return reclosed by reviewed corrected primaries and refreshed 2/4-vCPU replicas, recorded in INV-01-32 and INV-01-33. |
| Q33 publication, live links and miss continuation | Accepted scoped M4 2/4-vCPU finding and independently rebuilt replicas in INV-01-33. Shared staging remains an unestablished alternative, not a prerequisite to the accepted register-transfer path. |
| Q34 startup without real mode | Accepted scoped source and observed M4 2/4-vCPU findings after complete primary and independently rebuilt replica checks in INV-01-34. |
| Final program with all established mechanisms active | Accepted `q34-startup-final-01` / `q34-startup-final-01-check-01`: all five nested mechanism checks PASS, 404404 operations / 5489617 records, unsupported[]. One separately budgeted VM/check pair completed without retry. Exact evidence is in INV-01-34. |
| Product, architecture, correctness and methodology breadth | All four perspectives addressed above and accepted in the final independent findings review; no new question or unresolved substantive return. |
| Every surfaced question filed | Final review confirms the existing owners above; the backlog explicitly records the current carryforward implications. No sixth feasibility question emerged. |
| Disposable scratch program retired | Complete after final acceptance; original working directories were archived and removed. At the owner's subsequent request, unchanged producer/runtime-oracle code and the adapted supervisor are now preserved in a frozen repository reproduction snapshot. Historical raw evidence remains external. |


## Retained evidence and scratch retirement

Raw evidence remains outside Git under `/Users/liaozping/orca/natemu-raw`.
Each historical run retains its own producing sources, build inputs, manifest,
outputs and checksums, including failed and partial outcomes. Final producer
`q34-startup-build-05/source`, independent `q34-startup-oracle-01` and bounded
`q34-startup-supervisor-01` remain available for reproduction; the final run,
checker and campaign inventories are recorded in [INV-01-34](../investigations/INV-01-34.md).
A future experiment needs its own reviewed budget and fresh output identity.

After final independent acceptance, root removed only `experiments/track01`
and `experiments/track01-replication`. Immediately before removal, every current
file and file-set was compared with the sealed
[retirement archive](/Users/liaozping/orca/natemu-raw/track01-scratch-retirement-01).
All 36 working files are recoverable; the 37-entry archive inventory is
`e0451acf8128000784cfcc474e9e9ec6ceab2c337a883ad1d4c1a7fbb8c96247`.
The independent reviewer also verified the archive against the working files.
This archive preserves the final working copies; it does not change any
historical run's producing identity. No scratch implementation carries forward
as a prototype or architecture decision.


### Repository reproduction copy — owner-requested preservation

Following closure, the owner requested keeping the program in the repository
for reproduction on another machine. This supersedes the earlier source-deletion
requirement; the program remains retired from development. The
[frozen reproduction snapshot](../../../experiments/track01/README.md) contains
all final producer sources and the complete runtime oracle chain, byte-identical
to their retained accepted versions. The bounded supervisor is adapted to use
Git-versioned oracle sources without separate source checksum files, as the
owner requested. Per-run source snapshots and raw-result integrity checks remain.
Its README records provenance, host/SDK/resource requirements and commands.
Original evidence identities remain unchanged. Raw RAM, traces and compiled
outputs stay outside Git.


## Filed to other tracks

Cost verdicts (`Q-04-03` to `Q-04-10`), the translator comparison (`Q-03-02`),
in-shim versus host device handling (`Q-03-03`), the paging and code-page
mechanism choice (`Q-03-04`), the machine layout and clock contract
(`Q-02-03`), the QEMU baseline (`Q-05-09`), and the adaptation effort stop
rule (`Q-06-06`). Parked so they are not lost: 32-bit compatibility mode and
legacy floating-point precision policy (scope and correctness), host-side crash
containment of the translator and device models (`Q-02-04`), and host
preemption, sleep and thermal behavior (shared with every VM; performance).
