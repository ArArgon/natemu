# 09 — Architectural Nuances Register

A running log of sharp-edged, easily-missed details that constrain the design. Each entry:
**what**, **why it bites**, **confidence**, **status** (Open / Resolved / Mitigated).

Confidence: `V` = verified on this machine or in a spec, `R` = reported by a credible source,
`I` = my inference, `?` = unverified.

---

## Host baseline (measured on this machine, 2026-08-08)

```
Machine       : Apple M2 Max
OS            : macOS 26.5.2 (build 25F84)
kern.hv_support        = 1     # Hypervisor.framework usable
kern.hv_vmm_present    = 0     # not currently running under a VMM
hw.optional.arm.FEAT_LSE2 = 1  # atomic/unaligned-atomic support present
machdep.virtual_address_size = 47
```

---

## N-01 — Host userspace VA is 47 bits, x86-64 guests want 48

**What.** `machdep.virtual_address_size = 47` on this Mac (`V`). A macOS user process gets a
47-bit virtual address space. x86-64 defines a 48-bit canonical VA space (two 47-bit halves:
`0x0000_0000_0000_0000..0x0000_7FFF_FFFF_FFFF` and `0xFFFF_8000_0000_0000..`), and every real
x86-64 kernel lives in the upper half.

**Why it bites.** The single fastest softmmu trick — map the entire guest VA space 1:1 into the
host VA space and let the host MMU do the translation with zero software cost — needs the host
to have *more* VA bits than the guest. Here the host has 47 usable bits total, and the guest
wants 47 bits *per half*, plus the emulator's own code/data/code-cache must live somewhere.
A flat identity map of a full x86-64 address space into a macOS process is **not possible**.

**Consequences / options.**
- Confine guests to a 32-bit or ~small-47-bit VA window (fine for a toy, useless for a real OS).
- Software TLB / softmmu (QEMU's approach) — the thing we're trying to beat.
- Run the translator inside a Hypervisor.framework VM where *we* program `TCR_EL1` and can
  claim the full 48-bit (or 52-bit with FEAT_LVA) VA space at EL1/EL0. **This is currently the
  strongest single argument for the "helper VM" design** (`I`, needs confirmation that Apple
  cores implement 48-bit VA at EL1 in a guest — see N-02).
- Shadow paging into ARM stage-1 tables, which is only available if we own EL1 anyway.

**Status.** Open — pivotal. Feeds the Design A vs Design B decision.

---

## N-02 — Page granule mismatch: Apple's native page is 16 KB, x86 assumes 4 KB

**What.** Apple Silicon uses a 16 KB base page in macOS. x86-64 paging is defined on 4 KB pages
(plus 2 MB / 1 GB large pages). (`V` for macOS; `?` for whether Apple cores implement the ARM
4 KB translation granule, `ID_AA64MMFR0_EL1.TGran4`, at EL1 inside a guest.)

**Why it bites.** If we want the ARM hardware MMU to *be* the guest's MMU (shadow paging), the
protection granularity must be ≤ the guest's. With only a 16 KB granule, one ARM PTE covers four
x86 PTEs that may legitimately have different R/W/X/user permissions — so we would have to
demote to software emulation whenever a 16 KB span is non-uniform. Same problem for
write-protecting code pages for self-modifying-code detection: 16 KB granularity means ~4x the
false-positive write traps.

**Status.** **RESOLVED — PASS. 2026-08-08 live probe `probe_g1_tgran_tso.c` / `/tmp/hv_g1_probe/run.log`:** guest EL1 `ID_AA64MMFR0_EL1=0x000010000f100002` → `TGran4=0x0 SUPPORTED`, `TGran16=0x1 SUPPORTED`, `TGran64=0xF NI`, `PARange 40`; `hv_vm_config_set_ipa_granule(HV_IPA_GRANULE_4K)` + `hv_vm_map` 4K both succeed (macOS 26+). So helper VM at EL1 **can** use `TCR_EL1.TG0/TG1=4K` and 4K stage-2; 1:1 shadow paging is viable. The residual N-02 cost is PTE-quad homogeneity (see 11-pte-quad-gate.md: synthetic 55–87% → `natemu-page-quads` PV mandatory) and 4K TLB reach (12 MiB vs 48 MiB). G-1 CLOSED.

---

## N-03 — This machine (M2) has no nested virtualization

**What.** Nested virtualization on Apple Silicon arrived with M3-class cores and later macOS
releases; M1/M2 do not support it (`R`).

**Why it bites.** Two separate things must not be confused:
1. The *helper VM* design needs only **one** level of virtualization (our translator runs as the
   guest of Hypervisor.framework). M2 is fine for that.
2. Running an x86 guest that itself uses VT-x/AMD-V (e.g. Docker Desktop inside the guest, or
   Windows with VBS/HVCI enabled) requires us to emulate x86 virtualization extensions in
   software. That is *not* helped by ARM nested virt anyway — but if we ever wanted to
   accelerate the translator's own EL2 needs, M2 is a hard stop.

**Status.** Open — mostly a note that Windows-with-VBS is out of scope on any near-term design.

---

## N-04 — Timebase resolution mismatch: `CNTVCT_EL0` ticks at 24 MHz

**What.** Apple Silicon's architectural counter runs at ~24 MHz (`R`). x86 `RDTSC` on a modern
part increments at the nominal core frequency (GHz range) and guests assume invariant, high
resolution TSC. Verified: `CNTVCT_EL0` is native/no-trap (`02 §1.5/§3.2`); `CNTPCT` traps (`EC=0x18`) and is GIC-gated.

**Why it bites.** A naive `RDTSC` → `CNTVCT_EL0` mapping gives ~41.7 ns granularity. Guest code
that busy-loops on TSC deltas, calibrates loops, or uses TSC for fine-grained profiling will see
quantized, sometimes-zero deltas. Linux's TSC calibration and `tsc_clocksource` watchdog may
reject it. A scaled/interpolated software TSC costs an extra multiply per `RDTSC` and breaks
monotonicity guarantees across cores if done carelessly.

**Mitigation — designed in 10 §2.** Scale `TSC=(CNTVCT*mul)>>shift` (~2.4 GHz), advertise `CPUID 0x15/0x16` + `0x40000010` to skip PIT/HPET calibration, per-vCPU clamp `max(new,last+1)`, `TSC_DEADLINE`→`CNTV_CVAL_EL0` inside VM, plus kvmclock ref page for wall-clock correctness (`10 §2.3–2.5`).

**Status.** Mitigated — design in `10-interrupt-timer-device-dossier.md` §2; Tier 2 keeps LAPIC timer inside VM via `CNTV_CVAL_EL0`.

---

## N-05 — ARM instruction cache is not coherent with data cache; x86's effectively is

**What.** x86 guarantees that a store to an instruction's bytes is observed by subsequent
execution of that instruction with only a serializing instruction required (and for
cross-modifying code, an IPI + serialize). ARM requires explicit `DC CVAU` + `IC IVAU` + `DSB` +
`ISB` (`V`, architectural).

**Why it bites.** This is *not* the same problem as code-cache invalidation, but it compounds it.
Every guest write that lands on a page containing translated code must invalidate our translated
blocks; guest kernels do this constantly (module loading, `text_poke`/alternatives, ftrace,
eBPF JIT, static keys). At 16 KB write-protect granularity (N-02) the trap rate could be brutal.

**Mitigation sketch.** This is the single best paravirtualization opportunity: a modified guest
kernel already calls `flush_icache_range()` on non-x86 architectures, so an x86 guest kernel
patched to call a hypercall at every code-patching site gives us exact, cheap invalidation with
no write-protection at all (`I`).

**Status.** Open — flagged as a top-tier PV win.

---

## N-06 — TSO does not solve unaligned atomics or `CMPXCHG16B`

**What.** Apple's TSO mode changes *ordering*, not *atomicity granularity* (`R`). x86 allows a
`LOCK`ed RMW to straddle a cache line ("split lock") and guarantees atomicity; ARM does not.
x86 `CMPXCHG16B` needs a 16-byte atomic CAS, which ARM provides only via `FEAT_LSE2`/`CASP` with
alignment requirements.

**Why it bites.** Correct emulation of misaligned `LOCK` operations requires a fallback — FEX
uses a global lock / signal-based backpatching scheme. Rare but correctness-critical; a guest
*kernel* hitting this is a hang, not a slow path.

**Status.** Open — solvable, cost is a rare slow path. `hw.optional.arm.FEAT_LSE2 = 1` here (`V`).

---

## N-07 — macOS W^X: JIT pages cannot be simultaneously writable and executable

**What.** Hardened-runtime processes must use `MAP_JIT` plus per-thread
`pthread_jit_write_protect_np()` toggling; the classic dual RW/RX alias via `mach_vm_remap` is
restricted (`R`, current status needs verification on macOS 26).

**Why it bites.** A translator writes to its code cache constantly. Every write burst needs a
mode toggle, and the toggle is per-thread state — it interacts badly with multi-threaded
translation and with signal handlers that run on the same thread.

**Note.** Inside the helper VM (Design B) this constraint **disappears entirely** — we own the
stage-1 tables and can map our code cache RWX. Another point for Design B (`I`).

**Status.** Open.

---

## N-08 — Entitlement coupling: hypervisor + JIT + distribution

**What.** `com.apple.security.hypervisor` and `com.apple.security.cs.allow-jit` are separate
entitlements with separate signing requirements (`R`).

**Why it bites.** Design A needs JIT. Design B needs hypervisor (and JIT only *inside* the VM,
where macOS's rules don't apply). Whether both can be held at once, and whether a
non-paid-account developer can sign locally, determines whether this project is even runnable on
a dev machine, let alone distributable.

**Status.** Resolved in 02 §5: `com.apple.security.hypervisor` is **not restricted** — ad-hoc signing `codesign -s - --entitlements` suffices (02 §5.1 MERGED live on M2 Pro), no paid account, no Hardened Runtime, no SIP disable. `HV_DENIED` only without it. `allow-jit` (for Tier 1 host JIT) is similarly ad-hoc; inside a helper VM (Tier 2) W^X is via stage-1 tables and needs no entitlement at all. See also `10-interrupt-timer-device-dossier.md` §4.

---

## N-09 — Precise exceptions are the tax on every optimization

**What.** x86 faults are precise: at a page fault, every architectural register must hold its
pre-instruction value and the faulting instruction must be restartable.

**Why it bites.** This is the constraint that kills aggressive optimization. Lazy flag
evaluation, register allocation across basic blocks, reordering, and instruction fusion all have
to be undoable at any memory-touching instruction. Rosetta 2 and FEX sidestep much of this
because a userspace segfault usually means "the process dies"; a *system* emulator takes page
faults as the normal, hot path — every demand-paged guest page, every COW fault, every swap-in.

**Why it matters here specifically.** In full-system emulation, page faults are not an error
path, they are the mechanism. The cost of maintaining restartability is paid on the hot path.
This is arguably the deepest reason why system-level DBT has historically been much slower than
user-level DBT, and it is not fixed by TSO or by hardware MMU reuse.

**Status.** Open — needs a concrete answer in the design (checkpoint/replay vs. conservative
state materialization at every potentially-faulting instruction).

---

## N-10 — "Reuse existing components" and "better than QEMU" are in tension

**What.** Design observation, not a fact.

**Why it bites.** QEMU's TCG is slow largely *because* of its portable, retargetable design:
softmmu on every access, an IR that discards x86 flag semantics into explicit computation, no
cross-block register allocation. FEX and Rosetta are fast largely because they are narrow —
one guest, one host, one privilege level, userspace only. Reusing FEX's fast core means
retrofitting a system-level memory and privilege model into a design that assumes neither;
reusing QEMU's system model means inheriting the thing that is slow. The seam between "reused
fast translator" and "reused correct system model" is where this project's real engineering risk
lives.

**Status.** Open — the central architecture question. To be settled in the design doc.
