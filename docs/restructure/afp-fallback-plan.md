# AFP fallback: x86-exact floating point on hosts without FEAT_AFP, at no cost to the common case

Status: draft for independent review (November 15, 2026), branch `afp-fallback` off `cpu-features` (43b553e). No code
yet. Owner direction (November 15): plan, review, then build; it must be performant; keep this plan with a progress
table. Evidence: [the inventory](afp-inventory.md), which checks each claim against Arm's pseudocode (Sail translation
of the Armv9.4 ASL), the code and the SDM text natemu already quotes. Section numbers below written "inventory 1.2"
point into it.

**Goal:** on a host whose vCPU lacks FEAT_AFP (the M1 and M2), natemu's SSE, AVX, FMA and x87 results and flags are
what the M4 gives today, or as close as each item's cost justifies. "Performant" is a gate, not a wish:
- **The M4:** generated code and compile-cache keys stay byte-identical (replay gate).
- **A host without AFP, in the common mode:** MXCSR 1F80H (everything masked, no DAZ, no FTZ), as Linux and most programs
  run. Every fix costs nothing there, except the one NaN guard of decision A, which is measured before it ships.
- **Rare modes:** costs fall only in code compiled for them (DAZ, FTZ, an unmasked exception), which is keyed
  separately, so other code never pays for them.

**Non-goals:**
- Making the M4 any more exact than it is: the x87 larger-significand NaN rule (COR-09) stays as it is there, unless a
  cold path built here fixes both hosts for free.
- 80-bit x87 precision.
- RPRES (off by policy).
- Guest feature changes. The guest CPU is the same on every host.

## 1. The enabler: an M4 can emulate a host without AFP

Every AFP behaviour in the Arm pseudocode depends on FPCR.AH, FIZ or NEP being set (inventory 0). An M4 with those
three clear and fexc told `afp=0` is architecturally the M2's floating-point machine. Today the resident sets AH and NEP
unconditionally (`execution::fpcr`, `x87_fpcr`), so forcing `afp=0` gives a combination no machine has.

Chunk 1 makes `fpcr`/`x87_fpcr` depend on the effective host AFP bit, which the host passes in `cache::Control`. That
bit is the probe's value after `NATEMU_FEXC_FEATURE_OFF`. So `NATEMU_FEXC_FEATURE_OFF=afp` on the M4 becomes full M2
emulation. All development, the tests and the benchmarks run on the M4 that way. One confirming run on the real M2
(owner) closes it.

## 2. What differs without AFP, and what this plan does about each

Costs are in Arm instructions per affected x86 instruction, with constants hoisted per block. "Common mode" means
MXCSR 1F80H.

| # | Difference without AFP (inventory section) | Fix | Cost in the common mode | Cost elsewhere |
| --- | --- | --- | --- | --- |
| 1 | `fpcr` writes RES0 bits; can't emulate an M2 (1) | host-conditional `fpcr`, `x87_fpcr`, `--x87-fpcr` | 0 | 0 |
| 2 | MINSS/MAXSS/MINSD/MAXSD miss IE for a QNaN: FEX's non-AFP scalar path uses the quiet `fcmp` (1.5) | `fcmpe` in that path (fork patch) | 0 | 0 |
| 3 | DAZ + FTZ (gcc `-Ofast`): an FZ flush sets UFC without IXC, so PE is missing (1.7, 1.8) | map UFC to UE and PE under effective FTZ, in `mxcsr_flags` and `FexcMxcsr` | 0 | +1-2 in inline STMXCSR |
| 4 | DAZ alone does nothing (FIZ is RES0) (1.7) | FZ = 0 and a software input flush per FP source, in code keyed DAZ-only | 0 | 3 per source |
| 5 | FTZ alone also flushes inputs, and the flush lacks IXC (1.7) | FZ = 0 and a software output flush that raises UFC and IXC exactly for flushed lanes (`fmul` by 1-2^-p), in code keyed FTZ-only | 0 | ~5 per result |
| 6 | MIN/MAX under DAZ return the raw denormal (1.5 C) | flush inputs before the compare, in DAZ-keyed code | 0 | +6 per MIN/MAX |
| 7 | Unmasked SIMD DE never fires (1.6) | software denormal-input test with x86's precedence, folded into `FexcSimdCheckWords` as FPSR bit 7 (no `msr`), only under unmasked DE | 0 | ~10-14 per checked op |
| 8 | x87 DE for FLD and arithmetic with an m32/m64 operand is never set (1.6, A6) | an integer test of the memory operand's bits, ORed into the pending byte `FexcX87End` already stores | ~4 per x87 memory-operand FP instruction | same |
| 9 | Unmasked UE: a result rounding up to ±MIN_NORMAL raises a spurious #XM (1.8) | when UFC is set and a lane equals ±MIN_NORMAL, decide in a slow path by exact recomputation; only under unmasked UE | 0 | ~3-4 plus a rare slow path |
| 10 | Unmasked IE: FMA inf×0 + QNaN raises a spurious #XM (1.3) | sanitize the multiplicands where the addend is a NaN; only under unmasked IE | 0 | ~5 per FMA |
| 11 | NaN results: the default NaN is +qNaN instead of x86's negative indefinite (`0.0/0.0` prints "nan", not "-nan"); QNaN with SNaN picks the SNaN; FMA picks NaNs in another order; FMA and ADDSUBPS negations flip a NaN's sign (1.1-1.4) | **decision A**: one "result is NaN" guard per FP arithmetic op (`fcmeq`, `uminv`, `fmov`, `cbz`), with the exact x86 rule in an out-of-line cold path | 3 scalar, 4 packed (+1 `mov` when the destination aliases a source) | same |
| 12 | Masked DE: the sticky MXCSR.DE flag is never set (1.6) | **decision B**: accept, or an opt-in strict mode | 0 if accepted | ~10-14 per op if strict |
| 13 | Masked UE flag in the MIN_NORMAL band (1.8) | accept as a declared difference | 0 | 0 |

Already right without AFP (inventory 5, last list):
- MIN/MAX vector NaN, signed-zero and IE behaviour;
- scalar-insert semantics;
- single-NaN propagation and two NaNs of the same kind;
- IE, ZE, OE and PE in general;
- inexact tiny UE outside the band;
- conversions with FZ = 0;
- every resident helper (the resident runs with FPCR = 0 on every host).

**Code keying for rows 4-7, 9 and 10.** Compiled code is keyed today on the unmasked sets and an x87 rounding override,
but not on DAZ or FTZ (inventory 4). On hosts without AFP only, two key bits are added:
- DAZ;
- effective FTZ (FTZ with UE masked, the condition `fpcr` already uses).

The M4's keys and code are therefore unchanged. LDMXCSR, FXRSTOR and XRSTOR are already resident transactions, and the
context is recomputed at every lookup.

## 3. Decisions for the owner

**A. Exact NaN results in the common mode.** This is the guard of row 11. It costs 3-4 instructions per FP arithmetic
op on hosts without AFP, and the M4 pays nothing. It fixes:
- the negative indefinite, the most visible difference: glibc prints "nan" for 0/0 instead of x86's "-nan";
- the QNaN/SNaN pair;
- the FMA NaN order and NaN signs;
- in the cold path, the x87 larger-significand rule, for both hosts.

One residual remains: a masked FMA inf×0 + QNaN still leaves a spurious IE flag.

The recommendation is to build it behind a measurement gate (chunk 4). It ships on by default if, in M2 emulation, the
FP-heavy existing benchmarks (`cray`, `luajit`, `avx`, `sqlite`, `zstd`; 5 interleaved runs each) slow down by at most
3% geometric mean and at most 5% on any one. Otherwise it goes back to the owner with the numbers.

The alternative is to accept these as declared NaN payload and sign differences on hosts without AFP, at zero cost.

**B. The masked DE flag in the common mode.** The recommendation is to accept it as a declared difference on hosts
without AFP:
- DE is not in glibc's `FE_ALL_EXCEPT`, so standard C fenv never reads it;
- exactness costs ~10-14 instructions on every FP op;
- unmasked DE is still exact (row 7).

The alternative is an opt-in strict mode, a host option keyed in the compiled code.

## 4. Chunks, gates and performance checks

The M4's default (AFP on) must stay byte-identical in every chunk: replay of both recorded streams
(`natemu-raw/cpu-c0-stream-20261115-02`) against the new fexc gives 0 differences. Every functional gate runs in M2
emulation on the M4. The full suite runs once, before merge.

| Chunk | Content | Gate |
| --- | --- | --- |
| 1. Emulation and baseline | rows 1; effective AFP bit in `cache::Control`; `fpcr`/`x87_fpcr` follow it; `x87_fpcr_differs_only_when_it_must` and related unit tests updated | replay identical on the M4; in M2 emulation, run the AFP-dependent runners (`run_i5a_de.py`, `run_i5c.py`, `run_i6_faults.py`, `run_i8_faults.py`, `run_i5b.py images`, the FEX ASM suite) and the QEMU corpora (`gen_i5a`/`gen_i6`/`gen_i8` comparisons): record the failing cases. This is the work list, and checks the inventory's predictions |
| 2. Zero-cost fixes | rows 2, 3, 8 (row 2 is a fork patch on the `natemu` branch) | replay identical on the M4; in M2 emulation, those runners' affected cases pass; micro check: x87 memory-operand loop within noise on the M4 and +≤5% in M2 emulation |
| 3. Keyed rare modes | the two key bits; rows 4, 5, 6, 7, 9, 10 | replay identical on the M4; in M2 emulation, `run_i5c.py` DAZ/FTZ/unmasked cases and the corpora's DAZ/FTZ groups pass; a common-mode replay in M2 emulation shows the generated code unchanged by these rows (the cost stays in keyed code) |
| 4. NaN guard (decision A) | row 11: guard plus cold path (SSE first-source rule and negative indefinite, FMA Rn/Rm/addend order, x87 rule); the FMA negations done by integer sign handling where cheaper | in M2 emulation, the NaN cases of the corpora and runners pass; **performance gate:** the 5 FP-heavy benchmarks interleaved ×5, guard on vs off, in M2 emulation; ship per decision A's threshold |
| 5. Records and the M2 run | declared differences (rows 12, 13 and any residual) in the README and contracts; the startup line keeps `host -afp`; the owner runs the suite subset and the probe tool on the M2 | the M2 run matches M2 emulation |

**Performance method.** This follows the standing rules: benchmarks only at the end of a chunk; only the main session
runs VMs; at least 5 interleaved runs per build before claiming any change under 5%. The common-mode cost of rows 2-10
is checked structurally, not by benchmark: their code is emitted only under keyed modes, which a replay of the
common-mode streams in M2 emulation proves.

## 5. Risks

- **The fork and upstream FEX's policy.** The fork is ArArgon/natemu-fex, branch `natemu`; commits there are authored by
  the owner and reviewed like natemu's. Rows 2, 4-7, 9-11 touch FEX's JIT or natemu's FEX shim. These changes stay on
  the fork: upstream FEX's contribution policy does not accept AI-generated code, so none of it is offered upstream.
- **M2 emulation misses a real-M2 difference.** The pseudocode says there is none. The owner's M2 run checks it.
- **Cold-path correctness.** The x86 NaN rules are table-driven in the SDM (Vol.1 Table 4-7, FMA's operand order).
  The cold path is tested against QEMU and real-x86 cases where they differ, using the tiebreaker rule.
- **A side finding outside AFP** (inventory 1.4): FEX's ADDSUBPS fallback without FCMA flips NaN signs on every host. So
  FCMA belongs in the exactness class, not speed. That is fixed in the cpu-features records. FCMA is on every
  supported chip, so no host is affected.

## Progress

| Date | Chunk | State | Evidence |
| --- | --- | --- | --- |
| Nov 15 | research | done: [inventory](afp-inventory.md) | read-only; Arm ASL (Sail), code, SDM |
| Nov 15 | plan | drafted; independent review next | this file |
