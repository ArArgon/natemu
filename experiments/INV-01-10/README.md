# INV-01-10 experiment — toolchain inventory probe

Measures, on this host, whether the build/test/debug toolchain `Q-01-10`
needs is present and how it resolves: versions of `clang`, `lldb`,
`llvm-objdump`, `python3`, `rustc`, `git`, `codesign`; presence/absence of
`xcode-select`'s active developer directory, `xctrace` (including whether it
is functional without Xcode.app), `cmake`, `ninja`, `qemu-system-x86_64`, and
Homebrew; the Command Line Tools SDK directory listing; the CLT package
build identifier; and the exact `PATH` the probe ran under.

Method reference: `../../docs/research/investigations/INV-01-10.md`.

## Layout

- `src/tools.sh` — POSIX `sh`, standard Unix tools only (`xcrun`, `command`,
  `pkgutil`, `sed`, `awk`), no third-party dependency. Emits one JSON object
  to stdout per invocation. Each checked tool is resolved with
  `xcrun --find <tool>` first (the Xcode/CLT-aware resolver actually used to
  dispatch these tools) and falls back to a plain `command -v` lookup for
  tools `xcrun` does not register (`rustc`, `brew`).
- `src/check.py` — Python 3 standard-library checker. Validates a `tools.sh`
  output against the invariant every truthful run must satisfy: a tool
  reported present has a non-empty path and (where applicable) version; a
  tool reported absent has both null.
- `run.sh` — the evidence run. Refuses without `NATEMU_RAW_ROOT` set to an
  existing directory outside the repository; writes nothing under
  `experiments/`. Produces one positive inventory run, one `--negative`
  control run, a `check.py` pass/fail record for each, and an environment
  capture (`sw_vers`, `git rev-parse HEAD`, `xcode-select -p`), all under
  `$NATEMU_RAW_ROOT/INV-01-10/<UTC-timestamp>-<git-short-rev>/`, plus a
  `SHA256SUMS.txt` of every file written.

## Negative control

`run.sh` always includes one run with `--negative`, which deliberately
reports `tools.clang.present: true` alongside an empty
`tools.clang.version`, violating the present-implies-nonempty-version
invariant. `check.py` must FAIL that output with exactly that violation;
its `negative.check.txt` output is retained as proof the checker detects
the injected defect. It establishes no claim.

## Build and run

There is no separate build step (`tools.sh` is a shell script). To run:

```sh
NATEMU_RAW_ROOT=/path/outside/repo ./run.sh
```

Development smoke tests (running `src/tools.sh` directly, or
`python3 src/check.py <file>` against a scratch-directory output) are not
evidence runs and must not write under `$NATEMU_RAW_ROOT`.
