# M2 task 0: first check of the 32-bit plan's open premises

Research only, no emulator code. Date: November 20, 2026. Counts are static (instructions present in the file), not
how often they run; where that matters it is said. Packages were Debian 13 (trixie) i386 as of today, checked against the
archive's SHA-256 values, unpacked in scratch directories and deleted afterwards. Disassembly was GNU objdump on the x86-64
reference machine (Ryzen 7 8845HS, Ubuntu 26.04). The counting script was a short awk over `objdump -d` output.

## 1. The guest kernel supports 32-bit programs, and has them on by default

Which kernel: the one in `tests/resources.toml` (`debian-kernel`) and `testkit/inputs.py`: Debian's signed
`linux-image-6.12.107+deb13-amd64_6.12.107-1_amd64.deb` (the netboot kernel is the same 6.12.107+deb13). I downloaded that
exact package and read `boot/config-6.12.107+deb13-amd64` out of it:

```
CONFIG_IA32_EMULATION=y
# CONFIG_IA32_EMULATION_DEFAULT_DISABLED is not set
CONFIG_COMPAT=y   CONFIG_COMPAT_32=y   CONFIG_COMPAT_BINFMT_ELF=y   CONFIG_MODIFY_LDT_SYSCALL=y
CONFIG_X86_X32_ABI=y  CONFIG_X86_X32_DISABLED=y   # x32 is off, which does not matter here
# CONFIG_COMPAT_VDSO is not set                   # the 32-bit vDSO (with __kernel_vsyscall) is mapped
```

So 32-bit ELF programs run with no `ia32_emulation=1` on the command line. `modify_ldt` is there too (Wine, 16-bit
segments). Confidence: high (config of the exact package, in the framework's pin).

Consequence worth noting for the plan: Linux's 32-bit vDSO chooses the system call helper at boot from CPUID. It uses
SYSENTER when the CPU reports it (Intel profile: yes), SYSCALL when it is an AMD profile, and INT 0x80 otherwise. natemu
presents an Intel CPU, so the helper uses SYSENTER, matching the plan.

## 2. Debian 13 i386 exists for multiarch; its baseline is "i686 plus SSE2 on the CPU", but code is still x87

- Release notes, section "Reduced support for i386" (debian.org/releases/trixie/release-notes/issues): i386 is no longer a
  regular architecture; no kernel and no installer; "the architecture's sole remaining purpose is to support running legacy
  code, for example, by way of multiarch or a chroot on a 64-bit (amd64) CPU"; "Its instruction set requirements include
  SSE2 support, so it will not run successfully on most of the 32-bit CPU types that were supported by Debian 12."
- The i386 package index (`dists/trixie/main/binary-i386/Packages.xz`) lists libc6 2.41-12+deb13u4, libstdc++6 14.2.0-19,
  busybox-static, python3.13-minimal, libx264, libvorbis, libsdl2 and so on. So `libc6:i386` multiarch works. Confidence: high.
- What the compiler emits is a different question from what the CPU must have. Debian's gcc-14 source
  (`debian/rules2`, gcc-14 14.2.0-19) configures `--with-arch-32=i686` on i386 and amd64 (i586 only for the old jessie
  case). i686 has no SSE; `-mfpmath` therefore defaults to the x87. Measured in the binaries (section 3): python3.13 has
  zero SSE register uses in 955,943 instructions; libstdc++, libgmp, libvorbis, zlib, libpng-class code the same. Floating
  point in ordinary Debian i386 programs is x87. SSE2 appears only where a package deliberately builds or selects it (glibc's
  string functions and a few libm routines, chosen at run time by CPUID; libx264's `i686/sse2` variant directory). The SSE2
  requirement is the distribution's statement about the CPU, which the glibc and a few packages use, not a compiler default.
  Confidence: high for the compiler default (source) and the measured binaries; the release-notes sentence is quoted.

## 3. Instruction counts in Debian 13 i386 binaries

Columns: FISTTP (needs SSE3), FLDCW, 64-bit FILD (`fildll`), 64-bit FISTP (`fistpll`), and FILD m64 directly followed by
FISTP m64 (adjacent instructions), the trapping x87 set (FPREM, FPREM1, FSCALE, FYL2X, FYL2XP1, FPATAN, FPTAN, FSIN, FCOS,
FSINCOS, F2XM1, FXTRACT, FBLD, FBSTP), and the instruction total. FBLD/FBSTP were never found.

| file | insns | FISTTP | FLDCW | FILD m64 | FISTP m64 | adjacent pair | trapping x87 |
|---|---|---|---|---|---|---|---|
| libc.so.6 (2.41) | 448,954 | 0 | 3 | 4 | 0 | 0 | 3 (FSCALE) |
| libm.so.6 | 210,560 | 0 | 359 | 29 | 21 | 0 | 124 |
| ld-linux.so.2 | 40,692 | 0 | 0 | 0 | 0 | 0 | 0 |
| libc.a (static) | 418,376 | 0 | 3 | 4 | 0 | 0 | 3 (FSCALE) |
| libstdc++.so.6 | 399,502 | 0 | 38 | 18 | 15 | 0 | 0 |
| python3.13 | 955,943 | 0 | 104 | 101 | 52 | 27 (37 within 3 insns) | 0 |
| busybox-static | 481,337 | 0 | 44 | 16 | 10 | 0 | 0 |
| libvorbis.so.0 | 27,359 | 0 | 60 | 1 | 0 | 0 | 49 |
| libgmp.so.10 | 132,201 | 0 | 4 | 2 | 1 | 0 | 0 |
| libz.so.1 | 20,977 | 0 | 0 | 0 | 0 | 0 | 0 |
| libx264 (generic) | 318,006 | 0 | 238 | 282 | 11 | 0 | 188 |
| libx264 i686/sse2 variant | 446,608 | 0 | 20 | 202 | 10 | 0 | 0 |

Trapping set inside libm: FYL2X 26, FPATAN 28, FSCALE 22, FYL2XP1 16, F2XM1 11, FXTRACT 9, FPREM 6, FPREM1 6 (none of
FSIN, FCOS, FPTAN, FSINCOS: glibc's sine, cosine and tangent are C code). libvorbis: FSCALE 10, FPATAN 12, FYL2X 9,
F2XM1 8, FSINCOS 6, FSIN 2, FCOS 2. libx264 generic: F2XM1 66, FYL2X 56, FSCALE 66 (inlined pow, log, exp).

Findings:

- **FISTTP is absent everywhere (0 in about 3.5 million instructions).** Debian's i386 baseline has no SSE3, so the cast
  sequence is FNSTCW, FLDCW (truncating copy), FISTP, FLDCW, as the plan assumes. FLDCW is the common one: 359 in libm,
  104 in Python, 44 in busybox, 238 in generic libx264. Each cast uses two.
- **The 8-byte FILD then FISTP pair is real, but only in some programs.** CPython has 27 adjacent pairs (37 within three
  instructions), e.g. `fildll (%ecx); fistpll 0x38(%esp)` followed by integer loads of the two halves: that is gcc's
  64-bit atomic load (`_Py_atomic` style) through the x87, copying 8 bytes without tearing. glibc, libstdc++, busybox, libm,
  libvorbis, libgmp, libz and libx264 have no adjacent pair (libstdc++ has the instructions apart, from conversions;
  `std::atomic<int64_t>` is inlined into user code, so it appears in the program that uses it, not in libstdc++).
  Remaining FISTP m64 uses are casts to `long long` (`fldcw; fistpll; fldcw`, seen in CPython) and conversions.
  Confidence: high for the counts; the pair is gcc's known idiom, **inferred** from the code shape (load, store to stack,
  then two 4-byte reads), not from the source.
- **The trapping x87 set is concentrated in libm, libvorbis-like numeric libraries and inlined math.** libc and the loader
  have only 3 FSCALE (printf's floating-point path). Python and busybox have none. How often they run is not measured here;
  a program calling `pow`, `exp`, `log`, `atan2`, `fmod` in a loop traps on each call if it reaches these routines. Some
  libm routines also have SSE2 or FMA variants that glibc selects by CPUID at run time (libm holds about 400 SSE2-style
  instructions in a handful of ifunc variants, probably `exp`, `log`, `pow`, `sin`, `cos`, `atan`, **inferred**, not
  confirmed by symbol names); with the CPU features natemu advertises, those calls may avoid the x87 routines, so the
  dynamic trap rate needs measuring on a real run (open item below).
- **SSE2 in the baseline:** libc.so.6 has 17,206 lines using XMM registers (6,144 SSE2-class), all from CPUID-selected
  string and memory functions; busybox (static glibc) 10,833; libm 532. Everything else here has zero. So the guest
  needs SSE2 in CPUID to run glibc as shipped, but the application code is x87.

## 4. INT 0x80 versus the vDSO helper

| program | how it makes system calls |
|---|---|
| glibc dynamic (libc.so.6) | `call *%gs:0x10` (the kernel's `__kernel_vsyscall`) at 468 sites; `int $0x80` at 6 sites: the two signal-return trampolines inside the sigaction code, `vfork`, `clone` (twice), and one more in a small wrapper (sem_trywait's neighbourhood). |
| dynamic loader (ld-linux.so.2) | `int $0x80` at 54 sites and no vDSO call: the loader runs before thread-local storage and the vDSO pointer exist, so every system call it makes (open, read, mmap, mprotect, close of each shared library) is INT 0x80. Every dynamically linked program starts this way. |
| glibc static (libc.a) | `int $0x80` in brk, clone, clone3, vfork, sigaction, early allocation, the startup code (libc-start, libc-tls: 5, dl-support, dl-printf) and `libc-do-syscall-int80`; 459 `call *%gs:0x10` sites elsewhere. A static busybox: 18 INT 0x80 and 245 vDSO calls. |
| signal return | glibc installs its own `__restore` / `__restore_rt` (SA_RESTORER) which executes `int $0x80` (sigreturn or rt_sigreturn). The kernel's vDSO also offers `__kernel_sigreturn` and `__kernel_rt_sigreturn`, which use INT 0x80 too. So every signal return is an INT 0x80. |
| Go 386 runtime | INT 0x80 for every system call: `src/runtime/sys_linux_386.s` defines `INVOKE_SYSCALL` as `INT $0x80` ("using int $0x80 is as fast as calling __kernel_vsyscall", the call through `0x10(GS)` is commented out), used 42 times in that file. Only clock_gettime and nanotime use the vDSO's function. |

So: the vDSO helper carries the bulk of a libc program's system calls, but INT 0x80 is never negligible: every dynamic
program's start-up, every signal return, clone and vfork (process creation, hot in `configure`/`make`), and all of Go.
Both paths need a fast route; the plan's "SYSENTER only" view would leave these. Confidence: high (disassembly and Go
source read today).

## 5. FEX's 32-bit ASM tests

- FEX at the pinned commit (`ac479dd`) has **245** `.asm` files under `third_party/FEX/unittests/32Bit_ASM/` (all with
  `"Mode": "32BIT"`; X87 holds 138 of them, including `DF_05`/`DF_07` which are FILD/FISTP m64 and
  `invalid_fist_overflow_64bit`). Its lists: `Disabled_Tests` (2 tests, undefined behaviour or rounding), `Known_Failures`
  (`D9_F8`). The 64-bit ones under `unittests/ASM/` are 2,270 files, none with a `Mode` key.
- Today's runner is `tools/fexc/upstream_tests/run_asm.py`. Its skip rule, in `one()`:
  `if h.get('MODE', '64BIT').upper() != '64BIT': return {..., 'outcome': 'unsupported', 'reason': '32-bit code'}`
  (docstring: "`Mode` 32-bit is out of the slice"). In practice the rule is never reached for them: `testkit/artifacts.py`
  `_fex_asm()` extracts only `unittests/ASM` (`git archive <pin> unittests/ASM`) and the runner walks that directory
  (`rglob('*.asm')`), so the 245 files are not handed to it at all. The rule is only exercised by a synthetic self-test
  case (`bits32.asm`). Nothing else in `testkit/`, `tests/` or `tools/` mentions `32Bit_ASM`. Confidence: high (read).
- To use them in task 1 onwards the framework must also archive `unittests/32Bit_ASM` and the runner must accept the mode.

## 6. Box64 and FILD/FISTP m64

Read `src/dynarec/arm64/dynarec_arm64_df.c` on GitHub (main branch, fetched today). Box64's x87 is doubles, and it
**does** keep 64-bit integers exact for the FILD/FISTP pair, in two ways:

- A register loaded by FILD m64 is tagged as an integer register (`NEON_CACHE_ST_I64`), and a following FISTP m64 stores the
  raw 64 bits with no conversion (`if(ST_IS_I64(0)) VST64(...)`).
- In 32-bit mode (`rex.is32bits`), FILD m64 also saves the original integer next to the converted double in the emulator
  state (`fpu_ll[8]`, "for 64bits fild / fist sequence": a pair of the integer and the double it became). FISTP m64 compares
  the register's current double to the saved copy and, if equal (the value was not changed in between), stores the saved
  integer, so the value survives exactly even above 2^53; otherwise it rounds the double.

So Box64 treats this as a problem worth special handling, and only for 32-bit programs. Confidence: high (read the
source); I did not run it.

## What this means for the plan

- **The 8-byte FILD/FISTP worry is real but narrow.** No system library has the adjacent atomic-load pair; CPython does
  (27 places). It matters for programs using `std::atomic<int64_t>` or `long long` volatile loads compiled for i686. Values
  above 2^53 (hashes, pointers packed in 64 bits, flag words with high bits set) would silently change. Box64 handles it;
  FEX's double mode does not. The narrow fix in the design (compile a FILD m64 directly followed by a FISTP m64 in one
  block as one 8-byte move) matches the idiom as it appears in CPython (`fildll mem; fistpll stack`, adjacent). It should
  also keep a Box64-style exact integer when a loaded 64-bit integer is stored again unchanged later; a plain adjacent-pair
  rule would cover the atomic idiom but not that. Decide between the two before the cast-fix task.
- **FLDCW casts are the common case, FISTTP does not exist.** 0 FISTTP and 800+ FLDCW in the libraries counted; Debian's
  i686 default with x87 maths means every `(int)x` and `(long long)x` is the four-instruction sequence with two FLDCW. The
  cast fix in the design is needed, not optional; libm alone has 359 FLDCW (plus 154 FNSTCW).
- **The trapping x87 instructions are mostly confined to maths libraries.** libm has 124, libvorbis 49, generic libx264
  188; libc 3; Python, busybox, libstdc++, libgmp, libz none. A pure-integer or string workload (`configure`, `make`,
  compilers, shells) never meets them; a numeric or media workload does, on each pow/exp/log/atan call. The dynamic rate is
  unmeasured. Not common enough to change the plan, but task 1's test programs should include one that calls libm.
- **INT 0x80 stays in the picture.** The dynamic loader uses it for every system call it makes at start-up (54 sites, no
  alternative), every signal return uses it, as do clone and vfork, and Go uses it for everything. The SYSENTER path is the
  bulk for steady-state libc code, but a fast path for INT 0x80 is needed too, not only for `__kernel_vsyscall`.
- **Kernel and packages are ready.** The framework's pinned Debian 13 kernel needs no boot option, and Debian 13 i386
  packages install by multiarch. The CPU must advertise SSE2 (glibc i386 selects its SSE2 routines) while application code
  stays x87.

## Open items (not answerable without a run)

- How many trapping x87 instructions and FLDCW run per second in real i386 programs (a profile on the x86 reference
  machine with `perf` or a natemu run through `tools/natemu-test` once 32-bit works). A cheap first step: a small i386 C program
  calling `pow`, `exp`, `log`, `atan2`, `fmod`, `sin`, `cos` under gdb on the reference machine to see which libm routines
  reach the x87 transcendentals with an SSE2 CPU.
- Which libm functions have SSE2/FMA ifunc variants in this build (needs symbol names; the shipped file is stripped).
- Whether glibc's x87 helper `__sqrtl`-style paths and `FPREM` loops matter in practice (libm's 6 FPREM, 6 FPREM1).
