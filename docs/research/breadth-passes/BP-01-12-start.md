# BP-01-12-start — integrated-prototype breadth pass

## Metadata

- Breadth-pass ID: `BP-01-12-start`
- Question: `Q-01-12` — Can an approved prototype integrate translation, paging,
  precise exceptions, interrupts, and at least two concurrent vCPUs?
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_prototype_author`

This pass organizes questions and establishes no evidence, finding, or
conclusion. It fixes nothing about how the mechanisms are implemented; it
records what the later method must predeclare and what a reviewer must be able
to falsify.

## Product assumptions

- **What "integrated" has to mean.** The track's prototype clause requires
  translation, paging, exceptions, and interrupts combined with at least two
  vCPUs running concurrently. Five mechanisms demonstrated in five modes of one
  binary, or in five runs, satisfy the words and not the requirement. The
  candidate reading this record proposes for the method to freeze: **one
  process, one build, one positive run**, in which every mechanism is on the
  same critical path as the others — the faulting instruction is a *translated*
  instruction; the fault is delivered through the *guest's own* IDT and its
  handler is itself translated code; the guest's `#PF` handler edits the *same*
  page tables the software MMU walks and its `INVLPG` invalidates the *same*
  software TLB; the interrupt is taken at a boundary of *translated* code and
  its ISR is translated; and both vCPUs are host threads over *one* shared
  guest-physical memory. Composition, not coexistence, is the subject: a
  mechanism set can fail only in combination (an interrupt pending while a
  fault is being delivered; a code page written while the other vCPU executes
  its translation; a `CR3` write while the peer walks the same tables), and
  those combinations are the reason paper feasibility was ruled insufficient.
- **Concurrency must be real, not time-sliced.** A single-threaded round-robin
  scheduler passes any progress-only oracle ("both vCPUs advanced", "the
  counter reached 2N"). The method must therefore predeclare an oracle that a
  round-robin implementation *cannot* pass. Two candidates, and the question of
  whether both are required: (a) a two-party sense-reversing spin barrier,
  which deadlocks under any non-preemptive single-threaded interleaving and so
  is a structural rather than statistical witness; (b) overlapping execution
  windows recorded from both vCPUs against one system-wide monotonic host
  clock, which is statistical but quantifies the overlap. Neither alone is
  obviously sufficient: (a) can be satisfied by a preemptively time-sliced
  single core, and (b) depends on an instrument that itself perturbs the
  measured region.
- **What the prototype is not.** It is not a performance result, not a design,
  not an emulator, and not evidence about any pinned guest. Its guest is a
  synthetic x86-64 assembly program written to exercise mechanisms, so it
  establishes nothing about the `Q-00-02` compatibility boundary, direct boot,
  the `Q-01-13` machine contract, or the `Q-02-03` CPU contract. The code is
  discarded after the track (`experiments/README.md`), which is a reason to
  bound its scope, not a licence to weaken its oracle.
- **Which x86-64 subset suffices without becoming an emulator.** The closure
  rule the method must state: the decoded subset is closed under *what the
  guest program needs to exercise the five mechanisms*, never under what Linux
  needs. Both directions are failure modes. Too small, and the mechanisms are
  exercised on a straw guest — a `#PF` oracle that only ever faults on a simple
  `mov` store says less than one that also faults on a locked read-modify-write
  (which must not perform its read side twice on restart) or on an operand that
  straddles a present and a not-present page (whose restart must not
  double-apply the present half). Too large, and the work becomes
  emulator-building that the code gate cannot review in proportion to its
  value. Every deliberate simplification — flat addressing only, no FPU/SSE, no
  32-bit or compatibility mode, no real ACPI, no device model beyond a
  LAPIC-like register block, no CPUID/MSR surface beyond what the guest reads —
  must be listed with an argument that it does not weaken the mechanism oracle,
  and a reviewer must be able to reject that argument per item. Privileged-state
  surface (`iretq`, `mov cr3`, `invlpg`, `sti`/`cli`, `hlt`) is mechanism, not
  ISA breadth, and is not a simplification candidate.

## Adjacent systems

- Production dynamic translators (QEMU TCG and others) solve the same five
  problems, and their solutions are prior art that may locate a mechanism but
  cannot substitute for observation on this host: `Q-00-03` fixes QEMU TCG as
  the decision baseline for *performance*, and nothing in this record compares
  against it or may be read as doing so. Reading a mechanism out of another
  system's design is neither Source-backed (not a primary specification of this
  host's behavior) nor Observed.
- The host mechanisms the prototype composes are owned elsewhere and are
  dependencies here, not results: `MAP_JIT` and write-protect toggling
  (`Q-01-04`, `KA-01`), guest-physical backing and reservation limits
  (`Q-01-03`, `KA-03`), page-tracking granularity at the 16 KiB host page
  versus the 4 KiB guest page (`Q-01-05`, `KA-07`), fault delivery and resume
  (`Q-01-06`, `KA-04`), host clocks and cross-thread wakeup latency (`Q-01-07`,
  `KA-05`), and Arm-versus-x86 ordering (`Q-01-08`, `KA-06`). If any of those
  investigations later contradicts an assumption this prototype relied on, this
  record reopens rather than overriding them.
- Instruction-set and memory-model facts come from the Intel SDM, AMD APM, and
  Arm ARM; host-API facts from Apple documentation, manual pages, and the local
  SDK headers. This pass cites none of them and settles none of them.

## Negative and boundary cases

- **Precision and restart of `#PF` is not "it did not crash".** Precision means
  that at delivery every prior instruction's effect is visible in guest memory
  and registers and *none* of the faulting instruction's is; restart means that
  after `IRETQ` the faulting instruction completes **exactly once**. The
  observable oracle therefore needs, at minimum: a per-site fault counter; a
  guest memory location whose final value distinguishes zero, one, and two
  applications of the faulting instruction's effect; `CR2` equal to the exact
  faulting linear address; the pushed `RIP` equal to the *start* of the
  faulting instruction rather than the next one; and an error code whose
  present/write/user bits differ correctly between a not-present read, a
  not-present write, and a write to a read-only present page — a translator
  that always reports one encoding is wrong and passes a crude oracle. A/D bits
  add a second observable the guest can read back from its own PTEs: accessed
  set on access, dirty set only on write and only once the fault is resolved.
- **Interrupt delivery is not "the counter moved".** Timer ticks posted by a
  host thread cannot be required equal to guest-observed ticks without a
  predeclared coalescing rule: if the guest is slower than the post rate, ticks
  coalesce, and an oracle demanding equality fails for a correct implementation
  while an oracle demanding only "greater than zero" passes a broken one. The
  method must choose — post-on-EOI (no coalescing, equality is then a real
  invariant) or an inequality with a floor plus "every delivered interrupt was
  EOI'd" — before any run. Boundary cases: `IF` must actually gate delivery
  (no advance inside a predeclared `cli` region, prompt delivery after `sti`);
  a halted vCPU must *wake* on an interrupt rather than spin, which a count
  oracle cannot see and a host-side in-`HLT` time plus wake count can;
  interrupt checks only at block boundaries leave a long or spinning block
  undeliverable-into, so the pathological guest spin loop and the block-length
  bound belong in the method; and the ordering when a fault and an interrupt
  are simultaneously deliverable must be predeclared rather than discovered.
- **Self-modifying code.** The guest overwrites an instruction on a page whose
  translation is already cached, then executes it. Observing only the new
  behavior is insufficient: a translator that never caches passes trivially, so
  a retranslation counter greater than zero must accompany it. Granularity is a
  live boundary: host-page write tracking covers four guest pages at 16 KiB,
  so detection over-triggers relative to the guest's 4 KiB unit — a cost and
  correctness question owned by `Q-01-05`/`KA-07`, exercised here only at
  whatever granularity the prototype picks. The write-into-the-currently-
  executing-block case and the cross-vCPU case (one vCPU writes code the other
  is executing, which x86 subjects to a serializing-operation contract) are
  distinct and much harder; whether either is in scope must be an explicit
  method decision, and if out of scope, an explicit limitation routed to
  `Q-01-05` and `Q-01-08` rather than a silent gap.
- **Negative controls are the code gate's only evidence that the checker is an
  oracle.** Each control must be a single-flag mutation of the *same* binary
  and must produce a *completed* run whose JSON the checker marks FAIL with a
  non-zero exit; a control that crashes, hangs, or exits before the checker
  runs demonstrates nothing about the checker. The controls divide by
  determinism, and the method must classify each one rather than presenting
  them as equivalent: a broken restart (`IRET` resumes past the faulting
  instruction) and a dropped IPI are **deterministic** — they must fail every
  time; a non-atomic locked RMW is **near-deterministic** given a predeclared
  iteration count large enough that a lost update is overwhelmingly likely, but
  it is still an interleaving argument; a removed barrier is **stochastic** —
  arm64 need not exhibit the reordering in any given window, so its
  non-failure is not evidence that the barrier is unnecessary and it cannot be
  the control that satisfies the code gate. At least one deterministic control
  must carry that load. Whether a stale-code-cache control (detection disabled,
  old behavior observed) should be added to witness the SMC invariant is an
  open method-freeze item: an invariant with no control that breaks it is
  unwitnessed and must be declared as such.
- **Every invariant needs a breaking control.** The general rule this pass puts
  to the method: enumerate the invariants, name for each the control that
  breaks it, and record the ones with no such control as unwitnessed rather
  than counting them as passes.

## Host and guest matrix

- Only one host exists for this session: `Mac16,10` (Apple M4, 10 cores,
  16 GiB), macOS 26.6.2 build `25G83`, SDK 26.4, host page size 16384, Command
  Line Tools clang without Xcode. Any Observed result is valid for that host and
  OS alone; `P1` (M1 Pro / Sequoia 15) replication is owed before platform-wide
  use, per the executor guide's incremental replication rule, and the machine is
  the same generation and release line as `INV-00-02`'s `P2` pair but not the
  identical model.
- `INV-00-02`'s predeclared confound binds this record: no `P1`-versus-`P2`
  difference may be attributed to chip generation rather than macOS release, or
  the reverse, and a single-host run cannot even pose the question.
- The guest dimension is degenerate by construction — one synthetic assembly
  program, assembled with `clang -target x86_64-elf`, its `.text` and symbols
  extracted by a stdlib ELF reader because `llvm-objcopy` is absent on this
  host. That toolchain path is itself a dependency on `Q-01-10`, and its
  absence-of-tools note is an observation about this host, not a claim.
- The vCPU dimension is two. `INV-00-02` makes 2, 4, and 8 the acceptance
  topologies and `INV-00-06` makes 1 vCPU diagnostic only; the track's
  prototype clause requires "at least two". Two therefore satisfies the
  prototype clause and establishes nothing about 4 or 8, which is a limitation
  to carry, not a matrix to silently narrow. Guest RAM at prototype scale says
  nothing about the 8 GiB cell owned by `Q-01-03`/`KA-03`.
- Repetition is a matrix dimension too: one successful run is not evidence of a
  mechanism that works. The method must predeclare a repetition count with all
  repetitions required to pass — a flaky integrated prototype is a failure, not
  a censored sample, in keeping with `INV-00-08`'s rule that a hang is absent
  progress rather than an excluded sample.

## Multicore interactions

- Two host threads over one guest-physical memory make every shared structure a
  concurrency question: the page tables the guest edits from its `#PF` handler
  while the peer walks them; the per-vCPU software TLB and what a peer's `CR3`
  write or `INVLPG` must do to it; the per-vCPU code cache and what a peer's
  store into a code page must do to it; and the interrupt-pending state written
  by a host timer thread and by the peer vCPU's ICR write. Which of these the
  prototype actually exercises versus declares out of scope is a method
  decision that must be explicit, because the unexercised ones are exactly
  where composition failures hide.
- **x86-TSO on arm64.** TSO forbids store-store, load-load, and load-store
  reordering and permits only store-to-later-load reordering; arm64 permits all
  four. The message-passing pattern (writer stores data then flag; reader spins
  on flag then reads data) tests precisely the store-store and load-load pair
  that TSO forbids and arm64 relaxes, which is why it is the right invariant
  for a prototype — and also why passing it must not be reported as "TSO is
  preserved". What a passing run supports is narrower: *the emission rule the
  translator used was exercised under real concurrency and the invariant held in
  every one of N predeclared iterations*. Store-buffering, load-buffering,
  IRIW, and the rest of a litmus suite are not run here and are owned by
  `Q-01-08` and `Q-02-02`. Locked RMW is a separate obligation — single-copy
  atomicity plus full ordering, via LSE atomics or an exclusive-monitor loop —
  and its oracle (a shared counter that must equal exactly 2N) is
  interleaving-dependent rather than architectural.
- Host scheduling is uncontrolled. If macOS places both vCPU threads on one
  core, or on E-cores under a thermal or QoS decision, overlap evidence can
  fail even though the mechanism is sound — a **false-negative** mode, not a
  correctness failure, and one the record must name rather than explain away
  after seeing the data. Placement and normalization are owned by `Q-05-06`.
- Cross-vCPU TLB shootdown (the guest's own `INVLPG`-plus-IPI protocol) and
  cross-modifying code are the two multicore mechanisms most likely to be left
  out of a minimal prototype. If they are, the limitation must be stated and
  routed to `Q-01-08` and `Q-01-05`, because a later family claim that leans on
  them cannot lean on this record.

## Alternatives

- **Family boundary.** The prototype instantiates the mechanism set of family
  `F2` (dynamic binary translation with a software MMU). It is one instance of
  `F2`, not `F2` itself: a pass is an existence proof that these mechanisms
  compose in one process on one host, not a finding that `F2` is admissible
  across the required matrix, and certainly not a performance or design result.
  `Q-01-02` owns family admissibility and must not read more out of this record
  than that.
- `F1` (interpretation with a software MMU) shares the paging, exception,
  interrupt, and SMP mechanisms and differs in the execution engine; whether
  evidence about the shared mechanisms transfers is a `Q-01-02` judgment, not
  something this record may assert. `F4` (ahead-of-time translation with
  dynamic fallback) shares the software-MMU set but adds code discovery and
  fallback, which are not exercised here.
- **`F3` is explicitly not screened.** In `F3` the translated arm64 runs at EL1
  inside a Hypervisor.framework VM and the *hardware* MMU implements guest
  paging, so its paging, TLB, and exception mechanisms are different ones
  (stage-1/stage-2 translation, hypervisor traps, `HV` exit paths). A
  software-MMU pass is not evidence for `F3`, and — the more dangerous
  direction — a software-MMU failure would not kill `F3`. No reader of this
  record may treat it as either.
- `F5` (hardware x86 virtualization) is expected killed by `KA-02` under
  `Q-01-01` and is unrelated to this prototype; `F6` (user-mode-only emulation)
  is outside the full-system envelope and is recorded, not screened.
- Alternative *methods* for the same question that this pass considered and
  does not choose: five separately gated mechanism experiments with a written
  composition argument (rejected — the track requires an integrated prototype
  precisely because the composition argument is the thing in doubt); a
  two-vCPU prototype without paging or exceptions (rejected — omits the
  mechanisms whose interaction is hardest); and running the existing Track 01
  mechanism probes back-to-back in one process (rejected — coexistence, not
  composition). The method must record why the chosen shape is the minimum that
  answers the question rather than the maximum the host allows.

## Falsifiers and stop paths

- **Kill assumptions this question tests.** `KA-11` is the assumption this
  record owns: translation, guest paging, precise exceptions, interrupts, and
  two concurrently executing vCPUs compose in one process on the supported
  hosts. `KA-06` (x86-TSO preservation with public instructions while at least
  two vCPUs run concurrently) is co-tested here and in `INV-01-08`, and this
  record can only supply the narrow MP-invariant part described above. `KA-04`
  is co-tested in a specific and load-bearing way: its "if false" disposition
  says explicit-check families survive only if `INV-01-12` shows composition,
  and a prototype that raises and resumes faults by *explicit checks in
  generated code* exercises exactly that branch — the signal/`ucontext` and
  Mach-exception branches remain `INV-01-06`'s. `KA-01`, `KA-03`, `KA-05`, and
  `KA-07` are dependencies consumed here and owned elsewhere; this record must
  not be cited as their test.
- **Failure is not automatically a kill.** `INV-00-06` family kill 3 requires a
  *structural* correctness incompatibility surviving independent replication and
  states that an ordinary implementation defect is not structural impossibility.
  A prototype that fails because it is unfinished, mis-decoded, or under-tuned
  falsifies nothing. Conversely a genuine, replicated inability to compose the
  five mechanisms is a candidate `F2` family kill, and becomes an input to
  project stop input 3 only when every family in the closed `Q-01-02`/`Q-03-01`
  register has a reviewed kill — one surviving family falsifies it.
- **Sequence is itself a falsifier condition.** `BP-00-01-start` records that
  method and code review must pass before the prototype runs; a result produced
  before those gates is not evidence regardless of what it shows. The record
  must show the method and code frozen before any raw artifact exists.
- Specific falsifiers the run can produce: the faulting instruction's effect
  applied zero or twice; a wrong `CR2`, error code, or pushed `RIP`; ticks
  delivered while `IF` is clear, or a halted vCPU that never wakes; IPIs sent
  but not received; a counter that is not exactly 2N; any MP-invariant
  violation; no overlap between the two vCPUs' execution windows; a modified
  instruction that executes with its old behavior; or any negative control the
  checker reports as PASS.

## Measurement failure modes

- **The prototype being slow proves nothing.** No timing value produced here is
  a performance result. Wall time exists to characterize the run for the
  manifest, has no decision threshold, and may not enter `Q-00-04` aggregation,
  `Q-04-01` cost modelling, or any comparison with the `Q-00-03` baseline. The
  record must say so, because a small hand-written translator will be slow and
  the temptation to read that as a family signal is exactly the error
  `INV-00-06`'s maturity prerequisite forbids.
- **The prototype passing proves mechanism viability on this host only.** It
  does not establish the mechanisms at guest scale, at 4 or 8 vCPUs, on `P1`,
  on a real Linux guest, or under a real device set — and it establishes no
  correctness property of any future emulator.
- **Oracle blindness.** An invariant that holds regardless of implementation is
  not an oracle; a pass on such an invariant is decoration. The controls are the
  audit of this, which is why their determinism classification matters.
- **Absence of a violation is weak evidence.** For the memory-model and
  atomicity invariants, "no violation in N iterations" is the honest statement,
  with N predeclared; "TSO preserved" and "atomicity guaranteed" are not
  available from this method at any N.
- **Instrument perturbation.** The overlap evidence is collected by an
  exit-to-host from inside translated code, which serializes the vCPU at the
  measurement point and is chosen by the translator; the windows measured are
  therefore instrumented execution, and they must be wide enough that the exit
  cost is negligible relative to the claimed overlap. The clock must be a
  single system-wide monotonic source comparable across threads — a property
  owned by `Q-01-07`, consumed here.
- **Coalescing and rate mismatch.** Any interrupt-count comparison without the
  predeclared coalescing rule can be made to pass or fail by choice of post
  rate; freezing that rule before data is the only defense.
- **Post-hoc oracle adjustment.** Every invariant, threshold, iteration count,
  repetition count, exclusion, and stop criterion is frozen in the method before
  the evidence run; discovering after the run which invariants "should" have
  applied converts the prototype from evidence into narrative.
- **Environment drift.** Host build, SDK, clang version, and git revision are
  captured per run, because a mechanism claim tied to `MAP_JIT`, page size, or
  scheduler behavior is only as reproducible as the environment record.

## Track 00 residuals and implications owned by `Q-01-12`

| Inherited item | Origin | Disposition here |
| --- | --- | --- |
| None of the twelve open cross-record implications in `BP-00-end` is assigned to `Q-01-12`. | `BP-00-end` product/architecture/correctness/methodology tables | Checked row by row; nothing to carry. Stated so a reviewer need not re-derive it. |
| The mandatory feasibility prototype combines translation, paging, exceptions, interrupts, and at least two concurrently executing vCPUs; `Q-01-12` owns the integrated capability question; method and code review must pass before it runs. | `BP-00-01-start`, "Multicore interactions" | Carried in full: it fixes the "integrated" reading above and the gate sequence in "Falsifiers and stop paths". |
| `Q-01-12` is a named required accepted input freezing "accepted licensing, public-API, entitlement, and distribution constraints"; if absent, failed, or reopened, no licensing/platform kill is allowed unless a separately accepted universal fact applies. | `INV-00-06`, dependency table | Carried: the investigation must record the exact API and signing profile the prototype used — public APIs only, ad-hoc signing, no restricted entitlement — so `Q-00-06`'s input is satisfiable, and must not overstate it as a platform-wide fact. |
| Acceptance topologies are 2, 4, and 8 vCPUs; 1 vCPU is diagnostic. | `INV-00-02`, `INV-00-06` | Carried as a stated limitation: two vCPUs meets the prototype clause and covers neither 4 nor 8. |
| The `P1`/`P2` predeclared confound bars generation-versus-release attribution until a reviewed crossed pair is measured. | `INV-00-02` | Carried in the host matrix; a single-host run cannot pose the question. |
| The `Q-01-12` backlog row already records `Q-02-01` as surfaced. | `question-backlog.md` | Retained: the prototype's invariant vocabulary is an input to the `Q-02-01` correctness surface, not a substitute for it. |

## Required coverage

| Dimension | What this pass examined | Owners |
| --- | --- | --- |
| Product assumptions | The composition-versus-coexistence reading of "integrated"; real versus time-sliced concurrency; the closure rule bounding the x86-64 subset; what the prototype is not. | `Q-01-12`, `Q-00-02` |
| Adjacent systems | Prior-art translators as locators rather than evidence; the six host mechanisms consumed as dependencies. | `Q-00-03`, `Q-01-03`–`Q-01-08` |
| Negative and boundary cases | Fault precision, restart-exactly-once, error-code and `CR2` discrimination, A/D bits, straddling operands, faulting RMW; `IF` gating, `HLT` wake, coalescing, block-boundary delivery, fault/interrupt ordering; SMC granularity, in-block and cross-vCPU writes; control determinism and the unwitnessed-invariant rule. | `Q-01-05`, `Q-01-06`, `Q-01-07`, `Q-05-01` |
| Host and guest matrix | Single-host applicability, `P1` replication owed, degenerate synthetic guest, two-vCPU limit, prototype-scale memory, repetition as a dimension. | `Q-05-02`, `Q-01-03`, `Q-01-10` |
| Multicore interactions | Shared page tables, per-vCPU TLB and code cache, TSO scope limit and the MP invariant, locked RMW, uncontrolled placement as a false-negative mode, shootdown and cross-modifying code as likely omissions. | `Q-01-08`, `Q-02-02`, `Q-05-06` |
| Alternatives | `F2` instantiated but not established; `F1`/`F4` shared-mechanism transfer left to `Q-01-02`; `F3` explicitly not screened in either direction; rejected alternative method shapes. | `Q-01-02`, `Q-03-01` |
| Falsifiers and stop paths | `KA-11` owned, `KA-06` and the explicit-check branch of `KA-04` co-tested, `KA-01`/`KA-03`/`KA-05`/`KA-07` consumed; defect versus structural impossibility; family kill 3 and project stop input 3 requiring a closed register; gate sequence as a falsifier condition. | `Q-00-06`, `Q-01-02`, `Q-01-06`, `Q-01-08` |
| Measurement failure modes | Slowness proves nothing and may not enter any aggregate; a pass is host-local; oracle blindness; absence of violation at predeclared N; instrument perturbation; coalescing rate choice; post-hoc oracle adjustment; environment drift. | `Q-00-04`, `Q-04-01`, `Q-05-01`, `Q-05-08` |

## Backlog updates

Every issue surfaced above maps to an existing backlog question. Each was
checked against the Scope, Platform, Correctness, Architecture, Performance,
Benchmarking, Integration, and Deferred-security tables before this pass
declined to create an identifier; in particular, cross-vCPU code-cache
coherence sits inside `Q-01-05`'s "tracking modification of guest memory,
including self-modifying code" together with `Q-01-08`'s "invalidation and
cross-vCPU events", and negative-control adequacy sits inside `Q-05-01`'s
method scope alongside the control fixture `BP-00-end` already routed there.
No new question identifier is created, and the reserved Track 01 range
`Q-01-27`–`Q-01-29` is returned unused.

The following existing rows carry an issue from this pass and are annotated
`BP-01-12-start` in their breadth-pass column so it stays visible to synthesis
and to each owning question's question gate:

| Row | Issue routed to it |
| --- | --- |
| `Q-01-02` | The prototype instantiates `F2` only; `F3` is screened in neither direction, and `F1`/`F4` transfer is a family-register judgment. |
| `Q-01-05` | Host-page (16 KiB) versus guest-page (4 KiB) tracking granularity, and cross-vCPU code-cache coherence if the prototype declares it out of scope. |
| `Q-01-06` | The prototype exercises the explicit-check branch of `KA-04`; the signal/`ucontext` and Mach-exception branches remain unexercised by it. |
| `Q-01-07` | Cross-thread comparability of the monotonic clock used for overlap evidence, and the timer post-rate/coalescing rule. |
| `Q-01-08` | The TSO claim is limited to one MP invariant at a predeclared N; the litmus suite, cross-vCPU shootdown, and cross-modifying code stay owned here. |
| `Q-02-01` | The prototype's invariant vocabulary is an input to the correctness surface, not a substitute for it (already recorded on the `Q-01-12` row). |
| `Q-05-01` | Negative-control adequacy: stochastic controls cannot satisfy the code gate, and an invariant with no breaking control is unwitnessed. |
| `Q-05-02` | Replication coverage owed: `P1`, and the 4- and 8-vCPU acceptance topologies the prototype does not reach. |
| `Q-05-06` | Uncontrolled P/E-core placement is a false-negative mode for overlap evidence. |
