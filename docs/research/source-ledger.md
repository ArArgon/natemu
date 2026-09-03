# Source ledger

This ledger maps every inspected source to the claims it supports or
contradicts. It starts empty.

## Admissibility

Per the evidence policy, a Source-backed claim is admissible only when it is
directly supported by a primary specification, official documentation,
versioned upstream source, or peer-reviewed paper, with version, scope, and
applicability recorded at claim level. Secondary sources may help locate
evidence, but cannot carry a load-bearing claim. No inherited source is
accepted: a source must be inspected and recorded during the work that
relies on it.

## Claim-level mapping template

Source identifiers are `SRC-<nnn>` and claim identifiers
`CLM-<track>-<nn>-<nn>`, per the [identifier table](README.md#identifiers).
A source keeps one identifier across every claim it maps to.

Add one row for each source-to-claim mapping after inspecting the source. A
source that supports multiple claims requires separate rows, one for each
claim. The placeholder row below is a
non-entry example that describes the schema; it records no source or claim.

| Source ID | Classification (primary/secondary) | Title | Publisher/owner | Stable URL or repo commit | Claim ID | Exact source section | Relationship (supports/contradicts) | Source revision/date | Applicability | Review ID | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `<SRC-ID>` | `<primary or secondary>` | `<title>` | `<owner>` | `<stable URL or commit>` | `<claim ID>` | `<exact section>` | `<supports or contradicts>` | `<revision/date>` | `<applicability boundaries>` | `<review ID>` | `<accept / revise / reject>` |

The `verdict` is a review outcome, not a claim of truth. Claim-level citations,
the required source gate, and an explicit relationship for both support and
contradiction remain mandatory.
