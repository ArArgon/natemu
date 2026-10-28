# Slice 3 contract: up to four vCPUs on the general path, and retiring the specialized paths

Status: proposal (not implemented), October 27, 2026, revised after review; §5 recorded.
[Restructure](../vm-restructure.md) step 3 and the phase boundary; builds on the [slice-1](slice1-contract.md) and
[slice-2](slice2-contract.md) contracts. Old behaviour to cover: [contract inventory](contracts.md) (its numbers below).
No performance tuning.

## 1. N vCPUs (1–4) on the general execution path
**Per vCPU / shared.** Each vCPU keeps all slice 2 built for one (window: shared record, `State`, FEX frame, slot,
staging, stacks; shadow tables and pool; lookup area; metadata arena; code area) at the same private VAs, backed by its
own backing block (fixed stride); host side: thread, `Vcpu`, watchdog, `Compiler` (own `fexc --serve` and object cache).
Shared: guest RAM (one alias, same VA everywhere) and the global area; all guest-RAM mappings are Normal WB Inner
Shareable (pinned). Each vCPU's tables carry its own ASID, so its shadow entries could not hit for another vCPU even in
a shared TLB. That is a precaution, not shown necessary: an S3a control with every vCPU on ASID 0 and global shadows
also passed every S3a check, because HVF keeps vCPUs' TLBs apart (across host-thread migration too). The ASIDs are
harmless, but the migration stress cannot detect a regression in them. (History: that ASID-0 untagged mode was removed
at S3i2; every vCPU's shadows are tagged with its own ASID.) `State` gains the vCPU id (replacing cache.rs's `VCPU = 0`); each vCPU reads back `ACTLR_EL1.EnTSO` at start.
`owners.rs` (barrier, cancellation, join, vCPU-before-VM teardown) runs the execution service per thread; the `hvc #0x4a`
owner service stays until the family drivers retire (S3i). Any resident halt or host error cancels all vCPUs.

**Global area.** (a) Code-page mask: a byte per guest-physical page, bit v = vCPU v holds blocks from the page (exists).
(b) Writable mask: a byte per page, bit v = vCPU v may hold a writable shadow of it (moved from the per-vCPU lookup
bitmap: peers read it). (c) Per vCPU an invalidation record `{pending, stale[GUEST_PAGES/64]}` replacing the 30-entry
`Queue`: `pending` bits PAGES, FLUSH, SHADOWS; `stale` is a page bitmap. Producers only set bits (atomic OR), the page bit
before `pending`; the consumer swaps `pending` to 0, then each `stale` word: no overflow, no lock, no lost entry. (d) Per
vCPU a shadow-reset counter. All mask, record and counter updates are atomic RMWs (SeqCst
`fetch_or`/`fetch_and`/`fetch_add`) on both host and resident.

**Who produces entries, who consumes them.**
- A resident write to guest RAM that hits a page with code bits (guest store fault, split-store part, walker A/D update):
  `old = mask.fetch_and(0)`; for each other bit v set PAGES and the page in v's record, kick v; then its own slice-2
  handling (step if its running block is from the page, else invalidate). Clearing all bits fixes S2e's "A/D retry with
  other vCPUs' code bits". After the RAM write it re-reads the mask (after `dmb ish`) and posts new bits.
- Host writes to guest RAM happen only with every vCPU stopped and set FLUSH in every record (as today for one).
- Marking a new code page (below) sets SHADOWS for peers holding writable shadows of it. Guest INVLPG/CR3: nothing.

Consumers drain at every resident entry (miss, linker, abort, split, step, system instruction, stop, request return) and
again **after placing** a block, before entering it, so a write racing the fetch kills it. If that drain kills the
just-placed block, the vCPU steps the instruction (uncached slot block, as slice 2) instead of re-dispatching, so a peer
rewriting the page on every fetch cannot starve it; the linker never links to or enters a target killed this way
(`stale_links`). Blocks die only at their owner's resident entries (an entry whose drain kills the current block steps or
re-dispatches it, never resumes it, as `aborted` does now), so the linker keeps a dead or unknown header as `bad_link`
(second S2e review item) and a drain in its own `find` that kills its source stays `stale_links`. **Cross-modified code**
is seen at the executing vCPU's next resident entry: every serialising instruction is a system instruction (SDM "Handling
Self- and Cross-Modifying Code"); kicks make it prompt. Boot phase: CPUID, SERIALIZE, IRET stay draining entries.

**New code pages and writable shadows (the one synchronous shootdown).** No vCPU may write a source page between the byte
copy and its protection, so marking moves before the copy, into the resident: on a miss it walks the fetch itself (the
host copies from the guest-physical page it is given; host fetch walks and their A-bit writes go), sets its code bit
(`fetch_or`), then reads the writable mask. Install mirrors it (set writable bit, read code mask; any bit → the code-write
path, not a writable install), so no later writable install misses the code bit. For each peer p with a writable bit: read
p's reset counter, set SHADOWS, kick p, wait until the counter changes (not until the bit clears: p may re-set it),
draining its own record meanwhile (mutual waiters both progress); if its own code bit is cleared meanwhile (a write hit
the page), stop waiting: the block placed from the page is then killed and its instruction stepped (as after any failed
post-place check: a dead block, a flush during the miss or a source page without its owner's mark), so every live block's
source pages carry its owner's mark unless a write since cleared it and posted the page. Serving SHADOWS: reset own shadows (`tlbi` own ASID, `dsb ish`), clear own
writable bits, increment the counter (release). A block using the next page: the request carries that page's
guest-physical address, marked the same way first; a fetch fault there cuts the block before it. **Peers that stop
draining.** Before any exit to the host that may block indefinitely (finished at `stop_at` or halt, parked, stopped for
host writes, cancelled), a vCPU serves SHADOWS unasked; if it also flushes, it clears its code bits. Chosen over the host
serving SHADOWS for parked vCPUs (one path, no host access to resident tables).

**Shared guest page tables.** Walkers set A/D by compare-exchange from the value read (success if a peer stored the same
value, else re-walk; built in V3). Change: exhausting `WALK_ATTEMPTS` under contention (data or fetch walk) no longer
halts `unsupported_mode`; the instruction or miss resumes without an install, re-faults and re-walks, outside the
no-progress test (x86 re-walks without bound; the watchdog contains a livelock). **Shadows stay owner-private with local
maintenance** (local `tlbi`, no broadcast; no vCPU writes another's tables). x86 TLBs are per CPU: a PTE write, INVLPG or
CR3 load obliges no other vCPU until it runs its own INVLPG/CR3 (IPIs: boot phase). Shadows are its TLB, its cache its
ITLB (slice-2 keys, flushes unchanged). A writable shadow may outlive a peer clearing D, like a cached dirty TLB entry
(SDM §4.10.4.3), so contract 13 holds per vCPU at install; contract 16 (eager retirement on table writes) is not required
(contracts §3 items 3, 5, 6) and is dropped. Rejected: editing peers' descriptors plus `tlbi vae1is`.

**Stopping a running vCPU (precise).** Slice 2's stop page, built now. x28 moves to frame page + 0x400: admitted fields (≤
3040) stay in the frame page and FEX's entry store `str xzr, [x28, #3776]` lands alone in the next VA page
(PRIVATE+0x24000, pressure.rs's `SPILLS` while families build), mapped to the vCPU's stop granule (one IPA granule per
vCPU, nothing else in it; size read from `hv_vm_config_get_ipa_granule`). Kick v: a resident `hvc` that returns at once;
the host sets v's kick flag and `hv_vm_protect`s v's granule read-only (per-vCPU host mutex). v's next block entry takes a
stage-2 fault to v's thread, which restores the granule and injects a stop (ELR/SPSR from PC/CPSR, PC = the resident stop
gate, EL1h masked). The gate requires ELR = a placed or step block's stop-store PC (else halt), banks the entry state (RIP
= block RIP, CF inverted), drains and re-dispatches. Latency: one block (no in-block loops; helpers bounded). A kick
finding v in the host or resident is served by its next drain (the later fault is an empty stop). **Premise:** `hv_vm.h`
does not document `hv_vm_protect` taking effect on a running vCPU; S3b tests it first. *Result (S3b1, Oct 28,
`experiments/track03/stop-premise/`):* holds — 150,000 kicks at 1/2/4 vCPUs, unloaded and oversubscribed, none lost;
one stage-2 exit per kick (EC 0x24, WnR, DFSC 0x0f, FAR = the stop granule, 16 KiB); no-kick controls zero exits; the
exit usually precedes the call's return. The ms-scale tail (up to 1.6% > 1 ms at 4 vCPUs oversubscribed, fallback
alike) is host scheduling, partly in the protect call and partly the vCPU thread waking late (independent replication:
up to ~10 ms after the call returned), so the bound is **the stop takes effect at the next stop-store after
`hv_vm_protect` returns**; wall-clock latency carries a host-scheduling tail. Replication also showed: a protect issued
while v is in the host stays in force and fires at the first stop-store after re-entry (8,000/8,000) — so the granule
is restored only on the stop exit itself, never on other exits; concurrent protect calls contend (p99 ~200 µs with 8
callers vs ~30 µs with one), so redundant protects are skipped; unkicked peers neither exit nor stall (throughput −5–10%,
unattributed). Fallback: protect, then
`hv_vcpus_exit(v)`; still precise at the next block entry after re-entry. `hv_vcpus_exit` alone stays watchdog only.
Rejected: a virtual IRQ (owner-thread only, lands at any native PC).

## 2. Memory model
**Hardware TSO for what is admitted.** Admission decodes only single general-register loads and stores (1–8 bytes, any
alignment) plus the fence and swap words below; other pair, SIMD, acquire/release, exclusive and LSE words are refused, so
FEX TSO emulation stays off. Evidence before two vCPUs share data (S3t): with EnTSO on, no x86-TSO-forbidden outcome of
MP, SB, LB, WRC, IRIW, 2+2W, S, R over those forms at every width, aligned, unaligned within 16 bytes, crossing 16 bytes
and crossing a line, plus a store made by EL1 code after an exception entry through a second alias (split service). An
EnTSO-off control must show MP or LB violations, else inconclusive. Pair, SIMD, other LSE forms: own cells first.

**Atomicity.** Aligned 1–8-byte accesses are single-copy atomic; unaligned ones that do not cross a 16-byte boundary are
too with LSE2, which the captive M4 vCPU enumerates (MMFR2.AT = 1), pending S3t. Crossing 16 bytes inside a line is not
atomic (owner decision 2). Split-service policy: walk both pages, write the parts through the RAM alias in address order
(any granularity), `dsb ish`, resume; peers may see either part alone. It is ordered after its vCPU's earlier stores
(EnTSO across the exception; S3t checks) and before later accesses (the `dsb`). A page-crossing load is one native load.

**Locked and atomic instructions** stay cut except two the old ordering and atomic contracts (50, 43) need: (a) LFENCE/
MFENCE/SFENCE, which FEX emits as `dmb ld`/`dmb sy`/`dmb st`; admission accepts exactly those words (under TSO only MFENCE
orders). (b) Memory XCHG (implicit LOCK): FEX emits `swpal` only with SupportsAtomics, else an `ldaxr`/`stlxr` loop;
natemu pins LSE in fexc's features and admits `swpal` only at a site of kind `swap` (write intent, nothing committed at an
abort: retry or #PF; WnR not checked strictly). An XCHG operand that crosses a 16-byte boundary takes an alignment fault
(FSC 0x21) at the swap site → stop `unsupported_atomic` (admission refuses it when the address is known). The stop is
precise for data (nothing was written) but not for page-table bits: the write-intent walk before the access has already
set A/D of the pages it walked, as an x86 walk does before a faulting access; and if a crossed page is absent or
read-only that walk faults first, so the guest sees #PF, not `unsupported_atomic`. `swpal` is admitted only with Rt ≠ XZR
(with the result discarded the architecture drops its acquire semantics; FEX never emits that form). Split locks,
LOCK ADD/CMPXCHG/XADD/BT* and CMPXCHG16B wait for the boot phase.

## 3. Covering the old specialized paths
"Delete": the family code, host driver, runner, checker and worker go; the independent models (`i4v_ordering_model`,
`i4v_coherence_model`, `i4v_pae_model`, `i4v_modes_model`) move to `tools/models/` with contracts §3's fixes.

| Old campaign | What the general path needs | Recommendation |
| --- | --- | --- |
| I1 entry, I2 live translation, I3 paging/allocated, address, stack | Nothing new: slices 1–2 cover them (fault sets, guard, split, precision verifier, admission). Port I3 cells the slice-1 fault matrix lacks (CR0.WP=0, 2 MiB leaves, reserved bits, SMEP, carry/overflow) as generated cases. REP and FP cells wait for that ISA. Retained-route completion (42) and the resident stack bound (62): a general-path check or an explicit retirement reason. | Deleted at S3a2 (below). |
| Six-program QEMU check, gate-binary audit | `run_i4v_qemu.py` binds native reports of six programs. Two of them (`live-low-low_operand`, `stack-low-low_stack`) were cells S3a2 deleted, so the check now reproduces only from bundles retained before S3a2; the general-path corpora against QEMU (fixtures, seed corpora, faults) cover those programs' behaviour. The four ordering programs (xchg, lfence, sfence, mfence) live until the ordering family goes. `tools/reference/ref.py` takes its QEMU pins from `run_i4v_qemu.py`. The gate-binary audit (contract 61, `check_i4v_fence_gate.py`) audited the LFENCE completion sink and the families' gates; it retires with the LFENCE sink (contract 51, a boot-phase handoff item). What still exists of 61, the resident gates' save sequences, the vectors and the general gates' ERET restore path, is checked instruction by instruction by `tools/check_resident_stack.py` (S3i2) on the built runtime. | Deleted at S3i (pins moved to `ref.py`); the gate audit's remainder replaced at S3i2. |
| I4 entry/faults/remap/compose/pressure; seeded progress, schedule, partial progress | N vCPUs (S3a), coherence (S3d, S3f). Contracts 45–48 and 53 describe the old global service lock and FIFO; the new design has no global lock, so they retire; progress is checked by S3h. REP partial progress (44): boot-phase handoff. | Mapped at S3h (table there); delete at S3i. |
| I4 events (49) | Interrupt delivery: boot phase (handoff item). | Delete; rebuilt with delivery. |
| Aliases, concurrent stack, stack boundary | Per-vCPU INVLPG (S3c), shared tables (S3f); #SS and split pushes are slice 1. | Re-express in S3f, delete. |
| Coherence (four-level parent, A/D, size) | S3c + S3f. Legacy and PAE variants need S3g. | Re-express in S3f/S3g, delete. |
| Ordering, fence, atomic progress | Fences and memory XCHG (§2), litmus programs (S3e). LFENCE register capture (51) needs SSE: handoff item. | Re-express in S3e, delete. |
| Containment | Tamper hooks per vCPU, cancellation, fresh-process control (S3h). | Re-expressed in S3h (table there); delete at S3i. |
| Controls (39), concurrent controls (33) | Long mode: resident system transactions and a multi-vCPU cell (S3c). Legacy/PAE, PDPTEs, IA-32e activation, far transfers: S3g. | S3c + S3g, delete. |
| Paging-off, mixed modes | 32-bit flat profile + paging-off identity resolver (S3g). | S3g, delete. |
| Paging-off steps, address forms (segment bases/limits, CS limit, A20, 16-bit address size, linear wrap, device, hole) | Nothing this phase (owner decision 1): refused by admission or the resolver. | Boot phase if needed; delete at S3i. |

**S3g (~2–3 runs) stays in this phase:** a `fexc` 32-bit profile through allowlist, precision patches, verifier and
admission (flat segments; FEX loads segment bases from the frame for explicit DS/ES/SS/CS prefixes in 32-bit code, so
admission refuses those prefixes), code mode in the key, `control()` over the existing legacy/PAE walkers, hidden PDPTEs
in the bank, and resident transactions with descriptor checks, listed from a disassembly of the pinned kernel's startup
code: at least CR0.PE/PG, CR4.PAE/PSE, EFER.LME, LGDT, far JMP/RET, likely MOV Sreg after LGDT, LTR, CPUID and PUSHF/POPF.

### S3a2: covering and deleting the I1–I3, address and stack paths
**Built (Oct 27).** Program-driver description lines (general, no per-case code): per vCPU `cpl 0|3`, `wp 0|1`,
`smep 0|1`, `nxe 0|1`; per space `large ADDR` (the 2 MiB range one PDE leaf; guest RAM holds one such frame per run) and
`entry ADDR LEVEL set|clear BITS` (any entry of a walk, PML4E..PTE); code may sit anywhere canonical. The resident needed
nothing new: its walker already implemented CPL0/3 rights with WP, SMEP and NXE, reserved bits (51:40, XD without NXE, PS
in a PML4E/PDPTE, 20:13 in a 2 MiB leaf), 2 MiB leaves shadowed as 4 KiB pieces with A/D on the PDE, and upper-half
shadows (TTBR1). Not supported: CR4.SMAP (refused, `unsupported_mode`; S3c). `tools/run_slice1_faults.py` gained an SDM
model of the driver's tables (`Machine`, unit-tested in `tools/test_run_slice1_faults.py`), fetch checks before each
instruction's accesses, per-page leaf A/D comparison (data exact, code A where fetched), cases that run to their end
(QEMU full run) and generated paging cells: 6 controls (CPL3; CPL0; CPL0 WP=0; CPL0 SMEP; CPL0 SMEP with supervisor
code; NXE=0) × mutation (unmap, read-only, supervisor, reserved, non-present garbage on a data page; XD, supervisor, unmap,
reserved on the code page) × level 1–4 × 4 KiB/2 MiB leaf, rotated over the corpus; the 14 position-independent
access-form programs (data forms, push/pop/ret, page-crossing accesses) take every control × mutation of the leaf of
each page they touch plus a rotation of the upper levels, at three placements in which the code, the data/stack pages
and the store base `rdi` (its own page) sit under different PML4 entries (all lower half; all upper half; code alone in
the upper half), so a mutation hits the access under test rather than the prelude's store or the fetch; the runner
refuses a run in which an access-form program never faults at its own access (1,236 of the paging cells do; every
program, including pop and ret stack reads and upper-half loads and read-modify-writes). Results: the fault set grows
from 434 to 3,169 cases (2,724 paging cases covering all 252 control × mutation × level × leaf-size cells; 522 run to
their end, 846 fault on fetch), all match at block sizes default and 1; the multi-vCPU corpora (N=4) take the paging cases without a
2 MiB page. No resident change was needed and none is claimed.

**Walker refusals the boot phase must respect (prerequisites).** The walker (`crates/abi/src/paging.rs`) treats any
PWT, PCD or PAT bit (entry bits 3 and 4 at any level, bit 7 of a PTE, bit 12 of a 2 MiB leaf) as non-write-back and the
resident halts (`shadow status=1 detail=7`) when such a page would be shadowed; under the default PAT some of these
combinations are write-back on x86. This is a refusal, not a wrong result, and the model and corpora never set them; a
guest that maps owned RAM that way needs the walker to evaluate the PAT first. PS in a PDPTE is reserved: there are no 1 GiB pages, so the
boot-phase CPUID must report Page1GB=0. The A bit of a non-leaf entry is set by the walk but not compared (leaf A/D are).

**Old checks → covering check or retirement reason.** "Faults" = `run_slice1_faults.py`; "cells" = its paging cells.

| Old check | General-path check, or reason |
| --- | --- |
| I1 control, live_load ×4 (exact effects, ADD flags incl. carry/overflow, preserved state: 1, 2) | Fixtures and seed corpora vs QEMU (registers, flags, memory), ASM suite, `frame_intact` every run. |
| I1 fault (unknown native exception traps, no guessed guest fault: 56) | Resident `aborted`: an abort at an unknown PC or with a syndrome not its site's kind halts (`unknown_abort`/`site_mismatch`) before any effect. S3h: `--tamper site` (sites off the instruction grid → `unknown_abort`) and `site-kind` (load/store sites claiming to be guard stubs → `site_mismatch`) on every vCPU at N = 1..4 (`contain`). |
| I1 eight submission mutations, N=2 refusal (55, 54) | Admission refuses malformed objects (host unit tests); resident `request` refuses bad replies (`--tamper offset` → `bad_reply`); link defences (`--forge-header`, `--tamper*`); N=0/5 refused (`limits`, unit test). |
| I2 live, capacity (38) | Host requests = distinct blocks per vCPU (`run_slice3_vm.py`), exact-request compile cache (unit test), small-cache flush probes. |
| I2 source_stale (34) | S2e SMC probes, FEX SelfModifyingCode in the ASM suite, seed corpora own-page stores. |
| I2 context_stale | Code-cache key holds CR3/CPL/mode/controls; no context changes without system instructions: S3c (CR3, WP, NXE programs). |
| I2 cancel, bad_reply (35) | No cancellable request in the synchronous design; peer cancellation: `limits` forged header; bad replies as above. |
| I3 cold, carry, overflow | Fixtures (edge-carry-chain), fault probes' prelude (`add` with OF) and division probes (`cmp` with CF, held inverted) at faults. |
| I3 load_np, store_ro, parent_ro, wp_off, user_denied, reserved, fetch_nx, smep, large (7, 9, 10, 11, 12) | Cells: np-garbage, read-only at CPL0 (leaf, parent levels), CPL0 WP=0 (runs to its end, D set), CPL3 supervisor, reserved per level, XD/SMEP on the code page, 2 MiB leaf variants with A/D on the PDE. Non-leaf A is not compared (policy, §3 item 7 of the inventory). |
| I3 read-only fill until D, one upgrade (13) | Leaf D compared on every case (D only when written); the upgrade count is internal (resident `upgrades`), not compared. |
| I3 pressure, helper (5, 41) | Exact state at every fault across the corpora at block sizes default and 1 (precision verifier, admission); division-helper probes (#DE through the 128/64 helper). |
| I3 fp (ADDPS, MXCSR), partial (REP, 44) | Wait for SSE / REP admission (boot-phase handoff; contract 44 already listed). |
| Address {high, low} × cold, load_np, user_denied, fetch_nx, smep | Cells on the access-form programs at low, high and high-code placements. |
| Address low_operand, private_operand (21) | High-code placement (upper-half code, lower-half operands); non-canonical probes at the resident's private range (#GP, nothing written). |
| Stack {high, low} × cold, low_stack, ro/np first and after first, wp_off, user_ro/denied/ok, parent_ro | Push/pop access forms × cells at three placements; slice-1 page mutations of stack pages in programs with several pushes. |
| Stack fetch_nx, smep; ss_first, ss_after_first, ss_span, gp_after_pushes, read_np_after_pushes (8, 37) | Code-page cells (no compile before a first-instruction fetch fault); non-canonical stack/data probes (#SS/#GP), non-canonical-end probes; slice-1 unmap of pop pages. |
| Address 16 and stack 48 reply-word flips (35) | Producer transport (packets) gone; the design core (no false success, failure is never #PF) as I2 bad_reply. |
| 42 retained-route completion before a remap acknowledgement | Retired: there is no acknowledgement protocol; each vCPU's shadows are its TLB, which may keep a translation until its own INVLPG/CR3 (SDM §4.10.4). Remaps with per-vCPU INVLPG: S3f. |
| 62 resident stack bound | `tools/check_resident_stack.py` (S3i2): a runnable audit of the built runtime's call graph (no recursion, no unaccounted indirect branch, declared stack switches only); on the phase's final binary 11,568 bytes, 16,000 with one nested entry to its halt, of an exception stack grown to 32 KiB at S3i2. |

**Deleted.** Host drivers `i1.rs`, `i2_i3.rs`, `i4v.rs` (address), `i4v_stack.rs` (1,042 lines) and the I3 scenario
branches of `recovery::prepare` plus three report helpers and `Vm::run` only they used; `run_i1.py` keeps only the I4
cells, `run_i4v.py` loses the address baseline and stack cells; `check_i3_pressure.py`, `i3_worker.py`, `i4v_worker.py`
(+ test) and the I3 allocated producer package except `fex_export.py` (the I4-V producer build applies it). **Kept until
their campaigns retire:** runtime/abi family code (modes are layered: `translation`, `recovery`, `pressure`, `partial`,
`stack` and `validation` serve every family), `check_i1`/`check_i2`/`check_i3_paging`/`i4v_check`/`i4v_stack_check`
(imported as helpers by the I4 and I4-V oracles), `fixtures/i1` (the I4 cells' body).

## 4. Chunks, in order (each keeps slice-1/2 sets green: fixtures, seed 4242, control flow, faults, ASM tests, probes)
- **S3r, SMP reference (parallel, independent author).** The S2p multiboot stub gains AP start-up (INIT/SIPI), per-vCPU
  entry state and an all-CPU register and memory dump; pinned QEMU system mode `-smp 2..4`; programs deterministic by
  construction (handshake flags). *Check:* self-test plus a corrupted-dump control.
- **S3t, TSO and atomicity probe (parallel, independent author, hand-written arm64, no FEX).** §2's cells on 2–4 vCPUs,
  plus page-crossing loads and mixed sizes on overlapping bytes. *Check:* EnTSO-off control shows violations; EnTSO on,
  zero forbidden outcomes in ≥10^6 trials per cell, judged by `i4v_ordering_model.py` (locations as data); zero torn
  values for unaligned accesses within 16 bytes, `swpal` included, with a forced-tear positive control (two narrower
  stores) that must show tears; tear counts for accesses crossing 16 bytes reported.
- **S3a, N vCPUs.** Layout and stride, vCPU id and ASID, global masks, records and counters, owners running the execution
  service, per-vCPU compiler, driver `cpu` lines, per-vCPU `check_links`, A/D re-walk change. *Check:* N = 1..4 (0 and 5
  refused), each vCPU runs a different corpus program on disjoint pages concurrently and matches its reference, also
  oversubscribed (more busy threads than cores) so threads migrate; host requests equal distinct blocks per vCPU; EnTSO
  read back on every vCPU; a tamper-forced halt on any vCPU stops and joins all, then the VM is destroyed.
  *Built (Oct 27):* backing block v at `v * (BACKING_BYTES + code area)` holds vCPU v's window, lookup, metadata and code
  areas at vCPU 0's offsets (guest RAM and the global area only in block 0); private and shadow descriptors are nG, ASID
  = vCPU number + 1 (TLBI by ASID; ASID 0 stayed the families' untagged world with global shadows until S3i2 removed it; a precaution, see §1). `State::vcpu`; global area: code and writable masks, `Record {pending, stale}` per vCPU,
  reset counters (abi `cache`), host and resident producers (`page_written`, runtime `cache::written`: swap the mask to 0,
  post page then `PAGES`), drain (swap `pending`, then words). A halt (also on the host's new `REPLY_PARK`, which ends
  a vCPU at `stop_at`, a fetch fault or no code) resets shadows and writable bits first. `Fault::Contended` replaces the
  walk-exhaustion halt (resident resumes; host fetch re-walks). `owners::supervise` is generic; the program driver runs
  every vCPU through it (description `cpu` sections, `body ADDR HEX`, `--tamper-vcpu`); `tools/run_slice3_vm.py`
  (`corpora`, `smp`, `limits`). Not yet: kicks, post-write re-check, peers' writable-shadow shootdown (S3b/S3d).
- **S3b, precise stop.** Premise first: a vCPU in a hot linked loop (zero exits), kicked by `hv_vm_protect` alone (no
  `hv_vcpus_exit`), must stop within 1 ms per kick, else the fallback; a no-kick control never exits; per-kick latency
  reported. Then frame bias (moving `SPILLS` or the stop page), granules, kick, stage-2 exit, gate. *Check:* a hot linked
  loop kicked 10^4 times by a peer ends equal to QEMU, every stop at a stop-store PC; kicks into host/resident harmless;
  forged PC halts.
- **S3c, long-mode system instructions.** Reply kind `system` (host-decoded with FEX's decoder, cached). Resident
  transactions: MOV to/from CR0/CR3/CR4 (WP, PGE, SMEP; reserved bits, PCIDE, SMAP or CPL > 0 → #GP(0), LOCK → #UD),
  INVLPG and CR3 loads (own shadows and whole cache, same-value CR3 too), RDMSR/WRMSR EFER (NXE; LME while PG → #GP).
  *Check:* QEMU system-mode programs (CR3 switch, INVLPG after a PTE change, WP toggling, NXE); fault cases against
  `i4v_modes_model.py`; a PTE change without INVLPG accepted as either history by `i4v_coherence_model.check_observation`;
  concurrent controls (33): one vCPU writes CR3/WP/NXE while peers run the same tables and code unaffected.
  *Built S3c1 (Oct 28, single and multi vCPU; concurrent controls are S3c2):* a miss at a system instruction gets reply
  `REPLY_SYSTEM` with the host's decode (`State::system`; FEX's cut gives map/opcode, host `system.rs` decodes the operands
  of `0f 20/22`, `0f 01 /7` m, `0f 30/32`; cached with fexc's answer); the resident checks it (`System::well_formed`) and
  executes it (`execution.rs` `system`): #UD (LOCK, CR1/5-7/9-15), #GP (CPL > 0; CR0 bits 63:32, PG or PE cleared, NW
  without CD; CR3 bits 63:40; CR4 reserved for the model incl. PCIDE, SMAP, PAE/LA57 changes; EFER reserved bits, LME change
  under PG; any MSR but EFER), else commits; CR0[31:0] reserved bits are ignored, ET hard-wired. Any CR0/CR4/EFER change and
  every CR3 load flush the vCPU's cache and reset its shadows; INVLPG drops shadows and blocks of the address's 2 MiB range.
  Refused (`unsupported_system`): MOV CR2/CR8, CR0 bits but WP, CR4 feature bits (OSFXSR ...), CR3 PWT/PCD. Driver
  `identity` spaces; `tools/run_slice3_vm.py system` (QEMU `system.json` programs; fault cases by `tools/system_fault_model.py`
  at QEMU's prefix state, five cases also by four `i4v_modes_model` profiles; the PTE change without INVLPG by `i4v_coherence_model`).
  *Built S3c2 (Oct 28, concurrent controls, contract 33):* generated QEMU programs `sys-ctl-*` (`programs.ctl_program`) at 2 and 4 CPUs
  (CPU 0 toggles CR3/WP/NXE while peers run the same tables and code; finals depend on each CPU's own controls) run by
  `tools/run_slice3_vm.py system` at block sizes default and 1; no resident change was needed (controls, cache and shadows are per vCPU).
  *Boot-phase items from S3c1 review:* MOV from CR2 (the page-fault handler reads it); the CR4 bits Linux sets (OSFXSR,
  OSXMMEXCPT, OSXSAVE, FSGSBASE, UMIP) and CR0 TS/MP/NE/AM changes are refused today; MSRs other than EFER #GP (FS/GS base,
  KERNEL_GS_BASE, STAR/LSTAR, PAT … needed); CR4 profile bits (VMXE, SMXE, PKE, UMIP) become reserved from the CPUID
  profile; every system instruction is a host round trip and every CR3 load flushes the whole cache (correct without
  PCID, costly on context switches).
- **S3d, cross-vCPU code coherence.** §1's marking, writable mask, shootdown, self-reset, resident fetch walk, page posts,
  post-write check, post-place drain and step. *Check:* a unit test that an A/D refusal with a peer's code bit ends;
  against S3r: a peer writes fresh code then a flag and the other vCPU jumps to it (new page, reused data page), patch a
  function and run it after `mov cr3, cr3`, all vCPUs patching and running in rounds, data on a peer's code page; a stress
  posting the page of a vCPU's running block (never `bad_link`; steps, stale links counted) at block sizes default/1; a
  page rewritten on every fetch (the runner finishes); two vCPUs translating pages the other writes (no deadlock); a vCPU
  finishing while a peer misses on a page it held writable.
  *Built (Oct 28):* the resident walks each miss's fetch itself (abi `execution::fetch`, `State::fetch`/`fetch_fault`; contended
  walks re-walk), marks the page (`cache::mark`) and shoots down peers' writable shadows (`execution::mark`: counter read, bit
  re-checked, `SHADOWS` + kick, wait while draining its own record; own bit cleared → stop waiting) before the host copies the
  bytes from the given guest-physical page; host fetch walks, their A-bit writes and kicks are gone. The next page is walked
  only when the host's bytes ran out at the page end (reply `REPLY_NEXT_PAGE`: the compiled block ended `end_of_input` there, or
  its first instruction did not decode) and marked the same way; its fault cuts the block (or is the stop for a straddling
  instruction). The host's stop RIP is in `State::stop_at` (nothing is fetched there). After placing, the resident drains again;
  a dead block, a flush during the miss or a source page whose mark was cleared (a peer's write, possibly drained before the
  block existed, or its own fetch walk's A write) kills the block, if live, and steps the instruction instead (`stale_links`
  in the linker). (The first build stepped without killing a block whose only fault was a lost mark: it stayed in L2,
  unmarked, and no later write reached it; found by review. `stale@3/4` targets it; 20 runs did not provoke it before the
  fix, since a peer clears the mark before it posts, so the wait usually ends before the post is drained.) A stop at the step block drains and enters the step block again (it
  holds one already-fetched instruction), so kicks faster than a block entry cannot discard the step: without this, the data
  stress below at block size 1 did not finish in 6.5 min. Walker A/D writes into a code page no longer refuse and re-walk:
  `guest_memory::CodeWrites` clears and posts first, then writes (no livelock, whoever re-marks the page; unit test), and every
  A/D or split-store write through the RAM alias is re-checked after `dmb ish` (`cache::rechecked`). A writable install sets its
  bit before it reads the marks (an install after an eviction reads them again, else installs read-only). Halts already served
  `SHADOWS` unasked (S3a). INVLPG's spurious code bits stay (conservative). Check: `tools/run_slice3_vm.py coherence`
  (`programs.coherence_all`, QEMU reference): fresh code on a new page and on a reused data page, 6 patch rounds (also after
  `mov cr3, cr3`), every CPU patching in turn, data on a peer's running code page (stress), a page rewritten with its own
  bytes on every fetch, CPUs translating each other's written pages, writers ending before or while a peer translates their
  page, and (`stale`, 3 and 4 vCPUs) a function called in a loop until a patch shows while peers store data on its page — 20
  programs × 2 block sizes, all match; negative control (shootdown disabled): `fresh-new` and `fresh-reused` fail
  at 2 and 4 vCPUs. **Cliff:** a peer storing continuously to data on the page a vCPU runs from makes that vCPU step every
  instruction (each shootdown is answered by the next faulting store); `data@4` takes ~120,000 steps for 20,000 loop rounds.
- **S3e, ordering on the general path.** Fences, memory XCHG (`swap` sites, alignment stop), generated litmus programs
  (sb, mp, lb, wrc, iriw, 2+2W, sb-mfence, sb-xchg, XCHG chains) at N = 2..4, with unaligned and split-store variants.
  *Check:* outcomes within `i4v_ordering_model.py`'s allowed sets in ≥10^5 trials per cell; chains linearizable; XCHG
  faults before commit leave memory unchanged (single-vCPU cases against the QEMU prefix and signal references).
  *Built (Oct 28):* fexc series10 (`natemu-raw/fexc-native-20261026-01/series10`; shims only, no new patch: LSE was already on
  through the `atomics_lse` feature default, so FEX emits `swpal`): the allowlist admits memory XCHG (86/87, LOCK allowed only
  there), LFENCE/MFENCE/SFENCE (exactly `0f ae e8/f0/f8`, no 66/F2/F3) and PAUSE (`f3 90` without REX.B: FEX's `yield`; not in
  the list above, admitted because the S3r spin loops use it); FEX's `AtomicSwap` is site kind 12 `swap` (`data_reg` = Rt, Rs;
  write intent; an address guard, no split check; a fault point for the precision patches). Objects on the 64 fixtures and the
  44 seed-4242 programs are identical to series9. Admission decodes exactly `dmb ld`/`dmb sy`/`dmb st`, `yield` and
  `swpal{b,h,,x}` (no other swap, barrier or hint); a swap word must be a `swap` site describing it (base not x28/sp, width,
  Rt, Rs, no offset), and a guest-RIP-addressed swap crossing 16 bytes is refused. The resident accepts either WnR at a swap
  site (translation/permission fault: walk with write intent, retry or #PF, nothing written), and an alignment fault (FSC
  0x21) at a swap whose operand crosses 16 bytes banks the state at the instruction and stops `unsupported_atomic`, which (like
  a guest fault) ends only that vCPU: nothing was written to data, but A/D of the walked pages may already be set, and an
  absent crossed page gives #PF instead (§2); the faults runner compares registers and memory for these four probes, not leaf
  A/D. Admission also refuses `swpal` with Rt = XZR (fixture case). Deviation: the guest-RIP crossing case surfaces as an
  admission refusal (host error), not as that stop. Checks: `tools/run_slice3_vm.py litmus` (generated families sb, sb-mfence, sb-xchg, mp, lb, 2+2W, XCHG chain at
  N = 2..4, wrc at 3..4, iriw at 4; locations aligned, unaligned inside 16 bytes, and page-crossing (`split`; exchanged words
  stay aligned; S3f added `split-hi`, the same placement with the changing byte on the second page — in `split` it sits on
  the first, so the second half of a split store was never observed): 69 cells × 100,000 trials, zero outcomes outside `i4v_ordering_model`'s enumerated sets (the model gained
  final-memory outputs for 2+2W and chains), every chain outcome linearizable (all n! orders seen); control (sb-mfence without
  MFENCE, aligned/unaligned) shows the forbidden outcome in all 6 cells; a split store completes with `dsb ish`, so SB cannot
  relax there, and the split service is slow (an exception round trip against a race window of nanoseconds), so the split
  cells skew to one outcome: the other CPU nearly always finds the store complete. The control refuses a split variant, and
  (from S3f) judges run diffs and trial counts like the normal run). Faults: XCHG access forms join the paging matrix and four split-lock
  probes (`unsupported_atomic`, QEMU prefix state): 3,443 cases match at block sizes default and 1; an XCHG stride program
  joins the loop faults (signal reference, 846 cases × 3 sizes); two XCHG SMC probes. S3r `handshake`, `token-ring`,
  `xchg-lock` at 2–4 vCPUs match the model and live QEMU. ASM suite 235 pass (227 + Pause, Primary_86/87/87_2/87_3,
  15_XX_5/6/7); Primary_87_Atomic16/32/64 are split locks (unsupported). Not built: LOCK ADD/CMPXCHG/XADD/BT*, CMPXCHG8B/16B,
  split locks (boot phase); LFENCE register capture (51).
- **S3f, shared-table coherence.** Tests only: A/D races on one PTE from N vCPUs while one clears A/D with plain stores
  and its own INVLPG; remap with per-vCPU INVLPG; 2 MiB ↔ 4 KiB changes; aliases across vCPUs; contended walks never halt.
  *Check:* each vCPU's observations and final PTE bytes against `i4v_coherence_model.py`.
  *Built (Oct 28; no resident change was needed):* `tools/bench/smp/sharedpt.py` generates the programs (one shared body,
  one shared table set loaded by every CPU at CPL0, plain-store handshakes, a private log per CPU) and judges the logs through
  the model (`walk`, `Cache`, `access_outcomes`, `check_observation`; the observation passed is what a CPU can see: the frame
  and the leaf bytes; a fresh walk replaces the CPU's single cache entry); `tools/run_slice3_vm.py sharedpt` also compares
  the schedule-independent memory (final PTE bytes, data pages, flags) with pinned QEMU and requires every CPU to reach its end.
  Programs, each at 2 and 4 vCPUs, block sizes default and 1, 10 runs each (32 configurations, 320 runs, all pass):
  `ad-race` (450 synchronized rounds per run, the PTE sampled by every CPU after its access and exactly by CPU 0 after all
  acknowledged: A, or A+D when anyone wrote; a free race of 10,000 accesses per CPU against CPU 0 clearing with plain stores +
  its own INVLPG, where every sample must be a state the model reaches), `remap` (41 rounds: before its own INVLPG a CPU reads
  and stores through V, after it only the new frame is permitted), `large` (a PDE flipping between a 2 MiB leaf and a table in a
  random sequence, with large→large and table→table rewrites; exact PDE/PTE A/D after every round), `alias` (two linear
  pages onto one frame, 200 rounds, roles drawn so one or both PTEs get A, A+D or stay clean; exact A/D of both PTEs per
  round; every word verified through a third alias), `contended-{data,fetch,both}` (a clearer cycling a PTE through four
  values with 64 spins between stores while the others INVLPG and access: 6,000 walks per walker) and `contended-max` (no
  pause: 150 walks). Histories seen over all 320 runs: A/D races: round samples after a read: A 19,829, A+D 7,071; after a
  write A+D 27,100 (never less); rounds' final PTE A 5,800, A+D 12,200; free race (all three states, no D without A): reads
  saw clean 33,436, A 114,219, A+D 270,661 and writes clean 21,322, A 885, A+D 401,317 (the 'write → A' samples are a write
  whose D a later clear removed and a reader's walk then set A). Remap: after the CPU's own INVLPG always the new frame
  (4,920); before it, 10,800 reads and stores through a stale translation (old frame) and 4,720 each of the new frame (CPUs
  that had no cached translation); natemu never replaces a cached translation by itself, so "new before INVLPG" occurs only
  without a cached entry. Large: 4,680 pre-INVLPG reads of T saw the new state, 3,200 the old; every post-INVLPG access hit
  the new state (2,680 from a 2 MiB leaf, 2,240 from a table). Contended: the Contended path is exercised: with
  `Fault::Contended` turned back into a `unsupported_mode` halt (control below) every contended run halts within about 20–200
  walks; unmodified, 160 of 160 runs finish and the final PTE bytes match the model. **Finding (not a defect of the
  contract):** a CPU that stores to the PTE with no pause at all (`contended-max`) starves the walker's compare-exchange: about
  200 walks/s (a plain 64-spin pause gives tens of thousands), so 6,000 such walks exceed the 10 s watchdog; x86's locked
  A/D update cannot be starved this way. The watchdog contains it, as §1 says; no kernel stores this fast to a PTE.
  Negative controls (each applied once, then reverted completely; `git diff crates/` showed only the XZR admission change
  afterwards): (1) the walker's A/D update as a plain store instead of a compare-exchange: `ad-race` fails in 11 of 20 runs at
  4 vCPUs (a write's D lost to a reader's stale store: PTE sample A without D after its own write), none at 2, none unmodified (20
  of 20); (2) INVLPG keeping the shadows: `remap`, `large` and `alias` fail in all 24 runs (after INVLPG the old frame,
  A/D not set); (3) `Contended` as a halt: all 80 `contended-*` runs fail. Model limits: the model's INVLPG of a 2 MiB leaf drops one
  4 KiB entry; natemu drops the whole 2 MiB range (stricter, so every permitted history still holds).
  *S3e review carry-overs, done with S3f:* admission refuses `swpal` with Rt = XZR (fixture case); the XCHG text above and the
  S3e note state that the alignment stop leaves A/D possibly set and that an absent crossed page is a #PF, and the faults
  runner says it does not compare leaf A/D for those probes; the `split-hi` litmus variant (a rerun at 20,000 trials per
  cell: 90 cells = 24 aligned + 24 unaligned + 21 `split` + 21 `split-hi`, zero outcomes outside the model, the control
  still showing the forbidden SB outcome in all 6 cells); `litmus --control` judges run diffs and trial counts and refuses
  split variants; `tools/test_i4v_ordering_model.py` runs by path from the repo root.
- **S3g — dropped from this phase (owner decision 3); kept for the boot phase.** *Was:* 32-bit flat protected mode and transitions. As §3, disassembly first. *Check:* S3r programs running a
  `startup_32`-like path (paging off → PAE tables → LME → PG → far jump to 64-bit → compatibility mode → paging off),
  legacy two-level with 4 MiB pages; fault matrices against `i4v_pae_model.py` and `i4v_modes_model.py` (contracts 28–31);
  mixed modes on N vCPUs sharing a code page; explicit segment prefixes refused.
- **S3h, containment and progress.** *Check:* tamper on each vCPU at N = 1..4 → all stopped, joined, VM destroyed, fresh
  process healthy; a peer that never acks a shootdown, compile-server death and a malformed reply on vCPUs 1–3; all vCPUs
  finish under sustained cross-vCPU code writes; one vCPU finishes while a peer misses on its page; watchdog contains a
  stuck wait.
  *Built (Oct 28):* `tools/run_slice3_vm.py contain`; no resident change, no containment defect found: every failure ended
  as §1 designs it (a halt or host error cancels the peers, which stop at their next host exit or are forced out; a
  resident wait on a peer that never drains is bounded only by the waiter's watchdog). Driver hooks (opt-in, on
  `--tamper-vcpu V`; nothing changes without them): the reply tamper now runs in the service loop at every reply but a
  park, after the reply kind is set, so it may change the kind (`--tamper next-page`: a block request answered with
  `REPLY_NEXT_PAGE`, then again after the next page was walked → `bad_reply`; S3d review carry-over); the older tampers
  (`offset`, `word`, `literal`, `record`, and `site`/`site-kind` below) act only on a block reply, so they no longer fire
  at system replies; `--tamper site`
  and `site-kind` (above, contract 56); `--ignore-kicks` (every stop resumes at its stop store without the gate, so the
  vCPU never drains while in linked code); `--kill-compiler N` (SIGKILL of its `fexc --serve` before request N + 1); `--stall-compiler N` (SIGSTOP there: alive,
  never answering).
  General mechanisms added: (a) a watchdog stop reports where the vCPU was (`guest` code or `resident`, native PC) and
  which peers' `SHADOWS` request was still pending; (b) a failed compile-server request names the server, its PID and its
  exit (`owner 2: fexc --serve (pid P) exited (signal: 9 (SIGKILL)): …`, exit status 1); (c) every `run-program` writes a
  `teardown:` line after destroying the VM, on success and on failure (before, a kicker or setup error returned before
  the explicit destroy and left it to `Drop`): vCPUs joined (the line is written after the joins: the vCPU threads that
  returned a result), vCPUs destroyed with status 0, the VM's destroy status and
  the child processes left (`waitpid(-1, WNOHANG)`: ECHILD = no compile server alive or unreaped); (d) every
  compile-server request has a deadline for its whole reply (`--compile-deadline-ms`, default 30 s: the slowest compile
  measured on the corpora, 6,080 vCPU runs at N = 4 and at N = 2 with block size 1, took 13.4 ms with the server's start);
  on expiry the server is killed and the run fails as when it dies (`owner k: fexc --serve (pid P) exited (signal: 9
  (SIGKILL)): timed out: no reply within 30s, killed`). The request body is written without a deadline (a few KiB,
  which the pipe takes whether or not the server reads).
  *Checks (each failing run is one process, must end within 10 s, or the watchdog + 5 s, and print a clean teardown
  line; a fresh process follows each at once with N fixture programs on N vCPUs, which must match QEMU):* (1, 4) ten
  tampers (forged header, link-point offset, next-page twice, system decode, linked word, literal and record before a
  flush, forged stop PC, sites off the grid, wrong site kind) on each vCPU v at N = 1..4, the others in linked loops:
  v halts with its defence's stop, the others are `cancelled`, exit 0: 100 runs (10 tampers × 10 vCPU positions), all as stated, each run ≤ 0.16 s. (2) A peer that never answers a
  shootdown (`unacked`, vCPU k = 1..3 of 4; k keeps exiting on host kicks every millisecond, so only the waiter's own
  watchdog can fire): vCPU 0 stops `watchdog`, `resident`, with k's `SHADOWS` pending, k `cancelled`, the others ended;
  the untampered control matches QEMU with one shootdown: 3 of 3 (each 1.04 s at a 1 s watchdog), control ok. (3) vCPU k's compile server killed before its
  51st request (k = 1..3 of 4): exit 1, the vCPU, server and signal named: 3 of 3 (≤ 0.05 s); the same server stopped instead
  (`--stall-compiler`, deadline 2 s): exit 1, the vCPU, server and "no reply within 2s" named: 3 of 3 (2.06 s). (5) A guest `jmp $` on vCPU v
  of 4 (watchdog 1 s) stops `watchdog`, `guest`, while the others reach their ends and match: 4 of 4 (1.04 s at a 1 s watchdog). A guest that
  legitimately spins (vCPU 0 waits in a linked loop for a flag vCPU 1 raises after 1.5·10^9 delay rounds, ~0.7 s): a
  `guest` watchdog stop at a 300 ms budget, both vCPUs at their ends at the default 10 s. Progress (as `coherence`, QEMU
  reference, block sizes default and 1): `mutual`, `data`, `rewrite` at 2 and 4 vCPUs and `stale` at 3 and 4 (sustained
  cross-vCPU code writes), `finish` and `finish-late` at 2 and 4 (vCPUs ending before or while a peer translates their
  page): 12 programs × 2 block sizes, 24 of 24 match. Fresh-process controls: 113 of 113 healthy, each with a clean teardown line.
  **Watchdog: legitimate spin or containment failure.** The watchdog bounds each span inside `hv_vcpu_run` without a host
  exit (host work between exits is not counted), guest code and resident waits alike. A guest that legitimately spins
  past the budget gets the same stop as a loop that never ends: the budget (10 s, `--watchdog-ms`) is policy, and the
  report is the diagnosis: `guest pc=…` (the guest's own loop; a containment failure only if the guest was waiting for
  something the emulator owed it) or `resident pc=… shadows_asked=[k]` (the emulator itself waited, on vCPU k). A
  resident wait for a peer also counts the peer's host service time (a compile) into the waiter's span: milliseconds in
  practice, not bounded by design (the compile deadline bounds each such compile).

  **Old containment and progress checks → S3h check or retirement reason** (S3i deletes the families: drivers
  `i4v-containment`, `i4v-progress`; `check_i4v_containment.py` and its test and testdata, `test_containment_supervisor.py`,
  `check_i4v_schedule.py`, `check_i4v_progress_protocols.py`, `check_i4v_atomic_progress.py`,
  `check_i4v_partial_progress.py` and their tests, `check_i4_pressure.py`, `run_i4v.py --progress/--containment`,
  `run_i1.py --i4-pressure`).

  | Old contract or check | General-path check, or reason |
  | --- | --- |
  | 45 fault service serialized (exclusive service, FIFO, one operation per vCPU; `check_i4`, `check_i4v_progress_protocols`, concurrent controls and coherence traces) | Retired: there is no global service lock or queue. Each vCPU serves its own faults in its own resident; cross-vCPU state is atomic RMWs on masks, records and counters (§1). Successor property: a waiter drains its own record while it waits, so mutual waits progress; checked by `mutual` (`coherence`, `contain` progress). Guest-visible consequences: `coherence`, `sharedpt`, `litmus`. |
  | 46 acknowledgement after covered effects; admission reopens after the owner set | Replaced by the one synchronous shootdown (§1): the waiter goes on only after the peer's reset counter moved (its `tlbi` + `dsb ish` done). `coherence` `fresh-reused` (negative control: shootdown off fails); `contain` `unacked` (served when untampered; never served: the waiter waits until its watchdog). Remap acknowledgements: retired at S3a2 (42). |
  | 47 progress under sustained maintenance (≤ 8 queued heads, round robin; `check_i4_pressure`, concurrent families) | Progress without a fairness bound: `contain` progress (every vCPU reaches its end under sustained cross-vCPU code writes and while a peer translates a finishing vCPU's page), `litmus` (every vCPU completes its K iterations), `sharedpt` contended walks (6,000 per walker). A block killed as it is placed steps one instruction, so a miss always progresses; known limits, both measured: `data@4` steps every instruction (S3d cliff) and a no-pause PTE store loop starves walks (S3f, watchdog-contained). The heads bound retires with the queue. |
  | 48 capacity exhaustion is a state-preserving refusal (queue full; I2 capacity) | Retired: no queue; invalidation records are bitmaps (no capacity, no overflow, no lost entry), and a full code cache flushes (probes with a small `--code-cache`). |
  | 53 seeded schedules replay exactly | Retired: a test mechanism of the old coordinator's schedule points. The general path runs real concurrency; outcomes are judged by models (`litmus`, `sharedpt`) or by programs deterministic by construction against QEMU (`smp`, `coherence`). |
  | 57 contained failure with the original cause, within bounds | `contain`: tamper halts (typed stop name), compile-server death (vCPU, PID, signal), a compile server that never answers (killed at its request deadline; vCPU, PID, deadline named), a peer that never acknowledges (waiter `watchdog`, `resident`, the peer named), every run within its bound (measured below). Old kinds: worker exit → compile-server death; a hung worker → the request deadline (`stall`); malformed reply → `offset`, `next-page`, `system`; resident panic holding the coordinator lock → any resident halt (there is no lock); withheld acknowledgement → `unacked`; cancellation with deferred work → retired (requests are synchronous: nothing is deferred; peers stop at their next host exit or are forced out). |
  | 58 failure never fabricates completion | `contain`: the tampered vCPU stops `resident` with its reason and peers `cancelled`, never `reached`; a host error exits 1 without a state report. The frozen-trace replay retires with the trace; a halt banks the state, a `watchdog` or `cancelled` stop is reported imprecise. |
  | 59 a fresh machine after a failure is healthy | `contain`: a fresh process right after every failing run, its N fixture programs matching QEMU. |
  | 60 owned compiler processes always cleaned up | `contain`: `teardown: … children none` after every run, failing and fresh, also after a server killed at its request deadline. Container ownership (tokens, immutable ids) retires with Docker: fexc runs natively. |
  | Containment campaign (25 cells + 25 fresh controls; detection ≤ 3 s, joined stop ≤ 10 s; N3 untested) | `contain` covers N = 1..4 for every tamper; measured below. The gate-relative outer watchdog (`experiments/track01/run_check.py`) is replaced by the per-vCPU watchdog (time in the VM), the compile-request deadline (time in the compiler) and the runner's bound. |
  | Seeded progress families (stack, controls, coherence, atomic × N1–4 × seeds; partial) | Stack, controls and coherence: S3a2, S3c2, S3d/S3f checks; atomic progress: `litmus` XCHG chains and S3r `xchg-lock`; partial (REP, 44): boot-phase handoff. |
- **S3i, retirement and handoff.** Delete the `families` feature (runtime/abi `families/`), drivers `i4v_*`,
  `concurrent*`, `recovery` (`i1`, `i2_i3`, `i4v` and `i4v_stack` went at S3a2), abi `translation`/`recovery`/`pressure`/
  `concurrency` and lib.rs bodies, runtime `coordinator`, `hvc #0x4a`/`OwnerService`, old runners/checkers/workers
  (including the I1–I3/address/stack checkers S3a2 kept as other families' oracle helpers); inventory gains a
  "general-path check" column; boot-phase handoff written (44, 51, 49, 64-bit FS/GS bases, the CPUID/SERIALIZE/IRET drain
  rule). *Check:* suites green; no campaign-named module; every contract maps to a check, a retirement reason or a
  boot-phase item; the resident stack audit (62) redone on the phase's final binary.
  *Built S3i1 (Oct 28, the deletion; S3i2 is the inventory column, the handoff and the stack audit):* S3h review
  carry-overs first: `contain` checks each tampered vCPU's banked state (FEX frame intact; the `system`, `site` and
  `site-kind` tampers at the unchanged start state, rip 0x100000 and every GPR 0; the others at a block boundary the run
  reached: a translated block's entry, reported by the driver as `block_rips`, or the section's end, where a forged
  header halts the linker at the exit of the block before it), so 56 and 58 are enforced; every `contain` process is
  killed 5 s past its bound (a hang is a diff); the driver keeps the vCPUs' supervision when a kicker fails (the
  teardown line comes from it alone; the kicker's error is returned after it) and reads staging only for a block reply.
  Deleted: the `families` feature with runtime/abi `families/` (17,955 lines), abi `translation`/`recovery`/`pressure`/
  `concurrency` (+ the pressure binaries) and lib.rs's I1 object and record fields, runtime `coordinator`, the
  `service` gate (`hvc #0x4a`), host drivers `concurrent*`/`i4v_*`/`recovery` (6,118), host `translation` and
  `process_owner` (798; the families' worker transport), `diagnostics`, the owner service (`OwnerService`, `Owners`),
  `Vm`, `enter_resident`/`run_to_hvc`, `new_vm_nonce`; the old runners, checkers, workers, producers (`i4v_producer`,
  `i3_producer`), `i4v_qemu`, their tests and testdata, the outer-supervisor tests (`test_containment_supervisor.py`,
  `test_supervisor_ownership.py`: the outer supervisor and its container ownership retired, S3h table, 57 and 60) and `fixtures/i1`, `fixtures/i4v`, `fixtures/i4v-qemu`. The
  32-bit families go under decision 3, and with them the legacy two-level walker `paging32.rs` (no general-path caller;
  recover it from commit 4902ee5 if the boot phase needs legacy paging). Moved:
  the four models to `tools/models/` (`ordering_model`, `coherence_model`, `pae_model`, `modes_model`, a package; their
  tests with them); the QEMU pins to `tools/reference/ref.py` (`IMAGE`, `PINS`); the old producer's pinned bodies, which
  `tools/fexc/test_fexc.py` compares fexc against, to `tools/fexc/producer_bodies.json`. Contracts §3's model fixes
  (items 3, 15 in `modes_model`; 4, 12 as declared policy) are left to S3i2: the moved models are unchanged.
  Checks: cargo tests 70 pass (1 ignored; 183 `#[test]` before), no build warnings (one before: `spawn_held`); Python
  tests 51 (318 before); fixtures 64 and seed 4242 44 match at block sizes default and 1; faults 3,443 at both; control
  flow 64 × 3 sizes, loop faults 846 × 3; probes 85; ASM 235 pass; `run_slice3_vm.py` corpora (3,040 programs at N = 2,
  3, 4), smp 15, limits, kicks (1,000 per vCPU), system, coherence 40, sharedpt 32 (1 run each), litmus 90 cells × 5,000
  trials (none outside the model), contain (100 tampers with the banked-state checks, 113 fresh controls, progress 24):
  all pass.
  *Built S3i2 (Oct 28, the last chunk):* dead code and wording left by S3i1: every vCPU's shadows are tagged with its ASID
  (the ASID-0 untagged mode, its `tlbi vmalle1` branch and `Space::tagged` are gone), abi items with no non-test caller
  deleted (`GuestRam::load32`/`compare_exchange32`/`read_bytes`, `paging::canonical_span`, `Space::clear`, the I1 layout
  constants), `owners::supervise` without its no-op deadline/poll/failure parameters, the unreferenced asm gates
  `enter`/`returned`; contracts §3 model fixes (`tools/models/`): item 15, `modes_model` WRMSR raises #GP for a nonzero
  EDX and declares its Intel EFER profile (the `system` runner now cross-checks `sysfault-efer-reserved-edx` with
  EDX = 1 on profile 22, not the EAX case); items 4 and 12 declared as policy and CPU profile in `coherence_model`
  (fault-path leaf A as a switchable `LEAF_A_ON_FAULT`, `MAXPHYADDR` 40, no 1 GiB pages, PWT/PCD/PAT non-write-back),
  unit-tested; item 3 (`modes_model` profiles 34/35 require a fresh walk after a PTE write without INVLPG) retired with
  32-bit (owner decision 3): the profiles start in 32-bit code and the 64-bit property is checked through
  `coherence_model.check_observation` (`system`, `sharedpt`); `pae_model`'s profile items likewise. The resident stack
  audit (62) and the remainder of the gate audit (61) as a runnable check, `tools/check_resident_stack.py` (+ test):
  11,568 bytes at the deepest gate path (… `aborted` → `access_aborted` → walker A/D write into a code page → `step` →
  `request` → … → `halt`), 16,000 with one nested entry to its halt (that path declared from the source, not derived);
  the 16 KiB exception stack left 384 bytes, so it grew to 32 KiB (`host/src/execution.rs`; 16,768 bytes margin); boot
  6,976 of 16,384; division helpers 48 + 16 of 64; `dispatch_fast` touches no stack (budget 0); no recursion; the only
  indirect branches are the boot jump and `dispatch_fast`'s entry into a cached block. Below the exception stack 32 KiB
  stay unmapped. An overflow is contained to the vCPU but not harmless: the fault on SP_EL1 re-faults on every frame
  push down through the guard until a push lands in the vCPU's own boot/block stack (`STACK_TOP` region), which it
  overwrites, and the vCPU then halts `unknown_abort`.
  Contract inventory: "General-path check per contract" (all 64 mapped, none unmapped). Handoff: [boot-handoff.md](boot-handoff.md).

### Phase end (S3i2, October 28)

| Criterion ([vm-restructure.md](../vm-restructure.md) phase boundary, step 2 end state) | Evidence on the final tree |
| --- | --- |
| Slice 2 works: VM-resident code cache, in-VM dispatch and block linking, control flow, first speed numbers against QEMU TCG | [Slice-2 contract](slice2-contract.md) built (S2a–S2g); flow 64 × 3 block sizes, probes 85, loop faults 846 × 3, ASM 235; S2g numbers in [boot-handoff.md](boot-handoff.md) §4 (int 0.97×, mem64 0.29× of QEMU time). |
| Up to four vCPUs on the general execution path | corpora N = 2, 3, 4 (3,040 + 3,042 + 3,040 program runs), smp 15, kicks, contain at N = 1..4. N = 0 cannot be described; `execution::prepare` refuses it (unit test `one_to_four_vcpus`); 5 is refused (limits). |
| The general path covers the old specialized paths' behaviour (paging modes, mode/control transitions, ordering, coherence); those paths retired | Every contract has a general-path check, retirement reason or boot-phase item ([contracts.md](contracts.md), 64 of 64); old paths deleted at S3a2 and S3i1; 32-bit and paging-off modes out of the phase (owner, October 28; decision 3). |
| Handoff to the boot phase documented | [boot-handoff.md](boot-handoff.md). |
| Step 2: crates organised by responsibility, no module named after a test campaign; test programs, expectations and mutations in drivers outside the emulator crates | Met: crates by responsibility (host `vm`, `memory`, `owners`, `execution`, `admission`, `system`, `json`; abi `execution`, `code_object`, `guest_memory`, `paging`, `shadow`, `resident`; runtime `execution`, `cache`, `tables`, `record`, `gates.S`); no file in `crates/` or `tools/` carries a campaign name; programs, QEMU expectations and models live in `tools/` and `fixtures/`. **Deviation for the owner:** the test driver is still inside the host crate: `run-program` (`host/src/drivers/`) and its tamper hooks, some of which act in the service loop (`forge_stop`, `ignore_stops`, the reply tampers in `host/src/execution.rs`). Minimal move, proposed for when the boot phase adds a loader entry point (not done now): split the host into a library and a driver crate, and turn `forge_stop` into a generic stop-ELR hook the driver supplies. |
| Suites green on the final tree | `cargo build` 0 warnings; `cargo test` 68 pass (1 ignored); Python tool tests 62; fixtures 64 and seed 4242 44 match at block sizes default and 1; faults 3,443 at both; flow 64 × 3; loop faults 846 × 3; probes 85; ASM 235 pass; `run_slice3_vm.py` corpora 9,122, smp 15, limits, kicks (1,000 per vCPU at N = 1, 2, 4), system 69, coherence 40, sharedpt 32 (1 run each), litmus 90 cells × 5,000 trials (none outside the model), contain (100 tampers, 4 unacked, 3 compiler deaths, 3 stalls, 4 spins, 2 legitimate spins, 24 progress, 113 fresh controls); `check_resident_stack.py` ok. After the S3i2 review fixes (32 KiB exception stack, `dispatch_fast` stack check, a doc-comment edit to `shadow.rs` that left the runtime binary unchanged) the whole suite was re-run from the command block in [boot-handoff.md](boot-handoff.md) §1 under zsh: the same counts, all pass. Nothing staged (`git diff --cached` empty). |

## 5. Owner decisions (recorded October 27, 2026)
1. **32-bit and paging-off segmentation forms go to the boot phase** (segment bases and limits, CS limit, A20, 16-bit
   addressing, device/hole resolver): unused by the flat `startup_32` and long-mode kernel, FEX support unverified. The
   general path refuses non-flat segments, explicit segment prefixes, A20 off and 0x67 in 32-bit code
   (`unsupported_mode`/`unsupported_form`); old code goes at S3i; they return only if the boot path needs them. Not
   covered: 64-bit FS/GS bases (Linux per-CPU data via `%gs`), a boot-phase prerequisite.
2. **Tearing across 16 bytes.** A non-LOCKed 2/4/8-byte access inside one 64-byte line that spans a 16-byte boundary may
   tear for other vCPUs: a documented deviation (SDM "Guaranteed Atomic Operations": atomic on P6 and later). Within one
   16-byte block: atomic, pending S3t; line- or page-crossing: not guaranteed by x86 either; LOCKed and crossing 16 bytes:
   stops. Only racing multi-vCPU code is affected; tearing is silent (a boot-phase diagnostic may count such stores).
   Replaces a resident atomic service (a load-side check on every unaligned access).
3. **All 32-bit support goes to the boot phase (owner, October 28).** The S3g survey ([s3g-plan.md](s3g-plan.md)) found
   that, on the planned 64-bit boot protocol with ACPI-mailbox AP start-up and no LA57, Debian 6.12 runs no 32-bit or
   paging-off instruction and no x86-64 Linux path uses legacy two-level, 4 MiB or legacy-PAE paging; only Ubuntu 6.8's
   AP trampoline briefly goes 64 → compatibility → paging off → PAE/LME/PG → 64. S3g is dropped from this phase: the
   32-bit profile, paging-off resolver, mode transitions, descriptor loads and legacy/PAE walking are built in the boot
   phase only if its chosen kernel needs them (the plan's S3g1–S3g3 are the starting point). The 32-bit families
   (paging-off, paging-off steps, address forms, mixed modes, legacy/PAE controls and coherence variants) retire at S3i
   with this reason; their models stay in `tools/models/` for the boot phase.
