# 21 — Self-Modifying Code: What x86 Requires, Who Actually Does It, and What Detection Costs

**Date:** 2026-08-08 · **Host:** Apple M2 Max (8 P + 4 E), macOS 26.5.2, SDK Hypervisor 259.5.15
**Probes:** `probes/smc/smc.c` + `probes/smc/payload.h` (in-VM, guest EL1) and `probes/smc/qemu_smc.py`
(QEMU 11.0.3 x86-64 TCG, the same Alpine 3.24.1 guest `15` measured)
**Raw logs:** `probes/smc/results/`

`06 §8` rated guest-SMC detection "difficulty 5, catastrophic if wrong, 5–20 % if right", and every
number in that sentence was an estimate. `09` N-05 flagged the ARM I-cache non-coherency as a
"top-tier PV win" without measuring it. `12` rank 5 says "5–20 % if wrong, 4× false traps at 16 K".
This document measures the mechanism and, more importantly, measures **how often a real Linux guest
actually triggers it** — which turns out to be the load-bearing question, and the answer is not what
the corpus assumed.

> **Reading the labels.**
> `MEASURED` — taken on this machine today, by a probe in this repo, with a stated control.
> `SOURCE(file:line)` — read out of the named source file.
> `CITED(doc)` — quoted from a document I retrieved (SDM/APM revisions given exactly).
> `INFERRED` — my reasoning from the above. Never a measurement.
> `UNRESOLVED` — stated as open, with the experiment that would close it.

---

## Bottom line

### The three findings that change the design

**1. Steady-state SMC in a Linux guest is zero, and boot-time SMC is ~17 000 events, once.**
`MEASURED` (`probes/smc/qemu_smc.py`, QEMU's own `tb_phys_invalidate` counter, sampled 5–50×/s
over the whole run): an Alpine 3.24.1 x86-64 guest invalidates **17 086–17 313 translation blocks
during boot** and then **exactly zero** during 5.6 s of compute benchmarks, 1.4 s of 400
`fork`+`exec`, and **30.5 s of 7-Zip LZMA**. Eight independent boots; the boot figure is invariant
to `-smp 1` vs `-smp 4`, to `mitigations=off`, to `nokaslr`, and to `ftrace=function`.

**2. Boot-time kernel self-patching is wide but early, and that is why it is cheap.**
`SOURCE`: a stock x86-64 kernel patches **83 % of its own text pages** at boot (alternatives,
retpolines, return thunks, ENDBR sealing, ftrace NOPs, jump labels — 124 k–345 k sites) while
touching only **3.3 % of its text bytes**. `MEASURED`: that entire storm produces only ~5 100 TB
invalidations, because `alternative_instructions()` runs before most of the kernel has ever
executed and therefore before most of it has been translated. **The largest single invalidation
event in a Linux boot is not patching at all** — 69 % of the total lands in the last 120 ms of
boot, coinciding with the kernel freeing and poisoning `.init.text` (`INFERRED`, §2.4).

**3. `IC IVAU` broadcast does *not* degrade under SMP contention — unless the other cores are also
doing cache maintenance.** `MEASURED`, 3 sweeps: at 8 vCPUs, invalidating a 4 KiB code region costs
**0.62–0.93× its solo cost** when the other 7 vCPUs are idle, walking memory, executing their own
code, **or executing the very lines being invalidated** (a true cross-core I-cache shootdown). It
costs **2.16–2.40×** when the other 7 vCPUs are issuing their own `IC IVAU`s. This is the opposite
shape from `19`'s `TLBI` result (3.7–4.5× with merely-busy contenders) and it means I-cache
maintenance is **not** the SMP hazard `09` N-05 feared.

### Every number I measured

| # | Quantity | Value | Tag |
|---|---|---|---|
| 1 | `CTR_EL0` at guest EL1 | `0x8444c004`: **IDC=0, DIC=0**, IminLine = DminLine = **64 B**, PIPT I-cache | `M` |
| 2 | `DC CVAU` / `IC IVAU` / `IC IALLU` at guest EL1 under HVF | execute natively, **0 EL2 traps** | `M` |
| 3 | Write fault on a write-protected code page, handler skips the store | **28.3 ns** (27.0–28.3, n=3), 91 cyc | `M` |
| 4 | …control: same loop, same addresses, page writable | 0.65 ns | `M` |
| 5 | …+ read `ESR_EL1` + `FAR_EL1` + the faulting instruction word | +0.65 ns | `M` |
| 6 | …+ **classify the frame** (shadow L3 → OA → per-frame kind byte) | **+1.95 ns** | `M` |
| 7 | …+ emulate the store through a writable alias of the same frame | +0.3…+1.6 ns | `M` |
| 8 | `ESR_EL1` for a stage-1 write permission fault at EL1 | `0x9600004f`, DFSC `0x0f`, WnR=1, **ISV=0** | `M` |
| 9 | Full protect→fault→unprotect→write→re-protect cycle | **682–822 ns**, of which **61–83 % is TLB maintenance** | `M` |
| 10 | `DSB`+`TLBI VAAE1IS`+`DSB`+`ISB` | 250–282 ns (confirms `16`'s 279) | `M` |
| 11 | Does un-write-protecting (RO→RW) need a `TLBI`? | **Yes. 51 200 / 51 200 retries re-faulted without one** | `M` |
| 12 | `DC CVAU` over 4 KiB of rewritten code + `DSB`+`ISB` | **94–100 ns** (1.45–1.55 ns per 64 B line) | `M` |
| 13 | `IC IVAU` over 4 KiB + `DSB`+`ISB` | **420–760 ns** (multimodal; see §4.3) | `M` |
| 14 | Canonical `dc×N; dsb; ic×N; dsb; isb` over 4 KiB | **490–850 ns** | `M` |
| 15 | `IC IALLU` + `DSB` + `ISB` | **110–174 ns, independent of range size** | `M` |
| 16 | Crossover: `IC IALLU` beats per-line `IC IVAU` above | **~1 KiB** | `M` |
| 17 | Execute 4 KiB of code (1023 `add`s), I-cache warm | 269–304 ns (0.28 ns/insn) | `M` |
| 18 | `IC IVAU` 4 KiB **then execute it** (invalidate + refill) | 880–1035 ns | `M` |
| 19 | `IC IALLU` **then execute** the same 4 KiB | **576–640 ns** | `M` |
| 20 | **Whole SMC turnaround**: rewrite 4 KiB + canonical maintenance + execute | **1650–2015 ns** | `M` |
| 21 | …the rewrite stores alone (64 word stores) | 24 ns | `M` |
| 22 | `IC IVAU` 4 KiB at 8 vCPUs, others idle / walking / executing / **executing the same lines** | **0.62–0.93×** solo | `M` |
| 23 | `IC IVAU` 4 KiB at 8 vCPUs, others also issuing `IC IVAU` | **2.16–2.40×** | `M` |
| 24 | `IC IALLU` at 8 vCPUs, others issuing `IC IVAU` | 1.60–4.07× | `M` |
| 25 | Whole SMC turnaround at 8 vCPUs, others issuing `IC IVAU` | 1.71–1.85× | `M` |
| 26 | Byte-compare a 4 KiB code page against a shadow copy | **118–120 ns** (34.6 GB/s) | `M` |
| 27 | 64-bit multiply-accumulate checksum of a 4 KiB page | **244–251 ns** | `M` |
| 28 | …the same compare when it mismatches in the first 16 bytes | 0.65 ns | `M` |
| 29 | Alpine x86-64 Linux boot, TB invalidations (8 boots) | **17 086 – 17 313** | `M` |
| 30 | …during `alu4`/`ls4`/`nqueens` (5.6 s), 400×`fork`+`exec` (1.4 s), 7-Zip LZMA (30.5 s) | **0, 0, 0** | `M` |
| 31 | Boot invalidations, `-smp 4` / `mitigations=off` / `nokaslr` / `ftrace=function` | 17 094 / 17 195 / 17 192 / 17 238 — **invariant** | `M` |
| 32 | Kernel text pages dirtied by boot self-patching (Alpine 6.12.81) | **83 %** of code pages, **3.3 %** of code bytes | `SOURCE` |

### The one sentence

> **The expensive part of SMC detection is not detecting it. It is the TLB maintenance you do
> around it (500–565 ns per protect/unprotect pair, 61–83 % of the cycle) and the I-cache
> maintenance you do after it (490–850 ns per 4 KiB page). The fault itself is 28 ns. And on a
> non-JIT Linux guest you take zero of them after boot.**

---

## 0. Method, and its guards

**In-VM probe** (`probes/smc/smc.c`, build `probes/build.sh smc/smc.c`). Guest EL1 payload under our
own `VBAR_EL1`, 4 KiB stage-1 (`TCR_EL1.T0SZ=16`, per `14 §G-1a`), all timing from `CNTVCT_EL0`
read inside the VM, so **no host exit occurs inside any measured window**. The probe counts EL2
traps (`EC 0x18`) across every run and reports them; the count was **0** everywhere, which is itself
result #2 above — `DC CVAU`, `IC IVAU` and `IC IALLU` are not trapped by HVF's `HCR_EL2`.

* **The vCPU starts at EL0.** `hv_vcpu_set_reg(vcpu, HV_REG_CPSR, 0x3c5)` (EL1h, DAIF masked) before
  the first `hv_vcpu_run`, or the payload's first `MRS` vectors to `VBAR+0x400`.
* **Matched controls.** Every fault figure is a difference against a control that runs the
  *identical* instruction sequence over the *identical* address stream, differing only in the shadow
  L3's `AP[2]` bit.
* **A cache-set trap that bit `16` and bit me.** The first version of the store loop used
  `base + page*4096`, i.e. offset 0 in every page, so all 512 stores mapped to one L1 cache set and
  the *control* paid conflict misses the faulting arm never pays (control read 7.16 ns instead of
  0.65). The stream now rotates the intra-page offset over 64 lines. This inflates nothing and
  deflates nothing — it just stops the control from being wrong. `16 §Method` documents the same
  guard; it is easy to lose when writing a new probe.
* **Counter quantisation is a real hazard here.** `CNTVCT_EL0` ticks at 24 MHz (41.67 ns). With 16
  stores per timed sample the quantum is 2.6 ns *per store*, and every handler variant reported
  exactly `28.65 ns` — the differences between them were smaller than one tick. The `wp` experiment
  now batches 128 stores per sample (0.33 ns quantum). **Any probe in this corpus that reports
  sub-10-ns differences must state its batch size.**
* **Repetition.** `wp` and `cycle`: 3 independent process invocations, each 512 pages × 400 (resp.
  100) rounds. `icache`: 15 rounds per body per size. `smp`: 3 full sweeps, 21 rounds per body, each
  timed sample covering 1024 whole-region passes, and **every N is bracketed by a freshly measured
  N=1 run** so drift cannot masquerade as scaling (the `19` protocol).
* **Guest-rate probe** (`probes/smc/qemu_smc.py`). Boots the x86-64 Alpine guest under
  `qemu-system-x86_64 11.0.3 -accel tcg,thread=single` (byte-identical configuration to `15 §2.3`),
  attaches a QMP socket, and polls `info jit` every 20–200 ms for the entire run, recording
  `TB count`, **`TB invalidate count`** (= `tb_ctx.tb_phys_invalidate_count`), `gen code size` and the
  TLB-flush counters, plus host-timestamped serial output. It re-uses
  `probes/qemu_baseline/work/`; it downloads nothing and writes nothing outside `probes/smc/`.
  The boot figure reproduces `15 §4.1`'s one-shot `TB invalidate count 17238` to within 0.9 %.

**What the QEMU counter is and is not.** `tb_phys_invalidate()` is bumped once per *translation
block* destroyed. In QEMU full-system that happens when a guest store lands on a guest-physical page
from which a TB was translated (via the `TLB_NOTDIRTY` / `DIRTY_MEMORY_CODE` path), and when a RAM
region is removed. It is therefore a direct, in-vivo count of **"guest wrote memory we had already
translated"** — exactly the event this document is about. It is *not* a count of guest writes (one
write can kill many TBs) and it is *not* a count of pages (§6 converts). It is a **lower** bound on
what we would see, because QEMU flushes its whole TB cache on some events and re-translates lazily —
but `TB flush count` was **0** in every run, so no such flush contaminates these numbers.

---

## 1 (A) — What x86 actually requires

The SDM section numbers everyone quotes (`8.1.3`, `11.6`) have been renumbered. Quotes below are
`CITED` verbatim from **Intel SDM Vol 3, order 325384-092US, June 2026** (`§11.1.3`, `§14.6`,
`§14.7`, `§25.29.1`) and **AMD64 APM Vol 2, publication 24593 Rev 3.42, March 2024** (`§7.6.1`),
extracted from the official PDFs. The 2016 edition (325384-059US, `§8.1.3`/`§11.6`) is word-for-word
identical apart from `←` vs `:=` in the pseudocode.

### 1.1 Same-processor SMC — Intel *recommends*, hardware *guarantees*, software *depends*

> **§11.1.3 Handling Self- and Cross-Modifying Code**
> The act of a processor writing data into a currently executing code segment with the intent of
> executing that data as code is called self-modifying code. IA-32 processors exhibit
> model-specific behavior when executing self-modified code, depending upon how far ahead of the
> current execution pointer the code has been modified.
> […] To write self-modifying code and ensure that it is compliant with current and future versions
> of the IA-32 architectures, use one of the following coding options:
> ```
>     (* OPTION 1 *)
>     Store modified code (as data) into code segment;
>     Jump to new code or an intermediate location;
>     Execute new code;
>
>     (* OPTION 2 *)
>     Store modified code (as data) into code segment;
>     Execute a serializing instruction; (* For example, CPUID instruction *)
>     Execute new code;
> ```
> The use of one of these options is not required for programs intended to run on the Pentium or
> Intel486 processors, but are recommended to ensure compatibility with the P6 and more recent
> processor families.

And the hardware mechanism, §14.6:

> A write to a memory location in a code segment that is currently cached in the processor causes
> the associated cache line (or lines) to be invalidated. **This check is based on the physical
> address of the instruction.** In addition, the P6 family and Pentium processors check whether a
> write to a code segment may modify an instruction that has been prefetched for execution. If the
> write affects a prefetched instruction, the prefetch queue is invalidated. **This latter check is
> based on the linear address of the instruction.**

Two things follow that the corpus has half-stated:

* **The cache-invalidation half is physical-address-based and unconditional.** No software action is
  required for it. There is *no* SDM statement that `CLFLUSH` is needed for same-processor SMC — I
  searched Vol 2 and Vol 3 of 325384-092US and found nothing of the kind. Option 1/Option 2 exist
  only to flush the *prefetch/decode* pipeline, and only on P6+.
* **The prefetch-queue half is linear-address-based**, which is why §25.29.1 says: *"special care
  must be taken for self-modifying code to work correctly when the physical addresses of the
  instruction and the written data are the same, but the linear addresses differ."* This is exactly
  Linux's `text_poke` case, and it is exactly why Linux calls `sync_core()` there (§2.2).

AMD is stronger and simpler, and states the guarantee explicitly (APM Vol 2 §7.6.1):

> **Self-Modifying Code.** […] To support self-modifying code, AMD64 processors will flush any lines
> from the instruction cache that such stores hit, and will additionally check whether an
> instruction being modified is already in decode or execution behind the store instruction. If so,
> it will flush the pipeline and restart instruction fetch to acquire and re-decode the updated
> instruction bytes. **No special action is needed by software for such updates to be immediately
> recognized.** As with cache coherency, the check for instructions that are in flight is performed
> using physical addresses to avoid aliasing issues that could arise with virtual (linear) addresses.

### 1.2 Cross-modifying code — Intel *requires* a protocol

> The act of one processor writing data into the currently executing code segment of a second
> processor […] is called cross-modifying code. […] To write cross-modifying code and ensure that it
> is compliant with current and future versions of the IA-32 architecture, **the following processor
> synchronization algorithm must be implemented**:
> ```
>    (* Action of Modifying Processor *)
>    Memory_Flag := 0; (* Set Memory_Flag to value other than 1 *)
>    Store modified code (as data) into code segment;
>    Memory_Flag := 1;
>
>    (* Action of Executing Processor *)
>    WHILE (Memory_Flag ≠ 1)
>         Wait for code to update;
>    ELIHW;
>    Execute serializing instruction; (* For example, CPUID instruction *)
>    Begin executing modified code;
> ```

**Correction to a claim the corpus is likely to make:** there is **no** SDM sentence saying the
processor "does not guarantee" XMC without the protocol. The normative force is entirely in *"must
be implemented"*. AMD is looser still — it explicitly blesses an *asynchronous* XMC protocol with no
handshake at all, provided the store is "contained entirely within a naturally-aligned quadword",
and enumerates the three tearing outcomes that remain possible (APM Vol 2 §7.6.1).

### 1.3 Which of these is an emulator obliged to honour?

**Both, and honouring same-processor SMC exactly is the easy one — because of how we detect it.**

This inverts the framing in the task and in `06 §8.1`. The reasoning:

* We do not detect SMC by watching the *fetch*. We detect the **write**, synchronously, by a
  hardware permission fault taken before the store retires. At the moment we regain control the
  store has not happened. Whatever we do next — invalidate translations, then let the write land —
  is ordered strictly *before* any subsequent instruction on that vCPU, and therefore before any
  fetch of the modified bytes. **We satisfy Option 1 and Option 2 without the guest executing
  either.** This is strictly more conservative than x86 requires, for free.
* The linear-vs-physical asymmetry (§1.1) is also handled for free *provided* the protection is
  keyed on the guest-**physical** frame. It is not handled at all if it is keyed on the guest
  virtual address. `06 §8.4` already says this; §2.2 below gives the `SOURCE` that proves it is
  not hypothetical.
* **XMC is where an emulator may legitimately punt — and should not.** The SDM permits us to assume
  the executing processor runs a serializing instruction. If we took that permission we could defer
  cross-vCPU invalidation until the other vCPU serialises. But (a) our detection is synchronous
  anyway, so the extra cost of being correct is a cross-vCPU rendezvous, not a new mechanism; and
  (b) `MEASURED` #22 says the cross-core cost is *nil* on this hardware — 7 vCPUs executing the very
  lines being invalidated cost 0.80–0.93× of solo. **There is nothing to buy by punting.**
* What we *may* punt on: the tearing behaviour of a write that straddles an instruction boundary
  while another vCPU is mid-fetch. x86's answer is model-specific; ours is "the write is atomic
  with respect to invalidation because we hold the writer at a fault". That is a *stronger*
  guarantee than any real x86, so no guest can distinguish it.

**The one thing we must not do** is the reverse trade: rely on the guest telling us. `07 §3.2`
grades PV SMC notification C and is right — but its reason ("kernel patching is rare and cheap")
is now backed by measurement (§2), and its other reason ("x86 has nothing to hook in userspace") is
confirmed by `SOURCE`: V8's `FlushInstructionCache` on x64 is *an empty function body*
(`src/codegen/x64/cpu-x64.cc:17-36`). There is literally no call site to intercept.

---

## 2 (B) — Who actually writes code in a Linux guest, and how often

### 2.1 The measurement first

`MEASURED` (`probes/smc/results/jit.*.json`, QEMU `TB invalidate count`, one Alpine 3.24.1 x86-64
guest, kernel 6.18.35-virt, no disk, no network):

| Run | Boot invalidations | Task | Task duration | Invalidations during the task |
|---|---|---|---|---|
| `boot.fine` | 17 100 | — | — | — |
| `bench` | 17 086 | `alu4`,`ls4`,`nqueens` | 5.64 s | **0** |
| `spawn` | 17 102 | 400 × `fork`+`exec` of busybox | 1.38 s | **0** |
| `sevenzip` | 17 100 | `7zz b -mmt1 -md=22` | 30.54 s | **0** |
| `boot.smp4` | 17 094 | — (`-smp 4`) | — | — |
| `boot.mitoff` | 17 195 | `mitigations=off` | — | — |
| `boot.nokaslr` | 17 192 | `nokaslr` | — | — |
| `boot.ftrace` | 17 238 | `ftrace=function` | — | — |

TB *count* keeps growing during the tasks (+2 646, +5 013, +11 143) — that is **new translation**,
not re-translation. `TB flush count` was 0 throughout, so nothing is hidden behind a wholesale flush.

> **In 37.5 s of real guest work spanning compute, a kernel-heavy process-creation storm and a
> genuine application, a real x86-64 Linux guest caused not one single translated-code
> invalidation.**

Caveats, stated before anyone quotes this:

* **No JIT, no module load, no disk.** This guest is an initramfs with busybox, a static benchmark
  and `7zz`. There is no JVM, no V8, no `.NET`, no BPF program being loaded, no `insmod`, and no
  paging of executable file pages from a block device. Those are precisely the steady-state code
  writers (§2.5), and this measurement says nothing about them.
* **`fork`+`exec` produces zero invalidations for a structural reason worth understanding.** 400
  execs of busybox reuse the *same physical text pages* every time (busybox lives in tmpfs, backed
  by RAM that is never freed). QEMU keys TBs on guest-physical address, so a new address space is
  free. `MEASURED`: `TLB partial flushes` climbed 41 862 → 49 044 over those 400 spawns (~12 per
  `fork`+`exec`), so the address-space churn is real — it just isn't *code* churn. **`15 §7.2`'s
  31× process-creation slowdown is a TLB/address-space problem, not an SMC problem.** A guest that
  demand-pages executable file pages from a disk *would* invalidate on reclaim; that is unmeasured.
* **The `ftrace=function` null result is INCONCLUSIVE, not evidence.** No ftrace messages appeared
  on the console, so I could not confirm `CONFIG_FUNCTION_TRACER` is enabled in Alpine's `virt`
  kernel at all. Do not read it as "enabling the function tracer is free".

### 2.2 Boot-time kernel self-patching — wide, sparse, and *early*

`SOURCE`, Alpine `linux-lts` 6.12.81 x86-64 (patch-site tables parsed out of the decompressed
vmlinux; `.text` = 16 MiB, ~3 844 pages of real code):

| Mechanism | Section | Sites | Bytes written | Distinct text pages | % of code pages |
|---|---|---:|---:|---:|---:|
| `apply_alternatives` | `.altinstructions` | 7 726 | 44 791 | 690 | 18.0 % |
| — of which ex-paravirt | `ALT_FLAG_DIRECT_CALL` | 3 808 | — | — | — |
| `apply_retpolines` | `.retpoline_sites` | 8 479 | ~5–6 B/site | 1 837 | 47.8 % |
| `apply_returns` | `.return_sites` | 53 554 | 267 770 | 3 167 | 82.4 % |
| `apply_seal_endbr` | `.ibt_endbr_seal` | 11 449 | 45 796 | 1 775 | 46.2 % |
| `callthunks_patch_builtin_calls` | `.call_sites` | 221 480 | 1 107 400 | 3 216 | 83.7 % |
| `alternatives_smp_unlock` | `.smp_locks` | 9 145 | 1 B/site | 1 705 | 44.4 % |
| `ftrace_init` | `__mcount_loc` | 43 186 | 215 930 | 2 946 | 76.6 % |
| `jump_label_init` | `__jump_table` | 7 443 | 2 or 5 B/site | 1 405 | 36.6 % |
| `static_call_init` | `.static_call_sites` | 6 105 | 5 B/site | 1 137 | 29.6 % |

Union: **3 195 of 3 844 code pages (83.1 %)** on a default boot, 3 224 (83.9 %) if
`X86_FEATURE_CALL_DEPTH` enables call thunks. Median **155 bytes patched per touched page**; total
546 940 bytes = **3.26 % of `.text`**.

**Correction to the brief and to `06 §8.4`:** `apply_paravirt()` and `.parainstructions` **no longer
exist**. Paravirt patching was folded into the alternatives table via `ALT_FLAG_DIRECT_CALL`
(`SOURCE arch/x86/include/asm/alternative.h:13`, handler `alt_replace_call()` at
`arch/x86/kernel/alternative.c:395-433`); 3 808 of the 7 726 alternative sites *are* the ex-paravirt
indirect→direct call rewrites.

**Ordering is what makes this cheap.** `SOURCE init/main.c`: `jump_label_init()` :927 →
`mm_core_init()` :964 → `poking_init()` :965 → `ftrace_init()` :966 → `arch_cpu_finalize_init()`
:1071 (which calls `alternative_instructions()` from `arch/x86/kernel/cpu/common.c:2392`) →
`rest_init()` :1105 → `smp_init()` :1572 → `mark_rodata_ro()` :1443. The comment at
`alternative.c:441-442` is explicit: *"This runs before SMP is initialized to avoid SMP problems
with self modifying code."* All of it therefore runs **single-CPU, with text still plain RW**, via
`text_poke_early()` — a bare `memcpy` plus one local `sync_core()`, no IPI, no alias
(`SOURCE alternative.c:1751-1775`).

`MEASURED` consequence: that entire 83 %-of-pages storm costs **~5 100 TB invalidations** (the early
burst cluster, §2.4), not 3 200 pages' worth. At the moment `alternative_instructions()` runs, only
about 100 KB of kernel code has ever executed and therefore been translated (5 100 TBs × QEMU's
measured 20 target bytes/TB, `15 §4.1`). **We only pay to invalidate what we have already
translated, and boot patches code that has not run yet.**

This is the reconciliation between "83 % of pages get patched" (alarming) and "17 000 invalidations
total" (trivial), and it is the single most useful thing in this section.

### 2.3 The `text_poke` alias, and the XMC protocol Linux already pays for

`SOURCE arch/x86/kernel/alternative.c:1858-1962` (`__text_poke`):

```c
	pgprot = __pgprot(pgprot_val(PAGE_KERNEL) & ~_PAGE_GLOBAL);
	ptep = get_locked_pte(poking_mm, poking_addr, &ptl);
	local_irq_save(flags);
	pte = mk_pte(pages[0], pgprot);
	set_pte_at(poking_mm, poking_addr, ptep, pte);
	prev = use_temporary_mm(poking_mm);
	func((u8 *)poking_addr + offset_in_page(addr), src, len);   /* the actual write */
	barrier();
	pte_clear(poking_mm, poking_addr, ptep);
	unuse_temporary_mm(prev);
	flush_tlb_mm_range(poking_mm, poking_addr, ..., PAGE_SHIFT, false);
```

`poking_mm` is a whole private `mm_struct` with a randomized `poking_addr`
(`SOURCE arch/x86/mm/init.c:812-844`). **The write VA and the execute VA are different; only the
physical page is shared.** `06 §8.4`'s warning is confirmed at source: **virtual-address-keyed SMC
detection is simply wrong for Linux.**

And `SOURCE alternative.c:2078-2086`:

```c
static void do_sync_core(void *info) { sync_core(); }
void text_poke_sync(void) { on_each_cpu(do_sync_core, NULL, 1); }
```

`text_poke_bp_batch` (`SOURCE :2254-2385`) issues up to **three** of these per batch of ≤ 256 sites
(`TP_VEC_MAX = PAGE_SIZE/16`, `:2229-2230`). **The guest already implements and already pays for the
full XMC protocol of §1.2, at every runtime patch site.** Anything a PV interface could offer here
is a strictly smaller saving than `07 §3.2` assumed, because the serialization points already exist
and are already synchronous.

### 2.4 Where the 17 100 actually land

`MEASURED`, 20 ms polling, bursts of > 50 invalidations per poll:

```
  0.65..0.71s  +1522     |
  0.81..0.87s   +725     |  early cluster:  ~5 100 total  (30%)
  0.87..0.93s  +1330     |
  0.93..0.98s  +1615     |
  0.98..1.04s   +101     |
  2.20..2.26s  +6256     |  late cluster:  ~11 800 total  (69%)
  2.26..2.32s  +5520     |
```

Split by cluster across six boots: early **5 070–5 293**, late **11 684–11 776**. Both halves are
stable to ±2 %, and neither moves with `mitigations=off`.

Guest kernel timeline for the same boot (`printk.time=1`, kernel clock): `Freeing SMP alternatives
memory: 40K` @ 0.515, `Unpacking initramfs...` @ 1.015, `Freeing initrd memory: 14244K` @ 1.354,
**`Freeing unused kernel image (initmem) memory: 2996K` @ 1.589**, `(text/rodata gap) 1732K` @ 1.592,
`Run /init as init process` @ 1.593. The host-clock offset (from `NATEMU_INIT_UP` at host 2.37 s) is
≈ 0.78 s, which puts the late cluster at kernel time ≈ 1.42–1.54 s — immediately before the initmem
free is *printed*.

`INFERRED`: the late cluster is `free_init_pages()` in `arch/x86/mm/init.c`, which `set_memory_rw`s
and then **`memset`s `POISON_FREE_INITMEM` (0xCC) over ~3 MB of `.init.text` that has just finished
executing**, plus the text/rodata and rodata/data gaps. That is ~750–1 200 pages of freshly executed,
freshly translated code overwritten with `int3` in one burst. It fits the magnitude, the timing and
the mechanism.

`UNRESOLVED`, and the experiment: I could not confirm the attribution. `keepinitrd` is not an x86
parameter (`Unknown kernel command line parameters "keepinitrd"`), and QEMU exposes no per-event
detail. **The experiment that settles it:** patch `tb_phys_invalidate()` to log
`(tb->page_addr[0], cpu->cc->get_pc(cpu))` to a ring buffer, or write a `-d` log line, and
histogram the invalidations by guest physical page and by the RIP of the writing instruction. One
run, one afternoon, and it also generalises to any other guest.

**Why the attribution matters more than it looks.** If the largest SMC event in a Linux boot is a
*memset over freed code*, then the dominant case is **not** "the guest patched an instruction". It
is **"the guest freed a code page and is reusing it as data"** — an *unmap/free* event with 4 KiB
granularity, no partial-instruction hazard, no XMC hazard, and no need for byte-precision. That is a
much easier problem, and it argues for handling page-lifetime events explicitly rather than treating
everything as byte-level SMC.

### 2.5 Steady-state writers — what exists, and what nobody has measured

`SOURCE`, all at Linux v6.12 unless noted:

* **Static keys / jump labels.** 7 443 sites. Runtime flips go `arch_jump_label_transform_queue` →
  `text_poke_queue` → `text_poke_finish` (`arch/x86/kernel/jump_label.c:123-147`), 2 or 5 bytes each,
  through the alias, with the INT3 protocol. **Rate: no data.** There is no in-kernel counter and no
  published measurement. It is strictly event-driven and **0/s in a quiescent system**. The kernel
  ships a rate limiter (`include/linux/jump_label_ratelimit.h`) whose canonical user is
  `netstamp_needed_key` (`net/core/dev.c:2111-2162`) — source-level evidence that bursty flipping is
  a known problem, not a measurement of its rate.
* **ftrace.** 43 186 `__mcount_loc` sites (46 742 in the 6.18 Alpine kernel in this repo). All are
  converted to 5-byte NOPs **at boot** via `text_poke_early` (`ftrace_poke_late` is still 0,
  `SOURCE arch/x86/kernel/ftrace.c:39,112-126`). Runtime enable goes through `text_poke_queue`, i.e.
  169 batches ≈ **507 global IPI rounds** for a full `function` tracer enable (`INFERRED` from the
  256-site batch limit).
* **kprobes.** 1 byte (`int3`) per probe through the alias, plus a full `on_each_cpu` round
  (`SOURCE arch/x86/kernel/kprobes/core.c:815-822`). **273 unrelated probe slots share one 4 KiB
  executable page** (`slots_per_page` with `MAX_INSN_SIZE = 15`).
* **uprobes** do *not* write user text. `SOURCE kernel/events/uprobes.c:467-556`: allocate a fresh
  anon page, `copy_highpage` the original, patch one byte through a kernel `kmap`, then
  `__replace_page`. **A whole new physical frame appears behind the user's executable VA.** For a
  physical-address-keyed translator this is an *unmap + map*, not an SMC event — and it silently
  un-shares that page from the page cache for every mm that has the probe.
* **Module load.** `execmem_alloc` returns `PAGE_KERNEL`, i.e. **RW and NX**
  (`SOURCE arch/x86/mm/init.c:1056-1078`); relocations are a plain `memcpy`
  (`arch/x86/kernel/module.c:174-199`); then `module_finalize` runs the *entire* §2.2 battery again
  per module; then `module_enable_text_rox` flips it at the same VA. `MEASURED` over 4 223 Debian
  6.12.100 modules: median `.text` **3 199 B** (1 page), mean 18 170, p90 29 237, max 6.87 MB
  (`amdgpu`); median 13 mcount sites, 13 return sites. **Per module: one fresh contiguous code
  region, written wholesale, patched at ~65 sites, then made executable.** Modules loaded on a
  typical boot: **no data**.
* **BPF JIT — and this is the one that will hurt.** The JIT emits into an ordinary RW buffer and
  publishes via `bpf_arch_text_copy` → `text_poke_copy` → `__text_poke`
  (`SOURCE arch/x86/net/bpf_jit_comp.c:3509-3512`), so publication is aliased. But
  `SOURCE kernel/bpf/core.c:863-925`:
  ```c
   * The prog_pack allocator uses HPAGE_PMD_SIZE page (2MB on x86) to host BPF programs.
  #define BPF_PROG_CHUNK_SIZE	(1 << 6)      /* 64 bytes */
  #define BPF_PROG_PACK_SIZE (SZ_2M * num_possible_nodes())
  ```
  **Up to 64 independent BPF programs, and BPF trampolines, share one 4 KiB page.** Live patching of
  running JIT text (`bpf_arch_text_poke` for fentry/fexit attach, `bpf_arch_poke_desc_update` for
  tail calls) is 5 bytes at a time via `text_poke_bp`.
* **Userspace dynamic linker: the GOT is data.** `MEASURED` over 106 Debian `coreutils` binaries:
  **0** with `DT_TEXTREL`, **0** with an RWX `PT_LOAD`, **0** with a separate `.got.plt`. `/usr/bin/ls`
  puts `.plt` in `LOAD R-X` and `.got` in `LOAD RW-`, different segments, no shared page.
  `SOURCE glibc 2.40 sysdeps/x86_64/dl-machine.h:218-224, 465-470, 500-517`: PLT/GOT fixups and
  IRELATIVE ifunc results are plain stores to `.got`, no `mprotect`, never executable.
  ⚠ **One new exception, and it lands on the version you would pin:** glibc **2.40 introduced x86-64
  PLT rewriting** (`SOURCE sysdeps/x86_64/dl-machine.h:679-714`) which *does* `mprotect` text
  writable and rewrite instructions in place — but it is gated on
  `GLIBC_TUNABLES=glibc.cpu.plt_rewrite=1|2` and defaults to **0** (`manual/tunables.texi:60`).
* **HotSpot.** Code cache is **RWX in one mapping** on Linux — same VA for write and execute, no
  alias, no W^X (`SOURCE src/hotspot/os/linux/os_linux.cpp:2743-2765`). Inline-cache transitions are
  **one aligned 4-byte store into a live `call rel32`**
  (`cpu/x86/nativeInst_x86.cpp:267-294`: *"we cannot rely on locks here, since the free-running
  threads must run at full speed"*). Deopt is one atomic 8-byte store. ⚠ **`ICache::invalidate_range`
  on x86 is not a no-op** — the stale comment says it is, but the generated stub is
  `mfence; {clflush; add; dec; jnz}; mfence` (`cpu/x86/icache_x86.cpp:31-69`). **Code and mutable
  data share pages by construction:** a `CodeBlob` is header + relocations + instructions + stubs +
  oops + metadata in one contiguous RWX allocation, and the nmethod header (`_state`, `_gc_epoch`,
  `_is_unloading_state`) is mutated at steady state *on the same 4 KiB page as instructions*.
* **V8.** Same VA for write and execute; the write window is a thread-local `WRPKRU` toggle, not an
  `mprotect` (`SOURCE src/common/code-memory-access.h:81-87` — *"The executable permission cannot be
  retracted with PKU"*). **`FlushInstructionCache` on x64 is an empty function**
  (`src/codegen/x64/cpu-x64.cc:17-36`). Modern V8 inline caches are **data** (`FeedbackVector` on the
  JS heap, `src/ic/ic.cc:362-400`) — the classic "V8 patches its own code constantly" belief is
  out of date on x86-64. What *does* get written live: Wasm jump-table slots, GC code-space
  compaction (`compact_code_space` is on by default, `src/objects/instruction-stream.cc:18-30`), and
  inline safepoint/handler metadata contiguous with instructions. The mutable `MemoryChunk` flag word
  sits at **offset 0 of every 256 KiB code chunk**.
* **.NET CoreCLR** is the well-behaved one: a genuine `memfd` double mapping, RX view and RW view,
  default-on since .NET 7 (`SOURCE src/coreclr/minipal/Unix/doublemapping.cpp:62,204-266`), and
  tier0→tier1 promotion is a *data* write to an interleaved data page one page away from the stub
  code (`vm/precode.h:349-354`).
* **CPython 3.13/3.14 copy-and-patch JIT is off by default** and there is **no official CPython
  Linux binary with it at all** (`SOURCE configure.ac` at both `v3.13.0` and `v3.14.0`:
  `enable_experimental_jit=no`). It never re-patches after publication.

**Rates: there is essentially no published data.** Kernel steady-state patch rate: none (observable
via `PERF_RECORD_TEXT_POKE`, hooks at `alternative.c:2348`, `kprobes/core.c:781,821,828,837`).
HotSpot nmethods/s: none. V8 code bytes/s: none. CoreCLR methods/s: none (floor: 26 methods for a
blank console app). BPF program size/count on a live system: none. **I will not invent them, and
§6's JIT scenario is therefore a parametric model, not a prediction.**

---

## 3 (C) — Mechanism design space

### 3.1 The four candidates, and what the numbers say about each

| Mechanism | What it costs us, MEASURED | Verdict |
|---|---|---|
| **Write-protect in the shadow stage-1 PT, trap on write** | 28.3 ns per write fault; 0 ns when nothing writes; 250–282 ns per `TLBI` if you change the protection | **Default.** Nothing else is close. |
| **Page hash / shadow-copy compare, checked on block entry** | 118–120 ns per 4 KiB byte-compare, 244–251 ns per 4 KiB checksum | **Reject as a general mechanism** (§3.3) |
| **No protection, re-check on entry via a generation counter** | ~1 ns per block entry (1 load + cmp + branch), but needs a *source* for the generation bump | Escalation only (§3.4) |
| **Sub-page dirty tracking** | Cannot be done by the MMU — 4 KiB is the stage-1 floor. Requires instrumenting the *writer's* translated stores | Escalation only, and the right one (§3.5) |

### 3.2 Why write-protection is cheap here specifically

`16 §1` found that a **permission fault is 3.3× cheaper than a translation fault** (28 vs 94 ns),
because VMSAv8-64 forbids caching invalid descriptors so a translation fault must re-walk — nested
through HVF's stage-2 — every time, whereas a permission fault hits in the TLB and pays only
exception entry + `ERET`. `MEASURED` here, independently: **28.32 ns** (27.02–28.32 across three
process invocations), against a 0.65 ns control. `16`'s figure reproduces exactly.

That rule — *keep shadow entries present-but-restricted rather than invalid* — is exactly what SMC
detection wants, and it is why this mechanism costs 28 ns and not 94.

`MEASURED`, and this is new: **the handler can do real work for free.** Reading `ESR_EL1`, `FAR_EL1`
and the faulting instruction word adds **0.65 ns**. Classifying the faulting frame — index the
shadow L3 (which the handler must read anyway), extract the output address, and load one byte from a
dense per-frame kind table — adds **1.95 ns**, i.e. **7 % of the fault**. Emulating the store
through a writable alias of the same physical frame adds **0.3–1.6 ns**. `16 §1`'s "an out-of-line
handler is free" generalises: at 91 cycles of unavoidable exception-entry cost, a handler has ~6
cycles of slack per nanosecond and everything we need to do fits.

`MEASURED`, and it is a hard constraint: **`ESR_EL1.ISV = 0`** for a stage-1 write permission fault
at EL1 (`ESR = 0x9600004f`). The syndrome does **not** name the store's source register. A handler
that wants to emulate the store must fetch the instruction word at `ELR_EL1` and decode it itself.
This is architectural (ISV is essentially only set for stage-2 aborts), not an Apple quirk, but it
means the "emulate the store" strategy carries an instruction decoder — shared, as it happens, with
doc 20's PTE-write emulator (§5).

### 3.3 Page-granularity vs sub-page: what the other emulators do, and why

The corpus already records the landscape (`05 §2.7, §3.3, §4.4`, `03 §1.4`, `04`), and nothing I
measured contradicts it:

* **QEMU TCG** protects at `TARGET_PAGE_SIZE` — but not with the MMU. It sets `TLB_NOTDIRTY` in the
  softmmu TLB entry's `addr_write`, so *every* store to a code page takes the slow path forever
  (`CITED 05:377-380`). QEMU can afford a permanent per-store check because every store already goes
  through softmmu. **We cannot: our entire thesis is that guest stores are native `STR`s.** This is
  the structural reason we need protection and escalation where QEMU needs neither.
* **Captive** flags physical pages from which code was translated and **write-protects them in the
  host page tables**; a write fault invalidates that page's translations, and a write to the
  *currently executing* page is handled by emulating the store and leaving via a non-local jump
  (`CITED 03:105`). That is the design recommended here, arrived at independently.
* **box64** `protectDB()`/`checkCRC` with a "hotpage" heuristic and `BOX64_DYNAREC_SMC` /
  `BOX64_DYNAREC_NOHOTPAGE` knobs (`CITED 05:506-509`) — i.e. page protection with a CRC fallback for
  pages that churn. **FEX** ships `SMCChecks = none | mtrack | full` (`CITED 05:209, 240`), `mtrack`
  being mprotect-based page tracking and `full` re-validating before every execution.
* **Rosetta 2** supports guest SMC but has no incremental path: *"every write to a page containing
  translated code invalidates and re-translates"*, and guest-generated code is *"the acknowledged
  worst case, is never AOT-cached, and re-pays translation on every process start"* (`CITED 04:1128`).

The pattern is unanimous: **page granularity by default, with an escape hatch.** Nobody does 64-byte
hardware protection because no MMU offers it. Where sub-page granularity appears (dirty bitmaps at
64/128-byte resolution) it is always maintained by *instrumented stores in the translated code*, not
by the MMU — DynamoRIO's "sandboxing" is the clearest instance.

`MEASURED` and relevant: **64 bytes is the natural sub-page unit on this hardware**, because
`CTR_EL0` gives `IminLine = DminLine = 64 B` and `IC IVAU` operates at exactly that granularity.

### 3.4 The corpus has already argued that page hashing is expensive and brittle — the numbers agree

`06 §8.3` step 2 proposes: on repeated faults to a page, unprotect it and attach a checksum check to
each block from that page, verified on **block entry**. `12` rank 5 lists "checkCRC" as an
alternative to `mprotect`.

`MEASURED`: a byte-compare of a 4 KiB page against a shadow copy costs **118–120 ns** (34.6 GB/s,
and it is cache-resident — the "colder" rotating variant is 120.1 ns, so this is not a
best-case number). A 64-bit multiply-accumulate checksum costs **244–251 ns** — *worse* than the
compare, because the compare is two independent `LDP` streams and the MAC chain is dependent.

Now the break-even, which is the part `06` did not do:

* A **fault** is paid once per *write*. 28.3 ns.
* A **hash** is paid once per *block entry*. 118 ns for a whole page; even hashing only the ~20-byte
  extent of one translation block would still cost an `LDP` pair plus a compare plus a branch —
  call it 2–4 ns — on **every entry to every block on that page**.
* QEMU's `info jit` measures **20 target bytes per TB** on this guest (`15 §4.1`). A page therefore
  holds ~200 blocks. For the hash to win, the page must be written more often than blocks on it are
  entered. **That never happens for code.** If it did, the page is not code.

> **Verdict: delete the checksum rung from `06 §8.3`'s ladder.** It is 4× more expensive than the
> fault it replaces, on the wrong axis (per-entry vs per-write), and it is unsound against a
> concurrent writer on another vCPU unless the check is atomic with the execution — which it cannot
> be. The one thing the compare is good at is **early exit**: 0.65 ns when the first 16 bytes
> differ. That makes it a fine *validator* (a cheap "did this page change?" for a page we already
> suspect), and a terrible *detector*.

### 3.5 The pathological case: one page holding hot code and hot data

This is the case that decides whether the design survives contact with a JIT, so it is worth being
precise about where it actually occurs. `MEASURED` and `SOURCE` (§2.5):

**Where it does *not* occur, contrary to `06 §8.3`:**

* Kernel `.text` and `.rodata` are page-aligned by the linker script; they do not share 4 KiB pages.
  (The kernel *maps* text with 2 MiB pages, but we shadow at 4 KiB — `14 §G-1b` proved 4 KiB
  permission enforcement inside one 16 KiB host page — so the mapping granule is irrelevant to us.)
* Userspace ELF images: `MEASURED` 0 of 106 Debian `coreutils` binaries have an R-X and an RW
  `PT_LOAD` sharing a page, and 0 have `DT_TEXTREL` or an RWX segment. The dynamic linker writes
  `.got`, which is data, in a different segment.

**Where it does occur, and badly:**

| Case | Density | Written at steady state? |
|---|---|---|
| **BPF `prog_pack`** | up to **64 programs per 4 KiB page**, 64-byte chunks, plus trampolines | yes — `bpf_arch_text_poke` on every fentry/fexit attach, `bpf_arch_poke_desc_update` on tail-call map updates |
| **kprobe insn slots** | **273 slots per page** | on every probe arm/disarm |
| **HotSpot nmethod** | header + relocs + instructions + stubs + oops + metadata contiguous | **yes, constantly** — `_state`, `_gc_epoch`, `_is_unloading_state` mutated on the same page as instructions; inline caches are 4-byte stores *into* live `call rel32` |
| **V8 code chunk** | 64-byte code alignment; inline safepoint/handler/unwinding metadata contiguous with instructions; `MemoryChunk` flag word at **offset 0 of each 256 KiB chunk** | yes — GC compaction rewrites the instruction stream; Wasm jump-table slots |

Note the shape: **it is not "code next to unrelated data". It is "code next to its own metadata,
written by the runtime that owns the code."** That matters, because it means the writer is a small,
identifiable piece of guest code — which is exactly what makes the escalation in §6.3 tractable.

The cost of getting it wrong is easy to bound. `MEASURED`: one write fault on a protected page is
28.3 ns; a fault *plus* unprotect-and-re-protect is **682–822 ns**. A HotSpot GC epoch bump that
touches 10 000 nmethod headers spread over 2 000 pages, with eager re-protection, is
2 000 × 750 ns = **1.5 ms**. Do it every 10 ms and you have burned 15 % of a core on nothing.
**Eager re-protection, not protection, is what kills you.**

---

## 4 (D) — Cost, measured

Probe: `probes/build.sh smc/smc.c`; `./smc/bin/smc [info|wp|cycle|icache|smp|hash]`.
Raw: `probes/smc/results/run-{wp-cycle,icache-1,smp-3}.log`.

### 4.1 Machine facts that constrain everything else

```
  CTR_EL0 = 0x000000008444c004
    IminLine = 64 B   DminLine = 64 B   CWG = 64 B   ERG = 64 B
    IDC = 0   -> DC CVAU to PoU is REQUIRED after writing code
    DIC = 0   -> IC IVAU is REQUIRED after writing code
    L1Ip = 3  -> PIPT instruction cache
  EL2 traps during any run: 0
```

* **Both halves of the maintenance sequence are mandatory on this silicon.** `FEAT_DIC`/`FEAT_IDC`
  would have let us drop one or both; Apple M2 offers neither. `06 §8.1`'s "the general sequence is
  `DC CVAU` → `DSB ISH` → `IC IVAU` → `DSB ISH` → `ISB`" is not merely the general case here, it is
  the required case.
* **The I-cache is PIPT.** No aliasing worries, and it means invalidation is keyed on the same thing
  our SMC tracking should be keyed on (physical address) — a small but real convenience.
* **None of it traps.** `DC CVAU`, `IC IVAU` and `IC IALLU` all execute at guest EL1 under
  Hypervisor.framework without an EL2 exit. HVF does not set `HCR_EL2.TPU`/`TPC`. Had it done so,
  every one of these would have cost an 808 ns host exit (`02`) and this design would be dead.

### 4.2 Write-protecting a code page and taking the fault

512 distinct 4 KiB pages, 400 rounds, 128 stores per timed sample, three independent process
invocations:

```
  control: page WRITABLE, store lands, no fault                   0.65 ns
  RO page, handler skips the store (bare permission fault)       28.32 ns NET   (27.02 - 28.32)
    + read ESR_EL1 + FAR_EL1 + the faulting instruction word     28.97 ns NET   (+0.65)
    + classify the frame: shadow-L3 -> OA -> per-frame kind byte 30.27 ns NET   (+1.95)
    + emulate the store via a writable alias, Rt from the insn   29.30 ns NET   (+0.98)
  ESR_EL1 = 0x9600004f  EC=0x25  DFSC=0x0f (permission, level 3)  WnR=1  ISV=0
  fault counts: 204800/204800 in every arm; classifier 204800/204800 correct
```

The classify arm reports `code=204800, page-table=0, unknown=0` — the discrimination in §5 is not a
sketch, it ran.

### 4.3 The full protect / fault / unprotect / write / re-protect cycle

512 pages × 100 rounds, three invocations. The loop stores to a read-only page; the handler clears
`AP[2]`, invalidates, and returns without advancing `ELR` so the store retries; the loop then re-sets
`AP[2]` and invalidates again.

```
  FULL cycle, handler unprotects with TLBI VAAE1IS            682 - 822 ns NET
      component: bare store over the same VA stream               0.73 ns
      component: RW->RO->RW descriptor edits, no barriers         0.49 ns
      component: dsb + TLBI VAAE1IS + dsb + isb                250 - 282 ns
      component: full permission round trip (2 edits + 2 TLBI) 495 - 636 ns
      => TLB maintenance is 500-565 ns of the cycle, i.e. 61-83 %
```

The `TLBI` figure reproduces `16 §3`'s 279.77 ns and `19`'s 208–316 ns solo range.

**And the result that matters most in this section:**

```
  FULL cycle, handler unprotects WITHOUT a TLBI                737 - 872 ns
      retries that still faulted at the same FAR:            51199 - 51200 of 51200
```

> **Un-write-protecting a page requires a `TLBI`. Every single time.** 51 200 out of 51 200 retries
> re-faulted at the same `FAR` after the descriptor was made writable with only a `DSB ISHST` + `ISB`.
>
> This is the exact counterpart of `16 §2`'s central optimisation and it must not be confused with
> it. `16` showed that turning an **INVALID** descriptor **VALID** needs no invalidate, because
> VMSAv8-64 forbids caching invalid entries — worth 3.0× on the shadow-fill path. Here the entry is
> **VALID and restrictive**, which the architecture explicitly permits the TLB to cache, and the
> hardware does cache it, deterministically. **The `TLBI` elision does not extend to SMC
> un-protection.** Any design that assumes otherwise will take an infinite fault loop.

Consequence for the design: **the ~560 ns of TLB maintenance per protect/unprotect pair is the
dominant cost of SMC handling, not the 28 ns fault**, and the only lever is to do fewer pairs — i.e.
never re-protect eagerly (§6.3).

### 4.4 I-cache and pipeline maintenance after rewriting code

The region is real executable code (`add x0,x0,#1` × N, then `ret`), identity-mapped RW+X at EL1.
Figures are per whole-region pass, medians of 15 rounds. `— ns/64B` and `ns/4KiB` are per-step
derived after subtracting the `dsb ish; isb` floor (12.6 ns).

```
  region:                          64 B      256 B     1 KiB     4 KiB    16 KiB    64 KiB
  dsb ish; isb            (floor)  12.6      12.7      12.6      12.6      12.0      13.0
  dc cvau xN; dsb; isb             14.5      17.9      35.1     106.6     392.4    1580.7
  ic ivau xN; dsb; isb            135.6     151.3     226.4     753.6    2212.9    7078.1
  ic iallu;   dsb; isb            144.6     134.8     153.4     170.9     159.8     173.8
  CANONICAL dc;dsb;ic;dsb;isb     142.6     148.6     254.9     849.5    2421.9    7537.1
  execute the region, warm          1.0       6.4      49.2     268.8    1195.2    4996.1
  ic ivau xN; dsb; isb; execute   136.1     175.6     328.9     920.8    3960.9   14908.9
  ic iallu;   dsb; isb; execute   154.6     216.3     216.3     625.7    2000.0    7447.3
  rewrite + canonical + execute   161.5     222.5     490.4    1787.8    6812.0   26857.4
  rewrite only (1 word / 64 B)      0.3       1.9       5.9      24.4      86.8     313.2
```

Per-64-byte-line and per-4 KiB-page, from the same run:

| Sequence | ns per 64 B line | **ns per 4 KiB page** |
|---|---|---|
| `dc cvau` ×N + `dsb`+`isb` | 1.45 – 1.55 | **94 – 100** |
| `ic ivau` ×N + `dsb`+`isb` | 6.9 – 11.6 (size-dependent) | **420 – 760** |
| canonical `dc;dsb;ic;dsb;isb` | 7.3 – 13.1 | **490 – 850** |
| `ic iallu` + `dsb`+`isb` | — (O(1)) | **110 – 174, any size** |

**Six things fall out, four of them not expected.**

1. **`IC IVAU` is 5–7× more expensive than `DC CVAU`.** 1.5 ns/line to clean the D-cache to PoU;
   6.9–11.6 ns/line to invalidate the I-cache. If you are optimising this sequence, optimise the
   `IC` half.
2. **`IC IALLU` is O(1) in the range and beats per-line `IC IVAU` above ~1 KiB.** At 1 KiB it is
   153 vs 226 ns; at 4 KiB, 171 vs 754; at 64 KiB, 174 vs 7 078 — **41× cheaper**. Including the
   subsequent refill (`iallu;…;execute` vs `ivau;…;execute`) it still wins at every size ≥ 1 KiB:
   626 vs 921 ns at 4 KiB, 7 447 vs 14 909 ns at 64 KiB.
   ⚠ **Caveat that must travel with this number:** in this microbenchmark the only thing in the
   I-cache *is* the region plus a tiny loop, so `IALLU`'s collateral damage is near zero. In a real
   translator it also discards the translator's own hot code and every other guest translation.
   **`UNRESOLVED`** — the experiment: run a large, I-cache-resident synthetic workload (≥ 256 KiB of
   hot code) and measure its slowdown when an `IC IALLU` is injected at 10/100/1000 Hz.
3. **The per-`IC IVAU` cost is not linear and the sequence has a large fixed floor.** One line costs
   123 ns above the barrier floor; 64 lines cost 505 ns; 1024 lines cost 8 724 ns. Fitting the top
   end gives ~8.6 ns/line with a negligible intercept; the small end is dominated by the `DSB ISH`
   completion latency (~120 ns) that a single `IC` still has to wait out. **Invalidating one line
   costs a quarter of what invalidating a whole page costs.** Batch aggressively.
4. **The solo `IC IVAU` figure is multimodal and reproducibly so.** Across 24 independent solo
   brackets the 4 KiB figure clusters tightly at **~421 ns**, **~520–610 ns** and **~740–760 ns**,
   with almost nothing between. It is not noise (each is a median of 21 rounds × 1024 passes) and it
   is not quantisation (the counter quantum here is 0.04 ns). `INFERRED`: it depends on how many
   cores are in a state that must acknowledge the inner-shareable broadcast. **This is why §4.5's
   ratios are quoted against an adjacent freshly measured solo run and never against a stored one.**
5. **The whole SMC turnaround for one 4 KiB code page is 1.65–2.0 µs**: rewrite 64 words (24 ns) +
   canonical maintenance (850 ns) + execute the result (269 ns), measured end-to-end at 1788 ns with
   a warm-execute floor of 269 ns. **The stores are 1.4 % of it. The maintenance is ~50 %. The
   I-cache refill is the rest.**
6. **Executing 4 KiB of code after invalidating it costs 920 ns against 269 ns warm** — the refill is
   ~650 ns per 4 KiB page, comparable to the invalidation itself. Any accounting that charges only
   the `IC` instructions undercounts by ~2×.

### 4.5 Scaling to 2, 4 and 8 vCPUs — the number nobody had

vCPU 0 measures a 4 KiB region; vCPUs 1..N−1 contend. Every N is bracketed by a freshly measured
N=1 run (the `19` protocol). Three full sweeps; the table gives the ratio to the adjacent solo.

**`IC IVAU` ×64 + `DSB ISH` + `ISB` over 4 KiB, ratio to adjacent solo:**

| Contenders are… | N=2 | N=4 | **N=8** |
|---|---|---|---|
| idle-spinning | 0.96 – 1.02 | 0.83 – 1.04 | **0.70 – 0.72** |
| walking their own 4 KiB pages | 0.70 – 1.07 | 0.95 – 1.34 | **0.66 – 0.82** |
| executing their own 4 KiB code region | 0.88 – 1.44 | 0.90 – 0.98 | **0.74 – 0.84** |
| **executing the very lines being invalidated** | 0.76 – 0.99 | 0.66 – 1.05 | **0.62 – 0.93** |
| **issuing their own `IC IVAU` storms** | 1.03 – 1.26 | 1.39 – 6.70 | **2.16 – 2.40** |

Same experiment, other sequences, at N=8 with `IC`-storming contenders:
`IC IALLU` **1.60 – 4.07×**, canonical **2.00 – 2.33×**, the whole rewrite+maintain+execute
turnaround **1.71 – 1.85×**, `DC CVAU` **1.03 – 1.08×** (essentially immune), the warm-execute floor
**1.00 – 1.07×**.

**The interpretation, and it is the most decision-relevant thing in §4:**

> **A broadcast I-cache invalidate is not made more expensive by other cores merely running — not
> even by other cores executing the exact lines you are invalidating. It is made more expensive only
> by other cores also doing cache maintenance.**

* **The true cross-core I-cache shootdown is free.** Seven vCPUs executing the same 4 KiB region
  while vCPU 0 `IC IVAU`s every line of it: **0.62–0.93× of solo**, i.e. no penalty at all. This is
  the case `09` N-05 was worried about and it is a non-event on this hardware. `INFERRED` as to
  mechanism: the I-cache is PIPT and non-writable, so a DVM instruction-invalidate is a
  fire-and-forget snoop that does not have to wait for a coherence handshake the way a TLBI's `DSB`
  does.
* **It is the exact opposite shape from `TLBI`.** `19` items 7–8 measured `TLBI VAE1IS` at
  **3.7–4.5×** with seven merely-busy vCPUs, and 1.6–2.0× under true shootdown. I-cache maintenance
  is 0.7–0.9× under both. **The corpus should stop lumping "TLB and I-cache maintenance" together.
  They have opposite SMP behaviour.**
* **The one regime that does degrade — everyone invalidating at once — is real but bounded and
  self-limiting.** 2.2–2.4× at 8 vCPUs. Note that this is the regime a *translator* is in when eight
  vCPUs are all translating new code simultaneously (a cold start, or eight JITs warming up). Budget
  for it; do not design around it.
* One sweep produced a 6.70× outlier at N=4 in the `IC`-storm arm; the other two sweeps read 1.39×
  and 1.59× at the same point. It is reported rather than dropped; the N=8 column, where all three
  sweeps agree to within 11 %, is the one to quote.
* All numbers here are **P-cores only** (`QOS_CLASS_USER_INTERACTIVE`); `19` item 20 shows E-core
  placement changes vCPU clock by up to 3×. 8 vCPUs on 8 P-cores is the configuration measured.

---

## 5 (E) — Interaction with the shadow-PT coherence protocol

Doc 20 is being written concurrently and owns the guest-page-table side. This section states only
what falls out of *my* measurements about the interface between the two. It does not attempt to
specify doc 20's mechanism.

### 5.1 They cannot be separate mechanisms, because there is one bit

A shadow stage-1 L3 descriptor has **one** `AP[2]`. There is no way to write-protect a frame "for
page-table reasons" and independently "for code reasons". Whichever subsystem clears the bit
un-protects the page for the other. Therefore:

> **One protection bit, one fault handler, one reverse map, and a per-frame *kind set* — not a kind
> enum.** Protection is refcounted by kind; a frame is writable in the shadow tables iff no
> subsystem claims it.

`MEASURED` cost of the discrimination: **+1.95 ns on a 28.32 ns fault (+6.9 %)**. The handler reads
`FAR_EL1`, indexes the shadow L3 (a load it must perform anyway to change the descriptor), masks out
the output address, and loads one byte from a dense per-frame table. That table is **1 byte per
4 KiB frame = 256 KiB per GiB of guest RAM = 0.024 % overhead**, and a byte holds 8 kind bits.
204 800 of 204 800 faults classified correctly in the probe.

An alternative implementation — a hash table keyed on GPA — would be a probe plus a chain walk on a
cold line, i.e. tens of nanoseconds on a 28 ns fault. **Use the dense array.** At 0.024 % it is not
worth thinking about.

### 5.2 The two subsystems want opposite responses to the fault, and the numbers say to give them opposite responses

`MEASURED`, from §4.2 and §4.3:

| Response | Cost | Right for |
|---|---|---|
| Emulate the write in place, leave the page protected | **29.3 ns** (fault + decode + alias store) | **Page-table writes.** You must observe *every* PTE write; the next one is likely imminent; and emulation gives you the old and new values, which is what a shadow-PT protocol needs. |
| Unprotect, retry the store, re-protect later | **28.3 ns + 280 ns + 280 ns = ~590 ns** amortised over the whole write burst | **Code writes.** The writer is almost always a `memcpy`/`memset`/JIT emit that will write the page many more times. One fault for the burst instead of one per store. |

The ratio is 20×, which means getting this wrong in either direction is expensive:

* Un-protecting on a *PTE* write costs ~590 ns and then you must re-protect before the guest's next
  PTE write or you miss it — a race you cannot win without stopping the vCPU.
* Emulating every store to a *code* page costs 29 ns per store; a 4 KiB JIT emit is 512 8-byte
  stores = **15 µs**, against 590 ns for unprotect-and-let-it-run. **26× worse.**

So the classifier is not an optimisation. It is load-bearing.

### 5.3 What "the same handler must tell them apart" actually costs, end to end

Handler skeleton, with `MEASURED` costs:

```
  exception entry + ERET, nothing else                                28.32 ns   (91 cyc)
  + MRS FAR_EL1, MRS ESR_EL1, LDR the faulting instruction word       +0.65 ns
  + index shadow L3, mask OA, LDRB the frame's kind byte              +1.95 ns
  + branch on kind:
      kind & CODE  -> invalidate this frame's translations,
                      clear AP[2], TLBI VAAE1IS, ERET without
                      advancing ELR (the store retries)               +250..282 ns
      kind & PT    -> decode Rt (ISV=0, so from the insn word),
                      store through the writable alias, advance ELR   +0.98 ns
      both         -> do the CODE work AND the PT work
```

The `both` case (a frame that is simultaneously a guest page table and guest code) is rare but
well-defined and costs the sum. It is not a special case in the code, because kinds are a set.

### 5.4 Three shared requirements that neither document can drop unilaterally

1. **Physical-frame keying and a reverse map.** `text_poke` writes through `poking_mm` at a
   different virtual address (`SOURCE`, §2.3); uprobes replace the physical page entirely
   (`SOURCE kernel/events/uprobes.c:467-556`). Doc 20 needs the same rmap for the aliasing reasons
   `06 §2.4` gives. **It is one structure and one cost, not two.** `06 §2.4` calls the rmap "the
   single most-underestimated piece of shadow paging"; charging it once instead of twice is a small
   consolation.
2. **An instruction decoder in the fault handler.** Forced by `ESR_EL1.ISV = 0` (`MEASURED`, §4.2)
   for anything that emulates a store. Doc 20 needs it for PTE writes; we need it for the
   emulate-in-place path and for the "wrote the page currently executing" case. One decoder.
3. **Refcounted un-protection.** A rule, not a mechanism: *un-protecting a frame is legal only when
   the requesting subsystem's kind bit is the last one set.* Without it, doc 20's optimisation of
   dropping protection during a large page-table build (a `fork`) silently breaks SMC detection for
   any frame that is both.

### 5.5 The one place they genuinely conflict

Doc 20 will want to **batch or defer** `TLBI`s, because `16 §2` showed the invalidate is 59 % of a
shadow fill and `19` showed it is what breaks SMP scaling. SMC detection **cannot** defer the
`TLBI` that un-protects a code page, because `MEASURED` §4.3 shows the stale read-only entry is
cached 100 % of the time and the guest's store will fault forever.

The resolution is asymmetric and follows from the measurement: **defer the re-protect, never the
un-protect.** Un-protection is on the critical path of a faulting guest instruction and must
complete; re-protection is discretionary and can be batched arbitrarily, or skipped until the next
translation from that frame. That is §6.3's rule, arrived at from the doc-20 side.

---

## 6 (F) — Recommendation, and a cost model

### 6.1 The mechanism

1. **Write-protect at 4 KiB guest-physical granularity in the shadow stage-1 tables**, keeping
   entries **present-but-read-only**, never invalid. `MEASURED`: 28.3 ns per fault versus `16`'s
   94.4 ns for a translation fault. `14 §G-1b` proved 4 KiB permission enforcement works inside one
   16 KiB host page, so `06 §8.3`'s "under design (A) mprotect granularity is 16 KiB → 4×
   amplification" does not apply to us and `12` rank 5's "4× false traps at 16 K" should be struck.
2. **One mechanism shared with doc 20**, with a dense per-frame kind bitmask (1 B/frame, 0.024 % of
   guest RAM). `MEASURED` classification cost: **+1.95 ns**, 6.9 % of the fault.
3. **Honour same-processor SMC exactly** — it is free, because we detect the write, not the fetch
   (§1.3). **Honour XMC exactly too** — `MEASURED` §4.5 says cross-core I-cache invalidation costs
   nothing on this hardware, so there is nothing to buy by punting.
4. **Never re-protect eagerly.** The un-protect `TLBI` is mandatory (`MEASURED`, 51 200/51 200) and
   the pair costs 500–565 ns. Re-protect a frame lazily, when we next translate from it. This turns
   "one 590 ns cycle per write" into "one 590 ns cycle per write-burst".
5. **Use `IC IALLU` above ~1 KiB, `IC IVAU` below.** `MEASURED` crossover; 41× at 64 KiB.
   ⚠ Subject to the collateral-damage caveat in §4.4 item 2; until that is measured, set the
   threshold conservatively (≥ 16 KiB, or ≥ 4 pages in one batch) and make it a tunable.
6. **Do not hash code pages.** §3.4. Replace `06 §8.3`'s checksum rung.
7. **For the pathological code+data page, de-optimise the writer, not the page.** The fault gives us
   `ELR_EL1`, i.e. the guest instruction that wrote. Re-translate *that* block with an inline
   64-byte-granularity dirty-bitmap update on that store (64 bytes because `MEASURED`
   `IminLine = 64 B`), then un-protect the page permanently. This is DynamoRIO's sandboxing applied
   selectively, and it fits the measured shape of the problem: in every real instance (HotSpot
   nmethod headers, BPF `prog_pack` neighbours, V8 `MemoryChunk` flags) **the writer is a small,
   identifiable, hot piece of guest code** — which is exactly the thing a DBT is good at
   specialising.
8. **Treat page-lifetime events as first-class, separately from byte-level SMC.** `INFERRED` from
   §2.4: the largest invalidation event in a Linux boot is a code page being freed and reused.
   Emulated-device DMA into a formerly-code frame is the same event (`06 §8.4`'s last warning) and
   must go through the same path.

### 6.2 The revised escalation ladder (replacing `06 §8.3`)

| Rung | Trigger | Action | `MEASURED` cost |
|---|---|---|---|
| 0 | frame has translations | present + read-only in every shadow PTE mapping it | 0 when nothing writes |
| 1 | first write fault | invalidate this frame's blocks; un-protect (`TLBI` mandatory); retry the store; **do not re-protect** | 28 ns + 280 ns |
| 2 | frame is translated from again | re-protect (descriptor edit + `TLBI`) | 280 ns |
| 3 | frame oscillates 1↔2 more than N times per epoch | stop protecting it; instrument the writing block (from `ELR_EL1`) with a 64-byte dirty-bitmap store | a few instructions per store in one block |
| 4 | the writing block cannot be identified (many writers) | leave unprotected; validate blocks from that frame against a shadow copy on entry — **early-exit compare only** | 0.65 ns on mismatch in the first 16 B; 118 ns worst case |
| 5 | frame is freed / re-purposed / DMA target | drop all translations for the frame; clear its `CODE` kind bit | unmap path, no fault |

Rung 4 is the one I would build last and hope never to need; §3.4 explains why it cannot be the
default. Note it is a *validator* on an already-suspect page, not a detector.

### 6.3 Cost model

The unit is a **frame-write-burst**: one guest write burst to one 4 KiB frame that held translated
code. `MEASURED` components:

```
  permission fault + classify + decide                    30.3 ns
  un-protect: descriptor edit + TLBI VAAE1IS + barriers  280   ns
  re-protect (lazy, only if re-translated):              280   ns
  ------------------------------------------------------------
  detection subtotal                                    ~590   ns  per frame-write-burst
  I-cache maintenance for the frame (canonical, 4 KiB)   490 - 850 ns
  I-cache refill on first re-execution                  ~650   ns
  ------------------------------------------------------------
  total non-translation cost                            ~1.7 - 2.1 us  per frame
  + re-translation of the frame's blocks                 not measured here
```

The re-translation term is the one I did not measure and it is likely the largest for a
JIT workload. `15 §4.1` gives QEMU's 20 target bytes/TB and 316 host bytes/TB; `15 §4` gives 12.7 %
of boot time in codegen. Anyone using this model must supply their own translation-throughput number.

**(i) Boot.** `MEASURED`: 17 100 TB invalidations. At QEMU's 20 target bytes/TB and ~200 TBs per
4 KiB page, and allowing for partial coverage, the invalidations span an estimated **1 500–4 500
distinct frames** (`INFERRED`; the histogram experiment in §2.4 would replace this with a
measurement). At 1.9 µs/frame that is **2.9–8.6 ms**. Boot on a translator targeting `15 §8c`'s
defensible 2–4× over QEMU TCG lands at roughly **0.6–1.1 s**. Detection + maintenance is therefore
**0.3–1.4 % of boot**. Re-translation of 17 100 blocks against a boot that translates ~88 000 is
+19 % of translation work; at `15 §4`'s 12.7 % codegen share that is **~2.5 % of boot**.
> **Boot: 3–4 % of boot time, one-off.** `06 §8`'s "5–20 %" is 2–5× pessimistic for boot.

**(ii) Steady-state server workload.** `MEASURED`: **zero** invalidations across 37.5 s of compute,
process-creation and application work. The residual cost of the mechanism when nothing writes is
**exactly zero** — a present-but-read-only descriptor costs nothing on loads or instruction fetches,
and the only stores that fault are the ones we want.
> **Steady state, no JIT and no module churn: < 0.1 %.** Not "5–20 %". This is the finding that most
> changes the corpus's risk register.
>
> Two things are *not* covered by that measurement and must be budgeted separately:
> **module loading** (median 1 page of fresh code per module, `MEASURED` §2.5 — a mapping event, not
> an SMC event) and **executable page reclaim from a disk-backed filesystem** (unmeasured; this
> guest has no disk). Neither is byte-level SMC; both are page-lifetime events (rung 5).

**(iii) JIT-heavy workload.** This is a parametric model, because **the emission rate is not
measured anywhere** (§2.5). Let *E* be bytes of guest code emitted per second and *F* the number of
benign data writes per second landing on frames that also hold code.

```
  cost/s  =  (E / 4096) x 1.9 us          [new/rewritten code frames]
          +  F_burst x 0.59 us            [benign writes, one per burst, rung 1-2]
          +  translation of E bytes       [unavoidable for any DBT]
```

At *E* = 1 MB/s the first term is 0.05 % of a core; at 100 MB/s it is 4.8 %. **The first term is not
the problem.** The problem is the second: if the escalation ladder fails and a HotSpot GC epoch bump
faults on 2 000 nmethod-header frames with eager re-protection, that is 1.5 ms; at 100 Hz it is
**15 % of a core** (§3.5). With rung 1–2 (lazy re-protect) it is 2 000 × 0.59 µs = 1.2 ms at the same
rate — still 12 %. **Only rung 3 (instrument the writer) fixes it**, and rung 3 is the piece of this
design that is not yet built or measured.
> **JIT-heavy: 5–15 % if the escalation ladder is not built; plausibly 1–3 % if rung 3 works. This
> is the residual risk of the whole design and it is UNRESOLVED.**

### 6.4 What this changes in the corpus

* **`06 §8`'s "difficulty 5, catastrophic if wrong, 5–20 % if right" should be re-scoped.** The
  *steady-state* figure is `MEASURED` at ~0 for a non-JIT guest and the boot figure at 3–4 %. The
  difficulty rating stands, but the risk is concentrated entirely in JIT guests and specifically in
  the code+data page case, not in kernel self-patching.
* **`06 §8.3`'s escalation ladder loses its checksum rung** (§3.4) and gains a page-lifetime rung.
* **`06 §8.3`'s "16 KiB granularity → 4× amplification" no longer applies**, per `14 §G-1b`.
  **`12` rank 5's "4× false traps at 16 K" should be struck.**
* **`09` N-05 should be downgraded.** Its concern was that ARM's non-coherent I-cache "compounds"
  code-cache invalidation and that at 16 KiB granularity "the trap rate could be brutal". `MEASURED`:
  the granularity is 4 KiB, the trap rate on a real Linux guest after boot is zero, the maintenance
  is 490–850 ns per page, and — the part it got most wrong — **cross-core I-cache invalidation costs
  nothing** (§4.5). Its "single best paravirtualization opportunity" claim should be retired in
  favour of `07 §3.2`'s grade C, which is now backed by measurement.
* **`06 §8.4`'s list of kernel self-modifiers is right but its cost implication is wrong.** All of it
  happens before the code has been executed and therefore before it has been translated (§2.2).
  The `.parainstructions` entry should be removed — it no longer exists.
* **New: `19`'s TLBI conclusions do not transfer to I-cache maintenance.** They have opposite SMP
  behaviour and must be modelled separately (§4.5).

---

## 7 What is unresolved, and the experiment for each

1. **Attribution of the 11 800-invalidation late boot burst.** `INFERRED` to be
   `free_init_pages()` poisoning `.init.text`. **Experiment:** patch `tb_phys_invalidate()` to log
   `(page_addr, writing RIP)` and histogram. Half a day; also yields the frame count the §6.3 boot
   model currently estimates.
2. **The frame count behind the 17 100 TB invalidations.** Same experiment. The boot cost model has
   a 3× range purely because of this.
3. **`IC IALLU`'s real collateral cost.** §4.4 item 2. **Experiment:** a workload with ≥ 256 KiB of
   hot code, with `IC IALLU` injected at 10/100/1000 Hz, measuring the workload's slowdown.
4. **A JIT guest.** Every steady-state measurement here is on a guest with no JIT. **Experiment:**
   add `openjdk21-jre` (or a Node build) to the Alpine initramfs and re-run `qemu_smc.py`. This is
   the single highest-value follow-up in this document; it converts §6.3(iii) from a parametric model
   into a measurement.
5. **Executable page reclaim.** This guest has no disk, so executable file pages are never evicted
   and re-read. **Experiment:** add a virtio-blk root filesystem and run a workload with a working
   set larger than RAM.
6. **Rung 3 (instrumenting the writing block).** Not built, not measured. It is the only thing that
   fixes the code+data page case and the whole JIT risk rides on it.
7. **`ftrace=function` was inconclusive** — I could not confirm the Alpine `virt` kernel has
   `CONFIG_FUNCTION_TRACER`. **Experiment:** boot a kernel where `available_filter_functions` is
   non-empty and toggle the tracer while sampling `info jit`.
8. **The multimodal `IC IVAU` solo cost** (~421 / ~560 / ~750 ns for the same 4 KiB, reproducibly).
   Cause unknown. It does not affect the ratios (which are bracketed) but it means any absolute
   I-cache figure quoted from this document carries a ±40 % band.
9. **One machine, one OS.** M2 Max, macOS 26.5.2. `CTR_EL0.{IDC,DIC}` are `0` here; a part with
   `FEAT_DIC`/`FEAT_IDC` would delete half or all of §4.4's cost. Re-read `CTR_EL0` on M1/M3/M4
   before treating 490–850 ns/page as a platform constant.
10. **`ESR_EL1.ISV = 0` was measured only for `STR Xt, [Xn]`.** Other store forms (`STP`, `STUR`,
    unaligned, atomics) were not tested. The decoder in §5.4 must handle all of them; its size and
    cost are unmeasured.

---

## Reproduce

```sh
cd probes
./build.sh smc/smc.c
./smc/bin/smc info      # CTR_EL0, clock, EL2-trap census
./smc/bin/smc wp        # write-protect fault, five handler strategies + matched control
./smc/bin/smc cycle     # full protect/fault/unprotect/write/re-protect + components
./smc/bin/smc icache    # dc/ic/iallu/canonical/exec-after-invalidate, 64 B .. 64 KiB
./smc/bin/smc smp       # the same at 1/2/4/8 vCPUs, five contender kinds
./smc/bin/smc hash      # byte-compare and checksum throughput over a 4 KiB page

cd smc
./qemu_smc.py --all                                   # boot + bench + spawn + 7-zip + smp4
./qemu_smc.py --poll 0.02 --loglevel 7 \
              --extra-append "printk.time=1" --task none --label boot.verbose
```

`qemu_smc.py` needs `probes/qemu_baseline/work/` (fetched by that probe's `build.sh`) and
`qemu-system-x86_64` 11.0.3. Raw logs and the JSON time series are in `probes/smc/results/`.
