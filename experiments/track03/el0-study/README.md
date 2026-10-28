# EL0 study: run translated code at EL0 instead of EL1t?

Status: design study, October 28, 2026. It gathers evidence only. Nothing in `crates/` or `tools/fexc` was changed.
Owner question: why not run translated blocks at EL0 and let the VM's page tables and traps provide isolation,
instead of admission (`crates/host/src/admission*.rs`) and the in-code guards from the fexc patches?

Labels used below:
- **measured**: this study, or an earlier measurement that is named.
- **code**: read in the code at the cited place.
- **not verified**: neither of the above.

Files:
- `probe.c`: a standalone HVF probe, independent of natemu. Build it with `build.sh OUT`, run it with `OUT/probe [reps] [scale]`.
- `census.py`: a static word census of fexc slice-1 output.
- Raw output is in `/private/tmp/claude-501/-Users-liaozping-orca-natemu/025ed521-9978-4fb2-aaf7-b90da95d37f3/scratchpad/el0/`:
  - `final.txt`: the run reported here, `probe 9 2`;
  - `try2.txt`: a replicate;
  - `try3.txt`: the trap checks;
  - `census.txt` and `census.json`.

## Answer in brief

Hardware at EL0 can replace the **isolation** half of admission. It cannot replace the in-code **address guard**
without giving up exact x86 #GP/#SS for non-canonical addresses.

The reason is that generated code must still touch some private plumbing:
- the x28 frame and the stop page;
- the code area, including literal loads from it;
- the L1 lookup table, if `dispatch_fast` stays inline;
- the helper stack.

At EL0 that plumbing must be EL0-accessible. Every canonical address is a guest address, so the plumbing can only
live in the x86 non-canonical hole. A guest access with a non-canonical address that lands on one of those pages
would complete silently instead of raising #GP.

The other checks are x86 semantics or recovery at any privilege level:
- the split check;
- the #DE checks;
- the branch-target check;
- precision;
- the stop store.

So moving to EL0 with exact semantics removes **no generated-code word**. It adds about **16 ns per resident entry**
that is now a `br` (the dispatcher miss and the linker). Linked edges, `dispatch_fast` hits and fault round trips
cost the same at EL0 and EL1 (measured).

**Recommendation: stay at EL1t now.** A hybrid would remove the guard for scalar integer accesses with exact
semantics: EL1 code that makes guest accesses with the unprivileged `ldtr`/`sttr`. Record it as an owner decision
for later (§5).

## 1. Inventory: what depends on generated code running at EL1t

| # | Mechanism (code) | EL1 dependence | Change needed at EL0 |
|---|---|---|---|
| 1 | Block entry: `runtime/src/execution.rs` `BLOCK_SPSR = 0x3c4` (l.120, `enter` l.450-466) → `gates.S` `enter_allocated` / `resume_exception` (ERET). | SPSR.M = EL1t. | SPSR.M = 0 (`0x3c0`). Trivial. |
| 2 | `dispatch_fast` (`gates.S`), whose address the resident writes into the frame (`execution.rs` l.157). | (a) It lives in resident text, mapped `Rights::Text` = AP 10, EL1 read-only (`host/src/memory.rs` `map_private`). (b) It reads L1 through `ldr x1, =l1_address` in the lookup area, which is EL1 data. (c) It increments `fast_counter` in `State`, which is EL1 data (`runtime/src/main.rs` l.33). | Copy it to an EL0-executable page (for example the start of the code area). A branch from EL0 into EL1-only text gives an instruction abort, EC 0x20 (measured). Map L1 read-only for EL0. The lookup area is mapped as 2 MiB blocks, so L1 (64 KiB at +0x10000) needs its own mapping. Move the counter to an EL0-writable page or drop it. Hit cost is unchanged: 0.922 ns per exit at both levels (measured, §3). |
| 3 | Miss path and linker: `gates.S` `returned_allocated` and `link_exit` start with `msr spsel, #1` and `mrs nzcv`. | `msr spsel` is UNDEFINED at EL0: EC 0 (measured). | Enter with `svc #imm` (or `brk`). The handler takes NZCV from SPSR_EL1 and SP from SP_EL0, as `exception_capture` already does. Cost +15.9 ns per entry (§3). |
| 4 | Block linking: `runtime/src/cache.rs` `link` patches word 1 through the RW alias, then `synchronize_code` (`dc cvau` / `ic ivau`, l.237-250). The linked words are plain `b`. | The resident does the maintenance at EL1. | No change to linking. The code area (RX alias `CODE_VA`) and the step slot (`SLOT_VA`) must be EL0-executable (AP 11, UXN 0, PXN 1). A block-to-block `b` costs the same (measured). |
| 5 | Literal loads in generated code (`ldr x0, L` in link points; RIP literals). | They read the code page. | The code area must be EL0-readable as well as executable. Execute-only does not work. |
| 6 | Stop store `str xzr, [x28, #3776]` lands on the stop page (`STOP_OFFSET`, EL1 data, host `execution.rs` l.98). A stage-2 protect makes it exit to the host, and the host injects `stop_gate` (host `execution.rs` l.708-727: SPSR from CPSR, CPSR 0x3c5). `stopped` requires `spsr & 0x1f == 4` (`runtime/src/execution.rs` l.498). | It checks for EL1t. | The stop page becomes EL0-writable and the checks expect EL0t (0). The stage-2 protect trap from an EL0 store reaches the host with the same syndrome as from EL1t: `0x93c2804f` in both cases (measured). Host injection from EL0 was not tested. |
| 7 | Exception gates: all 16 `vectors` go to `exception_capture`. `aborted` (`execution.rs` l.807-857) requires `spsr & 15 == 4` and EC 0x25 for data aborts. `access_aborted` (l.860-880) requires EC 0x25. | Faults from EL1t arrive in the "current EL, SP0" group, EC 0x25 / 0x21. | Faults arrive in the "lower EL" group: EC 0x24 data abort, 0x20 instruction abort. The SPSR mode check changes. `brk` keeps EC 0x3c and its immediate: `ESR 0xf2000001` for `brk #1` at EL0 (measured). Resident faults (EL1h) get their own vector group, which separates them from guest faults. Round-trip cost is equal (§3). |
| 8 | x28 frame: `FRAME_OFFSET` page, EL1 data (host `execution.rs` l.97). It holds native pointers (`DispatcherLoopTop`, `ExitFunctionLinker`, `LUDIV/LDIV` handlers; abi `execution.rs` `frame::POINTERS`), `State.rip`, the block header and the pre-NZCV slot. | EL1 data. | The frame must be EL0 read-write. It becomes untrusted input to the resident, as syscall arguments are to a kernel. The resident already validates the header (`linker`) and the ELR (`cache::block_at`). A corrupted pointer at EL0 can only reach EL0-executable code. |
| 9 | Private window at `PRIVATE = 0x0000_8000_4000_0000`, in the x86 non-canonical hole. Contents: resident text, `State`, shared record, staging, exception stack, tables and pool, metadata, lookup, global masks and the **guest-RAM alias** (host `execution.rs` l.91-110). | At EL1, generated code can reach all of it. Isolation rests on the guard plus admission. | All of it except items 2-8 stays EL1-only and becomes hardware-protected. An EL0 load or store to an EL1-only page faults with DFSC 0x0f (permission, level 3). An EL0 access to the unmapped hole or outside the 48-bit range faults with DFSC 0x04 (translation, level 0) (measured). The EL0-reachable set is: frame and stop page (8 KiB), block stack (16 KiB), code area (16-128 MiB), slot (64 KiB), and L1 (64 KiB, read-only). |
| 10 | Shadow descriptors: `shadow::PAGE` = AP 00 \| UXN \| PXN; `READ_ONLY` = AP[2] (abi `shadow.rs` l.23-29). Host pre-linked shadows: `memory.rs` l.236. | They are EL1-only data. | AP 01 (EL0 read-write) and AP 11 (read-only). x86 U/S, WP, SMEP and SMAP stay in the software walker. The resident writes guest RAM through its alias, not through shadows (code: `split`, walker), so PAN is not involved. |
| 11 | SP_EL0: generated code runs on SP_EL0 = `STACK_TOP` (`gates.S` `_start`; `enter` sets `f.sp`). The resident runs on SP_EL1. | None, apart from the page permission. | The stack page becomes EL0 read-write. Nothing else changes. |
| 12 | FPCR and flags: `mrs/msr nzcv`, `cfinv`, `rmif`, `setf8/16` (admission decode). The host sets CPACR_EL1 = 3<<20 (`host/src/vm.rs` l.199). | None. | None. `mrs`/`msr nzcv`, `msr fpcr` and `msr tpidr_el0` all complete at EL0 (measured, with CPACR FPEN = 3). |
| 13 | `msr`/`mrs` or cache maintenance in generated code: admission refuses every system word except NZCV moves, three `dmb` forms and `yield` (`structure.rs` `decode`). | Required at EL1 for isolation. | At EL0 these trap by hardware (measured, natemu's SCTLR has UCI, UCT, DZE, UMA and nTWI = 0): `msr ttbr0_el1`, `mrs sctlr_el1`, `msr spsel`, `tlbi`, `at`, `hvc`, `eret` → EC 0; `dc civac`/`cvau`, `ic ivau`, `dc zva`, `msr daifset`, `mrs ctr_el0` → EC 0x18; `wfi` → EC 0x1. |
| 14 | Helpers: 128/64 division `blr x3` → `natemu_ludiv`/`ldiv` in resident text (`execution.rs` l.35-115), using 48 bytes of native stack. The guard, split and divide stubs are `brk #1/#2/#3`. The split service completes through the guest-RAM alias (`execution.rs` `split`). | The division helper is in EL1 text. | Copy the division routines to an EL0-executable page, or enter them with `svc` (+15.9 ns). `brk` stubs and the split service are unchanged apart from items 7 and 14. |
| 15 | `tools/check_resident_stack.py` checks the gate and ERET binary shape. | — | It must be updated with items 2, 3 and 7. |

## 2. Classification: admission rules and FEX patches

Classes: **(i)** needed only because of shared EL1 privilege or address space, so hardware replaces it at EL0;
**(ii)** x86 semantics or recovery at any privilege level; **(iii)** both, or unclear. Where a class says
"EL0: …", it describes how a violation would show up at EL0.

**Admission records** (`admission.rs` `admit_records`):

| Rule | Class |
|---|---|
| Magic, version, sizes, profile/settings, RIP | (ii) consumer correctness |
| Feature bits checked against ID registers | (ii). A missing feature traps at either level. |
| Code layout | (iii). `spill_bytes == 0` exists because unverified `sp` use would be an isolation hole at EL1. The rest is (ii). |
| SRA map, PC table, sorted sites, site fields, intent, stub matching | (ii) recovery |
| Guest-RIP relocations (plausible literal values) | (ii) |

**Code rules** (`admission/structure.rs`):

| Rule | Class |
|---|---|
| Decode allowlist: no system, exception-generating, `ret`/`bl`/`blr` except the helper, or SIMD words | (iii). The system and exception-generating part is (i) (traps at EL0, measured). The SIMD part is (ii): unrecorded accesses and vector-state ownership. |
| x28 never written; `sp` written only by the helper frame; no `sp` accesses | (i). EL0: a violation can only corrupt the vCPU's own EL0 pages. |
| Direct branches forward to decoded words; no fall-through into literals or stubs; literal loads only of guest-RIP slots | (iii). At EL1, executing a literal as code could be any instruction. At EL0 it could only trap. Check bypass is still a semantic error. |
| Frame accesses inside admitted fields; native pointers loaded only by exits or helpers; `State.rip` written whole | (i) for the pointer rules (at EL1, a written pointer is an arbitrary EL1 jump); (ii) for `State.rip`. |
| **Address guard on every access** (`eor x0,xA,xA,lsl #16; tbnz x0,#63`) | **(iii)**. It is isolation of everything in `PRIVATE` (i), and non-canonical #GP/#SS (ii). At EL0, hardware covers the EL1-only pages and the unmapped hole, but not the EL0-accessible plumbing of item 9. So the guard stays for exact semantics. |
| Split check on every store wider than one byte | (ii). x86 page-crossing stores are all-or-nothing; Arm may commit part of one (DDI0487 B2.8.2.1, slice-1 contract). |
| Entry prologue exact; stop store only there; header store only there | (ii). Precise stop, and the linker's trust in the header (which it also validates). |
| Exits: branch-target guard on computed exits; exact link-point form; no branch into words 2-4; `ExitFunctionLinker` loaded only there | (ii) mostly: #GP at a non-canonical branch target, and correct linker patching. "Only these indirect branches" is (i). |
| Division checks exact | (ii) #DE |
| Each stub reached by exactly one check; no branch into a check window | (iii). Bypassing a guard is an isolation break at EL1, but only a semantic error at EL0. |
| `swpal` only, result kept; `dmb ld/sy/st` and `yield` only | (ii) ordering. Refusing `wfe`, `isb` and `dsb sy` is (iii). |

**FEX patches** (`tools/fexc/patches`, `shim/`):

| Patch | Class |
|---|---|
| 0001-0003 (portability, code-window seam, export hook) | Build or compile-only. No class. |
| 0004 site recording | (ii) |
| 0005 block cutting / single-gate exits | (ii) and dispatch plumbing. The system-instruction split is EL-independent. |
| 0006 precision | (ii) |
| 0007 address guard | (iii) |
| 0007 split check, branch-target check, push via `stur` + `sub`, `ret` without writeback | (ii) |
| 0008 #DE | (ii) |
| 0009 self-loop exits and stop store | (ii). Precise stop through stage 2 works from EL0 too (measured). |
| 0010 linker exits | Plumbing, EL-independent. |
| `shim/fexc_slice1.inc` allowlist | (ii) |

**Static size** (measured; `census.py` with the series10 `fexc`, slice1 profile):
- Scope: 556 blocks, 4,233 guest instructions and 1,680 access sites, from four sources: the 64 fixtures,
  the seed-4242 corpus, the slice-2 flow corpus and the six benchmarks.
- Total: 26,642 words, 6.29 per guest instruction.

| Class | Words | Share | Per access | Class (i)/(ii)/(iii) |
|---|---|---|---|---|
| address guard (`eor`+`tbnz`; far form 3 words; `b G`) | 2,490 | 9.3% | 1.48 | (iii) |
| guard-only address computation into x1 | 586 | 2.2% | 0.35 | (iii) |
| split check | 1,509 | 5.7% | 0.90 | (ii) |
| branch-target guard | 90 | 0.3% | — | (ii) |
| divide-check branches (a lower bound; other check words count as body) | 148 | 0.6% | — | (ii) |
| pre-NZCV save | 460 | 1.7% | — | (ii) |
| prologue: header 2 words + stop store 1 word per block | 1,668 | 6.3% | — | (ii) / plumbing |
| exits (link points + computed) | 3,267 | 12.3% | — | plumbing |
| out-of-line `brk` stubs (cold) | 1,941 | 7.3% | — | follow their checks |
| body | 14,483 | 54.4% | — | — |

The address guard plus its address word is 3,076 in-line words. That is 12.5% of the 24,701 non-stub words, 1.83
words per access, plus one cold stub per guard.

**Runtime cost of the guard** (S2g, October 27, `tools/bench/README.md`; not re-run here). Removing the guard in the
native class-removed variants changed ns per iteration as follows:

| Benchmark | Change with the guard removed |
|---|---|
| mem64 | -0.05 of 0.79 (about 6%) |
| call | -0.11 of 1.87 (about 6%) |
| mixed | within noise |
| int | within noise |

## 3. Measurements (standalone HVF probe)

Setup:
- One vCPU on the main thread, QoS user-interactive.
- 4 KiB stage 1, 48-bit TTBR0. Page AP/UXN/PXN set per role.
- natemu's SCTLR value 0x30d01985, ACTLR EnTSO, CPACR FPEN = 3, CNTKCTL EL0VCTEN.
- Timing by `cntvct_el0` at 24 MHz around loops of 1-40 million operations, minus the empty loop where marked.
- 9 repetitions, median (min-max).

Machine:
- M4, macOS 26.6.2, 14:08 PDT October 28.
- Not idle: load average 2.4-2.5. Another agent's `vmloop` held one core at 100%, and other benchmark agents were running.
- A replicate (`try2.txt`, load 1.9) agreed within 1 ns everywhere except EL1t translation faults: 73.6 versus 78.2 ns, with overlapping ranges.

| Operation | EL0 | EL1t |
|---|---|---|
| `svc` → EL1 handler (7 instructions) → `eret` | **31.10 ns** (31.07-31.27) | 31.07 ns (`svc` to EL1h) |
| Today's resident entry from EL1t: `br` → `msr spsel,#1` … `eret` | — (UNDEFINED at EL0) | **15.20 ns** (15.19-15.37) |
| Data abort, permission fault (store), skip, `eret` | 38.13 ns | 38.12 ns |
| Data abort, permission fault (load), skip, `eret` | 35.63 ns | — |
| Data abort, translation fault, skip, `eret` | 74.2 ns (71.5-80.0) | 78.2 ns (73.8-81.6) |
| `hvc` to the host and back (no host work) | — | 660 ns (640-668) |
| Linked block edge (`add; b` to the next 64-byte block, 64 per loop) | 0.528 ns (0.503-0.551) | 0.530 ns (0.524-0.541) |
| Computed exit through a `dispatch_fast` copy (frame store, pointer load, `br`, L1 probe in a read-only page, `br`; 16-block ring; no counter) | **0.922 ns** | 0.923 ns |
| Load-add-store, 16 per loop, same address (EL0 read-write data page) | 0.433 ns (0.422-0.440) | 0.422 ns |
| The same loop with `ldtr`/`sttr` | — | 0.441 ns (0.440-0.443) |

Trap and permission checks (measured, `final.txt` / `try3.txt`):
- **EL0, faults by hardware:**
  - Load or store to an EL1-only page → EC 0x24, DFSC 0x0f.
  - Store to a read-only page → EC 0x24, DFSC 0x0f, WnR.
  - Load from `0x0000_8000_4000_0000` (natemu's `PRIVATE` address, unmapped here) → EC 0x24, DFSC 0x04 (translation fault, level 0).
  - Load from `0x0001_0000_0000_0000` (outside 48 bits) → DFSC 0x04.
  - Branch into EL1 text → instruction abort, EC 0x20.
- **EL0, system instructions** (the EC list is in §1 item 13):
  - `msr ttbr0_el1`, `mrs sctlr_el1`, `msr spsel`, `tlbi vmalle1`, `tlbi vae1`, `at s1e1r`, `hvc`, `eret`
    trap to EL1 as EC 0.
  - `dc civac`, `dc cvau`, `ic ivau`, `dc zva`, `msr daifset`, `mrs ctr_el0` trap as EC 0x18.
  - `wfi` traps as EC 0x1.
  - `brk #1` traps as EC 0x3c with ISS 1.
  - None of these reached the host.
- **EL0, complete:** `mrs`/`msr nzcv`, `msr fpcr`, `msr tpidr_el0`, `mrs cntvct_el0`, and loads and stores to EL0 pages.
- **EL1t:** the same EL1-only load completes; `tlbi` and `dc civac` complete; the hole and outside-range loads fault (EC 0x25, DFSC 0x04).
- **`ldtr`/`sttr` at EL1t:**
  - `ldtr` from an EL1-only page → EC 0x25, DFSC 0x0f.
  - `sttr` to a read-only page → DFSC 0x0f.
  - `ldtr` from the hole → DFSC 0x04.
  - `sttr` to an EL0 read-write page completes.
- **Stage 2:** `hv_vm_protect(granule, READ)` and a store from EL0 → host exit, syndrome `0x93c2804f` (EC 0x24, WnR,
  DFSC 0x0f). The result is identical from EL1t. So the precise-stop mechanism works from EL0.

Reading:
- (a) An exception entry from EL0 costs the same as one from EL1t (31 ns). Data aborts cost the same at both levels.
  The only EL0-specific cost is that the resident entries reached by `br` today (miss, linker) become exceptions:
  31.1 - 15.2 = **+15.9 ns per entry**. Both paths here use minimal handlers. natemu's full-frame save and restore is
  paid at either level and was not measured.
- (b) Isolation by hardware works as described.
- (c) Block-to-block branches, the dispatcher fast path and plain loads and stores cost the same at EL0 and EL1t,
  within run-to-run spread.

## 4. Dispatch at EL0: per-exit overhead

| Exit kind | Today (EL1t) | At EL0 | Difference |
|---|---|---|---|
| Linked edge | `b` | `b` | 0 (measured equal) |
| `ret` / indirect jump, L1 hit | `dispatch_fast` | the same code on an EL0-executable page with a read-only L1 | 0 (measured 0.922 versus 0.923 ns). The `fast` counter must move to an EL0-writable page or be dropped. |
| L1 miss, or first use of a literal exit (linker) | `br` + `msr spsel` | `svc` | +15.9 ns per entry |
| Shadow retry, guest fault, guard/split/divide stub | exception | exception | 0 (measured equal) |
| Compile request, system instruction, kick | `hvc` to host | unchanged (the resident is still at EL1) | 0. Each costs about 0.66 µs plus host work. |

Exit counts per run are from S2g (`natemu-raw/s2g/bench-natemu2.json`, stats at N, identical at 2N):

| Benchmark | Linker | Compiles | Retries | `fast` hits |
|---|---|---|---|---|
| int | 2 | 1 | 0 | 0 |
| mem64, mem4096 | 2 | 1 | 512 | 0 |
| call | 4 | 4 | 1 | 1 per iteration (60 million) |
| branchy | 10 | 6 | 0 | 0 |
| mixed | 2 | 1 | 32 | 0 |

Steady-state iterations never enter the resident. The EL0 surcharge is therefore at most about 10 × 15.9 ns ≈ 0.16 µs
per run. It is invisible in the S2g slopes, as an estimate from these counts.

`dispatch_fast` can run at EL0 with read-only lookup tables (measured with a copy). Its miss branch must become
`svc`. Workloads that enter the resident per instruction already pay an exception or a host round trip at EL1, so
the surcharge does not dominate there either. Examples are SMC stepping (`data@4`: about 120,000 steps per 20,000
rounds, boot-handoff §4) and future system-instruction-heavy boot code. This is an estimate and was not measured.

## 5. Recommendation: stay at EL1t now; keep two options on record

**Expected removal of checks under EL0 with exact x86 semantics: none in generated code.**
- Admission's (i) rules would become redundant for isolation. They cost about 2 µs of host time per block (slice-2
  contract), not runtime.
- The guard stays, because it is (iii): §2, and item 9 in §1.

**Expected added costs:**
- +15.9 ns per miss or linker entry (measured, minimal handlers).
- 0 per linked edge, dispatcher hit or fault (measured).
- Moving `dispatch_fast`, the division helpers and the fast counter to EL0-mapped pages.

**Benefit:** defence in depth. An admission or decoder gap could no longer reach the resident, its tables, the
guest-RAM alias or other vCPUs. It could only corrupt that vCPU's own EL0 pages.

**Risks:**
- The frame becomes guest-adjacent untrusted input. The resident already validates the header and the ELR.
- Every EL-dependent check (items 1, 3, 6, 7, 14) changes at once.
- The host's stop injection from EL0 is not tested.
- Interrupt masking for EL0 under HVF was not verified.

**Work:** about 5-7 agent-sized chunks.
1. Page roles and permissions (shadows, frame, stop page, stack, code area and slot, L1 split mapping).
2. EL0 trampolines: copies of `dispatch_fast` and the helpers, and `svc` entries for miss and linker.
3. Fault and stop path changes (EC, SPSR, host injection, `check_resident_stack.py`).
4. Admission: decide which (i) rules to keep as correctness checks, with mutation tests.
5. Full suite plus an independent review, 1-2 chunks.

**Option A, EL0 with guards kept (defence in depth).** Worth doing only if the owner wants isolation to stop
depending on admission as the ISA grows (SSE, x87, REP, LOCK forms). The runtime gain is zero.

**Option B, hybrid: EL1t code, guest accesses as `ldtr`/`sttr`, all plumbing EL1-only.** This would remove the
address guard with exact semantics for scalar general-register accesses. A non-canonical access, or one into the
private window, would fault by hardware: measured DFSC 0x0f or 0x04 at EL1t. The resident already turns gap and
level-0 faults into #GP (`access_aborted`, `noncanonical`).

Savings, measured statically: up to 1.48 guard words per access plus the cold stubs. The address word (0.35 per
access) stays where the access uses a register offset, because `ldtr` has only base plus a 9-bit immediate.

Its costs and limits:
- `ldtr`/`sttr` were 4.5% slower on the one serial loop measured (0.441 versus 0.422 ns).
- There are no unprivileged pair, SIMD or atomic forms. FEAT_LSUI was not checked on the M4 (not verified).
  So SSE vector accesses, `swpal` and `ldp`/`stp` keep the guard.
- PSTATE.UAO must stay 0. natemu does not set it (code: no reference).
- It needs a FEX emitter patch, plus an admission rule: "a guest access is `ldtr`/`sttr`, or guarded".
- The prize is bounded by S2g's measured guard cost (about 6% on memory and call loops, noise elsewhere).
- No performance tuning this phase (owner, October 27).

It belongs on the slice-2 contract's §5 deferred optimisation list, with an owner decision, because it changes the
isolation mechanism.

**Rejected: EL0 without guards.** It needs an owner-accepted deviation: non-canonical accesses that hit the
EL0-accessible plumbing would not raise #GP, and stores there would corrupt the vCPU's own frame or stack.

Another idea was not evaluated (no evidence): relocating the plumbing into canonical space the guest does not map.

**Interaction with the next phase:**
- **Interrupt delivery** (IDT, IRET, APIC, IPIs): it is planned at block boundaries through the stop mechanism, and
  delivery runs in the resident at EL1. It is EL-independent. Stage-2 stop traps from EL0 are measured to work.
- **SSE state:** this is the item that limits every option. There are no unprivileged vector loads, so vector
  accesses need the guard and sites whatever the level. EL0 needs CPACR FPEN = 3, which natemu already sets, and
  `msr fpcr` (MXCSR rounding) works at EL0 (measured).
- **More instruction forms:**
  - At EL1, each new A64 class FEX emits must be proven isolation-safe in `structure.rs`.
  - At EL0 (option A), a decoder gap is a correctness risk, not an isolation break.
  - x87 soft-float helpers would need EL0-executable copies, or `svc` at +15.9 ns per call.
  - RDTSC could read `cntvct_el0` at either level (EL0 measured with CNTKCTL EL0VCTEN).

## Follow-up: EL0 without the address guard

Status: design study, October 28, 2026 (same day, one pass). Evidence only; nothing in `crates/` or `tools/fexc` changed.
Question: what would translated code at EL0 need so that the address guard and the isolation half of admission go,
and the hardware alone raises x86 #GP/#SS for non-canonical addresses? What deviation, if any, would remain?

Files:
- `probe_noguard.c`: a standalone HVF probe. Build like `probe.c` (the `xcrun clang ... -framework Hypervisor` line of
  `build.sh`, then `codesign` with `entitlements.plist`); run `probe_noguard [reps] [scale]`. It uses natemu's TCR with
  TTBR0 **and** TTBR1 enabled (T0SZ = T1SZ = 16, TBI0 = TBI1 = 0) and natemu's SCTLR.
- `literals.py`: a static census of fexc slice-1 code (same block walk as `census.py`): literal loads, x28 frame
  accesses by offset, `sp` accesses, helper calls, spills.
- Raw output in `/private/tmp/claude-501/-Users-liaozping-orca-natemu/025ed521-9978-4fb2-aaf7-b90da95d37f3/scratchpad/el0b/`:
  `final.txt` (`probe_noguard 9 1`), `try2.txt` (replicate), `try1.txt` (first run, `3 0.2`), `literals.txt`/`.json`,
  `arm-B2-2-1.txt` and `arm-align.txt` (Arm DDI0487 M.c §B2.2.1 and §B2.8.2, fetched from documentation-service.arm.com).
- Machine: M4, macOS 26.6.2, 15:13-15:16 PDT, load average 1.6-1.8. All `check` lines were identical in the three
  runs. In `final.txt` the empty loop was disturbed (0.387 ns, range 0.27-0.50), so the stop-check rows below come
  from `try2.txt`; every other timing agreed within 0.01 ns.

### How often translated code touches emulator memory today (static, measured)

`literals.py` over the four corpora of §2 (556 blocks, all compiled `ok`):

| Access | Words | When it runs |
|---|---|---|
| `str x0, [x28, #0]` block-header store | 556 (1 per block) | every block entry, linked edges included |
| `str xzr, [x28, #3776]` stop store | 556 (1 per block) | every block entry |
| `str w0, [x28, #1032]` pre-NZCV save | 230 | each instruction that writes NZCV before an access site |
| `str x, [x28, #24]` `State.rip` store | 828 | each unlinked exit (783 link points + 45 computed exits) |
| `ldr x1, [x28, #2952]` linker pointer | 783 | each unlinked literal exit; a linked exit skips it (word 1 becomes `b`) |
| `ldr x1, [x28, #2920]` dispatcher pointer | 45 | each computed exit |
| `ldr x3, [x28, #3024/3032]` division helpers | 21 | each 64-bit div/idiv whose dividend needs 128 bits |
| `ldr x0, L` literal load from the code area | 783 | each unlinked literal exit; **all 783 read a guest-RIP slot; no other literal load exists** |
| `sp` accesses | 42 | all are the `lr` push/pop around the 21 helper `blr`s (patch 0008) |
| RA spills | 0 | `spill_bytes` is 0 in all 556 blocks, and admission refuses any other value (`admission.rs` l.213-215) |

So in steady state (linked edges, no division helper, no computed exit) the only emulator accesses are the two prologue
stores per block entry, plus the pre-NZCV save for flag-writing read-modify-write instructions. Dynamic counts on the
bench loops were not taken (not verified); per block entry they follow from the table.
126 blocks already carry RIP values as `movz/movk` immediates (relocation kind `guest_rip_move`).

### Can each access be eliminated, not moved?

| Access | Elimination | Evidence |
|---|---|---|
| Header store | **Drop it.** Only the linker reads it (`runtime/src/execution.rs` l.242; the pointers and the header are the only frame fields the resident reads besides `State.rip` and pre-NZCV, l.240-242, 481, 680). At EL0 the linker is entered by `svc`, so ELR_EL1 names the source block (`cache::block_at`, as `aborted` and `stopped` already do). | code; `svc` from EL0 measured in §3 (31 ns round trip) |
| `State.rip` store + linker pointer load | Link point becomes `movz/movk x0, rip` + `svc #LINK` (RIP in x0, exit kind in the immediate). The linker still patches word 1 into `b`. | code; movz/movk form exists in fexc output |
| `State.rip` store + dispatcher pointer load | Computed exit becomes `mov x0, xR; b dispatch` with `b` relocated by the resident at placement, as link points are patched today. `dispatch` lives in an EL0 execute-only page; its miss path is `svc`. | measured: frame-free exit through dispatch with L1 as read-only data 0.964 ns, with L1 encoded as code (execute-only; per entry `movz/movk x2 = rip; sub; cbnz miss; b block`) 1.221 ns |
| Division pointer loads, `lr` push/pop, helper stack | `svc #DIV` (or `brk`): the resident divides at EL1. No EL0 stack remains, so SP_EL0 can point at nothing. | +31 ns per helper division (§3 `svc` cost); 21 sites in 556 blocks |
| Literal loads | Replaced by `movz/movk` (above). Then the code area reads no data: the JITCodeTail RIP slot is read only by the resident. | `literals.py`: 783/783 literal loads are link points |
| Pre-NZCV save | Into a register instead of memory: x28 is free once the frame is gone, so `mrs x28, nzcv`; the resident reads x28 from the captured frame. | not verified (a FEX patch-0006 change) |
| Stop store | See next section. | measured |
| RA spills | None today. If FEX ever needs one it would go to `sp` memory, i.e. an EL0 data page. Options: cut the block instead (fexc), or admit spills to a page that is part of the guest-state window. | not verified; no spill seen |

### Can the EL0 window be empty?

The stop check must be an access the host can make fault at a block entry. Measured options (`try2.txt`, EL0, per
block entry, loop minus empty loop):

| Stop check | Extra cost | Host fault when armed | EL0 data page needed |
|---|---|---|---|
| `str xzr` to the stop word (today) | 0.000 ns | stage-2 read-only: exit `0x93c2804f` (§3) | stop page EL0 read-write |
| `ldr xzr` from the stop word | 0.000 ns | `hv_vm_protect(granule, 0)` accepted (rc 0); load exit `0x93c28007`, store exit `0x93c28047` (EC 0x24, translation L3) | stop page EL0 **read-only**, holding zeros |
| `bl pad` to a `ret` on an EL0 execute-only page alone in its 16 KiB granule | **+0.456 ns** | stage-2 R/W without X: exit `0x8200000f` (EC 0x20, instruction abort, permission L3); restored, it runs again | **none** |

The `bl` pad clobbers x30, which FEX uses only as a register-allocator temporary (slice-1 contract §1), dead at a block
entry. The stop gate would map x30 (the return address) to the block instead of ELR. A stage-2 change on something every
entry already touches, its own code, does not work: removing X from the code area stops the vCPU at an arbitrary
instruction, not at an entry (code reasoning; not measured).

Other primitives (`final.txt`):
- **Execute-only code** (AP 00, UXN 0, PXN 1): EL0 `br` into it completes. EL0 `ldr` from it → EC 0x24 permission L3;
  `str` → permission L3, WnR; an `ldr x, literal` executed inside it faults the same way (FAR = the literal). EL1 still
  reads it.
- **Read-only page** (lookup-table role): EL0 load completes; EL0 store → permission L3, WnR.
- EL1t loads from an EL0 read-write page complete (PAN is not in the way).

**Answer: yes, for the slice-1 instruction set.** With the header dropped, exits through `svc`/relocated `b`, division
by `svc`, no literals, pre-NZCV in a register, L1 encoded as code and the stop as an instruction fetch, translated code
makes **no data access** to emulator memory. Every x86-non-canonical address is then unmapped, EL1-only or EL0
execute-only, and a data access to it faults. The price, measured on loops: +0.46 ns per block entry, +0.26 ns per
computed exit (code-encoded versus data L1), about +31 ns per helper division (an `svc` round trip instead of a `blr`), and +15.9 ns per linker or miss entry (`svc`
instead of today's `br`, §3). Not measured end to end.

**Limit.** FEX keeps more guest state in its frame than slice 1 uses (`third_party/FEX/FEXCore/include/FEXCore/Core/CoreState.h`:
`flags[48]` (DF and others), `mxcsr`, `fs_cached`/`gs_cached`, segment indices, `mm[8]` and `FCW`/`AbridgedFTW` (x87),
`avx_high`, `xmm`). Once those instructions are admitted, that state needs an EL0 read-write page or registers. As a page
it holds only this vCPU's own guest state, never a pointer the resident trusts. Which fields FEX would touch was not
verified.

### Hardware #GP coverage outside the window (measured, EL0 loads)

| Address | Result |
|---|---|
| canonical: `0x0000_7fff_ffff_f000`, `0xffff_8000_0000_0000`, `0xffff_ffff_ffff_f000` (mapped) | complete |
| `0x0000_8000_0000_0000` (TTBR0-side hole; L0 entry 256 exists, as natemu's `PRIVATE` table) | translation fault L1 |
| `0x0000_8000_4000_0000` (an EL1-only page, natemu's `PRIVATE`) | permission fault L3 |
| `0x0000_ffff_ffff_fff8`, `0x0000_8880_1234_5678` (a kernel pointer truncated to 48 bits) | translation fault L0 |
| `0xffff_0000_0000_0000`, `0xffff_7fff_ffff_fff8` (TTBR1-side hole) | translation fault L0 |
| outside both ranges: `2^48`, `2^63`, `LIST_POISON1` `0xdead000000000100`, `LIST_POISON2` `0xdead000000000122`, KASAN offset `0xdffffc0000000000`, slab poison `0x6b6b6b6b6b6b6b6b` | translation fault L0 |
| `0xffff_4000_0000_0000`: an EL0 read-write page mapped in the TTBR1-side hole | **complete** (a window page would be reachable) |

Every fault had EC 0x24 and FAR = the address. Straddling and wrapping accesses:
- An 8-byte load or store at `0x0000_7fff_ffff_fffc` (canonical into hole) faults: translation L1, FAR = `0x0000_8000_0000_0000`
  (the first byte of the faulting page). The store committed nothing to the canonical page.
- A load at `0xffff_7fff_ffff_fffc` (hole into canonical) faults with FAR = the start.
- A load at `0xffff_ffff_ffff_fffc` wraps: FAR = 0. x86's rule for a wrapping access was not checked (not verified).
  This is unchanged by the privilege level.

The resident already gives exact #GP/#SS for all of these (code): `access_aborted` accepts translation and permission
faults at any level (DFSC 4-7, 12-15), requires `FAR - address < width`, and `pages()` raises #GP, or #SS for an
SS-segment site, unless both the first and the last byte are canonical (`runtime/src/execution.rs` l.721-746, 873-885). At EL0 only
the EC changes (0x25 → 0x24) and the SPSR mode check changes.

### Size and placement of a residual window (if the owner keeps any)

- **Minimal, measured option set above:** 0 bytes.
- **If the stop stays a data access** (0 ns instead of +0.46 ns): one 4 KiB page per vCPU, EL0 **read-only**, all zeros,
  armed by `hv_vm_protect(granule, 0)` and alone in its 16 KiB IPA granule. A guest store to it raises an exact #GP
  (permission fault). A guest load returns 0 instead of #GP.
- **If L1 stays data** (0.26 ns cheaper per computed exit): +64 KiB EL0 read-only, holding (guest RIP, native entry)
  pairs. Loads observe them; stores fault.
- **Today's frame** cannot be split into read-write and read-only parts without changing FEX's offsets. The written
  fields end at 1036, the pointers sit at 2920-3040 and the stop word at 3776. That leaves 2,740 bytes for two 4 KiB
  page boundaries (`abi/src/execution.rs` `frame`). So keeping any pointer load means guest-writable native branch targets.
- **Placement:** put any page in the TTBR1-side hole, away from both canonical edges (for example `0xffff_4000_0000_0000`).
  Linux poison values and the KASAN offset lie outside both ranges and fault anyway (measured above). A kernel pointer
  truncated to 48 bits lands in the TTBR0-side hole, not there. Which pointer patterns guests produce in general was not
  measured.

### Which remaining checks are x86 semantics at any privilege level

| Check | Verdict | Reason |
|---|---|---|
| Split check (page-crossing stores) | **Kept** | Arm DDI0487 M.c §B2.8.2.1.1: an access not within one aligned 16-byte quantity "is not guaranteed to be single-copy atomic except at the byte access level". §D1.4.1 (pinned in `artifacts/research/track01/source-snapshots/2026-10-27/arm-ddi0487Mc-D1-4-1.json`): for a Data Abort on an instruction that performs more than one single-copy atomic access, memory it affects "can be UNKNOWN". x86 commits neither part. Measured on the M4: eight page-crossing store forms (`str x`, `str w`, two `stp`, `str q`, into read-only and unmapped pages, at EL0 and EL1t) committed **nothing**. That is one implementation; relying on it would be a deviation from the architecture. |
| #DE checks | **Kept** | A64 `udiv`/`sdiv` return 0 on a zero divisor and do not trap (DDI0487C.a, cited in INV-03-02). x86 raises #DE. |
| Branch-target check | **Kept** | x86 raises #GP at the branch with no push, pop or RIP change. A lazy check at dispatch (fetch walk → `NONCANONICAL`) comes after `call`'s push or `ret`'s pop, and RIP already holds the target. 90 words in 556 blocks. |
| Precision (flag elimination, RA hoisting, no fusion, pre-NZCV, CF inversion) | **Kept** | exact state at a fault, at any privilege level (slice-1 contract §1). Pre-NZCV moves to a register (not verified). |
| Site map and recovery records | **Kept** | `aborted` maps every fault PC to a site or halts `unknown_abort` (`execution.rs` l.807-826) |

### Admission at EL0 without the window

With isolation enforced by hardware (system instructions trap, measured in §3; EL1-only pages fault; branches into EL1
text abort; no EL0 data page holds a trusted pointer), admission splits as follows:
- **Object validation stays mandatory**, because the resident parses the object: magic, version, sizes, sorted and
  in-range sites, SRA map and profile pin, feature bits, link-point offsets and targets, relocation values.
  This is about 2 µs of host time per block.
- **The code-shape rules can become an optional debug check:** the decode allowlist, x28/`sp` rules, frame-field rules,
  guard and stub structure, and "no branch into a check window". A violation is then a FEX correctness bug confined to
  that vCPU's guest, and the hardware still contains it.
- Two rules stay mandatory as correctness, because the resident patches or maps by them: "every exit has the exact
  link-point or computed form", and "the entry prologue is exact".

Not changed by this decision: x86 U/S, WP and SMEP stay in the software walker. Every shadow is EL0-accessible, so a
CPL change must reset or split shadows, at EL1t and at EL0 alike. CPL changes are boot-phase work (code: no CPL
transition exists yet).

### Today versus EL0 without the guard

| Mechanism | Today (EL1t) | EL0, empty window | Reason |
|---|---|---|---|
| Address guard (`eor`+`tbnz`, 1.48 words/access, plus 0.35 address words) | in code | **removed** | every non-canonical address faults (measured); `pages()` gives #GP/#SS (code) |
| Admission code-shape rules | isolation | **optional debug** | hardware isolation (measured traps and permission faults) |
| Admission object validation | yes | **kept** | the resident parses the object |
| x28 frame (header, RIP, pre-NZCV, 4 pointers) | EL1 data | **removed** | header unused at EL0; RIP in x0; pointers replaced by `svc` or relocated `b`; pre-NZCV in a register (not verified) |
| Literal loads (783) | code area readable | **changed** to `movz/movk` | code area becomes execute-only (measured) |
| Stop store (1 per entry) | `str` to the stop page | **changed** to `bl` to a pad on an execute-only page | stage-2 no-X exit measured; +0.46 ns per entry |
| `dispatch_fast` + L1 | EL1 text, data L1 | **changed**: execute-only copy, L1 as code | +0.26 ns per computed exit, measured |
| Linker, miss | `br` into resident text | **changed** to `svc` | +15.9 ns per entry (§3) |
| Division helper + EL0 stack | `blr` + `lr` push | **changed** to `svc` | +31 ns per helper division; no EL0 stack left |
| Split check | in code | **kept** | Arm permits partial commit (cited); the M4 showed none |
| #DE, branch-target check, precision, sites | in code | **kept** | x86 semantics (cited above) |

### Residual deviation (for the owner to accept or reject)

With the full design above there is **none** for the slice-1 instruction set:
- Every data access by translated code to an x86-non-canonical address takes a hardware fault, and the resident turns
  it into an exact #GP or #SS (measured for every region of the 64-bit space; existing resident logic).
- No EL0-reachable page holds data.

If the owner prefers the 0-ns data stop to the +0.46 ns instruction-fetch stop, the deviation is exactly this:
- A guest **load** from one 4 KiB non-canonical page per vCPU, at a fixed address in the `0xffff_0000_0000_0000`-
  `0xffff_7fff_ffff_ffff` hole, returns zeros instead of raising #GP. Stores there still raise #GP.
- Nothing in that page is ever read by the emulator.

Future instructions whose guest state FEX keeps in its frame (MXCSR, FS/GS base, DF, x87, AVX upper halves) would add
one EL0 read-write page holding only this vCPU's own guest state, unless that state is kept in registers.

### Work, in agent-sized chunks

1. FEX patches: exits with `movz/movk` RIP in x0 and `svc #LINK`; computed exits `b dispatch` with a relocation record;
   division by `svc`; no header store; pre-NZCV into x28; stop as `bl pad` with a relocation record. Plus
   fexc/`check_sites.py`/verifier updates.
2. Resident: EL0 entry (SPSR 0); `svc` gates (miss, linker, division) using x0 and ELR; lower-EL vectors and
   EC 0x24/0x20 in `aborted`; stop gate keyed on x30; division at EL1; execute-only `dispatch` page and code-encoded L1
   writer; relocation of the `b dispatch` and `bl pad` words at placement.
3. Host: page roles (code area, slot and pad execute-only; shadows AP 01/11; no EL0 frame, stop page or stack); kicks
   by stage-2 no-X on the vCPU's pad granule; stop injection from EL0 (not tested yet). Update
   `check_resident_stack.py`.
4. Admission: split it into mandatory object validation and an optional debug code-shape check. Add negative tests:
   a non-canonical access at each former window address, and both straddles, give #GP/#SS.
5. Full suites (fixtures, seed 4242, flow, faults, slice-3 sets, bench) plus an independent review (1-2 chunks).
