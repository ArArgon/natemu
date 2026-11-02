# Correctness checklist: contracts the current validation enforces

Status: first draft, October 26, 2026. Written by an author who does not write
the emulator. This inventories existing assertions; it does not certify their
correctness or applicability to a replacement. Use it for the mechanism currently
being changed under [the restructure plan](../vm-restructure.md#correctness-and-execution-discipline).
Numbered plan references below refer to an earlier draft, not current execution
requirements. The current plan governs verification and migration without an
upfront whole-suite audit.

Later change (S3a2, October 27, 2026): the I1–I3, address and stack cells, their host drivers and their I3-only tools
are deleted; [slice3-contract.md](slice3-contract.md#s3a2-covering-and-deleting-the-i1i3-address-and-stack-paths) maps
each of their checks to a general-path check or a retirement reason. References below stay as of `577a7f2`.

Later change (S3i, October 28, 2026): every runner, checker, worker, producer and fixture named below
(`run_i1.py`, `run_i4v.py`, `run_i4v_qemu.py`, `check_i*.py`, `i4v_*.py`, the workers, `tools/i4v_producer`,
`tools/i4v_qemu`, `fixtures/i1`, `fixtures/i4v`) and the runtime/abi regression families are deleted; none of them can
be run. The four independent models moved to `tools/models/` (`ordering_model.py`, `coherence_model.py`,
`pae_model.py`, `modes_model.py`). [slice3-contract.md](slice3-contract.md) §3, its S3a2 and S3h tables and §5 map
each old check to a general-path check, a retirement reason (32-bit and paging-off families: owner decision 3,
deferred to the boot phase) or a boot-phase item. References below stay as of `577a7f2`.

Later change (boot-features, October 30, 2026): the rows of the table below were brought up to `9110c6f` (I5a-I10, K1/K2, M1/M3,
L1-L4, X1-X4, S4, P3 and the guest-state page move G1; [the plan's Built notes](boot-features-plan.md)). The contract bodies in §1
still read as of `577a7f2`.

### General-path check per contract (S3i2, October 28, 2026)

Every contract below has a general-path check, a retirement reason or a boot-phase handoff item
([boot handoff](boot-handoff.md) lists the items). Sources: [slice-3 contract](slice3-contract.md) §3 table, its S3a2
and S3h tables and §5 owner decisions 1–3 (D1 segmentation/A20/resolver, D2 tearing across 16 bytes, D3 all 32-bit and
paging-off support), and the slice-1/2 contracts. Runners (all under `tools/`, each `--fexc FEXC --out DIR`):
**fixtures/seed** `run_slice1_vm.py` (64 fixtures and 44 seed-4242 programs vs QEMU, block sizes default and 1);
**faults** `run_slice1_faults.py` (7,833 precise-fault cases at I10, paging cells among them, default and 1); **flow**
`run_slice2_vm.py` (64 control-flow programs × 3 sizes); **loop faults** `run_slice2_faults.py` (846 × 3); **probes**
`run_slice2_probes.py` (85 code-cache/SMC probes); **ASM** `fexc/upstream_tests/run_asm.py` (235 FEX ASM tests at S3i2, 1,864 passing at I10);
`run_slice3_vm.py` modes **corpora, smp, limits, kicks, system, coherence, sharedpt, litmus, contain, privilege, delivery, events**; **stack**
`check_resident_stack.py`. Added by the boot-features chunks, each named where a row uses it: corpora `tools/reference/gen_i{1,2,3,5a,6,6b,7,8,9,10}.py`
against QEMU 7.2 and `run_i5a_faults.py`, `run_i5a_de.py`, `run_i5b.py`, `run_i6_faults.py`, `run_i6b.py`, `run_i8_faults.py`,
`run_i9_faults.py`, `run_i10_faults.py`, `run_k1.py`, `run_k2.py`, `run_m1.py`, `run_m3.py`, `run_l3.py`, `run_l4a.py`, `run_x3.py`, `run_x4.py`. "Unit" = `cargo test` or the named Python test.

| # | General-path check, retirement reason or boot-phase item |
| --- | --- |
| 1 | fixtures/seed, flow, ASM, corpora and smp: final GPRs, RFLAGS and memory against QEMU. Vector, x87 and MXCSR results: the I5a (SSE to SSE4.2), I6 (x87), I6b (x87 transcendentals, PCMPxSTRx), I8 (AVX, AVX2, FMA, F16C), I9 (MMX, AES-NI, PCLMULQDQ, MOVBE, PDEP/PEXT, FFREEP) and I10 (0x67, XLAT) corpora (`tools/reference/gen_i5a.py`, `gen_i6.py`, `gen_i6b.py`, `gen_i8.py`, `gen_i9.py`, `gen_i10.py`) against QEMU 7.2 at block sizes default and 1, with QEMU's recorded departures stepped around (two-NaN rule, x87 condition codes, DAZ MIN/MAX); DE is judged by the SDM (`run_i5a_de.py`: QEMU never sets it), the x87 helper forms by `run_i6b.py cases`. Unmasked SIMD and x87 exceptions are refused (`unsupported_system`), not built (`runtime/src/execution/sse.rs:9`, `x87.rs:6`). |
| 2 | Same runs (whole register file and memory regions compared), `frame_intact` on every run, private state unreachable (21). Vector state: XMM, YMM upper halves and MXCSR in the I5a/I8 corpora, and equal to the run cut before the instruction at every fault of `run_i5a_faults.py` and `run_i8_faults.py`. x87 and MMX state: the stack stored at the end of the I6/I6b/I9 corpora; `run_i6_faults.py` and `run_i9_faults.py` compare it at each fault. Segment state: `system` (S2b programs against QEMU, descriptor-table and result memory; fault cases by `models/segment_model.py`). XCR0: `run_i5b.py` (XSETBV/XGETBV, XSAVE/XRSTOR round trips). |
| 3 | MOV CR keeps RFLAGS: `system` (QEMU programs, RFLAGS compared). EFER.LMA writes ignored: resident code reading only (`runtime/src/execution.rs` `system`, WRMSR keeps LMA); no program writes EFER with LMA clear and reads it back. Upper halves outside 64-bit mode: retired, D3. |
| 4 | Retired, D3: only 64-bit code is decoded (`Cpu::control` refuses any other mode, `abi/src/execution.rs:837-844`, and the vCPU stops `unsupported_mode`). Since K1 the mode is no longer in the code-cache key (`Cpu::code_context`, `abi/src/execution.rs:886`; see 32). |
| 5 | Division helper probes (#DE through the 128/64 `svc` helper) in faults; ASM; I6b's x87 (`svc #5`) and PCMPxSTRx (`svc #6`) helpers run in the resident (`abi/src/execution.rs:47, 51`): `run_i6b.py` corpus and cases. The gates save all 32 Q registers, FPCR and FPSR, clear FPCR (`gates.S:80`) and the ERET path restores them (stack). Helper stack: retired (the helpers run on the exception stack inside the gate bound of 62; the dispatcher's budget is 0). Vector width and FP controls: the I5a/I8 corpora and fault runs (see 2). LFENCE sink: retired with 51. |
| 6 | faults and loop faults at every block size (exact state at the fault); `fexc/verify_precision.py` and admission. |
| 7 | faults (error codes 0/2/3/5/7/9/17 and CR2 per paging cell), loop faults. Delivery through the IDT: built in X1a (`runtime/src/execution/delivery.rs:62`, no fault stops a run): faults and loop faults reach the driver's logging IDT and `delivery_check.py` rebuilds each frame, handler RSP and CPL with `models/delivery_model.py`; `run_slice3_vm.py delivery` (double-fault and shutdown cases, judged by the model alone) and `system` `dlv-handlers`. Events: 49. |
| 8 | faults: non-canonical stack/data probes (#SS/#GP), non-canonical ends (S3a2 table), and Intel's segment-prefix classes (#SS only for an SS-class reference: `null_segment_probes`, `run_i5b.py` `segment-*`). Far-transfer targets: `system` fault cases judged by `models/segment_model.py` (S2b). With 0x67: `run_i10_faults.py` (a garbage upper half never faults; FS.base + EA non-canonical is #GP(0)). |
| 9 | faults: paging cells, 6 controls (CPL3, CPL0, WP=0, SMEP, SMEP with supervisor code, NXE=0) × mutations × levels × leaf sizes. |
| 10 | faults: reserved per level, non-present garbage. MAXPHYADDR 40 (`abi/src/paging.rs:3`, reserved bits 51:40 at `:136`) and no 1 GiB pages (PS above a PDE is #PF with RSVD, `:138`; CPUID.80000001H:EDX[26] = 0, `tools/models/cpu_profile.json:1030`) are declared profile (`models/coherence_model.py`, `cpu_profile.py`). |
| 11 | faults: 2 MiB leaf cells (A/D on the PDE); sharedpt `large`. |
| 12 | faults (leaf A/D per page compared; fault-path leaf-A policy declared in `models/coherence_model.py`, §3 item 4), sharedpt (`ad-race`, `alias`, `large`). Non-leaf A not compared (§3 item 7). PAE and paging-off rules: retired, D3. |
| 13 | Guest consequence (D set exactly when written): faults (leaf D compared), sharedpt `ad-race` (after its own write a CPU always sees D). The install rule itself (a shadow is writable only for a write whose walk returned D, per vCPU): resident code reading only (`runtime/src/execution.rs`, `writable = write && t.writable && t.dirty`). Upgrade counts are internal (S3a2 table). |
| 14 | faults: page-crossing access forms and split stores (all-or-nothing, CR2 policy §3 item 8); litmus `split`/`split-hi`. Atomicity across 16 bytes: D2; aligned MOVAPS/MOVAPD/MOVDQA are one LDP/STP pair since I5a (`abi/src/code_object.rs:41-42`; its tear test was run once, no committed runner). |
| 15 | system (CR3 switch, INVLPG after a PTE change, WP and NXE toggles; same-value CR3 flushes), sharedpt `remap`/`large`/`alias` (after its own INVLPG a CPU sees only the new state), `run_k1.py` (remap + INVLPG or CR3 reload, a page crossing, a 2 MiB boundary, `invlpg [eax]` with 0x67). Paging-control changes (CR0.PG/WP, CR4.PGE/PAE/SMEP/SMAP/PCIDE/LA57, EFER.NXE/LME/LMA, MTRRs) are global TLB flushes (`runtime/src/execution.rs:1193`); clearing CR0.PG/PE and changing CR4.PAE/LA57 are #GP(0) (`:1155`, `:1173`); CR0.CD/NW are recorded only: refused or retired, D3. |
| 16 | Retired: not required (slice-3 §1; §3 items 3, 5, 6); each vCPU's shadows are its TLB until its own INVLPG/CR3. |
| 17 | Retired as a requirement (§3 item 2): either history is accepted, `system` (PTE change without INVLPG through `coherence_model.check_observation`), sharedpt (`ad-race` free race) and `run_k1.py` (natemu only: a PTE change without invalidation keeps the old translation, either frame accepted, the new one after INVLPG). |
| 18 | system (CR3 switches; every CR3 load resets the vCPU's shadows and makes its non-global blocks not current, `runtime/src/execution.rs:1169`, `cache.rs:319`; a block runs again only after a fetch walk finds its source pages, `cache.rs:289`), coherence (patch after `mov cr3, cr3`), `run_k1.py` (K rounds of CR3 A/B with different bytes at one linear address, 1-4 vCPUs, both block sizes). PAE ABA: retired, D3. |
| 19 | sharedpt `large` (2 MiB leaf ↔ table, both ways). |
| 20 | sharedpt `alias` (two linear pages, one frame, per-alias A/D); faults access forms at low, high and high-code placements. |
| 21 | faults: the E3 window probes (`window_probes`: every page class of the resident's private range, the former guest-state alias and its tables' pages, the high pool and high-code placement: #GP/#SS, nothing written) and non-canonical probes (S3a2 table); `frame_intact` (`host/src/drivers/program.rs:1443`) on every run; contain `el0-*` (forged loads, stores and branches into the private window halt `private_access`). Deviation (G1): the guest-state page at `GUEST_STATE_VA` 0xffff_4000_0000_1000 (`abi/src/execution.rs:88`, x86 non-canonical with bit 63 set, so Linux's user-pointer checks refuse it) is reachable by an access inside it (the vCPU's own state): `guest_state_probes`; LA57 and LAM must stay unadvertised (`guest_state_page_layout`, `abi/src/execution.rs:1649`). |
| 22 | Retired, D3 (paging off). |
| 23 | 64-bit addressing is flat: FS/GS base + EA mod 2^64 (I7: `gen_i7.py` corpus and `segment_probes` at base + EA) and the 0x67 32-bit address size, truncated before the segment base (I10: `gen_i10.py`, `run_i10_faults.py`). The 32-bit offset wrap, subpage bases and 16-bit address size: retired, D3 (CS/SS/DS/ES bases and limits unused, S2b). |
| 24 | Retired, D1: segmentation is flat (S2b; `Cpu::control` needs CS.L = 1, D = 0); segment loads and their rights checks are judged by `models/segment_model.py` in `system`. |
| 25 | Retired, D1. |
| 26 | Built under M1/M3/L3 rather than as D1 specified. A non-RAM page is a device page: serviced once and in order, never shadowed or retried, the walk's faults first, other forms typed `unsupported_mmio` stops (M3: `run_m3.py` `corpus` against QEMU and `models/mmio_model.py`, `faults`, `refusals`, `kicks`, `xmc`). An unassigned page (the legacy hole 0xA0000-0xFFFFF, [R, 4 GiB) but device pages) reads all-ones at the access width, writes are ignored and counted, RAM untouched (L3 rule: `run_l3.py` `pages`; `run_m1.py`). Ports: `run_l3.py` `qemu`, `cases`. Devices: L3 (UART), X3, L4a, L4b. |
| 27 | Retired, D3. |
| 28 | system: QEMU programs and fault cases (`system_fault_model.py` at QEMU's prefix state; five also by `models/modes_model.py`). PG-without-PE and legacy cases: retired, D3. |
| 29 | Retired, D3 (PAE hidden PDPTEs). |
| 30 | WRMSR EFER (reserved EAX and EDX bits, LME change under PG): system. IA-32e activation and LMA transitions: retired, D3. |
| 31 | system: S2b far transfers (JMP/CALL m16:32 and m16:64, RET far, IRETQ) against QEMU (`seg-basic`, `seg-boot` at 1, 2, 4 vCPUs) and `models/segment_model.py` (descriptor-check order, #GP/#NP/#SS, no effect on a fault; QEMU's departures in `QEMU_DIFFERS`). Compatibility-mode targets stop `unsupported_mode` and call gates `unsupported_system` before any effect (`runtime/src/execution/segments.rs:10-11, 379`): retired, D3. Task switches: not built. |
| 32 | Code-cache key holds CPL and the gating and rounding bits (`Cpu::code_context`, `abi/src/execution.rs:886`); since K1 not CR3, paging or mode controls: those are TLB flushes, after which every block's source pages are fetch-walked again before it runs (`cache::lookup`, `cache.rs:289`): system, coherence, `run_k1.py`. Stale compiler replies: retired, requests are synchronous (S3a2 table, I2 cancel). |
| 33 | system `sys-ctl-*` at 2 and 4 CPUs, block sizes default and 1 (S3c2). Hidden PDPTEs: retired, D3. |
| 34 | coherence (20 programs × 2 sizes), contain progress, probes (SMC), ASM (SelfModifyingCode), `run_k1.py` (a write to the other space's idle block, remaps through INVLPG and CR3 reloads), `run_k2.py xmc`/`coherence` and `run_m3.py xmc` (cross-modifying code at kept system records and device-load sites: old or new, never another stop). Writes from another mode: retired, D3. |
| 35 | contain (`offset`, `next-page`, `system`, `word`, `literal`, `record`, forged header), admission unit tests, and compile-cache entries that fail admission when used (`persistent_cache_answers_and_forgeries_are_refused`, `host/src/execution.rs:1323`). Reply-word flips: retired with the producer transport (S3a2 table). |
| 36 | Whole-cache flush only at the owner's resident entries, and since K1 only for a full pool (`runtime/src/execution.rs:200, 221`), the host's `FLUSH` at load (`:627`) and a DR7 write arming a new instruction breakpoint (`:1222`); a dead or unknown header is `bad_link`: contain (`word`/`literal`/`record` before a flush, forged header), probes with small `--code-cache`, coherence `stale`. Reference counting and reclaim order: retired (per-vCPU caches, no shared code; slice-2 contract). |
| 37 | faults: code-page cells (no compile before a first-instruction fetch fault), `straddle-*` (an instruction ending on the next page: translated across it when present, else #PF at the next page's first byte before decode); the resident walks the fetch before the host copies (S3d, `host/src/execution.rs:1217-1236`). |
| 38 | corpora (host requests = distinct blocks per vCPU), probes (small-cache flushes). The host keys an answer by the bytes fexc read (the reply's `extent`) and the gating arguments it consulted, plus a persistent on-disk cache for `boot-linux` (P3): `compiler_keys_answers_by_what_fexc_read` (`host/src/execution.rs:1264`, unit). Busy slot and cancellation: retired (synchronous, S3a2 table). |
| 39 | Placement (write alias, code synchronization, then lookup insert) under every suite; contain (forged header, linked word/literal/record tampers → `bad_link`). |
| 40 | Every suite (each first access is a shadow miss); faults compare CR2 and vector on all 7,833 cases (the I10 standing suite). |
| 41 | faults and loop faults at block sizes default and 1, precision verifier, admission. |
| 42 | Retired (S3a2 table): no acknowledgement protocol; shadows are each vCPU's TLB. |
| 43 | litmus XCHG chains (linearizable), faults (XCHG access forms, split-lock probes → `unsupported_atomic`), loop faults (XCHG stride). Under remap: retired with 42. LOCK ADD/CMPXCHG/XADD/BT*, CMPXCHG8B/16B: built in I3 (boot-features plan, Built (I3): RMW litmus cells, chains, contention, fault cases); split locks stop `unsupported_atomic` (owner decision). |
| 44 | I2: REP strings one element per pass (registers exact at every element): QEMU corpus `gen_i2.py` at block sizes default and 1; mid-string faults against QEMU's signal context (`run_slice2_faults.py` on its flow form, 112 midway); kicks `rep@N` (stops between elements, resumed exactly). Read-once of device sources: M3 (MMIO strings are a typed stop). |
| 45 | Retired (S3h table): no global service lock or queue; successor property checked by coherence `mutual`. |
| 46 | Replaced by the synchronous shootdown (S3h table): coherence `fresh-reused` (control: shootdown off fails), contain `unacked`. |
| 47 | contain progress, litmus, sharedpt contended walks (S3h table); the heads bound retired with the queue. |
| 48 | Retired (S3h table): no queue; a full code cache flushes (probes). |
| 49 | `events` (X2; QEMU x2APIC programs `ev-sti-shadow`, `ev-mov-ss`, `ev-popf`, `ev-iretq`, `ev-hlt-wake`, `ev-nmi` and the chained-shadow forms at 1, 2, 4 vCPUs, block sizes default and 1): every delivery is judged by `models/delivery_model.py` `recognise`/`after_instruction` (IF, STI and MOV SS shadows, NMI blocking, priority, RF mid-REP); its `--kick-events` stress (events posted at random stops, each delivery logged and at an allowed boundary, never late after a shadow); `delivery`, `privilege`, `run_x3.py` (LAPIC priority/TPR/EOI, IPIs, timer), `run_l4a.py` (I/O APIC and 8259 routing), `run_x4.py` (mailbox wake). Pending events are the vCPU's `Events` record (`abi/src/execution.rs:412`: posts set bits by atomic OR, NMI posts merge), taken by `events::take` at the top of `find` (`runtime/src/execution/events.rs:54-72`); guest IF is read only in `events::next` (the Arm IRQs that carry kicks and posts stay unmasked in translated code, E2). |
| 50 | litmus (234 cells: the S3e families' 90 plus I3's RMW families, at N = 2..4 and all four variants, `models/ordering_model.py`; I5a's vector families `vec-*` add 72 via `--families`), S3t probe. |
| 51 | Raw LFENCE/SFENCE/MFENCE: litmus (`sb-mfence`, `vec-sb-mfence`), ASM. LFENCE register capture: retired (there is no completion sink: the three fences are plain `dmb ld/sy/st`, `tools/fexc/shim/fexc_slice1.inc:637`, admitted at `host/src/admission/structure.rs:168`, so nothing is captured). |
| 52 | Every run: the resident (EL1; translated code runs at EL0, E2) halts unless EL1, SCTLR M/C/I and EnTSO hold (`runtime_main`, `runtime/src/main.rs:61`); `run_slice3_vm.py` checks EnTSO read back on every vCPU. |
| 53 | Retired (S3h table): a test mechanism of the old coordinator. |
| 54 | N = 0 cannot be described (a description has at least one vCPU); `execution::prepare` refuses 0 and 5 and a RAM size that is zero, not a 2 MiB multiple or above 3 GiB (unit test `execution::tests::one_to_four_vcpus`, `host/src/execution.rs:1356`); 5 is refused end to end by limits. Isolation: corpora and smp (a different program per vCPU on disjoint pages). vCPUs 1..N-1 start parked on the ACPI wakeup mailbox (X4): `run_x4.py` units, adversarial and trampoline at N = 2..4. |
| 55 | Admission unit tests, limits (S3a2 table). |
| 56 | contain `site` and `site-kind` on every vCPU at N = 1..4, banked state checked (S3a2 and S3h tables); contain `el0-*` (system instructions and window accesses forged over a block's entry trap in hardware: `trapped`, `private_access`; `tools/run_slice3_vm.py:1849`). |
| 57 | contain (S3h table). |
| 58 | contain (S3h table; banked state checked since S3i1). |
| 59 | contain: a fresh process after every failing run. |
| 60 | contain: `teardown: … children none` after every run. Containers: retired with Docker. |
| 61 | stack: gates' save sequences, vectors and the ERET restore path, instruction by instruction. LFENCE sink and partial-store gates: retired with 51 and 44. |
| 62 | stack (`check_resident_stack.py` on the HEAD build): gate 19,072 bytes (23,216 with one nested entry to its halt) of the 32,768-byte exception stack; boot 12,752 of 32,768; the dispatcher `dispatch_template` 0 SP uses or literal loads, budget 0 (the helper stack is gone). |
| 63 | fexc unit tests (`fexc/test_slice1.py`: unsupported forms and window ends cut, never compiled), admission; the host copies only the walked guest-physical page and the resident walks the next one (S3d); an instruction that needs bytes past the end gets `partial_decode`, the host then asks for the next page (`host/src/execution.rs:1217-1236`), and a 16th byte is never fetched (`straddle-*`, `long-*` cases). |
| 64 | `fexc/check_sites.py`, precision verifier, admission (every memory word must be a described site), contain `site`/`site-kind`. |

Source state: working tree at commit `577a7f2`. The starting point is the accepted
[I4-V contract-to-test matrix](../research/reviews/INV-03-01-i4v-coverage.md).
Every runner, checker and model under `tools/` was read, plus
[the ARM64 execution ABI](../research/specs/arm64-execution-abi.md). File names
below are relative to `tools/` unless a path is given. A `file:line` names the key
assertion(s). The suspected wrong expectations in section 3 were re-read in the
source. Other line numbers can drift with edits, so the per-family inventory must
re-verify them.

## How to read this

Each contract is a property of an emulator, not of a test case. Each entry gives:

- **Enforced by:** checker `file:line` for the key assertions.
- **Grid:** the parameters it is exercised over.
- **Observable today:** one of
  - *guest*: registers, memory, exception packet, guest page-table bytes, control registers;
  - *internal*: counters, witness records, Rust-field-derived records, native leaves, resident addresses;
  - *trace*: the runtime's emitted event stream;
  - *binary*: audit of compiled code;
  - *host*: processes, threads, containers, wall-clock time.
- **Kind:** one of
  - *architectural*: defined by the Intel SDM, the AMD APM or x86-TSO;
  - *design*: a choice of this emulator or of its ABI;
  - *producer-transport*: specific to the FEX producer running in an out-of-process
    worker or Docker container. Such a contract may become obsolete when code
    generation moves in-process. This document does not decide that.

Mixed kinds are written as, for example, "architectural core; design policy".

### Families and grids (from `run_i4v.py:58-104`, `run_i1.py:240-312`)

| Family | Checker | Grid |
| --- | --- | --- |
| I1 entry/live load/fault | `check_i1.py` | control, live_load vectors 0–3, fault, 8 host-submission mutations; reject N=2 |
| I2 live translation | `check_i2.py` | live, source_stale, context_stale, cancel, capacity, bad_reply |
| I3 paging | `check_i3_paging.py` | cold, load_np, store_ro, parent_ro, wp_off, user_denied, reserved, fetch_nx, smep, carry, overflow, large |
| I3 allocated | `check_i3_pressure.py` | {pressure, fp, helper} × {cold, load_np, store_ro} + partial (REP) |
| I4 entry/faults/remap/compose/events/pressure | `check_i4.py`, `check_i4_pressure.py` | N=1–4 (entry also delay 0/50); reject N=0,5 |
| Address (eager and live) | `i4v_check.py` | {high, low RIP} × cold, load_np, user_denied, fetch_nx, smep, low_operand, private_operand; live high/cold × 16 reply-word flips |
| Stack | `i4v_stack_check.py` | {high, low} × 17 scenarios; high/cold × 48 reply-word flips (3 chunks × 16) |
| Aliases | `check_i4v_alias.py` | {low, high} × N1–4 |
| Concurrent stack / mixed aliases | `check_i4v_stack_concurrent.py` | {plain, parent, leaf} × {low, high} × N1–4 |
| Paging-off | `i4v_off_check.py` | {base, offset} × {cold, readonly, warm}; base/cold × 48 flips |
| Paging-off steps | `i4v_off_steps_check.py` | segments, a20-off, ds-limit, cs-before-load, cs-inside-load, cs-first, ds-readonly, device, hole, linear-wrap; segments × 25 flips |
| Mixed modes | `check_i4v_mixed.py` | {off-first, paged-first} × N1–4 |
| Controls | `i4v_controls_check.py`, models `i4v_pae_model.py`, `i4v_modes_model.py` | 39 programs (`run_i4v.py:69-71`) |
| Concurrent controls | `i4v_controls_concurrent_check.py` | {legacy, pae, long-aba, long-direct, rejected} × N1–4 × every actor (50) |
| Stack boundaries | `check_i4v_stack_boundary.py` | {ss, gp, split, split-np, split-ro} × N1–4 × every actor; region by parity (50) |
| Address forms | `check_i4v_address_concurrent.py` | {subpage, a20-off, linear-wrap, address16, ds-boundary, cs-boundary} × N1–4 × every actor (60) |
| Ordering | `check_i4v_ordering.py`, model `i4v_ordering_model.py` | litmus sb, mp, lb, wrc, iriw, sb-mfence, sb-xchg at fixed N; xchg, xchg-pre, xchg-post, lfence, sfence, mfence at N1–4; every rotation; seeds 1, 37; 32 trials (154 cells) |
| Fence | `check_i4v_fence.py` | 5 operand cases × N1–4, 16 trials |
| Coherence | `check_i4v_coherence.py`, model `i4v_coherence_model.py` | {parent, ad-legacy, ad-pae, ad-four, size-pae, size-four} × N1–4 × every actor (60) |
| Seeded progress | `check_i4v_schedule.py` + `check_i4v_progress_protocols.py`, `check_i4v_atomic_progress.py`, `check_i4v_partial_progress.py` | {stack, controls, coherence, atomic} × N1–4 × 4 seeds; partial × N1–4 × 2 seeds (72) |
| Containment | `check_i4v_containment.py` | 5 failure kinds × {N1 a0, N2 a0, N2 a1, N4 a0, N4 a3}, each with a fresh-process control (25 + 25) |
| Binary gate audit | `check_i4v_fence_gate.py` | the produced runtime ELF (`--partial` with progress/containment) |
| QEMU comparison | `run_i4v_qemu.py`, `i4v_qemu/model.py` | six scalar programs |

## 1. Contract list

There are 64 contracts, grouped by emulator responsibility.

### A. Instruction effects and state preservation

**1. An executed instruction has exactly its x86 effect.** This covers: ADD sets
CF/ZF/SF/OF/AF/PF; SETZ; MOV loads and stores of 8/16/32/64 bits with
zero-extension of 32-bit destinations and merging of 16-bit ones; PUSH imm32
sign-extended to 8 bytes; 16-bit PUSH decrementing by 2; PXOR/POR across all
lanes; ADDPS round-to-nearest with MXCSR sticky flags; LAHF/SETO; XCHG
exchanging and zero-extending.
- Enforced by:
  - `check_i1.py:82-93` (independent ADD flag model), `:131-151`;
  - `check_i3_paging.py:91-95, 269-271`;
  - `check_i3_pressure.py:57-59` (ADDPS, MXCSR 0x1FA0);
  - `i4v_check.py:111-119`;
  - `i4v_stack_check.py:91-101, 295-304`;
  - `check_i4v_stack_concurrent.py:62-84`;
  - `check_i4v_stack_boundary.py:121-146`;
  - `i4v_off_check.py` (moffs 16-bit merge, final EAX 0xFFFF0042);
  - `check_i4v_fence.py:45-69, 117-128` (binary32 RNE reference, LAHF/SETO, MXCSR);
  - `i4v_coherence_model`-driven DWORD effects `check_i4v_coherence.py:147-162, 502-510`;
  - `i4v_qemu/model.py:14-33`.
- Grid: ADD inputs (0,0), (2⁶⁴−1,1), (2⁶³−1,1), (1,2); every family's programs.
- Observable today: guest. FEX flag encodings (native NZCV with inverted carry,
  PF/AF in x26/x27, x20 scratch) are internal.
- Kind: architectural. The native flag representation is producer-transport.

**2. Architectural state an instruction does not write is preserved.** This
covers the full canonical state: 16 GPRs including upper halves, RIP, RFLAGS,
16 full YMM including upper halves, MXCSR, x87, segments, XCR0 and control
registers. It also covers guard bytes around every write, and the runtime-private
bank being unchanged.
- Enforced by:
  - `check_i1.py:106, 163-164`;
  - `check_i3_paging.py:54-66, 228, 275` (type-strict exact);
  - `i4v_check.py:233-234, 254-255`;
  - `i4v_stack_check.py:301-305`;
  - `check_i4v_ordering.py:367, 374-377`;
  - `check_i4v_stack_concurrent.py:98-105, 518-523`;
  - `i4v_controls_concurrent_check.py:1047`;
  - `check_i4v_coherence.py:611-627`.
- Grid: every case.
- Observable today: guest. Bank and native frames are internal.
- Kind: architectural. Bank isolation is design.

**3. Undefined and implementation-chosen results are deterministic stated
policies.** MOV CR leaves arithmetic flags unchanged. The upper halves of the
first four GPRs are zeroed outside 64-bit mode. EFER.LMA writes are ignored.
- Enforced by: `i4v_controls_concurrent_check.py:238, 583`; `i4v_controls_check.py`
  (docstring declares flag retention); `i4v_modes_model.py:150` (LMA);
  `i4v_controls_concurrent_check.py:671-672` (32-bit masking outside long mode).
- Grid: controls, concurrent controls.
- Observable today: guest.
- Kind: design policy. Label it policy and compare it with QEMU where QEMU
  defines it.

**4. Decoding depends on the current mode, not on the bytes alone.** Bytes
`48 8b 03` decode as DEC EAX; MOV EAX,[EBX] in compatibility/protected32 and as
MOV RAX,[RBX] in 64-bit mode. A compiled object is keyed by decode mode.
- Enforced by: `i4v_controls_worker.py:14-16, 24`; `test_i4v_controls_worker.py:51-66`;
  `i4v_coherence_worker.py:22`; `run_i4v.py:683` (bitness = 64 iff mode 4).
- Grid: controls and coherence profiles.
- Observable today: internal (request fields).
- Kind: architectural.

**5. Live guest state survives every helper call and gateway.** This covers GPRs,
flags, full vector width and FP controls. Only declared outputs change.
Native FPCR/FPSR are restored after runtime FP work. The poison written by a
deliberately clobbering helper never leaks.
- Enforced by:
  - `check_i3_pressure.py:216, 318-319, 365-377`;
  - `check_i4v_fence.py:214-225` (LFENCE completion sink captures all roots and restores exactly);
  - binary audit in contract 60.
- Grid: i3-helper × 3; fence 5 × N1–4.
- Observable today: internal (witness frames). Guest consequence: final registers.
- Kind: design (ABI §4 gateway rules). The guest outcome is architectural.

### B. Exceptions

**6. Exceptions are precise.** Every instruction before the faulting one is
committed. The faulting instruction commits nothing: no register, flag, memory
or RSP change. RIP is the faulting instruction.
- Enforced by:
  - `check_i3_paging.py:159-173, 269-271`;
  - `check_i3_pressure.py:46-64`;
  - `i4v_stack_check.py:309`;
  - `check_i4v_stack_boundary.py:149-165, 311-337`;
  - `check_i4v_stack_concurrent.py:266, 380-397`;
  - `check_i4v_address_concurrent.py:243-267, 616-637`;
  - `i4v_off_steps_check.py:183, 234, 260-267`;
  - `i4v_controls_check.py` (profiles 7, 20, 34, 35: a later fault never rolls back a committed control/transfer).
- Grid: every faulting scenario.
- Observable today: guest. The native pre-commit frame is internal.
- Kind: architectural.

**7. A guest page fault reports vector 14, CR2 = faulting linear address and the
SDM error code.** The error-code bits are P, W, U, RSVD and I/D. I/D appears only
when EFER.NXE or CR4.SMEP is enabled. CR2 is unchanged by #GP, #SS and internal
repairs. A fetch fault reports CR2 = RIP.
- Enforced by:
  - `check_i3_paging.py:36-49, 219-224, 273-275`;
  - `i4v_check.py:26-37, 117-118, 249`;
  - `i4v_stack_check.py:32-50, 296-298`;
  - `i4v_controls_check.py:136` (no invented I/D without NXE);
  - `check_i4v_coherence.py:480-487`;
  - `check_i4v_stack_boundary.py:170-176`;
  - `check_i4v_address_concurrent.py:179-211`.
- Grid: error codes 0, 2, 3, 5, 7, 9, 17.
- Observable today: guest.
- Kind: architectural.

**8. Canonicality is checked, and the fault type follows the segment.** A
noncanonical stack (SS-based) access gives #SS(0). A noncanonical DS-based
access gives #GP(0). A noncanonical RIP gives #GP(0) before any walk. A span
crossing the canonical hole is noncanonical. A noncanonical far-transfer target
gives #GP(0) after the descriptor checks.
- Enforced by:
  - `i4v_check.py` (private_operand; noncanonical cases in unit tests only);
  - `i4v_stack_check.py` (ss_first, ss_after_first, ss_span, gp_after_pushes);
  - `check_i4v_stack_boundary.py:89-93, 168-176`;
  - `i4v_modes_model.py:198`.
- Grid: stack 4 scenarios × 2 regions; boundary ss/gp × N × actor; controls profile 38.
- Observable today: guest.
- Kind: architectural.

**9. Paging permissions follow the SDM.** Effective R/W and U/S are the AND
across levels. CPL0 writes honor CR0.WP. A CPL3 write to a read-only page faults
regardless of WP. A CPL3 access to a supervisor page faults. A fetch from an NX
page faults when NXE is set. A CPL0 fetch from a user page faults under SMEP.
CPL comes from CS.
- Enforced by:
  - `check_i3_paging.py:106-137` (table encodings), `:219-227`;
  - `i4v_stack_check.py:170-171` (parent_ro, wp_off, user_ro, user_denied, user_ok);
  - `i4v_controls_check.py:109-125`;
  - `i4v_coherence_model.py:154-165`.
- Grid: I3 paging 12; stack 17; controls 2-level.
- Observable today: guest.
- Kind: architectural.

**10. Reserved and non-present entries are distinguished.** A present entry with
a reserved bit set faults with error RSVD|P (9). A non-present entry ignores all
other bits (error 0 or 2). PS on a PML4E or PDPTE is reserved under the declared
CPU profile. XD is reserved when NXE is clear.
- Enforced by: `check_i3_paging.py:124-125, 132-133`; `i4v_check.py:133-134`;
  `i4v_coherence_model.py:141-146`.
- Grid: I3 reserved, load_np.
- Observable today: guest.
- Kind: architectural, under a declared MAXPHYADDR of 40 and no 1-GiB pages.

**11. 2-MiB guest leaves translate correctly and get their own A/D.** A and D are
set on the PDE leaf and the PT is untouched. Native shadows may be 4-KiB pieces.
- Enforced by: `check_i3_paging.py:136-137, 154-155`;
  `i4v_coherence_model.py:159-163, 177-180`; `check_i4v_coherence.py:141-143`.
- Grid: I3 large; coherence size-pae, size-four.
- Observable today: guest. Leaf size is internal.
- Kind: architectural. 4-KiB native pieces are design.

### C. Accessed and dirty bits

**12. A/D bits are written exactly as the declared walker policy says.**
- The rules:
  - Non-leaf A is set before descending, even when the leaf later faults.
  - Leaf A is set only after the leaf permission check passes.
  - D is set only on a permitted write; reads never set D.
  - No entry outside the walk changes.
  - PAE PDPTEs never get A.
  - Paging-off never touches tables.
- Enforced by:
  - `check_i3_paging.py:120-156, 225-227`;
  - `i4v_check.py:122-158`;
  - `i4v_stack_check.py:156-195`;
  - `check_i4v_stack_boundary.py:179-227`;
  - `check_i4v_address_concurrent.py:179-211, 826`;
  - `i4v_coherence_model.py:128-133, 154-165`;
  - `i4v_pae_model.py:51-78`;
  - `i4v_modes_model.py:91-105`.
- Grid: every paged family.
- Observable today: guest (table bytes).
- Kind: the architectural core is "A when used, D on write, no D on read". The
  fault-path rules are design policy (see section 3, item 7).

**13. A native shadow is never writable while the guest D bit is clear.** A read
fill installs a read-only shadow. The first write returns to the runtime, which
sets D and only then upgrades the shadow. Exactly one native fault per such
upgrade.
- Enforced by:
  - `check_i3_paging.py:229-249` (store permission fault, outcome repaired);
  - `i4v_stack_check.py:198-211`;
  - `i4v_controls_concurrent_check.py:521-540, 604, 608-616`;
  - `check_i4v_coherence.py:493-501` (`leaf & 128 == 0` unless load);
  - `check_i4.py:373, 388-402` (one dirty repair per CPU after remap).
- Grid: I3 store cases, stack, concurrent controls, coherence, I4 remap.
- Observable today: internal (native leaf, fault count). Guest consequence: D appears exactly when written.
- Kind: design (appendix item 3). The D-before-write order is architectural.

**14. A page-crossing store translates each page separately and commits
all-or-nothing.**
- Success writes 4+4 bytes to two physical pages.
- If the second page is not present or read-only, the store raises #PF with
  error 2 or 3, CR2 = first byte of the second page, zero bytes written and RSP
  unchanged.
- First-page A and ancestor A are set. New D is set on neither page.
- On success, D is set on both pages.
- Enforced by: `check_i4v_stack_boundary.py:104-118, 170-176, 179-195, 229-241, 253-268, 363-374, 402-403`.
- Grid: split, split-np, split-ro × N1–4 × every actor.
- Observable today: guest.
- Kind: architectural core, with design policy for the no-partial-write and CR2
  choices (section 3, item 8).

### D. Translation caching and invalidation

**15. A guest invalidation is honored before the next affected access.** This
covers:
- a CR3 write, including a same-value write;
- INVLPG;
- CR0.PG/WP/CD/NW and CR4.PSE/PAE/PGE/SMEP changes.

After an invalidation, the next access walks fresh. A cached clean entry cannot
fabricate a dirty write.
- Enforced by:
  - `i4v_controls_check.py` (profiles 1, 7, 12, 14; `CT:221`);
  - `check_i4v_coherence.py:524-527` (ack drops leaves), `:326-334`;
  - `i4v_coherence_model.py:199-221`.
- Grid: controls; coherence ad-* stage 4–5.
- Observable today: guest (value read, A/D). Generations are internal.
- Kind: architectural.

**16. Guest page-table writes retire dependent shadows eagerly.** A changed PTE
becomes visible without guest INVLPG, under the coordinated mutation path.
- Enforced by:
  - `check_i4.py:360-409` (remap: new data at same VA);
  - `check_i4v_stack_concurrent.py:463, 518-523`;
  - `check_i4v_coherence.py` (parent, size profiles);
  - `i4v_modes_model.py:213-216` (profiles 34, 35);
  - `check_i4v_partial_progress.py:126-135`;
  - `check_i4v_atomic_progress.py:160-166`.
- Grid: I4 remap/compose/pressure, concurrent stack, coherence, controls 34/35, partial, atomic.
- Observable today: guest.
- Kind: design (ABI §2, conservative policy). The SDM permits this but does not
  require it (section 3, item 5).

**17. After A/D are cleared without invalidation, the current expectation is
that a cached translation is reused.** No rewalk happens and A/D stay clear.
- Enforced by: `check_i4v_coherence.py:333, 429, 497-501, 524`.
- Grid: ad-legacy, ad-pae, ad-four × N × actor.
- Observable today: guest (table bytes) and internal (walk count, fault count).
- Kind: design policy that the SDM does not require; flagged in section 3, item 2.

**18. Reloading a parent entry or root, including returning to an old numeric
value (ABA), never revives an old route.**
- Enforced by:
  - `check_i4v_coherence.py:377-379, 493-496, 525-526`;
  - `i4v_controls_check.py` (profile 24, ABA);
  - `i4v_controls_concurrent_check.py:553-559`.
- Grid: coherence parent; controls pae-aba, long-aba.
- Observable today: guest (data lands on the right page). Context identity is internal.
- Kind: architectural outcome; context generation is design.

**19. A 2-MiB ↔ 4-KiB page-size change is honored after invalidation, both
ways, for both subpages.**
- Enforced by: `check_i4v_coherence.py:108-111, 141-143, 380-381, 510, 617`.
- Grid: size-pae, size-four × N × actor.
- Observable today: guest (physical landing). Leaf size is internal.
- Kind: architectural.

**20. Multiple linear aliases of one physical page form addresses exactly, and
each alias carries its own permissions.**
- Low VA X and high VA 0xffff8000…+X resolve to the same GPA, with no truncation.
- A store through an alias whose parent or leaf is read-only faults (#PF error 3,
  CR2 = RSP−8, no D) even though the other alias is writable.
- After a remap, no alias keeps the old route.
- Enforced by: `check_i4v_alias.py:22-23, 32-101, 104-168`;
  `check_i4v_stack_concurrent.py:302-354, 366-397`; `check_i4v_coherence.py:41-43, 335-336, 347`.
- Grid: alias low/high × N; parent/leaf × low/high × N; coherence four-level.
- Observable today: guest (registers, CR2, tables) and internal (native alias leaves, ack routes).
- Kind: architectural. Native leaves are design.

**21. Runtime-private memory is unreachable from the guest.** A guest operand in
the private range gives #GP(0) with no compile request and no native entry. No
guest physical address resolves to private backing. The private bank is never
changed.
- Enforced by: `i4v_check.py` (private_operand), `:233-234`; `run_i4v.py:532`;
  `check_i1.py:106`; `i4v_off_check.py` (control "private backing leak").
- Grid: address high/low; I1 rejects.
- Observable today: guest (vector) and internal (bank, request count).
- Kind: design (ABI §2) with an architectural noncanonical fault.

### E. Modes, segmentation and the physical resolver

**22. With paging off, execution never reads guest page tables, never writes A/D
and never raises #PF.** Linear = physical after segmentation and A20. A native
miss is repaired invisibly. A store is not visible before its retry.
- Enforced by: `i4v_off_check.py:107-116, 176, 180-184`; `i4v_off_steps_check.py:261, 265`;
  `check_i4v_mixed.py:114-125` (off owners never set A/D).
- Grid: off32 6 + 48; off-steps 10; mixed 8.
- Observable today: guest (poisoned tables unchanged, CR2 0xDEAD1000, vector 0).
  Walk counts are internal.
- Kind: architectural (with ABI §2).

**23. Segmented address formation is exact.**
- Linear address = (base + offset) mod 2³². Subpage segment bases work.
- With a 16-bit address size, the offset is truncated to 16 bits before the base
  is added.
- The same offset is used by a store and a following load.
- Enforced by: `i4v_off_steps_check.py:33, 128-133`; `check_i4v_address_concurrent.py:152-176`.
- Grid: segments, linear-wrap, subpage, address16 × N × actor.
- Observable today: guest.
- Kind: architectural.

**24. Segment limits and rights are checked before paging, and the fault is
precise.**
- An access whose last byte exceeds the limit raises #GP(0).
- A write to a read-only data segment raises #GP(0).
- The CS limit applies to the whole instruction span, including a first fetch.
- None of these writes, compiles, sets A/D or changes CR2. The completed prefix
  is kept.
- Enforced by:
  - `i4v_off_steps_check.py:183, 234, 260-267`;
  - `check_i4v_address_concurrent.py:159-176, 214-222, 243-267, 616-637, 860-868`;
  - `i4v_off_steps_worker.py:17-21` / `i4v_address_worker.py:68-69` (CS-limit request admission).
- Grid: ds-limit, ds-readonly, cs-first, cs-before-load, cs-inside-load; ds-boundary,
  cs-boundary × N × actor.
- Observable today: guest.
- Kind: architectural.

**25. With A20 disabled, physical bit 20 is masked after segmentation and
paging, for data and instruction fetch.** Code identity uses the masked physical
address. Page-walk addresses are not masked.
- Enforced by: `i4v_off_steps_check.py:35-36, 96-99, 128-140`;
  `check_i4v_address_concurrent.py:38-39, 115, 220, 460-465`.
- Grid: off-steps a20-off; address a20-off × N × actor.
- Observable today: guest (which physical page is read or written).
- Kind: design/platform (PC-compatible A20 gate).

**26. The physical resolver separates RAM, device endpoints and holes.**
- A device access is serviced once, in order, and never directly mapped or
  retried. A stored device value is retained.
- A hole discards writes and reads all-ones at the access width.
- RAM is untouched by device and hole accesses.
- Enforced by: `i4v_off_steps_check.py:54-61, 186-194, 294-297`.
- Grid: device, hole.
- Observable today: guest (registers, memory) and internal (service records).
- Kind: design (machine map). All-ones open bus is a PC convention.

**27. Paged and paging-off owners can share physical source and data, and each
resolves routes by its own rules.** A source change invalidates both kinds of
owner. A PTE remap changes only paged routes.
- Enforced by: `check_i4v_mixed.py:34-36, 93-94, 114-125, 152-173, 252-263, 296-300`;
  `check_i4v_address_concurrent.py:45-67, 328-338, 824-826`.
- Grid: mixed {off-first, paged-first} × N1–4; address forms × N × actor.
- Observable today: guest (memory, tables). Native leaves are internal.
- Kind: architectural data effects; design ownership.

### F. Control and mode transitions

**28. MOV to CR0/CR3/CR4 either commits fully or faults with no effect.** The
cases are:
- #GP(0) for CR0.PG without PE, NW without CD, a reserved CR4 value, or CPL > 0.
- #UD for a LOCK prefix or CR1.
- A 66 prefix does not truncate the value.
- Ignored CR3 low bits are retained.
- A failed write leaves the context unchanged.

The next guest fetch is redispatched under the new controls.
- Enforced by: `i4v_controls_check.py:128-147` (profiles 0–12),
  `:221`; `i4v_controls_concurrent_check.py:165-193, 684-693, 694-696, 1107-1110`.
- Grid: controls 0–12; concurrent rejected × N × actor.
- Observable today: guest (CR values, vector, RIP, memory). Generation and invalidation counts are internal.
- Kind: architectural.

**29. Legacy PAE hidden PDPTEs behave as guest architectural state.**
- Loading:
  - They are loaded only on a CR3 write, or on a CR0.PG/CD/NW or
    CR4.PSE/PAE/PGE/SMEP change that results in PAE paging.
  - A load reads all four entries, then validates.
  - A present entry with reserved bits raises #GP(0), including an unused slot.
  - A failed load publishes nothing and keeps the previous snapshot.
- They are not reloaded by PDPT memory writes, INVLPG, CR0.WP changes or native
  eviction.
- IA-32e activation never loads them.
- They have no A/D bits.
- Enforced by:
  - `i4v_pae_model.py:10, 35-48, 51-78, 102-195`;
  - `i4v_controls_check.py` (profiles 13–21);
  - `i4v_controls_concurrent_check.py:188, 930-944, 999-1001`;
  - `check_i4v_coherence.py:403-408, 616-625`;
  - `i4v_coherence_model.py:66-75`.
- Grid: controls 13–23; concurrent pae × N × actor; coherence *-pae.
- Observable today: guest (values read through the old or new root, CR values,
  vector). Attempts, reads and hidden generation are internal.
- Kind: architectural (SDM 4.4.1). Read-all-then-validate is design (appendix item 5).

**30. EFER and IA-32e activation follow the SDM rules.**
- WRMSR EFER outside {SCE, LME, LMA, NXE} raises #GP(0). An LME change while
  PG=1 raises #GP(0). LMA writes are ignored.
- Setting CR0.PG with LME requires CR4.PAE, CS.L=0 and a TR that does not
  reference a 16-bit TSS; otherwise #GP(0). On success LMA is set.
- With LMA set:
  - clearing CR4.PAE raises #GP;
  - clearing PG in 64-bit code raises #GP;
  - clearing PG in compatibility mode clears LMA.
- Enforced by: `i4v_modes_model.py:148-163`; `i4v_controls_concurrent_check.py:174-188, 685-688`;
  `i4v_controls_check.py` (profiles 22–29, 36).
- Grid: controls long-*; concurrent long-direct, long-aba × N × actor.
- Observable today: guest.
- Kind: architectural, with vendor-profile choices (section 3, item 15).

**31. Far transfers are descriptor-checked and transactional.**
- JMP ptr16:32 in 64-bit mode raises #UD.
- JMP m16:64 reads a 10-byte pointer through current paging, then the GDT
  descriptor. The check order is:
  - null, TI or limit violation → #GP(sel);
  - S=0 or non-code → #GP(sel);
  - L=D=1 → #GP(sel);
  - not present → #NP(sel);
  - noncanonical target → #GP(0).
- A pointer or descriptor fault preserves the old state and records no read.
- A target-fetch fault keeps the committed transfer.
- The GDT is not written.
- Enforced by: `i4v_modes_model.py:172-202`; `i4v_controls_concurrent_check.py:618-635, 690-693`;
  `i4v_controls_check.py` (profiles 22, 30–38).
- Grid: controls 30–38; concurrent long-direct.
- Observable today: guest (vector, error, CS, RIP). Read records are internal.
- Kind: architectural. Unsupported descriptor forms reject explicitly (design).

**32. A committed control or mode transition changes context identity. Code and
compiler replies for the old context are never entered, even when the numeric
control tuple returns to an old value.**
- Enforced by: `i4v_controls_check.py:221, 389` (stale_reply, stale_entry, rebound reply);
  `i4v_controls_concurrent_check.py:553-559, 1052-1058`; `check_i4v_mixed.py:253-261`.
- Grid: controls roundtrip, pae-aba, long-aba; concurrent families 0, 21, 24.
- Observable today: internal (packets, counters). Guest consequence: the
  correct code runs.
- Kind: design (ABI §4).

**33. A vCPU's local control commit never changes a peer's architectural state,
hidden PDPTEs or routes.** A peer's retained access completes exactly once on its
old route. A peer's still-current compiler reply stays valid and is executed.
- Enforced by: `i4v_controls_concurrent_check.py:83-88, 641-650, 874-923, 1126-1149`.
- Grid: concurrent controls 50 cells.
- Observable today: guest (peer control, CS, RIP, GPR snapshots) and internal
  (context, route, retained ops).
- Kind: architectural core (control registers are per CPU); design protocol.

### G. Code lifetime and self-modifying code

**34. A stale translation is never executed after its source bytes change.** This
holds for writes through another alias, from another mode and from another vCPU.
The replacement executes exactly once and the old fallthrough is not resumed.
- Enforced by:
  - `check_i2.py:64-72, 145-147`;
  - `check_i4.py:303-313, 422-438`;
  - `check_i4v_alias.py:156-166`;
  - `check_i4v_stack_concurrent.py:346-351, 492-513`;
  - `i4v_controls_concurrent_check.py:569-577, 848-851, 945-985`;
  - `check_i4v_address_concurrent.py:677-706`;
  - `check_i4v_mixed.py:253-263`.
- Grid: I2 source_stale; I4 compose; alias, stack, concurrent controls, address, mixed × N.
- Observable today: guest (new result, source leaf D). Masks and counters are internal.
- Kind: architectural (self-modifying code), with a design protocol.

**35. A compiler reply that is stale, mismatched or corrupted is rejected before
any entry. Producer failure never becomes a guest exception.** "Mismatched"
covers source, context, owner, mode or RIP; "corrupted" covers any word or chunk.
On rejection: no native entry, no shadow, code slot blank, guest state unchanged,
earlier instructions kept.
- Enforced by:
  - `check_i2.py:31-137`;
  - `i4v_check.py:186-210`;
  - `i4v_stack_check.py:137-153`;
  - `i4v_off_check.py:52-65, 140-148`;
  - `i4v_off_steps_check.py:45-47, 97-107`;
  - `check_i4v_mixed.py:39-52`;
  - `i4v_controls_check.py:149-158`;
  - `run_i4v.py:520, 533-548, 563, 574-578`.
- Grid: 16/48/48/25 single-bit flips; mixed 96; stale and rebound replies.
- Observable today: internal (status/detail codes, packets).
- Kind: the design core ("no false success; failure is not #PF") survives. Wire
  framing, chunk counts and word layout are producer-transport.

**36. Old code stays physically intact while anything references it.**
- References include frames, saved operations, loaded link targets and pins.
- Reuse is blocked meanwhile.
- Code is reclaimed only by the last releaser, after every release.
- Entry after reclaim is rejected.
- Nothing is leaked at the end.
- Enforced by:
  - `check_i4.py:249-260, 289-298, 316-327`;
  - `check_i4v_stack_boundary.py:52-53, 384, 405, 487-513`;
  - `check_i4v_coherence.py:446-448, 538-554, 582-586, 628-630`;
  - `i4v_controls_concurrent_check.py:971-985, 1087-1105`;
  - `check_i4v_address_concurrent.py:702-711, 818-819`.
- Grid: compose, boundary, coherence, concurrent controls, address × N × actor.
- Observable today: internal (code bytes, ref masks) and trace.
- Kind: design (ABI §6).

**37. Source fetch translation and permission come before code generation.** A
fetch fault (NX, SMEP, CS limit, noncanonical stack precheck) produces no compile
request and no native entry.
- Enforced by: `check_i3_paging.py:179, 208, 218`; `run_i4v.py:532, 573`;
  `i4v_stack_check.py:216, 220`; `check_i4v_address_concurrent.py:617-620`;
  `i4v_off_steps_check.py` (cs-first).
- Grid: fetch_nx, smep, ss_first, ss_span, cs-first, cs-boundary.
- Observable today: internal (request count, events). The fault is guest.
- Kind: design (ABI §2: source translation uses guest fetch permission).

**38. The code cache follows a defined lifecycle.** A miss issues exactly one
request owned by its slot. A hit compiles nothing. A busy slot, cancellation or
exhausted capacity is rejected without changing the pending identity. Host
events never mutate guest packets.
- Enforced by: `check_i2.py:31-137`; worker compile counts (`i4v_*_worker.py`
  `verify_compilations`; e.g. `i4v_address_worker.py:141`); `run_i4v.py:533-538`.
- Grid: I2 6 scenarios; every live family.
- Observable today: internal.
- Kind: design. Request/reply packets and directories are producer-transport.

**39. Publication is complete before eligibility.** Code is blank until a fully
validated reply arrives. Publication is all-or-nothing even when earlier chunks
were valid. The executable mapping is installed and instruction maintenance done
before entry.
- Enforced by: `i4v_check.py:207-210`; `i4v_stack_check.py:148-153`;
  `check_i4v_stack_concurrent.py:497-513` (blank_before_publish); the
  synchronization argument (section 4).
- Grid: live families.
- Observable today: internal and binary.
- Kind: design.

### H. Fault recovery and partial effects

**40. An internal shadow miss is repaired invisibly.** Only the unperformed
access retries, exactly once. CR2 does not change and no #PF is injected. A guest
#PF happens only when the guest tables deny the access.
- Enforced by: `check_i3_paging.py:229-249, 273-274`; `check_i3_pressure.py:532`;
  `i4v_off_check.py:176, 184`; `check_i4v_address_concurrent.py:345-358, 571-591`.
- Grid: every family with native misses.
- Observable today: guest (CR2, vector 0) and internal (ESR class, outcome code).
- Kind: design (ABI §2, §5).

**41. Recovered state at a fault equals the architectural state before the
faulting instruction, even under register pressure.** This covers spilled
values, overwritten destinations and lazily computed flags.
- Enforced by:
  - `check_i3_pressure.py:73-176` (independent ARM interpreter over exported maps; root resolution);
  - `check_i3_paging.py:159-173`;
  - `check_i4v_fence.py:109-123`.
- Grid: i3 pressure/fp/helper × 3; fence.
- Observable today: internal (root maps, spills). Guest consequence: the exception-time registers.
- Kind: architectural outcome. Root maps, recipes and FEX flag representation are
  producer-transport/ABI (appendix item 1).

**42. An admitted access completes on its retained old route before a
conflicting remap is acknowledged.** A later unadmitted access resolves afresh.
Retained peer effects land on the old page.
- Enforced by:
  - `check_i4.py:223-248, 266-302, 360-409`;
  - `check_i4v_coherence.py:424-473, 489-492`;
  - `check_i4v_stack_boundary.py:324, 391-401, 453-479`;
  - `check_i4v_address_concurrent.py:550-557, 663-692`.
- Grid: I4 faults/remap; coherence; boundary; address × N × actor.
- Observable today: guest (data on the old page) and trace.
- Kind: design (ABI T5, §6 step 3).

**43. LOCK XCHG is atomic and is never replayed.** A precommit fault retries
with no effect. A postcommit fault completes the remaining store once. Under a
concurrent remap, the post-exchange store drains on its old route first.
- Enforced by:
  - `check_i4v_ordering.py:177-191, 350-355, 378-390`;
  - `i4v_ordering_model.py:279-310` (token-chain check);
  - `check_i4v_atomic_progress.py:54-185`.
- Grid: xchg, xchg-pre, xchg-post × N1–4 × rotation × seed; progress atomic × N × 4 seeds.
- Observable today: guest (token chain, memory) and internal (fault records, trace).
- Kind: architectural atomicity; design recovery.

**44. REP string operations complete elements in order.**
- RCX/RSI/RDI reflect the completed elements at any interruption.
- RIP stays at the REP until it is done.
- A device or changed-on-read source value is read once and kept across a store
  retry.
- A completed element is not replayed.
- Enforced by: `check_i3_pressure.py:280-341`; `check_i4v_partial_progress.py:15-19, 34-211`;
  `check_i4v_containment.py:200-291`.
- Grid: I3 partial; partial × N1–4 × 2 seeds; containment f5 at (2,0).
- Observable today: guest (registers, memory, PTE) and internal (read/write/retry counts).
- Kind: the register rule is architectural. Read-once is a design guarantee
  stronger than hardware (appendix item 11).

### I. Multi-vCPU coordination

**45. Fault service is serialized.** Exclusive service starts only after every
active access has detached. Queued operations are served FIFO. Each vCPU has at
most one outstanding operation. Nobody waits for service while holding a grant.
- Enforced by:
  - `check_i4.py:161-334`;
  - `check_i4v_progress_protocols.py:40-58, 120-173`;
  - `i4v_controls_concurrent_check.py:854-873`;
  - `check_i4v_coherence.py:419-473`.
- Grid: every concurrent family.
- Observable today: trace.
- Kind: design (ABI §6, T4).

**46. A vCPU acknowledges an invalidation only after its covered memory effects
complete.** Admission reopens only after the complete owner set has
acknowledged. No owner enters between a mutation and its own acknowledgement.
Each acknowledgement resolves that owner's own context.
- Enforced by:
  - `check_i4.py:274-283`;
  - `check_i4_pressure.py:131-138`;
  - `check_i4v_stack_concurrent.py:174-178`;
  - `check_i4v_coherence.py:515-537`;
  - `check_i4v_stack_boundary.py:481-491`;
  - `check_i4v_address_concurrent.py:693-697, 751-755`;
  - `i4v_controls_concurrent_check.py:779-794, 937-944, 961-970`.
- Grid: every concurrent family, delayed last owner at N ≥ 3.
- Observable today: trace and internal (ack records).
- Kind: design (ABI §2 shootdown, §6 step 2).

**47. Ordinary work makes progress under sustained maintenance.** After at most
eight queued heads, a waiting vCPU runs one real generated instruction on the
current route. Turns are round-robin. Every vCPU progresses, with at least
ceil(heads/8) ordinary intervals.
- Enforced by:
  - `check_i4_pressure.py:83, 108-111, 150-151, 175-206`;
  - `check_i4v_progress_protocols.py:46`;
  - `i4v_controls_concurrent_check.py:795-810, 1014-1015`;
  - `check_i4v_address_concurrent.py:764-775, 822`.
- Grid: I4 pressure; concurrent stack, alias, address, concurrent controls; progress stack.
- Observable today: trace. Guest consequence: interval data.
- Kind: design (ABI §6 step 5, T10). The current checker is stricter than the
  ABI (section 3, item 10).

**48. Capacity exhaustion is a declared, state-preserving refusal.** A full
queue rejects without changing any queue word or identity. The rejected
operation is readmitted later under its original identity. No work is admitted
beyond capacity.
- Enforced by: `check_i4v_progress_protocols.py:23-37, 61-117`; `check_i2.py` (capacity).
- Grid: progress stack/controls × N ≥ 2 with reduced pool; I2 capacity.
- Observable today: internal (queue digests, result 131).
- Kind: design (ABI §8).

**49. Events stay pending and are delivered only at eligible boundaries.**
- A pending event request is durable; repeated notifications coalesce into one.
- It is delivered only at an eligible guest boundary, honoring IF, interrupt
  shadow and priority.
- It survives helpers, drains and translation waits; a wait rechecks before
  sleeping.
- There is exactly one handoff, and the packet is the canonical committed state.
- Guest IF never suppresses coordination.
- Enforced by:
  - `check_i4.py:185-213, 441-486`;
  - `check_i4v_stack_concurrent.py:279-299`;
  - `i4v_controls_concurrent_check.py:725-737, 816-829, 1119-1121`;
  - `check_i4v_address_concurrent.py:716-728, 919-925`;
  - `check_i4v_partial_progress.py:150-152`;
  - `check_i4v_atomic_progress.py:122-126`;
  - `check_i4v_mixed.py:98-111`.
- Grid: I4 events; concurrent families.
- Observable today: guest (packet) and internal (blocked masks, token state).
- Kind: architectural boundary/IF/shadow concepts; design encoding. It is not
  IDT/APIC delivery.

**50. Guest-observable memory ordering stays within x86-TSO.**
- Allowed outcomes:
  - SB allows all four outcomes.
  - MP forbids (1,0).
  - LB forbids (1,1).
  - WRC forbids (1,1,0).
  - IRIW forbids (1,0,1,0).
- MFENCE and LOCK XCHG forbid SB (0,0).
- XCHG chains are linearizable.
- Enforced by: `i4v_ordering_model.py:45-76, 147-223, 246-276`;
  `check_i4v_ordering.py:159-174, 346-349, 394-395`.
- Grid: 154 ordering cells, 32 trials each.
- Observable today: guest.
- Kind: architectural (soundness only; section 4).

**51. Fences complete without losing live state.** LFENCE retains GPR, vector,
deferred-flag and sticky-FP roots across a complete capture, completion barrier
and exact restore. Raw LFENCE/SFENCE/MFENCE preserve all state. None of these
cases is a fence-ordering claim.
- Enforced by: `check_i4v_fence.py:109-128, 148-154, 190-225`;
  `check_i4v_ordering.py:155, 381, 396-399`.
- Grid: fence 5 × N1–4; ordering lfence/sfence/mfence × N1–4.
- Observable today: guest (output block, MXCSR) and internal (sink frame).
- Kind: architectural outcome; design completion mechanism.

**52. Native execution runs under the admitted host profile.** That profile is:
- hardware TSO enabled;
- EL1 with SCTLR M/C/I set and alignment checking off;
- the declared TCR/TTBR1;
- required CPU features, and no use of SVE/RCPC.
- Enforced by:
  - `i4v_check.py:23, 224-230`;
  - `check_i4v_ordering.py:324, 336-338, 382`;
  - `check_i4v_fence.py:211-212`;
  - `check_i4v_stack_concurrent.py:328-330`;
  - `i4v_controls_concurrent_check.py:1051`;
  - `check_i4v_coherence.py:634-635`;
  - `check_i4v_containment.py:41-52`;
  - `i4v_producer/export.py:234` (feature line).
- Grid: every native cell.
- Observable today: internal.
- Kind: design (host admission, appendix item 12).

**53. Seeded schedules are deterministic and replay exactly offline.** Each
choice is the declared PRNG draw over the trace-derived eligible set. Every
grant is consumed by the next real operation. There are no unconsumed grants.
- Enforced by: `check_i4v_schedule.py:22-26, 29-106`; `check_i4v_atomic_progress.py:195-223`.
- Grid: 72 progress cells; containment prefixes.
- Observable today: trace and internal (decision ledger).
- Kind: design (a test mechanism). Section 3.6 of the plan keeps schedule points.

**54. The vCPU count is admitted, and per-vCPU state is isolated.**
- The admitted N range is accepted; others are rejected with a diagnostic.
- Every participant starts, finishes and is joined.
- Per-vCPU data, banks and registers never leak across vCPUs.
- Enforced by: `check_i4.py:62-114`; `run_i1.py:249-251, 258-260`;
  `check_i4v_schedule.py:41-49`.
- Grid: N=0..5.
- Observable today: guest (per-CPU results) and host.
- Kind: design (ABI §6, configured N).

### J. Admission, failure and containment

**55. A malformed host submission or object is rejected before any effect.** This
covers version, profile, size, target, entry, code, address and vCPU. Guest
memory and the bank are unchanged.
- Enforced by: `check_i1.py:99-110`; `run_i1.py:240-248`.
- Grid: 8 mutations.
- Observable today: guest memory and internal status.
- Kind: design (ABI §3: mismatched version/profile rejects before entry).

**56. An unexpected native exception inside translated code traps without side
effects.** It does not become a guessed guest fault.
- Enforced by: `check_i1.py:157-164`.
- Grid: I1 fault.
- Observable today: internal (trap packet).
- Kind: design (ABI §3: unknown synchronous fault PCs contain the VM).

**57. A failure becomes contained machine failure with the original cause
preserved, within bounds.**
- The failure kinds are:
  - producer worker exit;
  - malformed reply;
  - resident panic while holding the coordinator lock;
  - withheld acknowledgement;
  - cancellation with deferred work.
- Detection takes at most 3 s from the gate and joined stop at most 10 s.
- All owners are joined, every vCPU is destroyed, and the VM is destroyed after
  the joins.
- A cancelled owner is never reported as successful.
- Enforced by: `check_i4v_containment.py:19-23, 326-352, 355-383`;
  `experiments/track01/run_check.py:161-170` (outer gate watchdog).
- Grid: 25 cells.
- Observable today: host and internal (witness).
- Kind: design. Worker exit, malformed reply and the Docker form of cancellation
  are producer-transport. Panic, withheld acknowledgement and the bounds survive.

**58. Failure never fabricates completion.**
- The machine state at failure equals the replay of the trace up to the failure
  gate.
- In-flight instructions have no after-state.
- No cancelled release or reclamation is performed.
- A held lock or reference stays visibly held, not repaired.
- Enforced by: `check_i4v_containment.py:36-37, 55-197, 200-291, 370-382`.
- Grid: 25 cells.
- Observable today: guest (memory, tables, GPR images) and internal (witness, ledger).
- Kind: design.

**59. A fresh machine after a failure is healthy.** A separate host process with
the same configuration passes the full healthy checks. It gets a new VM nonce and
no shared cancellation state.
- Enforced by: `check_i4v_containment.py:512-529`; `run_i4v.py:319-327`.
- Grid: 25 controls.
- Observable today: host.
- Kind: design.

**60. Owned compiler processes and containers are always cleaned up.**
- Each is registered before spawn or create, with a unique token.
- A cancelled job never runs its payload.
- Only owned containers are removed, by immutable id; a foreign container is
  never touched.
- The process group is gone and reaped.
- An unacknowledged create fails closed.
- Enforced by: `i4v_producer/ownership.py:24-302`; `check_i4v_containment.py:393-509`;
  `run_i4v.py:779-816`; `run_i1.py:330-343`; `fex_worker.py:106, 119-129`.
- Grid: every run.
- Observable today: host.
- Kind: producer-transport. An in-process producer needs the residual contract
  "cancelled or failed generation leaves no work running after teardown".

### K. Binary and producer interface

**61. The runtime gates have the declared shape.**
- Every GPR, every Q register, FPCR, FPSR, NZCV and SP_EL0 are saved before any
  scratch use.
- Completion uses DSB SY + ISB.
- Restore is a full ERET.
- The vectors are 16 identical branches to capture.
- The boot sequence and literals are declared.
- The partial store is `STRB; RET`.
- The handler has no indirect or unexpected branches.
- Enforced by: `check_i4v_fence_gate.py:39-64, 72-265`; `run_i4v.py:26-33, 298-299, 385`
  (cells bound to the audited gate addresses).
- Grid: the produced runtime ELF.
- Observable today: binary.
- Kind: design (ABI §3, §5).

**62. The emitted resident stack depth fits the reserve.** The recomputed figure
is 15,040 of 16,384 bytes for the accepted ELF.
- Enforced by: reviewer audit recorded in the coverage matrix ("Native
  synchronization…" section). It is not an automated checker.
- Observable today: binary.
- Kind: design (ABI §8).

**63. Code generation reads only the bytes it was given.**
- Requests are bounded: 1–64 bytes, 1–32 instructions, canonical or 32-bit RIP.
- An instruction straddling the supplied window is rejected, not compiled.
- Unsupported input is rejected, never silently compiled.
- Enforced by: `i4v_producer/adapt.py:15-52, 69-89`; `i4v_producer/compile.py:23-25, 67-68`;
  `i4v_producer/export.py:226`.
- Grid: every compile.
- Observable today: host artifacts.
- Kind: design (plan §3.3 rejection rule). The current form is FEX-specific.

**64. Every faultable native instruction has site metadata, and the metadata is
truthful.** The metadata gives guest instruction, access kind and width, address
form, continuation and phase. The structural admitter rejects unmapped native
memory accesses and unknown opcodes.
- Enforced by:
  - `i3_producer/parser.py:114-123, 137-164, 184-231`;
  - `i3_worker.py:53-87`;
  - `i4v_producer/export.py:254-259` (pinned annotations);
  - `i4v_producer/compile.py:83-84` (range checks).
- Grid: every compile.
- Observable today: binary.
- Kind: design (plan §3.3 object contract); FEX encodings are producer-transport.

## 2. Internal-only assertions and their replacements

The plan admits four replacement forms: a guest-visible outcome, a contract
snapshot at a quiescent point, an ABI-stated trace property, or a binary audit.
The snapshot covers shadow leaves per context, eligible objects and their sources,
and retained references. Otherwise an assertion is removed with a reason. Groups
below cover all families; "every family" means the same field appears in each
checker listed in section 1.

| Internal assertion (examples) | Contract | Replacement |
| --- | --- | --- |
| Walk, read, retry and repair counts: `check_i3_paging.py:36-49, 224`; `i4v_check.py:251`; `i4v_stack_check.py:307`; `i4v_controls_check.py:187, 389`; `i4v_controls_concurrent_check.py:157, 1053, 1059-1061`; `i4v_pae_model.py:59-77`; `check_i4v_coherence.py:429, 467, 611-633`; `check_i4v_address_concurrent.py:887`; `check_i4v_mixed.py:285`; `check_i4v_partial_progress.py:179-183`; `check_i4v_ordering.py:324` | 12, 15, 17, 40 | **Remove as expectations.** The SDM does not observe walk counts, and they forbid a TLB. Guest table bytes, CR2 and values already cover A/D and freshness. Keep one ABI trace property: no walk is attributed to a hit. Keep a planted break for "skip invalidation". |
| PDPTE load attempts and reads (`i4v_pae_model.py`, `i4v_controls_check.py` profiles 13–21, `check_i4v_coherence.py:616-625`) | 29 | Guest outcome: after a PDPT memory write, a load through the hidden root still reads old-root data; after a qualifying control write it reads new-root data. Keep read-all-then-validate as policy, observable as no partial publication on #GP. |
| Native shadow leaf descriptors, upper roots and alias leaves: `i4v_check.py:274-285`; `i4v_stack_check.py:198-211, 332-341`; `i4v_off_check.py:203-212`; `check_i4v_alias.py:104-168`; `check_i4v_stack_concurrent.py:302-354`; `i4v_controls_concurrent_check.py:1081-1086`; `check_i4v_coherence.py:223-231, 617`; `check_i4v_stack_boundary.py:377-382`; `check_i4v_address_concurrent.py:931-936` | 13, 20, 21, 26 | **Contract snapshot** with a refinement check: every shadow leaf is no wider than a translation the model permits (writable only if guest D=1 and effective W; device and hole pages never mapped; no leaf reaches a page the guest mapping no longer reaches). Drop exact descriptor values, table addresses and epochs. |
| Native fault records (ESR EC/DFSC/WnR, FAR, ELR, SPSR) on every family, e.g. `check_i3_paging.py:229-249`; `check_i4v_ordering.py:177-191`; `check_i4v_partial_progress.py:49-65`; `i4v_controls_check.py:401-416` | 40, 43, 44 | **ABI trace property**: each internal repair retries exactly the registered site once; a precommit fault leaves pre-instruction state; a postcommit fault never repeats. Drop host syndrome encodings. |
| Resident code addresses and gate PCs: `check_i1.py:33-42, 115-119`; `run_i4v.py:385`; `check_i4v_containment.py:41-52`; `bind_partial_store` `run_i4v.py:26-33` | 61 | **Binary audit tied to symbols**; cells reference symbols, not addresses. |
| Published code bytes and code slot images: `check_i4.py:425-430`; `i4v_check.py:207-210`; `check_i4v_ordering.py:326-333`; `check_i4v_fence.py:148-154`; `check_i4v_stack_boundary.py:52-53, 384, 405`; `check_i4v_stack_concurrent.py:30-59` | 36, 39, 64 | **Snapshot**: eligible objects' sources match guest memory; retained objects are intact while referenced; reclaimed slots are not eligible. **Producer audit**: barrier and site presence checked by independent disassembly of emitted code. Drop byte equality with pinned FEX bodies. |
| Visit and request counts, request/reply packets, `compiler_requests == requests`: `check_i2.py:31-137`; every live family; `run_i4v.py:366-368, 386-389, 405-409, 429-438, 485-500, 532-538, 573-582` | 37, 38, 35 | **Trace property**: one generation request per (context, RIP, mode, source generation) miss; none before a successful fetch translation; none on a hit. Guest outcome: a fetch fault leaves no code entered. Chunk counts and packet framing are removable (producer-transport). |
| Reply-word mutation grids (16/48/48/25/96 flips) | 35 | Replace with **failure-site injection** at the producer interface: corrupt object metadata, identity or code before admission, and expect structural rejection with no entry. A flip inside a structurally valid instruction cannot be caught by admission alone; leave it to producer-output audit, not a runtime expectation. |
| Coordinator fields: lock/next_ticket, queue ring, generation, published, servicing, `event_pending`, `event_notifications`: `check_i4.py:405-409`; `check_i4v_coherence.py:598-605`; `i4v_controls_concurrent_check.py:1136-1146`; `check_i4v_address_concurrent.py:828-865`; `check_i4v_stack_boundary.py:514-526`; `check_i4v_atomic_progress.py:186-191` | 45, 46 | **Snapshot** at quiescence: no active grant, empty queue, all references released, lock released. Ordering legality stays as **trace properties**: FIFO, exclusive after drain, acknowledgement after covered effects, reopen after full set. Drop Rust-layout values. |
| Retained-reference masks, `retained_code`, `reuse_blocked`, `released`, `reclaimed`, `stale_entries`: `check_i4.py:422-438`; `check_i4v_stack_concurrent.py:492-513`; `i4v_controls_concurrent_check.py:1087-1105`; `check_i4v_coherence.py:446-448, 538-554` | 34, 36 | **ABI trace property**: retire → last release → reuse order; no entry of an ineligible object. **Snapshot** of retained references. Force reuse by declaring a small code-cache capacity (plan §4.1). |
| Maintenance scalars: serial, depth, epoch, batch, `next_ordinary`, `interval_counts`, `request_history`, duplicate rejections: `check_i4_pressure.py:19-172`; `check_i4v_stack_concurrent.py:108-230`; `check_i4v_progress_protocols.py:23-117` | 47, 48 | **Trace property** stated in the ABI: at most eight heads between ordinary admissions; one outstanding operation per vCPU; FIFO head order. Keep `leaf_history` as guest table bytes. Drop the nine-request quota, the FNV digests and result code 131; replace the latter with a typed refusal reason. |
| Event token state, blocked bitmask history, `wait_checks`, boundary enum: `check_i4.py:472-485`; `check_i4v_stack_concurrent.py:279-299`; `i4v_controls_concurrent_check.py:725-737, 1119-1121`; `check_i4v_address_concurrent.py:919-925` | 49 | **Guest outcome**: delivered packet state and boundary. **Trace property**: exactly one delivery per pending request, never while a listed blocker holds. Drop the bit encoding. |
| Helper clobber witness: `check_i3_pressure.py:365-377`; fence sink frame `check_i4v_fence.py:217` | 5 | **Guest outcome**: run a helper-calling program with sentinels in every GPR, full YMM and MXCSR, and compare the final state. **Binary audit** of gateway save/restore. |
| Root maps, spill witnesses, ARM interpreter over exported bodies: `check_i3_pressure.py:73-176`; `i3_producer/parser.py:148-231` | 41, 64 | **Guest outcome**: exception-time registers under pressure compared with the model. **Producer-output audit** for map truthfulness by independent disassembly. The FEX representation (inverted carry, x26/x27) moves behind reconstruction recipes. |
| Host system registers (SCTLR, ACTLR, TCR, CurrentEL, ID_AA64ISAR0): `check_i4v_ordering.py:336-338` and the rest under contract 52 | 52 | **Host admission check** reported once per machine (contract snapshot); not repeated per cell. Relax ISAR0 to "LSE present" (section 3, item 13). |
| Scheduler decision ledger: `check_i4v_schedule.py:64-105` | 53 | Keep as a **trace property** of the schedule-plan mechanism (plan §3.6); plans name contract events, not internal counts. |
| Containment witness fields, `trace_length`, `next_ticket > lock`: `check_i4v_containment.py:36-37, 113-116, 181-195, 365-382` | 57, 58 | **Machine stop report**: stop reason as a typed enum (not text); guest registers and requested memory; trace prefix property "no effect after the failure point". Drop Rust fields. |
| `bank_before == bank_after` | 2, 21 | Keep as a **snapshot** item: private regions unchanged. |
| Producer provenance: Docker flags, image ids, FEX log markers, `worker.json`, delivered-file equality: `fex_worker.py`, `i3_worker.py:20-21, 114-126, 185-240`; `i4v_producer/export.py:220-253`; `compile.py:28-31, 65-78` | 60, 63 | Producer-transport. **Removable** if generation moves in-process. Replace with a producer build id in the trace. |
| Harness identity, schema key sets, bool-as-int strictness, delayed-owner timing (`delayed_owner_ms == 50`) | — | Harness hygiene, not emulator contracts. Keep in the new runner; not counted. |

## 3. Suspected wrong expectations

Each item gives evidence and why the old emulator passed anyway. Per plan §4.3, a
fix needs an SDM/APM or ABI citation, an independent model or QEMU result,
reviewer agreement and a logged behavior change. These are candidates, not
decisions.

### Contradicts the architecture

1. **Initial CR0 with ET=0.** The seed states use CR0 = 0x80010001 (`i4v_check.py:89`,
   the `check_i3_paging.guest_seed` seed) or 0x10001 (`i4v_off_check.py:75`). The
   checkers require `guest_after.control[0]` to equal that seed. SDM Vol 3 §2.5
   says CR0.ET is hard-coded to 1 on P6 and later processors, so no conforming
   CPU reports ET=0. The control programs themselves write ET=1 (0x10011,
   0x80010011, `i4v_controls_check.py:43-55`). Why it passed: the emulator copies
   the seed verbatim. Fix: seed with ET=1, or declare ET normalization at machine
   admission and expect the normalized value.

### Over-specified: the SDM permits alternatives the checker rejects

These do not contradict the SDM. They lock in one legal behavior and present it
as required. Each must be either relabeled as design policy in the ABI (and
compared with QEMU where QEMU defines it) or relaxed to accept every legal
outcome.

2. **Cached history required after clearing A/D without invalidation**
   (`check_i4v_coherence.py:333, 429, 497-501, 524`). SDM Vol 3 §4.10.2–4.10.4
   allows a cached translation to be used without setting A/D again. It equally
   allows the entry to have been evicted, so a fresh walk sets A/D. The checker's
   own model says "both a legitimate cached hit and a fresh/internal walk are
   permitted" (`i4v_coherence_model.py:6-7`, and its
   `test_cleared_ad_without_invalidation_permits_both_histories`). The checker
   docstring says "No universal cache-policy claim" (`check_i4v_coherence.py:561`).
   Why it passed: the old emulator retains native shadows until an
   acknowledgement. Suggested fix: accept either history, as long as the walk
   record and the A/D bytes agree. `i4v_coherence_model.check_observation`
   exists for this and is unused by the checker.

3. **No TLB may survive a PTE change without INVLPG** (`i4v_modes_model.py:213-216`,
   controls profiles 34 and 35). Both programs clear a PTE in memory with no
   INVLPG and require the next access to the same page to #PF. A far jump does
   not flush the TLB, and the SDM allows the old entry to be used. The walk-count
   equalities (`i4v_controls_check.py:187, 196-206, 389`) and the
   no-fault-on-stage-5 totals (`check_i4v_coherence.py:633`) have the same effect.
   Why it passed: the old emulator walks on every fetch and retires routes on
   every table write.

4. **The leaf A bit stays clear on a protection- or NX-faulting access, while
   parents get A.** Sites: `check_i3_paging.py:148-153`, `i4v_check.py:122-158`,
   `i4v_stack_check.py:184-195`, `check_i4v_stack_boundary.py:217-218`,
   `check_i4v_address_concurrent.py:203-204`, `i4v_coherence_model.py:154-165`.
   SDM §4.8 does not say whether a faulting access sets a leaf's A bit. This is
   appendix item 2, and the coherence model calls it "the selected permitted
   policy". It should be stated as policy, not as SDM behavior.

5. **Eager A sets by the emulator's own remap acknowledgements**
   (`check_i4v_stack_boundary.py:393-401`, `check_i4v_address_concurrent.py:609-611`;
   final tables compared byte-exact). Processors may set A speculatively, so this
   is legal, but it forbids an emulator that does not re-walk on acknowledgement.

6. **The eager page-table tracking in contract 16** is legal (a TLB may drop
   entries early) but is not required. It is ABI §2 policy, and tests should say
   so.

7. **Exact walk counts and orders** (contract 12 fault-path rules, and section 2,
   first row). These are policy, not architecture.

8. **A split store commits nothing and reports CR2 = start of the second page**
   (`check_i4v_stack_boundary.py:166-176, 363-374`). This matches observed Intel
   and AMD behavior. The SDM states only "the linear address that caused the
   fault" and gives restartability. It is policy (appendix item 4) to compare with
   QEMU.

9. **REP: device value read once, destination route acquired before the second
   source sample, event delivered only after the whole REP completes**
   (`check_i3_pressure.py:298-302`; `check_i4v_partial_progress.py:93-96, 176-186`).
   - Read-once is stronger than hardware, which re-executes a faulted iteration.
     It is a deliberate ABI guarantee (§1, T8).
   - Delivering after completion is legal. SDM Vol 1 §7.3.9.3 permits, but does
     not require, recognition between iterations.
   - The ABI's "interrupt only at its legal point" (T8) and the one-iteration work
     bound (§6) do not pin delivery after completion. A rebuild that delivers
     between iterations, with RIP at the REP and RCX remaining, would be
     SDM-faithful and would fail these checks.
   - No guest-visible #PF on a MOVS destination is exercised at all. The REP
     faults here are internal shadow misses.

10. **The fairness rotation is stricter than the ABI.** `check_i4_pressure.py:110`
    requires the ordinary owner to be `ordinary_count % n` whether or not that
    vCPU is waiting. `check_i4v_progress_protocols.py:46` reserves that vCPU and
    blocks its own submission near the bound. ABI §6 step 5 says only "admit a
    waiting ordinary vCPU round-robin". An implementation that skips idle vCPUs
    conforms to the ABI and would fail.

11. **The positive N ≥ 2 progress claims run with a queue pool smaller than N**
    (`check_i4v_schedule.py:19`; `check_i4v_progress_protocols.py:110-116`). ABI §6
    and §8 require one operation slot per vCPU and "do not omit cases to fit a
    pool". That profile is right for the failure path, but the healthy-progress
    contract also needs cells at the full per-vCPU reserve.

12. **Policy choices under a declared CPU profile that the machine description
    must state.**
    - `check_i3_paging.py:133` uses reserved bit 40, which needs MAXPHYADDR = 40.
    - `i4v_coherence_model.py:144-145` treats PDPTE PS as reserved, which means no
      1-GiB pages.
    - `i4v_coherence_model.py:150, 153` treats PAT=1 as non-write-back. This is
      conservative: with the default PAT, entry 4 is write-back.

13. **Host feature check too strict** (`check_i4v_ordering.py:338`). It requires
    ID_AA64ISAR0.Atomic == 2 exactly, for every profile. Hosts with LSE128 report
    3. The ABI requires "LSE", not this exact field value.

### Model gaps that would assert wrongly if a case reached them

No executed case asserts these today. Fix them before the models are reused.

14. **Latent errors in the controls model.**
    - `i4v_controls_check.py:135` returns #UD for MOV to CR2 (legal at CPL0) and
      for CR8 (legal in 64-bit mode).
    - `:137-140` accepts reserved CR0 bits. SDM: #GP(0) on writing 1 to a reserved
      CR0 bit.
    - `:141` performs no CR3 reserved-bit check.
    - `:143-144` rejects only the single CR4 value 0xFFFFFFFF.

15. **WRMSR EFER and the activation rules in the modes model.**
    - `i4v_modes_model.py:148` ignores EDX. EFER bits 63:32 are reserved, so a
      nonzero EDX must raise #GP.
    - The reserved set ~0xD01 is the Intel layout; AMD defines SVME, LMSLE, FFXSR
      and TCE.
    - The LMA-ignore and 16-bit-TSS activation rules are vendor-profile choices.
    - Declare the vendor profile.

16. **Checker weaknesses (checker controls, not expectations).**
    - `check_i1.py:74-79` `equal()` uses `zip` with no length check.
    - `i4v_controls_check.py:413` and `i4v_controls_concurrent_check.py:1075`
      copy the observed exception tuple into the expected one after partial
      checks, so unchecked ESR fields are unconstrained.
    - The `check_i3_paging.py:243-244` comment says EA, but the mask selects CM
      (bit 8); EA (bit 9) is unchecked there.

### Checked and found consistent

- I/D only with NXE or SMEP. Error codes 0/2/3/5/7/9/17.
- #SS for SS-based and #GP for DS-based noncanonical accesses.
- The PDPTE reload trigger set and reserved mask (`i4v_pae_model.py:10, 35-40`).
- The PAE PDE/PTE reserved mask, bits 62:40 (`i4v_coherence_model.py:143`).
  SDM Tables 4-8 to 4-11 mark bits 62:MAXPHYADDR reserved in PAE paging. One
  extraction flagged this mask as wrong; it is correct.
- The far-jump check order, L=D=1 #GP before #NP and canonical target after
  present, which matches the SDM JMP pseudocode for nonconforming code segments.
- LOCK MOV CR #UD (Intel). The 32-bit linear wrap and the 16-bit offset
  truncation.
- The x86-TSO allowed sets in `i4v_ordering_model.py`, including IRIW and WRC
  forbidden. LFENCE and SFENCE are never treated as store→load fences.
- The litmus checkers never require a relaxed outcome to be observed.

## 4. Non-case obligations

### Synchronization, publication and lifetime argument

These are the seven premises of the accepted argument
([matrix](../research/reviews/INV-03-01-i4v-coverage.md#native-synchronization-publication-and-lifetime-argument)).
They must be re-established against the new `coord`, `code_cache` and `memory`
sources. Guest litmus results cannot prove any of them.

1. Authority handoff: ticket acquire with an acquire load and release with a
   release increment. Mutable shared state changes only under that authority.
   Rendezvous fields outside the lock use declared release/acquire pairs.
2. No wait-induced cycle: scheduler refusal returns immediately. Compiler waits
   and generated execution happen outside the lock.
3. Closed admission before mutation: OPEN → CLOSING → EXCLUSIVE drains saved
   operations before an incompatible change. Atomic and partial operations keep
   only their remaining effect.
4. Acknowledgement is completed work: break-before-make, then
   DSB/TLBI/DSB/ISB on the owning CPU. Reopening requires the full owner set.
   Oracles also inspect walks and later accesses, not just acknowledgement bits.
5. Publication precedes eligibility: validate identity under authority, write
   through the non-executable alias, DC/IC and barriers, install the executable
   mapping, then make the object eligible. Shared-code consumers perform local
   instruction synchronization.
6. Entry and reclamation use different conditions: a source or context change
   disables entry immediately. Code is freed only when every reference is gone.
7. Events and failure witnesses: durable pending request; release-published
   witness; original cause survives cleanup; joined stop precedes VM destruction.

Planted breaks from plan §4.4 cover each premise. The argument is extended
whenever a new writer or observer is added: DMA, debugger, devices, shared
contexts under SMP (plan §3.2).

### Containment bounds and cleanup

- Detection at most 3,000 ms and joined stop at most 10,000 ms from the
  observed failure gate. Measured maximum: 2,830 ms for both.
- Successful outer gate-relative supervision covers host and VM teardown.
- Before the fresh control starts, every owner thread is joined, every vCPU and
  the VM destroyed, every owned child reaped and every owned container removed.
- N3 containment is untested.
- An abandoned lock or reference is preserved as failed state.
- The bounds are runner policy, not ABI text. Derive them from measurement on the
  new image (plan §5 step 0) and state them.

### Resident stack bound

The conservative emitted stack-depth audit is redone for every new runtime binary
against the declared per-vCPU reserve. It is not inherited from the old ELF.

### Checker corruption controls

Every checker carries self-tests that prove it rejects corrupted evidence.
Approximate counts:

| Checker | Approximate corruptions |
| --- | --- |
| `check_i1` | 19 |
| `check_i2` | 18 |
| `check_i3_paging` | about 105 |
| `check_i3_pressure` | about 47 |
| `check_i4` | about 92 |
| `check_i4_pressure` | about 21 |
| `i4v_check` | about 36 + 32 word flips |
| `i4v_stack_check` | 34 + 96 |
| `i4v_off_check` | 36 + 96 |
| `i4v_off_steps_check` | 38 + 144 |
| `check_i4v_mixed` | 23 + 96 |
| `i4v_controls_check` | about 100 + 16 |
| `i4v_controls_concurrent_check` | about 40 |
| `check_i4v_coherence` | about 100 |
| `check_i4v_alias` | about 40 |
| `check_i4v_stack_concurrent` | about 150 |
| `check_i4v_stack_boundary` | about 60 |
| `check_i4v_address_concurrent` | about 120 |
| `check_i4v_ordering` | about 60 |
| `i4v_ordering_model` | about 20 |
| `check_i4v_fence` | about 80 |
| `check_i4v_fence_gate` | about 150 |
| `check_i4v_atomic_progress` | about 40 |
| `check_i4v_schedule` | 15 + 4 |
| `check_i4v_progress_protocols` | about 30 |
| `check_i4v_partial_progress` | about 160 |
| `check_i4v_containment` | about 70 |
| `i4v_qemu/test_model.py` | 36 byte flips + 64 RFLAGS bits + 5 projection |

The converted checks need equivalent controls written by their separate authors.
Known control gaps to close:

- the items in section 3, item 16;
- `check_i4v_coherence.py` does not use the model's `check_observation`;
- retained goldens in `tools/testdata/*.json.gz` are emulator-captured
  ("development regression fixture; not independent acceptance") and must not
  serve as expected values;
- the ds-boundary address profile has only a synthetic fixture
  (`test_check_i4v_address_concurrent.py:906-935`);
- stack-family actor rotation is selected only in unit tests (`run_i4v.py:767`
  binds compiler owner 0).

### Fresh rebuild reproduction

- A serialized rebuild from captured sources: `run_i4v.py:140-203` snapshots the
  inputs and rejects symlinks.
- Pinned toolchain `rustc 1.97.0`, `--offline --locked`, a pinned producer
  SHA-256, and host signing with the entitlement.
- The same runtime ELF hash across replicas.
- Every artifact checksummed.
- Limits: build 2 jobs / 8 GiB; worker 512 MiB; 120 s per cell; 1,200 s per
  bundle; 2 GiB per I4-V run and 1 GiB per legacy run.
- Counts separate distinct, repeated-baseline and control executions.
- Failed developmental runs are retained and never relabeled.
- Recorded-decision native playback is unimplemented and unclaimed. Only offline
  replay is.

### QEMU comparison scope

- **Programs** (`i4v_qemu/model.py:6-8`):
  - `load` `488b03`;
  - `stack` `68bbbcbdbe 6668 5251 6668 6261 488b42f8 8b5af4`;
  - `xchg` `87038902`;
  - `lfence` `0faee8`, `sfence` `0faef8`, `mfence` `0faef0`.
- **Resident sources** (`run_i4v_qemu.py:46-51`): live-low-low_operand,
  stack-low-low_stack, and `ordering-{xchg,lfence,sfence,mfence}-n1-rotation0-seed1`.
- **QEMU**: pinned Debian `qemu-user-static 1:7.2+dfsg-7+deb12u18+b3 arm64`, the
  7.2 line recorded as 7.2.22 in the matrix, with all five artifact hashes pinned.
  It runs `-cpu qemu64` in Linux-user mode, under NASM 2.16.03, in a pinned
  Docker image with no network, a read-only filesystem and resource caps.
- **Compared to the independent model**:
  - all 16 GPRs;
  - full 64-bit RFLAGS, equal to 0x202, with no mask;
  - RIP = start + length;
  - all 8,192 memory bytes.
- **What it does not cover**:
  - The resident result is compared to the same model, not directly to QEMU.
  - Ordering cells compare eight resident GPR roots and only native NZCV
    preservation; R8–R15 are QEMU-only canaries.
  - None of the six programs computes arithmetic flags, so "full RFLAGS" is a
    preservation check.
  - No paging, privileged, fault-retry, concurrency or ordering claim.
- **For the new suite**: label policy items from the plan appendix and compare
  them with QEMU wherever QEMU defines the outcome. Candidates: ADD flag results,
  split-store CR2, the REP register state on a guest #PF, the segment and A20
  results under QEMU system mode. That is new scope; this document does not
  decide it.

### Not emulator contracts

- `check_setup.py` checks host prerequisites only.
- `experiments/track03/containment/` is a disposable macOS sandbox and
  entitlement probe. No checker enforces it, and it makes no emulator claim.

## 5. Independent models available

| Model | Independence | Reuse |
| --- | --- | --- |
| `i4v_ordering_model.py` | stdlib only; states "no runtime, producer, or implementation-model imports" | Reusable as-is for litmus outcome sets and XCHG chains. |
| `i4v_coherence_model.py` | dataclasses/typing only; own walker, cache and fixtures | Reusable. Its fixture topology (frame addresses) needs parameterizing for new machine descriptions. Use `check_observation` for either-history checking. |
| `i4v_pae_model.py` | no imports | Reusable for PDPTE load, reload and walk. It embeds the 40-bit reserved mask and the one-shot handler-skip convention (`:176-179`). Separate those as declared profile inputs. |
| `i4v_modes_model.py` | imports only `i4v_pae_model` | Reusable after removing the fixture-specific PTE mutations (`:213-216`) and fixing section 3, items 14–15. |
| `fixtures/i4v/expectations.py` | hand-written x86 effects, no emulator imports | Reusable for the stack and paging-off programs. |
| `i4v_qemu/model.py` | states "no resident/runtime implementation imports" | Reusable as-is for the six scalar programs. |
| Function-level references: `check_i1.add_flags` (`:82-93`); the binary32 RNE adder in `check_i4v_fence.py:48-69`; `i4v_controls_check.translate` (2-level); the walkers and segment formation in `check_i4v_address_concurrent.py:152-267` and `check_i4v_stack_boundary.py:198-241` | independent code | Reusable once lifted out of their checkers. |
| `check_i3_paging` `CASES` and `tables()` | hand-declared answers | Fixture-bound; reusable as expected values for the same programs. |
| `check_i3_pressure.simulate` (ARM interpreter over FEX exports) | independent, but over FEX output | Producer-specific; obsolete if the producer changes. |
| Trace replay models in `check_i4`, `check_i4_pressure`, `check_i4v_progress_protocols`, `check_i4v_atomic_progress`, `check_i4v_stack_boundary`, `check_i4v_address_concurrent`, `i4v_controls_concurrent_check`, `check_i4v_coherence`, `check_i4v_partial_progress`, `check_i4v_containment` | independent Python, but they replay the runtime's own event IDs and Rust-derived records | **Not reusable as-is.** The protocol logic (FIFO, drain-before-mutation, acknowledgement sets, reopen, reclaim order) is the right specification for trace properties. Rewrite it against the ABI-specified trace format before implementation (plan §4.2). |
| `tools/testdata/*.json.gz` goldens | captured from the current emulator | Not independent; regression aids only. |
