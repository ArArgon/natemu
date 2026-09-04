# BP-01-13-start — guest machine contract breadth pass

## Metadata

- Breadth-pass ID: `BP-01-13-start`
- Question: `Q-01-13`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_machine_author`

This pass organizes questions and establishes no evidence or conclusion.

## Coverage

- **Product assumptions:** a device-class name (`Q-01-09`'s "network",
  "block", and so on) does not by itself define a bootable, driver-satisfiable
  machine; `Q-01-13` must define the machine-contract dimensions a stock,
  unmodified Linux kernel (per `Q-00-02`'s pinned candidates) actually
  consumes at direct-boot time: virtio transport (mmio versus pci), guest
  discovery of that transport (kernel command line `virtio_mmio.device=`
  versus ACPI/DT enumeration), ACPI table subset (`RSDP`/`XSDT`/`FADT`/`MADT`/
  `DSDT`) versus a no-ACPI path, xAPIC versus x2APIC mode, timer sources
  (LAPIC timer, TSC deadline, HPET absence, PIT/ACPI PM timer presence),
  UART (8250 at `0x3F8`) versus virtio-console, reset/shutdown mechanism (ACPI
  S5 via PM1a, keyboard-controller reset, triple fault), and the e820 memory
  map. This is candidate-option analysis, not a production design; `Q-01-13`'s
  own resolution method is explicit that no production machine is chosen here
  (Track 03 selects one).
- **Adjacent systems and alternatives:** QEMU's `microvm` machine type and
  Firecracker's device model are existing systems that boot stock Linux with
  virtio-mmio and a minimal or absent ACPI table set — useful adjacent
  precedent that a no-ACPI or reduced-ACPI path is not purely theoretical, but
  citing their behavior is not a substitute for the primary Virtio/ACPI/Linux
  specifications the brief requires; any reference to them in Phase B must be
  clearly non-load-bearing or backed by its own primary source. The competing
  alternative — full virtio-pci with a complete-enough ACPI table set for PCI
  enumeration and interrupt routing — is the other admissible option set; the
  pass must not assume MMIO-without-ACPI is strictly simpler once PCI's own
  ACPI/MSI dependency chain is accounted for (see negative/boundary cases).
- **Negative and boundary cases:** virtio-pci enumeration and interrupt
  routing on real x86 hardware normally depends on ACPI (MCFG for PCI
  Express config space, `_PRT`/MADT for legacy INTx or MSI routing), so a
  "virtio-pci without ACPI" combination may not be a coherent option at all;
  Phase B's decision-point table must record whether each transport is
  actually severable from the ACPI-table-subset dimension or whether the two
  dimensions are coupled. A second boundary case: SMBIOS/DMI tables are
  separate from the ACPI table set the brief enumerates, and stock
  distribution userspace (`dmidecode`, `systemd-detect-virt`, and other tools
  some workloads may invoke) can read them; their absence under a no-firmware
  direct-boot path is expected non-fatal for kernel boot but is a machine-
  contract completeness question in its own right. Because `Q-01-13`'s own
  applicability dimensions already include "device feature / host API"
  broadly, this SMBIOS/DMI gap is folded into this question's own scope for
  Phase B rather than filed as a new backlog question (see Backlog updates).
  A third boundary case: without a hardware RTC/CMOS device, a direct-boot
  guest has no wall-clock time source independent of a boot-time value passed
  by the loader; whether that is acceptable for the accepted workload set is
  also folded into this question's scope, distinct from `Q-01-07`'s host-side
  timer-resolution/latency question.
- **Host and guest matrix:** every specification citation must pin an exact
  version consistent with `Q-00-02`'s pinned candidates — Linux 6.12.108 and
  6.18.49, OASIS Virtio 1.4 CS01 (already cited by `INV-00-02` as `SRC-028`,
  reusable by ID), ACPI 6.6 (already cited as `SRC-029`) — so a Phase B claim
  citing Linux 6.12 docs must not silently generalize to 6.18 behavior or vice
  versa without checking both. `INV-00-02`'s "Resolution" section already
  assigns `Q-01-13` the job of selecting "the Virtio transport and negotiated
  features, ACPI/APIC table subset, discovery path, timers, reset/shutdown
  behavior, and verify[ing] required stock drivers" — that is the Track 00
  residual this question inherits and Phase B's source-analysis table must
  visibly answer each item as a candidate-option set. No Observed claim is
  possible here since Track 02 owns boot evidence; every claim is Source-backed
  or Inferred from specifications.
- **Multicore interactions:** MADT (part of the ACPI table set) is the
  standard x86 mechanism for per-CPU Local APIC/x2APIC enumeration and CPU
  bring-up ordering; if a no-ACPI machine-contract option is on the table, the
  pass must ask how a direct-boot Linux guest would learn vCPU count and APIC
  IDs at all without MADT, since x86 Linux does not consume a device tree for
  CPU topology the way Arm does — this is a genuine coupling between the
  ACPI-table-subset dimension and SMP bring-up (`Q-01-08`/`KA-06`) that Phase
  B's decision table must record rather than leave implicit. xAPIC-versus-
  x2APIC mode is itself an MADT-encoded choice (separate Local APIC and Local
  x2APIC structure types), so this dimension and the ACPI dimension are not
  independent either.
- **Falsifiers and stop paths:** this question is the direct test for
  `KA-12` ("A guest machine contract satisfiable by stock Linux drivers
  exists for the device set without firmware (direct boot)"); its row states
  the resolution is source analysis with boot evidence deferred to Track 02,
  and a false finding routes to project stop input 3. `Q-01-13` is
  prerequisite to `Q-01-09` and `Q-02-03` per the existing backlog
  dependencies, and depends in turn on `Q-01-09`'s device-backing
  admissibility findings and `Q-01-07`'s timer-mechanism findings — a device
  backing `Q-01-09` finds inadmissible cannot appear in an admissible machine-
  contract option set here. A falsifier specific to this question: if every
  candidate option set requires a Linux driver or kernel feature that is a
  loadable module absent from a stock distribution's default initramfs (per
  the Kconfig audit the brief requires), that option set is inadmissible
  without a boundary-crossing initramfs modification, which would itself
  contradict `Q-00-02`'s "unmodified" definition.
- **Measurement failure modes:** the reviewer standard (`REV-032`) recomputes
  digests and re-extracts cited text and rejects any claim whose cited section
  does not literally support it; because this investigation is source-analysis
  only, its principal measurement-failure risk is citation drift — citing a
  section that discusses a related but distinct mechanism (for example citing
  a general PCI enumeration section for an MSI-specific routing claim), or
  citing Linux kernel-parameters documentation for a default that a specific
  distribution's build actually overrides via its own kernel config. A second
  failure mode: OASIS Virtio 1.4 CS01 section numbers must be re-verified
  against the specific captured revision rather than a remembered or cached
  numbering, since normative documents are revised between committee
  specifications.

## Backlog updates

no new issues found
