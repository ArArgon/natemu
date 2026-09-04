# INV-01-09 experiment — device-backing capability probe

Measures, on this host/OS/SDK, the `Q-01-09`/`KA-08` falsifier facts: whether
each candidate unprivileged public host backing for the minimal device set
actually behaves as documented. `Q-01-07` (timer/APIC) has its own probe;
shutdown needs no capability probe (process exit).

Method reference: `../../docs/research/investigations/INV-01-09.md`.

## Layout

- `src/probe.c` — the C probe. Apple clang only, SDK headers only, no
  third-party dependency. Emits one JSON object to stdout per invocation:
  `vmnet_start_interface` (host-mode, both unentitled and ad-hoc-signed with
  the restricted `com.apple.vm.networking` entitlement), `socket()` for
  `SOCK_STREAM`/`SOCK_DGRAM`/`SOCK_RAW`, `posix_openpt`/`grantpt`/`unlockpt`,
  `getentropy` at 256 bytes and at 257 bytes (one byte past the documented
  per-call maximum), and block I/O (`F_NOCACHE`, `F_FULLFSYNC`, `pwrite`/
  `pread`, `F_PUNCHHOLE`, and a sparse `ftruncate` checked via `st_blocks`).
- `src/check.py` — Python 3 standard-library checker. Validates a probe
  output against the invariants a truthful run must satisfy; used both as
  the negative-control check and, later, as an independent sanity check on
  Observed results (it does not itself decide claim acceptance).
- `src/vmnet.entitlements.plist` — the `com.apple.vm.networking` entitlement,
  applied by `run.sh` for the entitled-ad-hoc invocation only.
- `build.sh [build-dir]` — compiles `src/probe.c` into the requested build
  directory and ad-hoc signs it with no entitlement. `run.sh` places that
  directory under the external run artifact.
- `run.sh` — the evidence run. Refuses without `NATEMU_RAW_ROOT` set to an
  existing directory outside the repository; writes nothing under
  `experiments/`. Produces three probe runs (unentitled, entitled-ad-hoc, and
  the entitled-ad-hoc binary's `--negative` control), a `check.py` pass/fail
  record for each, and an environment capture (`sw_vers`, `sysctl`,
  `clang --version`, `git rev-parse HEAD`, `codesign -d --entitlements -`,
  SDK path), all under
  `$NATEMU_RAW_ROOT/INV-01-09/<UTC-timestamp>-<git-short-rev>/`, plus a
  `SHA256SUMS.txt` of every file written. The runner fails closed unless the
  unentitled normal checker passes and the negative checker fails. The
  block-I/O and sparse-file
  checks write their transient temp files under that run's `scratch/`
  subdirectory (removed once empty after the run), never under `experiments/`.

## Negative control

`run.sh` includes an unentitled run with `--negative`, before applying the
restricted entitlement, which deliberately
reports `getentropy_over_limit.ok` as `true` regardless of the real result of
requesting 257 bytes from `getentropy` in one call. `getentropy(2)` (`SRC-115`)
documents a 256-byte maximum per call, so a truthful run must observe failure
at 257 bytes; `check.py` asserts `getentropy_over_limit.ok == false`
unconditionally. The negative run must FAIL that check, and its
`negative.check.txt` output is retained as proof the checker detects the
injected defect. It establishes no claim.

## Build and run

```sh
./build.sh
NATEMU_RAW_ROOT=/path/outside/repo ./run.sh
```

Development smoke tests (compiling and running `build/probe` directly, or
`python3 src/check.py <file>` against a scratch-directory output) are not
evidence runs and must not write under `$NATEMU_RAW_ROOT`.
