# General QEMU reference tool

Runs any straight-line x86-64 program under the pinned QEMU Linux-user (same image and QEMU hash as
`tools/run_i4v_qemu.py`; container flags copied from it; no new image) and returns final GPRs,
RFLAGS, RIP and memory. Nothing is keyed by program name. Independent of the compiler and the VM.

    tools/with-env python3 tools/reference/build_slice1.py                                   # build fixtures/slice1/{programs,expected}.json
    tools/with-env python3 tools/reference/gen_slice1.py -o OUT.json                         # (what the build runs) inputs, seeded, host clang
    tools/with-env python3 tools/reference/ref.py PROGRAMS.json -o RESULTS.json              # (what the build runs) QEMU results
    python3 -m unittest discover -s tools/reference                                        # self-check (needs Docker)

Defaults: QEMU at `$NATEMU_RAW_ROOT/reference-slice1-20261026-01/pin/qemu-x86_64-static` (copy of the pinned
binary, SHA-256 checked against `run_i4v_qemu.PINS`), run files in `.../run/` (ELFs, QEMU stdout/stderr/log
per program). Override with `--qemu`, `--workdir`. Requires host `clang` (assembles the harness stubs; the
generator also assembles programs) and Docker.

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

## RFLAGS

- Settable and meaningful under Linux-user: CF PF AF ZF SF OF (mask 0x8d5). Initial RFLAGS may set only these.
- Fixed: bit 1 = 1 and IF = 1 (user-mode POPF cannot clear IF); asserted as `rflags & ~0x8d5 == 0x202`, not compared.
- DF is outside the tracked set (initial DF unsupported). TF/NT/RF/VM/AC/ID/IOPL are not represented.
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
  State is carried forward: a later instruction that writes a flag defines it again. An instruction reading an undefined
  flag (adc/sbb/setcc/cmovcc) is rejected, because the undefined value would flow into registers; the generator inserts a
  `cmp` first. `rflags_compare_mask = 0x8d5 & ~undefined_flags`.
- `insns` is required for every program. It is cross-checked against QEMU's own disassembly of the executed bytes:
  instruction count, mnemonic, operand width (destination register, else size suffix), immediate shift counts, and for
  CL shifts the preceding `mov $N, %cl`.
- Shifts/rotates by CL: annotate the count actually in CL (the generator emits `mov cl, imm` immediately before).

## Limits

- Straight-line, 64-bit, unprivileged code only; control flow out of the code (branches, calls) is not handled and
  would not reach the capture sequence (rejected by the RIP check). The program must fall through.
- Linux-user cannot represent: privileged state (CRs, MSRs, segments/descriptors, IDT), guest paging/page-table
  faults, exceptions as architectural events (a fault kills the process: nonzero exit is an error), IF/IOPL/TF changes,
  other CPUs/ordering. Use the existing independent models for those.
- Address window for program bytes [0x10000, 0x3_0000_0000); tested to ~0x2_0000_1000; harness pages
  0x40000..0x44000 are reserved. CPU model `-cpu qemu64` (no AVX/BMI etc.). Code bytes must not overlap memory regions
  (same page is fine). Max 100 regions.
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
