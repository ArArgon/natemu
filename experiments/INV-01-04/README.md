# INV-01-04 — executable-memory (`MAP_JIT`) probe

Measures whether and how generated code can be made executable, and what each
write/execute transition costs, for `Q-01-04` and kill assumption `KA-01`.

- Method record: `docs/research/investigations/INV-01-04.md`
- Breadth pass: `docs/research/breadth-passes/BP-01-04-start.md`

## What it does

| Probe | Observable |
| --- | --- |
| support | `pthread_jit_write_protect_supported_np()` |
| allocation limits | `MAP_JIT` mappings of 256 MiB, 1 GiB, 4 GiB: success and `errno` |
| toggle cost | `pthread_jit_write_protect_np(0)` / `(1)`, timed with `mach_absolute_time`, reported raw and in batches of 100 pairs because a single toggle sits at or below the timebase tick |
| write, invalidate, execute | an assembled arm64 stub (`movz w0,#42; ret`), `sys_icache_invalidate`, then called |
| cross-thread execution | thread A holds the region writable while thread B executes from it: executed or faulted, and with which signal |
| same-thread execution while writable | the same test on the thread that holds write access |
| write without toggling | expected to fault; records signal, `si_code`, and whether `si_addr` is the region — also a `Q-01-06` datum |

Signing configuration is captured per run by `codesign -d --entitlements -` on
the measured binary in `env.txt`. Results apply to the signing configuration
recorded there and to no other.

## Build and run

    export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw
    ./run.sh

`run.sh` refuses to run unless `NATEMU_RAW_ROOT` is set, is an existing
directory, and lies outside this repository, and builds into the run directory
so nothing is written under `experiments/`.

## Negative control

`./probe --negative` assembles the stub to return 41 while the record still
declares 42, a code-generation defect. `src/check.py` must report `FAIL` on
that output and `PASS` on the normal output. An absent platform capability is
a `NOTE`, not a `FAIL`.
