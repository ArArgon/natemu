# C0: static instruction census of the Debian 6.12.107 boot path

Chunk C0 of `docs/restructure/boot-features-plan.md` §3. It counts the x86 instruction forms the real boot binaries
contain and classifies each against what natemu admits today. Static counts only; nothing was executed.

Run order (raw data lives in `/Users/liaozping/orca/natemu-raw/kernel-census-20261028-01/`, nothing downloaded is committed):

```
python3 -I extract.py  RAW      # split bzImage, decompress vmlinux, unpack initramfs ELF files
python3 -I census.py   RAW      # objdump every executable section, classify  (python3 -I census.py --selftest)
python3 -I report.py   RAW .    # results.md, forms-kernel.tsv, RAW/forms-initrd.tsv
```

Files here: `extract.py`, `census.py`, `report.py`, `results.md` (every table, generated), `forms-kernel.tsv` (every refused
form of the decompressor and vmlinux, one row per mnemonic + operand shape, first-use order). The same for the
initramfs is `RAW/forms-initrd.tsv`; `RAW/census.json` holds every row.

## 1. Inputs

All from `https://deb.debian.org/debian/`, SHA256 checked.

| Input | URL (under `dists/trixie/main/installer-amd64/20250803+deb13u7/images/`) | Bytes | SHA256 | Checked against |
| --- | --- | --- | --- | --- |
| kernel `linux` | `netboot/debian-installer/amd64/linux` | 12,142,528 | `2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08` | INV-03-03 claim 13 (the full hash) and the versioned `SHA256SUMS` |
| `initrd.gz` | `netboot/debian-installer/amd64/initrd.gz` | 40,723,557 | `57303d157cffce3fa402301667fbc9b2280aa7372082b081e777375ae49da5f2` | INV-03-03 and the versioned `SHA256SUMS` |
| `SHA256SUMS` | `SHA256SUMS` | 80,699 | not hashed | source of the two rows above |
| kernel package | `pool/main/l/linux/linux-image-6.12.107+deb13-amd64-unsigned_6.12.107-1_amd64.deb` | 107,896,912 | `7d13cd82a1d377e40826c313f5dbbdb2edf1b48c713a375e4cfe5e207749cb4e` | `Packages.xz` of trixie, whose own SHA256 (`7778d3e3…2498`) matches `dists/trixie/InRelease` |
| `linux-config-6.12_6.12.107-1_amd64.deb` | `pool/main/l/linux/` | 1,507,312 | `77f7110636e83da8b130fa87da94191361a0125c7c4d6f207857c68e6a073953` | same chain; downloaded, not used |

- The config is `/boot/config-6.12.107+deb13-amd64` from the linux-image package (283,337 bytes, sha256
  `9b460f1003db4d417288e9c14caecfa14245dab07186df948fef0f3661e38ceb`). The `.deb` was deleted after extraction.
  Relevant options: `KERNEL_ZSTD`, `SMP`, `PARAVIRT`, `X86_5LEVEL`, `X86_KERNEL_IBT`, `EFI_MIXED`, `RELOCATABLE`, `RANDOMIZE_BASE`,
  `MITIGATION_RETPOLINE`, `BLK_DEV_INITRD` with every `RD_*` codec.
- The package's `vmlinuz` (12,141,056 bytes, sha256 `c32ad949…2d51`) equals the installer `linux` except 7 bytes in the first
  12,141,056 and 1,472 trailing bytes (probably the signature wrapper). All analysis uses the installer `linux`; the package
  image was not decompressed.
- Not done: the GPG signature of `InRelease` (key `4CB50190207B4758A3F73A796ED0E7B82643E131`) could not be checked, because the
  Debian keys fetched from ftp-master did not include that signing key. The trust chain is the recorded hashes plus
  `SHA256SUMS`/`InRelease` hashes over the same mirror.
- The `+deb13` kernel here is the trixie installer's; the plan's choice of Debian over Ubuntu is not touched.

## 2. Parts extracted

| Part | Where | Size |
| --- | --- | --- |
| real-mode setup | `bzImage[0:0x5000]` (`setup_sects` = 39, boot protocol 0x020f, `xloadflags` 0x7f) | 20,480 |
| decompressor head | protected-mode start + `[0:0x2cc)` (`payload_offset` = 0x2cc): `startup_32` at 0, `startup_64` at 0x200 | 716 |
| compressed payload | `payload_offset`, 0xb575e2 bytes, zstd | |
| decompressor tail | after the payload: `.text`, `.rodata`, `.data` of `arch/x86/boot/compressed` | 229,138 |
| vmlinux | payload decompressed with the `zstd` binary (the 4-byte size trailer matches), an ELF with section headers but no symbols | 57,644,792 |
| initramfs | `initrd.gz`, a gzip of one newc cpio: 2,511 entries, 102 ELF files (34.5 MB), `/init` is a 456-byte shell script | 67,584,000 |

`/init` runs `/bin/busybox init` after mounts and udev, so the boot chain is busybox (`/usr/bin/busybox`, also `/usr/bin/sh`),
the dynamic loader and libc/libm. The other 98 ELF files (45 executables, 53 libraries: udev, installer tools) are counted as separate groups.

## 3. Method

1. Each executable section was disassembled with Apple `llvm-objdump` (`/usr/bin/objdump`, x86-64). Raw bytes in the
   decompressor head/tail were wrapped in a one-section ELF (`wrap()` in `census.py`) so objdump can read them.
2. Admission is decided from the instruction **bytes**, by a Python port of `Primary`/`Two`/`Classify` in
   `tools/fexc/shim/fexc_slice1.inc` (including fences, `endbr64`, `pause`, `rep ret`, memory `xchg`, the prefix refusals)
   and the system decode in `crates/host/src/system.rs`. A refused form gets a class:
   `SSE` (SSE, MMX, AES, SHA, LDMXCSR), `AVX` (VEX/EVEX that are not BMI), `x87` (D8–DF, WAIT, FXSAVE/FXRSTOR),
   `LOCK` (an F0 prefix that is not memory XCHG), `string` (MOVS/CMPS/STOS/LODS/SCAS, with or without REP),
   `system` (the Sys verdicts of the allowlist: privilege, descriptors, MSR, I/O, CPUID, RDTSC, SYSCALL, UD2, flag control, port I/O),
   `BMI` (popcnt/lzcnt/tzcnt and the VEX BMI1/2 forms), `FS/GS` (an opcode that would be admitted but has an FS/GS
   override), `small-int` (cld/std/clc/stc/cmc, lahf/sahf, shld/shrd, cmpxchg/xadd/cmpxchg16b without LOCK, enter/leave,
   prefetch, movnti), `other` (all else, e.g. XSAVE, RDRAND, `bnd`/REPNE/address-size prefixes, ADX, MOVBE), `int3`
   (0xCC; the kernel pads functions with it; it is a system form but never executed in sequence, so it is its own column) and `bad`.
   `small-int` is part of "other" in the brief and is split out because it is exactly chunk I1.
3. A form is `mnemonic + operand shape` from the objdump text (`movq r,m`, `lock cmpxchgl r,m`). Prefixes that objdump prints as
   separate lines (`lock`, `data16`, `cs`) are glued to the next instruction (this was a bug that hid all LOCK sites at first).
4. First-use order is a region rank (decompressor head, decompressor tail, kernel head = first 0x2000 bytes of `.text`,
   `.init.text`, rest of `.text`, alternatives, `.exit.text`), then address. There are no symbols, so this is a layout
   proxy, not a trace.
5. Checks: `census.py --selftest` (22 byte-level cases from `test_slice1.py` and the plan); raw `grep` over the vmlinux
   `.text` disassembly agrees exactly with the classifier on `lock` prefixes (9,701), `int3` (3,030,037) and total instructions
   (7,018,174 lines = 7,008,473 plus 9,701 glued prefix lines); `rep` lines 5,009 vs 5,014 string instructions (5 without REP);
   `%fs:`/`%gs:` lines 35,834 vs 35,730 FS/GS-class (the other 104 are in refused instructions of another class).

### Limits

- Static linear sweep, not a trace: counts say what the binaries contain, not what a boot executes or how often. Many sites
  never run (BUG `ud2`, SEV/TDX/Xen paths, non-selected ifunc variants, error paths).
- Data inside `.text` is decoded as instructions. In the vmlinux this is small (`bad` = 0); in the decompressor tail the
  code part was cut at the first long ASCII run (0x1fd52 of 0x37f12), data between functions and 32-bit islands (EFI mixed
  mode, the 5-level `trampoline_32bit_src`) remain decoded as 64-bit. The head's 32-bit part (`[0,0x200)`, `startup_32`) was
  decoded as 32-bit but classified with the 64-bit rules (REX bytes are INC/DEC there), so its numbers are approximate
  and the 3 `bad` are those. The real-mode setup (20,480 bytes, 16-bit) was not classified: the 64-bit boot protocol
  never runs it, and owner decision 3 defers 32-bit and 16-bit code.
- The vmlinux ELF has merged section names: `.head.text`, `.entry.text`, `.noinstr.text` and the rest are one `.text`.
  No symbols (the shipped `System.map` is 83 bytes), so examples are addresses; reproduce with
  `objdump -d --start-address=.. x/vmlinux.elf`.
- Alternatives: the replacement side is `.altinstr_replacement` (9,057 instructions) and `.altinstr_aux` (2,214), counted as
  their own regions. The original side sits inside `.text` and is counted there, but sites are not paired with replacements
  (`.altinstructions` was not parsed). Which replacement applies depends on the CPUID/MSR profile (S1), and paravirt
  replacements (`cli`/`sti`/`pushfq` are about 1,500 of the system forms there) are patched in regardless of CPUID.
- Compilers dispatch at run time (glibc ifunc: AVX2/EVEX variants, ERMS, FSRM; zstd BMI2 variants in the decompressor). The
  initramfs AVX/SSE numbers overstate what a given CPU profile selects.
- `tzcnt` is `rep bsf` (F3 0F BC) in the kernel: it executes as BSF on a CPU without BMI1, so the profile decides what
  those sites mean. The same holds for `lzcnt`/`popcnt` sites behind alternatives.
- The admission classifier ports the allowlist's opcode logic, not FEX's decoder: forms FEX fails to decode would also be
  refused in practice. The non-canonical-branch cut is dynamic and not modelled.

## 4. Results

Counts per region (full table with percentages: `results.md` §A). "Admitted" excludes the int3 padding in its share.

| Region | Instructions | Admitted | Refused (excl. int3) | Admitted share |
| --- | --- | --- | --- | --- |
| decompressor head 32-bit (informational) | 93 | 71 | 22 | 76% |
| decompressor head 64-bit (`startup_64`) | 53 | 37 | 16 | 70% |
| decompressor tail (code part) | 31,934 | 31,441 | 491 | 98.5% |
| vmlinux `.text` | 7,008,473 (3,030,037 int3) | 3,910,414 | 68,022 | 98.3% |
| vmlinux `.init.text` | 167,902 | 165,110 | 2,735 | 98.4% |
| vmlinux `.altinstr_replacement` | 9,057 (24 int3) | 6,938 | 2,095 | 76.8% |
| vmlinux `.altinstr_aux` / `.exit.text` | 2,214 / 3,217 | 2,214 / 3,213 | 0 / 4 | 100% / 99.9% |
| initramfs chain (busybox, ld.so, libc, libm) | 578,752 | 490,544 | 88,208 | 84.8% |
| initramfs other executables (45 files) | 1,195,229 | 1,156,212 | 39,017 | 96.7% |
| initramfs libraries (53 files) | 3,157,050 | 2,690,480 | 465,961 | 85.2% |

Refused instructions by class (kernel = decompressor + vmlinux, all regions):

| Class | Kernel | Initramfs chain | Other initramfs (45 executables, 53 libraries) | Chunk |
| --- | --- | --- | --- | --- |
| FS/GS | 36,854 (vmlinux `.text` 35,730, `.init.text` 1,088, alt 36) | 6,113 | 21,486 | I7 |
| system | 17,283 | 615 | 738 | S2a, S2b, S3, X1a, L3 (see below) |
| LOCK | 10,020 (all inside admitted-opcode families: `lock xadd` 2,693, `or` 1,297, `dec` 995, `and` 750, `add`, `cmpxchg`, `inc`, `bts`/`btr`) | 565 | 820 | I3 |
| string | 5,725 (`rep stosq` 4,326, `rep stosl` 600, `rep movsq` 423, `rep movsl` 158, `rep movsb` 103) | 349 | 2,620 | I2 |
| small-int | 1,330 (`prefetcht0` 697, `leave` 217, `shrd`/`shld` 132, `cld` 61, `cmpxchg` 74 + 19 `cmpxchg16b`, `xadd` 81, `movnti` 18, `prefetchw` 17, `prefetcht1` 8, `std` 3, `clc` 3) | 324 | 6,784 | I1 |
| BMI | 1,939 (`tzcnt` 954 as `rep bsf`, `shrx`/`shlx` 788, `lzcnt` 68, `popcnt` 66, `bzhi` 48) | 706 | 6,795 | I1 (per profile) |
| SSE | 95 (+`ldmxcsr`, `emms`) | 48,917 | 149,283 | I5a, I5b |
| x87 | 31 (`fninit`, `fxsave64`/`fxrstor64`) | 13,411 | 1,029 | I6 |
| AVX | 64 (`vpxor` etc., `vzeroupper`, one AMX `tilerelease`) | 17,170 | 312,375 | I8 |
| other | 41 (`xsave*`/`xrstor*` family 26, `rdrand`/`rdseed` 9, `wrussq` 4, `tpause`, `clwb`) | 38 | 3,048 | I5b, S3 |

### Refused forms on the kernel boot path, by class, first use first

Kernel regions in first-use order; counts are over all kernel regions; `dec` = decompressor tail (offsets in the bzImage's
protected-mode image), other addresses are vmlinux virtual addresses. The complete lists (all operand shapes) are in
`forms-kernel.tsv`; `results.md` §B groups them per class.

- **small-int** (I1): `cld` (decompressor `startup_64` `0x200`, every entry), `std` (`dec 0x2a9`, with `rep movsq`), `prefetcht1/t0`
  (`dec 0x1d22`, `0x4106`, zstd), `leave` (217, from `0xffffffff81000da7`), `shrdq` (`.init.text 0xffffffff83373e27`),
  `cmpxchgl/q` (`0x81002c36`, `0x8100de6c`), `xadd` (`0x810698c3`), `shld`, `cmpxchg16b` (`0x818e7a38`), `movnti`, `prefetchw`.
  No `enter`, `lahf`/`sahf`, `stc`/`cmc`. `clc` 3.
- **system** (78 forms; the decompressor first): `cli` (`dec 0x1`... entry), `lgdt` (`dec head`), `mov` to/from CR and segment
  registers (165 + 165), `lret`, `rdmsr` 60/`wrmsr` 81 (`dec 0x10b/0x111`), `lldt`, `ltr`, `hlt`, `pushfq` 539/`popfq` 22,
  `cpuid` 59, `outb/outw/inb` (port 0x80 and serial in the decompressor, `dec 0x156`), `rdtsc` 112, `lidt`, `clflush`, `vmmcall`;
  kernel `.text`: `xgetbv`, `ud2` 9,839 and `ud1l` 3,764 (BUG/WARN/KASAN traps), `rmpadjust`, `iretq`, `vmcall`, `sti` 569,
  `pvalidate`, `inw/inl/outl`, `outsb`, `syscall` 2, `sgdt`, `rdfsbase/wrfsbase`, `rdpkru/wrpkru`, `lar`, `serialize`, `verw`,
  `wbinvd` 23, `xsetbv`, `lkgs`, `invlpg` 3 (already decoded by `system.rs`), `encls`, `ljmpl`, `monitor`/`mwait`,
  `rdpmc`, `invpcid`, `rep ins/outs`, `sidt`, `sldt`, `tdcall`, `swapgs` 22, `wrgsbase/rdgsbase`, `ud1`, `sysretq/l`, `lsl`;
  replacements: `clac` 190, `stac` 106, `rdtscp` 19, `clflushopt`. Only `mov cr`, `rdmsr`/`wrmsr`, `invlpg` are decoded today.
- **LOCK** (I3): `.init.text` first (`lock xadd` at `0xffffffff83354219`, `lock or` `…83356808`, `lock bts/btr` `…83362b9b`),
  then `.text` (`lock sub` `0x810036c1`, `lock cmpxchg` `0x81012fbb`). The decompressor has none.
- **string** (I2): decompressor first: `rep stosl` (`dec 0xa9`), `rep stosq` (`dec 0x29`, 4,326 in total: kernel page clearing),
  `rep movsq` (`dec 0x2aa`, with DF=1), `rep movsl`, `rep cmpsb` 3, `rep movsb` (`dec 0x1d1e5`), `.init.text` `rep stosb`,
  then non-REP `movsb/movsw` (`0x816bc6b8`, `0x81750757`), `rep stosw`. Plus `rep ins/outs` (system).
- **FS/GS** (I7): `.text 0xffffffff81000058` (`movq %gs:` in `startup_64`/`head_64` stack setup), `subq %gs:`
  (`…8100018d`, per-CPU), then `.init.text` `incl/movl/decl %gs:` (`…83358679`), `movslq`, `cmp`, `test`; `.text` `add`,
  `cmove`, `testb`, `cmpb`, `movb`, `movw`. 38 mnemonics, 53 forms (read-modify-write forms inc/dec/add/sub are the bulk).
- **BMI**: decompressor `lzcntl` (`dec 0x116b`), `tzcntl`, `shlx/shrx` (zstd, 788), `bzhi`, `andn`; `.init.text` `tzcntq`
  (`…83358e27`, `rep bsf`); `.altinstr_replacement` `popcntq/l` (66).
- **x87**: decompressor `fninit` (`dec 0x1d5d0`), `fnstsw`, `fnstcw` (FPU probe); `.init.text` `fxsave`, `fldl`/`fdivl`/`fmull`/
  `fsubp`/`fistpl`/`wait` (`…83369a1d`+, the FPU self-test), `fxsave64`; `.text` `fnclex`, `fildl`, `fxrstor64`
  (`0x81051bc8`, `0x81051c06`).
- **SSE** (95, all in `.text` from `0x81051b38`): `ldmxcsr` (`0x81051b38`) and `emms` (`0x81051bca`, in the FPU helpers), then
  vector routines using the kernel FPU: `movdqu/movdqa/movd`, `pxor`, `paddd/q`, `punpckl*`, `pshufd/b`, `psrld/pslld`, `por`
  (`0x810b297d`-`0x810b2a1a` first group). No scalar float SSE in the kernel.
- **AVX** (64): `vpxor`, `vpaddd`, `vmovdqa/u`, `vprord`, `vpshufd`, `vpermi2d`, `vextracti128`, `vzeroupper`, `tilerelease`
  (`0x810b2be4`+). These are kernel-FPU crypto routines.
- **other**: `rdrandq` (decompressor KASLR, `dec 0x1e002`), `xrstor(s)64`/`xsave64` (`.init.text 0x8336a7c2`, FPU init),
  `rdseedq`, `xsaves64` (`0x810550f9`), `xsaveopt64`/`xsavec64`/`clwb` (alternatives), `wrussq` 4 (CET), `tpause`.

### Kernel regions at a glance

- `.altinstr_replacement` carries 2,095 refused instructions (24 more are int3) in 9,057: `sti`/`cli`/`pushfq` paravirt replacements
  (about 1,540), `clac`/`stac` 296, `popcnt` 66, `prefetchw`, `cmpxchg16b`, `rdtsc(p)`, `wbinvd`, `clflush(opt)`, `wrmsr`,
  `vmmcall`, `verw`, `xsave*`. `.altinstr_aux` (2,214) is fully admitted.
- The decompressor `startup_64` (head 64-bit) refuses `cld`, `cli`, `lgdt`, `mov cr`, `lret`, `std`, `popfq`, `hlt`, `pushfq`,
  `cpuid`, `rdmsr`/`wrmsr` early on: these gate the first run (R1a).

### Initramfs

Chain (busybox, ld.so, libc, libm), per file (`results.md` §D): busybox 79,045 instructions, 77,455 admitted (98.0%): refused are
SSE 714 (`movups`, `movaps`, `pxor`, zeroing and copies), FS/GS 684 (stack canary `%fs:0x28`), string 189, system 2. ld.so:
34,470 of 37,085 admitted; SSE 2,198, AVX 172, system 126. libc: 317,469 of 347,981 admitted; FS/GS
4,565 (TLS), SSE 13,257, AVX 10,109 (ifunc variants), BMI 704, system 487 (`syscall`, `cpuid`, `rdtsc`). libm: 61,150 of 114,641; SSE 32,748, x87 12,978.
Over the whole initramfs the heaviest refused forms are SSE (`movdqa`, `pxor`, `movaps`, `movups`, scalar `movsd`/`mulsd`/
`cvtsi2sd`), AVX (`vpclmulqdq`, `vpxorq`, `vaesenc`, EVEX), `syscall` 648, `cpuid` 174, `rdtsc` 126, `xtest`, `leave`,
`shrd/shld`, `prefetch`, `cmpxchg`, `rorx`, `mulx`, `adcx/adox`, `movbe`, `addr32 call`, `lock cmpxchg/xadd`, `rep stos/movs`.

## 5. What this means for the plan

- Every refused class in §1c is hit by the kernel itself: FS/GS (I7, biggest by count), LOCK (I3), REP strings (I2),
  small-int (I1) and BMI inside the decompressor and `.init.text`, x87 `fninit`/`fxsave`/`fxrstor` (I6, boot-time FPU
  probing, with no x87 arithmetic in the steady kernel), and a small SSE/AVX set only inside kernel-FPU routines
  (I5a/I5b/I8 are needed for those code paths and for all of userspace, not for the early kernel path).
- New refusals to place: `ud2`/`ud1`, `rdrand`/`rdseed` (decompressor KASLR calls `rdrand` unless `nokaslr`), `xsave*`/`xrstor*`/
  `xsetbv`/`xgetbv`, `lar`/`lsl`/`verw`, `monitor`/`mwait`/`tpause`, `rdpkru`/`wrpkru`, `rdpmc`, `invpcid`, `serialize`,
  `rep ins/outs`, `wbinvd`, `swapgs`/`wr/rd fsbase gsbase`, `syscall`/`sysret`, `lkgs`, SEV/TDX forms (`pvalidate`, `rmpadjust`,
  `psmash`, `rmpupdate`, `tdcall`, `vmmcall`, `vmcall`, `encls`). These are `system` instructions that fexc cuts today; the host
  `system.rs` decodes only MOV CR, RDMSR/WRMSR and INVLPG. Typed refusal or CPUID-gating (S1) is the likely resolution for the SEV/TDX/SGX group.
- Phase-end check: rerun `extract.py`, `census.py`, `report.py` with the final admission and diff `forms-kernel.tsv`; every
  row whose class is not `admitted` must name its chunk or a typed refusal.
