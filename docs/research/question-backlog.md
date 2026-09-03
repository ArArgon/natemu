# Question backlog

This backlog is intentionally empty until separately authorized research
creates entries. Its presence organizes future work only; it does not
authorize browsing, source acquisition, probes, experiments, benchmarking, or
any other research activity.

## Status vocabulary

Use exactly one status per question:

- `proposed` — recorded but not yet authorized for research.
- `gated` — authorized question awaiting its question-gate review.
- `investigating` — authorized evidence work is in progress.
- `review` — evidence work is awaiting an applicable review gate.
- `accepted` — the question has an accepted, applicable resolution.
- `rejected` — the question or its proposed resolution was rejected.
- `blocked` — progress cannot continue; record the dependency or reason.

## Category headings

### Scope

### Platform

### Correctness

### Architecture

### Performance

### Benchmarking

### Integration

### Deferred security

## Reusable entry template

Copy this template into the applicable empty category only after separate
research authorization. Do not treat the placeholders as an entry.

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `<Q-ID>` | `<question>` | `<decision relevance>` | `<dependencies or none>` | `<decisions affected>` | `<host / OS / guest / CPU / topology / workload>` | `<source, observation, inference, or hypothesis>` | `<unstarted / label and links>` | `<required gate and verdict>` | `<IDs, or no new issues found>` |

## Breadth-pass rule

For each authorized question, the executor must record an auditable breadth
pass and update this backlog with every newly surfaced issue. If the pass
surfaces none, record the exact phrase `no new issues found`. A breadth pass
organizes questions; it does not establish evidence or a conclusion.
