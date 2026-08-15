# 25 — The Correctness Oracle: how we know it is right, and how we debug it when it is not

**Date:** 2026-08-08 · **Host:** Apple M2 Max (8P+4E), 32 GiB, macOS 26.5.2 (25F84), `hw.pagesize 16384`
**Reference emulators used:** QEMU 11.0.3 (Homebrew, `arm64_tahoe`) and **Rosetta 2** (`oahd` live on this host)
**Guest:** Alpine 3.24.1 `vmlinuz-virt` 6.18.35 + the `probes/qemu_baseline/` initramfs — reused read-only, not rebuilt
**New artifacts:** `probes/oracle/` — `rsp.py`, `lockstep.py`, `plugin_state.c`, `bisect.py`, `flagtorture.c`,
`run_flagtorture.sh`, `diverge_demo.sh`, `results/`

> **Labels.** `MEASURED` = run on this machine today, command and output in `probes/oracle/results/`.
> `SOURCE(file:line)` = read out of this corpus or a source tree. `CITED(link)` = external, not run here.
> `INFERRED` = arithmetic on a measured number, or a design argument. `UNRESOLVED` = named experiment, not run.
> No estimate is presented as a measurement anywhere in this document.

---

## 0. Bottom line

### 0.1 The finding that reorders the plan

`08 §4` and `08 §7` risk 6 both say the same thing: *"Tier 1 as permanent diff oracle is non-optional
(07 §6). Budget for it."* `SOURCE(docs/research/08-feasibility-synthesis.md:129)`. `08 §9` then
sequences Tier 1 at **10–16 person-months** as the prerequisite that produces that oracle
`SOURCE(docs/research/08-feasibility-synthesis.md:142)`.

**That is the wrong order, and it is expensive.** Measured today, in one working session:

> A **deterministic, instruction-count-addressable, bisectable differential oracle over a real
> x86-64 Linux boot** was built out of stock QEMU 11.0.3, one 218-line TCG plugin and a 94-line
> Python script. It localises the first architecturally differing instruction between two
> emulator configurations in a **1.218-billion-instruction boot** to the exact instruction, in
> **~35 seconds of wall clock and ~11 MB of trace**. `MEASURED`, `probes/oracle/results/diverge_demo.log`

The oracle does not cost 10–16 pm and does not have to wait for Tier 1. It costs about an
engineer-week, it exists before a single line of natemu is written, and every subsequent
milestone can be gated on it. Tier 1 remains valuable as a *second implementation* — see §1.6 —
but it is not the critical path to being debuggable.

### 0.2 The four numbers that decide the whole testing strategy

| # | Question | Answer | Tag |
|---|---|---|---|
| 1 | Can you lockstep single-step two emulators through a boot over gdbstub? | **No.** 1 890 steps/s → **7.5 days** for one boot. At 3 000 steps you are still in SeaBIOS. | `MEASURED` |
| 2 | Can you compare them at every instruction anyway? | **Yes**, via a hash chain in a TCG plugin: 146 M insn/s, 0.4 bytes of trace per 1 000 instructions. **87 000× faster than lockstep.** | `MEASURED` |
| 3 | Is QEMU deterministic enough to be an oracle? | **Only with the exact recipe in §2.2.** Out of the box, two identical boots diverge at instruction **1 703 936 of 1 218 199 285** (0.14 % in). With `-icount` + a pinned RTC they agree through **1 218 183 168** (99.9987 %). | `MEASURED` |
| 4 | Is a full-EFLAGS differential test even well-posed? | **No, not naively.** Under strict comparison of all six arithmetic flags, **16 of 69** instruction groups disagree between two *shipping, correct* x86-64 implementations. With SDM undefined-flag masking, **0 of 69**. | `MEASURED` |

Number 4 is the one that would have cost a month of chasing phantom bugs.

### 0.3 The recommendation in one paragraph

**Build three oracles, in this order, and one debugging mechanism before any of them.** The
mechanism is **single-vCPU deterministic replay driven by retired-instruction count** — not
retrofitted, but designed in on day one, because in a DBT we synthesise the guest's clock and
deliver its interrupts, so determinism costs a design constraint rather than a subsystem (§5.2).
The oracles are: **(1)** the QEMU checkpoint-hash differential over boots and workloads (§1, §2) —
available now, cheap, catches control-flow and architectural-state divergence; **(2)** the
golden-vector ISA suites, with EFLAGS as the primary target and a **written-down defined-flag
mask** as a first-class artifact (§3); **(3)** the in-guest invariant checkers — shadow-table
equivalence, tier-0 fault classification, ASID generation — which are assertions rather than tests
and are the only thing that scales to SMP (§4, §6). Cost: **4.5–7.0 person-months**, which is
**additive to** `08`'s 28–48 pm, not inside it (§7.2). Explicitly not tested: x87 80-bit
fidelity, SMM, multi-vCPU replay, and the undefined-flag bits — §7.4 states what each of those
accepts.

---

## 1. (A) Differential testing against QEMU — at what granularity, and what it costs

### 1.1 The five candidate granularities, priced

| Granularity | Mechanism | Measured cost | Verdict |
|---|---|---|---|
| **Per instruction, lockstep** | two gdbstubs, `s` + `g` per step | **1 890 steps/s** → 7.5 days/boot | **Unusable for boots.** Usable for ≤10⁷-instruction windows |
| **Per instruction, hash chain** | TCG plugin, rolling FNV over PC+GPRs+RFLAGS, checkpoint every 2¹⁶ | **146 M insn/s** (pc), boot in 8.4 s | **This is the answer** |
| **Per basic block** | same plugin at TB granularity | not separately measured; strictly cheaper | Redundant — the per-instruction version is already cheap |
| **Per syscall / ring transition** | breakpoint on the `SYSCALL` entry MSR target | not measured | Useful as a *coarse* filter for userspace bring-up |
| **End state only** | final hash + guest output | free | Necessary, wildly insufficient — tells you *that* it broke, never *where* |

### 1.2 What lockstep actually costs — measured

`probes/oracle/lockstep.py` spawns two QEMU x86-64 full-system instances with `-S -gdb tcp::N`,
speaks the GDB Remote Serial Protocol directly (`probes/oracle/rsp.py`, 154 lines — **no GDB
binary is involved, and none is installed on this host**), single-steps both, and diffs the
24 core registers (16 GPRs + RIP + EFLAGS + 6 segment selectors) after every instruction.

```
A: qemu (baseline)  B: qemu (baseline)  noack=False/False
  step     1000  rip=00000000000ea29b      1808 steps/s
  step     2000  rip=00000000000ec90b      1882 steps/s
  step     3000  rip=00000000000ea438      1890 steps/s
--- lockstep result ---
steps            : 3000     wall: 1.587 s     rate: 1890 steps/s
```
`MEASURED`, `python3 probes/oracle/lockstep.py --steps 3000`

Per-packet breakdown against a single stub, so the two-emulator number is explained rather than
merely reported:

```
step only     16340 ops/s    61.2 us/op
g only         5308 ops/s   188.4 us/op
step+g         3868 ops/s   258.5 us/op
```
`MEASURED`

Three facts fall out:

- **The register read, not the step, is the cost.** `g` is 3× `s`, because QEMU's x86-64 `g`
  packet is **1 216 hex characters (608 bytes)** — it serialises the whole x87/SSE/AVX file even
  though a state diff needs 164 bytes of it. `MEASURED`
- **QEMU 11.0.3's gdbstub does not support no-ack mode.** `qSupported` answers
  `PacketSize=20020;qXfer:features:read+;vContSupported+;multiprocess+` and `QStartNoAckMode`
  returns the empty (unsupported) reply, so every packet pays a `+` round trip. `MEASURED` —
  this is the single cheapest available speedup of the lockstep path and it is upstream's to make.
- **Two emulators cost exactly 2×** (3 868 → 1 890), i.e. the driver is not the bottleneck.

**Extrapolation.** The boot measured here is **1 218 199 285 instructions** to power-down
(§2.2). At 1 890 steps/s that is **644 000 s = 7.5 days**. `INFERRED` from two measured numbers.
An hour of lockstep buys **6.8 M instructions = 0.56 % of the boot**; at step 3 000 the RIP trail
is `ec917 ec908 ec90b ec90d ec929 ec92b ec92e ec6e7` — still inside the SeaBIOS ROM region.
`MEASURED`

> **Verdict on the standard trick.** "Drive both under GDB" is the folklore answer and it is the
> wrong one at this scale. It is right for one thing only: once §2 has localised a divergence to a
> 65 536-instruction window, lockstep over *that window* costs 35 seconds and gives you a debugger
> on both sides simultaneously. Keep `lockstep.py`; do not point it at a boot.

### 1.3 The mechanism that does work — an instruction-count-addressable state hash

`probes/oracle/plugin_state.c` (218 lines) registers a per-instruction callback and folds
`(PC[, 16 GPRs, RFLAGS])` into a rolling FNV-1a hash, emitting `(icount, hash)` every 2ᵏ
instructions. Register reads use `qemu_plugin_read_register` (QEMU ≥ 8.2) and are done **only**
when the hash needs them or when the instruction lies inside an explicitly requested dump window
— so a stage-2 re-run costs the same as the cheap stage-1 pass.

Measured on the full boot, `-smp 1`:

| Configuration | Wall | Rate | Trace bytes |
|---|---|---|---|
| no plugin, free-running | **2.33 s** | 508 M insn/s | 0 |
| no plugin, deterministic (`-icount`+RTC) | **3.81 s** | 320 M insn/s | 0 |
| + PC hash chain, `ckpt=16` | **8.36 s** | **146 M insn/s** | **503 541 B** |
| + full-register window dump, 65 536 insns | +~0 | — | **10 886 066 B** (166 B/insn) |

`MEASURED`. The checkpoint file is **0.41 bytes per 1 000 guest instructions**. A full boot's
trace fits in half a megabyte; a year of nightly boots fits on a laptop.

`mode=regs` (hashing all 18 registers every instruction) was started and **abandoned after
>10 minutes without completing** — `MEASURED` as a negative result. That is the honest cost of
per-instruction register hashing through the plugin API, and it is why the design hashes PC on the
wide pass and reads registers only inside the window. **UNRESOLVED:** the exact `mode=regs` rate.
The experiment that settles it is a bounded run (`--steps`-limited guest, e.g. `natemu.task=alu`)
rather than a full boot; it was not worth the wall clock today because the two-stage design makes
the answer irrelevant.

### 1.4 The end-to-end divergence hunt, demonstrated on a real difference

`probes/oracle/diverge_demo.sh` boots the same kernel under two *differently configured* x86-64
emulators (`-cpu qemu64`, i.e. an AMD-vendor CPU, versus `-cpu Nehalem`, an Intel-vendor one),
bisects, re-runs the window, and prints the first differing instruction.

```
== stage 1: checkpointed state hash, A=qemu64 B=Nehalem
real 9.436s / real 7.845s
STATEHASH insns=1218199285 ... / insns=1225981156 ...
  ckpt bytes: 503541 / 506845

== stage 2: bisect to the window
A: 18589 checkpoints, 1218199285 insns total
B: 18707 checkpoints, 1225981156 insns total
last agreeing checkpoint : 196608
first differing checkpoint: 262144
window                    : [196608, 262144)  (65536 instructions)

== stage 3: re-run both with a full-register window dump [196608,262144)
  window bytes: 10886066

== stage 4: first differing instruction
FIRST DIVERGING INSTRUCTION at icount 201155
  A pc=00000000000ea627
  B pc=00000000000ea627
  rbx     A=68747541  B=756e6547
  rcx     A=444d4163  B=6c65746e
  rdx     A=69746e65  B=49656e69
```
`MEASURED`, `probes/oracle/results/diverge_demo.log`

`0x68747541 0x444d4163 0x69746e65` is `"Auth" "cAMD" "enti"`; `0x756e6547 0x6c65746e 0x49656e69`
is `"Genu" "ntel" "ineI"`. The tool found **the `CPUID` leaf-0 vendor string**, at instruction
201 155 out of 1.2 billion, from nothing but two half-megabyte hash files. **Four boots, ~35 s of
wall clock, no human in the loop.** That is the deliverable this document exists to argue for.

`bisect.py` is 94 lines including its docstring. The prompt asked whether a working 50-line
prototype beats three pages of description; it does, and this is it.

### 1.5 How far into a boot can you get — the honest answer per mechanism

| Mechanism | Reach into a 1.218 G-instruction boot | Tag |
|---|---|---|
| gdbstub lockstep, 1 hour | 6.8 M insns (**0.56 %**) — still in firmware | `INFERRED` |
| gdbstub lockstep, 1 week | ~1.14 G (**94 %**) | `INFERRED` |
| Checkpoint hash chain, 8.4 s | **100 %**, both sides, every instruction covered by a hash | `MEASURED` |
| Checkpoint + window re-run, 35 s | **100 %**, plus exact first-divergence localisation | `MEASURED` |

### 1.6 What QEMU is and is not, as an oracle

**Is:** an independent implementation with a completely different internal model (softmmu TLB,
memory-resident guest registers, sleb128 unwind tables — `SOURCE(docs/research/17-precise-exceptions.md:53)`),
already booting this exact guest on this exact host `SOURCE(docs/research/15-qemu-tcg-baseline.md:1)`,
and instrumentable without patching.

**Is not:** a specification. Section 3 measures QEMU disagreeing with Rosetta 2 on 16 of 69 flag
groups. Every such disagreement must be adjudicated against the SDM before it is called a natemu
bug. **The oracle finds divergences; a human decides who is wrong.**

**Not usable for:** anything multi-vCPU (§2.3), device timing, or performance. And note `13`'s
standing warning applies to oracles too: an oracle you have not proven deterministic is a
random-number generator that occasionally says "FAIL".

**On Bochs.** `17 §7.1` proposes Bochs as the best-fidelity oracle
`SOURCE(docs/research/17-precise-exceptions.md:1043)`. It is not installed on this host and was
not evaluated. `UNRESOLVED` — the experiment is `brew install bochs` plus a checkpoint plugin
equivalent, roughly a day. Its value is specifically the cases where QEMU and Rosetta disagree.

---

## 2. Determinism — the precondition nobody in this corpus has stated

### 2.1 The negative result

Two **byte-identical** QEMU invocations, same kernel, same initramfs, same everything:

```
run A: insns=1182440746  final_hash=8399e29b2df4dcd0
run B: insns=1178697715  final_hash=0a5a0c11a1121ad2
bisect: last agreeing checkpoint 1703936 / first differing 1769472
```
`MEASURED`

**They diverge after 1.70 million instructions — 0.14 % into the boot — and end up 3.7 million
instructions apart.** Any differential testing built on free-running QEMU produces a divergence
report on the first run, every run, in early firmware, forever.

Adding `-icount shift=3,sleep=off` alone is a large improvement and still not enough:

```
last agreeing checkpoint : 823984128   (68 % of the boot)
first differing checkpoint: 824049664
```
`MEASURED`

### 2.2 The recipe that works

```
-accel tcg -icount shift=3,sleep=off -rtc base=2020-01-01,clock=vm -smp 1
```

```
run 1: insns=1218199330 final_hash=fc140c26345f60a4
run 2: insns=1218199330 final_hash=fc140c26345f60a4
bisect: IDENTICAL: same final icount and same final hash
```
`MEASURED`, two independent processes.

`-icount` makes the virtual clock a function of retired instructions; `-rtc base=<fixed>,clock=vm`
removes the wall-clock read the guest performs during early boot. Both are required; neither
alone suffices.

**The residual, stated honestly.** A later pair of runs that differed only in *where their stdout
went* (a pipe being drained by `grep` versus `/dev/null`) produced `1218199330` and `1218199285`
— agreeing on **all 18 589 checkpoints through icount 1 218 183 168** and differing only in the
final ~16 K instructions. `MEASURED`, `probes/oracle/results/diverge_demo.log` stage 2 with
`CPUB=qemu64`. The cause is the serial backend: the guest polls the 16550 THR-empty bit during
power-down and the number of poll iterations depends on how fast the *host* consumer drains the
pipe. **Determinism is therefore bit-exact for 99.9987 % of the boot and breaks only in the
shutdown tail.** Fix: use an identical, non-blocking serial sink on both sides (`-serial file:`
or `-serial null`) and terminate the comparison at a guest-emitted marker rather than at
power-down. This is exactly the class of bug the oracle exists to find, and it found itself.

**Cost of determinism:** 3.81 s versus 2.33 s for the same boot, i.e. **1.64×**. `MEASURED`.
That is the price of a usable oracle and it is cheap.

### 2.3 Why `-smp 1` is not a limitation you can lift

QEMU's own record/replay is single-vCPU for the same reason ours must be: deterministic replay of
a multi-vCPU guest requires serialising the memory interleaving, which destroys the thing you were
trying to test. `19 §4a` shows the point empirically from the other side — the weak-memory outcome
counts vary by an order of magnitude between runs of the *same* configuration (1.06 M / 1.11 M at
N=2, 3.36 M / 4.21 M at N=8) `SOURCE(docs/research/19-smp-scaling.md:466)`. That variance *is* the
nondeterminism. **SMP correctness is not a differential-testing problem; it is an invariant and
litmus problem (§4).** `plugin_state.c` makes this refusal explicit: it counts and reports
instructions on vCPUs other than 0 rather than silently hashing an interleaving.

---

## 3. (B) Instruction-level ISA testing — EFLAGS is the target, and the test is not what you think

### 3.1 Why EFLAGS, restated with citations to the actual mechanism

The flag scheme is inherited from FEX, and every one of its three cleverness's is a silent-wrongness
surface:

- **PF is never computed at definition time.** FEX stores the raw 8-bit result in `pf_raw` and
  resolves `PF = popcount(pf_raw ^ 1) & 1` at read time, via a 3-instruction XOR cascade (4/2/1),
  **stored inverted** — `SOURCE(docs/research/raw/fexcore-a619f6192d8935d61.md:200)`,
  `:207`, `:215`. Worse, the parity *pass* is gated on a per-block header bit
  `CurrentHeader->ReadsParity`, set only if some instruction in the block actually read PF
  `SOURCE(docs/research/raw/fexcore-a619f6192d8935d61.md:194)`. A missed set of that bit is a
  correct-looking block that silently drops PF.
- **AF is stored as `src1 ^ src2`** and resolved as bit 4 of `(src1^src2^result)`, borrowing the
  `result` term from the byte PF already had to save
  `SOURCE(docs/research/raw/fexcore-a619f6192d8935d61.md:228)`, `:241`. `06 §6.3` notes AF's only
  *consumers* are invalid in 64-bit mode but it stays readable via `PUSHF`/`LAHF`
  `SOURCE(docs/research/06-x86-system-semantics.md:390)` — i.e. it must be correct and is almost
  never exercised. That is the textbook profile of a bug that surfaces in month nine.
- **CF polarity is tracked symbolically.** ARM's borrow is inverted; FEX carries a `CFInverted`
  flag through the IR and `GetRFLAG` absorbs it (`Invert ^= CFInverted`), un-inverting only on the
  way out to guest EFLAGS `SOURCE(docs/research/raw/fexcore-a619f6192d8935d61.md:280)`. A polarity
  bookkeeping error is a one-bit error in the *most* consumed flag.

And `17 §5.1` closes the trap: **at a fault, all of them are live** — the exception frame pushes
the full RFLAGS including PF and AF and the guest handler can read it
`SOURCE(docs/research/17-precise-exceptions.md:855)`. There is no dead flag at a fault point.

### 3.2 The test that was actually built and run

`probes/oracle/flagtorture.c` (361 lines) executes real x86-64 flag-writing instructions over a
controlled operand corpus and folds `(op, width, src1, src2, cf_in, result, flags)` into a
per-group FNV-1a hash. Capture uses **`SETO` + `LAHF` only** — no `PUSHFQ`, hence no stack traffic
and no red-zone interaction, and the captured set is exactly the six arithmetic flags the deferred
scheme is at risk on. Carry-in is set with a `cmpb`/`cmc` pair so that ADC/SBB/RCL/RCR are tested
in both carry states.

**Coverage design, and why the 8-bit arm is the load-bearing one.** PF is a function of the low 8
bits of the result alone; AF is a function of the low 4 bits of each source plus carry-in. So an
**exhaustive** 256 × 256 × {CF=0,1} sweep at width 8 is a *complete decision procedure for PF and
AF at every width*, for every operation whose low byte is computed identically at all widths —
ADD, ADC, SUB, SBB, AND, OR, XOR, CMP, TEST, INC, DEC, NEG. `INFERRED` (this is an argument about
the flag definitions in `06 §6.1`, not a measurement), and it is what makes the exhaustive claim
affordable: the wider arms then only have to cover CF/OF/SF/ZF, which do depend on the top bits,
and are covered by a structured corpus (boundary values ±1 around every power of two and every
sign boundary, plus a fixed-seed xorshift tail).

### 3.3 The two implementations that were differentially tested — on this host, today

This host runs **Rosetta 2** (`oahd` live; `arch -x86_64` works) `MEASURED`. So two independent,
shipping x86-64 implementations are available *right now*, and one of them — Rosetta — is the
closest existing thing to what natemu will be: an ARM64 DBT using Apple hardware TSO and `FlagM`.

`probes/oracle/run_flagtorture.sh` builds the same C source for `x86_64-macos` and
`x86_64-linux-musl` with `zig cc`, runs the first under Rosetta 2 and the second under QEMU 11 TCG
full-system in a purpose-built 1.4 MB initramfs (its own, under `probes/oracle/work/` — the
`qemu_baseline` images are read only), and diffs the group hashes.

### 3.4 The result, and the finding that saves a month

**Strict comparison of all six arithmetic flags:**

```
groups compared: 69   divergent under --strict: 16
divergent: BSF.64 BSR.64 RCL.32 RCL.8 RCR.16 RCR.64 RCR.8
           ROL.16 ROL.32 ROL.64 ROL.8 ROR.8 SHR.16 SHR.32 SHR.64 SHR.8
```
`MEASURED`, `probes/oracle/results/flagtorture.strict.{rosetta,qemu}.txt`

**23 % of instruction groups disagree between two correct implementations.** Drilling in with
`--dump` gives the exact bit:

```
SHR.8  0x80 >> 2  cf=0  ->  res=0x20   Rosetta: OF=1     QEMU: OF=0
ROL.8  0x01 rol 8 cf=0  ->  res=0x01   Rosetta: OF=1     QEMU: OF=0
BSF.64 (src=0)                          destination and CF/PF/SF/AF/OF all differ
```
`MEASURED`

Every one is an **architecturally undefined** bit: the SDM defines `OF` for shifts and rotates
**only when the count is 1**, defines nothing but `ZF` for `BSF`/`BSR` (and leaves the
*destination* undefined when the source is zero), and leaves `AF` undefined for the logic
operations. Rosetta 2 reproduces the real-silicon behaviour of leaving `OF` at the original MSB;
QEMU clears it. Neither is wrong.

**With a per-instruction defined-flag mask applied** (`M_LOGIC`, `M_SHIFT(c)`, `M_ROT(c)`,
`M_BSF`, `M_BT` — written into `flagtorture.c:56-70` directly from SDM Vol.2 "Flags Affected"):

```
== A: Rosetta 2 (arch -x86_64), corpus=256    real 0.16
== B: QEMU 11 TCG full-system, corpus=256     real 2.31
== diff
IDENTICAL: Rosetta 2 and QEMU TCG agree on every group hash
FLAGTOTAL cases=3310080 hash=0a4e5678f46845e9
```
`MEASURED`, `probes/oracle/results/flagtorture.defined.{rosetta,qemu}.txt`

> **The finding.** A full-EFLAGS differential test is **not well-posed** without a written-down
> undefined-flag mask. Skipping that step produces a 23 % false-positive rate on the first run,
> against implementations that are both correct. **The mask is a first-class test artifact and
> must be version-controlled next to the tests**, with a citation to the SDM paragraph for every
> masked bit. This is the single most useful thing in this section and it cost one afternoon to
> discover because two implementations were run rather than one.

Corollary, and it cuts the other way: **guests do depend on real-silicon behaviour of undefined
bits.** Rosetta chose to reproduce it; QEMU did not. natemu should reproduce Intel behaviour in the
undefined cases (it is nearly free — `OF = MSB(original)` falls out of the shift lowering) but must
never *test* against it, because that would pin an implementation choice as a requirement. Keep a
separate, non-gating `--strict` report so drift there is visible without being fatal.

### 3.5 Coverage, quantified

| Arm | Cases | Coverage claim | Tag |
|---|---|---|---|
| width 8, 12 two-operand + 3 one-operand + 7 shift/rotate ops | **65 536 / 131 072** per two-operand group | **Exhaustive** over (src1, src2, CF-in) | `MEASURED` |
| PF at all widths | — | **Complete**, via the low-byte factorisation (§3.2) | `INFERRED` |
| AF at all widths | — | **Complete**, via the low-nibble factorisation | `INFERRED` |
| widths 16/32/64 | 256 × 256 × {0,1} per group | Structured: all sign/carry boundaries ±1, plus pseudo-random | `MEASURED` |
| shift/rotate counts | 0 … 65 (0 … 16 at width 8) | **Exhaustive over the masked count, including the count==0 no-flag-update case** | `MEASURED` |
| **Total** | **3 310 080 cases, 69 groups** | in **0.16 s** (Rosetta) / **2.31 s** (QEMU, including a kernel boot) | `MEASURED` |

The count==0 case deserves its own line: `06 §6.5` names it as *"a classic source of 'works for an
hour then miscompares' bugs"* `SOURCE(docs/research/06-x86-system-semantics.md:428)` and `17 §5.1`
prescribes the branchless `CSEL` fix `SOURCE(docs/research/17-precise-exceptions.md:874)`. It is
covered exhaustively here at every width, for free, because the count loop starts at 0.

**What is not covered and should be added, in priority order.** `SHLD`/`SHRD` (double-precision
shifts, a genuine flag corner and used by real compilers); `MUL`/`IMUL`/`DIV`/`IDIV`;
`COMISS`/`COMISD`/`UCOMISS`/`UCOMISD` → `ZF`/`PF`/`CF`, which is the **hot** PF consumer and the
one `06 §6.4` says `AXFLAG` handles in a single instruction
`SOURCE(docs/research/06-x86-system-semantics.md:409)` — so it is both the most performance-critical
and the least tested path; `LAHF`/`SAHF`/`PUSHF`/`POPF` round trips (which is where PF and AF
actually become observable); `ADX` (`ADCX`/`ADOX`, which write CF and OF independently);
`CMPXCHG`/`CMPXCHG8B`/`CMPXCHG16B` ZF semantics; and 32-bit-compat-mode forms. `INFERRED` cost:
about a day of `flagtorture.c` extension per group above.

### 3.6 Existing suites — adopt, do not write

| Suite | What it is | Verdict for this project |
|---|---|---|
| **`kvm-unit-tests`** | Bare-metal x86 tests run inside a VM; dedicated coverage of `#PF` error codes, SMEP/SMAP, A/D bits, `IRET`/NMI nesting, and an `emulator.c` restartability suite | **Adopt first.** `17 §7.1` already names it `SOURCE(docs/research/17-precise-exceptions.md:1051)`, and `17 §2.7` notes the `ESR_EL1.WnR` bug it specifically catches `SOURCE(docs/research/17-precise-exceptions.md:455)`. It is the only suite that tests the *system* surface. Not installed here — `UNRESOLVED`, ~1 day to stand up |
| **RISU** (ARM's random-instruction-sequence differential tester) | Generates random instruction sequences, runs them under two implementations, compares register state at trace points | **Adopt the design, not the tool.** RISU is built around a *master/apprentice* pair exchanging register dumps at `RISU`-marker instructions — exactly §1.3's window dump, and its x86 support is not the mature part. Steal the marker-and-compare protocol; `plugin_state.c`'s window dump is already that protocol |
| **QEMU `tests/tcg`** | Per-target correctness tests shipped in the QEMU tree, including x86 flag tests | Adopt as a smoke suite. Cheap. Note it is QEMU-shaped, so it will not catch a bug QEMU also has |
| **FEX `unittests/` and `ASM/`** | FEX's own per-opcode assembly tests with expected register/flag state | **Highest-value adoption after `kvm-unit-tests`**, because we are inheriting FEX's *flag scheme* — its tests encode the invariants that scheme depends on. Note `SOURCE(docs/research/raw/fexcore-a619f6192d8935d61.md:331)`: `XAFLAG` appears **only** in `unittests/`, i.e. FEX's own suite covers paths its JIT never emits |
| **Intel SDE** | x86 emulator + instruction-set checker | x86-host only. Not usable on this machine. `CITED` |
| **`sandsifter`** | Fuzzes the *decoder* by walking the instruction-encoding space | Relevant to the decoder, not the flags. Low priority: FEXCore's `X86Tables` decoder is inherited and already hardened (`08 §4`) |
| **Blink test corpora** | Small, userspace | Marginal — `05` already rules Blink out as a component |

### 3.7 Fuzzing, and the honest limit

A random-instruction fuzzer is only as good as its oracle, and §3.4 shows the oracle needs a
hand-written undefined-flag mask per opcode. **Fuzzing is therefore blocked behind the mask table,
not the other way round.** Recommendation: write the mask table for the ~150 flag-writing
instructions first (that is a week of SDM reading and it is the *specification* of our flag
semantics), then fuzz. Doing it in the other order produces a fuzzer that reports 23 % false
positives and gets turned off.

---

## 4. (C) Memory model — testing the *emulator*, not the silicon

### 4.1 What `14` and `19` already proved, and what they did not

`14 §G-4` and `19 §4a` proved that **`ACTLR_EL1.EnTSO=1` makes the hardware enforce
store→store and load→load ordering**, up to 8 vCPUs, with zero weak outcomes in 24 configurations
and a strongly positive weak baseline `SOURCE(docs/research/19-smp-scaling.md:499)`.

That is a property of **the machine**, established with **hand-written ARM assembly**. It says
nothing about whether *our translation of x86 code* preserves x86-TSO. Three distinct ways the
emulator can break TSO on hardware that does not:

1. **The JIT reorders.** `EnTSO` constrains the *hardware's* store buffer. It does not stop our
   optimiser from hoisting a translated load above a translated store, or sinking a store past a
   load, at IR level. Nothing in `EnTSO` is visible to our scheduler.
2. **The JIT merges.** Two adjacent 4-byte guest stores lowered to one `STP` are a single
   16-byte store: another vCPU can no longer observe the intermediate state. `06 §7.4` flags
   exactly this hazard for MMIO `SOURCE(docs/research/06-x86-system-semantics.md:492)`; it is
   equally a TSO-visibility hazard for ordinary memory.
3. **`EnTSO` is per-vCPU context state.** If a vCPU is created, migrated, or resumed without
   `ACTLR_EL1.EnTSO` set, that vCPU runs weak while the others run TSO — the worst possible failure
   mode, because it is *intermittent and core-dependent*. `14`'s own caution that "a hypervisor
   that context-switches the register without applying it" passes a read-back test applies to our
   own vCPU setup path.

### 4.2 The rule every concurrency test in this project must obey

`14` records the cautionary tale in full: a **lockstep-synchronised MP formulation produced 0
violations in both the TSO and the non-TSO rounds**, and the harness correctly reported
INCONCLUSIVE rather than a false pass, because the per-iteration handshake had closed the race
window `SOURCE(docs/research/14-gate-results-verified.md:106)`. The free-running formulation with a
**concurrency witness** — the count of iterations in which the reader observed the writer advance —
is what worked: 810 345 witnesses in the weak round, 8 897 164 in the TSO round, i.e. the threads
overlapped **11× more** in the round that produced zero violations
`SOURCE(docs/research/14-gate-results-verified.md:96)`.

> **Mandatory rule, to be enforced in the harness, not in review:**
> **A concurrency test with no witness output is not a test.** Every litmus run must emit
> (a) the violation count, (b) the witness count, and (c) a threshold; and must report
> **INCONCLUSIVE**, never PASS, when the witness is below threshold. A "0 violations" line with no
> witness beside it should fail the build.

The corollary that is easy to miss: **the witness threshold must be calibrated against the weak
control**, not chosen. Run every litmus test twice — once with `EnTSO=0` — and require that the
control produce violations. A test whose weak arm is also clean is measuring nothing, and that is
precisely how `14`'s lockstep formulation failed.

### 4.3 The corpus, and which tests actually discriminate

x86-TSO **permits** store-buffering, so `SB` cannot distinguish TSO from ARM's weak model —
`14` says this explicitly `SOURCE(docs/research/14-gate-results-verified.md:88)`. The
discriminating set:

| Litmus | Forbidden under x86-TSO? | Allowed under ARM weak? | Use |
|---|---|---|---|
| **SB** | no (allowed) | yes | **Control only** — must produce outcomes under both, else the harness is broken |
| **MP** (store→store + load→load) | **yes** | yes | The primary test. `14`/`19` use the counter variant; keep it |
| **LB** (load-buffering) | **yes** | yes | Tests load→store ordering, which MP does not |
| **IRIW** | **yes** | yes | Tests multi-copy atomicity — the property MP cannot see. **Needs ≥4 vCPUs** |
| **WRC**, **RWC** | **yes** | yes | Transitivity / cumulativity |
| **2+2W**, **R**, **S** | **yes** | yes | Cheap additions once the harness exists |
| **MP + `LOCK XCHG`** | yes | — | Tests that `LOCK` lowered to an LSE atomic really is a full barrier |
| **SB + `MFENCE`** | **yes** (fence kills it) | — | Tests `MFENCE` → `DMB ISH` |
| **MP with `MOVNTI`** | **no** (x86 non-temporal stores are weakly ordered) | — | Tests that we did *not* over-strengthen in a way that hides a missing `SFENCE` in the guest |

**How to run them inside the emulated guest.** The tests must be **x86-64 machine code translated
by natemu**, executing on ≥2 guest vCPUs, or they test nothing we own. Two harness shapes:

- **In-guest, bare-metal (preferred).** Port `probes/tso_litmus/tso_litmus.c`'s free-running,
  witness-carrying structure into an x86-64 bare-metal payload booted by natemu. That reuses a
  structure this corpus has already debugged, and it removes the guest kernel's scheduler from the
  measurement. `INFERRED` cost: ~1 week, most of it x86 bring-up rather than litmus logic.
- **In-guest, under Linux.** Build `litmus7`-generated C from the diy toolsuite
  `CITED(http://diy.inria.fr/)`, statically linked, run in the Alpine initramfs with threads pinned.
  Cheaper to start, noisier, and the guest scheduler can close the race window — so the witness
  rule matters even more. `herd7`/`litmus7` are **not installed on this host** `MEASURED` and there
  is no Homebrew formula for herdtools; they come via `opam`. `UNRESOLVED` — a half-day to install.

**The essential addition:** run the whole corpus at **every JIT optimisation setting** — no
chaining, chaining, trace formation, cross-block register allocation on/off. Hazards 1 and 2 in
§4.1 are *optimiser* bugs and a litmus suite run only at the default setting will not see them.
This is the concurrency analogue of `17 §7.2`'s "build a strategy-(a) mode and diff the two"
`SOURCE(docs/research/17-precise-exceptions.md:1062)`.

### 4.4 The two pathological cases the prompt names, and how to witness them

**(a) Unaligned atomics spanning a cache line.** `06 §7.3`: x86 (P6+) guarantees atomicity for any
unaligned access **within a cache line**; `FEAT_LSE2` guarantees it only **within 16 bytes**. So an
unaligned 8-byte access at offset 60 of a 64-byte line is atomic on x86 and **not** on Apple
silicon `SOURCE(docs/research/06-x86-system-semantics.md:474)`.

*Test:* a writer thread alternately stores `0x0000…0` and `0xFFFF…F` to an 8-byte location at line
offset 60; N reader threads load it and count any value that is neither. **Witness:** the number of
iterations in which a reader observed the value change. **Control:** the identical test at offset 0,
which must produce zero tears with a live witness. Expected outcome: **tears at offset 60**, i.e. a
real, measurable fidelity gap — and then the design decision (accept it, or detect misalignment and
take the slow path) is made against a number rather than a paragraph. `UNRESOLVED` and worth
running early; `06` calls the gap "real, narrow, low-frequency" without measuring the frequency.

**(b) `LOCK`ed RMW spanning a page boundary.** `17 §2.5` is unambiguous: never lower `LOCK` to
load-modify-store; always to one LSE atomic `SOURCE(docs/research/17-precise-exceptions.md:409)`.
LSE atomics fault on misalignment, so a `LOCK XADD` at page offset 4093 cannot be lowered at all
and must take `06 §7.3`'s vCPU-rendezvous path (~10–50 µs)
`SOURCE(docs/research/06-x86-system-semantics.md:470)`.

*Test:* `LOCK XADD` at every offset in the last 16 bytes of a page whose successor page is (i)
present, (ii) read-only, (iii) absent — while N other vCPUs hammer the same line with aligned
atomics and a reader looks for a torn or lost update. This test hits **three** designs at once: the
rendezvous, the precise-exception path for a faulting atomic, and TSO. **It is the single highest-value
concurrency test in the plan** and it will not be reached by any random workload. Add a
**counter for rendezvous entries** to the shipping build (`17 §7.6` already asks for per-page fault
counters `SOURCE(docs/research/17-precise-exceptions.md:1128)`) so that a guest that hits split
locks in production is visible rather than merely slow.

### 4.5 The `EnTSO` cost hazard that testing must also cover

`19 §4b` measured `EnTSO=1` costing **4.6×–12.6× at 8 vCPUs on heavily write-shared cache lines**,
while being free on loads and on private buffers
`SOURCE(docs/research/19-smp-scaling.md:545)`. That is a spinlock/refcount/ring-head profile. It is
a *performance* result, but it belongs in the correctness plan for one reason: the temptation to
turn `EnTSO` off adaptively, per-region or per-vCPU, will arise. **Any such mechanism must be
gated behind the full litmus corpus running with the mechanism live**, because a dynamic TSO
enable/disable is exactly hazard 3 in §4.1 with a scheduler attached to it.

---

## 5. (E) Debuggability infrastructure — ranked by value ÷ cost

### 5.1 The ranking

| Rank | Item | Value | Cost | V/C | First milestone? |
|---|---|---|---|---|---|
| **1** | **Deterministic single-vCPU execution** (instruction-count-driven virtual time and interrupt delivery) | Without it nothing below works twice | **Low if designed in, very high if retrofitted** (§5.2) | **highest** | **Yes — non-negotiable** |
| **2** | **Instruction-count-addressable state hash + window dump + bisect** | Localises any divergence in a 10⁹-instruction run to one instruction | **Measured: one afternoon** for the QEMU side; ~1 week for the natemu side | **highest** | **Yes** |
| **3** | **Guest-state dump on divergence / on any internal assertion** | Turns "it panicked" into "here is the instruction and the state" | Trivial once #2's record format exists — same struct | very high | **Yes** |
| **4** | **The IR verifier for `17 §0.5`'s invariant** | Makes the precise-exception invariant *checkable, not conventional* `SOURCE(docs/research/17-precise-exceptions.md:1114)` | ~2 weeks | very high | **Yes** |
| **5** | **Shadow-MMU equivalence checker** (§6.1) | The MMU is the design's whole thesis; this is a total function over its state | ~1 week; **sub-millisecond per GiB to run** (§6.1) | high | **Yes** |
| **6** | **Fault-injection modes** (saturating / Nth / adversarial) | `17 §7.3`: 100 % fault-path coverage on 10⁹ instructions for **26 s** in-VM `SOURCE(docs/research/17-precise-exceptions.md:1090)` | ~1 week | high | Milestone 2 |
| **7** | **gdbstub for the guest** | Human debugging, and lets `lockstep.py` drive natemu unchanged | ~2–3 weeks (register map, memory read through shadow tables, breakpoints vs SMC) | medium | Milestone 2 |
| **8** | **Full record/replay of external inputs** (device DMA, host time, entropy) | Reproduces a customer bug from a log | ~1–2 months, and see §5.3 | **medium/low** | **No** |
| **9** | **Reverse execution** | Occasionally magical | Very high on top of #8 | low | **No** |

**Note that #7 — the gdbstub, the thing everyone builds first — ranks seventh.** It is a *human*
debugging tool. Items 1–5 are *machine* debugging tools, and at 10⁹ instructions the machine has to
find the bug before the human can look at it. Build the gdbstub, but build it second.

### 5.2 Deterministic replay when the JIT runs at EL1 inside a VM — the big one, answered

This is the question the prompt calls "the big one", and the answer is more encouraging than it
looks, for a reason specific to being a **DBT rather than a hypervisor**.

**Sources of nondeterminism, and who controls each:**

| Source | Under a hardware hypervisor | **Under natemu (DBT)** |
|---|---|---|
| Guest `RDTSC` / `RDTSCP` | Guest executes it; trapping is expensive | **We synthesise it.** The guest never executes a real timer read — `06 §11.3` already maps TSC to `CNTVCT × scale` `SOURCE(docs/research/06-x86-system-semantics.md:674)`. Return a function of **retired instruction count** in replay mode: determinism is free |
| Interrupt arrival point | Asynchronous, host-timed | **We deliver it.** Interrupts enter the guest at translated-block boundaries under our control. Make the delivery point a function of retired-instruction count — this is precisely QEMU's `-icount` design, and §2.2 measured it working |
| Guest reads of `CNTVCT_EL0` | Hardware-accelerated, untrapped (`08 §3.5`) | Irrelevant — *guest* code is x86 and never executes it. **Our own** EL1 code reading it is under our control |
| Device completion (virtio) | Asynchronous | Ours. Complete descriptors at instruction-count deadlines in replay mode |
| Host scheduler / DVFS | Affects timing | Affects only *timing*, not the instruction stream, once the three rows above are instruction-counted |
| **Multi-vCPU memory interleaving** | **Uncontrollable without serialising** | **Same. This is the one that does not yield.** |

**Verdict, stated plainly:**

> **Single-vCPU deterministic replay is achievable at low cost — but only if it is a design
> constraint from day one.** Every nondeterministic input must be funnelled through a small,
> named set of call sites, and there must be a global retired-instruction counter that is cheap
> enough to maintain unconditionally. Retrofitting it is the expensive version: QEMU's `-icount`
> reaches into its whole timer subsystem, and `19`'s finding that the framework GIC delivers SGIs
> **without a host exit** `SOURCE(docs/research/19-smp-scaling.md:390)` is exactly the kind of
> fast path that is wonderful for performance and fatal for after-the-fact determinism.
>
> **Multi-vCPU deterministic replay should not be attempted.** It is not a schedule question; it is
> the same reason QEMU's rr is single-vCPU, and the same reason `19 §4a`'s violation counts vary
> by 10× between identical runs. SMP bugs are found by litmus (§4), by invariants (§6), and by
> stress with assertions on — not by replay. **This is a limitation we accept, and §7.4 states what
> it costs us.**

**The cheap 80 %.** A *retired-instruction counter plus a deterministic TSC and a deterministic
interrupt-delivery point* is most of the value for a small fraction of the cost, and it does not
require logging anything. Full record/replay (item 8) adds the ability to reproduce a bug from a
*customer's* log, which is a support feature, not a development one. Build the first; defer the second.

### 5.3 Automatic bisection of a divergence — already prototyped, and it transfers

`bisect.py` + `plugin_state.c`'s window mode is the whole loop, and the only thing it needs from
natemu is: **emit `(icount, rolling_hash)` every 2ᵏ instructions, and be able to re-run.** That is
two counters and a file handle. The natemu side of this is a day of work, and it makes every
subsequent bug a 35-second bisect instead of a week of `printf`.

Design requirements the prototype exposes, which are not obvious until you build it:

1. **The hash must chain, not accumulate independently.** A per-checkpoint hash of "current state"
   would let a transient divergence heal invisibly; a chain makes the first divergence permanent.
2. **The checkpoint grid must be the same on both sides.** Powers of two of the retired-instruction
   count, agreed a priori. `bisect.py` intersects the two grids and refuses if they do not overlap.
3. **The wide pass must not read registers.** Measured: PC-only hashing runs at 146 M insn/s;
   register hashing did not finish a boot in 10 minutes. Read registers only inside the window.
4. **Windowed re-runs must be as cheap as the wide pass** — otherwise stage 3 dominates. Achieved by
   making register reads conditional on the window predicate inside the callback.

---

## 6. (D) System-level and MMU testing — invariants, not tests

`17 §7.5` puts it correctly: *"Make the invariant checkable, not conventional"*
`SOURCE(docs/research/17-precise-exceptions.md:1114)`. The MMU is where that principle pays best,
because the property is a **total function over the shadow state** and can be checked completely.

### 6.1 Shadow ≡ guest — a checkable invariant, and it is nearly free

**The invariant.** For every guest linear address `L` mapped by the guest's x86 tables under `CR3`:
the ARM stage-1 shadow entry for `L` in the corresponding ASID must (i) exist iff the guest entry is
present *and* the shadow is not deliberately restricted, (ii) have the same output frame,
(iii) have `AP`/`UXN`/`PXN` consistent with the guest's `R/W`, `U/S`, `NX` and the current `CR0.WP`,
`CR4.SMEP`/`SMAP`, (iv) never be *more* permissive than the guest entry, and (v) have `AF`/`D`
state consistent with §6.2.

**The direction that matters.** (iv) is the security- and correctness-critical half and it is a
one-way check: the shadow may legitimately be *less* permissive than the guest (that is the whole
"present-but-restricted" lever, `16 §2` / `17 §5.4`), but never more. A checker that demands
equality will drown in false alarms; a checker that verifies **containment plus a reason for every
restriction** will not.

**Cost.** `16 §2` measured a full 4-level x86 software walk at **1.14 ns** and walk+translate+store
at **5.13 ns** `SOURCE(docs/research/16-shadow-mmu-costs.md:169)`. A diff needs the guest walk plus
the shadow walk, i.e. roughly 2 × 1.14 ns per page warm. Applying `17 §0.4` finding 3's warning
that a cold table costs ~4× the hot-loop figure
`SOURCE(docs/research/17-precise-exceptions.md:124)`:

| Working set | Pages | Warm (2.3 ns/pg) | Cold-adjusted (9.2 ns/pg) | Tag |
|---|---|---|---|---|
| 64 MiB | 16 384 | 0.04 ms | 0.15 ms | `INFERRED` |
| 1 GiB | 262 144 | 0.60 ms | **2.4 ms** | `INFERRED` |
| 8 GiB | 2 097 152 | 4.8 ms | 19 ms | `INFERRED` |

> **A complete shadow-vs-guest equivalence sweep of a 1 GiB working set costs single-digit
> milliseconds.** `INFERRED` from `16 §2`'s measured walk cost. That is cheap enough to run **at
> every `MOV CR3`** in a debug build, and cheap enough to run periodically even in a
> performance-profiling build. There is no excuse for this invariant not to exist. **UNRESOLVED:**
> the measurement that would confirm it is a variant of `probes/shadow_mmu/` that walks both trees
> and diffs; ~1 day.

### 6.2 A/D bits — the invariant that catches silent drift

`14` establishes `HAFDBS = 0` on this host, so **every** x86 Accessed/Dirty update is fault-driven
`SOURCE(docs/research/14-gate-results-verified.md:172)`, and `16 §6` recommends replacing the
`AF=0` fault scheme with `AT S1E1R` sweeps at **26.4 ns/page**
`SOURCE(docs/research/16-shadow-mmu-costs.md:344)`.

Fault-driven means **drift is silent by construction**: nothing re-checks. Two invariants, checked
in the same sweep as §6.1:

- **Writable ⇒ dirty.** If a shadow entry permits writes, the guest PTE must already have `D=1`.
  A violation means we granted write permission without setting the guest's dirty bit — the guest
  will later fail to write back a dirty page and lose data with no diagnostic.
- **Present ⇒ accessed.** If a shadow entry is present, the guest PTE must have `A=1`.

And the **reverse direction, which is the one that will actually break**: when the guest *clears*
`A` or `D` (page reclaim, `kswapd`, `clear_refs`), the shadow must be demoted back to restricted, or
the next guest write will not fault and `D` will never be re-set. **Test:** run the guest under
memory pressure so `kswapd` is active, with the sweep running at every `CR3` load. This is a
workload requirement, not a test-code requirement, and it is easy to forget: a boot-to-init test
never reclaims anything.

`16 §6` also flags that setting `AF` **without** a TLBI was observed to work (139 ns vs 441 ns) but
is **not architecturally guaranteed** — "an implementation is permitted to cache an `AF=0` entry"
`SOURCE(docs/research/16-shadow-mmu-costs.md:336)`. **Test:** an `AF`-fault-count assertion (exactly
`16`'s "512 faults per round" check) that runs on **every supported host generation**, and a build
flag that reverts to the 441 ns arm. Do not let a 3× optimisation ride on one machine's behaviour.

### 6.3 The TLBI-skip on invalid→valid — sound, with two exceptions that must be assertions

`16 §2` states the argument and measures it: VMSAv8-64 does not permit a TLB to hold an entry for
an **invalid** descriptor, so a fill that turns an invalid L3 entry valid needs only `DSB ISHST` +
`ISB`; the probe took **exactly 512 faults per round** with no TLBI, i.e. no page faulted twice
`SOURCE(docs/research/16-shadow-mmu-costs.md:180)`. Dropping the TLBI takes the fill from 439 ns to
146 ns — "the single largest optimisation available in this design"
`SOURCE(docs/research/16-shadow-mmu-costs.md:192)`.

**The argument is sound as stated, and it is stated too narrowly.** Two cases break it, and neither
is visible in the probe because the probe fills a fresh table under one ASID:

1. **ASID recycling.** With 256 ASIDs `SOURCE(docs/research/14-gate-results-verified.md:168)`,
   an ASID handed to a new address space may still hold TLB entries from the *previous* occupant.
   From the TLB's point of view those entries are **valid**, so the invalid→valid rule provides no
   protection — the guest VA is not un-cached, it is cached with the *wrong* translation. The skip
   is sound **only if every ASID is invalidated (`TLBI ASIDE1IS`) at the moment it is recycled,
   before it is handed out.** That is a rule, and it should be an assertion: a generation counter
   per ASID slot, checked on every `TTBR0_EL1` write in a debug build.
2. **Table-page reuse.** ARM permits caching of *intermediate* (non-leaf) descriptors. If an L2/L3
   table page is freed and reused for a different linear range, a walk may still hit a cached
   intermediate pointing at the old table. Invalid→valid at the leaf says nothing about this. The
   rule: **table pages enter a quiescence/RCU grace period before reuse, exactly as `17 §5.5`
   already requires for translated code blocks** `SOURCE(docs/research/17-precise-exceptions.md:963)`
   — one mechanism, now three obligations.

> **Finding.** *"Skip TLBI on invalid→valid"* is sound as a statement about leaf descriptors within
> a single ASID generation and a stable table topology. It is **not** sound across ASID recycling or
> table-page reuse, and `16 §2` does not say so. Both exceptions are cheap to make into assertions;
> neither is likely to be found by testing, because both are rare and their symptom is a wrong
> translation rather than a fault.

**Test that would find them anyway:** shrink the resource. See §6.4.

### 6.4 The 256-ASID policy — shrink the resource to make the rare path common

`16 §5` measured the cost and concluded N-11 is "quantitatively a non-issue": a fresh ASID costs a
full refill (~20.4 ns/page, bounded at ~63 µs), an ASID rollover costs **0.1 ns/page more than
that** (20.52 vs 20.40), and the modelled rate is **8–40 rollovers/s = 0.05–0.25 % of one core**
`SOURCE(docs/research/16-shadow-mmu-costs.md:305)`.

That is a *performance* verdict, and it makes the *correctness* problem worse: at 8–40 rollovers/s
a recycling bug will be hit by a random workload roughly once a minute and will manifest as a
wrong translation in an unrelated process. Untraceable.

> **The trick, and it is the same one `17 §7.3` uses for faults: make the rare path common by
> shrinking the resource.** Build with `ASID_BITS = 2` (4 ASIDs) in a debug configuration. Rollover
> then happens thousands of times per second instead of tens, every recycling bug fires within
> seconds, and both §6.3 exceptions become reachable by ordinary workloads. Cost: a `#define` and
> one build-matrix entry. This is the highest value-per-line item in the whole MMU test plan.

The same shrink applies elsewhere: a 4-entry shadow-table cache, a 1-page code arena, a 2-entry
site table. Any capacity constant in the design should be shrinkable from the build system.

### 6.5 The imprecision the design deliberately accepts — how to test that it is never observable

`17 §0.1` splits the fault handler: **tier 0** (shadow miss) does **no** state reconstruction
whatsoever; **tier 1** (a fault the guest itself would have taken) adds +28 ns of reconstruction
`SOURCE(docs/research/17-precise-exceptions.md:25)`. The correctness of the whole design therefore
reduces to one claim:

> **Every fault that reaches tier 0 is genuinely invisible to the guest.**

This is a *classification* property, not a state-reconstruction property, and that makes it much
easier to test than it first appears.

**Test 1 — the tier-0 classification assertion (highest value in this document after §6.4).**
At every tier-0 fault, re-walk the **guest's own** x86 page tables for the faulting linear address
and assert that they permit the access. If they do not, the fault was misclassified and should have
been tier 1 — abort loudly with both states. Cost: **1.14 ns** per assertion (`16 §2`'s measured
4-level walk) on a path that already costs 145.8 ns, i.e. **under 1 %**
`SOURCE(docs/research/16-shadow-mmu-costs.md:169)`. It is cheap enough to leave on in release builds.
`17 §7` does not propose this and it is the cheapest thing here.

**Test 2 — metamorphic, from `17 §7.4`.** With a deterministic configuration (§5.2), run the same
workload with fault injection on and off and assert bit-identical final guest state
`SOURCE(docs/research/17-precise-exceptions.md:1105)`. §2 supplies the missing half: the *mechanism*
for "bit-identical final guest state" is the checkpoint hash chain, and it gives you not just
identical/different but *where*.

**Test 3 — the second implementation, from `17 §7.2`.** Build strategy-(a) (conservative
materialisation, ~50 lines of emitter) behind a flag and require bit-identical guest state at every
guest exception `SOURCE(docs/research/17-precise-exceptions.md:1062)`. This is the cheapest
high-value artifact in `17` and this document endorses it without modification.

**Test 4 — the observers `17` does not enumerate.** The imprecision is unobservable only if nobody
is looking. Four observers exist:

| Observer | Covered by | Status |
|---|---|---|
| The faulting vCPU itself | The single-committing-store invariant (`17 §0.5`) | Covered |
| **Another vCPU** | Same invariant + `17 §5.5`'s RCU rule | Covered |
| **A device doing DMA** | **Nothing in `17`.** virtio devices read guest memory asynchronously, so a partially-committed instruction is observable by the *device model* even when no vCPU can see it | **Gap.** Test: a virtio debug mode that scans descriptor buffers for partially-written multi-store instruction targets, plus an assertion that no DMA read overlaps an in-flight committing store |
| **The guest's own debug facilities** | Nothing | **Gap.** A guest debugger (`gdb` on a guest process), `kprobes`, `ftrace`'s `int3` patching, and hardware `DR0-3` all observe *instruction boundaries*. Test: run `perf`, a `kprobe`, and an in-guest `gdb` single-step over a page-crossing store and a `REP MOVS` |

Both gaps are real and neither is exotic — `ftrace` is on in every distro kernel, and `06 §8.4`
already identifies `text_poke_bp` as needing a vCPU rendezvous
`SOURCE(docs/research/06-x86-system-semantics.md:530)`.

**Test 5 — the prefix trap.** `17 §2.2` warns that recovered RIP must point at the **first prefix
byte**, and that getting it wrong makes the resumed instruction *a different instruction*
`SOURCE(docs/research/17-precise-exceptions.md:288)`. This is directly testable and trivially so:
a `REP MOVSB` with both a segment override and an address-size override, faulted mid-string by
adversarial injection, resumed, and checked for the right byte count. One test, one afternoon,
catches a bug class with an "hours-long incubation period".

---

## 7. (F) The plan, the cost, and what will not be tested

### 7.1 Mapped onto the milestone ladder from `08 §9`

| `08 §9` milestone | Test artifacts that must exist **by the end** of it | New? |
|---|---|---|
| **0. Now (pre-code)** | QEMU deterministic checkpoint oracle (§2.2); `plugin_state.c`/`bisect.py`; `flagtorture.c` + the **defined-flag mask table**; `kvm-unit-tests` running under QEMU as the *golden* reference log | **Built today**, except the mask table beyond §3.4's groups and `kvm-unit-tests` |
| **2. Tier 1 bring-up** (10–16 pm) | natemu emits the same checkpoint format → `bisect.py` works against QEMU unmodified; `flagtorture` passes; `kvm-unit-tests` boot-and-pass list tracked as a ratchet; guest-state dump on any assertion | Yes |
| **3. Tier 2 nanokernel** (8–14 pm) | Shadow≡guest sweep at every `CR3` (§6.1); A/D invariants (§6.2); `ASID_BITS=2` debug build (§6.4); ASID generation assertion (§6.3); tier-0 classification assertion (§6.5 test 1) | Yes |
| **4. x86 system surface** (6–10 pm) | Fault-injection modes (`17 §7.3`); strategy-(a) diff build (`17 §7.2`); the prefix/`REP` restart tests (§6.5 test 5); `#PF` error-code tests from `kvm-unit-tests`; guest gdbstub | Partly in `17` |
| **5. Performance closure** | Full litmus corpus at **every optimisation setting** (§4.3); split-lock and cache-line-tearing tests (§4.4); `EnTSO` per-vCPU assertion (§4.1 hazard 3) | Yes |

**The ratchet.** Every suite above must be a **monotone** gate: the set of passing
`kvm-unit-tests`, the set of matching flag groups, and the boot checkpoint at which natemu diverges
from QEMU are all numbers that may only go up. A "boot divergence icount" that increases every week
is the single best progress metric this project can have, and it is free once §2 exists.

### 7.2 Cost, and whether it fits inside `08`'s 28–48 pm

| Item | Person-months | Basis |
|---|---|---|
| QEMU differential oracle (checkpoint, bisect, harness, CI) | **0.2** | `MEASURED` — the prototype in `probes/oracle/` is one session's work; 0.2 pm is hardening + CI |
| natemu side of the checkpoint format + window dump | **0.25** | `INFERRED` — two counters and a file handle (§5.3) |
| Determinism as a design constraint (instruction-counted TSC, interrupt delivery, device deadlines) | **0.5–1.0** | `INFERRED`; **×5–10 if retrofitted** (§5.2) |
| Flag/ISA golden vectors + **the defined-flag mask table for ~150 instructions** | **0.75–1.0** | `INFERRED` — §3.5's gaps at ~1 day/group, plus a week of SDM reading for the mask |
| `kvm-unit-tests` adoption + ratchet | **0.5** | `INFERRED` |
| MMU invariant checkers + shrinkable-capacity debug builds | **0.5** | `INFERRED` from §6.1's measured walk cost |
| Fault injection + strategy-(a) diff build | **0.5** | `17 §7.2`/`§7.3` call both cheap; `17 §7.2` says (a) is ~50 lines of emitter |
| In-guest litmus harness + witness discipline + split-lock/tearing tests | **1.0–1.5** | `INFERRED`; most of it is x86 bare-metal bring-up, reusing `probes/tso_litmus/`'s structure |
| Guest gdbstub | **0.5–0.75** | `INFERRED` |
| CI: nightly boot-divergence bisect, per-commit flag/ISA suites | **0.25** | `INFERRED` — §3.4's full suite runs in 2.3 s |
| **Total** | **4.5–7.0 pm** | |

**Does it fit inside 28–48 pm? No — and it never did.** `08 §4`'s "Write ourselves" list
`SOURCE(docs/research/08-feasibility-synthesis.md:88)` and `08 §9`'s sequencing
`SOURCE(docs/research/08-feasibility-synthesis.md:140)` contain **no test-infrastructure line item**;
`08 §7` risk 6 says "Budget for it" and then does not
`SOURCE(docs/research/08-feasibility-synthesis.md:129)`. The honest restatement:

> **Tier 2 is 28–48 pm of emulator plus 4.5–7.0 pm of correctness infrastructure = 33–55 pm.**
> That is **10–15 %** of total effort on test, against an industry norm nearer 30–50 % for an
> emulator. The reason it can be that low is that most of the coverage comes from *invariants and
> a free external oracle* rather than from written tests — and if that argument turns out to be
> optimistic, the overrun lands in this line item first.

**The schedule offset.** `08` charges the oracle to Tier 1's 10–16 pm. §1 shows a usable oracle
costs 0.2 pm and is available before any natemu code exists. That does not make Tier 1 worthless —
a second *from-scratch* implementation catches shared-assumption bugs that QEMU never will — but it
**removes the argument that Tier 1 must be finished before Tier 2 can be debugged**. If staffing
forces a choice, that is a real degree of freedom `08 §9` did not have.

### 7.3 What gets run when

| Cadence | Suite | Measured/estimated cost |
|---|---|---|
| **Per commit** | `flagtorture` (3.3 M cases), QEMU `tests/tcg`, unit tests, IR verifier on every block | **2.3 s** for flagtorture `MEASURED` |
| **Per merge** | Boot-to-init checkpoint diff vs QEMU; `kvm-unit-tests` ratchet; MMU invariants on; `ASID_BITS=2` build | ~10 min `INFERRED` |
| **Nightly** | Fault injection (saturating + Nth); strategy-(a) diff build; litmus corpus × optimisation matrix; memory-pressure A/D run | hours; `17 §7.3` prices 100 % fault coverage at **26 s per 10⁹ instructions** in-VM `SOURCE(docs/research/17-precise-exceptions.md:1090)` |
| **Weekly** | Long-running guest workloads with all assertions on; split-lock/tearing stress at 8 vCPUs | overnight |

### 7.4 What will NOT be tested, and what that accepts

Stated plainly, because a test plan that claims to cover everything is a test plan nobody believes.

1. **Multi-vCPU deterministic replay — will not exist.** *Accepts:* any SMP bug that is not caught
   by litmus, by an invariant, or by an assertion is found only by reproduction under stress, and
   some will not be reproducible at all. Mitigation: make the assertions cheap enough to leave on
   in production builds (§6.5 test 1 costs <1 %), so an SMP bug at least *reports* rather than
   corrupting. **This is the largest accepted risk in the document.**
2. **Architecturally undefined flag bits — deliberately masked.** *Accepts:* a guest that depends on
   real-silicon behaviour of an undefined bit will misbehave and our test suite will be silent.
   Mitigation: match Intel behaviour where free, and keep the non-gating `--strict` report so drift
   is visible. Measured exposure: 16 of 69 groups (§3.4).
3. **x87 80-bit precision.** `06 §9.3` calls it "the worst fidelity problem". No oracle here
   resolves it: QEMU uses softfloat, Rosetta and ARM NEON do not have 80-bit. *Accepts:* long-double
   numerical differences. Mitigation: none available at acceptable cost. **This is a finding, not a
   plan.**
4. **SMM, real mode, and 5-level paging (LA57).** `06 §10.2`/`§10.3` scope them out; `14` shows
   `VARange=0` makes LA57 unshadowable and it "must not be advertised to the guest"
   `SOURCE(docs/research/14-gate-results-verified.md:44)`. *Accepts:* firmware and legacy boot paths
   are out of scope. Mitigation: assert loudly on entry to any of them rather than emulating badly.
5. **Device-model fidelity beyond virtio.** *Accepts:* a guest driver that pokes an unimplemented
   register gets a defined error, not silicon behaviour.
6. **Performance regressions in the correctness build.** The debug configuration is 100–1000×
   slower (`17 §7.2`) and nothing asserts that the *release* build has the same semantics as the
   debug build except the metamorphic test in §6.5 test 2. *Accepts:* a bug that exists only when
   assertions are compiled out. Mitigation: the strategy-(a) diff build and the checkpoint hash are
   both runnable in release configuration.
7. **Older macOS and other Apple generations.** `14`'s corpus-wide caveat applies to every number
   here: single machine, single OS version `SOURCE(docs/research/14-gate-results-verified.md:196)`.
   *Accepts:* the `AF`-without-TLBI optimisation (§6.2) and the 4 KB IPA granule are macOS 26+ and
   M2-verified only.

---

## 8. Open questions, each with the experiment that settles it

| # | Question | Experiment | Cost |
|---|---|---|---|
| 1 | What is the true per-instruction register-hash rate through the QEMU plugin API? | `mode=regs` on a bounded workload (`natemu.task=alu`) rather than a boot | 1 hour |
| 2 | Does Bochs agree with Rosetta or with QEMU on the 16 undefined-flag groups — i.e. which one matches real silicon? | `brew install bochs`; run `flagtorture --strict` as a third arm | 1 day |
| 3 | How often do unaligned 8-byte accesses spanning a cache line actually tear on M2? | §4.4(a)'s witness-carrying tearing test, in-VM | 1 day |
| 4 | Does the shadow≡guest sweep really cost ~2.4 ms/GiB? | A `probes/shadow_mmu/` variant that walks both trees and diffs | 1 day |
| 5 | Can `kvm-unit-tests` be made to run and how many pass under QEMU today (the target ratchet)? | Build it; record the pass list as the reference | 1 day |
| 6 | Does the ASID-recycling hazard in §6.3 actually fire, and how fast with `ASID_BITS=2`? | Extend `probes/g1_mmu4k/` with a recycling stress under a shrunken ASID space | 2 days |
| 7 | Does the serial-backend nondeterminism in §2.2 vanish with `-serial file:` on both sides? | Re-run §2.2 with matched non-blocking sinks | 15 minutes |
| 8 | Is `litmus7`-generated code translated by natemu subject to JIT reordering that hardware TSO does not prevent? | The §4.3 optimisation matrix, once a JIT exists | — (blocked on natemu) |

---

## 9. Reproduce everything in this document

```bash
# harness (reuses probes/qemu_baseline/work/ read-only; re-fetch it with that dir's build.sh)
cd probes/oracle
cc -O2 -shared -fPIC -o plugin_state.so plugin_state.c \
   -I/opt/homebrew/include $(pkg-config --cflags glib-2.0) -undefined dynamic_lookup

# sec 1.2  gdbstub lockstep, and its self-test
python3 lockstep.py --steps 3000 --report-every 1000
python3 lockstep.py --steps 5000 --inject-divergence 2500 --report-every 0

# sec 1.4 / 2.2  determinism + end-to-end divergence bisect
./diverge_demo.sh                 # qemu64 vs Nehalem -> finds the CPUID vendor string
CPUB=qemu64 ./diverge_demo.sh     # identical configs -> determinism control

# sec 3.4  Rosetta 2 vs QEMU TCG, 3.31 M flag cases
CORPUS=256 ./run_flagtorture.sh
```

Raw outputs kept in `probes/oracle/results/`:
`diverge_demo.log`, `boot.qemu64.ckpt`, `boot.qemu64.run2.ckpt`,
`flagtorture.{strict,defined}.{rosetta,qemu}.txt`.

---

## 10. Corrections and additions to the corpus

1. **`08 §7` risk 6 and `08 §9`** — the oracle is sequenced as an output of Tier 1 (10–16 pm). §1
   measures a usable one at ~0.2 pm, available before any natemu code. **The dependency should be
   removed from the critical path.**
2. **`08`'s 28–48 pm** contains no test-infrastructure line item. §7.2 adds **4.5–7.0 pm**, giving
   **33–55 pm**.
3. **`16 §2`'s "the TLBI on the fill path is unnecessary"** is sound for leaf descriptors within one
   ASID generation and a stable table topology. §6.3 identifies **two cases where it is not** —
   ASID recycling and table-page reuse — neither of which the probe could have seen. `16` should be
   amended with the two rules.
4. **`16 §5`'s "N-11 is quantitatively a non-issue"** is right about performance and understates the
   *correctness* exposure: 8–40 rollovers/s is fast enough to hit a recycling bug constantly and
   slow enough to make it untraceable. §6.4's `ASID_BITS=2` debug build is the cheap fix.
5. **`17 §7`** is a good testing section and this document does not contradict it. It should gain:
   the **tier-0 classification assertion** (§6.5 test 1, <1 % cost, catches the misclassification
   that the whole two-tier design rests on), and the **two unenumerated observers** — device DMA and
   the guest's own debug facilities (§6.5 test 4).
6. **`14 §G-4`'s witness discipline should be promoted to a corpus-wide rule** (§4.2): a concurrency
   result reported without a witness count and a weak-arm control is not a result. `14` learned this
   the expensive way and the lesson is currently recorded only as a footnote.
7. **New, and it belongs in whatever document owns the flag design:** an **undefined-flag mask
   table** is a required deliverable, not a testing detail. Without it, 23 % of flag groups produce
   false divergences against a correct reference (§3.4, `MEASURED`).

---

## References

`08` feasibility synthesis; `14` gate results; `15` QEMU TCG baseline (the harness reused here);
`16` shadow-MMU costs (walk, TLBI, ASID, A/D figures); `17` precise exceptions (§7 testing strategy,
which this document extends rather than replaces); `19` SMP scaling (TSO at 8 vCPUs, and the
run-to-run variance that rules out SMP replay); `06` x86 system semantics (flags, atomics, SMC, TSC);
`docs/research/raw/fexcore-a619f6192d8935d61.md` (the FEX flag scheme, with FEX source line cites).
External: Intel SDM Vol.2 "Flags Affected" per instruction and Vol.3A Ch.6; ARM DDI 0487 (VMSAv8-64
TLB caching rules); the diy toolsuite `CITED(http://diy.inria.fr/)`; `kvm-unit-tests`
`CITED(https://gitlab.com/kvm-unit-tests/kvm-unit-tests)`; RISU
`CITED(https://git.linaro.org/people/peter.maydell/risu.git)`.
