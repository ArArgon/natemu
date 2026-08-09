# 23 — Can FEXCore Be Retrofitted as the JIT for a Full-System Emulator?

**Date:** 2026-08-08 · **Host:** Apple M2 Max, macOS 26.5.2 (25F84), Darwin 25.5.0
**Subject:** FEX-Emu @ `fd141ed6d721d03062619e4702bca1a0c93b6dd9` (shallow clone at `/tmp/fexread/FEX`;
HEAD is a merge commit, so no archaeology is possible from this checkout)
**Build test:** `/tmp/fexbuild/b1` — cmake 4.3.4, Homebrew clang 22.1.8, ninja. Logs
`/tmp/fexbuild/configure{1..4}.log`, `/tmp/fexbuild/build{_fexcore,2..6}.log`
**Inputs:** five raw sub-investigations at `docs/research/raw/fexcore-*.md` (unaudited; see §12),
plus `14`, `15`, `16`, `17` for the measured baselines this document reasons against.

> **Evidence classes.** Every claim below carries one:
> **MEASURED** — I ran it on this machine today. **SOURCE** — I opened the file and read the cited
> lines myself. **RAW** — asserted by one of the five prior sub-investigations and *not*
> independently re-checked by me. **CITED** — an external document. **INFERRED** — my reasoning;
> the assumption is stated inline. No estimate is ever presented as a measurement.

---

## 0. Verdict

**Do not retrofit FEXCore in place, and do not write a fresh x86→ARM64 backend. Fork FEXCore, take
its backend and its user-mode frontend, rewrite its dispatch/cache layer, and run it as a
host-process compile server rather than at bare EL1 — but do none of this yet, because the
cheaper half of the measured win is elsewhere.**

Unpacked into the four claims that carry it:

1. **The part of FEXCore that is hardest to write is the part that ports cleanly.** The ARM64
   backend emits only EL0-legal system registers (`NZCV`, `FPCR`, `CNTVCT_EL0`, `TPIDRRO_EL0`,
   `RNDR` — SOURCE, `JIT/{ALUOps,MiscOps,MemoryOps,AtomicOps}.cpp`), has no TLB, no address base
   register, and no OS calls in the emitted instruction stream. **52 of 56 FEXCore translation
   units compile on macOS/arm64** after six shim headers and eight macro defines; the four failures
   are the persistent code cache, the profiler, telemetry, and one `sincos` fallback — none of them
   the JIT (MEASURED, §7).
2. **The part that is structurally hostile is not the memory model — it is the dispatch layer and
   the single-bitness assumption.** FEXCore's "guest VA == host VA, bare `LDR [Xn]`" lowering is
   *exactly what our design wants*, because hardware stage-1 shadow paging makes guest VA the
   executing VA at EL1 (`14` G-1a). The prior dossier called this the biggest blocker; it is in
   fact the biggest asset (§4). The real blockers are (a) `Frontend.cpp:1396`'s hard assert that
   operating mode never changes at runtime, with `Config.Is64BitMode` threaded through **89 sites**
   (SOURCE, grep count), and (b) a code cache keyed on a bare 64-bit guest VA with no CPL, no
   CR3/ASID and no mode component, whose L2 is *literally* shaped as a virtual page table
   (`LookupCache.h:193-208`, RAW; dispatcher assembly `Dispatcher.cpp:182-235`, RAW).
3. **The system ISA is stubbed, not absent, and the stub sites are a finite enumerated worklist.**
   Privileged instructions are decoded and dispatched — to
   `OpDispatchBuilder::PermissionRestrictedOp`, which raises `#GP` because FEX is always ring 3
   (SOURCE, `OpcodeDispatcher.cpp:5137-5143`). There are **28 such table slots across 5 dispatch
   tables ≈ 13 distinct instruction groups** (SOURCE, §3.1). That is a mechanical worklist, not a
   structural rewrite. Conversely x87 **is** implemented at true 80-bit via SoftFloat-3e `extF80`
   (SOURCE, `X87.cpp`, `F80Fallbacks.h`, `Config.json.in:493-499`) — a large asset the corpus had
   not established.
4. **The decision is not urgent, and making it first would be a sequencing error.** `15 §8a`
   measures the software-MMU share of QEMU TCG's host time at **54.5% on a real application**
   (7-Zip) → a 2.20× win available from replacing softmmu alone, and `15 §5.1` puts the codegen
   half at 2.45×. The MMU half is available *without touching the JIT at all* and is 3–5× cheaper
   in engineering (§9). Choosing a JIT before measuring hardware-stage-1 + an unmodified TCG spends
   the expensive budget first.

### 0.1 Grading, by component

| FEXCore component | LoC | Verdict for a full-system retrofit |
|---|---|---|
| `CodeEmitter/` (ARM64 assembler) | 20,090 | **Take unchanged.** No OS coupling, no EL assumptions. |
| `JIT/` (IR → ARM64 lowering) | 13,213 | **Take, extend.** Add system-ISA lowerings; the flag/TSO machinery is the crown jewel (§10). |
| `OpcodeDispatcher/` (x86 → IR) | 11,307 | **Take for user-mode ISA; extend for system ISA.** 28 stub slots to fill. |
| `IR/` (IR + passes) | 8,995 | **Take.** IR is JSON-generated (`IR.json`), so adding ops is cheap. |
| `X86Tables/` (decoder tables) | 5,051 | **Take.** Already carries entries for privileged opcodes. |
| `LookupCache` + `Dispatcher` + block linking | ~3,000 | **Rewrite.** Wrong key, wrong geometry, wrong invalidation index (§3.4). |
| Fault reconstruction (`Core.cpp:139-250`) | ~150 | **Take the *format*, replace the delivery** (§5). |
| Allocator / VA reservation (`Utils/Allocator*.cpp`) | ~1,500 | **Discard.** Steals the upper 48-bit VA half; incompatible with a guest that owns all of it (§4.2). |
| `CodeCache/` (persistent AOT cache) | — | **Discard for v1.** Does not build on Darwin (MEASURED, §7); guest-VA-relative anyway. |

LoC counts: MEASURED (`wc -l` over `.cpp/.h/.inl`). Licence: MIT (SOURCE, `LICENSE`, and
`SPDX-License-Identifier: MIT` per file). That matters in §9.

---

## 1. What has to be true for "retrofit" to mean anything

Our design (from `14`, `16`, `17`) is: an ARM64 payload at guest **EL1** inside a
Hypervisor.framework VM; x86 4-level page tables shadowed into ARM 4 KB-granule **stage-1** tables
under our own `TTBR0_EL1`/`VBAR_EL1`; hardware TSO via `ACTLR_EL1.EnTSO`; stage-2 via `hv_vm_map`.
Translated code therefore executes **at EL1, with the guest's own virtual addresses, with the MMU
doing the translation in hardware**.

FEXCore was built for the opposite deployment: an x86-64 *userspace* process emulated inside a
Linux (or Wine/Windows) *host process* at EL0, one guest thread per host thread, guest addresses
identical to host addresses because the host kernel hands them out via `mmap`.

The five raw dossiers answered "what is FEXCore". This document answers the six questions they did
not: whether its IR can express the system ISA, whether its memory model survives the move, whether
its fault machinery survives a change of delivery mechanism, whether its threading shape matches a
vCPU, whether it can run with no OS underneath, and what it all costs.

---

## 2. Re-verification of the load-bearing prior claims

The five raw dossiers are marked RAW and unaudited. I re-opened the source for every claim my
verdict rests on. Results:

| Prior claim | Cited at | Status |
|---|---|---|
| Guest load/store lowers to bare `[Xn]`/`[Xn,#imm]`/`[Xn,Xm,ext]`; no base register exists | `JIT/MemoryOps.cpp:587-606` | **CONFIRMED (SOURCE).** `GenerateMemOperand` has exactly three return shapes and `Base.X()` is the guest pointer verbatim. |
| Pinned register set leaves no spare for a memory base | `ArchHelpers/Arm64Emitter.h:31-52` | **CONFIRMED (SOURCE).** `STATE=x28`, `TMP1-4=x0-x3`, `REG_PF=r26`, `REG_AF=r27`, `REG_CALLRET_SP=x25`, `VTMP1/2=v0/v1`, `SVE_OPT_PRED=p2`. |
| `Frontend.cpp:1396` asserts operating mode never changes at runtime | `Frontend.cpp:1393-1396` | **CONFIRMED (SOURCE).** Verbatim: `LOGMAN_THROW_A_FMT(BlockInfo.Is64BitMode == CTX->Config.Is64BitMode, "Expected operating mode to not change at runtime!");` |
| "No CPL / no privilege model anywhere" | `OpcodeDispatcher.cpp:200-202` | **PARTIALLY REFUTED (SOURCE).** There is no CPL *state*, correct — but there *is* a privilege *model*: `PermissionRestrictedOp` (`:5137`) exists precisely to raise `#GP` for ring-0 instructions, and 28 table slots bind to it. The hook points are present and enumerated. This is a materially more favourable finding than "absent". |
| FEXCore never creates a host thread | `Utils/Threads.cpp:10-21` | **CONFIRMED (SOURCE).** `CreateThread_Default` is `ERROR_AND_DIE_FMT("Frontend didn't setup thread creation!")`. |
| FEX steals the upper half of the 48-bit VA for itself | `Utils/Allocator.cpp:276-290` | **CONFIRMED (SOURCE).** `Begin48BitVA = 0x0'8000'0000'0000`, `End48BitVA = 0x1'0000'0000'0000`, `StealMemoryRegion(...)`. |
| Barrier is stored *before* the instruction is downgraded, with release ordering | `Utils/ArchHelpers/Arm64.cpp:2108-2163` | **CONFIRMED (SOURCE).** Read all four cases (LDAR/LDAPR→LDR, STLR→STR, LDAPUR→LDUR, STLUR→STUR); return values `0`/`-4`/`0`/`-4` as claimed. |
| Reserved `nop` after TSO loads, before TSO stores | `JIT/MemoryOps.cpp:799/814/829` and `:1803/1819` | **CONFIRMED (SOURCE).** All six sites, with the comment `// Half-barrier once back-patched.` |
| CF polarity tracked symbolically, rectified by `cfinv` | `OpcodeDispatcher.h:1753-1771` | **CONFIRMED (SOURCE).** `RectifyCarryInvert` reads exactly as quoted, including the non-FlagM GPR-XOR fallback. |
| AF stored as `src1^src2`, resolved against the PF byte | `OpcodeDispatcher/Flags.cpp:182-192, 227-252` | **CONFIRMED (SOURCE).** |
| PF deferred to a 3-instruction XOR cascade gated on `ReadsParity` | `JIT/ALUOps.cpp:327-346`; `Flags.cpp:174-180`; `RedundantFlagCalculationElimination.cpp:746` | **CONFIRMED (SOURCE).** |
| 2-instruction software `axflag` (`csetm`+`ccmn`) | `JIT/ALUOps.cpp:308-325` | **CONFIRMED (SOURCE).** |
| LRCPC2 disabled by MIDR for erratum 3877900 | `Source/Common/HostFeatures.cpp:440-467` | **CONFIRMED (SOURCE).** Eight part numbers listed; cross-references LLVM PR 124274. |
| `TYPE_PRIV` is an alias of `TYPE_INVALID` | `X86Tables/X86Tables.h:302-308` | **CONFIRMED (SOURCE)** — but see §3.1: only a handful of *table info* entries use it; the *dispatch* tables bind privileged opcodes to real stubs. The alias is a decoder-completeness marker, not the privilege story. |
| FEXCore has no build/include dependency on `Source/` | `FEXCore/Source/CMakeLists.txt:226-232` | **CONFIRMED indirectly (MEASURED).** `ninja FEXCore` built 52 objects with no `Source/`-side target in the dependency chain. |

**Claims I did not re-verify** and which therefore remain RAW: the `LookupCache` L1/L2 geometry and
sizing, the dispatcher's inline probe assembly, the block-link thunk layout and delinker pointer
arithmetic, the SMC/mprotect fan-out, and the varint `vl64pair` encoding details. They are
consistent with what I did read and I have no reason to doubt them, but they are second-hand.

---

## 3. IR expressiveness for system emulation

### 3.1 The stub worklist — the single most useful finding in this section

Privileged x86 instructions are **fully decoded and fully dispatched**. Their dispatch target is
`OpDispatchBuilder::PermissionRestrictedOp` (SOURCE, `OpcodeDispatcher.cpp:5137-5143`):

```cpp
void OpDispatchBuilder::PermissionRestrictedOp(OpcodeArgs) {
  BreakOp(Op, FEXCore::IR::BreakDefinition {
                .ErrorRegister = 0, .Signal = SIGSEGV,
                .TrapNumber = X86State::X86_TRAPNO_GP, .si_code = 0x80, });
}
```

That is the architecturally correct behaviour for a CPL-3 emulator, and it means the frontend
already knows which instructions are privileged and already routes them to a single replaceable
function. The complete set of binding sites (SOURCE,
`FEXCore/Source/Interface/Core/OpcodeDispatcher/*.h`):

| Table / line | Opcode range | Instructions | Slots |
|---|---|---|---|
| `BaseTables.h:29` | `0x6C–0x6F` | `INS`/`OUTS` (byte/word, rep forms) | 4 |
| `BaseTables.h:66` | `0xE4–0xE7` | `IN`/`OUT` imm8 | 4 |
| `BaseTables.h:70` | `0xEC–0xEF` | `IN`/`OUT` DX | 4 |
| `BaseTables.h:76` | `0xFA–0xFB` | `CLI`/`STI` | 2 |
| `SecondaryTables.h:9` | `0F 06–09` | `CLTS`, `SYSRET`, `INVD`, `WBINVD` | 4 |
| `SecondaryTables.h:15` | `0F 20–23` | `MOV CRn` / `MOV DRn`, both directions | 4 |
| `SecondaryTables.h:17` | `0F 30` | `WRMSR` | 1 |
| `SecondaryTables.h:19` | `0F 32–33` | `RDMSR`, `RDPMC` | 2 |
| `SecondaryModRMTables.h:15` | Grp7 `mod=3 reg=2 rm=7` | `INVLPGA` | 1 |
| `SecondaryModRMTables.h:18` | Grp7 `mod=3 reg=3 rm=0` | `SWAPGS` | 1 |
| `SecondaryGroupTables.h:13-16` | Grp6 `/3` | `LTR` (×4 prefix variants) | 4 |
| `SecondaryGroupTables.h:29-32` | Grp7 `/3` | `LIDT` (×4) | 4 |
| `SecondaryGroupTables.h:39-42` | Grp7 `/6` | `LMSW` (×4) | 4 |
| `SecondaryGroupTables.h:44-47` | Grp7 `/7` | `INVLPG` (×4) | 4 |
| `SecondaryTables.h:20` (→ `UnimplementedOp`, `#UD`) | `0F 34–36` | `SYSENTER`, `SYSEXIT` | 3 |
| `SecondaryModRMTables.h:8-9` (→ `UnimplementedOp`) | Grp7 `mod=3 reg=0 rm=0/1` | VMX entry points | 2 |

**28 `PermissionRestrictedOp` slots + 5 `UnimplementedOp` slots ≈ 13 distinct instruction groups**
after collapsing the ×4 prefix duplication. That is the entire ring-0 opcode surface FEX declined to
implement, and it is a bounded list you can work through.

Three adjacent cases that are *not* in that list but still need work (SOURCE,
`OpcodeDispatcher/BaseTables.h:58-78`, `OpcodeDispatcher.cpp:4772-4870`): `HLT` (`0xF4`), `INT1`
(`0xF1`) and `UD2` (`0F 0B`) are all bound to `INTOp`, which raises a `Break` rather than
implementing the instruction. `HLT` in particular must become "halt this vCPU until an interrupt" in
a system emulator — a scheduling operation, not a fault.

### 3.2 Per-feature verdict

Legend: **(a)** present · **(b)** absent but mechanically addable · **(c)** structurally hostile.

| Feature | Verdict | Evidence |
|---|---|---|
| **Privilege levels / CPL state** | **(b)** | No CPL field in `CPUState` (SOURCE, `CoreState.h:110-160` — segment *bases* and `_idx` selectors only). But the `gdt_segment` bitfield (`CoreState.h:133-147`) already carries `DPL`, `Type`, `S`, `P`, `L`, `D`, `G`. Adding `cpl` to `CPUState` and a `CheckCPL` IR op is small. The *hard* part is not the state, it is the ~13 instruction groups above and the CPL-changing control transfers. |
| **CPL checks on every privileged op** | **(b)** | The check sites already exist (§3.1) and currently unconditionally raise `#GP`. Making them conditional on `cpl != 0` is a one-line change per site. |
| **Segmentation with non-flat bases** | **(b), with a caveat** | Bases exist and are folded into addressing as a plain IR `Add` (`Addressing.cpp:47-49`, RAW). **ES/CS/SS/DS bases are only 32-bit wide** in `CPUState` (SOURCE, `CoreState.h:124-125`) — fine for x86 semantics, but they are *discarded entirely in 64-bit mode* (RAW, `OpcodeDispatcher.cpp:4062`) and **limits are never enforced on any access** (`CalculateGDTLimit` has one non-JIT caller, RAW). For a system emulator that must fault on limit violations in 16/32-bit protected mode, every memory op needs a bounds check that has no IR representation today. |
| **Descriptor-table loads (`LGDT`/`LIDT`/`LLDT`/`LTR`)** | **(b)** | `LIDT`/`LTR` are `PermissionRestrictedOp` stubs; `LGDT` (Grp7 `/2`) routes to `TYPE_SECOND_GROUP_MODRM` with no handler. But real descriptor-walking IR *already exists* for 32-bit `mov seg,r/m`: `UpdatePrefixFromSegment` reassembles `Base0/1/2` from a GDT entry into `*_cached` (RAW, `OpcodeDispatcher.cpp:4197-4212`). That is the template. It is base-only — no limit, type, or DPL check. |
| **Control registers CR0/CR3/CR4/EFER** | **(b)** | Zero occurrences in FEXCore outside CPUID feature bits and two comments (SOURCE, tree-wide grep). `MOV CRn` is a stub (§3.1). No CR state, no paging-mode state machine. Adding the *registers* is trivial; adding the *side effects* (CR0.PG toggling paging, CR3 write flushing the shadow tables and the code cache, CR4.PSE/PAE/LA57 changing the walk) is where the work is — and CR3-write invalidation collides with §3.4. |
| **MSRs** | **(b)** | `WRMSR`/`RDMSR` are stubs. `IA32_TSC_AUX` is faked in `RDTSCP` (SOURCE, `OpcodeDispatcher.cpp:5034`) and `FS_BASE`/`GS_BASE` exist as `fs_cached`/`gs_cached`. A model-specific-register file plus `STAR`/`LSTAR`/`CSTAR`/`SFMASK`/`EFER` is new but self-contained. |
| **IN/OUT (port I/O)** | **(b)** | Stubs (`0xE4-0xE7`, `0xEC-0xEF`, plus `INS`/`OUTS`). Needs a new IR op that exits to the device model. Straightforward, and `FLAGS_BLOCK_END` is already set on all of them (SOURCE, `BaseTables.cpp:380-389`) so the block-splitting is already right. |
| **`INT n` / IDT vectoring** | **(c) — structurally hostile** | `INTOp` (SOURCE, `OpcodeDispatcher.cpp:4772-4800`) is hard-wired to *host-OS syscall detection*: `int 0x80` → `SyscallOp` on Linux, `int 0x2E` → `SyscallOp` on Windows, everything else → a `Break` that raises a host signal. There is no IDT, no vector table, no error-code push, no stack switch, no IST. This is not a stub to fill; it is a control-transfer mechanism that does not exist. |
| **`IRET`** | **(c)** | `IRETOp` **is** implemented (SOURCE, `OpcodeDispatcher.cpp:178-215`) and pops RIP/CS/EFLAGS/RSP/SS — i.e. exactly the *userspace signal-frame return* shape. Its own comment at `:201-202` reads *"RSP and SS only happen in 64-bit mode or if this is a CPL mode jump! FEX doesn't support a CPL mode switch"*. Returning to a *different* CPL, with the associated stack switch and descriptor checks, is absent by design. |
| **`SYSCALL`** | **(b)/(c)** | Bound to `SyscallOp` in 64-bit and to `NOPOp` in 32-bit (SOURCE, `SecondaryTables.cpp:29-30`). `SyscallOp` emits an `_Syscall` IR op that lowers to a **devirtualized vtable call into the embedder** (RAW, `JIT/BranchOps.cpp:312-322`; `JIT.cpp:660-662`). For a system emulator, `SYSCALL` must instead load `LSTAR`, mask `RFLAGS` with `SFMASK`, load CS/SS from `STAR`, and switch to CPL 0 — an entirely different lowering. **(b)** for the mechanism (an IR op with a new lowering), **(c)** for the fact that the ring transition it implies is what `Frontend.cpp:1396` forbids. |
| **`SYSENTER`/`SYSEXIT`** | **(b)** | `UnimplementedOp` → `#UD`. Nothing to preserve; write from scratch. |
| **`SYSRET`** | **(b)** | `PermissionRestrictedOp` stub. |
| **x87 to 80-bit precision** | **(a) — present, and this is a major asset** | `OpSize::f80Bit` is a first-class IR size (SOURCE, `OpcodeDispatcher/X87.cpp:79-212`); arithmetic goes through SoftFloat-3e `extF80` via 54 enumerated fallback handlers (SOURCE, `CoreState.h:271-329`), with FCW-driven rounding/precision state (SOURCE, `Interpreter/Fallbacks/F80Fallbacks.h:12-61`). A 64-bit fast path exists but is **opt-in and off by default**: `X87ReducedPrecision`, default `false`, described as *"reduces emulation accuracy and may result in rendering bugs"* (SOURCE, `Config.json.in:493-499`). Writing correct 80-bit x87 from scratch is a multi-person-month job; this is free. |
| **MMX** | **(a)** | 123 `FLAGS_SF_MMX*` table entries (MEASURED, grep count); `mm[8][2]` in `CPUState` (SOURCE, `CoreState.h:130`). |
| **A20 gate** | **(b), trivial** | Zero occurrences tree-wide (MEASURED, case-insensitive grep). It is an address mask; in our design it belongs in the shadow-page-table walker, not in the JIT at all. |
| **Real mode / 16-bit / big-real / vm86** | **(c) — the hardest single item** | Zero occurrences of `real.?mode`, `vm86`, or `16.?bit mode` in `Frontend.cpp` or `OpcodeDispatcher.cpp` (MEASURED, grep). `Is64BitMode` is a **boolean**, not a mode enum, and it appears at **89 sites** in `FEXCore/` (MEASURED, grep count). It selects: GPR operand size (`OpcodeDispatcher.h:1255`, RAW), the static-register file (`Arm64Emitter.cpp:387-403`, RAW), and the decoder tables. Turning that boolean into a runtime mode with three or four values, and making the *code cache* key on it, is the single largest structural change in this document. |
| **Protected-mode boot path** | **(c)** | Follows from the above: booting an x86 machine means executing 16-bit real mode, then 32-bit protected, then 64-bit long mode, in one process, with mode changes at runtime. That is precisely what `Frontend.cpp:1396` asserts against. |

### 3.3 Is the IR itself expressive enough?

**Yes, and this is not the constraint (SOURCE + INFERRED).** The IR is generated from
`FEXCore/Source/Interface/IR/IR.json` by `Scripts/json_ir_generator.py` at build time (SOURCE,
`FEXCore/Source/CMakeLists.txt:149`). Adding an op is a JSON edit plus a `DEF_OP` in the backend.
The IR already carries the shapes a system ISA needs: side-effecting ops with block-end semantics
(`_Syscall`, `_Thunk`, `_Break`), context load/store at arbitrary `CPUState` offsets
(`_LoadContextGPR`/`_StoreContextGPR`), an inline-constant mechanism, and per-op read/write flag
masks used by the dead-flag pass (`RedundantFlagCalculationElimination.cpp:277-401`, RAW).

The IR's *gap* is not expressiveness but **the absence of a "this op may fault and must be
restartable" contract**, which is §5's problem, and the absence of any notion that the meaning of an
op depends on a mode, which is §3.2's problem.

### 3.4 The dispatch and cache layer is the real structural hostility

This is where I disagree in emphasis with the raw dossiers, which foreground the memory model.

- The lookup key is a bare `uint64_t` guest VA at **all three cache levels**, and the tag compare in
  the dispatcher is a single `sub`/`cbnz` in hand-written ARM64 (RAW, `Dispatcher.cpp:215`;
  `BranchOps.cpp:210`). Adding a mode/CPL/CR3 component means widening `LookupCacheEntry` past the
  16-byte `ldp` fast path and editing generated assembly in at least three places.
- L2 is indexed `(VA & (VirtualMemSize-1)) >> 12` then `VA & 0xFFF` — *literally a shadow virtual
  page table* (RAW, `LookupCache.h:193-208`). For a system emulator you want a physically-indexed
  cache so that a block survives a CR3 switch and dies when its *physical* page is written. That
  structure is not reusable.
- Invalidation is keyed by guest VA page (`CodePages` is `map<VA>>12, vector<block RIPs>>`, RAW,
  `LookupCache.h:91`). A physical invalidation index does not exist.
- Every block-link record embeds an **absolute guest RIP literal** (RAW, `JIT.cpp:995`), and the
  delinkers do `Record - 0x10` pointer arithmetic (RAW, `JIT.cpp:501/519/559`).
- The one existing "mode-like" key, the trap flag, is handled by **bypassing the cache entirely**,
  not by keying it (RAW, `JIT.cpp:537-540`; `Dispatcher.cpp:172-173`). That is a designed-in signal
  that FEX's authors avoided a mode key deliberately.

**INFERRED:** at ~3,000 LoC, this layer is cheaper to rewrite than to retrofit, and rewriting it is
also where you would introduce the physical-page invalidation index and the per-CR3 generation
counter that a system emulator needs anyway. Treat it as new code, not as ported code.

---

## 4. Memory-model retrofit

### 4.1 The good news, which the prior dossier framed backwards

FEXCore lowers every guest memory access to a bare ARM64 load/store on a register holding the raw
guest pointer — no base add, no mask, no TLB probe, no page-table walk (SOURCE,
`JIT/MemoryOps.cpp:587-606`, read in full). The dossier `fexcore-a9b7db7e5866a73e2.md` calls this
"the biggest blocker" for a system retrofit, on the assumption that a system emulator must insert
address translation.

**Our design does not.** `14` G-1a closed by experiment: a 4-level, 4 KB-granule stage-1 table with
`T0SZ=16` works at guest EL1, and `14` G-1b showed 4 KB permission enforcement inside a single 16 KB
host page. Translated code at EL1 therefore executes with **guest virtual addresses translated by
hardware**. `LDR Xd, [Xn]` where `Xn` holds a guest VA is not merely acceptable — it is the whole
point of the design.

So the quantitative answer to "how close is it?": **the instruction selection is already exactly
right, and the register allocator needs no change** (the raw dossier's "no spare pinned register for
a TLB base" concern evaporates, because there is no TLB base). The three tiers of TSO load lowering
(`ldapur`/`ldapr`/`ldar` at `MemoryOps.cpp:779-830`, SOURCE) also become *unnecessary* under
hardware TSO — `16 §20-21` measured `EnTSO=1` at 0% on loads and +7–8% on DRAM-bound stores, versus
FEX's own ranking of hardware TSO as strictly better than every software tier (RAW,
`FEXGetConfig/Main.cpp:306-345`). FEXCore already has the switch: `Context.h:458-469` turns all
software TSO emulation off when `SupportsHardwareTSO` (RAW). **Set it and delete the ladder.**

### 4.2 The bad news: coexistence, not addressing

The problem is not *where guest data lives*; it is *where the runtime lives*. FEXCore needs, per
process and per thread, a large amount of its own virtual address space:

| Allocation | Size | Source |
|---|---|---|
| Shared JIT code buffer (grows geometrically) | up to **128 MiB** (`MAX_CODE_SIZE`) | SOURCE, `SharedCodeBufferManager.cpp:17` |
| `LookupCache` L1+L2+pool, **per thread** | `VirtualMemSize/4096*8 + 128 MiB + up to 16 MiB` ≈ **272 MiB** at the default `VirtualMemSize = 1<<36` | RAW, `LookupCache.cpp:27`, `.h:452-456`; `Context.h:339` |
| Call-return shadow stack, per thread | **4 MiB** (`CALLRET_STACK_SIZE = 0x400000`) | SOURCE, `InternalThreadState.h:112` |
| `InternalThreadState` + `InterruptFaultPage` | page-aligned struct + 1 guard page, and the fault page must be within **65520 bytes** of `BaseFrameState` for a single-instruction store | SOURCE, `InternalThreadState.h:90, 122-131` |
| JIT guard page, host C++ heap, host stack | unbounded | SOURCE, `InternalThreadState.h:115-117` |

FEX solves this on Linux by **stealing the entire upper half of the 48-bit VA** (SOURCE,
`Allocator.cpp:276-290`: `0x0000_8000_0000_0000` … `0x0001_0000_0000_0000`) so that guest
allocations stay low, and by confining 32-bit guests below 4 GiB with a separate allocator.

**That trick is unavailable to us.** A full-system x86-64 guest owns *both* canonical halves: user
space low, kernel space at `0xFFFF_8000_0000_0000`+. There is no free region.

Options, with costs:

1. **Put the runtime behind `TTBR1_EL1`.** Attractive at first glance — ARM gives you a second
   translation regime for free — but the x86-64 guest's kernel half maps to exactly the ARM top-half
   VAs that `TTBR1` covers, so the collision is unchanged. **Does not work** (INFERRED; the
   assumption is that we must present the guest an unmodified canonical 48-bit x86 VA layout).
2. **Give the runtime its own ASID and switch `TTBR0_EL1` on every entry/exit to runtime code.**
   `16 §13` measured `MSR TTBR0_EL1` + `ISB` at **10.3 ns**, and `16 §14` measured TLB refill after
   an ASID switch at 20.4–22.5 ns per 4 KB page touched. INFERRED: acceptable if the runtime is
   entered rarely (compile, fault, device exit) but ruinous if entered per block. Since block
   linking means the steady state never re-enters the dispatcher (RAW, `BranchOps.cpp:212-231`),
   this is plausible — but it means the JIT's own data structures are unreachable from translated
   code, which breaks `LookupCache`'s inline L1 probe at block ends.
3. **Paravirtualized VA hole.** Reserve, say, 512 GiB of guest VA and require the guest kernel not
   to use it. This is the `07`-style negotiated PV layer. Cheap and effective for a cooperating
   Linux guest; fatal for the "boot arbitrary x86 firmware/Windows" product case that `08 §143`
   says is the only defensible one.
4. **Move the runtime out of the guest VA space entirely — the compile-server split (§7.3).**
   Dissolves the problem completely.

**INFERRED conclusion for §4:** option 4 is the only one that is both correct for an arbitrary guest
and free of a per-block cost. This is the strongest single argument for the split-JIT architecture,
and it is an argument the corpus has not previously made.

### 4.3 One thing that *does* have to change in the memory path

`Frontend.cpp`'s multiblock decoder reads guest instruction bytes **directly through the host
address space** while compiling. With shadow paging, the compiler must instead walk the guest's
page tables to fetch instruction bytes, and must handle a page fault *during decode* (an x86
instruction can straddle a page boundary where the second page is not present). FEX has an
analogous hook — `SyscallHandler::QueryGuestExecutableRange` (RAW,
`FEXCore/include/FEXCore/HLE/SyscallHandler.h:65`) — which is one of only three pure-virtual methods
an embedder must implement. **(b), mechanically addable**, and the interface for it already exists.

---

## 5. Fault and exception retrofit

### 5.1 The delivery mechanism is not in FEXCore, so there is nothing to port

All `sigaction` installation, `ucontext` handling and signal dispatch lives in
`Source/Tools/LinuxEmulation/LinuxSyscalls/SignalDelegator.cpp` — the *frontend* (RAW). FEXCore
itself installs no handlers; `FEXCore::SignalDelegator` has **zero pure-virtual methods** and the
Windows backend passes a `DummySignalDelegator` (RAW). Empirically, FEX already ships a backend
where faults arrive as Windows SEH rather than POSIX signals and FEXCore is unchanged.

**INFERRED:** taking faults as ARM exceptions at EL1 under our own `VBAR_EL1` is a third delivery
mechanism of the same kind. There is no FEXCore code to modify for that; there is embedder code to
write.

### 5.2 The reconstruction *format* is delivery-agnostic and reusable

`ContextImpl::RestoreRIPFromHostPC(Thread, HostPC)` (RAW, `Core.cpp:139-175`) takes a raw host PC
and returns a guest RIP. It reads `Frame->State.InlineJITBlockHeader` — a `CPUState` field the JIT
stores on **every block entry** in two instructions (RAW, `JIT.cpp:794-798`) — then linearly walks a
varint `(hostPCdelta, guestRIPdelta)` table appended to the block. **No `ucontext`, no signal, no
OS involvement.** It is directly callable from an EL1 exception handler with `ELR_EL1` as the
argument.

Guest GPR/XMM values need no reconstruction at all: they live permanently in fixed host registers
under FEX's static register allocation (RAW, `Arm64Emitter.cpp:47-99`). Recovering them is a copy
out of wherever the fault handler spilled them — `ucontext` on Linux, our own spill area at EL1.
`17 §0.2` item 8 measured that permutation at **+0.3 ns, i.e. free**.

### 5.3 Cross-check against `17`: the two documents agree, which is the surprise

`17 §0.1`'s conclusion is that the *hot* fault — a shadow-page-table miss — needs **no** state
reconstruction whatsoever (fill and retry, invisible to the guest, 145.8 ns), and only the rare
*guest-visible* fault reconstructs (+28 ns).

FEXCore's metadata format was chosen on exactly that assumption. Verbatim from the tree (RAW,
`JIT.cpp:1025-1026`): *"This is a trade-off between compression inside the JIT code space and
execution time when reconstructing the RIP. RIP reconstruction when faulting is less likely so we
are requiring the accumulation."*

The raw dossier `fexcore-abeea3f01b82244f7.md` reads that comment as a threat: *"the format was
chosen on the explicit assumption that faults are rare"*. **Under `17`'s two-tier model that
assumption is correct**, because shadow misses never reach the reconstruction path. I judge the
dossier's framing here to be wrong, and this is the one place where reading `17` changes the
conclusion.

Quantitatively, FEX's linear varint walk is *cheaper* than the QEMU mechanism `17` measured:

| Mechanism | Cost | Source |
|---|---|---|
| QEMU `cpu_unwind_data_from_tb`, sleb128 linear scan, 64-insn TB | 118–125 ns | `17 §0.2` item 14, MEASURED |
| StackMap binary search, N=4096, on the real fault path | ≈29 ns | `17 §0.2` item 12, MEASURED |
| Open-addressed hash, N=1K–64K, warm | 2.5–3.7 ns | `17 §0.2` item 13, MEASURED |
| **FEXCore: `InlineJITBlockHeader` anchor + varint walk of entries before the fault** | **not measured; O(instructions-before-fault) with one dependent branch per entry, but with no table lookup at all because the block is already identified** | RAW + INFERRED |

**INFERRED:** FEXCore's design sits between QEMU's and the hash — it pays 2 instructions per block
entry on the *fast* path (which `17 §0.2` item 3 measured as *below noise* for a single store) to
avoid any search for "which block", then walks linearly within the block. That is a defensible
point on the curve and I would keep it. **UNRESOLVED:** its actual per-fault cost. The experiment
is a microbenchmark that calls `RestoreRIPFromHostPC` at a known offset into blocks of 8/32/128
guest instructions, on this host, and compares against `17`'s items 11–14.

### 5.4 Three things that genuinely break

1. **`REP` string operations are non-restartable by construction.** Verbatim (RAW,
   `OpcodeDispatcher.cpp:3233-3235`): *"FEX doesn't support partial faulting REP instructions… If
   FEX is to gain support for faulting REP instructions, then this implementation needs to change
   significantly."* `REP MOVS`/`REP STOS` become single `_MemCpy`/`_MemSet` IR ops lowered to a
   host-side loop whose cursors live in `TMP1`/`TMP2` — **not** static registers, so a fault
   recovers the *pre-instruction* RCX/RDI/RSI while memory has been partially written. In a system
   emulator, a `REP MOVS` straddling into an unmapped or MMIO page is routine. **Must be rewritten.**
   **(c).**
2. **MMIO that returns a value into a guest register has no supported path.** `RestoreThreadState`
   contains the admission (RAW, `SignalDelegator.cpp:266-270`) that modifying JIT-resident register
   state from a fault handler *"might result in tearing without real state reconstruction"* and is
   unimplemented. Under SRA, writing a guest GPR from a handler means writing the right host
   register in the spill frame — solvable, but it is new machinery, and the `IgnoreMask` wrinkle for
   partially-spilled syscall frames (RAW, `SignalDelegator.cpp:303-312`) shows the edge cases are
   real. **(b), non-trivial.**
3. **The register cache flush that makes faults precise is documented as an accident.** Verbatim
   (RAW, `Core.cpp:622-634`): *"Do a partial register cache flush before every instruction… it makes
   our exception handling behaviour more predictable. It is potentially correctness bearing in that
   sense, but that is a side effect here."* A system emulator needs that property as an **enforced
   invariant**, not a side effect of a register-allocation heuristic. `17 §0.3` shows the cheap way
   to get it (RIP-only materialisation, ≤0.09 ns, below noise) rather than the expensive way
   (materialise everything, +170–241% MEASURED). **(b) — but it must be made explicit and tested.**

### 5.5 The unaligned-atomic backpatcher needs a new fault discriminator

FEX distinguishes "host misalignment" from "guest fault" purely on `si_code == BUS_ADRALN` plus
`IsAddressInCodeBuffer` (RAW, `SignalDelegator.cpp:963-983`). At EL1 the equivalent is `ESR_EL1`
`EC=0x25` with `DFSC = 0b100001` (alignment fault) versus the permission/translation codes `14`
G-1b showed us decoding. That is a *cleaner* discriminator than the signal one, not a worse one
(INFERRED). Under hardware TSO the whole mechanism may also be unnecessary — but keep it, because
`LOCK`-prefixed unaligned RMW still needs it (§10.4).

---

## 6. Threading and SMP

**The shape matches.** FEXCore never creates a host thread (SOURCE, `Utils/Threads.cpp:10-21` —
the default hook is `ERROR_AND_DIE_FMT`). `Context::ExecuteThread(Frame)` runs the dispatcher on
*whatever host thread calls it* (RAW, `Core.cpp:383`). `InternalThreadState` contains no OS handle,
no TID, no `pthread_t` (SOURCE, `InternalThreadState.h:90-131`). One guest vCPU = one host thread
inside a VM is the same shape as one guest thread = one host thread; the embedder supplies the
thread either way.

What does not survive the move to bare EL1:

| Dependency | Where | Assessment |
|---|---|---|
| `WritePriorityMutex` (guards `GuestToHostMap`, taken shared on every compile) | `include/FEXCore/Utils/WritePriorityMutex.h:7,9` — `linux/futex.h`, `sys/syscall.h`, 4 raw `syscall(SYS_futex,…)` sites, all `#ifndef _WIN32` | **Must be replaced.** No futex at EL1. Replace with a WFE/SEV spin or a scheduler-free ticket lock. ~100 LoC. |
| `InterruptableConditionVariable` | `include/FEXCore/Utils/InterruptableConditionVariable.h:9-16` | Same. |
| `SignalScopeGuards` deferred-signal machinery, `syscall(SYS_rt_sigprocmask,…)` | `include/FEXCore/Utils/SignalScopeGuards.h:161,174` | At EL1, "mask signals" becomes `DAIFSet`. Cheaper, but a rewrite. |
| `SpinWaitLock` (per-block backpatch mutex) | `include/FEXCore/Utils/SpinWaitLock.h` — `LoadExclusive`/`WFELoadAtomic` | **Works as-is at EL1.** Pure `LDAXR`/`WFE`, no OS. |
| `InterruptFaultPage` + `VirtualProtect` for safe-point interruption | `InternalThreadState.h:125`; `Dispatcher.cpp:277-279` (RAW) | **Works, and arguably better.** At EL1 "reprotect a page" is our own stage-1 PTE edit. `16 §8` measured a single-page TLBI + barriers at **279–285 ns**, so arming/disarming costs ~0.3 µs. Compare: the vCPU IPI `02` measured at 3.1 µs. |
| Any libc call from *generated code* | — | **None.** Generated code calls only: the syscall-handler vtable slot, `CPUID`/`XCR` handlers, `ExitFunctionLink`, `LUDIV`/`LDIV`, and ~54 x87/SSE4.2 fallback handlers (SOURCE, `JIT.cpp:641-666`; `CoreState.h:271-329`). All are C++ functions in the runtime, none is a libc entry point. |
| TLS in generated code | — | **None.** The thread pointer is `x28` = `STATE`, planted by the dispatcher. `TPIDRRO_EL0` is read only for the `RDTSCP` CPU index, gated on `SupportsCPUIndexInTPIDRRO` (SOURCE, `JIT/MiscOps.cpp:233-239`). |

**Cross-vCPU coherence is a genuinely new problem, not a ported one.** FEX shares one code buffer
across threads and relies on x86's own coherence plus per-block spinlocks (RAW). A system emulator
must additionally handle guest `INVLPG`/TLB-shootdown IPIs invalidating *another* vCPU's shadow
tables and code cache. `15 §7.2` measured 400×`fork`+`exec` at **≈31× slower than HVF and identical
under aarch64-on-aarch64 TCG** — i.e. address-space-switch cost is the worst measured workload and
is *not* a codegen problem. No JIT choice helps there.

---

## 7. Bare-EL1 viability — and the compile-server alternative

### 7.1 Build test (MEASURED)

Out-of-tree build of FEX @ `fd141ed` on macOS 26.5.2 / arm64, Homebrew clang 22.1.8, cmake 4.3.4,
ninja. Nothing was written into the repo; everything is under `/tmp/fexbuild`.

**Configure — three hard stops, all in the build system, none in the code:**

| # | Failure | Cause | Workaround |
|---|---|---|---|
| 1 | `CMake Error at Source/Tools/CMakeLists.txt:7 (find_package): Could not find a package configuration file provided by "Qt5"` | `BUILD_FEXCONFIG` defaults `TRUE` | `-DBUILD_FEXCONFIG=FALSE` |
| 2 | `CMake Error at unittests/ASM/CMakeLists.txt:1 (enable_language): No CMAKE_ASM_NASM_COMPILER could be found` | unittests gated on `BUILD_TESTING`, which cmake defaults ON | `-DBUILD_TESTING=OFF` |
| 3 | `CMake Error at CMakeLists.txt:509 (string): string sub-command STRIP requires two arguments` — preceded by `Scripts/aarch64_fit_native.py` and `Scripts/NeedDisabledSVE.py` both dying on `FileNotFoundError: '/proc/cpuinfo'` | `TUNE_CPU` defaults to `"native"`, whose arm64 path shells out to two Python scripts that **read `/proc/cpuinfo` unconditionally** | `-DTUNE_CPU=apple-m1` |

With those three flags, **configure succeeds** (`EXIT=0`, `/tmp/fexbuild/configure4.log`). Note also
that all five `HAS_SYSCALL_*` feature probes report *Failed* on Darwin — FEX handles that
gracefully.

**Compile — five distinct classes of source-level blocker, all in support code:**

| Round | Error | Fix applied |
|---|---|---|
| 1 | `AllocatorHooks.h:9: fatal error: 'malloc.h' file not found` | shim `malloc.h` → `<stdlib.h>` + `<malloc/malloc.h>` + `malloc_usable_size` ⇒ `malloc_size` |
| 2 | `MADV_HUGEPAGE` / `MADV_NOHUGEPAGE` undeclared; `SYS_rt_sigprocmask` undeclared; `linux/futex.h` not found | `-D` defines + shim `linux/futex.h` |
| 3 | `linux/limits.h`, `sys/prctl.h`, `syscall.h` not found; `FUTEX_WAIT_BITSET`/`FUTEX_WAKE_BITSET` undeclared | three shim headers + two defines |
| 4 | `SYS_getcpu`, `SYS_tgkill`, `SYS_renameat2`, `sendfile` undeclared | four numeric defines + a shim `sys/sendfile.h` |
| 5 (final) | `mremap`/`MREMAP_*` (no Darwin equivalent); `sincos` (Darwin has `__sincos`); `SpinWaitLock::LoadExclusive` overload resolution failing because Darwin's `size_t` is `unsigned long` and `uint64_t` is `unsigned long long` | **not fixed** |

**Result: `ninja -k 0 FEXCore` compiled 52 of 56 `FEXCore_object` translation units** (MEASURED,
`/tmp/fexbuild/build6.log`). The four failures:

| Failing TU | Reason | Needed by the JIT? |
|---|---|---|
| `Interface/Core/CodeCache.cpp` | `mremap`+`MREMAP_DONTUNMAP`, `sendfile`, and the LP64 `size_t`/`uint64_t` divergence | **No** — persistent AOT cache, optional |
| `Utils/Profiler.cpp` | `linux/magic.h`, `statfs` | **No** — profiler |
| `Utils/Telemetry.cpp` | `sendfile` (via `FEXHeaderUtils/Filesystem.h:219`) | **No** |
| `Interface/Core/Interpreter/Fallbacks/InterpreterFallbacks.cpp` | `sincos` (Darwin spells it `__sincos`) | **Yes, but trivially** — one symbol |

Everything in `Interface/Core/JIT/`, `Interface/Core/OpcodeDispatcher/`, `Interface/IR/`,
`Interface/Core/{Frontend,Core,Dispatcher,LookupCache,Addressing,CPUID,CPUBackend}.cpp` built
clean.

**Interpretation (INFERRED from a MEASURED result):** "FEXCore is C++ with libc/STL and therefore
can't run bare" is too pessimistic. The libc surface it actually touches in the JIT path is
`malloc`/`free`, `mmap`/`mprotect`, `futex`, `sigprocmask`, and libm's `sincos`. FEX already
demonstrates the endpoint: `Source/Windows/WOW64/CMakeLists.txt` links the same
`$<TARGET_OBJECTS:FEXCore_object>` with `-static -nostdlib -nostartfiles -nodefaultlibs -lc++
-lc++abi -lunwind` against a hand-rolled musl-derived CRT (RAW). **A bare-EL1 FEXCore is a port of
that CRT, not a rewrite of FEXCore.** I estimate that at 2–4 person-months (INFERRED; the
assumption is that `Source/Windows/Common/CRT/`'s ~1,000 LoC is representative of what a
freestanding EL1 CRT needs, which understates the allocator work).

`-fno-exceptions` is already the default (`/tmp/fexbuild/build6.log` command lines, MEASURED), which
removes the single largest freestanding-C++ obstacle.

### 7.2 What still argues against running FEXCore at bare EL1

1. **§4.2's coexistence problem is unsolved.** ~400 MiB of runtime VA per thread inside an address
   space the guest owns entirely.
2. **The C++ heap.** FEXCore allocates during compilation — IR nodes, `robin_map` growth,
   `fextl::vector`s. A general-purpose allocator at EL1 is real work, and its failure modes
   (fragmentation under a long-running guest) are the kind that surface after six months.
3. **Debuggability.** A JIT bug at EL1 inside a VM with no OS underneath is debugged by staring at
   registers. The same bug in a macOS process is debugged with lldb, ASan and a REPL.
4. **The rebase tax.** FEX merges upstream PRs continuously (HEAD here is merge PR #5811). Any
   invasive change to `Allocator.cpp`, `Threads.cpp`, `WritePriorityMutex.h` — the exact files a
   bare-EL1 port must rewrite — is a permanent merge conflict.

### 7.3 The compile-server split — serious analysis

**Architecture.** FEXCore runs unmodified in the *host* macOS/arm64 process. It compiles guest
blocks into a buffer that is `hv_vm_map`'d into the guest's IPA space as RX. Guest EL1 executes
those blocks with guest VAs through our shadow stage-1 tables. On a code-cache miss the guest exits
to the host (HVC), the host compiles, the guest resumes.

**Why it is attractive — five arguments, in descending strength:**

1. **It dissolves §4.2 entirely.** The JIT's 128 MiB code buffer, its 272 MiB/thread lookup cache,
   its heap, its call-ret stack and its `InternalThreadState` all live in *host* VA. Only the
   generated code and a small execution-side control block need guest-visible addresses. This is
   the argument that decides it. (INFERRED, but the premise — that FEXCore's own allocations are
   ~400 MiB/thread and that the guest owns both canonical halves — is SOURCE.)
2. **Zero porting cost for FEXCore.** §7.1 measured that 52/56 TUs already build on this exact host.
   Fix `sincos`, drop the code cache, and you have a working library.
3. **W^X disappears.** On Apple Silicon a process that writes its own JIT pages must use `MAP_JIT`
   and `pthread_jit_write_protect_np`, and `04` lists cheap W^X as one of Rosetta's transferable
   wins. Here the host writes a plain RW mapping and the guest sees it RX through stage-2. There is
   no per-thread W^X toggle in the fast path at all. (INFERRED; the mechanism is `hv_vm_map`
   protection flags, which `02` established.)
4. **Codegen is not on the steady-state fast path.** `15 §4` measured translation/codegen at
   **0.0–0.1%** of host samples in every steady-state workload, and 12.7% only during boot. The
   exit cost is therefore paid where it is affordable.
5. **The generated code is already EL-agnostic.** The JIT emits only `NZCV`, `FPCR`, `CNTVCT_EL0`,
   `CNTVCTSS_EL0`, `TPIDRRO_EL0`, `RNDR`/`RNDRRS` — all EL0-accessible and therefore also EL1-legal
   (SOURCE, grep over `JIT/*.cpp`). Nothing FEXCore emits assumes EL0.

**Why it is not free — the costs, quantified where possible:**

1. **Every host callout from generated code becomes an 808 ns VM exit** (`02 §6.1`, MEASURED
   floor). The callout set is enumerable (SOURCE, `JIT.cpp:641-666`; `CoreState.h:271-329`):

   | Callout | Frequency | Mitigation |
   |---|---|---|
   | ~54 x87 / SSE4.2 `extF80` fallback handlers | every x87 arithmetic op | **Compile SoftFloat-3e + cephes to a position-independent blob and map it into the guest.** Both are freestanding C with no OS dependency. Essential — `15`'s 7-Zip has no x87, but real guest code does, and `04` measured Rosetta at only 6% of native on x87 precisely because this path is expensive even when local. |
   | `LUDIV`/`LDIV` (128-bit divide) | every wide `DIV` | Same blob. |
   | `CPUID` / `XCR` handlers | rare | Make table-driven in-guest, or accept the exit. |
   | `SyscallHandler` vtable slot | replaced entirely by real `SYSCALL`/`SYSENTER` semantics in a system emulator | N/A |
   | **`ExitFunctionLink`** | **every not-yet-linked branch** | **This is the one that matters.** Split it: the *lookup* ("is the target compiled?") must run in-guest against a guest-visible cache; only the *compile* exits. Requires the §3.4 dispatch rewrite anyway. |

2. **Cross-VM code patching needs a protocol.** FEX backpatches generated code in three places:
   block linking (`JIT.cpp:577-608`, RAW), delinking on invalidation (`JIT.cpp:500-529`, RAW), and
   unaligned-atomic downgrade (`Arm64.cpp:2108-2163`, SOURCE). If the writer is the host and the
   executor is the guest, the host's `ClearICache` does not invalidate the *guest's* I-cache.
   **UNRESOLVED.** The experiment: host writes a new instruction into an `hv_vm_map`'d RX page while
   a vCPU is executing nearby; measure whether the guest observes the old instruction, and whether a
   guest-side `IC IVAU` + `DSB ISH` + `ISB` (or a `TLBI`-free stage-2 remap) is sufficient. This is
   a half-day probe and it is the biggest single unknown in the split design.
3. **Backpatching under concurrency gets harder, not easier.** FEX's per-block `SpinLockFutex` lives
   *inside the JIT block tail* (RAW, `CPUBackend.h:89-90`) and is taken by the faulting thread. In a
   split design the faulting party (guest vCPU) and the patching party (host) are in different
   address spaces and different privilege regimes.
4. **You still have to do everything in §3.** The split is orthogonal to system-ISA support.

**Verdict on the split (INFERRED):** it is the right v1 architecture, on the strength of argument 1
alone, but it is contingent on the cross-VM I-cache experiment in cost 2. If that experiment fails,
the fallback is a bare-EL1 port (§7.1 says that is a CRT port, not a rewrite) with a
paravirtualized VA hole (§4.2 option 3).

---

## 8. Summary table: the six questions

| # | Question | Answer | Class |
|---|---|---|---|
| 1 | Can the IR express the system ISA? | Yes; the IR is JSON-generated and extensible. 13 instruction groups are stubbed at enumerated sites (§3.1). Structurally hostile: runtime mode switching (89 sites), IDT vectoring, CPL-changing `IRET`, real mode. x87 80-bit is **present**. | SOURCE |
| 2 | Does the memory model retrofit? | The *lowering* is already exactly right (bare `LDR [Xn]` on a guest VA = what hardware stage-1 gives us). The *runtime's own ~400 MiB/thread of VA* is the problem, and FEX's solution (steal the upper 48-bit half) is unavailable. | SOURCE + INFERRED |
| 3 | Does the fault machinery survive a delivery change? | Yes — reconstruction lives in FEXCore and is `ucontext`-free (`RestoreRIPFromHostPC`); delivery lives in the frontend and is already pluggable (Windows SEH proves it). Consistent with `17`'s two-tier model. Breaks: `REP`, MMIO-into-register, the accidental precise-fault invariant. | RAW + SOURCE |
| 4 | Is one-vCPU-per-thread the same shape? | Yes. FEXCore creates no threads and stores no OS handles. Replace futex locks, `sigprocmask`, and `VirtualProtect`-based safe points; `SpinWaitLock` works as-is. Cross-vCPU shootdown is new work regardless of JIT. | SOURCE |
| 5 | Can the core run at bare EL1? | Plausibly — 52/56 TUs already compile on Darwin, `-fno-exceptions` is default, and FEX already ships a `-nostdlib` backend. But §4.2 and the C++ heap argue for the host compile-server split instead. | MEASURED + INFERRED |
| 6 | Does it build standalone on macOS/arm64? | **Yes, with 3 cmake flags, 6 shim headers and 8 macro defines. 52/56 TUs.** The 4 failures are non-JIT. | MEASURED |

---

## 9. Effort estimate

All three options assume the same surrounding work (nanokernel, shadow MMU, device model,
interrupt/timer) from `08 §9`; only the **JIT-and-x86-semantics** portion is compared here, so these
numbers are *not* whole-project numbers and must not be added to `08`'s 28–48 pm.

Every figure below is **INFERRED**. The justification column names what it is inferred *from*.

### Option A — fork FEXCore, split-JIT configuration

| Work item | pm | Justified by |
|---|---|---|
| Learn the codebase to productivity | 1–2 | 70k LoC of dense C++, custom emitter, custom allocators, JSON-generated IR, 20k-LoC hand-written assembler. Not amortizable across a team. |
| System ISA: 13 instruction groups (§3.1) + CR/MSR/descriptor state + IDT vectoring + CPL transitions | 8–14 | The dispatch sites are enumerated and the descriptor-walk IR exists as a template (`UpdatePrefixFromSegment`), so decode/dispatch is cheap. The semantics are `06`'s difficulty-4/5 material. Consistent with `08 §134`'s independent 6–10 pm for the same surface, plus the FEXCore-specific mode-boolean rework. |
| Runtime mode switching: `Is64BitMode` bool → mode enum across 89 sites + the `Frontend.cpp:1396` assert + two static-register files | 2–4 | 89 sites MEASURED; each is small but the register-file switch (`x64::SRA` 18 entries vs `x32::SRA` 10) is invasive. |
| Rewrite dispatch/cache/link layer (mode+CR3 key, physical invalidation index, guest-visible lookup for the split) | 2–4 | ~3,000 LoC replaced, plus hand-written ARM64 in the dispatcher and three delinkers. |
| Restartable `REP`, MMIO-into-register, explicit precise-fault invariant | 1–3 | FEX's own comment says `REP` "needs to change significantly"; `17 §0.3` gives the cheap shape for the invariant. |
| Split-JIT plumbing: code-buffer `hv_vm_map`, cross-VM I-cache protocol, fallback blob (SoftFloat+cephes) mapped in-guest, compile-exit path | 2–4 | Small in LoC, high in unknowns; contains the UNRESOLVED experiment in §7.3. |
| Upstream rebase tax, first year | 1–2 | The files a retrofit must own (`Allocator.cpp`, `Threads.cpp`, `LookupCache.*`, `Dispatcher.cpp`) are actively developed upstream. |
| **Total** | **17–33 pm** | |

*Delete-work credit not counted above:* the software-TSO ladder, the unaligned backpatcher's
Linux/Windows plumbing, the 32-bit VA allocator, and the persistent code cache can all be dropped,
which is why the range is not wider.

### Option B — write a fresh x86-64 → ARM64 backend

| Work item | pm | Justified by |
|---|---|---|
| Decoder + tables to FEX-comparable coverage | 3–5 | `X86Tables/` is 5,051 LoC of *data*; getting prefixes/VEX/EVEX/modrm right is where the bugs are. `13`'s audit found decoder bugs in this corpus already. |
| IR + optimization passes | 2–4 | `IR/` is 8,995 LoC; a minimal version is smaller but the flag-elimination pass is what makes §10's tricks pay. |
| ARM64 emitter | 2–3 | `CodeEmitter/` is 20,090 LoC, but most is mechanical encoding; generatable. |
| Integer + flag lowering | 3–5 | This is where §10's eight techniques live. Reimplementable from this document. |
| SSE/AVX lowering | 5–9 | `OpcodeDispatcher/{Vector,AVX_128,Crypto}.cpp` alone is a large fraction of the 11,307 LoC. `15 T-2` shows getting this wrong costs 42× on vectorized code. |
| x87 to 80-bit | 2–4 | Or 0–1 pm by linking SoftFloat-3e directly, as FEX does. |
| System ISA | 8–14 | Same as Option A — this work is unavoidable either way. |
| **Total to FEX-comparable coverage** | **25–44 pm** | |
| **Total to a reduced target** (integer + SSE2 + x87-reduced, enough to boot Linux) | **12–20 pm** | The reduced target is a real option and should be costed separately; it fails the moment a guest runs AVX2 code. |

### Option C — retrofit QEMU TCG

Two different things get called "retrofitting TCG" and they have wildly different value:

- **C1 — replace TCG's aarch64 *backend*: not worth doing (1–3 pm, near-zero payoff).** `15 §5.1`
  MEASURED that codegen accounts for 3.51× of the 8.59×, and the limiter is TCG's *IR*, not its
  instruction selector: TCG spills all globals at TB boundaries, has only `CC_OP`-style lazy flags,
  and produces 15.7× code expansion (`15 §4.1`, MEASURED via `info jit`). A better backend under
  the same IR changes little.
- **C2 — replace TCG's *softmmu* with hardware stage-1 shadow paging: the best effort-per-× in this
  document (6–12 pm).** `15 §8a` MEASURED the softmmu share of host time on a real application at
  **54.5%** → 2.20×, and by two other independent methods at 2.04× and 2.45×. You keep TCG's entire
  system ISA — real mode, CR/MSR, descriptors, IDT, `INVLPG`, SMP — which is Options A and B's
  single largest line item. Justification for 6–12 pm: `accel/tcg/cputlb.c` and the
  `target/i386/tcg` walk are the only things being replaced, and `16` has already measured every
  primitive the replacement needs.

  **Blocking issue: QEMU is GPLv2.** FEXCore is MIT (SOURCE, `LICENSE`). If the product must ship
  non-GPL, C2 is unavailable as a *product* — but it remains available as a **measurement**, which
  is what §9.1 recommends.

### 9.1 Recommended sequencing

**INFERRED, and this is the actionable output of this document:**

1. **First, build C2 as a throwaway experiment (not a product).** Wire hardware stage-1 shadow
   paging under an unmodified QEMU TCG x86-64 target and measure. If `15 §8a`'s 2.20× materialises,
   you have MEASURED — not modelled — that the MMU half is real, on this silicon, with `16`'s
   costs actually charged rather than assumed (`15 T-10` names that gap explicitly). If it does
   *not* materialise, no JIT choice can save the project, and you have learned that for 6–12 pm
   instead of 25–44.
2. **Only then choose a JIT.** If C2 lands, Option A (fork FEXCore, split-JIT) at 17–33 pm buys the
   remaining ~2.45× codegen half and is MIT-licensed. Option B is dominated by A: the 25–44 pm
   estimate exceeds A's, and A's largest line item (system ISA, 8–14 pm) is identical in both. The
   only scenario where B wins is if the FEXCore fork tax proves worse than estimated — and §7.1's
   build result argues it will not.
3. **Do not attempt bare-EL1 FEXCore in v1.** §7.3 argument 1 is decisive.

---

## 10. The eight portable techniques

These are worth copying **regardless of the verdict above**. Every one has been re-read at source
by me. Written here at reimplementation detail so this section stands alone.

### 10.1 CF polarity as a symbolic compile-time bit, rectified with `cfinv`

**Problem.** x86 `SUB` sets CF as a *borrow* (CF=1 when a<b); ARM64 `SUBS` sets C as a *carry*
(C=0 when a<b). The naive fix is an XOR after every subtraction.

**FEX's answer.** Track the polarity as a compile-time boolean, never at runtime. State (SOURCE,
`OpcodeDispatcher.h:1331-1341`):

```cpp
bool CFInverted {};             // host carry is currently inverted vs guest carry
const bool CFInvertedABI {true}; // the convention at block boundaries
```

Addition clears it, subtraction sets it, and **no instruction is emitted for either**. Only when
something actually *reads* CF, or a block ends, is the polarity rectified (SOURCE,
`OpcodeDispatcher.h:1753-1771`):

```cpp
void RectifyCarryInvert(bool RequiredInvert) {
  if (CFInverted != RequiredInvert) {
    if (CTX->HostFeatures.SupportsFlagM && !NZCVDirty) {
      _CarryInvert();          // lowers to a single `cfinv` (FEAT_FlagM)
      CachedNZCV = nullptr;
    } else {
      unsigned Bit = IndexNZCV(RFLAG_CF_RAW_LOC);
      SetNZCV(_Xor(OpSize::i32Bit, GetNZCV(), Constant(1u << Bit)));
      CalculateDeferredFlags();
    }
    CFInverted ^= true;
  }
  LOGMAN_THROW_A_FMT(CFInverted == RequiredInvert, "post condition");
}
```

**Three secondary tricks that fall out of it:**
- `GetRFLAG` absorbs the polarity (`Invert ^= CFInverted;`, RAW `OpcodeDispatcher.h:2068`) so no
  caller ever reasons about it.
- `SetNZ_ZeroCV` uses `subs Xd, Xn, #0` for ≥32-bit widths *specifically* to get an inverted C for
  free (RAW, `OpcodeDispatcher.h:1681-1704`).
- `SetCFDirect` computes `0 - x` so the hardware carry lands already inverted (RAW,
  `OpcodeDispatcher.h:1788-1803`).
- On the way out to guest EFLAGS, one `CF ^= 1` (RAW, `Core.cpp:221-222`).

**Reimplementation note.** The `!NZCVDirty` guard matters: `cfinv` operates on live PSTATE, so if
NZCV has been materialised into a GPR you must take the XOR path instead. Without FEAT_FlagM the
fallback costs one `eor` plus a PSTATE round trip; measure before assuming FlagM is present.

### 10.2 AF stored as `src1 ^ src2`, resolved against the saved PF byte

**Problem.** x86's auxiliary-carry flag is bit 4 of `(src1 ^ src2 ^ result)`. Computing it eagerly
costs an XOR and an extract on every arithmetic instruction.

**FEX's answer.** Store *only* `src1 ^ src2` at def time and never mask (SOURCE,
`OpcodeDispatcher/Flags.cpp:227-252`):

```cpp
// We store the XOR of the arguments. At read time, we XOR with the appropriate
// bit of the result (available as the PF flag) and extract the appropriate bit.
// Again 64-bit to avoid masking.
Ref XorRes = Src1 == Src2 ? Constant(0) : _Xor(OpSize::i64Bit, Src1, Src2);
SetRFLAG<RFLAG_AF_RAW_LOC>(XorRes);
```

with constant-operand fast paths above it: if one operand is a constant, the whole thing collapses
to `SrcB` (bit 4 clear) or `_Not(SrcB)` (bit 4 set).

Read time is **one `eor` and one `ubfx`** (SOURCE, `Flags.cpp:182-192`):

```cpp
auto AFWord = GetRFLAG(RFLAG_AF_RAW_LOC);  // src1 ^ src2
auto Result = GetRFLAG(RFLAG_PF_RAW_LOC);  // the result byte, already stored for PF
return _Bfe(OpSize::i32Bit, 1, 4, _Xor(OpSize::i64Bit, AFWord, Result));
```

**The key insight is the coupling:** the `result` term is *free* because PF (§10.3) already had to
save the result byte. AF and PF share one stored value.

**Direct writes preserve the invariant by pre-XORing with PF[4]** so the later XOR cancels — see
`FixupAF` and `SetAFAndFixup` (SOURCE, `Flags.cpp:194-220`). Both carry the derivation in a comment:
`(AF[4] ^ PF[4]) ^ PF[4] = AF[4]`.

**Layout requirement:** `pf_raw` and `af_raw` are adjacent 32-bit fields with a `static_assert`
enforcing it (RAW, `CoreState.h:263`) so a single `ldp`/`stp` moves both.

### 10.3 PF fully deferred to a 3-instruction XOR cascade, gated per block

**Def side is a register move. Nothing is computed** (SOURCE, `Flags.cpp:222-225`):

```cpp
void OpDispatchBuilder::CalculatePF(Ref Res) {
  // Calculation is entirely deferred until load, just store the 8-bit result.
  SetRFLAG<RFLAG_PF_RAW_LOC>(Res);
}
```

**Read side** lowers `_Parity` to three instructions (SOURCE, `JIT/ALUOps.cpp:327-346`):

```cpp
eor(i32Bit, TMP1, Raw,  Raw,  LSR, 4);
eor(i32Bit, TMP1, TMP1, TMP1, LSR, 2);
if (Op->Invert) eon(i32Bit, Dest, TMP1, TMP1, LSR, 1);
else            eor(i32Bit, Dest, TMP1, TMP1, LSR, 1);
if (Op->Mask)   and_(i32Bit, Dest, Dest, 1);
```

`Invert` folds into the third instruction for free (`eon` instead of `eor`) — which matters because
FEX stores PF *inverted*.

**The gate.** Most blocks never read parity, so the parity-specific IR pass is gated on a block
header bit set the first time anything reads PF (SOURCE, `Flags.cpp:174-180`):

```cpp
Ref OpDispatchBuilder::LoadPFRaw(bool Mask, bool Invert) {
  // Most blocks do not read parity, so PF optimization is gated on this flag.
  CurrentHeader->ReadsParity = true;
  return _Parity(GetRFLAG(RFLAG_PF_RAW_LOC), Mask, Invert);
}
```

consumed at `RedundantFlagCalculationElimination.cpp:746-748` (`if
(CurrentIR.GetHeader()->ReadsParity) OptimizeParity(...)`). **Copy the gate, not just the cascade** —
it is what keeps the optimization from costing compile time on the 95% of blocks that do not need
it.

**Bonus, one ARM64 instruction:** packing raw PF back into EFLAGS uses `ornror` (RAW,
`Flags.cpp:110-119`) — build `111P1111`, rotate, invert to `000p0000`, OR in. The tree's comment is
`// This is 1 A64 instruction :-)`.

### 10.4 A reserved `nop` slot next to every faultable atomic

Every TSO load/store of ≥16 bits emits a `nop()` adjacent to it, so that a later in-place patch is
**same-size and needs no relocation** (SOURCE, `JIT/MemoryOps.cpp`, all six sites read):

- **Loads:** `nop()` goes **after** the load — `:799` (LRCPC2 `ldapur*`), `:814` (LRCPC `ldapr*`),
  `:829` (ARMv8.0 `ldar*`). Comment at each: `// Half-barrier once back-patched.`
- **Stores:** `nop()` goes **before** the store — `:1803` (`stlur*`), `:1819` (`stlr*`).
- **8-bit accesses get no slot**, with the comment `// 8bit load is always aligned to natural
  alignment` — they cannot fault.
- **`memcpy`/`memset` lowerings reserve two slots**, one per load/store (RAW, `:2170-2172`,
  `:2194-2196`).

### 10.5 Store the barrier *before* downgrading the instruction, with release ordering

This is the correctness crux and the single most important detail in this section (SOURCE,
`Utils/ArchHelpers/Arm64.cpp:2108-2121`):

```cpp
uint32_t LDR = LDR_INST; LDR |= Size << 30; LDR |= AddrReg << 5; LDR |= DataReg;
if (HandleType != UnalignedHandlerType::NonAtomic) {
  // Ordering matters with cross-thread visibility!
  std::atomic_ref<uint32_t>(PC[1]).store(DMB_LD, std::memory_order_release);
}
std::atomic_ref<uint32_t>(PC[0]).store(LDR, std::memory_order_release);
ClearICache(&PC[0], 8);
return 0;   // With the instruction modified, now execute again.
```

If you wrote the plain `LDR` first, another thread could execute it *before* the barrier landed and
observe an unordered access. Writing the barrier first is safe in both interleavings: a thread that
sees the old `ldar` plus a `dmb` is merely over-ordered.

The whole region is held under a per-block spin mutex stored **inside the JIT block tail** (SOURCE,
`Arm64.cpp:2103-2106`), with the comment *"code must be carefully backpatched from last to first.
Multiple threads can be attempting to handle the SIGBUS or even be executing the code being
backpatched."* And there is an idempotence path: if `PC[0]` already decodes as a downgraded
`LDR`/`LDUR`, another thread won the race and the handler returns success (SOURCE, `:2165+`).

### 10.6 Half-barrier polarity, and the `-4` return

Two barrier encodings (SOURCE, `Arm64.cpp:106-108`):

```cpp
constexpr uint32_t DMB    = 0xD50330BF | 0x0B00; // = 0xD5033BBF  DMB ISH   (inner shareable, all)
constexpr uint32_t DMB_LD = 0xD50330BF | 0x0D00; // = 0xD5033DBF  DMB ISHLD (inner shareable, load)
```
(the tree writes the base in binary as `0b1101'0101'0000'0011'0011'0000'1011'1111`; the encoded
values are MEASURED by evaluating that expression)

| Faulting instruction | Rewritten to | Barrier slot | Handler return |
|---|---|---|---|
| `LDAR*` / `LDAPR*` | `LDR` | `PC[1] = DMB ISHLD` | `0` (re-execute) |
| `STLR*` | `STR` | `PC[-1] = DMB ISH` | **`-4`** (back up one instruction) |
| `LDAPUR*` | `LDUR` (imm9 copied out of the original encoding) | `PC[1] = DMB ISHLD` | `0` |
| `STLUR*` | `STUR` (imm9 copied) | `PC[-1] = DMB ISH` | **`-4`** |

All four verified at SOURCE, `Arm64.cpp:2108-2163`. **`DMB ISHLD` after a load emulates acquire**
(nothing after may move before it); **`DMB ISH` before a store emulates release** (nothing before
may move after it). That is why §10.4's slot is after loads and before stores.

The `-4` is the non-obvious part: for stores the barrier is at `PC[-1]`, which has *already
executed*, so the handler must rewind the PC by one instruction so the freshly-written `DMB`
actually runs. The tree's comment is `// Back up one instruction and have another go`.

**Documented hazard, in FEX's own words** (RAW, `Config.json.in:466-467`): once a site is
downgraded, *aligned* traffic flowing through that same code loses single-copy atomicity. Ship the
knob, default it on, and know what it costs.

**Frequency, which decides whether this is worth it (RAW, `fexcore-a619f6192d8935d61.md` §C.4):**
for the four backpatchable forms this fires **once per static site, ever**. For the non-patchable
forms (`CASPAL`, `CASAL`, LSE RMW, `LDAXR` loops) there is no patch, so *every dynamic execution*
takes the fault — that is the pathological case and it is why `StrictInProcessSplitLocks` and the
kernel-assisted path exist.

### 10.7 Two-instruction software `axflag`

`AXFLAG` (FEAT_FlagM2) converts ARM `fcmp` NZCV into the x86 `COMISS` encoding in one instruction.
Without it, FEX does it in two (SOURCE, `JIT/ALUOps.cpp:308-325`):

```cpp
DEF_OP(AXFlag) {
  if (CTX->HostFeatures.SupportsFlagM2) { axflag(); }
  else {
    // AXFLAG is defined in the Arm spec as
    //   gt: nzCv -> nzCv
    //   lt: Nzcv -> nzcv  <==>  1 + 0
    //   eq: nZCv -> nZCv  <==>  1 + (~0)
    //   un: nzCV -> nZcv  <==>  0 + 0
    // For the latter 3 cases we get the right NZCV by adding V_inv to
    // (eq ? ~0 : 0). The remaining case is forced with ccmn.
    auto V_inv = GetReg(IROp->Args[0]);
    csetm(i64Bit, TMP1, CC_EQ);
    ccmn(i64Bit, V_inv, TMP1, StatusFlags{0x2} /* nzCv */, CC_LE);
  }
}
```

Note the interface asymmetry: the emulated form needs `V_inv` passed in as an operand, the real
instruction does not, so the IR op takes `Invalid()` when FlagM2 is present (RAW,
`OpcodeDispatcher.h:2170`).

**Do not insert anything between `fcmp` and `axflag`** — FEX sets PF *before* the pair specifically
because some implementations macro-fuse them (RAW, `OpcodeDispatcher.h:2146-2151`). `XAFLAG` (the
x86→ARM direction) is never emitted.

### 10.8 Disable LRCPC2 by MIDR on the errata-3877900 cores

`LDAPUR`/`LDAPURB`/`LDAPURH` execute with full Load-Acquire ordering instead of the relaxed RCpc
ordering on several ARM cores, which makes the LRCPC2 tier *slower* than the tier below it. FEX
disables the feature by MIDR (SOURCE, `Source/Common/HostFeatures.cpp:440-467`):

```cpp
bool IgnoreLRCPC2 = (Core_MIDR_Implementer == Implementer_ARM) &&
    (PartNum == PartNum_V2 || PartNum_V3 || PartNum_X3 || PartNum_X4 ||
     PartNum_X925 || PartNum_V3AE || PartNum_C1Ultra || PartNum_C1Premium);
if (IgnoreLRCPC2) { HostFeatures->SupportsTSOImm9 = false; break; }
```

The in-tree comment cites Cortex erratum **3877900** and LLVM PR
[124274](https://github.com/llvm/llvm-project/pull/124274) (CITED — the URL is in the source
comment). Note the loop iterates over **all** core MIDRs, so a big.LITTLE part with one affected
core disables the feature process-wide.

**Relevance to us (INFERRED):** Apple cores are not in the list, and `14`/`16` establish we get
hardware TSO anyway, so all three software tiers become dead code in our design. Copy the
*pattern* — per-MIDR feature suppression driven by errata, applied globally across heterogeneous
cores — not the specific list.

### 10.9 A ninth, which the brief did not name but which is worth as much

**Deferred NZCV residency.** FEX keeps the four arithmetic flags in the *PSTATE register* for as
long as possible and only materialises them into a GPR when an instruction is about to clobber
them (SOURCE, `OpcodeDispatcher.h:1294-1303, 1328-1329`; flush at `Flags.cpp:254-261`). Combined
with §10.1–10.3 this means a typical arithmetic instruction emits **zero** flag-handling
instructions. The measured consequence for anyone not doing this is in `15 §5.1`: TCG's codegen,
which does *not* do this, costs 3.51× against the same host.

---

## 11. UNRESOLVED

| # | Question | Experiment that would resolve it |
|---|---|---|
| U-1 | **Can the host patch code that a guest vCPU is executing, through an `hv_vm_map`'d RX page, safely?** The split-JIT design (§7.3) depends on it. | Map an RW host buffer as RX into the guest. Have a vCPU spin executing a `b` self-loop inside it. From the host, store a different instruction and issue `__builtin___clear_cache`. Measure whether the guest ever observes the new instruction without a guest-side `IC IVAU; DSB ISH; ISB`, and how long the window is. Half a day. **This is the highest-value unresolved item in this document.** |
| U-2 | **What does `RestoreRIPFromHostPC` actually cost per fault?** §5.3 compares it to `17`'s measured alternatives by reasoning only. | Microbenchmark: call it at known offsets into compiled blocks of 8/32/128 guest instructions, on this host, warm and cold. Compare to `17 §0.2` items 11–14. One day. |
| U-3 | **Does the C2 experiment (hardware stage-1 under unmodified QEMU TCG) actually deliver `15 §8a`'s 2.20×?** All of §9.1 hangs on it. | Build it (6–12 pm) — or, much cheaper, a partial version that shadows only user-mode guest pages and falls back to softmmu elsewhere, to get a first number in ~2 pm. |
| U-4 | **How much of FEX's user-mode frontend survives once `Is64BitMode` becomes a runtime mode?** I counted 89 sites but did not classify them. | Mechanical: classify all 89 into (i) decoder-table selection, (ii) operand-size selection, (iii) register-file selection, (iv) genuine semantic branches. One week. |
| U-5 | **What is FEXCore's compile throughput on this host?** The split-JIT exit budget in §7.3 assumes compile time is small relative to 808 ns exits, but I never measured it. | Build FEXCore standalone (§7.1 says 52/56 TUs already compile — fix `sincos`, stub `CodeCache`), feed it a static x86-64 binary's `.text`, and time `CompileBlock` per block. Two days. |
| U-6 | **Does hardware TSO make FEX's unaligned-atomic backpatcher unnecessary, or only mostly?** §10.4–10.6 are documented on the assumption we still want them. | Litmus test at guest EL1 with `ACTLR_EL1.EnTSO=1`: unaligned `LOCK CMPXCHG` straddling a cache line, two vCPUs. `14`'s TSO litmus harness is the starting point. |
| U-7 | **Segment-limit enforcement cost.** §3.2 says every memory op in 16/32-bit protected mode needs a bounds check with no IR representation. I did not estimate what that costs. | `17`'s methodology: measure a translated x86 store with and without a two-instruction `cmp`+`b.hi` against a pinned limit register. |

---

## 12. Threats to validity

| # | Issue |
|---|---|
| T-1 | **The five raw dossiers are unaudited and I re-verified only ~15 claims** (§2). Everything marked RAW is second-hand. In particular the whole `LookupCache`/`Dispatcher`/block-linking analysis in §3.4 — which drives a "rewrite it" recommendation — rests on RAW claims I did not open the assembly for. |
| T-2 | **The build test proves compilation, not function.** 52/56 TUs compiling on macOS/arm64 says nothing about whether the resulting library *runs*. Several of my shims are lies (`prctl` returns −1, `SYS_futex` is defined as 0, futex constants are invented). A binary built with those shims would deadlock. The result is diagnostic of *header/API coupling*, which is what §7 needed, and of nothing else. |
| T-3 | **Every effort number in §9 is INFERRED and none is calibrated against a completed comparable project.** They are anchored to LoC counts, enumerated worklists, and `08`'s independent estimates for the same surface — which is better than feel, but a factor-of-two error in either direction would not surprise me. Treat the *ranking* (C2 < A < B) as more trustworthy than the magnitudes. |
| T-4 | **The split-JIT recommendation rests on one unresolved experiment (U-1).** If cross-VM code patching turns out to require a stage-2 remap per patch, the 808 ns exit floor plus `02`'s `hv_vm_protect` O(mappings) cliff (313 ns → 27 µs) would change the recommendation. |
| T-5 | **I read FEX at one commit, and it is a fast-moving project.** HEAD is a merge commit from an active PR stream. Anything about defaults, config knobs, or file layout may be stale within months. Nothing here should be quoted without re-checking against the then-current tree. |
| T-6 | **I did not evaluate box64, Blink, or any other MIT/BSD translator.** `05` names FEXCore as the reuse candidate and the brief asked about FEXCore, so this document compares FEXCore against "write it yourself" and "TCG" only. box64 in particular has a different (wrapper-heavy) architecture that may or may not be worse for this purpose. |
| T-7 | **The "guest owns every address" premise in §4.2 assumes we present an unmodified canonical 48-bit x86-64 VA layout.** If the design accepts a PV VA hole (`07`'s negotiated-PV framing already contemplates guest patches), §4.2's problem shrinks a lot and bare-EL1 FEXCore becomes more attractive. That is a *product* decision — "boot arbitrary firmware" vs "boot our Linux" — not an engineering one, and `08 §143` argues the former is the only defensible product case. |
| T-8 | **I did not verify that FEX's x87 is *correct* to 80 bits, only that it is *implemented* to 80 bits.** SoftFloat-3e `extF80` with FCW-driven state is the right machinery; whether FEX drives it correctly in all rounding/precision-control/exception cases is untested here. `04` measured Rosetta at 6% of native on x87, so this path is slow everywhere and correctness is what matters. |

---

## 13. What this changes in the rest of the corpus

Non-binding notes for whoever next edits those documents — **this document modifies none of them.**

- **`05 §"existing-translators"`** lists FEXCore as reusable for "frontend + IR + ARM64 backend".
  That is confirmed, but should be qualified: the dispatch/cache layer is *not* reusable (§3.4), and
  the 90k-LoC figure should be split into the take/rewrite/discard grading in §0.1.
- **`08 §132`** costs "Tier 1 bring-up: FEXCore + softmmu + minimal machine" at 10–16 pm. §9's
  Option A suggests the *FEXCore-specific* portion of a Tier-2 retrofit is 17–33 pm on top, which
  is consistent with `08`'s 28–48 pm total but makes the JIT share larger than `08 §133-134`'s
  implied 14–24 pm.
- **`12`'s ranking** puts MMU first and codegen third. §9.1 agrees and strengthens it: the MMU win
  is available without choosing a JIT at all, and should be measured first.
- **`17 §0.1`'s two-tier fault model** is *compatible* with FEXCore's reconstruction design, not in
  tension with it (§5.3). The raw dossier's contrary reading should not be propagated.
- **A new claim worth adding to `09` (architectural nuances):** a full-system x86-64 guest occupies
  both canonical VA halves, so there is no free virtual address range for an in-guest runtime. That
  constraint is what selects the split-JIT architecture and it does not appear anywhere in the
  corpus today.
