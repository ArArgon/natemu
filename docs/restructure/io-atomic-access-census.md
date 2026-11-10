# Current atomic instruction census for host I/O observers

November 8, 2026. Source inspection only; no build, execution, frequency census or
new hardware-atomicity evidence. This inventories the current natemu FEX slice1
producer, admission and resident implementation, not every path in upstream FEX.
Links identify the inspected working-tree code and may move with later edits.

## Finding

**The confirmed guest-protected software-atomic class is an admitted memory XCHG/LOCK RMW or
CMPXCHG8B whose actual operand crosses a 16-byte boundary and reaches the
resident alignment-abort fallback.** Its implementation copies bytes under
`cache::split_begin`/`split_end`; that protocol excludes participating guest
vCPUs through shadow invalidation/waits. A raw host observer does not acquire
that protocol merely by reading the backing RAM. This is an actual implementation
gap for an uncoordinated I/O observer, not a claim that all locked instructions
are emulated with a software lock. [Fallback][fallback], [exclusion][exclusion],
[byte-copy implementation][copies].

This is not the complete access-atomicity audit. Independent source review also
found resident scalar helpers that copy bytes without acquiring exclusion, and
an unresolved qualification for native ordinary scalar accesses crossing 16
bytes within a guest cache line. Those separate cases are recorded below.

## Instruction families and current lowerings

Widths below are the complete memory operand in bytes. “Cross16” means the
runtime condition described below; width1 can never satisfy it. Arithmetic,
logical and bit-modifying rows require an explicit LOCK prefix. The [producer's
lockability predicate][lockable] admits memory-destination forms and rejects
register-destination LOCK forms; the [native admission decoder][admission]
recognizes the listed acquire/release LSE encodings as a closed set.

| x86 instruction family | Width | Current native lowering | Software-only atomic case |
| --- | --- | --- | --- |
| Memory `XCHG`, with or without explicit LOCK | 1/2/4/8 | `SWPAL` (B/H/W/X) | Cross16 for2/4/8. [XCHG dispatcher][xchg], [atomic emitter][atomic] |
| `LOCK ADD`, `ADC`, `SUB`, `SBB`, `INC`, `DEC`, `XADD` | 1/2/4/8 | `LDADDAL`; carry is added to the source for ADC/SBB, subtraction negates the source, DEC adds−1 | Cross16 for2/4/8. [ALU dispatcher][alu], [carry dispatcher][carry], [INC/DEC][inc], [XADD][xadd], [emitter][atomic] |
| `LOCK AND`, `BTR` | AND1/2/4/8; BTR2/4/8 | `LDCLRAL`; AND complements the mask, BTR clears its selected bit | Cross16 for2/4/8. [ALU][alu], [bit dispatcher][bits], [emitter][atomic] |
| `LOCK OR`, `BTS` | OR1/2/4/8; BTS2/4/8 | `LDSETAL` | Cross16 for2/4/8. [ALU][alu], [bits][bits], [emitter][atomic] |
| `LOCK XOR`, `NOT`, `BTC` | XOR/NOT1/2/4/8; BTC2/4/8 | `LDEORAL`; NOT uses an all-ones mask | Cross16 for2/4/8. [ALU][alu], [NOT][not], [bits][bits], [emitter][atomic] |
| `LOCK CMPXCHG` | 1/2/4/8 | `DMB ISH` then `CASAL` | Cross16 for2/4/8. Barrier is required even for a failed locked compare. [Dispatcher][cmp], [emitter][atomic], [admission barrier check][barrier] |
| `LOCK NEG` | 1/2/4/8 | Plain load, negate, one `CASAL`; mismatch exits to the same guest RIP and retries | Only the crossing CAS phase uses software exclusion. The ordinary successful CAS is hardware atomic; the preliminary load and retry do **not** make the whole sequence a software-locked critical section. [Exact emitted sequence][neg] |
| `CMPXCHG8B`, with or without LOCK | 8 | `CASPAL` on two32-bit elements; `DMB ISH` only with LOCK | Cross16. The current implementation also gives the unlocked form atomic CAS behavior; do not generalize this strengthening to unlocked CMPXCHG/XADD. [Pair dispatcher][pair], [emitter][atomic] |
| `CMPXCHG16B`, with or without LOCK | 16 | `CASPAL` on two64-bit elements; `DMB ISH` only with LOCK | **None through this fallback:** aligned16 stays within a16-byte unit; a misaligned operand raises guest `#GP(0)` before the split-lock branch. [Pair dispatcher][pair], [abort decision][abort] |

Bit-string operations use the **full2/4/8-byte operand unit** at the effective
address after the signed bit-string displacement, not a one-byte update at the
selected bit. This distinction is executable in [the current dispatcher][bits].

The producer defaults `SupportsAtomics` to true in its [feature profile][features].
Upstream LL/SC fallback code is present in `AtomicOps.cpp`, but the natemu
admission set above is LSE, so that unrelated source path is not evidence of an
admitted software-lock implementation. Likewise, slice1 NEG calls its dedicated
single-attempt emitter instead of upstream FEX's inline backward CAS loop.

## Exact software trigger and exclusions

The [abort handler][abort] checks an EL0 alignment abort and then
`(address & 15) + width > 16` for SWAP/ATOMIC sites. Thus the crossing offsets are:

| Width | Start offset modulo16 that reaches the crossing predicate |
| --- | --- |
| 1 | None |
| 2 | 15 |
| 4 | 13–15 |
| 8 | 9–15 |
| 16 | Misaligned CMPXCHG16B faults first; no valid split fallback |

Natural misalignment alone is insufficient: an8-byte operand at offset1 stays
within16. Crossing a cache line or a page is sufficient to cross16 for these
widths, but neither is necessary: an8-byte operand at offset12 already qualifies
within the same cache line and page. The source fallback is therefore broader
than the usual cache-line sense of “split lock.” This describes the implemented
fault routing, not fresh verification of every host alignment behavior.

Fault/unsupported cases remain distinct from software completion:

- Misaligned CMPXCHG16B: `#GP(0)` first. Enabled guest alignment checking can
  produce `#AC(0)` before the software split-lock branch. Noncanonical addresses
  and page faults also remain faults, not partially completed atomics. [Abort][abort]
- Both touched pages are translated for write before fallback effects. An MMIO
  page is refused through the typed atomic MMIO path. [Fallback][fallback]
- LOCK on a non-lockable opcode or register destination is cut to the resident
  `#UD` path. `66` on CMPXCHG8B/16B is refused by the current producer policy;
  it is not silently treated as another supported atomic flavor. [Predicate][lockable],
  [prefix policy][policy], [LOCK cut decoder][ud]
- Ordinary unlocked arithmetic RMW, unlocked CMPXCHG and unlocked XADD are not
  made instruction-atomic by this census. Slice1 explicitly emits load/store
  sequences for the latter two. [CMPXCHG][cmp], [XADD][xadd]
- Register-only XCHG and register arithmetic have no guest-RAM observer issue.
  REP/string operations are not one atomic transaction merely because they are
  one x86 instruction. Ordinary accesses and vector widths require a separate
  access-shape census, not inclusion in the locked-RMW list by analogy.

## Why a host pointer bypasses this protection

In [split_lock][fallback], the resident records the affected GPA pages, waits
for peers that hold shadows to reset them, copies the old bytes, computes the
selected SWP/fetch/CAS/CASP result, copies the result bytes, then releases.
`split_end` issues `DSB ISH`; other vCPUs consult the split-page state before
installing shadows or executing resident accesses. [Protocol][exclusion]

The data operation itself is **not** a host atomic: `GuestRam::copy_in` and
`copy_out` use volatile byte copies. A host backend outside this admission can
observe a partial write or interleave a write between the fallback read and
write. An atomic host load alone does not acquire the software protocol; nor
does `volatile`, `vm-memory`, an iovec, or a fence. [Copies][copies]

This conclusion does not make ordinary native atomic cases safe for arbitrary
`memcpy` snapshots either: multiple host loads need not represent one instant.
It identifies which current **guest atomic update** can itself be implemented
as multiple host-visible writes, requiring observer participation/exclusion.

## Other access forms: confirmed implementation versus required follow-up

The following must remain distinct from the protected locked-RMW fallback:

| Form | Inspected implementation | Assessment |
| --- | --- | --- |
| `MOV Sreg, m16` and `MOV m16, Sreg` | `selector_operand`/`MOV_FROM_SREG` call `linear → copy → GuestRam::copy_out/in`. Since November 10, 2026 `GuestRam::copy_in/copy_out` make a naturally aligned 2-, 4- or 8-byte range one native access of that width (volatile u16/u32/u64); misaligned and page-crossing ranges (always split by `copy` into per-page byte chunks) stay byte copies. [Segment helpers][segments] | The aligned case is now single-copy atomic; faults, A/D bits, data breakpoints, code-page invalidation and the device-page stop are unchanged, all decided in `copy`/`pages` before the access. A misaligned operand may still tear against peers (SDM 9.1.1 requires atomicity there only for cached accesses within one cache line), a remaining follow-up. |
| `LDMXCSR`, `VLDMXCSR`, m32 | Producer routes these loads to the resident's `linear` path, now one 32-bit load when aligned (as above). [SSE helpers][sse], [routing][mxcsrroute] | Aligned case fixed as above; misaligned m32 remains a byte copy. The resident also contains STMXCSR/VSTMXCSR handlers, but current normal producer routing translates those stores through FEX's width-specific m32 store; their handler presence alone is not evidence that ordinary execution uses them. [Translated stores][mxcsrstore] |
| Gather and masked-vector elements | Per-element calls to `copy`, so each naturally aligned 4- or 8-byte element is now one native access. [Gather][gather], [masked moves][maskmov] | The SDM describes gathers and VEX masked moves as separate element accesses of the element width (gather elements in any order) and states no atomicity across elements; an aligned element is then an ordinary aligned scalar access, so one native access per element is plainly correct (and stronger than the byte loop), and no whole-vector atomicity is inferred. MASKMOVDQU/MASKMOVQ and byte elements are byte accesses with no multi-byte requirement and are unchanged. |
| Ordinary native unaligned 2/4/8-byte accesses crossing 16 bytes but inside a guest cache line | Native ordinary load/store; current abort dispatch has no corresponding ordinary-scalar atomic fallback. [Abort][abort] | An operation-specific platform qualification remains necessary. Earlier M4 probes observed no tears at offsets 15/14/12, but explicitly did not establish a guarantee. They do not prove every offset or mixed host/kernel observer case. [Probe qualification][probe] |
| Page-table A/D updates | Naturally aligned `AtomicU64` compare-exchange. [GuestRam CAS][ptecas] | Native atomic commit, not byte-copy emulation. |
| Aligned m128 `MOVAPS`, `MOVAPD`, `MOVDQA` and their VEX.128 forms | Slice1 uses GPR `LDP`/`STP` with the alignment guard. [Legacy forms][mov128], [VEX forms][vmov128] | Uses the existing selected M4/LSE2 aligned GPR-pair premise. Do not generalize it to arbitrary Q-register instructions or 256-bit operands. |

`segments::copy` waits against another vCPU's split lock through `pages`, but
does not acquire `split_begin` itself. Waiting against an existing transaction
does not prevent another observer from seeing its own bytewise transfer.

The distinction is the **atomicity of each architecturally required memory
access**, not atomicity of an entire instruction or I/O buffer. Ordinary
unlocked RMW, REP, page-crossing scalar stores, x87 extended values and large
save areas must not be called defective merely because the whole operation
has intermediate states. Conversely, a scalar access does not lose its own
guarantee just because a helper implements it as a byte loop. Intel's
[SDM][intel] gives the aligned scalar, cached within-line unaligned scalar and
named aligned 128-bit move contracts; [INV-03-04 claim 33][accesscontract]
records the selected access classification. This note is source evidence, not
a new hardware qualification or an observed-tearing report.

## Practical priority, without a frequency claim

1. Keep the naturally aligned scalar path distinct: it uses the admitted LSE
   operations and is the first fast-path candidate to evaluate, not evidence of
   how often guest code executes it.
2. Ensure the host-I/O admission contract covers all overlapping split-lock
   pages, including same-page crossings. Use width2/4/8 boundary cases and the
   lowering classes above as a focused correctness matrix.
3. Include NEG retry/failure, compare-exchange failure, unlocked CMPXCHG8B,
   bit-string address displacement, cross-page operands and MMIO/fault exclusions.
4. Measure dynamic occurrence separately before assigning optimization effort.
   This source pass provides no instruction counts or hot-path percentage.

Before calling the common path fully qualified, separately resolve the scalar
helper accesses and the cross16 ordinary-native qualification above. The
candidate implementation direction for aligned scalar helpers is one native
access of the required width, preserving their existing walk/fault/tracking
checks; it does not require putting every ordinary MOV behind a lock.

Independent review accepted the locked-RMW source mapping and identified the
additional helper and qualification cases above. No production code was
changed and no new build or runtime test was performed.

Source hygiene: comments at `UNSUPPORTED_ATOMIC` and within admission still
describe the former stop-on-cross16 policy. The executable `split_lock` path
now performs the operation. This note follows the code; no comment or source
file was changed during the census.

[fallback]: ../../crates/runtime/src/execution.rs#L2669
[abort]: ../../crates/runtime/src/execution.rs#L2366
[exclusion]: ../../crates/runtime/src/cache.rs#L1371
[copies]: ../../crates/abi/src/guest_memory.rs#L122
[lockable]: ../../tools/fexc/shim/fexc_slice1.inc#L497
[policy]: ../../tools/fexc/shim/fexc_slice1.inc#L715
[ud]: ../../crates/host/src/system.rs#L553
[admission]: ../../crates/host/src/admission/structure.rs#L188
[barrier]: ../../crates/host/src/admission/structure.rs#L782
[features]: ../../tools/fexc/fexc.cpp#L69
[atomic]: ../../third_party/FEX/FEXCore/Source/Interface/Core/JIT/AtomicOps.cpp#L14
[xchg]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L1140
[alu]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L4993
[carry]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L246
[inc]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L3208
[xadd]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L2854
[not]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L2799
[bits]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L2535
[cmp]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L4129
[pair]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher.cpp#L4180
[neg]: ../../tools/fexc/shim/fexc_sites.inc#L824
[segments]: ../../crates/runtime/src/execution/segments.rs#L47
[sse]: ../../crates/runtime/src/execution/sse.rs#L63
[gather]: ../../crates/runtime/src/execution/gather.rs#L75
[maskmov]: ../../crates/runtime/src/execution/maskmov.rs#L81
[probe]: ../../experiments/track03/tso-coverage/README.md#L130
[ptecas]: ../../crates/abi/src/guest_memory.rs#L113
[mov128]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/Vector.cpp#L27
[vmov128]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/AVX_128.cpp#L130
[intel]: https://cdrdv2-public.intel.com/868137/325462-089-sdm-vol-1-2abcd-3abcd-4.pdf
[accesscontract]: ../../docs/research/investigations/INV-03-04.md#L4505
[mxcsrroute]: ../../tools/fexc/shim/fexc_slice1.inc#L612
[mxcsrstore]: ../../third_party/FEX/FEXCore/Source/Interface/Core/OpcodeDispatcher/Vector.cpp#L3464
