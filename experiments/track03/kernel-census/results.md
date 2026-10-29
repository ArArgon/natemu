### A. Instruction counts by class (static, linear sweep)

| Region | Total | admitted | SSE | AVX | x87 | LOCK | string | system | int3 | BMI | FS/GS | small-int | other | bad | admitted share (int3 padding excluded) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dec head 32-bit [0,0x200) | 93 | 71 | 0 | 0 | 0 | 0 | 1 | 17 | 0 | 0 | 0 | 1 | 0 | 3 | 76.3% |
| dec head 64-bit [0x200,end) | 53 | 37 | 0 | 0 | 0 | 0 | 1 | 12 | 0 | 0 | 0 | 3 | 0 | 0 | 69.8% |
| dec tail 64-bit, code part [0,0x1fd52) | 31,934 | 31,441 | 0 | 0 | 3 | 0 | 21 | 53 | 2 | 401 | 0 | 12 | 1 | 0 | 98.5% |
| vmlinux .altinstr_aux | 2,214 | 2,214 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 100.0% |
| vmlinux .altinstr_replacement | 9,057 | 6,938 | 0 | 0 | 0 | 0 | 3 | 1,948 | 24 | 66 | 36 | 30 | 12 | 0 | 76.8% |
| vmlinux .exit.text | 3,217 | 3,213 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 99.9% |
| vmlinux .init.text | 167,902 | 165,110 | 0 | 0 | 15 | 319 | 685 | 593 | 57 | 18 | 1,088 | 12 | 5 | 0 | 98.4% |
| vmlinux .text | 7,008,473 | 3,910,414 | 95 | 64 | 13 | 9,701 | 5,014 | 14,656 | 3,030,037 | 1,454 | 35,730 | 1,272 | 23 | 0 | 98.3% |
| initrd:init chain (busybox, ld.so, libc, libm) | 578,752 | 490,544 | 48,917 | 17,170 | 13,411 | 565 | 349 | 615 | 0 | 706 | 6,113 | 324 | 38 | 0 | 84.8% |
| initrd:other executables | 1,195,229 | 1,156,212 | 21,156 | 5,888 | 174 | 238 | 1,616 | 387 | 0 | 406 | 8,810 | 295 | 47 | 0 | 96.7% |
| initrd:other libraries | 3,157,050 | 2,690,480 | 128,127 | 306,487 | 855 | 582 | 1,004 | 351 | 609 | 6,389 | 12,676 | 6,489 | 3,001 | 0 | 85.2% |

### B. Kernel side (decompressor + vmlinux): refused mnemonics by class, in first-use order

Per class: distinct mnemonics (operand shapes merged), ordered by first use (region rank, then address) for the kernel and by count for the initramfs; shapes = distinct operand shapes (r register, m memory, i immediate, a absolute address, x/y/z xmm/ymm/zmm, sr segment register).


**SSE**: 95 instructions, 15 distinct mnemonics, 19 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `ldmxcsr` | 1 | 1 | vmlinux:.text@ffffffff81051b38 |
| `emms` | 1 | 1 | vmlinux:.text@ffffffff81051bca |
| `movdqu` | 6 | 2 | vmlinux:.text@ffffffff810b297d |
| `movdqa` | 10 | 2 | vmlinux:.text@ffffffff810b2986 |
| `movd` | 17 | 2 | vmlinux:.text@ffffffff810b299e |
| `paddq` | 1 | 1 | vmlinux:.text@ffffffff810b29ca |
| `pxor` | 13 | 2 | vmlinux:.text@ffffffff810b29dc |
| `punpckldq` | 8 | 1 | vmlinux:.text@ffffffff810b2a0e |
| `punpcklqdq` | 4 | 1 | vmlinux:.text@ffffffff810b2a16 |
| `paddd` | 12 | 1 | vmlinux:.text@ffffffff810b2a1a |
| `pshufb` | 4 | 1 | vmlinux:.text@ffffffff810b2a26 |
| `psrld` | 4 | 1 | vmlinux:.text@ffffffff810b2a39 |
| `pslld` | 4 | 1 | vmlinux:.text@ffffffff810b2a3e |
| `por` | 4 | 1 | vmlinux:.text@ffffffff810b2a44 |
| `pshufd` | 6 | 1 | vmlinux:.text@ffffffff810b2aa8 |

**AVX**: 64 instructions, 12 distinct mnemonics, 17 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `vmovdqu` | 8 | 3 | vmlinux:.text@ffffffff810b2be4 |
| `vmovd` | 1 | 1 | vmlinux:.text@ffffffff810b2bf2 |
| `vmovdqa` | 9 | 4 | vmlinux:.text@ffffffff810b2bf6 |
| `vpaddq` | 1 | 1 | vmlinux:.text@ffffffff810b2c28 |
| `vpxor` | 13 | 1 | vmlinux:.text@ffffffff810b2c30 |
| `vpermi2d` | 2 | 1 | vmlinux:.text@ffffffff810b2c59 |
| `vpaddd` | 12 | 1 | vmlinux:.text@ffffffff810b2c6d |
| `vprord` | 8 | 1 | vmlinux:.text@ffffffff810b2c7a |
| `vextracti128` | 2 | 1 | vmlinux:.text@ffffffff810b2c90 |
| `vpshufd` | 6 | 1 | vmlinux:.text@ffffffff810b2cb9 |
| `vzeroupper` | 1 | 1 | vmlinux:.text@ffffffff810b2d54 |
| `tilerelease` | 1 | 1 | vmlinux:.text@ffffffff81d01048 |

**x87**: 31 instructions, 14 distinct mnemonics, 14 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `fninit` | 7 | 1 | dec:.text@1d5d0 |
| `fnstsw` | 2 | 1 | dec:.text@1d5d2 |
| `fnstcw` | 2 | 1 | dec:.text@1d5d6 |
| `fxsave` | 1 | 1 | vmlinux:.init.text@ffffffff83369a1d |
| `fldl` | 2 | 1 | vmlinux:.init.text@ffffffff83369aef |
| `fdivl` | 1 | 1 | vmlinux:.init.text@ffffffff83369af5 |
| `fmull` | 1 | 1 | vmlinux:.init.text@ffffffff83369afb |
| `fsubp` | 1 | 1 | vmlinux:.init.text@ffffffff83369b07 |
| `fistpl` | 1 | 1 | vmlinux:.init.text@ffffffff83369b09 |
| `wait` | 2 | 1 | vmlinux:.init.text@ffffffff83369b0d |
| `fxsave64` | 4 | 1 | vmlinux:.init.text@ffffffff8336a7df |
| `fnclex` | 1 | 1 | vmlinux:.text@ffffffff81051bc8 |
| `fildl` | 1 | 1 | vmlinux:.text@ffffffff81051bcc |
| `fxrstor64` | 5 | 1 | vmlinux:.text@ffffffff81051c06 |

**LOCK**: 10,020 instructions, 27 distinct mnemonics, 38 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `lock xaddl` | 2,557 | 1 | vmlinux:.init.text@ffffffff83354219 |
| `lock orb` | 1,250 | 1 | vmlinux:.init.text@ffffffff83356808 |
| `lock addl` | 704 | 2 | vmlinux:.init.text@ffffffff83358727 |
| `lock andb` | 720 | 1 | vmlinux:.init.text@ffffffff83361875 |
| `lock btrq` | 327 | 2 | vmlinux:.init.text@ffffffff83362b9b |
| `lock btsq` | 655 | 2 | vmlinux:.init.text@ffffffff83362bc4 |
| `lock xaddq` | 136 | 1 | vmlinux:.init.text@ffffffff83385fc8 |
| `lock incl` | 679 | 1 | vmlinux:.init.text@ffffffff8338a430 |
| `lock cmpxchgl` | 688 | 1 | vmlinux:.init.text@ffffffff83398230 |
| `lock decl` | 941 | 1 | vmlinux:.init.text@ffffffff833998f0 |
| `lock incq` | 163 | 1 | vmlinux:.init.text@ffffffff8339ef31 |
| `lock addq` | 252 | 2 | vmlinux:.init.text@ffffffff8339f534 |
| `lock subq` | 330 | 2 | vmlinux:.text@ffffffff810036c1 |
| `lock cmpxchgq` | 355 | 1 | vmlinux:.text@ffffffff81012fbb |
| `lock decq` | 54 | 1 | vmlinux:.text@ffffffff8107ec2c |
| `lock cmpxchgb` | 6 | 1 | vmlinux:.text@ffffffff8109781c |
| `lock orl` | 42 | 2 | vmlinux:.text@ffffffff8112698a |
| `lock andq` | 15 | 2 | vmlinux:.text@ffffffff81149db1 |
| `lock subl` | 94 | 2 | vmlinux:.text@ffffffff8117211e |
| `lock andl` | 15 | 2 | vmlinux:.text@ffffffff811b8777 |
| `lock xorl` | 1 | 1 | vmlinux:.text@ffffffff8127734b |
| `lock xorq` | 1 | 1 | vmlinux:.text@ffffffff81277523 |
| `lock orq` | 5 | 2 | vmlinux:.text@ffffffff81277598 |
| `lock xorb` | 22 | 2 | vmlinux:.text@ffffffff8131ff75 |
| `lock cmpxchg16b` | 5 | 1 | vmlinux:.text@ffffffff813b9ebc |
| `lock cmpxchgw` | 2 | 1 | vmlinux:.text@ffffffff81862941 |
| `lock btsl` | 1 | 1 | vmlinux:.text@ffffffff81d12d38 |

**string**: 5,725 instructions, 10 distinct mnemonics, 10 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `rep stosl` | 600 | 1 | dec:.text@a9 |
| `rep movsq` | 423 | 1 | dec:.text@2aa |
| `rep stosq` | 4,326 | 1 | dec:.text@29 |
| `rep movsl` | 158 | 1 | dec:.text@1a3f3 |
| `rep cmpsb` | 3 | 1 | dec:.text@1cc75 |
| `rep movsb` | 103 | 1 | dec:.text@1d1e5 |
| `rep stosb` | 84 | 1 | vmlinux:.init.text@ffffffff83354844 |
| `movsb` | 8 | 1 | vmlinux:.text@ffffffff816bc6b8 |
| `movsw` | 4 | 1 | vmlinux:.text@ffffffff81750757 |
| `rep stosw` | 16 | 1 | vmlinux:.text@ffffffff817b91a9 |

**system**: 17,283 instructions, 78 distinct mnemonics, 90 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `cli` | 475 | 1 | dec:.text@1 |
| `lgdtl` | 1 | 1 | dec:.text@1a |
| `movl` | 165 | 4 | dec:.text@22 |
| `lretl` | 3 | 1 | dec:.text@3b |
| `rdmsr` | 60 | 1 | dec:.text@10b |
| `wrmsr` | 81 | 1 | dec:.text@111 |
| `lldtw` | 3 | 1 | dec:.text@115 |
| `ltrw` | 3 | 1 | dec:.text@11d |
| `lgdtq` | 8 | 1 | dec:.text@257 |
| `lretq` | 3 | 1 | dec:.text@264 |
| `movq` | 165 | 4 | dec:.text@276 |
| `popfq` | 22 | 1 | dec:.text@292 |
| `hlt` | 12 | 1 | dec:.text@49 |
| `pushfq` | 539 | 1 | dec:.text@52 |
| `cpuid` | 59 | 1 | dec:.text@5b |
| `outb` | 397 | 2 | dec:.text@156 |
| `inb` | 197 | 2 | dec:.text@164 |
| `outw` | 47 | 2 | dec:.text@176 |
| `rdtsc` | 112 | 1 | dec:.text@1e01f |
| `clflush` | 14 | 1 | dec:.text@1f392 |
| `lidtq` | 6 | 1 | dec:.text@1fb8a |
| `vmmcall` | 51 | 1 | vmlinux:.text@ffffffff8100002b |
| `xgetbv` | 5 | 1 | vmlinux:.text@ffffffff81000303 |
| `ud2` | 9,839 | 1 | vmlinux:.text@ffffffff8100062c |
| `rmpadjust` | 6 | 1 | vmlinux:.text@ffffffff81000965 |
| `iretq` | 12 | 1 | vmlinux:.init.text@ffffffff8336474f |
| `vmcall` | 12 | 1 | vmlinux:.init.text@ffffffff83374092 |
| `inl` | 40 | 1 | vmlinux:.init.text@ffffffff833740b5 |
| `movw` | 48 | 2 | vmlinux:.init.text@ffffffff833864ce |
| `outl` | 41 | 2 | vmlinux:.init.text@ffffffff833da815 |
| `sti` | 569 | 1 | vmlinux:.text@ffffffff81005528 |
| `pvalidate` | 3 | 1 | vmlinux:.text@ffffffff81005d62 |
| `inw` | 14 | 2 | vmlinux:.text@ffffffff81029c01 |
| `outsb` | 1 | 1 | vmlinux:.text@ffffffff8102d06a |
| `syscall` | 2 | 1 | vmlinux:.text@ffffffff8103328b |
| `sgdtq` | 4 | 1 | vmlinux:.text@ffffffff8103c402 |
| `rdfsbaseq` | 4 | 1 | vmlinux:.text@ffffffff8103e6ab |
| `wrfsbaseq` | 2 | 1 | vmlinux:.text@ffffffff8103ed00 |
| `rdpkru` | 18 | 1 | vmlinux:.text@ffffffff8103ed18 |
| `wrpkru` | 7 | 1 | vmlinux:.text@ffffffff8103ed2f |
| ... 38 more, see TSV | | | |

**int3**: 3,030,120 instructions, 1 distinct mnemonics, 1 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `int3` | 3,030,120 | 1 | dec:.text@13f |

**BMI**: 1,939 instructions, 13 distinct mnemonics, 16 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `shlxl` | 21 | 1 | dec:.text@100f |
| `lzcntl` | 68 | 1 | dec:.text@116b |
| `tzcntl` | 8 | 1 | dec:.text@176c |
| `shrxl` | 12 | 2 | dec:.text@1a83 |
| `shlxq` | 317 | 2 | dec:.text@1f86 |
| `shrxq` | 438 | 1 | dec:.text@1f8b |
| `bzhiq` | 48 | 1 | dec:.text@3596 |
| `andnq` | 4 | 1 | dec:.text@8254 |
| `tzcntq` | 946 | 2 | vmlinux:.init.text@ffffffff83358e27 |
| `rorxl` | 5 | 1 | vmlinux:.text@ffffffff816c2002 |
| `sarxq` | 6 | 1 | vmlinux:.text@ffffffff816d45d9 |
| `popcntq` | 36 | 1 | vmlinux:.altinstr_replacement@ffffffff83722d14 |
| `popcntl` | 30 | 1 | vmlinux:.altinstr_replacement@ffffffff83722f25 |

**FS/GS**: 36,854 instructions, 38 distinct mnemonics, 53 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `movq` | 15,604 | 3 | vmlinux:.text@ffffffff81000058 |
| `subq` | 11,489 | 1 | vmlinux:.text@ffffffff8100018d |
| `incl` | 2,094 | 1 | vmlinux:.init.text@ffffffff83358679 |
| `movl` | 1,962 | 3 | vmlinux:.init.text@ffffffff8335869b |
| `decl` | 2,362 | 1 | vmlinux:.init.text@ffffffff833586fa |
| `cmpq` | 138 | 3 | vmlinux:.init.text@ffffffff8335dc7f |
| `testl` | 35 | 1 | vmlinux:.init.text@ffffffff83360f3d |
| `cmpl` | 71 | 3 | vmlinux:.init.text@ffffffff83362a09 |
| `movslq` | 111 | 1 | vmlinux:.init.text@ffffffff83389a79 |
| `addq` | 903 | 3 | vmlinux:.text@ffffffff8100223c |
| `cmovel` | 41 | 1 | vmlinux:.text@ffffffff810057b4 |
| `addl` | 146 | 2 | vmlinux:.text@ffffffff8100ebd0 |
| `testb` | 98 | 1 | vmlinux:.text@ffffffff810136bb |
| `cmpb` | 69 | 1 | vmlinux:.text@ffffffff81029826 |
| `movb` | 169 | 2 | vmlinux:.text@ffffffff81029843 |
| `movw` | 9 | 1 | vmlinux:.text@ffffffff8102c7eb |
| `testw` | 135 | 1 | vmlinux:.text@ffffffff8102ccb2 |
| `movzwl` | 18 | 1 | vmlinux:.text@ffffffff8102cd78 |
| `andl` | 19 | 1 | vmlinux:.text@ffffffff810377bf |
| `cmoveq` | 1 | 1 | vmlinux:.text@ffffffff810448d6 |
| `movzbl` | 36 | 1 | vmlinux:.text@ffffffff8104b839 |
| `orq` | 8 | 2 | vmlinux:.text@ffffffff81061044 |
| `decq` | 295 | 1 | vmlinux:.text@ffffffff8107b2e6 |
| `incq` | 939 | 1 | vmlinux:.text@ffffffff810ab6e8 |
| `cmpw` | 15 | 1 | vmlinux:.text@ffffffff810cae95 |
| `orw` | 7 | 2 | vmlinux:.text@ffffffff810cb6d2 |
| `orl` | 5 | 2 | vmlinux:.text@ffffffff811571b1 |
| `subl` | 1 | 1 | vmlinux:.text@ffffffff812024c5 |
| `movsbq` | 13 | 1 | vmlinux:.text@ffffffff81353644 |
| `movsbl` | 4 | 1 | vmlinux:.text@ffffffff81353cff |
| `cmovsl` | 2 | 1 | vmlinux:.text@ffffffff8143767b |
| `incw` | 9 | 1 | vmlinux:.text@ffffffff81a3ac1a |
| `decw` | 8 | 1 | vmlinux:.text@ffffffff81a3ac3c |
| `shlq` | 1 | 1 | vmlinux:.text@ffffffff81d13dd4 |
| `pushq` | 1 | 1 | vmlinux:.text@ffffffff81e000ad |
| `btq` | 4 | 1 | vmlinux:.text@ffffffff81e00189 |
| `btrq` | 6 | 1 | vmlinux:.text@ffffffff81e00194 |
| `sarq` | 26 | 1 | vmlinux:.altinstr_replacement@ffffffff837228e9 |

**small-int**: 1,330 instructions, 21 distinct mnemonics, 24 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `cld` | 61 | 1 | dec:.text@0 |
| `std` | 3 | 1 | dec:.text@2a9 |
| `prefetcht1` | 8 | 1 | dec:.text@1d22 |
| `prefetcht0` | 697 | 2 | dec:.text@4106 |
| `leave` | 217 | 1 | vmlinux:.text@ffffffff81000da7 |
| `shrdq` | 66 | 2 | vmlinux:.init.text@ffffffff83373e27 |
| `cmpxchgl` | 19 | 1 | vmlinux:.text@ffffffff81002c36 |
| `cmpxchgq` | 44 | 1 | vmlinux:.text@ffffffff8100de6c |
| `xaddb` | 10 | 1 | vmlinux:.text@ffffffff810698c3 |
| `shldq` | 64 | 2 | vmlinux:.text@ffffffff81098047 |
| `xaddw` | 1 | 1 | vmlinux:.text@ffffffff810a7b5a |
| `xaddl` | 54 | 1 | vmlinux:.text@ffffffff81202fc6 |
| `xaddq` | 16 | 1 | vmlinux:.text@ffffffff81217e84 |
| `cmpxchgb` | 11 | 1 | vmlinux:.text@ffffffff8135364b |
| `shldl` | 1 | 1 | vmlinux:.text@ffffffff817427a7 |
| `cmpxchg16b` | 19 | 1 | vmlinux:.text@ffffffff818e7a38 |
| `shrdl` | 1 | 1 | vmlinux:.text@ffffffff81c79a70 |
| `movntiq` | 14 | 1 | vmlinux:.text@ffffffff81ca63af |
| `movntil` | 4 | 1 | vmlinux:.text@ffffffff81ca6416 |
| `clc` | 3 | 1 | vmlinux:.text@ffffffff81ca6606 |
| `prefetchw` | 17 | 1 | vmlinux:.altinstr_replacement@ffffffff837259c6 |

**other**: 41 instructions, 11 distinct mnemonics, 11 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `rdrandq` | 7 | 1 | dec:.text@1e002 |
| `xrstors64` | 8 | 1 | vmlinux:.init.text@ffffffff8336a7c2 |
| `xrstor64` | 8 | 1 | vmlinux:.init.text@ffffffff8336a7d3 |
| `rdseedq` | 2 | 1 | vmlinux:.init.text@ffffffff833c2f04 |
| `xsave64` | 3 | 1 | vmlinux:.text@ffffffff81051a47 |
| `xsaves64` | 3 | 1 | vmlinux:.text@ffffffff810550f9 |
| `wrussq` | 4 | 1 | vmlinux:.text@ffffffff8109cf0a |
| `tpause` | 1 | 1 | vmlinux:.text@ffffffff81ca6af6 |
| `xsaveopt64` | 2 | 1 | vmlinux:.altinstr_replacement@ffffffff837240b2 |
| `xsavec64` | 2 | 1 | vmlinux:.altinstr_replacement@ffffffff837240b6 |
| `clwb` | 1 | 1 | vmlinux:.altinstr_replacement@ffffffff8372aea4 |

**bad**: 3 instructions, 1 distinct mnemonics, 1 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `decl` | 3 | 1 | dec:.text@56 |

#### B2. Heaviest 15 refused forms (mnemonic + operand shape) in the kernel side

| class | form | count | first use |
|---|---|---|---|
| int3 | `int3 ` | 3,030,120 | dec:.text@13f |
| FS/GS | `movq m,r` | 15,389 | vmlinux:.text@ffffffff81000058 |
| FS/GS | `subq m,r` | 11,489 | vmlinux:.text@ffffffff8100018d |
| system | `ud2 ` | 9,839 | vmlinux:.text@ffffffff8100062c |
| string | `rep stosq r,m` | 4,326 | dec:.text@29 |
| system | `ud1l r,r` | 3,764 | vmlinux:.text@ffffffff81d14305 |
| LOCK | `lock xaddl r,m` | 2,557 | vmlinux:.init.text@ffffffff83354219 |
| FS/GS | `decl m` | 2,362 | vmlinux:.init.text@ffffffff833586fa |
| FS/GS | `incl m` | 2,094 | vmlinux:.init.text@ffffffff83358679 |
| FS/GS | `movl m,r` | 1,905 | vmlinux:.init.text@ffffffff8335869b |
| LOCK | `lock orb i,m` | 1,250 | vmlinux:.init.text@ffffffff83356808 |
| LOCK | `lock decl m` | 941 | vmlinux:.init.text@ffffffff833998f0 |
| FS/GS | `incq m` | 939 | vmlinux:.text@ffffffff810ab6e8 |
| BMI | `tzcntq r,r` | 926 | vmlinux:.init.text@ffffffff83358e27 |
| FS/GS | `addq m,r` | 756 | vmlinux:.text@ffffffff8100223c |

#### B3. Kernel refused mnemonics in `.altinstr_replacement` (alternatives replacement side)

| class | mnemonic | count |
|---|---|---|
| system | `sti` | 561 |
| system | `pushfq` | 509 |
| system | `cli` | 467 |
| system | `clac` | 190 |
| system | `stac` | 106 |
| BMI | `popcntq` | 36 |
| BMI | `popcntl` | 30 |
| system | `movq` | 28 |
| FS/GS | `sarq` | 26 |
| int3 | `int3` | 24 |
| system | `rdtsc` | 19 |
| system | `rdtscp` | 19 |
| small-int | `prefetchw` | 17 |
| system | `wbinvd` | 15 |
| small-int | `cmpxchg16b` | 13 |
| FS/GS | `movq` | 10 |
| system | `clflush` | 7 |
| system | `wrmsr` | 7 |
| system | `vmmcall` | 7 |
| system | `verw` | 5 |
| other | `xrstors64` | 5 |
| system | `clflushopt` | 4 |
| system | `vmcall` | 3 |
| string | `rep movsb` | 3 |
| other | `xsaveopt64` | 2 |
| other | `xsavec64` | 2 |
| other | `xsaves64` | 2 |
| system | `movl` | 1 |
| other | `clwb` | 1 |

### C. Initramfs binaries: refused mnemonics by class (all 3 groups merged, ordered by count)

Per class: distinct mnemonics (operand shapes merged), ordered by first use (region rank, then address) for the kernel and by count for the initramfs; shapes = distinct operand shapes (r register, m memory, i immediate, a absolute address, x/y/z xmm/ymm/zmm, sr segment register).


**SSE**: 198,200 instructions, 186 distinct mnemonics, 285 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `movdqa` | 28,743 | 3 | usr/lib/x86_64-linux-gnu/libpsx.so.2.75:.text@226e |
| `pxor` | 21,858 | 2 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@1392 |
| `movaps` | 19,417 | 4 | usr/lib/x86_64-linux-gnu/libdebconfclient.so.0.0.0:.text@1312 |
| `movups` | 14,964 | 3 | usr/lib/libtextwrap.so.1.0.0:.text@139f |
| `movdqu` | 12,004 | 3 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@1d03 |
| `movsd` | 10,268 | 3 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@14b4 |
| `movq` | 7,941 | 5 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@16e1 |
| `movd` | 4,150 | 6 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@1474 |
| `movss` | 3,582 | 3 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@3071 |
| `ucomisd` | 3,369 | 2 | usr/lib/libiw.so.30:.text@34e2 |
| `mulsd` | 3,305 | 2 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@1ec9 |
| `paddd` | 3,171 | 2 | usr/lib/libexpat.so.1.12.3:.text@4a35 |
| `cvtsi2sd` | 3,139 | 1 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@1eb5 |
| `por` | 3,018 | 2 | usr/lib/x86_64-linux-gnu/libuuid.so.1.3.0:.text@261f |
| `addsd` | 2,975 | 2 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@2075 |
| `movapd` | 2,679 | 2 | usr/sbin/haveged:.text@5189 |
| `pshufd` | 2,627 | 2 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@147c |
| `pand` | 2,553 | 2 | usr/lib/libz.so.1.3.1:.text@4dcf |
| `pslld` | 2,467 | 2 | usr/lib/udev/ata_id:.text@544a |
| `psrld` | 2,436 | 2 | usr/lib/udev/ata_id:.text@5445 |
| `aesenc` | 2,356 | 1 | usr/lib/ossl-modules/fips.so:.text@6b9b1 |
| `pcmpeqb` | 2,166 | 2 | usr/lib/x86_64-linux-gnu/libblkid.so.1.1.0:.text@217e6 |
| `comisd` | 1,974 | 2 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@1f34 |
| `punpcklqdq` | 1,928 | 1 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@1d3c |
| `pmovmskb` | 1,779 | 1 | usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2:.text@235dc |
| ... 161 more, see TSV | | | |

**AVX**: 329,545 instructions, 318 distinct mnemonics, 797 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `vpclmulqdq` | 41,748 | 5 | usr/lib/ossl-modules/fips.so:.text@78484 |
| `vpxorq` | 35,193 | 6 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@141ec |
| `vaesenc` | 34,137 | 4 | usr/lib/ossl-modules/fips.so:.text@6c220 |
| `vpxor` | 19,427 | 4 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@11f5c |
| `vbroadcastf64x2` | 13,728 | 1 | usr/lib/ossl-modules/fips.so:.text@10f3da |
| `vpternlogq` | 13,490 | 6 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@15a25 |
| `vmovdqu64` | 12,517 | 7 | usr/lib/x86_64-linux-gnu/libgcrypt.so.20.5.0:.text@a4128 |
| `vmovdqu` | 11,525 | 6 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@7919 |
| `vpshufb` | 11,064 | 6 | usr/lib/ossl-modules/fips.so:.text@6e1a3 |
| `vpaddd` | 9,539 | 6 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@f796 |
| `vmovdqa64` | 8,083 | 9 | usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2:.text@27c2a |
| `vpslldq` | 7,683 | 3 | usr/lib/ossl-modules/fips.so:.text@6e286 |
| `vextracti32x4` | 7,430 | 2 | usr/lib/ossl-modules/fips.so:.text@77f2b |
| `vmovdqu8` | 7,045 | 7 | usr/lib/ossl-modules/fips.so:.text@77e2b |
| `vpsrldq` | 5,110 | 3 | usr/lib/ossl-modules/fips.so:.text@6e236 |
| `vaesdec` | 5,036 | 4 | usr/lib/ossl-modules/fips.so:.text@6c9c0 |
| `vextracti64x4` | 4,381 | 1 | usr/lib/x86_64-linux-gnu/libgcrypt.so.20.5.0:.text@c0115 |
| `vpsrld` | 4,325 | 3 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@f6ff |
| `vaesenclast` | 3,667 | 4 | usr/lib/ossl-modules/fips.so:.text@6c636 |
| `vmovdqa` | 3,444 | 6 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@41a4 |
| `vpslld` | 3,332 | 3 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@f7ad |
| `vmovups` | 2,824 | 6 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@90bf |
| `vpor` | 2,663 | 4 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@f213 |
| `vpaddq` | 2,608 | 6 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@fc8b |
| `vpand` | 2,581 | 4 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@f083 |
| ... 293 more, see TSV | | | |

**x87**: 14,440 instructions, 80 distinct mnemonics, 87 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `fstp` | 2,464 | 1 | usr/bin/fallocate:.text@424d |
| `fldt` | 2,411 | 1 | usr/bin/fallocate:.text@4233 |
| `fxch` | 1,673 | 1 | usr/lib/libgcc_s.so.1:.text@607f |
| `fstpt` | 1,098 | 1 | usr/bin/fallocate:.text@419e |
| `fld` | 865 | 1 | usr/bin/fallocate:.text@42d5 |
| `fmul` | 635 | 2 | usr/lib/libgcc_s.so.1:.text@6090 |
| `faddp` | 476 | 1 | usr/lib/libgcc_s.so.1:.text@6740 |
| `fabs` | 369 | 1 | usr/lib/libgcc_s.so.1:.text@6758 |
| `fucomi` | 355 | 1 | usr/lib/libgcc_s.so.1:.text@6744 |
| `fmulp` | 293 | 1 | usr/lib/libgcc_s.so.1:.text@6914 |
| `fldz` | 287 | 1 | usr/bin/fallocate:.text@4205 |
| `fucompi` | 278 | 1 | usr/lib/libgcc_s.so.1:.text@6770 |
| `fld1` | 248 | 1 | usr/lib/libgcc_s.so.1:.text@6079 |
| `flds` | 243 | 1 | usr/lib/libgcc_s.so.1:.text@690a |
| `fnstsw` | 224 | 2 | usr/lib/libgcc_s.so.1:.text@67f3 |
| `fcompi` | 220 | 1 | usr/lib/libgcc_s.so.1:.text@73ee |
| `fchs` | 213 | 1 | usr/lib/libgcc_s.so.1:.text@6800 |
| `fxam` | 194 | 1 | usr/lib/libgcc_s.so.1:.text@67f1 |
| `fadd` | 181 | 2 | usr/lib/libgcc_s.so.1:.text@7438 |
| `fsubrp` | 174 | 1 | usr/bin/fallocate:.text@42ec |
| `fcomi` | 163 | 1 | usr/lib/libgcc_s.so.1:.text@73bc |
| `fldcw` | 160 | 1 | usr/bin/fallocate:.text@42d7 |
| `fsubp` | 109 | 1 | usr/lib/libgcc_s.so.1:.text@673e |
| `fsub` | 104 | 2 | usr/lib/libgcc_s.so.1:.text@745c |
| `fmuls` | 85 | 1 | usr/bin/fallocate:.text@42ee |
| ... 55 more, see TSV | | | |

**LOCK**: 1,385 instructions, 21 distinct mnemonics, 24 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `lock cmpxchgl` | 628 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@2563 |
| `lock orq` | 155 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@23d8 |
| `lock xaddl` | 143 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@34b6 |
| `lock addl` | 123 | 2 | usr/lib/ossl-modules/legacy.so:.text@7e7c |
| `lock cmpxchgq` | 88 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@248e |
| `lock subl` | 62 | 2 | usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2:.text@11fe9 |
| `lock cmpxchg16b` | 60 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@4205 |
| `lock xaddq` | 25 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@3657 |
| `lock cmpxchgw` | 22 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@27f8 |
| `lock cmpxchgb` | 20 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@27c0 |
| `lock addq` | 13 | 2 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@41df5 |
| `lock orl` | 13 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@8f31d |
| `lock xaddb` | 8 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@3176 |
| `lock xaddw` | 8 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@3316 |
| `lock andl` | 6 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@94c32 |
| `lock subq` | 3 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@41ea3 |
| `lock btsw` | 2 | 1 | usr/lib/libasound.so.2.0.0:.text@9e42e |
| `lock decl` | 2 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@998f2 |
| `lock decq` | 2 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@9f8c6 |
| `lock incl` | 1 | 1 | usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2:.text@1bd30 |
| `lock btsl` | 1 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@90040 |

**string**: 2,969 instructions, 8 distinct mnemonics, 8 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `rep stosq` | 1,303 | 1 | usr/lib/cdebconf/frontend/newt/plugin-terminal.so:.text@18e9 |
| `rep movsb` | 570 | 1 | usr/lib/udev/pcmcia-check-broken-cis:.text@1844 |
| `rep stosl` | 438 | 1 | usr/bin/resolv:.text@10fe |
| `rep movsq` | 332 | 1 | usr/lib/x86_64-linux-gnu/libatomic.so.1:.text@28ce |
| `rep stosb` | 210 | 1 | usr/lib/udev/pcmcia-check-broken-cis:.text@184c |
| `rep movsl` | 113 | 1 | usr/bin/kbd_mode:.text@11d2 |
| `movsb` | 2 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@589f0 |
| `stosl` | 1 | 1 | usr/lib/udev/scsi_id:.text@53f9 |

**system**: 1,353 instructions, 25 distinct mnemonics, 25 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `syscall` | 648 | 1 | usr/lib/x86_64-linux-gnu/libpsx.so.2.75:.text@20fd |
| `cpuid` | 174 | 1 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@22e7 |
| `rdtsc` | 126 | 1 | usr/lib/x86_64-linux-gnu/libhavege.so.2.0.0:.text@3a7e |
| `outb` | 85 | 1 | usr/bin/bterm:.text@a896 |
| `xtest` | 71 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@16a8b8 |
| `hlt` | 55 | 1 | usr/sbin/console-type:.text@10d1 |
| `ud2` | 55 | 1 | usr/sbin/dhcp6c:.text@25f9 |
| `outsl` | 31 | 1 | usr/lib/ossl-modules/legacy.so:.text@13a0a |
| `insb` | 17 | 1 | usr/lib/ossl-modules/legacy.so:.text@13a11 |
| `outsb` | 16 | 1 | usr/lib/ossl-modules/legacy.so:.text@13a0e |
| `clflush` | 12 | 1 | usr/lib/ossl-modules/legacy.so:.text@7e78 |
| `xgetbv` | 9 | 1 | usr/lib/libgcc_s.so.1:.text@4c7d |
| `<unknown>` | 8 | 1 | usr/lib/ossl-modules/legacy.so:.text@13a1d |
| `outl` | 7 | 1 | usr/lib/x86_64-linux-gnu/libpci.so.3.13.0:.text@af49 |
| `pushfq` | 6 | 1 | usr/lib/ossl-modules/fips.so:.text@6b2be |
| `popfq` | 6 | 1 | usr/lib/ossl-modules/fips.so:.text@6b840 |
| `inb` | 5 | 1 | usr/lib/x86_64-linux-gnu/libpci.so.3.13.0:.text@b0c7 |
| `insl` | 5 | 1 | usr/lib/ossl-modules/fips.so:.text@8b041 |
| `outsw` | 4 | 1 | usr/lib/ossl-modules/legacy.so:.text@139ea |
| `inl` | 4 | 1 | usr/lib/x86_64-linux-gnu/libpci.so.3.13.0:.text@b0e0 |
| `outw` | 2 | 1 | usr/lib/x86_64-linux-gnu/libpci.so.3.13.0:.text@af96 |
| `inw` | 2 | 1 | usr/lib/x86_64-linux-gnu/libpci.so.3.13.0:.text@b0e8 |
| `xend` | 2 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@8f8e6 |
| `rdpkru` | 2 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@110b87 |
| `wrpkru` | 1 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@110c22 |

**int3**: 609 instructions, 1 distinct mnemonics, 1 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `int3` | 609 | 1 | usr/lib/x86_64-linux-gnu/libgcrypt.so.20.5.0:.text@6c8fc |

**BMI**: 7,501 instructions, 21 distinct mnemonics, 25 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `rorxl` | 2,645 | 1 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@22d21 |
| `mulxq` | 834 | 2 | usr/lib/ossl-modules/fips.so:.text@8abaf |
| `rorxq` | 826 | 1 | usr/lib/x86_64-linux-gnu/libgcrypt.so.20.5.0:.text@f6a3c |
| `tzcntq` | 820 | 2 | usr/lib/libgcc_s.so.1:.text@5cb8 |
| `tzcntl` | 788 | 1 | usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1:.text@58fd |
| `andnl` | 408 | 1 | usr/lib/ossl-modules/fips.so:.text@71b0a |
| `shrxq` | 300 | 1 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@7374 |
| `shlxq` | 286 | 2 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@7296 |
| `andnq` | 190 | 1 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@7379 |
| `shrxl` | 93 | 2 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@6354 |
| `blsmskl` | 56 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@1674a9 |
| `lzcntl` | 54 | 1 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@632d |
| `shlxl` | 44 | 1 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@6287 |
| `bzhil` | 42 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@162423 |
| `sarxl` | 41 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@1620f7 |
| `bzhiq` | 31 | 1 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@74f6 |
| `blsmskq` | 20 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@1675ff |
| `lzcntq` | 7 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@163002 |
| `addr32 mulxq` | 6 | 1 | usr/lib/ossl-modules/fips.so:.text@8d7b9 |
| `sarxq` | 6 | 1 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@35d7d |
| `blsrl` | 4 | 1 | usr/lib/x86_64-linux-gnu/libmvec.so.1:.text@1e526 |

**FS/GS**: 27,599 instructions, 12 distinct mnemonics, 21 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `movq` | 12,427 | 3 | usr/bin/archdetect:.text@10b5 |
| `subq` | 12,084 | 1 | usr/bin/archdetect:.text@1133 |
| `movl` | 2,582 | 3 | usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2:.text@2cfd |
| `cmpl` | 252 | 1 | usr/lib/x86_64-linux-gnu/libresolv.so.2:.text@483c |
| `xorq` | 147 | 1 | usr/lib/x86_64-linux-gnu/librt.so.1:.text@114a |
| `addq` | 37 | 2 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@29fd7 |
| `cmpq` | 29 | 3 | usr/lib/udev/ata_id:.text@17e00 |
| `movb` | 22 | 3 | usr/lib/udev/iocost:.text@129cd |
| `cmpb` | 9 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@a296f |
| `xchgl` | 7 | 1 | usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2:.text@b805 |
| `jb` | 2 | 1 | usr/lib/ossl-modules/fips.so:.text@8b042 |
| `movslq` | 1 | 1 | usr/bin/udevadm:.text@681d3 |

**small-int**: 7,108 instructions, 13 distinct mnemonics, 16 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `leave` | 2,227 | 1 | usr/lib/udev/pcmcia-check-broken-cis:.text@1565 |
| `shrdl` | 1,196 | 1 | usr/lib/udev/ata_id:.text@5695 |
| `prefetcht0` | 1,184 | 1 | usr/lib/libcrypt.so.1.1.0:.text@9c9c |
| `shldq` | 876 | 2 | usr/lib/libgcc_s.so.1:.text@ba7f |
| `shldl` | 779 | 1 | usr/lib/x86_64-linux-gnu/libuuid.so.1.3.0:.text@35e9 |
| `shrdq` | 686 | 3 | usr/lib/libgcc_s.so.1:.text@8867 |
| `prefetcht1` | 144 | 1 | usr/lib/x86_64-linux-gnu/libzstd.so.1.5.7:.text@3e6e0 |
| `cld` | 6 | 1 | usr/lib/ossl-modules/fips.so:.text@6b2c9 |
| `prefetchnta` | 2 | 1 | usr/lib/libcrypt.so.1.1.0:.text@9c31 |
| `cmpxchgw` | 2 | 1 | usr/lib/libasound.so.2.0.0:.text@9df5a |
| `cmpxchgl` | 2 | 1 | usr/lib/libasound.so.2.0.0:.text@9dfec |
| `clc` | 2 | 1 | usr/lib/x86_64-linux-gnu/libgcrypt.so.20.5.0:.text@13be88 |
| `std` | 2 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@adc18 |

**other**: 3,086 instructions, 33 distinct mnemonics, 36 distinct mnemonic+shape forms.

| mnemonic | count | shapes | first use |
|---|---|---|---|
| `leal` | 1,196 | 1 | usr/lib/x86_64-linux-gnu/libgcrypt.so.20.5.0:.text@79578 |
| `adcxq` | 736 | 2 | usr/lib/ossl-modules/fips.so:.text@8abe8 |
| `adoxq` | 630 | 2 | usr/lib/ossl-modules/fips.so:.text@8abff |
| `addr32 callq` | 181 | 1 | usr/lib/libcrypt.so.1.1.0:.text@307e |
| `vprotd` | 80 | 1 | usr/lib/ossl-modules/fips.so:.text@6f958 |
| `vprotq` | 64 | 1 | usr/lib/ossl-modules/fips.so:.text@206f21 |
| `movbeq` | 40 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@1624a0 |
| `incsspq` | 28 | 1 | usr/lib/libgcc_s.so.1:.text@22dbb |
| `movbel` | 28 | 2 | usr/lib/ossl-modules/fips.so:.text@74a99 |
| `addr32 movq` | 16 | 1 | usr/lib/ossl-modules/fips.so:.text@8ac8f |
| `rdsspq` | 13 | 1 | usr/lib/libgcc_s.so.1:.text@22730 |
| `pushq` | 11 | 1 | usr/lib/ossl-modules/legacy.so:.text@139fb |
| `xrstor` | 6 | 1 | usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2:.text@13454 |
| `addr32 shrq` | 6 | 1 | usr/lib/ossl-modules/fips.so:.text@8c677 |
| `rdrandq` | 5 | 1 | usr/lib/ossl-modules/legacy.so:.text@7f44 |
| `xabort` | 5 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@8f6df |
| `addr32 mulq` | 4 | 1 | usr/lib/ossl-modules/fips.so:.text@8c39c |
| `addr32 incq` | 4 | 1 | usr/lib/ossl-modules/fips.so:.text@8d8ed |
| `xbegin` | 4 | 1 | usr/lib/x86_64-linux-gnu/libc.so.6:.text@8f6ce |
| `rdseedq` | 3 | 1 | usr/lib/ossl-modules/legacy.so:.text@7fb4 |
| `xsave` | 3 | 1 | usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2:.text@13438 |
| `xsavec` | 3 | 1 | usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2:.text@134f8 |
| `addr32 shll` | 2 | 1 | usr/lib/ossl-modules/fips.so:.text@8b742 |
| `addr32 sbbq` | 2 | 1 | usr/lib/ossl-modules/fips.so:.text@8d93c |
| `xorl` | 2 | 1 | usr/lib/ossl-modules/fips.so:.text@1d57d6 |
| ... 8 more, see TSV | | | |

### D. Per-file class counts for the init chain (/init is a 456-byte shell script run by busybox; ld.so and libc load into every dynamic binary)

| file | total | admitted | SSE | AVX | x87 | LOCK | string | system | int3 | BMI | FS/GS | small-int | other | bad |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| usr/bin/busybox | 79,045 | 77,455 | 714 | 0 | 0 | 0 | 189 | 2 | 0 | 0 | 684 | 1 | 0 | 0 |
| usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 | 37,085 | 34,470 | 2,198 | 172 | 16 | 24 | 8 | 126 | 0 | 0 | 61 | 2 | 8 | 0 |
| usr/lib/x86_64-linux-gnu/libm.so.6 | 114,641 | 61,150 | 32,748 | 6,889 | 12,978 | 0 | 0 | 0 | 0 | 2 | 803 | 71 | 0 | 0 |
| usr/lib/x86_64-linux-gnu/libc.so.6 | 347,981 | 317,469 | 13,257 | 10,109 | 417 | 541 | 152 | 487 | 0 | 704 | 4,565 | 250 | 30 | 0 |
