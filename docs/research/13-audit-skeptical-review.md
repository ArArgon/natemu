# 13 — Skeptical Audit of the Research Corpus

**Date:** 2026-08-08 · **Auditor:** independent pass over `00`–`12` + raw probe artifacts
**Method:** re-decoded the raw probe output by hand against the ARM ARM instead of trusting the
probe's own printout or the summarizing docs; traced each headline number back to whether it was
*measured*, *modelled*, or *cited*.

**Verdict:** the corpus is unusually good on breadth and sourcing, and three of its four
load-bearing conclusions do not currently survive scrutiny in the form they are stated. None of
the errors below are fatal to the project; all of them change what the next experiment should be.

---

## A. Errors of fact

### A-1 — The G-1 probe's own decoder is wrong in two places (`/tmp/hv_g1_probe/run.log:66,67`)

Raw guest read: `ID_AA64MMFR0_EL1 = 0x000010000f100002`.

| Field | Bits | Raw | Probe printed | Correct (ARM DDI 0487, D24.2.75) |
|---|---|---|---|---|
| `TGran4` | [31:28] | `0x0` | SUPPORTED | **SUPPORTED** ✓ (`0b0000`=yes, `0b1111`=no, `0b0001`=yes+LPA2) |
| `TGran64` | [27:24] | `0xF` | NOT SUPPORTED | **NOT SUPPORTED** ✓ |
| `TGran16` | [23:20] | `0x1` | **NOT SUPPORTED** ✗ | **SUPPORTED** — this field uses the *inverted* encoding (`0b0000`=no, `0b0001`=yes) |
| `PARange` | [3:0] | `0x2` | **32 bits, 0 GB** ✗ | **40 bits** — corroborated by `hv_vm_config_get_max_ipa_size → 40` in the same log |

`00-index.md:31` quotes the *corrected* values, so a downstream agent silently fixed the decode
without saying so. The raw log and the index therefore disagree, and anyone who re-runs the probe
gets the wrong answer printed at them. **Fix the probe's decoder before it is trusted again.**

**Also unreported, and it matters:**

- `ASIDBits` [7:4] = `0` → **8-bit ASIDs, i.e. 256**. x86 `PCID` is 12-bit (4096). A shadow-paging
  design that wants one ARM ASID per guest `CR3` has a 16× shortfall and will hit ASID recycling
  (→ full TLB flush) far sooner than the x86 guest expects. **This is a new, unlogged design
  constraint.** It is not in `06`, `09`, or `12`.
- `ID_AA64MMFR1_EL1 = 0x11212000` → `HAFDBS` [3:0] = `0`: **no hardware access-flag/dirty-bit
  management**. This independently confirms `06`'s claim, but note it makes x86 A/D emulation
  strictly fault-driven, which `12` ranks *last* (rank 8). Given `H`-quad uncertainty and a 256-ASID
  budget, A/D fault traffic deserves re-ranking.
- Good news, also unreported: `PAN` [23:20] = `2` → **`FEAT_PAN2`**, so `AT S1E1RP/S1E1WP` really are
  available — the fault-free operand-probing trick `06 §4` relies on is confirmed present, not
  assumed. And `XNX` [31:28] = `1` (`FEAT_XNX`), which gives separate EL0/EL1 execute-never, i.e. a
  clean way to shadow x86 `SMEP`.

### A-2 — `MIDR_EL1 = 0x610f0000`, part number `0x0`

The guest sees implementer `0x61` (Apple) with **part = 0**. Real M2 cores report part IDs in the
`0x03x` range. HVF is presenting a **synthetic MIDR**. Consequences:

1. The guest cannot identify the host core, so no core-specific tuning from inside the VM.
2. More importantly: **every ID register the guest reads is HVF-mediated, not hardware.** Treat all
   G-1 ID-register results as "what Apple's hypervisor advertises", which is the right thing for
   *our* design, but do not generalise them to bare-metal Apple Silicon behaviour.

---

## B. Errors of inference — claims stronger than their evidence

### B-1 — G-1 is marked CLOSED. It is about half closed. **This is the most important item here.**

`00-index.md:31` and `09` N-02 both say G-1 is closed and conclude *"helper VM at EL1 **can** use
`TCR_EL1.TG0/TG1=4K`; 1:1 shadow paging is viable."*

Look at what the probe actually executed (`run.log:31–46`): **15 instructions, all `mrs`/`msr`/`hvc`.**
No page tables were built. `SCTLR_EL1.M` was never set. `TCR_EL1` was never written. **No address
was ever translated by a 4 KB stage-1 regime.** The conclusion is an inference from an ID register
to a working translation regime.

That inference is *probably* right — but "probably right" is exactly what a go/no-go gate is
supposed to eliminate, and this gate is the one the entire Tier-2 architecture rests on. The same
gap applies to stage 2: `run.log:87` shows a 4 KB `hv_vm_map` at a 4 KB-offset host VA *returned
success*, but **no guest access was ever made through that mapping**. A successful `hv_vm_map` is
not proof of correct 4 KB-granular translation or permission enforcement underneath a 16 KB host
page.

**What actually closes G-1:** a guest that builds a real 4 KB-granule L0–L3 table, sets
`TCR_EL1.TG0/TG1=4K` + `T0SZ=16` (48-bit VA — also never verified), enables the MMU, and then
demonstrates (a) a load through it returning the right data, (b) a *permission fault* at 4 KB
granularity on the adjacent 4 KB page inside the same 16 KB host page. Item (b) is the one that
can genuinely fail. Half a day of work.

### B-2 — TSO: the probe ran the right test, but it is a weaker test than it looks

`run.log:70` shows the guest wrote `ACTLR_EL1 = 2` and read `2` back, and `run.log:79` shows
host↔guest coherence. This *is* Asahi's own screening test (`cpufeature_impdef.c`,
`has_apple_tso()`), and it rules out the documented failure mode ("advertises TSO in `AIDR_EL1`,
then ignores writes to `ACTLR_EL1`").

But it only proves the **bit is stored**, not that the **memory model changed**. A hypervisor that
saves/restores the register without applying it passes this test. `06`'s own author flagged exactly
this: *"reading the bit back is not sufficient evidence — a litmus test is required."* No litmus
test was run. **Until one is, treat "hardware TSO works in the VM" as Medium confidence, not
Verified** — and it is worth 20–30% of system performance, so it deserves the real test: a
store-buffer-forwarding / `SB` (store-buffer) litmus pattern across two vCPUs, TSO-on vs TSO-off,
counting non-sequentially-consistent outcomes.

### B-3 — G-2's "mandatory" verdict is probably obsolete, and nobody reconciled it

`11-pte-quad-gate.md:11` states the premise plainly: 1:1 shadow paging is possible *"only at 4K
stage-1 — gated on G-1 — but stage-2 `hv_vm_map` and host `mmap` default to 16K."*

G-1 then returned `TGran4 = supported`, **and** the same log (`run.log:13,84–87`) shows
`HV_IPA_GRANULE_4K` is settable and 4 KB `hv_vm_map` succeeds on macOS 26. If shadow stage-1 is
4 KB and stage-2 is 4 KB, **each guest 4 KB PTE maps to one shadow PTE and the quad problem largely
dissolves** — quads were only ever needed to paper over a 16 KB shadow granule.

Yet `00-index.md:20,32` still reports **"G-2 = mandatory"** and `12` still charges 15–35% of the
MMU win to quad heterogeneity (rank 6). These cannot both be current. Note the word "default" in
`11:11` — the doc was reasoning about the *default* granule and appears not to have been revised
after the 4 KB granule turned out to be *selectable*. **`11` and `12` need re-deriving against the
G-1 result before `natemu-page-quads` gets any engineering time.**

### B-4 — `H = 55–87%` is a model output, not a measurement, and it is presented as data

`11 §2` labels the estimate *"reasoned, not guessed"* and `00-index.md:32` reports "Synthetic
55–87%" in a status column alongside genuinely measured gates. Two problems:

1. **Circularity.** The parameters of `tools/g2_quad_measure.py --synthetic` were chosen by the same
   agent, calibrated to a qualitative table that the same agent wrote. A model tuned to reproduce
   its author's priors is not independent evidence for those priors.
2. **The metric dominates the result.** `11 §2` reports loose-`H` of 55/66/86% and strict-`H`
   (A/D included) of 14/10/37% for the *same* workloads. When a definitional choice swings the
   headline number by 4×, the number is not yet a measurement of anything. The decision thresholds
   (`>90%` optional, `<70%` mandatory) are applied to the loose metric without justifying why loose
   is the right one.

The real dump (`--dump mem.bin --cr3 …`) is listed as a 1-day follow-up and has **not** been run
(`/tmp/guest.bin` is absent). Until it is, `H` should carry no weight in an architecture decision.

### B-5 — The Tier-2 speedup projection multiplies speedups that are not independent

`12:23`: *"MMU+TSO 1.5–2.2× + codegen 1.3–1.8× → Tier 2 2.5–5×."*

Captive's **2.21× over QEMU already includes Captive's own JIT**, its privilege mapping, *and* its
host-MMU offload — it is an end-to-end number. Multiplying it (or the 1.5× HSPT figure, which is
also end-to-end) by a separately-sourced codegen factor double-counts. The multiplicands also come
from different guests, hosts, baselines, and QEMU vintages (Captive's baseline is QEMU circa
2016–2019).

Worse, the two known deltas both cut *against* us relative to Captive: Captive's x86 host had a
**native 4 KB page** and **hardware A/D bits**; we have `HAFDBS = 0` (A-1) and a 16 KB host page.
Captive's 2.21× is better read as an **optimistic ceiling we are unlikely to reach**, not a floor to
multiply upward from. The defensible claim today is: *"the closest prior art got 2.2× over
contemporaneous QEMU on SPECint; we have no independent evidence we would do better, and two
identified reasons we might do worse."*

### B-6 — The `1.74× softMMU vs ldr` microbenchmark does not measure what it is used for

`12` rank 1 cites a live probe: a 9-instruction software-MMU sequence at 0.466 ns vs `ldr` at
0.267 ns. As a *microbenchmark* that is fine. As evidence for the whole-program MMU win it omits
every mechanism that makes softmmu actually expensive: ~9× instruction-cache footprint, the
branch to the slow path and its mispredicts, the software-TLB miss path, and the register pressure
that the sequence steals from the block's register allocator. A hot, perfectly-predicted,
fully-pipelined loop is the **best case for softmmu**, so 1.74× is a *lower bound on the cost*, not
an estimate of it. That it lands near the independently-sourced 1.51× HSPT figure is coincidence,
not corroboration — and `12` treats the agreement as confirmation.

---

## C. A contradiction between documents that has not been retracted

`07` asserts as a headline finding that **"Apple Silicon does not implement the 4 KiB translation
granule,"** verified via `hw.pagesize = 16384`. That inference is invalid — the host OS page size
says nothing about which translation granules the architecture implements — and G-1 contradicts it
directly.

This matters because `07`'s single A-grade recommendation (`natemu-page-quads` as *"the one PV
feature worth building"*) was derived from that false premise, and `11`/`12` inherited it (B-3).
`07` has not been corrected and still reads as authoritative. **`07 §3.1`, `07 §6.4`, `11`, and
`12` rank 6 form one dependency chain rooted in a refuted claim.**

---

## D. Process / hygiene risks

| # | Risk | Detail |
|---|---|---|
| D-1 | **Not a git repository** | ~745 KB of research across 13 documents, no version control, no history, no attribution. Multiple agents have rewritten shared files (`09` was edited under me mid-session). There is currently **no way to tell what changed, when, or on whose evidence.** `git init` + commit is the single highest-value 60 seconds available. |
| D-2 | **Load-bearing artifacts live in `/tmp`** | `12` and `00` cite `/tmp/probe_mmu_tso.c`, `/tmp/probe_dram2.c`, `/tmp/report_mmu_tso.md`, `/tmp/d3.c`, `/tmp/probe_hv_fault.c`, `/tmp/hv_g1_probe/*`. All present now; **all lost on reboot.** Every measured claim in the corpus becomes unreproducible at that point. Move into `probes/` in-repo. |
| D-3 | **Probe sources scattered** | `probe_g1_granule.c` / `probe_g1_tgran_tso.c` sit at repo root; `tools/__pycache__` is checked in as a build artifact; `.playwright-mcp/` holds 13 unrelated browser dumps. Needs a layout and a `.gitignore`. |
| D-4 | **Single-machine, single-OS-version basis** | Every measurement is M2 Max / M2 Pro on macOS 26.5.2. The exit-scaling regression past 6 vCPUs, the 4 KB stage-2 availability (macOS 26+ only), and TSO writability (reported broken on older macOS) are all plausibly version- and silicon-specific. `10` already flags "refresh on M3/M4"; that flag should be promoted to a corpus-wide caveat. |
| D-5 | **No adversarial pass was run** | Each dossier was written by one agent and summarized by another that trusted it. The A-1 decoder bug survived into the index only because a later agent quietly patched around it. Independent verification, not summarization, is what was missing. |

---

## E. What still stands up

To be fair to the corpus — these are well-evidenced and I would build on them:

- **Rosetta-for-Linux TSO mechanism** (`04`): grounded in Apple's own MIT-licensed
  `RosettaPatch.diff`, disassembly of the shipping binary, and the Asahi/FEX source that shares the
  ABI. Multiple independent primary sources agreeing. `ACTLR_EL1` bit 1 / `AIDR_EL1` bit 9 are
  confirmed by two independent implementations. High confidence.
- **HVF exit cost ~750–810 ns** (`02`, and an independent measurement agreeing to within 8%): two
  separately-built harnesses on the same host converging. The derived **~3 M exits/s per-VM
  ceiling** is the most actionable number in the corpus.
- **`hv_vm_protect` does not flush the whole stage-2 TLB** — tested by the right experiment
  (guest throughput under concurrent protect storm), and it refutes widely-repeated folklore.
- **x86 4-level paging ↔ ARM 4 KB-granule stage-1 structural isomorphism** (`06`): this is an
  architectural fact, checkable on paper, and it is the genuine insight of the corpus.
- **`FEAT_FlagM`/`FlagM2` present on every Apple generation** — `CFINV`/`AXFLAG` for x86 flag
  emulation. Architecturally verifiable, high value.
- **FEXCore (MIT, ~90k LoC) as the frontend/IR/backend candidate** (`05`): licensing and scope
  reasoning is sound regardless of the performance disputes above.

---

## F. Recommended next actions, in order

1. **`git init` and commit everything as-is** (D-1) — before any further edits, so the corrections
   below are reviewable as diffs.
2. **Move `/tmp` probes into `probes/` and fix the G-1 decoder** (D-2, A-1).
3. **Actually close G-1**: build a 4 KB-granule stage-1 table at guest EL1, enable the MMU, prove a
   translated load *and* a 4 KB-granular permission fault inside one 16 KB host page. Also verify
   `T0SZ=16` (48-bit VA) is accepted — `09` N-01 depends on it and it was never tested. (~0.5 day,
   B-1)
4. **Run a TSO litmus test** (two vCPUs, store-buffer pattern, TSO on vs off) rather than a
   read-back. (~0.5 day, B-2)
5. **Re-derive `11` and `12` rank 6 against the 4 KB stage-2 result**, and retract or qualify `07`'s
   4 KB-granule claim and everything downstream of it. (B-3, C)
6. **Re-state the Tier-2 projection** as a single end-to-end comparison against a *current* QEMU
   TCG baseline that we measure ourselves, not as a product of literature multipliers. (B-5)
7. Only then: revisit whether `natemu-page-quads` deserves any engineering time at all.

---

*This audit deliberately does not soften its findings. The corpus is strong; the specific problem
is that summarization was mistaken for verification, and two gates were marked closed on evidence
that does not close them.*
