# 24 — Power-On to Shell: The Boot Path, and Which Legacy x86 We Refuse to Implement

**Date:** 2026-08-08 · **Host:** Apple M2 Max, macOS 26.5.2 (25F84)
**New measurement in this doc:** `probes/qemu_baseline/plugin_isaclass.c` (new, read-only QEMU TCG
plugin) + `probes/qemu_baseline/bzimage_hdr.py` (new, read-only) + `probes/qemu_baseline/isaclass_run.sh`
(new driver). Raw output: `probes/qemu_baseline/results/isaclass/`.
**Guest under measurement:** Alpine 3.24.1 netboot, **Linux 6.18.35-0-virt x86-64**, musl userspace,
QEMU 11.0.3 `-accel tcg,thread=single -cpu max -machine q35 -smp 1 -m 3072`.

Every claim below carries a tag:
**`M`** = measured here · **`S(file:line)`** = read out of primary source · **`C`** = cited
· **`I`** = inferred/argued · **`?`** = unresolved, with the experiment named.

Prior docs assume a steady-state 64-bit long-mode guest. Nothing in the corpus addressed how the
guest reaches that state. This document closes that gap.

---

## 0. Bottom line

1. **Direct kernel boot. No firmware, no real mode, ever, in Phase 1.** The entire hand-off is
   ~1,100–1,400 lines of code in the two production references (Firecracker + rust-vmm
   `linux-loader`) `M`. The state the kernel demands at its 64-bit entry point is eleven items
   long and every one of them is a register write or a page-table store `S`.
2. **The legacy boot path we are refusing to implement is measurably small but not free.** Under
   QEMU's `-kernel`, SeaBIOS *does* still run: **5,324,079 executed instructions below 1 MB,
   0.47 % of a boot, 4,535 distinct static instructions** `M`. That is the real-mode/16-bit
   surface. It is small enough that "we could add it later" is credible, and large enough that
   "it is free" is false.
3. **x87 is not a Phase-1 problem and is not a performance problem — it is a pure correctness
   problem.** A full boot to a shell executes **8 x87 instructions, of which 0 are arithmetic**
   `M`. A 34-billion-instruction 7-Zip run executes the same 8 `M`. The moment userspace formats a
   floating-point number with musl `printf`, genuine 80-bit x87 appears — `fldt`/`fstpt`,
   `fldcw`, `fistpll` — but only **1,535 instructions in 43.8 billion (3.5 × 10⁻⁶ %)** `M`.
   **Verdict: correct soft-float80, no fast path, no reduced-precision mode. It cannot matter for
   performance and it can only hurt on correctness.**
4. **Not advertising LA57 is not merely permitted — it is load-bearing.** If we advertise
   `CPUID.7.0:ECX[16]`, the kernel builds a trampoline that **leaves long mode into 32-bit
   protected mode with paging disabled and re-enters** `S(pgtable_64.c:104-197)`. With LA57
   unadvertised and `CR4.LA57=0` at entry, `configure_5level_paging()` returns at its first
   branch and that transition never happens `S(pgtable_64.c:136-137)`. **This is what makes
   "a directly-booted UP Linux never leaves long mode" true.**
5. **SMP is the one thing that drags real mode back in.** AP bring-up on x86-64 Linux still goes
   through a 16-bit real-mode SIPI trampoline `S(trampoline_64.S:61-100)`. Uniprocessor first is
   therefore not a convenience in the milestone ladder — it is what lets Milestones 1–5 exist at
   all.
6. **SMEP/SMAP/NX map onto ARM XN/PXN/PAN faithfully and cheaply; `CR0.WP=0` does not map at
   all.** The x86 (U/S, R/W) 4-tuple is a *bijection* with ARM `AP[2:1]` `I`. `CR0.WP=0` has no
   ARM encoding and is the one whole-shadow invalidator with no cheap answer `I`.

---

## 1. (A) Skip the legacy boot entirely — the direct kernel boot

### 1.1 What the kernel actually demands

The 64-bit entry point is at **kernel load address + 0x200**, and its ABI is frozen:

> "64bit entry is 0x200 and it is ABI so immutable! We come here either from startup_32 or
> directly from a 64bit bootloader. If we come here from a bootloader, kernel(text+data+bss+brk),
> ramdisk, zero_page, command line could be above 4G. **We depend on an identity mapped page table
> being provided that maps our entire kernel(text+data+bss+brk), zero page and command line.**"
> — `S(arch/x86/boot/compressed/head_64.S:278-288)`, entry forced by `.org 0x200` at `:277`

`Documentation/arch/x86/boot.rst` states the required state as `C`
([kernel.org](https://www.kernel.org/doc/html/latest/arch/x86/boot.html)):

| # | Requirement | How we satisfy it |
|---|---|---|
| 1 | "CPU must be in 64-bit mode with paging enabled" | `CR0.PE\|CR0.PG`, `CR4.PAE`, `EFER.LME\|EFER.LMA` |
| 2 | "A GDT must be loaded with the descriptors for selectors `__BOOT_CS`(0x10) and `__BOOT_DS`(0x18)" | write 4 quadwords into guest RAM, set GDTR |
| 3 | "Both descriptors must be 4G flat segment" | limit 0xFFFFF, G=1 |
| 4 | "`__BOOT_CS` must have execute/read permission, `__BOOT_DS` read/write" | type bits |
| 5 | "CS must be `__BOOT_CS` and DS, ES, SS must be `__BOOT_DS`" | selector + hidden descriptor cache |
| 6 | "Interrupt must be disabled" | `RFLAGS = 0x2` |
| 7 | "`%rsi` must hold the base address of the struct boot_params" | one register |
| 8 | ident map covering `init_size` from the kernel start, plus zero page and cmdline | build a PML4/PDPT/PD |

**The doc is stricter than reality on item 2/5** `I`: `startup_64` immediately zeroes DS/ES/SS/FS/GS
(`S(head_64.S:293-300)`) and then reloads *its own* GDT and CS via `lgdt`/`lretq`
(`S(head_64.S:357-366)`). So only CS need be a valid 64-bit code segment at entry. Firecracker
confirms this empirically by using selectors **0x08/0x10**, not the documented 0x10/0x18
`S(firecracker src/vmm/src/arch/x86_64/regs.rs:201-246)`.

### 1.2 The recipe, from production code

`S(firecracker src/vmm/src/arch/x86_64/regs.rs)` — 492 lines total, 285 before `mod tests` `M`:

```
GDT (4 entries at guest-phys 0x500), IDT stub at 0x520:
  [0] gdt_entry(0x0000, 0, 0)        NULL
  [1] gdt_entry(0xa09b, 0, 0xfffff)  CODE   L=1 (64-bit), G=1, P=1, DPL=0, type=0xb
  [2] gdt_entry(0xc093, 0, 0xfffff)  DATA   D/B=1, G=1, P=1, type=0x3
  [3] gdt_entry(0x808b, 0, 0xfffff)  TSS    (a KVM VMX-entry requirement, not a Linux one)
  CS=sel 0x08; DS=ES=FS=GS=SS=sel 0x10; TR=sel 0x18

Boot page tables (3 pages, 4 KiB apart, in low guest RAM):
  PML4  @ 0x9000 : [0] = 0xa000 | 0x03            (P|RW)      -> covers VA [0, 512 GiB)
  PDPT  @ 0xa000 : [0] = 0xb000 | 0x03            (P|RW)      -> covers VA [0, 1 GiB)
  PD    @ 0xb000 : [i] = (i << 21) | 0x83, i<512  (P|RW|PS)   -> 512 x 2 MiB, ident [0, 1 GiB)
  CR3 = 0x9000 ; CR4 |= PAE(0x20) ; CR0 |= PE(0x1) | PG(0x8000_0000)
  EFER |= LME(0x100) | LMA(0x400)

Registers: RFLAGS = 0x2 ; RIP = entry ; RSP = RBP = 0x8ff0 ; RSI = 0x7000 (zero page)
FPU:       FCW = 0x37f ; MXCSR = 0x1f80
```

Memory layout `S(firecracker .../layout.rs)`: `ZERO_PAGE_START = 0x7000`,
`CMDLINE_START = 0x20000` (max 2048 B), `HIMEM_START = 0x100000`, `RSDP_ADDR = 0xe0000`,
`BOOT_STACK_POINTER = 0x8ff0`.

e820, exactly four kinds of entry `S(firecracker .../mod.rs:388-417)`:
`[0, 0x9fc00)` RAM · `[0x9fc00, 0xe0000)` RESERVED (EBDA/BIOS) · PCI MMCONFIG window RESERVED ·
then one RAM entry per DRAM region starting at `max(1 MiB, region_start)`. `boot_params.acpi_rsdp_addr`
is set so the kernel does not have to scan low memory for the RSDP `S(mod.rs:381-386)`.

`type_of_loader = 0xff` (KERNEL_LOADER_OTHER), `boot_flag = 0xaa55`, `header = 'HdrS'`,
`cmd_line_ptr`, `cmdline_size`, `ramdisk_image`, `ramdisk_size` `S(mod.rs:358-379)`. **The whole
`boot_params` page must be zeroed first** — the `sentinel` byte at offset 0x1ef exists precisely to
catch loaders that copy garbage `S(bootparam.h:143-155)`.

### 1.3 The kernel we actually boot, decoded

`probes/qemu_baseline/bzimage_hdr.py` on the exact image the harness boots
(`work/nb-x86_64/boot/vmlinuz-virt`, Linux 6.18.35-0-virt) `M`:

| field | value | consequence |
|---|---|---|
| `header` @0x202 | `HdrS` | valid |
| `version` | **2.15** | modern protocol |
| `setup_sects` | 31 | protected-mode kernel begins at file offset **(31+1)×512 = 16384** |
| `loadflags` | `LOADED_HIGH` | load above 1 MiB |
| `xloadflags` | `XLF_KERNEL_64 \| XLF_CAN_BE_LOADED_ABOVE_4G \| XLF_EFI_HANDOVER_64 \| XLF_EFI_KEXEC \| **XLF_5LEVEL** \| **XLF_5LEVEL_ENABLED**` | 64-bit entry exists; **and the kernel is 5-level-capable** |
| `pref_address` / `kernel_alignment` | 0x1000000 / 0x1000000 | 16 MiB |
| `relocatable_kernel` / `min_alignment` | 1 / 2²¹ | we may place it |
| `init_size` | **0x2604000 = 38.0 MiB** | **this is how much must be identity-mapped** |
| `cmdline_size` | 2047 | command-line cap |

Note the interaction: Firecracker identity-maps only 1 GiB. Alpine's `init_size` is 38 MiB and the
kernel loads at 1–16 MiB, so it fits `I`. **Design rule:** identity-map all of guest RAM with 2 MiB
entries rather than a fixed 1 GiB, so that an initrd placed high is also covered. Cost is 512 PD
entries per GiB — and 2 MiB guest pages are exactly the case `16 §4` wants us to shadow with ARM
2 MiB blocks rather than 512 4 KiB descriptors.

### 1.4 What it costs to implement

Measured line counts of the two production references `M`:

| Component | Lines | Note |
|---|---|---|
| rust-vmm `linux-loader` `src/loader/bzimage/mod.rs` | 324 | incl. tests; validates `HdrS`, `version >= 0x0200`, `loadflags & 1`, copies the payload |
| Firecracker `arch/x86_64/regs.rs` | 492 (285 pre-tests) | sregs, GDT, page tables, FPU |
| Firecracker `arch/x86_64/mod.rs` | 814 (591 pre-tests) | `configure_system_for_boot`, e820 |
| Firecracker `arch/x86_64/gdt.rs` | 147 (123 pre-tests) | descriptor encoding |
| Firecracker `arch/x86_64/layout.rs` | 136 | constants only |

**≈1.1–1.4 kLoC total, in Rust, with tests.** `I` Against the 28–48 person-month Tier-2 estimate in
`08 §2`, the boot loader is **not a line item**; it is under a person-week. The schedule risk in
"boot" lives entirely in CPUID/MSR/ACPI/device breadth (`06 §10`, `10 §5.1`), not in the hand-off.

### 1.5 What this forecloses — stated plainly

| Capability lost | Because |
|---|---|
| **BIOS / UEFI / OVMF / SeaBIOS** | all start in 16-bit real mode at `0xFFFF_FFF0` |
| **Any bootloader** (GRUB, systemd-boot, syslinux) | they are firmware clients |
| **Unmodified bootable disk images and install ISOs** | the MBR/El Torito path is real mode |
| **Option ROMs, VGA BIOS, INT 10h/13h/15h** | real mode |
| **Windows, at any version** | requires UEFI x64 (or BIOS) firmware; there is no direct-kernel-boot equivalent |
| **Any non-Linux OS** | the boot protocol is Linux-specific |
| **`-cdrom`-style installs; testing firmware itself** | ditto |
| **PXE / iPXE network boot** | option ROM |

This is exactly the trade Firecracker and cloud-hypervisor made: direct-boot-only VMMs that ship no
firmware at all, and whose entire x86 boot path is the ~1.1 kLoC in §1.4 `M`. The trade is
defensible **only** because `07 §2.1`/`08 §2` already scope Phase 1 to "stock x86-64 Linux → shell".
If the product statement ever becomes "boot the customer's existing disk image", this decision
reverses and Section 2 becomes mandatory.

**Cost of the road not taken:** implementing firmware boot means real mode + unreal mode + A20 +
16-bit protected mode + 32-bit protected mode with non-flat segments + the 16→32→64 ladder. `06 §10.3`
already recommended deferring it; Section 2 below scopes it.

### 1.6 Is the direct-boot path really free of legacy execution? Measured.

QEMU's `-kernel` is *not* a clean control here, and this matters. Captured SeaBIOS debug output from
the exact harness invocation `M` (`results/isaclass/seabios.log`):

```
SeaBIOS (version rel-1.17.0-0-gb52ca86e094d-prebuilt.qemu.org)
Running on QEMU (q35)
boot order:
1: /rom@genroms/linuxboot_dma.bin
```

So every number in `15-qemu-tcg-baseline.md` for "x86-64 boot" includes a full real-mode firmware
boot. Splitting the boot by linear address `M`:

| Region | executed insns | % of boot | distinct static insns |
|---|---:|---:|---:|
| `vaddr < 1 MiB` — **SeaBIOS + linuxboot ROM + kernel 16-bit setup** | **5,324,079** | **0.468 %** | **4,535** |
| 1 MiB – 4 GiB — decompressor, 32-bit head, static userspace | 566,463,548 | 49.80 % | 10,329 |
| 4 GiB – kernel half — PIE/mmap userspace | 3,302,608 | 0.290 % | 17,223 |
| kernel half (`≥ 0xffff800000000000`) | 562,374,275 | 49.44 % | 464,583 |
| **total** | **1,137,464,510** | 100 % | 496,670 |

Read this two ways. **(a)** The legacy boot is 0.47 % of boot instructions — direct boot buys almost
nothing in *runtime*. **(b)** It is 4,535 distinct instructions of 16-bit, segmented,
mode-switching code — and *that* is the number that maps to engineering effort. Direct boot buys
schedule, not speed.

*Caveat `?`:* the `< 1 MiB` bucket is a proxy for "legacy-mode code", not a CPU-mode read. QEMU's
reset vector at `0xFFFF_FFF0` lands in the 1 MiB–4 GiB bucket, so the legacy figure is a slight
**under**count. The experiment that would settle it exactly: a plugin using
`qemu_plugin_register_vcpu_insn_exec_cb(..., QEMU_PLUGIN_CB_R_REGS)` reading `CR0.PE`/`EFER.LMA`
per instruction — ~50× slower but exact.

---

## 2. (B) If we do need legacy modes later — the minimum, and its cost under shadow paging

Difficulty column is **specific to a hardware-MMU shadow-paging design**, which is a different
answer from "difficulty in a softmmu interpreter".

| Feature | Needed for direct-boot Linux? | Needed for Windows? | Difficulty under shadow paging | Notes |
|---|---|---|---|---|
| **Real mode (16-bit, seg×16, 20-bit wrap)** | **No** — except SMP AP bring-up (§2.2) | **Yes** (UEFI still starts in real mode at the reset vector) | **3** — *not* an MMU problem: real mode has no paging, so the shadow is a flat identity map. It is a *decoder* problem: 16-bit operand/address-size defaults, and every memory op is `base = seg<<4`. | Best served by a small interpreter, not the JIT |
| **A20 gate** | No | Yes (early firmware) | **2** — one AND-mask on linear addresses, in the interpreter only | Dead by the time protected mode starts |
| **Big/unreal mode** (16-bit CS, 4 GiB DS limit) | No | Yes (some option ROMs) | **1** given the descriptor cache | Falls out of §2.3 |
| **32-bit protected mode, flat** | **No** (skipped) | Yes, briefly | **2** | The 4 GiB flat case needs no segment arithmetic |
| **32-bit protected mode, non-flat bases/limits** | No | Rare | **4** — see §2.3 | The genuinely hard one |
| **16→32→64 transitions** | **No** for UP (§3.1); **yes** for SMP | Yes | **4** — each transition rebuilds or discards the whole shadow, §3 | |
| **`LGDT`/`LIDT`** | **Yes, 1,285 executed per boot** `M` | Yes | **1** — store into our descriptor-table shadow; `LIDT` retargets our x86-exception dispatch | 1,274 of the 1,285 execute below the kernel half, i.e. in firmware + early boot `M` |
| **`LLDT` / LDT** | No for boot; **yes if 32-bit userspace runs** (`CONFIG_MODIFY_LDT_SYSCALL=y` in this kernel `M`) | Yes (WoW64) | **3** | |
| **`LTR` / TSS as a data structure** | **Yes** (RSP0, IST1-7) | Yes | **1** — read fields on ring transition, never task-switch | `06 §3.3` |
| **Hardware task switching / task gates** | **No** — does not exist in 64-bit mode | No | **do not implement** — raise `#GP` | `06 §3.3` |
| **Call gates** | No | No | **do not implement** | |
| **V8086 mode** | **No** | **No** (Win64 dropped NTVDM) | **do not implement**; `#GP` on `EFLAGS.VM` set | Only DOS software cares |

### 2.1 The one measured surprise: descriptor-table traffic is not zero

`LGDT/LIDT/LLDT/LTR/SGDT/SIDT/SLDT/STR` executed **1,285 times per boot**, of which **11** are in
the kernel half `M`. So a direct-booted kernel *does* touch descriptor tables — but ~99 % of the
traffic is firmware and early boot that direct boot deletes. The residual (per-CPU `lgdt`/`lidt`/`ltr`
during CPU bring-up) is trivial `I`.

### 2.2 SMP AP bring-up drags real mode back in — this is the sharp edge

Linux starts secondary CPUs by sending INIT–SIPI–SIPI; the SIPI vector is a **real-mode CS:IP**, and
`trampoline_start` is `.code16` `S(arch/x86/realmode/rm/trampoline_64.S:38,61)`. The 16-bit body is
short and fully enumerable `S(trampoline_64.S:61-100)`:

```
cli; wbinvd; ljmpw; mov %cs,%ax; mov %ax,{ds,es,ss}; lock btsl (spinlock); mov $rm_stack_end,%esp
call verify_cpu            # CPUID + RDMSR/WRMSR
lidtl tr_idt ; lgdtl tr_gdt
movl $(CR0_STATE & ~PG),%eax ; movl %eax,%cr0        # -> 32-bit protected mode
ljmpl $__KERNEL32_CS, $pa_startup_32
```

then 32-bit: `wrmsr`, `movl pa_tr_cr4,%eax; mov %eax,%cr4` (PAE), set `EFER.LME`, `mov %eax,%cr0`
(PG) → compatibility mode → `ljmp` to 64-bit `S(trampoline_64.S:124-207)`.

Three options, in increasing order of honesty:

1. **`-smp 1` for Milestones 1–5.** Costs nothing, defers everything. **Recommended.**
2. **Intercept the SIPI.** We own the LAPIC (`10 §1.3`), so on SIPI we can read
   `trampoline_header` (`tr_start`, `tr_efer`, `tr_cr4`, `trampoline_pgd`
   `S(trampoline_64.S:283-292)`) out of guest memory and start the AP directly in long mode at
   `tr_start`. **This is a PV hack that depends on kernel-internal layout and will break.** State
   it as such if used.
3. **A ~2–4 kLoC 16/32-bit interpreter** that only has to execute those ~100 instructions correctly.
   This is the defensible answer and it is *also* the first brick of a future firmware path.

**Recommendation:** ladder Milestone 6 (SMP) onto option 3, not option 2. `19-smp-scaling.md`'s
numbers only become collectible after this.

### 2.3 Non-flat segment bases — separate the two questions

**Legacy segmentation (32-bit non-flat bases/limits) — refuse for now.** ARM stage-1 has no
segmentation. A non-zero base means every memory operand becomes `ADD tmp, x_base, x_index` before
the load, and a finite limit means a compare-and-branch per access. The mitigation that makes this
tractable is *not* an MMU trick, it is **specialising translated code on the segment descriptor
cache** (`06 §3.2`): if `base == 0 && limit == 0xFFFFFFFF` (overwhelmingly the common case) emit
nothing, and invalidate translations keyed on the segment register when it or its GDT/LDT entry is
written. Difficulty 4, but the *flat* case costs zero.

**FSGSBASE in long mode — mandatory, and cheap, and it is not legacy.** Measured on the boot `M`:

| | executed / boot |
|---|---:|
| instructions with an `%fs:`/`%gs:` override | **4,487,371 (0.394 %)**, 4,484,609 of them in the kernel half |
| `SWAPGS` | 12,102 (all kernel) |
| `RDFSBASE/WRFSBASE/RDGSBASE/WRGSBASE` | 8,542 (all kernel) |

Under the fork/exec-heavy `spawn` workload the segment-override share **rises to 0.627 %** and
`SWAPGS` to 91,128 `M`; under 7-Zip, `SWAPGS` is 139,664 `M`. Kernel per-CPU data (`%gs:`) and
userspace TLS (`%fs:`) are hot. `06 §3.1`'s plan — pin `fs_base`/`gs_base` in two ARM GPRs, one
extra `ADD` per access, `SWAPGS` as a 2-instruction swap — is confirmed as the right shape by the
measurement: at 0.4–0.6 % of instructions, one extra `ADD` costs well under 1 % `I`.

**Do not conflate these.** "We refuse legacy segmentation" and "we implement FS/GS bases" are
compatible statements; the second is non-negotiable.

---

## 3. (C) Mode-transition mechanics in a shadow-paging design

### 3.1 The claim we can now make, and its precondition

> **A directly-booted, uniprocessor, LA57-unadvertised x86-64 Linux never leaves long mode and
> never changes paging mode after entry.** `I`, resting on `S` below.

Evidence: `configure_5level_paging()` is called unconditionally from `startup_64`
`S(head_64.S:399-409)`, and its first real act is:

```c
if (!cmdline_find_option_bool("no5lvl") &&
    native_cpuid_eax(0) >= 7 && (native_cpuid_ecx(7) & BIT(16))) {
        l5_required = true;   /* ... */
}
/* The trampoline will not be used if the paging mode is already set to
 * the desired one. */
if (l5_required == !!(native_read_cr4() & X86_CR4_LA57))
        return;
```
`S(arch/x86/boot/compressed/pgtable_64.c:122-137)`

With `CPUID.7.0:ECX[16] = 0` and `CR4.LA57 = 0` at entry, `false == false` → **early return**. If we
advertised LA57 the kernel would instead build a trampoline in low memory, drop to 32-bit protected
mode with paging off, toggle `CR4.LA57`, and re-enter long mode — because *"Setting or clearing
CR4.LA57 in long mode would trigger #GP"* `S(head_64.S:336-355)`. It would also read the real-mode
BIOS data area at physical `0x40e`/`0x413` to place that trampoline `S(pgtable_64.c:52-57)`.

The kernel we boot has **`XLF_5LEVEL` and `XLF_5LEVEL_ENABLED` set** `M` — it is fully willing. The
only thing stopping it is our CPUID. `14`'s `VARange = 0` constraint therefore has a second,
sharper consequence than "48-bit VA ceiling": **advertising LA57 would force us to implement a
64→32→64 mode transition on the boot path.**

### 3.2 Control-register traffic, measured

Per boot `M`:

| event | count | of which kernel-half |
|---|---:|---:|
| `mov → CR0` | **516** | 7 |
| `mov → CR3` | **19,905** | 19,900 |
| `mov → CR4` | **65** | 62 |
| `mov CRn →` (read) | 1,602 | 835 |
| `INVLPG`/`INVPCID` | 38,451 | all |
| `RDMSR`/`WRMSR` | 165 | 129 |
| `CPUID` | 981 | 690 |
| `IN`/`OUT` | 740,751 | 738,238 |

509 of 516 `CR0` writes are below the kernel half — i.e. **firmware and the 16→32→64 ladder**. A
direct boot deletes essentially all `CR0` traffic `I`. `CR4` writes are 65 in an entire boot: these
are one-time feature enables, not a hot path. `CR3` writes are the real workload at ~20 k/boot, and
that is `16 §5`'s ASID problem, not a mode-transition problem.

### 3.3 What each transition does to the shadow

| Guest event | Shadow response | Rebuild or re-mode? |
|---|---|---|
| `CR3` write (same paging mode) | switch ARM `TTBR0_EL1` to the shadow root for that PCID/ASID; if unseen, allocate an ASID and demand-fill | **neither** — routine. `13 ns` for `MSR TTBR0_EL1 + ISB` `16 §1` |
| `CR0.PG 0→1` | the guest goes from physical to virtual addressing. Before: shadow = identity. After: shadow = walk of `CR3`. | **full rebuild** — but it happens ≤ 2× per CPU lifetime |
| `CR0.PG 1→0` | back to identity | **full rebuild** |
| `CR0.WP 1→0` | **no ARM encoding exists** (§3.4) | **hard** — see below |
| `CR0.PE 0→1` (real→protected) | different translation mode entirely | **different translator**, not a rebuild |
| `CR4.PAE 0→1` | guest PTE format changes 32-bit → 64-bit; our walker must switch table readers | **full rebuild**; walker is mode-parameterised |
| `CR4.PSE` | enables 4 MiB pages in non-PAE mode | rebuild; shadow them as ARM 2 MiB blocks + a second 2 MiB block `I` |
| `CR4.PGE` (global pages) | x86 "don't flush on CR3 write". ARM has **nG** (bit 11 of the descriptor) with the inverse polarity: nG=0 means global. Map `PTE.G=1 → nG=0`. | **incremental** — but flipping PGE off must flush, since previously-global entries survive ASID changes `I` |
| `CR4.LA57` | **must never happen** (§3.1); `VARange=0` makes it unimplementable `14` | **refuse — do not advertise** |
| `CR4.SMEP` | recompute PXN on every user-accessible shadow descriptor | **full rebuild**, once |
| `CR4.SMAP` | switch to using `PSTATE.PAN` (§3.5) | **no rebuild** — it is a PSTATE bit |
| `CR4.FSGSBASE` | enables `RD/WRFSBASE` at CPL 3 | **no MMU effect**; decoder gate only |
| `EFER.LME 0→1` | arms long mode; takes effect at the next `CR0.PG` write | no immediate effect |
| `EFER.LMA` | read-only status | none |
| `EFER.NXE 0→1` | PTE bit 63 changes meaning from *reserved* to *NX* | **full rebuild** — before NXE, bit 63 set is a reserved-bit `#PF`; after, it is XN `I` |

**Rebuild is affordable precisely because these are boot-time events.** With 65 `CR4` writes and
516 `CR0` writes per whole boot `M`, even a 10 ms full-shadow rebuild costs under 6 s of… no: at 65
events it costs 0.65 s worst case, and in practice the mode-affecting subset is <10 events `I`. **A
full-shadow rebuild on any of PG/PAE/PSE/PGE/SMEP/NXE is the correct engineering answer.** Do not
build incremental machinery for them.

### 3.4 `CR0.WP` — the one that does not map

ARM stage-1 `AP[2:1]` has exactly four encodings and no "EL1 may write what EL0 may only read":

| `AP[2:1]` | EL1 | EL0 |
|---|---|---|
| 00 | RW | — |
| 01 | RW | RW |
| 10 | RO | — |
| 11 | RO | RO |

x86 (`U/S`, `R/W`) with `CR0.WP=1` is a **bijection** onto this `I`:
`(S,RW)→00`, `(S,RO)→10`, `(U,RW)→01`, `(U,RO)→11`. Supervisor read/write of user pages: allowed on
both. User access to supervisor pages: denied on both. **Faithful.**

With `CR0.WP=0`, x86 lets ring 0 write pages marked read-only. There is no ARM encoding, because
clearing `AP[2]` would also grant EL0 write access. Options, none free:
(a) rebuild the shadow with `AP[2]=0` everywhere and lose user-RO enforcement for the duration;
(b) trap and interpret stores while `WP=0`; (c) refuse and `#GP`.
**`?` UNRESOLVED — the experiment:** extend `plugin_isaclass.c` to decode the *value* written to
`CR0` (needs `QEMU_PLUGIN_CB_R_REGS`) and count `WP` 1→0 transitions across boot, `spawn`, module
load, and a `ftrace`/`kprobe` workload. If modern Linux never clears `WP` (it now uses a temporary
mm for `text_poke` rather than clearing `WP`) then (c) is acceptable for Phase 1. **This is the
single highest-value follow-up in this document.**

### 3.5 SMEP / SMAP / NX → XN / PXN / PAN — verified mapping

`14` measured `ID_AA64MMFR1_EL1`: **`XNX = 1` (FEAT_XNX)** and **`PAN = 2` (FEAT_PAN2)** `M(14)`.
Both are the features this mapping needs.

| x86 | ARM stage-1 | Faithful? |
|---|---|---|
| `PTE.NX` on a **supervisor** page | `PXN = 1` (bit 53) | ✅ |
| `PTE.NX` on a **user** page | `UXN = 1` (bit 54) | ✅ |
| `CR4.SMEP = 1` | set `PXN = 1` on every descriptor with `AP[1] = 1` | ✅ — SMEP *is* PXN-on-user-pages `I` |
| `CR4.SMAP = 1` | `PSTATE.PAN = 1` while at EL1 | ✅ |
| `STAC` (set `EFLAGS.AC`, i.e. *allow*) | `MSR PAN, #0` | ✅ — **note the inversion**: AC=1 means "SMAP off"; PAN=1 means "access off" |
| `CLAC` | `MSR PAN, #1` | ✅ |
| `AT S1E1RP` / `S1E1WP` (PAN-aware address translation) | available with FEAT_PAN2 `M(14)`, 26.4 ns `M(16 §item18)` | ✅ — lets the fault handler ask "would this have faulted under PAN?" |

Without `FEAT_XNX` there is a single `XN` bit and SMEP would be inexpressible. **`XNX=1` is
therefore a load-bearing gate, and `14` closed it.**

**`?` One residual fidelity question.** ARM has an architectural rule in the EL1&0 regime forcing
`PXN` on regions writable at EL0 (the ARMv8 analogue of always-on SMEP). If that rule is
unconditional on this hardware, then a guest running with `CR4.SMEP = 0` that *legitimately*
executes kernel code out of a user-writable page will fault where real x86 would not. **The
experiment:** in `probes/g1_mmu4k`-style code, map one 4 KiB page `AP=01` (EL0 RW) with descriptor
`PXN=0`, branch into it from EL1, and check whether `ESR_EL1` reports a permission fault
(`EC=0x21`, instruction abort). One afternoon; it decides whether "advertise SMEP" is a
recommendation or a requirement. **Provisional recommendation regardless: advertise SMEP and SMAP.**
If ARM forces the conservative behaviour, advertising SMEP makes the guest agree with it.

*Also note `HAFDBS = 0` `M(14)`:* Accessed and Dirty are fault-driven, measured at 139 ns per A-bit
fault without a TLBI and 441 ns with one `M(16 items 16-17)`. That is orthogonal to mode
transitions but it is the tax on every shadow rebuild's first touch.

---

## 4. (D) The x87 / legacy-FP cliff — measured, then decided

### 4.1 The measurement

New plugin `plugin_isaclass.c`; four scenarios; all counts are **executed** guest instructions `M`:

| Scenario | total insns | **x87** | **x87 arith** | MMX | AVX | SSE-int | SSE-fp | FXSAVE grp |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| boot → shell → poweroff (`task=none`) | 1,137,464,510 | **8** | **0** | 0 | 0 | 249,902 | 929 | 243 |
| + 400 × fork/exec busybox (`task=spawn`) | 1,513,340,257 | **8** | **0** | 0 | 0 | 430,981 | 20,703 | 4,795 |
| + 7-Zip 26.01 benchmark (`task=sevenzip`) | 34,208,957,740 | **8** | **0** | 0 | 0 | 284,742 | 3,463 | 1,378 |
| + full `bench` suite, which `printf`s doubles | 43,801,187,169 | **1,535** | **1,299** | 0 | 0 | 7,134,641,602 | 155,341 | 1,833 |

The 8 x87 instructions in the first three rows are **the same 8** — `FNINIT ×4`, `FNSTSW ×2`,
`FNSTCW ×2` `M` — i.e. FPU initialisation by the kernel and by musl's startup, and nothing else.
**A Linux x86-64 kernel boot performs zero x87 arithmetic.**

The fourth row is the interesting one. The only difference is that `bench` calls `printf("%.4f", …)`.
musl's `fmt_fp()` takes a `long double`, and on x86-64 `long double` **is** the x87 80-bit type, so
formatting a `double` drags in the x87 unit. The 22 distinct mnemonics executed `M`:

```
fldcw 152   fstpt 140   fstp 136   fucomi 116   fld 116   fldt 100   fldz 96
fimull 96   fxch 96     fnstcw 78  fildll 76    fistpll 76 fsubrp 76  fldl 60
fucompi 20  fmulp 20    fildl 20   fdivs 20     fadd 20   fiaddl 15
fninit 4    fnstsw 2
```

Three things to read out of that list `I`:
- **`fldt` / `fstpt` are 80-bit loads and stores.** Genuine extended precision is on the wire, not
  just in the register file.
- **`fldcw` / `fnstcw` (152 / 78)** means the code *changes the control word* — precision control
  and/or rounding mode. A soft-float implementation must honour PC=24/53/64, not just RC.
- **`fildll` / `fistpll` paired with `fldcw`** is the classic "set RC=truncate, convert long double
  → int64, restore" idiom. Integer-conversion corner cases (`fist` overflow → integer indefinite)
  are therefore live, and FEX ships eight separate unit tests for exactly them
  `C(unittests/ASM/X87/invalid_fist_overflow*.asm)`.

**Share of total: 1,535 / 43,801,187,169 = 3.5 × 10⁻⁶ %.**

*Caveats `?`:* musl, not glibc — glibc's `printf`/`strtod`/`strtold` and its `libm` `long double`
entry points use *more* x87 than musl, so this is a lower bound. No X11, no graphics, no Wine, no
32-bit binaries (where the i386 ABI returns floats in `ST(0)` and x87 is pervasive `C(06 §9.3)`).
Single kernel, single distro, `n=1` per scenario — but the x87 count was byte-identical (8) across
three independent boot runs `M`.

### 4.2 What the prior art does

- **Rosetta 2:** x87 is **not** translated to native AArch64. It is a software 80-bit
  extended-precision library — `rosetta::runtime::library::x87_*` over an explicit `X87State`
  `C(04:502-517)`. Measured on this host: a `long double` recurrence runs at **~6 % of native**
  `M(04:526)`. Community projects (`rosettax87`, `x87sidecar`) hook `translate_insn` to replace it
  and report 4–5× on x87-heavy code `C(04:533-536)`.
- **FEX:** ships **two complete x87 back-ends** — `X87.cpp` (80-bit soft-float, 29.6 kB) and
  `X87F64.cpp` (reduced precision, 14.1 kB) `C(FEX repo)`. `X87F64.cpp` states the trade in a
  comment: *"ignore the rounding precision, we're always 64-bit in F64"* `C`. FEX's own framing:
  soft-float is *"quite accurate, [but] obscenely slow"*; the F64 path exists because *"most Windows
  games configure the x87 unit to be lower precision than 80-bit"*
  `C([fex-emu.com](https://fex-emu.com/FEX-2302/))`.
  The strongest evidence that the two paths are **observably different**: FEX maintains two parallel
  test suites, `unittests/ASM/X87/` and `unittests/ASM/X87_F64/`, ~200 files each, with different
  expected results `C`.
- **box64:** x87 is not true 80-bit `C(05:500)`.

### 4.3 The three options, and exactly what each breaks

| Option | Speed | What breaks |
|---|---|---|
| **Full 80-bit soft-float** (Berkeley SoftFloat / QEMU `softfloat`) | 50–200 cycles/op, 20–50× vs hardware `C(06 §9.3)` | nothing |
| **64-bit `double` approximation** | ~1 op | see below |
| **Trap-and-emulate** (`#NM` via `CR0.TS`/`EM`) | thousands of cycles/op | nothing, but slower than soft-float and adds an exception path |

The `double` approximation breaks in **four** observable ways, and the usual write-up only mentions
the first `I`:

1. **Mantissa:** 64 explicit bits → 53. Any accumulation loop diverges; `printf("%.20Lf")` prints
   wrong digits.
2. **Exponent range — the one people forget.** x87 has a 15-bit exponent (max ≈ 1.19 × 10⁴⁹³²);
   `double` has 11 bits (max ≈ 1.8 × 10³⁰⁸). An intermediate that x87 represents finitely becomes
   `+Inf`. `expl`, `powl`, `tgammal`, and any scaled-accumulator idiom silently produce infinities.
   Symmetrically, underflow to zero happens ~4600 decades early.
3. **Precision control is unimplementable.** `FLDCW` with PC=24/53/64 is measured live in our own
   guest (152 executions `M`). A `double` back-end can honour PC=53 and PC=24 only approximately and
   PC=64 not at all.
4. **`FXSAVE`/`FSAVE` memory images are wrong.** The 80-bit register images are architecturally
   visible in memory; anything that saves, inspects, or transports FPU state (a signal handler,
   `ptrace`, a checkpoint, a debugger) sees the wrong bytes.

### 4.4 Recommendation

> **Implement correct 80-bit soft-float. Do not build a reduced-precision fast path. Do not build
> trap-and-emulate.**

The justification is the measurement, not taste `I`: at **3.5 × 10⁻⁶ %** of instructions, even a
200× per-instruction penalty is **0.0007 % of runtime**. There is no performance argument to be had.
FEX's reduced-precision mode exists because FEX's workload is 32-bit Windows games where x87 is
pervasive — a workload we have explicitly scoped out (`07 §2.1`). Adopting their fast path would
import their correctness debt (two test suites, two sets of expectations) to buy a rounding error in
the fifth decimal place of a number nobody measures.

**Correctness caveat, stated plainly:** soft-float80 is only "correct" if we also get the
*non-arithmetic* state right — the rotating register stack with `TOP` in the status word, the tag
word, `FIP`/`FDP`/`FOP`, and **the fact that MMX aliases onto the x87 registers and mutates the tag
word** `C(06 §9.3)`. We measured **zero MMX instructions in 43.8 billion** `M`, so MMX can be a
correct-but-slow path — but it cannot be *absent*, because `EMMS` and the tag-word aliasing are
observable from x87 code.

### 4.5 SSE / AVX / XSAVE, and the MXCSR↔FPCR mapping

Measured `M`: **zero AVX128 and zero AVX256 instructions in any scenario** — including 7-Zip and an
auto-vectorised matmul, because `-cpu max` under TCG advertises AVX but Alpine's binaries are built
for the x86-64 baseline. SSE-integer is the only vector traffic that matters at boot
(249,902/boot = 0.022 %) and it explodes to **16.3 %** in the vectorised `bench` `M`. This is
consistent with, and supports, `06 §9.1`'s recommendation: **advertise x86-64-v2 (SSE4.2 + POPCNT +
CMPXCHG16B), consider v3 later, never AVX-512** (128-bit NEON is the hard ceiling; YMM would consume
all 32 `V` registers).

`FXSAVE`/`XSAVE`-family: 243 per boot, 4,795 under `spawn` `M` — i.e. it scales with context
switches, exactly as expected, and is a *layout* problem not a speed problem. `XGETBV`/`XSETBV`:
2 per boot `M`. The `XSAVE` area layout and `XCR0` must be internally consistent with the CPUID
leaf-0xD sub-leaves we publish (`06 §10.1`), and getting that wrong is a classic early-bring-up
bug.

`MXCSR` ↔ `FPCR`/`FPSR` is already worked out in `06 §9.4` and this document does not revisit it,
except to re-flag the three items that bite: **the RC encodings 01/10 are transposed** between x86
and ARM; **x86 separates DAZ from FZ and base ARM `FPCR.FZ` does both** (`FEAT_AFP` fixes it but is
absent on M1/M2, present on M3/M4 — a *generation-dependent fidelity split*); and **Apple does not
implement trapped FP exceptions**, so an unmasked `MXCSR` requires a slow path that polls `FPSR`
after each op. All three are unchanged by anything measured here.

---

## 5. (E) The minimum viable milestone ladder

Each rung is independently demonstrable, names the one feature that unlocks it, and states its
observable. Deliberately, **no rung requires real mode until M6.**

| # | Milestone — the observable | Feature that unlocks it | Depends on |
|---|---|---|---|
| **M0** | **A VM starts and executes one ARM64 instruction at guest EL1, and we read `ESR_EL1` after a deliberate fault.** | Hypervisor.framework VM lifecycle + `VBAR_EL1` | already done — `14` G-1 |
| **M1** | **A hand-written x86-64 stub, translated by our JIT, writes `'H'` to port 0x3F8 and we see it on stdout.** | decoder + JIT + one MMIO/PIO exit path | M0 |
| **M2** | **The same stub runs under a shadow page table we built**: it faults on an unmapped page, we fill the shadow from an x86-format PTE tree we constructed, and it re-executes. | shadow-paging fill path (`16 §2`: 145.8 ns) | M1 |
| **M3** | **`bzImage` loads and the kernel prints its banner to the 16550 UART.** *"Linux version 6.18.35…"* | **Linux 64-bit boot protocol** (§1.2) + a 16550 | M2 |
| **M4** | **The kernel reaches `start_kernel` → `setup_arch` → prints the e820 map and survives `init_mem_mapping`.** | CPUID leaves 0/1/7/0xB/0xD/0x15/0x16/0x8000_000x + `EFER`/`STAR`/`LSTAR`/`FMASK`/`FS_BASE`/`GS_BASE`, and `CR3`-switch shadow support | M3 |
| **M5** | **Timer interrupts arrive; the scheduler runs; the kernel reaches `Freeing unused kernel image memory`.** | x2APIC (MSR path, no MMIO — `10 §1.5`), LAPIC timer / TSC-deadline, TSC synthesis (`06 §11.3`), IDT + `IRET` + ring transitions | M4 |
| **M6** | **`Run /init as init process` — the first userspace instruction executes.** | `SYSCALL`/`SYSRET`, `SWAPGS`, user/supervisor shadow permissions (§3.5), initramfs via `boot_params.ramdisk_*` | M5 |
| **M7** | **A busybox shell prints a prompt.** *(the `NATEMU_SHELL_UP` marker the QEMU harness already emits)* | virtio-console, or just the UART; `fork`/`execve` → many `CR3` switches → the ASID allocator (`16 §5`) | M6 |
| **M8** | **The shell runs `ls`, `cat /proc/cpuinfo`, and a static binary that `printf`s a float.** | **x87 soft-float** (§4.4), `FXSAVE`/`XSAVE` layout | M7 |
| **M9** | **virtio-blk: mount a real root filesystem; `apk` installs a package.** | virtio-blk + virtio-net + MMIO/PCI ECAM + minimal ACPI (RSDP/XSDT/FADT/MADT/MCFG/DSDT — adapt QEMU's AML, `06 §10.1`) | M8 |
| **M10** | **`-smp 2`: a second CPU comes online (`smpboot: Booting Node 0 Processor 1`).** | **16-bit real-mode interpreter for the SIPI trampoline** (§2.2) + IPI delivery + TSO (`14` G-4) | M9 |
| **M11** | **Self-hosting: compile a C file inside the guest.** | SMC/code-cache invalidation under a real page-cache workload (`06 §8`) — the first place the JIT design is genuinely stressed | M10 |
| **M12** | *(optional, scope change)* **OVMF boots.** | full real mode + A20 + non-flat protected mode + option ROMs | M11 + §2 |

Two properties worth calling out:

- **M3 is reachable with essentially no x86 system semantics.** The boot protocol hands the kernel a
  running long-mode CPU; the first thing it needs is a serial port. This is the earliest possible
  "it prints something".
- **The x87 rung (M8) is deliberately *after* the shell rung (M7).** The measurement says the shell
  comes up with 8 x87 instructions, none arithmetic — so `FNINIT`/`FNSTCW`/`FNSTSW` as stubs get you
  to M7, and the soft-float library is only needed the first time userspace formats a float.

**Cross-check against `08 §9`:** `08`'s sequencing (Tier 1 bring-up → nanokernel → x86 system
surface → performance closure) is *phase*-granular; this ladder is *demo*-granular and slots
underneath it. Nothing here contradicts the 28–48 pm estimate; §1.4 removes ~0 pm from it (the
loader was never the cost) and §2.2 **adds** an item that was not previously scheduled: a 16-bit
interpreter, ~2–4 kLoC, on the critical path to *any* SMP number. `19-smp-scaling.md` should note
that dependency.

---

## 6. (F) CPUID and feature advertisement

### 6.1 Must claim

Baseline `x86-64-v2` `C(06 §9.1)`: `FPU MMX SSE SSE2 SSE3 SSSE3 SSE4.1 SSE4.2 POPCNT CX16 CMOV
CLFLUSH FXSR MSR PAE PSE PSE36 PGE MTRR APIC SEP LM NX SYSCALL RDTSCP`.
Plus: `X2APIC` (`10 §1.5` — MSR path, no MMIO trap), `TSC_DEADLINE`, `CONSTANT_TSC` +
`NONSTOP_TSC` + `INVTSC` (`CPUID.8000_0007:EDX[8]`), `XSAVE`+`OSXSAVE`+leaf 0xD, `FSGSBASE`,
`SMEP`, `SMAP`, `PCID`, `ARCH_CAPABILITIES` (`CPUID.7.0:EDX[29]`), the hypervisor bit
(`CPUID.1:ECX[31]`) and leaves `0x4000_0000+` including `0x4000_0010` TSC-kHz (`06 §11.3`).

Two high-value claims:
- **`IA32_ARCH_CAPABILITIES` with `RDCL_NO|IBRS_ALL|MDS_NO|TAA_NO|SSB_NO`** — Linux then disables
  every speculative-execution mitigation. Honest (we are not an x86 pipeline) and worth a large
  single-digit percent `C(06 §10.1)`.
- **`SMEP` and `SMAP`** — §3.5: advertising them makes the guest's expectations match what ARM
  `PXN`/`PAN` enforce anyway.

### 6.2 Must NOT claim — the actively dangerous bits

| Feature bit | Why it is dangerous |
|---|---|
| **`LA57` — `CPUID.7.0:ECX[16]`** | **The worst one.** `ID_AA64MMFR2_EL1.VARange = 0` `M(14)` caps us at 48-bit VA, so 5-level tables are unshadowable — *and* claiming it forces the kernel through a 64→32→64 trampoline `S(pgtable_64.c:104-197)`. The guest kernel is `XLF_5LEVEL_ENABLED` and willing `M`. **Belt and braces: also pass `no5lvl` on the command line.** |
| **AVX-512 (`CPUID.7.0:EBX[16]` and friends)** | 32 × 512-bit ZMM + 8 mask registers ⇒ 128 ARM `V` registers needed, 32 exist `C(06 §9.1)`. Unimplementable, not just slow. |
| **AVX/AVX2** (`CPUID.1:ECX[28]`, `7.0:EBX[5]`) | YMM = 2 `V` regs each = all 32 registers with zero scratch `C(06 §9.1)`. Defer to a later phase; do not claim on day 1. |
| **`FRED`** (`CPUID.7.1:EAX[17]`) | This kernel is built `CONFIG_X86_FRED=y` `M`. Claiming it makes Linux abandon the IDT for an entirely different event-delivery mechanism — a second, parallel exception path to implement. |
| **`VMX` / `SVM`** (`CPUID.1:ECX[5]`, `8000_0001:ECX[2]`) | Nested virtualisation. `02 §4.2`: M1/M2 give guests no EL2. Do not chase it. |
| **`SGX`, `TSX`/`RTM`/`HLE`, `MPX`, `CET`, `PKU`/`OSPKE`, `SME`/`SEV`/`TDX`** | Each pulls in a large architectural subsystem with no ARM analogue. TSX in particular would require transactional semantics we cannot provide. |
| **`SMX`, `MONITOR`/`MWAIT` (`CPUID.1:ECX[3]`)** | `MWAIT` invites the guest to idle in a way we must then emulate as a halt; `10 §2` prefers a plain `HLT` path. |
| **`PCOMMIT`/`CLWB`/`PMEM`-adjacent** | persistent-memory semantics we do not model. |
| **`x2APIC` *without* implementing the full MSR block** | Half-claiming is worse than not claiming: Linux commits to the MSR path at init and never falls back. |
| **`UMIP` (`CPUID.7.0:ECX[2]`)** | This kernel is `CONFIG_X86_UMIP=y` `M`; claiming it makes the kernel set `CR4.UMIP`, and we must then fault `SGDT/SIDT/SLDT/STR/SMSW` from CPL 3. Cheap to honour but must not be claimed unless honoured. |
| **`MTRR` (`CPUID.1:EDX[12]`)** | Optional. Either omit (Linux falls back to PAT) or ship a trivial WB-default + one UC hole `C(06 §10.3)`. |
| **`PSE-36`, `PAE` in 32-bit paths** | Only matters if we ever run a 32-bit kernel; irrelevant to Phase 1 and cheap to leave out. |

### 6.3 How to keep the kernel out of paths we cannot implement

Three mechanisms, in order of reliability `I`:

1. **CPUID is the primary gate**, because the kernel's mode decisions are CPUID-driven — the LA57
   case is the proof `S(pgtable_64.c:122-130)`.
2. **The command line is the belt-and-braces gate**, and it is free: we control it entirely (§1.2).
   `no5lvl`, `nosmp`/`maxcpus=1` (until M10), `noxsave`-family, `clocksource=tsc`,
   `tsc=reliable`, `console=ttyS0`, `nokaslr` during bring-up. Cap: 2047 bytes `M`.
3. **`hardware_subarch`** in `boot_params` (offset 0x23c) selects a sub-architecture. We set 0
   (PC-compatible); the Jailhouse/Xen values exist and are *not* what we want.

**`?` UNRESOLVED — the experiment that would make §6.2 empirical rather than argued:** extend
`plugin_isaclass.c` with an execution-time callback (`QEMU_PLUGIN_CB_R_REGS`) that logs
`(EAX, ECX)` at every `CPUID` — 981 of them per boot `M` — producing the exact set of leaves the
kernel interrogates, in order. That converts "here is what we think we must claim" into "here is the
list the kernel actually asks for". Half a day.

---

## 7. Threats to validity

1. **Everything is one kernel (6.18.35), one distro (Alpine/musl), one machine.** glibc uses more
   x87 than musl. A distro with 32-bit binaries would change §4 materially.
2. **The kernel/user split is a `vaddr` bit-63 proxy, not a CPL read.** Early boot and the
   decompressor run at low addresses and are counted "user". Where the split matters (`swapgs`,
   `fsgsbase`, `%gs:`) the counts are unambiguous because those instructions only exist in kernel
   code; where it matters less (`total`) the label is loose. Stated in the plugin source.
3. **`n = 1` per scenario for the workload runs.** The boot scenario was run four times during
   plugin development: total executed instructions spanned 1,137,464,510 – 1,138,810,336
   (**0.12 % spread**) and the x87 count was **exactly 8 every time** `M`. Percentages below
   ~0.001 % should be read as order-of-magnitude.
4. **The `< 1 MiB` legacy bucket undercounts** (§1.6): QEMU's reset vector at `0xFFFF_FFF0` is
   outside it.
5. **We measured QEMU's *guest instruction stream*, which is faithful, not QEMU's own behaviour.**
   But `-cpu max` advertises features (AVX, LA57) that our design will not, so the measured stream
   is the stream of a *more* capable CPU. That is conservative for §4 (a less capable CPUID cannot
   introduce x87) and could be non-conservative for §6 (a kernel that saw fewer features might take
   a *different* path we have not observed). The CPUID-logging experiment in §6.3 closes this.
6. **The 28–48 pm estimate in `08` is not re-derived here.** This document adjusts two line items:
   the loader is ~0 (§1.4), the 16-bit SIPI interpreter is new and unscheduled (§2.2).

## 8. Open items, ranked by value

| # | Question | Experiment | Cost |
|---|---|---|---|
| 1 | Does modern Linux ever clear `CR0.WP`? (§3.4) | plugin with `CB_R_REGS` decoding the value stored to CR0, across boot / module load / ftrace | 1 day |
| 2 | Does ARM force `PXN` on EL0-writable pages? (§3.5) | `probes/g1_mmu4k`-style: `AP=01`, `PXN=0`, branch into it from EL1, read `ESR_EL1` | 0.5 day |
| 3 | Exactly which CPUID leaves does the kernel ask for, in order? (§6.3) | plugin logging `(EAX,ECX)` at each of the 981 `CPUID`s | 0.5 day |
| 4 | Is a SIPI-intercept AP bring-up (§2.2 option 2) stable across kernel versions? | try it against 3 kernel builds before committing to it over the interpreter | 2 days |
| 5 | Exact legacy-mode instruction count by CPU mode, not address (§1.6) | per-insn `CR0.PE`/`EFER.LMA` read | 0.5 day |
| 6 | glibc (not musl) x87 profile | rebuild the initramfs on a glibc distro and re-run `plugin_isaclass` | 1 day |

---

## 9. Reproducing this

```bash
cd probes/qemu_baseline
# work/ is gitignored; if absent:  ./build.sh   (needs zig, curl)

cc -O2 -shared -fPIC -o bin/plugin_isaclass.so plugin_isaclass.c \
   -I/opt/homebrew/include $(pkg-config --cflags glib-2.0) -undefined dynamic_lookup

./isaclass_run.sh boot     none          # boot -> shell -> poweroff
./isaclass_run.sh spawn    spawn         # + 400 fork/exec
./isaclass_run.sh sevenzip sevenzip      # + 7-Zip 26.01
./isaclass_run.sh bench    bench:all     # + the bench suite (this is the one with x87)

./bzimage_hdr.py work/nb-x86_64/boot/vmlinuz-virt

# confirm SeaBIOS really runs under -kernel:
qemu-system-x86_64 ... -debugcon file:seabios.log -global isa-debugcon.iobase=0x402
```

Raw outputs: `probes/qemu_baseline/results/isaclass/{isaclass,run}.{boot,spawn,sevenzip,bench}.txt`,
`bzimage_hdr.txt`, `seabios.log`. Nothing in the existing harness was modified.

## 10. Sources

**Primary source read directly** (fetched 2026-08-08 from
`raw.githubusercontent.com/torvalds/linux/master`):
`arch/x86/boot/compressed/head_64.S` (539 lines; `startup_32`:82, `startup_64`:278, `.org 0x200`:277,
`verify_cpu`:133 — 32-bit path only, `configure_5level_paging`:409) ·
`arch/x86/boot/compressed/pgtable_64.c` (201 lines; the LA57 decision at :122-137) ·
`arch/x86/realmode/rm/trampoline_64.S` (294 lines; `.code16`:38, `trampoline_start`:61) ·
`arch/x86/include/uapi/asm/bootparam.h` (215 lines; `sentinel`:143-155, `XLF_*`:20-27).

**Firecracker** `main`, `src/vmm/src/arch/x86_64/{regs,mod,gdt,layout}.rs`.
**rust-vmm linux-loader** `main`, `src/loader/bzimage/mod.rs`.
**FEX-Emu** `main`, `FEXCore/Source/Interface/Core/OpcodeDispatcher/{X87,X87F64}.cpp`,
`unittests/ASM/X87{,_F64}/`.

**Cited:** [Linux x86 boot protocol](https://www.kernel.org/doc/html/latest/arch/x86/boot.html) ·
[FEX-2302 "Bug fixes to 64-bit x87 emulation"](https://fex-emu.com/FEX-2302/) ·
[FEX-2505](https://fex-emu.com/FEX-2505/) · [FEX releases](https://github.com/FEX-Emu/FEX/releases)

**Corpus:** `04 §x87` (Rosetta measurements), `05 §2.6`, `06 §3/§9/§10.3`, `07 §2.1`, `08 §2/§9`,
`10 §1.5/§5.1`, `14` (`VARange=0`, `XNX=1`, `PAN=2`, `HAFDBS=0`, `ASIDBits=0`), `15` (boot
baselines), `16` (shadow-fill and TLBI costs), `19` (SMP — see §5 note).
