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
before a change that can alter what natemu or the tests do merges into `main` or an integration branch; docs-only
changes and pushes of work branches need none, and the user may explicitly bypass it.
[When the gate is needed](docs/testing.md#when-the-gate-is-needed) and [how much to run](docs/testing.md#how-much-to-run)
give the details: iterate on narrow runs and keep heavy runs (gate, stress, benchmarks) to once per change set. A new test is a case in a family under `tests/`, not a new script. Do not run
`tools/run_suite.py` or start VMs outside the framework's queue, and do not read test inputs from `natemu-raw`: the
framework builds or downloads them into its cache. Older pages under `docs/restructure/` that say to run
`run_suite.py` predate this.

## README

`README.md` is for a newcomer who wants to run natemu: plain prose, what to do and what they will see, hardware named by chip
(M1, M2, M4) and any unavoidable term explained in a few words. Mechanism, exact deviation lists, measurement history and tool
internals go on a page under `docs/`, linked from the README. Done when someone who has never seen the codebase understands
every README sentence.
