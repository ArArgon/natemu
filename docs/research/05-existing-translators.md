# 05 — Existing x86-64 → ARM64 Translators: Architecture, Techniques, Reusable Components

**Status:** research note · **Date:** 2026-08-08 · **Audience:** natemu architecture
**Question this answers:** *Which existing open-source projects can we take code from, rather than write ourselves?*

Project philosophy assumed throughout: **reuse as much existing code as possible**. Every project below is
therefore evaluated not as "is it good software" but as *"can I pull a subsystem out of it, and what does
that cost me?"* — on three axes:

- **Value** — how much work does it save?
- **Extraction difficulty** — how coupled is it to its host project's assumptions?
- **License compatibility** — MIT/Apache/BSD (take freely) vs LGPL (link, publish modifications) vs GPL/AGPL (viral).

A recurring theme: **almost every fast x86→ARM64 translator that exists is a *userspace* translator that maps
guest virtual addresses 1:1 onto host virtual addresses.** The moment you need ring 0, CR3, page tables and
guest-physical memory, you either add a software MMU (QEMU's model, ~10 extra host instructions per guest
memory access) or you build shadow paging on top of the host MMU. That single decision dominates the design,
and it is the main thing that determines which of these projects you can reuse.

---

## 0. TL;DR — recommended reuse stack

| Layer | Take from | License | Why |
|---|---|---|---|
| x86-64 decoder + tables | **FEXCore** `X86Tables/` (5.0k LoC) | MIT | Complete through AVX2, MIT, already separated from semantics |
| x86 → IR semantics | **FEXCore** `OpcodeDispatcher/` (19.2k LoC) | MIT | Best-maintained MIT x86-64 semantics anywhere; NZCV-native flag model |
| SSA IR + passes | **FEXCore** `Interface/IR/` (5.7k LoC) | MIT | Small, arena-allocated, JSON-generated; 438 ops |
| ARM64 code generation | **FEXCore** `JIT/` (13.2k) + `CodeEmitter/` (20.1k) | MIT | Hand-written ARM64 assembler + IR→ARM64 backend |
| Unaligned-atomic SIGBUS handler | **FEXCore** `Utils/ArchHelpers/Arm64.cpp` (2.2k LoC) | MIT | Nearly drop-in; solves the single hardest x86-on-ARM64 problem |
| Software MMU / TLB design | **QEMU** `accel/tcg/cputlb.c` — *port the design, not the code* | GPL-2.0 | Reference implementation; do not copy source into an MIT project |
| Guest physical memory | **rust-vmm `vm-memory`** | Apache-2.0/BSD | Accelerator-agnostic |
| Guest kernel loading | **rust-vmm `linux-loader`** | Apache-2.0 | bzImage + ELF + zero-page/e820 |
| Serial / RTC | **rust-vmm `vm-superio`** | Apache-2.0 | Tiny, no KVM dependency |
| virtio transport + devices | **rust-vmm `virtio-queue`** (+ Cloud Hypervisor device impls) | Apache-2.0/BSD-3 | Split-queue plumbing is tedious and solved |
| Correctness oracle | **Bochs** (differential testing only, not linked) | LGPL-2.1 | Most faithful open x86-64 semantics incl. paging/SMM |

**One-line answer:** *Fork FEXCore for the CPU (MIT, ~90k LoC of frontend+IR+ARM64 backend), replace its 1:1
memory model with a QEMU-style softmmu of your own writing, and build the machine around rust-vmm crates.*
Details, alternatives and risks below.

---

## 1. Master comparison table

Star counts / activity as of **2026-08-08**. "Core LoC" = the part you would actually reuse, measured by
`wc -l` on a fresh checkout (see Appendix A for method).

| Project | Guest→Host | IR | Core LoC | License | Stars | Last push | System-level (ring0/MMU)? | Extractable? |
|---|---|---|---|---|---|---|---|---|
| **FEX-Emu** | x86/x86-64 → ARM64 | Custom SSA (438 ops) | ~90k (FEXCore+CodeEmitter) | **MIT** | 7.8k | 2026-08-07 | No — 1:1 VA, no softmmu | **Yes** — builds as a shared lib; already reused on Linux + Windows/Wine |
| **box64** | x86-64 → ARM64/RV64/LA64 | **None** (multi-pass direct emit) | ~50k dynarec (of 22 MB src) | **MIT** | 5.6k | 2026-08-08 | No — no rings, no paging, vestigial segmentation | Partial — everything reads a global `x64emu_t` |
| **box86** | x86 → ARM32/x86 | None | — | MIT | 3.8k | 2026-01 | No | Partial; largely subsumed by box64's 32-bit mode |
| **QEMU TCG** | any → any | TCG (register machine, ~100 ops) | 40k (tcg+accel/tcg) + 20.6k (i386 frontend) | **GPL-2.0** | 13.5k | 2026-08-05 | **Yes** — the reference softmmu | Hard, and GPL contaminates |
| **Unicorn 2** | QEMU-derived, library | TCG (QEMU 5.0 vintage) | ~21 MB fork | **GPL-2.0** | 9.2k | 2026-07-30 | Paging yes; rings/devices no | It *is* the extraction — but frozen at QEMU 5.0 |
| **Blink** | x86-64 → any (interp + copy-paste JIT) | None | ~20 MB repo, ~220 KB binary | **ISC** | 7.6k | 2025-12-10 | No (userspace only) | Decoder maybe; rest is size-optimized spaghetti |
| **Bochs** | x86-64 full system, interpreter | None | ~47 MB | **LGPL-2.1** | 1.3k | 2026-08-07 | **Best in class** (5-level paging, SMM, VMX, AVX-512) | Not a library; use as oracle |
| **86Box / PCem** | legacy PC device models | n/a | ~259 MB | GPL-2.0(+) | 4.5k | 2026-08-08 | n/a | Devices are decoupled but retro-era, wrong target |
| **libcpu** | multi-arch → LLVM IR | LLVM IR | 2.8 MB | BSD-2 | 363 | **archived 2021** | No | Dead — cautionary tale only |
| **Valgrind VEX** | x86-64 → VEX IR → ARM64 | VEX IR (typed SSA, IRSB) | ~7.8 MB (angr/vex fork) | **GPL-2.0+** | 114 (fork) | 2026-07-22 | Lifter only; no ring0 | **Proven** (pyvex/angr) — but GPL |
| **Rellume** | x86-64/AArch64/RV64 → LLVM IR | LLVM IR (register "facets") | ~1 MB | **LGPL-2.1+** | 512 | 2024-06 | No | Yes, it's a library — but stale |
| **Instrew** | Rellume + LLVM JIT DBT | LLVM IR | ~290 KB | LGPL-2.1+ | 291 | 2024-06 | No | Research quality |
| **Remill** | x86-64/AArch64 → LLVM IR | LLVM IR (semantics-as-bitcode) | ~10.6 MB | **Apache-2.0** | 1.8k | 2026-07-01 | No | Yes (permissive!) but slow lifted code |
| **McSema** | binary → LLVM | LLVM IR | — | AGPL-3.0 | 2.8k | **archived 2022** | No | Dead + AGPL |
| **RevNG** | via QEMU TCG → LLVM IR | LLVM IR | ~20 MB | GPL-2.0 | 1.7k | 2026-08-07 | No | GPL, static-analysis oriented |
| **retdec** | decompiler | LLVM IR | ~33 MB | MIT | 8.6k | 2026-05-26 | No | Wrong problem (decompilation) |

Complementary infrastructure (not translators):

| Project | Role | License | Stars | Status |
|---|---|---|---|---|
| **rust-vmm** crates | guest memory, virtio, loaders, serial | Apache-2.0 / BSD-3 | per-crate | active |
| **Cloud Hypervisor** | full VMM, KVM+MSHV abstraction | Apache-2.0 (+BSD-3) | ~6.1k | active |
| **Firecracker** | minimal microVM device set | Apache-2.0 | — | active |
| **libkrun** | *library* VMM, **KVM on Linux + HVF on macOS/ARM64** | Apache-2.0 | ~2.6k | active |
| **UTM** | QEMU + Hypervisor.framework on Apple Silicon | Apache-2.0 (QEMU is GPL) | — | active |

---

## 2. FEX-Emu — the primary reuse candidate

**Repo:** <https://github.com/FEX-Emu/FEX> · **License: MIT** · 7,834 ★ · 316 forks · last push **2026-08-07**
(HEAD `fd141ed6`, 2026-08-06) · 226 open issues · releases on a monthly `FEX-YYMM` cadence (FEX-2604 shipped
2026-04-09, FEX-2508 in Aug 2025).

### 2.1 Structure and the FEXCore boundary

The repository is cleanly split, and this split is the single most important fact about FEX for our purposes:

```
FEXCore/        the emulation library      — MIT, builds as libFEXCore.so
CodeEmitter/    hand-written ARM64 assembler (also used standalone)
Source/Tools/   FEXLoader + LinuxEmulation (Linux syscall/ELF/signal layer)
Source/Windows/ WOW64 + ARM64EC frontends for Wine on Windows-on-ARM
ThunkLibs/      host-native library thunks (Vulkan, GL, SDL2, X11, ...)
```

Measured line counts (fresh checkout, `.cpp/.h/.inl`):

| Component | LoC | Role |
|---|---:|---|
| `FEXCore/Source/Interface/Core/X86Tables/` | 5,051 | x86-64 decode tables (base, 0F, 0F38, 0F3A, VEX, x87, groups) |
| `FEXCore/Source/Interface/Core/OpcodeDispatcher/` + `OpcodeDispatcher.{cpp,h}` | 19,216 | x86 semantics → IR (ALU, Vector, AVX_128, X87, X87F64, Crypto, Flags) |
| `FEXCore/Source/Interface/IR/` | 5,731 | IR defs (`IR.json`, 438 ops), emitter, PassManager, 4 passes |
| `FEXCore/Source/Interface/Core/JIT/` | 13,213 | IR → ARM64 (ALUOps, VectorOps 194 KB, MemoryOps 98 KB, AtomicOps, BranchOps…) |
| `FEXCore/Source/Interface/Core/Dispatcher/` | 2,844 | entry/exit trampolines, block linking, signal return |
| `FEXCore/Source/Utils/ArchHelpers/Arm64.cpp` | 2,212 | **unaligned-atomic SIGBUS handler + backpatcher** |
| `CodeEmitter/` | 20,090 | standalone ARM64 (+SVE) instruction encoder |
| `FEXCore/include/` | 6,117 | public headers (60 files) |
| **Reusable subtotal** | **~90,000** | |
| `Source/Tools/LinuxEmulation/` | 29,853 | *Linux-specific — the part you throw away* |
| `Source/Windows/` | 8,574 | *proof the core is OS-agnostic* |
| `ThunkLibs/` | 32,041 | *userspace-only concept — throw away* |

**FEXCore builds as a standalone shared library.** Its only dependencies are `fmt`, `xxHash`,
`unordered_dense`, an in-repo header-only `FEXHeaderUtils`, `CodeEmitter`, and a 128-bit `cephes` math
fallback — all permissive. `FEXCore/LICENSE` is MIT, separate from (but identical to) the root LICENSE.

### 2.2 Public API — how decoupled is it really?

`FEXCore/include/FEXCore/Core/Context.h` exposes a small virtual interface:

```
CreateNewContext(HostFeatures) -> unique_ptr<Context>
InitCore(); CreateThread(RIP, RSP, CPUState*); ExecuteThread(...); DestroyThread(...)
CompileRIP(thread, rip); ClearCodeCache(); InvalidateCodeBuffersCodeRange(start, len)
RestoreRIPFromHostPC(); ReconstructCompactedEFLAGS(); ReconstructXMMRegisters()
RunCPUIDFunction(); RunXCRFunction(); SetHardwareTSOSupport(bool); EnableExitOnHLT()
```

and requires the *embedder* to supply: `SyscallHandler`, `SignalDelegator`, `ThunkHandler`, `HostFeatures`,
`CodeMapWriter`. That is exactly the shape you want. The strongest evidence it is genuinely reusable is
`Source/Windows/`: the same FEXCore drives **Wine's WOW64 and ARM64EC layers on Windows-on-ARM**, a completely
different OS, ABI and signal model from Linux. FEXCore has already survived one hostile re-embedding.

Two API details worth noting for a system-level project: `EnableExitOnHLT()` already exists (a hook for
"guest executed HLT, give control back"), and `RestoreRIPFromHostPC()` / `ReconstructCompactedEFLAGS()` give
you precise guest-state reconstruction at an arbitrary host PC — the machinery you need for precise
exceptions, which is *mandatory* for MMU faults and not something you want to write yourself.

### 2.3 IR design

SSA, explicitly sized, arena-allocated. From `FEXCore/docs/IR.md` and `IR.json`:

- **438 op definitions** in `IR.json` (3,264 lines), from which the dispatcher, emitter and printer are
  code-generated. Adding an op = editing JSON. This is a genuinely nice extension story.
- Explicit sizes (1/2/4/8/16 bytes, arbitrary vector element counts), **scalar and vector ops are separate**
  (`MUL` vs `VMUL`) — the IR "loosely models ARM64", which means the backend is thin.
- `IRHeader` is node 0; `CodeBlock`s are jump targets holding inclusive begin/end pointers and forming a
  singly-linked list; `BeginBlock`/`EndBlock` delimit.
- In memory: an `IntrusiveAllocator` linear arena; `OrderedNode`s in a doubly-linked list in dominance order;
  node references are `uint32_t` offsets (`allocation offset / sizeof(OrderedNode)`) so **the whole IR buffer
  is relocatable without pointer fixups** — nice for caching translated IR to disk.
- Passes are few and deliberate: `RedundantFlagCalculationElimination`, `RegisterAllocationPass`,
  `x87StackOptimizationPass`, `IRValidation`, `IRDumperPass`. Constant propagation and DCE ride along the
  emitter (`NumUses` tracking).

**Memory ops are the tell:**

```
SSA = LoadMem     RegisterClass:$Class, OpSize:#Size, GPR:$Addr, GPR:$Offset, OpSize:$Align, MemOffsetType, u8:$OffsetScale
SSA = LoadMemTSO  RegisterClass:$Class, OpSize:#Size, GPR:$Addr, GPR:$Offset, ...   # "x86 TSO compatible load"
      StoreMem / StoreMemTSO  (same shape)
      LoadMemPair / StoreMemPair            # LDP/STP fusion
```

`$Addr` is a **raw guest address used directly as a host address**. There is no TLB register, no memory-base
register, no guest→host translation step in the IR. TSO is a *first-class IR distinction* (`LoadMemTSO` vs
`LoadMem`), which is exactly the right factoring — but the address model is 1:1.

### 2.4 Register allocation and flags

- **SRA (static register allocation):** guest GPRs are pinned to host registers across the whole JIT.
  `Arm64Emitter.h` also pins dedicated registers for `REG_PF` and `REG_AF` (x86 parity and aux-carry, the two
  flags with no ARM64 equivalent) and `REG_CALLRET_SP` for the return-address stack optimization. The pinning
  map differs between the Linux and ARM64EC builds (ARM64EC steals r26/r27 for its own ABI) — proof the
  mapping is parameterized, not hard-coded.
- **Dynamic RA** for IR temporaries via `RegisterAllocationPass`. FEX-2406 replaced a "quadratic or even
  infinite time" allocator with a **linear two-pass** design.
- **Flags: NZCV-native.** `OpcodeDispatcher.h` carries a `CachedNZCV` and a `SaveNZCV()` discipline: x86
  condition codes ride in the *host* NZCV register wherever possible, spilling to a GPR only when an
  instruction clobbers NZCV. PF/AF live in their pinned GPRs. `RedundantFlagCalculationElimination` deletes
  flag computations whose results are never read. This is strictly better than QEMU's `CC_OP/CC_DEP1/CC_DEP2`
  lazy-thunk model for a fixed ARM64 target, and better than box64's older `xFlags` bitfield emulation
  (box64 has since added its own `NATIVE_FLAGS` mode that converges on the same idea).
  FEX is moving toward requiring **FEAT_FlagM / ARMv8.4** as a minimum (issue #4120) to get `RMIF`/`CFINV`
  and friends.

### 2.5 Atomics, TSO and the unaligned problem — the most valuable single file

This is where FEX has invested more than anyone else in the open-source world, and it is the part I would
copy first.

The problem: **x86 atomics don't care about alignment; ARM64 atomics fault unless naturally aligned.**
FEAT_LSE2 (ARMv8.4) removes the fault for unaligned accesses *within a 16-byte granule*, but x86 permits
"split locks" spanning a cacheline, so LSE2 is a partial fix. FEX's own release notes put the cost of
accurately emulating x86's memory model at **"near a 10× performance hit"** in the worst case.

FEX's layered answer, all configurable (`FEXCore/Source/Interface/Config/Config.json.in`):

| Knob | Default | Meaning |
|---|---|---|
| `TSOEnabled` | true | emit `LoadMemTSO`/`StoreMemTSO` (LDAR/STLR) at all. "Highly likely to break any multithreaded application if disabled." |
| `VectorTSOEnabled` | **false since FEX-2406** | make vector load/stores atomic too — "cost too high for most hardware" |
| `MemcpySetTSOEnabled` | **false since FEX-2406** | make `rep movs`/`stos` atomic |
| `HalfBarrierTSOEnabled` | true (recommended) | **backpatch unaligned loads/stores into half-barrier atomics** |
| `StrictInProcessSplitLocks` | — | global lock for an unaligned atomic crossing a 16-byte/cacheline granule |
| `KernelUnalignedAtomicBackpatching` | — | use backpatching to avoid kernel context switches when the kernel handles the fault |
| `SMCChecks` | `mtrack` | `none` / `mtrack` (page-tracking invalidation) / `full` (validate before every run) |
| `ForceTSO...` per-module ranges | — | disable TSO for a whole module, or a byte range, with per-instruction opt-back-in |

`FEXCore/Source/Utils/ArchHelpers/Arm64.cpp` (2,212 LoC) implements `HandleUnalignedAccess(Thread,
HandleType, ProgramCounter, GPRs, IsJIT)`: a **SIGBUS handler that decodes the faulting ARM64 instruction and
emulates it**. It masks against `CASPAL_MASK`, `CASAL_MASK`, `LDAXR_MASK`/`LDAR_INST`/`LDAPR_INST`,
`STLR_INST`, `RCPC2_MASK`/`LDAPUR_INST`/`STLUR_INST`, then runs hand-written `RunCASPAL`, `HandleCASAL`,
`HandleAtomicMemOp`, `HandleAtomicLoad/Store`, `HandleAtomicLoadstoreExclusive`, `DoCAS16/32/64/128`,
`HandleCAS_NoAtomics` (for hosts without LSE), returning the number of bytes to advance the PC. It also
optionally **backpatches** the faulting site so the next execution takes a slower-but-non-faulting path
instead of trapping again.

This file is nearly self-contained: it needs `InternalThreadState` for the config flags and the strict-split-lock
mutex, and an `ARCHITECTURE_arm64` guard. Porting it costs maybe a day and saves months. **Take it.**

Also relevant: `SetHardwareTSOSupport(bool)` exists in the public API — FEX knows about hosts (Apple Silicon,
Nvidia Denver-style) where TSO can be enabled in hardware and the software barriers can be dropped entirely.

### 2.6 SIMD and x87

- AVX/AVX2 supported; `AVX_128.cpp` emulates 256-bit YMM as two 128-bit NEON halves so it works on baseline
  ARMv8 (SVE is used opportunistically — `PRED_TMP_16B`/`PRED_TMP_32B`, `SVE_OPT_PRED` for x87). `VectorOps.cpp`
  is 194 KB, the largest backend file — SIMD is where the bodies are buried.
- x87: `X87.cpp` (80-bit path) and `X87F64.cpp` (reduced-precision 64-bit path) plus an
  `x87StackOptimizationPass`. FEX-2604 inlined SIN/COS/TAN for the reduced-precision path for a **3.7×**
  speedup by avoiding JIT exits.
- Scalar SSE gained 2–12 % from using ARM's AFP (Alternate Floating-Point) and RPRES extensions on Apple SoCs
  and newer Cortex cores (FEX-2406).

### 2.7 SMC, code cache, block linking

- SMC: `SMCChecks = mtrack` — page-tracking-based invalidation (write-protect pages containing translated
  code, invalidate on fault), with `full` (re-validate before every execution) as a paranoid fallback and a
  Mono-specific hook mode for JIT-heavy guests.
- Block linking via the `Dispatcher`'s `ExitFunctionLinker` trampoline plus a `LookupCache` (L1/L2 hash
  tables). FEX-2604 turned on a **dynamic L1 lookup cache** and turned *off* L2 by default to cut memory
  (one leak fix took a test from 409 MB to 6 MB).
- A `CodeCache` (`CodeCache.cpp`, 37.9 KB) supports persisting translations; `FEXOfflineCompiler` exists as a
  tool. Relocations are handled in `Arm64Relocations.cpp`.

### 2.8 Coupling to Linux userspace — the honest assessment

Coupled parts (all in `Source/`, all discardable):
- `LinuxEmulation/` (29.9k LoC): syscall tables, ELF loading, `SignalDelegator`, thread/clone handling, rootfs.
- `ThunkLibs/` (32k LoC): the "call the host's native libGL instead of translating it" trick. **Meaningless for
  system-level translation** — a guest kernel has no shared libraries to thunk.

Coupled parts *inside* FEXCore (the real work):
- **The 1:1 address space.** `docs/ProgrammingConcerns.md`: FEX "runs both 32-bit and 64-bit guest applications
  in the same address space as FEX itself," and for 32-bit guests "reserve[s] up all memory above 4 GB." FEXCore
  avoids the system allocator entirely (`ENABLE_GLIBC_ALLOCATOR_HOOK_FAULT` fault-injects violations) precisely
  because it is fighting the guest for address space. Every `LoadMem`/`StoreMem` dereferences a guest address
  directly.
- CPUID/XCR emulation is present and configurable, which is good, but there is **no ring/CPL model, no
  descriptor tables, no CR0/CR3/CR4, no page-table walker, no IN/OUT, no LGDT/LIDT/IRET-with-privilege-change.**
  FS/GS bases exist (they're needed for TLS) but as flat offsets, not real segmentation.

### 2.9 Verdict for a system-level project

**Reuse the frontend + IR + backend; replace the memory model.** Concretely, the work is:

1. Add a guest-physical/guest-virtual distinction. Either (a) emit a TLB lookup in `MemoryOps.cpp` for
   `LoadMem`/`StoreMem` — i.e. graft QEMU's softmmu shape onto FEX's IR, costing ~9–10 host instructions per
   access on ARM64 (see §3.4); or (b) shadow paging: keep the 1:1 fast path and mirror guest page tables into
   host `mmap`/`mprotect` so the host MMU does the work, taking faults on guest page-table writes. (b) preserves
   FEX's speed but is a *lot* of subtle work and interacts badly with a 64-bit guest address space.
2. Add privileged-instruction dispatch: new IR ops for `MOV CRn`, `IN`/`OUT`, `LGDT`/`LIDT`/`LTR`, `IRET`,
   `SYSCALL`/`SYSRET` at ring transitions, `INVLPG`, `WRMSR`/`RDMSR`, `HLT` (partly present).
3. Add an exception/interrupt model (IDT delivery, error codes, `#PF` CR2) — FEX's precise-state reconstruction
   (`RestoreRIPFromHostPC`) is the foundation and is already there.
4. Add real segmentation for the parts a kernel uses (GDT/LDT loading, CPL checks on the few paths that matter).

That is a serious project, but it is *far* smaller than writing 90k lines of x86-64 semantics and an ARM64
backend. And the license (MIT) means you can do it in a closed or differently-licensed tree.

**Related evidence the architecture travels:** `felix86` is an independent FEX-inspired x86-64→**RISC-V**
translator that reached playable-games status in 2026, and box64 carries four backends (arm64/rv64/la64/ppc64le)
off one pass driver. IR-based x86→host translation is a well-trodden shape.

---

## 3. QEMU TCG — the only mature *system-level* answer

**Repo:** <https://github.com/qemu/qemu> · **GPL-2.0** (mixed; TCG core files are GPL-2.0-or-later, some
BSD/MIT) · 13.5k ★ · last push 2026-08-05.

Measured LoC (sparse checkout of master):

| Path | LoC | Role |
|---|---:|---|
| `tcg/` (excl. backends) | 21,797 | TCG IR, `tcg.c` (regalloc/liveness), `optimize.c`, `tcg-op*.c`, gvec |
| `tcg/aarch64/` | 3,793 | **the ARM64 backend — surprisingly small** |
| `accel/tcg/` | 14,727 | softmmu (`cputlb.c` 93 KB), `cpu-exec.c`, `tb-maint.c`, MTTCG ops, plugins |
| `include/tcg/` | 3,365 | IR headers |
| `target/i386/tcg/` | 20,648 | x86 frontend: `decode-new.c.inc` (123 KB), `emit.c.inc` (153 KB), `translate.c` (116 KB), `seg_helper.c` (84 KB), `fpu_helper.c` (114 KB) |
| `target/i386/` (total) | 27,615 | + CPU model, CPUID, KVM/HVF glue |
| `hw/i386/` | 23,796 | PC machine, ACPI, IRQ routing |
| `hw/virtio/` | 34,423 | virtio devices |

So: a complete, correct, system-capable x86-64→ARM64 translation engine is **~60k lines** (`tcg` + `accel/tcg`
+ `target/i386/tcg`), comparable to FEXCore. The difference is that QEMU's 60k includes a software MMU and
full privileged-mode semantics, and FEXCore's 90k includes neither but generates much better code.

### 3.1 TCG IR

Explicitly **not SSA** — a register machine (per `docs/devel/tcg-ops.rst`). Five temp classes:
`TEMP_FIXED` (the `cpu_env` pointer, permanently in a host register), `TEMP_GLOBAL` (CPU state at an offset
from env, live across blocks), `TEMP_CONST`, `TEMP_TB` (translation-block scoped), `TEMP_EBB` (extended-basic-block
scoped, dies at any exit). ~100+ ops: arithmetic/logical/bit/shift, `setcond`/`movcond`/`negsetcond`, vector
ops, multiword carry chains, `mb` (barrier), and the guest-specific `qemu_ld_i32/i64/i128`,
`qemu_st_*`, `exit_tb`, `goto_tb`, `lookup_and_goto_ptr`.

Backends are `tcg-target.c.inc` files included into `tcg.c`, driven by GCC-style register constraints
(`tcg-target-con-set.h`, `tcg-target-con-str.h`). Register allocation is liveness-based and local, done in
`tcg.c`. Optimization is one pass (`optimize.c`, 89 KB — constant folding, copy propagation).

This is a *deliberately weak* IR: it's designed so translation is fast, not so generated code is good. The
Instrew paper (§5) measures exactly this trade.

### 3.2 x86 TSO on a weak host — how QEMU actually does it

`target/i386/tcg/tcg-cpu.c:166`:

```c
.guest_default_memory_order = TCG_MO_ALL & ~TCG_MO_ST_LD,   /* x86 TSO */
```

`tcg/aarch64/tcg-target-mo.h`:

```c
#define TCG_TARGET_DEFAULT_MO  0     /* AArch64 guarantees nothing */
```

and `tcg/tcg-op-ldst.c:117`:

```c
static void tcg_gen_req_mo(TCGBar type) {
    type &= tcg_ctx->guest_mo;
    type &= ~TCG_TARGET_DEFAULT_MO;
    if (type) tcg_gen_mb(type | TCG_BAR_SC);
}
```

Every guest load emits `tcg_gen_req_mo(TCG_MO_LD_LD | TCG_MO_ST_LD)` and every store
`tcg_gen_req_mo(TCG_MO_LD_ST | TCG_MO_ST_ST)` *before* the access. On AArch64 with an x86 guest, `guest_mo`
keeps LD_LD/LD_ST/ST_ST and the host contributes nothing, so **QEMU emits an explicit `dmb ish` barrier before
essentially every guest memory operation** when `CF_PARALLEL` is set. (The optimizer merges adjacent barriers.)
It does *not* use LDAR/STLR the way FEX and box64 do — it uses full fences. This is a large part of why
MTTCG x86-on-ARM64 is slow.

Crucially, `tcg_canonicalize_memop()` **drops atomicity entirely in serial mode**:

```c
if (!(tcg_ctx->gen_tb->cflags & CF_PARALLEL)) { op &= ~MO_ATOM_MASK; op |= MO_ATOM_NONE; }
```

So single-threaded / round-robin TCG pays nothing. MTTCG only auto-enables when
`guest_default_memory_order & ~TCG_TARGET_DEFAULT_MO == 0`, which for x86-on-ARM64 is **false** — so
**MTTCG is not enabled by default for an x86 guest on an ARM64 host**; you get round-robin single-threaded
TCG unless you force `-accel tcg,thread=multi`. That's a headline correctness/perf fact for anyone planning
SMP guests.

QEMU does have a rich atomicity model in `include/exec/memop.h` (`MO_ATOM_IFALIGN`, `MO_ATOM_IFALIGN_PAIR`,
`MO_ATOM_WITHIN16`, `MO_ATOM_WITHIN16_PAIR`, `MO_ATOM_SUBALIGN`, `MO_ATOM_NONE`) and the AArch64 backend uses
`have_lse2` to pick `MO_ATOM_WITHIN16` when FEAT_LSE2 is present, falling back to LDXP/STXP loops for 128-bit.
Atomic guest RMW goes through `tcg_gen_atomic_*`; if it can't be done natively, `EXCP_ATOMIC` exits and the
instruction is re-executed under a global lock.

### 3.3 SMC and TB invalidation

Physical-page granularity: each TB records `tb_page_addr0`/`tb_page_addr1`; pages holding translated code are
write-protected (`PAGE_WRITE_ORG`), and a write fault triggers `tb_invalidate_phys_page`. Under MTTCG the page
table is a **lockless radix tree with cmpxchg insertion**, direct jumps are patched atomically via
`tcg_gen_tb_set_jmp_target()`, and per-TB `jmp_lock` protects the jump lists. This design is the reference; it
handles the hard case (guest kernel writing to a page containing code executing on another vCPU).

### 3.4 The softmmu cost — quantified

`accel/tcg/cputlb.c` (93 KB) implements a per-vCPU direct-mapped TLB plus a small fully-associative victim
TLB. The fast path is emitted inline by each backend. From `tcg/aarch64/tcg-target.c.inc`
(`prepare_host_addr`, line 1650), on an **AArch64 host** the emitted sequence per guest memory access is:

1. `LDP tmp0, tmp1, [env, #tlb_mask_table_ofs]` — load `CPUTLBDescFast.{mask,table}`
2. `AND tmp0, tmp0, addr LSR #(PAGE_BITS - ENTRY_BITS)` — index
3. `ADD tmp1, tmp1, tmp0` — entry address
4. `LDR tmp0, [tmp1, #addr_read|addr_write]` — comparator
5. `LDR tmp1, [tmp1, #addend]` — host offset
6. *(optional)* `ADD tmp2, addr, #(s_mask - a_mask)` for unaligned checks
7. `AND tmp2, addr_adj, #compare_mask`
8. `CMP tmp0, tmp2`
9. `B.NE slow_path`
10. the actual `LDR`/`STR` at `addr + addend`

≈ **9–11 host instructions with three dependent loads, for every single guest load or store.** Published
whole-program figures agree: QEMU 6.1 in system mode averages ~**17.4 host instructions per guest
instruction**, of which about **20 host instructions per translated memory instruction** go to MMU emulation
(see *Fast TLB Simulation for RISC-V Systems*, arXiv:1905.06825, and derived measurements).

**This is the number that should drive natemu's architecture.** If you can avoid a softmmu — via shadow paging
on the host MMU, or by exploiting a 64-bit host address space with a guest-physical window — you win back a
large multiple. If you can't, budget ~10 instructions/access and design the TLB layout carefully (QEMU's
`CPUTLBEntry` is deliberately power-of-two sized, `addend` adjacent to the comparator so one base register
serves both loads).

`tcg_use_softmmu` is a *runtime* flag in modern QEMU, and `qemu-user` compiles the fast path out entirely —
useful precedent for building one engine with both modes.

### 3.5 Plugins, and what else is worth stealing

`accel/tcg/plugin-gen.c` + `include/qemu/qemu-plugin.h`: a stable, GPL-compatible plugin ABI with
translation-block / instruction / memory-access callbacks and register access. If you want instrumentation,
this is a good API to imitate. `tcg/perf.c` and `tcg/debuginfo.c` give perf/jitdump integration — copy the
*idea* (your JIT will be unprofilable otherwise).

### 3.6 Verdict

- **Do not link QEMU code into an MIT/Apache tree.** GPL-2.0 is the single biggest strike against reuse here.
- **Do port the designs**: the softmmu TLB layout, the `MO_ATOM_*` atomicity taxonomy, the physical-page TB
  invalidation scheme, and `tcg_gen_req_mo`'s "guest MO minus host MO" barrier calculus are all clean-room
  reimplementable from the documentation.
- **Do use QEMU as the reference implementation and bring-up target.** `qemu-system-x86_64 -accel tcg` on an
  ARM64 Mac boots real guests today; UTM ships exactly this. It is your baseline to beat and your
  differential-test partner.

---

## 4. box64 / box86 — the "no IR" school

**Repo:** <https://github.com/ptitSeb/box64> · **MIT** · 5,574 ★ · 453 forks · last push **2026-08-08** ·
v0.4.4 · ~22 MB of C. box86: <https://github.com/ptitSeb/box86>, MIT, 3,794 ★, last push 2026-01 (effectively
feature-frozen; box64 subsumes 32-bit guests via `box32.c`).

### 4.1 Architecture: direct emit, but multi-pass

There is **no IR**. `src/dynarec/arm64/dynarec_arm64_00.c` and siblings are giant `switch(opcode)` bodies that
decode ModR/M with macros and call ARM64 emitters directly:

```c
case 0x00:
    INST_NAME("ADD Eb, Gb");
    SETFLAGS(X_ALL, SF_SET_PENDING);
    nextop = F8;
    GETEB(x1, 0); GETGB(x2);
    emit_add8(dyn, ninst, x1, x2, x4, x5);
    EBBACK;
    break;
```

But it is not single-pass. `src/dynarec/dynarec_native.c` drives passes **0 → 0.1–0.5 → 1 → 2 → 2.5 → 3**:
pass 0 builds a per-x86-instruction `instruction_arm64_t` metadata array (jump targets, flag liveness,
register liveness); passes 0.x propagate flag/SIMD liveness and do DCE; pass 1 allocates FP/SIMD registers;
pass 2 sizes and lays out native code; pass 2.5 allocates the buffer; pass 3 emits. The same switch bodies run
at each pass, gated by `STEP`/`PASS2`/`PASS3` macros. So: a compiler over a flat metadata array rather than a
node graph.

The entry point closest to a reusable API is
`FillBlock64(uintptr_t addr, int is32bits, int inst_max, int is_new, int noalt)` → `dynablock_t*`.

### 4.2 Register map (static)

`src/dynarec/arm64/arm64_mapping.h`:

```c
#define xRAX 10 ... #define xR15 25      /* TO_NAT(A) == xRAX + A  */
#define xFlags 26  #define xRIP 27  #define xSavedSP 28
#define xEmu 0                            /* r0 = x64emu_t*        */
/* x1..x6 scratch, x87pc = 7, xLR = 30    */
```

All 16 GPRs + RIP + flags permanently pinned to x10–x28. No dynamic GPR allocator. SIMD/x87/MMX *do* get a
lightweight dynamic allocator over the 32 NEON registers via a `neoncache_t`.

### 4.3 Flags: lazy, plus a "native flags" fast path

Classic deferred flags: `x64emu_t.df` holds `op1/op2/res` + an opcode discriminator; EFLAGS are only
materialized when consumed. A `flagcache_t` tracks `status_unk/set/none_pending/none` per instruction.

The newer **`BOX64_DYNAREC_NATIVEFLAGS`** (default on) lets x86 condition codes ride directly in ARM64 NZCV
produced by the real `ADDS`/`SUBS`. `instruction_arm64_t` carries `need_nat_flags`, `before_nat_flags`,
`nat_flags_op`, `set_nat_flags`, `use_nat_flags`; constants `NF_EQ/NF_SF/NF_VF/NF_CF/NF_PF_V`; and the
`GOCOND` conditional-jump macro branches on `IFNATIVE(A)` to emit a native `B.cs`/`B.cc` (with an
`INVERTED_CARRY` fixup for x86's borrow convention) instead of `TSTw_mask(xFlags, …)`. `dynarec_arm64_arch.c`
serializes this state (`arch_flags_t`) so a mid-block SIGSEGV can still reconstruct correct x86 flags.

This is convergent evolution with FEX's `CachedNZCV`. Both projects independently concluded that
**mapping x86 flags onto host NZCV, with liveness-driven elimination, is the right answer** — a strong signal
for our own design.

### 4.4 SIMD, atomics, SMC, chaining

- **SIMD:** AVX/AVX2 since v0.3.0 (July 2024); YMM = two 128-bit NEON halves, so **no SVE required**.
  `BOX64_AVX=1` → AVX/BMI1/F16C/VAES; `BOX64_AVX=2` → AVX2/BMI2/FMA/ADX/VPCLMULQDQ. x87 is *not* true 80-bit.
- **Atomics:** `arm64_lock.h`/`.S` with LDAXR/STLXR/LDAR primitives for `LOCK`-prefixed ops.
  `BOX64_DYNAREC_STRONGMEM` (0–4, escalating TSO strictness) and `BOX64_DYNAREC_WEAKBARRIER` (0/1/2) control
  ordering; per-instruction `will_write/will_read/last_write/lock` bits (commented `// [strongmem]`) decide
  where a barrier is actually needed rather than fencing every access. **Apple Silicon's hardware TSO bit is
  not used** (discussion #2420 — known idea, not implemented).
- **SMC:** `src/dynarec/dynablock.c`, `protectDB()`/`protectDBJumpTable()` mprotect the guest code page
  read-only; the SIGSEGV handler calls `MarkDynablock()` and routes future entries through `block->jmpnext`.
  Pages known to mix code and data are exempted (`always_test`/NEVERCLEAN) and get a runtime `checkCRC()`
  at block entry instead. Knobs: `BOX64_DYNAREC_DIRTY`, `BOX64_DYNAREC_SMC`, `BOX64_DYNAREC_NOHOTPAGE`.
- **Chaining:** `CreateJmpNext` patches an `LDR literal` + `BR` trampoline for direct jumps. A `callret_t`
  array implements a **return-address predictor**: `CALL`/`RET` pairs pre-plant a `NOP` or `UDF` so `RET`
  jumps straight back instead of going through the dispatcher (`BOX64_DYNAREC_CALLRET`, default 2).

### 4.5 The wrapping philosophy — and why it doesn't transfer

box64's headline trick is *not* the dynarec: `rebuild_wrappers.py` parses signature strings (`iFpp` =
returns int, args pointer/pointer) in `src/wrapped/*_private.h` and generates glue that pulls x86 ABI
arguments out of `x64emu_t` and **calls the host's native ARM64 libc / libGL / SDL2 / libpthread**. Hot loops
in system libraries therefore run as untranslated native ARM64 code. This is where much of the "~90 % of
native" figure comes from.

**For a system-level emulator this is worth exactly nothing** — a guest kernel has no host-substitutable
shared libraries. Both box64's and FEX's biggest practical performance levers (wrapping, thunking) evaporate
when the workload is a kernel. Plan accordingly: our achievable ceiling is closer to the pure-dynarec number
than to the marketed number.

### 4.6 Published performance

The canonical comparison (box86.org, Raspberry Pi 400, 4×1.8 GHz Cortex-A72, Ubuntu 21.10 arm64, March 2022,
updated Feb 2025):

| Benchmark | QEMU x86 | QEMU x86-64 | FEX x86 | FEX x86-64 | box86 | box64 | armhf native | aarch64 native |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 7-zip (MIPS) | 691 | 931 | 1197 | 1486 | 3117 | 3084 | 6157 | **5787** |
| dav1d 1-thread | 10.57 | 11.24 | 21.75 | 27.28 | 49.94 | 49.72 | 170.07 | **185.57** |
| dav1d 4-thread | 11.47 | 15.65 | 45.31 | 52.67 | 118.97 | 116.64 | 290.99 | **312.23** |
| glmark2 | 2 | 4 | 139 | 164 | 179 | 178 | — | **181** |
| OpenArena | err | 0.083 | 0.9 | 1.8 | 4.2 | 8.4 | 5.2 | **8.4** |

Ratios to native aarch64: **7-zip — box64 53 %, FEX 26 %, QEMU 16 %. dav1d — box64 27 %, FEX 15 %, QEMU 6 %.**
Note dav1d is hand-written SSE, which is the worst case for a translator; glmark2 is GPU-bound so everything
except QEMU hits native.

Rosetta 2 comparison from the same source (Apple M1, 7-zip): macOS native 47,239 / Rosetta 2 33,746 (**71 % of
native**); Linux native 43,271 / box64 24,746 (**57 % of native**).

A 2026 datapoint (foo2zjs raster filter, Cortex-A53): box64 tuned 19.25 s, FEX 21.23 s, native aarch64 7.80 s
— **box64 41 %, FEX 37 % of native** on an in-order core.

Caveat: this benchmark set is 2022-vintage and both projects have improved a lot; FEX in particular has landed
a new register allocator, AVX128 rework, AFP/RPRES, and x87 inlining since. Treat the *ordering* as roughly
stable and the *magnitudes* as dated.

### 4.7 Verdict

box64 is MIT and technically excellent, but as a **building block** it is markedly worse than FEX:
every emitter macro reads and writes a global `x64emu_t` whose layout is Linux-process-shaped, there is no IR
boundary to cut along, and the value-add (wrapping) is irrelevant to us. **Read it for techniques** —
`arm64_mapping.h`'s register map, the NATIVE_FLAGS design, the `callret` return-address predictor, the
CRC-vs-mprotect SMC hybrid — and take FEX for code.

---

## 5. LLVM-based lifting: Rellume / Instrew

**Rellume** <https://github.com/aengelke/rellume> · LGPL-2.1+ · 512 ★ · last push **2024-06-17** (stale) · ~1 MB.
**Instrew** <https://github.com/aengelke/instrew> · LGPL-2.1+ · 291 ★ · last push **2024-06-07** (stale) · ~290 KB.

Rellume lifts x86-64 / AArch64 / RV64 machine code to LLVM IR, at **whole-function granularity**, into a
function taking a pointer to a CPU-state struct. Instrew wraps it in a client/server DBT: the lightweight
client owns the guest address space, code cache and execution; the server does lifting, instrumentation and
LLVM codegen, returning ELF objects that the client relocates and links.

### 5.1 The numbers (VEE'21, SPEC CPU2017 ref, single-threaded)

Setup: Intel Xeon E5-2697v3 (x86-64 host) and Cavium ThunderX2 99xx (AArch64 host); GCC 9.2/10.2 `-O3`;
QEMU 5.2.0; HQEMU 2.5.2 hybrid; LLVM 9.

**x86-64 guest → x86-64 host** (mean *overhead* over native):

| System | All | INT | FP |
|---|---:|---:|---:|
| **Instrew** | **53 %** | 67 % | 40 % |
| HQEMU-hybrid | 124 % | 136 % | 114 % |
| **QEMU** | **716 %** | 275 % | **1541 %** |

QEMU's FP slowdown alone ranges **7.7×–38× (geomean 16.4×)** — that is soft-float emulation, and it is the
single most damning QEMU number in the literature. Best case for Instrew: 619.lbm at **7 % overhead**.

Optimization ablation (normalized runtime vs native, x86-64→x86-64):

| Benchmark | base | +hhvm CC | +callret | both |
|---|---:|---:|---:|---:|
| geomean | 1.96 | 1.69 | 1.85 | **1.53** |
| avg. translation time | 782 s | 875 s | 1245 s | **1881 s** |

**RISC-V → x86-64:** Instrew 76 % vs QEMU 202 % (INT); 40 % vs 1267 % (FP).

**x86-64 → AArch64** (Fig. 3): "overall significant performance improvements compared to state-of-the-art
tools," but *worse than on an x86-64 host*, for three stated reasons — (1) **the HHVM calling convention is
not supported by upstream LLVM on AArch64, so the entire register state is written back to memory** at every
translation-unit boundary; (2) x86-64 guests rarely use AArch64's extra registers; (3) the ThunderX2 does
fewer internal optimizations. This is the crux: *the LLVM approach's biggest win (keeping guest registers in
host registers across translated units) is unavailable on our exact target.*

### 5.2 Techniques worth stealing

- **Call/return optimization ("callret"):** function calls in guest code are lifted to real LLVM calls to the
  dispatcher; guest returns become LLVM `ret`; indirect jumps become tail calls. This **implicitly implements
  a shadow stack and exploits the host CPU's return-address predictor** — worth ~0.11–0.69 normalized runtime
  per benchmark. box64's `callret` and FEX's `REG_CALLRET_SP` are the same idea.
- **Register-mapped calling convention:** map a fixed set of guest registers to host argument/return
  registers so translated units pass state in registers. On ARM64 this needs a custom calling convention in
  the backend — i.e. **if you write your own backend you get this for free, and LLVM denies it to you.**
- **Whole-function granularity** beats basic-block granularity, and beats HQEMU's trace merging.

### 5.3 Why not adopt Instrew/Rellume

1. **Compile time.** 602.gcc spends **~55 % of its wall time translating**; the full optimization set raises
   average translation time by **140 %** (782 s → 1881 s across the suite). For an interactive VM boot this is
   fatal without a tiering scheme that these projects don't have.
2. **Correctness gaps that matter for us.** Atomics: RISC-V LR/SC is lifted as *non-atomic stores* ("very hard
   to represent in LLVM-IR; left to future work"). Rounding modes: LLVM has "only very basic support" for
   non-default modes, so Rellume software-emulates some conversions. There is no memory-model story for x86
   TSO on ARM64 at all.
3. **Userspace only, no MMU, no privileged instructions.**
4. **Both repos stale since mid-2024**; LGPL-2.1+ (workable — dynamic linking, publish modifications — but not
   as free as MIT).
5. LLVM as a runtime dependency is ~100 MB of build and a moving target.

**Verdict:** read the papers, steal callret and the register-mapped-CC idea, don't take the code. The
strongest *strategic* takeaway is that Instrew's 53 % overhead is an existence proof that ~1.5× native is
reachable for x86-64 DBT with good codegen — and that QEMU's 8× is a *codegen quality* problem, not an
inherent DBT cost.

**HQEMU** (CGO'12, TCG fast path + LLVM optimization of hot traces in a background thread) sits between them
at 124 % overhead but requires a patched LLVM 6.0 and is unmaintained. **RevNG** lifts *through* QEMU's TCG
into LLVM IR — architecturally interesting (it reuses QEMU's frontends for free) but GPL-2.0 and aimed at
static analysis. **libcpu** tried exactly the "lift everything to LLVM" plan and was archived in 2021 with x86
support incomplete — the cautionary data point.

---

## 6. Trail of Bits' lifting stack and the rest of the LLVM crowd

### 6.1 Remill — the best *permissively licensed* x86-64 semantics library

<https://github.com/lifting-bits/remill> · **Apache-2.0** · ~1,780 ★ · 184 forks · last push **2026-07-01** ·
~10.6 MB.

Remill's distinguishing idea: **instruction semantics are written as C++ template functions, compiled to LLVM
bitcode by clang ahead of time, then inlined at lift time** and cleaned up by LLVM's optimizer. You never
hand-write an IR emitter per instruction.

- **Coverage** (`lib/Arch/X86/Semantics/`): `AVX, BINARY, BITBYTE, CALL_RET, CMOV, COND_BR, CONVERT, DECIMAL,
  FLAGOP, FLAGS, FMA, INTERRUPT, IO, LOGICAL, MMX, POP, PUSH, ROTATE, RTM, SEMAPHORE, SHIFT, SSE, STRINGOP,
  SYSCALL, SYSTEM, UNCOND_BR, X87, XOP, XSAVE`. Note **`SYSTEM.cpp`, `IO.cpp`, `INTERRUPT.cpp`** — Remill
  actually models some privileged/system instructions, which is rare in this survey. README claims AVX-512
  support (folded into `AVX.cpp` via templated wide-vector types). Architectures: x86, amd64, AArch64,
  SPARC32/64.
- **Flags** are *functional*, not emulated hardware: `ZeroFlag(T res) { return T(0) == res; }`, sign flag via a
  `__remill_flag_computation_sign(...)` intrinsic. The consumer decides how flags are physically stored — which
  is exactly the flexibility a project mapping onto ARM64 NZCV wants.
- **Memory is token-threaded:** `Memory *__remill_write_memory_32(Memory*, addr_t, uint32_t)`,
  `uint32_t __remill_read_memory_32(Memory*, addr_t)` — each write consumes and returns an opaque `Memory*`,
  expressing sequential dependency in pure SSA without aliasing analysis.
- **Memory model is first class:** `__remill_barrier_load_load / load_store / store_load / store_store`,
  `__remill_atomic_begin/end`, `__remill_compare_exchange_memory_N`. Almost nothing else in this survey models
  ordering at the IR level. For x86-TSO-on-ARM64 that is directly useful vocabulary.
- **Static only.** Remill produces bitcode to be compiled offline; there is no JIT, no code cache, no chaining.
- **Speed of lifted code is the weak point.** Heavy inlining of generic templated semantics plus token-threaded
  memory means LLVM must "see through" a lot before it can generate tight code. Remill publishes no
  performance numbers, which is itself telling — it is positioned as an offline analysis tool.

**Verdict:** the highest-value thing here for us is **the semantics corpus as a cross-check / gap-filler**, not
as a runtime. If FEXCore is missing an instruction, Remill (Apache-2.0) is a legitimate place to read the
correct semantics from. Its `Memory*` intrinsics vocabulary is worth imitating in our IR. Do **not** plan to
execute Remill-lifted code.

### 6.2 The license fault line: McSema / anvill are AGPL

| Project | License | Status |
|---|---|---|
| **remill** (instruction semantics) | **Apache-2.0** | active 2026 |
| **rellic** (bitcode → C decompiler) | **Apache-2.0** | active, 607 ★ |
| **mcsema** (program-level lifting) | **AGPL-3.0** | **archived 2022-08-23** |
| **anvill** (McSema's successor) | **AGPL-3.0** | 374 ★, last push 2024-09; main branch consumes specs from a **closed-source** Ghidra plugin |

The seam sits exactly between *instruction semantics* (permissive) and *program recovery / CFG reconstruction*
(AGPL). AGPL's network-copyleft would reach a hosted translator service. **Take Remill; never link anvill or
McSema.**

### 6.3 RevNG

<https://github.com/revng/revng> · **GPL-2.0 as a whole** · ~1,716 ★ · last push 2026-08-07 · ~20 MB · 7,974
commits on `develop`.

Lifts *through QEMU's TCG frontends* ("revamb") into LLVM IR, then does stack/ABI/type recovery. Architecturally
elegant — it gets QEMU's enormous instruction coverage for free — but individual files carrying MIT headers do
not save it: **the QEMU dependency makes the pipeline GPL-2.0**. Reusable only via process isolation, not
in-process linking. Aimed at static analysis, not execution.

### 6.4 HQEMU

<http://csl.iis.sinica.edu.tw/hqemu> — **the site is dead** (connection refused), confirming abandonment.
Design (Hong et al., CGO 2012): TCG runs cold code and profiles it; hot **traces** are handed to a background
LLVM optimizing thread that recompiles them and hot-swaps the TCG version. Requires a patched LLVM 6.0.
Measured by the Instrew team at **124 % overhead** vs native on SPEC CPU2017 x86-64→x86-64 (vs QEMU's 716 %),
so the hybrid idea works — but it "still suffers from the known architectural limitations of TCG: it is not
possible to further increase translation granularity to allow more complex control flows."

**The technique — cheap tier-0 JIT + background optimizing tier-1 for hot code — is the single most important
architectural idea to carry forward**, because it is the only known answer to the compile-time problem that
sinks pure-LLVM approaches (§5.3).

### 6.5 Others, briefly

- **retdec** (avast/retdec, **MIT**, 8,601 ★, active 2026-05): static retargetable *decompiler* to readable C.
  Permissive and broad, but solving a different problem.
- **BinRec**: research system that runs a binary under QEMU/PANDA-style tracing, **merges multiple execution
  traces** into one LLVM IR module, and recompiles a standalone binary. The trace-merging technique for
  recovering indirect-jump targets is the transferable idea. Canonical repo location unconfirmed.
- **Egalito** (columbia/egalito, GPL-3.0, 226 ★): binary *recompiler* for hardening, with its own low-level
  "Chunk/EIR" layout-preserving IR; x86-64 + AArch64. Reference for metadata-driven binary rewriting.
- **rv8** (rv8-io/rv8, **MIT**, 724 ★): RISC-V→x86-64 JIT with jump-target caching, inline caches and macro-op
  fusion. Mirror image of our problem; the Instrew paper notes it uses "a fixed register mapping to reduce
  memory accesses" and gets speedups comparable to Instrew's, but "is not easily portable."

---

## 7. QEMU-derived libraries and interpreters

### 7.1 Unicorn Engine

<https://github.com/unicorn-engine/unicorn> · **GPL-2.0** · 9,217 ★ · last push 2026-07-30 · ~21 MB ·
197 open issues · **no tagged release since 2.1.4 (Sept 2024)**.

Unicorn 2.x forked **QEMU 5.0.1** and is explicitly not kept in sync — the FAQ: *"Unicorn does lots of dirty
hacks within qemu code which prevents it from sync painlessly."* The real engineering contribution is
**de-globalizing TCG**: QEMU's singleton CPU/TB-cache/MMU state was refactored into per-`uc_engine` context so
multiple independent CPU instances coexist in one process. That patch set is the most interesting artifact
here — proof that TCG *can* be made reentrant — but it is large, invasive, tied to QEMU 5.0 internals, and
forward-porting it would be a from-scratch redo.

Since 2.0.2 Unicorn emulates the target's **real MMU** (page tables, CR3): `uc_mem_map` takes physical
addresses, `uc_emu_start` takes virtual addresses, and `UC_TLB_VIRTUAL` restores the old flat mode for speed.
But it is a **headless CPU, not a system emulator**: no BIOS, no chipset, no interrupt controller, no devices,
and CPL/privileged-instruction semantics are incomplete (issue #252). Hooks (`UC_HOOK_CODE`) can cost ~10×.

**Verdict:** excellent for prototyping and differential testing with real paging; GPL-2.0 and 6-year-old QEMU
internals rule it out as a shipped component.

### 7.2 Blink

<https://github.com/jart/blink> · **ISC** (maximally permissive) · 7,566 ★ · last push 2025-12-10.

Interpreter + an unusual JIT: `blink/jit.c` generates trampolines that splice together *already-compiled* C
micro-op functions — Blink scans its own compiled functions for the trailing `RET` and `memcpy`s the bytes into
a generated stub. Copy-and-patch, not instruction selection. Warms up instantly (great for short-lived
processes) and reportedly beats `qemu-x86_64` ~2× on some SSE-heavy code.

Scope: **Linux userspace only**, ~150 syscalls, ~500 x86 instructions (through SSSE3-ish). `blink/memory.c` has
a soft address-space model for the guest process image, not hardware paging. Famous for a **~220 KB static
binary** (~115 KB minimal).

**Verdict:** ISC license and a clean table-driven decoder make `blink/x86.c` a plausible standalone decoder
donor, but Blink's whole design is size-optimized interleaving — extraction is surgery. The copy-and-patch JIT
idea is worth knowing about (it is the cheapest possible tier-0). Contributes nothing at system level.

### 7.3 Bochs

<https://github.com/bochs-emu/Bochs> (mirror; canonical home SourceForge) · **LGPL-2.1** · 1,346 ★ ·
last push 2026-08-07 · 14,500+ commits.

Pure interpreter, but the **most architecturally faithful open x86-64 implementation that exists**: 5-level
paging / 57-bit linear addresses, SMM, VMX (posted interrupts, `IA32_SPEC_CTRL` virtualization), AVX-512
subsets (VBMI2, VNNI, BITALG, VAES, VPCLMULQDQ, GFNI), with a matching disassembler. `cpu/cpudb/` holds
per-model CPUID feature bundles (Skylake-X, Icelake-U, …) — directly useful as a source of CPUID profiles.
There's a first-class instrumentation API. Speed is tens of MIPS.

**Verdict: adopt as the correctness oracle, not as a runtime component.** Differential-testing a new
translator against Bochs instruction-by-instruction (especially for paging, flags, and x87/SSE corner cases)
is the highest-leverage QA investment available, and LGPL keeps it at arm's length as a separate process.
`cpu/fetchdecode*` tables and `cpudb` are also a useful secondary source of x86 semantics next to the SDM.

### 7.4 86Box / PCem, libcpu

**86Box** <https://github.com/86Box/86Box> · GPL-2.0+ (mixed) · 4,549 ★ · active. Excellent, decoupled legacy
PC device models (chipsets, IDE/SCSI/MFM, VGA, sound) behind an I/O-port/MMIO dispatch layer. **Wrong era** for
a paravirtualized x86-64 Linux guest — you want virtio, not a Sound Blaster. Note only as a good example of
bus/device decoupling.

**libcpu** <https://github.com/libcpu/libcpu> · BSD-2 · 363 ★ · **archived 2021-09**. Per-arch frontends
lifting to LLVM IR with MCJIT as the backend; x86 support never completed. Dead. Lesson: a complete, correct
CISC frontend is the expensive part, and "LLVM will handle the rest" does not rescue you from it.

### 7.5 Valgrind VEX

Canonical home valgrind.org; the practically-usable fork is <https://github.com/angr/vex> (114 ★, GPL-2.0,
active 2026-07-22, ~7.8 MB), maintained to back **pyvex/angr**.

- **IR:** `libvex_ir.h` defines `IRSB` (superblock, single-entry/multi-exit, 1–50 guest instructions),
  a clean `IRStmt` (side effects: `Put`/`PutI` to guest state offsets, stores, temp assignments) /
  `IRExpr` (pure trees) split, **SSA `IRTemp`s** (runtime-checked single assignment), fully typed, arena-managed.
  Architecturally the most mature general-purpose lifting IR in this survey.
- **Flags:** the canonical **lazy thunk** model — the frontend `Put`s `CC_OP`, `CC_DEP1`, `CC_DEP2`, `CC_NDEP`
  describing *what operation produced the flags*, and `calculate_condition`/`calculate_eflags` materialize
  them only when read. QEMU uses the same design.
- **Backend:** `host_arm64_isel.c` — VEX genuinely has an **AArch64 host backend** with instruction selection
  and register allocation. It is one of very few complete, mature open x86-64→ARM64 lifting *and* codegen
  pipelines.
- **Extraction is proven** (pyvex uses VEX purely as a lifter, with no Valgrind runtime).
- **Weaknesses:** GPL-2.0+; codegen quality is explicitly not a goal (Valgrind's own baseline JIT overhead is
  ~4×, ~100× under Memcheck); atomics are helper-based with no concurrent memory model; userspace-oriented,
  no ring-0 semantics.

**Verdict:** the best *non-MIT* alternative to FEXCore for the frontend. If FEX's license or C++ style were a
problem, VEX would be the fallback. Given FEXCore is MIT and has a better-tuned ARM64 backend, VEX is second
choice — but its IR design (typed, SSA, guest-state-offset-addressed) is worth copying.

---

## 8. System-level DBT: the prior art that actually matters

Everything in §2–§7 except QEMU and Bochs is a *userspace* translator. The literature on translating
**privileged** code is much thinner, and it is where natemu's real risk lives. Three references are worth
reading in full:

**Kedia & Bansal, "Fast Dynamic Binary Translation for the Kernel," SOSP 2013**
(DOI [10.1145/2517349.2522718](https://doi.org/10.1145/2517349.2522718), pp. 101–115). DBT applied to the OS
kernel itself. The hard parts they address are exactly ours: interrupts arriving mid-translated-block, page
faults with precise state, instructions that affect the MMU, and kernel code patching (Linux does a lot of
runtime self-modification — alternatives, static keys, ftrace). Their headline result is that with careful
code-cache design and by *avoiding trap-and-emulate for privileged operations*, kernel DBT overhead can be
kept to low single-digit percentages. **Directly on point; read it before designing the code cache.**

**Jiang et al., "A System-Level Dynamic Binary Translator using Automatically-Learned Translation Rules,"
CGO 2024** ([arXiv:2402.09688](https://arxiv.org/abs/2402.09688)). Extends learning-based DBT — previously
user-mode only — to full-system emulation, prototyped on QEMU. Contributions are named "coordination overhead
reduction," "coordination elimination," and "code scheduling," i.e. techniques for cheaply interleaving
fast-path translated code with the slow-path system-emulation layer. Results: **1.36× over QEMU 6.1 on SPEC
CINT2006, 1.15× on real applications**, at system level. Two readings: (a) beating QEMU at system level is
hard — a whole CGO paper buys 15–36 %; (b) the "coordination overhead" framing is exactly the problem any
translator faces when every guest memory access might need to consult a software MMU.

**Historical hardware-assisted whole-system translation** — the existence proofs that this design point works:
- **Transmeta Crusoe/Efficeon**: x86 via *Code Morphing Software*, a software VM below the OS translating x86
  into VLIW "molecules" of 2 or 4 32-bit "atoms". A 700 MHz Crusoe delivered roughly 500 MHz Pentium III-class
  x86 performance — ~70 % of contemporaneous native, achieved on 2000-era silicon with translation *of
  everything including ring 0*. Crusoe shipped without SSE (MMX only), which is what actually killed it;
  Efficeon widened to a 256-bit VLIW and added SSE.
- **NVIDIA Denver / Carmel**: a simple hardware ARM decoder plus software DBT running *below the OS*,
  translating to an internal VLIW format and caching optimized translations in a **128 MB reserved region of
  main memory**. Shipped in Tegra K1 (Nexus 9, 2014) at 2.5 GHz; Carmel (2018) is 10-wide superscalar.
  Same-ISA optimization rather than cross-ISA, but the "translation cache as a reserved physical memory
  region, invisible to the OS" pattern is instructive.
- **IBM DAISY** (Yorktown, late 1990s): PowerPC → internal VLIW at runtime; originated shadow registers for
  precise-exception recovery and tree-region translation grouping. Both techniques are still the standard
  answers.
- **Intel Houdini**: ARM-on-x86 for Android on Atom, discontinued ~2016, essentially undocumented. The mirror
  image of our problem, and a reminder that these layers are business decisions as much as technical ones.

---

## 9. Proprietary state of the art (calibration targets, not reuse)

### 9.1 Apple Rosetta 2

Architecture (publicly documented): **AOT translation at install time** — x86-64 code is "fully translated,
just once, when an application is installed on an Apple silicon Mac" — plus a **JIT** for code the AOT pass
cannot see (guest JITs, dynamically generated code). Translated binaries are cached under `/var/db/oah`.

Its speed comes substantially from **hardware**:
- **A hardware TSO mode**: Apple cores can be switched to Total Store Order so translated code needs *no*
  memory barriers at all. XNU flips this "through modifications to a special register upon exit from the
  kernel for programs running under Rosetta 2." This is the single largest advantage Rosetta has over every
  open-source translator, and §10.5 explains why we probably cannot have it.
- **Dedicated x86-flag-computation instructions** in the SoC (undocumented Apple extensions), removing the
  cost that FEX and box64 pay via NZCV juggling and PF/AF pinning.

x87 80-bit extended precision is *not* preserved bit-exactly (ARM has no 80-bit float); translated x87
generally runs at 64-bit double precision — the same compromise FEX offers as `X87F64` and box64 makes
unconditionally.

The *standardized* ARM equivalents of Apple's tricks, which we **can** target on any modern ARMv8.4+ core:
**FEAT_FlagM / FlagM2** (`CFINV`, `RMIF`, `SETF8/16` — cheap x86-style flag manipulation; FEX is planning to
require this, issue #4120) and **FEAT_JSCVT / FJCVTZS** (ARMv8.3, JS-semantics float→int truncation, useful
for x86 conversion corner cases). Detect at runtime; do not assume Apple's undocumented extras.

Published headline number (box86.org, M1, 7-zip): **Rosetta 2 ≈ 71 % of native**, vs box64 ≈ 57 %. Other
commonly quoted "70–80 % of native" figures are folklore-grade; Apple publishes no SPEC numbers.

### 9.2 Microsoft x86/x64-on-ARM64

Per Microsoft Learn (`/windows/arm/apps-on-arm-x86-emulation`, `/windows/arm/arm64ec`, current as of 2026):

- A **block JIT**: "just-in-time compiling blocks of x86 instructions into Arm64 instructions with
  optimizations... A service caches these translated blocks of code to reduce the overhead of instruction
  translation... produced for each module so other apps can make use of them." That is `xtajit.dll` /
  `xtajit64.dll` plus the **XtaCache** service — a *shared, per-module, on-disk* translation cache, which is a
  design worth copying for a VM that boots the same kernel repeatedly.
- **x64 apps get no WOW64 layer**: the OS ships **Arm64X PE binaries** (dual x64 + ARM64EC code in one file)
  so emulated x64 processes load OS components directly.
- **ARM64EC** is an *ABI*, not a translator: it lets a module be recompiled to native ARM64 while keeping the
  x64 calling convention, stack layout and struct layout, so native and emulated code interoperate in one
  process with no marshaling boundary. FEX implements an ARM64EC backend (`Source/Windows/ARM64EC/`), which is
  why FEXCore is provably OS-portable.
- **Prism** (Windows 11 24H2) is the rewritten emulator, "optimized and tuned specifically for Qualcomm
  Snapdragon processors," with some features gated on Snapdragon X hardware — strongly implying it exploits
  vendor memory-ordering/flag extensions the way Rosetta does.
- **Explicitly user-mode only**: "doesn't support drivers... kernel mode components must be compiled as
  Arm64." Neither Apple nor Microsoft ships a system-level x86→ARM64 translator. **That gap is natemu's
  premise, and also its warning: nobody with a large budget has chosen to do this.**

---

## 10. The machine around the CPU: VMM frameworks and device models

If the CPU comes from FEXCore, everything else — guest physical memory, kernel loading, interrupt controller,
timers, virtio — is a separate reuse problem, and here the answer is unambiguous: **rust-vmm**.

### 10.1 rust-vmm crates

Org: <https://github.com/rust-vmm> (~34 repos; community repo 605 ★). Contributors: AWS (Firecracker), Intel,
Microsoft (Cloud Hypervisor), Alibaba, Google. **All crates dual-licensed Apache-2.0 OR BSD-3-Clause**
(a few BSD-3-only) — maximally compatible. All show commits into mid-2026.

| Crate | Version (2026) | Purpose | KVM-coupled? |
|---|---|---|---|
| `vm-memory` | 0.18.0 | Guest physical memory (`GuestAddress`, `GuestMemoryMmap`, `VolatileSlice`) | **No** |
| `linux-loader` | 0.14.0 | bzImage / ELF / PE kernel loading, boot params | **No** |
| `virtio-queue` | 0.18.0 | Virtqueue / descriptor-chain parsing | No |
| `virtio-bindings` | 0.2.7 | FFI to Linux virtio headers | No |
| `virtio-device` (in `vm-virtio`) | — | `VirtioDevice` trait + default MMIO transport | No |
| `vhost` / `vhost-user-backend` | 0.17.0 / 0.23.0 | vhost / vhost-user protocol + backend framework | No |
| `vmm-sys-util` | 0.15.0 | eventfd, ioctl helpers, terminal, syscalls | No |
| `vm-superio` | 0.8.1 | 16550 serial UART + PL031 RTC | No |
| `event-manager` | 0.4.2 | epoll event loop | No |
| `vm-allocator` | 0.1.4 | Address-range / IRQ allocation | No |
| `acpi_tables` | 0.2.1 (new, Feb 2026) | Static + AML dynamic ACPI table builder | No |
| `vm-fdt` | 0.3.0 | Devicetree blob writer (stale since 2023) | No |
| `vm-device` | 0.1.0 (**stale since 2021**, 17k downloads) | Device/resource bus | No |
| `kvm-ioctls` / `kvm-bindings` | 0.25.0 / 0.14.1 | `/dev/kvm` wrappers | **Yes** |
| `mshv-bindings` / `mshv-ioctls` | 0.7.0 | Microsoft Hypervisor | Yes (MSHV) |

`vm-memory`'s stated goal is precisely ours: *"decouple VM memory consumers from VM memory providers... boot
loader, device drivers, virtio backend drivers and vhost drivers... could be shared and reused by multiple
hypervisors."* **A software MMU sitting on top of a `GuestMemoryMmap` is exactly the intended shape.**

Only `kvm-*` and `mshv-*` touch an accelerator. Everything else is directly usable by a translator-backed VMM.

### 10.2 Cloud Hypervisor — the trait shape to imitate

<https://github.com/cloud-hypervisor/cloud-hypervisor> · Apache-2.0 (+BSD-3) · ~6.1k ★ · ~11,190 commits ·
active 2026. Its internal `hypervisor` crate is feature-gated (`kvm`, `mshv`, `sev_snp`, `tdx`,
`mshv_emulator`) and exposes:

- `Hypervisor` — factory, backend selected by feature/runtime probe (KVM first, MSHV fallback)
- `Vm` (`vm.rs:337`) — `create_vcpu()`, `set_user_memory_region()`, `IrqRoutingEntry`, `get_preferred_target()`
- `Vcpu` (`cpu.rs:396`) — **`run(&mut self) -> Result<VmExit, HypervisorCpuError>`**, `get_regs`/`set_regs`
  over a backend-neutral `StandardRegisters`, plus `CpuState`/`ClockData` for snapshot/restore
- Neutral enums: `HypervisorType { Kvm, Mshv }`, `MpState`, `IoEventAddress`, `RegList`, `Register`

**Could a translator be a third backend?** Structurally yes — a translation dispatch loop can synthesize the
same `VmExit` variants (MMIO, PIO, Hypercall, Shutdown) after running translated blocks. Friction: `CpuState`
and `StandardRegisters` are enums with only KVM/MSHV variants, and `get_preferred_target()` assumes a
hardware vCPU init packet. **Imitate the trait shape in our own crate rather than trying to extend theirs.**
The `Vcpu::run() -> VmExit` contract is the right central abstraction and costs nothing to adopt.

### 10.3 Firecracker — the minimum x86-64 boot path, and an important negative result

<https://github.com/firecracker-microvm/firecracker> · Apache-2.0 · ~35.9k ★ · ~8,235 commits · active 2026.

Boot path (`arch/x86_64/mod.rs`, `layout.rs`) — the concrete recipe:
- `load_kernel()` handles **ELF (`vmlinux`) and bzImage** via `linux-loader`
- **Two boot protocols**: `configure_pvh()` builds `hvm_start_info` + `hvm_memmap_table_entry` at
  `PVH_INFO_START = 0x6000`; `configure_64bit_boot()` fills the classic zero page / `boot_params` at
  `ZERO_PAGE_START = 0x7000`
- **e820 map** via `add_e820_entry` (`E820_RAM`/`E820_RESERVED`, MMIO gaps at 4 GiB and 256 GiB)
- **Both** `mptable::setup_mptable()` (last 1 KiB of the 640 KiB low region) **and** ACPI
  (`create_acpi_tables()`, RSDP at `0xe0000`)
- Per-vCPU CPUID/MSR normalization via `CpuConfiguration` / CPU templates

Devices (`devices/legacy/mod.rs`): on x86-64 only **`I8042Device`** (used purely as a guest reset/shutdown
signal, not a real PS/2 controller) and **`SerialDevice`** (16550 at 0x3f8). `RTCDevice` (PL031) is aarch64-only.
`device_manager/mod.rs` now gates on `pci_enabled: bool`, dispatching to `PciDevices` or `MMIOVirtioDevices`.

**The negative result matters more than the positive one:** Firecracker has **no userspace PIC, IOAPIC or
PIT**. `vstate/vm.rs` calls `KVM_CREATE_IRQCHIP` and routes via `KVM_IRQCHIP_IOAPIC` — interrupt controller
and timer emulation live *in the kernel*. So a translator-based VMM **cannot lift an interrupt controller out
of Firecracker; it must write its own** (or take QEMU's, with the GPL consequence, or paravirtualize the
problem away).

**Minimum viable x86-64 Linux boot set**, synthesized:

| Device | Needed? | Notes |
|---|---|---|
| 16550 serial @ 0x3f8 | yes (practically) | early console; `earlycon=uart,io,0x3f8` |
| i8042 stub | yes | guest-initiated reset/shutdown only |
| PIC (8259) + IOAPIC | **yes, must write** | not available from Firecracker |
| PIT (8254) / HPET | **yes, or paravirt clock** | kvm-clock / TSC + timer IRQ injection can substitute |
| CMOS/RTC | usually skippable | if a paravirt clocksource is present |
| MPTable **or** ACPI | one of them | **MPTable is far simpler** for fixed topology; Firecracker emits both |
| virtio transport | **virtio-mmio** | no PCI bus, no ECAM, no MCFG; `virtio_mmio.device=` on cmdline |
| virtio devices | blk, net, console, rng | from `virtio-queue` + Cloud Hypervisor implementations |
| PCI / virtio-pci | no, initially | only needed for passthrough/GPU/hotplug later |

### 10.4 libkrun / krunkit — and a critical disqualifier

**libkrun** <https://github.com/containers/libkrun> · Apache-2.0 · ~2.6k ★ · ~1,479 commits · libkrun 2.0
(README self-describes as "pre-stable"; C API stable since 1.0.0). A dynamic library exposing a whole VMM
behind `krun_*` calls (`include/libkrun.h`), using **KVM on Linux and Hypervisor.framework on macOS/ARM64**.
Requires the companion **`libkrunfw`** (embedded Linux kernel + firmware blob; `-sev` / `-tdx` variants exist).

**Disqualifier:** `src/vmm/src/lib.rs` is `#[cfg(target_arch = "x86_64")]` / `#[cfg(target_arch = "aarch64")]`
gated throughout — libkrun only virtualizes **same-architecture** guests, because HVF is hardware-virt only.
**On Apple Silicon, libkrun can run aarch64 Linux guests and nothing else. There is no CPU emulation path in
libkrun at all.** It is therefore not a base we can build an x86-64 guest on; it is a *reference* for
"library-shaped VMM with a C API" and for HVF integration mechanics. Its accelerator seam is ad-hoc `cfg`
branching, not a clean trait — noticeably worse than Cloud Hypervisor's for our purposes.

**krunkit** <https://github.com/containers/krunkit> · Apache-2.0 · 335 ★ · ~150 commits · active. The
macOS/Apple-Silicon launcher wrapping libkrun for `podman machine`; `cmdline.rs` confirms virtio-gpu
(`"virtio-gpu,width=800,height=600"`) backed by libkrun's `rutabaga_gfx`. **No Rosetta integration exists in
krunkit or libkrun source** (no `rosetta` string in `cmdline.rs` or `libkrun.h`). Rosetta-for-Linux in the
Podman ecosystem comes from the separate **`applehv`** provider, which talks to Virtualization.framework.

### 10.5 Apple Silicon platform constraints — read this before choosing a host target

**Hypervisor.framework** (<https://developer.apple.com/documentation/hypervisor>) lists exactly two platform
topics: "Apple silicon" and "Intel-based Mac". On Intel Macs it supports x86/VMX guests; **on Apple Silicon it
is ARM64-guest-only.** There is no path to hardware-virtualizing an x86-64 guest on an M-series Mac — which is
precisely why a translator is needed. Entitlement required: `com.apple.security.hypervisor` (plus legacy
`com.apple.vm.hypervisor` pre-macOS 11); `com.apple.vm.networking` and `com.apple.vm.device-access` for the
adjacent capabilities.

**W^X / JIT is a hard constraint.** macOS on ARM64 enforces per-thread W^X on `MAP_JIT` pages. QEMU's handling
is the reference implementation and should be copied verbatim in spirit:

```c
/* include/qemu/osdep.h, __APPLE__ */
static inline void qemu_thread_jit_execute(void) { pthread_jit_write_protect_np(true);  }
static inline void qemu_thread_jit_write(void)   { pthread_jit_write_protect_np(false); }
```
called from `accel/tcg/translate-all.c:272` before emitting code; `util/cacheflush.c` then issues
`__builtin___clear_cache()` on **both** the RW and RX aliases. `tcg/region.c` additionally supports a
**split W^X** mode via `mach_vm_remap()` — two mappings (one RW, one RX) of the same physical pages — which
avoids per-thread toggling entirely at the cost of a Mach VM call. For a multi-threaded translator, **the
split-mapping approach is strongly preferable**: `pthread_jit_write_protect_np` is per-thread state and
interacts badly with a background optimizing compiler thread (§6.4). Code signing/notarization requires the
`com.apple.security.cs.allow-jit` hardened-runtime entitlement.

**Rosetta for Linux VMs** is *not* reachable from a custom VMM built on Hypervisor.framework.
`VZLinuxRosettaDirectoryShare` (macOS 13+) is a **Virtualization.framework** class that exposes the host's
Rosetta directory to the guest over **virtiofs**; the guest registers it via `binfmt_misc` as an x86-64
interpreter. It therefore (a) only works inside an **arm64** Linux guest, (b) requires linking
Virtualization.framework, and (c) carries Apple's EULA restricting use outside Apple's frameworks. It is not a
component we can adopt, and it does not solve our problem anyway (it needs an ARM64 guest kernel).

**The TSO bit is not available to us.** <https://github.com/saagarjha/TSOEnabler> documents that XNU enables
TSO "through modifications to a special register upon exit from the kernel for programs running under Rosetta
2." TSOEnabler generalizes this via a **kernel extension** exposing a `kern.tso_enable` sysctl — requiring
**SIP disabled and a loaded kext**, pinning affected threads to P-cores, and depending on undocumented kernel
image offsets that break across macOS releases. There is no Apple-sanctioned userspace mechanism. **Plan for
software TSO (LDAR/STLR + FEX's unaligned-atomic handler) as the permanent answer on macOS**, and treat
hardware TSO as an optional fast path only on Linux hosts where it may be exposed (FEXCore already has
`SetHardwareTSOSupport(bool)` for this).

### 10.6 UTM — the existing shipped answer

<https://github.com/utmapp/UTM> · Apache-2.0 (QEMU components GPL, dynamically linked) · ~34.9k ★ · active.
Uses Hypervisor.framework for same-arch (arm64) guests and **QEMU TCG for x86-64 guests on Apple Silicon**.
It is the working, shippable proof that the W^X, code-signing and packaging problems are all solvable on
macOS — and it is also the performance baseline natemu must beat, since a user's alternative today is
"install UTM."

### 10.7 NEMU — disambiguation

Two unrelated projects: **Intel NEMU** (<https://github.com/intel/nemu>, GPL-2.0/LGPL-2.1, 926 ★,
**archived 2021-04**) was a QEMU fork stripped to a minimal cloud subset; its own repo states it is superseded
by **Cloud Hypervisor**. **OpenXiangShan/NEMU** (340 ★, active) is an unrelated RISC-V64 teaching/research
emulator used for differential testing and checkpointing in the XiangShan CPU project — no relevance here,
although its *use* as a differential-testing reference mirrors what we should do with Bochs.

---

## 11. Cross-cutting technique summary

What every serious x86→ARM64 translator converges on:

| Problem | Consensus answer | Who does it |
|---|---|---|
| **Flags** | Map x86 CF/ZF/SF/OF onto host **NZCV**; keep PF/AF in dedicated registers or compute lazily; eliminate dead flag computations by liveness | FEX (`CachedNZCV` + `RedundantFlagCalculationElimination`), box64 (`NATIVE_FLAGS`), Rosetta 2 (hardware) |
| **Flags (fallback)** | Lazy thunks: record op + operands, materialize on read | QEMU (`CC_OP/CC_DEP*`), VEX, box64's classic `df/op1/op2/res` |
| **TSO on weak host** | Prefer **LDAR/STLR acquire-release** over full `dmb`; make it a first-class IR distinction; make it configurable per-module | FEX (`LoadMemTSO`), box64 (`STRONGMEM` levels) |
| **TSO (blunt)** | `dmb ish` before every access when parallel | QEMU (`tcg_gen_req_mo`) |
| **TSO (free)** | Hardware TSO mode bit | Rosetta 2 on Apple Silicon; FEX has `SetHardwareTSOSupport()` |
| **Unaligned atomics** | FEAT_LSE2 where available; otherwise SIGBUS handler that decodes + emulates + **backpatches** | FEX `ArchHelpers/Arm64.cpp` (the reference implementation) |
| **SMC** | mprotect the code page, invalidate on write fault; CRC re-check for known code+data pages; physical-page granularity + lockless page table for SMP | box64 (`protectDB` + `checkCRC`), FEX (`SMCChecks=mtrack`), QEMU (`tb-maint.c`) |
| **Block linking** | Patch a direct branch/trampoline between blocks; hash-table lookup cache for indirect | all of them |
| **Returns** | **Exploit the host return-address predictor** via a shadow stack / callret pairing | Instrew (callret), box64 (`callret_t`), FEX (`REG_CALLRET_SP`) |
| **256-bit SIMD** | Emulate YMM as 2×128-bit NEON (works on baseline ARMv8; don't require SVE) | FEX `AVX_128.cpp`, box64 `ymm_get_reg` |
| **x87** | Provide a reduced-precision 64-bit fast path alongside a correct 80-bit path | FEX (`X87.cpp` / `X87F64.cpp`), box64 (64-bit only) |
| **Guest memory** | Userspace: 1:1 VA. System: softmmu TLB (~10 host insns/access) or shadow paging | FEX/box64 vs QEMU |

---

## 14. Notes on measurement provenance

Numbers in this document come from: (a) direct source reading of current `main`/`master` (FEX `fd141ed6`
2026-08-06; QEMU `master` 2026-08-05); (b) `wc -l` on sparse checkouts (Appendix A); (c) the projects' own
release notes; (d) peer-reviewed papers (VEE'21 for Instrew/HQEMU/QEMU SPEC numbers; arXiv:1905.06825 for
softmmu instruction counts); (e) box86.org's cross-emulator benchmark (2022, updated 2025) — the only
apples-to-apples FEX/box64/QEMU/Rosetta comparison that exists publicly, and dated.

**Known gaps:** no public SPEC-grade x86-64-on-ARM64 comparison of FEX vs box64 vs QEMU on modern silicon
(Apple M-series, Cortex-X, Neoverse V2). If natemu needs a defensible baseline, we should run one.

---

## Appendix A — how the LoC numbers were produced

```
git clone --depth 1 --filter=blob:none --no-checkout <repo>
git sparse-checkout init --cone && git sparse-checkout set <paths> && git checkout
find <dir> -type f \( -name '*.c' -o -name '*.cpp' -o -name '*.h' -o -name '*.inc' \) | xargs wc -l
```

Raw line counts including comments and blank lines; treat as relative magnitudes, not SLOC.
