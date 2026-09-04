# BP-00-end — Track 00 end-of-track breadth pass

## Metadata

- Breadth-pass ID: `BP-00-end`
- Scope: the whole of Track 00 — `INV-00-01` through `INV-00-09`
- Date: 2026-09-03
- Executor/session: Claude Code main session (drafted by subagent
  `bp00_end_drafter`, finalized by main session)

This pass organizes questions across the nine Track 00 resolutions from the
product, architecture, correctness, and methodology perspectives. It
establishes no evidence, finding, or conclusion, and it re-adjudicates no
pending gate.

## Product

Jointly assumed by all nine resolutions: a local, headless, full-system
x86-64 Linux instance on Apple Silicon; direct boot without firmware; SMP as
baseline, not an extension; QEMU TCG as the only mandatory comparator; and a
success contract whose every term is currently zero-weight until downstream
prerequisites pass. No resolution asserts a user population, a working
mechanism, or an economic result.

| Unresolved implication | Records and sections | Owning backlog question |
| --- | --- | --- |
| Portfolio aggregation is specified three incompatible ways: a non-compensating per-workload conjunction with "no scalar weighted score authorized" (`INV-00-01`, "Inclusion, aggregation, and alternative rule"), a weighted geometric mean `G` that tolerates a 10% per-cell regression (`INV-00-04`, "Finite denominator and weighting algebra"; conditions 2–4), and de-duplicated observed volumes that may not reuse performance weights (`INV-00-07`, "Credibility conditions" item 1). | `INV-00-01`, `INV-00-04`, `INV-00-07` | `Q-00-04` |
| The economic zone `1.0 <= BCR < 1.5` has no disposition: `INV-00-06` ("Family-level kills" item 7) kills only below 1.0, `INV-00-07` ("Credibility conditions" item 2) requires at least 1.5 for credibility, and the "Performance decision zones" table defines an inconclusive band for performance only. | `INV-00-06`, `INV-00-07` | `Q-00-06`, `Q-06-05` |
| UEFI/Windows implications are routed to lifecycle cost (`INV-00-09`, "Resolution and populated implications register", rows `U02`, `U07`, `W06`), but the cost model (`INV-00-07`, "Valuation convention and accounting model") has no extensibility or optionality account, and non-admitted work carries `I=0` benefit. Analysis-only implications can therefore enter neither side of the ledger. | `INV-00-07`, `INV-00-09` | `Q-06-03` |

Downstream tracks inherit: a candidate user set (U1/U2 provisional, U3/U4
conditional), six named workloads of which four are proposed mandatory, and
the rule that no diagnostic, conditional, or unmeasured cell carries success
or economic weight.

## Architecture

Jointly assumed: public macOS APIs only; no process, sandbox, or emulator
architecture selected anywhere in Track 00; family admissibility deferred to
`Q-01-02`/`Q-03-01`; and containment stated as observable properties rather
than a mechanism.

| Unresolved implication | Records and sections | Owning backlog question |
| --- | --- | --- |
| A dependency on an installed privileged helper is a reopenable scope boundary in `INV-00-05` ("Claim inventory and resolution") and in `INV-00-08` ("Falsifiers, controls, and limitations"), but reads as a family kill under "prohibited distribution condition" in `INV-00-06` ("Family-level kills" item 1). Nothing decides which disposition applies first. | `INV-00-05`, `INV-00-06`, `INV-00-08` | `Q-01-02`, `Q-00-05` |
| Containment budgets terminate an offending instance within 1 s of a cap breach (`INV-00-08`, "Operational oracle contract", Resource budgets), while every mandatory workload must run to accepted completion (`INV-00-01`, "Candidate workload matrix"). Whether budgets bind during measured runs — and a termination counts as containment success or correctness failure — is unassigned between the two. | `INV-00-01`, `INV-00-08` | `Q-01-14` |
| Baseline currency and research budget pull opposite ways: any newer upstream QEMU release triggers a precommitted full decision-baseline rerun (`INV-00-03`, "Comparison protocol" item 11), while extensions are capped at 20 executor-days or 10% of remaining budget (`INV-00-06`, "Bounded targeted-research exception"). | `INV-00-03`, `INV-00-06` | `Q-05-05`, `Q-06-05` |

Downstream tracks inherit: a closed, independently reviewed family register as
a precondition for any project-wide quantifier; the deployment boundary of
routine operation without root; and the requirement that a family-specific
failure never becomes a project stop while a register entry survives.

## Correctness

Jointly assumed: correctness is a prerequisite for every timing sample; a
failed oracle invalidates dependent performance, resource, and economic
derivatives; and a defect accepted later must be propagated to named cells.

| Unresolved implication | Records and sections | Owning backlog question |
| --- | --- | --- |
| Required vCPU topologies conflict. `INV-00-02` ("Resolution") makes 2, 4, and 8 vCPUs acceptance topologies in a 48-cell matrix, and `INV-00-08` ("Resolution") requires 2/4/8 containment coverage; `INV-00-01` ("Candidate workload matrix") makes 8 conditional on an accepted 8-vCPU claim and `INV-00-04` ("Applicability matrix") admits 8 only where accepted coverage requires it. | `INV-00-01`, `INV-00-02`, `INV-00-04`, `INV-00-08` | `Q-05-02`, `Q-00-02` |
| Core placement is a frozen stratum dimension in `INV-00-04` ("Finite denominator and weighting algebra") and a per-cell recorded field in `INV-00-01` ("Candidate workload matrix"), but `INV-00-08` ("Resolution") limits coverage to "P/E-core placement strata available to the accepted instrumentation". If placement is uncontrollable, one record narrows coverage while the other keeps the stratum in the denominator. | `INV-00-01`, `INV-00-04`, `INV-00-08` | `Q-05-06` |
| Guest-visible rejection behavior is specified for statically and runtime-unsupported operations (`INV-00-02`, "Resolution") and for unauthorized host access (`INV-00-08`, "Operational oracle contract"), but no record reconciles a guest-architectural exception with a containment-classified denial. | `INV-00-02`, `INV-00-08` | `Q-02-03`, `Q-02-04` |

Downstream tracks inherit: a cell passes only on readiness, all vCPUs online,
device and shutdown checks, and accepted Track 02 status; an oops, stall,
timeout, or unexplained skip is a failure, never a censored sample.

## Methodology

Jointly assumed: every threshold, weight, exclusion, and denominator is frozen
before data; missing evidence is never a negative result; mutable sources are
snapshotted with digests and refreshed near synthesis; and no Track 00 record
runs an experiment or selects a phase outcome.

| Unresolved implication | Records and sections | Owning backlog question |
| --- | --- | --- |
| The dependency maps are mutually circular: `INV-00-04` ("Hypothesis and falsifier"; `CLM-00-04-08`) makes threshold sufficiency conditional on `Q-00-07`, while `INV-00-07` ("Dependency map and phase boundary") makes its benefit inputs conditional on the `Q-00-04` inclusion and aggregation contract. Neither names a resolution order. | `INV-00-04`, `INV-00-07` | `Q-06-01` |
| Platform coverage is stated Cartesian-style in `INV-00-01` ("Applicability matrix": each generation × each supported macOS/SDK) and in the `S*` definition of `INV-00-04`, but `INV-00-02` ("Resolution") admits only the pinned pairs `P1` and `P2` and explicitly forbids Cartesian expansion. | `INV-00-01`, `INV-00-02`, `INV-00-04` | `Q-05-02` |
| Normative policy values are approved asymmetrically: `INV-00-07` ("Credibility conditions" item 4) requires a named Track 00 decision owner to approve the discount rate, BCR, and payback limit before input collection, while the `1.50×`/`1.25×`/`0.909×` floors of `INV-00-04` ("Resolution") rely on gate review alone. | `INV-00-04`, `INV-00-07` | `Q-00-04`, `Q-00-07` |

Downstream tracks inherit: a finite frozen denominator `S*`/`K*`/`C*`; the
delegation of estimator, multiplicity, censoring, and precision choices to
`Q-05-01`/`Q-05-07`; instrument validation as a precondition for any resource
guard (`Q-05-08`); and the rule that governance cessation is labelled as such
and never reported as infeasibility.

## Required coverage

| Dimension | What this pass examined | Owners |
| --- | --- | --- |
| Product assumptions | Provisional users, zero-weight conditional classes, six-workload portfolio, materially-better and economic-credibility contracts. | `Q-00-01`, `Q-00-04`, `Q-00-07` |
| Adjacent systems | QEMU MTTCG as sole decision baseline; single-thread TCG, linux-user, Apple Intel-binary translation, native arm64, native x86, and remote execution as zero-weight controls. | `Q-00-03`, `Q-06-04` |
| Negative and boundary cases | Excluded ISA tiers, firmware, 32-bit entry, unsupported-feature rejection, denied-sentinel access, malformed descriptors, injected failing tests, wrong-architecture artifacts. | `Q-00-02`, `Q-02-03`, `Q-02-04` |
| Host/guest matrix | Pinned pairs `P1`/`P2`, four candidate guests, 2/8 GiB RAM, exact build and SDK pinning still outstanding. | `Q-05-02`, `Q-05-03` |
| Multicore interactions | All-vCPU-online admission, cross-vCPU faults and pressure, oversubscription, two live peers, placement classes, 1 vCPU as diagnostic only. | `Q-01-08`, `Q-02-02`, `Q-05-06` |
| Alternatives | Per-workflow alternative ladder, correctness-equivalence matching, and the rule that a cheaper equivalent falsifies one workload, not the product. | `Q-06-02`, `Q-06-04` |
| Falsifiers | Family versus project kills, maturity predicate before any performance kill, register closure before any all-family quantifier, coverage gaps as governance rather than infeasibility. | `Q-00-06`, `Q-04-02`, `Q-06-05` |
| Measurement failure modes | Post-hoc thresholds and weights, benchmark and metric substitution, tuning asymmetry, cross-host ratios, order and thermal drift, censoring, multiplicity, stale baseline, helper-thread resource asymmetry. | `Q-00-04`, `Q-05-01`, `Q-05-05`, `Q-05-07` |

## Backlog updates

Every implication above maps to an existing backlog question; each was checked
against the Scope, Platform, Correctness, Architecture, Performance,
Benchmarking, Integration, and Deferred-security tables before this pass
declined to create an identifier. No new question identifier is created. Each
owning row named above is annotated `BP-00-end` in its breadth-pass column so
the implication stays visible to synthesis and to the owning question's
question gate.

## Return-review residuals

Every Track 00 investigation reached independent `accept` on its applicable
source and adversarial gates on 2026-09-03 (`REV-024`/`REV-025`,
`REV-031`/`REV-032`, `REV-033`/`REV-034`). The return cycles left these
non-blocking observations, each already routed to an owning question so it
stays visible to synthesis:

| Residual | Origin | Owner |
| --- | --- | --- |
| Withdrawal of the frozen QEMU release triggers nothing under the version-order rollover rule. | `REV-030` | `Q-05-05` |
| Guest-side activation preconditions and macOS 14+ AOT-caching variance of the Intel-translation diagnostic control. | `REV-034` | `Q-06-04` |
| An `unavailable`-control fixture belongs in the future experiment code. | `REV-034` | future code gate under `Q-05-01` |
| `SRC-006`–`SRC-011` are mutable pages held by retrieval date without capture digests; the 90-day refresh rule in `INV-00-07` covers them. | `REV-024` | `Q-00-07` refresh rule |
| Advisory wording: `CLM-00-08-04` says “inherited by children” where the man page says “each process it creates”; the macOS scope is absent from the `CLM-00-03-04` applicability column. | `REV-031`, `REV-033` | owners, at next revision of those records |

No residual reopens a gate. The twelve cross-record implications above are
the track's open disagreements; none was resolved here, and each remains
owned by the question named in its row.
