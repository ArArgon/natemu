# Build-first executor guidance

Read [the workflow](docs/research/README.md) first. For implementation, read
[the implementation plan and setup](docs/implementation.md), then
[INV-03-01's current summary](docs/research/investigations/INV-03-01.md#current-direction--build-first-october-25-2026)
and its adjacent decision inventory. The plan owns implementation order; the
investigation owns current results and the next assignment. Consult the
[Track03 charter](docs/research/tracks/03-architecture-options.md) for applicable
requirements and the completed [feasibility charter](docs/research/tracks/01-feasibility.md)
for inherited findings. Follow detailed evidence only for a relevant premise.

Use independent subagents for consequential design/code reviews and evidence
replication; keep authors and reviewers separate. Fix editorial errors directly;
only substantive correctness, execution or reproduction defects block acceptance.

Do not read or search git history for archived Track01 material unless the task
names it. Before creating or revising error or learning records, read
[the error documentation guidance](docs/retrospectives/README.md).

## Testing

Run tests only through `tools/natemu-test`; read [the testing page](docs/testing.md) first. `tools/natemu-test run`
runs what your change concerns, `--tier smoke` one small case of everything, `--gate` everything. The gate is needed
before a change that can alter what natemu does merges into `main` or an integration branch. A change only to tests
and the test framework instead re-runs every test it adds, changes or can weaken, each new check shown to fail when it
should; docs-only changes and pushes of work branches need none, and the user may explicitly bypass either.
[When the gate is needed](docs/testing.md#when-the-gate-is-needed) and [how much to run](docs/testing.md#how-much-to-run)
give the details: iterate on narrow runs and keep heavy runs (gate, stress, benchmarks) to once per change set. A new
test is a case in a family under `tests/`, not a new script; the scripts a family runs live in its folder or
`tests/lib` ([tests/README.md](tests/README.md)) and refuse to start outside the framework. Do not start VMs outside the
framework's queue, and do not read test inputs from `natemu-raw`: the framework builds or downloads them into its cache.
Older pages under `docs/restructure/` that run `tools/run_suite.py` or scripts under `tools/` are historical records;
those commands no longer run ([docs/restructure/README.md](docs/restructure/README.md)).

### Every test is self-contained

A new Mac with a fresh clone, the prerequisites the [README](README.md) lists and `tools/natemu-test` must
be able to run every test and benchmark: the framework builds or downloads each input from what the repository declares
(a recipe in the repo, a fixed upstream source), into its own cache. Nothing may depend on what happens to be on one
machine: no file in a home folder, scratchpad, `/tmp` or another checkout, no image or disk prepared by hand, no
container image left on a build box, no tool installed outside the declared prerequisites (a tool a test needs is
declared so the framework checks it before the run, not discovered by a failure). The same holds for numbers: one
that goes into a doc, a commit message or a decision comes from a case the framework runs, so anyone can repeat it from
the repo, and says which case and machine ("bench case `guest-paper`, M2 Max", not a run folder's id). A one-off
script is fine while exploring; a number from it is an experiment and is called one, with what ran and where.
Done when deleting every machine's caches and scratch folders loses nothing but time.

### Benchmark numbers

[docs/benchmarks.md](docs/benchmarks.md) shows each benchmark's latest numbers, not their history, and it stays current:
a change that adds a benchmark, or moves one by more than its noise, updates the page in the same change, replacing the
numbers it moved rather than adding a row or a section. Its numbers come from the framework's bench cases, and each
table says which: the case, the machine, the natemu commit and the date.

The method is an experiment's. A number is compared only with numbers from the same machine: no table or ratio mixes
machines, because timings depend on the machine. (A gate may run as a mosaic, its families on different machines at one
commit, since pass and fail do not; a benchmark never may.) QEMU, on the same Mac as natemu, and the real-x86 reference
are the control group, measured under natemu's conditions (nothing else running, the same inputs and options); a control
older than natemu's numbers says when it was measured. The page's numbers come from the machine that holds the control
group (today the M4). A change's effect is its runs against the baseline's on one machine, a difference counted only
beyond the case's noise (the spread of its repeated runs on that machine). Run as little as the decision needs:
- **Reuse what exists.** Any run of the baseline on that machine with the same cases, inputs and options counts; do not
  measure it again. A case's noise, once known on a machine, is reused too.
- **One run of the change**, and another only when its difference is within about twice the noise.
- **Only the cases the change can move**; the full set once per change set.
- **Fail fast.** Build both sides and run one short case before a long run; stop at the first difference far beyond
  the noise and find its cause first.

A comparison with the page's numbers does not measure a change (they may come from another machine or another day). The A/B and what caused the difference go in the commit message, or in the plan or findings page
of the work; an A/B on another machine is evidence there, never the page's numbers. The page may say in a sentence what
moved a number, without the old value. An experiment (a profile, a prototype, an A/B of two builds, a case run with
other options) may show its numbers where it is discussed, labelled as an experiment; they become the page's numbers
once a bench case measures them.

## README

`README.md` is for a newcomer who wants to run natemu: plain prose, what to do and what they will see, hardware named by chip
(M1, M2, M4) and any unavoidable term explained in a few words. Mechanism, exact deviation lists, measurement history and tool
internals go on a page under `docs/`, linked from the README. Done when someone who has never seen the codebase understands
every README sentence.

## The FEX fork

`third_party/FEX` is natemu's own fork, never contributed upstream. Its `CLAUDE.md` line "AI must not be used to
generate code for contributions to this project" is upstream FEX's policy for contributions to FEX; it does not apply to
changes in natemu's fork (owner, November 23, 2026). Agents may change the fork as any other part of natemu, with the
same review and test rules. The next fork commit with a real FEX change also replaces that line in the fork's
`CLAUDE.md` with a pointer to this section (owner: "you can even overwrite the CLAUDE.md"), so the fork is rebuilt once.
