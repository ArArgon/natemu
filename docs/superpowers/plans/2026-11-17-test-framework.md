# Test Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pytest-based test framework (`testkit`) that runs natemu's unit, integration and wrapped legacy tests on any
Apple Silicon Mac without `natemu-raw`, with locked answers, a content-addressed cache with cleanup, machine-wide claims
with exclusive windows, declared families and variants, change-based selection and detached runs.

**Architecture:** pytest is the engine: our plugin collects declared families (`tests/<family>/family.toml`) into
items, and replaces pytest's run loop (`pytest_runtestloop`) with a controller that asks a per-machine claim pool before
starting each batch, runs batches in worker subprocesses (`python -m pytest` on node ids), and replays their serialized
reports into the controller session, so pytest's reporting, `-k`, JUnit and summaries work unchanged. Everything a
machine builds or downloads lives in one content-addressed cache shared by its worktrees; answers live only as hashes in
per-family `tests.lock` files.

**Tech Stack:** Python ≥ 3.11 (stdlib `tomllib`), pytest 9.2.0, tomli-w, hypothesis (framework tests only),
cargo-nextest (fetched as a pinned resource), Apple `container` for QEMU seeding, cargo `+1.97.0`.

**Spec:** `docs/superpowers/specs/2026-11-17-test-framework-design.md` (frozen v1). Requirement numbers (R…) below are the
spec's.

## Decisions taken for the spec's open questions

- Approach A (owner, November 17).
- Detached runs (open question 2): `natemu-test submit` starts a detached run; `run` stays a foreground command. Both use
  the same file-backed pool; no per-machine service (spec R7.4 satisfied by submit/status/wait).
- Lock granularity (9): one `tests.lock` per family.
- Remote execution (5): optional, last task (owner: optional if testing is a hurdle).
- Campaigns and benchmarks (R12, R3.6–R3.7): out of this plan; their steps run as wrapped legacy items (Task 15). Guest
  content builds on the Mac (open question 12) come with their onboarding.
- Time targets (4): smoke ≤ 10 min on an M2 Max; the gate no slower than `run_suite.py` on the same machine (measured in
  Task 16).

## Global Constraints

- Python ≥ 3.11; third-party packages only from `testkit/requirements.lock`, installed with `pip --require-hashes`
  (R11.2). No other dependency.
- No stored data (R1.2): nothing reads or writes `natemu-raw` except wrapped legacy items, which are labelled `wrapped`
  in every report (R13.1).
- Cache root: `$NATEMU_TEST_CACHE`, default `~/Library/Caches/natemu/testkit` (machine-wide, shared by worktrees). Machine
  state (pool, durations, run registry): `<cache root>/state/`. Run outputs: `<worktree>/.natemu/runs/<run-id>/`. Machine
  list: `<worktree>/.natemu/machines.toml`. `.natemu/` is gitignored (R9.1).
- Hashes are derived by tools, never hand-written, except download pins in `tests/resources.toml` (R2.5).
- Cargo is invoked as `cargo +1.97.0 … --offline --locked`, as today.
- Custom result categories, used verbatim in reports and code: `excluded`, `not-runnable`, `stale-lock`,
  `build-failure`, `unpreparable`, `reused`, `unsupported`, `wrapped`.
- A gate (`--gate`) fails on any failure, error, `stale-lock`, `build-failure`, `unpreparable`, `not-runnable`, or a
  changed exclusion set (R5.8).
- Never touch another machine's checkout; remote work uses scratch clones of a named commit.

## Review Focus

1. Two worktrees ask for the same cache key at once (same natemu build, same corpus): exactly one builds, the other waits
   and uses the result. Test in Task 3.
2. A builder or download dies half-way (SIGKILL, full disk): no partial entry is ever served; the next request rebuilds.
   Test in Task 3 and Task 4.
3. A worker subprocess is killed or times out mid-batch: its claim is released, its unfinished items are reported as
   errors (not lost, not passed), the run continues. Test in Task 10.
4. A selection that matches nothing (`--tier affected` with only docs changed, a typo in `-k`): a plain message listing
   why, exit status 5, and a gate with nothing selected fails. Test in Task 11.
5. No network while preparing: every item needing the resource is `unpreparable` with the URL and error; items not
   needing it still run. Test in Task 4 and Task 10.

---

## File structure

```text
tools/natemu-test                 sh bootstrap: venv from requirements.lock (cached by its hash), exec python -m testkit
testkit/requirements.in, .lock    pytest==9.2.0, tomli-w, hypothesis (lock: uv pip compile --generate-hashes)
testkit/pytest.ini                testpaths = tests; -p testkit.plugin; markers
testkit/__main__.py               CLI: run, submit, status, wait, lock, check-lock, prepare, gc
testkit/host.py                   machine facts
testkit/cache.py                  content-addressed entries, build locks, leases, roots, cleanup
testkit/resources.py              declared downloads and derived recipes
testkit/sched/policy.py           pure scheduling decisions (fake-clock testable)
testkit/sched/pool.py             file-backed machine pool over policy; old .vm.lock interop; status data
testkit/durations.py              recorded durations per item
testkit/family.py                 family.toml schema, suites, variants, features, claims, rules
testkit/collect.py                pytest collector for family.toml → items
testkit/lock.py                   fingerprints, tests.lock I/O, stale/exclusion checks, check-lock
testkit/runloop.py                controller (batches, pool, workers, report replay) and worker mode
testkit/prepare.py                preparation phase (artifacts, resources, inputs) before tests start
testkit/select.py                 tiers, smoke, affected (path rules + git diff)
testkit/report.py                 custom categories, gate verdict, run dir summary
testkit/runs.py                   run registry, submit/status/wait
testkit/artifacts.py              natemu binaries, fexc, nextest, QEMU reference
testkit/harness/programs.py       straight-line program harness, normalizer, seeding backends, reuse store
testkit/harness/legacy.py         wrapped run_suite step
testkit/harness/units.py          nextest and Python tool-test suites with per-test sub-results
testkit/tests/                    the framework's own tests (pytest, no VM)
tests/features.toml               feature registry (detect, hide)
tests/resources.toml              external resources (URLs, pins)
tests/selection.toml              shared-machinery paths for affected selection
tests/unit/family.toml            Rust and Python unit suites
tests/fp/family.toml, rules.py    i8 and i8b deterministic suites, FP exclusion rule
tests/fp/tests.lock               generated
tests/legacy/family.toml          wrapped run_suite steps
docs/testing.md                   how to run, write and lock tests
```

---

### Task 1: Environment, package skeleton and CLI

**Files:**
- Create: `tools/natemu-test`, `testkit/requirements.in`, `testkit/requirements.lock`, `testkit/pytest.ini`,
  `testkit/__init__.py`, `testkit/__main__.py`, `testkit/plugin.py`, `testkit/tests/test_cli.py`
- Modify: `.gitignore` (add `.natemu/`)

**Interfaces:**
- Produces: `python -m testkit <command> [args]`; `testkit.paths.cache_root() -> Path`, `state_dir() -> Path`,
  `runs_dir(worktree: Path) -> Path`, `repo_root() -> Path` (in `testkit/paths.py`).

- [ ] **Step 1:** Write `testkit/tests/test_cli.py::test_help_lists_commands`: `python -m testkit --help` exits 0 and its
  output contains each of `run submit status wait lock check-lock prepare gc`.
- [ ] **Step 2:** `test_paths_default_and_override`: with `NATEMU_TEST_CACHE` unset, `cache_root()` ends with
  `Library/Caches/natemu/testkit`; with it set to a tmp dir, `cache_root()` is that dir and `state_dir()` is
  `<it>/state`.
- [ ] **Step 3:** Run them; expect failures (no module).
- [ ] **Step 4:** Write `requirements.in` (`pytest==9.2.0`, `tomli-w`, `hypothesis`), generate the lock with
  `uv pip compile --generate-hashes --python-version 3.11 testkit/requirements.in -o testkit/requirements.lock`.
- [ ] **Step 5:** Write `tools/natemu-test`: venv at `<cache root>/venv/<sha256 of requirements.lock, 16 hex>/`, created
  with `python3 -m venv` and `pip install --require-hashes -r testkit/requirements.lock` when missing (atomic: build in
  `…tmp-<pid>` then `mv`), then `exec <venv>/bin/python -m testkit "$@"` with `PYTHONPATH=<repo>`.
- [ ] **Step 6:** Implement `paths.py`, argparse CLI with the eight subcommands (stubs that exit 2 "not yet" except
  `--help`), `pytest.ini` (`testpaths = tests`, `addopts = -p testkit.plugin -p no:cacheprovider`).
- [ ] **Step 7:** `tools/natemu-test -m pytest testkit/tests -q` → the two tests pass (the CLI's `-m pytest` passthrough is
  how framework tests run).
- [ ] **Step 8:** Commit `testkit: bootstrap, paths and CLI skeleton`.

### Task 2: Machine facts

**Files:** Create `testkit/host.py`, `testkit/tests/test_host.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) Host(machine_id: str, chip: str, p_cores: int, e_cores: int, os_build: str,
  features: dict[str, bool])`; `host.detect() -> Host` (cached per process); `host.parse_sysctl(text: str) -> dict`.

- [ ] **Step 1:** Tests: `test_parse_features` feeds `hw.optional.arm.FEAT_AFP: 0\nhw.optional.arm.FEAT_SB: 1\n` and
  expects `{'afp': False, 'sb': True}` (lowercased, `FEAT_` stripped); `test_detect_real_machine` asserts
  `detect().p_cores >= 1`, `machine_id` is 36 characters, `'afp' in features`.
- [ ] **Step 2:** Run; expect failure.
- [ ] **Step 3:** Implement: `sysctl hw.optional.arm`, `sysctl -n hw.perflevel0.physicalcpu hw.perflevel1.physicalcpu
  machdep.cpu.brand_string`, `sw_vers -buildVersion`, `ioreg -rd1 -c IOPlatformExpertDevice` (`IOPlatformUUID`).
- [ ] **Step 4:** Tests pass. Commit `testkit: machine facts`.

### Task 3: Content-addressed cache, leases and cleanup

**Files:** Create `testkit/cache.py`, `testkit/tests/test_cache.py`

**Interfaces:**
- Produces:
  - `cache.key(*parts: str | bytes | Path) -> str` (sha256 hex; a `Path` contributes its file bytes, or for a directory
    the sorted relative names and file bytes).
  - `cache.get_or_build(kind: str, key: str, build: Callable[[Path], None], *, size_hint: int = 0) -> Path`: returns
    `<root>/<kind>/<key>/`; `build(tmpdir)` fills a temporary dir which is renamed into place only when it returns; one
    builder per key machine-wide (flock on `<root>/locks/<kind>-<key>`), others wait; a leftover temp dir is removed.
  - `cache.lease(run_id: str, paths: Iterable[Path]) -> None`; `cache.release(run_id)`; leases live in
    `state/leases/<run_id>.json` with the owner pid.
  - `cache.register_worktree(path: Path)`; `cache.gc(cap_bytes: int, roots: set[Path]) -> list[Path]` deletes
    least-recently-used entries (by `last-used` stamp touched on every `get_or_build`) not leased by a live pid and not in
    `roots`, until under `cap_bytes`; returns what it deleted.
  - Default cap: 100 GiB, overridable by `NATEMU_TEST_CACHE_CAP` (bytes, or with a G suffix).

- [ ] **Step 1:** Tests:
  - `test_build_once_concurrently`: two processes call `get_or_build('x', k, slow_build)` at once; `slow_build` appends
    to a counter file; counter == 1 and both get the same path.
  - `test_killed_builder_never_served`: a child starts building and is SIGKILLed mid-way; the next `get_or_build`
    rebuilds and no temp dir remains.
  - `test_gc_respects_leases_and_roots`: three entries over cap; one leased by a live pid, one in roots; gc deletes only
    the third; a lease whose pid is dead does not protect.
  - `test_key_directory_stable`: same tree in two places → same key; one byte changed → different key.
- [ ] **Step 2:** Run; expect failures.
- [ ] **Step 3:** Implement as specified; publish with `os.rename` of the finished temp dir (same filesystem).
- [ ] **Step 4:** Tests pass. Commit `testkit: content-addressed cache with leases and cleanup`.

### Task 4: Resources

**Files:** Create `testkit/resources.py`, `tests/resources.toml`, `testkit/tests/test_resources.py`

**Interfaces:**
- Consumes: `cache.get_or_build`.
- Produces: `resources.fetch(name: str) -> Path` (verified file in the cache); `resources.derived(name: str, recipe:
  Callable[[Path], None], inputs: list[Path | str]) -> Path` (cache kind `derived`, key over recipe source, inputs and
  resource pins); exception `Unpreparable(name: str, reason: str)`.
- `tests/resources.toml` entry: `[resource.<name>] urls = [...]` (tried in order), `sha256 = "…"`, optional
  `extract = "<member path>"` (from `.deb` via `ar`+`tar`, or `.tar.*`), `size` (bytes, informational).
- Download client: `urllib.request` with User-Agent `natemu-testkit` and nothing else identifying (R14.8).

- [ ] **Step 1:** Tests (with a local `http.server` in a thread serving a fixture file):
  - `test_fetch_verifies_pin`: correct pin → path; wrong pin → `Unpreparable` naming both hashes; nothing cached.
  - `test_fetch_falls_back_to_second_mirror`: first URL 404 → second used.
  - `test_fetch_no_network`: unreachable URLs → `Unpreparable` with the URL and error text.
  - `test_derived_rebuilds_when_recipe_changes`: changing the recipe function's source changes the key.
- [ ] **Step 2:** Run; expect failures. **Step 3:** Implement. **Step 4:** Pass. Commit `testkit: pinned resources`.

### Task 5: Scheduling policy (pure)

**Files:** Create `testkit/sched/__init__.py`, `testkit/sched/policy.py`, `testkit/tests/test_policy.py`

**Interfaces:**
- Produces:
  - `@dataclass Request(id: str, owner_pid: int, cores: int, exclusive: bool, perf: bool, priority: int, asked: float,
    expected: float | None, label: str, worktree: str)` — `perf` means performance cores only; priority higher first
    (`INTERACTIVE = 10`, `GATE = 0`).
  - `@dataclass Grant(request: Request, start: float, core_class: str)` with `core_class in {'P', 'E', 'X'}` (`X` = the
    whole machine).
  - `@dataclass Config(p_cores: int, e_cores: int, window_budget: float = 600, min_shared: float = 120)`.
  - `@dataclass State(held: list[Grant], waiting: list[Request], window_open_since: float | None,
    last_window_closed: float | None)`.
  - `policy.decide(state: State, cfg: Config, now: float) -> list[tuple[Request, str]]`: the requests to grant now with
    their core class. Rules, in this order:
    1. While an exclusive grant is held, grant nothing else.
    2. Exclusive waiters form the window queue (priority, then `asked`). A window is *pending* while that queue is
       non-empty. If none are held and `now - last_window_closed >= min_shared` or no shared work is waiting, open a
       window and grant the head exclusive request (`X`) when nothing else is held.
    3. Inside an open window, grant the next exclusive request when the previous ends, until the queue is empty or
       `now - window_open_since >= window_budget` (a single over-budget request still runs alone); then close it.
    4. Shared requests by (priority desc, asked asc); first come first served within a priority: a request is not
       granted while an earlier same-priority request that does not fit is waiting.
    5. While a window is pending, a shared request is granted only if `expected` is known and `now + expected <= drain
       horizon` (the latest expected end among held shared grants); unknown durations wait.
    6. Core classes: `perf` requests take P; others take P if free, else E. Budget: sum of held cores per class ≤ that
       class's cores.
    7. A request with `cores > p_cores` (perf) or `> p_cores + e_cores` is never granted; `decide` leaves it waiting and
       `policy.unsatisfiable(request, cfg) -> bool` reports it (the caller makes it `not-runnable`, R4.2).
- [ ] **Step 1:** Unit tests with explicit times: `test_exclusive_never_overlaps`, `test_window_batches_exclusives`,
  `test_window_budget_closes`, `test_min_shared_interval_after_window`, `test_backfill_only_short_known`,
  `test_priority_before_age`, `test_fifo_large_claim_not_starved`, `test_e_cores_for_non_perf_only`,
  `test_unsatisfiable`.
- [ ] **Step 2:** Property test `test_random_workloads_invariants` (hypothesis, a simulator that advances a fake clock to
  the next end time and calls `decide`): over random mixes of up to 40 requests (cores 1–6, exclusive 20%, perf 40%,
  durations 1–300 s, two priorities), assert at every instant: no overlap with an exclusive grant; per-class budget held;
  every satisfiable request is eventually granted (simulation ends with an empty queue); shared work is granted within
  `window_budget + min_shared + max duration` of asking.
- [ ] **Step 3:** Run; expect failures. **Step 4:** Implement `decide`. **Step 5:** All pass. Commit
  `testkit: scheduling policy with exclusive windows and backfill`.

### Task 6: Machine pool

**Files:** Create `testkit/sched/pool.py`, `testkit/tests/test_pool.py`

**Interfaces:**
- Consumes: `policy.decide`, `Request`, `Config`, `host.detect`.
- Produces: `Pool(state_dir: Path, cfg: Config | None = None)`, `pool.submit(req: Request) -> None`,
  `pool.poll(req_id: str) -> Grant | None` (runs `decide` under the flock, prunes dead pids, records grants),
  `pool.release(req_id: str)`, `pool.snapshot() -> State`, `pool.waiting_reason(req_id) -> str` (e.g.
  `"window: 3 exclusive tests ahead (l4b from worktree pcid-fix), ~240 s"`).
- Interop (R4.7, R4.9): while it holds any shared VM grant the pool holds a shared `flock` on
  `<natemu-raw>/.vm.lock` if that folder exists; a window holds it exclusively; before opening a window it waits until
  `run_suite.idle()`'s process checks find no natemu or QEMU VM outside the window (reuse `tools/run_suite.py:idle`'s
  checks as a function `pool.outside_vms() -> list[str]`).

- [ ] **Step 1:** Tests: `test_two_processes_share_budget` (two processes submit 3 × 2-core requests on a 4-core config;
  log intervals; never > 4 cores); `test_dead_owner_released` (SIGKILL a holder; another request is granted within 1 s);
  `test_waiting_reason_names_blocker`; `test_vm_lock_interop` (an external `flock -x` on a fake `.vm.lock` delays a
  window until released).
- [ ] **Step 2:** Run; fail. **Step 3:** Implement (state JSON written atomically under the flock). **Step 4:** Pass.
  Commit `testkit: machine claim pool`.

### Task 7: Durations

**Files:** Create `testkit/durations.py`, `testkit/tests/test_durations.py`

**Interfaces:** `durations.expected(nodeid: str) -> float | None`; `durations.record(nodeid: str, seconds: float)`
(EWMA, α = 0.3, stored in `state/durations.json` under flock).

- [ ] **Step 1:** `test_record_and_expected`: record 10 then 20 → 13.0; unknown → None. **Step 2–4:** fail, implement,
  pass. Commit `testkit: recorded durations`.

### Task 8: Families, suites, variants and claims

**Files:** Create `testkit/family.py`, `testkit/collect.py`, `testkit/report.py`, `tests/features.toml`,
`testkit/tests/test_family.py`, `testkit/tests/fixtures/fam/family.toml`

**Interfaces:**
- Produces:
  - `family.load(path: Path) -> Family`; `Family(name, dir, covers: list[str], rules: Callable | None, suites:
    list[Suite], inputs: dict[str, InputDecl])`; `Suite(name, kind: Literal['deterministic','judged','legacy','units'],
    harness: str, input: str | None, reference: Literal['qemu','x86'] | None, matrix: dict[str, list], needs: Needs,
    smoke: list[str] | bool, batch: int, judge: str | None, params: dict, rerun: int, extra: dict)`;
    `Needs(cores: int = 1, exclusive: bool = False, why: str = '', perf: bool = False, dedicated: bool = False,
    network: bool = False)` — `exclusive` requires `why` (load error otherwise); `dedicated` implies `perf`.
  - `family.expand(suite) -> list[Variant]`; `Variant(params: dict[str, object], id: str)` where `id` is
    `k=v,...` over the last path component of each matrix key, sorted (e.g. `afp=False,fp=fast`).
  - Matrix keys are field paths: `engine.*`, `machine.*` (passed to the harness as natemu settings) and `host.<feature>`
    (host requirement).
  - `features.toml`: `[feature.afp] detect = "afp"`, `hide_env = { NATEMU_FEXC_FEATURE_OFF = "afp" }` (transitional,
    R5.2).
  - `family.placement(variant, host: Host, require_real: set[str]) -> tuple[bool, dict[str, str], str]`: runnable?,
    environment for hiding, and the way each requirement is met (`real`/`hidden`), or the reason it is not runnable
    (R5.3, R5.4a).
  - `rules`: optional `rules.py` in the family dir defining `applies(suite: str, case: str, variant: Variant, met:
    dict[str, str]) -> str | None` (exclusion reason or None).
  - Collector: `pytest_collect_file` yields a `FamilyFile` for `family.toml`; items are `CaseItem` with node id
    `tests/<family>/family.toml::<suite>::<case>[<variant id>]`; case names come from the family's `tests.lock`
    (Task 9), or for `legacy`/`units` suites from the harness's `cases(suite)`.
  - `report.py`: `pytest_report_teststatus` maps `user_properties['testkit']` to the categories listed under Global
    Constraints; `report.gate_verdict(stats) -> tuple[bool, list[str]]`.
- [ ] **Step 1:** Tests against the fixture family (3 cases, matrix `engine.fp=[fast,accurate]` × `host.afp=[true,false]`,
  a `rules.py` excluding case `c1` when `fp=fast` and `afp` met hidden or real-absent):
  - `test_expand_ids`: four variant ids, exactly `afp=False,fp=accurate` … sorted.
  - `test_exclusive_needs_why`: a suite with `exclusive = true` and no `why` → `FamilyError`.
  - `test_placement_hidden_and_real`: on a host with afp, `afp=False` is runnable `hidden` with the hide env; with
    `require_real={'afp'}` it is not runnable with reason `needs afp absent (real)`; on a host without afp, `afp=True`
    is not runnable `needs afp`.
  - `test_collect_reports_excluded_with_reason`: pytester run shows `excluded` for `c1[afp=…,fp=fast]` with the reason in
    the summary, and nothing silent (counts add up to 12).
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass. Commit `testkit: families, variants, placement`.

### Task 9: Lock, fingerprints and check-lock

**Files:** Create `testkit/lock.py`, `testkit/tests/test_lock.py`

**Interfaces:**
- Consumes: `cache.key`, `Family`, harness `normalizer_sources()` (Task 14).
- Produces:
  - `tests.lock` schema: `[input.<name>] fingerprint, output` and `[suite.<suite>] fingerprint, reference, seeded = {tool,
    pin, machine, date}`, `[suite.<suite>.cases] <case> = "<answer sha256>"`, `[suite.<suite>.excluded] "<variant id>" =
    [cases…]`.
  - `lock.fingerprint_input(fam, name) -> str`: generator source (+ files it imports from the repo, listed in the input
    declaration as `sources = [...]`), args. Never local tool versions (R2.7).
  - `lock.fingerprint_suite(fam, suite) -> str`: input fingerprint, guest-visible matrix keys (`machine.*`), normalizer
    sources, reference pin. Not `needs`, `smoke`, `batch`, timeouts.
  - `lock.read(fam) -> Lock`, `lock.write(fam, Lock)` (tomli-w, sorted keys, stable output).
  - `lock.status(fam, suite) -> Literal['ok','stale','missing']`.
  - `lock.check(fam) -> list[str]`: stale entries; a suite whose distinct answer hashes are fewer than 10% of its cases
    (and more than 20 cases) ("normalizer may discard information"); a `seeded.tool` other than the suite's declared
    reference.
- [ ] **Step 1:** Tests: `test_fingerprint_ignores_scheduling_fields` (changing `needs`, `smoke`, `batch` keeps it;
  changing generator args changes it); `test_stale_detected`; `test_write_is_stable` (write, read, write → identical
  bytes); `test_check_flags_collapsed_answers`; `test_check_flags_wrong_reference`.
- [ ] **Step 2–4:** fail, implement, pass. Commit `testkit: locks and fingerprints`.

### Task 10: Run loop, preparation and runs

**Files:** Create `testkit/runloop.py`, `testkit/prepare.py`, `testkit/runs.py`, `testkit/tests/test_runloop.py`,
`testkit/tests/fixtures/sleepfam/family.toml` (a `judged` suite whose harness `sleep` sleeps `params.seconds` and passes
or fails by `params.ok`)

**Interfaces:**
- Consumes: pool, durations, placement, report categories, `cache.lease`.
- Produces:
  - Plugin hook `pytest_runtestloop(session)` (controller unless `--testkit-worker`): groups items into units
    `(family, suite, variant)` chunks of `suite.batch` items; orders by expected duration desc; for each unit submits a
    `Request` (cores, exclusive, perf from `Needs`; priority from `--priority interactive|gate`, default `interactive`,
    `gate` when `--gate`); on grant spawns `python -m pytest -c testkit/pytest.ini --testkit-worker <run>/units/<n>.ids
    --testkit-report <run>/units/<n>.jsonl -p no:terminal` (prefixed `taskpolicy -c background` for class `E`), with the
    variant's hide env; tails the JSONL and replays each report via `config.hook.pytest_report_from_serializable` and
    `pytest_runtest_logreport`; records durations; releases on exit. A unit whose worker dies reports every item without
    a report as error `worker died (signal N)`. Per-unit timeout: `params.timeout`, default 3600 s.
  - Worker mode: collects only the given node ids, runs them in order, writes `pytest_report_to_serializable` JSON lines.
  - `prepare.prepare(items) -> dict[nodeid, str]`: runs each item's harness `requirements(item)` (artifacts, resources,
    inputs) once per distinct requirement, holding a shared CPU claim (never in a window, R14.4); failures map items to
    `unpreparable: <reason>` or `build-failure: <reason>`; leases everything for the run id.
  - `runs.new(args) -> Run(id, dir)`; `runs.submit(args) -> str` (detached `start_new_session`, stdout to `run.log`);
    `runs.status(id | None) -> str` (pool snapshot plus run registry); `runs.wait(id) -> int` (exit status).
- [ ] **Step 1:** Tests (pytester, fake 4-core pool config via `NATEMU_TEST_CORES=4,0`):
  - `test_reports_replayed`: 6 sleep items (0.2 s) → summary `6 passed`, JUnit has 6 testcases.
  - `test_worker_killed_items_error`: an item that SIGKILLs its own worker → that unit's remaining items are errors
    `worker died`, other units pass, exit 1.
  - `test_two_runs_share_machine`: two concurrent runs with exclusive and shared sleep items; from their logged intervals
    no exclusive overlaps anything and cores never exceed 4.
  - `test_unpreparable_resource`: an item requiring a resource with an unreachable URL is `unpreparable`, others pass.
  - `test_submit_wait`: `submit` returns an id at once; `wait` returns the run's exit status; `status` lists it.
- [ ] **Step 2–4:** fail, implement, pass. Commit `testkit: run loop, preparation, detached runs`.

### Task 11: Selection

**Files:** Create `testkit/select.py`, `tests/selection.toml`, `testkit/tests/test_select.py`

**Interfaces:**
- Produces: options `--tier smoke|affected|gate|campaign|bench` (default `affected`), `--base REV` (default
  `git merge-base HEAD main`), `--require-real FEATURE` (repeatable), `--fresh`; `select.changed_files(base) ->
  list[str]` (committed since base plus uncommitted and untracked); `select.affected(families, changed) ->
  tuple[set[str], list[str]]` (selected family names, unmapped files).
- Rules: unit suites run in every tier (R6.3a); `smoke` = units + smoke cases; `affected` = smoke + every family whose
  `covers` globs match a changed file; a file matching `tests/selection.toml` `shared` globs (`testkit/**`,
  `tools/reference/**`, harness files) selects all smoke; unmapped files are printed in the summary; `gate` = everything
  except `campaign`/`bench` kinds. Nothing selected → message and exit 5; `--gate` with nothing selected → exit 1.
- [ ] **Step 1:** Tests: `test_affected_by_path` (changing `crates/runtime/src/fp/x.rs` selects family `fp` only);
  `test_unmapped_listed`; `test_shared_selects_smoke`; `test_empty_selection_exit_5`; `test_gate_empty_fails`.
- [ ] **Step 2–4:** fail, implement, pass. Commit `testkit: tiers and affected selection`.

### Task 12: Artifacts

**Files:** Create `testkit/artifacts.py`, `testkit/tests/test_artifacts.py`; add `cargo-nextest` and
`qemu-user-static` entries to `tests/resources.toml`

**Interfaces:**
- Produces:
  - `artifacts.source_key(paths: list[str]) -> str`: hash over `git ls-files -s` of those paths plus the bytes of
    modified and untracked files under them.
  - `artifacts.natemu() -> Natemu(host: Path, runtime: Path, stub: Path)`: cache kind `natemu`, key over
    `crates/`, `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml`, `crates/host/entitlements.plist`; builds in the
    worktree's `target/` with the cargo commands of `tools/run_slice1_vm.py:build` (plus `-p natemu-cli` for the stub,
    as `tools/run_boot_smoke.py:build` does), codesigns, copies into the entry laid out as `release/natemu-host`,
    `aarch64-unknown-none/release/natemu-runtime`, `release/natemu` (so a legacy runner can use the entry as
    `CARGO_TARGET_DIR` with `NATEMU_PREBUILT=1`).
  - `artifacts.fexc() -> Path`: cache kind `fexc`, key over the `third_party/FEX` gitlink and `tools/fexc/**`; built with
    `tools/fexc/build.sh <tmp>/b`; returns `<entry>/b/obj/fexc`.
  - `artifacts.nextest() -> Path`; `artifacts.qemu_user() -> Path` (extracted `qemu-x86_64-static`, its sha256 checked
    against `tools/reference/ref.py:PINS['qemu-x86_64-static']`).
- [ ] **Step 1:** Tests: `test_source_key_changes_with_uncommitted_edit` (temp git repo); `test_natemu_layout`
  (marked `slow`: builds once, entry has the three files, host is signed: `codesign -d` exit 0).
- [ ] **Step 2:** Find the pins: the cargo-nextest release tarball for `universal-apple-darwin` at a fixed version and
  its sha256 (from the release's `.sha256` asset), and the `qemu-user-static_1%3a7.2+dfsg-7+deb12u18+b3_arm64.deb` URL
  on `snapshot.debian.org` whose sha256 equals `PINS['qemu-arm64.deb']`; write them to `tests/resources.toml`.
- [ ] **Step 3–4:** implement, pass. Commit `testkit: natemu, fexc, nextest and QEMU artifacts`.

### Task 13: Unit families

**Files:** Create `testkit/harness/__init__.py`, `testkit/harness/units.py`, `tests/unit/family.toml`,
`testkit/tests/test_units.py`

**Interfaces:**
- Produces: harness registry `harness.get(name) -> Harness` with methods `cases(suite) -> list[str]`,
  `requirements(item) -> list[Requirement]`, `run(items, ctx) -> Iterable[SubResult]`; `SubResult(nodeid, outcome:
  Literal['passed','failed','skipped'], longrepr: str, category: str | None, duration: float)`.
  - `units:rust`: one case `all`; runs `cargo +1.97.0 nextest run --offline --locked --no-fail-fast --message-format
    libtest-json` with `NEXTEST_EXPERIMENTAL_LIBTEST_JSON=1` (and `FEXC=<artifacts.fexc()>`), emitting one SubResult per
    test as `tests/unit/family.toml::rust::<binary>::<test>`.
  - `units:python`: one case per test file that `unittest discover -s tools -p 'test_*.py'` finds (the `pytests` step
    today), plus `tools/models/test_split_wait_model.py` (the `split-wait-model` step); each file runs in the worker via
    `pytest.main([file])` with `FEXC` set, its test functions reported as SubResults.
  - `tests/unit/family.toml`: suites `rust` (`needs.cores = 8`, `perf = false`) and `python` (`needs.perf = true` for
    `test_vm_lock.py` and `test_check_setup.py`, whose timing the suite keeps on P cores, via a per-case `perf_cases`
    list).
- [ ] **Step 1:** Tests: `test_nextest_libtest_json_parse` (fixture output with one ok, one failed, one ignored → three
  SubResults with those outcomes); `test_python_cases_match_unittest_discover`.
- [ ] **Step 2–4:** fail, implement, pass; then `tools/natemu-test run --tier smoke -k unit` → all unit tests pass on this
  machine. Commit `testkit: Rust and Python unit suites`.

### Task 14: Programs harness and the FP family

**Files:** Create `testkit/harness/programs.py`, `tests/fp/family.toml`, `tests/fp/rules.py`, `tests/fp/tests.lock`
(generated), `testkit/tests/test_programs.py`

**Interfaces:**
- Consumes: `artifacts.natemu`, `artifacts.fexc`, `artifacts.qemu_user`, `ref.run_programs`, `ref.norm`,
  `flags.analyze`, `run_slice1_vm.description`, `declared_limit`, `page_checks`, `fp_deviations.corpus_deviations`.
- Produces:
  - `programs.normalize(program: dict, state: dict) -> bytes`: canonical JSON of `gpr` (16 ints), `rflags &
    (flags.TRACKED & ~flags.analyze(ref.norm(program)['insns']))`, `rip`, `memory` (addr, data) — the fields
    `ref.compare` compares, under the same mask. Hash: sha256 of it.
  - Self-consistency: for every program of a seeded corpus, `ref.compare(qemu_result, natemu_state)` is empty iff
    `normalize` of both is equal (tested on fixtures/slice1).
  - Seeding (`natemu-test lock tests/fp`): builds the input (generator from the input declaration, in a cache entry),
    runs `ref.run_programs` with `NATEMU_REFERENCE_BACKEND=apple` and `artifacts.qemu_user()`, writes per-case answer
    hashes, the excluded sets per variant (from `rules.applies` with both `real` and `hidden` met), and provenance.
    `reference = 'x86'` cases use `tools/reference/ref_native.py` with `--host` from `NATEMU_X86_HOST`.
  - Running: per item `description(p)` → `natemu-host <runtime> run-program <fexc> program.txt <work> --code-shape`
    with `NATEMU_FP=<engine.fp>` (transitional until run-program takes `--config`); outcome: `unsupported` category if
    `declared_limit`; failure if not `reached`, if `page_checks` find problems, or if `sha256(normalize)` differs from
    the locked answer (message: the differing fields, computed by re-seeding the one case only when `--diff` is given).
  - Reuse (R7.5): store `normalize` output under key (natemu artifact key, input output hash, variant id, chip,
    met-way, programs.py source hash, os_build) in cache kind `outputs`; a hit is re-checked against the current lock
    and reported `reused`; `--fresh` disables.
  - `tests/fp/family.toml`: inputs `i8` (`tools/reference/gen_i8.py --seed 20261030 --random 60`) and `i8b`
    (`tools/reference/gen_i8.py` with its defaults: seed 20261031, as `i8-corpus-20261030-02` was made);
    suites `i8`, `i8b`: `kind = deterministic`, `harness = programs`, `reference = qemu`, matrix `engine.fp =
    [fast, accurate]`, `host.afp = [true, false]`, `batch = 64`, `smoke = ["i8-forms-000"]`, `covers =
    ["crates/runtime/src/**/fp*", "third_party/FEX", "tools/fexc/**", "tools/fp_deviations.py"]`.
  - `tests/fp/rules.py`: `applies` returns the deviation names from `fp_deviations.corpus_deviations('i8-corpus-20261030-01'
    | '-02')[case]['classes']` when `fp == 'fast'` and `afp` is met `hidden` or absent, else None.
  - i8b's 7 known QEMU departures: per R2.13a they need x86 answers; until seeded from x86 they are declared in
    `family.toml` as `reference_overrides = { "i8-forms-001" = "x86", … }` and, without `NATEMU_X86_HOST` at lock time,
    are left unlocked: reported `stale-lock` (missing answer), which makes a gate fail visibly rather than pass.
- [ ] **Step 1:** Tests: `test_normalize_matches_compare` (fixtures/slice1 programs and expected: compare-empty ⇔ equal
  hashes; mutate one masked RFLAGS bit → still equal; one GPR → differ); `test_rules_exclude_tagged_fast_no_afp`.
- [ ] **Step 2:** Run; fail. **Step 3:** Implement. **Step 4:** Pass.
- [ ] **Step 5:** `tools/natemu-test lock tests/fp` (uses QEMU through `container`); commit the lock.
- [ ] **Step 6:** `tools/natemu-test run tests/fp` on this M2 Max: `i8[afp=False,fp=*]` real, `afp=True` variants
  `not-runnable: needs afp`; all others pass; the tagged cases are `excluded` only in `fp=fast,afp=False`. Compare with
  `run_slice1_vm.py` on the same corpus (same pass/fail per case).
- [ ] **Step 7:** Commit `testkit: programs harness and FP family (i8, i8b)`.

### Task 15: Wrapped legacy steps

**Files:** Create `testkit/harness/legacy.py`, `tests/legacy/family.toml`, `testkit/tests/test_legacy.py`

**Interfaces:**
- Consumes: `tools/run_suite.py:steps(F, O)`, `SERIAL_WHY`, `E_OK`, `QUICK`, `Step.judge`; `artifacts.natemu()`,
  `artifacts.fexc()`.
- Produces: `legacy` suite with one case per run_suite step except `build`, `cargo-test`, `cargo-test-fexc`, `runtime`,
  `release`, `pytests` and the steps now covered by migrated families (`i8`, `i8b` and their `-m2f`/`-m2a` variants;
  listed in `family.toml` `replaced = [...]`). Needs: `exclusive` with `why = SERIAL_WHY[name]` for serial steps;
  `cores = step.w`; `perf = not fnmatch(name, E_OK)`. Smoke = `QUICK`. Run: the step's argv with `F` = `artifacts.fexc()`,
  `O` = the run dir, env `NATEMU_PREBUILT=1`, `CARGO_TARGET_DIR=<natemu artifact entry>`, `NATEMU_VM_LOCK_HELD=1`; pass
  = exit 0, or `step.judge(out)` true on exit 1; category `wrapped` on every result; serial steps rerun once on failure,
  both attempts kept (R8.5).
- [ ] **Step 1:** Tests: `test_every_step_mapped` (every `steps()` name is a case or in `replaced` or the builds list);
  `test_serial_steps_exclusive_with_why`.
- [ ] **Step 2–4:** fail, implement, pass; `tools/natemu-test run --tier smoke` runs the legacy smoke cases.
- [ ] **Step 5:** Commit `testkit: wrapped legacy steps`.

### Task 16: Documentation and end-to-end check

**Files:** Create `docs/testing.md`; modify `README.md` (one plain sentence pointing to `docs/testing.md` for running
tests), `docs/implementation.md` (the "Running the suite" paragraph points to the new runner; `run_suite.py` stays until
parity).

- [ ] **Step 1:** Write `docs/testing.md`: running (`tools/natemu-test run`, tiers, `submit/status/wait`), writing a family,
  locking and seeding, what each result category means, cache location and cleanup.
- [ ] **Step 2:** Full check on this machine: `tools/natemu-test run --gate` alongside a `run --tier smoke` from a second
  worktree; record wall time against `tools/run_suite.py` on the same commit; record both in the doc's last section.
- [ ] **Step 3:** Commit `docs: testkit`.

### Task 17 (optional): Remote execution

Only if Tasks 1–16 are done and the `ssh localhost` test below is practical.

**Files:** Create `testkit/remote.py`, `testkit/tests/test_remote.py`

**Interfaces:** `--machines NAME,...|all` reads `.natemu/machines.toml` (`[machine.NAME] ssh = "user@host"`); the
controller treats each remote machine as a worker pool: snapshot the worktree as a temporary commit (`git stash create`
or `HEAD`), `git bundle` against the remote's `git rev-parse HEAD`, fetch into `~/.cache/natemu-testkit/scratch/<id>`
(a `git clone --shared` of the remote checkout, never the checkout itself), bootstrap `tools/natemu-test` there, run the
remote's units with `--testkit-worker`, stream JSONL back over ssh; placement uses each remote `host.detect()`.
- [ ] **Step 1:** `test_remote_localhost` (skipped unless `ssh -o BatchMode=yes localhost true` works): a sleep family runs
  half its units on `localhost` as a remote; merged report equals a local run; killing the ssh mid-run reports the lost
  units as errors.
- [ ] **Step 2–4:** fail, implement, pass. Commit `testkit: remote execution`.

---

## Changes after the plan review (November 17)

An independent review found these; each overrides the task text above where they differ.

- **Task 1:** `pytest.ini` lives at the repo root (`testpaths = tests`, `addopts = -p testkit.plugin -p no:cacheprovider`), not
  under `testkit/`, so the rootdir is the repo and node ids are `tests/<family>/family.toml::…`. The bootstrap refuses a
  `python3` older than 3.11.
- **Task 3:** a published entry is made read-only; cleanup restores write permission before deleting it.
- **Task 5:** a window is not pending until `last_window_closed + min_shared` while shared work waits; during that interval
  shared requests follow the ordinary rules (already in the policy's `window_pending`). `unsatisfiable` is
  `cores > p_cores` for perf requests and `cores > max(p_cores, e_cores)` otherwise; `Needs.cores = "all"` means the
  machine's performance cores at request time. `decide` takes `external_busy: bool`: while the old `.vm.lock` is held
  elsewhere or VM processes run outside the framework, no window opens (it stays pending, backfill continues) and the
  waiting reason says so. The pool checks both without blocking (`LOCK_EX | LOCK_NB`, `outside_vms()`).
- **Task 8:** the harness protocol (above, `testkit/harness/__init__.py`) and the lock schema are fixed here. A harness may
  define `lock_status(fam, suite) -> 'ok' | 'stale' | 'missing'` and `fingerprint_sources(fam, suite) -> list[Path]`.
  Case names: deterministic suites take them from the lock; with no lock, one `(unlocked)` item per variant reports
  `stale-lock`; a locked case without an answer (i8b's 7 departures before x86 seeding) reports `stale-lock: no answer`.
  Judged, legacy and units suites take them from `cases()`. Item reasons and counts are printed in a `testkit` summary
  section. The plugin takes over the run loop only when the session has `CaseItem`s.
- **Task 10:** the worker exits when its parent dies (it watches `os.getppid()`); a unit timeout kills the worker's
  process group; a batch's `expected` is the sum of its items'; `logstart`/`logfinish` are replayed for progress; worker
  stdout goes to a file. Workers of non-legacy items get `NATEMU_REF_CACHE=off`, `NATEMU_ASM_CACHE=off`,
  `NATEMU_QEMU_CACHE=off`. At the end of a run: the run's used entries are recorded per worktree
  (`state/used/<worktree hash>.json`); `gc(cap, roots)` runs with roots = the latest used set of every live worktree,
  plus live leases (R8.4); `.natemu/runs/` keeps the 20 newest runs and any with a `keep` file (R8.5).
- **Task 11:** a run writes `coverage.json` (node id → outcome, machine, how host requirements were met). `--gate
  --with-run DIR` (repeatable) counts an item that is `not-runnable` here as covered when it passed in that run of the
  same commit (R5.7). A machine with every feature (the M4) can complete a gate alone (R5.3).
- **Task 12:** the natemu key also covers `.cargo/config.toml`. Each run sets natemu's compile cache explicitly to
  `<cache>/compile-cache/<natemu key>/` (R8.8).
- **Task 13:** nextest runs as `cargo +1.97.0 nextest …` with the artifact's folder first on `PATH`; `NATEMU_REQUIRE_QEMU_IMG=1`
  as `run_suite.py` sets it. Each Python tool-test file runs in its own subprocess with `-c /dev/null -p no:cacheprovider`
  (no testkit plugin); `test_split_wait_model` is already found by `discover -s tools` and is not added again.
- **Task 14:** the input's output hash covers the canonical `programs` list only, not the generator header (which records
  the local clang). At collection the variant's computed exclusion set is compared with the lock's; a difference adds an
  `exclusion-changed` item (R2.13, R5.8).
- **Task 15:** legacy steps never point `CARGO_TARGET_DIR` at a cache entry. Before legacy items run, the worktree's
  `target/` gets the signed artifact binaries (copied with `cp -c`). Steps that build themselves (`l1`, `l2`, `l2-iasl`,
  `boot-smoke`) hold a per-worktree build lock exclusively (other legacy units of that worktree hold it shared), and the
  signed binaries are copied back after each. A step whose `natemu-raw` inputs are absent is `not-runnable: needs
  natemu-raw (PATH)`; a `Step.skip` maps to `not-runnable`.

## Status (November 19)

Full gate at 64370f68 on the M2 Max, combined with the M3's AFP half at the same commit (`--gate --with-run`, run
20261119-090704-89a7 with m3-afp-6437): 80,184 passed, 0 failed, exit 0. Later on November 19: a worker killed
outright no longer leaves its campaign VM running (the controller stops it), and `run --diff` shows what differs for a
case whose locked answer is stored as a hash only. Task 17 (remote execution) stays unbuilt.

## Status (November 18)

Tasks 1–16 are implemented on branch `test-framework`; an independent code review's twelve findings are fixed. Task 17
(remote execution) is not built: it was optional, and testing it needs Remote Login on a test Mac (`ssh localhost`) or
the M4; the November 17 spike showed the approach works. Measured on the M2 Max: the FP family in 14 s (8 s with
reuse); all unit tests in 8 min; the smoke tier in 18 min, above the 10-minute target (the two full nextest passes take
most of it). Not yet done: a full `--gate` run against `tools/run_suite.py` on the same commit, a run on the M4, x86
answers for i8b's seven QEMU departures, a claim that keeps concurrent QEMU reference containers from timing out.

### Verification on the M2 Max (November 18)

- Full gate (`run --gate`): 44 min; 2,028 unit tests and 173 of 177 wrapped steps passed, 1,049 FP cases passed
  (reused from an identical earlier run, re-checked against the lock), 1,164 AFP-on variants not runnable (no AFP here).
  No failure came from the framework. The failures are natemu's behavior without AFP, from unchanged scripts:
  the four AFP-dependent `natemu-abi` fptrap tests (also under plain `cargo test`), 24 i8 programs that deviate in fast
  mode without a deviation tag and `i8-forms-044` in accurate mode (both as `tools/run_slice1_vm.py` reports them),
  `i8-1` (the same programs at block size 1), `k2-stmxcsr` (the documented `masked-sse-de` deviation),
  `i8-faults-m2a` (1 of 846: `chain3-movaps-mis` hits the watchdog in accurate mode), `system` (its QEMU_DIFFERS list
  disagrees with this machine's QEMU), and `test_fp_deviations` (natemu-raw's corpus folder here is a symlink).
- Benchmarks (`run --tier bench`): all 20 measured in 10.5 min while the gate run shared the machine; every guest
  benchmark verified both runs, none contended. The guest image was built from `tools/bench/image` on the x86 box
  (`ssh:ubuntu@192.168.12.10`) and its expected outputs equal the M4 reference image's.
- Seen once, not again in the gate: two wrapped steps whose QEMU reference containers started at the same moment timed
  out in Apple's container service (XPC timeout). A claim for reference-container starts would prevent it.
