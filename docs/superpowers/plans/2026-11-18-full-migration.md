# Full Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** every step of `tools/run_suite.py` runs as a testkit family on any Apple Silicon Mac, with no `natemu-raw`,
no wrapped (legacy) items, QEMU and the x86 machine used only when seeding, and answers checked against locked hashes;
real natemu bugs it finds are recorded, test-side defects are fixed. Then the full gate runs on this M2 Max and the M4.

**Architecture:** three layers, each removing one kind of dependency without rewriting the 47 runner scripts.
(1) *Inputs*: every file a runner reads from natemu-raw is built by the framework (recipes, pinned downloads, a pinned
QEMU build) into a per-machine **input root** with the same relative paths, and runners get `NATEMU_RAW_ROOT=<input
root>`; corpora move to the programs harness. (2) *Answers*: the two places that run QEMU for runners
(`tools/reference/ref.py:run_programs` and `tools/qemucache.py`) get a seed mode and a locked mode: `natemu-test lock`
runs QEMU and records each answer's hash in the family's lock; a test run refuses to start QEMU and accepts a cached
answer only if its hash is locked. (3) *Reporting*: a `runner` harness runs a runner script as a judged item and turns
the `results.json` most runners write into per-case results.

**Tech Stack:** as `2026-11-17-test-framework.md`; QEMU 11.1.2 built from its release tarball.

**Spec:** `docs/superpowers/specs/2026-11-17-test-framework-design.md`; this plan finishes R13 (migration).

## Global Constraints

- The decisions and constraints of `2026-11-17-test-framework.md` hold.
- A runner script's checks do not change; only where its inputs and QEMU answers come from (R13.2).
- QEMU (user and system) is the pinned build of this plan on every machine; a runner never starts QEMU outside seeding.
- Answers seeded from x86 use `ubuntu@192.168.12.10` (AMD Ryzen 7 8845HS, the machine the existing x86 answers came
  from); `NATEMU_X86_HOST` overrides.
- Real natemu bugs found are recorded in `docs/research/notes/2026-11-test-migration-findings.md` (symptom, step, case,
  host, reproduction command); they are not fixed here.

## Review Focus

1. A locked-mode run whose QEMU answer is missing from the machine's cache: it must report `stale-lock` (needs seeding
   here), not start QEMU and not pass. Test in Task 3.
2. Seeding on a second Mac produces a different answer than the lock (QEMU nondeterminism, a different build): a
   seeding failure naming the key, never a silent re-lock. Test in Task 3.
3. A runner that writes no `results.json` or a partial one: the step is judged by its exit status alone and says so.
   Test in Task 4.
4. Two runners sharing one input (the i7 corpus, the kernel): built once per machine, never per step. Test in Task 2.
5. A step that the old suite runs in several variants (block size 1, CPL 0, the `-m2f`/`-m2a` FP variants): each is
   one variant of one family entry, not a duplicated entry. Test in Task 5.

---

### Task 1: Pinned QEMU system build

**Files:** `testkit/artifacts.py`, `tests/resources.toml` (QEMU 11.1.2 release tarball from download.qemu.org, sha256),
`testkit/tests/test_artifacts.py`

- `artifacts.qemu_system() -> Path`: build `qemu-system-x86_64` (target `x86_64-softmmu` only, no GUI, Homebrew's
  build dependencies) from the pinned tarball into a cache entry; firmware (`share/qemu`) installed beside it.
- `tools/bench/smp/smp.py` and every caller that finds `qemu-system-x86_64` use `NATEMU_QEMU_SYSTEM` when set.
- Test: the built binary reports `11.1.2`; `qemucache`'s key changes with the binary (it hashes it already).

### Task 2: The input root

**Files:** `testkit/inputs.py`, `tests/resources.toml`, `tools/run_virtio_block.py` (only if its `prepare` needs an
argument it lacks), `tools/bench/image`-style recipe for the network rootfs, `testkit/tests/test_inputs.py`

- `inputs.root() -> Path`: a cache entry laid out like natemu-raw, holding exactly:
  - `kernel-census-20261028-01/dl/{linux,initrd.gz}`: the Debian trixie netboot installer files, pinned downloads (URLs
    and sha256 from `docs/research/investigations/INV-03-03.md`);
  - `reference-slice1-20261026-01/pin/qemu-x86_64-static`: `artifacts.qemu_user()`;
  - `io3-block-20261108-01/{inputs-01,prerequisites/*.deb}`: built by `run_virtio_block.py prepare` from the pinned
    kernel package and e2fsprogs (its recorded `inputs.json` gives the arguments);
  - `io-network-tools-20261108-01/rootfs`: a recipe in `tools/io/network-rootfs/` built by the bench-image builder
    setting (packages and versions from `docs/restructure/io-benchmarks.md`);
  - generated corpora that runners read as programs only (the i7 corpus for `run_slice1_faults`): generator + args;
  - `fixtures/slice1` built by `tools/reference/build_slice1.py` from its committed manifest (into the repo path the
    runners read, as today, but by the framework).
- Nothing else: a runner reaching for anything not listed fails loudly (the empty-root run of November 18 is the check).
- Test: building twice reuses every part; a missing pin is `unpreparable`.

### Task 3: QEMU answers: seed mode and locked mode

**Files:** `tools/reference/ref.py` (`_cache`, `run_programs`), `tools/qemucache.py`, `testkit/answers.py`,
`testkit/tests/test_answers.py`

- `NATEMU_ANSWERS=seed|locked` and `NATEMU_ANSWERS_LOCK=<family tests.lock>`; caches live in the input root.
- seed: as today (run pinned QEMU on a miss, store), plus append `key = sha256(answer)` to a seed log.
- locked: on a miss, raise `AnswerMissing(key)` (the step reports `stale-lock: needs seeding on this machine`); on a
  hit, the answer's hash must equal the lock's entry for that key, else `AnswerMismatch` (a failure).
- `natemu-test lock FAMILY` for a runner family: run each step in seed mode with an empty answer cache, collect the seed
  log into `[answers]` of the family's lock; on a machine that seeds again, a differing hash is a seeding failure.
- Test: miss in locked mode never starts QEMU (a fake QEMU that records calls); mismatch fails; re-seed is identical.

### Task 4: The runner harness

**Files:** `testkit/harness/runner.py`, `testkit/tests/test_runner.py`

- A suite entry runs one runner script with arguments (`argv`, `{fexc}`, `{out}` placeholders), `NATEMU_RAW_ROOT` =
  the input root, `NATEMU_ANSWERS=locked`, the family's lock; claims as the old step's (`SERIAL_WHY`, `w`, `E_OK`,
  moved into each entry's `needs`).
- Pass: exit 0, or the step's judge (`i8b_judge` and kin move into the family's `judges.py`).
- Per-case results: if the runner wrote `results.json` with a `results` list of `{name, outcome, diffs}`, each becomes
  a sub-result; otherwise the step reports its exit status alone and says "no per-case results".
- Test: a fake runner writing results.json gives N sub-results; one writing none gives one result with that note.

### Task 5: Families

**Files:** `tests/<family>/family.toml` (+ `judges.py`, `tests.lock`) for each, `tests/legacy` removed

Groups (one family each; every `run_suite.py` step maps to exactly one entry or variant; a test checks the mapping):

| Family | Steps | Harness |
| --- | --- | --- |
| `programs` | fix, seed, i9, i9-big, i10, rex, i7, i7-cpl0, sha, adx, xsave, br66 and their `-1` | programs (`engine.max_block` and `vm.cpl` variants; sha/adx/xsave seeded on x86) |
| `fp` | i8, i8b (+ `-1`, `-m2f`, `-m2a`) | programs (exists; add `engine.max_block`) |
| `flow` | flow, i6b corpus | programs, flow programs supported by locking QEMU's executed-path mask with the answer |
| `faults` | faults@*, i7-faults@*, loopfaults-*, probes@*, i5c, i5a-de, i6/i8/i9/i10-faults (+ variants) | runner |
| `smp` | corpora-c*, smp, litmus, limits, privilege, delivery, kicks, contain, system, coherence, sharedpt, events, vector-* | runner |
| `system` | k1-*, k2-*, pic-*, x1, x3-*, x4, m1, m2*, m3, l3, l4*, r1*, s4, i5b-*, nostop-*, icebp, cpu-forms, frame-*, instruction-gaps*, maskmovq*, stack, barriers, asm* | runner |
| `boot` | l1, l2, l2-iasl, boot-smoke | runner |
| `io` | io-coverage*, net-linux*, virtio-lifecycle* | runner |

- Seed every family; commit the locks.

### Task 6: Test-side fixes and notes

- Merge the agents' fixes (fptrap AFP tests, `k2-stmxcsr`, FP deviation tags and `test_fp_deviations`).
- Every remaining failure on this M2 Max is classified: test defect (fix), framework defect (fix), or natemu bug
  (record in the findings note with its reproduction).

### Task 7: Gates

- Full gate on this M2 Max; then on the M4 from a scratch clone of the branch (never its checkouts), with the bench image
  builder setting copied; combine coverage (`--with-run`). Record both in the findings note and the plan.

---

## Revision after the plan review (November 18)

The review's verdict was "not ready": Task 3 kept a per-machine cache of seeded answers, which breaks R2.3/R2.4 (an
answer is never stored; a run never needs QEMU). These revisions replace the tasks above where they differ.

- **Task 3' (answers):** `tools/reference/answers.py` (stdlib only, usable by every runner) is called at the comparison
  points, not the QEMU calls: `ref.compare` (user mode), `smp.compare`/`judge_smp` (system mode), `run_x3.compare_qemu`,
  `run_l4b`'s comparison, and the l3/l4a/r1 serial-capture, s4 "did not finish" and hang paths. Each point names the
  *projection* it compares (the fields, under the masks it applies). Modes: `live` (default outside testkit: today's
  behavior), `seed` (run QEMU, record `key -> sha256(projection)` plus small derived values the projection needs, such
  as an RFLAGS mask from the executed path), `locked` (never run QEMU: hash natemu's state under the same projection
  and compare with the lock; a missing key is `stale-lock`). In locked mode the QEMU-running functions return a token
  carrying the key, so runner code that passes results to the comparison is unchanged; runners that read QEMU fields
  directly are converted one by one. Keys are machine-independent: the program or image bytes, the QEMU pin (source
  tarball sha256 and build options), the QEMU arguments; never the backend, a binary hash or a code hash. Mismatch
  details need `--diff` (seeds the one case live).
- **References (owner, November 18):** every case is checked against correct x86 behavior, from whichever source gives
  it (QEMU or the x86 machine; choosing is the test writer's job, R2.13a), except x87: x87 is checked against FEX's
  result (reduced precision, the only x87 mode natemu has: `tools/fexc/fexc.cpp` fixes X87REDUCEDPRECISION=1) until
  accurate 80-bit x87 exists (docs/restructure/afp-accurate-plan.md), when the accurate variant moves to x86 answers.
  So the programs harness gains a third reference, `fex` (seeded by running natemu; the lock records that provenance),
  and `run_i6b`'s 2-ulp tolerance against QEMU goes away: its 48 x87 programs take `fex`, its 64 SSE4.2 PCMPxSTRx
  programs keep QEMU's answers (QEMU and the Ryzen agree on all 64, checked November 18).
- **Task 2' (inputs and state):** the input root stays an immutable cache entry. Writable state (`.vm.lock`,
  `.net-ports.lock`, `asm-cache.sqlite`, `build_slice1`'s run folder) moves to `NATEMU_STATE_ROOT` (default: the raw
  root, so the old suite is unchanged), set by testkit to a per-machine state folder. Added inputs: the `s2d` corpus
  (`gen_slice2` defaults, required, not optional); the virtio rng-inputs chain for `run_virtio_block prepare`
  (`run_virtio_rng`'s inputs: kernel, initrd, busybox package, all pinned); e2fsprogs as a pinned host tool.
  The network rootfs builds on the Mac (the local amd64 container), as R14.3 requires.
- **Task 4' (per case):** one adapter per results shape (`{results: [{name, outcome}]}`, top-level lists of
  `{name, ok, problems}`, `{name, errors}`, `pass`/`reasons`, `faults.json`, `system.json`); runners with no per-case
  output (k1, k2, pic, m1-m3, x1, x3, x4, r1, s4, l3, l4a/b, nostop, asm, boot_smoke) gain a standard `results.json`.
  A step counts as migrated only when it reports per case. The runner harness keeps legacy's environment
  (`NATEMU_PREBUILT=1`, `CARGO_TARGET_DIR` = the natemu entry, `NATEMU_VM_LOCK_HELD=1`, the guest-state alias, the
  build lock for steps that build) before `legacy.py` is removed.
- **Task 5':** 190 steps; `i6b-cases(-1)` map to `flow`'s i6b entry as cases; `stack` and `barriers` become a `static`
  family (no VM). i8b's 7 departures are now x86-answered cases, so its judge goes away.
- **Task 1':** QEMU is built with `--enable-tools` (qemu-img), installed with its firmware under `share/qemu`;
  `smp.QEMU` and `io_common`'s qemu-img read `NATEMU_QEMU_SYSTEM`/`NATEMU_QEMU_IMG` at import. nasm and iasl are
  checked as host tools with recorded versions.
- **Order:** Task 3' and the test fixes land before any seeding; then every family is seeded once.
- **Found while seeding fp (November 18):** the offline model's x86 rules disagree with the real-x86 answers for i8b's
  i8-forms-045, i8-random-097 and i8-random-111. To check against natemu's accurate mode before deciding whether the
  model or the x86 harness is wrong.

- **Answers, final form (owner left the balance to the implementer, November 18):** each locked answer has its sha256
  (always), its canonical value inline when that is at most 1 KiB (so runners compare unchanged and failures show real
  diffs), and reproduction steps (supplier, its pin, the program or image and arguments) so anyone can re-derive and check
  it. Larger answers are hash plus reproduction steps. References come from a supplier with several backends (QEMU user
  or system, the x86 machine, the Intel-SDM model, FEX for x87; Rosetta once validated per instruction group), each answer
  recording which; seeding is pooled per backend.
- **Bulk migration runs in parallel agents,** one per runner family (faults, smp, system, io, boot, static), once the
  answer hooks and the input root have landed; each brings its family to locked, per-case results and reports test-side
  fixes and natemu findings.
- **Where an expectation lives (owner, November 18):** answers produced by running a reference (QEMU, x86, the model,
  FEX) go in the lock with hash, short value and reproduction steps. Expectations derived by reasoning (SDM tables,
  hand-computed values) are code: inline in the test beside the case, reviewed as code, no lock and no reproduction
  steps. A check that needs no VM (a rule table, the decoder, the FP model, a parser) belongs in a unit test (cargo test or
  pytest); one that judges what natemu does when it runs guest code is an integration test, even with a static expectation.

## Status (November 18)

- **Migrated:** every `tools/run_suite.py` step runs in a family: unit, fp and programs in their own harnesses; the 148
  runner cases in `tests/{boot,static,system,smp,faults,io}`, each reporting per case and comparing with locked QEMU
  answers (faults 11,882, smp 8,304, system 1,640). The legacy family is removed. Batch A (SDM- and model-judged runners)
  and batch B (QEMU-comparing runners) ran as parallel agents; their findings are in
  docs/research/notes/2026-11-test-migration-findings.md.
- **M2 Max gate** (run 20261118-045129-009e, idle machine, 32 min): 82,954 passed, 2,329 not runnable (FEAT_AFP variants),
  2 failed (probes: answers seeded in a worktree without FEX's tests; fixed and reseeded, probes now 88/88 locked).
- **M4 gate** (scratch clone `~/natemu-testkit-gate`, run 20261118-054302-7e7b, 51 min): 85,074 passed, 2 not runnable,
  7 failed: `test_suite_accounting` (fixed: it opened the real natemu-raw lock), net-linux and net-linux-pci (the M4's
  Homebrew Python, which the framework's environment is built from, cannot accept connections from other processes; the
  system Python can: a setting on that Mac), and the qcow 64 GiB write-zeroes timing test (13 s against a 5 s bound, on a
  disk 95% full). The bench tier was not run there (the M4 has no ssh access to the x86 builder). Coverage was not combined.
