# A faster KDE desktop: specification

Status: draft 6 (patched after code audit, November 27, 2026), branch `kde-desktop-spec` on `land-kde1-gate` (b39c4393), revised after four
reviews (GPT-6.2 Astra, section 7) and the owner's answers on targets. The plan that carries it out is
[kde-desktop-plan.md](kde-desktop-plan.md). Where this page disagrees with [frame-keyed-plan.md](frame-keyed-plan.md),
[fault-around-plan.md](fault-around-plan.md) or [shared-code-plan.md](shared-code-plan.md) on goals, order or
acceptance, this page holds; their mechanisms stay as written there unless section 5 changes them.

## In one page

- **Goal:** the representative KDE desktop with Chromium clearly faster than `main`, cold first; aspiration a third of
  native or five times QEMU; regressions only as small, unfixable trade-offs the owner accepts.
- **Barriers** (milestones with a performance expectation): **M1** the base (P1 + P3 + caps) lands after its crash is
  fixed, and the owner chooses the next investment (the store by default); **M4** vCPUs execute each other's blocks
  (go / no-go); **M5** the unified code store complete, landed. M2 and M3 are progress on the way.
- **Cadence:** changes pooled; each change runs the tests it concerns; each batch smoke and a 15-minute tripwire; decision
  runs where a step decides; gate, desktop scorecard and standing set only at barriers; stored base runs reused.
- **Debugging:** never decide on a number nobody can explain; debug in proportion to what it would change.
- **Management:** one line per investment, each step on the last one's tip; designs reviewed by another model before
  code, only real defects blocking; assignments end at a named commit; nothing pushed or deleted without the owner.

## 1. Goal

**The representative KDE Plasma desktop experience under natemu, with Chromium as its browser, clearly faster than on
`main` today (`be88e684`).** "Representative" is the whole session, not one application or one benchmark: the session's
start, starting and switching applications, moving and resizing windows, starting browsers, running web pages.

- **Bottom line** (owner, November 26): clearly better than `main`.
- **Aspiration** (owner): above a third of native speed, or above five times QEMU, on the same rows. Section 1.1 shows
  how far today's numbers are from it; the references (KVM, QEMU) are measured when a decision needs them (section 3.1),
  and each barrier says where the rows stand.
- **Cold first:** the cold reading (compile cache off: first starts, nothing kept from an earlier boot) must be decent
  and is the headline. Warm gains (compile cache on) are welcome and reported beside it.
- **Regressions** are acceptable only when they are small, buy something larger, and cannot be fixed: a trade-off,
  never a flawed design. Each one is named with what it buys and why it cannot be removed, and the owner accepts it.

The project reaches the goal in two parts:

1. **The landed base (M1):** P1 (exact frame-keyed lookup, checked links, the code TLB and guards), P3 (shadow
   fault-around) and the disk and answer caps, on `land-kde1-gate`, once its crash is fixed (section 6).
2. **The next investment, by default the unified code store (M2-M5):** one copy of translated code that every vCPU
   executes, in place of one private copy per vCPU. The desktop profile found a warm Konsole start spending about 60% of
   its busy time on natemu's code-cache work (experiment, frame-keyed plan section 1), and 0.33 of that start's 0.55
   vCPU-s of pool and host requests on blocks a peer held live (experiment, shared-code plan section 1). That is one row;
   no browser row has such a measurement. **M1 ends with a decision** (section 2.2): from measurements on the cold rows, the
   store is confirmed as the next investment, or first-seen translation or process-switch work goes first. The owner
   decides.

### 1.1 Where the time goes today, and what it does and does not show

`main` (`b168570a`, the desktop cases on `be88e684`), Mac mini M4, 4 vCPUs, compile cache off, one run
(`desktop-kde-profile` and `-browser-profile`, run `20261126-205447-84bd`, the `perf` resident), in vCPU seconds by the
resident's buckets (`crates/abi/src/execution.rs` 4823-4845 at `b39c4393`): **guest** is all generated code at EL0,
dispatcher and guard stubs included; **host** is every host exit (the Hypervisor.framework exit and entry and all host
work: translation, admission, device I/O); **code cache** is the sum of pool, place, link (resident entries at literal
exits), relink, lookup, miss and enter. KVM numbers are the November 26 reference runs on the x86 box (experiment,
`desktop-kde-kvm`, run folder not kept).

| Row | Wall | KVM | Busy | Guest | Host | Code cache | Shadow, abort | Other |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Konsole warm start | 1.07 s | 0.18 s | 1.60 | 0.38 | 0.18 | about 0.8 | 0.05 | |
| Konsole cold start | 1.63 s | 0.20 s | 2.26 | 0.47 | 0.72 | about 0.7 | 0.13 | |
| Chromium cold start | 8.92 s | 0.44 s | 21.9 | 2.57 | 10.2 | about 5.2 | 0.83 | shootdown 0.7 |
| Firefox cold start | 9.98 s | 0.82 s | 19.1 | 3.16 | 7.09 | about 5.8 | 0.88 | |
| Speedometer (90 s window) | score 2.42 | score 17.4 | 263 | 74.4 | 25.8 | at least 93 (relink 44.6, link 19.1, lookup 15.8, pool 13.2) | 25.9 | tlb 18.5 |
| Session start to idle | 31.2 s | | 75.9 | 30.1 | 13.2 | about 17 | 7.5 | |
| Window grow (3.2 s) | | | 4.88 | 2.45 | 0.19 | about 1.0 | 0.72 | |

What it shows:

- **The gaps to native are large:** 6-9× on application starts, 12-21× on browser starts, 7× on Speedometer. A third of
  native is 3×: below 1.3 s for a cold Chromium start.
- **Each row is dominated by different work.** Warm application starts: code-cache work. Cold browser starts: host exits
  (whose split into first-seen translation, repeat requests and I/O this table cannot give) and code-cache work.
  Speedometer: generated code and relinking. Window frames: generated code.
- **P1 and P3 aim at parts of it** (relink, link, lookup, tlb; shadow and abort), and the first M1 reading moved the rows
  they aim at (section 6); the answer-map cap is in the same commit and its share is not separated.

What it does not show:

- vCPU seconds are not wall time: four vCPUs overlap, and a row's wall time follows its serial stretches.
- How much of the host time is first-seen translation, how much repeated requests, how much is a peer's code (outside warm
  Konsole), how much the process switches cost in the serial stretches.
- Where QEMU stands on these rows (no desktop QEMU control yet).

So this table supports pursuing the desktop rows; it **neither sets a ceiling on the aspiration nor chooses the next
investment**. M1 measures what is missing (plan L5) and the next investment is chosen from it.

**Secondary goal:** the standing benchmarks (`docs/benchmarks.md`) better where the store can move them (programs that
start processes or switch between them) and no worse elsewhere.

**Not goals:** GNOME (one comparison reading only), the JVM, another OS, frame rates beyond what code-cache work costs
them, folding the template pool and the host's answer map into the store (evaluated, deferred: shared-code plan 12a).

## 2. Deliverables

### 2.1 The project

The unified code store on by default on `main`, the per-vCPU code path deleted, with:

- the scorecard (3.1) of the final commit clearly better than `main` and than M1 on the cold rows, against the
  aspiration's references, every worse row an accepted trade-off;
- the standing set (3.2) better or the same on every case, `docs/benchmarks.md` updated;
- the gate passed at that commit; no crash in any scorecard or standing run.

### 2.2 The milestones

Three milestones are **barriers**: each has a performance expectation, and the work stops there for its tests and
numbers. The others mark progress on the way and are pooled into the next barrier (section 3).

| Milestone | What exists at its end | Barrier? | Its numbers | Its decision |
|---|---|---|---|---|
| **M1 Base landed** | P1 + P3 + caps on `main`, the `block_at` crash fixed with a regression test | **yes**: clearly better than `main` on the cold rows | scorecard against `main`; the cold rows' breakdown; standing set in full; `docs/benchmarks.md` | owner approves the PR, and chooses the next investment (the store by default) |
| M2 One store line | the store's chunks 1, 2, 3 and chunk 5's prerequisites on M1, with the fixes section 5 names; behaviour unchanged | no | tripwires (3.3) | none |
| M3 Store in use, private | one lookup table with separate find and execute rules; one link rule; per-vCPU state out of the records; blocks placed in the store with reclamation; the private code rows removed | no | tripwires; decision runs at S2 and S4a | S2: whether the link rule's cost is a trade-off or must be designed away |
| **M4 Shared code** | a vCPU enters a peer's live block; adoptable blocks unlinked in one interim form; guest and host (DMA) writes kill a page's blocks for every vCPU | **yes**: adoption moves the cold start rows | scorecard against M1; adoption on and off on the same layout; adoption counters; the process-heavy standing group | go / no-go: adoptions replace most eligible requests a peer held live |
| **M5 Store complete** | links made once for every vCPU in a form legal for shared code; the per-vCPU path deleted; on by default | **yes**: better again than M1, no standing case worse | scorecard against M1 and `main`; standing set in full; `docs/benchmarks.md` | owner approves the PR: the project's deliverable |

### 2.3 A step

A step (plan L2-L7, I1-I4, S1-S6) is done when, on its line's tip:

1. it does what the plan says, with the tests that concern it through `tools/natemu-test`, each new check shown to fail
   when it should;
2. its design (before code) and its code were reviewed by a model other than its author (Fable when available, GPT-6.2
   Astra through Codex otherwise), and the substantive findings answered in its plan section;
3. its record is in its plan section: what changed, its tests, any decision run's numbers (case, machine, commit, date).

A step is not benchmarked or gated by itself (section 3). Proof effort scales with risk: a step needs the tests that
make it safe to build on; the full concurrent proof (shared-code plan section 9's mutations and stress) belongs to the
steps where peers run shared code (S5, S6).

## 3. Testing and benchmark cadence

Changes are **pooled**: during iteration only the tests and cases a change concerns run; heavier runs come once per batch
or at a barrier. Every number is from a framework case on the Mac mini M4, alone on the machine (AGENTS.md, "Benchmark
numbers"): a stored base run on the M4 with the same cases, inputs and options is reused, never repeated; a case's
known noise is reused; a candidate runs once, again only when its difference is within about twice the noise; a
difference far beyond the noise is explained before going on. A crash in any run is a failure, whatever the numbers.

| When | Tests (any correctness machine) | Benchmarks (the M4) |
|---|---|---|
| **Each change, while iterating** | `tools/natemu-test run` on what the change concerns; new checks shown to fail once | none |
| **Each batch of steps that joins the line** | `--tier smoke` | the **tripwire** (3.3), when the M4 is idle; it never blocks the next step |
| **A decision run** (S2's link-rule cost, S4a's interim exit cost, M1's guard-cost choice, an inconclusive next-investment choice) | — | only the cases that decision needs, against stored base runs |
| **A barrier** (M1, M4, M5) | the gate at the barrier's exact commit (a mosaic of families on several machines is allowed) | the scorecard (3.1); the standing set (3.2): in full at M1 and M5, its process-heavy group at M4 |
| **A number moves unexpectedly, or a crash** | a diagnostic build, on a correctness machine | a profile run of the one case concerned, labelled an experiment |

**Performance debugging.** Never decide on a number nobody can explain, and debug in proportion to what it would change.
When runs come back as expected, nothing extra: the `perf` runs already hold the buckets. When a number surprises, or a
regression appears, its cause is found before anything is decided: usually a bisect of the batch and a profile of the
one case (buckets, then the EL0 sampler and guest `perf`). A regression offered as a trade-off needs its cause known,
since only a cost that cannot be fixed qualifies. Naming a step's new hot-path cost and adding a counter for a new
mechanism are good practice for the author, not gates.

### 3.1 The scorecard

- **At M1 and M4:** the existing desktop cases, `desktop/desktop-kde-profile` (session, idle, UI interactions, Konsole and
  Dolphin starts) and `desktop/desktop-kde-browser-profile` (Chromium and Firefox ESR starts, Speedometer 3.1 in
  Chromium), with their `-warm` variants (the warm browser case runs the starts only, so warm Speedometer is
  unmeasured). Base runs stored on the M4 are reused (at `b168570a`: runs `84bd` and `fd58`, November 26) after plan L3's
  validity checks are applied to them offline; only a run they reject is replaced. These cases fail on a missing row, a
  phase timeout, a failed VM exit, a session that never reaches idle, or a UI action that did not happen (plan L3).
- **Plain-build cases** (new at M1, plan L3): the same phases with the ordinary resident and no request log. They give the
  numbers of `docs/benchmarks.md`'s new **Desktop experience** section, and they confirm the rows that decide a barrier
  (a cold start row that makes "clearly better", or the row a go / no-go turns on), one run a side: the profiling
  build's cost does not fully cancel, since a change that removes translation requests (the store above all) also
  removes their logging.
- **Rows:** each start's wall time (cold, and the second start in the same boot); Speedometer's score. The UI phases'
  frame rates are diagnostic until a presentation counter replaces today's display notifications (capped at one per 50
  ms, so a fall from 60 to 30 presentations per second can read 20 on both sides); busy vCPU seconds are diagnostic.
- **"Clearly better"** (the bottom line): every cold start row (Konsole, Dolphin, Chromium, Firefox, the session) better
  beyond twice its noise, no row worse beyond noise except an accepted trade-off, Speedometer not worse.
- **References** (KVM on the x86 box; QEMU on the M4) place the aspiration. They are measured when a decision needs them
  (the next-investment choice after M1, or M5's report), with one protocol for every backend written then; the existing
  KVM case is reused until then.

### 3.2 The standing set

The bench family's natemu cases behind `docs/benchmarks.md`, each with the metric and vCPU count its table uses (the
harness default is one vCPU; the page also reports four):

| Group | Cases (metric; vCPUs) | The store can move it? |
|---|---|---|
| process- and switch-heavy | `guest-build`, `guest-hackbench` (seconds; 1 and 4), `guest-hackbench32`, `guest-hackbench32-int80`, `guest-startup32` (seconds; 1), `io5-solo32` (configure and make seconds; 4), `guest-paper` (start seconds; 4) | yes: the secondary goal |
| compute | `guest-zstd`, `guest-cray`, `guest-cray-strict`, `guest-7zip`, `guest-sqlite`, `guest-avx`, `guest-luajit`, `guest-luajit-interp`, `guest-locks`, `guest-mem` (seconds; 1 and 4), `guest-scimark` (Mflops; 1), the 32-bit `guest-cray32*`, `guest-sqlite32`, `guest-scimark32*` (1) | no: must not regress |
| 32-bit and segment code | `compat-tls-natemu`, `mixed-far-natemu` (as the page) | no: must not regress |
| graphics and web | `glmark2-natemu`, `glmark2-natemu-sse41` (score; 4), `display-fill` (its timings; 1), `speedometer-natemu`, `speedometer-natemu-warm` (score; 4) | partly (browsers) |

On `docs/benchmarks.md`, M1 moves the page's io5 row (now "context-switch-heavy Debian workload": 522 s against QEMU's
901 s, November 14) into a **Whole-system workloads** section with the 32-bit CPython build (both run by `run_io5.py` on
the Debian cloud image), named by what it does ("Debian cloud image: package installs and a build (io5)") and keeping
its dated numbers; `io5-solo` adds a build to that workload, so it does not replace the row. The new **Desktop
experience** section holds the scorecard rows from M1 on. `debug/web-tooling-profile` stays in the diagnostic
record. In full the set is about three hours on the M4 (estimate).

### 3.3 The tripwire

A short set that catches a large regression close to where it entered: `guest-build`, `guest-hackbench` (4 vCPUs),
`guest-startup32` and `desktop/desktop-kde-profile`, about 15 minutes. One run each against the last barrier's stored
runs, once per batch of steps that joined the line, queued when the M4 is idle. A move beyond twice a case's known noise,
or a crash, pauses new steps; the steps since the last clean tripwire are bisected on the line. Its numbers decide
nothing else and go in no document beyond the plan's record of the trip.

## 4. Project and code management

### 4.1 Roles

- **Owner:** sets the goal, accepts trade-offs, approves PRs and pushes, chooses the next investment at M1.
- **Coordinator** (one session): assigns steps, checks every agent's runs, commits and base each round, integrates,
  keeps the machines busy, reports at barriers with numbers.
- **Authors:** one agent per step, in its own worktree.
- **Reviewers:** a model other than the author's; they may propose better designs. **Only substantive correctness,
  execution or reproduction defects block** (AGENTS.md); editorial points are fixed directly and never block. Every
  review brief says so, and the coordinator triages each review by that rule and by this section 3's cadence.
- **Assignments expire.** Each assignment names its base commit and the tip and evidence that end it, and stops there; an
  agent never starts work the assignment does not name, so a coordinator outage ends work by itself.
- **The record is in the tracked plan:** each step's accepted commit, review and evidence.
- **Machines:** the Mac mini M4 for every number; the M2 Max, M3 and rented M4 for builds and correctness runs; the x86
  box for amd64 builds and the KVM reference.

### 4.2 Code

- **One line per investment.** M1's work goes on the work branch `m1` and lands on `main` through `land-kde1-gate`. The
  store is one work line, `p2-store`, on M1's commit: every step starts from the line's tip and joins it, with its tests,
  before the next step starts; nothing is built in parallel on another base. The line is gated at its barriers (M4, M5),
  not per step.
- **Working branches** are short-lived and join their line at their step's end. Experiments, failure demos and measured
  builds may stay as branches or tags; nothing is deleted without the owner. The inventory of November 26 is in
  `.natemu/archive/` on the coordinating Mac.
- **Landing:** a PR to `main` with the owner's approval; fast-forward, then rebase, then squash, then merge commit, in that
  order of preference; a fixture a test pins by commit stays reachable through the landing; nothing pushed without the
  owner.
- **Docs:** each step's record in its plan section; `docs/benchmarks.md` only from standing-set runs at a barrier.

## 5. The unified code store on the P1 base

### 5.1 What it is

Every translated block, its record and the exact lookup table live in one area that every vCPU maps at the same
address. A vCPU that misses looks the block up there and, if a peer has placed it and the vCPU may execute it (D1),
enters it as is: no host request, copy, relocation or placement. Each vCPU keeps what a CPU keeps: its L1 tables, its
code TLB, its dispatcher and a writable private head (dispatcher, step and tail code), its pending stop. The template
pool and the host's answer map stay behind the store.

Requirements:

- **R1 x86 semantics only.** Nothing depends on the guest OS; TLB rules are the SDM's (4.10).
- **R2 shared code changes only by legal words:** between `b`, `nop` and `svc` (Arm ARM, concurrent modification and
  execution; `shared::patchable`), at a trampoline or an `svc` word.
- **R3 no shared word is written for one vCPU's TLB event;** a vCPU's TLB state lives in its own L1 and code TLB.
- **R4 a write to code kills the page's blocks once for every vCPU,** by the page's generation, for guest stores and host
  DMA alike: pre-store writes on read-only shadows (`written`) clear `CODE_PAGES` before bumping `gen` and posting/kicking;
  post-store rechecks whose store to RAM has already completed (`rechecked` after EL1 RAM-alias stores and windowless host
  DMA's post-copy recheck) issue `fence(SeqCst)` and then **bump `gen` before clearing `CODE_PAGES`** and posting/kicking,
  closing the window where an adopter marks between `clear` and `bump` and caches a stale or torn block in L1.
- **R5 nothing is freed except with every vCPU stopped** (chunk 5's barrier), including a peer that is parked, in a host
  request, or in the middle of a device access. Acknowledged peers spinning on `flush.released` must serve
  `take_shadows()` (preventing a 3-vCPU deadlock against a mid-block peer waiting in `split_begin`, `remap_shootdown`, or
  `mark`), and `mmio::device`'s poll loop must not touch shared store state while `away = 1`.
- **R6 sized from guest RAM and host memory,** with a defined behaviour when even one region does not fit.
- **R7 a block is published only when complete:** relocated, its code synchronized, its sites and per-chunk PC-ownership
  index entry (with release store of the chunk's record count) in place, and the final page check passed, all before the
  L2 CAS. P1 files a record before synchronizing its code (`crates/runtime/src/cache.rs` 1330-1352), which is correct only
  while nobody else can see it.

### 5.2 Decisions of the combined design

Lines are on `land-kde1-gate` unless named.

- **D1 One table, two questions.** P1's `natemu_abi::fkeys` (per vCPU; {32-bit tag, block number}; deletion) and
  chunk 1's `shared::Table` ({28-bit record offset / 16, 12-bit epoch, 24-bit tag}; compare-and-swap; no deletion before a
  flush) become one storage, `shared::Table`'s, keyed by P1's hash. Finding a *template* (to reuse its code at another
  address) and finding a block this vCPU may *execute* are different matches: executing absolute code needs the full
  RIP; a block crossing a page needs both frames as this vCPU walks them; a PIC translation needs its alias,
  canonicality and compat32 checks. System records stay findable; the interpreter's mutable decodes stay private.
  Record offset 0 is reserved, records are 16-byte aligned. P1's hash takes its home from the high bits, `shared::Table`
  from the low: the hash is remixed so that frames 2 MiB apart below 2 GiB do not share homes.
- **D2 One name for a block:** a record offset wherever a block is named across code (lookup slots, P1's link records,
  the guard-miss path).
- **D3 One link rule, starting safe.** Shared-capable blocks link only (1) locally: a direct `b` when every linear page
  and frame pair of the target occurs in the source (the existing `local` predicate, `crates/runtime/src/cache.rs` 3066;
  equal frames alone would admit aliases that later remap apart) and the target is in the same region; and (2) by checked
  links (P1's existing restrictions on top: a position-independent source, which runs at several addresses, links
  directly only where its actual entry RIP and the target's PIC form allow, `crates/runtime/src/cache.rs` 2700-2746 at
  `b39c4393`, with its wider stop and fence check for pages reached by direct links) (into the source's region head's dispatcher slot) everywhere else. Guarded links between
  pages are not used in shared code until a generation-aware design exists: a guard checks the target's address and
  frame in the running vCPU's code TLB, so after the target page is rewritten on the same frame and the code TLB refilled
  by a new block there, an old stub would pass again while its source is live. Step 1d's plain links into global blocks
  go (R3). What this costs is measured at S2.
- **D4 No link records in the shared store.** P1's private link records (`LINKS_VA`) stay private during M2–S3 while
  kills still unlink locally; they do not move into the store's records span (`store::Layout` reserves room only for
  records and chunk tables). Under D3's link rule, shared-code links are never undone before a VM-wide flush
  (shared-code plan section 4 and third review N1/N2), so S6 uses lock-free word patches (`patchable` and the
  `synced`/`stamp` rule) with no link records or inbound lists. Before S6, the combined design writes down concurrent
  patch ownership (which writer may patch which word, and the `synced` rule for every direct edge).
- **D5 Stops inside a guard stub.** A stub (the 48 bytes before `code`) has no guest effect and writes no flags, so a stop
  there resumes at the block's guest RIP through the dispatcher. Tested at every stub word, on a guard hit and a miss.
- **D6 Stops in shared translations** bank the RIP actually entered (from x0 or the private page-base slot, by the
  interrupted word), not the record's; tested on two aliases of the same record, with REP and an interrupt shadow.
- **D7 Flags.** `TAIL` and `GUARDED` are both bit 24: `TAIL` moves, and the synthetic tail record clears `GUARDED`. The
  tail's recovery fix (`tail-owned-recovery`: banking the recovery state before a flush retires its origin) is code, not
  only a test, and comes with its overwrite test.
- **D8 Layout.** Every branch into the head through `head_at` and every PC compare through `head_offset` / `head_pc`,
  P1's new sites included (`guard_words`'s `b GUARD_MISS`, line 446; `returned()`'s `GUARD_MISS` check; `link()`'s
  `in_some_form`, around line 2798); P1's link-span assertion (line 631) rewritten, with `REGION_TABLES_OFFSET` placed
  after `LINKS_OFFSET`. The head keeps chunk 2's 64 KiB `TAIL` slot after `PROBES` (preserving `SLOT_BYTES = 64 KiB` and
  `NATEMU_STORE_TEST`), not `shared-tail`'s 32 KiB split of `SLOT`; `cache::tail()` maps head targets from regions
  `r >= 1` into `TAIL`'s reach (reducing via `head_offset` at region 0's `TAIL_VA`, or placing at `head_at(code, TAIL)`),
  and in S4a reads source words from `store::CODE_WRITE_VA`. Tests force execution in a high region (index 3 of four)
  through a missed guard, a checked link and a tail and probe return; allocating the regions alone proves nothing.
- **D9 Memory.** One budget for the store's backing, each vCPU's private state (P1's tables included), the template pool
  and the answer cap, done with the layout (I2), with the behaviour when it does not fit.
- **D10 VM-wide dispatcher slots.** A computed exit encodes its context's dispatcher slot, so slot numbers are VM-wide
  before any peer executes a block (S5), with opposite acquisition orders, exhaustion, breakpoints and fences tested.
- **D11 Compiler specialization.** A block compiled under host features or a compat32 x87 guess that does not hold for an
  adopter is never adopted by it; the table must not offer the same mismatched block again and again.
- **D12 No explicit link patching in adoptable blocks until S6.** An adopter executes the very words the owner patched,
  so from S4a on adoptable blocks keep their literal exits in one fixed interim form: a checked branch into the dispatcher
  slot where that form is legal, and the unpatched `svc #LINK` (the resident's linker entry) where it is not (a REP
  string's self-exit; a context with no slot; an STI-first block's slow self-exit, which must reach the resident to
  serve a held event). S4a measures this form's cost against linked exits; S5 compares adoption on
  and off on the same layout, so the interim form's cost cannot veto sharing by itself.
- **D14 The first time shared storage fills.** Once `shared::take` returns nothing, refilling spans a peer may be running
  needs the VM-wide barrier: chunk 5's runtime barrier is wired first (plan S3b) and drives store reclamation from the
  first step that places into the store (S4a), tested with a peer running, a peer parked through several epochs and a
  held host reply. The return protocol is `mmio-flush-return`'s (`docs/restructure/shared-code-plan.md` at that branch,
  section 8), which supersedes the earlier branch's. When the private code row goes (S4b), a writable private alias of the
  head stays.
- **D13 Ownership of PCs.** Every PC in the store maps to its record through per-chunk ownership, used by `block_at`, the
  host's audits and symbolization alike.

**Open questions for the steps that meet them** (each answered in that step's design, before its code):

- **S4a, forward progress after reclamation:** once peers compete for the freed store, a requester's retry can find it
  full again; the private allocator treats a second failure after a flush as a bad reply
  (`crates/runtime/src/execution.rs` 265, 417 at `b39c4393`). A reservation or a bounded fallback that tells contention
  from a reply too large to place.
- **S5, generation rollover:** a fetch can mark a page and fault before placing anything, so generations can advance
  without consuming storage; a safe epoch rollover before a label can recur, with a near-wrap unit case.

P3 is independent of the store; linking stays lazy; the private-tail stop is the chosen stop mechanism.

## 6. Where things stand (November 26, 22:00) and what went wrong

| Work | Branch | State |
|---|---|---|
| P1, P3, caps | `land-kde1-gate` (b39c4393) | built, reviewed, gated; **crashes** in KDE desktop runs (below) |
| guard local-skip | `land-kde1-guardcost` | same crash |
| store chunk 1 | `shared-code` (on `main`) | built, unit-tested, used by nothing |
| store chunk 2 | `shared-code-regions` (on chunk 1) | built, tested at two regions (0 and 1, odd last region); nothing placed in the store |
| store chunk 5 | `shared-flush`, `mmio-flush-return` (on chunk 2, one commit behind its tip) | unit schedule, private-cache flush harness and `mmio::ones` host-flush fix; no runtime `shared::Barrier` wiring yet |
| store chunk 3 | `shared-tail` (on `main`) + ten evidence branches | built; stop evidence on `main`'s residents only |
| store chunks 4, 6, 7 | — | not started |

**The crash** (cause confirmed after review 2, plan L1): on the Mac mini M4, `b39c4393` and its diagnostic build halted `BAD_STOP` in 3 of 8 desktop case runs
(`desktop-kde-profile` and `-browser-profile`; one more on the M2 Max passed), the guard-cost version in 1 of 2,
`main` in 0 of 4: an interrupt at an EL0 PC in the code area that
`cache::block_at` attributes to no live block (`crates/runtime/src/execution.rs` 2775). The diagnostic build
`diag-badstop` shows the PC in 48 uncovered bytes directly before a block. Confirmed by `diag-badstop` at `e02b84ea` (a full scan for the owner on failure), run `20261126-215633-c8dc`
(`desktop-kde-browser-profile`): halt words `[0x180001, 0xc1010080fffffff0, 0x800050a55bc0]`: interrupt 1 at a PC
equal to the ELR; **kind 3**, `block_at` found nothing but the scan found the owner; the owner live and `GUARDED`
(flags `0x01010080`), its code 24 bytes long; the PC 16 bytes before its code, in its guard stub. `block_at`'s search
(`crates/runtime/src/cache.rs` 3979-3985 at `b39c4393`) assumes at most one record without code between the search
point and the owner; with a body this short, two system records after it defeat the search. The two earlier halts
(`5a65`, `df40`) came from the first diagnostic (`01ae36a1`), which could not tell a search miss from an unowned PC.

**First scorecard reading** (one run each; `desktop-kde-browser-profile`, Mac mini M4, `b168570a` against `b39c4393`, the
`perf` resident): session start to idle 31.2 → 25.6 s; Chromium cold, warm, third start 8.9 → 8.1, 6.7 → 4.4, 8.3 →
5.1 s; Firefox cold and warm 10.0 → 9.5, 4.9 → 4.7 s; Speedometer 2.42 → 2.99 (±18-24%); busy vCPU time 703 → 521 s. To be
redone on the fixed commit (plan L5).

**What went wrong:** the store's chunks were started on `main` in parallel with P1 and never brought onto it while P1
changed the same layout and functions; no milestone or deliverable was written down; numbers were taken per change
against different bases instead of on one scorecard; when the coordinating session hit its usage limit (15:44), work
continued unsupervised for two hours on a base that will not ship; and a landing candidate passed its gate while
crashing on the very workload it targets, because no desktop run was part of its acceptance. Sections 2-4 answer each.

## 7. Reviews

**Review 1 (GPT-6.2 Astra through Codex, November 26, of draft 1; Fable needed credits).** Six blocking findings, all adopted: the
`block_at` search (section 6, plan L2); find versus execute and publication order (D1, R7); guarded links after a
same-frame rewrite (D3); link patching visible to adopters and VM-wide slots (D10, D12); the tail's production recovery
fix, its flag and PIC RIP (D6, D7); S4 split at the first reuse of shared storage, with real peer capacity flushes and
host DMA in the generation protocol (R4, R5, plan S4). Its SHOULDs adopted: executable high-region tests (D8), the
budget in I2 (D9), the hash remix (D1), the scorecard on the ordinary resident and AGENTS.md's reuse rules (3), the
missing standing cases (3.2), expiring assignments and gated merges (2.3, 4.1). Its note adopted (section 1: the peer
share measured again on M1).

**Review 2 (GPT-6.2 Astra, November 26, of draft 2).** Verdict: revise before execution. Dispositions:

- *B1, the crash diagnosis overstated:* adopted. The confirming run is recorded with its raw words (section 6); L2's
  regression asserts its preconditions; the clean desktop runs are predeclared (plan L2).
- *B2, section 1.1 cannot set a ceiling or choose the next investment:* adopted. Section 1.1 says what the buckets are and
  what the table does not show; M1 measures the cold rows' breakdown and ends with the choice (2.2, plan L5).
- *B3, the scorecard not comparable across backends:* adopted (3.1): one protocol and phase order, frames as
  presentations or unavailable, busy seconds diagnostic, failing cases go red, three base boots for noise, "clearly
  better" defined.
- *B4, S4a's first full:* adopted (D14).
- *B5, the link predicate and the interim exit form:* adopted (D3, D12).
- *SHOULD, the proof sequence:* adopted in the plan (I2, I4, S1, S5).
- *SHOULD, gate and measure the executable that lands:* adopted (plan L0, L4).
- *SHOULD, the standing set specified:* adopted (3.2), with the Debian-workload row's control problem stated.
- *NOTE, the budget's cases:* adopted (plan I2).

**Review 3 (GPT-6.2 Astra, November 26, of draft 3), triaged by section 4.1's rule and section 3's cadence:**

- *The crash fix could miss a second layout* (a record without code at C+24 and real code at C+32): adopted, plan L2
  (search by executable extent; both layouts tested).
- *P1's PIC-alias and STI-first link restrictions missing from D3/D12:* adopted (D3, D12).
- *S4a forward progress after reclamation; S5 generation rollover:* recorded as open questions for those steps' designs
  (5.2).
- *The next-investment choice needs an attribution method and an inconclusive outcome:* adopted in a light form (plan L5:
  attribution by request class and the execute predicate, wall-time ranges, an inconclusive result settled by a bounded
  A/B).
- *Cold launch modes cannot both be measured in one boot; frame counts across backends:* deferred with the references
  (3.1), which are no longer measured before M1 lands.
- *One regression rule for decisions:* adopted (section 3: the noise rule; plan L4: guard-cost left out if inconclusive).
- Editorial (the 6% inference; `display-fill`'s metric; Web Tooling in the diagnostic record; pinned fixtures through
  landing): fixed directly.

**Owner, after review 3:** editorial findings never block (4.1); pool changes for tests and benchmarks, barriers only
where a milestone has a performance expectation, plus a tripwire for regressions (section 3).

**Review 4 (GPT-6.2 Astra, November 26, of draft 4; Fable needed credits):** four findings marked blocking, all adopted
as small changes: peer eligibility counted at request time or marked unknown (plan L5); the profiling build's logging
cost confirmed away on deciding rows (3.1); S2's decision run against its immediate parent (plan S2); UI rows checked for
the action happening, frame rates diagnostic until a presentation counter exists (3.1, plan L3). Its SHOULD (the validity
checks applied offline to the stored base runs) adopted (3.1); its editorial points fixed.

**Code audit of draft 6 (November 27, against the branch code):** fixed five mismatches between the text and the branches and
two soundness gaps in the kill/barrier protocols:
D4 brought in line with `shared-code-plan.md`'s third review and `store::Layout` (no link records in the shared store;
P1's link records stay private during M2–S3); D8 expanded with `REGION_TABLES_OFFSET` after `LINKS_OFFSET`, `returned()`'s
`GUARD_MISS` `head_pc` check, chunk 2's 64 KiB `TAIL` slot over `shared-tail`'s `SLOT` split, and `cache::tail()` mapping
head targets from high regions; section 6 corrected for chunk 2's two-region tests and chunk 5's absent runtime barrier;
plan line 7 changed to reconcile `shared-code-plan.md` across all four branches; plan `I3` scoped to the `mmio-flush-return`
prerequisites with the runtime VM-wide barrier separated into `S3b` ahead of `S4a`; plan `I4`'s four paired-stop observer
sweeps kept as one-time verification rather than commit-pinned tests on `main`; R4/S5 updated to require `bump` before
`clear` on post-store rechecks (`rechecked` and windowless DMA's post-copy recheck); R5/S3b updated to require peer
`take_shadows()` during `while !over(e)` and `away`-safe `mmio::device` polling; and R7/S4a updated to publish the per-chunk
PC-ownership index entry before the L2 CAS.

