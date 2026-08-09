I'll explore the FEX source systematically.
I read the tree at `/tmp/fexread/FEX` (HEAD = `fd141ed`). Here is what the code actually does, with citations.

---

## 1. Where signal handlers are installed, and the recovery path

**Installation** is entirely in the frontend (`LinuxEmulation`), not FEXCore. FEXCore never calls `sigaction`; it only *exports a config blob* describing its dispatcher/SRA layout.

- Host `sigaction` is installed via `SYS_rt_sigaction` in `/tmp/fexread/FEX/Source/Tools/LinuxEmulation/LinuxSyscalls/SignalDelegator.cpp:825` (from `InstallHostThunk`, line 749), with a single global thunk `SignalHandlerThunk` at `SignalDelegator.cpp:60-65`.
- The thunk recovers the thread object from **8 bytes below the sigaltstack base** — `GetThreadFromAltStack`, `SignalDelegator.cpp:53-58`, planted at `SignalDelegator.cpp:1015-1021`. There is no TLS lookup in the fault path; alt-stack is mandatory (`sigaltstack` at `:1027`).
- Dispatch chain: `SignalDelegator::HandleSignal` (`:77-99`) runs a vector of host handlers, then a frontend handler, then falls through to `HandleGuestSignal` (`:592`).
- Registrations: `SIGILL` and `SIGSEGV` at `SignalDelegator.cpp:958-959`; `SIGBUS` at `:984` (arm64 only); pause signal at `:987`; the per-guest-signal handler for all signals at `:990-992`. The **SMC / shadow-page handler** is registered separately and later, at `/tmp/fexread/FEX/Source/Tools/LinuxEmulation/LinuxSyscalls/Syscalls.cpp:797` (`SyscallHandler::HandleSegfault`). Order matters: `HandleFrontendSIGSEGV` (`SignalDelegator.cpp:571`) runs *before* `HandleSegfault`.

**Recovery when a SIGSEGV/SIGBUS lands inside JIT code** — `SignalDelegator::SpillSRA`, `/tmp/fexread/FEX/Source/Tools/LinuxEmulation/LinuxSyscalls/SignalDelegator.cpp:106-137`:

```cpp
Thread->CurrentFrame->State.rip = CTX->RestoreRIPFromHostPC(Thread, ArchHelpers::Context::GetPc(ucontext));
for (size_t i = 0; i < Config.SRAGPRCount; i++) {
  const uint8_t SRAIdxMap = Config.SRAGPRMapping[i];
  ...
  Thread->CurrentFrame->State.gregs[i] = ArchHelpers::Context::GetArmReg(ucontext, SRAIdxMap);
}
```

then FPRs (`:119-131`) and EFLAGS via `ReconstructCompactedEFLAGS` / `SetFlagsFromCompactedEFLAGS` (`:133-135`).

Gate on "was I in the JIT?" is `CTX->IsAddressInCodeBuffer(Thread, OldPC)` — `SignalDelegator.cpp:295`, implemented as a **linear scan over code buffers** at `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/CPUBackend.cpp:340-359` plus `/tmp/fexread/FEX/FEXCore/Source/Interface/Context/Context.cpp:55-57`.

---

## 2. THE CRITICAL QUESTION: side table vs. always-in-memory CPUState

**Answer: neither, and this is the single most important fact for your retrofit.**

FEX uses a **static register allocation (SRA)**: 16 guest GPRs + PF/AF live *permanently* in fixed host ARM64 registers for the entire duration of JIT execution, and 16 guest XMMs live in fixed host vector registers. So the CPUState in memory is **stale** whenever you're in JIT code.

- SRA map: `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp:47-67` (`x64::SRA`, 18 entries: x4,x7,x5,x6,x8..x17,x19,x29, then `REG_PF`=r9, `REG_AF`=r24), `:93-99` (`x64::SRAFPR`, 16), `:234-246` (x32, 10). Comment at `:64`: "PF/AF must be last." The count exported to the signal delegator deliberately drops those two: `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp:2647-2648` — `// PF/AF are the final two SRA registers. We only want GPRs`.
- Mapping is a pure linear index: `Arm64Emitter.cpp:407-419`, `// X86 Registers are mapped linerally from the StaticRegisters span.`

**RIP is NOT in a host register and is NOT a global side table.** It is reconstructed from **per-block inline metadata appended to the JIT code itself**:

`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/Core.cpp:139-175`, `ContextImpl::RestoreRIPFromHostPC`:

```cpp
const uint64_t BlockBegin = Frame->State.InlineJITBlockHeader;
auto [InlineHeader, InlineTail] = GetFrameBlockInfo(Thread->CurrentFrame);
if (InlineHeader) {
  if (HostPC >= BlockBegin && HostPC < BlockBegin + InlineTail->Size) {
    auto RIPEntry = ...InlineTail->OffsetToRIPEntries;
    uint64_t StartingHostPC = BlockBegin;
    uint64_t StartingGuestRIP = InlineTail->RIP;
    for (uint32_t i = 0; i < InlineTail->NumberOfRIPEntries; ++i) {
      auto Offset = FEXCore::Utils::vl64pair::Decode(RIPEntry);
      RIPEntry += Offset.Size;
      if (HostPC >= (StartingHostPC + Offset.IntegerARMPC)) { ... accumulate ... }
      else break;
    }
    return StartingGuestRIP;
  }
}
return Frame->State.rip;   // fallback
```

The anchor `Frame->State.InlineJITBlockHeader` is a **field of the guest CPUState** (`/tmp/fexread/FEX/FEXCore/include/FEXCore/Core/CoreState.h:91`) which the JIT stores **on every block entry point**, two instructions:

`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/JIT/JIT.cpp:794-798`
```cpp
// Get the address of the JITCodeHeader and store in to the core state.
// Two instruction cost, each 1 cycle.
adr_OrRestart(TMP1, &HeaderLabel);
str(TMP1, STATE, offsetof(FEXCore::Core::CPUState, InlineJITBlockHeader));
```

So the actual model is: **"which block am I in" is kept live in memory at all times (2 insns/entry), and "where in the block" is recovered by decoding a compressed per-block varint table."** Guest GPR/XMM values need no reconstruction beyond copying host regs out of `ucontext` — but they *do* need that copy, and that copy is not free.

`RestoreRIPFromHostPC` is declared at `/tmp/fexread/FEX/FEXCore/include/FEXCore/Core/Context.h:88` and used by Linux (`SignalDelegator.cpp:108`), Seccomp (`/tmp/fexread/FEX/Source/Tools/LinuxEmulation/LinuxSyscalls/Seccomp/SeccompEmulator.cpp:328`), WOW64 (`/tmp/fexread/FEX/Source/Windows/WOW64/Module.cpp:307`), ARM64EC (`/tmp/fexread/FEX/Source/Windows/ARM64EC/Module.cpp:393`), and Windows invalidation (`/tmp/fexread/FEX/Source/Windows/Common/InvalidationTracker.cpp:275`).

**There is no host→guest side table anywhere.** The `LookupCache` is guest→host only: `fextl::robin_map<uint64_t, BlockEntry> BlockList` at `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/LookupCache.h:89`, `AddBlockMapping` `:96`, `FindBlock` `:105`. Grepping `FindHostCodeForRIP` / `LookupHostToGuest` returns nothing — those names don't exist. `HostToGuest*` hits are all thunk-library trampolines, unrelated.

---

## 3. Register allocator ↔ faults: what walks host GPRs back into CPUState

Yes — guest registers are live in host registers at every fault point, and the walk-back is exactly the loop in `SpillSRA` (`SignalDelegator.cpp:110-131`) using the config built in `Dispatcher::MakeSignalDelegatorConfig` (`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp:2646-2691`).

Flags are the ugly part — `ContextImpl::ReconstructCompactedEFLAGS`, `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/Core.cpp:177-251`:
- NZCV comes from **host PSTATE**, not memory: `Core.cpp:200-204`.
- PF/AF come from **host GPRs** r9/r24: `Core.cpp:206-209` (`Frame->State.pf_raw = HostGPRs[CPU::REG_PF.Idx()]`).
- CF is stored inverted and must be un-inverted: `Core.cpp:221-222`.
- PF and AF are *deferred/lazy* and get materialized here with a popcount: `Core.cpp:230-239`.
- DF is stored pre-transformed as 1/-1: `Core.cpp:244-248`.

x87/MMX are **not** in host registers — they're flushed to CPUState by the register cache. `ReconstructX87*` in FEXCore is unrelated to fault handling; it's `ReconstructX87StateFromFSW_Helper`, a guest-level FSW→Top helper in `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/X87.cpp:421`.

**Crucially, the register cache is flushed at every guest-instruction boundary** — `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/Core.cpp:622-634`:

```cpp
// Do a partial register cache flush before every instruction. This
// prevents cross-instruction static register caching, ...
// However, it makes our exception handling behaviour more predictable.
// It is potentially correctness bearing in that sense, but that is a
// side effect here ...
Thread->OpDispatcher->FlushRegisterCache(true);
```

Read that comment skeptically: the FEX developers themselves say the property your system emulator would depend on (SRA regs hold architecturally-committed values at fault points) is a **side effect of an RA heuristic**, not an enforced invariant. `FlushRegisterCache` is at `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.h:1166-1215`.

There is also a syscall-partial-spill wrinkle: if the fault lands inside a syscall trampoline, some SRA regs are already spilled and must not be double-spilled — `IgnoreMask = Frame->InSyscallInfo & 0xFFFF` at `SignalDelegator.cpp:303-312`.

---

## 4. Restartable faults

FEX has **three distinct restart mechanisms**, none of which is a general "re-execute the faulting guest instruction" facility. Guest SIGSEGV proper is *not* restartable — it is delivered to the guest handler.

### (a) Unaligned-access backpatch — a true instruction-level restart

Handler: `SignalDelegator.cpp:963-984`. It filters to `si_code == BUS_ADRALN` and PC-in-code-buffer (`:965-974`), then:

```cpp
const auto Result = FEXCore::ArchHelpers::Arm64::HandleUnalignedAccess(Thread, ..., PC, ArchHelpers::Context::GetArmGPRs(ucontext));
ArchHelpers::Context::SetPc(ucontext, PC + Result.value_or(0));
return Result.has_value();
```
(`SignalDelegator.cpp:978-981`)

The **return value is a signed PC delta**, `std::optional<int32_t>` (`/tmp/fexread/FEX/FEXCore/include/FEXCore/Utils/ArchHelpers/Arm64.h:35-36`), and this is the whole restart protocol:

- `+4` = emulate-and-skip (the handler did the access in software; e.g. CASPAL `/tmp/fexread/FEX/FEXCore/Source/Utils/ArchHelpers/Arm64.cpp:2055-2057`, atomic mem ops `:2076-2078`).
- `0` = **re-execute the same host instruction**, after rewriting it. `Arm64.cpp:2108-2121`: LDAR/LDAPR is rewritten in place to `LDR`, with a half-barrier `DMB_LD` back-patched into `PC[1]`, `ClearICache(&PC[0], 8)`, then `// With the instruction modified, now execute again. return 0;`
- `-4` = **back up one instruction** so the newly-patched preceding `DMB` also executes. `Arm64.cpp:2122-2133` for STLR→STR: `std::atomic_ref<uint32_t>(PC[-1]).store(DMB, ...)` then `// Back up one instruction and have another go / return -4;`. Same for STLUR at `:2149-2162`.
- Cross-thread safety: the whole backpatch region is under a per-block futex spinlock stored **inside the JIT block tail** — `FEXCore::Utils::SpinWaitLock::UniqueSpinMutex lk(&InlineTail->SpinLockFutex);` at `Arm64.cpp:2106`, with `SpinLockFutex` declared in `JITCodeTail` (`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/CPUBackend.h:89-90`). Lines `:2165-2205` handle "another thread already backpatched this."

So: **this is a host-instruction restart, not a guest-instruction restart.** No guest state is reconstructed at all on this path — `SpillSRA`/`RestoreRIPFromHostPC` are never called. It works precisely because the host instruction is idempotent up to the point of fault. That property does **not** generalize to an MMIO or shadow-PTE-miss handler that needs to synthesize a value into a guest register and then continue.

### (b) SMC / write-to-code-page — a *guest-instruction* restart, via single-step re-entry

`/tmp/fexread/FEX/Source/Tools/LinuxEmulation/LinuxSyscalls/SyscallsSMCTracking.cpp:119-130`:

```cpp
if (CTX->IsAddressInCodeBuffer(...) && !CTX->IsCurrentBlockSingleInst(Thread) &&
    CTX->IsAddressInCurrentBlock(Thread, FaultAddress & FEX_PAGE_MASK, FEX_PAGE_SIZE)) {
  // ... reconstruct the context and repeat the faulting instruction as a single-instruction block ...
  ThreadObject->SignalInfo.Delegator->SpillSRA(Thread, ucontext, Thread->CurrentFrame->InSyscallInfo & 0xFFFF);
  ArchHelpers::Context::SetPc(ucontext, Config.AbsoluteLoopTopAddressFillSRA);
  ArchHelpers::Context::SetArmReg(ucontext, 1, 1); // ENTRY_FILL_SRA_SINGLE_INST_REG to force a single step
}
```

This *is* the restart pattern you'd want: full state reconstruction → return to dispatcher top → recompile/re-enter the faulting guest RIP as a **1-instruction block**. The single-step register is `ENTRY_FILL_SRA_SINGLE_INST_REG = TMP2` (`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.h:92-94`). `IsCurrentBlockSingleInst` / `IsAddressInCurrentBlock` are `Core.cpp:124-132` (they read the same inline tail). Same idiom on Windows: `/tmp/fexread/FEX/Source/Windows/WOW64/Module.cpp:904-907`.

**Note the correctness caveat**: the guest instruction is re-executed *from its start* with the SRA values as spilled. That is only sound if the faulting host store is the instruction's first architecturally-visible effect. Nothing enforces that — `FlushRegisterCache(true)` at `Core.cpp:634` is the only thing making it usually true.

### (c) Deferred-signal / interrupt fault page — asynchronous only

`InterruptFaultPage` is `mprotect(PROT_NONE)`'d and the JIT stores a zero byte to it at safe points (`Dispatcher.cpp:278-281`; per-entry-point at `JIT.cpp:770-780`). Async signals arriving inside a critical section are queued (`SignalDelegator.cpp:639-665`) and replayed when the refcount hits zero (`:598-626`). Nested case just bumps PC by 4 (`:628-632`). This defers delivery; it does not restart anything.

There's also a JIT guard-page overflow path that does a raw `longjmp` out of the JIT (`SignalDelegator.cpp:579-586`, `ManuallyLoadJumpBuf`) and a syscall-argument fault-safe path `FaultSafeUserMemAccess::TryHandleSafeFault` (`SignalDelegator.cpp:574`, `/tmp/fexread/FEX/Source/Tools/LinuxEmulation/LinuxSyscalls/Syscalls.h:621-623`).

### (d) Guest signal delivery (the non-restart path)

`HandleDispatcherGuestSignal` (`SignalDelegator.cpp:284`): `StoreThreadState` snapshots the *entire* `CPUState` by memcpy onto the host stack (`:161-164`, comment `// We can't guarantee if registers are in context or host GPRs / So we need to save everything`), spills SRA (`:316`), builds a guest signal frame, sets `Frame->State.rip` to the guest handler (`:372`), zeroes guest FPU state (`:389-395`), and re-enters the dispatcher at `AbsoluteLoopTopAddressFillSRA` (`:398`). Return is via a `hlt(0)` SIGILL trampoline (`Dispatcher.cpp:386-391`) caught by `HandleSIGILL` (`SignalDelegator.cpp:406-437`) → `RestoreThreadState` (`:182-282`).

`RestoreThreadState` contains an explicit admission of the limits of this design, at `SignalDelegator.cpp:266-270`:

```cpp
if (Context->Flags & ContextFlags::CONTEXT_FLAG_INJIT) {
  // XXX: Unsupported since it needs state reconstruction
  // If we are in the JIT then SRA might need to be restored to values from the context
  // We can't currently support this since it might result in tearing without real state reconstruction
}
```

i.e. **a guest handler that modifies `uc_mcontext` GPRs and returns into JIT code is silently ignored.**

Guest-synthesized faults (`#UD`, `#GP` from the dispatcher, division, etc.) go through the `Break` IR op, which writes an 8-byte `SynchronousFaultData` blob into the frame and branches to a per-signal dispatcher stub: `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/JIT/MiscOps.cpp:59-97`; stubs at `Dispatcher.cpp:393-433` (SIGSEGV is synthesized by `ldr x1, [x1=0]`, `:429-432`). Struct at `/tmp/fexread/FEX/FEXCore/include/FEXCore/Core/CoreState.h:419-426`.

---

## 5. REP string instructions

**FEX explicitly does not support partial/faulting REP.** Verbatim, `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp:3233-3235`:

```cpp
// FEX doesn't support partial faulting REP instructions.
// Converting this to a `MemSet` IR op optimizes this quite significantly in our codegen.
// If FEX is to gain support for faulting REP instructions, then this implementation needs to change significantly.
```

`REP STOS` becomes a single `_MemSet` IR op with `RCX` forced to 0 afterward (`OpcodeDispatcher.cpp:3244-3246`); `REP MOVS` becomes `_MemCpy` likewise (`:3278, 3288-3290`). Both then generate a **host-side loop using scratch registers only** — `DEF_OP(MemSet)` at `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp:1851-1899` moves length/address into `TMP1`/`TMP2` (`:1878-1884`) and post-index stores through `TMP2` (`:1891-1898`). `DEF_OP(MemCpy)` is at `MemoryOps.cpp:2086`.

Consequence: if the copy faults on element *k*, `TMP1`/`TMP2` are not SRA registers, so `SpillSRA` recovers **the pre-instruction `RCX`/`RDI`/`RSI`**, and `RestoreRIPFromHostPC` returns the RIP of the `REP MOVS` itself. Partial progress is architecturally invisible; the guest sees a fault as if nothing had been copied, even though memory *was* partially written. For a system emulator where a REP MOVS straddling an unmapped/MMIO page is routine, this is a hard blocker requiring the "change significantly" the comment anticipates.

---

## 6. Cost structure of the reconstruction path

Not a hash. Not a binary search. **Per-block inline metadata + a linear varint walk.**

Data structures, `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/CPUBackend.h:63-96`:
```cpp
struct JITCodeHeader { uint32_t OffsetToBlockTail; };
struct JITCodeTail {
  size_t Size; uint64_t RIP; size_t GuestSize;
  uint32_t NumberOfRIPEntries; uint32_t OffsetToRIPEntries;
  uint32_t SpinLockFutex; bool SingleInst; uint8_t _Pad[3];
};
```

Emission and the design rationale, `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/JIT/JIT.cpp:1019-1054`:
```
// Packed using two variable length integer entries to ensure the size isn't too large.
// ... each entry is relative to each other instead of absolute offset from the start of the JIT block.
// When reconstructing the RIP, each entry must be walked linearly and accumulated with the previous entries.
// This is a trade-off between compression inside the JIT code space and execution time when reconstruction the RIP.
// RIP reconstruction when faulting is less likely so we are requiring the accumulation.
```

**That last sentence is the whole ballgame for your retrofit: the format was chosen on the explicit assumption that faults are rare.**

Encoding is `vl64pair` (1/2/4/8-byte-class packed `(hostPCdelta, guestRIPdelta)`), `/tmp/fexread/FEX/FEXCore/Source/Utils/variable_length_integer.h:194-230`, encode call at `JIT.cpp:1050`, decode at `Core.cpp:158`.

Cost accounting per JIT-code fault:
1. `IsAddressInCodeBuffer` — linear over `CurrentCodeBuffer` + `SignalHandlerCodeBuffers` (`CPUBackend.cpp:340-359`), called at least twice (`SignalDelegator.cpp:295` and `:965`).
2. `RestoreRIPFromHostPC` — **O(entries-before-fault)** sequential varint decode with a data-dependent branch per entry (`Core.cpp:157-168`). Not O(log n), no early index.
3. 16 GPR + 16 FPR `memcpy`s out of `ucontext` (`SignalDelegator.cpp:110-131`).
4. EFLAGS: 22-iteration loop + popcount + several bit-fiddles (`Core.cpp:183-250`).
5. For the guest-delivery path, an additional **full `sizeof(FEXCore::Core::CPUState)` memcpy** onto the host stack (`SignalDelegator.cpp:164`) and another back on restore (`:261`).

Mitigating factor on entry-count: RIP entries are only emitted for instructions that can fault or end a block, not every instruction — `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/Core.cpp:636-637` gated by `CanHaveSideEffects` (`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.h:248-278`: any memory-touching operand, `FLAGS_DEBUG_MEM_ACCESS`, `FLAGS_SETS_RIP`, or `FLAGS_BLOCK_END`). Emitted as `IROp_GuestOpcode` → `DebugData->GuestOpcodes` (`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/JIT/MiscOps.cpp:42-46`, plus block entry points at `JIT.cpp:939`). Still, for a memory-heavy block, essentially every instruction gets an entry, so the walk length ≈ instruction index within the block.

FEX itself instruments this as a known cost: `FEXCORE_PROFILE_ACCUMULATION(..., AccumulatedSignalTime)` at `SignalDelegator.cpp:63`, `AccumulatedSIGBUSCount` at `:976`, `AccumulatedSMCCount` at `SyscallsSMCTracking.cpp:116`.

---

## Bottom line for a system-level retrofit

1. **No side table to reuse or invert.** Host→guest exists only as a per-block varint walk anchored on a CPUState field written at every block entry. If you want O(1) host-PC→guest-RIP, you're adding a new structure.
2. **Every fault requires a full SRA spill** (32 register copies + lazy-flag materialization). There is no cheap "just tell me the faulting guest address/register" path.
3. **Restart exists but only in two narrow, hand-built forms**: host-instruction backpatch-and-retry for `BUS_ADRALN` (`Arm64.cpp:2106-2162`), and reconstruct-then-single-step-recompile for SMC (`SyscallsSMCTracking.cpp:119-130`). The latter is the template you'd generalize for shadow-PTE misses; the cost is a full state spill plus a dispatcher round-trip plus a 1-instruction block compile per miss.
4. **MMIO with a value returned into a guest register has no supported path.** `RestoreThreadState` `SignalDelegator.cpp:266-270` says modifying JIT-resident register state from a fault handler "might result in tearing without real state reconstruction" and is unimplemented.
5. **REP string ops are non-restartable by construction** (`OpcodeDispatcher.cpp:3233`), and the JIT loop keeps its cursor in scratch registers (`MemoryOps.cpp:1878-1898`), so partial progress is unrecoverable.
6. **The metadata format is explicitly optimized against your workload** (`JIT.cpp:1025-1026`).