# Test framework refactor: requirements

Status: requirements, frozen November 17, 2026 (v1). This document says what the new test framework must do, not how to
build it. Each requirement is marked **[owner]** when the project owner stated it, **[proposed]** when it was filled
in during the discussion and still awaits the owner's review, or **[review call]** when an independent review raised
it and it was settled by the author without asking (the owner may still overrule). Open questions are listed at the
end.

## Why

The current framework is `tools/run_suite.py` (about 185 steps) over about 75 runner scripts (`tools/run_*.py`, about
30k lines), plus cargo tests, 40 Python test files, Linux workload campaigns (`tools/correctness/`) and benchmarks
(`tools/bench/`). The owner's problems with it:

1. **Disk use keeps growing.** `natemu-raw` holds inputs, caches, suite outputs and one-off study outputs in one flat
   folder (84 GB and 1,015 folders on the M4, whose disk is 92% full; 16 GB here). Nothing knows what is still needed,
   so cleanup is done with hand-written scripts. Some inputs are symlinks into other runs' folders.
2. **Caching is poor.** Three separate SQLite caches (assembler, QEMU reference runs, reference results), each wired
   into runners by hand, with no size limit or eviction, and only usable when `natemu-raw` exists.
3. **Everything depends on one machine's `natemu-raw`.** Even the compiler under test is found by reading a path out
   of `docs/restructure/boot-handoff.md`.
4. **Quick debug runs get blocked by long suites.** One machine-wide lock (`.vm.lock`), held exclusively for the whole
   serial phase.
5. **Fast iterations sometimes run the full suite.**
6. **Images are large.**
7. Multiple agents, in different worktrees, submit runs without knowing about each other.

## Goals and non-goals

Goals: every requirement below.

Non-goals:

- Rewriting the tests themselves. Test logic, harness code and judges move over; their assertions do not change
  unless a requirement forces it.
- Keeping or migrating old evidence folders cited by docs. **[owner]** They will be retired separately.
- The design of natemu's own launch configuration. **[owner]** It is natemu's (`docs/configuration.md`); the framework
  only consumes it.

Anti-goals (would make the refactor a failure even if it technically works):

- A framework that reinvents what well-tested tools already do. **[owner]**
- A test that silently does not run, or counts as passing when it did not run.
- A shared machine, folder or service that every run depends on.

## Vocabulary

- **Test**: anything that passes or fails. **Benchmark**: anything that produces numbers. **[owner]**
- **Deterministic test**: compared against a frozen reference answer. **Judged test**: a function evaluates the run. **[owner]**
- **Input**: what a test runs: a program corpus, a guest image, a disk.
- **Answer**: the reference result a deterministic test is compared with.
- **Seeding**: producing an answer by running a reference (QEMU or real x86 hardware).
- **Lock**: the committed record of derived hashes (inputs and answers).
- **Harness**: shared code that runs one kind of workload in natemu and collects what came out (programs, multi-vCPU
  programs, boot to serial milestones, a Linux guest campaign).
- **Claim**: what a test needs from the machine while it runs.
- **Host requirement**: what a test needs from the machine's hardware (a CPU feature such as FEAT_AFP).
- **Variant**: one expansion of a test over a set of values (AFP on/off, FP fast/accurate, vCPU count).
- **Run**: one request to execute a selection of tests.

## Requirements

### R1. Machine independence and data

- R1.1 Tests and benchmarks run on any supported Apple Silicon Mac. No run depends on a particular machine, on the
  M4's `natemu-raw`, or on any shared folder or service. **[owner]**
- R1.2 No stored data: no Git LFS, no shared bucket, no copied folders. **[owner]**
- R1.3 A machine builds every input it needs the first time it needs it: from generators and recipes in the repo, or
  by downloading a public artifact pinned by hash. **[owner]**
- R1.4 Every input must be buildable on any runner machine. Only answers may come from seeding. **[proposed]** (This is
  why the Linux campaigns' x86-built guest programs block their onboarding; see R12.)

### R2. Seeding, answers and the lock

- R2.1 For tests, QEMU and the remote x86 machine are used only to seed. **[owner]** (Benchmarks may still run QEMU or
  native baselines; see R3.7.)
- R2.2 Seeding applies only to deterministic tests. **[owner]**
- R2.3 Seeding is done by whoever adds or changes a deterministic test, before committing. The answer's hash is
  recorded in the lock; the answer itself is not stored. **[owner]**
- R2.4 A runner checks natemu's result against the locked hash and never needs QEMU or the x86 machine. It seeds only
  when someone wants the reference output itself, for example to see which register differs after a failure. **[owner]**
- R2.5 Hashes are derived by a tool, never written by hand in test declarations. The one exception is the pin of a
  downloaded artifact. **[owner]**
- R2.6 Hashes live in a lock file separate from the declarations. **[owner]** One lock per test family, next to its
  declarations. **[proposed; one root lock is the alternative]**
- R2.7 Each lock entry records a fingerprint of what produced it, made only from what is in the repo and only from
  what determines the answer: the input's recipe and generator sources, the guest-visible configuration (R2.12), the
  normalizer, and the pin of the reference tool (the QEMU build, the x86 harness). Claims, tags, timeouts and other
  scheduling fields are not part of it, so editing them never makes a lock stale. A test
  whose fingerprint no longer matches reports "stale lock" instead of running, and a check before commit catches the
  same. Tools installed on a machine (its assembler, its image tools) are never part of the fingerprint; they are
  recorded for diagnosis only. **[proposed]**
- R2.8 The lock changes only through an explicit lock or re-seed command; the change is reviewed in a commit.
  **[proposed]** Because hashes are opaque, each entry also records how it was seeded (reference tool and pin, machine,
  date), and the pre-commit check flags a family where many distinct cases share few answer hashes (a normalizer that
  throws information away) and an answer seeded from anything other than its declared reference. Answers are reseeded
  independently from time to time to confirm they reproduce. **[proposed]**
- R2.9 Results are normalized before hashing (for example, RFLAGS bits that x86 leaves undefined are masked per case).
  The normalization is versioned and part of the fingerprint. **[proposed]**
- R2.10 A failure to reproduce a locked input or answer is reported as a seeding/build failure, never as a test failure.
  **[proposed]**
- R2.11 Answers may be a single value or an allowed set (litmus outcomes). **[proposed]**
- R2.12 By default a reference answer depends only on what the guest is given. In natemu's configuration that is
  `[machine]`, `[boot]` and the device lists, so variants over `[engine]` and `[debug]` settings share one reference
  answer. A family may override this for a setting that does change what the guest sees (R2.13). **[proposed]**
- R2.13 Which cases apply to a variant is the test family's rule, not the framework's. **[owner]** New host
  features and engine settings will need their own treatment (for example, further levels of FP fast mode), so the
  framework hard-codes none of it. A family supplies, next to its tests, a rule that decides for each case and
  variant whether it applies; a case that does not apply is excluded with a named reason. The framework only requires
  that every exclusion carries a reason, is reported with a count, and is never silent. The set of cases each variant
  excludes is itself locked, so a bug in a rule (a feature detected wrongly, a tag applied too widely) shows up as an
  exclusion change instead of a quiet pass. **[proposed: the mechanism]**

  A variant checks only correct behavior; it never checks a known-wrong result. Testing a documented wrong behavior
  (for example, that fast mode without AFP deviates exactly as documented) is the job of a standalone test that
  targets that feature or architecture, not of a variant of a general test. **[owner]** Such a test is placed by its
  host requirements like any other (R5); whether it requires the feature really absent is its writer's call.
  **[proposed]**

  The FP family's rule today, as an example of such a rule. **[owner]** Cases known to deviate in fast mode on a host
  without AFP are not run there; accurate mode runs on hosts with and without AFP. **[proposed]** The exclusion's
  reason is the deviation's name in `tools/fp_deviations.py`; every case that runs must match the reference exactly;
  today's offline-model judgement of deviated results leaves the variants, and becomes a standalone test if it is
  kept. A host with AFP hidden counts as a host without AFP,
  for this rule and for coverage (R5.7), unless the requester asks for real absence (R5.4a). Deviation tags are
  reviewed and each names a documented deviation; they are never generated from observed failures, so the exclusion
  cannot hide a new regression.
- R2.13a Making sure admitted cases are right is the test writer's job. Where QEMU is known to depart from x86 (i8b's 7
  known departures, the QEMU departures x3 judges with an SDM model today), the writer seeds those cases from the x86
  reference instead, so every admitted case has a strict answer. **[owner]** References can therefore differ per case
  within one family, and each case's answer records which reference produced it (R2.8). For system-level cases (APIC,
  interrupts) the writer decides what an x86 reference means, for example KVM on the x86 machine versus bare metal.
  **[proposed]**
- R2.14 The unit of a deterministic result is one case (one program of a corpus, one LTP entry), not one runner step.
  The lock holds one answer hash per case, so a failure names the failing cases without seeding; seeding is needed only
  to show what differs inside a case. **[review call]**
- R2.15 Inputs come in two kinds. **[proposed]**
  - *Answer-bearing inputs* are the exact bytes a reference ran: the machine code of a program corpus. The answer is
    only valid for those bytes, so they must rebuild identically on every machine; their output hash is in the lock.
    What is checked is the built bytes against the locked hash. The tools that build them (the assembler) are recorded
    for diagnosis, not pinned up front: a machine whose bytes differ gets a build failure naming the tool (R2.10), and
    that tool is then pinned as a hash-pinned download. Checked on November 17: `gen_i8.py --seed 20261030 --random 60` produced byte-identical programs on
    this M2 Max (Homebrew clang 23.1.5) and the M4 (Apple clang 21.0.0), matching `i8-corpus-20261030-01`.
  - *Environment inputs* are what a test runs in: kernels, initramfs and disk images. They need not be byte-identical
    across machines or rebuilds (timestamps, file system layout). They are cached per machine (the cache key may include
    local tool versions) and never have an output hash in the lock. An answer that depends on one (a campaign's
    per-case exit codes) is keyed by the recipe and its pinned inputs only, never by a machine's local tools, so it
    holds on every machine.
  - Guest programs that a reference ran (the campaigns' test binaries, such as stress-ng) are built software, not
    corpora: they are built from pinned upstream inputs with a toolchain pinned by digest, and cached per machine like
    environment inputs. Their hash is recorded in the cache entry on that machine, never committed to the repo and
    never required to match an earlier build. **[owner]** (revised November 19, 2026; it read "their bytes are locked,
    or they are built with a toolchain pinned by digest", which was taken to mean a hash in the repo)
  - *Locked* means recorded in a family's `tests.lock`, committed and reviewed: answers, input fingerprints and the
    corpora's program hashes. *Cached* means kept per machine and rebuildable at any time: everything built from a
    recipe, with its hash recorded for diagnosis only. Upstream downloads carry a pin (R14.1); built software and
    images never do. **[owner]**
- R2.16 A corpus whose generator no longer reproduces it is regenerated with the current generator and reseeded, and
  the lists tied to its cases (known QEMU departures, FP deviation tags) are re-derived. Old corpora are not kept as
  stored data (R1.2). **[review call]** No new input or answer needs to match any existing corpus in `natemu-raw`; the
  new framework starts from its generators. (`i8-corpus-20261030-01` happens to be a current-generator corpus; its
  setup notes that the original pre-review generator is unavailable.)

### R3. Tests and benchmarks

- R3.1 Every test and benchmark is declared: its inputs, harness, VM configuration, judge or answer, claims, host
  requirements, variants and selection tags are data on the test, not lists elsewhere. Today's `SERIAL_WHY`, `E_OK`,
  `EST`, `SHARDS` and `QUICK` tables in `run_suite.py` disappear. **[owner]**
- R3.2 A judged test's tolerances, repeat count and rerun policy are part of the test. Every attempt is recorded,
  including reruns. **[proposed]**
- R3.3 Judges and other helpers live next to the declarations of their family. Shared machinery lives in one library.
  **[owner]**
- R3.4 Variants are declared once and expanded by the framework, with stable names such as `i8[afp=off]`. No
  duplicated test entries with skip conditions. **[owner]**
- R3.5 The VM configuration of a test uses natemu's own configuration format and keys. The framework does not define a
  VM schema of its own; it reads only what it needs for scheduling (vCPU count). **[owner]**
- R3.6 Benchmarks are compared only on the same machine. **[owner]** No ratio, delta or ranking is ever computed
  across machines; a report covering several machines groups benchmark numbers by machine. **[proposed]**
- R3.7 Benchmark baselines (QEMU or native) may be cached. **[owner]** They are keyed by machine identity, builds and
  benchmark bytes, reused only while the drift canary (`tools/bench/canary.py`) holds, and an A/B comparison always
  runs both sides on one machine. A comparison against a reused baseline cannot claim a difference smaller than the
  canary's tolerance (5% today); smaller ones need both sides measured in the same session. **[proposed]**
- R3.8 Rust tests, Python tests, VM runners, Linux campaigns and benchmarks all fit this model. Rust and Python unit
  tests keep their own runners, but each test still appears as its own result. **[proposed]**

### R4. Claims and machine sharing

- R4.1 Some tests run alongside others; some need exclusive machine access. **[owner]**
- R4.2 Each test declares what it needs from the machine: a CPU budget (derived from the VM's vCPUs and any workers the
  harness starts), dedicated performance cores, or exclusive use of the machine. Exclusive tests state why. A budget
  larger than a machine's performance cores is a placement requirement: on that machine the test is not runnable
  (R5.4), never left waiting forever. **[proposed]**
- R4.3 macOS cannot pin threads to cores. **[owner]** A CPU budget is accounting: the machine never grants more than
  its performance cores at once. "Dedicated" means the budget is reserved and nothing is granted past it while the test
  runs.
- R4.4 Claims hold across every run on the machine, from any worktree or agent, and are released when a test ends or
  its process dies. **[proposed]**
- R4.5 Quick runs are not blocked by long ones. **[owner]** Neither extreme works: taking exclusivity per test makes
  every exclusive test pay for draining the machine, so with several contenders nobody makes progress fast; holding it
  for a whole phase blocks everyone else for the phase. **[owner]** Exclusive work is therefore grouped into
  **exclusive windows** **[owner]**:
  - Exclusive tests from every run on the machine wait in one queue. Once the machine has drained, a window opens and
    they run back to back, so one drain serves many exclusive tests. The queue is ordered by priority, then age, so a
    quick run's exclusive test does not wait behind an older gate's. **[proposed]**
  - A window closes when the queue is empty or its time budget is spent; shared work then resumes. Nothing else runs
    inside a window, preparation included (R14.4). **[proposed]** The budget is configurable (around 10 minutes to
    start), and a single exclusive test longer than the budget gets a window of its own. Between two windows the
    machine guarantees a minimum interval for shared work, so shared tests longer than the backfill horizon are not
    starved when exclusive work keeps queueing. **[proposed]**
  - A quick run's shared tests wait for at most the window in progress plus its drain; the drain is as long as the
    longest shared test already running, and an exclusive test also waits for VM processes started outside the
    framework (R4.9). **[proposed]**
  - While a window is pending, the scheduler backfills: it starts only shared tests expected to finish before the
    drain would complete anyway (from recorded durations, R6.5), so short work keeps flowing and long tests do not
    delay the window. **[owner]**
- R4.6 Within one priority, claims are granted first come, first served, so a large claim is not starved by small
  ones. **[proposed]**
- R4.7 During the migration, old tools that use `.vm.lock` or `lockf` and new runs exclude each other. **[proposed]**
- R4.8 Ordinary tests, with no timing or performance requirement, may run on the efficiency cores. **[owner]** A test
  with a time bound, a performance measurement or truly concurrent vCPUs stays on the performance cores; that is
  declared on the test (R3.1), replacing today's `E_OK` list. **[proposed]**
- R4.9 An exclusive test also waits until no natemu or QEMU process started outside the framework is running, as the
  serial phase does today, and the status view (R7.3) names what it is waiting for. **[review call]**

### R5. Host requirements and placement

- R5.1 Machines have different chips (M2, M3, M4, ...). **[owner]** Host requirements name features (FEAT_AFP), not
  chips. Features, chip, core counts and machine identity are detected on the machine, not written down.
- R5.2 A test may require a feature present or absent. Whether a host that has a feature may stand in for one that
  lacks it by hiding it, and how it is hidden, is declared with the feature or by the test, not built into the
  framework. Hiding goes through natemu's configuration once it offers it. Until then the framework uses today's
  test-only switch, `NATEMU_FEXC_FEATURE_OFF=afp` (`crates/host/src/admission.rs`), so an M4 can cover the no-AFP
  combinations meanwhile. **[proposed]**
- R5.3 Capable machines run all; incapable machines run only what they can. **[owner]** A machine that has a feature
  runs every variant, including the ones that need the feature absent (by hiding it, where the feature can be hidden).
  A machine that lacks it runs only the variants that do not need it. **[proposed: the hiding detail]**
- R5.4 A variant that no machine in the run could run is reported as not runnable, with the reason (for example
  "needs AFP"). It is never silently dropped and never counted as passing; whether its absence leaves a gate
  incomplete is R5.7's coverage rule. A variant that ran on some machine is not reported as not runnable on the others.
  **[proposed]**
- R5.4a Deciding when a change needs a variant on a particular kind of machine is the requester's job: a run may pass
  required features to the runner (for example, an FP change asking for AFP really absent rather than hidden), and
  the runner then places variants accordingly. **[owner]** Without such a request, a variant runs once, on a machine
  that can run it, preferring one where the requirement holds for real; tests declared "every chip" (R5.6) run on all.
  **[proposed; the owner has not decided once versus every capable machine]** The exact options (a feature really present
  or absent, every capable machine) are left to the builder. Requested features that no available machine provides
  make those variants not runnable here (R5.4), not silently relaxed. **[proposed]**
- R5.4b The split of responsibilities: the requester (a person or an agent) decides what to run, including tests,
  tiers and required features; the scheduler only makes sure the request is dispatched correctly (placement, claims,
  coverage reporting). The scheduler never widens or narrows a request on its own. **[owner]**
- R5.5 Every result records the machine, the chip and how each host requirement was met (real or hidden).
  **[proposed]**
- R5.6 Some tests run once on every chip (host-compatibility tests). **[proposed]**
- R5.7 Coverage across host kinds is required. **[owner]** A family declares which combinations of host features and
  settings a gate must cover (for FP today: accurate mode with and without AFP, fast mode with and without AFP). A gate
  run reports, per test, which required combinations ran, on which machine and whether a feature was real or hidden; a
  gate that missed one is incomplete, not passed. Coverage may be combined across runs of the same tested commit, so
  machines used one after another can complete a gate together. **[proposed]**
- R5.8 A gate passes only when every selected test passed: a stale lock, a seeding or build failure, an unpreparable
  resource or an unexpected change in a family's exclusions (R2.13) each mean the gate did not pass, and the report
  says which. **[review call]** A test this machine cannot run (not-runnable: too few cores, a missing feature) does
  not pass silently either: unless another machine's run covers them (R5.7), the gate ends incomplete, lists them loudly with the ways
  to close it and their risk, and passes without those results only on a person's explicit decision (no prompt: a later command). **[owner, November 19]**

### R6. Selection and iteration

- R6.1 A fast iteration never ends up running the full suite. **[owner]**
- R6.2 Runs can be selected by tier or class, by family, by test or variant name, and by tags such as kind or claim.
  **[owner: tiers or classes]**
- R6.3 Proposed tiers: `smoke` (one small case per harness and family, a few minutes), *affected* (smoke plus the
  families covering the changed files: a selection helper the requester may use, not something the scheduler decides,
  per R5.4b), `gate` (everything except campaigns and benchmarks;
  required before a push or merge), and `campaign` / `bench` (only when named). **[proposed]**
- R6.3a Unit tests are cheap, so every tier runs all of them: all Rust tests and all Python tool and model tests.
  **[owner: Rust tests run in full if they take a few minutes at most]** (On the November 14 quick run: `cargo test` 7 s
  and `cargo test` with the compiler 3 s after the build, the Python tests 54 s.) Change-based selection is therefore
  only about which integration (VM) tests a code change concerns; that mapping is the real problem. **[owner]**
- R6.3b Each integration family declares the source paths it covers (path rules, as in GitHub Actions' `paths`
  filters); *affected* picks the families whose rules match the changed files. **[owner: declared path rules first]**
  A changed file no rule claims is listed for the requester rather than guessed at, and a change to shared machinery
  (the framework, a harness) selects every family's smoke tests. **[proposed]** A coverage-measured map is not
  pursued for now: its soundness and feasibility are uncertain. **[owner]**
- R6.4 The full gate runs only when explicitly asked for. **[proposed]**
- R6.5 Long tests start first, using durations recorded from earlier runs, not hand-written estimates. **[proposed]**

### R7. Many agents, many worktrees

- R7.1 Several agents in different worktrees submit runs without knowing about each other. **[owner]**
- R7.2 Each run tests its own worktree's code; nothing from another worktree's build can leak into it. **[proposed]**
- R7.3 A waiting test says why and behind what (test, worktree, expected time left). A status command shows the
  machine's whole queue: running and waiting, by worktree and priority. **[proposed]**
- R7.4 A run survives the end of the command or agent turn that started it: it gets an ID, keeps going, and its result
  can be checked later. **[proposed; see open question 2]**
- R7.5 Identical work is not repeated: a deterministic test whose binaries, inputs, VM configuration, variant, chip,
  way each host requirement was met (R5.5), harness and normalizer sources, and macOS build match a finished run
  reuses that run's normalized output, which is compared again against the current lock, so a reseeded or tightened
  answer is never satisfied by an old verdict. One that matches a running test waits for its output. Tests whose
  output is not fully determined by their inputs (multi-vCPU, allowed-set answers) are never reused. This is the one allowed exception to R5.4b: the requested test is still answered, the
  report marks the result as reused and from which run, and a requester can ask for a fresh run. Judged tests and
  benchmarks are never reused. **[proposed]**
- R7.6 One run cannot occupy a machine indefinitely at the expense of others (priority classes; possibly a share cap).
  **[proposed]**

### R8. Artifacts, caches and disk

- R8.1 Disk use stays bounded with automatic cleanup; no hand-written cleanup scripts. **[owner]**
- R8.2 Caching works across runs and worktrees. **[owner]** Caches hold only what a machine can rebuild: built inputs,
  built binaries, assembled programs, benchmark baselines, and reference outputs a machine seeded for diagnosis (R2.4),
  which it can seed again. Each cache has a size cap. **[proposed]**
- R8.3 Built binaries (natemu, its host and resident, fexc) are identified by the source and settings they were built
  from. Worktrees with the same code share one build; fexc on the same pin is built once per machine. Two runs needing
  the same build do not build it twice at once. **[proposed]**
- R8.4 Cleanup keeps what is still referenced, and deletes the rest. Referenced means: by the locks and declarations
  of every live worktree on the machine (not one "current" lock), by stored benchmark baselines and the builds they
  were measured with, by kept runs, and by a lease that each queued or running run holds on every resource and build
  it prepared, released when the run ends or its process dies. **[proposed]**
- R8.5 Run outputs expire unless marked to keep. A passing test's output is not kept beyond its run, except that every
  attempt of a test that failed and then passed on a rerun is kept, so the flake can be diagnosed. **[proposed]**
- R8.6 Images cannot blow up disk use. **[owner]** One base per image recipe; per-run changes go to qcow2 overlays or
  snapshot disks (natemu supports both). **[proposed]**
- R8.7 Cargo's per-worktree `target/` folders are outside the framework's cleanup, and so are snapshot overlays in the
  temporary folder (natemu frees them at exit). The status view reports their size, since `target/` folders grow
  fastest when many agent worktrees exist. **[proposed]**
- R8.8 natemu's own compile cache (`~/Library/Caches/natemu/fexc`) is shared state: each run sets it explicitly
  (a per-build folder, or off), so one build's results do not depend on how warm another build left it, and builds do
  not evict each other's entries. **[review call]**

### R9. Local machine configuration

- R9.1 The list of machines is local, not committed: a gitignored folder in the repo (for example `.natemu/`). **[owner]**
- R9.2 It holds only names and ssh addresses; everything else is detected (R5.1).

### R10. Remote execution

- R10.1 Remote execution is in scope as a design requirement: declarations, claims, placement and results already
  carry what it needs, so adding it changes no test. **[proposed]** Whether it is built now or later is open (question 5).
- R10.2 A remote run tests exactly the requester's code: the worktree, uncommitted changes included, is captured as a
  temporary commit, and the run is identified by that commit's hash. **[proposed]**
- R10.3 A remote run claims from that machine's own pool, alongside work started directly on it. **[proposed]**
- R10.4 Placement considers each machine's current queue. **[proposed]**
- R10.5 A machine that fails its setup check is not used; one that drops off mid-run has its unfinished tests reported
  as lost, and rerun elsewhere when another machine fits. **[proposed]**
- R10.6 Results from all machines merge into one report; only failing tests' outputs are fetched back. **[proposed]**
- R10.7 Scratch checkouts made for remote runs expire like other run outputs. **[proposed]**

### R11. Tooling choice

- R11.1 Adopt well-tested tools wherever they fit; build only what no existing tool does for this setup. **[owner]**
- R11.2 The framework's own tools (its third-party packages) are pinned and the same on every machine. Tools that build
  inputs follow R2.15 instead: recorded, and pinned only when a machine's build differs. **[proposed]**

### R12. Campaigns

- R12.1 Linux workload campaigns (LTP, OpenSSL, CPython, libm, litmus, stress, torture) are organized in the same
  model as tests and benchmarks, even if their onboarding comes later. **[owner]** The owner leans towards onboarding
  them in this refactor, depending on its difficulty. **[owner]** R1.4 is the known obstacle (R12.2).
- R12.2 Their guest programs must become buildable on any runner (R1.4); today they are compiled on the x86 machine.
  Their per-case expected results are answers, seeded and locked like any other. **[proposed]**

### R13. Migration

- R13.1 Families move over one at a time. Until a family is converted, its existing runner may be wrapped whole and
  judged by its exit status, with QEMU still running live and `natemu-raw` still used. Such a wrapped family does not
  yet meet R1 and R2, and the run report says so. **[review call]**
- R13.2 Converting a family means its cases become individual results compared against locked answers (R2.14).
  The tests' assertions do not change; their comparison and reporting path does. **[review call]**

### R14. Resource preparation

Today each tool fetches its own resources: `run_io5.py` and `tools/play/build.sh` carry their own URLs and hashes
(a Debian cloud image, kernel packages from `snapshot.debian.org`, source tarballs from kernel.org and python.org);
`tools/play/build.sh` builds the Debian root file system over ssh on the x86 machine; several I/O runners take BusyBox
and other files from an earlier run's `inputs.json` folder in `natemu-raw`; results land in different places
(`natemu-raw`, `~/Library/Caches/natemu/play`).

- R14.0 The framework provides resource support and caching as a capability of its own: declaring, fetching,
  verifying, deriving and caching resources. Tests and harnesses get resources only through it and never download or
  build shared resources themselves, so a resource shared by many test cases (a base image) is fetched and prepared
  once per machine, not once per case, run or worktree. **[owner]**
- R14.1 Every external resource (a base Debian image, a kernel package, BusyBox, a source tarball) is declared once in
  the repo, by name, with its URLs and pinned hash (R2.5's one exception). Tests and recipes refer to it by name; no
  tool carries its own URL or hash. **[proposed]**
- R14.2 URLs are versioned or dated, never "latest" or a daily image that rotates; a resource may list several
  mirrors (for example `deb.debian.org`, then the dated `snapshot.debian.org` archive). A download is checked against
  its pin before any use; a partial or mismatched file is never used. **[proposed]**
- R14.3 Resources derived from downloads (a Debian root file system from snapshot packages, a BusyBox initramfs, a raw
  or qcow2 base from a cloud image) are environment inputs (R2.15), built by recipes in the repo on the Mac itself, not
  on the x86 machine (R1.4). **[proposed]**
- R14.4 Preparation happens before tests start: a run first prepares what its selected tests need, outside any test's
  timeout, so no timed or exclusive test ever waits on a download or build. Preparation still takes a CPU claim like
  any shared work, and none starts while an exclusive window is open (R4.5). A requester can also
  ask to prepare without running anything, to ready a machine ahead (before remote dispatch, or before going
  offline). **[proposed]**
- R14.5 Prepared resources are shared by every worktree on the machine, through the cache (R8.2): each is prepared
  once, and a second request waits for the first instead of preparing it again. They count toward the cache's size
  cap, and cleanup keeps those referenced by current declarations (R8.4). Large ones are kept as one base, with
  per-run changes in overlays (R8.6). **[proposed]**
- R14.6 A resource that cannot be prepared (no network, an upstream that has gone away, a hash mismatch, a missing
  host tool such as `qemu-img`) makes the tests that need it not runnable, with the reason; never a test failure.
  Because no data is stored (R1.2), a vanished upstream means re-pinning the resource in the repo. **[proposed]**
- R14.7 A test that needs the network while it runs (a guest running `apt-get` or `curl`, as IO5 does) declares it as
  a host requirement, so it is placed only on machines with network access and a network failure is reported as such.
  **[proposed]**
- R14.8 Download requests identify the client generically and carry no user or project details. **[proposed]**

## Approaches considered

This section is context, not a requirement. The requirements above hold for any approach; the choice between these
is made in the implementation plan, against R11.1.

| Approach | Summary | Status |
| --- | --- | --- |
| A. pytest plus a thin natemu plugin | pytest (with pytest-xdist) supplies discovery, variants (`parametrize`), filtering (`-k`, `-m`), reporting; cargo-nextest runs the Rust tests; the plugin adds the lock, cache, claims, placement and selection | Recommended; not yet chosen by the owner |
| B. TOML declarations plus our own runner | Pure-data declarations; we own discovery, variants, filtering and reporting | Fully declarative, but reinvents what A gets for free (against R11.1) |
| C. Bazel or Buck2 | Content-addressed builds and caching, remote execution | Rejected: would absorb cargo, FEX's CMake and signing, and its hermetic test model fights Hypervisor.framework, exclusive machines and timing tests; remote execution needs a server |
| D. Nix for inputs | Content-addressed inputs with cleanup from roots | Rejected: schedules no tests, weak macOS sandbox, a second package world |

Evidence for A (a throwaway spike with pytest 9.2.0 and pytest-xdist 3.8.0, sleep-only tests, this M2 Max and the M4):
claims enforced at test start held exclusivity and the core budget across two concurrent sessions; a quick session
waited at most one exclusive test; a killed session's claims were released; tests were placed by detected AFP across
both machines and ran on one shipped commit, with merged reports. Measured costs and limits: an xdist worker holds the
next test while it waits, so claims must be checked when a test starts, and throughput was about 20% below ideal on
the toy load; xdist's own ssh workers are not placement-aware and its code-copying (`--rsyncdir`) is deprecated.

## Open questions

For the owner, most important first:

1. **Approach.** Not a requirement (see above), but needed before the implementation plan: A (pytest plus a thin
   plugin) is recommended; the owner has not chosen it yet.
2. **Detached runs (R7.4).** Should a run be submit-and-check-back by default, rather than a foreground command? This
   comes before any scheduler work: it decides whether claims, queueing and priority across unrelated processes
   (R4.4–R4.6) live in a shared file or in a small per-machine service.
3. **Done criteria for the migration.** Must every one of today's ~185 suite steps map to a new test (or be dropped
   with the owner's agreement) before `run_suite.py` is deleted? What proves parity: the same pass/fail on the same
   commit, on the M2 and the M4? Is "no family still wrapped (R13.1), so nothing reads `natemu-raw`" part of done?
4. **Time targets.** How long may `smoke`, *affected* and `gate` take? Is the spike's ~20% throughput loss
   (claims checked at test start under xdist) acceptable for the gate, or must the framework pack work as tightly as
   today's estimates and shards do?
5. **Remote execution (R10).** Built in this refactor (last), or later?
6. **Review of the [proposed] requirements.**

Left for later or for the builder:

7. **Exclusivity levels (R4.2).** Is one exclusive level enough, or do some timing tests need to share the machine with
   light work, and are benchmarks exclusive by default? The owner will decide later.
7a. **Once or every capable machine (R5.4a).** Without a requester's request, does a variant run once or on every
    machine that can run it? Undecided; the owner's view is that it matters only when the change needs it.
8. **x86 diagnosis.** When a test seeded on x86 hardware (adx, sha, xsave) fails and the x86 machine is unreachable,
   what does the failure report show?
9. **Lock granularity (R2.6).** One lock per family or one at the root?
10. **Program harnesses and `--config`.** The program-test harnesses (`run-program`) do not launch through natemu's
    configuration file. Should they move to it, or keep their own interface?
11. **Keeping path rules current (R6.3b).** Declared rules can go stale when code moves. A coverage-measured check
    (LLVM source-based coverage for the host side and fexc; `minicov`, which needs a nightly compiler, for the
    bare-metal resident) was considered and set aside for now as of uncertain soundness and feasibility.
12. **Building Linux guest content on the Mac (R12.2, R14.3).** The campaigns' x86 guest programs and the Debian
    root file system are built on the x86 machine today. Cross-compile on the Mac, or build them in a Linux container
    (the reference tool already uses a pinned Apple `container` image)?
