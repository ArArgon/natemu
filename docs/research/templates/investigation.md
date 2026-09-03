# Investigation template

Copy this file for a separately authorized investigation. Every field below is
intentional and blank by design; this template records no research, sources,
questions, methods, or results.

## Metadata

- Investigation ID (`INV-<track>-<nn>`, per the
  [identifier table](../README.md#identifiers)):
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
  measurement-failure coverage:
- Backlog entries created for newly surfaced questions, or exact statement:
  `no new issues found`
- Discoveries during source, method, execution, or result work, recorded
  separately with stage and backlog ID, or exact statement: `no new issues found`

## Source inventory

| Source ID | Classification | Version/date | Exact sections | Claim use | Applicability limits |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## Claim inventory

Use one row per claim, with identifier `CLM-<track>-<nn>-<nn>` under this
investigation's number. The evidence label belongs to each individual claim;
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

## Code validation

- Experiment code revision and validation artifact:
- Negative and fault-injection checks:
- Expected failure behavior and observed validation outcome:

## External immutable raw artifact

- Immutable raw artifact location:
- Manifest location:
- Manifest contents: producing experiment code/revision, method, environment,
  inputs, integrity information:

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

Consolidated roll-up only: aggregate the breadth-pass and later-stage
discovery entries already recorded above; record new discoveries there, not
here.

- All backlog IDs from the entries above, or exact statement: `no new issues found`

## Limitations

- Scope, applicability, and uncertainty limits:

## Applicable gates and review links

Record every row. `not-applicable` requires an explicit reason. Gate
applicability by evidence type is defined normatively in the evidence policy's
[promotion and challenge](../evidence-policy.md#promotion-and-challenge)
section; the rule for when an applicable gate passes is defined in
[gate completion](../evidence-policy.md#gate-completion). The Verdict(s) cell
records every required review's verdict, with the gate-closing verdict last.

| Gate | Applicable? | NA reason | Review ID(s) | Independence required/met | Verdict(s) | Unresolved changes | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| question |  |  |  |  |  |  |  |
| source |  |  |  |  |  |  |  |
| method |  |  |  |  |  |  |  |
| code |  |  |  |  |  |  |  |
| result |  |  |  |  |  |  |  |
| replication |  |  |  |  |  |  |  |
| adversarial |  |  |  |  |  |  |  |
| synthesis |  |  |  |  |  |  |  |
