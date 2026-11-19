# Campaign Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every long-running check natemu has (the four I/O and serial stress runners, the M1 performance check and the
ten Linux workload campaigns in `tools/correctness`) runs from `tools/natemu-test run --tier campaign` (or `--tier bench`)
on any Apple Silicon Mac, with inputs built on that Mac and per-case answers locked or judged.

**Architecture:** Campaigns are ordinary families with `tier = "campaign"`. Their kind stays what decides a result:
`deterministic` (a locked per-case answer) or `judged` (a checker). Two pieces are new. First, container-built inputs:
a recipe folder run in the amd64 Debian container the benchmark image already uses (`artifacts.BENCH_BUILDER`, through
Rosetta), producing an initramfs or a tree as a cache entry. Second, a `campaign` harness that boots one image part per
case, reads `CT-RESULT` lines and compares each test's normalized result with its locked answer. The four stress
runners need only new inputs and runner cases.

**Tech Stack:** Python 3.11+, pytest (testkit), Apple `container` (amd64, Rosetta), Debian trixie snapshot packages,
the pinned QEMU 11.1.2 system build (`artifacts.qemu_system()`), natemu's stub/runtime builds (`artifacts.natemu()`).

**Spec:** docs/superpowers/specs/2026-11-17-test-framework-design.md (R1.4, R2, R3.6-R3.8, R12, R13, R14); campaign
behaviour as recorded in docs/restructure/correctness-*.md and docs/restructure/io-plan.md.

## Decisions for the owner to review

1. **Campaign is a tier, not a kind** (owner's question, November 18): `tier = "campaign"` with kinds `deterministic`
   or `judged`.
2. **Answers for the Linux campaigns come from the pinned QEMU 11.1.2 system build**, booting the same kernel and image
   with the same CPU model `boot.py` uses (`QCPU`). Reasons:
   - It is local: any Mac can seed it.
   - It is like-for-like: same kernel, root and image. The x86 box runs a different kernel as a non-root user, which
     made LTP's native results unusable.
   - It matches how the campaigns were already judged: LTP's oracle was QEMU.

   A case where QEMU is wrong takes an `x86` override (native run of the same `run-S.sh` on the x86 box), as the
   programs families do. OpenSSL, torture, libm, gstr and x86st are also run natively once at seeding, as a cross-check
   reported like `natemu-test xref`.
3. **Judged, not locked:**
   - litmus: herd7's x86-TSO allowed-state set; a forbidden state fails.
   - stress-ng: rc/pass/skip as in the QEMU run, no `fail:` lines, no BUG/Oops in the kernel log.
   - public: `public-campaign.py`'s classify; "completed-needs-oracle-review" is reported as `unverified`, never as
     passed.
4. **Known natemu deviations are rules, not answers:**
   - reduced-precision x87: torture's 8, libm's 332;
   - AVX-512-only libm tests;
   - CPU-count skips at N=1.

   A family's `rules.py` excludes them with the documented reason; nothing locks a wrong result as correct.
5. **Timing-sensitive tests** (LTP getrusage04, pselect01_64, kill11; CPython's timerfd cases) are listed per suite in
   `flaky = {name = "why"}`. They are rerun once and report `flaky` instead of failing the campaign.
6. **run_io_soak and run_serial_stress accept the framework's stress-ng build.** Their pinned binary hash came from a
   build kept only in natemu-raw. They get `--stress-provenance FILE`, a JSON file the recipe writes. The source pin
   (`ffe89071…52bd`, the same tarball the benchmark image uses) is unchanged.
7. **`--forced` io-stress runs** need separately built scheduling-hook binaries. They are left out and listed as not
   onboarded.

## Global Constraints

- Every input is built on the Mac from a recipe in the repo or a pinned download (sha256), never copied from
  natemu-raw (R14). Recipes pin Debian packages through snapshot.debian.org plus a `debs.sha256` file, as
  `tools/io/network-rootfs/container.sh` does.
- The x86 machine and QEMU are used only to seed (R2.1). A campaign run starts neither.
- Benchmarks are compared only on the same machine (R3.6).
- Campaign and bench suites never run in `--gate` (`testkit/select.py:70`).
- Locks stay TOML 1.0. Repro strings are shared through the lock's `[repro]` table (`answers.share_repro`).
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Work stays on `test-framework`; no push.

## Review Focus

- A recipe whose apt snapshot or URL is unreachable must fail preparation with the URL in the message (category
  `unpreparable`), not hang. Container builds take a `timeout`.
- A guest that never prints `CT-END` (hang, kernel panic, watchdog) must fail the case with the last 40 serial lines,
  not pass with zero results. Task 6 test: `test_part_without_ct_end_fails_with_serial_tail`.
- A test present in the lock but missing from a run's output (crashed mid-part) must fail as `missing`, not be
  skipped. Task 6 test: `test_locked_test_missing_from_output_fails`.
- Two campaign parts must never write the same output folder or share an image copy that one of them modifies. Task 6
  test: `test_parts_use_private_work_folders`.
- Seeding the same suite twice must give identical answers for every test not marked flaky. Task 7 test:
  `test_two_seeds_agree_or_are_flagged`.

---

## Part A: the stress runners and the M1 performance check

### Task 1: io-stress and lifecycle-stress as campaign cases

**Files:**
- Modify: `tests/io/family.toml` (new suite `io-campaign`)
- Modify: `testkit/harness/runner.py` (adapter for `{pass: bool, errors: [...]}` single results)
- Test: `testkit/tests/test_runner.py`

**Interfaces:**
- Produces: suite `io/io-campaign` with `tier = "campaign"`, `kind = "judged"`, `harness = "runner"`,
  `needs = {exclusive = true, why = "120 s VM with a watchdog: timing-sensitive"}`, `params = {timeout = 900}`.
  Cases:
  - `io-stress-n2` and `io-stress-n4`: `tools/run_io_stress.py --cpus 2|4 --host {natemu_host} --runtime {natemu_runtime} --fexc {fexc} --out {out}`.
  - `lifecycle-reset` and `lifecycle-disable`: `tools/run_io_lifecycle_stress.py --case reset|disable ...` with the same binaries.
  - `lifecycle-corrupt-control`: `--corrupt-cycle 17`, with `judge = "judges:must_fail"` (the negative control must fail).
- Produces: `_shape_single(d)` in runner.py, which turns `{"pass": bool, "errors": [...]}` into `[(case_name, 'passed'|'failed', '; '.join(errors))]`.

- [ ] **Step 1:** Write `test_single_result_shape`: `per_case` on `{"pass": false, "errors": ["x"]}` gives one failed entry with message `x`; on `{"pass": true, "errors": []}` one passed entry.
- [ ] **Step 2:** Run `tools/natemu-test -m pytest testkit/tests/test_runner.py -q -p no:cacheprovider`. Expect the new test to FAIL.
- [ ] **Step 3:** Add `_shape_single` after `_shape_pass_map` in `ADAPTERS`. It must check that `pass` is a bool, so the existing `pass`-map shape still matches first. Add the suite, the cases and `tests/io/judges.py:must_fail(out)`, which returns `(True, 'negative control failed as required')` when `out/result.json` has `pass: false`.
- [ ] **Step 4:** Run the unit tests (PASS), then `tools/natemu-test run --tier campaign tests/io/family.toml`. Expect 5 passed.
- [ ] **Step 5:** Commit.

### Task 2: fio preparation, matrix inputs and stress-ng as recipes

**Files:**
- Create: `tools/io/fio-prep/` holding:
  - `container.sh` (fio 3.39-1 and its dependency closure from snapshot `20261108T000000Z`, `dpkg -x`, writes `rootfs.tar`, `elf-closure.json`, `packages.json`);
  - `packages.txt` and `debs.sha256`;
  - `profiles/*.fio` (the 11 reviewed profiles, copied as text from `natemu-raw/m2-fio-setup-20261114-01/profiles`);
  - the closure logic of that folder's `prepare_bundle.py` and `finish_bundle.py`, folded into `container.sh` or a `closure.py` beside it.
- Create: `tools/io/stress-ng/container.sh` (stress-ng 0.19.02 orig `ffe89071…52bd` plus the Debian tarball pinned in `tools/correctness/stress-remote.sh`, `make STATIC=1`; writes `bin/stress-ng`, `source.tar.xz`, `provenance.json` = `{"binary_sha256", "source_sha256", "recipe_sha256", "builder"}`)
- Modify: `testkit/inputs.py` (`fio_prep()`, `matrix_inputs()`, `stress_ng()`; new root paths `FIO_PREP = 'io-benchmark-prep-20261108-01'`, `STRESS = 'io-safety-stress-20261108-01'`)
- Modify: `tools/run_io_soak.py`, `tools/run_serial_stress.py` (`--stress-provenance FILE`: when given, the binary must hash to its `binary_sha256` and the source to `SOURCE_SHA`; the retained-build checks are skipped and the provenance is recorded in `manifest.json`)
- Test: `testkit/tests/test_inputs.py`, `tools/test_io_soak_provenance.py`

**Interfaces:**
- Produces: `inputs.fio_prep() -> Path` (a folder with `rootfs/`, `profiles/`, `elf-closure.json`).
- Produces: `inputs.matrix_inputs() -> Path` (the output of `run_io_matrix.py prepare --fio-prep <fio_prep> --block-inputs <block_inputs> --out`).
- Produces: `inputs.stress_ng() -> Path` (a folder with `bin/stress-ng`, `source.tar.xz` and `provenance.json`).
- All three are `resources.derived` entries keyed by the recipe folder and `artifacts.BENCH_BUILDER`.

- [ ] **Step 1:** Write `test_soak_accepts_matching_provenance` and `test_soak_refuses_mismatched_provenance`. They call the soak prepare's provenance check with a temporary binary and a provenance file whose hash matches, then one whose hash does not; the second raises `ValueError('stress binary/source pin')`.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement the recipes and the input functions, then the `--stress-provenance` option in both runners. The `container run` command follows `inputs._network_recipe`, with `timeout=3600`.
- [ ] **Step 4:** Run the tests (PASS), then `PYTHONPATH=. python3 -c "from testkit import inputs; print(inputs.matrix_inputs())"`. Expect a folder holding `inputs.json`, `linux` and `rootfs/bin/busybox` with sha `7ae95c26…`.
- [ ] **Step 5:** Commit.

### Task 3: serial-stress and soak as campaign cases

**Files:**
- Create: `tools/run_io_campaign.py`. It is a driver: `serial-unpaced`, `serial-paced` and `serial-loaded` run `run_serial_stress.py prepare` into `{out}/inputs` and then `run`; `soak` runs `run_io_soak.py prepare` and then `run --backend natemu`. Inputs come from `NATEMU_RAW_ROOT`'s `FIO_PREP`/matrix and `STRESS` paths.
- Modify: `tests/io/family.toml` (cases in `io-campaign`), `testkit/inputs.py` (`PATHS`/`components()` gain the three inputs)
- Test: `tools/test_io_campaign.py` (argument assembly only)

**Interfaces:**
- Consumes: `inputs.matrix_inputs()` and `inputs.stress_ng()` (Task 2).
- Produces: cases `serial-unpaced`, `serial-paced`, `serial-loaded` and `soak`.
  - All have `needs = {exclusive = true, why = "heartbeat gaps of at most 2 s; 5-minute loaded soak"}`.
  - `soak` has `params.timeout = 2400`.

- [ ] **Step 1:** Write `test_serial_loaded_uses_factor_3_and_cpu_load`. It asserts that the driver's prepare argv for `serial-loaded` contains `--factor 3 --cpu-load --stress <root>/io-safety-stress-20261108-01/bin/stress-ng`, and its run argv contains `--mode unpaced`.
- [ ] **Step 2:** Run it. Expect FAIL.
- [ ] **Step 3:** Implement the driver and the cases.
- [ ] **Step 4:** Run the test (PASS), then `tools/natemu-test run --tier campaign tests/io/family.toml`. Expect 9 passed: Task 1's five plus these four.
- [ ] **Step 5:** Commit.

### Task 4: the M1 performance check in the bench tier

**Files:**
- Modify: `testkit/artifacts.py` (`natemu_at(rev: str) -> Build`: the stub and runtime built from `git archive REV` in a scratch folder, cached by the tree hash)
- Modify: `testkit/inputs.py` (`io5_inputs()`: `tools/run_io5.py prepare`'s folder, built from its pinned downloads)
- Modify: `tools/run_m1_perf.py` (`--baseline-build DIR`, `--final-build DIR`, `--fio-prep`, `--block-inputs`, `--io5-inputs` override the `RAW_ROOT` constants; defaults unchanged)
- Modify: `tests/bench/family.toml` (case `m1-perf`: `run_m1_perf.py full --dir {out} ...`, `params.timeout = 7200`)
- Test: `testkit/tests/test_artifacts.py`

**Interfaces:**
- Produces: `artifacts.natemu_at('ecec5d77')`, the baseline build, keyed by the commit's tree.

- [ ] **Step 1:** Write `test_natemu_at_keys_by_tree`. Two revisions with the same tree must give the same cache key; build steps are stubbed.
- [ ] **Step 2:** Run it. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the test (PASS), then `tools/natemu-test run --tier bench -k m1-perf`. Expect `measured`, with `summary.md` in the run folder.
- [ ] **Step 5:** Commit.

## Part B: the Linux workload campaigns

### Task 5: container-built campaign images

**Files:**
- Create: `tools/correctness/recipes/<suite>/build.sh` for `openssl`, `torture`, `libm`, `gstr`, `x86st`, `ltp`, `cpy` (cpyt and cpyb), `stress`, `litmus` and `public`.
  - Each script runs the matching `remote.sh` / `*-remote.sh` stages inside the container as user `ubuntu`, with `W=/home/ubuntu/ct`, so the absolute paths the campaigns bake in stay the same.
  - It writes `/work/<suite>.cpio.zst`, `/work/native-run.tar` (what a native run needs) and `/work/manifest.json` (sources and their sha256).
- Create: `tools/correctness/recipes/common.sh`. It holds the snapshot apt setup and the play rootfs (`tools/play/rootfs.sh`'s package set). It also folds in the fixes that were only overlays: `/dev/stdin` and `/dev/fd` links in `init`, `mkcpio.py` mtimes set to `SOURCE_DATE_EPOCH`, and GNU `stat -c %s`.
- Create: the missing glue, as committed files:
  - the OpenSSL orig and Debian tarball pins (from the Debian `.dsc`);
  - `ltp-list.py` and `libm.py` invoked by the recipes;
  - `cpy-tests.txt` generated by `cpy-remote.sh pytest --list`;
  - `x86st-skip.txt`, with each entry's reason from correctness-x86-litmus.md;
  - herdtools7 7.58 and litmus-tests-x86 a6dadce pins;
  - the LKMM corpus generator for `public`.
- Modify: `testkit/inputs.py` (`campaign_image(suite: str) -> Path`, a `resources.derived` entry keyed by the recipe folder, `common.sh`, `tools/correctness/init` and `BENCH_BUILDER`; `timeout = 4 * 3600`)
- Test: `testkit/tests/test_campaign_inputs.py`

**Interfaces:**
- Produces: `inputs.campaign_image(suite) -> Path`, a folder with `<suite>.cpio.zst`, `native-run.tar` and `manifest.json`.

- [ ] **Step 1:** Write `test_campaign_image_key_changes_with_recipe` (stubbed build: editing `recipes/openssl/build.sh` changes the key) and `test_manifest_lists_every_download_with_sha256` (it parses each recipe's pin file).
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement `campaign_image` and the OpenSSL and torture recipes first. The other suites' recipes follow in Tasks 8-10.
- [ ] **Step 4:** Run the tests (PASS). Then build `campaign_image('openssl')` and boot it once by hand under the pinned QEMU with `boot.py`'s command line. Expect `CT-END`, and 246 `CT-RESULT openssl` lines.
- [ ] **Step 5:** Commit.

### Task 6: the campaign harness

**Files:**
- Create: `testkit/harness/campaign.py`
- Create: `tests/correctness/family.toml` (one family, one suite per campaign, all `tier = "campaign"`)
- Modify: `testkit/report.py` (new categories `flaky` and `unverified`, shown in the summary; `missing` is a failure)
- Test: `testkit/tests/test_campaign_harness.py`

**Interfaces:**
- Consumes: `inputs.campaign_image(suite)`, `resources.fetch('debian-netboot-linux')` (the 6.12.107 kernel `boot.py`'s `K` points to), `artifacts.natemu()`, `artifacts.fexc()`, `artifacts.qemu_system()`.
- Produces: suite keys:
  - `parts = N` (cases `part-0` … `part-(N-1)`);
  - `vcpus = [4]` (a matrix key `machine.vcpus`);
  - `cap = 3600`;
  - `normalize = "<module>:<fn>"` (which fields of a `CT-RESULT` line make the answer);
  - `flaky = {test = "why"}`.
- Produces: `boot(emu: str, image: Path, suite: str, part: str, vcpus: int, work: Path, cap: float) -> BootResult(reached: bool, serial: str, results: list[tuple[str, dict]])`. `emu` is `natemu` or `qemu`. Arguments are as in `tools/correctness/boot.py`. Each `work` folder is private.
- Produces: `parse(serial: str) -> list[tuple[str, dict]]`. It reads `CT-RESULT <kind> <name> k=v ...` lines, and the `CT-CASE`, `CT-HASH` and `CT-DMESG` lines a suite's normalizer needs.
- Produces: in `CaseResult.sub`, one `SubResult` per test:
  - `passed` when the normalized result hashes to the locked answer;
  - `failed` with `locked: <value>, got: <value>` (values are kept inline in the lock when under 1 KiB, `answers.py`);
  - `flaky`, or `missing`.
- Seeding: `TESTKIT_SEED_ANSWERS=1` boots under `qemu` and writes answers through `answers.py` like the runner harness. The `reference_overrides` value `x86` runs the test's command from `native-run.tar` on `NATEMU_X86_HOST`.

- [ ] **Step 1:** Write the tests:
  - `test_parse_ct_result_lines`: three kinds of line, and `sec=`/`bogo=` dropped by the default normalizer.
  - `test_part_without_ct_end_fails_with_serial_tail`
  - `test_locked_test_missing_from_output_fails`
  - `test_flaky_test_rerun_once_then_reported_flaky`
  - `test_parts_use_private_work_folders`

  `boot` is replaced by a fake that returns canned serial text.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement the harness and the `openssl` and `torture` suites:
  - openssl normalizer: `rc`, `ok`, `notok`;
  - torture normalizer: `rc` and `out`. `return-addr` is masked to `rc`.
  - `rules.py` excludes torture's 8 x87 cases, with the reason "natemu runs x87 in reduced precision (FEX)".
- [ ] **Step 4:** Run the tests (PASS). Then seed with `TESTKIT_SEED_ANSWERS=1 tools/natemu-test run --tier campaign tests/correctness/family.toml -k "openssl or torture"` twice, run `tools/natemu-test lock tests/correctness --from-run R1 R2`, and run it locked. Expect every test to pass except the excluded ones.
- [ ] **Step 5:** Commit.

### Task 7: seeding cross-check and stability

**Files:**
- Modify: `testkit/xref.py` (also takes campaign suites: QEMU against native per test, written like the programs report)
- Modify: `testkit/cli.py` (`lock --from-run` already refuses answers that differ between seeds; for campaign suites it names the test and suggests `flaky`)
- Test: `testkit/tests/test_lock_answers.py`

**Interfaces:**
- Consumes: Task 6's seeding output.

- [ ] **Step 1:** Write `test_two_seeds_agree_or_are_flagged`. Two seed logs that differ on one test must give exit 1 and a message naming that test and `flaky`. When that test is in the suite's `flaky`, the lock is written without it.
- [ ] **Step 2:** Run it. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it (PASS), then `tools/natemu-test xref tests/correctness --suite openssl --suite torture -o /tmp/x.json`. The recorded result is natemu equal to native on all 246 OpenSSL runs, and on torture apart from its documented differences.
- [ ] **Step 5:** Commit, adding the xref summary to docs/research/notes/2026-11-test-migration-findings.md.

### Task 8: libm, gstr and x86st

**Files:**
- Create: `tools/correctness/recipes/{libm,gstr,x86st}/build.sh`
- Modify: `tests/correctness/family.toml`, `tests/correctness/rules.py`
- Test: `testkit/tests/test_campaign_harness.py` (normalizer cases)

**Interfaces:**
- Normalizers:
  - libm and gstr: `rc`, where 77 counts as `unsupported`.
  - x86st: `rc`, `ok`, `fail`, `skip`. `out` is dropped, because it prints addresses.
- Rules:
  - libm: the 332 reduced-precision x87 tests and the 54 AVX-512 ones, each with its reason, listed from correctness-userspace.md.
  - x86st: one boot per test (`parts = 25`). `mov_ss_trap` and the four compat-mode tests are expected to stop the VM: `expect_stop = [...]`, reported as `unsupported`.

- [ ] **Step 1:** Write `test_libm_77_is_unsupported` and `test_x86st_drops_out_hash`.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement. Seed twice, lock, run locked.
- [ ] **Step 4:** Expect all to pass apart from the rules' exclusions.
- [ ] **Step 5:** Commit.

### Task 9: LTP and CPython

**Files:**
- Create: `tools/correctness/recipes/{ltp,cpy}/build.sh`
- Modify: `tests/correctness/family.toml`, `tests/correctness/rules.py`
- Test: `testkit/tests/test_campaign_harness.py`

**Interfaces:**
- ltp: `parts = 4`. Normalizer: the rc class (`pass` for 0, `tconf` for 32, else the rc). `run-ltp.sh` now keeps TCONF output, so the reason is visible on a failure. `flaky`: getrusage04, pselect01_64, kill11 (reasons from correctness-userspace.md). Rule: getcwd04 is excluded at `machine.vcpus = 1` ("needs 2 CPUs").
- cpyt: `parts = 12`. Normalizer: `CT-CASE` status per case. `flaky`: the three timerfd cases. cpyb: `CT-HASH` lines compared with the locked build hashes (seeded from QEMU's build of the same tree).

- [ ] **Step 1:** Write `test_ltp_rc_classes` and `test_cpy_case_status_parsed`.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement. Seed twice, lock, run locked.
- [ ] **Step 4:** Expect all to pass, with flaky cases reported as `flaky`.
- [ ] **Step 5:** Commit.

### Task 10: stress-ng, litmus and public (judged)

**Files:**
- Create: `tools/correctness/recipes/{stress,litmus,public}/build.sh`
- Create: `tests/correctness/judges.py` (wraps `stress-check.py`, `litmus-check.py` and `public-campaign.py:classify`)
- Modify: `tests/correctness/family.toml` (`kind = "judged"` for these three suites)
- Test: `testkit/tests/test_campaign_harness.py`

**Interfaces:**
- stress: each test's rc/pass/skip must equal the QEMU seed's (locked like an answer), with no `fail:` lines and no `BUG`/`Oops` in `CT-DMESG`.
- litmus: an observed state outside herd7's allowed set fails (`FORBIDDEN`). Allowed states that are never seen are reported in the message, not failed. The herd7 oracle is built by the recipe and stored in the image entry (`oracle/FAMILY.txt`).
- public: classify's categories map as follows:
  - `failure`, `identity-error` and the caps fail;
  - `skip` is `unsupported`;
  - `completed-needs-oracle-review` is `unverified`, a gate-visible category that is never `passed`.

- [ ] **Step 1:** Write the tests:
  - `test_litmus_forbidden_state_fails`
  - `test_litmus_unseen_allowed_state_passes_with_note`
  - `test_stress_dmesg_oops_fails`
  - `test_public_needs_review_is_unverified`
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the whole campaign tier once on this Mac: `tools/natemu-test run --tier campaign`. Record times with `natemu-test times --detail`.
- [ ] **Step 5:** Commit. Update docs/testing.md (a campaign section) and the README's one-line pointer.

## Not onboarded

- `run_io_stress.py --forced`: needs scheduling-hook binaries built separately.
- `repro.sh` / `run-repro.sh`: a debugging aid with no oracle.
- `x86con`: the same programs as x86st with console output, used to find where a stop happens. It stays a manual
  diagnostic.
