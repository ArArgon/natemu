# INV-01-06 M06-M1 — delivered-Mach state-only exception baseline

Method record: `docs/research/investigations/INV-01-06.md`, "Delivered-Mach
supplement M06-M1" (lines 524-814). Self method review `REV-186`, independent
method acceptance `REV-188`. This directory is the code deliverable that
method acceptance authorized; it is a **new** experiment beneath
`experiments/INV-01-06/`, separate from the accepted signal probe in
`experiments/INV-01-06/{src,replication}` and `run.sh`, which this work does
not modify.

## Execution boundary

**No code here has been run against a real fault.** `build.py` compiles this
experiment's C source against MIG-generated code, verifies the generated
routine id, freezes the wire layout, and disassembles the result — it never
executes the produced `state_server` binary. `run.py`'s `run` action, which
would execute it, refuses before doing so unless **both** an accepted self
code review (`REV-228`) and an accepted **independent** code review
(`REV-229`, reserved, not yet written) bind the exact source-packet digest —
per `INV-01-06.md:806-814` ("no runtime preflight/evidence before independent
code acceptance") and this experiment's own authoring boundary. `python3
run.py plan` is pure Python (coverage enumeration only) and is the only
action safe to run before REV-229 exists.

## What it measures

A state-only Mach exception server (`EXCEPTION_STATE | MACH_EXCEPTION_CODES`,
`ARM_THREAD_STATE64`, MIG routine 2406 `mach_exception_raise_state`) that
repairs one worker's dedicated `PROT_NONE` data page and replies with an
exact, unchanged copy of the delivered general state, so the faulting
load/store retries exactly once and the worker resumes with the right data
and untouched sentinels/SP. Everything (workers, servers, the owned prior
handler) runs as threads inside one fresh-exec'd process — an own-process
exception-port installation, never a foreign task or elevated entitlement.
See `INV-01-06.md:539-544,600-663` for the exact scope and boundary; this is
CLM-01-06-22/-23, a Hypothesis until code and result gates pass.

## Layout

| File | Role |
| --- | --- |
| `src/state_server.c` | worker + server harness; the only hand-written native source |
| `build.py` | MIG generation, routine-id verification, wire-layout freeze, compile, disassemble — build only, never executes |
| `coverage.py` | pure-Python coverage manifest (signing × n × jit × access × topology × path, plus the 11 auxiliary arms) |
| `check.py` | pure-Python two-pass checker (wire pass + semantic pass) with its own negative-fixture self-test |
| `run.py` | supervisor: `plan` (pure Python) and `run` (gated; refuses without independent REV-229) |
| `wire-layout.json` | build.py output: frozen request/reply byte layout (not checked in; regenerate via `build.py`) |

## Build

    export NATEMU_RAW_ROOT=/Users/liaozping/orca/natemu-raw   # external, outside this repo
    python3 build.py --output "$NATEMU_RAW_ROOT/INV-01-06/m1/<stamp>"

`build.py` refuses to run without `NATEMU_RAW_ROOT` set to an existing
directory outside the repository (same refusal shape as the sibling
`experiments/INV-01-06/run.sh`), and refuses if the pinned SDK's
`mach/mach_exc.defs` hash no longer matches the accepted `SRC-162` value
(`3ddbd5d01091643dab842d6b153fe3ae0de65498f28fb4afc51dd3c6d6637b4d`). It
writes generated MIG sources, `wire-layout.json`, `verify_layout.c` (a
`-fsyntax-only` translation unit whose `_Static_assert`s cross-check every
frozen offset/size against the real generated headers), `disassembly.txt`
(the fault-site `asm_load`/`asm_store` and the repair/reply routine), and
`build-manifest.json` (file hashes, pinned MIG/compiler versions, the
verified routine id, and `runtime_executed: false`).

## Plan / coverage

    python3 run.py plan

Prints the full coverage manifest and the exact source-packet digest as
canonical JSON. See `coverage.py` for the cross-product and sample plan:
96 eligible positive cells (32 combinations requiring immutable-JIT on the
non-hardened signing cell are recorded `invalid`, never dropped), 3 fresh
children per eligible cell at seeds 60601-60603 with 10 warm-up + 100 checked
events per worker, and 11 auxiliary arms (8 runtime negatives + terminal +
restoration + prior-handler-unsupported) at 3 fresh children each.

## Negative control

`check.py`'s self-test (`python3 check.py`, no arguments) is the runnable
check this experiment leaves behind. It builds one minimal valid trace,
confirms the two-pass checker reports it clean, then applies five named
mutations plus two structural ones and confirms each is independently
rejected with a distinct reason: wrong routine id, a reply carrying changed
general state, a double outstanding fault (two arms with no resume between),
an unreleased right, a duplicated disposal, a restoration mismatch, and a
receive with no reply or terminal record (a MIG_NO_REPLY-shaped omission).
None of this launches `state_server`; the fixtures are hand-built JSONL rows
matching the trace schema documented at the top of `check.py`.

## Disposal and reference accounting

No success path in `catch_mach_exception_raise_state` (or its two sibling
routines) ever returns `MIG_NO_REPLY`: every receive is answered by a real
reply (a repair success, a forwarded reply, or `KERN_FAILURE` for a rejected
event) or an explicit terminal record — `check.py`'s
`check_no_mig_no_reply` enforces this on every trace. Every right this
harness acquires is disposed exactly once, after restoration is confirmed
(`teardown_worker` in `src/state_server.c`):

| Right | Acquired | Disposed | How |
| --- | --- | --- | --- |
| Worker receive right (`recv_port`) | `mach_port_allocate(..., RECEIVE, ...)` in `register_worker` | `teardown_worker` | `mach_port_mod_refs(..., RECEIVE, -1)` |
| Worker send right (same name) | `mach_port_insert_right(..., MAKE_SEND)` in `register_worker` | `teardown_worker` | `mach_port_mod_refs(..., SEND, -1)` |
| Worker thread control port (`thread_self`) | `mach_thread_self()` in `register_worker` | `teardown_worker` | `mach_port_deallocate` |
| Prior receive right (`prior_recv_port`, forward path only) | `mach_port_allocate` in `register_worker` | `teardown_worker` | `mach_port_mod_refs(..., RECEIVE, -1)` |
| Prior send right (forward path only) | `mach_port_insert_right` in `register_worker` | `teardown_worker` | `mach_port_mod_refs(..., SEND, -1)` |
| Port set (serial topology only) | `mach_port_allocate(..., PORT_SET, ...)` in `main` | end of `main`, after every worker's teardown | `mach_port_mod_refs(..., PORT_SET, -1)` |
| Dedicated data page | `mmap` in `worker_thread` | `teardown_worker` | `munmap` |
| Pre-existing exception-port entries (snapshot) | never acquired — inspected only via `thread_get_exception_ports` | n/a: not owned by this experiment | `thread_set_exception_ports` restores the exact saved tuple; the saved `mach_port_t` values are not separately deallocated because they were never given an extra reference by the snapshot call |

`check.py`'s `check_disposal` fails a trace missing any of the first five
rows' disposal record for a worker that reached `register`, and separately
fails a right disposed more than once. The port set and data page are
recorded for completeness but are not Mach send/receive rights, so they are
not part of that automated check (see `check.py`'s `EXPECTED_DISPOSALS`).

## Baselines: non-JIT then immutable-JIT

The non-JIT baseline (`asm_load`/`asm_store` in `src/state_server.c`) is two
plain functions compiled into this binary's own `__TEXT` segment — never
`MAP_JIT`, never written after link. The immutable-JIT baseline
(`build_immutable_jit`) uses the exact accepted Q-01-04 procedure already in
use by `experiments/INV-01-06/src/probe.c` and `experiments/INV-01-04/src/
probe.c`: allocate `MAP_JIT`, `pthread_jit_write_protect_np(0)`, write the
generated bytes, `sys_icache_invalidate`, `pthread_jit_write_protect_np(1)`.
"Immutable" here means that lock is never reopened for the rest of the
process's life — a write-window toggle mid-campaign is INV-01-19's distinct
composition question, explicitly out of scope (`INV-01-06.md:594-598,648-649`).

## Known limitation: routine ids entirely outside this subsystem

`serve_n` uses `mach_msg_server_once(mach_exc_server, ...)`, the standard MIG
receive/dispatch/reply loop, rather than hand-rolling message receive. For a
message whose routine falls inside subsystem 2405-2410 but is not 2406
(`mach_exception_raise`/`mach_exception_raise_state_identity`), our own
`catch_mach_exception_raise`/`catch_mach_exception_raise_state_identity`
stubs run and log a real `reply` record with `role: "reject"` — this is the
traced, checked path for a same-subsystem wrong-routine-id event. For a
`msgh_id` entirely outside that range, `mach_exc_server` itself returns
`FALSE` before calling any `catch_` function, and `mach_msg_server_once`
answers it with the standard `MIG_BAD_ID` error reply on its own — correctly
(no `MIG_NO_REPLY`, no hang), but without one of this experiment's own
`jlog` trace records. This is a disclosed observability gap in the trace,
not a functional defect: the reply is still always sent, and it always
carries an error, never a success. Re-implementing MIG's receive loop by
hand to add a trace line for that specific edge case was judged a worse
trade than reusing the standard, already-correct implementation; an
independent reviewer may reasonably ask for it before REV-229.

## Method question this author could not resolve without deciding the method

INV-01-06.md does not name a specific list of local signing cells beyond "ad
hoc non-Hardened Runtime" and "ad hoc Hardened Runtime+allow-jit"
(`INV-01-06.md:719-723`). `coverage.py` names these `adhoc-plain` and
`adhoc-hardened-allow-jit` and treats `jit=1` on `adhoc-plain` as an eligible
but `invalid` cell rather than omitting it, so no coverage cell is silently
dropped; that naming is this author's own choice, not a method-text quote,
and an independent code reviewer or the method author should confirm it
matches what M06-M1 intended before REV-229.
