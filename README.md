# natemu

natemu is an unvalidated research project. Its decision question is whether an
unmodified x86-64 Linux system emulator on Apple Silicon using public macOS APIs
can be materially better than QEMU TCG while meeting an explicit correctness
scope.

The SMP baseline is in scope from the outset: when separately authorized,
research must treat multi-processor guest behavior and relevant platform
dimensions explicitly.

This checkout organizes the research phase only; it does not execute research,
experiments, probes, or benchmarks. Begin with the
[research workspace](docs/research/README.md).

The project lifecycle is research, then planning, then implementation. Research
establishes evidence and constraints; planning turns those findings into a
proposed approach; implementation follows only after planning.

Material in historical commit `a121702` is untrusted history, not validated
evidence or a current implementation direction.
