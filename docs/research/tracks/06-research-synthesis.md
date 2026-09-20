# Research synthesis

This track defines how research must assemble a final decision packet from
the preceding tracks. It is a roadmap, not a decision. Work on it follows the [research workflow](../README.md).

## Inputs

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

The synthesis gate selects exactly one outcome: stop, continue targeted
research, or authorize planning. No other outcome exists, and no outcome may
be implied without the gate. The packet carries Track 03 architectural decisions into planning. Planning
owns construction details and delivery and requires its separate authorization.
A planning decision may rest on reviewed cost projections and scoped experiments;
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
