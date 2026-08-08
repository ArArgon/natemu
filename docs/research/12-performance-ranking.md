# 12 — Performance Concerns Ranked + Tracking

> **CORRECTIONS (2026-08-08), see `13-audit-skeptical-review.md` and `14-gate-results-verified.md`:**
> - **Rank 6 (PTE-quad homogeneity) is void.** 4 KB granule is available and enforced at both
>   translation stages, so the "15-35% of the MMU win lost" penalty does not apply and
>   `natemu-page-quads` is not needed.
> - **Rank 2 (TSO) is upgraded to behavioural evidence**: MP litmus gave 1,706,240 reordering
>   outcomes at `EnTSO=0` and 0 at `EnTSO=1`. The *cost* of running with TSO on remains unmeasured.
> - **Rank 8 (A/D + TLB reach) is under-ranked.** `HAFDBS = 0` is now the only remaining
>   hardware-assist gap in the MMU path. Also newly known: only **256 ASIDs** vs x86's 4096 PCIDs
>   (`09` N-11), which is not costed anywhere in this table.
> - **The "Net: Tier 2 2.5-5x" line double-counts.** Captive's 2.21x is already end-to-end (its JIT
>   *and* its host-MMU offload); multiplying it by a separately-sourced codegen factor is not sound,
>   and the multiplicands come from different guests, hosts and QEMU vintages. Replace with a single
>   end-to-end comparison against a QEMU TCG baseline we measure ourselves.
> - **Rank 1's `1.74x` softMMU microbenchmark is a lower bound on softmmu cost, not an estimate of
>   it** — a hot fully-predicted loop omits I-cache bloat, slow-path mispredicts and register
>   pressure. Its agreement with HSPT's 1.51x is coincidence, not corroboration.

**Date:** 2026-08-08 · **Host:** M2 Max/Pro, macOS 26.5.2, HV 259.5.15, `hw.pagesize 16384`
**Source:** live probes dispatched 2026-08-08 (multi-agent), artifacts in `/tmp/probe_mmu_tso.c`, `/tmp/probe_dram2.c`, `/tmp/hv_g1_probe/run.log`, `tools/g2_quad_measure.py`

Ordered by **whole-program impact Tier 2 vs QEMU TCG** (SPECint → kernel_build). `V` = verified live.

---

## Ranked

| Rank | Concern | Impact | Live probe | Mitigation |
|---|---|---|---|---|
| **1** | **Guest `ld/st` → host MMU** `05:390` `06:169` | **1.5× geomean (HSPT 1.51×, Captive 2.21× INT, 32× TLB)**. 9–11 insn/access (3 loads) vs 0 on shadow `06:172` | `V` softMMU 9-insn **0.466 ns vs `ldr` 0.267 ns = 1.74×** (`probe_mmu_tso.c`). Captive `67` vs `40` bytes `03:148` but `3.44×` faster blocks | Shadow paging `TCR_EL1.TG0/TG1=4K` `V` (`probe_g1_tgran_tso.c` `TGran4=0x0`) + `hv_vm_map` 4K `V` + `TTBR1` kernel-half `06:182` |
| **2** | **TSO barriers** `01:476/498` | **2.4–3.4× on L1** (`ldr/str 1.00×` vs `ldapr/stlr 2.38×` live, `3.43×` in 01:6.3) for `2–5%` host EnTSO `01:432` → net `~1.3–1.5×` on mem-heavy, `~1.1×` geomean | `V` `ldapr 0.409 ns 1.53×` vs `ldar 0.365 1.37×` → `ldapr==ldar` (no win, `LRCPC` correctness only `01:476`); `stlr 1.56× vs str`; DRAM `ldapr 1.14×` warm, cold `0.68×` (MLP preserved) | HW `EnTSO` in VM `V` (`hv_vcpu_set_sys_reg` + guest `msr actlr_el1` coherent `probe_g1_tgran_tso.c:121`) — erase `dmb ish` `05:352` |
| **3** | **Codegen quality** `05:530` `03:148` | **`~4.4×` QEMU→Rosetta** (QEMU 16%→Rosetta 71% 7-zip), QEMU `17.4` vs Captive `~10` host/guest, Instrew `53%` vs QEMU `716%` same-ISA `05:576` | Capture vs TCG `17.4` vs `~10`; `FEX` 26% / `box64` 53% / `QEMU` 16% `05:530` | Take `FEXCore` `90K` MIT `05:38` (SRA+FlagM `06:372`), `CRC32CX` `6`-insn hash `04:630` + `BL/RET` shadow `04:608` next |
| **4** | **Precise exceptions** `06:313/430` | **`10–20%` vs `2–5×`** (spill 16 stores/insn `06:430` vs StackMap `100–300 cyc` on fault `06:323`) | `10k faults/s ×200cyc=0.06%`, `1M×200cyc=5.7%` model | StackMap (`hostPC→guest RIP/GPR/lazy-flag`) + `load-all→compute→store-all` `06:348` + `AT S1E1R` `10–20cyc` `06:346` |
| **5** | **SMC / code-cache** `06:86` `09:50` | **`5–20%` if wrong**, `4×` false traps at 16K | `phys`-indexed cache `03:100` reuses across `fork`/`so`; `mprotect` vs `checkCRC` `05:507` | `phys` cache + rmap + checksum escalation |
| **6** | **PTE-quad homogeneity** `11:35` | **15–35% of MMU win lost** (`H=55–87%` → fallback `f=0.25–0.45`, `1.1–1.5 ns` extra/ld `→0.4–0.7cyc` CPI `+25–45%` on mem) | `V` `tools/g2_quad_measure.py --synthetic` `55.1%`/`66.5%`/`86.9%` hetero `90% pfn_gap` | `natemu-page-quads` order-2+THP `~300 LoC` `11:130` |
| **7** | **Exits / devices** `02:652` `10:169` | **`1.2M/core` `3M/machine`** budget; legacy IDE/e1000 `8–10×` vs virtio `312` vs `81K` notifies `10:149` | `V` `808 ns` HVC `890 ns` MMIO `3.1 µs` IPI `313 ns→27 µs` `hv_vm_protect` cliff `02:652` | `x2APIC` MSRs `0x800–0x8FF` `10:57` + in-VM LAPIC `10:62` (`0 exits`), few large `hv_vm_map` extents |
| **8** | **A/D + TLB reach** `06:204/225` | **`2` faults/page/epoch `0.15s/GiB`** + **`12→48 MiB` 4× loss** (3072+256 entries) `06:225` | `500–2000cyc/miss` model `06:182`, `1 GiB sweep 0.26–1.05 Gcyc` | `A` eager (legal `06:112`) → `1` fault/page, `2M` blocks `128 quads` `06:182`, `FEAT_TTL` `06:227` |

**Net:** MMU+TSO `1.5–2.2×` `08:91` + codegen `1.3–1.8×` → **Tier 2 `2.5–5×`** `08:28` (SPECint `2.21×` → kernel_build near `32×` `07:345`). Without `page-quads` falls to `1.6–3×`.

---

## Tracking — cheapest signal first

| Time | What to run | Command / artifact | Expected |
|---|---|---|---|
| **Next 1 day** | G-2 real dump | `qemu-system-x86_64 -qmp unix:/tmp/qmp.sock … -hmp unix:/tmp/hmp.sock` → `echo 'info registers' + 'pmemsave 0 0x100000000 /tmp/guest.bin'` → `python3 tools/g2_quad_measure.py --dump /tmp/guest.bin --cr3 0x… --json` `11:60` `tools/g2_quad_measure.py:217` | Confirm `H 55–87%` → `page-quads` mandatory `<70%` |
| **With M1** | SoftMMU vs TSO ablation | `clang -O2 -arch arm64 /tmp/probe_mmu_tso.c && /tmp/probe_mmu_tso` (already `V` 1.74×/2.38×) + `perf stat -e dtlb_load_misses` on `make -j` vs SPECint | `dtlb_misses` × shadow-miss model `06:182` → faults/sec budget |
| **With HV** | Stage-2 analogue | `/tmp/probe_hv_fault.c` `hv_vm_map 4K 0.6 µs` + `hv_vcpu_run` data-abort `11.5 µs` host path → in-VM `~0.5 µs` proves `hv_vm_protect` not on page-fault path | Validate `500–2000cyc` `06:182` |
| **Codegen** | `FEX` vs `TCG` | `7-zip`/`SPECint` under `M1` `FEX 26%/box64 53%/QEMU 16%` `05:530` + `CRC32CX`/`BL-RET` ablation | Isolate `~10` vs `17.4` gap `05:390` |

Live probe logs: `/tmp/probe_mmu_tso.c:313` / `/tmp/report_mmu_tso.md` / `/tmp/d3.c` (quad degraded `+110%`/load). G-1 already `V` (`TGran4` `V`, EnTSO `V`).

---

## Minimal win order (ponytail)

`x2APIC MSI-X` `10:57` → `EnTSO`+`SRA/FlagM` `01:36` `06:372` → `CRC32/BR` `04:630` → `phys-cache` `03:100` → in-VM LAPIC `10:62` — first two decide budget.

*Generated 2026-08-08.*
