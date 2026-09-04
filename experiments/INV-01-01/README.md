# INV-01-01 experiment — platform capability probe

Measures, on this host/OS/SDK, the falsifier facts `Q-01-01` needs and the
`KA-01`/`KA-02` kill-assumption checks: `MAP_JIT` mmap admissibility, the
`pthread_jit_write_protect_np`/`pthread_jit_write_protect_supported_np`/
`pthread_jit_write_with_callback_np` write/execute toggle, `mach_vm_remap`
page aliasing, `mach_absolute_time`/`mach_timebase_info`, thread QoS/affinity
API return codes, `getentropy`, `posix_openpt`, `setrlimit(RLIMIT_NOFILE)`,
and Hypervisor.framework (`hv_vm_create`, `hv_vm_get_max_vcpu_count`,
`hv_vcpu_create`, and `hv_vcpu_get_sys_reg(ID_AA64MMFR0_EL1)` decoded into
PARange/TGran4/TGran16/TGran64), both unentitled and ad-hoc-signed with
`com.apple.security.hypervisor`.

Method reference: `../../docs/research/investigations/INV-01-01.md`.

## Layout

- `src/probe.c` — the C probe. Apple clang only, SDK headers only, no
  third-party dependency. Emits one JSON object to stdout per invocation.
- `src/check.py` — Python 3 standard-library checker. Validates a probe
  output against the invariants a truthful run must satisfy; used both as
  the negative-control check and, later, as an independent sanity check on
  Observed results (it does not itself decide claim acceptance).
- `src/hypervisor.entitlements.plist` — the `com.apple.security.hypervisor`
  entitlement, applied by `run.sh` for the entitled invocation only.
- `build.sh` — compiles `src/probe.c` to `build/probe` (git-ignored) and
  ad-hoc signs it with no entitlement.
- `run.sh` — the evidence run. Refuses without `NATEMU_RAW_ROOT` set to an
  existing directory outside the repository; writes nothing under
  `experiments/`. Produces three probe runs (unentitled, entitled, and the
  entitled binary's `--negative` control), a `check.py` pass/fail record for
  each, and an environment capture (`sw_vers`, `sysctl`, `clang --version`,
  `git rev-parse HEAD`, `codesign -d --entitlements -`, SDK path), all under
  `$NATEMU_RAW_ROOT/INV-01-01/<UTC-timestamp>-<git-short-rev>/`, plus a
  `SHA256SUMS.txt` of every file written.

## Negative control

`run.sh` always includes one run with `--negative`, which deliberately
reports `jit_write_protect.executed_value` as `0xBAD` regardless of the real
executed value. `check.py` asserts `executed_value == expected_value`
whenever `toggle_ok` is true; the negative run must FAIL that check, and its
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
