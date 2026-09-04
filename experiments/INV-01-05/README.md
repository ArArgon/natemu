# INV-01-05 — guest-memory write-tracking and invalidation-cost probe

Measures the public mechanisms for tracking modification of guest memory, their
granularity, and their cost, for `Q-01-05` and kill assumption `KA-07`.

- Method record: `docs/research/investigations/INV-01-05.md`
- Breadth pass: `docs/research/breadth-passes/BP-01-05-start.md`

## What it does

| Probe | Observable |
| --- | --- |
| write tracking | 64 MiB region set to `PROT_READ`; a `SIGSEGV`/`SIGBUS` handler records `si_addr`, re-protects the containing host page, and returns so the store retries. Fault count, address attribution, and round-trip time |
| `mprotect` cost | alternating protections on one host page, timed per call |
| concurrent tracking | two threads faulting on disjoint page ranges at once: fault count, per-thread address attribution, and per-thread round-trip time |
| granularity | `mprotect` of a 4 KiB sub-range of a host page (return and `errno`), a host-page-aligned control, and the writability of each 4 KiB slot afterwards |
| signal classes | write to a mapped read-only page versus write to an unmapped page: which signal and `si_code` each produces |
| software alternative | per-store cost of a guest-page-granularity dirty bitmap against the same loop without it, with a discarded warm-up pass and alternating order |

The bitmap comparison reports its own resolution: the run-to-run spread of the
unmodified loop bounds what the differential can mean, and the record declares
whether the difference is resolvable at all.

## Build and run

    export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw
    ./run.sh

`run.sh` refuses to run unless `NATEMU_RAW_ROOT` is set, is an existing
directory, and lies outside this repository, and builds into the run directory
so nothing is written under `experiments/`.

## Negative control

`./probe --negative` makes the fault handler record a null faulting address
instead of `si_addr`, so the tracking mechanism cannot name the modified page.
`src/check.py` must report `FAIL` on that output and `PASS` on the normal
output. An absent platform capability is a `NOTE`, not a `FAIL`.
