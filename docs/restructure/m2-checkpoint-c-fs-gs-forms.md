# Checkpoint C compatibility FS and GS operand checks

**Superseded (November 23, 2026):** the per-opcode checks this inventory tracks were replaced by one generic operand
check ([the plan](m2-checkpoint-c-plan.md#generated-code-one-generic-check-and-two-identity-bits)); every "Open" and
"In implementation" row of the generated table is covered by it. Kept as the record of the forms and their paths.

**Frozen snapshot, not current status.** The inventory was written part-way through checkpoint C, at the build
that had the checked scalar MOV family and the resident cut for based string sources (Codex's working commit
`0730558e`; FEX `fba5d0f0` on the fork's `m2-checkpoint-c` branch, then `a228fd8`). The line-number links are to that
build and have moved since. Built after the snapshot, and still marked "In implementation" or "Open" in the tables
below: CMP, XOR and SUB reads; the other integer reads (ALU, CMOVcc, bit counts, IMUL, CRC32, MOVBE loads);
MOV-immediate, SETcc, MOVBE and MOVNTI stores; ADD and register-source ALU updates of memory; XLAT; XCHG and LOCK
CMPXCHG; implicit multiply and divide; the BMI memory sources; MOVUPS/MOVUPD and their VEX.128 loads; and the inline
check for dword MOV. No later commit covers the other "Open" rows.

This source inventory freezes natemu `0730558e` and its FEX gitlink `a228fd8`.
The FEX links point into the pinned submodule in that checkout.
Pre-C admission refers to `035b243e`. The compiler's `Primary`, `Two`, `Vex`, `Avx`, `Mmx`,
`Sse`, `X87`, and `Classify` tables delimit the forms under discussion, including feature gates,
mandatory prefixes, and register-only forms. Among these tables the frozen subset adds the
resident cut for based string sources. This inventory describes the path that performs each
access. A source check is distinct from a completed runtime test or independent acceptance.
[Compiler admission and cuts][slice].

## Generated operands

| Previously admitted compatibility memory forms | Effect-producing path and status at `0730558e` |
| --- | --- |
| Register MOV `88`–`8B`, accumulator moffs `A0`–`A3`, extending MOV `0F B6/B7/BE/BF` | **Checked.** Actual memory calls through `LoadSource_WithOpSize` and `StoreResult_WithOpSize` use `FexcCheckSegmentAddress`. Moffs passes its decoded absolute offset, and extensions pass the source width with destination merge information. Admission independently enumerates the forms, including `66` word-to-word B7/BF already decoded by this implementation, and checks the native access, the stub and continuation. The resident checks the loaded descriptor and returns a linear address or completes a wrapped scalar transfer. [FEX hook][hook], [moffs and extending loads][moffs], [admission][admit], [resident check][stub], [wrapped completion][wrapped]. |
| SUB, XOR and CMP with a register destination and ModRM memory source | **In implementation.** All use a generic GPR source load. SUB/XOR then compute flags and write the register. CMP writes flags only. The frozen subset admits them outside its checked scalar list. [FEX ALU][alu], [CMP and TEST][cmp], [admission][admit]. |
| Other ordinary integer single loads or stores | **Open.** Share the checked access with the preceding rows. This includes ADD/OR/ADC/SBB/AND register destination, TEST and memory-first CMP including immediate forms, CMOVcc, SETcc, immediate MOV `C6/C7 /0`, MOVBE, MOVNTI, MUL/IMUL, DIV/IDIV, BSF/BSR, POPCNT/TZCNT/LZCNT, CRC32, and the admitted BMI1/BMI2 memory-source forms. Most use `LoadSourceGPR` or `StoreResultGPR` wrappers. CMOVcc reads memory even when the condition fails. DIV/IDIV read the divisor before the divide effect. MOVBE uses ordinary loads and stores around byte reversal. [Compiler policy][slice], [CMOV and SETcc][cmov], [integer products and divide][muldiv], [ALU and MOVBE][alu]. |
| XLAT | **Open.** A dedicated byte load uses the offset from EBX and unsigned AL before replacing AL. It bypasses the generic source wrapper. [XLAT lowering][xlat]. |
| Generated non-atomic RMW | **Open.** Memory-destination ADD/OR/ADC/SBB/AND/SUB/XOR and their immediate forms, INC/DEC, NOT/NEG, ordinary shifts/rotates, SHLD/SHRD where generated, and the non-LOCK XADD/CMPXCHG paths use a read and a later store or a dedicated memory sequence. Check the architectural write permission and whole operand before the first effect, including paths that compare, change flags or suppress a transfer. Resident carry-rotate and double-shift cuts are separate below. [Integer dispatch][integer], [ALU][alu], [RMW decoder policy][slice]. |
| Bit-string BT/BTS/BTR/BTC | **Open on the ordinary generated path.** The effective offset uses the signed register-selected word/dword adjustment before segmentation. Immediate selection stays inside the addressed unit. The width is the entire unit, and modifiers need write permission. FEX's `BTOp` directly lowers loads, stores or atomics on this address. The alignment-gated resident path checks the adjusted offset before its one-instruction retry. [FEX bit-string addressing][bt], [resident alignment cut][bt-resident]. |
| LOCK arithmetic/bit operations, memory XCHG, CMPXCHG/XADD and compatibility CMPXCHG8B | **Open.** Native memory XCHG is always atomic. The lockable forms use direct address creation and atomic IR, including CAS and the paired compare/exchange. Establish the check before the atomic operation and preserve its write intent even when comparison fails. Generated split access and split-lock continuations consume site addresses after the instruction has entered execution. They need the same checked span, with separate completion proofs for split read, write and atomic paths. [Lock policy][slice], [FEX atomic lowering][atomic], [runtime split paths][split]. |
| Explicit near target or data operand on CALL/JMP `FF /2,/4`, PUSH `FF /6`, and POP `8F /0` | **Open.** The generic load feeds the near target or value, then CALL/PUSH write the implicit SS stack. POP loads its SS stack and computes its updated stack pointer before the generic destination store. Guard the explicit operand with the correct address state and preserve the original instruction's fault order and stack commit. The implicit stack never takes the FS/GS prefix. [FEX stack/near control][stack], [explicit JMP][jump]. |
| Ordinary MMX, SSE and AVX/AVX2 loads/stores and memory arithmetic | **Open.** The allow tables cover supported movement, scalar and packed arithmetic/compare, conversion, shuffle, logical, crypto and widening/narrowing forms. Generic wrappers can take 1-, 2-, 4-, 8-, 16- or 32-byte architectural spans. Use the input/output memory width, including `VCVTPH2PS`, scalar VEX and widening vector reads, rather than register width. Keep the feature/FP gate before the segment check and identify alignment, non-temporal and split effects after it. STMXCSR and VSTMXCSR already use a generated dword store wrapper. [SSE/AVX tables][slice], [generic FEX transfer][hook], [STMXCSR][stmxcsr]. |
| Narrow vector elements, broadcasts and vector pairs | **Open.** MOVHP/LP, insert/extract (including PINSR/PEXTR and INSERTPS), and broadcast paths can call `MakeSegmentAddress` and issue lane transfers directly. Aligned 16-byte moves can use `LoadMemPair`/`StoreMemPair` with two eight-byte registers to retain single-copy atomicity. Packed 32-byte operands can also lower to pair sites. Prove the architectural span before the first native access or lane/register effect, with a separate pair/split continuation contract. [Legacy vector addressing][vector], [element and broadcast paths][elements], [AVX with 128-bit host vectors][avx128], [native pair sites][pair]. |
| Generated x87 memory arithmetic, compares, loads, integer conversions and stores, FNSTCW/FNSTSW, inline FLDCW | **Open.** In the reduced-precision path the guest widths are m16/m32/m64, independent of the host double representation. Arithmetic and comparisons load the source before using it. Loads and stores interact with the x87 stack cache, control word, fault roots and delayed state changes. Inline FLDCW also has a resident control-word helper after its word load. Prove the operand before the first guest effect and preserve x87 gating, pending exceptions, stack state and integer-retention retry. [x87 selection][slice], [reduced-precision lowering][x87f64], [x87 dispatch][x87fex], [guess and continuation][stub]. |

Generated ordinary-RAM accesses ultimately use native load/store sites. Native shadow misses retry
those sites, while an access with a completion record can become one host-served MMIO transaction.
The segment check must dominate either route. The existing scalar slice has wrapped RAM, ROM and
unassigned-page completion and the existing typed refusal for unsupported crossing device accesses.
Other generated forms do not inherit wrapped completion or MMIO admission from the scalar check.
The packed string-compare helpers and x87 numerical helpers consume values already loaded by
the generated instruction. They do not perform a second guest memory access on behalf of the
operand. [Native abort/split processing][split], [completion classification][completion],
[device effects][mmio], [packed string compare lowering][pcmp], [x87 helper][helper].

## Resident operands

| Previously admitted compatibility memory forms | Effect-producing path and status at `0730558e` |
| --- | --- |
| MOVS/LODS/CMPS based source with the admitted REP variants | **Checked.** The compiler cuts these forms, and admission rejects them from generated blocks. The resident checks the selected source and ES destination per access and skips both when REP starts with zero ECX. MOVS consumes the source before the ES write, CMPS reads ES before its source, and the transaction preserves completed progress. Unassigned/firmware reads and allowed device transactions use the resident string backing route. STOS/SCAS have only the fixed ES operand. [Cut and decode][string-decode], [resident string order][strings], [device routing][mmio]. |
| Port OUTS; INS as the fixed-ES control | **Checked.** `ports::transaction` checks the I/O permission before the operand and checks the selected OUTS source per element. INS does not use FS/GS. [Port transaction][ports]. |
| BOUND, ARPL, memory segment moves, LES/LDS/LSS/LFS/LGS, memory far CALL/JMP, LAR/LSL/VERR/VERW and user-readable descriptor/control stores | **Checked through resident operands.** BOUND checks both bounds together, and ARPL checks a writable word before its RMW. Load-far and far control transfer read the offset and selector as one span. MOV to/from segment, selector inspection, SGDT/SIDT/SLDT/STR and SMSW call the same width/direction rule where their privilege gates allow the operand. Implicit descriptor reads, updates and stack effects use their own paths. [Compatibility operations][compat], [segment operands][seg-execute], [far operands][far], [SMSW][sys]. |
| Memory RCL/RCR by CL and the selected immediate zero-count forms, SHLD/SHRD m16 by CL | **Checked on these cuts.** The resident uses one checked writable operand, preflights the span, and commits data and flags with the instruction's existing count policy. The rest of the generated shift group stays open. [Cut policy][slice], [resident memory RMW][rotate]. |
| CLFLUSH/CLFLUSHOPT/CLWB | **Checked.** Use the resident byte-read address and page-walk rule. They do not execute FEX's generic cache-line maintenance helpers under this profile. [Cache cut][slice], [resident cache transaction][cache]. |
| FXSAVE/FXRSTOR, XSAVE/XSAVEOPT/XSAVEC/XRSTOR, LDMXCSR/VLDMXCSR and VCVTPS2PH m | **Checked on resident routes.** Image instructions check the architectural area, then alignment and the actual component backing. MXCSR loads check a dword. Half-conversion stores check their destination before conversion effects are committed. [Image transaction][xsave], [MXCSR][sse], [half store][half]. |
| Resident x87 environment/control/image operations, FLD/FSTP m80 | **Checked on resident routes.** FLDCW when cut, FLDENV/FNSTENV, FRSTOR/FNSAVE and m80 transfer paths pass their full width after x87 gating and before their memory operation. Register-only transcendental helpers are not guest memory accesses. [Resident x87 transaction][x87], [x87 helper][helper]. |
| VGATHER/VPGATHER (VSIB) | **Active lanes checked.** The resident computes each signed index at the operand address size, checks that element before its load, and preserves earlier destination/mask progress on a later fault. The unconditional selector check still applies to an empty mask in this snapshot. Empty-mask null handling is in flight following the independent AMD reference snapshot `b8018a26`. [Cut policy][slice], [resident gather][gather]. |
| VMASKMOVPS/PD and VPMASKMOVD/Q | **Active lanes checked.** The resident checks all active segment ranges before page preflight, then moves active data and commits the destination only after preflight. The unconditional selector check still applies to an empty mask. The separate empty-mask change must leave active null handling and all-active-lane preflight intact. [Cut policy][slice], [resident masked moves][mask]. |
| MASKMOVQ, MASKMOVDQU, VMASKMOVDQU | **Checked full span.** The implicit DI-based store preflights its entire 8- or 16-byte span even when the byte mask is all zero. This is a different retained policy from the active-lane forms. [Decoder][string-decode], [resident masked moves][mask]. |

The common resident operand rule checks the segment at the original offset and width, then wraps
the resulting compatibility linear address. Existing tests show examples of flags/state preserved on faults,
including wrapped scalar and resident transfers, string progress, gather partial progress, masked-store
preflight and x87/environment and state image lengths. They do not establish every row or every width.
[Operand rule][segs], [compatibility fixtures][cases].

## Carryover boundaries and implementation order

- Existing non-access routes include register-only forms, LEA, direct near branches, fences, implicit SS accesses and any fixed ES destination. The admitted PREFETCH/PREFETCHW and CLDEMOTE are non-faulting hints, and Reserved-NOP cuts do not touch the decoded memory operand. Verify that an ignored prefix stays ignored. [Compiler rule][slice], [FEX prefetch][hints].
- Generated `67h` compatibility addressing is a pre-C refusal. Earlier resident cuts retain their separately decoded address sizes. Other exclusions include the pre-C opcode/feature refusals in the allow tables, including EVEX/AVX-512, XOP/RTM, unimplemented BCD x87 memory helpers, and unsupported shift-group aliases or undefined-count cases. Privileged system operations that reject CPL 3 before using an operand do not add a user memory route. [Compiler exclusions][slice], [resident privilege gate][sys].
- Build one shared generated access check for scalar reads and writes, then apply it to the single-access integer rows. Keep independent implementation and test work for memory-destination RMW and atomics, adjusted bit-string offsets, explicit stack/control operands, plain and element/pair vector operands, and generated x87. The producer and mandatory admission must agree on the operand width, effect order, and retry/completion contract for each group. Reuse checked resident transactions when they preserve the instruction's ordering, and ensure no previously admitted family can fall through unchecked when the checkpoint closes. [Admission][admit], [resident span checks][segs].

Use the compatibility runtime cases at both block sizes and the compiler replay for the open generated
groups. For scalar forms, distinguish read from write permission, width and preserved register/flags.
For memory RMW, require unchanged memory and flags on a segment fault, including unsuccessful CAS and
register-index bit addressing. For stack/control, check the explicit operand's fault order and saved
stack state. For vectors, include scalar, narrow element, full and paired spans and the segment-before-
alignment case. For x87, check pending/gated fault priority and unchanged status, tags and stack on a
segment fault. Exercise wrapped/split and device routes according to the existing completion contract for
each path. For the resident masked cases, an active-lane second fault and an empty mask need separate
judgments, including the null-selector and legacy full-span distinctions above. [Compatibility fixtures][cases].

[slice]: ../../tools/fexc/shim/fexc_slice1.inc#L48
[admit]: ../../crates/host/src/admission/structure.rs#L746
[stub]: ../../crates/runtime/src/execution.rs#L3936
[wrapped]: ../../crates/runtime/src/execution.rs#L4078
[split]: ../../crates/runtime/src/execution.rs#L4141
[completion]: ../../tools/fexc/shim/fexc_mmio.inc#L19
[mmio]: ../../crates/runtime/src/execution/mmio.rs#L171
[string-decode]: ../../crates/host/src/system.rs#L342
[strings]: ../../crates/runtime/src/execution/strings.rs#L33
[ports]: ../../crates/runtime/src/execution/ports.rs#L106
[compat]: ../../crates/runtime/src/execution/compat.rs#L16
[segs]: ../../crates/runtime/src/execution/segments.rs#L188
[seg-execute]: ../../crates/runtime/src/execution/segments.rs#L41
[far]: ../../crates/runtime/src/execution/segments.rs#L704
[sys]: ../../crates/runtime/src/execution.rs#L3032
[bt-resident]: ../../crates/runtime/src/execution/events.rs#L273
[rotate]: ../../crates/runtime/src/execution/rotate.rs#L33
[cache]: ../../crates/runtime/src/execution/syscalls.rs#L223
[xsave]: ../../crates/runtime/src/execution/xsave.rs#L51
[sse]: ../../crates/runtime/src/execution/sse.rs#L88
[half]: ../../crates/runtime/src/execution/half.rs#L5
[x87]: ../../crates/runtime/src/execution/x87.rs#L350
[helper]: ../../crates/runtime/src/execution/helpers.rs#L11
[gather]: ../../crates/runtime/src/execution/gather.rs#L47
[mask]: ../../crates/runtime/src/execution/maskmov.rs#L41
[cases]: ../../tests/smp/compat_limits.py#L24
[hook]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L4687-L4893
[moffs]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L1105-L1402
[alu]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L4956-L5243
[cmp]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L1008-L1141
[cmov]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L625-L671
[muldiv]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L3933-L4022
[xlat]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L3013-L3022
[integer]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L1447-L1611
[bt]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L2455-L2608
[atomic]: ../../third_party/FEX/FEXCore/Source/Interface/Core/JIT/AtomicOps.cpp#L14
[stack]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L329-L553
[jump]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L919-L932
[stmxcsr]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/Vector.cpp#L3464-L3469
[vector]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/Vector.cpp#L27
[elements]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/Vector.cpp#L2066-L2206
[avx128]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/AVX_128.cpp#L130
[pair]: ../../third_party/FEX/FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp#L746
[pcmp]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/Vector.cpp#L5535-L5566
[x87f64]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/X87F64.cpp#L58
[x87fex]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/X87.cpp#L603
[hints]: ../../third_party/FEX/FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp#L2497
