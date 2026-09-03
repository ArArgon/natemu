# Research-phase executor guidance

## Scope guard

Organizing these files does not authorize browsing, experiments, probes,
benchmarks, or research execution.

## Required reading

Before doing research work, executors must read:

- `docs/research/README.md`
- `docs/research/evidence-policy.md`
- `docs/research/executor-guide.md`

## Evidence and decisions

Reject inherited probes and results as evidence. Do not make binding
implementation decisions during research organization. Perform breadth passes,
add identified gaps to the research workspace, and use primary sources for
load-bearing claims.

Method and harness reviews must pass before experiments begin. Correctness
failures invalidate performance samples. Load-bearing findings require
independent review.

Keep raw data outside Git. Treat SMP and platform dimensions explicitly in
research questions, methods, evidence, and conclusions.
