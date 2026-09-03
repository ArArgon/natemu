# Research-phase executor guidance

The project is in its research phase. Executors must first read:

- `docs/research/README.md`
- `docs/research/evidence-policy.md`
- `docs/research/executor-guide.md`

Treat material at or before preservation commit `a121702` as inherited
provenance, not evidence. Reject inherited probes and results as evidence. Do
not make binding implementation decisions during research. Perform breadth
passes, add identified gaps to the question backlog
(`docs/research/question-backlog.md`), and use primary sources for
load-bearing claims.

Method and code reviews must pass before experiments begin.
Correctness failures invalidate performance samples.
Load-bearing findings require independent review.

Keep newly generated raw data outside Git and treat SMP and platform
dimensions explicitly in research questions, methods, evidence, and
conclusions.
