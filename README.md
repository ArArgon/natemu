# natemu

natemu is building an x86-64 Linux system emulator for Apple Silicon/macOS26,
using host-side translation and a resident ARM64 runtime inside a VM. The goal is
material improvement over QEMU TCG within an explicit correctness scope.

**Current state:** I1–I4 are implemented and independently reproduced in finite
profiles. A real host-side FEX worker feeds the Rust resident runtime; tests cover
precise recovery/paging and N=1/2/3/4 concurrent faults, remaps, code retirement,
pending events and sustained maintenance fairness. The complete regression run
passes87 jobs. Next is the post-I4 integrated validation pass. Arbitrary guest
programs, stock Linux boot and workload performance remain ahead.

Start with the [implementation plan and local setup](docs/implementation.md).
It records Rust/assembly/C++ ownership, the early producer loop, N-vCPU design,
boot/initramfs progression and measurements alongside development. Read the
[workflow](docs/research/README.md) for execution/review rules and
[INV-03-01](docs/research/investigations/INV-03-01.md#current-direction--build-first-october-25-2026)
for current results and the next assignment.

```sh
./tools/with-env python3 -B tools/check_setup.py
```

The launcher supplies the durable sibling raw-data directory unless explicitly
overridden. The check installs nothing and launches no VM or container. Missing
inputs are reported separately from unverified runtime/FEX capabilities.

Grow one working emulator through tested increments. Plan the next executable
slice; resolve consequential unknowns as they arise. SMP is designed for configured
N throughout, with one/two-vCPU bring-up and early three/four-vCPU checks.

Historical material at commit `a121702` and tag `track01-archive-2026-09-06` is
not current direction or evidence; do not search it unless the task names it.
The old reset plans under `docs/superpowers/` are historical. The frozen
[Track01 reproduction program](experiments/track01/README.md) remains unchanged.
