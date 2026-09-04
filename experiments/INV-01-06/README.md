# INV-01-06 — exception delivery and restartability probe

Measures whether a fault raised inside generated code can be caught with full
register state and the thread resumed at a chosen PC, for `Q-01-06` and kill
assumption `KA-04`.

- Method record: `docs/research/investigations/INV-01-06.md`
- Breadth pass: `docs/research/breadth-passes/BP-01-06-start.md`

## What it does

| Probe | Observable |
| --- | --- |
| state structs | `__DARWIN_OPAQUE_ARM_THREAD_STATE64`, `__has_feature(ptrauth_calls)`, the `_STRUCT_ARM_EXCEPTION_STATE64` variant the build **compiles against** (`__esr` width — a header property, not an observation), and `sizeof(_STRUCT_MCONTEXT64)`; each fault case additionally records the `uc_mcsize` the kernel reported, which is the observation that bears on what was delivered |
| fault and resume | arm64 stubs in a `MAP_JIT` region that fault on an unmapped load, an undefined instruction, and `brk #0`. The handler reads `__pc`, `__sp`, `__x[]`, `__es.__esr`, `__es.__far`, then skips the faulting instruction and writes a declared sentinel into `x0`; the caller checks the returned value |
| resume path | the PC is written through `__darwin_arm_thread_state64_set_pc_fptr`, and the build's `__DARWIN_OPAQUE_ARM_THREAD_STATE64` / `ptrauth_calls` settings are recorded. In the non-opaque form that accessor preprocesses to a direct `__pc` assignment, so the two are not comparable here and are not presented as independent paths; whether a resume PC must be a *signed* pointer is owed to a ptrauth-enabled build |
| delivery latency | faulting instruction to the first statement of the handler, over 3000 faults, with undelivered attempts counted as misses and excluded rather than recorded as a wrapped stale timestamp. Each fault case also carries one cold single observation, named as such, which supports no timing claim |
| concurrent faults | two threads faulting at once on distinct addresses; each thread verifies that the state it saw was its own |
| Mach exception ports | `mach_port_allocate`, `mach_port_insert_right`, `task_get_exception_ports`, and `thread_set_exception_ports` return codes, plus how many task-level `EXC_BAD_ACCESS` handlers are already installed. API availability only: no exception is delivered through the port, and the port is installed on a scratch thread that takes no fault and is removed immediately |

The handler resumes **only** a fault the faulting thread armed, at a PC inside
the JIT region; an unarmed fault exits with status 96 and a fault outside the
region with status 95. Without that guard any stray fault anywhere in the
process was advanced past and reported as a successful resume.

## Build and run

    export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw
    ./run.sh

`run.sh` refuses to run unless `NATEMU_RAW_ROOT` is set, is an existing
directory, and lies outside this repository, and builds into the run directory
so nothing is written under `experiments/`.

## Negative control

`./probe --negative` makes the handler write a value one less than the declared
sentinel into the resumed register: control returns but the requested state
does not, which is not a restart. `src/check.py` must report `FAIL` on that
output and `PASS` on the normal output. An absent platform capability is a
`NOTE`, not a `FAIL`.
