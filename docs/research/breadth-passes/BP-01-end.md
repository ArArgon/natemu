# BP-01-end — Track 01 end-of-track breadth pass

## Metadata

- Breadth-pass ID: `BP-01-end`
- Scope: all nineteen current Track 01 investigations — `INV-01-01` through
  `INV-01-14`, plus `INV-01-18`, `INV-01-19`, and `INV-01-21`–`INV-01-23`
- Status: working end-of-track pass; closure remains pending
- Date: 2026-09-04
- Executor/session: Claude Code main session (drafted by subagent
  `bp01_end_drafter`, finalized by main session); reconciled by Codex `/root`
  against `e87a5fe` on 2026-09-04. This revision preserves the draft's open
  implications and corrects its gate snapshot; it is not a closing review.

This pass organizes questions across the nineteen Track 01 investigations from
the product, architecture, correctness, and methodology perspectives. It
establishes no evidence, finding, or conclusion, and it re-adjudicates no
pending gate. Where a record's gate is open, this pass reports the open state
rather than reading through it.

**Gate state this pass reports against** (owning investigation tables and
review ledger at `e87a5fe`, 2026-09-04): eleven original investigations are
accepted: `INV-01-01`, `-03`–`-06`, `-09`–`-14`.
The platform/toolchain/licensing adversarial chain closed at `REV-121`;
the prototype chain closed at `REV-120`. These acceptances retain their
recorded claim and host limits.

Current return delta, 2026-09-05: `INV-01-02` source passed at REV-159;
adversarial REV-151 returned candidate/class counting, conditional F3
dependencies, kill eligibility, helper-order inference and status corrections.
REV-171 accepted the corrections and finite register closure, including bounded
F5 adoption. Twelve original bounded packets are accepted; this does not close their
new supplemental scope or prove untested mechanisms. INV06's delivered-Mach
supplement passed source187/method188; its code and delivered evidence remain
open, and corrected original claim05 is documentary only.

`INV-01-07` retains two I16-failed runs with no admissible result. Containment
method/code REV-139/145 and directional-bound method/code REV-153/162 passed.
The fresh full run `20260905T073805Z-103f417` completed with checker PASS;
MAN-01-07-03 identifies its immutable artifacts. Independent inspection found
checker coverage gaps (strict-dispatch early wakeups and positive round-trip
minimum) plus nominal-rate/dispatch interpretation issues. No finding is
promoted; the negative companion MAN-01-07-04 is terminal with expected failures;
method/code/result review remains open. REV193 further reproduced malformed
clock-field PASS outcomes; fresh clock-only prerequisite M07-C1 passed method194,
with code195/196 in progress. It is not an accepted instrument or substitute
timer resolution. Source192/212
supply qualified documentary premises only.

`INV-01-08` source/method/result corrections passed REV-166/167/168 historically;
amended reporting code passed REV-198. Adversarial REV202 returned missing
fallback correctness oracles, unsafe handler stdio, source/cost/scope claims
and an unmet clock prerequisite. F08-2 source206/method207 and the corrected
historical interpretation208 are accepted; no new F08-2 result is accepted.
Producer209/code210 and fresh correctness-qualified data/replication remain
required. Historical fallback cost and T3 latency are unqualified diagnostics,
not inputs to a cost model or KA-06 disposition. Both MP LDAR/STLR histograms are
COMPARABLE, but user-interactive plain MP exposed zero relaxed outcomes in
both runs and cannot corroborate its strengthened counterpart under the
method's prerequisite. All four SB comparisons stay INCONCLUSIVE; KA-06
is open. The CMODX source correction passed REV-166.

Five follow-ups (INV-01-18/19/21/22/23) passed independent bounded source
reviews REV-154–158. Shared-code protocol method passed REV-180 and experiment implementation is underway;
none of the five has an experiment result or an accepted complete
protocol/lifecycle/time-policy/scheduling resolution.

The user's 2026-09-04 instruction is to finish Track 01 without another Apple
Silicon machine for now. Under the executor guide's incremental replication
rule, unavailable-host replication is deferred until that host is available
and must precede platform-wide synthesis use. This changes neither the
`P1`/`P2` product matrix nor the applicability of existing observations.
It does not waive independent review, same-host independent replication,
correctness gates, or resolution of the remaining Track 01 questions.

## Product

Jointly assumed by the investigations: the accepted Track 00 envelope,
unchanged — public macOS APIs only; routine operation without root, an
installed privileged helper, kernel extensions, private APIs, or undeclared
entitlements; unmodified x86-64 Linux guests booted directly without firmware;
SMP as baseline; the headless device set; and the two pinned host/OS pairs
`P1`/`P2` as the whole admissible matrix. No record selects a design, prefers a
family, makes a performance or economic statement, or authorizes planning.
Every Observed claim in the track was taken on one host — `Mac16,10` (M4) /
macOS 26.6.2 (`25G83`) / SDK 26.4 — which is `P2`'s generation and release line
but not `P2`'s model.

| Unresolved implication | Records and sections | Owning backlog question |
| --- | --- | --- |
| The network device class survives on one disjunct that cannot do the job the class was named for. `vmnet.framework` was Observed unavailable unentitled (`VMNET_FAILURE`, `status_code: 1001`) and fatal (`SIGKILL`) ad-hoc-entitled, and it was the only L2-capable candidate named, so `KA-08`'s "BSD sockets *or* an available `vmnet` mode" survives only through the disjunct that cannot move Ethernet frames (`INV-01-09`, "Implications for `KA-08`/`KA-12`/`KA-13` and families F1–F4"). `INV-01-13` carries the consequence as an Open Transport cell — a virtio-net contract must be fed by a user-space TCP/IP stack in the host process — while `INV-01-11` classifies libslirp and lwIP only as optional dependencies with libslirp's six bundled copyrights unaudited ("Resolution: dependency compatibility table"). A now load-bearing dependency is carried on an optional dependency's licence footing. | `INV-01-09`, `INV-01-11`, `INV-01-13` | `Q-01-13`, `Q-01-11` |
| The mandatory 8 GiB guest-RAM cells have no host on which they have been shown reachable. `INV-01-03` ("Limitations") reserved 8 GiB and *backed* 2 GiB on a 16 GiB host and states it "cannot establish behavior for a fully resident 8 GiB guest", routing the required host memory configuration to `Q-05-02` and `Q-01-14`; `INV-01-14`'s memory row carries a provisional overhead ceiling it calls family-dependent ("Implications for `KA-08`/`KA-12`/`KA-13`"). `KA-03` is therefore "not falsified on the host of record" rather than satisfied, and no record owns the host on which a mandatory cell could run. | `INV-01-03`, `INV-01-14`, `INV-01-02` (`KA-03` row) | `Q-05-02`, `Q-01-14` |
| Per-instance memory containment has no kernel-enforced refusal on this host. `RLIMIT_AS`/`RLIMIT_DATA` could not even be *set* to a lower soft value (`set_ok: false`, `set_errno: 22`), so the memory budget rests on `task_info` self-accounting plus supervisor-enforced termination — which `INV-01-14` itself calls "materially weaker than kernel-enforced refusal" ("Correctness status"). `INV-00-08`'s containment contract requires termination within 1 s of a cap breach, and `INV-01-14` answers the `BP-00-end` implication with "budgets bind unconditionally", so a self-accounting miss is a containment miss with no second line. Whether self-accounting-only is sufficient is expressly not established here. | `INV-01-14`, `INV-01-09` | `Q-01-14`, `Q-02-04` |
| The licensing kill surface narrowed to zero rows on a distribution model the project has not chosen. `INV-01-11` added `Model C` (a copyleft-licensed NatEmu binary) at the `REV-096` A1 return, under which both previously "Incompatible" cells resolve, and `INV-01-02` records the consequence that `KA-10` can kill no registered family today (`CLM-01-02-15`). `Model C`'s source/adversarial review is now accepted (`REV-093`/`REV-121`), but the actual distribution model is undecided, and `INV-00-07`'s cost model has no extensibility or optionality account in which "choosing a model to keep a family alive" could be priced (`BP-00-end`, Product table, row 3). Four uncertainty categories — chiefly the Intel SDM cross-ISA implementation-scope question — are untouched by any model. | `INV-01-11`, `INV-01-02` | `Q-01-11`, `Q-06-03` |

Downstream tracks inherit: a device set whose backings are Observed working on
one host for block, serial, and entropy; a network class narrowed to a
user-space stack; a timer/APIC class with **no measured input at all** (pending
re-run); and the rule that a device-class failure narrows the machine contract
for every surviving family alike rather than selecting among them.

## Architecture

Jointly assumed: the six-family register `F1`–`F6` and the thirteen
kill-assumption identifiers `KA-01`–`KA-13` as fixed vocabulary, owned by
`INV-01-02`; that admissibility is recorded, never preference, with comparison
reserved to `Q-03-01`; that a family-specific failure is not a project stop
while a register entry survives; and that survival means the absence of an
eligible reviewed kill, not positive admissibility across the matrix.

| Unresolved implication | Records and sections | Owning backlog question |
| --- | --- | --- |
| The measured signing configuration does not establish the intended distributed-product configuration. `INV-01-04` measured `MAP_JIT` allocation, toggle cost, stub execution, and per-thread write/execute exclusivity under ad-hoc signing with **no Hardened Runtime and no `jit-write-allowlist`**, on 1–2 threads ("Limitations"); `SRC-059` documents that the notarizable Developer ID / Hardened-Runtime configuration requires `com.apple.security.cs.allow-jit` and is limited to **one `MAP_JIT` region**, and that `com.apple.security.cs.jit-write-allowlist` removes `pthread_jit_write_protect_np` outright (`INV-01-01`, "Limitations", `KA-01` scope). A single-region constraint bears directly on a shared code cache under 2–8 vCPU threads, which is `Q-01-18`'s now-open subject, and `INV-01-02` carries the quadrant qualifier into `F2`'s mandatory-API cell without any record having tested the other quadrants. | `INV-01-01`, `INV-01-04`, `INV-01-02` (`F2` row, `KA-01` row), `INV-01-12` (`CLM-01-12-04` configuration) | `Q-01-04`, `Q-01-18`, `Q-01-02` |
| `F3` survives on two premises that no registered evidence establishes, and nothing anywhere exercises its distinguishing mechanism. Its distribution premise — whether a Developer ID-signed, notarized build may carry `com.apple.security.hypervisor` — remains a Hypothesis: the candidate source packet in `INV-01-02` does not establish distribution permission (`INV-01-02`, `CLM-01-02-03`); its granule premise now has an accepted raw `ID_AA64MMFR0_EL1` read and an accepted bounded field interpretation from `SRC-161` (`CLM-01-02-16`/`-17`, independent source/derivation `REV-134`). Bounded register adoption passed REV-171; full API-view applicability and EL1 mechanism composition remain open; the original `INV-01-01` probe and its no-decode scope are unchanged. `INV-01-12` states that its `F2` prototype screens `F3` in neither direction, and `INV-01-05`/`INV-01-06` state that their software-MMU and EL0-signal evidence neither supports nor obstructs it. | `INV-01-01`, `INV-01-02` (`F3` row, `CLM-01-02-03`, `CLM-01-02-10`), `INV-01-05`, `INV-01-06`, `INV-01-12` | `Q-01-01`, `Q-01-02` |
| Finite register closure and bounded F5 adoption passed independent adversarial return REV-171 after source REV-159. This meets only the register-review prerequisite for project-wide quantifiers. Every other necessary premise and applicable gate remains required for INV-00-06 project-stop inputs 1–5, and a newly credible family or contradiction reopens closure. The F5 kill remains solely the public-API finding, not a prohibition of translated x86 code inside an ARM VM. The reconciled register counts accepted owning inputs separately from assumptions proved; host-sensitive observations are not discharged against the full supported-host matrix. | `INV-01-02` ("Kill-assumption register", "Closure, reopening, and what this record does not do"), `INV-01-01` ("Resolution: `KA-02` disposition") | `Q-01-02`, `Q-00-06` |
| The kill machinery lacks the limb the only recorded kill actually needed. `INV-00-06` family-kill item 1 has no "required public API absent on the mandatory hosts" limb, so the `F5` kill was first routed through the strained "unsupported toolchain behavior" reading and then re-routed at the `REV-096` A2 return through item 1's public-API limb by argument — the API is public, but compiled out of and unreachable from every accepted arm64 build (`INV-01-02`, `CLM-01-02-09`). The reading is stated as an analogy in the owning record and flagged by the reviewer as an omission in `INV-00-06` itself, not as a defect of `INV-01-01`. | `INV-01-01`, `INV-01-02`, `INV-00-06` ("Family-level kills" item 1) | `Q-00-06` |

Downstream tracks inherit: four surviving families of unequal evidential
standing (`F2` best covered by one composed prototype instance, `F1` and `F4`
covered only through shared mechanisms, `F3` covered not at all in its
distinguishing mechanism); one originating accepted kill (`F5`) whose adoption in the register remains under review; one out-of-envelope
record (`F6`) that may not enter any denominator; the privileged-helper
disposition order resolved family-kill-first (`CLM-01-02-12`); and the rule
that vCPU-to-thread mapping and device-backing choice are within-family
variants, not family axes.

## Correctness

Jointly assumed: that a mechanism not falsified on one host *survives* and does
not pass; that a forbidden outcome not observed is corroboration at best and
never a prohibition; that an integrity-rule failure suppresses every number the
run produced; and that no record may relabel another record's claim or lift a
magnitude out of it.

| Unresolved implication | Records and sections | Owning backlog question |
| --- | --- | --- |
| `KA-06` is open and the rule that would close it can flip on noise. All four SB strengthened pairs adjudicated INCONCLUSIVE under the frozen histogram rule, because their count-weighted both-observed cell fractions fail the predeclared comparability screen; this is not a comparison of complete interleaving distributions or a causal overlap test, so these SB comparisons provide no corroboration of the strengthened mapping; the former claim that this is the entirety of the ordering divergence is withdrawn (`INV-01-08`, "Provisional resolution", "Correctness status"). Replication then flipped `MP`/`DMB ISH`/background INCONCLUSIVE→COMPARABLE (28.3×→9.0×) by straddling the 10× boundary (`REV-086`), and the method amendment adding an INDETERMINATE band was accepted by REV-167 and implemented/reviewed at REV-198. Later adversarial REV202 reopened separate fallback/handler/source/result dependencies; the band does not supply missing correctness or clock evidence. `INV-01-12`'s one litmus pattern, whose stochastic control did not fire, must not be read upward into a disposition. | `INV-01-08` ("Provisional resolution", "Correctness status", method gate row), `INV-01-12` ("Kill-assumption dispositions and family implication"), `INV-01-02` (`KA-06` row) | `Q-01-08`, `Q-02-02` |
| **Pending re-run.** `KA-05` has no disposition, and four other records' timer-bearing cells rest on that absence. Both `MAN-01-07-01` and `MAN-01-07-02` failed integrity rule `I16`; the second failure exposed scheduling ambiguity between adjacent cross-clock reads and reopened the method gate — so no resolution, read cost, wakeup-error, delivery-latency, or multi-vCPU wakeup-rate figure exists, and `KA-05` is specifically *not* falsified either (`INV-01-07`, "`KA-05` disposition and family implications"). Consequently: `KA-08`'s timer/APIC class is untested (`INV-01-09`, "Implications for `KA-08`…"); `INV-01-13`'s clocksource, RTC, and wall-clock rows have no measured host input ("Limitations"); `Q-01-22`'s time-base policy and `Q-01-21`'s power-state continuity have no facts to derive from; and the interrupt limb of `KA-11` rests on the prototype's own host timer rather than on a characterized facility. | `INV-01-07`, `INV-01-09`, `INV-01-13`, `INV-01-12`, `INV-01-02` (`KA-05` row) | `Q-01-07`, `Q-01-22` |
| "Precise and restartable" is established only over a bounded instruction set, split across two records that each disclaim the other's half. `INV-01-06` catches faults with full register state and resumes at a chosen PC for three fault classes on two threads, but states plainly that the commit-discipline half "is a property of a translator's commit discipline, not of macOS" and that its probe *skips* the faulting instruction rather than restarting it ("Kill-assumption disposition and family implications"). `INV-01-12` supplies exactly-once restart for a load, two locked read-modify-writes, and a stack `push` — bounded to naturally aligned accesses that do not cross a guest page, since anything else raises `#GP` ("Limitations", items 6–8). x86 permits arbitrary unaligned accesses and a Linux guest performs them. | `INV-01-06`, `INV-01-12`, `INV-01-02` (`KA-04` row) | `Q-02-01`, `Q-01-06` |
| Host and guest page granularity disagree, and the only guest-granular hardware candidate carries a data-loss hazard. Protection tracking is sound but operates at the 16 KiB host page, covering exactly four guest pages per notification; software checks at 4 KiB guest granularity are the second mechanism, and their isolated marginal is the one cost figure in the group that reproduced (`INV-01-05`, "Kill-assumption disposition and family implications"). `mincore`'s `MINCORE_MODIFIED` was removed from the `KA-07` disposition at the `REV-092` return and re-filed as an unevaluated candidate: its only observed re-arm, `MADV_FREE_REUSABLE`, is a memory-reclaim advisory whose header text reads "pages can be reused (by anyone)", so using it to re-arm tracking may discard live guest RAM — a correctness failure, not a cost. It is also undocumented in `madvise(2)`. | `INV-01-05`, `INV-01-03` (`CLM-01-03-14`), `INV-01-02` (`KA-07` row) | `Q-01-05`, `Q-04-01` |
| SMP is the product baseline, but the integrated prototype exercises only two vCPUs. Every mechanism probe measured 1–2 threads (`INV-01-03`/`-04`/`-05`/`-06`, "Limitations"), the prototype composed exactly two vCPUs (`INV-01-12`, "Limitations", item 2), and only `INV-01-08` reached 4 and 8 — for atomics and wakeup rate, not for the composed mechanism set. The acceptance matrix requires 2, 4, and 8 vCPU topologies, and the host scheduling contract that would let single-thread evidence be read at 8 vCPUs is `Q-01-23`, now investigating with source acceptance REV-158 and no experiment. | `INV-01-03`, `INV-01-04`, `INV-01-05`, `INV-01-06`, `INV-01-08`, `INV-01-12` | `Q-05-02`, `Q-01-23` |

Downstream tracks inherit: a composed `F2` mechanism instance on one host, five
repetitions, every invariant passing; a set of kill assumptions each narrower
than its own name; the rule that no number from a run that failed an integrity
rule may be cited, quoted, or carried; and the standing bar that structural
correctness kills belong to Track 02 and cannot be anticipated here.

## Methodology

Jointly assumed: method and code are gated before any evidence run; every
experiment carries a negative control the checker must flag; raw artifacts live
outside the repository under a manifest with per-file digests and a code
revision; a platform-sensitive observation is valid only for the host and OS it
was measured on; and missing evidence is a method state, never a negative
result.

| Unresolved implication | Records and sections | Owning backlog question |
| --- | --- | --- |
| The reported timing magnitudes cannot be carried into a cost model without their replication limits. Independent replication of the same binaries on the same host moved the memory group's magnitudes by −52.9% to +631% — signal delivery −52.9%, toggle cost −27.1%, `mprotect` +40%, fault round-trip maximum +631%, two-thread cost +4.3%→+17.93% — with `CLM-01-05-13` (+11.5%, sign-consistent) the sole robust exception (`INV-01-04`/`-05`/`-06`, "Limitations"; `REV-076`, `REV-091`). The events group's `T4b` ratio reproduced within 1.7% but its plain-SB user-interactive arm fell from 39 to 1 relaxed outcome per 10⁶, marginal power (`REV-086`). The prototype's wall time and parallelism ratio are barred from performance use by their own result gate (`REV-101`). Every median, percentile, and percentage in the track is a this-run value; no cost model, family comparison, or synthesis may lift one. | `INV-01-04`, `INV-01-05`, `INV-01-06`, `INV-01-08`, `INV-01-12` | `Q-04-01`, `Q-05-01` |
| Same-host independent replication does not widen host/OS applicability. Every replication that passed — `MAN-01-01-02`, `MAN-01-03-02`–`MAN-01-06-02`, `MAN-01-08-03`, `MAN-01-09-02`, `MAN-01-10-02`, `MAN-01-12-02`, `MAN-01-14-02` — was an independent re-implementation **on the same host**, so `P1` (M1 Pro / Sequoia 15) replication is owed on every Observed claim in the track and each kill assumption's own "the supported hosts" phrasing remains undischarged (`INV-01-02`, "Gate-status snapshot and volatility", point 2). The single exception is `KA-02`'s disposition, which rests on an architectural SDK gate rather than host behaviour and is stated not to need `P1`. | every record with an Observed claim; `INV-01-02` ("Gate-status snapshot and volatility") | `Q-05-02` |
| Reviewer independence is met per gate and concentrated per group. Each of the four reviewer groups disclosed that one context wrote most or all of its subjects' reviews: `REV-099` recommends "an uninvolved adversarial reviewer re-open them before synthesis relies on them"; `REV-100` records that "this reviewer cleared every gate for these records, so a later uninvolved adversarial reader would still add value"; `REV-113` carries the same standing recommendation; `REV-120` records that "one reviewer wrote every gate of this record after the question gate". No gate is thereby invalid, and no record disputes the disclosures — the recommendation remains visible to the synthesis owner `Q-06-01` and does not silently create a new failed gate. | `INV-01-01`, `INV-01-03`–`INV-01-06`, `INV-01-09`–`INV-01-14`; review ledger `REV-083`, `REV-099`, `REV-100`, `REV-102`, `REV-103`, `REV-107`, `REV-113`, `REV-119`, `REV-120` | `Q-06-01` |
| Two measurement conditions the track relies on are outside its own artifacts. Core placement is **not requestable through documented public API** (`INV-01-07`, `CLM-01-07-08`, Inferred), while `INV-00-04` keeps P/E placement as a frozen stratum in the denominator and `INV-00-01` records it per cell (`BP-00-end`, Correctness table, row 2) — Track 01 now supplies the platform fact that sharpens that disagreement without resolving it. Separately, every timing figure in the memory group was taken at an executor-observed 1-minute load average of 2.43 with other agents' processes running, an out-of-band `uptime` reading **that no `env.txt` carries**; the manifests state its provenance rather than the artifact carrying the figure (`INV-01-03`/`-06`, "Limitations"; `REV-090`). | `INV-01-03`, `INV-01-06`, `INV-01-07`, `INV-00-01`, `INV-00-04` | `Q-05-06`, `Q-05-08` |
| Five platform follow-ups remain unresolved, and three govern composition beyond the measured cases. `Q-01-18` (cross-thread code publication, live patching, quiescence), `Q-01-19` (JIT write-protect state across fault delivery and handler discipline), and `Q-01-23` (vCPU-thread scheduling and oversubscription at 2/4/8) each govern whether 1–2-thread mechanism results transfer to the acceptance topologies; `Q-01-21` (power states and background execution) and `Q-01-22` (guest time-base policy) await repaired `INV-01-07` measurements for their experimental or policy inputs — **pending re-run**. All five are `investigating`, with bounded source packets independently accepted at REV-154–158; later applicable gates remain open. `Q-01-19` additionally depends on `Q-01-18`; `Q-01-22` on the exact guest contract and `Q-01-21`; `Q-01-23` on `Q-01-08`, `Q-01-18`, and `Q-05-06`. | `INV-01-04`, `INV-01-06`, `INV-01-07`, `INV-01-08`, `INV-01-12` ("Newly surfaced questions" in each) | `Q-01-18`, `Q-01-19`, `Q-01-21`, `Q-01-22`, `Q-01-23`, `Q-06-01` |

Downstream tracks inherit: the admissibility rule for header-exposed, thinly
documented behavior, including its foreign-architecture exclusion and its
per-release re-probe obligation (`INV-01-01`, `CLM-01-01-15`); the rule that a
failed integrity rule is a method defect and not a platform result; the
requirement that every register cell trace to a claim identifier with its
evidence label and gate status; and the volatility warning that no gate status
in `INV-01-02` may be relied on without re-checking the owning record's own
gate table.

## Required coverage

| Dimension | What this pass examined | Owners |
| --- | --- | --- |
| Product assumptions | Public-API-only envelope, no root or privileged helper, direct boot, SMP baseline, headless device set, `P1`/`P2` as the whole matrix; the undecided distribution model and the entitlement whose distribution eligibility is unestablished. | `Q-00-02`, `Q-00-05`, `Q-01-01`, `Q-01-11` |
| Adjacent systems | Hypervisor.framework (arm64 VM API only), Virtualization.framework with an `aarch64` Linux guest, Rosetta's `VZLinuxRosettaDirectoryShare` (Intel binaries inside such a guest), QEMU TCG as study-only source, `vmnet.framework`, libslirp/lwIP as the user-space stack the network narrowing now requires. | `Q-01-02`, `Q-01-09`, `Q-01-11`, `Q-00-03` |
| Negative and boundary cases | Negative controls must be read individually: the prototype's stochastic weak-order control did not fire, and no negative companion was run for the second failed timer run; the `MAP_JIT`-unavailable, no-op-callback, unarmed-fault, clang-absent, `I5`-injection, nonatomic-RMW, skip-restart, and drop-IPI injections; `F6` as the in-register-but-unscreened boundary; the eight privileged-helper combinations deliberately not promoted; the `I16` failure itself as a control catching a method defect. | `Q-00-02`, `Q-01-02`, `Q-02-04`, `Q-05-01` |
| Host/guest matrix | One host only (`Mac16,10` / macOS 26.6.2 `25G83` / SDK 26.4 / ad-hoc signing, non-Hardened-Runtime); 8 GiB reserved and 2 GiB backed on 16 GiB physical; no guest booted by any record; `P1` owed on every Observed claim; Hardened Runtime, Developer ID, notarized, and `jit-write-allowlist` quadrants untested. | `Q-05-02`, `Q-01-01`, `Q-01-04` |
| Multicore interactions | 1–2 threads in every mechanism probe; two vCPUs in the prototype at measured parallelism 2.00; 2/4/8 threads only for LSE atomics, wakeup rate, and lost-update checks; cross-modifying code and cross-vCPU TLB shootdown explicitly out of scope; the aggregate multi-vCPU wakeup rate **pending re-run**. | `Q-01-08`, `Q-01-18`, `Q-01-23`, `Q-05-02` |
| Alternatives | Signal/`ucontext` versus Mach exception ports versus explicit checks; fault-based versus 4 KiB software-check versus `mincore`-polling tracking; `pthread_jit_write_protect_np` versus the callback write model; pty versus stdio, BSD sockets versus `vmnet`; interpretation versus dynamic versus ahead-of-time translation versus EL1-under-HVF; three candidate distribution models. | `Q-01-02`, `Q-03-01`, `Q-01-05`, `Q-01-09` |
| Falsifiers | `KA-01`–`KA-13` with each routed to an `INV-00-06` family-kill item or project-stop input; `KA-02` false in the accepted originating record (`F5` adoption still under review); eleven `KA-` rows have accepted owning inputs, including partial findings; `KA-06` open; `KA-05` has no disposition and `KA-09` has an accepted build/test/debug limb only (**`KA-05` pending re-run**); `KA-10`'s kill surface narrowed to zero rows; the register itself not closed, blocking project-stop inputs 1–5. | `Q-00-06`, `Q-01-02`, `Q-01-07`, `Q-01-08`, `Q-01-10` |
| Measurement failure modes | Non-reproducible timing magnitudes across all measuring groups; the histogram boundary flipping a verdict on noise; marginal positive-control power in one SB stratum; uncaptured host load; uncontrollable core placement; same-host replication mistaken for widened applicability; per-group reviewer concentration; gate-status drift inside a cross-record register. | `Q-04-01`, `Q-05-01`, `Q-05-06`, `Q-05-08`, `Q-06-01` |

## Backlog updates

Every implication above maps to an existing backlog question. Each was checked
against the Scope, Platform, Correctness, Architecture, Performance,
Benchmarking, Integration, and Deferred-security tables before this pass
declined to create an identifier — including the five questions Track 01 itself
filed (`Q-01-18`, `Q-01-19`, `Q-01-21`, `Q-01-22`, `Q-01-23`), which already own
the cross-thread-publication, fault-state, power-state, time-base, and
scheduling issues surfaced here. **No new question identifier is created.**
Each owning row named in the four implication tables is annotated `BP-01-end`
in its breadth-pass column so the implication stays visible to synthesis and to
the owning question's question gate.

Annotated rows: `Q-00-06`, `Q-01-01`, `Q-01-02`, `Q-01-04`, `Q-01-05`,
`Q-01-06`, `Q-01-07`, `Q-01-08`, `Q-01-09`, `Q-01-10`, `Q-01-11`,
`Q-01-12`, `Q-01-13`, `Q-01-14`, `Q-01-18`,
`Q-01-19`, `Q-01-21`, `Q-01-22`, `Q-01-23`, `Q-02-01`, `Q-02-02`, `Q-02-04`,
`Q-04-01`, `Q-05-01`, `Q-05-02`, `Q-05-06`, `Q-05-08`, `Q-06-01`, `Q-06-03`.

## Return-review residuals

Seven records reached independent `accept` on their applicable independent gates on
2026-09-04 (`REV-058`/`REV-059`/`REV-057`/`REV-100`/`REV-091`/`REV-092` and
`REV-060`/`REV-061`/`REV-062`/`REV-098`/`REV-082`/`REV-099`), and `INV-01-12`
closed its adversarial chain at `REV-120`. `INV-01-01`, `-10`, and `-11`
subsequently closed at `REV-121`, bringing the accepted original set to eleven.
The return cycles left these
non-blocking observations, each routed to an owning question so it stays
visible to synthesis. None reopens a gate.

| Residual | Origin | Owner |
| --- | --- | --- |
| `INV-00-06` family-kill item 1 has no "required public API absent on the mandatory hosts" limb, so the only kill the track recorded had to be routed through the public-API limb by analogy. | `REV-104` | `Q-00-06` |
| A standing recommendation, repeated by three reviewer groups, that an uninvolved adversarial reviewer re-open `INV-01-01`/`-09`/`-10`/`-11`/`-12`/`-13`/`-14` before synthesis leans on them. | `REV-099`, `REV-113`, `REV-120` | `Q-06-01` |
| A `vmnet` positive control is missing from every arm, so the general `VMNET_FAILURE` is not attributable to the absent entitlement; a future arm needs a host or configuration where `vmnet` can succeed. | `REV-083`, `REV-099` | `Q-01-09` |
| `check.py` in `INV-01-01` still raises a traceback on malformed JSON; `INV-01-12`'s output fallback triggers only on an empty file. | `REV-068`, `REV-089` | future code gates under `Q-01-01`, `Q-01-12` |
| `I12a`/`I12b` have no owning claim or metrics row (frozen in accepted code, an omission not a conflict), and `CLM-01-12-16` is out of numeric order. | `REV-097` | `INV-01-12` owner, at next revision |
| A non-NULL `vmnet` handle without callback success is abandoned at exit rather than stopped. | `REV-062` | `Q-01-09` |
| A zero both-observed count in both arms of a litmus pair yields COMPARABLE, an edge reachable only when `I2` would already fail the run. | `REV-079` | `Q-01-08` |
| Advisory wording: `INV-01-12`'s wall time is stated as 178–250 ms against a 250.8 ms maximum, with a typographic ambiguity in "vCPU 1 644 979"; `SRC-059`'s paragraph ordinals are non-reproducible though the quoted sentences are exact; two `SRC-052` ledger rows both map `CLM-01-01-12`. | `REV-106`, `REV-066` | owners, at next revision of those records and of the source ledger |

Eighteen cross-record implications are recorded above as the track's open
disagreements; none was resolved here, and each remains owned by the question
named in its row. Three groups of cells are explicitly **pending re-run** on
`INV-01-07` and cannot be finalized by this pass: the `KA-05` disposition and
every family's timer dependence; the timer/APIC limb of `KA-08` and the
clocksource/RTC/wall-clock rows of the machine contract; and the input set for
`Q-01-21` and `Q-01-22`. Two further gate states are open rather than adverse:
`INV-01-02`'s register is not closed, and `INV-01-08`'s result,
method-amendment, checker-amendment, and adversarial work remains outstanding
with `KA-06` undisposed; its replication gate passed at `REV-118`.

This reconciliation found no need for a new question identifier. It preserves
all eighteen cross-record implications, adds their missing backlog backlinks,
and includes the five active follow-up records in the track's closure scope.
A final pass must refresh these states after the remaining work is reviewed.
