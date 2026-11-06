# Research synthesis

This track defines how research must assemble a final decision packet from
the preceding tracks. It is a roadmap, not a decision. Work on it follows the [research workflow](../README.md).

## Inputs

Track03's prior [claim20 closure](../investigations/INV-03-01.md#clm-03-01-20--performance-led-component-choice-and-current-recomposition)
is withdrawn; the extension-versus-hybrid comparison ended with insufficiency.
The owner now directs build-first increments with FEX as the first adapter while
final codegen selection stays open. Retain scoped constructions, measurements and cost falsifiers; do
not consume historical closure as an accepted final component selection.

Synthesis remains pending. The ABI-first milestone will contribute its readable
contract, reviewed specification, scoped runtime/fragment results, unresolved
assumptions and cost sensitivities. Completing that milestone alone supplies
neither a final generator choice nor acceptance of the combined architecture.
The next synthesis task is to assess those inputs when available alongside the
remaining scope, performance and economic evidence, identifying the exact gap
that changes stop, further development/research or investment in delivery. No synthesis campaign or budget
is started by the workflow revision.

Only findings that have passed their applicable reviews may support synthesis
conclusions. Include unreviewed exploratory observations as explicitly unresolved
concerns where relevant, never as accepted findings; do not hide contradictory
observations merely because they are unreviewed. Open items raised during
synthesis become entries in the
[question backlog](../question-backlog.md). The inputs are the accepted
outputs of the preceding tracks: the scope, baselines, "materially better"
definition, and stop conditions from
[Scope and success](00-scope-and-success.md); the resolutions and recorded
kills from [Feasibility](01-feasibility.md); the architecture decisions,
correctness and interaction evidence, alternatives and falsifiers from
[Track 03](03-architecture-options.md); the ranges
from [Performance estimation](04-performance-estimation.md); and the results
from [Benchmarking and validation](05-benchmarking-and-validation.md).

## Required analysis

The packet must contain:

- **Criteria comparison.** Each surviving option — the set carried forward
  by Track 03, with applicable estimates and measurements — compared
  against the approved success criteria from Scope and success. The
  comparison presents accepted findings, each identified by its evidence
  label, and may not manufacture claims that passed no gate.
- **Rejected alternatives.** Every rejected option carried forward from the
  rejected-option record, with reasons and falsifiers intact.
- **Unknowns.** Every material unknown, applicability limit, and unresolved
  question, stated rather than smoothed over.
- **Dependency scan.** Which conclusions depend on which findings, so a
  reopened finding identifies every dependent conclusion.
- **Contradiction scan.** A deliberate search for findings that contradict
  each other or the recommendation; any contradiction reopens the affected
  findings per the [research workflow](../README.md).

## Independent critique

Before the synthesis gate, the packet requires adversarial critique by an
independent reviewer, per the independence requirements in the
[research workflow](../README.md). The critique and its resolution
remain part of the packet.

## Outcome

Under the October25 build-first direction, synthesis selects stop, continue
targeted development/research, or invest in product delivery. It judges the
project's direction and claims rather than gating each reversible implementation
increment. Plan construction and validation alongside those increments.
An investment decision may rest on reviewed cost projections and scoped experiments;
it must not present them as measured end-to-end superiority. Unresolved issues
that could invalidate the selected architecture require targeted research, not
relabeling as later validation. Record remaining implementation and qualification
obligations with their acceptance conditions and consequences. The eventual
product success thresholds remain unchanged.

## Exit criteria

The track is complete when the packet contains every required analysis, the
independent critique is resolved, the synthesis gate has recorded an
`accept` verdict and its single outcome, the end-of-track breadth pass is
recorded, and all surfaced questions are filed in the question backlog.
