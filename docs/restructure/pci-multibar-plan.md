# Multi-BAR, multi-function PCI: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: revised November 23, 2026, after an independent review (accept with required changes: four blocking findings
on test selection, an undecoded test device, an ACPI check that saw no PCI bus, and the I/O straddling rule, all
folded in below); a second check confirmed those and found the decode A/B named an unusable bench case: folded in. Owner direction (same day): plan multi-BAR PCI now, beside the platform devices and the virtio
device interface, because display, SATA, NVMe and IDE need it (roadmap M3 P1).

**Goal:** a PCI function may have up to six BARs of any kind (32-bit memory, 64-bit memory, I/O), and a slot up to
eight functions, so later devices (a Bochs-style display: BAR0 and BAR2; AHCI: BAR5 and I/O BARs; NVMe: MSI-X in BAR4;
IDE bus-master: an I/O BAR; a firmware PM function: function 1) can be built without touching the bus again.

**Architecture:** the bus (`crates/host/src/pci/mod.rs`) keeps one registered memory range for its window and, when a
function asks for an I/O BAR, one registered I/O-port range; both route each access to the function and BAR that
currently decode it, so the registry stays frozen while a guest sizes and moves BARs. Launch-time BAR addresses come
from a small allocator that keeps every existing virtio function's BAR0 where it is today. The configuration-space
code adapted from cloud-hypervisor (`pci/configuration.rs`) already models six BARs, I/O and 64-bit BARs and MSI-X
tables in any BAR (`msix.rs`); its BAR write masking is completed by the bus's decode (below).

**Tech stack:** Rust, `crates/host`. No new dependencies, no new adapted source.

**Spec:** the Design section below; PCI Local Bus 3.0 (6.1 header, 6.2.1 multi-function, 6.2.5 BARs, 3.2.2.3.2
configuration mechanism #1), ACPI 6.5 6.4.3.5 (address space descriptors). Reference reading only (MIT): OpenVMM
`vm/devices/pci/pci_core` and `pci_resource_assignment` at `5d6ab1cf`. The test function follows QEMU's `pci-testdev`
(`docs/specs/pci-testdev.rst`, and QEMU's device as a behaviour reference for its test table, as natemu's other
QEMU-modelled devices are; no code copied).

## Global constraints

- Default launch unchanged: with no new device, the ACPI tables, the E820 map, every virtio function's configuration
  space and BAR0 address, the registered port ranges and every existing test are byte-for-byte what they are at the
  base this branch is rebased onto. The I/O window is registered, and the root bridge's `_CRS` lists it, only when some
  function requests an I/O BAR.
- The registry stays frozen at launch (`Devices::freeze`); the bus never registers or moves a range after install.
- Guest-visible behaviour follows the PCI specification; where it leaves a choice, QEMU's `pc` board.
- Lock order unchanged: virtio state lock, then a function's configuration lock, then MSI-X, then chipset. BAR bases
  and decode enables stay published in atomics for lock-free routing.
- Port accesses stay synchronous (`Device::read`/`write`): an I/O BAR handler never defers.
- Rust unit tests run in the framework as one unit (`tools/natemu-test run tests/unit -k rust`, item `rust/all`); a
  step's named tests are read from that run's `.natemu/runs/<id>/junit.xml`. Runner cases are selected with `-k "a or
  b"` (one `-k`).
- Each new check is shown to fail against a deliberate break of the behaviour it pins (named in its step).
- Benchmarks: `Window::decode` is on every MMIO access to the PCI window (virtio notifies included), so Task 1 runs an
  A/B (below); the page changes only if a number moves beyond its noise.
- Tests only through `tools/natemu-test`; the gate passes before merge.
- Branch `pci-multibar` off `m2-peripherals` (the integration branch it merges back into after its gate), rebased onto the platform-devices track when it lands (both edit `acpi.rs`,
  `linux.rs`, `config.rs`, `acpi_model.py`; the `PCI0_CRS` constant test will conflict); the virtio device interface
  plan rebases onto this one (both edit `virtio/pci.rs`). Commit with pathspecs.

## Review Focus

- A guest sizing a BAR (writing all-ones, reading back) while another vCPU accesses it: the access goes to the old
  base or decodes nothing, never to another function (Task 1 `sizing_probe_decodes_nothing`; break: decode from the
  raw register without the size mask).
- A 64-bit BAR whose upper half is written before its lower half, or the reverse: no transient decode outside the
  window reaches a function (Task 1 `mem64_halves_written_in_either_order`).
- Two BARs a guest programs to overlap: routing stays deterministic, lowest (slot, function, BAR) first (Task 1
  `overlapping_bars_pick_lowest`).
- An I/O access that straddles the end of an I/O BAR: `IoWindow` splits it into bytes and decodes each; bytes inside
  the BAR are served, bytes outside any BAR read FFH and drop writes (Task 2 `io_access_straddling_bar_end`).
- Functions 1-7 without function 0: refused at `Bus::add` (PCI 3.0 6.2.1) (Task 1
  `function_without_function_0_is_refused`).

## Design

### Function interface

```rust
pub enum BarKind { Mem32, Mem64, Io }
pub struct Bar { pub kind: BarKind, pub base: u64, pub len: u64 }
pub trait Function: Send + Sync {
    fn config_read(&self, reg_idx: usize) -> u32;
    fn config_write(&self, reg_idx: usize, offset: u64, data: &[u8]);
    /// The BARs as they decode now: base masked with !(len - 1), the Command register's memory (BarKind::Mem*) or
    /// I/O (BarKind::Io) enable set; lock-free.
    fn bars(&self) -> [Option<Bar>; 6];
    /// Launch-time BAR requests: (index, kind, len), len a power of two (memory at least 16, I/O at least 4).
    fn bar_requests(&self) -> Vec<(u8, BarKind, u64)>;
    /// Program BAR `index` to `base` at launch, decoding still off.
    fn assign(&self, index: u8, base: u64);
    fn bar_access(&self, vcpu: usize, bar: u8, offset: u64, size: u64, value: Option<u64>) -> Access;
    fn bar_poll(&self, vcpu: usize, bar: u8, token: u64) -> Access;
    fn quiesce(&self) -> crate::Result<bool>;
}
```

A 64-bit BAR occupies index n and n+1; `bars()[n+1]` is `None`. The bus decodes a memory BAR only when it lies
wholly in the window, an I/O BAR only when base + len <= 0x10000 (cloud-hypervisor's I/O BAR keeps its upper 16 bits
writable). The host bridge has no BARs. The virtio function implements the new methods with BAR0 only (request
`(0, Mem32, 0x8000)`), its `bars()` the value its `bar()`/`bar_len()` give today.

### Slots, functions and configuration cycles

- `Slots` becomes 32 slots of 8 functions. `Bus::add(slot: u8, function: u8, f)`; function 0 before any other in its
  slot; slot 0 function 0 is the host bridge.
- Configuration mechanism #1 honours bits 10:8 (function). Function 0's header-type byte reads with bit 7 (multi-
  function) set when its slot has another function: the bus ORs it into reads of register 3 of function 0 (PCI 3.0
  6.2.1; QEMU sets it on function 0), so functions need not know their neighbours. Absent functions read all-ones,
  as today.
- `_PRT` is unchanged: one entry per slot for INTA (`pci0` takes `(slot, gsi)`); functions of one slot share it. Per-
  pin routing waits for a device that uses INTB-INTD.

### Launch-time placement

- Memory: function 0's memory BAR0 of at most 64 KiB keeps `platform::bar(slot)` (today's 64 KiB stride over the
  window's first 2 MiB), so virtio layouts do not move. Every other memory BAR is placed from `WINDOW.0 + 2 MiB`
  upward, naturally aligned, largest first; 32- and 64-bit memory BARs share the 32-bit window (a 64-bit BAR below
  4 GiB is legal). Exhaustion is a launch error naming the function and BAR.
- I/O: `IO_WINDOW` = C000H..=FFFFH (inclusive, 16 KiB; free on this board: PM at B000H, debug and profile ports at
  F4H-FBH, pvpanic at 505H). I/O BARs are placed in it, naturally aligned, largest first.
- No 64-bit window above 4 GiB and no expansion ROM BAR (no consumer).

### Routing

- Memory: `Bus::install` flattens the present functions into one list; `Window::decode` walks it and each function's
  memory BARs, lowest (slot, function, BAR) first.
- I/O: `IoWindow`, registered with `register_ports(0xC000, 0x4000, ..)` only when an I/O BAR was requested; the same
  walk over I/O BARs. An access lying wholly in one BAR goes to it; otherwise `IoWindow` splits it into bytes and
  decodes each, as the registry does at a range's end. Unclaimed bytes read FFH and drop writes. Stated deviation: the
  registry sees the whole window as owned, so with `strict_unassigned` such bytes are not reported as unassigned and
  the run report counts the window's ports as owned; the window exists only when a function has an I/O BAR (today only
  the test function), so default launches are unaffected.
- Deferred tokens: the function's token keeps its low 52 bits; above them the BAR index (3 bits), function (3 bits)
  and slot (5 bits). Virtio tokens are a per-device counter, far below 2^52; the bus asserts the bound as it asserts
  2^56 today, and the `Function` doc comment and `deferred_tokens_carry_the_slot` change with it.

### ACPI

With an I/O BAR present, `\_SB.PCI0._CRS` gains `WordIO (ResourceProducer, MinFixed, MaxFixed, PosDecode,
EntireRange, 0, 0xC000, 0xFFFF, 0, 0x4000)` (6.4.3.5.3) after the configuration ports' `IO` descriptor.
`tools/models/acpi_model.py` learns `PCI0`'s `_CRS`: the bus-number, CF8 IO, memory-window and (when present) I/O
window descriptors, each with its exact range.

### Test function (test option)

`[debug] pci_testdev = true` (a `[debug]` table key, beside `pool`; off by default, documented as a test option):
two functions at the first free PCI slot, behaving as QEMU's `pci-testdev` (vendor 1B36H, device 0005H): BAR0 memory
and BAR1 I/O with its header and test table. Function 1 also has the optional 64-bit memory BAR2-3 (64 KiB), reading
as the 32-bit little-endian offset within the BAR (natemu's definition; QEMU's optional BAR is unbacked). With the
MMIO transport and no other PCI function, the key installs the bus (as any PCI function does today). No slot free:
launch error.

## File structure

| File | Responsibility |
| --- | --- |
| `crates/host/src/pci/mod.rs` | `Bar`, `BarKind`, the new `Function`, slots of functions, the flattened list, `IoWindow` |
| `crates/host/src/pci/place.rs` (new) | the launch-time allocator, pure and unit-tested |
| `crates/host/src/pci/testdev.rs` (new) | the test function |
| `crates/host/src/virtio/pci.rs` | the virtio function on the new interface (BAR0 only, behaviour unchanged) |
| `crates/host/src/boot/acpi.rs` | `_CRS` I/O window when present |
| `crates/host/src/linux.rs`, `crates/driver/src/drivers/linux.rs` | `Bus::add` call sites; the load-only path can place a PCI function and the test function for L2 dumps |
| `crates/cli/src/config.rs`, `crates/cli/src/config/tests.rs`, `docs/configuration.md` | `debug.pci_testdev` |
| `tools/models/acpi_model.py`, `tests/boot/run_l2.py`, `tests/boot/run_l2_iasl.py` | `PCI0` checks; an L2 dump with PCI functions |
| `tests/devices/run_pci.py`, `tests/devices/family.toml` | the guest case `pci-multibar` (the family the platform-devices plan creates) |

## Tasks

### Task 1: the interface, functions per slot and memory routing

**Files:** modify `crates/host/src/pci/mod.rs`, `crates/host/src/virtio/pci.rs`, `crates/host/src/linux.rs`.

**Interfaces:** produces `Bar`, `BarKind`, the `Function` trait above; `Bus::add(&mut self, slot: u8, function: u8,
f: Arc<dyn Function>) -> Result<()>`; `const TOKEN_BITS: u32 = 52`.

- [ ] **Step 1: unit tests** in `pci/mod.rs` (the test `Stub` grows a BAR list): `bar0_routing_unchanged` (the
  existing routing tests pass with only call signatures and the token width changed), `routes_to_bar_n_with_offset`,
  `mem64_halves_written_in_either_order`, `sizing_probe_decodes_nothing`, `overlapping_bars_pick_lowest`,
  `function_bits_select_function`, `multifunction_bit_on_function_0_only_with_siblings`,
  `function_without_function_0_is_refused`, `token_carries_slot_function_bar` (a deferred access from slot 3
  function 2 BAR 4 polls back to the same BAR).
- [ ] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; the new tests FAIL in junit.xml.
- [ ] **Step 3: implement.**
- [ ] **Step 4: run** the unit tests (PASS) and `tools/natemu-test run tests/io -k "io-coverage-pci or net-linux-pci"`
  (PASS, behaviour unchanged). Show `sizing_probe_decodes_nothing` fails with the size mask removed.
- [ ] **Step 5: A/B** of the decode change, on one machine, alternating three rounds: `TESTKIT_BENCH_AT=<parent>
  tools/natemu-test run --tier bench -k <case>` then the same without `TESTKIT_BENCH_AT` (the checkout), where
  `<case>` is a bench case whose disk and network run on the PCI transport (check `io5-solo`'s transport in
  `tests/io/run_io5.py`; if no bench case uses PCI, first add a PCI variant of `io5-solo` to `tests/bench/family.toml`,
  shown to run). Compare its wall time; record the rounds and their spread in the commit message; the benchmarks page
  changes only if the difference exceeds the spread. (`m1-perf` does not fit: it compares against its fixed baseline
  commit and takes about 80 minutes.)
- [ ] **Step 6: commit** `pci: functions per slot and BARs beyond BAR0 on the bus`.

### Task 2: launch-time placement and the I/O window

**Files:** create `crates/host/src/pci/place.rs`; modify `pci/mod.rs`.

**Interfaces:** `pub fn place(requests: &[Request]) -> Result<Vec<Placed>>`, `Request { slot: u8, function: u8, bar:
u8, kind: BarKind, len: u64 }`, `Placed { request: Request, base: u64 }`; `pub const IO_WINDOW: (u16, u16) = (0xC000,
0xFFFF)` (inclusive); `Bus::install` calls `place`, then `assign`, and registers `IoWindow` when an I/O BAR exists.

- [ ] **Step 1: unit tests** in `place.rs`: `function0_memory_bar0_keeps_slot_stride`,
  `others_naturally_aligned_above_2mib`, `largest_first_no_overlap`, `io_bars_in_io_window`,
  `exhaustion_names_the_bar`. In `pci/mod.rs`: `io_bar_routes_when_io_decoding_on`,
  `io_bar_silent_when_io_decoding_off`, `io_access_straddling_bar_end`, `io_bar_past_64k_does_not_decode`,
  `no_io_window_without_io_bars` (the registry's port ranges equal today's).
- [ ] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; FAIL. **Step 3: implement.** **Step 4: run**; PASS.
- [ ] **Step 5: commit** `pci: launch-time BAR placement and the I/O window C000H-FFFFH`.

### Task 3: the test function, ACPI resources and the L2 dump

**Files:** create `crates/host/src/pci/testdev.rs`; modify `boot/acpi.rs`, `config.rs`, `config/tests.rs`,
`docs/configuration.md`, both `linux.rs`, `tools/models/acpi_model.py`, `tests/boot/run_l2.py`,
`tests/boot/run_l2_iasl.py`.

- [ ] **Step 1: unit tests**: in `testdev.rs`, the header and test-table registers on both BARs of function 0 and
  function 1, and function 1's 64-bit BAR reading its offsets; in `acpi.rs`, `pci0_crs_has_io_window_with_io_bars`
  and `pci0_crs_unchanged_without_io_bars`; in `config/tests.rs`, the key.
- [ ] **Step 2: run** `tools/natemu-test run tests/unit -k rust`; FAIL. **Step 3: implement.**
- [ ] **Step 4: L2**: the driver's `boot-linux --load-only` gains `--pci-testdev` (and places the entropy device on
  PCI with its existing `--rng` plus a `--transport pci` flag if it has none); `run_l2.py` and `run_l2_iasl.py` add a
  dump per N with both; the model checks `PCI0`'s `_CRS` descriptors. Run `tools/natemu-test run tests/boot -k l2`
  (PASS); show the model check fails with the WordIO range ending at FFFEH.
- [ ] **Step 5: commit** `pci: a pci-testdev test function and the root bridge's I/O window`.

### Task 4: the guest case

**Files:** create `tests/devices/run_pci.py`; modify `tests/devices/family.toml` (`covers` adds `crates/host/src/pci/**`,
`crates/host/src/virtio/pci.rs`). This task waits for the platform-devices track, which creates `tests/devices`; if
that track has not landed, this task creates the family itself with the `covers` and layout that plan's Task 2 gives.

**Interfaces:** `run_pci.py prepare` builds a BusyBox initramfs from the framework's inputs as
`tests/io/run_virtio_rng.py prepare` does; it checks `busybox --list` has `devmem`, `hexdump` and `dd` and refuses to
build otherwise; `run_pci.py run --case pci-multibar --cpus N`, judged from serial markers; `self-check`.

- [ ] **Step 1: the case `pci-multibar`** (`--set debug.pci_testdev=true`, 1 and 2 vCPUs): the init finds the two
  functions under `/sys/bus/pci/devices/` by vendor `0x1b36` and device `0x0005`; dmesg has `root bus resource [io
  0xc000-0xffff window]` and no `can't claim`, `no space` or `failed to assign`; for each function `echo 1 > enable`
  (no driver binds, so nothing else turns decoding on), then the Command register in `config` has memory and I/O
  enabled; `resource` lists the placed bases and sizes; `devmem` reads BAR0's header register; `dd if=/dev/port bs=1
  skip=<BAR1> count=4 | hexdump` reads BAR1's (`/dev/port` needs only root's `CAP_SYS_RAWIO`); function 1's BAR2
  reads its offsets. S5 ends the run.
- [ ] **Step 2: show it fails** against three breaks, one at a time: no multi-function bit (function 1 absent), no
  `IoWindow` (BAR1 reads FFH), no WordIO (the kernel reports no I/O window and reassigns or fails BAR1); passes here.
- [ ] **Step 3: run** `tools/natemu-test run` then `--gate`; PASS.
- [ ] **Step 4: commit** `tests/devices: the multi-BAR, multi-function guest case`.

## Out of scope

A 64-bit prefetchable window above 4 GiB, expansion ROM BARs, PCI bridges and more buses, PCIe extended configuration
space (MMCONFIG), plain MSI capability, per-pin `_PRT` (INTB-INTD), BAR placement in the firmware boot path (M3's
firmware assigns BARs itself; routing already follows guest programming).
