# Architecture candidates — speculation, not locked

Speculation from design discussions on 2026-09-05/06. Not evidence, not
decisions, not requirements. Nothing here is locked. The test-program author
may start from any of it or none of it; the architecture-options track owns
every choice. Do not cite this file as support for a claim. This directory is
outside the review workflow.

## Shadow paging candidates

- Lazy: react to the guest's announcements only (INVLPG, CR3/CR4 writes,
  shootdown IPIs); fill the shadow on fault.
- Eager: write-protect guest page-table pages; take a fault per guest write.
- Eager with apply-at-announcement: the fault records the change; the Arm
  tables are updated at the announcement with break-before-make and one
  broadcast TLBI per batch.
- Adaptive retranslation of hot table-writing sites so they call the shim
  instead of faulting.
- Soundness note: the shadow behaves as a large translation cache, which x86
  permits to stay stale until announced; an absent-to-present change needs no
  announcement because the shadow has no entry and fills fresh on first touch.
- Data structures: root cache (guest CR3 to shadow root), back-map (shadow
  table to guest table page), reverse map (physical page to every shadow entry
  mapping it), pending-change list.
- Share the kernel-half subtree across address spaces.
- Accessed/dirty bits set by the shim at fill and first write (shadow entry
  read-only until first write); hardware management, if present per
  generation, is an optimization.
- CPUID levers: advertise not-vulnerable-to-Meltdown so the guest disables
  page-table isolation; do not advertise PCID; mirror 2 MiB and 1 GiB pages one
  to one; optional KVM-signature paravirtual features later.

## Address-space layout candidates

- The x86 non-canonical gaps inside Arm's 48-bit ranges (128 TiB per half) can
  hold the shim and code cache; five-level x86 paging is out of scope via
  CPUID.
- (i) Both guest halves at EL0 with the shim alone at EL1; shim pages EL1-only
  so a guest access into the gap faults into a synthesized #GP; two shadow
  views per address space switched on ring transitions.
- (ii) Guest kernel at EL1 beside the shim; one shadow per address space; the
  shim is protected only by the guest never forming a gap address; the
  instance is still contained by stage-2.
- (iii) Guest EL2 where the hardware offers it (`hv_vm_config_get_el2_supported`).

## Code-page handling candidates

- Write-protect pages holding translations; a guest write faults into the
  shim; the writing vCPU's block ends immediately (x86 self-modifying-code
  contract: the next instruction sees the change); other vCPUs drop stale
  translations lazily at their next interrupt or serializing instruction via a
  per-vCPU generation counter (x86 cross-modifying-code contract; the Intel SDM
  protocol and Linux int3 patching rely on it).
- Two-state per-page scheme: "writable" (writable per guest, never executable,
  translations untrusted) and "code" (read-only regardless of guest,
  translations trusted); writable-to-code on an execute fault, code-to-writable
  on a write fault; each flip is a broadcast TLBI; pages that flip too often
  escalate.
- Execute-fault linking: all guest pages no-execute in the shadow; translated
  code may jump to a raw guest address; the execute fault is the linker and JIT
  trigger for cold edges and inline-cache misses; hot indirect branches keep a
  software inline cache.
- Escalation rungs: sub-page translated-range tracking; adaptive retranslation
  of emitter store sites with an inline range check; verify-on-entry
  translations with blocks ending at stores for pages that keep flipping.
- Common needs: state and protection per physical page across all virtual
  aliases (the kernel patches text through an alias); fault classification by
  walking the guest table; one per-physical-page metadata table shared with
  shadow paging (state, flip counter, translated-range bitmap, translation
  list, page-table-page flag, reverse map), in shared memory because the host
  translator registers translations into it.
- Real-world frequency: RWX pages are common among server JITs; same-page
  write-and-execute is common during JIT warm-up and kernel boot and rare in
  steady state; truly self-modifying hot code is rare.

## Interrupt injection candidates

- Per-block poll (proven by QEMU) versus a forced boundary using the fault
  metadata.
- Timer as a deadline on the Arm compare register.
- Halted vCPUs wake through the host: the framework traps WFI and the host
  thread sleeps until the timer or a kick.
- Host device interrupts via the virtual GIC's SPI/MSI API.

## Secondary CPU start notes

Kernel side, source-walked 2026-09-06, to be re-cited properly in the record:
`X86_ACPI_MADT_WAKEUP` is `def_bool y` on `X86_64 && ACPI && SMP &&
X86_LOCAL_APIC`, so every 64-bit SMP kernel since the feature landed
(mid-2022; exact version to cite) has it. `acpi_parse_mp_wake` registers the
mailbox wakeup unconditionally when the MADT structure is present. The OS
writes the APIC ID and wakeup vector, release-stores command = 1, and spins
without timeout until firmware clears it. The AP arrives "on a direct transfer
from 64-bit BIOS with identity mapped page tables" and immediately loads its
own IDT, GDT and CR3. Version 0 of the structure forbids CPU offlining, so
present version 1. The exact AP register state is in the TDX GHCI
specification §4.1 that the ACPI text defers to.

Test shape for Track 01: another vCPU plays the OS role, since no translator
exists to run a real kernel. Windows is not confirmed to consume the mailbox
natively (Azure TDX Windows guests sit above a paravisor that consumes it);
Windows is analysis-only anyway.

The shim is the "firmware": it publishes ACPI tables (RSDP, XSDT, MADT with
LAPIC entries and a version-1 multiprocessor-wakeup structure, FADT, minimal
DSDT), reserves the 4 KiB mailbox in the memory map, keeps an identity-mapped
page-table set for AP entry, starts a vCPU thread at the wakeup vector with
the specified initial state, then clears the command field. Detecting the OS's
command store: poll from a host thread, or write-protect the mailbox page in
the shadow so the release-store faults into the shim. Real-mode INIT-SIPI-SIPI
is never needed for kernels with the mailbox path; a trampoline interpreter is
the fallback only if a target kernel predates it.

## Host-published code notes

- Publication ordering: release store by the host, acquire load by the vCPU.
- Cache maintenance split: the host cleans the data cache; the shim invalidates
  the instruction cache and issues ISB using guest addresses, in the dispatcher
  before first entry.
- Branch patching for block chaining is allowed under Arm's
  concurrent-modification rules for branch instructions.
- Request paths: hypercall (one VM exit per new block) versus a shared queue
  with an MSI wakeup.
- A possible in-VM cold-code tier if round trips dominate boot.

## Translator requirements collected for `Q-03-02`

Expose the pages each translation unit touches; accept invalidation by
physical page; support sub-page ranges; keep interrupt polls and fault metadata
at internal block boundaries in multi-block units; whether units may cross
page boundaries is a measurement; a per-instruction metadata format for
precise state; support re-emitting one instruction's translation on request.

## System-mode adaptation: three shapes

Comparison belongs to `Q-03-02`; an effort stop rule belongs to `Q-06-06`.

- Shape 1, user-mode translator plus shim semantics. Hardware paging removes
  the memory-path change (direct loads and stores are already emitted). The
  remaining list: privileged instructions routed to shim calls;
  translator-raised exceptions routed to shim delivery; the per-CPU base swap
  as a call-out; an interrupt poll; physical-page keying of the code cache (a
  crude first cut is one translation context per guest address space); 32-bit
  compatibility mode if in scope. The system-semantics library for the shim
  can be ported from QEMU's x86 kernel-mode helpers (plain C over a CPU state
  struct; license is not an obstacle with no distribution), with Bochs as a
  second model to check against.
- Shape 2, the QEMU translator wholesale with its software MMU deleted. A
  complete x86 system front end, arm64 back end, precise recovery,
  block-boundary interrupt checks, physical-page invalidation and device
  models already exist; switch memory operations to the direct form of its
  user-mode builds and turn TSO on. Helper-call-heavy generated code implies
  the translator lives inside the VM rather than on the host. Possibly the
  cheapest route to a first Linux boot; code quality can be upgraded later if
  measurement says so.
- Shape 3, an interpreter for kernel mode plus a translator for user mode at
  first. Kernel time is a minority of most workloads; first boot arrives with
  bounded new code; kernel-mode translation is added class by class from
  profiles.
- Cross-cutting: hide PCID, LA57, MONITOR/MWAIT, TSX, CET/SMAP-class features
  as needed, MCE, PMU and XSAVES via CPUID so their instruction and MSR
  families need no implementation; a microvm-style machine with no legacy
  timers or PIC.
- Effort stop rule: if adaptation to a first single-vCPU Linux shell exceeds a
  stated budget, that is a stop signal, recorded before adaptation starts.
