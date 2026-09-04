# INV-01-06 — exception delivery and restartability probe

Measures whether a fault raised inside generated code can be caught with full
register state and the thread resumed at a chosen PC, for `Q-01-06` and kill
assumption `KA-04`.

- Method record: `docs/research/investigations/INV-01-06.md`
- Breadth pass: `docs/research/breadth-passes/BP-01-06-start.md`

## What it does

| Probe | Observable |
| --- | --- |
| state structs | `__DARWIN_OPAQUE_ARM_THREAD_STATE64`, `__has_feature(ptrauth_calls)`, and which `_STRUCT_ARM_EXCEPTION_STATE64` variant the build receives (`__esr` width) |
| fault and resume | arm64 stubs in a `MAP_JIT` region that fault on an unmapped load, an undefined instruction, and `brk #0`. The handler reads `__pc`, `__sp`, `__x[]`, `__es.__esr`, `__es.__far`, then skips the faulting instruction and writes a declared sentinel into `x0`; the caller checks the returned value |
| resume path | the same unmapped-load case resumed twice, once by assigning `__pc` directly and once through `__darwin_arm_thread_state64_set_pc_fptr`, so the record shows whether a resume PC must be a signed pointer on this host |
| delivery latency | faulting instruction to the first statement of the handler, over 3000 faults |
| concurrent faults | two threads faulting at once on distinct addresses; each thread verifies that the state it saw was its own |
| Mach exception ports | `mach_port_allocate`, `mach_port_insert_right`, `task_get_exception_ports`, and `thread_set_exception_ports` return codes, plus how many task-level `EXC_BAD_ACCESS` handlers are already installed. API availability only: no exception is delivered through the port, and the port is installed on a scratch thread that takes no fault and is removed immediately |

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
