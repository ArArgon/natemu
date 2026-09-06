# BP-01-03-start — address-space representation breadth pass

> Historical question-opening coverage. Findings and current work belong to
> [INV-01-03](../investigations/INV-01-03.md); this pass is not a live status
> record and new work need not update it.


## Metadata

- Breadth-pass ID: `BP-01-03-start`
- Question: `Q-01-03`
- Date: 2026-09-03
- Executor/session: Claude Code authoring subagent `t01_memory_author`

This pass organizes questions and establishes no evidence or conclusion. Every
mechanism named below is named as something to question against a primary
source or a controlled probe, never as a fact and never as a selected design.

## Coverage

### Product assumptions

- The accepted boundary (`INV-00-02`) makes 2 GiB **and 8 GiB** guest RAM
  mandatory acceptance sizes at 2, 4, and 8 vCPUs, on `P1` and `P2`. This
  question must therefore ask whether one host process can represent 8 GiB of
  guest-physical memory *simultaneously with* everything else a running
  instance needs, not whether 8 GiB can be reserved in isolation.
- Question the assumption that guest-physical memory must be one contiguous
  host reservation at all. Contiguity is a convenience for a direct-map
  addressing scheme, not a requirement stated anywhere in the envelope;
  families differ in whether they need it (`F1`/`F2` may indirect through a
  software MMU, `F3` needs whatever mapping contract Hypervisor.framework
  imposes, `F4` may need a stable address for statically translated code).
  Recording the reservation limit must not silently select the contiguous
  design.
- Separate representation from translation. This question owns guest-physical
  and guest-virtual *representation* and its limits; guest page-table walking,
  invalidation, and modification tracking are `Q-01-05`, and executable
  regions are `Q-01-04`. The virtual-address *budget* is shared, so the three
  must be questioned jointly or each will assume the others' space is free.

### Adjacent systems

- Existing full-system emulators and hypervisors solve this with softmmu
  indirection, host-address direct maps, or hardware second-stage translation.
  Their solutions may locate evidence; none may be cited. Anything used must be
  re-derived from Apple documentation, macOS manual pages, or the versioned SDK
  headers (`<sys/mman.h>`, `<mach/mach_vm.h>` — note `mach_vm_remap` is
  declared there, not in `<mach/vm_map.h>`), at a stated SDK revision.
- The unit tests of adjacency worth asking: does anything in the public macOS
  surface offer a *second* address space (a child task's map, a Mach memory
  entry, a Hypervisor.framework guest-physical map) that would let guest and
  host addresses be decoupled rather than shared?

### Alternatives

- Direct map: reserve guest-physical as one host range, guest address plus a
  base register. Cheapest access, largest reservation, largest blast radius.
- Softmmu indirection: a lookup per guest access; no large reservation, but
  the cost lands in `Q-04-01` and the fault-free path in `Q-01-05`.
- Aliasing views: `mach_vm_remap` (and `mach_vm_remap_new`) to give the same
  guest-physical page more than one host virtual address with different
  protections. Must be questioned, not assumed: whether alias views carry
  independent protections, whether writes through one view are visible through
  another without explicit action, and whether each alias multiplies VM map
  entries and therefore kernel cost.
- Sparse/lazy backing: reserve `PROT_NONE` and commit on demand; asks whether
  reservation is cheap and whether commit is per-16-KiB-page and observable.
- Reclaim paths (`madvise(MADV_FREE_REUSABLE)`, purgeable memory, `mincore`)
  as a way to hand guest RAM back to the host: does reclaim silently reset
  protections or residency state that another mechanism depends on?

### Negative and boundary cases

- Reservation failure: what `mmap`/`mach_vm_allocate` return and which `errno`
  or `kern_return_t` for 8 GiB, 64 GiB, and 1 TiB `PROT_NONE` requests. A
  reservation that succeeds proves nothing about backing; a commit-and-touch
  probe is a separate question with a separate failure mode.
- **Page-size mismatch.** The host page is 16 KiB and the accepted guest uses
  4 KiB (and 2 MiB) pages. One host page covers four guest pages. Ask what
  happens to a protection change requested on a 4 KiB-aligned but not
  16 KiB-aligned range: rejection (`EINVAL`) is safe and expensive; silent
  rounding is cheap and a **correctness hazard**, because it widens or narrows
  protection over three neighbouring guest pages that the guest expects to be
  independent. The rounding direction must be established, not assumed.
- Guest-physical holes: MMIO windows, the sub-1 MiB legacy region, and the
  PCI hole fall on 4 KiB boundaries the host cannot express. Ask whether a hole
  can be represented at all without either 16 KiB-quantizing the guest machine
  contract (`Q-01-13`) or routing those accesses through software checks.
- Aliasing the same guest-physical page at many guest-virtual addresses is
  normal guest behavior; a representation that assumes injectivity is wrong.
- VM map fragmentation: per-page protection changes may create one map entry
  per range. Ask whether there is a per-process entry ceiling and whether the
  cost of mapping operations grows with entry count — a scaling failure mode,
  not just a constant.
- Exhaustion: the only host available has 16 GiB of physical RAM. An 8 GiB
  guest plus code cache plus shadow structures will meet the macOS memory
  compressor and swap. Whether that cell is *measurable* here is a coverage
  question, not a feasibility answer.

### Host and guest matrix

- `P1` (M1 Pro / Sequoia 15) and `P2` (M4 / Tahoe 26) per `INV-00-02`; SDK
  26.4 on the available host. The 16 KiB host page must be **observed per host**
  (`vm_page_size`, `getpagesize`, `sysconf(_SC_PAGESIZE)`, `hw.pagesize`) and
  compared, because the three may disagree and a compile-time `PAGE_SIZE`
  constant may disagree with all of them.
- Guest dimension: 4-level paging, 4 KiB and 2 MiB pages; LA57 and 1 GiB pages
  excluded unless the boundary is reopened. Guest RAM 2 GiB and 8 GiB.
- Host physical RAM and swap/compressor configuration is a dimension of this
  question's *applicability*, not a constant: an 8 GiB result on a 16 GiB host
  does not transfer to a 64 GiB host, and vice versa.

### Multicore interactions

- The host address space is shared by every vCPU thread. There is no per-thread
  page table, so every mapping or protection change is process-wide and
  immediately visible to threads that did not request it. Ask whether that is
  stronger than the guest requires (harmless) or a source of spurious faults on
  other vCPU threads (not harmless).
- Mapping operations plausibly serialize on a process-wide VM lock. Any
  reservation, aliasing, or commit cost measured with one thread is a lower
  bound on the 2/4/8-vCPU cost and must be labelled as such.
- Per-vCPU shadow structures (TLB caches, dirty bitmaps, translation
  metadata) scale with vCPU count and consume the same virtual-address and
  residency budget as guest RAM. The budget must be stated per topology.

### Falsifiers and stop paths

- Primary kill assumption: **`KA-03`** — one host process can reserve and back
  guest-physical memory of at least 8 GiB plus translated-code and metadata
  regions within its virtual-address and resource limits. Falsified by a
  reproducible failure to obtain or back that combination on an accepted host.
- Contributing: **`KA-07`** (tracking granularity depends on the 16 KiB/4 KiB
  relationship established here) and **`KA-01`** (the code cache competes for
  the same virtual-address budget; see `BP-01-04-start`).
- If `KA-03` fails for every registered family, the route is `INV-00-06`
  project stop input 3 (a mandatory RAM cell is unreachable). If it fails only
  for families needing a contiguous direct map, it is a family kill under
  family-kill item 2, and one surviving family falsifies the project stop.
- A reservation that succeeds but cannot be backed, or backing that succeeds
  only by compressing/swapping the host into unusability, must be recorded as a
  *conditional* pass with its host-RAM applicability, never as a plain pass.

### Measurement failure modes

- First touch versus steady state: the cost of the first write to a 16 KiB page
  is a commit fault, not a memory-access cost. Conflating them overstates or
  understates by orders of magnitude, in either direction depending on which
  side is being argued.
- Residency accounting: resident size, `phys_footprint`, compressed pages, and
  purgeable pages are different numbers with different meanings. Which
  `task_info` counter is read must be declared before the run, and the
  instrument itself is owned by `Q-05-08`.
- Page cache and prior runs: a re-run touching the same file-backed region is
  not a cold run. Anonymous versus file-backed guest RAM is a dimension.
- The macOS memory compressor turns a memory-capacity question into a
  CPU-cost question invisibly; a timing sample taken while compression is
  active measures the compressor.
- Thermal state and E-core placement change every timing number on this
  hardware; placement is owned by `Q-05-06` and instruments by `Q-05-08`, and
  results here must record which cores ran.
- Sampling a 2 GiB subset and extrapolating to 8 GiB assumes linearity that
  the compressor and the VM map entry count both violate.

## Backlog updates

This pass surfaced no issue that an existing backlog row cannot own, and it
creates no new question identifier. Each surfaced issue is routed to the row
that already owns it; the routing is recorded here so it stays visible to the
question gate and to synthesis.

| Surfaced issue | Owning question |
| --- | --- |
| Cost and granularity of protection changes and modification tracking under the 16 KiB/4 KiB mismatch | `Q-01-05` |
| Virtual-address and residency competition between guest RAM, code cache, and shadow structures | `Q-01-04` (code cache), `Q-01-14` (per-instance budgets) |
| Whether 4 KiB-quantized guest-physical holes are expressible in the guest machine contract | `Q-01-13` |
| Whether a Hypervisor.framework guest-physical mapping contract exists and is admissible | `Q-01-01`, `Q-01-02` |
| Host physical RAM and swap configuration required for a valid mandatory 8 GiB cell | `Q-05-02` |
| Validity of residency, RSS, footprint, and timing instruments across the matrix | `Q-05-08` |
| P/E-core placement and thermal normalization for any timing sample here | `Q-05-06` |
| Reservation/commit cost as a scaling component of the performance model | `Q-04-01` |
