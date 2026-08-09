I'll explore the FEX-Emu source systematically.
Findings below. Source tree: `/tmp/fexread/FEX` @ `fd141ed6d721d03062619e4702bca1a0c93b6dd9` (Aug 6 2026). Note this is a *recent* FEX with a persistent on-disk code cache (`FEXCore/Source/Interface/Core/CodeCache.cpp`) — that changes some answers vs. older FEX writeups.

---

## 1. The dispatcher

`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp` — there is only one dispatcher now (`class Dispatcher final : public Arm64Emitter`, `Dispatcher.h:36`); there is no separate `Arm64Dispatcher.cpp` in this revision. It is generated once per `ContextImpl` into a `MAX_DISPATCHER_CODE_SIZE` VirtualAlloc buffer (`Dispatcher.cpp:40`), *not* per thread.

Loop structure (`Dispatcher.cpp:54-342`):
- `Dispatcher.cpp:61` `DispatchPtr = GetCursorAddress<AsmDispatch>();` — entry, signature `void(*)(CpuStateFrame*, bool SingleInst)` (`Dispatcher.h:67`).
- `Dispatcher.cpp:90` `AbsoluteLoopTopAddressFillSRA` (re-entry that reloads static regs), `Dispatcher.cpp:144` `AbsoluteLoopTopAddress` (the real loop top, 16B aligned at `:141`).
- `Dispatcher.cpp:147` `ldr(RipReg, STATE_PTR(CpuStateFrame, State.rip));` — **the only lookup input is `State.rip`.**
- `Dispatcher.cpp:172-173`: TF (trap flag) check → `CompileSingleStep`.

The probe sequence in the dispatcher is **L2 only** (L1 is probed at block ends instead, see §3). `Dispatcher.cpp:182-235`:

```
ldr(TMP1, STATE_PTR(CpuStateFrame, Pointers.L2Pointer));          // :182
and_(..., TMP4, RipReg, VirtualMemorySize - 1);                    // :187 mask to VA size
lsr(..., TMP2, TMP4, 12);                                          // :195 page index
ldr(TMP1, TMP1, TMP2, LSL_64, 3);                                  // :198 page-table load
cbz(TMP1, &NoBlock);                                               // :201
and_(..., TMP2, TMP4, 0x0FFF);                                     // :204 byte offset in page
add(TMP1, TMP1, TMP2, LSL, ilog2(sizeof(LookupCacheEntry)));       // :207 (<<4)
ldp(TMP4, TMP2, TMP1, 0);                                          // :212 {HostCode, GuestCode}
sub(TMP2, TMP2, RipReg); cbnz(TMP2, &NoBlock);                     // :215-216 tag check
cbz(TMP4, &NoBlock);                                               // :219
... insert into L1 (State.L1Pointer/L1Mask, :224-231) ...
br(TMP4);                                                          // :234 enter block
```

Miss path `NoBlock` (`Dispatcher.cpp:311-341`): spill SRA, call `ContextImpl::CompileBlock(CTX, Frame, RIP, 0)`, `br(TMP1)` into the result.

Block exit back to dispatch: `Pointers.DispatcherLoopTop` (= `AbsoluteLoopTopAddress`, set at `Dispatcher.cpp:2616`), loaded and `br`'d in `FEXCore/Source/Interface/Core/JIT/BranchOps.cpp:212-231`. Block exits do **not** return via `ret` to a dispatcher trampoline; they jump.

`ExitFunctionLinkerAddress` (`Dispatcher.cpp:285-307`) is the shared stub that block-link thunks `blr` into: it spills SRA, calls `Arm64JITCore::ExitFunctionLink(Frame, Record)` with the record pointer in LR (`mov x1, lr`, `:290`), refills SRA, `br(TMP1)`.

---

## 2. Code cache / LookupCache — the key

**The key is the raw 64-bit guest RIP (virtual address). Nothing else.** No CPL, no CS.L/D bit, no CR3/ASID, no thread id, no FS/GS base, no CR0/EFER.

`FEXCore/Source/Interface/Core/LookupCache.h:161-164`:
```cpp
struct LookupCacheEntry {
  uintptr_t HostCode;
  uintptr_t GuestCode;   // <-- the entire tag
};
```
`LookupCache::FindBlock(InternalThreadState*, uint64_t Address)` — `LookupCache.h:176`. The `Thread` argument is used only for stats/locking, not for keying.

**Three levels:**

| Level | Structure | Where | Sizing |
|---|---|---|---|
| L1 | direct-mapped array of `LookupCacheEntry` (16 B), hash = `Address & L1PointerMask` | `LookupCache.h:178` | `MIN_L1_ENTRIES = 8*1024`, `MAX_L1_ENTRIES = 1*1024*1024` (`LookupCache.h:452-453`), i.e. 128 KB → 16 MB per thread. Dynamically resized by `UpdateDynamicL1Stats` (`LookupCache.h:231-272`) |
| L2 | two-level page table: `PagePointer[(Addr & (VirtualMemSize-1)) >> 12]` → 4096-entry array indexed by `Addr & 0xFFF` | `LookupCache.h:193-208` | `SIZE_PER_PAGE = 4096 * 16 = 64 KB` per guest page (`LookupCache.h:456`); pool `CODE_SIZE = 128 MB` (`:455`) → ~2048 guest code pages before `AllocateBackingForPage` returns 0 and the whole L2 is flushed (`LookupCache.h:426-438`, `:406-412`) |
| L3 | `fextl::robin_map<uint64_t, BlockEntry> BlockList` (full hash map, RIP → `{HostCode, CodePages}`) | `LookupCache.h:89`, `:105-111` | unbounded |

Hash functions: L1 is `Address & L1PointerMask` (mask = entry-count − 1, power of two; in the JIT it's pre-shifted by 4: `GetScaledL1PointerMask()`, `LookupCache.h:361-363`, stored in `CPUState::L1Mask`, `CoreState.h:107-108`). L2 index is `(Address & (VirtualMemSize-1)) >> 12` where `VirtualMemSize` defaults to `1ULL<<36` (64-bit) / `1ULL<<32` (32-bit) — `Context.h:339`, `Core.cpp:87`. That masking **aliases** addresses above the VA size; the full-`GuestCode` tag compare is what disambiguates (comment `LookupCache.h:210-211`). L3 is robin-hood hashing on the raw 64-bit RIP.

Total per-thread allocation: `TotalCacheSize = VirtualMemSize/4096*8 + CODE_SIZE + MAX_L1_SIZE` (`LookupCache.cpp:27`) ≈ 128 MB + 128 MB + 16 MB of VA at 64 GB VirtualMemSize.

**Retrofit implication:** to add a physical-address + mode key you'd have to widen `LookupCacheEntry` (breaking the single-`ldp` fast path at `Dispatcher.cpp:212` and `BranchOps.cpp:207`), and the whole L2 geometry is hard-wired to "guest VA page >> 12, byte offset within page" — it is literally a shadow of the guest virtual page table.

---

## 3. Block linking

Two distinct mechanisms.

**(a) Direct/inline-constant branches — patched branch instructions.** `FEXCore/Source/Interface/Core/JIT/BranchOps.cpp:46-234` `DEF_OP(ExitFunction)`. For a compile-time-known target it emits `EmitLinkedBranch(NewRIP, IsCall)` (`BranchOps.cpp:183`), which is defined at `FEXCore/Source/Interface/Core/JIT/JITClass.h:345-354`: it pushes a `PendingJumpThunks` entry and emits a single `b`/`bl` to a thunk.

Thunks are emitted after the whole multiblock, `FEXCore/Source/Interface/Core/JIT/JIT.cpp:980-998`:
```
BindOrRestart(&PendingJumpThunk.Label);
b_OrRestart(&l_DoLink);     // initially: skip the br
br(TMP1);
BindOrRestart(&l_DoLink);
ldr(TMP1, &l_ExitLink); blr(TMP1);
// ExitFunctionLinkData follows, JIT.cpp:993-997:
dc64(0);                                                   // HostCode
PlaceNamedSymbolLiteral(InsertGuestRIPLiteral(GuestRIP));  // GuestRIP (a RELOC_GUEST_RIP_LITERAL)
dc64(CallerAddress - ThunkAddress);                        // CallerOffset
```
`ExitFunctionLinkData` = `{uint64_t HostCode; uint64_t GuestRIP; int64_t CallerOffset;}` at `FEXCore/Source/Interface/Context/Context.h:50-54`. The full ASCII enumeration of unlinked/linked/out-of-range states is in the comment block at `BranchOps.cpp:102-159` — worth reading verbatim if you're porting.

Backpatching: `Arm64JITCore::ExitFunctionLink` at `FEXCore/Source/Interface/Core/JIT/JIT.cpp:531-612`.
- `:535` `auto GuestRip = Record->GuestRIP;` → `:546` `FindBlock` → `:551` `CompileBlock`.
- If within ±128 MB (`IsInt26`), it **overwrites the caller's `b`/`bl` in place** with a direct branch to host code (`JIT.cpp:577-593`, `std::atomic_ref<uint32_t>::store` + `ClearICache`).
- Otherwise it stores `HostCode` into the record and rewrites the thunk's first instruction to `ldr TMP1, <HostCode>` (`JIT.cpp:594-608`).
- Both paths register a delinker: `Thread->LookupCache->AddBlockLink(GuestRip, Record, delinker, lk)` (`JIT.cpp:584/588/608`).

**Undo on invalidation.** `GuestToHostMap::BlockLinks` is a `std::pmr::map<BlockLinkTag{GuestDestination, HostLink}, BlockDelinkerFunc>` (`LookupCache.h:58-82`), ordered so all links *to* a given guest destination are a contiguous range. `GuestToHostMap::Erase` (`LookupCache.h:113-123`):
```cpp
auto lower = BlockLinks->lower_bound({Address, nullptr});
auto upper = BlockLinks->upper_bound({Address, (ExitFunctionLinkData*)UINTPTR_MAX});
for (auto it = lower; it != upper; it = BlockLinks->erase(it)) it->second(it->first.HostLink);
BlockList.erase(Address);
```
The delinkers `DirectBlockDelinker` / `IndirectBlockDelinker` (`JIT.cpp:500-529`) restore the original `b`/`bl` to the thunk (or the thunk's `b 0x2`) and `ClearICache`. Note the `Record - 0x10` pointer arithmetic — the thunk layout is baked into the delinker.

**(b) Indirect branches / returns** are *not* linked; they inline an L1 probe at the block end (`BranchOps.cpp:199-215`) plus a `<GuestReturnRIP, HostReturnPC>` shadow call-ret stack (`REG_CALLRET_SP`, `BranchOps.cpp:192-197`, rationale at `BranchOps.cpp:95-100`). That shadow stack is wiped on any invalidation (`Core.cpp:992-997`).

---

## 4. Invalidation / SMC

**Granularity: 4 KB guest page, keyed on guest virtual page number.** `GuestToHostMap::CodePages` is `fextl::map<uint64_t /*VA>>12*/, fextl::vector<uint64_t /*block entry RIPs*/>>` (`LookupCache.h:91`), populated by `AddBlockExecutableRange` (`LookupCache.h:144-154`).

FEXCore side:
- `GuestToHostMap::InvalidateRange(Start, Length)` — `LookupCache.h:125-137`: walks `CodePages` in `[Start>>12, (Start+Len-1)>>12]` and `Erase`s every block entry (which also severs links, §3).
- `LookupCache::InvalidateCacheRange` — `LookupCache.h:333-347`, same for the thread-local L1/L2 via `CachedCodePages`.
- `ContextImpl::InvalidateCodeBuffersCodeRange` — `Core.cpp:969-983`: iterates *every live CodeBuffer's* `GuestToHostMap`.
- `ContextImpl::InvalidateThreadCachedCodeRange` — `Core.cpp:985-998`: per-thread L1/L2 + call-ret stack flush.
- Both assert `CodeInvalidationMutex` is held uniquely (`Core.cpp:972`, `:986`). That mutex is `ContextImpl::CodeInvalidationMutex` (`Context.h:367`, exposed via `GetCodeInvalidationMutex()`, `Context.h:223`); compilation takes it *shared* (`Core.cpp:853`).

Mechanism, Linux-specific layer (`/tmp/fexread/FEX/Source/Tools/LinuxEmulation/LinuxSyscalls/`):
- **mprotect + SIGSEGV** is the default (`SMCChecks = mtrack`). `SyscallHandler::MarkGuestExecutableRange` (`SyscallsSMCTracking.cpp:136-196`) does `mprotect(..., PROT_READ)` on every writable VMA overlapping a page that first gained compiled code; it handles shared-mapping *mirrors* by walking `Resource->FirstVMA` chains (`:170-186`).
- `SyscallHandler::HandleSegfault` (`SyscallsSMCTracking.cpp:48-134`) is the write fault handler: finds the VMA, calls `TM.InvalidateGuestCodeRange(Thread, FaultBase, FEX_PAGE_SIZE, UnprotectRegionCallback)` (`:113`, mirrors at `:106-108`), where the callback re-`mprotect`s `PROT_READ|PROT_WRITE` (`:87-90`). Then `:119-130` — if the faulting store may hit the *currently executing* block, it rewrites the ucontext PC to `AbsoluteLoopTopAddressFillSRA` with `ENTRY_FILL_SRA_SINGLE_INST_REG = 1` to force re-execution as a single-instruction block.
- The fan-out to all threads is in `FEX::HLE::ThreadManager::InvalidateGuestCodeRange` (`ThreadManager.h:218-246`): takes `ThreadCreationMutex` + `CodeInvalidationMutex`, then `CTX->InvalidateCodeBuffersCodeRange` + a loop of `CTX->InvalidateThreadCachedCodeRange` per thread.
- Syscall hooks: `InvalidateCodeRangeIfNecessary` (`Syscalls.h:256-264`) called from mmap/munmap/mprotect/mremap/shmat/shmdt in `SyscallsSMCTracking.cpp:337, 380, 410, 481, 547, 573`.
- **`SMCChecks = full`** is the alternative: per-instruction inline code comparison. `Core.cpp:640-663` emits `_ValidateCode(CodeOriginal, InstAddressReg, InstSize)` before every instruction, branching to `_ThreadRemoveCodeEntry()` (→ `ContextImpl::ThreadRemoveCodeEntryFromJit` → `SyscallHandler->InvalidateGuestCodeRange(Thread, GuestRIP, 1)`, `Core.cpp:1000-1002`) plus an exit. JIT emission at `BranchOps.cpp:380` and `:440`.
- Windows equivalent lives in `/tmp/fexread/FEX/Source/Windows/Common/InvalidationTracker.cpp`.

**Key point for you:** FEXCore itself has *no* notion of how invalidation is detected. `FEXCore::HLE::SyscallHandler::InvalidateGuestCodeRange` is a virtual no-op (`FEXCore/include/FEXCore/HLE/SyscallHandler.h:62`) and `MarkGuestExecutableRange` is likewise a frontend hook. The whole "guest page changed" policy is frontend-owned — that boundary is actually the *good news* for a system-level retrofit: you'd implement guest-page-table-change → `InvalidateCodeBuffersCodeRange` there. The bad news is that everything below that boundary is keyed on guest VA pages.

---

## 5. Per-thread vs shared

**Split.** L1 + L2 are strictly per-thread; L3 + block links + generated code are shared across threads via the `CodeBuffer`.

- `InternalThreadState::LookupCache` is a per-thread `NonMovableUniquePtr<FEXCore::LookupCache>` (`FEXCore/include/FEXCore/Debug/InternalThreadState.h:98`), created in `ContextImpl::InitializeCompiler` (`Core.cpp:396`) which also publishes `State.L1Pointer` / `State.L1Mask` / `Pointers.L2Pointer` into the thread's CPU state (`Core.cpp:400-403`).
- `LookupCache::Shared` is a raw `GuestToHostMap*` (`LookupCache.h:274`) pointing into `CodeBuffer::LookupCache` (`SharedCodeBufferManager.h:26`), assigned at `JIT.cpp:669-670`. `SharedCodeBufferManager` explicitly exists to share one code buffer across threads (`SharedCodeBufferManager.h:42-50`).
- So generated host code **is shared across threads**. Threads swap to a newer CodeBuffer via `ClearCache` / `ChangeGuestToHostMapping` (`JIT.cpp:673-681`, `:1082-1091`; `LookupCache.h:171-174`), old versions persist read-only until refcount drops.
- Locking: `GuestToHostMap::Lock` is a `WritePriorityMutex` (`LookupCache.h:46`) with token types enforcing lock-held-ness. L1 reads are deliberately **unlocked and non-atomic** — see the honest caveat at `LookupCache.h:371-377` ("This approach has not been fully vetted yet") and `:306-309`.

---

## 6. Is there any mode/context key today? No.

Search for a mode key in the cache path returns nothing. The only place mode is consulted is at *decode* time, and FEX asserts it never changes:

`FEXCore/Source/Interface/Core/Frontend.cpp:1393-1396`:
```cpp
// Decode operating mode from thread's CS segment.
const auto CSSegment = Core::CPUState::GetSegmentFromIndex(Thread->CurrentFrame->State, Thread->CurrentFrame->State.cs_idx);
BlockInfo.Is64BitMode = CSSegment->L == 1;
LOGMAN_THROW_A_FMT(BlockInfo.Is64BitMode == CTX->Config.Is64BitMode, "Expected operating mode to not change at runtime!");
```

That assertion is the single most important line in this report for your retrofit: **FEX hard-assumes one bitness for the whole process lifetime.** `Config.Is64BitMode` is a process-wide `FEX_CONFIG_OPT` (`Context.h:348`) consumed all over the frontend (`Frontend.cpp:350, 377, 395, 456, 484, 698, 773-807, 992-1032`) and IR gen (`Core.cpp:254, 279, 549, 555, 1039`). Even the offline cache validator fakes a GDT entry to match it (`CodeCache.cpp:416-424`).

A "compiled block" is identified by:
- in the lookup caches: guest RIP alone;
- in the generated code metadata: `JITCodeTail{ .RIP = Entry, .GuestSize = Size, .SingleInst = ... }` (`JIT.cpp:1013-1018`, struct at `FEXCore/Source/Interface/Core/CPUBackend.h:73-85`).

The only *implicit* extra key components today are: (a) which `CodeBuffer`/`GuestToHostMap` generation you're on, (b) TF/single-step, which is handled by **bypassing** the cache entirely rather than keying it — `JIT.cpp:537-540` (`ExitFunctionLink`: "If TF is set, the cache must be skipped as different code needs to be generated") and `Dispatcher.cpp:172-173`. That TF handling is the closest existing analogue to a mode key, and it is a bypass, not a key. There is no CPL/ring concept anywhere in FEXCore.

---

## 7. Guest RIP baked into generated code

Yes, in several places — and FEX already has a relocation system for exactly this, because of the persistent code cache.

`FEXCore/Source/Interface/Core/JIT/Relocations.h:11-28` defines four relocation types; two carry guest RIP:
- `RELOC_GUEST_RIP_MOVE` — a fixed-size 4-instruction constant materialization into a GPR.
- `RELOC_GUEST_RIP_LITERAL` — an 8-byte literal in the code stream.

Emission sites:
- `Arm64JITCore::InsertGuestRIPMove(Reg, Constant)` — `FEXCore/Source/Interface/Core/JIT/Arm64Relocations.cpp:90-103`. Records `{Offset = GetCursorOffset(), Type = RELOC_GUEST_RIP_MOVE, GuestRIP = Constant, RegisterIndex}` then `LoadConstant(..., PadType::AUTOPAD)` — **padded to fixed size precisely so it can be rewritten later**.
- Callers: `ALUOps.cpp:64` (`DEF_OP(EntrypointOffset)` — `Entry + Op->Offset`, i.e. any materialized guest address, which is *every* x86 instruction that needs its own RIP: call pushes, RIP-relative, exceptions), `BranchOps.cpp:176` (TF-check path storing RIP), `BranchOps.cpp:86` (ARM64EC).
- `InsertGuestRIPLiteral` / `PlaceNamedSymbolLiteral` — `Arm64Relocations.cpp:74-88`, `:58-72`; used at `JIT.cpp:995` to place the `GuestRIP` field of every `ExitFunctionLinkData`. So **every block link record contains an absolute guest RIP literal.**
- `Arm64JITCore::TakeRelocations(GuestBaseAddress)` — `Arm64Relocations.cpp:105-119`: rebases by subtracting the binary base, i.e. FEX already converts absolute RIPs to file-relative for on-disk caching. `ContextImpl::ApplyCodeRelocations(GuestDelta, Code, Relocations, RelocationOffset, ForStorage)` (`Context.h:130`) re-applies them on load.
- `RELOC_NAMED_SYMBOL_LITERAL` currently has exactly one symbol, `SYMBOL_LITERAL_EXITFUNCTION_LINKER` (`Relocations.h:41`, resolved at `Arm64Relocations.cpp:14-21`); `RELOC_NAMED_THUNK_MOVE` for thunk pointers (`Arm64Relocations.cpp:23-35`).
- Relocations are discarded unless the cache generator is active: `Core.cpp:912-915` `if (!CodeCache.IsGeneratingCache) Thread->CPUBackend->ClearRelocations();`.

Also note `JITCodeTail.RIP` (`JIT.cpp:1014`) and the varint `<HostPCOffset, GuestRIPOffset>` table appended after every block (`JIT.cpp:1020-1055`) used for fault-time RIP reconstruction — another absolute-guest-VA dependency living inside the code buffer.

---

## Skeptical summary for your retrofit

What helps you:
- The relocation infrastructure (`Relocations.h`, `Arm64Relocations.cpp`, `ApplyCodeRelocations`) already exists and already relocates guest RIPs, with fixed-size padded constant loads. Retargeting/relocating baked RIPs is a solved problem here.
- Invalidation detection is entirely frontend-owned behind two virtuals (`SyscallHandler::InvalidateGuestCodeRange` / `MarkGuestExecutableRange`), so a guest-page-table-walker hook is a clean insertion point.
- Block delinking is generic (`BlockDelinkerFunc` + ordered `BlockLinks` map keyed by destination), so invalidating a physical page could sever links if you can map physical → the set of guest destinations.

What hurts:
- The key is a bare `uint64_t` guest VA in *all three* levels, and the tag compare is a single `sub`/`cbnz` against `State.rip` in hand-written ARM64 at `Dispatcher.cpp:215` and `BranchOps.cpp:210`. Adding a mode/CPL/ASID component means editing generated assembly in at least three places and widening `LookupCacheEntry` past the 16-byte `ldp`/`stp` fast path.
- The L2 structure is *literally* indexed as `VA>>12` then `VA&0xFFF` — it is a shadow guest page table. A physically-indexed cache would want a different structure entirely; L2 is not reusable.
- `Frontend.cpp:1396` asserts operating mode never changes at runtime, and `Config.Is64BitMode` is threaded through ~30 frontend/IR sites. Supporting real mode / 16-bit / ring transitions is not a "add a key" change, it's a frontend rework.
- `CodePages` invalidation is keyed by guest VA page, and blocks record their `CodePages` as VA pages (`LookupCache.h:87`, `Core.cpp:917-930`). A physical-invalidation model needs a second index (phys page → blocks) that does not exist.
- `ExitFunctionLinkData.GuestRIP` (absolute VA) is embedded in every link thunk, and the `Record - 0x10` layout arithmetic in the delinkers (`JIT.cpp:501, 519, 559`) is fragile to any struct change.
- No CPL/ring/privilege concept exists anywhere in FEXCore. The only "context" that changes codegen (TF) is handled by cache bypass, which is a hint that FEX's designers avoided a mode key deliberately.