# INV-01-03 — address-space representation probe

Measures what one host process can reserve, back, alias, and protect, for
`Q-01-03` (address spaces) and kill assumption `KA-03`.

- Method record: `docs/research/investigations/INV-01-03.md`
- Breadth pass: `docs/research/breadth-passes/BP-01-03-start.md`
- Manifests: recorded under `docs/research/manifests/` after the evidence run.

## What it does

| Probe | Observable |
| --- | --- |
| page size | `vm_page_size`, `getpagesize`, `sysconf(_SC_PAGESIZE)`, `hw.pagesize`, compile-time `PAGE_SIZE` |
| reservation | `mmap` `PROT_NONE` at 8 GiB, 64 GiB, 1 TiB: success and `errno` |
| commit and touch | 8 GiB `PROT_READ|PROT_WRITE` mapping, a bounded 2 GiB sample touched one host page at a time; resident size and `phys_footprint` from `task_info` before and after |
| aliasing | `mach_vm_remap` of a region, write through one view and read through the other, in both directions, with a same-view read-back positive control |
| sub-host-page protection | `mprotect` of a 4 KiB-aligned range that is not host-page-aligned: return value, `errno`, and the writability of each 4 KiB slot afterwards |
| advice | `mincore` and `madvise(MADV_FREE_REUSABLE)` return values |

The 8 GiB mapping is touched over a bounded 2 GiB sample rather than in full:
the mandatory 8 GiB guest cell is a *coverage* question (`Q-05-02`), and
touching 8 GiB on a 16 GiB host measures the memory compressor.

## Build and run

    export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw
    ./run.sh

`run.sh` refuses to run unless `NATEMU_RAW_ROOT` is set, is an existing
directory, and lies outside this repository. It builds into the run directory
under the raw root, so nothing is written under `experiments/`. Each run
produces `result.json`, `result-negative.json`, `check.txt`,
`check-negative.txt`, `env.txt`, `build.log`, and the measured `probe` binary.

## Negative control

`./probe --negative` writes a pattern through the source mapping that differs
from the one it then compares against, so the same-view read-back positive
control fails. `src/check.py` must report `FAIL` on that output and `PASS` on
the normal output; `run.sh` runs both and prints each verdict.

A capability the platform does not offer is reported as a `NOTE`, not a
`FAIL`: the checker judges the probe, not the platform.
