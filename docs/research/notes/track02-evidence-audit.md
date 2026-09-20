# Track 02 evidence overlap audit — candidate

Independent audit by `/root/evidence_audit`, 2026-09-09. This note is a candidate
assessment, not an accepted finding or a replacement for an investigation.
Read-only inspection of current documents and frozen producer/oracle sources;
no VM execution, external-source revalidation, or archived git-history search.
“Source-backed” below distinguishes source contracts already recorded in accepted
investigations from their observed fixture results. Those investigations retain
their original scope and references.

## Verdict

**Inferred:** none of Q-02-01–04 has a complete answer in the current repository.
All four are important, but “unstarted” hides substantial applicable inputs from
Track 01. Preserve those inputs and investigate their explicit residuals instead
of rerunning mechanism feasibility. The governing workflow disallows treating
pre-preservation material as evidence; old accepted rows referencing absent
INV-00 records are not substitute current evidence
([workflow](../README.md), [backlog](../question-backlog.md)).

| Question | Existing answer/input | Important residual and recommended treatment |
| --- | --- | --- |
| Q-02-01: correctness surface and coverage | Track 02 already enumerates nine broad areas and five method families. Track 01 independently accepts finite composed paging/fault/interrupt/publication/startup cases. | Keep as the coverage inventory and measurement-eligibility owner. It still lacks a complete declared CPU/machine surface, feature-by-feature coverage, actual translator validation and stock SMP userspace boot. Do not reopen whether basic native faults or interrupts can be composed. |
| Q-02-02: SMP | Q30 establishes finite table changes, remote invalidation, aliases, A/D effects and selected atomic mutations. Q32 establishes finite interrupt cases; Q33 live target replacement and publication. TSO configuration/readback is established. | Keep ordering/litmus and stress for the actual translation sequences and chosen mechanisms. TSO readback is not litmus evidence; serialized fixture operations do not cover arbitrary atomics. Tag reuse, reclamation and alternative shared staging remain outside the accepted fixture. |
| Q-02-03: CPU/machine/clock contract | Q34 records ACPI mailbox layout, upstream 5.19 feature floor, audited vendor startup paths, mandatory Ubuntu startup32 handling, and a separate direct-BSP-boot source contract. | Keep and resolve before a full coverage claim. No complete CPUID/MSR/device/clock model or stock-image boot exists. Treat “proven minimal x86 layout” and fixed clock wording as proposals requiring evidence, not existing findings. |
| Q-02-04: containment | Track 01 has bounds, watchdogs, malformed response refusal and finite compiler-failure cases. | Keep: experimental resource supervision does not establish guest/shim isolation or containment of host translator/device failures. Define retained properties and exercise the actual selected boundaries. |

The question text and status are in the [backlog](../question-backlog.md#correctness);
the surface/method inventory is in the [Track 02 charter](../tracks/02-correctness.md).

## Exact limits of reusable evidence

- **Observed, scoped:** Q30's accepted profile is four-level paging, 36-bit
  physical addresses, WB RAM, 4 KiB/2 MiB pages, selected scalar/CAS forms and
  two/four vCPUs on the recorded M4. It excludes PCID, 1 GiB pages and optional
  permission features. Its global writer lock/eager eviction mechanism cannot
  establish correctness of a future mechanism that removes those constraints
  ([Q30 completion breadth](../investigations/INV-01-30.md#q30-completion-breadth-submitted-to-findings-review)).
  Local code confirms aligned-CAS enforcement in `q30_access` and serialized
  fault mutation, while the independent walk oracle explicitly returns
  `unsupported_attributes` for non-WB selectors
  ([q30.c](../../../experiments/track01/source/q30.c),
  [x86_walk.py](../../../experiments/track01/oracle/x86_walk.py)).
- **Observed, scoped:** Q31 uses handwritten arm64 sequences and does not implement
  floating point. It explicitly carries forward vector partial progress, MMIO
  side effects, broader nested delivery and feature combinations. Scalar RAM
  fault recovery therefore does not close these cases
  ([Q31 assumptions and breadth](../investigations/INV-01-31.md#assumptions-and-breadth)).
- **Observed, scoped:** Q32 uses fixed maskable APIC interrupts and normal 64-bit
  IDT delivery. NMI, SMI, INIT, ExtINT, FRED and all APIC modes are not established;
  retained unknowns include device coalescing and timer overruns
  ([Q32 assumptions and breadth](../investigations/INV-01-32.md#assumptions-and-breadth)).
  This is a coverage limit, not a recommendation to implement every excluded
  feature: Q02-03 must determine which are exposed or required.
- **Observed, scoped:** Q33 establishes owner-stopped register transfer, finite
  host-produced ADD/MOV blocks, immutable live-link replacement and stale-response
  rejection. It explicitly does not establish executable-memory reclamation/reuse,
  W^X transitions, arbitrary generated code or shared staging
  ([accepted Q33 resolution](../investigations/INV-01-33.md#accepted-observed-resolution--independent-replicas-and-findings-complete)).
- **Source-backed, scoped:** Q33 records that inspected Apple allocation/mapping
  declarations do not themselves establish effective cross-domain memory
  attributes or atomic publication guarantees. This does not prove incoherence;
  concurrent shared host/guest staging remains an unresolved alternative
  ([Q33 claims 08–09](../investigations/INV-01-33.md#hostguest-transfer-contract-preparation--source-findings-accepted-2026-09-07)).
- **Source-backed plus observed finite startup:** Q34 separates mailbox AP startup
  from BSP loading and stock boot, and boot-once from hotplug/kexec. The frozen
  `head64` explicitly assumes a successful `verify_cpu` return; it does not execute
  general CPUID/vendor sanitization. Consequently, neither the included Linux
  excerpts nor the successful composite oracle proves the full CPU contract
  ([Q34 claims 01–09](../investigations/INV-01-34.md#supporting-contracts-and-claims),
  [q34.c, `head64`](../../../experiments/track01/source/q34.c)).
- **Source-backed experiment description:** the initial layout uses RWX stage-2
  RAM for controlled inputs and explicitly claims no isolation validation.
  Later bounded compiler failure/refusal cases do not replace containment tests
  ([Q30 method](../investigations/INV-01-30.md#method-and-controls),
  [Q33 accepted resolution](../investigations/INV-01-33.md#accepted-observed-resolution--independent-replicas-and-findings-complete)).

## Minimal corrective framing

**Inferred recommendation:** retain the four identifiers. Seed Q02-01 with a
coverage table that separates source contract, accepted finite observation,
unsupported feature, and untested production behavior. Make Q02-03 its input
contract, Q02-02 its SMP expansion, and Q02-04 its containment expansion. Link
the specific existing claims above rather than marking all work answered or
repeating Track 01. Remove Q02-03's self-dependency and refresh its retired
dependency references when migrating the row. No new experimental execution is
needed to make this bookkeeping accurate.

## Reassessment: Track 02 precedes candidate selection

Following the owner's dependency challenge, `/root` and independent reviewer
`/root/coverage_review` revisited the assessment on 2026-09-09. **Inferred:**
“not fully answered” does not mean “must finish before Track 03.” All nine
surface areas have specification and coverage-definition work available now;
none can receive full empirical conformance acceptance independently of a
concrete candidate. A candidate need not be the final selected implementation.

All four backlog questions mix early requirements with later validation:

- Q02-01 can define coverage and oracle requirements now; translator conformance
  and stock SMP boot require candidate execution later.
- Q02-02 can define ordering and coherence invariants and inherit Track 01's
  scoped findings now; broader validation depends on actual access sequences,
  paging, publication and synchronization mechanisms.
- Q02-03 can derive guest requirements and feature dependencies now; the exact
  exposed CPU/machine/clock profile must be reconciled with candidate capability,
  then validated. Its proposed layout is not an architecture-independent fact.
- Q02-04 can specify retained containment properties now; fault injection needs
  concrete process, memory and device boundaries later.

The proposed FP/state, device-memory interaction and reclamation additions are
requirements to make explicit now, with implementation checks downstream. The
containment omission is an immediate charter/backlog alignment issue. These
are not four additional immediate experimental campaigns.

Strict track-by-track completion would also conflict with Q03-03/04 requesting
Track 04 measurements while Track 04's charter assumes the Track 03
recommendation. Candidate evaluation must permit bounded iteration before the
final recommendation. Track 02 should supply correctness obligations, applicable
existing evidence and explicit unresolved risks to that iteration; subsequent
performance claims still need correctness evidence for the actual mechanism
and configuration measured. Full emulator conformance remains a later obligation,
not an implied authorization to implement it during this research audit.

Sources: [Track 02](../tracks/02-correctness.md),
[Track 03](../tracks/03-architecture-options.md),
[Track 04](../tracks/04-performance-estimation.md),
[Track 05](../tracks/05-benchmarking-and-validation.md),
[question dependencies](../question-backlog.md), and
[research/implementation boundary](../README.md).
