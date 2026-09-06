# Research experiments

Experiment code lives in `experiments/INV-<track>-<nn>/`. Each README describes
operation, inputs, controls, failure behavior and output interpretation. The
[owning investigation](../docs/research/investigations/README.md) records scientific
conclusions, applicability and current work. Follow [the research workflow](../docs/research/README.md)
for pre-execution and findings reviews. Synthetic validation and compilation are
preparation, not experimental evidence.

New evidence runs use `NATEMU_RAW_ROOT` outside Git, retain a source/dependency
and build-input snapshot, build from that snapshot, and associate build, controls
and results through one run identity. Keep artifacts and checksums even when a
run fails. Correctness and timing qualification are separate outputs.

The active Mach (`INV-01-06/m1`), clock prerequisite (`INV-01-07/c1`), SMP
(`INV-01-08`) and shared-code (`INV-01-18`) runners use explicit new formats.
Other retained runners are historical readers/producers: their documented
commands explain recovery, and their original code remains recoverable in Git.
Before new evidence collection with a historical runner, apply the retained-run
requirements in the workflow and obtain pre-execution review.

No experiment result is implementation or an architecture choice. Inherited
material at or before `a121702` remains provenance only.
