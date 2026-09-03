# Evidence policy

This policy applies only when research has been separately authorized. This
organization work does not collect, assess, or promote evidence.

## Evidence labels

- **Source-backed** — a claim directly supported by a primary specification,
  official documentation, versioned upstream source, or peer-reviewed paper,
  with the relevant version, scope, and applicability recorded. Secondary
  sources may locate evidence but cannot carry load-bearing claims.
- **Observed** — a result produced by an approved harness and method, with its
  raw-data manifest, environment, and limitations recorded.
- **Inferred** — a reasoned conclusion from identified Source-backed and/or
  Observed evidence; the reasoning and uncertainty are explicit.
- **Hypothesis** — a testable proposition that has not yet been established.

Every substantive claim must carry one of these labels. Unlabelled claims are
not evidence and cannot support a decision. Each Source-backed claim requires a
claim-level citation, including source version and applicability to the defined
product envelope. Load-bearing claims require independent review.

Raw data is an immutable external artifact, not Git content. Its manifest must
identify the artifact, producing harness and version, method, environment,
inputs, and integrity information. A prior checkpoint may seed hypotheses only;
it is not accepted evidence.

## Promotion and challenge

The applicable gates depend on the evidence type. Every question requires the
question gate. Source-backed evidence requires the source gate. An experiment
requires the method, harness, and result gates. A load-bearing Observed finding
also requires independent replication. Every load-bearing candidate finding
requires adversarial review.

Eligible reviewed findings may enter candidate synthesis. The synthesis gate
then approves, revises, or rejects the final decision packet; it is not an
individual-finding prerequisite. A contradiction reopens the finding and every
dependent decision until the contradiction is resolved or the claim is
withdrawn.

No policy label converts a future action into authorization. Browsing, source
acquisition, harness work, data collection, or synthesis remains conditional on
separate authorization for that research work.
