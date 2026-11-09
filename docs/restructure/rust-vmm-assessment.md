# How much natemu could use or contribute to rust-vmm

November 7, 2026. **Research-only assessment**, requested out of curiosity.
This proposes neither a migration nor an upstream submission. No runtime,
dependency or build changes were made for this assessment. The separately
authorized queue/transport evaluation owns its own results.

## Overall assessment

**Inferred:** natemu could use rust-vmm for selected host-side infrastructure,
especially new virtio I/O. Its central achievement—the translated x86 machine,
resident runtime and coherence protocol—would remain natemu. A percentage of
code migrated would obscure the distinction between replacing useful small
modules and replacing the architecture.

**Source-backed:** rust-vmm is a collection of reusable virtualization crates,
not a single VMM into which natemu must fit. Its [current crate inventory][catalog]
contains memory, virtio, device dispatch, ACPI, allocators, loaders and OS-specific
utilities. This permits gradual adoption with no requirement to import its
KVM execution model. For the immediate I/O milestone, use the
[reuse survey](io-rust-reuse.md); broader migration has no demonstrated payoff.

## Adoption map

Current natemu source paths identify the behavior to preserve. Upstream contents
are source-backed; suitability and priority are engineering inferences.

| Natemu responsibility | Possible rust-vmm reuse | Assessment |
| --- | --- | --- |
| New virtio transport and queues; existing [device registry](../../crates/host/src/devices.rs) | `virtio-queue`, `virtio-bindings`, selected `virtio-device` helpers | Strongest fit. Useful work is still ahead, so reuse can prevent duplication. Keep natemu's MMIO completion, resource limits, reset and IRQ ownership; the [completed exploratory probe](io0-rust-vmm-evaluation.md) identifies concrete gaps. |
| [RAM backing](../../crates/host/src/memory.rs), [guest access](../../crates/abi/src/guest_memory.rs) | [`vm-memory` access traits][memory] | A host-side adapter is plausible. Full allocation migration is optional and low priority. Preserve separate guest RAM/private runtime ranges, holes, aliases and backing lifetime; generic dirty tracking cannot replace code/PTE coherence. |
| [HVF VM/vCPU wrapper](../../crates/host/src/vm.rs) | No HVF replacement in the inspected catalog; [`kvm-ioctls`][kvm] is specifically KVM | Retain. The current thread-affine vCPU ownership, GIC serialization and destroy order are required behavior. A Rust ecosystem dependency does not turn Apple HVF into KVM. |
| [Resident execution](../../crates/runtime/src/execution.rs), [shadow tables](../../crates/runtime/src/tables.rs), [code cache](../../crates/runtime/src/cache.rs), [FEX admission](../../crates/host/src/admission.rs) | No corresponding cross-ISA execution engine in the inspected catalog | Retain. Guest x86 semantics, fault/effect recovery, Arm gateways, translated-source invalidation and FEX adaptations are the specialized emulator. Ordinary VMM crates do not discharge those contracts. |
| [PIC](../../crates/host/src/pic.rs), [I/O APIC](../../crates/host/src/ioapic.rs), [LAPIC](../../crates/runtime/src/execution/lapic.rs), PIT/RTC/PM and [clock](../../crates/host/src/clock.rs) | `vm-superio` covers a narrower/different device set; KVM irqchip interfaces are not software models | Retain the current platform. There is no inspected ready replacement for this coordinated PC chipset and x86 interrupt-delivery implementation. |
| [UART](../../crates/host/src/uart.rs) | [`vm-superio::Serial`][superio] | Technically adjacent, but not behavior-equivalent: upstream documents a 64-byte always-enabled RX FIFO and no FCR emulation; natemu has a 16-byte FIFO, FCR and character-time behavior. A swap needs compatibility tests and offers no immediate I/O milestone benefit. |
| [Linux direct loader](../../crates/host/src/boot.rs) | [`linux-loader`][loader] | Requires adaptation: the pinned source gates bzImage and x86 boot parameter types on Rust's host `target_arch = x86/x86_64`. Natemu loads an x86 guest on an Arm host. Initial x86 state, machine layout and AP startup remain natemu even after parser reuse. |
| [ACPI generation](../../crates/host/src/boot/acpi.rs) | [`acpi_tables`][acpi] | Plausible later consolidation of AML/table construction. It supplies builders, not natemu's topology, resource assignments, PM policy or AP mailbox contract. Check required structures and preserve independent table/boot checks. |
| Device dispatch and fixed resource map | [`vm-device`][device], [`vm-allocator`][allocator] | Possible, low priority. They overlap working local registration/range checks. Preserve port-width splitting, unassigned-read policy, synchronous refusal and per-vCPU context; an adapter can otherwise become more code than the registry it replaces. |
| [vCPU supervision](../../crates/host/src/owners.rs), timer threads, wakeups | [`event-manager`][events], pieces of `vmm-sys-util` | Not an unchanged Darwin substitution: the event manager imports epoll directly. Retain supervision, halt liveness, cancellation and join-before-destroy rules. Portable individual utilities can be assessed when a real consumer needs them. |
| [Standing suite](../../tools/run_suite.py), [boot/device models](../../tools/models) | Upstream unit/fuzz/model tests supplement natemu's checks | Retain integration oracles. Upstream queue tests cannot establish translated x86 ordering, precise effects, SMP wakeups or DMA/code coherence. Import applicable tests with provenance; do not replace end-to-end coverage with dependency test counts. |

`vm-superio`'s RTC is **ARM PL031**, not natemu's PC CMOS/MC146818 register model.
Matching the word “RTC” is insufficient grounds for adoption. [Upstream device scope][superio]
also lists a minimal i8042; natemu does not need to add that device merely to use
other crates.

## Main hazards

1. **Giving reusable code too much memory authority.** A valid GPA or a volatile
   pointer is not permission to write guest RAM while generated code runs.
   Preserve admitted snapshots/commits, pending-translation checks and payload →
   status/ring → IRQ ordering. `vm-memory`'s memory-map snapshot is not a snapshot
   of guest data. See [its contract][memory] and the [I/O survey](io-rust-reuse.md).
2. **Importing host/guest equivalence assumptions.** The loader's architecture
   guards are a concrete example. KVM/eventfd/epoll assumptions also arise in
   adjoining crates. Check each selected feature and target rather than treating
   all “Rust VMM” code as portable.
3. **Changing an already-tested guest machine.** UART FIFO behavior, PM shutdown,
   RTC/timer relationships, IRQ routing and boot tables are observable. A library
   migration must preserve them or explicitly propose a compatibility change.
4. **Splitting ownership between old and new implementations.** Two device buses,
   two RAM owners or two reset authorities can create stale callbacks and missed
   invalidation. Introduce one narrow adapter at a time and remove displaced
   responsibility only after its tests cross the new interface.
5. **Maintenance and policy cost.** Host-library dependencies change natemu's
   [current policy](../../Cargo.toml). Keep the resident and shared ABI free of
   `std`-based device dependencies. Pin compatible crate versions and features;
   retain notices and account for local patches and upstream release changes.

## Contributing natemu work upstream is a separate question

**Inferred candidates, in descending near-term practicality:**

- Small queue/MMIO conformance fixes, portable tests and Darwin compatibility
  changes found by using existing crates. These have a concrete upstream consumer
  and can be submitted independently of natemu's execution engine.
- Cross-architecture Linux loading: separating guest image format from host
  architecture could benefit other emulator/tool users. A proposal must cover
  boot structures, endian/layout assumptions and tests; removing one `cfg` alone
  would not establish correctness.
- A reusable HVF wrapper with documented vCPU thread affinity, mapping lifetime,
  GIC concurrency and Darwin tests. Natemu's wrapper is a useful starting point,
  but currently includes its particular layout and doorbell policy. Extract only
  with a clear second consumer and scope; no upstream interest is assumed.
- Independently usable PC device state machines and deterministic tests, where
  their clock/IRQ interfaces can be separated cleanly. **Provenance must be
  reviewed first:** several natemu model headers explicitly describe QEMU
  transcriptions. Rust source language alone does not establish eligibility for
  a new project's license terms. This is an unresolved packaging concern, not
  a legal conclusion or a proposed relicensing.

The resident, FEX integration and all-writer coherence engine are poor first
extraction targets: their useful behavior depends on the rest of natemu. Keeping
them in natemu does not prevent consuming rust-vmm crates.

**Source-backed process:** rust-vmm asks new-component proposals to explain use
by other VMMs and discusses scope/design before accepting the component. Existing
crates also require documentation, tests and CI. This makes contributions
plausible, not guaranteed. [Community contribution guidance][community]
No maintainer was contacted and no issue or pull request was created.

## Evidence boundary

Monorepo files were read from root's retained checkout at
`671e367124cf7717d832b21de86b62eccf33ac27`; links below name that revision.
`vm-superio` and community guidance were inspected through their primary web
pages and remain moving references. This assessment performed no build, migration,
behavioral comparison or performance measurement. It does not inherit successful
execution from source availability; the isolated queue probe reports separately.

[catalog]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/README.md
[memory]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-memory/src/guest_memory.rs
[kvm]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/kvm/kvm-ioctls/src/lib.rs
[superio]: https://github.com/rust-vmm/vm-superio#serial-console
[loader]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/linux-loader/src/loader/mod.rs
[acpi]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/acpi_tables/README.md
[device]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-device/README.md
[allocator]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/vm-allocator/README.md
[events]: https://github.com/rust-vmm/rust-vmm/blob/671e367124cf7717d832b21de86b62eccf33ac27/event-manager/src/lib.rs
[community]: https://github.com/rust-vmm/community#adding-a-new-virtualization-component
