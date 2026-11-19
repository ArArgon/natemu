# Testing natemu

`tools/natemu-test` runs natemu's tests on any Apple Silicon Mac. The first run on a machine builds or downloads what
the tests need (natemu itself, the fexc compiler, test programs, pinned tools) into a cache shared by all your
checkouts; later runs reuse it. Nothing has to be copied from another machine. The requirements this implements are in
[the spec](superpowers/specs/2026-11-17-test-framework-design.md).

## Running tests

```sh
tools/natemu-test run                     # the tests your change concerns (the "affected" tier)
tools/natemu-test run --tier smoke        # one small case of everything, plus all unit tests
tools/natemu-test run --gate              # everything: what must pass before a push or merge
tools/natemu-test run tests/fp            # one family; any pytest selection works (-k, node ids)
```

`run` without a tier looks at what you changed since `main` (committed or not) and runs every unit test, every
family's smoke cases, and the families whose declared paths match your changes. Files no family claims are listed so
you can decide whether to widen the run.

Runs from different checkouts and agents share the machine safely: each test asks the machine's queue before it
starts. Tests that need the machine to themselves (timing bounds) run in *exclusive windows*: queued exclusive tests
from every run go back to back once the machine is idle, then shared work gets its turn again. A quick run waits at
most for the window in progress. Between runs of the same priority the queue is fair: the run that has had the least
machine time goes first, so a one-test run is not held behind a six-thousand-test one, and a run that has had nothing
for 30 minutes goes to the front. VMs started by older scripts through `tools/vmlock.py` queue the same way.
`tools/natemu-test replay` replays recorded runs through these rules, without running anything: use it to check a
scheduling change.

```sh
tools/natemu-test submit --tier smoke     # start a run in the background; prints its id
tools/natemu-test status                  # the machine's queue: what runs, what waits and why
tools/natemu-test status RUN              # one run
tools/natemu-test wait RUN                # wait for it; exits with its status
```

```sh
tools/natemu-test prepare --tier bench    # build and fetch what a selection needs, run nothing (same selection as run)
```

Preparation only claims the machine when something is missing; what is already in the cache is just looked up.

Each run keeps its output in `.natemu/runs/<id>/` (JUnit in `junit.xml`, every worker's log under `units/`, each
runner case's log under `runner/`). A passed case's output folder is deleted as soon as it passes; a failed case keeps
its own in `out/<case>` (`run --keep-outputs` keeps them all). The 20 newest runs are kept, and any run folder with a
file named `keep` in it.

At the end of every run a `times` section says where the time went: start-up and collection, preparation (what was
built and what came from the cache), the tests themselves, and the report, with each family's share and its longest
unit. `tools/natemu-test times [RUN] --detail` shows it again later with the cleanup after the run, every unit split
into queue, start-up and test time, and the slowest requirements to prepare.

## What a result means

Besides passed and failed:

| Result | Meaning | Fails a gate |
| --- | --- | --- |
| excluded | the family's rule says this case does not apply to this variant (the reason is shown) | no |
| not-runnable | this machine cannot run it, for example a variant that needs AFP on a Mac without it | yes, unless covered (below) |
| stale-lock | the test's locked answer no longer fits its inputs, or the case has no answer yet: re-seed | yes |
| build-failure | an input built here differs from the locked one, or a build failed | yes |
| unpreparable | a download or other preparation failed (no network, upstream gone) | yes |
| exclusion-changed | the family's rule now excludes other cases than its lock records | yes |
| unsupported | natemu stopped at a declared limit of the program harness | no |
| reused | an identical earlier run's output, checked again against the current lock | no |
| wrapped | a step of the old `tools/run_suite.py` run unchanged (it still uses `natemu-raw`) | if it failed |

A campaign (`--tier campaign`, `tests/correctness`) judges each test it runs by that test's own verdict, and adds:

| Result | Meaning | Fails the run |
| --- | --- | --- |
| expected-fail | the test failed and its suite lists it in `expected`, with the reason | no |
| unexpected-pass | the test passed although `expected` lists it: remove the entry | yes |
| flaky | the test is listed in `flaky` and failed again when its part was rerun | no |
| missing | the image lists the test, but the run never reported it (the guest crashed or hung) | yes |
| stale-entry | an `expected` or `flaky` entry names a test the image does not have (renamed or removed?) | yes |
| unverified | the test ran to the end, but nothing checks that its output is right | no, and never a pass |

A Mac with every feature (an M4) can pass a gate alone. On a Mac without one, combine runs of the same commit:
`--gate --with-run .natemu/runs/<id-from-the-M4>` counts the variants this Mac cannot run as covered where they passed
in that run. `--require-real afp` places "AFP absent" variants only on a Mac that really lacks AFP instead of hiding it
on one that has it.

## Writing tests

A family is a folder under `tests/` with a `family.toml`. It declares its inputs, its suites, the variants to run, what
each suite needs from the machine, and the source paths it covers:

```toml
[family]
name = "fp"
covers = ["crates/runtime/src/fp/**"]          # changes here select this family

[input.i8]
generator = "tools/reference/gen_i8.py"         # builds the corpus on any machine
args = ["--seed", "20261030", "--random", "60"]

[[suite]]
name = "i8"
kind = "deterministic"                          # compared with a locked answer; "judged" for code that decides
harness = "programs"                            # a module in testkit/harness, or "file.py:Class" in this folder
input = "i8"
reference = "qemu"                              # what produces the answers when seeding: qemu or x86
matrix = { "engine.fp" = ["fast", "accurate"], "host.afp" = [true, false] }
needs = { cores = 1 }                           # or exclusive = true + why, perf = true, dedicated = true
smoke = ["i8-forms-000"]
batch = 64
```

Each combination of matrix values is its own test, named like `i8-forms-000[afp=False,fp=fast]`. `engine.*` and
`machine.*` keys are natemu settings; `host.<feature>` keys are requirements on the Mac (features are listed in
`tests/features.toml`). A machine with a feature also runs the variants that need it absent by hiding it.

Where x86 leaves a result to the processor, a locked answer would pin one processor's choice. A programs suite with
`judge = "approx"` judges RCPPS, RCPSS, RSQRTPS and RSQRTSS by the Intel SDM's rules instead
(tools/models/afp_fp/approx.py): any result within the SDM's error bound passes, the results the SDM fixes are
required, and the rest of the program is still checked against the locked answer.

A natemu bug a test catches never blocks a run: list it in the family's `known-bugs.toml` (`[bug.<id>]` with a
`note` and `cases` patterns over the node id, `*` and `?` wildcards). The test keeps running; a failure is reported as
`known-bug`, grouped by bug, and a pass as `known-bug-passed` (remove the entry). Neither fails the gate
(testkit/known_bugs.py).

A family's `rules.py` may define `applies(suite, case, variant, met)` returning a reason to exclude a case from a
variant. A variant only ever checks correct behavior; testing a documented wrong behavior belongs in its own test.

## Seeding and locks

Deterministic tests never need QEMU or the x86 machine when they run. Whoever adds or changes one seeds it once:

```sh
tools/natemu-test lock tests/fp           # build inputs, run the reference, write tests/fp/tests.lock
tools/natemu-test check-lock              # stale entries, suspicious answers, cases without an answer
```

The lock holds only hashes: of each built input and of each case's answer (the normalized final state). Commit it
with the change. Seeding uses Apple's `container` for QEMU (`container system start` once per boot); cases whose
reference is `x86` need `NATEMU_X86_HOST=user@host` and are left without an answer otherwise, so they report
stale-lock rather than pass.

## Where amd64 inputs are built

Inputs built for x86-64 (the recipes' Debian packages, the campaign images, stress-ng) build in one pinned amd64
Debian container image. By default that is this Mac's Apple `container`, through Rosetta. A machine can send them to
a real x86-64 Linux machine instead, where the same image runs natively under Docker (several times faster):

```toml
# ~/Library/Caches/natemu/testkit/config.toml (this machine, every checkout), or .natemu/config.toml (one checkout)
[build]
builder = "ssh:ubuntu@x86-box"
```

Entries record and are keyed by where they were built, so a Mac-built entry and an x86-built one never stand in for
each other. The stress-ng build gives the same binary either way (`53140a75...`).

## The cache

Everything a machine builds or downloads lives in `~/Library/Caches/natemu/testkit` (`NATEMU_TEST_CACHE` to move it),
identified by what it was built from, and is shared by every checkout. Entries are read-only once built. After each
run the cache is cleaned down to its cap (100 GiB; `NATEMU_TEST_CACHE_CAP`), least recently used first, keeping what
any live checkout used last and what running runs hold. `tools/natemu-test gc` cleans on demand. Each checkout's own
cargo `target/` folder is not part of it.

```sh
tools/natemu-test clean      # this checkout's finished run folders and leftover temporary folders; cache down to its cap
tools/natemu-test purge      # the same, and every cache entry no running run uses: the next run rebuilds what it needs
```

Both take `--dry-run` to say what would go and how much, and `--all-worktrees` to include every checkout that has
run tests on this machine (other worktrees' run folders are otherwise kept). Neither touches a running run, a run folder with a `keep`
file, the current Python environment or the machine's scheduler state and recorded durations.

Downloads are declared once in `tests/resources.toml` with their URLs and pinned sha256; nothing is used unverified.

## Benchmarks

```sh
tools/natemu-test run --tier bench                    # every benchmark, each alone on the machine
tools/natemu-test run --tier bench -k guest-zstd      # one
```

`tests/bench` wraps the existing `tools/bench` scripts unchanged: the micro-benchmarks against QEMU, the SMP tools, Linux
boot times, and the guest benchmarks of [benchmarks](benchmarks.md) (12 programs at 1 vCPU, and `zstd` on QEMU for
comparison). Each takes the whole machine. Numbers are written per machine, to
`.natemu/runs/<id>/bench/<machine id>/<benchmark>/`, and never compared across machines.

The guest benchmarks' initramfs is built the first time it is needed, from `tools/bench/image` (the reference box's
recipe, unchanged), and then comes from the cache; the kernel is Debian's pinned package. Where it is built is a setting:

```toml
# .natemu/config.toml (this machine only, not committed); or NATEMU_BENCH_BUILDER=...
[bench]
builder = "local"                   # default: this Mac, in an amd64 Debian container through Rosetta
# builder = "ssh:ubuntu@x86-box"    # a real x86-64 Linux machine: the recipe is copied there, built, fetched, removed
```

`m1-perf` is the M1 performance check ([design](m1-config-and-qcow2-design.md), "Performance check"):
`tools/run_m1_perf.py full` compares natemu at its baseline commit with this checkout's build, in fio matrix runs on raw,
qcow2 and overlay disks and in IO5, and passes only when its verdict is PASS (`summary.md` in its folder). It takes about
80 minutes and 3-4 GiB of free disk. Everything it reads is built here: the baseline is `git archive` of that commit,
built like the current one and cached by the commit's tree (`artifacts.natemu_at`); the fio preparation and block inputs
are built from their recipes (see the input root below); IO5's disk, kernel and CPython source come from `run_io5.py prepare` on the pinned Debian cloud
image and CPython tarball (`inputs.io5_inputs`). It runs as 21 pieces, one per planned run (`m1-perf/piece-NN`), each
holding the machine for only its own run, so other work runs in between; all pieces record into one results folder kept
in the machine's state folder, a rerun measures only what is missing (`--fresh` starts over), and the piece that finds
nothing left reports the verdict.

The image's expected outputs are recorded by whichever machine builds it, and every benchmark result names the builder.
Switching builders builds a new image rather than reusing the other one's. Not
wrapped yet: the drift canary and stored baselines (`tools/bench/canary.py`), whose inputs still live in `natemu-raw`.

## Moving the old suite over

Every step of `tools/run_suite.py` now runs in a family. The unit, fp and programs families have their own harnesses. The
rest are runner families (`tests/{boot,static,system,smp,faults,io}`), generated by `python3 -m testkit.migrate` from
`run_suite.py`'s step list: each case is one unchanged runner invocation that reports per case (`results.json`), reads its
inputs from the input root below and compares with QEMU's locked answers instead of running QEMU.

Seeding a runner family's QEMU answers is a run followed by a lock: run the cases with `TESTKIT_SEED_ANSWERS=1` (QEMU runs and
each case's answers are logged in the run folder), twice, then `tools/natemu-test lock tests/<family> --from-run RUN1 RUN2`.
Answers that differ between the two runs are dropped where no comparison uses them, and an answer that differs from the
locked one is refused rather than overwritten. Each answer is a hash, with the value inline when it is short, and the steps
that reproduce it (supplier, pin, program or image hash, arguments).

The inputs those runners read can also be built on any machine instead of copied: `testkit.inputs.root()` makes a cache
entry laid out like `natemu-raw` (the netboot kernel and initrd, the QEMU reference binary, the virtio block inputs and
kernel package, the network tools' root, the i7 and s2d program corpora, the fio preparation and a static stress-ng; no
expected answers) from pinned downloads and recipes in the repo (`tools/io/network-rootfs` for the network tools,
`tools/io/fio-prep` for Debian's fio and its libraries, `tools/io/stress-ng` for stress-ng; each runs in the amd64 Debian
container through Rosetta and needs Apple's `container`). `testkit.inputs.matrix_inputs()` is `run_io_matrix.py
prepare`'s folder built from them, which `run_serial_stress.py` and `run_io_soak.py` read; they take the framework's
stress-ng with `--stress-provenance <its provenance.json>`. Point a runner at it with
`NATEMU_RAW_ROOT=<that folder>` and `NATEMU_STATE_ROOT=<a writable folder>`: the entry is read-only, and the runners'
VM and port locks, result caches and reference work folder go to the state root (by default the raw root, as before).
Every runner on the machine must use the same state root, since the VM lock only excludes runners that share its file.

The Linux workload campaigns' images are built the same way: `testkit.inputs.campaign_image(suite)` runs
`tools/correctness/recipes/<suite>/build.sh` in that container (`remote.sh`'s stages as the user `ubuntu`, so the
paths baked into the image match the reference builds) and gives a folder with `<suite>.cpio.zst`, `<suite>-cases.txt`
(one `PART TEST` line per test, PART being the kernel's `part=` word) and `manifest.json` (every download with its URL
and sha256). Every downloaded file is checked against a pin file in `tools/correctness/recipes` (`common/` for what all
suites share). A build that finds no pin file writes the hashes it saw to the state folder's `repin/` and stops
before building; check them and copy them into the recipes. OpenSSL and torture have recipes so far.
