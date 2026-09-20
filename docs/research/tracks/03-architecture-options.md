# Architecture and implementation direction

Track 03 decides what to build and establishes that its design choices preserve
the required guest behavior, individually and together. It owns the correctness
work formerly organized under [Track 02](02-correctness.md). This charter sets
requirements; it is not evidence that an architecture already satisfies them.
Work follows the [research workflow](../README.md).

## Starting point and boundaries

Start with the host-translator/EL1-shim direction and the accepted, scoped
[Track 01 findings](01-feasibility.md). Reuse their evidence only where the
chosen mechanism and conditions match. Changes to those mechanisms need their
own correctness assessment. The frozen reproduction program remains retired.

The host-codegen/captive-VM-shim architecture is fixed. QEMU TCG, FEX and other
generators are component choices within it, not end-to-end alternatives. Select
semantic/helper reuse separately; upstream system coverage does not by itself
establish suitability, correctness after adaptation or lower cost.

The target is unmodified modern x86-64 Linux, direct boot and SMP, with timer/APIC,
serial, block, network, entropy and shutdown support. Preserve the required
startup32 transition where applicable; real mode remains out of scope. AVX2 is
the ISA ceiling, not a requirement to expose every feature. Inherit floating-point
machinery from the chosen translator, record its actual behavior, and reconcile
any mismatch with the guest/workload requirements rather than silently changing
the compatibility boundary. Isolation and crash containment remain required.
Optional lifecycle features require an explicit scope decision. UEFI and Windows
implications are analyzed without implementing either.

Use Apple capabilities broadly, documented or undocumented, where they help the
design. Establish the behavior relied on and state the applicable host/OS limits;
an observation is not a platform-wide guarantee. M4 is the host of record. Raise
M2/M4 discrepancies when a key feature depends on them, without a general M2
parity prerequisite. Record licensing implications as warnings, not candidate
selection blockers. Assess adaptation effort, maintenance and cost against QEMU
TCG alongside correctness.

## How work proceeds

Propose a **design choice**, or a tightly coupled group, and identify the uncertainty
most likely to change it. Inspect existing evidence and source contracts, explore
with bounded probes if useful, revise, then collect reviewed evidence where needed
to accept the choice. State its assumptions about surrounding mechanisms and
check the relevant interactions; revisit acceptance when those assumptions change.
The **architecture** is the coherent combined result. Coupled choices may develop
together; neither a completed CPU specification nor completion of another track
is a prerequisite to discussing them.

Experiments belong here when they answer a design question: translator recovery,
SMP ordering, a publication or retirement protocol, an Apple capability, or a cost
that changes the choice. [Track 04](04-performance-estimation.md) supplies cost
analysis and [Track 05](05-benchmarking-and-validation.md) supplies measurement
methods and baselines concurrently. They do not impose separate experimental
phases. Use the workflow's distinction between exploration and decision evidence:
bounded exploratory runs need no prior independent review; experiments intended
to settle a choice need pre-execution review, findings review and independent
replication of load-bearing observations. Both retain their producing inputs,
outputs and failures. Exploratory results guide further work, not accepted
correctness, rejection or performance conclusions; promotion requires a fresh
run under a reviewed method. Source-only work goes directly to findings review.
Before running, record the question, budget and stopping condition in its owning
investigation as required by the workflow.

Choose verification methods for the claim: specification analysis, independent
oracles, fault injection, fuzzing, litmus tests or stress where relevant. State
allowed outcomes, including undefined or implementation-dependent behavior,
coverage limits and how oracle disagreements are resolved. Shared implementation
ancestry must not be the sole basis for validating the same behavior. Correctness
checks qualify the behavior measured; potentially affected performance evidence
is quarantined until impact is established, and affected measurements are rerun
after correction. Timing qualification remains separate from correctness.

## Design questions and required behaviors

These are sections of one architecture assessment, not twelve sequential gates
or twelve mandatory experiments. For each choice establish the applicable
behaviors below; an exclusion must be consistent with the supported guest and
workload scope. Read the governing specifications for instruction/device details.

| Design question | Behaviors the chosen mechanism must preserve |
| --- | --- |
| **1. What virtual CPU is exposed?** Feature profile, privileged state, modes and topology. | CPUID, MSRs, instructions and state dependencies agree and satisfy target kernels/workloads. Privilege checks, control-register operations, segmentation and kernel/userspace transitions behave correctly. Unsupported features have specified discovery and rejection behavior. CPU identities agree across topology, interrupt routing and firmware tables. |
| **2. Which translator and system-semantics implementation?** Generator, helpers and any interpretation. | Exposed integer, flags, floating-point and SIMD behavior survives optimization, block boundaries and helpers. Preserve rounding, exceptions, extended-state save/restore and required mode transitions. Feature enablement and saved-state support agree. Identify inherited behavior and the changes needed for system execution. |
| **3. How are execution and state split between host, shim and translated code?** Execution privilege, address layout, state ownership and communication. | Guest state remains authoritative and recoverable through calls, faults, exits and resumes. Guest addresses/control flow cannot expose private shim or host state. Helpers are reachable in the chosen execution environment. Requests, replies, cancellation and commit points preserve ownership and reject stale results. |
| **4. How are guest memory and shadow mappings managed?** Paging, permissions, address spaces, memory types and RAM/MMIO routing. | Preserve supported walks, permissions, A/D effects, address checks and page sizes. Changes become visible when x86 requires them. SMP invalidation, aliases, page-table retirement and address-space-tag reuse remain coherent. Access checks and completion remain consistent during remapping. |
| **5. How are precise exceptions and recovery implemented?** Recovery state, commit boundaries and delivery. | Correct exception selection/priority, guest RIP, error/address information and saved state. Internal shadow repairs remain distinct from guest faults. Preserve instruction-specific partial progress and permitted side effects, including device accesses. Handle faults during delivery; metadata must recover live flags and FP/vector state where applicable. |
| **6. How do memory operations preserve SMP atomicity and ordering?** Access sequences, atomic fallbacks, fences and synchronization. | Supported ordinary and locked operations retain their required atomicity and ordering, including applicable misaligned/split cases. Helpers compose with ordinary accesses and relevant observers; a lock taken only by atomic helpers is insufficient by itself. Guest TSO does not automatically establish host/device publication ordering or multi-instruction atomicity. |
| **7. How is translated code identified, published, invalidated and reclaimed?** Lookup, linking, mutation tracking and lifetimes. | Blocks match source bytes, mappings and relevant CPU state. Publication safely exposes complete code. Self/cross-modification and aliases take effect at required boundaries. Handle direct links, stale translation replies and active executions. Code and recovery metadata outlive all execution, return, fault and interrupt references. |
| **8. How are interrupts, vCPU waits and coordination implemented?** APIC behavior, injection, wakeups and rendezvous. | Preserve masking, interrupt shadows, priorities, pending/acknowledged state and applicable coalescing. Route timers, IPIs and devices correctly. Deliver at legal boundaries, including interruptible long operations. Avoid lost wakeups and circular waits during halt, shim execution, translation waits, faults and shootdowns. |
| **9. What clock and timer contract is exposed?** Sources, scaling, offsets and deadlines. | Reported frequency/capabilities match behavior. Cross-vCPU observations, supported clock writes, timer programming and cancellation agree. Define effects of host scheduling delays, pause and sleep. Preserve promised monotonicity/continuity without assuming real-time delivery. |
| **10. What machine boots and which lifecycle operations are supported?** Loader, memory map, firmware tables, BSP/AP startup and shutdown. | Kernel/initrd/parameters, registers, mappings and reserved memory meet the direct-boot contract. Firmware, devices and CPU topology agree. Support target stock-kernel startup paths, including required startup32, without silently adding real mode. Define reset/reboot and CPU lifecycle limits explicitly; boot and shutdown are required. |
| **11. Which device interfaces/backends, and where do they execute?** Serial, block, network, entropy and shutdown discovery/transport/placement. | Preserve specified registers/queues, negotiated features, buffer ownership, visibility, completion and interrupt ordering. Validate guest-controlled descriptors/addresses. Device writes participate in applicable page/code coherence. Handle errors, cancellation, reset and stale completions. Block completion/flush honors the advertised cache and durability contract. |
| **12. What contains failures and resource exhaustion?** Isolation boundaries, failure handling and teardown. | Guest activity cannot access unauthorized host/peer state. Contain translator/device failures to the declared boundary. Define allocation/cache exhaustion, malformed replies and partial-I/O outcomes. Outstanding work cannot touch retired resources. Contained VM termination is a valid failure outcome; transparent recovery is not automatically required. |

## Interactions to establish

Evaluate the actual combined design against these interactions. Individual
component results do not establish their composition. A concrete candidate may
expose additional interactions; record them with the affected decision.

- **CPU state, dispatch and privilege:** mode, control-state and feature changes
  reselect, guard or invalidate incompatible blocks, including direct links.
  Emulated guest privilege never inherits the shim's authority by accident.
- **Recovery and temporary state:** guest registers, deferred flags, FP/vector
  controls and partial effects remain recoverable across helpers, faults and
  interrupts; shim/host temporary state cannot contaminate them.
- **All memory writers and aliases:** guest CPUs, shim helpers, host backends and
  any host-kernel writes into RAM participate in the applicable page-table and
  code-coherence policy, even when they bypass guest CPU write traps.
- **Remapping and split accesses:** permission checks, access routing, atomicity,
  fault precedence and partial completion stay consistent through commit.
- **Host/guest ordering and MMIO:** publication, completion and notification
  ordering spans execution domains. Translation and retry paths preserve MMIO
  width and side effects without incorrect speculation, merging or replay.
- **Translation and source mutation:** results remain valid when published;
  source/mapping changes and cancellation cannot admit stale replies.
- **Retirement and outstanding references:** code, metadata, links, return PCs,
  mappings and tags survive all references, including pending host callbacks.
- **Events, waits and locks:** timer/device/IPI arrival cannot be lost between
  checking and sleeping. Coordination remains possible during translation,
  faults and halt without waiting on a vCPU blocked by the same protocol.
- **Boot, topology and time:** CPU identities, startup state, firmware tables,
  interrupt destinations and per-vCPU clock/timer behavior agree.
- **Failure and resource reuse:** reset, cancellation or teardown cannot deliver
  late translation replies, I/O completions or interrupts into a new lifetime.
  Preserve promised storage semantics through partial failures.

## Decision record and completion

Keep the current architecture in [INV-03-01](../investigations/INV-03-01.md).
Its [twelve-question inventory](../investigations/INV-03-01.md#current-design-choices-and-evidence-ownership)
is the main entry point for current decisions: fixed requirements, open choices,
applicable evidence and next closure work. The four investigation identifiers
are evidence work areas, not replacements for these twelve charter questions.
Their current summaries own active work; historical pass-local next actions
are not concurrent assignments. Scoped source acceptance is distinct from
acceptance of a design choice or the combined architecture.
For each choice record the mechanism/implementation direction, required behavior
and interactions, supporting evidence, material alternatives and rejection
reasons, cost/adaptation implications, and unresolved issues or falsifiers.
Keep detailed evidence in its owning investigation and link it rather than
duplicating status. Existing `Q-03-02/03/04` investigations may feed this record;
open another only when a concrete uncertainty needs separate work. The retired
`Q-02-*` identifiers remain references to obligations absorbed here.

A choice is accepted only when reviewed evidence establishes preservation of its
required behavior and relevant interactions. A correctness uncertainty capable
of invalidating it keeps that choice provisional; unaffected work continues.
Scope tradeoffs and material work budgets go to the owner when concrete evidence
requires a decision. Routine mechanism choices belong to this track.

Track 03 is complete when the coherent architecture and implementation direction
have independent acceptance across the questions and interactions above, material
alternatives and falsifiers are recorded, and the product/architecture/correctness/
methodology breadth check has no unresolved issue that could invalidate the
selection. Explicit applicability limits and later implementation-validation
obligations carry into synthesis. A rejected design can route to stop or further
research without inventing an accepted architecture.

The output goes to [synthesis](06-research-synthesis.md). It establishes design
choices, not correctness of unwritten code or measured end-to-end superiority.
Planning owns construction details and delivery; implementation still needs its
separate authorization. Editorial errors are fixed without blocking acceptance;
substantive correctness, evidence and reproduction defects remain blocking.
