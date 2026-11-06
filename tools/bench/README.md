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
- `attribution/` (S2g): word classes per benchmark, native class-removed variants and array-size sweeps (see the last section).
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
| mem4096 | 0.993 | 9.7 | 0.398 | 2.50 | one linked block; 256 pages per sweep (see below) |
| call | 0.156 | 1.6 | 0.744 | 0.21 | linked call edge; `ret` through the fast dispatcher (one L1 hit per iteration) |
| branchy | 0.410 | 0.5 | 0.597 | 0.69 | six blocks, all edges linked |
| mixed | 0.080 | 0.9 | 0.283 | 0.28 | one linked block |

Guest RAM grew from 1 to 4 MiB for this (the 1 MiB array of mem64/mem4096 did not fit). S2g re-measured and attributed these
numbers (next section); its mem4096 findings supersede the explanation that stood here: the TLB-reach reading is not supported.

## Measurements and attribution (S2g, October 27, 2026)

Measurement and attribution only; nothing was tuned, and the §5 optimisation list of the slice-2 contract stays deferred.

**Setup.** M4 (4 P + 6 E cores), macOS 26.x, AC power, no thermal/performance warning recorded, series9 `fexc`, default block size.
The commands were started from the agent's shell, so they ran at the default QoS (no command-line way to raise it to user-interactive;
`taskpolicy` only clamps downwards) and macOS does not pin cores. The machine was not idle: other agents ran short jobs, load
averages 1.5-2.7, CPU 83-96% idle at the start/end snapshots (a `top` sample at the start of the first natemu run read 83% idle;
all others 94-96%; `host_start/host_end` in each `results.json`). Order, back to back, 7 rounds plus a discarded warm-up each:
natemu (19:52), QEMU system TCG (19:55), natemu (19:56), QEMU (19:58). The linux-user column was not run (`--no-linux-user`).
`results.json`, logs and the sweeps below are in `natemu-raw/s2g/`.

**Steady state against QEMU system TCG** (mean of the two back-to-back pairs; each pair's own ratio was within 0.01 of it;
spread = (max-min)/median of the 7 rounds, pair 1 / pair 2):

| bench | natemu ns/insn | spread % | QEMU ns/insn | spread % | ratio | natemu ns/iter | natemu MIPS | target |
|---|---|---|---|---|---|---|---|---|
| int | 0.191 | 1.6 / 2.0 | 0.196 | 3.3 / 1.3 | 0.97 | 1.91 | 5,240 | <= 1.0: met |
| mem64 | 0.0995 | 18.1 / 6.3 | 0.342 | 3.6 / 1.2 | 0.29 | 0.80 | 10,050 | <= 0.7: met |
| mem4096 | 1.013 | 16.8 / 14.7 | 0.391 | 3.0 / 2.4 | 2.59 | 8.10 | 990 | none |
| call | 0.157 | 3.3 / 2.2 | 0.759 | 14.8 / 6.0 | 0.21 | 1.72 | 6,380 | none |
| branchy | 0.411 | 1.6 / 7.7 | 0.585 | 1.7 / 2.0 | 0.70 | 4.82 | 2,440 | none |
| mixed | 0.080 | 2.7 / 1.9 | 0.280 | 3.4 / 1.3 | 0.29 | 1.04 | 12,460 | none |

The mem64, mem4096 and one branchy spread exceed the 5% asked for in the QEMU baseline chunk (single slow rounds on a shared host);
the medians of the two pairs agree within 2% for every benchmark. Counters at N
and 2N were identical in every run (blocks, links, linked, compiles; only `fast` grows with N for `call`: one dispatcher hit per iteration).

**Executed native words per iteration in the linked steady state** (counted from the series9 objects as the VM links them; the
path of `branchy` is weighted by the model's branch outcomes; `call` includes the callee and the block after the call):

| bench | guest insns/iter | native words/iter | per guest insn | FEX body (default profile) | slice-1 body extras | address guards | split checks | divide checks | link exits | prologue (header 2 + stop check 1) | dispatcher |
|---|---|---|---|---|---|---|---|---|---|---|---|
| int | 10 | 22 | 2.2 | 16 | 2 | 0 | 0 | 0 | 1 | 3 | 0 |
| mem64, mem4096 | 8 | 28 | 3.5 | 12 | 3 | 6 | 3 | 0 | 1 | 3 | 0 |
| mixed | 13 | 40 | 3.1 | 18 | 6 | 9 | 3 | 0 | 1 | 3 | 0 |
| call | 11 | 54 + 12 = 66 | 6.0 | n/a | n/a | 12 | 6 | 0 | 2 | 9 | 15 (ret: 3 in the block, 12 in `dispatch_fast`) |
| branchy | 11.75 | 33.2 | 2.8 | 18.2 | 1.0 | 0 | 0 | 0 | 3.5 | 10.5 | 0 |

"FEX body" is the word count of the same block compiled by the default profile (`--no-tso --no-end-marker`, no checks), counted up
to its first branch; "slice-1 extras" is the executed body minus that (for `int`, the two `mov x5, x4` before the shifts; the causes of
the others were not analysed word by word). For `call` the default profile
cuts blocks differently, so no split is given. None of the six benchmarks contains a divide, so divide checks are 0 words
(they are exercised by the fault corpus, not measured here). QEMU's words (`-d out_asm`) were not counted in this pass.

**Class-removed variants run natively outside the VM** (`attribution/build_native.py`): the linked blocks, patched exactly as the resident
links them (the branch to the entry plus 4), are executed by a small harness with the frame (x28) and the dispatcher replicated; a
class is removed by turning its words into `nop` (`noguard`, `nosplit`, `nochecks` = both, `nostop`, `noprologue` = header and stop
words, `skip3` = links jump past the three prologue words, `nodisp` = the `ret` jumps straight to its one return site), or `fexonly`
(the default-profile block with its in-block loop). Median over 8 pooled passes (two sweeps of 3 and 5 passes of 7 rounds,
N and 2N slope; the second ran under more host load, so the base moved by up to 5%), delta against `base` in ns per iteration
(negative = faster without the class). Deltas are not additive and below about 0.05 ns (5% of an `int` iteration) are noise:

| bench | native base | VM (natemu) | noguard | nosplit | nochecks | nostop | noprologue | skip3 | nodisp | fexonly |
|---|---|---|---|---|---|---|---|---|---|---|
| int | 1.99 | 1.91 | +0.04 | +0.03 | +0.03 | -0.02 | -0.06 | -0.02 | - | -0.10 |
| mem64 | 0.79 | 0.80 | -0.05 | -0.05 | -0.05 | +0.00 | +0.02 | +0.01 | - | -0.13 |
| mixed | 1.02 | 1.04 | -0.01 | -0.01 | -0.01 | +0.01 | -0.01 | +0.01 | - | -0.03 |
| call | 1.87 | 1.72 | -0.11 | -0.12 | -0.14 | -0.15 | -0.14 | -0.10 | -0.38 | - |
| branchy | 5.07 | 4.82 | - | - | - | +0.15 | +0.09 | +0.09 | - | - |
| mem4096 | 4.55 | 8.10 | -0.04 | +0.15 | +0.02 | 0.00 | -0.85 | -0.32 | - | -0.91 |

Reading (what the data supports, no more):

- **int** (0.97x): the same hot block runs in the same 1.9 ns natively and in the VM, and removing any class changes it by at most 5%, within noise.
  The iteration is a serial chain (add, shift, xor, shift, xor, multiply: about 8 cycles at 4 GHz = 1.9 ns), so 22 words cost nothing
  on a wide core (inferred from the dependency chain; no PMU); neither natemu nor QEMU can go much below the chain, which is why the ratio sits near 1.
- **mem64 / mixed** (0.29x): the VM equals native. Guards and split checks cost about 0.05 ns of 0.8 ns on mem64 (6% each, 6% together)
  and nothing measurable on mixed; the whole slice-1 code against the default FEX block is 16% (mem64) and 3% (mixed).
- **call** (0.21x): the dispatcher for `ret` (L1 hit, 15 words) costs 0.38 ns of 1.87 ns per iteration (20%), the largest single class;
  the checks (6%-8% each) and the prologue are the rest of the removable cost. One L1 hit per iteration, never a miss. (The harness's base
  read 1.73 ns in the quiet sweep, equal to the VM's 1.72; 1.87 is the pooled median including the loaded sweep.)
- **branchy** (0.70x): 4.8 ns per iteration natively and in the VM, unchanged by removing the prologue; consistent with the two
  data-dependent branches mispredicting about once per iteration (inferred from the model's branch outcomes; no PMU), not with
  anything natemu adds.
- **Everything but mem4096 equals the same blocks run natively**: once the loop is linked, nothing measurable of the VM (tables, EL1,
  watchdog, host) is on the hot path.

**mem4096.** Natively (no VM) the same blocks take 4.4-5.0 ns/iter and QEMU 3.1 ns, but natemu 8.1 ns. A sweep of the array size
(power of two, pages of 4 KiB, wrap mask patched into the benchmark) at two strides isolates the cause (`attribution/vmvar.py` in
the VM, `build_native.py` with `PAGES`/`STRIDE` natively; ns per iteration, median):

| 4 KiB pages touched | 4 | 16 | 32 | 64 | 128 | 256 |
|---|---|---|---|---|---|---|
| native, stride 4096 | 0.77 | 0.76 | 2.11 | 3.87 | 4.68 | 5.03 |
| native, stride 4160 | 0.76 | 0.76 | 0.77 | 0.89 | 0.89 | 0.91 |
| natemu, stride 4096 | 0.75 | 0.75 | 0.92 | 5.33 | 7.06 | 8.75 (7.3 in two earlier runs) |
| natemu, stride 4160 | 0.77 | 0.77 | 0.73 | 0.89 | 0.86 | 1.07 |

Stride 4160 (4096 + one cache line) touches the same number of distinct pages, so the same stage-1 TLB footprint, but is fast at every size.
The slowdown follows the power-of-two stride (all accesses share the low 12 address bits: with an M4 P-core L1D of 128 KiB, 8-way,
64-byte lines, assumed and not verified here, they use only 4 sets = 32 lines, and in-flight loads and stores alias on their low address bits),
appears natively at the same size (the step at 32-64 pages = 32-64 lines), and is independent of the VM. Natively the stride is the whole of the 4.4-5.0 ns.
The TLB-reach hypothesis (4 KiB guest pages against 16 KiB host pages) is therefore **not supported as the cause**: with a cache-friendly stride
the 256-page footprint costs only +0.15 to +0.3 ns/iter in the VM (1.07 against 0.73-0.77), about 2-4% of the 8.1 ns. What is not explained is the
extra 2.3-3.7 ns/iter the VM adds on top of native at stride 4096 (7.3-8.8 against 5.0 at 256 pages), and why QEMU, which has the
same host-address aliasing, pays only 0.4 ns/iter more than for mem64. A TLB miss that compounds the already serialised L1 misses is a
candidate for the first; a PMU (needs root: `kpc`) would settle it; unproven. The 3 prologue words (header and stop-check stores to the
frame) cost 0.3-0.9 ns/iter of the native 4.4-5.0 only in this one benchmark (the stores contend with the miss-bound data stream; mechanism
unproven).

**Targets.** int 0.97x (<= 1.0) and the memory loop mem64 0.29x (<= 0.7): both reported as met on these numbers; the less
favourable loop for natemu, mem4096, is 2.59x. Reproduce: `tools/bench/attribution/` (`words.py` classes, `sweep.py` class-removed variants,
`vmvar.py` array-size sweeps; need `NATEMU_RAW_ROOT`, `cc`, `clang`).

**Caveats.** Native deltas come from patched images that are checked only for their iteration count, not their results; the
dispatcher in the harness is `dispatch_fast` with its two literal loads replaced by frame loads. The two native sweeps ran under
different host load and CPU frequency (the harness's calibration read 2.9-3.9 GHz), so deltas under 0.05 ns/iter (about 5% of `int`,
1% of `mem64`) are not resolved. Word counts are per iteration at the linked steady state, not per guest instruction retired. The spread
targets (5%) were missed for mem64, mem4096 and one branchy pair. Compile cost, QEMU word counts, the linux-user column and PMU cycles were
not measured.

## Boot benchmarks (`run.py`, `boot.py`)

The Linux boot harnesses of the performance phase, brought into the repository on November 5 with the stub's named
command line (from `natemu-raw/bench-20261105-03/scripts/bench.py` and `natemu-raw/qcmp-20261103-01/boot.py`; their
measurement, verification and record fields are unchanged; natemu now runs as the `natemu` stub, not `natemu-host
boot-linux`, which had the same 60 s watchdog and default code cache). Kernel and initramfs are arguments; nothing defaults
to natemu-raw.

    python3 tools/bench/run.py --out DIR --name NAME --emu natemu --vcpus 1 --bench zstd --kernel BZIMAGE --initrd bench.cpio \
        --fexc FEXC [--compile-cache off]
    python3 tools/bench/boot.py --out DIR --name NAME --emu natemu --vcpus 1 --kernel BZIMAGE --initrd INSTALLER_INITRD \
        --fexc FEXC --cache off

`run.py`: one boot of the bench initramfs (built by natemu-raw/bench-20261105-03/scripts/build.sh), the benchmark run twice
in the guest, each timed START to END on the serial console (`wall_s`, `wall2_s`; the guest's own `guest_s`, `guest2_s`;
per phase `phases`, `phases2`) and verified (`verified1`, `verified2`); a line per boot in DIR/bench.jsonl. `boot.py`: the
Debian boot timed from process start to its end-point line (the installer's "Select a language", or the root-fs panic
without an initrd), `wall_s`, a line per boot in DIR/boots.jsonl. Both record `contended` (foreign VM processes seen every
0.5 s through `vms.sh`, whose own command line names no VM binary) and exit 0 good, 1 not reached or not verified, 3 good
but contended. QEMU (`--emu qemu-multi|qemu-single`) uses qcmp-20261103-01's board and CPU model. Run each boot under the
VM lock (`/usr/bin/lockf -k .../natemu-raw/.vm.lock`) with no other VM running.

The scripts kept as evidence in natemu-raw (these two originals, their campaign drivers and the other campaigns' scripts)
use the older command lines: the stub's positional `natemu RUNTIME FEXC BZIMAGE [OPTIONS]`, which the stub no longer
accepts after November 5, and `natemu-host RUNTIME boot-linux FEXC BZIMAGE`, which still works. Leave them as they are; use
these copies for new measurements.

Regression check of the clap change (November 5, M4, cache off, one VM at a time under the lock, none contended; records in
`natemu-raw/clap-compare-20261105-01`): the stub at 71feccb (positional, through a shell shim that `exec`s it) against the
clap stub, the same resident and fexc (series65-p2). Startup, 20 interleaved runs each, median: `--help` 2.64 ms old (a
usage refusal) vs 2.70 ms new; an immediate exit (missing kernel) 2.65 vs 2.67 ms. Three interleaved rounds, median
(range): installer N = 1 to "Select a language" 5.343 s (5.312-5.398) old vs 5.215 s (5.169-5.295) new; `zstd` N = 1 of
bench-20261105-03's initramfs, run 1 `wall_s` 3.064 (3.037-3.087) vs 3.055 (2.997-3.075), run 2 `wall2_s` 2.884
(2.840-2.907) vs 2.920 (2.851-3.787; the 3.787 run's own `guest2_s` is 3.76), every run verified. No slowdown. The
installer's ranges do not overlap, the new stub 0.13 s (2.4%) faster; not explained (the shim's `sh` costs milliseconds).
