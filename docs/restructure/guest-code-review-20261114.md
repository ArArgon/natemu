# Guest code review: what natemu runs at EL0, and where its time most likely goes

November 14, 2026, branch `perf-real-workload` at `4b19f44` (second batch confirmed). Group G of the
[architecture review](architecture-review-20261113.md) (root cause D). Documenting review: no code change, no VM run.
The compiler `fexc` (a host process) was run directly on x86 bytes, as the host runs it, and its Arm output read. Evidence
and the scratch scripts that produced every table are in `natemu-raw/guest-code-review-20261114/` (`xlate.py` translates
and classes words; `run_corpus.py` the idiom corpus; `sample_bins.py` the static samples of the workload's binaries;
`sumbuckets.py` the bucket sums). Nothing here is a recommendation to build; section 6 lists candidates as candidates.

## 0. Summary of findings

1. **The guest bucket is 36.6% of configure's busy time and 67.8% of make's** at head (`natemu-raw/relink-gate-20261114-01/
   head-3/perf-{configure,make}.log`, four vCPUs summed): 36.4 vCPU-s over 83.9 M runs (434 ns, about 1,700-1,900 cycles
   per run) and 358.2 vCPU-s over 235.5 M runs (1.5 us per run). Against the native arm64 VZ reference (configure 7.8 s,
   make 27.1 s, io5-perf-notes reference table) the guest bucket alone is **about 4.7x native on configure and about
   3.3-3.8x on make** (make: 358 vCPU-s against 27.1 s x 3.5-4 busy cores); the whole emulator is 8.1x and 4.5x by wall.
   The ratio is per workload, not per instruction (the native build compiles for arm64 with an arm64 compiler).
2. **FEX's instruction selection is not the gap.** On the same bytes, FEX's own profile emits 2.2-2.3 Arm words per x86
   instruction for cc1, libc and dash (static sample, section 2); the slice-1 code natemu runs is 2.9-3.2 words per
   instruction once linked (4.3 before linking, exits and stubs included). The straight-line ALU/SIMD mapping is one to
   one or close (int body: 10 x86 -> 18 body words, of which 5 are flag bookkeeping; `movsd`/`mulsd`/`addsd` -> 1 word each).
3. **What natemu adds to FEX's words is about 0.9-1.0 word per x86 instruction**, nearly all for exactness and
   containment: the page-split check before every store wider than a byte (3 words plus, for most stores, one address
   word: 19-25% of cc1's static words), one extra `mov` after every load (the destination is UNKNOWN after an abort, so
   loads go to a scratch register: about 7% of words), a saved-NZCV `mrs` before every memory read-modify-write, and the
   computed-exit sequence. Measured ceiling for the split checks alone: -3.7% wall on `build`, -6.5/-6.7% on `luajit`/
   `zstd` (chunk 28), so roughly 6-10% of guest time where stores are frequent.
4. **The largest single structural cost found is control flow leaving blocks through the shared dispatcher**: every
   `ret`, indirect `call` and `jmp` runs 15 words and three taken branches, one of them an indirect `br` whose target
   depends on the guest RIP; chunk 28's samples put 15-16.5% of `build`'s EL0 time there, 77-80% of it on the word the
   `br` lands on (misprediction, not the compare). cc1 is C++ (0.9% `ret`, 0.3% indirect call/jmp per static instruction);
   its share on make is inferred at 10-17% of the guest bucket. natemu-side.
5. **The guest bucket also pays for what the resident leaves behind.** Configure's guest runs are 434 ns long and 84 M of
   them follow a return from EL1; make's are 1.5 us. Cold predictors, i-cache and TLB after each return are charged to
   `guest`. Unmeasured; by the run lengths it could be 10-25% of configure's guest bucket and 5-10% of make's. This is
   the first thing the sampler must settle (section 5).
6. **REP strings are translated one element per block pass for MOVS**: `rep movsq` is 17 words per 8 bytes, `rep movsb`
   13 words per byte, each iteration leaving and re-entering the block through its own linked `b`; only REP STOS has the
   bulk path (chunk 26: 5 words per 16 bytes). The kernel has 418 `rep movsq` and 4,315 `rep stosq` sites (copy_page,
   memcpy, copy_user without ERMS, which natemu does not advertise); cc1 has 536 inline `rep movsb` and 243 `rep stosq`.
   `build`'s REP blocks were 2.1% of its block samples; configure's fork/exec path copies pages and argument strings, so
   its share is probably larger (unknown, 2-5%). FEX-side (fork), bounded, the chunk 26 pattern.
7. **Flags cost 5-9% of words**: PF and AF are kept raw in two static registers (`x26`, `x27`: one or two `mov`s per
   flag-writing instruction whose flags may outlive the block, and every block's last flag writer qualifies, since the
   next block is unknown); CF is materialised for shifts, rotates, `imul`, `inc`/`dec` (`rmif`, `cset`, `cfinv`); a 16-bit
   compare is 5 words. Partly inherent to x86 on Arm (PF/AF, CF-preserving `inc`/`dec`), partly the single-block scope
   (FEX's flag elimination stops at the block end). Time share unknown; these words are cheap but `rmif`/`mrs nzcv`
   serialise on NZCV.
8. **glibc's AVX2 string functions** (strlen, strcmp, memchr: everything a shell, sed, make and cc1 call constantly)
   translate badly: `vpmovmskb` is 24 words (the 64-bit mask constant is rebuilt twice with four `movk` each; two
   `addp` reductions), and every 256-bit operation writes the YMM upper half through to the state page (`stp`/`ldr`/`str`,
   3-4 words). `__strlen_avx2`'s loop body is 39 words for 5 x86 instructions. FEX-side quality; share unknown (user
   string code is plausibly 3-8% of configure's user-mode guest time). Not measured.
9. **Code bloat is a front-end cost in itself**: 4.3 words (17 bytes) of Arm per x86 instruction (about 3.5-4 bytes)
   for cc1, a flat 34 MB binary whose top 20 blocks held 6% of `build`'s block samples over 9,155 distinct blocks. A 4-5x
   larger instruction footprint on a workload that is already front-end bound natively plausibly costs more than the
   words' execution. Hypothesis; needs the PMU or a footprint experiment.
10. **Not costs**: memory ordering (hardware TSO: plain `ldr`/`str`; only `lock cmpxchg` pays `dmb ish` + `casal`), the
    address-space guard (gone since E3), register allocation across blocks (static registers), alignment checks
    (2 words per aligned SSE access, rare in this workload).

Ranked by estimated share of the guest bucket (section 3): configure: cold state after returns (10-25%, low confidence),
dispatcher exits (8-15%, medium), split checks (5-8%, medium-high), REP strings and kernel copies (2-5%, low), flags
(3-6%, low), glibc AVX2 string code (3-8%, low), load-then-move (0-5%, low). make: dispatcher exits (10-17%, medium),
split checks (6-9%, medium-high), cold state (5-10%, low), flags (3-6%, low), code footprint (unknown), REP (1-3%).
No single item is above a fifth of the guest bucket, and the two largest are natemu's structure, not FEX's selection.

## 1. What is known about guest time (measured)

**Buckets at head** (`sumbuckets.py` over the per-vCPU `vcpu` lines; busy = all buckets but `halted`/`mailbox`):

| | configure | make -j4 |
| --- | --- | --- |
| busy vCPU-s | 99.4 | 528.6 |
| guest | 36.4 s (36.6%), 83.9 M runs, 434 ns per run | 358.2 s (67.8%), 235.5 M runs, 1,521 ns per run |
| next buckets | host 10.9, link 9.2, miss 8.4, abort 5.1, tlb 4.2, enter 3.6, delivery 3.3 | link 34.9, miss 24.4, host 23.6, abort 13.8, tlb 12.8, enter 10.6 |

A run is translated code between two exceptions: at about 4 GHz, 1,700-1,900 cycles on configure and about 6,100 on
make. The bucket is charged from the exception return to the next exception entry, so it holds the EL0 blocks, the
dispatcher and L1 tables, and the pipeline state the resident left behind.

**Native reference** (io5-perf-notes, reference table; Apple VZ arm64, same snapshot steps, median of 3): configure 7.8 s,
make 27.1 s. Head medians (decision 37): 63.2 s and 122.5 s. Guest-only ratios are given in the summary; they bound the
achievable gain: if translated code ran at native speed, configure would still be about 27 s of handlers and make about
45 s of wall.

**Where `build`'s EL0 time went** (chunk 24/28 sampler, `natemu-raw/breakdown-20261105-01/prof/build-n1.prof.md`,
`codeq-20261105-01/codeq28.txt`; the bench `build`, gcc on a small tree, one vCPU): blocks 48.1% of samples, resident 43.5%,
dispatcher and L1 8.5% of all samples = 15.0% of EL0 samples (16.5% with the exits' target check); split checks 4.4% held /
4.7% by words; REP blocks 2.1% of block samples; `svc` exits 4.3%; user-space blocks 94.5% of block samples; top block
(the kernel's `rep stosq`, 27 words) 1.1%; top 20 blocks 6%. The dispatcher's samples: 77% on the L1 entry's first word,
20% on the dispatcher head's first word (the two words a taken `br`/`b` lands on). Flag words and load-then-move were
not classed in that run.

**Benchmarks vs QEMU** (decision 29, `bench-round-20261114-01/tables.txt`, N = 1 cold): cray 0.02x, zstd 0.10x, luajit
0.21x, 7zip 0.19x, avx 0.19x, sqlite 0.32x, build 0.33x, locks 0.35x, mem 0.47x, hackbench 0.75x. Compute-bound loops are
far ahead; process- and syscall-heavy programs are where the structure shows. From the earlier phase against an AMD box
(architecture review section 2): cray 0.89x native, 7zip 1.32x, zstd 1.45x, build 4.4x.

**The S2g attribution** (tools/bench/README.md, October 27): the same FEX blocks run natively on the host in the harness
time the same as in the VM for int, mem64, mixed, call, branchy (the VM adds nothing measurable once linked); `int`'s 22
words per 10 x86 instructions cost nothing beyond the dependency chain (8 cycles); `call`'s dispatcher `ret` (15 words)
was 20% of its iteration; guards and split checks 6% of mem64. Those blocks are the best case: tiny, hot, linked loops.

## 2. What a translated block looks like today

Read from fexc's output (`--profile slice1`, the host's flags: `crates/host/src/execution.rs:3043-3090`; `--cpl0 --tsc
--bulk` for CPL 0), disassembled with clang/objdump as `tools/bench/attribution/dis2.py` does. Layout from
`crates/abi/src/execution.rs` (`ENTRY_OFFSET` 4: one header word; `cache::DISPATCH`, `L1_CODE`, `l1_words`) and the
dispatcher template (`crates/runtime/src/gates.S:244-259`).

**Anatomy** (what each class of word is, who emits it):

| Words | Emitter | Why |
| --- | --- | --- |
| FEX body: ALU, SIMD, address arithmetic, loads/stores | FEX | the translation proper |
| flag bookkeeping: `subs x26`, `mov x27`, `rmif`, `cset`, `cfinv`, `ccmp`, `mrs x25, nzcv` | FEX (+ slice-1 patch 5 for `mrs x25`) | PF/AF raw registers, CF materialisation, incoming NZCV for exact faults at RMW sites |
| load-then-move: `ldr x20, [..]; mov xR, x20` | FEX under slice 1 (`fexc_precision.h:27-30`) | a load's destination is UNKNOWN after an abort (DDI0487 D1.4.1.4) |
| split check: `[add x1, base, #off;] add x0, x1, #w-1; eor x0, x0, x1; tbnz w0, #12, stub` | slice-1 patch (admission: `structure.rs` "Split") | a store crossing a page must complete exactly across a fault |
| alignment check: `and x0, A, #15; cbnz x0, stub` | slice 1 | #GP for misaligned aligned-SSE forms |
| branch-target check: `eor x0, xG, xG, lsl #16; tbnz x0, #63, stub` | slice 1 | #GP at a non-canonical computed target |
| literal exit: `movz/movk w0, #rip; svc #LINK` (2-3 words) | slice 1 | the linker rewrites word 0 to `b entry`: 1 executed word once linked |
| computed exit: `mov x0, xR; b <dispatcher slot>` | slice 1 (`DISPATCH_PLACEHOLDER`, relocated at placement) | every `ret`, indirect `call`/`jmp` |
| dispatcher: `and x1, x0, #mask; adrp x2; add x2, x2, x1, lsl #6; br x2` | natemu (`gates.S`) | into the RIP's L1 set |
| L1 entry (way 0 hit): `movz x2; movk x2 x3; sub x2, x2, x0; cbnz x2, way1; b entry` | natemu (`l1_words`) | 7 words executed on a hit |
| stubs: `brk #n` | slice 1 | never executed on the fast path |
| header word, literal slots, tail | FEX/natemu | data, not executed |

So a computed exit is 4 (block) + 4 (dispatcher) + 7 (L1) = **15 words and three taken branches** (`b` to the slot, the
indirect `br`, `b entry`), 17 with `ret`'s pop; a direct branch once linked is 1 word; a block entry costs no prologue
(the stop check of S2g is gone).

**The int loop** (tools/bench/bodies/int.asm; 10 x86 -> 23 words; `corpus.out`'s method on `int.json`):

```
add  x4, x4, x6            ; add rax, rbx
mov  x5, x4 / lsl x5, x5, #13 / eor x4, x4, x5      ; mov rdx,rax; shl rdx,13; xor rax,rdx
mov  x5, x4 / lsr x5, x5, #7 / eor x4, x4, x5       ; shr/xor
smulh x20, x4, x10 / mul x4, x4, x10 / asr x21, x4, #63 / cmp x20, x21 / ccmp xzr,#0,#1,eq / cset x20, hs   ; imul: 6 words, 4 for CF/OF
subs x26, x7, #1 / rmif / mov x27, x7 / mov x7, x26 ; dec rcx: 4 words (CF kept via rmif, PF/AF raw)
b.ne +0x10 ; mov w0,#..; movk; svc #1 ; mov w0,#..; svc #1   ; two literal exits (each becomes one `b` when linked)
```

The `cset x20, hs` for `imul`'s CF survives because `dec` does not write CF and the block ends: nothing after the block
is visible to FEX's flag elimination. In FEX's own multiblock mode this loop is one unit and the `add` kills CF.

**A real cc1 block** (0xc0ed94, 12 x86 -> 46 words, static sample seed 11): five stack stores each cost `add x1, x8, #off`
+ 3 check words + the `str` (15 split words of 46, plus 5 address words the check forces: the store itself uses the
immediate form), five `brk` stubs; `movq xmm0, [rip+..]` is `mov w21/movk` + `ldr d2` + `mov.8b v16, v2` (load-then-move
for SIMD too). Another (0x1766922, 10 -> 28): four loads, each `ldr x20` + `mov`; `shr eax, 6` is 5 words (`lsr w26`,
`cmp`, `eor` for CF bit, `rmif`, `mov x4, x26`); `bt rsi, r15` is `lsr` + `rmif`; `jae` is `cfinv` + `b.hs`. A `pop`x6 +
`jmp rax` epilogue (0x1ac8217): each `pop` is `ldr x20, [x8], #8; mov xR, x20` (2 words against 1 native), then the
15-word exit. These are in `natemu-raw/guest-code-review-20261114/` (reproduce: `sample_bins.py` with seed 11).

**Static sample of the workload's binaries** (`sample_bins.py`: 300-400 random instruction boundaries of `.text`, 64 bytes
each translated as the host would at that RIP, CPL 3; kernel with `--cpl0 --tsc`; blocks end at the first control-flow
instruction, so random starts give shorter blocks than the dynamic ones, 3.1-4.5 x86 instructions). Binaries copied
read-only out of the IO5 snapshot (`extract.py`, debugfs `dump`); the kernel is the bzImage's zstd payload. Every block
counts once however hot it is:

| binary | blocks | words/x86 insn, all | once linked (exits = 1 `b`, stubs excluded) | FEX default profile, same blocks | split | mem | load/store sites | alu | flags (classed) | exits (link words) | computed exits |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| cc1 | 300 | 4.27 | 3.16 | 2.23 | 19.0% | 11.3% | 0.48/insn | 28.9% | 3.6% | 25.6% | 0.03/insn |
| libc.so.6 | 296 | 3.84 | 2.90 | 2.17 | 14.1% (sample 1) | 10.0% | 0.40 | 27.3% | 5.4% | 24.5% | 0.06 |
| dash | 300 | 3.92 | 3.03 | 2.32 | 19.5% | 11.9% | 0.45 | 28.3% | 5.1% | 21.4% | 0.05 |
| bash / sed / make / as | 400 each | 3.96 / 3.66 / 3.76 / 4.08 | 3.26 / 2.90 / 2.84 / 2.75 | - | 12.8-17.1% | 9.1-12.2% | 0.37-0.45 | 28-31% | 4.1-5.3% | 22-33% | 0.03-0.04 |
| vmlinux (CPL 0) | 166 | 2.57 | 1.88 | 1.30 | 17.8% | 10.6% | 0.27 | 29.6% | 5.6% | 27.2% | 0.00 |

The "flags" column undercounts (only the unambiguous forms are classed: `rmif`, `cset`, `cfinv`, `mrs/msr nzcv`, writes
to x26/x27 by moves; the `cmp`/`eor` that compute a shift's CF are counted as ALU). Reading: natemu's slice-1 code once
linked is **about 1.3-1.4x FEX's own words** on this code; the difference is the split checks and their address words
(about 0.8 + 0.25 words per instruction on cc1), load-then-move (about 0.3), `mrs x25` at RMWs, the computed-exit words.
cc1 is the heaviest: 28% of its static instructions are stores or pushes (12.4% + 3.0%), each paying the check.

**Idiom corpus** (`corpus.asm`, 48 snippets; `corpus.out` has every word): a `push` is 5 words (`sub x1; add; eor; tbnz;
str pre-index`); a `call` 11 (constant return address 2, push 5, exit 3); `ret` 7 in the block + 11 in dispatcher/L1;
`mov rax, [fs:0x28]` 3 (`ldr` of the FS base from the state page, `ldr`, `mov`); `add dword [rdi+8], 1` 10 (`mrs x25,
nzcv`, load, add, 4 check/address words, store, 2 PF/AF moves); `inc dword [rbx]` 11 (`cset` + `rmif` to keep CF);
`cmp ax, 0x37` 5; `movsd/mulsd/addsd` 1 each (+ 1 move for the load); `movaps` load 5 (`and/cbnz` alignment, `ldp`, two
`fmov` into the vector); `lock add` 4 (`ldaddal` + flags), `lock cmpxchg` 5 (`dmb ish` + `casal` + flags), `xchg` 2
(`swpal`); `rep movsb` 13 words per byte, `rep movsq` 17 per qword, `rep stosb/stosq` under `--bulk` 34-36 words with a
5-word `stp` loop per 16 bytes inside the page; `vpcmpeqb ymm, ymm, [mem]` 6 (two `cmeq` plus the YMM upper-half
`ldr`/`str` to the state page), `vpmovmskb eax, ymm1` 24; `syscall`, `cpuid`, `rdtsc` (CPL 3) are system cuts: the block
ends before them with a literal exit that is never linked (a resident trip each: `system`/`helper` buckets, not `guest`).

## 3. Findings, ranked by estimated share of the guest bucket

Shares are of the guest bucket (configure 36.4 vCPU-s / make 358 vCPU-s), estimates unless marked measured; confidence:
high = measured on this workload or a close one; medium = measured on another workload or counted statically with a
plausible dynamic weight; low = inferred from word counts and workload shape only. Class: **F** = FEX codegen quality
(fork `ArArgon/natemu-fex`, byte-identical gate), **N** = natemu structure (resident, linker, dispatcher, admission
contract), **I** = inherent to exact x86 on Arm.

| # | Finding | Class | configure | make | Conf. | Known / inferred | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Computed exits through the shared dispatcher: 15 words, 3 taken branches, the `br`'s target a function of RIP (one site, thousands of targets: mispredicts), the compare irrelevant | N | 8-15% | 10-17% | medium | known on `build` (15-16.5% of EL0 samples), inferred for cc1 from its 1.2% static ret/indirect rate and C++ shape; configure's sh/sed/make/gcc-driver mix is `build`-like | codeq28.txt; disp28.txt (predictable exit 0.95-2.4 ns, random 4.9-5.4 ns); `gates.S:253`; `l1_words` |
| 2 | Cold state after each return from EL1 (predictors, L1I, L1D, TLB), charged to `guest`; configure's runs are 1,700-1,900 cycles, 84 M of them | N (resident design) | 10-25% | 5-10% | low | inferred from run length; the review called it out (section 1B); no measurement separates it from the code | head-3 logs (434 / 1,521 ns per run); chunk 9 (990 cycles per entry before fixes) |
| 3 | Split-store checks: 3 words + usually an address word per store wider than a byte; 19-25% of cc1's static words, 28% of its instructions store | N (exactness contract) | 5-8% | 6-9% | medium-high | measured ceiling on `build` -3.7% wall (about 7% of its guest time), `zstd` -6.7%, `luajit` -6.5%, `hackbench` 0 | chunk 28 `ceil-summary.md`; `structure.rs` "Split"; sample tables above |
| 4 | REP MOVS one element per block pass (`rep movsq` 17 words/qword, `rep movsb` 13/byte, a linked self-exit per element); kernel copy_page/memcpy/copy_user use `rep movsq` (ERMS not advertised: `boot-features-plan.md:93`); cc1 inlines 536 `rep movsb` | F (fork) | 2-5% | 1-3% | low | `build`: REP blocks 2.1% of block samples (kernel `rep stosq` 1.1%, since made bulk); configure's fork/exec copies pages and strings: inferred | `rep-cpl0-bulk.out`; vmlinux.dis counts (418 movsq, 36 movsb, 4,315 stosq, 31 stosb); cc1.dis (536 movsb, 243 stosq) |
| 5 | Flag bookkeeping at block scope: PF/AF raw moves, CF materialisation (`rmif`/`cset`/`cfinv`), `mrs x25` per RMW; 5-9% of words | I (PF/AF, CF-preserving ops) + F (block-scope liveness) | 3-6% | 3-6% | low | word counts only; `rmif`/`mrs nzcv` serialise on NZCV; never sampled by class | int block; cc1 0x1766922; S_rmw, S_shifts |
| 6 | glibc AVX2 string functions: `vpmovmskb` 24 words (mask constant rebuilt twice), YMM upper-half write-through 3-4 words per 256-bit op; `__strlen_avx2` body 39 words / 5 insns | F | 3-8% of user time | 1-4% | low | glibc selects `*_avx2` (AVX2 advertised); strlen/strcmp/memchr are ubiquitous in sh/sed/make/cc1; no sample by symbol yet | S_avx2_strlen in corpus.out; patch 0021 (`avx_high` write-through, series29) |
| 7 | Load-then-move: +1 `mov` per load (and per SIMD load), about 7% of words | N (exact faults) | 0-5% | 0-5% | low | the M4 may eliminate register moves at rename; S2g saw `int`'s two extra moves cost nothing; unknown on memory-bound code | `fexc_precision.h:27-30`; cc1 0x1766922 |
| 8 | Code footprint: 17 bytes of Arm per x86 instruction (4-5x), on a flat profile (top 20 blocks 6%, 9,155 blocks in 12 s of `build`) | N+F | ? | ? | low | hypothesis; natively cc1 is front-end bound; no PMU | build-n1.prof.md; sample tables |
| 9 | Exits into system instructions and helpers (`syscall`, `sysret`, `cpuid`, x87 `svc`, 128/64 division): resident trips | N | small (in `system`/`helper`, not `guest`) | small | high | hackbench 15% of EL0 samples were `svc` exits; configure's 12.6 M system instructions sit in `system` 2.6 s | head-3; chunk 22 |
| 10 | Alignment checks on aligned SSE/AVX forms, branch-target checks (2 words each) | N | <1% | <1% | medium | static: align 0.01-0.03 words/insn, btcheck 0.03-0.06 | sample tables |
| 11 | Memory ordering and atomics | I | about 0 | about 0 | high | hardware TSO; `dmb ish` only before `casal` (`locks`) | S_lock; architecture review section 2 |

What is **known**: items 1 and 3 on `build` and the small benchmarks, items 9-11, the word anatomy and static ratios of
section 2. What is **inferred**: every configure/make share above (no sample of either has ever been taken: chunk 24/28
sampled the bench `build`, a different gcc on a tiny tree, one vCPU, `nokaslr`), item 2 entirely, items 4-8's dynamic
weights.

## 4. What the sampler must confirm first

1. The EL0 split of configure and make: blocks vs dispatcher/L1 vs words at block entry right after an exception return
   (item 2: sample density on the first words of a run against the rest), user vs kernel, per binary (cc1, as, sh, sed,
   make, libc, python, kernel).
2. Computed exits: their count per second, how many distinct targets each site sees (monomorphic sites are the inline-
   cache case; the `br` cost is only worth attacking if sites are mostly monomorphic), and the share of samples on the two
   landing words.
3. Per-word classes on the hot blocks: split, flags (`rmif`/`cset`/`cfinv`/x26-x27 moves), load-then-move, REP bodies,
   AVX2 string lowerings, YMM write-through: `codeq28.py`'s method with the classes of `xlate.py`.
4. The native comparison for the hottest functions: (a) the same FEX blocks run natively in the S2g harness
   (`tools/bench/attribution/build_native.py`) to separate the VM from the code; (b) `clang -O2 -arch arm64` of the same
   libc routines (strlen, memcpy) for a words-and-time floor; (c) QEMU TCG's `-d out_asm` word counts on the same blocks.
5. Which kernel string alternatives the guest patched in (ERMS/FSRM off: `clear_page_rep`, `copy_page` with `rep movsq`,
   `rep_movs_alternative`); the on-disk kernel text is pre-alternatives, so only a running guest tells.

Decision rule carried from the architecture review: a FEX-internal track only if FEX's own words (not glue) hold more
than 60% of guest time **and** the guest bucket is above 2x native; by section 2's words FEX's own body is about 70% of
the linked words, so the first condition is open and the sampler decides it; the second condition is met on both steps.

## 5. How to measure this precisely next (sampler design, condensed)

Measurement only; no translator change. Build it as its own feature (`sampler`), never under `perf` (rule of decision 16:
the perf build is the instrument; study code perturbs `lookup`). The default resident's loaded sections stay byte-identical.

- **Source of samples.** A periodic interrupt in the resident from the virtual timer, the one HVF delivers to the VM: the
  resident already programs it for the guest's LAPIC deadline (`cntv_cval_el0`, `crates/runtime/src/execution/lapic.rs:124`),
  so the sampler shares it, arming the earlier of the LAPIC deadline and the next sample tick and serving both on expiry
  (EL0 may read the counter but not program the timer: `CNTKCTL_EL1`, `crates/host/src/vm.rs:450`). Whether EL1 may also
  use the physical timer under HVF is unverified (nothing in the tree uses `cntp_*`) and not needed. No PMU: HVF exposes no
  PMUv3 to the guest (nothing in the tree touches `pmcr`/`pmccntr`; the S2g notes record "PMU needs root"), so cycle
  sampling is out; time sampling at a fixed rate is the instrument, as the chunk 24 host-side sampler was (it forced vCPU
  exits every 150-250 us from the host, which over-sampled EL1; an in-resident timer sees the EL0 PC exactly).
- **What one sample records**, into a per-vCPU ring in the EL1-only state page (16 bytes each, 1 Mi entries = 16 MiB per
  vCPU, drained by the host at run end or when half full): ELR_EL1 (the interrupted PC), SPSR.EL (EL0 or EL1), the code
  context (CPL, root), CNTVCT, and the number of cycles since the last return to EL0 (CNTVCT at `eret`, kept in a
  resident word: item 2's "distance from run start"). Attribution to a block is offline: code-area offset -> block
  record (`cache::block_at`'s binary search over the placement-ordered index, replicated in Python from a dump of the
  block records at run end: RIP, context, code offset, size, source pages) -> guest RIP + position in the block ->
  word class from the admitted object (the host's answer cache or the pool holds the bytes) -> symbol, by the guest
  binary's `objdump` at that RIP (page -> file via the block's source GPA and the guest's `/proc/*/maps`-equivalent: the
  simplest is to log, in the serial console, each exec's binary and load address from a tiny guest-side `LD_AUDIT`/
  `ld.so --list`-free method: read `/proc/PID/maps` once per new process from a background `sh` loop in the snapshot).
  For the kernel: `System.map` of the Debian kernel (in the snapshot's `/boot`).
- **Rate and overhead.** 4 kHz per vCPU (250 us): configure gives about 250 k samples, make 2 M; the interrupt costs an
  exception entry and about 40 words (about 0.2-0.3 us) = under 0.15% of busy time. Check: the perf buckets of a sampler
  run against the head profile must agree within the run-to-run band (guest +-2%, no bucket off by more than 5%); and
  the sampler's own EL1 time appears as a bucket of its own. Reject the run if either fails.
- **Chunks and gates.** 0: the ring, the timer, the dump, the offline attribution (`sampler.py`) with unit tests on
  synthetic records (offset -> block -> RIP -> class; a word table from `xlate.py`'s classes). Gate: a K1 or bench program
  with one known hot loop (the `int` body under the program driver) puts >= 95% of EL0 samples on that block's words and
  the per-word density matches the loop's shape; `call` puts 15-25% on the dispatcher/L1 landing words (S2g: 20%).
  1: one profile each of configure and make with the sampler on (only the main session runs them), report: EL0/EL1 split,
  per-binary, per-symbol top 50, per-class shares (section 4's list), computed-exit site census (count per site from a
  second ring of `(site, target)` pairs sampled at the dispatcher's miss and at a 1-in-64 hit tap, or from a one-off
  counting build), run-start density. Cost: a day of implementation, two profile runs (about 10 minutes of VM time), under
  2 GB of rings and dumps. Stop rule: if chunk 0's gate fails twice, stop and fall back to the chunk 24 host-side sampler
  with its EL1 over-sampling caveat.
- **What decides what.** FEX's own words above 60% of EL0 time with the guest above 2x native -> a FEX track is on the table
  (owner sign-off). Dispatcher landing words above 8% of EL0 samples -> item 1's census decides inline cache vs nothing.
  Run-start density holding more than 10% of EL0 samples in the first 200 cycles of runs -> the resident's return path and
  footprint (items of root cause B) are a guest-bucket lever too. REP and AVX2-string shares above 3% each -> bounded
  FEX-side items. Everything under 3% is recorded and left.

## 6. Candidate directions (candidates, not recommendations)

| Candidate | Side | Size | Risk | Gate it needs |
| --- | --- | --- | --- | --- |
| Inline cache per computed-exit site (one compare + direct `b`, filled on first trip; chunk 28's microbenchmark: 0 ns at a monomorphic site) or a PAC return cache for `ret` | N (linker, admission: a new stub kind) | L | linker-scale patching, unlinking with the direct links; HVF exposure of PAC keys for the return cache | the site census (section 4 item 2); dispatcher >= 8% of EL0 |
| Exact split-check elision for 8-aligned stack stores (one guard per block over RSP; cliff for misaligned stacks) | N+F (admission proof + shim) | M | performance cliff, admission proof over RSP writes | sampler share of split words on pushes (2.4% of `build`) |
| Bulk REP MOVS inside a page (the chunk 26 pattern for MOVS; forward DF, no overlap or the SDM's overlap rule respected) | F (fork) | S-M | element-boundary exactness on faults mid-string | REP share on configure >= 3% |
| AVX2 string lowerings: hoist/`movi` the `vpmovmskb` mask constant, cheaper 32-byte mask reduction, keep YMM upper halves in registers across the loop (the write-through was chosen for exact state at faults) | F (fork) | M | the byte-identical gate on every other block; exact-state rule for `avx_high` | per-symbol sampler share of `__*_avx2` >= 3% |
| Multiblock scope for hot loops (FEX's `--multiblock`: the block holds the loop; flags and constants optimised across the back edge) | F+N (admission's single-exit/forward-branch rules; the resident's exits per block) | L | contract change: back edges inside blocks, interrupt windows, exact faults inside a loop | flags + CF materialisation share >= 5% on hot loops |
| Flag words: cheaper PF/AF (FEX's own `--disable PF/AF`-style deferred computation already exists upstream; check what slice 1 disables) | F | S-M | exactness of PF/AF at every fault point | class share |
| Return path footprint: fewer resident words/lines touched on the common miss/link return, `eret` warm paths (root cause B work) | N | M | none new | run-start density |
| Advertise ERMS/FSRM so the guest kernel and glibc choose `rep movsb/stosb` paths that a bulk translation serves | N (CPU profile; owner decision: deviates from the profile's model) | S | changes guest-visible CPUID; needs bulk MOVS first | after bulk MOVS |

Not candidates: a data-table dispatcher (needs an EL0-readable page in the private window: a documented new deviation, and
it costs about 1 ns on unpredictable exits, chunk 28); dropping load-then-move (impossible exactly); dropping the split
check in general (no cheaper exact form, chunk 28); memory-model changes (nothing to gain).

## 7. Limits of this review

Static word counts weight every block once; the dynamic mix of configure and make has never been sampled on this
workload, and the shares in section 3 are built from `build`'s 2026-11-05 sample (another gcc, one vCPU, `nokaslr`, the
old resident with the stop prologue) plus word counts. The kernel sample is weak (166 blocks; 134 of 300 starts fell on
padding or system instructions; the on-disk text is before Linux's alternatives patching). The flags class is
undercounted. No cycle-level measurement exists for any class (no PMU); time per word is not constant (S2g: 22 words in
8 cycles on `int`). The native reference compares workloads, not instructions. CPython itself (`/build/python`) was not in
the snapshot's expected path and was not sampled.
