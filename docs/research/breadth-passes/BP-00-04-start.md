# BP-00-04-start — materially-better breadth pass

## Metadata

- Breadth-pass ID: `BP-00-04-start`
- Question: `Q-00-04`
- Date: 2026-09-03
- Executor/session: Codex `/root`, goal thread

This pass organizes questions and establishes no evidence or conclusion.

## Coverage

- **Product:** precommit user-visible metrics, workload weights or pass rules,
  minimum effect sizes, uncertainty requirements, and regression guardrails.
- **Architecture neutrality:** success applies identically to every surviving
  family and every fair baseline; it may not embed a mechanism preference.
- **Correctness:** no incorrect or unresolved configuration contributes a
  performance success, regardless of speed.
- **Host/guest/SMP matrix:** define whether the threshold is per configuration,
  workload class, geomean, tail, or all of these across required platform and
  vCPU coverage. Avoid allowing a 1-vCPU win to hide SMP loss.
- **Alternatives and falsifiers:** compare against the best accepted fair
  baseline for each workload; expose severe regressions and compatibility loss
  independently of aggregate speed.
- **Measurement failures:** prevent post-hoc metric choice, bare means,
  multiplicity, benchmark cherry-picking, and treating statistical significance
  as practical materiality.

## Backlog updates

no new issues found

