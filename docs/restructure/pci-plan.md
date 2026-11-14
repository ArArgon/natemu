# Virtio over PCI with message interrupts: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: built November 13, 2026, on branch `pci` (from `main` at dcffe4b): Tasks 1-7 done and reviewed; Task 8's
benchmark round, full suite and records follow. Where the build departed from this text it is marked "As built". Owner direction (November 13): keep virtio-MMIO
as it is; PCI lives alongside it; build on existing code rather than write from scratch; Windows is a later
consideration that must not be blocked. Owner corrections to the first draft (same day): new dependencies are allowed
when judicious; the product is an OS-agnostic emulator with Debian as the first guest only, so guest-visible behaviour
is justified by the specifications and by what a real PC does, never by one kernel's source. Independent review
(November 13, on the revised text): accept with required changes; every blocking finding is accepted and folded in
below (power-off drain through the bus, read-back of read-write fields, 8-byte accesses, the host bridge identity,
two unrunnable gate steps, and the exact upstream adaptations). Owner decisions after the review (same day): the flag
and default as proposed; licence housekeeping later; a per-device mix of transports is allowed as an experimental
option; the legacy interrupt line (INTx) is part of this plan.

**Goal:** a second virtio transport, virtio-PCI with MSI-X, selectable per launch, so that a device interrupt costs no
VM exit (today: InterruptStatus read, InterruptACK write, EOI broadcast, about 1.5 us each).

**Architecture:** the queues, devices, owner threads and reset/lifecycle machinery of `crates/host/src/virtio` are
unchanged. A new register front end maps the virtio-PCI capability structures onto the existing register state, and
the one place that raises an interrupt posts an MSI message to the LAPICs instead of driving an I/O APIC line. Generic
PCI (config space, MSI-X table) is adapted from cloud-hypervisor; the bus, host bridge and virtio glue are written here.

**Tech stack:** Rust, `crates/host`; new crates `thiserror` and `log` (see constraints); adapted source: cloud-hypervisor
`f84dae558c8b9518e9d9bf8c0f1b64cd4dc8e546`: `pci/src/configuration.rs` (Apache-2.0 AND BSD-3-Clause),
`pci/src/msix.rs` (Apache-2.0 OR BSD-3-Clause), `BarReprogrammingParams` from `pci/src/device.rs`, and the
masked/pending delivery logic of `virtio-devices/src/transport/pci_device.rs:947-990`. Upstream's
`LICENSES/Apache-2.0.txt` and `BSD-3-Clause.txt` are copied to `third_party/cloud-hypervisor/`.

**Spec:** the "Design" section below (there is no separate spec document). Evidence for the motivation:
`natemu-raw/io-gap-20261111-01/summary.md` and `gap.txt`.

## Global constraints

- Default launch is unchanged: without the new flag the ACPI tables, the E820 map, the device registry and every
  existing test are byte-for-byte what they are at dcffe4b.
- Adapted files stay as close to upstream as they can, so a later upstream fix can be compared line by line: they keep
  their licence header, add a line naming the upstream path and the pinned commit, and keep upstream's use of small
  general-purpose crates. That adds two dependencies to `crates/host`: `thiserror` (upstream's error enums) and `log`
  (42 call sites in the two files; the CLI installs a stderr logger printing `natemu: pci: ...`). `serde` is already a
  dependency. Only what cannot work on this host is replaced: `vm-migration`, `vm-memory`, `vmm-sys-util` (Linux
  eventfd), `vm-device`, `vm-allocator`, VFIO. Any further dependency is named, with its reason, in the commit that
  adds it.
- Guest-visible behaviour follows the specifications (PCI Local Bus 3.0, ACPI 6.5, virtio 1.2, Intel SDM) and, where
  they leave a choice, a real PC (QEMU's `pc` board as its stand-in, as for the interrupt chipset). Each value cites
  one of those at its definition. Linux is cited only as "what the first guest does" and is a test, not a requirement;
  nothing may work only because one kernel tolerates it.
- Lock order: virtio state lock, then the function's config-space lock, then MSI-X lock, then chipset lock. Never the
  reverse; no config-space write takes the state lock. The command register's bus-master and memory-space bits and the
  BAR base are also published in atomics for lock-free readers.
- The device registry is frozen at launch (`Devices::freeze`). The bus therefore registers the whole PCI memory window
  as one range and routes each access to the function whose BAR currently decodes that address, so a guest may size,
  move and disable BARs as the PCI specification allows without any change to the registry.
- Benchmarks only at the end (Task 8). The full suite once, before merge.
- Branch `pci` off `main`. Commit with pathspecs.

## Design

### What the guest sees (only with `--virtio-transport pci`)

| Item | Value | Basis |
| --- | --- | --- |
| Config access | mechanism #1, ports 0xCF8 (address) and 0xCFC-0xCFF (data); no ECAM, no MCFG | PCI 3.0 3.2.2.3.2; conventional PCI needs nothing above offset 0xFF, and MSI-X and the virtio capabilities are below it |
| Host bridge | 00:00.0, vendor 0x8086, device 0x1237 (i440FX), class 0x060000, header type 0, no BARs | QEMU `pc`'s host bridge, the board the rest of the chipset already follows. Guests key behaviour on it: FreeBSD enables MSI in a VM only for bridges on its list, which has this one and not cloud-hypervisor's 0x0D57 (`sys/dev/pci/pci.c`) |
| ACPI | DSDT `\_SB.PCI0`: `_HID` PNP0A03, `_UID` 0, `_CRS` = bus 0-0, IO 0xCF8-0xCFF, 32-bit memory window 0xE000_0000-0xEFFF_FFFF. The bus range is a WordBusNumber descriptor with length 1. `_PRT` routes each slot's INTA# to its GSI as a hardwired source (ACPI 6.5 6.2.13). No `_OSC` (not PCIe), no MCFG. The descriptor bytes are emitted by hand as COM1's are and checked once with `iasl -d` | ACPI 6.5 6.1.5 and 6.2.2; PCI Firmware 3.x 4.1 (root bridge `_CRS`) |
| E820 | the reserved entry `[low_end, 4 GiB)` is split so that 0xE000_0000-0xEFFF_FFFF is unlisted | on a PC the PCI window is absent from the memory map, not reserved in it (QEMU `pc`: the hole below 4 GiB) |
| Devices | block 00:01.0, network 00:02.0, entropy 00:03.0; vendor 0x1AF4, device 0x1040 + virtio ID, revision 1, subsystem vendor 0x1AF4, subsystem ID = virtio ID, class as QEMU's (`0x010000` block, `0x020000` network, `0x00FF00` entropy) | virtio 1.2 4.1.2 |
| Interrupt pin | 1 (INTA#) on each virtio function; interrupt line register = the device's GSI (block 16, network 17, entropy 18, the lines the MMIO transport uses) | PCI 3.0 6.2.4; firmware's role for the line register |
| BAR0 | 32-bit, non-prefetchable memory, 0x8000 bytes, programmed at launch to 0xE000_0000 + 0x1_0000 x slot with memory decoding and bus mastering off | PCI 3.0 6.2.5; firmware's role on a PC, since natemu boots without one |
| BAR0 layout | 0x0000 common cfg, 0x1000 ISR, 0x2000 device cfg, 0x3000 notify (`notify_off_multiplier` 4, `queue_notify_off` = queue index), 0x4000 MSI-X table, 0x5000 MSI-X PBA | virtio 1.2 4.1.4; PCI 3.0 6.8.2 |
| Capabilities | MSI-X (table size = queues + 1), then vendor capabilities COMMON_CFG, NOTIFY_CFG, ISR_CFG, DEVICE_CFG, each `length` 0x1000 | virtio 1.2 4.1.4; PCI_CFG capability (4.1.4.8) is a follow-up |
| FADT | unchanged (IAPC_BOOT_ARCH 0x0005: "MSI Not Supported", bit 3, stays clear) | ACPI 6.5 5.2.9.3 |

What the first guest (Debian's Linux) needs from this was checked separately and is covered by Task 7's gates, not
by the design's justification.

BAR behaviour is the specification's: the size probe (all-ones write) reads back the size mask; a new address moves the
decode; with the command register's memory-space bit clear the range does not decode (reads all-ones, writes ignored,
as an unclaimed address); MSI-X delivery requires bus mastering. A BAR moved outside the window does not decode (a
host bridge forwards only its windows). Costs of claiming the whole window, both stated in the README: a read of a
window address no BAR decodes takes a host exit each time instead of mapping the shared all-ones page, and the strict
unassigned-access switch no longer stops on such an access. One stated leniency: an access to an unimplemented
offset inside BAR0, or with an unexpected width, reads zero and ignores writes (it does not stop the vCPU as the MMIO
front end's refusals do).

### Interrupt delivery

`Irqs::msi(address, data)` decodes the x86 MSI format (Intel SDM Vol. 3, 11.11.1 and 11.11.2:
destination ID in address bits 19:12, destination mode bit 2, redirection hint bit 3; vector in data bits 7:0, delivery
mode bits 10:8, trigger mode bit 15) into the chipset's existing `Message` with `level: false` and sends it through
`Lapics::send`. Edge messages clear the TMR bit, so the resident's EOI makes no host exit (existing behaviour).
Stated simplifications: the message is always delivered edge-triggered (data bit 15 is ignored; level-triggered MSI is
not used by PCI devices), and delivery mode ExtINT is delivered as fixed, as the chipset already does.

A used-buffer interrupt for queue `q`: set ISR bit 0; if MSI-X is enabled and bus mastering is on, deliver the entry
`queue_msix_vector[q]` names, or set its pending bit when that entry or the function is masked (delivered at unmask);
`VIRTIO_MSI_NO_VECTOR` (0xFFFF) or MSI-X disabled delivers nothing. A configuration change uses `config_msix_vector`.
The network device's interrupt hold-back keeps working: it remembers which queues owe an interrupt and delivers each
queue's vector once when it releases. `fail()` (NEEDS_RESET) delivers `config_msix_vector` the same way. While
MSI-X is disabled, the ISR is mirrored in the PCI status register's interrupt-status bit (virtio 1.2 4.1.4.5; PCI 3.0
6.2.3).

INTx, for a guest that does not enable MSI-X: while MSI-X is disabled and the command register's interrupt-disable bit
(bit 10) is clear, the function's GSI line is asserted while the ISR is nonzero, and the ISR read that clears it lowers
the line: the existing level-line code, driven from the ISR instead of InterruptStatus. Enabling MSI-X lowers the line
and keeps it low (PCI 3.0 6.8.3.3: a function must not use INTx while MSI-X is enabled). This path costs the guest two
exits per interrupt (the ISR read and the level EOI). PCI interrupt lines are active low by ACPI convention; the I/O
APIC model, like QEMU's, ignores the polarity bit, so the guest's programming is accepted as it is.

### Register front end

Common-cfg fields that have an MMIO equivalent are translated to that MMIO register and go through the existing
`Virtio::access`/`poll` path, so resets, deferral and ordering are shared and untouched:

| Common cfg (offset, width) | Existing register |
| --- | --- |
| device_feature_select 0x00/4, device_feature 0x04/4 | DEVICE_FEATURES_SEL, DEVICE_FEATURES |
| driver_feature_select 0x08/4, driver_feature 0x0C/4 | DRIVER_FEATURES_SEL, DRIVER_FEATURES |
| device_status 0x14/1 | STATUS |
| queue_select 0x16/2 | QUEUE_SEL |
| queue_size 0x18/2 (write) | QUEUE_NUM |
| queue_enable 0x1C/2 | QUEUE_READY |
| queue_desc 0x20/8, queue_driver 0x28/8, queue_device 0x30/8 (as two 4-byte halves) | QUEUE_{DESC,DRIVER,DEVICE}_{LOW,HIGH} |

PCI-only fields are new private registers on the same path (never reachable from the MMIO front end): config_msix_vector
0x10/2, queue_msix_vector 0x1A/2 (read back 0xFFFF for a vector index >= table size), queue_size read (the written
size, else the queue maximum), num_queues 0x12/2, config_generation 0x15/1 = 0, queue_notify_off 0x1E/2, and the ISR
byte (read clears). Every read-write field reads back what was written (virtio 1.2 4.1.4.3): the two feature
selectors, the selected driver-feature half, queue_select and the three queue addresses are served from state, since
the MMIO registers they map to are write-only. An 8-byte access to a 64-bit field is split into its two halves
(4.1.3.1 permits either), and 8-byte accesses to the MSI-X table and PBA are passed through (PCI 3.0 6.8.2). A write
to the device-specific configuration is ignored, not refused. As built, two more leniencies: a queue-layout write
while the queue is enabled is ignored (the MMIO front end refuses it, which would stop the vCPU), and with Bus Master
clear the device still processes requests and holds its interrupts pending until Bus Master is set. queue_enable = 1 with no size written uses the queue
maximum; a queue_enable write of 0, which a driver must not make, is treated as the MMIO queue reset. A device reset sets both kinds of
vector to 0xFFFF. A notify write at 0x3000 + 4q takes the existing lock-free `kick(q)` path first.

### Out of scope (not blocked)

ECAM/MCFG and PCIe; the PCI_CFG capability; 64-bit BARs; hotplug;
non-virtio PCI devices; interrupt hold-back and EVENT_IDX for the disk device.

## File structure

| File | Responsibility |
| --- | --- |
| `crates/host/src/pci/configuration.rs` (new, adapted) | one function's 256-byte config space: write masks, BARs, capability list |
| `crates/host/src/pci/msix.rs` (new, adapted) | MSI-X capability, table, PBA, masking, delivery through a hook |
| `crates/host/src/pci/mod.rs` (new) | `Bus`: functions by slot, the 0xCF8/0xCFC port device, the memory-window device and its BAR routing, the host bridge, platform constants |
| `crates/host/src/virtio/pci.rs` (new) | virtio-PCI function: capabilities, BAR0 dispatch, common-cfg translation |
| `crates/host/src/virtio/mod.rs` | private PCI-only registers, per-queue vectors, the raise path |
| `crates/host/src/irq.rs` | `Irqs::msi` |
| `crates/host/src/boot/acpi.rs`, `boot.rs` | `PCI0` device, the E820 hole |
| `crates/host/src/linux.rs`, `crates/cli/src/main.rs`, `crates/driver/src/drivers/linux.rs` | the launch option, the stderr logger, the `Layout` literal |
| `tools/run_net_linux.py`, `tools/run_virtio_block.py`, `tools/run_io_coverage.py`, `tools/run_io_matrix.py`, `tools/io_common.py` | `--transport pci` variants; the shared guest probe and judge |

---

### Task 1: MSI delivery into the LAPICs

**Files:** modify `crates/host/src/irq.rs`.

**Interfaces:**
- Produces: `impl Irqs { pub fn msi(&self, address: u64, data: u32) -> bool }` (true when some LAPIC accepted).

- [ ] Write tests in `irq.rs`'s test module, on the existing test board with 2 vCPUs in x2APIC and xAPIC modes:
  `msi_fixed_physical_reaches_only_its_destination` (address `0xFEE0_1000`, data `0x41`: vector 0x41 pending on vCPU 1
  only, its TMR bit clear), `msi_logical_destination_follows_the_ldr`, `msi_vector_below_16_is_dropped`,
  `msi_outside_fee_is_dropped` (address `0xFEC0_0000` returns false, posts nothing).
- [ ] Run `cargo test -p natemu irq::` and see them fail to compile.
- [ ] Implement `Irqs::msi`: lock the chipset, build `Message { dest, logical, mode, vector, level: false }`, call
  `lapics.send`. Refuse addresses whose bits 31:20 are not 0xFEE.
- [ ] Run the tests; commit.

### Task 2: PCI configuration space (adapted)

**Files:** create `crates/host/src/pci/configuration.rs`, `crates/host/src/pci/mod.rs` (module declaration only);
modify `crates/host/src/lib.rs`.

**Interfaces:**
- Produces, with upstream's signatures unless listed: `PciConfiguration::new(...)` as upstream minus the `state`
  argument (it keeps `msix_config: Option<Arc<Mutex<MsixConfig>>>`, which is how a config write to the MSI-X message
  control reaches the table, upstream lines 1063-1083); `read_reg`; `write_config_register(&mut self, reg_idx, offset,
  data) -> Vec<BarReprogrammingParams>`; `add_pci_bar`; `add_capability(&mut self, &dyn PciCapability) ->
  Result<usize>`; `get_bar_addr(&self, bar_num: usize) -> u64`; plus one addition, `command(&self) -> u16`.
  BAR routing (Task 4) reads the live BAR register and the command register; it does not rely on the reprogramming
  list, which upstream only produces while memory decoding is on. As built: the base is taken from the register
  itself (`read_reg(4)`), because upstream's `get_bar_addr` returns a cache that byte and word writes do not update.

- [ ] Add `thiserror` and `log` to the workspace and `crates/host`. Copy upstream `pci/src/configuration.rs` at the
  pinned commit. The complete list of changes: remove snapshot/migration (`vm-migration`, the `state` argument and
  `PciConfigurationState`); replace `vm_device::PciBarType` with a local enum; bring `BarReprogrammingParams` in from
  `pci/src/device.rs`; replace `vm_memory::ByteValued` in the tests with plain byte conversion; add `command()`. Keep
  the rest as upstream has it, unused class enums included.
- [ ] Keep every upstream unit test that still applies (BAR sizing probe, 64-bit BAR halves, capability chain,
  read-only fields). Add `bar_base_and_command_are_readable_with_decoding_off_and_on` (the launch state has decoding
  off; `get_bar_addr` and `command()` reflect each write either way).
- [ ] `cargo test -p natemu pci::configuration`; commit.

### Task 3: MSI-X (adapted)

**Files:** create `crates/host/src/pci/msix.rs`.

**Interfaces:**
- Consumes: nothing from natemu.
- Produces: `pub type Deliver = Arc<dyn Fn(u64, u32) + Send + Sync>`; `MsixConfig::new(msix_vectors: u16, deliver:
  Deliver, devid: u32) -> Result<Self, Error>` (upstream's, with the interrupt group and saved state replaced);
  upstream's `set_msg_ctl`, `enabled`, `masked`, `read_table`, `write_table`, `read_pba(&mut self, ..)`,
  `set_pba_bit`, `MsixCap::new(table_pci_bar, table_size, table_off, pba_pci_bar, pba_off)`; and one addition,
  `trigger(&mut self, vector: u16)`: deliver the entry's message, or set its pending bit when the entry or the
  function is masked; nothing when disabled or out of range. Upstream has this logic in the virtio transport
  (`virtio-devices/src/transport/pci_device.rs:947-990`), from where it is adapted.

- [ ] Copy upstream `pci/src/msix.rs`. The complete list of changes: replace `InterruptSourceGroup`/`EventFd` and the
  `hypervisor::Vm` notifier with `Deliver` (`inject_msix_and_clear_pba` calls it with the entry's address and data);
  replace `vm_memory::ByteValued` with plain little-endian byte conversion; remove snapshot code and the `state`
  argument; add `trigger`. Guest-triggerable `error!`/`warn!` calls stay as upstream wrote them; the logger (Task 6)
  bounds their volume.
- [ ] Port upstream tests; add `masked_entry_sets_pending_and_delivers_once_on_unmask`,
  `function_mask_holds_every_vector_then_delivers_each_pending_once`, `disabled_delivers_nothing_and_sets_no_pending`,
  `rewriting_an_entry_while_masked_delivers_to_the_new_address` (the affinity-change sequence Linux uses).
- [ ] `cargo test -p natemu pci::msix`; commit.

### Task 4: bus, config ports, host bridge

**Files:** modify `crates/host/src/pci/mod.rs`.

**Interfaces:**
- Consumes: `PciConfiguration` (Task 2), `devices::{Device, Devices, Shared}`.
- Produces: `pub trait Function: Send + Sync { fn config_read(&self, reg_idx: usize) -> u32; fn config_write(&self,
  reg_idx: usize, offset: u64, data: &[u8]); /** BAR0's current base, or None while memory decoding is off. */
  fn bar(&self) -> Option<u64>; fn bar_len(&self) -> u64; fn bar_access(&self, vcpu: usize, offset: u64, size: u64,
  value: Option<u64>) -> Access; fn bar_poll(&self, vcpu: usize, token: u64) -> Access; /** As `Device::quiesce`. */
  fn quiesce(&self) -> Result<bool>; }` (`bar` is read lock-free:
  the function publishes it in an atomic from its config writes); `Bus::new() -> Self` (host bridge at slot 0);
  `Bus::add(&mut self, slot: u8, f: Arc<dyn Function>) -> Result<()>`;
  `Bus::install(self, devices: &mut Devices) -> Result<()>` (registers ports 0xCF8..0xD00 and the memory window as one
  `Direct` device; a deferred access's token carries the slot so `poll` reaches the same function; the window device's
  `quiesce` calls every function's, since `Devices::quiesce` at power-off reaches only registered devices);
  `pub mod platform { pub const WINDOW: (u32, u32) = (0xE000_0000, 0xF000_0000); pub const fn bar(slot: u8) -> u32 }`.

- [ ] Tests: `type1_probe_as_linux_does_it` (write 0x8000_0000 to 0xCF8 as 4 bytes, read it back equal; then
  `pci_sanity_check`: class 0x0600 at 00:00.0), `absent_function_reads_all_ones_and_ignores_writes`,
  `byte_and_word_data_accesses_address_within_the_register` (0xCFC+1, 0xCFE), `address_without_enable_bit_reads_ones`,
  `byte_write_to_cf9_does_not_change_the_address`, `nonzero_bus_or_function_is_absent`; and for the window, with a
  stub function: `window_routes_to_the_bar_that_decodes_the_address`, `a_moved_bar_decodes_at_its_new_address_only`,
  `memory_decoding_off_reads_all_ones_and_ignores_writes`, `window_addresses_no_bar_claims_read_all_ones`,
  `overlapping_bars_route_to_the_lowest_slot` (a guest error; deterministic), `a_bar_outside_the_window_does_not_decode`,
  `quiesce_reaches_every_function_and_reports_done_only_when_all_are`, and
  `only_a_dword_write_changes_the_address_and_cf8_reads_as_unclaimed_below_dword` (PCI 3.0 3.2.2.3.2; Linux's probe
  begins with a byte write to 0xCFB).
- [ ] Implement, using upstream `pci/src/bus.rs` `PciConfigIo` for the register/offset decode of the data port. It
  differs from upstream on purpose in one place: upstream applies byte and word writes to the address register; the
  specification and the test above do not.
- [ ] `cargo test -p natemu pci::`; commit.

### Task 5: the virtio-PCI function

**Files:** create `crates/host/src/virtio/pci.rs`; modify `crates/host/src/virtio/mod.rs`.

**Interfaces:**
- Consumes: Tasks 1-4; `Virtio::access`, `Virtio::poll`, `Virtio::kick`, `Virtio::config_access`.
- Produces: `impl Virtio { pub fn pci(self: &Arc<Self>, slot: u8) -> Arc<dyn pci::Function> }` (for `Bus::add`; its
  BAR0 is programmed to `pci::platform::bar(slot)`; calling it switches this device to the PCI transport for good:
  MSI-X once the driver enables it, and until then its GSI as the legacy INTx line).

- [ ] Tests, in `virtio/pci.rs`, driving BAR0 and config space as Linux's `vp_modern_probe` and `vp_find_vqs` do
  (helper: `negotiated_over_pci(&rig)`): `capabilities_are_the_four_virtio_structures_and_msix_within_bar0`;
  `negotiation_reaches_driver_ok_with_exact_field_widths`; `queue_size_reads_the_maximum_until_written`;
  `queue_enable_without_a_size_uses_the_maximum`; `vector_fields_read_back_and_refuse_out_of_range_with_no_vector`;
  `completion_delivers_the_queues_vector_once` (count `Deliver` calls and check address/data);
  `no_vector_or_msix_disabled_or_bus_master_off_delivers_nothing`;
  `completion_while_masked_is_delivered_at_unmask`; `device_reset_returns_vectors_to_no_vector_and_drops_stale_raises`
  (a completion of the closed incarnation delivers nothing); `network_hold_back_delivers_each_owing_queue_once`;
  `notify_at_multiplier_offset_kicks_that_queue_without_the_state_lock`; `isr_read_clears`;
  `unimplemented_offsets_and_widths_read_zero_and_never_refuse`;
  `a_deferred_reset_over_pci_polls_to_completion` (reuses the block rig's held disk call);
  `mmio_front_end_cannot_reach_the_private_registers`; `every_read_write_field_reads_back_what_was_written`;
  `eight_byte_accesses_to_queue_addresses_and_the_msix_table_take_effect`; `device_config_writes_are_ignored`;
  `a_failed_device_delivers_the_config_vector`; `isr_is_mirrored_in_pci_status_while_msix_is_disabled`;
  `intx_line_follows_the_isr_while_msix_is_disabled`; `interrupt_disable_bit_holds_the_line_low`;
  `enabling_msix_lowers_the_line_and_completions_no_longer_raise_it`; `isr_read_lowers_the_line`;
  `power_off_drains_a_held_block_request_through_the_bus` (the existing quiesce test, entered through
  `Devices::quiesce`).
- [ ] Run and see them fail.
- [ ] Implement: the private registers and `vectors: [u16; 3]` in `State`; `raise` takes the queue (or "config") and
  branches on the transport; the hold-back remembers owing queues; `virtio/pci.rs` holds the layout constants,
  capability bytes and the BAR0 dispatch. `irq()` drives the INTx line from the ISR while MSI-X is disabled and
  keeps it low otherwise.
- [ ] `cargo test -p natemu virtio::` (all existing MMIO tests unchanged and passing); commit.

### Task 6: machine description and launch option

**Files:** modify `crates/host/src/boot/acpi.rs`, `crates/host/src/boot.rs`, `crates/host/src/linux.rs`,
`crates/cli/src/main.rs`.

**Interfaces:**
- Produces: CLI `--virtio-transport <mmio|pci>` (default `mmio`), and, marked experimental in its help text, the
  per-device form `--virtio-transport disk=pci,net=mmio,rng=mmio` (unnamed devices take `mmio`);
  `Layout { pci: bool, .. }` (true when any device is on PCI; false in
  `Layout::DEFAULT`); `acpi::build_with_devices(vcpus, base, virtio_mmio: &[VirtioMmio], pci: &[(u8, u32)]) -> Tables`
  (the PCI functions as (slot, GSI) for `_PRT`; an empty slice emits no `PCI0`; `build_with_virtio` keeps its
  signature and passes an empty slice).

- [ ] Tests: `default_tables_and_map_are_unchanged` (hash of `build_with_virtio` blobs and of `Layout::DEFAULT.e820()`
  equal to values recorded from dcffe4b before the change); `pci_map_leaves_the_window_unlisted_and_stays_sorted`;
  `pci_dsdt_has_pci0_and_no_virtio_mmio_devices` (decode with the test module's existing AML walker, or compare
  against bytes checked once with `iasl -d`, as the QEMU-derived tables were).
- [ ] Implement. `linux.rs` puts each enabled device on its chosen transport: MMIO devices as today; PCI devices
  through `Virtio::pci(slot)` on a `pci::Bus`, installed when it has at least one. ACPI gets the MMIO manifest and the
  PCI (slot, GSI) list. Add `mixed_tables_list_each_device_once` (disk on PCI, network on MMIO: one `LNRO0005`
  device, one `_PRT` entry) and `pci_prt_routes_each_slot_to_its_gsi`.
- [ ] The CLI installs a `log` logger writing `natemu: pci: ...` lines to stderr at `warn` and above, at most 16 lines
  per run, then one "further messages suppressed" line (a guest can trigger these at will).
- [ ] `cargo test -p natemu boot::`; `cargo build --release` (updating `Cargo.lock`; the suite builds `--offline
  --locked` and both crates are already in the local cargo cache); commit.

### Task 7: guest acceptance

**Files:** modify `tools/run_net_linux.py`, `tools/run_virtio_block.py`, `tools/run_io_coverage.py`,
`tools/run_suite.py`, `tools/io_common.py` (virtio-pci is built into the Debian kernel: no new module; the play image
needed no change).

- [ ] Add `--transport {mmio,pci}` to the four runners (passed through as `--virtio-transport`). `run_io_matrix.py`
  pins "the first virtio interrupt" to CPU 0, which under PCI is the config vector, and queue vectors of the disk are
  kernel-managed and refuse affinity writes: in PCI mode it instead reads the queue vector's
  `/proc/irq/N/effective_affinity_list` and records that CPU as the interrupt vCPU. Also and a guest probe step
  that prints, in PCI mode: the entries of `/sys/bus/pci/devices`, the `virtio` lines of `/proc/interrupts`, and
  `dmesg | grep -iE 'pci|virtio|msi'`.
- [ ] Gate A (boot): play image, `--rng --disk --net --virtio-transport pci`, 1, 2 and 4 vCPUs: four PCI functions
  present; each virtio device has `PCI-MSIX` lines in `/proc/interrupts` with nonzero counts after use; no
  "No config space access function found", no "Cannot find an available gap", no BAR reassignment or
  "can't claim BAR" line in dmesg.
- [ ] Gate B (function): `run_net_linux.py --transport pci` 9/9 at 1/2/4 vCPUs; `run_virtio_block.py --transport pci`;
  `run_io_coverage.py --transport pci` (same pass set as MMIO, including reboot, disk-full and power-off drain).
- [ ] Gate C (the point of the work): with the perf log on, a depth-16 fio run shows, on the vCPU that takes the
  disk's queue vector, zero EOI-broadcast exits (the per-vCPU count the November 11 gap tooling already reads) and
  device exits equal to that vCPU's own queue notifications only. The status-read and acknowledge counters are zero
  by construction and prove nothing.
- [ ] Gate D (edge cases in the guest): `echo 2 > /proc/irq/N/smp_affinity` for a network queue vector during a
  transfer (the disk's vectors are managed and refuse the write), traffic continues and the count moves to that CPU; boot with `pci=nomsi`: the devices come up on the legacy line (`IO-APIC ... fasteoi` lines 16-18 in
  `/proc/interrupts`) and a short disk and network check passes; one experimental mixed launch (disk on PCI, network
  on MMIO) passes the same short check; depth-1 I/O at 1 vCPU (the vCPU halts between requests) completes at
  the MMIO rate or better with no run ending early.
- [ ] Add suite steps `net-linux-pci` and `io-coverage-pci`; commit.

### Task 8: review, measurement, records

- [ ] Independent review of the whole branch by a separate reviewer agent (Opus); fix substantive findings.
- [ ] One benchmark round, PCI against MMIO on the same binary (`run_io_matrix.py` pinned jobs at 1 and 4 vCPUs;
  network up/down as the November 12 round), reusing recorded QEMU and VZ numbers. The interrupt vCPU is found as
  Task 7 describes, a stated change of method from the MMIO rounds. Expected: 1-vCPU queued jobs and
  depth 1 improve by roughly 10-15% (estimate from 3 exits x 1.5 us); 4-vCPU fio-bound jobs about unchanged.
- [ ] Full suite once. Update `crates/host/src/virtio/README.md` (transport section, stated deviations), the
  backlog row in `docs/research/investigations/INV-03-01.md`, and, only if `tools/run_io5.py` has reached `main` by
  then (it is on `perf-real-workload`, not on this branch), its `EXPECTED` list (the PCI lines are MMIO-mode only).
- [ ] Owner decides the default transport and the merge.

## Review focus

Conditions the design implies that are most likely to bite, each pinned by a test above:

1. A vector or the whole function is masked while completions arrive (Linux does this on every affinity change):
   nothing is lost, nothing is delivered twice. Tasks 3 and 5; Gate D.
2. The last interrupt before a vCPU halts: an MSI must wake a halted vCPU and the run must not be ended while a device
   may still deliver (`Irqs::busy`/`live` accounting was written for level lines). Task 5; Gate D depth 1 at 1 vCPU.
3. A reset or queue disable with requests in flight: no interrupt reaches a vector the driver has since released.
   Task 5.
4. A guest that does not enable MSI-X (`pci=nomsi`, or another guest): the devices work on the legacy line, and a
   guest that switches from one to the other (Linux probes with the line, then enables MSI-X) never sees both or
   neither. Task 5; Gate D.
5. A guest that sizes, moves or disables a BAR (any guest may; Linux reassigns when it dislikes firmware's choice):
   the decode follows the specification and an access in flight during the move is not lost or misrouted. Tasks 2
   and 4.
6. Power-off drain over PCI: the devices are no longer registry entries, so the drain must reach them through the
   bus, and the quiesce rule "guest writes are ignored except the interrupt acknowledge" has no acknowledge in this
   transport. Tasks 4 and 5; Gate B (io-coverage power-off cases).
7. A guest other than Linux: fields read back, 8-byte accesses, a different MSI enabling policy. Task 5's
   specification tests; no second guest is booted in this plan (unverified: Windows on a conventional-PCI root without
   `_OSC`).

## Owner decisions (November 13)

1. Flag `--virtio-transport mmio|pci`, default `mmio` until Task 8's numbers are in.
2. Licence housekeeping (the repository has no licence file) is deferred; the adapted files keep upstream's headers and
   the two upstream licence texts are copied under `third_party/cloud-hypervisor/` meanwhile.
3. Mixing transports per device is allowed as an experimental option: one boot check, not the full matrices.
4. The legacy interrupt line is in scope.
