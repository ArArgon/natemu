# AMX / SME acceleration of AVX — candidate assessment

2026-11-06. Source-reading notes for the owner's feasibility question. This file
is outside the review workflow: candidate ideas, not accepted evidence,
requirements, implementation assignments, or a change to current direction.
No local benchmark or execution experiment was run.

## Candidate conclusion

**Inferred:** Apple AMX and Arm SME can potentially accelerate selected translated
AVX-family regions. Neither instruction-set width nor matrix peak throughput
establishes efficient general AVX/AVX2/AVX-512 emulation. Retain NEON as the general
baseline; consider SME2 batching or matrix-shaped regions only when profiling and
exact semantics justify them. AMX here means Apple's proprietary extension,
not Intel AMX.

## Source-backed starting points

- Apple's [LLVM M4 support change](https://lists.llvm.org/pipermail/llvm-commits/Week-of-Mon-20240610/1382621.html)
  lists SME and SME2 and explicitly distinguishes them from unsupported ordinary
  SVE. Arm's [SME introduction](https://developer.arm.com/community/arm-community-blogs/b/architectures-and-processors-blog/posts/arm-scalable-matrix-extension-introduction)
  describes streaming mode as a subset of SVE2 with its own vector length.
  Thus SME is useful for ordinary vector operations as well as matrices; it is
  incorrect to dismiss it as matrix-only.
- Apple's [XNU SME state documentation](https://github.com/apple-oss-distributions/xnu/blob/main/doc/arm/sme.md)
  explains that streaming transitions clear V/Z and predicate state, require
  saving live values, and restrict many legacy SIMD instructions. SM and ZA
  enablement are separate. Its kernel management contract is not proof of this
  project's VM state handling. Apple also documents
  [Hypervisor SME state access](https://developer.apple.com/documentation/hypervisor/hv_vcpu_get_sme_state(_:_:)),
  including an unsupported-platform result; exact SDK/runtime availability and
  guest configuration remain integration checks.
- [Hello SME!](https://arxiv.org/html/2409.18779v1), sections II and III and
  Table I, reports 512-bit streaming vectors on its M4 testbed. Its single
  performance-core FP32 microbenchmarks report 113 GFLOPS for NEON FMLA,
  31 for ordinary SSVE FMLA, 501 for SME2 multi-vector FMLA accumulating in ZA,
  and 2009 for SME outer products. These are different operations, not
  interchangeable AVX translations. Testbed: 2024 M4 iPad Pro, iOS 17.5;
  these numbers are not natemu/macOS 26 results, and core placement was inferred
  from QoS and behavior rather than pinned. The paper's scalar integer
  loop-control instructions remain inside streaming loops. Its multicore
  results suggest shared SME resources, so per-vCPU scaling needs measurement.
- Original reverse-engineering work [corsix/amx register file](https://github.com/corsix/amx/blob/main/RegisterFile.md)
  describes 64-byte registers and no direct transfers between AMX and A64
  general-purpose/SIMD registers: transfers go through memory.
  Its [FMA description](https://github.com/corsix/amx/blob/main/fma.md)
  includes pointwise vector mode as well as outer-product mode. These are
  reverse-engineered behaviors, not an Apple-supported ISA contract.
- Arm authors' [SSVE compiler study](https://arxiv.org/html/2506.02233v1)
  specifically studies vector operations without ZA and identifies the need for
  a cost model covering mode changes, data movement, and fallback performance.
  It supports selective profitability analysis, not a natemu speedup claim.

## Consequences to assess before implementation

**Inferred:** A 512-bit streaming register can hold an entire ZMM or a predicated
YMM-width value. This can reduce splitting and register pressure, but ordinary
SSVE throughput may erase those advantages. Long regions amortize transitions;
switching per guest instruction is an unattractive hypothesis. Integer address
arithmetic and branches do not inherently require exits from streaming mode;
unsupported SIMD operations and helper ABI boundaries are the relevant costs.

**Hypothesis:** Recognizing compatible groups of independent FMA accumulations
could exploit SME2's multi-vector forms. Recognizing matrix-shaped loops could
use outer products. Both need proof of guest floating-point ordering, rounding,
exception and NaN behavior, lane/mask semantics, memory effects, precise faults,
and reconstructible architectural state. A mathematical resemblance is
insufficient permission to reassociate floating-point operations or guest loads.

M4-specific specialization must have an M2 fallback. Do not assume undocumented
AMX has the same accessibility inside HVF as in a host process. Likewise,
CPUID-advertised AVX-512 requires the entire advertised feature contract and
architectural state handling, not merely 512-bit arithmetic.

The [existing performance backlog](../../restructure/perf-phase2-plan.md#backlog-owner-november-4)
defers SME/SME2 AVX2 work. This note does not reopen its implementation order.
If pursued, first compare representative existing translated loops against
SSVE and SME2 candidates, including entry/exit, faults, helper calls and N-vCPU
contention; retain the current path unless complete-region results win.

## Upstream backend inspection, 2026-11-06

Source inspection only; no execution or performance claims. Public upstream
`main` snapshots were fetched by commit for this check:

- **FEX `ae495b971b1e148f7fac26fe7451b1a5df971303`**, committed
  2026-11-06 14:52:49 UTC. Searching `FEXCore/` and `Source/` for SME/SME2,
  streaming entry/exit, SVCR, and outer-product emission found feature
  enumeration/detection and exception diagnostics, but no SME translation
  backend. [HostFeatures.cpp](https://github.com/FEX-Emu/FEX/blob/ae495b971b1e148f7fac26fe7451b1a5df971303/Source/Common/HostFeatures.cpp#L535)
  sets `SupportsSVE128/256` from ordinary SVE2 support and, for 256 bits, the
  ordinary SVE vector length. SME detection does not activate this backend.
- **Box64 `50855a1e2bdb0bd09135bb84d6904223775f16a5`**, committed
  2026-11-06 19:36:04 UTC. The corresponding search over `src/` found no SME
  translation backend. Its [`SMSTART()` macro](https://github.com/ptitSeb/box64/blob/50855a1e2bdb0bd09135bb84d6904223775f16a5/src/dynarec/dynarec_helper.h#L80)
  resets strong-memory sequence bookkeeping; it is not the Arm instruction.
  A concrete [VADDPS translation](https://github.com/ptitSeb/box64/blob/50855a1e2bdb0bd09135bb84d6904223775f16a5/src/dynarec/arm64/dynarec_arm64_avx_0f.c#L455)
  loops over `1 + vex.l` 128-bit pieces and emits NEON `VFADDQS`.

**Inferred:** These inspected upstream backends do not provide an existing SME
mode-management implementation to reuse for natemu. This is scoped negative
source evidence, not a claim about every fork, pending change, or future release.
