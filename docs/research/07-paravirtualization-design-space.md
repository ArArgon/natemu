# 07 — Para-virtualization Design Space for Cross-ISA System Emulation

**Question:** the project premise is *"para-virtualization to reduce hardware emulation overheads"* for an
x86-64 guest on an ARM64 (Apple Silicon) host. Para-virtualization means **the guest is modified**. That
single word forecloses a large part of the product space. This document works out (a) what scope
para-virtualization actually forces, (b) what the historical PV designs are worth *to a cross-ISA
translator specifically* (which is not the same as what they were worth to a same-ISA hypervisor),
(c) which PV ideas are genuinely new here, (d) the measured numbers, and (e) three scope points with
effort estimates.

**Environment measured while writing this doc:** Apple M2 Max (`Mac14,5`), `hw.pagesize = 16384`,
`kern.hv_support = 1`. Hypervisor.framework headers inspected at
`/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Hypervisor.framework/Headers/`
— confirms the arm64 surface: `hv_vm_map(hv_uvaddr_t uva, hv_ipa_t ipa, size_t, hv_memory_flags_t)` /
`hv_vm_protect(hv_ipa_t ipa, size_t, hv_memory_flags_t)`, both documented as requiring a
**"page aligned address"** and a **"multiple of the page size"** — i.e. **16 KiB granularity at stage 2**
(see §3.1). The SDK also ships `hv_gic*.h`, so the framework provides an in-kernel GICv3 model; for an
x86 guest the LAPIC/IOAPIC model still belongs inside the DBT unikernel (§3.4).

---

## 0. Bottom line up front

Five conclusions, in descending order of how much they should change the plan:

1. **The premise is mostly wrong, but for an interesting reason.** Almost every classical PV feature
   (PV-MMU, PV-EOI, PV-IPI, PV syscall, PV spinlocks) exists to **avoid VM exits** or to **avoid trapping
   non-virtualizable instructions**. A dynamic binary translator has *neither problem*: it never traps,
   because it rewrites; and a call from translated code into a C device model costs tens of nanoseconds,
   not the ~1–2 µs of a hardware VM exit. **PV features whose value is "one fewer exit" lose one to two
   orders of magnitude of value in a DBT.** The features that survive are the ones that avoid *work*,
   not exits.

2. **The single biggest win available is not PV at all — it is using the host MMU instead of a software
   MMU, and it works on a completely unmodified guest.** Measured: HSPT/ESPT get **1.51×–1.59×** over
   QEMU system-mode purely from replacing softmmu with embedded shadow page tables; Captive gets
   **2.21×** on SPECint (and **32.4×** / **25.9×** on TLB-flush / TLB-evict microbenchmarks) largely from
   mapping guest page tables onto host page tables. None of that requires touching the guest.

3. **Apple Silicon has a hard constraint that changes the design and is the strongest genuine argument
   for PV in this project: the CPU does not implement the 4 KiB translation granule.** `hw.pagesize` is
   16384 on M-series, and Asahi Linux requires `CONFIG_ARM64_16K_PAGES` for exactly this reason. So the
   Captive trick — "map guest PTEs 1:1 onto host PTEs" — **cannot be done 1:1 for an x86-64 guest using
   4 KiB pages**, at stage 1 *or* stage 2. You are forced into Captive's degraded "n:m mapping with
   sub-page permission checks in translated code" path. A *paravirtualized guest kernel that allocates
   physical memory in 16 KiB-aligned, permission-uniform quads (or just uses 2 MiB mappings)* would
   restore the 1:1 mapping. **This is the one PV feature that is worth real money here and that nobody
   else has needed.** (§3.1, §6.4)

4. **"PV code-cache / SMC notification" is a much weaker idea than intuition suggests.** Linux x86 has no
   `flush_icache_range` to hook — on x86 it is architecturally a no-op, so the hook does not exist in the
   places that matter. Kernel code patching *does* have hooks (`text_poke_bp`, `apply_alternatives`, jump
   labels, ftrace, module load) but is rare and cheap. The expensive self-modifying code is in
   **userspace JITs** (V8, JVM, .NET, LuaJIT, glibc trampolines) which you cannot patch and which emit no
   notification on x86. §3.2.

5. **The pragmatic 80/20 — native ARM64 kernel + translated x86-64 userspace (the Rosetta-for-Linux
   model) — is the fastest design by a wide margin (reported 4–5× to >20× over QEMU, ~70–80 % of native),
   but it is a different product.** It cannot boot an x86 kernel, cannot run Windows, and cannot be used
   for firmware/driver/OS work. Also: Apple ships it for free in `Virtualization.framework`, and FEX-Emu
   / box64 are open-source alternatives. If that is the product, the differentiated engineering is small.
   If system-level emulation is the product, this option is off the table by definition. §3.6, §6.

**Recommendation in one line:** treat "para-virtualization" as an *optional, negotiated accelerator layer*
(the KVM model: advertise features, guest uses them if it has them, everything still works if it does
not) layered on a **hardware-MMU-accelerated, unmodified-guest** core. Do **not** make a patched guest
kernel a prerequisite. Details in §6.

---

## 1. Terminology discipline: four separable axes

Xen's own taxonomy is the cleanest way to stop the word "para-virtualization" from meaning four different
things. Xen identifies four axes that can each independently be *emulated* or *paravirtualized*
([Xen Project, "The Paravirtualization Spectrum, Part 2"](https://xenproject.org/blog/the-paravirtualization-spectrum-part-2-from-poles-to-a-spectrum/)):

| Axis | PV | HVM | PVHVM | PVH |
|---|---|---|---|---|
| Disk / network devices | PV (virtio-like) | Emulated (IDE, e1000) | PV | PV |
| Interrupts / timers | PV | Emulated (LAPIC/PIT/HPET) | PV | PV |
| Emulated platform (motherboard, BIOS, legacy boot) | None | Full | Full | None |
| Privileged instructions | PV (hypercalls) | HW extensions | HW extensions | HW extensions |
| Page tables | **PV-MMU** | Shadow / HAP | Shadow / HAP | HW extensions (EPT/NPT) |

The critical distinction for this project:

- **Axes 1 and 2 (devices, interrupts/timers) require only a *driver*, not a modified kernel.** Stock
  Linux, stock Windows (via Hyper-V enlightenments + virtio drivers), and stock FreeBSD all ship these.
  This is what people usually mean when they say "PV" today and it is **not** true para-virtualization.
- **Axes 4 and 5 (privileged instructions, page tables) require a modified kernel** — a different build,
  a different entry path, a hypercall ABI. This is true PV, and this is what forecloses Windows/macOS.

Everything downstream depends on which of these the project means. The rest of this document keeps them
strictly separate.

---

## 2. The scope fork: what each guest target costs you

### 2.1 The four realistic targets

| # | Guest target | Guest modified? | Can run Windows / macOS-x86? | What the emulator must implement | What it gets to skip |
|---|---|---|---|---|---|
| **(a)** | **Patched Linux kernel** (`CONFIG_NATEMU`, Xen-PV-style) | Yes — kernel build + ongoing rebase | **No** | Hypercall ABI; PV-MMU protocol; PV interrupt/timer/IPI; PV device transport; guest boot protocol (no BIOS/UEFI); still must emulate *all* of x86-64 user mode + the kernel's non-PV instruction stream | Legacy platform (PIC, PIT, CMOS, A20, real mode, SMM), LAPIC/IOAPIC, possibly page-table shadowing, possibly 4 KiB-granule problem |
| **(b)** | **Unikernel** (Unikraft, MirageOS, OSv, rust-vmm style) | Yes — but it is *your* code | **No** | Same as (a) but a much smaller surface: single address space, often no user/kernel split, no fork, no demand paging | Everything in (a) *plus* privilege-level transitions, most of the MMU, signals, process model |
| **(c)** | **Unmodified Linux + virtio drivers** ("PV devices only") | **No** | Linux yes; Windows yes with virtio + Hyper-V enlightenments | Full x86-64 including ring 0: paging (4-level + PSE/PAE + PCID + NX + SMEP/SMAP + WP), segmentation, IDT/GDT/TSS, MSRs, CPUID, `syscall`/`sysret`/`sysenter`, APIC (or virtio replacements), TSC, virtio-mmio or virtio-pci transport, a boot protocol (Linux boot protocol or UEFI) | Legacy device zoo (IDE/e1000/AC97/…) if you require virtio-only; SMM; real mode if you use the Linux 64-bit boot entry |
| **(d)** | **Unmodified Windows** | No | Yes | All of (c) **plus**: UEFI (OVMF-equivalent), ACPI tables, a real emulated platform (PCI(e) config space, PS/2 or USB HID, VGA/QXL/virtio-gpu), TPM 2.0 (Win11), `HLT`/`MWAIT`/`MONITOR`, TSC invariance, ACPI power states, HPET, and correct enough LAPIC/IOAPIC/MSI semantics to survive HAL init | Nothing |

### 2.2 The tradeoff, made explicit

- **(a) and (b) are what "para-virtualization" literally means. They are also the two options with the
  smallest addressable market.** A patched-kernel-only emulator is a research artifact or an internal
  CI tool. Xen PV — the most successful true-PV system ever built — has been in slow retreat since 2013
  precisely because the ecosystem cost exceeded the performance benefit once hardware caught up.
- **(c) is where essentially all modern value is.** Note carefully: **this is not para-virtualization.**
  It is "full emulation of the CPU + PV devices". Calling it PV is the most common error in this space.
- **(d) roughly doubles the device/firmware work relative to (c) and adds a long tail of
  "Windows notices something is wrong" bugs**, but requires *no new CPU-emulation capability* beyond (c).
  So (c)→(d) is a device/firmware project, not a CPU project. Defer it, but do not architecturally
  preclude it.

### 2.3 Quantified savings per PV feature — for a **DBT**, not a hypervisor

This is the table that matters. The right-hand column is the correction that most analyses miss.

| PV feature | What it removes in a *hypervisor* | Value to a *hypervisor* | Value to a **cross-ISA DBT** | Needs modified kernel? |
|---|---|---|---|---|
| **virtio-blk / -net / -console** vs IDE / e1000 / 16550 | Dozens of MMIO exits per I/O op | Very large (order 2–10× I/O throughput) | **Large if devices live outside the translator** (Tier 2, §6.2: real `hv_vcpu_run` exits); **modest if devices are C functions in-process** (Tier 1) | **No** — driver only |
| **PV clock** (kvmclock / Hyper-V reference TSC) | RDTSC/HPET traps; fixes migration time-warp | Moderate | **Small on throughput, large on correctness.** A 10×-slow emulator makes wall-clock timers pathological (guest thinks 10× more time passed per instruction). Do this early for sanity, not speed | **No** — driver only |
| **PV EOI** | One APIC MMIO write per interrupt | Small–moderate | **≈0.** An emulated-LAPIC write is a helper call | Yes |
| **PV IPI** | N × ICR writes → 1 hypercall | Moderate on large SMP | **Small.** Batch in the translator instead | Yes |
| **PV spinlocks** (`pvqspinlock`) | Lock-holder preemption during overcommit | Large under overcommit | **Real but second-order.** A DBT is already ~10× slow, which amplifies LHP; but you control the scheduler, so fix it there first | Yes |
| **PV TLB flush** | Remote shootdown IPIs to descheduled vCPUs | Measured **~35 %** on a TLB-stress microbenchmark (Hyper-V, 3 m 33 s → 2 m 20 s) | **Moderate on SMP guests.** Also: the hook already exists in stock Linux (`pv_ops.mmu.flush_tlb_multi`) so this is *cheap PV* — not a kernel fork | Yes, but hook is upstream |
| **Async page fault** | Host swap latency reparked as a guest fault | Moderate | **≈0.** Deprecated upstream since 5.10 anyway | Yes |
| **PV-MMU (Xen-style: read-only guest PTs + `HYPERVISOR_mmu_update`)** | Shadow page-table maintenance and trap-on-PTE-write | Historically large — this was Xen's headline trick | **Small-to-moderate, and only if you already use shadow/host page tables.** It removes the *invalidation* half of the cost, never the *per-access* half. If you use a softmmu, the per-access cost dominates and PV-MMU does nothing for it | Yes — deep (`PARAVIRT_XXL`) |
| **PV SMC / icache notification** | n/a (same-ISA hypervisors don't care) | n/a | **Small — see §3.2.** Covers kernel text patching (rare) but *not* userspace JITs (the expensive case), because x86 has no `flush_icache_range` for userspace to call | Yes + libc/JIT changes |
| **PV ASID / context-switch notification** | n/a | n/a | **≈0 — solved better architecturally.** Key the code cache on *guest physical* address; Captive does this and gets code reuse across address spaces for free | No (design, not PV) |
| **PV syscall** | n/a | n/a | **≈0 in a pure DBT.** A guest `syscall` is a jump to translated guest-kernel entry code — there is no host trap to elide. It becomes valuable *only* if the guest kernel is native host-ISA (§3.6) | n/a |
| **PV page allocation in 16 KiB-aligned, permission-uniform quads** | n/a | n/a | **Potentially very large on Apple Silicon — §3.1.** Restores 1:1 host-MMU mapping that the missing 4 KiB granule destroys | Yes — but a small, self-contained patch |
| **Replace guest kernel with a native ARM64 kernel** (Rosetta model) | — | — | **Removes 100 % of ring-0 emulation, MMU emulation, device emulation, interrupt emulation.** 5–20× vs QEMU TCG | The kernel is *replaced*, not patched |

**Read this table as:** virtio + PV clock (free, no guest fork) → PV TLB flush (nearly free, upstream hook)
→ 16 KiB page quads (project-specific, high value) → everything else (low value, high cost).

---

## 3. Historical PV designs, mined for what transfers

| System | Era | What it paravirtualized | Measured win | Transfers to a **cross-ISA translator**? |
|---|---|---|---|---|
| **Xen PV** ([SOSP'03](https://www.cl.cam.ac.uk/research/srg/netos/papers/2003-xensosp.pdf)) | 2003 | Everything: ring-1 guest kernel, read-only guest page tables + `HYPERVISOR_mmu_update` (batched (ptr,val) lists), event channels for interrupts, PV block/net, `HYPERVISOR_update_va_mapping`, hypercall-based descriptor/MSR/CR access | **<3–4 % vs native** (PostgreSQL/OSDB 4 %, SPEC WEB99 2 %) | **Partly.** The batched-PTE-update idea is the transferable core: it converts an unpredictable stream of traps into an explicit, batchable, semantically-rich call. But you only benefit if you maintain shadow/host page tables. **See the caveat below.** |
| **Xen PV, 64-bit reality check** | 2007+ | — | **64-bit PV can be *slower* than HVM.** AMD removed segment limits in long mode, so Xen must put the guest kernel in ring 3 alongside userspace and use separate page tables per privilege level; **every syscall costs TLB flushes**, and there is no `sysret` hypercall (PV guests use `iret`) | **This is the cautionary tale.** True PV bought Xen ~3 % in 2003 and *negative* performance in 2008. Hardware ate the benefit. |
| **Xen PVH / PVHVM** | 2013+ | Devices, interrupts, timers, boot — *nothing else*. Privileged ops and pagetables use HW extensions | PVHVM measured **15–41 % faster than PV** on realistic workloads | **Yes — and this is the arc to copy.** The industry converged on "PV the devices, hardware the CPU/MMU". |
| **Xen on ARM** ([LPC 2012](https://blog.linuxplumbersconf.org/2012/wp-content/uploads/2012/09/2012-lpc-virt-xen-arm-stabellini.pdf)) | 2012 | Devices, interrupts, timers only. Explicitly: **"no PV MMU calls, no shadow pagetables"** — uses stage-2 translation | (design simplification, not a benchmark) | **Yes — strongest single data point.** Given a clean second-stage MMU, the Xen team, who *invented* PV-MMU, chose not to build one again. |
| **KVM PV features** ([QEMU docs](https://www.qemu.org/docs/master/system/i386/kvm-pv.html)) | 2008– | `kvmclock` (2.6.26), `kvm-nopiodelay`, `kvm-asyncpf` (2.6.38, deprecated 5.10), `kvm-steal-time` (3.1), `kvm-pv-eoi` (3.10), `kvm-pv-unhalt`/pvspinlock (3.12), `kvm-pv-tlb-flush` (4.16), `kvm-pv-ipi` (4.19), `kvm-poll-control`, `kvm-pv-sched-yield`, `kvm-asyncpf-int`, `kvm-msi-ext-dest-id` (5.10) | Individually single-digit to ~35 % on targeted workloads | **The *model* transfers even where the features don't.** Every one is CPUID-negotiated and optional; the guest runs unmodified if absent. **This is the right shape for natemu's PV layer.** Note KVM never added a PV-MMU — EPT/NPT made it pointless. |
| **Hyper-V enlightenments** ([QEMU docs](https://www.qemu.org/docs/master/system/i386/hyperv.html)) | 2008– | `hv-vapic` (VP-assist page for APIC), `hv-time` (reference TSC page), `hv-synic` + `hv-stimer` (synthetic interrupt controller + 4 timers/vCPU), `hv-tlbflush` (+`-ext`, `-direct`), `hv-ipi`, `hv-spinlocks`, `hv-vpindex`, `hv-runtime` (steal time), `hv-reenlightenment`, `hv-evmcs`, `hv-xmm-input`, `hv-avic` | PV TLB flush: **~35 %** on TLB-stress (3 m 33 s → 2 m 20 s) | **Yes, and uniquely: this is the only way to paravirtualize an *unmodified* Windows guest.** Windows ships the enlightenment client in-box. If Windows is ever a target, `hv-*` is the PV interface, not a custom one. |
| **VMware VMI / `paravirt_ops`** ([LWN](https://lwn.net/Articles/175706/)) | 2006–2010 | A vendor-neutral MMU + CPU + IRQ indirection layer in Linux | — | **Removed in Linux 2.6.37** because Intel VT-x/AMD-V hardware outperformed it. **The definitive precedent: a general-purpose PV-MMU interface was built, shipped, and deleted.** |
| **Linux `paravirt_ops` today** ([kernel docs](https://docs.kernel.org/virt/paravirt_ops.html)) | 2018– | Split by [tip commit `fdc0269e8958`](https://lore.kernel.org/all/tip-fdc0269e8958a1ec95b8ac685c1d372c24c60faa@git.kernel.org/) and siblings: Xen-PV-only ops moved under `CONFIG_PARAVIRT_XXL` | — | **Verified in current `arch/x86/include/asm/paravirt_types.h`:** what remains **unconditional** under plain `CONFIG_PARAVIRT` is small and useful — `pv_ops.mmu.flush_tlb_{user,kernel,one_user,multi}`, `pv_ops.mmu.exit_mmap`, `notify_page_enc_status_changed`, `pv_ops.irq.{safe_halt,halt}`, plus `pv_ops.lock.*` (pvqspinlock) and the PV sched-clock hook. Everything interesting for a PV-MMU — CR2/CR3, `set_pte`/`set_pmd`/alloc/release, descriptor tables, MSRs, `load_sp0`, `iret` — is **`PARAVIRT_XXL`, i.e. Xen-PV-only**. **Practical consequence: `flush_tlb_multi` is a free, upstream, no-fork PV hook you can use today. A real PV-MMU means turning on `PARAVIRT_XXL` and writing a Xen-PV-class backend — a guest fork.** |
| **User-mode Linux** | 1999– | The whole kernel, as a host userspace process; `ptrace`/`SECCOMP` for syscall interception, host `mmap` for guest paging | ~20 % slowdown typical; **~50 % on syscall-heavy**; poor on I/O | **Conceptually yes, mechanically no.** UML's core idea — *let the host MMU do the guest's paging via `mmap`* — is the same idea as ESPT/HSPT. Its fatal flaw (syscall interception cost) does not apply to a DBT. |
| **L4Linux** ([SOSP'97](https://cseweb.ucsd.edu/classes/fa01/cse221/papers/haertig-ukernel-perf-sosp97.pdf)) | 1997 | Linux as a single L4 server task; IPC-based syscalls, external pager | **5–10 % on AIM**; ~6 % attributable to the microkernel; up to 2.5× on microbenchmarks | Mostly historical. Confirms the ceiling: a very well done PV port costs ~5–10 %, i.e. **PV is a "shave the last 10 %" tool, not a 2× tool** — unless it removes an entire emulation subsystem. |
| **Lguest** | 2007–2017 | A minimal, pedagogical x86 PV hypervisor in ~5 kLoC | — | **Removed in Linux 4.14.** Same story as VMI. |
| **MagiXen** | 2006 | IA-32 DBT *inside* Xen on IA-64 — the closest historical relative of this project | — | Cited by Captive; the "put the translator inside a hypervisor VM" structure is exactly Tier 2 (§6.2). |
| **HyperMAMBO-X64** | 2017 | AArch32→AArch64 DBT running *under* a hypervisor, transparent to the guest OS | — | Same structural lesson. Cross-ISA translation belongs *below* the guest kernel and *inside* a hardware VM. |

### The Xen-PV-MMU caveat, stated precisely

Xen PV-MMU removes the cost of **keeping a second set of page tables in sync**. It does *nothing* about
the cost of **translating an address on every guest memory access** — because on Xen the hardware does
that. In a DBT, the second cost is the dominant one and the first is a rounding error. **PV-MMU therefore
optimizes the wrong half of the problem for a DBT that uses a software MMU, and is redundant for a DBT
that uses the host MMU.** It becomes interesting only in the narrow band where you use host page tables
*and* PTE-write interception is measurably hot (fork/exec/mmap-heavy workloads) — and even there,
Linux's `set_pte` hooks are `PARAVIRT_XXL`-only, i.e. a guest fork.

---

## 4. PV opportunities that are genuinely specific to cross-ISA translation

Evaluated honestly. Grades: **A** = build it, **B** = build it if cheap, **C** = don't.

### 3.1 PV physical-page allocation to work around the missing 4 KiB granule — **grade A (the standout)**

**The problem.** Apple Silicon does not implement the 4 KiB translation granule. Confirmed on the target
machine: `hw.pagesize = 16384` on an M2 Max; Asahi Linux requires `CONFIG_ARM64_16K_PAGES` for the same
reason. This holds at **both** stages: guest stage-1 page tables written by a unikernel inside a
`Hypervisor.framework` VM, and stage-2 mappings established by `hv_vm_map()`, and ordinary host `mmap()`.

**Why it matters.** The entire "use the host MMU for guest translation" family of techniques
(Captive, ESPT, HSPT) depends on installing one host PTE per guest PTE. An x86-64 Linux guest uses 4 KiB
pages for essentially all of userspace. Four guest pages must share one host page, and you may only map
them together when all four are (i) contiguous in guest-physical address and (ii) identical in
permissions (R/W, U/S, NX). Otherwise you fall back to Captive's own escape hatch, which the paper
describes as: *"we support a software-based approach where additional checks are made in translated
code. However, this increases the runtime overhead."*

**The PV fix.** A patched guest kernel that:
- allocates userspace physical pages in **16 KiB-aligned quads with uniform permissions**, and/or
- prefers 2 MiB mappings (Linux already 2 MiB-maps the kernel direct map, so *kernel-mode* accesses are
  already fine — this is a userspace problem), and/or
- exposes an explicit "this 16 KiB region is homogeneous" hint to the translator.

**Value.** This is the difference between "host MMU does the translation" (Captive-class, 1.5–2×) and
"host MMU does the translation *sometimes*, with software sub-page checks otherwise" (much less). It is
the only PV feature on this list whose absence degrades the *core* optimization. It is also a **small,
self-contained patch** — a page-allocator policy tweak, not a `PARAVIRT_XXL` fork.

**Do this measurement first (cheapest high-value experiment in the whole document):** boot an
x86-64 Linux guest under QEMU, dump the guest page tables, and compute *what fraction of mapped 4 KiB
userspace PTEs already fall into contiguous, permission-uniform 16 KiB quads* under a realistic workload
(kernel build, browser, database). If the answer is >90 %, you do not need this PV feature and Tier 2
(§6.2) works on an unmodified guest. If it is 50 %, this feature is the project. **This single number
decides whether "para-virtualization" is a good premise or a bad one.**

### 3.2 PV code-cache / SMC notification — **grade C** (the intuition is appealing but the mechanism isn't there)

The idea: the guest tells the translator "I modified code at X", so the translator can skip
write-protecting code pages and skip trap-driven `tb_invalidate`.

Why it is weaker than it looks:

1. **On x86 there is nothing to hook for userspace.** `flush_icache_range` / `__builtin___clear_cache`
   are architecturally no-ops on x86 because the I-cache is coherent with stores. Every x86 JIT — V8,
   HotSpot, .NET, LuaJIT, PyPy, glibc's PLT/IFUNC trampolines — writes bytes and jumps. There is no call
   to intercept. **This is the expensive case and PV cannot reach it** without also modifying every JIT.
2. **Kernel text patching *does* have hooks** — `text_poke()` / `text_poke_bp()` (INT3 + IPI-sync +
   tail-write), `apply_alternatives()`, jump labels / static keys, static calls, ftrace, kprobes, module
   load/unload. But these are **rare** (boot, module load, tracing enable) and already cheap.
3. **The real cost in QEMU is not SMC, it is false sharing at page granularity.** QEMU write-protects
   any page containing a translated block; benign *data* writes to that page then fault and invalidate.
   With **16 KiB** host pages on Apple Silicon this gets **4× worse** than on a 4 KiB host. The fix is
   sub-page dirty tracking / finer invalidation granularity in the translator, not a guest hypercall.
4. There *is* a narrow, real win: **a PV "relaxed SMC" mode** in which the guest kernel promises to
   announce all kernel-text modification, letting the translator drop write protection on kernel text
   pages entirely. Bounded, cheap, worth maybe low single-digit percent. Grade C+ — do it only if the
   PV layer already exists for other reasons.

**Corollary worth remembering:** a *user-mode* translator sees `mmap(PROT_EXEC)` / `mprotect(PROT_EXEC)`
directly and therefore gets high-quality SMC notification **for free, with no guest modification**. A
*system-mode* translator sees only PTE writes and must infer. This is one more structural advantage of
the Rosetta model (§3.6).

### 3.3 PV address-space switch / ASID hinting — **grade C (solved better without PV)**

The naive system-mode DBT flushes its code cache on `MOV CR3`. The fix is architectural, not PV:
**key the code cache on guest *physical* address**. Captive does exactly this and notes the bonus:
*"translated code is re-used across different virtual mappings to the same physical address, e.g. when
using shared libraries."* Guest PCID/ASID can be mapped onto ARM64 ASIDs in the stage-1 tables of the
unikernel. No guest cooperation required.

### 3.4 PV interrupts / PV timers — **grade B (context-dependent)**

- In a **user-mode-hosted** translator (Tier 1), an emulated LAPIC write is a C call. PV buys ~nothing.
- In a **VM-hosted** translator (Tier 2), device models live in the host process across a real
  `hv_vcpu_run` boundary (~µs). Now PV/virtio matters a lot. **Mitigation that beats PV: put the
  interrupt controller model *inside* the unikernel**, so LAPIC accesses never exit the VM. Only true
  I/O (virtio queues) exits, and virtio batches that by design.
- **PV clock is worth doing early regardless**, for correctness rather than speed: a 10×-slow emulator
  with a real-time-driven tick produces pathological guest behaviour (timer storms, RCU stalls,
  softlockup warnings). Either a kvmclock-equivalent or a *virtual* time base tied to instruction count.

### 3.5 PV syscall — **grade C in a DBT, grade A only in the hybrid**

In a DBT there is no host trap on a guest `syscall`; the translator simply continues translating at the
guest's `MSR_LSTAR` entry point. There is no cost to remove. The one real optimization is *translator*
work, not PV: recognize the syscall entry/exit sequence as a chained region so you don't break the trace.

The idea only pays when the guest kernel is **native host ISA** — then `syscall` becomes a direct call
into an ARM64 kernel with argument marshalling. That is §3.6.

### 3.6 Native ARM64 kernel + translated x86-64 userspace (Rosetta-for-Linux) — **grade A for performance, but it is a different product**

This is the *maximal* para-virtualization: the guest kernel is not patched, it is **replaced**.

What it eliminates, completely:
- x86 MMU emulation (the host kernel's real MMU does real paging)
- All device emulation (a real ARM64 kernel runs real virtio drivers against a real hypervisor)
- All interrupt-controller and timer emulation
- All privileged x86 emulation: rings, IDT/GDT/TSS, MSRs, segmentation, `sysret` corner cases, SMM,
  real/protected/compatibility mode transitions, CPUID quirks
- The 4 KiB-granule problem (§3.1) — the ARM64 kernel just uses 16 KiB pages natively

What remains:
- x86-64 **user-mode** translation (still the hard part: full SSE/AVX, flags including AF/PF, `rep`
  string ops, precise faults)
- syscall ABI translation, x86-64 Linux → aarch64 Linux (~350 syscalls; the hard ones are `clone`,
  `futex`, `ioctl`, `signalfd`, `ptrace`, and anything with a struct-layout difference)
- signal frame construction in x86-64 format on an ARM64 stack
- `%fs`-base / TLS multiplexing against the host's `TPIDR_EL0`
- vDSO substitution; `binfmt_misc` registration; `/proc/cpuinfo` and `uname` spoofing (Apple's Rosetta
  does exactly this — hardcoded `vendor_id: VirtualApple`, `model 142`, static MHz/cache values)
- **memory ordering**: x86 TSO on a weakly-ordered ARM64 host
- **page-size leakage**: x86 binaries that hardcode 4096 can misbehave on a 16 KiB-page kernel

Measured: Rosetta-for-Linux is reported at **4–5× QEMU** on typical Docker workloads, **~2–4×** on
7-Zip specifically, **>20×** in some reports, and roughly **70–80 % of native** ARM performance for
compute-bound code.

**The honest strategic problem:** Apple ships this for free via `VZLinuxRosettaDirectoryShare`, and
FEX-Emu and box64 are open-source and already run on Apple Silicon. If this is the product, the
differentiating engineering is small and the competition is a shipped, tuned, vendor-privileged binary
translator with hardware TSO access.

### 3.7 Apple-Silicon-specific issues that dominate the PV question

| Issue | Impact | Notes |
|---|---|---|
| **No 4 KiB translation granule** | Blocks 1:1 host-MMU mapping of guest 4 KiB PTEs | §3.1. **Highest-priority unknown.** Verify inside a `Hypervisor.framework` guest by reading `ID_AA64MMFR0_EL1.TGran4`. |
| **x86 TSO on weak ARM64** | Every guest load/store must become `ldar`/`stlr` or be fenced, unless hardware TSO is available. Typically **15–40 %** on memory-heavy translated code | Apple's hardware TSO bit lives in `ACTLR_EL1` and is what Rosetta uses. **Whether a guest at EL1 inside `Hypervisor.framework` can set it is unverified and is the second-highest-priority experiment.** A half-day test. Single-vCPU guests sidestep the issue entirely. |
| **`hv_vcpu_run` exit cost** | Order of a microsecond | Argues for the Captive structure: DBT and interrupt controller *inside* the VM, only virtio queues outside. Measure it. |
| **TTBR0/TTBR1 split** | An *advantage* over Captive's x86 host | Guest ring 3 → EL0/TTBR0; DBT runtime + guest ring 0 → EL1/TTBR1. Cleaner than Captive's ring-0/ring-3 x86 arrangement. |
| **x86 flags (AF, PF) have no ARM64 equivalent** | Standard DBT tax | Lazy flag evaluation; well-understood. |
| **JIT W^X on macOS** | Only affects a *host-process* JIT (Tier 1); needs `MAP_JIT` + `pthread_jit_write_protect_np` | Inside a `Hypervisor.framework` VM (Tier 2) the guest owns its own stage-1 tables and W^X is a non-issue. Another point for Tier 2. |

---

## 5. How much does any of this actually buy? Measured data

### 5.1 Hardware vs software MMU — the numbers that matter most

| Source | Setting | Result |
|---|---|---|
| VMware, [*Performance Evaluation of Intel EPT Hardware Assist*](https://www.vmware.com/pdf/Perf_ESX_Intel-EPT-eval.pdf) | EPT vs shadow paging, ESX, same-ISA | **Up to 48 % on MMU-intensive applications; up to 600 % on MMU-intensive microbenchmarks** |
| Bhargava et al., [*Accelerating Two-Dimensional Page Walks for Virtualized Systems*](https://pages.cs.wisc.edu/~remzi/Classes/838/Spring2013/Papers/p26-bhargava.pdf), ASPLOS 2008 | Nested paging cost | **Nested walks are 3.9–4.6× slower than native walks**; extending the page-walk cache to the nested dimension recovers **15–38 %**; large pages add a further **3–22 %** |
| Chang et al., *Efficient Memory Virtualization for Cross-ISA System Mode Emulation* (ESPT), [VEE 2014](https://dl.acm.org/doi/10.1145/2576195.2576201) | Cross-ISA, QEMU + embedded shadow page tables via a kernel module | **~40 % speedup** |
| Wang et al., *HSPT*, VEE 2015 | Same idea, `mmap`-only (no kernel module) | **1.51× (ARM-on-x86 system mode); 1.59× (IA-32-on-x86-64)** |
| Spink et al., *Hardware-Accelerated Cross-Architecture Full-System Virtualization*, ACM TACO 2016 (Captive) | ARM guest, x86 host, KVM + host MMU | **2.5× average, up to 5.88× over QEMU**; 915 MIPS average, 1280 MIPS peak |
| Spink, Wagstaff, Franke, [*A Retargetable System-Level DBT Hypervisor*](https://research-repository.st-andrews.ac.uk/bitstream/handle/10023/24320/Spink_2020_ACM_Trans._Comput._Syst_Retargetable_System_Level_AAM.pdf?sequence=1), ACM TOCS 2020 | ARMv8-A guest, x86-64 host, vs QEMU 2.12.1 | **2.21× geomean SPEC CPU2006 integer**; **6.49× geomean floating point**; ARMv7-A **2.5× avg, up to 6×**; achieves **~40 % of a physical system's performance** |

**Interpretation.** The clean, guest-agnostic number is **~1.5×** (HSPT/ESPT: fix the memory path only)
and **~2.2×** (Captive: fix the memory path *and* the privilege model *and* the JIT). The 6.49× FP figure
is **not** an MMU result — it comes from QEMU using softfloat while Captive uses the host FPU, and is
largely unavailable to you on ARM64 because x86 SSE semantics (denormals, `MXCSR`, x87 80-bit) do not map
as cleanly onto NEON as ARM VFP maps onto SSE. Do not budget for it.

### 5.2 Where does full-system emulation time actually go?

Captive's SimBench and JIT breakdowns are the best public decomposition:

| Measurement | Value | Source |
|---|---|---|
| SimBench **TLB-Flush** speedup, Captive vs QEMU | **32.38×** | TOCS 2020, Fig. 19 |
| SimBench **TLB-Evict** speedup | **25.88×** | ibid. |
| SimBench **Mem-Hot-MMU / Mem-Cold-MMU** | Among the largest speedups (~4–8× band) | ibid. |
| SimBench **Data-Fault** | Captive is **slower** than QEMU (a guest page fault becomes a real host fault + guest PT walk) | ibid. |
| SimBench **Small-Blocks / Large-Blocks** (codegen throughput) | Captive **85 % / 65 % slower** than QEMU; Captive is **2.6× slower at translating basic blocks** overall | ibid. |
| JIT time split | Decode **2.75 %**, translate **54.54 %**, register allocation **25.63 %**, encode **17.08 %** | ibid., Fig. 20 |
| Captive model size | **8,100 LoC** of ADL vs **17,766 + 7,948 LoC** for QEMU's ARM model + softfloat | ibid. |

**The message:** memory/TLB emulation is where the order-of-magnitude wins live (25–32× on targeted
microbenchmarks), but it translates to only ~2.2× on real integer applications, because on SPECint the
guest spends most of its time in straight-line user code where the softmmu fast path — a handful of extra
host instructions per access — is a constant-factor tax rather than a catastrophe. Conversely, kernel-heavy,
fork/exec-heavy, page-fault-heavy workloads (compiles, container startup, package installs) sit much closer
to the microbenchmark end. **If your target workload is "boot a distro and build software", you are in the
regime where MMU acceleration pays 2–4×; if it is "run SPECint", it pays ~1.5–2×.**

Also note the honest downside: **Captive is 2.6× slower at JIT compilation than QEMU and loses on
`Data-Fault`.** A hardware-MMU design trades throughput for latency; short-running processes and
fault-heavy workloads can regress.

### 5.3 What PV itself is worth (same-ISA baselines)

| System | Measured |
|---|---|
| Xen PV vs native (2003, 32-bit) | **<3–4 %** overhead (PostgreSQL/OSDB 4 %, SPEC WEB99 2 %) |
| Xen PVHVM vs Xen PV | PVHVM **15–41 % faster** on realistic workloads |
| Xen 64-bit PV vs HVM | PV can be **slower**, because syscalls require page-table switches / TLB flushes |
| Hyper-V PV TLB flush | **~35 %** on a TLB-stress microbenchmark (3 m 33 s → 2 m 20 s) |
| L4Linux vs native | **5–10 %** (AIM); up to **2.5×** on microbenchmarks |
| UML vs native | ~**20 %** typical, ~**50 %** on syscall-heavy |
| Rosetta-for-Linux vs QEMU | **~2–4×** (7-Zip), **4–5×** typical Docker, **>20×** in some reports; **~70–80 % of native** |

**The dispositive comparison:** the best true-PV system ever built bought **3 %** over native in its era,
and **negative** performance a hardware generation later. Replacing the guest kernel with a native-ISA
one buys **4–20×**. **PV as a fine-grained optimization is worth single-digit percent. PV as a
whole-subsystem elimination is worth an order of magnitude. There is almost nothing in between.**

---

## 6. Three scope points, least to most ambitious

Effort in person-months for an experienced systems engineer, including test/debug but excluding
polish/packaging. Performance is relative to QEMU TCG system mode on the same host.

### Tier 1 — "Better TCG": user-mode-hosted x86-64 system emulator with a real optimizing JIT

**Scope.** Stay in a normal macOS process. Keep a QEMU-like structure (device models as C, softmmu),
but replace the translation core: proper IR, real register allocation across superblocks/traces, lazy
flag evaluation, direct SSE→NEON lowering instead of helper calls, host-`ldar`/`stlr` only where the
guest actually needs ordering, indirect-branch inline caching, physical-address-keyed code cache
(§3.3). Softmmu is micro-optimized (larger/adaptive TLB, victim cache) but **not** replaced.

**Explicitly not attempted:** `Hypervisor.framework`, host-MMU-backed translation. Note that the
HSPT/ESPT approach — the obvious "cheap MMU win" — is **substantially blocked on Apple Silicon by the
16 KiB page size**: you cannot `mmap` a guest 4 KiB page at 4 KiB granularity in a host process.

**Guest target:** (c) — unmodified x86-64 Linux + virtio. No guest modification at all.

**Effort:** **10–16 pm.** (x86-64 decoder + IR + optimizer + ARM64 backend ≈ 7–11; softmmu tuning ≈ 1–2;
device/machine model ≈ 2–3 if reusing existing designs.)

**Expected:** **1.5–2.5×** over TCG. Ceiling is real: you still pay ~5–8 host instructions per guest
memory access.

**Risk:** low. This is well-trodden. Good first milestone regardless of final ambition, because the JIT
is required by every tier.

### Tier 2 — "Captive for Apple Silicon": DBT unikernel inside a `Hypervisor.framework` VM  ← **recommended target**

**Scope.** An ARM64 unikernel runs at guest EL1 inside a `Hypervisor.framework` VM and contains the JIT,
the runtime, and the interrupt-controller model. Guest x86-64 ring 3 executes at EL0 under TTBR0; guest
ring 0 and the DBT run at EL1 under TTBR1. **The guest's x86 page tables are shadowed into the
unikernel's ARM64 stage-1 tables, so the hardware MMU performs guest-virtual → host-physical
translation** (stage 2 via `hv_vm_map` handles IPA→PA). Device backends (virtio-blk/net/console, and
optionally a virtio-gpu) live in the host process behind `hv_vcpu_run` exits; PV/virtio matters here
because those exits are real.

**Guest target:** (c) — unmodified x86-64 Linux + virtio — **with an optional, CPUID-negotiated PV layer**
(the KVM model, §3): `natemu-pv-tlb-flush` (via the already-upstream `pv_ops.mmu.flush_tlb_multi`),
`natemu-pvclock`, and — the one that might be mandatory — **`natemu-page-quads`** (§3.1). Windows (d) is
architecturally reachable later via `hv-*` enlightenments plus firmware work; do not preclude it.

**Effort:** **28–48 pm.** Breakdown: JIT/backend (shared with Tier 1) 10–16; unikernel + boot + EL1/EL0
plumbing + stage-1 shadow page tables 8–14; x86 system architecture (paging modes, PCID, segmentation,
IDT/GDT/TSS, MSRs, CPUID, `syscall`/`sysret`, exceptions with precise faults) 6–10; virtio + machine
model + Linux boot protocol 4–8.

**Expected:** **2.5–5×** over TCG on system workloads (Captive's 2.21× SPECint understates
kernel-heavy/boot/build workloads and overstates nothing); **less if the 16 KiB-granule fallback path is
hot** — which is exactly why the §3.1 measurement comes first.

**Risks, ranked:**
1. **4 KiB granule unavailable (§3.1).** Could cut the headline win in half. *Mitigate by measuring
   PTE-quad homogeneity in a real guest before committing.*
2. **TSO.** If guest EL1 cannot set Apple's `ACTLR_EL1` TSO bit, budget 15–40 % on memory-heavy code,
   or ship single-vCPU first. *Half-day experiment.*
3. **Debuggability.** A JIT inside a bare-metal unikernel inside a VM is a genuinely hard thing to
   debug. Budget for a matching Tier-1-style user-mode reference implementation to diff against —
   this is not optional, and it is another reason to build Tier 1 first.
4. **Precise exceptions.** Restarting a partially-executed translated block on a guest page fault is
   the classic system-DBT correctness swamp. Captive's `Data-Fault` regression is the visible symptom.

### Tier 3 — "Rosetta-shaped hybrid": native ARM64 kernel + translated x86-64 userspace

**Scope.** Abandon x86 ring-0 emulation entirely. Ship a native ARM64 Linux kernel (in a
`Virtualization.framework` or `Hypervisor.framework` VM, or even container-style), a `binfmt_misc`
handler, a user-mode x86-64→ARM64 translator, a syscall/ABI shim, signal-frame translation, TLS
multiplexing, a vDSO, and an AOT translation cache.

**Guest target:** none of (a)–(d) — it is *not a system emulator*. It runs x86-64 Linux **binaries**.

**Effort:** **24–44 pm** from scratch (user-mode JIT 12–24; syscall/ABI/signals/TLS 6–12; AOT cache,
binfmt, packaging 4–8). **Or 2–5 pm** to wrap FEX-Emu or box64 and do integration/tuning.

**Expected:** **5–20×** over TCG; **60–80 % of native** for compute-bound code (Rosetta-class), assuming
you solve TSO. Without hardware TSO, closer to **45–60 %** of native.

**Risk:** **strategic, not technical.** Apple ships Rosetta-for-Linux for free; FEX-Emu and box64 are
mature open-source competitors; and only Apple's own translator gets privileged hardware TSO access.
Building this from scratch is 24–44 pm to reach parity with something free.

### Comparison

| | Tier 1 | **Tier 2** | Tier 3 |
|---|---|---|---|
| Guest modified? | No | No (optional PV layer) | Kernel *replaced* |
| Boots an x86 kernel? | Yes | Yes | **No** |
| Path to Windows? | Yes (device work) | Yes (device work) | **No** |
| Effort | 10–16 pm | 28–48 pm | 24–44 pm (or 2–5 wrapping FEX) |
| Speed vs TCG | 1.5–2.5× | 2.5–5× | 5–20× |
| Speed vs native ARM | ~5–10 % | ~15–35 % | 60–80 % |
| Novel? | No | **Yes — Captive has never been done on Apple Silicon** | No (Apple + FEX + box64) |
| Biggest risk | none material | 16 KiB granule; TSO | competing with free |

---

## 7. Recommendation

**Adopt Tier 2 as the north star, build Tier 1 first as both a de-risking step and a permanent debugging
reference, and reject guest-kernel para-virtualization as a *prerequisite* while adopting it as an
*optional negotiated layer*.**

Concretely:

1. **Reframe the premise.** "Para-virtualization to reduce hardware emulation overheads" should become
   **"hardware-assisted memory virtualization to eliminate the software MMU, plus PV devices, plus an
   optional PV accelerator layer."** The first clause is where the measured 1.5–2.2× lives and it costs
   the guest nothing. The premise as literally stated optimizes the wrong thing (§2.3, §3-caveat).

2. **Copy KVM's shape, not Xen's.** Every PV feature is CPUID-negotiated and optional; an unmodified
   guest boots and runs correctly, just slower. This preserves target (c) and keeps (d) open. Note that
   KVM — with full hardware assist — never shipped a PV-MMU, and Xen-on-ARM explicitly declined to build
   one. Do not build one.

3. **Take the free PV wins immediately, in this order:** virtio-blk/net/console; a PV clock (for
   *correctness* under a slow emulator, not speed); `pv_ops.mmu.flush_tlb_multi` (already unconditional
   upstream, no guest fork); `pv_ops.lock.*` if SMP overcommit shows up.

4. **Run these two experiments before writing Tier 2 code.** They are cheap and they determine the
   design:
   - **PTE-quad homogeneity.** In a booted x86-64 Linux guest under a realistic workload, what fraction
     of mapped userspace 4 KiB PTEs fall into contiguous, permission-uniform 16 KiB quads? This decides
     whether §3.1's PV page-allocator patch is optional or mandatory — i.e. whether *true* PV is
     required at all.
   - **TSO in a guest.** Can code at EL1 inside a `Hypervisor.framework` VM enable Apple's `ACTLR_EL1`
     TSO bit? Worth 15–40 % on memory-heavy translated code. Also measure `hv_vcpu_run` round-trip cost
     while you are there, since it sets the device-model architecture.

5. **The one PV feature worth designing for from day one is `natemu-page-quads` (§3.1)** — a guest
   page-allocator policy that keeps 4 KiB physical pages in 16 KiB-aligned, permission-uniform groups.
   It is the only item on the list that protects the *core* optimization rather than shaving a few
   percent, it is unique to this host, and it is a small patch rather than a `PARAVIRT_XXL` fork.

6. **Do not build PV-SMC notification, PV-EOI, PV-IPI, PV context-switch notification, or PV syscall.**
   §3.2–§3.5: they are either unreachable (userspace JITs), redundant (physical-address-keyed code
   cache), or optimize a cost a DBT does not pay (VM exits, host traps).

7. **If the real goal is "run x86 Linux software fast on a Mac" rather than "emulate an x86 system",
   stop and reconsider.** Tier 3 wins by 5–20×, Apple gives it away, and FEX-Emu/box64 already exist.
   The defensible product is *system-level* emulation — booting kernels, firmware, drivers, and
   non-Linux guests — which Tier 3 structurally cannot do and which Rosetta will never do.

---

## 8. Open questions requiring verification

| # | Question | How to resolve | Blocks |
|---|---|---|---|
| 1 | Does an `Hypervisor.framework` guest report `ID_AA64MMFR0_EL1.TGran4` as supported? | Read the register from guest EL1 in a minimal VM (~1 day) | Tier 2 core design |
| 2 | PTE-quad homogeneity fraction in a real x86-64 Linux guest | Dump guest CR3 tree under QEMU + a script (~2 days) | Whether true PV is needed at all |
| 3 | Can guest EL1 set Apple's `ACTLR_EL1` TSO bit under `Hypervisor.framework`? | Direct experiment (~0.5 day) | 15–40 % of Tier 2/3 perf |
| 4 | `hv_vcpu_run` round-trip latency on M2/M3/M4 | Microbenchmark (~0.5 day) | Device-model architecture |
| 5 | Exact per-benchmark EPT-vs-shadow numbers from the VMware paper | The abstract's "up to 48 % / up to 600 %" is cited here; the per-benchmark table could not be extracted from the PDF and is quoted second-hand | Only affects the strength of a supporting citation |
| 6 | Does `Hypervisor.framework` permit a nested (EL2) guest on M3+/macOS 15+, and is it useful here? | Docs + experiment | Only relevant if you want to run KVM *inside* the guest |

---

## 9. Sources

- Barham et al., *Xen and the Art of Virtualization*, SOSP 2003 — https://www.cl.cam.ac.uk/research/srg/netos/papers/2003-xensosp.pdf
- Xen Project, *The Paravirtualization Spectrum, Part 2: From poles to a spectrum* — https://xenproject.org/blog/the-paravirtualization-spectrum-part-2-from-poles-to-a-spectrum/
- Xen Project wiki, *Paravirtualization (PV)* — https://wiki.xenproject.org/wiki/Paravirtualization_(PV)
- Xen, *Hypercall ABI* — https://xenbits.xenproject.org/docs/sphinx-unstable/guest-guide/x86/hypercall-abi.html
- Stabellini, *Xen on ARM: ARMv7 with virtualization extensions*, Linux Plumbers 2012 — https://blog.linuxplumbersconf.org/2012/wp-content/uploads/2012/09/2012-lpc-virt-xen-arm-stabellini.pdf
- QEMU, *Paravirtualized KVM features* — https://www.qemu.org/docs/master/system/i386/kvm-pv.html
- QEMU, *Hyper-V Enlightenments* — https://www.qemu.org/docs/master/system/i386/hyperv.html
- QEMU, *Translator Internals* (TCG, SMC handling) — https://www.qemu.org/docs/master/devel/tcg.html
- Linux kernel, *Paravirt_ops* — https://docs.kernel.org/virt/paravirt_ops.html
- tip commit `fdc0269e8958`, *x86/paravirt: Move the Xen-only pv_mmu_ops under the PARAVIRT_XXL umbrella* — https://lore.kernel.org/all/tip-fdc0269e8958a1ec95b8ac685c1d372c24c60faa@git.kernel.org/
- Linux, `arch/x86/include/asm/paravirt_types.h` — https://raw.githubusercontent.com/torvalds/linux/master/arch/x86/include/asm/paravirt_types.h
- LWN, *The VMI virtualization interface* — https://lwn.net/Articles/175706/
- LWN, *x86/alternative: text_poke() enhancements* — https://lwn.net/Articles/773956/
- Spink, Wagstaff, Franke, *A Retargetable System-Level DBT Hypervisor*, ACM TOCS 2020 — https://research-repository.st-andrews.ac.uk/bitstream/handle/10023/24320/Spink_2020_ACM_Trans._Comput._Syst_Retargetable_System_Level_AAM.pdf?sequence=1 (also USENIX ATC'19: https://www.usenix.org/system/files/atc19-spink.pdf)
- Spink, Wagstaff, Franke, *Hardware-Accelerated Cross-Architecture Full-System Virtualization*, ACM TACO 2016 — https://www.research.ed.ac.uk/en/publications/hardware-accelerated-cross-architecture-full-system-virtualizatio
- Wagstaff, Bodin, Spink, Franke, *SimBench: A Portable Benchmarking Methodology for Full-System Simulators*, ISPASS 2017 — http://ispass.org/ispass2017/slides/wagstaff_simbench.pdf
- Chang et al., *Efficient Memory Virtualization for Cross-ISA System Mode Emulation* (ESPT), VEE 2014 — https://dl.acm.org/doi/10.1145/2576195.2576201
- Wang, Li et al., *HSPT: Practical Implementation and Efficient Management of Embedded Shadow Page Tables for Cross-ISA System Virtual Machines*, VEE 2015 — https://www.semanticscholar.org/paper/fa8e9614381ed39e6b978c691aa81e5365984a20
- VMware, *Performance Evaluation of Intel EPT Hardware Assist* — https://www.vmware.com/pdf/Perf_ESX_Intel-EPT-eval.pdf
- Bhargava, Serebrin et al., *Accelerating Two-Dimensional Page Walks for Virtualized Systems*, ASPLOS 2008 — https://pages.cs.wisc.edu/~remzi/Classes/838/Spring2013/Papers/p26-bhargava.pdf
- Härtig et al., *The Performance of µ-Kernel-Based Systems*, SOSP 1997 — https://cseweb.ucsd.edu/classes/fa01/cse221/papers/haertig-ukernel-perf-sosp97.pdf
- Hyper-V paravirtualized remote TLB flushing patch series — https://groups.google.com/g/linux.kernel/c/4hSdoDMBcvg
- Apple, *Rosetta 2 on a Mac with Apple silicon* — https://support.apple.com/guide/security/rosetta-2-on-a-mac-with-apple-silicon-secebb113be1/web
- Inoki, *Things You Might Want to Know About Apple's Rosetta 2 for Linux VMs* — https://blog.inoki.cc/2026/02/28/Apple-Rosetta-Linux-VM-Secret-en/
- Box86/Box64, *Box86/Box64 vs QEMU vs FEX (vs Rosetta2)* — https://box86.org/2022/03/box86-box64-vs-qemu-vs-fex-vs-rosetta2/
- Xen Orchestra, *PV vs PVHVM* — https://xen-orchestra.com/blog/pv-vs-pvhvm-on-next-xenserver/
- Gregg, *Xen Modes: What Color Is Your Xen?* — https://www.brendangregg.com/blog/2014-05-07/what-color-is-your-xen.html
- Igalia, *Comparing virtualization software performance: QEMU vs UML vs KVM* — https://blogs.igalia.com/berto/2007/06/27/comparing-virtualization-software-performance-qemu-vs-uml-vs-kvm/
