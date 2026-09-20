# INV-<track>-<nn> — title

During exploration, use only the relevant sections for the brief record required
by the [workflow](../README.md#conduct-an-investigation), and label runs exploratory.
Expand the method and review record when seeking acceptance of a finding.

## Current summary

- **Question:** Q-<track>-<nn>, the question and decision it affects.
- **Conclusion and applicability:** accepted finding and evidence type, or no
  accepted conclusion; host/OS/SDK, guest, signing, SMP and workload limits.
  Distinguish accepted original scope from open supplements.
- **Material blockers:** substantive defects and their consequences; none if clear.
  State pending review separately as work status.
- **Next action:** one bounded step, decision consequence, budget and stopping
  condition. Missing execution budgets must be resolved before running.

## Assumptions and breadth

Question-opening breadth coverage; current blockers here, downstream questions
linked to their backlog homes, and retained unknowns. State when none were found.

## Supporting contracts and claims

Explain the relied-on meaning with direct primary citations (sections, symbols,
paths; versions only where material). Keep existing claim identifiers. Label each
substantive claim Source-backed, Observed, Inferred, or Hypothesis; state inference
and uncertainty. Include contradictions and meaningful negative findings.

## Method and controls

Hypothesis/falsifiers, procedure, inputs, positive/negative controls, correctness
oracle, alternatives/confounders, sample plan, exclusions, resource/time budget,
and stop criteria. Explain platform and SMP dimensions. For source-only work,
explain the audit scope and how contrary sources were sought.

## Evidence and limitations

Results and direct artifact references, run identity/checksums and retained build
inputs; correctness separately from timing qualification. Explain applicability,
replication, alternatives, exclusions, unresolved defects and unreviewed evidence.
Do not promote historical evidence through formatting changes.

## Review and self-checks

Link the pre-execution and findings threads when needed. Briefly record submitted
self-checks and their limits here. Incorporate accepted corrections and unresolved
substantive findings above, so this section need not reconstruct review history.
Historical review/manifests may be linked for a specific finding or audit.
