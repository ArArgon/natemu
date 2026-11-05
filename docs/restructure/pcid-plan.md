# PCID phase plan: PCID and INVPCID on Arm ASIDs, accurate "not vulnerable" bits, a cheap exact VERW, PTI-on benchmarks

Status: plan, November 5, 2026, at 28eca9a (branch `restructure`), revised after an independent review the same day and
after the owner's MDS decision (below). Nothing here is built or measured yet. Inputs: [perf-phase2-plan.md](perf-phase2-plan.md)
(item 6, "Phase result", the backlog, "Applicability", "Benchmark conventions"); [perf-plan.md](perf-plan.md) "Measured
(chunk 8)" (local links), "Measured (chunk 14)" (global kernel shadows kept across CR3 loads, supervisor upper half only),
"Measured (chunk 19)" (L1 fill lists visited at CR3 loads), "Measured (chunk 24)" (the PCID gate; its arithmetic in
`natemu-raw/breakdown-20261105-01/estimates.txt`); the mitigation study `natemu-raw/mitig-20261103-01/`; the owner decisions in
[INV-03-01](../research/investigations/INV-03-01.md) (Performance rows of November 3). The phase's chunks are named P1-P8 so
they are not confused with the performance phase's numbered chunks. Estimates are labelled and show their arithmetic.

**Owner decision (November 5): MDS_NO stays 0, and VERW is made cheap.** Reasons: the M4 has a load value predictor and a load
address predictor (the FLOP and SLAP attacks, reported by The Register, January 29, 2025); every guest context (all vCPUs,
both privilege classes, every process) runs in one host process on the same translated code; how those predictors are tagged
by ASID or exception level is not published; and VERW, emulated, cannot clear them. So natemu does not claim "not affected by
MDS"; Linux keeps executing VERW on every return to user mode, and P2 makes that VERW an exact inline translation so it costs
little.

## 1. Scope and exit criteria

**In scope.**
1. IA32_ARCH_CAPABILITIES (MSR 10AH, enumerated by CPUID.(EAX=7,ECX=0):EDX[29]) with only the bits that are true of
   natemu's emulated CPU on its host (2.4), RDCL_NO only after the resident's speculation audit (P1).
2. Only if P4's gate passes (section 5, the PCID stop rule): PCID and INVPCID in the presented CPU, every rule exact per the
   SDM (2.1-2.3), and items 3, 4 and 6.
3. Per-PCID copies on Arm ASIDs: data shadows, block currency, L1 dispatcher tables and links per PCID, kernel global
   shadows shared across copies (3).
4. A check mode for the copies (3.7).
5. VERW translated inline, exactly (P2, owner decision).
6. Fast paths for the CR3 accesses PTI makes per kernel entry, if the counts call for them (P7).
7. PTI-on cells in the standing benchmarks (4).

**Not in scope.** SMAP (its own profile item; interaction noted in 2.3), LA57, 1 GiB pages, more than 4 vCPUs (owner-deferred),
shadow paging for guests without PCID (rejected in 3.1), the performance-phase backlog, the correctness pass.

**Done means (measured with the batch-end rules: three interleaved rounds, one VM at a time, every run verified).** If P4's
gate (or P5b's check) stops PCID, the phase is done when P1-P3 meet criteria 3, 4, 6 and 7 and the PCID record states the
gate's numbers; criteria 1, 2 and 5 apply only when PCID is built.
1. The PTI-on cells (4.1) meet the bar the owner sets against QEMU's PTI-on cells once P3 has measured both (owner decision 2).
2. PTI-on installer N = 1: a target derived before copies are built, from P3's `pti=on` baseline (no PCID) and P4's re-count
   with PCID on (what copies would keep, chunk 24's method): target = baseline - 0.85 x counted saving (the 15% allows for the
   copies' own costs, which chunk 24 did not estimate). Chunk 24's 14.40 s and its 5.85 s bound cannot serve: they were
   measured with Linux's default `pti=auto` and no PCID, where Linux keeps the kernel image global; with `pti=on` and PCID it
   makes the kernel image non-global, so both the cost and what copies keep change.
3. Every mitigations-off cell of the batch-end matrix (11 benchmarks and the installer, N = 1 and 4) unchanged or faster
   within its run-to-run range against the phase result (82462c2), rerun the same day. Chunk 24 bounds the possible gain
   there at 0.19 s (`luajit`, 3.5%); a slowdown beyond the range blocks the phase.
4. A default command line boots with Linux printing PTI disabled; MDS is reported as vulnerable with buffer clearing attempted
   (MDS_NO and MD_CLEAR stay 0) and TAA, MMIO stale data, GDS, RFDS and ITS as not affected, and the VERW it runs is
   inline (P2's counter).
5. Check mode (3.7): installer boots (mitigations off and `pti=on`, N = 1 and 4) and the `pti=on` `hackbench` and `mem` cells
   at N = 4 run clean; every new test green and every negative control red.
6. The existing suites green (`cargo test`, `run_k1.py`, `run_k2.py`, `run_x3.py`, `run_x4.py`, `run_slice3_vm.py`,
   `run_boot_smoke.py`, `run_r1.py`, the stack audit, `--check-reuse --code-shape` boots).
7. An independent review of each chunk's diff and the read-only workload-specific-code audit at the batch end.

## 2. What the SDM requires

Vol.3A citations are to the local snapshot `artifacts/research/track01/source-snapshots/2026-09-03/intel-sdm-vol3a.txt`
(edition 253668-092US), where paging is chapter 5. Vol.2 (INVPCID, MOV CR, VERR/VERW, CPUID) and Vol.4 (Table 2-2) are not in
the snapshot; they are cited from the current SDM, and P1 adds those pages to the snapshot before anything is coded from
them. Where the SDM leaves an order or a behaviour open, the reference AMD machine decides (owner rule) if P3 finds it can run a
ring-0 probe. That machine is itself a VM (a KVM guest), so a probe there shows the hardware under its hypervisor.

Code comments to correct in a later code chunk (they cite chapter-4 numbers of an older edition; paging is chapter 5 in 092):
`crates/abi/src/shadow.rs:17-19`, `crates/abi/src/execution.rs:321, 492`, `crates/abi/src/paging.rs:292`,
`crates/runtime/src/cache.rs:1124`, `crates/runtime/src/execution.rs:935, 1028, 1052, 1427, 1431`.

### 2.1 Enabling and CR3

- **CPUID** (Vol.2A CPUID): PCID is CPUID.1:ECX[17]; INVPCID is CPUID.(7,0):EBX[10]. Today neither is advertised
  (CPUID.1:ECX = 0xf6f83203; leaf 7 EBX = 0x218801a9; `cpu_profile.rs`), and CR4_VALID (0x150fbc) lacks bit 17. Both come
  from `tools/models/cpu_profile.json` through the generator.
- **CR4.PCIDE (bit 17)** (5.10.1): MOV to CR4 is #GP if it would change PCIDE from 0 to 1 while IA32_EFER.LMA = 0 or
  CR3[11:0] is not 000H. MOV to CR0 is #GP if it would clear CR0.PG while CR4.PCIDE = 1 (natemu already refuses every PG
  clear). With PCIDE = 0 the current PCID is 000H. After a PCIDE change the processor uses CR3 as specified for the new value at
  once (5.5.2).
- **CR3** (5.5.2, Tables 5-12 and 5-13): with PCIDE = 1, bits 11:0 are the PCID, M-1:12 the root, 60:M reserved; bits 61 and
  62 are LAM (not advertised: reserved); bit 63 is reserved in CR3. With PCIDE = 0, bits 3 and 4 are PWT and PCD and bit 63 is
  reserved, so a MOV with bit 63 set is #GP (SDM-defined; today's check already does this). The PML4 read uses PAT entry 0 with
  PCIDE = 1, else 2 x PCD + PWT from CR3 (5.9.2).
- **Bit 63 of the MOV source** with PCIDE = 1 selects no-flush (5.10.4.1) and is not written to CR3 (Vol.2B MOV CR): reads
  return it as 0.
- **PCID 000H** is an ordinary PCID with PCIDE = 1; with PCIDE = 0 everything is PCID 000H (5.10.1), so setting PCIDE needs no
  flush.

### 2.2 What each operation invalidates (5.10.4.1)

The SDM gives minimum invalidations. A processor may invalidate more at any time ("Processors need not implement any TLBs ...
may invalidate any TLB entry at any time", 5.10.2.2; "The processor is always free to invalidate additional entries", the end
of 5.10.4.1). So natemu is exact if it never uses an entry the minimum says is gone; dropping more costs only speed.

| Operation | Must invalidate | Need not touch |
| --- | --- | --- |
| MOV to CR0 changing PG from 1 to 0 | every entry of every PCID, global ones too | (natemu: #GP, 64-bit only) |
| MOV to CR3, PCIDE = 0 | non-global entries of PCID 000H | global entries |
| MOV to CR3, PCIDE = 1, bit 63 = 0 | non-global entries of the PCID in source bits 11:0 | other PCIDs; global entries |
| MOV to CR3, PCIDE = 1, bit 63 = 1 | nothing | everything; entries of that PCID made under an older root stay usable (entries belong to the PCID, 5.10.1) |
| MOV to CR4 changing PGE, or PCIDE 1 → 0 | every entry of every PCID, global ones too | |
| MOV to CR4 changing PAE, or SMEP 0 → 1 | every entry of the current PCID | |
| INVLPG m | entries for the page with the current PCID, and global entries for the page whatever their PCID; paging-structure caches of the current PCID | other PCIDs' non-global entries |
| #PF | entries for the faulting page with the current PCID | |
| INVPCID type 0 (individual address) | non-global entries for (PCID, address) | global entries |
| INVPCID type 1 (single context) | non-global entries of the PCID | global entries |
| INVPCID type 2 (all contexts, globals included) | every entry of every PCID | |
| INVPCID type 3 (all contexts) | non-global entries of every PCID | global entries |

- **Global pages and PCIDs** (5.10.2.4): with CR4.PGE, an entry from a leaf with G = 1 may be used under any PCID. So global
  shadows may serve every copy, and INVLPG must reach a global shadow wherever it lives.
- **TLB contents** (5.10.2.2): an entry holds the frame, the combined R/W and U/S, XD (with NXE), the dirty flag and the memory
  type. natemu's shadows also bake in CR0.WP, EFER.NXE and CR4.SMEP-dependent rights from the walk, which is why every change
  of those resets every copy (3.6).
- **Several CPUs** (5.10.5): each CPU invalidates its own TLB (the guest uses IPIs). Copies are per vCPU, so no guest operation
  needs a cross-vCPU invalidation; natemu's own code-write protocol does (3.6).

### 2.3 INVPCID (Vol.2A INVPCID; 5.10.4.1)

`66 0F 38 82 /r`: INVPCID r64, m128 in 64-bit mode; INVPCID r32, m128 in protected and compatibility mode (the register is
32 bits there). The descriptor: bits 11:0 PCID, 63:12 reserved, 127:64 linear address. Exceptions listed in 64-bit mode:
- #UD: CPUID.(7,0):EBX.INVPCID = 0; a LOCK prefix. A register (mod = 11) second operand is not in the SDM's list; P3 probes it on
  the AMD machine (natemu's decoder treats it as #UD until then, as for the other m-only forms).
- #GP(0): CPL > 0; a non-canonical memory operand (CS, DS, ES, FS, GS); type > 3 (the whole register); descriptor bits 63:12 not
  all 0; type 0 or 1 with a non-zero PCID while CR4.PCIDE = 0; type 0 with a non-canonical descriptor address.
- #SS(0): a non-canonical SS-based memory operand. #PF: the descriptor read faults.
- Protected and compatibility mode: the same checks with the segment-limit forms of #GP/#SS. natemu's resident runs 64-bit mode;
  a CPL-0 compatibility-mode INVPCID stops it (`unsupported_system`) as for other unimplemented forms; CPL 3 is #GP.
- **Order, open**: the SDM lists the exceptions without an order, and its pseudocode reads the descriptor before it tests the
  type. natemu follows the pseudocode until the AMD machine decides: #UD, CPL, the memory operand (#GP/#SS, then #PF of the
  read), then the type, then the descriptor's checks. If no ring-0 probe is possible this stays recorded as open.

**SMEP and SMAP.** SMEP is advertised and enforced by the fetch walker; with PCID only the CR4 rule above applies, and natemu
already resets everything on any SMEP change (more than the minimum). SMAP is not advertised; if it later needs a third shadow
space per privilege class, each copy needs a third ASID (3.3's budget).

### 2.4 IA32_ARCH_CAPABILITIES bits (Vol.4 Table 2-2, MSR 10AH)

Today MSR 10AH is #GP and CPUID.(7,0):EDX is 0, so Linux assumes the bugs its tables give a Haswell (family 6, model 3CH,
natemu's presented CPU) in a VM. Bit positions are those of Vol.4 Table 2-2 as Linux names them (`msr-index.h`); P1 checks
each against the SDM pages it adds to the snapshot. The owner's policy is accurate bits: every bit true of natemu as it runs is set, every other one is 0; P1
re-confirms each against its source (Intel's "CPUID Enumeration and Architectural MSRs" document and the per-flaw guidance)
and records the reason per bit; host
properties are checked, not assumed. Host facts (this Mac, `sysctl hw.optional.arm`, November 5): Apple M4; FEAT_CSV3 = 1,
FEAT_CSV2 = 1, FEAT_SB = 1, FEAT_SSBS = 0.

| Bit | Name | Value | Why |
| --- | --- | --- | --- |
| 0 | RDCL_NO (Meltdown) | 1, only with host FEAT_CSV3 and after P1's audit | Translated CPL-3 code runs with the user shadow space, which holds only translations the walker granted at CPL 3: a supervisor page has no Arm mapping there. The same Arm tables map the resident's private region (with the guest-RAM alias) EL1-only; FEAT_CSV3 keeps faulting-load data from feeding speculative address use. The resident itself, running a CPL-3 access on the guest's behalf at EL1 through the alias (`segments::linear` → `pages` → `copy_out`, string and MMIO completion, emulated system instructions), could load before its permission result is resolved; P1 audits those paths and puts SB (host FEAT_SB) or masking after the permission result. The bit is filled in at startup from CSV3 (0 without it). |
| (L1TF) | none; Linux skips L1TF when RDCL_NO is set | follows bit 0 | A not-present or reserved guest PTE produces no shadow. |
| 5 | MDS_NO | **0 (owner decision)** | See the decision at the top. MD_CLEAR (CPUID.(7,0):EDX[10]) also stays 0: VERW clears nothing on the host. |
| 8 | TAA_NO | 1 | TAA needs TSX; RTM/HLE are not advertised and TSX_CTRL is 0. True architecturally. |
| 6 | IF_PSCHANGE_MC_NO | 1 | No machine check on a page-size change (MCE/MCA not advertised, no VMX). True architecturally. |
| 2, 19 | RSBA, RRSBA | P1 measures; probably RSBA = 1 | 0 would assert that RET is predicted only from a return stack. natemu translates RET as a computed exit through the dispatcher, i.e. a host indirect branch predicted by the host's indirect predictor; if P1 confirms that, RSBA = 1 is the accurate value (and RRSBA follows its definition). |
| 13, 14, 15 | SBDR_SSDP_NO, FBSDP_NO, PSDP_NO (MMIO stale data) | 1 | MMIO is an exit to natemu's device models and mapped RAM is ordinary memory: there is no uncore buffer through which MMIO could read or propagate stale data. The general host value-predictor question is MDS_NO's (kept 0). |
| 26, 27 | GDS_NO, RFDS_NO | 1 | natemu has no gather unit (AVX2 gathers are emulated as ordinary loads) and no Atom register file; the general host value-predictor question is MDS_NO's (kept 0). |
| 62 | ITS_NO | 1 | Intel designates this bit for hypervisors to tell a guest it is not affected by ITS, a flaw of Intel's branch-target selection; Spectre v2 stays reported (no predictor claim), so Linux keeps its Spectre v2 mitigation and drops only the ITS thunks. |
| 4 | SSB_NO | 0 | Speculative store bypass is general out-of-order behaviour; the host has no SSBS control (FEAT_SSBS = 0). |
| 20, 24 | BHI_NO, PBRSB_NO | 0 | Host predictor behaviour, not claimed. |
| 1, 3, 7, 17, 18, 25, 28 | IBRS_ALL, SKIP_L1DFL_VMENTRY, TSX_CTRL, FB_CLEAR, FB_CLEAR_CTRL, GDS_CTRL, RFDS_CLEAR | 0 | Controls or VMX features natemu does not have. |

The MSR is read-only (writes #GP), with CPUID.(7,0):EDX[29] = 1.

**What Linux does with them (motivating only; Debian 6.12.107, from its boot log).** Without the MSR it enables PTI, VERW
buffer clearing ("MDS: Vulnerable: Clear CPU buffers attempted, no microcode"), reports MMIO stale data and SRBDS as unknown and
applies ITS thunks. With RDCL_NO it skips Meltdown and L1TF, so PTI is off unless `pti=on`; MDS clearing stays (MDS_NO = 0);
with the MMIO bits MMIO stale data reads not affected, and with ITS_NO it drops the ITS thunks.
With PCID it uses 6 dynamic address-space IDs per CPU; under PTI each address space has a kernel and a user PCID (12 per CPU),
its entry code reads and writes CR3 on every kernel entry and exit, and with INVPCID it invalidates user-PCID pages with
INVPCID type 0. With `pti=on` and PCID it makes the kernel image non-global but keeps the entry text global. P3 and P4 confirm
these in counts; nothing in the design depends on them.

## 3. Design: per-PCID copies on Arm ASIDs

### 3.1 Options

Chunk 24's PTI installer estimate (`estimates.txt`, that cell's own unit costs: shadow install 0.48 us, revalidation 0.05 us,
linker entry 0.39 us, dispatcher miss 0.24 us), with Linux's default `pti=auto` and no PCID (kernel image global):

| Option | What it keeps across a no-flush CR3 load | Exact? | Chunk 24 bound, PTI installer |
| --- | --- | --- | --- |
| A. PCID architectural only | nothing more than today | yes (drops more than the minimum) | none |
| B. Copies of data shadows | each PCID's data shadows | yes (3.7) | refaults 7,040,498 x 0.48 us = 3.38 s |
| C. B plus blocks, L1 and links per PCID (**recommended**) | also which blocks are current, their L1 entries and links | yes (3.4-3.5) | adds revalidations 2,081,269 x (0.05 + 0.39) us + 2,817,059 x (0.05 + 0.24) us = 0.92 + 0.82 s, and the K1 time of those loads 0.77 s: total 5.85 s (upper bound) |
| D. Keep shadows per CR3 root without PCID, write-protecting every guest page-table page | everything, for any guest | only with full page-table write tracking | rejected: a CR3 load without PCID must flush; hiding that needs a fault on every page-table write (classic shadow paging), not needed once the guest has PCID |

P4 re-counts this with `pti=on` and PCID on before B is built (the gate in P4).

### 3.2 Copies

- A **copy** is one PCID's pair of shadow spaces (supervisor and user, as today), each with its own roots and ASID, per vCPU.
  A vCPU has K copy slots; a small table maps PCIDs to slots. With PCIDE = 0 the guest uses PCID 0, slot 0.
- **Switch** (a no-flush load to a PCID that holds a slot): select that slot's roots and ASID; no TLBI, no table writes.
  `tables::select` must compare the loaded ASID (slot and space), not only the user bit as today.
- **Flushing load** (bit 63 = 0, or PCIDE = 0): reset that slot alone (3.6 says how its pages are freed).
- **Recycling**: a PCID without a slot takes the least recently used slot, reset first: a new PCID never sees another PCID's
  entries. Dropping a copy is always exact.
- **K**: Linux uses 12 PCIDs per CPU under PTI and 6 without; chunk 24 sent 260.1 k of 263.0 k PTI loads to one of the 6 most
  recent roots (roots: two per address space under PTI). Starting value 16, confirmed or changed from P4's counts.
- **Pool**: one pool per vCPU shared by its slots, with a free list (today's pool is bump-allocated from two ends and freed only
  whole); on capacity, the least recently used other slot is reset before a full reset. Sized from P3's and P4's counts of table
  pages per root. It no longer fits the vCPU's 2 MiB window (pool at 0xe0000-0x200000), so it moves into the vCPU's private
  window (P5a).
- **Memory** (estimate): roots 4 pages per slot, 16 x 16 KiB = 256 KiB per vCPU; the pool as counted (today 288 pages, 1.125
  MiB per vCPU; if each of 16 slots needed today's pool, 18 MiB per vCPU, 72 MiB at N = 4, an upper bound). Block records do
  not grow (3.4).

### 3.3 ASIDs

- Today: 8-bit ASIDs (TCR_EL1.AS = 0); vCPU v has 2v + 2 (supervisor) and 2v + 3 (user); ASIDs 0 and 1 are unused, so 254
  (2-255) are available. Shadow and private-region entries are nG, tagged with the space's ASID.
- With copies: vCPU v gets a block of 2K ASIDs, base 2 + 2Kv; slot j uses base + 2j and base + 2j + 1 (the user ASID stays the
  supervisor one with bit 0 set). Budget: 254 / 32 = 7 vCPUs at K = 16 (today's maximum is 4). With 16-bit host ASIDs
  (ID_AA64MMFR0_EL1.ASIDBits, read through HVF in P3), TCR_EL1.AS = 1 removes the limit; not needed at 4 vCPUs.
- Cost to measure (P5b): the resident's private region is nG, so each ASID fills its own Arm TLB entries for the resident's
  code, tables and state; more ASIDs in use means more Arm TLB misses after switches. Making them global is not possible (each
  vCPU's private window has the same addresses).
- Same assumption as today, now tested (P5b): a vCPU's local TLBI is enough because the hypervisor handles a vCPU thread moving
  between host cores.

### 3.4 Blocks and L1 tables per copy

Today a block is current when `checked == epoch`, or it is global and `checked >= global_epoch`; every CR3 load moves `epoch`,
visits the L1 fill lists (chunk 19) and prunes links into non-global blocks.

- **Currency.** The `checked` word becomes a 16-bit slot mask and a 48-bit stamp from one counter that moves at every flush;
  each slot has `flushed_at`. A non-global block is current under slot q when q is in its mask and its stamp is later than
  `flushed_at[q]`. A revalidation under q sets the mask to the slots still live plus q and the stamp to now (exact: a slot still
  live had no flush since the old stamp, so none before now; any later flush is after the new stamp). A flush of q is
  `flushed_at[q] = now`, with no per-block work; recycling a slot is a flush of it. Global blocks keep `GLOBAL` and
  `global_epoch` and are current under every slot (5.10.2.4). INVLPG, INVPCID type 0 and #PF cut blocks as `cache::invlpg` does
  today, under every slot (more than the minimum).
- **L1 tables.** Each table (one per code context) has an owning slot, and the dispatcher probes a table only while it is owned
  by the current slot. At a switch to another slot the current context's table is parked: its dispatcher slot is pointed at the
  miss (one slot write, about 90 ns per chunk 12) and the table is left intact. Switching back with no flush of the owner since:
  unpark (one slot write). A fill attempt under another slot re-owns the table with chunk 19's fill-list visit (emptying entries
  not current under it), the same cost as one CR3 load today; a flush of the owner empties through the list at unpark. Context
  changes pass through `select_l1`, so a parked or foreign-owned table is never probed. Under PTI the kernel entry stub runs a
  few instructions at CPL 0 under the user PCID and ends at the MOV to CR3 (a resident trip), so it never fills the parked
  kernel table: two slot writes per kernel entry and exit (estimate).
- **In-VM reuse** (chunk 10) and the shared host answer cache are keyed by frame, bytes and context: unchanged; a reused copy is
  current only under the slot that walked it.

### 3.5 Links per copy

- **Invariant**: for every patched link X → Y that is not local, every slot X is current under, Y is current under too (global
  = every slot). It needs no work at switches (nothing becomes current at a switch) or at flushes (a flush removes the slot from
  every non-global block at once). It is kept (a) at link time: if Y's live slots do not contain X's, X's extra slots are dropped;
  (b) at revalidation of X under q: X's non-local outbound links into blocks not current under q are undone; (c) when Y is cut:
  inbound links are undone (as today).
- **Global into non-global: demotion.** Under `pti=on` with PCID Linux keeps the entry text global and the rest non-global, so
  refusing such links would cost a resident trip per kernel entry (16.0 M x 0.18 us, `hackbench`'s trip cost in chunk 24 =
  2.9 s on `hackbench` N = 1, estimate). Instead the global block X is demoted when it links into non-global Y: `GLOBAL` cleared,
  mask set to the slots it was validated under that are live in Y, stamp now. Exact: dropping X's global status only makes it
  stop being current sooner (at those slots' next flush). A global block W linking into X must then be demoted too; the cascade
  follows the link records into X (the "into global targets" segment). P6 counts the cascade lengths first; if a cascade is long,
  the link is not made instead.
- **Local links** (chunk 8: every linear page of the target is a page of the source, from the same frames) are exempt from the
  invariant, from hooks (a) and (b) and from the check mode's link check. They stay exact per slot for chunk 8's reason: a source
  runs only under a translation of its pages that is valid now under the current slot (entered current through lookup, L1 or a
  non-local link into a current block, or by a local link from a block that was: induction), and its target fetches only those
  pages through the same frames in the same context. Nothing in that argument names an epoch or a slot.
- Result: no link prune at a no-flush switch.

### 3.6 Shared global shadows, resets, freed pages, INVLPG, other vCPUs

- **Shared half.** The kept half (chunk 14: supervisor space, upper canonical half, global translations only) becomes shared:
  its tables are linked from every slot's supervisor upper root, so a global kernel shadow made under one PCID serves all. Arm
  entries stay nG and ASID-tagged, so each slot refills its Arm TLB from the shared tables by hardware walk, with no resident trip.
- **Per top-level entry.** A non-global shadow in the upper half must not reach other slots: each 512 GiB top-level entry of the
  supervisor upper half is shared while every shadow under it is global, and becomes per slot when a non-global one is installed
  under it (its shared subtree is unlinked from every slot then, as chunk 14's "next CR3 load is a full reset"). A global shadow
  under a per-slot entry lives in that slot only (exact; a refault elsewhere). An entry returns to shared only at a full reset.
  With `pti=on` and PCID, Linux's top entry holds both the global entry text and the non-global kernel image, so it is per slot;
  with mitigations off everything there is global and shared, as today.
- **Full resets reset every slot and the shared half.** The shadows bake in rights that depend on CR0.WP, EFER.NXE and CR4.SMEP
  (2.2), so every path that resets today must reach all copies: a change of CR0.PG or WP, EFER.NXE/LME/LMA, CR4.PGE, PAE, SMEP,
  SMAP, PCIDE or LA57 (`CR4_PAGING`), an MTRR or PAT write (P5a lists which WRMSRs pass `changed` today), INVPCID type 2,
  the APIC base move (`lapic.rs`), pool capacity (`install`, `install_ones`), the uncached-page list overflowing (`execution.rs`
  M2 path), a peer's shadow-reset request (`take_invalidations`), the vCPU's own `mark`, and the whole-cache `FLUSH`.
- **Freed table pages.** A table page returns to the free list only after a TLBI that covers every ASID that could have walked it,
  walk caches included: for a slot's own tables, `TLBI ASIDE1` of both its ASIDs; for shared-half tables or a full reset, a local
  `TLBI VMALLE1` (it also drops other vCPUs' entries on this core: only a cost).
- **INVLPG** removes the 2 MiB range's shadows from every slot and the shared half (it must reach global entries whatever their
  PCID, and a global shadow may live in a per-slot entry), then one local `TLBI VAAE1` per page (all ASIDs) in place of a VAE1
  per space; #PF and INVPCID type 0 likewise for their pages.
- **Code-write protocol.** The vCPU's writable bits stay a superset of its writable shadows in all slots; a peer's request and
  the vCPU's own `mark` reset every slot. Kept copies leave more writable shadows alive, so more shootdowns than chunk 14's 5-8 per
  vCPU per run; P5b counts them.

### 3.7 Exactness argument and check mode

Four invariants; each chunk names the ones it touches:
1. **Data**: every shadow in slot q (or the shared half) is an entry an SDM-minimal TLB for PCID q could still hold: made by a
   walk under PCID q (or global), not invalidated since by any operation of 2.2, with no more rights than that walk gave under
   the controls (WP, NXE, SMEP, CPL class) in force.
2. **Code**: generated code under slot q enters only blocks current under q: through the resident (lookup checks currency),
   through L1 (only a table owned by q, each entry current under q) or through links (3.5's invariant, local links exempt).
3. **Isolation**: a slot never holds another PCID's or another vCPU's entries: recycling resets tables and invalidates both ASIDs
   before reuse; each vCPU has its own ASID block; a table page is reused only after 3.6's TLBI.
4. **Own pages**: the guest never gets a shadow onto natemu's own pages and no descriptor links outside the shadow tables:
   `Space::install`'s private-target check and `Space::next`'s table check must cover all K slots' roots, the shared half and the
   relocated pool (today they cover one set of roots and the 288-page pool), and a slot's tables may link only its own tables or
   shared-half tables.

**Check mode** (a resident build feature, like `perf`, and a host flag; off in measured runs):
- An independent reference TLB in the resident: at every shadow install and every block validation, an independent walker (not
  natemu's walk result) walks from the architectural CR3 and PCID and records (PCID, page) → (frame, effective rights under the
  current WP/NXE/SMEP and CPL class, global). Every operation of 2.2 applies exactly its minimum to the reference.
- Checks: at each install and validation, the installed frame and rights equal the independent walk's; after each INVLPG, #PF,
  INVPCID, flushing load and CR4 or CR0 write, the entries it touches; at every switch into a slot and every 4,096th resident
  entry, the whole slot and the shared half: each shadow in the reference with the same frame and no more rights, each block
  current under the slot with its source pages there through its frames, each entry of the owned L1 table and each non-local
  link out of a block current under the slot leading to a block current under it. At recycling and at every page freed: the
  slot's tables empty, the TLBI done after the last use. A failure halts with the PCID, the address and the last invalidation.
- Limits, stated plainly: check mode reads natemu's tables, not the Arm TLB, so it cannot see a stale Arm TLB entry left by a
  missing TLBI. The negative controls in P5b (a TLBI removed at recycling, at a flush, at a page free) cover that by guest-visible
  failure; optionally, `AT S1E0R` probes of addresses whose shadow was just removed check that the walk now fails.
- Plus the existing `--check-reuse --code-shape` boots. QEMU cannot be a differential reference: its TCG has neither PCID nor
  INVPCID (INV-03-01 profile notes; `natemu-raw/qemumax-20261104-01/` README: neither `like` nor `max` has them).

## 4. PTI-on benchmarks

### 4.1 Cells

Command line (owner decision 1): the standing one with `pti=on` in place of `mitigations=off`, i.e. `pti=on nokaslr`, other
mitigations at Linux's defaults; each run's serial log must show PTI force-enabled, and its other mitigation lines are recorded.
Cells: installer N = 1 and 4; `hackbench`, `locks`, `build`, `mem` at N = 1 and 4 (system-call, IPC, fork/exec and fault heavy);
`zstd` N = 1 as the compute canary. Same initramfs and pins as `natemu-raw/bench-20261105-03/`, cold and warm runs as there,
compile cache off. Per change, from P5b on: PTI installer N = 1 and PTI `hackbench` N = 1 for changes touching CR3, TLB, shadow or
link paths; the full PTI set at batch end. The standing comparisons keep `mitigations=off nokaslr` (decided).

### 4.2 QEMU like-for-like

QEMU 11.1.2 TCG multi-threaded, the like-for-like `-cpu` string (`Haswell,-pcid,-hle,-rtm,-invpcid,-tsc-deadline,-erms,-mca,
-mce,+sha-ni,+adx,+clflushopt,+clwb,+smep,+umip`), the same command line. TCG has no PCID or INVPCID, so QEMU runs PTI without
them; its column is QEMU at its best for this command line. P3 probes whether TCG accepts `+arch-capabilities` with natemu's bits
(QEMU property names `rdctl-no`, `taa-no`, `rsba`, ...); if so, QEMU gets the same bits and both guests apply the same non-PTI
mitigations, else the differing lines are recorded. A natemu-without-PCID like-for-like cell uses the guest's
`clearcpuid=pcid,invpcid` (P3 checks 6.12 accepts names) on both emulators, once at batch end. After PCID ships, QEMU no longer
does the same guest work in the mitigations-off cells either (Linux uses PCID on natemu only); noted in the matrix. QEMU is
measured once for the PTI set, three rounds.

### 4.3 Why PCID's gain differs with PTI and without

- **With PTI** every system call, interrupt and fault from user mode switches from the process's user root to its kernel root
  and back. Both are reused at once, whatever the number of processes, so with copies nearly every switch finds its copy
  (chunk 24's PTI installer: 260.1 k of 263.0 k returning loads went to one of the 6 most recent roots). The saving scales with
  kernel entries.
- **With mitigations off** CR3 is loaded only when the vCPU switches processes; a copy helps only on a switch back to one of
  Linux's 6 per-CPU address-space IDs, and only by what that process refaults. Chunk 24's bounds: 0.19 s (`luajit`, 4.2 k
  returning loads), 0.09 s installer, 0.11 s `build` (new processes start on new roots), 0.00 / 0.03 s `hackbench` N = 1 / 4
  (80 tasks per the bench README; at N = 1 24.2 k of its 26.2 k returning loads went to a root 12 or more loads back, beyond 6
  IDs), 0.02 s `mem`, none on `locks` and `cray` (one address space, no CR3 loads in the window). Hence exit criterion 3.

### 4.4 Expected effect (estimates; P3 measures the "today" column)

Per-load costs: a CR3 load's own work 1.5 us (chunk 24, `hackbench` N = 1 mitigations off: 0.04 s / 26.9 k) to 3.7 us (chunk 24
PTI installer: 0.98 s / 263 k). After copies, per kernel entry and exit (cell trip cost t from `estimates.txt`: `hackbench` 0.18
us, `locks` 0.13, `mem` 0.24, `build` 0.55, `zstd` 0.33): two CR3 reads and two CR3 writes, four trips; one VERW, one more trip unless
P2 has made it inline; two L1 slot writes at about 0.09 us; with demotion (3.5) no extra linker trip. Per entry: 5t + 0.18 us with VERW as a
trip, 4t + 0.18 us with P2's inline VERW (copy-switch cost unmeasured). Kernel entries from chunk 24's mitigations-off windows (system calls and page
faults; interrupts not counted, so each is a lower bound).

| Cell (N = 1) | Off, s (phase result) | Kernel entries (chunk 24) | PTI adds today, loads only (lower bound) | PTI adds after copies, VERW as a trip / inline (P2) |
| --- | --- | --- | --- | --- |
| installer | 5.42 | P3 counts them | P3 measures (`pti=auto`, no PCID: +8.9 s in chunk 24) | P4's re-count sets it |
| `hackbench` | 11.86 | 16.0 M system calls | 32 M x 1.5-3.7 us = 48-118 s | 16.0 M x 1.08 us = 17.3 s / 16.0 M x 0.90 us = 14.4 s |
| `locks` | 3.90 | 3.08 M system calls | 6.2 M x 1.5-3.7 us = 9-23 s | 3.08 M x 0.83 us = 2.6 s / x 0.70 us = 2.2 s |
| `mem` | 7.93 | >= 1.24 M faults | >= 2.5 M x 1.5-3.7 us = 3.7-9.2 s | >= 1.24 M x 1.38 us = 1.7 s / x 1.14 us = 1.4 s |
| `build` | 11.48 | >= 82.7 k faults | >= 0.25-0.6 s | >= 0.24 s / >= 0.20 s |
| `zstd` | 3.07 | >= 4.4 k faults (at `-9`; now `-12`) | ~0.01-0.03 s | ~0.01 s |

N = 4 has no estimate: chunk 24 counted kernel entries per cell at N = 1, and the work splits over four vCPUs; P3 measures them.
What the table says: copies remove most of PTI's cost on fault- and call-light work, but on `hackbench` the remaining per-entry
trips stay large (14-17 s), so P7 (CR3 reads inline at CPL 0, as CR2 in chunk 26; a fast path for a no-flush CR3 write to a slot
in hand, as SYSCALL's in chunk 22) is likely needed; P3's counts of CR3 reads and writes per entry decide. Today's PTI `hackbench`
may run for minutes (lower bound 48 s added), so P3 runs it once first with a 900 s cap.

## 5. Chunks

Each chunk: measure first, build, tests with negative controls (scratch mutations that must turn a test red, outputs kept), the
per-change measurement (the cells it targets plus installer N = 1 and one canary per benchmark family, three interleaved runs; from
P5b on also 4.1's PTI pair), an independent review, then its record in perf-plan.md style.

**Order and the PCID stop rule (owner, November 5: if PCID turns out to cost much, skip it).** P1-P3 stand on their own and come
first: the accurate bits with the speculation audit, the exact inline VERW, the PTI baselines. P4 builds PCID's architecture on
a branch and measures it; its gate decides whether any per-PCID copy is built. If the gate fails, the phase ends after P1-P3
(P4's branch not merged, PCID and INVPCID not advertised), with PCID recorded as not built and why (the gate's numbers). A
second check after P5b can still stop it (P5b).

**P1: the IA32_ARCH_CAPABILITIES bits, and the resident's speculation audit.**
- Measure first: the default-command-line installer N = 1 on HEAD (three runs) with its mitigation lines; how RET is translated
  (FEX's output for `ret` blocks: a computed exit through the dispatcher, a host indirect `br`), deciding RSBA/RRSBA.
- Audit: every resident path that performs a CPL-3 access on the guest's behalf through the guest-RAM alias (`segments::linear`
  → `pages` → `copy_out`, string and MMIO completion, emulated system instructions): after the permission result, an SB (or an
  address mask depending on it) before the first load or store through the alias. Measured on `hackbench` and `mem` N = 1.
- Build (after the audit): MSR 10AH read-only with 2.4's values, RDCL_NO from the host's CSV3 at startup, CPUID.(7,0):EDX[29]; in
  `cpu_profile.json` and the generator. The Vol.2 and Vol.4 pages, Intel's "CPUID Enumeration and Architectural MSRs" document and the per-flaw
  guidance (TAA, MMIO stale data, GDS, RFDS, ITS) added to the snapshot; each set bit re-confirmed against them, its reason recorded.
- Tests: RDMSR value, WRMSR #GP, the CPUID bit; a host without CSV3 gives bit 0 = 0 (unit test); boot log (PTI disabled, MDS
  vulnerable with clearing attempted; TAA, MMIO stale data, GDS, RFDS, ITS not affected). Negative controls: the MSR left #GP with the CPUID bit set; an audit
  barrier removed (a unit test that the barrier sits between the check and the access, by the stack-audit's method of inspecting
  the resident's code).
- Measure: default command line installer N = 1; the mitigations-off cells must not move.

**P2: VERW inline, exact (owner decision).**
- Measure first: a scratch counter of VERWs per cell under the default command line and `pti=on` at P1's tree, and their trip
  time (perf resident); estimate before the count: one VERW per return to user, `hackbench` N = 1 16.0 M x 0.18 us = 2.9 s.
- Build: VERW (register and m16 forms) translated inline where the result can be computed exactly without the resident: the selector
  is checked as Vol.2B VERR/VERW defines (null selector, TI and the GDT/LDT limit, the descriptor's S bit and type: a writable data
  segment, DPL against CPL and RPL), ZF set or cleared, no other flag or state changed; any case outside the inline form's reach (an
  LDT selector, a descriptor read that would fault or needs a walk, a memory operand that faults) exits to the resident's existing
  transaction (`segments::inspect`), which stays the reference. The descriptor is read through the shadow like any guest load, so a
  GDT change is seen.
- Tests: the existing VERR/VERW cases run inline and through the resident with identical results; every selector class (null,
  beyond limit, LDT, code, read-only data, writable data, each DPL/RPL/CPL combination); a GDT entry rewritten between two VERWs.
  Negative controls: the DPL check dropped; the limit check off by one; ZF left unchanged on failure.
- Measure: `hackbench` and `locks` N = 1 under `pti=on` and the default command line.

**P3: baselines and probes (no code kept).**
- natemu at P2's tree, the PTI cells of 4.1 (`hackbench` first, once, 900 s cap), then three rounds; QEMU's PTI cells three
  rounds; the default command line installer on both.
- Scratch counters (perf resident, tree restored after): per kernel entry the CR3 reads, CR3 writes and VERWs; per root the table
  pages in use; links from global into non-global blocks; L1 fills per root; global and non-global shadow installs.
- Probes: host ASID width through HVF; QEMU `+pcid` (refusal expected) and `+arch-capabilities,...`; `clearcpuid=pcid,invpcid` on
  6.12; whether the AMD machine can run a ring-0 probe (a kernel module in that VM) for INVPCID's exception order and its register
  form.
- Output: 4.4's "today" column, pool and K evidence, the input to owner decision 2.

**P4: PCID and INVPCID, architecture only (option A), and the gate.**
- Build: the CPUID bits; CR4_VALID bit 17 with 2.1's checks; CR3 with PCIDE = 1 (PCID field, bit 63 not stored, reserved bits, the
  PML4 read's PAT entry 0); INVPCID decode (host) and the resident transaction with 2.3's checks and order. Every no-flush load
  acts as today's CR3 load; INVPCID type 0 does INVLPG's work, types 1 and 3 today's CR3 flush, type 2 today's global flush.
- Tests (`run_k1.py`-style, natemu only, 1-4 vCPUs, both block sizes): each #GP/#UD of 2.1 and 2.3 with the state unchanged;
  CR3 read-back without bit 63; PCIDE 0 → 1 with CR3[11:0] = 0 and not; each mandatory invalidation of 2.2 as a case where the
  guest changes a PTE and must see the new mapping after the operation (data and code). Negative controls: each check dropped;
  INVPCID type 2 not reaching global entries.
- Measure: the mitigations-off set (Linux now uses PCID there; three interleaved rounds against P3's tree) and the PTI cells at
  N = 1.
- **Gate (go/no-go before any copy is built).** Inputs, all measured on this branch: (i) the mitigations-off cells against P3's
  tree; (ii) chunk 24's counter run again under `pti=on` with PCID on (PCIDs in place of roots): what copies would keep per PTI
  cell, priced with each cell's own unit costs (an upper bound); (iii) the cost of a slot switch, from a scratch build that
  switches TTBR/ASID at every no-flush load without keeping anything, timed on PTI `hackbench` N = 1 (switches x cost). **Go**
  only if all hold: no mitigations-off cell slower beyond its run-to-run range; on the PTI installer N = 1 and on at least two of
  `hackbench`, `locks`, `mem`, `build` at N = 1, the counted saving less the switch cost is at least 10% of the cell's wall; and
  4.4's residual estimate, redone with the measured switch cost, leaves natemu able to meet owner decision 2's bar. **Stop**
  otherwise: P4 is not merged and the phase goes to P8 with P1-P3 only. The gate's numbers also set exit criterion 2 and K.

**P5a: layout, free list and slots with behaviour unchanged.**
- Build: the pool moved into the vCPU's private window with a free list; slot roots and ASID blocks allocated (one slot used);
  `select` comparing the ASID; invariant 4's checks over every slot root, the shared half and the new pool; the list of full-reset
  paths of 3.6 routed through one function that resets every slot.
- Tests: abi unit tests: an install targeting any slot root, the state page, a pool page or a shared-half table is refused; a
  descriptor linking into another slot's table is corrupt; the free list never hands out a page before its TLBI. Negative controls:
  each check narrowed to today's ranges.
- Measure: the per-change set: no change expected (one slot).

**P5b: per-PCID data-shadow copies (option B) and check mode, data part.**
- Measure first: none beyond the gate's (P4: counts and the slot-switch cost).
- Build: PCID-to-slot table, switches, flushing loads per slot, recycling, the shared half per top-level entry, INVLPG/#PF/INVPCID
  type 0 over all slots with `TLBI VAAE1`, per-slot and full-reset TLBIs, the reference TLB.
- Tests: `run_k1.py` cases: PCIDs p and q map one page differently, switches with and without bit 63; INVPCID 0/1/3 from another
  PCID; INVLPG of a global page from another PCID; more PCIDs than K cycled (each sees its own page, never another's); two vCPUs
  with the same PCID numbers; a WP, NXE or SMEP change after retained copies (rights re-walked in every slot); a vCPU-migration storm
  (many competing host threads so vCPU threads move between cores while the guest cycles PCIDs with remaps; each vCPU must see only
  its own mappings). Negative controls: recycling without TLBI; recycling without resetting tables; a page freed before its TLBI; a
  flushing load that keeps the slot; INVPCID type 1 ignored for a non-current PCID; a non-global shadow put in the shared half;
  INVLPG reaching only the current slot; a full-reset path that resets only the current slot.
- Measure: the per-change set plus PTI `mem` and `locks` N = 1; shootdowns and full resets per vCPU.
- Second check: if a mitigations-off cell is slower beyond its range (Arm TLB pressure, pool resets, shootdowns) and the chunk
  cannot remove it, or the PTI cells gain under half of what the gate counted for data copies, P4-P5b are not merged and the phase
  goes to P8 with P1-P3, PCID recorded as not built with these numbers.
- Risks: Arm TLB pressure from more ASIDs (seen as slower mitigations-off cells); pool exhaustion; more shootdowns.

**P6: blocks, L1 and links per copy (option C) and check mode, code part.**
- Measure first: revalidations and links undone at no-flush switches after P5b; links from global into non-global blocks and their
  demotion cascade lengths.
- Build: mask-and-stamp currency; L1 ownership and parking; the link invariant's hooks and demotion, local links exempt.
- Tests: `run_k1.py` code cases: a code page remapped under PCID p while q runs, p reloaded with and without flush (new code after
  a flush; either code without one, never q's); a link chain made under p entered under q where q maps the target RIP elsewhere; an L1
  entry for a RIP mapped differently in p and q; a global block linking into a non-global one, then a flush of that slot (the global
  block must not reach the stale target); local links under two PCIDs; chunk 19's `contexts` case under two PCIDs. Negative
  controls: a revalidation keeping stale slots in the mask; the link-time check removed; the revalidation hook removed; demotion
  skipped; the cascade stopped one link early; unparking after a flush of the owner without the list visit; parking skipped.
- Measure: the per-change set and the PTI set at N = 1.
- Risks: `bad_link` halts from a missed hook; slot writes on hot paths (counted).

**P7: CR3 fast paths (only if P3/P6's counts show the trips decide the PTI cells).**
- Measure first: CR3 reads and writes per second per PTI cell and their trip time (perf resident).
- Build: MOV from CR3 inline at CPL 0 (as MOV from CR2 in chunk 26; bit 63 never stored); a resident fast path for a no-flush MOV
  to CR3 to a PCID holding a slot (switch roots, park/unpark L1, no transaction).
- Tests: inline CR3 read-back against the resident's; CPL > 0 still #GP; a no-flush write to a PCID without a slot takes the slow
  path; a flushing write never takes the fast path. Negative controls: the fast path taken for bit 63 = 0, or for a recycled slot.
- Measure: PTI `hackbench`, `locks` N = 1 and 4.

**P8: batch end.** The full mitigations-off matrix and the PTI set, three rounds; QEMU's PTI cells from P3; the
natemu-without-PCID like-for-like cell (4.2); the check-mode boots of exit criterion 5; the read-only workload-specific-code
audit; the phase record and the investigation record's row. If PCID was stopped: the same matrix and PTI set on the P1-P3 tree
(no check-mode or PCID cells), and the record says PCID was not built, at which gate, and with what measured cost and counted
gain. Applicability: every rule here is conditioned on CR0, CR3, CR4,
EFER, PCIDs and G bits only; the patterns beyond Linux that hit it are kernels that tag address spaces with PCIDs (Windows' KVA
shadow and FreeBSD's PTI use PCID by their published descriptions; not tested here). The one layout-dependent gain stays chunk
14's (global pages in the upper half), now decided per top-level entry within it.

## 6. Owner decisions needed

1. The PTI-on cells' command line: **`pti=on nokaslr`, other mitigations at Linux's defaults, on both emulators.**
2. The done bar for the PTI-on cells against QEMU's PTI-on cells: **set after P3 has measured both** (recommendation then: faster
   than QEMU in every PTI cell, as in the mitigations-off matrix, if P3's numbers and 4.4's estimates make that reachable).

Decided, recorded here: MDS_NO stays 0 and VERW is made cheap (owner, November 5; reasons at the top; P2). The standing
comparisons keep `mitigations=off nokaslr` (owner, earlier). Everything else (K, pool size, the per-top-level-entry rule, the
other bits, per-change PTI cells, QEMU flags, P7) is the orchestrator's from the evidence named in each chunk.
