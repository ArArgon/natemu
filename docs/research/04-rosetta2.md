# 04 — Rosetta 2 Internals & Rosetta-for-Linux

**Question this answers:** what does Apple actually do to get x86-64 → arm64 translation to ~75% of
native, and which of those techniques are available to a system-level emulator that is *not* Apple?

**Confidence taxonomy used throughout:**

| Tag | Meaning |
|---|---|
| `VERIFIED` | Apple documentation, merged upstream kernel code, an ARM spec, **or measured/observed first-hand on this machine** (M2 Max, macOS 26.5.2 build 25F84, 2026-08-08) |
| `RE-CLAIM` | Reverse-engineering result from a credible named researcher (Dougall Johnson, Koh M. Nakagawa/FFRI, Asahi, FEX devs), or my own static analysis of Apple's shipped binaries. Plausible, not authoritative. |
| `SPECULATION` | Inference. Flagged explicitly. Do not build on without testing. |

**Host feature baseline for everything measured here** (`VERIFIED`, `sysctl`, M2 Max / macOS 26.5.2):

```
hw.optional.arm.FEAT_FlagM   = 1     CFINV, RMIF, SETF8/16
hw.optional.arm.FEAT_FlagM2  = 1     AXFLAG, XAFLAG
hw.optional.arm.FEAT_CRC32   = 1     CRC32CX  (used for indirect-branch hashing, §2.6)
hw.optional.arm.FEAT_LSE     = 1     LSE atomics (x86 LOCK prefix maps here)
hw.optional.arm.FEAT_LSE2    = 1     16-byte single-copy atomicity, unaligned atomics
hw.optional.arm.FEAT_AFP     = 0     <-- NO architectural AFP on M2; see §2.3
hw.optional.arm.FEAT_FRINTTS = 1
```

The `FEAT_AFP = 0` line is important: it is why this machine exercises Rosetta's **IMPDEF**
floating-point-mode path rather than the architectural one, which is what let me decode it (§2.3).

First-hand work for this dossier: static inspection (`otool -tvV`, `strings`, `file`) of
`/usr/libexec/rosetta/*`, `/Library/Apple/usr/libexec/oah/libRosettaRuntime`, and
`/Library/Apple/usr/libexec/oah/RosettaLinux/{rosetta,rosettad}`; plus purpose-built x86-64
microbenchmarks and CPUID probes run under Rosetta on this Mac. Raw notes inline.

---

## 0. TL;DR for natemu

1. Rosetta 2's speed is **~60% architecture, ~40% secret silicon**. The architecture (AOT whole-image
   translation, 1:1 register pinning, flag liveness analysis, hardware return-address stack pairing)
   is fully transferable. The silicon (TSO mode, PF/AF in NZCV, AFP-style FP mode, cheap W^X toggle)
   is *partly* transferable — TSO and AFP are reachable, the flag bits are not, from a normal process.
2. The realistic ceiling for a **user-mode** DBT with all of Apple's tricks is **~70–80% of native on
   integer**, **~70% on SSE**, and a **cliff to ~5–10%** on x87 and anything that forces
   interpretation. Rosetta hits that ceiling; nothing suggests it can be beaten meaningfully without
   a heavier optimizing IR (which Apple deliberately declined — see §3.6).
3. Rosetta 2 is **strictly ring-3**. It has no MMU emulation, no privileged ISA, no interrupts, no
   I/O. A system-level emulator gets *none* of Rosetta's structural simplifications for free; the
   parts you can steal are the code-generation and code-cache techniques, not the overall design.
4. **The single most important transferable data point** is §5: on Linux, Apple got TSO by
   **upstream-style `prctl(PR_SET_MEM_MODEL, PR_SET_MEM_MODEL_TSO)` in the guest kernel** — i.e. even
   *Apple* could not get TSO without a kernel change. There is no magic hypercall.

---

## 1. Architecture

### 1.1 The component inventory (`VERIFIED` — observed on this machine)

```
/usr/libexec/rosetta/
  oahd              arm64e   the ahead-of-time daemon (LaunchDaemon, com.apple.oahd)
  oahd-helper       arm64    one-shot translation worker (does the actual AOT compile)
  oahd-root-helper  arm64e   privileged helper
  runtime           arm64    the in-process runtime, mapped into every translated process
  translate_tool    arm64e   CLI translate front-end
  debugserver       arm64e   debug stub for translated processes
/Library/Apple/usr/libexec/oah/
  libRosettaRuntime          the *translator* proper (x86 decoder + IR + arm64 backend)
  runtime -> /usr/libexec/rosetta/runtime
  translate_tool -> ...
  debugserver -> ...
  RosettaLinux/
    rosetta          ELF aarch64, static, stripped, 1.73 MB — the in-guest Linux translator
    rosettad         ELF aarch64, static, stripped,  384 KB — the in-guest AOT cache daemon
```

Note the internal codename: env vars are accepted under **both** `ROSETTA_` and **`CAMBRIA_`**
prefixes (both prefix strings are present in the binaries; `CAMBRIA_ADVERTISE_AVX=1` works — tested).
"Cambria" is Apple's internal name for the project. `oah` (the daemon prefix and `/var/db/oah`) is
believed to stand for *"On-demand Ahead-of-time Handler"* / a Rosetta-1-era holdover — `SPECULATION`.

### 1.2 The two translators are different programs (`RE-CLAIM`, high confidence)

`libRosettaRuntime` and `runtime` have **disjoint source-file sets** (extracted from
`__cstring` assertion strings — this is Apple's own file list, so it is close to authoritative):

`libRosettaRuntime` (**the compiler**):
```
Translator.cpp  Translator.h  IrBuilder_x86.h  BuilderBase.h  BasicBlock.cpp
Decoder.cpp  Opcode.cpp  Operand.cpp  Register.cpp  BranchTargetFinder.cpp
FlagAnalysis.cpp  TranslatorFlags.cpp  TranslatorFlagsSaveRestore.cpp
MulDivAnalysis.cpp  AdditionMultiplication.cpp  Division.cpp
TranslatorArithmetic.cpp  TranslatorShifts.cpp  TranslatorBitTest.cpp
TranslatorMulDiv.cpp  TranslatorStringOps.cpp  TranslatorAes.cpp
TranslatorSseConversions.cpp  TranslatorSseFpComparisons.cpp
TranslatorSseMovesShuffles.cpp  TranslatorSseShifts.cpp  TranslatorSseStringOps.cpp
TranslatorX87.cpp  x87.cpp  Fprem.cpp  Fsqrt.cpp  FstIeee.cpp  Rounding.cpp
InternalFloat.cpp  GuardBits.cpp  Segmentation.cpp  StateRecovery.cpp
AssemblerBuffer.h  AssemblerHelpers.cpp  Fixup.cpp  Repatch.cpp  Init.cpp
```

`runtime` (**the in-process supervisor**):
```
TranslationCache.cpp  TranslationCacheAot.cpp  TranslationCacheJit.cpp  TranslationCacheAbi.cpp
IndirectBranchHashMap.cpp  DeltaCodedOffsetMap.cpp  TwoLevelOffsetMap.cpp  OffsetPairArray.cpp
RedBlackTree.h  BestFitAllocator.h  ExecutableHeap.cpp  VmProtections.cpp  ThreadSelfRestrict.cpp
ArmInterpreter.cpp  ArmInterpreterRuntime.cpp
Image.cpp  ImageInfo.cpp  Library.cpp  RebaseRuntimeMachO.cpp  CodeSignature.cpp
Thread.cpp  ThreadContext*.cpp (7 variants)  ThreadStateArm.h  ThreadWaiter.cpp
ExceptionHandlers.cpp  ExceptionServer.cpp  MachExceptions.cpp  MachMsgTrapArgs.cpp
DebugServer.cpp  CodeFragmentMetadata.cpp  Decoder.cpp
```

Architectural read (`RE-CLAIM`): a **basic-block-scoped, IR-based, single-pass compiler**
(`IrBuilder_x86.h` → `BasicBlock` → `AssemblerBuffer`) with a small number of **targeted dataflow
analyses** — flag liveness (`FlagAnalysis.cpp`), mul/div result-width analysis
(`MulDivAnalysis.cpp`) — and a `StateRecovery.cpp` pass that lets the runtime reconstruct precise
x86 state at any arm64 PC. It is *not* a general optimizing compiler (no SSA/regalloc/scheduling
files); it is a very good macro-expander plus a handful of peepholes. Dougall Johnson independently
concludes the same thing and says the remaining headroom would cost exactly the complexity Apple
avoided.

The presence of `ArmInterpreter.cpp` in `runtime` is notable (`RE-CLAIM`): Rosetta ships an **arm64
interpreter**, almost certainly for single-stepping/state-recovery on exceptions and for the
debugserver, not for the hot path.

### 1.3 AOT path — how a translation is produced and cached

`VERIFIED` (Apple Platform Security guide) + `RE-CLAIM` (FFRI Project Champollion part 1):

1. `execve` of a Mach-O x86_64 image; kernel notices the mismatch and starts the Rosetta
   `runtime` as the image's "translation stub".
2. `runtime` talks to **`oahd`** over Mach IPC (`com.apple.oahd`, ports `com.apple.oahd.requests` /
   `com.apple.oahd.receive` — strings confirmed on this machine) using the undocumented
   `fileport_makeport` / `fileport_makefd` syscalls to pass file descriptors, so the runtime never
   gets a path into the cache — only a read-only fd to one artifact.
3. `oahd` computes **SHA-256 of the file contents** and **SHA-256 of the execution path** and looks
   up `/var/db/oah/<content-hash>/<path-hash>/<name>.aot`.
4. On miss it forks **`oahd-helper`** with the two fds. The helper writes `<name>.aot.in_progress`
   and `rename()`s it atomically, so concurrent launches never see a torn artifact
   (strings `AotRequest.cpp`, `!gRenamedAotPath.empty()`, `in-progress translation did not result in
   a cached/negative aot` confirm this on this machine).
5. `oahd` also maintains **negative cache entries** (`AotCacheKind::NeedsTranslation` /
   negative-aot strings), **`purge_old_aot`**, **`retranslate_aot_directory`** and an
   **`Oah.version`** stamp — so the whole cache is invalidated and rebuilt when Rosetta itself is
   updated. That is a *versioned, content-addressed, machine-global* code cache.
6. `VERIFIED` (Apple security guide): the AOT artifact is **signed with a device-specific key held by
   the Secure Enclave** before it may be executed. The cache is therefore not portable between
   machines and cannot be poisoned offline. Note the security-guide's other admission:
   *"translated x86_64 code is permitted to execute through Rosetta with no signature information at
   all"* — i.e. the *source* x86 binary need not be signed; only the *artifact* is.

**AOT file format** (`RE-CLAIM`, FFRI): the `.aot` is an ordinary **arm64 Mach-O**, with
`__TEXT` = translated code, `__LINKEDIT` = metadata, a `__stubs_sh` section acting as a
zero-initialized lazy-binding cache, and a private load command **`LC_AOT_METADATA` (0xcacaca01)**
carrying the x86 text RVA and the source-image path. `oahd` string
`integer overflow in aot metadata load command fragment list` on this machine corroborates the
fragment-list structure. **Constants and data are *not* copied** — the translated code reads them
out of the original x86 image, which is mapped alongside.

**Translation unit granularity** (`RE-CLAIM`): the *unit of caching* is the whole image
("code fragment" per image, plus per-`__TEXT`-section fragments); the *unit of compilation* is the
basic block; the *unit of correctness* is the **individual x86 instruction** — Dougall Johnson:
Rosetta keeps "canonical state between each instruction", which is what makes precise exceptions,
signal delivery and debugging tractable. This is the central design trade: give up cross-instruction
optimization, buy precise state everywhere.

### 1.4 The AOT shared cache

`RE-CLAIM` (FFRI part 2): there is a **pre-translated system-wide cache**
`aot_shared_cache`, magic `0x6568636143746F41` = `"AotCache"`, ~1924 entries in the Big Sur era,
three mapping descriptors — the AOT analogue of `dyld_shared_cache_x86_64`.

**`VERIFIED` on this machine, and it has grown a lot.** `oahd` still hard-codes the legacy path
`/System/Library/dyld/dyld_shared_cache_x86_64`, but on macOS 26.5 the caches live in the Cryptex:

```
/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld/
  aot_shared_cache.0 .. aot_shared_cache.6      7 files, 6,124,500,029 bytes apparent
                                                (2.2 GB actually on disk — they are sparse)
  dyld_shared_cache_arm64e.*                    the native cache, for scale
```

So the pre-translated x86 system-library corpus went **~2.4 GB (macOS 11) → 6.12 GB apparent /
2.2 GB resident (macOS 26.5)**. The *concept* is what matters: **the entire x86 system library set is
translated once, at OS install time, signed, and shared read-only across every translated process.**

This matters more than it looks. Most of the instruction footprint of a typical x86 macOS app is
libSystem/CoreFoundation/AppKit, not app code. Pre-translating and *sharing* that removes it from
both the translation-time budget and the per-process memory budget.

### 1.5 When JIT is used instead

`VERIFIED` (Apple security guide) + `RE-CLAIM`: three cases.

* **JIT-generated guest code** — any x86 page the guest writes and then executes (JS engines, JVM,
  .NET, Wine's thunks, Mono). Handled by `TranslationCacheJit.cpp` in-process. Apple:
  *"The translation stub then translates x86_64 pages during the image's execution"*, with the
  kernel verifying page hashes against the source binary's signature where one exists.
* **Cache miss / unsigned / disallowed AOT** — `oahd` can return "negative", forcing JIT.
* **Indirect-branch targets not statically discovered** — `translate_indirect_branch` consults the
  translation red-black tree first (`find_translation_in_tree_x86`), and only JITs on miss
  (`RE-CLAIM`, FFRI).

The AOT and JIT paths use the **same translator** (`rosettad` on Linux carries both
`TranslationMode::Aot` assertions and a JIT mode, and asserts
`Can't translate dyld stubs in JIT mode` / `jit translations should never be segmented` — observed).
So JIT is not a degraded translator, it is the same code generator with a different driver and no
persistence.

### 1.6 Code-cache W^X: the cheapest trick in the building (`RE-CLAIM`, my own disassembly)

`runtime` contains `ThreadSelfRestrict.cpp`, and its code is:

```
        mov     x9, #0x7fffffe00000 ; movk x9, #0xfe0     // fixed Rosetta shared page
        ldr     x8, [x9, #0x10]                            // permission-register value to install
        ldrb    w9, [x9]                                   // CPU-variant selector (0..3)
        ...
        dsb     ish
        msr     S3_4_C15_C2_7, x8      // variant A
        isb
        ret
        dsb     ish
        msr     S3_6_C15_C1_5, x8      // variant B  (== APRR_EL0 on M1-era parts)
        isb
        ret
```

Both are **EL0-accessible IMPDEF system registers**. `S3_6_C15_C1_5` is the register publicly
documented by the iOS RE community as **APRR_EL0**, the per-thread page-permission remap register
behind `pthread_jit_write_protect_np()`; `S3_4_C15_C2_7` is the newer-silicon equivalent (SPRR-class).
The value is fetched from a fixed Rosetta shared page rather than being computed, and the CPU-variant
byte selects which register to poke.

**What this buys:** flipping the translation buffer between writable and executable costs
`dsb; msr; isb` — a handful of cycles, **per thread**, **no syscall**, **no TLB shootdown**, **no
`mprotect`**. A JIT that does W^X honestly with `mprotect` pays a syscall and an IPI-based shootdown
per flip; Rosetta pays ~nothing. `TRANSFERABLE — partially`: on macOS, `pthread_jit_write_protect_np`
gives you exactly this (with the `MAP_JIT` entitlement); on Linux/Asahi there is no equivalent, and
on a hypervisor-hosted design you would instead use two aliased mappings (RW view + RX view of the
same physical pages), which costs an extra TLB entry but no syscalls.

### 1.7 Register allocation: static pinning, not allocation (`RE-CLAIM`, FFRI + corroborated here)

FFRI's Champollion documents a fixed, total mapping:

```
RAX→x0  RCX→x1  RDX→x2  RBX→x3
RSP→x4  RBP→x5  RSI→x6  RDI→x7
R8..R15 → x8..x15
XMM0..XMM15 → q0..q15
```

There is **no register allocator**. All 16 GPRs and all 16 XMMs live permanently in host registers.
AArch64's 31 GPRs + 32 vector registers make this possible with room to spare; Dougall Johnson:
*"64-bit x86 has half as many registers as 64-bit ARM, so this isn't a problem for Rosetta 2."*
The leftovers are the emulator's: `x16/x17` scratch, **`x18`** as the thread-context pointer, and —
confirmed in my own disassembly of `runtime`'s entry thunk — the real **ARM `SP` is repurposed** as a
second pointer into the runtime thread block (`mov sp, x18` right before entering translated code),
which is legal precisely because guest RSP has been moved into `x4`.

**I confirmed this mapping independently, two ways** (`VERIFIED` — the Linux binary retains a handful
of symbols, which is how):

*(1) The runtime entry trampolines* (`RosettaLinux/rosetta`, `<runtime_entry>` and ~40 near-identical
copies) spill exactly x0–x15 and nothing else:

```
runtime_entry:
        stp     x0,  x1,  [x18, #0x10]     ; RAX, RCX
        stp     x2,  x3,  [x18, #0x20]     ; RDX, RBX
        stp     x4,  x5,  [x18, #0x30]     ; RSP, RBP
        stp     x6,  x7,  [x18, #0x40]     ; RSI, RDI
        stp     x8,  x9,  [x18, #0x50]     ; R8,  R9
        stp     x10, x11, [x18, #0x60]     ; R10, R11
        stp     x12, x13, [x18, #0x70]     ; R12, R13
        stp     x14, x15, [x18, #0x80]     ; R14, R15
        mrs     x29, NZCV                  ; guest EFLAGS live in NZCV
        stp     x29, x17, [x18, #0x90]
        str     x30, [x18, #0x8]
        orr     x18, x18, #0x8000000000000000   ; tag "we are inside the runtime"
```

*(2) `<runtime_syscall>`* shuffles guest System-V syscall arguments into the Linux AArch64 syscall
ABI, and the shuffle is only correct under exactly Champollion's mapping:

```
runtime_syscall:
        mov     x4, x8          ; R8  -> arg5      (guest R8  is x8)
        mov     x8, x0          ; RAX -> syscall#  (guest RAX is x0)
        mov     x0, x7          ; RDI -> arg1      (guest RDI is x7)
        mov     x1, x6          ; RSI -> arg2      (guest RSI is x6)
        mov     x2, x2          ; RDX -> arg3      (guest RDX is x2)
        mov     x3, x10         ; R10 -> arg4      (guest R10 is x10)
        mov     x5, x9          ; R9  -> arg6      (guest R9  is x9)
        cmp     x8, #0x1b1      ; bound-check against 433 syscalls
        ...     ldpsw / br      ; 32-bit-offset jump table of per-syscall handlers
        svc     #0              ; common case: straight passthrough
```

Corroborating offsets from the macOS FP save/restore routine I disassembled: guest XMM0–XMM15 occupy
`+0x00 .. +0xFF` of the FP save area as eight `stp q,q`; guest MXCSR-derived FPSR at `+0x200`,
FPCR at `+0x204`.

**The complete translated-code register ABI** (`VERIFIED` by disassembly, Linux build):

| host reg | role |
|---|---|
| `x0`–`x15` | guest `RAX RCX RDX RBX RSP RBP RSI RDI R8..R15` |
| `x16`, `x17` | translator scratch |
| **`x18`** | **thread-context pointer**; bit 63 set while inside the runtime |
| `x19` | indirect-branch hash-table base |
| `x20` | indirect-branch hash-table mask |
| `x21` | reloaded from `[x18+0x1a8]` — image/code base |
| `x22` | current guest RIP / branch target |
| `x23`–`x29` | runtime scratch |
| `x30` | link register (see §2.5 — real `BL`/`RET`) |
| `SP` | repurposed as a second runtime pointer (guest RSP lives in `x4`) |
| **`NZCV`** | **guest EFLAGS** (CF/ZF/SF/OF, + PF/AF in bits 26/27 on Apple silicon) |
| `q0`–`q15` | guest `XMM0`–`XMM15` |
| `FPCR`/`FPSR` | guest `MXCSR` projection (§2.3) |

**This is the single most transferable structural decision in Rosetta.** Zero spill traffic in the
common case is worth more than any peephole. It only works because the guest register file fits in
the host register file — true for x86-64→AArch64, **false** for AArch64→x86-64 and for
x86-64→RISC-V-without-enough-regs.

---

## 2. Micro-architectural tricks

### 2.1 TSO — `VERIFIED` that it exists, `RE-CLAIM` for the encoding

Apple Silicon implements a **runtime-switchable Total Store Ordering mode**. When enabled, ordinary
`LDR`/`STR` acquire x86-style ordering (no store→load reordering visible; stores become globally
ordered), so the translator can emit plain loads and stores instead of `LDAR`/`STLR` pairs or `DMB`s.

* `VERIFIED` (upstream-proposed Linux code, Apple's own developer docs): the control is an
  **`ACTLR_EL1` IMPDEF bit** (bit 1), settable only at EL1, therefore **per-thread state the kernel
  must context-switch**.
* `VERIFIED` (measurement, third-party): TSO mode costs ~**8.9% average** on *native* ARM code
  (Wrenger et al., "Analyzing the memory ordering models of the Apple M1", JSA 2024). So it is not
  free — it is just enormously cheaper than the alternative.
* **The alternative** is the thing to internalize: without TSO, a correct x86 DBT must make every
  guest load an acquire-load and every guest store a release-store (or worse, fence). FEX-Emu's own
  numbers put software TSO emulation at a **large multiplicative cost** — this is the single biggest
  reason non-Apple x86-on-ARM emulators lose.
* Third-party access on macOS: `saagarjha/TSOEnabler`, a kext that sets the `ACTLR_EL1` bit for
  arbitrary processes. Requires disabling SIP. `VERIFIED` that it exists; not a shipping option.

**Transferability:** if natemu runs guest code inside a **Hypervisor.framework VM**, natemu owns
`ACTLR_EL1` for its vCPUs and can set the TSO bit itself. That is a *significant* argument for the
hypervisor-hosted design over a plain user-mode process. If natemu runs as a plain macOS process, TSO
is unreachable without a kext. (Flagging as `SPECULATION` that HVF permits writing the IMPDEF
`ACTLR_EL1` bit from a guest EL1 — it *should*, since the guest owns EL1, but this must be tested on
the target machine before it is designed around.)

### 2.2 x86 flags — the "hidden hardware support" claim: **CONFIRMED, with nuance**

The claim circulating as "Apple has hidden x86 flag support in the M1" is **substantially true** but
is three separate things that get conflated. Separating them:

**(a) Architectural, documented, not Apple-specific — `VERIFIED` (ARM ARM).**
Rosetta relies on **FEAT_FlagM** (`CFINV`, `RMIF`, `SETF8`, `SETF16`) and **FEAT_FlagM2**
(`AXFLAG`, `XAFLAG`). These are in the ARM architecture, mandatory from Armv8.4/8.5, and shipped by
every Apple core since the M1 and by many non-Apple cores. They exist *because* of x86 emulation:
`CFINV` fixes x86's inverted borrow convention on subtract in one instruction; `RMIF` inserts an
arbitrary bit of a register into NZCV (used to restore CF/OF from a saved flag word); `SETF8/SETF16`
recompute NZV for a narrow result; and `AXFLAG`/`XAFLAG` convert ARM FP condition flags to and from
what the ARM ARM coyly calls an "external format" that Dougall Johnson notes "by some strange
coincidence… is x86". **All of this is available to any AArch64 emulator.** Check `FEAT_FlagM`/
`FEAT_FlagM2` and use them.

**(b) Apple-specific, undocumented, real — `RE-CLAIM` (Dougall Johnson, widely reproduced).**
x86 defines six arithmetic flags; ARM's NZCV holds four. CF/ZF/SF/OF map cleanly. **PF (parity) and
AF (adjust)** — both 8080 fossils — do not. Dougall Johnson's finding: the M1 has an **undocumented
mode in which `ADDS`/`SUBS`/`CMP` additionally compute PF and AF and deposit them in NZCV bits 26 and
27**, i.e. in the otherwise-unused low bits of PSTATE/NZCV. Cost of correct PF/AF emulation:
**zero**. Cost in software: a parity reduction (several instructions) and a nibble-carry extraction
per flag-setting instruction — on *every* `add`/`sub`/`cmp`, which is a large fraction of x86 code.
This is genuinely Apple-only, genuinely undocumented, and **not transferable**.
Corroborating detail (`RE-CLAIM`): Dougall notes that Rosetta 2 **for Linux** appears to emulate
PF/AF in software — consistent with the bits being unavailable to a guest, and a useful hint about
what natemu will have to do.

**(c) The mitigation Apple actually leans on — `RE-CLAIM`, and the transferable part.**
`FlagAnalysis.cpp` + `TranslatorFlags.cpp` + `TranslatorFlagsSaveRestore.cpp` in `libRosettaRuntime`
implement the **unused-flags optimisation**: if, on every path out of a flag-setting instruction, the
flags are overwritten before being read, **do not compute them**. This is a standard DBT technique
and it is what you use instead of hardware assist. On real x86 code it kills the large majority of
flag computations, because compilers emit `cmp`/`test` immediately before the consuming branch and
almost never read flags across a call. Combined with FlagM (a), a non-Apple emulator can get most of
the way; the residue is PF/AF for the small set of instructions whose flags genuinely escape.

**Verdict on the "hidden flag support" claim: not debunked — confirmed** as (b), but the framing is
misleading, because (a) is architectural and available to everyone and (c) is where most of the win
actually comes from.

### 2.3 Floating point and SIMD

**SSE→NEON — `RE-CLAIM` (source-file evidence).** Five dedicated translator files:
`TranslatorSseConversions`, `TranslatorSseFpComparisons`, `TranslatorSseMovesShuffles`,
`TranslatorSseShifts`, `TranslatorSseStringOps`, plus `TranslatorAes.cpp`. SSE/SSE2/SSE3/SSSE3/
SSE4.1/SSE4.2 map to NEON largely 1:1 for the arithmetic core; the expensive residue is
**shuffles** (x86's `pshufb`/`palignr`/`shufps` semantics vs `TBL`/`EXT`), **FP compare predicates**
(x86 has 8+ unordered/signalling variants, NEON has fewer), and the **SSE4.2 string instructions**
(`PCMPESTRI` &c.) which are essentially microcoded and get their own file. `AESENC`/`AESDEC` map onto
ARMv8 crypto (`AESE`/`AESMC`) but *not* 1:1 — the round structure differs by one step, which is why
there is a whole `TranslatorAes.cpp`.

**FP mode / denormals / MXCSR — `RE-CLAIM`, my own disassembly.** In `runtime`'s entry-to-translated-
code thunk:

```
        msr     FPCR, xzr                       // baseline: RNE, no FZ
        ldrb    w17, [x18, #0x170]
        tbz     w17, #1, skip
        mov     w17, #6                         // FPCR bits 1|2 == AH | NEP  (FEAT_AFP)
        msr     FPCR, x17
skip:   ...
```

and in the FP state-save routine:

```
        mrs     x1, FPCR
        ldrb    w2, [x18, #0x170]
        tbnz    w2, #1, done                    // if the AFP path is in use, FPCR is the whole story
        mrs     x2, S3_6_C15_C2_5               // else: IMPDEF register holds the extra mode bits
        and     x2, x2, #3
        orr     x1, x1, x2                      // fold them into the saved FPCR image
done:   str     w1, [x0, #0x204]
```

I then found the **decisive confirmation** in the *Linux* build (`RosettaLinux/rosetta`, disassembled
with `llvm-objdump`), which is unstripped enough to show the feature probe verbatim:

```
        ; walk the ELF auxv looking for AT_HWCAP (16)
        cmp     x9, #0x10
        ...
        ldrb    w8, [x8, #0x1]
        tbz     w8, #0x3, no_afp        ; HWCAP bit 11 == HWCAP_CPUID
                                        ; (must be set, or MRS of an ID reg from EL0 SIGILLs)
        mrs     x8, ID_AA64MMFR1_EL1
        tst     x8, #0xf00000000000     ; bits [47:44] == the FEAT_AFP field
        b.eq    no_afp
        mov     w9, #2                  ; fp_mode = 2  (architectural FEAT_AFP)
        strb    w9, [x8, #0xcb8]
        b       done
no_afp: mov     w9, #1                  ; fp_mode = 1  (no AFP)
        strb    w9, [x8, #0xcb8]
```

and the guest-`MXCSR` assembly routine, which decodes what the IMPDEF register actually holds:

```
        ldrb    w27, [x18, #0x138]      ; fp_mode: 0 = IMPDEF, 1 = neither, 2 = FEAT_AFP
        tbnz    w27, #1, mode_afp
        cbz     w27, mode_impdef
        lsr     w28, w24, #24           ; mode 1: FPCR.FZ (bit 24) -> MXCSR.FZ only
        bfi     w23, w28, #15, #1
        ret
mode_impdef:
        mrs     x25, S3_6_C15_C2_5
        bfi     w23, w25, #6,  #1       ; IMPDEF bit 0  ->  MXCSR bit 6  == DAZ
        lsr     w28, w25, #1
        bfi     w23, w28, #15, #1       ; IMPDEF bit 1  ->  MXCSR bit 15 == FZ
        ret
mode_afp:
        bfi     w23, w24, #6,  #1       ; FPCR.FIZ (bit 0)  ->  MXCSR.DAZ
        ubfx    w28, w24, #24, #1
        bfi     w23, w28, #15, #1       ; FPCR.FZ  (bit 24) ->  MXCSR.FZ
        ret
```

**Decoded (`RE-CLAIM`, high confidence — the bit positions are unambiguous):**

* Rosetta needs **x86 FP corner-case semantics** — x86's NaN propagation and default-NaN sign, its
  "tininess before rounding" convention, its DAZ (denormal inputs → zero) and FZ (flush denormal
  results → zero) — which ARM's IEEE-default behaviour does *not* match.
* Armv8.7 **FEAT_AFP** provides this architecturally: `FPCR.AH` (bit 1, alternate NaN/denormal
  handling), `FPCR.NEP` (bit 2, scalar ops preserve the upper vector bits — matching x86 scalar SSE),
  and `FPCR.FIZ` (bit 0, flush inputs to zero == x86 DAZ). Rosetta's entry thunk sets
  **`FPCR = 6` = `AH | NEP`** statically, and drives `FIZ`/`FZ` dynamically from guest `MXCSR`.
* On Apple parts **predating architectural AFP** (`ID_AA64MMFR1_EL1[47:44] == 0`, which includes this
  M2 Max), the DAZ and FZ controls live in the **EL0-accessible IMPDEF register `S3_6_C15_C2_5`**:
  **bit 0 == x86 DAZ, bit 1 == x86 FZ**. Rosetta reads it to build guest MXCSR and folds it into the
  saved-FPCR word during context save.
* `ldmxcsr`/`stmxcsr`/`mxcsr_to_fpcr_fpsr`/`read_mxcsr` symbols confirm guest **MXCSR** (rounding
  mode, FTZ, DAZ, exception masks) is projected onto FPCR/FPSR — **not** emulated in software.

`TRANSFERABLE — yes, and cheaply.` **FEAT_AFP is architectural** from Armv8.7 and is advertised in
`ID_AA64MMFR1_EL1[47:44]`; probe it (behind `HWCAP_CPUID` on Linux) and set `FPCR.{AH,NEP}` plus
`FIZ`/`FZ` from guest MXCSR. On pre-8.7 Apple silicon the IMPDEF register is the fallback, and —
critically — **the fact that Apple's own Linux-guest binary reads `S3_6_C15_C2_5` with a plain `MRS`
from EL0 proves it is readable from an ordinary userspace process inside a Virtualization.framework
guest**, i.e. it is not trapped by the hypervisor. That resolves what would otherwise be an open
question for natemu.

**x87 — the disaster zone. `VERIFIED` by measurement here.** x87 is **not** translated to native
code in any meaningful sense; it is dispatched to a library of C++ helper routines. The symbol list
in both `libRosettaRuntime` and `runtime` is unambiguous — dozens of exported
`rosetta::runtime::library::x87_*` functions operating on an explicit `X87State` struct and an
`X87Float80` type:

```
x87_fld_fp80(X87State*, X87Float80)   x87_fst_fp80(const X87State*)
x87_fadd_ST / x87_fmul_ST / x87_fdiv_ST / x87_fsub_ST / x87_fdivr_ST ...
x87_fprem1  x87_fpatan  x87_fyl2xp1  x87_fsincos  x87_fscale  x87_fxtract  x87_frndint
x87_fist_i16/i32/i64  x87_fucomi  x87_fincstp/fdecstp ...
```
plus `Fprem.cpp`, `Fsqrt.cpp`, `FstIeee.cpp`, `InternalFloat.cpp`, `GuardBits.cpp` — a full
**software 80-bit extended-precision implementation with explicit guard bits**. Every x87
instruction is a call into this. AArch64 has no 80-bit float, so this is the only correct option, but
the cost is brutal.

**Measured on this machine** (M2 Max, macOS 26.5.2; same C source, `clang -O1`; `long double`
recurrence, x86-64 build vs arm64 build):

| workload | native arm64 | x86-64 via Rosetta | ratio |
|---|---|---|---|
| integer LCG + compare, 60M iters | 0.059 s | 0.076 s | **78% of native** |
| `double` FMA recurrence, 60M iters | 0.217 s | 0.286 s | **76% of native** |
| `long double` recurrence, 6M iters | 0.022 s | 0.339 s | **~6% of native** |

*(Caveat, stated honestly: on Apple arm64 `long double == double`, so the native column is doing
64-bit work while the Rosetta column is doing genuine 80-bit work. The ratio is therefore the
practical penalty a program pays for using `long double`, not a like-for-like ISA comparison. The
order of magnitude — 10–15× — is the point, and it matches the community consensus.)*

The community response confirms the severity: **`Lifeisawful/rosettax87`** and **`athei/x87sidecar`**
both hook Rosetta's `translate_insn` to substitute a real AArch64 JIT for the x87 handlers, reporting
**~4–5× on x87-heavy code** and describing stock Rosetta x87 as running "about 100× slower than it
ought to" for old Wine games. `x87sidecar`'s README also documents a structural fact worth stealing:
Rosetta **validates that every executing arm64 PC maps back to an x86 PC in its own tables**, and
aborts with `rosetta error: no code fragment associated with the given arm pc` otherwise — a strong
integrity invariant that also makes code injection into a Rosetta process essentially impossible
(hence their out-of-process "sidecar" design).

**AVX — `VERIFIED` by direct experiment on this machine, and the answer is surprising.**

CPUID under Rosetta on macOS 26.5.2 by default:
```
vendor=GenuineIntel  brand="VirtualApple @ 2.50GHz"  max leaf=13
SSE3=1 SSSE3=1 SSE4.1=1 SSE4.2=1 AES=1 POPCNT=1 CX16=1
OSXSAVE=0 AVX=0 F16C=0 FMA=0 MOVBE=0    leaf7.ebx=0 (BMI1=0 AVX2=0 BMI2=0 AVX512F=0 SHA=0)
```
…yet a hand-written `vmovups/vaddps ymm` sequence **executes correctly**, and an `-mavx2` intrinsics
build **runs fine**. And there is an env var:

```
$ ROSETTA_ADVERTISE_AVX=1 ./cpuid_probe
OSXSAVE=1 AVX=1 F16C=1 FMA=1 MOVBE=1
leaf7.ebx=0x00000128  →  BMI1=1 AVX2=1 BMI2=1 ; AVX512F=0 SHA=0 ERMS=0 ADX=0
```
(`CAMBRIA_ADVERTISE_AVX=1` works identically. Values 1/2/3 all enable; 0 disables.)

So: **AVX/AVX2/FMA/F16C/BMI1/BMI2/MOVBE are implemented and executable, but hidden from CPUID by
default.** This is Apple deliberately keeping the *advertised* feature set at the level that shipping
Intel Macs had in common, so that runtime-dispatching code (`__builtin_cpu_supports`, IPP/MKL/
OpenSSL/ffmpeg dispatch tables) picks the SSE path — because the AVX path is *slower*. Confirmed by
measurement:

| 245M-element FMA loop | time | vs native |
|---|---|---|
| native arm64 (NEON) | 0.020 s | 1.00× |
| x86-64 `-msse4.2` via Rosetta | 0.028 s | 0.71× |
| x86-64 `-mavx2 -mfma` via Rosetta | 0.040 s | **0.50×** |

256-bit YMM operations are split into two 128-bit NEON operations plus register-halving overhead, so
AVX2 code is measurably **slower under Rosetta than the SSE build of the same source.** Apple's
"support" is a correctness feature for binaries that use AVX unconditionally, not a performance
feature. The translator confirms 128-bit-only internals: `translate_avx_low`,
`translate_avx_low called on non-AVX register`, `reg.is_avx() && reg.index < 16`,
`vextractf128`/`vextracti128`, and `Unsupported VAES instruction`.

**AVX-512 — `VERIFIED` absent.** An EVEX-encoded `vmovaps zmm0,zmm1` (`62 f1 7c 48 28 c1`) raises
**SIGILL** (exit 132). AVX512F is 0 in CPUID even with `ADVERTISE_AVX=1`.

For reference, the Linux build's `/proc/cpuinfo` flag string is hard-coded in `RosettaLinux/rosetta`
and enumerates exactly the emulated ISA (`VERIFIED`, string extracted verbatim):
```
fpu tsc de cx8 apic sep cmov pat pse36 clflush mmx fxsr sse sse2 syscall nx lm rep_good nopl
pni cpuid pclmulqdq ssse3 cx16 sse4_1 popcnt sse4_2 aes lahf_lm movbe fma avx f16c rdrand
bmi1 avx2 bmi2
```
Note what is **absent**: AVX-512 (any), SHA, ADX, ERMS/FSRM, RDSEED, TSX, CLWB, and all of
`x86-64-v4`. Present set ≈ **`x86-64-v3` minus a few**.

### 2.4 Unaligned access — `VERIFIED` (architecture) + measured

AArch64 permits unaligned ordinary loads/stores to Normal memory, so x86's unaligned semantics need
no work at all for scalar and NEON accesses. The only residue is **unaligned atomics** (x86 permits a
`lock` op straddling a cache line; AArch64 does not) — Apple's FEAT_LSE2 (present on this machine,
`hw.optional.arm.FEAT_LSE2 = 1`) makes single-copy-atomic 16-byte accesses work within a 16-byte
granule; the truly-straddling case must be trapped and fixed up. `SPECULATION`: Rosetta likely traps
it; I found no direct evidence either way.

Measured: `atomic_fetch_add(seq_cst)` × 20M took **0.080 s natively and 0.080 s under Rosetta** —
*identical*. `lock xadd` compiles to a native LSE atomic (`ldaddal`) with no emulation overhead. This
is a nice result: **x86 atomics are one of the few things a good AArch64 DBT gets for free.**

### 2.5 Return-address prediction — `RE-CLAIM` (Dougall Johnson), and important

Naively, an x86 `CALL` becomes "push return address; branch", and `RET` becomes "pop; indirect
branch" — which **destroys the hardware return-address stack** and costs a full branch mispredict
(15–20 cycles) on every function return. Rosetta instead **rewrites `CALL`→`BL` and `RET`→`RET`**,
keeping the architectural stack pushes/pops as ordinary loads/stores *and* additionally maintaining
a **shadow stack of (expected x86 return address, corresponding translated arm64 target)** pairs,
validated on return. When the guest does something legal-but-weird (return-address rewriting,
setjmp/longjmp, exception unwinding), validation fails and the runtime falls back to a table lookup.

`TRANSFERABLE — and this is one of the highest-value items on the list.` Call/return is extremely
frequent; getting the host RAS to predict guest returns is worth several percent on its own.

### 2.6 Indirect branches — `RE-CLAIM`

`runtime` carries `IndirectBranchHashMap.cpp`, `DeltaCodedOffsetMap.cpp`, `TwoLevelOffsetMap.cpp`,
`OffsetPairArray.cpp` and `RedBlackTree.h`. Read: a **tiered x86-PC → arm64-PC lookup** — an inline
hash-map fast path in translated code, backed by delta-coded / two-level offset tables per image
(compact, cache-friendly), backed by a red-black tree of all loaded fragments for the general case,
backed by the JIT translator on a miss. The delta-coded offset map is the interesting bit: because
translation is monotone within an image, the x86→arm64 offset map compresses extremely well, which
keeps the whole thing in cache.

**And here is the fast path, verbatim** (`VERIFIED`, `<runtime_br_miss>+0x34` in the Linux build):

```
        ldr      x21, [x18, #0x1a8]
        crc32cx  w23, wzr, x22        ; hash the guest target with ONE hardware instruction
        and      w23, w23, w20        ; & table mask
        add      x24, x19, x23, lsl #4 ; 16-byte entries: { guest_pc, host_pc }
        ldp      x25, x26, [x24]
        sub      x27, x25, x22
        cbnz     x27, slow_path       ; tag mismatch -> probe / runtime
        br       x26                  ; hit: jump straight to translated code
```

**~6 instructions and one cache line for a fully general indirect branch.** The hash is `CRC32CX`,
which is `FEAT_CRC32` — mandatory from Armv8.1, so **available to any AArch64 emulator**. Entries are
16-byte `{guest_pc, host_pc}` pairs in an open-addressed, power-of-two table whose base and mask are
*pinned in `x19`/`x20`*, so the fast path needs no memory loads to find the table. This is one of the
cleanest, most directly stealable things in the entire system.

Measured indirect-dispatch cost here: a 60M-iteration 8-way `switch` ran 0.037 s native vs 0.046 s
under Rosetta (**80% of native**) — i.e. jump tables do not fall off a cliff.

### 2.7 The translated-code ↔ runtime ABI is a small fixed set of stubs (`VERIFIED`)

The Linux binary exposes exactly **13 named runtime entry points** — this is `TranslationCacheAbi.cpp`
made visible:

```
runtime_entry     runtime_savedreg   runtime_savdnzcv   runtime_br_miss
runtime_exit_br   runtime_exit_ret   runtime_exit_sys   runtime_exitcall
runtime_exitinst  runtime_exitljmp   runtime_exitsigr   runtime_sigtramp
runtime_syscall
```

Translated code never calls arbitrary runtime functions; it branches to one of a **small, fixed,
known-offset stub table**, each of which spills exactly the state that stub needs. `exitinst` is the
per-instruction escape (interpreted/complex instructions), `exit_br`/`exit_ret`/`exitljmp` are the
control-flow escapes, `exit_sys`/`syscall` the syscall path, `exitsigr`/`sigtramp` the signal path.
`TRANSFERABLE` — the design lesson is *minimise and fix the ABI surface*, because every stub is a
place where guest state must be materialised exactly.

**One cautionary find.** The blob at `<runtime_savdnzcv>+0x4c` is a **64-iteration shift-subtract
loop** — software **128 ÷ 64 division**, which AArch64 cannot do natively (x86 `DIV`/`IDIV` take a
128-bit dividend; `UDIV` is 64÷64). That is ~64 dependent iterations per wide divide. It is exactly
why `libRosettaRuntime` ships `MulDivAnalysis.cpp` and `Division.cpp`: the translator works hard to
*prove* the dividend fits in 64 bits so it can emit a single `UDIV` instead. **Budget for this** —
128-bit division and `MUL`-with-128-bit-result are the two integer instructions with no cheap
AArch64 equivalent.

---

## 3. Performance

### 3.1 First-hand measurements (all `VERIFIED`, M2 Max / macOS 26.5.2, 2026-08-08)

| microbenchmark | native arm64 | Rosetta x86-64 | Rosetta / native |
|---|---|---|---|
| integer LCG + branch, 60M | 0.059 s | 0.076 s | **0.78** |
| scalar `double` recurrence, 60M | 0.217 s | 0.286 s | **0.76** |
| vector FMA loop, 245M elem (SSE build) | 0.020 s | 0.028 s | **0.71** |
| vector FMA loop, 245M elem (AVX2 build) | 0.020 s | 0.040 s | **0.50** |
| 8-way `switch` dispatch, 60M | 0.037 s | 0.046 s | **0.80** |
| `seq_cst` atomic add, 20M | 0.080 s | 0.080 s | **1.00** |
| `long double` (x87) recurrence | 0.022 s | 0.339 s | **~0.06** |

These are microbenchmarks and should be read as bounds on *classes*, not as application predictions.
The clustering at **0.71–0.80 for everything that isn't x87 or AVX** is the headline.

### 3.2 AOT translation cost (`VERIFIED`, measured here)

A 325 KB x86-64 Mach-O (4000 trivial functions, `-O0`): **first launch 0.34 s wall**, subsequent
launches **≈0.00 s**. So single-digit-hundred-milliseconds for a small binary; large applications
(Adobe/Office-scale, tens of MB) take seconds on first launch, once, and are then free forever.
Code expansion: Dougall Johnson measured **1.64×** for sqlite3 (1.05 MB x86 text → 1.72 MB arm64).
That is remarkably tight for a DBT — most template-JITs land at 3–5× — and is direct evidence of how
much the flag-liveness analysis and register pinning are buying.

### 3.3 SPEC CPU — the best public dataset (`VERIFIED` measurement, third-party)

**No peer-reviewed paper has ever run SPEC under Rosetta 2.** The only real data is industry
measurement. AnandTech's M1 review (Nov 2020, now only via the Wayback Machine) ran SPEC
CPU2006/2017 native and under Rosetta on the same machine. Geomeans computed from their per-benchmark
charts:

| suite | Rosetta / native (geomean) | best subtest | worst subtest |
|---|---|---|---|
| SPECint2006 | **78.3%** | 445.gobmk 94.7% | 456.hmmer 59.9% |
| SPECfp2006 | **76.5%** | 450.soplex 88.9% | 470.lbm 61.9% |
| SPECint2017 | **78.2%** | 541.leela_r 89.3% | **502.gcc_r 49.9%** |
| SPECfp2017 | **71.8%** | 510.parest_r 83.0% | 519.lbm_r 57.2% |

An independent M1 SPEC run (Huawei ExaGear team, clang 11 `-O3 -flto`) gives **int2006 81.7%,
fp2006 85.2%, int2017 82.7%, fp2017 77.7%** — same shape, ~4 points higher level. AnandTech's own
reading: memory-footprint-bound workloads exceed 90%; execution-bound workloads are worst.
`502.gcc_r` at 49.9% is the worst public data point anywhere, attributed to "uncommon code patterns."

**So the folk figure "~70–80% of native" is correct, and the honest statement is: geomean 72–83%,
per-benchmark range 50–95%.**

### 3.4 Geekbench — including the crypto/int/float split

The famous M1 pair (1313/5888 Rosetta vs 1687/7433 native ≈ 78–79%) is **two different sessions** and
should be treated as low confidence. A same-machine, same-OS-build, same-GB-version pair uploaded two
minutes apart (Mac17,3, macOS 26.5.2, GB 5.5.1) gives **74.3% ST / 72.9% MT** overall, and the
category split is the interesting part:

| category | ST Rosetta / native | MT |
|---|---|---|
| **Crypto (AES-XTS)** | **52.5%** | 53.2% |
| **Integer** | **77.5%** | 76.5% |
| **Floating point** | **73.7%** | 71.0% |

Worst subtests: AES-XTS 52.5%, Machine Learning 63.1%, Horizon Detection 63.7%, HTML5 67.0%.
Best: Navigation 91.3%, Text Compression 90.4%. The crypto result corroborates §2.3 — AES-NI does
*not* map 1:1 onto the ARM crypto extension.

**Has the ratio improved over six years? No.** Same MacBookAir10,1 (M1), Rosetta:
macOS 11.0.1 / GB5.3.1 → **SC 1282 / MC 5883**; macOS 26.5.1 / GB5.1.1 → **SC 1272 / MC 5808**.
Flat within noise across 15 macOS major versions. **Rosetta's steady-state code quality was finished
at launch; what has changed since is coverage (AVX2 in macOS 15), not speed.** For natemu that is
encouraging *and* sobering: the architecture reaches its ceiling immediately, and then stops.

> ⚠️ **Data-quality trap.** Geekbench results reporting CPU name **"VirtualApple @ 2.50GHz"** are
> widely cited as Rosetta results and **most are not** — that string is also what a native arm64
> macOS guest in a VM reports. (My own CPUID probe in §2.3 shows Rosetta reports the same brand
> string.) The only reliable discriminator is the build string: `for macOS x86 (64-bit)` = Rosetta,
> `for macOS AArch64` = native. There is essentially **no public Geekbench 6 Rosetta data at all**,
> and M2/M3/M4-era Rosetta-vs-native measurement is a genuine gap in the public record.

### 3.5 Real applications and runtimes

| workload | Rosetta / native | note |
|---|---|---|
| 7-Zip, M1 | **71.4%** | 33,746 vs 47,239 |
| Python pyperformance, M1 | **65–69%** throughput | arm64 wins 59 of 60 tests |
| Python **startup** | **32–36%** (2.8–3.1× slower) | see §3.7 — startup tax ≫ throughput tax |
| Chrome Speedometer 2.0, M1 | **55%** | JIT-heavy |
| Chrome JetStream 2 | **59%** | JIT-heavy |
| Cubase/Omnisphere instance count, M1 Pro | **~70%** | 148 vs 212 instances |
| Photoshop, M1 (PCMag, forced same machine) | **~91%** | Adobe *claimed* 1.5×; measurement says ~9–10% |
| Lightroom import/export, M1 | **100–113%** | Rosetta sometimes **wins** — GPU/ISP/IO-bound |
| Cyberpunk 2077, CrossOver+Rosetta, M5 Air | FPS parity, **2.2× CPU**, +16% power | GPU-bound; the tax shows up as CPU and watts, not frames |

Two lessons here that matter more than the numbers:
* **Assume any JIT runtime (V8, JVM, .NET, LuaJIT, PyPy) sits at 55–65%, not 78%.** Guest-generated
  code never gets AOT-cached and re-pays translation on every process start.
* **GPU-bound and I/O-bound work costs nothing** — sometimes less than nothing, when the native build
  happens to lack an optimized path the x86 build has. Beware of that inversion when benchmarking:
  **you are always comparing two different compiler outputs, not just the translator.**

⚠️ Two frequently-miscited figures: the Premiere Pro "80% faster on M1" number is *M1-under-Rosetta
vs an Intel Mac*, not Rosetta vs native. And Phoronix once found x86-under-Rosetta beating native
arm64 on FLAC encoding purely because the ARM build lacked optimized paths.

### 3.6 Versus QEMU, box64, FEX, Prism

**Same-silicon cross-translator comparison** (7-Zip, M1 MacBook Pro, box86.org):

| | score | % of its own native |
|---|---|---|
| macOS native arm64 | 47,239 | 100% |
| **macOS Rosetta 2** | **33,746** | **71%** |
| Linux native arm64 (Asahi) | 43,271 | 100% |
| **Linux Box64 (Asahi)** | **24,746** | **57%** |

**Rosetta vs QEMU-user, in a Linux VM** — the spread is enormous and the popular "2–5× faster" claim
has no traceable primary source:
* *Best case* (sysbench, M1 Max): native 30,333 ev/s · Rosetta **25,567 (84.3%)** · QEMU
  **1,068 (3.5%)** → **Rosetta ≈ 24× QEMU**. But sysbench's tight prime loop is a best case for a
  translator and a worst case for TCG.
* *Worst case* (real parallel image build, M2 Max): native 28 s · QEMU 142 s · Rosetta **190 s** —
  Rosetta **34% slower than QEMU**, because it was pinned to a single core (an OrbStack integration
  bug, since fixed). Integration quality dominated the translator entirely.
* **Uncontrolled variable in every published Docker benchmark:** whether the guest kernel has the TSO
  patch (§5.2). With and without it you are measuring two different systems.

**QEMU-user vs FEX vs box64** (Raspberry Pi 400, % of native aarch64): 7-Zip — QEMU **16%**,
FEX **26%**, box64 **53%**. dav1d 1T — QEMU 6%, box64 27%. glmark2 (GPU-bound) — QEMU **2%**,
box64 **98%**. That **~25% on vectorized / ~50% on integer / ~100% on GPU-bound** spread is the right
mental model and it applies to Rosetta and Prism too. (The 2022 FEX column is badly stale; recent FEX
releases report +38.9% FPS in Cyberpunk 2077 and 3.7× on x87 trig.)

**Microsoft Prism** (Windows-on-ARM, 2024) — closest architectural analogue: user-mode, AOT+JIT with
a cache, on silicon with x86-emulation assists. Be skeptical of both headline claims:
* *"2× faster"* — traceable to a Windows Experience Blog footnote comparing **Snapdragon X Elite vs
  8cx Gen 3**, i.e. new emulator confounded with new silicon. Not a Prism-vs-xtajit measurement.
* *"10–20% faster"* — **no primary Microsoft source exists**; the number appears nowhere in
  Microsoft's own emulation documentation and circulates only in secondary press.
* *Same-hardware measurement* (Surface Pro X upgraded to 24H2, so the emulator is the only variable):
  Speedometer 3 **+10%**, Cinebench R23 ST **+8%**, MT **+42%**.
* *Prism % of native:* Notebookcheck concludes **~20% penalty ⇒ ~70–80% of native**. Note that
  mainstream WoA reviews systematically *avoid* publishing emulated-vs-native tables — Notebookcheck
  dropped Cinebench R23 precisely because emulation "would distort the results."

> **Prism and Rosetta 2 land in the same ~70–85% band, both roughly 4–5× the class of QEMU-user.
> Two independent teams, with different silicon help, arriving at the same number is the strongest
> available evidence that this band is a real ceiling and not an artifact of either implementation.**

*Also worth knowing:* **Parallels Desktop 20.3's x86-64 emulation is built on FEX-Emu**, and
Parallels' own documentation warns of **2–7 minute boot times**. That vendor-stated number is
the only quantitative datum in the entire Parallels/VMware/UTM category. VMware Fusion has no x86
emulation mode at all. QEMU *system*-mode is strictly slower than user-mode, so the 16%-of-native
figure above is an **upper bound** for UTM-style full-system emulation — a directly relevant data
point for natemu.

### 3.7 What the memory-model literature says the TSO bit is worth

This is the most rigorously quantified area, and the numbers are larger than usually cited:

* **Risotto (ASPLOS'23)**: fences inserted for x86→ARM memory-model correctness consume **up to 75%
  of execution time (freqmine), 48% on average** in QEMU-class translators.
* **Cost of *having* TSO on, for native ARM code**: 8.94% (JSA'24, M1 Ultra, SPECspeed FP);
  **4.2% harmonic mean** in a 2026 ASPLOS revision, dropping to **1.9%** with simple software
  changes — the outliers turn out to be implementation artifacts, some already fixed on M4.
* **Arancini (ASPLOS'26)**, a modern LLVM-based x86→ARM translator, is **8.01× slower than native**
  (6.01× with native libc) — versus Rosetta's ~1.25×. Its ablation is instructive: disabling
  **fence merging** costs up to 1.4×, while disabling **dead-flag elimination** costs only **0–10%**,
  because LLVM's own DCE recovers most of it.

**Net: you pay ~4–9% to have TSO on, and it buys away a ~48% average translation tax. That is the
single best cost/benefit ratio in this entire document.**

### 3.8 A sanity check on the academic Rosetta baselines

Several recent ML/compiler papers ("Guaranteed Guess", EMNLP 2025; arXiv:2411.16341) quote
**Rosetta 13.94 ms vs native 7.39 ms = 1.89×** on M2 Pro. ⚠️ **Do not use this number.** Those are
**~7–14 ms `-O0` HumanEval toy programs**, where the `oahd` handshake, AOT mapping and runtime init
dominate. It measures **process-startup tax, not steady-state code quality** — §3.9's ~5–6 ms fixed
per-launch overhead accounts for essentially the whole gap. At SPEC scale Rosetta is ~1.25×, not
1.89×.

### 3.9 Translation and startup cost, measured

My own measurement (§3.2): 325 KB binary, **0.34 s first launch, ~0 s thereafter.** A more careful
matched-pair measurement on the same machine class decomposes it:

| | cold (1st run) | warm (min of 5) |
|---|---|---|
| trivial binary, x86 / Rosetta | **239 ms** | 7.2 ms |
| trivial binary, arm64 native | 110 ms | 2.1 ms |
| 3.58 MB `__text`, x86 / Rosetta | **465 ms** | 8.9 ms |
| 2.45 MB `__text`, arm64 native | 133 ms | 2.5 ms |

* **Fixed first-launch tax ≈ 130 ms**, independent of size (oahd handshake + helper spawn + artifact
  creation + signing).
* **Marginal translation throughput ≈ 16 MB/s of x86 `__text`.** Extrapolating: a 100 MB text
  segment (large Adobe app) ≈ **6 s** on first launch — matching the anecdotal "~20 s first launch"
  reports on M1-class hardware.
* **Steady-state, fully cached, Rosetta still adds ~5–6 ms per process launch.** *This is the number
  that matters most in practice.* It is why Python startup is 2.8–3.1× slower while throughput is
  only ~1.5× slower, and why short-lived processes — shell scripts, build tools, test runners — suffer
  disproportionately.

**Code expansion: the widely-repeated "2–4×" figure is folklore.** The only actual instruction-byte
measurement is Dougall Johnson's **1.64×** for sqlite3. The "2.49×" number traces to a paper's
measurement of **RSS** for a trivial process (2.49 MB vs 1.03 MB) — runtime footprint, not code size.
Budget **~1.6–1.7× code expansion** for a Rosetta-quality translator.

### 3.5 The ceiling, stated plainly

For a **user-mode** x86-64→AArch64 translator with: whole-image AOT, static register pinning, flag
liveness, hardware TSO, FlagM, RAS-preserving call/ret, and a persistent shared code cache —

> **integer/branchy code: 70–80% of native. Scalar FP: ~75%. 128-bit SIMD: ~70%. Anything requiring
> interpretation or software semantics (x87, 256-bit AVX, unusual FP modes): 5–50%.**

And the corollaries that matter for natemu:
* You do **not** get to 90%+ without cross-block optimization, and Apple's engineers judged that
  trade not worth the complexity in exception handling and translation time. Believe them.
* The **variance** matters more than the mean. A workload that lands on an interpreted path loses
  10–100×. Coverage of the ISA in *compiled* form is worth more than optimizing the compiled form.
* Everything above is **ring 3**. A system-level emulator adds MMU translation, privileged-state
  emulation, and device I/O on top — none of which Rosetta pays. Do not budget 75% for a
  system emulator based on these numbers.

### 3.6 Dougall Johnson's own conclusion, verbatim

> "I believe there is room for performance improvement in Rosetta 2, by performing more
> inter-instruction optimisations. However, this would come at the cost of significantly increased
> complexity (especially for debugging and exception handling), and increased translation times.
> Engineering is about making the right tradeoffs, and I'd say Rosetta 2 has done exactly that."

---

## 4. Primary sources

Ranked by value. Full URLs in References.

1. **Dougall Johnson, "Why is Rosetta 2 fast?" (2022-11-09)** — the single best document. Flags,
   FlagM/FlagM2, the undocumented PF/AF-in-NZCV extension, TSO, the return-address shadow stack, the
   AFP-like FP extension, code expansion measurement, and the unused-flags optimisation. Read it
   first and read all of it.
2. **Koh M. Nakagawa (FFRI), "Project Champollion" parts 1 & 2 + appendix** — the systematic RE:
   `oahd`/`oahd-helper` protocol, `/var/db/oah` layout and hashing, the `.aot` Mach-O format and
   `LC_AOT_METADATA`, the register mapping table, lazy binding via `__stubs_sh`, the JIT decode
   pipeline with runtime offsets, the `aot_shared_cache` header format, and the `ROSETTA_PRINT_IR` /
   `ROSETTA_PRINT_SEGMENTS` debug hooks. Repo archived 2025-10-07 but intact; includes a
   **Ghidra SLEIGH/CSPEC patch set** for disassembling `.aot` files and an `AotSharedCacheExtractor`.
   Also presented at Black Hat (Asia/EU) — the slides are a faster read than the blog.
3. **Apple Platform Security guide, "Rosetta 2 on a Mac with Apple silicon"** — the only *official*
   description of AOT vs JIT and of the Secure-Enclave-signed translation artifacts.
4. **Apple, "Running Intel Binaries in Linux VMs with Rosetta"** + **"Accelerating the performance of
   Rosetta"** (developer.apple.com/documentation/virtualization) — official, short, and the source of
   the `prctl(PR_SET_MEM_MODEL, PR_SET_MEM_MODEL_TSO)` requirement.
5. **WWDC20-10686 "Explore the new system architecture of Apple silicon Macs"** and
   **WWDC22-10002 "Create macOS or Linux virtual machines"** — marketing-level, but WWDC20 is where
   Apple states the AOT-at-install-time model on the record.
6. **`marcan`/Asahi, "[PATCH 0/4] arm64: Support the TSO memory model" (2024-04-11)** — the
   authoritative account of the `ACTLR_EL1` TSO bit, `ARM64_HAS_TSO`, and the `PR_SET_MEM_MODEL`
   ABI. See §5.
7. **Wrenger et al., "Analyzing the memory ordering models of the Apple M1"** (JSA 2024) and
   **"TOSTING: Investigating Total Store Ordering on ARM"** (ARCS 2023) — peer-reviewed measurement
   of what TSO mode costs and how it behaves.
8. **`Lifeisawful/rosettax87`, `Lifeisawful/rosettax87_jit`, `athei/x87sidecar`** — practical RE of
   Rosetta's x87 path and of its arm64-PC→x86-PC validation invariant.
9. **`saagarjha/TSOEnabler`** — kext to enable TSO for arbitrary macOS processes.
10. **`FFRI/ProjectChampollion/AotSharedCacheExtractor`** — working parser for `aot_shared_cache`.

### 4.1 Undocumented-instruction hunting

Dougall Johnson also maintains AArch64 microarchitecture work (`dougallj/applecpu`,
`dougallj/apple_arm64_hw_regs`) that catalogues Apple IMPDEF system registers by brute-force probing.
That methodology — enumerate `S3_<op1>_C<n>_C<m>_<op2>` and observe faults — is how the TSO bit,
APRR, and the flag extension were all found, and it is directly reusable if natemu wants to check
what a given Apple part exposes to EL0/EL1.

---

## 5. Rosetta for Linux VMs — the important section

### 5.1 The delivery mechanism (`VERIFIED`)

Apple ships **`/Library/Apple/usr/libexec/oah/RosettaLinux/`** containing two **statically linked,
stripped aarch64 ELF executables** (confirmed with `file` on this machine):

```
rosetta    1,726,504 bytes   the translator + runtime, as a Linux ELF
rosettad     383,560 bytes   the AOT translation cache daemon
```

Virtualization.framework exposes them to a guest via **`VZLinuxRosettaDirectoryShare`**, mounted into
the VM as a **virtiofs share conventionally tagged `rosetta`**. The guest mounts it and registers the
`rosetta` binary as a **`binfmt_misc`** handler for the x86-64 ELF magic; thereafter any x86-64 ELF
`execve`d in the guest is handed to `rosetta`, which maps and translates it. `rosetta`'s own usage
string, extracted here, is exactly:

```
Usage: rosetta <x86_64 ELF to run>
ROSETTA_DEBUGSERVER_PORT    wait for a debugger connection on given port
```

Nothing else is documented in the binary. There is **no kernel module, no driver, no device node** in
the share — it is one static userspace binary plus a cache daemon.

### 5.2 How the in-VM Rosetta gets TSO — **the answer is: the guest kernel must be patched** (`VERIFIED`)

This is the headline finding of the whole dossier and it is unambiguous.

Apple's own developer documentation, *"Accelerating the performance of Rosetta"*, tells Linux
distributors to add TSO support **to the guest kernel** and states that applications must call:

```c
prctl(PR_SET_MEM_MODEL, PR_SET_MEM_MODEL_TSO);
```

**per thread**, and that the purpose is to let "the CPU's memory model match x86-64 behavior" so that
emulators "avoid emulating TSO in software, which degrades performance."

Consequences, spelled out:

* **There is no Apple-specific hypercall and no paravirtual TSO interface.** `VERIFIED` by my own
  full disassembly of `RosettaLinux/rosetta` (`llvm-objdump -d`, 115k instructions): **zero `HVC`
  instructions, zero `SMC` instructions.** Every kernel entry is a plain `svc #0` (165 sites). The
  only system registers it touches are `NZCV` (flag save/restore, 97 sites), `FPCR`/`FPSR`,
  `TPIDR_EL0`, `CNTVCT_EL0`/`CNTVCTSS_EL0`/`CNTFRQ_EL0`, `ID_AA64MMFR1_EL1` (the AFP probe, §2.3) and
  the IMPDEF `S3_6_C15_C2_5`. **It is an ordinary Linux userspace program.**
* **The mechanism is the ordinary EL1 one.** Under Virtualization.framework the Linux guest runs at
  EL1 and therefore **owns `ACTLR_EL1`**. The guest kernel sets the Apple TSO bit in `ACTLR_EL1` for
  threads that asked for it, and context-switches it. Apple's virtual CPU exposes the IMPDEF bit to
  the guest; that is the entire "trick."
* **Therefore `Virtualization.framework` is not doing anything Hypervisor.framework structurally
  cannot.** The vCPU's `ACTLR_EL1` is guest-owned state in both. `SPECULATION` (important, test
  before relying on it): what VZ *does* provide that HVF does not is (a) the licensed delivery of the
  Rosetta binaries into the guest via `VZLinuxRosettaDirectoryShare`, and (b) whatever host-side
  attestation `rosetta` performs at startup. Community reports that Rosetta-for-Linux "only works
  under VZ" are consistent with a *licensing/plumbing* restriction rather than a hardware one —
  and libkrun/krunkit's claimed Rosetta support (it is an HVF-based VMM) is the strongest evidence
  that the restriction is not architectural.
* **The prctl ABI is the same one Asahi upstreamed.** marcan's April 2024 series
  *"arm64: Support the TSO memory model"* adds `PR_SET_MEM_MODEL` / `PR_GET_MEM_MODEL` with
  `PR_MEM_MODEL_DEFAULT` and `PR_MEM_MODEL_TSO`, an `ARM64_HAS_TSO` cpucap distinguishing CPUs that
  are *intrinsically* TSO from Apple's IMPDEF-`ACTLR_EL1` case, and `ACTLR_EL1` save/restore on
  context switch. Cover-letter framing: *"x86 CPUs implement a stricter memory model than ARM64
  (TSO). For this reason, x86 emulation on baseline ARM64 systems requires very expensive memory
  model emulation."* It had shipped downstream in Asahi for over a year at that point and is what
  FEX-Emu consumes. Upstream reception was **contested** — maintainers objected to enshrining an
  undocumented IMPDEF feature and to userspace-ABI fragmentation. Treat "merged upstream" as
  **not established**; treat "available in Asahi and in Apple-blessed guest kernels" as established.

**Read for natemu:** the deepest lesson is that **even Apple, on Apple's own hardware, could not get
TSO into a Linux guest without changing the guest kernel.** TSO is EL1-privileged, per-thread state.
Any design that wants it must either (a) own EL1 (run guest code inside your own HVF VM, where you
program `ACTLR_EL1` yourself), or (b) ship a kernel change, or (c) do software TSO and pay for it.
There is no fourth option.

### 5.3 AOT caching in the Linux guest (`RE-CLAIM`, my own strings analysis — and it corrects a common
misconception)

Rosetta-for-Linux is **not** JIT-only. Strings extracted from the two binaries:

`rosettad`:
```
usage: %s <command> [<args>]
  daemon [<cache path>]
      Runs the aot translation daemon, using <cache path> as its cache directory.
      If <path> is empty, the daemon defaults to caching in "$HOME/.cache/rosettad".
  translate <input binary> <output binary>
Failed to create socket / Failed bind socket / Failed to listen on socket
Filesystem socket path too long, shorten socket filename or change AOT cache root directory
".aotcache"
```

`rosetta`:
```
"%s/.cache"   "%s/.cache/rosetta"   "%s/.cache/rosetta/%s.flu"
DISABLE_AOT          AOT_ERRORS_ARE_FATAL
Failed to map AOT header / Failed to map AOT translation / Failed to map metadata for AOT
Failed to map runtime routines for AOT / Failed to mprotect runtime routines for AOT
AOT header specified too many segments   aot_hdr->segment_count <= kMaxSegments
segment_meta->kind == CodeFragmentKind::Aot
did not find other fragments for segmented aot
```

So the Linux side reproduces the macOS architecture: a **daemon** (`rosettad`) reachable over a
**UNIX socket**, a **per-user on-disk AOT cache** (`~/.cache/rosettad`, `.aotcache`; the runtime also
uses `~/.cache/rosetta/*.flu`), and a **fall-back JIT** in-process. `ROSETTA_DISABLE_AOT=1` forces
JIT. Note `Failed to mprotect runtime routines for AOT` — on Linux there is no APRR trick available,
so the guest pays real `mprotect` calls (§1.6). This is one concrete reason to expect
Rosetta-for-Linux to be *somewhat* slower than Rosetta-on-macOS on JIT-heavy guests.

### 5.4 `ROSETTA_DEBUGSERVER_PORT` and the debug path (`VERIFIED` from the binary)

`rosetta`'s only documented environment variable. Setting it makes `rosetta` **wait for a debugger
connection on that TCP port before starting the guest program**. Implementation, from source-file
names and strings inside `RosettaLinux/rosetta`:

```
GDBServer.cpp  PacketHandlers.cpp
"debugserver port must be set" / "debugserver port not valid" / gEnvironment.debugserver_port != 0
PTRACE_SEIZE / PTRACE_INTERRUPT / PTRACE_CONT / PTRACE_EVENT_STOP / PTRACE_EVENT_CLONE
PTRACE_GETREGSET for GPRs / PTRACE_GETREGS for FPRs / PTRACE_SETREGS ... while adjusting rip
"Failed to get debug regset for thread %d"
```

i.e. `rosetta` implements a **GDB remote-serial-protocol server** that presents the *x86-64* register
view (`rip`, x86 GPRs, x87/SSE FPRs) to the attached debugger, while using **Linux `ptrace` on
itself/its threads** to drive the underlying arm64 execution. This is the same role
`/usr/libexec/rosetta/debugserver` plays on macOS (`DebugServer.cpp` in the macOS `runtime`).
Usage: `docker run -e ROSETTA_DEBUGSERVER_PORT=1234 …`, then `gdb -ex 'target remote :1234'`.

`RosettaLinux/rosetta` also contains vDSO handling (`Failed to find vdso elf header in aux vector`,
`get_vdso_dynsym_shdr`, `get_vdso_load_offset`) — it must locate the **arm64** vDSO and re-present
x86-compatible entry points to the guest program, plus `ProcMapsParser.cpp`, `MmapTracker.cpp`,
`ElfMapper.cpp`, `InitStack.cpp`, `ThreadContextSyscalls.cpp`, `Signals.cpp`. That last set is the
whole Linux personality layer: **x86-64 syscall ABI → arm64 syscall ABI translation**, signal frame
translation, and `/proc/self/maps` synthesis.

### 5.5 What the guest sees (`VERIFIED`, string extracted verbatim)

The `/proc/cpuinfo` `flags` line is a **hard-coded string constant** inside `RosettaLinux/rosetta`
(§2.3). Rosetta-for-Linux therefore advertises **AVX/AVX2/FMA/F16C/BMI1/BMI2/MOVBE by default**,
unlike the macOS side which hides them behind `ROSETTA_ADVERTISE_AVX`. `ROSETTA_ADVERTISE_AVX` /
`advertise avx value not valid` are also present in the Linux binary, so the same knob exists there.

### 5.6 Ecosystem plumbing

Docker Desktop's *"Use Rosetta for x86/amd64 emulation on Apple Silicon"*, Lima/Colima's
`rosetta: enabled: true`, OrbStack, and Podman/krunkit all wire up the same three steps: mount the
virtiofs `rosetta` tag, register `binfmt_misc`, run `rosettad`. `SPECULATION` (worth confirming
before relying on it): most report substantially better throughput than `qemu-user-static` binfmt but
a long tail of images that fail — typically statically-linked or unusual-`ld.so` images, and anything
that trips the ISA gaps in §6.

---

## 6. What Rosetta 2 explicitly does **not** do

This list is the point of the whole dossier for a system-level emulator: **essentially every
simplification below is one natemu does not get.**

| Not supported | Confidence | Detail |
|---|---|---|
| **Ring 0 / privileged x86** | `VERIFIED` | No CPL, no CR0–CR4, no MSRs, no `IRET`/interrupts, no `SYSENTER`/`SYSCALL` semantics beyond routing to the host syscall path, no I/O ports. `libRosettaRuntime` string `unsupported privilege level: %x` — it *notices* privilege levels only to reject them. Kernel extensions cannot be translated. |
| **x86 MMU / paging** | `VERIFIED` | Guest linear addresses **are** host virtual addresses. No page tables, no TLB, no `CR3`, no A/D bits, no nested paging. The whole address-translation problem simply does not exist for Rosetta. |
| **Full segmentation** | `VERIFIED` (strings) | There *is* a `Segmentation.cpp` and `mov_segment` / `push_segment` / `pop_segment` / `get_segment_limit` / `translate_far_call` / `translate_far_jmp` / `translate_far_ret` — but bounded: `CS can't be set with mov_segment`, **`setting GS to a nonzero selector is unsupported`**, `LDT 0x%llx not supported`, `i386_set_ldt(kLdtAutoAlloc) not supported`, `Descriptor not supported`. Practically: FS/GS **base** (TLS) works; real descriptor tables, limits and far transfers are stubs for Wine-style edge cases. |
| **32-bit x86** | `VERIFIED` | No i386 support at all. Confirmed structurally (Apple removed 32-bit from macOS in Catalina, before Rosetta 2 existed) and locally — the toolchain can no longer even link `-arch i386`. This is *the* reason 32-bit Windows games need Wine's 32-on-64 (`wow64`) or a separate emulator. |
| **AVX-512** | `VERIFIED` (measured) | EVEX → **SIGILL**. Not advertised under any setting. |
| **AVX/AVX2 as a *fast* path** | `VERIFIED` (measured) | Implemented (macOS 15+ / present here), hidden from CPUID by default, and **slower than the SSE path** because YMM is emulated as 2×128-bit. |
| **SHA / ADX / ERMS / TSX / RDSEED / CLWB** | `VERIFIED` (CPUID + Linux flags string) | Absent. `x86-64-v3`-ish, never `v4`. |
| **Native-speed x87** | `VERIFIED` (measured) | Software 80-bit library. 10–100× penalty. |
| **General self-modifying code, cheaply** | `RE-CLAIM` | SMC *works* — that is what `TranslationCacheJit.cpp` is for — but every write to a page containing translated code invalidates and re-translates. There is no cheap incremental patching of a translated block; the machinery is `Repatch.cpp` for the translator's own fixups, not for guest SMC. Guest-generated code (JS/JVM/.NET/Wine thunks) is the acknowledged worst case, is never AOT-cached, and re-pays translation on every process start. |
| **Injecting your own arm64 into a Rosetta process** | `RE-CLAIM` (x87sidecar) | Rosetta validates that every executing arm64 PC has a corresponding x86 PC in its tables; foreign code aborts with `rosetta error: no code fragment associated with the given arm pc`. |
| **Running under a non-Apple hypervisor (in-guest)** | `SPECULATION` | Widely reported that Rosetta-for-Linux requires Virtualization.framework rather than a generic HVF VMM; libkrun/krunkit's support suggests this is plumbing/licensing, not architecture. Untested here. |
| **Being around much longer** | `VERIFIED` | At WWDC 2025 Apple announced that most of Rosetta 2 will be **removed in macOS 28 (2027)**, with residual support limited to unmaintained games and older frameworks. Plan accordingly if you intend to *depend* on it rather than learn from it. |

---

## 7. Transferable techniques — ranked for natemu

**Tier 1 — take these, they are free and large.**
1. **Static register pinning** (§1.7). Guest GPRs + vector regs permanently in host regs. Costs
   nothing, saves everything. Only viable when host register file ≥ guest register file.
2. **Flag liveness analysis** (§2.2c). Do not compute what is not read. This is where most of the
   flag cost goes away, hardware assist or not.
3. **FEAT_FlagM / FlagM2** (§2.2a). Architectural. `CFINV`, `RMIF`, `SETF8/16`, `AXFLAG`/`XAFLAG`.
   Check the cpucaps and use them.
4. **Call/return as `BL`/`RET` with a validated shadow stack** (§2.5). Preserves the host
   return-address predictor. Large and frequently overlooked.
5. **Persistent, content-addressed, versioned code cache** (§1.3). Hash content *and* identity; write
   `.in_progress` + atomic rename; keep a version stamp that invalidates everything on translator
   update; keep negative entries.
6. **Pre-translate and share the system libraries once** (§1.4). For a system emulator the analogue
   is a shared, read-only translation of the guest kernel image and hot shared objects.
7. **`CRC32CX`-hashed inline indirect-branch table** (§2.6). Six instructions, one cache line,
   `{guest_pc, host_pc}` pairs, base+mask pinned in registers. `FEAT_CRC32` is Armv8.1-mandatory, so
   this is available to everyone. Back it with delta-coded per-image offset tables → global tree →
   translate.
8. **A small fixed stub ABI between translated code and the runtime** (§2.7) — 13 entry points, not
   an open call surface.

**Tier 2 — take these, they need care.**
9. **TSO** (§2.1, §5.2). Only reachable if you own EL1 — i.e. **run guest code inside your own
   Hypervisor.framework VM.** This is a strong architectural argument for the hypervisor-hosted
   design, and it is worth *multiples*, not percents, on multithreaded guests. Verify HVF lets your
   guest EL1 write the IMPDEF `ACTLR_EL1` bit before designing around it.
10. **FEAT_AFP** (§2.3). Architectural from Armv8.7 (`ID_AA64MMFR1_EL1[47:44]`); gives x86
   NaN/tininess/denormal corner cases for free via `FPCR.{AH,NEP}` plus `FIZ`/`FZ` from guest MXCSR.
   On pre-8.7 Apple parts the IMPDEF `S3_6_C15_C2_5` carries DAZ (bit 0) and FZ (bit 1) and is
   **confirmed EL0-readable inside a VZ guest**. Copy Apple's exact probe: check `HWCAP_CPUID`, read
   `ID_AA64MMFR1_EL1`, pick one of three modes.
11. **Cheap W^X via page-permission-remap registers** (§1.6). `pthread_jit_write_protect_np` on
    macOS; dual RW/RX aliased mappings elsewhere. Never `mprotect` per flip.
12. **Precise per-instruction state + a state-recovery pass** (§1.2, `StateRecovery.cpp`). Apple pays
    real performance for this and considers it worth it. For a *system* emulator, where you must also
    deliver guest interrupts and faults precisely, it is not optional.

**Tier 3 — know about these, you cannot have them.**
13. **PF/AF computed in hardware into NZCV bits 26/27** (§2.2b). Apple-only, undocumented. Budget for
    software PF/AF on the residue that liveness analysis cannot kill — this is exactly what
    Rosetta-for-Linux appears to do.
14. **Secure-Enclave-signed translation artifacts** (§1.3). A nice integrity model; not reproducible
    outside Apple.

15. **A cheap 128÷64 divide** (§2.7). It does not exist on AArch64. Apple pays a 64-iteration
    software loop; so will you. You can only reduce the *frequency*, via the dividend-width analysis
    in `MulDivAnalysis.cpp`. Same for 128-bit `MUL` results.

**Anti-patterns Apple deliberately avoided, and so should you:**
* Do not build a general optimizing IR with cross-block optimization first. Apple measured 1.64× code
  expansion and ~75% of native from a *macro-expander with two analyses*. Get that first.
* Do not chase 256-bit SIMD width matching. Splitting YMM into 2×128-bit made AVX code *slower* than
  SSE code on the same source. Width mismatch is a trap.
* Do not treat x87 as a footnote. It is a 10–100× cliff and it is why old software is unusable, not
  merely slow.

---

## 8. Open questions for natemu (things this dossier could not settle)

1. Does Hypervisor.framework permit a guest at EL1 to set the Apple TSO bit in `ACTLR_EL1`, and does
   HVF context-switch it correctly across `hv_vcpu_run`? **Test directly — this is the highest-value
   unknown in the whole document.**
2. ~~Is the IMPDEF `S3_6_C15_C2_5` (AFP-equivalent) register accessible from a guest, or trapped?~~
   **Resolved (§2.3):** Apple's own Linux-guest binary reads it with a plain `MRS` from EL0, so it is
   readable from userspace inside a VZ guest. Still open: is it *writable* from EL0, or does Rosetta
   only read it while something at EL1 sets it?
3. Does this M2 Max expose the PF/AF-in-NZCV mode to a guest at all? (Probably reachable the same way
   Rosetta reaches it — a mode bit somewhere — but nobody has published the enable.)
4. What exactly does `RosettaLinux/rosetta` do at startup to detect it is under Virtualization.
   framework, and is that check the reason it is VZ-only? (`rosettad`/`rosetta` are stripped static
   ELFs; a targeted disassembly of the init path would answer it.)
5. Measured cost of `mprotect`-based W^X vs APRR on the *same* JIT workload — i.e. how much of the
   macOS-vs-Linux Rosetta gap is §1.6.

---

## References

**Primary reverse engineering**
- Dougall Johnson, *Why is Rosetta 2 fast?* — https://dougallj.wordpress.com/2022/11/09/why-is-rosetta-2-fast/
- Dougall Johnson, blog index — https://dougallj.wordpress.com/
- Koh M. Nakagawa (FFRI Security), *Project Champollion* — https://github.com/FFRI/ProjectChampollion
  - Landing page — https://ffri.github.io/ProjectChampollion/
  - Part 1, *Analyzing AOT files and Rosetta 2 runtime* — https://ffri.github.io/ProjectChampollion/part1/
  - Part 2, *Analyzing other aspects of Rosetta 2 runtime and AOT shared cache files* — https://ffri.github.io/ProjectChampollion/part2/
  - Appendix — https://ffri.github.io/ProjectChampollion/appendix/
  - `AotSharedCacheExtractor` — https://github.com/FFRI/ProjectChampollion/tree/main/AotSharedCacheExtractor
  - Ghidra SLEIGH/CSPEC patches — https://github.com/FFRI/ProjectChampollion/tree/main/ghidra

**Apple official**
- Apple Platform Security, *Rosetta 2 on a Mac with Apple silicon* — https://support.apple.com/guide/security/rosetta-2-on-a-mac-with-apple-silicon-secebb113be1/web
- Apple Developer, *Accelerating the performance of Rosetta* — https://developer.apple.com/documentation/virtualization/accelerating-the-performance-of-rosetta
- Apple Developer, *Running Intel Binaries in Linux VMs with Rosetta* — https://developer.apple.com/documentation/virtualization/running-intel-binaries-in-linux-vms-with-rosetta
- `VZLinuxRosettaDirectoryShare` — https://developer.apple.com/documentation/virtualization/vzlinuxrosettadirectoryshare
- WWDC22-10002, *Create macOS or Linux virtual machines* — https://developer.apple.com/videos/play/wwdc2022/10002/
- WWDC20-10686, *Explore the new system architecture of Apple silicon Macs* — https://developer.apple.com/videos/play/wwdc2020/10686/

**TSO / Linux kernel**
- marcan, *[PATCH 0/4] arm64: Support the TSO memory model* (2024-04-11) — https://patchew.org/linux/20240411-tso-v1-0-754f11abfbff@marcan.st/
- LKML mirror — https://lkml.iu.edu/hypermail/linux/kernel/2404.1/04825.html
- Wrenger, Lüdtke, Lohmann, *Analyzing the memory ordering models of the Apple M1*, Journal of Systems Architecture (2024) — https://www.sra.uni-hannover.de/Publications/2024/wrenger_24_jsa.pdf · https://www.sciencedirect.com/science/article/pii/S1383762124000390
- Wrenger et al., *TOSTING: Investigating Total Store Ordering on ARM*, ARCS 2023 — https://www.sra.uni-hannover.de/Publications/2023/tosting-arcs23/wrenger_23_arcs.pdf · https://dl.acm.org/doi/10.1007/978-3-031-42785-5_10
- `saagarjha/TSOEnabler` — https://github.com/saagarjha/TSOEnabler

**x87 / community RE**
- `Lifeisawful/rosettax87` — https://github.com/Lifeisawful/rosettax87
- `Lifeisawful/rosettax87_jit` — https://github.com/Lifeisawful/rosettax87_jit
- `athei/x87sidecar` — https://github.com/athei/x87sidecar

**AVX**
- `carsongoodwin32/rosetta2_avx_dive` — https://github.com/carsongoodwin32/rosetta2_avx_dive
- Stockfish issue #5707, *Running x86 AVX2 binary on Apple Silicon with Rosetta 2* — https://github.com/official-stockfish/Stockfish/issues/5707

**General**
- Wikipedia, *Rosetta (software)* — https://en.wikipedia.org/wiki/Rosetta_(software)

**Measurements marked `VERIFIED` in §2.3, §3.1, §3.2 and all `strings`/`otool` extracts** were taken
by me on 2026-08-08: Apple M2 Max, macOS 26.5.2 (25F84), Xcode `clang`, sources in the session
scratchpad. They are reproducible with the snippets described inline.
