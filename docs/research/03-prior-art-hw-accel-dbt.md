# Prior Art: Hardware-Accelerated Full-System Dynamic Binary Translation

**Scope.** Using a host hypervisor / hardware MMU / privilege-level machinery to accelerate *cross-ISA full-system* emulation. The target of this project is an x86-64 guest on an Apple Silicon (ARM64) host using `Hypervisor.framework`, so every work below is graded on transferability to that setting.

**Status:** research notes, compiled 2026-08-08. Section 1 (Captive) and Section 2 (HyperMAMBO-X64) are read from the primary sources (PDFs extracted and quoted). Later sections are broader surveys.

---

## 0. Executive summary of the design space

There are exactly three known strategies for making cross-ISA *system* emulation fast, and only the third is a general solution:

| Strategy | Representative | Core idea | Ceiling |
|---|---|---|---|
| **Software MMU (softmmu)** | QEMU TCG | Software TLB array consulted on every guest load/store; guest page-table walks in software | ~10–20 host instructions per guest memory access; 5–100x slower than native |
| **Host-OS-mediated shadow mapping** | HSPT / "Embedded Shadow Page Table" (`mmap`-based), Embra | Use host `mmap`/`mprotect` to build a guest-virtual → host-virtual map so a guest access is one host instruction | Limited by host OS page-table API granularity and syscall cost; ~1.9x over QEMU |
| **Bare-metal-in-a-VM: own the hardware MMU and privilege levels** | **Captive**, HyperMAMBO-X64, MagiXen, Transmeta CMS | Run the DBT *inside a hardware VM* so it can program page tables, rings/ELs, IDT/VBAR, TLB flushes directly | 2.2–2.5x over QEMU; ~40% of a same-clock native core; within ~1.4x of native on some workloads |

The rest of this document is mostly about the third row, because that is what this project is.

---

## 1. Captive — the closest prior art

**Papers**
- T. Spink, H. Wagstaff, B. Franke. *Hardware-Accelerated Cross-Architecture Full-System Virtualization*. ACM TACO 13(4), Article 36, Dec 2016. DOI: [10.1145/2996798](https://dl.acm.org/doi/10.1145/2996798)
- T. Spink, H. Wagstaff, B. Franke. *A Retargetable System-Level DBT Hypervisor*. USENIX ATC '19, pp. 505–520. **Best Paper.** [usenix.org/conference/atc19/presentation/spink](https://www.usenix.org/conference/atc19/presentation/spink) · [PDF](https://www.usenix.org/system/files/atc19-spink.pdf)
- Extended journal version: *A Retargetable System-Level DBT Hypervisor*, ACM Trans. Comput. Syst. (TOCS), 2020. DOI: [10.1145/3386161](https://dl.acm.org/doi/10.1145/3386161) · [AAM PDF (St Andrews)](https://research-repository.st-andrews.ac.uk/bitstream/handle/10023/24320/Spink_2020_ACM_Trans._Comput._Syst_Retargetable_System_Level_AAM.pdf)
- T. Spink. *Efficient Cross-architecture Hardware Virtualisation*. PhD thesis, University of Edinburgh, 2016/2017. Handle [1842/25377](https://era.ed.ac.uk/handle/1842/25377) — **Chapter 6 is the long-form version of the TACO paper and contains the implementation detail the papers omit; read this one first.**
- Also relevant from the same group: *Efficient Asynchronous Interrupt Handling in a Full-system Instruction Set Simulator* (LCTES '16); *SimBench* micro-benchmark suite (Wagstaff et al.).
- Source: **https://github.com/avisi-group/captive** (MIT, still receiving commits as of Dec 2025). Linux + KVM only; the maintained model is an AArch64 guest.

### 1.1 The structural idea

Captive is *three* pieces:

1. **Hypervisor** — an ordinary Linux user-space process. Talks to **KVM** to create a same-architecture (x86-64) hardware VM. Holds all *device* models (GIC, SP804 timer, UARTs, VirtIO block). Guest- and host-architecture agnostic; its only dependency is KVM.
2. **Execution engine** — a **unikernel** that runs *inside* that VM. There is no OS in the VM. The engine is effectively an x86-64 kernel: it sets up the GDT, the IDT, the page tables, installs fault handlers, and contains the JIT compiler and code cache. Host-architecture specific, guest-architecture agnostic.
3. **Architectural implementation** — generated offline by **GenSim** from an ArchC-derived ADL. Provides the decoder, the per-instruction IR generator functions, the guest MMU model, and the guest exception/interrupt behaviour. Guest-architecture specific.

The thesis names the middle layer precisely: the **"Native VM"** is a *same-architecture* VM (x86-64 on x86-64) obtained from Intel VT via KVM. The **"Guest VM"** (ARM) is then constructed *inside* it in software. Captive is therefore not a cross-ISA hypervisor in the hardware sense — it is a cross-ISA DBT that has been given a bare-metal machine to live on.

```
Host Linux (ring 3)  ── Captive hypervisor process ── device models, KVM ioctls
        │  KVM / Intel VT (VMX root)
        ▼
Native VM (VMX non-root, x86-64 bare metal)
   ring 0 : execution engine + JIT + code cache + page-fault handler
            + translated code for guest PL1 (kernel) code
   ring 3 : translated code for guest PL0 (user) code
```

### 1.2 Privilege levels: guest modes → host rings

This is the mechanism most often mis-summarised, so verbatim from the thesis (§6.3.3.4):

- ARM **PL0 (USR)** guest code is executed in x86 **ring 3**; ARM **PL1** guest code (SVC, ABT, IRQ, …) is executed in x86 **ring 0**.
- ARM has many *modes* but only two *privilege levels*, so the mapping is 2→2 and clean.
- Ring transitions are emitted by the JIT as **`syscall`/`sysret`** (fast system-call instructions), explicitly *not* `int` — "avoiding the overhead associated with a software interrupt instruction".
- Example traces given: a guest `swi #0` compiles to a `sysenter`-class transition into ring 0; the guest's `movs pc, lr` return compiles to `sysret` back to ring 3. A guest data abort arrives as a *real x86 page fault*, which enters ring 0, runs the ABT-mode behaviour, and returns via `sysret`.
- **x86 call gates** (GDT entries) are used so that ring-3 translated code can invoke helper routines that need ring-0 privilege, again avoiding `int`.
- **`%fs`** holds the per-CPU emulated-CPU-state pointer (keeps GPRs free for the register allocator). **`%gs`** is the base for the "secondary guest virtual memory" region (see §1.4). **`%r15d`** is a dedicated host register holding the guest PC.

The payoff is not aesthetic. Because the host CPU's current ring matches the guest's current privilege level, the **user/supervisor bit in the host page tables directly enforces the guest's user/kernel page permissions in hardware**. No software permission check on any memory access.

### 1.3 The MMU mechanism — mapping guest page tables onto the host hardware MMU

This is the central contribution. It is *shadow paging*, but shadow paging done by a DBT that owns real page tables rather than one that must go through `mmap`.

**Address-space layout (thesis Fig. 6.13, TOCS Fig. 15).** The Native VM's 48-bit virtual address space is split:
- **Lower half** (`0` … `2^47−1`, and for a 32-bit guest just the bottom 4 GB) = **the guest's virtual address space, 1:1**.
- **Upper half** = Captive itself: execution engine code, heap/stack, and a **one-to-one identity map of guest *physical* memory** (used by the software guest-MMU model to walk guest page tables by physical address).
- Captive's own mappings are marked **global** so they survive TLB flushes.

**The fault-driven fill (thesis Fig. 6.10).**
1. A guest load/store is emitted as **a single host `mov` on the unmodified guest virtual address**.
2. First touch → **real x86 page fault inside the Native VM**.
3. Captive's page-fault handler calls the *generated* guest-MMU model: walk the guest (ARM) page tables → guest physical address + permission bitmask, or a fault.
4. If permitted, synthesise the equivalent **x86 PTE** and install it; return and **retry the faulting instruction**. The guest MMU returns the *full* permission set so a page that is also writable only faults once.
5. If not permitted, a **non-local jump** out of the fault handler to a "safe point" in the main execution loop, which then runs the guest-architecture fault behaviour (e.g. ARM enters ABT mode).

**Permission translation (thesis Fig. 6.11).** ARM L2 descriptor AP/APX bits → x86 PTE `USER/SUPERVISOR` + `READ/WRITE` + `PRESENT`. Example given: ARM "PL1 read-only, PL0 no access" → x86 PTE with both `R/W` and `U/S` cleared, i.e. supervisor-read-only. This only works because of the ring tracking in §1.2.

**Invalidation is O(1).** The entire guest address space is confined to the **first entry of the top-level page table** (PML4[0], covering 0–512 GB). A guest TLB-flush instruction is intercepted and handled by **clearing the present bit on that single L4 entry + one host TLB flush**. "This makes invalidations very quick to perform." In the 64-bit TOCS version this generalises to "invalidate the first 256 PML4 entries".

**ASIDs / PCIDs.** Rather than rebuilding on every guest context switch, Captive keeps **one L3 table per guest ASID** and swaps the PML4[0] pointer when the guest ASID changes (thesis Fig. 6.12). Guest `TLBIASID`-style instructions are trapped and used to invalidate only that ASID's tables. Acknowledged limitation: only works if the guest ISA *has* ASIDs and the guest kernel *uses* them (the same limitation HSPT hit). Fallback suggestion: key on the guest page-table base pointer value.

**Page-size mismatch.** "We support an n:m mapping between guest and host page sizes, where n, m are powers of 2." A larger guest page becomes multiple host pages. A *smaller* guest page than the host page is the hard direction: "we must emulate memory accesses carefully to ensure permissions within a page are not violated."

**64-bit guest on 64-bit host** (the ATC'19/TOCS extension; the 2016 thesis explicitly punted on this). The guest's 64-bit VA no longer fits in the host's lower half. Captive's answer:
- Emit **extra checking on every guest memory access** to see whether the guest VA is above the split.
- If it is, **switch the host page-table base to a second set of tables** for the upper mapping, and **mask the guest VA** down into the lower half before the access.
- Cost: a host TLB flush on every switch — "could be quite expensive if a series of memory access instructions alternate between address spaces". Mitigation: use **PCIDs**, treating lower-half and upper-half as two "processes", so the switch is a PCID change rather than a flush.
- The paper's own table lists this as the ARMv8-A-specific challenge: "64-bit guest on 64-bit host emulation → Additional techniques for MMU emulation."

**SLAT interaction.** With Intel EPT / AMD NPT enabled (KVM enables it by default), the guest-virtual → host-*virtual* step disappears: the hardware walks Captive's page tables to a Native-VM physical address and EPT maps that to host physical. So the hardware does a **two-dimensional walk that spans the guest→host ISA boundary**. Measured EPT benefit on SPEC CPU2006 was only **~3%** for Captive (and ~0% for QEMU/KVM and VirtualBox) — consistent with the literature that EPT helps MMU-microbenchmarks (VMware measured up to 600%) far more than SPEC.

### 1.4 Code cache, SMC, and the physical-address indexing decision

- Translations are **indexed by guest *physical* address**, unlike QEMU which indexes by guest virtual address. Consequences:
  - Guest page-table changes **do not invalidate translations** (QEMU must flush).
  - The same translation is reused across **different virtual mappings of the same physical page** (shared libraries, `fork`).
  - Cost: the guest PC is virtual, so a code-cache lookup needs a V→P translation, worst case a guest page-table walk. Mitigated by **same-page block chaining** (blocks are terminated at page boundaries, and chaining is only allowed within a page, so the common case never re-translates the PC).
  - Cost: the JIT **cannot constant-fold the guest virtual PC** into translations. Compensated by dedicating `%r15d` to the guest PC, which turns PC-relative loads into `mov 0x64(%r15d), %eax` — still 3 instructions vs QEMU's 13 for the same guest `ldr r0, [pc, #92]`.
- **SMC detection**: physical pages from which code has been translated are flagged and **write-protected in the host page tables**. A write fault to a flagged page invalidates that page's translations. Special case: if the write hits the page *currently executing*, the store is emulated and control leaves the translation via a non-local jump rather than resuming it.

### 1.5 Devices and MMIO — the part that is *not* hardware-accelerated

Device models live in the hypervisor, outside the VM. Naively that means **a KVM MMIO exit per device access**, which is very expensive; and device accesses are frequent (the thesis works the example of a 100 Hz Linux timer needing GIC + timer reads + acks per tick).

Captive's solution:
1. **Detect device accesses using the Native VM's own MMU**: pages backing device MMIO ranges are marked not-present in Captive's page tables, so a device access faults **inside the VM** (cheap) rather than exiting the VM (expensive).
2. **Communicate with the hypervisor without a VM exit**: a shared request structure plus a **two-barrier spin protocol**. The guest vCPU spins on barrier 1; a hypervisor thread that was already parked on that barrier wakes, performs the emulated-device access, writes the result, and enters barrier 2, releasing the guest. No VMCS save/restore.
3. **Speculative device-access instruction rewriting** was implemented and *rejected*: speculation was correct 100% of the time, but (a) the smallest emitted `mov` is 3 bytes, leaving only an undefined-opcode trap as a rewrite target, and trapping `#UD` costs an exception frame anyway; (b) device accesses are physical but the instruction operates on virtual addresses, so a V→P translation is needed per access.

**Interrupts.** The emulated GIC's output line is injected as a **real x86 IRQ into the Native VM**. The VM's IRQ handler sets an *interrupt-pending flag*; translated code checks that flag **at every basic-block boundary** before chaining. Guest instructions expand to many host instructions and are not re-entrant, so the interrupt cannot be taken mid-instruction — block boundaries are the safe points. (The region-based scheme of thesis Ch. 5 allowed sparser checks; Captive's block-based JIT cannot use it.)

### 1.6 Retargetability (the ATC'19 contribution)

- Guest ISA described in a **modified ArchC ADL**; instruction semantics written in a C-like DSL that mirrors manual pseudocode.
- Offline: lowered to a **domain-specific SSA**, aggressively optimised (DCE, inlining, constant/value propagation, jump threading, load coalescing, PHI elimination — O1..O4), then emitted as C++ **generator functions** that call the JIT's emitter API. O1→O4 shrinks the generated ARMv8-A model from **271,299 → 120,162 lines (−56%)**.
- Operations are tagged **fixed** (evaluated at translate time) vs **dynamic** (must be emitted) — classical partial evaluation.
- Online: decode a block → invoke generator functions → build an **invocation DAG**, collapsing at side-effecting nodes and emitting low-level IR (which on x86 is essentially x86 with virtual registers) → **linear-scan/simplified-graph-colouring register allocation** with dead-instruction marking → direct machine-code encoding + relative-jump patching.
- Model sizes: **8,100 lines** of ADL for ARMv8-A, vs **17,766 lines** for QEMU's ARM model **+ 7,948 lines** of QEMU softfloat.
- Other described guests and their pain points: ARMv7-A (IT blocks spanning pages), x86-64 (stateful/prefix decoding — needed an *external* decoder library), RISC-V ("no significant challenges"), TI TMS320C6x DSP (VLIW, nested delay slots), Arm Mali-G71 GPU (bundle headers).

### 1.7 Floating point and SIMD

- QEMU calls softfloat helpers; Captive emits **host FP/SIMD instructions operating directly on the guest register file**, with **inline fix-up code** where the architectures disagree (NaN sign, rounding modes, infinities, flags). Documented example: `SQRTSD` of a negative returns a *negative* NaN on x86 while ARMv8 `FSQRT` returns a *positive* NaN — fixed up inline.
- Constraint: **the host must be at least as precise as the guest**; otherwise fall back to x87 80-bit or to softfloat.
- Isolated effect: hardware FP gives **2.17x** over QEMU; replacing Captive's FP with QEMU's softfloat drops it to **1.68x** — i.e. **1.30x** of the speedup is attributable to hardware FP alone. The rest of the 6.49x FP-suite number comes from combining that with the rest of the design.

### 1.8 Numbers

**ATC '19 / TOCS 2020** — ARMv8-A guest (full unmodified ARM Linux) on x86-64, QEMU 2.12.1 baseline, host Intel Xeon E5-1620 v3 @ 3.5 GHz:

| Metric | Value |
|---|---|
| SPEC CPU2006 **integer**, geomean speedup vs QEMU | **2.21x** |
| SPEC CPU2006 **C++ floating-point**, geomean vs QEMU | **6.49x** |
| Benchmarks slower than QEMU | 456.hmmer, 462.libquantum (suboptimal register allocation in hot code) |
| vs native ARM: Raspberry Pi 3 (Cortex-A53 @ 1.2 GHz) | Captive is **~2x faster** than the real chip |
| vs native ARM: AMD Opteron A1170 (Cortex-A57 @ 2.0 GHz) | Captive reaches **~40%** of native |
| vs native x86-64 on the host itself | native is **7.24x** faster than Captive |
| JIT translation throughput vs QEMU | Captive is **2.6x slower** to translate a block (SimBench Small/Large-Blocks: 85% / 65% slower) |
| JIT time breakdown | Translate 54.54%, RegAlloc 25.63%, Encode 17.08%, Decode 2.75% |
| Code quality (429.mcf, block-chaining disabled, `rdtscp` around each block) | Captive blocks run **3.44x** faster than QEMU's |
| Code density | Captive **67.53** host bytes/guest instruction vs QEMU **40.26** (Captive emits real vector ops; QEMU emits calls). ~**10 host instructions per guest instruction** plus prologue/epilogue |
| ARMv7-A guest | ~**2.5x** average, up to 6x |
| SimBench micro-benchmarks | Captive wins nearly everywhere; two memory categories show **32.38x** and **25.88x**. Loses on Small-Blocks, Large-Blocks (codegen speed) and Data-Fault handling |

**TACO 2016 / thesis** — ARMv7-A (RealView PB-A8, Linux 4.3.0, Arch Linux ARM userspace) on x86-64, QEMU 2.4.0 baseline:

| Metric | Value |
|---|---|
| SPEC CPU2006 int geomean vs QEMU | **2.5x** (best: 429.mcf at **5.88x**) |
| vs **HSPT** ("Private SPT" embedded shadow page tables in QEMU), both normalised to the Android Emulator | HSPT **1.94x**, Captive **2.05x**; on 483.xalancbmk HSPT 1.72x vs Captive **2.88x** |
| Slowdown vs native ARM (ODROID-XU, Cortex-A15 @ 2.0 GHz) | Captive **1.4x**, QEMU **3.51x** |
| EPT on vs off | **~3%** for Captive; ~0% for QEMU/KVM and VirtualBox on SPEC |
| I/O `hdparm` **cached** reads | native x86 12384 MB/s; VirtualBox 11941; QEMU/KVM 11881; QEMU-DBT-x86 1265 (9.79x slowdown); **QEMU-DBT-ARM 157 (78.87x slowdown)**; **Captive 1695 (7.3x slowdown)** → Captive is **10.8x** faster than QEMU here |
| I/O **buffered** reads | native 173.52 MB/s; Captive 155.72 (**1.11x** slowdown); QEMU/ARM 105.77 (1.64x) |

### 1.9 Captive's stated limitations

From the thesis §7.2 and the papers' future-work sections:
1. **Single-core only.** "Whilst we currently focus on a single-core implementation…" Multi-core was designed for but never implemented. This is the single biggest gap — the code cache, SMC protection, and TLB-shootdown design all change under SMP.
2. **The MMU mapping is only as good as the ISA pair.** "The behavioural mapping of an ARM MMU to an x86 MMU works well, because the configuration and operation, whilst incompatible, are *similar in behaviour*. This may not necessarily be the case for other architectures." A guest whose MMU is not a hierarchical page-table walker (their example: an ARC variant using I/O-space page mappings) may not map at all.
3. **64-bit-on-64-bit costs extra.** Address-space split checking, page-table switching, TLB flush or PCID juggling per cross-half access.
4. **Hard KVM dependency.** Host OS portability sacrificed by design; the alternative (a custom kernel module talking to VT/AMD-V directly, VirtualBox-style) was judged too expensive and a deployment barrier.
5. **GenSim can't express** branch delay slots, context-sensitive/prefix decoding (hence the external x86 decoder), non-standard control flow (zero-overhead loops), VLIW, or out-of-order semantics.
6. **All devices are software models** in the hypervisor; no pass-through, no host-device backing. Explicitly listed as future work.
7. **No nested virtualization** of the guest: guest EL2 / VMX-in-guest is out of scope. Listed as future work.
8. **Interrupt checks at every block boundary** (the cheaper region-based placement of Ch. 5 is incompatible with the block-based JIT).
9. **Slower JIT than QEMU** (2.6x), tolerable only because translations are longer-lived (physical indexing).

### 1.10 Captive lineage / follow-on work worth reading

- **M. Kristien. *Efficient cross-architecture simulation of multicore systems*. PhD thesis, University of Edinburgh, 2024.** Handle [1842/42345](https://era.ed.ac.uk/handle/1842/42345). Directly attacks Captive's #1 limitation. Three contributions: (a) a **scalable and provably correct scheme for emulating LL/SC guest atomics on a CAS-only host** — the standard "LL/SC → CAS" shortcut is *divergent* from real hardware, and this fixes that while staying scalable; (b) memory-efficient decode caching; (c) multicore full-system simulation including dynamically generated code. Read this before designing the SMP story.
- **T. Spink, B. Franke. *Accelerating Shared Library Execution in a DBT*. LCTES '24.** DOI: [10.1145/3652032.3657565](https://doi.org/10.1145/3652032.3657565). Exploits the physical-address-indexed code cache to share translations of shared libraries.
- **SimBench** (Wagstaff et al.) — the full-system-emulator micro-benchmark suite used for Captive's per-category breakdown (Mem-Hot-MMU, Mem-Cold-MMU, Undef-Instruction, Syscall, Data-Fault, Instruction-Fault, Small/Large-Blocks, Same/Inter-Page Direct/Indirect, TLB-Flush, TLB-Evict). If you want an apples-to-apples comparison with Captive, this is the harness to reuse.

---

## 2. HyperMAMBO-X64 — the hypervisor-assisted *application-level* translator

**Papers**
- A. d'Antras, C. Gorgovan, J. Garside, J. Goodacre, M. Luján. *HyperMAMBO-X64: Using Virtualization to Support High-Performance Transparent Binary Translation*. VEE '17, pp. 228–241. DOI: [10.1145/3050748.3050756](https://dl.acm.org/doi/10.1145/3050748.3050756) · [PDF](https://pure.manchester.ac.uk/ws/portalfiles/portal/159336267/vee_6.pdf)
- A. d'Antras, C. Gorgovan, J. Garside, M. Luján. *Optimizing Indirect Branches in Dynamic Binary Translators*. ACM TACO 13(1), Article 7, 2016. DOI: [10.1145/2866573](https://dl.acm.org/doi/10.1145/2866573)
- A. d'Antras et al. *Low Overhead Dynamic Binary Translation on ARM*. PLDI '17. DOI: [10.1145/3062341.3062371](https://dl.acm.org/doi/abs/10.1145/3062341.3062371) · [PDF](https://pure.manchester.ac.uk/ws/files/56078084/pldi_16.pdf)
- MAMBO tool: https://github.com/beehive-lab/mambo

> **Correction to a common mis-citation:** MAMBO-X64 / HyperMAMBO-X64 translate **AArch32 → AArch64**, *not* x86-64 → ARM64. The motivating scenario is deleting AArch32 hardware from future ARMv8 cores (Cavium ThunderX already shipped without it). The *techniques* are what transfer, not the ISA pair.

### 2.1 The hybrid model

Neither application-level (fast, not transparent) nor system-level (transparent, slow). Instead:

```
EL2  Hypervisor (KVM-based) — traps AArch64→AArch32 mode switches
EL1  AArch64 guest kernel — runs NATIVELY, owns its own page tables
EL0  AArch64 guest processes — run natively
EL0  DBT + translated code — runs here, under hypervisor control
```

- The guest kernel runs **native at EL1** and manages its own stage-1 page tables. Therefore **there is no software MMU and no page-table format translation at all** — this is the entire performance story.
- The hypervisor traps the guest kernel's attempt to `ERET` into AArch32 EL0, and instead enters the DBT.
- The DBT and its translated code execute at **EL0**, so ordinary stage-1 permissions and permission faults apply correctly with zero extra work.
- On any exception (syscall, page fault, virtual interrupt), the DBT reconstructs the **AArch32 register context** and hypercalls back; the hypervisor restores the original page tables and **synthesises an exception entry in the guest kernel** so the kernel believes an AArch32 process faulted. Fully transparent — even a guest debugger sees the AArch32 process.

### 2.2 Context recovery (precise state)

Three mechanisms, worth copying:
- **Translation-time-known exceptions** (syscalls, undefined instructions): recovery code compiled directly into the fragment.
- **Runtime faults** (data aborts): per-fragment metadata table listing every potentially-faulting instruction and how to reconstruct the AArch32 register context at it.
- **Asynchronous virtual interrupts**: keeping metadata for *every* instruction is impractical, so instead — **catch the interrupt, resume the fragment with interrupts disabled until the end of the fragment**, then recover state from the fragment-linking metadata. This is the classic "defer to the next safe point" trick, done at fragment rather than block granularity.

### 2.3 Memory management without owning the page tables

- A **separate region of guest physical RAM** is reserved for the DBT (image, heap, code cache, metadata), one instance per VM. The DBT **never modifies guest OS RAM**.
- Injection into the process address space is done with a **shadow top-level page table** held in DBT RAM. Layout (Fig. 5): `0–4 GB` = the 32-bit process's pages, **copied from the guest OS's own top-level entries** (only top-level entries need copying since the lower levels are shared verbatim); then DBT RAM; then dynamic DBT mappings (e.g. guard pages for the return-address stack); then a **full identity map of guest RAM** so the DBT can walk guest page tables in software *for permission queries only* (notably to check execute permission before translating a page).
- On SMP, another vCPU can change the process's top-level page table while it is running under the DBT. Handled by **trapping the guest's TLB-invalidation instructions to EL2** and updating shadow top-level entries there — safe because the architecture *requires* a TLB flush for the change to be visible.

### 2.4 Code-cache consistency — a three-tier scheme

This is the most transferable part of the paper.

1. **ASID tagging.** Every translated fragment is tagged with the 16-bit **ASID** it came from, read out of the guest's system registers. Fragment lookup is keyed on (VA, ASID). Two processes with different code at the same VA coexist; a context switch causes **no** invalidation.
2. **TLB-invalidation tracking.** Any guest page-table change requires a TLB flush; all guest TLB flush instructions are trapped to EL2, and the hypervisor calls back into the DBT to invalidate affected fragments, then performs the flush on the guest's behalf. Because most TLB flushes are for *data* pages, they added a filter: when the translator first reads instructions from a page, it hypercalls to **register (VA, ASID) at page granularity**; the EL2 trap handler then does a **hash-table lookup** and skips the DBT callback entirely for flushes that touch no registered page.
3. **Stage-2 write protection for SMC.** Trapping I-cache maintenance was rejected for three concrete reasons: (a) ARMv8 gives EL2 **no way to perform an I-cache flush on behalf of an EL1/EL0 page-table regime**; (b) guests flush the whole I-cache often, which would nuke all fragments in all address spaces; (c) fragments can be sourced from *data* (MAMBO-X64's branch-table translation), which is modified with no I-cache flush at all. Instead, when a page is registered, **the hypervisor write-protects the underlying guest physical page in the stage-2 tables**. A guest write traps to EL2 → DBT invalidates → protection removed → page dropped from the watch set.

Fig. 6 in the paper is the data structure: fragments → (VA range, ASID) → guest physical pages, with invalidation propagating up from either level.

### 2.5 Numbers

Host: AppliedMicro X-Gene X-C1, 8 cores @ 2.4 GHz, Debian Unstable, Linux 4.6. Baseline = the **same AArch32 binaries executing natively** on the same chip (X-Gene still has AArch32 hardware).

| Benchmark | Native | MAMBO-X64 | HyperMAMBO-X64 |
|---|---|---|---|
| SPEC CPU2006 geomean | 1.00 | **1.010x faster than native** | **1.011x faster than native** |
| Integer µbench (1e9 increments), s | 2.92 | 2.92 | 2.92 |
| Syscall µbench (1e7 `getppid`), s | 1.00 | 8.39 | **10.63** |
| Page-fault µbench (2 GB, 4 kB touches), s | 1.94 | 1.96 | 1.96 |
| Signal µbench (1e6 SIGSEGV), s | 1.67 | 6.19 | **4.65** |

- Being **faster than native** is real and explained: denser AArch64 encodings collapsing AArch32 sequences, Dynamo-style trace formation, and X-Gene pipeline stalls in GCC's AArch32 output that vanish in the translated AArch64 code.
- Worst regressions vs MAMBO-X64: **up to 13%** on `gcc` and `cactusADM` (and less on `wrf`). Root cause is *indirect*: HyperMAMBO must catch page faults, and page faults **truncate NET trace recording**, producing many small traces.
- Syscalls are the clear weak point: **~10.6x** a native syscall, because every DBT entry/exit goes through EL2.
- MAMBO-X64 itself (TACO 2016) reports **~10% overhead vs native** on AArch32→AArch64, using hardware-assisted function returns (exploiting the hardware return-address predictor), branch-table inference, and fast atomic hash tables for indirect branches.

### 2.6 Implementation gap and limitations

- **There is no architectural way to trap an AArch64→AArch32 mode switch** on shipping ARMv8 parts. The prototype **patched the guest kernel**, replacing the `ERET`-into-AArch32 with an `HVC`. They argue an AArch64-only core would naturally raise an Illegal Mode exception. This is a real hardware-support gap, not an implementation detail.
- **Requires an AArch64 guest kernel.** It cannot run a 32-bit OS. Not a full-system translator.
- **Cannot see memory-mapped files**, so file-backed persistent code caching is unavailable; they propose hashing code contents instead, at a performance cost.
- **JIT-heavy guests suffer** from page-granularity write protection. Proposed fix (untested): the DynamoRIO **parallel-mapping / "mirror RAM"** trick — a second view of guest RAM in the guest physical space where writes to code pages are *not* trapped, plus instrumented writes with a hash-table check, so a JIT's own writes never trap to EL2.
- Comparison points the paper makes: **MagiXen** (IA-32 on Itanium under Xen, reusing IA-32 EL as its DBT) still pays page-table translation because the paravirtualised guest's page tables are still x86-format, and **x86 has no tagged TLB/ASIDs**, so every context switch flushes the TLB and therefore invalidates all translated code for the process. **PinOS** (Pin extended to whole-system under Xen) needs runtime checks for page-table modification and runs at **~50x slowdown**.

---


## 3. Older and adjacent: co-designed VMs, commercial transitions, hardware BT extensions

Sources for this section were gathered from the primary papers where retrievable; items flagged **[unverified]** could not be reached in full text.

### 3.1 Transmeta Crusoe / Efficeon — Code Morphing Software (CMS)

**J. Dehnert, B. Grant, J. Banning, R. Johnson, T. Kistler, A. Klaiber, J. Mattson. "The Transmeta Code Morphing Software: Using Speculation, Recovery, and Adaptive Retranslation to Address Real-Life Challenges." CGO 2003, pp. 15–24.** DOI [10.1109/CGO.2003.1191529](https://doi.org/10.1109/CGO.2003.1191529) · [PDF](https://safari.ethz.ch/digitaltechnik/spring2018/lib/exe/fetch.php?media=dehnert_transmeta_code_morphing_software.pdf)

x86 **full system** (including BIOS) on a proprietary VLIW. TM5800 issues 2 or 4 RISC "atoms" per "molecule" across 5 units; **64 GPRs + 32 FPRs**, enough that *every x86 architectural register is permanently bound to a native register*. Almost no hardware interlocks — CMS guarantees correctness by scheduling. The native ISA changed freely across generations with zero x86-visible impact.

**Hardware assists (the list worth memorising):**
1. **Shadowed register file + `commit`/`rollback`.** Every register holding x86 state exists twice (working + shadow). Atoms write only the working copy; `commit` at translation end copies working→shadow; any fault triggers `rollback`. Commit is "effectively free"; rollback "costs less than a couple of branch mispredictions."
2. **Gated store buffer.** Stores held until commit; dropped on rollback. Memory is checkpointed alongside registers.
3. **Alias-detection hardware.** The translator explicitly marks load/store atoms as protected/protecting; hardware verifies at runtime that reordered references did not overlap. Explicitly simpler than IA-64's ALAT — no fully-associative table, no hardware replacement policy, because *the translator names the entries*.
4. **I/O-reordering trap.** Each memory atom carries a "I was reordered w.r.t. x86 program order" bit; if such an atom touches an I/O-mapped page, hardware raises an exception. This is how MMIO stays correct without serialising all memory.
5. **Sub-page (fine-grain) write protection**, with a small hardware cache of fine-grain entries and a *software* miss handler.
6. A **"translated" bit** per page driving write-protect-on-translate (Klaiber, *The Technology Behind Crusoe Processors*, 2000).

**Precise x86 exceptions.** The scheduler hoists faulting atoms freely. On any native fault: **rollback → re-execute the region's x86 instructions in the interpreter**, which is precise at every boundary. If the interpreter does not reproduce the fault, it was a speculation artifact and is discarded. Interrupts use the same path, so translations never reason about interruptibility.

**Adaptive retranslation.** Interpret → count → translate regions of **up to 200 x86 instructions** as single-entry/multiple-exit traces; chain exits. Genuine recurring faults cause the region to be **narrowed around the faulting instruction**, ultimately to a *zero-instruction translation* that calls the interpreter for that one instruction. Policies **accumulate monotonically** on a translation to prevent oscillation between two translations that each fix one problem.

**Numbers (ablation — the most useful data in the paper):**

| Ablation | Cost |
|---|---|
| Suppress all memory reordering | boots ≤5% … **26.08%** (Windows ME boot), mean boots **10.09%**; apps mean **33.14%**, worst **97.29%** (048.ora) |
| Remove alias-detection hardware | mean boots **5.48%**, mean apps **23.53%**, worst **94.49%** (048.ora) |
| Remove fine-grain write protection | protection faults ×**46.8–59.4**; molecules/x86-instr ×**1.02–3.8** (Win98 boot 3.8×) |
| Self-revalidating translations (add) | Quake Demo2 **+28% frame rate** |
| Self-checking translations everywhere | **+83% code size**, **+51% dynamic molecules** → used selectively only |

**SMC handling.** Baseline: write-protect any x86 page from which code was translated. Refinements: fine-grain protection; **self-revalidating translations** (a removable prologue re-verifies the x86 bytes and re-arms protection); **self-checking translations** (checks inlined, and the check loads may be reordered past stores using the alias hardware); **stylized SMC** (an `add %eax,0x123456` whose immediate is patched in a loop is translated to `ld %temp,[label+1]; add %eax,temp` — the immediate is *loaded from the code stream*); and **translation groups** (LRU list of alternate translations of the same x86 region — the Windows 9x device-independent BLT driver alone needed **up to 33 versions**). DMA writes invalidate too.

**Limitations.** No escape valve: cannot punt to native, cannot refuse code, cannot wait for a recompile. Translator cost is itself a significant fraction of runtime. Absolute performance never matched contemporary hardware x86; the value proposition was power.

### 3.2 IBM DAISY and BOA

**K. Ebcioğlu, E. R. Altman. "DAISY: Dynamic Compilation for 100% Architectural Compatibility." ISCA-24 (1997), pp. 26–37.** DOI [10.1145/264107.264126](https://doi.org/10.1145/264107.264126) · [PDF](https://people.eecs.berkeley.edu/~kubitron/courses/cs252-S09/handouts/papers/daisy.pdf) · [ISCA@50 retrospective](https://bpb-us-w2.wpmucdn.com/sites.coecis.cornell.edu/dist/7/587/files/2023/06/Ebcioglu_1997_DAISY.pdf)

PowerPC (also S/390, x86 discussed) on an 8-issue **tree VLIW**. **Full system: a VMM in ROM translates all code including the OS kernel, at page granularity.**

**The memory-map idea — the most transferable thing in the paper.** VLIW virtual memory is split three ways: a **low region** identity-mapped to guest physical memory; a **middle region** holding the VMM (identity-mapped ROM + scratch); and a **top region** at `VLIW_BASE` (e.g. `0x80000000`) holding translated code, where **the translation of guest physical page `n` lives at virtual address `n × N + VLIW_BASE`** with `N = 4`. A cross-page guest branch to guest physical `n` is *literally* a VLIW branch to `n×4 + VLIW_BASE`; if unmapped, a **"VLIW translation missing" exception** invokes the VMM, which translates the 4 KB guest page, maps it (from an LRU pool of frames at the top of real memory), and resumes. An alternative design uses a hash table guest-physical → translated-real, dropping the fixed 4× expansion at a speed cost.

Because addressing derives arithmetically from the **guest physical address**, self-referential code, PC-relative branches, checksumming code, and embedded FP constants all work transparently, and **all translated memory is invisible to the guest OS**. Note this is the same decision Captive made 20 years later (physical-address-indexed code cache) taken to its logical extreme.

**Hardware assists.** Per-register **exception tag bit** (speculative op that faults sets the tag rather than trapping; tag propagates; the trap fires when a non-speculative/commit op consumes it — so loads hoist arbitrarily, and the extra state is invisible to the guest so needs no context-switch save/restore). Load-above-store support with retranslate-on-alias. **Speculative MMIO loads become no-ops with the tag set** and are re-executed non-speculatively at commit (same idea as Crusoe's I/O-reorder trap). **Fine-grain code-modification interrupt**: any write by CPU or I/O to a translated memory unit raises a *precise* exception to the VMM. Precise exceptions recovered by **backward scan / state repair** rather than commit-rollback.

**Numbers.** Pathlength reduction (≈ infinite-cache ILP) on SPECint95 at 8-issue: **mean 2.5×**. **Code expansion 4×** (16 KB per translated 4 KB page). **Translation cost 4,315 RS/6000 instructions per PowerPC instruction** — vs ~100,000 for a static VLIW compiler and ~65,000 for gcc, while landing **within 20%** of the static compiler's quality. Amortisation analysis: at 4,000 instr/instr and 1,000 unique code pages a 2 s program costs **+14%**; at 10,000 pages, **+707%**; SPEC95 mean reuse factor is **>500,000**, so amortisation is easy in practice and brutal for short-lived/cold-heavy workloads.

**Limitations.** Numbers come from a compiled-simulation harness in which **kernel routines were called natively, not simulated**; no cycle-accurate model; 4× bloat; cross-page branches expensive; never shipped.

**BOA** (Altman, Gschwind et al., IBM RC 21665, 2000; "BOA: Targeting Multi-Gigahertz with Binary Translation," 1999) inverts DAISY's priority: **maximise frequency, accept higher CPI**, on the thesis that a translation layer buys you a deeply-pipelined simple core clocked far above an OoO PowerPC. [Semantic Scholar](https://www.semanticscholar.org/paper/4aece4b112282fc267e8b963e91eeb4f803718fe) · [CiteSeerX](https://citeseerx.ist.psu.edu/viewdoc/summary?doi=10.1.1.153.1828) **[unverified — full text not retrieved]**

### 3.3 HP Dynamo — the same-ISA trace baseline

**V. Bala, E. Duesterwald, S. Banerjia. "Dynamo: A Transparent Dynamic Optimization System." PLDI 2000, pp. 1–12.** DOI [10.1145/349299.349303](https://doi.org/10.1145/349299.349303) · [PDF](https://cseweb.ucsd.edu/classes/sp00/cse231/dynamopldi.pdf)

PA-8000 on PA-8000, HP-UX 10.20, **no hardware, OS, or compiler support at all**. Interpret until a taken branch; count only *selected* start-of-trace addresses (backward-branch targets and fragment-cache exits); past threshold switch to **MRET (Most Recently Executed Tail)** — speculatively take the next interpreted sequence as the hot trace, with **no path profiling**. Emit into a 150 KB **fragment cache**, flushed when the trace-creation rate spikes.

**Numbers.** `+O2` binaries under Dynamo vs standalone: **li and m88ksim >22%, perl ~18%, compress ~14%, average ~9%**; average `+O2` under Dynamo ≈ standalone `-O4`. **Fragment optimisation contributes only ~3%** — *most of the win is trace selection itself* (implied partial inlining and code layout). Steady-state overhead **<1.5%**. Interpretation alone is **~40× slower** than native.

**The crucial asymmetry.** Dynamo has a **bailout**: on `go` and `vortex` it gives up and returns to native execution. A cross-ISA system has no such escape hatch — exactly the point the CMS paper makes about Dynamo.

### 3.4 Intel IA-32 Execution Layer

**L. Baraz, T. Devor, O. Etzion, S. Goldenberg, A. Skaletsky, Y. Wang, Y. Zemach. "IA-32 Execution Layer: A Two-Phase Dynamic Translator Designed to Support IA-32 Applications on Itanium-Based Systems." MICRO-36 (2003), pp. 191–201.** DOI [10.1109/MICRO.2003.1253195](https://doi.org/10.1109/MICRO.2003.1253195)

IA-32 **user mode** on Itanium. Two-phase: fast basic-block translation with inserted profiling, then **superblock** optimisation of hot code hot-swapped in place; a later revision added an interpreter as phase 0 so cold code is never translated. **A single translator body serves Windows and Linux** behind a thin OS-abstraction layer.

**Numbers.** Profiling/detection instrumentation costs only **2–4%** of execution time. Performance **comparable to a 1.5 GHz Xeon MP** on many applications, and comfortably beat Itanium's *hardware* IA-32 decoder — which Intel consequently **deleted from Montecito (2006)**. This is the strongest historical evidence that a good software translator beats a bolted-on hardware decoder.

**No special hardware assists** (it used generic Itanium features: predication, control/data speculation via NaT bits and the ALAT, rotating registers). Hard problems the paper names: precise exceptions, x87/MMX/SSE emulation (80-bit extended precision), misalignment.

### 3.5 DEC FX!32

**A. Chernoff et al. "FX!32: A Profile-Directed Binary Translator." IEEE Micro 18(2), 1998, pp. 56–64.** DOI [10.1109/40.671403](https://doi.org/10.1109/40.671403) · also R. Hookway, M. Herdeg, *Digital Technical Journal* 9(1), 1997 · [USENIX NT'97](https://www.usenix.org/conference/nt-97/digital-fx32-running-32-bit-x86-applications-alpha-nt)

x86 Win32 on Alpha/Windows NT 4.0. **The hybrid:** first run interprets while recording a **profile** (hot procedure entries, indirect-call targets); between runs a **background static translator** emits **native Alpha DLLs** transparently substituted for hot procedures; untranslated code falls back to the emulator; the profile database accumulates across runs, so the application gets faster the more it is used. Win32 API calls route to native Alpha system DLLs — only application code is ever emulated.

**Numbers.** ~**40–50% of native x86** speed on comparable hardware (stated goal ~70%). **Limitations:** explicitly *not* faithful — **64-bit rather than 80-bit floating point**, and no support for the NT Debug API because it cannot rematerialise precise x86 state at arbitrary points. Requires offline passes and persistent per-application state; no system code.

**Relevance:** FX!32 is the direct ancestor of Rosetta 2's persistent AOT cache — translate once, store to disk, reuse forever — minus the profile-directed part.

### 3.6 Apple's three transitions

**68k → PowerPC (1994).** Original interpreter by **Gary Davidian**, begun mid-1990 and first targeted at the **Motorola 88000** before Apple pivoted to PowerPC; shipped in ROM. Later replaced by **Eric Traut**'s dynamic recompiler. The architectural trick: classic Mac OS APIs were invoked via **68k A-line traps**, so trap targets could be swapped for native PowerPC implementations — **only application code was emulated; the OS ran native**. **Universal Procedure Pointers (UPPs)** encoded per-routine ISA for the Mixed Mode Manager. Scope: the **user subset of the 68EC040 — no MMU, no 68881 FPU**. Davidian also wrote the Power Macintosh **NanoKernel** beneath Mac OS that handled traps and mode switching. [Wikipedia](https://en.wikipedia.org/wiki/Mac_68k_emulator) · [CHM interview](https://computerhistory.org/blog/transplanting-the-macs-central-processor-gary-davidian-and-his-68000-emulator/)

**PowerPC → x86: Rosetta 1 (2006–2011).** Licensed **Transitive QuickTransit**. User-level only; **could not translate G5-specific instructions or AltiVec**; no kernel extensions; Apple recommended it only for interaction-heavy, compute-light apps. Removed in 10.7. [Wikipedia](https://en.wikipedia.org/wiki/Rosetta_(software))

**Rosetta 2 (2020– ) — the most important comparison point.**

*Mechanism.* **AOT-first**: on first execution the entire `__TEXT` segment of an x86-64 Mach-O is translated to ARM64 and written to disk under `/var/db/oah/`. Each x86 instruction is translated **exactly once** and never re-emitted on branching, preserving code locality and I-cache behaviour. A **JIT fallback** handles only indirect jumps whose targets are missing from lookup tables (function starts + unreferenced basic blocks are pre-populated). Measured expansion for an `sqlite3` binary: **1.05 MB x86 → 1.72 MB ARM64 ≈ 1.64×**.

*Hardware assists in Apple Silicon* — the crux of this whole document:
1. **TSO mode.** A non-standard **total-store-ordering mode** toggled per-thread by XNU via an `ACTLR_EL1`-family system-register bit on kernel exit for Rosetta threads. When enabled, plain ARM loads/stores acquire x86 ordering — eliminating the fences that otherwise dominate x86-on-ARM cost. The same idea shipped on **NVIDIA Denver/Carmel** and **Fujitsu A64FX**. Third-party toggle: [TSOEnabler](https://github.com/saagarjha/TSOEnabler) (notes it also **pins the thread to P-cores** and must be enabled for *all* threads of a process).
2. **Undocumented PF/AF flag extension.** ADD/SUB/CMP compute x86's **parity flag and adjust flag** into **NZCV bits 26–27**. These two are the classic worst case in software x86 emulation (PF is a byte popcount parity).
3. **FEAT_FlagM / FlagM2**: `CFINV` (x86 SBB/borrow), `RMIF` (rotate bits into flag positions — x86 shift/rotate flag semantics), `SETF8`/`SETF16` (8/16-bit operand flag results), `AXFLAG`/`XAFLAG` (ARM↔x86 FP condition-flag encodings). *Confirmed present on M2 Max via `sysctl` — see §7.0.*
4. **Alternate floating-point mode** predating/analogous to FEAT_AFP: x86 NaN propagation, x86 tininess-detection timing, x86 denormal/FTZ behaviour, selectable in FPCR. *(Note: `hw.optional.arm.FEAT_AFP = 0` on M2 Max, so the standard architectural feature is absent; the Apple-private mode is what Rosetta uses.)*
5. **16 x86-64 GPRs into 31 ARM64 GPRs**, so no spilling of guest architectural state — the same structural property Crusoe engineered deliberately with 64 GPRs.

*Software-side:* dead-flag elimination; prologue/epilogue pairing into `LDP`/`STP` with delayed SP updates (mirroring the x86 stack engine; single micro-ops on M1); SSE2 → optimised NEON (not scalar fallback); CALL/RET → `BL`/`RET` with validation bookkeeping so the **hardware return-address stack predicts correctly**. Primary analysis: [dougallj, "Why is Rosetta 2 fast?"](https://dougallj.wordpress.com/2022/11/09/why-is-rosetta-2-fast/) · [Apple security doc](https://support.apple.com/guide/security/rosetta-2-on-a-mac-with-apple-silicon-secebb113be1/web)

*Rosetta 2 in Linux VMs (macOS 13+).* The host exposes `/Library/Apple/usr/libexec/oah/RosettaLinux` into an ARM64 Linux guest through a **virtiofs share tagged `rosetta`** (`VZLinuxRosettaDirectoryShare`). Guest: `mount -t virtiofs rosetta /media/rosetta`, then register with **`binfmt_misc`** so x86-64 ELF headers dispatch to `/media/rosetta/rosetta`. The guest still supplies x86-64 shared libraries. Rosetta translates **x86-64 Linux syscalls to ARM64 Linux syscalls**, rewrites `uname` to report `x86_64`, and intercepts `/proc/cpuinfo` to return a synthetic "VirtualApple @ 2.50GHz". AOT caching is opt-in via `rosettad daemon <cachepath>` (default `$HOME/.cache/rosettad`), which must run **in the same mount namespace** and be executed **directly from the virtiofs mount**; the runtime finds it over a Unix socket at `/run/rosettad/rosetta.sock`, producing `*.aotcache` files. [Apple docs](https://developer.apple.com/documentation/virtualization/accelerating-the-performance-of-rosetta) · [internals writeup](https://blog.inoki.cc/2026/02/28/Apple-Rosetta-Linux-VM-Secret-en/) · [UTM docs](https://docs.getutm.app/advanced/rosetta/)

*Limitations.* **User-mode only in both host and VM modes — no x86 kernel, no x86 ring 0, no x86 page tables.** No 32-bit x86, no kernel extensions, no x86 hypervisors; AVX/AVX2 only in very recent revisions. Apple has announced substantial removal in macOS 28 (2027). **The gap this project targets — full-system x86-64 under Hypervisor.framework — is precisely what Rosetta 2 does not do, while sitting on hardware built for it.**

### 3.7 NVIDIA Denver / Carmel

**D. Boggs, G. Brown, N. Tuck, K. S. Venkatraman. "Denver: NVIDIA's First 64-bit ARM Processor." IEEE Micro 35(2), 2015, pp. 46–55.** DOI [10.1109/MM.2015.12](https://doi.org/10.1109/MM.2015.12)

ARMv8-A guest on a proprietary VLIW-ish internal ISA — **same-family, not cross-ISA**. (Denver was **originally an x86 project**, abandoned over Intel patent licensing.)

**Dual path**: a **hardware ARM decoder** executes cold code directly (dual-issue in-order) while a software **Dynamic Code Optimizer** running *below the OS* translates hot regions to the native 7-wide (Denver) / 10-wide (Carmel) format. **128 MB "optimization cache" carved out of main memory and hidden from the OS** — Smith & Nair's *concealed memory*, exactly as in Crusoe and DAISY. The DCO does register renaming, loop unrolling, load hoisting and redundancy elimination — statically doing what an OoO machine does dynamically, which is why the core can be in-order. L1 is **128 KB I** + 64 KB D per core; the unusually large I-cache is a direct consequence of translated-code expansion. **Hardware TSO support** is present (same family as Apple Silicon and A64FX).

**Limitations.** Warm-up sensitivity (benchmark-friendly, less so on short-running or branch-chaotic code); the 128 MB carve-out is a real memory tax on a mobile part; never shipped a cross-ISA capability.

### 3.8 LoongArch LBT (Loongson Binary Translation extension) and LATX

LoongArch LA64 host; x86-64 / ARM / MIPS guests. Linux support merged in **6.6**.

**Hardware assists — the clearest modern statement of "which x86 state belongs in hardware":**
- **4 scratch registers `scr0`–`scr3`** — spill space for translator temporaries surviving across translated blocks without touching memory.
- **An `eflags` register** implementing **x86/ARM condition-flag semantics natively**, with three separately-detectable sub-features (**LBT_X86, LBT_ARM, LBT_MIPS**), so the translator does not synthesise x86 flags from LoongArch comparisons.
- **`ftop`** — the **x87 floating-point stack top-of-stack pointer in hardware**, removing the software TOS bookkeeping that makes x87 emulation painful.
- LBT state is saved/restored on vCPU context switch by KVM with a **"late enabling"** scheme mirroring the FPU, and exposed via get/set ioctls for VM migration.
[LWN: LoongArch KVM LBT](https://lwn.net/Articles/979754/) · [Phoronix: LBT in Linux 6.6](https://www.phoronix.com/news/LoongArch-LBT-Linux-6.6)

**LATX / LAT** ("Loongson Architecture Translator for x86") is **user-level**, built on heavily-modified **QEMU 6**, using **LSX/LASX vector extensions** and **LBT_X86**, plus an **AOT mode** and **runtime library pass-through** (host-native libc/libGL substitution, borrowed from Box64). Coverage: x87, MMX, SSE–SSE4.2, POPCNT, AES, PCLMULQDQ, CMPXCHG16B; AVX/AVX2/FMA/F16C/BMI experimental. Limitations: user-space only, requires LSX+LBT_X86, no 16-bit real mode, library pass-through 64-bit only, SMC invalidated per-page by default. A **LATX-SYS** full-system effort and a **COGBT** LLVM-IR-based translator (fixed register mapping, Flag Elimination Pass, profile-guided translation-unit formation) also exist. [LATX README](https://github.com/lat-opensource/lat/blob/master/README.en.rst) · [LoongsonLab BT research](https://loongsonlab.github.io/en/research/binarytranslation)

**Numbers.** LATX's own documentation makes only qualitative claims. Quantitative comparison appears in *An Instruction Inflation Analyzing Framework for Dynamic Binary Translators*, ACM TACO, DOI [10.1145/3640813](https://doi.org/10.1145/3640813), which measures instruction inflation across QEMU, LATX, and Rosetta 2 — notably that LATX reaches near-1:1 for stack operations by **lazily updating SP** despite LoongArch lacking push/pop. **[unverified — paywalled; do not cite specific inflation ratios without checking]**

**Design lesson.** LBT says the three pieces of x86 state worth putting in hardware are **flags, an FP stack pointer, and scratch registers**. Apple Silicon independently arrived at two of the three (flags via PF/AF + FlagM2; scratch via register abundance) and skipped the x87 TOS.

### 3.9 Elbrus (MCST) / Lintel, and Zhaoxin

**Elbrus 2000 (E2K)** is MCST's VLIW ISA. **Lintel** is a **VM-level, full-system dynamic binary translator** giving system-level IA-32/x86-64 compatibility on the Elbrus VLIW — explicitly described as "similar to Transmeta Crusoe". It boots **unmodified x86 operating systems** (Linux, MS-DOS, Windows 95/2000/XP/7, QNX) with on-the-fly **adaptive multi-pass optimisation** "combined with binary-translation support hardware embedded in the Elbrus architecture". The Elbrus ISA's BT-serving features are the same family as DAISY's: explicit speculative execution with per-value **diagnostic tags**, predication, tagged memory, wide rotating register file, hardware support for precise-state recovery. MCST's stated design goal (2015) is **~20% overhead** for x86/x86-64 BT. [Wikipedia: Elbrus 2000](https://en.wikipedia.org/wiki/Elbrus_2000) · [E2K Technology and Implementation](https://www.researchgate.net/publication/220769330_E2K_Technology_and_Implementation)

**[unverified]** Nearly all primary Lintel literature is Russian-language (MCST, Ermolovich). The "20% overhead" is a *stated goal*, not an independently measured result.

**Zhaoxin** ships **native x86-64** silicon (VIA/Centaur lineage), not a binary-translation host. No evidence of a Zhaoxin cross-ISA BT product was found; it does not belong in this taxonomy.

### 3.10 The framing literature: co-designed virtual machines

**J. E. Smith, R. Nair. *Virtual Machines: Versatile Platforms for Systems and Processes*. Morgan Kaufmann, 2005. ISBN 1558609105.** [Ch. 1 preprint](https://courses.cs.vt.edu/~cs5204/fall07-kafura/Papers/Virtualization/Intro-To-VM-BookChapter-Smith) · [ScienceDirect](https://www.sciencedirect.com/book/9781558609105/virtual-machines)

The defining concept is **concealed memory**: the VM software occupies "a region of memory that is not visible to any application or system software … carved out of real memory at boot time," and the guest is **never informed of its existence**. Every system in this section implements it — Crusoe's translation cache, DAISY's top-region translated-code area, Denver's 128 MB optimisation cache, Lintel's resident footprint. **On Apple Silicon the analogue is trivially available: guest physical memory the VMM simply never advertises to the guest firmware/E820 map.** The taxonomy separates *process VMs* (FX!32, IA-32 EL, Rosetta) from *system VMs* (Crusoe, DAISY, Lintel) from *co-designed VMs* (host ISA designed jointly with the translator and not otherwise exposed). Smith's Wisconsin group produced follow-on work on efficient co-designed x86 VMs with a fused-microop internal ISA (~2005–2007): [jes.ece.wisc.edu/upc06.html](https://jes.ece.wisc.edu/upc06.html)

### 3.11 Three conclusions from the historical record

1. **Every successful full-system co-designed VM used exactly three hardware primitives**: (i) checkpoint/rollback of architectural state so the translator can reorder freely and recover to a precise guest boundary via an interpreter; (ii) a memory-reordering *detector* (alias hardware, ALAT, exception tags) rather than a memory-ordering *restriction*; (iii) write protection finer than a page, for SMC and mixed code/data. Apple Silicon under `Hypervisor.framework` gives **(iii) via stage-2 permissions** and, uniquely, **hardware TSO**, which obviates much of (ii). It gives **nothing for (i)** — recovery must be software (translation-boundary checkpointing + interpreter re-execution), i.e. the CMS design minus the free commit.
2. **Apple Silicon already has the x86 semantic assists** LBT put in hardware. PF/AF in NZCV[27:26], FlagM2, alternate-FP mode and TSO are collectively the difference between Rosetta 2's ~1.64× code expansion and QEMU TCG's ~5–10×. **The open question is whether those assists remain usable from EL1/EL0 guest context inside a `Hypervisor.framework` VM**, since TSO is toggled by XNU on kernel exit for Rosetta threads specifically.
3. **CMS's adaptive-retranslation policy machinery is the most transferable software artifact.** Region-narrowing around genuine faults down to zero-instruction translations; monotonically accumulating conservative policies to prevent oscillation; translation groups for stylized SMC; immediates loaded from the code stream. All are pure software, and all address failure modes (Windows 9x BLT drivers with 33 code versions, self-patching immediates, BIOS mixed code/data) that a **full-system** x86-64 emulator will hit and that a **user-mode** translator like Rosetta 2 never sees.


## 4. QEMU/KVM hybrids, LLVM-based DBT, and userspace x86→ARM64 translators

### 4.1 Embedded / Hardware-assisted Shadow Page Tables (ESPT / HSPT)

**C.-J. Chang, J.-J. Wu, W.-C. Hsu, P. Liu, P.-C. Yew. *Efficient Memory Virtualization for Cross-ISA System Mode Emulation*. VEE 2014.** DOI [10.1145/2576195.2576201](https://doi.org/10.1145/2576195.2576201). Guest ARM → host x86-64.

- Establishes the motivating number: **memory-subsystem emulation is ~30–40% of total execution time in QEMU system mode** (corroborating Tong's 38.1%, §6.1).
- Mechanism: classic shadow page tables force a *page-table switch* (CR3 swap) between DBT context and guest context. ESPT instead **embeds the shadow page table into the emulator process's own host page table**, so no switch is needed; a kernel module installs the entries. A translated load/store becomes **a single host instruction on the guest virtual address**.
- **1.51× speedup on SPEC CINT2006**; the "Private SPT" variant reaches **1.94×** over the Android Emulator baseline that Captive also used (Captive: **2.05×**).
- Limitation vs Captive: still confined to a user process, so it needs a kernel module and **cannot use host privilege levels to enforce guest U/S protection**. That gap is precisely why Captive dominates it.
- **Applicability:** this is the design to fall back to if a hardware VM turns out to be unusable. It is also the honest "how much does the hardware VM buy you over a clever `mmap` scheme" measurement: about 2.05/1.94 ≈ **6%** on that particular baseline, though Captive wins far more on I/O-heavy work.

### 4.2 Cota et al. — the softmmu cost breakdown and MTTCG

**E. G. Cota, P. Bonzini, A. Bennée, L. P. Carloni. *Cross-ISA Machine Emulation for Multicores*. CGO 2017.** [PDF](http://www.cs.columbia.edu/~luca/research/cota_CGO17.pdf). Prototype "Pico"; hosts Skylake i7-6700, POWER8, Opteron; guests x86-64 and AArch64.
- **Pico-user is 20–90% faster than QEMU-user** (better TB hashing, QHT cache efficiency). **Pico-*system* is only ≈ QEMU-system single-threaded** — the added locks and fences needed for correctness cancel the hashing win. A sobering data point about how much of full-system cost is *not* in the translator.
- Scales to **64 threads** on PostgreSQL/pgbench and Masstree, "in line with KVM's". On PARSEC it sometimes scales *better than native* because DBT slowdown delays cache-contention cliffs. Much of this landed upstream as QEMU **MTTCG**.
- Atomics: fully-correct LL/SC-via-CAS "incurs significant slowdown"; their store-instrumentation approach gives **~2× average slowdown for CINT2006**, with Pico-ST adding only **4%** over the bitmap baseline.

**E. G. Cota, L. P. Carloni. *Cross-ISA Machine Instrumentation using Fast and Scalable Dynamic Binary Translation*. VEE 2019.** DOI [10.1145/3313808.3313811](https://doi.org/10.1145/3313808.3313811) · [PDF](https://braap.org/~cota/pubs/cota_vee19.pdf). Tool is **Qelt**. *(Note: "Pico" was the CGO'17 prototype name; there is no relation to Microsoft's "picoprocess"/Drawbridge.)*
- **1.76× (INT) / 2.18× (FP) over QEMU** as a full-system cross-ISA emulator; **1.5×–3×** over prior cross-ISA full-system instrumentation tools; matches **Pin** for cache-simulation instrumentation.
- **The best published breakdown of softmmu cost**, in the paper's own priority order:
  1. Non-compulsory TLB misses cause guest page faults taking **hundreds of host instructions**.
  2. Even on a *hit*, each guest memory access expands to several host instructions to index and compare the TLB.
  3. Flush cost — "it is for this reason that QEMU has a small, static TLB size."
- Mitigations: dynamic TLB resizing on a **100 ms history window** with 70%/30% up/down thresholds (fixing the oscillation in Tong et al.'s policy); host-FPU use guarded by flag checks; per-vCPU indirect-branch caching.
- **Applicability:** this quantifies exactly what the Captive/`Hypervisor.framework` approach *deletes*. Items (1) and (2) go to **zero** with a hardware MMU; item (3) is replaced by ARM `TLBI` cost.

**QEMU MTTCG** ([design doc](https://www.qemu.org/docs/master/devel/multi-thread-tcg.html)): per-vCPU TCG context and region (no lock during translation), single shared code buffer, `tlb_flush_*_all_cpus_synced` for cross-vCPU flushes via `async_safe_run_on_cpu()`, `tcg_gen_mb()` barriers merged to the strongest form. **MTTCG is enabled by default only when host and guest memory orders are compatible** (`TCGCPUOps::guest_default_memory_order`) — i.e. **x86-on-ARM is precisely the disfavoured case**, which is the upstream project's own admission of the problem in §6.6.

### 4.3 HQEMU

**D.-Y. Hong, C.-C. Hsu, P.-C. Yew, J.-J. Wu, W.-C. Hsu, P. Liu, C.-M. Wang, Y.-C. Chung. *HQEMU: A Multi-Threaded and Retargetable Dynamic Binary Translator on Multicores*. CGO 2012.** DOI [10.1145/2259016.2259030](https://doi.org/10.1145/2259016.2259030) · [source](https://github.com/sailfish009/hqemu)

QEMU **TCG as the fast-path translator on the execution thread**, plus an **LLVM optimiser running on separate threads/cores** that re-optimises hot regions (hot traces found via hardware performance counters, then merged). Optimisation cost is *offloaded* rather than amortised. Supports process-level *and* full-system; the LLVM optimiser can run in-process or as a standalone optimisation server.

**2.4× (SPEC CPU2006 INT) and 4× (FP) over QEMU.** Limitations: requires a *modified* LLVM, pinning it to LLVM 6.0 and an old QEMU; inherits TCG IR's limitations.

**Applicability:** the two-tier "fast translator on the hot path, heavy optimiser on another core" structure is directly reusable and is the obvious answer to Captive's admitted **2.6× slower JIT**. Captive explicitly could not do this because its unikernel has no threads and cannot host LLVM (§1); on Apple Silicon the optimiser could live in the *host process* and communicate through the same shared-memory channel used for devices.

### 4.4 Instrew / Rellume — LLVM-based lifting done properly

**A. Engelke, M. Schulz. *Instrew: Leveraging LLVM for High Performance Dynamic Binary Instrumentation*. VEE 2020.** DOI [10.1145/3381052.3381319](https://doi.org/10.1145/3381052.3381319) · [PDF](https://home.cit.tum.de/~engelke/pubs/2003-vee.pdf). Lifts machine code **directly to LLVM IR** (via **Rellume**, [github.com/aengelke/rellume](https://github.com/aengelke/rellume)) rather than through a proprietary IR, and isolates the instrumenter in a separate process. Register handling uses **"facets"** — multiple typed views of each architectural register with a mandatory native facet. Average rewriting overhead **72%** vs **Valgrind's 367%**.

**A. Engelke et al. *Efficient LLVM-Based Dynamic Binary Translation*. VEE 2021.** DOI [10.1145/3453933.3454022](https://doi.org/10.1145/3453933.3454022) · [PDF](https://home.cit.tum.de/~engelke/pubs/2104-vee.pdf). SPEC CPU2017, single-threaded:

| System | Overhead vs native |
|---|---|
| **Instrew** | **53%** (67% INT, 40% FP); best case **7%** |
| HQEMU 2.5.2 | 124% (136% INT, 114% FP) |
| QEMU 5.2.0 | **716%** (275% INT; FP **7.7×–38×**, geomean **16.4×**) |

Uses a specialised calling convention to exploit LLVM's register allocator, and **forwards guest FS/GS to the native x86-64 segment registers** — the trick whose ARM64 analogue is `TPIDR_EL0`/`TPIDRRO_EL0`.

**Limitation for this project:** user-mode only, and the fastest configurations assume same-ISA or near-ISA hosts.

### 4.5 Memory-model-correct x86→ARM64: Risotto, Lasagne, Arancini

**Risotto** (ASPLOS 2023) — see §6.6 for the numbers. Beyond the fence measurement, its contributions are: a **formalisation of QEMU's TCG IR memory model**, then *proved* mapping schemes for x86→TCG and TCG→ARM. It finds QEMU **both over-fences** (costing performance) **and has incorrect fence usages that cause run-time errors**. Uses **LDAPR/STLR (FEAT_LRCPC)** for efficient x86 acquire/release. Also reports **a defect in the Arm-Cats model** that forces extra fences, with a proposed revision.

**Lasagne** — N. Sprokholt, S. Chakraborty et al. PLDI 2022. DOI [10.1145/3519939.3523719](https://doi.org/10.1145/3519939.3523719). *Static* x86→ARM64 with a verified memory-model mapping. Reduces fences by **up to ~65%, 45.5% average**. Limitation: static-only coverage — Arancini shows it **cannot lift the Phoenix map-reduce benchmarks at all** (jump tables, function pointers).

**Arancini** — Reimers, Sprokholt, Fink, Augoustis, Kammermeier, Rocha, **Spink**, Gouicem, Chakraborty, Bhatotia. ASPLOS 2026. DOI [10.1145/3779212.3790127](https://doi.org/10.1145/3779212.3790127) · [PDF](https://www.st.ewi.tudelft.nl/sschakraborty/papers/ASPLOS2026-Arancini.pdf). **Note Tom Spink (Captive) is a co-author** — this is the current state of the art from the same lineage. x86-64 → ARM and RISC-V.
- **ArancinIR**: one IR shared by a static AOT translator and a dynamic fallback ("hybrid binary translation"). Formalised memory model with **formally verified** x86-64→ARM and →RISC-V mappings. **First translator to handle mixed-size accesses for ARM targets.** Calls to native host shared libraries are forwarded rather than translated.
- Numbers (Phoenix, 16 threads, ThunderX2 / SOPHON SG2042, geomean vs native): **8.01× slower (ARM)**, **4.52× slower (RISC-V)**; with native library linking, **6.01×** and **3.81×**. **Up to 5× faster than Risotto/QEMU-based translators.** Only **≤2.1% of unique instructions** are translated dynamically. Ablation: **dead-flag elimination gives little** (LLVM's DCE already does it); **fence merging is worth up to ~1.4×**.
- Limitations: user-mode, not full-system; still 4–8× native in absolute terms; the static phase needs the binary ahead of time.
- **Applicability:** the AOT-primary/JIT-fallback split (≤2.1% dynamic) is the Rosetta 2 shape, independently validated, and is very likely the right shape for guest *userspace* here. It does **not** apply to a guest kernel, which is discovered at runtime.

### 4.6 Box64 and FEX-Emu — the practical x86-64→ARM64 state of the art

Benchmarks from [box86.org (March 2022)](https://box86.org/2022/03/box86-box64-vs-qemu-vs-fex-vs-rosetta2/), RPi 400 @ 1.8 GHz, Ubuntu 21.10 arm64:

| Benchmark | Native aarch64 | Box64 | FEX x86_64 | QEMU x86_64 |
|---|---|---|---|---|
| 7-Zip | 5787 | **3084 (53%)** | 1486 (26%) | 931 (16%) |
| dav1d 1T | 185.57 | 49.72 (27%) | 27.28 (15%) | 11.24 (6%) |
| dav1d 4T | 312.23 | 116.64 | 52.67 | 15.65 |
| GLMark2 | 181 | 178 | 164 | 4 |

On an **M1 MacBook Pro**, 7-Zip: macOS native 47,239 → **Rosetta 2 = 33,746 (71%)**; Linux native 43,271 → **Box64 = 24,746 (57%)**.

Box64's edge over QEMU comes partly from **wrapped native libraries** (calling native ARM64 `.so` instead of translating x86 libs), but [box64 issue #107](https://github.com/ptitSeb/box64/issues/107) notes it is **"at least 4× faster than qemu"** even with little or no wrapping. **This trick is unavailable inside a guest kernel** and only partially available for guest userspace in a full-system emulator.

**FEX-Emu** ([fex-emu.com](https://fex-emu.com/) · [GitHub](https://github.com/FEX-Emu/FEX)): custom IR, AVX/AVX2, syscall translation layer, thunks forwarding OpenGL/Vulkan to host libraries.
- **TSO is the dominant cost.** [FEX-2404](https://fex-emu.com/FEX-2404/): emulating x86's memory model can be **"near a 10× performance hit,"** worst for vector code. With TSO emulation disabled, **memcpy/memset go from 2–3 GB/s to 88 GB/s (~30–40×)**. With FEAT_LRCPC/LRCPC2 they recommend TSO on but Vector-TSO and Memcpy-TSO off.
- Flags: [FEX-2306](https://fex-emu.com/FEX-2306/) cut EFLAGS *unpacking* from four instructions per flag bit to two. And critically: **Apple M1's undocumented extension has ADDS/SUBS/CMP compute x86 PF and AF into NZCV bits 26 and 27** — accurate x86 flag emulation at zero cost.

### 4.7 Apple's TSO bit — now documented

**Apple's own developer documentation** ([Accelerating the performance of Rosetta](https://developer.apple.com/documentation/virtualization/accelerating-the-performance-of-rosetta)) names the bit: **`ACTLR.TSOEN`**. Setting it to 1 switches the CPU to the **x86-64 TSO memory model**; clearing it restores standard ARM64 ordering. Apple ships **Linux kernel patches** ([RosettaPatch.zip](https://developer.apple.com/download/files/RosettaPatch.zip), applying cleanly to Linux 6.10) that context-switch `ACTLR.TSOEN` per thread and expose `prctl(PR_SET_MEM_MODEL, PR_SET_MEM_MODEL_TSO)`. The upstream equivalent is Hector Martin's series: [LWN, "arm64: Support the TSO memory model"](https://lwn.net/Articles/969484/) (April 2024).

**This is the most important single finding in this document for feasibility.** Apple documents `ACTLR.TSOEN` *for guests inside a VM* — the Rosetta-for-Linux path is exactly "an ARM64 guest kernel inside `Virtualization.framework` toggling TSOEN per thread." So the bit is reachable from guest EL1, at least for Apple's own path. **What must be verified is whether `HCR_EL2.TACR` traps ACTLR accesses for a `Hypervisor.framework` guest, and whether the trap (if any) can be satisfied.**

### 4.8 Counterpoint: Google's warehouse-scale finding

**Instruction Set Migration at Warehouse Scale.** Christopher, Crossan, Dobson, Kennelly, Lewis, Lin, Maas, Ranganathan, Rapati, Yang (Google). arXiv [2510.14928](https://arxiv.org/abs/2510.14928), Oct 2025. Analysing ~40,000 commits of Google's x86→ARM migration, they **"forewent binary translation altogether in our deployment"** for performance reasons, and found code-translation-class commits are **1% of commits and <4% of LoC**. The honest framing of *when* full-system DBT still matters: **closed-source software, legacy operating systems, firmware, and kernel-level guests** — i.e. exactly this project's target — and when it does not.

### 4.9 Also worth reading

- **BinRec** (Altinay et al., EuroSys 2020, DOI [10.1145/3342195.3387550](https://doi.org/10.1145/3342195.3387550)) — dynamic-trace-driven lifting to LLVM IR.
- **Spink & Franke, *Accelerating Shared Library Execution in a DBT*, LCTES 2024** (DOI [10.1145/3652032.3657565](https://doi.org/10.1145/3652032.3657565)) — mixed-mode native/translated execution in QEMU, reported at **2.7×–6.3× average speedup**. This is the same native-library-forwarding idea Box64, Risotto, and Arancini all independently converged on.

---

## 5. Using the ARM64 hardware MMU to emulate x86 paging

### 5.1 Shadow vs nested paging — the classic numbers

**K. Adams, O. Agesen. *A Comparison of Software and Hardware Techniques for x86 Virtualization*. ASPLOS 2006.** DOI [10.1145/1168857.1168860](https://doi.org/10.1145/1168857.1168860) · [talk deck](https://pages.cs.wisc.edu/~markhill/restricted/asplos06_vm_talk.pdf). Pentium 4 672 @ 3.8 GHz with VT-x, VMware Player 1.0.1 in both software-BT and hardware modes.

Nano-benchmark, **page-table modification (`ptemod`)**:

| Configuration | Cost |
|---|---|
| Native | **1 cycle** (a simple store) |
| Software-BT VMM | **~400 cycles** (adaptive translation converges on a special TRACE translation) |
| Hardware VMM (VT-x + shadow paging) | **~11,000 cycles** |

The hardware VMM is **bimodal**: native speed, or ~11,000 cycles on a VM exit. On computation-bound SPEC the two approaches are a toss-up; kernel-intensive workloads split. The paper's thesis is that first-generation VT-x rarely beat software BT because **it offered no MMU virtualisation support and did not co-exist with software MMU techniques.**

**This is the canonical warning for this project: hardware assist only wins if the exits are rare, and page-table maintenance is where exits concentrate.** Note that Captive's design largely sidesteps it — because the *guest's* page-table stores are just ordinary translated stores to guest RAM, and the shadow is rebuilt lazily on fault, there is no per-PTE-write trap at all.

**R. Bhargava, B. Serebrin, F. Spadini, S. Manne (AMD). *Accelerating Two-Dimensional Page Walks for Virtualized Systems*. ASPLOS 2008.** DOI [10.1145/1346281.1346286](https://doi.org/10.1145/1346281.1346286) · [PDF](http://www.cs.columbia.edu/~cdall/candidacy/pdf/Bhargava2008.pdf)
- **The formula:** with an *n*-level guest table and an *m*-level nested table, a 2D walk requires **n·m + n + m** page-entry references. For 4-level/4-level that is **24**. Four-level nested paging has **6× more page-entry references** than a native walk.
- Extending the Opteron **page-walk cache** to also cache the nested dimension: **15–38%** guest performance improvement. Adding a **Nested TLB**: up to **7%** more. For low-locality nested references, combined: **3–22%**.
- **2 MB nested pages yield 21–43% fewer TLB misses** than 4 KB nested pages.
- **Direct relevance:** ARM stage-1 + stage-2 has the same structure and the same 24-reference worst case. **This is the argument for making `hv_vm_map()` stage-2 mappings as coarse as possible** — ideally huge contiguous blocks over the guest-RAM slab — so the nested dimension collapses to one or two levels.

**Agile Paging** — Gandhi, Basu, Hill, Swift. ISCA 2014 / SIGARCH CAN 44(3). DOI [10.1145/3007787.3001212](https://doi.org/10.1145/3007787.3001212). Combines shadow and nested paging *within a single page walk*, starting nested and switching to shadow (or vice versa) mid-walk. **[unverified — PDF not retrieved, no numbers quoted here]** The mechanism is directly relevant: this project's design is effectively "**shadow at stage 1, nested at stage 2**", i.e. a static version of agile paging.

### 5.2 x86-64 vs ARMv8-A paging: the actual differences

Architectural analysis cross-checked against the Arm ARM and Intel SDM. Items flagged **[VERIFY]** are open questions to settle on real hardware.

**Table geometry.** Both are 4-level radix at a 4 KB granule with 48-bit VA. But **macOS/Apple Silicon uses a 16 KB granule**, where ARM gives a 3-level walk over a 47-bit VA and the L2 "block" is 32 MB rather than 2 MB. Two separate questions follow with different answers:

1. **Stage 2 (`hv_vm_map`)** — granularity is the host page size, i.e. **16 KB** (macOS 26 adds `HV_IPA_GRANULE_4KB`, §7.0). This is fine; stage 2 should map guest RAM in the largest blocks possible anyway (see Bhargava above). It matters only for how finely SMC write-protection and MMIO trapping can be applied.
2. **Stage 1 shadow tables built by the in-VM DBT** — these are *ours*. Whether a **4 KB granule** is usable at stage 1 depends on **`ID_AA64MMFR0_EL1.TGran4`**. **[VERIFY via `HV_FEATURE_REG_ID_AA64MMFR0_EL1`.] This is the highest-priority feasibility check in the whole project.** If TGran4 is unsupported, every 4 KB x86 guest page must be handled at 16 KB granularity, meaning a shadow entry can only be installed when **all four** 4 KB guest pages inside a 16 KB host page agree on permissions — otherwise those accesses fall back to software emulation. Captive anticipates exactly this: it supports *n:m* page-size mappings and notes that smaller guest pages "must emulate memory accesses carefully to ensure permissions within a page are not violated."

*Aside:* Apple's IOMMU (**DART**) only supports 16 KB-aligned pages, which is why Asahi Linux standardised on 16 KB globally ([feature support](https://asahilinux.org/docs/platform/feature-support-m1/)). That constraint is about **device DMA, not CPU stage-1**, so it does not by itself settle the TGran4 question.

**Accessed/Dirty bits.** x86 hardware *always* sets A on use and D on write. ARMv8.0's Access Flag is **software-managed** — an entry with AF=0 raises an Access Flag fault. **FEAT_HAFDBS** (ARMv8.1-TTHM) adds hardware AF update (HA) and hardware dirty-state via the **DBM** bit (HD). **[VERIFY `ID_AA64MMFR1_EL1.HAFDBS` on M-series via `HV_FEATURE_REG_ID_AA64MMFR1_EL1`]** — no public dump was found for M1/M2/M3. If HAFDBS is absent, every first touch and every first write to a guest page costs a fault to set the guest's A/D bits. **Even with HAFDBS, ARM sets A/D in *your shadow* entries, not in the guest's x86 PTEs**, so you must propagate them back — either lazily when the guest reads its own PTEs, or by write-protecting and sweeping.

**Permission model — the genuinely hard mismatch.** x86 has *independent* U/S and R/W bits at every level, ANDed down the walk, plus NX per entry, plus **CR0.WP** (when 0, supervisor ignores R/W on user pages), **SMEP**, **SMAP**, and PKU. ARM encodes access with **AP[2:1]**, a four-point lattice:

| AP[2:1] | EL1 | EL0 |
|---|---|---|
| 00 | RW | none |
| 01 | RW | RW |
| 10 | RO | none |
| 11 | RO | RO |

**This lattice cannot express several legal x86 states.** ARM's AP is *monotone* — EL1 always has at least the access EL0 has — whereas x86's U/S × R/W cross-product is not. The unrepresentable cases that matter:
- **user-writable but supervisor-read-only**
- **user-RW while supervisor has no access**
- **`CR0.WP = 0`** (used by real kernels during early boot and by some kernel self-patching paths)

The standard fix is **dual shadow address spaces**: one stage-1 tree used while the guest is at CPL0, another used while the guest is at CPL3. That also cleanly handles SMEP/SMAP. Rough correspondences otherwise: **SMEP ≈ PXN**, **SMAP ≈ FEAT_PAN** (with UAO for the deliberate-override case), **NX ≈ UXN/PXN**. **FEAT_S1PIE** (Armv8.9/9.4) permission indirection would relax the AP lattice considerably — **[VERIFY]** whether any Apple silicon exposes it.

**Break-before-make — a correctness trap with no x86 analogue.** ARM *requires* BBM when changing a live translation's output address, size, or attributes (relaxed only by FEAT_BBM levels 1/2): write an invalid entry → `DSB` → `TLBI` → `DSB` → `ISB` → write the new entry. **x86 permits atomic PTE replacement with no such protocol.** A shadow that naively mirrors guest PTE edits will violate BBM and can produce a **TLB conflict abort**. This must be designed in from the start, not retrofitted.

**TLB invalidation.** x86 `INVLPG` is **core-local and non-broadcast**; SMP shootdown is done in software via IPIs. ARM `TLBI VAE1IS` is **hardware-broadcast** to the inner-shareable domain and needs `DSB ISH` + `ISB` for completion. This is an **opportunity**: a guest `INVLPG` becomes one `TLBI`, and the guest's software IPI shootdown storm can be short-circuited for the TLB work (the IPIs still need delivering for the guest's own bookkeeping). x86 also implicitly flushes non-global entries on `MOV CR3`; on ARM you change `TTBR0_EL1` and rely on ASIDs.

**PCID vs ASID.** x86 PCID is **12 bits (4096)**, carried in `CR3[11:0]`, with `INVPCID` (4 flush types) and a `CR3[63]` no-flush bit. ARM ASID is **8 or 16 bits**, selected by `TCR_EL1.AS` (16-bit requires the wider-ASID feature), living in `TTBR[63:48]`; stage 2 adds a separate **VMID** (8 or 16 bits with FEAT_VMID16). Mapping up to 4096 guest PCIDs onto 256 or 65536 host ASIDs needs a **reclaim/generation-rollover allocator**; with only 8-bit ASIDs this becomes a real bottleneck. **[VERIFY `TCR_EL1.AS` width on Apple Silicon.]**

**Segmentation.** In 64-bit mode CS/DS/ES/SS bases and limits are ignored, but **FS and GS bases are live** (`IA32_FS_BASE`/`IA32_GS_BASE`, `WRFSBASE`/`WRGSBASE`, and `SWAPGS` — the last is load-bearing for every Linux and Windows syscall entry). Map these to **`TPIDR_EL0` / `TPIDRRO_EL0` / `TPIDR_EL1`** plus an add in translated addressing; `SWAPGS` becomes a register swap. This is exactly what Instrew does in the opposite direction. **32-bit compat mode needs full base+limit checks emitted inline.**

**Self-referencing page tables.** Windows (and some other kernels) install a self-map PDE so the page tables are visible through a virtual window. Under a Captive-style direct mapping this is mostly harmless — guest PTE loads/stores just hit guest physical memory. Under shadowing it matters twice: (a) **write-protection of guest PT pages must also cover the self-map window's virtual aliases**, and (b) the guest can *read* its own PTEs and observe A/D bits, so lazy A/D propagation must be correct through that window.

**Real mode and mode transitions.** There is no ARM equivalent of real mode, big real mode, or the `CR0.PG` / `EFER.LME` / `CR4.PAE` transition sequence. Practical structure: **interpret (or translate against a flat identity map) until the guest sets `CR0.PG`**, then switch to the hardware-MMU shadow path. The **A20 gate** is an address-line mask with no ARM analogue and must be folded into address generation (or handled by a stage-2 remap) while active.

### 5.3 The verification checklist this section produces

These are the first experiments to run, in priority order:

1. **`ID_AA64MMFR0_EL1.TGran4`** — does stage-1 support a 4 KB granule on the target chip? *If no, the entire fast-path premise degrades.*
2. **Can a guest at EL1 set `ACTLR_EL1.TSOEN`**, or does `Hypervisor.framework` trap ACTLR via `HCR_EL2.TACR`? *If yes, you delete the single largest cost in every userspace x86-on-ARM translator (FEX: "near 10×" on memory-heavy code). If no, you inherit Risotto/Arancini's fence-placement problem.*
3. **`ID_AA64MMFR1_EL1.HAFDBS`** — hardware A/D management?
4. **`TCR_EL1.AS`** — 8-bit or 16-bit ASIDs?
5. **`hv_vcpu_run()` round-trip latency.** No published VM-exit measurement for Apple `Hypervisor.framework` was found. Given Adams & Agesen's ~11,000-cycle VT-x figure and the fact that exit frequency decides everything, this should be an early microbenchmark.
6. **`hv_vm_config_get_el2_supported()`** on the target chip (nested virt is reported as **M3+ under macOS 15**; also surfaced as `VZGenericPlatformConfiguration.isNestedVirtualizationSupported`). *Note: nested virt is **not required** for the Captive design — you need exactly one EL1, which M1/M2 already provide. It matters only if the x86 guest itself runs a hypervisor (i.e. you want to emulate VT-x), or if you want the engine in its own EL2 regime.*


## 6. Where the time actually goes: the performance ceiling of cross-ISA emulation

Confidence markers: **[P]** = verified against the primary paper text; **[S]** = from a secondary summary/snippet only.

### 6.1 Software MMU

**The headline number.** X. Tong, T. Koju, M. Kawahito, A. Moshovos. *Optimizing Memory Translation Emulation in Full System Emulators*. ACM TACO 11(4), 2015. DOI [10.1145/2686034](https://doi.org/10.1145/2686034). **38.1% of QEMU's full-system emulation time is spent in memory emulation on average — despite QEMU already having a software TLB.** Their STLB optimisations (dynamic-sized TLB, victim TLB, targeting both lookups and refills) give an **average 24.4% whole-emulator speedup**. **[P]** *Caveat: full-system QEMU circa 2014; QEMU's TLB has been retuned since.*

**Why it is structural.** Spink's thesis measures the SPEC CPU2006 integer operand mix: **~50% of dynamically executed guest instructions perform a memory operation**. Under a softmmu, roughly every other guest instruction pays a virtual→physical translation. **[P]**

**Function-level profile.** A. Karaman, [*Measuring QEMU Performance in System Mode*](https://ahmedkrmn.github.io/TCG-Continuous-Benchmarking/Measuring-QEMU-Performance-in-System-Mode/) (2020) — booting Debian netboot across 5 targets, 136.7B–236.7B guest instructions: **[P]**

| Function | % of system-mode runtime |
|---|---|
| `helper_lookup_tb_ptr` (indirect-branch resolution) | **6.29–16.91%** |
| `tlb_flush_by_mmuidx_async_work` | 2.73–9.10% |
| `get_phys_addr_lpae` (guest page-table walk, aarch64) | 4.71% |
| `tlb_set_page_with_attrs` | 2.57–3.29% |
| `victim_tlb_hit` | 0.74–1.25% |
| `tb_htable_lookup` / `qht_lookup_custom` | 0.63–1.41% / ~0.62% |

*Methodology caveat: an OS boot is TLB-flush- and translation-heavy, over-weighting TLB maintenance relative to SPEC steady state. Still, >15% in page-walk + TLB maintenance alone is a real full-system signal.*

**Bellard's original framing.** [QEMU, USENIX ATC FREENIX 2005](https://www.usenix.org/legacy/event/usenix05/tech/freenix/full_papers/bellard/bellard.pdf): user-mode QEMU ≈ **4× slower than native on integer, 10× on FP**; in full-system emulation **"the cost of the software MMU induces a slowdown of a factor of 2"** relative to user-mode. **[S — canonical but 20 years old]**

**How much is recoverable.**
- **Embra** (Witchel & Rosenblum, SIGMETRICS 1996, DOI [10.1145/233013.233025](https://doi.org/10.1145/233013.233025)): software TLB + direct-mapped relocation array; full-system MIPS/IRIX at **3–9× slowdown** — but *same-ISA*, so a floor, not a cross-ISA result. **[S]**
- **HSPT** (Chang, Wu, Hsu, Liu, Yew), *Efficient memory virtualization for cross-ISA system mode emulation*, VEE 2014, DOI [10.1145/2576195.2576201](https://doi.org/10.1145/2576195.2576201) — shadow page tables built via `mmap`. This is Captive's direct comparison point (§1.8: HSPT 1.94× vs Captive 2.05× over the Android Emulator).
- **BTMMU**, VEE 2021, DOI [10.1145/3453933.3454015](https://doi.org/10.1145/3453933.3454015): a low-cost host-MMU hardware extension + kernel module + patched QEMU. **1.40× (IA32→MIPS64) and 1.36× (x86_64→MIPS64) over baseline QEMU on SPEC CINT2006.** **[P]** *Note how modest: even fully fixing the MMU buys only ~1.4×, which brackets the softmmu tax at roughly 28–38% and corroborates Tong's 38.1%.*
- **Captive** — the host-MMU approach: **2.21× over QEMU** (§1.8).

### 6.2 Condition flags

**FEX-Emu's deferred-flags/NZCV rework** ([FEX-2312](https://fex-emu.com/FEX-2312/), Dec 2023). Previously FEX emitted "a dozen or so additional instructions for calculating flags" per flag-setting op; the rework maps x86 SF/ZF/CF/OF directly onto ARM NZCV and dedicates two registers to PF/AF (no ARM equivalent, "fairly costly to calculate"). Measured uplift on a Lenovo X13s: **Geekbench +17.6%, Bytemark up to +60%, >2× FPS in CPU-heavy games.** **[P]** This is the best quantification of the flag tax in a real x86-on-ARM64 translator.

**Hardware assists** (dougallj, [*Why is Rosetta 2 fast?*](https://dougallj.wordpress.com/2022/11/09/why-is-rosetta-2-fast/)): `FEAT_FlagM` (`CFINV` for x86 borrow-vs-carry, `RMIF` for fixed-shift flag semantics, `SETF8`/`SETF16` for 8/16-bit operand flag results); `FEAT_FlagM2` (`AXFLAG`/`XAFLAG` for FP condition-flag format). **Apple's undocumented mode computes PF and AF in hardware**; without it, software emulation needs **~5 extra instructions per subtraction**, and add/sub are among the most-executed x86 instructions. **[P]**

**Budget:** flags are worth roughly **10–35% of cross-ISA overhead** for integer-heavy x86 guests absent hardware assist. FEX's +17.6%/+60% is what you recover by using NZCV well *in software*; Apple's PF/AF mode is what you recover on top.

### 6.3 Indirect branches

**The claim.** d'Antras, Gorgovan, Garside, Luján. *Low Overhead Dynamic Binary Translation on ARM*. PLDI 2017. DOI [10.1145/3062341.3062371](https://doi.org/10.1145/3062341.3062371) · [PDF](https://pure.manchester.ac.uk/ws/files/56078084/pldi_16.pdf): *"It is widely acknowledged that indirect branch handling is the single biggest source of overhead for DBTs."* **[P]**

**The ablation that quantifies it** (MAMBO-X64, AArch32→AArch64, SPEC CPU2006, X-Gene 1 @ 2.4 GHz): **[P]**

| Configuration | Overhead vs native |
|---|---|
| Single-entry/single-exit fragments + hash-table indirect lookup | **11.1%** |
| + hardware-assisted function returns (software RAS driving the hardware RAS) | **4.1%** |
| + NET trace generation alone | **2.9%** |
| ReTrace (both) | **1.0% *faster* than native** |

**≈7 of the 11.1 percentage points — >60% of residual overhead — was return/indirect-branch handling.**

- Companion TACO 2016 paper (DOI [10.1145/2866573](https://doi.org/10.1145/2866573)): hardware-assisted returns **reduce DBT overhead by 40% on average, up to 90%**; MAMBO-SS achieves near-100% return prediction accuracy. **[S]**
- **Tiaozhuan**, TACO 2024, DOI [10.1145/3703355](https://doi.org/10.1145/3703355): *"each guest indirect branch instruction still translates into approximately 10 host instructions"* in state-of-the-art schemes; IBTC and MAMBO-SS need 7 instructions in the fast path; their FAM+EABE needs 1–2. Result: **−4.2% dynamic instructions, +3.9% perf average, up to +19.4%** on SPEC CPU2006. **[P]**
- **SPIRE**, VEE 2013, DOI [10.1145/2451512.2451516](https://doi.org/10.1145/2451512.2451516): SPC-indexed redirecting cuts indirect-branch overhead **36.2% average, up to 51.4%**, at 5.6% extra memory. **[S]**
- **In QEMU**: `helper_lookup_tb_ptr` is **6.29–16.91%** of system-mode runtime (§6.1). **[P]**
- **Rosetta 2**: x86→ARM address mapping via two-level binary search, ARM→x86 via top-level binary search over delta-encoded bit-packed entries, memoised in a hash map; `CALL`/`RET` → `BL`/`RET` so the **hardware return-address stack predicts correctly**, with validation bookkeeping on a side stack. **[P]**

### 6.4 Self-modifying code and code-cache invalidation

The only hard numbers are Transmeta's (CMS, CGO 2003 — see §3.1 for mechanism):

| Ablation | Cost |
|---|---|
| **No fine-grain (sub-page) write protection** — Win95 boot | 52.8× more protection faults, **2.2× slowdown** |
| — Win98 boot | 59.4× faults, **3.8× slowdown** |
| — MultimediaMark | 46.8× faults, **1.6× slowdown** |
| — WinStone Corel | 54.2× faults, **2.1× slowdown** |
| **Self-checking translations everywhere** | **+83% code size** (58–100%), **+51% dynamic molecules** (11–124%); real impact higher due to I-cache |
| **Self-revalidating translations** (add) | Quake Demo2 **+28% frame rate** |

**Full-system SMC coherence.** HyperMAMBO-X64 (§2.4) pushes it into the hypervisor: stage-2 write protection plus trapped guest TLB invalidations filtered through a hash lookup on registered (VA, ASID) pairs. Its inverted lesson matters here: **on x86 hosts the absence of tagged TLBs/ASIDs forces a full TLB flush on every context switch, which necessarily invalidates all translated code for that process** — a structural argument that **ARM64 hosts are better for this than x86 hosts**. It also cites **PinOS at ~50× slowdown**, attributed largely to runtime checks for page-table modification. **[P]**

### 6.5 Exceptions, interrupts, precise state

The precise-exception tax shows up as *the cost of not reordering*. CMS quantifies it by disabling speculation: **[P]**

| Ablation | Cost |
|---|---|
| Suppress memory reordering (strictly precise x86 memory-op order) | **mean 10.09% on OS boots** (Linux 1.48%, DOS 18.95%, Win98 17.19%, WinXP 19.64%, **WinME 26.08%**); **mean 33.14% on applications** (Quake Demo2 29.71%, 072.sc 44.64%, 048.ora 66.22%, 052.alvinn 78.38%, **047.tomcatv 97.29%**) |
| Remove alias-detection hardware | **mean 5.48% boots, 23.53% apps** — "almost as severe as not reordering at all" |

Recovery mechanism: shadow registers + gated store buffer; **commit effectively free, rollback "less than a couple of" cycles**; re-interpret the region to reproduce precise x86 state; adaptive retranslation narrows regions when faults recur.

**The uncomfortable finding for this project.** Captive beats QEMU on nearly every SimBench category (two at 32.38× and 25.88×) **except `Data-Fault`, where Captive is *slower* than QEMU**, because of the bookkeeping needed to recover which guest virtual address faulted. **[P]** That is a clean instance of *hardware-accelerating the MMU makes precise fault attribution harder* — directly relevant, since an x86 guest needs `CR2` + a correct error code on every `#PF`.

HyperMAMBO's page-fault path shows the same pattern from the other side: recovering guest register state at the faulting instruction **truncates NET traces, costing up to 13% extra on gcc/cactusADM**. **[P]** MAMBO-X64's speculative address generation (assume AArch32 base+offset doesn't wrap, catch mis-speculation via faults) is the same bet in the other direction, and mis-speculation was measured to be rare. **[P]**

### 6.6 Memory model / TSO on weak hardware — the biggest single number

**Gouicem, Sprokholt, Ruehl, Rocha, Spink, Chakraborty, Bhatotia. *Risotto: A Dynamic Binary Translator for Weak Memory Model Architectures*. ASPLOS 2023.** DOI [10.1145/3567955.3567962](https://doi.org/10.1145/3567955.3567962) · [PDF](https://www.st.ewi.tudelft.nl/sschakraborty/papers/risotto-asplos23.pdf)

> *"By observing the performance of no-fences, we see that fences account for a large portion of the execution time of our benchmarks, **up to 75% (for freqmine), 48% on average**."* **[P — verbatim from the PDF]**

Methodology so you can discount appropriately: **x86-64 guest on ARMv8.1**, 2× Marvell ThunderX2 CN9975 (28c/4-way SMT @ 2.0 GHz, 112 hardware threads, pinned to one socket, turbo off), **QEMU v6.1.0**, **PARSEC 3.0 + Phoenix**, 5 runs. `no-fences` is *incorrect* but serves as the oracle for maximum fence-elimination gain. This is a **multithreaded** suite on a 112-thread machine, so 48% is an upper-ish bound for laptop-class hardware.

Risotto's own gain (formally verified minimal mappings, fence merging, weaker `DMBLD`/`DMBST` instead of `DMBFF`): **up to 19.7%, 6.7% average over QEMU.** Its separate cross-ISA native-shared-library linking gives **1.4× (md5-1024) to 23× (sha256-8192)** on OpenSSL/sqlite and **1× (sqrt) to 10× (cos)** on libm (native reaches 25×). **[P]**

**Practitioner corroboration.** FEX-Emu ([FEX-2306](https://fex-emu.com/FEX-2306/)): *"Emulating the x86 memory model is the number one thing that slows down FEX emulation today."* Without hardware TSO, FEX falls back to atomics + LRCPC, which **"absolutely demolishes performance"**; they note **"ARM Cortex's hardware LRCPC implementation is barely any faster than atomics."** FEX exposes per-app config to *disable* memory-model emulation for free performance — at the cost of crashes in e.g. Unity games. On Asahi Linux, FEX uses a kernel `prctl` to enable **Apple's hardware TSO**. **[P]**

**What hardware TSO itself costs.** Wrenger, Töllner, Lohmann. *TOSTING: Investigating Total Store Ordering on ARM*. ARCS 2023. DOI [10.1007/978-3-031-42785-5_10](https://doi.org/10.1007/978-3-031-42785-5_10): on multithreaded SPEC CPU2017 FP, **TSO mode is on average 8.94% slower than ARM's native weak ordering**. **[S]**

**So the design point is ~9% (hardware TSO) vs ~48% average / up to 75% (software fences).** That gap is the entire reason Rosetta 2 is viable and QEMU-on-generic-ARM is not. Hardware TSO modes also exist on **NVIDIA Denver/Carmel** and **Fujitsu A64FX**.

**Related:** *lasagne* (static x86→AArch64 translation with a formally-justified memory-model mapping) and ArMOR (Lustig et al.) on memory-model shim synthesis are the companion literature for getting the fence placement minimal-and-correct.

### 6.7 Achieved ceilings — what anyone has actually measured

| System | Guest → Host | Benchmark | Result | Source |
|---|---|---|---|---|
| **Rosetta 2** | x86-64→AArch64, M1 | SPEC CPU2006/2017 | memory-bound **>90% of native**; **fp2017 ~70–80%**; 502.gcc_r **49.87%**; worst = 456.hmmer (L1-resident compute) | AnandTech M1 Mac mini review, Nov 2020 **[P]** |
| **Rosetta 2** | x86-64→AArch64, M1 | 7-zip | 33746 vs 47239 native = **71% of native** | [box86.org](https://box86.org/2022/03/box86-box64-vs-qemu-vs-fex-vs-rosetta2/) **[P]** |
| **Box64** | x86-64→AArch64, M1 Linux | 7-zip | 24746 vs 43271 = **57% of native** | box86.org **[P]** |
| **Box64 / FEX / QEMU** | x86-64→AArch64, RPi 400 | 7-zip | native 5787; Box64 3084 (**53%**), FEX 1486 (**26%**), QEMU 931 (**16%**) | box86.org **[P]** |
| same | | dav1d 1-thread | native 185.57; Box64 49.72 (**27%**), FEX 27.28 (**15%**), QEMU 11.24 (**6%**) | box86.org **[P]** |
| same | | openarena fps | native 8.4; Box64 8.4 (**100%**, GPU-bound), FEX 1.8, QEMU 0.083 | box86.org **[P]** |
| **MAMBO-X64** | AArch32→AArch64 | SPEC CPU2006 | **−1% (i.e. a speedup) on X-Gene**, 2.5% overhead on A57, <7.5% on A53; PARSEC 2.1% @1–4 threads, 4.9% @8 | PLDI'17 **[P]** |
| **Captive** | ARMv8-A→x86-64, **full-system** | SPEC CPU2006 | **2.21× over QEMU (int), 6.49× (FP)**; **7.24× slower than native x86-64 host**; ≈**40% of a 2.0 GHz Cortex-A57** | TOCS 2020 / ATC'19 **[P]** |
| **QEMU user** | various | SPEC CPU2006 | ~**4× int, 10× FP** (Bellard 2005) | **[P/S]** |
| **QEMU system** | x86 | SPEC CINT2006 | **18.73× average slowdown vs native** (QEMU 6.1) | arXiv [2402.09688](https://arxiv.org/abs/2402.09688) **[S]** |

Calibration points: Captive emits **~10 host instructions and 67.53 bytes of host code per guest instruction** (QEMU: 40.26 bytes); Rosetta 2's AOT output for `sqlite3` is **1.64× the x86 binary size**.

**Atomics axis.** Cota, Bonzini, Bennée, Carloni. *Cross-ISA Machine Emulation for Multicores*. CGO 2017. [PDF](http://www.cs.columbia.edu/~luca/research/cota_CGO17.pdf): fully-correct LL/SC-via-CAS emulation "incurs significant slowdown"; their store-instrumentation approach gives **~2× average slowdown for CINT2006** with Pico-ST adding only **4%** over the bitmap baseline. They also note QEMU's FP goes through softfloat integer code — which is where the extra order of magnitude on CFP2006 comes from, and is a portability choice, not an intrinsic DBT cost. **[P]**

### 6.8 Taxonomies and "limits of DBT"

- **SimBench** (Wagstaff, Spink, Franke, ISPASS 2013) is the closest thing to an accepted cost taxonomy for full-system emulators, and its categories are exactly the axes above: `Mem-Hot-MMU`, `Mem-Hot-NoMMU`, `Mem-Cold-MMU`, `Mem-Cold-NoMMU`, `Undef-Instruction`, `Syscall`, `Data-Fault`, `Instruction-Fault`, `Small-Blocks`, `Large-Blocks`, `Same-Page-Direct/Indirect`, `Inter-Page-Direct/Indirect`, `TLB-Flush`, `TLB-Evict`. **Use it as the evaluation skeleton for this project.**
- **CMS (CGO 2003)** is the best taxonomy of *hardware assists*: gated store buffer + shadow registers (precise exceptions), alias hardware (memory reordering), fine-grain write protection (SMC), adaptive retranslation as the policy layer. Every number in §6.4–6.5 is an ablation of one of those.
- **Instruction Set Migration at Warehouse Scale**, Google, arXiv [2510.14928](https://arxiv.org/abs/2510.14928) (Oct 2025). From ~40,000 commits of Google's x86→Arm migration: they **"forewent binary translation altogether in our deployment"** explicitly for performance, and found code-translation-class commits are **1% of commits and <4% of LoC** — refuting the conventional wisdom that translation dominates ISA migration. The "why nobody ships DBT at scale" citation.

### 6.9 Why full-system is so much worse than user-mode

Composing the measured deltas:

1. **Softmmu**: Bellard's ~2× vs user-mode; Tong's 38.1% of runtime; BTMMU's 1.36–1.40× recovery ceiling; Captive's 2.21×.
2. **Guest page-table walks and TLB maintenance are themselves emulated**: on a boot profile, `get_phys_addr_lpae` 4.71% + `tlb_flush_by_mmuidx_async_work` up to 9.10% + `tlb_set_page_with_attrs` up to 3.29%.
3. **Code-cache invalidation is coupled to guest TLB/ASID behaviour** — HyperMAMBO shows an x86 host's untagged TLB forces full flushes at every guest context switch, destroying the code cache.
4. **Device MMIO and I/O.** Spink's `hdparm` **cached** reads (page-cache reads that still traverse the memory subsystem): **QEMU ARM-on-x86 DBT is 78.87× slower than native**, QEMU x86-on-x86 DBT 9.79×, **Captive 7.3×** — a **10.8×** improvement purely from doing address translation in hardware. Buffered (uncached) disk reads: Captive **1.11×** vs QEMU/ARM **1.64×**. MMIO is pathological because every access must be checked against the memory-region radix tree rather than hitting the TLB fast path — Cota notes this tree is "read on every TLB miss."
5. **Privileged/system instruction and exception emulation.** Captive supports full-system for only **2 of its 6 modelled guest ISAs**, citing *"the sheer amount of (non-trivial) work necessary to implement system-level functionality (e.g. the MMU behavior, system instructions, interrupt/exception handling protocols)."*
6. **Net**: user-mode QEMU ~4–10×; full-system QEMU ~**18.73×** on CINT2006; and the best published cross-ISA full-system DBT (Captive) still sits at **7.24× native**. Versus Rosetta 2's ~70–80% of native for **user-mode with ISA-specific hardware assists**.

### 6.10 The one-paragraph budget for this project

For x86-64-on-ARM64 the four dominant taxes, with best-supported magnitudes:

| Tax | Magnitude without assist | Recoverable to |
|---|---|---|
| **Memory model / TSO** | **48% of runtime average, up to 75%** (Risotto, multithreaded) | ~**9%** with hardware TSO (TOSTING) |
| **Software MMU** | **38.1%** of full-system runtime (Tong) | ~1.4× (BTMMU) / 2.21× over QEMU (Captive) |
| **Indirect branches / returns** | **~7 of 11.1 pp** of residual overhead; ~**10 host instructions** per indirect branch | ~1–2 instructions (Tiaozhuan); near-zero with hardware RAS (MAMBO-X64) |
| **Flags** | **+17.6% Geekbench / up to +60% Bytemark** from NZCV mapping alone; **~5 instructions per sub** for PF/AF | near-zero with Apple's PF/AF mode + FlagM2 |

SMC and precise exceptions are second-order in *average* cost (2–4× only in pathological Windows-boot-like workloads; 10–33% for suppressing reordering) but first-order in *correctness* and in the design constraints they impose.

**Apple Silicon uniquely provides hardware for three of the four** — TSO, FlagM/FlagM2 plus undocumented PF/AF, and a hardware RAS that survives CALL/RET translation. That is precisely why Rosetta 2 lands at **70–80% of native** while FEX/Box64 land at **26–57%** and QEMU at **6–16%** on the same class of workload. **Whether those three assists are reachable from EL1/EL0 inside a `Hypervisor.framework` VM is the determining question for this project.**




## 7. Synthesis: what Captive/HyperMAMBO teach us, and what breaks on Apple Silicon

### 7.0 Ground truth about the target platform

Facts verified directly from the macOS 26.5.2 SDK headers (`Hypervisor.framework/Headers/*.h`) and `sysctl` on an Apple M2 Max, rather than from documentation:

| Fact | Value | Source |
|---|---|---|
| Guest execution levels | EL1 + EL0; **EL2 also available** when `hv_vm_config_get_el2_supported()` is true and `hv_vm_config_set_el2_enabled()` is set | `hv_vm_config.h` (macOS 15.0+) |
| Guest EL2 system registers exposed | `HCR_EL2`, `VTTBR_EL2`, `VTCR_EL2`, `TTBR0/1_EL2`, `TCR_EL2`, `SCTLR_EL2`, `VBAR_EL2`, `ESR_EL2`, `FAR_EL2`, `HPFAR_EL2`, `SPSR_EL2`, `ELR_EL2`, `CPTR_EL2`, `MDCR_EL2`, `CNTHCTL_EL2`, `CNTVOFF_EL2`, `VPIDR/VMPIDR_EL2` | `hv_vcpu_types.h`, all `API_AVAILABLE(macos(15.0))` |
| Stage-2 control | `hv_vm_map(void*, hv_ipa_t, size, flags)`, `hv_vm_unmap`, `hv_vm_protect` | `hv_vm.h` |
| Stage-2 permissions | `HV_MEMORY_READ/WRITE/EXEC/UEXEC` + `MAXPROT_*` — note **separate `EXEC` and `UEXEC`**, i.e. PXN/UXN are independently controllable at stage 2 | `hv_types.h` |
| Stage-2 granule | 16 KB only until macOS 26; **macOS 26.0 adds `HV_IPA_GRANULE_4KB`** via `hv_vm_config_set_ipa_granule()` | `hv_vm_config.h` |
| Host page size | **16384** (16 KB) | `getconf PAGE_SIZE` on M2 Max |
| Exit reasons | `EXCEPTION` (with `ESR_ELx` syndrome + `FAR_ELx` VA + IPA), `VTIMER_ACTIVATED`, `CANCELED`, `UNKNOWN`. **No port-I/O-style exit; every guest-initiated exit is an exception or an `HVC`.** | `hv_vcpu_types.h` |
| Interrupt injection | `hv_vcpu_set_pending_interrupt(HV_INTERRUPT_TYPE_IRQ/FIQ)`; GICv3 via `hv_gic_*` (macOS 15+) | `hv_vcpu.h`, `hv_gic.h` |
| Debug traps | `hv_vcpu_set_trap_debug_exceptions`, `hv_vcpu_set_trap_debug_reg_accesses` | `hv_vcpu.h` |
| M2 Max relevant ISA features | `FEAT_FlagM=1`, `FEAT_FlagM2=1` (→ `rmif`, `cfinv`, `setf8/16`, `axflag`/`xaflag`), `FEAT_LSE=1`, `FEAT_LSE2=1`, `FEAT_LRCPC2=1`, `FEAT_JSCVT=1`, `FEAT_PAuth=1`, `FEAT_BTI=1`, **`FEAT_AFP=0`**, `FEAT_ECV=1` | `sysctl hw.optional.arm.*` |

### 7.1 What transfers essentially unchanged

**(1) The whole structural idea.** Hypervisor.framework gives exactly what Captive needed from KVM: a same-ISA hardware VM in which you own the MMU, the exception vectors, the privilege split, and the TLB. Captive's three-part decomposition (host-side hypervisor process with device models / bare-metal unikernel execution engine / generated guest model) maps 1:1 onto `Hypervisor.framework` + an ARM64 unikernel + an x86-64 model.

**(2) Guest privilege → host privilege.** Captive: guest PL1 → x86 ring 0, guest PL0 → x86 ring 3. Here: guest **CPL0 → EL1**, guest **CPL3 → EL0**. x86 nominally has 4 rings but rings 1 and 2 are effectively unused by every OS we care about, so the 2→2 mapping holds. The consequence is the important part: the *host page tables' AP bits enforce the guest's U/S bit in hardware*, with zero software permission checking on the fast path.

- Captive's `syscall`/`sysret` ring switch → **`SVC` / `ERET`** here. `ERET` is cheap; `SVC` is a real exception entry through our own `VBAR_EL1`, so cost is comparable but not free.
- Captive's **x86 call gates** (ring-3 translated code calling ring-0 helpers) → **`SVC #imm`** with immediate-dispatched handlers.
- Captive's `%fs` = per-CPU state pointer → **`TPIDR_EL1`** (and a reserved GPR); Captive's `%r15d` = guest PC → a reserved GPR (ARM64 has 31, so this is cheaper here than on x86).

**(3) Fault-driven shadow page-table fill.** Emit a guest load/store as a single host `ldr`/`str`; first touch faults; the fault handler walks the *x86 guest* page tables (through an identity map of guest physical memory in the high half), synthesises the ARM64 stage-1 descriptor with full permissions, installs it, and retries. Verbatim Captive.

**(4) O(1) invalidation via a top-level trick.** Captive clears `present` on PML4[0] and flushes. ARM64's equivalent is even better: **`TTBR0_EL1` and `TTBR1_EL1` are two independent hardware page-table bases**, and `TLBI ASIDE1IS` / `TLBI VMALLE1IS` give ASID-scoped and global flushes directly.

**(5) Physical-address-indexed code cache + write-protect-based SMC detection.** Both mechanisms are ISA-neutral. Note both Captive *and* HyperMAMBO independently converged on write-protecting the *physical* page, and HyperMAMBO specifically put that protection in **stage 2** — which is exactly what `hv_vm_protect()` gives us here, for free, without touching our own stage-1 tables.

**(6) ASID-tagged translations and per-ASID shadow tables.** Captive keys shadow L3 tables on the guest ASID; HyperMAMBO keys *code fragments* on the ASID. For an x86 guest: key on **CR3 value** (and PCID when the guest enables it). This is precisely the fallback Captive itself proposed for guests without ASIDs, and it is the common case for x86 since PCID is only widely used with KPTI.

**(7) Never exit the VM for device access.** Captive's two-barrier shared-memory protocol with a parked host thread is the single most reusable engineering trick in the thesis, and it matters *more* here: a `hv_vcpu_run()` round trip is a Mach user↔kernel transition, and Hypervisor.framework has no cheap PIO-style exit to fall back on. Mark device IPA ranges not-present in our *stage-1* tables so device accesses fault **inside** the VM, then hand off via shared memory.

**(8) Interrupts: inject for real, check at safe points.** Captive injects a real x86 IRQ into the Native VM; the VM's handler sets a pending flag; translated code checks it at basic-block boundaries. Here: `hv_vcpu_set_pending_interrupt(..., HV_INTERRUPT_TYPE_IRQ, true)` → our own `VBAR_EL1` IRQ handler → flag → block-boundary check. Identical shape. HyperMAMBO's refinement is worth stealing: on an async interrupt, **resume with interrupts masked until the end of the current fragment**, then recover precise state from fragment-link metadata, rather than keeping per-instruction recovery metadata.

**(9) Precise-state recovery, three tiers** (HyperMAMBO §2.2): compile recovery inline for translation-time-known exceptions (syscall, `#UD`); per-fragment side tables for potentially-faulting memory instructions; defer-to-fragment-end for asynchronous interrupts. This is the cheapest known scheme and it is directly applicable.

**(10) Hardware FP/SIMD with inline fix-ups, not softfloat.** Captive's isolated measurement — **2.17x over QEMU with hardware FP, 1.68x with QEMU's softfloat, i.e. 1.30x attributable to hardware FP alone** — is the quantitative argument for never calling a softfloat helper. Note the constraint: the host must be at least as precise as the guest, and x86-80-bit x87 has no ARM64 equivalent, so x87 long-double will need software or a documented accuracy compromise.

### 7.2 What is *better* on Apple Silicon than in Captive's setting

**(a) The 64-bit-on-64-bit address-space problem largely evaporates.** Captive's worst structural cost is that a 64-bit guest VA does not fit in the host's lower half, forcing a range check on every memory access plus a page-table-base switch and TLB flush (or PCID juggling) for upper-half accesses. On ARM64, **`TTBR0_EL1` covers the low VA range and `TTBR1_EL1` covers the high VA range, selected by the top address bits in hardware** — and x86-64's canonical split (low half `0x0000_0000_0000_0000`–`0x0000_7FFF_FFFF_FFFF`, high half `0xFFFF_8000_...`) lines up with ARM64's `T0SZ=T1SZ=16` split almost exactly. A guest x86-64 user address lands in TTBR0 territory and a guest x86-64 kernel address lands in TTBR1 territory **with no masking, no range check, and no table switch**. This is a materially better structural fit than Captive had, and it is arguably the strongest technical argument for this project's viability.
- The cost: our engine's own code/data and the guest-physical identity map need somewhere to live. Options: carve a hole in the high half that the guest kernel provably does not use (fragile); use the **EL2 regime** if available (clean); or map the engine into *both* regimes at addresses the guest cannot generate.

**(b) Guest EL2 is available (macOS 15+, M3 and later).** `hv_vm_config_set_el2_enabled()` plus the full set of `*_EL2` system registers means the execution engine can run at **guest EL2 with its own `TTBR0_EL2`/`TCR_EL2`/`VBAR_EL2`**, entirely separate from the EL1/EL0 regime used for translated code, *and* can program its own **`VTTBR_EL2`/`VTCR_EL2` stage-2 tables**. That gives a second, engine-controlled protection layer for SMC write-protection and MMIO trapping without any round trip to macOS. Captive had no equivalent (nested virtualization was explicitly listed as future work).
- **But note: nested virtualization is *not required* for the Captive design.** You need exactly one EL1, which M1/M2 already provide. Guest EL2 matters only (i) if you want a clean regime split for the engine, or (ii) if the x86 guest itself runs a hypervisor and you must emulate VT-x. **Do not make it a hard dependency**; verify with `hv_vm_config_get_el2_supported()` (also surfaced as `VZGenericPlatformConfiguration.isNestedVirtualizationSupported`).

**(b2) `TLBI` is hardware-broadcast; `INVLPG` is not.** x86 `INVLPG` is core-local and SMP shootdown is done in software with IPIs. ARM `TLBI VAE1IS` broadcasts to the inner-shareable domain (with `DSB ISH` + `ISB` for completion). So a guest `INVLPG` becomes **one** `TLBI`, and the guest's IPI-based shootdown storm can be short-circuited for the *TLB* work (the IPIs still need delivering for the guest's own bookkeeping). This is a real multi-core advantage over Captive's x86 host.

**(b3) ARM64's tagged TLB saves the code cache.** HyperMAMBO's analysis of MagiXen (§2.6) is the counter-example: **on x86 hosts the absence of tagged TLBs forces a full flush at every guest context switch, which necessarily invalidates all translated code for that process.** On an ARM64 host, ASIDs make per-address-space translation caching work — the same property HyperMAMBO exploited.

**(c) Separate `EXEC` and `UEXEC` stage-2 permissions** (`HV_MEMORY_EXEC` vs `HV_MEMORY_UEXEC`) give independent PXN/UXN control at stage 2 — useful for SMEP-style guest semantics and for keeping the code cache non-executable from translated guest code.

**(d) Hardware flag-format assists.** `FEAT_FlagM`/`FEAT_FlagM2` are present on M2 Max: `cfinv`, `rmif`, `setf8`/`setf16`, and `axflag`/`xaflag` for converting between ARM and x86 floating-point comparison flag conventions. Captive never had an analogous assist for the guest's flag semantics and paid for it in emitted code.

**(e) 31 general-purpose registers** vs x86-64's 16. Captive burned `%r15d` on the guest PC and `%fs`/`%gs` on state pointers and still complained about register pressure ("456.hmmer and 462.libquantum … suboptimal register allocation in hot code"). Mapping x86-64's 16 GPRs into ARM64's 31 leaves ample room for the guest PC, CPU-state pointer, flag scratch, and memory-base registers, all pinned.

### 7.3 What breaks, or costs more, on Apple Silicon

**(1) 16 KB pages — the single biggest platform-specific hazard.**
- The host page size on Apple Silicon is 16 KB. A 4 KB x86 guest page is *smaller* than the host granule, which is precisely the direction Captive flagged as hard: "In the case of smaller guest pages, we must emulate memory accesses carefully to ensure permissions within a page are not violated."
- **Stage 1 inside the VM is ours** (`TCR_EL1.TG0`/`TG1`), so we can ask for a 4 KB stage-1 granule — *if* the CPU implements it. **Check `ID_AA64MMFR0_EL1.TGran4` on the target part; do not assume.** If 4 KB stage-1 is unavailable, guest 4 KB pages cannot be shadowed 1:1 and the entire fast-path premise collapses into per-access permission checking.
- **Stage 2** was 16 KB-granule-only until **macOS 26.0**, which adds `HV_IPA_GRANULE_4KB`. With a 16 KB stage-2 granule, SMC write-protection and MMIO trapping are 4x coarser than the guest's 4 KB pages, producing false traps on adjacent data. macOS 26 fixes this; older macOS does not.
- x86 2 MB and 1 GB large pages map cleanly onto ARM64 4 KB-granule block descriptors (2 MB block, 1 GB block). Under a 16 KB granule they do not (32 MB / 64 GB blocks), and Captive's n:m power-of-2 rule degrades to 128 host PTEs per guest 2 MB page.

**(2) x86 Accessed/Dirty bit emulation — a cost Captive never paid.** x86 hardware sets A and D in the *guest's own* PTEs during a page-table walk, and Linux/Windows depend on it. In a shadow scheme the hardware updates *our* descriptors, not the guest's. The standard fix is to install shadow entries read-only / access-flag-clear and take a fault to set the guest's A/D bits before upgrading — i.e. **two extra faults per page per cycle of the guest's reclaim algorithm**. ARMv8.1 `FEAT_HAFDBS` sets AF/DBM in *host* descriptors, which does not help directly; it can only be used as a cheap "was this shadow entry touched" oracle for lazily propagating A/D into guest PTEs. Budget for this explicitly; it is a known shadow-paging tax (see the Adams & Agesen and Bhargava lines of work in §5).

**(3) The AP lattice cannot express x86's permission cross-product — this forces dual shadow trees.** x86 has *independent* U/S and R/W bits ANDed down the walk. ARM encodes access with **AP[2:1]**, a **monotone** four-point lattice ({EL1 RW/EL0 none}, {EL1 RW/EL0 RW}, {EL1 RO/EL0 none}, {EL1 RO/EL0 RO}) in which **EL1 always has at least the access EL0 has**. Unrepresentable legal x86 states include:
- **user-writable but supervisor-read-only**
- **user-RW while supervisor has no access**
- **`CR0.WP = 0`** — a ring-0 write to a read-only user page succeeds (used by real kernels during early boot and by kernel self-patching paths)

The standard fix is **two shadow stage-1 trees: one used while the guest is at CPL0, one while at CPL3**, switched on the EL0↔EL1 transition we are already making. That also cleanly handles SMEP/SMAP. Budget for it: it doubles shadow-table memory and doubles the fault-fill work. Rough correspondences otherwise: **SMEP ≈ PXN**, **SMAP ≈ FEAT_PAN** (UAO for the deliberate-override case; note `EFLAGS.AC` is set/cleared by ordinary guest instructions, so the PAN mapping must be maintained in translated code, not only at mode switches), **NX ≈ UXN/PXN**. **FEAT_S1PIE** (Armv8.9/9.4) permission indirection would relax the lattice considerably — **[VERIFY]** whether any Apple silicon exposes it.

**(3b) Break-before-make — a correctness trap with no x86 analogue.** ARM *requires* BBM when changing a live translation's output address, size, or attributes (relaxed only by FEAT_BBM levels 1/2): invalid entry → `DSB` → `TLBI` → `DSB` → `ISB` → new entry. **x86 permits atomic PTE replacement with no such protocol.** A shadow that naively mirrors guest PTE edits will violate BBM and can raise a **TLB conflict abort**. Design this in from the start.

**(3c) PCID (12 bits, 4096) vs ASID (8 or 16 bits).** `TCR_EL1.AS` selects 8-bit or 16-bit ASIDs. Mapping up to 4096 guest PCIDs (or an unbounded set of guest CR3 values) onto host ASIDs needs a **reclaim / generation-rollover allocator**; with only 8-bit ASIDs it becomes a real bottleneck. **[VERIFY `TCR_EL1.AS` width on the target chip.]** Stage 2 adds a separate **VMID** (8 or 16 bits with FEAT_VMID16), which Apple controls, not us.

**(3d) No real mode, and the A20 gate.** There is no ARM equivalent of real mode, big real mode, or the `CR0.PG`/`EFER.LME`/`CR4.PAE` transition sequence. Practical structure: **interpret (or translate against a flat identity map) until the guest sets `CR0.PG`**, then switch to the hardware-MMU shadow path. The **A20 gate** is an address-line mask with no ARM analogue and must be folded into address generation, or handled with a stage-2 remap while active.

**(3e) Self-referencing page tables.** Windows installs a self-map PDE exposing its page tables through a virtual window. Under shadowing this matters twice: write-protection of guest PT pages must also cover the self-map's **virtual aliases**, and the guest can *read* its own PTEs and observe A/D bits, so lazy A/D propagation must be correct through that window.

**(4) x86 segmentation.** `FS`/`GS` bases (`MSR_FS_BASE`, `MSR_GS_BASE`, `MSR_KERNEL_GS_BASE`, `swapgs`, `wrfsbase`/`wrgsbase`) are used pervasively by Linux (per-CPU data), glibc (TLS), and Windows. ARM64 has `TPIDR_EL0`/`TPIDRRO_EL0`/`TPIDR_EL1` but **no segmented addressing** — every `%fs:`/`%gs:`-prefixed access becomes an explicit base+offset. Ironically Captive *consumed* x86's `FS`/`GS` for its own state pointers; here we must both emulate them for the guest and find our own equivalents (`TPIDR_EL1` + pinned GPRs). `swapgs` semantics (swap on ring transition) interact with our EL0↔EL1 transitions and need care.

**(5) No cheap "different flavour of VM exit" to tune against.** Captive compared MMIO exits vs PIO exits and then avoided both. Hypervisor.framework offers only exception-based exits (`HV_EXIT_REASON_EXCEPTION` with ESR/FAR/IPA) and `HVC`. So Captive's *conclusion* (shared-memory barrier, never exit) is the only option worth pursuing, not one of several.

**(6) macOS-specific process constraints.**
- The `com.apple.security.hypervisor` entitlement is mandatory; unsigned/unentitled binaries cannot create a VM.
- Host-side W^X (`MAP_JIT`, `pthread_jit_write_protect_np`) applies to code the *host process* executes. Our code cache lives in guest memory mapped via `hv_vm_map`, so the guest side is governed by `HV_MEMORY_EXEC/UEXEC` instead — but verify that a region mapped `WRITE|EXEC` at stage 2 is actually permitted, and design for W^X-with-flip if it is not.
- `hv_vm_allocate` (macOS 15+) exists specifically to get memory suitable for guest mapping; prefer it over raw `mmap`.

**(7) TSO — the one problem neither Captive nor HyperMAMBO faced, and the highest-risk unknown.** Both had ISA pairs with a compatible or stronger host memory model (x86 host TSO, ARM guest weak — the *easy* direction). This project has the **hard** direction: a TSO guest on a weakly-ordered host.

The good news, from Apple's own documentation ([Accelerating the performance of Rosetta](https://developer.apple.com/documentation/virtualization/accelerating-the-performance-of-rosetta)): the control is **`ACTLR.TSOEN`** — set to 1 the CPU uses the **x86-64 TSO memory model**, cleared it reverts to standard ARM64 ordering. Apple ships **Linux kernel patches** ([RosettaPatch.zip](https://developer.apple.com/download/files/RosettaPatch.zip)) that context-switch `ACTLR.TSOEN` per thread and expose `prctl(PR_SET_MEM_MODEL, PR_SET_MEM_MODEL_TSO)`; upstream equivalent is Hector Martin's [arm64 TSO series](https://lwn.net/Articles/969484/). Critically, **that path is an ARM64 guest kernel inside a `Virtualization.framework` VM toggling TSOEN**, so the bit is reachable from *some* guest EL1.

**The open question is whether `Hypervisor.framework` traps ACTLR accesses via `HCR_EL2.TACR` for an arbitrary guest, and whether such a trap can be satisfied.** If TSOEN is reachable, we delete the single largest cost in every userspace x86-on-ARM translator. If it is not, the fallback is conservative barrier insertion, which the literature (§6.6) measures at **48% of runtime average, up to 75%** (Risotto) and **"near a 10× hit"** on memory-heavy code (FEX) — larger than every gain Captive's MMU trick delivers. **Settle this experimentally before anything else.**

**(8) Multi-core.** Captive is single-core and says so. Multi-vCPU adds: cross-vCPU shadow-table shootdown (guest `INVLPG`/`INVPCID`/`MOV CR3` must invalidate *other* vCPUs' shadow tables — ARM64's broadcast `TLBI ...IS` variants help, but only for our own tables); SMC detection races; and correct x86 atomics. Martin Kristien's 2024 Edinburgh thesis (§1.10) is the state of the art on the last point and shows that the common LL/SC↔CAS shortcut is *divergent from real hardware*.

**(9) Retargetability is probably the wrong trade here.** Captive's ATC'19 headline contribution — generating the translator from an ADL — cost a **2.6x slower JIT** and Captive's own table lists x86-64 as its hardest guest ("complex instruction encoding, requiring stateful decoder" → they used an external decoder library). With exactly one guest and one host, hand-write the translator (FEX-Emu / Box64 / Rosetta-style) and take Captive's *systems* mechanisms rather than its GenSim front end.

### 7.4 The honest expectation

Captive's own numbers set the bar: **2.2–2.5x faster than QEMU full-system**, **~40% of a same-clock native core**, **~1.4x slower than a real (older, slower) chip**, and still **7.24x slower than native execution on the host**. That was for a *simpler* guest (ARM) on a *more capable-for-this-purpose* host (x86 with 4 KB pages, TSO, and hardware A/D). An x86-64-guest-on-ARM64-host system faces additional taxes — A/D emulation, `CR0.WP`, segmentation, flags, and above all TSO — that Captive did not pay. HyperMAMBO's near-native result is not a counter-example: it achieves that precisely by **not** emulating the MMU at all, which is only possible when the guest kernel is native. A full-system x86-on-ARM emulator cannot use that escape hatch.

The realistic target is therefore "**substantially better than QEMU full-system, well short of Rosetta 2's user-mode numbers**", and the design's whole job is to make the MMU and device paths cost nothing so the remaining budget can be spent on flags, TSO, and indirect branches.

### 7.5 Concrete design decisions this literature supports

1. Execution engine as a **bare-metal ARM64 unikernel inside a Hypervisor.framework VM**; device models in the host process. *(Captive)*
2. Guest CPL0 → EL1, CPL3 → EL0; enforce guest U/S with the host's AP bits. *(Captive)*
3. Guest x86-64 low half → `TTBR0_EL1`, high half → `TTBR1_EL1`; **no masking, no CR3-style switch**. *(new; fixes Captive's worst 64-bit cost)*
4. Shadow stage-1 tables filled **on fault**, with full permissions pre-populated so each page faults once. *(Captive)*
5. Identity map of guest physical memory in the engine's regime for software x86 page-table walks. *(Captive, HyperMAMBO)*
6. **Code cache indexed by guest physical address**, blocks terminated at page boundaries, chaining restricted to same-page. *(Captive)*
7. SMC detection by **stage-2 write protection** (`hv_vm_protect`) of physical pages from which code was translated; drop protection after invalidation. *(HyperMAMBO's variant of Captive's scheme, and it maps directly onto `hv_vm_protect`)*
8. Shadow tables keyed on guest **CR3/PCID**, one set per key, invalidated on the corresponding guest TLB operation. *(Captive's ASID scheme + its own stated CR3 fallback)*
9. Device MMIO detected by a **stage-1 not-present fault inside the VM**, serviced by a **two-barrier shared-memory handoff to a parked host thread** — never a VM exit. *(Captive)*
10. Interrupts injected with `hv_vcpu_set_pending_interrupt`, latched to a flag by our own `VBAR_EL1` handler, checked at block boundaries; async precise state recovered by **running to end-of-fragment with interrupts masked**. *(Captive + HyperMAMBO)*
11. **Hardware FP/SIMD with inline fix-ups**, never a softfloat helper call; document the x87-80-bit compromise. *(Captive)*
12. **Two shadow stage-1 trees**, one for guest-CPL0 and one for guest-CPL3, switched at the EL0↔EL1 transition — because ARM's AP lattice is monotone and cannot express x86's U/S × R/W cross-product or `CR0.WP = 0`. *(new; not needed for Captive's ARM guest)*
13. **Keep `hv_vm_map()` stage-2 mappings as coarse as possible** — ideally huge contiguous blocks over the guest-RAM slab — so the two-dimensional walk's nested dimension collapses. Bhargava et al. put the worst case at **24 page-entry references** for a 4-level/4-level 2D walk, and measure **21–43% fewer TLB misses** from 2 MB vs 4 KB nested pages. *(new; §5.1)* Use fine-grained stage-2 protection only for SMC-watched and MMIO pages.
14. **Two-tier compilation**: a fast in-VM translator on the hot path plus a heavy optimiser in the *host process*, reached over the same shared-memory channel used for devices. This is HQEMU's structure (**2.4×/4× over QEMU**, §4.3) and it directly answers Captive's admitted 2.6× slower JIT — which Captive could not fix because its unikernel has no threads and cannot host LLVM.
15. **AOT-primary, JIT-fallback for guest userspace.** Rosetta 2 and Arancini independently converged on this (Arancini: **≤2.1% of unique instructions translated dynamically**). Not applicable to the guest kernel, which is discovered at runtime.
16. Investigate running the engine at **guest EL2** (`hv_vm_config_set_el2_enabled`) for a clean regime split and an engine-owned stage 2 — **but do not make it a hard dependency**, since it is M3+ / macOS 15+ and the design does not need it. *(new capability; no prior art)*
17. **Settle the TSO question experimentally first.** No amount of MMU cleverness survives a 48%-of-runtime barrier tax.
18. **Handle break-before-make correctly from day one** when updating live shadow entries; x86 has no equivalent protocol and a naive mirror of guest PTE edits will raise TLB conflict aborts.
19. **Adopt SimBench as the evaluation harness** (§6.8) so results are directly comparable with Captive's published per-category numbers.

---

## 8. Master comparison table

Columns: **Applicability** is graded for *this* project (x86-64 guest, ARM64 Apple Silicon host, `Hypervisor.framework`).

| # | System | Year | Host ISA | Guest ISA | Sys? | Key mechanism | Reported speedup vs named baseline | Limitations | Applicability to Apple Silicon + Hypervisor.framework |
|---|---|---|---|---|---|---|---|---|---|
| 1 | **Captive** | 2016 (TACO) / 2019 (ATC) / 2020 (TOCS) | x86-64 + Intel VT (KVM) | ARMv7-A, **ARMv8-A**; also x86-64, RISC-V, C6x, Mali (partial) | **Yes** | Bare-metal unikernel DBT inside a same-ISA hardware VM; guest PTEs → host PTEs on demand; guest privilege level → host ring; physical-address-indexed code cache; barrier-based MMIO without VM exit | **2.21×** SPEC int / **6.49×** SPEC FP over QEMU 2.12 (ARMv8 guest); **2.5×** over QEMU 2.4 (ARMv7); **2.05×** vs HSPT's 1.94× over Android Emulator; ~**40%** of a 2.0 GHz A57; **1.4×** slower than an ODROID-XU vs QEMU's 3.51×; I/O cached reads **10.8×** QEMU | Single-core; MMU mapping only works for behaviourally-similar MMUs; 64-on-64 needs masking + page-table switch; hard KVM dependency; ADL can't express prefixes/VLIW/delay slots; all devices in software; no nested virt; JIT 2.6× slower than QEMU | **Highest — this is the blueprint.** Every mechanism maps; TTBR0/TTBR1 makes the 64-on-64 problem *better* than Captive's; `hv_vm_protect` gives stage-2 SMC protection Captive had to do in stage 1 |
| 2 | **HyperMAMBO-X64** | 2017 (VEE) | AArch64 (KVM/EL2) | AArch32 (**user mode only**) | Hybrid | Hypervisor traps AArch64→AArch32 EL0 switches; DBT + translated code run at EL0; **guest kernel runs native so page tables need no translation**; ASID-tagged fragments; trapped TLBI; stage-2 write protection for SMC | **1.011× faster than native** AArch32 on SPEC CPU2006 (matching MAMBO-X64's 1.010×); syscall µbench **10.63 s** vs native 1.00 s | Requires an AArch64 guest kernel — not a full-system translator; no architectural way to trap the mode switch (kernel was patched); no memory-mapped-file awareness → no file-backed persistent cache; JIT-heavy guests suffer from page-granularity WP; up to **13%** worse than MAMBO-X64 on gcc/cactusADM | **Medium-high for mechanisms, zero for the model.** Its escape hatch (native guest kernel) is unavailable to us. But ASID-tagged fragments, TLBI-trap filtering, stage-2 SMC protection and 3-tier precise-state recovery all transfer directly |
| 3 | **MAMBO-X64** | 2016–17 (TACO/PLDI) | AArch64 | AArch32 (user) | No | Hardware-assisted returns driving the hardware RAS, branch-table inference, fast atomic hash tables, NET traces, speculative address generation | **~10% overhead** → **1.0% faster than native** with ReTrace; hardware returns cut DBT overhead **40% avg, up to 90%** | User mode; same-family ISA pair | **High for indirect-branch technique.** The hardware-RAS trick is the reason Rosetta 2 translates CALL/RET to BL/RET |
| 4 | **Transmeta CMS** | 2000/2003 | Proprietary VLIW | x86 (**full system incl. BIOS**) | **Yes** | Interpret→profile→translate ≤200-instr regions; commit/rollback via shadow registers + gated store buffer; alias hardware; I/O-reorder trap; sub-page write protection; adaptive retranslation with monotonic policies | Ablations: no reordering **−33.14%** apps / −10.09% boots; no alias HW **−23.53%** apps; no fine-grain WP up to **3.8×** slowdown; self-revalidation **+28%** Quake fps | No escape valve; translator cost significant; never matched native x86 perf; all assists were custom silicon | **High for policy, low for hardware.** No commit/rollback or alias hardware on Apple Silicon; the **adaptive-retranslation policy machinery and SMC taxonomy transfer verbatim** and address failure modes user-mode translators never see |
| 5 | **IBM DAISY** | 1997 | 8-issue tree VLIW | PowerPC (**all code incl. kernel**) | **Yes** | VMM in ROM; translation of guest physical page *n* at VLIW VA `n×4 + VLIW_BASE`; per-register exception tags; speculative MMIO→no-op; fine-grain code-modification interrupt | ILP **2.5×** at 8-issue on SPECint95; **4×** code expansion; **4,315** instr/instr to translate (vs 65–100k for static compilers, within 20% of their quality) | Simulated, with kernel routines called natively; 4× bloat; cross-page branches expensive; never shipped | **Medium.** The physical-address-derived translated-code addressing is the purest form of Captive's physical indexing; the amortisation analysis is the reference for AOT-vs-JIT decisions |
| 6 | **BOA** | 1999–2000 | 6-issue VLIW | PowerPC | Yes | DAISY assists, frequency-first design | *[unverified]* | *[unverified]* | Low |
| 7 | **HP Dynamo** | 2000 | PA-8000 | PA-8000 (same ISA) | No | MRET trace selection with no path profiling; 150 KB fragment cache; bail out to native when unstable | **+9% avg, >22% max** over `+O2` natives; **<1.5%** steady-state overhead; interpretation alone **40×** native; fragment *optimisation* only worth ~3% — **trace selection is the win** | Same-ISA; has a bailout a cross-ISA system cannot have | **Medium.** Establishes that trace formation, not peephole optimisation, is where DBT performance comes from |
| 8 | **Intel IA-32 EL** | 2003 | Itanium | IA-32 (user) | No | Two-phase: fast BB translation + profiling → superblock optimisation hot-swapped in; one translator body for Windows and Linux | Profiling costs **2–4%**; performance ≈ a **1.5 GHz Xeon MP**; beat Itanium's *hardware* x86 decoder, which Intel then deleted in Montecito | User mode; x87 80-bit precision and misalignment called out as hard | **Medium.** The strongest evidence that software translation beats bolted-on hardware decode; the two-phase tiering model is directly reusable |
| 9 | **DEC FX!32** | 1996–99 | Alpha / NT | x86 Win32 (user) | No | Emulate + profile on first run; **background static translator emits native Alpha DLLs**; profile DB accumulates across runs; API calls routed to native DLLs | **40–50% of native x86** | Not faithful (64-bit not 80-bit FP); no NT Debug API; needs offline passes and per-app persistent state | **Medium.** Direct ancestor of Rosetta 2's persistent AOT cache |
| 10 | **Apple 68k→PPC** | 1994 | PowerPC | 68EC040 user subset | No | ROM interpreter (Davidian) then dynamic recompiler (Traut); A-line traps redirected to native code; UPPs + Mixed Mode Manager | — | **No MMU, no FPU** emulation; only app code emulated, OS native | Low (historical) |
| 11 | **Rosetta 1 / QuickTransit** | 2004–2011 | x86 | PowerPC (user) | No | Transitive QuickTransit | — | No G5 instructions, no AltiVec, no kexts | Low |
| 12 | **Rosetta 2** | 2020– | **Apple ARM64** | **x86-64 (user)** | No | **AOT-first**: whole `__TEXT` translated once to disk (`/var/db/oah/`); JIT only for unknown indirect targets; CALL/RET→BL/RET for hardware RAS; LDP/STP pairing; SSE2→NEON | **~71% of native** (7-zip), **>90%** memory-bound, **~70–80%** fp2017, worst 49.87% (502.gcc_r); code expansion **1.64×** | **User mode only** — no x86 kernel, ring 0, or page tables; no 32-bit x86; no kexts; scheduled for removal in macOS 28 | **Critical reference point and the performance bar.** Its hardware assists (TSO mode, PF/AF in NZCV[27:26], FlagM2, alternate-FP mode) are exactly what we need to determine reachability of from guest EL1/EL0 |
| 13 | **Rosetta 2 Linux-VM mode** | 2022– (macOS 13+) | Apple ARM64 host, ARM64 Linux guest | x86-64 Linux ELF (user) | No | virtiofs share tagged `rosetta` + `binfmt_misc`; x86-64→ARM64 *syscall* translation; `rosettad` AOT cache over `/run/rosettad/rosetta.sock` | Same engine as native Rosetta 2 | Needs an ARM64 Linux guest and x86-64 guest libraries; user mode; synthetic `/proc/cpuinfo` | **High as a baseline to beat.** It is the closest shipping thing to "x86 workloads in a VM on Apple Silicon", and it *cannot* run an x86 kernel — which is exactly this project's differentiator |
| 14 | **NVIDIA Denver / Carmel** | 2014 / 2018 | Proprietary VLIW | ARMv8-A (same family) | Below-OS | Hardware ARM decoder for cold code + software DCO for hot; **128 MB optimisation cache hidden from the OS**; hardware TSO | — | Warm-up sensitive; 128 MB memory tax; never cross-ISA | **Medium.** Confirms hardware TSO is a real, shipped, general design point; "concealed memory" maps to guest RAM we never advertise |
| 15 | **LoongArch LBT + LATX** | 2021– | LoongArch LA64 | x86-64 / ARM / MIPS (user) | Partial (LATX-SYS in progress) | Hardware `eflags` register, `scr0`–`scr3` scratch registers, x87 `ftop` in hardware; LATX = modified QEMU 6 + LSX/LASX + AOT + library pass-through | Qualitative only; TACO instruction-inflation study *[unverified]* | User space; requires LSX+LBT_X86; no 16-bit real mode | **Medium.** The clearest modern statement of *which x86 state belongs in hardware*: flags, x87 TOS, scratch registers. Apple has 2 of the 3 |
| 16 | **Elbrus / Lintel** | ~2005– | Elbrus VLIW | x86 / x86-64 (**full system**, boots Windows/Linux/QNX) | **Yes** | Adaptive multi-pass BT + Elbrus BT-support hardware (speculative execution with diagnostic tags, predication, tagged memory) | **~20% overhead** — a *stated design goal*, **not measured** *[unverified]* | Primary literature Russian-only; unverifiable claims | **Low-medium.** Proof by existence that full-system x86 BT can boot unmodified OSes, but with custom silicon |
| 17 | **Zhaoxin** | — | — | — | — | **Not a binary-translation host** — ships native x86-64 silicon | — | — | **N/A** — does not belong in this taxonomy |
| 18 | **MagiXen** | 2007 | Itanium + Xen | IA-32 (paravirtualised guest OS) | Yes-ish | IA-32 EL reused as the DBT inside Xen | — | **Only paravirtualised guests**; still pays x86 page-table translation; **x86 has no tagged TLB, so every context switch flushes the TLB and invalidates all translated code** | **Medium as a cautionary tale.** Its central failure mode (untagged TLB destroying the code cache) does not apply on ARM64 — ASIDs save us |
| 19 | **PinOS** | 2007 | x86 + Xen | x86 whole-system instrumentation | Yes | Pin extended below the OS under Xen | **~50× slowdown** | Runtime checks for page-table modification | **Low, but a useful floor.** Shows what happens without hardware-assisted page-table-change detection |
| 20 | **Embra** | 1996 | MIPS | MIPS (same ISA) | Yes | Software TLB + direct-mapped relocation array | **3–9× slowdown** | Same-ISA, so a floor not a cross-ISA result | **Low-medium.** The origin of the "relocation array" idea that HSPT and Captive generalise |
| 21 | **HSPT** | 2014 (VEE) | x86-64 Linux | ARM (system) | Yes | Embedded shadow page tables built with host `mmap`; "Private SPT" per guest ASID | **1.94×** over the Android Emulator (Captive: 2.05×) | Bounded by host `mmap`/`mprotect` API and syscall cost; needs guest ASIDs | **High conceptually.** This is what you build if you *cannot* get a hardware VM. Useful as a fallback design and as the direct comparison Captive chose |
| 22 | **BTMMU** | 2021 (VEE) | MIPS64 (+ HW extension) | IA-32 / x86-64 | Yes | Low-cost host-MMU hardware extension + kernel module + patched QEMU | **1.40×** (IA32→MIPS64), **1.36×** (x86_64→MIPS64) over QEMU on CINT2006 | Requires a custom host MMU extension | **Medium.** Its modest 1.4× is the best evidence for how much of the total is *actually* softmmu |
| 23 | **Risotto** | 2023 (ASPLOS) | ARMv8.1 | x86-64 | user (QEMU-based) | Formally verified minimal x86→ARM memory-model mappings, fence merging, `DMBLD`/`DMBST` instead of `DMBFF`; cross-ISA native library linking | **Up to 19.7%, 6.7% avg** over QEMU; measures **fences = 48% of runtime avg, up to 75%**; library linking 1.4–23× | Multithreaded PARSEC on a 112-thread machine amplifies fence cost | **Critical.** Its 48%/75% measurement is the single strongest argument for settling the hardware-TSO question first |
| 24 | **FEX-Emu** | ongoing | AArch64 | x86 / x86-64 (user) | No | NZCV-mapped deferred flags, PF/AF in dedicated registers; hardware TSO via `prctl` on Asahi | Flags rework: **Geekbench +17.6%, Bytemark +60%, >2× FPS**; overall **26%** of native (7-zip, RPi 400) | User mode; memory-model emulation is "the number one thing that slows down FEX" | **High.** Best open-source reference for x86→ARM64 flag and TSO handling |
| 25 | **Box64** | ongoing | AArch64 | x86-64 (user) | No | DBT + **native host library pass-through** (libc, libGL) | **57%** of native (7-zip, M1 Linux), **53%** (RPi 400); **100%** on GPU-bound openarena | User mode; library pass-through changes semantics | **High as a performance reference**, low as a design (library pass-through is unavailable to a full-system emulator) |
| 26 | **QEMU TCG (system)** | 2005– | retargetable | retargetable | **Yes** | Software MMU with software TLB; virtual-address-indexed code cache; softfloat | **18.73×** slowdown vs native on CINT2006 (QEMU 6.1); memory emulation is **38.1%** of runtime; `helper_lookup_tb_ptr` **6.29–16.91%** | The baseline everyone beats | **The baseline.** Also the source of most of the negative results that motivate this project |
| 27 | **HQEMU** | 2012 (CGO) | x86-64 | x86 (retargetable) | Both | TCG fast path on the execution thread + **LLVM optimiser on other cores**, hot traces found via hardware perf counters | **2.4× (SPEC INT) / 4× (FP)** over QEMU | Needs a *modified* LLVM (pinned to LLVM 6.0 + an old QEMU); inherits TCG IR limits | **High.** The answer to Captive's 2.6×-slower JIT: put the optimiser in the host process, not the unikernel |
| 28 | **Instrew / Rellume** | 2020–21 (VEE) | x86-64 | x86-64 (user) | No | Lift machine code **directly to LLVM IR**; "facets" (typed views per architectural register); instrumenter in a separate process; guest FS/GS forwarded to native segment registers | SPEC CPU2017: **53% overhead** (67 INT / 40 FP), best case **7%**, vs HQEMU 124% and QEMU 5.2 **716%** | User mode; fastest configs assume same/near-ISA host | **Medium.** The FS/GS-to-native-register trick has a `TPIDR_ELx` analogue; the LLVM-IR-direct lifting is an alternative to Captive's domain-specific IR |
| 29 | **Qelt** (Cota & Carloni) | 2019 (VEE) | x86-64 | x86-64 / AArch64 (system) | **Yes** | Dynamic TLB resizing (100 ms window, 70%/30% thresholds), host-FPU with flag guards, per-vCPU indirect-branch cache | **1.76× INT / 2.18× FP** over QEMU; 1.5–3× over prior cross-ISA full-system instrumentation; matches Pin for cache simulation | Still a softmmu — improves the constant, not the asymptote | **High as the cost model.** Its 3-item softmmu breakdown is exactly what a hardware MMU deletes |
| 30 | **Pico** (Cota et al.) | 2017 (CGO) | Skylake / POWER8 / Opteron | x86-64, AArch64 | Both | Better TB hashing (QHT); store-instrumentation for correct LL/SC | **Pico-user 20–90% faster than QEMU-user**; **Pico-system ≈ QEMU-system** (locks/fences cancel the win); scales to **64 threads**; atomics ~2× on CINT2006 | The system-mode result is the sobering one | **High.** Evidence that most full-system cost is *not* in the translator, which is the whole argument for attacking the MMU instead |
| 31 | **Lasagne** | 2022 (PLDI) | ARM64 | x86-64 (static) | No | Verified static x86→ARM64 memory-model mapping | Fences reduced **up to ~65%, 45.5% avg** | **Static-only** — cannot lift Phoenix benchmarks at all (jump tables, function pointers) | **Medium.** The fence-minimisation result is what you fall back to if `ACTLR.TSOEN` is unreachable |
| 32 | **Arancini** | 2026 (ASPLOS) | ARM64, RISC-V | x86-64 (user) | No | **ArancinIR** shared by a static AOT translator and a dynamic fallback (hybrid BT); formally verified memory-model mappings; first to handle **mixed-size accesses** on ARM; native library forwarding | Phoenix 16T geomean vs native: **8.01× (ARM)**, **6.01×** with native libs; **up to 5× faster than Risotto/QEMU**; only **≤2.1%** of unique instructions translated dynamically; fence merging worth up to **1.4×**; dead-flag elimination worth little | User mode, not full-system; still 4–8× native; static phase needs the binary ahead of time | **High.** Current SOTA from the Captive lineage (Spink is a co-author). The AOT-primary / ≤2.1%-JIT shape is right for guest userspace, wrong for a guest kernel |
| 33 | **Adams & Agesen** | 2006 (ASPLOS) | x86 + VT-x | x86 | Yes | Comparison of software BT vs first-gen hardware virtualisation | `ptemod` nano-benchmark: native **1 cycle**, software-BT VMM **~400 cycles**, hardware VMM **~11,000 cycles** | First-gen VT-x had no MMU virtualisation support | **Critical as a warning.** Hardware assist only wins if exits are rare. Captive sidesteps it: guest PTE writes are ordinary translated stores, and the shadow is rebuilt lazily on fault — **no per-PTE trap at all** |
| 34 | **Bhargava et al.** | 2008 (ASPLOS) | x86 + AMD NPT | x86 | Yes | Two-dimensional page-walk acceleration: nested page-walk caching + Nested TLB | 2D walk needs **n·m+n+m = 24** references for 4-level/4-level (**6×** a native walk); page-walk-cache extension **15–38%**; Nested TLB **+7%**; **2 MB nested pages → 21–43% fewer TLB misses** | Requires hardware changes | **High.** The quantitative argument for keeping `hv_vm_map` stage-2 mappings coarse |
| 35 | **Agile Paging** | 2014 (ISCA) | x86 | x86 | Yes | Switch between shadow and nested paging *mid-walk* | *[unverified — PDF not retrieved]* | — | **Medium.** This project is effectively a static form of it: shadow at stage 1, nested at stage 2 |

---

## 9. Open questions and first experiments

Ranked by how much of the design they invalidate if the answer is bad.

| # | Question | How to answer | If the answer is bad |
|---|---|---|---|
| 1 | **Can a `Hypervisor.framework` guest at EL1 set `ACTLR_EL1.TSOEN`?** Or does `HCR_EL2.TACR` trap it? | Write a minimal VM, attempt the MSR from guest EL1, observe whether `hv_vcpu_run` returns `HV_EXIT_REASON_EXCEPTION` with an ESR indicating a trapped system-register access. Apple documents the bit and ships Linux patches that use it *inside a VM*, so there is reason for optimism. | Fall back to verified minimal fence mappings (Risotto/Lasagne/Arancini). Expect **48% of runtime average, up to 75%** on multithreaded workloads (§6.6), or **"near 10×"** on memory-heavy code (FEX). This alone can sink the project's performance case. |
| 2 | **Does stage-1 support a 4 KB granule?** `ID_AA64MMFR0_EL1.TGran4`. | Read `HV_FEATURE_REG_ID_AA64MMFR0_EL1`. | Every 4 KB guest page must be handled at 16 KB granularity: a shadow entry can only be installed when **all four** 4 KB guest pages inside a 16 KB host page agree on permissions; otherwise software-emulate those accesses. Captive's *n:m* rule applies but the fast path degrades badly. |
| 3 | **What is `hv_vcpu_run()` round-trip latency?** | Microbenchmark: guest `HVC` in a loop, measure host-side. Compare against Adams & Agesen's ~11,000-cycle VT-x figure. | If exits are expensive, Captive's "never exit — shared-memory two-barrier handoff with a parked host thread" becomes mandatory rather than merely preferable. |
| 4 | **`ID_AA64MMFR1_EL1.HAFDBS`** — hardware Access Flag / dirty-bit management? | Read `HV_FEATURE_REG_ID_AA64MMFR1_EL1`. | Every first touch and first write to a guest page costs a fault to set the guest's x86 A/D bits. Note that **even with HAFDBS, ARM updates *your shadow* entries, not the guest's x86 PTEs**, so back-propagation is required either way. |
| 5 | **`TCR_EL1.AS`** — 8-bit or 16-bit ASIDs? | Guest-side read after setting up EL1. | An 8-bit ASID space against a 12-bit guest PCID space (or unbounded CR3 values) needs an aggressive reclaim/generation allocator and will thrash. |
| 6 | **Is `HV_MEMORY_WRITE \| HV_MEMORY_EXEC` on the same stage-2 region actually permitted?** | Try it. | Design for W^X-with-flip in the code cache. Note the code cache lives in *guest* memory, so host-side `MAP_JIT` rules may not apply — but verify rather than assume. |
| 7 | **`hv_vm_config_get_el2_supported()`** on the target chips. | API call. | Nothing fatal — the design needs only one EL1. Guest EL2 is an optimisation (clean regime split, engine-owned stage 2) and a prerequisite only for emulating guest VT-x. |
| 8 | **`HV_IPA_GRANULE_4KB`** availability (macOS 26+). | `hv_vm_config_get_default_ipa_granule()` / attempt `set`. | SMC write-protection and MMIO trapping are 4× coarser than the guest's 4 KB pages, producing false traps on adjacent data. Workable, but a measurable tax. |
| 9 | **`FEAT_S1PIE`** (permission indirection) on any Apple part. | `sysctl hw.optional.arm.*` / ID register read. | Keep the dual-shadow-tree design (§7.3(3)), which works regardless. |

**Recommended order of work:** (1) → (2) → (3) in a throwaway probe harness, *before* writing any translator code. Questions 1 and 2 together determine whether the Captive architecture is viable on this host at all; question 3 determines the device/MMIO design.
