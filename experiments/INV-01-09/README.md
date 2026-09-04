# INV-01-09 experiment — device-backing capability probe

Measures, on this host/OS/SDK, the `Q-01-09`/`KA-08` falsifier facts: whether
each candidate unprivileged public host backing for the minimal device set
actually behaves as documented. `Q-01-07` (timer/APIC) has its own probe;
shutdown needs no capability probe (process exit).

Method reference: `../../docs/research/investigations/INV-01-09.md`.

## Layout

- `src/probe.c` — the C probe. Apple clang only, SDK headers only, no
  third-party dependency. Emits one JSON object to stdout per invocation:
  `vmnet_start_interface` (host-mode; `--scratch-dir` is required, not
  defaulted, so a direct invocation from this tree cannot write temp files
  under `experiments/`), `socket()` for `SOCK_STREAM`/`SOCK_DGRAM`/
  `SOCK_RAW`, `posix_openpt`/`grantpt`/`unlockpt`, `getentropy` at 256 bytes
  and at 257 bytes (one byte past the documented per-call maximum), and
  block I/O (`F_NOCACHE`, `F_FULLFSYNC`, `pwrite`/`pread`, `F_PUNCHHOLE` with
  `st_blocks` captured immediately before/after the punch on the same file
  the chain wrote, and a sparse `ftruncate` of that same file checked via
  `st_blocks`). An unrecognized flag exits 2 with a usage message.
- `src/check.py` — Python 3 standard-library checker. Validates a probe
  output against the invariants a truthful run must satisfy; used both as
  the negative-control check and, later, as an independent sanity check on
  Observed results (it does not itself decide claim acceptance). It does
  not hard-fail a run in which the vmnet completion handler never fires
  within the 5 s wait (`callback_fired: false`, `timed_out: true`) — that is
  a predeclared, reportable outcome, not a checker defect — and it does not
  hard-fail a disagreement between the synchronous return value and the
  callback status, since no source establishes those must agree.
- `src/vmnet.entitlements.plist` — the restricted `com.apple.vm.networking`
  entitlement under test, applied by `run.sh` for the entitled-restricted
  invocation.
- `src/benign.entitlements.plist` — `com.apple.security.get-task-allow`, a
  non-restricted entitlement applied for the entitled-benign attribution
  control (see Attribution control below).
- `build.sh [build-dir]` — compiles `src/probe.c` into the requested build
  directory and ad-hoc signs it with no entitlement. `run.sh` places that
  directory under the external run artifact.
- `run.sh` — the evidence run. Refuses without `NATEMU_RAW_ROOT` set to an
  existing directory outside the repository, and refuses unless the tree is
  committed; writes nothing under `experiments/`. Produces, in this order:
  an unentitled normal arm, an unentitled `--negative` arm (fault injection
  happens before any entitlement is applied), an entitled-benign normal arm,
  and an entitled-restricted normal arm; a `check.py` pass/fail record for
  each process that exits cleanly; `codesign -d --entitlements -` and
  `codesign --verify --verbose` for every signed variant; the raw process
  exit status with any termination signal decoded
  (`entitled-*.exit.txt`); the executing UID (`identity.txt`, see below);
  and an environment capture (`sw_vers`, `sysctl`, `clang --version`,
  `git rev-parse HEAD`, SDK path), all under
  `$NATEMU_RAW_ROOT/INV-01-09/<UTC-timestamp>-<git-short-rev>/`, plus a
  `SHA256SUMS.txt` of every file written. The block-I/O and sparse-file
  checks write their transient temp files under that run's `scratch/`
  subdirectory (removed once empty after the run), never under
  `experiments/`.

## Executing identity

`BP-01-09-start`'s measurement-failure-mode coverage requires recording the
exact executing UID for every unprivileged/`EPERM` claim, notably
`SOCK_RAW`. `run.sh` writes `id`/`id -u` to `identity.txt`. TCC/permission
grant state is a stated method decision, not captured: no public,
unprivileged command-line tool reads this account's TCC grant database
without itself requiring a grant. Consequence: the `SOCK_RAW` `EPERM` result
establishes that the recorded UID lacked raw-socket privilege under its
ambient state at run time; it does not establish what that ambient state
was beyond the UID itself, and does not generalize to a differently
provisioned account.

## Attribution control

A single entitled arm cannot attribute a termination to the *restricted*
status of `com.apple.vm.networking` specifically, as opposed to ad-hoc
signing with any entitlement or a malformed plist. `run.sh` therefore signs
and runs a second entitled variant carrying
`com.apple.security.get-task-allow` — a non-restricted entitlement — under
the same ad-hoc identity before signing and running the restricted variant.
If the benign arm exits cleanly while the restricted arm is killed, the
termination is attributable to `com.apple.vm.networking`'s restricted
status specifically. `codesign --verify --verbose` output is captured for
every signed variant so a signing failure is distinguishable from a runtime
kill.

## Negative control

`run.sh` includes an unentitled run with `--negative`, before applying any
entitlement, which deliberately reports `getentropy_over_limit.ok` as `true`
regardless of the real result of requesting 257 bytes from `getentropy` in
one call. `getentropy(2)` (`SRC-115`) documents a 256-byte maximum per call,
so a truthful run must observe failure at 257 bytes; `check.py` asserts
`getentropy_over_limit.ok == false` unconditionally. The negative run must
FAIL that check, and `run.sh` scores the fault injection as demonstrated
only when the checker's exit status is exactly `1` *and* its output reports
at least one violated rule — a checker usage error or a crash on truncated
output does not count as a successful demonstration. `negative.check.txt` is
retained as proof the checker detects the injected defect. It establishes
no claim.

## Build and run

```sh
./build.sh
NATEMU_RAW_ROOT=/path/outside/repo ./run.sh
```

Development smoke tests (compiling and running `build/probe` directly with
`--scratch-dir` pointed at a scratch directory, or `python3 src/check.py
<file>` against a scratch-directory output) are not evidence runs and must
not write under `$NATEMU_RAW_ROOT`. `./validate.sh` runs the fixture-based
preflight check (`tests/valid.json` must PASS, `tests/negative.json` must
FAIL) without touching the raw root.
