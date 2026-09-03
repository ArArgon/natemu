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

Gate applicability by evidence type is defined normatively in the evidence
policy's [promotion and challenge](evidence-policy.md#promotion-and-challenge)
section. The completion rule — an applicable gate passes only when all
required reviews have verdict `accept`, required independence is met, and
every required change is closed — is defined normatively in
[gate completion](evidence-policy.md#gate-completion).

This ledger adds the recording mechanics. A missing applicable entry means the
corresponding gate has not passed. `not-applicable` is a verdict only with an
explicit reason; it cannot stand in for an omitted review. A `not-applicable`
gate is recorded directly in this ledger with its reason; no critique document
is produced for it. A self-review is required for every applicable gate, but
does not replace an independent review where policy requires one. An
independent reviewer is a different author working in a separately recorded
review context, able to challenge both evidence and method.
