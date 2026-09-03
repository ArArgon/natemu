# Review ledger

This ledger records every review. It starts empty. A missing applicable entry
means the corresponding gate has not passed; it may never be treated as
silently satisfied.

## Review types

Use one of: `question`, `source`, `method`, `code`, `result`,
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
section, and the completion rule for an applicable gate in
[gate completion](evidence-policy.md#gate-completion). Independence — who
qualifies as an independent reviewer, and where self-review does not
substitute for independent review — is defined in the
[executor guide](executor-guide.md#independence).

This ledger adds the recording mechanics. A missing applicable entry means the
corresponding gate has not passed. `not-applicable` is a verdict only with an
explicit reason; it cannot stand in for an omitted review. A `not-applicable`
gate is recorded directly in this ledger with its reason; no critique document
is produced for it.
