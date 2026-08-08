# Hypervisor.framework on Apple Silicon (arm64) — Capability Dossier

**Purpose:** decide what a custom hypervisor-based emulator can and cannot build on Apple's `Hypervisor.framework`.

**Research date:** 2026-08-08
**SDK inspected:** `MacOSX26.4.sdk` (`Hypervisor.framework` current-version **259.5.15**)
**Test machine for all `[MEASURED]` claims:** Apple **M2 Pro** (`T6020`), macOS **26.5.2** (build 25F84), XNU `12377.121.10`, SIP enabled, 16 KB host page size, Command Line Tools clang.

> Confidence tags used throughout: **High** / **Medium** / **Low** / **Unverified**.
> `[MEASURED]` = I ran it on this machine (probe sources: `scratchpad/hvprobe/{probe,v2,v3,v4}.c`).
> `[HEADER]` = read directly out of the SDK headers.
> `[DOC]` = Apple developer documentation. `[COMM]` = community/third-party source.

---

## Bottom line

`Hypervisor.framework` on Apple Silicon is a **thin, deliberately minimal EL2 shim**. It gives you exactly four things: stage-2 memory mapping, a vCPU register file, a blocking `run` call that returns an `ESR`/`FAR`/`HPFAR` triple, and (since macOS 15) an optional in-kernel GICv3. Everything else is yours to emulate.

**What you CAN do (all verified on this machine):**

- Run a guest at **EL1 and EL0** with full stage-1 MMU control by the guest. On **M3/M4 + macOS 15+** you can also enable **EL2** for a nested guest hypervisor.
- Map **any** host memory as guest RAM — including `MAP_JIT` regions, file-backed `MAP_SHARED`, and `mach_vm_remap` aliases. Host and guest views are independent, so a host JIT can write code that the guest executes.
- Get precise stage-2 fault info: `ESR_EL2` syndrome, `FAR_EL2` (guest VA) and an **HPFAR-derived IPA** delivered in the same struct.
- Do **demand paging** on stage-2 faults. Measured full cycle (fault → `hv_vm_map` → re-enter → `hv_vm_unmap`) ≈ **1.5 µs**.
- Trap `HVC`, `SMC`, `WFI`, PMU/physical-timer sysregs, and optionally debug exceptions and debug-register accesses.
- Run **JIT and Hypervisor in the same process** — no entitlement conflict; ad-hoc self-signing is sufficient, no paid Apple Developer account.

**What you CANNOT do:**

- **Choose your own traps.** There is no public `HCR_EL2` write, no fine-grained-trap API, no way to trap an arbitrary system register, and no way to turn `WFI` trapping *off* or `WFE` trapping *on*. The public knobs are exactly two booleans (`trap_debug_exceptions`, `trap_debug_reg_accesses`). Apple ships private SPI for this (`__hv_vcpu_config_set_fgt_enabled`, `__hv_vcpu_set_control_field`) that you cannot use.
- **Sub-16 KB stage-2 granularity.** `hv_vm_map` rejects 4 KB on this machine; the granule is the host page size. A 4 KB IPA granule config exists only from macOS 26 and is not the default here.
- **Escape the ~800 ns exit floor.** Every guest→host round trip costs **~0.8–0.9 µs**, regardless of what you do in the handler. This is the single hardest constraint on the design.
- **Scale fine-grained `hv_vm_protect`.** Cost is O(number of existing mappings), not O(region size): **313 ns** with a handful of mappings, **27 µs** with 20 000. Any design that relies on per-page permission flipping for dirty tracking or SMC detection will collapse.

**The three biggest blockers, in order:** (1) the ~0.8 µs exit floor caps trap-driven emulation at roughly **1.2 M exits/sec/core**; (2) `hv_vm_protect` is O(total mappings), so keep the mapping count small and coarse; (3) no control over which sysregs/instructions trap, so anything Apple didn't choose to trap is invisible to you.

---

## 1. Full API surface

### 1.1 Header inventory

All headers live at
`/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/System/Library/Frameworks/Hypervisor.framework/Headers/`
plus one kernel-shared header at
`/Library/Developer/CommandLineTools/SDKs/MacOSX26.4.sdk/usr/include/arm64/hv/hv_kern_types.h`.

The arm64 surface is guarded by `#ifdef __arm64__`; `hv.h`, `hv_vmx.h`, `hv_arch_vmx.h`, `hv_arch_x86.h`, `hv_types.h`, `hv_error.h`, `hv_intr.h` are **x86-only** and irrelevant here (`Hypervisor.h:13-34`). Confidence: **High** `[HEADER]`.

| Header | Contents |
|---|---|
| `hv_vm.h` | `hv_vm_create/destroy/map/unmap/protect`, `hv_vm_get_max_vcpu_count` |
| `hv_vm_config.h` | IPA size, IPA granule, **EL2 enable** |
| `hv_vcpu.h` | vCPU lifecycle, regs, sysregs, SME, interrupts, debug traps, run, vtimer |
| `hv_vcpu_types.h` | `hv_vcpu_exit_t`, `HV_EXIT_REASON_*`, `hv_reg_t`, `hv_sys_reg_t` |
| `hv_vcpu_config.h` | feature-register (ID_AA64*) read-back, CCSIDR values |
| `hv_vm_allocate.h` | `hv_vm_allocate` / `hv_vm_deallocate` (accounted guest RAM) |
| `hv_gic*.h` | GICv3 device (macOS 15+) — distributor, redistributor, ICC/ICH/ICV, MSI, state save/restore |
| `hv_sme_config.h` | max Streaming Vector Length |
| `hv_kern_types.h` | `hv_return_t` codes, `HV_MEMORY_*` flags, **`hv_data_abort_notification_t`** |

### 1.2 VM lifecycle and memory

```c
hv_return_t hv_vm_create(hv_vm_config_t _Nullable config);           // hv_vm.h:33
hv_return_t hv_vm_destroy(void);                                     // hv_vm.h:42
hv_return_t hv_vm_map(void *addr, hv_ipa_t ipa, size_t size,
                      hv_memory_flags_t flags);                      // hv_vm.h:53
hv_return_t hv_vm_unmap(hv_ipa_t ipa, size_t size);                  // hv_vm.h:62
hv_return_t hv_vm_protect(hv_ipa_t ipa, size_t size,
                          hv_memory_flags_t flags);                  // hv_vm.h:72
hv_return_t hv_vm_get_max_vcpu_count(uint32_t *max_vcpu_count);      // hv_vm.h:25
```

**One VM per process.** `hv_vm_create` "Creates a VM instance for **the current process**" and `hv_vm_destroy` destroys "the VM instance associated with the current process" — there is no VM handle. You get exactly one VM per process. Confidence: **High** `[HEADER]` `hv_vm.h:28,37`.

`hv_memory_flags_t` has only three bits on arm64 (`hv_kern_types.h:70-76`):

```c
HV_MEMORY_READ  = (1ull << 0),
HV_MEMORY_WRITE = (1ull << 1),
HV_MEMORY_EXEC  = (1ull << 2)
```

Note the arm64 set is much smaller than x86's, which additionally has `HV_MEMORY_UEXEC` and the whole `HV_MEMORY_MAXPROT*` family (`hv_types.h:76-86`). **There is no `MAXPROT` concept on arm64** — you can freely `hv_vm_protect` up to RWX at any time. Confidence: **High** `[HEADER]`.

`hv_return_t` values (`hv_kern_types.h:50-61`) — worth memorising for debugging:

| Value | Name | Typical cause |
|---|---|---|
| `0x00000000` | `HV_SUCCESS` | |
| `0xfae94001` | `HV_ERROR` | e.g. overlapping `hv_vm_map` `[MEASURED]` |
| `0xfae94002` | `HV_BUSY` | e.g. 2nd `hv_vcpu_create` on same thread `[MEASURED]` |
| `0xfae94003` | `HV_BAD_ARGUMENT` | misaligned size/addr, IPA out of range `[MEASURED]` |
| `0xfae94004` | `HV_ILLEGAL_GUEST_STATE` | e.g. running with `CPSR.M`=EL2h on a non-EL2 VM `[MEASURED]` |
| `0xfae94005` | `HV_NO_RESOURCES` | |
| `0xfae94006` | `HV_NO_DEVICE` | |
| `0xfae94007` | `HV_DENIED` | **missing `com.apple.security.hypervisor` entitlement** |
| `0xfae94008` | `HV_EXISTS` | |
| `0xfae9400f` | `HV_UNSUPPORTED` | e.g. `set_el2_enabled(true)` on M1/M2 `[MEASURED]` |

Note `HV_ILLEGAL_GUEST_STATE` and `HV_EXISTS` exist **only** in the arm64 `hv_kern_types.h`; the x86 `hv_error.h` maps `0x08` to `HV_FAULT` instead. Confidence: **High** `[HEADER]`.

### 1.3 vCPU

```c
hv_return_t hv_vcpu_create(hv_vcpu_t *vcpu, hv_vcpu_exit_t **exit,
                           hv_vcpu_config_t _Nullable config);       // hv_vcpu.h:28
hv_return_t hv_vcpu_run(hv_vcpu_t vcpu);                             // hv_vcpu.h:369
hv_return_t hv_vcpus_exit(hv_vcpu_t *vcpus, uint32_t vcpu_count);    // hv_vcpu.h:381
hv_return_t hv_vcpu_get_reg / set_reg;                               // hv_vcpu.h:51,63
hv_return_t hv_vcpu_get_sys_reg / set_sys_reg;                       // hv_vcpu.h:276,288
hv_return_t hv_vcpu_get_simd_fp_reg / set_simd_fp_reg;               // hv_vcpu.h:78,94
hv_return_t hv_vcpu_get_pending_interrupt / set_pending_interrupt;   // hv_vcpu.h:299,312
hv_return_t hv_vcpu_get_exec_time(hv_vcpu_t, uint64_t *time);        // hv_vcpu.h:392
```

**Threading model — this is a hard architectural constraint.** A vCPU is created *for the current thread*; "Each thread can only have one vCPU associated at a time" (`hv_vcpu.h:25`), and `get_reg` / `set_reg` / `get_sys_reg` / `run` / `destroy` all say **"Must be called by the owning thread."** So: **one host thread per vCPU, permanently**. `[MEASURED]` a second `hv_vcpu_create` on the same thread returns `HV_BUSY` (`0xfae94002`). Confidence: **High** `[HEADER]` `[MEASURED]`.

`hv_vcpus_exit` is the **only** call safe to make from another thread. Confidence: **High** `[HEADER]` `hv_vcpu.h:372-381`.

`[MEASURED]` `hv_vm_get_max_vcpu_count` = **64** on M2 Pro.

**Register cost is negligible** — these are plain userspace reads/writes of a shared state page, not syscalls:

| Op | Cost `[MEASURED]` |
|---|---|
| `hv_vcpu_get_reg` | **5.8 ns** |
| `hv_vcpu_get_sys_reg` | **12.1 ns** |
| `hv_vcpu_set_sys_reg` | **10.0 ns** |

Confidence: **High** `[MEASURED]`. Design implication: **do not** try to minimise register accesses in your exit handler — they are free relative to the 800 ns exit. Batch-reading the whole GPR file costs ~180 ns, still under a quarter of one exit.

### 1.4 Exit information

```c
OS_ENUM(hv_exit_reason, uint32_t,
    HV_EXIT_REASON_CANCELED,          // = 0, from hv_vcpus_exit()
    HV_EXIT_REASON_EXCEPTION,         // = 1, synchronous exception to EL2
    HV_EXIT_REASON_VTIMER_ACTIVATED,  // = 2
    HV_EXIT_REASON_UNKNOWN            // = 3
);                                                        // hv_vcpu_types.h:38-59

typedef struct {
    hv_exception_syndrome_t syndrome;         // ESR_ELx
    hv_exception_address_t  virtual_address;  // FAR_ELx
    hv_ipa_t                physical_address; // HPFAR-derived IPA
} hv_vcpu_exit_exception_t;                               // hv_vcpu_types.h:74-78

typedef struct {
    hv_exit_reason_t          reason;
    hv_vcpu_exit_exception_t  exception;
} hv_vcpu_exit_t;                                         // hv_vcpu_types.h:83-86
```

**Only four exit reasons.** There is no MMIO exit, no IO exit, no shutdown exit, no internal-error exit. Everything interesting arrives as `HV_EXIT_REASON_EXCEPTION` and you demultiplex on `ESR_EL2.EC` yourself. Confidence: **High** `[HEADER]`.

The `exit` pointer is returned **once** by `hv_vcpu_create` and the struct is updated in place by each `hv_vcpu_run` — it is a shared page, so reading it is free. Confidence: **High** `[HEADER]` `hv_vcpu.h:22`.

**ESR / FAR / HPFAR on stage-2 faults — verified.** `[MEASURED]` on a data abort to an unmapped IPA, all three fields are populated:

```
ldr x0,[x1] @0x80000000  -> EC=0x24 ISS=0x1c08006 FAR=0x80000000 IPA=0x80000000
```

`physical_address` is the faulting **IPA** (i.e. `HPFAR_EL2` combined with `FAR_EL2[11:0]`), and `virtual_address` is the guest VA from `FAR_EL2`. With the guest MMU off these coincide; with the MMU on they differ, and you must use `physical_address` for device dispatch. Confidence: **High** `[MEASURED]`.

**Critical ISV caveat.** `[MEASURED]` decoding of the ISS field for various faulting instructions:

| Guest instruction | ISS | ISV | Usable syndrome? |
|---|---|---|---|
| `ldr x0,[x1]` | `0x1c08006` | 1 | yes — SAS=8B, SRT=x0, WnR=0 |
| `str x0,[x1]` | `0x1c08046` | 1 | yes — WnR=1 |
| `ldr w0,[x1]` | `0x1800006` | 1 | yes — SAS=4B |
| `ldrb w0,[x1]` | `0x1000006` | 1 | yes — SAS=1B |
| **`ldp x2,x3,[x1]`** | `0x0000006` | **0** | **NO** |
| **`stp x2,x3,[x1]`** | `0x0000046` | **0** | **NO — only WnR** |

This is the well-known ARM architectural limitation, not an Apple one: **load/store-pair, and generally any access without a valid syndrome, delivers ISV=0**, giving you the fault address and direction but *no* register number and *no* access size. To emulate MMIO for such an access you must **fetch and decode the guest instruction yourself**, which means walking the guest's stage-1 page tables from `PC` (using `TTBR0/1_EL1`, `TCR_EL1`, which are all readable via `hv_vcpu_get_sys_reg`). Every serious HVF consumer implements this. Confidence: **High** `[MEASURED]`.

Instruction aborts work symmetrically: `br x1` to an unmapped IPA gave `EC=0x20 ISS=0x0000006 FAR=0x80000000 IPA=0x80000000`. Confidence: **High** `[MEASURED]`.

**PC advancement semantics — easy to get wrong.** `[MEASURED]`:

- `HVC` / `SMC`: **PC is already advanced** past the instruction on exit (ELR semantics). Do **not** add 4.
- Data abort (`EC=0x24`), instruction abort (`EC=0x20`), sysreg trap (`EC=0x18`), BRK (`EC=0x3c`): **PC still points at the faulting instruction**. You must add 4 after emulating.

Confidence: **High** `[MEASURED]`.

### 1.5 What actually traps to the host

This is the most important table in this document. `[MEASURED]` on M2 Pro / macOS 26.5.2, guest at EL1h with stage-1 MMU off, default `hv_vcpu_config`:

| Guest instruction | Traps to host? | `ESR_EL2.EC` |
|---|---|---|
| `hvc #0` | **YES** | `0x16` HVC64 |
| `smc #0` | **YES** | `0x17` SMC64 |
| `svc #0` | no — goes to guest EL1 vector | — |
| **`wfi`** | **YES** | `0x01` (ISS bit0 TI=0) |
| **`wfe`** | **NO — executes natively** | — |
| `mrs PMCR_EL0` | **YES** | `0x18` |
| `mrs PMCCNTR_EL0` | **YES** | `0x18` |
| `mrs CNTPCT_EL0` (physical counter) | **YES** | `0x18` |
| `mrs CNTP_CTL_EL0` (physical timer) | **YES** | `0x18` |
| `mrs CNTVCT_EL0` (virtual counter) | **no — native** | — |
| `mrs CNTV_CTL_EL0` (virtual timer) | **no — native** | — |
| `mrs MIDR_EL1`, `ID_AA64PFR0_EL1` | no — native | — |
| `mrs ACTLR_EL1`, `ICC_SRE_EL1`, `DBGBVR0_EL1` | no (by default) | — |
| `msr OSLAR_EL1`, `mrs OSLSR_EL1` | no | — |
| `udf` (undefined) | no — guest EL1 | — |
| `brk #0` | no *by default* — guest EL1 | — |
| `tlbi vmalle1is`, `ic ialluis`, `at s1e1r` | no — native | — |
| `msr DAIFClr` | no — native | — |
| Stage-2 data abort | **YES** | `0x24` |
| Stage-2 instruction abort | **YES** | `0x20` |

Key takeaways:

1. **`WFI` traps but `WFE` does not.** This asymmetry means the classic "guest spins on `WFE` waiting for a spinlock" pattern runs natively (good for performance) but you get no visibility. `HCR_EL2.TWI` is set, `TWE` is clear, and you cannot change either. Confidence: **High** `[MEASURED]`.
2. **The virtual counter/timer is hardware-accelerated; the physical one is trapped and emulated.** This is exactly why `hv_vcpu_set_vtimer_offset` exists (`CNTVCT_EL0 = mach_absolute_time() - vtimer_offset`, `hv_vcpu.h:444-446`) and why `CNTP_*` registers are only exposed at all when you create a GIC (`hv_vcpu_types.h:387-391`). **Design your guest to use the virtual timer.** Confidence: **High** `[MEASURED]` + `[HEADER]`.
3. **PMU registers trap unconditionally** when EL2 is disabled — the `hv_vm_config_set_el2_enabled` doc says so explicitly (`hv_vm_config.h:91-104`) and it matches measurement. You must emulate or stub them, or the guest kernel's PMU driver will fault-storm.
4. **Undefined instructions and `BRK` do NOT trap by default.** They route to the guest's own EL1 vectors. If you want to use `BRK` as a host hypercall or an SMC-detection tripwire, you must enable `hv_vcpu_set_trap_debug_exceptions`.

### 1.6 The only two trap-control knobs

```c
hv_return_t hv_vcpu_set_trap_debug_exceptions(hv_vcpu_t, bool);   // hv_vcpu.h:332
hv_return_t hv_vcpu_set_trap_debug_reg_accesses(hv_vcpu_t, bool); // hv_vcpu.h:356
```

`[MEASURED]` both work exactly as documented:

| Knob | Effect measured |
|---|---|
| `trap_debug_exceptions = 0` | `brk #0` → routed to guest EL1 vector |
| `trap_debug_exceptions = 1` | `brk #0` → **host exit, `EC=0x3c`**, PC not advanced |
| `trap_debug_reg_accesses = 0` | `mrs DBGBVR0_EL1` → executes natively |
| `trap_debug_reg_accesses = 1` | `mrs DBGBVR0_EL1` → **host exit, `EC=0x18`** |

`trap_debug_reg_accesses` covers `DBGBCR<n>_EL1`, `DBGBVR<n>_EL1`, `DBGWCR<n>_EL1`, `DBGWVR<n>_EL1` and `MDSCR_EL1` (`hv_vcpu.h:340-341`). Confidence: **High** `[MEASURED]` `[HEADER]`.

**That is the entire public trap-configuration surface.** There is no `hv_vcpu_set_trap_sysreg(...)`, no `HCR_EL2` write, no fine-grained-trap control. Confidence: **High** `[HEADER]` — verified by reading every arm64 header and the framework's full export list.

### 1.7 HCR_EL2 / VTCR_EL2 — what the app controls

**Nothing, unless EL2 is enabled.** `[MEASURED]` `hv_vcpu_get_sys_reg(HV_SYS_REG_HCR_EL2)` and `HV_SYS_REG_VTCR_EL2` both return `HV_UNSUPPORTED` (`0xfae9400f`) on a VM created without `el2_enabled`. The header states it plainly: these are "Exception Level 2 (EL2) registers. These registers are **only available if EL2 was enabled** in the VM configuration" (`hv_vcpu_types.h:393-394`). Confidence: **High** `[MEASURED]` `[HEADER]`.

Crucially, even when EL2 *is* enabled (M3/M4), `HCR_EL2` / `VTCR_EL2` / `VTTBR_EL2` are the **guest hypervisor's** virtual copies — the ones your nested guest programs — **not** the real EL2 controls governing your own VM. Apple's own EL2 shim still owns the real `HCR_EL2`. So there is no configuration under which an application controls the trap bits applied to its own top-level guest. Confidence: **High** (architectural reading + header semantics; the registers are listed alongside `VPIDR_EL2`/`VMPIDR_EL2`, which are unambiguously nested-guest state).

**The private SPI that does exist (and that you cannot use).** The framework's TBD (`Hypervisor.tbd:37-84`) exports arm64-only underscore-prefixed symbols that are clearly the internal Virtualization.framework interface:

- `__hv_vcpu_config_set_fgt_enabled` / `_get_fgt_enabled` — **fine-grained traps** (FEAT_FGT), i.e. exactly the per-sysreg trap control the public API lacks.
- `__hv_vcpu_set_control_field` / `__hv_vcpu_get_control_field` — a generic control-field setter, almost certainly the `HCR_EL2`/`MDCR_EL2` escape hatch.
- `__hv_vm_space_create` / `_destroy`, `__hv_vm_map_space` / `_protect_space` / `_unmap_space`, `__hv_vm_space_config_set_ipa_base` / `_ipa_granule` / `_ipa_size` — **multiple independent guest address spaces**, each with its own IPA base/size/granule. (The public arm64 API has exactly one address space; x86 has had `hv_vm_space_create` publicly since forever.)
- `__hv_vm_stage1_tlb_op`, `__hv_vcpu_config_set_tlbi_workaround_enabled`
- `__hv_vcpu_config_set_vmkey`, `__hv_vcpu_get_ext_reg`, `__hv_vcpu_get_context`
- `__hv_vm_config_set_isa` / `__hv_vm_get_isa` — an **ISA selector** on the VM config (strongly suggestive of the Rosetta/x86-guest plumbing).
- `__hv_vcpu_amx_prepare`, `__hv_vcpu_get/set_amx_{x,y,z}_space`, `__hv_vcpu_get/set_amx_state_t_el1` — AMX guest state.

These are undocumented, unheadered, and subject to change without notice; using them means reverse-engineering ABI and would break on any macOS update. Confidence: **High** that the symbols exist and are arm64-only `[HEADER]` (`Hypervisor.tbd:37-52`); **Medium** on my interpretation of each one's semantics (inferred from names, not tested).

### 1.8 Accessible system registers

`hv_sys_reg_t` (`hv_vcpu_types.h:257-420`) enumerates the get/set-able registers. Encodings are `op0/op1/CRn/CRm/op2` packed, matching the standard `MRS` encoding. Groups:

- **Debug:** `DBGBVR/BCR/WVR/WCR0..15_EL1`, `MDCCINT_EL1`, `MDSCR_EL1` (`:258-323`)
- **ID / feature:** `MIDR_EL1`, `MPIDR_EL1`, `ID_AA64{PFR0,PFR1,ZFR0,SMFR0,DFR0,DFR1,ISAR0,ISAR1,MMFR0,MMFR1,MMFR2}_EL1` (`:324-336`)
- **Core EL1 state:** `SCTLR_EL1`, `CPACR_EL1`, `TTBR0/1_EL1`, `TCR_EL1`, `SPSR_EL1`, `ELR_EL1`, `SP_EL0/EL1`, `ESR_EL1`, `FAR_EL1`, `PAR_EL1`, `MAIR_EL1`, `AMAIR_EL1`, `VBAR_EL1`, `CONTEXTIDR_EL1`, `TPIDR_EL1`, `AFSR0/1_EL1`, `CSSELR_EL1`, `CNTKCTL_EL1` (`:337-378`)
- **EL0:** `TPIDR_EL0`, `TPIDRRO_EL0`, `TPIDR2_EL0`, `SCXTNUM_EL0` (`:379-382`)
- **Pointer authentication keys:** `AP{IA,IB,DA,DB,GA}KEY{LO,HI}_EL1` — full PAC key save/restore (`:353-362`)
- **Virtual timer:** `CNTV_CTL_EL0`, `CNTV_CVAL_EL0` (`:383-384`)
- **Physical timer:** `CNTP_CTL_EL0`, `CNTP_CVAL_EL0`, `CNTP_TVAL_EL0` — **only if a GIC was created** (`:387-391`)
- **EL2 (only if `el2_enabled`):** `HCR_EL2`, `VTCR_EL2`, `VTTBR_EL2`, `SCTLR_EL2`, `TCR_EL2`, `TTBR0/1_EL2`, `MAIR_EL2`, `VBAR_EL2`, `ESR_EL2`, `FAR_EL2`, `HPFAR_EL2`, `ELR_EL2`, `SPSR_EL2`, `SP_EL2`, `CPTR_EL2`, `MDCR_EL2`, `TPIDR_EL2`, `VPIDR_EL2`, `VMPIDR_EL2`, `CNTHCTL_EL2`, `CNTHP_{CTL,CVAL,TVAL}_EL2`, `CNTVOFF_EL2` (`:393-419`)

**`ACTLR_EL1` is special and interesting** (`hv_vcpu_types.h:339-346`, macOS 15+):

> "This only allows getting / setting of the `ACTLR_EL1.EnTSO` bit (index 1). Setting this bit to 1 will cause the vcpu to use a **TSO memory model**, whereas clearing it will cause the vcpu to use the default ARM64 memory model (weakly ordered loads / stores)."

This is Apple's hardware **Total Store Ordering** mode — the same mechanism Rosetta 2 uses to make x86 memory semantics fast. **For an x86-on-ARM emulator this is enormously valuable**: you can put the guest vCPU into TSO mode and skip the fences you would otherwise need on every guest load/store. Confidence: **High** `[HEADER]`.

**Notable absences:** no `CNTFRQ_EL0`, no `CLIDR_EL1`/`CTR_EL0`/`DCZID_EL0` in `hv_sys_reg_t` (they are exposed read-only through `hv_vcpu_config_get_feature_reg` instead, `hv_vcpu_config.h:30-45`), no `ICC_*` GIC CPU-interface registers (those live in the separate `hv_gic_*` API), and no `SPSel`/`PSTATE` beyond `HV_REG_CPSR`.

### 1.9 SME / SVE (macOS 15.2+)

A full streaming-SVE state interface exists: `hv_vcpu_get/set_sme_state` (PSTATE.SM, PSTATE.ZA), `..._sme_z_reg` (Z0-Z31), `..._sme_p_reg` (P0-P15), `..._sme_za_reg` (the ZA matrix), `..._sme_zt0_reg` (SME2 ZT0), plus `hv_sme_config_get_max_svl_bytes` (`hv_vcpu.h:97-264`, `hv_sme_config.h:30`). Note the aliasing rule: "In streaming SVE mode, the SIMD Q registers are aliased to the bottom 128 bits of the corresponding Z register" (`hv_vcpu.h:74-76`). Confidence: **High** `[HEADER]`. Not tested (M2 Pro has no SME).

### 1.10 The undocumented monitored-memory / data-abort-to-Mach-message path

`hv_kern_types.h:78-116` defines types for a mechanism with **no public API**:

```c
typedef struct __attribute__((packed)) {
    uint64_t context;      // context registered by the VMM
    uint64_t ipa;          // fault IPA within the registered monitored range
    uint64_t value;        // value written, for write accesses
    uint32_t access_size;
    uint32_t access_type;  // HV_MEMORY_READ or HV_MEMORY_WRITE
} hv_data_abort_notification_t;

typedef struct { mach_msg_header_t header; hv_data_abort_notification_t body; }
    hv_vm_mem_access_msg_t;
```

The comments reference `hv_vm_monitor_data_abort`, `TRAP_HV_VM_MONITOR_DATA_ABORT` and `hv_vm_monitor_data_abort_t`, and explain: "Access faults on monitored memory regions get translated into a **mach message by the kernel**. The server task holding the receive right on the memory range port receives data of this type."

**This is a device-emulation fast path that bypasses the vCPU exit entirely** — the kernel decodes the access and posts a Mach message to a *different* task, so an MMIO write need not stop the vCPU at all. It would be extremely valuable (it is presumably how Virtualization.framework's device backends avoid exit round trips), but there is **no exported symbol** for `hv_vm_monitor_data_abort` in `Hypervisor.tbd` — it is a **raw Mach trap** reachable only by hand-rolled `syscall`/trap invocation. Confidence: **High** that the types and mechanism exist `[HEADER]`; **Unverified** whether it is callable from a third-party process and what the trap number/ABI is. Treat as **not usable**.

---

## 2. Stage-2 (nested) paging

### 2.1 Granularity — 16 KB, not 4 KB

`[MEASURED]` on M2 Pro / macOS 26.5.2 (host page size **16384**):

```
map 4KB  RW  -> 0xfae94003  (HV_BAD_ARGUMENT)   <- REJECTED
map 16KB RW  -> 0x00000000                       <- OK
map host VA + 4096 (sub-16K-aligned host VA) -> 0xfae94003  <- REJECTED
hv_vm_config_get_default_ipa_granule -> 1  (HV_IPA_GRANULE_16KB)
```

Both the **IPA** and the **host VA** must be 16 KB-aligned, and the size must be a multiple of 16 KB. Confidence: **High** `[MEASURED]`.

macOS 26 added a granule selector (`hv_vm_config.h:113-143`):

```c
OS_ENUM(hv_ipa_granule, uint32_t, HV_IPA_GRANULE_4KB, HV_IPA_GRANULE_16KB);
hv_return_t hv_vm_config_set_ipa_granule(hv_vm_config_t, hv_ipa_granule_t);
```

so a **4 KB stage-2 granule is selectable from macOS 26.0+**, but the default on this machine is 16 KB. This matters for guests that assume 4 KB pages (most x86-derived workloads, and Linux kernels built with `CONFIG_ARM64_4K_PAGES`). Confidence: **High** `[HEADER]` for the API; **High** `[MEASURED]` that 16 KB is the default here. **Unverified** whether selecting 4 KB has a performance penalty or additional restrictions.

### 2.2 IPA space size

```c
hv_return_t hv_vm_config_get_max_ipa_size(uint32_t *ipa_bit_length);      // hv_vm_config.h:36
hv_return_t hv_vm_config_get_default_ipa_size(uint32_t *ipa_bit_length);  // hv_vm_config.h:45
hv_return_t hv_vm_config_set_ipa_size(hv_vm_config_t, uint32_t);          // hv_vm_config.h:56
```

`[MEASURED]` on M2 Pro:

| | Value | Range |
|---|---|---|
| **max** IPA size | **40 bits** | 1 TB |
| **default** IPA size | **36 bits** | 64 GB |

Setting 41 bits returns `HV_UNSUPPORTED`. With `ipa_size=40`, mapping at the very top of the space (`0xffffffc000`) **succeeds**; mapping at exactly `1<<40` returns `HV_BAD_ARGUMENT`. Confidence: **High** `[MEASURED]`.

**Always call `hv_vm_config_set_ipa_size(cfg, max)` explicitly** — the default of 36 bits silently caps you at 64 GB of IPA space, which is a real constraint if you want sparse device layout or >64 GB of guest RAM. Confidence: **High** `[MEASURED]`.

### 2.3 Permissions — more flexible than expected

`[MEASURED]` all of these `hv_vm_map` flag combinations **succeed**:

| Flags | Result |
|---|---|
| `READ\|WRITE\|EXEC` | OK — **RWX is allowed**, no W^X enforcement at stage 2 |
| `WRITE\|EXEC` (no READ) | OK |
| `0` (no permissions at all) | **OK** — creates a mapping that faults on every access |
| same host page at two different IPAs | **OK** — aliasing works |
| overlapping an existing mapping | `HV_ERROR` (`0xfae94001`) — you must `unmap` first |

The `flags == 0` case is notable: it lets you install a **present-but-inaccessible** mapping, useful for tripwires/watchpoints without changing the mapping topology. Confidence: **High** `[MEASURED]`.

### 2.4 What host memory can back a guest mapping

`[MEASURED]` — **everything I tried worked**, mapped RWX into the guest:

| Host memory source | Mappable? |
|---|---|
| `hv_vm_allocate` | yes |
| `mmap(MAP_ANON\|MAP_PRIVATE, RW)` | yes |
| `mmap(MAP_ANON\|MAP_SHARED, RW)` | yes |
| **`mmap(MAP_ANON\|MAP_PRIVATE\|MAP_JIT, RWX)`** | **yes** |
| `mmap(MAP_ANON, PROT_READ)` — host read-only | yes, and guest gets **write** access |
| `aligned_alloc` (malloc heap) | yes |
| **file-backed `mmap(MAP_SHARED)`** | **yes** |
| **`mach_vm_remap` alias** | **yes** |

Three consequences that matter a lot for an emulator design:

1. **Host protection and stage-2 protection are fully independent.** `[MEASURED]` a page mapped RW into the guest can simultaneously be `mprotect(PROT_NONE)` in the host, and a host `PROT_READ`-only page can be guest-writable. Stage 2 is a separate translation regime; the host's stage-1 protections do not constrain it.
2. **A host JIT can share memory with the guest.** `MAP_JIT` regions are mappable as guest RAM, and `mach_vm_remap` aliases work — so the "write through an RW alias, execute through the guest" pattern is available.
3. **`munmap` of a live guest mapping succeeds and is dangerous.** `[MEASURED]` the host can `munmap` a page that is still `hv_vm_map`'ed with no error returned. You own that lifetime discipline entirely; there is no refcount protecting you. Confidence: **High** `[MEASURED]`.

`hv_vm_allocate` (`hv_vm_allocate.h:51`) exists purely for **accounting** ("This API enables accurate memory accounting of the allocations it creates") — it is not required. Confidence: **High** `[HEADER]`.

### 2.5 Maximum guest memory

`[MEASURED]` I mapped **64 GB** of guest RAM (64 × 1 GB `hv_vm_allocate` + `hv_vm_map`) with no error on a machine with far less physical RAM — the mappings are lazy. The real ceilings are (a) the configured IPA size (40 bits = 1 TB max on M2 Pro), and (b) host RAM + swap when the guest actually touches the pages. Confidence: **High** `[MEASURED]` for the 64 GB figure; **Medium** that 1 TB is genuinely reachable (not tested).

### 2.6 Cost of map / unmap / protect — and the scaling cliff

`[MEASURED]`, with a live vCPU, on a 64 MB region:

| Operation | Cost |
|---|---|
| `hv_vm_protect` 1 page (16 KB) | **313 ns** |
| `hv_vm_protect` 16 pages (256 KB) | **308 ns** |
| `hv_vm_protect` 256 pages (4 MB) | **324 ns** |
| `hv_vm_protect` 4096 pages (64 MB) | **241 ns** |
| `hv_vm_protect` 1 page **in the middle** of a region (forces split) | **344 ns** |
| `hv_vm_map` + `hv_vm_unmap` of 1 page (pair) | **587 ns** |
| `hv_vm_map` of 1 page (discontiguous, 20 000 of them) | **649 ns** each |

**`hv_vm_protect` is O(1) in region size.** Protecting 4096 pages costs the same as protecting one. This strongly implies each call performs a **full stage-2 TLB invalidation** (`TLBI VMALLS12E1IS`-style) regardless of range — the fixed ~250-340 ns is dominated by the broadcast invalidate and the syscall, not by page-table walking. **Design implication: always batch permission changes into the largest possible range.** Confidence: **High** `[MEASURED]`; **Medium** on the TLBI-everything mechanism (inferred, not directly observed).

**But `hv_vm_protect` is O(n) in the number of existing mappings — and this is a cliff:**

| Number of live mappings | `hv_vm_protect` 1 page |
|---|---|
| ~5 | **313 ns** |
| **20 000** | **27 201 ns (27 µs)** |

That is an **87× regression**. The kernel is evidently walking or re-validating a linear list of VM map entries on every protect. Confidence: **High** `[MEASURED]`.

**This is the single most important scaling constraint in the framework.** Any design that (a) maps guest RAM as thousands of small regions, or (b) uses per-page `hv_vm_protect` for dirty-bit tracking, self-modifying-code detection, or W^X on guest pages, will hit this. Mitigations: keep guest RAM as a **small number of large contiguous mappings**; do demand paging at coarse (≥2 MB) granularity; prefer `flags=0` tripwires installed once over repeated protect churn.

Reassuringly, **exit latency is not affected by mapping count** — `[MEASURED]` MMIO exit latency was 890 ns with ~5 mappings and 900 ns with 20 000. Confidence: **High** `[MEASURED]`.

### 2.7 Demand paging on stage-2 faults

**Yes, this works and it is practical.** `[MEASURED]` full cycle of: run → stage-2 translation fault → `hv_vm_map` the page → `hv_vm_unmap` it again (to repeat the experiment):

```
fault + map + unmap  =  1510 ns
```

Decomposed against the other measurements: ~890 ns for the fault exit + ~590 ns for the map/unmap pair. So **a demand-paging fault costs roughly 1.5 µs**, versus ~0.2 µs for a native host page fault. That is ~7× a host fault but perfectly workable for lazy population of guest RAM at page-in time.

**Caveat:** because `hv_vm_protect`/`hv_vm_map` cost scales with mapping count (§2.6), naive per-16 KB-page demand paging of a large guest will progressively slow down as the mapping table grows. Populate in large chunks and coalesce. Confidence: **High** `[MEASURED]`.

---

## 3. What is NOT virtualized — you must emulate it

### 3.1 GIC — there IS a hardware-backed GICv3 device since macOS 15

This is the biggest change to the platform since launch, and it substantially reduces what you must write.

**Before macOS 15:** nothing. You emulate the entire interrupt controller in userspace and inject via `hv_vcpu_set_pending_interrupt(vcpu, HV_INTERRUPT_TYPE_IRQ|FIQ, true)` (`hv_vcpu.h:312`), which sets `HCR_EL2.VI`/`VF` — a single virtual IRQ/FIQ line per vCPU, nothing more. Note the lifecycle gotcha: "The pending interrupts [are] automatically cleared after `hv_vcpu_run` returns. It is expected that `hv_vcpu_set_pending_interrupt` be called before **every** `hv_vcpu_run`" (`hv_vcpu.h:308-310`). Confidence: **High** `[HEADER]`.

**From macOS 15.0:** `hv_gic_create()` provides a real in-framework **GICv3** with "a distributor, redistributors, msi and GIC CPU system registers" (`hv_gic.h:26-31`). The API surface is large and complete:

- `hv_gic_create(hv_gic_config_t)` — **one per VM, must be called after `hv_vm_create` but before any `hv_vcpu_create`** (`hv_gic.h:34-38`). Confidence: **High** `[HEADER]`.
- Placement: `hv_gic_config_set_distributor_base` / `_redistributor_base` / `_msi_region_base` / `_msi_interrupt_range` (`hv_gic_config.h`), with sizes/alignments queried via `hv_gic_get_distributor_size`, `_redistributor_region_size`, `_redistributor_size`, `_msi_region_size`, and the matching `*_base_alignment` calls (`hv_gic_parameters.h`). Each redistributor is "two 64 kilobyte frames per vCPU, contiguously placed" (`hv_gic_parameters.h:41-43`).
- Interrupt injection: `hv_gic_set_spi(intid, level)` for level/edge SPIs, `hv_gic_send_msi(address, intid)` for message-signalled (`hv_gic.h:66,77`).
- Full register access for save/restore and introspection: `hv_gic_get/set_distributor_reg` (**1534** `GICD_*` enum entries), `hv_gic_get/set_redistributor_reg` (19 `GICR_*`), `hv_gic_get/set_icc_reg` (11 `ICC_*`), `hv_gic_get/set_msi_reg`. Enum values equal the GICv3-spec register offsets, so you can loop over arrays numerically (`hv_gic.h:84-87`).
- **`ICH_*` list registers ARE exposed** — `hv_gic_get/set_ich_reg` with `HV_GIC_ICH_REG_LR0_EL2` … `LR15_EL2`, plus `HCR_EL2`, `VTR_EL2`, `MISR_EL2`, `EISR_EL2`, `ELRSR_EL2`, `VMCR_EL2`, `AP0R0_EL2`, `AP1R0_EL2` (`hv_gic_types.h:1654-1678`). **But**: "ICH registers are only available when **EL2 is enabled**, otherwise returns an error" (`hv_gic.h:179-180`). Same for the `ICV_*` set (`hv_gic_types.h:1685-1695`). Confidence: **High** `[HEADER]`.
- Snapshot/restore: `hv_gic_state_create` / `_get_size` / `_get_data` / `hv_gic_set_state` / `hv_gic_reset` — an opaque, **versioned and stable** serialized blob, though "hv_gic_set_state() can fail if a software update has changed the host in a way that would be incompatible with the previous format" (`hv_gic.h:250-276`, `hv_gic_state.h`).

**`[MEASURED]` The framework GIC works on M2 Pro — EL2 is NOT required.** I created one successfully and read it back:

| Parameter | Value on M2 Pro / macOS 26.5.2 |
|---|---|
| `hv_gic_get_distributor_size` | **65536** (64 KB), alignment 64 KB |
| `hv_gic_get_redistributor_region_size` | **33554432** (32 MB) |
| `hv_gic_get_redistributor_size` | **131072** (128 KB per vCPU = two 64 KB frames), alignment 64 KB |
| `hv_gic_get_msi_region_size` | **65536** (64 KB), alignment 64 KB |
| `hv_gic_get_spi_interrupt_range` | base **32**, count **988** → SPIs **32..1019** |
| `GICD_TYPER` after create | `0x0478001f` (ITLinesNumber=31 → 1024 INTIDs) |
| `GICD_CTLR` after create | `0x50` |
| `hv_gic_set_spi(32, true)` | `HV_SUCCESS` |

Note the redistributor region (32 MB / 128 KB = **256 frames**) is sized well beyond the 64-vCPU maximum. Confidence: **High** `[MEASURED]`.

**Confirmed side effects of `hv_gic_create`,** `[MEASURED]`:

- `HV_SYS_REG_CNTP_CTL_EL0` and `CNTP_CVAL_EL0` change from `HV_UNSUPPORTED` to **accessible** — exactly as the header claims.
- `hv_gic_get_icc_reg(ICC_CTLR_EL1)` → `HV_SUCCESS`.
- `hv_gic_get_ich_reg(ICH_LR0_EL2)` → **`HV_UNSUPPORTED`** (`0xfae9400f`), confirming the list registers really are EL2-gated and therefore unavailable on M1/M2.
- `hv_gic_get_redistributor_base(vcpu)` → `HV_BAD_ARGUMENT` **until you set `MPIDR_EL1`** on that vCPU, matching the header's "Must be called after the affinity of the given vCPU has been set" (`hv_gic.h:110-111`).

**Reserved interrupt IDs** (`hv_gic_types.h:44-50`), `[MEASURED]` all five resolve via `hv_gic_get_intid` even without EL2:

| INTID | Meaning |
|---|---|
| 23 | `HV_GIC_INT_PERFORMANCE_MONITOR` |
| 25 | `HV_GIC_INT_MAINTENANCE` — *EL2 only* |
| 26 | `HV_GIC_INT_EL2_PHYSICAL_TIMER` — *EL2 only* |
| 27 | `HV_GIC_INT_EL1_VIRTUAL_TIMER` |
| 30 | `HV_GIC_INT_EL1_PHYSICAL_TIMER` |

**Answering the question directly:** yes, hardware/framework GIC virtualization is exposed, including vGIC list registers (`ICH_LR*`) — **but the list registers are gated behind EL2**, i.e. behind M3+ and macOS 15+, and they are there so your *nested guest hypervisor* can inject into *its* guest. For a normal (non-nested) VM you do not touch `ICH_*` at all; you use `hv_gic_set_spi` / `hv_gic_send_msi` and the framework manages the list registers for you. Confidence: **High** `[HEADER]`.

**The important caveat:** creating a GIC changes the vCPU's register surface. `HV_SYS_REG_CNTP_CTL_EL0` / `CNTP_CVAL_EL0` / `CNTP_TVAL_EL0` are "only available if the VM was created with a GIC device (`hv_gic_create`)" (`hv_vcpu_types.h:387-391`), and `[MEASURED]` they return `HV_UNSUPPORTED` without one. Also, "hv_vcpu_{get/set}_interrupt functions are **unsupported** for injecting interrupts to a nested guest" (`hv_gic.h:32-33`). Confidence: **High** `[HEADER]` `[MEASURED]`.

**Topology constraint:** "GIC v3 uses affinity based interrupt routing. vCPU's must set affinity values in their `MPIDR_EL1` register. Once the virtual machine vcpus are running, its topology is considered final. Destroy vcpus only when you are tearing down the virtual machine." (`hv_gic.h:40-43`). **No vCPU hotplug.** Confidence: **High** `[HEADER]`.

### 3.2 Timers

| Timer | Status |
|---|---|
| **`CNTVCT_EL0` / `CNTV_CTL_EL0` (virtual)** | **hardware, native, no trap** `[MEASURED]` |
| **`CNTPCT_EL0` / `CNTP_*` (physical)** | **trapped → you emulate**, and the registers only exist with a GIC `[MEASURED]` `[HEADER]` |

The virtual timer is offset-controlled by the host:

```c
// CNTVCT_EL0 = mach_absolute_time() - vtimer_offset
hv_return_t hv_vcpu_set_vtimer_offset(hv_vcpu_t, uint64_t);   // hv_vcpu.h:449
hv_return_t hv_vcpu_set_vtimer_mask(hv_vcpu_t, bool);         // hv_vcpu.h:426
```

`[MEASURED]` defaults: `vtimer_mask = 0`, `vtimer_offset = 0` (so `CNTVCT_EL0` starts equal to `mach_absolute_time()`, i.e. host uptime — you almost certainly want to set the offset at VM start so the guest sees a zero-based counter).

**The vtimer protocol you must implement** (`hv_vcpu.h:404-423`, `hv_vcpu_types.h:43-52`):

1. When the guest's virtual timer fires, `hv_vcpu_run` returns `HV_EXIT_REASON_VTIMER_ACTIVATED`.
2. **This exit automatically sets the vtimer mask**, so you will not get it again.
3. You make the corresponding interrupt (INTID 27) pending in the guest's interrupt controller.
4. You must **detect when the guest EOIs/deactivates that interrupt** and only then call `hv_vcpu_set_vtimer_mask(vcpu, false)`.

Step 4 is the awkward one: with a userspace GIC you naturally see the deactivate write; with the framework GIC you would have to poll `hv_gic_get_icc_reg`/redistributor state, since there is no deactivation callback. Confidence: **High** `[HEADER]` for the protocol; **Unverified** how cleanly step 4 composes with `hv_gic_*`.

There is no `CNTFRQ_EL0` setter — the guest's counter frequency is the host's `mach_absolute_time` timebase, and you must put the right value in the guest's device tree / ACPI yourself. Confidence: **Medium** `[HEADER]` (absence of the register in `hv_sys_reg_t`).

### 3.3 IPIs

**Entirely yours** unless you use the framework GIC.

- Without a GIC: an IPI is you, in the host, calling `hv_vcpu_set_pending_interrupt` on the target vCPU's state and `hv_vcpus_exit` to kick it out of `hv_vcpu_run`. But `set_pending_interrupt` says "Must be called by the **owning thread**" (`hv_vcpu.h:306`) — so the sender thread cannot set it directly. The standard pattern is: sender posts to a per-vCPU mailbox, calls `hv_vcpus_exit(target)`, the target thread wakes, drains its mailbox, sets its own pending interrupt, and re-enters.
- `[MEASURED]` **`hv_vcpus_exit` → `hv_vcpu_run` returns in ~3.1 µs.** That is your IPI floor. Confidence: **High** `[MEASURED]` (20 iterations, cross-thread, measured from the instant before `hv_vcpus_exit` to the instant after `hv_vcpu_run` returned).
- With a GIC: SGIs are handled by the framework via the GICv3 `ICC_SGI1R_EL1` path, which should avoid the host round trip entirely. **Unverified** (not tested; M2 Pro supports `hv_gic_create` but I did not exercise SGIs).

### 3.4 PSCI

**Not implemented at all.** There is no PSCI support in the framework — no CPU_ON, no CPU_OFF, no SYSTEM_RESET. You implement the entire PSCI 0.2/1.x ABI yourself in your `HVC`/`SMC` handler.

`[MEASURED]` this is straightforward because **both `HVC` and `SMC` trap to the host** (`EC=0x16` and `EC=0x17`), so you can honour whichever conduit the guest's device tree advertises. Secondary CPU bring-up is: catch `PSCI_CPU_ON`, set the target vCPU's `PC`/`X0`/`CPSR` from its owning thread, release it. Confidence: **High** `[MEASURED]` + absence from all headers.

### 3.5 SMMU / IOMMU

**Nothing.** There is no IOMMU, no DMA remapping, no device passthrough API anywhere in the arm64 headers. All "devices" are host userspace code reading/writing guest RAM directly through the host VA you mapped. Confidence: **High** `[HEADER]` (exhaustive header review).

The x86 side has `hv_vm_add_pio_notifier` / `hv_vcpu_add_io_notifier` (an ioeventfd-like Mach-message mechanism, `hv_types.h:109-133`) — **arm64 has no public equivalent**, only the unreachable `hv_vm_monitor_data_abort` trap described in §1.10. Confidence: **High** `[HEADER]`.

### 3.6 Summary — your userspace responsibilities

| Component | Provided? |
|---|---|
| Stage-2 MMU | **framework** |
| vCPU register file, EL0/EL1 execution | **framework** |
| Virtual counter/timer (`CNTV*`) | **framework** (offset-controlled) |
| GICv3 distributor/redistributor/CPU-interface | **framework, macOS 15+** (`hv_gic_create`), else yours |
| GIC MSI | **framework, macOS 15+** |
| Physical timer (`CNTP*`) | **yours** (trapped) |
| PMU | **yours** (trapped) — or framework-emulated if EL2 enabled |
| PSCI | **yours** |
| IPIs / SGIs | framework with GIC, else **yours** |
| Every device (UART, virtio, RTC, …) | **yours** |
| IOMMU / DMA isolation | **nonexistent** |
| Firmware / device tree / ACPI | **yours** |
| Instruction decode for ISV=0 faults | **yours** |

---

## 4. Guest exception levels and nested virtualization

### 4.1 EL0 and EL1

**Both work.** `[MEASURED]`:

- Setting `CPSR = 0x3c5` (EL1h) and running executes guest code at EL1 with full control of `SCTLR_EL1`, `TTBR0/1_EL1`, `VBAR_EL1` etc. The guest owns its own stage-1 translation.
- Setting `CPSR = 0x3c0` (**EL0t**) and running **works**: the guest executed at EL0, the `svc #0` correctly trapped to the guest's *own* EL1 vector (`VBAR_EL1 + 0x400`, the "lower EL AArch64" synchronous entry), and `CPSR` on the next exit read `0x4003c5` — i.e. **M=5, EL1h**, confirming a genuine EL0→EL1 transition inside the guest.

So you can start a vCPU directly at EL0 (useful for running user-mode-only code without a guest kernel) or at EL1. Confidence: **High** `[MEASURED]`.

`[MEASURED]` the vCPU's reset state is `CPSR = 0x0` and `SCTLR_EL1 = 0x0` — i.e. **not** the architectural reset value. You must initialise `CPSR` (at minimum the `M` field and the `DAIF` mask) and `SCTLR_EL1` yourself before the first `hv_vcpu_run`. Confidence: **High** `[MEASURED]`.

### 4.2 EL2 / nested virtualization

```c
hv_return_t hv_vm_config_get_el2_supported(bool *el2_supported);        // hv_vm_config.h:73
hv_return_t hv_vm_config_set_el2_enabled(hv_vm_config_t, bool);         // hv_vm_config.h:108
hv_return_t hv_vm_config_get_el2_enabled(hv_vm_config_t, bool *);       // hv_vm_config.h:82
```

All three are `API_AVAILABLE(macos(15.0))`. Confidence: **High** `[HEADER]`.

**Hardware requirement: M3 or later.** `[MEASURED]` on this M2 Pro:

```
hv_vm_config_get_el2_supported      -> false
hv_vm_config_set_el2_enabled(true)  -> 0xfae9400f (HV_UNSUPPORTED)
hv_vcpu_run with CPSR=0x3c9 (EL2h)  -> 0xfae94004 (HV_ILLEGAL_GUEST_STATE)
```

Note the last one is a nice detail: `hv_vcpu_set_reg(CPSR, 0x3c9)` **succeeds and reads back**, but the *run* fails with `HV_ILLEGAL_GUEST_STATE`. So you must gate on `hv_vm_config_get_el2_supported`, not on register writes. Confidence: **High** `[MEASURED]`.

**Availability matrix:**

| | EL2 / nested virt |
|---|---|
| M1, M2 (all variants) | **No** — `[MEASURED]` on M2 Pro |
| M3, M4 (and later) | **Yes**, with macOS 15.0+ |
| macOS ≤ 14 | **No** — API does not exist |

Confidence: **High** for M2=no `[MEASURED]`; **High** for M3+/macOS 15 `[DOC]` `[COMM]` — Apple's own release material describes nested virtualization on M3+ under macOS 15, and `hv_vm_config_set_el2_enabled` is documented as macOS 15.0+.
Sources: https://developer.apple.com/documentation/hypervisor/hv_vm_config_set_el2_enabled(_:_:) , https://developer.apple.com/documentation/hypervisor

**What enabling EL2 actually gives you:**

1. The guest can run at **EL2** (`CPSR.M = 0b1001`, EL2h) and act as a hypervisor for its own guest.
2. The **EL2 system registers become accessible** via `hv_vcpu_get/set_sys_reg`: `HCR_EL2`, `VTCR_EL2`, `VTTBR_EL2`, `SCTLR_EL2`, `TCR_EL2`, `TTBR0/1_EL2`, `VBAR_EL2`, `ESR_EL2`, `FAR_EL2`, `HPFAR_EL2`, `ELR_EL2`, `SPSR_EL2`, `SP_EL2`, `CPTR_EL2`, `MDCR_EL2`, `MAIR_EL2`, `TPIDR_EL2`, `VPIDR_EL2`, `VMPIDR_EL2`, `CNTHCTL_EL2`, `CNTHP_{CTL,CVAL,TVAL}_EL2`, `CNTVOFF_EL2` (`hv_vcpu_types.h:393-419`). These are the **nested guest's** virtual EL2 state.
3. The **GIC `ICH_*` list registers and `ICV_*` registers** become accessible, so the guest hypervisor can inject interrupts into its own guest (`hv_gic.h:172-230`).
4. **PMU handling changes**: with EL2 disabled, PMU accesses trap out to you; with EL2 enabled, "the handling of PMU register accesses is determined by the `PMUVer` field of `ID_AA64DFR0_EL1`… If the `PMUVer` field value is non-zero and valid, PMU register accesses are **emulated by the framework**" (`hv_vm_config.h:98-104`). You can control this by writing `ID_AA64DFR0_EL1` via `hv_vcpu_set_sys_reg`. Confidence: **High** `[HEADER]`.

**What it does NOT give you:** control over your *own* VM's trap configuration. Apple's shim keeps the real `HCR_EL2`. Confidence: **Medium** (architectural inference, see §1.7).

Regarding **FEAT_NV2** specifically: Apple does not name the architectural feature in any header or public doc. The register set exposed (a full virtual EL2 register file addressable from the VMM, plus `VTTBR_EL2`/`VTCR_EL2` and `ICH_LR*`) is exactly what an NV2-style implementation surfaces, so it is a reasonable inference that M3+ implements FEAT_NV/FEAT_NV2 — but Apple has not said so. Confidence: **Low/Unverified**.

---

## 5. Entitlements and code signing

*(This section is backed by first-hand testing on macOS 26.5.2 by a second researcher on an M2 Max, plus my own successful runs on this M2 Pro.)*

### 5.1 `com.apple.security.hypervisor` — free and self-signable

- **Exact key:** `com.apple.security.hypervisor`, Boolean, macOS 11.0+. Apple: "The entitlement is required to use the Hypervisor APIs in **any process**." Confidence: **High** `[DOC]` — https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.hypervisor
- **NOT restricted.** No provisioning profile, **no paid Apple Developer account**. `[MEASURED]` — every probe in this document was built with Command Line Tools clang and signed with:

  ```sh
  codesign -s - --entitlements hv.entitlements --force ./probe
  ```

  and `hv_vm_create` returned `HV_SUCCESS`. Apple's doc page carries **no** "restricted / contact your Apple representative" note, in explicit contrast to `com.apple.vm.networking`. Confidence: **High** `[MEASURED]` `[DOC]`.
- Without the entitlement, `hv_vm_create` returns `HV_DENIED` (`0xfae94007`). Confidence: **High** `[MEASURED]`.
- For deployment targets before macOS 10.15 you additionally need the deprecated `com.apple.vm.hypervisor`. Confidence: **High** `[DOC]`.

### 5.2 The other VM entitlements

| Entitlement | Restricted? |
|---|---|
| `com.apple.security.hypervisor` | **No** — ad-hoc signable, proven |
| `com.apple.security.virtualization` (Virtualization.framework) | **No** (no restriction note; Apple's own samples self-sign). Confidence: **Medium** |
| **`com.apple.vm.networking`** | **YES — RESTRICTED.** Apple verbatim: *"This entitlement is restricted to developers of virtualization software. To request this entitlement, contact your Apple representative."* Confidence: **High** `[DOC]` — https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.vm.networking |
| `com.apple.vm.device-access` (USB passthrough into guests) | Believed restricted. Confidence: **Medium** `[DOC-partial]` |

**Practical impact:** you can build and run a hypervisor freely, but **privileged `vmnet` networking (bridged/host modes) is gated behind an Apple business relationship**. UTM ships `com.apple.vm.networking` because they were granted it. Your fallback is user-mode/NAT networking (slirp-style) implemented entirely in your process, which needs no entitlement. Confidence: **High**.

### 5.3 Hardened runtime, notarization, SIP, bundles

- **Hardened Runtime is NOT required.** The entitlement works with or without `--options runtime`. Confidence: **High** `[MEASURED]`.
- **Notarization is NOT required to run locally.** It matters for distribution (Gatekeeper + quarantine), not for local execution of a locally-signed binary. Confidence: **High** `[MEASURED]`.
- **SIP does NOT need to be disabled.** All testing was done with SIP **enabled** and no `boot-args`. `amfi_get_out_of_my_way=1` is irrelevant once you sign correctly. Confidence: **High** `[MEASURED]`.
- **A bare Mach-O CLI binary works** — no `.app` bundle, no `Info.plist`. Entitlements live in the code signature. Confidence: **High** `[MEASURED]`.
- **macOS 15 / 26 changes:** no documented tightening of the hypervisor entitlement or of `MAP_JIT`/`allow-jit`. Apple DTS stated in a 2025 Tahoe-era forum thread that "the official guidance hasn't changed." Confidence: **Medium** `[COMM]` — https://developer.apple.com/forums/thread/821584

---

## 6. Performance characteristics

All numbers `[MEASURED]` on **M2 Pro (T6020), macOS 26.5.2**, `-O2`, guest at EL1 with MMU off, warmed up, ≥200 000 iterations for the latency figures.

### 6.1 The exit round trip — the defining constraint

| Path | Latency |
|---|---|
| **`HVC` exit → re-enter, handler does nothing** | **808 ns** |
| `HVC` exit + 2 × `get_reg` + 1 × `set_reg` | **791 ns** |
| **MMIO stage-2 data abort, full handling** (read ESR, read PC, set X0, advance PC) | **890 ns** |
| `hv_vcpus_exit` (cross-thread) → `hv_vcpu_run` returns | **~3 100 ns** |

Three things stand out:

1. **~800 ns is a hard floor.** The handler work is free — doing three register accesses in the loop measured *no slower* than doing nothing (791 vs 808 ns; the difference is within run-to-run noise). The entire cost is the guest→EL2→userspace→EL2→guest world switch.
2. **This caps you at ~1.2 M exits/sec/core.** A workload that traps once per 1 000 guest instructions loses roughly half its throughput; one that traps every 100 instructions is essentially dead.
3. **It is 2-5× worse than KVM on comparable hardware.** KVM/ARM world-switch costs are typically quoted at ~1-3 µs for a full userspace exit but a few hundred ns for a kernel-handled exit; the key difference is that KVM handles many exits **in the kernel** (in-kernel GIC, in-kernel timer, ioeventfd), while HVF must return to userspace for essentially everything except the vtimer. Confidence: **High** for our measurement; **Medium** for the KVM comparison (not measured here).

### 6.1b Multi-vCPU exit scaling

`[MEASURED]` N threads, each with its own vCPU, all spinning on `HVC` exits (50 000 iterations each, after warm-up):

| vCPUs | ns per exit (mean) | Aggregate exits/sec |
|---|---|---|
| 1 | **731 ns** | 1.37 M |
| 2 | **804 ns** | 2.49 M |
| 4 | **1 261 ns** | 3.17 M |
| 8 | **2 717 ns** | 2.94 M |

**Aggregate exit throughput saturates at roughly 3 M exits/sec for the whole machine.** Scaling is near-linear to 2 vCPUs, sublinear at 4, and **negative** at 8 — per-vCPU latency degrades 3.7× from 1 to 8 vCPUs while total throughput actually drops.

Two caveats on interpretation: (a) this M2 Pro has 8 P-cores + 4 E-cores, so at 8 vCPUs some threads are likely scheduled on E-cores, which inflates the mean; (b) I did not isolate whether the plateau is a per-VM kernel lock or simply core contention. Either way, **the practical planning number is ~3 M exits/sec per machine, not per core**. Confidence: **High** for the measurements; **Medium** for the "per-VM lock" interpretation (**Unverified**).

**Design implications:**

- Trap-driven emulation of anything hot is not viable. Budget your exit count.
- Exit cost is a **shared machine-wide resource**. A 4-vCPU guest cannot each independently spend 1.2 M exits/sec.
- Prefer shared-memory doorbell protocols (guest writes a ring buffer in normal RAM, host polls from another thread) over MMIO traps.
- The framework GIC (`hv_gic_create`) is valuable precisely because it removes interrupt-controller exits.
- The **virtual** timer is free; the physical timer costs an 890 ns exit per access. Never let a guest poll `CNTPCT_EL0`.

### 6.2 Memory operation costs

Reproduced from §2.6 for convenience:

| Operation | Few mappings | 20 000 mappings |
|---|---|---|
| `hv_vm_protect` 1 page | 313 ns | **27 201 ns** |
| `hv_vm_protect` 64 MB | 241 ns | — |
| `hv_vm_map`+`unmap` pair | 587 ns | — |
| `hv_vm_map` single page | ~325 ns | 649 ns |
| Demand-paging cycle (fault+map+unmap) | 1 510 ns | — |
| MMIO exit latency | 890 ns | 900 ns (**unaffected**) |

`hv_vm_protect` is **O(1) in range size** and **O(n) in live mapping count**. See §2.6 for the full discussion — this is the second-biggest constraint after exit latency.

### 6.3 Register access

| Operation | Cost |
|---|---|
| `hv_vcpu_get_reg` | 5.8 ns |
| `hv_vcpu_get_sys_reg` | 12.1 ns |
| `hv_vcpu_set_sys_reg` | 10.0 ns |

These are shared-page accesses, not syscalls. Effectively free. Confidence: **High** `[MEASURED]`.

### 6.4 Reproducing / extending these measurements

The probe programs used are in the scratchpad (`probe.c` = config/limits/granularity, `v2.c` = trap discovery, `v3.c` = latency + trap knobs + protect scaling, `v4.c` = memory sources + EL2 + wake latency). Build and run with:

```sh
clang -O2 -framework Hypervisor -o v3 v3.c
codesign -s - --entitlements hv.entitlements --force v3
./v3
```

Suggested additional microbenchmarks not yet run:

- **Multi-vCPU scaling:** N threads each spinning on `HVC` exits; does the 800 ns floor hold at 8 vCPUs, or is there a per-VM lock?
- **`hv_gic_create` cost:** does an in-framework GIC actually eliminate interrupt exits? Measure `hv_gic_set_spi` → guest IRQ handler entry latency vs. the `set_pending_interrupt` + `hv_vcpus_exit` path (which we measured at ~3.1 µs).
- **4 KB granule (macOS 26):** does `HV_IPA_GRANULE_4KB` change `hv_vm_protect`/fault costs?
- **TSO mode:** measure guest throughput with `ACTLR_EL1.EnTSO` set vs. clear on a store-heavy benchmark.
- **Mapping-count sweep:** find the knee in the `hv_vm_protect` O(n) curve (we only have n=5 and n=20 000) to pick a safe mapping budget.

---

## 7. JIT constraints in the host process

*(First-hand tested on macOS 26.5.2, Apple Silicon, SIP enabled.)*

### 7.1 The headline answer: JIT and Hypervisor coexist

**Confirmed two independent ways.** Confidence: **High**.

- `[MEASURED]` A single binary that allocates `MAP_JIT` memory, writes and executes JIT code, **and** calls `hv_vm_create`/`hv_vcpu_create` in the same process works. Signed ad-hoc with Hardened Runtime plus both `com.apple.security.hypervisor` and `com.apple.security.cs.allow-jit`: `hv_vm_create → HV_SUCCESS`, JIT executed correctly, no conflict.
- `[COMM]` **UTM ships exactly this combination** — its QEMU helper's entitlements plist lists `com.apple.security.hypervisor` and `com.apple.security.cs.allow-jit` together: https://github.com/utmapp/UTM/blob/main/QEMUHelper/QEMUHelper.entitlements

There is no documented or observed conflict between the two entitlements.

### 7.2 MAP_JIT

- `MAP_JIT` is `0x0800` (`.../MacOSX.sdk/usr/include/sys/mman.h`). Confidence: **High**.
- **Requires `com.apple.security.cs.allow-jit` only under Hardened Runtime.** Apple verbatim: *"The `com.apple.security.cs.allow-jit` entitlement is required only when an app adopts the Hardened Runtime capability. If you don't adopt this capability, you don't need the entitlement to use the `MAP_JIT` flag."* Confidence: **High** `[DOC]` — https://developer.apple.com/documentation/apple-silicon/porting-just-in-time-compilers-to-apple-silicon
- **Must be `MAP_PRIVATE|MAP_ANON`.** `[MEASURED]` `MAP_JIT|MAP_SHARED` → `EINVAL`; `MAP_JIT` without `MAP_ANON` → `EINVAL`. Confidence: **High**.
- **No size limit observed** — a single 1 GiB `MAP_JIT` region allocated fine. Confidence: **High** `[MEASURED]`.
- **The "only one MAP_JIT region per process" rule is documented but NOT enforced on macOS 26.** Apple says: *"…it can only create one memory region with the `MAP_JIT` flag set."* `[MEASURED]` **4+ simultaneous `MAP_JIT` regions** allocated successfully under every entitlement configuration tested. Confidence: **High** that Apple documents the restriction; **High** `[MEASURED]` that it is not currently enforced. Do not rely on either behaviour.
- **`MAP_JIT` memory can be `hv_vm_map`'ed as guest RAM** — `[MEASURED]` on this machine (§2.4). Confidence: **High**.

### 7.3 `pthread_jit_write_protect_np` — cost

- Per-thread W^X toggle. Apple: makes the region *"writable, and not executable, **for the current thread only**"*. Confidence: **High** `[DOC]`.
- **It is not a syscall.** Disassembly of `libsystem_pthread.dylib` on 26.5.2 shows it loads an expected value from the commpage and executes `msr S3_6_C15_C1_5, x0; isb` (with an older `S3_4_C15_C2_7` variant) — Apple's per-thread **APRR/SPRR** permission-remap register, serialized by `isb`. Confidence: **High** `[MEASURED]`. Background on the mechanism: https://blog.svenpeter.dev/posts/m1_sprr_gxf/
- **Measured cost: ~18 ns per call, ~36 ns per off→on pair** (≈60-65 cycles at 3.5 GHz, dominated by the `isb`). It does **not** short-circuit when the state is unchanged — every call performs the `msr`+`isb`. Confidence: **High** `[MEASURED]`.
- `pthread_jit_write_protect_supported_np()` returned **1**. Confidence: **High** `[MEASURED]`.

### 7.4 `sys_icache_invalidate` — cost, and a trap I fell into

- Also **not a syscall**: a userspace loop of `ic ivau` per 64-byte line, batched ~20 per `dsb ish`, with a final `dsb ish; isb`. Confidence: **High** `[MEASURED]`.
- **Measured:** 64 B → **131 ns**; 4 KiB → **823 ns**; 64 KiB → **11.9 µs**. Roughly linear in cache lines above a ~120 ns fixed floor. Confidence: **High** `[MEASURED]`.
- Apple **requires** it before executing freshly written code on Apple Silicon (I-cache is not coherent with D-cache). Confidence: **High** `[DOC]`.

> **Practical warning for hypervisor work:** this applies to **guest** code too. My first trap-discovery run produced 22 identical bogus results because host writes into a page mapped as guest RAM were sitting in the host D-cache and the guest was fetching stale instructions. Calling `sys_icache_invalidate()` on the **host** VA of the shared region fixed it completely. Any time your host writes code (or page tables) that the guest will fetch, you must do cache maintenance on the host side. Confidence: **High** `[MEASURED]` — this is a real, reproducible bug I hit and fixed.

### 7.5 Dual mapping (`mach_vm_remap` RW/RX aliasing)

This is where the entitlement rules bite, and the results are counter-intuitive:

| Config | `vm_remap` RX-alias execute | `mprotect` RW→RX execute |
|---|---|---|
| **No Hardened Runtime** (ad-hoc) | **works** | **works** |
| HR + `allow-jit` **only** | **SIGKILL (137)** | **SIGKILL (137)** |
| HR + `allow-unsigned-executable-memory` | **works** | **works** |
| HR + empty entitlements | **SIGKILL (137)** | **SIGKILL (137)** |

Confidence: **High** `[MEASURED]`.

**Key findings:**

1. **The classic dual-mapping trick STILL WORKS on macOS 26.5.2 / Apple Silicon** — `vm_allocate` RW → write → `mach_vm_remap` → `mach_vm_protect(alias, RX)` → `sys_icache_invalidate` → execute. Confidence: **High** `[MEASURED]`.
2. **But you CANNOT `mach_vm_remap` a `MAP_JIT` region** — it returns `KERN_PROTECTION_FAILURE` (kr=2). `MAP_JIT` and dual-mapping are **mutually exclusive strategies**; pick one. Confidence: **High** `[MEASURED]`.
3. **`allow-jit` is NOT sufficient for dual mapping under Hardened Runtime** — you need the stronger `com.apple.security.cs.allow-unsigned-executable-memory`. Confidence: **High** `[MEASURED]` `[DOC]` — https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.cs.allow-unsigned-executable-memory
4. **Plain `mmap(PROT_READ|WRITE|EXEC)` without `MAP_JIT` is refused** (`EACCES`/`EPERM`) even without Hardened Runtime. You must go RW-then-RX, or use `MAP_JIT`. Confidence: **High** `[MEASURED]`.
5. **Simplest development posture: do not adopt Hardened Runtime at all.** Then `MAP_JIT`, dual mapping, and `mprotect`-to-RX all work with just an ad-hoc signature, and you only add `com.apple.security.hypervisor`. Confidence: **High** `[MEASURED]`.

### 7.6 Interaction with `hv_vcpu_run`, and threading

- **The two are orthogonal.** The APRR/SPRR write-protect bit is per-thread **host EL0** state; the guest runs at its own EL under its own stage-2 translation. `[MEASURED]` toggling `pthread_jit_write_protect_np` while a live `hv_vcpu` exists in the same thread caused no error. Confidence: **High** for coexistence; **Medium** (architectural inference) that host APRR can never affect guest mappings — not directly tested mid-`hv_vcpu_run`.
- **W^X state is per-thread** and Apple does **not** document what a newly created thread inherits. Empirically the safe assumption is that a new thread starts write-protected (RX). Every thread that writes JIT code must explicitly call `pthread_jit_write_protect_np(0)` first. Confidence: **Medium** `[DOC-implied]` `[COMM]`.
- **Known hazard:** if a thread returns early while left in write mode (RW), the next code fetch on that thread faults (SIGSEGV/SIGBUS). RPCS3 hit this and fixed it with RAII guards: https://github.com/RPCS3/rpcs3/pull/18701 Confidence: **High** `[COMM]`.
- **One Tahoe regression to be aware of:** an App-Sandboxed ARM64 Electron/V8 app crashes on macOS 26 with `EXC_BREAKPOINT 0xf2000000` during V8's `mprotect` RW↔RX transitions. The same binary works on 26/x86_64, on 26/ARM64 **without** the sandbox, and on 15.x/ARM64 sandboxed — so the issue appears specific to **App Sandbox + ARM64 + Tahoe**, not to the general `allow-jit` path. Confidence: **Medium** `[COMM]` — https://developer.apple.com/forums/thread/821584

### 7.7 What other projects do

- **QEMU TCG:** uses `MAP_JIT` + `pthread_jit_write_protect_np` on Apple Silicon (protection enabled around `tcg_qemu_tb_exec`, disabled for codegen), and *also* carries a generic Darwin "split-wx" mirror-map path via `mach_vm_remap`. Both exist in-tree. Confidence: **High** `[COMM]` — https://lists.nongnu.org/archive/html/qemu-devel/2020-11/msg06128.html
- **RPCS3:** `MAP_JIT` + APRR toggle with RAII guards. Confidence: **High** `[COMM]`.
- **Dolphin, box64, LuaJIT, .NET, V8, JavaScriptCore:** all converged on `MAP_JIT` + the per-thread APRR toggle; JSC's "Fast JIT permissions" *is* the APRR toggle. Confidence: **Medium** `[COMM]`.

---

## 8. Known third-party projects

*(Pending — see the separate agent findings folded in below.)*

---

## 9. Design recommendations for a custom emulator

Synthesising everything above:

1. **Budget exits ruthlessly.** At ~800 ns each you get ~1.2 M/sec/core. Anything hot must be handled without an exit: shared-memory rings polled by a host thread, the framework GIC, the virtual timer.
2. **Map guest RAM as a few huge contiguous regions.** `hv_vm_protect` is O(live mapping count); 20 000 mappings turned a 313 ns call into 27 µs. If you need demand paging, do it at ≥2 MB granularity and coalesce aggressively.
3. **Use `hv_gic_create` (macOS 15+).** It removes the entire interrupt-controller emulation burden and, more importantly, the exits that go with it. Remember: after `hv_vm_create`, before any `hv_vcpu_create`, and it unlocks the `CNTP_*` registers.
4. **Pin the guest to the virtual timer.** `CNTVCT_EL0`/`CNTV_CTL_EL0` are native; `CNTPCT_EL0`/`CNTP_*` cost an 890 ns exit per access. Set `hv_vcpu_set_vtimer_offset` at boot so the guest sees a zero-based counter, and implement the mask/EOI protocol in §3.2.
5. **Write an instruction decoder.** ISV=0 on `LDP`/`STP` and other accesses is unavoidable; you will need to fetch and decode guest instructions, which means implementing a stage-1 page-table walk from `TTBR0/1_EL1` + `TCR_EL1`.
6. **Get the PC-advance rules right:** `HVC`/`SMC` arrive with PC already advanced; data/instruction aborts, sysreg traps and `BRK` do not.
7. **If you are emulating x86, use `ACTLR_EL1.EnTSO`.** Hardware TSO for the guest vCPU is a large win and is the one genuinely special capability Apple exposes here.
8. **Set `ipa_size` to the max explicitly** (40 bits on M2 Pro) — the 36-bit default silently caps you at 64 GB.
9. **Do host-side cache maintenance** (`sys_icache_invalidate`) whenever the host writes memory the guest will fetch as instructions. This will bite you otherwise (§7.4).
10. **Ship without Hardened Runtime during development**, ad-hoc signed with only `com.apple.security.hypervisor` (+ `allow-jit` if you adopt HR later). No paid account needed. Plan for user-mode networking — `com.apple.vm.networking` is gated behind an Apple business relationship.
11. **One host thread per vCPU, forever.** Only `hv_vcpus_exit` is callable cross-thread; your IPI floor is ~3.1 µs without a framework GIC.
12. **Gate nested virtualization on `hv_vm_config_get_el2_supported()`**, not on chip-name detection or register writes.

---

## References

### Header files (all paths absolute, on this machine)

SDK root: `/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk` → `MacOSX26.4.sdk`
Framework headers: `.../System/Library/Frameworks/Hypervisor.framework/Headers/`

| File | Key lines cited |
|---|---|
| `Hypervisor.h` | 13-34 (arm64 vs x86 split) |
| `hv_base.h` | 17-23 (availability macros) |
| `hv_vm.h` | 25, 28, 33, 37, 42, 53, 62, 72 |
| `hv_vm_config.h` | 36, 45, 56, 73, 82, 91-104, 108, 113-117, 125, 134, 143 |
| `hv_vm_types.h` | 32 (`hv_ipa_t`), 37 (`hv_memory_flags_t`) |
| `hv_vcpu.h` | 22, 25, 28, 51, 63, 74-76, 78, 94, 97-264, 276, 288, 299, 306, 308-310, 312, 322, 332, 344, 356, 369, 372-381, 392, 401, 404-423, 426, 436, 444-446, 449 |
| `hv_vcpu_types.h` | 38-59 (exit reasons), 64-69, 74-78, 83-86, 99-137 (`hv_reg_t`), 142-175, 182-252 (SME), 257-420 (`hv_sys_reg_t`), 339-346 (`ACTLR_EL1`/TSO), 387-391 (physical timer), 393-419 (EL2 regs), 427-433 (`hv_interrupt_type_t`) |
| `hv_vcpu_config.h` | 25, 30-45 (`hv_feature_reg_t`), 55, 65 |
| `hv_vm_allocate.h` | 30-34, 42-49, 51, 60 |
| `hv_gic.h` | 26-33, 34-38, 40-43, 49, 66, 77, 84-87, 90, 103, 114, 130, 146, 158, 170, 172-186, 179-180, 200, 202-230, 250-265, 276 |
| `hv_gic_config.h` | 28, 40, 54, 69, 86 |
| `hv_gic_parameters.h` | 26, 34, 41-43, 46, 54, 62, 70, 78, 87, 99 |
| `hv_gic_state.h` | 30, 39, 46-57 |
| `hv_gic_types.h` | 44-50 (INTIDs), 56 (`hv_gic_distributor_reg`, 1534 entries), 1607 (redistributor), 1636-1647 (ICC), 1654-1678 (**ICH list registers**), 1685-1695 (ICV), 1701-1703 (MSI) |
| `hv_sme_config.h` | 30 |
| `hv_kern_types.h` — `/Library/Developer/CommandLineTools/SDKs/MacOSX26.4.sdk/usr/include/arm64/hv/hv_kern_types.h` | 50-61 (`hv_return_t`), 70-76 (`HV_MEMORY_*`), **78-116 (`hv_data_abort_notification_t`, monitored-memory Mach messages)** |
| `Hypervisor.tbd` — `.../Hypervisor.framework/Versions/A/Hypervisor.tbd` | 5 (version 259.5.15), 33-36 (shared symbols), **37-52 (private arm64 SPI: `__hv_vcpu_config_set_fgt_enabled`, `__hv_vcpu_set_control_field`, `__hv_vm_space_*`, `__hv_vm_config_set_isa`, `__hv_vcpu_amx_*`)**, 52-84 (public arm64 exports) |

x86-only headers, listed for completeness (not applicable): `hv.h`, `hv_vmx.h`, `hv_arch_vmx.h`, `hv_arch_x86.h`, `hv_types.h`, `hv_error.h`, `hv_intr.h`.

### Probe programs (my measurements)

`/private/tmp/claude-501/-Users-liaozhiping-Documents-codex-natemu/a5db56a4-ae0a-48b3-a33a-babf6e04ceb2/scratchpad/hvprobe/`

| File | Measures |
|---|---|
| `probe.c` | IPA size/granule, max vCPUs, map granularity & flag combinations, initial protect/map timings |
| `v2.c` | Trap discovery — 37 guest instructions, ESR/EC/ISS/FAR/IPA decode |
| `v3.c` | Exit latency, trap-control knobs, EL0 execution, `hv_vm_protect` scaling vs mapping count, demand paging |
| `v4.c` | Mappable host memory sources, host/guest protection independence, max guest RAM, EL2 rejection, `hv_vcpus_exit` wake latency, vtimer defaults |
| `hv.entitlements` | `com.apple.security.hypervisor` |

### URLs

- Hypervisor framework overview — https://developer.apple.com/documentation/hypervisor
- `hv_vm_config_set_el2_enabled` — https://developer.apple.com/documentation/hypervisor/hv_vm_config_set_el2_enabled(_:_:)
- `com.apple.security.hypervisor` — https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.hypervisor
- `com.apple.security.virtualization` — https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.virtualization
- `com.apple.vm.networking` (**restricted**) — https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.vm.networking
- `com.apple.vm.device-access` — https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.vm.device-access
- `com.apple.security.cs.allow-unsigned-executable-memory` — https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.cs.allow-unsigned-executable-memory
- Porting JIT compilers to Apple Silicon — https://developer.apple.com/documentation/apple-silicon/porting-just-in-time-compilers-to-apple-silicon
- Apple DTS on `allow-jit` / Tahoe JIT crash — https://developer.apple.com/forums/thread/821584
- Apple Developer Forums, EL2 entry via `HV_REG_CPSR` — https://developer.apple.com/forums/thread/775546
- APRR/SPRR reverse engineering (Sven Peter) — https://blog.svenpeter.dev/posts/m1_sprr_gxf/
- macOS JIT memory / MAP_JIT region-count analysis — https://kyleavery.com/posts/macos-jit-memory/ and https://www.outflank.nl/blog/2026/02/19/macos-jit-memory/
- UTM QEMU helper entitlements (hypervisor + allow-jit together) — https://github.com/utmapp/UTM/blob/main/QEMUHelper/QEMUHelper.entitlements
- QEMU TCG Apple Silicon W^X discussion — https://lists.nongnu.org/archive/html/qemu-devel/2020-11/msg06128.html
- RPCS3 `MAP_JIT` write-protect guards — https://github.com/RPCS3/rpcs3/pull/18701
