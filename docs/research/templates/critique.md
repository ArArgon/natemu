# Critique template

Copy this file only for a separately authorized review. The intentional fields
below are unpopulated; this template does not perform research or establish a
verdict.

## Metadata

- Review ID:
- Review type: `question | source | method | harness | result | replication | adversarial | synthesis`
- Subject ID(s):
- Reviewer/session:
- Date:

## Reviewer independence

- Independence: `self | independent`
- Different author and separately recorded review context (if independent):
- Conflicts, prior involvement, and limits on independence:

## Artifacts inspected

| Artifact | Version/manifest | Exact sections or outputs inspected | Integrity/location |
| --- | --- | --- | --- |
|  |  |  |  |

## Challenge checklist

Record a disposition for every dimension. `not-applicable` requires a reason;
no applicable dimension may be omitted.

| Challenge dimension | Disposition (`issue` / `clear` / `not-applicable`) | NA reason | Evidence/artifact reference | Finding | Required action | Action status |
| --- | --- | --- | --- | --- | --- | --- |
| Scope and decision relevance |  |  |  |  |  |  |
| Source applicability and claim-level support |  |  |  |  |  |  |
| Method and controls |  |  |  |  |  |  |
| Harness validation, including negative/fault-injection behavior |  |  |  |  |  |  |
| Statistics, samples, exclusions, and stop criteria |  |  |  |  |  |  |
| Correctness criteria and failure modes |  |  |  |  |  |  |
| Multicore behavior, vCPU/core placement, and topology assumptions |  |  |  |  |  |  |
| Integration assumptions and product-envelope boundaries |  |  |  |  |  |  |
| Alternative explanations and falsifiers |  |  |  |  |  |  |
| Contradictions, unresolved evidence, and confidence |  |  |  |  |  |  |
| Synthesis dependency chain from claims to decision |  |  |  |  |  |  |
| Synthesis contradictions and phase-boundary review |  |  |  |  |  |  |

## Synthesis-specific checks

For synthesis reviews, complete the two synthesis rows in the challenge table.
The phase-boundary finding must verify that the research outcome is `stop`,
`continue targeted research`, or `authorize planning`; it does not authorize
implementation.

## Required changes

- Required changes, owners, and return gate:

## Verdict

- Verdict: `accept | revise | reject`
- Rationale:
- Applicability limitations:

`accept` is prohibited if any required action remains unresolved, any
applicable challenge dimension lacks a disposition, or required reviewer
independence is unmet. Use `revise` or `reject` instead, and return the work
to the applicable earlier gate.
