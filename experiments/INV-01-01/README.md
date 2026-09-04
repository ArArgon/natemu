# INV-01-01 experiment — platform capability probe

Measures, on this host/OS/SDK, the falsifier fact `Q-01-01`/`KA-01` needs:
`MAP_JIT` mmap admissibility, the `pthread_jit_write_protect_np`/
`pthread_jit_write_protect_supported_np`/`pthread_jit_write_with_callback_np`
write/execute toggle (the two paths use distinct constants — `42` vs `43` —
so a callback that writes nothing cannot be mistaken for one that ran),
`mach_vm_remap` page aliasing, `mach_absolute_time`/`mach_timebase_info`,
thread QoS/affinity API return codes, `getentropy`, `posix_openpt`,
`setrlimit(RLIMIT_NOFILE)`, and `kern.hv_support`. It also probes
Hypervisor.framework (`hv_vm_create`, `hv_vm_get_max_vcpu_count`,
`hv_vcpu_create`, and `hv_vcpu_get_sys_reg(ID_AA64MMFR0_EL1)`, reported as
`id_aa64mmfr0_el1_raw` only — no decoded PARange/TGran fields; see
`INV-01-01.md` source inventory for why), both unentitled and ad-hoc-signed
with `com.apple.security.hypervisor`, as an input to `Q-01-08`/`Q-01-12`/
family `F3`. **`KA-02` (Hypervisor.framework x86-64 execution on Apple
Silicon) is decided documentarily in `INV-01-01.md`, not by this probe** —
the arm64 SDK build this probe links against has no x86-64 VM surface to
call at all, so no probe on this platform could exercise it.

Method reference: `../../docs/research/investigations/INV-01-01.md`.

## Layout

- `src/probe.c` — the C probe. Apple clang only, SDK headers only, no
  third-party dependency. Emits one JSON object to stdout per invocation.
- `src/check.py` — Python 3 standard-library checker. Requires the
  `KA`-relevant fields for a non-`--negative` run — `map_jit.mmap_ok`,
  `jit_write_protect.toggle_ok`/`crashed`, and (keyed on the invocation's
  own `label`) `hypervisor.hv_vm_create_hr` matching `HV_DENIED` for
  `unentitled` or `HV_SUCCESS` for `entitled` — plus internal-consistency
  rules (a call reported not-attempted must be `null`; a section that did
  not crash must carry `crash_signal: 0`). A run in which a capability
  simply failed is not enough to PASS.
- `src/hypervisor.entitlements.plist` — the `com.apple.security.hypervisor`
  entitlement, applied by `run.sh` for the entitled invocation only. The
  `com.apple.security.cs.jit-write-allowlist` entitlement, which per
  `SRC-059` would make `pthread_jit_write_protect_np` and
  `pthread_jit_write_with_callback_np` mutually exclusive, is not exercised
  by any run here — see `INV-01-01.md` Limitations.
- `build.sh [output-dir]` — compiles `src/probe.c` against
  `-isysroot "$(xcrun --show-sdk-path)"` (pinning the build to the resolved
  SDK by construction) and ad-hoc signs it with no entitlement. Defaults to
  `./build` (git-ignored) for local dev; `run.sh` passes a `mktemp -d`
  directory instead.
- `run.sh` — the evidence run. Refuses without `NATEMU_RAW_ROOT` set to an
  existing directory outside the repository, resolved with `pwd -P` so a
  symlink into the repository cannot pass the guard; builds into a
  `mktemp -d` directory so nothing is written under `experiments/`; wraps
  every probe invocation in a portable wall-clock timeout (30 s) so a hung
  probe cannot fill the raw root. Produces three probe runs (unentitled,
  entitled, and the entitled binary's `--negative` control), a `check.py`
  pass/fail record for each, a positive-control check (the unentitled and
  entitled `hv_vm_create_hr` must differ, or the run is flagged), and an
  environment capture (`sw_vers`, `sysctl` including `kern.hv_support`,
  `clang --version`, `git rev-parse HEAD`, `codesign -d --entitlements -`,
  the resolved SDK path, and the SHA-256 of every SDK header the
  investigation cites), all under
  `$NATEMU_RAW_ROOT/INV-01-01/<UTC-timestamp>-<git-short-rev>/`, plus a
  `SHA256SUMS.txt` of every file written.

## Negative control

`run.sh` always includes one run with `--negative`, which deliberately
reports `jit_write_protect.executed_value` as `0xBAD` regardless of the real
executed value. `check.py` asserts `executed_value == expected_value`
whenever `toggle_ok` is true; the negative run must FAIL that check, and its
`negative.check.txt` output is retained as proof the checker detects the
injected defect. It establishes no claim. `check.py`'s required-outcome
rules additionally FAIL a run in which `MAP_JIT` is unavailable (`KA-01`
falsified) even though every field stays internally consistent — this was a
defect found and fixed at the `REV-039` return: a checker that only checks
consistency lets "everything failed" PASS.

## Build and run

```sh
./build.sh
NATEMU_RAW_ROOT=/path/outside/repo ./run.sh
```

Development smoke tests (compiling and running `build/probe` directly, or
`python3 src/check.py <file>` against a scratch-directory output) are not
evidence runs and must not write under `$NATEMU_RAW_ROOT`.
