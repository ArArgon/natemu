# SMP reference (S3r): QEMU system mode, 1-4 CPUs

Reference for slice 3's multi-vCPU work. Programs with 2-4 CPUs run on the pinned native `qemu-system-x86_64` (Homebrew 11.1.2, `-smp N`,
MTTCG `-accel tcg,thread=multi`; `thread=single` where noted) and the final state of every CPU plus shared memory is captured. The same bytes
and per-CPU initial registers (the `natemu-smp-v1` schema below) are what natemu runs later. Independent of natemu: nothing here reads the
emulator or its crates.

    python3 tools/bench/smp/smp.py selftest --out DIR     # known program vs model, then corrupted-dump controls
    python3 tools/bench/smp/smp.py det      --out DIR [--reps 5] [--cpus 2,3,4] [--programs a,b] [--modes multi,single]
    python3 tools/bench/smp/smp.py litmus   --out DIR [--runs 20] [--k 32768] [--tests sb,mp] [--single-runs 3 --single-k 4096]
    python3 tools/bench/smp/smp.py build    [--k 32768]    # rewrite programs.json / litmus.json from programs.py

Needs `nasm`, QEMU, python3. `det` fails (exit 1) if any run differs from the model or if repeated runs give different dumps. `litmus` fails
only on tool errors; allowed/forbidden labelling comes from `tools/models/ordering_model.py` (`enumerate_outcomes`), reported in the results.

## Files

- `stub_smp.asm` - the boot stub (flat Multiboot-1 image, no linker), `smp.py` - image builder, runner, dump parser/comparer, commands above.
- `programs.py` - generators for `programs.json` (deterministic programs) and `litmus.json` (litmus programs at k = 32768 iterations, with and
  without PAUSE in the spin loops). `model_smp.py` - independent expected final state of the deterministic programs, written from the
  per-program specification below (not from the assembly), using only Python integers.
- Bytes are authoritative in the json files (`code` is hex). Re-running `build` must not change them (nasm 3.02).

## Stub: how CPUs start (extends `tools/bench/stub.asm`)

Map: identity-mapped 4 KiB pages 0..4 MiB, present/rw/user with A/D preset (no walk ever writes); stub, GDT and tables at 0x100000..0x106000,
config at 0x106000, result slots 0x107000, boot stacks 0x108000..0x10c000, IDT, fault stubs and handler 0x10c000, TSSs 0x10e000, bodies and
data from 0x110000 (python-patched into the image).

1. **BSP** (SeaBIOS enters the Multiboot image on CPU 0): copies a 33-byte real-mode trampoline to 0x7000, loads the GDT, enables PAE, EFER.LME,
   paging, far-jumps to 64-bit CPL0. CPU index = initial APIC id (`cpuid` leaf 1 EBX[31:24]); per-CPU boot stack 0x109000 + 0x1000 * id.
2. **INIT-SIPI-SIPI** (only when `ncpu > 1`): the BSP enables the local APIC in x2APIC mode (IA32_APIC_BASE bits 10-11, SVR 0x1FF) and writes
   the x2APIC ICR (MSR 0x830) three times with destination shorthand "all excluding self": INIT (level assert), then SIPI vector 0x07 twice,
   each followed by a busy delay. No MP table or ACPI parsing: QEMU's `-smp N` gives APIC ids 0..N-1.
3. **AP**: real mode at 0x7000 -> `lgdt`, CR0.PE -> far jump to the flat 32-bit segment -> the same PAE/LME/paging sequence as the BSP -> 64-bit
   CPL0 (the BSP's and APs' code from `common32` on is one path). Each CPU masks its PICs, points its GS base at its own 0x100-byte result slot
   (`IA32_GS_BASE`, set at CPL0 and kept across `iretq`), loads its own TSS (RSP0 = its boot stack) and the shared IDT, and increments the
   `ARRIVE` counter. Controls from here on: CR0 = PE|ET|WP|PG (0x80010011, CD/NW clear), CR4 = PAE (0x20), EFER = LME|LMA|NXE (0xd00); the
   same values natemu's program driver starts a vCPU with.
4. **Start barrier**: the BSP waits until `ARRIVE == ncpu`, then stores `GO`; every CPU spins on `GO`. Then each CPU loads its own 16 GPRs
   from its entry of the CPU table and does `iretq` to **CPL3** (CPL0 when the CPU's `cpl` is 0) at its own `rip` (user CS/SS, IOPL = 3 so
   the program may `out`, IF = 0, RFLAGS per CPU masked to the six arithmetic flags). No interrupts after start-up. An exception (vectors
   0..31, interrupt gates) stores the GPRs and RFLAGS as the faulting instruction found them and a fault record (vector, error code, RIP, CR2)
   in the CPU's slot, restores the start-up CR4, CR3, CR0 and EFER and joins the capture at `done` (a CPL3 fault switches to RSP0).
5. **Body**: runs unmodified; the end is a fall-through. python appends `jmp capture` (5 bytes) after each body.
6. **Capture** (CPL3, GS-relative): store 16 GPRs and `pushfq` to the own slot, set the slot's `done` (TSO: after the register stores), CPUs > 0
   park in a `pause` loop; CPU 0 waits for every `done`, then writes the dump to debugcon (port 0xe9) with `rep outsb`, then `out 0xf4`
   (`isa-debug-exit`, process exit status 1). User code must not use FS/GS.

Dump: `"SMPCFG01"`, ncpu (u64); per CPU 136 bytes (16 GPR qwords in reference order, RFLAGS qword); each `capture:true` memory region in program
order; `"ENDDUMP!"`. `parse_dump` rejects a wrong length, header or footer. The final RIP is not part of the dump (it is the end of the body by construction).

Pitfall found: RAM beyond the image is **not** zero under QEMU/SeaBIOS (the kernel path string lands near 0x250000), so `build_image` writes
zero regions explicitly into the image (image is up to 4 MiB, loaded in milliseconds).

## Schema extension: `natemu-smp-v1`

A superset of the `tools/reference` program, with the single `code`/`rip`/`gpr` replaced by per-CPU entries (a one-CPU program with one body is
a plain reference program):

    {"format": "natemu-smp-v1", "name": label, "ncpu": 1..4,
     "bodies": [{"addr", "code": hex}...],                     # code images; CPUs may share one
     "cpus":   [{"rip", "gpr": [16], "rflags"}...],            # ncpu entries, index = CPU number; rip must lie inside a body
     "memory": [{"addr", "data": hex | "size","fill":"zero"|"lcg","seed"[, "capture": false]}...],   # shared by all CPUs
     "params": {...}}                                         # informational (model parameters, litmus locations)

- A CPU's code ends where the body containing its `rip` ends (`addr + len(code)`); that is its final RIP. All CPUs start together after every CPU
  has arrived (start barrier) with their own registers. Integers are numbers or `0x` strings; GPR order `rax rcx rdx rbx rsp rbp rsi rdi r8..r15`.
- `rflags` is the initial arithmetic-flag value (0 everywhere here); IOPL = 3 and IF = 0 are implied. Final RFLAGS is compared under 0x8d5.
- Memory: all RAM below 4 MiB is mapped user rw; `memory` lists the regions whose initial contents are defined and (unless `capture:false`)
  whose final contents are compared. Stacks (`rsp` of CPU i = 0x3E1800 + 0x2000 * i) are mapped but not listed, not initialised and not
  compared; bodies never use them. Bodies are position-dependent (absolute addresses in the regions); code is not stored to.
- `lcg` fill: `x = x * 6364136223846793005 + 1442695040888963407 (mod 2^64)`, byte = `x >> 56`, from `seed` (as `tools/bench`).
- Result of a run: `{"cpus": [{"gpr": [16], "rflags"}], "memory": [{"addr","data"}]}` (`smp.parse_dump`); `smp.compare(prog, got, want)` lists differences.
- `system: true` (system programs): CPUs may have `"cpl": 0` (default 3); QEMU runs with `-cpu max,-pcid,-smap` (natemu's CPU model has
  neither, so CR4.PCIDE/SMAP are reserved); the dump has a 40-byte fault record per CPU after the CPU slots and each CPU's result a
  `fault`: None or (vector, error code, RIP, CR2 for #PF), compared by `smp.compare`. natemu runs such a program in an identity-mapped
  space (driver `identity`), so bodies may build page tables from physical addresses.

## (c) System programs (`system.json`, from `programs.system_all`)

CPL0 bodies with MOV to/from CR0/CR3/CR4, INVLPG (four operand forms and a non-canonical no-op), RDMSR/WRMSR EFER: a CR3 switch between
two address spaces (also a same-value load and ignored low CR3 bits), INVLPG after PTE rewrites, CR0.WP toggled around supervisor writes to
a read-only page (the last one faults), EFER.NXE toggled with read-backs (an XD bit becomes reserved: the last read faults), control
read-backs with CR4.PGE/SMEP set and cleared, a SMEP fetch fault, an XD fetch fault, each at 1, 2 and 4 CPUs (own code page, tables and data
per CPU); and a PTE change without INVLPG (either history is legal: not compared with QEMU). Each CPU's two table sets identity-map
0..4 MiB with A/D preset except on its test pages (their A/D are compared). Bodies end by restoring the start-up controls. Fault cases
(`sysfault-*`, `params.fault_at` = the instruction under test): reserved CR0/CR3/CR4 bits, PCIDE, SMAP, clearing PE/PG/PAE, NW without CD,
LOCK, CR1/CR5, CPL3 forms, reserved EFER bits, LME cleared with paging on, unknown MSRs. QEMU 11.1.2 is not a reference for most of these
(it accepts some writes the SDM faults and hangs on others); `tools/run_slice3_vm.py system` judges them with `tools/system_fault_model.py`.
S1 adds MSR read-backs compared with QEMU (`sys-msr-syscall`: STAR/LSTAR/CSTAR/FMASK, SYSENTER, FS/GS/KERNEL_GS_BASE, TSC_AUX, per-CPU
values, the stub's GS base saved and restored; `sys-msr-mtrr`: PAT, MTRR_DEF_TYPE, variable and fixed MTRRs) and fault cases for more
profile MSR rules and LOCK CPUID/RDTSC. CPUID values, MSR reset values and the like are natemu's CPU, not QEMU's: the runner judges them
with `tools/models/cpu_profile.py` (`profile_program`).

Concurrent controls (`sys-ctl-<scenario>@N`, N = 2, 4; `programs.ctl_program`, contract 33): CPU 0 toggles CR3 (roots A, B, a copy A2 of A; B maps V to
another frame and has no U), CR0.WP and EFER.NXE 6 times per round for 10 rounds, settling each round in a state from a fixed generator, while its peers
(one shared body, shared tables and code) loop on loads, stores and fetches, log their own CR0/CR3/EFER and what V resolves to, and ACK through flags
(plain loads and stores). Peers walk V afresh (INVLPG) before each read when CPU 0 toggles CR3, and R before the final store in the WP scenarios, so a leaked root or WP shows at once. A final stage has each peer make one access whose outcome depends only on its own controls (R store: #PF 3 with WP=1; XD fetch:
#PF 0x11 with NXE=1; X read: #PF 9 with NXE=0) while CPU 0 holds the opposite controls; CPU 0 ends by faulting on its own. Scenarios: `cr3`, `wp-clear`
(peers clear WP), `wp-set`, `wp-mixed` (4 CPUs: peer 2 clears), `nxe-keep`, `nxe-clear`, `nxe-mixed`, `all`. Compared with QEMU like the other system programs.

## (e) Code coherence programs (`coherence.json`, from `programs.coherence_all`; S3d)

Code written at run time by one CPU and run by others, at 2 and 4 CPUs, judged against QEMU's run of the same bytes (QEMU keeps translated code
coherent with stores). A run-time code page has a one-byte body at +0 (natemu's driver then maps it writable and executable) and a captured
region from +0x10, so its final bytes are compared; run-time code is entered with `jmp` and returns through `jmp r13`; handshakes are plain
stores and loads (no PAUSE, no XCHG). `fresh-new`: CPU 0 writes 6 variants of a function in turn into a page, each published by a flag and run by
every other CPU before the next; `fresh-reused`: the same on a page the others first stored to and loaded from; `fresh-cr3` (CPL0): the same, each
CPU loading CR3 with its own value before running the function; `rounds`: every CPU in turn patches one shared function, all run it each round;
`data`: CPUs 1.. store to their own word on the page CPU 0's hot loop runs from until it ends; `rewrite`: CPUs 1.. store CPU 0's loop bytes back
over it (same values) until it ends; `mutual`: CPU i loops on its page and stores to CPU i+1's; `finish` / `finish-late`: CPUs 1.. store to a
page, set a flag and end (immediately / after a delay loop) while CPU 0 waits for the flags and runs a function on that page. `stale` (3 and 4 CPUs):
CPU 0 calls a one-block function in a loop until its result changes, CPUs 1.. store data on its page until CPU 0 is done, and the last
one patches a single instruction of the function after CPU 0 went round 2,000 times (an uncaptured counter). Pinned QEMU (MTTCG) now and
then never finishes `stale` (2 of 8 runs at 3 CPUs, 1 of 10 at 4); the runner repeats a QEMU run that times out.

## (f) Shared guest page-table programs (`sharedpt.py`; S3f)

Generated at run time (no json), judged by `tools/run_slice3_vm.py sharedpt` against `tools/models/coherence_model.py` (walks, `Cache`,
`access_outcomes`, `check_observation`) and, for what is schedule-independent, QEMU. Every program is a system program (CPL0) with ONE shared body
and ONE shared table set (identity map of 0..4 MiB, 4 KiB pages, A/D preset except on the test pages) loaded by every CPU; CPU `rbx` logs what it saw in
a private log area (not captured) and the judge replays the logs through the model. Handshakes are plain stores and loads.
`ad-race`: all CPUs access one PTE (A/D clear); synchronized rounds (CPU 0 clears with a plain store + INVLPG, each CPU INVLPGs and reads or
writes per a role table, samples the PTE; CPU 0 samples after every acknowledgement: exactly the model's A/D), then a free race (CPU 0 clears + INVLPGs
in a loop while the others do random reads, writes and INVLPGs; every PTE sample must be a state the model reaches), then more rounds ending with all writers.
`remap`: CPU 0 flips a PTE between two frames each round; each CPU reads and stores through V before its own INVLPG (a mode table: stale reads, INVLPG before the
remap is seen, none), after a staggered delay INVLPGs, then reads and stores (new frame only). `large`: a PDE switches between a 2 MiB leaf and a
table (T's frame differs, S's does not), each CPU INVLPGs at its own time; the PDE's and T's PTE's A/D after each round are exact. `alias`: two linear pages map one
frame; each round every CPU writes its word through one alias (or reads through another) and, after all wrote, verifies every word through a third alias (the
identity page or either alias); both PTEs' A/D after each round are exact. `contended-{data,fetch,both,max}`: a clearer cycles a PTE through four values (bits 9, 10)
while the others INVLPG and access it (store/load, or run code from its page); `-max` pauses nowhere (walkers are starved but finish). The final PTE bytes come from
a last synchronized round. Memory the schedule decides (pre-INVLPG store words, phase-2 words, logs) is not captured; registers are not compared.

## (a) Deterministic programs (`programs.json`, 8 programs x 2/3/4 CPUs)

Every one ends in `cmp r11, r11` (all six flags defined) and has a schedule-independent final state; spin waits only on flags written by a
specific peer (handshake), never on an unordered race. `model_smp.py` computes each from its specification:

| name | what | notes |
|---|---|---|
| disjoint-lcg | CPU i runs an LCG-indexed load/add/store sweep over its own 4 KiB page (200000 + 10000 i iterations) | no sharing |
| shared-ro | all CPUs read a shared 64 KiB LCG table with CPU-specific strides, store a checksum into CPU-specific slots | shared read-only data, per-CPU output slots |
| handshake | CPU 0 (body A) fills a 256-qword table, publishes a flag; CPUs > 0 (body B) wait for it, checksum the table (per-CPU multiplier), set a done flag; CPU 0 waits for all done flags and sums the results | two bodies, release/acquire by TSO order, no LOCK |
| token-ring | a single token word passes 0 -> 1 -> ... -> n-1 -> 0 for 1000 rounds; each holder logs to its own slot of an interleaved log | strictly alternating, log content deterministic |
| xchg-lock | spin lock with `xchg [m], eax` guarding a read-modify-write of a shared counter, 5000 iterations per CPU | slice 3's memory XCHG (implicit LOCK); final counter = n * 5000 |
| lock-counter | (I3, contention) every CPU runs 20000 rounds of LOCKed read-modify-writes on shared qwords at `r15` (0x260000, one 256-byte block): +0 `lock xadd` 1, +8 `lock inc`, +16 `lock add` 3, +24 `lock sub dword` 2, +32 two `lock neg` (the word starts at 1), +40 `lock cmpxchg` +1 retried until it succeeds, +48 two `lock xor` of the CPU's token r8, +56 `lock bts` then `lock btr` of the CPU's bit r9 = i, +64 `lock or dword` of r10 = 1 << i, +80 a 128-bit +1 by `lock cmpxchg16b` retried, +96 a 64-bit +1 by `lock cmpxchg8b` retried; ends with rax rbx rcx rdx zeroed | final words: +0, +8, +40 = n * 20000; +16 = 3 n * 20000; +24 dword = -2 n * 20000 mod 2^32; +32 = 1; +48, +56 = 0; +64 dword = 2^n - 1; +80 (128-bit) and +96 (64-bit) = n * 20000; registers r15 r8 r9 r10 as started, rsi = 0 |
| neg-race, neg-private | (I3) 20000 `lock neg qword [r15]` per CPU on one shared word (race) or each CPU's own (private), each starting at 1 | every word ends 1; the runner also requires some CPU of `neg-race` to link more exits than any of `neg-private` (the lost-race exit to the NEG's own RIP ran) |

## (b) Litmus programs (`litmus.json`: sb mp lb wrc iriw sb-mfence sb-xchg iriw-mfence, each at `thread=multi` k=32768 and a `-pause` form)

Declarations come from `tools/models/ordering_model.py` (`PROFILES`; `iriw-mfence` is declared locally with a fence between the two loads). Each
CPU runs k iterations of: CPU 0 waits for all CPUs to finish the previous iteration and resets the locations; every CPU announces iteration `it`
in its own sequence word and waits for the others' (symmetric arrival); a per-CPU xorshift picks a 0..63-spin random start offset (without it the CPU
that arrives last at the barrier always wins the race and the interesting outcomes never appear); the test's plain `mov dword` stores,
loads, `mfence` or `xchg` run; loaded values (bytes) go to a per-CPU result array (2 bytes per iteration). Locations x, y (and d0, d1) are separate
128-byte slots (distinct cache lines). The runner histograms per-iteration outcomes in the model's `outputs` order and labels each with
`enumerate_outcomes(test).outcomes` ("observed outside model" would be a QEMU/model/program bug).

## Results (`/Users/liaozping/orca/natemu-raw/qemu-smp-20261027-01/`)

Host Apple M4, macOS 26.6.2, QEMU 11.1.2 (binary SHA-256 in `*/manifest.json`), nasm 3.02. QEMU printed nothing on stderr in a spot check of handshake@4 (`thread=multi`).

- `selftest/`: xchg-lock@2 matches the model; 4 corrupted-dump controls (GPR bit of CPU 0, GPR of CPU 1, one flag, one byte of shared memory)
  and 3 malformed-dump controls (truncated, bad footer, extra byte) are all reported; a run with one CPU's iteration count changed disagrees with the
  model.
- `det/`: 15 programs x `-smp 2,3,4` x (`thread=multi`, `thread=single`), 20 runs each = 600 runs: **all 30 configurations match the model in 20/20 runs
  and all 20 dumps are byte-identical** (1 distinct dump per configuration). Wall time 0.05-0.25 s per run.
- `litmus/` (`thread=multi`, 30 runs x 32768 = 983,040 trials per test; `thread=single`, 5 x 4096 = 20,480): **no outcome outside the model's allowed set in
  any test.** Observed outcome sets, `thread=multi`, (outputs in model order):

| test | outcomes seen (count) | model-allowed, never seen |
|---|---|---|
| sb (r0,r1) | 01 838832, 10 85747, 11 58461 | 00 |
| sb-mfence | 01 846407, 10 77394, 11 59239 | none (00 forbidden, not seen) |
| sb-xchg | 01 860501, 10 19478, 11 103061 | none (00 forbidden, not seen) |
| mp (flag,data) | 00 132478, 01 6891, 11 843671 | none (10 forbidden, not seen) |
| lb (r0,r1) | 00 93332, 01 839429, 10 50279 | none (11 forbidden, not seen) |
| wrc | 7 of 8 outcomes (101 505843, 111 256125, ...) | none (the forbidden one not seen) |
| iriw, iriw-mfence | 14 of 16 outcomes each, 1010 (the TSO-forbidden one) not seen | iriw: 0101 allowed, never seen |

  `thread=single` (round-robin on one host thread) gives one outcome per test: each CPU runs a whole time slice, so iterations never interleave
  (sb 01, mp 11, ...). It is a determinism check (the deterministic programs pass in it), not a litmus source. Full counts: `litmus/litmus-results.json`.

## Limits

- QEMU TCG is not an x86 CPU: these outcome sets are QEMU's, a sanity reference. In particular **SB's allowed 00 was never observed** in 983,040 trials
  (cause not investigated), so QEMU cannot discriminate "SB without a fence" from "SB
  with"; sb-mfence/sb-xchg at 00 = 0 prove nothing about the emulator under test. Use the model's allowed set, not QEMU's observed set, as the oracle.
- Outcome frequencies are skewed by barrier latency and run-to-run host load; they are not comparable between machines or runs. Only containment in
  the allowed set is a result.
- Not covered: LOCK-prefixed read-modify-write (only `xchg`), unaligned and line/page-crossing accesses, SMC across CPUs, interrupts/IPIs after
  start-up, TLB shootdown, concurrent controls over shared tables, more than 4 CPUs.
- QEMU 11.1.2 deviations seen by the system programs: a reserved-bit #PF has P = 0 in its error code (SDM: 1; `run_slice3_vm.py` corrects
  it, declared); MOV to CR0 with bits 63:32, PE=0/PG=1 or NW without CD, MOV to CR4 setting PCIDE (with `-pcid`) or clearing PAE, WRMSR
  EFER with reserved bits or an LME change under paging, and RDMSR/WRMSR of an unknown MSR complete without #GP; MOV to CR4 setting a
  reserved bit or SMAP (with `-smap`), MOV to CR0 clearing PG and MOV to CR3 with bits above MAXPHYADDR hang (the vCPU stays at the
  instruction, no exception logged).
- Each CPU's boot path touches shared stub memory (ARRIVE/GO, result slots) before the body; programs must not use 0x100000-0x10c000 or
  0x3E0000-0x3F0000 other than the stacks above. Bodies at 0x110000.. and regions below 0x400000.
- `thread=single` litmus runs use the `-pause` variants (PAUSE in the spin loops, untested without); `thread=multi` uses the plain ones.
