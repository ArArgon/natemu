# BP-00-03-start — fair-baseline breadth pass

## Metadata

- Breadth-pass ID: `BP-00-03-start`
- Question: `Q-00-03`
- Date: 2026-09-03
- Executor/session: Codex `/root`, goal thread

This pass organizes questions and establishes no evidence or conclusion.

## Coverage

- **Product and adjacency:** classify a system as a fair baseline only when it
  solves the same in-scope x86-64 guest job; classify native arm64, remote
  execution, containers, and specialized translation as alternatives or
  diagnostic controls when their contract differs.
- **Architecture neutrality:** QEMU TCG is mandatory. Other baselines must be
  selected by relevance and availability, not by expected ranking.
- **Correctness:** compare only configurations passing the same declared guest
  workload correctness criteria. A baseline failure is reported, not silently
  removed.
- **Host/guest/SMP matrix:** run both sides on the same machine and OS, with the
  same guest, workload, vCPU count, memory budget, and device semantics;
  disclose core placement and tuning.
- **Negative cases and falsifiers:** configuration features unavailable on one
  side require a predeclared common-denominator comparison or a separately
  labelled capability comparison, never substitution after observing results.
- **Measurement failures:** guard against tuning asymmetry, version ambiguity,
  order and thermal effects, cross-machine comparisons, and unequal warm-up.

## Backlog updates

no new issues found

