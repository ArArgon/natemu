# Platform devices (pvpanic, SMBIOS, PS/2): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: built November 24, 2026 (Tasks 1-5, branch `claude/platform-devices-c98b44` from `m2-peripherals` 6dfd0634,
commits from 5fa3b5d1 on); the gate is the integration gate of the peripherals round, still to run. Built as planned:
pvpanic at port 505H (a panic is `Power(4)`), SMBIOS 3.0 with its entry point in a read-only legacy ROM page at F0000H,
an 8042 with a PS/2 keyboard and mouse (adapted from OpenVMM and bhyve), the three `[machine]` keys on in the CLI and
off in `natemu-host` unless `--pvpanic`, `--smbios` or `--ps2`, and the `tests/devices` family (`pvpanic-panic`,
`platform[1]`, `platform[2]`, `platform-off`; smoke has `pvpanic-panic` and `platform[1]`), whose initramfs lives in the
shared input root. Where it departed: the legacy ROM page is data only (a fetch from it stops as not RAM); Task 3's
Device-page break stops at Linux's iBFT scan, before the DMI scan; the `--skip-capability` demonstration ends with a
watchdog stop after about 60 s rather than a timeout; IRQ 1 is raised for the controller's own replies (as a real 8042
and QEMU), and a controller reply over a full output buffer replaces the unread byte (OpenVMM's behaviour; QEMU keeps
it pending); the bench harness gives its switches to each runner as `--natemu-set KEY=VALUE` (m1-perf's baseline
build: `--baseline-set`), which the runners pass to natemu as `--set`. The plan was revised November 23 after an
independent review and a second check (all findings folded in below). Owner direction (same day): build the platform
devices with the highest return first, in their own track beside multi-BAR PCI and the virtio device interface; natemu
stays permissively licensed (adapted code is Apache-2.0, BSD or MIT, never GPL/LGPL).

**Goal:** three PC platform devices a guest finds without drivers we ship: a pvpanic device (a guest kernel panic ends
the run with its own typed stop), SMBIOS tables (the guest sees a vendor, product, processors and memory), and an 8042
keyboard controller with a PS/2 keyboard and mouse (M3 needs it for firmware and installer input).

**Architecture:** pvpanic and the 8042 are board devices on the existing port registry (`crates/host/src/devices.rs`)
with ACPI entries in the DSDT (`crates/host/src/boot/acpi.rs`). SMBIOS is a table builder plus a read-only page at
F0000H that the runtime maps like the firmware image, so every read form (Linux scans it with string copies) runs
natively. None uses PCI or touches guest RAM after boot. Each device has a `[machine]` key, default on.

**Tech stack:** Rust, `crates/host`, `crates/runtime`, `crates/abi`; adapted source with provenance headers (as
`crates/host/src/pci/configuration.rs`): OpenVMM `vm/devices/chipset/src/i8042/{mod.rs,ps2keyboard.rs,spec.rs}` at
`5d6ab1cf` (MIT); FreeBSD bhyve `usr.sbin/bhyve/amd64/ps2mouse.c` (BSD-2-Clause; pin the commit when adapting);
cloud-hypervisor `arch/src/smbios.rs` (Apache-2.0 AND BSD-3-Clause; pin the commit when adapting). OpenVMM's files use
crates natemu does not have (`bitfield_struct`, `open_enum`, `inspect`, `input_core`, `futures`, `chipset_device`):
they are expanded by hand into plain Rust, and the provenance header says so. Licence texts go to
`third_party/<project>/`.

**Spec:** the Design section below. References: QEMU's `docs/specs/pvpanic.rst` (register contract, read for facts
only), Linux `drivers/misc/pvpanic/pvpanic.c` (what the first guest reads: `capability` and `events` print `%x`),
DMTF DSP0134 (SMBIOS 3.x), the IBM PS/2 Technical Reference and the 8042 command set, ACPI 6.5 5.2.9.3
(IAPC_BOOT_ARCH) and 6.4 (resource descriptors).

## Global constraints

- Behaviour is justified by the specifications and a real PC (QEMU's `pc` board as its stand-in), never by one
  kernel; Linux is a test, not a requirement.
- Defaults: `machine.pvpanic`, `machine.smbios` and `machine.ps2` are `true` in the CLI (owner may flip before Task 5
  lands). The test driver `natemu-host` (which has no `--set`) defaults all three off and gains explicit flags
  `--pvpanic`, `--smbios` and `--ps2` (its `boot-linux` run and `--load-only` dump alike), so every test and bench case
  that starts `natemu-host` keeps today's machine. With all three off, the ACPI tables, the E820 map, the device
  registry and guest memory are byte-for-byte today's.
- With `machine.ps2 = false`, port 64H keeps today's reset-only behaviour (`pm.rs` KBD_REG); the reset command FEH
  ends the run with `Stop::Power(pm::RESET)` with PS/2 on or off.
- Benchmark conditions do not change (the QEMU controls run `i8042=off`: `tools/bench/run.py:97`,
  `tools/bench/boot.py:60`, `tests/io/run_io5.py:347`). Bench cases that start `natemu-host` are unchanged by its
  defaults. Bench cases that start the `natemu` CLI get `--set machine.pvpanic=false --set machine.smbios=false --set
  machine.ps2=false` from the bench harness (`testkit/harness/bench.py`), and only for a build whose source tree's
  `crates/cli/src/config.rs` names `machine.ps2` (a text check of the tree the build came from, never running it), so
  `TESTKIT_BENCH_AT` baselines, `m1-perf`'s baseline commit and any A/B against an older commit still run. No
  benchmark number moves and no A/B is owed. Turning the devices on in benchmarks, with a matching control, is a later
  decision.
- New dependencies are allowed when judicious (owner, November 24): name each, with its reason, in the commit that adds it; never a project's internal workspace crates. (As built, Task 4 expanded OpenVMM's `bitfield_struct` and `open_enum` uses by hand under the earlier rule; kept.)
- Rust unit tests run in the framework as one unit (`tools/natemu-test run 'tests/unit/family.toml::rust/all'`; `-k
  rust` also works but adds the qcow, FP and FS sweeps); a step's named tests are read from that run's
  `.natemu/runs/<id>/junit.xml`. The steps below write `-k rust` for short. Runner cases are selected with `-k`.
- Each new check is shown to fail against a deliberate break of the behaviour it pins (named in its step), not only at
  a commit where the feature is absent or does not compile.
- Tests only through `tools/natemu-test`; the gate passes before merge (this changes what natemu does, including the
  runtime).
- Branch `platform-devices` off `m2-peripherals`, the integration branch it merges back into after its gate. Commit with pathspecs. This track lands first of the three sibling plans
  (multi-BAR PCI and the virtio device interface rebase onto it).

## Review Focus

- A Linux DMI scan of F0000H-FFFFFH with `memcpy_fromio` (`rep movsl`, `movsw`, `movsb`): every read form must run
  without a stop; stores to the page are dropped (Task 3 runtime test `string_reads_of_legacy_rom_run_natively`;
  break: the page registered as a `Device` instead, which stops with `FORM`).
- Host input while the guest is idle in HLT: a queued key must reach the output buffer and raise IRQ 1 from the host
  thread, waking the vCPU (Task 4 `host_key_raises_irq1_without_a_guest_read`; break: fill only on guest reads).
- A keyboard command arriving while the output buffer is full: the 8042 queues or drops as the controller does,
  never stops the vCPU (Task 4 `command_with_full_output_buffer`).
- `reboot=k` with PS/2 on: Linux's keyboard-controller reboot still ends the run with `reset` (the existing
  `io-coverage` case `reboot-kbd` runs with the new default; Task 4 runs it).
- 64 vCPUs (`MAX_VCPUS`): 64 type 4 structures still fit the space after the ACPI tables (Task 3
  `table_fits_for_64_vcpus`).

## Design

### pvpanic

- **Guest-visible:** an ISA device at port 505H (one byte), ACPI `Device (PEVT)` with `_HID` string `"QEMU0001"`,
  `_CRS` `IO (Decode16, 0x505, 0x505, 1, 1)`, `_STA` 0x0F. Read returns the supported events, bit 0 PANICKED and bit 1
  CRASH_LOADED (value 3); bit 2 SHUTDOWN is not offered (S5 already ends the run). A write keeps only supported bits.
- **Host behaviour:** PANICKED sets a new power request `pm::PANIC` (= 4): the run ends with `Stop::Power(4)`.
  Every nonzero power request after a port access takes the same quiesce and drain (`execution.rs`, REQUEST_PORT then
  REQUEST_POWER_POLL), so a panic drains disks exactly as a reset does; power requests are last-writer-wins
  (`pm.rs`, `debug_exit.rs`), so a panic racing a reset on another vCPU ends with whichever was stored last, both
  being typed stops. `program::power_name` names 4 `"panic"`; the CLI exits 2 as for any stop that is not S5.
  CRASH_LOADED (the guest is about to run its crash kernel) is counted (`crash_loaded`, printed in the run report
  beside the access counts) and the run continues. Unsupported bits are ignored.
- **Placement:** installed beside the PM block for both prepare paths (direct and firmware boot). Written here (about
  60 lines); no adaptation.

### SMBIOS

- **Tables:** SMBIOS 3.0 structures built at load time into the ACPI NVS range after the ACPI tables, 16-byte
  aligned, passed to the loader with the ACPI blobs (`Inputs`, `Loaded::writes`) on both load paths (`crates/host`
  `linux.rs` and the driver's load-only path, `crates/driver/src/drivers/linux.rs`). Structures, values chosen per
  DSP0134 and, where it leaves a choice, QEMU's `pc`:
  - Type 0: vendor `"natemu"`, version `natemu <crate version>`, release date a constant `"11/23/2026"`,
    characteristics 0x08 (not supported), extension byte 2 bit 4 (virtual machine).
  - Type 1: manufacturer `"natemu"`, product `"natemu x86-64"`, version the crate version, UUID all zero (not set),
    wake-up type 6 (power switch), family `"natemu"`.
  - Type 3: manufacturer `"natemu"`, type 0x01 (Other), states 0x03 (safe).
  - Type 4, one per vCPU: socket `"CPU <n>"`, type 0x03 (central), family 0x01 (Other), processor ID from the CPU
    profile's CPUID leaf 1 EAX and EDX, manufacturer `"natemu"`, max and current speed 2000 MHz, status 0x41
    (populated, enabled), core and thread count 1.
  - Type 16: location 0x01, use 0x03 (system memory), error correction 0x06 (as QEMU), maximum capacity in KiB, or
    0x8000_0000 with the extended field from 2 TiB, devices = ceil(RAM / 16 GiB).
  - Type 17, one per 16 GiB chunk: size in MiB (extended size above 32767 MiB), form factor 0x09 (DIMM), type 0x07
    (RAM), locator `"DIMM <n>"`.
  - Type 19, one per RAM extent of the boot layout: `[0, low_end)` and, when present, `[4 GiB, 4 GiB + high)`.
  - Type 32: boot status 0 (no errors). Type 127: end.
- **Entry point:** the 24-byte SMBIOS 3.0 entry point (`"_SM3_"`, version 3.0.0, entry point revision 1, structure
  table maximum size and 64-bit address), checksummed, at F0000H. **A read-only legacy ROM page:** the legacy hole's
  backing (`exec::LEGACY_HOLE`: its alias exists but is never used) holds the page F0000H-F0FFFH, written by the host at
  load; a `Control` field names the range (`legacy_rom: (u64, u64)`, `crates/abi/src/execution.rs`); the runtime's
  `mmio::rom` answers true for it as for the firmware image, so `readable_pages` installs a read-only shadow (onto the
  hole's alias instead of the image) and every read form runs natively, stores are dropped. Only the direct boot path
  sets it; a firmware boot gets SMBIOS from its firmware (M3).
- **Adaptation:** cloud-hypervisor's builder supplies the structure and string encoding; natemu adds types 3, 4, 16,
  17, 19 and 32 and drops what it does not use (its OEM-strings type 11 stays, unused, for a later systemd credential
  path).

### PS/2 (8042 controller, keyboard, mouse)

- **Guest-visible:** ports 60H (data) and 64H (status/command), IRQ 1 (keyboard) and IRQ 12 (mouse), both ISA edge.
  ACPI: `Device (KBD)` `_HID` EISA `PNP0303`, `_CRS` `IO (Decode16, 0x60, 0x60, 1, 1)`, `IO (Decode16, 0x64, 0x64, 1,
  1)`, `IRQNoFlags () {1}`; `Device (MOU)` `_HID` EISA `PNP0F13`, `_CRS` `IRQNoFlags () {12}`. FADT IAPC_BOOT_ARCH
  gains bit 1 (8042): 0x0007 instead of 0x0005.
- **Controller and keyboard** adapted from OpenVMM: self-test, interface tests, command byte (interrupt enables, XLAT
  translation to set 1, port disables), output-port writes and pulse commands (a reset pulse sets `pm::RESET`, the
  same stop as today), keyboard reset/ID/LEDs/typematic/scan-set commands. Stripped: `inspect`, save/restore,
  `ChangeDeviceState`, the resolver, the async `InputSource`.
- **Mouse** adapted from bhyve's `ps2mouse.c` (OpenVMM's is a stub that only acknowledges): reset (FA AA 00), defaults,
  enable/disable reporting, sample rate and resolution (with argument bytes), scaling, status request (three bytes),
  stream and remote mode, read data, get ID (00, or 03 after the IntelliMouse sample-rate sequence 200, 100, 80), wrap
  mode.
- **Input from the host:** a `Ps2Input` handle (cloneable, `Send`): `keyboard(&self, set2: &[u8])` and
  `mouse(&self, dx: i16, dy: i16, buttons: u8)` take the device's lock, queue the bytes, and if the output buffer is
  empty move the next byte in and raise IRQ 1 or 12 as the command byte allows, from the calling thread, as the UART's
  receive side does (`uart.rs` `feed`); guest reads of 60H refill from the queue. Nothing produces input yet (the
  display front end will); the handle exists for that and for tests. A future producer holds an `Irqs::busy` guard
  while input can still arrive, as the UART feeder does, or a run whose vCPUs are all halted ends before the key.
- **Disabled:** with `machine.ps2 = false` the 8042 is not installed, the DSDT has neither device, IAPC_BOOT_ARCH
  stays 0x0005 and port 64H is `pm.rs`'s reset-only register, as today.

### Configuration

`[machine]` keys `pvpanic`, `smbios`, `ps2` (booleans), overridable with `--set machine.<key>=false`; listed in
`docs/configuration.md`'s defaults table and the `--set` key list; `crates/cli/src/main.rs` passes them to `Boot`. A
firmware boot ignores `smbios`.

## File structure

| File | Responsibility |
| --- | --- |
| `crates/host/src/pvpanic.rs` (new) | the pvpanic register, its install function, the `crash_loaded` count |
| `crates/host/src/boot/smbios.rs` (new, adapted) | SMBIOS structure and entry-point builder |
| `crates/host/src/ps2.rs`, `ps2/keyboard.rs`, `ps2/mouse.rs` (new, adapted) | 8042, keyboard, mouse, `Ps2Input`, install |
| `crates/host/src/lib.rs`, `crates/host/src/boot.rs` | module declarations; SMBIOS placement; legacy ROM bytes in the backing |
| `crates/abi/src/execution.rs`, `crates/runtime/src/execution/mmio.rs` | `Control::legacy_rom`; `mmio::rom` and the read-only shadow for it |
| `crates/host/src/pm.rs`, `crates/host/src/clock.rs` | `PANIC`; `clock::install` takes `ps2: bool` for the 64H registration |
| `crates/host/src/boot/acpi.rs` | DSDT entries, IAPC_BOOT_ARCH, `Board { pvpanic, ps2 }` argument to `build_tables` |
| `crates/host/src/linux.rs` | `Boot` fields and defaults, installing the devices in both prepare paths, the report's count |
| `crates/driver/src/drivers/linux.rs` | the load-only path builds tables with `Board` and SMBIOS, with the same defaults and flags |
| `crates/driver/src/drivers/program.rs` | `power_name` for `PANIC` |
| `crates/cli/src/config.rs`, `crates/cli/src/config/tests.rs`, `crates/cli/src/main.rs`, `docs/configuration.md` | the three keys |
| `tools/models/acpi_model.py` | PNP0303, PNP0F13 and QEMU0001 with their resources; the 8042 flag only with PNP0303 |
| `tests/boot/run_l2.py`, `tests/boot/run_l2_iasl.py` | dumps with the devices on (default) and off |
| `tests/devices/` (new family) | `family.toml`, `run_platform.py`, `tests.lock` |
| `testkit/inputs.py` | `platform_inputs()`: the BusyBox initramfs with stock modules for `run_platform.py` |
| bench case commands (`testkit/harness/bench.py` and the bench runners it starts) | the three `--set ...=false` |
| `third_party/openvmm/`, `third_party/freebsd-bhyve/` | licence texts |

## Tasks

### Task 1: pvpanic, the `panic` stop and the `Board` argument

**Files:** create `crates/host/src/pvpanic.rs`; modify `lib.rs`, `pm.rs`, `linux.rs` (both prepare paths, `Boot`,
the report), `boot/acpi.rs`, `crates/driver/src/drivers/{program.rs,linux.rs}`, `crates/cli/src/{config.rs,main.rs}`,
`config/tests.rs`, `tools/models/acpi_model.py`, `tests/boot/run_l2.py`, `tests/boot/run_l2_iasl.py`.

**Interfaces:**
- Produces: `pub const PANIC: u8 = 4` in `pm.rs`; `pub const PORT: u16 = 0x505` and `pub(crate) fn install(devices:
  &mut Devices) -> Result<Arc<PvPanicStats>>` in `pvpanic.rs` (`PvPanicStats { crash_loaded: AtomicU64 }`);
  `pub struct Board { pub pvpanic: bool, pub ps2: bool }` in `acpi.rs`, a parameter of `build_tables`
  (`Board::default()` is all false; callers pass the configured values).

- [ ] **Step 1: unit tests** in `pvpanic.rs`: `read_reports_panicked_and_crash_loaded` (505H reads 3),
  `panicked_write_requests_panic` (`devices.power() == pm::PANIC`), `crash_loaded_counts_and_continues` (power 0,
  count 1), `unsupported_bits_are_ignored` (write 04H: power 0, count 0). In `acpi.rs`:
  `dsdt_lists_pvpanic_when_enabled` (the `QEMU0001` string and IO descriptor bytes only with `pvpanic: true`), and
  `default_tables_and_map_are_unchanged` extended to build with `Board::default()`. In `config/tests.rs`: the key
  parses and `--set machine.pvpanic=false` works.
- [ ] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; the new tests FAIL in junit.xml.
- [ ] **Step 3: implement**; the CLI defaults `pvpanic` to true, `natemu-host` to false with `--pvpanic` to turn it on;
  `acpi_model.py` accepts QEMU0001 only with `_CRS` IO 505H length 1.
- [ ] **Step 4: run** the unit tests (PASS); `tools/natemu-test run tests/boot -k l2` (both cases PASS; `run_l2.py`
  and `run_l2_iasl.py` now dump each N twice: as today, and with `--pvpanic` (later tasks add `--smbios --ps2` to
  that dump)). Show the model check fails when the DSDT's IO descriptor says length 2.
- [ ] **Step 5: commit** `pvpanic: a guest panic ends the run with Stop::Power(PANIC)`.

### Task 2: the `devices` test family and the `pvpanic-panic` case

**Files:** create `tests/devices/family.toml`, `tests/devices/run_platform.py`, `tests/devices/tests.lock`; modify
`testkit/inputs.py`.

**Interfaces:**
- `run_platform.py prepare --kernel-deb DEB --kernel VMLINUZ --busybox-deb DEB --out DIR`: builds `platform.cpio`
  (BusyBox plus stock modules `pvpanic_mmio` and `psmouse` with their dependencies from the kernel package, as
  `tests/io/run_virtio_block.py prepare` takes modules) and `inputs.json`, following `run_virtio_rng.py`'s structure;
  `testkit/inputs.py` gains `platform_inputs()` as `rng_inputs()` is built.
- `run_platform.py run --natemu --runtime --fexc --inputs --out --lock --case NAME [--set KEY=VALUE ...]`: one VM per
  case, 120 s each, judged from serial markers and the host's exit line; `self-check` as the other runners.
- `family.toml`: `covers` = the files of this plan's File structure table plus `crates/host/src/devices.rs`,
  `crates/host/src/irq.rs`, `crates/host/src/execution.rs`, `crates/host/src/boot/**`, `tests/devices/**`,
  `tests/lib/**`, `testkit/inputs.py` (not `crates/**`, the narrow selection the testing proposal asks for); smoke
  cases `platform[1]` and `pvpanic-panic`.

- [ ] **Step 1: the case `pvpanic-panic`**: the init loads `pvpanic_mmio`, checks
  `/sys/bus/platform/devices/QEMU0001:00/capability` reads `3`, then `echo c > /proc/sysrq-trigger` with `panic=0`
  on the command line. Judge: stderr ends with `natemu: Power(4)`, exit status 2, serial has `Kernel panic`,
  finished within 30 s of the trigger.
- [ ] **Step 2: show it fails** with `--set machine.pvpanic=false`: the judge reports the missing capability file;
  and with the runner's `--skip-capability` option (kept for this demonstration) the run times out instead of
  stopping with `Power(4)`. It passes with the default.
- [ ] **Step 3: commit** `tests/devices: platform runner and the pvpanic-panic case`.

### Task 3: SMBIOS tables and the legacy ROM page

**Files:** create `crates/host/src/boot/smbios.rs`; modify `boot.rs`, `linux.rs`, `crates/driver/src/drivers/linux.rs`,
`crates/abi/src/execution.rs`, `crates/runtime/src/execution/mmio.rs`, `config.rs`, `config/tests.rs`,
`third_party/cloud-hypervisor/` (provenance line for the adapted file).

**Interfaces:**
- Produces: `pub fn build(input: &SmbiosInput, table_at: u64) -> Smbios` with `SmbiosInput { vcpus: usize, cpuid_1:
  (u32, u32), layout: Layout, version: &'static str }` and `Smbios { table: Vec<u8>, entry: [u8; 24] }`;
  `pub const ENTRY_POINT: u64 = 0xF_0000`; `Control::legacy_rom: (u64, u64)` (start, end; `(0, 0)` none).

- [ ] **Step 1: unit tests** in `smbios.rs`: `entry_point_checksums_to_zero`, `entry_point_names_table` (anchor
  `_SM3_`, length 0x18, version 3.0.0, address and maximum size match `table`), `structures_are_well_formed` (handles
  unique, each string set double-NUL terminated, type 127 last), `one_processor_per_vcpu`,
  `memory_matches_layout` (type 19 ranges equal the layout's RAM extents; type 17 sizes sum to the RAM),
  `table_fits_for_64_vcpus`. Runtime: `string_reads_of_legacy_rom_run_natively` and `stores_to_legacy_rom_are_dropped`
  in the runtime's own tests beside the firmware-image ROM tests.
- [ ] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; FAIL.
- [ ] **Step 3: implement**: the table after the ACPI tables on both load paths; the entry point written into the
  hole's backing page by a new `Backing::write_legacy_rom(&self, gpa: u64, bytes: &[u8])` (`GuestRam` excludes the
  holes, so the loader's RAM writes cannot reach it; the backing maps the whole span, holes included, at
  `ram_offset + gpa`), with an assertion that the page lies in `exec::LEGACY_HOLE` and inside the VM's mapping and the
  resident's alias; `Control::legacy_rom` set when SMBIOS is on; `mmio::rom` covers it. A unit test reads the bytes
  back through the resident-side alias address the runtime will shadow, in a prepared (not load-only) backing.
- [ ] **Step 4: run** the unit tests (PASS); `tools/natemu-test run tests/boot` (PASS: ACPI unchanged by SMBIOS; the
  firmware-image cases unchanged).
- [ ] **Step 5: the case `platform`** in `tests/devices` (1 and 2 vCPUs): dmesg has `SMBIOS 3.0.0 present.` and
  `DMI: natemu natemu x86-64`; `/sys/class/dmi/id/{sys_vendor,product_name,bios_vendor}` read as above
  (`CONFIG_DMI_SYSFS=y` in the guest kernel); `/sys/firmware/dmi/entries/` holds entries of types 0, 1, 3, 4 (vCPU
  count), 16, 17, 19, 32, 127; the run ends with `Power(1)`. Show it fails against the page registered as a `Device`
  (stop `FORM` at the DMI scan) and with a wrong entry-point checksum (no `SMBIOS` line), and passes here.
- [ ] **Step 6: commit** `smbios: SMBIOS 3.0 tables with the entry point in a read-only page at F0000H`.

### Task 4: the 8042, PS/2 keyboard and mouse

**Files:** create `crates/host/src/ps2.rs`, `ps2/keyboard.rs`, `ps2/mouse.rs`, `third_party/openvmm/LICENSE`,
`third_party/freebsd-bhyve/LICENSE`; modify `lib.rs`, `clock.rs` (`install` gains `ps2: bool`), `pm.rs` (docs),
`boot/acpi.rs` (`KBD`, `MOU`, IAPC_BOOT_ARCH), `linux.rs` (both prepare paths), `crates/driver/src/drivers/linux.rs`,
`config.rs`, `config/tests.rs`, `tools/models/acpi_model.py`.

**Interfaces:**
- Consumes: `acpi::Board` (Task 1), `Irqs::line(irq: usize, level: bool)`, the registry's `power: Arc<AtomicU8>`.
- Produces: `pub(crate) fn install(devices: &mut Devices, irqs: &Irqs) -> Result<Ps2Input>`;
  `Ps2Input::{keyboard(&self, set2: &[u8]), mouse(&self, dx: i16, dy: i16, buttons: u8)}`.

- [ ] **Step 1: unit tests** in `ps2.rs` (ports through `Devices::port`): `self_test_answers_55`,
  `keyboard_reset_answers_fa_aa`, `keyboard_id_is_ab_83`, `xlat_translates_set2_to_set1` (queued 1CH arrives as 1EH
  with XLAT, 1CH without), `host_key_raises_irq1_without_a_guest_read` (and not while the command byte's interrupt
  bit is clear), `mouse_reset_answers_fa_aa_00`, `intellimouse_sequence_makes_id_3`,
  `mouse_packet_after_movement_raises_irq12`, `pulse_reset_requests_reset` (FEH on 64H: `devices.power() ==
  pm::RESET`), `output_port_reset_bit_requests_reset`, `command_with_full_output_buffer` (no refusal; queued or
  dropped as the controller does), `disabled_ps2_keeps_reset_only_port`. In `acpi.rs`:
  `dsdt_lists_ps2_and_iapc_has_8042`.
- [ ] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; FAIL.
- [ ] **Step 3: implement** (adapted files keep upstream structure where it survives the expansion, with a provenance
  header naming upstream path and commit); `acpi_model.py`: the 8042 bit is valid only with exactly one PNP0303 at
  60H/64H IRQ 1 and one PNP0F13 at IRQ 12.
- [ ] **Step 4: run** the unit tests (PASS); `tools/natemu-test run tests/boot -k l2` (PASS, the second dump now with
  `--pvpanic --smbios --ps2`); show the model rejects IAPC_BOOT_ARCH 0x0007 in a dump with PNP0303 removed; `tools/natemu-test run tests/io
  -k "io-coverage"` (`reboot-kbd` PASS: `Power(2)` with PS/2 on).
- [ ] **Step 5: extend the `platform` case**: after `modprobe psmouse`, `/proc/bus/input/devices` lists `AT Translated
  Set 2 keyboard` on serio0 and a PS/2 mouse on serio1; dmesg has no `i8042: Can't read CTR` and no `psmouse ...
  failed`. Show it fails with the mouse's get-ID reply removed (psmouse probe fails) and passes here.
- [ ] **Step 6: commit** `ps2: an 8042 with a PS/2 keyboard and mouse (adapted from OpenVMM and bhyve)`.

### Task 5: defaults, the disabled path, benchmarks and the gate

**Files:** modify `docs/configuration.md`, `README.md` (one sentence: a guest kernel panic now ends the run),
`tests/devices/family.toml`, the bench case commands.

- [ ] **Step 1: the case `platform-off`**: `--set machine.pvpanic=false --set machine.smbios=false --set
  machine.ps2=false`: no `DMI:` line, no `/sys/firmware/dmi/entries`, no serio devices, no `QEMU0001`; `reboot -f`
  with `reboot=k` ends with `Power(2)`.
- [ ] **Step 2: bench commands**: the harness rule of Global constraints, with a unit test of `testkit/harness/bench.py`
  (a source tree with the key gets the three switches; one without gets none). List, in the commit message, which
  bench cases start `natemu` and which `natemu-host`; check one smoke bench run's recorded commands
  (`--keep-outputs`), and one `TESTKIT_BENCH_AT=<this branch's base>` round, which must start without the switches.
- [ ] **Step 3: run** `tools/natemu-test run tests/devices`, then `tools/natemu-test run --gate`; PASS. A failure
  from the new default in another family is fixed here, not by turning the default off.
- [ ] **Step 4: docs**: `docs/configuration.md` rows for the three keys; this plan's Status line says what was built
  and where it departed.
- [ ] **Step 5: commit** `platform devices: defaults on, configuration, benchmarks unchanged`.

## Out of scope

A host input source for PS/2 (display front end, MU); SMBIOS through fw_cfg for firmware boots (M3); SMBIOS OEM
strings for systemd credentials (with vsock); the PCI form of pvpanic; HPET; turning the devices on in benchmarks.
