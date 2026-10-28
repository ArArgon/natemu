# QEMU TCG baseline (slice 2 yardstick)

Steady-state ns per guest instruction of six small x86-64 loops under **`qemu-system-x86_64` TCG running natively on the Mac**
(binding yardstick) and, reported only, **`qemu-x86_64` linux-user** in the pinned container. natemu runs the identical bytes later.

    python3 tools/bench/build_bench.py                    # (re)assemble bodies -> benchmarks.json   (needs nasm)
    python3 tools/bench/run_qemu.py --out DIR [--reps 7] [--scale 1.0] [--bench int,mem64] [--no-linux-user] [--no-system]

Needs: `brew install qemu` (pinned in `DIR/manifest.json`: version, binary SHA-256, dependencies), `nasm`, `cc`, Docker (linux-user
column; image and QEMU hash are the ones `tools/reference/ref.py` pins). Results go to `DIR/results.json`; the run prints a table.

## Files

- `bodies/*.asm`, `benchmarks.json`: the benchmarks (bytes are authoritative; position independent; fall through at the end).
- `stub.asm`: bare-metal boot stub. `model.c`: independent native model (final GPRs + exact dynamic instruction count).
- `run_qemu.py` (harness), `lu_entry.py` (timing loop inside the container), `build_bench.py` (assemble, json, helpers).

## Benchmarks

N is the iteration count, held in `rcx`; loop ends with `dec rcx; jnz`. Instruction counts are exact (model, cross-checked
against QEMU linux-user `-d exec -singlestep`, one TB per instruction).

| name | what | insns/iter |
|---|---|---|
| int | add, mov, shl, xor, mov, shr, xor, imul + dec/jnz; one loop-carried chain (xorshift + multiply) | 10 |
| mem64, mem4096 | load-add-store over a 1 MiB array at 0x200000, stride 64 / 4096, wrapping, plus a running checksum in r8 | 8 |
| call | `call` leaf (`push rbx; lea; add; mov; pop rbx; ret`), checksum of results (+1 initial `jmp`) | 11 |
| branchy | LCG; two data-dependent jcc per iteration (`test edx,1`/`jz`, `test edx,6`/`jnz`) | 9..12 (avg 11.75, exact from model) |
| mixed | checksum over a 64 KiB LCG-filled buffer at 0x110000: qword load, byte `movzx`, `imul`, shifts, store back | 13 |

Subset: 64-bit integer, ordinary load/store, `jcc/jmp/call/ret`, `movzx`; no SSE, string ops or locked ops. Layout: code at
0x100000 (its own page, no stores to it), data 0x200000 (1 MiB), buffer 0x110000, stack 0x300000 (8 KiB, rsp = 0x301800).

## Reusable description (for natemu)

`benchmarks.json` entries are `tools/reference` programs: `{name, code (hex), rip, gpr[16], rflags, memory, insns}` with rcx (gpr[1])
as the N placeholder. Memory regions use `{"addr","size","fill":"zero"|"lcg","seed"}` instead of inline data (a 1 MiB zero region
would be 2 MB of JSON); `build_bench.program(bench, n)` expands to the exact `{"addr","data"}` schema, and `build_bench.model(...)` gives the
expected final GPRs and instruction count. `insns` is informational only (static list); `ref.py`'s annotation cross-check does not apply
to loops. `results.json` carries `expected` (final GPRs and dynamic instruction count) for every N that was run.
LCG fill: `x = x*6364136223846793005 + 1442695040888963407 (mod 2^64)`, byte = `x >> 56`, starting from the seed.

## System-TCG setup

`qemu-system-x86_64 -accel tcg,thread=single -cpu max -smp 1 -m 64 -machine pc -nographic -nodefaults -monitor none -serial none
-device isa-debug-exit,iobase=0xf4,iosize=0x04 -no-reboot -debugcon file:OUT -kernel IMAGE` (no KVM/HVF).

The stub is a Multiboot-1 flat binary (a.out kludge, no linker; SeaBIOS loads it): 32-bit entry, PAE, EFER.LME, CR3 =
PML4 -> PDPT -> PD -> two PTs, **4 KiB pages identity-mapping 0..4 MiB** (present/rw/user, accessed+dirty preset so the walk never
writes), far jump to a 64-bit ring-0 segment, PIC masked, then `iretq` to **CPL3** (user code/data segments, IOPL=3 so the guest can
`out`) with the benchmark's initial GPRs. IF=0 throughout, no IDT, no TSS, no interrupts. After the body (same bytes as the
linux-user ELF) a capture sequence stores the 16 GPRs, writes them to the `isa-debugcon` port 0xe9 (no flow control, so no UART
timing) and writes 0xf4 (`isa-debug-exit`, process exit code 1). The harness reads the 128 bytes and compares them to the model.

## Method

Per benchmark the image runs at N, 2N, 4N (`n_default` x `--scale`, sized so 4N takes about 1-2 s), the three interleaved across
all benchmarks and repeated `--reps` times after one discarded warm-up round. Wall time is `perf_counter` around the process
(system) or around the QEMU process inside the container (linux-user, so docker start/attach is excluded). Per repetition the slope of
time against exact dynamic instruction count over the three points is the ns/instruction (startup falls into the intercept,
reported as `startup_s`); ns/iteration is the same fit against N. Reported: median over repetitions, spread as (max-min)/median
(`range_pct`) and CV. Pairwise (2N-N, 4N-2N) slopes are kept in `results.json` as a linearity check. Every run's final GPRs are
compared with the model: a mismatch aborts, so a broken benchmark cannot look fast. Host load (uptime, CPU usage, top processes,
thermal) is recorded at start and end.

## What limits fairness

- **Different QEMU versions.** System column is Homebrew 11.1.2; linux-user is Debian's 7.2. The linux-user column is not a version-matched
  control, and it has no softmmu (guest addresses are host addresses plus an offset), so memory-heavy code looks better there than under system TCG.
- **Timed externally.** Wall time includes process launch; the slope removes it only if startup is constant in N (the linearity check and
  intercepts show it is). The linux-user ELF also copies the 1 MiB regions out at the end (constant).
- **Not an idle host.** macOS gives no CPU pinning, and background daemons (notably `fseventsd` at 80-100% of a core during these runs) share the
  machine; the load is recorded in `results.json`. Spread numbers include that.
- **Guest page tables are warm.** Tables are static and A/D preset: no table-walk side effects; the 4 KiB-stride benchmark touches 256 distinct pages
  per sweep and exercises QEMU's softmmu TLB (dynamic size) but not page faults. QEMU's TLB-entry A/D handling is therefore best-case.
- **Flags and memory ordering.** QEMU computes flags lazily, so benchmarks that kill flags quickly (all of these) do not stress its flag
  materialisation. These loops contain no locked or SSE ops, which dominate real software.
- **CPL3 and paging.** Linux-user has no CPL or guest paging at all; system TCG pays for both (TLB fill on miss). Natemu
  pays for its own shadow paging; the comparison is system TCG vs natemu, which is why system TCG is the yardstick.
- **Steady-state only.** QEMU's translation and boot cost (startup intercept is about 40-45 ms system, 5-7 ms linux-user) is excluded; compile cost
  belongs in a separate natemu-side measurement (contract section 4).

## natemu (`run_natemu.py`, S2c measurement)

    tools/with-env python3 tools/bench/run_natemu.py --fexc FEXC --out DIR [--reps 5] [--scale 1.0] [--bench int,mem64] [--max-block N]

The same `benchmarks.json` programs through natemu's program driver (`natemu-host RUNTIME run-program`, the general execution
path: resident code cache, linking, dispatcher), at N and 2N (`n_default` x `--scale`), interleaved, `--reps` rounds after a
discarded warm-up. ns/insn = (t(2N) - t(N)) / (insns(2N) - insns(N)) per round, process wall time; median and range. Every run's
final GPRs must equal `model()` and stop at the program end; N and 2N must compile the same blocks and make the same resident
entries and links (only the dispatcher's L1 hits of computed exits, `ret`, may grow with N). No tuning: measurement only.

October 27, 2026 (S2c, series8, default block size, 5 rounds, M4, not idle; `natemu-raw/s2c/bench/results.json`):

| bench | natemu ns/insn | range % | QEMU system TCG | ratio | per iteration in the steady state |
|---|---|---|---|---|---|
| int | 0.189 | 0.2 | 0.200 | 0.94 | one linked self-loop block |
| mem64 | 0.102 | 12.3 | 0.346 | 0.30 | one linked block; 512 shadow retries at start |
| mem4096 | 0.993 | 9.7 | 0.398 | 2.50 | one linked block; 256 pages per sweep (TLB misses in the VM) |
| call | 0.156 | 1.6 | 0.744 | 0.21 | linked call edge; `ret` through the fast dispatcher (one L1 hit per iteration) |
| branchy | 0.410 | 0.5 | 0.597 | 0.69 | six blocks, all edges linked |
| mixed | 0.080 | 0.9 | 0.283 | 0.28 | one linked block |

Guest RAM grew from 1 to 4 MiB for this (the 1 MiB array of mem64/mem4096 did not fit). Not attributed yet (S2g).
