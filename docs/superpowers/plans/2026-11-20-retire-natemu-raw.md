# Retire natemu-raw (planned, not started)

**Goal:** nothing in the test framework or the scripts it runs reads or writes `natemu-raw`; a Mac without that folder
runs every tier, the bench family included. Whether to archive or delete the folder afterwards is the owner's call (it
also holds historical records that docs link to); this plan does not touch it.

**State on 2026-11-20** (branch `remove-old-framework`): the families' runner cases already get the framework's input
root as `NATEMU_RAW_ROOT` and its state folder as `NATEMU_STATE_ROOT` (testkit/harness/runner.py), so no test reads
`natemu-raw`. Three uses remain:

1. **The VM lock** `natemu-raw/.vm.lock`: `testkit/sched/pool.py` (`raw_root`, `vm_lock_file`, `vm_lock`,
   `external_busy`) takes it so that a script run by hand under `tools/vmlock.py` and framework runs exclude each
   other; `tools/vmlock.py` takes it at `STATE_ROOT/.vm.lock` (`tools/raw_root.py`: `NATEMU_STATE_ROOT`, else the raw
   root). It existed for the old runners, which are gone.
2. **Bench inputs**: `testkit/harness/bench.py` passes the real `natemu-raw` to bench cases, and
   `tests/bench/run_m1_perf.py` reads `m1-perf/`, `batch1-fexc-20261116-03/obj/fexc`, `io-benchmark-prep-20261108-01`,
   `io3-block-20261108-01/inputs-01` and `io5-inputs-20261113-01` from it. Missing inputs make a bench case
   not-runnable.
3. **Scripts run by hand** (`tests/lib/testenv.py`'s manual path under `tools/vmlock.py`): `tools/raw_root.py` defaults
   `RAW_ROOT` to `natemu-raw` beside the main checkout and `STATE_ROOT` to the same folder, e.g.
   `tests/io/run_net_linux.py`'s `.net-ports.lock`.

## Steps

- [ ] **VM lock into the state folder.** One lock file in testkit's state folder (`paths.state_dir()`), used by
  `pool.vm_lock` and `tools/vmlock.py` alike; drop `raw_root()` from the pool. Keep the outside-VM check
  (`outside_vms`). Update `tools/test_vm_lock.py` and `testkit/tests/test_pool.py`.
- [ ] **Bench inputs from the framework.** Each input in item 2 becomes a framework input (a recipe, a pinned download or
  an existing artifact: `artifacts.fexc()` for the fexc build) in `tests/resources.toml` / `testkit/inputs.py`, and
  `bench.py` passes the input root instead of `raw_root()`. Record built outputs' hashes, never require them
  (owner's rule for built binaries).
- [ ] **Hand runs.** `tools/raw_root.py`'s defaults point at the framework's input root and state folder (prepared by
  `tools/natemu-test`), with a clear message when they are not prepared; `NATEMU_RAW_ROOT` still overrides.
- [ ] **Docs and checks.** README, docs/implementation.md, docs/testing.md and `tools/check_setup.py` stop naming
  `natemu-raw` as a requirement; a test in `testkit/tests/test_layout.py` fails if a file outside `docs/` refers to it.
- [ ] **Verify** with `NATEMU_RAW_ROOT` pointing at an empty folder: `tools/natemu-test run --gate`, then the bench
  family, on the M2 Max.
