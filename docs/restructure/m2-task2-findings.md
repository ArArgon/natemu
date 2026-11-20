# M2 task 2: 32-bit code compiles and passes admission offline

November 20, 2026. Task 2 of [the M2 plan](m2-32bit-design.md): the compiler and the host's admission check accept 32-bit
code. Nothing runs it yet: the resident still stops before it asks for a block of 32-bit code (see "What task 3a needs"). 64-bit output is unchanged (see "64-bit output").

## What was built

- **Compiler (`tools/fexc`).** `--profile slice1 --bitness 32` is accepted (it was refused). It runs FEX's own 32-bit
  path in its own FEX context. `--push-run` with it is refused (the option assumes 8-byte stack slots); the host does
  not send it for 32-bit requests.
- **Allowed-instruction list.** The 32-bit rows are in `shim/fexc_slice1.inc`, keyed on the request's mode
  (`fexc_bits32`). The table below gives each decision.
- **FEX fork, one commit** (`0e96493`, local until the fork is pushed): our string-instruction code stepped ESI, EDI and
  ECX with 64-bit additions and gave the destination no segment. In 32-bit code it now steps them in 32 bits, so they
  wrap at 4 GiB and their upper halves stay zero, and stores to ES:EDI as FEX's own string code does. 64-bit output is
  the same.
- **Admission (`crates/host/src/admission`).** `admit_mode(.., bits32)`: the object's mode must be the one asked for;
  the register map is the 32-bit one (`SRA32`); branch targets, block ends and pushed return addresses wrap at 4 GiB;
  loads of the ES, CS, SS and DS base fields are allowed (in 32-bit code only). `admit` is the 64-bit entry, unchanged.
- **Host compiler (`Compiler::bits32`).** A 32-bit request sends `--bitness 32`, never `--push-run`, is admitted under
  the 32-bit rules and is always compiled: its answer is not kept in memory (so never moved to another address), not
  read from or written to the persistent cache, and never offered as a template for reuse. The field follows the code
  segment's mode (`Cpu::code_mode`).
- **Offline admission tool.** `natemu-host unused admit-corpus DIR` admits every `NAME.obj` in a folder (compiled from
  `NAME.src`), with the code-shape check, and prints the time per block. The old ignored `corpus` test calls the same
  function.

## The 32-bit rows

| Form | Decision | Why |
| --- | --- | --- |
| INC/DEC r32 (40-4F) | translated | REX prefixes only in 64-bit code |
| DAA, DAS, AAA, AAS, AAD, SALC | translated | FEX translates them; its tests cover them |
| AAM (D4) | the shim's | divides by its immediate: #DE for 0, which FEX's code does not raise (and our divide hooks reject the form) |
| PUSHA, POPA | the shim's | FEX moves ESP between the eight accesses, so a fault in the middle would leave ESP changed |
| ARPL (63) | the shim's | writes a selector's RPL; 63 is MOVSXD in 64-bit code |
| LES, LDS (C4, C5 with a memory operand) | the shim's | segment loads; FEX knows the bytes only as VEX prefixes, so they are found from the bytes |
| BOUND (62 with a memory operand) | the shim's | raises #BR; FEX knows 62 only as the EVEX prefix |
| POP DS/ES/SS, PUSH of a segment register, MOV to and from Sreg, INT, INTO, far call/jump/return, IRET, PUSH/POP FS/GS, LSS/LFS/LGS, SYSENTER, SYSCALL | the shim's | already marked by opcode; verified in 32-bit mode |
| 0x67 (16-bit addressing; SI/DI/CX for strings, LOOP, JCXZ) | refused (stops the VM) | not built: the string code, the site records and admission know 32- and 64-bit addresses only |
| 66 on a near branch (16-bit EIP) | refused | not built |
| 82 (alias of group 80) | refused | FEX decodes it but cannot translate it ("Unknown ALU Op") |
| CS/DS/ES/SS prefixes | translated | FEX adds the named segment's base, read from the CPU state page; the fields are 0 |
| Direct branches | translated | the target wraps at 4 GiB, so the 64-bit "non-canonical target" refusal does not apply |

"Refused" is the development behaviour the plan chose: the VM stops and names the instruction.

Not decided here: the null FS/GS selector gate (two code-identity bits and a #GP stub), which the plan lists under
accuracy and which belongs with the code identity work.

## Register map: decision and evidence

**Decision: FEX's compact 32-bit plan, unchanged.** fexc reports the map it used in every object: EAX, ECX, EDX, EBX,
ESP, EBP, ESI, EDI in x4, x7, x5, x6, x8, x9, x10, x11, then PF and AF in x26 and x27; XMM0-7 in v16-v23. The eight Arm
registers that hold R8-R15 in 64-bit code (x12-x17, x19, x29) and v24-v31 are scratch registers in 32-bit code. The
host's second map is `natemu_abi::execution::SRA32`; admission pins it for 32-bit objects.

Evidence that the rest of the chain takes the second map without change:

- Admission, with the code-shape check, admitted all 719 blocks of FEX's 32-bit suite and the unit test's forms. Its
  rules name x0-x3, x25, x28 and "not a scratch register", never a guest register's number.
- Our precision and site checkers read the map from the compiler's report. On 32-bit code: 2,545 compiles, 5,267 access
  sites, no violation. Under `--bits32` the checker leaves out its comparison of each site with a model of the x86
  instruction's accesses (P7b: the model knows 64-bit instructions), so 32-bit site records are checked against the
  emitted code only (every access is a site of the right width, base and offset) until task 3 runs them against a
  reference.

**Does FEX's 32-bit code assume the upper halves of the general registers are zero? Yes.** It uses the whole 64-bit Arm
register as an address (`ldr w20, [x9, #8]` for `[ebp+8]`, `str w9, [x8, #-4]!` for a push, `mov x0, x4` for
`call eax`). Its own 32-bit writes keep the upper half zero. So the shim must enter 32-bit code with the eight
registers zero-extended.

**What real hardware does** (AMD Ryzen 7 8845HS, Linux 7.0, a user program that far-jumps to the 32-bit code segment
0x23 and back to 0x33; method and output in this page's appendix):

- A register the 32-bit code does not write keeps its upper half, R8-R15 included.
- A 32-bit write (`mov eax, eax`, `inc ecx`, `pop edi`) zeroes the upper half; 8- and 16-bit writes keep it.
- RSP comes back zero-extended even when the 32-bit code never touched it (a control that far-jumps within 64-bit
  code keeps RSP's upper half).
- 32-bit stack instructions work with a non-zero upper half of RSP: only ESP is used.

So hardware keeps the upper half of an untouched register, and FEX needs it zero while 32-bit code runs. The shim
cannot tell "untouched" from "written with the same low half" when it saves the state. See the decision under "What
task 3a needs". (The measurement is of an AMD processor; natemu presents an Intel one, whose manual calls the bits
undefined.)

## 64-bit output

- **The replay check**, now a framework case (`static/fexc/fexc-replay`): it boots the Debian kernel and installer
  initrd once at 1 vCPU with the persistent cache off, records every request the host sends to the compiler, and has
  the compiler of the checkout's merge base with `main` and the checkout's own compiler answer them. Result here:
  about 54,000 requests (53,907 and 53,650 in two runs), 0 differences (33 s). The base compiler is built by the framework (`testkit.artifacts.fexc_at`).
- The 64 fixture programs under three sets of options (192 compiles): identical reports and code.
- The whole `static` and `unit` families pass (they hold the pinned 64-bit bodies and shapes).

## FEX's 32-bit tests

The framework now extracts `unittests/32Bit_ASM` beside `unittests/ASM`. `run_asm.py --compile32` (case
`static/fexc/fexc-asm32`) builds each of the 245 tests as FEX does, finds the reachable code from the entry, compiles
every block for 32-bit code and has the host admit every object. Result: 245 pass; 719 blocks, 3,365 instructions
translated; 719 admitted. Blocks end at instructions the shim runs: segment register moves 528, the tests' final `hlt`
245, x87 control and state instructions 160, gathers 32, ARPL 19, and a few each of segment pushes and pops, PUSHA/POPA,
AAM, far transfers. Refused forms met: 0x67 addressing in 9 places and one `REPNE MOVS`, which 64-bit code refuses too.

Running the tests (and comparing their results) waits for 32-bit code in the VM, task 3.

## What task 3a needs

- **Mode wiring.** The compiler's mode follows `Cpu::code_mode` (set beside the other per-request flags in
  `crates/host/src/execution.rs`), but no request comes from 32-bit code while the resident stops
  `unsupported_code_mode` there. Task 3a lifts those stops, and has the instructions the shim runs decoded in 32-bit
  mode (task 1's decoder takes the mode).
- **State load and save around 32-bit code** (`crates/runtime/src/execution.rs`: the entry frame built from `SRA`, the
  save reading it back; `execution/sse.rs` for XMM): load EAX..EDI zero-extended into x4..x11 and XMM0-7 into v16-v23
  only; leave x12-x17, x19, x29 and v24-v31 out of both directions, so R8-R15 and XMM8-15 keep their values in the CPU
  state (`entry_frame_rest_zero` then has to allow or zero them). A test that they survive 64 -> 32 -> 64.
- **The upper halves of RAX..RDI after 32-bit code. Decided (the orchestrating session's decision, reversible; the
  owner is informed): the shim saves them as zero.** R8-R15 and XMM8-15 are preserved exactly. The alternatives were:
  keep the old upper half when the low half is unchanged (matches the measured hardware for an untouched register, but
  is silently wrong when 32-bit code writes the value a register already had, such as `xor eax, eax` on a zero low
  half, and would have to exempt RSP, which the measurement shows zero-extended even when untouched); or exact
  behaviour, which needs translated code to track which registers it wrote. Zero differs from the measured hardware and
  from QEMU only for a register the 32-bit code never wrote. The Intel manual calls these bits undefined after a switch
  to 32-bit code, the measurement is of an AMD processor while natemu presents an Intel one, and no real code can rely
  on them (any `push ebx` / `pop ebx` pair zeroes RBX's upper half on hardware). The round-trip test's locked QEMU
  answers must be written for this choice (no expectation on the upper half of an untouched legacy register).
- **Addresses that wrap at 4 GiB.** FEX adds a small displacement with 64-bit addressing (`[ebp-8]` with EBP = 4
  reaches 0xFFFF_FFFF_FFFF_FFFC, not 0xFFFF_FFFC), and PUSH and POP move ESP with a 64-bit update (POP at
  ESP = 0xFFFF_FFFC leaves 0x1_0000_0000). The plan already gives task 3a "the 32-bit wrap of base plus offset and a
  32-bit fault address". Such an access lands outside the low 4 GiB, where a 32-bit process has nothing mapped, so it
  faults and the shim can redo it at the wrapped address from the site record. In a process that also has memory above
  4 GiB (checkpoint C) the access could silently succeed at the wrong address: decide there whether these forms need
  32-bit address arithmetic in translated code (one more instruction per PUSH/POP).
- **Fetching near 4 GiB.** The host fetches a window of up to two pages for a block. The compiler refuses a window
  that crosses 4 GiB in 32-bit code (`CheckWindow` in `fexc.cpp`), so the host must end the window at 4 GiB there (and
  let the instruction that runs off the end wrap or fault as x86 does), or a block near the top fails to compile.
- **A displacement to a wrapped target.** Admission's check of a guest address built in the code (`displacement_ok` in
  `admission/structure.rs`) takes a 64-bit difference, so an address that is the instruction's end plus a displacement
  and wraps past 4 GiB is refused. It fails safe (a loud refusal), and 32-bit code has no RIP-relative operands, so only
  a pushed return address or an exit target meets it, which other rules prove; revisit if a refusal shows up.
- **Replay with both modes.** The replay case's stream is 64-bit only. Once 32-bit code runs, record a stream that
  mixes both, or add a check that a 64-bit request answered after a 32-bit one by the same compiler process is
  byte-identical to the same request answered first (the two modes are separate FEX contexts in one process).
- **The inline CPL-0 forms** (CLI/STI, SWAPGS, MOV from CR2, VERW, RDTSC) are 64-bit code's: the compiler refuses
  `--cpl0` and `--tsc` with `--bitness 32` and the host does not send them, so in 32-bit CPL-0 code they are the
  shim's (SWAPGS is #UD outside 64-bit mode).
- **Instructions the shim must run in 32-bit code**, beyond the 64-bit set: ARPL, LES/LDS, BOUND, PUSHA/POPA, AAM, INTO,
  POP DS/ES/SS and PUSH of every segment register; IRET, far transfers and software interrupts with 32-bit sizes.
- **Segment bases.** The four fields (`frame::LEGACY_BASES`) are never written, so they are 0. When non-flat segments
  run (task 5 or 6), the shim writes them as it writes the FS and GS bases.

## Leads, not built

- The indirect-branch target check (two instructions before every `ret`, indirect call and jump) cannot fire in 32-bit
  code: a 32-bit target is always canonical. Dropping it needs the compiler and admission's shape check to agree.
- While only flat segments run, the DS and ES base loads of string instructions add two instructions per element for
  a value that is always 0.

## Effort

Approximate, from the session that did the work (it has no clock of its own), in agent hours of active work: reading the plan and the code about 2; compiler rows, FEX fix and first
compiles about 1.5; admission and the host compiler about 1.5; FEX's 32-bit suite offline and the admission tool about
1; checkers on 32-bit code about 0.5; the replay case and the base-compiler build about 1; the hardware measurement,
tests through the framework and this page about 1.5. About 9 in all, against the plan's 18-34. The register plan
needed no work in the compiler or the checkers (FEX and our tools already carry the map); its cost moves to the shim's
state save in task 3a.

## Appendix: the upper-half measurement

A static, non-PIE 64-bit program on the x86-64 reference machine (`gcc -O1 -static -no-pie`, the far jumps in
assembly). It sets RAX..R15 to values with a distinctive upper half (`0x1111111100000001` and so on), points RSP at a
`MAP_32BIT` stack and ORs `0x55555555` into its upper half, then `ljmp *m16:32` to selector 0x23 and one of three 32-bit
bodies, each ending `ljmp $0x33, $back`, where 64-bit code stores all sixteen registers:

- nothing (`nop`);
- `mov eax, eax; inc ecx; xor edx, 0; lea ebx, [ebx]; movzx ebp, bp; add si, 0; mov dl, dl`;
- `push eax; pop edi; mov esi, esp`.

The control jumps through the same far pointer form to selector 0x33 instead. Task 3a's round-trip test is the place
for this sequence as a framework case; it cannot run in natemu before 32-bit code does.

```
nop only:                  rax 1111111100000001 ... rdi 8888888800000008   rsp 0000000041700000   r8-r15 unchanged
32-bit writes:             rax 0000000000000001 (mov eax,eax)   rcx 0000000000000003 (inc ecx)
                           rdx 0000000000000003 (xor edx,0; mov dl,dl)   rbx 0000000000000004 (lea ebx,[ebx])
                           rbp 0000000000000006 (movzx ebp,bp)   rsi 7777777700000007 (add si,0: kept)
push eax; pop edi:         rdi 0000000000000001   rsi 0000000041700000 (mov esi,esp)   rax 1111111100000001
control, 64-bit far jump:  rsp 5555555541a72000 (upper half kept)
```

RSP entered the 32-bit code as 0x55555555_417xxxxx in the first three rows.
