# Box64 and TCG bindings for the bounded captive-body probe

Author: `/root/direction_review`. This is execution preparation, not an executed
result or independent approval of the eventual probe. The author also wrote the
TCG exporter; another reviewer must inspect the integrated extraction and runner.
It consumes the current execution campaign's preparation allowance.

## Exact artifact scope

Sources are the seven `outputs/<fixture>.native` files in each sealed run:

- `/private/tmp/natemu-track03-comparison-raw/box64-export-02`
- `/private/tmp/natemu-track03-comparison-raw/tcg-export-02`

All fourteen native-file hashes were checked against their run's `SHA256SUMS`.
Each selection below is an unchanged, contiguous byte range, with offsets measured
from byte zero of that native file. The retained decoded guest bytes, native
logs and TCG IR/state offsets were cross-checked against disassembly of the sealed
bytes using installed Apple LLVM. Temporary Mach-O wrappers only supplied object
headers to the disassembler; no guest/native body executed. Inspection files are
under `/var/folders/7d/bdcp0n7x4tsdrt9d_5qyz09r0000gn/T/natemu-binding-disasm-gbwcnfwb`.
The producing sources/logs and native bytes remain in the sealed runs.

| Fixture | Box64 half-open range | TCG half-open range |
| --- | --- | --- |
| `bare_load` | `[0x00,0x04)` | `[0x10,0x20)` |
| `bare_store` | `[0x00,0x0c)` | `[0x10,0x20)` |
| `bare_alias_load` | `[0x00,0x04)` | `[0x10,0x20)` |
| `live_load` | `[0x00,0x40)` | `[0x0c,0x80)` |
| `live_store` | `[0x00,0x44)` | `[0x0c,0x80)` |
| `live_alias_load` | `[0x00,0x40)` | `[0x0c,0x80)` |
| `nop` | `[0x00,0x00)` | `[0x10,0x10)` |

There is no emitted guest-work instruction for NOP; its empty extraction is a
wrapper-only control, not a fabricated native NOP. Every nonempty selected range
contains no branch, PC-relative address, literal load or helper call. Append the
declared return wrapper after the range; do not execute bytes past the range.
Do not mutate instructions or remove internal barriers to obtain a faster body.

## Common inputs and expected guest results

Use a separate aligned state allocation and accessible guest-RAM allocation.
Guest address values in these fixtures are native mapped addresses; the experiment
does not include address translation, guest permission checks or shadow misses.
Keep the 8-byte load/store location and 16-byte vector destination disjoint from
each other and from candidate state. Use matching addresses/alignment and input
values across candidates. Keep the state and native-RAM context alive through
return and any controlled native fault.

Let `C`/`D` be initial RCX/RDX, `A` initial RAX, `M` the 8-byte memory word,
and `V0`/`V1` the two 16-byte vector inputs. For alias-load fixtures, `A` is
the memory pointer. For other fixtures, RBX is that pointer; for every live fixture,
RDI is the vector-output pointer. Define `S = (C + D) mod 2^64`,
`D' = (D & ~255) | (S == 0)`, and `V = V0 XOR V1`.

- Bare load/alias: RAX becomes `M`; remaining guest registers, flags and memory
  remain unchanged. Bare store writes initial `A` to the memory word.
- Every live fixture: RCX becomes `S`, RDX becomes `D'`, XMM0 and the vector
  destination become `V`; XMM1 remains `V1`. Load variants set RAX to `M`;
  store writes initial `A` and leaves RAX unchanged.
- Live arithmetic flags are the flags of the 64-bit `C+D`, unaffected by MOV,
  SETZ or the vector instructions. Validate deferred fields as described below,
  not just SETZ. Cover zero, carry, signed overflow and nonzero sums in correctness
  inputs; do not assume the final host NZCV is a materialized x86 flags register.
- RIP advancement is excluded from both bodies. Do not count it as checked guest
  behavior or silently add one candidate's RIP-update cost to only its timing.

The wrapper must snapshot live outputs before restoring native registers. Reinitialize
inputs between independent cases, especially alias-load pointers and the vector
destination. A repeated-body timing loop changes RCX/RDX/vector state unless the
wrapper resets them; report that loop policy and its setup cost explicitly.

For the independent arithmetic oracle, derive CF from `S < C`, ZF from `S == 0`,
SF from bit63 of `S`, OF from bit63 of `~(C XOR D) AND (C XOR S)`, AF from bit4
of `C XOR D XOR S`, and PF from even parity of the low byte of `S`. These are
x86 flag bits0,6,7,11,4,2 respectively; compare that mask (`0x8d5`) separately
from preserved non-arithmetic flags. Neither candidate's live body needs incoming
native NZCV. Bare and empty bodies preserve it. None of the selected bytes reads
or writes x30, so a wrapper may use a saved-LR x30 as its loop count with non-S
SUB and CBNZ; those loop instructions must not overwrite final native flags.

## Box64 state, register and flag binding

The retained `src/dynarec/arm64/arm64_mapping.h` maps `xEmu=x0`, guest
RAX/RCX/RDX/RBX/RDI to `x10/x11/x12/x13/x17`. Set those native registers directly.
Do not assume Box64 loads them from its state struct in the extracted body.
State storage must extend through byte `0x467` (an aligned `0x500`-byte allocation
is sufficient for these accesses). All offsets below are from native `x0`.

| Offset | Width | Input/output meaning |
| --- | --- | --- |
| `0x90` | 16 | Input XMM0; live body writes XOR result back before dispatch |
| `0xa0` | 16 | Input XMM1; unchanged |
| `0x44c` | 4 | Live output deferred operation `7`, `d_add64` |
| `0x450` | 8 | Live output original RCX (`C`) |
| `0x458` | 8 | Live output original RDX (`D`, before SETZ) |
| `0x460` | 8 | Live output addition result (`S`) |

`src/include/regs.h` identifies `d_add64`; `src/emu/x64emu.c`'s
`case d_add64` recovers arithmetic flags from `op1`, `op2`, `res`. Native
`x26` is the old `xFlags` register, not the newly materialized arithmetic result.
Initialize it if checking preserved state; the selected body leaves it untouched.
The retained deferred fields must equal `(7,C,D,S)`. ADD's native NZCV is also
still available at return: `N=S[63]`, `Z=(S==0)`, `C=unsigned carry`, and
`V=signed overflow`; the wrapper must capture it before a flag-setting instruction.
AF/PF require the recorded arithmetic operands/result, not NZCV alone.

Clobbers beyond declared guest outputs:

| Body | Native registers written |
| --- | --- |
| Bare load/alias | `x10` |
| Bare store | No native register/flags writes |
| Live load/alias | `x3,x10,x11,x12,q0,q1,NZCV` |
| Live store | `x3,x11,x12,q0,q1,NZCV` |
| Empty NOP | None |

`x0`, `x13`, `x17`, SP and LR remain untouched by selected bytes. Wrapper calls
must preserve their own ABI independently; Box64's internal register assignment
is not the platform C ABI. Vector inputs are loaded from state by live bodies,
so initial native q0/q1 contents do not supply the guest inputs.

The live extraction includes the final SSE cache writeback (`str q0,[x0,#0x90]`).
It excludes the following literal-based dispatch, RIP update, table64 and padding.
Bare loads contain only LDR. Bare store retains `dmb ishld; str; dmb ishst`.
Live load/alias retain ISHLD/ISHST surrounding the vector-store path; live store
retains its three emitted barriers. This is the actual software-ordering output,
not a body regenerated for captive hardware TSO.

## TCG state, register and flag binding

Native `x19` points to the emulated CPU environment. Provide at least 16 writable
bytes before it and `0x410` bytes after it, with vector-compatible alignment.
The live body retains an internal byte write at `env-0x0c`; initialize the padding
and account for that write. No other native input register supplies guest state.
The register-index/state offsets agree with retained `target/i386/cpu.h`,
the generated IR and the sealed instructions.

| Offset from `x19` | Width | Meaning |
| --- | --- | --- |
| `0x00` | 8 | RAX; load result or initial store value/alias pointer |
| `0x08` | 8 | RCX; initial `C`, live output `S` |
| `0x10` | 8 | RDX; initial `D`, live output `D'` |
| `0x18` | 8 | RBX memory pointer |
| `0x38` | 8 | RDI vector-output pointer |
| `0x110` | 8 | Live output `cc_dst=S` |
| `0x118` | 8 | Live output `cc_src=D` |
| `0x128` | 4 | Live output `cc_op=11`, `CC_OP_ADDQ` |
| `0x3c0` | 16 | Input/output XMM0 |
| `0x400` | 16 | Input XMM1, unchanged |
| `-0x0c` | 1 | Live output upstream `can_do_io=1`; not guest state |

Verify arithmetic through `(cc_op,cc_dst,cc_src)=(11,S,D)`; original `C` is
recoverable as `(S-D) mod 2^64`. The corresponding source is `CC_OP_ADDQ`
in `target/i386/cpu.h` and `compute_aco_addq` in `target/i386/tcg/cc_helper.c`.
TCG's final native NZCV is from `cmp S,0` for SETZ, not ADD's arithmetic flags.
Do not compare that NZCV directly with Box64's ADDS result. The extraction retains
the final `cc_op` store, which is necessary to interpret the two deferred values.

| Body | Native registers written |
| --- | --- |
| Bare load/alias | `x20` |
| Bare store | `x20,x21` |
| Live load/alias | `x16,x20,x21,x22,q0,q1,NZCV` |
| Live store | `x16,x20,x21,x22,x23,q0,q1,NZCV` |
| Empty NOP | None |

`x19`, SP and LR are preserved. The selected bytes need no `guest_base` register:
actual operand accesses use a loaded pointer plus xzr. Live bodies include vector
loads/writeback, canonical guest-GPR writes and deferred flag writes. Bare bodies
include state load/result writeback. They retain the emitted ISHLD/ISH/ISHST
barriers; live vector stores use two GPRs and STP, not Box64's Q-register store.

Excluded: the shared 80-byte native prologue, TB entry interrupt check and initial
`can_do_io` write, RIP update and out-of-block branches/dispatcher return. The
retained internal `can_do_io=1` write in live bodies makes these extraction costs
specific to this upstream output; it cannot be described as an unavoidable captive
cost. There are no selected PC-relative instructions requiring relocation.

## Interpretation and controlled-fault limit

The slices preserve actual guest-work lowering and some state costs, but have
different state conventions. Entry/setup/return, excluded dispatch, register
materialization, translation, recovery metadata and helper costs remain outside
body timing. Empty-NOP subtraction does not equate those costs.

A mapped/unmapped access control can validate native probe fault capture. It does
not establish precise x86 recovery: for example Box64 may still hold the new XMM0
only in q0 when the intervening load faults, while TCG already wrote its canonical
XMM0. Do not invoke a full-recovery implementation or interpret these different
partial native states as generator failure. Inspect/capture the declared state and
native context under the probe's existing fault mechanism. Any inability to bind
the finite selected bytes safely makes that measurement unavailable within the
fixed allowance, not a reason to open another recovery-field investigation.
