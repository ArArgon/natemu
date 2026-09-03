# Research-phase executor guidance

## Scope guard

Organizing these files does not authorize browsing, experiments, probes,
benchmarks, or research execution.

## When separately authorized to conduct research

The scope guard above controls this organization-only work. When separately
authorized to conduct research, executors must first read:

- `docs/research/README.md`
- `docs/research/evidence-policy.md`
- `docs/research/executor-guide.md`

When separately authorized to conduct research, treat material at or before
preservation commit `a121702` as inherited provenance, not evidence. Reject
inherited probes and results as evidence. Do not make binding implementation
decisions during research organization. Perform breadth passes, add identified
gaps to the question backlog (`docs/research/question-backlog.md`), and use
primary sources for load-bearing claims.

When separately authorized to conduct research, method and code reviews
must pass before experiments begin.
Correctness failures invalidate performance samples.
Load-bearing findings require independent review.

When separately authorized to conduct research, keep newly generated raw data
outside Git and treat SMP and platform dimensions explicitly in research
questions, methods, evidence, and conclusions.
