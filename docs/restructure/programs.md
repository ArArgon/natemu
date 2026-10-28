# Guest x86 program inventory for the current validation suite

Generated 2026-10-26 from the working tree at commit `577a7f2` (no git history read, VM not run). Decoded with GNU objdump (`/opt/homebrew/opt/binutils/bin/objdump -D -b binary -m i386[:x86-64] -M intel`); each program was decoded in the mode(s) the suite actually executes it in. Where independent Python oracles exist (`tools/i4v_controls_check.py`, `tools/i4v_modes_model.py`) they were run, unmodified, only to learn which instructions of the control programs execute, in what mode, and where they fault.

Later change (S3a2, October 27, 2026): the I1–I3, address and stack cells, their host drivers and their I3-only tools
are deleted; [slice3-contract.md](slice3-contract.md#s3a2-covering-and-deleting-the-i1i3-address-and-stack-paths) maps
each of their checks to a general-path check or a retirement reason. References below stay as of `577a7f2`.

## 0. Scope, counting rules and mode glossary

Scope: every x86 byte sequence that any runner, worker, oracle, fixture or QEMU comparison in `crates/`, `tools/` and `fixtures/` places in guest memory, hands to the FEX producer as source, or executes under QEMU. Excluded and listed in section 4: archived research under `experiments/`, decoder-only unit-test byte strings, and QEMU/NASM harness glue. Native AArch64 bodies (FEX output, `pressure_body.arm64`, runtime assembly) are not guest programs and are not inventoried. I1 `control` and `fault` cases run native NOP/UDF and contain no x86.

A **program** below is one distinct byte sequence in one family/role (for example the same two bytes `89 03` appear as the coherence store in 64-bit and in 32-bit profiles; they are listed as separate programs because the decode and address size differ). Counts used in the form tables are numbers of such program entries that statically contain the form.

Execution modes (names follow `crates/abi/src/modes.rs:mode()`):

| Label | Meaning | Decode as |
|---|---|---|
| `long64` | IA-32e active, CS.L=1 (64-bit code), 4-level paging | x86-64 |
| `compat` | IA-32e active, CS.L=0, CS.D=1 (compatibility mode, 32-bit code), 4-level paging | i386 |
| `P32-off` | Protected mode, CS.D=1, CR0.PG=0 (flat or biased segments, A20 on unless noted) | i386 |
| `P32-paged` | Protected mode, CR0.PG=1, CR4.PAE=0 (legacy 2-level, 4-byte entries) | i386 |
| `P32-PAE` | Protected mode, CR0.PG=1, CR4.PAE=1, EFER.LME=0 (3-level PAE) | i386 |

Programs that are not control programs run in one mode for their whole life. Control programs (profiles 0-38) change mode while running; section 2.3 gives the mode per instruction from the independent models.

The FEX producer is asked for 32-bit decoding for every mode except `long64` (`tools/i4v_controls_worker.py:bitness`), 64-bit decoding for `long64`.

## Headline numbers

- Program entries inventoried: **79** (23 long-mode, 10 protected32/compat/PAE data-path programs, 39 single-owner control profiles, 7 concurrent-control variants not byte-identical to a single-owner profile). Of these, 77 are executed by the suite; one (`moffs_store32`) is defined by a fixture but never executed, and one (ordering variant `xchg`) is defined and compiled but unreachable from any ordering profile.
- Distinct byte sequences across all entries: **71**.
- Plus 9 instruction-sized slices of the 37-byte `moffs37` program (compiled and executed one at a time), plus the QEMU reuse of the load, stack, xchg_result, lfence, sfence and mfence programs. The control programs are built from only **30 distinct non-jump instruction atoms** (section 1.3, 2.3).

## 1. Summary of distinct instruction forms

Notation: `R8/R16/R32/R64` general registers of that width, `M<n>[...]` an n-bit memory operand with the addressing form in brackets (`disp8`/`disp32` displacements; `moffs32` absolute 32-bit offset via DS), `immN` an N-bit immediate, `xmm` an XMM register. "Prefixes" are in the form text. "#programs" counts program entries (see section 0); the breakdown is long = long-mode data programs, data32 = protected32/compat/PAE data programs, ctl = single-owner control profiles, ctl-conc = concurrent control variants. Forms marked **R** are executed by the resident as privileged/semantic transactions and never handed to FEX in the current suite; the new generator must still decode them to know instruction length and operands, and the new design must decide whether they are generated or kept resident.

### 1.1 Long mode (64-bit code) forms

| Form | Modes in which executed | #programs | Breakdown | Notes |
|---|---|---|---|---|
| `add R64, R64` | long64 | 5 | 5 long | writes CF PF AF ZF SF OF |
| `addps xmm, xmm` | long64 | 2 | 2 long | SSE packed single add; raises MXCSR sticky flags; x87/SSE state |
| `lahf` | long64 | 1 | 1 long | reads SF ZF AF PF CF into AH |
| `lfence` | long64 | 2 | 2 long | LFENCE; live dependencies on prior flag/vector results (fence adapter) |
| `mfence` | long64 | 2 | 2 long | full fence |
| `mov M16[R64+disp8], R16` | long64 | 1 | 1 long |  |
| `mov M32[R64], R32` | long64 | 2 | 2 long |  |
| `mov M32[R64], imm32` | long64 | 6 | 6 long |  |
| `mov M64[R64], R64` | long64 | 1 | 1 long |  |
| `mov R32, M32[R64+disp8]` | long64 | 2 | 2 long |  |
| `mov R32, M32[R64]` | long64 | 6 | 6 long |  |
| `mov R32, imm32` | long64 | 1 | 1 ctl |  |
| `mov R64, M64[R64+disp8]` | long64 | 2 | 2 long |  |
| `mov R64, M64[R64]` | long64 | 18 | 5 long, 9 ctl, 4 ctl-conc | (also present in programs where it is never reached: 4) |
| `movdqu M128[R64+disp8], xmm` | long64 | 1 | 1 long |  |
| `movdqu M128[R64], xmm` | long64 | 4 | 4 long |  |
| `por xmm, xmm` | long64 | 1 | 1 long | SSE2 integer OR; no flags |
| `push imm16 (66 68)` | long64 | 2 | 2 long | RSP -= 2; flags unchanged |
| `push imm32 (sign-extended to 64)` | long64 | 2 | 2 long | RSP -= 8; flags unchanged |
| `pxor xmm, xmm` | long64 | 2 | 2 long | SSE2 integer XOR; no flags |
| `rep movsb  [rdi],[rsi]  (F3 A4)` | long64 | 1 | 1 long | executed by a resident helper (per-element copy with fault retry), not compiled today |
| `sete R8` | long64 | 4 | 4 long | reads ZF |
| `seto R8` | long64 | 1 | 1 long | reads OF |
| `sfence` | long64 | 1 | 1 long | store fence |
| `stmxcsr M32 [R64+disp8]` | long64 | 1 | 1 long | executed by the resident as the fence program's semantic suffix, not compiled today |
| `xchg M32[R64], R32  (implicit LOCK)` | long64 | 3 | 3 long | atomic exchange (implicit LOCK); returns old value in the register |
| `ljmp M16:64 [R64]  (REX.W FF /5)` | long64 | 9 | 5 ctl, 4 ctl-conc | **R**  |
| `ljmp ptr16:32  (EA, #UD when executed in long64)` | long64 | 1 | 1 ctl | **R**  |
| `mov cr0, R64` | long64 | 1 | 1 ctl | **R**  |

### 1.2 32-bit code forms (P32-off, P32-paged, P32-PAE, compat)

| Form | Modes in which executed | #programs | Breakdown | Notes |
|---|---|---|---|---|
| `mov M16[moffs32], R16` | P32-off | 1 | 1 data32 |  |
| `mov M32[R32], R32` | P32-off, P32-paged, P32-PAE, compat | 22 | 6 data32, 12 ctl, 4 ctl-conc |  |
| `mov M32[bx+si], R32  (67h 16-bit addressing)` | P32-off, P32-paged | 2 | 2 data32 |  |
| `mov M32[moffs32], R32` | P32-off | 1 | 1 data32 |  |
| `mov R16, M16[moffs32]` | P32-off | 1 | 1 data32 |  |
| `mov R32, M32[R32]` | P32-off, P32-paged, P32-PAE, compat | 31 | 6 data32, 18 ctl, 7 ctl-conc | (also present in programs where it is never reached: 1) |
| `mov R32, M32[bx+si]  (67h 16-bit addressing)` | P32-off, P32-paged | 2 | 2 data32 |  |
| `mov R32, M32[moffs32]` | P32-off | 1 | 1 data32 |  |
| `mov R32, R32` | P32-off, P32-paged, compat | 7 | 7 data32 |  |
| `mov R32, imm32` | P32-off, P32-paged, P32-PAE, compat | 48 | 2 data32, 39 ctl, 7 ctl-conc |  |
| `invlpg M8 [R32]` | P32-PAE | 1 | 1 ctl | **R**  |
| `jmp rel32  (E9)` | P32-off, P32-PAE | 8 | 3 ctl, 5 ctl-conc | **R**  |
| `ljmp ptr16:32  (EA)` | compat | 17 | 13 ctl, 4 ctl-conc | **R** (also present in programs where it is never reached: 1) |
| `lock mov cr0, R32  (LOCK-prefixed MOV CR: #UD)` | P32-off | 1 | 1 ctl | **R**  |
| `mov cr0, R32` | P32-off, P32-paged, P32-PAE, compat | 41 | 34 ctl, 7 ctl-conc | **R**  |
| `mov cr0, R32 (66-prefixed)` | P32-off | 1 | 1 ctl | **R**  |
| `mov cr1, R32` | P32-off | 1 | 1 ctl | **R**  |
| `mov cr3, R32` | P32-off, P32-paged, P32-PAE | 27 | 22 ctl, 5 ctl-conc | **R**  |
| `mov cr4, R32` | P32-off, P32-paged, P32-PAE, compat | 29 | 25 ctl, 4 ctl-conc | **R**  |
| `wrmsr` | P32-off, compat | 18 | 16 ctl, 2 ctl-conc | **R** EFER only (ECX=0xc0000080); resident-handled |

Mode attribution inside 1.2: data32 programs run in the modes listed in their section (`moffs37` P32-off only; `mixed_*` P32-off and compat; `addr32/addr16` P32-off and P32-paged; `coh_*32` P32-paged and P32-PAE). Control programs execute the `mov R32, imm32` / `89 03` / `8b 03` atoms in every 32-bit mode.

Distinct (mode class, form) rows: 29 for long-mode code and 20 for 32-bit code, 49 in total (this counts prefix variants such as 66h / 67h / LOCK as separate rows).

### 1.3 Observations that bound the subset

- **Compiled-by-FEX scalar atoms in the control programs** are exactly four forms: `mov R32, imm32` (18 distinct immediates: 0x0, 0x11, 0x100, 0x600, 0x620, 0x680, 0x6a0, 0x1000, 0x1003, 0x1020, 0x3000, 0x6000, 0x10011, 0x20010011, 0x80000011, 0x80010010, 0x80010011, 0xffffffff), `mov M32[R32], R32` (`89 03`), `mov R32, M32[R32]` (`8b 03`) and the 64-bit-only `mov R64, M64[R64]` (`48 8b 03`). Each profile is compiled one instruction at a time at its actual RIP (`tools/i4v_controls_worker.py`). All other control atoms (MOV to CR0/CR3/CR4, WRMSR, far jumps, near jump, INVLPG, prefixed variants) are resident semantic transactions. Excluding the two jump kinds, the control programs use 30 distinct instruction atoms; the two jump kinds add the `e9 rel32` and `ea ptr16:32` shapes.
- **Long-mode vector/flag work** is confined to the 17-byte XOR/OR/ALIAS/FP_ALIAS fragments and the 25/29-byte fence fragment (PXOR/POR/ADDPS, ADD, SETcc, LAHF, 16/64-bit and MOVDQU stores, LFENCE, STMXCSR).
- **Memory-ordering** programs add `xchg M32, R32` (implicit LOCK), `mfence`, `sfence`, `lfence` and `mov M32[R64], imm32`.
- **Stack** program adds `push imm32` (sign-extended 8-byte push), `66 push imm16` (two-byte push) and two loads relative to RDX with a negative disp8 (`[rdx-8]` 64-bit, `[rdx-12]` 32-bit).
- **Segmentation/addressing** forms (P32 modes): moffs `A1` / `66 A1` absolute through DS, 16-bit addressing through `67h` (`[bx+si]`), plus segment base/limit/A20 effects carried by the resident, not by the instruction bytes.
- **Branches**: the only control-flow instructions are the resident-handled `jmp rel32` (control profiles 0, 21, 24 and their concurrent variants) and `ljmp` (`ea ptr16:32` in profiles using far transfers, `48 ff 2e` ljmp m16:64). There are no conditional branches, calls, returns, x87, AVX, integer multiply/divide, or string operations other than `rep movsb`.
- **Flags**: only `add` (long-mode fragments) writes arithmetic flags in a program the generator must translate; control programs and ordering/stack programs leave flags unchanged (MOV, PUSH, XCHG, fences). MOV CR is assumed to leave arithmetic flags unchanged by emulator policy (`tools/i4v_controls_check.py` docstring).

## 2. Programs

### 2.0 Index of program entries

| Id | Name | Bytes | Mode(s) | Hex |
|---|---|---|---|---|
| L01 | `xor17` | 17 | long64 | `660fefc14801d1488b030f94c2f30f7f07` |
| L02 | `or17` | 17 | long64 | `660febc14801d1488b030f94c2f30f7f07` |
| L03 | `alias17` | 17 | long64 | `660fefc14801d1488b000f94c2f30f7f07` |
| L04 | `fp_alias17` | 16 | long64 | `0f58c14801d1488b000f94c2f30f7f07` |
| L05 | `load3` | 3 | long64 | `488b03` |
| L06 | `stack20` | 20 | long64 | `68bbbcbdbe6668525166686261488b42f88b5af4` |
| L07 | `stack20b` | 20 | long64 | `68bbbcbdbe6668535166686261488b42f88b5af4` |
| L08 | `repmovsb` | 2 | long64 | `f3a4` |
| L09 | `fence29` | 29 | long64 | `0f58c14801d10faee89f0f90c048890f66894708f30f7f47100fae5f20` |
| L10 | `ord_sb` | 8 | long64 | `c703010000008b02` |
| L11 | `ord_mp_writer` | 12 | long64 | `c70301000000c70201000000` |
| L12 | `ord_reader` | 4 | long64 | `8b038b0a` |
| L13 | `ord_wrc_middle` | 8 | long64 | `8b03c70201000000` |
| L14 | `ord_writer` | 6 | long64 | `c70301000000` |
| L15 | `ord_sb_mfence` | 11 | long64 | `c703010000000faef08b02` |
| L16 | `ord_sb_xchg` | 10 | long64 | `c7030100000087068b02` |
| L17 | `ord_xchg_result` | 4 | long64 | `87038902` |
| L18 | `ord_lfence` | 3 | long64 | `0faee8` |
| L19 | `ord_sfence` | 3 | long64 | `0faef8` |
| L20 | `ord_mfence` | 3 | long64 | `0faef0` |
| L21 | `ord_xchg` | 2 | long64 | `8703` |
| L22 | `coh_store64` | 2 | long64 | `8903` |
| L23 | `coh_load64` | 2 | long64 | `8b03` |
| X01 | `moffs37` | 37 | P32-off | `ba00001000b8420000008902b8ffffffffa10000100089c2b8ffffffff66a10000100089c1` |
| X02 | `moffs_store32` | 35 | P32-off | `b842000000a300001000ba000010008b12b84300ffff66a300001000b9000010008b09` |
| X03 | `mixed_old` | 6 | P32-off, compat | `89c1890b8b02` |
| X04 | `mixed_new` | 6 | P32-off, compat | `89c189138b02` |
| X05 | `addr32` | 6 | P32-off, P32-paged | `89c1890b8b02` |
| X06 | `addr32b` | 6 | P32-off, P32-paged | `89c189138b02` |
| X07 | `addr16` | 8 | P32-off, P32-paged | `89c1678908678b00` |
| X08 | `addr16b` | 8 | P32-off, P32-paged | `89c1678910678b00` |
| X09 | `coh_store32` | 2 | P32-paged, P32-PAE | `8903` |
| X10 | `coh_load32` | 2 | P32-paged, P32-PAE | `8b03` |
| C01 | `ctl00` | 52 | P32-off, P32-paged | `b8110001008903b8110001800f22c08b03b8003000000f22d88b03b8001000000f22d88b03b8110001000f22c08b03e9ccffffff` |
| C02 | `ctl01` | 20 | P32-off, P32-paged | `b8110001800f22c08b03b8001000000f22d88b03` |
| C03 | `ctl02` | 30 | P32-off, P32-paged | `b8110000800f22c0b8110001008903b8110001800f22c0b8ffffffff8903` |
| C04 | `ctl03` | 15 | P32-off | `b8110001008903b8100001800f22c0` |
| C05 | `ctl04` | 15 | P32-off | `b8110001008903b8110001200f22c0` |
| C06 | `ctl05` | 15 | P32-off | `b8110001008903b8ffffffff0f22e0` |
| C07 | `ctl06` | 15 | P32-off | `b8110001008903b8110001800f22c0` |
| C08 | `ctl07` | 25 | P32-off, P32-paged | `b8110001008903b8110001800f22c0b8003000000f22d88b03` |
| C09 | `ctl08` | 18 | P32-off, P32-paged | `b8031000000f22d8b8110001800f22c08b03` |
| C10 | `ctl09` | 11 | P32-off, P32-paged | `b811000180660f22008b03` |
| C11 | `ctl10` | 16 | P32-off | `b8110001008903b811000180f00f22c0` |
| C12 | `ctl11` | 15 | P32-off | `b8110001008903b8110001800f22c8` |
| C13 | `ctl12` | 30 | P32-off, P32-paged | `b8110001800f22c08b03b8800600000f22e08b03b8000600000f22e08b03` |
| C14 | `ctl13` | 18 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08b03` |
| C15 | `ctl14` | 33 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08b030f013b8b03b8201000000f22d88b03` |
| C16 | `ctl15` | 30 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08b038b03b8201000000f22d88b03` |
| C17 | `ctl16` | 38 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08b03b8110000800f22c08b03b8a00600000f22e08b03` |
| C18 | `ctl17` | 23 | P32-off | `b8110001008903b8200600000f22e0b8110001800f22c0` |
| C19 | `ctl18` | 35 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08b03b8110001008903b8201000000f22d88b03` |
| C20 | `ctl19` | 35 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08b03b8110001008903b8a00600000f22e08b03` |
| C21 | `ctl20` | 18 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08b03` |
| C22 | `ctl21` | 41 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08b03b8201000000f22d88b03b8201000000f22d8e9e7ffffff` |
| C23 | `ctl22` | 56 | P32-off, compat, long64 | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e8b03b8110001000f22c08b03` |
| C24 | `ctl23` | 74 | P32-off, P32-PAE, compat, long64 | `b8200600000f22e0b8110001800f22c08b03b8110001000f22c0b8000100000f30b8006000000f22d8b8110001800f22c0ea382010001800488b0348ff2e8b03b8110001000f22c08b03` |
| C25 | `ctl24` | 38 | P32-off, compat, long64 | `8b03b8110001800f22c0ea112010001800488b0348ff2e8b03b8110001000f22c0e9daffffff` |
| C26 | `ctl25` | 38 | P32-off, compat | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0b8000000000f30` |
| C27 | `ctl26` | 15 | P32-off | `b8000100000f30b8110001800f22c0` |
| C28 | `ctl27` | 39 | P32-off, compat | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0b8000600000f22e0` |
| C29 | `ctl28` | 46 | P32-off, compat, long64 | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800b8110001000f22c0` |
| C30 | `ctl29` | 7 | P32-off | `b8ffffffff0f30` |
| C31 | `ctl30` | 45 | P32-off, compat, long64 | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800ea2d2010001800` |
| C32 | `ctl31` | 38 | P32-off, compat | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010003800` |
| C33 | `ctl32` | 41 | P32-off, compat | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b03` |
| C34 | `ctl33` | 41 | P32-off, compat | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b03` |
| C35 | `ctl34` | 46 | P32-off, compat, long64 | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e8b03` |
| C36 | `ctl35` | 41 | P32-off, compat, long64 | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b03` |
| C37 | `ctl36` | 41 | P32-off | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b03` |
| C38 | `ctl37` | 38 | P32-off, compat | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001000` |
| C39 | `ctl38` | 47 | P32-off, compat, long64 | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e488b03` |
| K01 | `conc00g2` | 52 | P32-off, P32-paged | `b8110000008903b8110001800f22c08b03b8003000000f22d88b03b8001000000f22d88b03b8110001000f22c08b03e9ccffffff` |
| K02 | `conc21g1` | 56 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08b03b8201000000f22d88b03b800000000b800010000b820100000b8201000000f22d8e9d8ffffff` |
| K03 | `conc21g2` | 56 | P32-off, P32-PAE | `b8200600000f22e0b8110001800f22c08903b8201000000f22d88b03b800000000b800010000b820100000b8201000000f22d8e9d8ffffff` |
| K04 | `conc22g1` | 71 | P32-off, compat, long64 | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e8b03b800000000b800010000b811000100b8110001000f22c08b03` |
| K05 | `conc22g2` | 71 | P32-off, compat, long64 | `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e8b03b800000000b800010000b811000100b8110001000f22c08903` |
| K06 | `conc24g1` | 43 | P32-off, compat, long64 | `8b03b8110001800f22c0ea112010001800488b0348ff2e8b03b800000000b8110001000f22c0e9d5ffffff` |
| K07 | `conc24g2` | 43 | P32-off, compat, long64 | `8903b8110001800f22c0ea112010001800488b0348ff2e8b03b800000000b8110001000f22c0e9d5ffffff` |

### 2.1 Long-mode programs (64-bit code)

#### L01. XOR fragment (PXOR/ADD/MOV load/SETZ/MOVDQU)

- **Key**: `xor17`  |  **Bytes**: 17  |  **Modes**: long64
- **Hex**: `660fefc14801d1488b030f94c2f30f7f07`
- **Defined at**:
  - `crates/abi/src/translation.rs:7-9 (XOR)`
  - `tools/check_i2.py:29`
  - `tools/check_i4.py:32`
  - `tools/check_i3_paging.py:34`
  - `tools/fex_worker.py:16`
  - `fixtures/i1/live_load.json (x86_hex)`
  - `tools/i4v_producer/export.py:180 (fex_alias_xor64)`
  - `tools/i4v_producer/testdata/xor-low, xor-high (source.x86)`
  - `crates/host/src/concurrent.rs:656-665 (compose writes wire::XOR at recovery::SOURCE_OFFSET)`
- **Run by**: I1 live_load; I2 live/source_stale/context_stale/cancel/capacity/bad_reply; I3 paging (12 scenarios cold, load_np, store_ro, parent_ro, wp_off, user_denied(CPL3), reserved, fetch_nx, smep, carry, overflow, large); I4 compose/events/pressure and alias-low/high (source bytes written to guest memory at recovery::SOURCE_OFFSET); I4 entry/faults/remap only run the pinned native body whose x86 provenance is this fragment (guest RIP 0x102000 and 0xffff800000102000, FEX bitness 64).
- **Notes**: Load address register is RBX; vector store address register is RDI. ADD writes CF/PF/AF/ZF/SF/OF, SETZ reads ZF, MOVDQU stores 16 bytes (unaligned) to [rdi]. Bytes 0-16 are the whole program.

```
off   bytes                     decoded                                        | executed-in / note
   0  660fefc1                  pxor xmm0,xmm1                                  | long64
   4  4801d1                    add rcx,rdx                                     | long64
   7  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  10  0f94c2                    sete dl                                         | long64
  13  f30f7f07                  movdqu XMMWORD PTR [rdi],xmm0                   | long64
```

#### L02. OR fragment (POR in place of PXOR)

- **Key**: `or17`  |  **Bytes**: 17  |  **Modes**: long64
- **Hex**: `660febc14801d1488b030f94c2f30f7f07`
- **Defined at**:
  - `crates/abi/src/translation.rs:10-13 (OR; derived from XOR by patching byte 2 to 0xeb)`
  - `tools/check_i2.py:30`
  - `tools/check_i4.py:33`
  - `tools/fex_worker.py:17`
  - `tools/i4v_producer/export.py:185 (fex_alias_or64)`
  - `tools/i4v_producer/testdata/or-low, or-high`
- **Run by**: I2 source-change (host sends wire::OR through SOURCE command); I4 source rewrite at runtime/src/concurrent/source.rs:137; I4-V alias-low/high.
- **Notes**: Differs from XOR only in opcode byte 2 (ef -> eb).

```
off   bytes                     decoded                                        | executed-in / note
   0  660febc1                  por xmm0,xmm1                                   | long64
   4  4801d1                    add rcx,rdx                                     | long64
   7  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  10  0f94c2                    sete dl                                         | long64
  13  f30f7f07                  movdqu XMMWORD PTR [rdi],xmm0                   | long64
```

#### L03. ALIAS fragment (XOR with load through [rax])

- **Key**: `alias17`  |  **Bytes**: 17  |  **Modes**: long64
- **Hex**: `660fefc14801d1488b000f94c2f30f7f07`
- **Defined at**:
  - `crates/abi/src/pressure.rs:15-18 (ALIAS, XOR with byte 9 = 0)`
  - `tools/check_i3_pressure.py:25`
  - `tools/i3_producer/parser.py:18`
  - `tools/i3_producer/testdata/alias.x86`
- **Run by**: I3 pressure and helper profiles (i3-pressure, i3-helper, and the prefix of i3-partial): scenarios cold, load_np, store_ro.
- **Notes**: Load address register is RAX (the loaded value overwrites it); store address register is RDI.

```
off   bytes                     decoded                                        | executed-in / note
   0  660fefc1                  pxor xmm0,xmm1                                  | long64
   4  4801d1                    add rcx,rdx                                     | long64
   7  488b00                    mov rax,QWORD PTR [rax]                         | long64
  10  0f94c2                    sete dl                                         | long64
  13  f30f7f07                  movdqu XMMWORD PTR [rdi],xmm0                   | long64
```

#### L04. FP_ALIAS fragment (ADDPS in place of PXOR, load through [rax])

- **Key**: `fp_alias17`  |  **Bytes**: 16  |  **Modes**: long64
- **Hex**: `0f58c14801d1488b000f94c2f30f7f07`
- **Defined at**:
  - `crates/abi/src/pressure.rs:18-20 (FP_ALIAS, 16 bytes; lifecycle packet length 16)`
  - `tools/check_i3_pressure.py:26`
  - `tools/i3_producer/parser.py:19`
  - `tools/i3_producer/testdata/fp_alias.x86`
- **Run by**: I3 i3-fp profile: cold, load_np, store_ro.
- **Notes**: 16 bytes (no 66 prefix). ADDPS reads/writes MXCSR exception flags.

```
off   bytes                     decoded                                        | executed-in / note
   0  0f58c1                    addps xmm0,xmm1                                 | long64
   3  4801d1                    add rcx,rdx                                     | long64
   6  488b00                    mov rax,QWORD PTR [rax]                         | long64
   9  0f94c2                    sete dl                                         | long64
  12  f30f7f07                  movdqu XMMWORD PTR [rdi],xmm0                   | long64
```

#### L05. Bare 64-bit load

- **Key**: `load3`  |  **Bytes**: 3  |  **Modes**: long64
- **Hex**: `488b03`
- **Defined at**:
  - `crates/abi/src/validation.rs:4 (SOURCE)`
  - `crates/abi/src/containment.rs:28 (PARTIAL_SOURCE)`
  - `crates/host/src/i4v.rs:92`
  - `tools/i4v_worker.py:13`
  - `tools/i4v_producer/export.py:190 (fex_memory_data64)`
  - `fixtures/i4v/cases.json:18 and fixtures/i4v/fex_memory_data64.s`
  - `tools/i4v_qemu/model.py:6 (QEMU case load)`
  - `tools/i4v_producer/testdata/load-high`
- **Run by**: I4-V live/load scenarios (cold, load_np, user_denied(CPL3), fetch_nx, smep, low_operand, noncanonical, noncanonical_operand, private_operand, noncanonical_rip) at RIP 0x102000 and upper-canonical 0xffff800000102000; containment/partial-progress hold it at source offset +16 of the actor (crates/host/src/concurrent/partial_progress.rs:146); QEMU scalar comparison.
- **Notes**: Also appears (as 64-bit-only MOV, LOAD64) inside the control profiles that reach long mode (22, 23, 24, 32-36, 38). Same bytes in 32-bit mode would decode as DEC EAX / MOV EBX..., so these must always be decoded as 64-bit.

```
off   bytes                     decoded                                        | executed-in / note
   0  488b03                    mov rax,QWORD PTR [rbx]                         | long64
```

#### L06. Stack program (PUSH imm32, PUSH imm16 x2, two loads)

- **Key**: `stack20`  |  **Bytes**: 20  |  **Modes**: long64
- **Hex**: `68bbbcbdbe6668525166686261488b42f88b5af4`
- **Defined at**:
  - `crates/abi/src/stack.rs:11-14 (SOURCE), stack.rs:28-33 source(0)`
  - `tools/i4v_stack_check.py:25`
  - `tools/i4v_stack_worker.py:13`
  - `tools/i4v_producer/export.py:193 (fex_push_immediates64)`
  - `fixtures/i4v/cases.json:68 and fixtures/i4v/fex_push_immediates64.s`
  - `tools/i4v_qemu/model.py:6 (QEMU case stack)`
  - `host writers: crates/host/src/i4v_stack.rs:119, crates/host/src/concurrent.rs:660-665`
  - `tools/i4v_producer/testdata/push-high`
- **Run by**: I4-V stack-live scenarios (cold, low_stack, ro_first, ro_after_first, np_after_first, wp_off, user_ro, user_denied, user_ok, parent_ro, fetch_nx, smep, ss_first, ss_after_first, ss_span, gp_after_pushes, read_np_after_pushes); concurrent stack-low/high, stack-parent-*, stack-leaf-*; stack-boundary (ss, gp, split, split-np, split-ro) at low/high RIP; scheduler family "stack"; QEMU.
- **Notes**: push imm32 sign-extends to a 64-bit push (8 bytes); 66-prefixed pushes are 2 bytes. RSP is decremented by 8, 2, 2. Does not touch flags.

```
off   bytes                     decoded                                        | executed-in / note
   0  68bbbcbdbe                push 0xffffffffbebdbcbb                         | long64
   5  66685251                  pushw 0x5152                                    | long64
   9  66686261                  pushw 0x6162                                    | long64
  13  488b42f8                  mov rax,QWORD PTR [rdx-0x8]                     | long64
  17  8b5af4                    mov ebx,DWORD PTR [rdx-0xc]                     | long64
```

#### L07. Stack program, replacement generation (second PUSH imm16 = 0x5153)

- **Key**: `stack20b`  |  **Bytes**: 20  |  **Modes**: long64
- **Hex**: `68bbbcbdbe6668535166686261488b42f88b5af4`
- **Defined at**:
  - `crates/abi/src/stack.rs:28-33 source(1) (byte 7 += 1)`
  - `tools/check_i4v_stack_concurrent.py:27 (NEW_SOURCE)`
  - `tools/i4v_producer/export.py:198 (fex_push_immediates64_changed)`
  - `tools/i4v_producer/testdata/replacement-low, replacement-high`
  - `runtime/src/concurrent/stack_source.rs:126 rewrite_source(&p::source(1))`
- **Run by**: Concurrent stack profiles rewrite the live source to this generation (self-modifying-code / coherence case).
- **Notes**: Differs from stack20 in byte 7 only (0x52 -> 0x53).

```
off   bytes                     decoded                                        | executed-in / note
   0  68bbbcbdbe                push 0xffffffffbebdbcbb                         | long64
   5  66685351                  pushw 0x5153                                    | long64
   9  66686261                  pushw 0x6162                                    | long64
  13  488b42f8                  mov rax,QWORD PTR [rdx-0x8]                     | long64
  17  8b5af4                    mov ebx,DWORD PTR [rdx-0xc]                     | long64
```

#### L08. REP MOVSB (resident-helper fixture; not compiled by the code generator)

- **Key**: `repmovsb`  |  **Bytes**: 2  |  **Modes**: long64
- **Hex**: `f3a4`
- **Defined at**:
  - `crates/abi/src/partial_progress.rs:15 (SOURCE)`
  - `crates/host/src/translation.rs:332 (SOURCE_OFFSET+256, i3-partial)`
  - `crates/host/src/concurrent/partial_progress.rs:140 (SOURCE at id*256)`
  - `tools/check_i4v_partial_progress.py:40`
- **Run by**: I3 i3-partial (after the ALIAS prefix, RSI=device page, RDI=data+4095, RCX=2); I4-V partial-progress (per owner id*256) and scheduler family "partial"/containment partial gate.
- **Notes**: Runtime executes it through a native helper (runtime/src/partial.rs:run, partial_progress.rs) that checks the two bytes literally (ptr::read_volatile(...) != 0xa4f3) and performs per-element byte copies. REP count 2, DF=0 required.

```
off   bytes                     decoded                                        | executed-in / note
   0  f3a4                      rep movsb   (F3 A4: movs BYTE PTR [rdi],[rsi])  | long64
```

#### L09. LFENCE live-dependency program (ADDPS/ADD/LFENCE/LAHF/SETO/stores/STMXCSR)

- **Key**: `fence29`  |  **Bytes**: 29  |  **Modes**: long64
- **Hex**: `0f58c14801d10faee89f0f90c048890f66894708f30f7f47100fae5f20`
- **Defined at**:
  - `crates/abi/src/fence.rs:21-24 (SOURCE, 25 bytes) and fence.rs:34-39 source() (29 bytes = +STMXCSR)`
  - `crates/host/src/concurrent/fence.rs:123`
  - `tools/check_i4v_fence.py:23-24 (SOURCE + SEMANTIC_SUFFIX)`
  - `tools/i4v_producer/export.py:12 (fex_lfence_dependencies, 25 bytes)`
  - `tools/i4v_producer/probe_lfence.py:13`
  - `tools/i4v_producer/testdata/lfence-0, lfence-1 (RIP 0x102000 and 0x102100)`
- **Run by**: I4-V fence (5 arithmetic cases x N=1..4 owners x 16 trials). FEX compiles only the first 25 bytes (8 instructions); the trailing STMXCSR is a resident semantic suffix.
- **Notes**: The code generator must handle: ADDPS (MXCSR update), ADD (flags), LFENCE as a live-dependency fence, LAHF, SETO, 64/16-bit stores, MOVDQU store. STMXCSR is executed by the resident, not compiled.

```
off   bytes                     decoded                                        | executed-in / note
   0  0f58c1                    addps xmm0,xmm1                                 | long64
   3  4801d1                    add rcx,rdx                                     | long64
   6  0faee8                    lfence                                          | long64
   9  9f                        lahf                                            | long64
  10  0f90c0                    seto al                                         | long64
  13  48890f                    mov QWORD PTR [rdi],rcx                         | long64
  16  66894708                  mov WORD PTR [rdi+0x8],ax                       | long64
  20  f30f7f4710                movdqu XMMWORD PTR [rdi+0x10],xmm0              | long64
  25  0fae5f20                  stmxcsr DWORD PTR [rdi+0x20]                    | long64
```

#### L10. Ordering participant: sb

- **Key**: `ord_sb`  |  **Bytes**: 8  |  **Modes**: long64
- **Hex**: `c703010000008b02`
- **Defined at**:
  - `crates/abi/src/ordering.rs:348 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_sb`
  - `fixtures/i4v/ordering/cases.json:32 (source_hex)`
  - `fixtures/i4v/ordering/sb.s`
  - `tools/i4v_producer/testdata/order-sb`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
- **Run by**: ordering profile 0 "sb" (store buffering, N=2, both owners). Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.

```
off   bytes                     decoded                                        | executed-in / note
   0  c70301000000              mov DWORD PTR [rbx],0x1                         | long64
   6  8b02                      mov eax,DWORD PTR [rdx]                         | long64
```

#### L11. Ordering participant: mp_writer

- **Key**: `ord_mp_writer`  |  **Bytes**: 12  |  **Modes**: long64
- **Hex**: `c70301000000c70201000000`
- **Defined at**:
  - `crates/abi/src/ordering.rs:349-351 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_mp_writer`
  - `fixtures/i4v/ordering/cases.json:42 (source_hex)`
  - `fixtures/i4v/ordering/mp_writer.s`
  - `tools/i4v_producer/testdata/order-mp_writer`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
- **Run by**: profile 1 "mp" (message passing, role 0). Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.

```
off   bytes                     decoded                                        | executed-in / note
   0  c70301000000              mov DWORD PTR [rbx],0x1                         | long64
   6  c70201000000              mov DWORD PTR [rdx],0x1                         | long64
```

#### L12. Ordering participant: reader

- **Key**: `ord_reader`  |  **Bytes**: 4  |  **Modes**: long64
- **Hex**: `8b038b0a`
- **Defined at**:
  - `crates/abi/src/ordering.rs:352 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_reader`
  - `fixtures/i4v/ordering/cases.json:52 (source_hex)`
  - `fixtures/i4v/ordering/reader.s`
  - `tools/i4v_producer/testdata/order-reader`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
- **Run by**: profile 1 "mp" (roles > 0), profile 3 "wrc" (role 2), profile 4 "iriw" (roles 2 and 3). Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.

```
off   bytes                     decoded                                        | executed-in / note
   0  8b03                      mov eax,DWORD PTR [rbx]                         | long64
   2  8b0a                      mov ecx,DWORD PTR [rdx]                         | long64
```

#### L13. Ordering participant: wrc_middle

- **Key**: `ord_wrc_middle`  |  **Bytes**: 8  |  **Modes**: long64
- **Hex**: `8b03c70201000000`
- **Defined at**:
  - `crates/abi/src/ordering.rs:353 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_wrc_middle`
  - `fixtures/i4v/ordering/cases.json:62 (source_hex)`
  - `fixtures/i4v/ordering/wrc_middle.s`
  - `tools/i4v_producer/testdata/order-wrc_middle`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
- **Run by**: profile 2 "lb" (load buffering; both owners) and profile 3 "wrc" (role 1). Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.

```
off   bytes                     decoded                                        | executed-in / note
   0  8b03                      mov eax,DWORD PTR [rbx]                         | long64
   2  c70201000000              mov DWORD PTR [rdx],0x1                         | long64
```

#### L14. Ordering participant: writer

- **Key**: `ord_writer`  |  **Bytes**: 6  |  **Modes**: long64
- **Hex**: `c70301000000`
- **Defined at**:
  - `crates/abi/src/ordering.rs:354 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_writer`
  - `fixtures/i4v/ordering/cases.json:72 (source_hex)`
  - `fixtures/i4v/ordering/writer.s`
  - `tools/i4v_producer/testdata/order-writer`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
- **Run by**: profile 3 "wrc" (role 0) and profile 4 "iriw" (roles 0 and 1). Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.

```
off   bytes                     decoded                                        | executed-in / note
   0  c70301000000              mov DWORD PTR [rbx],0x1                         | long64
```

#### L15. Ordering participant: sb_mfence

- **Key**: `ord_sb_mfence`  |  **Bytes**: 11  |  **Modes**: long64
- **Hex**: `c703010000000faef08b02`
- **Defined at**:
  - `crates/abi/src/ordering.rs:355-357 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_sb_mfence`
  - `fixtures/i4v/ordering/cases.json:82 (source_hex)`
  - `fixtures/i4v/ordering/sb_mfence.s`
  - `tools/i4v_producer/testdata/order-sb_mfence`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
- **Run by**: profile 5 "sb-mfence". Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.

```
off   bytes                     decoded                                        | executed-in / note
   0  c70301000000              mov DWORD PTR [rbx],0x1                         | long64
   6  0faef0                    mfence                                          | long64
   9  8b02                      mov eax,DWORD PTR [rdx]                         | long64
```

#### L16. Ordering participant: sb_xchg

- **Key**: `ord_sb_xchg`  |  **Bytes**: 10  |  **Modes**: long64
- **Hex**: `c7030100000087068b02`
- **Defined at**:
  - `crates/abi/src/ordering.rs:358 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_sb_xchg`
  - `fixtures/i4v/ordering/cases.json:92 (source_hex)`
  - `fixtures/i4v/ordering/sb_xchg.s`
  - `tools/i4v_producer/testdata/order-sb_xchg`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
- **Run by**: profile 6 "sb-xchg". Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.
- **Notes**: XCHG with a memory operand is implicitly LOCKed and returns the old value in the register.

```
off   bytes                     decoded                                        | executed-in / note
   0  c70301000000              mov DWORD PTR [rbx],0x1                         | long64
   6  8706                      xchg DWORD PTR [rsi],eax                        | long64
   8  8b02                      mov eax,DWORD PTR [rdx]                         | long64
```

#### L17. Ordering participant: xchg_result

- **Key**: `ord_xchg_result`  |  **Bytes**: 4  |  **Modes**: long64
- **Hex**: `87038902`
- **Defined at**:
  - `crates/abi/src/ordering.rs:359 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_xchg_result`
  - `fixtures/i4v/ordering/cases.json:102 (source_hex)`
  - `fixtures/i4v/ordering/xchg_result.s`
  - `tools/i4v_producer/testdata/order-xchg_result`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
  - `tools/i4v_qemu/model.py:6-7 (QEMU SOURCES)`
- **Run by**: profiles 7 "xchg", 8 "xchg-pre" and 9 "xchg-post" (8 and 9 inject a fault around the atomic); atomic-progress scheduler family; QEMU case xchg. Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.
- **Notes**: XCHG with a memory operand is implicitly LOCKed and returns the old value in the register.

```
off   bytes                     decoded                                        | executed-in / note
   0  8703                      xchg DWORD PTR [rbx],eax                        | long64
   2  8902                      mov DWORD PTR [rdx],eax                         | long64
```

#### L18. Ordering participant: lfence

- **Key**: `ord_lfence`  |  **Bytes**: 3  |  **Modes**: long64
- **Hex**: `0faee8`
- **Defined at**:
  - `crates/abi/src/ordering.rs:360 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_lfence`
  - `fixtures/i4v/ordering/cases.json:112 (source_hex)`
  - `fixtures/i4v/ordering/lfence.s`
  - `tools/i4v_producer/testdata/order-lfence`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
  - `tools/i4v_qemu/model.py:6-7 (QEMU SOURCES)`
- **Run by**: profile 10 "lfence-control"; QEMU case lfence. Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.

```
off   bytes                     decoded                                        | executed-in / note
   0  0faee8                    lfence                                          | long64
```

#### L19. Ordering participant: sfence

- **Key**: `ord_sfence`  |  **Bytes**: 3  |  **Modes**: long64
- **Hex**: `0faef8`
- **Defined at**:
  - `crates/abi/src/ordering.rs:361 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_sfence`
  - `fixtures/i4v/ordering/cases.json:122 (source_hex)`
  - `fixtures/i4v/ordering/sfence.s`
  - `tools/i4v_producer/testdata/order-sfence`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
  - `tools/i4v_qemu/model.py:6-7 (QEMU SOURCES)`
- **Run by**: profile 11 "sfence-control"; QEMU case sfence. Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.

```
off   bytes                     decoded                                        | executed-in / note
   0  0faef8                    sfence                                          | long64
```

#### L20. Ordering participant: mfence

- **Key**: `ord_mfence`  |  **Bytes**: 3  |  **Modes**: long64
- **Hex**: `0faef0`
- **Defined at**:
  - `crates/abi/src/ordering.rs:362 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_mfence`
  - `fixtures/i4v/ordering/cases.json:132 (source_hex)`
  - `fixtures/i4v/ordering/mfence.s`
  - `tools/i4v_producer/testdata/order-mfence`
  - `tools/check_i4v_ordering.py:27-30 (SOURCES tuple)`
  - `tools/i4v_qemu/model.py:6-7 (QEMU SOURCES)`
- **Run by**: profile 12 "mfence-control"; QEMU case mfence. Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.

```
off   bytes                     decoded                                        | executed-in / note
   0  0faef0                    mfence                                          | long64
```

#### L21. Ordering participant: xchg

- **Key**: `ord_xchg`  |  **Bytes**: 2  |  **Modes**: long64
- **Hex**: `8703`
- **Defined at**:
  - `crates/abi/src/ordering.rs:363 (source(variant))`
  - `tools/i4v_producer/export.py CASES fex_order_xchg`
  - `fixtures/i4v/ordering/cases.json:142 (source_hex)`
  - `fixtures/i4v/ordering/xchg.s`
  - `tools/i4v_producer/testdata/order-xchg`
- **Run by**: NOT reachable from any ordering profile (variant 11): defined in ordering.rs source()/body()/image(), fixtures/i4v/ordering/xchg.s and compiled as a probe artifact only. Placed at RIP 0x102000 + owner*256 (crates/host/src/concurrent/ordering.rs:223-224); RBX, RDX and RSI hold full-width guest addresses (fixtures/i4v/ordering/README.md); 32-bit data, 64-bit decode.
- **Notes**: XCHG with a memory operand is implicitly LOCKed and returns the old value in the register.

```
off   bytes                     decoded                                        | executed-in / note
   0  8703                      xchg DWORD PTR [rbx],eax                        | long64
```

#### L22. Coherence store [rbx] <- eax (long-mode profiles)

- **Key**: `coh_store64`  |  **Bytes**: 2  |  **Modes**: long64
- **Hex**: `8903`
- **Defined at**:
  - `crates/abi/src/coherence.rs:234-238 (source(STORE))`
  - `tools/check_i4v_coherence.py:26`
  - `tools/i4v_producer/export.py:130 (fex_store_eax_rbx64)`
  - `tools/i4v_producer/testdata/coherence-store-0/-1 (RIP 0x102000, 0x102310)`
- **Run by**: I4-V coherence profiles 0, 3, 5 (4-level tables), owner source at 0x102000 + owner*256 + kind*16; scheduler family "coherence".
- **Notes**: Same bytes as the 32-bit coherence store; here decoded in 64-bit mode (address register RBX).

```
off   bytes                     decoded                                        | executed-in / note
   0  8903                      mov DWORD PTR [rbx],eax                         | long64
```

#### L23. Coherence load eax <- [rbx] (long-mode profiles)

- **Key**: `coh_load64`  |  **Bytes**: 2  |  **Modes**: long64
- **Hex**: `8b03`
- **Defined at**:
  - `crates/abi/src/coherence.rs:234-238 (source(LOAD))`
  - `tools/check_i4v_coherence.py:26`
  - `tools/i4v_producer/export.py:133 (fex_load_eax_rbx64)`
  - `tools/i4v_producer/testdata/coherence-load-0/-1`
- **Run by**: I4-V coherence profiles 0, 3, 5.

```
off   bytes                     decoded                                        | executed-in / note
   0  8b03                      mov eax,DWORD PTR [rbx]                         | long64
```

### 2.2 Protected32 / compatibility / PAE data-path programs

#### X01. Protected32 moffs load program (9 instructions, 37 bytes)

- **Key**: `moffs37`  |  **Bytes**: 37  |  **Modes**: P32-off
- **Hex**: `ba00001000b8420000008902b8ffffffffa10000100089c2b8ffffffff66a10000100089c1`
- **Defined at**:
  - `crates/abi/src/paging_off32.rs:8-12 (SOURCE)`
  - `crates/host/src/i4v_off.rs:44-48 and i4v_off_steps.rs:55 (host writers)`
  - `tools/i4v_off_check.py:25`
  - `tools/i4v_producer/export.py:172 (fex_moffs_load32)`
  - `fixtures/i4v/cases.json:156 and fixtures/i4v/fex_moffs_load32.s`
  - `tools/i4v_producer/testdata/load32`
- **Run by**: i4v-off-live scenarios cold/readonly/warm at guest EIP/RIP 0x102000 and 0x102080 (flat CS, A20 on); i4v-off-steps scenarios segments, a20-off, ds-limit, cs-before-load, cs-inside-load, cs-first, ds-readonly, device, hole, linear-wrap (EIP 0x2000, CS base 0x300000 etc., one instruction compiled per step)
- **Notes**: Step slices (offsets 0,5,10,12,17,22,24,29,35,37) are compiled as separate one-instruction FEX cases fex_moffs_load32_step0..8 (export.py:148-171; step6 reuses step3). Segment-relative: moffs and [edx] go through DS.

```
off   bytes                     decoded                                        | executed-in / note
   0  ba00001000                mov edx,0x100000                                | P32-off
   5  b842000000                mov eax,0x42                                    | P32-off
  10  8902                      mov DWORD PTR [edx],eax                         | P32-off
  12  b8ffffffff                mov eax,0xffffffff                              | P32-off
  17  a100001000                mov eax,ds:0x100000                             | P32-off
  22  89c2                      mov edx,eax                                     | P32-off
  24  b8ffffffff                mov eax,0xffffffff                              | P32-off
  29  66a100001000              mov ax,ds:0x100000                              | P32-off
  35  89c1                      mov ecx,eax                                     | P32-off
```

#### X02. Protected32 moffs store program (defined by fixture only; not executed)

- **Key**: `moffs_store32`  |  **Bytes**: 35  |  **Modes**: P32-off
- **Hex**: `b842000000a300001000ba000010008b12b84300ffff66a300001000b9000010008b09`
- **Defined at**:
  - `fixtures/i4v/cases.json:233 (x86_hex)`
  - `fixtures/i4v/fex_moffs_store32.s`
  - `fixtures/i4v/expectations.py (self-test only)`
- **Run by**: No runner, worker, producer CASES entry or Rust constant uses it; only fixtures/i4v/test_expectations.py checks the expectation helper.
- **Notes**: Included for completeness; the new generator does not need it unless the owner wants it exercised. Contains MOV moffs32,EAX (A3) and 66 A3 (MOV moffs32,AX).

```
off   bytes                     decoded                                        | executed-in / note
   0  b842000000                mov eax,0x42                                    | P32-off
   5  a300001000                mov ds:0x100000,eax                             | P32-off
  10  ba00001000                mov edx,0x100000                                | P32-off
  15  8b12                      mov edx,DWORD PTR [edx]                         | P32-off
  17  b84300ffff                mov eax,0xffff0043                              | P32-off
  22  66a300001000              mov ds:0x100000,ax                              | P32-off
  28  b900001000                mov ecx,0x100000                                | P32-off
  33  8b09                      mov ecx,DWORD PTR [ecx]                         | P32-off
```

#### X03. Mixed32 program, original generation

- **Key**: `mixed_old`  |  **Bytes**: 6  |  **Modes**: P32-off, compat
- **Hex**: `89c1890b8b02`
- **Defined at**:
  - `crates/abi/src/mixed32.rs:16 (SOURCE)`
  - `tools/check_i4v_mixed.py:26`
  - `tools/check_i4v_address_concurrent.py:21 (same bytes, address profiles)`
  - `tools/i4v_producer/export.py:142 (fex_mixed32_old)`
  - `tools/i4v_producer/probe_mixed32.py:11`
  - `tools/i4v_producer/testdata/mixed32-old-0 (RIP 0x102000), mixed32-old-1 (0x102080)`
  - `crates/host/src/concurrent.rs:656-662`
- **Run by**: i4v-mixed off-first and paged-first, N owners: owner mode = (owner + profile - 1) & 1; mode 0 = protected32 paging off (CR0=0x10001,CR4=0x600,EFER=0), mode 1 = default seed (CR0=0x80010001,CR4=0x620,EFER=0xd00, CS.D=1,L=0 => compatibility mode with 4-level paging).
- **Notes**: mov ecx,eax; mov [ebx],ecx; mov eax,[edx]. Mode 1 is named "paged" in mixed32.rs but by modes::mode() it is IA-32e compatibility mode (see ambiguities).

```
off   bytes                     decoded                                        | executed-in / note
   0  89c1                      mov ecx,eax                                     | P32-off/compat
   2  890b                      mov DWORD PTR [ebx],ecx                         | P32-off/compat
   4  8b02                      mov eax,DWORD PTR [edx]                         | P32-off/compat
```

#### X04. Mixed32 program, replacement generation (STORE EDX)

- **Key**: `mixed_new`  |  **Bytes**: 6  |  **Modes**: P32-off, compat
- **Hex**: `89c189138b02`
- **Defined at**:
  - `crates/abi/src/mixed32.rs:17 (REPLACEMENT)`
  - `tools/check_i4v_mixed.py:27`
  - `tools/check_i4v_address_concurrent.py:22`
  - `tools/i4v_producer/export.py:145 (fex_mixed32_changed)`
  - `tools/i4v_producer/testdata/mixed32-replacement-0/-1`
- **Run by**: Same runs, after the source page is rewritten (old-code-entry observability).
- **Notes**: Differs in byte 3 (0b -> 13): store EDX instead of ECX.

```
off   bytes                     decoded                                        | executed-in / note
   0  89c1                      mov ecx,eax                                     | P32-off/compat
   2  8913                      mov DWORD PTR [ebx],edx                         | P32-off/compat
   4  8b02                      mov eax,DWORD PTR [edx]                         | P32-off/compat
```

#### X05. Address-formation program, 32-bit addressing (profiles 0,1,2,4,5), generation 1

- **Key**: `addr32`  |  **Bytes**: 6  |  **Modes**: P32-off, P32-paged
- **Hex**: `89c1890b8b02`
- **Defined at**:
  - `crates/abi/src/address_concurrent.rs:195-213 (source steps 0..2), offsets at :185-194`
  - `crates/host/src/concurrent/address.rs:206-211 (writer)`
  - `tools/check_i4v_address_concurrent.py:21`
  - `tools/i4v_producer/export.py:65-82 (fex_address_store_ecx/load_edx...)`
- **Run by**: i4v-address-concurrent profile 0,1,2,4,5: owner CS/DS formation, A20, segment limits; actor owner runs paging-off (CR0=0x10001), other owners run legacy 2-level paged (CR0=0x80010001, CR4=0x600, EFER=0). Instructions are placed at page offset 0x123 + step offset (EIP = 0x2000 + step offset) with 0xcc padding. Step offsets 0,2,4 (end 6). Profile 5 shrinks the CS limit so the last fetch faults.
- **Notes**: Per-step FEX cases (tools/i4v_address_worker.py:case_for): step 0 reuses fex_moffs_load32_step8 (89c1), step 1 fex_address_store_ecx (890b), step 2 fex_address_load_edx (8b02).

```
off   bytes                     decoded                                        | executed-in / note
   0  89c1                      mov ecx,eax                                     | P32-off/P32-paged
   2  890b                      mov DWORD PTR [ebx],ecx                         | P32-off/P32-paged
   4  8b02                      mov eax,DWORD PTR [edx]                         | P32-off/P32-paged
```

#### X06. Address-formation program, generation 2 (store EDX), profiles 0,1,2,4,5

- **Key**: `addr32b`  |  **Bytes**: 6  |  **Modes**: P32-off, P32-paged
- **Hex**: `89c189138b02`
- **Defined at**:
  - `crates/abi/src/address_concurrent.rs:195-213 (step1, generation 2)`
  - `tools/check_i4v_address_concurrent.py:22`
  - `tools/i4v_producer/export.py:68 (fex_address_store_edx)`
- **Run by**: Same, after source replacement.

```
off   bytes                     decoded                                        | executed-in / note
   0  89c1                      mov ecx,eax                                     | P32-off/P32-paged
   2  8913                      mov DWORD PTR [ebx],edx                         | P32-off/P32-paged
   4  8b02                      mov eax,DWORD PTR [edx]                         | P32-off/P32-paged
```

#### X07. Address-formation program, 16-bit addressing (67h prefix), profile 3, generation 1

- **Key**: `addr16`  |  **Bytes**: 8  |  **Modes**: P32-off, P32-paged
- **Hex**: `89c1678908678b00`
- **Defined at**:
  - `crates/abi/src/address_concurrent.rs:195-213 (profile==3 arms)`
  - `crates/host/src/concurrent/address.rs:206-211`
  - `tools/check_i4v_address_concurrent.py:23`
  - `tools/i4v_producer/export.py:74-80 (fex_address_store_ecx16, fex_address_load16)`
- **Run by**: i4v-address-concurrent profile 3: owner CS/DS formation, A20, segment limits; actor owner runs paging-off (CR0=0x10001), other owners run legacy 2-level paged (CR0=0x80010001, CR4=0x600, EFER=0). Instructions are placed at page offset 0x123 + step offset (EIP = 0x2000 + step offset) with 0xcc padding. Step offsets 0,2,5 (end 8). EBX=0x1234fff0 and ESI/offset wraps modulo 64K.
- **Notes**: Per-step FEX cases: step 0 fex_moffs_load32_step8, step 1 fex_address_store_ecx16 (678908), step 2 fex_address_load16 (678b00). 67h in 32-bit mode selects 16-bit addressing: [bx+si]. FEX body does explicit 16-bit address arithmetic (add + ubfx).

```
off   bytes                     decoded                                        | executed-in / note
   0  89c1                      mov ecx,eax                                     | P32-off/P32-paged
   2  678908                    mov DWORD PTR [bx+si],ecx                       | P32-off/P32-paged
   5  678b00                    mov eax,DWORD PTR [bx+si]                       | P32-off/P32-paged
```

#### X08. Address-formation program, 16-bit addressing, generation 2 (store EDX)

- **Key**: `addr16b`  |  **Bytes**: 8  |  **Modes**: P32-off, P32-paged
- **Hex**: `89c1678910678b00`
- **Defined at**:
  - `crates/abi/src/address_concurrent.rs:195-213`
  - `tools/check_i4v_address_concurrent.py:24`
  - `tools/i4v_producer/export.py:77 (fex_address_store_edx16)`
- **Run by**: Same, after source replacement.

```
off   bytes                     decoded                                        | executed-in / note
   0  89c1                      mov ecx,eax                                     | P32-off/P32-paged
   2  678910                    mov DWORD PTR [bx+si],edx                       | P32-off/P32-paged
   5  678b00                    mov eax,DWORD PTR [bx+si]                       | P32-off/P32-paged
```

#### X09. Coherence store [ebx] <- eax (32-bit profiles)

- **Key**: `coh_store32`  |  **Bytes**: 2  |  **Modes**: P32-paged, P32-PAE
- **Hex**: `8903`
- **Defined at**:
  - `crates/abi/src/coherence.rs:234-238 (source(STORE))`
  - `tools/check_i4v_coherence.py:26`
  - `tools/i4v_producer/export.py:136 (fex_store_eax_ebx32)`
- **Run by**: I4-V coherence profile 1 (legacy 2-level paging, 4-byte entries, CR3=0x1000) and profiles 2, 4 (PAE, CR3=0x1020).
- **Notes**: Same bytes as coh_store64; decoded with 32-bit address size.

```
off   bytes                     decoded                                        | executed-in / note
   0  8903                      mov DWORD PTR [ebx],eax                         | P32-paged/P32-PAE
```

#### X10. Coherence load eax <- [ebx] (32-bit profiles)

- **Key**: `coh_load32`  |  **Bytes**: 2  |  **Modes**: P32-paged, P32-PAE
- **Hex**: `8b03`
- **Defined at**:
  - `crates/abi/src/coherence.rs:234-238 (source(LOAD))`
  - `tools/check_i4v_coherence.py:26`
  - `tools/i4v_producer/export.py:139 (fex_load_eax_ebx32)`
- **Run by**: I4-V coherence profiles 1, 2, 4.

```
off   bytes                     decoded                                        | executed-in / note
   0  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged/P32-PAE
```

### 2.3 Single-owner control profiles 0-38 (mode-changing, tools/i4v_controls_check.py names)

All control programs start in `P32-off` with CR0=0x10011 and RIP=0x102000 (FEX compiled at the actual guest RIP; guest linear = RIP). The per-instruction "executed-in" column comes from the unmodified independent Python models; "xN" counts visits (profiles 0, 21 and 24 loop). Instruction forms that FEX compiles are `mov eax,imm32`, `mov [ebx],eax`, `mov eax,[ebx]` and (long mode only) `mov rax,[rbx]`; the rest are resident-handled. The `ljmp ptr16:32` target field holds `0x102000 + offset-after-instruction`.

Common atoms (decoded once; used by many profiles):

| Atom (decoded) | Bytes | Decode | Compiled by FEX? / used in |
|---|---|---|---|
| `mov eax,0x0` | `b800000000` | 32-bit | yes; in 1 control programs |
| `mov eax,0x100` | `b800010000` | 32-bit | yes; in 15 control programs |
| `mov eax,0x600` | `b800060000` | 32-bit | yes; in 2 control programs |
| `mov eax,0x1000` | `b800100000` | 32-bit | yes; in 2 control programs |
| `mov eax,0x3000` | `b800300000` | 32-bit | yes; in 2 control programs |
| `mov eax,0x6000` | `b800600000` | 32-bit | yes; in 14 control programs |
| `mov eax,0x1003` | `b803100000` | 32-bit | yes; in 1 control programs |
| `mov eax,0x80010010` | `b810000180` | 32-bit | yes; in 1 control programs |
| `mov eax,0x80000011` | `b811000080` | 32-bit | yes; in 2 control programs |
| `mov eax,0x10011` | `b811000100` | 32-bit | yes; in 16 control programs |
| `mov eax,0x20010011` | `b811000120` | 32-bit | yes; in 1 control programs |
| `mov eax,0x80010011` | `b811000180` | 32-bit | yes; in 35 control programs |
| `mov eax,0x620` | `b820060000` | 32-bit | yes; in 23 control programs |
| `mov eax,0x1020` | `b820100000` | 32-bit | yes; in 4 control programs |
| `mov eax,0x680` | `b880060000` | 32-bit | yes; in 1 control programs |
| `mov eax,0x6a0` | `b8a0060000` | 32-bit | yes; in 2 control programs |
| `mov eax,0xffffffff` | `b8ffffffff` | 32-bit | yes; in 3 control programs |
| `invlpg BYTE PTR [ebx]` | `0f013b` | 32-bit | resident; in 1 control programs |
| `mov cr0,eax` | `0f22c0` | 32-bit | resident; in 34 control programs |
| `mov cr1,eax` | `0f22c8` | 32-bit | resident; in 1 control programs |
| `mov cr3,eax` | `0f22d8` | 32-bit | resident; in 22 control programs |
| `mov cr4,eax` | `0f22e0` | 32-bit | resident; in 25 control programs |
| `wrmsr` | `0f30` | 32-bit | resident; in 16 control programs |
| `mov rax,QWORD PTR [rbx]` | `488b03` | 64-bit | yes; in 9 control programs |
| `ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)` | `48ff2e` | 64-bit | resident; in 5 control programs |
| `mov cr0,eax   (66h prefix, ModRM mod=00 ignored)` | `660f2200` | 32-bit | resident; in 1 control programs |
| `mov DWORD PTR [ebx],eax` | `8903` | 32-bit | yes; in 12 control programs |
| `mov eax,DWORD PTR [ebx]` | `8b03` | 32-bit | yes; in 18 control programs |
| `lock mov cr0,eax   (objdump: mov cr8,eax; suite: LOCK MOV CR0 -> #UD)` | `f00f22c0` | 32-bit | resident; in 1 control programs |

#### C01. Control profile 0: roundtrip

- **Key**: `ctl00`  |  **Bytes**: 52  |  **Modes**: P32-off, P32-paged
- **Hex**: `b8110001008903b8110001800f22c08b03b8003000000f22d88b03b8001000000f22d88b03b8110001000f22c08b03e9ccffffff`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[0])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 0 (roundtrip). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0. Also i4v-controls-concurrent (extended/replacement variants below).
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): roundtrip.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000100                mov eax,0x10011                                 | P32-off x2
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off x2
   7  b811000180                mov eax,0x80010011                              | P32-off
  12  0f22c0                    mov cr0,eax                                     | P32-off
  15  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
  17  b800300000                mov eax,0x3000                                  | P32-paged
  22  0f22d8                    mov cr3,eax                                     | P32-paged
  25  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
  27  b800100000                mov eax,0x1000                                  | P32-paged
  32  0f22d8                    mov cr3,eax                                     | P32-paged
  35  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
  37  b811000100                mov eax,0x10011                                 | P32-paged
  42  0f22c0                    mov cr0,eax                                     | P32-paged
  45  8b03                      mov eax,DWORD PTR [ebx]                         | P32-off
  47  e9ccffffff                jmp 0xffffffd1   (E9 rel32)                     | P32-off
```

#### C02. Control profile 1: reload

- **Key**: `ctl01`  |  **Bytes**: 20  |  **Modes**: P32-off, P32-paged
- **Hex**: `b8110001800f22c08b03b8001000000f22d88b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[1])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 1 (reload). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): reload.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000180                mov eax,0x80010011                              | P32-off
   5  0f22c0                    mov cr0,eax                                     | P32-off
   8  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
  10  b800100000                mov eax,0x1000                                  | P32-paged
  15  0f22d8                    mov cr3,eax                                     | P32-paged
  18  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
```

#### C03. Control profile 2: wp

- **Key**: `ctl02`  |  **Bytes**: 30  |  **Modes**: P32-off, P32-paged
- **Hex**: `b8110000800f22c0b8110001008903b8110001800f22c0b8ffffffff8903`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[2])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 2 (wp). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): wp.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000080                mov eax,0x80000011                              | P32-off
   5  0f22c0                    mov cr0,eax                                     | P32-off
   8  b811000100                mov eax,0x10011                                 | P32-paged
  13  8903                      mov DWORD PTR [ebx],eax                         | P32-paged
  15  b811000180                mov eax,0x80010011                              | P32-paged
  20  0f22c0                    mov cr0,eax                                     | P32-paged
  23  b8ffffffff                mov eax,0xffffffff                              | P32-paged
  28  8903                      mov DWORD PTR [ebx],eax                         | P32-paged ; faults #PF(err=3)
```

#### C04. Control profile 3: bad-pg

- **Key**: `ctl03`  |  **Bytes**: 15  |  **Modes**: P32-off
- **Hex**: `b8110001008903b8100001800f22c0`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[3])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 3 (bad-pg). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0. Also i4v-controls-concurrent unchanged.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): bad-pg.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000100                mov eax,0x10011                                 | P32-off
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off
   7  b810000180                mov eax,0x80010010                              | P32-off
  12  0f22c0                    mov cr0,eax                                     | P32-off ; faults #GP(err=0)
```

#### C05. Control profile 4: bad-nw

- **Key**: `ctl04`  |  **Bytes**: 15  |  **Modes**: P32-off
- **Hex**: `b8110001008903b8110001200f22c0`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[4])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 4 (bad-nw). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): bad-nw.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000100                mov eax,0x10011                                 | P32-off
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off
   7  b811000120                mov eax,0x20010011                              | P32-off
  12  0f22c0                    mov cr0,eax                                     | P32-off ; faults #GP(err=0)
```

#### C06. Control profile 5: bad-cr4

- **Key**: `ctl05`  |  **Bytes**: 15  |  **Modes**: P32-off
- **Hex**: `b8110001008903b8ffffffff0f22e0`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[5])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 5 (bad-cr4). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): bad-cr4.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000100                mov eax,0x10011                                 | P32-off
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off
   7  b8ffffffff                mov eax,0xffffffff                              | P32-off
  12  0f22e0                    mov cr4,eax                                     | P32-off ; faults #GP(err=0)
```

#### C07. Control profile 6: cpl3

- **Key**: `ctl06`  |  **Bytes**: 15  |  **Modes**: P32-off
- **Hex**: `b8110001008903b8110001800f22c0`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[6])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 6 (cpl3). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL3.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): cpl3.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000100                mov eax,0x10011                                 | P32-off
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off
   7  b811000180                mov eax,0x80010011                              | P32-off
  12  0f22c0                    mov cr0,eax                                     | P32-off ; faults #GP(err=0)
```

#### C08. Control profile 7: fetch-np

- **Key**: `ctl07`  |  **Bytes**: 25  |  **Modes**: P32-off, P32-paged
- **Hex**: `b8110001008903b8110001800f22c0b8003000000f22d88b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[7])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 7 (fetch-np). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): fetch-np.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000100                mov eax,0x10011                                 | P32-off
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off
   7  b811000180                mov eax,0x80010011                              | P32-off
  12  0f22c0                    mov cr0,eax                                     | P32-off
  15  b800300000                mov eax,0x3000                                  | P32-paged
  20  0f22d8                    mov cr3,eax                                     | P32-paged
  23  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged ; faults #PF(err=0); fetch itself faults, bytes never decoded
```

#### C09. Control profile 8: ignored-cr3

- **Key**: `ctl08`  |  **Bytes**: 18  |  **Modes**: P32-off, P32-paged
- **Hex**: `b8031000000f22d8b8110001800f22c08b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[8])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 8 (ignored-cr3). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ignored-cr3.

```
off   bytes                     decoded                                        | executed-in / note
   0  b803100000                mov eax,0x1003                                  | P32-off
   5  0f22d8                    mov cr3,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
```

#### C10. Control profile 9: operand-mod

- **Key**: `ctl09`  |  **Bytes**: 11  |  **Modes**: P32-off, P32-paged
- **Hex**: `b811000180660f22008b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[9])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 9 (operand-mod). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): operand-mod.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000180                mov eax,0x80010011                              | P32-off
   5  660f2200                  mov cr0,eax   (66h prefix, ModRM mod=00 ignored)| P32-off
   9  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
```

#### C11. Control profile 10: lock

- **Key**: `ctl10`  |  **Bytes**: 16  |  **Modes**: P32-off
- **Hex**: `b8110001008903b811000180f00f22c0`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[10])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 10 (lock). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): lock.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000100                mov eax,0x10011                                 | P32-off
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off
   7  b811000180                mov eax,0x80010011                              | P32-off
  12  f00f22c0                  lock mov cr0,eax   (objdump: mov cr8,eax; suite: LOCK MOV CR0 -> #UD)| P32-off ; faults #UD(err=0)
```

#### C12. Control profile 11: reserved-cr1

- **Key**: `ctl11`  |  **Bytes**: 15  |  **Modes**: P32-off
- **Hex**: `b8110001008903b8110001800f22c8`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[11])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 11 (reserved-cr1). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): reserved-cr1.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000100                mov eax,0x10011                                 | P32-off
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off
   7  b811000180                mov eax,0x80010011                              | P32-off
  12  0f22c8                    mov cr1,eax                                     | P32-off ; faults #UD(err=0)
```

#### C13. Control profile 12: pge

- **Key**: `ctl12`  |  **Bytes**: 30  |  **Modes**: P32-off, P32-paged
- **Hex**: `b8110001800f22c08b03b8800600000f22e08b03b8000600000f22e08b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[12])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 12 (pge). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1000, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pge.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000180                mov eax,0x80010011                              | P32-off
   5  0f22c0                    mov cr0,eax                                     | P32-off
   8  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
  10  b880060000                mov eax,0x680                                   | P32-paged
  15  0f22e0                    mov cr4,eax                                     | P32-paged
  18  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
  20  b800060000                mov eax,0x600                                   | P32-paged
  25  0f22e0                    mov cr4,eax                                     | P32-paged
  28  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
```

#### C14. Control profile 13: pae-enable

- **Key**: `ctl13`  |  **Bytes**: 18  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[13])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 13 (pae-enable). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pae-enable.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
```

#### C15. Control profile 14: pae-invlpg

- **Key**: `ctl14`  |  **Bytes**: 33  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08b030f013b8b03b8201000000f22d88b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[14])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 14 (pae-invlpg). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pae-invlpg.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  18  0f013b                    invlpg BYTE PTR [ebx]                           | P32-PAE
  21  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  23  b820100000                mov eax,0x1020                                  | P32-PAE
  28  0f22d8                    mov cr3,eax                                     | P32-PAE
  31  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
```

#### C16. Control profile 15: pae-evict

- **Key**: `ctl15`  |  **Bytes**: 30  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08b038b03b8201000000f22d88b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[15])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 15 (pae-evict). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pae-evict.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  18  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  20  b820100000                mov eax,0x1020                                  | P32-PAE
  25  0f22d8                    mov cr3,eax                                     | P32-PAE
  28  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
```

#### C17. Control profile 16: pae-wp-pge

- **Key**: `ctl16`  |  **Bytes**: 38  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08b03b8110000800f22c08b03b8a00600000f22e08b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[16])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 16 (pae-wp-pge). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pae-wp-pge.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  18  b811000080                mov eax,0x80000011                              | P32-PAE
  23  0f22c0                    mov cr0,eax                                     | P32-PAE
  26  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  28  b8a0060000                mov eax,0x6a0                                   | P32-PAE
  33  0f22e0                    mov cr4,eax                                     | P32-PAE
  36  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
```

#### C18. Control profile 17: pae-invalid-enable

- **Key**: `ctl17`  |  **Bytes**: 23  |  **Modes**: P32-off
- **Hex**: `b8110001008903b8200600000f22e0b8110001800f22c0`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[17])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 17 (pae-invalid-enable). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pae-invalid-enable.

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000100                mov eax,0x10011                                 | P32-off
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off
   7  b820060000                mov eax,0x620                                   | P32-off
  12  0f22e0                    mov cr4,eax                                     | P32-off
  15  b811000180                mov eax,0x80010011                              | P32-off
  20  0f22c0                    mov cr0,eax                                     | P32-off ; faults #GP(err=0)
```

#### C19. Control profile 18: pae-invalid-cr3

- **Key**: `ctl18`  |  **Bytes**: 35  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08b03b8110001008903b8201000000f22d88b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[18])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 18 (pae-invalid-cr3). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pae-invalid-cr3.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  18  b811000100                mov eax,0x10011                                 | P32-PAE
  23  8903                      mov DWORD PTR [ebx],eax                         | P32-PAE
  25  b820100000                mov eax,0x1020                                  | P32-PAE
  30  0f22d8                    mov cr3,eax                                     | P32-PAE ; faults #GP(err=0)
  33  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
```

#### C20. Control profile 19: pae-invalid-cr4

- **Key**: `ctl19`  |  **Bytes**: 35  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08b03b8110001008903b8a00600000f22e08b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[19])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 19 (pae-invalid-cr4). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pae-invalid-cr4.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  18  b811000100                mov eax,0x10011                                 | P32-PAE
  23  8903                      mov DWORD PTR [ebx],eax                         | P32-PAE
  25  b8a0060000                mov eax,0x6a0                                   | P32-PAE
  30  0f22e0                    mov cr4,eax                                     | P32-PAE ; faults #GP(err=0)
  33  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
```

#### C21. Control profile 20: pae-nonpresent

- **Key**: `ctl20`  |  **Bytes**: 18  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08b03`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[20])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 20 (pae-nonpresent). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pae-nonpresent.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE ; faults #PF(err=0); fetch itself faults, bytes never decoded
```

#### C22. Control profile 21: pae-aba

- **Key**: `ctl21`  |  **Bytes**: 41  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08b03b8201000000f22d88b03b8201000000f22d8e9e7ffffff`
- **Defined at**:
  - `crates/abi/src/controls.rs:208-364 (PROGRAMS[21])`
  - independent twin: tools/i4v_controls_check.py:51-74
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 21 (pae-aba). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0. Also i4v-controls-concurrent and scheduler family "controls".
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): pae-aba.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE x2
  18  b820100000                mov eax,0x1020                                  | P32-PAE
  23  0f22d8                    mov cr3,eax                                     | P32-PAE
  26  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  28  b820100000                mov eax,0x1020                                  | P32-PAE
  33  0f22d8                    mov cr3,eax                                     | P32-PAE
  36  e9e7ffffff                jmp 0xffffffec   (E9 rel32)                     | P32-PAE
```

#### C23. Control profile 22: ia32e-roundtrip

- **Key**: `ctl22`  |  **Bytes**: 56  |  **Modes**: P32-off, compat, long64
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e8b03b8110001000f22c08b03`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[0], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 22 (ia32e-roundtrip). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0. Also i4v-controls-concurrent.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-roundtrip.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat
  38  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  41  48ff2e                    ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)    | long64
  44  8b03                      mov eax,DWORD PTR [ebx]                         | compat
  46  b811000100                mov eax,0x10011                                 | compat
  51  0f22c0                    mov cr0,eax                                     | compat
  54  8b03                      mov eax,DWORD PTR [ebx]                         | P32-off
```

#### C24. Control profile 23: ia32e-pae

- **Key**: `ctl23`  |  **Bytes**: 74  |  **Modes**: P32-off, P32-PAE, compat, long64
- **Hex**: `b8200600000f22e0b8110001800f22c08b03b8110001000f22c0b8000100000f30b8006000000f22d8b8110001800f22c0ea382010001800488b0348ff2e8b03b8110001000f22c08b03`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[1], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 23 (ia32e-pae). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-pae.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  18  b811000100                mov eax,0x10011                                 | P32-PAE
  23  0f22c0                    mov cr0,eax                                     | P32-PAE
  26  b800010000                mov eax,0x100                                   | P32-off
  31  0f30                      wrmsr                                           | P32-off
  33  b800600000                mov eax,0x6000                                  | P32-off
  38  0f22d8                    mov cr3,eax                                     | P32-off
  41  b811000180                mov eax,0x80010011                              | P32-off
  46  0f22c0                    mov cr0,eax                                     | P32-off
  49  ea382010001800            ljmp 0x18, 0x102038   (EA ptr16:32)             | compat
  56  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  59  48ff2e                    ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)    | long64
  62  8b03                      mov eax,DWORD PTR [ebx]                         | compat
  64  b811000100                mov eax,0x10011                                 | compat
  69  0f22c0                    mov cr0,eax                                     | compat
  72  8b03                      mov eax,DWORD PTR [ebx]                         | P32-off
```

#### C25. Control profile 24: ia32e-aba

- **Key**: `ctl24`  |  **Bytes**: 38  |  **Modes**: P32-off, compat, long64
- **Hex**: `8b03b8110001800f22c0ea112010001800488b0348ff2e8b03b8110001000f22c0e9daffffff`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[2], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 24 (ia32e-aba). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x6000, CR4=0x620, EFER=0x100, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0. Also i4v-controls-concurrent, scheduler family "controls" and containment.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-aba.

```
off   bytes                     decoded                                        | executed-in / note
   0  8b03                      mov eax,DWORD PTR [ebx]                         | P32-off x2
   2  b811000180                mov eax,0x80010011                              | P32-off
   7  0f22c0                    mov cr0,eax                                     | P32-off
  10  ea112010001800            ljmp 0x18, 0x102011   (EA ptr16:32)             | compat
  17  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  20  48ff2e                    ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)    | long64
  23  8b03                      mov eax,DWORD PTR [ebx]                         | compat
  25  b811000100                mov eax,0x10011                                 | compat
  30  0f22c0                    mov cr0,eax                                     | compat
  33  e9daffffff                jmp 0xffffffdf   (E9 rel32)                     | P32-off
```

#### C26. Control profile 25: ia32e-lme-active

- **Key**: `ctl25`  |  **Bytes**: 38  |  **Modes**: P32-off, compat
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0b8000000000f30`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[3], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 25 (ia32e-lme-active). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-lme-active.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  b800000000                mov eax,0x0                                     | compat
  36  0f30                      wrmsr                                           | compat ; faults #GP(err=0)
```

#### C27. Control profile 26: ia32e-no-pae

- **Key**: `ctl26`  |  **Bytes**: 15  |  **Modes**: P32-off
- **Hex**: `b8000100000f30b8110001800f22c0`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[4], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 26 (ia32e-no-pae). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-no-pae.

```
off   bytes                     decoded                                        | executed-in / note
   0  b800010000                mov eax,0x100                                   | P32-off
   5  0f30                      wrmsr                                           | P32-off
   7  b811000180                mov eax,0x80010011                              | P32-off
  12  0f22c0                    mov cr0,eax                                     | P32-off ; faults #GP(err=0)
```

#### C28. Control profile 27: ia32e-clear-pae

- **Key**: `ctl27`  |  **Bytes**: 39  |  **Modes**: P32-off, compat
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0b8000600000f22e0`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[5], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 27 (ia32e-clear-pae). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-clear-pae.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  b800060000                mov eax,0x600                                   | compat
  36  0f22e0                    mov cr4,eax                                     | compat ; faults #GP(err=0)
```

#### C29. Control profile 28: ia32e-clear-pg64

- **Key**: `ctl28`  |  **Bytes**: 46  |  **Modes**: P32-off, compat, long64
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800b8110001000f22c0`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[6], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 28 (ia32e-clear-pg64). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-clear-pg64.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat
  38  b811000100                mov eax,0x10011                                 | long64
  43  0f22c0                    mov cr0,rax                                     | long64 ; faults #GP(err=0)
```

#### C30. Control profile 29: ia32e-efer-reserved

- **Key**: `ctl29`  |  **Bytes**: 7  |  **Modes**: P32-off
- **Hex**: `b8ffffffff0f30`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[7], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 29 (ia32e-efer-reserved). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-efer-reserved.

```
off   bytes                     decoded                                        | executed-in / note
   0  b8ffffffff                mov eax,0xffffffff                              | P32-off
   5  0f30                      wrmsr                                           | P32-off ; faults #GP(err=0)
```

#### C31. Control profile 30: ia32e-ea64

- **Key**: `ctl30`  |  **Bytes**: 45  |  **Modes**: P32-off, compat, long64
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800ea2d2010001800`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[8], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 30 (ia32e-ea64). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-ea64.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat
  38  ea2d2010001800            (EA ljmp ptr16:32, invalid opcode in 64-bit mode)| long64 ; faults #UD(err=0)
```

#### C32. Control profile 31: ia32e-gdt-limit

- **Key**: `ctl31`  |  **Bytes**: 38  |  **Modes**: P32-off, compat
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010003800`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[9], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 31 (ia32e-gdt-limit). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-gdt-limit.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010003800            ljmp 0x38, 0x102026   (EA ptr16:32)             | compat ; faults #GP(err=56)
```

#### C33. Control profile 32: ia32e-not-present

- **Key**: `ctl32`  |  **Bytes**: 41  |  **Modes**: P32-off, compat
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b03`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[10], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 32 (ia32e-not-present). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-not-present.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat ; faults #NP(err=24)
  38  488b03                    mov rax,QWORD PTR [rbx]                         | - ; never reached (an earlier instruction faults)
```

#### C34. Control profile 33: ia32e-long-default

- **Key**: `ctl33`  |  **Bytes**: 41  |  **Modes**: P32-off, compat
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b03`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[11], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 33 (ia32e-long-default). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-long-default.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat ; faults #GP(err=24)
  38  488b03                    mov rax,QWORD PTR [rbx]                         | - ; never reached (an earlier instruction faults)
```

#### C35. Control profile 34: ia32e-pointer-np

- **Key**: `ctl34`  |  **Bytes**: 46  |  **Modes**: P32-off, compat, long64
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e8b03`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[12], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 34 (ia32e-pointer-np). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-pointer-np.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat
  38  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  41  48ff2e                    ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)    | long64 ; faults #PF(err=0)
  44  8b03                      mov eax,DWORD PTR [ebx]                         | - ; never reached (an earlier instruction faults)
```

#### C36. Control profile 35: ia32e-fetch-np

- **Key**: `ctl35`  |  **Bytes**: 41  |  **Modes**: P32-off, compat, long64
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b03`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[13], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 35 (ia32e-fetch-np). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-fetch-np.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat
  38  488b03                    mov rax,QWORD PTR [rbx]                         | long64 ; faults #PF(err=0); fetch itself faults, bytes never decoded
```

#### C37. Control profile 36: ia32e-tss16

- **Key**: `ctl36`  |  **Bytes**: 41  |  **Modes**: P32-off
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b03`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[14], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 36 (ia32e-tss16). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-tss16.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off ; faults #GP(err=0)
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | - ; never reached (an earlier instruction faults)
  38  488b03                    mov rax,QWORD PTR [rbx]                         | - ; never reached (an earlier instruction faults)
```

#### C38. Control profile 37: ia32e-data-cs

- **Key**: `ctl37`  |  **Bytes**: 38  |  **Modes**: P32-off, compat
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001000`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[15], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 37 (ia32e-data-cs). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-data-cs.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001000            ljmp 0x10, 0x102026   (EA ptr16:32)             | compat ; faults #GP(err=16)
```

#### C39. Control profile 38: ia32e-noncanonical

- **Key**: `ctl38`  |  **Bytes**: 47  |  **Modes**: P32-off, compat, long64
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e488b03`
- **Defined at**:
  - `crates/abi/src/modes.rs:259-326 (PROGRAMS[16], via controls::instructions)`
  - independent twin: tools/i4v_modes_model.py:12-47
  - host: crates/host/src/i4v_controls.rs:142-159 (control::source(profile) -> recovery::SOURCE_OFFSET, RIP 0x102000)
- **Run by**: i4v-controls profile 38 (ia32e-noncanonical). Start state: CR0=0x10011 (PE,ET,WP; paging off), CR3=0x1020, CR4=0x600, EFER=0, CS=[8,0,0xffffffff,0xc09b] (CS.D=1), flat data; CPL0.
- **Notes**: Short name in the independent oracle (tools/i4v_controls_check.py:NAMES): ia32e-noncanonical.

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat
  38  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  41  48ff2e                    ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)    | long64 ; faults #GP(err=0)
  44  488b03                    mov rax,QWORD PTR [rbx]                         | - ; never reached (an earlier instruction faults)
```

### 2.4 Concurrent-control variants (profiles 0, 21, 22, 24 generation 2; 21, 22, 24 extended generation 1)

#### K01. Concurrent control profile 0 (roundtrip), generation 2 (replacement)

- **Key**: `conc00g2`  |  **Bytes**: 52  |  **Modes**: P32-off, P32-paged
- **Hex**: `b8110000008903b8110001800f22c08b03b8003000000f22d88b03b8001000000f22d88b03b8110001000f22c08b03e9ccffffff`
- **Defined at**:
  - `crates/abi/src/control_concurrent.rs:76-100 (program() additions), :108-130 (source_bytes), :56-70 (replacement/offset)`
  - independent twin: tools/i4v_controls_concurrent_check.py:42-60 (owner_program)
  - host: crates/host/src/concurrent/controls.rs:445-458 (source at SOURCE_OFFSET + id*256, ljmp targets use owner id)
- **Run by**: i4v-controls-concurrent profile 0, N=1..4 owners, actor role; ljmp target in bytes shown is for owner 0 (add 0x100*id for owner id). Scheduler family "controls" uses profiles 21 and 24.
- **Notes**: Replacement overwrites bytes 0-4 (first MOV EAX,imm32 becomes MOV EAX,0x11).

```
off   bytes                     decoded                                        | executed-in / note
   0  b811000000                mov eax,0x11                                    | P32-off x2
   5  8903                      mov DWORD PTR [ebx],eax                         | P32-off x2
   7  b811000180                mov eax,0x80010011                              | P32-off
  12  0f22c0                    mov cr0,eax                                     | P32-off
  15  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
  17  b800300000                mov eax,0x3000                                  | P32-paged
  22  0f22d8                    mov cr3,eax                                     | P32-paged
  25  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
  27  b800100000                mov eax,0x1000                                  | P32-paged
  32  0f22d8                    mov cr3,eax                                     | P32-paged
  35  8b03                      mov eax,DWORD PTR [ebx]                         | P32-paged
  37  b811000100                mov eax,0x10011                                 | P32-paged
  42  0f22c0                    mov cr0,eax                                     | P32-paged
  45  8b03                      mov eax,DWORD PTR [ebx]                         | P32-off
  47  e9ccffffff                jmp 0xffffffd1   (E9 rel32)                     | P32-off
```

#### K02. Concurrent control profile 21 (pae-aba), generation 1 (extended program revision 1)

- **Key**: `conc21g1`  |  **Bytes**: 56  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08b03b8201000000f22d88b03b800000000b800010000b820100000b8201000000f22d8e9d8ffffff`
- **Defined at**:
  - `crates/abi/src/control_concurrent.rs:76-100 (program() additions), :108-130 (source_bytes), :56-70 (replacement/offset)`
  - independent twin: tools/i4v_controls_concurrent_check.py:42-60 (owner_program)
  - host: crates/host/src/concurrent/controls.rs:445-458 (source at SOURCE_OFFSET + id*256, ljmp targets use owner id)
- **Run by**: i4v-controls-concurrent profile 21, N=1..4 owners, actor role; ljmp target in bytes shown is for owner 0 (add 0x100*id for owner id). Scheduler family "controls" uses profiles 21 and 24.
- **Notes**: Adds MOV EAX,0 / MOV EAX,0x100 / MOV EAX,0x1020 before original instruction 8. 

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE x2
  18  b820100000                mov eax,0x1020                                  | P32-PAE
  23  0f22d8                    mov cr3,eax                                     | P32-PAE
  26  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  28  b800000000                mov eax,0x0                                     | P32-PAE
  33  b800010000                mov eax,0x100                                   | P32-PAE
  38  b820100000                mov eax,0x1020                                  | P32-PAE
  43  b820100000                mov eax,0x1020                                  | P32-PAE
  48  0f22d8                    mov cr3,eax                                     | P32-PAE
  51  e9d8ffffff                jmp 0xffffffdd   (E9 rel32)                     | P32-PAE
```

#### K03. Concurrent control profile 21 (pae-aba), generation 2 (replacement)

- **Key**: `conc21g2`  |  **Bytes**: 56  |  **Modes**: P32-off, P32-PAE
- **Hex**: `b8200600000f22e0b8110001800f22c08903b8201000000f22d88b03b800000000b800010000b820100000b8201000000f22d8e9d8ffffff`
- **Defined at**:
  - `crates/abi/src/control_concurrent.rs:76-100 (program() additions), :108-130 (source_bytes), :56-70 (replacement/offset)`
  - independent twin: tools/i4v_controls_concurrent_check.py:42-60 (owner_program)
  - host: crates/host/src/concurrent/controls.rs:445-458 (source at SOURCE_OFFSET + id*256, ljmp targets use owner id)
- **Run by**: i4v-controls-concurrent profile 21, N=1..4 owners, actor role; ljmp target in bytes shown is for owner 0 (add 0x100*id for owner id). Scheduler family "controls" uses profiles 21 and 24.
- **Notes**: Adds MOV EAX,0 / MOV EAX,0x100 / MOV EAX,0x1020 before original instruction 8. Replacement overwrites bytes 16-17 (LOAD -> STORE [ebx],eax).

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b811000180                mov eax,0x80010011                              | P32-off
  13  0f22c0                    mov cr0,eax                                     | P32-off
  16  8903                      mov DWORD PTR [ebx],eax                         | P32-PAE x2
  18  b820100000                mov eax,0x1020                                  | P32-PAE
  23  0f22d8                    mov cr3,eax                                     | P32-PAE
  26  8b03                      mov eax,DWORD PTR [ebx]                         | P32-PAE
  28  b800000000                mov eax,0x0                                     | P32-PAE
  33  b800010000                mov eax,0x100                                   | P32-PAE
  38  b820100000                mov eax,0x1020                                  | P32-PAE
  43  b820100000                mov eax,0x1020                                  | P32-PAE
  48  0f22d8                    mov cr3,eax                                     | P32-PAE
  51  e9d8ffffff                jmp 0xffffffdd   (E9 rel32)                     | P32-PAE
```

#### K04. Concurrent control profile 22 (ia32e-roundtrip), generation 1 (extended program revision 1)

- **Key**: `conc22g1`  |  **Bytes**: 71  |  **Modes**: P32-off, compat, long64
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e8b03b800000000b800010000b811000100b8110001000f22c08b03`
- **Defined at**:
  - `crates/abi/src/control_concurrent.rs:76-100 (program() additions), :108-130 (source_bytes), :56-70 (replacement/offset)`
  - independent twin: tools/i4v_controls_concurrent_check.py:42-60 (owner_program)
  - host: crates/host/src/concurrent/controls.rs:445-458 (source at SOURCE_OFFSET + id*256, ljmp targets use owner id)
- **Run by**: i4v-controls-concurrent profile 22, N=1..4 owners, actor role; ljmp target in bytes shown is for owner 0 (add 0x100*id for owner id). Scheduler family "controls" uses profiles 21 and 24.
- **Notes**: Adds MOV EAX,0 / MOV EAX,0x100 / MOV EAX,0x10011 before original instruction 12. 

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat
  38  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  41  48ff2e                    ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)    | long64
  44  8b03                      mov eax,DWORD PTR [ebx]                         | compat
  46  b800000000                mov eax,0x0                                     | compat
  51  b800010000                mov eax,0x100                                   | compat
  56  b811000100                mov eax,0x10011                                 | compat
  61  b811000100                mov eax,0x10011                                 | compat
  66  0f22c0                    mov cr0,eax                                     | compat
  69  8b03                      mov eax,DWORD PTR [ebx]                         | P32-off
```

#### K05. Concurrent control profile 22 (ia32e-roundtrip), generation 2 (replacement)

- **Key**: `conc22g2`  |  **Bytes**: 71  |  **Modes**: P32-off, compat, long64
- **Hex**: `b8200600000f22e0b8000100000f30b8006000000f22d8b8110001800f22c0ea262010001800488b0348ff2e8b03b800000000b800010000b811000100b8110001000f22c08903`
- **Defined at**:
  - `crates/abi/src/control_concurrent.rs:76-100 (program() additions), :108-130 (source_bytes), :56-70 (replacement/offset)`
  - independent twin: tools/i4v_controls_concurrent_check.py:42-60 (owner_program)
  - host: crates/host/src/concurrent/controls.rs:445-458 (source at SOURCE_OFFSET + id*256, ljmp targets use owner id)
- **Run by**: i4v-controls-concurrent profile 22, N=1..4 owners, actor role; ljmp target in bytes shown is for owner 0 (add 0x100*id for owner id). Scheduler family "controls" uses profiles 21 and 24.
- **Notes**: Adds MOV EAX,0 / MOV EAX,0x100 / MOV EAX,0x10011 before original instruction 12. Replacement overwrites the final two bytes (LOAD -> STORE).

```
off   bytes                     decoded                                        | executed-in / note
   0  b820060000                mov eax,0x620                                   | P32-off
   5  0f22e0                    mov cr4,eax                                     | P32-off
   8  b800010000                mov eax,0x100                                   | P32-off
  13  0f30                      wrmsr                                           | P32-off
  15  b800600000                mov eax,0x6000                                  | P32-off
  20  0f22d8                    mov cr3,eax                                     | P32-off
  23  b811000180                mov eax,0x80010011                              | P32-off
  28  0f22c0                    mov cr0,eax                                     | P32-off
  31  ea262010001800            ljmp 0x18, 0x102026   (EA ptr16:32)             | compat
  38  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  41  48ff2e                    ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)    | long64
  44  8b03                      mov eax,DWORD PTR [ebx]                         | compat
  46  b800000000                mov eax,0x0                                     | compat
  51  b800010000                mov eax,0x100                                   | compat
  56  b811000100                mov eax,0x10011                                 | compat
  61  b811000100                mov eax,0x10011                                 | compat
  66  0f22c0                    mov cr0,eax                                     | compat
  69  8903                      mov DWORD PTR [ebx],eax                         | P32-off
```

#### K06. Concurrent control profile 24 (ia32e-aba), generation 1 (extended program revision 1)

- **Key**: `conc24g1`  |  **Bytes**: 43  |  **Modes**: P32-off, compat, long64
- **Hex**: `8b03b8110001800f22c0ea112010001800488b0348ff2e8b03b800000000b8110001000f22c0e9d5ffffff`
- **Defined at**:
  - `crates/abi/src/control_concurrent.rs:76-100 (program() additions), :108-130 (source_bytes), :56-70 (replacement/offset)`
  - independent twin: tools/i4v_controls_concurrent_check.py:42-60 (owner_program)
  - host: crates/host/src/concurrent/controls.rs:445-458 (source at SOURCE_OFFSET + id*256, ljmp targets use owner id)
- **Run by**: i4v-controls-concurrent profile 24, N=1..4 owners, actor role; ljmp target in bytes shown is for owner 0 (add 0x100*id for owner id). Scheduler family "controls" uses profiles 21 and 24.
- **Notes**: Adds MOV EAX,0 before original instruction 7. 

```
off   bytes                     decoded                                        | executed-in / note
   0  8b03                      mov eax,DWORD PTR [ebx]                         | P32-off x2
   2  b811000180                mov eax,0x80010011                              | P32-off
   7  0f22c0                    mov cr0,eax                                     | P32-off
  10  ea112010001800            ljmp 0x18, 0x102011   (EA ptr16:32)             | compat
  17  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  20  48ff2e                    ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)    | long64
  23  8b03                      mov eax,DWORD PTR [ebx]                         | compat
  25  b800000000                mov eax,0x0                                     | compat
  30  b811000100                mov eax,0x10011                                 | compat
  35  0f22c0                    mov cr0,eax                                     | compat
  38  e9d5ffffff                jmp 0xffffffda   (E9 rel32)                     | P32-off
```

#### K07. Concurrent control profile 24 (ia32e-aba), generation 2 (replacement)

- **Key**: `conc24g2`  |  **Bytes**: 43  |  **Modes**: P32-off, compat, long64
- **Hex**: `8903b8110001800f22c0ea112010001800488b0348ff2e8b03b800000000b8110001000f22c0e9d5ffffff`
- **Defined at**:
  - `crates/abi/src/control_concurrent.rs:76-100 (program() additions), :108-130 (source_bytes), :56-70 (replacement/offset)`
  - independent twin: tools/i4v_controls_concurrent_check.py:42-60 (owner_program)
  - host: crates/host/src/concurrent/controls.rs:445-458 (source at SOURCE_OFFSET + id*256, ljmp targets use owner id)
- **Run by**: i4v-controls-concurrent profile 24, N=1..4 owners, actor role; ljmp target in bytes shown is for owner 0 (add 0x100*id for owner id). Scheduler family "controls" uses profiles 21 and 24.
- **Notes**: Adds MOV EAX,0 before original instruction 7. Replacement overwrites bytes 0-1 (first LOAD -> STORE).

```
off   bytes                     decoded                                        | executed-in / note
   0  8903                      mov DWORD PTR [ebx],eax                         | P32-off x2
   2  b811000180                mov eax,0x80010011                              | P32-off
   7  0f22c0                    mov cr0,eax                                     | P32-off
  10  ea112010001800            ljmp 0x18, 0x102011   (EA ptr16:32)             | compat
  17  488b03                    mov rax,QWORD PTR [rbx]                         | long64
  20  48ff2e                    ljmp FWORD PTR [rsi]   (REX.W FF /5: m16:64)    | long64
  23  8b03                      mov eax,DWORD PTR [ebx]                         | compat
  25  b800000000                mov eax,0x0                                     | compat
  30  b811000100                mov eax,0x10011                                 | compat
  35  0f22c0                    mov cr0,eax                                     | compat
  38  e9d5ffffff                jmp 0xffffffda   (E9 rel32)                     | P32-off
```

## 3. Ambiguities, data-versus-code and attribution notes

1. **Same bytes, different modes.** `89 03`, `8b 03` appear as long-mode coherence programs (decode `mov [rbx],eax` / `mov eax,[rbx]`) and as 32-bit coherence/control atoms (`mov [ebx],eax` / `mov eax,[ebx]`). `48 8b 03` is only ever executed in `long64` (reached by control profiles 22, 23, 24, 32, 33, 34, 35, 36, 38 after the far transfer to selector 0x18, and by the long-mode programs `load3`). If decoded as 32-bit it would be `dec eax; mov ...`, so the mode must travel with every compile request (the suite does this: `bitness(mode)` in `tools/i4v_controls_worker.py`).
2. **`mixed32` "paged" mode is compatibility mode.** `crates/abi/src/mixed32.rs` calls its mode 1 "paged", but its guest state is the default seed (CR0=0x80010001, CR4=0x620, EFER=0xd00, CS=[8,0,0xffffffff,0xc09b]), i.e. IA-32e active with CS.D=1, CS.L=0 = `compat` per `modes.rs:mode()`. It translates through the 4-level walker. The address-concurrent family instead uses true legacy 2-level paging (CR4=0x600, EFER=0). Treat the `mixed_*` programs as `P32-off` and `compat`, `addr*` as `P32-off` and `P32-paged`. The `coh_*32` programs run `P32-paged` (profile 1) and `P32-PAE` (profiles 2, 4); the record-level evidence (`crates/abi/src/coherence.rs:pae()/mode()`) distinguishes them, but the instruction bytes are identical.
3. **Programs that stop early or carry bytes that are never reached.** From the independent models, the final executed instruction faults in these profiles: #GP in 3, 4, 5, 6, 17, 18, 19, 25, 26, 27, 28, 29, 31, 33, 36, 37, 38; #UD in 10, 11, 30; #NP in 32; #PF in 2 (write to a read-only page) and 34 (far-pointer read); and an instruction-fetch #PF in 7, 20 and 35 (the faulting instruction's bytes are never read by the emulator; they exist only in the source image). Profiles 32, 33, 34, 36 and 38 carry trailing instructions that are never reached (marked `never reached` in section 2.3). Profiles 0, 21 and 24 loop through `jmp rel32` (the model stops at 17, 12 and 11 visits respectively).
4. **Profile 30 (`ia32e-ea64`) executes `ea ptr16:32` in 64-bit mode on purpose** (the second `ljmp` after the first transfer succeeded) to expect #UD; listed as `ljmp ptr16:32 (EA, #UD when executed in long64)`.
5. **LOCK/66h/Mod-ignored forms are deliberate negative tests.** `f0 0f 22 c0` (LOCK MOV CR0) is #UD by emulator policy (GNU objdump prints it as `mov cr8,eax` per AMD's alternate encoding; the suite treats it as LOCK-prefixed and undefined), `0f 22 c8` is MOV CR1 (#UD), `66 0f 22 00` is MOV CR0,EAX with an operand-size prefix and a Mod field of 00 (the Mod bits are ignored).
6. **Padding and trailing bytes.** Source pages are filled with `0xcc` (INT3) around the programs (and `0x33` in the second physical page of the A20 cases; address-concurrent uses `0xcc` with programs at page offset `0x123`). None of the padding is ever executed; programs end at the resident's instruction count, not at a terminator. FEX packet words zero-pad sources up to 8/16/24/40 bytes; the zeros are not instructions.
7. **The 29-byte fence program mixes compiled and resident bytes.** FEX compiles the first 25 bytes (8 instructions, `fex_lfence_dependencies`); the final `0f ae 5f 20` (STMXCSR [rdi+0x20]) is a "semantic suffix" executed by the resident (`tools/check_i4v_fence.py:24`). The new generator must either translate it or the resident must keep ownership.
8. **REP MOVSB (`f3 a4`) is an explicitly admitted resident fixture, not generated code** (`tools/check_i3_pressure.py` docstring: "REP is an explicitly admitted resident fixture, not generated FEX REP"). It is nevertheless a guest instruction the suite executes, in `long64` with RSI=device page, RDI=`data+4095`, RCX=2, DF=0.
9. **Ordering variant `xchg` (`87 03`) is unreachable** from `ordering::Config::variant()` (profiles 0-12 map to variants 0-10; variant 11 exists in `source()/body()/image()` and in the producer CASES/testdata) and `moffs_store32` is fixture-only. They are listed so the decision to drop them is explicit.
10. **Concurrent control variants.** `ljmp` targets and `Instruction` placement depend on owner id (`0x102000 + id*0x100 + offset`); only owner 0 is shown. Profiles 0 and 3 under `i4v-controls-concurrent` have the same bytes as their single-owner counterparts; their generation 2 (replacement of `mov eax,0x10011` by `mov eax,0x11` for profile 0) is listed. Profile 3 has no generation 2.
11. **Absolute data addresses are not in the bytes.** All data accesses are register-indirect (RBX/RDX/RSI/RDI/EBX/EDX) except the moffs program (absolute 0x100000 in the instruction bytes) and the 16-bit-addressed programs (offsets derived from BX+SI).
12. **QEMU comparison** (`tools/run_i4v_qemu.py`, `tools/i4v_qemu/model.py`) runs the load, stack, xchg_result, lfence, sfence and mfence programs as ordinary x86-64 user code under Linux-user TCG; it adds only harness glue (a NASM capture sequence: register saves, `mov rsp`, `pushfq`, `lea`, `syscall`, `jmp guest`) which is not a guest program of the emulator suite.

## 4. Sources searched and excluded

- **Native AArch64 only (no x86):** I1 `control` (NOP,RET) and `fault` (UDF,RET) in `crates/host/src/main.rs`; FEX bodies in `tools/*_worker.py`/`export.py` (`body=`) and `crates/abi/src/*.rs` BODY constants; `crates/abi/src/pressure_body.arm64`; `crates/runtime/src/gates.S`.
- **Archived research, not run by the validation suite:** `experiments/` (track01 process-design probes, track03 codegen checkpoints). `experiments/track03/codegen-checkpoint/fixtures.json` lists additional source forms that are *not* used by current tools: `live_store` (`660fefc14801d14889030f94c2f30f7f07`), `bare_store` (`488903`), `bare_alias_load` (`488b00`); `bare_load` (`488b03`) and `live_load` (the XOR fragment) coincide with suite programs. `tools/check_i1.py` only cites this file in a docstring.
- **Upstream provenance copies, never executed:** `fixtures/i4v/upstream/FEX/unittests/**/*.asm` (originals with HLT and absolute addresses) and `fixtures/i4v/upstream/kvm-unit-tests/*.excerpt.c`.
- **Decoder-only unit-test byte strings (not guest programs):** `crates/abi/src/controls.rs` tests (`decode(&[0x0f,0x22,0xc0|...])` with `66`/`f0` prefixes, lines ~570-660), `crates/abi/src/modes.rs:351`, `tools/test_*.py` and `fixtures/i4v/test_expectations.py` synthetic sources.
- **Compile-only FEX probes** (`tools/i4v_producer/probe_*.py`, `tools/i4v_producer/testdata/*`) reuse the programs above; the testdata directories are the retained compilations of the same sources at the RIPs `0x102000`, `0x102080`, `0x102100`, `0x102310`, `0x2000`, `0xffff800000102000` and per-step RIPs `0x102000..0x102023`.
