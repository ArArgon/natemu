# tests/

Everything here runs through `tools/natemu-test` ([docs/testing.md](../docs/testing.md)).

- **`tests/<family>/`**: one test family. `family.toml` declares its suites and cases, `tests.lock` holds its locked
  answers, and the folder holds the scripts its cases run (`run_*.py`), what only those scripts use (`*_expected.py`
  models, judges), and their unit tests (`test_*.py`, run by the `unit` family).
- **`tests/lib/`**: scripts and modules that more than one family uses (`run_slice1_vm.py`, `io_common.py`, the
  answer caches, the shared expected models), with their unit tests.
- **`tools/`** keeps the development tools: `natemu-test`, the compiler (`tools/fexc`), the reference supplier
  (`tools/reference`), the models (`tools/models`), the benchmarks' and campaigns' image builders (`tools/bench`,
  `tools/correctness`, `tools/io`), `check_setup.py`, `vmlock.py`, `raw_root.py`.

## The scripts refuse to run by hand

Every script here starts with one line that imports `tests/lib/testenv.py`. That puts `tests/lib`, every family folder,
`tools` and `tools/reference` on the import path, so a script finds what it shares with other families wherever it
lives. Run as a program, a script first calls `testenv.require_framework(__file__)`:

- A script a suite runs only starts under `tools/natemu-test`, which queues its VMs, gives it the framework's inputs and
  locked answers, and judges it. Started by hand, it says which command runs it, for example
  `tools/natemu-test run tests/faults -k i5c --keep-outputs`. The case's log starts with the exact command and the
  settings the framework gave it.
- A script no suite runs (the mutation checks in `tests/system`, the I/O measurements and diagnostics in `tests/io`,
  `tests/smp/run_capacity_linux.py`, `tests/boot/run_l1_smoke.py`) starts only under `python3 tools/vmlock.py
  shared|exclusive python3 tests/...`, which queues it with the framework's work.

Importing a script, as the unit tests do, is never refused.

## Adding a script

Put it in the folder of the family whose cases run it, or in `tests/lib` if several families import it. Start it with the
same import line as its neighbours, and call `testenv.require_framework(__file__)` first thing under
`if __name__ == '__main__':`. If another family's runners reach it (by import or by path), add its folder to that
family's `covers`. `testkit/tests/test_layout.py` checks all of this.
