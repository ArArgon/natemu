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
| concurrent tracking | two threads faulting on disjoint page ranges at once: fault count, per-thread round-trip time, and attribution checked as *recorded thread identity against address range* — each thread stores its identity in thread-local storage before faulting, so the control can actually fail |
| granularity | `mprotect` of a 4 KiB sub-range of a host page (return and `errno`), a host-page-aligned control, and the writability of each 4 KiB slot afterwards |
| signal classes | write to a mapped read-only page versus write to an unmapped page: which signal and `si_code` each produces |
| software alternative | two paired measurements, 8 repetitions each with two discarded warm-up passes per variant and alternating order: a whole-loop pair (store versus store-plus-bitmap) whose difference is explicitly **not** interpretable as a bitmap cost, and an isolated pair (index arithmetic versus index arithmetic plus the bitmap update) from which a marginal can be read |

The whole-loop pair is reported for context only: its baseline is a
cache-missing store whose retirement the added work perturbs, so its difference
measures the pair of loops rather than the bitmap. The isolated pair removes
that store from both sides. A marginal counts only if its sign is identical in
every repetition and its median exceeds the mean of the two variants'
run-to-run spreads; a resolvable *negative* marginal fails the run, because
adding work cannot make a loop faster.

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
output, and takes the expected mode as its second argument so a negative-mode
artifact can never be checked as a normal one. An absent platform capability is
a `NOTE`, not a `FAIL`.
