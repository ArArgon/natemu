# General QEMU reference tool

Runs any straight-line x86-64 program under the pinned QEMU Linux-user (the image and QEMU hashes
first pinned by the I4-V QEMU check, now `ref.IMAGE`/`ref.PINS`; container flags copied from that check; no new image) and returns final GPRs,
RFLAGS, RIP and memory. Nothing is keyed by program name. Independent of the compiler and the VM.

    tools/with-env python3 tools/reference/build_slice1.py                                   # build fixtures/slice1/{programs,expected}.json
    tools/with-env python3 tools/reference/gen_slice1.py -o OUT.json                         # (what the build runs) inputs, seeded, host clang
    tools/with-env python3 tools/reference/ref.py PROGRAMS.json -o RESULTS.json              # (what the build runs) QEMU results
    python3 -m unittest discover -s tools/reference                                        # self-check (needs Docker)

Defaults: QEMU at `$NATEMU_RAW_ROOT/reference-slice1-20261026-01/pin/qemu-x86_64-static` (copy of the pinned
binary, SHA-256 checked against `ref.PINS`), run files in `.../run/` (ELFs, QEMU stdout/stderr/log
per program). Override with `--qemu`, `--workdir`. Requires host `clang` (assembles the harness stubs; the
generator also assembles programs) and Docker.

For `ref.run_programs` callers, `NATEMU_REFERENCE_BACKEND=apple` uses Apple container with the separately pinned
Debian ARM64 image in `ref.APPLE_IMAGE`. It runs the same SHA-256-checked QEMU executable, with networking disabled,
a read-only root, and two CPUs/512 MiB. A shell/coreutils launcher supplies the same per-job arguments and 60-second
bound; the output parser is shared. Provenance records the actual backend, image and Linux kernel, rather than claiming
the original Docker environment. Corpus-build scripts that invoke Docker directly still need Docker.

## Program (input) and result (output) JSON

    program: {"name": label only, "code": hex, "rip": addr, "gpr": [16], "rflags": value,
              "memory": [{"addr": a, "data": hex}...], "insns": [{op, width, count?, cc?, src_nonzero?}...] (required)}
    result : {"name", "qemu": {"gpr": [16 hex], "rflags": hex, "rip": hex, "memory": [{"addr","data"}...]},
              "disasm": [QEMU's own disassembly of the executed bytes],
              "undefined_flags": hex|null, "rflags_compare_mask": hex}
    file   : {"format": "natemu-reference-v2", "image", "qemu_sha256", "qemu_package", "qemu_argv", "results": [...]}

Prefix mode: a program with `"prefix": k` runs only bytes [0, k) of its code (k must be an instruction boundary; 0 is
allowed) and its result, which carries `prefix`, is the state at that boundary: the reference for an exception raised by
the instruction at k (nothing of it or after it ran). `insns` annotates the whole program; it is cut to the instructions
QEMU executed before the disassembly cross-check and the flag analysis, so the compare mask is the one at the boundary.
A k inside an instruction is refused (QEMU's disassembly then covers bytes beyond k). Used by `tools/run_slice1_faults.py`.

GPR order rax rcx rdx rbx rsp rbp rsi rdi r8..r15. Integers are JSON numbers or `0x` strings; output is
`0x%016x`. Result memory regions are in input order, same addresses and lengths; all are captured.
`ref.compare(result, gpr, rflags, rip, memory)` returns the differences against another implementation's final
state: 16 GPRs, final RIP, RFLAGS only under `rflags_compare_mask`, and memory, which must list exactly the
program's regions (same count, addresses and order; a missing or extra region is a difference).

Memory capture: only the registered regions are returned. A store to a mapped page outside them (e.g. the code or
harness page) is not observed, and a store to an unmapped address kills the process (tool error). Register regions
that cover everything the program may write; whole pages are the natural unit.

Execution: the ELF entry is a setup stub (harness pages at 0x40000..0x44000) that loads RFLAGS and all 16 GPRs
and jumps to `rip`; the code runs and falls through into the capture sequence appended at `rip+len(code)`
(MOV-saves all GPRs, PUSHFQ, records the capture label as final RIP, writes state and regions to stdout).
The tool checks final RIP = `rip+len(code)`, and QEMU's `-d in_asm` log must show exactly the supplied bytes.

## I5a (SSE) programs

`gen_i5a.py` programs carry `sse: true` (the VM runner sets CR4.OSFXSR) and `cpu: qemu64,+ssse3,+sse4_1,+sse4_2` (qemu64 has
SSE..SSE3 only). They load XMM0..15 from a pattern block at the data region's start, set MXCSR with LDMXCSR (every exception
masked), and end by storing XMM0..15 and STMXCSR into the data region, which `compare` covers like any memory. SSE lines are
annotated `{op, sse: true}`: the disassembly check compares the mnemonic's first three letters (QEMU's AT&T names add suffixes),
and `flags.step` lets COMISS/UCOMISS/COMISD/UCOMISD and PTEST define all six flags, every other SSE form none. The pinned QEMU
(7.2) differs from the SDM in places the corpus meets: two NaN operands follow its x87 rule (the larger significand) instead
of the SSE rule (SDM Vol.1 Table 4-7: the first source), MIN/MAX under DAZ return the original denormal (the SDM's DAZ converts
it to a signed zero before the operation; the generator keeps MIN/MAX out of DAZ groups), and DE is never set.

## I8 (AVX) programs

`gen_i8.py` programs carry `sse: true` and `avx: true` (the VM runner sets CR4.OSFXSR, CR4.OSXSAVE and XCR0 = 7) and `cpu:
qemu64,+ssse3,+sse4_1,+sse4_2,+popcnt,+xsave,+avx,+avx2,+fma,+f16c,+bmi1,+bmi2`. They load YMM0..15 from the pattern block,
set MXCSR with LDMXCSR, run the VEX forms at VEX.128 and VEX.256, and store YMM0..15 and STMXCSR (DE masked out) into the data
region. Annotations are `{op, sse: true}` (`flags.step` lets VCOMISS & co., VPTEST, VTESTPS and VTESTPD define the six
flags). QEMU 7.2 departures the generator avoids: MIN/MAX under DAZ (as I5a), VCVTPS2PH under FTZ and VCVTPH2PS under DAZ
(QEMU flushes; the SDM ignores both), and VBROADCASTI128 (its disassembler prints `.byte`, which the annotation check cannot
follow).

## RFLAGS

- Settable and meaningful under Linux-user: CF PF AF ZF SF OF (mask 0x8d5). Initial RFLAGS may set only these.
- Fixed: bit 1 = 1 and IF = 1 (user-mode POPF cannot clear IF); asserted as `rflags & ~0x8d5 == 0x202`, not compared.
- DF is compared since I1 (`flags.TRACKED`; initial DF is 0, cld/std change it). TF/NT/RF/VM/AC/ID/IOPL are not represented.
- Undefined bits are masked by rule, never per program: each instruction annotation `{op, width, count?, cc?}`
  goes through the table in `flags.py` (Intel SDM "Flags Affected"):
  - and/or/xor/test: AF. mul/imul: SF ZF AF PF. div/idiv: all six.
  - shl/shr/sar: masked count (&31, &63 for 64-bit) 0: none; 1: AF; above 1: AF and OF. SHL/SHR also leave CF
    undefined when the masked count is >= the operand width (8/16-bit operands only; SAR's CF is the sign bit, defined).
  - rol/ror: count 0 none; count other than 1: OF.
  - bt/bts/btr/btc: CF defined (selected bit), ZF unaffected, OF SF AF PF undefined.
  - bsf/bsr: ZF defined, CF OF SF AF PF undefined. The destination is also undefined for a zero source, so the
    annotation must carry `src_nonzero: true` (the generator guarantees it by construction: it moves a nonzero immediate
    into the source immediately before); without it the analysis rejects the program.
  - none: add/adc/sub/sbb/cmp/neg (all six defined), inc/dec (CF untouched), mov/movzx/movsx/lea/push/pop/not/setcc/
    cmovcc/xchg/bswap/cbw/cwde/cdqe/cwd/cdq/cqo.
  - I1 (`gen_i1.py`): clc/stc/cmc CF; sahf SF ZF AF PF CF; lahf and cmc read flags, pushf reads all six; shld/shrd as
    shifts (32/64 only); cmpxchg/xadd/popcnt all defined; tzcnt/lzcnt OF SF PF AF undefined; andn/bextr/bzhi/blsi/
    blsmsk/blsr AF PF (and SF where the SDM leaves it) undefined; mulx/rorx/sarx/shlx/shrx/prefetch/movnti/enter/leave
    none. blsi/blsmsk/blsr CF is not compared: the pinned QEMU 7.2 computes it from a stale cc_src (BLSI with a zero
    source gives CF = 1; fixed upstream in QEMU 8.0, "target/i386: Fix C flag for BLSI, BLSMSK, BLSR").
  - I2 (`gen_i2.py`): movs/stos/lods none; cmps/scas all six defined, untouched by a REP form whose annotation `count`
    (initial RCX) is 0. A string annotation also names its repeat prefix (`rep`: rep/repe/repne, absent for none), checked
    against QEMU's disassembly (`rep`, `repz`, `repnz`) instead of the operand width (the generator writes the width into
    the mnemonic). QEMU ends a block at a REP string instruction and translates it again as the next block's start, so
    the straight-line cross-check takes each translated address once (`disasm`).
  - I3 (`gen_i3.py`): a LOCK prefix changes no flag rule (each locked form has its unlocked form's flags); an annotation
    `lock: true` must match a `lock` prefix in QEMU's disassembly (and its absence the absence of one); cmpxchg8b/16b define
    ZF only (the rest unaffected) and carry no width check (the mnemonic names it). The pinned QEMU (7.2) computes
    cmpxchg8b/16b's other flags from a stale lazy-flag state when an earlier instruction of the same translation block set
    them (`mov eax, 0xff; mul al; cmpxchg8b m; sbb m, r8d`: QEMU subtracts no borrow; Rosetta 2, the SDM and natemu keep
    MUL's CF = 1), and crashes on `mul al; cmpxchg8b m; setb`; the I3 generator puts `pushfq` (which makes QEMU store that
    state) right before every cmpxchg8b/16b and pops the slot after.
  State is carried forward: a later instruction that writes a flag defines it again. An instruction reading an undefined
  flag (adc/sbb/setcc/cmovcc) is rejected, because the undefined value would flow into registers; the generator inserts a
  `cmp` first. `rflags_compare_mask = 0xcd5 & ~undefined_flags` (0x8d5 before I1: DF was not compared).
- `insns` is required for every program. It is cross-checked against QEMU's own disassembly of the executed bytes:
  instruction count, mnemonic, operand width (destination register, else size suffix), immediate shift counts, and for
  CL shifts the preceding `mov $N, %cl`.
- Shifts/rotates by CL: annotate the count actually in CL (the generator emits `mov cl, imm` immediately before).

## Control-flow programs (`flow`, S2d)

A program with `"flow": true` may contain branches, loops, calls and returns; it must still end by reaching the capture
sequence at `rip+len(code)` (a jump or fall-through to the end of the code). `"offsets"` lists the start offset of every
annotated instruction (parallel to `insns`; the annotations describe the static instructions, once each). QEMU also logs the
translation blocks it executes (`-d in_asm,exec,nochain`); the tool rebuilds the executed instruction sequence from them,
cross-checks every executed instruction against its annotation and the supplied bytes, and runs the flags rule along that
path, so `rflags_compare_mask` and `undefined_flags` are those of what ran (the result adds `executed_insns`). Control flow has
annotations `jcc` (with `cc`), `jmp`, `call`, `ret`, `loop`, `loope`, `loopne`, `jrcxz`; a conditional branch or `loope/loopne`
reading an undefined flag is rejected like `adc`. The generator is `gen_slice2.py` (structured, seeded; see its header);
`tools/run_slice2_vm.py` runs the corpus in the VM.

**Fault mode** (`"faults": {"protect": [{"addr": page, "mode": "unmap"|"readonly"}, ...]}`, flow programs, at most two
pages, each inside a memory region; `{}` or an empty list protects nothing): QEMU runs `-singlestep` with a SIGSEGV
handler on an alternate stack. The setup stub saves and `munmap`s (or `mprotect`s read-only) the pages before the program
runs; the handler stores the context the signal delivers (all GPRs, RIP, EFLAGS, the sigcontext error code, trap number and CR2,
and `si_addr`), restores the pages (an unmapped page comes back with its saved bytes, so memory can be written out) and
writes the state and every region out like the capture sequence. The result's `qemu` is that state, `fault` the delivered
error code and addresses, `executed_insns` the instructions completed before the fault and `path_ix` their static indices.
A program whose page is never touched (or never written) runs to the end and has no `fault`. Known QEMU limits, handled by
`tools/run_slice2_faults.py`: the sigcontext trap number is -1 (every fault here is a page fault), its error code is the
access QEMU made (a read-modify-write's load is a read; a never-mapped address reports P=1), and the flags a `cmp` left lazily
computed can be stale at a faulting `setcc` store (AF/CF) even single-stepped; the runner takes the exact state from the
unrolled executed path instead (`gen_slice2.unrolled`).

## Limits

- QEMU's disassembly (`in_asm`) goes wrong inside translation blocks longer than 1 KiB; the annotation cross-check catches it
  (the run fails loudly, it is never silently wrong).
- A flow program that leaves its code region (other than into the capture sequence or the fault handler) is refused.
- Straight-line programs (or `flow` programs, above), 64-bit, unprivileged code only. A straight-line program must fall
  through to the capture sequence (rejected by the RIP check otherwise).
- Linux-user cannot represent: privileged state (CRs, MSRs, segments/descriptors, IDT), guest paging/page-table
  faults, exceptions as architectural events (a fault kills the process: nonzero exit is an error), IF/IOPL/TF changes,
  other CPUs/ordering. Use the existing independent models for those.
- Address window for program bytes [0x10000, 0x3_0000_0000); tested to ~0x2_0000_1000; harness pages
  0x40000..0x44000 are reserved. CPU model `-cpu qemu64` (no AVX/BMI etc.), or a program's `cpu` (qemu64 plus `+feature`
  flags, e.g. the I1 corpus's `qemu64,+bmi1,+bmi2,+abm,+popcnt`, recorded in its result). Code bytes must not overlap memory regions
  (same page is fine). Max 100 regions.
- Segment bases (I7): a program's optional `fsbase`/`gsbase` (canonical, default 0) are loaded by WRFSBASE/WRGSBASE in a
  prelude at 0x40f00 (the ELF entry then; `+fsgsbase` is added to the CPU model), because Linux-user's `arch_prctl`
  refuses bases at or above its task size. The VM runners pass them as the driver's `fsbase`/`gsbase` items.
- QEMU is a software model of x86, not hardware: behavior of undefined bits and a few corners (e.g. 8/16-bit
  shift CF beyond width) is whatever QEMU 7.2 does; those bits are masked, not trusted.

## First-slice corpus (`gen_slice1.py`, `fixtures/slice1/`)

`programs.json` holds inputs only (bytes, state, annotations, source `asm`); `expected.json` holds the QEMU results.
Layout: code 0x100000; data 0x200000 and stack 0x300000, 8 KiB each (two pages, so accesses cross a 4 KiB boundary);
rbx rbp rsi rdi r12 r13 point into the data region, r8 is a small index, rsp is near STACK+0x1000. Random programs mix
mov/movzx/movsx/lea, ALU, shifts/rotates (immediate and CL), imul/mul, setcc/cmovcc, push/pop, xchg, bswap, bt-family
(register forms, memory forms with immediate bit offset only), bsf/bsr, div/idiv, cbw..cqo, movabs, with memory
operands based on rsp/rbp/r12/r13 (special encodings), index forms, absolute and RIP-relative addressing (displacement
resolved by a second assembly pass). Non-faulting division: divisor is a freshly moved nonzero immediate (and not -1
for idiv), the dividend high half is zeroed (div) or sign-extended with cbw/cwd/cdq/cqo (idiv), so the quotient fits.
bsf/bsr: the source is a freshly moved nonzero immediate, so no zero-source behaviour is exercised.

### Building the fixtures (not committed)

`fixtures/slice1/programs.json` and `expected.json` are derived and git-ignored; run `build_slice1.py` (also called by
`test_ref.py`) to create them at those same paths. It regenerates a file when it or `build.json` is missing, or its
inputs changed (seed, `gen_slice1.py`/`ref.py`/`flags.py`/`container_entry.py` contents, programs, QEMU pins);
otherwise it does nothing. Without Docker or the pinned QEMU it still builds `programs.json` and exits nonzero for `expected.json`
(`--programs-only` skips that step; the test skips with the reason).

- `fixtures/slice1/manifest.json` (committed) is the input pin: generator script + seed, and the QEMU identity
  (`format`, image id, QEMU sha256, package, argv). Gate: `ref.py`'s image/QEMU pins and `expected.json`'s header must
  equal it, else the build fails ("PIN MISMATCH"). Change it only when deliberately moving the pinned QEMU/image or seed.
- Outputs are not hash-gated: host clang is not pinned, and correctness rests on QEMU plus `ref.py`'s per-run checks.
- `fixtures/slice1/build.json` (git-ignored) records what was actually built: inputs, host clang version and sha256 of both
  outputs. Cite it when reporting a verification run's corpus.
