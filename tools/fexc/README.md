# fexc: native macOS build of the FEXCore compiler subset

Status: **C1a done** (library + link smoke test). **C1b done** (`fexc` CLI and JSON
output, 5-input smoke check). **C1c done in effect**: the 53 pinned old-producer bodies with a recorded
bitness (`tools/i4v_producer/export.py` CASES) are byte-identical to the native output, which
`test_fexc.py` asserts; the structural self-check is `test_fexc.py` too. **S1a done**
([slice 1 contract](../../docs/restructure/slice1-contract.md) §3): `natemu-fexc-v2` with access
sites, `spill_bytes`, `cf_inverted` per PC entry, and `--object` (binary record in
`crates/abi/src/code_object.rs`); `check_sites.py` checks the sites against the 64 reference
programs and the emitted code. **S1c done**: profile `slice1` (allowlist block cutting, single-gate exits, see
"Profile slice1"). **S1b1 done**: the precision patches under `--profile slice1` and `verify_precision.py` (see
"Precision (S1b1)"). Not done: address guard, split-store check, canonical branch targets (S1b2).

## Build

```sh
tools/fexc/build.sh /Users/liaozping/orca/natemu-raw/i4v-producer-20261026-02/source BUILD_DIR
```

`BUILD_DIR` must not exist. The script checks the SHA-256 of the pinned
`upstream.tar.gz` (FEX `48d71752e2a363d5535181f803b0b0e9666c1825`) and
`dependency-overlay.tar`, extracts them to `BUILD_DIR/src`, applies `patch.py`
(every edit asserts its anchor, so it only applies to a fresh tree), configures
`CMakeLists.txt` against that tree, builds with 4 jobs and runs
`fexc_linktest --init` and `smoke.py`. Cold build about 25 s. Current tree:
`/Users/liaozping/orca/natemu-raw/fexc-native-20261026-01/s1b1` (~490 MB:
472 MB source, ~19 MB objects). Kept for comparison only: `s1c` (slice1 without the precision patches, the "before" of
the cost numbers) and `c1b` (the v1 build without sites). No Docker, no Linux VM; needs Homebrew `cmake`, `ninja`, Apple clang.

## What builds

`libfexcore.a`: decoder/frontend, x86 tables, OpDispatcher + IR, passes,
register allocation, ARM64 JIT backend/emitter, dispatcher generator, plus the
vendored fmt, xxhash, SoftFloat-3e and cephes. `fexc_linktest` creates a
`FEXCore::Context` (which references the whole library) and, with `--init`,
runs `InitCore()` (generates the dispatcher). Both succeed. No guest code has been compiled.

`CMakeLists.txt` is our own project that lists the sources of
`FEXCore/Source/CMakeLists.txt`; FEX's top-level CMake is not used (it assumes
Linux and drags in the application layer).

## Excluded

- Everything under `Source/` (FEXLoader/FEXInterpreter, LinuxEmulation syscalls,
  thunks, signals, FEXServer, config tools, `Source/Common` host-feature probing).
- vixl (disassembler/simulator), Zydis, tracy, jemalloc/rpmalloc, Catch2, range-v3,
  tiny-json. The disassembler is not needed; use `llvm-objdump` on the output.
- FEX's own `Config::Load()` file/env layers (not called; set options with
  `FEXCore::Config::Set`).

## Shimmed / patched (why)

`shim/` (force-included `fexc_compat.h`, plus stub headers):
- Linux headers FEXCore names but a compile-only tool does not use:
  `malloc.h`, `syscall.h`, `sys/prctl.h` (prctl returns -1/EINVAL = "no MDWE"),
  `sys/sendfile.h` (read/write emulation), `sys/vfs.h`, `linux/{limits,futex,magic}.h`.
- Constants: `MADV_HUGEPAGE/NOHUGEPAGE/DONTDUMP`, `MAP_FIXED_NOREPLACE`, `MREMAP_*`
  (inert). `mremap` aborts if reached (disk-cache page finalisation, never used).
- `syscall(...)` is redirected to `fexc_syscall` (`shim/fexc_platform.cpp`): futex
  wait returns immediately (waiters re-check in a loop, so it degrades to polling;
  wake is a no-op), rt_sigprocmask is a no-op, getrandom uses `arc4random_buf`,
  other numbers fail with ENOSYS. Valid because the tool is single-threaded and
  receives no signals.
- `memalign`, `malloc_usable_size`, `sincos` (glibc extensions).
- Generated files use FEX's own generators (`json_ir_generator.py`,
  `config_generator.py`); `git_version.h` is a fixed stub.

`patch.py` (applied to the extracted tree):
- `SpinWaitLock.h`: `size_t` is `unsigned long` on macOS but `uint64_t` is
  `unsigned long long`; add the missing overloads.
- `AllocatorHooks.h`: JIT/dispatcher buffers are mapped without PROT_EXEC (macOS
  arm64 refuses writable+executable anonymous maps; we never execute output).
  **Consequence: generated code cannot be run in-process; it is only exported.**
- `AllocatorHooks.cpp`: macOS `aligned_alloc` rejects small alignments (returned
  NULL and crashed `CreateNewContext`); use `posix_memalign`.

C1b patches in `patch.py`: the guest-code window seam from `i4v_producer/adapt.py`
(`ResolveGuestCodePointer`, integer arithmetic on logical PCs in `Frontend.cpp/.h`,
`Core.cpp`); the export hook (`shim/fexc_hook.h`, called from `ContextImpl::CompileCode`);
extra `DebugData` fields (entry prologue end offsets, static register map, filled in
`JIT.cpp`); the dense PC table (`fexc_dense_pc_table`); 16 KiB-page fixes (guard pages sized by
`FEXC_HOST_PAGE_SIZE`, `VirtualDontNeed` zeroes with `memset` because macOS
`MADV_DONTNEED` does not).

S1a patches in `patch.py` (see "Access sites" below): `OpcodeDispatcher.h`/`Core.cpp` capture the dispatcher's CF
inversion at each `GuestOpcode`; `JIT.cpp` calls `FexcAfterOp` after every emitted IR op and keeps the spill size;
`JITClass.h` declares it; `MemoryOps.cpp` includes `shim/fexc_sites.inc`, which defines it.

Warnings are suppressed (`-w`) for the vendored sources.

## fexc CLI (C1b)

```
BUILD_DIR/obj/fexc (--hex HEX | --file RAW) --rip N [--bitness 32|64] [--max-insts N] [--profile default|slice1]
                   [--multiblock] [--sparse-pc] [--no-end-marker] [--no-tso]
                   [--feature NAME=0|1]... [--out FILE] [--object FILE]
```

Input: raw x86 bytes (1..65536), full 64-bit guest RIP (canonical, including
upper half; 32-bit mode needs rip+len <= 4 GiB), mode. The bytes are an immutable
window at that RIP; FEX reads them through `ResolveGuestCodePointer` by logical
address, so nothing is mapped at the guest address. Anything past the window is
"not executable" and ends the block (`end.reason = end_of_input`).
`--max-insts N` stops after N guest instructions (default: FEX's own limit, 5000).
One fresh FEX context per run (FEX fixes the mode per context). Exit code: 0 ok,
1 no instruction translated (JSON still written), 2 bad arguments, 3 FEX init failed.

### Settings (one place: `FeatureTable()` / `Settings` in `fexc.cpp`)
Recorded in the output under `settings` and `features`. Default profile
`old-producer-compat+m4`: TSO on (also vector and memcpy/memset TSO), multiblock off,
SVE/LRCPC/LRCPC2/RPRES/WFXT off, LSE atomics and preserve_all ABI on (SVE, LRCPC, LRCPC2, LSE and
preserve_all match the old producer's logged `NATEMU_FEATURES`; RPRES and WFXT are pinned off to
match the old profile), FEX's `InstCountCI` end marker on. Every other
bit is Apple M4's ID-register view (sysctl `hw.optional.arm.*`); what the old Linux VM
exposed for those bits (FlagM, FCMA, ...) is **unverified**; the integer code of the 53 pinned
bodies and 64 reference programs does not depend on them. `DCacheLineLog2` is fixed at 4. `--feature
NAME=0|1` overrides one bit. Later this table is to be derived from the VM's vCPU.

### Output (`format: natemu-fexc-v2`, one JSON object; v2 = v1 plus `sites`, `code.layout.spill_bytes`, `cf_inverted` per `pc_table` entry)
- `input`, `settings`, `features`: echo of everything that influenced codegen.
- `status`: `ok` | `no_instructions_translated`. (`no_code` when FEX produced nothing.)
- `code`: `hex` (the whole FEX block: 4-byte header, entry prologue, body,
  end marker, exit/link stubs, tail, encoded RIP table), `size`, `alignment`,
  `layout` (header; per-entry `entry_native_offset` and `body_native_offset` from a
  small patch in `JIT.cpp`; `end_marker_offset` of `udf #0x420f`; `tail` offset/size and
  decoded fields; `code_region_begin/end`), `relocations`.
  Relocation literal slots in `hex` are zeroed; each relocation carries its
  `emitted_value`. Kinds actually seen: `guest_rip_literal` (8-byte guest RIP the block
  exits to / its own entry), `named_symbol_literal` (`exit_function_linker`: host address of
  FEX's block-link routine), `guest_rip_move` (guest-RIP constant materialised by a
  mov/movk sequence; `size` is null because unpadded sequences are 1-4
  instructions). The other FEX kinds (thunk, patchable) are decoded but only arise with
  disk-cache or thunk features.
- `state_abi`: state register (`x28`), static register map actually used for this
  mode (GPRs in x86 order then PF, AF; xmm registers), offsets in `CpuStateFrame` of the
  fields the code touches, and `state_pointer_loads`: every `ldr xN,[x28,#imm]` in the code that
  reads FEX's helper/dispatch pointer table (`Pointers`), with the pointer's name. These
  state-relative loads are how generated code reaches helpers, syscall and the dispatcher;
  they are *not* relocations.
- `guest`: instructions translated and their bytes, per decoded `blocks`
  (status), every decoded instruction (`translated`, FEX's mnemonic, `ends_block`,
  `sets_rip`) and `end` = {rip, reason, detail}. Reasons: `max_instructions`,
  `fex_instruction_limit`, `block_ending_instruction`, `end_of_input`,
  `invalid_instruction`/`partial_decode`/`not_executable`/`unimplemented_instruction`
  (decoder status), `dispatch_failure` (OpDispatcher cannot translate), `other`
  (e.g. the dispatcher itself requested a block end).
- `pc_table`: FEX's `DebugData.GuestOpcodes` verbatim (guest offset, native offset), plus
  `cf_inverted`: the dispatcher's guest-CF inversion at that instruction's start (the block
  entry is in the ABI state, inverted). Dense by default since v2: one entry per guest
  instruction, which sites need (an instruction FEX does not consider able to fault, e.g. the
  moffs `movabs`, would otherwise get no `GuestOpcode` and its access would be attributed to the
  previous instruction). `--sparse-pc` restores FEX's own sparse table, diagnostics only. The
  setting changes only the tail's encoded table, not the code region (checked on the 64 programs).
- `sites`: see below.
- `subblocks`: FEX's native sub-block extents.
- `recovery`: what is present (per-access sites, flag location, PC table, static register map,
  spill bytes) and what is still absent (the guarantee that guest state at a site equals the
  state before its guest instruction, committed effects, guard and split check).
- `diagnostics`: FEX log messages captured during compilation (for example
  `NoExec instruction in entry block` is expected at the end of the window).

## Known limits / problems seen
- FEX decodes with user-mode semantics: system instructions are not recognised as such
  (`syscall` becomes a 576-byte call-out through `SyscallHandlerFunc`; `mov cr3,eax`
  translates one 2-byte instruction and then reports `partial_decode`). The runtime
  must cut blocks before system instructions itself (vm-restructure step 3); the `default` profile does not, profile `slice1` does.
- Code reaches memory by using the guest address directly (`ldar x4,[x6]`): guest VA is
  assumed to equal the host/VM address in the translated code. Segment bases for 32-bit
  code live in `CpuStateFrame` (`cs_cached` ... `gs_cached`).
- Jump targets and block links go through `exit_function_linker` / `DispatcherLoopTop`
  state pointers; the resident runtime must provide equivalents.
- JIT temp-buffer overflow normally recovers through a SIGSEGV guard-page long jump in
  FEX's signal handler, which this tool does not install. Blocks up to 5000 SSE/x87/ALU
  instructions (about 100 KB of native code) compiled fine; an overflow would crash, not
  corrupt.
- 16 KiB host pages: guard pages and `VirtualDontNeed` are patched (see above); the
  "Failed to mprotect last page" error that appeared before the patch no longer occurs.
- `fexc` ends with `_exit` instead of destroying the context (FEX's destructors assume a
  running emulator thread).

## Smoke check
`smoke.py FEXC OUT_DIR` compiles two sample inputs from `tools/i4v_producer/export.py`
CASES (64-bit high RIP, 32-bit) and three from `fixtures/slice1/programs.json`, writes JSON
and disassembles each block with `xcrun objdump` (llvm-based; it wraps the words in a
Mach-O object first). The full comparison with the old pinned bodies and the structural
self-check are in `test_fexc.py`.

## Profile slice1 (S1c)

`--profile slice1` (put it first: later flags override) compiles for the first connected slice
([contract](../../docs/restructure/slice1-contract.md) §2). The `default` profile is unchanged (the 53 pinned bodies and the
64 programs give the same code, sites, PC table and relocations as the S1a build).

- **Settings** (recorded in `settings`: `profile`, `block_cutting`, `exits`, plus the usual fields): 64-bit only, FEX TSO
  emulation off (hardware TSO, so plain `ldr`/`str`, `ordering: plain`), no end marker, dense PC table, multiblock off
  (`--multiblock` and `--sparse-pc` are refused; `--no-tso` and `--no-end-marker` are no-ops), FlagM required
  (`features.required_by_settings_profile`; `--feature flagm=0` is refused).
- **Exits.** `BranchOps.cpp::ExitFunction` takes FEX's existing `BLOCK_DEBUGGING` branch when the runtime flag
  `fexc_slice1` is set (the compile-time constant stays off: `Core.cpp::CompileBlock` would force one instruction per
  block). Every exit is `ldr x0,<rip literal>; str x0,[x28,#State.rip]; ldr x1,[x28,#Pointers.DispatcherLoopTop]; br x1`
  (computed targets store their register). The 8-byte RIP literal is placed in the code and registered as a
  `guest_rip_literal` relocation (FEX's debugging branch leaves it unregistered). No exit-linker literal is emitted, so there
  is no `named_symbol_literal`, L1 lookup, block link or x25 call-return stack; the only state pointer loaded is
  `DispatcherLoopTop`. Guest RIP constants for RIP-relative addresses and pushed return addresses stay `guest_rip_move`.
  The literals are data inside the code region (check_sites masks them from its disassembly).
- **Block cutting.** Before each instruction `GenerateIR` asks `shim/fexc_slice1.inc` (`FexcSlice1::Cut`). If the instruction
  is outside the allowlist, the block ends before it: `ExitFunction` to its RIP (or, when it is the first instruction, no
  code: `status: no_code`, rc 1, with `guest.end`). `guest.end.reason` is `system_instruction` or `unsupported_form`, with
  `detail` and `cut_opcode_map/cut_opcode/cut_fex_name`. A decoder end-of-input status (`partial_decode`,
  `not_executable`, `bad_relocation`) on the FIRST instruction also gives no code, with that status as the reason (never FEX's
  NoExec/SIGSEGV block); mid-block, FEX already ends the block before the undecodable bytes without emitting anything for
  them, and the status stays the end reason. Dispatch failures of allowlisted instructions stay `dispatch_failure`.
  `--object` is written only when the status is `ok`.
- **How instructions are classified.** The patched decoder records the opcode map (`DecodedInst::FexcMap`: primary,
  0f plain/F3/F2/66 overlay, 0f38, 0f3a, 3dnow); the key is (map, `OPRaw`, ModRM.reg for group opcodes) plus the decoder's
  prefix flags. FEX's table names are not usable alone (`MOV` is also 0f 20..23 and 8e), FEX does not decode every system
  instruction (0f 20..23 are two bytes without ModRM; lss, clac, invpcid fail to decode), and a decode failure
  keeps the map, opcode byte and ModRM. The system class wins over prefix checks; the allowlist never admits something that
  did not decode.
- **Allowlist.** Primary map: add/or/adc/sbb/and/sub/xor/cmp (all six forms), push/pop r, push imm, mov (88-8b, a0-a3,
  b0-bf, c6/c7 /0), lea, movsxd, imul (69/6b), test, group 1, shifts/rotates (c0/c1/d0-d3 except /6; rcl/rcr only with a register operand: FEX branches around the whole access for a
  static or dynamic zero count, which would lose a guest #PF), group 3 inc/dec-style
  ops (test /0, not, neg, mul, imul; div/idiv are `unsupported_form` until S1f), fe/ff inc/dec/push, nop, endbr64 (f3 0f 1e fa, a no-op) and register xchg
  (86/87 only with mod=3, 90-97), cbw/cwde/cdqe and cwd/cdq/cqo. Two-byte map (no mandatory-prefix overlay): nop (0f 1f), cmovcc,
  setcc, bt/bts/btr/btc (a3/ab/b3/bb, ba /4-7), imul, movzx, movsx, bsf/bsr, bswap. Branches (end the block): jcc rel8/rel32,
  jmp rel8/rel32 and near indirect, call rel32/indirect, ret, rep ret (f3 c3), ret imm16. All with no LOCK, REP/REPNE (except
  rep ret and endbr64), FS/GS override, address-size override, and (branches) no operand-size prefix; 66-prefixed bswap
  (undefined result) is cut.
- **System class** (reason `system_instruction`): segment push/pop and moves, lss/lfs/lgs, far call/jmp/ret, iret,
  int/int3/into/icebp, ud2/ud1/ud0, pushf/popf/cli/sti, hlt, port I/O, all of 0f 00 and 0f 01 (descriptor tables, invlpg,
  swapgs, rdtscp, xgetbv, ...), lar/lsl, syscall/sysret/sysenter/sysexit, clts, invd/wbinvd, clflush, mov cr/dr, wrmsr/rdmsr/rdpmc,
  rdtsc, cpuid, rd/wr fs/gs base, vmread/vmwrite, invept/invvpid/invpcid. Everything else outside the allowlist (string ops,
  LOCK forms, memory xchg, cmpxchg/xadd, x87/SSE/AVX, fences, cld/std, clc/stc, lahf/sahf, loop/jrcxz, shld/shrd,
  tzcnt/popcnt, prefetch, leave/enter, ...) is `unsupported_form`.

### Native control flow inside one guest instruction
`scan_skips.py` (and a test in `test_slice1.py`) looks, in the emitted code, for a forward branch inside one guest
instruction's native range that jumps over one of its recorded sites, and for admitted memory forms with no site at all. It scans
613 single-instruction forms (shifts and rotates by 1, CL and immediates 0/5/31/32/255 at 8/16/32/64 bits, memory and
register; bt family; cmov, bsf/bsr, imul, movzx/movsx, ALU forms, setcc, push/pop, call/jmp [m], ...) and the 63 fixture
programs that compile. Only rcl/rcr with a memory operand were found (a `cbz` on the count around the load/store, and no
access at all for a masked-to-zero static count: rcl/rcr by 0 or 32 at 8/16/64 bits); they are cut. All other forms access
memory unconditionally, including shifts and rotates by CL and by an immediate that masks to zero.

## Access sites (natemu-fexc-v2)

`sites[]`, sorted by `native_offset`, one entry per native instruction FEX emitted for a guest
memory access. Recorded in `Arm64JITCore::FexcAfterOp` (`shim/fexc_sites.inc`, called from the
JIT's IR-op dispatch loop in `JIT.cpp`) right after each IR op is emitted. At that point register
allocation is done, so the registers are final, and the op's emitted native range is known; the
`GuestOpcode` IR op in force (one per guest instruction, a metadata op that emits no code) names
the guest instruction. FEX emits IR ops in order and no later pass moves a memory op across a
`GuestOpcode`, except that `TryPostRAMerge` fuses two adjacent guest push or pop into one
`stp`/`ldp` (`PushTwo`/`PopTwo`); those sites are `push_pair`/`pop_pair` and carry both guest
instructions (`guest_offset`, `guest_offset2`). The dispatcher's CF inversion is captured where the
`GuestOpcode` op is created (after the per-instruction `FlushRegisterCache(true)`). Segment and
the x86 addressing form come from FEX's own decode of the instruction.

| field | meaning |
|---|---|
| `native_offset` | the faulting native instruction (last load/store-class word of the op's range with a base other than x28/sp) |
| `guest_offset`, `guest_offset2` | guest instruction(s), offset from the block RIP; `guest_offset2` only for fused pairs |
| `insn_native_offset` | native offset where that guest instruction's translation starts (its PC-table entry) |
| `ir_op` | FEX IR op (diagnostic) |
| `kind` | `load`, `store`, `load_pair`, `store_pair`, `push`, `pop`, `push_pair`, `pop_pair`, `other` (a guest-memory IR op the record cannot describe: a consumer must refuse it); `guard`, `split` reserved for S1b |
| `width` | bytes the native instruction accesses (pairs: both registers) |
| `intent`, `rmw` | `read`/`write`; for a memory-destination read-modify-write the load has `rmw: load`, `intent: write`, the store `rmw: store` (derived at emission: a load followed by a plain store in one guest instruction; valid because admitted instructions have at most one explicit memory operand) |
| `segment` | `ss` for stack ops and RSP/RBP-based operands, else `ds` (decides #SS(0) vs #GP(0)) |
| `base_reg`, `index_reg`, `extend`, `shift`, `imm` | native address form: base register number (31 = sp), optional index register with `uxtw`/`sxtw`/`sxtx` and shift, constant offset |
| `data_regs`, `data_class` | transfer register(s) (31 = xzr), `gpr`/`fpr` |
| `writeback`, `writeback_delta` | `pre`/`post` base update performed by the access itself (push: pre, -size; pop: post, +size) |
| `ordering` | `plain`, `acquire` (ldar family, TSO on), `release` |
| `nzcv`, `cf_inverted`, `flags_written_before_site` | guest NZCV is in PSTATE (`frame` is for the precision patches); CF inversion at the guest instruction start; an NZCV-writing IR op of this instruction precedes the site (approximate: only ops that write NZCV by contract, not inline compares) |
| `completed_effects` | `unverified`: nothing yet guarantees that no effect of the instruction is committed before the site |
| `located` | the faulting word was found in the op's range (false would be a recorder bug) |
| `x86` | FEX's decode of the explicit memory operand: `rip_relative`, `base`, `index` (x86 register numbers, -1 none), `scale`, `disp`; null for stack ops |

Example (`mov eax, [rsi]`, TSO on):

```json
{"native_offset":12,"guest_offset":0,"guest_offset2":null,"insn_native_offset":12,"ir_op":"LoadMemTSO","kind":"load","width":4,
 "intent":"read","rmw":"none","segment":"ds","base_reg":10,"index_reg":null,"extend":"none","shift":0,"imm":0,"data_regs":[4],
 "data_class":"gpr","writeback":"none","writeback_delta":0,"ordering":"acquire","nzcv":"pstate","cf_inverted":true,
 "flags_written_before_site":false,"completed_effects":"unverified","located":true,
 "x86":{"rip_relative":false,"base":6,"index":-1,"scale":1,"disp":0}}
```

Known deviations of FEX from x86 in the default profile, fixed under `--profile slice1` by the precision patches (S1b1):
`cmp`/`test`/`bt` with a memory operand whose flags are overwritten later lose the access entirely (flag elimination,
then dead-code elimination; `check_sites.py` reports ELIMINATED without `--slice1`). `push`/`pop` of adjacent guest
instructions are fused. Load and pop destinations can be static registers.

Accepted natemu policy: `bt`/`bts`/`btr`/`btc` with a memory operand access ONE byte (the byte holding the bit), for an
immediate and for a register bit index alike, not the x86 operand width. The fault, CR2 and A/D bits are those of that byte.
`check_sites.py` reports these as NARROWED and does not fail them.

The site's `imm`, `writeback` and `writeback_delta` (address = base register value before the access + `imm`; pre-index: `imm`
equals `writeback_delta`, post-index: `imm` is 0) are read from the emitted instruction word (FEX's `Push` with
`NeedsMoveAfterwards` emits `stur [base,#-n]` and updates the base with a later `sub`, which is no writeback of the access).

`--object FILE` (only when the status is `ok`) writes the binary form, version 2: a 128-byte header, the code (relocation
slots zeroed), then site, PC-entry and relocation tables, layout in `crates/abi/src/code_object.rs`. Besides what the JSON
carries per site (including `located`), the header records the profile, the `settings` bits (TSO, multiblock, dense PC, end
marker), the end reason code (the `guest.end.reason` string), the feature profile (`feature_bits`: bit i is the i-th member
of `features.host_features`, plus count and DCacheLineLog2). `check_sites.py` and `test_fexc.py` assert object and JSON agree.
Numeric codes are shared with `shim/fexc_hook.h`.

## Precision (S1b1)

Under `--profile slice1` only (the default profile's output is unchanged: identical JSON on the 64 fixtures, a 264-program
generated corpus and the 613-form matrix, except the host address of FEX's exit-linker literal), `patch.py` applies the
[contract](../../docs/restructure/slice1-contract.md) §1 patches 2-5, with predicates in `shim/fexc_precision.h`:

| patch | FEX file::function | change |
|---|---|---|
| 2 | `RedundantFlagCalculationElimination.cpp::Classify` | every guest-memory op (LoadMem, StoreMem, their TSO and pair forms, Push, Pop) reads all six flags, so no flag write before it is deleted or narrowed; `InvalidateFlags` (no code) is neither read nor write, else it would make the previous instruction's flags look dead just before an access |
| 2b | `...::EliminateDeadCode`, `RegisterAllocationPass.cpp::Run` (forward) | a guest load with a dead value (`cmp`/`test`/`bt [m]`) is kept; its destination register is freed at once |
| 3 | `RegisterAllocationPass.cpp::Run` (backwards) | a guest-memory op and a guest instruction boundary (`GuestOpcode`) count as an access of every static register, so a value is coalesced into its SRA register only if no access (and no instruction boundary) lies between its definition and its store; this covers constants pooled across instructions. A load's own value never gets an SRA register (DDI0487 D1.4.1.4); a pop's writeback stack pointer stays in RSP's register (the access is its only write) |
| 4 | `RegisterAllocationPass.cpp::Run` (forward) | `TryPostRAMerge`'s candidate is reset at `GuestOpcode`: no push/pop pair, no merge across instructions |
| 5 | `RedundantFlagCalculationElimination.cpp::Run` | an instruction with an NZCV writer (FEX's flag table or `ImplicitFlagClobber`) before one of its accesses gets `LoadNZCV; StoreContext` (`mrs xT, nzcv; str wT, [x28, #1032]`) right after its `GuestOpcode`; its sites say `nzcv=frame`. The slot is `CPUState.flags[RFLAG_NZCV_LOC]`, FEX's own NZCV home outside the JIT, reported as `state_abi.state_offsets.natemu_pre_nzcv` (JSON only; the binary object has no field for it) |

The patch-3 boundary barrier is stricter than the contract (which only names accesses): it keeps every static-register
write inside its own instruction, so each PC-table entry has the exact state and flag writes can be checked per instruction.

Per site (slice1): `nzcv` is `frame` when the instruction saved it, `cf_inverted` is the CF inversion at the instruction
start (valid for PSTATE and the frame copy, since nothing writes NZCV before the site otherwise), and `completed_effects`
is `none`, or `unverified` if the recorder sees an earlier store site or an unsaved NZCV write in the same instruction
(never seen). `flags_written_before_site` also counts `ImplicitFlagClobber` ops under slice1.

`verify_precision.py [--before FEXC0] [CORPUS.json ...]` checks the guarantee on the emitted code (docstring: P1-P7):
between a site's instruction start and the site no static-register (GPR, x26, x27, xmm) write, no store, no frame write
but the pre-NZCV save, no NZCV write unless `nzcv=frame` with the save first; load destinations not static registers; at
most one store site per instruction and last; no fused pair; `completed_effects=none`; no skippable site; for annotated
programs every flag a later site observes has a native write in its defining instruction (flags.py rules); plus
`check_sites` for programs with `asm` (no eliminated access). Corpora: fixtures, the scan_skips matrix, adversarial
programs per patch, and any `gen_slice1.py` corpus given. Against `s1c` it reports every defect class (SRA writes from
pooled constants and RMW flag results, unsaved NZCV writes, load destinations in SRA registers, fused pairs, eliminated
loads and flag writes); against `s1b1` it passes.

## Checks

- `python3 -m unittest tools/fexc/test_fexc.py`: structure, exact sites for hand-checked instructions, object file,
  identity of the 53 pinned old-producer bodies.
- `python3 -m unittest tools/fexc/test_slice1.py`: profile slice1. Every system class and unsupported form placed after two
  allowed instructions ends the block exactly at its RIP with the right reason and the same code as the two instructions alone;
  as the first instruction it gives no code; ~100 allowed forms and 11 branch forms translate; every exit in the branch cases
  and the 64 programs is the single gate (disassembled), with no named-symbol literal, no pointer load but
  `DispatcherLoopTop`, no x25; the 64 programs translate completely except those with div/idiv.
- `python3 tools/fexc/scan_skips.py`: the native-control-flow scan above.
- `python3 tools/fexc/verify_precision.py [--before FEXC0] [CORPUS.json ...]`: the slice1 precision guarantee (above).
- `python3 tools/fexc/check_sites.py [--known-ok] [--slice1]`: the 64 reference programs; expected accesses from the fixture `asm`
  against the sites, sites against the `xcrun objdump` disassembly of the code (every load/store with a base other than
  x28/sp must be a site and the reverse), dense versus sparse code bytes, binary object versus JSON.
