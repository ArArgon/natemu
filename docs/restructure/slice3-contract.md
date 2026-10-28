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
harmless, but the migration stress cannot detect a regression in them. `State` gains the vCPU id (replacing cache.rs's `VCPU = 0`); each vCPU reads back `ACTLR_EL1.EnTSO` at start.
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
the page), stop waiting and redo the miss. Serving SHADOWS: reset own shadows (`tlbi` own ASID, `dsb ish`), clear own
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
(FSC 0x21) at the swap site → stop `unsupported_atomic` (admission refuses it when the address is known). Split locks,
LOCK ADD/CMPXCHG/XADD/BT* and CMPXCHG16B wait for the boot phase.

## 3. Covering the old specialized paths
"Delete": the family code, host driver, runner, checker and worker go; the independent models (`i4v_ordering_model`,
`i4v_coherence_model`, `i4v_pae_model`, `i4v_modes_model`) move to `tools/models/` with contracts §3's fixes.

| Old campaign | What the general path needs | Recommendation |
| --- | --- | --- |
| I1 entry, I2 live translation, I3 paging/allocated, address, stack | Nothing new: slices 1–2 cover them (fault sets, guard, split, precision verifier, admission). Port I3 cells the slice-1 fault matrix lacks (CR0.WP=0, 2 MiB leaves, reserved bits, SMEP, carry/overflow) as generated cases. REP and FP cells wait for that ISA. Retained-route completion (42) and the resident stack bound (62): a general-path check or an explicit retirement reason. | Deleted at S3a2 (below). |
| Six-program QEMU check, gate-binary audit | `run_i4v_qemu.py` binds native reports of six programs. Two of them (`live-low-low_operand`, `stack-low-low_stack`) were cells S3a2 deleted, so the check now reproduces only from bundles retained before S3a2; the general-path corpora against QEMU (fixtures, seed corpora, faults) cover those programs' behaviour. The four ordering programs (xchg, lfence, sfence, mfence) live until the ordering family goes. `tools/reference/ref.py` takes its QEMU pins from `run_i4v_qemu.py`. | Delete at S3i (move the pins). |
| I4 entry/faults/remap/compose/pressure; seeded progress, schedule, partial progress | N vCPUs (S3a), coherence (S3d, S3f). Contracts 45–48 and 53 describe the old global service lock and FIFO; the new design has no global lock, so they retire; progress is checked by S3h. REP partial progress (44): boot-phase handoff. | Delete at S3h. |
| I4 events (49) | Interrupt delivery: boot phase (handoff item). | Delete; rebuilt with delivery. |
| Aliases, concurrent stack, stack boundary | Per-vCPU INVLPG (S3c), shared tables (S3f); #SS and split pushes are slice 1. | Re-express in S3f, delete. |
| Coherence (four-level parent, A/D, size) | S3c + S3f. Legacy and PAE variants need S3g. | Re-express in S3f/S3g, delete. |
| Ordering, fence, atomic progress | Fences and memory XCHG (§2), litmus programs (S3e). LFENCE register capture (51) needs SSE: handoff item. | Re-express in S3e, delete. |
| Containment | Tamper hooks per vCPU, cancellation, fresh-process control (S3h). | Re-express in S3h, delete. |
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
| I1 fault (unknown native exception traps, no guessed guest fault: 56) | Resident `aborted`: an abort at an unknown PC or with a syndrome not its site's kind halts (`unknown_abort`/`site_mismatch`) before any effect. No run-time test forces one yet (gap: an S3h tamper). |
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
| 62 resident stack bound | No check yet: the audit is per binary (inventory §4) and S3b–S3h still add resident paths; redone on the phase's final binary at S3i. |

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
  = vCPU number + 1 (TLBI by ASID; ASID 0 stays the families' untagged world with global shadows; a precaution, see §1). `State::vcpu`; global area: code and writable masks, `Record {pending, stale}` per vCPU,
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
- **S3d, cross-vCPU code coherence.** §1's marking, writable mask, shootdown, self-reset, resident fetch walk, page posts,
  post-write check, post-place drain and step. *Check:* a unit test that an A/D refusal with a peer's code bit ends;
  against S3r: a peer writes fresh code then a flag and the other vCPU jumps to it (new page, reused data page), patch a
  function and run it after `mov cr3, cr3`, all vCPUs patching and running in rounds, data on a peer's code page; a stress
  posting the page of a vCPU's running block (never `bad_link`; steps, stale links counted) at block sizes default/1; a
  page rewritten on every fetch (the runner finishes); two vCPUs translating pages the other writes (no deadlock); a vCPU
  finishing while a peer misses on a page it held writable.
- **S3e, ordering on the general path.** Fences, memory XCHG (`swap` sites, alignment stop), generated litmus programs
  (sb, mp, lb, wrc, iriw, 2+2W, sb-mfence, sb-xchg, XCHG chains) at N = 2..4, with unaligned and split-store variants.
  *Check:* outcomes within `i4v_ordering_model.py`'s allowed sets in ≥10^5 trials per cell; chains linearizable; XCHG
  faults before commit leave memory unchanged (single-vCPU cases against the QEMU prefix and signal references).
- **S3f, shared-table coherence.** Tests only: A/D races on one PTE from N vCPUs while one clears A/D with plain stores
  and its own INVLPG; remap with per-vCPU INVLPG; 2 MiB ↔ 4 KiB changes; aliases across vCPUs; contended walks never halt.
  *Check:* each vCPU's observations and final PTE bytes against `i4v_coherence_model.py`.
- **S3g, 32-bit flat protected mode and transitions.** As §3, disassembly first. *Check:* S3r programs running a
  `startup_32`-like path (paging off → PAE tables → LME → PG → far jump to 64-bit → compatibility mode → paging off),
  legacy two-level with 4 MiB pages; fault matrices against `i4v_pae_model.py` and `i4v_modes_model.py` (contracts 28–31);
  mixed modes on N vCPUs sharing a code page; explicit segment prefixes refused.
- **S3h, containment and progress.** *Check:* tamper on each vCPU at N = 1..4 → all stopped, joined, VM destroyed, fresh
  process healthy; a peer that never acks a shootdown, compile-server death and a malformed reply on vCPUs 1–3; all vCPUs
  finish under sustained cross-vCPU code writes; one vCPU finishes while a peer misses on its page; watchdog contains a
  stuck wait.
- **S3i, retirement and handoff.** Delete the `families` feature (runtime/abi `families/`), drivers `i4v_*`,
  `concurrent*`, `recovery` (`i1`, `i2_i3`, `i4v` and `i4v_stack` went at S3a2), abi `translation`/`recovery`/`pressure`/
  `concurrency` and lib.rs bodies, runtime `coordinator`, `hvc #0x4a`/`OwnerService`, old runners/checkers/workers
  (including the I1–I3/address/stack checkers S3a2 kept as other families' oracle helpers); inventory gains a
  "general-path check" column; boot-phase handoff written (44, 51, 49, 64-bit FS/GS bases, the CPUID/SERIALIZE/IRET drain
  rule). *Check:* suites green; no campaign-named module; every contract maps to a check, a retirement reason or a
  boot-phase item; the resident stack audit (62) redone on the phase's final binary.

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
