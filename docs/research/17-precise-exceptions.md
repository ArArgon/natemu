# 17 — Precise Exceptions in a System-Level x86-64 DBT

**Date:** 2026-08-08 · **Host:** Apple M2 Max, macOS 26.5.2, SDK Hypervisor 259.5.15
**Probe:** `probes/precise_exc/precise_exc.c` + `probes/precise_exc/payload.h`
(build `probes/build.sh precise_exc/precise_exc.c`; raw log `probes/precise_exc/run-all.log`
— that log is **one** snapshot; every range quoted below is across 3–5 independent process
invocations, which is what the DVFS drift documented in §8 requires)
**Source read for §6:** QEMU `master` as of today, fetched and read directly — `accel/tcg/translate-all.c`,
`accel/tcg/cputlb.c`, `tcg/tcg.c`, `tcg/region.c`, `target/i386/tcg/tcg-cpu.c`, plus QEMU `v1.0`
`translate-all.c` for the historical comparison.

> **Reading the labels.** `M` = measured on this machine today, in-VM at guest EL1.
> `D` = derived by subtracting one measured quantity from another. `X` = modelled/extrapolated —
> arithmetic on an `M` number, not an observation. `C` = cited from a primary source (SDM, ARM ARM,
> QEMU source, a paper). `?` = attempted and inconclusive.

---

## 0. Bottom line

### 0.1 The recommendation in one paragraph

**Carry no precise-exception cost on the fast path at all. Reconstruct on the fault path, but only
on the rare *guest-visible* fault, never on the common *shadow-miss* fault — they are different
events and the corpus has been conflating them.** Concretely: a two-tier fault handler whose tier 0
(shadow fill, `16 §2`: **145.8 ns `D`**) does no state reconstruction whatsoever and whose tier 1
(genuine guest `#PF`/`#GP`/…) adds **+28 ns `D`** of reconstruction; a per-block site table found by
a flat code-arena index rather than by binary search or linear scan; a single-committing-store
instruction shape that makes restartability free for the ~95 % of x86 instructions with at most one
memory write; explicit interpretation for the descriptor/far-transfer minority; and `AT S1E1WP`
used **only** where one probe amortises over hundreds of elements, never per-instruction.

### 0.2 The measured numbers this rests on

| # | Quantity | Cost | Tag |
|---|---|---|---|
| 1 | **Materialising 16 guest GPRs before an instruction** (8 `STP`, L1-resident context block) | **1.12 – 1.34 ns** | `M` |
| 2 | …as a fraction of a measured memory-touching translated x86 instruction (0.66–0.76 ns) | **+170 % … +241 %** (4 runs) | `D` |
| 3 | Materialising **RIP only** (1 store/instruction) | **below noise; ≤ 0.09 ns, ≤ 13 %** | `M` |
| 4 | Materialising RIP + 2 lazy-flag operands (3 stores) | +0.22 … +0.50 ns (**+34 … +76 %**, wide) | `D` |
| 5 | Marginal cost of one 8-byte store to a hot context block | 0.10 ns (few) → **0.22 ns** (16, store-port bound) | `M` |
| 6 | **Bare in-VM permission fault + `ELR+=4` + counter + `ERET`** | **29 – 43 ns, median ≈ 36 ns** | `D` |
| 7 | …**plus** spill/reload `x0–x17` + read `ELR`/`FAR`/`ESR` in the handler | **+1.7 ns (free)** | `D` |
| 8 | …**plus** permuting 16 host regs into the guest CPUState | **+0.3 ns (free)** | `D` |
| 9 | …**plus** a StackMap binary search, N = 256 / 4096 / 65536 | **+20 / +29 / +41 ns** | `D` |
| 10 | **Full strategy-(b) fault path, 4096-entry StackMap** | **57 – 67 ns, median 64 ns** | `M` |
| 11 | StackMap binary search **warm** (microbenchmark), N = 4096 | **7.0 ns** | `D` |
| 12 | …the same search **on the real fault path** (table cold) | **≈ 29 ns — 4.1× the microbenchmark** | `D` |
| 13 | Open-addressed **hash** lookup, N = 1 K … 64 K (warm) | **2.5 – 3.7 ns abs; 0.0 – 1.5 ns net** | `M` |
| 14 | **QEMU `cpu_unwind_data_from_tb`**: sleb128 linear scan, icount = 16 / 32 / 64 / 128 | **44–48 / 78–79 / 118–125 / 189–196 ns** | `M` |
| 15 | …asymptotic per-guest-instruction decode cost | **≈ 2.0 ns/row** | `D` |
| 16 | `AT S1E1WP` **alone**, no `ISB`, no `PAR_EL1` read | **8.6 – 10.0 ns** | `M` |
| 17 | `AT S1E1WP` + `ISB` + `MRS PAR_EL1` (a usable probe) | **22.5 – 24.6 ns** | `M` |
| 18 | **Probing 1 operand before a store**, net of the store | **+24.5 ns = 24–30× a bare `STR`** | `D` |
| 19 | **Probing 2 operands before a store**, net | **+45.1 … +45.7 ns = 43–55× a bare `STR`** | `D` |
| 20 | `AT` on a **present-but-read-only** page (probe fails, permission) | **20.6 – 20.8 ns** | `M` |
| 21 | `AT` on an **unmapped** page (probe fails, translation) | **40.6 – 42.5 ns — 2.05×** | `M` |
| 22 | `AT S1E1RP`/`S1E1WP` report `PAR_EL1.F=1` + correct `FST` **taking zero exceptions** | **verified** | `M` |
| 23 | Bare `STR` control (same address stream) | 0.81 – 1.19 ns | `M` |

Reference points from `16`: in-VM permission fault **28.2 ns**, translation fault **94.4 ns**, full
shadow fill without TLBI **145.8 ns**, single-page TLBI **279–285 ns**, bare `HVC` host exit
**808 ns**, measured host-side abort path **11.5 µs**.

### 0.3 The cost model, and the answer to "at what fault rate does metadata stop winning?"

Let `I` = translated x86 instructions retired per second, `T` = mean cost of one translated x86
instruction, `F_g` = **guest-visible** faults per second (the guest's own `#PF`/`#GP`/…), `F_s` =
shadow-miss faults per second. Anchors: `T = 0.70 ns` (`X`, from the measured 0.66–0.76 ns
memory-instruction body and 0.69 ns 4-ALU body), memory-operation density 40 % (`C`, `06 §2.3`).

| Strategy | Fast-path cost / instruction | Fault-path cost / **guest** fault | Whole-program overhead |
|---|---|---|---|
| **(a) materialise everything** | **+1.34 ns** (`M`) | 0 | **+191 %** (`X`) |
| **(a′) materialise before memory ops only** (40 %) | +0.54 ns (`X`) | 0 | **+77 %** (`X`) |
| **(b) StackMap / site table** | **≈ 0** (`M`, item 3) | **+28 ns** (`D`, item 12) | `F_g × 2.8e-8` |
| **(c) QEMU modern: sleb128 side table** | ≈ 0 | **+120 ns** for a 64-insn TB (`M`) + TB lookup | `F_g × 1.2e-7` + lookup |
| **(c′) QEMU ≤ 1.0: re-translate on fault** | ≈ 0 | **10–100 µs** (`X`, a full re-lift + re-codegen) | `F_g × 5e-5` |
| **(d) checkpoint/rollback** | ≈ 0 *if hardware existed* | — | **not available: no `FEAT_TME`, no gated store buffer** |

Setting each equal to (a′)'s +77 %:

| Crossover — the fault rate at which the strategy stops beating conservative spilling | Guest faults/s |
|---|---|
| **(b) site table** | **2.8 × 10⁷** |
| **(c) QEMU sleb128 scan, 64-insn TBs** | **6.4 × 10⁶** |
| **(c′) QEMU-1.0 re-translation @ 50 µs** | **1.6 × 10⁴** |

**Realistic guest fault rates are 10⁴ – 10⁶/s.** So:

> **(b) beats (a) by a factor of 30 – 3000 in margin, and the crossover is physically unreachable:
> at 2.8 × 10⁷ guest faults/s the shadow fills alone (146 ns each) would consume 4.1 seconds of CPU
> per second. (c) also wins, with ~4× less margin. (c′) — re-translation — genuinely loses at the
> top of the realistic range, which is presumably exactly why QEMU deleted it in 2011.**

At the fault rates that matter, strategy (b)'s whole-program precise-exception tax is:

| Guest faults/s | 10⁴ | 10⁵ | 10⁶ | 10⁷ |
|---|---|---|---|---|
| (b) site table, +28 ns | **0.03 %** | **0.28 %** | **2.8 %** | 28 % |
| (c) sleb128, +120 ns | 0.12 % | 1.2 % | 12 % | 120 % |
| (a′) spilling, for comparison | 77 % | 77 % | 77 % | 77 % |

`12` rank 4's model — *"10 k faults/s × 200 cyc = 0.06 %, 1 M × 200 cyc = 5.7 %"* — is **confirmed
in shape and roughly correct in magnitude**, though its "200 cycles" is a coincidence: the real
figure is 28 ns of *reconstruction* on top of a 36 ns fault, and the 200-cycle number was never
attached to a specific mechanism. `12`'s "**10–20 % vs 2–5×**" headline should be restated as
**"≤ 0.3 % vs +77 % … +191 %"**: metadata is an order of magnitude cheaper than `12` claimed, and
conservative spilling is at the *bottom* of `06`'s 2–5× range, not the middle.

### 0.4 The five findings that change the design

1. **The hot fault does not need precise state.** A shadow-miss fault is invisible to the guest: we
   fill and retry. Only a fault the guest itself would have taken needs RIP, GPRs, RFLAGS, CR2 and
   an error code. `06 §5` and `12` rank 4 both cost precise exceptions against *all* faults. Split
   the handler and the cost drops by whatever fraction of faults are shadow misses — which, in a
   demand-paging guest with a warm shadow, is most of them.
2. **Reconstructing registers in the handler is free; *finding the metadata* is the entire cost.**
   Spilling `x0–x17` and permuting 16 registers into the guest CPUState cost **+1.7 ns and +0.3 ns**
   — statistically indistinguishable from zero, because the exception entry has already flushed the
   pipeline and the stores retire in its shadow. The **lookup** costs +20…+41 ns. Optimise the
   lookup; ignore the reconstruction.
3. **A warm microbenchmark of the lookup understates it by 4×.** The same 4096-entry binary search
   is 7.0 ns in a hot loop and ≈ 29 ns on the real fault path, because on the fault path the table
   is cold. This is exactly the error `13 §B-6` caught in `12` rank 1, in a new place. **Any future
   "the lookup is only N ns" claim must be measured on the fault path, not in a loop.**
4. **`AT`-based probe-then-execute is 25–55× a plain store and must not be used per instruction.**
   `06 §5.3(a)` and `06 §13(9)` promote `AT S1E1RP/WP` as *the* answer for multi-operand
   instructions. Measured: one probe is **+24.5 ns**, two are **+45.7 ns**, against a 0.9 ns store.
   `06`'s "~10–20 cycles" is wrong by 4–8× (`16 §6` already said so) and the *design consequence* —
   that probing is affordable per-instruction — is wrong outright. Probing is affordable only when
   amortised over ≥ 100 elements (`REP` chunking) or gated behind a page-crossing test that is
   almost never taken.
5. **`16`'s "present-but-restricted rather than invalid" lever is confirmed by a second, independent
   mechanism.** `16 §1` measured permission faults at 3.3× cheaper than translation faults. This
   probe measures **`AT` probes at 2.05× cheaper on a present-but-read-only page (20.7 ns) than on
   an unmapped one (42.5 ns)** — same architectural cause (invalid descriptors can never be
   TLB-cached and must re-walk, nested through stage 2), different instruction. The lever applies to
   the probe path as well as the fault path.

### 0.5 The invariant

> **Every future optimisation must preserve this, and nothing else is required:**
>
> **At every host instruction inside a translated block, the complete pre-instruction x86
> architectural state of the x86 instruction currently in flight must be recoverable from
> `(host GPRs ∪ host NEON ∪ guest CPUState ∪ siteTable[host_pc])`, and no architecturally visible
> guest state — no GPR, no flag, no memory — may have been modified since that x86 instruction
> began, except by a single final committing store.**
>
> Two corollaries are load-bearing and should be assertions in the IR, not conventions:
> **(i) at most one committing store per translated x86 instruction** — more requires a probe, a
> split, or interpretation; **(ii) all guest register writes are ordered after all guest memory
> reads** ("load-all → compute → store-all").

---

## 1. What exactly must be recoverable, per x86 fault class

`C` throughout: Intel SDM Vol.3A **Ch. 6** — §6.5 and Table 6-1 (classification), §6.12.1 (the
exception stack frame), §6.13 (error codes), §6.15 (per-vector descriptions); Vol.3A §4.7 (`#PF`
error code and `CR2`); Vol.2B (`REP` restart semantics).

### 1.1 The three classes

| Class | Saved CS:RIP | Saved RFLAGS | Restartable | Vectors |
|---|---|---|---|---|
| **Fault** | address of the **faulting instruction**, including all its prefixes | the **pre-instruction** RFLAGS, with `RF` set (see below) | yes — the instruction re-executes from the top | `#DE`, `#UD`, `#NM`, `#PF`, `#GP`, `#SS`, `#NP`, `#TS`, `#BR`, `#AC`, `#XM`, `#CP`, instruction-breakpoint `#DB` |
| **Trap** | address of the **next instruction** | the **post-instruction** RFLAGS | n/a — the instruction completed | `#BP` (INT3), `#OF` (INTO), `INT n`, single-step and data-breakpoint `#DB` |
| **Abort** | imprecise; no guarantee | imprecise | no | `#DF`, `#MC` |

### 1.2 What each register must contain

**On a fault**, at the moment the exception frame is pushed:

- **All 16 GPRs** hold their values *as of the instruction boundary before* the faulting
  instruction. Nothing the faulting instruction computed is visible.
- **RIP** = the address of the **first byte of the faulting instruction, including prefixes.** This
  matters for `REP`-prefixed strings, for `LOCK`-prefixed RMW, and for the `MOV SS` / `POP SS`
  interrupt shadow. A translator that records "guest RIP" as the address after the prefixes will
  produce a guest that re-executes the instruction *without its prefix* on `IRET` — a silent
  corruption with an hours-long incubation period.
- **RSP** unchanged by the faulting instruction. On a *same-privilege* fault the frame is pushed on
  the current stack; on a **ring 3 → ring 0** fault, `SS:RSP` are loaded from the TSS (`RSP0`, or
  `IST[n]` if the IDT gate specifies one) and the *old* `SS:RSP` are pushed in the frame. In 64-bit
  mode the frame is always 5 quadwords (`SS`, `RSP`, `RFLAGS`, `CS`, `RIP`) even for same-privilege
  faults — `06 §4.3` already records this and it is the reason `IRET` always pops five.
- **RFLAGS** must be **completely architecturally correct**, including `PF` and `AF`, because it is
  pushed into the frame and the guest can read it. This is the single hardest constraint on lazy
  flags and §5.1 deals with it.
  - **`RF` (bit 16)**: on most faults the pushed RFLAGS has `RF = 1`, so that when the handler
    `IRET`s back to the faulting instruction, an instruction breakpoint on that address does not
    re-trigger. Getting this wrong produces an infinite `#DB` loop the moment a guest debugger sets a
    breakpoint. `#DB` itself, `#BP` and `#OF` do not set `RF`.
  - **`TF` (bit 8)** is cleared in the *live* RFLAGS on entry (interrupt gates also clear `IF`), but
    the *pushed* copy retains its pre-exception value.
- **CS** must have the correct selector *and* correct hidden descriptor-cache contents (`L`, `D`,
  DPL) — in 64-bit mode the base is 0 and the limit unchecked, so only the attribute bits matter.
- **CR2** = the faulting **linear** address for `#PF`, not the address of the paging structure. Our
  handler gets this from `FAR_EL1` — which is exactly right, since the shadow tables use the guest's
  own linear addresses.

**On a trap**, everything above holds except that CS:RIP points at the *next* instruction and the
architectural state is the *post*-instruction state. `#DB` is both: instruction breakpoints and the
`RF`-related cases are faults; single-step (`TF`), data breakpoints and the task-switch `#DB` are
traps. `DR6` must be set to reflect which condition fired, and — a classic emulator bug — `DR6` bits
are **sticky**: the CPU sets them and never clears them; only software does.

**On an abort**, nothing is guaranteed. We do not generate `#MC` at all (`06 §4.3`), and `#DF` we
generate synthetically with a zero error code and no fidelity requirement on the interrupted state
beyond what a guest can observe (nothing useful).

### 1.3 Error codes

| Vector | Error code | Contents |
|---|---|---|
| `#PF` (14) | yes | `P`(0) `W/R`(1) `U/S`(2) `RSVD`(3) `I/D`(4) `PK`(5) `SS`(6) `SGX`(15) |
| `#GP`(13) `#SS`(12) `#NP`(11) `#TS`(10) | yes | selector-style: `EXT`(0) `IDT`(1) `TI`(2) + index(3:15); **0** if the fault is not selector-related, which is the overwhelmingly common `#GP` case |
| `#AC`(17) | yes | always **0** |
| `#DF`(8) | yes | always **0** |
| `#CP`(21) | yes | control-protection error code |
| `#DE #DB #BP #OF #BR #UD #NM #MF #XM #VE` | **no** | — |

**Where each `#PF` error-code bit comes from in our design** (`X`, the mapping is forced by the
architecture but the handler is not yet written):

| Bit | Source |
|---|---|
| `P` (present) | the **software x86 walk** the tier-0 handler already performed — not from `ESR_EL1` |
| `W/R` | `ESR_EL1.WnR` (bit 6) for data aborts; **but** see §2.6 — `WnR` is unreliable for some instruction classes and the translator must supply it |
| `U/S` | the EL the abort was taken from (`ESR_EL1.EC` 0x24 = lower EL = ring 3, 0x25 = current EL = ring 0), cross-checked against the guest's shadow `CPL` |
| `RSVD` | the software walk's reserved-bit check (depends on `MAXPHYADDR`, `EFER.NXE`, `CR4.PSE` — `06 §2.1`) |
| `I/D` | `ESR_EL1.EC` = 0x20/0x21 (instruction abort) vs 0x24/0x25 (data abort) |
| `PK`, `SS`, `SGX` | zero — we do not advertise PKU, CET shadow stacks, or SGX |

Note the asymmetry: **`P` and `RSVD` cannot come from the ARM fault at all**, because the ARM fault
only tells us our *shadow* was inadequate. Every genuine `#PF` therefore *requires* the software x86
walk to have run — which the tier-0 handler ran anyway. This is why splitting the handler costs
nothing: tier 1 starts from tier 0's completed walk.

### 1.4 The one thing precise exceptions require that is easy to forget

**Interrupts.** They are not faults, but they are subject to the same recoverability requirement,
except that they are only *taken* at instruction boundaries. `06 §4.3` and `03 §2.2` both give the
right answer: **do not carry per-instruction metadata for asynchronous events.** Latch the pending
interrupt in a flag, and let the current translated block run to its end (or to the next backward
branch) before delivering. HyperMAMBO-X64 states this explicitly (`03 §2.2`, verbatim): *"catch the
interrupt, resume the fragment with interrupts disabled until the end of the fragment, then recover
state from the fragment-linking metadata."* Costs nothing and removes an entire class of metadata.

Two x86 quirks constrain where the boundary may be: the **`STI` one-instruction shadow** and the
**`MOV SS`/`POP SS` shadow**, both of which suppress interrupt (and `#DB`) recognition for exactly
one instruction. A block boundary must never be placed inside a shadow.

---

## 2. The genuinely hard instructions

### 2.1 The taxonomy that actually matters

Restartability is only hard when an instruction can produce an architecturally visible effect
*before* it can still fault. Sorting x86-64 by that criterion:

| Class | Population | Restartable how | Fast-path cost |
|---|---|---|---|
| **A. ≤ 1 memory read, no memory write** | huge | free — nothing committed | **0** |
| **B. ≤ 1 memory read + exactly 1 memory write, write last** | huge | free — "load-all → compute → store-all" | **0** |
| **C. Read-modify-write to the *same* address, `LOCK`ed** | common | free — lower to **one** LSE atomic | **0** |
| **D. Multi-write to a contiguous ≤ 64-byte range** (`PUSH`, `POP`, `PUSHA`, `ENTER`, `LEAVE`, `CALL`, `IRET` frame) | common | page-crossing test, then probe | **~1 predicted-not-taken branch** |
| **E. Unaligned access crossing a page boundary** | rare at runtime, unprovable statically | page-crossing test, then split + probe | **~1 predicted-not-taken branch** |
| **F. `REP MOVS/STOS/CMPS/SCAS/LODS`** | hot in kernels | architectural induction variables, or chunk-with-probe | **0 (naive) / amortised probe (fast)** |
| **G. Descriptor and far-transfer ops** (`MOV Sreg`, `LSS/LDS/LES`, far `JMP`/`CALL`/`RET`, `IRET` with privilege change, `LGDT`/`LIDT`/`LLDT`/`LTR`, `SYSRET` edge cases) | rare | **interpret** | 0 on the fast path (they are not on it) |
| **H. `CMPXCHG16B`, `XSAVE`/`XRSTOR`, `FXSAVE`/`FXRSTOR`** | rare | `CASPAL` / probe / interpret | 0 |

**Classes A, B and C are ~95 % of dynamic x86-64 instructions and are restartable at literally zero
fast-path cost.** That is the whole result of this section, and it is why `06 §5`'s difficulty
rating of 5 is about the *global discipline*, not about per-instruction machinery.

### 2.2 `REP MOVS`/`STOS`/`SCAS` — partial completion

`C`, SDM Vol.2B, `REP` description: string operations with a repeat prefix are **interruptible and
restartable**. On a fault or interrupt mid-string, `RCX` holds the count of iterations *not yet
performed*, `RSI`/`RDI` point at the *next* element, and `RIP` points at the **first prefix byte** of
the string instruction so that `IRET` resumes the loop. `DF` selects the direction. `REP` with
`RCX = 0` performs nothing but still advances `RIP`.

Two real hazards:

1. **RIP must include the prefixes.** A `REP` instruction can carry a segment override and an
   address-size override in addition to `F3`. If the recovered RIP points past any of them, the
   resumed instruction is a *different instruction*. Historically this has been an erratum class on
   real silicon; it is trivially a bug class in an emulator.
2. **The `RCX`/`RSI`/`RDI` update ordering.** They must be consistent with the elements actually
   transferred, including the case where the fault happens on the *load* of iteration *k* (nothing
   of iteration *k* committed) versus on the *store* of iteration *k* (the load happened but is not
   architectural, so still nothing committed).

**Cheap correct translation** (`06 §5.3` already has this): emit an explicit loop whose induction
variables *are* the architectural `RCX`/`RSI`/`RDI`, updated at the top of each iteration. A fault
mid-loop is then precise for free. The cost is that the counters cannot live in scratch registers —
but under design (B) with static register allocation they are in fixed ARM registers anyway, so the
"cost of correctness" `06` describes **does not exist here**. It exists in QEMU because TCG keeps
guest registers in memory; it does not exist in an SRA design.

**The fast version, and this is where `AT` earns its place.** Chunk the loop: before transferring
*k* elements, prove that the whole *k*-element run lies within one already-present, already-writable
page. One `AT S1E1WP` on the last byte of the chunk plus a cheap arithmetic check on the first is
sufficient. Measured amortisation:

| Chunk | Probe cost/element (`X`, on the measured 24.5 ns) |
|---|---|
| k = 8 | 3.06 ns — **worse than not chunking** |
| k = 64 | 0.38 ns |
| k = 512 (a full 4 KiB page of bytes) | **0.048 ns** |
| k = 4096 (a page of bytes, `MOVSB`) | 0.006 ns |

So: chunk at page granularity, probe once per page, and the probe disappears. Below ~64 elements,
don't chunk. This is a concrete, measured design rule that replaces `06 §5.3`'s vaguer suggestion.

### 2.3 Unaligned accesses crossing a page boundary

The hazard is specific and it is a genuine ARM/x86 divergence:

- **x86** treats an unaligned access spanning two pages as a single instruction that faults with
  **no** partial side effect if either page is inaccessible. Real hardware pre-checks both pages.
- **ARM** permits an unaligned access crossing a page boundary to have written the first page before
  faulting on the second (ARM ARM: the access is not required to be single-copy atomic across the
  boundary, and abort behaviour for the unwritten part is `CONSTRAINED UNPREDICTABLE`).

So a plain `STR` of a guest 8-byte store at guest VA `…FFC` is **not** a correct translation when the
next page is absent.

**QEMU's answer**, read directly from `accel/tcg/cputlb.c:1730-1793` (`C`):

```c
    mmu_lookup1(cpu, &l->page[0], l->memop, l->mmu_idx, type, ra);
    last = addr + l->page[0].size - 1;
    crosspage = (addr ^ last) & TARGET_PAGE_MASK;
    if (likely(!crosspage)) { ... } else {
        ...
        /* Lookup and recognize exceptions from the second page. */
        if (mmu_lookup1(cpu, &l->page[1], 0, l->mmu_idx, type, ra)) { ... }
```

It resolves **both** pages — raising any exception — before performing **any** part of the access.
That is probe-then-execute, and in QEMU it is free because the softmmu TLB lookup was needed anyway.

**Our answer.** We have no TLB lookup to piggyback on, so the check has to be explicit:

```
    eor  xt, xaddr, xlast          // xlast = xaddr + size - 1, usually a constant fold
    lsr  xt, xt, #12
    cbnz xt, Lcross                // essentially never taken
    str  xdata, [xaddr]
```

Two ALU ops and one predicted-not-taken branch. For a random 8-byte address the branch is taken with
probability 7/4096 ≈ 0.17 %, so it costs ~1 cycle of front-end and no mispredicts. Measured proxy
from item 5: the 4-ALU baseline loop runs at 0.69 ns for four independent ALU ops, so two more are
worth roughly 0.1–0.2 ns — comparable to the 0.22 ns of one extra store, and **an order of magnitude
cheaper than the 24.5 ns probe it replaces on the common path**.

Emit the check only where alignment cannot be proven. Compilers align aggressively, and for
stack-relative accesses with a provably 8- or 16-aligned `RSP` the translator can drop it entirely.

`Lcross` then does the honest thing: two `AT S1E1WP` probes (45.7 ns `D`), then either the split
access or a synthesised `#PF` on whichever page failed. At a 0.17 % taken rate the amortised cost is
**0.08 ns/access** (`X`).

### 2.4 `PUSH`/`POP`, `PUSHA`/`POPA`, `ENTER`, `LEAVE`, `CALL`, `IRET`

`06 §5.3` states the constraint correctly for `PUSH [mem]`: the load may succeed and the store may
`#PF`, and `RSP` must be unchanged. The general solution is the same discipline as everything else:

```
    // PUSH [mem]:
    ldr   xtmp, [xea]              // may fault -> nothing committed
    sub   xnew, xrsp, #8           // scratch, NOT the architectural RSP
    str   xtmp, [xnew]             // may fault -> RSP still unchanged
    mov   xrsp, xnew               // commit
```

`RSP` is written *after* the last faulting access. Cost: zero — the `sub` was needed anyway.

**Multi-store forms** (`PUSHA` = 8 stores, `ENTER` with a nesting level = up to 32, `CALL` far, `IRET`
building a 5-quadword frame) are class D. All of them write a **contiguous range of at most ~64
bytes anchored at RSP**, so the only way one of them straddles a page boundary is if `RSP` is within
64 bytes of a page edge. The same 2-instruction page-crossing test covers the whole group:

```
    sub  xlo, xrsp, #TOTAL
    eor  xt, xlo, xrsp
    lsr  xt, xt, #12
    cbnz xt, Lslow                 // probe both pages, then commit
```

The subtlety worth naming: even when the range is on one page, `PUSHA`'s stores 1–4 committing before
store 5 faults *is* an architectural side effect (memory below `RSP` is ordinary memory the guest can
read). Because the page-crossing test proves all eight stores hit the same page and that page's
permission was already checked by store 1, store 5 cannot fault for a *different* reason than store 1
would have. **The test is therefore not merely an optimisation, it is the correctness argument.**
(`PUSHA`/`POPA` do not exist in 64-bit mode; this matters only for 32-bit compat, which `06 §3.2`
says we must support.)

### 2.5 `LOCK`ed RMW that faults on the write after the read

This is the case people fear, and on ARM it is **free**, provided one rule is followed:

> **Never lower a `LOCK`-prefixed operation to a load-modify-store sequence. Always lower it to a
> single LSE atomic** (`LDADDAL`, `LDSETAL`, `LDCLRAL`, `LDEORAL`, `SWPAL`, `CASAL`, `CASPAL`).

A single LSE atomic against a read-only or absent page raises a permission/translation abort with
`ESR_EL1.WnR = 1` and performs **nothing**. There is no intermediate state to recover. `06 §7.4`
already maps `LOCK` ops to LSE atomics for *ordering* reasons; the precise-exception argument is
independent and equally strong.

Interaction with the shadow-D-bit scheme (`06 §2.5`): shadow entries for guest-writable pages are
installed **present but read-only** until the first write, so the first `LOCK` RMW to any page takes
a permission fault. That is **28.2 ns** (`16 §1`) and it happens once per page per epoch. Correct,
and cheap — and it is exactly the behaviour real x86 needs, since x86 sets `D` before performing a
locked RMW.

`CMPXCHG16B` → `CASPAL`: both require 16-byte alignment and both `#GP`/fault otherwise, so the
requirements coincide exactly (`06 §7.3`). Free.

The one genuinely unsolvable case is the **split lock** — a `LOCK`ed RMW crossing a cache line. ARM
has no instruction for it. `06 §7.3`'s vCPU-rendezvous answer is the only correct one and it is also
the only one that is *precise*: park all other vCPUs, then perform the RMW non-atomically with full
knowledge that no one can observe an intermediate state. ~10–50 µs, frequency near zero.

### 2.6 Descriptor and segment loads — interpret them

`MOV DS, ax`, `LSS`, far `CALL`/`JMP`/`RET`, `IRET` with a privilege change, `LGDT`/`LIDT`/`LLDT`/`LTR`,
task-related operations: each performs multiple memory reads (GDT/LDT descriptor, possibly a TSS
read), multiple validity and privilege checks in a specified order with distinct error codes, and
updates a hidden descriptor cache. Making them restartable in generated code would require probing
every access and carefully ordering every check.

**Do not generate code for them. Interpret them.** They are cold (`06 §3.3`: call gates and task
switching are unused by every mainstream OS; segment loads outside 32-bit compat mode are rare), the
interpreter is precise at every boundary by construction, and this is exactly Transmeta's terminal
adaptive-retranslation state — `03 §3.1`, verbatim: *"Genuine recurring faults cause the region to be
narrowed around the faulting instruction, ultimately to a zero-instruction translation that calls the
interpreter for that one instruction."* We can apply the same policy **statically**, at translation
time, for a known instruction list, and dynamically for anything that turns out to fault repeatedly.

### 2.7 A caveat on `ESR_EL1.WnR`

`ESR_EL1.WnR` distinguishes read from write aborts, and we need it for the `#PF` error code's `W/R`
bit. It is **not reliable for every instruction class** — for cache-maintenance operations and for
some atomic forms the architecture defines `WnR` in ways that do not match "the guest instruction was
a write". Since the translator knows statically whether the x86 instruction it emitted was a read, a
write, or an RMW, **the `W/R` bit should come from the site table, not from `ESR_EL1`.** One more
field in the site-table entry; zero fast-path cost. This is a correctness detail that will not show
up in testing until a guest checks `#PF` error codes, which `kvm-unit-tests` does.

---

## 3. The strategy space, costed against the measured budget

### 3.1 (a) Conservative materialisation

Spill every architectural register to the guest CPUState before anything that can fault.

**Fast-path cost — measured, and it is the best case.** All stores in this probe hit a single
128-byte L1-resident context block, the baseline loop has spare store-port slots, and no account is
taken of the register-allocation pressure the CPUState pointer and the spill code steal. Every figure
below is therefore a **lower bound**.

```
  4 ALU ops (a stand-in for one simple translated x86 insn)   0.69 ns
    + 16 registers spilled as 8 STP                           2.03 ns   -> +1.34 ns
    + 16 registers spilled as 16 STR                          4.18 ns   -> +3.49 ns
    + 8 STP then 8 LDP (spill AND reload)                     3.37 ns   -> +2.68 ns
  load + ALU + store (a memory-touching translated x86 insn)  0.66 ns
```

The marginal cost of a store saturates at **0.22 ns** once there are more than four — the store port,
not the cache, is the limit. Using `STP` halves it to **0.084 ns/register**.

**Whole-program:** +1.34 ns on a 0.66–0.70 ns instruction is **+191 %** if applied everywhere,
**+77 %** if applied only before memory operations (40 % density). Measured directly against the
memory-instruction body across four runs the figure is **+170 % … +241 %**. `06 §5.2`'s "easily
2–5×" is therefore **confirmed at the bottom of its range** — 2.7–3.4× for the naive version — and
that is with every advantage granted to the spilling implementation.

**What it permits:** everything. Any reordering, any register allocation, any fusion, because the
architectural state is in memory at every fault point. This is genuinely why it is tempting.

**What it forbids:** nothing, and that is the point — it converts a *correctness* problem into a
*throughput* problem, and the throughput problem is 2–3×.

**Complexity:** trivial. One emitter routine. This is the fallback if the site-table infrastructure
ever proves unworkable, and it should be kept as a build flag *specifically as a differential-testing
oracle* (§7).

**QEMU is (a), for registers.** This surprised me and it is worth stating precisely, because the task
framing (and the corpus) treats QEMU as the metadata/re-translation exemplar. From `tcg/tcg.c:5416`
(`C`, verbatim):

```c
        if (def->flags & TCG_OPF_SIDE_EFFECTS) {
            /* sync globals if the op has side effects and might trigger
               an exception. */
            sync_globals(s, i_allocated_regs);
        }
```

and the matching liveness rule at `tcg/tcg.c:4319-4323`:

```c
            } else if (def->flags & TCG_OPF_SIDE_EFFECTS) {
                /* Like reading globals: sync_globals */
                call_flags = TCG_CALL_NO_WRITE_GLOBALS;
```

Every TCG op that might fault — every `qemu_ld`/`qemu_st`, every helper call — forces all *globals*
(which is where the guest's GPRs live) back to their canonical memory location in `CPUArchState`
first. QEMU pays strategy (a) for registers on the fast path. What it *doesn't* materialise is the
guest PC and the lazy-flag selector, and that is what the side table is for (§3.3).

### 3.2 (b) Metadata / StackMap

Emit, for each host PC that can fault, a description of where the guest state lives. Reconstruct only
on a fault.

**Fast-path cost — measured at approximately zero.** The only thing that could cost anything is
materialising RIP, and:

```
  load + ALU + store (a memory-touching translated x86 insn)  0.658 / 0.761 / 0.659 ns
    + 1 store (RIP materialised)                              0.652 / 0.765 / 0.575 ns
```

The difference is **below the noise floor**: −12.7 %, +0.6 %, −0.9 % across three runs. `06 §5.2(2)`
says *"Do not materialize RIP before each instruction (1 store per instruction ≈ 10–20 % alone)"* —
**that estimate is 3–10× pessimistic on this hardware**, because the store retires into spare
store-port bandwidth that a 3-instruction translated body is not using. The conclusion (derive RIP
from a side table) is still right, but the *reason* is code size and I-cache footprint, not cycles.

The genuine fast-path cost of (b) is therefore **space, not time**: the site table. `06 §5.2`
estimates 0.5–1.5× the generated code size. That is a real I-cache/memory concern but not a
throughput one, and it is entirely off the hot path.

**Fault-path cost — measured end to end.** The probe implements a realistic handler: spill `x0–x17`
to a stack frame (9 `STP`), read `ELR`/`FAR`/`ESR`, branchless binary search of an N-entry table
keyed on `(ELR ^ FAR>>12)`, permute 16 spilled host registers into the guest CPUState (8 `LDP`+`STP`
pairs), write RIP/CR2/error-code, reload `x0–x17`, `ERET`. Medians over 5 independent process
invocations, each configuration bracketed by a fresh minimal-handler baseline run immediately before
it:

| Handler | Absolute (median of 5) | Δ vs adjacent minimal baseline |
|---|---|---|
| minimal: `ELR+=4`; counter; `ERET` | 30.0 – 43.5 ns, med ≈ 36 | — |
| + spill/reload `x0–x17`, read `ELR`/`FAR`/`ESR` | 36.6 – 41.8 ns | **+1.7 ns** |
| + permute 16 regs into the guest CPUState | 38.6 – 44.4 ns | **+0.3 ns** |
| + StackMap binary search, N = 256 (8 steps) | 46.0 – 58.1 ns | **+20.2 ns** |
| + StackMap binary search, N = 4096 (12 steps) | 57.1 – 66.7 ns | **+28.9 ns** |
| + StackMap binary search, N = 65536 (16 steps) | 71.2 – 76.4 ns | **+40.8 ns** |

Two results here are worth more than the headline:

**(i) Register reconstruction is free.** +1.7 ns to spill and reload 18 host registers, +0.3 ns to
permute 16 of them into the CPUState. The exception entry has already serialised the machine; the
stores retire in its shadow. `06 §5.2`'s worry about reconstruction cost is misplaced — it is the
*search* that costs.

**(ii) The search costs 4× more on the fault path than in a microbenchmark.** The identical
4096-entry branchless binary search measures **7.0 ns** in a hot loop (§3.2 microbenchmark below) and
**≈ 29 ns** inside the fault handler. Cause: on the fault path the table is cold, because between
faults the guest touches thousands of other addresses. This is the same methodological trap
`13 §B-6` identified in `12` rank 1, in a new place, and it should be a standing rule:
**metadata-lookup costs must be measured on the fault path.**

**Microbenchmark, for comparison and for the design of the lookup** (all `M`, net of the measured
2.71–2.74 ns xorshift key generation):

| Structure | N=64 | 256 | 1024 | 4096 | 16384 | 65536 | 262144 |
|---|---|---|---|---|---|---|---|
| branchless binary search (net) | 1.5–2.0 | 3.3–3.5 | 5.2–5.6 | 6.9–7.3 | 7.8–9.0 | **18.1–18.3** | 23.5–26.2 ns |
| …per step | 0.25–0.33 | 0.41–0.44 | 0.52–0.56 | 0.58–0.61 | 0.56–0.64 | **1.13–1.14** | 1.30–1.46 ns |
| open-addressed hash (net) | — | — | 0.0–0.4 | — | — | **0.6–1.5** | — ns |
| open-addressed hash (absolute) | — | — | 2.5 | — | — | **3.3–3.7** | — ns |

The per-step cost is flat at ~0.6 ns while the table fits L1/L2 and doubles at N = 65536 (a 256 KiB
table) when the search's early steps start missing. **A hash is 10–25× cheaper than a binary search
at large N** because it is one dependent load instead of log₂N.

**What (b) permits:** cross-block register allocation (each site records the current assignment),
lazy flags (the tuple's location is in the map), reordering of anything that writes no architectural
state, instruction fusion where the fused unit still has ≤ 1 committing store. **What it forbids:**
discarding any input to the live lazy-flag tuple across a fault point; committing more than one store
per x86 instruction without a probe; any reordering that moves an architectural write before a
faulting access.

**Complexity: high, and it is *global*.** `06 §12` Risk 1 is right that this is the real danger:
every future optimisation must preserve the invariant, and violations produce silent guest corruption
under memory pressure. §7 is the answer.

### 3.3 (c) Re-translation on fault — and what QEMU actually does

**The task framing says this is QEMU's approach. It was, until 2011. It is not any more, and the
reason it was abandoned is exactly the cost model in §0.3.**

**QEMU v1.0** (`translate-all.c:111`, fetched and read, `C`):

```c
int cpu_restore_state(TranslationBlock *tb, CPUState *env, unsigned long searched_pc)
{
    ...
    tcg_func_start(s);
    gen_intermediate_code_pc(env, tb);          /* RE-LIFT the whole block */
    ...
    j = tcg_gen_code_search_pc(s, (uint8_t *)tc_ptr, searched_pc - tc_ptr);
                                                /* RE-CODEGEN, stopping at the host PC */
    ...
    restore_state_to_opc(env, tb, j);
#ifdef CONFIG_PROFILER
    s->restore_time += profile_getclock() - ti;
    s->restore_count++;
#endif
```

A full guest-instruction re-decode plus a full TCG code-generation pass, per fault. QEMU had
dedicated profiler counters for it (`restore_time`, `restore_count`), which is what you build when
something is expensive enough to need watching.

**QEMU master** (`accel/tcg/translate-all.c:100-194`, `C`) replaced it with a per-TB side table.
`encode_search()` emits, for each guest instruction in the TB, `INSN_START_WORDS` (= 3, from
`include/tcg/insn-start-words.h`) sleb128-encoded deltas plus one sleb128 host-offset delta, seeded
from `{tb->pc, 0…, tb->tc.ptr}`. `cpu_unwind_data_from_tb()` **linearly decodes from the start of the
TB** until the accumulated host PC passes the faulting one:

```c
    for (i = 0; i < num_insns; ++i) {
        for (j = 0; j < INSN_START_WORDS; ++j) {
            data[j] += decode_sleb128(&p);
        }
        iter_pc += decode_sleb128(&p);
        if (iter_pc > host_pc) {
            return num_insns - i;
        }
    }
```

and for x86 (`target/i386/tcg/tcg-cpu.c:85`, `C`) the recovered state is exactly **two things**:

```c
static void x86_restore_state_to_opc(CPUState *cs, const TranslationBlock *tb,
                                     const uint64_t *data)
{
    int cc_op = data[1];
    ...
    env->eip = new_pc;
    ...
    if (cc_op != CC_OP_DYNAMIC) {
        env->cc_op = cc_op;
    }
}
```

`eip` and `cc_op` — the lazy-flag selector. **The GPRs are not restored because they were never in
registers**: `sync_globals` put them in `CPUArchState` before the faulting op (§3.1). So modern QEMU
is **(a) for registers + a side table for PC and lazy flags**, and the side table is decoded by
linear scan, not binary search.

**Measured cost of that linear scan**, implemented byte-for-byte to QEMU's encoding in the guest and
timed in-VM (`M`, target = the middle instruction of the TB, so `icount/2 + 1` rows are decoded):

| TB icount | table bytes | rows walked | cost |
|---|---|---|---|
| 4 | 17 | 3 | 16.3 – 18.8 ns |
| 8 | 35 | 5 | 24.6 – 28.4 ns |
| 16 | 73 | 9 | 43.9 – 47.8 ns |
| 32 | 149 | 17 | 78.5 – 79.0 ns |
| **64** | 295 | 33 | **118 – 125 ns** |
| **128** | 578 | 65 | **189 – 196 ns** |

Asymptotically **≈ 2.0 ns per guest instruction decoded** (`D`, from the 64→128 delta); more at small
counts where the data-dependent `tbnz` continuation branches mispredict.

**And that is not the whole fault path.** `cpu_restore_state` first calls `tcg_tb_lookup(host_pc)`,
which is (`tcg/region.c:248`, `C`) a **mutex acquisition plus a balanced-tree lookup**:

```c
    qemu_mutex_lock(&rt->lock);
    tb = q_tree_lookup(rt->tree, &s);
    qemu_mutex_unlock(&rt->lock);
```

I did not measure this (`?`); an uncontended mutex round trip plus a red-black-tree descent over
10⁴–10⁵ TBs is plausibly another 20–100 ns, and it *serialises* fault handling across vCPUs.

**Verdict on (c).** A **64-instruction TB costs 118–125 ns of pure state reconstruction — comparable
to the entire 145.8 ns in-VM shadow fill.** Compared to (b)'s measured 28 ns for a cold binary search
over 4096 entries, QEMU's scheme is **4.3× more expensive** and it gets *worse* as blocks get longer,
which is precisely the direction an optimising translator wants to go. QEMU pays it because a linear
scan needs no per-TB index structure and TCG blocks are short; we should not.

**Does (c) compose with aggressive optimisation?** Modern QEMU's version composes with *nothing* —
it recovers only PC and `cc_op`, and it works only because `sync_globals` has already done strategy
(a) for the registers. The v1.0 version composed with nothing either, for a subtler reason: **it
required code generation to be deterministic and replayable.** Any profile-guided, adaptive, or
randomised codegen decision breaks re-translation, and so does any optimisation whose input state
(e.g. a speculated invariant that has since been invalidated) is no longer available. This is a
structural argument, not a performance one, and it is the deeper reason to reject (c′).

### 3.4 (d) Checkpoint / rollback — Transmeta CMS, and why we cannot have it

This is the strongest precedent in the literature and the only design that kept aggressive reordering
*and* precise faults in shipping silicon. It is worth stating the mechanism concretely because the
parts that are software transfer to us verbatim.

**The hardware** (`03 §3.1`, `C`, from Dehnert et al., *The Transmeta Code Morphing Software: Using
Speculation, Recovery, and Adaptive Retranslation to Address Real-Life Challenges*, CGO 2003,
DOI 10.1109/CGO.2003.1191529):

1. **Shadowed register file + `commit`/`rollback`.** *"Every register holding x86 state exists twice
   (working + shadow). Atoms write only the working copy; `commit` at translation end copies
   working→shadow; any fault triggers `rollback`. Commit is 'effectively free'; rollback 'costs less
   than a couple of branch mispredictions.'"*
2. **Gated store buffer.** *"Stores held until commit; dropped on rollback. Memory is checkpointed
   alongside registers."* — this is the piece that makes the whole thing work and the piece with no
   ARM analogue.
3. **Alias-detection hardware.** The translator marks load/store atoms protected/protecting; hardware
   verifies at runtime that reordered references did not overlap. *"Explicitly simpler than IA-64's
   ALAT — no fully-associative table, no hardware replacement policy, because the translator names
   the entries."*
4. **I/O-reordering trap.** Each memory atom carries a "I was reordered w.r.t. x86 program order"
   bit; hardware raises an exception if such an atom touches an I/O page.
5. **Sub-page (fine-grain) write protection** with a hardware cache and a software miss handler.

**The precise-exception mechanism** (`03 §3.1`, verbatim): *"The scheduler hoists faulting atoms
freely. On any native fault: **rollback → re-execute the region's x86 instructions in the
interpreter**, which is precise at every boundary. If the interpreter does not reproduce the fault,
it was a speculation artifact and is discarded. Interrupts use the same path, so translations never
reason about interruptibility."*

That last clause is the design's real elegance: because rollback restores memory as well as
registers, and because the interpreter is the sole source of precise state, **the translated code
never carries any recovery metadata at all** and the optimiser is completely unconstrained inside a
region of up to 200 x86 instructions.

**Adaptive retranslation after a rollback** (verbatim): *"Genuine recurring faults cause the region
to be narrowed around the faulting instruction, ultimately to a zero-instruction translation that
calls the interpreter for that one instruction. Policies accumulate monotonically on a translation to
prevent oscillation between two translations that each fix one problem."*

**What it was worth**, from CMS's own ablations (`03 §3.1`/`§6.5`, `C`): suppressing all memory
reordering — i.e. running strictly precise — cost **mean 10.09 % on OS boots** (WinME 26.08 %) and
**mean 33.14 % on applications** (047.tomcatv 97.29 %, 052.alvinn 78.38 %, 048.ora 66.22 %). Removing
the alias hardware alone cost **5.48 % / 23.53 %**, *"almost as severe as not reordering at all."*

**Why we cannot have it.** Apple Silicon under Hypervisor.framework provides:

| CMS primitive | Available to us? |
|---|---|
| shadow register file + `commit`/`rollback` | **no** |
| gated store buffer | **no** |
| transactional memory as a substitute | **no** — `FEAT_TME` absent (`06 §5.4`) |
| alias-detection hardware | **no** — but largely obviated by hardware TSO (`14`, `16 §7`) |
| I/O-reorder trap | **no** — obviated by leaving MMIO unmapped (`06 §7.4`) |
| sub-page write protection | **yes** — 4 KB stage-1 + stage-2 (`14`) |

`03 §3.11`'s conclusion is exactly right and worth quoting because it is the framing this whole
document sits inside: *"Apple Silicon under Hypervisor.framework gives (iii) via stage-2 permissions
and, uniquely, hardware TSO, which obviates much of (ii). It gives nothing for (i) — recovery must be
software (translation-boundary checkpointing + interpreter re-execution), i.e. the CMS design minus
the free commit."*

**Software emulation of (d) is not viable, and the reason is worth being precise about.** Register
checkpointing in software is cheap — 16 `STP` at block entry is 1.34 ns (`M`), amortised over a
whole block instead of over one instruction, so ~0.02 ns/instruction for a 64-instruction block.
**Memory checkpointing is not.** Without a gated store buffer, a block that has performed any store
before the fault cannot be rolled back, so blocks must be split at every store — which, at 40 %
memory density with roughly a third of those being stores, means a block boundary every ~7
instructions and destroys the reordering window the scheme exists to create. `06 §5.4` reached this
conclusion; this document confirms the arithmetic and adds that **the register half of the checkpoint
is affordable and should be taken anyway** — see §6.

**What transfers verbatim, and should:** the *policy* layer. Adaptive retranslation with monotonically
accumulating conservative policies, region narrowing down to zero-instruction (interpreted)
translations, and the anti-oscillation rule are pure software, cost nothing, and address failure modes
we will hit. §6.4 makes this a recommendation.

---

## 4. The interaction that matters most: does a cheap fault make the tradeoff affordable?

**The claim to test:** "cheap in-VM faults (146 ns) should make the cheap-fast-path / expensive-fault-path
tradeoff affordable."

**Verdict: the conclusion is right, the reasoning is wrong, and the correct reasoning is more useful.**

### 4.1 Why the stated reasoning is wrong

The 145.8 ns shadow fill is **paid identically by every strategy.** It is the cost of discovering that
the shadow was stale and repopulating it — it has nothing to do with precise exceptions. Strategy (a)
pays it, strategy (b) pays it, strategy (c) pays it. It cancels out of every comparison in §0.3.

The quantity that actually trades against the fast path is the **incremental** reconstruction cost:
**+28 ns** for (b), **+120 ns** for (c). And it is paid only on **guest-visible** faults, not on
shadow misses (§0.4 finding 1).

### 4.2 The correct statement, and it is counterintuitive

> **Making the fault path cheap makes reconstruction a *larger* fraction of the fault handler, not a
> smaller one — while leaving it a negligible fraction of the program.**

| Fault path | Reconstruction as a fraction of it |
|---|---|
| host-side abort path, 11.5 µs (`12`, `02`) | 0.24 % |
| bare `HVC` host exit, 808 ns (`02`) | 3.5 % |
| **full in-VM shadow fill, 145.8 ns (`16 §2`)** | **19 %** |
| bare in-VM permission fault, 28.2 ns (`16 §1`) | **99 %** |

So the in-VM design does **not** make precise-exception reconstruction disappear into the noise of
the fault handler; it promotes it to a first-order term *of the handler*. That is why §6's two-tier
split matters: keeping the 28 ns off the hot (shadow-miss) fault is worth 19 % of the hot fault path,
which is a real number in a design whose whole thesis is fault-handling cost.

Simultaneously, as a fraction of the *program*, it stays negligible: 28 ns × 10⁶ guest faults/s =
2.8 % of one core, against strategy (a)'s +77 %.

### 4.3 The crossover, stated three ways

- **(b) vs (a′):** `F_g = 2.8 × 10⁷ /s`. At that rate the shadow fills alone would consume 4.1 s of
  CPU per second. **Unreachable.**
- **(c) vs (a′):** `F_g = 6.4 × 10⁶ /s`, ignoring `tcg_tb_lookup`. Still 6–600× above realistic rates,
  but only ~4× of headroom above a pathological workload.
- **(c′) re-translation vs (a′):** `F_g ≈ 1.6 × 10⁴ /s` at 50 µs per re-translation. **This is inside
  the realistic range.** A guest doing demand paging at 100 k faults/s under re-translation would pay
  ~500 % overhead in state recovery alone.

**Answer to the question as asked:** strategy (b) stops beating (a) at ~2.8 × 10⁷ guest faults/s, and
strategy (c) at ~6.4 × 10⁶ — both physically unreachable in a system that is also paying 146 ns per
shadow fill. Strategy (c′) — the thing the corpus believed QEMU does — stops beating (a) at
~1.6 × 10⁴/s, which is reachable, and that is the empirical case for not doing it.

### 4.4 The second-order effect the model does not capture, and it favours (b) further

Strategy (a) does not merely add stores. It **prevents cross-block register allocation entirely**,
because a register whose value must be in memory at every fault point cannot usefully live in a
register across blocks. That is `12` rank 3's "codegen quality" axis (`~4.4×` QEMU→Rosetta), and it
is not in the +77 % figure. The +77 % is the *floor* of (a)'s cost. I did not measure this
(`?` — it needs a real translated workload, not a synthetic loop) and it should not be quantified
without one.

---

## 5. Interaction with the rest of the design

### 5.1 Lazy flags — which flags are live at a fault?

**All of them.** The x86 exception frame pushes the full `RFLAGS`, including `PF` and `AF`, and the
guest handler can read it. There is no "dead flag at a fault point."

But the requirement is narrower than it first looks, and the narrowing is the whole trick:

> **On a *fault*, the required `RFLAGS` is the *pre-instruction* value — the value produced by the
> last flag-writing instruction *before* the faulting one.**

So the recoverable state is not "every instruction's flag inputs" but **one rolling lazy-flag tuple**
`(op_kind, src1, src2, width)`. It is a single live value, like RIP. Consequences:

- **Cost: 2–3 live pseudo-registers, permanently.** With 31 ARM GPRs for 16 x86 GPRs + RIP-base +
  flags tuple + `fs_base`/`gs_base` + the CPUState pointer + the site-table pointer, the budget fits
  (`06 §3.1` makes the same count). If they must be *spilled* instead, the measured cost is the
  "+3 stores" case: **+34 % … +76 %** on a memory-touching instruction across four runs (a wide
  spread — this is the noisiest figure in the probe). **Keeping the flag tuple in registers is worth
  tens of percent and is the single strongest argument for static register allocation.**
- **You may defer computing `RFLAGS`; you may not discard its inputs across a fault point.** `06 §5.2(3)`
  states this correctly.
- **Classic dead-flag elimination is illegal across a fault point**, because a fault is an observation.
  But **materialisation elimination is still legal and is the entire point of lazy flags** — you keep
  the tuple, you simply never compute the bits unless someone asks. `06 §5.2(3)`'s claim that
  "dead-flag elimination is far weaker here" is true but overstated in impact: the optimisation that
  matters (not computing `PF` and `AF`, ever, on the fast path) is unaffected.
- **The site table must record where the tuple lives**, exactly as QEMU's `data[1]` records `cc_op`
  (`target/i386/tcg/tcg-cpu.c:112`, `C`). One extra field.
- **The shift-count-zero trap** (`06 §6.5`): a shift with `CL == 0` leaves flags unchanged, which is a
  *data-dependent* conditional flag update. Under lazy flags the fix is a `CSEL` between the old and
  new tuple — 2 instructions, branchless — and it is fully compatible with recovery because both
  candidate tuples are live in registers at the fault point.
- **`PF`/`AF` are cheap when needed** (`06 §6.3`/`§6.4`): 3 instructions for `AF` and ~6 for `PF`,
  given the operands — which recovery guarantees are live. `AF` really is nearly free.

### 5.2 Cross-block register allocation

Permitted under (b), and this is (b)'s main payoff over (a). Requirements:

1. Every fault site records the current guest-register → host-location assignment.
2. **Compress by epoch, not by site.** Most consecutive fault sites share an assignment. Store a
   per-block assignment table plus, per site, a small epoch index (a few bits). `06 §5.2` proposes
   this and it is right. This is also what keeps the site table small enough to stay warm — which,
   given the measured 4× cold-lookup penalty (§0.4 finding 3), is a throughput argument and not just
   a space argument.
3. **The assignment must be a function of host PC alone**, not of a runtime path. Any optimisation
   that makes a register's location path-dependent (e.g. a diamond where a value is in `x9` on one
   arm and spilled on the other) must reconcile before the join, or emit separate sites.

### 5.3 TSO and the replay hazard

Three distinct questions, with three different answers.

**(i) Can a restarted instruction re-execute a store?** Only if it had already committed one, which
the invariant forbids for classes A–C and which the page-crossing test forbids for D–E. For class F
(`REP`) the architectural induction variables make each element idempotent-by-position. So: **no, by
construction.** This is why the invariant is stated in terms of "a single committing store" rather
than something weaker.

**(ii) Can a restarted instruction re-execute a *load*, and does that violate TSO?** Yes to the first,
no to the second. Loads have no side effects observable by other cores, so re-executing one is not a
memory-model event. The re-executed load may return a *different value* than the aborted first
attempt — which is fine precisely because the invariant guarantees the first attempt's value was never
used to produce architectural state. The exception is **MMIO**, where a load *is* a side effect; the
design already leaves MMIO unmapped and handles it in software (`06 §7.4`), so it never appears inside
a restartable translated instruction.

**(iii) Does a `LOCK`ed RMW risk a torn or duplicated update?** Not if it is a single LSE atomic
(§2.5). A load-modify-store lowering would be catastrophic here — it can fault between the load and
the store, and a restart would then perform the read twice with a different value, or worse, another
core could interleave. **"Never lower `LOCK` to load-modify-store" is a precise-exception rule as much
as a memory-model rule**, and it should be enforced in the IR.

One more, specific to this hardware: **`EnTSO` does not change any of this.** TSO is an ordering
property between different instructions; restart is a property of one instruction's internal steps.
`16 §7` measured `EnTSO` at 0 % on loads, scattered stores and pointer chasing, and +7–8 % on
DRAM-bandwidth-bound sequential stores. Nothing in this document interacts with that.

### 5.4 The "present-but-restricted rather than invalid" lever

`16` identified this as a first-class design lever from the fault side: permission faults are
**28.2 ns** and translation faults **94.4 ns**, a 3.3× difference, because VMSAv8-64 permits caching
only *valid* descriptors, so an invalid entry must re-walk (nested through HVF's stage 2) every time.

**This probe confirms the lever through a second, independent mechanism:**

```
  AT S1E1WP + ISB + MRS PAR_EL1 on read-only pages    (PAR.F=1, permission)   20.6 - 20.8 ns
  AT S1E1WP + ISB + MRS PAR_EL1 on unmapped pages     (PAR.F=1, translation)  40.6 - 42.5 ns
  AT S1E1WP + ISB + MRS PAR_EL1 on read-write pages   (PAR.F=0, success)      21.9 - 24.6 ns
```

**2.05×**, from the same architectural cause, measured with an instruction that takes no exception at
all. Note also that a *failing permission* probe costs the same as a *succeeding* one (20.7 vs
21.9 ns) — the translation is TLB-resident either way and only the permission check differs.

Applications in this document:

- **Guest page-table write-protection** and **SMC code-page protection** should keep entries
  **present-but-read-only**, never invalid. 3.3× cheaper on fault, 2.05× cheaper on probe.
- **The shadow-D-bit scheme** (`06 §2.5`) already works this way and is therefore on the cheap side.
- **The `AF=0` A-bit scheme is on the expensive side** and `16 §6` already recommends replacing it
  with `AT S1E1R` sweeps at 26.4 ns/page.
- **Fault injection for testing** (§7.3) becomes 3.3× cheaper: mark pages present-but-read-only rather
  than invalid to force a fault on every write.

### 5.5 SMC — the handler may find the faulting code invalidated

A real hazard, with a clean answer.

The faulting vCPU cannot have its own block invalidated between the fault and the handler running:
the abort is synchronous and the handler runs with `DAIF` masked. **Another vCPU can.** If vCPU 1
frees the block's code and site table while vCPU 0 is faulting inside it, vCPU 0 reads freed metadata
and reconstructs garbage — a silent guest-state corruption with no diagnostic.

Rules:

1. **Never free a translated block's code bytes or its site table at invalidation time.** Unlink it
   from the lookup structures (so no new entries occur), and defer the free until every vCPU has
   passed a quiescent point. This is RCU, and it is what QEMU does with `tb_lock`/`tb_remove`.
2. The quiescent point is already required for a different reason: `06 §8.4` shows that faithful
   `text_poke_bp` emulation needs a **vCPU rendezvous** before letting the patching write land. Reuse
   it — one mechanism, two obligations.
3. **The site table must be reachable from the host PC without consulting any structure that
   invalidation mutates.** The flat code-arena index in §9.2 satisfies this: the index entry is
   overwritten only when the arena chunk is *reused*, which happens only after the grace period.
4. Captive hit the mirror-image case and solved it the same way (`03 §1.4`, `C`): *"if the write hits
   the page currently executing, the store is emulated and control leaves the translation via a
   non-local jump rather than resuming it."*

`03 §6.5` records a directly relevant warning from the closest prior art: **Captive is *slower* than
QEMU on SimBench's `Data-Fault` category**, uniquely among all categories (it wins 32.38× and 25.88×
elsewhere), *"because of the bookkeeping needed to recover which guest virtual address faulted."*
That is precisely this document's subject matter, in the one system that most resembles our design,
and it is the empirical reason to design the recovery path deliberately rather than assume it is
noise.

---

## 6. What other real systems did

| System | Fast-path precise-exception cost | Recovery mechanism | Why it does or doesn't transfer |
|---|---|---|---|
| **Transmeta CMS** | **zero** | Hardware shadow registers + gated store buffer; `commit` at region end, `rollback` on any fault, then **re-execute the region in the interpreter**; adaptive retranslation narrows recurring-fault regions to zero-instruction (interpreted) translations (`03 §3.1`, `C`) | The **only** design that kept full reordering and precise faults. Hardware unavailable to us (no `FEAT_TME`, no gated store buffer). **The policy layer transfers verbatim** — §6.4. Ablation: strictly precise memory order cost **10.09 % on boots / 33.14 % on apps** |
| **IBM DAISY / BOA** | zero | Per-register **exception tag bits**: a speculative faulting op sets the tag instead of trapping; the tag propagates; the trap fires when a non-speculative op consumes it. *"Precise exceptions recovered by backward scan / state repair rather than commit-rollback."* Speculative MMIO loads become no-ops with the tag set and are re-executed non-speculatively at commit (`03 §3.2`, `C`) | Hardware. Interesting because the extra state is *invisible to the guest* and needs no context-switch save/restore. No ARM analogue |
| **Intel IA-32 EL** | — | Generic Itanium features: predication, control/data speculation via NaT bits and the ALAT. The paper names **precise exceptions** as one of its three hard problems, alongside x87 80-bit precision and misalignment (`03 §3.4`, `C`) | Confirms that without checkpoint hardware, precise exceptions are a named, first-order difficulty even for Intel |
| **DEC FX!32** | — | Did not solve it: *"no support for the NT Debug API because it cannot rematerialise precise x86 state at arbitrary points"* (`03 §3.5`, `C`) | The failure mode we are avoiding, stated by its authors |
| **NVIDIA Denver / Carmel** | zero | Software DCO below the OS, with load hoisting and reordering; a **hardware ARM decoder** executes cold code directly. Hardware TSO present (`03 §3.7`, `C`) | The dual-path idea (hardware executes cold code) has no cross-ISA analogue for us. Nothing published on its recovery mechanism |
| **LoongArch LBT** | — | Nothing on precise exceptions. Its hardware assists are state *representation*: 4 scratch registers, a native `eflags` register with x86 semantics, an `ftop` x87 stack pointer (`03 §3.8`, `C`) | *"LBT says the three pieces of x86 state worth putting in hardware are flags, an FP stack pointer, and scratch registers."* A native `eflags` register would remove §5.1 entirely. We don't have one |
| **Captive** | zero | Guest load/store is a single host `mov` on the unmodified guest VA; a real host page fault runs the generated guest-MMU model; on a genuine guest fault, a **non-local jump out of the fault handler to a safe point** in the main loop, which then runs the guest-architecture fault behaviour. Interrupt checks at **every basic-block boundary** because *"guest instructions expand to many host instructions and are not re-entrant"* (`03 §1.3`, `§1.5`, `C`) | **Our structural ancestor.** Note the honest cost: Captive **loses to QEMU on `Data-Fault`** (`03 §6.5`) — the one category that is this document's subject |
| **HyperMAMBO-X64** | zero | **Three tiers** (`03 §2.2`, `C`, verbatim): translation-time-known exceptions (syscalls, undefined instructions) get recovery code compiled directly into the fragment; runtime faults get a **per-fragment metadata table listing every potentially-faulting instruction and how to reconstruct the register context**; asynchronous interrupts are **resumed with interrupts disabled until the end of the fragment**, then recovered from fragment-linking metadata | **The cheapest known scheme and directly applicable.** §6 adopts all three tiers. Measured cost of the fault path: page faults truncate NET trace recording, *"costing up to 13 % extra on gcc/cactusADM"* |
| **QEMU (modern)** | **strategy (a) for GPRs** — `sync_globals` before every side-effecting op; plus a sleb128 side table for `eip` and `cc_op` only | `tcg_tb_lookup` (mutex + rbtree) → `cpu_unwind_data_from_tb` **linear sleb128 scan from the start of the TB** → `x86_restore_state_to_opc` writes `env->eip` and `env->cc_op` (`C`, source quoted in §3.3) | **Measured: 118–125 ns for a 64-instruction TB, 189–196 ns for 128.** Works because TCG blocks are short and registers are already in memory. Does not compose with cross-block register allocation |
| **QEMU (≤ v1.0, 2011)** | zero | **Full re-lift + re-codegen of the TB on every fault**, stopping at the faulting host PC (`gen_intermediate_code_pc` + `tcg_gen_code_search_pc`), with dedicated `restore_time`/`restore_count` profiler counters (`C`) | **Genuinely loses to conservative spilling at ~1.6 × 10⁴ faults/s** (§0.3). QEMU replaced it. Do not resurrect it |
| **FEX / box64** | zero, effectively | A userspace segfault kills the process, so precise state is only needed for signal delivery to a guest handler — a rare, slow path where a conservative reconstruction is affordable. `03 §4.6` records nothing at all about precise state in either project | **This is the reason user-mode results do not transfer.** In a system emulator the guest's `#PF` handler is the *demand-paging path*: COW, swap-in, `mmap` population, stack growth. It is not an error path, it is the hot path, and it runs thousands to millions of times a second |
| **Rosetta 2** | zero | Same category as FEX: user mode, and Apple additionally has hardware TSO and (reportedly) hardware assists. No published recovery mechanism | Same non-transfer |

**Why user-mode's approach is insufficient at system level, stated as one number.** A user-mode
translator can afford *any* recovery cost, because the event is a program crash or a rare guest
signal — call it 10⁰–10² per second. A system emulator's guest takes `#PF` as its normal mechanism for
demand paging, copy-on-write, stack growth and swap-in — 10⁴–10⁶ per second. **That is a factor of
10⁴, and it is exactly the factor that moves recovery from "don't care" to "design the whole code
generator around it."**

### 6.4 What to steal from CMS's policy layer

Pure software, costs nothing, and addresses failure modes we will hit (`03 §3.11`, `C`):

1. **Adaptive retranslation.** When a translated region faults repeatedly at the same site, retranslate
   it more conservatively — first with the aggressive optimisation disabled around that site, then with
   the region narrowed, ultimately down to a **zero-instruction translation that calls the interpreter
   for that one instruction.**
2. **Monotonically accumulating policies.** Once a translation has been given a conservative policy,
   never remove it when adding another. This is CMS's explicit anti-oscillation rule — without it, two
   translations that each fix one problem ping-pong forever.
3. **Translation groups.** Keep an LRU list of alternate translations of the same guest region. CMS
   needed **up to 33 versions** for the Windows 9x device-independent BLT driver.
4. **Stylised SMC.** An `add %eax, $imm` whose immediate is patched in a loop should be translated to
   *load the immediate from the code stream* rather than embedding it.

---

## 7. Testing strategy

The failure mode is silent guest corruption surfacing hours in. Everything below is designed around
one principle: **make the rare path common, deterministically.**

### 7.1 The oracle problem, and the answer

Three oracles, in increasing cost and decreasing coverage:

1. **Our own interpreter, in-process.** We need one anyway (§2.6). Build it first, make it the
   reference, and never optimise it.
2. **Bochs.** A straight interpreter with the best SDM-fidelity reputation available, able to dump
   complete CPU state at every instruction. ~100× slower than native, so it bounds a differential run
   to a boot plus a small suite — which is enough, because the interesting divergences happen during
   boot and early userspace.
3. **`kvm-unit-tests`.** The right *test corpus*, not oracle: it runs bare-metal in a VM and has
   dedicated tests for `#PF` error codes, SMEP/SMAP, A/D bits, `IRET`/NMI nesting, and — directly on
   point — an `emulator.c` suite that exercises instruction restartability. This is the fastest way to
   find the §1.3 error-code bugs and the §1.2 `RF`/`DR6` bugs.

### 7.2 Differential testing at every fault site

The construction `06 §12` Risk 1 asks for, made concrete:

- Build a mode where the runtime, at **every** fault site (not every fault — every *site*), reconstructs
  the full architectural state from the site table and compares it, field by field, against the
  interpreter stepped to the same guest instruction. Divergence aborts with both states dumped.
- Run it over the same workloads used for performance. It will be 100–1000× slower; that is acceptable
  because it is a correctness gate, not a benchmark.
- **Also build a strategy-(a) mode** (conservative materialisation) behind a build flag. It is ~50 lines
  of emitter, it is trivially correct, and it gives a second independent implementation to diff against
  — the two should produce bit-identical guest state at every guest exception. This is the single
  cheapest high-value test artifact in the plan, and it doubles as the fallback if (b) proves
  unworkable.

### 7.3 Exhaustive fault injection — and why this hardware makes it practical

**This is the strongest argument in this document for the in-VM design, and it is a testing argument,
not a performance one.**

Run the guest with **every** shadow page installed present-but-read-only (or with `AF = 0`), so that
**every guest memory access takes a fault** and the entire recovery path is exercised on every single
instruction. Cost, using measured numbers:

| Configuration | Per faulting access | 10⁹-instruction workload @ 40 % memory density |
|---|---|---|
| present-but-read-only, minimal handler (`16 §1`) | **28.2 ns** | **11.3 s** |
| present-but-read-only, full recovery + compare | **≈ 64 ns** (`M`, item 10) | **25.6 s** |
| full shadow fill each time (`16 §2`) | 145.8 ns | 58.3 s |
| invalid entries instead (translation fault) | 94.4 ns | 37.8 s |
| **if faults exited to the host** (`12`, measured abort path) | 11.5 µs | **4,600 s = 77 minutes** |

**100 % fault-path coverage on a billion-instruction workload costs 26 seconds in-VM and 77 minutes
if faults exit to the host.** That is a 180× difference between "run this on every commit" and "run
this overnight, occasionally." Note also that using **present-but-read-only** rather than **invalid**
makes the injection 3.3× cheaper (`16 §1`, §5.4) — the same lever, applied to testing.

Three injection modes:

- **Saturating:** everything faults, always. Maximum coverage, deterministic.
- **Nth-access:** fault exactly the *N*th memory access of the run, for N = 1…K, and after each,
  compare the reconstructed state against the interpreter stepped to the same point. This is
  Transmeta's *"if the interpreter does not reproduce the fault, it was a speculation artifact"*
  turned into a test oracle, and it is the only way to get systematic per-site coverage rather than
  statistical.
- **Adversarial:** fault only the *second* page of every access that crosses a page boundary (§2.3),
  and only the *last* store of every multi-store instruction (§2.4). This targets exactly the cases
  the invariant is designed to handle and that a random injector will almost never hit.

### 7.4 Metamorphic testing

With a deterministic configuration (single vCPU, deterministic device models, fixed TSC), run the same
workload with fault injection **on** and **off** and assert bit-identical final guest state and
identical output. Any precise-exception bug is by definition a divergence between "the instruction ran
straight through" and "the instruction faulted and was restarted." This catches bugs the state
comparison misses, because it also covers the *guest's* reaction to a state we reconstructed wrongly
but self-consistently.

### 7.5 Make the invariant checkable, not conventional

`06 §12` Risk 1's mitigation — *"Make 'is this state recoverable?' a type-level or assertion-level
property in the IR, not a convention"* — is right and should be built before the first optimisation:

1. Every IR node declares the guest locations it **kills** and the guest locations it **may fault at**.
2. A verifier pass asserts, at every may-fault node: the union of live locations covers all 16 GPRs,
   RIP, and every input of the live lazy-flag tuple; and **at most one committing store** has been
   emitted since the current x86 instruction began.
3. The verifier runs in debug builds on every translated block, and in release builds on a sampled
   fraction.
4. **The site-table emitter and the verifier read the same data structure.** If they can disagree, they
   will.

### 7.6 Instrumentation to ship

- Per-page fault counters, so `06 §12` Risk 2's SMC fault storms and this document's guest-fault-rate
  assumption are both observable rather than assumed.
- A histogram of **shadow-miss faults vs guest-visible faults**. The entire cost model in §0.3 turns on
  that ratio, and it is currently **modelled, not measured** (§8).
- A counter for tier-1 entries and the mean site-table lookup latency, using `CNTVCT_EL0` — remembering
  (`16 §Method`) that `PMCCNTR_EL0` traps to EL2, so the only in-guest clock has 41.67 ns granularity
  and must be used on batches.

---

## 8. Method, and what is wrong with these measurements

**Method.** Structure, register discipline, the `Ltimeit` batching protocol, the watchdog, the
EL2-trap counter and the `IC IALLU` after any host rewrite of guest code are all copied from
`probes/shadow_mmu` so the numbers are directly comparable to `16`. Timing is `CNTVCT_EL0` read
in-guest at EL1 under our own `VBAR_EL1`; no host exit occurs inside any measured window. Every fault
measurement has a matched non-faulting control executing the identical instruction sequence over the
identical address stream. Faulting addresses rotate over 64 pages × 32 intra-page offsets so neither
the address predictor nor a single cache set dominates. The AT-verification mode asserts **zero
exceptions taken**. The mode-4 handler counts faults and the harness asserts the exact expected count.

**Guards that caught real errors during this work, recorded because they will recur:**

1. **Cache-set aliasing in the fault loop.** The first version of the mode-4 touch loop used
   `base + page*4096` with no intra-page offset, so all 64 addresses landed in the same cache set and
   the *no-fault control* measured **10.26 ns** instead of 0.99 ns. Adding `(i & 31) * 128` — exactly
   what `16` does — fixed it. Any address stream that walks pages must spread within them.
2. **Position-in-run effects.** `16 §1` documented that the first configuration executed in a process
   pays extra. Confirmed here: the first mode-4 configuration measured 42.6 ns and a *bit-identical*
   handler measured 33.5 ns later in the same process. Fixed by running the whole sequence twice and
   discarding pass 1, **and** by bracketing every configuration with a fresh minimal-handler baseline
   run immediately before it.
3. **A non-physical result caught by the ordering check.** Before those fixes, the "+ permute 16 regs"
   handler measured *faster* than the strictly-smaller minimal handler. That is impossible, and it was
   the signal that the run order, not the handler, was being measured.

**What is wrong with these numbers, plainly:**

- **The clock estimate wanders badly.** Across invocations the dependent-`ADD` chain gave **1.94 –
  3.36 GHz**. `16` reported 3.18–3.32 GHz and a ±5 % cycle column; this probe's cycle columns are
  **±40 %** and should not be used. Every conclusion in this document rests on nanoseconds. The cause
  is presumably DVFS and core placement; `QOS_CLASS_USER_INTERACTIVE` is set and did not fix it.
- **The spill measurements are a best case and are stated as a lower bound.** One 128-byte L1-resident
  context block, a baseline loop with idle store-port slots, and no accounting for the register
  pressure the spill code creates. A real translated workload will be worse, possibly much worse — see
  §4.4.
- **The "translated x86 instruction" baseline (0.66–0.76 ns) is synthetic.** It is a 3-instruction ARM
  loop, not a translated workload. The `T = 0.70 ns` anchor in §0.3 is therefore `X`, and the
  percentage overheads scale inversely with it: if real translated code averages 1.4 ns/instruction,
  every overhead percentage in §0.3 halves. **The crossover fault rates halve too, and remain
  unreachable.**
- **The site-table lookup I recommend (§9.2) is not the one I measured.** I measured a flat binary
  search and a flat hash. The two-level scheme (flat code-arena index → small in-block table) is
  `X`, estimated at 15–30 ns cold by analogy with the measured 29 ns for a cold 4096-entry search.
  It should be measured before it is relied on.
- **The hash was measured only warm.** Its 0.0–1.5 ns net figure is a hot-loop number and, by this
  document's own §0.4 finding 3, is not the fault-path number.
- **Single vCPU throughout.** Nothing here measures the SMC/invalidation rendezvous, `tcg_tb_lookup`'s
  mutex analogue, or any multi-core effect.
- **One machine, one OS version, one core type**, as with the whole corpus (`13 §D-4`).
- **No x87/SSE state.** Recovering 16 more 128-bit registers is not measured and is not free.

---

## 9. Recommendation, restated as an implementation plan

### 9.1 The design

**Name:** two-tier fault handler + zero-cost site tables + probe-only-where-amortised.

1. **Fast path carries nothing.** No RIP materialisation, no register spilling, no flag
   materialisation. Guest GPRs in fixed ARM registers (static register allocation), lazy flags as one
   rolling tuple in registers, RIP derived from host PC.
2. **Every translated block emits a site table**, keyed on host PC, holding: guest RIP, the
   register-assignment epoch, the lazy-flag tuple's location, and the static read/write/RMW class
   (§2.7). Delta-encode and share epochs across sites (`06 §5.2`).
3. **Two-tier fault handler.**
   - **Tier 0** — the hot path, target **145.8 ns** (`16 §2`): read `FAR_EL1`/`ESR_EL1`, walk the guest
     x86 tables, and if the guest mapping permits the access, synthesise the ARM descriptor, store it,
     **`DSB ISHST` + `ISB`, no `TLBI`** (`16`'s design rule), `ERET`. **No state reconstruction at
     all.** The guest never learns this happened.
   - **Tier 1** — only when the walk says the guest itself would fault, target **+28 ns** (`M`) plus
     frame construction: find the site, materialise RIP and `RFLAGS`, spill the SRA registers into the
     guest CPUState, set `CR2` from `FAR_EL1`, build the error code from the completed walk + the site's
     static class, push the 5-quadword frame on the correct stack (`RSP0`/`IST[n]` from the TSS if the
     privilege changes), vector to the guest IDT.
4. **Asynchronous events never carry metadata.** Latch, run to the end of the current block (never
   inside an `STI` or `MOV SS` shadow), then deliver — HyperMAMBO's tier 3 (`03 §2.2`).
5. **Translation-time-known exceptions** (`SYSCALL`, `#UD`, `INT n`, `INT3`) get recovery compiled
   inline — HyperMAMBO's tier 1. No site-table entry needed.
6. **Instruction shape:** load-all → compute → store-all, at most one committing store. Page-crossing
   test (2 ALU + 1 predicted-not-taken branch) on any store whose alignment or stack range cannot be
   proven. `REP` with architectural induction variables, chunked at page granularity with one `AT`
   probe per page. `LOCK` always a single LSE atomic. Descriptor/far-transfer ops interpreted.
7. **`AT S1E1RP`/`S1E1WP` only where amortised over ≥ 100 elements or gated behind a ~0.2 %-taken
   branch.** Never per-instruction.
8. **Keep shadow entries present-but-restricted, never invalid**, wherever the semantics allow — 3.3×
   cheaper on fault (`16 §1`), 2.05× cheaper on probe (`M`, this document).
9. **CMS's policy layer**: adaptive retranslation, monotonic conservative policies, region narrowing to
   interpreted single instructions, translation groups.

### 9.2 The lookup structure

Measured constraints: a cold binary search costs **~29 ns at N = 4096** and **~41 ns at N = 65536**;
QEMU's linear scan costs **~2.0 ns per guest instruction in the block**. Both scale the wrong way.

**Recommended (`X`, not yet measured):** divide the code arena into fixed 4 KiB chunks and keep a flat
array `chunk_index[(host_pc - arena_base) >> 12]` → block descriptor. **One load, no search, no lock.**
Then a small sorted site array *inside the block's own metadata*, bounded at ~64 entries by capping
block length — **1.5–2.0 ns warm** (`M`), and the block descriptor load has already pulled the region
into cache. Estimated **15–30 ns cold**, i.e. at or below the measured 4096-entry binary search, with
no lock and no dependence on total code-cache size.

The property that matters more than the speed: **the index entry is a pure function of host PC and is
mutated only when a chunk is *reused*, which happens only after an RCU grace period.** That is what
makes §5.5 safe.

### 9.3 The invariant, and what it forbids

Restated from §0.5, with the optimisation-by-optimisation consequences:

| Optimisation | Legal? | Condition |
|---|---|---|
| cross-block register allocation | **yes** | assignment is a function of host PC; recorded per epoch |
| lazy flags (defer materialisation) | **yes** | tuple inputs stay live across every fault point |
| dead-flag elimination (discard the tuple) | **no** | a fault is an observation of `RFLAGS` |
| reordering non-architectural computation | **yes** | unconditionally |
| hoisting a load above a faulting store | **yes** | loads commit nothing |
| sinking an architectural register write below a faulting access | **required** | this *is* "store-all last" |
| fusing two x86 instructions | **only if** the fused unit has ≤ 1 committing store and the intermediate boundary is not a fault point |
| keeping `REP` counters in scratch registers | **yes, with** a site-table rule for reconstructing the architectural values from scratch + iteration count |
| lowering `LOCK` to load-modify-store | **never** | §2.5, §5.3 |
| more than one committing store per x86 instruction | **only with** a page-crossing test or a probe |

### 9.4 Build order

1. **Interpreter first.** It is the oracle (§7.1), the fallback for class G (§2.6), and CMS's terminal
   retranslation state (§6.4). Nothing else can be validated without it.
2. **Site-table infrastructure and its verifier (§7.5) before any optimisation exists.** `06 §13(4)`
   says this and it is the single most important sequencing decision in the project.
3. **Strategy (a) emitter behind a build flag**, as a second implementation to diff against (§7.2).
4. **Fault injection (§7.3) as a CI gate**, not an occasional exercise — it costs 26 s per billion
   instructions.
5. Only then: cross-block register allocation, lazy flags, fusion.

---

## 10. Open questions

1. **What is the shadow-miss : guest-visible fault ratio for a real Linux guest?** The entire cost
   model in §0.3 turns on it, and it is currently `X`. Measurable today by instrumenting a QEMU
   `x86_64` guest's `#PF` count against a modelled shadow-miss count, or in-VM once tier 0 exists.
   **This is the highest-value follow-up in this document.**
2. **The two-level site-table lookup (§9.2) is modelled, not measured.** Extend `probes/precise_exc`
   mode 4 with a `chunk_index` + in-block search handler and measure it on the fault path.
3. **The hash lookup was measured warm only.** Measure it in the mode-4 handler alongside the binary
   search.
4. **`T`, the mean cost of a translated x86 instruction, is synthetic.** Every percentage in §0.3
   scales inversely with it. It needs a real translated workload — which needs a translator.
5. **The cost of (a) on real codegen quality** (§4.4) is unmeasured and is the part of (a)'s cost that
   is *not* in the +77 % figure.
6. **Multi-vCPU.** The SMC/invalidation rendezvous (§5.5), and whether the site-table structures need
   any lock at all under the RCU discipline, are unmeasured.
7. **x87/SSE recovery.** 16 more 128-bit registers, plus x87's stack-relative register naming and
   80-bit precision (`06 §9.3`). Not costed here.
8. **Does `ESR_EL1.WnR` ever disagree with the translator's static class** (§2.7) in a way that
   matters? Testable directly with `kvm-unit-tests`' `#PF` error-code suite once tier 1 exists.
9. **The clock instability** (§8) makes cycle-level reasoning impossible in this probe. If cycle counts
   are ever needed, the only route is `PMCCNTR_EL0` from EL2 (i.e. from the host across an exit), or
   instruction counting.
10. **`FEAT_BBM` is absent** (`06 §0.2`), so changing a live shadow descriptor's size or output address
    requires break-before-make. A tier-0 handler that promotes a 4 KiB shadow entry to a 2 MiB block
    must do BBM, which costs a `TLBI` (~280 ns, `16 §3`) and, worse, opens a window in which another
    vCPU faults on an invalid entry. Interaction with precise exceptions unexplored.

**Reproduce:**
`cd probes && ./build.sh precise_exc/precise_exc.c && ./precise_exc/bin/precise_exc all`
(individual experiments: `cal`, `spill`, `lookup`, `at`, `atv`, `pf`).
