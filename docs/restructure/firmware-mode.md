# natemu's firmware boot mode (boot-time interpreter plan, section 7)

Status: November 22, 2026, the design; nothing built. Spec: [the plan](m2-boot-interpreter-plan.md) sections 4, 6 and 7,
and the contracts [m2-exit-firmware.md](m2-exit-firmware.md) (the exit test's firmware), [real-code-suites.md](real-code-suites.md)
(OVMF) and [interp-integration.md](interp-integration.md) (the interpreter's memory layer).

## 1. The region and its mapping

- **The image** (nonzero, a multiple of 4 KiB, at most 16 MiB) is guest-physical [4 GiB - size, 4 GiB): inside the PC
  hole above 3 GiB, so disjoint from RAM, and above the I/O APIC (FEC00000H) and the local APIC (FEE00000H). OVMF is
  FFC00000H-FFFFFFFFH, the exit test's FFFF0000H-FFFFFFFFH. No copy below 1 MiB (nothing reads F0000H); the 2 MiB blocks'
  bytes below the image stay unassigned.
- **A guest-RAM region in the fourth slot.** `GuestRam` holds four regions; RAM uses at most three ([0, A0000H),
  [100000H, low end), and [4 GiB, ...) only above 3 GiB of RAM). The image is the fourth, marked read-only
  (`GuestRam::rom`): `range()` finds it like RAM (reads, fetches, shadow installs), and GuestRam's own stores (`write`,
  `copy_in`, `compare_exchange64`) drop a store into it and report it made, as x86 drops a store to ROM (a page walk's
  A/D update of a table in ROM included). `exec::guest_memory(ram, firmware, va)` builds it; the resident's `guest_ram()`
  passes `cache::Control::firmware_bytes` (appended; 0, the default, is none), the host's `Backing::ram` the same.
- **Backing.** With a firmware the backing's RAM span covers [0, max(ram_end(R), 4 GiB)) (address space only; untouched
  pages cost nothing), so every guest page's backing offset stays ram_offset + gpa, the image's too. The host copies the
  image there in `prepare`, before any vCPU runs, through the backing rather than GuestRam. The resident's alias maps the
  2 MiB blocks holding it read-only at EL1 (`Span::read_only`): a writer that misses its check halts the resident instead
  of changing the ROM. Shadows map it through its IPA as RAM is (`tables::install`).
- **Fetch:** `fetch_walk`'s `guest_ram().range` and the interpreter's `fetch_page` find it unchanged; the host reads the
  bytes for a translation through its GuestRam.
- **Per-page tables: unchanged** (`cache::pages()` stays ram_end(R); the plan said they must reach 4 GiB). The ROM is
  never written, so its pages need no code marks, chains or writable bits; below 3 GiB of RAM they lie past the tables,
  which the cache already reads as "no code to invalidate, counts as marked" (`mark`, `marked`, `invalidate`, `file`'s
  chain); above 3 GiB they are tracked as any page.

## 2. Each writer's check

`mmio::rom(gpa)` (GuestRam's read-only test). The ROM is RAM for reads and for every classification (`mmio::ram`); every
store to it is dropped:

- **Through GuestRam's stores, with no edit:** `copy_in` in INS, in `split_lock`'s emulated read-modify-write and in
  page-straddling slots; the walker's A/D compare-exchange; the host's writes.
- **Explicit**, before a raw store or the code-write protocol: `segments::copy` (a ROM chunk skips `code_write`, the
  store and `rechecked`: SGDT to ROM, a delivery's push); `segments::atomic_byte` (applies `update` to the byte read,
  writes nothing: an accessed bit in a ROM GDT stays clear, as on x86); the raw stores of resident-executed instructions
  in `aligned_operand` (far pointers and frames; not the far-transfer functions), `stack.rs`, `rotate.rs` and `half.rs`
  (their ROM part not stored).
- **The interpreter's memory layer:** a third class beside RAM and unassigned, ROM (bit 1 of `Mem::pages`' entries):
  read as RAM; a store dropped, LOCK's compare-exchange returning the value read and writing nothing, the code-write
  protocol skipped.
- **The shadows** (translated 64-bit code: the exit test's from the ROM, OVMF's SEC and later):
  - `translate`: a read of a ROM page is shadowed read-only, never writable on read; a write is not shadowed (no writable
    bit, no code write) and returns the page as a device page's. `install` never maps a ROM page writable (the belt).
  - `access_aborted`: a write touching a ROM page takes the device path. A plain store (a STORE site with its completion
    record) completes with nothing stored and RIP past it, answered by `mmio::local` (no host exit). LOCK and XCHG (SWAP
    and ATOMIC sites) go to `split_lock`, whose emulation stores through `copy_in` (dropped): registers and flags exact.
    The device path's refusals stay for the other forms (store pairs, pushes, a store crossing a page, read-modify-write
    forms without a completion record such as `add [rom], eax`): `unsupported_mmio` before any effect, a recorded
    deviation for code that writes its ROM so. (If a workload needs them: run the instruction alone with its ROM page
    shadowed writable onto a scratch copy for that instruction, as a UC page is shadowed for the step block.)
  - The page-crossing store's completion (the split stub) and `vector_aligned` store raw: their ROM parts are skipped.

## 3. The start states

- **vCPU 0:** `natemu_abi::power_on::power_on` (CS F000H base FFFF0000H limit FFFFH, EIP FFF0H, CR0 60000010H, EFLAGS 2,
  EDX the signature) with `cpu::power_on_msrs(true)`: IA32_MTRR_DEF_TYPE 0 and no valid variable range (not
  `MTRR_INITIAL`), IA32_APIC_BASE FEE00900H. A power-on variant of `execution::set_cpu` writes them (today's applies
  `reset_msrs` and `firmware_mtrrs`; its callers are unchanged). `start()` enters the interpreter.
- **`flat32`** (OVMF's `Main32`, the start TDX gives): the same but CR0 60000011H, CS 08H base 0 limit FFFFFFFFH
  attributes C09BH, SS DS ES FS GS 10H base 0 limit FFFFFFFFH C093H, EIP FFFFFFF0H.
- **vCPUs 1..N-1:** the power-on state with `power_on_msrs(false)` (APIC base FEE00800H), published waiting for a SIPI
  before any vCPU runs (`execution::wait_for_sipi`). No mailbox, no ACPI tables, no e820, nothing in RAM.
- **The board:** COM1, the I/O APIC and 8259s, the PIT, the RTC, the PM block (PM1a_CNT at B004H: power-off), the
  debug-exit port if asked. No virtio device or PCI bus (refused).

## 4. The options

- **Library:** `linux::Boot::firmware: Option<boot::firmware::Firmware { image, start }>` (`start`: `Reset` or `Flat32`),
  `kernel` empty; `prepare` then skips the kernel loader, the tables and the mailbox.
- **natemu:** `--firmware FILE` (refused with `--kernel`, `--initrd`, `--cmdline` or a device); the file's
  `[boot.firmware]` `image` and `start = "reset" | "flat32"` (`--set boot.firmware.start=flat32`); `[boot]` takes one of
  `kernel` and `firmware` (`uefi` stays M3's). docs/configuration.md.
- **Test driver:** `natemu-host RUNTIME boot-linux FEXC IMAGE --firmware [--firmware-start flat32]`, with boot-linux's
  report (stop, final state, per-vCPU stops and counters, ports and unassigned pages), `--stop-at`, `--serial`,
  `--vcpus`, `--ram`.
- **Program driver:** the item `firmware HEX`: the image at 4 GiB - its size; an `identity` space maps its pages writable
  and executable (a store then meets the ROM rule, not a #PF).

## 5. Tests

- **What runs now** (`natemu-machine`, tests/interp): from the power-on state, the reset vector fetched from the ROM and
  real-mode code storing to its ROM (MOV, LOCK BTS) and reading the old bytes back, to a stop; the same code run on to a
  form outside the first version (its typed stop and state); in 64-bit mode at CPL 0, over tables mapping the ROM, a
  translated MOV store, SGDT (`segments::copy`), LOCK XADD and XCHG (`split_lock`) to the ROM, and code fetched from the
  ROM storing to its own page, each read back unchanged.
- **`exit-fw-natemu`** (1, 2, 4 CPUs; `run_exit_fw.py --variant firmware --backend natemu`): `judge('firmware', N, answer,
  want_exit='poweroff')` on COM1 and the driver's stop, and bounds on decode exits and shootdowns per vCPU (from the
  report's counters; a decode count added to it if missing). **`ovmf-natemu`** (reset and `main32`; `run_ovmf.py
  --backend natemu`): `--stop-at` SEC's entry; the registers from the final state and the devices from the ports and
  unassigned pages (none), compared with the locked KVM answer and judged by `run_ovmf.judge` without its debug-port line
  (a parameter, on for the references: natemu stops at SEC's entry, and RBP already names the boot firmware volume).
  Until the far JMP, segment loads and MOV CR land, each passes a stage like `exit-ap-natemu`'s, the BSP's typed stop at
  its first unbuilt operation with its state (exit test and OVMF `reset`: `interp_unsupported` mov_to_cr at their first
  `mov cr0, eax`, found by ndisasm in the image as built, with CS F000H and GDTR loaded; `main32`: far_jump at its far
  JMP); the stage goes when that branch lands.
- **Default boots:** `boot-smoke`, `l1`, `l2`, `boot-i386`, `exit-ap-natemu`, interp `natemu` and `natemu-machine`,
  system `initsipi`; the gate before merge; a benchmark round with the hackbench cases against the parent.

## 6. Files

abi `guest_memory.rs`, `execution.rs` (the layout constants, `guest_memory`, `Control::firmware_bytes`); runtime
`execution.rs` (`guest_ram`, `translate`, `install`, `access_aborted`, the split store, `vector_aligned`), `mmio.rs`,
`segments.rs` (`copy`, `atomic_byte`, `aligned_operand`), `stack.rs`, `rotate.rs`, `half.rs`, `interpret.rs` (`Mem` and
`fetch_page`'s comment only); host `execution.rs` (`prepare`, the power-on `set_cpu`), `boot/firmware.rs` (new),
`linux.rs`, `devices.rs` (no device range over the image); driver `linux.rs`, `program.rs`; cli `main.rs`, `config.rs`
and its tests; tests/boot `run_exit_fw.py`, `run_ovmf.py`, `family.toml` and their unit tests; tests/interp
`run_interp_natemu.py`; docs/configuration.md and this page.

## 7. Risks for today's boots

- `GuestRam` grows by two words and its stores by a compare; the resident's `guest_ram()` loads `firmware_bytes` and
  branches, so `fetch_walk`, `translate`, `segments::pages` and the walker's callers differ from the parent's
  disassembly (interp-integration's list). Not on the fast system-call path or `find`'s hit path; the benchmark round
  decides (expected inside noise).
- `translate` and `access_aborted` gain a compare per shadow miss and abort; `copy` one per chunk.
- `Control` gains an appended word; the resident's fields keep their places.
- Without a firmware, the host's spans, backing size, Control values, MSRs and the kernel path are today's.
- `interpret.rs` and `segments.rs` are shared with the far-transfer work: only `Mem`, `copy`, `atomic_byte` and
  `aligned_operand` change here, not `resident()` or the far-transfer and descriptor functions.
