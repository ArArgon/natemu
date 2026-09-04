# INV-01-10 experiment — toolchain inventory probe

Measures, on this host, whether the build/test/debug toolchain `Q-01-10`
needs is present, how it resolves, and — added at the `REV-053`/`REV-054`
return — **where it actually comes from**: versions of `clang`, `lldb`,
`llvm-objdump`, `python3`, `rustc`, `git`, `codesign` (which has no
`--version` option; reported as `version: null` plus a
`version_unavailable_reason` string, not a fabricated version), each tagged
with a `provenance` classification (`CLT` / `OS` / `Homebrew` / `user-local`
/ `other`) since "a tool exists on this host" and "a tool is publicly
distributed for the supported hosts" (what `KA-09`/"sustainable" actually
asks) are different claims — on this host `rustc` resolves `user-local`
and `codesign` resolves `OS`, neither `CLT`; presence/absence of
`xcode-select`'s active developer directory, `xctrace` (tested via the
*resolved* binary, not a fresh `PATH` lookup, with its exit status and
captured failure message both in the JSON), `cmake`, `ninja`,
`qemu-system-x86_64`, and Homebrew; the Command Line Tools SDK directory
listing; the CLT package build identifier; and the exact `PATH` the probe
ran under.

Method reference: `../../docs/research/investigations/INV-01-10.md`.

## Layout

- `src/tools.sh` — POSIX `sh`, standard Unix tools only (`xcrun`, `command`,
  `pkgutil`, `sed`, `awk`), no third-party dependency. Emits one JSON object
  to stdout per invocation. Each checked tool is resolved with
  `xcrun --find <tool>` first (the Xcode/CLT-aware resolver actually used to
  dispatch these tools) and falls back to a plain `command -v` lookup for
  tools `xcrun` does not register (`rustc`, `brew`).
- `src/check.py` — Python 3 standard-library checker. Requires
  `tools.clang/python3/git/codesign.present` to be `true` on any
  non-`--negative` run (`KA-09`'s build/test/debug limb; `REV-054` C1) —
  internal consistency alone is not enough, since a run in which a required
  tool is simply absent stays internally consistent. A malformed (non-JSON)
  probe output is reported as a distinct exit status (`3`), not conflated
  with a rule violation (exit `1`) — `REV-054` C3.
- `run.sh` — the evidence run. Refuses without `NATEMU_RAW_ROOT` set to an
  existing directory outside the repository, resolved with `pwd -P` so a
  symlink into the repository cannot pass the guard (`REV-054` C5).
  Produces one positive inventory run, one `--negative` control run
  (invoked through the interpreter, `sh src/tools.sh`, not `./tools.sh`, so
  nothing under `experiments/` is ever touched, not even a `chmod` —
  `REV-054` C4), a `check.py` pass/fail record for each, and an environment
  capture (`sw_vers`, `git rev-parse HEAD`, `xcode-select -p`), all under
  `$NATEMU_RAW_ROOT/INV-01-10/<UTC-timestamp>-<git-short-rev>/`, plus a
  `SHA256SUMS.txt` of every file written.

## Negative control

`run.sh` always includes one run with `--negative`, which deliberately
reports `tools.clang.present: true` alongside an empty
`tools.clang.version`, violating the present-implies-nonempty-version
invariant. `check.py` must FAIL that output with exactly that violation;
its `negative.check.txt` output is retained as proof the checker detects
the injected defect. It establishes no claim. `check.py`'s required-outcome
rule additionally FAILs a run in which `clang` (or `python3`/`git`/
`codesign`) is simply absent — `KA-09` falsified on this cell — even though
every field stays internally consistent; this was a defect found and fixed
at the `REV-054` return, the same pattern found in `INV-01-01`'s checker.

## Build and run

There is no separate build step (`tools.sh` is a shell script). To run:

```sh
NATEMU_RAW_ROOT=/path/outside/repo ./run.sh
```

Development smoke tests (running `src/tools.sh` directly, or
`python3 src/check.py <file>` against a scratch-directory output) are not
evidence runs and must not write under `$NATEMU_RAW_ROOT`.
