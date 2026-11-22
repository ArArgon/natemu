# Testing natemu

`tools/natemu-test` runs natemu's tests on any Apple Silicon Mac. The first run on a machine builds or downloads what
the tests need (natemu itself, the fexc compiler, test programs, pinned tools) into a cache shared by all your
checkouts; later runs reuse it. Nothing has to be copied from another machine. The requirements this implements are in
[the spec](superpowers/specs/2026-11-17-test-framework-design.md).

## Running tests

```sh
tools/natemu-test run                     # the tests your change concerns (the "affected" tier)
tools/natemu-test run --tier smoke        # one small case of everything, plus all unit tests
tools/natemu-test run --gate              # everything: what must pass before a merge that needs it (below)
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
for 30 minutes goes to the front. VMs started outside the framework through `tools/vmlock.py` queue the same way.
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

## When the gate is needed

The full gate (`--gate`, passing at that exact commit) is for changes that can alter what natemu or the tests do,
before they merge into `main` or an integration branch:

- code on a shared path: code identity, dispatcher and lookup tables, resident entry and exit, register save,
  admission, the FEX fork or fexc's output;
- the build: toolchain or dependency pins, build flags, the FEX submodule;
- the test framework itself: `testkit/`, harnesses, locks, generators;
- a change set that closes a plan's task or checkpoint.

Everything else gets `tools/natemu-test run` (the affected families plus every family's smoke cases): a change confined
to one area the framework attributes to its families, and new or changed test cases. If `run` lists files no family
claims, attribute them or run the gate.

No gate at all for docs, comments, plans and READMEs, or for pushing a work branch; its gate comes when it merges.

**Exception:** the user may explicitly bypass the gate for a given merge or push. Record the bypass (in the commit
message or the merge description) and the tier that did run.

## How much to run

Iterate quickly and run heavy tests rarely, without giving up the gate:

- **While iterating:** run the family or the cases the change concerns (`tools/natemu-test run tests/fp -k ...`), plus
  the cases the change adds or alters.
- **At the end of each task, and at once after a change to something every block or exit passes through** (code
  identity, dispatcher and lookup tables, resident entry and exit, register save, admission, the FEX fork's output):
  `tools/natemu-test run`, which adds every family's smoke cases and the affected families. A narrow run cannot see
  how far such a change reaches.
- **Heavy runs, once per change set, in the queue:** `--gate`, multi-vCPU stress, campaign boots and benchmarks run
  when a change set's fixes are all in, not between fixes. Where the gate is needed (above), it passes at that exact
  commit unless the user explicitly bypasses it.
- **Benchmarks where they decide something,** and at the milestones a plan names.
- **A failure in a heavy run** sends the work back to quick iteration, with the failing case added to the narrow set.
- **Report which tier ran.** A narrow or affected pass never stands in for the gate.

## What a result means

Besides passed and failed (a failure fails every run, exit status 1):

| Result | Meaning | Fails the run |
| --- | --- | --- |
| excluded | the family's rule says this case does not apply to this variant (the reason is shown) | no |
| not-runnable | this machine cannot run it, for example a variant that needs AFP on a Mac without it | a gate, unless covered or accepted by a person (below) |
| stale-lock | the test's locked answer no longer fits its inputs, or the case has no answer yet: re-seed | yes |
| build-failure | an input built here differs from the locked one, or a build failed | yes |
| unpreparable | a download or other preparation failed (no network, upstream gone) | yes |
| exclusion-changed | the family's rule now excludes other cases than its lock records | yes |
| unsupported | natemu stopped at a declared limit of the program harness | no |
| reused | an identical earlier run's output, checked again against the current lock | no |

A run that fails exits with status 1, and so does `tools/natemu-test wait RUN` for it; a gate also fails when it
selected nothing.

A campaign (`--tier campaign`, `tests/correctness`) judges each test it runs by that test's own verdict, and adds:

| Result | Meaning | Fails the run |
| --- | --- | --- |
| expected-fail | the test failed and its suite lists it in `expected`, with the reason | no |
| unexpected-pass | the test passed although `expected` lists it: remove the entry | yes |
| flaky | the test is listed in `flaky` and failed again when its part was rerun | no |
| missing | the image lists the test, but the run never reported it (the guest crashed or hung) | yes |
| stale-entry | an `expected` or `flaky` entry names a test the image does not have (renamed or removed?) | yes |
| unverified | the test ran to the end, but nothing checks that its output is right | no, and never a pass |

A suite's `part_args` are options of the campaign's own runner, added to each part's `part=` word on the kernel command
line; the stress suite uses them to run a small smoke size (`scale=0.05,timeout=20`). Set `TESTKIT_PART_ARGS` to
replace them for one run: `SUITE:ARGS` for one suite, several separated by `;`, an empty ARGS meaning none (the
runner's defaults). `TESTKIT_PART_ARGS=stress:scale=1,timeout=240 tools/natemu-test run --tier campaign
tests/correctness/family.toml -k stress` is the original full-size stress-ng campaign, and
`TESTKIT_PART_ARGS='stress:scale=1,timeout=240;litmus:'` runs both at full size. A value without `SUITE:` applies to
every suite of the run that no entry names.

A Mac with every feature (an M4) runs everything alone. On a Mac without one, a gate in which everything that ran passed ends
not passed, under a red line such as `ALL TESTS PASSED, BUT 8 TESTS DID NOT RUN ON THIS MACHINE`, the list of them and three
ways to close it, each a later command. Nothing is asked while the run goes (a prompt would block a run nobody watches); an
agent that ran the gate hands the choice to the user and then runs the command for it:

1. **Ignore those tests (risk: high).** Nothing has checked them on this commit. `tools/natemu-test run --gate --complete
   RUN --accept-not-runnable` on the same machine runs nothing again and passes the gate without them; the flag records a
   person's decision and is never a default.
2. **Run only those tests on a machine that can (risk: medium-low).** There, on the same commit: `tools/natemu-test run
   --gate --complete RUN` (RUN being the first machine's run folder, copied over) runs only what that run reported
   not-runnable and gives the verdict of both runs together.
3. **Run the whole gate again on that machine (risk: low).**

The other way round, a gate can count another machine's run of the same commit:
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
needs = { cores = 1 }                           # or exclusive = true + why, perf = true, dedicated = true, mixed = true
smoke = ["i8-forms-000"]
batch = 64
```

`mixed = true` beside `perf = true` lets a case that claims more cores than the Mac has performance cores take the whole
machine instead, so its vCPU threads also run on efficiency cores (`smp` and `litmus`: two 4-vCPU VMs at once). Not on an
M1, whose efficiency cores have no TSO: there such a case is not-runnable unless the Mac has that many performance cores.

A unit without `perf` runs on an efficiency core when no performance core is free, but only if it ends sooner there than by
waiting for one. How much slower efficiency cores are (about 3x on an M2 Max) is measured once per Mac and macOS version,
in about 6 s at the first `natemu-test` start with nothing else running, and cached; `tools/natemu-test status` shows it.
`NATEMU_TEST_E_SLOWDOWN` overrides the factor (1 turns this off). So a long unit is not started on a slow core near the
end of a run, and recorded durations are kept in performance-core time.

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

### Adding a test for an instruction

A test for one x86 instruction is a small straight-line program in one of the program corpora. Each corpus is produced by
a generator under `tools/reference/`, named in the family's `[input.*]` entry: add the program to the generator whose
subject fits, then seed (below) and commit the generator and the lock together.

| Subject | Family, suite | Generator |
| --- | --- | --- |
| integer, flags, addressing, branches in general | `tests/programs`: `fix` (fixed forms), `seed` (random) | `gen_slice1.py` |
| structured control flow | `tests/programs`: `s2d` | `gen_slice2.py` |
| MOVBE, MMX, AES-NI, PCLMULQDQ, PDEP/PEXT | `tests/programs`: `i9`, `i9-big` | `gen_i9.py` |
| the 0x67 address-size prefix, XLAT | `tests/programs`: `i10` | `gen_i10.py` |
| several REX prefixes | `tests/programs`: `rex` | `gen_rex.py` |
| FS/GS segment overrides | `tests/programs`: `i7` | `gen_i7.py` |
| SHA, ADX, XSAVEOPT/XSAVEC | `tests/programs`: `sha`, `adx`, `xsave` | `gen_sha.py`, `gen_adx.py`, `gen_xsave.py` |
| the 66 prefix on near branches | `tests/programs`: `br66` | `gen_br66.py` |
| string compares (PCMPxSTRx), x87 transcendentals | `tests/programs`: `i6b` | `gen_i6b.py` |
| SSE, AVX, AVX2, FMA, F16C (floating point) | `tests/fp`: `i8`, `i8b` | `gen_i8.py` |

An instruction none of these fits gets its own generator, `[input.<name>]` and `[[suite]]` in `tests/programs` (or
`tests/fp` when its result depends on the floating-point mode or on AFP). Faults, paging, interrupts and several vCPUs
are not program corpora: those are cases of the runners the `faults`, `system` and `smp` families call.

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

Entries are keyed by the build environment: this Mac's Rosetta, or an x86 machine's CPU model, CPU flags and kernel
release (read from it once per run), never by its address. So a Mac-built entry and an x86-built one never stand in
for each other, while two equivalent x86 machines share their entries. Campaign images and stress-ng record which
machine built them. Only upstream inputs (sources, packages, the builder image) are pinned; what a build produces is
recorded (its hash, in the entry's provenance), never required to match an earlier build.

The x86 machine runs at most two builds at once (`BUILD_SLOTS` in `testkit/inputs.py`), shared by every Mac that uses
it, so the first images come back early instead of all of them late. While a campaign image builds there, its run
holds only a *light* claim on this Mac: it takes no cores and never holds up an exclusive window or other runs' work.

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

`TESTKIT_BENCH_AT=REV tools/natemu-test run --tier bench ...` measures natemu and its compiler as committed at REV
instead of the checkout's (built from `git archive`, cached), with the same cases, kernel and image: a baseline for
the same round. `TESTKIT_BENCH_VCPUS=4` runs the guest benchmarks at 4 vCPUs (default 1).

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
`tests/bench/run_m1_perf.py full` compares natemu at its baseline commit with this checkout's build, in fio matrix runs on raw,
qcow2 and overlay disks and in IO5, and passes only when its verdict is PASS (`summary.md` in its folder). It takes about
80 minutes and 3-4 GiB of free disk. Everything it reads is built here: the baseline is `git archive` of that commit,
built like the current one and cached by the commit's tree (`artifacts.natemu_at`); the fio preparation and block inputs
are built from their recipes (see the input root below); IO5's disk, kernel and CPython source come from `run_io5.py prepare` on the pinned Debian cloud
image and CPython tarball (`inputs.io5_inputs`). It runs as 21 pieces, one per planned run (`m1-perf/piece-NN`), each
holding the machine for only its own run, so other work runs in between; all pieces record into one results folder kept
in the machine's state folder, a rerun measures only what is missing (`--fresh` starts over), and the piece that finds
nothing left reports the verdict.

`io5-solo32` is the 32-bit toolchain build (`tests/io/run_io5.py --i386`): in one boot of the Debian cloud image, CPython's
`./configure` and `make -j4` by the 64-bit userland and then by a Debian i386 userland that `debootstrap` installs in the
guest, whose python must give the x86 reference machine's answers (a pinned hash). `io5-solo32-qemu` is the same under
QEMU, `io5-solo32-profile` writes natemu's timing log (`tests/io/io5_compiles.py DIR` summarizes it), and
`NATEMU_X86_HOST=user@host tools/natemu-test run --tier bench -k io5-solo32-reference` boots it on the x86 machine under
KVM to record that hash and real x86's times. That last case is marked `remote = true`: nothing runs or is timed on this
Mac, so it takes one core instead of the whole machine.

The image's expected outputs are recorded by whichever machine builds it, and every benchmark result names the builder.
Switching builders builds a new image rather than reusing the other one's. Not
in the framework yet: the drift canary and stored baselines (`tools/bench/canary.py`), whose inputs still live in `natemu-raw`.

## Runner families

The unit, fp and programs families have their own harnesses. The rest are runner families
(`tests/{boot,static,system,smp,faults,io}`): each case is one invocation of a script in the family's folder (or
`tests/lib`, for scripts several families share; [tests/README.md](../tests/README.md)) that reports per case
(`results.json`), reads its inputs from the input root below and compares with QEMU's locked answers instead of running
QEMU. The scripts refuse to start outside the framework. To debug one, run its case with `--keep-outputs`: the case's
log (`.natemu/runs/<run>/runner/<case>.log`) starts with the exact command and the settings the framework gave it.

Seeding a runner family's QEMU answers is a run followed by a lock: run the cases with `TESTKIT_SEED_ANSWERS=1` (QEMU runs and
each case's answers are logged in the run folder), twice, then `tools/natemu-test lock tests/<family> --from-run RUN1 RUN2`.
Answers that differ between the two runs are dropped where no comparison uses them, and an answer that differs from the
locked one is refused rather than overwritten. Each answer is a hash, with the value inline when it is short, and the steps
that reproduce it (supplier, pin, program or image hash, arguments).

The compiler's own checks under `tools/fexc` (the smoke check, `check_sites.py`, `verify_precision.py`, `scan_skips.py`,
the serve and channel tests and the unittest files `test_fexc.py` and `test_slice1.py`) are the `fexc` suite of
`tests/static`, each case one script judged by its exit status. Three of its cases are not plain script runs:
`fexc-replay` shows that a compiler change left 64-bit output alone (it records the compile requests of one boot and
compares the answers of the compiler at the checkout's merge base with `main`, which the framework builds, with the
checkout's own; a change meant to alter 64-bit output fails it by design); `fexc-asm32` compiles FEX's 245 32-bit
tests offline and has the host admit every block; `fexc-precision-32` runs the precision and site checks on 32-bit code.

When an answer kept only as a hash differs, the failure can say only that it differs. Run the case again with
`tools/natemu-test run ... --diff`: a case that failed that way runs once more with live QEMU answers, and the message
adds what the runner's own field-by-field comparison reports. When the case then passes, either the locked answer is
stale or natemu fails only sometimes; rerunning without `--diff` tells them apart. That rerun's result is not counted.

The inputs those runners read can also be built on any machine instead of copied: `testkit.inputs.root()` makes a cache
entry laid out like `natemu-raw` (the netboot kernel and initrd, the QEMU reference binary, the virtio block inputs and
kernel package, the network tools' root, the i7 and s2d program corpora, the fio preparation and a static stress-ng; no
expected answers) from pinned downloads and recipes in the repo (`tools/io/network-rootfs` for the network tools,
`tools/io/fio-prep` for Debian's fio and its libraries, `tools/io/stress-ng` for stress-ng; each runs in the amd64 Debian
container through Rosetta and needs Apple's `container`). `testkit.inputs.matrix_inputs()` is `run_io_matrix.py
prepare`'s folder built from them, which `run_serial_stress.py` and `run_io_soak.py` read; they take the framework's
stress-ng with `--stress-provenance <its provenance.json>`. The runner harness points each runner at it with
`NATEMU_RAW_ROOT=<that folder>` and `NATEMU_STATE_ROOT=<the machine's state folder>`: the entry is read-only, and the
runners' VM and port locks, result caches and reference work folder go to the state root. Every runner on the machine
uses the same state root, since the VM lock only excludes runners that share its file.

The Linux workload campaigns' images are built the same way: `testkit.inputs.campaign_image(suite)` runs
`tools/correctness/recipes/<suite>/build.sh` in that container (`remote.sh`'s stages as the user `ubuntu`, so the
paths baked into the image match the reference builds) and gives a folder with `<suite>.cpio.zst`, `<suite>-cases.txt`
(one `PART TEST` line per test, PART being the kernel's `part=` word) and `manifest.json` (every download with its URL
and sha256). Every downloaded file is checked against a pin file in `tools/correctness/recipes` (`common/` for what all
suites share). A build that finds no pin file writes the hashes it saw to the state folder's `repin/` and stops
before building; check them and copy them into the recipes. Recipes: openssl, torture, torture32, ltp, ltp32, stress, litmus,
cpyt/cpyb, public, libm, libm32, gstr, x86st and i386. The glibc math (`libm`) and string (`gstr`) recipes run
glibc's own `make check` once in the container, only to record each test's exact command, which the guest then runs;
the kernel x86 selftests' (`x86st`) list of tests not run (those that would stop the VM) is
`tools/correctness/x86st-skip.txt`.

Four campaigns of 32-bit programs compare with the x86 reference machine (`reference = "x86"` on the suite): `i386`
(small 32-bit C programs built static and dynamic, Debian's i386 BusyBox and CPython with part of its regression tests,
and 32- and 64-bit programs at once; in the gate, at 1, 2 and 4 vCPUs), and in the campaign tier `torture32` (the
torture suite built for i686), `ltp32` (the `ltp` suite's LTP and selection built for i686) and `libm32` (glibc's math
tests built for i686: x87 code; also with AFP hidden, and in accurate mode then). `ltp32` and `libm32` judge each test
by its own exit status, as `ltp` and `libm` do, against the reference machine's: a failure with the reference's own
status passes (it fails the same way there, as in `torture32`), and a test unsupported under natemu that passed there
fails. A `libm32` program that fails (all of them x87 long double or extended-precision deviations, listed in
`expected`) must also fail exactly as recorded for its floating-point mode, by the hash of its whole output and exit status
(`tests/correctness/libm32-fails-MODE.txt`); otherwise it is `changed`, a failure, so an expected program hides no new
failure and no case that fails by more. Record those after a change that alters them, on natemu, with
`TESTKIT_SEED_FAILURES=1`: delete the files first and record twice (a program whose failure differs between the two is
recorded `fp=*` and judged by failing alone; the family's test lists the only ones allowed). A recording run reports
what it records as unverified, and `--gate` refuses to start with `TESTKIT_SEED_FAILURES` or `TESTKIT_SEED_ANSWERS`
set. Its `expected_without.afp` entries name their deviation (`tests/lib/fp_deviations.py`); in accurate mode only
those whose deviation that mode keeps (`x87-band-ue-flag`) still apply. Their recipes share `tools/correctness/i386.sh` (gcc's
32-bit multilib in the sysroot, Debian's i386 packages through a third private apt), which the `x86st` recipe also uses
for the kernel selftests' `*_32` programs. The answers are files in the repo: each `i386` test's whole output in
`tests/correctness/i386/`, each `torture32` program's exit status and output hash in `tests/correctness/torture32.txt`,
each `ltp32` and `libm32` test's exit status in `tests/correctness/ltp32.txt` and `libm32.txt` (a suite's `answers`
key names its files: an answer for a test the image no longer lists is a stale-entry).
To record them after changing a program or the recipe, run the suite with `TESTKIT_SEED_ANSWERS=1
NATEMU_X86_HOST=user@host` (an x86-64 Linux machine with `zstd`, `cpio` and passwordless `sudo`): each part then runs
there instead of under natemu, in the image's own root (`chroot`), and the verdict writes the answer files; commit
them with the change. Such a run reports every test as unverified, never as passed, and writes
`tests/correctness/<suite>-reference.txt` (the machine's CPU and kernel, how it ran, the image). `ltp32`
(`reference_boot = "kvm"`) instead boots each part there under KVM, with the stock kernel, command line, vCPUs and RAM
of a natemu boot, since the kernel decides most LTP results, and the machine's CPU less protection keys,
which natemu's profile has not: the machine also needs Docker and `/dev/kvm` (QEMU is Debian's, at the recipes'
snapshot, installed in the builds' pinned container image at each boot). For `torture32`, `ltp32` and `libm32`, delete the answer file first
and seed twice: a program whose output hash differs between the two runs (it prints an address)
is recorded as `rc *` and compared by exit status alone (an `ltp32` or `libm32` test whose status differs, as `*`),
so seeding over an old file would mark changed programs that way too.

A recipe may also write `<suite>-boot.txt`, one `PART [kernel=FILE] WORD...` line per part: the kernel that part boots
(a file it built into the image folder, instead of the stock Debian kernel) and words added to its kernel command line.
The public campaign (`recipes/public`) uses it: its kernel test modules (KUnit, atomic64, bitmap, vmalloc, lock and RCU
torture) run on a 6.12.107 kernel it builds, since Debian's kernel ships none of them, and every row gets its guest
time limit as `pubcap=300`.
