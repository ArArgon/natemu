# Campaign Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every long-running check natemu has (the four I/O and serial stress runners, the M1 performance check and the
ten Linux workload campaigns in `tools/correctness`) runs from `tools/natemu-test run --tier campaign` (or `--tier bench`)
on any Apple Silicon Mac, with inputs built on that Mac. Every campaign is self-contained: its own harness decides each
test, and no reference machine (QEMU or the x86 box) is involved.

**Architecture:** Campaigns are ordinary families with `tier = "campaign"` and `kind = "judged"`. Two pieces are new.
First, container-built inputs: a recipe folder run in the amd64 Debian container the benchmark image already uses
(`artifacts.BENCH_BUILDER`, through Rosetta), producing an initramfs or a tree as a cache entry. Second, a `campaign`
harness that boots one image part per case, reads the `CT-RESULT` lines and turns each test's own verdict into a
result. The four stress runners need only new inputs and runner cases.

**Tech Stack:** Python 3.11+, pytest (testkit), Apple `container` (amd64, Rosetta), Debian trixie snapshot packages,
natemu's stub/runtime builds (`artifacts.natemu()`).

**Spec:** docs/superpowers/specs/2026-11-17-test-framework-design.md (R1.4, R2, R3.6-R3.8, R12, R13, R14); campaign
behaviour as recorded in docs/restructure/correctness-*.md and docs/restructure/io-plan.md.

## Status (November 19)

- Done on `test-framework`, each task independently reviewed: Part 0, Part A (Tasks 1-4), Part B (Tasks 5-10, all ten
  Linux campaigns with their recipes, harness and expected lists) and Part C.
- Scheduler (owner, November 18): fair share between runs, `vmlock.py` queuing in the same queue, no idle exclusive
  grant, `natemu-test replay`, light claims for remote image builds; m1-perf as 21 resumable pieces.
- Host-dependent expectations: `expected_without.<feature>` (libm's 22 band-ue-flag tests on a Mac without AFP). libm
  is clean on the M2 Max (1,452 passed, 354 expected-fail) and on the M3 with AFP (1,474 passed, 332 expected-fail).
  Full-size stress-ng: every stressor run passed or was expected (cap 1,800 s; runs 20261119-043949-c2b0 and
  20261119-075513-6fa7). Other results: docs/research/notes/2026-11-test-migration-findings.md.
- public's LKMM corpus (Task 5's last input) is in since 31e059d9: 35 klitmus7 modules judged by herd7.
- Open: m1-perf set D (IO5), not rerun since November 18; CPython's timerfd cases on the M3 (they fail on both boots
  there, so the flaky entry does not cover them; docs/research/notes/2026-11-test-migration-findings.md).

## Format decisions (November 18, from Task 6)

1. `<suite>-cases.txt`: one line per test, `PART TEST`. PART is the boot's exact `part=` word, and TEST is the name
   its CT-RESULT line carries. Parts are the distinct PARTs in first-seen order, and their count is the suite's
   `parts`. Each recipe builds the list from the same files its run-S.sh reads, so the two cannot disagree.
2. stress-ng: a stressor that runs to its 240 s timeout passes when stress-ng's own result is rc 0 with no `fail:`
   lines. The timeout is noted, not failed (correctness-stress.md treats such runs as verified), so the plan's
   `timeout_ok` list is dropped.
3. CPython (cpyt): judged per module, as run-cpy.sh reports, with the failing cases named in the message.
   `expected`/`flaky` entries name the module, and their reason names the case: VSOCK; the timerfd cases in test_os.
4. A suite key `ct` (default: the suite's name) is the `ct=` word booted, for cpyt/cpyb, whose scripts are
   run-cpy.sh/run-cpyb.sh.
5. x86st tests that stop the VM (mov_ss_trap, the four compat-mode tests) go in x86st-skip.txt with their reason
   and report `unsupported`, not `expected`: a part that stops never reaches CT-END.
6. public: the case's index comes from the boot's own CT-BEGIN part. The id check (PUB-START/PUB-RESULT) still
   guards identity.

## Carried into Tasks 9-10 (review of the harness, November 18)

- cpyt: a module skipped as a whole has no `CT-RESULT cpy` line, so it would show as `missing`. Use regrtest's own
  per-module verdict (`CT-LOG |` and `CT-LOG rc=` lines, as cpy-check.py reads them) for modules without a line.
- public: pass the manifest row's guest cap as `pubcap=` on the kernel command line (run-public.sh otherwise falls
  back to 300 s).
- A flaky test that hangs the guest: the part fails for no CT-END whatever the rerun shows. Decide in Task 9 whether
  the rerun's results replace the first boot's for a part without CT-END.

## Decisions for the owner to review

1. **Campaign is a tier, not a kind** (owner, November 18): `tier = "campaign"`, `kind = "judged"`.
2. **Campaigns are self-contained** (owner, November 18): each test is judged by the campaign's own harness inside the
   guest, and nothing else is compared. No reference outputs are seeded, nothing is locked, and no QEMU or x86 run is
   needed.
   - LTP: exit code 0 passes; 32 (TCONF) is `unsupported`; anything else fails.
   - OpenSSL: rc 0 with `notok=0` passes.
   - torture: rc 0 passes (the programs abort on a wrong result).
   - libm and gstr: rc 0 passes; 77 is `unsupported`. AVX-512-only tests report 77 on natemu by themselves.
   - x86st: rc 0 with `fail=0` passes; skips are `unsupported`.
   - CPython: each regrtest case's status; cpyb passes when every build step exits 0.
   - stress-ng: rc 0, no `fail:` lines, and no BUG/Oops in the kernel log. Stressors listed in `timeout_ok` may end at
     the 240 s timeout.
   - litmus: herd7's x86-TSO allowed-state set, built by the recipe (a model in the image, not a reference run); an
     observed forbidden state fails.
   - public: `public-campaign.py`'s classify; "completed-needs-oracle-review" is `unverified`, never `passed`.
3. **Known non-passes are listed with their reason**, in the suite's `expected = {test = "why"}`:
   - natemu's documented deviations: reduced-precision x87 (torture's 8, libm's 332);
   - tests that cannot pass in this guest: VSOCK, ptrace `global.attach`, and getcwd04 at 1 vCPU.

   An expected test that fails reports `expected-fail` (not a failure). One that passes reports `unexpected-pass`,
   which fails the case so the list is corrected. Every entry cites the document where it was found.
4. **Timing-sensitive tests** (LTP getrusage04, pselect01_64, kill11; CPython's timerfd cases) are listed per suite in
   `flaky = {name = "why"}`. They are rerun once and report `flaky` instead of failing the campaign.
5. **Test lists live in the repo.** LTP's list (`ltp-list.py`, with its exclusions), CPython's module list and
   x86st's skip list are committed files, each with reasons, so a changed list shows in review.
6. **Native and QEMU runs stay available as diagnostics.** `tools/correctness/native.sh` and `boot.py`'s `qemu` mode are
   unchanged, but the framework never calls them.
7. **run_io_soak and run_serial_stress accept the framework's stress-ng build.** Their pinned binary hash came from a
   build kept only in natemu-raw. They get `--stress-provenance FILE`, a JSON file the recipe writes. The source pin
   (`ffe89071…52bd`, the same tarball the benchmark image uses) is unchanged.
8. **`--forced` io-stress runs** need separately built scheduling-hook binaries. They are left out and listed as not
   onboarded.

## Global Constraints

- Every input is built on the Mac from a recipe in the repo or a pinned download (sha256), never copied from
  natemu-raw (R14). Recipes pin Debian packages through snapshot.debian.org plus a `debs.sha256` file, as
  `tools/io/network-rootfs/container.sh` does.
- Campaigns use neither QEMU nor the x86 machine, at run time or ever (owner, November 18).
- Benchmarks are compared only on the same machine (R3.6).
- Campaign and bench suites never run in `--gate` (`testkit/select.py:70`).
- Locks stay TOML 1.0. Repro strings are shared through the lock's `[repro]` table (`answers.share_repro`).
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Work stays on `test-framework`; no push.

## Review Focus

- A recipe whose apt snapshot or URL is unreachable must fail preparation with the URL in the message (category
  `unpreparable`), not hang. Container builds take a `timeout`.
- A guest that never prints `CT-END` (hang, kernel panic, watchdog) must fail the case with the last 40 serial lines,
  not pass with zero results. Task 6 test: `test_part_without_ct_end_fails_with_serial_tail`.
- A test in the part's list but missing from a run's output (crashed mid-part) must fail as `missing`, not be
  skipped. The list comes from the image (`/ct/<suite>-cases.txt`). Task 6 test: `test_listed_test_missing_from_output_fails`.
- Two campaign parts must never write the same output folder or share an image copy that one of them modifies. Task 6
  test: `test_parts_use_private_work_folders`.
- An `expected` entry whose test now passes must not stay silently listed. Task 7 test:
  `test_expected_failure_that_passes_fails_the_case`.

---

## Part 0: clones, never copies

### Task 0: `cache.clone()` and per-run images as APFS clones

**Files:**
- Modify: `testkit/cache.py` (`clone(src: Path, dest: Path) -> None`: `clonefile(2)` through `cp -c` / `cp -cR`, falling
  back to a copy only where the volume is not APFS, and saying so once per run)
- Modify: `testkit/artifacts.py`, `testkit/inputs.py` (build outputs copied into entries go through `clone`)
- Modify: the runners' per-run copies of inputs larger than 1 MiB (the 38 `shutil.copy*`/`copytree` sites in `tools/run_*.py`
  and `tools/io_common.py`, starting with `run_net_linux.py`'s 85 MB tool tree), to `io_common.clone` (same behaviour)
- Modify: the disk-image runners (`io_common.IMAGE_FORMATS`) gain a matrix variant `disk.format = ["raw", "overlay"]`:
  a qcow2 overlay over the read-only cached base (natemu's backing-file path), next to the raw clone
- Test: `testkit/tests/test_cache.py`

- [ ] **Step 1:** Write `test_clone_shares_blocks_on_apfs`. Clone a 64 MiB file; the free space on the volume drops by
  less than 1 MiB and the clone's content equals the source. Skip where the volume is not APFS. Also write
  `test_clone_falls_back_to_copy` (with `cp -c` made to fail).
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement, then convert the call sites.
- [ ] **Step 4:** Run the tests (PASS) and the io family. Expect the same results as before, and `natemu-test times`
  showing no slower preparation.
- [ ] **Step 5:** Commit.

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
  - It writes `/work/<suite>.cpio.zst`, `/work/<suite>-cases.txt` (every test the image runs, by part) and `/work/manifest.json` (sources and their sha256).
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
- Produces: `inputs.campaign_image(suite) -> Path`, a folder with `<suite>.cpio.zst`, `<suite>-cases.txt` and `manifest.json`.

- [ ] **Step 1:** Write `test_campaign_image_key_changes_with_recipe` (stubbed build: editing `recipes/openssl/build.sh` changes the key) and `test_manifest_lists_every_download_with_sha256` (it parses each recipe's pin file).
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement `campaign_image` and the OpenSSL and torture recipes first. The other suites' recipes follow in Tasks 8-10.
- [ ] **Step 4:** Run the tests (PASS). Then build `campaign_image('openssl')` and boot it once by hand under natemu with `boot.py`'s command line. Expect `CT-END`, and 246 `CT-RESULT openssl` lines.
- [ ] **Step 5:** Commit.

### Task 6: the campaign harness

**Files:**
- Create: `testkit/harness/campaign.py`
- Create: `tests/correctness/family.toml` (one family, one suite per campaign, all `tier = "campaign"`, `kind = "judged"`)
- Create: `tests/correctness/verdicts.py` (one function per campaign: a `CT-RESULT` record to a verdict)
- Modify: `testkit/report.py` (new categories `flaky`, `expected-fail` and `unverified`, shown in the summary;
  `missing` and `unexpected-pass` are failures)
- Test: `testkit/tests/test_campaign_harness.py`

**Interfaces:**
- Consumes: `inputs.campaign_image(suite)`, `resources.fetch('debian-netboot-linux')` (the 6.12.107 kernel `boot.py`'s
  `K` points to), `artifacts.natemu()`, `artifacts.fexc()`.
- Produces: suite keys:
  - `parts = N` (cases `part-0` … `part-(N-1)`);
  - `vcpus = [4]` (a matrix key `machine.vcpus`);
  - `cap = 3600`;
  - `verdict = "verdicts:<fn>"`;
  - `expected = {test = "why"}`;
  - `flaky = {test = "why"}`.
- Produces: `boot(image: Path, suite: str, part: str, vcpus: int, work: Path, cap: float) -> BootResult(reached: bool, serial: str)`.
  It runs natemu with `tools/correctness/boot.py`'s command line. Each `work` folder is private.
- Produces: `parse(serial: str) -> list[tuple[str, dict]]`. It reads `CT-RESULT <kind> <name> k=v ...` lines, plus the
  `CT-CASE` and `CT-DMESG` lines a suite's verdict needs.
- Produces: verdict functions `(name: str, fields: dict, extra: list[str]) -> tuple[str, str]`, returning
  (`passed`|`failed`|`unsupported`, message).
- Produces: in `CaseResult.sub`, one `SubResult` per listed test, from its verdict, then adjusted:
  - `expected` and failed: `expected-fail`;
  - `expected` and passed: `unexpected-pass`;
  - `flaky` and failed: reruns the part once, then `flaky` if it still fails;
  - listed but absent from the output: `missing`.

- [ ] **Step 1:** Write the tests:
  - `test_parse_ct_result_lines`
  - `test_part_without_ct_end_fails_with_serial_tail`
  - `test_listed_test_missing_from_output_fails`
  - `test_flaky_test_rerun_once_then_reported_flaky`
  - `test_parts_use_private_work_folders`
  - `test_openssl_notok_fails` (`rc=0 ok=10 notok=1` fails)

  `boot` is replaced by a fake that returns canned serial text.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement the harness and the `openssl` and `torture` suites. torture's `expected` lists its 8 x87
  cases with the reason "natemu runs x87 in reduced precision (FEX)" (docs/restructure/correctness-userspace.md).
- [ ] **Step 4:** Run the tests (PASS), then `tools/natemu-test run --tier campaign tests/correctness/family.toml -k "openssl or torture"`.
  Expect all 246 OpenSSL runs and every torture program to pass, apart from torture's 8 `expected-fail`.
- [ ] **Step 5:** Commit.

### Task 7: keeping the expected list honest

**Files:**
- Modify: `testkit/harness/campaign.py`, `testkit/report.py`
- Test: `testkit/tests/test_campaign_harness.py`

- [ ] **Step 1:** Write the tests:
  - `test_expected_failure_that_passes_fails_the_case`;
  - `test_expected_entry_for_unknown_test_is_reported`: an entry naming no listed test is a `stale-entry` failure,
    so renamed tests surface.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them. Expect PASS.
- [ ] **Step 5:** Commit.

### Task 8: libm, gstr and x86st

**Files:**
- Create: `tools/correctness/recipes/{libm,gstr,x86st}/build.sh`
- Modify: `tests/correctness/family.toml`, `tests/correctness/verdicts.py`
- Test: `testkit/tests/test_campaign_harness.py` (verdict cases)

**Interfaces:**
- Verdicts:
  - libm and gstr: rc 0 passes; 77 is `unsupported` (this includes the 54 AVX-512-only tests, which glibc skips on a
    CPU without AVX-512).
  - x86st: rc 0 with `fail=0` passes.
- `expected`:
  - libm: the 332 reduced-precision x87 tests, each with its reason, listed from correctness-userspace.md.
  - x86st: one boot per test (`parts = 25`). `mov_ss_trap` and the four compat-mode tests stop the VM; they are
    `expected` with the reason from correctness-x86-litmus.md.

- [ ] **Step 1:** Write `test_libm_77_is_unsupported` and `test_x86st_fail_count_fails`.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement, then run the suites.
- [ ] **Step 4:** Expect all to pass apart from `expected-fail` entries.
- [ ] **Step 5:** Commit.

### Task 9: LTP and CPython

**Files:**
- Create: `tools/correctness/recipes/{ltp,cpy}/build.sh`
- Modify: `tests/correctness/family.toml`, `tests/correctness/verdicts.py`
- Test: `testkit/tests/test_campaign_harness.py`

**Interfaces:**
- ltp: `parts = 4`. Verdict: 0 passes, 32 (TCONF) is `unsupported`, anything else fails. `run-ltp.sh` now keeps the
  output of TCONF and failing entries, so the reason is visible. `flaky`: getrusage04, pselect01_64, kill11 (reasons
  from correctness-userspace.md). `expected`: getcwd04 at `machine.vcpus = 1` ("needs 2 CPUs").
- cpyt: `parts = 12`. Verdict: each `CT-CASE` status (pass, skip as `unsupported`, fail or error). `flaky`: the three
  timerfd cases. `expected`: the VSOCK case ("no vsock device in the guest").
- cpyb: the build steps' rc, all 0.

- [ ] **Step 1:** Write `test_ltp_rc_classes` and `test_cpy_case_status_parsed`.
- [ ] **Step 2:** Run them. Expect FAIL.
- [ ] **Step 3:** Implement, then run the suites.
- [ ] **Step 4:** Expect all to pass, with flaky cases reported as `flaky`.
- [ ] **Step 5:** Commit.

### Task 10: stress-ng, litmus and public (judged)

**Files:**
- Create: `tools/correctness/recipes/{stress,litmus,public}/build.sh`
- Modify: `tests/correctness/verdicts.py` (wraps `stress-check.py`'s scan, `litmus-check.py` and `public-campaign.py:classify`)
- Modify: `tests/correctness/family.toml`
- Test: `testkit/tests/test_campaign_harness.py`

**Interfaces:**
- stress: rc 0 (or the 240 s timeout for stressors in `timeout_ok`: mutex, mtx, pipeherd, sem, schedmix, yield,
  vforkmany, which run to it natively too), no `fail:` lines and no `BUG`/`Oops` in `CT-DMESG`. The OPS counts in
  `stress-list.txt` stay as committed.
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

## Part C: QEMU answers only where nothing else knows the right value

Owner, November 18: where a test's own harness, model or written expectation decides correctness, a QEMU answer is not
needed. An audit of every answer site (11,882 faults, 8,304 smp and 1,640 system answers) sorted them into three
groups. **Keep**: natemu compared value for value with QEMU, and nothing else knows the value. **Drop**: QEMU only
cross-checks an expectation the test already has. **Static**: the value can be written down from the architecture.
Each row is one task in the usual five steps:
1. a test pinning the runner's verdict without the answer;
2. run it, expect FAIL;
3. remove the lookup, or add the static expectation;
4. run the family, expect the same results;
5. drop the lock entries with `natemu-test lock`, then commit.

| Runner (family/cases) | Answers | Change |
| --- | --- | --- |
| slice1_faults, slice3 corpora (random corpora) | most of faults; 6,886 shared with smp | keep |
| slice1_faults built-in probes (`/rip/*`, `/relocated`) | about 3,500 | static: a three-instruction prelude on known registers |
| slice2_faults `#signal-context` | 405 | drop (QEMU checked against QEMU) |
| slice2_faults (the rest) | 2,736 | keep |
| slice2_probes loop programs | 21 | static: a small interpreter of the probes' ALU ops (`add_chain` already does it for long loops) |
| slice3 kicks, contain control, coherence | bare + `#exit` | static, as `model_smp.py` does for `smp` |
| slice3 sharedpt | 16 | drop: `sharedpt.judge` decides; add the data pages to the judge |
| slice3 system, segments, events | about 610 | drop the cross-checks (`QEMU_DIFFERS`, `QEMU_HANGS`, `QEMU_EVENTS_*`); static registers |
| i5b-faults | 261 | drop: the SDM rule decides |
| i5b-images | 46 | static XSAVE/FXSAVE images |
| s4, x1, x3, l4a, l4b, m3 | about 200 | drop: the models decide; add their few registers to the expectations |
| r1a, r1b | about 60 | drop, after the judge covers r1a's variant programs (today only the QEMU compare catches them) |
| l3 | UART | static: 16550A register values |
| k1, m2, x4 | bare + `#exit` | static: the programs write known constants |

programs and fp keep QEMU's answers: they compare final register and memory state, value for value.

## Not onboarded

- `run_io_stress.py --forced`: needs scheduling-hook binaries built separately.
- `repro.sh` / `run-repro.sh`: a debugging aid with no oracle.
- `x86con`: the same programs as x86st with console output, used to find where a stop happens. It stays a manual
  diagnostic.
