# Survey: keeping translator guest state reachable while keeping guests out

Status: research survey, October 28, 2026. It gathers evidence only; nothing in `crates/` or `tools/fexc` changed.
Statements without a citation are labelled **inference**. "Not found" means the item was searched for within this
survey's budget (about 60 tool calls) and no primary source was found.

## The question

natemu runs FEX-translated code at EL0 in a Hypervisor.framework (HVF) VM on an Apple M4. Isolation and exact x86 faults
come from hardware: every x86-non-canonical address faults, and emulator pages are EL1-only or execute-only
(`experiments/track03/el0-study/README.md`, l.382-387 and l.480-488). FEX's generated code reads and writes some guest
state in memory through x28 in the middle of blocks: DF, the FS/GS bases, MXCSR, x87 registers and status, and the AVX
upper halves (README l.389-393 and l.490-491). At EL0 that state would need an EL0-accessible page. That page sits at a
non-canonical x86 address, so a guest access to exactly that page would reach the page instead of raising #GP.

The owner asks three things:

1. Can this state be handled with specialised snippets or traps instead?
2. How does FEX handle it?
3. What existing solutions to this general problem exist?

natemu's own measured costs (README §3, l.165, l.171, l.215):

| Operation | Cost |
|---|---|
| `svc` round trip from EL0 to an EL1 handler and back | 31.10 ns |
| Linked block edge | 0.528 ns |
| `svc` instead of `br` into the resident | +15.9 ns per entry |

## 1. FEX

FEX is pinned in `third_party/FEX` at commit `48d71752e2a363d5535181f803b0b0e9666c1825`. Paths below are relative to
`third_party/FEX/FEXCore`.

### Where guest state lives

| State | Where FEX keeps it | Source |
|---|---|---|
| Pointer to the CPU state | x28 (`STATE`) | `Source/Interface/Core/ArchHelpers/Arm64Emitter.h` l.33 |
| 16 GPRs | Statically allocated host registers: x4-x17, x19, x29 | `Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp` l.47-66 |
| PF and AF | Pinned to x26 and x27 | `Arm64Emitter.h` l.45-46 |
| Call-return stack pointer | x25 | `Arm64Emitter.h` l.48 |
| XMM registers | 16 static vector registers v16-v31 (`SRAFPR`) | `Arm64Emitter.cpp` l.92-97 |
| AVX upper halves (`avx_high[16][2]`) | Memory | `include/FEXCore/Core/CoreState.h` l.114 |
| MXCSR | Memory (`mxcsr`) | `CoreState.h` l.123 |
| FS and GS bases (`fs_cached`, `gs_cached`) | Memory | `CoreState.h` l.127-128 |
| DF | Memory, as byte `flags[10]` | `CoreState.h` l.129; `include/FEXCore/Core/X86Enums.h` l.67 |
| x87 TOP | Memory, as `flags[43]`, 3 bits | `X86Enums.h` l.98 |
| x87 tag word and control word | Memory (`AbridgedFTW`, `FCW`) | `CoreState.h` l.191-192 |
| MMX/x87 register file (`mm[8][2]`) | Memory | `CoreState.h` l.130 |
| Block header and RIP | Memory | `CoreState.h` l.91, l.104 |

Notes on the table:

- The AVX upper halves would sit in registers only with 256-bit SVE. FEX uses a 256-bit guest vector length only when
  the host supports both SVE256 and AVX (`Source/Interface/Core/OpcodeDispatcher.h` l.1638). Otherwise it emulates AVX
  as two 128-bit halves (`OpcodeDispatcher/AVX_128.cpp` l.114-128).
- The M4's `sysctl hw.optional.arm` lists no `FEAT_SVE` key (measured on this machine, macOS 26.6.2). **Inference:** on
  the M4, FEX takes the 128-bit path, so `avx_high` lives in memory.

### How FEX caches memory-resident state within a block

- **A per-block register cache.** The IR builder keeps a 64-bit cache bitmap (`OpcodeDispatcher.h` l.1905-1925). DF has
  index 31, MM0-MM7 have indices 20-27, and the AVX upper halves have indices 48-63.
- **When the cache is written back.** `FlushRegisterCache` writes back the cached entries that were written: DF is
  stored at l.1226-1227, and MM and AVX-high entries through `CacheIndexToContextOffset` (l.1177-1262). It runs in
  full at every new block (`StartNewBlock`, l.148-154) and at exits.
- **Per instruction.** FEX calls `FlushRegisterCache(true)` after every guest instruction (`Source/Interface/Core/Core.cpp`
  l.630-638). That is the "SRA only" mode, which flushes only the GPR and FPR entries (`OpcodeDispatcher.h`
  l.1196-1203). So DF, MM and AVX-high stay cached until a block boundary.
- **What this means for natemu.** A block that uses DF, MMX/x87 or AVX-high touches memory at least once at entry, to
  load, and once at the exit, if the value was written. These accesses are in the middle of the block whenever the
  first use comes after the entry.
- **The x87 stack pass.** `Source/Interface/IR/Passes/x87StackOptimizationPass.cpp` (l.18-31) keeps a virtual x87 stack
  within a block. It emits the loads and stores to the stack registers, TOP and tag word only at block end. If a value
  comes from a previous block, it falls to a "slow path" that loads and stores the stack registers directly. FEX
  reads and writes TOP directly in memory (`OpcodeDispatcher/X87.cpp` l.30 and l.58) and writes the tag word at l.54.
- **x87 slow-path instruction counts.** The FEX-2510 release notes (mirror:
  <https://newreleases.io/project/github/FEX-Emu/FEX/release/FEX-2510>) report that caching on the slow path cut ARM
  instructions from 169 to 72 (Half-Life) and from 3,165 to 1,743 (Oblivion).
- **Reduced precision.** `X87ReducedPrecision` defaults to false. It "emulates X87 floating point using 64-bit
  precision" (`Source/Interface/Config/Config.json.in` l.621-627). The precision mode selects 64-bit rather than f80
  operand sizes (`OpcodeDispatcher/X87.cpp` l.129 and l.518-551). Full precision uses SoftFloat F80 helpers
  (`Source/Common/SoftFloat.h`), called through the JIT's F80 ABI thunks (`Source/Interface/Core/JIT/JIT.cpp`
  l.125-190).
- **MXCSR outside guest instructions.** When the JIT is entered, FEX reads `mxcsr` to set FPCR.FIZ from MXCSR.DAZ
  (`Arm64Emitter.cpp` l.645-659). LDMXCSR and STMXCSR load and store the field (`OpcodeDispatcher/Vector.cpp` l.3165
  and l.3297).
- **FS/GS bases.** These are loaded from memory for each segment-override address computation and for RD/WRFSBASE
  (`Source/Interface/Core/OpcodeDispatcher.cpp` l.389-392 and l.2982-2998).
- **DF.** DF is read when a REP string operation chooses its direction (`OpcodeDispatcher.h` l.290-291).

### Does FEX protect its own state from the guest?

- **Not found.** No FEX document or source describes protecting `CPUState` from guest loads and stores.
- **Inference:** FEX is a user-mode emulator that shares one process address space with the guest, as Houdini does
  (below). The x86 analogue in QEMU is explicit: "Users with non-virtualization use cases must not rely on QEMU to
  provide guest isolation" (QEMU `docs/system/security.rst`, v10.1.0, l.48-50).

## 2. Other x86-on-ARM translators (and Houdini)

| Translator | What is published on state and x87 | Source |
|---|---|---|
| Apple Rosetta 2 | "a full, slow, software implementation of x87's 80-bit floating point numbers". PF/AF are kept in NZCV bits 26-27, an Apple extension. Where CPU state lives, and FS/GS and DF handling: **not found**. | Dougall Johnson, "Why is Rosetta 2 fast?", <https://dougallj.wordpress.com/2022/11/09/why-is-rosetta-2-fast/> |
| Microsoft Prism / xtajit | JIT-compiles x86 blocks to Arm64 and caches the translations per module. User mode only. State layout and x87 internals: **not found**. | <https://learn.microsoft.com/en-us/windows/arm/apps-on-arm-x86-emulation> |
| Box64 (ARM64 dynarec) | The 16 GPRs are in x10-x25, EFLAGS (including DF) in x26 and RIP in x27. x0 (`xEmu`) points to `x64emu_t`, which holds `xmm[16]`, `ymm[16]`, `x87[8]`, `mmx[8]`, `sw`, `top`, `cw`, `mxcsr`, `fpu_tags` and `segs_offs[6]`. SSE and AVX registers are cached in NEON registers by `sse_get_reg`/`ymm_get_reg`. The x87 stack has push and get helpers. x87 80-bit precision is "not really emulated" (v0.3.6). | commit `30d5c77188fa9cf8bea7064d93ced554c3a34cde`: `src/dynarec/arm64/arm64_mapping.h` l.41-60, l.102; `src/emu/x64emu_private.h` l.62-97; `src/dynarec/arm64/dynarec_arm64_helper.h` l.316-354, l.981-999; <https://box86.org/2025/06/new-box64-v0-3-6-released> |
| ExaGear / Huawei | **Not found.** | none |
| Intel Houdini (ARM→x86, the reverse direction) | Interpreter. ARM registers are in a `struct proc_state` that "can be read/written from both ARM and x86". The guest shares the virtual address space, there are multiple RWX pages, and "Houdini ignores the execute bit entirely". | B. Hong, Black Hat USA 2021, <https://i.blackhat.com/USA21/Wednesday-Handouts/US-21-Hong-Sleight-of-ARM-Demystifying-Intel.pdf> |
| Transmeta CMS | Every register has a shadow register. `commit` copies working state to the shadow and `rollback` restores it. Stores wait in a gated store buffer until commit. Where CMS itself lives and how it is hidden: **not found** in the sources reached. | Dehnert et al., CGO 2003, DOI 10.1109/CGO.2003.1191529, via <https://www.cs.cornell.edu/courses/cs6120/2019fa/blog/transmeta/>; gated store buffer patent US 6,011,908 |
| IBM DAISY | Translated code is kept "in a portion of main memory not visible to the old architecture", managed by a VMM in ROM. | IBM report RC23262, <https://dominoweb.draco.res.ibm.com/reports/rc23262.pdf> |
| DEC FX!32 | **Not found** (Chernoff et al., IEEE Micro 1998, not reached). | none |

**Inference (pattern):** user-mode translators such as FEX, Box64 and Houdini keep their state in ordinary process
memory and do not claim to isolate it from the guest. The whole-system translators, Transmeta CMS and DAISY, keep the
translator in memory the guest architecture cannot address.

## 3. System-level translators and VMMs that protect their own state

| System | Mechanism | Limits and cost | Source |
|---|---|---|---|
| VMware BT (Adams and Agesen, ASPLOS 2006) | The VMM is mapped "in the high part of the guest's address space". Guest segments are "truncated" so they do not overlap it, and a translated access to the VMM then faults. Selected instructions use %gs, which is not truncated, as "an escape into VMM-level data structures"; for example, guest %rcx is spilled through %gs. Guest instructions that carry a %gs prefix are translated differently. | "Current Intel CPUs lack support for segment limits in 64 bit mode … the software VMM fails Popek and Goldberg's Safety requirement" on that host. AMD Opteron supports 64-bit segment limits. No cost is given for the %gs path. | <https://pdos.csail.mit.edu/6.1810/2019/readings/adams06vmware.pdf>, §3.2 and §4.4 |
| Xen PV, 32-bit | Xen is mapped into the top 64 MB of every address space. Guest kernels run in ring 1, with segment limits that exclude that 64 MB. 32-bit hypervisor support was removed later (4.3 per the thread). | Paging alone cannot separate ring 1 from ring 3. | xen-devel, <https://lists.xen.org/archives/html/xen-devel/2005-01/msg01182.html> |
| QEMU TCG | `env` (CPUArchState) is pinned in host register `TCG_AREG0` = x19 on AArch64. In system mode, every guest access goes through the softmmu TLB, so a guest address never names host memory. | TCG "must not" be relied on for isolation (security.rst l.48-50). | QEMU v10.1.0 `tcg/aarch64/tcg-target.h` l.45; `docs/devel/tcg.rst` l.170-190 |
| VirtualBox raw mode / recompiler | The recompiler was dropped in 6.1.0 ("Drop recompiler"). Raw-mode internals: **not found**. | | <https://borncity.com/blog/2019/12/13/virtualbox-6-1-0-freigegeben/> (secondary) |
| Bochs | **Not inspected.** | | none |

**Inference:** VMware's design is the closest precedent to natemu's question:

- It uses one designated access path to reach VMM state, the %gs escape that only the translator emits.
- It gives every other guest access hardware protection: segment truncation, which plays the role of natemu's
  non-canonical fault.
- It translates guest uses of that path specially: guest %gs-prefixed instructions get a non-IDENT translation.

The 64-bit Intel case shows a precedent for accepting a residual gap when hardware lacks the feature.

## 4. Software fault isolation and sandboxing

| System | How runtime state is kept away from untrusted code | Measured cost | Source |
|---|---|---|---|
| NaCl x86-64 | An aligned 4 GB region with 10×4 GB guard regions on each side. r15 ("RZP") holds the base and is read-only to untrusted code. Addresses are formed from r15 plus a 32-bit value. The trusted runtime is outside the region. | 7% average | Sehr et al., USENIX Security 2010, <https://www.scs.stanford.edu/~zyedidia/docs/papers/nacl2.pdf> |
| NaCl ARM | Untrusted code is confined to the low 1 GB; the upper 3 GB hold the trusted runtime. `bic` masks store and branch targets, with guard regions. Loads are not constrained. | under 5% | same paper |
| LFI (ASPLOS 2024) | 4 GiB sandboxes with 80 KiB guard regions. Reserved registers: x21 is the base, x18 always holds a valid sandbox address, and x30 and sp are always in-sandbox. The runtime-call table is in the sandbox's first page, which is read-only to the sandbox, so a call needs no trampoline or mode switch. Current LLVM LFI moves the base to x27 and adds x25, which points to "a thread-local virtual register file for storing runtime context". Thread-pointer `mrs`/`msr` are rewritten to x25-relative accesses. | 7% on the SPEC 2017 subset. Sandbox-to-sandbox yield "roughly 50 cycles". A null syscall is 6× faster than Linux. | <https://www.scs.stanford.edu/~zyedidia/docs/papers/lfi.pdf> §4.4, §5.3, §6; <https://releases.llvm.org/22.1.0/docs/LFI.html> |
| Wasm (Wasmtime) | Linear memory is reserved with guard regions so bounds checks can be removed. This needs signal-based traps, a 64-bit host and a reservation of at least 4 GiB. | Explicit bounds checks cost roughly 1.2×-1.8×. | <https://docs.wasmtime.dev/examples-fast-execution.html> |
| Segue | Keeps the Wasm memory base in the x86-64 segment register (gs), removing the base add. | Removes 44.7% of Wasm's overhead on SPEC CPU 2006. | Narayan et al., ASPLOS 2025, <https://cseweb.ucsd.edu/~dstefan/pubs/narayan:2025:segue.pdf> |
| XFI | Inline software guards, a two-stack model and a standalone verifier, for kernel and user modules. Details of how runtime state is placed: not extracted. | "modest" | Erlingsson et al., OSDI 2006, <https://static.usenix.org/event/osdi06/tech/full_papers/erlingsson/erlingsson_html/index.html> |
| Deterministic sandboxes | **Not found** within budget. | | none |

**Inference:** in every SFI scheme above, runtime per-thread state lives outside the addressable sandbox. Where code
must read it inline, it is either a read-only page that is part of the sandbox's address contract (LFI's call table)
or a reserved register (NaCl r15, LFI x21/x27 and x25, Segue gs).

**Inference (difference from natemu):** an SFI sandbox defines its own address space, so a reserved page inside it
creates no observable deviation. natemu must reproduce x86's #GP for every non-canonical address, so any EL0-reachable
page at a non-canonical address is a deviation.

## 5. Hardware features

| Feature | What it would give natemu | On the M4 | On the M4 inside an HVF guest |
|---|---|---|---|
| FEAT_S1POE (POR_EL0) | Per-thread, EL0-writable overlay permissions on up to 8 keys, which also apply to instruction fetch. Translated code could open the state key while in its own code. **Inference:** a guest access to the page is still a plain EL0 access, so POE alone cannot tell a guest load from a FEX state load at the same time. | Not shown. LLVM's `apple-m4` feature list is v8.7-A plus extensions and has no POE (`AArch64Processors.td`, llvmorg-21.1.0, `AppleM4`). macOS `sysctl hw.optional.arm` has no POE key (measured). POE "was introduced in Armv8.9". | The HVF SDK headers have no `HV_SYS_REG_POR_EL0` or `ID_AA64MMFR3_EL1` enum (measured on the local SDK). Not found. |
| FEAT_LSUI | Unprivileged load, store and atomic instructions that let EL1 access EL0 memory without clearing PAN (Armv9.6). Useful only if translated code ran at EL1 with guest accesses made unprivileged. | **Inference:** absent, because the M4 is v8.7-A per LLVM. | Not found. |
| MTE | Tag-checked accesses. A state page with a tag that guest-derived pointers never carry would fault on guest access. | Absent: `hw.optional.arm.FEAT_MTE` = 0 (measured). | Absent: guest `ID_AA64PFR1_EL1` = `0x0000000202000001`, MTE field [11:8] = 0 (measured through `hv_vcpu_get_sys_reg`). |
| PAN / EPAN | Stops EL1 from touching EL0 pages; EPAN also covers EL0 execute-only pages. This is the opposite direction to natemu's need (EL0 must not reach a page). | | Guest `ID_AA64MMFR1_EL1` = `0x0000100011312000`, PAN field [23:20] = 3, which is FEAT_PAN3/EPAN (measured). |
| Apple SPRR (APRR successor) | Page permissions are an index into per-EL permission registers. User-space JIT W^X switching (`pthread_jit_write_protect_np`) writes `S3_6_C15_C1_5`. | Present (M1 onward). | Under HVF, "every access to registers probably related to SPRR always faulted". A private entitlement, `com.apple.private.hypervisor.vmapple`, allows EL1 guests to use them. Not available to third parties. |
| Intel MPK/PKU (x86 analogue) | Per-thread PKRU switching via WRPKRU. ERIM pairs it with binary inspection. | x86 only. | n/a |
| CHERI / Morello DDC | A default data capability bounds every legacy load and store, so a register-held capability could reach state that legacy accesses cannot. | Not on Apple hardware; Morello is an Arm research board (2022). | n/a |

Sources for the table:

- **POE:** Linux protection-keys documentation patch,
  <https://lists.infradead.org/pipermail/linux-arm-kernel/2024-October/965531.html>, which also gives the v8.9 note.
- **LSUI:** <https://patchew.org/linux/20260121190622.2218669-1-yeoreum.yun@arm.com>.
- **SPRR:** S. Peter, <https://blog.svenpeter.dev/posts/m1_sprr_gxf/>.
- **ERIM:** WRPKRU "11–260 cycles", <https://arxiv.org/pdf/1801.06822>.
- **Morello:** <https://www.theregister.com/2022/08/26/arm_cheri_morello/> and
  <https://unikraft.org/blog/2022-12-01-unikraft-on-morello>.
- **Measured values:** from a small HVF probe in this survey's scratchpad (`hv_vm_create`, `hv_vcpu_create`,
  `hv_vcpu_get_sys_reg`), signed with `experiments/track03/native-handoff/entitlements.plist`, on an Apple M4 running
  macOS 26.6.2.

**Inference:** no hardware feature found on the M4 under HVF lets one EL0 register or mode reach a page that other EL0
accesses cannot. MTE and POE are absent, SPRR is entitlement-gated, and PAN works in the wrong direction.

## 6. Applying it to each guest-state item

The approaches considered for each item:

- **Registers:** keep the state in registers.
- **Trap:** trap into the runtime.
- **svc snippet:** load and store at block entry or exit through `svc`.
- **Per-block cache:** cache the state per block and spill only at exits.
- **State page:** an EL0 guest-state page, which leaves one page of residual non-exactness.
- **Hardware permission feature.**

| Item | How FEX accesses it | Candidates with source support | Known cost |
|---|---|---|---|
| DF | Byte in memory. Cached per block, written back at block end (`OpcodeDispatcher.h` l.1226-1227). Read by REP string operations. | **Registers:** Box64 keeps DF inside EFLAGS in x26 (`arm64_mapping.h` l.58). **Per-block cache:** FEX already does this. **svc snippet:** DF changes only on CLD, STD, POPF and IRET. | Register: no memory access. svc: 31 ns per access. No source measures DF specifically. |
| FS/GS base | Loaded from memory for each segment-prefixed access (`OpcodeDispatcher.cpp` l.389-392). | **Registers:** Segue uses a dedicated segment register for a base; LFI's x25 holds thread context; Box64 keeps `segs_offs` in memory. **svc snippet on writes:** WRFSBASE and the `arch_prctl` path are rare (**inference**). **Trap.** | Segue: 44.7% of Wasm overhead removed by moving a base into a register. svc: 31 ns per access, too much if paid on every TLS access (**inference**: TLS accesses are frequent; not measured). |
| MXCSR | Memory. Read at JIT entry for FPCR.FIZ; read and written by LDMXCSR/STMXCSR. | **Registers:** the rounding and flush state are already live in FPCR; `msr fpcr` works at EL0 (README l.298). **Trap or svc** on LDMXCSR/STMXCSR (**inference:** rare instructions). | svc: 31 ns per LDMXCSR/STMXCSR. Not measured on workloads. |
| x87 registers, TOP, tag and status | `mm[8]`, `flags[43]` and `AbridgedFTW` in memory. The x87 stack pass removes stack traffic within a block, and a slow path covers cross-block use. | **Per-block cache:** FEX's x87 stack pass; Box64's x87 cache helpers. **Trap:** Rosetta's "full, slow, software implementation" (mechanism not published). **svc snippet:** load at block entry and store at exit. **State page.** | FEX slow path: 169→72 and 3,165→1,743 ARM instructions per sequence (release notes). svc at entry and exit: 2 × 31 ns per x87 block, against 0.53 ns per linked edge (natemu). |
| AVX upper halves | `avx_high` in memory on non-SVE256 hosts, which includes the M4 (**inference**, see §1). Cached per block (indices 48-63). | **Registers:** FEX with SVE256, not available on the M4. Box64 `ymm_get_reg` caches them in NEON registers. **Per-block cache.** **svc snippet** at block entry and exit. **State page.** | svc as for x87. No source measures AVX-high spill cost. |
| All items | | **State page** at one fixed non-canonical address per vCPU. **Hardware permission feature:** none usable (§5). | Zero extra instructions over FEX's current code (**inference**). Deviation: a guest load or store to that 4 KiB page does not raise #GP. |

## Options for natemu

Each option names its sources. Costs are natemu's measured costs unless stated otherwise.

1. **One EL0 guest-state page at a fixed non-canonical address, with residual non-exactness.**
   - It keeps FEX's x28-relative code unchanged for DF, FS/GS, MXCSR, x87 and AVX-high (FEX sources in §1).
   - The deviation: guest accesses to that one 4 KiB page per vCPU reach the page instead of raising #GP. This is the
     same shape as the data-stop deviation already described in README l.484-488.
   - The page holds only that vCPU's guest state and no pointer the resident trusts (README l.391-392).
   - Precedent for accepting a residual gap: VMware's 64-bit Intel case (Adams and Agesen §4.4).

2. **Registers for DF and FS/GS base, and FPCR for MXCSR's live part.**
   - Sources: Box64 keeps DF in an EFLAGS register; Segue and LFI keep bases and thread context in reserved registers;
     README l.298 shows `msr fpcr` works at EL0.
   - It needs FEX register-assignment patches. Not verified: which host registers are free under FEX's
     `Arm64Emitter.cpp` allocation.
   - **Inference:** this removes the most frequent mid-block accesses (DF on REP strings, FS on TLS) without a page.

3. **`svc` snippets for rarely changed state.** These are LDMXCSR/STMXCSR, WRFSBASE and FS/GS reloads, CLD/STD/POPF,
   and FXSAVE/XSAVE-style whole-state operations.
   - Each costs 31 ns (natemu). **Inference:** acceptable only where the instruction is rare.
   - Precedent: VMware's special translation of %gs-prefixed instructions (Adams and Agesen §3.2).

4. **Per-block caching with `svc` load and store at block entry and exit for x87 and AVX-high.**
   - FEX already caches these per block (`FlushRegisterCache`, the x87 stack pass).
   - Moving the entry and exit memory accesses into `svc` costs about 62 ns per block that touches the state. Compare
     0.53 ns for a linked edge.
   - **Inference:** this costs more than option 1 for x87- or AVX-heavy loops. FEX's per-instruction flush leaves
     these entries cached (Core.cpp l.638), but FEX's "slow path" for cross-block x87 still loads from memory, which
     would also need `svc`.

5. **Trap per instruction (Rosetta-like slow x87).**
   - Rosetta's x87 is software and slow (Johnson), but its trap mechanism is not published.
   - At 31 ns per trap, natemu would pay this on every x87 instruction. **Inference:** reasonable only for x87 code
     that is not hot.

6. **Hardware permission feature: not available.**
   - POE, LSUI and MTE are absent on the M4 or not exposed (§5). SPRR needs a private entitlement under HVF. PAN works
     in the opposite direction.
   - Revisit this if a future Apple core exposes POE or MTE to HVF guests.

7. **Translated code at EL1 with guest accesses made unprivileged (`ldtr`/`sttr`).** natemu's README already lists
   this (l.281). It needs a FEX emitter patch and is the one route where an EL1-only state page needs no residual gap.
   No external translator was found that uses it.
