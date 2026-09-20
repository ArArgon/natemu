# Track 03 retrospective: architectural framing drift

Recorded September 20, 2026, at the owner's request. Author: `/root`.

## What went wrong

The assistant let an upstream implementation drive the research agenda instead
of consistently evaluating code-generation components inside the architecture
the owner had already fixed: **host translation plus a captive VM with an EL1
shim**. QEMU TCG, FEX and similar projects were codegen choices within that
architecture, not competing end-to-end solutions.

The main failure was systematic oversight of scope, comparison and decision
closure. It was not primarily a collection of incorrect source statements.
Locally sound observations accumulated under a misweighted selection rationale.
The absence of an accepted final architecture did not make that drift harmless:
it affected what was investigated, which alternatives received attention and
what the assistant presented as the leading direction.

This was the assistant's responsibility. The owner did not need to supply a new
high-level specification or repeat the architectural boundary to prevent it.

## The direction was already explicit

- [Track 00](../research/tracks/00-scope-and-success.md#initial-target) specifies the EL1
  shim, native paging, exceptions, timer/interrupt controller and TSO, with
  translation on the host.
- [Track 01](../research/tracks/01-feasibility.md#working-assumptions) records owner
  decisions of September 5/6: host translation, with an existing x86-to-arm64
  code generator chosen later in Track 03.
- [Track 03](../research/tracks/03-architecture-options.md#starting-point-and-boundaries)
  preserves that host-translator/EL1-shim starting point.

Track 03's question about the “translator and system-semantics implementation”
allows decisions about generators, helpers and interpretation **within** this
boundary. The assistant overextended it into a preference based on upstream
system-emulator coverage. No cited instruction requested that change in framing.
The owner's September 20 reminders reaffirmed the existing direction; they were
not a new architecture decision.

## How the oversight propagated

| Systematic failure | What happened | Why local checks did not prevent it |
| --- | --- | --- |
| Fixed constraints were not used as active decision criteria | The records retained the host/shim words, but the comparison increasingly favored QEMU's existing system machinery. | Mentioning the architecture was treated as sufficient alignment; the inference from source facts to selection was not checked against it consistently. |
| Implementation-first anchoring | An early QEMU audit developed into a QEMU-first adaptation direction. Its state, helpers and recovery conventions increasingly shaped proposed interfaces. | Concrete implementation details were available and productive to inspect, but availability was not evidence of best fit. |
| Asymmetric comparison | QEMU received detailed adaptation proposals while alternatives were more often assessed through their upstream limitations. | Greater knowledge of one candidate was allowed to resemble lower risk or lower cost. Comparable interfaces and burdens of proof were missing. |
| Local review substituted for decision review | Independent reviews checked source facts and conditional protocols. Their scoped acceptance accumulated around the candidate. | Those reviews did not establish the comparative preference. The parent investigation failed to keep that distinction operational. |
| Investigation ownership displaced charter ownership | Four investigation work areas accumulated findings across twelve charter questions. | The many-to-many structure was legitimate, but the combined record did not consistently show which decision each finding could close and which assumptions remained provisional. |
| Follow-up work became self-perpetuating | Each inspected helper or protocol exposed another detail and supplied the next bounded action. | Individual passes had stopping conditions, but the series was not sufficiently checked for whether it changed an architectural choice or merely deepened one adapter. |
| Progress reporting used the wrong unit | Reviewed-claim counts and repeated source progress obscured the unresolved selection rationale. | A source fact, a conditional adapter proof and an accepted architectural choice were counted or discussed too similarly. |

These are observable failures in the work and its presentation, not a claim to
have isolated every internal cause. The owner's corrections exposed a control
failure broader than any one sentence or candidate preference.

## Concrete errors and their limits

The assistant's explanation favored QEMU because it already implements system
semantics and therefore appeared to require less new semantic work. That was an
unsupported comparative inference. Each candidate's implementation is coupled
to an execution environment that the captive shim deliberately changes. Source
coverage alone establishes neither transferability nor adaptation cost.

Other affected inferences included allowing QEMU's filtered CPU model to shape
the proposed guest profile, giving Box64 an asymmetric burden to demonstrate a
compelling advantage, conflating an LLVM backend choice with writing an entire
frontend, and overstating a shared QEMU revision requirement across independently
selected components. Candidate-specific continuation and memory proofs also
could not be transferred to another generator or walker without rechecking their
premises.

The [framing audit](../research/investigations/INV-03-01.md#architecture-framing-audit--september-20)
covered 75 numbered claims: 61 retained their scoped use, six entries required
inference/scope corrections, and eight additional entries contained conditional
proof artifacts requiring re-establishment if their components change. No source
fact was demonstrated false **because of this drift**. This was not a fresh
verification of every source fact, and the count is not a measure of effort,
importance or percentage of the project that remains valid. The assistant's
initial emphasis on “61 retained” understated the centrality of the comparison
that needed rebuilding.

For traceability, the six inference/scope entries were `CLM-03-01-05/08`,
`CLM-03-02-01/06/07` and `CLM-03-03-26`. The eight additional proof artifacts
were the traces attached to `CLM-03-01-07` and `CLM-03-04-07/08/09/10/12/13/14`.
The latter group's lowering, recovery, checkpoint and capacity premises depend
on the generator; claims 13/14 separately depend on the selected walker. These
are dispositions of attached inferences/proofs as well as the numbered source
paragraphs, not a finding that fourteen source statements were false.

## Blast radius by Track 03 question

| Investigation | Charter questions affected | Required disposition |
| --- | --- | --- |
| [Q-03-02](../research/investigations/INV-03-02.md) — codegen | 2: generator, helpers and semantics | Rebuild comparative rankings against a common captive-runtime interface. Reuse valid source inventories; do not carry forward preference from upstream system coverage. |
| [Q-03-01](../research/investigations/INV-03-01.md) — combined architecture | 1: CPU profile; 3: execution/state split; 5: recovery | Rejustify CPU exposure independently of an upstream accelerator mask. Separate common contracts from adapter choices, then recheck their composition. |
| [Q-03-04](../research/investigations/INV-03-04.md) — memory and code lifetime | 4–7: memory, recovery, atomicity and code lifetime; interfaces to 8 and 12 | Re-establish affected lowering, checkpoint, capacity and walker proofs for the selected components. Preserve generic ownership, ordering and lifetime requirements. |
| [Q-03-03](../research/investigations/INV-03-03.md) — machine and devices | 8–12: events, clocks, boot, devices and containment | Correct dependency coupling; retain independently justified device research. Recheck interfaces when CPU/runtime choices change. |

The fixed high-level architecture and accepted Track 01 feasibility findings do
not require restarting. There was no Track 03 implementation or runtime
experiment to rebuild or rerun at this audit. That limits material rework, but
does not excuse the research effort spent under the wrong prioritization.

## Corrective practice and closure

The existing twelve-question architecture table should control progress; the
investigations remain places to do and retain the work. Each architectural choice
needs an explicit fixed requirement, open decision, common interface, applicable
candidate evidence and closure condition. No separate claim-count ledger or new
approval bureaucracy is needed.

For this correction:

1. Define the codegen–shim contract without assuming a candidate's state layout,
   host runtime or helper architecture. Select semantic/helper components
   separately from codegen.
2. Compare candidates against the same contract, including generated-code
   quality, required adaptations and residual costs. Unknowns stay unknown;
   uneven inspection does not establish a ranking.
3. Mark adapter-specific findings by their actual premises. Review a change of
   premises before crediting the proof to another component.
4. At each coherent decision review, ask both whether the evidence is correct
   and whether it justifies the claimed choice inside the fixed architecture.
5. Before chaining another source pass, identify the charter decision it can
   change and why the remaining uncertainty is architectural rather than later
   implementation conformance. Report decision closure separately from source
   findings.

The records have begun correcting the framing, and the blast radius has been
audited. **The mistake is not resolved merely by this retrospective or by editing
the preference language.** Closure requires the rebuilt comparison and the
affected decisions/proofs to receive appropriate independent review. Those
substantive outcomes remain owned by the investigations, not this retrospective.

### Inventory reconciliation after the correction

The [current twelve-question inventory](../research/investigations/INV-03-01.md#current-design-choices-and-evidence-ownership)
now separates fixed requirements, component choices, applicable evidence and
remaining closure work. Its linked investigations retain the original findings
and superseded pass history. The matched comparison and selected component
policy in INV-03-02 replace the unsupported system-coverage preference; this is
not a measured performance or effort ranking. Reviewed conditional integration
in INV-03-01 claim18 does not establish whole-architecture acceptance.

This addresses the inventory/navigation failure. Questions2/5 and completion
breadth remain with their current owners; they are not hidden by the historical
61/six/eight counts. Future progress must be reported against those decisions,
not the volume of accepted source findings. This retrospective records the
mistake and corrective practice, while the investigations own technical closure.

## Follow-up: a known launch premise was read but not enforced

The first Track 03 containment run, `containment-reviewed-01`, stopped in its
unrestricted control: `hv_vcpu_run` did not return before the watchdog. No
restricted worker ran. Its runner installed a finite `RLIMIT_CPU`, despite the
current [Track 01 reproduction requirements](../../experiments/track01/README.md)
and [accepted launch diagnosis](../research/investigations/INV-01-30.md#cpu-limit-result-and-full-foundation-restoration-submission)
requiring inherited unlimited CPU limits for that recorded harness. The
launch-premise mismatch was explicit in the sources and runner. A separately
reviewed control subsequently completed with the setter omitted and unchanged
guest/profile/entitlements; this supports the narrow launch diagnosis without
establishing an OS-wide CPU-limit rule.

The systematic failure repeated the earlier pattern: reading and citing a
constraint did not make it an enforced input to preparation or independent
review. The author added a seemingly conservative resource limit; the root
reviewer had read the reproduction requirements and still missed the conflict.
This is a preparation/review error, not missing user specification or evidence
against containment. The correction is to carry applicable inherited launch
conditions into the actual runner and review them before execution, retaining
wall-time/process-group bounds. The failed run stays preserved; the reviewed
single-variable control preceded the resumed matrix. The independent findings
thread records the exact diagnostic scope.


## Follow-up: static preparation also needs an immutable evidence boundary

The FEX schema-only preparation at
`/private/tmp/natemu-track03-checkpoint-static-20260920T093906Z` included a stale
self-hash for its manifest. Independent review and root verification caught the
mismatch. The author then reported overwriting that temporary manifest while
correcting the producer, before preserving the failed version. The original
failed manifest bytes are therefore not claimed retained or reconstructed.
The separately named fresh check at
`/private/tmp/natemu-track03-checkpoint-static-20260920T094625Z` records explicit
inputs and non-self-referential hashes; root verified all 16 listed files against
the current patch. This does not retroactively validate the old record.

The systematic oversight was treating a small static preparation check as exempt
from the retained-run discipline used for VM experiments. Source/schema checks
can affect architectural decisions too. Apply the same input identity, failure
preservation and non-overwrite boundary when recording them, using existing
retention mechanisms where practical. A static pass still establishes only its
actual scope, never compiler conformance or runtime recovery. This is an agent
execution/review-control failure, not a missing user specification or evidence
against FEX's architectural suitability.
