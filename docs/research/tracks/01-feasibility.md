# Feasibility

This track is a roadmap only. It lists what separately authorized research
must establish about the host platform before any architecture is proposed;
it records no findings, and no claim about how the platform actually behaves
belongs here. Work on this track runs only under separate research
authorization and follows the [research governance](../README.md), the
[evidence policy](../evidence-policy.md), and the
[executor guide](../executor-guide.md).

## Questions the track must answer

Each area below becomes one or more entries in the
[question backlog](../question-backlog.md), resolved with labelled evidence
through the applicable gates (see
[gate completion](../evidence-policy.md#gate-completion)):

- **Platform facts.** What the public macOS APIs actually permit an emulator
  process to do, across at least two Apple Silicon generations and at least
  two supported macOS releases, established from primary sources and
  controlled observation — never from folklore or the preservation
  checkpoint.
- **Architecture families.** Which broad emulator architectures (for example
  interpretation, translation, and hardware-assisted approaches) are even
  admissible under those platform facts, before any is preferred.
- **Address spaces.** Whether and how guest physical and virtual address
  spaces can be represented within a host process, and at what limits.
- **Translation and executable memory.** Whether and how generated code can
  be made executable under any code-signing and write/execute rules the
  platform imposes, and what that costs.
- **Paging and invalidation.** Whether and how guest page-table semantics and
  TLB-style invalidation can be honored, and what mechanisms exist for tracking
  modification of guest memory, including self-modifying code.
- **Exceptions.** Whether guest faults can be delivered precisely and
  restartably through available host mechanisms.
- **Timers and interrupts.** What timekeeping and asynchronous-event
  facilities exist, at what resolution and latency, and whether and how
  guest timer and interrupt delivery can be built on them.
- **Multicore coordination.** Whether and how multiple vCPUs can execute,
  synchronize, and deliver cross-CPU events on the host, and whether and
  where the host memory model diverges from the guest's.
- **Devices.** Whether the minimal headless device set defined by
  [Scope and success](00-scope-and-success.md) can be backed by public APIs.
- **Toolchain.** Whether the required build, test, debug, and profiling
  toolchain exists and is sustainable on the supported hosts.
- **Licensing.** Whether every contemplated dependency and source of study
  is license-compatible with the project's intended distribution.

## Kill assumptions

The track must maintain an explicit list of assumptions whose failure kills
the project or an architecture family, each phrased falsifiably, each tested
early, and each mapped to a stop condition from Scope and success.

## Required spike

Feasibility cannot be closed on paper. The track requires at least one
integrated spike that combines translation, paging, exceptions, and
interrupts with at least two vCPUs running concurrently, executed under an
approved method and harness and independently reviewed. A spike demonstrates
mechanism viability only; it is not a performance result and not a design.

## Exit criteria

The track is complete when every area above has an accepted resolution or a
recorded kill, the spike has passed its applicable gates including
independent review, the end-of-track breadth pass is recorded, and all
surfaced questions are filed in the question backlog. A recorded kill is an
accepted finding that a kill assumption fails: a family-level kill removes
that architecture family from consideration, while a project-level kill
routes to a stop condition from Scope and success and into synthesis — it
does not count as track completion.
