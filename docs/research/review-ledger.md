# Review ledger

This ledger is empty until separately authorized research produces work for
review. A missing applicable entry means the corresponding gate has not
passed; it may never be treated as silently satisfied.

## Review types

Use one of: `question`, `source`, `method`, `harness`, `result`,
`replication`, `adversarial`, or `synthesis`.

## Reusable entry template

The placeholder row is a non-entry example of the schema. It records no
review, finding, verdict, or gate completion.

| Review ID | Type | Subject | Reviewer/session | Independence | Evidence/artifacts inspected | Findings | Required changes | Verdict | Date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `<REV-ID>` | `<review type>` | `<question, claim, method, artifact, or packet ID>` | `<reviewer and session>` | `<self or independent>` | `<links, manifests, and versions>` | `<findings>` | `<required changes or none>` | `<accept / revise / reject / not-applicable (reason)>` | `<YYYY-MM-DD>` |

## Gate completion, applicability, and independence

Apply the question gate to every question; source to Source-backed claims;
method, harness, and result to experiments; replication to load-bearing
Observed findings; adversarial to every load-bearing candidate finding; and
synthesis to the final decision packet. If a gate is `not-applicable`, the
entry must state why. A self-review is required for every applicable gate, but
does not replace an independent review where policy requires one. An
independent reviewer is a different author working in a separately recorded
review context, able to challenge both evidence and method.

For every investigation, all eight gate types must be explicitly recorded as
applicable or `not-applicable` with a reason; a missing applicable gate is not
passed. An applicable gate passes only when all required reviews have verdict
`accept`, all required independent review is met, and every required change is
closed. A review may not record `accept` when its required changes remain open
or its required independence is unmet. `not-applicable` is a verdict only with
an explicit reason; it cannot stand in for an omitted review.
