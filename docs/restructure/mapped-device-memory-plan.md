# Mapped device memory: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: revised November 23, 2026, after an independent review (Fable: accept with required changes; six blocking
findings on the classification point, RAM-only completion paths, the shootdown's holder scan, the remap trigger, the
`GuestRam` region limit and two unfalsifiable breaks, all folded in below with the seven non-blocking ones; a second
check confirmed them and found the widened shootdown could wait on ended vCPUs and one break could not fail: fixed by
keeping `split_begin`'s flag scan, with `holding()` before the table read as the whole install protocol). Owner
direction (same day): plan mapped device memory first among the display-related work; which display and audio devices
to build is not decided, so this plan builds the primitive and test-only consumers, no display device. It follows the
multi-BAR PCI plan (`docs/restructure/pci-multibar-plan.md`), whose `Function`/`Bar` interface it extends, and uses the
`tests/devices` family the platform-devices plan creates. Amended November 24, 2026: a region's backing is a POSIX
shared-memory object, so another process (a front end, through the control socket of
`docs/restructure/control-api-plan.md`) can map the same pages; an experiment the same day showed `hv_vm_map` accepts
such memory (below). An independent review of the amendment required that clients get only a read-only descriptor
(folded in).

**Goal:** a PCI memory BAR can be backed by host memory that the guest reads and writes natively (no exit per access)
and the host reads and writes directly, at whatever address the guest programs the BAR to; the primitive a Bochs-style
display, virtio-vga, a firmware framebuffer or virtio-gpu host-visible memory would stand on.

**Architecture:** each mapped region is its own host allocation, mapped into the VM once at VM creation at a fixed IPA
above the RAM backing, and into the resident's private tables at a fixed alias VA. The guest-physical base is only a
lookup key: a seqlocked table of regions in the shared global area (guest base, length, alias, enabled). The resident's
`translate` gains a branch for mapped pages beside its RAM path, so the guest's page-table permissions, A/D updates
and #PF apply as for RAM; every resident completion path that accepts only RAM today learns the mapped backing.
Moving or disabling the BAR rewrites the table; the vCPU whose access caused it sees the table's sequence number move
across its exit and makes every vCPU drop its shadows before it continues, through the shootdown the split lock
already uses. Nothing is unmapped or remapped in the Hypervisor framework when a BAR moves.

**Tech stack:** Rust: `crates/host`, `crates/runtime`, `crates/abi`, `crates/driver`. New dependencies are allowed when judicious (owner, November 24): name each, with its reason, in the commit that adds it; never a project's internal workspace crates.

**Spec:** the Design section below; PCI Local Bus 3.0 6.2.2 (Command register memory enable), 6.2.5 (BARs); Intel SDM
Vol. 3A chapter 12 (memory types: the stated deviation below).

## Global constraints

- Default launch unchanged: with no mapped region, no extra VM mapping, no extra resident alias, an empty region table,
  and every existing test byte-for-byte as at the base this branch is rebased onto.
- Contract 21 holds: no guest-physical address resolves to private backing; a region's alias maps only its own
  allocation (EL1-only alias; `Space::install` keeps refusing private-table targets).
- **Allowed in mapped memory:** data loads and stores of every width and form translated code and the resident
  complete for RAM, including page-crossing stores (within one region, or RAM next to a region), string forms, vector
  forms and aligned atomics: every access translated code makes natively once the page is shadowed, and the
  resident's completions of translated code's accesses (`access_aborted`, `split`, `vector_aligned`, `wrapped_load`),
  and the boot-time interpreter's data accesses. **Refused** (each a typed stop, a stated deviation; the list is Task
  2's audit; the `mapped-refusals` or `mapped-interpreter` sub-case covering each is named): instruction fetch (`no_code
  fetch_not_ram`; `fetch`); page tables (`unsupported_mode`; `page-table`); split locks (`unsupported_mmio` `atomic`;
  `split-lock`, and the interpreter's `split-lock`); a store crossing from a region onto a device or unassigned page,
  or across two regions (`crossing`; `crossing-device`, `crossing-regions`); every access the resident makes through
  its own walk (`segments::pages`), each `unsupported_mmio` `system`: through `segments::copy`/`linear`, one shared
  check (`descriptor-table`, `idt`, `popf-stack`, `iret-frame`, `fxsave-image`, `xsave-image`, `x87-m80`,
  `ldmxcsr-operand`, `gather-element`; the TSS, far transfers' and event delivery's frames and FSTP m80 go through the
  same check with no case of their own), and the scalar completions with checks of their own (`rcl-operand`,
  `enter-frame`, `ins-destination`, `vcvtps2ph-destination`, and `Span`'s, `maskmov-destination`); the interpreter's
  resident operations (`interp_memory`; the interpreter's `descriptor-table`, whose check its far transfers' and
  deliveries' frames share). Without a case: a compatibility-mode FS/GS string element (`form`), a compatibility-mode
  load wrapping at 4 GiB beside a region (`crossing`), and the ACPI wakeup mailbox (RAM only, `unsupported_mode`, as
  today). Virtio buffers or rings
  there are rejected by `GuestRam::range` validation, as today (no device-to-device DMA).
- **Memory type (stated deviation):** a mapped page runs natively whatever its PAT/MTRR type. For RAM, `translate`
  honours UC/WT/WP by stepping (`execution.rs` near 3771); a framebuffer mapped UC- by `ioremap` or `/dev/mem` would then
  step every access. Device memory has no side effects, so native access gives the same results.
- A mapped page is never code-marked or table-marked (no fetch or walk can use it), so host writes to it need no
  invalidation and no window.
- The region set is fixed at launch (which functions have mapped BARs and their sizes); only bases and enables change
  at run time; at most 8 regions, total at most the PCI window's 256 MiB.
- **No cost on RAM paths:** every audited site tests RAM first and consults the region table only for a page that is
  not RAM; RAM installs do not read the table; the remap trigger is two loads of `seq` per port or device exit (before the request and once it is answered). Tasks 2
  and 3 each end with an A/B (below) that shows no benchmark moves beyond its noise.
- The host never waits for a vCPU inside a device handler (`devices.rs` contract): the remap wait runs in the
  resident, as the split-lock owner's does.
- Rust unit tests run in the framework as one unit (the `rust/all` item of `tests/unit/family.toml`; the implementer
  confirms the node id with `--collect-only`; `-k rust` also selects the qcow, FP and FS sweeps); named tests are read
  from that run's `.natemu/runs/<id>/junit.xml`. Resident behaviour needs EL1, so it is tested by program cases, not
  Rust unit tests. Each new check is shown to fail against a deliberate break named in its step.
- Tests only through `tools/natemu-test`; the gate passes before merge (this changes the runtime).
- Branch `mapped-memory` off `m2-peripherals` (the integration branch), after multi-BAR PCI and the platform-devices track land. Commit with pathspecs.

## Review Focus

- **Remap visibility across vCPUs:** vCPU A moves the region and then sets a flag in RAM; vCPU B, spinning on the flag
  in translated code without exiting, then reads the old and the new address: old reads all-ones, new reads the region
  (Task 3 `remap_is_visible_after_flag`; break: drop only the moving vCPU's shadows, as `lapic::set_base` does).
- **A vCPU installing a shadow while the table changes:** B is held between its table read and its install while A
  moves the region; B must not keep a shadow of the old base (Task 3 `install_races_publish`, driven by an
  `io-test-hooks` point between the read and the install, as `BEFORE_SPLIT` and `AFTER_ACK` drive their races; break:
  call `holding()` after the table read and the hold point instead of before).
- **A halted peer holding shadows of the old base** drops them before it next runs guest code (Task 3
  `halted_peer_drops_on_wake`; break: skip peers whose `IoWindow.quiescent` is 1 in the shootdown).
- **Page-crossing and string stores into a region:** an unaligned 16-byte store across two pages of one region and an
  unaligned `rep movsb` complete as for RAM (Task 2 `mapped-load-store`; break: keep `split`'s non-RAM `CROSSING`
  refusal).
- **Guest permissions:** a store through a read-only or D=0 mapping of a region gives #PF or sets D exactly as for RAM
  (Task 2 `mapped-permissions`; break: install mapped shadows writable regardless of the walk).

## Design

### Host side

- `crate::pci::DeviceMemory`: one POSIX shared-memory object (`shm_open(O_RDWR | O_CREAT | O_EXCL)` with a short unique
  name, `/natemu.<pid>.d<n>` (the fexc channels take `/natemu.<pid>.<n>` with a counter of their own), within macOS's
  31-byte limit; `ftruncate` to the length once, which fixes it: macOS allows only that one size change; a second `shm_open(O_RDONLY)` of the name for the descriptor handed out; then
  `shm_unlink`, so only the descriptors name it and nothing outlives the process), mapped `MAP_SHARED` read-
  write at a 2 MiB-aligned address (reserve twice the length, `MAP_FIXED` into the aligned part, unmap the rest);
  length a power of two from 2 MiB to 256 MiB (2 MiB: the resident alias's block size). Volatile 8-byte-wide accessors
  (`read_words`, `write_words`) for host copies, so host stores are single-copy atomic per word; `as_ptr()` for a later
  scanout; `fd()` the read-only descriptor, the only one the control socket passes to a client (`SCM_RIGHTS`): on it
  `mmap(PROT_WRITE, MAP_SHARED)` fails (with EPERM on macOS, not POSIX's EACCES: Task 1's `fd_is_read_only`), so a
  client cannot write device memory; and `view()` a second read-only mapping of the same object in this process.
- **Why shared memory, and the evidence.** A front end in another process must see the guest's frames without a copy
  per frame. Experiment (a scratch program, not a test case; November 24, M3 on macOS 15.7.3 and M2 Max on macOS
  26.6.2): `hv_vm_map` of a `shm_open` + `mmap(MAP_SHARED)` region and of an IOSurface's base address both succeed; a
  vCPU's store into each is read by a second process that maps the object (the shared-memory object by name, the
  IOSurface by global ID). Task 1's `view()` test and Task 2's `SUM` (computed through `view()`) make the shared-memory
  half a framework case. An IOSurface (for a Metal front end) is the alternative; it cannot travel over a Unix socket
  (it needs a Mach port), so it waits for the native front end.
- A multi-BAR function names a mapped BAR with `Function::mapped(bar) -> Option<Arc<DeviceMemory>>` (as built: a
  trait method defaulting to `None`, so no other function changes; the request tuple is unchanged). `Bus::publish_to`
  gives the bus the table (`Backing::mapped_table`; the backing exists in `prepare` before the bus); `Bus::install`
  (in `prepare`) returns `pci::Mapped` (the regions in (slot, function, BAR) order, the table's entry order, and a
  publish counter), which the launch puts into `Prepared` (`Prepared::mapped`; Task 2's driver test device passes its own list to
  `Backing::machine` the same way); the VM does not exist yet. `Backing::machine(mapped)`, the only way to create a
  machine, takes them: `Machine::create(memory, mapped)` (crate-private) has a pure `vm::mapping_plan` count them in
  the IPA size computation (`vm.rs`, from the backing size today) and place each at `MAPPED_IPA + offset` (offsets the
  sum of the lengths before, so 2 MiB-aligned, in BAR order; `MAPPED_IPA` the first 1 GiB-aligned IPA at or above the
  backing's end), and `hv_vm_map`s each read/write and never executable; then, since the machine has no windows,
  `Backing::machine` itself `map_private`s `MAPPED_VA + offset` EL1 read/write in every window, before any vCPU
  (`memory.rs` `mapped_spans`, a `Span` whose target is backing-relative and 2 MiB-aligned: a target past the backing
  works arithmetically).
- `MAPPED_VA` = `GUEST_RAM_VA + 3 GiB`: the alias slots of the 3-4 GiB hole, already present in the resident's tables
  and never used (the hole is not RAM), below the firmware image's top 16 MiB; a const assert keeps 256 MiB of
  regions clear of the firmware alias. With RAM above 3 GiB those slots are still unused (high RAM aliases from 4 GiB).
  The region alias is written after the RAM and firmware aliases (`map_private` replaces entries), and
  `alias_fits_the_hole_slots` asserts that no RAM or firmware alias entry falls in the region range.
- The bus publishes a mapped BAR into the table only on an effective decode change: the function's memory enable or
  the BAR's decoded base changed, with multi-BAR's rule that a BAR decodes only wholly inside the window (a size probe
  of all-ones decodes nothing and so disables the region until the BAR is written back). Accesses that still reach the
  bus's `Window` for a mapped BAR's range read all-ones and drop writes.
- Report counters: `remaps` (shootdowns run) and `mapped_installs` (shadows installed onto regions; Task 2 counts it in
  `cache::Control` and the driver reports it), beside the access
  counts in the run report. As built for Linux (Task 4): `Prepared::mapped_report` (the table at the end, the bus's
  publishes, per vCPU `mapped_installs` and `remaps`), which the `natemu` CLI prints as one `natemu: mapped {...}` line
  on standard error before the stop line, only with regions (the default launch prints nothing new); `k1_stats` gains
  `mapped` (`installs`, `remaps`), absent while both are zero.

### Shared state (`crates/abi`)

- `MappedRegions` in the global area (`cache::MAPPED`, after the scheduling-test metadata, below `CODE_PAGES`; the host
  reaches it as `Backing::mapped_regions`): `seq: AtomicU64` (even = stable, odd = being written: a seqlock) and up to 8
  entries `{ base: u64, len: u64, alias: u64, enabled: u32 }` (`MappedRegion`; `publish(i, region)` replaces one,
  `read()` gives the even `seq` and a consistent copy of all 8). Written only by the host, under the bus's lock; read by
  residents with the seqlock protocol. No field in `Control` (resident-owned after prepare).

### Resident side

- **Classification in `translate`** (`execution.rs` near 3761, where a non-RAM page returned early as a device page): a
  page that is not RAM, not this vCPU's APIC page (the APIC decodes first, `lapic_beats_mapped_region`) and not ROM, but
  lies in an enabled region, takes a mapped branch (`install_mapped`; `translate` now says which class it found:
  `Class::Ram(uncached)`, `Mapped(region)` or `Other`, the device path); overlapping enabled regions: the
  lowest-numbered table entry (BAR order) holds the shared range (`overlapping_regions_pick_lowest`): writable as for RAM (`Write && W && D`, the
  D=1 read upgrade, the walk's A/D updates), global as the walk's G bit says (a full shadow reset drops global
  shadows too, so the shootdown still covers them; Linux's `ioremap` mappings are global), no memory-type stepping, no
  code mark, no WRITABLE_PAGES bit (those index RAM pages). The shadow is installed with a one-region `GuestRam` for
  the hit (base → alias), never a table of all regions (`GuestRam` holds four regions and asserts beyond them).
- **Install protocol** (the holder race): before reading the table, `cache::holding()` (SeqCst, as `split_wait` does),
  then read the table (seqlock, retry while odd or moved), look up, install. That order is the whole protocol: either
  the mover's holder scan sees this vCPU's flag (and resets it), or this vCPU's table read comes after the publish.
  The same applies to an all-ones shadow installed over a page that a region may later cover (`install_ones`), so the
  holding flags are complete for every shadow a remap can stale. One place (`execution.rs` `install_mapped`: the read,
  then Task 3's `MAPPED_BEFORE_INSTALL` point, then the install); a capacity eviction between them clears the flag, so
  the table is read again under a new one. `install_ones` reads the table too (no all-ones shadow over an enabled
  region's page). A table never published (`seq` 0) has no region and never will: `Backing::machine` publishes every
  region's entry, disabled, before any vCPU exists (for every region it maps, so no publisher can forget; unit test
  `regions_are_published_before_any_vcpu`), so `seq` is nonzero exactly when the VM has regions; a VM without regions
  pays one load of `seq` per non-RAM page and sets no flag. The flag's store is followed by a SeqCst fence before the
  table's loads.
- **Completion paths:** every site that accepts only RAM today (`mmio::ram(` and `guest_ram()` users: 55 in
  `crates/runtime/src`, 57 matching lines less the two definitions, among them `access_aborted`'s device test near
  4214, `wrapped_load` 4315, `split` 4569, `split_lock` 4680, `mmio::string_operand`, `segments/span.rs` `Backing`,
  `half.rs`, `rotate.rs`, `stack.rs`, `segments.rs` copies) is audited in Task 2 (the list with each decision is in its
  commit message): each either learns a `Mapped` backing (mixed RAM and one region included: `Class::memory`, guest
  RAM plus the region, since `GuestRam` holds four regions and RAM takes three), or keeps refusing with a typed stop
  that the refusal list names. What learns it: translated code's completions (above); the resident's own walk
  (`segments::pages`) keeps RAM-only users, each refusing a mapped page with the `system` stop. The interpreter's
  `Mem::check` gains the class (`MAPPED`) with plain loads and stores for the core's data accesses; its resident
  operations (`transitions.rs`) stop at a mapped page as at a device's, and a LOCK crossing 16 bytes there is refused.
- **Remap trigger:** after any exit whose device access may have changed the table, `REQUEST_PORT` or
  `REQUEST_DEVICE` answered `DONE` (also after `PENDING` polls), the resident compares `MappedRegions.seq` with its value
  before the request; if it moved, it runs `cache::remap_shootdown()` and resets its own shadows before resuming the
  guest. No completion flag is needed, so a configuration write (ports) and a test device's MMIO `MOVE` register are
  covered alike. Only a publish made during some vCPU's port or device exit triggers it: the publisher must be a device
  handler running on that exit (the bus's configuration writes are); a publish from any other host thread is never seen.
- **Ordering:** a Dekker pairing. The installer stores its holding flag, `fence(SeqCst)`, reads the table; the mover
  (after the publish, which its own exit made) runs `fence(SeqCst)`, then per peer loads the reset counter and then the
  flag (SeqCst). Either the mover sees the flag (the peer is asked), or the peer's table read follows the publish. The
  counter is read before the flag: a peer found with a clear flag between a reset and its next `holding()` reads the
  table afterwards, and a counter that moves after the mover's load moved after its fence, so every later table read of
  that peer sees the publish.
- **Completions that read the table and then wait** (`split`, `vector_aligned`, `wrapped_load`, the interpreter's
  `Mem::check`): `split_wait` may serve a peer's shadow drop, a remap's shootdown among them, so a class read before it
  can name a table older than a completed remap. With a mapped page among its classes and the shadow generation moved
  since its walks, the completion retries the instruction; RAM-only completions never retry. A page that the walk found in no region but that is in one at the all-ones step (`mmio::ones`,
  `install_ones`) was published meanwhile: every shadow goes and the access retries (the reset moves the generation, so
  the no-progress test does not stop it); `wrapped_load`'s mixed path instead reads it as all-ones, a read linearized
  before the publish. The interpreter makes all of an instruction's checks again when a reset since its first check
  could have staled a region any of them found.
- **`remap_shootdown`:** `split_begin`'s scan without taking the split lock: post `SHADOWS` to every peer whose
  `SPLIT[3+p]` holding flag is set (now set by mapped and all-ones installs too), kick them, wait for each one's reset
  counter to move while serving its own peer requests, then reset its own shadows. Termination is `split_begin`'s: a
  vCPU that has stopped for good reset first and cleared its flag, so it is never waited for. Halted peers are woken by
  the kick and counted by counter, as for split locks. After it returns no vCPU holds a shadow built from the old
  table.
- **Unchanged:** the claim cache (the PCI window stays one claimed range; `translate` sees mapped pages first), the
  frozen registry, page tables, code marks and split locks.

### Test consumers

- **Driver test device** (`crates/driver/src/drivers/test_devices.rs`, for bare programs under `natemu-host
  run-program`, as the `--device-at` cases in `tests/system/run_l3.py`): `--mapped-at GPA,LEN` (repeatable, in table
  order; regions may overlap each other and the xAPIC page) creates a region enabled at a base, plus
  `--mapped-control-at GPA` region 0's control page with 8-byte registers `MOVE` (+0, write a new base, published into
  the table from the handler (`Backing::mapped_table`, an owned handle); 0 disables; a base is allowed when page-aligned,
  below FEC00000H and the firmware image, every page outside RAM, the legacy ROM range, the test devices' pages and the
  control page, else refused), `SUM` (+8, read: the host's 64-bit sum of the region's words) and `FILL` (+16, write:
  the host fills the region, word k = value XOR k * 9E3779B97F4A7C15H). The output gains `mapped` (per region: the
  table's final `base` and `enabled`, the accesses to its initial range that reached the host, its sum through
  `view()`, its nonzero words, `publishes`) and per vCPU `mapped_installs` (`cache::Control::mapped_installs`) and
  `remaps` (`cache::Control::remaps`). `--mapped-hold SIG,ACK,GO,DONE` (io-test-hooks build) drives
  `install_races_publish`: vCPU 1 held at `MAPPED_BEFORE_INSTALL` until the move's publish and then until DONE or 300 ms. This exercises the resident without PCI; the cases are
  `tests/system/run_mapped.py`'s, one framework case each.
- **`SUM` reads through `view()`**, the second mapping, never the main one: each case that checks `SUM` also checks
  that guest stores reach another mapping of the object.
- **PCI test function** (`debug.mapped_testdev`, multi-BAR's test-option style): BAR0 a 2 MiB mapped region, BAR2 a
  4 KiB MMIO page with the same `SUM` and `FILL` registers, for Linux guest cases. Identity (decided November 23; the
  owner had no preference): vendor 1B36H, device 0005H as multi-BAR's test function, with its own subsystem ID and
  revision, documented as natemu's test function (no driver binds either; it is never on by default). As built:
  subsystem 1B36H:0100H, revision 01H, class 00FFH; BAR0-1 64-bit prefetchable memory (so the control page is BAR2),
  BAR2 32-bit memory; function 0 of the lowest PCI slot still free (after `debug.pci_testdev`'s), no INTx line.
- **Linux guest tool** (Task 4, as built): `read()` and `write()` of `/dev/mem` stop at the end of RAM on x86
  (`valid_phys_addr_range` against `high_memory`), so `dd` cannot reach a BAR above it; and BusyBox's `devmem` maps
  afresh each time, so it cannot hold a shadow across a move. `mapped-bar` therefore uses a small static program
  (`bin/mapped-tool`, nasm source in `tests/devices/run_platform.py`): `i`/`o` copy standard input into and the region
  out to standard output through an mmap of `/dev/mem`; `w` (pinned to the last CPU) maps the old base once and spins
  on it in plain loads, and must read all-ones after the move (a stale shadow keeps the old value). The old base is
  read back with `devmem`'s 8-byte loads at four offsets, not copied: there it is a device page, where natemu refuses
  the kernel's `rep movsq` (`unsupported_mmio`). The named break shows at 2 vCPUs (the probe stays stale); at 1 vCPU
  only `remaps` catches it (the probe's context switch drops its shadows).

## File structure

| File | Responsibility |
| --- | --- |
| `crates/abi/src/execution.rs`, `crates/abi/src/lib.rs` | `MappedRegions` (global area), `MAPPED_VA` and its assert |
| `crates/host/src/pci/memory.rs` (new) | `DeviceMemory` |
| `crates/host/src/pci/mod.rs` | mapped BAR requests, publishing on effective decode changes |
| `crates/host/src/linux.rs`, `crates/host/src/vm.rs`, `crates/host/src/memory.rs`, `crates/host/src/execution.rs` | `Prepared` carries regions; `Machine::create` maps them and sizes the IPA; the alias span; report counters |
| `crates/runtime/src/execution.rs`, `crates/runtime/src/execution/mmio.rs`, `crates/runtime/src/cache.rs`, `crates/runtime/src/execution/{ports.rs,segments.rs,segments/span.rs,half.rs,rotate.rs,stack.rs,interpret.rs}` | `translate`'s branch, install protocol, completion paths, remap trigger, `remap_shootdown` |
| `crates/driver/src/drivers/{test_devices.rs,program.rs}` | `--mapped-at`, `--mapped-control-at` |
| `tests/system/family.toml`, `tests/system/run_l3.py` (or a sibling runner in `tests/system`) | bare-program cases |
| `crates/host/src/pci/mapped_testdev.rs` (new), `crates/cli/src/config.rs`, `crates/cli/src/config/tests.rs`, `docs/configuration.md` | the PCI test function and its key |
| `tests/devices/run_platform.py` (the multi-BAR case's runner; no `run_pci.py` exists), `tests/devices/family.toml` | the Linux guest case `mapped-bar` |
| `crates/host/src/linux.rs`, `crates/cli/src/main.rs` | the test function's placement and `Prepared::mapped_report`; the CLI's `natemu: mapped` line |
| `docs/restructure/contracts.md` | the new memory class, its refusals, the memory-type deviation and the remap rule |

## Tasks

### Task 1: host allocation, VM mapping and the region table

**Files:** create `crates/host/src/pci/memory.rs`; modify `crates/abi/src/execution.rs`, `crates/abi/src/lib.rs`,
`crates/host/src/vm.rs`, `crates/host/src/memory.rs`, `crates/host/src/execution.rs`, `crates/host/src/linux.rs`.

**Interfaces:** `DeviceMemory::new(len: u64) -> Result<Arc<DeviceMemory>>`, `read_words`, `write_words`,
`fd(&self) -> BorrowedFd<'_>`, `view(&self) -> Result<View>` (a read-only `MAP_SHARED` mapping, unmapped on drop); a pure
`mapping_plan(backing_end: u64, regions: &[u64]) -> MappingPlan { ipa_bits: u32, maps: Vec<(u64 /*ipa*/, u64 /*len*/)>,
aliases: Vec<(u64 /*va*/, u64 /*len*/)> }` used by `Machine::create`; `MappedRegions::{publish, read}` (seqlock).

- [ ] **Step 1: unit tests**: `device_memory_is_2mib_aligned_power_of_two`, `view_sees_writes` (a word written
  through the main mapping reads back through `view()`, and through a `PROT_READ` `mmap` of `fd()` in a child: the test
  binary re-executed by `posix_spawn` with `--exact <child test> --ignored` and an environment flag, the descriptor
  passed with `posix_spawn_file_actions_adddup2` since `shm_open` sets FD_CLOEXEC; no deliberate crash, which would
  leave a crash report outside the cache), `fd_is_read_only` (`mmap(PROT_WRITE, MAP_SHARED)` of `fd()` fails with
  EPERM, macOS's errno for it), `object_has_no_name` (a `shm_open` of the generated name after `new` fails with ENOENT), `host_writes_are_word_atomic` (a reader
  thread never sees a torn word while a writer alternates two patterns), `plan_places_regions_above_backing` and
  `plan_grows_ipa_bits`, `empty_plan_matches_today` (no maps, today's IPA bits; no VM is created in unit tests),
  `seqlock_reader_never_sees_a_half_entry`, `alias_fits_the_hole_slots` (the const assert's bound).
- [ ] **Step 2: run** the Rust unit tests; FAIL. **Step 3: implement.** **Step 4: run**; PASS.
- [ ] **Step 5: commit** `memory: host-backed device memory mapped above the RAM backing`.

### Task 2: resident classification, shadows and completion paths

**Files:** the runtime files of the File structure table; `crates/driver/src/drivers/{test_devices.rs,program.rs}`;
`tests/system/family.toml` and its runner.

**Interfaces:** `natemu-host run-program --mapped-at GPA,LEN --mapped-control-at GPA`.

- [ ] **Step 1: the audit**: list every `mmio::ram(`/`guest_ram()` site in `crates/runtime/src` in the commit message
  with its decision (learns `Mapped`, or refused with which typed stop); the refusal list in Global constraints and
  `contracts.md` is updated from it.
- [ ] **Step 2: bare-program cases**: `mapped-load-store` (every load and store width, an unaligned 16-byte store
  across two pages of the region, a store crossing from RAM into the region, `rep movsb` and `rep stosq` unaligned, a
  vector store, an aligned LOCK ADD: values read back, the host's `SUM` matches, and the run's device access count for
  the region is zero); `mapped-permissions` (#PF error and CR2 for a store through a read-only mapping, D set by a store
  through a D=0 mapping, as the same program gives on RAM); `mapped-host-fill` (the guest reads the host's `FILL`
  pattern); `mapped-uc` (a UC mapping of the region runs natively: device access count zero, `mapped_installs` > 0);
  `mapped-refusals` (a fetch, a page table, a split lock, and each refusal from the audit give their typed stop);
  `lapic_beats_mapped_region`; `overlapping_regions_pick_lowest`.
- [ ] **Step 2b: run** them; FAIL (option unknown, then wrong results).
- [ ] **Step 3: implement.**
- [ ] **Step 4: run**; PASS. Show `mapped-load-store` fails with `translate`'s non-RAM early return left in place for
  mapped pages (stores reach the device path: access count nonzero) and with `split`'s `CROSSING` refusal kept;
  `mapped-permissions` fails with mapped shadows installed writable regardless of the walk; `mapped-refusals` fails
  with fetch allowed.
- [ ] **Step 4b: A/B**, on one machine, alternating three rounds: `TESTKIT_BENCH_AT=<parent> tools/natemu-test run
  --tier bench -k <cases>` then the checkout, for the 64-bit guest benchmarks and one I/O workload (`io5-solo`), with
  no mapped region; record rounds and spread in the commit message; any difference beyond the spread is a defect to fix
  before the commit, not a page update.
- [ ] **Step 5: commit** `runtime: mapped device memory as a page class, read and written natively`.

### Task 3: moving and disabling a region

**Files:** `crates/runtime/src/cache.rs`, `crates/runtime/src/execution.rs`, `crates/runtime/src/execution/{mmio.rs,ports.rs}`,
`crates/abi/src/execution.rs`, `crates/driver/src/drivers/test_devices.rs`, the bare-program family.

**Interfaces:** `cache::remap_shootdown()`; the `seq` comparison after `REQUEST_PORT`/`REQUEST_DEVICE`; the test
device's `MOVE` register.

- [ ] **Step 1: bare-program SMP cases** at 2 and 4 vCPUs: `remap_is_visible_after_flag` and `install_races_publish`
  (Review Focus); `halted_peer_drops_on_wake` (B halts holding a shadow of the region, A moves it and sends B an IPI; B
  reads the old address: all-ones); `disable_then_enable` (`MOVE 0` makes the range read all-ones and drop stores;
  moving back restores the same bytes); `remap_under_load` (four vCPUs storing to the region in a loop, at each of its
  three bases, while one of them, vCPU 0, moves it among them 1,000 times; after the last move and a rendezvous, the old bases read all-ones from every vCPU, each word holds
  its writer's last store (made after the last move), the host's `SUM` equals the sum the guest computes, the host's publish count is
  1,000 and `remaps` is at least 1,000: any vCPU whose exit spans a publish also shoots down). A store racing a move may
  land at either base or be dropped by the window: all legal. (As built: the 2- and 4-vCPU variants are sub-cases of
  one framework case per name, each at block sizes default and 1; `remap_under_load` runs at 4.)
- [ ] **Step 2: run**; FAIL. **Step 3: implement** (including the `io-test-hooks` point `MAPPED_BEFORE_INSTALL`).
  **Step 4: run**; PASS. Show `remap_is_visible_after_flag` fails when only the moving vCPU drops its shadows,
  `install_races_publish` fails with `holding()` moved after the table read and the hold point (at the read itself the flag is set before the mover scans, so that placement cannot fail; the held vCPU first resets its shadows by toggling CR4.PGE, since any RAM install sets the flag), and `halted_peer_drops_on_wake` fails when
  the shootdown skips quiescent peers.
- [ ] **Step 4b: A/B** as in Task 2 (the remap trigger adds a load to every port and device exit).
- [ ] **Step 5: commit** `runtime: a mapped region moves with a VM-wide shadow shootdown`.

### Task 4: PCI mapped BARs and the Linux guest case

**Files:** create `crates/host/src/pci/mapped_testdev.rs`; modify `crates/host/src/pci/mod.rs`, `crates/cli/src/config.rs`,
`crates/cli/src/config/tests.rs`, `docs/configuration.md`, `tests/devices/run_platform.py`, `tests/devices/family.toml`,
`docs/restructure/contracts.md` (as built also `crates/host/src/linux.rs`, `crates/host/src/execution.rs`,
`crates/cli/src/main.rs`).

- [ ] **Step 1: unit tests** in `pci/mod.rs`: `mapped_bar_publishes_on_base_write`, `memory_enable_off_disables_region`,
  `size_probe_disables_then_restore_enables`, `no_publish_without_decode_change`.
- [ ] **Step 2: the guest case `mapped-bar`** (`--set debug.mapped_testdev=true`, 1 and 2 vCPUs): the init enables the
  function (`echo 1 > enable`), writes a 2 MiB pattern into BAR0 through `/dev/mem` (as built through an mmap of it,
  `mapped-tool i`: `dd`'s `write()` stops at the end of RAM), reads
  BAR2's `SUM` with `devmem` and compares with the pattern's sum; writes `FILL` and reads the pattern back; then
  disables memory decoding through the sysfs `config` file, writes a new BAR0 base, re-enables, and checks the data at
  the new address and all-ones at the old one. S5 ends the run.
- [ ] **Step 3: run** the unit tests and the case; FAIL, then implement, then PASS. Show the case fails with the
  `seq` comparison removed after port exits (the old address still serves data on another vCPU in the 2-vCPU run, or
  the new one does not on the writer).
- [ ] **Step 4: contracts**: `docs/restructure/contracts.md` gains the mapped-memory class, its refusals, the memory-type
  deviation and the remap rule, with the cases that enforce each.
- [ ] **Step 5: run** `tools/natemu-test run` then `--gate`; PASS. **Commit** `pci: mapped BARs and their test function`.
  (As built: the gate runs once at the branch's final tip, not per task.)

## Cost

Each effective BAR change costs one full shadow reset on every vCPU and their refaults (counted by `remaps`); a guest
moves a BAR a handful of times at boot. A mapped page's first touch costs one walk and install, as a RAM page's.
Shadows are 4 KiB leaves, so a 16 MiB region uses up to 4,096 shadow entries and may evict RAM shadows under
pressure (not checked against the shadow capacity). Nothing here is measured yet; a display plan measures scanout,
first-touch and eviction cost with a bench case.

## Out of scope

A display device, scanout and dirty tracking (a later display plan builds on `DeviceMemory::as_ptr`); the legacy VGA
window at A0000H (a second, fixed region in the legacy hole, with a VGA device if one is chosen); execute-in-place from
device memory; device-to-device DMA; mapped regions in the firmware boot path (it has no PCI bus yet; M3); save and
restore of device memory; passing the descriptor to clients (the control API's `framebuffer` method, added with a
display); an IOSurface backing.
