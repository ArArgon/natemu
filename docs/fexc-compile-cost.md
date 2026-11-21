# fexc compile cost

What one fexc compile costs, where the time goes, and the per-request waste removed in November 2026. Measured on an
M2 Max with a Paper (Minecraft server) start under openjdk 25: about 507,000 requests over boot plus one start, almost all
small blocks of JIT-compiled Java (median 34 guest bytes; 9 of 20,000 Paper-phase blocks have any floating-point op).

## How to measure without a VM

1. Record the requests of a real run: `NATEMU_FEXC_RECORD=<file>` makes the host append every request it sends to fexc
   (u32 length + body, `crates/host/src/execution.rs`).
2. Replay them straight into fexc: `fexc --serve < <file> > /dev/null`. The stdin form of `--serve` reads exactly that
   stream. A full replay takes seconds, so profiles (`sample <pid>`) and A/B timings repeat cheaply.
3. Compare two compilers on the same stream reply by reply: code, JSON, object and extent must be identical; `args_read`
   may only lose bits (see below). `channel_replay_matches_the_pipe` in `crates/host/src/execution.rs` is the host's own
   form of this gate (`NATEMU_REPLAY`, `NATEMU_REPLAY_OLD`, `FEXC`).

Replay over stdin adds about 12% of read and write system calls that the host's shared-memory channel does not have;
the shares below leave them out.

## Where the time went (before)

About 2 µs per request plus 1.6 µs per guest instruction; a Paper block averaged 9.6 instructions (extended blocks:
up to four conditional branches per unit), 17 µs a request. Of that:

| part | share |
|---|---|
| natemu precision passes (floating-point checks, flushes, FMA, upper-zero) | 17% |
| FEX glue in the compile driver | 12% |
| natemu per-request setup, reset, capture and reply | 11.5% |
| natemu code-generator hooks (access sites) | 11.5% |
| FEX code generation | 11% |
| FEX register allocation | 10% |
| FEX IR building | 9% |
| FEX flag elimination | 8% |
| FEX decoder | 6.5% |
| natemu block cutting | 3.5% |

Memory allocation, spread over these, was about a fifth. The JSON reply is negligible (0.4%): `--serve` sends no report.
16% of requests were compiled twice: the first, extended attempt had more literal exits than the resident's four link
points and was thrown away after code generation. Those discarded attempts were 26% of the compile loop.

## What changed

- **Precision passes skip integer-only IR.** `AnyFpWork` (`tools/fexc/shim/fexc_precision.h`): without a
  floating-point op (`SimdFPOp`) or an x87 instruction, each of the five passes would change nothing, so they return
  after their per-compile clears. The answer is scanned once per compile.
- **An attempt that will be redone stops before the backend.** `Attempt` (`tools/fexc/fexc.cpp`) runs FEX's
  `GenerateIR`, counts the literal exits in the final IR (`LiteralExits`: one `svc #FEXC_SVC_LINK` per ExitFunction to a
  constant and per LOCK NEG), and skips code generation and the capture when the extended-block loop would retry anyway.
  An attempt that reaches the backend is still checked on its code words, as before.
- **Per-request state is reused, not freed.** The per-request lookup tables (`fexc_hook.h`) take their nodes from one
  arena that `ResetGlobals` releases after clearing them; the capture keeps its vectors' storage (`Capture::Clear`).
- **Over-long units stop before FEX's passes** (FEX fork, `GenerateIR`): fexc's `fexc_ext_abandon` hook sees the
  dispatcher's IR, and a unit the loop would compile again returns no IR there, before the x87, flag-elimination and
  precision passes and register allocation. No pass makes or removes an exit, so the count is the final one (checked equal
  on every recorded request); `Attempt` still checks the final IR and the code words.
- **FEX's pools keep their buffers in fexc** (FEX fork, `PoolBufferKeepClaimed`): fexc compiles on one thread all the
  time, so `DelayedDisownBuffer` no longer reads the clock to decide whether to give a buffer back.
- **The access-site hook tests op kinds by number.** `FexcRecordOp` looked each IR op's name up in two lists of names
  (39 string compares per op); a table by op number built once from the same lists replaces it.

Result on the recorded Paper requests: 17.2 → 12.0 µs a request (30% less replay time). Inside a Paper start (profiled,
empty compile cache) a fresh compile went from 26.1 to 19.4 µs, the fexc time of all four vCPUs from 11.25 to 8.35 s;
the start itself moved by about a second (38.7 → 37.5 s, within run-to-run noise): compiling is about 6% of vCPU time
there. Every reply is identical but for `args_read`.

### `args_read`

A reply's `args_read` names the gating request arguments the compile read; the host's compile cache keys on those only,
since an unread argument cannot change the compile. Skipped work no longer reads some: an integer-only block no longer
reads `--accurate` (nor `--daz`, `--ftz`, `--simd-unmasked` where those were read only by the precision passes), and a
discarded attempt's code generation no longer adds its reads. Such blocks can now be shared between floating-point
modes in the cache.

## Left as it is

- Discarded attempts still pay for decoding and IR building. Avoiding that means deciding the cut from the decoded
  instructions.
- FEX's control-flow graph, debug data, code buffer and decoder buffers allocate per compile (about 7% of samples).
- What remains is mostly FEX's own decoder, IR builder, passes, register allocator and code generator.
- Compile-ahead (compiling a block's exits before they run) was prototyped on November 4 and not shipped (no gain: the
  extra compiles slowed the vCPUs' own). On this Paper start 79% of fresh compiles were a known exit of an earlier block,
  but the median lead was 13 µs, under one compile; 35% had 20 µs or more. Its ceiling here is under a second.
