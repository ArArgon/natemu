# x87 64-bit integer copies: how common, and what fixing them would take

natemu holds x87 registers as doubles (53-bit significand). A 64-bit integer loaded with FILD m64 and stored again with
FISTP m64 comes back exact on real hardware (the 80-bit format has a 64-bit significand) but is rounded under natemu once
it is above 2^53 (checkpoint A: 0x123456789abcdef1 became 0x123456789abcdf00). This page answers whether that load/store
pair is an established pattern, where it appears, and what the two fix candidates would catch: **(b)** compile a FILD m64
whose value reaches a FISTP m64 unmodified (found by dataflow over the block) as an exact 8-byte move; **(c)** Box64's way,
keep the loaded integer beside the double in each x87 slot and return it from FISTP m64 if the slot is unchanged.

Survey made 2026-11-20. Compiler output from Compiler Explorer (godbolt.org API); binaries scanned on the x86 reference
machine with a classifier written for this survey (method in section 2); all downloads deleted afterwards.

## Summary

- **It is an established, compiler-generated idiom.** gcc (4.7 to 16) and clang (11 onward) implement every 64-bit
  atomic load and store as FILD m64 then FISTP m64 whenever the target has an x87 but no SSE: `-march=i586`, `i686`,
  `pentium-mmx`, which is Debian i386's default. MSVC does the same with `/arch:IA32` (VS 2019 16.4 onward). Rust's
  i586 target does it too. Hand-written copies exist in OpenJDK's 32-bit HotSpot, Wine (since February 2025), Free
  Pascal's run-time library and old FreeBSD kernels.
- **In Debian 13 i386 it is in 27 of 640 popular packages, at 2,291 places**, among them RocksDB, Firefox, BIND, OpenJDK,
  CPython, Abseil, LLVM, SpiderMonkey, Node.js, Qt 5 and 6, TBB, OpenSSL, GStreamer, libtorrent and gcc's own libatomic.
  Debian 8 (gcc 4.9, 2015) has only 36: the idiom grew with C11/C++11 atomics.
- **The shapes are simple.** 81% are adjacent instructions; the other 19% have only ordinary integer instructions between
  the two (at most 11), put there by gcc's scheduler. None goes through x87 stack shuffles, an 80-bit spill or a branch.
  So (b) with "nothing x87 in between, same block" catches all 2,291 static sites; strict adjacency catches 81%.
- **What only (c) would catch is dynamic**: a FISTP that faults and is restarted (first write to a fresh or copy-on-write
  page), and the 22 cast round trips (`(int64_t)` of a long double that was an `int64_t`) where the value crosses a
  branch (10) or is duplicated with FLD ST(0) (12, which a dataflow (b) also catches).
- **Impact:** silent rounding of 64-bit values above 2^53 that pass through atomics: nanosecond timestamps (wall-clock ns
  since 1970 are always above 2^53), 64-bit hashes and seeds, tagged pointers and sequence locks, and the bit patterns of
  `double`s stored in 64-bit atomics (Abseil flags, `std::atomic<double>` under gcc). The worst case found is OpenJDK
  21's `long[]`/`double[]` array copy in the VM runtime, which corrupts arbitrary Java data.

## 1. Compilers: which emit the pair, at which versions and flags

Test source: `__atomic_load_n`/`__atomic_store_n` on a `long long` (seq_cst, relaxed, release, and a load-then-store), C11
`_Atomic`, `std::atomic<long long>` and `std::atomic<double>`, `-O2` unless stated.

| compiler | flags / target | 64-bit atomic load | 64-bit atomic store |
|---|---|---|---|
| gcc 4.4, 4.6 (`std::atomic`) | `-m32 -march=i686` | two 32-bit `movl` (tearing; no `__atomic` builtins yet) | two `movl` |
| gcc 4.7.4 … 16.2 (17 versions tried) | `-march=i586`, `pentium-mmx`, `i686`; also `-O0`, `-Os` | **`fildq m; fistpq stack`** | **`fildq stack; fistpq m`** (+ `lock orl $0,(%esp)` for seq_cst) |
| same | `-march=i386`, `i486` | call `__atomic_load_8` (libatomic) | call `__atomic_store_8` |
| same | `-march=i686 -msse` (SSE1 only) | `movlps` | `movlps` |
| same | `-msse2`, `-march=pentium4`, `-march=x86-64` | `movq xmm` | `movq xmm` (+ `mfence`) |
| same | `-mno-80387` | gcc 16: libatomic call; gcc 4.7: `lock cmpxchg8b` | same |
| clang 3.3 … 5 | `-m32 -march=i686` | libatomic call | libatomic call |
| clang 7, 8 | same | `lock cmpxchg8b` | `lock cmpxchg8b` |
| clang 9, 10 | same (also `i586`) | **FILD/FISTP** | `lock cmpxchg8b` |
| clang 11 … 22 | same | **FILD/FISTP** | **FILD/FISTP** |
| clang 11 … 22 | `-march=pentium4` | `movq`/`movsd` | `movsd` + `movlps` |
| MSVC 19.0 (VS2015) … 16.3 | `/O2`, x86 | calls `_Load_seq_cst_8` or a `cmpxchg8b` loop | `cmpxchg8b` loop |
| MSVC 16.4 … 19.51 (VS2019/2022/VS18) | `/O2` (default `/arch:SSE2`) | `movq xmm` (also `__iso_volatile_load64`) | `movq xmm` from 16.8 |
| same | `/O2 /arch:IA32` | **FILD/FISTP** (also `__iso_volatile_load64/store64`) | **FILD/FISTP** |
| MSVC any | plain `volatile __int64`; `_InterlockedCompareExchange64` | two `mov`; `lock cmpxchg8b` | |
| ICC 13, 19, 2021.10 | `-m32 -mia32` / with SSE2 | `lock cmpxchg8b` / `movq` (13: `cmpxchg8b`) | `cmpxchg8b` / `movq` |
| rustc 1.x (LLVM) | `i586-unknown-linux-gnu` | **FILD/FISTP** | **FILD/FISTP** |
| rustc | `i686-unknown-linux-gnu` (SSE2 baseline) | `movq` | `movsd`/`movlps` |
| Go 1.0 … master | any `GO386` | MMX `MOVQ (AX), M0` + `EMMS` | MMX `MOVQ` + `LOCK XADDL` fence |

Notes, each read from the output or source:

- gcc's pair is always emitted as two consecutive instructions, but its scheduler then moves unrelated integer
  instructions between them (gcc 4.9 put `movl; testl` between, gcc 14 a `movl`; seen in 19% of Debian 13 sites).
- gcc passes `std::atomic<double>` through the integer path too: the load is `fildq; fistpq; fldl` in gcc 4.9, 6 and 16
  (gcc 7 loaded with `fldl` directly; GCC PR71245 removed some of these bounces), the store is `fstpl; fildq; fistpq` in
  gcc 16. Under natemu this rounds the low bits of almost every double stored in an atomic.
- clang's driver defaults 32-bit Linux to `pentium4` (SSE2) unless told otherwise (driver source, not re-checked here), so
  clang reaches the x87 path only with an explicit `-march=i586/i686`; gcc reaches it by default on Debian i386.
- MSVC has defaulted to `/arch:SSE2` since VS2012, so new MSVC code uses MOVQ. But code built `/arch:IA32` exists: Notepad++
  8.7.1 x86 has an adjacent pair caching a 64-bit global (`fildll 0x89e710; fistpll -0x8(%ebp)`, later the reverse store),
  the shape of MSVC STL's cached `QueryPerformanceFrequency` (inferred from the shape, not from symbols).
- Precision control (the FPU control word, which Windows sets to 53 bits) does not affect FILD or FISTP (SDM: PC applies to
  FADD/FSUB/FMUL/FDIV/FSQRT only), so the pair is exact on hardware under any control word.
- Free Pascal: `rtl/i386/fastmove.inc` `Move_8OrMore_IA32` copies memory with up to four FILDs then FISTPs in another
  order; it is chosen only when CPUID shows neither SSE nor MMX, and FPC keeps a separate `Move_8OrMore_Valgrind` because
  "valgrind up to 3.3 has a bug which prevents the default code to work". Delphi was not checked.
- Other hand-written uses: OpenJDK HotSpot 32-bit (`linux_x86_32.S` `_Atomic_move_long` for `jlong` atomic load/store and
  `_Copy_conjoint_jlongs_atomic`; the interpreter's generated code for every `volatile long/double` field access,
  `barrierSetAssembler_x86.cpp`: `fild_d(src); fistp_d(Address(rsp,0))`; array-copy stubs without SSE2). Wine
  `include/winnt.h` `ReadNoFence64` on i386 is `fildq %1; fistpq %0` since merge request 7237 (February 2025, "also used to
  implement C11 atomics by i686-w64-mingw32-gcc"). FreeBSD 4.x–6.x `i586_bcopy` copies 64 bytes per loop with eight FILDs
  then eight FISTPs in reverse order (kernel copy routine for Pentium-class CPUs).
- Valgrind's x86 front end (`VEX/priv/guest_x86_toIR.c`, current HEAD) translates FILD m64 to `I64StoF64` and FISTP m64 to
  `F64toI64S`: it has exactly natemu's defect and has never special-cased it.
- **int64 to floating conversions** (FILD then arithmetic, FSTP to a double, compare, return in ST0) are the large majority of
  FILD m64 sites (below). They round to a double on hardware too as soon as the result is stored as a double; they are not
  what (b) or (c) target, and differ from hardware only where 80-bit intermediates matter (the general double-mode gap).

Confidence: high for every row (compiled output); medium for the clang default and the MSVC STL attribution.

## 2. Binaries

**Samples.** Debian 13 (trixie) i386: the first 600 architecture-specific packages in popularity-contest's install ranking
(kernels, firmware, -dev/-doc/-data packages skipped) plus 58 picked by hand (CPython, Wine, Qt, OpenJDK, Firefox,
LibreOffice, LLVM, RocksDB, gRPC, Node.js, PostgreSQL, Mesa, Erlang, Ruby, PHP …): 640 packages, 4,168 ELF/PE files, 263
million instructions. Debian 8 (jessie, gcc 4.9, i586 baseline) from archive.debian.org: the same names where they existed
plus that era's equivalents (OpenJDK 7, Python 3.4, LLVM 3.5 …): 452 packages, 166 million instructions. Windows 32-bit
release builds: Python 3.13.1 embeddable (MSVC, incl. `vcruntime140.dll`), Node.js 22.11 win-x86, Notepad++ 8.7.1
portable. Wine 10.0's 809 i386 PE modules (built with i686-w64-mingw32-gcc) are inside the Debian 13 sample.

**Classifier.** For each FILD m64 (`fildll`) in `objdump -d` output, simulate the x87 stack forward, tracking which
positions hold the unmodified loaded value through FXCH, FLD ST(i), FST/FSTP ST(i), FFREE, other values pushed and
popped, an FSTP m80 spill and FLD m80 reload of the same slot, FLDCW/FNSTCW, conditional branches (fall-through, marked
cross-block), any instruction that is a branch target (marked cross-block) and direct jumps (followed). The site ends at the
first of: FISTP/FISTTP m64 of the unmodified value (a copy), arithmetic or compare using it, FST to float/double, FIST
m16/m32, return in ST0, the value discarded, or a call/indirect jump (unresolved). Copies with an FLDCW in between are
`(int64_t)` casts of a value that was an integer, listed separately as cast round trips. Tested on hand-written cases for
each shape. Function names come from the nearest exported symbol, so they are approximate in stripped libraries.

| | Debian 13 | Debian 8 | Windows sample |
|---|---|---|---|
| FILD m64 sites | 27,878 in 257 packages | 20,830 in 172 packages | 45 |
| conversions (not targeted) | 25,487 | 20,748 | 18 |
| **copy, adjacent** | **1,852** | **29** | **2** (Notepad++) |
| **copy, only non-x87 instructions between (2–12 apart)** | **439** | **7** | 0 |
| copy through x87 stack shuffles | 0 | 0 | 0 |
| copy through an 80-bit spill and reload | 0 | 0 | 0 |
| copy across a branch or join | 0 | 0 | 0 |
| cast round trip (FLDCW between): same block / across a branch | 12 / 10 | 7 | 0 |
| unresolved (call, indirect jump, window end; mostly non-code bytes) | 78 (0.3%) | 39 | 25 (misdecoded data in node.exe) |

Copy shapes in Debian 13: 1,585 load-shaped (global or pointer to stack: an atomic load), 416 store-shaped (stack to
memory: an atomic store), 221 stack to stack, 69 memory to memory (copy loops). Distance: 1,852 at 1 instruction, 296 at
2, 86 at 3, 35 at 4, 23 at 5–12.

Packages with copies in Debian 13 (sites): librocksdb 883, firefox-esr 606, bind9-libs 246, librubberband 81,
openjdk-21-jre-headless 62, libsrt 60, mesa-vulkan-drivers 54, libpython3.13 52, libabsl 48, python3.13-minimal 40,
libllvm19 27, libmozjs-128 21, libqt6core 14, libnode115 12, libgstreamer-plugins-base 11, libpython3.13-stdlib 10,
libtorrent-rasterbar 10, libreoffice-core 10, libtbb12 9, samba-libs 8, libatomic1 6, libqt5core 5, isc-dhcp-client 4,
libgrpc++ 3, libssl3 3, libimagequant 3, libavformat 3. Debian 8: openjdk-7 22, firefox-esr (iceweasel) 14. No copies in
glibc, libstdc++, busybox, systemd (only cast round trips), Wine 10.0's PE modules, or the MSVC-built Python and its CRT.

What the copies carry (function names from the scan, values inferred from the code they belong to):

- **OpenJDK 21** `AccessInternal::arraycopy_conjoint<long long>`, `<double>`, `arraycopy_conjoint_atomic`,
  `arraycopy_arrayof_conjoint`: adjacent `fildll (%edx,%eax,8); fistpll (%ecx,%eax,8)` copy loops for Java `long[]` and
  `double[]`, plus `_Atomic_move_long`. Arbitrary user data; plus the interpreter's runtime-generated volatile-field code,
  which no static scan sees.
- **Abseil** `FlagImpl::StoreValue` / `ReadSequenceLock`: flag values of up to 8 bytes (including `double` flags) kept in
  64-bit atomics, and a sequence lock.
- **RocksDB** (`MemTable::Add`, `PipelinedWriteImpl`, histograms), **BIND** (zone timers, transfer statistics), **SRT**
  (`sync::TimePoint`), **Firefox** (profiler, `mozilla::Atomic<uint64_t>`), **GStreamer** ring buffer, **libtorrent**
  counters, **LLVM** statistics and DWARF linker offsets, **TBB**, **OpenSSL** `CRYPTO_atomic_load/store`, **Qt**
  (`qHashBits` seed, CPU feature word), **libatomic** `__atomic_load_8` itself (what `-march=i386/i486` code calls).
- Debian 8 has so few because 2015 code rarely used C11/C++11 64-bit atomics; Debian 13 has 64 times as many.

Confidence: high for the counts and shapes (deterministic scan, cases spot-checked by disassembly: BIND, RocksDB, OpenJDK,
systemd, LibreOffice, Erlang, Notepad++); medium for what each value holds.

## 3. Box64 and FEX

**Box64** (`src/dynarec/arm64/dynarec_arm64_df.c`, `src/emu/x64rundf.c`, main of 2026-11-19) has both mechanisms:

- *Within a block:* FILD m64 pushes an x87 cache entry of type `NEON_CACHE_ST_I64` holding the raw 64 bits. An instruction
  that needs the value as a double calls `neoncache_promote_double`, which rewrites the entry (and the FILD) as a double;
  if nothing does, FISTP m64 stores the raw register (`if(ST_IS_I64(0)) VST64(...)`). FXCH and the like are cache
  renames, so they keep the type. At a block end an I64 entry is converted to a double (`SCVTF`) when spilled. This is
  (b), done by the dynarec's multi-pass type inference, and it costs nothing (cheaper than the conversion).
- *Across blocks, 32-bit programs only:* `fpu_ll[8]` (`{int64 sq; int64 sref}` per physical slot, "for 64bits fild / fist
  sequence"). FILD m64 also writes the integer and the double it became to `fpu_ll[(top+i)&7]`. FISTP m64 reads the slot's
  current double, compares it with `sref`, and on a match stores `sq`; otherwise it rounds as usual. Nothing invalidates
  it: the check is by value. Cost from the emitted code: FILD m64 about 6–7 extra Arm instructions (load TOP, adjust and
  mask, address, two stores), FISTP m64 about 10 on the hit path (load TOP, address, load, compare, branch, load, store);
  **zero on every other x87 instruction**. This is (c).
- Weak points of the by-value check: FXCH or FLD ST(i) move values between physical slots without moving `fpu_ll`, so the
  exact integer is lost (falls back to rounding), and in the rare case where two different integers round to the same
  double and are swapped, FISTP returns the other one. In a system emulator the shadow is also not architectural state:
  a guest context switch between FILD and FISTP (FXSAVE/XSAVE, another thread's FILD, FXRSTOR) could leave another
  thread's integer in the shadow with an equal double. natemu would need to clear it on FXRSTOR/XRSTOR/FRSTOR/FNINIT, or
  better carry the exact value through the 80-bit image FXSAVE writes (an 80-bit value holds any int64 exactly).

**FEX upstream:** its default x87 mode is 80-bit soft float, which is exact; reduced-precision (double) mode, which natemu
uses, converts on FILD (`FILDF64`: `_Float_FromGPR_S`, `_PushStack(Converted, Invalid())`) and on FIST, with no integer
tracking. But FEX's `x87StackOptimizationPass` already has the machinery (b) needs: each stack entry carries an optional
`Source` (the raw loaded value and its size), copied by FXCH and FLD ST(i) (`OP_COPYPUSHSTACK`), dropped by arithmetic, and
`OP_STORESTACKMEM` stores the raw source when the sizes match ("fld dword [rax]; fst dword [rbx]" becomes `ldr; str`). It is
used only for FLD/FST: FILD passes no source and FIST goes through the dispatcher. Extending it means FILD m64 records its
GPR as an *integer* source (a kind flag, so FLD m64 then FISTP m64 is not mistaken for a copy) and FISTP/FISTTP m64 stores
that source when present. Only within the pass's fast path (one block, stack state known), which covers every site found.

Confidence: high (source read; Box64 not run).

## 4. Summary and judgement

| class | Debian 13 sites | software | strict-adjacent (b) | dataflow (b) | (c) |
|---|---|---|---|---|---|
| adjacent copy | 1,852 | RocksDB, Firefox, BIND, OpenJDK, Python, Abseil, LLVM … | yes | yes | yes |
| non-x87 instructions between | 439 | BIND (161), RocksDB (140), Python (42), Abseil, SRT, Qt6 … | no | yes | yes |
| x87 stack shuffles, 80-bit spill | 0 (FreeBSD `i586_bcopy`, FPC `Move`: hand-written) | | no | yes | Box64's by-slot: no; per-value: yes |
| across a branch | 0 | | no | no | yes |
| cast round trip (FLDCW) | 12 same block, 10 across a branch | systemd, Node.js, LibreOffice, Erlang | no | 12 | 22 |
| FISTP faults and restarts (COW, first touch) | dynamic | any store-shaped site (416) | no* | no* | yes |
| guest context switch between the two | dynamic, rare | any | yes (block-local) | yes | only with shadow saved/cleared |
| runtime-generated code (HotSpot interpreter, JITs) | not countable statically | OpenJDK 32-bit | yes | yes | yes |

\* Unless the pair's store reports a fault at the FILD's address with the x87 state unchanged, which is legal for an
adjacent pair (FILD only loads) and makes the restart re-run the whole pair.

Fractions of the 2,291 static copies: strict adjacency 81%; dataflow (b) 100%; (c) 100%, plus the 22 cast round trips
and the dynamic cases. Debian 8: adjacency 29 of 36 (81% again), dataflow 36 of 36.

**Judgement.** The pattern is the standard code for 64-bit atomics on any x87-only 32-bit target, from gcc, clang, MSVC
`/arch:IA32` and Rust i586, and Debian i386 builds exactly that target, so every 32-bit Debian program using a 64-bit atomic
has it. The corruption is silent and affects only values above 2^53 (or `double` bit patterns): counters, sizes, sequence
numbers and sample counts are almost always below that and unaffected; nanosecond wall-clock timestamps (about 2^60.6, so
off by up to 128 ns), Windows-style 100 ns times (about 2^57, off by up to 800 ns), random 64-bit seeds and hashes, tagged
pointers and lock words with high bits set, and doubles kept in 64-bit atomics are affected every time. The cases that break
real programs found here are OpenJDK 32-bit (`long[]`/`double[]` array copies and every volatile long/double field in the
interpreter: arbitrary data), Abseil `double`/`int64` flags, and any lock-free structure that packs a counter into the high
half. A dataflow (b) inside FEX's existing stack pass catches every static site found at no run-time cost; (c) adds the
fault-restart and cross-block cases at a few instructions per FILD/FISTP m64 and nothing elsewhere, but in natemu its shadow
must be cleared or saved with the FPU state.

Sources: Compiler Explorer API (gcc, clang, MSVC, ICC, rustc); Go `src/internal/runtime/atomic/atomic_386.s` and
release-branch.go1 `sync/atomic/asm_386.s`; Free Pascal `rtl/i386/fastmove.inc`; OpenJDK jdk17u/jdk21u `linux_x86_32.S`,
`barrierSetAssembler_x86.cpp`, `stubGenerator_x86_32.cpp`; Wine MR 7237 (gitlab.winehq.org/wine/wine/-/merge_requests/7237)
and `include/winnt.h`; FreeBSD stable/6 `sys/i386/i386/support.s`; Valgrind `VEX/priv/guest_x86_toIR.c`; GCC PR71245
(gcc.gnu.org/pipermail/gcc-patches/2016-May/449477.html); Box64 at 3e5a0b59; FEX at the pinned ac479dd.
