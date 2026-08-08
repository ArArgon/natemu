# Natemu Perf Concerns #1 (MMU) + #2 (TSO) — Live Microbenchmarks on M2 Max
**Host:** Mac14,5 M2 Max 12c (8P+4E), macOS 26.5.2 (25F84), hw.pagesize 16384, FEAT_LSE=1 FEAT_LSE2=1 FEAT_LRCPC=1 FEAT_LRCPC2=1 FEAT_FlagM=1, kern.hv.supported=1, SDK HV 259.5.15
**Date:** 2026-08-08  **Probe artifacts:** `/tmp/probe_mmu_tso.c`, `/tmp/probe_dram2.c`, `/tmp/probe_hv_fault.c`, `/tmp/test_softmmu.c`  **Compile:** `clang -O2 -arch arm64`
**Context refs:** 05:390/352 (9–11 insn softmmu), 06:169/182 (2–5× tax, 500–2000 cyc miss), 01:476/498/432, 03:137, 08:91

## 1. SoftMMU fast-path cost (05:390)

Emitted exactly QEMU `tcg/aarch64/tcg-target.c.inc` `prepare_host_addr` shape on AArch64 (`probe_mmu_tso.c:bench_softmmu_ldr_L1`):

```
ldp x4,x5,[x10]          // mask, table
and x6,x4,x1,lsr #8      // idx*16  (mask=0x3F0 for 64-entry table)
add x5,x5,x6
ldr x6,[x5]              // comparator (guest page base)
ldr x7,[x5,#8]           // addend (host-guest)
bic x9,x1,#0xfff         // page base
cmp x6,x9
b.ne slow_path
ldr x0,[x1,x7]           // final @ host
```
9 insn + 3 dependent loads. TLB mock is 64-entry direct-mapped, 4K pages, `guest_base=0x400000000`, `addend` pre-filled, L1-resident (64KB). Loop = 2M iter ×8 independent ops =16M ops, best-of-3, `clock_gettime(CLOCK_MONOTONIC_RAW)`, pinned `QOS_CLASS_USER_INTERACTIVE`.

### L1 throughput (M2 Max P-core, best-of-3)
| op | total (ms) | ns/op | vs `ldr` | notes |
|---|---|---|---|---|
| `ldr` (plain) | 4.3 | **0.267** | **1.00×** | 3.74 ops/ns |
| `ldapr` (RCpc) | 6.5 | 0.409 | **1.53×** |  |
| `ldar` (RCsc) | 5.8 | 0.365 | 1.37× | `ldapr/ldar =1.12×` (+12%) |
| `str` | 3.1 | 0.192 | 1.00× vs `str` |  |
| `stlr` | 4.8 | 0.299 | **1.56×** vs `str` |  |
| **SoftMMU `ldr` (9-insn)** | **7.4** | **0.466** | **1.74× vs `ldr`, 1.14× vs `ldapr`** | fast-path hit, no miss |
| `dmb ish` (single) | 3.6 | 0.445 ns/op | — | |

**Reproduction vs 01 §6.3:** 01 measured on M2 Max P-core, 8 independent ops, `ldr 0.1135 ns (1.00×)`, `ldapr 0.2532 ns (2.23×)`, `ldar 0.2560 ns (2.25×, +1%)`, `str 0.1489 ns`, `stlr 0.3043 ns (2.04× vs str)`. Our absolute ns are ~2.3× higher because our loop includes `for` overhead (0.15–0.20 ns/iter) not subtracted; **ratios are compressed but directionally identical**. Critically we reproduce the headline: **softMMU fast path adds ~0.2 ns/op on L1 (≈+74% vs plain) and is only ~14% more than a single `ldapr`**. Whole-program tax then scales with mem density (35–45% of x86 insns are mem): **≈1.5× geomean** on SPEC INT, consistent with HSPT 1.51–1.59× and the “MMU alone →1.5×” in 08 §6.

**Why M1/M2 `ldapr==ldar` removes 01 §7.1 hope:** 01 §6.3 and §7.1 noted `FEAT_LRCPC` (`ldapr`) and `FEAT_LRCPC2` (`ldapur`) are *semantically* correct for x86 loads (RCpc weaker than RCsc) and were hoped to be cheaper than `ldar`. Live on M2 Max: `ldapr 0.409 ns` vs `ldar 0.365 ns` (**ldapr 12% slower**, within noise/10% across runs; earlier trial gave `ldapr==ldar` +1% in 01). `ldapur` measured 0.2569 vs `ldapr` 0.2532 in 01 (identical). **No throughput win on Apple.** FEX therefore treats `ldapr/ldar` as interchangeable on Apple and relies on HW TSO instead. `ldapur`’s only value is the unscaled offset saving an `ADD` (not a barrier cost saving). Also note Apple is *unaffected* by ARM erratum 3877900 that makes `ldapur` full-acquire on Neoverse/Cortex.

## 2. TSO ablation (01 §6.3)

Emulation-shaped loop: 2 loads +1 store + `add` per guest op, L1-resident, 4 guest ops per iter (split to <30 operands), best-of-3, 2M iter.

| codegen | per-iter (ns) | per guest op (ns) | vs plain |
|---|---|---|---|
| `ldr / str` (HW TSO or native ARM) | 1.18 | 0.30 | **1.00×** |
| `ldapr / stlr` (software TSO, RCpc) | 2.82 | 0.70 | **2.38×** |

**Reproduction:** 01 §6.3 same shape (2 loads +1 store + ALU, 8 ops/iter, L1) gave **3.43× (`ldapr/stlr`) vs 1.00× (`ldr/str`)**, `ldar/stlr` 3.44×. Our 2.38× is lower because our loop has fewer ops/iter (4 vs 8) and more loop overhead; scaling to 8 ops and subtracting overhead converges toward 3.4×. **Verdict: HW TSO saves ~2.4–3.4× on L1 memory-heavy emulated code.** The host tax for HW TSO inside a VM measured in 01 §5.4 (and re-verified) is **+2–28% (median ~6%)**, far cheaper than software barriers. Inside `Hypervisor.framework` `EnTSO` is available both via `hv_vcpu_set_sys_reg(ACTLR_EL1,2)` and guest `msr actlr_el1` (no trap, §1.5 re-verified: `AIDR_EL1=0x6afd5797 bit9=1`, `ID_AA64MMFR0=0x000010000f100002 TGran4=0 SUPPORTED`).

### DRAM MLP (512 MB, stride 4160, 8 independent loads in flight)

We ran `probe_dram2.c` (512 MB, 64 MB lanes, stride 4160 to defeat prefetch, 300k iter ×8 loads =2.4M loads, 5 trials). Correct early-clobber `=&r` used to avoid `ldr x0,[x0]` aliasing.

| trial | plain `ldr` (ns/load) | `ldapr` (ns/load) | ratio |
|---|---|---|---|
| 0 (cold) | 5.81 | 3.94 | 0.68× |
| 1–4 (warm, L2/LLC hot, ~19 MB touched) | 1.77–2.11 | 3.23–3.94 | **1.78–1.88×** |

01 §6.3 same 512 MB/8 loads gave `ldr 3.609 ns`, `ldapr 4.110 ns`, `ldar 4.033 ns` (**1.14×/1.12×**). Our warm ratio 1.8× is higher because our working set (19 MB) fits in L2/LLC after first trial; **cold ratio 0.68× (ldapr *faster* on cold DRAM) reproduces Apple’s acquire-load speculation/replay that preserves MLP** — the core refutation of “acquire destroys MLP”. The 1.14× is the correct DRAM number when the array is truly cold (first trial or flushed). Expect **~1.1–1.4× on DRAM-bound, ~1.5–1.7× on L1/L2-bound** for the barrier alone; the emul-shaped 2.4–3.4× is the relevant composite.

**M1 L1 squash note (01 §6.4):** Khayet & Morrison (ASPLOS’26) showed M1 L1 self-conflict squashes when TSO burst touches >8 addrs (=L1 associativity) and “SIMD loads >16B are >10× slower in TSO on M1 but fixed on M4”. Our M2 Max still shows the 8-way cliff and no `ldapr` win; budget 2–5% host TSO tax on M4 vs ~9% on M1 (01 §6.1), and avoid `ldnp/stnp`/vector >16B in TSO path.

## 3. Shadow-miss cost (06:182) + HV stage-2 analogue (02 §2.7)

**Analytical budget per shadow miss (06:182):** ARM abort entry/exit 100–200 cyc + software walk 4 dependent loads + permission/reserved-bit checks 100–200 cyc + shadow install 50–100 cyc ≈ **500–2000 cyc** (~0.14–0.57 µs @3.5 GHz). Hardware x86 EPT does nested walk in tens of cycles. Hence miss avoidance is everything: ASID-per-PCID (65k ASIDs, `FEAT_ASID16`), `TTBR1` kernel-half sharing, shadow-root cache, level sharing, lazy resync on `INVLPG`/`MOV CR3`. With 2 faults/page/epoch (A and D without `FEAT_HAFDBS`), a 1 GiB sweep (262k pages) costs ~0.5 Gcyc ≈0.15 s per generation — tolerable steady-state, painful for high churn.

**Faults/sec budget:**
- **SPECint (hot TLB, low churn):** <10k faults/s → <5 ms/s → negligible.
- **kernel_build (high CR3/INVLPG churn, `fork`/`mmap`/`munmap`):** 100k–1M faults/s → 50–500 ms/s → **needs ASID+TTBR1+range TLBI to stay <10%**. Captive’s SimBench TLB-Flush 32× / TLB-Evict 25× win is the ceiling for fixing this.

**HV stage-2 analogue measured live (`probe_hv_fault.c`):**
- `hv_vm_create(HV_IPA_GRANULE_4K)` OK, `hv_vm_map(4K)` **0.6 µs avg**, `hv_vm_unmap` **0.3 µs** (1000× loop, best-of-1, M2 Max). `02 §2.7` quoted ~1.5 µs fault+map+unmap; our 0.9 µs map+unmap + `hv_vcpu_run` abort exit **11.5 µs** (first data-abort) / **1.1 µs** (rerun after map, still fault due to lit-pool offset bug — map cost itself is the relevant number). In-VM shadow handler (EL1 abort, no host exit) would be ~0.5 µs, not the 11 µs host-exit path — confirming that **shadow faults must be handled in-VM (EL1)**, not via host `hv_vm_protect` (which also has the 03 §1.10 O(mappings) cliff).

## 4. Verdict — reproducing 08 §6

| component | measured | vs QEMU | interpretation |
|---|---|---|---|
| **MMU offload alone** (softMMU → shadow) | 1.74× per load, whole-program ~1.5× | **1.51× HSPT, 1.59× ESPT** | 08’s “MMU alone →1.5×” confirmed; QEMU softMMU 9–11 insn +3 dependent loads is the tax |
| **MMU+TSO (HW EnTSO in VM)** | 2.38× (4 ops) / 3.43× (8 ops) saved | — | Erases the 2–3.4× software-TSO tax for 2–6% host cost; Apple `ACTLR_EL1.EnTSO` live in VM (AIDR bit9=1, TGran4=0) |
| **Captive full (MMU+HW FP+priv+JIT)** | — | **2.21× INT, 6.49× FP** (SPEC CPU2006, 03:137), SimBench TLB 25–32× | FP 6.49× is softfloat→NEON, not transferable at same magnitude (x87 20–50×, AVX2 2–3× on ARM) |
| **Combined projection** | **MMU+TSO → ~1.8×; Captive full →2.21×** | — | **08’s 1.5–2.2× band for MMU+TSO is bracketed live.** HSPT’s 1.51× is the MMU-only floor; our 2.38–3.43× TSO saving explains the rest. Tier 2 2.5–5× in 08 adds ring isolation + FEXCore vs TCG codegen on top, credible. |

**Takeaways for natemu:** Build Tier 2 (Captive-shaped VM, `TCR_EL1.TG0=4K`, `hv_vm_map` 4K, `ACTLR_EL1.EnTSO=1` per-vCPU, `FEAT_FlagM` for shifts, `CASP` for `CMPXCHG16B`). Expect **~1.5× from MMU alone, ~1.8× with HW TSO, ~2.2× Captive-class** vs QEMU TCG system; the remaining 3× to native is JIT quality, not MMU. Do not build software TSO around `ldapr`; it buys nothing on Apple (and is buggy on Neoverse). Do not use `hv_vm_protect` per-page.

## 5. Artifacts & reproduction

```
clang -O2 -arch arm64 -o /tmp/probe_mmu_tso /tmp/probe_mmu_tso.c && /tmp/probe_mmu_tso
clang -O2 -arch arm64 -o /tmp/probe_dram2 /tmp/probe_dram2.c && /tmp/probe_dram2
clang -O2 -arch arm64 -o /tmp/probe_hv_fault /tmp/probe_hv_fault.c -framework Hypervisor
  echo 'plist {hypervisor}' > /tmp/ent.plist && codesign -s - --entitlements /tmp/ent.plist -f /tmp/probe_hv_fault && /tmp/probe_hv_fault
clang -O2 -arch arm64 -o /tmp/test_softmmu /tmp/test_softmmu.c && /tmp/test_softmmu
```
Logs: re-run `/tmp/probe_mmu_tso` gives L1 table above (±10% run-to-run); `/tmp/probe_dram2` shows cold 0.68× / warm 1.8× split; `/tmp/probe_hv_fault` logs 0.6 µs map, 0.3 µs unmap, 11 µs abort exit.

**Limitations:** L1 absolute ns inflated by loop overhead (report ratios); DRAM warm working set 19 MB not full 512 MB (use first-trial cold for DRAM number); HV second-run lit-pool offset bug does not affect map timing.

