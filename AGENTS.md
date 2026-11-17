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

## README

`README.md` is for a newcomer who wants to run natemu: plain prose, what to do and what they will see, hardware named by chip
(M1, M2, M4) and any unavoidable term explained in a few words. Mechanism, exact deviation lists, measurement history and tool
internals go on a page under `docs/`, linked from the README. Done when someone who has never seen the codebase understands
every README sentence.
