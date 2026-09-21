# Question backlog

Track 01 entries link to their owning investigations; current question content,
conclusions and work status live there. Track 02 identifiers redirect to Track 03; other historical rows are migrated
when used. Current workflow policies override their old whole-track dependencies.
New questions follow [the research workflow](README.md).

## Status vocabulary

The following vocabulary describes unmigrated historical rows in other tracks:

- `proposed` — recorded but not yet selected for work.
- `gated` — selected question awaiting its question-gate review.
- `investigating` — evidence work is in progress.
- `review` — evidence work is awaiting an applicable review gate.
- `accepted` — the question has an accepted, applicable resolution.
- `rejected` — the question or its proposed resolution was rejected.
- `blocked` — progress cannot continue; record the dependency or reason.

## Category headings

### Scope

The September 19 owner policies in the [workflow](README.md#work-order-and-architecture)
supersede documented-only capability and general cross-generation requirements
in historical rows below; licensing is a warning. Historical rulings retain their dates.

The historical `INV-00-*` acceptance labels below do not supply currently
unavailable investigation artifacts. Before using a workload, compatibility,
baseline or quantitative-success resolution in synthesis, its owning question
must bind an extant applicable record or explicitly resolve the missing input.
The current Track00/workflow envelope remains authoritative; no historical label
by itself supplies a benchmark portfolio or success threshold. See
[INV03-01 scope handling](investigations/INV-03-01.md#assumptions-and-breadth).
Q-00-01/02/03/04 own workload, compatibility, baseline and success inputs;
Q-04-01 owns cost ranges, Q-05-01 owns the qualified benchmark method, and
Q-06-03/06 own lifecycle estimates and the adaptation budget before synthesis
or implementation uses those inputs.

Track03's historical September21 [completion handoff](investigations/INV-03-01.md#track03-completion-and-synthesis-handoff--september21)
retains concrete inputs to those existing owners (final component selection is
reopened by the extension-versus-hybrid comparison): bounded binary64 x87 numerical
limits and status-observation costs; native eligibility/recovery/service/compile
frequencies; dual-compiler effort; named platform premises and retained Intel
semantic limits. These are disclosed synthesis/conformance obligations, not new
automatically authorized Track03 campaigns.

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions — breadth pass | Newly surfaced questions — source/method/execution/result work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `Q-00-01` | Which intended user classes and concrete, diverse workloads must the emulator represent? | Performance and compatibility evidence is meaningless if optimized for an unrepresentative job. | Initial product envelope | Compatibility boundary, benchmark suite, success aggregation | User class / workload / guest / vCPU topology / host constraints | Primary-source and upstream-workload audit followed by an explicit scope resolution | Source-backed and Inferred claims in `INV-00-01`; source gate passed (`REV-024`) | accepted; question `REV-001` accept; source `REV-024` accept (independent); adversarial `REV-025` accept (independent) | `Q-00-02`, `Q-00-03`, `Q-00-04`, `Q-00-07`, `Q-05-01` | source: `Q-05-03`, `Q-06-02` |
| `Q-00-02` | What testable x86-64 Linux compatibility boundary, including guest versions, CPU surface, boot path, devices, and rejection behavior, is required? | It bounds feasibility and correctness claims and prevents silent scope drift. | `Q-00-01` | Feasibility matrix, correctness surface, test coverage | Host generation / macOS and SDK / kernel and distribution / CPU profile / memory / vCPU topology / device set | Primary specifications and upstream support-policy analysis plus explicit boundary resolution | Source-backed, Inferred, and Hypothesis claims in `INV-00-02`; source gate passed (`REV-031`) | accepted; question `REV-001` accept; source `REV-031` accept (independent); adversarial `REV-032` accept (independent) | `Q-00-09`, `Q-02-01`, `Q-05-02`; `BP-00-end` (unresolved cross-record implication owned here) | source/review: `Q-02-03` (ex-`Q-01-13`, retired), `Q-02-03`, existing `Q-05-02`, `Q-05-03` |
| `Q-00-03` | Which systems and configurations are fair baselines, and which systems are diagnostic controls only? | The decision question requires comparison with QEMU TCG and forbids asymmetric comparisons. | `Q-00-01`, `Q-00-02` | Benchmark method, reporting, success judgment | Same host / guest / workload / resources / tuning effort / vCPU count | Primary documentation and versioned upstream-source audit plus comparison protocol | Source-backed and Inferred claims in `INV-00-03`; source gate passed (`REV-033`) | accepted; question `REV-001` accept; source `REV-033` accept (independent); adversarial `REV-034` accept (independent) | `Q-03-01` (ex-`Q-01-02`, retired), `Q-05-01` | source: `Q-05-05`, `Q-05-06`, `Q-06-04` |
| `Q-00-04` | What precommitted quantitative definition of “materially better” applies across the representative workload portfolio? | A threshold fixed after measurement would permit benchmark and metric cherry-picking. | `Q-00-01`, `Q-00-03`, `Q-00-07` | Performance estimation, benchmarking, stop/continue outcome | Workload class / metric / host / guest / vCPU count / confidence / aggregation | Decision analysis informed by workload requirements and measurement methodology | Source-backed, Inferred, and Hypothesis claims in `INV-00-04`; source gate passed (`REV-033`) | accepted; question `REV-001` accept; source `REV-033` accept (independent); adversarial `REV-034` accept (independent) | `Q-04-01`, `Q-05-01`, `Q-05-02`; `BP-00-end` (unresolved cross-record implication owned here) | source: `Q-05-07`, `Q-05-08` |
| `Q-00-05` | What are the phase’s explicit non-goals and deferred concerns? | Clear exclusions prevent research from silently becoming implementation or expanding the product envelope. | `Q-00-01`, `Q-00-02` | Research boundaries, planning eligibility | Platform / guest / device / UI / firmware / security / distribution | Boundary analysis and explicit scope resolution | Inferred claims in `INV-00-05`; source gate not applicable (`REV-015`) | accepted; question `REV-001` accept; source not-applicable `REV-015`; adversarial `REV-032` accept (independent) | `Q-00-08`, `Q-00-09`; `BP-00-end` (unresolved cross-record implication owned here); administrative disposition order unresolved in `INV-01-02` (`CLM-01-02-12`, REV-151); retained boundary unchanged |adversarial review maps distribution/privilege/lifecycle work to existing `Q-00-10` (ex-`Q-01-01`, retired), `Q-03-02` (ex-`Q-01-11`, retired), `Q-06-03`; `REV-151`: administrative order of helper-dependent family disposition and gated scope reopening remains unresolved; current deployment boundary is unchanged. |
| `Q-00-06` | Which concrete, checkable findings require a project-level stop, and which kill only one architecture family? | Research needs predeclared termination rules rather than post-hoc optimism. | `Q-00-02`, `Q-00-04`, `Q-00-07` | Feasibility kills and synthesis outcome | Technical / correctness / performance / economic / coverage / licensing | Falsifier analysis with claim-to-stop-condition mapping | Inferred and Hypothesis claims in `INV-00-06`; source gate not applicable (`REV-028`) | accepted; question `REV-001` accept; source not-applicable `REV-028`; adversarial `REV-034` accept (independent) | `Q-00-10` (ex-`Q-01-01`, retired), `Q-03-01` (ex-`Q-01-02`, retired), `Q-03-02` (ex-`Q-01-11`, retired), `Q-01-30` (ex-`Q-01-12`, retired), `Q-06-01`; `BP-00-end` (unresolved cross-record implication owned here); administrative disposition order unresolved in `INV-01-02` (`CLM-01-02-12`, REV-151); retained boundary unchanged; `BP-01-end` (open cross-record implications; working pass, not track closure) | source: `Q-04-02`, `Q-06-05` |
| `Q-00-07` | What quantitative conditions make the project economically credible? | Economic credibility is part of the governing decision question but currently has no threshold. | `Q-00-01` | Materially-better threshold, maintenance assessment, synthesis | User / workload / development effort / maintenance / alternative cost / adoption | Decision and sensitivity analysis using explicitly sourced inputs | Source-backed, Inferred, and Hypothesis claims in `INV-00-07`; source gate passed (`REV-024`) | accepted; question `REV-001` accept; source `REV-024` accept (independent); adversarial `REV-025` accept (independent) | `Q-03-01`, `Q-04-01`, `Q-06-01`; `BP-00-end` (unresolved cross-record implication owned here) | source: `Q-05-04`, `Q-06-02`, `Q-06-03` |
| `Q-00-09` | What analysis of UEFI and Windows implications must research deliver without implementing either? | The envelope requires analysis but does not define its depth or decision relevance. | `Q-00-02`, `Q-00-05` | Non-goals and synthesis unknowns | Firmware / guest OS / CPU profile / devices / licensing | Primary-specification boundary analysis | Source-backed and Inferred claims in `INV-00-09`; source gate passed (`REV-031`) | accepted; question `REV-001` accept; source `REV-031` accept (independent); adversarial `REV-032` accept (independent) | no new issues found | no new issues found |
| `Q-00-10` | Which Apple silicon and macOS capabilities are admissible, and does any distribution constraint apply? | The design depends on hardware controls reachable only from a hypervisor guest; the old envelope excluded them. | `Q-00-05` | Feasibility question set; architecture candidates | Host generation / macOS release | Owner ruling 2026-09-05: documented Apple silicon capabilities admissible; experiment project, no distribution constraint | not applicable (owner scope decision) | accepted by owner ruling 2026-09-05; no independent review | none | n/a |
| `Q-00-11` | What is the verdict when a required mechanism works on one chip generation but not another? | Per-mechanism kills would discard a workable design over an older host. | `Q-00-10` | Applicability statements per host; Track 01 wording | Host generation | Owner ruling 2026-09-05: narrows applicability, never a kill; M4 is host of record, M2 Pro/Max is replication debt | not applicable (owner scope decision) | accepted by owner ruling 2026-09-05; no independent review | none | n/a |
| `Q-00-12` | Is a second supported macOS release a research or product requirement? | Both available hosts run macOS 26. | `Q-00-11` | Scope envelope; applicability statements | macOS release | Owner ruling 2026-09-06: dropped | not applicable (owner scope decision) | accepted by owner ruling 2026-09-06; no independent review | none | n/a |

### Platform — Track 01

Questions of the [feasibility charter](tracks/01-feasibility.md). `Q-00-10` (ex-`Q-01-01`, retired) to `Q-01-23` are retired, never reassigned, and recoverable at tag `track01-archive-2026-09-06`.

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions — breadth pass | Newly surfaced questions — source/method/execution/result work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `Q-01-30` | [Guest paging under SMP](investigations/INV-01-30.md) | Current question, applicability, conclusion and next action are owned by the linked investigation. | — | — | — | — | — | — | — | — |
| `Q-01-31` | [Precise fault delivery](investigations/INV-01-31.md) | Current question, applicability, conclusion and next action are owned by the linked investigation. | — | — | — | — | — | — | — | — |
| `Q-01-32` | [Asynchronous interrupt injection](investigations/INV-01-32.md) | Current question, applicability, conclusion and next action are owned by the linked investigation. | — | — | — | — | — | — | — | — |
| `Q-01-33` | [Host-produced code publication, links and miss continuation](investigations/INV-01-33.md) | Current question, applicability, conclusion and next action are owned by the linked investigation. | — | — | — | — | — | — | — | — |
| `Q-01-34` | [Secondary CPUs start without real mode](investigations/INV-01-34.md) | Current question, applicability, conclusion and next action are owned by the linked investigation. | — | — | — | — | — | — | — | — |


Track 01's current breadth check refines existing owners without opening a
sixth feasibility question: `Q-00-02` owns the Linux 5.19 mailbox-feature floor
versus actual distribution compatibility; `Q-02-01/03` own stock-image boot,
the full CPU/`verify_cpu` contract and CPU lifecycle; `Q-03-02` owns translator
recovery metadata and the audited Ubuntu startup32 path; `Q-03-04` owns paging
and code-cache mechanism choice. SMP ordering/stress and containment remain
`Q-02-02/04`, qualified costs and measurements remain Tracks 04/05, M2 replication
remains `Q-05-02`, and adaptation effort remains `Q-06-06`. These are carryforward
questions, not downstream resolutions. The [charter's completion breadth
record](tracks/01-feasibility.md#completion-breadth--september-7)
records the review contexts and exact scope.

### Correctness

Track 02 is retired. `Q-02-01`–`Q-02-04` are retained as redirected obligations, not open standalone
investigations or accepted findings. Their meanings and current owners are in
the [Track 02 tombstone](tracks/02-correctness.md). Architectural decisions and
correctness evidence now live under [Track 03](tracks/03-architecture-options.md).
Existing references to these identifiers follow that routing; retirement neither
answers the questions nor supplies new evidence.

### Architecture

Use the [twelve-question Track 03 inventory](investigations/INV-03-01.md#current-design-choices-and-evidence-ownership)
for current architectural decisions. The four identifiers below group detailed
evidence; they do not replace the charter questions or imply four completion
gates. Each linked investigation has one current summary and bounded next action.

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions — breadth pass | Newly surfaced questions — source/method/execution/result work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `Q-03-01` | [Combined architecture and implementation direction](investigations/INV-03-01.md) | Current question, applicability, conclusion and next action are owned by the linked investigation. | — | — | — | — | — | — | — | — |
| `Q-03-02` | [Codegen and semantic components within the captive runtime](investigations/INV-03-02.md) | Current question, applicability, conclusion and next action are owned by the linked investigation. | — | — | — | — | — | — | — | — |
| `Q-03-03` | [Machine, device placement, clocks and containment](investigations/INV-03-03.md) | Current question, applicability, conclusion and next action are owned by the linked investigation. | — | — | — | — | — | — | — | — |
| `Q-03-04` | [Shadow memory, atomicity and translated-code lifetime](investigations/INV-03-04.md) | Current question, applicability, conclusion and next action are owned by the linked investigation. | — | — | — | — | — | — | — | — |

### Performance

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions — breadth pass | Newly surfaced questions — source/method/execution/result work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `Q-04-01` | What ranges and sensitivities bound each cost component and scaling failure mode for the shim design and its variants against QEMU TCG? | Point estimates and multiplied overlapping speedups would misstate plausibility. | `Q-00-04`, `Q-03-01`, applicable correctness status | Benchmark priorities and stop mapping | Host/OS / vCPUs / placement / workload / memory pressure / I/O / invalidation | Component models with labelled inputs and sensitivity analysis | unstarted | proposed | `Q-05-01`, `Q-05-02`; `BP-01-end` (open cross-record implications; working pass, not track closure) | no new issues found |
| `Q-04-02` | What performance-candidate maturity and optimization-exhaustion predicate makes a negative result eligible to kill a design variant? | An immature prototype can be slow for contingent reasons, while endless tuning can evade a real negative result. | `Q-03-01`, `Q-04-01` | Performance stop conditions | Family / implementation maturity / profile coverage / tuning effort / configuration | Reviewed maturity criteria, profiling coverage, optimization register, and bounded tuning evidence | unstarted | proposed | no new issues found | source analysis in `INV-00-06`: `Q-04-02` |
| `Q-04-03` | What do shadow fills and flushes cost at 1/2/4/8 vCPUs under a real guest kernel's page-table traffic? | Decides whether hardware paging stays ahead of QEMU's software lookup. | `Q-01-30` | Stop mapping; mechanism choice `Q-03-04` | vCPU count / workload | Fresh reviewed Track 04 instruments and qualified timing; Track 01 diagnostic timings do not carry forward | unstarted | proposed | none | none |
| `Q-04-04` | How often do hardware address-space tags recycle under a busy guest, and what does each recycle cost? | The tag budget is far smaller than x86's process-context identifiers. | `Q-01-30` | Mechanism choice `Q-03-04` | Host generation / process count | Measurement | unstarted | proposed | none | none |
| `Q-04-05` | If a host generation lacks hardware accessed/dirty management, what does emulating the bits by fault cost? | One extra fault per page per first access and first write. | `Q-01-30` | Mechanism choice `Q-03-04` | Host generation | Measurement | unstarted | proposed | none | none |
| `Q-04-06` | What is the overhead of interrupt injection by per-block polling versus a forced boundary, and the resulting latency bound? | Sets guest scheduling latency and per-block cost. | `Q-01-32` | Interrupt mechanism for planning | vCPU count / workload | Measurement | unstarted | proposed | none | none |
| `Q-04-07` | What are the translation-request round trip and throughput during boot, and do they force an in-VM cold-code tier? | Boot touches very many unique blocks. | `Q-01-33` | Host/guest split for planning | Workload | Measurement | unstarted | proposed | none | none |
| `Q-04-08` | What are the device exit rate and cost under the scoped workloads? | Exit frequency and cross-domain service latency require qualification; no fixed per-exit cost is established. | `Q-03-03` | Device placement | Workload / device class | Measurement | unstarted | proposed | none | none |
| `Q-04-09` | What idle-wait, host-exit and wake costs follow from the selected wait mechanism on each applicable host? | Sets idle responsiveness; record actual wait/exit behavior instead of assuming that the Track 01 WFE fixture establishes a WFI-to-host contract. | `Q-01-32` | Workload scope note | Host / macOS release | Measurement | unstarted | proposed | none | none |
| `Q-04-10` | What does JIT warm-up (code-cache growth and call-site patching under a JVM or Node workload) cost under the chosen code-page handling and its escalation rungs? | JIT workloads are in scope and are the case most exposed to code-page protection. | `Q-03-04` | Escalation-rung choice | Workload | Measurement against the QEMU baseline | unstarted | proposed | none | none |

### Benchmarking

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions — breadth pass | Newly surfaced questions — source/method/execution/result work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `Q-05-01` | Which capability, microbenchmark, subsystem, integrated and end-to-end methods answer the current decision with valid comparisons? | Measurements need correctness evidence and controls for the behavior measured. | `Q-00-01`, `Q-00-03`, `Q-00-04`, correctness of the behavior measured under Track 03, relevant cost questions | Performance conclusion eligibility | Stage / workload / configuration / repetitions / order / controls | Reviewed experimental methods and code with immutable raw artifacts | unstarted | proposed | `Q-05-02`, `Q-06-01`; `BP-01-12-start`; `BP-01-end` (open cross-record implications; working pass, not track closure) | no new issues found |
| `Q-05-02` | What incremental host, OS, guest, vCPU, placement, and workload coverage supports each stated applicability level? | Cross-machine comparisons and overbroad platform claims are invalid. | `Q-00-02`, `Q-05-01` | Narrow and platform-wide claims | M4; M2 Pro/Max where key feature differences affect the claim / exact OS and SDK revisions / guest matrix / 1,2,4,8+ vCPUs / workload classes; no second-release requirement (Q-00-12) | Coverage design and per-host controlled measurement | unstarted | proposed | `Q-06-01`; `BP-00-end` (unresolved cross-record implication owned here); `BP-01-12-start`; `BP-01-03-start` (host physical RAM and swap/compressor configuration required for a valid mandatory 8 GiB cell); `BP-01-end` (open cross-record implications; working pass, not track closure) | no new issues found |
| `Q-05-03` | What immutable, redistributable workload corpus fixes commits, image digests, package snapshots, inputs, correctness oracles, cache states, and update cadence for the accepted portfolio? | Mutable inputs permit drift and post-hoc benchmark selection. | `Q-00-01`, `Q-00-02`, `Q-05-01` | Benchmark reproducibility and workload applicability | Workload / license / source revision / guest / cache state | Corpus design, source and license audit, method review | unstarted | proposed | no new issues found | source analysis in `INV-00-01`: `Q-05-03` |
| `Q-05-04` | How will measurements distinguish interactive critical-path delay from unattended or parallel batch time and attribute speedup to realizable labor, schedule, or infrastructure savings? | Wall-clock acceleration alone does not establish economic benefit. | `Q-00-07`, `Q-05-01` | Economic-benefit inputs | User / workload / active wait / parallelism / avoided compute | Workflow observation and controlled measurement with predeclared attribution rules | unstarted | proposed | no new issues found | source analysis in `INV-00-07`: `Q-05-04` |
| `Q-05-05` | What QEMU version-freeze and rollover policy preserves baseline applicability as upstream releases change during research? | A mutable baseline permits selective reruns or stale synthesis claims. | `Q-00-03`, `Q-05-01` | Baseline manifests and synthesis freshness | QEMU release / host/OS / guest / workload / measurement date | Official release audit and precommitted rerun policy | unstarted | proposed | `BP-00-end` (unresolved cross-record implication owned here) | source analysis in `INV-00-03`: `Q-05-05`; adversarial `REV-030`: upstream withdrawal of the frozen release must have a disposition |
| `Q-05-06` | How are helper threads, host CPU availability, CPU-seconds, memory, and scheduler/core placement normalized when affinity control is incomplete? | Equal vCPU count alone may conceal unequal host-resource use and placement. | `Q-00-03`, `Q-05-01` | Fair comparison and resource guards | Host/OS / core type / helper count / vCPU topology / workload | Host instrumentation validation and blocked paired design | unstarted | proposed | `BP-00-end` (unresolved cross-record implication owned here); `BP-01-12-start`; `BP-01-end` (open cross-record implications; working pass, not track closure) | source analysis in `INV-00-03`: `Q-05-06` |
| `Q-05-07` | What statistical analysis plan handles hierarchical paired sampling, simultaneous bounds, timeouts/censoring, and missing cells? | A threshold is unusable without a reviewed estimator and uncertainty procedure. | `Q-00-04`, `Q-05-01` | Material-performance verdicts | Host session / VM boot / process / iteration / workload cell | Preregistered statistical method, simulation/power checks, independent method review | unstarted | proposed | no new issues found | source analysis in `INV-00-04`: `Q-05-07` |
| `Q-05-08` | Which host CPU-time, RSS, energy, thermal, and placement instruments are valid across the required macOS matrix? | Resource and thermal guardrails require validated, comparable measurements. | `Q-00-04`, `Q-05-02` | Resource guard validity | Apple generation / macOS / SDK / instrument / sampling rate | Official documentation and controlled instrument validation | unstarted | proposed | no new issues found; `BP-01-end` (open cross-record implications; working pass, not track closure) | source analysis in `INV-00-04`; `MAN-01-07-02` execution: adjacent cross-clock point comparisons admit unbounded scheduling gaps, motivating a bracketed-containment control under `INV-01-07` |
| `Q-05-09` | What does QEMU TCG measure on the host of record (and the second generation when available) under the current workflow, with the comparison workloads fixed with `Q-00-01` and `Q-00-04`? | Nothing can be called better without a fresh reference on the same hosts. | `Q-00-01`, `Q-00-04`, `Q-05-05` | Baseline for every Track 04 and Track 05 comparison | Host generation / workload | Measurement under the current workflow; independent review | unstarted | proposed | none | none |

### Integration

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions — breadth pass | Newly surfaced questions — source/method/execution/result work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `Q-06-01` | Does the synthesis packet expose every dependency, contradiction, unknown, applicability limit, and reopened finding before selecting its single outcome? | A decision cannot rely on ungated, contradictory, or overgeneralized evidence. | Accepted scope, architecture/correctness evidence, applicable feasibility findings, estimates and targeted measurements; Track 02 retired | Stop / continue targeted research / authorize planning | Entire approved product and evidence envelope | Direct evidence dependencies, contradiction scan, adversarial critique, and synthesis review | unstarted | proposed | `BP-00-end` (unresolved cross-record implication owned here); `BP-01-end` (open cross-record implications; working pass, not track closure) | no new issues found |
| `Q-06-02` | Which independently evidenced users have recurring Apple-Silicon workflows that require a full x86-64 Linux VM, and what adoption or sponsorship can synthesis rely on? | A defined target user is not evidence of population, full-system necessity, adoption, or willingness to fund the product. | `Q-00-01`, `Q-00-07` | Economic credibility and synthesis outcome | User segment / workload / alternative / organization / frequency / adoption | Fresh user research with independently auditable observations and alternative screening | unstarted | proposed | no new issues found | source analysis in `INV-00-01`: `Q-06-02` |
| `Q-06-03` | What five-year lifecycle effort and cost distribution covers delivery, correctness validation, platform coverage, release engineering, support, maintenance, CI, test hardware, and distribution? | Public prices cannot supply project-specific build and lifecycle cost. | `Q-00-07`, `Q-03-01` | Economic decision-rule inputs | Architecture family / staffing / host matrix / release cadence / support level | Bottom-up estimate with independently reviewed ranges and sensitivity analysis | unstarted | proposed | `BP-00-end` (unresolved cross-record implication owned here); `BP-01-end` (open cross-record implications; working pass, not track closure) | source analysis in `INV-00-07`: `Q-06-03` |
| `Q-06-04` | Which adjacent user-job alternatives are economic comparators for each accepted user class without entering the emulator speedup aggregate? | The cheapest correctness-equivalent alternative may be remote, native, cross-compiled, or user-mode even when it is not a fair emulator baseline. | `Q-00-01`, `Q-00-03`, `Q-00-07` | Economic benefit and stop tests | User / workload / semantics / local-or-remote / price / latency | Per-job alternative audit using primary terms/prices and observed correctness equivalence | unstarted | proposed | no new issues found | source analysis in `INV-00-03`: `Q-06-04`; adversarial `REV-034`: guest-side activation preconditions and macOS 14+ AOT-caching variance of the Intel-translation control |
| `Q-06-05` | What bounded research-extension budget and recurrence rule distinguishes “continue targeted research” from indefinite deferral? | An unconstrained gray zone defeats predeclared stop conditions. | `Q-00-06`, accepted research budget | Synthesis outcome | Unknown / failure mode / owner / equipment / time / remaining budget | Decision rule and retrospective falsifier audit | unstarted | proposed | `BP-00-end` (unresolved cross-record implication owned here) | source analysis in `INV-00-06`: `Q-06-05` |
| `Q-06-06` | What effort budget for adapting a translator to a first single-vCPU Linux shell, if exceeded, is a stop signal? | The adaptation is the largest unbounded effort; the rule must exist before the work starts. | `Q-03-02`, `Q-06-03` | Stop condition | n/a | Owner-set budget recorded before implementation adaptation; independent review of the rule. The single-vCPU shell is a delivery milestone, not a reduction of the required SMP envelope | unstarted | proposed | none | none |

### Deferred security

| Identifier | Question | Why it matters | Dependencies | Downstream decisions | Applicability dimensions | Resolution method | Evidence status | Review status | Newly surfaced questions — breadth pass | Newly surfaced questions — source/method/execution/result work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `Q-00-08` | What minimum isolation and crash-containment properties remain required, and which security properties are explicitly deferred? | “Security deprioritized” must not erase the two security requirements retained by the envelope. | `Q-00-02`, `Q-00-05` | Non-goals, feasibility constraints, synthesis unknowns | Process boundary / host resources / guest input / failure propagation | Public API and threat-boundary analysis; no broader security implementation | Source-backed, Inferred, and Hypothesis claims in `INV-00-08`; source gate passed (`REV-031`) | accepted; question `REV-001` accept; source `REV-031` accept (independent); adversarial `REV-032` accept (independent) | `Q-00-10` (ex-`Q-01-01`, retired), `Q-06-01` |source/review: `Q-05-02` (ex-`Q-01-14`, retired), `Q-02-04`; coverage uses existing `Q-05-02`, `Q-05-06`; `REV-151`: administrative order of helper-dependent family disposition and gated scope reopening remains unresolved; current deployment boundary is unchanged. |

## New questions and breadth discoveries

Reserve a `Q-<track>-<nn>` identifier and record the question, decision consequence,
applicability and intended resolution method. Once an investigation owns a Track 01
question, this backlog keeps its title and link only. Add a downstream question
here if it has no investigation yet; keep a current blocker or retained unknown
in the investigation that owns it. Record each discovery once, linking from the
breadth check. See [the workflow](README.md#conduct-an-investigation).
