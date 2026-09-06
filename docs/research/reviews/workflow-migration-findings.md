# Research workflow migration — findings review

- Reviewer: Codex `/root/clock_mach`, independent context from workflow author `/root`.
- Date: 2026-09-05.
- Scope: `docs/research/README.md`, `AGENTS.md`, retired policy/guide pointers,
  investigation/review templates, Track 01 indices and backlog migration,
  breadth notices and end-pass routing, historical ledger/manifests notices,
  and the shared experiment guidance. Compared to the user plan and pre-migration
  Git contents. This reviewer authored C1/M1 runner changes; those changes and
  their experiment READMEs are expressly outside this independent review.
- Current verdict: accept after the same-reviewer return below.

## Substantive blockers

1. **W1 — make the experimental execution boundary explicit.** Consequence:
   README's pre-execution step currently requires review before “collecting
   experimental evidence,” while the requested rule is before experiments run.
   A native pilot labeled non-evidence could read through the former wording.
   Evidence: “Conduct an investigation,” step 3, compared with the user plan's
   pre-execution review rule. Smallest remedy: say before experimental execution,
   preserving the explicit code-preparation/compile/synthetic-validation exception.
   Closure: inspect the changed sentence and the adjacent exception; no native
   execution or new experiment is required.
2. **W2 — retain the removed kill-assumption requirement.** Consequence:
   the former Track 01 roadmap required falsifiable kill assumptions tested early
   and mapped to Scope stop conditions; changing the roadmap to titles and links
   must not silently remove this research requirement. Evidence: pre-migration
   `docs/research/tracks/01-feasibility.md`, “Kill assumptions,” compared with the
   new workflow's preserved prototype and completion requirements. Smallest
   remedy: add one sentence retaining the requirement and pointing to INV-01-02's
   owning register. Closure: inspect that sentence and verify the existing register
   remains the home; no new dashboard or duplicate register is needed.

## Checks and limits

The replacement has two independent review steps, source-only direct findings
review, retained reviewer returns, review-author independence, independently
checked replication preparation/code, concise self-checks, substantive blocker
criteria, editorial correction disclosure, and separate final synthesis. It
preserves the product envelope, preservation-checkpoint exclusion, correctness
invalidation of timings, platform/SMP scope, bounded next actions, raw data outside
Git, source-snapshot builds, factual runtime checks, and strict format transition.

A Python comparison of all non-Track-01 `Q-` backlog table rows against Git HEAD
found identical content and order. Both investigation and feasibility indices have
exactly nineteen title/link entries without live review status. Historical ledger
and breadth notices explicitly retire update obligations and live status; the old
records remain recoverable. The end-pass now routes implications to owning
investigations/downstream questions; detailed preservation of all nineteen notes
is covered by the separate investigation migration review, not certified here.

No scientific source or experimental result was re-adjudicated. No editorial
edits or load-bearing workflow changes were authored by this reviewer. No external
source search or native experiment was needed for this policy/document comparison.

## Returns

### Return 1 — 2026-09-05, same independent reviewer

The workflow author changed step 3 to require review “before experimental
execution” and retained the following sentence explicitly allowing preparation,
compilation and synthetic validation. W1 is closed without broadening authoring
restrictions. The author also restored the Track 01 requirement for explicit
falsifiable kill assumptions, testing them early and mapping each to Scope stop
conditions, pointing directly to INV-01-02's existing register. W2 is closed
without introducing a duplicate register.

I inspected both changed sentences, their adjacent execution exception and
track-completion rules, and the unchanged register ownership. The corrections
preserve the rest of the requested simplification. No unresolved substantive
blocker remains within this review's stated scope. Verdict: **accept**.

