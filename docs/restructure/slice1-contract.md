# Slice 1 contract: precise faults in FEX-generated code

Status: proposal, revised after review, October 27, 2026 (S1, not implemented); [restructure](../vm-restructure.md)
step 3, first slice. FEX citations: pinned tree `natemu-raw/fexc-native-20261026-01/c1b/src/FEXCore/Source/Interface`.

## 1. Recovery: producer-emitted access sites at precise instruction boundaries

**What FEX gives and assumes.** `Core.cpp::GenerateIR` calls `FlushRegisterCache(true)` before every guest instruction
(comment: this "makes our exception handling behaviour more predictable"), then `_GuestOpcode` for instructions that
`OpDispatcher.h::CanHaveSideEffects` (every memory operand). So at an instruction start all prior GPR/PF/AF writes sit in
the static registers (`Arm64Emitter.cpp` `x64::SRA`: RAX x4, RCX x7, RDX x5, RBX x6, RSP x8, RBP x9, RSI x10, RDI x11,
R8-R15 x12-x17,x19,x29; PF x26, AF x27), and `Flags.cpp::CalculateDeferredFlags` has put guest NZCV in PSTATE. SRA
registers are never spilled (RA temporaries x18,x20-x24,x30; TMP x0-x3; x25 call-ret SP; x28 frame). FEX's own SIGSEGV
path (`SignalDelegator.cpp::SpillSRA`, `Core.cpp::RestoreRIPFromHostPC`, `ReconstructCompactedEFLAGS`) assumes at any
faulting PC: GPRs = SRA, RIP = last RIP-table entry ("as close as FEX can get"), NZCV from PSTATE with CF always inverted,
PF = popcount((x26 & 0xff) ^ 1) & 1, AF = ((x27 ^ x26) >> 4) & 1. natemu keeps the formulas; CF inversion is per site.

**Where that assumption is false** (each breaks either the state at a site or the "no effect yet" claim):
- Flags elided across accesses: `RedundantFlagCalculationElimination.cpp::ClassifyConst` gives `LoadMem`/`StoreMem`
  trivial flag info, so `ProcessBlock` deletes a flag write whose value is overwritten after an intervening access.
- CF inversion varies inside a block: the per-instruction flush is `SRAOnly`, which skips `RectifyCarryInvert`; only
  block ends normalise to `CFInvertedABI`. FEX's reconstruction is wrong after any subtraction-style instruction.
- Static-register writes moved early: the `RegisterAllocationPass.cpp` backwards pass coalesces an SSA value into its SRA
  register unless a load/store *of that register* intervenes; guest memory ops do not block it. `IREmitter.h::Constant`
  pools constants across instructions within a block, so a constant defined in instruction i and later stored to RAX can
  be coalesced into x4 before i's own accesses.
- Cross-instruction fusion: `TryPostRAMerge` skips `OP_GUESTOPCODE` and fuses two guest `push`/`pop` into one
  `stp`/`ldp` (`PushTwo`/`PopTwo`): one native access spans two guest instructions.
- Flags before an access: memory-destination read-modify-write (`add [m],r`, `inc/neg/shl/bts [m]`) emits
  `AddWithFlags` etc. into PSTATE before `StoreMem`; a store fault has lost the instruction's incoming NZCV.
- No address predicate: guest addresses are used raw, so a non-canonical address can reach private memory at
  `PRIVATE = 0x0000_8000_4000_0000`. That includes the x28 frame holding `Pointers.DispatcherLoopTop`, so a guest store
  could redirect an exit. Page-crossing stores may commit partially on either page (DDI0487 B2.8.2.1); x86 does not.
- No #DE: `OpcodeDispatcher.cpp::DIVOp/IDIVOp` emit no zero/overflow check (ARM `udiv` returns 0); 64-bit divides with
  a nonzero high half call `Pointers.LUDIVHandler/LDIVHandler` (`JIT/ALUOps.cpp`).
- No store forwarding or cross-instruction context caching affects slice-1 state (`PassManager.cpp::AddDefaultPasses`).

Alternatives: **A (chosen)** FEX records sites and patches make each instruction start precise: exact, costs a few native
instructions plus ~16 bytes metadata per access. **B** one guest instruction per block for faultable instructions with an
entry snapshot: exact, but a block exit/entry for about half of all instructions (defeats "cheaper than QEMU"). **C**
consumer disassembly (I3 `parser.py` style): cannot see CF inversion, elided flags, hoisting or fusion; brittle.

**Chosen guarantee.** fexc emits a site record for every admitted native guest-memory instruction and every guard stub.
At that native PC the guest state is exactly the state before the site's guest instruction, at locations the record
names, and none of that instruction's effects is committed. FEX patches that produce it:
1. Site export at emission (`JIT/MemoryOps.cpp` `LoadMem`, `StoreMem`, `LoadMemPair`, `StoreMemPair`, `Push`, `Pop`):
   native offset of the faulting word, current guest instruction (tracked in `JIT/MiscOps.cpp::GuestOpcode`, dense
   table on), IR access shape. Any other guest-memory IR op makes the compile `unsupported_form`.
2. Flag elimination treats every guest-memory op as reading all six flags.
3. No guest-memory op lies between the definition of a value and its store to a static register. The RA resets
   `PreferredReg` when one intervenes; this also covers pooled constants. Guest-memory destinations (`ldr`/`ldp`
   results and `Pop`'s value) are never allocated to SRA registers (risk 2). Writeback bases (push/pop on x8) and
   base/index registers are relied on to be unchanged after an abort.
4. `TryPostRAMerge` resets its merge candidate at `OP_GUESTOPCODE`, so no fusion crosses guest instructions.
5. Pre-NZCV: when an instruction writes NZCV and then emits any access site, `GenerateIR` inserts
   `mrs x0,nzcv; str w0,[x28,#natemu_pre_nzcv]` at the instruction's start (IR cursor saved at its `GuestOpcode`), and
   those sites say `nzcv=frame`. This is not deferred: with one vCPU an RMW load can still hit a read-only shadow, so
   its store can fault into a guest #PF. CF inversion is recorded at each instruction start.
6. Intent from the dispatcher: the load of a memory-destination RMW operand is marked `write` intent.
7. Address guard (ABI §2) before each non-RIP-relative access, flag-free: `eor x0, xA, xA, lsl #16; tbnz x0, #63, stub`
   (xA = base, or x0 after `add x0, base, disp|index`). It catches bit 63 ≠ bit 47, covering the private region; other
   non-canonical values take level-0 translation faults and canonical-end overruns land in the unmapped 1 GiB below
   `PRIVATE`, both classified at the site. RIP-relative addresses are checked at compile time; this relies on admission requiring every guest-RIP
   relocation to equal the compile RIP's value (a future relocating code cache must re-check). Stubs (`brk`) are sites.
8. Split check for every guest store wider than 1 byte, including RMW, push/call and pair stores:
   `add x0, a, #w-1; eor x0, x0, a; tbnz x0, #12, split`. The split stub is a service site: the runtime walks both pages
   with write intent, writes both parts through owned RAM or neither (#PF), applies any recorded writeback, and resumes
   after the store (`ACCESS_COMPLETED`). (Corrected Oct 27 after S1d1 review: an RMW load can hit existing readable
   shadows without faulting, so only its store faults and Arm permits a partial commit, DDI0487 B2.8.2.1.)

Site record (natemu-fexc-v2 JSON `sites` and binary object): `native_offset u32, guest_offset u32, kind
(load|store|load_pair|store_pair|push|pop|guard|split), width, intent (read|write), segment (ds|ss), base_reg,
index_reg|none, extend/shift, imm i32, data_regs, writeback, nzcv (pstate|frame), cf_inverted, completed_effects=none`.
GPRs/PF/AF use the object's SRA map. Non-canonical with `segment=ss` (RSP/RBP base) → #SS(0), else #GP(0).

## 2. Slice-1 code-object contract

**Compiler profile `slice1`** (`fexc.cpp` `Settings`): 64-bit, dense PC table, multiblock off, FlagM required; end marker
off (its `udf #0x420f` precedes every exit in `ExitFunction` and would trap); TSO emulation off (moot with one vCPU;
before two, show Apple TSO mode covers SIMD/pair/LSE accesses). Admission checks feature bits against the vCPU's ID registers.

**Block cutting (producer).** Before dispatching each instruction, `GenerateIR` checks a natemu allowlist against FEX's
own decode (`TableInfo`, `DecodedInst` operands/prefixes). Admitted: mov/movzx/movsx/movabs/lea, ALU, shift/rotate,
inc/dec/neg/not, imul/mul, div/idiv (after S1f), setcc/cmovcc, push/pop, register xchg, bswap, bt-family, bsf/bsr,
cbw..cqo. Operands are register, memory or immediate, with no LOCK, REP or FS/GS override. Memory `xchg` (`swpal`,
implicit LOCK) is cut until atomics are admitted, and later swap sites must not check WnR strictly. jmp/jcc (direct,
indirect) and call/ret end the block. Any other instruction ends the block *before* it (`ExitFunction` to its RIP;
`end.reason` is `system_instruction` for the plan's system list, else `unsupported_form`). A compile at such a RIP
returns no code, and slice 1 stops with that reason. The runtime never re-decodes.

**Exits.** Enable FEX's existing `BLOCK_DEBUGGING` branch of `BranchOps.cpp::ExitFunction` under `slice1`:
`str rip,[x28,#State.rip]; ldr Pointers.DispatcherLoopTop; br`, with the inline RIP as a literal. It is gated on a
runtime profile flag, not the global constant, which `Core.cpp::CompileBlock` would turn into one instruction per block.
This removes block linking, the L1 lookup and the x25 call-ret stack. One resident gate serves every exit: it reads
`State.rip`. After an exit: guest GPRs in SRA registers, NZCV with CF inverted (block-end rectify), PF/AF in x26/x27.

**Admission.** The host fetches source bytes through the guest walk, runs fexc and checks version, sizes, offsets,
sorted in-body sites, guest-RIP relocations equal to the requested RIP's values, and x28 offsets within
{`InlineJITBlockHeader`, `State.rip`, `natemu_pre_nzcv`, `Pointers.DispatcherLoopTop`, after S1f `LUDIVHandler/LDIVHandler`}.
It writes the object to resident staging at the service stop; the resident copies it into a private slot (RW alias, RX
mapping, I-cache maintenance) and fills every native pointer itself.

**State ownership.** natemu's `GuestState` bank (abi `recovery.rs`) is authoritative while no code runs. While code runs
the root map is the SRA map plus NZCV/x26/x27, and the bank's copies of those fields are stale. The FEX `CpuStateFrame`
at x28 is private per-vCPU plumbing (pointers, exit RIP, pre-NZCV slot), never guest state: no admitted instruction
writes DF or segment context. Entry (`gates.S` ERET path): SRA registers from the bank; NZCV from RFLAGS with C = !CF;
x26 = PF ^ 1; x27 = (AF << 4) ^ x26; x28 = frame; SP = private stack top with room for `spill_bytes` (the prologue
subtracts it, `ResetStack` restores it); PC = slot + entry offset. Exit: full capture, inverse mapping, RIP from frame.

**Faults.** The landing captures state and binary-searches `ELR - slot base` in the sites; memory sites check ESR and
FAR within base+offset..+width (unknown PC or mismatch contains the VM). A guard stub, or a level-0/gap translation fault
at a memory site, gives #GP(0)/#SS(0) by segment. Otherwise walk each touched page with the site's intent and the
committed CPL, CR0.WP, EFER.NXE, SMEP, SMAP. `RETRY_ACCESS`: install the shadow (writable only for write intent with D
set; a read-only → writable upgrade invalidates the TLB entry first; translated source pages stay read-only, a write to
one stops `unsupported_smc`) and ERET to the same PC. `GUEST_FAULT`: canonical state from the frame (SRA map; NZCV from
PSTATE or frame slot with `cf_inverted`; PF/AF formulas), RIP = block RIP + guest_offset, CR2/error code per risk 4,
exception packet to the host (no IDT delivery yet). `COMPLETE` cannot arise (at most one store per instruction, last).

**Helpers and limits.** None before S1f's pure LUDIV/LDIV 128/64 routines (x0-x3 only; other registers and NZCV kept;
no guest memory or faults). One vCPU, no chaining, one host round trip per block, no event delivery; FS/GS, LOCK (incl.
memory xchg), string, x87/SSE, CPUID/RDTSC and system instructions are cut.

**Prerequisite (gates S1d).** vm-map §6 step 3: a general resident/shadow table builder replacing `shadow.rs` (one 2 MiB
window per half today) with table-page allocation, capacity limits (exhaustion evicts or contains, never guesses) and TLB
invalidation on read-only → writable upgrade; plus §6 step 2's single guest-memory accessor.

## 3. Chunks, in order S1a → S1c → S1d → S1b → S1e → S1f (producer `tools/fexc`; consumer abi/runtime/host)
- **S1a, producer site export.** Patches 1 and 6; JSON `natemu-fexc-v2` `sites`, `spill_bytes`, `cf_inverted` per PC
  entry; `--object FILE` writes the binary record in `crates/abi/src/code_object.rs` (`offset_of!`-checked). Pin the
  DDI0487 text on load-destination and writeback state after a synchronous data abort, and on split-store commits, as a
  source snapshot (open if unavailable offline). *Check:* new `tools/fexc/check_sites.py` disassembles the 64
  `fixtures/slice1` blocks (`xcrun objdump`). Load/store words with a base other than x28/sp, excluding PC-relative
  literal loads, must equal the site set. The expected count includes the implicit stack accesses of push/pop. Each site
  must map to a memory-operand instruction in the fixture `asm`, at the same width.
- **S1c, producer cutting and exits.** Profile `slice1`, allowlist, `BLOCK_DEBUGGING` exit flag, feature-profile record.
  *Check:* each system class (`mov cr3`, `lgdt`, `in`, `hlt`, `cli`, `int`, `iretq`, `syscall`, `wrmsr`, `invlpg`) and
  memory `xchg` placed mid-window ends the block at its RIP with the right reason, and gives no code at window start. No
  `state_pointer_loads` outside the admitted set appear, and every exit has the single-gate form.
- **S1d, consumer: admit, execute, retry.** `code_object.rs` validation (unit tests: bounds, overflow, unsorted sites,
  feature mismatch); runtime slot, frame, entry, exit gate and the site-driven fault path with RETRY. The host test
  driver builds 4-level guest tables for each program's pages and loops compile → admit → execute. *Check:* fixtures
  match `expected.json` under `ref.compare` (division stops `unsupported_form` until S1f). PTE A/D bits equal the
  touched and written page sets. Retries > 0, including a read-only → writable upgrade.
- **S1b, producer precision.** Patches 2-5, 7 and 8 plus a verifier. Between a site's instruction start and the site:
  no write to x4-x17, x19, x29, x26 or x27; no NZCV write unless `nzcv=frame`; at most one store site per instruction,
  and last. Add a fixture where a constant is pooled before a memory access and later stored to a GPR. *Check:*
  `check_sites.py --precision` on the fixtures and a fresh `gen_slice1.py --seed` corpus. S1d still passes. Report the
  native instruction-count and run-time delta of the patches, the guard and the split check.
- **S1e, consumer guest faults.** Extend `ref.py` with a prefix mode that runs bytes [0, k) of a program. For each
  fixture and memory instruction k, clear the PTE of the page k touches first; the effective address comes from QEMU's
  prefix state and the generator `asm`, not FEX. *Check:* GPRs, masked RFLAGS and memory equal the prefix run; RIP =
  rip + offset(k); CR2/error code per policy; nothing later ran. Also: fetch #PF at a not-present code page (I bit);
  page-crossing pure stores (both parts or neither); non-canonical probes incl. `0x0000_8000_4000_0000` give #GP(0) (#SS(0)
  for RSP/RBP bases) with private memory and frame byte-identical.
- **S1f, division.** Resident LUDIV/LDIV; a DIV/IDIV guard (zero divisor; DIV high >= divisor; IDIV overflow reported
  by the helper) exits through a natemu `Break` site with the guest offset. *Check:* division fixtures pass; zero and
  overflow cases give a #DE packet whose state equals the QEMU prefix run.

## 4. Open risks and owner decisions

1. **Isolation cost.** The producer guard (patch 7) enforces ABI §2 at 2-3 instructions per access, measured in S1b.
   PAN cannot be the mechanism, because generated code itself touches private memory (x28 frame, spill area, literals).
   It may be added for defence in depth: with SCTLR.SPAN=1 the exception vectors must clear PAN explicitly.
2. **Load destination after abort.** Patch 3 stays strict until the pinned DDI0487 text shows destinations survive. The
   cost is one extra `mov` per memory-to-register load. Base/index/writeback restoration is relied on.
3. **Split-store path.** It is the only `ACCESS_COMPLETED` path in slice 1 and needs its own writeback record. Its
   frequency and cost are unmeasured.
4. **natemu policies, not SDM-defined** (implemented in S1e; QEMU Linux-user mode cannot check them). A read fault in
   a non-locked RMW instruction reports W=1. CR2 for a split access is the first byte of the faulting page; when both
   pages fault, the first (address order) is reported. After a failed crossing store or RMW second-page fault, the first
   page's A/D are already set (no byte written; beyond SDM 4.8, QEMU softmmu order). bt-family memory forms access one
   byte. A branch to a non-canonical target is #GP(0) at the branch (Intel); falling through into non-canonical code is
   #GP(0) at the first instruction with a non-canonical byte (the saved RIP may itself be non-canonical). #GP/#SS
   records carry CR2 = 0. The U bit comes from the committed CPL, with WP/SMEP/SMAP applied by the walker. Admission
   does not yet check guard structure: isolation rests on the producer and its verifier (hardening item).
5. **TSO evidence before two vCPUs** (§2); upkeep of patches to upstream RA, flag elimination and dispatcher. Slice 1
   drops block linking, L1 lookup, call-ret stack and push/pop fusion (code-cache step restores them). No scope change.
