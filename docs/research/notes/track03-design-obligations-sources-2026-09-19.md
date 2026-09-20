# Track 03 design obligations — source audit, September 19, 2026

Candidate note by `/root/external_research`, outside the accepted-evidence
workflow. Source-only audit; no experiments or archived-history access. Read
current workflow/feasibility charter in this context and current scope/Track 03.
This identifies requirements and interactions, not their satisfaction or a
preferred implementation. Moving upstream pages were inspected on this date;
pin their relevant versions before implementation or evidence campaigns.

## Major obligations and design consequences

1. **CPU profile, floating point and recovery form one contract.**
   **Source-backed:** Intel specifies CPUID qualification of CR4, XCR0 state
   components and AVX enablement; YMM management uses XSAVE. SIMD exception
   delivery depends on architectural control state. See [SDM Vol. 3A,
   §§2.5–2.6 and system programming for extended states](https://www.intel.com/content/dam/www/public/us/en/documents/manuals/64-ia-32-architectures-software-developer-system-programming-manual-325384.pdf),
   [Vol. 1, §§11.5.1 and 15.9–15.11](https://cdrdv2-public.intel.com/671436/253665-sdm-vol-1.pdf),
   and [Intel floating-point reference](https://www.intel.com/content/www/us/en/developer/articles/technical/floating-point-reference-sheet-for-intel-architecture.html).
   **Inferred:** Auditing a translator requires x87 precision/control/status,
   MXCSR rounding/exception state and denormals, state save/restore, privileged
   enabling, and complete recoverable state at faults and interrupts—not only
   AVX2 opcode coverage. Preserve the owner policy of inheriting FP machinery;
   record any inherited mismatch as a compatibility or adaptation decision.
   Unexposed optional extensions do not become requirements.

2. **Atomicity is separate from memory ordering.**
   **Source-backed:** [Intel SDM Vol. 3A, Locked Atomic Operations,
   especially bus locking](https://cdrdv2-public.intel.com/835754/253668-sdm-vol-3a.pdf)
   distinguishes ordinary access atomicity from LOCK; alignment does not by
   itself remove LOCK semantics. The same manual defines optional controls
   that fault split locks. **Inferred:** TSO does not make an arbitrary
   multi-instruction read-modify-write indivisible. The design needs a policy
   for supported misaligned/cache-line/page-crossing atomics and interactions
   with ordinary accesses on other vCPUs. A software lock used only by atomic
   helpers cannot exclude ordinary guest accesses. Do not invent split-lock
   exceptions unless the selected CPU contract permits them. Faulting wide
   memory operations also need instruction-specific partial-effect analysis,
   not a blanket assumption that every fault rolls back every write.

3. **Memory mutation has several producers.**
   **Source-backed:** [Virtio 1.3, §§2.7–2.8 and §5.2](https://docs.oasis-open.org/virtio/virtio/v1.3/virtio-v1.3.html)
   describes device reads/writes to guest buffers, publication of used buffers,
   notification ordering, negotiated features and reset state. **Inferred:**
   The memory design must account for CPU stores, device/backend writes,
   translator/shim metadata updates and initialization. A device write to RAM
   holding translated guest instructions or guest page tables must reach the
   relevant coherence policy; guest-CPU write protection alone does not
   establish that. Completion must not precede required guest visibility.
   Descriptor addresses, lengths, chains and lifetimes are a host trust
   boundary; stale completions after reset/teardown must not hit reused memory.

4. **Storage success must mean the advertised thing.**
   **Source-backed:** Virtio 1.3 §5.2.6.2 makes stable-write/flush completion
   depend on negotiated cache features and requires persistence where its
   stated conditions apply. **Inferred:** Backend caching, flush/error
   propagation, reset and shutdown must preserve that advertised contract.
   Merely copying bytes to host memory or completing an asynchronous write
   cannot automatically stand for persistent completion. Do not silently add
   stronger crash-durability promises than scope requires.

5. **Invalidation is more than removing a lookup entry.**
   **Source-backed:** [QEMU multi-thread TCG, Translation Blocks and Memory
   maps/TLBs](https://www.qemu.org/docs/master/devel/multi-thread-tcg.html)
   describes reversing direct links, updating caches, quiescing vCPUs for
   global changes, and synchronizing cross-vCPU TLB flushes.
   [Translator internals](https://www.qemu.org/docs/master/devel/tcg.html)
   describes CPU-state-dependent lookup and per-instruction recovery metadata.
   **Inferred:** Own guest-physical identity/aliases, code version and CPU mode
   in block validity; prevent stale links, return PCs or fault metadata from
   reaching reclaimed/reused code. Address-space tag reuse and page-table
   retirement need similarly explicit lifetime rules. These are established
   emulator concerns; the shim-specific mechanism and cost remain open.

6. **Boot, topology and CPU lifecycle must agree.**
   **Source-backed:** [Linux 6.12, 64-bit Boot Protocol](https://docs.kernel.org/6.12/arch/x86/boot.html)
   defines register/segment/mapping and boot-parameter entry conditions.
   [ACPI 6.5 §5.2.12.19](https://uefi.org/sites/default/files/resources/ACPI_Spec_6_5_Aug29.pdf)
   specifies mailbox version 0; [ACPI 6.6 §5.2.12.19](https://uefi.org/specs/ACPI/6.6/05_ACPI_Software_Programming_Model.html)
   specifies version 1 with reset support.
   **Inferred:** CPU IDs/topology, MADT, mailbox memory ownership and initial
   state must be consistent. Preserve the already recorded startup32 path and
   kernel feature floor. Do not infer stock-image compatibility from a
   specification entry path or require optional hotplug/reset extensions merely
   because newer ACPI describes them; state lifecycle limits explicitly.

7. **Interrupt delivery includes progress and ownership.**
   **Source-backed:** QEMU translator internals requires interrupt rechecking
   after CPU changes that unmask interrupts. Apple's
   [pending-interrupt API](https://developer.apple.com/documentation/hypervisor/hv_vcpu_set_pending_interrupt%28_%3A_%3A_%3A%29)
   requires the owning thread; [vCPU management](https://developer.apple.com/documentation/hypervisor/vcpu-management)
   identifies `hv_vcpus_exit` as the cross-thread exception.
   **Inferred:** Design the producer-to-owner handoff and pending/acknowledged
   state across running, halted, masked, shim and host-wait states; closing the
   check-to-sleep race and bounded interruptible work are explicit obligations.
   Do not assume WFI automatically produces a host exit. A host API for forcing
   an exit does not establish x86 APIC semantics or a latency guarantee.

8. **Clock coherence is independent of timestamp arithmetic.**
   **Source-backed:** [Linux x86 virtualization timekeeping, §§3.9 and
   4.1–4.3](https://docs.kernel.org/virt/kvm/x86/timekeeping.html) distinguishes
   TSC features, cross-CPU consistency, serialized observations, delayed
   interrupts and derived-clock conversion problems.
   **Inferred:** A selected counter scaling needs coherent frequency/offset
   reporting, per-vCPU reads, implemented TSC writes, timer programming and
   cancellation, and defined pause/preemption/sleep behavior. Host scheduling
   delay is shared with ordinary VMs; it is not a new platform-feasibility
   question. Migration and real-time guarantees are not implied requirements.

## Disposition

**Inferred:** The largest likely omissions in a translator/MMU/device split are
device-write coherence, atomic fallback versus ordinary accesses, code/metadata
reclamation, full FP/extended-state recovery, and notification-to-sleep progress.
Give each an owner and acceptance boundary in the design comparison. Existing
Track 01 findings support their exact fixtures; they do not discharge these
integration requirements. No new empirical result is claimed here.

Bounded next step: cross-check the proposed design-decision list against these
eight obligations and file only gaps lacking an owner. Stop after ownership and
scope are explicit; no new execution campaign is authorized by this note.
