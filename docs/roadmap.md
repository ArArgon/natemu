# natemu roadmap (tentative)

Drafted November 16, 2026, revised the same day. This is the owner's direction, not an
accepted plan: nothing here has an estimate beyond the rough ones quoted from existing
notes, and later milestones are deliberately vague. Order and scope are revisited at each
milestone's close. Only 64-bit guest operating systems are in scope; 32-bit support means
32-bit *programs* inside a 64-bit OS.

Guiding rules carried over: the finished product is OS-agnostic (justify behaviour by the
specs or real PC hardware, not one kernel); performance is a feature at every milestone,
not a final phase; each milestone closes on evidence (boots, test suite, benchmarks), not
on review convergence.

## Where we are

Stock Debian 13 cloud images boot and run real workloads on 1–N vCPUs: Hypervisor
framework VM with a small kernel-mode shim, FEX-based translation to Arm, PCI with MSI-X,
virtio disk/network/RNG, slirp networking with port forwarding, a CPU feature model
(x86-64-v3 guest, M2-class floor) and an exact-vs-fast floating-point switch (accurate
mode in progress). Faster than QEMU TCG on the benchmark set.

Two things now slow us down more than missing features:

- **Disk.** The scratch area outside the repo holds about 86 GB in roughly 950 run
  folders (one Debian snapshot alone is 15 GB); the machine's disk is at 95%.
- **Tests.** About 70 separate test runners under `tools/`, about 55 files hard-wired to
  that scratch area, no automated build/test service, and parallel agents queue on one
  VM lock.

## Priority order

M1 is the platform everything else stands on, so it gets the clear order below. The
items marked "parallel" touch different parts of the tree and can overlap.

| # | Work | Why now |
| --- | --- | --- |
| 1 | **Test and results hygiene** (parallel) | Stops the disk filling and the lock contention; every later item needs working tests. |
| 2 | **qcow2 disk images** | Directly attacks disk use; also needed by later milestones. |
| 3 | **Merge what is ready** (CPU features, floating-point fast mode) and finish accurate mode | Ready or nearly ready; unblocks a clean base. |
| 4 | **Settle configuration and device interface** (design only first) | Decide before the option count and device count grow with qcow2, USB, audio, GPU. |
| 5 | **M2: 32-bit programs** | First new capability after the platform is stable. |
| 6 | M3 UEFI, MU usability, M4 Windows | In order; see below. |

Anything in "Beyond" waits for the owner to schedule it.

### 1. Test and results hygiene

Goal: tests are cheap to run, results stay small, nothing depends on the scratch area
being a permanent store. The scratch area stays a scratchpad and ledger.

- **Results policy.** Keep a short summary per run (verdict, key numbers, how to
  reproduce) and delete bulky raw output after a fixed age or once a record cites the
  summary. Never keep a per-run disk image once the run ends; boot from a shared base.
  First action is a one-time prune of the current 86 GB, then enforce the policy.
- **Decouple the tests.** Test runners take their inputs and output folder from one
  place (already partly there: a single "raw root" setting) and keep their results
  outside the scratch area; runners that hard-code it are fixed one by one, not in a big
  rewrite.
- **Tiers.** A fast tier that runs on every change (minutes, at most one VM boot), the
  full suite once before merge (the current rule), and a long tier (benchmarks, large
  differential runs) only when a feature completes.
- **One job queue instead of a lock.** A single runner executes VM jobs one at a time
  and agents submit to it, so no agent spins waiting for the lock and a rerun after
  contention is not needed.
- **Build service.** GitHub's hosted macOS machines most likely cannot run the
  Hypervisor framework (nested virtualization), so assume a self-hosted runner on the
  owner's Mac for anything that boots a VM; hosted CI can still run the build and the
  non-VM tests. Verify before relying on it.

### 2. qcow2

Raw images are the main way disk use grows, so qcow2 comes forward from M3 into M1.

- **Scope for the first version:** qcow2 version 3, read and write, backing files,
  zero-cluster and discard support (the virtio disk already handles discard and
  write-zeroes). Not in v1: compression, encryption, internal snapshots.
- **Honest caveat to check first:** on this filesystem, raw images are already sparse
  files, so qcow2 alone saves less than hoped if the space is guest-written data. The
  big wins are (a) many small overlays on one shared base image, which is what the test
  tiers need, and (b) discard passing through so freed guest space is returned. Measure
  where the 86 GB actually goes before promising a number.
- **Disk-only snapshots** come almost free once backing files work. Whole-VM
  snapshots are not a priority.
- **VHD/VHDX** stays with Windows (M4), where Windows images need it.

### 4. Configuration and device interface (design first)

The owner is considering a code overhaul. To keep that productive rather than open-ended:

- **Rule:** an overhaul item is taken on only when it unblocks a milestone or removes a
  measured cost; each gets its own short plan and independent review, and the full
  suite must stay green throughout.
- **Configuration:** move to a written configuration file (TOML) with the command line
  as overrides once the option list is long enough to hurt. The decision is cheap now
  and expensive after USB, audio and GPU add options, so decide the format early even if
  conversion is gradual.
- **Device interface:** define one small way for a device to plug in (registers, memory
  mapping, interrupts, DMA, host backend), then move existing virtio devices onto it
  as they are touched, not all at once. Block backends (raw, qcow2) are the first test
  of it.
- **Multiple devices:** today the command line allows one disk, one NIC and one
  entropy device (the PCI bus itself has 32 slots; whether the virtio code copes with
  two of one kind is unchecked). The config describes devices as a list. Multi-disk,
  including read-only ISO disks, is built in M1 because M3 (installer ISO plus target
  disk) and M4 (Windows ISO, system disk, driver ISO) need it. Multi-NIC is in the
  config shape but built only when something needs it.
- **Architectural inefficiencies:** collect them as a measured list (the existing
  guest-code review already points at translation glue and exit paths) and schedule
  by payoff; no standing "cleanup phase".

## How priorities work

Every item in a milestone carries a tag:

- **P1:** required to close this milestone.
- **P2:** a later item depends on it. It need not finish inside this milestone, but it
  must land before the item that needs it starts. (Currently no item carries this tag;
  the dependencies are written into P1 items instead.)
- **P3:** optional: nothing depends on it. Free to move to another milestone or to MU,
  or to drop, without changing any exit criterion.

A milestone closes when its P1 items are done. P3 items are done when cheap, moved, or
left in MU. If a P3 item turns out to have a dependency, it is promoted to P1 or P2.

## M1 — 64-bit Linux, finished and a platform to build on

- **P1** Test and results hygiene (item 1), qcow2 (item 2).
- **P1** Merge the ready branches (CPU features, floating-point fast mode).
- **P1** Finish accurate floating-point mode (in progress): the exact behaviours hosts without Arm's alternate
  floating-point feature (AFP) need.
- **P1** Configuration format and device list (item 4), with multi-disk including
  read-only ISO disks (M3 and M4 need them).
- **P3** An accurate 80-bit x87 path (optional; fast mode keeps x87 in double precision).
- **P3** The deferred correctness pass: large-scale differential testing against real
  x86 behaviour, real workloads, re-checking decisions inferred from QEMU.
- **P3** Disk-only snapshots, which come with qcow2 backing files if that stays cheap.
- **P3** Multi-NIC built on the device list (the config shape is P1), IPv6 completion,
  lower idle latency, offloads where measurable.
- Debian stays the only guest here on purpose: it has no 32-bit code at boot, so it does
  not depend on M2. Other distributions come at or after M2.
- **Exit:** Debian boots from qcow2 and from raw; the fast test tier and the full suite
  run through the queue without manual lock handling; results stay under an agreed size
  budget; benchmark round recorded.

## M2 — 32-bit programs in a 64-bit Linux

Existing source assessment: about 4–8 engineer-weeks for a useful, tested profile; 1–2
weeks for a first demonstration.

- **P1** Compatibility-mode execution for 32-bit *user* programs, the only 32-bit code
  with a performance expectation (flat 32-bit code under the current 64-bit kernel
  and four-level paging), 32-bit syscalls and signals, thread-local storage, 32-bit
  floating point and SIMD.
- **P1** [Checkpoint C](restructure/m2-checkpoint-c-plan.md) covers mixed 32/64 user execution in one process,
  including far transfers between flat code segments and FS/GS limit checks in 32-bit code.
  Windows' 32-bit layer needs the mode-switch path. General non-flat default segments are deferred until a workload needs them.
- **P1** Narrow boot-time 16- and 32-bit execution, only the sequences that must run:
  the firmware's reset-vector climb to 64-bit mode, the start-up stubs firmware and
  Windows use to bring up extra CPUs, and the short protected-mode stretch before the
  jump to 64-bit. Not general real mode or protected-mode support. **Needed by M3.**
  - An interpreter is enough: boot-time 16-bit and 32-bit code has no performance
    expectation. Correctness only.
  - **Built** (branch `boot-interp`, November 22, 2026;
    [the plan](restructure/m2-boot-interpreter-plan.md)): the interpreter runs in the
    shim, fed by the host's decoder, in real mode, 16- and 32-bit protected mode with
    paging off and compatibility mode at CPL 0, and hands over to the translator once
    64-bit code runs. Segment-descriptor loads, control-register and mode-switch writes
    and memory with paging off are its own. Instructions outside its first list (what
    real start-up code uses) stop the VM with a report naming them; each one a real boot
    hits becomes a small addition.
  - **INIT and start-up inter-processor interrupts** work: parked vCPUs start in real
    mode at the given vector. Debian's kernel starts its CPUs that way when the ACPI
    wake-up mailbox is left out (`--no-mailbox`), and kvm-unit-tests' SMP tests pass.
  - **Estimate:** the plan guessed 72–118 agent hours (low confidence); the earlier cost
    note's 2–4 weeks covered flat 32-bit kernel-mode code only.
  - **Test in M2:** an exit test that runs a firmware-style reset vector from a mapped
    image through to 64-bit mode, since the real firmware only arrives in M3. It passes
    at 1, 2 and 4 vCPUs, and OVMF's reset vector runs to SEC, its first 64-bit stage.
- **P3** Other 64-bit distributions (Fedora, Alpine, Ubuntu...), which usually carry
  32-bit code at boot or in their installers, to test the OS-agnostic claim.
- **P3** Emulated USB controller with keyboard and mouse (HID). Nothing needs it while
  M3 provides PS/2 for firmware and installer input; promote it if PS/2 is dropped.
- **P3** Translation-glue and exit-cost work, and warm start through the persistent code
  cache, measured in this milestone's benchmark round.
- **P3** Audio through an Intel-HDA-class device; shared folders. Both are testable on
  Debian today and independent of the CPU work.
- **First check:** confirm the Debian 13 guest kernel has 32-bit program support enabled
  and that its i386 multiarch toolchain packages exist; the earlier note did not verify
  either. The 4–8 week estimate covered flat segments only, so expect it to grow with
  the far-transfer path.
- **Exit:** a stock i386 userland (static and dynamically linked, threaded) runs under
  the Debian guest, including a 32-bit toolchain build; performance recorded against
  QEMU.

## M3 — UEFI boot

- **P1** A UEFI firmware (a build of the open-source reference firmware; writing our own
  is not worth it) and the platform pieces it expects: ACPI tables, a firmware-config
  channel, persistent variable storage, a boot menu path.
- **P1** **Choose the firmware build first, as a short spike.** The choice sets most of
  M3's work. A stock QEMU-style firmware build expects chipset pieces natemu does not
  have: a power-management PCI function it programs itself (ours is a fixed block with
  no PCI function), QEMU's firmware-config channel with ACPI table loading, a flash
  device near 4 GB for code and variables, and CMOS memory sizing. The firmware built
  for the Cloud Hypervisor VMM needs a much smaller platform; compare the two before
  committing. Whichever is chosen, natemu's own ACPI builder must be delivered through
  the channel it reads. (From my own knowledge of the firmware project, unverified.)
- **P1** Platform pieces that do not exist yet (checked in the code today): the flash
  or firmware-config device from the spike, a host input source for the PS/2 keyboard
  and mouse (the controller exists; the display front end will add the source), and
  SMBIOS tables for a firmware boot (a direct boot has them; the firmware's come with M3). The ACPI tables already include the PCI root bridge with resources and
  interrupt routing.
- **P1** Multi-BAR, multi-function PCI. The bus today routes BAR0 only, one function per
  device, one fixed 256 MiB window and no I/O BARs. A SATA controller (BAR5 plus I/O
  BARs), a Bochs-style display (BAR0 and BAR2) and a firmware PM function (second
  function) need more.
- **P1** Boot Linux the way a PC does (firmware, bootloader, kernel) from the existing
  virtio disk, alongside the current direct-boot path.
- **P1** Display for the firmware and installers: a framebuffer device with resolution
  changes. Input comes from the PS/2 controller above.
- **P1** Decide whether Secure Boot variables are supported (Windows 11 expects a
  Secure-Boot-capable machine). Real variable protection normally needs System
  Management Mode, which stays out of scope.
- **P3** (recommended) A cheap Windows PE boot probe as soon as the firmware runs, to surface Windows
  surprises before the usability milestone rather than in M4.
- **Exit:** unmodified installers of named distributions boot via UEFI from disk and ISO.
  Some installers look for a CD-ROM drive and may not find their media on a virtio disk
  (unverified); if so, bring the CD-ROM device forward from M4.

## MU — Usability (between M3 and M4)

The user-facing work, kept apart so it can flex. It sits before Windows so M4 starts
with a usable machine: working display, input, audio and clipboard. It is also the home
for any P2 or P3 item that slipped from M1–M3, and the owner can reorder what is in it.

- **P1** A Linux desktop session usable end to end: display, keyboard and mouse,
  clipboard (needs a guest-side helper agent).
- **P3** Audio (if not done in M2).
- **P3** A display path better than the M3 framebuffer, sized to what a Windows desktop
  needs (this absorbs the display-acceleration item that was in M4).
- **P3** Shared folders (if not done in M2).
- **P3** vmnet as a second host network backend alongside slirp. Slirp is usable for now;
  vmnet needs root on this OS, which makes it hard to test and iterate on, so it waits
  until something needs bridged or faster networking.
- **P3** USB forwarding of physical devices, starting from a single-device probe (the
  existing source audit favours an emulated USB controller or USB/IP), once the emulated
  controller from M2 is solid.
- **P3** Whole-VM save/restore, last and only if cheap; not a priority.
- **P3** A management interface (control API) if it turns out easy; front ends build on it.
- **Exit:** the P1 desktop session works and every P2/P3 item is done or explicitly
  deferred.

## M4 — Windows 10 / 11 (TPM out of scope)

The largest unknown. Windows asserts far more about the machine than Linux does, so expect
a long tail of platform-compatibility fixes.

- **P1** Storage and network: reuse our virtio devices with the free signed virtio-win
  drivers on a second ISO (the installer loads the disk driver from it; network and the
  rest install afterwards).
- **P1** Installer media the Windows setup can read before any driver loads: an emulated
  SATA CD-ROM (or USB mass storage) for the install ISO and the driver ISO. Virtio alone
  is not enough, because the installer must keep reading its own media; the alternative
  is modifying the ISO, which breaks "stock".
- **P1** Windows starts extra CPUs through its own real-mode stub (from the M2 boot-time
  execution item).
- **P1** A Windows CPU-requirements audit early in M4: CPU identification bits and
  model-specific registers against Windows' needs. For example, the extended CPUID
  leaf does not report PREFETCHW today, which 64-bit Windows 8.1 and later require
  (whether it checks the bit or just runs the instruction is unverified).
- **P3** VHD/VHDX image support (a stock-ISO install onto qcow2 or raw does not need it).
- **P1** Timers and interrupt plumbing Windows needs (local APIC timer, RTC, ACPI
  power-management timer), PAT/MTRR behaviour. **P3** HPET (promote to P1 if the boot needs it): I believe Windows runs
  without one on other hypervisors, so add it only if the boot needs it (unverified).
- **P1** 32-bit applications inside Windows (the M2 work pays off here).
- **P1** Windows 11's TPM/Secure Boot checks: use the supported install-time bypasses
  while the TPM is out of scope.
- **P3** Multi-vCPU scaling and interrupt/exit costs under a Windows workload.
- **P3** Emulated NVMe (Windows 10/11 drives it without extra drivers) for a no-ISO
  install; optional Hyper-V-style paravirtual hints if they pay for themselves.
- **Exit:** a stock Windows 10 and Windows 11 ISO installs, boots to desktop, runs ordinary
  applications on multiple vCPUs; boot and install times recorded.

## Future (unordered, owner schedules)

Everything else on the earlier wish list is scheduled in M1–M4 or MU above, or is the separate hardware-coverage track below. What remains here:

- **Windows 7:** left for the future. If it is revisited, real CSM (legacy BIOS layer,
  needs real-mode execution) stays out of scope; the idea to check first is UEFI plus a
  small shim that fakes the legacy video call, assuming Windows 7 interprets that call in
  software (unverified).
- **Front end / native apps:** the window shell is the easy part. The real work is in the
  emulator: zero-copy display, input capture, cursor, resize, clipboard, audio, and
  usually a helper agent in the guest (several of these arrive in M3, M4 and MU).
  Depends on the management interface in MU. Packaging and signing (the Hypervisor
  entitlement) also belong here.
- **Software TPM** if Windows 11 users need it.

## Separate track: hardware coverage

Runs beside the milestones and does not gate them. It validates the CPU feature floor and
the guest-visible behaviour on every M-series generation we claim (M1 is supported but
unverified today; M2 and M4 are the machines we have run on), using the owner's machines
for the runs. Scheduled by the owner; results are recorded per chip.

## How performance is handled

Performance is expected only where it matters: translated 64-bit code and 32-bit user
programs. Boot-time 16-bit and 32-bit code is interpreted and judged on correctness.

Each milestone adds a benchmark for its own workload from existing public suites (we do
not author benchmarks), recorded once against QEMU and then re-run only when a feature
completes. A milestone is not closed with a known wasteful design, but a performance gap
against native may stay open if labelled as a gap.

## Open questions for the owner

1. Test hygiene first: OK to start with the one-time prune of the scratch area (keeping
   what current records cite) before anything else? Deleting is not reversible, so I will
   list what goes before removing it.
2. Should qcow2 land before or after the configuration-format decision? My suggestion:
   decide the format first (a day of design), build qcow2 against it.
