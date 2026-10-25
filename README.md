# natemu

natemu is building an x86-64 Linux system emulator for Apple Silicon/macOS26,
using host-side translation and a resident ARM64 runtime inside a VM. The goal is
material improvement over QEMU TCG within an explicit correctness scope.

**Current state:** I2 connects the Rust resident runtime to a real host-side FEX
compiler worker. Misses compile admitted x86 bytes, validate replies and execute
the resulting ARM64 code; checks cover cache reuse, stale replies, cancellation
and capacity. This is a finite one-vCPU development profile. Paging recovery,
concurrent execution, stock Linux boot and workload performance remain ahead.

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
