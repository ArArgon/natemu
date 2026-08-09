Let me refine the thunk/ELF/VDSO grep and examine Source/Windows.
This is the key evidence. Let me examine what the Windows backend implements.
# FEXCore ↔ Linux coupling: verified by include/link/dependency analysis

**Verdict up front:** the prior doc's conclusion (separable) is *directionally correct but under-argued*, and it is right for a non-obvious reason: FEX already ships a **non-Linux embedder** (`Source/Windows/`) that links the exact same `FEXCore_object` and runs on Wine/Windows. That is hard evidence, not layout inference. But the doc is wrong if it implied "no Linux in FEXCore" — there are ~46 POSIX includes in `FEXCore/`, and several are in *public headers* that an embedder must compile against. They are all `#ifndef _WIN32`-guarded, which is a different (and weaker) claim than "portable."

---

## 1. Build graph — what FEXCore actually links

`/tmp/fexread/FEX/FEXCore/CMakeLists.txt` is only 50 lines and does nothing but set flags + `add_subdirectory(Source/)` (line 40). All substance is in `/tmp/fexread/FEX/FEXCore/Source/CMakeLists.txt`.

**Targets produced** (`FEXCore/Source/CMakeLists.txt:260, 288-290`):
- `FEXCore_Base` — **STATIC** (line 260); config, allocator, logging, file loading
- `FEXCore_object` — **OBJECT** library of the JIT/IR/dispatcher (`AddObject`, line 288)
- `FEXCore` — **STATIC** (line 289), `FEXCore_shared` — **SHARED** (line 290), both built from `$<TARGET_OBJECTS:FEXCore_object>` (line 277)

**Link deps** (`FEXCore/Source/CMakeLists.txt:109`):
```cmake
set(LIBS fmt::fmt xxHash::xxhash FEXHeaderUtils CodeEmitter cephes_128bit)
```
plus:
- `vixl::vixl` — **optional only**, `if (ENABLE_VIXL_DISASSEMBLER OR ENABLE_VIXL_SIMULATOR)` (lines 111-113). Confirmed by grep: vixl appears only in `Source/Interface/Core/Dispatcher/Dispatcher.cpp:58,615,629,635` and `Source/Interface/Core/ArchHelpers/Arm64Emitter.{h,cpp}` — disassembler/simulator paths. **The JIT does not need vixl**; codegen is FEX's own `CodeEmitter/`.
- `Zydis::Zydis` — optional (lines 115-117)
- `dl` on non-MINGW (line 120); `synchronization` + `mincore` on MINGW (lines 122-125). Note: `grep -rn 'dlopen\|dlsym' FEXCore/Source FEXCore/include` returns **zero hits** — the `dl` link is vestigial.
- `atomic` on GCC only (lines 128-131)
- `unordered_dense::unordered_dense` — **PUBLIC, unconditional** (`AddDefaultOptionsToTarget`, line 256). `robin_map`/`robin_set` are thin wrappers over it: `FEXCore/include/FEXCore/fextl/robin_map.h`, `robin_set.h`.
- `TracyClient` — optional, `ENABLE_FEXCORE_PROFILER AND FEXCORE_PROFILER_BACKEND STREQUAL "TRACY"` (lines 264-266)
- **jemalloc/rpmalloc are NOT linked into FEXCore.** They live in a separate meta-target `JemallocLibs` (lines 300-308) that only `FEXCore_shared` pulls in (line 318). `JemallocDummy` (line 313) exists to opt *out*. Static `FEXCore`/`FEXCore_object` are allocator-agnostic.

**Does it depend on `Source/`?** **No.** `FEXHeaderUtils/` and `CodeEmitter/` are **root-level** siblings of `Source/`, not inside it (`ls /tmp/fexread/FEX`). Include dirs (`FEXCore/Source/CMakeLists.txt:226-232`) are `IncludePrivate/`, `CMAKE_CURRENT_SOURCE_DIR`, `${PROJECT_SOURCE_DIR}/include/`, `${CMAKE_BINARY_DIR}/include/` — nothing reaches into `Source/`. Dependency direction is strictly `Source/Tools → FEXCore`, never back.

**Codegen dependency:** the build requires `python3` at configure/build time for `Scripts/json_ir_generator.py` and `Scripts/config_generator.py` (lines 149, 187), plus `gzip` for the man page (line 193). Non-obvious embed friction.

**Stale line found:** `FEXCore/Source/CMakeLists.txt:306` does `set_source_files_properties(Interface/HLE/Thunks/Thunks.cpp ...)` but `FEXCore/Source/Interface/HLE/` **does not exist** (`ls` → No such file or directory). Dead no-op under `ENABLE_JEMALLOC_GLIBC_ALLOC`.

---

## 2. Linux-only surface — counted

Total files scanned: **176** (`.cpp/.h/.inl` under `FEXCore/Source` + `FEXCore/include`).
Total POSIX-ish includes (`sys/`, `linux/`, `pthread.h`, `unistd.h`, `signal.h`, `csignal`, `fcntl.h`, `dlfcn.h`): **46**.

`#include <sys/...>` — **20 sites**; `#include <linux/...>` — **3 sites**. Full list:

| File | Lines | Category |
|---|---|---|
| `FEXCore/include/FEXCore/Utils/AllocatorHooks.h` | 10 (`sys/mman.h`), 19 (`sys/types.h`) | **core** — but fully dual-pathed, see below |
| `FEXCore/include/FEXCore/Utils/InterruptableConditionVariable.h` | 9 (`linux/futex.h`), 10 (`sys/syscall.h`), 16 | **core** (`_WIN32`-guarded at :8, :27) |
| `FEXCore/include/FEXCore/Utils/WritePriorityMutex.h` | 7 (`linux/futex.h`), 9 (`sys/syscall.h`, `unistd.h`) | **core** (guarded at :6, :263, :273) |
| `FEXCore/include/FEXCore/Utils/SignalScopeGuards.h` | 10 (`signal.h`), 12 (`sys/syscall.h`), 14 | **core** (guarded at :11, :18) |
| `FEXCore/include/FEXCore/Utils/PrctlUtils.h` | 6 (`sys/prctl.h`) | **whole file** is `#ifndef _WIN32` (:4 … :56) |
| `FEXCore/include/FEXCore/Utils/File.h` | 8-9 (`fcntl.h`,`unistd.h`) | dual-pathed, 11 `_WIN32` branches |
| `FEXCore/include/FEXCore/Utils/Allocator.h` | 8 (`sys/types.h`) | guarded :79 |
| `FEXCore/include/FEXCore/Core/CodeCache.h` | 19 (`unistd.h`) | optional (code cache) |
| `FEXCore/Source/Utils/Allocator/64BitAllocator.cpp` | 25-27 (`sys/mman.h`, `sys/utsname.h`, `sys/user.h`) | **excluded on MINGW** (`CMakeLists.txt:12-15`) |
| `FEXCore/Source/Utils/Allocator.cpp` | 23-26; `"/proc/self/maps"` at **:220** | Linux-only, guarded :24/:37 |
| `FEXCore/Source/Utils/Profiler.cpp` | 3-7 (`linux/magic.h`, `sys/stat.h`, `sys/vfs.h`) | **optional/tooling** — profiler |
| `FEXCore/Source/Utils/AllocatorHooks.cpp` | 5-6, 18 | allocator, guarded |
| `FEXCore/Source/Interface/Core/Core.cpp` | 65, 70, 73, 75 | guarded :356, :451 |
| `FEXCore/Source/Utils/FileLoading.cpp` | 7-10 | dual-pathed :11/:17 |
| `FEXCore/Source/Common/JitSymbols.cpp` | 6-7 | **optional** — perf JIT symbols |
| `FEXCore/Source/Utils/Threads.cpp` | 6 (`pthread.h`), 7 | see §7 — pure indirection, no threads created |

Raw syscall usage — **exactly 3 files, 8 call sites**, all in headers, all `_WIN32`-guarded:
- `WritePriorityMutex.h` (4×), `SignalScopeGuards.h` (2×, e.g. `::syscall(SYS_rt_sigprocmask, ...)` at :161), `InterruptableConditionVariable.h` (2×) — all `SYS_futex`/`SYS_rt_sigprocmask`.

**Zero hits** for: `__NR_`, `memfd`, `shm_open`, `ucontext`, `sigaction`, `dlopen`/`dlsym`.
`/proc` appears in **exactly one place**: `FEXCore/Source/Utils/Allocator.cpp:220` — `open("/proc/self/maps", O_RDONLY)`, inside the 64-bit VA-reservation allocator that is compiled out on non-Linux.

**Core-JIT-needs-this vs. optional:**
- **Genuinely core:** the futex-based `WritePriorityMutex` (used for `GetCodeInvalidationMutex`), `SignalScopeGuards` deferred-signal machinery, and `AllocatorHooks` VirtualAlloc/VirtualProtect. All three already have Windows implementations in-tree.
- **Optional/tooling:** `Profiler.cpp` (`/proc`-adjacent `statfs`), `JitSymbols.cpp` (perf map), `64BitAllocator.cpp` (Linux VA-layout workaround, MINGW-excluded), `GDBJIT.cpp`.

There is **no syscall table, no `errno` translation, no ELF, no VDSO** anywhere in `FEXCore/`.

---

## 3. The embedder callback boundary — the key answer

This is the strongest evidence for separability. The boundary is small and explicit. FEXCore takes exactly **three** handler objects, via `FEXCore/include/FEXCore/Core/Context.h:131-133`:

```cpp
virtual void SetSignalDelegator(FEXCore::SignalDelegator* SignalDelegation) = 0;
virtual void SetSyscallHandler(FEXCore::HLE::SyscallHandler* Handler) = 0;
virtual void SetThunkHandler(FEXCore::ThunkHandler* Handler) = 0;
```

### Interfaces an embedder implements

**A. `FEXCore::HLE::SyscallHandler` — `/tmp/fexread/FEX/FEXCore/include/FEXCore/HLE/SyscallHandler.h:52`**
Pure-virtual (3 required):
- `:56` `virtual uint64_t HandleSyscall(CpuStateFrame*, SyscallArguments*) = 0;`
- `:65` `virtual ExecutableRangeInfo QueryGuestExecutableRange(InternalThreadState*, uint64_t) = 0;`
- `:66` `virtual std::optional<ExecutableFileSectionInfo> LookupExecutableFileSection(...) = 0;`

Optional (default no-op): `MarkGuestExecutableRange` :61, `InvalidateGuestCodeRange` :62, `MarkOvercommitRange` :63, `UnmarkOvercommitRange` :64, `PreCompile` :68, `GetSourcecodeResolver` :70, `SleepThread` :74.

Note the ABI enum at `:36-41` — `OS_LINUX64`, `OS_LINUX32`, **`OS_GENERIC`**. `OS_GENERIC` is documented as "No JIT-side argument handling, spill/fill all regs" and exists *precisely* for the non-Linux embedder.

**B. `FEXCore::SignalDelegator` — `/tmp/fexread/FEX/FEXCore/include/FEXCore/Core/SignalDelegator.h:58`**
Remarkably thin: **zero pure-virtual methods**. Only `virtual ~SignalDelegator()` (:60), `GetThunkCallbackRET()` with a `return 0;` default (:70-72), and a `SetConfig`/`GetConfig` pair over the POD `SignalDelegatorConfig` (:25-56) that FEXCore *pushes down to you* (dispatcher entry points, pause/stop handler addresses, SRA register mapping). Direction is FEXCore → embedder for data, not embedder → FEXCore for behavior. Windows literally uses a stub (see §6).

**C. `FEXCore::ThunkHandler` — `/tmp/fexread/FEX/FEXCore/include/FEXCore/Core/Thunks.h:17`**
One method: `:20` `virtual ThunkedFunction* LookupThunk(const IR::SHA256Sum&) = 0;`

**D. `FEXCore::Threads::Thread` — `/tmp/fexread/FEX/FEXCore/include/FEXCore/Utils/Threads.h:19`**
`joinable` :22, `join` :23, `detach` :24, `IsSelf` :25 — all pure virtual. Installed by function-pointer struct, not vtable: `Pointers{CreateThread, CleanupAfterFork}` :13-16, set via `SetInternalPointers` :38.

**E. `FEXCore::CodeMapOpener` / `CodeCache` — `/tmp/fexread/FEX/FEXCore/include/FEXCore/Core/CodeCache.h`**
`:128` `virtual int OpenCodeMapFile() = 0;` (returns an **fd** — a POSIX-ism in a public header, though Windows implements it at `Source/Windows/Common/ImageTracker.cpp:221`). Plus `AbstractCodeCache` pure-virtuals at `:243, 249, 254, 265, 272, 286`. **Optional subsystem** — only needed for persistent AOT code caching.

**F. `FEXCore::Config::Layer` — `/tmp/fexread/FEX/FEXCore/include/FEXCore/Config/Config.h:134`** — `virtual void Load() = 0;`

**G. `FEXCore::Utils::PooledAllocator`** — `ThreadPoolAllocator.h:342, 349`.

**`CodeLoader` is NOT a FEXCore interface.** `grep -rn 'CodeLoader' FEXCore/Source FEXCore/include` → **zero hits**. It lives at `/tmp/fexread/FEX/Source/Tools/CommonTools/CodeLoader.h`. **`HostFeatures` is a plain POD struct**, not an interface: `/tmp/fexread/FEX/FEXCore/include/FEXCore/Core/HostFeatures.h:15` — a flat bag of `bool SupportsSVE128`, `SupportsAES`, etc. The embedder fills it and passes it to `Context::CreateNewContext(HostFeatures)`. Two independent fillers exist: `Source/Common/HostFeatures.cpp:605` (Linux, reads MIDR/CTR) and `Source/Windows/Common/CPUFeatures.cpp` (Windows).

---

## 4. Syscall dispatch — FEXCore implements ZERO syscalls

Verified chain, end to end:

1. **Decode** — `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp:4789, 4797` call `SyscallOp(Op, ...)`.
2. **Frontend/IR** — `OpcodeDispatcher.cpp:38` `OpDispatchBuilder::SyscallOp`. It asks the *embedder* for the ABI at **`:52`**: `const auto OSABI = CTX->SyscallHandler->GetOSABI();` then picks `GPRIndexes_64` (Linux x86-64 RAX/RDI/RSI/RDX/R10/R8/R9, :44-47), `GPRIndexes_32` (:48-51), or **`OS_GENERIC` → `NumArguments = 0`** (:60-63). Emits `_Syscall(Arguments[0..6])` at **`:97`**.
3. **JIT** — `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/JIT/BranchOps.cpp:279` `DEF_OP(Syscall)`. It spills all static regs (:291), marks `InSyscallInfo` (:301), stacks 7 args (:303-310), then:
```cpp
:312  ldr(x0, STATE, offsetof(CpuStateFrame, Pointers.SyscallHandlerObj));
:313  ldr(x3, STATE, offsetof(CpuStateFrame, Pointers.SyscallHandlerFunc));
:314  mov(r1, STATE.R());          // ThreadState
:318  add(r2, rsp, 0);             // &SyscallArguments
:322  blr(Reg::r3);                // -> embedder
```
4. **Vtable binding** — `/tmp/fexread/FEX/FEXCore/Source/Interface/Core/JIT/JIT.cpp:660-662`:
```cpp
FEXCore::Utils::MemberFunctionToPointerCast PMF(&FEXCore::HLE::SyscallHandler::HandleSyscall);
Ptrs.SyscallHandlerObj  = reinterpret_cast<uint64_t>(CTX->SyscallHandler);
Ptrs.SyscallHandlerFunc = PMF.GetVTableEntry(CTX->SyscallHandler);
```

**Conclusion:** it is *literally* a devirtualized vtable slot baked into generated ARM64 code. FEXCore has no syscall implementation of any kind. The one exception is `Source/Interface/Core/JIT/MiscOps.cpp:268-272`, which inlines `SYS_getcpu` for the `RDTSCP`/`CPUID` leaf — and even that is `#ifdef _WIN32` dual-pathed (`0xa8` hardcoded on Windows at :268 vs `SYS_getcpu` at :270).

---

## 5. Thunks / ELF / /proc / VDSO — all outside FEXCore

Verified by grep, all four:

| Concern | In `FEXCore/`? | Actual location |
|---|---|---|
| VDSO | **No** — one *comment* only, `FEXCore/Source/Utils/Profiler.cpp:24` | `Source/Tools/LinuxEmulation/VDSO_Emulation.{cpp,h}`; `ThunkLibs/libVDSO/` |
| ELF | **No** — one *comment* only, `FEXCore/Source/Interface/Context/Context.h:121` ("FEX relocations are unrelated to ELF/PE relocations") | `Source/Tools/FEXInterpreter/ELFCodeLoader.h`, `Source/Tools/CommonTools/Linux/Utils/ELFContainer.cpp`, `ELFParser.h` |
| `/proc` | **One site**: `FEXCore/Source/Utils/Allocator.cpp:220` (`/proc/self/maps`, MINGW-excluded) | `Source/Tools/LinuxEmulation/LinuxSyscalls/EmulatedFiles/` |
| Thunks | Only the 1-method `ThunkHandler` vtable + IR op `DEF_OP(Thunk)` (`JIT/BranchOps.cpp:348`) | `Source/Tools/LinuxEmulation/Thunks.cpp`, `ThunkLibs/` (18 guest libs) |

---

## 6. The Windows/WOW64 backend — the empirical proof

**Yes, and it is substantial and current.** `Source/Windows/` is ~4,274 LoC across two full backends:
- **WOW64** (32-bit x86 on ARM64 Windows/Wine): `/tmp/fexread/FEX/Source/Windows/WOW64/Module.cpp` (1,053 lines)
- **ARM64EC** (64-bit x86 emulation, MS's ABI): `/tmp/fexread/FEX/Source/Windows/ARM64EC/Module.cpp` (1,044 lines)

Wired in at `/tmp/fexread/FEX/Source/CMakeLists.txt:9-11` (`if (MINGW) add_subdirectory(Windows/)`) and `Source/Windows/CMakeLists.txt:27-31`.

**It links the identical FEXCore object files** — `/tmp/fexread/FEX/Source/Windows/WOW64/CMakeLists.txt:1-20`:
```cmake
add_library(wow64fex SHARED Module.cpp libwow64fex.def
  $<TARGET_OBJECTS:FEXCore_object>)
target_link_libraries(wow64fex PRIVATE FEXCore_Base Common CommonTools CommonWindows CommonWindowsRuntime wow64_ex ntdll_ex)
target_link_options(wow64fex PRIVATE -static -nostdlib -nostartfiles -nodefaultlibs -lc++ -lc++abi -lunwind)
```
Note `-nostdlib -nodefaultlibs`: FEXCore runs here **with no libc at all**, on a hand-rolled CRT (`Source/Windows/Common/CRT/`, musl-derived math at `Common/CRT/Musl/`).

### What the alternate embedder actually provides

**Bring-up sequence** — `Source/Windows/WOW64/Module.cpp:531-549` (mirrored at `ARM64EC/Module.cpp:613-615`):
```cpp
:531  SignalDelegator = fextl::make_unique<FEX::DummyHandlers::DummySignalDelegator>();
:532  SyscallHandler  = fextl::make_unique<WowSyscallHandler>();
:537  FEX::Windows::Allocator::SetupHooks(NtDll);
:542  CTX = FEXCore::Context::Context::CreateNewContext(HostFeatures);
:545  CTX->SetSignalDelegator(SignalDelegator.get());
:546  CTX->SetSyscallHandler(SyscallHandler.get());
:547  CTX->InitCore();
:776  CTX->ExecuteThread(TLS.ThreadState());   // on an OS-created thread
```

Required pieces, with sizes:

| Piece | File | LoC | Notes |
|---|---|---|---|
| `SyscallHandler` impl | `WOW64/Module.cpp:432` `class WowSyscallHandler : public FEXCore::HLE::SyscallHandler` | ~80 | Sets `OSABI = OS_GENERIC` at `:435`. `HandleSyscallImpl` (`:438`) just forwards to `Wow64SystemServiceEx` / `WineUnixCall` and rewrites RIP/RSP/RAX by hand |
| `SignalDelegator` | **none written** — reuses `FEX::DummyHandlers::DummySignalDelegator` from `Source/Tools/CommonTools/DummyHandlers.h:29` | 0 | Windows uses SEH via `Common/Exception.h` instead |
| `HostFeatures` | `Common/CPUFeatures.cpp` | 169 | `FetchHostFeatures(IsWine, HostTypeEnum::Wow64)` |
| Allocator hooks | `Common/Allocator.{h,cpp}` + `Common/WinAPI/Alloc.cpp` | ~36 | `SetupHooks(NtDll)` |
| SMC / code invalidation | `Common/InvalidationTracker.{cpp,h}` | 452 | Backs `QueryGuestExecutableRange` (`Module.cpp:507-509`) |
| Module/image tracking | `Common/ImageTracker.{cpp,h}` | 352 | Also implements `FEXCore::CodeMapOpener` (`ImageTracker.h:39,47`) |
| CRT + WinAPI shims | `Common/CRT/`, `Common/WinAPI/` | ~1,000 | Because of `-nostdlib` |
| TSO / call-ret / guard pages | `Common/TSOHandlerConfig.h`, `CallRetStack.h`, `JITGuardPage.h`, `OvercommitTracker.h` | ~190 | |

**Bounded honesty:** `wow64fex` still links `Common` and `CommonTools` from `Source/`. But both shrink to near-nothing on MINGW: `Source/Common/CMakeLists.txt:13-19` drops `FEXServerClient.cpp`, `FileFormatCheck.cpp`, `Linux/SBRKAllocations.cpp`, `Linux/LinuxVersion.cpp`; `Source/Tools/CommonTools/CMakeLists.txt:3-5` drops `Linux/Utils/ELFContainer.cpp`, leaving **only `DummyHandlers.cpp`**. So the real residual coupling is: config parsing (`Source/Common/Config.cpp`), CPU feature detection, and a ~45-line stub header.

---

## 7. Threading — the embedder owns host threads entirely

**FEXCore never creates a host thread.** `grep -rn 'Threads::Thread::Create'` across `FEXCore/Source` + `FEXCore/include` yields exactly one hit and it is **inside a doc comment**: `/tmp/fexread/FEX/FEXCore/Source/Interface/Context/Context.h:173` — `*  - Thread->ExecutionThread = FEXCore::Threads::Thread::Create(ThreadHandler, Arg);` i.e. instructions *for the frontend*.

The indirection is a hard failure if unset — `/tmp/fexread/FEX/FEXCore/Source/Utils/Threads.cpp:10-21`:
```cpp
static fextl::unique_ptr<Thread> CreateThread_Default(ThreadFunc Func, void* Arg) {
  ERROR_AND_DIE_FMT("Frontend didn't setup thread creation!");
}
```

**Guest thread model:** `FEXCore::Core::InternalThreadState` — `/tmp/fexread/FEX/FEXCore/include/FEXCore/Debug/InternalThreadState.h:86`. A page-aligned, `std::is_standard_layout` (asserted `:127`) struct owning `OpDispatcher`, `CPUBackend`, `LookupCache`, `FrontendDecoder`, `PassManager`, and `BaseFrameState`. Crucially it exposes **`void* FrontendPtr;` at `:107`** — "Data pointer for exclusive use by the frontend." That's an explicit embedder escape hatch. It contains **no OS handle, no TID, no pthread_t** — `ExecutionThread` lives on the *embedder's* object (`Source/Tools/LinuxEmulation/LinuxSyscalls/ThreadManager.h:95`).

Lifecycle split:
- FEXCore: `Context::CreateThread(InitialRIP, StackPointer, NewThreadState)` — `Context.h:123-124`, impl `Core.cpp:416` (pure allocation + compiler init, **no thread spawn**); `DestroyThread` `Context.h:126` / `Core.cpp:448`; `ExecuteThread` `Context.h:74` / `Core.cpp:383` — **runs the dispatcher on whatever host thread calls it**.
- Embedder: `FEX::HLE::ThreadManager::CreateThread` (`ThreadManager.h:189`), `StopThread` (:197), `Pause()` (:200), `Stop()` (:203), `NotifyPause()` (:267), and the real host thread via `class PThread final : public FEXCore::Threads::Thread` at `Source/Tools/LinuxEmulation/LinuxSyscalls/Utils/Threads.cpp:196`.

### Safe-point interruption — yes, and it's elegant

Mechanism: a **per-thread guard page** placed at a fixed offset from CPU state, so a single unconditional store traps when armed.

- `InternalThreadState.h:125` — `alignas(FEX_PAGE_SIZE) uint8_t InterruptFaultPage[FEX_PAGE_SIZE];` commented "*Can be reprotected as RO to trigger an interrupt at generated code block entrypoints*"
- `InternalThreadState.h:128-130` — static_assert that its offset from `BaseFrameState` is `<= 65520` (ARM64 unsigned immediate store range), so the JIT can hit it in one instruction with no scratch register.
- **JIT emits the probe unconditionally** — `Source/Interface/Core/Dispatcher/Dispatcher.cpp:277-279`:
```cpp
// Trigger segfault if any deferred signals are pending
strb(ARMEmitter::XReg::zr, STATE,
     offsetof(InternalThreadState, InterruptFaultPage) - offsetof(InternalThreadState, BaseFrameState));
```
also at `Source/Interface/Core/JIT/JIT.cpp:773`.
- **Arming/disarming** — `Source/Interface/Core/Core.cpp:446` uses `Allocator::VirtualProtect(&Thread->InterruptFaultPage, ...)`; the software side is `DeferredSignalRefCountGuard` (`include/FEXCore/Utils/SignalScopeGuards.h:116-153`), refcounting into `State.DeferredSignalRefCount` with an x86-64-specific extra check at `:133-145`.
- **Pause/stop trampolines are generated by FEXCore but their addresses are handed to the embedder**: `Dispatcher.cpp:436, 439, 453` create `ThreadPauseHandlerAddressSpillSRA`, `ThreadPauseHandlerAddress`, `PauseReturnInstruction`; `Dispatcher.cpp:2676-2682` publishes them into `SignalDelegatorConfig` (fields at `SignalDelegator.h:40-47`).

Note the guard is `#ifndef _WIN32` (`Dispatcher.cpp:254, 273`) — Windows swaps in a `NtSuspendThread`/SEH scheme plus `CPU_AREA_IN_SYSCALL_CALLBACK_OFFSET` on ARM64EC (`Dispatcher.cpp:261-270`). Waiting uses `FEXCore::InterruptableConditionVariable` (`ThreadManager.h:102`), which is the futex header from §2.

---

## Bottom line

| Claim | Verdict |
|---|---|
| `FEXCore/` has no build/include dependency on `Source/` | **TRUE, verified.** Deps are root-level `FEXHeaderUtils`/`CodeEmitter` + `External/`. No include path reaches `Source/`. |
| FEXCore implements no syscalls | **TRUE.** One devirtualized vtable slot (`JIT.cpp:660-662`), zero syscall implementations. Only `SYS_getcpu` is inlined, and it's dual-pathed. |
| No ELF / VDSO / thunk bodies / `/proc` in FEXCore | **TRUE**, modulo one `/proc/self/maps` in a MINGW-excluded allocator (`Allocator.cpp:220`). |
| "FEXCore is Linux-free" | **FALSE.** 46 POSIX includes, 8 raw `syscall()` sites, futex-based locks in *public headers*. All `_WIN32`-guarded — portable-by-`#ifdef`, not portable-by-abstraction. |
| Clean callback boundary | **TRUE and small.** 3 setters (`Context.h:131-133`); realistically **4 required methods**: `HandleSyscall`, `QueryGuestExecutableRange`, `LookupExecutableFileSection`, `LookupThunk`. `SignalDelegator` has zero pure virtuals. |
| Embeddable in a non-Linux host | **PROVEN, not inferred.** `Source/Windows/{WOW64,ARM64EC}` link `$<TARGET_OBJECTS:FEXCore_object>` and run with `-nostdlib`. |

**Realistic retrofit cost, using WOW64 as the yardstick:** the *interface* work is ~100 lines (a `SyscallHandler` subclass; `SignalDelegator` can be a stub). The *platform* work is ~1,500-2,000 lines: memory hooks, SMC/invalidation tracking, host feature detection, module tracking, and a thread-suspend scheme to replace `InterruptFaultPage`. Plus you inherit ARM64-host-only codegen (`set(DEFINES -DJIT_ARM64)`, `CMakeLists.txt:85`) and a build-time `python3` dependency.