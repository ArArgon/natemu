I'll start by exploring the repo structure and running parallel searches.
## Summary verdict up front

FEXCore is a **strict 1:1 flat-address userspace emulator**. Guest VA == host VA, always, with **no base register, no offset, no TLB, no page-table walk, no privilege level**. Every guest load/store lowers to a bare ARM64 `ldr`/`str` on a register holding the raw guest pointer. Retrofitting shadow-paged ring0/3 system emulation would require touching every memory op lowering plus the register allocator (there is no spare pinned register reserved for it) — this is not a config flip.

Repo state: `/tmp/fexread/FEX`, shallow clone (`git log | wc -l` == 1, HEAD `fd141ed`), so **historical** claims cannot be verified from git here; all statements below are about the tree as it exists.

---

## 1. Guest load/store → ARM64: the actual emitted addressing

### IR level
`/tmp/fexread/FEX/FEXCore/Source/Interface/IR/IR.json:532`:
```
"SSA = LoadMem RegisterClass:$Class, OpSize:#Size, GPR:$Addr, GPR:$Offset, OpSize:$Align, MemOffsetType:$OffsetType, u8:$OffsetScale"
```
and `:577` for `LoadMemTSO`, `:543`/`:584` for `StoreMem`/`StoreMemTSO`. Note the operand list: **`$Addr` + `$Offset` + `$OffsetType` + `$OffsetScale`. There is no segment/base/space operand.**

### JIT lowering (`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp`)

`DEF_OP(LoadMem)` at **line 704**:
```cpp
const auto MemReg = GetReg(Op->Addr);
const auto MemSrc = GenerateMemOperand(OpSize, MemReg, Op->Offset, Op->OffsetType, Op->OffsetScale);
...
case IR::OpSize::i32Bit: ldr(Dst.W(), MemSrc); break;
case IR::OpSize::i64Bit: ldr(Dst.X(), MemSrc); break;
```
`DEF_OP(StoreMem)` at **line 1639** is symmetric (`strb/strh/str`).

`GenerateMemOperand` at **line 585-604** is the whole addressing story:
```cpp
if (Offset.IsInvalid()) {
  return ARMEmitter::ExtendedMemOperand(Base.X(), ARMEmitter::IndexType::OFFSET, 0);
} ...
  return ARMEmitter::ExtendedMemOperand(Base.X(), ARMEmitter::IndexType::OFFSET, Const);
...
  return ARMEmitter::ExtendedMemOperand(Base.X(), RegOffset.X(), ARMEmitter::ExtendedType::SXTX, FEXCore::ilog2(OffsetScale));
```

So emitted forms are exactly: `[Xn]`, `[Xn, #imm]`, `[Xn, Xm, {SXTX|UXTW|SXTW} #shift]`. **`Xn` is the guest pointer verbatim.** No third addend, no fixed base.

### TSO variants
`DEF_OP(LoadMemTSO)` **line 767** picks among three GPR paths by host feature:
- FEAT_LRCPC2 (`SupportsTSOImm9`): `ldapurb/ldapurh/ldapur` with imm9 (line 787-801)
- FEAT_LRCPC: `ldaprb/ldaprh/ldapr` (line 807-820)
- ARMv8.0 baseline: `ldarb/ldarh/ldar` (line 822-835)

Each non-8-bit case is followed by a literal `nop();` with the comment `// Half-barrier once back-patched.` (lines 805, 820, 834) — this is the backpatch slot (see §6).

`DEF_OP(StoreMemTSO)` **line 1778** mirrors it with `stlur*`/`stlr*`, and the `nop()` is emitted **before** the store (lines 1801, 1817).

Vector TSO gets no acquire/release instruction at all — just an optional `dmb ISHLD` / `dmb ISH` around a plain `ldr`/`str` (lines 843-855 and 1826-1830), gated on `CTX->IsVectorAtomicTSOEnabled()`.

### Answer to "is there a fixed host base register added to every guest address?"
**No.** The full pinned-register set is in `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.h:35-52`:
```cpp
constexpr auto STATE = ARMEmitter::XReg::x28;
constexpr auto TMP1 = ARMEmitter::XReg::x0; ... TMP4 = x3;
constexpr auto REG_PF = ARMEmitter::Reg::r26;
constexpr auto REG_AF = ARMEmitter::Reg::r27;
constexpr auto REG_CALLRET_SP = ARMEmitter::XReg::x25;
```
`x28` is the **CPUState pointer**, not a memory base. There is no `REG_MEM_BASE` equivalent anywhere.

Proof that guest VA == host VA at the OS boundary: `/tmp/fexread/FEX/Source/Tools/LinuxEmulation/LinuxSyscalls/SyscallsSMCTracking.cpp:325-327`:
```cpp
Result = reinterpret_cast<uint64_t>(::mmap(reinterpret_cast<void*>(addr), length, prot, flags, fd, offset));
```
The guest `mmap` is a raw host `mmap`, returned address handed straight back to the guest.

---

## 2. "Memory base" / MEM_OFFSET concept — what exists today

**Nothing.** Grepping the whole tree for `MEM_BASE`, `REG_MEM_BASE`, `Op_MemBase`, `_MEM_BASE`, `VirtualMemoryBase`, `MemoryBase`, `guest_base`, `GuestBase` yields only unrelated hits:

- `MemBase` in `OpcodeDispatcher.h:1058-1059,1474-1483` and `OpcodeDispatcher/Vector.cpp:3044+` — a local variable name for the **FXSAVE/XSAVE destination pointer**, unrelated.
- `FLAGS_MEM_OFFSET` in `FEXCore/Source/Interface/Core/X86Tables/BaseTables.cpp:137-153` — an x86 **decoder** flag for the `MOV moffs` opcode form, unrelated.
- `GuestBase` in `FEXCore/Source/Interface/Core/JIT/Arm64Relocations.cpp:105-111`, `FEXCore/include/FEXCore/Core/CodeCache.h:221`, `CPUBackend.h:118` — the **ELF load base used to make persisted JIT code relocatable**, i.e. code-cache bookkeeping, not a data-address offset.
- `ReadOnlySharedMemoryBase` in `Source/Windows/include/winternl.h` — Windows header.

Config: `/tmp/fexread/FEX/FEXCore/Source/Interface/Config/Config.json.in` has **no** option for a memory base/offset mode. The nearest things are `IS64BIT_MODE` (line 598) and TSO knobs (lines 439-480). There is no `Data/*.json` config schema — `Data/` holds `ThunksDB.json`, AppConfig, CI, CMake, nix, binfmts only.

The one internal "virtual memory size" is `ContextImpl::Config.VirtualMemSize` — `/tmp/fexread/FEX/FEXCore/Source/Interface/Context/Context.h:339`:
```cpp
uint64_t VirtualMemSize {1ULL << 36};
```
overridden to `1ULL << 32` for 32-bit guests at `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/Core.cpp:85-88`. This is **only** used to size/mask the code LookupCache (`FEXCore/Source/Interface/Core/LookupCache.cpp:27,48,56,62`; `LookupCache.h:193,314,396`) and the dispatcher's inline L2 lookup mask (`Dispatcher.cpp:185-191`). It is not applied to data addresses — and note that at `1<<36` it's smaller than the actual 48-bit VA space, i.e. it deliberately **aliases** code addresses in the L2 cache, guarded by a guest-RIP tag compare (`Dispatcher.cpp:212-216`).

I cannot confirm or refute the historical two-mode claim from this checkout (shallow clone). What I can say concretely: **there is exactly one mode today, and it is guest VA == host VA.**

---

## 3. Softmmu / TLB / address translation — honest answer: **none exists**

Case-insensitive grep across all `.cpp`/`.h` for `\bTLB\b|softmmu|AddressTranslat|PageTable|GuestToHost|HostToGuest`:

- `TLB`: only `Source/Tools/LinuxEmulation/LinuxSyscalls/EmulatedFiles/EmulatedFiles.cpp:497` (a fake `/proc/cpuinfo` string `"TLB size\t: 2560 4K pages"`) and `Source/Common/HostFeatures.h:194-195` (`TLBIOS`/`TLBIRANGE` ARM ISA feature bits for `FEAT_TLBIOS`, purely CPU-feature detection).
- `softmmu`, `AddressTranslat`, `PageTable`: **zero hits.**
- `GuestToHost`/`HostToGuest`: only in `ThunkLibs/` and `Source/Tools/LinuxEmulation/Thunks.cpp` — these are **function-pointer trampolines** for host-library thunking (`HostToGuestTrampolineTemplate`, `Thunks.cpp:43-87`) and X11 `Display*` mapping (`ThunkLibs/include/common/X11Manager.h:32-71`). Nothing to do with addresses.

The only guest→host lookup in the engine is `LookupCache` (`FEXCore/Source/Interface/Core/LookupCache.h`), which maps **guest RIP → host code pointer** (L1 direct-mapped at `:180-200`, L2 two-level page table at `:193-210`). It is a code-block dispatch cache, and the JIT inlines it at `Dispatcher.cpp:181-236`. It is architecturally the closest thing to a "TLB" in the codebase, and it is a plausible template for how you'd shape a data TLB — but it is **not** on the data path.

---

## 4. 48-bit guest AS and 32-bit guest mode

### 64-bit
`Source/Tools/LinuxEmulation/LinuxSyscalls/Syscalls.h:318`:
```cpp
constexpr static uint64_t TASK_MAX_64BIT = (1ULL << 48);
```
used in `SignalDelegator.cpp:719` to route out-of-range faults. FEX does **not** canonicalize or mask guest addresses on the load/store path — it relies on the host ARM64 VA layout matching. `FEXCore/Source/Utils/Allocator.cpp:277-289`:
```cpp
size_t Bits = FEXCore::Allocator::GetHostVABits();
if (Bits < 48) { return {}; }
uintptr_t Begin48BitVA = 0x0'8000'0000'0000ULL;
uintptr_t End48BitVA   = 0x1'0000'0000'0000ULL;
auto Regions = StealMemoryRegion(Begin48BitVA, End48BitVA);
```
FEX **steals** the upper half of the 48-bit VA for its own allocations so guest allocations stay low. `64BitAllocator.cpp:63-69` sets `UPPER_BOUND = (1ULL << 57)` (refined by `DetermineVASize()` at `:186-196` from actual host VA bits) and `LOWER_BOUND = 0x1'0000'0000ULL`.

### 32-bit guest — **not a separate MMU path, just a narrower GPR size**
`Is64BitMode` is a boolean that selects operand/address width, register file, and decoder tables. Key sites:
- `FEXCore/Source/Interface/Core/OpcodeDispatcher.h:1255`: `return Is64BitMode ? IR::OpSize::i64Bit : IR::OpSize::i32Bit;` (`GetGPROpSize()`)
- `FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp:387-403`: picks `x64::SRA` vs `x32::SRA` (10 vs 18 static regs; `x32::SRA` at `:234-246`)
- `FEXCore/Source/Interface/Core/Frontend.cpp:1395-1396`:
```cpp
BlockInfo.Is64BitMode = CSSegment->L == 1;
LOGMAN_THROW_A_FMT(BlockInfo.Is64BitMode == CTX->Config.Is64BitMode, "Expected operating mode to not change at runtime!");
```
  **This assert is a hard blocker for system emulation** — FEX explicitly forbids a mode switch at runtime.

The 32-bit address confinement is achieved *implicitly* by W-register arithmetic zero-extension. `FEXCore/Source/Interface/Core/JIT/JITClass.h:189-192`:
```cpp
static ARMEmitter::Size ConvertSize(const IR::IROp_Header* Op) {
  return Op->Size == IR::OpSize::i64Bit ? ARMEmitter::Size::i64Bit : ARMEmitter::Size::i32Bit;
}
```
So a 32-bit `Add` emits `add Wd, Wn, Wm`, which zero-extends into `Xd`; `GenerateMemOperand` then uses `Base.X()`. Guest 32-bit VA lands in host `[0, 4GB)` unchanged. Explicit narrowing only happens when the *address-size prefix* shrinks below GPR size — `Addressing.cpp:31-45`:
```cpp
if ((A.AddrSize < GPRSize) && !AllowUpperGarbage && Tmp) {
  uint32_t Bits = IR::OpSizeAsBits(A.AddrSize);
  if (A.Base || A.Index) { Tmp = IREmit->_Bfe(GPRSize, Bits, 0, Tmp); }
```
Address-size selection: `OpcodeDispatcher.cpp:4330`: `A.AddrSize = (Op->Flags & FLAG_ADDRESS_SIZE) != 0 ? (GPRSize >> 1) : GPRSize;`

Correspondingly, 32-bit guests use a separate VMA allocator (`Get32BitAllocator()->Mmap(...)`, `SyscallsSMCTracking.cpp:319`) and FEX reserves everything above 4GB for itself — see `docs/allocator_usage.md:12-22` and `docs/ProgrammingConcerns.md:3-5`.

---

## 5. Segments

### Storage
`/tmp/fexread/FEX/FEXCore/include/FEXCore/Core/CoreState.h:120-128`:
```cpp
uint16_t es_idx {}, cs_idx {}, ss_idx {}, ds_idx {};
uint16_t gs_idx {}, fs_idx {};
uint32_t mxcsr {};
// Segment registers holding base addresses
uint32_t es_cached {}, cs_cached {}, ss_cached {}, ds_cached {};
uint64_t gs_cached {};
uint64_t fs_cached {};
```
Note the asymmetry: **FS/GS bases are 64-bit; ES/CS/SS/DS bases are only 32-bit.** These are *bases*, not selectors — `_idx` holds the selector. A real GDT/LDT array does exist for 32-bit guests (`CoreState.h:132-158`, `gdt_segment* segment_arrays[2]`), with `GetSegmentFromIndex` / `CalculateGDTBase` / `CalculateGDTLimit` at `:160-178`.

**`CalculateGDTLimit` is only ever called from one place** — `Source/Tools/LinuxEmulation/LinuxSyscalls/x32/Thread.cpp:57` (checking whether a `set_thread_area` slot is free). **Segment limits are never enforced on any memory access.** No bounds check exists in the JIT.

### The dispatcher path
`/tmp/fexread/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp:4052-4099` (`GetSegment`):
```cpp
if (Is64BitMode) {
  if (Prefix == FLAG_FS_PREFIX) { return _LoadContextGPR(GPRSize, offsetof(CPUState, fs_cached)); }
  else if (Prefix == FLAG_GS_PREFIX) { return _LoadContextGPR(GPRSize, offsetof(CPUState, gs_cached)); }
  // If there was any other segment in 64bit then it is ignored
}
```
**In 64-bit mode CS/DS/ES/SS are literally dropped.** In 32-bit mode (`:4064-4098`) all six map to `*_cached` loads — but critically the default is:
```cpp
[[likely]] case FEXCore::X86Tables::DecodeFlags::FLAG_NO_PREFIX:
  return nullptr;
```
`DecodeAddress` calls `GetSegment(Op->Flags)` with `DefaultPrefix = FLAG_NO_PREFIX, Override = false` (`OpcodeDispatcher.cpp:4329`), so **absent an explicit prefix byte, no segment base is added at all — implicit DS and SS are assumed flat/zero even in 32-bit mode.** Only string ops force a default (`:3240` `FLAG_ES_PREFIX` with `Override=true`, `:3265-3266` for MOVS).

Non-zero legacy segment usage is merely *telemetered*, not handled: `CheckLegacySegmentRead` (`:4111-4155`) and `CheckLegacySegmentWrite` (`:4157-4195`) fire `TYPE_WRITES_32BIT_SEGMENT_{ES,CS,SS,DS}` — an explicit acknowledgement that FEX treats these as an anomaly.

### JIT-side
There is **no** segment handling in the JIT. Segment base becomes a plain IR `Add` folded into the address in `Addressing.cpp:47-49`:
```cpp
if (A.Segment && AddSegmentBase) {
  Tmp = Tmp ? IREmit->Add(GPRSize, Tmp, A.Segment) : A.Segment;
}
```
and `SelectAddressMode` (`Addressing.cpp:113-127`) can even fold the segment into the ARM64 register-offset operand:
```cpp
if (B.Index && B.Segment) { B.Base = IREmit->Add(GPRSize, B.Base, B.Segment); }
else if (B.Segment) { B.Index = B.Segment; B.IndexScale = 1; }
```
Segment *writes* in 32-bit mode do a real GDT walk in IR — `OpcodeDispatcher.cpp:4197-4212` (`UpdatePrefixFromSegment`), loading the descriptor and reassembling `Base0/Base1/Base2` into `*_cached`. This is the closest thing FEX has to descriptor-table emulation, and it is base-only (no limit, no type, no DPL).

### Explicit non-support of privilege
`OpcodeDispatcher.cpp:200-202`:
```cpp
// RSP and SS only happen in 64-bit mode or if this is a CPL mode jump!
// FEX doesn't support a CPL mode switch, so don't need to worry about this on 32-bit
```
Grep for `CPL|ring0|cr0|cr3|cr4|SupervisorMode|IsKernel` across FEXCore returns only comments and CPUID feature bits (`CPUID.cpp:465,1045`). **No CR registers, no CPL, no privilege model.**

---

## 6. Unaligned atomics / backpatching

This is FEX's most interesting mechanism and the one place code is patched at runtime.

**Where:** `/tmp/fexread/FEX/FEXCore/Source/Utils/ArchHelpers/Arm64.cpp` (2212 lines), public API at `/tmp/fexread/FEX/FEXCore/include/FEXCore/Utils/ArchHelpers/Arm64.h:35-36`.

**How it works:**

1. **Emit time** — every TSO load/store reserves an adjacent `nop()` slot. `MemoryOps.cpp:805,820,834` (`// Half-barrier once back-patched.` *after* loads) and `MemoryOps.cpp:1801,1817` (*before* stores). Ordering matters: a load's fence goes after, a store's before.

2. **Fault time** — an unaligned `ldar`/`stlr`/`ldapr`/`ldapur`/`stlur` raises `SIGBUS`/`BUS_ADRALN`. Handler: `Source/Tools/LinuxEmulation/LinuxSyscalls/SignalDelegator.cpp:963-983`:
```cpp
const auto PC = ArchHelpers::Context::GetPc(ucontext);
if (!Thread->CTX->IsAddressInCodeBuffer(Thread, PC)) { return false; }
if (info->si_code != BUS_ADRALN) { return false; }
const auto Result = FEXCore::ArchHelpers::Arm64::HandleUnalignedAccess(Thread, Delegator->GetUnalignedHandlerType(), PC, ...);
ArchHelpers::Context::SetPc(ucontext, PC + Result.value_or(0));
```
Windows equivalents: `Source/Windows/WOW64/Module.cpp:343-350`, `Source/Windows/ARM64EC/Module.cpp:308-312`.

3. **Patch** — `HandleUnalignedAccess` at `Arm64.cpp:1932`. It takes a per-block spinlock first (`Arm64.cpp:2103-2106`):
```cpp
// Lock code mutex during any SIGBUS handling that potentially changes code.
// Due to code buffer sharing between threads, code must be carefully backpatched from last to first.
FEXCore::Utils::SpinWaitLock::UniqueSpinMutex lk(&InlineTail->SpinLockFutex);
```
Then rewrites the instruction, `Arm64.cpp:2108-2122`:
```cpp
uint32_t LDR = LDR_INST;  LDR |= Size << 30;  LDR |= AddrReg << 5;  LDR |= DataReg;
if (HandleType != UnalignedHandlerType::NonAtomic) {
  // Ordering matters with cross-thread visibility!
  std::atomic_ref<uint32_t>(PC[1]).store(DMB_LD, std::memory_order_release); // Back-patch the half-barrier.
}
std::atomic_ref<uint32_t>(PC[0]).store(LDR, std::memory_order_release);
ClearICache(&PC[0], 8);
return 0;   // re-execute
```
Store case (`:2122-2135`) patches `PC[-1]` to `DMB` then `PC[0]` to `STR` and returns `-4` so the DMB executes. LDAPUR→LDUR at `:2133-2148`, STLUR→STUR at `:2149-2161`. The **barrier is written before the access instruction** in both cases so another thread can never observe a non-atomic access without its fence.

4. **Race handling** — `Arm64.cpp:2164-2205` re-decodes `PC[0]` to detect "another thread already backpatched this", including the ARMv8.0 LDAXP triple-instruction pattern (`DMB / STP / DMB`).

5. **Non-backpatchable ops** are emulated in-place instead: `HandleCASPAL` (`:524`), `HandleCASAL`, `HandleAtomicMemOp`, `HandleAtomicLoadstoreExclusive` (`:1675`), plus hand-written multi-word `DoLoad16/32/64/128` (`:176-337`) that decompose a straddling access into aligned pieces.

6. **`ParanoidTSO` no longer exists.** Zero hits tree-wide. The replacement knobs live in `FEXCore/Source/Interface/Config/Config.json.in`:
   - `TSOEnabled` (`:439`, default `true`)
   - `VectorTSOEnabled` (`:447`, default `false`)
   - `MemcpySetTSOEnabled` (`:454`, default `false`)
   - `HalfBarrierTSOEnabled` (`:462`, default `true`) — "controls if unaligned loads and stores should be backpatched to half-barrier atomics. **Can be dangerous due to aligned loadstores through the same code now become non-atomic.**"
   - `StrictInProcessSplitLocks` (`:470`, default `false`)
   - `KernelUnalignedAtomicBackpatching` (`:478`, default `true`)

   The two `UnalignedHandlerType` values (`HalfBarrier`, `NonAtomic`) are selected in `Source/Tools/LinuxEmulation/LinuxSyscalls/SignalDelegator.cpp:886-891` and `Source/Windows/Common/TSOHandlerConfig.h:14-18`.

7. There is also a **kernel-assisted** path: `Source/Tools/FEXInterpreter/FEXInterpreter.cpp:322-331` sets `PR_ARM64_UNALIGN_ATOMIC_BACKPATCH (1UL << 1) | PR_ARM64_UNALIGN_ATOMIC_EMULATE` via prctl, offloading this to a patched kernel.

---

## Implications for a system-level retrofit (concrete blockers)

1. **Every memory op must grow an address-translation step.** Today the entire lowering is `GenerateMemOperand` (`MemoryOps.cpp:585-604`) returning `[Xn]`. There is no chokepoint that already computes an address — the IR `Addressing.cpp` folds base+index+disp+segment straight into ARM64 addressing modes precisely to *avoid* an extra instruction.
2. **No spare pinned register.** `Arm64Emitter.h:35-52` + `Arm64Emitter.cpp:234-268` account for x0-x3 (TMP), x25 (callret SP), x26/x27 (PF/AF), x28 (STATE), and the SRA/RA spans consume the rest. A TLB base register would have to be carved out of the allocator (`JIT.cpp:630` `RAPass->AddRegisters(IR::RegClass::GPRFixed, StaticRegisters.size())`).
3. **Runtime mode switch is asserted against** — `Frontend.cpp:1396`. Ring transitions imply 64↔32 (and 16-bit) mode changes mid-execution.
4. **Segment limits/types/DPL are decoded but never enforced** (`CalculateGDTLimit` has exactly one non-JIT caller), and 64-bit mode discards CS/DS/ES/SS entirely (`OpcodeDispatcher.cpp:4062`).
5. **The unaligned-atomic backpatcher depends on faults being *host* faults on *host-valid* addresses.** With shadow paging, a `SIGBUS`/`SIGSEGV` becomes ambiguous between "guest #PF" and "host misalignment"; `SignalDelegator.cpp:965-974` currently disambiguates purely on `si_code == BUS_ADRALN` + `IsAddressInCodeBuffer`.
6. **`LookupCache`'s `VirtualMemSize` masking (`1<<36` default, `Context.h:339`) aliases guest RIPs** and is tag-checked against a single flat address space (`Dispatcher.cpp:212-216`). Per-address-space (per-CR3) code caches would need to be introduced.