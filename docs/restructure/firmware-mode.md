# natemu's firmware boot mode (boot-time interpreter plan, section 7)

Status: November 22, 2026, built (branch `worktree-agent-a5639dd274d60b095`, on `boot-interp` 73aa5b52, with the
interpreter's resident operations). Spec: [the plan](m2-boot-interpreter-plan.md) sections 4, 6 and 7, and the contracts
[m2-exit-firmware.md](m2-exit-firmware.md) (the exit test's firmware), [real-code-suites.md](real-code-suites.md) (OVMF)
and [interp-integration.md](interp-integration.md) (the interpreter's memory layer). The exit test's firmware runs whole
at 1, 2 and 4 CPUs and OVMF's `main32` and `reset` entries reach SEC with KVM's registers (`reset` through its MOV CR4
640H: CR4.MCE is accepted though natemu's CPU does not present MCE, a recorded deviation: section 5).

## 1. The region and its mapping

- **The image** (nonzero, whole 4 KiB pages, at most 16 MiB: `exec::firmware_size`) lies at guest-physical
  [4 GiB - size, 4 GiB) (`exec::FIRMWARE_END`): inside the PC hole above 3 GiB, so disjoint from RAM, and above the I/O
  APIC (FEC00000H) and the local APIC (FEE00000H). OVMF is FFC00000H-FFFFFFFFH, the exit test's FFFF0000H-FFFFFFFFH. No
  copy below 1 MiB (nothing reads F0000H); the bytes of its 2 MiB blocks below the image are unassigned, as on a PC.
- **Not RAM: a region of its own, asked for only where a page is not RAM.** `guest_ram()` and `mmio::ram` are unchanged
  (RAM only), so every RAM path of a running OS is as before (section 6). `mmio::rom(gpa)` (`Control::firmware_bytes`,
  appended; 0, the default, is none) and `mmio::firmware()` (the image's `GuestRam`) are used only on the paths that
  found a page outside RAM. `exec::guest_memory(ram, firmware, va)` is RAM plus the image as the fourth `GuestRam` region
  (RAM uses at most three): the interpreter's memory (`ram_and_rom()`) and the host's `Backing::readable`, the vCPUs'
  view (`Area::ram`) through which the host reads code to translate and reports read the image. Every host writer
  (loaders, device DMA, copy windows) keeps `Backing::ram`, RAM alone, so nothing of the host writes the image either.
  The image is looked for before the local APIC's page: a LAPIC base moved into it (above FF000000H) would read the
  image there (x86's APIC would win; no guest does that).
- **Backing.** With an image the backing's RAM span reaches 4 GiB (address space only: untouched pages are never
  backed), so the image's backing offset is its guest-physical address's as RAM's is; the host copies it there in
  `execution::prepare_with_firmware`, before any vCPU runs. The resident's alias maps the image's 2 MiB blocks read-only
  at EL1 (`Span::read_only`): a store that missed its check halts the resident instead of changing the image (shown by
  a mutation: section 5). Shadows map it through its IPA as RAM is, read-only (`install_rom`).
- **Fetch:** `fetch_walk` asks `rom_page` (out of line) for a page that is not RAM; the interpreter's `fetch_page` and
  `fetch` read through `ram_and_rom()`. The pool and template lookups read RAM only, so a block from the image is always
  the host's translation.
- **Per-page tables: unchanged** (`cache::pages()` stays ram_end(R); accepted against the plan's "must span up to 4 GiB").
  The image is never written, so its pages need no code marks, chains or writable bits: below 3 GiB of RAM they lie past
  the tables, which the cache reads as "no code to invalidate, counts as marked" (`mark`, `marked`, `invalidate`,
  `file`'s chain); above 3 GiB they are inside the tables and tracked as any page. Both are cases (section 5).

## 2. Each writer's check

The image is read as memory everywhere it can be reached, and a store to it is dropped (x86's ROM):

- **The interpreter's memory layer:** a third class beside RAM and unassigned, ROM (bit 1 of `Mem::pages`' entries):
  read as RAM; a store dropped; LOCK's compare-exchange returns the bytes and writes nothing; no code-write protocol.
- **The resident's `copy`** (all of `linear`'s accesses: descriptor tables, SGDT and the like, a delivery's frame): its
  not-RAM arm calls `copy_rom`, which reads the image and drops the store (a device page still stops `unsupported_mmio`).
- **`atomic_byte`** (accessed and busy bits): `rom_byte` applies `update` to the byte read and writes nothing (an accessed
  bit in a ROM GDT stays clear, as on x86; the exit test presets them).
- **Translated code (the shadows):** `translate` is unchanged: a page of the image is not RAM, so it is never shadowed by
  the RAM arm (no writable bit, no code write) and the access takes the not-RAM path. A read reaches `mmio::ones`, which
  shadows the image's page read-only onto the image (no host request; not global) and retries it, so every read form then
  runs natively. A store reaches `mmio::device`, which completes a MOV store with nothing stored and RIP past it, no host
  exit. **Deviation** (accepted; recorded in [boot-handoff.md](boot-handoff.md), "Typed refusals"): the forms the device
  path refuses stay refused for the image, before any effect (`unsupported_mmio`): store pairs, pushes, a store crossing
  a page, read-modify-write forms without a completion record (`add [rom], eax`), LOCKed forms and XCHG. If a workload
  needs them: run the instruction alone with its page shadowed writable onto a scratch copy for that one instruction, as
  a UC page is shadowed for the step block.
- **The resident's other instructions with a memory operand** that do not go through `linear` (far pointers and frames,
  RCL/RCR and VCVTPS2PH memory forms, XSAVE's preflight, INS): their not-RAM arms are unchanged, so an operand in the
  image stops `unsupported_mmio` (status `system`): the same deviation, same record.
- **Page tables in the image:** not supported (a walk reads tables from RAM only: `unsupported_mode`, as for any table
  outside RAM). OVMF and the exit test build theirs in RAM.

## 3. The start states

- **vCPU 0:** `natemu_abi::power_on::reset()` (Table 12-1's power-on state over a zero `Cpu`: CS F000H base FFFF0000H
  limit FFFFH, EIP FFF0H, CR0 60000010H, EFLAGS 2, EDX the signature) with `cpu::power_on_msrs(true)`: IA32_MTRR_DEF_TYPE
  0, no valid variable range (not `MTRR_INITIAL`), IA32_APIC_BASE FEE00900H, through `execution::set_cpu_at_power_on`
  (`set_cpu` and its callers unchanged). The resident's `start()` enters the interpreter.
- **`flat32`** (OVMF's `Main32`, the start TDX gives): the same with CR0 60000011H, CS 08H base 0 limit FFFFFFFFH
  attributes C09BH, SS DS ES FS GS 10H base 0 limit FFFFFFFFH C093H, EIP FFFFFFF0H (`boot::firmware::bsp`).
- **vCPUs 1..N-1:** the power-on state with `power_on_msrs(false)` (APIC base FEE00800H), published waiting for a SIPI
  before any vCPU runs (`execution::wait_for_sipi`). No mailbox, no ACPI tables, no e820, nothing else in RAM.
- **The board:** COM1, the I/O APIC and 8259s, the PIT, the RTC, the PM block (PM1a_CNT at B004H: power-off), the
  debug-exit port if asked. No virtio device and no PCI bus (refused).

## 4. The options

- **Library:** `linux::Boot::firmware: Option<boot::firmware::Firmware { image, start }>` (`start`: `Start::Reset` or
  `Start::Flat32`), with `kernel`, `initrd` and `cmdline` empty; `prepare` then takes `prepare_firmware` (the kernel path
  is untouched). `Outcome::decodes`: each vCPU's interpreter decode requests (any boot).
- **natemu:** `--firmware FILE` (refused with `--kernel`, `--initrd`, `--cmdline` or a device); the file's
  `[boot.firmware]` `image` and `start = "reset" | "flat32"` (`--set boot.firmware.image=`, `boot.firmware.start=`);
  `[boot]` takes exactly one of `kernel` and `firmware` (`uefi` stays M3's). [docs/configuration.md](../configuration.md).
- **Test driver:** `natemu-host RUNTIME boot-linux FEXC IMAGE --firmware [--firmware-start flat32]` with boot-linux's
  report (stop, final state, per-vCPU stops, `waiting`, `decodes`, shootdowns, ports and unassigned pages), `--stop-at`,
  `--serial`, `--vcpus`, `--ram`.
- **Program driver:** the machine-description item `firmware HEX` (once, in the shared space): the image at 4 GiB - its
  size, the backing as the firmware boot mode's (the MSRs stay the driver's).

## 5. What runs (framework cases, M2 Max)

- **`natemu-machine`** (tests/interp, run 20261122-075644-27b6, 21 of 21 match), seven with an image: `r-rom-reset` (the
  power-on state: the reset vector's JMP fetched from the image, MOV and LOCK BTS stores to it dropped, read back, to a
  stop), `r-rom-typed-stop` (the same run on to an OUT: its typed stop with that state), `p32-rom` (flat P32 code in the
  image: MOV and LOCK XADD to it dropped, a RAM store), `l64-rom` (64-bit at CPL 0: a translated MOV store and SGDT to the
  image dropped; LGDT of a GDT in the image and a MOV DS through a descriptor there, its accessed bit left clear; code
  run from the image twice, storing to the image, to the RAM page holding the RAM code's translations and to a data
  window), `l64-rom-high` (the same with 4 GiB + 2 MiB of RAM: the image's pages inside the per-page
  tables), `l64-rom-lock` (LOCK ADD to the image from translated code: `unsupported_mmio`, the deviation). Shown to fail
  when they should (run 20261122-075839-4035, a temporary mutation): an interpreter store reaching the image halts the
  resident (`unknown_abort`: the read-only alias), an SGDT to it treated as a device's stops `unsupported_mmio`.
- **`exit-fw-natemu`** (tests/boot; `run_exit_fw.py --variant firmware --backend natemu`; run 20261122-092452-bce8) at 1, 2 and 4
  CPUs, the BSP from the reset vector: COM1 `EXITFW long mode` and `EXITFW cpus=N aps=N-1 ids=1,...,N-1 status=0`, the
  run ended by the PM1a power-off, `judge('firmware', N, answer, want_exit='poweroff')`; decode exits 19 on the BSP and 16
  on each AP, shootdowns at most 2 per vCPU, under the bounds of 32 and 8 per vCPU (plan section 8).
- **`ovmf-natemu`** (tests/boot; `run_ovmf.py --backend natemu`; same run): `--stop-at` SEC's entry FFFD5E24H, `judge`
  without its debug-port line (natemu stops before SEC writes it; RBP names the boot firmware volume) and the registers
  equal to the locked KVM answer's. `main32` passes (RIP = RSI = FFFD5E24H, RBP FFFCC000H, CS 38H, CR0 E0000011H, CR3
  800000H, CR4 20H, EFER 500H; no port or unassigned-page access before it). `reset` passes too: at FFFFFE94H, in
  32-bit protected mode with paging off at CPL 0, its `mov cr4, eax` with EAX 640H (MCE, OSFXSR, OSXMMEXCPT) raised
  #GP(0) while CR4.MCE was outside `cpu_profile::CR4_VALID` (natemu's CPU presents no MCE: CPUID.1:EDX.MCE = 0), the
  known bug `ovmf-reset-cr4-mce`. Decided (coordinator, November 22) and built: natemu accepts CR4.MCE writes without
  presenting MCE in CPUID, a recorded deviation (`tools/models/cpu_profile.json` `control.cr4.bits.MCE`, so
  `CR4_VALID` holds it: the interpreter's CR4 rule and the 64-bit MOV CR4 transaction share the mask); the known bug is
  gone.
- **Default boots:** the families `boot`, `programs`, `system`, `smp`, `interp`, `static` and `unit` (the final report
  names the run).

## 6. Effect on today's boots

Experiment (not a framework case; M2 Max): the release residents of `boot-interp` deea8131 and of this branch, disassembled
function by function (`llvm-objdump`, addresses, branch targets and literal offsets normalized): 452 of 486 functions are
identical, among them `translate` (with the shadow install inlined), `segments::pages`, `access_aborted`, `find_from`,
`mapped`, `fetch_page`, `page_record::translation`, `tables::install`, `system`, `commit`, `set_cpl`, `sys_fast`,
`miss_fast` and `split_lock`. On the hot paths: `fetch_walk`'s RAM path is identical, its not-RAM arm a call (`rom_page`),
only branch offsets differ; `segments::copy` differs by a register rename (two callee-saved registers swapped) and its
not-RAM arm; the page walker's four instances (`paging::walk_once`) inline `GuestTables::read` (the table load with its
observer), which deea8131 calls: an inlining decision of the code-generation units, not code of this change (paging.rs
and guest_memory.rs are untouched; it comes and goes with the size of `mmio::ones`' firmware block alone, and against
e21b0209 the walker was identical), one call fewer per table level; `maskmov::transaction` and `xsave::transaction`
differ in scheduling only. The rest are cold paths (`mmio::device`, `mmio::ones`, `atomic_byte`, `accessed`, the
interpreter) and literal-pool offsets (libm, x87 helpers). Without an image, the host's spans, backing size, `Control`
values, MSRs and the kernel path are as before; a benchmark round on the final commit decides the rest.
