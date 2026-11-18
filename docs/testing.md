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
most for the window in progress.

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

Each run keeps its output in `.natemu/runs/<id>/` (JUnit in `junit.xml`, every worker's log under `units/`). The 20
newest runs are kept, and any run folder with a file named `keep` in it.

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

## The cache

Everything a machine builds or downloads lives in `~/Library/Caches/natemu/testkit` (`NATEMU_TEST_CACHE` to move it),
identified by what it was built from, and is shared by every checkout. Entries are read-only once built. After each
run the cache is cleaned down to its cap (100 GiB; `NATEMU_TEST_CACHE_CAP`), least recently used first, keeping what
any live checkout used last and what running runs hold. `tools/natemu-test gc` cleans on demand. Each checkout's own
cargo `target/` folder is not part of it.

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

The image's expected outputs are recorded by whichever machine builds it, and every benchmark result names the builder.
Switching builders builds a new image rather than reusing the other one's. Not
wrapped yet: the drift canary and stored baselines (`tools/bench/canary.py`), whose inputs still live in `natemu-raw`.

## Moving the old suite over

`tests/legacy` runs every step of `tools/run_suite.py` that no family has replaced yet, unchanged, with its old pass
rule. These steps still read `natemu-raw`. To move one: write a family for it, then list the step under `replaced` in
`tests/legacy/family.toml`. `tools/run_suite.py` stays until every step has moved.

The inputs those runners read can also be built on any machine instead of copied: `testkit.inputs.root()` makes a cache
entry laid out like `natemu-raw` (the netboot kernel and initrd, the QEMU reference binary, the virtio block inputs and
kernel package, the network tools' root, the i7 and s2d program corpora; no expected answers) from pinned downloads and
recipes in the repo (`tools/io/network-rootfs` for the network tools). Point a runner at it with
`NATEMU_RAW_ROOT=<that folder>` and `NATEMU_STATE_ROOT=<a writable folder>`: the entry is read-only, and the runners'
VM and port locks, result caches and reference work folder go to the state root (by default the raw root, as before).
Every runner on the machine must use the same state root, since the VM lock only excludes runners that share its file.
