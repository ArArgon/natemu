# Investigation template

Copy this file for a separately authorized investigation. Every field below is
intentional and blank by design; this template records no research, sources,
questions, methods, or results.

## Metadata

- Investigation ID:
- Owner/session:
- Date range:
- Status: `proposed | gated | investigating | review | accepted | rejected | blocked`
- Dominant investigation method/context (does not replace per-claim evidence labels):
- Authorization reference:

## Question and decision relevance

- Question ID and text:
- Decision(s) affected:
- Why this must be resolved now:

## Assumptions

- Declared assumptions:
- Assumptions to challenge:

## Auditable breadth pass and backlog updates

- Breadth-pass record/artifact:
- Product, architecture, correctness, methodology, boundary, alternative, and
  harness-failure coverage:
- Backlog entries created for newly surfaced questions, or exact statement:
  `no new issues found`
- Discoveries during source, method, execution, or result work, recorded
  separately with stage and backlog ID, or exact statement: `no new issues found`

## Source inventory

| Source ID | Classification | Version/date | Exact sections | Claim use | Applicability limits |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## Claim inventory

Use one row per claim. The evidence label belongs to each individual claim;
the investigation's dominant method/context must never substitute for it.

| Claim ID | Exact claim | Evidence label | Source/artifact reference | Version/revision | Applicability | Derivation (if inferred) | Review status |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |

## Hypothesis and falsifier

- Hypothesis:
- Falsifier(s):
- Predicted outcomes:

## Applicability matrix

| Host generation | OS/SDK | Guest | CPU profile | vCPUs/core placement | Workload | In scope? | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |

## Method

- Procedure and versions:
- Inputs and configuration:
- Evidence labels assigned to planned claims:

## Controls

- Positive controls:
- Negative controls:
- Baselines:

## Confounders

- Known confounders:
- Mitigations and residual risk:

## Metrics

| Metric | Definition | Collection method | Units | Decision threshold |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

## Sample plan

- Units, repetitions, randomization/order, warm-up, and power/precision target:

## Exclusion and stop criteria

- Predeclared exclusions:
- Stop/failure conditions:

## Harness validation

- Harness version and validation artifact:
- Negative and fault-injection checks:
- Expected failure behavior and observed validation outcome:

## External immutable raw artifact

- Immutable raw artifact location:
- Manifest location:
- Manifest contents: producing harness/version, method, environment, inputs,
  integrity information:

## Results

- Result artifact links:
- Summary with evidence labels:
- Analysis/statistics:

## Correctness status

- Correctness criteria and outcome:
- Open correctness risks:

## Alternative explanations

- Alternatives considered:
- Evidence distinguishing them:

## Newly surfaced questions

- Backlog IDs created, or exact statement: `no new issues found`

## Limitations

- Scope, applicability, and uncertainty limits:

## Applicable gates and review links

Record every row. `not-applicable` requires an explicit reason. Preserve
evidence-type applicability: question applies to every question; source to
Source-backed claims; method, harness, and result to experiments; replication
to load-bearing Observed findings; adversarial to every load-bearing candidate
finding; synthesis to the final decision packet only.

An applicable gate passes only when all required reviews have verdict `accept`,
required independence is met, and all required changes are closed. Otherwise
its status is not passed. A synthesis review is not an individual-finding
prerequisite.

| Gate | Applicable? | NA reason | Review ID(s) | Independence required/met | Verdict | Unresolved changes | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| question |  |  |  |  |  |  |  |
| source |  |  |  |  |  |  |  |
| method |  |  |  |  |  |  |  |
| harness |  |  |  |  |  |  |  |
| result |  |  |  |  |  |  |  |
| replication |  |  |  |  |  |  |  |
| adversarial |  |  |  |  |  |  |  |
| synthesis |  |  |  |  |  |  |  |
