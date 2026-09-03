# natemu

natemu is an unvalidated research project. Its decision question is whether an
unmodified x86-64 Linux system emulator on Apple Silicon using public macOS APIs
can be materially better than QEMU TCG while meeting an explicit correctness
scope.

The SMP baseline is in scope from the outset: research must treat
multi-processor guest behavior and relevant platform dimensions explicitly.

The project is in its research phase. Begin with the
[research workspace](docs/research/README.md).

The project lifecycle is research, then planning, then implementation. Research
establishes evidence and constraints; planning turns those findings into a
proposed approach only after research authorizes it; implementation begins only
after planning is completed and approved and implementation is separately
authorized.

Material in historical commit `a121702` is untrusted history, not validated
evidence or a current implementation direction.

The reset design and plan under `docs/superpowers/` are kept as records of
that decision and use older vocabulary: their "harness" is what the research
documents now call experiment code (and the "harness gate" is the code gate),
and their "spike" is now a prototype.
