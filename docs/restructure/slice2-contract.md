# Slice 2 contract: resident code cache, block linking and control flow

Status: proposal, revised after review, October 27, 2026 (S2, not implemented); [restructure](../vm-restructure.md) step 3;
builds on the [slice-1 contract](slice1-contract.md); FEX paths under `third_party/FEX/FEXCore/Source/Interface/Core`. Today
each block costs a VM exit, a new fexc process (~7 ms), admission (~2 µs), a copy into the one 64 KiB slot and a full-frame
ERET. Code density is ~5.7 native words per guest instruction (unpatched FEX: 3.1). Slice 2 removes the round trips and
dispatch cost and measures the result. No performance tuning this phase (owner, Oct 27); multi-vCPU is a slice-3 contract.

## 1. Code cache inside the VM
**Region (per vCPU).** Code lives in a resident-owned area: an RX alias at `PRIVATE + 0x1000_0000` and an RW alias at
`PRIVATE + 0x2000_0000` (the slot's W^X dual mapping, generalised). The area is 16 MiB by default (set at `prepare`) and
capped at 128 MiB, so every link is one `b`. A metadata arena (RW, non-executable, 8 MiB at `PRIVATE + 0x3000_0000`) holds,
per block: guest RIP and bytes, code range, sites, PC table, link points, link records it owns, and source pages as
guest-physical addresses. Both areas are bump-allocated, so `ELR → block` is a binary search; it replaces `header()` and
staging in the fault path. Const asserts pin the layout: guest RAM's alias ends below the code area, and every area lies
within `PRIVATE..PRIVATE + 1 GiB`. The host `private_space` builder must grow past its 2 MiB window, using 2 MiB blocks
where aligned. FEX code is position-independent, so placement is a copy plus `synchronize_code`; blocks stay ≤ 32 KiB; the
slot remains for uncached single-step blocks. **Eviction.** When either area would overflow, the next miss flushes the whole
cache, with nothing generated live and the state banked. The flush resets the bump pointers, L1/L2, link records and
code-page bitmap, and increments the flush generation (as FEX `ClearCodeCache` and QEMU `tb_flush` do).

**Key = (guest linear RIP, context)**, where context is the CPL, the code mode (CS.L/D; 64-bit only for now) and the paging
context (CR3 and paging controls). Code is RIP-specific, so linear aliases of one page get separate blocks. The cache is
x86's ITLB plus decoded-instruction cache: a translation may persist until INVLPG, a CR3 write or a paging-control write.
Cross-modified code need only be seen after the executing CPU serialises (SDM vol. 3, "Handling Self- and Cross-Modifying
Code"). All of these events are system instructions, which end the block and run in the resident.

**Invalidation.** CR3, INVLPG and INVPCID, and changes to CR0.PG/WP, CR4.PGE/PCIDE/SMEP or EFER.NXE: flush the whole cache.
(Slice 2 adds only the hook; PCID/global tags later.) A CPL or mode change needs no invalidation (both are in the key), but
L1 is keyed by RIP only, so it is cleared on every context change. Capacity: flush the whole cache. A write to a translated
guest-physical page: invalidate that page's blocks (below). Shadow changes: nothing. Writes that bypass the bitmap also
invalidate. Host writes to guest RAM (drivers now, devices later) happen only while the vCPU is stopped and flush the cache
before the next entry. The resident's own writes through the RAM alias (walker A/D updates, split completions) check the
bitmap and take the write-fault path below.

**Write protection** (generalises slice 1's `source_va`). A bitmap over guest-physical pages marks pages that hold
translations. `install` maps a marked page read-only whatever the walk grants. Placing a block marks its ≤ 2 source pages;
if one of them had a writable shadow (a per-page bit kept by `install`), the shadow space is reset first. A write on a
marked page whose walk grants the write invalidates that page's blocks, clears the bit, upgrades the shadow and retries.
Invalidating a block unlinks the records that target it, drops the records it owns, and clears its L2 and L1 entries. The
test "is this the faulting block's own source page?" sits inside `translate`, so the shadow-fault, split and A/D paths share
it. On a hit, the block (found by ELR) must not run past the instruction. The resident drops any split in progress and banks
the precise state at the start of that instruction. It then runs that one instruction as an uncached `max_instructions = 1`
block in the slot. The step block does not mark its source pages (else the store faults forever). The next miss recompiles.
The same single-step rule applies when an invalidation drained at a resident entry hits the running block. This replaces the
`unsupported_smc` stop (S2e). (ponytail: resets
and scans; a reverse map if profiles show it.) Known cliff: data written on a loop's own code page costs a fault, a step and
a recompile on every iteration. **Fault retries.** In linked loops native PCs repeat legitimately, so the slice-1
no-progress test (same PC, FAR and descriptor as the last retry) gains the shadow-reset generation. An eviction, a
protection reset or a flush clears it. Only an identical consecutive abort (same descriptor and generation) is no progress.

**Lookup and compile.** L1: 4,096 direct-mapped {host entry, RIP} pairs (64 KiB), indexed by `rip & mask`, like FEX's
`L1Pointer/L1Mask`. L2: an open-addressed hash table from (RIP, context) to block, 64 Ki entries. It replaces FEX's
page-pointer L2 and robin-map L3, which assume one user address space. Only a miss in both reaches the host. The request
(`hvc #TRANSLATE_HVC`) carries the flush generation, so the host never re-sends an object the resident holds. A reply may
batch up to K objects, the block plus its literal-exit targets. A target is added only when its fetch walk succeeds
(speculative A bits are permitted) and is never `stop_at`; adding a target never faults. K = 1. fexc becomes one persistent
`fexc --serve` process, with length-prefixed stdin/stdout and the same code as the CLI. No state may leak between requests
(fexc globals, FEX's RIP-keyed code cache, undestroyable contexts): if FEX has started no threads, the server forks per
request from an initialised parent; otherwise it runs `ClearCodeCache` and resets every fexc global. Host object cache:
keyed by (RIP, context, source bytes, features, `max_instructions`, every compile switch).

## 2. Dispatch and block linking
**Dispatcher.** `Pointers.DispatcherLoopTop` becomes a resident assembly fast path, like FEX's `Dispatcher.cpp` loop. It
uses only x0–x3 and preserves NZCV (`sub`+`cbnz`) and the static registers: it loads `State.rip`, probes L1, and on a hit
branches (`br`) to the block. A miss falls into today's `returned_allocated` path: bank, L2, host request, ERET. Computed
exits (ret, indirect jmp/call) keep their admitted form and use this path. When guest TF or a debug breakpoint is active
(boot phase), dispatch and linking are bypassed, as in FEX's CheckTF.

**Direct-exit linking.** Patch 0005's literal exit becomes `ldr x0,L; str x0,[x28,#State.rip]; ldr
x1,[x28,#Pointers.ExitFunctionLinker]; br x1`. This is FEX's own linker/dispatcher split. `ExitFunctionLinker` joins abi
`frame::POINTERS`, with its offset pinned. The resident linker runs on the slow path, once per edge: (1) read
`InlineJITBlockHeader`; link only if it exactly equals a live cache block's header, and never from the single-step slot;
halt on any other value. (2) Find or request the target; if the flush generation changed meanwhile, enter the target without
linking (FEX does the same: `JIT.cpp` `ExitFunctionLink`). (3) In each of the block's link points whose literal equals the
target, rewrite `ldr x0,L` to `b <entry>` through the RW alias. Maintenance is `dc cvau` on the RW address, `dsb ish`, `ic
ivau` on the RX address, `dsb ish`, `isb`. (4) Record {block, offset, original word, target} and enter. If the link-record
area is full, enter without linking. Linked state equals entry state (FEX's block-boundary ABI). A jcc has two link points,
and a loop's back edge links to its own entry. Unlinking restores the original word with the same maintenance. A flush drops
them all.

**Admission rules** (extending `structure.rs`, each with a mutation check): (a) A literal exit is exactly those four words
with a proven guest-RIP literal (block end or decoded direct target). Admission emits it as a link point {offset, target}.
`ExitFunctionLinker` is loadable only in that form, and computed exits load `DispatcherLoopTop`. (b) No branch enters words
2–4 of a literal exit (the check-window rule, widened). (c) The only store to `BLOCK_HEADER` is the entry prologue `adr
x0,header; str x0,[x28]`. (d) The resident writes placed code only at link-point first words, and only `ldr x0,L` or `b E`,
where E is the entry of a live block keyed (L, context). A debug self-check re-decodes every link point. Isolation is
unchanged: a link only removes the hop between two admitted entries. Known limit: admission proves a link target is one of {block end, decoded branch target}, not which exit it belongs to.

**Stops and interrupts.** Stopping a running vCPU is the host's job. For a precise stop, the host `hv_vm_protect`s the IPA
of a dedicated stop page (stage 2). FEX's `EmitSuspendInterruptCheck` store at block entry, `str
xzr,[x28,#InterruptFaultPage]` (admission pins it), then faults at the next block entry with the entry state intact. That
store costs one word per block entry, which the cost model counts once it is enabled. `hv_vcpus_exit` is imprecise and
serves only as the test watchdog.

**Global-ready now (multi-vCPU is slice 3).** S2b builds these in multi-vCPU form, so slice 3 adds shootdown rather than
redesigning. Per-vCPU caches: no vCPU ever patches code another vCPU runs. (A shared cache would need FEX's B↔B thunks,
because Arm's concurrent-modification list excludes `ldr`.) Code-page bitmap: indexed by guest-physical page, in memory
shared between owners. Invalidation queue: one per vCPU, drained at each resident entry (system instruction, fault or miss).
**Multiblock stays off:** a linked loop iteration pays only the exit `b`, 2-word prologue and block-end flags.

## 3. Control flow end to end
jcc, jmp, call and ret already compile; they now link or dispatch. `loop/loope/loopne/jrcxz` (rel8, no 67 prefix) join the
fexc allowlist and admission's `branch_target`. Non-canonical direct targets stay cut, and indirect targets keep the #GP
check before any state write.

**Corpus.** The reference author, independent of the compiler and VM, adds a control-flow mode to `gen_slice1.py`. It wraps
random straight-line bodies in structured templates: if/else diamonds, counted loops on a reserved counter (0–64 trips,
nested to depth 3), leaf calls, recursion to depth 8, `jmp [rip+table+idx*8]`, and loop/jrcxz. Programs terminate by
construction and have one exit, at `stop_at`. The reference is a full pinned-QEMU run. Undefined-flag masks follow the
executed path (`-d in_asm,exec,nochain`, last writer per flag). For faults inside loops, a guest signal replaces byte
prefixes: the harness makes the page `PROT_NONE` or read-only, and a handler dumps GPRs, RIP, RFLAGS and memory from QEMU's
precise SIGSEGV.

**FEX ASM tests** (`unittests/ASM`, 2,515 with `32Bit_ASM`). A converter assembles each test with pinned Homebrew `nasm`
(`-i Includes/`). It maps the `%ifdef CONFIG` JSON to a program: code at 0x10000, stack and `MemoryRegions` as declared,
`MemoryData`, and `RegData` as the expected values. The driver provides 4-level tables and demand-zero regions. On a
not-present #PF inside a declared region it maps a page, as an OS would, and resumes. Guest RAM grows to 64 MiB; exhaustion
is reported. `hlt` ends a test. The result is pass, fail, or unsupported with a reason: 32-bit code, `HostFeatures`, non-GPR
`RegData`, or a cut instruction. Every failure is re-run in QEMU, because FEX's expected values are a second reference, not
ground truth. `SelfModifyingCode/` and `Multiblock/` are named regressions. **Boot-phase notes.** A delivered data #PF
invalidates TLB entries for its page, so it also drops blocks whose source linear page it is. TF and breakpoints bypass the
fast path (§2).

## 4. Performance measurement (decided yardstick; results reported, not gating)
**Baselines.** The primary baseline is `qemu-system-x86_64` TCG run natively on the M4: Homebrew bottle, with version and
hash pinned per run. The guest is a bare-metal multiboot stub with interrupts off and the PIC masked. It builds the same
identity tables, enters long mode, runs the benchmark at natemu's linear addresses and exits via `isa-debug-exit` (`-accel
tcg,thread=single -cpu max`). Both are full-system emulators paying for guest paging. The secondary baseline is the
container's pinned `qemu-x86_64` linux-user. It has no softmmu and runs under Docker's Linux VM. **Benchmarks**
(driver-format x86, shared by all runners): integer loop (8 ALU instructions + dec/jnz), memory loop (load-add-store over 64
KiB), call-heavy (leaf call/ret with push/pop), branchy (data-dependent jcc + jump table), and division.

**Metrics.** Steady state is the slope of wall time from N to 2N iterations, so boot and compile cancel. Assert that the
same set of blocks is compiled at N and at 2N. At least 5 runs, reporting median and spread, on an idle machine on the
P-cores (user-interactive QoS; macOS has no hard pinning). Report ns per guest instruction (counts known by construction),
guest MIPS, and native words per guest instruction on the hot path. natemu's words are classified by admission's decoder;
QEMU's are counted from `-d out_asm`, which is never timed. Cycles are reported only with PMU access. Compile cost is
separate: compiles, fexc and admission time per block, VM round trip, and time to first iteration.

**Attribution.** Static: each hot word is classed as body, precision (diff against the retained build `s1c`), guard, split,
exit/prologue or dispatcher. Dynamic: measurement-only runs disable one check class at a time. This is a build feature (fexc
switch plus `--measure-unchecked=<class>`), never in default builds, and its objects are never cached. The time difference
per guest instruction is that class's cost. Per-class deltas are not additive. Counters: fast hits, slow entries, links,
unlinks, flushes, requests, steps, retries, splits. **Targets** (decided): steady-state ns per guest instruction against
QEMU system TCG, integer loop ≤ 1.0× and memory loop ≤ 0.7×. They are reported, not gating. Linux-user ratios go alongside.

## 5. Optimisation candidates (deferred to a later phase, not part of slice 2)
Recorded so attribution can name them: (1) guard once per base until modified (sound given the unmapped 1 GiB gap below
`PRIVATE`; needs a pinned layout invariant); (2) precision only where a fault can observe it, or per-site flag recipes; (3)
a cheaper flag rule (undefined-flag writes kill AF/PF); (4) cold-area stubs (0.53 words); (5) no prologue store (exits use
`blr`); (6) an inline L1 probe and the x25 call-return stack; (7) completing code-page stores outside translated bytes; (8)
multiblock (admission: backward branches to PC-table starts, non-monotone PC table, back-edge suspend checks); (9) batching
with K > 1.

## 6. Chunks, in order (each keeps fixtures 64/0/0 and faults 434/0/0)
- **S2a, compile server and object cache.** *Check:* objects identical to the CLI's on the fixtures and seed 4242, also with
  requests in adversarial order (repeats, interleaved RIPs, varying `max_instructions`); compile time reported.
- **S2p, QEMU baseline** (parallel, independent author): pinned QEMU, multiboot stub, five benchmarks, slope harness.
  *Check:* spread under 5%; linux-user numbers too.
- **S2b, code cache and lookup, no linking:** areas and const asserts, ELR → block, L1/L2, fast dispatcher, flush,
  global-ready bitmap and queue, writes to protected pages stopping with `unsupported_smc`, watchdog. *Check:* host requests
  equal distinct blocks on loop probes; a forced 64 KiB cache flushes and still matches.
- **S2c, linking:** exit pointer (patch 0005), admission rules (a)–(d), and resident link, unlink and self-check. *Check:*
  each rule refuses its mutation (branch into an exit tail, wrong literal, linker load outside the form, extra header store,
  tampered link point); a forged header halts; a flush during the target request enters unlinked; a hot loop makes 0 resident
  entries per 10^6 iterations; corpora pass at block sizes default/1/2.
- **S2d, control-flow corpus, signal fault reference, loop/jrcxz.** *Check:* seeded and reviewer programs, and a fault sweep
  inside loops, match QEMU.
  *Built (Oct 27):* `tools/reference/gen_slice2.py` (64 seeded programs from the template list above), `ref.py` `flow` and fault modes, `tools/run_slice2_vm.py` and `tools/run_slice2_faults.py` (see their headers and
  `tools/reference/README.md`). The fault reference is a single-stepped SIGSEGV run; error code and CR2 follow natemu's
  policy from the instruction QEMU faulted on, because QEMU reports read-modify-write loads as reads and never-mapped pages as
  protection faults, and the flags come from QEMU's exact straight-line run of the executed path, because its signal context
  can hold stale AF/CF after a `cmp` and a faulting `setcc`. `loop/loope/loopne/jrcxz` are allowlisted (fexc series9).
- **S2e, SMC invalidation and single-step.** *Check:* a write to another block's page retranslates; a store or a split store
  into the running block's next instruction runs the new bytes; the mixed code/data loop terminates correctly; FEX
  `SelfModifyingCode/` passes.
  *Built (Oct 27):* runtime `code_written` (the hit test, shared by stores, split stores and walker A/D writes), `step`
  (request `REQUEST_STEP`, host compiles with `max_instructions = 1`; record at lookup `STEP_RECORD`, code in the slot),
  `cache::invalidate` (marks blocks `DEAD`: L2 tombstone, L1 entry cleared, links into and out of them undone and their
  records dropped, page bit cleared) and per-page queue entries. The step block's own writes always invalidate and
  complete (one already-fetched instruction), which ends the step. Checks in `tools/run_slice2_probes.py`: SMC programs and
  FEX's three tests run as is under pinned QEMU (the bytes cross-check is skipped for them). Not exercised: walker A/D
  writes into a code page (the driver cannot put tables on code pages) and invalidations queued while a block runs (no
  producer until slice 3).
- **S2f, FEX ASM tests through the program driver.** *Check:* counts with reasons; none unexplained.
- **S2g, measurement and attribution.** *Check:* the §4 table and attribution reported against the targets; the phase ends
  here.

## 7. Owner decisions
Decided Oct 27: yardstick (native `qemu-system-x86_64` TCG, bare-metal guest; linux-user secondary), targets reported not
gating, no tuning this phase, multi-vCPU in slice 3. No further design or scope change proposed.
